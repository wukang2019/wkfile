# -*- coding: utf-8 -*-
"""v1.5 新增能力的端到端自测

覆盖：
  A 工具 annotations / title
  B 审计日志轮转
  C win_replace 多次匹配护栏
  D 写前语法预校验
  E 工具级禁用（tools_disable）
  F 启动拒绝：非本机监听 + 无效 Token
  G 免鉴权路径收敛（/health 最小信息、/ 需鉴权）
  H 数据目录隔离（配置/日志/备份/回收站不可经接口访问）+ 目录选址
  I Token 有效期（过期后拒绝服务）
  J 运行日志轮转（logs/wkfile.log 不再无限膨胀）
  K 只读 Token / 请求级只读
  L 备份查看/回滚 + tail/tree/multi_read + replace 预览
  M TLS 证书校验
  N 只读 Token 走 HTTP 端到端 + MCP 调用进审计
  O 自签证书生成（纯标准库，IP SAN 可校验）
  P 证书链完整性（缺中间证书 -> Electron 客户端连不上）
  Q 三件套 public + chain + key 的分开填与自动合并
  R 数据库与缓存（MySQL / Redis：协议、SQL 与命令分级、权限三重闸门）
  R7 超时边界（秒数夹取、脏配置不炸、多地址共用连接预算）
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import traceback
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
SERVER = os.path.join(os.path.dirname(HERE), "server")
sys.path.insert(0, SERVER)

import wkfile_agent as wa  # noqa: E402
import wkfile_mcp_endpoint as mcp  # noqa: E402

PASS, FAIL, SKIP = [], [], []
PY = sys.executable
TOKEN = "t" * 40            # 合格 Token
PORT = 18789


def check(name, cond, extra=""):
    (PASS if cond else FAIL).append(name)
    print("  [%s] %s%s" % ("OK" if cond else "XX", name, ("  <- " + extra) if extra and not cond else ""))


def skip(name, why):
    """环境不具备时跳过（不计入失败）—— 比如本机没装 MySQL。"""
    SKIP.append(name)
    print("  [--] %s（跳过：%s）" % (name, why))


def section(t):
    print("\n=== %s ===" % t)


def _raises(fn, exc=Exception):
    try:
        fn()
    except exc:
        return True
    except Exception:  # noqa: BLE001
        return False
    return False


# ---------------------------------------------------------------------------
section("A 工具 annotations / title")

check("工具数量为 33", len(mcp.TOOLS) == 33, str(len(mcp.TOOLS)))
check("每个工具都有 title + 4 项 annotations",
      all(t.get("title") and set(t.get("annotations", {})) ==
          {"readOnlyHint", "destructiveHint", "idempotentHint", "openWorldHint"}
          for t in mcp.TOOLS))
check("只读 21 / 写入 12", len(mcp.READ_ONLY_TOOLS) == 21 and len(mcp.WRITE_TOOLS) == 12,
      "%d/%d" % (len(mcp.READ_ONLY_TOOLS), len(mcp.WRITE_TOOLS)))
ro = {t["name"]: t["annotations"] for t in mcp.TOOLS}

check("win_read 只读且非破坏",
      ro["win_read"]["readOnlyHint"] and not ro["win_read"]["destructiveHint"])
check("win_trash 破坏性",
      ro["win_trash"]["destructiveHint"] and not ro["win_trash"]["readOnlyHint"])
check("win_append 非幂等（防客户端自动重试）", ro["win_append"]["idempotentHint"] is False)
check("win_mkdir 幂等", ro["win_mkdir"]["idempotentHint"] is True)
check("win_copy 幂等但仍破坏性",
      ro["win_copy"]["idempotentHint"] is True and ro["win_copy"]["destructiveHint"] is True)
check("全部 openWorldHint=False",
      all(t["annotations"]["openWorldHint"] is False for t in mcp.TOOLS))


# ---------------------------------------------------------------------------
section("B 审计日志轮转")

tmp = tempfile.mkdtemp(prefix="wf-features-")
root = os.path.join(tmp, "root")
os.makedirs(root, exist_ok=True)
audit_path = os.path.join(tmp, "audit.jsonl")

cfg = wa.Config({
    "host": "127.0.0.1", "port": PORT, "token": TOKEN, "roots": [root],
    "audit_log": audit_path,
    "audit_max_bytes": 2048,       # 故意调小，方便触发
    "audit_backup_count": 2,
}, os.path.join(tmp, "wkfile.config.json"))
cfg.save()

audit = wa.Audit(cfg)
for i in range(400):
    audit.write({"op": "list", "ok": True, "n": i, "pad": "x" * 40})

sizes = {}
for suffix in ("", ".1", ".2", ".3", ".4"):
    p = audit_path + suffix
    sizes[suffix or "(当前)"] = os.path.getsize(p) if os.path.isfile(p) else None
print("   轮转后各文件大小：", sizes)

check("当前文件存在且被限制在 max_bytes 附近",
      sizes["(当前)"] is not None and sizes["(当前)"] <= 2048 + 200, str(sizes["(当前)"]))
check("存在 .1 历史文件", sizes[".1"] is not None)
check("最多只保留 backup_count 份（.3 不应存在）", sizes[".3"] is None)
check("未配置 .4", sizes[".4"] is None)

cfg_off = wa.Config({"roots": [root], "audit_log": audit_path,
                     "audit_max_bytes": 0, "audit_backup_count": 3}, "x.json")
a2 = wa.Audit(cfg_off)
for i in range(200):
    a2.write({"op": "x", "pad": "y" * 100})
check("audit_max_bytes=0 时不轮转（保持旧行为）",
      os.path.getsize(audit_path) > 2048)


# ---------------------------------------------------------------------------
section("C win_replace 多次匹配护栏")

agent = wa.Agent(cfg)
rf = os.path.join(root, "conf.ini")
with open(rf, "w", encoding="utf-8") as fh:
    fh.write("host=localhost\npath=localhost\nother=localhost\n")

before = open(rf, "rb").read()
try:
    agent.dispatch("replace", {"path": rf, "old": "localhost", "new": "db1"})
    check("多次出现且未指定 count → 应被拒绝", False, "居然成功了")
except wa.PathError as exc:
    check("多次出现且未指定 count → 被拒绝", exc.code == "ambiguous_match", exc.code)
check("被拒绝后文件一字未动", open(rf, "rb").read() == before)

r = agent.dispatch("replace", {"path": rf, "old": "localhost", "new": "db1", "count": 1})
txt = open(rf, encoding="utf-8").read()
check("显式 count=1 只改第一处", txt.count("db1") == 1 and txt.count("localhost") == 2,
      txt.replace("\n", " | "))

r = agent.dispatch("replace", {"path": rf, "old": "path=localhost", "new": "path=db2"})
txt = open(rf, encoding="utf-8").read()
check("提供唯一上下文时可正常替换", "path=db2" in txt)

r = agent.dispatch("replace", {"path": rf, "old": "nope-not-here", "new": "x"})
check("无匹配 → 返回 replaced=0 且文件未动",
      r["replaced"] == 0 and "未改动" in r.get("message", ""), str(r))


# ---------------------------------------------------------------------------
section("D 写前语法预校验")

good_py = os.path.join(root, "good.py")
bad_py = os.path.join(root, "bad.py")
with open(bad_py, "w", encoding="utf-8") as fh:
    fh.write("print('original')\n")

try:
    agent.dispatch("write", {"path": bad_py, "content": "def broken(:\n    pass\n"})
    check("非法 .py 被拒绝", False, "居然写进去了")
except wa.PathError as exc:
    check("非法 .py 被拒绝", exc.code == "preflight_failed", exc.code)
check("被拒绝的 .py 原文件未变", open(bad_py, encoding="utf-8").read() == "print('original')\n")

r = agent.dispatch("write", {"path": good_py, "content": "x = 1\nprint(x)\n"})
check("合法 .py 正常写入", os.path.isfile(good_py) and r["bytes"] > 0)

jf = os.path.join(root, "c.json")
try:
    agent.dispatch("write", {"path": jf, "content": '{"a": 1,}'})
    check("非法 .json 被拒绝", False)
except wa.PathError as exc:
    check("非法 .json 被拒绝", exc.code == "preflight_failed", exc.code)
check("非法 .json 未落盘", not os.path.isfile(jf))

agent.dispatch("write", {"path": jf, "content": '{"a": 1}'})
check("合法 .json 正常写入", json.load(open(jf, encoding="utf-8")) == {"a": 1})

jlf = os.path.join(root, "d.jsonl")
try:
    agent.dispatch("write", {"path": jlf, "content": '{"a":1}\nnope\n'})
    check("非法 .jsonl 被拒绝（逐行校验）", False)
except wa.PathError as exc:
    check("非法 .jsonl 被拒绝（逐行校验）", exc.code == "preflight_failed", exc.code)

tf = os.path.join(root, "note.txt")
agent.dispatch("write", {"path": tf, "content": "这是随便写的，不用校验\n"})
check("未列出的后缀（.txt）不做校验", os.path.isfile(tf))

# 关掉开关后不该拦
cfg_np = wa.Config({"roots": [root], "token": TOKEN, "preflight_check": False,
                    "allow_delete": True, "allow_move": True}, "x.json")
agent_np = wa.Agent(cfg_np)
np_file = os.path.join(root, "nopreflight.py")
agent_np.dispatch("write", {"path": np_file, "content": "def broken(:\n"})
check("preflight_check=false 时放行（可写半截文件）", os.path.isfile(np_file))

# 替换路径也要过校验
bad2 = os.path.join(root, "r.py")
with open(bad2, "w", encoding="utf-8") as fh:
    fh.write("value = 1\n")
try:
    agent.dispatch("replace", {"path": bad2, "old": "value = 1", "new": "value = = 2"})
    check("win_replace 也过语法校验", False)
except wa.PathError as exc:
    check("win_replace 也过语法校验", exc.code == "preflight_failed", exc.code)


# ---------------------------------------------------------------------------
section("E 工具级禁用 tools_disable")

cfg_dis = wa.Config({"roots": [root], "token": TOKEN, "tools_disable": ["win_trash", "win_move"],
                     "allow_delete": True, "allow_move": True}, "x.json")
agent_dis = wa.Agent(cfg_dis)

names = [t["name"] for t in mcp.build_tools_list(disabled=agent_dis.disabled_tools)["tools"]]
check("被禁用的工具不出现在 tools/list", "win_trash" not in names and "win_move" not in names,
      str(names))
check("未被禁用的工具仍在列表里", "win_read" in names and "win_write" in names)

res = mcp.call_tool(agent_dis.dispatch, {"name": "win_trash", "arguments": {"path": os.path.join(root, "good.py")}},
                    disabled=agent_dis.disabled_tools)
check("直接调用被禁工具 → isError", res["isError"] is True)
check("错误文案说明是配置禁用", "禁用" in res["content"][0]["text"])

try:
    agent_dis.dispatch("trash", {"path": os.path.join(root, "good.py")})
    check("裸 HTTP 接口同样尊重禁用名单", False)
except wa.PathError as exc:
    check("裸 HTTP 接口同样尊重禁用名单", exc.code == "tool_disabled", exc.code)
check("被禁用的删除操作确实没执行", os.path.isfile(os.path.join(root, "good.py")))

check("工具名支持不带 win_ 前缀的写法", wa.disabled_tools(
    wa.Config({"tools_disable": ["trash", " win_move "]}, "x.json")) ==
    frozenset({"win_trash", "win_move"}))
check("tools_disable 为空时不禁用任何工具",
      wa.disabled_tools(wa.Config({}, "x.json")) == frozenset())


# ---------------------------------------------------------------------------
section("F 启动拒绝：非本机监听 + 无效 Token")

bad_cfg = os.path.join(tmp, "unsafe.config.json")
with open(bad_cfg, "w", encoding="utf-8") as fh:
    json.dump({"host": "0.0.0.0", "port": 18791, "token": "changeme",
               "roots": [root]}, fh, ensure_ascii=False)

p = subprocess.run([PY, os.path.join(SERVER, "wkfile_agent.py"), "--config", bad_cfg],
                   capture_output=True, text=True, encoding="utf-8", errors="replace",
                   timeout=60, cwd=SERVER)
out = (p.stdout or "") + (p.stderr or "")
check("占位 Token + 0.0.0.0 → 退出码 3", p.returncode == 3, "rc=%s out=%s" % (p.returncode, out[-200:]))
check("拒绝理由打印清晰", "拒绝启动" in out)

def run_briefly(extra_args, seconds=8):
    """跑一个会长期驻留的服务进程，短暂运行后收掉，返回 (是否曾存活, 输出)"""
    p = subprocess.Popen([PY, os.path.join(SERVER, "wkfile_agent.py"),
                          "--config", bad_cfg] + extra_args,
                         stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                         text=True, encoding="utf-8", errors="replace", cwd=SERVER)
    try:
        out, _ = p.communicate(timeout=seconds)
        return False, out or ""
    except subprocess.TimeoutExpired:
        p.terminate()
        try:
            out, _ = p.communicate(timeout=6)
        except subprocess.TimeoutExpired:
            p.kill()
            out, _ = p.communicate()
        return True, out or ""


alive, out2 = run_briefly(["--restrict-local"])
check("改成只监听本机后放行（进程能持续运行）", alive, out2[-200:])

alive3, out3 = run_briefly(["--allow-unsafe"])
check("--allow-unsafe 是显式逃生阀（能起来）", alive3 and "拒绝启动" not in out3, out3[-200:])

check("token_strength: 占位值判 bad", wa.token_strength("changeme")[0] == "bad")
check("token_strength: 空判 bad", wa.token_strength("")[0] == "bad")
check("token_strength: 短判 weak", wa.token_strength("abc123")[0] == "weak")
check("token_strength: 长随机判 ok", wa.token_strength(TOKEN)[0] == "ok")
check("is_loopback_bind 识别 127.*/::1/localhost",
      all(wa.is_loopback_bind(x) for x in ("127.0.0.1", "127.5.5.5", "::1", "localhost")))
check("is_loopback_bind 不认 0.0.0.0 / 内网 IP",
      not any(wa.is_loopback_bind(x) for x in ("0.0.0.0", "192.168.0.2", "::")))


# ---------------------------------------------------------------------------
section("G 免鉴权路径收敛（起真实服务验证）")

live_cfg = os.path.join(tmp, "live.config.json")
with open(live_cfg, "w", encoding="utf-8") as fh:
    json.dump({"host": "127.0.0.1", "port": PORT, "token": TOKEN, "roots": [root],
               "tools_disable": ["win_trash"]}, fh, ensure_ascii=False)

logf = os.path.join(tmp, "agent.out.log")
with open(logf, "w", encoding="utf-8") as fh:
    proc = subprocess.Popen([PY, os.path.join(SERVER, "wkfile_agent.py"), "--config", live_cfg],
                            stdout=fh, stderr=subprocess.STDOUT, cwd=SERVER)


def http(path, token=None, timeout=4):
    url = "http://127.0.0.1:%d%s" % (PORT, path)
    req = urllib.request.Request(url)
    if token:
        req.add_header("Authorization", "Bearer " + token)
    op = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with op.open(req, timeout=timeout) as resp:
            return resp.status, resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", "replace")
    except Exception as exc:  # noqa: BLE001
        return None, str(exc)


try:
    ready = False
    for _ in range(30):
        time.sleep(0.4)
        s, _b = http("/health")
        if s == 200:
            ready = True
            break
    check("服务启动成功", ready)

    s, body = http("/health")
    data = (json.loads(body).get("data") or {}) if s == 200 else {}
    check("/health 免鉴权可访问", s == 200, str(s))
    check("/health 不含主机名", "host" not in data, body[:120])
    check("/health 不含 Python 版本", "python" not in data)
    check("/health 仍带 service/version（自检脚本靠它）",
          data.get("service") == "wkfile" and data.get("version") == wa.VERSION)

    s2, b2 = http("/api/health")
    check("/api/health 同样免鉴权", s2 == 200)

    s3, b3 = http("/")
    check("GET / 不带 Token → 401", s3 == 401, str(s3))

    s4, b4 = http("/", TOKEN)
    d4 = (json.loads(b4).get("data") or {}) if s4 == 200 else {}
    check("GET / 带 Token → 200 且给出详细信息", s4 == 200 and "host" in d4, str(s4))
    check("详细状态里能看到禁用工具", d4.get("mcp", {}).get("tools_disabled") == ["win_trash"],
          str(d4.get("mcp")))

    # MCP：禁用名单生效
    op = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    payload = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/list"}).encode()
    req = urllib.request.Request("http://127.0.0.1:%d/mcp" % PORT, data=payload,
                                 headers={"Authorization": "Bearer " + TOKEN,
                                          "Content-Type": "application/json"})
    with op.open(req, timeout=6) as resp:
        tl = json.loads(resp.read().decode())
    tnames = [t["name"] for t in tl["result"]["tools"]]
    check("MCP tools/list 过滤掉了被禁工具", "win_trash" not in tnames, str(len(tnames)))
    check("MCP tools/list 里每个工具都带 annotations",
          all("annotations" in t for t in tl["result"]["tools"]))
finally:
    proc.terminate()
    try:
        proc.wait(timeout=8)
    except subprocess.TimeoutExpired:
        proc.kill()

print("\n--- 服务启动横幅 ---")
try:
    with open(logf, encoding="utf-8", errors="replace") as fh:
        banner = fh.read()
    for line in banner.splitlines()[:6]:
        print("   " + line)
    check("启动横幅含监听地址与鉴权状态",
          "listening" in banner and "[auth ON]" in banner)
    check("启动横幅显示工具数/禁用项", "tools:" in banner and "已禁用" in banner)
    check("启动横幅显示审计轮转配置", "audit:" in banner and "轮转" in banner)
except OSError as exc:
    check("读取启动日志", False, str(exc))


# ---------------------------------------------------------------------------
section("H 数据目录隔离（配置/日志/备份/回收站不可通过接口访问）")
g_tmp = tempfile.mkdtemp(prefix="wk-isolate-")
g_share = os.path.join(g_tmp, "share")
g_data = os.path.join(g_tmp, "wkfile")
os.makedirs(g_share, exist_ok=True)
os.makedirs(os.path.join(g_data, "logs"), exist_ok=True)
g_cfg_path = os.path.join(g_data, "wkfile.config.json")
with open(g_cfg_path, "w", encoding="utf-8") as fh:
    json.dump({"host": "127.0.0.1", "port": 1, "token": "t" * 40,
               # 故意把数据目录的父级也放进白名单，才能验出保护是否真的生效
               "roots": [g_share, g_tmp],
               "audit_log": "logs/audit.jsonl"}, fh)

g_cfg = wa.load_config(g_cfg_path)
g = wa.Guard(g_cfg)
for label, p in (
    ("配置文件", g_cfg_path),
    ("日志目录", os.path.join(g_data, "logs")),
    ("备份目录", os.path.join(g_data, "backups")),
    ("回收站", os.path.join(g_data, "trash")),
):
    try:
        g.resolve(p)
        check("拒绝访问 " + label, False, p)
    except wa.PathError as exc:
        check("拒绝访问 " + label, exc.code == "denied", exc.code)
check("白名单里的业务目录仍可访问",
      bool(g.resolve(os.path.join(g_share, "a.txt"))))

_old_prog, _old_data = wa.program_dir, wa.data_dir
try:
    exe_home = os.path.join(g_tmp, "app")
    os.makedirs(exe_home, exist_ok=True)
    with open(os.path.join(exe_home, "wkfile.exe"), "wb") as fh:
        fh.write(b"")
    wa.program_dir = lambda: exe_home
    pick = wa.data_dir()
    check("数据目录选址 = exe 同级的 wkfile\\",
          os.path.normcase(pick) == os.path.normcase(os.path.join(exe_home, "wkfile")), pick)
finally:
    wa.program_dir, wa.data_dir = _old_prog, _old_data

aud = wa.Audit(g_cfg)
check("审计日志落在数据目录内",
      os.path.normcase(os.path.abspath(aud.path)).startswith(os.path.normcase(g_data)),
      aud.path)
shutil.rmtree(g_tmp, ignore_errors=True)


# ---------------------------------------------------------------------------
section("I Token 有效期（过期后拒绝服务）")
import datetime as _dt  # noqa: E402

ttl_tmp = tempfile.mkdtemp(prefix="wk-ttl-")
ttl_share = os.path.join(ttl_tmp, "share")
os.makedirs(ttl_share, exist_ok=True)


def ttl_probe(expires_at, port):
    """起一个带指定到期时间的服务，返回 (状态码, 错误码, 启动日志)。"""
    cfgp = os.path.join(ttl_tmp, "wkfile.config.json")
    with open(cfgp, "w", encoding="utf-8") as fh:
        json.dump({"host": "127.0.0.1", "port": port, "token": TOKEN,
                   "roots": [ttl_share], "token_ttl_days": 30,
                   "token_expires_at": expires_at, "enable_mcp": True,
                   "audit_log": "logs/audit.jsonl"}, fh)
    lf = os.path.join(ttl_tmp, "srv-%d.log" % port)
    log = open(lf, "w", encoding="utf-8")
    proc = subprocess.Popen([PY, os.path.join(SERVER, "wkfile_agent.py"), "--config", cfgp],
                            stdout=log, stderr=subprocess.STDOUT)
    try:
        for _ in range(40):
            try:
                with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(
                        f"http://127.0.0.1:{port}/api/health", timeout=2) as r:
                    if r.status == 200:
                        break
            except Exception:
                time.sleep(0.3)
        req = urllib.request.Request(
            f"http://127.0.0.1:{port}/mcp",
            data=json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/list"}).encode(),
            headers={"Content-Type": "application/json",
                     "Authorization": "Bearer " + TOKEN},
            method="POST")
        try:
            with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(
                    req, timeout=8) as r:
                status, code = r.status, ""
        except urllib.error.HTTPError as exc:
            status = exc.code
            code = ""
            try:
                code = json.loads(exc.read().decode()).get("error", {}).get("code", "")
            except Exception:  # noqa: BLE001
                pass
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=6)
        except Exception:  # noqa: BLE001
            proc.kill()
        log.close()
    try:
        with open(lf, encoding="utf-8", errors="replace") as fh:
            banner = fh.read()
    except OSError:
        banner = ""
    return status, code, banner


_now = _dt.datetime.now().astimezone()

st, code, _b = ttl_probe((_now - _dt.timedelta(minutes=5)).isoformat(timespec="seconds"), 18792)
check("过期 Token -> 401 且 code=token_expired", st == 401 and code == "token_expired",
      f"{st} / {code}")
check("过期时启动横幅给出警告", "已过期" in _b and "token_expired" in _b)

st, code, banner = ttl_probe((_now + _dt.timedelta(days=30)).isoformat(timespec="seconds"), 18793)
check("有效期内 -> 200", st == 200, f"{st} / {code}")
check("有效期内横幅显示剩余天数", "token 剩" in banner)

st, code, _b = ttl_probe("", 18794)
check("token_expires_at 为空 -> 永久有效，200", st == 200, f"{st} / {code}")

# 到期时间串写坏了：按永久处理，但不能崩
st, code, _b = ttl_probe("not-a-real-time", 18795)
check("到期字段格式错误 -> 宽容处理为永久，200", st == 200, f"{st} / {code}")

check("make_expiry(0) 表示永久", wa.make_expiry(0) == "")
check("expiry_state 能识别剩余不足 3 天",
      wa.expiry_state(wa.make_expiry(1))[0] == "expiring")
shutil.rmtree(ttl_tmp, ignore_errors=True)


# ---------------------------------------------------------------------------
section("J 运行日志轮转（logs/wkfile.log 不再无限膨胀）")

rot_tmp = tempfile.mkdtemp(prefix="wk-rot-")
rot_path = os.path.join(rot_tmp, "logs", "wkfile.log")

w = wa.RotatingLogWriter(rot_path, max_bytes=2048, keep=2)
check("写入前自动建出 logs 目录", os.path.isdir(os.path.dirname(rot_path)))
for i in range(200):
    w.write("line-%03d-%s\n" % (i, "y" * 30))
w.flush()
w.close()
check("超过上限后发生轮转（出现 .1）", os.path.isfile(rot_path + ".1"))
check("历史份数不超过 keep", not os.path.isfile(rot_path + ".3"))
check("当前文件仍在上限附近", os.path.getsize(rot_path) <= 2048 * 2,
      str(os.path.getsize(rot_path)))

# keep=0 → 不留历史（换一个干净路径，免得看到上一段的 .1）
rot_path2 = os.path.join(rot_tmp, "logs", "nolimit.log")
wa.RotatingLogWriter(rot_path2, max_bytes=0, keep=0).close()
w0 = wa.RotatingLogWriter(rot_path2, max_bytes=256, keep=0)
for i in range(50):
    w0.write("z" * 40 + "\n")
w0.flush()
w0.close()
check("keep=0 时不留历史文件", not os.path.isfile(rot_path2 + ".1"))
check("keep=0 时当前文件被清掉（不会无限长）",
      os.path.getsize(rot_path2) < 256 * 2, str(os.path.getsize(rot_path2)))

# size() / 作为 sys.stdout 替身的基本协议
w1 = wa.RotatingLogWriter(rot_path, max_bytes=0, keep=0)
check("max_bytes=0 表示不轮转", w1.max_bytes == 0)
check("writer 实现了 stdout 需要的方法",
      all(hasattr(w1, n) for n in ("write", "flush", "writelines", "isatty", "fileno")))
check("isatty() 为 False（不会被当成终端）", w1.isatty() is False)
w1.write("ok\n")
w1.flush()
check("size() 能读到当前大小", w1.size() > 0)
w1.close()

wa.rotate_log_file(rot_path, 1)
check("rotate_log_file 直接调用可用", os.path.isfile(rot_path + ".1"))
shutil.rmtree(rot_tmp, ignore_errors=True)


# ---------------------------------------------------------------------------
section("K 只读 Token / 请求级只读")

tk_cfg = wa.Config({"token": "f" * 40, "readonly_token": "r" * 40}, os.path.join(
    tempfile.mkdtemp(prefix="wk-tk-"), "wkfile.config.json"))
tk_agent = wa.Agent(tk_cfg)
check("主 Token -> full", tk_agent.token_kind("f" * 40) == "full")
check("只读 Token -> readonly", tk_agent.token_kind("r" * 40) == "readonly")
check("不认识的 Token -> none", tk_agent.token_kind("x" * 40) == "none")
check("空 Token -> none", tk_agent.token_kind("") == "none")
check("未配置 readonly_token 时 -> none",
      wa.Agent(wa.Config({"token": "f" * 40}, "x.json")).token_kind("r" * 40) == "none")

tk_agent.set_request_mode(False)
check("普通请求的 effective_readonly 为 False", tk_agent.effective_readonly() is False)
try:
    tk_agent._require_write()
    check("完整权限请求允许写入", True)
except wa.PathError:
    check("完整权限请求允许写入", False)

tk_agent.set_request_mode(True)
check("只读请求的 effective_readonly 为 True", tk_agent.effective_readonly() is True)
try:
    tk_agent._require_write()
    check("只读请求写操作被拒", False)
except wa.PathError as exc:
    check("只读请求写操作被拒", exc.code == "read_only", exc.code)

# server 级 read_only 仍然生效
tk_cfg.read_only = True
tk_agent.set_request_mode(False)
try:
    tk_agent._require_write()
    check("server 级 read_only 仍会拒绝写入", False)
except wa.PathError as exc:
    check("server 级 read_only 仍会拒绝写入", exc.code == "read_only", exc.code)
tk_cfg.read_only = False
# 注意：base_dir 本身就是那个 mkdtemp 目录（base_dir = dirname(config_path)），
# 早先这里多套了一层 dirname，等于去 rmtree 整个系统临时目录 —— 沙箱的批量删除
# 护栏一拦、后面的用例就全不跑了。只删自己建的那一层。
shutil.rmtree(tk_cfg.base_dir, ignore_errors=True)


# ---------------------------------------------------------------------------
section("L 备份查看/回滚 + tail/tree/multi_read + replace 预览")

lt = tempfile.mkdtemp(prefix="wk-l-")
l_data = os.path.join(lt, "wkfile")
l_share = os.path.join(lt, "share")
os.makedirs(l_data, exist_ok=True)
os.makedirs(os.path.join(l_share, "sub"), exist_ok=True)
l_cfg_path = os.path.join(l_data, "wkfile.config.json")
with open(l_cfg_path, "w", encoding="utf-8") as fh:
    json.dump({"token": TOKEN, "roots": [l_share], "auto_backup": True,
               "audit_log": "logs/audit.jsonl"}, fh)
l_cfg = wa.load_config(l_cfg_path)
l_agent = wa.Agent(l_cfg)

target = os.path.join(l_share, "sub", "app.conf")
with open(target, "w", encoding="utf-8") as fh:
    fh.write("mode = debug\nport = 8080\n")
l_agent.dispatch("write", {"path": target, "content": "mode = release\nport = 8080\n"})
l_agent.dispatch("write", {"path": target, "content": "mode = prod\nport = 9090\n"})

res = l_agent.dispatch("backup_list", {})
check("win_backup_list 能看到历史版本", res["count"] >= 2, str(res["count"]))
check("备份条目能还原出原文件路径",
      any(os.path.normcase(it["original_path"]) == os.path.normcase(target)
          for it in res["items"]), str(res["items"][:1]))
check("备份列表默认落在 wkfile\\backups 下",
      os.path.normcase(res["backup_root"]).startswith(os.path.normcase(l_data)),
      res["backup_root"])

# 越界：不能拿任意路径当备份文件
try:
    l_agent.dispatch("backup_restore", {"backup_path": target})
    check("backup_restore 拒绝非备份目录的文件", False)
except wa.PathError as exc:
    check("backup_restore 拒绝非备份目录的文件", exc.code == "out_of_scope", exc.code)

res = l_agent.dispatch("backup_restore", {"original_path": target, "dry_run": True})
check("回滚 dry_run 不改动磁盘", res.get("dry_run") and open(target, encoding="utf-8").read()
      .startswith("mode = prod"))
check("回滚 dry_run 指明目标文件", os.path.normcase(res["target"]) == os.path.normcase(target))

res = l_agent.dispatch("backup_restore", {"original_path": target})
check("按 original_path 回滚成功", res.get("restored") is True, str(res)[:160])
check("回滚用的是最新的一份备份",
      "mode = release" in open(target, encoding="utf-8").read(),
      open(target, encoding="utf-8").read()[:60])
check("回滚前的版本被另存（回滚也可撤销）",
      bool(res.get("previous_version_backup")) and
      os.path.isfile(res["previous_version_backup"]))

# tail
with open(os.path.join(l_share, "big.log"), "w", encoding="utf-8") as fh:
    for i in range(1, 501):
        fh.write("log line %03d\n" % i)
res = l_agent.dispatch("tail", {"path": os.path.join(l_share, "big.log"), "lines": 5})
check("win_tail 只回最后 5 行", res["returned_lines"] == 5 and
      "log line 500" in res["content"] and "log line 496" in res["content"]
      and "log line 495" not in res["content"], res["content"][-60:])
check("win_tail 不把整个文件都读了", "log line 001" not in res["content"])
check("win_tail 起始行号正确（整文件可定位）", res["start_line"] == 496, str(res["start_line"]))

# tree
res = l_agent.dispatch("tree", {"path": l_share, "depth": 2})
names = [e["name"] for e in res["entries"]]
check("win_tree 列出子目录与文件", "sub" in names and "big.log" in names, str(names))
check("win_tree 带缩进层级",
      any(e["depth"] == 1 for e in res["entries"]), str(res["entries"][:2]))
check("win_tree 统计了目录/文件数", res["dirs"] >= 1 and res["files"] >= 1, str(res)[:120])
res = l_agent.dispatch("tree", {"path": l_share, "max_nodes": 1})
check("win_tree 超上限时标记 truncated", res["truncated"] is True)

# multi_read
res = l_agent.dispatch("multi_read", {"paths": [
    target, os.path.join(l_share, "big.log"), os.path.join(l_share, "nope.txt")]})
check("win_multi_read 逐个返回状态", res["count"] == 3 and res["ok_count"] == 2, str(res)[:120])
bad = [r for r in res["results"] if not r.get("ok")]
check("win_multi_read 单个失败不影响其它", len(bad) == 1 and bad[0]["error"] == "not_found",
      str(bad)[:120])
check("win_multi_read 拒绝空列表",
      _raises(lambda: l_agent.dispatch("multi_read", {"paths": []})))

# replace dry_run
res = l_agent.dispatch("replace", {"path": target, "old": "port = 8080",
                                   "new": "port = 7070", "dry_run": True})
check("win_replace dry_run 返回 diff", res.get("dry_run") and "+port = 7070" in res["diff"],
      res.get("diff", "")[:120])
check("win_replace dry_run 不落盘", "port = 8080" in open(target, encoding="utf-8").read())
res = l_agent.dispatch("replace", {"path": target, "old": "port = 8080", "new": "port = 7070"})
check("去掉 dry_run 后真正落盘", "port = 7070" in open(target, encoding="utf-8").read())
l_agent.set_request_mode(True)
try:
    l_agent.dispatch("backup_restore", {"original_path": target})
    check("只读 Token 不能执行回滚", False)
except wa.PathError as exc:
    check("只读 Token 不能执行回滚", exc.code == "read_only", exc.code)
l_agent.set_request_mode(False)
shutil.rmtree(lt, ignore_errors=True)


# ---------------------------------------------------------------------------
section("M TLS 证书校验")

check("没配证书时 tls_enabled 为 False",
      wa.tls_enabled(wa.Config({"tls_cert": "", "tls_key": ""}, "x.json")) is False)
check("证书私钥都填了才算启用",
      wa.tls_enabled(wa.Config({"tls_cert": "a.crt", "tls_key": "a.key"}, "x.json")) is True)

tls_probe_tmp = tempfile.mkdtemp(prefix="wk-tlsv-")
try:
    wa.validate_tls_pair("", "")
    check("空路径被拒", False)
except wa.PathError as exc:
    check("空路径被拒", exc.code == "bad_request", exc.code)
try:
    wa.validate_tls_pair(os.path.join(tls_probe_tmp, "no.crt"),
                         os.path.join(tls_probe_tmp, "no.key"))
    check("文件不存在被拒", False)
except wa.PathError as exc:
    check("文件不存在被拒", exc.code == "not_found", exc.code)
fake_crt = os.path.join(tls_probe_tmp, "a.crt")
fake_key = os.path.join(tls_probe_tmp, "a.key")
with open(fake_crt, "w", encoding="utf-8") as fh:
    fh.write("not a cert")
with open(fake_key, "w", encoding="utf-8") as fh:
    fh.write("not a key")
try:
    wa.validate_tls_pair(fake_crt, fake_key)
    check("内容不是证书时被拒", False)
except wa.PathError as exc:
    check("内容不是证书时被拒", exc.code == "bad_cert", exc.code)
check("describe_cert 对坏文件返回空串而不是抛异常", wa.describe_cert(fake_crt) == "")
shutil.rmtree(tls_probe_tmp, ignore_errors=True)


# ---------------------------------------------------------------------------
section("N 只读 Token 走 HTTP 端到端 + MCP 调用进审计")

ro_tmp = tempfile.mkdtemp(prefix="wk-ro-")
ro_share = os.path.join(ro_tmp, "share")
os.makedirs(ro_share, exist_ok=True)
ro_file = os.path.join(ro_share, "a.txt")
with open(ro_file, "w", encoding="utf-8") as fh:
    fh.write("hello\n")
RO_PORT = 18796
RO_TOKEN = "r" * 40
ro_cfg_path = os.path.join(ro_tmp, "wkfile.config.json")
with open(ro_cfg_path, "w", encoding="utf-8") as fh:
    json.dump({"host": "127.0.0.1", "port": RO_PORT, "token": TOKEN,
               "readonly_token": RO_TOKEN, "roots": [ro_share],
               "audit_log": "logs/audit.jsonl"}, fh)
ro_log = open(os.path.join(ro_tmp, "srv.log"), "w", encoding="utf-8")
ro_proc = subprocess.Popen([PY, os.path.join(SERVER, "wkfile_agent.py"), "--config",
                            ro_cfg_path], stdout=ro_log, stderr=subprocess.STDOUT)
try:
    ro_up = False
    for _ in range(40):
        try:
            with urllib.request.build_opener(
                    urllib.request.ProxyHandler({})).open(
                        "http://127.0.0.1:%d/api/health" % RO_PORT, timeout=2) as r:
                ro_up = r.status == 200
                if ro_up:
                    break
        except Exception:
            time.sleep(0.4)
    check("只读 Token 测试服务已就绪", ro_up)

    def ro_call(tool, args, token):
        body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                           "params": {"name": tool, "arguments": args}}).encode()
        rq = urllib.request.Request(
            "http://127.0.0.1:%d/mcp" % RO_PORT, data=body,
            headers={"Content-Type": "application/json",
                     "Authorization": "Bearer " + token}, method="POST")
        with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(
                rq, timeout=8) as r:
            return json.loads(r.read().decode())["result"]

    if ro_up:
        r1 = ro_call("win_read", {"path": ro_file}, RO_TOKEN)
        check("只读 Token 可以读", "hello" in r1["content"][0]["text"], str(r1)[:120])
        r2 = ro_call("win_write", {"path": ro_file, "content": "nope"}, RO_TOKEN)
        check("只读 Token 写被拒", r2.get("isError") and "只读" in r2["content"][0]["text"],
              str(r2)[:160])
        check("只读 Token 写失败后文件没被改",
              open(ro_file, encoding="utf-8").read() == "hello\n")
        r3 = ro_call("win_roots", {}, RO_TOKEN)
        check("win_roots 标明只读范围", "只读 Token" in r3["content"][0]["text"],
              r3["content"][0]["text"][:160])
        r4 = ro_call("win_write", {"path": ro_file, "content": "hello"},
                     TOKEN)
        check("主 Token 依然可写", not r4.get("isError"), str(r4)[:160])

    time.sleep(0.4)
    ro_audit = os.path.join(ro_tmp, "logs", "audit.jsonl")
    check("审计日志文件已生成（父目录自动创建）", os.path.isfile(ro_audit), ro_audit)
    if os.path.isfile(ro_audit):
        with open(ro_audit, encoding="utf-8") as fh:
            recs = [json.loads(x) for x in fh if x.strip()]
        ops = {r.get("op") for r in recs}
        check("MCP 工具调用进了审计日志", "read" in ops and "write" in ops, str(ops))
        check("只读请求带 scope=readonly 标记",
              any(r.get("scope") == "readonly" for r in recs),
              str([r for r in recs if r.get("scope")][:1]))
        check("被拒的写操作也记了 error",
              any(r.get("ok") is False and r.get("error") == "read_only" for r in recs),
              str([r for r in recs if r.get("ok") is False][:1]))
finally:
    ro_proc.terminate()
    try:
        ro_proc.wait(timeout=6)
    except Exception:
        ro_proc.kill()
    ro_log.close()
    shutil.rmtree(ro_tmp, ignore_errors=True)


# ---------------------------------------------------------------------------
section("O 自签证书生成（纯标准库，IP SAN 可校验）")

import socket as _socket          # noqa: E402
import ssl as _ssl                # noqa: E402
import threading as _threading    # noqa: E402

import wkfile_selfcert as sc      # noqa: E402


def decode_cert(path):
    """读证书的 subject/issuer/SAN。

    CPython 里这个能力在**私有模块** `_ssl` 上（`ssl._test_decode_cert` 早在 3.13
    就被删掉了，写 ssl._test_decode_cert 只会 AttributeError）。产品代码
    describe_cert 走的也是这个口子。
    """
    import _ssl as _sslcert

    return _sslcert._test_decode_cert(path)


def san_ips(path):
    return [v for t, v in decode_cert(path).get("subjectAltName", ())
            if t == "IP Address"]


check("网络地址判定：内网/回环/保留都算私有", all([
    wa.is_private_ip("192.168.1.5"),
    wa.is_private_ip("10.0.0.7"),
    wa.is_private_ip("172.20.3.4"),
    wa.is_private_ip("127.0.0.1"),
    wa.is_private_ip("169.254.1.1"),
    not wa.is_private_ip("8.8.8.8"),
    not wa.is_private_ip("223.75.245.202"),
    not wa.is_private_ip("not-an-ip"),
]))
_o_lan = wa.local_ipv4()
check("local_ipv4 返回一个合法 IPv4", wa.is_private_ip(_o_lan)
      or _o_lan.count(".") == 3, _o_lan)

_o_tmp = tempfile.mkdtemp(prefix="wk-selfcert-")
try:
    try:
        sc.generate_pair(cn="x")
        check("既没 IP 也没域名时拒绝生成", False)
    except ValueError:
        check("既没 IP 也没域名时拒绝生成", True)

    try:
        sc.generate_pair(ips=["999.1.1.1"])
        check("非法 IP 被拒（否则会签出一张永远校验失败的证书）", False)
    except ValueError:
        check("非法 IP 被拒（否则会签出一张永远校验失败的证书）", True)

    try:
        sc.generate_rsa_key(1024)
        check("低于 2048 位的密钥被拒", False)
    except ValueError:
        check("低于 2048 位的密钥被拒", True)

    _o_cert = os.path.join(_o_tmp, "s.crt")
    _o_key = os.path.join(_o_tmp, "s.key")
    _t0 = time.time()
    _out = sc.write_pair(_o_cert, _o_key, cn="wkfile-test",
                         ips=["127.0.0.1", "10.1.2.3"])
    _gen_secs = time.time() - _t0
    check("生成并落盘成功", os.path.isfile(_o_cert) and os.path.isfile(_o_key))
    check("返回绝对路径", os.path.isabs(_out[0]) and os.path.isabs(_out[1]))
    check("生成耗时可接受（< 120 秒）", _gen_secs < 120, "%.1fs" % _gen_secs)

    # 1) 能被 ssl 加载 —— 说明 PEM 结构、私钥与证书是配对的
    try:
        _ssl.SSLContext(_ssl.PROTOCOL_TLS_SERVER).load_cert_chain(_o_cert, _o_key)
        check("ssl.load_cert_chain 能加载（格式正确、公私钥配对）", True)
    except Exception as _exc:  # noqa: BLE001
        check("ssl.load_cert_chain 能加载（格式正确、公私钥配对）", False, str(_exc))

    check("describe_cert 能读出 CN", "wkfile-test" in wa.describe_cert(_o_cert),
          wa.describe_cert(_o_cert))

    # 2) SAN 里确实写了给定的 IP（缺一个，按那个 IP 连就必然校验失败）
    try:
        _info = decode_cert(_o_cert)
        _san = san_ips(_o_cert)
        check("SAN 含全部给定 IP", set(_san) == {"127.0.0.1", "10.1.2.3"}, str(_san))
        check("证书是 v3", _info.get("version") == 3, str(_info.get("version")))
    except Exception as _exc:  # noqa: BLE001
        check("SAN 含全部给定 IP", False, "解码失败: %s" % _exc)
        check("证书是 v3", False, "解码失败")

    # 3) 真起一次 TLS 握手：连签名对不对一起验了
    def _handshake(*, check_hostname, server_hostname, cafile=None):
        """起一个一次性的 TLS 服务端，客户端按给定策略握手，返回 (成功?, 详情)。"""
        srv_ctx = _ssl.SSLContext(_ssl.PROTOCOL_TLS_SERVER)
        srv_ctx.load_cert_chain(_o_cert, _o_key)
        lsock = _socket.socket()
        lsock.setsockopt(_socket.SOL_SOCKET, _socket.SO_REUSEADDR, 1)
        lsock.bind(("127.0.0.1", 0))
        lsock.listen(1)
        port = lsock.getsockname()[1]
        srv_err = {}

        def serve():
            try:
                conn, _ = lsock.accept()
                with srv_ctx.wrap_socket(conn, server_side=True) as s:
                    s.recv(16)
            except Exception as exc:  # noqa: BLE001
                srv_err["e"] = str(exc)
            finally:
                try:
                    lsock.close()
                except Exception:  # noqa: BLE001
                    pass

        th = _threading.Thread(target=serve, daemon=True)
        th.start()
        try:
            cli_ctx = _ssl.create_default_context(cafile=cafile)
            cli_ctx.check_hostname = bool(check_hostname)
            # 注意顺序：置 check_hostname=False 会顺带把 verify_mode 打回 CERT_NONE，
            # 所以要在它之后再显式要求校验 —— 否则这个用例就变成「什么都不验」了
            cli_ctx.verify_mode = _ssl.CERT_REQUIRED
            raw = _socket.create_connection(("127.0.0.1", port), timeout=8)
            try:
                with cli_ctx.wrap_socket(raw, server_hostname=server_hostname) as cs:
                    cs.sendall(b"ping")
                return True, ""
            finally:
                raw.close()
        except Exception as exc:  # noqa: BLE001
            return False, str(exc)
        finally:
            th.join(timeout=8)

    # 3a) 把它当信任锚 + 按 SAN 里的 IP 校验主机名 -> 必须成功
    #     这一条同时证明了：签名是对的、密钥是对的、IP SAN 真的起作用
    _ok, _why = _handshake(check_hostname=True, server_hostname="127.0.0.1",
                           cafile=_o_cert)
    check("作为信任锚 + 按 SAN 的 IP 校验主机名，握手成功", _ok, _why)

    # 3b) 反向对照：SAN 里没有的 IP 必须被拒 —— 否则说明主机名校验根本没生效
    _bad, _ = _handshake(check_hostname=True, server_hostname="10.9.9.9",
                         cafile=_o_cert)
    check("SAN 里没有的 IP 会被拒绝（负向对照）", not _bad)

    # 4) 不指定 IP 时，generate_selfcert 自动把本机地址补进 SAN
    _a_cert = os.path.join(_o_tmp, "auto.crt")
    _a_key = os.path.join(_o_tmp, "auto.key")
    wa.generate_selfcert(_a_cert, _a_key, cn="wkfile-auto")
    check("generate_selfcert 自动补 IP 并落盘",
          os.path.isfile(_a_cert) and os.path.isfile(_a_key))
    try:
        _san2 = san_ips(_a_cert)
        check("自动 SAN 一定含 127.0.0.1", "127.0.0.1" in _san2, str(_san2))
        check("自动 SAN 含本机内网 IP", wa.local_ipv4() in _san2, str(_san2))
    except Exception as _exc:  # noqa: BLE001
        check("自动 SAN 一定含 127.0.0.1", False, str(_exc))
        check("自动 SAN 含本机内网 IP", False, str(_exc))
    check("自动生成的证书能被 ssl 加载",
          _raises(lambda: _ssl.SSLContext(_ssl.PROTOCOL_TLS_SERVER).load_cert_chain(
              _a_cert, _a_key)) is False)

    # 5) 默认落盘位置在数据目录下的 tls\，不往 exe 旁边乱丢文件
    _dc, _dk = wa.default_selfcert_paths()
    check("默认证书目录位于数据目录内",
          os.path.dirname(_dc) == os.path.join(wa.data_dir(), "tls"), _dc)
    check("默认证书与私钥同名不同后缀",
          os.path.splitext(_dc)[0] == os.path.splitext(_dk)[0], f"{_dc} / {_dk}")

    # 6) CLI 入口能真的跑通（子进程，验证参数接线）
    _cli_dir = os.path.join(_o_tmp, "cli")
    _cli = subprocess.run(
        [PY, os.path.join(SERVER, "wkfile_agent.py"), "--gen-selfcert",
         "--selfcert-dir", _cli_dir, "--selfcert-cn", "wkfile-cli",
         "--selfcert-ip", "127.0.0.1,203.0.113.9"],
        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=180,
    )
    check("CLI --gen-selfcert 退出码为 0", _cli.returncode == 0,
          (str(_cli.returncode) + " " + (_cli.stdout or "") + (_cli.stderr or ""))[:200])
    _cli_cert = os.path.join(_cli_dir, "wkfile.crt")
    check("CLI 生成的证书文件存在", os.path.isfile(_cli_cert), _cli_cert)
    if os.path.isfile(_cli_cert):
        _san3 = san_ips(_cli_cert)
        check("CLI 的 SAN 只含显式给定的 IP（不做多余联网探测）",
              set(_san3) == {"127.0.0.1", "203.0.113.9"}, str(_san3))

    _pub = wa.detect_public_ip()
    print("  [ .. ] 公网 IP 探测结果：" + (_pub or "(未探到，通常是无直接出网)"))
finally:
    shutil.rmtree(_o_tmp, ignore_errors=True)


# ---------------------------------------------------------------------------
def _shake(cert, key, cafile):
    """起一次性 TLS 服务端，客户端只信任 cafile，返回 (成功?, 详情)。

    模拟的是 Electron / Node 系客户端：不做 AIA 补链，服务端发什么它就只认什么。
    """
    sctx = _ssl.SSLContext(_ssl.PROTOCOL_TLS_SERVER)
    sctx.load_cert_chain(cert, key)
    ls = _socket.socket()
    ls.setsockopt(_socket.SOL_SOCKET, _socket.SO_REUSEADDR, 1)
    ls.bind(("127.0.0.1", 0))
    ls.listen(1)
    prt = ls.getsockname()[1]

    def srv():
        try:
            cn_, _ = ls.accept()
            with sctx.wrap_socket(cn_, server_side=True) as s:
                s.recv(16)
        except Exception:  # noqa: BLE001
            pass
        finally:
            try:
                ls.close()
            except Exception:  # noqa: BLE001
                pass

    t = _threading.Thread(target=srv, daemon=True)
    t.start()
    try:
        cctx = _ssl.create_default_context(cafile=cafile)
        cctx.check_hostname = True
        cctx.verify_mode = _ssl.CERT_REQUIRED
        r = _socket.create_connection(("127.0.0.1", prt), timeout=8)
        try:
            with cctx.wrap_socket(r, server_hostname="127.0.0.1") as cs:
                cs.sendall(b"ping")
            return True, ""
        finally:
            r.close()
    except Exception as exc:  # noqa: BLE001
        return False, str(exc)
    finally:
        t.join(timeout=8)


section("P 证书链完整性（只有叶子证书 = Electron 客户端连不上）")

_p_tmp = os.path.join(tmp, "p-chain")
os.makedirs(_p_tmp, exist_ok=True)


def _w(path, text):
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(text)


def _r(path):
    with open(path, "r", encoding="utf-8") as fh:
        return fh.read()


try:
    # ---- 1) 计数/判定函数本身 ----
    _one = os.path.join(_p_tmp, "one.pem")
    _w(_one, "-----BEGIN CERTIFICATE-----\nQUJD\n-----END CERTIFICATE-----\n")
    _two = os.path.join(_p_tmp, "two.pem")
    _w(_two, "-----BEGIN CERTIFICATE-----\nQUJD\n-----END CERTIFICATE-----\n"
             "-----BEGIN CERTIFICATE-----\nREVG\n-----END CERTIFICATE-----\n")

    check("单证书文件数出 1 张", wa.count_cert_chain(_one) == 1,
          str(wa.count_cert_chain(_one)))
    check("双证书文件数出 2 张", wa.count_cert_chain(_two) == 2,
          str(wa.count_cert_chain(_two)))
    check("chain_health: 单证书判为不完整", wa.chain_health(_one) == (1, True),
          str(wa.chain_health(_one)))
    check("chain_health: 双证书判为完整", wa.chain_health(_two) == (2, False),
          str(wa.chain_health(_two)))
    check("chain_health: 文件不存在时保守判为不完整",
          wa.chain_health(os.path.join(_p_tmp, "nope")) == (0, True))
    check("提示文案点名了 Electron 客户端与 fullchain",
          "Electron" in wa.CHAIN_INCOMPLETE_LONG
          and "fullchain" in wa.CHAIN_INCOMPLETE_LONG)

    # ---- 2) 真造一条三级 CA 链，复现"缺中间证书就握手失败" ----
    if shutil.which("openssl") is None:
        print("  [skip] 本机无 openssl，跳过三级链实测")
    else:
        def _ossl(*a):
            return subprocess.run(["openssl", *a], capture_output=True, cwd=_p_tmp)

        _steps = [
            ("req", "-x509", "-newkey", "rsa:2048", "-nodes", "-keyout", "root.key",
             "-out", "root.crt", "-days", "3650", "-subj", "/CN=Probe Root CA",
             "-addext", "basicConstraints=critical,CA:TRUE",
             "-addext", "keyUsage=critical,keyCertSign,cRLSign"),
            ("req", "-newkey", "rsa:2048", "-nodes", "-keyout", "int.key",
             "-out", "int.csr", "-subj", "/CN=Probe Intermediate CA",
             "-addext", "basicConstraints=critical,CA:TRUE",
             "-addext", "keyUsage=critical,keyCertSign,cRLSign"),
            ("x509", "-req", "-in", "int.csr", "-CA", "root.crt", "-CAkey", "root.key",
             "-CAcreateserial", "-out", "int.crt", "-days", "3650",
             "-copy_extensions", "copy"),
            ("req", "-newkey", "rsa:2048", "-nodes", "-keyout", "leaf.key",
             "-out", "leaf.csr", "-subj", "/CN=127.0.0.1",
             "-addext", "subjectAltName=IP:127.0.0.1",
             "-addext", "basicConstraints=critical,CA:FALSE",
             "-addext", "keyUsage=critical,digitalSignature,keyEncipherment"),
            ("x509", "-req", "-in", "leaf.csr", "-CA", "int.crt", "-CAkey", "int.key",
             "-CAcreateserial", "-out", "leaf.crt", "-days", "365",
             "-copy_extensions", "copy"),
        ]
        _step_ok, _step_err = True, ""
        for _s in _steps:
            _pr = _ossl(*_s)
            if _pr.returncode != 0:
                _step_ok = False
                _step_err = _s[0] + " " + _pr.stderr.decode("utf-8", "replace")[-200:]
                break
        check("openssl 造出三级 CA 链（根 -> 中间 -> 叶子）", _step_ok, _step_err)

        if _step_ok:
            _p_leaf = os.path.join(_p_tmp, "leaf.crt")
            _p_lkey = os.path.join(_p_tmp, "leaf.key")
            _p_root = os.path.join(_p_tmp, "root.crt")
            _p_int = os.path.join(_p_tmp, "int.crt")
            # 这里必须走产品代码合并，而不是测试里手拼字符串 ——
            # 手拼只能证明"拼起来能用"，证明不了"程序拼得对"。
            _p_full = wa.prepare_cert_file(_p_leaf, _p_int)

            check("拼接后的链数出 2 张", wa.count_cert_chain(_p_full) == 2,
                  str(wa.count_cert_chain(_p_full)))
            check("拼接后的链被 chain_health 判为完整",
                  wa.chain_health(_p_full)[1] is False)

            # 2a) 服务端只发叶子 -> 客户端手上只有根，缺中间 -> 必须失败
            _b, _bw = _shake(_p_leaf, _p_lkey, _p_root)
            check("只发叶子证书时客户端验证失败（复现真实故障）", not _b, _bw)

            # 2b) 服务端发 叶子+中间 -> 同一客户端必须成功（证明改法有效）
            _g, _gw = _shake(_p_full, _p_lkey, _p_root)
            check("补上中间证书后同一客户端握手成功（证明改法有效）", _g, _gw)
except Exception as _exc:  # noqa: BLE001 —— 出错要记账，不能把后面几节悄悄带走
    check("P 节内部异常", False, repr(_exc))
    print("      %s" % traceback.format_exc().strip().splitlines()[-1])


section("Q 三件套 public + chain + key：分开填也能用")

_q_tmp = os.path.join(tmp, "q-chain")
os.makedirs(_q_tmp, exist_ok=True)
_q_int = os.path.join(_p_tmp, "int.crt")

if not (os.path.isfile(_p_leaf) and os.path.isfile(_q_int)):
    print("  [skip] 上一节的证书链没造出来（可能是本机没有 openssl）")
else:
    try:
        # 1) 分开给：两个文件各 1 张；挂在一起才算完整
        check("只给公钥证书时判为链不完整", wa.chain_health(_p_leaf) == (1, True),
              str(wa.chain_health(_p_leaf)))
        check("公钥证书 + 中间链 合起来判为完整",
              wa.chain_health(_p_leaf, _q_int) == (2, False),
              str(wa.chain_health(_p_leaf, _q_int)))

        # 2) validate_tls_pair 收第三个参数（链），三件套能配对
        try:
            _q_msg = wa.validate_tls_pair(_p_leaf, _p_lkey, _q_int)
            check("validate_tls_pair 接受三件套并配对成功", "链 2 张" in _q_msg, _q_msg)
        except Exception as _exc:  # noqa: BLE001
            check("validate_tls_pair 接受三件套并配对成功", False, str(_exc))

        # 3) 链文件写错路径必须当场拒掉（否则用户以为配好了，重启才发现起不来）
        try:
            wa.validate_tls_pair(_p_leaf, _p_lkey, os.path.join(_p_tmp, "nope.crt"))
            check("链文件不存在时被拒", False, "竟然没报错")
        except wa.PathError as _exc:
            check("链文件不存在时被拒", _exc.code == "not_found", _exc.code)

        # 4) prepare_cert_file：合并结果落在指定目录，张数正确
        _q_dir = os.path.join(_q_tmp, "tls")
        _q_full = wa.prepare_cert_file(_p_leaf, _q_int, work_dir=_q_dir)
        check("合并文件落在指定目录", os.path.dirname(_q_full) == _q_dir, _q_full)
        check("合并后是 2 张证书", wa.count_cert_chain(_q_full) == 2,
              str(wa.count_cert_chain(_q_full)))
        check("不配链时原样返回证书路径（不做无谓复制）",
              wa.prepare_cert_file(_p_leaf, "") == os.path.abspath(_p_leaf))

        # 5) 顺序不能反：站点证书必须在前，否则客户端读到的是中间 CA 当叶子
        with open(_q_full, "r", encoding="utf-8") as _fh:
            _q_blob = _fh.read()
        _q_first = _q_blob.find(_r(_p_leaf).splitlines()[1][:40])
        _q_second = _q_blob.find(_r(_q_int).splitlines()[1][:40])
        check("合并顺序：站点证书在前、中间链在后",
              -1 < _q_first < _q_second, "%s / %s" % (_q_first, _q_second))

        # 6) 真握手：合并出来的文件能被"只信任根"的客户端验证通过
        _q_ok, _q_err = _shake(_q_full, _p_lkey, _p_root)
        check("合并后的证书链能被客户端验证通过", _q_ok, _q_err)

        # 7) 工作目录不可用（这里用"同名路径已被一个普通文件占住"来构造，
        #    是 Windows 上最真实的场景）→ 必须退回系统临时目录，不能因此起不来
        _q_blocked = os.path.join(_q_tmp, "blocked")
        with open(_q_blocked, "w", encoding="utf-8") as _fh:
            _fh.write("这是一个普通文件，占了本该是目录的位置\n")
        _q_fb = wa.prepare_cert_file(_p_leaf, _q_int, work_dir=_q_blocked)
        check("work_dir 不可用时退回临时目录且文件有效",
              os.path.isfile(_q_fb) and wa.count_cert_chain(_q_fb) == 2
              and not _q_fb.startswith(_q_blocked), _q_fb)

        # 8) 二进制 DER 混进来时要给出能看懂的原因，而不是一句 SSLError
        _q_der = os.path.join(_q_tmp, "leaf.der")
        with open(_q_der, "wb") as _fh:
            _fh.write(b"\x30\x82\x01\x00")
        try:
            wa.prepare_cert_file(_q_der, _q_int, work_dir=_q_dir)
            check("二进制证书混进链时有明确报错", False, "竟然没报错")
        except wa.PathError as _exc:
            check("二进制证书混进链时有明确报错", "PEM" in _exc.message, _exc.message)
    except Exception as _exc:  # noqa: BLE001 —— 出错要记账，别把汇总也带走
        check("Q 节内部异常", False, repr(_exc))


# ---------------------------------------------------------------------------
section("R 数据库与缓存：协议 + 分级 + 权限三重闸门")
# ---------------------------------------------------------------------------

# ---- R1. MySQL SQL 分级 ----
if wa.dbmod is None:
    skip("R1 SQL 分级", "wkfile_mysql.py 未加载")
else:
    try:
        _r_cases = [
            ("SELECT * FROM t", "read"),
            ("select 1", "read"),
            ("  SHOW TABLES  ", "read"),
            ("DESC users", "read"),
            ("EXPLAIN SELECT 1", "read"),
            ("/* 注释 */ SELECT 1 -- 行注释", "read"),
            ("SELECT 'DROP TABLE x'", "read"),
            ("SELECT * FROM t WHERE a=';delete'", "read"),
            ("WITH c AS (SELECT 1) SELECT * FROM c", "read"),
            ("SELECT 1; ", "read"),
            ("SELECT 1; DROP TABLE t", "deny"),
            ("SELECT * FROM t INTO OUTFILE '/tmp/x'", "deny"),
            ("SELECT LOAD_FILE('/etc/passwd')", "deny"),
            ("SELECT * FROM t FOR UPDATE", "deny"),
            ("SELECT SLEEP(9)", "deny"),
            ("INSERT INTO t VALUES (1)", "write"),
            ("UPDATE t SET a=1", "write"),
            ("DELETE FROM t", "write"),
            ("DROP TABLE t", "write"),
            ("TRUNCATE TABLE t", "write"),
            ("WITH c AS (SELECT 1) DELETE FROM t", "write"),
            ("", "deny"),
        ]
        _r_bad = []
        for _sql, _want in _r_cases:
            _lvl, _kw, _why = wa.dbmod.classify_sql(_sql)
            if _lvl != _want:
                _r_bad.append("%r→%s(期望%s)" % (_sql[:36], _lvl, _want))
        check("R1 MySQL SQL 分级 %d 例全部正确" % len(_r_cases), not _r_bad,
              "; ".join(_r_bad))
        check("R1b 注释开头的写语句照样识别为写",
              wa.dbmod.classify_sql("/* hi */ DELETE FROM t")[0] == "write")
        check("R1c 末尾分号不误判成多语句",
              wa.dbmod.classify_sql("SELECT 1;\n")[0] == "read")
        check("R1d 字符串里的分号不误判成多语句",
              wa.dbmod.classify_sql("SELECT 'a;b'")[0] == "read")
        check("R1e EXPLAIN ANALYZE 到写语句上会被拦",
              wa.dbmod.classify_sql("EXPLAIN ANALYZE UPDATE t SET a=1")[0] == "write")
    except Exception as _exc:
        check("R1 SQL 分级内部异常", False, repr(_exc))

# ---- R2. Redis 命令分级 ----
if wa.redismod is None:
    skip("R2 Redis 分级", "wkfile_redis.py 未加载")
else:
    try:
        _rd_cases = [
            (["GET", "k"], "read"),
            (["HGETALL", "h"], "read"),
            (["SCAN", "0"], "read"),
            (["TYPE", "k"], "read"),
            (["SORT", "l"], "read"),
            (["SORT", "l", "STORE", "dst"], "write"),
            (["SET", "k", "v"], "write"),
            (["DEL", "k"], "write"),
            (["EXPIRE", "k", "10"], "write"),
            (["FLUSHALL"], "deny"),
            (["FLUSHDB"], "deny"),
            (["CONFIG", "GET", "maxmemory"], "deny"),
            (["EVAL", "return 1", "0"], "deny"),
            (["KEYS", "*"], "deny"),
            (["SHUTDOWN"], "deny"),
            (["SUBSCRIBE", "ch"], "deny"),
            (["MONITOR"], "deny"),
            (["UNKNOWN_CMD_XYZ"], "write"),
        ]
        _rd_bad = []
        for _args, _want in _rd_cases:
            _lvl, _why = wa.redismod.classify_command(_args[0], _args[1:])
            if _lvl != _want:
                _rd_bad.append("%s→%s(期望%s)" % (" ".join(_args)[:32], _lvl, _want))
        check("R2 Redis 命令分级 %d 例全部正确" % len(_rd_cases), not _rd_bad,
              "; ".join(_rd_bad))
        # KEYS 被禁的理由必须说清楚是"会阻塞实例"，不是含糊的"不允许"
        _keys_why = wa.redismod.classify_command("KEYS", ["*"])[1]
        check("R2b KEYS 的拒绝理由点明会阻塞实例", "阻塞" in _keys_why, _keys_why)
    except Exception as _exc:
        check("R2 Redis 分级内部异常", False, repr(_exc))

# ---- R3. 真库端到端（环境不具备则跳过）----
_R_DB = {
    "name": "e2e-mysql", "type": "mysql",
    "host": os.environ.get("DB_HOST", "127.0.0.1"),
    "port": int(os.environ.get("DB_PORT", "3306")),
    "user": os.environ.get("DB_USER", "root"),
    "password": os.environ.get("DB_PASSWORD", "root"),
    "database": os.environ.get("DB_NAME", "wkbds"),
    "allow_write": False, "max_rows": 50,
}
_R_REDIS = {
    "name": "e2e-redis", "type": "redis",
    "host": os.environ.get("REDIS_HOST", "127.0.0.1"),
    "port": int(os.environ.get("REDIS_PORT", "6379")),
    "password": os.environ.get("REDIS_PASSWORD", ""),
    "db": 0, "allow_write": False,
}

_r_mysql_ok = False
if wa.dbmod is not None:
    try:
        _r_mysql_ok = bool(wa.dbmod.MySQLSource(_R_DB).test().get("ok"))
    except Exception:  # noqa: BLE001
        _r_mysql_ok = False

_r_redis_ok = False
if wa.redismod is not None:
    try:
        _r_redis_ok = bool(wa.redismod.RedisSource(_R_REDIS).test().get("ok"))
    except Exception:  # noqa: BLE001
        _r_redis_ok = False

_r_base = os.path.join(tmp, "db-e2e")
os.makedirs(_r_base, exist_ok=True)
_r_cfg = wa.Config({"roots": [_r_base], "databases": [_R_DB, _R_REDIS]},
                   os.path.join(_r_base, "wkfile.config.json"))
_r_agent = wa.Agent(_r_cfg)


def _rcall(action, req=None, readonly=False):
    _r_agent.set_request_mode(readonly)
    try:
        return _r_agent.dispatch(action, req or {})
    finally:
        _r_agent.set_request_mode(False)


# ---- R3a. MySQL 真实链路 ----
if not _r_mysql_ok:
    skip("R3 MySQL 真实链路", "本机 %s:%s 连不上（设 DB_HOST/DB_USER/DB_PASSWORD 可指定）"
         % (_R_DB["host"], _R_DB["port"]))
else:
    try:
        _t = _rcall("db_test", {"source": "e2e-mysql"})
        check("R3a db_test 连通", _t.get("ok") is True, str(_t)[:160])
        # 注意不能简单搜 "password" —— 认证插件名 caching_sha2_password 里
        # 本身就带这个子串。要查的是"有没有把密码当字段回传"。
        _payload = json.dumps(_t, ensure_ascii=False)
        check("R3b db_test 不把密码当字段回传", '"password"' not in _payload,
              _payload[:160])
        check("R3b2 dsn 摘要不含密码",
              "topsecret" not in wa.dbmod.dsn_summary(
                  dict(_R_DB, password="topsecret")))

        _q = _rcall("db_query", {"source": "e2e-mysql",
                                 "sql": "SELECT 1 AS n, 'hi' AS s, NULL AS z"})
        check("R3c SELECT 结果与类型解码", _q["rows"] == [[1, "hi", None]], str(_q["rows"]))

        _q2 = _rcall("db_query", {"source": "e2e-mysql", "sql": "SELECT 1", "max_rows": 1})
        check("R3d max_rows 生效", _q2["row_count"] == 1, str(_q2)[:120])

        # 写语句走只读通道 -> 被拦且指引到正确的工具
        try:
            _rcall("db_query", {"source": "e2e-mysql", "sql": "SET @r_e2e = 1"})
            check("R3e 只读通道拒写并指引 win_db_exec", False, "竟然放行了")
        except wa.PathError as _exc:
            check("R3e 只读通道拒写并指引 win_db_exec",
                  _exc.code == "db_wrong_channel" and "win_db_exec" in _exc.message,
                  "%s / %s" % (_exc.code, _exc.message))

        # 未开 allow_write 时 db_exec 被拦
        try:
            _rcall("db_exec", {"source": "e2e-mysql", "sql": "SET @r_e2e = 1"})
            check("R3f 未开写权限时 db_exec 被拒", False, "竟然放行了")
        except wa.PathError as _exc:
            check("R3f 未开写权限时 db_exec 被拒", _exc.code == "db_write_disabled",
                  _exc.code)

        # 危险语句永远拒绝（即便开了写权限）
        _r_cfg_open = wa.Config(
            {"roots": [_r_base], "databases": [dict(_R_DB, allow_write=True)]},
            os.path.join(_r_base, "wkfile.config.json"))
        _r_agent_open = wa.Agent(_r_cfg_open)
        try:
            _r_agent_open.dispatch("db_exec",
                                   {"source": "e2e-mysql",
                                    "sql": "SELECT LOAD_FILE('/etc/passwd')"})
            check("R3g 开了写权限也拒 LOAD_FILE", False, "竟然放行了")
        except wa.PathError as _exc:
            check("R3g 开了写权限也拒 LOAD_FILE", _exc.code == "db_sql_rejected", _exc.code)

        # 只读 Token 下：即便数据源开了写、服务端不只读，也要拒
        _r_agent_open.set_request_mode(True)
        try:
            _r_agent_open.dispatch("db_exec", {"source": "e2e-mysql", "sql": "SET @x=1"})
            check("R3h 只读 Token 下写被拒", False, "竟然放行了")
        except wa.PathError as _exc:
            check("R3h 只读 Token 下写被拒", _exc.code == "read_only", _exc.code)
        finally:
            _r_agent_open.set_request_mode(False)

        # 开了写权限 + 主 Token -> 能写（临时表只影响本连接，不留痕）
        _w = _r_agent_open.dispatch(
            "db_exec", {"source": "e2e-mysql",
                        "sql": "CREATE TEMPORARY TABLE wkfile_e2e_tmp (a INT)"})
        check("R3i 开写权限后可执行写语句", "affected_rows" in _w, str(_w)[:120])

        _tb = _rcall("db_tables", {"source": "e2e-mysql", "database": _R_DB["database"]})
        check("R3j 列出数据表", isinstance(_tb.get("rows"), list), str(_tb)[:150])
        if _tb.get("rows"):
            _tname = [r[0] for r in _tb["rows"]][0]
            _sc = _rcall("db_schema", {"source": "e2e-mysql",
                                       "table": _tname, "database": _R_DB["database"]})
            check("R3k 查看表结构", _sc["columns"]["row_count"] > 0, str(_sc)[:150])

        # 类型写错 / 数据源不存在 -> 都要给可读原因
        try:
            _rcall("db_query", {"source": "e2e-redis", "sql": "SELECT 1"})
            check("R3l 数据源类型不符被拦", False, "竟然放行了")
        except wa.PathError as _exc:
            check("R3l 数据源类型不符被拦", _exc.code == "db_wrong_type", _exc.code)
        try:
            _rcall("db_query", {"source": "no-such-source", "sql": "SELECT 1"})
            check("R3m 不存在的数据源被拦", False, "竟然放行了")
        except wa.PathError as _exc:
            check("R3m 不存在的数据源被拦", _exc.code == "db_not_found", _exc.code)
    except Exception as _exc:
        check("R3 MySQL 链路内部异常", False, repr(_exc))

# ---- R3b. Redis 真实链路 ----
if not _r_redis_ok:
    skip("R3n Redis 真实链路", "本机 %s:%s 连不上（设 REDIS_PASSWORD 可指定密码）"
         % (_R_REDIS["host"], _R_REDIS["port"]))
else:
    try:
        _ri = _rcall("redis_info", {"source": "e2e-redis"})
        check("R3n redis_info 可用", _ri.get("ok") is True, str(_ri)[:160])
        _rk = _rcall("redis_keys", {"source": "e2e-redis", "pattern": "*", "limit": 5})
        check("R3o redis_keys 走 SCAN", isinstance(_rk.get("keys"), list), str(_rk)[:150])
        check("R3p redis_keys 返回续扫游标", "next_cursor" in _rk, str(_rk)[:100])

        try:
            _rcall("redis_write", {"source": "e2e-redis",
                                   "args": ["SET", "wkfile:e2e", "1"]})
            check("R3q 未开写权限时 Redis 写被拒", False, "竟然放行了")
        except wa.PathError as _exc:
            check("R3q 未开写权限时 Redis 写被拒", _exc.code == "db_write_disabled",
                  _exc.code)

        try:
            _rcall("redis_write", {"source": "e2e-redis", "args": ["FLUSHALL"]})
            check("R3r Redis 危险命令被拒", False, "竟然放行了")
        except wa.PathError as _exc:
            check("R3r Redis 危险命令被拒", _exc.code == "db_command_rejected", _exc.code)

        # 开写权限后真写一次，并清理干净
        _r_redis_open = wa.redismod.RedisSource(dict(_R_REDIS, allow_write=True))
        _r_redis_open.run(["SET", "wkfile:e2e:probe", "v1"])
        _gv = _r_redis_open.read_key("wkfile:e2e:probe")
        check("R3s 开写权限后可写可读", _gv.get("exists") is True
              and str(_gv.get("value")) == "v1", str(_gv)[:150])
        _r_redis_open.run(["DEL", "wkfile:e2e:probe"])
        check("R3t 测试数据已清理",
              _r_redis_open.read_key("wkfile:e2e:probe").get("exists") is False)
    except Exception as _exc:
        check("R3 Redis 链路内部异常", False, repr(_exc))

# ---- R4. MCP 工具注册 ----
try:
    _mcp_new = [t for t in mcp.TOOLS if t["name"].startswith(("win_db_", "win_redis_"))]
    check("R4a 新增 10 个数据库类工具", len(_mcp_new) == 10, str(len(_mcp_new)))
    check("R4b 每个新工具都有中文标题与风险标注",
          all(t.get("title") and t.get("annotations") for t in _mcp_new))
    check("R4c 写类工具被标成非只读",
          all(not t["annotations"]["readOnlyHint"] for t in _mcp_new
              if t["name"] in ("win_db_exec", "win_redis_write")))
    check("R4d 查询类工具被标成只读",
          all(t["annotations"]["readOnlyHint"] for t in _mcp_new
              if t["name"] in ("win_db_query", "win_db_tables", "win_db_schema",
                               "win_redis_get", "win_redis_keys")))
    _miss = [t["name"] for t in _mcp_new if mcp.ROUTE_MAP.get(t["name"]) not in wa.Agent.ROUTES]
    check("R4e 新工具都有对应的服务端路由", not _miss, str(_miss))
    # 工具级开关能覆盖到数据库工具
    _cfg_disabled = wa.Config(
        {"roots": [_r_base], "databases": [_R_DB],
         "tools_disable": ["win_db_query", "win_redis_write"]},
        os.path.join(_r_base, "wkfile.config.json"))
    _ag_dis = wa.Agent(_cfg_disabled)
    check("R4f tools_disable 能禁用数据库工具",
          "db_query" in _ag_dis.disabled_actions
          and "redis_write" in _ag_dis.disabled_actions,
          str(sorted(_ag_dis.disabled_actions))[:120])
    try:
        _ag_dis.dispatch("db_query", {"sql": "SELECT 1"})
        check("R4g 被禁用的数据库工具直接调用也被拒", False, "竟然放行了")
    except wa.PathError as _exc:
        check("R4g 被禁用的数据库工具直接调用也被拒",
              _exc.code == "tool_disabled", _exc.code)
except Exception as _exc:
    check("R4 MCP 注册内部异常", False, repr(_exc))

# ---- R5. 结果渲染 ----
try:
    _rv = _rcall("db_list")
    check("R5a db_list 渲染含数据源名", "e2e-mysql" in mcp.render("win_db_list", _rv))
    check("R5b db_list 不泄露密码",
          "password" not in mcp.render("win_db_list", _rv).lower())
    _rq = {"source": "x", "columns": ["a", "b"], "rows": [[1, None]], "row_count": 1,
           "truncated": False, "server_version": "8.0.0"}
    _txt = mcp.render("win_db_query", _rq)
    check("R5c 查询渲染成表格且 NULL 可见", "a" in _txt and "NULL" in _txt, _txt)
    _tbl = mcp._table_text(["名称", "值"], [["张三", "1"], ["李四", "22"]])
    _w = {mcp._display_width(ln.split(" | ")[0])
          for ln in _tbl.splitlines() if "-+-" not in ln}
    check("R5d 中文列按显示宽度对齐", len(_w) == 1, "%s / %s" % (sorted(_w), _tbl))
    check("R5e 空数据源渲染给出引导",
          "还没有配置" in mcp.render("win_db_list", {"sources": [], "count": 0}))
except Exception as _exc:
    check("R5 渲染内部异常", False, repr(_exc))

# ---- R6. 未配置数据源时的引导 ----
try:
    _r_empty = wa.Agent(wa.Config({"roots": [_r_base]},
                                  os.path.join(_r_base, "wkfile.config.json")))
    _re = _r_empty.dispatch("db_list", {})
    check("R6a 未配置时 db_list 返回 0 个源", _re["count"] == 0, str(_re))
    try:
        _r_empty.dispatch("db_query", {"sql": "SELECT 1"})
        check("R6b 未配置时查库被拦并给出引导", False, "竟然放行了")
    except wa.PathError as _exc:
        check("R6b 未配置时查库被拦并给出引导",
              _exc.code == "db_not_configured" and "界面" in _exc.message,
              "%s / %s" % (_exc.code, _exc.message))
except Exception as _exc:
    check("R6 内部异常", False, repr(_exc))

# ---- R7. 超时边界（不依赖真库）----
try:
    if wa.dbmod is None or wa.redismod is None:
        skip("R7 超时边界", "数据库模块未加载")
    else:
        import socket as _sk

        _my, _rd = wa.dbmod, wa.redismod
        _clamp_cases = [("-5", 8.0), ("0", 8.0), ("abc", 8.0), (None, 8.0),
                        (True, 8.0), ("", 8.0),
                        ("999999", _my.MAX_TIMEOUT_SEC), ("3", 3.0), ("0.5", 0.5)]
        _clamp_bad = []
        for _raw, _want in _clamp_cases:
            for _m in (_my, _rd):
                _got = _m._clamp_timeout(_raw, 8.0)
                if abs(_got - _want) > 1e-9:
                    _clamp_bad.append((_raw, _want, _m.__name__, _got))
        check("R7a 秒数夹取 %d 例（负数/0/脏值/超限）" % len(_clamp_cases),
              not _clamp_bad, str(_clamp_bad))

        # 负数会让 sock.settimeout 抛 ValueError —— 那不是 DBError，
        # 会一路冒到工具层变成"内部错误"，所以必须在读配置时就夹掉。
        _ms = _my.MySQLSource({"name": "x", "timeout_sec": -5,
                               "connect_timeout_sec": -1})
        _rs = _rd.RedisSource({"name": "x", "timeout_sec": "abc",
                               "connect_timeout_sec": "abc"})
        check("R7b 脏配置不会让 settimeout 抛 ValueError",
              _ms.timeout == 30.0 and _ms.connect_timeout == 8.0
              and _rs.timeout == 15.0 and _rs.connect_timeout == 8.0,
              "mysql=%s/%s redis=%s/%s" % (_ms.timeout, _ms.connect_timeout,
                                           _rs.timeout, _rs.connect_timeout))

        # 多地址必须共用同一份连接预算：
        # socket.create_connection 会给解析出的每个地址各发一整份 timeout，
        # 双栈机器上 IPv6 不通时，"8 秒"实际要等 16 秒。
        _real_gai = _sk.getaddrinfo

        def _fake_gai(host, port, *_a, **_k):
            if host == "e2e-multi.invalid":
                return [(_sk.AF_INET, _sk.SOCK_STREAM, 6, "",
                         ("10.255.255.%d" % i, port)) for i in (1, 2, 3)]
            return _real_gai(host, port, *_a, **_k)

        _sk.getaddrinfo = _fake_gai
        try:
            _t0 = time.time()
            try:
                _my.MySQLConn("e2e-multi.invalid", 3306, "u", "p",
                              connect_timeout=2.0).connect()
                check("R7c 多地址共享连接预算", False, "竟然连上了")
                _new_el = 0.0
            except _my.DBError as _de:
                _new_el = time.time() - _t0
                check("R7c 多地址共用一份连接预算（3 地址 / 2s 不叠加）",
                      _de.code == "timeout" and _new_el < 3.0,
                      "%s 用时 %.2fs" % (_de.code, _new_el))

            _t1 = time.time()
            try:
                _sk.create_connection(("e2e-multi.invalid", 3306), timeout=2.0)
            except OSError:
                pass
            _old_el = time.time() - _t1
            check("R7d 对照：旧写法确实会按地址数叠加（%.1fs > 4.5s）" % _old_el,
                  _old_el > 4.5, "%.2fs" % _old_el)
        finally:
            _sk.getaddrinfo = _real_gai
except Exception as _exc:
    check("R7 超时边界内部异常", False, repr(_exc))

# 清掉数据库测试目录（里面有配置、审计日志等）
shutil.rmtree(_r_base, ignore_errors=True)


# ---------------------------------------------------------------------------
shutil.rmtree(tmp, ignore_errors=True)

print("\n" + "=" * 60)
print("通过 %d / %d%s" % (len(PASS), len(PASS) + len(FAIL),
                        ("，跳过 %d" % len(SKIP)) if SKIP else ""))
if SKIP:
    print("跳过项（环境不具备，不计入失败）：")
    for s in SKIP:
        print("  - " + s)
if FAIL:
    print("失败项：")
    for f in FAIL:
        print("  - " + f)
print("=" * 60)
raise SystemExit(1 if FAIL else 0)
