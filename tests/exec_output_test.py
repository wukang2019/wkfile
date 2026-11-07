# -*- coding: utf-8 -*-
"""v1.7.18 win_exec 输出压缩 + win_tail 白名单口回归（不启服务、不开窗口）。

外部反馈：命令输出超限只留头部，而 composer / 迁移脚本的真正报错在尾部；
落盘的 logs/exec-*.txt 只有 win_read 能读，win_tail 被「数据目录禁访」拒绝。
"""
import os
import sys
import tempfile
import shutil

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "server"))
import wkfile_agent as wa  # noqa: E402

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print("  %s %s%s" % ("[OK]  " if cond else "[FAIL]", name,
                         "  :: %s" % detail if detail and not cond else ""))


def big_text(n=3000):
    return "\n".join("line-%04d" % i for i in range(n)) + "\nFATAL_AT_END\n"


def test_preview():
    print("\n========== 1. 双端预览纯函数 ==========")
    P = wa.Agent._exec_preview
    text = big_text(200)
    prev, omitted = P(text, r"C:\logs\exec-x.txt", 2048, 20, 40)
    check("头部保留", "line-0000" in prev)
    check("尾部报错保留", "FATAL_AT_END" in prev)
    check("中段已删", "line-0100" not in prev)
    check("省略行数准确", omitted == 141, str(omitted))
    check("预览不超上限", len(prev.encode("utf-8")) <= 2048)
    check("提示里带落盘路径", "exec-x.txt" in prev)

    one = "A" * 20000 + "\nFATAL_AT_END"
    prev2, _ = P(one, None, 1024, 20, 40)
    check("超长单行按字节兜底仍保尾部", "FATAL_AT_END" in prev2
          and len(prev2.encode("utf-8")) <= 1024, str(len(prev2.encode())))

    for h, t in ((0, 0), (5, 5), (500, 500)):
        p, _ = P(text, None, 2048, h, t)
        check("行预算 %d/%d 头尾都不丢" % (h, t),
              "line-0000" in p and "FATAL_AT_END" in p
              and len(p.encode("utf-8")) <= 2048)
    p_only_tail, _ = P(text, None, 2048, 0, 10)
    check("只给尾预算时仍保尾部", "FATAL_AT_END" in p_only_tail)


def make_agent(base, share, max_out=1024):
    cfg = wa.Config({
        "roots": [share], "max_read_bytes": 1 << 20, "max_write_bytes": 1 << 20,
        "exec_enabled": True, "exec_allowed_commands": ["type"],
        "exec_max_output_bytes": max_out, "auto_backup": False,
    }, os.path.join(base, "c.json"))
    cfg.base_dir = base
    return wa.Agent(cfg)


def test_real_exec():
    print("\n========== 2. 真实 op_exec + win_tail 闭环 ==========")
    base = tempfile.mkdtemp(prefix="wk-execout-")
    try:
        share = os.path.join(base, "share")
        os.makedirs(share)
        with open(os.path.join(share, "big.txt"), "w", encoding="utf-8") as fh:
            fh.write(big_text())
        a = make_agent(base, share)

        r = a.op_exec({"command": "type big.txt", "cwd": share,
                       "head_lines": 5, "tail_lines": 5})
        check("命令真的跑成功", r["exit_code"] == 0, str(r.get("output", "")[:120]))
        check("输出被判定截断", r["output_truncated"] is True)
        check("预览保留头部", "line-0000" in r["output"])
        check("预览保留尾部报错", "FATAL_AT_END" in r["output"])
        check("预览不含中段", "line-1500" not in r["output"])
        check("省略行数随参数变化", r["output_omitted_lines"] > 2900,
              str(r["output_omitted_lines"]))
        check("完整输出已落盘", bool(r["output_file"])
              and os.path.isfile(r["output_file"]))
        check("预览字节不超服务端上限", r["output_preview_bytes"] <= 1024,
              str(r["output_preview_bytes"]))
        tail = a.op_tail({"path": r["output_file"], "lines": 3})
        check("win_tail 能读 exec 落盘输出（旧版被拒）",
              "FATAL_AT_END" in str(tail.get("content", tail)),
              str(tail)[:160])
        rd = a.op_read({"path": r["output_file"], "max_bytes": 1 << 20})
        check("win_read 仍可读全文", "line-0000" in rd["content"])

        r2 = a.op_exec({"command": "type big.txt", "cwd": share,
                        "max_output_bytes": 999999})
        check("按调用不能放大服务端上限（仍截断）", r2["output_truncated"] is True)
        check("放大请求下预览仍不超上限", r2["output_preview_bytes"] <= 1024,
              str(r2["output_preview_bytes"]))
        r3 = a.op_exec({"command": "type big.txt", "cwd": share,
                        "max_output_bytes": 300, "head_lines": 3, "tail_lines": 3})
        check("按调用调小上限生效", r3["output_preview_bytes"] <= 300,
              str(r3["output_preview_bytes"]))
        check("调小后尾部报错仍在", "FATAL_AT_END" in r3["output"])
    finally:
        shutil.rmtree(base, ignore_errors=True)


if __name__ == "__main__":
    test_preview()
    test_real_exec()
    print("\nEXEC_OUTPUT_TEST: %d PASS / %d FAIL" % (len(PASS), len(FAIL)))
    raise SystemExit(1 if FAIL else 0)
