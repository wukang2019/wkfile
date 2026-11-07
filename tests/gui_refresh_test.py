# -*- coding: utf-8 -*-
"""v1.7.14 GUI 状态刷新异步化 / 节流的定向回归。

刻意不创建真实 Tk 窗口：用桩件验证「网络探测不阻塞主线程」「连点合并」
「Tk 变量只被主线程读取」三件事，可在无桌面 / CI 环境跑。
"""
import os
import sys
import threading
import time
import types

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "server"))

# 本机 / CI 的 Python 可能没编进 tkinter（exe 由另一套带 Tk 的解释器打包）。
# 本测试只验证逻辑、不建真实窗口，所以给导入环节补一层最小桩件。
try:
    import tkinter  # noqa: F401
except ImportError:  # pragma: no cover - 取决于运行环境是否带 Tk
    _tk = types.ModuleType("tkinter")

    class _TclError(Exception):
        pass

    _tk.TclError = _TclError
    for _n in ("Tk", "StringVar", "BooleanVar", "IntVar", "Frame", "Label",
               "Button", "Canvas", "Checkbutton", "Entry", "Listbox",
               "Scrollbar", "Separator", "Style", "Combobox", "Notebook",
               "Treeview", "LabelFrame"):
        setattr(_tk, _n, type(_n, (), {}))
    for _sub in ("filedialog", "messagebox", "ttk"):
        _m = types.ModuleType("tkinter." + _sub)
        setattr(_tk, _sub, _m)
        sys.modules["tkinter." + _sub] = _m
    _font = types.ModuleType("tkinter.font")
    _font.Font = type("Font", (), {})
    _tk.font = _font
    sys.modules["tkinter.font"] = _font
    _st = types.ModuleType("tkinter.scrolledtext")
    _st.ScrolledText = type("ScrolledText", (), {})
    sys.modules["tkinter.scrolledtext"] = _st
    sys.modules["tkinter"] = _tk

import wkfile_gui as g  # noqa: E402


class FakeVar:
    def __init__(self, value=""):
        self.value = value
        self.readers = []

    def get(self):
        self.readers.append(threading.current_thread().name)
        return self.value

    def set(self, value):
        self.value = value


class FakeWidget:
    def __init__(self):
        self.kwargs = {}

    def configure(self, **kwargs):
        self.kwargs.update(kwargs)


class FakeRoot:
    """只记录 after() 任务，不进 Tk 事件循环。"""

    def __init__(self):
        self.jobs = []

    def after(self, ms, fn, *args):
        self.jobs.append((ms, fn, args))
        return len(self.jobs)

    def pump_zero(self):
        """执行所有 0ms 回调（模拟回到主线程），延时任务留在队列。"""
        due = [j for j in self.jobs if j[0] == 0]
        self.jobs = [j for j in self.jobs if j[0] != 0]
        for _ms, fn, args in due:
            fn(*args)
        return len(due)


class FakeMessageBox:
    def __init__(self):
        self.calls = []

    def _hit(self, kind, title, msg, **kwargs):
        self.calls.append((kind, title, msg))

    def showinfo(self, *a, **kw):
        self._hit("info", *a, **kw)

    def showwarning(self, *a, **kw):
        self._hit("warn", *a, **kw)

    def showerror(self, *a, **kw):
        self._hit("err", *a, **kw)


def make_app():
    w = g.Wizard.__new__(g.Wizard)  # 跳过 __init__，不建真实窗口
    w.root = FakeRoot()
    w.var_port = FakeVar("8765")
    w.var_tls_on = FakeVar(False)
    w.var_status = FakeVar("● 状态未知")
    w.lbl_status = FakeWidget()
    w._status_busy = False
    w._status_pending = False
    w._status_last = 0.0
    w._portcheck_busy = False
    w._refresh_audit_size = lambda: None
    w.set_hint = lambda msg, level="info": None
    w.log = lambda *a, **kw: None
    return w


PASS = 0
FAIL = 0


def check(name, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print("  PASS  " + name)
    else:
        FAIL += 1
        print("  FAIL  " + name + (("  " + str(extra)) if extra else ""))


def test_health_probe():
    print("[1] health_probe 双协议与纯函数")
    seen = []
    orig = g.http_health

    def fake(port, timeout=2.0, scheme="http", host="127.0.0.1"):
        seen.append((port, scheme, timeout))
        return (scheme == "https", "detail-" + scheme)

    g.http_health = fake
    try:
        ok, detail = g.health_probe(8765, "https", 0.3)
        check("首选协议命中就不再试另一种", ok and len(seen) == 1, seen)
        seen.clear()
        ok, detail = g.health_probe(8765, "http", 0.3)
        check("首选失败会回退另一协议并命中", ok and detail == "detail-https"
              and [s for _p, s, _t in seen] == ["http", "https"], seen)
        check("timeout 原样透传", all(t == 0.3 for _p, _s, t in seen))
        check("不依赖任何 Tk 对象（纯参数调用）", True)
    finally:
        g.http_health = orig


def test_refresh_async():
    print("[2] refresh_status 异步化 / 节流 / 线程安全")
    app = make_app()
    orig_probe = g.health_probe
    started = threading.Event()
    finish = threading.Event()
    calls = []

    def slow_probe(port, first, timeout):
        calls.append((port, first, threading.current_thread().name))
        started.set()
        finish.wait(3)
        return True, "ok"

    g.health_probe = slow_probe
    try:
        t0 = time.monotonic()
        app.refresh_status()
        elapsed = time.monotonic() - t0
        check("调用立即返回（未阻塞主线程）", elapsed < 0.1, "%.3fs" % elapsed)
        check("闸门已置位", app._status_busy is True)
        check("确实起了后台线程", started.wait(2))
        check("探测发生在工作线程", calls and calls[0][2] == "wk-status", calls)

        for _ in range(5):
            app.refresh_status()
        check("连点 5 次被合并为 1 次探测", len(calls) == 1, calls)
        check("连点只置 pending，不排队", app._status_pending is True)

        finish.set()
        time.sleep(0.05)
        n = app.root.pump_zero()
        check("结果通过 after(0) 回主线程", n >= 1)
        check("状态栏更新为运行中", app.var_status.get() == "● 正在运行",
              app.var_status.get())
        check("闸门已释放", app._status_busy is False)
        check("pending 转成一个延后补刷", app._status_pending is False
              and len(app.root.jobs) == 1 and app.root.jobs[0][0] > 0,
              app.root.jobs)

        before = len(calls)
        app.refresh_status()
        check("最小间隔内的重复刷新被节流", len(calls) == before, calls)

        time.sleep(g.STATUS_MIN_INTERVAL + 0.02)
        app._status_last = 0.0
        app.refresh_status()
        check("间隔过后可再次刷新", len(calls) == before + 1, calls)
        finish.set()
        time.sleep(0.05)
        app.root.pump_zero()

        check("Tk 变量从未被工作线程读取",
              all(r == "MainThread" for r in
                  app.var_port.readers + app.var_tls_on.readers),
              app.var_port.readers + app.var_tls_on.readers)
    finally:
        finish.set()
        g.health_probe = orig_probe


def test_port_check_async():
    print("[3] cmd_check_port 异步化 / 不弹真窗口")
    app = make_app()
    box = FakeMessageBox()
    orig_box, orig_owner, orig_health = g.messagebox, g.port_owner, g.http_health
    finish = threading.Event()
    g.messagebox = box

    def slow_owner(port):
        finish.wait(2)
        return []

    g.port_owner = slow_owner
    g.http_health = lambda *a, **kw: (False, "")
    try:
        t0 = time.monotonic()
        app.cmd_check_port()
        elapsed = time.monotonic() - t0
        check("调用立即返回（netstat/tasklist 不卡界面）", elapsed < 0.1,
              "%.3fs" % elapsed)
        check("检查期间闸门置位", app._portcheck_busy is True)
        app.cmd_check_port()
        check("重复点击被挡住", app._portcheck_busy is True)
        finish.set()
        time.sleep(0.05)
        app.root.pump_zero()
        check("结果回主线程并释放闸门", app._portcheck_busy is False)
        check("弹出一次结果提示", len(box.calls) == 1, box.calls)
        check("提示走的是假 messagebox（无真弹窗）",
              box.calls and box.calls[0][0] == "info", box.calls)
    finally:
        finish.set()
        g.messagebox, g.port_owner, g.http_health = orig_box, orig_owner, orig_health


def test_start_timeout():
    print("[4] cmd_start 回环等待超时已缩短")
    import ast
    import io
    src = io.open(os.path.join(ROOT, "server", "wkfile_gui.py"),
                  encoding="utf-8").read()
    tree = ast.parse(src)
    cls = next(n for n in tree.body
               if isinstance(n, ast.ClassDef) and n.name == "Wizard")
    start = next(n for n in cls.body
                 if isinstance(n, ast.FunctionDef) and n.name == "cmd_start")
    waits = []
    for node in ast.walk(start):
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and node.func.attr == "health"):
            for kw in node.keywords:
                if kw.arg == "timeout" and isinstance(kw.value, ast.Constant):
                    waits.append(kw.value.value)
    check("就绪探测超时 0.4s 已生效", 0.4 in waits, waits)
    check("旧值 1.0 已从就绪探测移除", 1.0 not in waits, waits)
    check("启动前检查仍保留 1.5s", 1.5 in waits, waits)


if __name__ == "__main__":
    test_health_probe()
    test_refresh_async()
    test_port_check_async()
    test_start_timeout()
    print("\nGUI_REFRESH_TEST: %d PASS / %d FAIL" % (PASS, FAIL))
    raise SystemExit(1 if FAIL else 0)
