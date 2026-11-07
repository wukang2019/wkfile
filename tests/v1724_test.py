# -*- coding: utf-8 -*-
"""v1.7.24 契约回归：备份孤儿超删（必修） + win_exec 已知限制（写明，不加固）。

外部体检第三轮的结论：
  ① 开了硬链接去重后 .objects 会留孤儿（os.link 失败是代码自己承认的正常
     状态）。孤儿被算进 physical，但删版本永远减不掉这部分字节，于是
     backup_max_bytes 下清理会一直删到把有效备份全删光、孤儿还留着。
     win_backup_list 的 would_remove 又因为没传 physical 而报 0，与实际不符。
  ② exec 围栏在这条线上已经往返四次（堵死→误伤→收窄→又漏）。不再用
     正则/词法继续加固：cmd 的 if / for / 括号 / 重定向 / 变量展开无法静态
     穷举。本轮只把承诺降级成「尽力而为的静态检查，不是安全边界」。

本文件先于产品代码写成，修复前应当成片报红。
"""
import json
import os
import shutil
import sys
import tempfile
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "server"))
import wkfile_agent as wa  # noqa: E402
import wkfile_mcp_endpoint as m  # noqa: E402

PASS, FAIL = [], []
BLOCK_CODES = ("exec_hijack_suspected", "exec_dynamic_program")


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print("  %s %s%s" % ("[OK]  " if cond else "[FAIL]", name,
                         "  :: %s" % detail if detail and not cond else ""))


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
            "max_write_bytes": 16 << 20, "auto_backup": True}
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


def read_text(path):
    with open(path, "r", encoding="utf-8") as fh:
        return fh.read().replace("\r\n", "\n")


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


def stamps_of(broot):
    return sorted(d for d in os.listdir(broot)
                  if not d.startswith(".") and os.path.isdir(os.path.join(broot, d)))


def make_orphan_scenario(base, *, orphan_bytes=2 << 20, versions=2, version_bytes=10):
    """2MB 孤儿 + N 个有效版本；上限 1.5MB 时旧逻辑会把版本全删光。"""
    share = os.path.join(base, "share")
    os.makedirs(share, exist_ok=True)
    broot = os.path.join(base, wa.BACKUP_DIRNAME)
    orphan = os.path.join(broot, ".objects", "ab", "orphan.bin")
    put_raw(orphan, b"O" * orphan_bytes)       # nlink == 1，没有任何版本引用
    for i in range(versions):
        put_raw(os.path.join(broot, "2026110%d-120000-000" % i, "f.txt"),
                b"v" * version_bytes)
    return share, broot, orphan


# ---------------------------------------------------------------------------
def test_orphan_no_overdelete():
    print("\n========== 1. 孤儿实体不能让清理删光有效备份（启动清理） ==========")
    base = tempfile.mkdtemp(prefix="wk-orphan24-")
    try:
        share, broot, orphan = make_orphan_scenario(base)
        # Agent 构造时就会跑一次启动清理 —— 这正是「重启后安全网静默消失」
        a, _cfg = make_agent(base, share, backup_max_bytes=1500000,
                             backup_dedup=True)
        left = stamps_of(broot)
        check("2MB 孤儿 + 1.5MB 上限：两个有效版本都还在",
              len(left) == 2, "剩余 %s" % left)
        check("孤儿实体已被回收（不再白占额度）", not os.path.isfile(orphan))
        pol = a._backup_policy()
        # 「将删除几个」必须等于下次清理真正会删的份数
        check("would_remove 与实际删除份数一致",
              pol["would_remove"] == 2 - len(left),
              "would_remove=%s 实际删=%s" % (pol["would_remove"], 2 - len(left)))
        check("physical_bytes 不含孤儿体积",
              pol["physical_bytes"] < 1000, str(pol["physical_bytes"]))
        check("physical_bytes == total_bytes（同一个口径）",
              pol["physical_bytes"] == pol["total_bytes"], str(pol))
        listed = a.op_backup_list({})
        json_safe("win_backup_list 响应可序列化且不含 bytes", listed)
        rendered = m.render("win_backup_list", listed)
        check("文本块也报对份数", "按策略会清掉 0 份" in rendered,
              rendered[:200])
    finally:
        shutil.rmtree(base, ignore_errors=True)


def test_prune_failed_fallback():
    print("\n========== 2. prune 失败（权限/竞态）时也不能拿有效备份陪葬 ==========")
    base = tempfile.mkdtemp(prefix="wk-noprune24-")
    try:
        share, broot, orphan = make_orphan_scenario(base)
        a, cfg = make_agent(base, share, backup_max_bytes=0)  # 启动不删版本
        # 启动清理本身就会回收孤儿；这里重新投放一份，模拟「prune 因权限或
        # 竞态完全失败、孤儿仍然残留」的最坏情况。不这么做上面那条断言是假绿。
        put_raw(orphan, b"O" * (2 << 20))
        cfg.backup_max_bytes = 1500000
        a._prune_backup_objects = lambda: 0   # 模拟 prune 完全失败
        a.cleanup_backups()
        left = stamps_of(broot)
        check("prune 失败时有效版本仍全部保留", len(left) == 2, "剩余 %s" % left)
        check("孤儿本身仍在（没权限删，但不能算进额度）",
              os.path.isfile(orphan))
        pol = a._backup_policy()
        check("physical_bytes 排除删不掉的孤儿",
              pol["physical_bytes"] < 1000, str(pol["physical_bytes"]))
        check("不可回收占用被如实报出来",
              int(pol.get("unreclaimable_bytes", 0)) >= (2 << 20),
              str(pol.get("unreclaimable_bytes")))
        check("孤儿个数被如实报出来",
              int(pol.get("orphan_objects", 0)) == 1,
              str(pol.get("orphan_objects")))
        json_safe("带孤儿告警的 policy 可序列化", pol)
        rendered = m.render("win_backup_list", a.op_backup_list({}))
        check("文本块如实报出孤儿且不虚报删除",
              "无版本引用的内容实体" in rendered
              and "按策略会清掉 0 份" in rendered, rendered[:240])
    finally:
        shutil.rmtree(base, ignore_errors=True)


def test_plan_guard_and_policy_parity():
    print("\n========== 3. 纯函数护栏 / 正常策略不回退 / 口径一致 ==========")
    P = wa.Agent._backup_cleanup_plan
    now = 1000000
    entries = [{"stamp": "s%d" % i, "size": 40, "mtime": now} for i in range(5)]
    check("正常体积策略不受影响（100B 上限删 3 份）",
          len(P(entries, max_bytes=100, now=now)) == 3)
    check("存在删不掉的占用时按体积一份都不删",
          P(entries, max_bytes=100, total_bytes=(2 << 20) + 200, now=now) == [],
          str(P(entries, max_bytes=100, total_bytes=(2 << 20) + 200, now=now)))
    check("份数/天龄维度不受体积护栏影响",
          len(P(entries, max_bytes=100, retain_count=2,
                total_bytes=(2 << 20) + 200, now=now)) == 3)
    check("不可回收占用低于上限时仍按体积清理",
          len(P(entries, max_bytes=100, total_bytes=200 + 20, now=now)) == 3)

    base = tempfile.mkdtemp(prefix="wk-parity24-")
    try:
        share = os.path.join(base, "share")
        os.makedirs(share)
        broot = os.path.join(base, wa.BACKUP_DIRNAME)
        for i in range(5):
            put_raw(os.path.join(broot, "2026110%d-120000-000" % i, "f.txt"), b"x" * 10)
        a, _cfg = make_agent(base, share, backup_max_bytes=0)  # 启动不删
        cfg = a.cfg
        cfg.backup_max_bytes = 25
        pol = a._backup_policy()
        check("policy 报出按体积会清 3 份", pol["would_remove"] == 3, str(pol))
        before = len(stamps_of(broot))
        a.cleanup_backups()
        after = len(stamps_of(broot))
        check("policy 的 would_remove 等于 cleanup 实际删除数",
              pol["would_remove"] == before - after,
              "policy=%s 实际=%s" % (pol["would_remove"], before - after))
        check("保留最新两份", after == 2, str(stamps_of(broot)))
    finally:
        shutil.rmtree(base, ignore_errors=True)


# ---------------------------------------------------------------------------
KNOWN_BYPASS = [
    "dir && if exist payload.bat payload.bat",
    "dir && (payload.bat)",
    "dir && >nul payload.bat",
]


def test_exec_known_limits():
    print("\n========== 4. exec 已知限制：登记现状、不加固、写清口径 ==========")
    base = tempfile.mkdtemp(prefix="wk-limit24-")
    try:
        share = os.path.join(base, "share")
        os.makedirs(share)
        a, _ = make_agent(base, share, exec_enabled=True,
                          exec_allowed_commands=["echo", "dir", "type", "copy"])
        put_raw(os.path.join(share, "payload.bat"), b"@echo pwned\n")
        put_raw(os.path.join(share, "deploy.ps1"), b"Write-Output ok\n")
        # 这三条是刻意不修的已知限制：把现状钉死，避免以后有人误以为拦了。
        for command in KNOWN_BYPASS:
            code, msg = err_of(lambda c=command: a._exec_guard_hijack(c, share))
            check("已知限制登记（当前放行，不修）：%s" % command,
                  code is None, "code=%s msg=%s" % (code, msg[:100]))
        # 已有能力不能因为「写明限制」而回退
        for command, name in [
            ("dir && call payload.bat", "call 字面量"),
            ("set X=payload.bat\ncall %X%", "动态程序名"),
            ("dir && .\\payload.bat", "带路径脚本引用"),
        ]:
            code, _ = err_of(lambda c=command: a._exec_guard_hijack(c, share))
            check("既有拦截仍生效：%s" % name, code in BLOCK_CODES, "code=%s" % code)
        for command, name in [("type deploy.ps1", "读 .ps1 参数"),
                              ("copy payload.bat x", "复制 .bat 参数")]:
            code, _ = err_of(lambda c=command: a._exec_guard_hijack(c, share))
            check("既有不误伤仍生效：%s" % name, code is None, "code=%s" % code)
    finally:
        shutil.rmtree(base, ignore_errors=True)


def test_limit_wording():
    print("\n========== 5. 所有 AI 能看到的口径都写明「不是安全边界」 ==========")
    readme = read_text(os.path.join(ROOT, "README.md"))
    quick = read_text(os.path.join(ROOT, "README-快速上手.txt"))
    desc = next(t["description"] for t in m.TOOLS if t["name"] == "win_exec")
    contract_on = m.runtime_contract({
        "version": "1.7.24",
        "flags": {"exec_enabled": True, "exec_allowed_commands": ["php", "composer"]},
    })
    doc = wa.Agent._exec_guard_hijack.__doc__ or ""
    sources = [("README", readme), ("快速上手", quick),
               ("win_exec schema", desc), ("initialize 运行时契约", contract_on),
               ("围栏源码 docstring", doc)]
    for label, text in sources:
        check("%s 明确写了不是安全边界" % label, "不是安全边界" in text,
              text[:180])
    check("README 明确标出「已知限制」", "已知限制" in readme)
    for command in KNOWN_BYPASS:
        token = command.split("&& ")[-1]
        check("README 逐条列出已知绕过：%s" % token,
              token in readme, "缺 %s" % token)
    check("README 不再说「全链拦截」", "全链拦截" not in readme)
    check("README 不再说「每段都查」", "每段都查" not in readme)
    check("README 不再说「只放行能静态判定」",
          "只放行能静态判定程序的命令" not in readme)
    check("快速上手不再说「只放行能静态判定」",
          "只放行能静态" not in quick)
    check("README 给出未来方向（typed 命令）", "typed" in readme.lower())
    check("运行时契约仍在 1.5KB 护栏内",
          len(contract_on.encode("utf-8")) < 1536,
          str(len(contract_on.encode("utf-8"))))
    long_flags = {"exec_enabled": True, "exec_strict_chain": True,
                  "exec_allowed_commands": ["php", "composer", "dir", "type",
                                            "where", "findstr", "curl", "netstat"]}
    long_contract = m.runtime_contract({"version": "1.7.24", "flags": long_flags})
    check("长白名单 + 严格链式时契约也不超 1.5KB",
          len(long_contract.encode("utf-8")) < 1536,
          str(len(long_contract.encode("utf-8"))))
    check("退化后仍写明不是安全边界", "不是安全边界" in long_contract,
          long_contract[-160:])
    check("契约开启态仍报白名单与超时", "win_exec 已开启" in contract_on
          and "超时" in contract_on, contract_on[-160:])


def main():
    test_orphan_no_overdelete()
    test_prune_failed_fallback()
    test_plan_guard_and_policy_parity()
    test_exec_known_limits()
    test_limit_wording()
    print("\nV1724_TEST: %d PASS / %d FAIL" % (len(PASS), len(FAIL)))
    if FAIL:
        print("FAILED:")
        for name in FAIL:
            print("  - %s" % name)
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
