# -*- coding: utf-8 -*-
"""从开发机验证服务器公网 MCP 端点是否真的可用"""
import json
import sys
import urllib.error
import urllib.request

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://1.2.3.4:10087"
TOKEN = sys.argv[2] if len(sys.argv) > 2 else ""
OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def rpc(method, params=None, sid=None, expect_reply=True, timeout=15):
    body = {"jsonrpc": "2.0", "method": method}
    if expect_reply:
        body["id"] = 1
    if params is not None:
        body["params"] = params
    req = urllib.request.Request(
        BASE + "/mcp", data=json.dumps(body).encode("utf-8"), method="POST"
    )
    req.add_header("Content-Type", "application/json")
    req.add_header("Accept", "application/json, text/event-stream")
    if TOKEN:
        req.add_header("Authorization", "Bearer " + TOKEN)
    if sid:
        req.add_header("Mcp-Session-Id", sid)
    try:
        with OPENER.open(req, timeout=timeout) as r:
            return r.status, r.read().decode("utf-8", "replace"), r.headers.get("Mcp-Session-Id", "")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace"), ""
    except Exception as e:  # noqa: BLE001
        return None, str(e), ""


def parse(text):
    text = (text or "").strip()
    if not text:
        return None
    if text.startswith("data:") or "\ndata:" in text:
        for line in text.splitlines():
            if line.strip().startswith("data:"):
                try:
                    return json.loads(line.strip()[5:].strip())
                except Exception:  # noqa: BLE001
                    continue
        return None
    try:
        return json.loads(text)
    except Exception:  # noqa: BLE001
        return None


print("=" * 58)
print("  MCP 端点验证   " + BASE + "/mcp")
print("  Token: " + ("已提供 (%d 字符)" % len(TOKEN) if TOKEN else "未提供"))
print("=" * 58)

st, txt, sid = rpc(
    "initialize",
    {
        "protocolVersion": "2025-06-18",
        "capabilities": {},
        "clientInfo": {"name": "probe", "version": "1.0"},
    },
)
print("\n[1] initialize -> HTTP %s" % st)
payload = parse(txt)
if st == 200 and isinstance(payload, dict) and "result" in payload:
    info = payload["result"].get("serverInfo", {})
    print("    serverInfo : %s v%s" % (info.get("name"), info.get("version")))
    print("    session    : " + (sid[:12] + "..." if sid else "未下发"))
    OK = True
elif st == 401:
    print("    401 —— 需要正确的 Token（从服务器界面上「复制 MCP 配置」里取）")
    OK = False
else:
    print("    原始响应: " + str(txt)[:220])
    OK = False

if OK:
    st, txt, _ = rpc("tools/list", {}, sid=sid)
    payload = parse(txt)
    tools = []
    if isinstance(payload, dict) and "result" in payload:
        tools = payload["result"].get("tools", [])
    print("\n[2] tools/list -> HTTP %s，共 %d 个工具" % (st, len(tools)))
    for t in tools:
        print("      - %s" % t.get("name"))
    if tools:
        print("\n    结论：公网 MCP 端点完全可用，MCP 客户端可以直连。")

print("\n" + "=" * 58)
