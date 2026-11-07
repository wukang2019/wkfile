# -*- coding: utf-8 -*-
"""v1.7.26 契约回归：安全开关 fail-open 必修 + 配置告警进启动横幅。

第五轮外部体检（v1.7.25 已确认「脏数值配置三种写法实测能起、不 500」）
指出上一版把「脏值回退默认值」一刀切用在了安全开关上：默认值对安全
开关来说等于「关掉保护」——

    "read_only": "true"              -> False（只读被静默关闭）
    "ip_allowlist": "10.0.0.0/8"    -> []   （放行所有 IP）
    "allow_delete": "false"          -> True （删除被打开）
    "backup_required": "true"        -> False（强制备份失效）

而且启动横幅一声不吭，告警只躺在 health / probe 里。起不来能立刻发现，
静默放开不会发现。本版口径：

  ① 安全开关（布尔 / 安全数组）：严格识别 true/false/1/0，认不出来就
     **拒绝启动**；绝不回退默认值。
  ② 普通数值（大小 / 天数 / 端口）：回退默认 + 告警，方向不变。
  ③ 所有 config_warnings 必须打到启动横幅（含 port 回退的实际端口提示）。

另修一处同源脚枪：GUI 的 collect() 与 CLI 的 --token-days 都写 float，
v1.7.25 按 int 校验 token_ttl_days 会把 30.0 判脏回退 0（Token 变永久）。

本文件先于「修复完成」写成，修复前应当成片报红；断言刻意避开恒真条件。
"""
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import types
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "server"))
import wkfile_agent as wa  # noqa: E402

PASS, FAIL = [], []
# 用 getattr 兜住：拿 v1.7.25 源码跑同一份测试时要能干净报红，而不是崩在导入上
SAFETY_ERR = getattr(wa, "ConfigSafetyError", ValueError)
BOOL_KEYS = getattr(wa, "SAFETY_BOOL_KEYS", ())
LIST_KEYS = getattr(wa, "SAFETY_LIST_KEYS", ())


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print("  %s %s%s" % ("[OK]  " if cond else "[FAIL]", name,
                         "  :: %s" % detail if detail and not cond else ""))


def expect_raise(name, fn, exc_type, *needles):
    try:
        fn()
    except exc_type as exc:
        text = str(exc)
        ok = all(n in text for n in needles)
        check(name, ok, "缺关键词 %r / 实际 %r" % (needles, text[:220]))
        return text
    except Exception as exc:  # noqa: BLE001
        check(name, False, "抛了别的异常 %s: %s" % (type(exc).__name__, exc))
        return ""
    check(name, False, "根本没抛异常（fail-open 还在）")
    return ""


def make_cfg(base, **over):
    share = os.path.join(base, "share")
    os.makedirs(share, exist_ok=True)
    data = {"roots": [share], "token": "abcdefghijklmnopqrstuvwxyz0123456789",
            "auto_backup": True, "public_health": True}
    data.update(over)
    return wa.Config(data, os.path.join(base, "c.json"), env={}), share


def read_doc(rel):
    with open(os.path.join(ROOT, rel), "r", encoding="utf-8") as fh:
        return fh.read().replace("\r\n", "\n")


# ---------------------------------------------------------------------------
# 组 1：纯函数（严格解析 / 拒启判定 / 横幅渲染）
# ---------------------------------------------------------------------------
def group1():
    print("\n-- 组 1：安全开关纯函数 --")
    check("SAFETY_BOOL_KEYS 覆盖审计点名的四个开关",
          {"read_only", "allow_delete", "backup_required"} <= set(BOOL_KEYS))
    check("ip_allowlist 被归为安全数组（不是普通数组）",
          "ip_allowlist" in LIST_KEYS)
    check("roots 不在安全数组里（空 roots 是 fail-closed，不是 fail-open）",
          "roots" not in LIST_KEYS)

    coerce = getattr(wa, "coerce_safety_bool", None)
    check("coerce_safety_bool 已外提到 core 并挂到 wa", callable(coerce))
    if callable(coerce):
        for raw, want in ((True, True), (False, False), (1, True), (0, False),
                          ("true", True), ("TRUE", True), (" false ", False),
                          ("1", True), ("0", False)):
            got, normalized = coerce(raw)
            check("严格解析 %r -> %r" % (raw, want), got is want, repr(got))
        for raw in ("yes", "on", "2GB", "", None, 2, -1, 1.0, [], {}):
            got, _ = coerce(raw)
            check("无法识别 %r 一律不猜" % (raw,), got is None, repr(got))

    fn = getattr(wa, "validate_safety_config", None)
    check("validate_safety_config 已外提到 core 并挂到 wa", callable(fn))
    if not callable(fn):
        return
    defaults = dict(wa.DEFAULT_CONFIG)
    dirty = {"read_only": "yes", "ip_allowlist": "10.0.0.0/8",
             "allow_delete": None, "backup_required": 2,
             "tools_disable": "win_exec",
             "backup_max_bytes": "2GB"}
    cleaned, warns, errs = fn(defaults, dirty, {k: "file" for k in dirty})
    ekeys = {e["key"] for e in errs}
    check("五个脏安全键全部记为 fatal",
          ekeys == {"read_only", "ip_allowlist", "allow_delete",
                    "backup_required", "tools_disable"}, str(sorted(ekeys)))
    check("普通数值脏值不进 fatal（仍由另一函数回退）",
          "backup_max_bytes" not in ekeys)
    check("fatal 不偷偷把脏值改回默认值",
          cleaned["read_only"] == "yes" and cleaned["allow_delete"] is None
          and cleaned["ip_allowlist"] == "10.0.0.0/8", str(cleaned.get("read_only")))
    check("fatal 条目结构完整且不回显用户填的值",
          all({"key", "layer", "action", "expected", "got", "message"} <= set(e)
              and e["action"] == "fatal" and "拒绝启动" in e["message"]
              for e in errs)
          and not any("2GB" in e["message"] or "10.0.0.0/8" in e["message"]
                      for e in errs), str(errs[:1])[:260])
    check("干净配置不产生 fatal 也不产生提示",
          fn(defaults, {"read_only": False, "ip_allowlist": ["10.0.0.0/8"],
                        "allow_delete": True, "backup_required": True}, {})[1:] == ([], []))
    strict = {"read_only": "true", "allow_delete": "false",
              "backup_required": "1", "exec_enabled": 0,
              "ip_allowlist": ["127.0.0.1"], "tools_disable": ["win_exec"]}
    c2, w2, e2 = fn(defaults, strict, {k: "file" for k in strict})
    check("严格可识别的写法被规范化成真布尔",
          not e2 and c2["read_only"] is True and c2["allow_delete"] is False
          and c2["backup_required"] is True and c2["exec_enabled"] is False,
          str(c2["read_only"]) + "/" + str(c2["allow_delete"]))
    check("规范化写法的提示是 action=coerced（不是回退）",
          len(w2) == 4 and all(w["action"] == "coerced" for w in w2), str(w2[:1])[:200])
    check("安全数组接受字符串元素、拒绝非字符串元素",
          not fn(defaults, {"ip_allowlist": ["10.0.0.0/8"]}, {})[2]
          and any(e["key"] == "ip_allowlist"
                  for e in fn(defaults, {"ip_allowlist": [123]}, {})[2]))

    fmt = getattr(wa, "format_config_warnings", None)
    check("format_config_warnings 已挂到 wa", callable(fmt))
    if callable(fmt):
        fake = [
            {"key": "port", "layer": "file", "action": "fallback",
             "message": "配置项 port 类型不对，已回退到内置默认值"},
            {"key": "read_only", "layer": "file", "action": "coerced",
             "message": "安全开关 read_only 用的是非标准写法"},
        ]
        lines = fmt(fake, port=8788)
        blob = "\n".join(lines)
        check("横幅行带条数、两个键名与两条 message",
              len(lines) == 4 and "2 项" in blob and "port" in blob
              and "read_only" in blob and "已回退到内置默认值" in blob
              and "非标准写法" in blob, blob[:260])
        check("port 回退额外提示实际监听端口，避免照 10087 连",
              any("8788" in l and "listening" in l for l in lines), blob[:300])
        check("横幅不回显任何配置值", "SENTINEL_9f3a" not in blob and "10087" not in blob)
        check("无告警时横幅不制造噪音", fmt([], port=8788) == [])

    see = getattr(wa, "safety_env_errors", None)
    check("safety_env_errors 已挂到 wa", callable(see))
    if callable(see):
        env_errs = see(["WKFILE_READ_ONLY: 期望 true/false，已忽略",
                        "WKFILE_TOEKN: 未知配置键，已忽略",
                        "WKFILE_BACKUP_MAX_BYTES: 期望整数，已忽略"])
        check("env 层只把安全开关脏值升级为 fatal（不误伤普通键/拼错键）",
              len(env_errs) == 1 and env_errs[0]["key"] == "read_only"
              and env_errs[0]["layer"] == "env"
              and "maybe" not in json.dumps(env_errs, ensure_ascii=False),
              str(env_errs)[:220])


# ---------------------------------------------------------------------------
# 组 2：Config / load_config 加载层（四个审计用例逐一验）
# ---------------------------------------------------------------------------
def group2(base):
    print("\n-- 组 2：加载层不再静默开门 --")
    fatal_cases = [
        ("read_only 写成 yes", {"read_only": "yes"}, "read_only"),
        ("ip_allowlist 写成单字符串", {"ip_allowlist": "10.0.0.0/8"}, "ip_allowlist"),
        ("allow_delete 写成 null", {"allow_delete": None}, "allow_delete"),
        ("backup_required 写成 2", {"backup_required": 2}, "backup_required"),
        ("tools_disable 写成单字符串", {"tools_disable": "win_exec"}, "tools_disable"),
    ]
    for name, over, key in fatal_cases:
        expect_raise("无法识别的 " + name + " 拒绝启动",
                     lambda o=over: make_cfg(base, **o), SAFETY_ERR,
                     "拒绝启动", key)

    # 审计表格里的四个写法：要么按意图生效，要么拒绝启动，绝不能翻到危险侧
    audit = {"read_only": "true", "ip_allowlist": "10.0.0.0/8",
             "allow_delete": "false", "backup_required": "true",
             "port": "10087"}
    expect_raise("四个危险写法 + 脏 port 一起出现时拒绝启动",
                 lambda: make_cfg(base, **audit), SAFETY_ERR,
                 "拒绝启动", "ip_allowlist")
    text = ""
    try:
        make_cfg(base, **audit)
    except SAFETY_ERR as exc:
        text = str(exc)
    check("拒启消息不把 port 混进安全开关清单（port 仍属普通数值）",
          "port" not in text, text[:200])
    check("拒启消息不回显任何配置值",
          "10.0.0.0/8" not in text and "10087" not in text
          and "SENTINEL_9f3a" not in text, text[:200])
    sentinel = ""
    try:
        make_cfg(base, read_only="SENTINEL_9f3a")
    except SAFETY_ERR as exc:
        sentinel = str(exc)
    check("拒启消息只说键名与类型，不回显用户填的字符串",
          bool(sentinel) and "SENTINEL_9f3a" not in sentinel
          and "read_only" in sentinel, sentinel[:200])

    cases = [
        ({"read_only": "true"}, "read_only", True),
        ({"allow_delete": "false"}, "allow_delete", False),
        ({"backup_required": "true"}, "backup_required", True),
        ({"read_only": "false"}, "read_only", False),
        ({"exec_enabled": 1}, "exec_enabled", True),
    ]
    for over, key, want in cases:
        try:
            cfg, _ = make_cfg(base, **over)
            check("严格可识别写法 %s=%r 按意图生效（不回退默认）"
                  % (key, over[key]), getattr(cfg, key) is want,
                  repr(getattr(cfg, key)))
            check("该写法在 config_warnings 里留痕（action=coerced）",
                  any(w.get("key") == key and w.get("action") == "coerced"
                      for w in getattr(cfg, "config_warnings", [])))
        except Exception as exc:  # noqa: BLE001
            check("严格可识别写法 %s=%r 按意图生效" % (key, over[key]), False,
                  "%s: %s" % (type(exc).__name__, exc))
            check("该写法在 config_warnings 里留痕", False, "Config 没构造出来")

    typed = {"read_only": True, "ip_allowlist": ["10.0.0.0/8"],
             "allow_delete": False, "backup_required": True}
    cfg, _ = make_cfg(base, **typed)
    check("四个键写对类型时原样生效",
          all(getattr(cfg, k) == v for k, v in typed.items()),
          str({k: getattr(cfg, k) for k in typed}))
    check("写对类型时一条告警都没有",
          not getattr(cfg, "config_warnings", []),
          str(getattr(cfg, "config_warnings", []))[:160])

    # port 是连通性关键键：脏值（含 null / 字符串 / 越界）不再静默回退
    # 默认值，而是直接拒绝启动 —— 否则你会照着配置里写的端口连、连不上还
    # 以为服务没起（v1.7.27 把 port 从「普通数值回退」重分类为「拒绝启动」）。
    expect_raise("port 写字符串不再静默回退 8788（拒绝启动）",
                 lambda: make_cfg(base, port="10087"), wa.ConfigSafetyError,
                 "port", "拒绝启动")
    expect_raise("port 写 null 同样拒绝启动",
                 lambda: make_cfg(base, port=None), wa.ConfigSafetyError,
                 "port", "拒绝启动")
    # host 脏值仍属「普通数值」：回退默认 0.0.0.0 + 横幅告警，不拒绝启动
    hcfg, _ = make_cfg(base, host=None)
    check("host 写 null 不拒绝启动、回退 0.0.0.0",
          hcfg.host == "0.0.0.0", str(hcfg.host))
    cwh = getattr(hcfg, "config_warnings", [])
    check("host 脏值产生 action=fallback 告警且点名 host",
          any(w.get("key") == "host" and w.get("action") == "fallback" for w in cwh),
          str(cwh)[:200])
    # 其余普通数值（大小 / 天数）保持回退默认 + 告警
    numcfg, _ = make_cfg(base, backup_max_bytes="2GB",
                         backup_retain_days="30d")
    check("普通数值仍回退默认值（方向不变）",
          numcfg.backup_max_bytes == wa.DEFAULT_CONFIG["backup_max_bytes"]
          and numcfg.backup_retain_days == wa.DEFAULT_CONFIG["backup_retain_days"],
          "%r/%r" % (numcfg.backup_max_bytes, numcfg.backup_retain_days))
    cw = getattr(numcfg, "config_warnings", [])
    check("普通数值回退仍产生 action=fallback 告警",
          {w.get("key") for w in cw} == {"backup_max_bytes", "backup_retain_days"}
          and all(w.get("action") == "fallback" for w in cw), str(cw)[:200])
    check("告警可 json.dumps 且不回显值",
          json.dumps(cw, ensure_ascii=False) and "10087" not in json.dumps(cw))

    # 同源脚枪：GUI / CLI 写出的 token_ttl_days 是浮点
    for raw, want in ((30.0, 30.0), (30, 30), (0.0, 0.0)):
        ttlcfg, _ = make_cfg(base, token_ttl_days=raw)
        check("token_ttl_days=%r 不再被判脏回退 0（Token 不会变永久）" % (raw,),
              ttlcfg.token_ttl_days == want
              and not getattr(ttlcfg, "config_warnings", []),
              "%r / %r" % (ttlcfg.token_ttl_days,
                           getattr(ttlcfg, "config_warnings", [])))

    # 真从磁盘读：load_config 也要拒启，且不能改写文件
    dirty_path = os.path.join(base, "dirty.json")
    with open(dirty_path, "w", encoding="utf-8") as fh:
        json.dump({"roots": [os.path.join(base, "share")],
                   "token": "abcdefghijklmnopqrstuvwxyz0123456789",
                   "read_only": "yes"}, fh)
    with open(dirty_path, "rb") as fh:
        before = fh.read()
    expect_raise("load_config 读磁盘上的脏安全开关同样拒启",
                 lambda: wa.load_config(dirty_path, env={}), SAFETY_ERR,
                 "拒绝启动", "read_only")
    with open(dirty_path, "rb") as fh:
        check("拒启时没有偷偷改写配置文件", fh.read() == before)
    quoted_path = os.path.join(base, "quoted.json")
    with open(quoted_path, "w", encoding="utf-8") as fh:
        json.dump({"read_only": "true", "allow_delete": "false"}, fh)
    quoted = wa.load_config(quoted_path, env={})
    check("磁盘上带引号的布尔按意图严格解析（只读真被打开）",
          quoted.read_only is True and quoted.allow_delete is False
          and len(getattr(quoted, "config_warnings", [])) == 2,
          "%r/%r" % (quoted.read_only, quoted.allow_delete))
    null_path = os.path.join(base, "null.json")
    with open(null_path, "w", encoding="utf-8") as fh:
        json.dump({"allow_delete": None}, fh)
    expect_raise("安全开关写 null 也拒启（merge 层丢弃 null 的漏网）",
                 lambda: wa.load_config(null_path, env={}), SAFETY_ERR,
                 "拒绝启动", "allow_delete")
    # 环境变量层同一口径：安全开关脏值不能只被「忽略并告警」
    expect_raise("WKFILE_READ_ONLY=maybe 拒绝启动（env 层也不是 fail-open）",
                 lambda: wa.Config({}, os.path.join(base, "e.json"),
                                   env={"WKFILE_READ_ONLY": "maybe"}),
                 SAFETY_ERR, "拒绝启动", "read_only")
    envcfg = wa.Config({}, os.path.join(base, "e2.json"),
                       env={"WKFILE_READ_ONLY": "true", "WKFILE_PORT": "9999"})
    check("合法的安全开关 env 覆盖仍生效",
          envcfg.read_only is True and envcfg.port == 9999
          and not getattr(envcfg, "config_warnings", []),
          "%r/%r" % (envcfg.read_only, envcfg.port))
    good_path = os.path.join(base, "good.json")
    with open(good_path, "w", encoding="utf-8") as fh:
        json.dump({"read_only": True, "ip_allowlist": ["10.0.0.0/8"]}, fh)
    good = wa.load_config(good_path, env={})
    check("写对类型的磁盘配置能正常读回", good.read_only is True
          and good.ip_allowlist == ["10.0.0.0/8"])


# ---------------------------------------------------------------------------
# 组 3：health / probe 口径
# ---------------------------------------------------------------------------
def group3(base):
    print("\n-- 组 3：health / probe 与新口径一致 --")
    cfg, _ = make_cfg(base, backup_max_bytes="2GB", read_only="true")
    a = wa.Agent(cfg)
    a.set_request_mode(False)
    h = object.__new__(wa.Handler)
    h.agent = a
    health = h._health_minimal()
    check("health 仍带 count / keys",
          health.get("config_warnings", {}).get("count") == 2
          and {"backup_max_bytes", "read_only"}
              <= set(health["config_warnings"].get("keys") or []),
          str(health)[:200])
    check("health hint 不再笼统写「已回退内置默认值」（有 coerced 项）",
          "已回退内置默认值" not in health["config_warnings"].get("hint", ""),
          health["config_warnings"].get("hint", ""))
    check("health 不泄露配置值",
          not any(x in json.dumps(health, ensure_ascii=False)
                  for x in ("2GB", "abcdefghijklmnopqrstuvwxyz0123456789")))
    probe = a.op_probe({})
    check("probe.issues 主动报配置告警且提到启动横幅",
          any("配置" in s and "类型" in s and "启动横幅" in s
              for s in (probe.get("issues") or [])),
          str(probe.get("issues"))[:220])
    layers = probe["config"]["layers"]["config_warnings"]
    check("layers.config_warnings 带 action 区分两类处理",
          {w.get("action") for w in layers} == {"fallback", "coerced"}, str(layers)[:220])


# ---------------------------------------------------------------------------
# 组 4：GUI 不许用默认值覆盖脏安全配置
# ---------------------------------------------------------------------------
def group4(base):
    print("\n-- 组 4：GUI 阻断静默覆盖 --")
    src = read_doc("server/wkfile_gui.py")
    check("GUI 有专门的 ConfigSafetyError 分支（不当语法错误吞掉）",
          src.count("except wa.ConfigSafetyError") >= 2,
          str(src.count("except wa.ConfigSafetyError")))
    check("GUI 保存前检查 _config_blocked 并直接返回 False",
          "_config_blocked" in src and "已取消" in src)

    # 用最小 tkinter 桩件做一次真调用：脏配置 -> 锁住 -> 保存被拒
    try:
        import tkinter  # noqa: F401
    except ImportError:
        _tk = types.ModuleType("tkinter")
        _tk.TclError = type("TclError", (Exception,), {})
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
    old_path = g.CONFIG_PATH
    dirty_path = os.path.join(base, "gui-dirty.json")
    with open(dirty_path, "w", encoding="utf-8") as fh:
        json.dump({"read_only": "yes"}, fh)
    try:
        g.CONFIG_PATH = dirty_path
        app = object.__new__(g.Wizard)
        app.root = None
        app.log = lambda *a, **k: None
        calls = []

        class Msg:
            def showerror(self, *a, **k):
                calls.append(a[0] if a else "")

            def showwarning(self, *a, **k):
                calls.append(a[0] if a else "")

        app.msg = Msg()
        cfg = app._load_config_safe()
        check("脏安全配置下 GUI 仍能打开（不让窗口起不来）",
              isinstance(cfg, wa.Config))
        check("GUI 记录了阻断原因并弹窗说明服务已拒绝启动",
              bool(getattr(app, "_config_blocked", ""))
              and any("拒绝启动" in c for c in calls), str(calls)[:180])
        blocked = bool(getattr(app, "_config_blocked", ""))
        if blocked:
            check("GUI 锁住后 save_config 直接返回 False（不覆盖成默认值）",
                  app.save_config() is False)
        else:
            check("GUI 锁住后 save_config 直接返回 False（不覆盖成默认值）",
                  False, "旧版没有阻断标记，保存会拿默认值覆盖脏配置")
        with open(dirty_path, "r", encoding="utf-8") as fh:
            check("被锁住时磁盘配置原样未动", json.load(fh).get("read_only") == "yes")
    finally:
        g.CONFIG_PATH = old_path


# ---------------------------------------------------------------------------
# 组 5：真进程 —— 拒启 / 横幅
# ---------------------------------------------------------------------------
def _spawn(cfg_path, *extra):
    return subprocess.Popen(
        [sys.executable, os.path.join(ROOT, "server", "wkfile_agent.py"),
         "--config", cfg_path] + list(extra),
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT)


def _stop(proc, wait_first=0):
    if proc is None:
        return ""
    if proc.poll() is None and wait_first:
        try:
            proc.wait(timeout=wait_first)   # 拒启场景：先让它自己退出并打完横幅
        except subprocess.TimeoutExpired:
            pass
    if proc.poll() is None:
        proc.terminate()
    try:
        out, _ = proc.communicate(timeout=10)
        return out.decode("utf-8", "replace")
    except subprocess.TimeoutExpired:
        proc.kill()
        out, _ = proc.communicate(timeout=10)
        return out.decode("utf-8", "replace")


def group5(base):
    print("\n-- 组 5：真起服务（拒启 / 横幅告警） --")
    # 5a：脏安全开关 -> 进程直接退出、不监听、不改写文件
    port_a = 18936
    d = os.path.join(base, "cli-dirty")
    os.makedirs(d, exist_ok=True)
    share = os.path.join(d, "share")
    os.makedirs(share, exist_ok=True)
    cfg_path = os.path.join(d, "wkfile.config.json")
    body = {"host": "127.0.0.1", "port": port_a,
            "token": "0123456789abcdef0123456789abcdef",
            "roots": [share], "read_only": "true",
            "ip_allowlist": "10.0.0.0/8"}
    with open(cfg_path, "w", encoding="utf-8") as fh:
        json.dump(body, fh)
    raw_before = open(cfg_path, "rb").read()
    p = _spawn(cfg_path)
    out = ""
    try:
        out = _stop(p, wait_first=6)
        check("真 CLI：脏安全开关退出码为 5（不是 0 / 不是挂起）",
              p.returncode == 5, "rc=%s out=%r" % (p.returncode, out[:220]))
        check("真 CLI：横幅明确写拒绝启动并点名键",
              "拒绝启动" in out and "ip_allowlist" in out, out[:300])
        check("真 CLI：没有出现 listening 行（确实没绑端口）",
              "listening" not in out, out[:300])
        check("真 CLI：没有改写配置文件",
              open(cfg_path, "rb").read() == raw_before)
        s = socket.socket()
        s.settimeout(0.4)
        try:
            bound = s.connect_ex(("127.0.0.1", port_a)) == 0
        finally:
            s.close()
        check("真 CLI：端口确实没有在监听", not bound)
    finally:
        _stop(p)

    # 5b：脏普通数值（非连通性键）-> 服务照常起，但横幅逐条打印告警
    port_b = 18937
    n = os.path.join(base, "cli-num")
    os.makedirs(n, exist_ok=True)
    share_b = os.path.join(n, "share")
    os.makedirs(share_b, exist_ok=True)
    cfg_b = os.path.join(n, "wkfile.config.json")
    with open(cfg_b, "w", encoding="utf-8") as fh:
        json.dump({"host": "127.0.0.1", "port": port_b,
                   "token": "0123456789abcdef0123456789abcdef",
                   "roots": [share_b], "public_health": True,
                   "backup_max_bytes": "2GB"}, fh)
    p2 = _spawn(cfg_b, "--port", str(port_b))
    up = False
    health = {}
    try:
        for _ in range(60):
            time.sleep(0.2)
            try:
                with urllib.request.urlopen(
                        "http://127.0.0.1:%d/api/health" % port_b, timeout=1) as r:
                    body = json.loads(r.read().decode("utf-8", "replace"))
                    # /api/health 外层是 {"ok":true,"data":{...}} 信封
                    health = body.get("data", body) if isinstance(body, dict) else {}
                    up = r.status == 200
                    break
            except Exception:  # noqa: BLE001
                if p2.poll() is not None:
                    break
        check("脏普通数值下服务仍能起且 /api/health 200", up)
        check("health 仍报 config_warnings 键名",
              {"backup_max_bytes"}
              <= set(health.get("config_warnings", {}).get("keys") or []),
              str(health)[:220])
    finally:
        out2 = _stop(p2)
    check("真 CLI：启动横幅打印了配置告警与键名",
          "[!] 配置告警" in out2 and "backup_max_bytes" in out2, out2[:400])
    check("真 CLI：横幅不回显用户填的脏值",
          "2GB" not in out2, out2[:400])

    # 5c：port 脏值（字符串）直接拒绝启动，不绑定端口、不改写配置
    n3 = os.path.join(base, "cli-port")
    os.makedirs(n3, exist_ok=True)
    share_c = os.path.join(n3, "share")
    os.makedirs(share_c, exist_ok=True)
    cfg_c = os.path.join(n3, "wkfile.config.json")
    with open(cfg_c, "w", encoding="utf-8") as fh:
        json.dump({"host": "127.0.0.1", "port": "10087",
                   "token": "0123456789abcdef0123456789abcdef",
                   "roots": [share_c], "public_health": True}, fh)
    p3 = _spawn(cfg_c)
    out3 = _stop(p3, wait_first=6)
    check("真 CLI：port 写字符串拒绝启动（rc=5，不绑定端口）",
          p3.returncode == 5, "rc=%s out=%r" % (p3.returncode, out3[:220]))
    check("真 CLI：拒启信息点名 port 且不含令牌/值",
          "port" in out3 and "0123456789abcdef" not in out3, out3[:300])


# ---------------------------------------------------------------------------
# 组 6：文档 / 模板 / 示例配置口径
# ---------------------------------------------------------------------------
def group6():
    print("\n-- 组 6：文档与模板 --")
    rd = read_doc("README.md")
    check("README 有 v1.7.26 段落", "1.7.26" in rd)
    seg = rd[rd.find("## 版本"):] if "## 版本" in rd else rd
    check("README 写明安全开关拒绝启动、普通数值回退告警两类口径",
          "拒绝启动" in rd and "回退" in rd and "启动横幅" in rd)
    for key in ("read_only", "ip_allowlist", "allow_delete", "backup_required"):
        check("README 点名 %s 的手滑写法" % key, key in rd)
    q = read_doc("README-快速上手.txt")
    check("快速上手同步写明安全开关拒绝启动", "拒绝启动" in q and "1.7.26" in q)
    check("快速上手提示布尔不加引号 / 白名单用数组",
          "不加引号" in q or "true/false" in q)

    example = json.loads(read_doc("server/wkfile.config.example.json"))
    for key in ("read_only", "allow_delete", "backup_required"):
        check("示例配置 %s 是布尔不是字符串" % key,
              isinstance(example.get(key), bool), repr(example.get(key)))
    check("示例配置 ip_allowlist 是数组",
          isinstance(example.get("ip_allowlist"), list))
    tpl = wa._annotate_config(
        json.dumps(dict(wa.DEFAULT_CONFIG), ensure_ascii=False, indent=2))
    check("自动生成的模板写明安全开关必须 true/false、错了拒启",
          "true/false" in tpl and "拒绝启动" in tpl, tpl[:200])
    check("自动生成的模板写明告警会进启动横幅", "启动横幅" in tpl)
    check("自动生成的模板写明 port 要按 listening 行连",
          "listening" in tpl and "port" in tpl)
    check("模板里 token_ttl_days 是浮点（GUI 保存不会再被判脏）",
          '"token_ttl_days": 0.0' in tpl,
          [l for l in tpl.splitlines() if "token_ttl_days" in l][:1])


def main():
    base = tempfile.mkdtemp(prefix="wk1726-")
    try:
        group1()
        group2(base)
        group3(base)
        group4(base)
        group5(base)
        group6()
    finally:
        shutil.rmtree(base, ignore_errors=True)
    print("\n" + "=" * 60)
    print("V1726_TEST  通过 %d / 失败 %d" % (len(PASS), len(FAIL)))
    if FAIL:
        for f in FAIL:
            print("  FAIL: " + f)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
