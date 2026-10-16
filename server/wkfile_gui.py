#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
wkfile —— 图形化配置 / 一键安装向导

一个 exe 搞定服务端：双击打开界面，直接设置端口、允许操作的目录、来源 IP、
访问 Token、只读开关，然后一键保存配置 / 启动 / 装开机自启 / 放行防火墙。

运行模式
--------
    wkfile.exe                        打开图形向导（双击即可）
    wkfile.exe --run --config <path>  以控制台方式后台跑服务
                                      （计划任务 / 界面上的「启动服务」用）
    wkfile.exe --check-gui            自检：建窗口后立即退出（无人工交互）

只依赖标准库 + 同目录的 wkfile_agent 模块，打包成 exe 后完全离线可用。

运行时只在自己身边建【一个】文件夹 wkfile\\，所有状态都收在里面（见 wa.data_dir）。
"""

from __future__ import annotations

import json
import os
import re
import secrets
import socket
import subprocess
import sys
import tempfile
import threading
import time
import tkinter as tk
import tkinter.font as tkfont
from tkinter import filedialog, messagebox, ttk
from tkinter.scrolledtext import ScrolledText

import wkfile_agent as wa

APP_TITLE = "wkfile"
SUBTITLE = "让 AI 客户端（MCP）安全地查看与修改这台服务器的文件"
VERSION = wa.VERSION
# 作者署名（改作者只改 wkfile_agent.AUTHOR / AUTHOR_WECHAT 两处即可）
AUTHOR = getattr(wa, "AUTHOR", "")
AUTHOR_WECHAT = getattr(wa, "AUTHOR_WECHAT", "")
TASK_NAME = "wkfile"
FW_RULE = "wkfile"

FONT = "Microsoft YaHei UI"
MONO = "Consolas"


# ---------------------------------------------------------------------------
# 路径与运行环境
# ---------------------------------------------------------------------------


def app_dir() -> str:
    """exe / 脚本所在目录（打包后 __file__ 指向临时解压目录，必须用 exe 路径）"""
    return wa.program_dir()


# 数据目录统一由 wkfile_agent 决定：exe 旁边只会出现一个 wkfile\ 文件夹，
# 配置、PID、运行日志、审计日志、备份、回收站全在里面
DATA_DIR = wa.data_dir()
CONFIG_PATH = os.path.join(DATA_DIR, wa.CONFIG_FILENAME)
LOG_DIR = os.path.join(DATA_DIR, wa.LOG_DIRNAME)
RUN_LOG = os.path.join(LOG_DIR, wa.LOG_FILENAME)
AUDIT_LOG = os.path.join(LOG_DIR, wa.AUDIT_FILENAME)
PID_PATH = os.path.join(DATA_DIR, wa.PID_FILENAME)


def is_frozen() -> bool:
    return getattr(sys, "frozen", False)


def is_admin() -> bool:
    if sys.platform != "win32":
        return False
    try:
        import ctypes

        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def local_ipv4() -> str:
    """本机对外 IPv4（UDP 连一下不真的发包，只为让系统选路由）。

    实现统一放在 wkfile_agent 里（CLI 与自签证书也要用同一份），这里只做转发。
    """
    return wa.local_ipv4()


def port_in_use(port: int) -> bool:
    """端口是否已被别的进程监听。

    ⚠ 这里刻意不用 socket.bind() 试探：Windows 的 SO_REUSEADDR 语义是
    「允许抢占已被监听的端口」，带它 bind 会成功；不带它则会把 TIME_WAIT
    状态也算成占用。两种情况都会给出错误结论（实测漏报过）。
    因此以 netstat 的 LISTENING 记录为唯一判据，与「谁在占用」结论一致。
    """
    return len(port_owner(port)) > 0


def port_owner(port: int) -> list:
    """查出正在监听该端口的进程，返回 [(pid, 进程名), ...]。查不到就返回空表。"""
    found: list = []
    try:
        out = subprocess.run(
            ["netstat", "-ano", "-p", "TCP"],
            capture_output=True,
            text=True,
            encoding="mbcs",
            errors="replace",
            creationflags=0x08000000,
        ).stdout or ""
    except OSError:
        return found

    pids: list = []
    for line in out.splitlines():
        parts = line.split()
        if len(parts) >= 5 and parts[0].upper() == "TCP" and parts[3].upper() == "LISTENING":
            if parts[1].rsplit(":", 1)[-1] == str(port):
                try:
                    pid = int(parts[4])
                except ValueError:
                    continue
                if pid not in pids:
                    pids.append(pid)

    for pid in pids:
        name = "未知进程"
        try:
            t = subprocess.run(
                ["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"],
                capture_output=True,
                text=True,
                encoding="mbcs",
                errors="replace",
                creationflags=0x08000000,
            ).stdout or ""
            first_line = t.strip().splitlines()[0] if t.strip() else ""
            if first_line.startswith('"'):
                name = first_line.split('","')[0].strip('"')
        except Exception:
            pass
        found.append((pid, name))
    return found


def http_health(port: int, host: str = "127.0.0.1", timeout: float = 2.0,
                scheme: str = "http"):
    """探测端口上跑的是不是本服务，返回 (是否为本服务, 详情)。

    必须认准服务标识：随便一个 HTTP 服务（哪怕是返回 404 的）都会应答，
    若只判断「有没有响应」会把别的程序误认成本服务。
    同时绕开系统代理，避免本地请求被代理劫持。
    兼容旧版标识 winfile-agent，方便老服务平滑升级。
    scheme="https" 时接受自签证书 —— 自测的目的是「通不通」，不是「证书谁签的」。
    """
    import urllib.error
    import urllib.request

    url = f"{scheme}://{host}:{port}/api/health"
    handlers = [urllib.request.ProxyHandler({})]
    if scheme == "https":
        import ssl as _ssl

        ctx = _ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = _ssl.CERT_NONE
        handlers.append(urllib.request.HTTPSHandler(context=ctx))
    opener = urllib.request.build_opener(*handlers)
    try:
        with opener.open(url, timeout=timeout) as resp:
            body = resp.read(600).decode("utf-8", "replace")
            return ("wkfile" in body or "winfile-agent" in body), body
    except urllib.error.HTTPError as exc:
        return False, f"HTTP {exc.code}（端口上有 HTTP 服务，但不是本服务）"
    except Exception as exc:
        return False, str(exc)


def enable_dpi_awareness() -> None:
    if sys.platform != "win32":
        return
    try:
        import ctypes

        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except Exception:
            ctypes.windll.user32.SetProcessDPIAware()
    except Exception:
        pass


def _run_log_limits(config_path: str = "") -> tuple:
    """从配置里读运行日志的轮转参数；读不到就退回模块默认值。"""
    default_bytes = int(wa.DEFAULT_CONFIG.get("run_log_max_bytes") or 0)
    default_keep = int(wa.DEFAULT_CONFIG.get("run_log_backup_count") or 0)
    try:
        cfg = wa.load_config(config_path or CONFIG_PATH)
        return (int(getattr(cfg, "run_log_max_bytes", default_bytes) or 0),
                int(getattr(cfg, "run_log_backup_count", default_keep) or 0))
    except Exception:  # noqa: BLE001 —— 配置坏了也不能让日志重定向失效
        return default_bytes, default_keep


def ensure_stdio(path: str = "", config_path: str = "") -> None:
    """windowed exe 没有控制台，sys.stdout 是 None，直接 print 会崩。
    这里把缺失的流接到日志文件上，并让该文件写满后自动轮转。"""
    if sys.stdout is not None and sys.stderr is not None:
        return
    target = path or RUN_LOG
    max_bytes, keep = _run_log_limits(config_path)
    writer = wa.RotatingLogWriter(target, max_bytes, keep)
    if sys.stdout is None:
        sys.stdout = writer
    if sys.stderr is None:
        sys.stderr = writer


# ---------------------------------------------------------------------------
# 界面
# ---------------------------------------------------------------------------


class Wizard:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.busy = False
        self._danger_ack = False
        self.cfg = self._load_config_safe()

        self.var_host = tk.StringVar()
        self.var_port = tk.StringVar()
        self.var_token = tk.StringVar()
        self.var_show_token = tk.BooleanVar(value=False)
        self.var_ro_token = tk.StringVar()
        self.var_show_ro_token = tk.BooleanVar(value=False)
        # HTTPS：勾选开关只是界面上的「记忆」，真正生效看 tls_cert / tls_key 是否都填了
        self.var_tls_on = tk.BooleanVar(value=False)
        self.var_tls_cert = tk.StringVar()
        self.var_tls_chain = tk.StringVar()
        self.var_tls_key = tk.StringVar()
        self.var_tls_info = tk.StringVar(value="")
        # Token 有效期：下拉选「永久」或天数；var_token_exp 存算出来的绝对到期时间
        self.var_token_ttl = tk.StringVar(value="永久")
        self.var_token_exp = tk.StringVar(value="")
        self.var_readonly = tk.BooleanVar(value=False)
        self.var_allowdel = tk.BooleanVar(value=True)
        self.var_allowmove = tk.BooleanVar(value=True)
        self.var_backup = tk.BooleanVar(value=True)
        self.var_preflight = tk.BooleanVar(value=True)
        self.var_mcp = tk.BooleanVar(value=True)
        self.var_tools: dict[str, tk.BooleanVar] = {}
        self.var_audit_mb = tk.StringVar(value="5")
        self.var_runlog_mb = tk.StringVar(value="2")
        self.var_status = tk.StringVar(value="● 状态未知")
        self.var_puburl = tk.StringVar()
        self.var_hint = tk.StringVar(value="就绪。配置完成后依次点「保存配置」→「启动服务」。")
        # ---- 数据库页（④）----
        # db_items 是界面里正在编辑的工作副本，点「保存配置」才写进配置文件；
        # 这样用户改了一半直接关窗口也不会把磁盘上的配置改坏。
        self.db_items: list[dict] = []
        self.db_vars: dict[str, tk.StringVar] = {}
        self.var_db_write = tk.BooleanVar(value=False)
        self.var_db_hint = tk.StringVar(value="")
        self._db_sel = -1
        self._db_syncing = False
        # 公网 IP 探测的状态：_pubip 缓存最近一次探到的公网地址；
        # _puburl_touched 记录用户是否手动改过「服务器地址」，改过就不再自动覆盖。
        self._pubip = ""
        self._ip_probing = False
        self._puburl_touched = False
        self._setting_url = False
        # 端口改动时自动同步「服务器地址」里的端口（只改端口部分，不动主机名）
        self.var_port.trace_add("write", self._sync_puburl_port)
        self.var_puburl.trace_add("write", self._on_puburl_write)

        self._build()
        self._load_form()
        self.log(f"{APP_TITLE} v{VERSION}  ·  配置文件：{CONFIG_PATH}")
        if not is_admin():
            self.log("当前不是管理员权限，安装开机自启 / 操作防火墙需要以管理员身份运行", "warn")
        self.root.after(400, self.refresh_status)
        self.root.after(700, self.first_run_hint)

    def _load_config_safe(self):
        """读配置；解析失败时弹窗说明**具体哪一行错**，并退回默认值让界面还能开。

        现在鼓励用户手改 wkfile.config.json（模板自带注释），手改就可能出现
        语法错误。此时绝不能让窗口整个打不开 —— 那样用户连改回来的机会都没有。
        """
        try:
            return wa.load_config(CONFIG_PATH)
        except ValueError as exc:
            try:
                messagebox.showerror(
                    "配置文件有语法错误",
                    f"读取 {CONFIG_PATH} 失败：\n\n{exc}\n\n"
                    "通常是手改时少了逗号/引号，或 // 注释后面还写了别的字。\n"
                    "现在界面按【默认值】打开 —— 直接点「保存配置」会用默认值"
                    "覆盖那个坏文件（谨慎），或者先关掉本窗口用记事本修好再开。",
                    parent=self.root)
            except Exception:  # noqa: BLE001 —— 无头模式下弹不了窗，不拦流程
                pass
            # 直接构造默认值（不要走 load_config("")——空路径会让它往
            # 当前目录落一个杂散的模板文件）
            return wa.Config({}, CONFIG_PATH)

    # ---------------- 构建 ----------------

    def _build(self) -> None:
        root = self.root
        root.title(f"{APP_TITLE}  v{VERSION}   ——  {AUTHOR}")

        # 按屏幕可用尺寸自适应，且绝不超出屏幕（留出任务栏与标题栏的余量）
        sw = root.winfo_screenwidth()
        sh = root.winfo_screenheight()
        w = min(900, max(720, sw - 160))
        h = min(880, max(560, sh - 170))
        root.geometry(f"{w}x{h}+{max(0, (sw - w) // 2)}+{max(0, (sh - h) // 2 - 30)}")
        root.minsize(720, 520)
        self._setup_fonts()

        # 顶层用 grid：只有「选项卡」那一行可伸缩，运行日志永远贴底且有固定高度。
        # 之前 head/hint/body/log 全用 pack 且 body 带 expand=True，窗口一小
        # 选项卡内容就把日志区挤出可视区域，这就是"日志看不到"的根因。
        root.columnconfigure(0, weight=1)
        root.rowconfigure(3, weight=1)

        head = ttk.Frame(root, padding=(16, 12, 16, 8))
        head.grid(row=0, column=0, sticky="ew")
        ttk.Label(head, text=APP_TITLE, font=(FONT, 16, "bold")).pack(side="left")
        ttk.Label(head, text=f"  v{VERSION}  ·  {SUBTITLE}", foreground="#666").pack(
            side="left", pady=(5, 0)
        )
        self.lbl_status = ttk.Label(
            head, textvariable=self.var_status, font=(FONT, 11, "bold"), foreground="#b3261e"
        )
        self.lbl_status.pack(side="right")

        # 常驻状态条：任何一次操作的结果都会立刻显示在这里（不用去翻底部日志）
        self.lbl_hint = tk.Label(
            root,
            textvariable=self.var_hint,
            font=(FONT, 10),
            foreground="#1f2937",
            background="#eef4ff",
            anchor="w",
            justify="left",
            padx=14,
            pady=7,
            wraplength=max(400, w - 70),
        )
        self.lbl_hint.grid(row=1, column=0, sticky="ew")

        ttk.Separator(root, orient="horizontal").grid(row=2, column=0, sticky="ew")

        body = ttk.Frame(root, padding=(12, 10, 12, 0))
        body.grid(row=3, column=0, sticky="nsew")
        body.columnconfigure(0, weight=1)
        body.rowconfigure(0, weight=1)
        self.nb = ttk.Notebook(body)
        self.nb.grid(row=0, column=0, sticky="nsew")
        self.nb.add(self._tab_basic(), text="   ① 基础设置   ")
        self.nb.add(self._tab_security(), text="   ② 安全与权限   ")
        self.nb.add(self._tab_deploy(), text="   ③ 安装与运行   ")
        self.nb.add(self._tab_database(), text="   ④ 数据库   ")
        self.nb.add(self._tab_about(), text="   ⑤ 关于   ")

        logbox = ttk.LabelFrame(root, text=" 运行日志 ", padding=(8, 6))
        logbox.grid(row=4, column=0, sticky="ew", padx=12, pady=(8, 10))
        self.txt_log = ScrolledText(
            logbox,
            height=8,
            wrap="word",
            font=(MONO, 9),
            state="disabled",
            background="#fafafa",
            relief="flat",
        )
        self.txt_log.pack(fill="both", expand=True)

        # 作者署名单独占窗口最底一行 —— 放顶部会和运行状态挤在一起，把状态挤没
        ttk.Separator(root, orient="horizontal").grid(row=5, column=0, sticky="ew")
        foot = ttk.Frame(root, padding=(16, 5, 16, 8))
        foot.grid(row=6, column=0, sticky="ew")
        ttk.Label(foot, text=f"作者：{AUTHOR}   微信：{AUTHOR_WECHAT}",
                  foreground="#8a6d3b").pack(side="left")
        ttk.Label(foot, text=f"  {APP_TITLE} v{VERSION}  ·  免费使用，"
                             f"联系方式见「⑤ 关于」",
                  foreground="#999").pack(side="right")

    def _scroll_tab(self, padding: int = 14) -> tuple[ttk.Frame, ttk.Frame]:
        """把一页内容包进可滚动容器。

        返回 (outer, inner)：outer 交给 Notebook 当页签，控件都放进 inner。
        选项卡内容再高也只是在这页里滚动，不会把窗口撑大、更不会顶掉底部日志。
        """
        try:
            bg = ttk.Style().lookup("TFrame", "background") or "SystemButtonFace"
        except Exception:  # noqa: BLE001
            bg = "SystemButtonFace"

        outer = ttk.Frame(self.nb)
        canvas = tk.Canvas(outer, highlightthickness=0, borderwidth=0, background=bg)
        vsb = ttk.Scrollbar(outer, orient="vertical", command=canvas.yview)
        canvas.configure(yscrollcommand=vsb.set)
        vsb.pack(side="right", fill="y")
        canvas.pack(side="left", fill="both", expand=True)

        inner = ttk.Frame(canvas, padding=padding)
        window = canvas.create_window((0, 0), window=inner, anchor="nw")

        def _sync_region(_event=None):
            canvas.configure(scrollregion=canvas.bbox("all"))

        def _sync_width(event):
            canvas.itemconfigure(window, width=event.width)

        inner.bind("<Configure>", _sync_region)
        canvas.bind("<Configure>", _sync_width)

        def _on_wheel(event):
            first, last = canvas.yview()
            if first <= 0.0 and last >= 1.0:   # 内容没超出，不用滚
                return
            canvas.yview_scroll(int(-1 * (event.delta / 120)) or 0, "units")

        # 只在鼠标进入这一页时接管滚轮，避免影响其它页与文本框
        canvas.bind("<Enter>", lambda _e: canvas.bind_all("<MouseWheel>", _on_wheel))
        canvas.bind("<Leave>", lambda _e: canvas.unbind_all("<MouseWheel>"))
        return outer, inner

    def _setup_fonts(self) -> None:
        try:
            style = ttk.Style()
            if "vista" in style.theme_names():
                style.theme_use("vista")
        except Exception:
            pass
        for name in ("TkDefaultFont", "TkTextFont", "TkMenuFont", "TkHeadingFont"):
            try:
                tkfont.nametofont(name).configure(family=FONT, size=9)
            except Exception:
                pass

    # ---- 页 1：基础 ----

    def _tab_basic(self) -> ttk.Frame:
        outer, f = self._scroll_tab()

        g1 = ttk.LabelFrame(f, text=" 监听地址 ", padding=(12, 10))
        g1.pack(fill="x")
        row = ttk.Frame(g1)
        row.pack(fill="x")
        ttk.Label(row, text="监听地址：").pack(side="left")
        ttk.Combobox(
            row,
            textvariable=self.var_host,
            width=30,
            values=["0.0.0.0", "127.0.0.1"],
        ).pack(side="left")
        ttk.Label(row, text="   端口：").pack(side="left")
        ttk.Entry(row, textvariable=self.var_port, width=8).pack(side="left")
        ttk.Button(row, text="检查端口占用", command=self.cmd_check_port).pack(side="left", padx=8)
        ttk.Label(
            g1,
            text="0.0.0.0 = 允许别的机器访问（MCP 客户端装在另一台电脑上时必须选这项）\n"
            "127.0.0.1 = 只允许本机访问（仅本机自测时使用）",
            foreground="#666",
            justify="left",
        ).pack(anchor="w", pady=(8, 0))

        g2 = ttk.LabelFrame(f, text=" 允许操作的目录（白名单） ", padding=(12, 10))
        g2.pack(fill="x", pady=(12, 0))
        wrap = ttk.Frame(g2)
        wrap.pack(fill="x")
        self.lst_roots = tk.Listbox(
            wrap, height=6, activestyle="none", selectmode="extended", font=(FONT, 10)
        )
        sb = ttk.Scrollbar(wrap, orient="vertical", command=self.lst_roots.yview)
        self.lst_roots.configure(yscrollcommand=sb.set)
        self.lst_roots.pack(side="left", fill="both", expand=True)
        sb.pack(side="left", fill="y")
        btns = ttk.Frame(wrap)
        btns.pack(side="left", fill="y", padx=(10, 0))
        ttk.Button(btns, text="添加目录…", width=13, command=self.cmd_add_root).pack(pady=(0, 6))
        ttk.Button(btns, text="移除选中", width=13, command=self.cmd_del_root).pack(pady=(0, 6))
        ttk.Button(btns, text="清空列表", width=13, command=self.cmd_clear_roots).pack()
        ttk.Label(
            g2,
            text="服务只能操作这些目录（含子目录）里的文件，其它位置一律拒绝访问。",
            foreground="#666",
        ).pack(anchor="w", pady=(8, 0))

        return outer

    # ---- 页 2：安全 ----

    def _tab_security(self) -> ttk.Frame:
        outer, f = self._scroll_tab()

        g1 = ttk.LabelFrame(f, text=" 访问令牌 Token ", padding=(12, 10))
        g1.pack(fill="x")
        row = ttk.Frame(g1)
        row.pack(fill="x")
        ttk.Label(row, text="Token：").pack(side="left")
        self.ent_token = ttk.Entry(row, textvariable=self.var_token, width=46, show="*")
        self.ent_token.pack(side="left")
        ttk.Button(row, text="生成", width=7, command=self.cmd_gen_token).pack(side="left", padx=4)
        ttk.Button(row, text="复制", width=7, command=self.cmd_copy_token).pack(side="left")
        ttk.Checkbutton(
            row,
            text="显示",
            variable=self.var_show_token,
            command=self.cmd_toggle_token,
        ).pack(side="left", padx=8)

        row2 = ttk.Frame(g1)
        row2.pack(fill="x", pady=(8, 0))
        ttk.Label(row2, text="有效期：").pack(side="left")
        self.cmb_ttl = ttk.Combobox(
            row2,
            textvariable=self.var_token_ttl,
            width=8,
            values=["永久", "1", "3", "7", "30", "90", "365"],
        )
        self.cmb_ttl.pack(side="left")
        self.cmb_ttl.bind("<<ComboboxSelected>>", lambda _e: self._apply_token_ttl())
        ttk.Label(row2, text=" 天（填「永久」或 0 = 不自动失效）").pack(side="left")
        ttk.Button(row2, text="按此重新计时", command=self._apply_token_ttl).pack(
            side="left", padx=(10, 0)
        )
        self.lbl_token_exp = ttk.Label(
            g1, textvariable=self.var_token_exp, foreground="#666", justify="left"
        )
        self.lbl_token_exp.pack(anchor="w", pady=(6, 0))

        ttk.Label(
            g1,
            text="客户端（跑 MCP 的那台机器）必须带这个 Token 才能访问本服务。怀疑泄露了就点「生成」换一个，然后同步更新客户端。\n"
                 "设了有效期后，到期的那一刻起服务会拒绝所有请求（401 token_expired），"
                 "到期后所有客户端会突然连不上 —— 所以请提前点「生成」或「按此重新计时」续期。",
            foreground="#666",
            justify="left",
            wraplength=740,
        ).pack(anchor="w", pady=(8, 0))

        g1b = ttk.LabelFrame(f, text=" 只读 Token（可选） ", padding=(12, 10))
        g1b.pack(fill="x", pady=(12, 0))
        rowr = ttk.Frame(g1b)
        rowr.pack(fill="x")
        ttk.Label(rowr, text="Token：").pack(side="left")
        self.ent_ro_token = ttk.Entry(rowr, textvariable=self.var_ro_token, width=46, show="*")
        self.ent_ro_token.pack(side="left")
        ttk.Button(rowr, text="生成", width=7, command=self.cmd_gen_ro_token).pack(
            side="left", padx=4
        )
        ttk.Button(rowr, text="复制", width=7, command=self.cmd_copy_ro_token).pack(side="left")
        ttk.Button(rowr, text="清空", width=7, command=self.cmd_clear_ro_token).pack(
            side="left", padx=4
        )
        ttk.Checkbutton(
            rowr, text="显示", variable=self.var_show_ro_token,
            command=self.cmd_toggle_ro_token,
        ).pack(side="left", padx=8)
        ttk.Label(
            g1b,
            text="可选。填了之后，拿这个 Token 连接的客户端【只能读，不能写】—— 写 / 删 / 移动 / 还原一律被拒。\n"
                 "适合「只让 AI 看代码，不给它改的权限」这种场景。留空 = 不启用。",
            foreground="#666",
            justify="left",
            wraplength=740,
        ).pack(anchor="w", pady=(8, 0))

        g2 = ttk.LabelFrame(f, text=" 允许访问的来源 IP ", padding=(12, 10))
        g2.pack(fill="x", pady=(12, 0))
        self.txt_ips = ScrolledText(g2, height=4, wrap="none", font=(MONO, 9))
        self.txt_ips.pack(fill="x")
        ttk.Label(
            g2,
            text="每行一条；留空 = 不限制来源。支持三种写法：\n"
            "   精确 IP         192.168.1.20\n"
            "   前缀通配        192.168.1.*        10.*\n"
            "   网段 CIDR       10.0.0.0/8        192.168.0.0/16",
            foreground="#666",
            justify="left",
        ).pack(anchor="w", pady=(8, 0))

        g3 = ttk.LabelFrame(f, text=" 权限开关 ", padding=(12, 10))
        g3.pack(fill="x", pady=(12, 0))
        ttk.Checkbutton(
            g3,
            text="只读模式 —— 完全禁止写入 / 删除 / 改名（推荐给只做查看的场景）",
            variable=self.var_readonly,
        ).pack(anchor="w", pady=2)
        ttk.Checkbutton(
            g3,
            text="允许删除 —— 删除只移入回收站目录，可用 trash_id 恢复，不会真删",
            variable=self.var_allowdel,
        ).pack(anchor="w", pady=2)
        ttk.Checkbutton(
            g3,
            text="允许移动 / 改名 —— 关掉后 win_move 会被拒绝",
            variable=self.var_allowmove,
        ).pack(anchor="w", pady=2)
        ttk.Checkbutton(
            g3,
            text="覆盖前自动备份 —— 改写已有文件前先存一份到 wkfile\\backups",
            variable=self.var_backup,
        ).pack(anchor="w", pady=2)
        ttk.Checkbutton(
            g3,
            text="写入前语法校验 —— .py/.json/.jsonl/.xml/.toml 先验语法，不通过则不落盘"
                 "（想故意写入半截配置时请关掉）",
            variable=self.var_preflight,
        ).pack(anchor="w", pady=2)
        ttk.Checkbutton(
            g3,
            text="开放 MCP 端点 —— 允许客户端直连 …/mcp（关掉后所有 MCP 客户端都将无法接入）",
            variable=self.var_mcp,
        ).pack(anchor="w", pady=2)

        g4 = ttk.LabelFrame(f, text=" 工具级开关（取消勾选 = 该工具不出现、直接调用也被拒） ", padding=(12, 10))
        g4.pack(fill="x", pady=(12, 0))
        self._build_tool_toggles(g4)

        return outer

    def _tool_catalog(self) -> list[tuple[str, str, bool]]:
        """返回 [(工具名, 中文标题, 是否只读)]；拿不到 MCP 模块时返回空表。"""
        mod = getattr(wa, "mcpmod", None)
        if mod is None or not getattr(mod, "TOOLS", None):
            return []
        out = []
        for t in mod.TOOLS:
            name = t.get("name") or ""
            title = t.get("title") or name
            ro = bool((t.get("annotations") or {}).get("readOnlyHint"))
            out.append((name, title, ro))
        return out

    def _build_tool_toggles(self, parent: ttk.Frame) -> None:
        catalog = self._tool_catalog()
        if not catalog:
            ttk.Label(
                parent,
                text="（当前构建里没有 MCP 模块，工具级开关不可用）",
                foreground="#666",
            ).pack(anchor="w")
            return
        grid = ttk.Frame(parent)
        grid.pack(fill="x")
        for idx, (name, title, ro) in enumerate(catalog):
            var = tk.BooleanVar(value=True)
            self.var_tools[name] = var
            mark = "（只读）" if ro else ""
            ttk.Checkbutton(
                grid, text=f"{title}{mark}", variable=var
            ).grid(row=idx // 3, column=idx % 3, sticky="w", padx=(0, 14), pady=1)
        row = ttk.Frame(parent)
        row.pack(fill="x", pady=(8, 0))
        ttk.Button(row, text="全选", width=8, command=lambda: self._set_all_tools(True)).pack(
            side="left", padx=(0, 6)
        )
        ttk.Button(row, text="全部禁用", width=10, command=lambda: self._set_all_tools(False)).pack(
            side="left", padx=(0, 6)
        )
        ttk.Button(row, text="只留只读", width=10, command=self._only_readonly_tools).pack(
            side="left", padx=(0, 6)
        )
        ttk.Label(
            parent,
            text=(f"不改这里就是 {len(catalog)} 个工具全开。"
                  f"给 AI「只查看」时点「只留只读」最省事。"),
            foreground="#666",
        ).pack(anchor="w", pady=(6, 0))

    def _set_all_tools(self, value: bool) -> None:
        for var in self.var_tools.values():
            var.set(value)

    def _only_readonly_tools(self) -> None:
        ro = {name for name, _t, is_ro in self._tool_catalog() if is_ro}
        for name, var in self.var_tools.items():
            var.set(name in ro)

    # ---- 页 3：部署 ----

    def _tab_deploy(self) -> ttk.Frame:
        outer, f = self._scroll_tab()

        g1 = ttk.LabelFrame(f, text=" 服务控制 ", padding=(12, 10))
        g1.pack(fill="x")
        r1 = ttk.Frame(g1)
        r1.pack(fill="x")
        ttk.Label(r1, text="当前状态：").pack(side="left")
        ttk.Label(r1, textvariable=self.var_status, font=(FONT, 10, "bold")).pack(side="left")
        ttk.Button(r1, text="刷新状态", width=10, command=self.refresh_status).pack(side="right")
        r2 = ttk.Frame(g1)
        r2.pack(fill="x", pady=(10, 0))
        ttk.Button(r2, text="保存配置", width=13, command=lambda: self.save_config()).pack(
            side="left", padx=(0, 6)
        )
        ttk.Button(r2, text="启动服务", width=13, command=self.cmd_start).pack(side="left", padx=6)
        ttk.Button(r2, text="停止服务", width=13, command=self.cmd_stop).pack(side="left", padx=6)
        ttk.Button(r2, text="测试连接", width=13, command=self.cmd_test).pack(side="left", padx=6)

        g2 = ttk.LabelFrame(f, text=" 开机自启（Windows 计划任务） ", padding=(12, 10))
        g2.pack(fill="x", pady=(12, 0))
        r = ttk.Frame(g2)
        r.pack(fill="x")
        ttk.Button(r, text="安装开机自启", width=15, command=self.cmd_install_autostart).pack(
            side="left", padx=(0, 6)
        )
        ttk.Button(r, text="卸载开机自启", width=15, command=self.cmd_uninstall_autostart).pack(
            side="left", padx=6
        )
        ttk.Button(r, text="查询自启状态", width=15, command=self.cmd_query_autostart).pack(
            side="left", padx=6
        )
        ttk.Label(
            g2,
            text="以 SYSTEM 身份在开机时启动，不需要用户登录。需要管理员权限。",
            foreground="#666",
        ).pack(anchor="w", pady=(8, 0))

        g3 = ttk.LabelFrame(f, text=" 防火墙 ", padding=(12, 10))
        g3.pack(fill="x", pady=(12, 0))
        r = ttk.Frame(g3)
        r.pack(fill="x")
        ttk.Button(r, text="放行当前端口", width=15, command=self.cmd_fw_add).pack(
            side="left", padx=(0, 6)
        )
        ttk.Button(r, text="关闭放行", width=15, command=self.cmd_fw_del).pack(side="left", padx=6)
        ttk.Label(
            g3,
            text="放行入站 TCP 端口。需要管理员权限。云服务器还要在安全组里放行同一端口，并把来源限制成你自己的 IP。",
            foreground="#666",
        ).pack(anchor="w", pady=(8, 0))

        g4 = ttk.LabelFrame(f, text=" MCP 客户端配置（Trae / Cursor / Claude Desktop 等） ",
                            padding=(12, 10))
        g4.pack(fill="x", pady=(12, 0))

        r = ttk.Frame(g4)
        r.pack(fill="x")
        ttk.Label(r, text="服务器地址：").pack(side="left")
        ttk.Entry(r, textvariable=self.var_puburl, width=38).pack(side="left")
        self.btn_pubip = ttk.Button(r, text="检测公网 IP", width=12,
                                    command=self.cmd_detect_pubip)
        self.btn_pubip.pack(side="left", padx=(6, 0))
        ttk.Button(r, text="复制 MCP 配置", width=15, command=self.cmd_copy_mcp).pack(
            side="left", padx=8
        )

        ttk.Label(
            g4,
            text="把复制的 JSON 粘进客户端的 MCP 配置即可，本机不用装任何东西。\n"
                 "服务器地址要填「客户端那台机器能访问到」的地址：客户端在别的网络里就填公网 IP"
                 "（如 http://1.2.3.4:10087），\n"
                 "同一局域网才填内网 IP。启动时程序会自动探测公网 IP 并填上，"
                 "不合适可以直接改这里。",
            foreground="#666",
            justify="left",
        ).pack(anchor="w", pady=(8, 0))

        g4b = ttk.LabelFrame(f, text=" HTTPS（可选，填了就不再是明文） ", padding=(12, 10))
        g4b.pack(fill="x", pady=(12, 0))
        ttk.Checkbutton(
            g4b,
            text="启用 HTTPS  —— 勾上后服务器地址要写 https://，客户端的配置也要同步改",
            variable=self.var_tls_on,
            command=self.cmd_toggle_tls,
        ).pack(anchor="w")

        rt = ttk.Frame(g4b)
        rt.pack(fill="x", pady=(8, 0))
        ttk.Label(rt, text="公钥证书（PEM/CRT）：").pack(side="left")
        self.ent_cert = ttk.Entry(rt, textvariable=self.var_tls_cert, width=44)
        self.ent_cert.pack(side="left")
        ttk.Button(rt, text="浏览…", width=8, command=self.cmd_pick_cert).pack(
            side="left", padx=6
        )

        rm = ttk.Frame(g4b)
        rm.pack(fill="x", pady=(6, 0))
        ttk.Label(rm, text="中间证书链（可空）：").pack(side="left")
        self.ent_chain = ttk.Entry(rm, textvariable=self.var_tls_chain, width=44)
        self.ent_chain.pack(side="left")
        ttk.Button(rm, text="浏览…", width=8, command=self.cmd_pick_chain).pack(
            side="left", padx=6
        )
        self.btn_clear_chain = ttk.Button(rm, text="清空", width=6,
                                         command=self.cmd_clear_chain)
        self.btn_clear_chain.pack(side="left")

        rk = ttk.Frame(g4b)
        rk.pack(fill="x", pady=(6, 0))
        ttk.Label(rk, text="私钥（KEY）　　　：").pack(side="left")
        self.ent_key = ttk.Entry(rk, textvariable=self.var_tls_key, width=44)
        self.ent_key.pack(side="left")
        ttk.Button(rk, text="浏览…", width=8, command=self.cmd_pick_key).pack(
            side="left", padx=6
        )

        rc = ttk.Frame(g4b)
        rc.pack(fill="x", pady=(6, 0))
        ttk.Button(rc, text="检查证书", width=10, command=self.cmd_check_cert).pack(side="left")
        self.btn_selfcert = ttk.Button(rc, text="生成自签证书", width=14,
                                       command=self.cmd_gen_selfcert)
        self.btn_selfcert.pack(side="left", padx=8)
        self.lbl_tls = ttk.Label(rc, textvariable=self.var_tls_info, foreground="#666")
        self.lbl_tls.pack(side="left", padx=8)

        ttk.Label(
            g4b,
            text="三个框对应证书商给你的文件，照名字放就行：\n"
                 "  · 只给了「证书 + 私钥」两份　→ 中间链留空；\n"
                 "  · 给了「public + chain + key」三份　→ 第二份（chain / ca-bundle / "
                 "中间证书 / fullchain_x）填进「中间证书链」，程序会自动合并；\n"
                 "  · 只给了一份 fullchain（里面已含链）→ 填第一行、中间链留空。\n"
                 "没有域名只有 IP：点「生成自签证书」，程序按本机 IP 现场签一张"
                 "（纯本地运算，不装 openssl、不联网）。\n"
                 "自签证书客户端首次连接会提示不受信任，确认一次即可；私钥文件不要外传。",
            foreground="#666",
            justify="left",
            wraplength=740,
        ).pack(anchor="w", pady=(8, 0))

        g5 = ttk.LabelFrame(f, text=" 其它 ", padding=(12, 10))
        g5.pack(fill="x", pady=(12, 0))
        r = ttk.Frame(g5)
        r.pack(fill="x")
        ttk.Button(r, text="打开数据目录", width=15, command=self.cmd_open_data).pack(
            side="left", padx=(0, 6)
        )
        ttk.Button(r, text="查看运行日志", width=15, command=self.cmd_open_log).pack(
            side="left", padx=6
        )
        ttk.Button(r, text="查看审计日志", width=15, command=self.cmd_open_audit).pack(
            side="left", padx=6
        )
        r2 = ttk.Frame(g5)
        r2.pack(fill="x", pady=(8, 0))
        ttk.Label(r2, text="审计日志轮转：超过").pack(side="left")
        ttk.Entry(r2, textvariable=self.var_audit_mb, width=6).pack(side="left", padx=4)
        ttk.Label(r2, text="MB 就滚动，保留 3 份历史（填 0 = 不轮转）").pack(side="left")
        ttk.Button(r2, text="清空审计日志", width=13, command=self.cmd_clear_audit).pack(
            side="left", padx=8
        )
        r3 = ttk.Frame(g5)
        r3.pack(fill="x", pady=(6, 0))
        ttk.Label(r3, text="运行日志轮转：超过").pack(side="left")
        ttk.Entry(r3, textvariable=self.var_runlog_mb, width=6).pack(side="left", padx=4)
        ttk.Label(r3, text="MB 就滚动，保留 3 份历史（填 0 = 不轮转）").pack(side="left")
        self.lbl_audit_size = ttk.Label(g5, text="", foreground="#666")
        self.lbl_audit_size.pack(anchor="w", pady=(6, 0))

        return outer

    # ---------------- 表单读写 ----------------

    def _set_puburl(self, value: str) -> None:
        """程序自己改「服务器地址」时走这里，避免被当成用户手改。"""
        self._setting_url = True
        try:
            self.var_puburl.set(value)
        finally:
            self._setting_url = False

    def _on_puburl_write(self, *_args) -> None:
        if not self._setting_url:
            self._puburl_touched = True

    @staticmethod
    def _url_host(url: str) -> str:
        m = re.match(r"^[a-zA-Z][\w+.-]*://([^/:]+)", (url or "").strip())
        return m.group(1) if m else ""

    def _replace_url_host(self, url: str, host: str) -> str:
        return re.sub(r"^([a-zA-Z][\w+.-]*://)[^/:]+", r"\g<1>" + host,
                      (url or "").strip())

    def cmd_detect_pubip(self, auto: bool = False) -> None:
        """后台探测公网 IP（联网请求要放到线程里，否则界面会卡住）。"""
        if self._ip_probing:
            return
        self._ip_probing = True
        if not auto:
            self.log("正在探测公网 IP…")
            try:
                self.btn_pubip.state(["disabled"])
            except Exception:
                pass
        threading.Thread(target=self._probe_pubip, args=(auto,), daemon=True).start()

    def _probe_pubip(self, auto: bool) -> None:
        ip = wa.detect_public_ip()
        try:
            self.root.after(0, lambda: self._apply_pubip(ip, auto))
        except Exception:
            pass

    def _apply_pubip(self, ip: str, auto: bool) -> None:
        self._ip_probing = False
        if not auto:
            try:
                self.btn_pubip.state(["!disabled"])
            except Exception:
                pass
        if not ip:
            if not auto:
                self.log("没探到公网 IP —— 服务器可能没有直接出网，或被防火墙/代理挡住了。", "warn")
                self.log("  内网机器这很正常：公网地址要看你路由器的宽带 IP，"
                         "或云控制台里的弹性 IP，直接手填到「服务器地址」里即可。")
            return

        self._pubip = ip
        cur = (self.var_puburl.get() or "").strip()
        host = self._url_host(cur)
        if host and not wa.is_private_ip(host):
            # 已经是公网地址或域名，不动它
            if not auto:
                self._set_puburl(self._replace_url_host(cur, ip))
                self.log(f"公网 IP：{ip}（当前填的是 {host}，已替换）", "ok")
            return
        if auto and self._puburl_touched:
            return
        if cur:
            self._set_puburl(self._replace_url_host(cur, ip))
        else:
            self._set_puburl(f"{'https' if self.var_tls_on.get() else 'http'}://{ip}:"
                             f"{self.var_port.get()}")
        self.log(f"已自动填入公网 IP：{ip}"
                 + ("（客户端与服务器在同一局域网时，请把这里改回内网 IP）" if auto else ""), "ok")

    # ---- 页 4：数据库 ----

    _DB_FIELDS = ("name", "kind", "host", "port", "user", "password",
                  "database", "max_rows", "timeout_sec", "connect_timeout_sec")

    def _tab_database(self) -> ttk.Frame:
        # 和页①②③一样包进可滚动容器 —— 这一页的说明 + 数据源列表 + 编辑表单
        # 加起来比一屏高，裸 Frame 的话下面一半直接看不到也滚不动。
        outer, f = self._scroll_tab()
        f.columnconfigure(0, weight=1)

        tip = tk.Label(
            f,
            justify="left", anchor="w", wraplength=780, padx=12, pady=8,
            background="#eef4ff", foreground="#1f2937",
            text=("登记服务器上的 MySQL / SQL Server / Redis 后，AI 就能通过 MCP 一并查看数据，"
                  "不必再单独开一个数据库客户端。\n"
                  "默认全部「只读」：只放行 SELECT / SHOW / DESC 这类查询。"
                  "要允许 AI 改数据，得逐个勾上「允许写入」，且服务端不能是只读模式、"
                  "用的还得是主 Token —— 三个条件缺一不可。\n"
                  "无论怎么配，危险动作（INTO OUTFILE、LOAD_FILE、xp_cmdshell、"
                  "多语句、FLUSHALL、CONFIG、EVAL…）永远被拒绝。"
                  "一个都不加 = 数据库能力完全关闭。"),
        )
        tip.grid(row=0, column=0, sticky="ew")

        g1 = ttk.LabelFrame(f, text=" 数据源 ", padding=(12, 10))
        g1.grid(row=1, column=0, sticky="ew", pady=(10, 0))
        g1.columnconfigure(0, weight=1)

        cols = ("name", "type", "target", "mode")
        self.db_tree = ttk.Treeview(g1, columns=cols, show="headings", height=5,
                                    selectmode="browse")
        for key, label, width, stretch in (
            ("name", "名称", 150, False), ("type", "类型", 80, False),
            ("target", "地址", 330, True), ("mode", "权限", 80, False),
        ):
            self.db_tree.heading(key, text=label)
            self.db_tree.column(key, width=width, anchor="w", stretch=stretch)
        self.db_tree.grid(row=0, column=0, sticky="ew")
        self.db_tree.bind("<<TreeviewSelect>>", self._db_on_select)

        bar = ttk.Frame(g1)
        bar.grid(row=1, column=0, sticky="ew", pady=(8, 0))
        ttk.Button(bar, text="＋ MySQL", width=11,
                   command=lambda: self._db_add("mysql")).pack(side="left")
        ttk.Button(bar, text="＋ SQL Server", width=12,
                   command=lambda: self._db_add("sqlserver")).pack(side="left", padx=(6, 0))
        ttk.Button(bar, text="＋ Redis", width=11,
                   command=lambda: self._db_add("redis")).pack(side="left", padx=(6, 0))
        ttk.Button(bar, text="删除选中", width=10,
                   command=self._db_remove).pack(side="left", padx=(6, 0))
        ttk.Button(bar, text="测试连接", width=10,
                   command=self._db_test).pack(side="left", padx=(6, 0))
        ttk.Label(bar, textvariable=self.var_db_hint, foreground="#666").pack(
            side="left", padx=(12, 0))

        g2 = ttk.LabelFrame(
            f, text=" 编辑选中的数据源（改动即时记在本页，「保存配置」时才落盘） ",
            padding=(12, 10))
        g2.grid(row=2, column=0, sticky="ew", pady=(12, 0))
        g2.columnconfigure(1, weight=1)
        g2.columnconfigure(3, weight=1)

        self.db_vars = {k: tk.StringVar() for k in self._DB_FIELDS}
        self._db_entries: list = []

        def field(row, col, label, key, show=None):
            ttk.Label(g2, text=label).grid(row=row, column=col * 2, sticky="w",
                                           padx=(0, 6), pady=3)
            entry = ttk.Entry(g2, textvariable=self.db_vars[key], width=24,
                              show=show or "")
            entry.grid(row=row, column=col * 2 + 1, sticky="ew", padx=(0, 16), pady=3)
            self._db_entries.append(entry)
            return entry

        field(0, 0, "名称", "name")
        ttk.Label(g2, text="类型").grid(row=0, column=2, sticky="w", padx=(0, 6), pady=3)
        self.lbl_db_kind = ttk.Label(g2, text="—", foreground="#666")
        self.lbl_db_kind.grid(row=0, column=3, sticky="w", pady=3)

        field(1, 0, "主机", "host")
        field(1, 1, "端口", "port")
        field(2, 0, "用户名", "user")
        field(2, 1, "密码", "password", show="*")
        field(3, 0, "数据库 / 库号", "database")
        field(3, 1, "最大返回行/键数", "max_rows")
        field(4, 0, "查询超时（秒）", "timeout_sec")
        field(4, 1, "连接超时（秒）", "connect_timeout_sec")

        holder = ttk.Frame(g2)
        holder.grid(row=5, column=0, columnspan=4, sticky="w", pady=3)
        self.chk_db_write = ttk.Checkbutton(
            holder, text="允许写入（放开 INSERT/UPDATE/DELETE 与 Redis 写命令）",
            variable=self.var_db_write)
        self.chk_db_write.pack(side="left")

        self.lbl_db_note = ttk.Label(g2, text="", foreground="#666", justify="left")
        self.lbl_db_note.grid(row=6, column=0, columnspan=4, sticky="w", pady=(8, 0))

        for var in list(self.db_vars.values()) + [self.var_db_write]:
            var.trace_add("write", self._db_field_changed)
        self._db_set_enabled(False)
        self._refresh_db_note()
        return outer

    # ---- 数据库页的内部逻辑 ----

    def _db_kind_of(self, item: dict) -> str:
        return str((item or {}).get("type") or "mysql").lower()

    def _db_set_enabled(self, enabled: bool) -> None:
        state = "normal" if enabled else "disabled"
        for entry in getattr(self, "_db_entries", []):
            entry.configure(state=state)
        chk = getattr(self, "chk_db_write", None)
        if chk is not None:
            chk.configure(state=state)

    def _db_hint_set(self, text: str) -> None:
        self.var_db_hint.set(text)

    def _db_row_values(self, item: dict) -> tuple:
        kind = self._db_kind_of(item)
        host = item.get("host") or "127.0.0.1"
        if kind == "redis":
            port = item.get("port") or 6379
            who = (item.get("username") or "").strip()
            target = f"{who + '@' if who else ''}{host}:{port}/{item.get('db') or 0}"
            type_label = "Redis"
        elif kind in ("sqlserver", "sqlsrv", "mssql", "sqsrv"):
            port = item.get("port") or 1433
            target = f"{item.get('user') or 'sa'}@{host}:{port}/{item.get('database') or ''}"
            type_label = "SQL Server"
        else:
            port = item.get("port") or 3306
            target = f"{item.get('user') or 'root'}@{host}:{port}/{item.get('database') or ''}"
            type_label = "MySQL"
        return (item.get("name") or "(未命名)", type_label, target,
                "可写" if item.get("allow_write") else "只读")

    def _db_refresh_row(self, idx: int) -> None:
        rows = self.db_tree.get_children()
        if 0 <= idx < len(rows):
            self.db_tree.item(rows[idx], values=self._db_row_values(self.db_items[idx]))

    def _db_fill_form(self, item: dict) -> None:
        kind = self._db_kind_of(item)
        is_redis = kind == "redis"
        is_sqlsrv = kind in ("sqlserver", "sqlsrv", "mssql", "sqsrv")
        default_port = 6379 if is_redis else (1433 if is_sqlsrv else 3306)
        self._db_syncing = True
        try:
            self.db_vars["name"].set(item.get("name") or "")
            self.db_vars["host"].set(item.get("host") or "")
            self.db_vars["port"].set(str(item.get("port") or default_port))
            if is_redis:
                self.db_vars["user"].set(item.get("username") or "")
                self.db_vars["database"].set(str(item.get("db") or 0))
                self.db_vars["max_rows"].set(str(item.get("max_keys") or 200))
                self.lbl_db_kind.configure(text="Redis")
                default_timeout = 15
            elif is_sqlsrv:
                self.db_vars["user"].set(item.get("user") or "sa")
                self.db_vars["database"].set(item.get("database") or "")
                self.db_vars["max_rows"].set(str(item.get("max_rows") or 200))
                self.lbl_db_kind.configure(text="SQL Server")
                default_timeout = 30
            else:
                self.db_vars["user"].set(item.get("user") or "")
                self.db_vars["database"].set(item.get("database") or "")
                self.db_vars["max_rows"].set(str(item.get("max_rows") or 200))
                self.lbl_db_kind.configure(text="MySQL / MariaDB")
                default_timeout = 30
            self.db_vars["password"].set(item.get("password") or "")
            self.db_vars["timeout_sec"].set(str(item.get("timeout_sec") or default_timeout))
            self.db_vars["connect_timeout_sec"].set(
                str(item.get("connect_timeout_sec") or 8))
            self.var_db_write.set(bool(item.get("allow_write")))
        finally:
            self._db_syncing = False
        self._db_set_enabled(True)
        self._refresh_db_note()

    def _db_on_select(self, _event=None) -> None:
        sel = self.db_tree.selection()
        if not sel:
            return
        idx = self.db_tree.index(sel[0])
        if idx >= len(self.db_items):
            return
        self._db_sel = idx
        self._db_fill_form(self.db_items[idx])

    def _db_field_changed(self, *_args) -> None:
        """表单改动即时写回工作副本 —— 否则用户在两个数据源之间来回切会丢改动。"""
        if self._db_syncing:
            return
        idx = self._db_sel
        if idx < 0 or idx >= len(self.db_items):
            return
        item = self.db_items[idx]
        kind = self._db_kind_of(item)
        item["name"] = self.db_vars["name"].get().strip()
        item["host"] = self.db_vars["host"].get().strip()
        item["password"] = self.db_vars["password"].get()

        raw = self.db_vars["port"].get().strip()
        if raw:
            try:
                item["port"] = int(raw)
            except ValueError:
                pass  # 输入到一半（比如刚敲了个负号），保存时才报错
        raw = self.db_vars["timeout_sec"].get().strip()
        if raw:
            try:
                item["timeout_sec"] = float(raw)
            except ValueError:
                pass
        raw = self.db_vars["connect_timeout_sec"].get().strip()
        if raw:
            try:
                item["connect_timeout_sec"] = float(raw)
            except ValueError:
                pass

        if kind == "redis":
            item["username"] = self.db_vars["user"].get().strip()
            raw_db = self.db_vars["database"].get().strip()
            item["db"] = int(raw_db) if raw_db.isdigit() else raw_db
            raw_max = self.db_vars["max_rows"].get().strip()
            if raw_max.isdigit():
                item["max_keys"] = int(raw_max)
        else:
            item["user"] = self.db_vars["user"].get().strip()
            item["database"] = self.db_vars["database"].get().strip()
            raw_max = self.db_vars["max_rows"].get().strip()
            if raw_max.isdigit():
                item["max_rows"] = int(raw_max)

        item["allow_write"] = bool(self.var_db_write.get())
        self._db_refresh_row(idx)

    def _refresh_db_note(self) -> None:
        idx = self._db_sel
        kind = self._db_kind_of(self.db_items[idx]) if 0 <= idx < len(self.db_items) else ""
        if kind == "redis":
            note = ("Redis：用户名留空即只用 requirepass 密码；「库号」为 0-15 的整数。"
                    "列 key 一律走 SCAN，不会像 KEYS 那样阻塞实例。"
                    "「查询超时」管收发命令，「连接超时」管拨号（跨地域/走公网调大些）。")
        elif kind in ("sqlserver", "sqlsrv", "mssql", "sqsrv"):
            note = ("SQL Server：仅支持 SQL 身份验证（sa 等账号，不支持 Windows 集成认证），"
                    "需 SQL Server 2008 及以上。连接全程 TLS 加密，服务器用默认自签名证书"
                    "即可，无需额外配置。「数据库」是初始库，可留空。")
        elif kind == "mysql":
            note = ("MySQL：库名可留空，之后在 SQL 里用「库名.表名」限定即可。"
                    "「最大返回行/键数」限制单次回传的行数，防止一个大 SELECT 拖垮服务。"
                    "「连接超时」是拨号+解析的总预算，调大只影响连不上的等待上限。")
        else:
            note = "点上面的「＋ MySQL」「＋ SQL Server」或「＋ Redis」添加一个数据源。"
        self.lbl_db_note.configure(text=note)

    def _db_add(self, kind: str) -> None:
        existing = {str(i.get("name")) for i in self.db_items}
        n = 1
        while f"{kind}{n}" in existing:
            n += 1
        if kind == "redis":
            item = {"name": f"redis{n}", "type": "redis", "host": "127.0.0.1",
                    "port": 6379, "username": "", "password": "", "db": 0,
                    "allow_write": False, "max_keys": 200, "timeout_sec": 15,
                    "connect_timeout_sec": 8}
        elif kind == "sqlserver":
            item = {"name": f"sqlserver{n}", "type": "sqlserver",
                    "host": "127.0.0.1", "port": 1433, "user": "sa",
                    "password": "", "database": "", "allow_write": False,
                    "max_rows": 200, "timeout_sec": 30, "connect_timeout_sec": 8}
        else:
            item = {"name": f"mysql{n}", "type": "mysql", "host": "127.0.0.1",
                    "port": 3306, "user": "root", "password": "", "database": "",
                    "allow_write": False, "max_rows": 200, "timeout_sec": 30,
                    "connect_timeout_sec": 8}
        self.db_items.append(item)
        row = self.db_tree.insert("", "end", values=self._db_row_values(item))
        self._db_sel = len(self.db_items) - 1
        self.db_tree.selection_set(row)
        self.db_tree.focus(row)
        self._db_fill_form(item)
        self._db_hint_set(f"已添加「{item['name']}」，填好地址账号后点「测试连接」")

    def _db_remove(self) -> None:
        sel = self.db_tree.selection()
        if not sel:
            self._db_hint_set("先在列表里选中要删除的数据源")
            return
        idx = self.db_tree.index(sel[0])
        name = self.db_items[idx].get("name")
        self.db_tree.delete(sel[0])
        self.db_items.pop(idx)
        self._db_sel = -1
        self._db_syncing = True
        try:
            for var in self.db_vars.values():
                var.set("")
            self.var_db_write.set(False)
        finally:
            self._db_syncing = False
        self.lbl_db_kind.configure(text="—")
        self._db_set_enabled(False)
        self._refresh_db_note()
        self._db_hint_set(f"已移除「{name}」（点「保存配置」后生效）")

    def _db_test(self) -> None:
        idx = self._db_sel
        if idx < 0 or idx >= len(self.db_items):
            self._db_hint_set("先在列表里选中一个数据源")
            return
        item = dict(self.db_items[idx])
        self._db_hint_set(f"正在连接「{item.get('name')}」…")
        threading.Thread(target=self._db_test_worker, args=(item,), daemon=True).start()

    def _db_test_worker(self, item: dict) -> None:
        """连接测试放到后台线程 —— 目标不可达时最长会等好几秒，不能卡住界面。"""
        name = item.get("name")
        kind = self._db_kind_of(item)
        try:
            if kind == "redis":
                if wa.redismod is None:
                    raise RuntimeError("Redis 模块未加载（server 目录缺少 wkfile_redis.py）")
                info = wa.redismod.RedisSource(item).test()
                msg = (f"「{name}」连接正常：Redis {info.get('server_version')}"
                       f"，当前库 {info.get('dbsize')} 个 key，角色 {info.get('role')}")
            elif kind in ("sqlserver", "sqlsrv", "mssql", "sqsrv"):
                if getattr(wa, "sqlsrvmod", None) is None:
                    raise RuntimeError(
                        "SQL Server 模块未加载（server 目录缺少 wkfile_sqlsrv.py）")
                info = wa.sqlsrvmod.SQLServerSource(item).test()
                dbs = info.get("databases") or []
                msg = (f"「{name}」连接正常：{info.get('server_version')}"
                       f"，可见 {len(dbs)} 个库")
            else:
                if wa.dbmod is None:
                    raise RuntimeError("MySQL 模块未加载（server 目录缺少 wkfile_mysql.py）")
                info = wa.dbmod.MySQLSource(item).test()
                dbs = info.get("databases") or []
                msg = (f"「{name}」连接正常：MySQL {info.get('server_version')}"
                       f"，认证 {info.get('auth_plugin')}，可见 {len(dbs)} 个库")
        except Exception as exc:  # noqa: BLE001
            msg = f"「{name}」连接失败：{exc}"
        self.root.after(0, lambda: self._db_after_test(msg))

    def _db_after_test(self, msg: str) -> None:
        self._db_hint_set(msg)
        self.log(msg, "err" if "失败" in msg else "ok")

    def _load_databases(self) -> None:
        raw = getattr(self.cfg, "databases", None) or []
        self.db_items = [dict(x) for x in raw if isinstance(x, dict)]
        self.db_tree.delete(*self.db_tree.get_children())
        for item in self.db_items:
            self.db_tree.insert("", "end", values=self._db_row_values(item))
        self._db_sel = -1
        self._db_syncing = True
        try:
            for var in self.db_vars.values():
                var.set("")
            self.var_db_write.set(False)
        finally:
            self._db_syncing = False
        self.lbl_db_kind.configure(text="—")
        self._db_set_enabled(False)
        self._refresh_db_note()
        self._db_hint_set(f"已配置 {len(self.db_items)} 个数据源" if self.db_items
                          else "还没有配置数据源（不配 = 数据库能力关闭）")
        if self.db_items:
            rows = self.db_tree.get_children()
            if rows:
                self.db_tree.selection_set(rows[0])

    def _collect_databases(self) -> list:
        """把界面上的工作副本整理成可写入配置的干净结构，顺手做完整校验。"""
        out = []
        seen = set()
        for i, item in enumerate(self.db_items, 1):
            name = str(item.get("name") or "").strip()
            if not name:
                raise ValueError(f"第 {i} 个数据源还没填名称")
            if name in seen:
                raise ValueError(f"数据源名称重复：{name}（每个数据源的名字必须唯一）")
            seen.add(name)
            kind = self._db_kind_of(item)
            if kind not in ("mysql", "mariadb", "sqlserver", "sqlsrv",
                            "mssql", "sqsrv", "redis"):
                raise ValueError(f"数据源「{name}」的类型不认识：{kind}")
            host = str(item.get("host") or "").strip()
            if not host:
                raise ValueError(f"数据源「{name}」没填主机地址")
            try:
                port = int(item.get("port"))
            except (TypeError, ValueError):
                raise ValueError(f"数据源「{name}」的端口必须是数字")
            if not (1 <= port <= 65535):
                raise ValueError(f"数据源「{name}」的端口必须在 1-65535 之间")
            try:
                timeout = float(item.get("timeout_sec") or 0) or (
                    30.0 if kind != "redis" else 15.0)
            except (TypeError, ValueError):
                raise ValueError(f"数据源「{name}」的超时必须是数字（秒）")
            if timeout <= 0:
                raise ValueError(f"数据源「{name}」的超时必须大于 0")
            try:
                conn_timeout = float(item.get("connect_timeout_sec") or 0) or 8.0
            except (TypeError, ValueError):
                raise ValueError(f"数据源「{name}」的连接超时必须是数字（秒）")
            if conn_timeout <= 0:
                raise ValueError(f"数据源「{name}」的连接超时必须大于 0")
            entry = {
                "name": name,
                "type": kind,
                "host": host,
                "port": port,
                "password": str(item.get("password") or ""),
                "allow_write": bool(item.get("allow_write")),
                "timeout_sec": timeout,
                "connect_timeout_sec": conn_timeout,
            }
            if kind == "redis":
                raw_db = item.get("db", 0)
                try:
                    db_index = int(raw_db or 0)
                except (TypeError, ValueError):
                    raise ValueError(f"数据源「{name}」的库号必须是整数")
                if not (0 <= db_index <= 15):
                    raise ValueError(f"数据源「{name}」的库号必须在 0-15 之间")
                entry["username"] = str(item.get("username") or "")
                entry["db"] = db_index
                try:
                    entry["max_keys"] = max(1, int(item.get("max_keys") or 200))
                except (TypeError, ValueError):
                    entry["max_keys"] = 200
            else:
                entry["user"] = str(item.get("user") or "")
                entry["database"] = str(item.get("database") or "")
                try:
                    entry["max_rows"] = max(1, int(item.get("max_rows") or 200))
                except (TypeError, ValueError):
                    entry["max_rows"] = 200
            out.append(entry)
        return out

    # ---- 页 5：关于 ----

    def _tab_about(self) -> ttk.Frame:
        outer, f = self._scroll_tab()
        f.columnconfigure(0, weight=1)

        # —— 作者卡片 ——
        card = ttk.LabelFrame(f, text=" 作者 ", padding=(16, 12))
        card.grid(row=0, column=0, sticky="ew")
        card.columnconfigure(1, weight=1)
        ttk.Label(card, text=AUTHOR, font=(FONT, 20, "bold"),
                  foreground="#b3261e").grid(row=0, column=0, sticky="w")
        ttk.Label(card, text=f"{APP_TITLE}  v{VERSION}",
                  font=(FONT, 11), foreground="#666").grid(
            row=0, column=1, sticky="w", padx=(12, 0), pady=(6, 0))
        ttk.Label(card, text=SUBTITLE, foreground="#444",
                  wraplength=640, justify="left").grid(
            row=1, column=0, columnspan=2, sticky="w", pady=(8, 0))
        ttk.Label(card, foreground="#666", wraplength=640, justify="left",
                  text=("这个程序是我个人写的：把服务器上的文件操作、连接自检、"
                        "自签证书、MySQL / Redis 查询都收进一个绿色 exe，"
                        "全程零第三方依赖、不装 Python 也能跑。\n"
                        "它是免费的，也没有任何联网校验或功能限制。")).grid(
            row=2, column=0, columnspan=2, sticky="w", pady=(8, 0))

        # —— 联系方式 ——
        tip = ttk.LabelFrame(f, text=" 联系方式 ", padding=(16, 12))
        tip.grid(row=1, column=0, sticky="ew", pady=(12, 0))
        tip.columnconfigure(1, weight=1)

        ttk.Label(tip, text="作者姓名", foreground="#666").grid(
            row=0, column=0, sticky="w", pady=3)
        ttk.Label(tip, text=AUTHOR, font=(FONT, 12, "bold")).grid(
            row=0, column=1, sticky="w", padx=(14, 0), pady=3)

        ttk.Label(tip, text="微信号", foreground="#666").grid(
            row=1, column=0, sticky="w", pady=3)
        row1 = ttk.Frame(tip)
        row1.grid(row=1, column=1, sticky="w", padx=(14, 0), pady=3)
        self.var_wechat = tk.StringVar(value=AUTHOR_WECHAT)
        ttk.Entry(row1, textvariable=self.var_wechat, width=26, state="readonly",
                  font=(MONO, 11)).pack(side="left")
        ttk.Button(row1, text="复制", width=6,
                   command=self._copy_wechat).pack(side="left", padx=(8, 0))
        ttk.Label(row1, text="（微信搜索这个号就能加我）",
                  foreground="#666").pack(side="left", padx=(10, 0))

        ttk.Label(tip, foreground="#444", wraplength=780, justify="left",
                  text=("有问题或建议欢迎加微信沟通（备注「wkfile」）。"
                        "程序本身不联网、不上传任何数据，"
                        "这里只是把我的联系方式写在界面上而已。")).grid(
            row=2, column=0, columnspan=2, sticky="w", pady=(12, 0))

        # —— 其它入口 ——
        more = ttk.LabelFrame(f, text=" 相关 ", padding=(16, 12))
        more.grid(row=2, column=0, sticky="ew", pady=(12, 0))
        ttk.Button(more, text="查看完整文档（README.md）",
                   command=self._open_readme).pack(side="left")
        ttk.Button(more, text="打开数据目录",
                   command=lambda: self._open_path(wa.data_dir())).pack(
            side="left", padx=(8, 0))
        ttk.Label(more, text="（想看配置长什么样、或手动填数据库，都在数据目录里）",
                  foreground="#666").pack(side="left", padx=(10, 0))
        return outer

    def _copy_wechat(self) -> None:
        text = self.var_wechat.get()
        try:
            self.root.clipboard_clear()
            self.root.clipboard_append(text)
            self.root.update_idletasks()
        except Exception:  # noqa: BLE001
            pass
        self.log(f"微信号已复制：{text}", "ok")

    def _open_readme(self) -> None:
        for cand in (os.path.join(wa.program_dir(), "README.md"),
                     os.path.join(os.path.dirname(wa.program_dir()), "README.md")):
            if os.path.isfile(cand):
                self._open_path(cand)
                return
        self._open_path(wa.program_dir())

    @staticmethod
    def _open_path(path: str) -> None:
        try:
            os.startfile(path)                      # noqa: S606 —— Windows 专用
        except Exception:                           # noqa: BLE001
            try:
                subprocess.Popen(["explorer", os.path.normpath(path)])
            except Exception:                       # noqa: BLE001
                pass

    def _sync_puburl_port(self, *_args) -> None:
        """端口输入变化时，把「服务器地址」URL 里的端口同步成新值。"""
        raw = str(self.var_puburl.get() or "").strip()
        port = str(self.var_port.get() or "").strip()
        if not raw or not port.isdigit():
            return
        m = re.match(r"^(https?://)([^/:]+)(:\d+)?(/.*)?$", raw, re.IGNORECASE)
        if not m:
            return
        new = f"{m.group(1)}{m.group(2)}:{port}{m.group(4) or ''}"
        if new != raw:
            self._set_puburl(new)

    def _load_form(self) -> None:
        cfg = self.cfg
        self.var_host.set(cfg.host or "0.0.0.0")
        self.var_port.set(str(cfg.port or 8788))
        if not cfg.token:
            cfg.token = secrets.token_urlsafe(36)
        self.var_token.set(cfg.token)
        self.var_ro_token.set(getattr(cfg, "readonly_token", "") or "")
        ttl = float(getattr(cfg, "token_ttl_days", 0) or 0)
        self.var_token_ttl.set("永久" if ttl <= 0 else ("%g" % ttl))
        exp = str(getattr(cfg, "token_expires_at", "") or "")
        if not exp and ttl > 0:
            exp = wa.make_expiry(ttl)
        self.var_token_exp.set(exp)
        self.var_readonly.set(bool(cfg.read_only))
        self.var_allowdel.set(bool(cfg.allow_delete))
        self.var_allowmove.set(bool(getattr(cfg, "allow_move", True)))
        self.var_backup.set(bool(cfg.auto_backup))
        self.var_preflight.set(bool(getattr(cfg, "preflight_check", True)))
        self.var_mcp.set(bool(getattr(cfg, "enable_mcp", True)))
        try:
            mb = int(cfg.audit_max_bytes) / (1024 * 1024) if int(cfg.audit_max_bytes or 0) else 0
            self.var_audit_mb.set(("%g" % mb))
        except (TypeError, ValueError):
            self.var_audit_mb.set("5")
        try:
            rmb = int(getattr(cfg, "run_log_max_bytes", 0) or 0)
            self.var_runlog_mb.set(("%g" % (rmb / (1024 * 1024))) if rmb else "0")
        except (TypeError, ValueError):
            self.var_runlog_mb.set("2")
        disabled = wa.disabled_tools(cfg)
        for name, var in self.var_tools.items():
            var.set(name not in disabled)
        self.lst_roots.delete(0, "end")
        for r in cfg.roots or []:
            self.lst_roots.insert("end", r)
        self.txt_ips.delete("1.0", "end")
        if cfg.ip_allowlist:
            self.txt_ips.insert("1.0", "\n".join(cfg.ip_allowlist))
        cert = str(getattr(cfg, "tls_cert", "") or "")
        key = str(getattr(cfg, "tls_key", "") or "")
        chain = str(getattr(cfg, "tls_chain", "") or "")
        self.var_tls_cert.set(cert)
        self.var_tls_key.set(key)
        self.var_tls_chain.set(chain)
        self.var_tls_on.set(bool(cert and key))
        self.var_tls_info.set("")
        self.cmd_toggle_tls(quiet=True)
        self._set_puburl(f"{'https' if cert and key else 'http'}://{local_ipv4()}:"
                         f"{self.var_port.get()}")
        self._puburl_touched = False
        self._refresh_token_exp_label()
        self._load_databases()
        # 默认填的是内网 IP；客户端在别的机器/别的网络时那个地址根本到不了。
        # 后台探一次公网 IP 填上（探不到就保持内网 IP，不影响任何功能）。
        self.root.after(1200, lambda: self.cmd_detect_pubip(auto=True))

    def collect(self) -> dict:
        try:
            port = int(str(self.var_port.get()).strip())
        except ValueError:
            raise ValueError("端口必须是 1-65535 的数字")
        if not (1 <= port <= 65535):
            raise ValueError("端口必须是 1-65535 的数字")
        host = (self.var_host.get() or "0.0.0.0").strip()
        roots = [str(x).strip() for x in self.lst_roots.get(0, "end") if str(x).strip()]
        ips = [ln.strip() for ln in self.txt_ips.get("1.0", "end").splitlines() if ln.strip()]
        token = (self.var_token.get() or "").strip()
        if not token:
            token = secrets.token_urlsafe(36)
            self.var_token.set(token)
        ro_token = (self.var_ro_token.get() or "").strip()
        if ro_token and ro_token == token:
            raise ValueError("只读 Token 不能和主 Token 相同（否则它就不是只读的了）")
        # HTTPS：勾了就必须能配对加载，否则保存这个配置等于给自己埋一颗启动炸弹
        tls_cert = (self.var_tls_cert.get() or "").strip()
        tls_key = (self.var_tls_key.get() or "").strip()
        tls_chain = (self.var_tls_chain.get() or "").strip()
        if self.var_tls_on.get():
            if not tls_cert or not tls_key:
                raise ValueError("勾了 HTTPS，就要把证书和私钥都选上")
            try:
                wa.validate_tls_pair(tls_cert, tls_key, tls_chain)
            except wa.PathError as exc:
                raise ValueError("HTTPS 证书有问题：" + exc.message)
        else:
            tls_cert = ""
            tls_key = ""
            tls_chain = ""
        try:
            audit_mb = float(str(self.var_audit_mb.get()).strip() or "0")
        except ValueError:
            raise ValueError("审计日志轮转大小必须填数字（MB），例如 5")
        if audit_mb < 0:
            raise ValueError("审计日志轮转大小不能为负数")
        try:
            runlog_mb = float(str(self.var_runlog_mb.get()).strip() or "0")
        except ValueError:
            raise ValueError("运行日志轮转大小必须填数字（MB），例如 2")
        if runlog_mb < 0:
            raise ValueError("运行日志轮转大小不能为负数")
        ttl = self._ttl_days()
        expires = (self.var_token_exp.get() or "").strip()
        if ttl <= 0:
            expires = ""                     # 永久有效
        elif not expires:
            expires = wa.make_expiry(ttl)
        return {
            "host": host,
            "port": port,
            "token": token,
            "token_ttl_days": ttl,
            "token_expires_at": expires,
            "readonly_token": ro_token,
            "tls_cert": tls_cert,
            "tls_chain": tls_chain,
            "tls_key": tls_key,
            "roots": roots,
            "ip_allowlist": ips,
            "read_only": bool(self.var_readonly.get()),
            "allow_delete": bool(self.var_allowdel.get()),
            "allow_move": bool(self.var_allowmove.get()),
            "auto_backup": bool(self.var_backup.get()),
            "preflight_check": bool(self.var_preflight.get()),
            "enable_mcp": bool(self.var_mcp.get()),
            "tools_disable": sorted(n for n, v in self.var_tools.items() if not v.get()),
            "audit_max_bytes": int(audit_mb * 1024 * 1024),
            "run_log_max_bytes": int(runlog_mb * 1024 * 1024),
            "databases": self._collect_databases(),
        }

    def save_config(self, silent: bool = False) -> bool:
        try:
            form = self.collect()
        except ValueError as exc:
            messagebox.showerror("配置有误", str(exc), parent=self.root)
            return False

        if not form["roots"]:
            if not silent:
                messagebox.showwarning(
                    "缺少目录",
                    "请先在「基础设置」页添加至少一个允许操作的目录。",
                    parent=self.root,
                )
            return False

        try:
            cfg = wa.load_config(CONFIG_PATH)
        except ValueError as exc:
            # 磁盘上的配置有语法错误（多半是手改坏的）。这里直接放弃合并
            # 旧值：界面里当前显示的就是用户要保存的全部内容，
            # 用它整体覆盖那个坏文件正是修复手段之一。
            self.log(f"磁盘配置解析失败（{exc}），将用本次界面内容整体覆盖", "warn")
            cfg = wa.Config({}, CONFIG_PATH)
        for k, v in form.items():
            setattr(cfg, k, v)
        cfg.roots = [os.path.abspath(r) for r in form["roots"]]
        try:
            os.makedirs(LOG_DIR, exist_ok=True)
        except OSError:
            pass
        if not cfg.audit_log or not os.path.isabs(cfg.audit_log):
            cfg.audit_log = os.path.join(LOG_DIR, wa.AUDIT_FILENAME)
        try:
            cfg.save()
        except OSError as exc:
            messagebox.showerror("保存失败", f"写配置失败：{exc}", parent=self.root)
            return False

        missing = [r for r in cfg.roots if not os.path.isdir(r)]
        self.cfg = cfg
        self.log(f"配置已保存 → {CONFIG_PATH}")
        self.log(f"   监听 {cfg.host}:{cfg.port}   只读={cfg.read_only}   目录数={len(cfg.roots)}")
        disabled = wa.disabled_tools(cfg)
        if disabled:
            self.log(f"   已禁用 {len(disabled)} 个工具：" + ", ".join(sorted(disabled)), "warn")
        self.log(f"   删除={cfg.allow_delete}  移动={cfg.allow_move}  "
                 f"备份={cfg.auto_backup}  写前校验={getattr(cfg, 'preflight_check', True)}")
        self.log(f"   Token 有效期：{wa.expiry_text(cfg)}"
                 + ("" if wa.expiry_state(getattr(cfg, "token_expires_at", ""))[0] != "expired"
                    else "  ← 已过期，服务会拒绝所有请求"))
        if getattr(cfg, "readonly_token", ""):
            self.log("   只读 Token：已启用（用它的客户端只能读，写/删/移一律被拒）")
        if wa.tls_enabled(cfg):
            self.log("   HTTPS：已启用，客户端地址要写成 https://" +
                     (f"（{getattr(cfg, 'tls_cert', '')}）" if getattr(cfg, "tls_cert", "") else ""))
        self._refresh_token_exp_label()
        if missing:
            self.log("以下目录当前不存在，请确认： " + " ; ".join(missing), "warn")
        self._refresh_audit_size()
        return True

    # ---------------- 日志 / 状态条 ----------------

    def set_hint(self, msg: str, level: str = "info") -> None:
        """把结果同步到常驻状态条，保证点完按钮立刻看得见"""
        fg, bg = {
            "info": ("#1f2937", "#eef4ff"),
            "ok": ("#0b5132", "#e6f7ee"),
            "warn": ("#7a4b00", "#fff6e5"),
            "err": ("#8c1d18", "#fdecea"),
        }.get(level, ("#1f2937", "#eef4ff"))
        try:
            self.var_hint.set(msg)
            self.lbl_hint.configure(foreground=fg, background=bg)
        except Exception:
            pass
        try:
            self.root.update_idletasks()
        except Exception:
            pass

    def log(self, msg: str, level: str = "info") -> None:
        tag = {"info": "  ", "ok": "[OK] ", "warn": "[!] ", "err": "[X] "}.get(level, "  ")
        line = f"{time.strftime('%H:%M:%S')} {tag}{msg}\n"
        self.txt_log.configure(state="normal")
        self.txt_log.insert("end", line)
        self.txt_log.see("end")
        self.txt_log.configure(state="disabled")
        self.set_hint(msg, level)

    # ---------------- 基础页命令 ----------------

    def cmd_check_port(self) -> None:
        raw = str(self.var_port.get()).strip()
        try:
            port = int(raw)
        except ValueError:
            msg = f"端口「{raw}」不是数字，请填 1-65535 之间的整数"
            self.log(msg, "err")
            messagebox.showerror("端口无效", msg, parent=self.root)
            return
        if not (1 <= port <= 65535):
            msg = f"端口 {port} 超出范围，请填 1-65535"
            self.log(msg, "err")
            messagebox.showerror("端口无效", msg, parent=self.root)
            return

        # 先把「正在检查」显示出来，避免用户以为没反应
        self.set_hint(f"正在检查端口 {port} …", "info")
        self.root.update()

        owners = port_owner(port)
        reachable, _ = http_health(port, timeout=2.5)

        if not owners and not reachable:
            msg = f"端口 {port} 空闲可用，直接用它就行。"
            self.log(msg, "ok")
            messagebox.showinfo("端口检查", msg, parent=self.root)
            return

        who = (
            "、".join(f"{n}（PID {p}）" for p, n in owners)
            if owners
            else "未能识别（建议以管理员身份重新打开本程序再查一次）"
        )
        self_ok = reachable

        if self_ok:
            msg = f"端口 {port} 正在被本服务使用：{who}。健康检查通过，属于正常情况。"
            self.log(msg, "ok")
            messagebox.showinfo("端口检查", msg, parent=self.root)
        else:
            msg = f"端口 {port} 已被占用：{who}。请换一个端口，或先结束该进程。"
            self.log(msg, "err")
            if owners:
                pids = "、".join(str(p) for p, _ in owners)
                self.log(f"   结束办法：任务管理器 → 详细信息 → 按 PID {pids} 找到进程 → 右键结束任务")
            messagebox.showwarning("端口检查", msg, parent=self.root)

    def cmd_add_root(self) -> None:
        d = filedialog.askdirectory(title="选择允许 AI 操作的目录", parent=self.root)
        if not d:
            return
        d = os.path.abspath(d)
        existing = list(self.lst_roots.get(0, "end"))
        if d in existing:
            self.log(f"目录已在列表中：{d}", "warn")
            return
        self.lst_roots.insert("end", d)
        self.log(f"已添加目录：{d}")

    def cmd_del_root(self) -> None:
        sel = list(self.lst_roots.curselection())
        if not sel:
            messagebox.showinfo("提示", "请先在列表里选中要移除的目录", parent=self.root)
            return
        for i in reversed(sel):
            self.lst_roots.delete(i)
        self.log(f"已移除 {len(sel)} 个目录")

    def cmd_clear_roots(self) -> None:
        self.lst_roots.delete(0, "end")
        self.log("已清空目录列表")

    # ---------------- 安全页命令 ----------------

    def cmd_gen_token(self) -> None:
        try:
            days = self._ttl_days()
        except ValueError as exc:
            messagebox.showerror("有效期有误", str(exc), parent=self.root)
            return
        if not messagebox.askyesno(
            "重新生成 Token",
            "生成新 Token 后，客户端（跑 MCP 的那台机器）的旧 Token 会立即失效，\n"
            "需要重新把新 Token 填过去。确定继续？",
            parent=self.root,
        ):
            return
        self.var_token.set(secrets.token_urlsafe(36))
        self.var_token_exp.set(wa.make_expiry(days))
        self._refresh_token_exp_label()
        self.log("已生成新 Token（记得点「保存配置」并更新客户端）", "ok")
        self.log("   有效期：" + wa.expiry_text_of(self.var_token_exp.get()))

    def cmd_copy_token(self) -> None:
        self._clip(self.var_token.get())
        self.log("Token 已复制到剪贴板", "ok")

    def cmd_toggle_token(self) -> None:
        self.ent_token.configure(show="" if self.var_show_token.get() else "*")

    def cmd_gen_ro_token(self) -> None:
        self.var_ro_token.set(secrets.token_urlsafe(36))
        self.log("已生成只读 Token（记得点「保存配置」；它只能读，不能改文件）", "ok")

    def cmd_copy_ro_token(self) -> None:
        if not (self.var_ro_token.get() or "").strip():
            self.log("还没有只读 Token，先点「生成」", "warn")
            return
        self._clip(self.var_ro_token.get())
        self.log("只读 Token 已复制到剪贴板", "ok")

    def cmd_clear_ro_token(self) -> None:
        self.var_ro_token.set("")
        self.log("已清空只读 Token（保存后即停用该入口）", "warn")

    def cmd_toggle_ro_token(self) -> None:
        self.ent_ro_token.configure(show="" if self.var_show_ro_token.get() else "*")

    # ---- HTTPS / TLS ----

    def cmd_toggle_tls(self, quiet: bool = False) -> None:
        on = bool(self.var_tls_on.get())
        for w in (self.ent_cert, self.ent_key, self.ent_chain):
            w.configure(state="normal" if on else "disabled")
        try:
            self.btn_clear_chain.configure(state="normal" if on else "disabled")
        except Exception:
            pass
        self._sync_puburl_scheme()
        if quiet:
            return
        if on:
            self.log("已启用 HTTPS：选好证书与私钥后点「检查证书」，保存并重启后生效")
        else:
            self.var_tls_info.set("")
            self.log("已关闭 HTTPS（保存后以明文 HTTP 启动）", "warn")

    def _sync_puburl_scheme(self) -> None:
        """让「服务器地址」的协议跟着 HTTPS 开关走。"""
        raw = str(self.var_puburl.get() or "").strip()
        if not raw:
            return
        want = "https://" if self.var_tls_on.get() else "http://"
        m = re.match(r"^(https?://)(.*)$", raw, re.IGNORECASE)
        if m and m.group(1).lower() != want:
            self._set_puburl(want + m.group(2))

    def cmd_pick_cert(self) -> None:
        p = filedialog.askopenfilename(
            title="选择证书文件（.crt / .pem / .cer）",
            filetypes=[("证书文件", "*.crt *.pem *.cer *.der"), ("所有文件", "*.*")],
            parent=self.root,
        )
        if p:
            self.var_tls_cert.set(p)
            self.cmd_check_cert()

    def cmd_pick_key(self) -> None:
        p = filedialog.askopenfilename(
            title="选择私钥文件（.key / .pem）",
            filetypes=[("私钥文件", "*.key *.pem"), ("所有文件", "*.*")],
            parent=self.root,
        )
        if p:
            self.var_tls_key.set(p)
            self.cmd_check_cert()

    def cmd_pick_chain(self) -> None:
        p = filedialog.askopenfilename(
            title="选择中间证书链文件（chain / ca-bundle / fullchain_x）",
            filetypes=[("证书链文件", "*.crt *.pem *.cer"), ("所有文件", "*.*")],
            parent=self.root,
        )
        if p:
            self.var_tls_chain.set(p)
            self.cmd_check_cert()

    def cmd_clear_chain(self) -> None:
        self.var_tls_chain.set("")
        self.cmd_check_cert()

    def cmd_check_cert(self) -> bool:
        """当场把证书（+可选中间链）和私钥加载一次，配对不上别等启动时才失败。"""
        cert = (self.var_tls_cert.get() or "").strip()
        key = (self.var_tls_key.get() or "").strip()
        chain = (self.var_tls_chain.get() or "").strip()
        if not cert or not key:
            self.var_tls_info.set("证书和私钥都要选")
            return False
        try:
            wa.validate_tls_pair(cert, key, chain)
        except wa.PathError as exc:
            self.var_tls_info.set("✗ " + exc.message)
            self.log("证书检查不通过：" + exc.message, "err")
            return False
        desc = wa.describe_cert(cert)
        n, incomplete = wa.chain_health(cert, chain)
        tail = f" · {desc}" if desc else ""
        if incomplete:
            self.var_tls_info.set(f"⚠ 配对正常，但{wa.CHAIN_INCOMPLETE_SHORT}")
            self.log("HTTPS 证书配对正常，但证书链不完整（能发出去的只有 1 张）" + tail, "warn")
            self.log("  " + wa.CHAIN_INCOMPLETE_SHORT
                     + " —— 浏览器能自动补链所以看不出问题，", "warn")
            self.log("  但 Trae / Cursor 这类 Electron 客户端不补链，会直接连不上。", "warn")
            self.log("  → 两个办法：把中间链文件填进「中间证书链」那一栏，", "warn")
            self.log("     或者第一行直接改成证书商给的 fullchain / 证书链文件。", "warn")
            return True
        merged = "（已合并证书+中间链）" if chain else ""
        self.var_tls_info.set(f"✓ 配对正常 · 链 {n} 张{merged}{tail}")
        self.log(f"HTTPS 证书检查通过（链 {n} 张{merged}{tail}）", "ok")
        return True

    def _selfcert_target_ips(self) -> list:
        """该写进证书 SAN 的 IP 清单：SAN 少一个，客户端按那个地址连就必失败。"""
        ips = ["127.0.0.1"]
        for cand in (local_ipv4(), self._pubip, self._url_host(self.var_puburl.get())):
            cand = (cand or "").strip()
            if not cand or cand in ips:
                continue
            try:
                import ipaddress as _ip
                _ip.ip_address(cand)
            except ValueError:
                continue          # 域名/主机名不进 IP 列表
            ips.append(cand)
        return ips

    def cmd_gen_selfcert(self) -> None:
        """一键生成自签证书：只管生成，装不装、用不用交给用户。"""
        default_cert, default_key = wa.default_selfcert_paths()
        path = filedialog.asksaveasfilename(
            title="保存自签证书（同名 .key 会一起生成）",
            initialdir=os.path.dirname(default_cert),
            initialfile="wkfile.crt",
            defaultextension=".crt",
            filetypes=[("证书文件", "*.crt"), ("PEM 文件", "*.pem"), ("所有文件", "*.*")],
            parent=self.root,
        )
        if not path:
            return
        key_path = os.path.splitext(path)[0] + ".key"
        if os.path.exists(path) or os.path.exists(key_path):
            if not messagebox.askyesno(
                "覆盖确认",
                f"下面这些文件已存在，继续会覆盖：\n\n{path}\n{key_path}\n\n确定要覆盖吗？",
                parent=self.root,
            ):
                return

        ips = self._selfcert_target_ips()
        # 自动探测公网 IP 是启动 1.2 秒后才发的（线程里），用户可能比它更快点到这里。
        # 那就交给 worker 现场再探一次 —— 公网 IP 少写进 SAN，客户端按它连就必失败。
        need_pub = not any(not wa.is_private_ip(x) for x in ips)
        self._selfcert_t0 = time.time()
        try:
            self.btn_selfcert.state(["disabled"])
        except Exception:
            pass
        self.log("正在生成自签证书（RSA 2048，纯本地运算，一般几秒到十几秒）…")
        self.log("  写进证书的地址：" + "、".join(ips)
                 + ("（探到公网 IP 会自动补上）" if need_pub else ""))
        threading.Thread(
            target=self._gen_selfcert_worker,
            args=(path, key_path, ips, need_pub),
            daemon=True,
        ).start()

    def _gen_selfcert_worker(self, cert_path: str, key_path: str, ips: list,
                             need_pub: bool = False) -> None:
        err = ""
        if need_pub:
            pub = wa.detect_public_ip()
            if pub and pub not in ips:
                ips = list(ips) + [pub]
        try:
            wa.generate_selfcert(cert_path, key_path, cn="wkfile", ips=ips)
        except Exception as exc:  # noqa: BLE001
            err = str(getattr(exc, "message", "") or exc)
        try:
            self.root.after(0, lambda: self._gen_selfcert_done(cert_path, key_path, err))
        except Exception:
            pass

    def _gen_selfcert_done(self, cert_path: str, key_path: str, err: str) -> None:
        try:
            self.btn_selfcert.state(["!disabled"])
        except Exception:
            pass
        if err:
            self.var_tls_info.set("✗ 生成失败")
            self.log("自签证书生成失败：" + err, "err")
            return

        self.var_tls_cert.set(cert_path)
        self.var_tls_key.set(key_path)
        self.var_tls_chain.set("")            # 自签证书自带完整链，不需要中间链文件
        self.var_tls_on.set(True)
        self.cmd_toggle_tls(quiet=True)      # 顺带把「服务器地址」的协议头换成 https://
        ok = self.cmd_check_cert()
        spent = time.time() - getattr(self, "_selfcert_t0", time.time())
        self.log(f"自签证书已生成（用时 {spent:.1f} 秒）：{cert_path}", "ok")
        self.log(f"  私钥：{key_path}（不要外传）")
        if ok:
            messagebox.showinfo(
                "自签证书已生成",
                "证书和私钥都已生成并填入本页，检查通过。\n\n"
                f"证书：{cert_path}\n"
                f"私钥：{key_path}\n\n"
                "记得点「保存配置」，再点「启动服务」重启后生效。\n"
                "客户端首次连接会提示证书不受信任，确认一次即可。",
                parent=self.root,
            )
        else:
            messagebox.showwarning(
                "自签证书已生成，但检查未通过",
                "证书已生成并填入本页，但配对检查没通过，详见下方日志。",
                parent=self.root,
            )

    # ---- Token 有效期 ----

    def _ttl_days(self) -> float:
        """把「有效期」下拉/输入框里的值解析成天数；0 表示永久。"""
        raw = (self.var_token_ttl.get() or "").strip()
        if raw in ("", "永久", "永久有效", "不过期"):
            return 0.0
        try:
            days = float(raw)
        except ValueError:
            raise ValueError("Token 有效期请填「永久」或一个天数（如 30）")
        if days < 0:
            raise ValueError("Token 有效期不能是负数")
        return days

    def _apply_token_ttl(self) -> None:
        """按当前下拉值，从现在起重新计算到期时间并刷新显示。"""
        try:
            days = self._ttl_days()
        except ValueError as exc:
            messagebox.showerror("有效期有误", str(exc), parent=self.root)
            return
        self.var_token_exp.set(wa.make_expiry(days))
        self._refresh_token_exp_label()
        if days <= 0:
            self.log("Token 有效期：永久有效（不自动失效）", "ok")
        else:
            self.log(f"Token 有效期已设为 {days:g} 天（从现在起算）；"
                     f"记得点「保存配置」并【重启服务】才生效。", "ok")

    def _refresh_token_exp_label(self) -> None:
        exp = self.var_token_exp.get()
        state, _dt, _remain = wa.expiry_state(exp)
        text = "当前：" + wa.expiry_text_of(exp)
        if state == "expired":
            self.lbl_token_exp.configure(
                text=text + "　→ 服务会拒绝所有请求，请点「生成」换新 Token", foreground="#b3261e"
            )
        elif state == "expiring":
            self.lbl_token_exp.configure(
                text=text + "　→ 快到期了，建议提前点「按此重新计时」续期", foreground="#b26a00"
            )
        else:
            self.lbl_token_exp.configure(text=text, foreground="#666")

    # ---------------- 运行控制 ----------------

    def service_command(self) -> list:
        if is_frozen():
            return [os.path.abspath(sys.executable), "--run", "--config", CONFIG_PATH]
        return [
            os.path.abspath(sys.executable),
            os.path.abspath(__file__),
            "--run",
            "--config",
            CONFIG_PATH,
        ]

    def health(self, timeout: float = 2.0):
        try:
            port = int(str(self.var_port.get()).strip())
        except ValueError:
            return False, "端口无效"
        first = "https" if self.var_tls_on.get() else "http"
        ok, detail = http_health(port, timeout=timeout, scheme=first)
        if ok:
            return ok, detail
        # 界面上勾的 HTTPS 开关和「正在跑的那个服务实际用的协议」可能还没同步
        # （比如刚改完还没重启），所以另一种协议也试一次，别把运行中的服务报成未运行。
        other = "http" if first == "https" else "https"
        ok2, detail2 = http_health(port, timeout=timeout, scheme=other)
        return (ok2, detail2) if ok2 else (ok, detail)

    def refresh_status(self) -> None:
        ok, detail = self.health(timeout=1.5)
        if ok:
            self.var_status.set("● 正在运行")
            self.lbl_status.configure(foreground="#137333")
        else:
            self.var_status.set("● 未运行")
            self.lbl_status.configure(foreground="#b3261e")
        self._refresh_audit_size()
        return ok, detail

    def cmd_start(self) -> None:
        if self.busy:
            return
        if not self.save_config():
            return

        # 启动前的最后一道闸：绝不把无鉴权的文件读写接口挂到网络上
        cfg = wa.load_config(CONFIG_PATH)
        expose = not wa.is_loopback_bind(cfg.host)
        level, reason = wa.token_strength(cfg.token)
        if expose and level == "bad":
            msg = (
                f"监听地址是 {cfg.host}（别的机器能访问），但 {reason}。\n\n"
                "这等于把「任意文件读写」裸放到网络上。已拒绝启动。\n"
                "解决：去「安全」页点「生成」换一个随机 Token，再启动。"
            )
            self.log("拒绝启动：" + msg.splitlines()[0], "err")
            messagebox.showerror("拒绝启动", msg, parent=self.root)
            self.nb.select(1)
            return
        ro_token = str(getattr(cfg, "readonly_token", "") or "")
        if expose and ro_token:
            ro_level, ro_reason = wa.token_strength(ro_token)
            if ro_level == "bad":
                msg = (
                    f"配置了只读 Token，但它{ro_reason}。\n\n"
                    "虽然它不能改文件，但一个弱口令仍会被猜到，等于把目录结构白送出去。\n"
                    "解决：去「安全」页的「只读 Token」栏点「生成」换一个随机的再启动。"
                )
                self.log("拒绝启动：" + msg.splitlines()[0], "err")
                messagebox.showerror("拒绝启动", msg, parent=self.root)
                self.nb.select(1)
                return
        if (
            expose
            and not cfg.read_only
            and (cfg.allow_delete or cfg.allow_move)
            and not self._danger_ack
        ):
            msg = (
                f"当前监听 {cfg.host}，且允许删除/移动、没有开只读模式。\n\n"
                "这意味着：任何拿到 Token 的人都能删改这些目录里的文件。\n"
                "如果只是让 AI 帮你「看」代码，建议勾上「只读模式」。\n\n"
                "仍要继续启动吗？（本次运行不再重复询问）"
            )
            if not messagebox.askyesno("确认高风险配置", msg, parent=self.root):
                self.log("已取消启动（高风险配置未确认）", "warn")
                return
            self._danger_ack = True

        ok, _ = self.health(timeout=1.5)
        if ok:
            self.log("服务已经在运行", "warn")
            self.refresh_status()
            return

        # 端口被别的程序占着就别白启动了，直接拦住并说清是谁
        try:
            port = int(str(self.var_port.get()).strip())
        except ValueError:
            port = 0
        owners = port_owner(port) if port else []
        if owners:
            who = "、".join(f"{n}（PID {p}）" for p, n in owners)
            msg = f"端口 {port} 已被 {who} 占用，换一个端口再启动。"
            self.log(msg, "err")
            messagebox.showwarning("端口被占用", msg, parent=self.root)
            self.nb.select(0)
            return

        cmd = self.service_command()
        flags = 0x00000008 | 0x08000000  # DETACHED_PROCESS | CREATE_NO_WINDOW
        try:
            subprocess.Popen(cmd, cwd=DATA_DIR, close_fds=True, creationflags=flags)
        except OSError as exc:
            self.log(f"启动失败：{exc}", "err")
            messagebox.showerror("启动失败", str(exc), parent=self.root)
            return
        self.log(f"已拉起服务进程：{os.path.basename(cmd[0])} --run")
        self.busy = True
        try:
            ready = False
            for _ in range(24):
                self.root.update()
                time.sleep(0.5)
                if self.health(timeout=1.0)[0]:
                    ready = True
                    break
            if ready:
                self.log("服务已就绪，健康检查通过", "ok")
                self.cmd_test()
            else:
                self.log(f"等了 12 秒还没就绪，请看下方运行日志或 {os.path.relpath(RUN_LOG, DATA_DIR)}", "err")
        finally:
            self.busy = False
            self.refresh_status()

    def _read_pid(self):
        try:
            with open(PID_PATH, "r", encoding="utf-8") as fh:
                return int(fh.read().strip() or 0)
        except Exception:
            return 0

    def cmd_stop(self) -> None:
        pid = self._read_pid()
        stopped = False
        if pid:
            try:
                subprocess.run(
                    ["taskkill", "/PID", str(pid), "/T", "/F"],
                    capture_output=True,
                    creationflags=0x08000000,
                )
                stopped = True
                self.log(f"已终止服务进程 PID={pid}", "ok")
            except OSError as exc:
                self.log(f"taskkill 失败：{exc}", "err")
        else:
            self.log("没找到 PID 文件，尝试按进程名停止", "warn")
            try:
                subprocess.run(
                    ["taskkill", "/IM", "wkfile.exe", "/T", "/F"],
                    capture_output=True,
                    creationflags=0x08000000,
                )
            except OSError:
                pass
        try:
            if os.path.isfile(PID_PATH):
                os.remove(PID_PATH)
        except OSError:
            pass
        time.sleep(0.6)
        ok, _ = self.health(timeout=1.5)
        if ok:
            self.log("端口仍在响应，可能还有别的实例在跑（检查计划任务或手动启动的窗口）", "warn")
        elif stopped:
            self.log("服务已停止")
        self.refresh_status()

    def cmd_test(self) -> None:
        scheme = "https" if self.var_tls_on.get() else "http"
        ok, detail = self.health(timeout=2.5)
        if ok:
            self.log(f"本机 127.0.0.1 连通，/api/health 返回：{detail.strip()[:200]}", "ok")
        else:
            self.log(f"本机连不上：{detail}", "err")
            return
        # 再按对外地址测一次（验证非回环网卡是否可达）
        ip = local_ipv4()
        if self.var_host.get().strip() == "0.0.0.0" and ip != "127.0.0.1":
            ok2, d2 = http_health(int(self.var_port.get()), host=ip, timeout=2.5,
                                  scheme=scheme)
            if ok2:
                self.log(f"内网地址 {ip}:{self.var_port.get()} 也可达", "ok")
            else:
                self.log(f"内网地址 {ip}:{self.var_port.get()} 不可达（可能被防火墙拦了）：{d2}", "warn")

    # ---------------- 计划任务 ----------------

    def _schtasks(self, args: list) -> subprocess.CompletedProcess:
        return subprocess.run(
            ["schtasks"] + args,
            capture_output=True,
            text=True,
            encoding="mbcs",
            errors="replace",
            creationflags=0x08000000,
        )

    def cmd_install_autostart(self) -> None:
        if not self.save_config():
            return
        if not is_admin():
            messagebox.showwarning(
                "需要管理员权限",
                "安装开机自启会注册系统计划任务，请关掉本窗口，\n"
                "右键 exe → 以管理员身份运行后再试。",
                parent=self.root,
            )
            return
        cmd = self.service_command()
        if is_frozen():
            tr = f'"{cmd[0]}" --run --config "{CONFIG_PATH}"'
        else:
            tr = f'"{cmd[0]}" "{cmd[1]}" --run --config "{CONFIG_PATH}"'
        r = self._schtasks(
            [
                "/Create",
                "/TN",
                TASK_NAME,
                "/TR",
                tr,
                "/SC",
                "ONSTART",
                "/RU",
                "SYSTEM",
                "/RL",
                "HIGHEST",
                "/F",
            ]
        )
        if r.returncode == 0:
            self.log(f"开机自启已安装（计划任务 {TASK_NAME}）", "ok")
            messagebox.showinfo("完成", f"已安装开机自启计划任务：{TASK_NAME}", parent=self.root)
        else:
            self.log(f"安装失败（{r.returncode}）：{(r.stdout or r.stderr).strip()[:300]}", "err")
            messagebox.showerror(
                "安装失败",
                f"schtasks 返回 {r.returncode}\n{(r.stdout or r.stderr).strip()[:400]}",
                parent=self.root,
            )

    def cmd_uninstall_autostart(self) -> None:
        if not is_admin():
            messagebox.showwarning(
                "需要管理员权限", "请以管理员身份重新运行本程序。", parent=self.root
            )
            return
        r = self._schtasks(["/Delete", "/TN", TASK_NAME, "/F"])
        if r.returncode == 0:
            self.log(f"已卸载开机自启（计划任务 {TASK_NAME}）", "ok")
        else:
            self.log(f"卸载失败或本就没有：{(r.stdout or r.stderr).strip()[:200]}", "warn")

    def cmd_query_autostart(self) -> None:
        r = self._schtasks(["/Query", "/TN", TASK_NAME])
        out = (r.stdout or r.stderr or "").strip()
        if r.returncode == 0:
            self.log("开机自启：已安装")
            for ln in out.splitlines()[:8]:
                if ln.strip():
                    self.log("   " + ln.strip())
        else:
            self.log("开机自启：未安装")

    # ---------------- 防火墙 ----------------

    def cmd_fw_add(self) -> None:
        if not is_admin():
            messagebox.showwarning(
                "需要管理员权限",
                "操作防火墙规则需要管理员权限，\n请以管理员身份重新运行本程序。",
                parent=self.root,
            )
            return
        try:
            port = int(str(self.var_port.get()).strip())
        except ValueError:
            messagebox.showerror("端口无效", "端口必须是数字", parent=self.root)
            return
        self._netsh(
            [
                "advfirewall",
                "firewall",
                "delete",
                "rule",
                f"name={FW_RULE}",
            ],
            quiet=True,
        )
        r = self._netsh(
            [
                "advfirewall",
                "firewall",
                "add",
                "rule",
                f"name={FW_RULE}",
                "dir=in",
                "action=allow",
                "protocol=TCP",
                f"localport={port}",
                "profile=any",
            ]
        )
        if r.returncode == 0:
            self.log(f"防火墙已放行入站 TCP {port}", "ok")
        else:
            self.log(f"放行失败：{(r.stdout or r.stderr).strip()[:200]}", "err")

    def cmd_fw_del(self) -> None:
        if not is_admin():
            messagebox.showwarning(
                "需要管理员权限", "请以管理员身份重新运行本程序。", parent=self.root
            )
            return
        r = self._netsh(["advfirewall", "firewall", "delete", "rule", f"name={FW_RULE}"])
        if r.returncode == 0:
            self.log("已删除防火墙放行规则", "ok")
        else:
            self.log(f"删除失败或本就没有该规则：{(r.stdout or r.stderr).strip()[:200]}", "warn")

    def _netsh(self, args: list, quiet: bool = False) -> subprocess.CompletedProcess:
        return subprocess.run(
            ["netsh"] + args,
            capture_output=True,
            text=True,
            encoding="mbcs",
            errors="replace",
            creationflags=0x08000000,
        )

    # ---------------- 杂项 ----------------

    def _clip(self, text: str) -> None:
        self.root.clipboard_clear()
        self.root.clipboard_append(text or "")
        self.root.update()

    def _base_url(self, *, loopback: bool = False) -> str:
        try:
            port = int(str(self.var_port.get()).strip())
        except ValueError:
            port = 8788
        scheme = "https" if self.var_tls_on.get() else "http"
        if loopback:
            # 本机自测永远走 127.0.0.1，和对外地址无关
            return f"{scheme}://127.0.0.1:{port}"
        base = (self.var_puburl.get() or "").strip()
        if not base:
            # 没填就自己挑一个：优先探测到的公网 IP，其次内网 IP
            base = f"{scheme}://{self._pubip or local_ipv4()}:{port}"
        if "://" not in base:
            base = scheme + "://" + base
        return base.rstrip("/")

    def cmd_copy_mcp(self) -> None:
        token = self.var_token.get().strip()
        if not token:
            self.log("还没有 Token，先生成一个再复制配置。", "warn")
            return
        url = self._base_url() + "/mcp"
        cfg = {
            "mcpServers": {
                "wkfile": {
                    "type": "http",
                    "url": url,
                    "headers": {"Authorization": f"Bearer {token}"},
                }
            }
        }
        self._clip(json.dumps(cfg, ensure_ascii=False, indent=2))
        self.log("MCP 配置已复制到剪贴板", "ok")
        self.log(f"   url = {url}")
        host = self._url_host(url)
        if host and wa.is_private_ip(host):
            if self._pubip:
                self.log(f"   注意：地址里是内网 IP（{host}）。若客户端不在同一局域网，"
                         f"应改成公网 IP —— 点左边「检测公网 IP」会自动填。", "warn")
            else:
                self.log(f"   注意：地址里是内网 IP（{host}），只有同一局域网内的机器能访问。"
                         f"跨网络请改填公网 IP 或组网地址。", "warn")
        self.log('   若客户端只认 SSE，把 type 改成 "sse"、url 末尾的 /mcp 换成 /sse。')

    def cmd_open_data(self) -> None:
        try:
            os.startfile(DATA_DIR)
        except Exception as exc:
            self.log(f"打开目录失败：{exc}", "err")

    def cmd_open_log(self) -> None:
        if not os.path.isfile(RUN_LOG):
            self.log("还没有运行日志文件（服务启动后才会生成）", "warn")
            return
        try:
            os.startfile(RUN_LOG)
        except Exception as exc:
            self.log(f"打开日志失败：{exc}", "err")

    def cmd_open_audit(self) -> None:
        path = self._audit_path()
        if not os.path.isfile(path):
            self.log("还没有审计日志（有请求之后才会生成）", "warn")
            return
        try:
            os.startfile(path)
        except Exception as exc:
            self.log(f"打开审计日志失败：{exc}", "err")

    def _audit_path(self) -> str:
        path = self.cfg.audit_log or os.path.join(LOG_DIR, wa.AUDIT_FILENAME)
        if not os.path.isabs(path):
            path = os.path.join(DATA_DIR, path)
        return path

    def _refresh_audit_size(self) -> None:
        try:
            path = self._audit_path()
            total = 0
            if os.path.isfile(path):
                total += os.path.getsize(path)
            for idx in range(1, 10):
                p = f"{path}.{idx}"
                if os.path.isfile(p):
                    total += os.path.getsize(p)
            self.lbl_audit_size.configure(
                text=f"当前审计日志合计 {total / 1024.0:.1f} KB（含历史滚动文件）"
            )
        except Exception:  # noqa: BLE001
            pass

    def cmd_clear_audit(self) -> None:
        path = self._audit_path()
        existing = [path] + [f"{path}.{i}" for i in range(1, 10)]
        existing = [p for p in existing if os.path.isfile(p)]
        if not existing:
            self.log("没有审计日志文件可清空", "warn")
            return
        if not messagebox.askyesno(
            "确认清空",
            "将删除以下审计日志文件（含历史滚动文件）：\n\n"
            + "\n".join(existing)
            + "\n\n此操作不可撤销，确定继续吗？",
            parent=self.root,
        ):
            self.log("已取消清空审计日志", "warn")
            return
        done, failed = 0, []
        for p in existing:
            try:
                os.remove(p)
                done += 1
            except OSError as exc:
                failed.append(f"{p}: {exc}")
        if done:
            self.log(f"已删除 {done} 个审计日志文件", "ok")
        for f in failed:
            self.log("删除失败 " + f, "err")
        self._refresh_audit_size()

    def first_run_hint(self) -> None:
        if not os.path.isfile(CONFIG_PATH):
            self.log(
                "首次使用：①「基础设置」添加允许操作的目录 → ②「安全」核对 Token 和来源 IP → "
                "③「安装与运行」点「保存配置」→「启动服务」"
            )


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------


def _run_service(argv: list) -> int:
    cfg_path = CONFIG_PATH
    if "--config" in argv:
        i = argv.index("--config")
        if i + 1 < len(argv):
            cfg_path = argv[i + 1]
    # 先定位配置，再接管 stdout —— 这样日志轮转参数能按真实配置来
    ensure_stdio(config_path=cfg_path)
    try:
        os.makedirs(os.path.dirname(os.path.abspath(cfg_path)) or ".", exist_ok=True)
    except OSError:
        pass
    try:
        with open(PID_PATH, "w", encoding="utf-8") as fh:
            fh.write(str(os.getpid()))
    except OSError:
        pass
    print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] wkfile --run start, pid={os.getpid()}")
    try:
        return wa.main(["--config", cfg_path])
    finally:
        try:
            if os.path.isfile(PID_PATH):
                with open(PID_PATH, "r", encoding="utf-8") as fh:
                    if fh.read().strip() == str(os.getpid()):
                        os.remove(PID_PATH)
        except OSError:
            pass
        print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] wkfile --run exit")


def _run_gui() -> int:
    enable_dpi_awareness()
    root = tk.Tk()
    Wizard(root)
    root.mainloop()
    return 0


def _check_gui() -> int:
    """无头自检：建窗口 -> 校验控件 -> 立即退出。
    windowed exe 看不到 stdout，所以结果同时落到 gui-selftest.log。"""
    ensure_stdio()
    report_path = os.path.join(DATA_DIR, "gui-selftest.log")
    lines: list[str] = []

    def note(s: str) -> None:
        lines.append(s)
        try:
            print(s)
        except Exception:
            pass

    note(f"=== wkfile 图形界面自检  v{VERSION} ===")
    note(f"frozen   : {is_frozen()}")
    note(f"exe/脚本 : {app_dir()}")
    note(f"数据目录 : {DATA_DIR}")
    note(f"管理员   : {is_admin()}")

    problems = []
    try:
        enable_dpi_awareness()
        root = tk.Tk()
        app = Wizard(root)
        root.update_idletasks()
        note("窗口与控件创建：OK")
        note(f"窗口尺寸       : {root.winfo_width()}x{root.winfo_height()} "
             f"(屏幕 {root.winfo_screenwidth()}x{root.winfo_screenheight()})")
        if not app.lst_roots.winfo_exists():
            problems.append("目录列表控件缺失")
        if not app.var_token.get():
            problems.append("Token 未初始化")
        if not app.var_port.get():
            problems.append("端口未初始化")
        if not app.lbl_hint.winfo_exists():
            problems.append("状态提示条缺失（按钮反馈会看不见）")
        note(f"工具开关数量   : {len(app.var_tools)}")
        if not app.var_tools:
            problems.append("工具级开关未生成（MCP 模块不可用？）")
        note(f"写前语法校验   : {app.var_preflight.get()}")
        try:
            catalog = app._tool_catalog()
            note(f"工具目录       : {len(catalog)} 个（只读 "
                 f"{sum(1 for _n, _t, ro in catalog if ro)} 个）")
        except Exception as exc:
            problems.append(f"工具目录读取异常: {exc}")
        try:
            note(f"审计轮转大小   : {app.var_audit_mb.get()} MB")
            note(f"运行日志轮转   : {app.var_runlog_mb.get()} MB")
            app._refresh_audit_size()
        except Exception as exc:
            problems.append(f"审计体积刷新异常: {exc}")
        try:
            if not app.ent_ro_token.winfo_exists():
                problems.append("只读 Token 输入框缺失")
            note(f"只读 Token     : {'已配置' if app.var_ro_token.get() else '(未启用)'}")
        except Exception as exc:
            problems.append(f"只读 Token 控件异常: {exc}")
        try:
            if not app.ent_cert.winfo_exists():
                problems.append("TLS 证书输入框缺失")
            if not app.ent_chain.winfo_exists():
                problems.append("TLS 中间证书链输入框缺失")
            if not app.btn_clear_chain.winfo_exists():
                problems.append("「清空中间链」按钮缺失")
            if not app.btn_selfcert.winfo_exists():
                problems.append("「生成自签证书」按钮缺失")
            note(f"TLS 证书       : {app.var_tls_cert.get() or '(未启用)'}")
            note(f"TLS 中间链     : {app.var_tls_chain.get() or '(未设，按 fullchain 处理)'}")
        except Exception as exc:
            problems.append(f"TLS 控件异常: {exc}")
        try:
            if not app.btn_pubip.winfo_exists():
                problems.append("「检测公网 IP」按钮缺失")
            note(f"服务器地址     : {app.var_puburl.get() or '(空)'}")
            note(f"本机内网 IP    : {local_ipv4()}")
        except Exception as exc:
            problems.append(f"公网 IP 控件异常: {exc}")
        try:
            # 地址改写逻辑（不联网，纯字符串 + 一次假的公网 IP 回填）
            if app._url_host("http://1.2.3.4:8788/mcp") != "1.2.3.4":
                problems.append("服务器地址取 host 异常")
            if app._replace_url_host("http://192.168.1.5:8788/mcp", "1.2.3.4") \
                    != "http://1.2.3.4:8788/mcp":
                problems.append("服务器地址替换 host 异常")
            app._set_puburl(f"http://{local_ipv4()}:8788")
            app._puburl_touched = False
            app._apply_pubip("203.0.113.7", True)
            filled = app.var_puburl.get()
            if "203.0.113.7" not in filled or "192.168" in filled:
                problems.append(f"公网 IP 自动回填未生效（{filled}）")
            # 手改过之后就不该再被自动覆盖
            app._puburl_touched = True
            app._apply_pubip("198.51.100.9", True)
            if "203.0.113.7" not in app.var_puburl.get():
                problems.append("手改过的地址被自动探测覆盖了")
            app._set_puburl(f"http://{local_ipv4()}:8788")
            app._puburl_touched = False
            note("地址改写/公网回填: OK")
        except Exception as exc:
            problems.append(f"服务器地址逻辑异常: {exc}")
        try:
            # ---- 页 4：数据库 ----
            if not app.db_tree.winfo_exists():
                problems.append("数据库页的数据源列表缺失")
            if len(app.db_vars or {}) < 10:
                problems.append(f"数据库页字段变量缺失（只有 {len(app.db_vars or {})} 个）")
            # 页④必须包了可滚动容器（Canvas + Scrollbar）——
            # 1.6.3 之前是裸 Frame，内容比一屏高时下半截永远看不到也滚不动
            page4 = app.nb.nametowidget(app.nb.tabs()[3])
            p4_classes = [c.winfo_class()
                          for c in page4.winfo_children()]
            if "Canvas" not in p4_classes or "TScrollbar" not in p4_classes:
                problems.append(f"数据库页不是可滚动容器（{p4_classes}）")
            # 添加一条 MySQL，填好后应能通过 collect 的校验
            app._db_add("mysql")
            if len(app.db_items) != 1:
                problems.append("数据库：添加数据源未生效")
            app.db_vars["name"].set("selftest")
            app.db_vars["host"].set("127.0.0.1")
            app.db_vars["port"].set("3306")
            app.db_vars["user"].set("root")
            app.db_vars["password"].set("pw")
            app.db_vars["database"].set("demo")
            app.db_vars["max_rows"].set("123")
            app.db_vars["timeout_sec"].set("12")
            app.db_vars["connect_timeout_sec"].set("5")
            app.var_db_write.set(True)
            got = app._collect_databases()
            if not (len(got) == 1 and got[0]["name"] == "selftest"
                    and got[0]["type"] == "mysql" and got[0]["port"] == 3306
                    and got[0]["max_rows"] == 123 and got[0]["allow_write"] is True
                    and got[0]["password"] == "pw" and got[0]["timeout_sec"] == 12.0
                    and got[0]["connect_timeout_sec"] == 5.0
                    and got[0]["database"] == "demo" and got[0]["user"] == "root"):
                problems.append(f"数据库：collect 结果不符 {got}")
            # 重新回填表单，值必须对得上（含连接超时）
            app._db_fill_form(app.db_items[0])
            if app.db_vars["host"].get() != "127.0.0.1" or app.db_vars["port"].get() != "3306":
                problems.append("数据库：表单回填异常")
            if app.db_vars["connect_timeout_sec"].get() != str(5.0):
                problems.append(
                    f"数据库：连接超时回填异常（{app.db_vars['connect_timeout_sec'].get()!r}）")
            # Redis 的「库号」要落到 db 字段，且不能带 MySQL 的 user/database
            app._db_add("redis")
            app.db_vars["name"].set("cachetest")
            app.db_vars["database"].set("3")
            app.db_vars["password"].set("rp")
            got2 = app._collect_databases()
            redis_entry = [x for x in got2 if x["name"] == "cachetest"][0]
            if (redis_entry["type"] != "redis" or redis_entry["db"] != 3
                    or "user" in redis_entry or "database" in redis_entry):
                problems.append(f"数据库：Redis 结构不符 {redis_entry}")
            # SQL Server：默认端口 1433 / 默认用户 sa，结构同 MySQL 分支
            app._db_add("sqlserver")
            if not any(i.get("type") == "sqlserver" for i in app.db_items):
                problems.append("数据库：添加 SQL Server 未生效")
            app.db_vars["name"].set("erptest")
            app.db_vars["host"].set("127.0.0.1")
            app.db_vars["port"].set("1433")
            app.db_vars["user"].set("sa")
            app.db_vars["database"].set("erp")
            app.db_vars["password"].set("sp")
            got3 = app._collect_databases()
            sqlsrv_entry = [x for x in got3 if x["name"] == "erptest"][0]
            if (sqlsrv_entry["type"] != "sqlserver" or sqlsrv_entry["port"] != 1433
                    or sqlsrv_entry["user"] != "sa" or sqlsrv_entry["database"] != "erp"
                    or sqlsrv_entry["password"] != "sp"):
                problems.append(f"数据库：SQL Server 结构不符 {sqlsrv_entry}")
            # 回填：默认值与类型标签必须对
            app._db_fill_form([i for i in app.db_items
                               if i.get("name") == "erptest"][0])
            if (app.db_vars["port"].get() != "1433"
                    or app.db_vars["user"].get() != "sa"
                    or app.lbl_db_kind.cget("text") != "SQL Server"):
                problems.append("数据库：SQL Server 表单回填/类型标签异常")
            # 重名必须被拦下
            app.db_vars["name"].set("selftest")
            try:
                app._collect_databases()
                problems.append("数据库：重名数据源没被拦下")
            except ValueError:
                pass
            # 非法端口必须被拦下
            app.db_vars["name"].set("cachetest")
            app.db_vars["port"].set("99999")
            try:
                app._collect_databases()
                problems.append("数据库：非法端口没被拦下")
            except ValueError:
                pass
            # 连接超时 <= 0 也必须被拦下（负数会让 socket.settimeout 直接抛
            # ValueError，那已经越过了 DBError 的边界，不能让它写进配置）
            app.db_vars["port"].set("3306")
            app.db_vars["connect_timeout_sec"].set("-1")
            try:
                app._collect_databases()
                problems.append("数据库：负的连接超时没被拦下")
            except ValueError:
                pass
            # 收尾：清空，别把自检数据留进用户的配置
            while app.db_items:
                rows = app.db_tree.get_children()
                if not rows:
                    break
                app.db_tree.selection_set(rows[0])
                app._db_remove()
            if app.db_items:
                problems.append("数据库：删除数据源未清空")
            note("数据库页(④)     : OK（增删改/回填/重名、端口与连接超时校验/"
                 "collect 含 MySQL+Redis+SQL Server 全通过）")
        except Exception as exc:
            problems.append(f"数据库页异常: {exc}")
        try:
            # ---- 页 5：关于 ----
            note(f"作者署名       : {AUTHOR} / 微信号 {AUTHOR_WECHAT}")
            if not AUTHOR:
                problems.append("作者署名为空（wkfile_agent.AUTHOR）")
            if not AUTHOR_WECHAT:
                problems.append("作者微信号为空（wkfile_agent.AUTHOR_WECHAT）")
            if app.var_wechat.get() != AUTHOR_WECHAT:
                problems.append(
                    f"关于页微信号回填异常（{app.var_wechat.get()!r}）")
            app._copy_wechat()          # 复制到剪贴板，顺带验证不抛异常
            if app.root.clipboard_get() != AUTHOR_WECHAT:
                problems.append("微信号复制到剪贴板失败")
            tabs = [app.nb.tab(i, "text").strip() for i in app.nb.tabs()]
            if not any("关于" in t for t in tabs):
                problems.append(f"关于页签缺失（现有：{tabs}）")
            if len(tabs) != 5:
                problems.append(f"页签数量不是 5（{tabs}）")
            note(f"页签           : {' | '.join(tabs)}")
            note("关于页(⑤)      : OK（署名/微信号回填/复制/页签计数）")
        except Exception as exc:
            problems.append(f"关于页异常: {exc}")
        try:
            # 配置文件不存在时应自动落一份**带注释**的模板（数据库那段要能照着抄）
            probe = os.path.join(tempfile.gettempdir(), "wk-selfcfg-probe.json")
            if os.path.isfile(probe):
                os.remove(probe)
            wa.load_config(probe)
            if not os.path.isfile(probe):
                problems.append("load_config 没自动落盘模板配置文件")
            else:
                with open(probe, "r", encoding="utf-8") as fh:
                    body = fh.read()
                if "connect_timeout_sec" not in body:
                    problems.append("自动生成的配置里没提到 connect_timeout_sec")
                if '"databases"' not in body:
                    problems.append("自动生成的配置里没有 databases 段")
                if '"allow_write"' not in body:
                    problems.append("自动生成的配置里没有 allow_write 示例")
                if "//" not in body:
                    problems.append("自动生成的配置没有注释（用户看不懂怎么填）")
                # 带注释的文件必须能再读回来（注释剥离 + json 解析）
                again = wa.load_config(probe)
                if not isinstance(again.databases, list) or again.author != AUTHOR:
                    problems.append(
                        f"带注释的配置回读异常（databases={again.databases!r} "
                        f"author={again.author!r}）")
                note(f"配置自动落盘   : OK（{len(body)} 字节，含 databases 示例与注释）")
                os.remove(probe)
        except Exception as exc:
            problems.append(f"配置自动落盘异常: {exc}")
        try:
            app.set_hint("自检写入状态条", "ok")
            if app.var_hint.get() != "自检写入状态条":
                problems.append("状态条无法写入")
            note("状态条(操作反馈): OK")
        except Exception as exc:
            problems.append(f"状态条异常: {exc}")
        try:
            form = app.collect()
            note(f"collect()      : port={form['port']} host={form['host']} "
                 f"roots={len(form['roots'])} ips={len(form['ip_allowlist'])} "
                 f"token_len={len(form['token'])} disabled_tools={len(form['tools_disable'])} "
                 f"audit_max_bytes={form['audit_max_bytes']} "
                 f"run_log_max_bytes={form['run_log_max_bytes']} "
                 f"ro_token={'yes' if form['readonly_token'] else 'no'} "
                 f"tls={'on' if form.get('tls_cert') else 'off'} "
                 f"tls_chain={'yes' if form.get('tls_chain') else 'no'} "
                 f"databases={len(form.get('databases') or [])}")
        except Exception as exc:
            problems.append(f"collect() 异常: {exc}")
        try:
            cmd = app.service_command()
            note(f"服务启动命令   : {' '.join(cmd)}")
        except Exception as exc:
            problems.append(f"service_command() 异常: {exc}")
        try:
            p = int(app.var_port.get())
            note(f"port_in_use({p})  : {port_in_use(p)}")
            note(f"port_owner({p})   : {port_owner(p)}")
        except Exception as exc:
            problems.append(f"端口检查异常: {exc}")
        roots = list(app.lst_roots.get(0, "end"))
        note(f"配置中的目录   : {roots or '(空)'}")
        root.destroy()
    except Exception as exc:
        problems.append(f"窗口创建失败: {exc}")
        note(f"[X] 窗口创建失败: {exc}")

    if problems:
        note("[X] GUI 自检发现问题：")
        for p in problems:
            note("   - " + p)
        rc = 1
    else:
        note("[OK] GUI 自检通过：窗口、控件、配置读写均正常")
        rc = 0

    try:
        with open(report_path, "w", encoding="utf-8") as fh:
            fh.write("\n".join(lines) + "\n")
    except OSError:
        pass
    return rc


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if "--run" in argv:
        return _run_service(argv)
    if "--check-gui" in argv:
        rc = _check_gui()
        # 这里必须显式结束进程：windowed 编出来的 exe 在 root.destroy() 之后
        # 仍会因 Tcl 解释器未完全释放而挂着不退（源码版没这个问题）。
        # 调用方（批处理 / CI）会一直等下去，所以自检结果落盘后直接终结。
        try:
            sys.stdout.flush()
        except Exception:
            pass
        os._exit(rc)
    note = _agent_cli_notice(argv)
    if note:
        return note
    return _run_gui()


# wkfile_agent 的一次性子命令。这些命令要往控制台打印结果，而 exe 是
# --windowed 编译的（没有控制台、sys.stdout 是 None）：直接透传的话，
# 用户会看到「什么都没发生」，比报错更难查。所以这里挡下来讲清楚。
AGENT_CLI_FLAGS = ("--init", "--gen-token", "--gen-readonly-token",
                   "--gen-selfcert", "--renew-token")


def _agent_cli_notice(argv: list):
    hit = [a for a in argv if a in AGENT_CLI_FLAGS]
    if not hit:
        return None
    msg = (
        f"「{hit[0]}」是命令行（源码版）的用法，在图形版 exe 里没有输出窗口，\n"
        "所以没有执行 —— 避免你以为它做了什么，其实什么都没发生。\n\n"
        "对应做法：\n"
        "  · 生成自签证书 → 界面页③「HTTPS」栏点【生成自签证书】\n"
        "  · 生成 Token → 界面页②点【生成】\n"
        "  · Token 续期 → 界面页②改有效期后点【按此重新计时】\n"
        "  · 纯命令行（适合没有桌面的服务器）→ 用源码版：\n"
        "        python wkfile_agent.py " + hit[0]
    )
    try:
        messagebox.showinfo(f"{APP_TITLE} · 命令行参数", msg)
    except Exception:
        try:
            print(msg)
        except Exception:
            pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
