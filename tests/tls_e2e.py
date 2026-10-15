# -*- coding: utf-8 -*-
"""真起一个带 TLS 的服务，验证 HTTPS 下 MCP 直连 + 只读 Token + 审计都正常。

需要本机有 openssl（用来现生成一张自签证书）。没有就跳过，返回 0：
这条链路的证书校验部分已由 features_e2e.py 的 M 节覆盖，这里测的是"真跑起来"。

    python tests/tls_e2e.py
"""
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
SERVER = os.path.join(os.path.dirname(HERE), "server")
sys.path.insert(0, SERVER)

BASE = os.path.join(os.path.dirname(HERE), ".build", "tls-run")
SHARE = os.path.join(BASE, "share")
CFG = os.path.join(BASE, "wkfile.config.json")
TOKEN = "t" * 40
RO_TOKEN = "r" * 40
PORT = 18801
PY = sys.executable

if shutil.which("openssl") is None:
    print("跳过：本机没有 openssl，无法生成自签证书。")
    raise SystemExit(0)

shutil.rmtree(BASE, ignore_errors=True)
os.makedirs(SHARE, exist_ok=True)
with open(os.path.join(SHARE, "hello.txt"), "w", encoding="utf-8") as fh:
    fh.write("tls hello\n")

env = dict(os.environ, MSYS_NO_PATHCONV="1")
gen = subprocess.run(
    ["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes",
     "-keyout", os.path.join(BASE, "srv.key"), "-out", os.path.join(BASE, "srv.crt"),
     "-days", "30", "-subj", "/CN=wkfile-test",
     "-addext", "subjectAltName=IP:127.0.0.1"],
    cwd=BASE, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
if gen.returncode != 0:
    print("跳过：openssl 生成证书失败。\n" + gen.stdout.decode("utf-8", "replace")[-400:])
    raise SystemExit(0)

with open(CFG, "w", encoding="utf-8") as fh:
    json.dump({
        "host": "127.0.0.1", "port": PORT, "token": TOKEN, "readonly_token": RO_TOKEN,
        "roots": [SHARE], "tls_cert": os.path.join(BASE, "srv.crt"),
        "tls_key": os.path.join(BASE, "srv.key"), "audit_log": "logs/audit.jsonl",
    }, fh)

log = open(os.path.join(BASE, "srv.log"), "w", encoding="utf-8")
proc = subprocess.Popen([PY, os.path.join(SERVER, "wkfile_agent.py"), "--config", CFG],
                        stdout=log, stderr=subprocess.STDOUT)

import ssl  # noqa: E402

ctx = ssl.create_default_context()
ctx.check_hostname = False
ctx.verify_mode = ssl.CERT_NONE
opener = urllib.request.build_opener(
    urllib.request.ProxyHandler({}), urllib.request.HTTPSHandler(context=ctx))

results = []


def check(name, cond, extra=""):
    results.append((name, bool(cond)))
    print("  [%s] %s%s" % ("OK" if cond else "XX", name, ("  <- " + extra) if extra and not cond else ""))


try:
    up = False
    for _ in range(40):
        try:
            with opener.open("https://127.0.0.1:%d/api/health" % PORT, timeout=2) as r:
                if r.status == 200:
                    up = True
                    break
        except Exception:
            time.sleep(0.4)
    check("HTTPS 服务起来了", up)
    if up:
        with opener.open("https://127.0.0.1:%d/api/health" % PORT, timeout=3) as r:
            body = json.loads(r.read().decode())
        check("健康端点走 TLS 可读", body.get("data", body).get("service") == "wkfile", str(body))

        req = urllib.request.Request(
            "https://127.0.0.1:%d/mcp" % PORT,
            data=json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                             "params": {"protocolVersion": "2025-03-26", "capabilities": {},
                                        "clientInfo": {"name": "t", "version": "1"}}}).encode(),
            headers={"Content-Type": "application/json", "Authorization": "Bearer " + TOKEN},
            method="POST")
        with opener.open(req, timeout=5) as r:
            sid = r.headers.get("Mcp-Session-Id") or ""
            payload = r.read().decode()
        check("MCP initialize 走 HTTPS 成功", "serverInfo" in payload and bool(sid), payload[:120])

        def call(tool, args, token=TOKEN):
            body = json.dumps({"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                               "params": {"name": tool, "arguments": args}}).encode()
            rq = urllib.request.Request(
                "https://127.0.0.1:%d/mcp" % PORT, data=body,
                headers={"Content-Type": "application/json",
                         "Authorization": "Bearer " + token,
                         "Mcp-Session-Id": sid}, method="POST")
            with opener.open(rq, timeout=5) as r:
                return json.loads(r.read().decode())

        res = call("win_read", {"path": os.path.join(SHARE, "hello.txt")})
        txt = res["result"]["content"][0]["text"]
        check("HTTPS 下 win_read 正常", "tls hello" in txt, txt[:120])

        res = call("win_roots", {})
        txt = res["result"]["content"][0]["text"]
        check("主 Token 的 scope 是 full", "只读 Token" not in txt, txt[:200])

        res = call("win_roots", {}, token=RO_TOKEN)
        txt = res["result"]["content"][0]["text"]
        check("只读 Token 能读（win_roots）", "可操作的目录" in txt, txt[:200])
        check("只读 Token 提示自己是只读", "只读 Token" in txt, txt[:200])

        res = call("win_write", {"path": os.path.join(SHARE, "hello.txt"), "content": "x"},
                   token=RO_TOKEN)
        txt = res["result"]["content"][0]["text"]
        check("只读 Token 写被拒", res["result"].get("isError") and "只读" in txt, txt[:200])

        res = call("win_write", {"path": os.path.join(SHARE, "hello.txt"), "content": "y"},
                   token=TOKEN)
        check("主 Token 写正常", not res["result"].get("isError"), str(res)[:200])

    time.sleep(0.4)
    audit = os.path.join(BASE, "logs", "audit.jsonl")
    with open(audit, encoding="utf-8") as fh:
        lines = [json.loads(x) for x in fh if x.strip()]
    ops = {ln.get("op") for ln in lines}
    check("MCP 工具调用已进审计日志", "read" in ops and "write" in ops, str(ops))
    check("只读请求在审计里带 scope 标记",
          any(ln.get("scope") == "readonly" for ln in lines),
          str([ln for ln in lines if ln.get("scope")][:2]))
finally:
    proc.terminate()
    try:
        proc.wait(timeout=6)
    except Exception:
        proc.kill()
    log.close()
    time.sleep(0.2)
    try:
        with open(os.path.join(BASE, "srv.log"), encoding="utf-8") as fh:
            print("\n--- 服务输出 ---\n" + fh.read()[:1500])
    except OSError:
        pass

bad = [n for n, ok in results if not ok]
print("\n通过 %d / %d" % (len(results) - len(bad), len(results)))
if bad:
    print("失败：", bad)
sys.exit(1 if bad else 0)
