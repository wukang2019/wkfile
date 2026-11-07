# -*- coding: utf-8 -*-
"""v1.7.22 契约回归：响应必须真的能发出去 + 围栏⑦补漏 + 回滚体积闸。

背景（外部体检发现的 P0）：v1.7.21 的 win_patch 写成功后，把原文件的
完整 bytes 塞进返回值 changes[*].original。HTTP 侧 json.dumps 直接 500、
MCP 侧连接断，可文件已经落盘、审计也已经记了成功 —— 客户端看到失败
就去重试，同一处补丁被重复应用到已经改过的文件上。

旧测试全绿放过它，是因为只断言了 isError / 文件.content，从没把返回值
真正序列化一次。本文件把「响应能不能发出去」当成一等契约来验：
所有 MCP 工具、所有 HTTP action 的返回值都必须 json.dumps 成功，
并且递归检查没有 bytes 混进响应对象。
"""
import hashlib
import json
import os
import shutil
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "server"))
import wkfile_agent as wa  # noqa: E402
import wkfile_mcp_endpoint as m  # noqa: E402

PASS, FAIL, SKIP = [], [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print("  %s %s%s" % ("[OK]  " if cond else "[FAIL]", name,
                         "  :: %s" % detail if detail and not cond else ""))


def skip(name, why):
    SKIP.append(name)
    print("  [SKIP] %s :: %s" % (name, why))


def find_bytes(obj, path="$"):
    """找出响应对象里第一个不能进 JSON 的值，给出可读路径。"""
    if isinstance(obj, (bytes, bytearray, memoryview)):
        return path
    if isinstance(obj, dict):
        for k, v in obj.items():
            if not isinstance(k, str):
                return "%s.<非字符串键 %s>" % (path, type(k).__name__)
            hit = find_bytes(v, "%s.%s" % (path, k))
            if hit:
                return hit
    elif isinstance(obj, (list, tuple, set)):
        for i, v in enumerate(obj):
            hit = find_bytes(v, "%s[%d]" % (path, i))
            if hit:
                return hit
    return None


def json_safe(name, obj):
    """模拟 Handler._ok / _mcp_send_payload：只查 isError 拦不住序列化崩溃。"""
    hit = find_bytes(obj)
    if hit:
        check(name, False, "响应里混进了 bytes：%s" % hit)
        return False
    try:
        payload = json.dumps(obj, ensure_ascii=False, allow_nan=False)
    except (TypeError, ValueError) as exc:
        check(name, False, "json.dumps 抛 %s：%s" % (type(exc).__name__, exc))
        return False
    check(name, bool(payload) and json.loads(payload) is not None)
    return True


def make_agent(base, share, **over):
    data = {
        "roots": [share], "max_read_bytes": 4 << 20,
        "max_write_bytes": 16 << 20, "auto_backup": True,
        "preflight_check": True,
    }
    data.update(over)
    cfg = wa.Config(data, os.path.join(base, "c.json"), env={})
    cfg.base_dir = base
    a = wa.Agent(cfg)
    a.set_request_mode(False)
    return a, cfg


def put_raw(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as fh:
        fh.write(data)


def read_raw(path):
    with open(path, "rb") as fh:
        return fh.read()


def err_of(fn):
    try:
        fn()
        return None, ""
    except wa.PathError as exc:
        return exc.code, exc.message
    except Exception as exc:  # noqa: BLE001
        return exc.__class__.__name__, str(exc)


# ---------------------------------------------------------------------------
def test_p0_patch_response():
    print("\n========== P0. win_patch 响应必须可序列化 ==========")
    base = tempfile.mkdtemp(prefix="wk-p0-")
    try:
        share = os.path.join(base, "share")
        os.makedirs(share)
        a, _ = make_agent(base, share)
        p = os.path.join(share, "app.py")
        old = b"print('v1')\n"
        put_raw(p, old)

        # 1) 裸 HTTP：Handler._ok 就是 json.dumps({"ok":True,"data":data})
        data = a.op_patch({"changes": [{"path": p, "content": "print('v2')\n"}]})
        json_safe("P0 /api/patch 响应包可 json.dumps（不再 500）",
                  {"ok": True, "data": data})
        ch = data["changes"][0]
        check("P0 响应里不再出现 original 原始字节", "original" not in ch,
              str(sorted(ch.keys())))
        check("P0 响应给出原文件 sha256（可判幂等）",
              ch.get("original_sha256") == hashlib.sha256(old).hexdigest(),
              str(ch.get("original_sha256")))
        check("P0 响应给出原文件长度", ch.get("original_bytes") == len(old),
              str(ch.get("original_bytes")))
        check("P0 文件确实写进去了", read_raw(p) == b"print('v2')\n")

        # 2) MCP：structuredContent 原样带 data，bytes 会让整条连接断
        res = m.call_tool(a.dispatch, {"name": "win_patch", "arguments": {
            "changes": [{"path": p, "old": "v2", "new": "v3"}]}})
        json_safe("P0 MCP tools/call 整包可 json.dumps（不再断连）",
                  {"jsonrpc": "2.0", "id": 1, "result": res})
        check("P0 MCP 侧也真的改成功了", res["isError"] is False
              and read_raw(p) == b"print('v3')\n")

        # 3) dry_run / 多文件 / 新文件 都不能把 bytes 漏出来
        p2 = os.path.join(share, "new.txt")
        dry = a.op_patch({"changes": [{"path": p2, "content": "n\n"}],
                          "dry_run": True})
        json_safe("P0 dry_run 响应可序列化", {"ok": True, "data": dry})
        check("P0 dry_run 没有落盘", not os.path.exists(p2))
        multi = a.op_patch({"changes": [
            {"path": p2, "content": "n\n"},
            {"path": p, "old": "v3", "new": "v4"},
        ]})
        json_safe("P0 多文件 patch 响应可序列化", {"ok": True, "data": multi})
        check("P0 多文件响应每项都无原始字节",
              all("original" not in it for it in multi["changes"]))
        # 新建文件没有「原文件」，两个字段必须显式给 None / 0，不能凭空塞 bytes
        fresh = multi["changes"][0]
        check("P0 新建文件的原文件摘要为 None/0",
              fresh.get("original_sha256") is None and fresh.get("original_bytes") == 0,
              str(fresh))

        # 4) AI 重试安全提示：契约里必须明确写「别盲重试」
        schema = {t["name"]: t for t in m.TOOLS}["win_patch"]
        blob = json.dumps(schema, ensure_ascii=False)
        check("P0 win_patch schema 明确警告不要盲目重试",
              "重试" in blob and "expected_sha256" in blob, blob[:200])
    finally:
        shutil.rmtree(base, ignore_errors=True)


# ---------------------------------------------------------------------------
def test_p1_exec_forwarders():
    print("\n========== P1. 围栏⑦ call / start 转发内建 ==========")
    base = tempfile.mkdtemp(prefix="wk-exec-")
    try:
        share = os.path.join(base, "share")
        os.makedirs(share)
        a, _ = make_agent(base, share, exec_enabled=True,
                          exec_allowed_commands=["echo", "dir"])
        put_raw(os.path.join(share, "payload.bat"), b"@echo pwned\n")
        os.makedirs(os.path.join(share, "sub"))
        put_raw(os.path.join(share, "sub", "nested.bat"), b"@echo pwned\n")

        cases = [
            ("dir && call payload.bat", True, "call 借道（体检原例）"),
            ("echo ok && call payload.bat", True, "链上 call 借道"),
            ("echo ok && start payload.bat", True, "start 借道"),
            ('echo ok && start "" /MIN payload.bat', True, "start 带空标题与开关"),
            ("echo ok && call sub\\nested.bat", True, "call 相对路径脚本"),
            ("echo ok && cmd /c payload.bat", True, "cmd /c 转发"),
            ("echo ok && call :label", False, "call :label 只是标签"),
            ("echo innocent", False, "普通内建不误报"),
            ("dir && echo fine", False, "合法链不误报"),
        ]
        for command, blocked, name in cases:
            code, msg = err_of(lambda c=command: a._exec_guard_hijack(c, share))
            check("围栏⑦ %s" % name,
                  (code == "exec_hijack_suspected") if blocked else (code is None),
                  "code=%s msg=%s" % (code, msg[:120]))

        # 真实 op_exec 链路（首程序 echo 在白名单，会在 hijack 处被拒，不真跑命令）
        code, msg = err_of(lambda: a.op_exec({
            "command": "echo ok && call payload.bat", "cwd": share}))
        check("围栏⑦ op_exec 里同样拦下 call 借道",
              code == "exec_hijack_suspected", "code=%s msg=%s" % (code, msg[:120]))
    finally:
        shutil.rmtree(base, ignore_errors=True)


# ---------------------------------------------------------------------------
def test_p1_rollback_cap():
    print("\n========== P1. 回滚原文体积闸 ==========")
    base = tempfile.mkdtemp(prefix="wk-cap-")
    try:
        share = os.path.join(base, "share")
        os.makedirs(share)
        cap = 1 << 20
        a, cfg = make_agent(base, share, patch_max_rollback_bytes=cap)
        check("patch_max_rollback_bytes 已进配置",
              int(getattr(cfg, "patch_max_rollback_bytes", 0)) == cap)

        big = b"X" * (cap + 4096)
        p = os.path.join(share, "big.txt")
        put_raw(p, big)
        code, msg = err_of(lambda: a.op_patch({
            "changes": [{"path": p, "content": "tiny\n"}]}))
        check("超过回滚上限在写前被拒", code == "too_large",
              "code=%s msg=%s" % (code, msg[:160]))
        check("被拒后原文件一字未动", read_raw(p) == big)

        # 单文件都合格、加总超限，也要在写前拦下（不能改了一半再失败）
        part = b"Y" * (cap * 3 // 4)
        p1 = os.path.join(share, "a.txt")
        p2 = os.path.join(share, "b.txt")
        put_raw(p1, part)
        put_raw(p2, part)
        code2, msg2 = err_of(lambda: a.op_patch({"changes": [
            {"path": p1, "content": "a\n"}, {"path": p2, "content": "b\n"}]}))
        check("回滚总量超限同样写前拒绝", code2 == "too_large",
              "code=%s msg=%s" % (code2, msg2[:160]))
        check("总量被拒后两个文件都未改动",
              read_raw(p1) == part and read_raw(p2) == part)

        # 正常体积不受影响，且响应依然可序列化
        small = os.path.join(share, "small.txt")
        put_raw(small, b"a=1\n")
        data = a.op_patch({"changes": [{"path": small, "old": "1", "new": "2"}]})
        json_safe("体积闸开启后正常 patch 响应仍可序列化",
                  {"ok": True, "data": data})
        check("正常 patch 生效", read_raw(small) == b"a=2\n")
    finally:
        shutil.rmtree(base, ignore_errors=True)


# ---------------------------------------------------------------------------
def test_all_tool_contracts():
    print("\n========== 契约护栏. 所有工具响应必须可序列化 ==========")
    base = tempfile.mkdtemp(prefix="wk-contract-")
    try:
        share = os.path.join(base, "share")
        os.makedirs(share)
        a, _ = make_agent(base, share)
        put_raw(os.path.join(share, "a.txt"), b"hello\n")

        # MCP 全工具：参数留空，多数会在参数校验处变成 isError，少数正常返回。
        # 两条路径的响应对象都必须能 json.dumps —— 这正是旧测试漏掉的维度。
        for t in m.TOOLS:
            name = t["name"]
            try:
                res = m.call_tool(a.dispatch, {"name": name, "arguments": {}})
            except Exception as exc:  # noqa: BLE001
                res = {"content": [{"type": "text", "text": str(exc)}],
                       "isError": True}
            json_safe("MCP %s 响应可序列化" % name,
                      {"jsonrpc": "2.0", "id": 1, "result": res})

        # HTTP 全 action：用不存在的路径触发错误响应（不产生写副作用），
        # 成功路径由上面的专项用例覆盖。错误响应同样要能发出去。
        missing = os.path.join(share, "__missing__.txt")
        for action in wa.Agent.ROUTES:
            try:
                data = a.dispatch(action, {"path": missing})
            except wa.PathError as exc:
                data = {"ok": False,
                        "error": {"code": exc.code, "message": exc.message}}
            except Exception as exc:  # noqa: BLE001
                data = {"ok": False, "error": {"code": "internal",
                                               "message": str(exc)}}
            json_safe("HTTP /api/%s 响应可序列化" % action,
                      {"ok": True, "data": data})
    finally:
        shutil.rmtree(base, ignore_errors=True)


def main():
    test_p0_patch_response()
    test_p1_exec_forwarders()
    test_p1_rollback_cap()
    test_all_tool_contracts()
    print("\n========== 汇总 ==========")
    print("通过 %d / 失败 %d / 环境跳过 %d" % (len(PASS), len(FAIL), len(SKIP)))
    if FAIL:
        for name in FAIL:
            print("  [FAIL] " + name)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
