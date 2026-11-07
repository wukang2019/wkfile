# -*- coding: utf-8 -*-
"""v1.7.20：乐观锁闭环回归（不启服务、不开窗口、不联网）。

三处真实缺口：
1) win_write 描述让 AI 把「win_read 返回的 sha256」传给 expected_sha256，
   但 op_read 从来不返回 sha256 —— AI 按描述做必然拿不到值。
2) expected_sha256 冲突时只回「请重新读取」，AI 得再跑一趟 win_info
   才知道当前 hash；错误里应直接给出当前事实。
3) win_replace 没有 expected_sha256，读取与替换之间别人改动会被静默覆盖。

全部用真实 Agent 调用 + MCP call_tool/render 文本断言（纯文本客户端也要能用）。
"""
import hashlib
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
    data = {"roots": [share], "max_read_bytes": 1 << 20,
            "max_write_bytes": 4 << 20, "auto_backup": False}
    data.update(over)
    cfg = wa.Config(data, os.path.join(base, "c.json"))
    cfg.base_dir = base
    return wa.Agent(cfg), cfg


def code_of(fn):
    try:
        fn()
    except wa.PathError as exc:
        return exc.code, exc.message
    return ("<no-error>", "")


def mcp_call(agent, name, **args):
    """走真实 MCP 分发，拿纯文本 + 结构化两份结果。"""
    return m.call_tool(agent.dispatch, {"name": name, "arguments": args})


def test_read_hash():
    print("\n========== 1. win_read(hash=true) 一次拿内容 + 全文件 sha256 ==========")
    base = tempfile.mkdtemp(prefix="wk-lock-read-")
    try:
        share = os.path.join(base, "share")
        os.makedirs(share)
        p = os.path.join(share, "config.ini")
        body = b"a=1\nb=2\nc=3\n"
        with open(p, "wb") as fh:
            fh.write(body)
        a, _ = make_agent(base, share)
        want = hashlib.sha256(body).hexdigest()

        plain = a.op_read({"path": p, "hash": False, "line_numbers": False})
        check("默认不返回 sha256（零开销，旧行为不变）", "sha256" not in plain)

        r = a.op_read({"path": p, "hash": True, "line_numbers": False})
        check("hash=true 返回全文件 sha256", r.get("sha256") == want, str(r.get("sha256")))
        check("内容照样返回", r["content"] == "a=1\nb=2\nc=3")

        # 分页时每一页给的都是全文件 hash，不是本页 hash
        small = a.op_read({"path": p, "hash": True, "max_bytes": 8})
        check("分页首篇也带全文件 sha256", small.get("sha256") == want)
        if small.get("next_offset"):
            nxt = a.op_read({"path": p, "hash": True, "max_bytes": 8,
                             "offset": small["next_offset"]})
            check("续读页的 sha256 仍是全文件值", nxt.get("sha256") == want)
        else:
            check("续读页的 sha256 仍是全文件值", True)

        # 纯文本客户端：MCP content[0].text 里必须看得见
        out = mcp_call(a, "win_read", path=p, hash=True, line_numbers=False)
        text = out["content"][0]["text"]
        check("纯文本渲染里含全文件 sha256", want in text)
        check("纯文本渲染里明确标「全文件」", "全文件 sha256=" in text)
        check("structuredContent 里也有 sha256",
              out.get("structuredContent", {}).get("sha256") == want)

        # 超限降级：读取不被中断，只说明原因
        a_big, _ = make_agent(base, share, max_read_bytes=8)
        r2 = a_big.op_read({"path": p, "hash": True})
        check("超哈希上限时 sha256 为 None", r2.get("sha256") is None)
        check("超哈希上限时不影响读取正文", "a=1" in r2["content"])
        check("超哈希上限给出原因", bool(r2.get("sha256_note")))
        out2 = mcp_call(a_big, "win_read", path=p, hash=True)
        check("纯文本也说明未计算原因", "未计算" in out2["content"][0]["text"])
    finally:
        shutil.rmtree(base, ignore_errors=True)


def test_write_conflict():
    print("\n========== 2. win_write 冲突回传当前事实（省一趟往返） ==========")
    base = tempfile.mkdtemp(prefix="wk-lock-write-")
    try:
        share = os.path.join(base, "share")
        os.makedirs(share)
        p = os.path.join(share, "app.cfg")
        with open(p, "wb") as fh:
            fh.write(b"v1\n")
        a, _ = make_agent(base, share)
        stale = hashlib.sha256(b"v1\n").hexdigest()

        # 正常写入（hash 匹配）
        ok = a.op_write({"path": p, "content": "v2\n",
                         "expected_sha256": stale})
        check("hash 匹配时正常写入", "v2\n" == open(p, encoding="utf-8").read())
        check("写入返回里带新 sha256", bool(ok.get("sha256")))

        # 有人改了文件，AI 拿着旧 hash 来写
        with open(p, "wb") as fh:
            fh.write(b"someone-else\n")
        cur = hashlib.sha256(b"someone-else\n").hexdigest()
        code, msg = code_of(lambda: a.op_write(
            {"path": p, "content": "mine\n", "expected_sha256": stale}))
        check("冲突被拒绝", code == "conflict", code)
        check("错误文本里给出当前 sha256", cur in msg, msg[:200])
        check("错误文本里给出当前大小", "13 字节" in msg, msg[:200])
        check("错误文本提示用 win_read(hash=true) 重试",
              "win_read(hash=true)" in msg)
        check("错误文本警告别直接去掉 expected_sha256",
              "别直接去掉" in msg)
        check("冲突后原文件没被覆盖",
              open(p, "rb").read() == b"someone-else\n")

        # AI 按错误里的当前 hash 重试即可成功，无需再调 win_info
        retry = a.op_write({"path": p, "content": "mine\n",
                            "expected_sha256": cur})
        check("按当前 hash 重试成功（无需额外探测）",
              open(p, encoding="utf-8").read() == "mine\n" and bool(retry.get("sha256")))

        # MCP 纯文本路径也能读到当前 hash
        with open(p, "wb") as fh:
            fh.write(b"third\n")
        out = mcp_call(a, "win_write", path=p, content="x\n",
                       expected_sha256=cur)
        txt = out["content"][0]["text"]
        check("MCP 错误文本含 conflict 码", "conflict" in txt)
        check("MCP 错误文本含当前 sha256",
              hashlib.sha256(b"third\n").hexdigest() in txt)

        # 大文件：明确拒绝，不硬算、也不静默跳过校验
        a_big, _ = make_agent(base, share, max_read_bytes=2)
        code2, msg2 = code_of(lambda: a_big.op_write(
            {"path": p, "content": "z\n", "expected_sha256": "whatever"}))
        check("超哈希上限明确拒绝写入（不静默跳过校验）", code2 == "too_large", code2)
        check("超哈希上限未硬算且原文件不变",
              open(p, "rb").read() == b"third\n")
        check("超哈希上限给出替代方案", "win_replace" in msg2)
    finally:
        shutil.rmtree(base, ignore_errors=True)


def test_replace_lock():
    print("\n========== 3. win_replace 的 expected_sha256 防静默覆盖 ==========")
    base = tempfile.mkdtemp(prefix="wk-lock-replace-")
    try:
        share = os.path.join(base, "share")
        os.makedirs(share)
        p = os.path.join(share, "settings.ini")
        with open(p, "wb") as fh:
            fh.write(b"port=8080\n")
        a, _ = make_agent(base, share)
        h = a.op_read({"path": p, "hash": True})["sha256"]

        r = a.op_replace({"path": p, "old": "8080", "new": "9090",
                          "expected_sha256": h})
        check("hash 匹配时正常替换", r["replaced"] == 1
              and b"port=9090" in open(p, "rb").read())

        with open(p, "wb") as fh:
            fh.write(b"port=7777\n")
        cur = hashlib.sha256(b"port=7777\n").hexdigest()
        code, msg = code_of(lambda: a.op_replace(
            {"path": p, "old": "9090", "new": "1234",
             "expected_sha256": h}))
        check("读取与替换之间被改动时拒绝", code == "conflict", code)
        check("冲突文本给出当前 sha256", cur in msg)
        check("冲突后文件未被覆盖", open(p, "rb").read() == b"port=7777\n")

        # dry_run 也必须校验（预览过期版本的 diff 同样误导）
        code2, _ = code_of(lambda: a.op_replace(
            {"path": p, "old": "7777", "new": "1", "dry_run": True,
             "expected_sha256": h}))
        check("dry_run 也照样校验", code2 == "conflict", code2)

        # 未命中路径也校验（不能因为 old 找不到就跳过并发保护）
        code3, _ = code_of(lambda: a.op_replace(
            {"path": p, "old": "NO_SUCH", "new": "x",
             "expected_sha256": h}))
        check("未命中路径也照样校验", code3 == "conflict", code3)

        # 按当前 hash 重试成功
        r2 = a.op_replace({"path": p, "old": "7777", "new": "9999",
                           "expected_sha256": cur})
        check("按当前 hash 重试成功", r2["replaced"] == 1
              and b"port=9999" in open(p, "rb").read())

        # 不传 expected_sha256 时行为完全不变
        r3 = a.op_replace({"path": p, "old": "9999", "new": "6060"})
        check("不传时保持旧行为", r3["replaced"] == 1)
    finally:
        shutil.rmtree(base, ignore_errors=True)


def test_schema_and_pure_fn():
    print("\n========== 4. schema 口径 + 纯函数边界 ==========")
    tools = {t["name"]: t for t in m.TOOLS}
    for name, key in (("win_read", "hash"),
                      ("win_write", "expected_sha256"),
                      ("win_replace", "expected_sha256"),
                      ("win_info", "hash")):
        check("%s schema 声明 %s" % (name, key),
              key in tools[name]["inputSchema"]["properties"])
    check("win_write 描述指明 win_read(hash=true) 才能拿到 sha256",
          "win_read(hash=true)" in tools["win_write"]["description"]
          and "先用 win_read 读一遍" not in tools["win_write"]["description"])
    check("win_write 参数说明澄清默认返回值不含 sha256",
          "不是 win_read 默认返回值" in
          tools["win_write"]["inputSchema"]["properties"]["expected_sha256"]["description"])
    check("win_replace 描述不再只字未提并发保护",
          "expected_sha256" in tools["win_replace"]["inputSchema"]["properties"])

    base = tempfile.mkdtemp(prefix="wk-lock-fn-")
    try:
        p = os.path.join(base, "x.bin")
        with open(p, "wb") as fh:
            fh.write(b"hello")
        d, why = wa.sha256_under_cap(p, 100)
        check("小文件正常算 hash",
              d == hashlib.sha256(b"hello").hexdigest() and why == "")
        d2, why2 = wa.sha256_under_cap(p, 2)
        check("超限返回 None + 原因", d2 is None and "超过哈希上限" in why2)
    finally:
        shutil.rmtree(base, ignore_errors=True)

    # 契约体积仍守护栏（本轮没往契约加字，靠 schema + 错误文本承载）
    info = {
        "server_name": "S", "version": wa.VERSION, "roots": [],
        "token_scope": "full",
        "limits": {"max_read_bytes": 4 << 20, "max_write_bytes": 16 << 20,
                   "structured_omit_bytes": 4096, "upload_hint_bytes": 8192},
        "flags": {"auto_backup": True, "allow_delete": True,
                  "exec_enabled": False, "exec_allowed_commands": [],
                  "exec_timeout_sec": 60, "exec_timeout_max_sec": 300,
                  "exec_strict_chain": False},
    }
    check("契约体积仍 < 1.5KB",
          len(m.runtime_contract(info).encode("utf-8")) < 1536)


if __name__ == "__main__":
    test_read_hash()
    test_write_conflict()
    test_replace_lock()
    test_schema_and_pure_fn()
    print("\nOPTIMISTIC_LOCK_TEST: %d PASS / %d FAIL" % (len(PASS), len(FAIL)))
    raise SystemExit(1 if FAIL else 0)
