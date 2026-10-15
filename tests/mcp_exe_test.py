#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""用编译好的 exe 验证 MCP 端点：确认协议模块已打进 exe，链路完整可用"""

import json
import os
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _read_version() -> str:
    """从源码读版本号 —— 不写死，否则每次升版本这个脚本都得改。"""
    import re
    p = os.path.join(BASE, "server", "wkfile_agent.py")
    with open(p, "r", encoding="utf-8") as fh:
        m = re.search(r'^VERSION\s*=\s*"([^"]+)"', fh.read(), re.M)
    return m.group(1) if m else "?"


EXPECT_VER = _read_version()
EXPECT_TOOLS = 33


def _find_exe():
    if len(sys.argv) > 1:
        return sys.argv[1]
    for cand in (
        os.path.join(BASE, "dist", "wkfile.exe"),
        os.path.join(BASE, ".build", "dist-new", "wkfile.exe"),
    ):
        if os.path.isfile(cand):
            return cand
    return os.path.join(BASE, "dist", "wkfile.exe")


EXE = _find_exe()
WORK = os.path.join(BASE, ".build", "mcpexetest")
PORT = 8799
TOKEN = "exe-mcp-token-77c1"
OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    msg = f"  {'[OK]  ' if cond else '[FAIL]'} {name}"
    if detail and not cond:
        msg += f"  :: {detail}"
    print(msg)


def post(path, body, token=TOKEN, headers=None):
    url = f"http://127.0.0.1:{PORT}{path}"
    data = json.dumps(body, ensure_ascii=False).encode("utf-8")
    hdrs = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream"}
    if token:
        hdrs["Authorization"] = f"Bearer {token}"
    hdrs.update(headers or {})
    req = urllib.request.Request(url, data=data, headers=hdrs, method="POST")
    try:
        with OPENER.open(req, timeout=25) as r:
            return r.status, dict(r.headers), r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers), e.read().decode("utf-8", "replace")


def get(path, token=TOKEN):
    url = f"http://127.0.0.1:{PORT}{path}"
    hdrs = {"Accept": "application/json"}
    if token:
        hdrs["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, headers=hdrs, method="GET")
    try:
        with OPENER.open(req, timeout=25) as r:
            return r.status, dict(r.headers), r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers), e.read().decode("utf-8", "replace")


def main():
    if not os.path.isfile(EXE):
        print(f"[X] 找不到 exe: {EXE}")
        return 1

    shutil.rmtree(WORK, ignore_errors=True)
    os.makedirs(WORK, exist_ok=True)
    share = os.path.join(WORK, "share")
    os.makedirs(share, exist_ok=True)
    with open(os.path.join(share, "app.log"), "w", encoding="utf-8") as fh:
        fh.write("INFO ready\nERROR disk full\n")

    cfg = {
        "host": "127.0.0.1",
        "port": PORT,
        "token": TOKEN,
        "roots": [share],
        "ip_allowlist": ["127.0.0.1"],
        "enable_mcp": True,
        "audit_log": os.path.join(WORK, "audit.jsonl"),
    }
    cfg_path = os.path.join(WORK, "wkfile.config.json")
    with open(cfg_path, "w", encoding="utf-8") as fh:
        json.dump(cfg, fh, ensure_ascii=False, indent=2)

    print(f"启动 exe（首次运行可能被杀软扫描，耐心等）…\n  {EXE}")
    flags = 0
    if hasattr(subprocess, "CREATE_NO_WINDOW"):
        flags = subprocess.CREATE_NO_WINDOW
    proc = subprocess.Popen(
        [EXE, "--run", "--config", cfg_path],
        cwd=WORK, creationflags=flags,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )

    ready = False
    t0 = time.time()
    while time.time() - t0 < 90:
        try:
            st, _, _ = get("/api/health", token=None)
            if st in (200, 401):
                ready = True
                break
        except Exception:
            pass
        time.sleep(1)
    print(f"  服务就绪: {ready}  ({time.time() - t0:.1f}s)\n")
    if not ready:
        print("[X] 服务没能起来")
        proc.kill()
        return 1

    try:
        print("========== 1. exe 的 health / 状态接口 ==========")
        st, _, body = get("/health", token=None)
        h = json.loads(body).get("data") or {}
        check("安全：/health 免鉴权且只回最小信息",
              st == 200 and set(h) == {"service", "version"}, f"status={st} keys={sorted(h)}")

        st, _, _b = get("/", token=None)
        check("安全：GET / 不带 Token 被拒（401）", st == 401, f"status={st}")

        st, _, body = get("/", token=TOKEN)
        h = json.loads(body).get("data") or {}
        check("GET / 带 Token 返回详情", st == 200, f"status={st}")
        check(f"版本与源码一致（{EXPECT_VER}）", h.get("version") == EXPECT_VER, str(h.get("version")))
        check("MCP 已启用", h.get("mcp", {}).get("enabled") is True)
        check(f"MCP 注册了 {EXPECT_TOOLS} 个工具", h.get("mcp", {}).get("tools") == EXPECT_TOOLS, str(h.get("mcp")))
        check("exe 的写入无痕：tools_disable 为空",
              h.get("mcp", {}).get("tools_disabled") == [], str(h.get("mcp")))

        print("\n========== 2. exe 的 /mcp initialize ==========")
        st, hdrs, body = post("/mcp", {
            "jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {"protocolVersion": "2025-03-26", "capabilities": {},
                       "clientInfo": {"name": "exe-e2e", "version": "1"}},
        })
        obj = json.loads(body)
        sid = hdrs.get("Mcp-Session-Id")
        check("initialize 200", st == 200, f"{st} {body[:200]}")
        check("下发会话 ID", bool(sid))
        check("serverInfo 正常", obj.get("result", {}).get("serverInfo", {}).get("name") == "wkfile-mcp")

        print("\n========== 3. exe 的 tools/list ==========")
        st, _, body = post("/mcp", {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
                           headers={"Mcp-Session-Id": sid})
        tools = json.loads(body)["result"]["tools"]
        check(f"{EXPECT_TOOLS} 个工具", len(tools) == EXPECT_TOOLS, str(len(tools)))
        check("含 win_grep", any(t["name"] == "win_grep" for t in tools))
        check("每个工具都带 annotations（打包后也没丢）",
              all(set(t.get("annotations", {})) == {"readOnlyHint", "destructiveHint",
                                                    "idempotentHint", "openWorldHint"}
                  for t in tools))
        check("win_read 标为只读", next(t for t in tools if t["name"] == "win_read")
              ["annotations"]["readOnlyHint"] is True)
        check("win_trash 标为破坏性", next(t for t in tools if t["name"] == "win_trash")
              ["annotations"]["destructiveHint"] is True)

        print("\n========== 4. exe 的 tools/call（真实读写） ==========")
        st, _, body = post("/mcp", {"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                                    "params": {"name": "win_list", "arguments": {"path": share}}},
                           headers={"Mcp-Session-Id": sid})
        r = json.loads(body)["result"]
        txt = " ".join(i.get("text", "") for i in r["content"])
        check("win_list 看到 app.log", r["isError"] is False and "app.log" in txt, txt[:200])

        st, _, body = post("/mcp", {"jsonrpc": "2.0", "id": 4, "method": "tools/call",
                                    "params": {"name": "win_write",
                                               "arguments": {"path": os.path.join(share, "x.txt"),
                                                             "content": "written via mcp on exe\n"}}},
                           headers={"Mcp-Session-Id": sid})
        check("win_write 成功", json.loads(body)["result"]["isError"] is False, body[:200])

        st, _, body = post("/mcp", {"jsonrpc": "2.0", "id": 5, "method": "tools/call",
                                    "params": {"name": "win_grep", "arguments": {"regex": "ERROR", "root": share}}},
                           headers={"Mcp-Session-Id": sid})
        txt = " ".join(i.get("text", "") for i in json.loads(body)["result"]["content"])
        check("win_grep 命中", "disk full" in txt, txt[:200])

        st, _, body = post("/mcp", {"jsonrpc": "2.0", "id": 6, "method": "tools/call",
                                    "params": {"name": "win_read",
                                               "arguments": {"path": r"C:\Windows\win.ini"}}},
                           headers={"Mcp-Session-Id": sid})
        txt = " ".join(i.get("text", "") for i in json.loads(body)["result"]["content"])
        check("越权路径仍被拦截", "out_of_scope" in txt, txt[:200])

        print("\n========== 5. exe 的鉴权 ==========")
        st, _, _ = post("/mcp", {"jsonrpc": "2.0", "id": 7, "method": "tools/list", "params": {}}, token=None)
        check("无 Token 被拒 401", st == 401, str(st))

        print("\n========== 6. exe 的其余接口未受影响 ==========")
        st, _, body = post("/api/list", {"path": share})
        check("老 /api/list 仍可用", st == 200 and json.loads(body).get("ok") is True, body[:160])

        print("\n========== 7. 写盘保护仍然生效 ==========")
        # 备份不再往被操作的业务目录里塞，统一落在数据目录的 backups\ 下
        # （base_dir 就是 --config 所在目录，也就是 WORK）
        bk = os.path.join(WORK, "backups")
        post("/mcp", {"jsonrpc": "2.0", "id": 8, "method": "tools/call",
                      "params": {"name": "win_write",
                                 "arguments": {"path": os.path.join(share, "x.txt"), "content": "second write\n"}}},
             headers={"Mcp-Session-Id": sid})
        check("覆盖前自动备份目录已生成", os.path.isdir(bk), bk)

    finally:
        proc.terminate()
        try:
            proc.wait(timeout=8)
        except Exception:
            proc.kill()

    print("\n" + "=" * 62)
    print(f"通过 {len(PASS)} 项，失败 {len(FAIL)} 项")
    for f in FAIL:
        print("  - 失败:", f)
    print("=" * 62)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
