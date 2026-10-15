"""本机端到端：启 py 版服务 -> 走 MCP 端点 -> 读/写/搜索 -> 停止。"""
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.request

PY = sys.executable
BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WORK = os.path.join(BASE, ".build", "localrun")
SHARE = os.path.join(WORK, "share")
PORT = 8790
TOKEN = "local-test-token-xyz"

shutil.rmtree(WORK, ignore_errors=True)
os.makedirs(SHARE, exist_ok=True)
with open(os.path.join(SHARE, "demo.txt"), "w", encoding="utf-8") as fh:
    fh.write("hello winfile\nport = 8080\nline three\n")

cfg = {
    "host": "127.0.0.1",
    "port": PORT,
    "token": TOKEN,
    "roots": [SHARE],
    "read_only": False,
    "allow_delete": True,
    "auto_backup": True,
    "enable_mcp": True,
    "ip_allowlist": ["127.0.0.1"],
    "audit_log": os.path.join(WORK, "audit.jsonl"),
}
cfg_path = os.path.join(WORK, "wkfile.config.json")
with open(cfg_path, "w", encoding="utf-8") as fh:
    json.dump(cfg, fh, ensure_ascii=False, indent=2)

log = open(os.path.join(WORK, "server.log"), "w", encoding="utf-8")
proc = subprocess.Popen([PY, os.path.join(BASE, "server", "wkfile_agent.py"),
                         "--config", cfg_path], stdout=log, stderr=subprocess.STDOUT)

results = []
def check(name, ok, detail=""):
    results.append((name, ok, detail))
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f"   {detail}" if detail and not ok else ""))

def post(path, payload, session=None, method="POST"):
    req = urllib.request.Request(f"http://127.0.0.1:{PORT}{path}",
                                data=json.dumps(payload).encode("utf-8"),
                                headers={"Content-Type": "application/json",
                                         "Authorization": f"Bearer {TOKEN}"},
                                method=method)
    if session:
        req.add_header("Mcp-Session-Id", session)
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(req, timeout=20) as resp:
        body = resp.read().decode("utf-8")
        sid = resp.headers.get("Mcp-Session-Id")
        return json.loads(body) if body.strip() else None, sid

try:
    # 等服务就绪
    up = False
    for _ in range(40):
        try:
            with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(
                    f"http://127.0.0.1:{PORT}/api/health", timeout=2) as r:
                if r.status == 200:
                    up = True
                    break
        except Exception:
            time.sleep(0.5)
    check("服务启动并健康检查通过", up)
    if not up:
        raise SystemExit(1)

    init, sid = post("/mcp", {"jsonrpc": "2.0", "id": 1, "method": "initialize",
                              "params": {"protocolVersion": "2024-11-05", "capabilities": {},
                                         "clientInfo": {"name": "local-e2e", "version": "1"}}})
    check("MCP initialize 返回服务信息", bool(init and init.get("result", {}).get("serverInfo")),
          json.dumps(init)[:200])
    check("MCP 下发会话 ID", bool(sid), str(sid))

    tl, _ = post("/mcp", {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}}, session=sid)
    tools = (tl or {}).get("result", {}).get("tools", [])
    check(f"tools/list 返回工具（{len(tools)} 个）", len(tools) >= 18, str(len(tools)))

    def call(name, args, rid):
        r, _ = post("/mcp", {"jsonrpc": "2.0", "id": rid, "method": "tools/call",
                             "params": {"name": name, "arguments": args}}, session=sid)
        return r

    r = call("win_list", {"path": SHARE}, 3)
    txt = json.dumps(r, ensure_ascii=False)
    check("win_list 列出目录", "demo.txt" in txt, txt[:250])

    r = call("win_read", {"path": os.path.join(SHARE, "demo.txt")}, 4)
    txt = json.dumps(r, ensure_ascii=False)
    check("win_read 读到内容", "hello winfile" in txt, txt[:250])

    r = call("win_write", {"path": os.path.join(SHARE, "new.txt"), "content": "written locally"}, 5)
    txt = json.dumps(r, ensure_ascii=False)
    check("win_write 写入新文件", os.path.isfile(os.path.join(SHARE, "new.txt")), txt[:250])

    r = call("win_replace", {"path": os.path.join(SHARE, "demo.txt"),
                             "old": "port = 8080", "new": "port = 9090"}, 6)
    txt = json.dumps(r, ensure_ascii=False)
    after = open(os.path.join(SHARE, "demo.txt"), encoding="utf-8").read()
    check("win_replace 替换生效", "port = 9090" in after, txt[:200] + " | 文件: " + after[:80])
    check("覆盖前自动备份",
          os.path.isdir(os.path.join(WORK, "backups")) and
          any(files for _, _, files in os.walk(os.path.join(WORK, "backups"))))

    r = call("win_grep", {"regex": "port = ", "root": SHARE}, 7)
    txt = json.dumps(r, ensure_ascii=False)
    check("win_grep 搜索命中", "9090" in txt, txt[:250])

    r = call("win_list", {"path": "C:\\Windows"}, 8)
    txt = json.dumps(r, ensure_ascii=False)
    check("越权路径被拒绝", ("out_of_scope" in txt) or ("越界" in txt) or ("不允许" in txt), txt[:250])

    # 无 token 应 401
    try:
        req = urllib.request.Request(f"http://127.0.0.1:{PORT}/mcp",
                                     data=b'{"jsonrpc":"2.0","id":9,"method":"tools/list"}',
                                     headers={"Content-Type": "application/json"})
        urllib.request.build_opener(urllib.request.ProxyHandler({})).open(req, timeout=10)
        check("无 Token 被拒", False, "居然通过了")
    except urllib.error.HTTPError as exc:
        check("无 Token 被拒", exc.code == 401, f"HTTP {exc.code}")

    check("审计日志已落盘", os.path.isfile(os.path.join(WORK, "audit.jsonl")))

finally:
    proc.terminate()
    try:
        proc.wait(timeout=6)
    except Exception:
        proc.kill()
    log.close()

okn = sum(1 for _, ok, _ in results if ok)
print("\n" + "=" * 66)
print(f" 本机端到端结果：{okn}/{len(results)} 通过")
fails = [n for n, ok, _ in results if not ok]
if fails:
    print(" 失败项：" + "、".join(fails))
print("=" * 66)
print(f"测试目录保留在：{WORK}")
sys.exit(0 if not fails else 1)
