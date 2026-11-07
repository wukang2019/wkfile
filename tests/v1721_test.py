# -*- coding: utf-8 -*-
"""v1.7.21 一次推平五项：行为回归（真实 Agent 调用，不用字符串断言糊弄）。

A 纯逻辑外提   B 配置分层   C 审计降级   D 增量备份
E win_validate / win_patch / win_probe（含 MCP 文本与结构化双通道）
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
import wkfile_core as core  # noqa: E402
import wkfile_mcp_endpoint as m  # noqa: E402

PASS, FAIL, SKIP = [], [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print("  %s %s%s" % ("[OK]  " if cond else "[FAIL]", name,
                         "  :: %s" % detail if detail and not cond else ""))


def serializable(obj):
    """模拟 HTTP / MCP 出站。只查 isError 拦不住响应里混进 bytes。"""
    try:
        json.dumps(obj, ensure_ascii=False, allow_nan=False)
        return True
    except (TypeError, ValueError):
        return False


def skip(name, why):
    SKIP.append(name)
    print("  [SKIP] %s :: %s" % (name, why))


def make_agent(base, share, *, env=None, **over):
    data = {
        "roots": [share], "max_read_bytes": 1 << 20,
        "max_write_bytes": 8 << 20, "auto_backup": True,
        "preflight_check": True,
    }
    data.update(over)
    cfg = wa.Config(data, os.path.join(base, "c.json"), env={} if env is None else env)
    cfg.base_dir = base
    return wa.Agent(cfg), cfg


def put(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)


def put_raw(path, data: bytes):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as fh:
        fh.write(data)


def read(path):
    with open(path, "r", encoding="utf-8") as fh:
        return fh.read()


def err_of(fn):
    try:
        fn()
        return None, ""
    except wa.PathError as exc:
        return exc.code, exc.message
    except Exception as exc:  # noqa: BLE001
        return exc.__class__.__name__, str(exc)


def test_core():
    print("\n========== A. 纯逻辑外提 ==========")
    for name in ("human_size", "sha256_of", "sha256_under_cap", "decode_text",
                 "exec_timeout_cap", "expiry_state", "token_strength",
                 "merge_config_layers", "patch_plan", "cas_object_path"):
        check("wkfile_agent.%s 就是 core 里的同一对象" % name,
              getattr(wa, name, None) is getattr(core, name, None))
    check("core 能独立 import 且不依赖 Agent",
          not hasattr(core, "Agent") and not hasattr(core, "Config"))
    check("human_size 行为不变", wa.human_size(1048576) == "1.0 MB")
    check("exec_timeout_cap 脏值回落", wa.exec_timeout_cap("garbage") == 300
          and wa.exec_timeout_cap(0) == 300 and wa.exec_timeout_cap(999999) == 3600)
    tmp = tempfile.mkdtemp(prefix="wk-core-")
    try:
        p = os.path.join(tmp, "x.txt")
        put(p, "abc")
        h, why = wa.sha256_under_cap(p, 2)
        check("sha256_under_cap 超限不硬算", h is None and "超过哈希上限" in why)
        h2, why2 = wa.sha256_under_cap(p, 100)
        check("sha256_under_cap 未超限给全文件 hash",
              h2 == wa.sha256_of(p) and not why2)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_config_layers():
    print("\n========== B. 配置分层 ==========")
    defaults = {"port": 8788, "read_only": False, "roots": [],
                "token": "", "max_read_bytes": 4096}
    merged, rep = core.merge_config_layers(
        defaults,
        {"port": 9000, "roots": ["D:/a"]},
        {"WKFILE_PORT": "9100", "WKFILE_READ_ONLY": "true",
         "WKFILE_ROOTS": '["D:/b"]', "WKFILE_TOEKN": "oops",
         "WKFILE_MAX_READ_BYTES": "not-a-number"},
    )
    check("优先级 默认 < 文件 < 环境变量", merged["port"] == 9100)
    check("文件层生效（未被 env 覆盖的键）", rep["sources"]["token"] == "default"
          and rep["sources"]["port"] == "env")
    check("bool 环境变量按类型转", merged["read_only"] is True)
    check("list 环境变量按 JSON 转", merged["roots"] == ["D:/b"])
    check("env 来源记录准确", rep["env_applied"] == ["port", "read_only", "roots"])
    check("拼错的键被忽略且告警", any("WKFILE_TOEKN" in x for x in rep["env_warnings"]))
    check("脏值被忽略且告警", any("期望整数" in x for x in rep["env_warnings"]))
    check("未知键不会混进 merged", "toekn" not in merged)
    check("脏值不会污染 merged", merged["max_read_bytes"] == 4096)

    val, err = core.coerce_env_value("OFF", True)
    check("coerce bool 认 OFF", val is False and not err)
    val, err = core.coerce_env_value("1.5", 3)
    check("coerce int 拒绝小数", val is None and err)

    base = tempfile.mkdtemp(prefix="wk-layer-")
    try:
        share = os.path.join(base, "share")
        os.makedirs(share)
        env = {"WKFILE_PORT": "9111", "WKFILE_READ_ONLY": "true",
               "WKFILE_TOEKN": "typo"}
        a, cfg = make_agent(base, share, env=env)
        check("Config 真的应用了 env 覆盖", cfg.port == 9111 and cfg.read_only is True)
        rep2 = cfg.layers_report()
        check("layers_report 不泄露任何值",
              "token" not in str(rep2) and "9111" not in str(rep2))
        check("layers_report 报告拼错的键", any("WKFILE_TOEKN" in x
                                             for x in rep2["env_warnings"]))
        # save 不固化环境覆盖：写出的文件里不该出现 9111
        cfg.save()
        saved = read(cfg.config_path)
        check("save 不把环境变量固化进配置文件", "9111" not in saved)
        saved_cfg = json.loads(saved)
        check("save 仍保留原文件里的其它键", "roots" in saved_cfg)

        # 热加载不能把 env 覆盖冲掉
        hot = os.path.join(base, "hot.json")
        put(hot, json.dumps({"exec_timeout_sec": 77}))
        cfg2 = wa.Config({"exec_timeout_sec": 60}, hot, env={"WKFILE_EXEC_TIMEOUT_SEC": "25"})
        a2 = wa.Agent(cfg2)
        cfg2.base_dir = base
        a2._exec_cfg_mtime = None
        a2._hotload_exec_cfg()
        check("热加载不冲掉环境变量覆盖", cfg2.exec_timeout_sec == 25,
              str(cfg2.exec_timeout_sec))
        # 未被 env 覆盖的键仍可热加载
        put(hot, json.dumps({"exec_max_output_bytes": 12345}))
        os.utime(hot, (time.time() + 2, time.time() + 2))
        a2._hotload_exec_cfg()
        check("热加载仍能更新未覆盖的键", cfg2.exec_max_output_bytes == 12345,
              str(cfg2.exec_max_output_bytes))
    finally:
        shutil.rmtree(base, ignore_errors=True)


def test_audit_degrade():
    print("\n========== C. 审计降级 ==========")
    base = tempfile.mkdtemp(prefix="wk-audit-")
    try:
        share = os.path.join(base, "share")
        os.makedirs(share)
        a, cfg = make_agent(base, share, audit_buffer_max=3)
        audit = a.audit
        audit.path = os.path.join(base, "logs", "audit.jsonl")
        os.makedirs(os.path.dirname(audit.path), exist_ok=True)
        audit.write({"op": "first"})
        check("正常写入后 health=ok", audit.health()["state"] == "ok")
        check("正常写入确实落盘", "first" in read(audit.path))

        # 把目标变成一个目录：open(..., "a") 必失败
        blocker = os.path.join(base, "blocked")
        os.makedirs(blocker)
        audit.path = blocker
        for i in range(5):
            audit.write({"op": "buffered", "n": i})
        h = audit.health()
        check("写盘失败进入降级", h["state"] == "degraded")
        check("缓冲受 audit_buffer_max 限制", h["buffered"] == 3, str(h))
        check("溢出计数正确", h["dropped"] == 2, str(h))
        check("失败次数被记录", h["failures"] >= 5, str(h))
        _code, _msg = err_of(lambda: audit.write({"op": "still-running"}))
        check("主操作不受影响（审计降级时 write 仍不抛异常）", _code is None, _msg[:120])

        audit.path = os.path.join(base, "logs", "audit.jsonl")
        audit.write({"op": "recovered"})
        h2 = audit.health()
        check("恢复后缓冲自动补写", h2["flushed"] == 3 and h2["buffered"] == 0, str(h2))
        text = read(audit.path)
        check("补写顺序正确（旧账在前、新账在后）",
              text.index('"buffered"') < text.index('"recovered"'))
        check("probe 能看到审计健康", a.op_probe({})["audit"]["state"] == "ok")
    finally:
        shutil.rmtree(base, ignore_errors=True)


def test_backup_dedup():
    print("\n========== D. 增量备份 ==========")
    base = tempfile.mkdtemp(prefix="wk-dedup-")
    try:
        share = os.path.join(base, "share")
        os.makedirs(share)
        a, cfg = make_agent(base, share)
        p = os.path.join(share, "app.log")
        # 必须按字节写：Windows 文本模式会把 \n 变成 \r\n，那样初始内容和
        # op_write 写进去的字节就不是「同一份内容」，去重断言会假失败。
        put_raw(p, b"same-content\n")

        # 探测本机能否建硬链接；不支持就只验证回退路径
        objtest = os.path.join(base, "link-a")
        linktest = os.path.join(base, "link-b")
        put(objtest, "x")
        can_link = True
        try:
            os.link(objtest, linktest)
        except OSError:
            can_link = False

        a.op_write({"path": p, "content": "same-content\n"})
        a.op_write({"path": p, "content": "same-content\n"})
        st = a.dedup_stats()
        if can_link:
            check("同内容重复写走去重", st["hits"] >= 1, str(st))
            objs = os.path.join(base, wa.BACKUP_DIRNAME, ".objects")
            entities = [os.path.join(c, f) for c, _d, fs in os.walk(objs) for f in fs]
            check("同内容只存一份实体", len(entities) == 1, str(entities))
            check("去重统计累计省下字节", st["saved_bytes"] > 0)
        else:
            skip("硬链接去重断言", "本机文件系统不支持 os.link，已验证自动回退")
        # 历史版本仍是可直接读取的完整文件
        listing = a.op_backup_list({})
        check("backup_list 不把 .objects 当历史版本",
              all(".objects" not in it["backup_path"] for it in listing["items"]))
        check("历史版本数量正确", listing["count"] == 2, str(listing["count"]))
        check("备份文件仍可直接读取",
              all(read(it["backup_path"]) == "same-content\n"
                  for it in listing["items"]))
        check("policy 带出去重统计", listing["policy"]["dedup"]["enabled"] is True)
        restored = a.op_backup_restore({"original_path": p})
        check("去重后回滚仍可用", os.path.isfile(p), str(restored)[:120])

        # 关掉去重：hits 不再增长
        a2, cfg2 = make_agent(base, share, backup_dedup=False)
        p2 = os.path.join(share, "b.txt")
        put(p2, "x")
        a2.op_write({"path": p2, "content": "y"})
        check("backup_dedup=false 完全不走去重", a2.dedup_stats()["hits"] == 0)

        # 超过上限不参与去重（不算哈希、直接整份复制）
        a3, cfg3 = make_agent(base, share, backup_dedup_max_bytes=2)
        p3 = os.path.join(share, "c.txt")
        put(p3, "long enough content")
        a3.op_write({"path": p3, "content": "changed"})
        check("超过去重上限直接整份复制", a3.dedup_stats()["hits"] == 0)
        check("超限场景写入仍成功", read(p3) == "changed")

        # 孤儿实体回收
        if can_link:
            bdir = os.path.join(base, wa.BACKUP_DIRNAME)
            for name in os.listdir(bdir):
                d = os.path.join(bdir, name)
                if os.path.isdir(d) and not name.startswith("."):
                    shutil.rmtree(d, ignore_errors=True)
            # v1.7.28：孤儿回收已内联进 cleanup_backups（_prune_backup_objects 删除），
            # 不再有独立方法可调用；直接触发清理并断言孤儿实体被回收。
            a.cleanup_backups()
            remain = [f for c, _d, fs in os.walk(objs) for f in fs]
            check("无引用的内容实体被回收（内联清理）", not remain,
                  "remain=%s" % remain)
    finally:
        shutil.rmtree(base, ignore_errors=True)


def test_new_tools():
    print("\n========== E. validate / patch / probe ==========")
    base = tempfile.mkdtemp(prefix="wk-tools-")
    try:
        share = os.path.join(base, "share")
        os.makedirs(share)
        a, cfg = make_agent(base, share)
        p1 = os.path.join(share, "a.json")
        p2 = os.path.join(share, "b.txt")
        put(p1, '{"x": 1}\n')
        put(p2, "hello world\n")

        v = a.op_validate({"path": p1, "content": '{"x": 2}\n'})
        check("validate 合法内容通过", v["ok"] is True and v["checked"] == 1)
        check("validate 不落盘", read(p1) == '{"x": 1}\n')
        v2 = a.op_validate({"path": p1, "content": '{broken'})
        check("validate 拦下语法错误", v2["ok"] is False
              and v2["results"][0]["code"] == "preflight_failed", str(v2))
        v3 = a.op_validate({"path": p2, "old": "NOT-THERE", "new": "x"})
        check("validate 报 old 未命中", v3["ok"] is False
              and any("没有命中" in e for e in v3["results"][0]["errors"]))
        h = wa.sha256_of(p1)
        v4 = a.op_validate({"path": p1, "content": "{}", "expected_sha256": "0" * 64})
        check("validate 报乐观锁冲突", v4["ok"] is False
              and v4["results"][0]["code"] == "conflict")
        v5 = a.op_validate({"path": p1, "content": "{}", "expected_sha256": h})
        check("validate 乐观锁匹配时通过", v5["ok"] is True)

        changes = [
            {"path": p1, "content": '{"x": 2}\n'},
            {"path": p2, "old": "hello", "new": "hi"},
        ]
        dry = a.op_patch({"changes": changes, "dry_run": True})
        check("patch dry_run 不落盘", dry["applied"] is False
              and read(p1) == '{"x": 1}\n' and read(p2) == "hello world\n")
        check("patch dry_run 给出每项预览", dry["would_apply"] == 2
              and dry["changes"][0]["bytes"] > 0)
        done = a.op_patch({"changes": changes})
        check("patch 多文件一次成功", done["applied"] is True and done["count"] == 2)
        check("patch 内容正确", read(p1) == '{"x": 2}\n' and read(p2) == "hi world\n")
        check("patch 每份都有写前备份", all(c["backup"] for c in done["changes"]))

        amb = a.op_validate({"path": p1, "content": "{}",
                             "old": "x", "new": "y"})
        check("validate 拒绝 content+old/new 歧义请求",
              amb["ok"] is False and any("不能同时传" in e for e in amb["errors"]),
              str(amb))
        bad = [{"path": p1, "content": '{"ok": 1}\n'},
               {"path": p2, "old": "NOT-THERE", "new": "y"}]
        code, msg = err_of(lambda: a.op_patch({"changes": bad}))
        check("patch 预检失败整体不动", code == "precheck_failed"
              and read(p1) == '{"x": 2}\n', "%s %s" % (code, msg))

        # 执行中途失败 → 已写文件被回滚
        before1, before2 = read(p1), read(p2)
        orig_write = a._atomic_write
        state = {"n": 0}

        def flaky(full, data):
            state["n"] += 1
            if state["n"] == 2:
                raise OSError("simulated write failure")
            return orig_write(full, data)

        a._atomic_write = flaky
        try:
            code2, msg2 = err_of(lambda: a.op_patch({
                "changes": [{"path": p1, "content": '{"changed": 1}\n'},
                            {"path": p2, "content": "CHANGED-B\n"}],
                "backup": False,
            }))
        finally:
            a._atomic_write = orig_write
        check("patch 中途失败抛错并报告回滚", code2 == "patch_failed"
              and "回滚" in msg2, "%s %s" % (code2, msg2))
        check("patch 失败后磁盘回到调用前", read(p1) == before1
              and read(p2) == before2, "%r / %r" % (read(p1), read(p2)))

        a.set_request_mode(True)
        code3, _msg3 = err_of(lambda: a.op_patch({"changes": changes}))
        check("只读 Token 不能调 patch", code3 == "read_only")
        a.set_request_mode(False)

        pr = a.op_probe({})
        check("probe 版本与分层齐全", pr["version"] == wa.VERSION
              and "by_layer" in pr["config"]["layers"])
        check("probe 根目录可达性", len(pr["roots"]) == 1
              and pr["roots"][0]["exists"] is True)
        check("probe 审计/备份/安全/工具段齐全",
              all(k in pr for k in ("audit", "backup", "security", "tools", "issues")))
        check("probe 不输出 Token 明文", "token" not in str(pr).lower()
              or "token_scope" in str(pr["security"]))

        # 审计降级会在 probe 里变成问题
        a.audit._pending.append("stale")
        a.audit.failures = 1
        pr2 = a.op_probe({})
        check("probe 报出审计降级", pr2["ok"] is False
              and any("降级" in x for x in pr2["issues"]))

        # MCP 层：三个工具都注册、能路由、文本可读
        tools = {t["name"]: t for t in m.TOOLS}
        for name in ("win_validate", "win_patch", "win_probe"):
            check("schema 注册 %s" % name, name in tools
                  and m.ROUTE_MAP.get(name) in wa.Agent.ROUTES)
        check("validate 标只读 / patch 标写",
              "win_validate" in m.READ_ONLY_TOOLS and "win_patch" in m.WRITE_TOOLS
              and "win_probe" in m.READ_ONLY_TOOLS)
        res = m.call_tool(a.dispatch, {"name": "win_probe", "arguments": {}})
        check("call_tool(win_probe) 成功", res["isError"] is False
              and "自检" in res["content"][0]["text"])
        check("win_probe 响应真的能发出去（json.dumps）", serializable(res))
        check("probe 结构化字段完整",
              res["structuredContent"]["config"]["layers"]["order"]
              == ["default", "file", "env"])
        res2 = m.call_tool(a.dispatch, {"name": "win_validate",
                                        "arguments": {"path": p1,
                                                      "content": '{"v": 3}\n'}})
        check("call_tool(win_validate) 成功", res2["isError"] is False
              and "写前校验" in res2["content"][0]["text"])
        check("win_validate 响应真的能发出去（json.dumps）", serializable(res2))
        res3 = m.call_tool(a.dispatch, {"name": "win_patch", "arguments": {
            "changes": [{"path": p2, "content": "final\n"}]}})
        check("call_tool(win_patch) 成功", res3["isError"] is False
              and read(p2) == "final\n")
        # v1.7.22：这条就是拦 P0 的。旧版只查 isError，bytes 进了
        # structuredContent 也照样全绿，服务端却已经 500 / 断连。
        check("win_patch 响应真的能发出去（json.dumps）", serializable(res3))
        check("win_patch 响应里没有原文字节",
              all("original" not in it
                  for it in (res3.get("structuredContent") or {}).get("changes", [])),
              str(res3.get("structuredContent"))[:160])
    finally:
        shutil.rmtree(base, ignore_errors=True)


def main():
    test_core()
    test_config_layers()
    test_audit_degrade()
    test_backup_dedup()
    test_new_tools()
    print("\n========== 汇总 ==========")
    print("通过 %d / 失败 %d / 环境跳过 %d" % (len(PASS), len(FAIL), len(SKIP)))
    if FAIL:
        for name in FAIL:
            print("  [FAIL] " + name)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
