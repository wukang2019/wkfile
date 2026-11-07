# -*- coding: utf-8 -*-
"""v1.7.27 契约回归：热加载非法值丢弃 / patch 0 值不限制 / port 拒启 / 备份单遍清理。

第六轮外部审计（v1.7.26 发布后仍残留的 9 条）里，落到「代码真 bug」或
「实现口径错」的四条，本版逐一收口：

  #1  exec_strict_chain 热加载写 null 可静默关掉严格链；
      exec_allowed_commands 热改数字 -> /api/exec 500。
      -> 热加载加类型守门，非法值丢弃并保留上次生效值 + stderr 告警。
  #4  patch_max_total_bytes=0 被当成硬上限，每次补丁都被「超预算 0 字节」全拒。
      -> <=0 视为「不限制」（与 backup_max_bytes=0 不清理同一约定）。
  #5  port:null / host:null 静默回退默认值，连错端口还以为服务没起。
      -> port 列为连通性关键键，脏值直接拒绝启动；host 仍回退 0.0.0.0 + 告警。
  #8  备份清理每轮都单独扫一遍 .objects 去 prune，与 _backup_entries 的枚举重复。
      -> 复用 _backup_entries 已记下的孤儿路径直接删，单遍完成。

本文件先于「修复完成」写成，修复前应当成片报红；断言刻意避开恒真条件。
"""
import io
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "server"))
import wkfile_agent as wa  # noqa: E402

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print("  %s %s%s" % ("[OK]  " if cond else "[FAIL]", name,
                         "  :: %s" % detail if detail and not cond else ""))


def expect_raise(name, fn, exc_type, *needles):
    try:
        fn()
    except exc_type as exc:
        text = str(exc)
        ok = all(n in text for n in needles)
        check(name, ok, "缺关键词 %r / 实际 %r" % (needles, text[:220]))
        return text
    except Exception as exc:  # noqa: BLE001
        check(name, False, "抛了别的异常 %s: %s" % (type(exc).__name__, exc))
        return ""
    check(name, False, "根本没抛异常（fail-open 还在）")
    return ""


def make_cfg(base, **over):
    share = os.path.join(base, "share")
    os.makedirs(share, exist_ok=True)
    data = {"roots": [share], "token": "abcdefghijklmnopqrstuvwxyz0123456789",
            "auto_backup": True, "public_health": True}
    data.update(over)
    return wa.Config(data, os.path.join(base, "c.json"), env={}), share


def read_src(rel):
    with open(os.path.join(ROOT, rel), "r", encoding="utf-8") as fh:
        return fh.read()


# ---------------------------------------------------------------------------
# 组 A：exec 热加载类型守门（审计 #1）
# ---------------------------------------------------------------------------
def groupA():
    print("\n-- 组 A：exec 热加载非法值丢弃不套用（审计 #1） --")
    validate = getattr(wa.Agent, "_validate_hot_key", None)
    check("Agent._validate_hot_key 静态方法存在", callable(validate))

    # A1：纯函数守门逐键验证
    if callable(validate):
        # 安全开关：true/false 及可识别写法放行，null/字符串 yes/数字 一律拒
        for val, ok in ((True, True), (False, True), ("true", True),
                        ("false", True), ("1", True), (0, True),
                        (None, False), ("yes", False), (1.0, False), (2, False)):
            good, _why = validate("exec_strict_chain", val)
            check("exec_strict_chain %r -> %s" % (val, "放行" if ok else "拒"),
                  good is ok, "%r got %r" % (val, good))
        # 白名单：字符串数组放行，非数组/含非字符串元素/数字 一律拒
        for val, ok in ((["php", "composer"], True), ([], True),
                        ("php", False), (["php", 1], False), (123, False),
                        (None, False)):
            good, _why = validate("exec_allowed_commands", val)
            check("exec_allowed_commands %r -> %s" % (val, "放行" if ok else "拒"),
                  good is ok, "%r got %r" % (val, good))
        # 三个数值键：正整数放行，<=0 / 非 int / bool 一律拒
        for key in ("exec_timeout_sec", "exec_timeout_max_sec",
                    "exec_max_output_bytes"):
            for val, ok in ((30, True), (1, True), (0, False), (-1, False),
                            ("30", False), (True, False), (1.5, False)):
                good, _why = validate(key, val)
                check("%s %r -> %s" % (key, val, "放行" if ok else "拒"),
                      good is ok, "%r got %r" % (val, good))

    # A2：端到端 —— 热加载遇到非法值丢弃并保留上次生效值，且打告警
    base = tempfile.mkdtemp(prefix="wk1727a-")
    try:
        cfg_path = os.path.join(base, "wkfile.config.json")
        before = {"host": "127.0.0.1", "port": 8788,
                  "token": "abcdefghijklmnopqrstuvwxyz0123456789",
                  "roots": [os.path.join(base, "share")],
                  "exec_strict_chain": True,
                  "exec_allowed_commands": ["php", "composer"],
                  "exec_timeout_sec": 30}
        with open(cfg_path, "w", encoding="utf-8") as fh:
            json.dump(before, fh)
        cfg = wa.Config(before, cfg_path, env={})
        # Agent.__init__ 内部会 try/except 包裹 cleanup_backups/cleanup_trash，安全
        agent = wa.Agent(cfg)
        check("热加载前 exec_strict_chain 为 True",
              cfg.exec_strict_chain is True, repr(cfg.exec_strict_chain))
        check("热加载前 exec_allowed_commands 为白名单",
              cfg.exec_allowed_commands == ["php", "composer"],
              repr(cfg.exec_allowed_commands))

        # 把配置改成非法：strict_chain 写 null、allowed_commands 写数字
        bad = dict(before)
        bad["exec_strict_chain"] = None
        bad["exec_allowed_commands"] = 123
        bad["exec_timeout_sec"] = -5
        with open(cfg_path, "w", encoding="utf-8") as fh:
            json.dump(bad, fh)

        agent._exec_cfg_mtime = None  # 强制触发一次重读
        buf = io.StringIO()
        old_stderr = sys.stderr
        sys.stderr = buf
        try:
            agent._hotload_exec_cfg()
        finally:
            sys.stderr = old_stderr
        log = buf.getvalue()

        check("热加载后 exec_strict_chain 仍保留 True（未因 null 变 False）",
              cfg.exec_strict_chain is True, repr(cfg.exec_strict_chain))
        check("热加载后 exec_allowed_commands 仍保留原白名单（未被数字覆盖）",
              cfg.exec_allowed_commands == ["php", "composer"],
              repr(cfg.exec_allowed_commands))
        # 合法键（exec_timeout_sec 即便本次是 -5 也应被丢弃而非套用）
        check("热加载后 exec_timeout_sec 保留上次 30（非法 -5 被丢弃）",
              cfg.exec_timeout_sec == 30, repr(cfg.exec_timeout_sec))
        check("热加载把非法键打到 stderr 告警",
              "配置热加载跳过非法键" in log, log[:300])
        check("告警点名 exec_strict_chain",
              "exec_strict_chain" in log, log[:300])
        check("告警点名 exec_allowed_commands",
              "exec_allowed_commands" in log, log[:300])
    finally:
        shutil.rmtree(base, ignore_errors=True)


# ---------------------------------------------------------------------------
# 组 B：patch_max_total_bytes=0 视为不限制（审计 #4）
# ---------------------------------------------------------------------------
def groupB():
    print("\n-- 组 B：patch 预算 0/负数视为不限制（审计 #4） --")
    fn = getattr(wa, "patch_plan", None)
    check("patch_plan 可调用", callable(fn))
    if not callable(fn):
        return
    big = [{"path": "a.txt", "content": "x" * 2_000_000},
           {"path": "b.txt", "content": "y" * 1_000_000}]

    planned, errors = fn(list(big), max_total_bytes=0)
    check("max_total_bytes=0 不报错（视为不限制）", not errors, str(errors)[:200])
    check("max_total_bytes=0 仍返回规范化条目", len(planned) == 2,
          "planned=%d" % len(planned))

    planned, errors = fn(list(big), max_total_bytes=-1)
    check("max_total_bytes=-1 同样视为不限制", not errors, str(errors)[:200])

    planned, errors = fn(list(big), max_total_bytes=10)
    check("max_total_bytes=10 仍按硬上限拦截（未退化）",
          any("超过预算" in e for e in errors), str(errors)[:200])

    # 默认预算（PATCH_MAX_TOTAL_BYTES = 8MB）仍生效：超过 8MB 的单批应被拦
    huge = [{"path": "a.txt", "content": "x" * 5_000_000},
            {"path": "b.txt", "content": "y" * 5_000_000}]
    planned, errors = fn(huge)
    check("默认 8MB 预算仍生效（>8MB 单批被拦）",
          any("超过预算" in e for e in errors), str(errors)[:200])


# ---------------------------------------------------------------------------
# 组 C：port / host 不再静默回退（审计 #5）
# ---------------------------------------------------------------------------
def groupC(base):
    print("\n-- 组 C：port 脏值拒绝启动 / host 回退告警（审计 #5） --")
    SAFETY_ERR = getattr(wa, "ConfigSafetyError", ValueError)

    expect_raise("port 写字符串拒绝启动",
                 lambda: make_cfg(base, port="10087"), SAFETY_ERR,
                 "拒绝启动", "port")
    expect_raise("port 写 null 拒绝启动",
                 lambda: make_cfg(base, port=None), SAFETY_ERR,
                 "拒绝启动", "port")
    expect_raise("port 写 0（越界）拒绝启动",
                 lambda: make_cfg(base, port=0), SAFETY_ERR,
                 "拒绝启动", "port")
    expect_raise("port 写 70000（越界）拒绝启动",
                 lambda: make_cfg(base, port=70000), SAFETY_ERR,
                 "拒绝启动", "port")

    # host:null / 空串 -> 不拒启，回退 0.0.0.0 + 告警
    cfg, _ = make_cfg(base, host=None)
    check("host=null 不拒启，回退默认 0.0.0.0",
          cfg.host == "0.0.0.0", repr(cfg.host))
    warns = getattr(cfg, "config_warnings", []) or []
    check("host=null 产生 fallback 告警且点名 host",
          any(w.get("key") == "host" and w.get("action") == "fallback"
              for w in warns), str(warns)[:200])

    cfg2, _ = make_cfg(base, host="   ")
    check("host=空白字符串回退 0.0.0.0", cfg2.host == "0.0.0.0", repr(cfg2.host))

    # 真进程：port 脏值必须直接拒启（rc=5）、不绑端口、不回显令牌
    for label, port_val, port_int in (("port 为字符串", "10087", 18950),
                                      ("port 为 null", None, 18951)):
        d = os.path.join(base, "cli-" + label.replace(" ", "-"))
        os.makedirs(d, exist_ok=True)
        share = os.path.join(d, "share")
        os.makedirs(share, exist_ok=True)
        cfg_path = os.path.join(d, "wkfile.config.json")
        body = {"host": "127.0.0.1", "port": port_val,
                "token": "fedcba9876543210fedcba9876543210",
                "roots": [share], "public_health": True}
        with open(cfg_path, "w", encoding="utf-8") as fh:
            json.dump(body, fh)
        p = _spawn(cfg_path)
        out = _stop(p, wait_first=6)
        check("真 CLI：%s 拒绝启动（rc=5）" % label,
              p.returncode == 5, "rc=%s out=%r" % (p.returncode, out[:220]))
        check("真 CLI：%s 拒启信息点名 port 且不含令牌" % label,
              "port" in out and "fedcba9876543210" not in out, out[:300])
        check("真 CLI：%s 没有出现 listening（确实没绑端口）" % label,
              "listening" not in out, out[:300])


# ---------------------------------------------------------------------------
# 组 D：备份清理单遍遍历、复用孤儿路径（审计 #8）
# ---------------------------------------------------------------------------
def groupD(base):
    print("\n-- 组 D：备份清理单遍遍历孤儿回收（审计 #8） --")
    # D1：静态断言 cleanup_backups 不再单独走第二遍 _prune_backup_objects，
    #     而是复用 _backup_entries 记下的 _backup_orphan_paths。
    src = read_src("server/wkfile_agent.py")
    start = src.find("    def cleanup_backups(self):")
    if start == -1:
        check("能定位 cleanup_backups 方法", False, "找不到方法")
    else:
        end = src.find("\n    def ", start + 10)
        body = src[start:end if end != -1 else start + 2000]
        check("cleanup_backups 不再调用 _prune_backup_objects（单遍）",
              "_prune_backup_objects(" not in body, body[:200])
        check("cleanup_backups 复用 _backup_orphan_paths",
              "_backup_orphan_paths" in body, body[:200])

    # D2：端到端 —— 孤儿实体在 cleanup 时被复用路径直接删掉（不依赖二次 prune）
    cfg, _ = make_cfg(base, auto_backup=False)
    agent = wa.Agent(cfg)  # 先构造（此时备份目录为空，cleanup 无害）
    broot = wa.backup_root(cfg.base_dir)
    # 制造一个时间戳版本 + 一个 .objects 孤儿（没被任何版本引用）
    stamp_dir = os.path.join(broot, "20260101-120000")
    os.makedirs(os.path.join(stamp_dir, "share"), exist_ok=True)
    with open(os.path.join(stamp_dir, "share", "real.txt"), "w") as fh:
        fh.write("real-version-content")  # 1.0 左右字节
    obj_dir = os.path.join(broot, ".objects")
    os.makedirs(obj_dir, exist_ok=True)
    orphan = os.path.join(obj_dir, "orphan_entity.bin")
    with open(orphan, "w") as fh:
        fh.write("orphan-content-that-is-never-referenced")

    base2, entries, physical = agent._backup_entries()
    orphans = getattr(agent, "_backup_orphan_paths", []) or []
    check("_backup_entries 记录到孤儿路径", orphan in orphans,
          "orphans=%r" % orphans)
    check("_backup_entries 物理占用不含孤儿",
          physical == os.path.getsize(os.path.join(stamp_dir, "share", "real.txt")),
          "physical=%d" % physical)

    agent.cleanup_backups()
    check("清理后孤儿实体文件已被删除（复用路径单遍完成）",
          not os.path.exists(orphan), orphan)
    # 时间戳版本里被引用的真实文件应保留
    check("清理后仍保留有效版本文件",
          os.path.exists(os.path.join(stamp_dir, "share", "real.txt")))

    # D3：回归 —— backup_inode_sizes 兼容 3 元组 (inode_key, size, path)
    bis = getattr(wa, "backup_inode_sizes", None)
    if callable(bis):
        groups = {
            ".objects": [(("dev", 1), 100, "/p/a"),
                         (("dev", 2), 200, "/p/b")],
            "20260101": [(("dev", 1), 100, "/p/a"),
                         (("dev", 3), 300, "/p/c")],
        }
        physical, reclaim = bis(groups)
        check("backup_inode_sizes 3 元组：物理占用按唯一 inode 去重=600",
              physical == 600, "physical=%r" % physical)
        check("backup_inode_sizes 3 元组：reclaim 只算最后引用方=400",
              reclaim.get("20260101") == 400, "reclaim=%r" % reclaim)


# ---------------------------------------------------------------------------
# 真进程辅助
# ---------------------------------------------------------------------------
def _spawn(cfg_path, *extra):
    return subprocess.Popen(
        [sys.executable, os.path.join(ROOT, "server", "wkfile_agent.py"),
         "--config", cfg_path] + list(extra),
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT)


def _stop(proc, wait_first=0):
    if proc is None:
        return ""
    if proc.poll() is None and wait_first:
        try:
            proc.wait(timeout=wait_first)
        except subprocess.TimeoutExpired:
            pass
    if proc.poll() is None:
        proc.terminate()
    try:
        out, _ = proc.communicate(timeout=10)
        return out.decode("utf-8", "replace")
    except subprocess.TimeoutExpired:
        proc.kill()
        out, _ = proc.communicate(timeout=10)
        return out.decode("utf-8", "replace")


def main():
    base = tempfile.mkdtemp(prefix="wk1727-")
    try:
        groupA()
        groupB()
        groupC(base)
        groupD(base)
    finally:
        shutil.rmtree(base, ignore_errors=True)
    print("\n" + "=" * 60)
    print("V1727_TEST  通过 %d / 失败 %d" % (len(PASS), len(FAIL)))
    if FAIL:
        for f in FAIL:
            print("  FAIL: " + f)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
