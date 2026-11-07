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
import urllib.parse
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
# 黑盒期望值：TOOLS 里全部工具（本测试的配置把 exec_enabled 打开）。
# 和源码里的 TOOLS 数量保持同步；新增工具时记得同步改这里。
EXPECT_TOOLS = 37


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


def post_raw(path, data: bytes, token=TOKEN):
    """原始字节直传（/api/upload 用）：请求体不是 JSON。"""
    url = f"http://127.0.0.1:{PORT}{path}"
    hdrs = {"Content-Type": "application/octet-stream"}
    if token:
        hdrs["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, data=data, headers=hdrs, method="POST")
    try:
        with OPENER.open(req, timeout=60) as r:
            return r.status, r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")


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
        # exe 端到端验证 win_exec（PyInstaller 冻结环境里 subprocess 行为必须实测）
        "exec_enabled": True,
        "exec_allowed_commands": ["echo", "findstr", "type"],
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
        check("exe 的写入无痕：tools_disable 为空（exec 已开启）",
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
        ver = obj.get("result", {}).get("serverInfo", {}).get("version", "")
        ins = obj.get("result", {}).get("instructions", "")
        check("exe 握手下发运行时契约", "【运行时契约" in ins, ins[-200:])
        check("契约版本与 serverInfo 一致", bool(ver) and ver in ins)
        check("契约带出实际读写上限与 exec 状态",
              "单次读" in ins and "win_exec" in ins)

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

        # win_exec 端到端：exe（PyInstaller 冻结环境）里的 subprocess 必须实测
        st, _, body = post("/mcp", {"jsonrpc": "2.0", "id": 9, "method": "tools/call",
                                    "params": {"name": "win_exec",
                                               "arguments": {"command": "echo exec-from-exe"}}},
                           headers={"Mcp-Session-Id": sid})
        r = json.loads(body)["result"]
        txt = " ".join(i.get("text", "") for i in r["content"])
        check("win_exec 在 exe 里真实执行（echo 输出回传）",
              r["isError"] is False and "exec-from-exe" in txt, txt[:200])

        st, _, body = post("/mcp", {"jsonrpc": "2.0", "id": 10, "method": "tools/call",
                                    "params": {"name": "win_exec",
                                               "arguments": {"command": "whoami"}}},
                           headers={"Mcp-Session-Id": sid})
        r = json.loads(body)["result"]
        txt = " ".join(i.get("text", "") for i in r["content"])
        check("win_exec 白名单外命令（whoami）被拒",
              r["isError"] is True and "exec_not_allowed" in txt, txt[:200])

        # v1.7.8：白名单只查首程序，复合命令交给 cmd（PyInstaller 冻结环境实测）
        for cmd, needle, name in (
            ("echo exe-chain-a && echo exe-chain-b", "exe-chain-b", "win_exec 复合命令 && 不再拆链误拒"),
            ("echo exe-redir 2>&1",                  "exe-redir",   "win_exec 重定向 2>&1 不再拆出 1 误拒"),
            ('echo "exe quoted"',                    '"exe quoted"', "win_exec 带引号参数原样保留"),
            ("echo exe-pipe-src | findstr exe",      "exe-pipe-src", "win_exec 管道 | 正常工作"),
            ("cmd /c echo exe-nested",               "exe-nested",  "win_exec cmd /c 前缀照常可用"),
        ):
            st, _, body = post("/mcp", {"jsonrpc": "2.0", "id": 10, "method": "tools/call",
                                        "params": {"name": "win_exec",
                                                   "arguments": {"command": cmd}}},
                               headers={"Mcp-Session-Id": sid})
            r = json.loads(body)["result"]
            txt = " ".join(i.get("text", "") for i in r["content"])
            check(name, r["isError"] is False and needle in txt, txt[:200])

        # cmd /c 前缀剥掉后仍查首程序 —— whoami 借 cmd /c 走私必须被拒
        st, _, body = post("/mcp", {"jsonrpc": "2.0", "id": 10, "method": "tools/call",
                                    "params": {"name": "win_exec",
                                               "arguments": {"command": "cmd /c whoami"}}},
                           headers={"Mcp-Session-Id": sid})
        r = json.loads(body)["result"]
        txt = " ".join(i.get("text", "") for i in r["content"])
        check("win_exec cmd /c whoami 走私被拒",
              r["isError"] is True and "exec_not_allowed" in txt, txt[:200])

        # v1.7.9：stdin 直喂（免引号转义地狱）
        st, _, body = post("/mcp", {"jsonrpc": "2.0", "id": 13, "method": "tools/call",
                                    "params": {"name": "win_exec",
                                               "arguments": {"command": "findstr mcp-stdin",
                                                             "stdin": "mcp-stdin hit line\nplain line\n"}}},
                           headers={"Mcp-Session-Id": sid})
        r = json.loads(body)["result"]
        txt = " ".join(i.get("text", "") for i in r["content"])
        check("win_exec stdin 直喂脚本（findstr 读标准输入）",
              r["isError"] is False and "mcp-stdin hit line" in txt
              and "plain line" not in txt, txt[:200])

        # v1.7.9：structuredContent 机器可读字段（exit_code 不用再从文本解析）
        check("win_exec 返回 structuredContent（exit_code/duration_ms）",
              isinstance(r.get("structuredContent"), dict)
              and r["structuredContent"].get("exit_code") == 0,
              body[:200])

        # v1.7.9：超限输出落盘 output_file + win_read 可直读全量
        big_content = (("BIGLINE-" + "x" * 99 + "\n") * 2200
                       + "FATAL_AT_END_AT_TAIL\n")       # ~230KB > 64KB，尾部带报错行
        st, _, body = post("/api/write", {"path": os.path.join(share, "big.txt"),
                                          "content": big_content})
        check("准备大文件用于输出截断测试", st == 200, body[:120])
        st, _, body = post("/mcp", {"jsonrpc": "2.0", "id": 14, "method": "tools/call",
                                    "params": {"name": "win_exec",
                                               "arguments": {"command": "type big.txt"}}},
                           headers={"Mcp-Session-Id": sid})
        r = json.loads(body)["result"]
        sc = r.get("structuredContent") or {}
        out_file = sc.get("output_file")
        check("超限输出已落盘（output_truncated + output_file）",
              r["isError"] is False and sc.get("output_truncated") is True
              and out_file and out_file.endswith(".txt"), body[:200])
        # v1.7.18：预览必须头尾双端保留，且 win_tail 能直读 exec 落盘输出
        exec_text = " ".join(i.get("text", "") for i in r.get("content", []))
        check("预览保留头部与尾部报错（不再只留头）",
              "BIGLINE-" in exec_text and "FATAL_AT_END_AT_TAIL" in exec_text,
              exec_text[:200])
        check("返回省略行数与预览字节数",
              sc.get("output_omitted_lines", 0) > 0
              and sc.get("output_preview_bytes", 0) > 0,
              str({k: sc.get(k) for k in ("output_omitted_lines", "output_preview_bytes")}))
        st, _, body = post("/mcp", {"jsonrpc": "2.0", "id": 161, "method": "tools/call",
                                    "params": {"name": "win_tail",
                                               "arguments": {"path": out_file, "lines": 3}}},
                           headers={"Mcp-Session-Id": sid})
        t_res = json.loads(body)["result"]
        t_txt = " ".join(i.get("text", "") for i in t_res.get("content", []))
        check("win_tail 能读 exec 落盘输出（旧版被数据目录禁访拦截）",
              t_res.get("isError") is False and "FATAL_AT_END_AT_TAIL" in t_txt,
              t_txt[:200])
        st, _, body = post("/mcp", {"jsonrpc": "2.0", "id": 15, "method": "tools/call",
                                    "params": {"name": "win_read",
                                               "arguments": {"path": out_file,
                                                             "max_lines": 3}}},
                           headers={"Mcp-Session-Id": sid})
        txt = " ".join(i.get("text", "") for i in json.loads(body)["result"]["content"])
        check("win_read 能直读 exec 落盘的完整输出",
              json.loads(body)["result"]["isError"] is False and "BIGLINE-" in txt,
              txt[:200])

        # v1.7.11：win_read 续读游标 next_start_line（截断时直接给下一步参数）
        st, _, body = post("/mcp", {"jsonrpc": "2.0", "id": 20, "method": "tools/call",
                                    "params": {"name": "win_read",
                                               "arguments": {"path": out_file,
                                                             "max_lines": 3}}},
                           headers={"Mcp-Session-Id": sid})
        r = json.loads(body)["result"]
        sc = r.get("structuredContent") or {}
        txt = " ".join(i.get("text", "") for i in r["content"])
        check("win_read 截断时返回 next_start_line 续读游标",
              sc.get("next_start_line") == 4 and "start_line=4" in txt,
              txt[:160])
        st, _, body = post("/mcp", {"jsonrpc": "2.0", "id": 21, "method": "tools/call",
                                    "params": {"name": "win_read",
                                               "arguments": {"path": out_file,
                                                             "start_line": 4,
                                                             "max_lines": 2}}},
                           headers={"Mcp-Session-Id": sid})
        sc = json.loads(body)["result"].get("structuredContent") or {}
        check("按游标续读从第 4 行开始（start_line 生效）",
              sc.get("start_line") == 4 and sc.get("returned_lines") == 2,
              body[:160])

        # v1.7.12 围栏⑦（全链 + 内建名单）：cwd 劫持防护（exe 冻结环境同样生效）
        # a) 外部程序 findstr：cwd 投放同名 .bat 必须拒
        plant = os.path.join(share, "findstr.bat")
        with open(plant, "w", encoding="utf-8") as fh:
            fh.write("@echo planted-hijack-ran\n")
        st, _, body = post("/mcp", {"jsonrpc": "2.0", "id": 22, "method": "tools/call",
                                    "params": {"name": "win_exec",
                                               "arguments": {"command": "findstr x"}}},
                           headers={"Mcp-Session-Id": sid})
        r = json.loads(body)["result"]
        txt = " ".join(i.get("text", "") for i in r["content"])
        check("win_exec cwd 投放同名 findstr.bat 劫持被拒（exe 环境）",
              r["isError"] is True and "exec_hijack_suspected" in txt, txt[:200])
        # b) 全链检测：首程序合法，链上第二段是投放文件 → 拒（外部实测的绕过）
        st, _, body = post("/mcp", {"jsonrpc": "2.0", "id": 23, "method": "tools/call",
                                    "params": {"name": "win_exec",
                                               "arguments": {"command": "echo ok && findstr.bat y"}}},
                           headers={"Mcp-Session-Id": sid})
        r = json.loads(body)["result"]
        txt = " ".join(i.get("text", "") for i in r["content"])
        check("win_exec 链上第二段投放文件借道被拒（dir && payload.bat 场景）",
              r["isError"] is True and "exec_hijack_suspected" in txt, txt[:200])
        # c) cmd 内建：投放同名 echo.bat 劫持不了内建（不再误报）
        plant2 = os.path.join(share, "echo.bat")
        with open(plant2, "w", encoding="utf-8") as fh:
            fh.write("@echo planted-hijack-ran\n")
        st, _, body = post("/mcp", {"jsonrpc": "2.0", "id": 24, "method": "tools/call",
                                    "params": {"name": "win_exec",
                                               "arguments": {"command": "echo innocent"}}},
                           headers={"Mcp-Session-Id": sid})
        r = json.loads(body)["result"]
        txt = " ".join(i.get("text", "") for i in r["content"])
        check("win_exec cmd 内建（echo）不受 cwd 同名投放影响（不再误报）",
              r["isError"] is False and "innocent" in txt, txt[:200])
        os.remove(plant)
        os.remove(plant2)

        # v1.7.12：win_read offset 游标——超限文件的尾部必须读得到
        # （v1.7.11 的行号游标在 byte 截断文件上够不着尾部：实测尾部 99 行永久丢失）
        big2 = ("TAILFILE-" + "y" * 79 + "\n") * 55000          # ~ 4.6MB > 4MB 读上限
        big2 += "FINAL-LINE-MARKER-42\n"
        st, _, body = post("/api/write", {"path": os.path.join(share, "tail-file.txt"),
                                          "content": big2})
        check("准备超读限文件（4.6MB）用于 offset 游标测试", st == 200, body[:120])
        tail_path = os.path.join(share, "tail-file.txt")
        seg_total, cur_off, cur_line, guard = 0, 0, 1, 0
        while guard < 8:                                          # 防死循环
            guard += 1
            st, _, body = post("/mcp", {"jsonrpc": "2.0", "id": 30 + guard,
                                        "method": "tools/call",
                                        "params": {"name": "win_read",
                                                   "arguments": {"path": tail_path,
                                                                 "offset": cur_off,
                                                                 "start_line": cur_line,
                                                                 "max_lines": 60000}}},
                               headers={"Mcp-Session-Id": sid})
            r = json.loads(body)["result"]
            sc = r.get("structuredContent") or {}
            seg_total += int(sc.get("returned_lines") or 0)
            if not sc.get("truncated"):
                break
            cur_off = sc["next_offset"]
            cur_line = sc["next_start_line"]
        st, _, body = post("/mcp", {"jsonrpc": "2.0", "id": 99, "method": "tools/call",
                                    "params": {"name": "win_read",
                                               "arguments": {"path": tail_path,
                                                             "offset": cur_off,
                                                             "start_line": cur_line,
                                                             "max_lines": 60000}}},
                           headers={"Mcp-Session-Id": sid})
        r = json.loads(body)["result"]
        sc = r.get("structuredContent") or {}
        txt = " ".join(i.get("text", "") for i in r["content"])
        check("win_read offset 游标能读到超限文件的尾部（FINAL 标记可达）",
              sc.get("truncated") is False and "FINAL-LINE-MARKER-42" in txt
              and seg_total >= 55000,
              f"segs={guard} seg_total={seg_total} tail={txt[-120:]!r}")

        # v1.7.9：/api/upload 原始字节直传（本地→远程大文件通道）
        import hashlib as _hl
        payload = "uploaded-raw-payload-line\n" * 40                 # ~1.2KB 文本载荷
        up_path = os.path.join(share, "up-raw.txt")
        q = urllib.parse.quote(str(up_path), safe="")
        sha = _hl.sha256(payload.encode()).hexdigest()
        st, body = post_raw(f"/api/upload?path={q}&sha256={sha}", payload.encode())
        resp = json.loads(body)
        check("POST /api/upload 直传成功 + sha256 校验一致",
              st == 200 and resp.get("ok") and resp["data"]["sha256"] == sha
              and resp["data"]["bytes"] == len(payload), body[:200])
        st, _, body = post("/api/read", {"path": up_path})
        check("上传内容可读回（原始字节无损）",
              st == 200 and "uploaded-raw-payload-line" in body, body[:160])
        st, body = post_raw(f"/api/upload?path={q}&sha256={'0' * 64}", payload.encode())
        check("upload sha256 不匹配被拒（409 conflict）",
              st == 409 and "conflict" in body, body[:200])
        st, body = post_raw(f"/api/upload?path={q}", payload.encode(), token=None)
        check("upload 无 Token 被拒（401）", st == 401, str(st))
        st, body = post_raw(f"/api/upload?path={q}", payload.encode())
        resp = json.loads(body)
        check("upload 覆盖已有文件自动备份",
              st == 200 and resp.get("ok") and resp["data"].get("backup"), body[:200])

        # v1.7.9：win_write backup=false（按次关闭写前备份）
        st, _, body = post("/mcp", {"jsonrpc": "2.0", "id": 16, "method": "tools/call",
                                    "params": {"name": "win_write",
                                               "arguments": {"path": os.path.join(share, "nb.txt"),
                                                             "content": "no-backup\n",
                                                             "backup": False}}},
                           headers={"Mcp-Session-Id": sid})
        check("win_write backup=false 正常写入",
              json.loads(body)["result"]["isError"] is False, body[:200])

        # v1.7.10：win_write_b64 内容级 sha256 校验（防 AI 客户端静默截断）
        import base64 as _b64
        b64_payload = "b64-verify-payload-line\n" * 30            # ~0.75KB
        b64_raw = b64_payload.encode()
        b64_sha = _hl.sha256(b64_raw).hexdigest()
        b64_path = os.path.join(share, "b64-verify.txt")
        st, _, body = post("/mcp", {"jsonrpc": "2.0", "id": 17, "method": "tools/call",
                                    "params": {"name": "win_write_b64",
                                               "arguments": {"path": b64_path,
                                                             "base64": _b64.b64encode(b64_raw).decode(),
                                                             "expected_sha256": b64_sha}}},
                           headers={"Mcp-Session-Id": sid})
        check("win_write_b64 expected_sha256 一致时正常写入",
              json.loads(body)["result"]["isError"] is False, body[:200])
        st, _, body = post("/mcp", {"jsonrpc": "2.0", "id": 18, "method": "tools/call",
                                    "params": {"name": "win_write_b64",
                                               "arguments": {"path": b64_path,
                                                             "base64": _b64.b64encode(b64_raw).decode(),
                                                             "expected_sha256": "0" * 64}}},
                           headers={"Mcp-Session-Id": sid})
        r = json.loads(body)["result"]
        txt = " ".join(i.get("text", "") for i in r["content"])
        check("win_write_b64 sha256 不匹配 409 拒写 + 报错引导 /api/upload",
              r["isError"] is True and "conflict" in txt and "/api/upload" in txt,
              txt[:200])
        # 模拟「客户端截断」：base64 被截掉一半，hash 按完整内容算 → 必须拒
        half_b64 = _b64.b64encode(b64_raw).decode()[:len(_b64.b64encode(b64_raw).decode()) // 2]
        st, _, body = post("/mcp", {"jsonrpc": "2.0", "id": 19, "method": "tools/call",
                                    "params": {"name": "win_write_b64",
                                               "arguments": {"path": b64_path,
                                                             "base64": half_b64,
                                                             "expected_sha256": b64_sha}}},
                           headers={"Mcp-Session-Id": sid})
        r = json.loads(body)["result"]
        txt = " ".join(i.get("text", "") for i in r["content"])
        check("win_write_b64 模拟客户端截断被 hash 兜底拒写",
              r["isError"] is True and "conflict" in txt and "截断" in txt,
              txt[:200])

        # BOM 防误用：utf-8-sig 写入必须在 MCP 层被拦
        st, _, body = post("/mcp", {"jsonrpc": "2.0", "id": 11, "method": "tools/call",
                                    "params": {"name": "win_write",
                                               "arguments": {"path": os.path.join(share, "bom.php"),
                                                             "content": "<?php",
                                                             "encoding": "utf-8-sig"}}},
                           headers={"Mcp-Session-Id": sid})
        r = json.loads(body)["result"]
        txt = " ".join(i.get("text", "") for i in r["content"])
        check("utf-8-sig 写入被拦（防 BOM 破坏 PHP）",
              r["isError"] is True and "BOM" in txt, txt[:200])

        # win_grep pattern 别名（真会话路径）
        st, _, body = post("/mcp", {"jsonrpc": "2.0", "id": 12, "method": "tools/call",
                                    "params": {"name": "win_grep", "arguments": {"pattern": "ERROR", "root": share}}},
                           headers={"Mcp-Session-Id": sid})
        txt = " ".join(i.get("text", "") for i in json.loads(body)["result"]["content"])
        check("win_grep 用 pattern 参数名也能命中", "disk full" in txt, txt[:200])

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
        # v1.7.15：PyInstaller onefile 是「bootloader 父进程 + 真正跑服务的子进程」。
        # 只 terminate() 父进程会留下子进程继续监听端口、占着 dist/wkfile.exe，
        # 下一次 PyInstaller 构建删不掉旧 exe 就直接失败。/T 杀整棵树。
        try:
            subprocess.run(
                ["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                capture_output=True, timeout=10,
            )
        except Exception:  # noqa: BLE001 —— 源码版 / 非 Windows 下退化到下面的处理
            pass
        try:
            proc.terminate()
            proc.wait(timeout=8)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass

    print("\n" + "=" * 62)
    print(f"通过 {len(PASS)} 项，失败 {len(FAIL)} 项")
    for f in FAIL:
        print("  - 失败:", f)
    print("=" * 62)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
