# -*- coding: utf-8 -*-
"""v1.7.18 备份保留策略回归（纯函数 + 真实 Agent 目录清理，无服务、无窗口）。

覆盖外部反馈的「积了几百份小文件、只有 list/restore 没有清理」：
旧逻辑只按 backup_max_bytes 体积清，小文件远不到 2GB 就永远不清。
"""
import ast
import json
import os
import sys
import tempfile
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "server"))
import wkfile_agent as wa  # noqa: E402
import wkfile_mcp_endpoint as m  # noqa: E402

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print("  %s %s%s" % ("[OK]  " if cond else "[FAIL]", name,
                         "  :: %s" % detail if detail and not cond else ""))


def entries(n, size=10, age_days=0, now=None):
    now = time.time() if now is None else now
    return [{"stamp": "stamp-%03d" % i, "size": size,
             "mtime": now - age_days * 86400} for i in range(n)]


def test_plan():
    print("\n========== 1. 清理计划纯函数 ==========")
    P = wa.Agent._backup_cleanup_plan
    check("三个维度全 0 不删", P(entries(5)) == [])
    check("只按份数：保留 2 份删最旧 3 份",
          P(entries(5), retain_count=2) == ["stamp-000", "stamp-001", "stamp-002"])
    check("份数未超不删", P(entries(2), retain_count=2) == [])
    check("只按体积：100B 上限、5×40B 删 3 份",
          P(entries(5, size=40), max_bytes=100, now=1000000)
          == ["stamp-000", "stamp-001", "stamp-002"])
    check("体积未超不删", P(entries(5, size=10), max_bytes=1000) == [])
    now = 2000000
    old = entries(3, age_days=40, now=now) + entries(2, age_days=1, now=now)
    check("只按天龄：30 天前全删、近期保留",
          P(old, retain_days=30, now=now) == ["stamp-000", "stamp-001", "stamp-002"])
    check("天龄未超不删", P(entries(3, age_days=1, now=now),
                        retain_days=30, now=now) == [])
    check("三维度 OR：任一超就删",
          len(P(entries(6, size=40), max_bytes=1000, retain_days=0,
                retain_count=3, now=now)) == 3)
    check("空列表安全", P([]) == [])


def make_base(n=5):
    base = tempfile.mkdtemp(prefix="wk-bkret-")
    share = os.path.join(base, "share")
    os.makedirs(share)
    broot = os.path.join(base, wa.BACKUP_DIRNAME)
    for i in range(n):
        d = os.path.join(broot, "2026110%d-120000-000" % i)
        os.makedirs(d)
        with open(os.path.join(d, "f.txt"), "w", encoding="utf-8") as fh:
            fh.write("x" * 10)
    return base, share, broot


def make_agent(base, share, **over):
    data = {"roots": [share], "max_read_bytes": 1 << 20,
            "max_write_bytes": 1 << 20, "backup_max_bytes": 0}
    data.update(over)
    cfg = wa.Config(data, os.path.join(base, "c.json"))
    cfg.base_dir = base           # 让数据目录落在临时目录里
    return wa.Agent(cfg), cfg


def test_real_cleanup():
    print("\n========== 2. 真实目录清理（小文件，体积远不到上限） ==========")
    base, share, broot = make_base(5)
    a, cfg = make_agent(base, share, backup_retain_count=2)
    a.cleanup_backups()
    left = sorted(os.listdir(broot))
    check("按份数真的删到只剩 2 份", len(left) == 2, str(left))
    check("保留的是最新两份", left == ["20261103-120000-000", "20261104-120000-000"], str(left))

    base2, share2, broot2 = make_base(4)
    # 天龄：把前两个目录 mtime 改成 40 天前
    for i in (0, 1):
        d = os.path.join(broot2, "2026110%d-120000-000" % i)
        old = time.time() - 40 * 86400
        os.utime(d, (old, old))
    a2, _ = make_agent(base2, share2, backup_retain_days=30)
    a2.cleanup_backups()
    left2 = sorted(os.listdir(broot2))
    check("按天龄清掉 40 天前的目录", len(left2) == 2, str(left2))
    check("近 1 天的目录保留",
          left2 == ["2026110%d-120000-000" % i for i in (2, 3)], str(left2))

    base3, share3, broot3 = make_base(5)
    a3, _ = make_agent(base3, share3, backup_max_bytes=25)
    a3.cleanup_backups()
    left3 = sorted(os.listdir(broot3))
    check("原体积维度仍生效（上限 25B，每份 10B）", len(left3) == 2, str(left3))

    base4, share4, broot4 = make_base(5)
    a4, _ = make_agent(base4, share4)   # 全 0
    a4.cleanup_backups()
    check("全 0 策略保持旧行为：一份不删", len(os.listdir(broot4)) == 5)


def test_policy_and_contract():
    print("\n========== 3. list 的只读策略快照 / 配置与工具边界 ==========")
    base, share, broot = make_base(5)
    a, _ = make_agent(base, share, backup_retain_count=2)
    # Agent 构造时已自动清过一次，重建目录后再看快照，避免清理影响计数
    for i in range(5):
        d = os.path.join(broot, "2026110%d-120000-000" % i)
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, "f.txt"), "w", encoding="utf-8") as fh:
            fh.write("x" * 10)
    pol = a._backup_policy()
    check("policy 报出总份数 5", pol["total_count"] == 5, str(pol))
    check("policy 报出按策略会清 3 份", pol["would_remove"] == 3, str(pol))
    check("policy 报出阈值与总体积",
          pol["retain_count"] == 2 and pol["total_bytes"] == 50, str(pol))
    check("policy 是只读快照（调它不删文件）", len(os.listdir(broot)) == 5)
    listed = a.op_backup_list({})
    check("win_backup_list 返回 policy", listed.get("policy", {}).get("total_count") == 5)
    empty_txt = m.render("win_backup_list", {"backup_root": "R", "count": 0,
                                            "items": [], "policy": pol})
    check("空列表时文本块也带策略快照", "会清掉 3 份" in empty_txt, empty_txt[:160])
    full_txt = m.render("win_backup_list", {"backup_root": "R", "count": 1,
                                           "policy": pol,
                                           "items": [{"stamp": "s", "size_human": "10 B",
                                                      "original_path": "o", "backup_path": "b"}]})
    check("有备份时文本块也带策略快照", "会清掉 3 份" in full_txt, full_txt[:160])
    check("文本块说明没有删除权限", "不提供删除" in full_txt)
    check("不提供 win_backup_delete 工具",
          not any(t["name"] == "win_backup_delete" for t in m.TOOLS))

    src = open(os.path.join(ROOT, "server", "wkfile_agent.py"), encoding="utf-8").read()
    tree = ast.parse(src)
    node = next(n for n in tree.body if isinstance(n, ast.Assign)
                and any(isinstance(t, ast.Name) and t.id == "DEFAULT_CONFIG"
                        for t in n.targets))
    # 用 AST 取键，别用行首正则 —— DEFAULT_CONFIG 里有「两个键写在同一行」的情况
    keys = {k.value for k in node.value.keys if isinstance(k, ast.Constant)}
    ex = json.load(open(os.path.join(ROOT, "server", "wkfile.config.example.json"),
                        encoding="utf-8"))
    check("示例配置与 DEFAULT_CONFIG 键集合一致", keys == set(ex),
          "缺=%s 多=%s" % (sorted(keys - set(ex)), sorted(set(ex) - keys)))
    check("示例配置两个新键为默认 0（不擅自删已有备份）",
          ex.get("backup_retain_days") == 0 and ex.get("backup_retain_count") == 0)


if __name__ == "__main__":
    test_plan()
    test_real_cleanup()
    test_policy_and_contract()
    print("\nBACKUP_RETENTION_TEST: %d PASS / %d FAIL" % (len(PASS), len(FAIL)))
    raise SystemExit(1 if FAIL else 0)
