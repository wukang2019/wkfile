#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""initialize 运行时契约的纯函数回归（不启服务、不开窗口、不联网）。

验证点：契约随配置实时变、数字与协议层常量同源、旧版 info 可回落、
不泄露凭据、体积受控，以及 call_tool 的去重阈值确实等于契约里说的那个数。
"""

import os
import sys
import tempfile
import shutil

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(BASE, "server"))

import wkfile_agent as wa  # noqa: E402
import wkfile_mcp_endpoint as m  # noqa: E402

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print("  %s %s%s" % ("[OK]  " if cond else "[FAIL]", name,
                         "  :: %s" % detail if detail and not cond else ""))


def base_info(**over):
    info = {
        "server_name": "WIN-SRV01",
        "version": wa.VERSION,
        "roots": [r"D:\share\app"],
        "read_only": False,
        "token_scope": "full",
        "tools_disabled": ["win_exec"],
        "limits": {
            "max_read_bytes": 4 * 1024 * 1024,
            "max_write_bytes": 16 * 1024 * 1024,
            "structured_omit_bytes": m.STRUCTURED_CONTENT_OMIT_BYTES,
            "upload_hint_bytes": m.BASE64_UPLOAD_HINT_BYTES,
        },
        "flags": {
            "auto_backup": True,
            "allow_delete": True,
            "exec_enabled": False,
            "exec_allowed_commands": [],
            "exec_timeout_sec": 60,
            "exec_strict_chain": False,
        },
    }
    info.update(over)
    return info


def test_default_contract():
    info = base_info()
    txt = m.runtime_contract(info)
    print("\n========== 1. 默认配置下的契约 ==========")
    check("带运行时契约标题", "【运行时契约" in txt)
    check("版本号来自 info", wa.VERSION in txt)
    check("凭据权限来自 info", "凭据权限 full" in txt)
    check("读上限来自 info", "单次读 4 MB" in txt)
    check("写上限来自 info", "单次写与请求体 16 MB" in txt)
    check("上传引导阈值来自常量",
          m.human_bytes(m.BASE64_UPLOAD_HINT_BYTES) in txt)
    check("响应去重阈值来自常量",
          m.human_bytes(m.STRUCTURED_CONTENT_OMIT_BYTES) in txt)
    check("写明续读只看 eof", "eof=true" in txt and "next_offset" in txt)
    check("契约写明页尾换行字段", "content_ends_with_newline" in txt)
    check("契约不再让用户按 line_fragment/eof 猜换行",
          "其余页补一个" not in txt and "不要按 line_fragment / eof 猜" in txt)
    check("写明备份失败整体中止", "整体中止" in txt and "原文件不动" in txt)
    check("写明 backup=false", "backup=false" in txt)
    check("写明 BOM 限制", "utf-8-sig" in txt)
    check("exec 默认关闭有提示", "win_exec 默认关闭" in txt)
    check("不泄露凭据值或密钥字段",
          not any(k in txt for k in ("Bearer", "password", "secret",
                                     "readonly_token", "token\":")))
    check("契约体积 < 1.5KB", len(txt.encode("utf-8")) < 1536,
          str(len(txt.encode("utf-8"))))


def test_follows_config():
    print("\n========== 2. 契约随配置实时变化（不写死） ==========")
    info = base_info(
        token_scope="readonly",
        limits={"max_read_bytes": 1024, "max_write_bytes": 65536,
                "structured_omit_bytes": 2048, "upload_hint_bytes": 4096},
        flags={"auto_backup": False, "allow_delete": False,
               "exec_enabled": True,
               "exec_allowed_commands": ["php", "composer", "git", "node",
                                        "python", "robocopy", "xcopy"],
               "exec_timeout_sec": 25, "exec_strict_chain": True},
    )
    txt = m.runtime_contract(info)
    check("只读 Token 生效", "凭据权限 readonly" in txt)
    check("读上限跟着换成 1 KB", "单次读 1 KB" in txt)
    check("写上限跟着换成 64 KB", "单次写与请求体 64 KB" in txt)
    check("去重阈值跟着换成 2 KB", "2 KB" in txt)
    check("上传阈值跟着换成 4 KB", "4 KB" in txt)
    check("备份关闭生效", "自动备份关" in txt)
    check("禁止删除生效", "当前禁止删除" in txt)
    check("exec 开启生效", "win_exec 已开启" in txt)
    check("白名单只列前 6 项并给出总数", "等 7 项" in txt and "xcopy" not in txt)
    check("超时值跟着变", "超时 25 秒" in txt)
    check("严格链式标志跟着变", "严格链式校验开" in txt)
    check("不含服务端口令/主机名等无关信息", "WIN-SRV01" not in txt)


def test_legacy_and_initialize():
    print("\n========== 3. 旧 info 兼容 + 握手下发 ==========")
    legacy = {"server_name": "X", "version": "1.7.0",
              "roots": [], "tools_disabled": []}
    txt = m.runtime_contract(legacy)
    check("缺 limits/flags 不抛异常", isinstance(txt, str) and txt)
    check("缺字段时回落模块常量",
          m.human_bytes(m.STRUCTURED_CONTENT_OMIT_BYTES) in txt)
    check("缺字段时 exec 按关闭呈现", "win_exec 默认关闭" in txt)
    check("读上限未知用 ? 而不是编数字", "单次读 ?" in txt)

    res = m.build_initialize({"protocolVersion": "2025-03-26"}, info=base_info())
    ins = res["instructions"]
    check("initialize 里含原有建议流程", "先调用 win_roots" in ins)
    check("initialize 里含运行时契约", "【运行时契约" in ins)
    check("initialize 里含 roots", r"D:\share\app" in ins)
    check("initialize 里含被禁用工具提示", "win_exec" in ins)
    check("整个 instructions 体积 < 3KB", len(ins.encode("utf-8")) < 3072,
          str(len(ins.encode("utf-8"))))
    check("serverInfo 不受影响", res["serverInfo"]["version"] == wa.VERSION)
    check("第 5 条不再绝对声称会自动备份",
          "写入成功后服务端会自动备份" not in ins and "默认会先备份" in ins)


def test_threshold_is_shared():
    print("\n========== 4. 契约数字与 call_tool 实际阈值同源 ==========")

    def dispatch(action, payload):
        return {"content": payload["_body"], "eof": True}

    def call(n):
        body = "A" * n
        params = {"name": "win_read", "arguments": {"path": "x.txt"}}
        out = m.call_tool(lambda a, p: dispatch(a, {"_body": body}), params)
        return out["structuredContent"]

    n = m.STRUCTURED_CONTENT_OMIT_BYTES
    small = call(n)
    big = call(n + 1)
    check("阈值内不省略 content", small.get("content") == "A" * n)
    check("超阈值省略 content", "content" not in big)
    check("超阈值有 content_omitted 标记", big.get("content_omitted") is True)
    check("超阈值字节数准确", big.get("content_bytes") == n + 1)
    check("契约里写的是同一个阈值",
          m.human_bytes(n) in m.runtime_contract(base_info()))


def test_contract_matches_real_behavior():
    """验「代码做了什么」，不是「契约说了什么」。

    第三轮同一个坑的根因：契约写的是启发式拼接规则，测试只断言字符串存在。
    这里喂真实文件给 Agent 逐页读，只用返回值里客户端能拿到的字段重建，
    逐字节比对原文，并校验各页 returned_lines 之和等于逻辑行数。
    """
    print("\n========== 5. 行为断言：真实文件逐页读 + 逐字节还原 ==========")
    base = tempfile.mkdtemp(prefix="wk-contract-")
    old_rule_fails = False
    trae_rule_fails = False
    try:
        share = os.path.join(base, "share")
        os.makedirs(share)
        N = 16
        cfg = wa.Config({"roots": [share], "max_read_bytes": N,
                         "max_write_bytes": 1 << 20},
                        os.path.join(base, "c.json"))
        agent = wa.Agent(cfg)
        cases = [
            ("单行超长跨页 无尾换行", b"A" * (N + 1)),
            ("两行 末行长跨页 无尾换行", b"L1\n" + b"B" * (N + 1)),
            ("普通两行 有尾换行", b"L1\nL2\n"),
            ("普通两行 无尾换行", b"L1\nL2"),
            ("跨页且末尾有换行", b"X" * N + b"\nL2\n"),
            ("多行跨页 无尾换行",
             b"A" * 20 + b"\n" + b"B" * 20 + b"\n" + b"C" * 20),
        ]

        def read_pages(ag, path, limit):
            page = ag.op_read({"path": path, "max_bytes": limit,
                               "line_numbers": False})
            pages = [page]
            while not page["eof"] and page["next_offset"]:
                page = ag.op_read({"path": path,
                                   "offset": page["next_offset"],
                                   "start_line": page["next_start_line"],
                                   "max_bytes": limit,
                                   "line_numbers": False})
                pages.append(page)
                if len(pages) > 12:
                    break
            return pages

        def verify(label, data, pages):
            nonlocal old_rule_fails, trae_rule_fails
            rebuilt = "".join(
                x["content"] + ("\n" if x["content_ends_with_newline"] else "")
                for x in pages).encode()
            logical = len(data.decode().splitlines())
            checks = {
                "累计 returned_lines == 逻辑行数":
                    sum(x["returned_lines"] for x in pages) == logical,
                "按契约字段逐字节还原": rebuilt == data,
                "字节不丢": sum(x["consumed_bytes"] for x in pages) == len(data),
                "最后一页 eof": pages[-1]["eof"] is True,
                "每页都下发页尾换行字段":
                    all("content_ends_with_newline" in x for x in pages),
            }
            bad = [k for k, v in checks.items() if not v]
            check("%s（%d 页）" % (label, len(pages)), not bad,
                  ",".join(bad) or "")
            # 反例保护：两种启发式规则都不能覆盖全部样本，否则新字段就白加了
            old = "".join(x["content"] + ("" if x["line_fragment"] else "\n")
                          for x in pages).encode()
            trae = "".join(
                x["content"] + ("" if (x["line_fragment"] or x["eof"]) else "\n")
                for x in pages).encode()
            old_rule_fails = old_rule_fails or old != data
            trae_rule_fails = trae_rule_fails or trae != data

        for label, data in cases:
            p = os.path.join(share, "t.txt")
            with open(p, "wb") as fh:
                fh.write(data)
            verify(label, data, read_pages(agent, p, N))

        # Trae 的实测样本：4 MB + 1 字节、结尾无换行、跨页
        big_n = 4 * 1024 * 1024
        cfg_big = wa.Config({"roots": [share], "max_read_bytes": big_n,
                             "max_write_bytes": big_n + 1024},
                            os.path.join(base, "big.json"))
        big_data = b"A" * (big_n + 1)
        p = os.path.join(share, "big.txt")
        with open(p, "wb") as fh:
            fh.write(big_data)
        bp = read_pages(wa.Agent(cfg_big), p, big_n)
        verify("4MB+1 结尾无换行（Trae 复现样本）", big_data, bp)
        check("4MB+1 确实跨成 2 页且累计行数为 1",
              len(bp) == 2 and sum(x["returned_lines"] for x in bp) == 1,
              str([(x["returned_lines"], x["eof"]) for x in bp]))

        check("旧契约规则确实无法逐字节还原（反例有效）", old_rule_fails)
        check("Trae 建议的 eof 规则也无法逐字节还原（反例有效）", trae_rule_fails)
    finally:
        shutil.rmtree(base, ignore_errors=True)


if __name__ == "__main__":
    test_default_contract()
    test_follows_config()
    test_legacy_and_initialize()
    test_threshold_is_shared()
    test_contract_matches_real_behavior()
    print("\nMCP_CONTRACT_TEST: %d PASS / %d FAIL" % (len(PASS), len(FAIL)))
    raise SystemExit(1 if FAIL else 0)
