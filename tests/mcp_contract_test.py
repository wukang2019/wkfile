#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""initialize 运行时契约的纯函数回归（不启服务、不开窗口、不联网）。

验证点：契约随配置实时变、数字与协议层常量同源、旧版 info 可回落、
不泄露凭据、体积受控，以及 call_tool 的去重阈值确实等于契约里说的那个数。
"""

import os
import sys

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
    check("写明残页拼接规则", "line_fragment=true" in txt and "不补换行" in txt)
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


if __name__ == "__main__":
    test_default_contract()
    test_follows_config()
    test_legacy_and_initialize()
    test_threshold_is_shared()
    print("\nMCP_CONTRACT_TEST: %d PASS / %d FAIL" % (len(PASS), len(FAIL)))
    raise SystemExit(1 if FAIL else 0)
