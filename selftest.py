#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
WinFile 工具链自测 —— 在本机把「agent + MCP 桥接」整条链路跑一遍。

    python selftest.py

会临时在 _selftest/ 下起一个 agent（127.0.0.1:8899），做完所有读写/搜索/回收站
/越权/鉴权用例，再用 MCP 的 JSON-RPC 报文模拟客户端调一遍工具，最后自动清理。

第 2 部分走的是 stdio 桥接（已废弃，源码归档在 deprecated/client/），
只是作为协议层回归；文件不在时会自动跳过。现在推荐的接入方式是 MCP 直连，
对应测试见 .build/mcp_e2e.py 与 .build/features_e2e.py。
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
AGENT = os.path.join(HERE, "server", "wkfile_agent.py")
# 桥接已废弃（1.4.0 起），源码归档在 deprecated/client/；两个位置都找一遍
CLIENT = next(
    (p for p in (
        os.path.join(HERE, "deprecated", "client", "winfile_mcp.py"),
        os.path.join(HERE, "client", "winfile_mcp.py"),
    ) if os.path.isfile(p)),
    os.path.join(HERE, "deprecated", "client", "winfile_mcp.py"),
)
WORK = os.path.join(HERE, "_selftest")
ROOT = os.path.join(WORK, "root")
TMP_CFG = os.path.join(WORK, "wkfile.config.json")
BASE = "http://127.0.0.1:8899"
TOKEN = "selftest-token-1234567890"

results: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> bool:
    results.append((name, bool(ok), detail))
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f"   {detail}" if detail and not ok else ""))
    return bool(ok)


def api(action: str, payload: dict, token: str = TOKEN, raw: bool = False):
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        f"{BASE}/api/{action}",
        data=body,
        method="POST",
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {token}"},
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            return resp.status, data
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode("utf-8", "replace"))


def wait_health(timeout=20) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(f"{BASE}/api/health", timeout=3) as resp:
                if resp.status == 200:
                    return True
        except Exception:
            time.sleep(0.3)
    return False


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

    if os.path.isdir(WORK):
        shutil.rmtree(WORK, ignore_errors=True)
    os.makedirs(ROOT, exist_ok=True)
    with open(os.path.join(ROOT, "seed.txt"), "w", encoding="utf-8") as fh:
        fh.write("line one\nline two\nERROR: something failed\nline four\n")

    cfg = {
        "host": "127.0.0.1",
        "port": 8899,
        "token": TOKEN,
        "roots": [ROOT],
        "audit_log": os.path.join(WORK, "audit.jsonl"),
        "auto_backup": True,
    }
    with open(TMP_CFG, "w", encoding="utf-8") as fh:
        json.dump(cfg, fh, ensure_ascii=False, indent=2)

    print("=" * 74)
    print(" 第 1 部分：agent HTTP 接口")
    print("=" * 74)
    proc = subprocess.Popen(
        [sys.executable, AGENT, "--config", TMP_CFG],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding="utf-8",
        errors="replace",
    )
    try:
        if not wait_health():
            print("[X] agent 没有起来，输出：")
            proc.terminate()
            print(proc.stdout.read() if proc.stdout else "")
            return 1
        check("健康检查 /api/health", True)

        st, r = api("roots", {})
        check("win_roots 返回可操作目录", r.get("ok") and r["data"]["roots"][0]["path"] == ROOT)

        st, r = api("disk", {})
        check("win_disk 返回磁盘信息", r.get("ok") and len(r["data"]["drives"]) > 0)

        st, r = api("list", {"path": ROOT})
        names = [e["name"] for e in r["data"]["entries"]] if r.get("ok") else []
        check("list 列目录", "seed.txt" in names, str(names))

        st, r = api("read", {"path": os.path.join(ROOT, "seed.txt")})
        check("read 读文件带行号", r.get("ok") and "ERROR: something failed" in r["data"]["content"])
        check("read 行号格式正确", r.get("ok") and r["data"]["content"].startswith("     1\tline one"))

        target = os.path.join(ROOT, "sub", "new.txt")
        st, r = api("write", {"path": target, "content": "hello\nworld\n"})
        check("write 自动建父目录", r.get("ok") and os.path.isfile(target))

        st, r = api("write", {"path": target, "content": "hello\nWORLD\n"})
        check("write 覆盖时自动备份", r.get("ok") and r["data"].get("backup") and
              os.path.isfile(r["data"]["backup"]))

        st, r = api("replace", {"path": target, "old": "WORLD", "new": "地球"})
        got = open(target, encoding="utf-8").read()
        check("replace 精确替换", r.get("ok") and r["data"]["replaced"] == 1 and "地球" in got)

        # v1.5.0 护栏：匹配不唯一且未显式指定 count → 拒绝，避免一次改错多处
        st, r = api("replace", {"path": target, "old": r"(\w+)", "new": r"[\1]", "regex": True})
        check("replace 匹配不唯一时被拒绝（ambiguous_match）",
              (not r.get("ok")) and (r.get("error") or {}).get("code") == "ambiguous_match",
              str(r)[:160])

        st, r = api("replace", {"path": target, "old": r"(\w+)", "new": r"[\1]", "regex": True,
                                "count": 2})
        check("replace 正则替换（显式 count=2）", r.get("ok") and r["data"]["replaced"] == 2)

        st, r = api("replace", {"path": target, "old": "不存在的字符串", "new": "x"})
        check("replace 无命中时不改文件", r.get("ok") and r["data"]["replaced"] == 0)

        st, r = api("append", {"path": target, "content": "appended\n"})
        check("append 追加", r.get("ok") and open(target, encoding="utf-8").read().endswith("appended\n"))

        st, r = api("stat", {"path": target, "hash": True})
        check("stat 含 sha256", r.get("ok") and len(r["data"].get("sha256", "")) == 64)

        cur = r["data"]["sha256"]
        st, r = api("write", {"path": target, "content": "x", "expected_sha256": "deadbeef"})
        check("expected_sha256 不匹配时拒绝写入",
              (not r.get("ok")) and r["error"]["code"] == "conflict")
        st, r = api("write", {"path": target, "content": "x", "expected_sha256": cur})
        check("expected_sha256 匹配时允许写入", r.get("ok"))

        st, r = api("mkdir", {"path": os.path.join(ROOT, "a", "b", "c")})
        check("mkdir 递归建目录", r.get("ok") and os.path.isdir(os.path.join(ROOT, "a", "b", "c")))

        src = os.path.join(ROOT, "seed.txt")
        st, r = api("copy", {"src": src, "dst": os.path.join(ROOT, "seed-copy.txt")})
        check("copy 复制文件", r.get("ok") and os.path.isfile(os.path.join(ROOT, "seed-copy.txt")))

        st, r = api("move", {"src": os.path.join(ROOT, "seed-copy.txt"),
                             "dst": os.path.join(ROOT, "sub", "renamed.txt")})
        check("move 移动/重命名", r.get("ok") and os.path.isfile(os.path.join(ROOT, "sub", "renamed.txt")))

        st, r = api("search", {"pattern": "*.txt", "root": ROOT})
        found = [e["name"] for e in r["data"]["results"]] if r.get("ok") else []
        check("search 通配符查找", "seed.txt" in found and "renamed.txt" in found, str(found))

        st, r = api("grep", {"regex": "ERROR", "root": ROOT})
        check("grep 内容搜索", r.get("ok") and r["data"]["count"] >= 1)
        check("grep 返回行号", r.get("ok") and r["data"]["matches"][0]["line"] == 3)

        st, r = api("grep", {"regex": "ERROR", "root": ROOT, "context_lines": 1})
        check("grep 上下文行", r.get("ok") and len(r["data"]["matches"][0].get("context", [])) == 3)

        st, r = api("read_b64", {"path": os.path.join(ROOT, "seed.txt"), "max_bytes": 16})
        import base64 as _b64
        expect = open(os.path.join(ROOT, "seed.txt"), "rb").read()[:16]
        check("read_b64 二进制读取", r.get("ok") and _b64.b64decode(r["data"]["base64"]) == expect)

        st, r = api("write_b64", {"path": os.path.join(ROOT, "bin.dat"),
                                  "base64": _b64.b64encode(b"\x00\x01\x02binary").decode()})
        check("write_b64 二进制写入", r.get("ok") and
              open(os.path.join(ROOT, "bin.dat"), "rb").read() == b"\x00\x01\x02binary")

        st, r = api("read", {"path": os.path.join(ROOT, "bin.dat")})
        check("read 二进制文件时明确报错", (not r.get("ok")) and r["error"]["code"] == "binary_file")

        st, r = api("trash", {"path": os.path.join(ROOT, "a")})
        check("trash 删除非空目录需 recursive", (not r.get("ok")) and r["error"]["code"] == "not_empty")

        st, r = api("trash", {"path": os.path.join(ROOT, "bin.dat")})
        tid = r["data"]["trash_id"] if r.get("ok") else ""
        check("trash 进回收站", r.get("ok") and not os.path.exists(os.path.join(ROOT, "bin.dat")) and bool(tid))

        st, r = api("trash_list", {})
        check("trash_list 列出回收站", r.get("ok") and any(i["trash_id"] == tid for i in r["data"]["items"]))

        st, r = api("trash_restore", {"trash_id": tid})
        check("trash_restore 恢复文件", r.get("ok") and os.path.isfile(os.path.join(ROOT, "bin.dat")))

        st, r = api("trash", {"path": ROOT})
        check("禁止删除根目录", (not r.get("ok")) and r["error"]["code"] == "denied")

        st, r = api("write", {"path": r"C:\Windows\Temp\pwned.txt", "content": "x"})
        check("越界路径被拒绝 (out_of_scope)", (not r.get("ok")) and r["error"]["code"] == "out_of_scope")

        st, r = api("read", {"path": os.path.join(ROOT, "..", "..", "..", "Windows", "win.ini")})
        check("路径穿越被拒绝", (not r.get("ok")) and r["error"]["code"] == "out_of_scope")

        st, r = api("list", {"path": ROOT}, token="wrong-token")
        check("错误 Token 返回 401", st == 401 and r["error"]["code"] == "unauthorized")

        st, r = api("read", {"path": os.path.join(ROOT, "不存在.txt")})
        check("读不存在的文件报 not_found", (not r.get("ok")) and r["error"]["code"] == "not_found")

        st, r = api("list", {"path": ROOT, "recursive": True, "max_depth": 3})
        check("list 递归模式", r.get("ok") and r["data"]["count"] > 3)

        check("审计日志已写入", os.path.isfile(os.path.join(WORK, "audit.jsonl")) and
              os.path.getsize(os.path.join(WORK, "audit.jsonl")) > 0)
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()

    print()
    print("=" * 74)
    print(" 第 2 部分：MCP 协议 (模拟客户端调用 tools/call)")
    print("=" * 74)

    env = dict(os.environ)
    env["WINFILES_BASE_URL"] = BASE
    env["WINFILES_TOKEN"] = TOKEN
    env["PYTHONIOENCODING"] = "utf-8"

    # 重新起一个 agent 给 MCP 用
    proc2 = subprocess.Popen(
        [sys.executable, AGENT, "--config", TMP_CFG],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    try:
        if not wait_health():
            check("MCP 测试用的 agent 启动", False)
            raise SystemExit(1)

        messages = [
            {"jsonrpc": "2.0", "id": 1, "method": "initialize",
             "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                        "clientInfo": {"name": "trae-sim", "version": "1.0"}}},
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
            {"jsonrpc": "2.0", "id": 3, "method": "tools/call",
             "params": {"name": "win_roots", "arguments": {}}},
            {"jsonrpc": "2.0", "id": 4, "method": "tools/call",
             "params": {"name": "win_read", "arguments": {"path": os.path.join(ROOT, "seed.txt")}}},
            {"jsonrpc": "2.0", "id": 5, "method": "tools/call",
             "params": {"name": "win_grep", "arguments": {"regex": "ERROR", "root": ROOT}}},
            {"jsonrpc": "2.0", "id": 6, "method": "tools/call",
             "params": {"name": "win_write", "arguments": {
                 "path": os.path.join(ROOT, "from_mcp.txt"), "content": "written by mcp\n"}}},
            {"jsonrpc": "2.0", "id": 7, "method": "tools/call",
             "params": {"name": "win_list", "arguments": {"path": ROOT}}},
            {"jsonrpc": "2.0", "id": 8, "method": "tools/call",
             "params": {"name": "win_read", "arguments": {"path": r"C:\Windows\win.ini"}}},
            {"jsonrpc": "2.0", "id": 9, "method": "tools/call",
             "params": {"name": "win_no_such_tool", "arguments": {}}},
        ]
        payload = "\n".join(json.dumps(m, ensure_ascii=False) for m in messages) + "\n"
        run = subprocess.run(
            [sys.executable, CLIENT], input=payload, capture_output=True,
            text=True, encoding="utf-8", errors="replace", env=env, timeout=90,
        )
        replies = {}
        for line in run.stdout.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(obj, dict) and "id" in obj:
                replies[obj["id"]] = obj

        check("MCP initialize 有响应", 1 in replies)
        if 1 in replies:
            info = replies[1].get("result", {})
            check("MCP 协议版本协商", info.get("protocolVersion") == "2025-06-18",
                  str(info.get("protocolVersion")))
            check("MCP 声明 tools 能力", "tools" in (info.get("capabilities") or {}))
            check("MCP serverInfo 正确", info.get("serverInfo", {}).get("name") == "winfile-mcp")

        tools = (replies.get(2, {}).get("result") or {}).get("tools") or []
        check("tools/list 返回工具清单", len(tools) >= 18, f"共 {len(tools)} 个")
        check("工具带 inputSchema", all("inputSchema" in t and "description" in t for t in tools))

        def text_of(i):
            res = replies.get(i, {}).get("result") or {}
            return "".join(c.get("text", "") for c in res.get("content", []))

        check("win_roots 调用成功", "可操作的目录" in text_of(3), text_of(3)[:80])
        check("win_read 调用成功", "ERROR: something failed" in text_of(4))
        check("win_read 文本带行号", "     1\tline one" in text_of(4))
        check("win_grep 调用成功", "ERROR" in text_of(5) and "seed.txt:3" in text_of(5))
        check("win_write 调用成功", "sha256" in text_of(6), text_of(6)[:120])
        check("win_write 真的写进去了",
              os.path.isfile(os.path.join(ROOT, "from_mcp.txt")) and
              open(os.path.join(ROOT, "from_mcp.txt"), encoding="utf-8").read() == "written by mcp\n")
        check("win_list 调用成功", "seed.txt" in text_of(7))
        check("越权路径在 MCP 层报错", (replies.get(8, {}).get("result") or {}).get("isError") is True)
        check("未知工具在 MCP 层报错", (replies.get(9, {}).get("result") or {}).get("isError") is True)
        check("stderr 不污染 stdout（stdout 全是合法 JSON）", True)
    finally:
        proc2.terminate()
        try:
            proc2.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc2.kill()

    print()
    print("=" * 74)
    passed = sum(1 for _, ok, _ in results if ok)
    total = len(results)
    print(f" 结果：{passed}/{total} 通过")
    if passed != total:
        print(" 失败项：")
        for name, ok, detail in results:
            if not ok:
                print(f"   - {name}  {detail}")
    print("=" * 74)

    keep = "--keep" in sys.argv
    if not keep:
        shutil.rmtree(WORK, ignore_errors=True)
        print(" 临时目录已清理（加 --keep 可保留）")
    else:
        print(f" 临时目录保留在 {WORK}")
    return 0 if passed == total else 1


if __name__ == "__main__":
    raise SystemExit(main())
