# -*- coding: utf-8 -*-
"""v1.7.25 契约回归：脏数值配置 + 两处文档口径。

外部体检第四轮（v1.7.24 已确认 P0 为零、两条 P1 真闭环）剩下的问题：
  ① 配置文件层完全没做类型校验（只有环境变量层有 coerce_env_value）。
     "backup_max_bytes": "2GB" 会在启动清理的 int() 处抛——被
     Agent.__init__ 的 try/except 静默吞掉，等于保留策略整个失效；
     op_patch 则回 500。"patch_max_rollback_bytes": "8MB" 让 op_probe
     与 op_patch 一起 500，唯一能自检的工具也哑了。而这个人恰恰是
     手改 JSON 的运维本人，不是理论风险。
     修法：加载时按 DEFAULT_CONFIG 类型校验，脏值回退默认 + 在
     /api/health 与 win_probe 里主动报出来（不输出任何配置值）。
  ② README 把 features 用例数写死 211 并盖「v1.7.24 实测」章。实测
     有 openssl = 211/211、无 openssl = 195/195（另 2 项真库链路跳过），
     数字随可选依赖浮动，写死就是错的。
  ③ 使用说明.txt 的「说明书里把已知不拦的写法逐条列了出来」是自指
     错误——那份清单只在随包 README.md 里。

本文件先于产品代码写成，修复前应当成片报红。
"""
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "server"))
import wkfile_agent as wa  # noqa: E402

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print("  %s %s%s" % ("[OK]  " if cond else "[FAIL]", name,
                         "  :: %s" % detail if detail and not cond else ""))


def json_safe(name, obj):
    """新字段必须能过真实序列化链路，不能只看 isError / 字段存在。"""
    try:
        blob = json.dumps(obj, ensure_ascii=False)
    except (TypeError, ValueError) as exc:
        check(name, False, "%s: %s" % (type(exc).__name__, exc))
        return ""
    check(name, True)
    return blob


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
    return ""


def make_cfg(base, **over):
    share = os.path.join(base, "share")
    os.makedirs(share, exist_ok=True)
    data = {"roots": [share], "token": "abcdefghijklmnopqrstuvwxyz0123456789",
            "auto_backup": True, "public_health": True}
    data.update(over)
    cfg = wa.Config(data, os.path.join(base, "c.json"), env={})
    cfg.base_dir = base
    return cfg, share


def read_doc(rel):
    with open(os.path.join(ROOT, rel), "r", encoding="utf-8") as fh:
        return fh.read().replace("\r\n", "\n")


def main():
    base = tempfile.mkdtemp(prefix="wk1725-")
    try:
        # ---------------------------------------------------------------
        # 组 1：纯函数存在且行为正确
        # ---------------------------------------------------------------
        print("\n-- 组 1：配置类型校验纯函数 --")
        fn = getattr(wa, "validate_config_types", None)
        check("纯函数 validate_config_types 已外提到 core 并挂到 wa", callable(fn))
        if callable(fn):
            defaults = {"a": 10, "b": True, "c": "s", "d": [], "e": {}, "f": 1.5}
            dirty = {"a": "SENTINEL_DIRTY_9f3a", "b": 1, "c": 7, "d": "x", "e": "y",
                     "f": float("nan"), "a2": "unknown"}
            cleaned, warns = fn(defaults, dirty, {k: "file" for k in dirty})
            check("int 脏值 '2GB' 回退默认", cleaned["a"] == 10, str(cleaned["a"]))
            check("bool 键不收 int 冒充", cleaned["b"] is True, repr(cleaned["b"]))
            check("str 键不收数字", cleaned["c"] == "s", repr(cleaned["c"]))
            check("list 键不收字符串", cleaned["d"] == [], repr(cleaned["d"]))
            check("dict 键不收字符串", cleaned["e"] == {}, repr(cleaned["e"]))
            check("非有限浮点被拒", cleaned["f"] == 1.5, repr(cleaned["f"]))
            check("未知键原样保留（不静默抹掉用户自定义键）",
                  cleaned.get("a2") == "unknown")
            check("每个脏键都有一条结构化告警", len(warns) == 6, str(len(warns)))
            check("告警含 key/expected/got/message 且不回显用户填的值",
                  all(set(("key", "expected", "got", "message")) <= set(w)
                      for w in warns)
                  and not any("SENTINEL_DIRTY_9f3a" in w["message"] for w in warns),
                  str(warns[:1]))
            json_safe("告警列表可 json.dumps", warns)
            clean, w2 = fn(defaults, {"a": 0, "b": False, "c": "", "d": [1],
                                      "e": {"k": 1}, "f": 2}, {})
            check("合法值（0 / False / 空串 / 非空容器）一个都不误伤",
                  not w2 and clean["a"] == 0 and clean["b"] is False
                  and clean["d"] == [1] and clean["e"] == {"k": 1},
                  str(w2))

        # ---------------------------------------------------------------
        # 组 2：Config 加载层集成
        # ---------------------------------------------------------------
        print("\n-- 组 2：Config 脏值回退与告警 --")
        cfg, share = make_cfg(base, backup_max_bytes="2GB",
                              patch_max_rollback_bytes="8MB",
                              backup_retain_days="30",
                              auto_backup=1)
        roots_bad, _ = make_cfg(base, roots="D:\\share")
        check("backup_max_bytes 回退到默认整数",
              cfg.backup_max_bytes == wa.DEFAULT_CONFIG["backup_max_bytes"],
              repr(cfg.backup_max_bytes))
        check("patch_max_rollback_bytes 回退到默认整数",
              cfg.patch_max_rollback_bytes == wa.DEFAULT_CONFIG["patch_max_rollback_bytes"],
              repr(cfg.patch_max_rollback_bytes))
        check("backup_retain_days 的纯数字字符串也被判脏并回退",
              cfg.backup_retain_days == wa.DEFAULT_CONFIG["backup_retain_days"],
              repr(cfg.backup_retain_days))
        check("auto_backup 的 1 冒充 bool 被回退", cfg.auto_backup is True,
              repr(cfg.auto_backup))
        check("roots 的字符串被回退成默认空列表（不是逐字符拆成一堆根）",
              roots_bad.roots == [], repr(roots_bad.roots))
        warns = list(getattr(cfg, "config_warnings", []))
        keys = {w.get("key") for w in warns} if warns and isinstance(warns[0], dict) else set()
        check("Config 上有结构化 config_warnings", bool(warns) and bool(keys),
              str(warns)[:180])
        check("两个审计点名的键都在告警里",
              {"backup_max_bytes", "patch_max_rollback_bytes"} <= keys, str(sorted(keys)))
        layers = cfg.layers_report()
        check("layers_report 带出 config_warnings",
              layers.get("config_warnings") == warns, str(layers.get("config_warnings"))[:120])
        json_safe("layers_report 可 json.dumps", layers)
        check("告警里不回显任何配置值（Token / 路径 / 用户填的字符串）",
              all(x not in json.dumps(warns, ensure_ascii=False)
                  for x in ("2GB", "8MB", "abcdefghijklmnopqrstuvwxyz0123456789",
                            share.replace("\\", "\\\\"))),
              json.dumps(warns, ensure_ascii=False)[:200])
        good, _ = make_cfg(base, backup_max_bytes=0, patch_max_rollback_bytes=1,
                           backup_retain_days=-1, auto_backup=False)
        check("干净数值配置（0 / 1 / 负数 / False）不产生告警",
              not getattr(good, "config_warnings", []),
              str(getattr(good, "config_warnings", []))[:120])

        # ---------------------------------------------------------------
        # 组 3：运行时不再崩，且自检能自己报出问题
        # ---------------------------------------------------------------
        print("\n-- 组 3：脏配置下服务、自检、写入都不哑 --")
        a = None
        try:
            a = wa.Agent(cfg)
            check("Agent() 构造不抛异常", True)
        except Exception as exc:  # noqa: BLE001
            check("Agent() 构造不抛异常", False, "%s: %s" % (type(exc).__name__, exc))
        if a is not None:
            a.set_request_mode(False)
            try:
                probe = a.op_probe({})
                check("op_probe 不抛（唯一自检工具没哑）", True)
                blob = json_safe("op_probe 返回值可 json.dumps", probe)
                check("probe 的 issues 主动报出配置告警",
                      any("配置" in s and ("回退" in s or "类型" in s)
                          for s in (probe.get("issues") or [])),
                      str(probe.get("issues"))[:220])
                check("probe 里没有 bytes", not find_bytes(probe),
                      find_bytes(probe))
                check("probe 的 config.layers 带出 config_warnings",
                      bool((probe["config"]["layers"]).get("config_warnings")),
                      blob[:160])
            except Exception as exc:  # noqa: BLE001
                check("op_probe 不抛（唯一自检工具没哑）", False,
                      "%s: %s" % (type(exc).__name__, exc))
            target = os.path.join(share, "x.txt")
            try:
                r = a.op_patch({"changes": [{"path": target, "content": "hi\n"}]})
                check("op_patch 不抛且真的写入成功",
                      r.get("applied") is True and os.path.isfile(target),
                      str(r)[:160])
                json_safe("op_patch 返回值可 json.dumps", r)
            except Exception as exc:  # noqa: BLE001
                check("op_patch 不抛且真的写入成功", False,
                      "%s: %s" % (type(exc).__name__, exc))
            clean_probe = wa.Agent(good).op_probe({})
            check("干净配置的 probe 没有配置类 issues",
                  not any("回退" in s and "配置" in s
                          for s in (clean_probe.get("issues") or [])),
                  str(clean_probe.get("issues"))[:160])

        # ---------------------------------------------------------------
        # 组 4：/api/health 免鉴权响应报出来，但不泄露值
        # ---------------------------------------------------------------
        print("\n-- 组 4：/api/health 告警 --")
        def health_of(agent):
            h = object.__new__(wa.Handler)
            h.agent = agent
            return h._health_minimal()

        if a is not None:
            try:
                h = health_of(a)
                blob = json_safe("/api/health 响应可 json.dumps", h)
                check("health 里有 config_warnings",
                      bool(h.get("config_warnings")), str(h)[:180])
                cw = h.get("config_warnings")
                check("health 告警带键名，运维能知道改哪个",
                      isinstance(cw, dict)
                      and {"backup_max_bytes", "patch_max_rollback_bytes"}
                          <= set(cw.get("keys") or []), str(cw)[:180])
                check("health 不泄露任何配置值（免鉴权端点）",
                      "2GB" not in blob and "8MB" not in blob, blob[:200])
                check("health 仍带 service/version", "version" in h and "service" in h)
            except Exception as exc:  # noqa: BLE001
                check("/api/health 响应可 json.dumps", False,
                      "%s: %s" % (type(exc).__name__, exc))
            gh = health_of(wa.Agent(good))
            check("干净配置的 health 不带告警字段（不制造噪音）",
                  "config_warnings" not in gh, str(gh)[:160])

        # ---------------------------------------------------------------
        # 组 5：环境变量层既有行为不破
        # ---------------------------------------------------------------
        print("\n-- 组 5：env 层不回归 --")
        envcfg = wa.Config({}, os.path.join(base, "e.json"),
                           env={"WKFILE_BACKUP_MAX_BYTES": "oops",
                                "WKFILE_PORT": "9999"})
        check("env 脏值仍走既有 env_warnings（不重复报两遍）",
              len(envcfg.layers_report()["env_warnings"]) == 1
              and not getattr(envcfg, "config_warnings", []),
              str(envcfg.layers_report()["env_warnings"]))
        check("env 合法覆盖仍生效", envcfg.port == 9999, str(envcfg.port))
        check("env 脏值回退到默认值",
              envcfg.backup_max_bytes == wa.DEFAULT_CONFIG["backup_max_bytes"])
        check("拼错的环境键仍被忽略",
              wa.Config({}, os.path.join(base, "e2.json"),
                        env={"WKFILE_TOEKN": "x"}).layers_report()["env_warnings"])

        # ---------------------------------------------------------------
        # 组 6：真起临时服务，验证换版重启后 /api/health 与 /api/probe
        # ---------------------------------------------------------------
        print("\n-- 组 6：真起服务（脏配置换版重启场景） --")
        wire_ok = wire_check(base)
        check("脏配置下服务能起且 /api/health 200、含告警", wire_ok["health"])
        check("脏配置下 /api/probe 仍 200（自检没哑）", wire_ok["probe"])
        check("服务进程不是崩掉后被误判为成功", wire_ok["alive"])

        # ---------------------------------------------------------------
        # 组 7：文档口径
        # ---------------------------------------------------------------
        print("\n-- 组 7：文档数字与自指 --")
        rd = read_doc("README.md")
        state = rd[rd.find("当前状态（"):rd.find("当前状态（") + 900]
        check("README 状态段落同时给出 195 与 211 两种实测值",
              "195" in state and "211" in state, state[:220])
        check("README 说明用例数随 openssl 浮动",
              "openssl" in state.lower(), state[:220])
        check("README 不再把 211 盖章成绝对实测",
              "features 211/211（另 2 项" not in state, state[:220])
        feat_lines = [l for l in rd.split("\n") if "features_e2e.py" in l]
        check("README 每处 features_e2e.py 都说明用例数浮动（不再写死 211）",
              bool(feat_lines)
              and all(("195" in l and "211" in l) or "openssl" in l or "浮动" in l
                      for l in feat_lines),
              str(feat_lines)[:320])
        check("README 记录 v1.7.25 配置类型校验",
              "配置类型校验" in rd or "类型校验" in rd)
        q = read_doc("README-快速上手.txt")
        check("快速上手不再自指「说明书里的用例数」", "说明书里的用例数" not in q)
        check("快速上手不再自指「说明书里把已知不拦的写法」",
              "说明书里把已知不拦的写法" not in q)
        seg = q[q.find("1.7.24 两件事"):q.find("程序运行时【只创建")]
        check("1.7.24 段落明确指向随包 README.md（而非自指「说明书里」）",
              "README.md" in seg and ("已知限制" in seg or "已知不拦" in seg),
              seg[:320])
        tpl = wa._annotate_config(
            json.dumps(dict(wa.DEFAULT_CONFIG), ensure_ascii=False, indent=2))
        check("自动生成的配置模板提示数值键填纯整数、不支持带单位",
              "2GB" in tpl and ("纯整数" in tpl or "带单位" in tpl),
              tpl[:200])
    finally:
        shutil.rmtree(base, ignore_errors=True)

    print("\n" + "=" * 60)
    print("V1725_TEST  通过 %d / 失败 %d" % (len(PASS), len(FAIL)))
    if FAIL:
        for f in FAIL:
            print("  FAIL: " + f)
    return 1 if FAIL else 0


def wire_check(base):
    """真起一个带脏配置的实例，GET /api/health + POST /api/probe。"""
    out = {"health": False, "probe": False, "alive": False}
    port, token = 18932, "wire-token-0123456789abcdef"
    share = os.path.join(base, "wire-share")
    data = os.path.join(base, "wire-data")
    os.makedirs(share, exist_ok=True)
    os.makedirs(data, exist_ok=True)
    cfg = {"host": "127.0.0.1", "port": port, "token": token,
           "roots": [share], "auto_backup": True, "public_health": True,
           "backup_max_bytes": "2GB", "patch_max_rollback_bytes": "8MB"}
    cfg_path = os.path.join(data, "wkfile.config.json")
    with open(cfg_path, "w", encoding="utf-8") as fh:
        json.dump(cfg, fh)
    proc = None
    try:
        proc = subprocess.Popen(
            [sys.executable, os.path.join(ROOT, "server", "wkfile_agent.py"),
             "--config", cfg_path],
            cwd=data, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        up = False
        for _ in range(60):
            time.sleep(0.2)
            try:
                with urllib.request.urlopen(
                        "http://127.0.0.1:%d/api/health" % port, timeout=1) as r:
                    body = r.read().decode("utf-8", "replace")
                    if r.status == 200:
                        up = True
                        out["health"] = ("config_warnings" in body
                                         and "backup_max_bytes" in body)
                        break
            except Exception:  # noqa: BLE001
                if proc.poll() is not None:
                    break
        out["alive"] = up and proc.poll() is None
        if up:
            req = urllib.request.Request(
                "http://127.0.0.1:%d/api/probe" % port,
                data=b"{}", headers={"Content-Type": "application/json",
                                     "Authorization": "Bearer " + token},
                method="POST")
            try:
                with urllib.request.urlopen(req, timeout=8) as r:
                    body = r.read().decode("utf-8", "replace")
                    out["probe"] = r.status == 200 and "config_warnings" in body
            except urllib.error.HTTPError as exc:
                out["probe"] = False
                print("    probe HTTP %s: %s" % (exc.code, exc.read()[:120]))
            except Exception as exc:  # noqa: BLE001
                print("    probe ERR %r" % exc)
    finally:
        if proc is not None:
            proc.terminate()
            try:
                proc.wait(timeout=8)
            except subprocess.TimeoutExpired:
                proc.kill()
            try:
                proc.stdout.close()
            except Exception:  # noqa: BLE001
                pass
    return out


if __name__ == "__main__":
    sys.exit(main())
