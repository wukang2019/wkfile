# -*- coding: utf-8 -*-
"""调用服务器 MCP 工具一次（验证用）

用法：
    python mcp-call-test.py <base_url> <token> <tool_name> [args_json]
示例：
    python mcp-call-test.py http://1.2.3.4:10087 TOKEN win_roots
    python mcp-call-test.py http://1.2.3.4:10087 TOKEN win_list "{\"path\":\"D:\\\\share\"}"
"""
import json
import sys
import urllib.error
import urllib.request

BASE = sys.argv[1]
TOKEN = sys.argv[2]
TOOL = sys.argv[3]
ARGS = json.loads(sys.argv[4]) if len(sys.argv) > 4 else {}
OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def rpc(method, params=None, sid=None, timeout=30):
    body = {"jsonrpc": "2.0", "id": 1, "method": method}
    if params is not None:
        body["params"] = params
    req = urllib.request.Request(
        BASE.rstrip("/") + "/mcp", data=json.dumps(body).encode("utf-8"), method="POST"
    )
    req.add_header("Content-Type", "application/json")
    req.add_header("Accept", "application/json, text/event-stream")
    req.add_header("Authorization", "Bearer " + TOKEN)
    if sid:
        req.add_header("Mcp-Session-Id", sid)
    with OPENER.open(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8", "replace")), r.headers.get("Mcp-Session-Id", "")


def text_of(result):
    parts = []
    for item in (result or {}).get("content", []):
        if item.get("type") == "text":
            parts.append(item.get("text", ""))
    return "\n".join(parts)


print("=" * 58)
print("  调用工具: %s" % TOOL)
print("  参数: %s" % json.dumps(ARGS, ensure_ascii=False))
print("=" * 58)

try:
    _, sid = rpc(
        "initialize",
        {
            "protocolVersion": "2025-06-18",
            "capabilities": {},
            "clientInfo": {"name": "probe", "version": "1.0"},
        },
    )
    payload, _ = rpc("tools/call", {"name": TOOL, "arguments": ARGS}, sid=sid)
except urllib.error.HTTPError as exc:
    print("HTTP %s: %s" % (exc.code, exc.read().decode("utf-8", "replace")[:300]))
    sys.exit(1)
except Exception as exc:  # noqa: BLE001
    print("调用失败: %s" % exc)
    sys.exit(1)

if "error" in payload:
    print("协议错误: " + json.dumps(payload["error"], ensure_ascii=False))
    sys.exit(1)

result = payload.get("result", {})
if result.get("isError"):
    print("[工具返回错误]")
print(text_of(result)[:3000])
print("=" * 58)
