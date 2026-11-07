# -*- coding: utf-8 -*-
r"""v1.7.29 两条「被低估」审计项的收口：

审计 N21（tls_cert:null 静默关 TLS）：
  之前 tls_cert / tls_key / tls_chain 写成 null / 非字符串时，merge 当「没填」
  丢掉、回退默认空串，tls_enabled() 静默返回 False——你以为 HTTPS 在跑、实际
  明文 HTTP，而 probe.issues / config_warnings / /health 全是空的。这与 host:null
  同源（都是「配置写了却因类型问题被悄悄丢掉」），但 plaintext 比绑 0.0.0.0 更危险。
  -> v1.7.29 在 Config.__init__ 里补告警：显式写成 null / 非字符串、或 cert/key
     只配一个，都写进 config_warnings（启动横幅 / win_probe / /health 都带得出），
     不再静默 fail-open。想关 TLS 请显式写空字符串 ""。

审计 N26（README 说 `type .\deploy.ps1` 加白名单就能过，实际不是）：
  这条命令被拦的真实原因是「围栏⑦ cwd 劫持防护」（`.\deploy.ps1` 是 cwd 优先于
  PATH 的脚本引用，cwd 里有同名脚本就拒），不是白名单。即便把 type 加进
  exec_allowed_commands，照样被 exec_hijack_suspected 拦下——按文档加白名单是白费
  功夫。本测试用「type 已在白名单」为前提，证明 cwd 劫持仍会拦，且裸文件名
  `type deploy.ps1` 与绝对路径 `type C:\abs\deploy.ps1` 不拦（给出正确绕法）。
"""
import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "server"))
import wkfile_agent as wa  # noqa: E402

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print("  %s %s%s" % ("[OK]  " if cond else "[FAIL]", name,
                         "  :: %s" % detail if detail and not cond else ""))


def make_cfg(over):
    base = tempfile.mkdtemp(prefix="wk1729-")
    share = os.path.join(base, "share")
    os.makedirs(share, exist_ok=True)
    data = {"roots": [share], "token": "abcdefghijklmnopqrstuvwxyz0123456789",
            "auto_backup": True, "public_health": True}
    data.update(over)
    return wa.Config(data, os.path.join(base, "c.json"), env={}), share


def tls_warn_keys(cfg):
    return {w.get("key") for w in (getattr(cfg, "config_warnings", None) or [])}


# ---------------------------------------------------------------------------
# 组 A：tls_cert / tls_key / tls_chain 的 fail-open 告警（审计 N21）
# ---------------------------------------------------------------------------
def groupA():
    print("\n-- 组 A：TLS 配置 null/类型不对/不匹配 必须告警，不能静默关 TLS --")
    # A1：tls_cert: null -> 告警 + tls_enabled 为 False
    cfg, _ = make_cfg({"tls_cert": None, "tls_key": ""})
    w = tls_warn_keys(cfg)
    check("tls_cert:null 触发 config_warnings", "tls_cert" in w, "keys=%s" % w)
    check("tls_cert:null 时 tls_enabled 为 False（明文）",
          wa.tls_enabled(cfg) is False, repr(wa.tls_enabled(cfg)))
    # A2：tls_cert 写成数字（非字符串）
    cfg, _ = make_cfg({"tls_cert": 123, "tls_key": "k.key"})
    w = tls_warn_keys(cfg)
    check("tls_cert:数字 触发 config_warnings", "tls_cert" in w, "keys=%s" % w)
    check("tls_cert:数字 时 tls_enabled 为 False", wa.tls_enabled(cfg) is False)
    # A3：cert 配了、key 写成空串（不匹配）
    cfg, _ = make_cfg({"tls_cert": "cert.crt", "tls_key": ""})
    w = tls_warn_keys(cfg)
    check("tls_cert 有值但 tls_key 空串 -> 不匹配告警",
          ("tls_cert" in w or "tls_key" in w), "keys=%s" % w)
    check("不匹配时 tls_enabled 为 False", wa.tls_enabled(cfg) is False)
    # A4：显式写空字符串 ""（合法关 TLS）-> 不告警
    cfg, _ = make_cfg({"tls_cert": "", "tls_key": ""})
    w = tls_warn_keys(cfg)
    check("tls_cert:\"\" 空串不告警（合法关 TLS）",
          "tls_cert" not in w and "tls_key" not in w, "keys=%s" % w)
    # A5：cert/key 都填成字符串（哪怕文件不存在）-> 配置层不告警
    #     （文件存在性由启动期 validate_tls_pair 管，不属于本 fix 范围）
    cfg, _ = make_cfg({"tls_cert": "cert.crt", "tls_key": "key.key"})
    w = tls_warn_keys(cfg)
    check("tls_cert/tls_key 都填字符串 -> 配置层不告警",
          "tls_cert" not in w and "tls_key" not in w, "keys=%s" % w)
    # A6：告警确实进 probe.issues（不让它只躺在 config_warnings 字段里）
    agent = wa.Agent(cfg)
    probe = agent.op_probe({})
    issues_txt = " ".join(probe.get("issues", []))
    # 用 A1 那种会告警的配置重新构造，确保 issues 里有 TLS 字样
    cfg2, _ = make_cfg({"tls_cert": None})
    agent2 = wa.Agent(cfg2)
    probe2 = agent2.op_probe({})
    issues2 = " ".join(probe2.get("issues", []))
    check("win_probe.issues 提到 TLS 配置异常（null 关 TLS）",
          ("TLS" in issues2) or ("tls_cert" in issues2), "issues=%s" % issues2[:300])


# ---------------------------------------------------------------------------
# 组 B：README 纠偏——`type .\deploy.ps1` 被 cwd 劫持拦，不是白名单；加白名单没用
# ---------------------------------------------------------------------------
def groupB():
    print("\n-- 组 B：type .\\deploy.ps1 被 cwd 劫持拦（非白名单），加白名单绕不过 --")
    base = tempfile.mkdtemp(prefix="wk1729b-")
    share = os.path.join(base, "share")
    os.makedirs(share, exist_ok=True)
    # 在 cwd（工作目录 = 授权根）里放一个 deploy.ps1，模拟「想读的脚本就在这里」
    with open(os.path.join(share, "deploy.ps1"), "w", encoding="utf-8") as fh:
        fh.write("Write-Output hi\n")
    cfg = wa.Config(
        {"roots": [share],
         "token": "abcdefghijklmnopqrstuvwxyz0123456789",
         "exec_enabled": True,
         "exec_allowed_commands": ["type"],   # 关键是：type 已经在白名单里
         "auto_backup": True, "public_health": True},
        os.path.join(base, "c.json"), env={})
    agent = wa.Agent(cfg)

    # B1：type 已在白名单，但仍被 cwd 劫持拦下（证明不是白名单机制、加白名单没用）
    try:
        agent._exec_guard_hijack("type .\\deploy.ps1", share)
        code = None
        msg = ""
    except wa.PathError as exc:
        code = exc.code
        msg = str(exc)
    check("type 在白名单时 `type .\\deploy.ps1` 仍被拦",
          code == "exec_hijack_suspected", "code=%s" % code)
    check("拦截码是 cwd 劫持（exec_hijack_suspected），不是白名单",
          code == "exec_hijack_suspected",
          "msg=%s" % msg[:160])

    # B2：裸文件名 `type deploy.ps1`（无 .\）在 type 白名单下不拦（没路径就不查）
    try:
        agent._exec_guard_hijack("type deploy.ps1", share)
        code2 = None
    except wa.PathError as exc:
        code2 = exc.code
    check("裸文件名 `type deploy.ps1`（type 白名单）放行", code2 is None,
          "code=%s" % code2)

    # B3：绝对路径 `type C:\abs\deploy.ps1` 不拦（不是 cwd 相对引用，正确绕法）
    abs_ps1 = os.path.join(base, "abs_deploy.ps1")
    with open(abs_ps1, "w", encoding="utf-8") as fh:
        fh.write("Write-Output hi\n")
    try:
        agent._exec_guard_hijack('type "%s"' % abs_ps1, share)
        code3 = None
    except wa.PathError as exc:
        code3 = exc.code
    check("绝对路径 `type C:\\abs\\deploy.ps1` 放行（正确绕法）", code3 is None,
          "code=%s" % code3)


if __name__ == "__main__":
    groupA()
    groupB()
    print("\n==== v1.7.29 回归：%d 通过 / %d 失败 ====" % (len(PASS), len(FAIL)))
    if FAIL:
        print("失败项：")
        for n in FAIL:
            print("  - " + n)
        raise SystemExit(1)
    print("全部通过。")
