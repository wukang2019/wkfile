# -*- coding: utf-8 -*-
"""v1.7.23 契约回归：围栏的动态程序名/误伤、体积闸脚枪、四条老账、文档。

外部体检第二轮的结论：v1.7.22 把 `call payload.bat` 堵上了，但把程序名
藏进变量或 for 变量就过；反过来 `type deploy.ps1` / `copy payload.bat x`
这种纯参数被当成投放脚本误拦。另外 `patch_max_rollback_bytes=0` 不是
「不限」而是把刚加的内存闸整个关掉。

本文件先于产品代码写成，修复前应当成片报红。
"""
import importlib.util
import json
import os
import re
import shutil
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "server"))
import wkfile_agent as wa  # noqa: E402
import wkfile_mcp_endpoint as m  # noqa: E402

PASS, FAIL, SKIP = [], [], []
BLOCK_CODES = ("exec_hijack_suspected", "exec_dynamic_program")


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print("  %s %s%s" % ("[OK]  " if cond else "[FAIL]", name,
                         "  :: %s" % detail if detail and not cond else ""))


def skip(name, why):
    SKIP.append(name)
    print("  [SKIP] %s :: %s" % (name, why))


def err_of(fn):
    try:
        fn()
        return None, ""
    except wa.PathError as exc:
        return exc.code, exc.message
    except Exception as exc:  # noqa: BLE001
        return exc.__class__.__name__, str(exc)


def make_agent(base, share, **over):
    data = {"roots": [share], "max_read_bytes": 4 << 20,
            "max_write_bytes": 16 << 20, "auto_backup": True,
            "preflight_check": True}
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


def find_bytes(obj, path="$"):
    if isinstance(obj, (bytes, bytearray, memoryview)):
        return path
    if isinstance(obj, dict):
        for k, v in obj.items():
            if not isinstance(k, str):
                return "%s.<非字符串键>" % path
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
    try:
        json.dumps(obj, ensure_ascii=False)
    except (TypeError, ValueError) as exc:
        check(name, False, "json.dumps 失败：%s" % exc)
        return
    check(name, find_bytes(obj) is None,
          "响应里混进不可序列化对象：%s" % find_bytes(obj))


# ---------------------------------------------------------------------------
def test_exec_dynamic():
    print("\n========== 1. 围栏⑦：动态程序名 / 参数误伤 ==========")
    base = tempfile.mkdtemp(prefix="wk-exec23-")
    try:
        share = os.path.join(base, "share")
        os.makedirs(share)
        a, _ = make_agent(base, share, exec_enabled=True,
                          exec_allowed_commands=["echo", "dir", "type", "copy"])
        put_raw(os.path.join(share, "payload.bat"), b"@echo pwned\n")
        put_raw(os.path.join(share, "deploy.ps1"), b"Write-Output ok\n")
        os.makedirs(os.path.join(share, "sub"))
        put_raw(os.path.join(share, "sub", "nested.bat"), b"@echo pwned\n")

        blocked = [
            ("set X=payload.bat\ncall %X%", "set 变量 + 换行"),
            ("set X=payload.bat&call %X%", "set 变量 + & 链"),
            ("for %i in (payload.bat) do call %i", "for 变量"),
            ("call %X%", "call 变量"),
            ("call !X!", "延迟展开变量"),
            ("cmd /c %X%", "cmd /c 变量"),
            ("dir && call payload.bat", "回归：call 字面量"),
            ("dir && %cd%\\payload.bat", "回归：%cd% 绝对路径"),
            ("echo ok && call sub\\nested.bat", "回归：相对路径脚本"),
        ]
        for command, name in blocked:
            code, msg = err_of(lambda c=command: a._exec_guard_hijack(c, share))
            check("围栏⑦ 拦下 %s" % name, code in BLOCK_CODES,
                  "code=%s msg=%s" % (code, msg[:110]))

        allowed = [
            ("type deploy.ps1", "读一个 .ps1"),
            ("copy payload.bat x", "复制一个 .bat"),
            ("type x > p.bat", "重定向生成脚本文件"),
            ("echo 100% done", "参数里带百分号"),
            ("dir && echo fine", "普通合法链"),
            ("call :label", "批处理标签"),
        ]
        for command, name in allowed:
            code, msg = err_of(lambda c=command: a._exec_guard_hijack(c, share))
            check("围栏⑦ 不误伤 %s" % name, code is None,
                  "code=%s msg=%s" % (code, msg[:110]))

        code, msg = err_of(lambda: a.op_exec({
            "command": "dir && set X=payload.bat\ncall %X%", "cwd": share}))
        check("op_exec 里同样拦下动态程序名", code in BLOCK_CODES,
              "code=%s msg=%s" % (code, msg[:110]))
    finally:
        shutil.rmtree(base, ignore_errors=True)


# ---------------------------------------------------------------------------
def test_rollback_cap_zero():
    print("\n========== 2. patch_max_rollback_bytes=0 不能等于关闸 ==========")
    base = tempfile.mkdtemp(prefix="wk-cap23-")
    try:
        share = os.path.join(base, "share")
        os.makedirs(share)
        big = b"Z" * ((8 << 20) + 4096)      # 超过内置安全默认 8MB
        p = os.path.join(share, "big.txt")
        put_raw(p, big)
        for bad in (0, -1):
            put_raw(p, big)                  # 上一轮可能已被成功 patch 改写
            a, cfg = make_agent(base, share, patch_max_rollback_bytes=bad)
            code, msg = err_of(lambda a=a: a.op_patch({
                "changes": [{"path": p, "content": "tiny\n"}]}))
            check("配置 %s 时大文件 patch 仍被体积闸拒绝" % bad,
                  code == "too_large", "code=%s msg=%s" % (code, msg[:130]))
            check("配置 %s 时原文件一字未动" % bad, read_raw(p) == big)
        # 显式给一个正数上限仍按配置生效
        put_raw(p, big)
        a2, _ = make_agent(base, share, patch_max_rollback_bytes=1024)
        code2, _ = err_of(lambda: a2.op_patch({
            "changes": [{"path": p, "content": "tiny\n"}]}))
        check("正数上限照旧生效", code2 == "too_large", "code=%s" % code2)
    finally:
        shutil.rmtree(base, ignore_errors=True)


# ---------------------------------------------------------------------------
def test_backup_required():
    print("\n========== 3. backup_required：服务端能强制写前必备份 ==========")
    base = tempfile.mkdtemp(prefix="wk-reqbk-")
    try:
        share = os.path.join(base, "share")
        os.makedirs(share)
        p = os.path.join(share, "a.txt")
        put_raw(p, b"v1\n")

        a0, _ = make_agent(base, share)
        ok, msg = err_of(lambda: a0.op_write({"path": p, "content": "v2\n",
                                              "backup": False}))
        check("默认不开启时 backup=false 保持兼容", ok is None,
              "code=%s msg=%s" % (ok, msg[:110]))

        a, cfg = make_agent(base, share, backup_required=True)
        check("backup_required 已进入配置", bool(getattr(cfg, "backup_required", False)))
        cases = [
            ("op_write", lambda: a.op_write({"path": p, "content": "x\n",
                                             "backup": False})),
            ("op_append", lambda: a.op_append({"path": p, "content": "x\n",
                                               "backup": False})),
            ("op_replace", lambda: a.op_replace({"path": p, "old": "v2",
                                                 "new": "z", "backup": False})),
            ("op_patch", lambda: a.op_patch({"backup": False, "changes": [
                {"path": p, "content": "x\n"}]})),
        ]
        for name, fn in cases:
            code, msg = err_of(fn)
            check("强制备份时 %s 的 backup=false 被拒" % name,
                  code == "backup_required", "code=%s msg=%s" % (code, msg[:110]))
        check("被拒后文件内容没变", read_raw(p) == b"v2\n")
        ok2, msg2 = err_of(lambda: a.op_write({"path": p, "content": "v3\n"}))
        check("不传 backup 时正常写且有备份", ok2 is None,
              "code=%s msg=%s" % (ok2, msg2[:110]))
        check("强制备份开关下确实产生了备份文件",
              any(f for _c, _d, fs in os.walk(wa.backup_root(base)) for f in fs
                  if not f.startswith(".")))

        a3, _ = make_agent(base, share, backup_required=True, auto_backup=False)
        code3, _ = err_of(lambda: a3.op_write({"path": p, "content": "v4\n"}))
        check("auto_backup=false 与强制备份冲突时拒绝写入",
              code3 == "backup_required", "code=%s" % code3)
    finally:
        shutil.rmtree(base, ignore_errors=True)


# ---------------------------------------------------------------------------
def test_backup_inode_volume():
    print("\n========== 4. 备份体积认硬链接 ==========")
    base = tempfile.mkdtemp(prefix="wk-inode-")
    try:
        share = os.path.join(base, "share")
        os.makedirs(share)
        a, _ = make_agent(base, share, backup_dedup=True)
        broot = wa.backup_root(base)
        obj = os.path.join(broot, ".objects", "aa", "entity.bin")
        size = 2 << 20
        put_raw(obj, b"Q" * size)
        for i, st in enumerate(("20260101-00000000-000", "20260102-00000000-000")):
            dst = os.path.join(broot, st, "root", "big.bin")
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            os.link(obj, dst)
        res = a._backup_entries()
        entries = res[1]
        physical = res[2] if len(res) > 2 else None
        logical = sum(int(e.get("size") or 0) for e in entries)
        check("两个历史版本共享实体时统计不翻倍",
              logical <= size, "logical=%d size=%d" % (logical, size))
        check("物理占用按 inode 去重（<= 一份实体）",
              physical is not None and physical <= size,
              "physical=%s size=%d" % (physical, size))
        pol = a._backup_policy()
        check("win_backup_list 策略快照的 total_bytes 不虚高",
              int(pol.get("total_bytes") or 0) <= size,
              "total_bytes=%s" % pol.get("total_bytes"))

        fn = getattr(wa, "backup_inode_sizes", None)
        if fn is None:
            check("纯函数 backup_inode_sizes 已外提", False, "符号不存在")
        else:
            groups = {".objects": [(7, 100)], "s1": [(7, 100), (8, 50)],
                      "s2": [(7, 100)]}
            phys, reclaim = fn(groups)
            check("纯函数：物理占用只算一次共享 inode", phys == 150, str(phys))
            check("纯函数：可释放量归到最后引用者",
                  reclaim.get("s1") == 50 and reclaim.get("s2") == 100,
                  str(reclaim))
    except OSError as exc:
        skip("硬链接体积用例", "当前文件系统不支持 os.link：%s" % exc)
    finally:
        shutil.rmtree(base, ignore_errors=True)


# ---------------------------------------------------------------------------
def test_audit_dropped_alert():
    print("\n========== 5. 审计丢账必须告警 ==========")
    base = tempfile.mkdtemp(prefix="wk-audit23-")
    try:
        share = os.path.join(base, "share")
        os.makedirs(share)
        a, _ = make_agent(base, share)
        a.audit.dropped = 3
        a.audit._pending.clear()
        h = a.audit.health()
        check("dropped>0 时审计健康不再显示 ok",
              h.get("state") != "ok" or bool(h.get("alerts")),
              "state=%s alerts=%s" % (h.get("state"), h.get("alerts")))
        p = a.op_probe({})
        text = json.dumps(p, ensure_ascii=False)
        check("win_probe 的 issues 里明确报出丢账",
              any(("丢弃" in x or "丢失" in x) for x in p.get("issues", [])),
              str(p.get("issues"))[:180])
        check("win_probe 自检 ok=False", p.get("ok") is False, text[:180])
        rendered = m.render("win_probe", p)
        # 不能只查「丢弃 N 条」这行固定计数 —— 那是常态文本，0 条也会绿。
        check("MCP 文本渲染里也有丢账告警（非常态文本）",
              "审计告警" in rendered, rendered[:260])
        json_safe("带告警的 probe 响应仍可序列化", {"ok": True, "data": p})
    finally:
        shutil.rmtree(base, ignore_errors=True)


# ---------------------------------------------------------------------------
def test_probe_redaction():
    print("\n========== 6. 只读 Token 的 win_probe 不回绝对路径 ==========")
    base = tempfile.mkdtemp(prefix="wk-redact-")
    try:
        share = os.path.join(base, "share")
        os.makedirs(share)
        a, _ = make_agent(base, share)
        # JSON 里反斜杠会被转义，直接拿原始路径比对会假绿
        roots_needle = json.dumps(share, ensure_ascii=False)[1:-1]
        backup_needle = json.dumps(wa.backup_root(base), ensure_ascii=False)[1:-1]
        full = a.op_probe({})
        check("完整 Token 仍看得到完整 roots",
              roots_needle in json.dumps(full, ensure_ascii=False))
        a.set_request_mode(True)
        ro = a.op_probe({})
        blob = json.dumps(ro, ensure_ascii=False)
        check("只读 Token 的 probe 不含 roots 绝对路径", roots_needle not in blob,
              blob[:220])
        check("只读 Token 的 probe 不含备份绝对路径", backup_needle not in blob,
              blob[:220])
        check("只读 Token 的 probe 标注了脱敏",
              bool(ro.get("redacted"))
              or bool((ro.get("security") or {}).get("probe_redacted")),
              str(ro.get("security")))
        check("脱敏后仍保留状态信息", len(ro.get("roots", [])) == 1
              and "exists" in ro["roots"][0])
        json_safe("脱敏后的 probe 响应可序列化", {"ok": True, "data": ro})
    finally:
        shutil.rmtree(base, ignore_errors=True)


# ---------------------------------------------------------------------------
def test_docs():
    print("\n========== 7. 文档一致性 ==========")
    readme = os.path.join(ROOT, "README.md")
    text = open(readme, "r", encoding="utf-8").read()
    nums = set(re.findall(r"features_e2e\.py[^\n]*?(\d{3})\s*用例", text))
    nums |= {x for pair in re.findall(r"features\s+(\d{3})/(\d{3})", text)
             for x in pair}
    check("README 里 features 用例数只有一种口径", len(nums) == 1,
          "出现 %s" % sorted(nums))
    check("README 测试命令块含 v1723_test.py", "v1723_test.py" in text)
    # v1.7.27 起，文档不能只做形状断言 —— 要验真内容，否则会和代码再次脱节
    check("README 记录 v1.7.27 这轮修复", "v1.7.27" in text)
    check("README 写明 port 脏值拒绝启动（不再静默回退）",
          "port" in text and "拒绝启动" in text)
    check("README 写明 type/copy 需进白名单（不是围栏漏判）",
          "type" in text and "白名单" in text and "cmd 内建" in text)
    quick = open(os.path.join(ROOT, "README-快速上手.txt"), "r",
                 encoding="utf-8").read()
    check("快速上手不再超卖「再也借不到道」", "再也借不到道" not in quick)
    check("快速上手补了 backup_required 配置说明",
          "backup_required" in quick and "强制先备份再写" in quick)
    check("快速上手仍写明备份只能由运维清、AI 无删备份权限",
          "备份只能由运维" in quick)

    spec = importlib.util.spec_from_file_location(
        "build_release", os.path.join(ROOT, "build-release.py"))
    br = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(br)
    tmp = tempfile.mkdtemp(prefix="wk-crlf-")
    try:
        dst = os.path.join(tmp, "sample.txt")
        br.write_text(dst, "第一行\n第二行\n")
        b = open(dst, "rb").read()
        check("write_text 不再产生 \\r\\r\\n", b"\r\r\n" not in b, repr(b))
        check("write_text 仍是 CRLF", b"\r\n" in b, repr(b))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ---------------------------------------------------------------------------
def test_contracts():
    print("\n========== 8. 新增字段的 JSON 契约 ==========")
    base = tempfile.mkdtemp(prefix="wk-contract23-")
    try:
        share = os.path.join(base, "share")
        os.makedirs(share)
        a, _ = make_agent(base, share, backup_required=True)
        p = os.path.join(share, "a.txt")
        put_raw(p, b"a=1\n")
        json_safe("write 响应可序列化",
                  {"ok": True, "data": a.op_write({"path": p, "content": "a=2\n"})})
        json_safe("patch 响应可序列化", {"ok": True, "data": a.op_patch({
            "changes": [{"path": p, "old": "2", "new": "3"}]})})
        json_safe("backup_list 响应可序列化",
                  {"ok": True, "data": a.op_backup_list({})})
        for ro in (False, True):
            a.set_request_mode(ro)
            pr = a.op_probe({})
            json_safe("probe(readonly=%s) 响应可序列化" % ro,
                      {"ok": True, "data": pr})
            res = m.call_tool(a.dispatch, {"name": "win_probe", "arguments": {}})
            json_safe("MCP win_probe(readonly=%s) 响应可序列化" % ro, res)
    finally:
        shutil.rmtree(base, ignore_errors=True)


def main():
    test_exec_dynamic()
    test_rollback_cap_zero()
    test_backup_required()
    test_backup_inode_volume()
    test_audit_dropped_alert()
    test_probe_redaction()
    test_docs()
    test_contracts()
    print("\n========== 汇总 ==========")
    print("通过 %d / 失败 %d / 环境跳过 %d" % (len(PASS), len(FAIL), len(SKIP)))
    if FAIL:
        for name in FAIL:
            print("  [FAIL] " + name)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
