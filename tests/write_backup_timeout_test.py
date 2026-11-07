# -*- coding: utf-8 -*-
"""v1.7.19：写类工具 backup=false 一致性 + win_exec 超时上限可配。

行为断言优先：真的调 op_append / op_replace / op_exec，数备份目录里的文件，
而不是只检查代码里有没有某个字符串。
"""
import ast
import json
import os
import sys
import tempfile
import shutil

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "server"))
import wkfile_agent as wa  # noqa: E402
import wkfile_mcp_endpoint as m  # noqa: E402

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print("  %s %s%s" % ("[OK]  " if cond else "[FAIL]", name,
                         "  :: %s" % detail if detail and not cond else ""))


def make_agent(base, share, **over):
    data = {
        "roots": [share], "max_read_bytes": 1 << 20,
        "max_write_bytes": 8 << 20, "auto_backup": True,
        "exec_enabled": True, "exec_allowed_commands": ["type"],
        "exec_timeout_sec": 60, "exec_max_output_bytes": 65536,
    }
    data.update(over)
    cfg = wa.Config(data, os.path.join(base, "c.json"))
    cfg.base_dir = base
    return wa.Agent(cfg), cfg


def backup_files(base):
    """数「历史版本文件」——.objects 是 v1.7.21 增量备份的内容实体库，
    不是一次改写产生的一份备份，不能混进这个计数里。"""
    root = os.path.join(base, wa.BACKUP_DIRNAME)
    out = []
    for cur, dirs, files in os.walk(root):
        dirs[:] = [d for d in dirs if d != ".objects"]
        out.extend(os.path.join(cur, f) for f in files)
    return out


def test_backup_opt_out():
    """四个写工具都能按次跳过备份，且磁盘上确实没有多出备份文件。"""
    print("\n========== 1. backup=false 真实行为（append/replace） ==========")
    base = tempfile.mkdtemp(prefix="wk-backupopt-")
    try:
        share = os.path.join(base, "share")
        os.makedirs(share)
        p = os.path.join(share, "app.log")
        with open(p, "w", encoding="utf-8") as fh:
            fh.write("alpha\nbeta\n")
        a, _ = make_agent(base, share)

        # 默认行为不变：仍然备份
        r = a.op_append({"path": p, "content": "gamma\n"})
        check("append 默认生成备份", bool(r["backup"])
              and os.path.isfile(r["backup"]), str(r.get("backup")))
        check("append 默认确实写了备份文件", len(backup_files(base)) == 1,
              str(len(backup_files(base))))

        # backup=false：不复制、但内容照样落盘
        before = len(backup_files(base))
        r = a.op_append({"path": p, "content": "delta\n", "backup": False})
        check("append backup=false 返回 backup=None", r["backup"] is None)
        check("append backup=false 不多出备份文件",
              len(backup_files(base)) == before, str(len(backup_files(base))))
        check("append backup=false 内容仍写入",
              open(p, encoding="utf-8").read().endswith("gamma\ndelta\n"))

        # replace 同理
        r = a.op_replace({"path": p, "old": "beta", "new": "BETA"})
        check("replace 默认生成备份", bool(r["backup"])
              and os.path.isfile(r["backup"]))
        before = len(backup_files(base))
        r = a.op_replace({"path": p, "old": "BETA", "new": "BETA2",
                          "backup": False})
        check("replace backup=false 返回 backup=None", r["backup"] is None)
        check("replace backup=false 不多出备份文件",
              len(backup_files(base)) == before)
        check("replace backup=false 内容仍改动",
              "BETA2" in open(p, encoding="utf-8").read())

        # 大文件追加：跳过备份时不该复制几 MB
        big = os.path.join(share, "huge.log")
        with open(big, "wb") as fh:
            fh.write(b"x" * (3 << 20))
        before = len(backup_files(base))
        r = a.op_append({"path": big, "content": "tail\n", "backup": False})
        check("3MB 文件追加 backup=false 不复制", len(backup_files(base)) == before)
        check("3MB 文件追加后体积正确",
              os.path.getsize(big) == (3 << 20) + len(b"tail\n"))

        # dry_run / 未命中：本来就不写盘，更不能凭空多出备份
        before = len(backup_files(base))
        a.op_replace({"path": p, "old": "NO_SUCH", "new": "x", "backup": False})
        a.op_replace({"path": p, "old": "BETA2", "new": "Z",
                      "dry_run": True, "backup": False})
        check("未命中/dry_run 不产生备份", len(backup_files(base)) == before)

        # 对齐 op_write：现在四个写工具口径一致
        for name, req in (("write", {"path": p, "content": "w\n"}),
                          ("append", {"path": p, "content": "a\n"}),
                          ("replace", {"path": p, "old": "w", "new": "W"})):
            if name == "write":
                before = len(backup_files(base))
                a.op_write(dict(req, backup=False))
                check("write backup=false 仍一致", len(backup_files(base)) == before)

        # auto_backup=false 时即便不传 backup 也不备份
        a2, _ = make_agent(base, share, auto_backup=False)
        before = len(backup_files(base))
        a2.op_append({"path": p, "content": "z\n"})
        check("auto_backup=false 时 append 不备份",
              len(backup_files(base)) == before)
    finally:
        shutil.rmtree(base, ignore_errors=True)


def test_schema_and_contract():
    print("\n========== 2. schema / 契约与实现口径一致 ==========")
    tools = {t["name"]: t for t in m.TOOLS}
    for name in ("win_write", "win_replace", "win_append", "win_write_b64"):
        props = tools[name]["inputSchema"]["properties"]
        check("%s schema 声明 backup 参数" % name,
              "backup" in props and props["backup"]["type"] == "boolean")

    info = {
        "server_name": "S", "version": wa.VERSION, "roots": [],
        "token_scope": "full",
        "limits": {"max_read_bytes": 4 << 20, "max_write_bytes": 16 << 20,
                   "structured_omit_bytes": 4096, "upload_hint_bytes": 8192},
        "flags": {"auto_backup": True, "allow_delete": True,
                  "exec_enabled": True, "exec_allowed_commands": ["php"],
                  "exec_timeout_sec": 60, "exec_timeout_max_sec": 900,
                  "exec_strict_chain": False},
    }
    txt = m.runtime_contract(info)
    check("契约写明 backup=false", "backup=false" in txt)
    check("契约 backup 口径不再只点名两个工具",
          "写类工具均可传 backup=false" in txt)
    check("契约下发本次上限 900 秒", "上限 900 秒" in txt)
    check("契约体积仍 < 1.5KB", len(txt.encode("utf-8")) < 1536,
          str(len(txt.encode("utf-8"))))
    ins = m.build_initialize({}, info=info)["instructions"]
    check("整个 instructions < 3KB", len(ins.encode("utf-8")) < 3072,
          str(len(ins.encode("utf-8"))))

    # 默认（exec 关闭）配置也要守住体积护栏
    info["flags"] = {"auto_backup": True, "allow_delete": True,
                     "exec_enabled": False, "exec_allowed_commands": [],
                     "exec_timeout_sec": 60, "exec_timeout_max_sec": 300,
                     "exec_strict_chain": False}
    txt_default = m.runtime_contract(info)
    check("默认契约体积 < 1.5KB",
          len(txt_default.encode("utf-8")) < 1536,
          str(len(txt_default.encode("utf-8"))))
    check("exec 关闭时契约不重复讲 exec 细节",
          "win_exec 已开启" not in txt_default)

    exec_desc = tools["win_exec"]["description"]
    exec_timeout_prop = tools["win_exec"]["inputSchema"]["properties"]["timeout"]
    check("schema 写明长任务拆批", "长任务请拆成小批" in exec_desc
          and "拆批" in exec_timeout_prop["description"])
    check("schema 不再写死最大 300",
          "最大 300" not in exec_timeout_prop["description"]
          and "exec_timeout_max_sec" in exec_timeout_prop["description"])


def _raises_bad_request(agent, req):
    try:
        agent._exec_timeout(req)
    except wa.PathError as exc:
        return exc.code == "bad_request"
    return False


def test_timeout_cap():
    print("\n========== 3. 超时上限纯函数 + 真实 op_exec 夹取 ==========")
    F = wa.exec_timeout_cap
    for bad in (None, "", "abc", 0, -1, [], {}):
        check("脏值 %r 回落默认 300" % (bad,), F(bad) == 300, str(F(bad)))
    check("正常值 900 生效", F(900) == 900)
    check("字符串数字可用", F("120") == 120)
    check("超大值夹到硬顶 3600", F(999999999) == wa.EXEC_TIMEOUT_HARD_CAP)
    check("自定义 default 生效", F(None, default=120) == 120)

    # 真实 Agent 上算超时（纯逻辑，不启进程、不碰 subprocess）
    base = tempfile.mkdtemp(prefix="wk-timeout-")
    try:
        share = os.path.join(base, "share")
        os.makedirs(share)
        a, cfg = make_agent(base, share, exec_timeout_max_sec=45,
                            exec_timeout_sec=20)
        check("默认 timeout 取 exec_timeout_sec=20", a._exec_timeout({}) == 20)
        check("请求 timeout 被新上限 45 夹住（旧版是 300）",
              a._exec_timeout({"timeout": 9999}) == 45)
        check("按调用调小仍生效", a._exec_timeout({"timeout": 5}) == 5)
        check("非整数 timeout 报 bad_request",
              _raises_bad_request(a, {"timeout": "abc"}))

        # 热加载改上限，下次调用立即生效
        cfg.exec_timeout_max_sec = 10
        check("热加载改上限后下次调用生效",
              a._exec_timeout({"timeout": 9999}) == 10)
        cfg.exec_timeout_max_sec = "garbage"
        check("脏配置不抛异常并回落 300",
              a._exec_timeout({"timeout": 9999}) == 300)
        cfg.exec_timeout_max_sec = 0
        check("配置为 0 也回落 300", a._exec_timeout({"timeout": 9999}) == 300)
        cfg.exec_timeout_max_sec = 999999999
        check("超大配置夹到硬顶 3600",
              a._exec_timeout({"timeout": 999999999}) == wa.EXEC_TIMEOUT_HARD_CAP)
        cfg.exec_timeout_max_sec = 45
        cfg.exec_timeout_sec = 100
        check("exec_timeout_sec 大于上限时也被夹住",
              a._exec_timeout({}) == 45)
    finally:
        shutil.rmtree(base, ignore_errors=True)


def test_config_keys():
    print("\n========== 4. 配置键集合 / 热加载白名单 ==========")
    src = open(os.path.join(ROOT, "server", "wkfile_agent.py"),
               encoding="utf-8").read()
    tree = ast.parse(src)
    node = next(n for n in tree.body if isinstance(n, ast.Assign)
                and any(isinstance(t, ast.Name) and t.id == "DEFAULT_CONFIG"
                        for t in n.targets))
    keys = {k.value for k in node.value.keys if isinstance(k, ast.Constant)}
    ex = json.load(open(os.path.join(ROOT, "server",
                                    "wkfile.config.example.json"),
                        encoding="utf-8"))
    check("示例配置与 DEFAULT_CONFIG 键集合一致", keys == set(ex),
          "缺=%s 多=%s" % (sorted(keys - set(ex)), sorted(set(ex) - keys)))
    check("新键默认值 300 = 保持旧行为",
          ex.get("exec_timeout_max_sec") == 300)
    check("新键在热加载白名单里",
          "exec_timeout_max_sec" in wa.Agent._EXEC_HOT_KEYS)
    check("配置注释里有新键说明",
          '"exec_timeout_max_sec"' in src)


if __name__ == "__main__":
    test_backup_opt_out()
    test_schema_and_contract()
    test_timeout_cap()
    test_config_keys()
    print("\nWRITE_BACKUP_TIMEOUT_TEST: %d PASS / %d FAIL" % (len(PASS), len(FAIL)))
    raise SystemExit(1 if FAIL else 0)
