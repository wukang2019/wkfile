#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""MCP 端点端到端测试：Streamable HTTP + 旧版 SSE 传输 + 鉴权 + 越权拦截"""

import json
import os
import shutil
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(BASE, "server"))

import wkfile_agent as wa  # noqa: E402

HOST = "127.0.0.1"
PORT = 8815
TOKEN = "mcp-e2e-token-9f3a"
OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(f"  {'[OK]  ' if cond else '[FAIL]'} {name}" + (f"  :: {detail}" if detail and not cond else ""))


def post(path, body, token=TOKEN, headers=None, raw_body=None):
    url = f"http://{HOST}:{PORT}{path}"
    data = raw_body if raw_body is not None else json.dumps(body, ensure_ascii=False).encode("utf-8")
    hdrs = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream"}
    if token:
        hdrs["Authorization"] = f"Bearer {token}"
    hdrs.update(headers or {})
    req = urllib.request.Request(url, data=data, headers=hdrs, method="POST")
    try:
        with OPENER.open(req, timeout=20) as r:
            return r.status, dict(r.headers), r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers), e.read().decode("utf-8", "replace")


def get(path, token=TOKEN, headers=None):
    url = f"http://{HOST}:{PORT}{path}"
    hdrs = {"Accept": "application/json"}
    if token:
        hdrs["Authorization"] = f"Bearer {token}"
    hdrs.update(headers or {})
    req = urllib.request.Request(url, headers=hdrs, method="GET")
    try:
        with OPENER.open(req, timeout=20) as r:
            return r.status, dict(r.headers), r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers), e.read().decode("utf-8", "replace")


def jrpc(method, params=None, msg_id=1):
    m = {"jsonrpc": "2.0", "id": msg_id, "method": method}
    if params is not None:
        m["params"] = params
    return m


def call_tool(name, args, sid=None, msg_id=9):
    hdrs = {"Mcp-Session-Id": sid} if sid else None
    return post("/mcp", jrpc("tools/call", {"name": name, "arguments": args}, msg_id), headers=hdrs)


def tool_text(body):
    try:
        obj = json.loads(body)
    except Exception:
        return ""
    res = obj.get("result") or {}
    items = res.get("content") or []
    return " ".join(i.get("text", "") for i in items if isinstance(i, dict))


def main():
    tmp = tempfile.mkdtemp(prefix="mcp-e2e-")
    share = os.path.join(tmp, "share")
    os.makedirs(share, exist_ok=True)
    with open(os.path.join(share, "app.log"), "w", encoding="utf-8") as fh:
        fh.write("INFO boot ok\nERROR db timeout\nINFO retry ok\n")
    with open(os.path.join(share, "seed.txt"), "w", encoding="utf-8") as fh:
        fh.write("line one\nline two\n")

    cfg_path = os.path.join(tmp, "wkfile.config.json")
    cfg_data = {
        "host": HOST,
        "port": PORT,
        "token": TOKEN,
        "roots": [share],
        "ip_allowlist": ["127.0.0.1"],
        "audit_log": os.path.join(tmp, "audit.jsonl"),
        "enable_mcp": True,
    }
    with open(cfg_path, "w", encoding="utf-8") as fh:
        json.dump(cfg_data, fh, ensure_ascii=False)

    cfg = wa.load_config(cfg_path)
    agent = wa.Agent(cfg)
    wa.Handler.agent = agent
    httpd = wa.ThreadingHTTPServer((HOST, PORT), wa.Handler)
    httpd.daemon_threads = True
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    time.sleep(0.8)

    print("\n========== 1. 健康检查与状态接口 ==========")
    st, _, body = get("/health", token=None)
    h = json.loads(body)
    hd = h.get("data") or {}
    check("安全：/health 免鉴权可访问", st == 200, f"status={st}")
    check("安全：/health 只回最小信息（service + version）",
          set(hd) == {"service", "version"}, str(sorted(hd)))
    check("安全：/health 不含主机名", "host" not in hd, str(hd))
    check("安全：/health 不含 Python 版本", "python" not in hd, str(hd))

    st, _, _b = get("/", token=None)
    check("安全：GET / 不带 Token 被拒（401，不再泄露详情）", st == 401, f"status={st}")

    st, _, body = get("/", token=TOKEN)
    d = json.loads(body).get("data") or {}
    check("GET / 带 Token 返回详细状态", st == 200, f"status={st}")
    check("详细状态报告 mcp.enabled", d.get("mcp", {}).get("enabled") is True)
    check("详细状态报告 33 个工具", d.get("mcp", {}).get("tools") == 33, str(d.get("mcp")))
    check("详细状态版本与源码常量一致", d.get("version") == wa.VERSION, str(d.get("version")))

    print("\n========== 2. Streamable HTTP：initialize ==========")
    st, hdrs, body = post("/mcp", jrpc("initialize", {
        "protocolVersion": "2025-03-26",
        "capabilities": {},
        "clientInfo": {"name": "e2e", "version": "1"},
    }))
    obj = json.loads(body)
    sid = hdrs.get("Mcp-Session-Id") or hdrs.get("mcp-session-id")
    check("initialize 200", st == 200, f"status={st} body={body[:200]}")
    check("下发 Mcp-Session-Id", bool(sid), str(list(hdrs.keys())))
    check("协议版本协商为 2025-03-26", obj.get("result", {}).get("protocolVersion") == "2025-03-26")
    check("serverInfo.name = winfile-mcp", obj.get("result", {}).get("serverInfo", {}).get("name") == "wkfile-mcp")
    check("capabilities.tools 已声明", "tools" in obj.get("result", {}).get("capabilities", {}))
    check("instructions 里带出可操作目录", share in obj.get("result", {}).get("instructions", ""))

    print("\n========== 3. 通知：notifications/initialized ==========")
    st, _, body = post("/mcp", {"jsonrpc": "2.0", "method": "notifications/initialized"})
    check("通知返回 202 且无 body", st == 202 and body == "", f"status={st} body={body[:80]!r}")

    print("\n========== 4. tools/list ==========")
    st, _, body = post("/mcp", jrpc("tools/list", {}, 2), headers={"Mcp-Session-Id": sid})
    tools = json.loads(body)["result"]["tools"]
    names = [t["name"] for t in tools]
    check("返回 33 个工具", len(tools) == 33, str(len(tools)))
    check("含 win_read / win_write / win_grep",
          all(n in names for n in ("win_read", "win_write", "win_grep")))
    check("每个工具都有 inputSchema", all("inputSchema" in t for t in tools))

    print("\n========== 5. tools/call 正常链路 ==========")
    st, _, body = call_tool("win_roots", {}, sid)
    check("win_roots 成功", st == 200 and json.loads(body)["result"]["isError"] is False, body[:200])
    check("win_roots 列出白名单目录", share in tool_text(body))

    st, _, body = call_tool("win_list", {"path": share}, sid)
    check("win_list 成功", json.loads(body)["result"]["isError"] is False, body[:200])
    check("win_list 看到 app.log", "app.log" in tool_text(body))

    st, _, body = call_tool("win_write", {"path": os.path.join(share, "new.txt"), "content": "hello from mcp\n"}, sid)
    check("win_write 成功", json.loads(body)["result"]["isError"] is False, body[:300])

    st, _, body = call_tool("win_read", {"path": os.path.join(share, "new.txt")}, sid)
    check("win_read 读回内容", "hello from mcp" in tool_text(body), body[:300])

    st, _, body = call_tool("win_replace", {"path": os.path.join(share, "new.txt"), "old": "mcp", "new": "MCP"}, sid)
    check("win_replace 成功", json.loads(body)["result"]["isError"] is False, body[:300])
    st, _, body = call_tool("win_read", {"path": os.path.join(share, "new.txt")}, sid)
    check("win_replace 生效", "hello from MCP" in tool_text(body), body[:300])

    st, _, body = call_tool("win_grep", {"regex": "ERROR", "root": share}, sid)
    check("win_grep 命中日志", "db timeout" in tool_text(body), body[:300])

    st, _, body = call_tool("win_disk", {}, sid)
    check("win_disk 成功", json.loads(body)["result"]["isError"] is False, body[:200])

    print("\n========== 6. 错误路径 ==========")
    st, _, body = call_tool("win_no_such_tool", {}, sid)
    r = json.loads(body)["result"]
    check("未知工具 -> isError（不是断连）", r.get("isError") is True and "未知工具" in tool_text(body))

    st, _, body = call_tool("win_read", {"path": r"C:\Windows\win.ini"}, sid)
    r = json.loads(body)["result"]
    check("越权路径被拒且提示 out_of_scope",
          r.get("isError") is True and "out_of_scope" in tool_text(body), tool_text(body)[:200])

    st, _, body = post("/mcp", jrpc("no/such/method", {}, 3), headers={"Mcp-Session-Id": sid})
    check("未知方法返回 -32601", json.loads(body).get("error", {}).get("code") == -32601, body[:160])

    st, _, body = post("/mcp", None, raw_body=b"{not json", headers={"Mcp-Session-Id": sid})
    check("坏 JSON 返回 400 + -32700", st == 400 and json.loads(body)["error"]["code"] == -32700, f"{st} {body[:120]}")

    print("\n========== 7. 鉴权与会话 ==========")
    st, _, body = post("/mcp", jrpc("tools/list", {}, 4), token=None)
    check("无 Token -> 401", st == 401, f"status={st}")
    st, _, body = post("/mcp", jrpc("tools/list", {}, 5), token="wrong-token")
    check("错 Token -> 401", st == 401, f"status={st}")
    st, _, body = post("/mcp", jrpc("tools/list", {}, 6), headers={"Mcp-Session-Id": "bogus-session"})
    check("无效会话 -> 404", st == 404, f"status={st}")

    print("\n========== 8. 响应格式与 GET 探测 ==========")
    st, hdrs, body = post("/mcp", jrpc("tools/list", {}, 7),
                          headers={"Mcp-Session-Id": sid, "Accept": "text/event-stream"})
    ct = (hdrs.get("Content-Type") or "").lower()
    check("只接受 SSE 时返回 text/event-stream", st == 200 and "text/event-stream" in ct, ct)
    check("SSE 体是 data: 行", body.strip().startswith("event: message"), body[:80])

    st, _, body = get("/mcp")
    check("GET /mcp -> 405", st == 405, f"status={st}")

    print("\n========== 9. 批量 JSON-RPC ==========")
    st, _, body = post("/mcp", [jrpc("tools/list", {}, 11), jrpc("ping", {}, 12)],
                       headers={"Mcp-Session-Id": sid})
    arr = json.loads(body)
    check("批量返回 2 条响应", isinstance(arr, list) and len(arr) == 2, body[:160])

    print("\n========== 10. 旧版 HTTP+SSE 传输 ==========")
    try:
        req = urllib.request.Request(
            f"http://{HOST}:{PORT}/sse",
            headers={"Authorization": f"Bearer {TOKEN}", "Accept": "text/event-stream"},
            method="GET",
        )
        resp = OPENER.open(req, timeout=15)
        ep_line, data_line = resp.readline().decode(), resp.readline().decode()
        check("SSE 首事件是 endpoint", ep_line.strip() == "event: endpoint", ep_line.strip())
        endpoint = data_line.strip().replace("data:", "").strip()
        check("endpoint 带 sessionId", "sessionId=" in endpoint, endpoint)

        st, _, b2 = post(endpoint, jrpc("initialize", {"protocolVersion": "2024-11-05",
                                                       "capabilities": {},
                                                       "clientInfo": {"name": "sse", "version": "1"}}))
        check("POST /messages -> 202", st == 202, f"status={st}")

        got = None
        deadline = time.time() + 10
        while time.time() < deadline:
            line = resp.readline().decode()
            if line.startswith("data:"):
                got = json.loads(line[5:].strip())
                break
        check("经 SSE 流收到 initialize 响应",
              bool(got) and got.get("result", {}).get("serverInfo", {}).get("name") == "wkfile-mcp",
              str(got)[:200])

        st, _, b3 = post(endpoint, {"jsonrpc": "2.0", "id": 21, "method": "tools/call",
                                    "params": {"name": "win_read",
                                               "arguments": {"path": os.path.join(share, "seed.txt")}}})
        got2 = None
        deadline = time.time() + 10
        while time.time() < deadline:
            line = resp.readline().decode()
            if line.startswith("data:"):
                got2 = json.loads(line[5:].strip())
                break
        txt = " ".join(i.get("text", "") for i in (got2 or {}).get("result", {}).get("content", []))
        check("经 SSE 流完成 tools/call", "line one" in txt, txt[:160])
        resp.close()
    except Exception as exc:  # noqa: BLE001
        check("SSE 传输整体可用", False, f"{exc!r}")

    print("\n========== 11. 审计日志 ==========")
    time.sleep(0.3)
    try:
        with open(cfg_data["audit_log"], encoding="utf-8") as fh:
            lines = [json.loads(x) for x in fh if x.strip()]
        check("审计日志有记录", len(lines) > 0, str(len(lines)))
    except Exception as exc:  # noqa: BLE001
        check("审计日志有记录", False, repr(exc))

    print("\n========== 12. 关闭 MCP 开关 ==========")
    agent.cfg.enable_mcp = False
    st, _, body = post("/mcp", jrpc("tools/list", {}, 31), headers={"Mcp-Session-Id": sid})
    check("enable_mcp=false 时 /mcp 返回 404", st == 404, f"status={st}")
    st, _, body = get("/", token=TOKEN)
    check("详细状态反映 mcp 已关闭",
          (json.loads(body).get("data") or {}).get("mcp", {}).get("enabled") is False)
    agent.cfg.enable_mcp = True

    httpd.shutdown()
    shutil.rmtree(tmp, ignore_errors=True)

    print("\n" + "=" * 62)
    print(f"通过 {len(PASS)} 项，失败 {len(FAIL)} 项")
    if FAIL:
        print("失败清单：")
        for f in FAIL:
            print("  -", f)
    print("=" * 62)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
