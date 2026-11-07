# -*- coding: utf-8 -*-
"""v1.7.12 抽验（reviewer）：双游标/残行、_backup 失败影响面、append 边界、换行压边界。

只碰临时目录，不起服务、不占端口、不改产品代码。
运行：python test/v1712/spotcheck.py
"""
import os
import shutil
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.abspath(os.path.join(HERE, "..", "..", "server")))
import wkfile_agent as wa  # noqa: E402

fails = []


def chk(name, cond, detail=""):
    print(("PASS " if cond else "FAIL ") + name + (("  " + str(detail)) if detail and not cond else ""))
    if not cond:
        fails.append(name)


def read_all(agent, path, max_bytes, max_lines=0, rounds=200):
    page = agent.op_read({"path": path, "max_bytes": max_bytes,
                          "max_lines": max_lines, "line_numbers": False})
    pages = [page]
    while not page["eof"]:
        page = agent.op_read({"path": path, "offset": page["next_offset"],
                              "start_line": page["next_start_line"],
                              "max_bytes": max_bytes, "max_lines": max_lines,
                              "line_numbers": False})
        pages.append(page)
        if len(pages) > rounds:
            break
    return pages


def main():
    base = tempfile.mkdtemp(prefix="wk-spot-")
    share = os.path.join(base, "share")
    os.makedirs(share)
    cfg = wa.Config({"roots": [share], "max_read_bytes": 4096, "max_write_bytes": 100},
                    os.path.join(base, "wkfile.config.json"))
    agent = wa.Agent(cfg)

    # ---------- A. 超长行导致字节截断落在行中间（limit=4096） ----------
    p = os.path.join(share, "longline.txt")
    logical = ["L1", "A" * 5000, "L3", "L4"]
    with open(p, "w", encoding="utf-8", newline="\n") as f:
        f.write("\n".join(logical) + "\n")
    size = os.path.getsize(p)
    pages = read_all(agent, p, 4096)
    print("A. pages =", len(pages),
          "returned_lines =", [x["returned_lines"] for x in pages],
          "consumed =", [x["consumed_bytes"] for x in pages],
          "bases =", [x["line_number_base"] for x in pages])
    chk("A1 续读到 EOF", pages[-1]["eof"] is True)
    chk("A2 sum(consumed_bytes) == size",
        sum(x["consumed_bytes"] for x in pages) == size,
        (sum(x["consumed_bytes"] for x in pages), size))
    chk("A3 sum(returned_lines) == 4",
        sum(x["returned_lines"] for x in pages) == len(logical),
        sum(x["returned_lines"] for x in pages))
    chk("A4 next_offset 每页前进且与下一页 offset 对齐",
        all(a["next_offset"] == b["offset"] and b["offset"] > a["offset"]
            for a, b in zip(pages, pages[1:])))
    chk("A5 硬上限未被突破",
        all(x["consumed_bytes"] <= 4096 for x in pages),
        [x["consumed_bytes"] for x in pages])
    # A6/A7：末页带行号读，L3/L4 必须为 3/4
    last = agent.op_read({"path": p, "offset": pages[-2]["next_offset"],
                          "start_line": pages[-1]["line_number_base"],
                          "max_bytes": 4096, "line_numbers": True})
    nums = [ln.split("\t", 1)[0].strip() for ln in last["content"].splitlines()]
    texts = [ln.split("\t", 1)[1] for ln in last["content"].splitlines() if "\t" in ln]
    chk("A6 L3 行号 = 3", "L3" in texts and nums[texts.index("L3")] == "3", nums)
    chk("A7 L4 行号 = 4", "L4" in texts and nums[texts.index("L4")] == "4", nums)

    # ---------- D. 单行 > max_read_bytes（4MB 配置，6MB 一行） ----------
    base2 = tempfile.mkdtemp(prefix="wk-spot-big-")
    share2 = os.path.join(base2, "share")
    os.makedirs(share2)
    cfg2 = wa.Config({"roots": [share2]}, os.path.join(base2, "wkfile.config.json"))
    agent2 = wa.Agent(cfg2)
    big = os.path.join(share2, "huge_one_line.txt")
    with open(big, "wb") as f:
        f.write(b"B" * (6 * 1024 * 1024) + b"\n" + b"tail\n")
    big_size = os.path.getsize(big)
    bpages = read_all(agent2, big, cfg2.max_read_bytes, rounds=20)
    print("D. pages =", len(bpages), "eof =", bpages[-1]["eof"],
          "consumed =", [x["consumed_bytes"] for x in bpages],
          "returned_lines =", [x["returned_lines"] for x in bpages])
    chk("D1 20 轮内到 EOF", bpages[-1]["eof"] is True and len(bpages) <= 20)
    chk("D2 sum(consumed_bytes) == size",
        sum(x["consumed_bytes"] for x in bpages) == big_size,
        (sum(x["consumed_bytes"] for x in bpages), big_size))
    chk("D3 累计行数 == 2", sum(x["returned_lines"] for x in bpages) == 2,
        sum(x["returned_lines"] for x in bpages))
    chk("D4 每页不超 4MB", all(x["consumed_bytes"] <= cfg2.max_read_bytes for x in bpages),
        [x["consumed_bytes"] for x in bpages])
    chk("D5 offset 严格递增",
        all(b["offset"] > a["offset"] for a, b in zip(bpages, bpages[1:])))

    # ---------- E. 换行符恰好压在窗口边界（显式 max_bytes=32 < max_read_bytes=64） ----------
    base3 = tempfile.mkdtemp(prefix="wk-spot-edge-")
    share3 = os.path.join(base3, "share")
    os.makedirs(share3)
    cfg3 = wa.Config({"roots": [share3], "max_read_bytes": 64}, os.path.join(base3, "wkfile.config.json"))
    agent3 = wa.Agent(cfg3)
    LIM = 32
    for n in (LIM - 1, LIM, LIM + 1):        # 换行压在边界前一字节 / 正好压边界 / 越界一字节
        p3 = os.path.join(share3, "edge%d.txt" % n)
        logical = ["L1", "A" * n, "L3", "L4"]
        data = ("\n".join(logical) + "\n").encode("utf-8")
        with open(p3, "wb") as f:
            f.write(data)
        pages = read_all(agent3, p3, LIM, rounds=10)
        tag = "E[n=%d]" % n
        print(tag, "pages =", len(pages),
              "returned =", [x["returned_lines"] for x in pages],
              "consumed =", [x["consumed_bytes"] for x in pages],
              "frag =", [x["line_fragment"] for x in pages])
        chk(tag + "1 有限轮次内到 EOF", pages[-1]["eof"] is True and len(pages) <= 10,
            (pages[-1]["eof"], len(pages)))
        chk(tag + "2 sum(consumed_bytes) == size",
            sum(x["consumed_bytes"] for x in pages) == len(data),
            (sum(x["consumed_bytes"] for x in pages), len(data)))
        chk(tag + "3 sum(returned_lines) == 逻辑行数 4",
            sum(x["returned_lines"] for x in pages) == len(logical),
            sum(x["returned_lines"] for x in pages))
        # 客户端视角：只用返回值里能拿到的 content_ends_with_newline 判断页尾换行。
        # 以前这里用 data[offset:offset+consumed]（测试持有原文）来判，等于在验
        # 「我说了什么」而不是「服务做了什么」——v1.7.17 改成客户端可得字段。
        seen = "".join(
            x["content"] + ("\n" if x["content_ends_with_newline"] else "")
            for x in pages
        )
        chk(tag + "4b 只用返回字段逐字节还原原文",
            seen.encode("utf-8") == data, (len(seen), len(data)))
        lines_all = seen.splitlines()
        chk(tag + "4 拼接行序列 == 逻辑行序列（无空串/重复/丢失）",
            lines_all == logical and "" not in lines_all, lines_all)
        chk(tag + "5 每页内容字节数 <= 本页上限",
            all(len(x["content"].encode("utf-8")) <= LIM for x in pages),
            [len(x["content"].encode("utf-8")) for x in pages])
        chk(tag + "6 任何一页都不以空行开头",
            all(not x["content"].startswith("\n") for x in pages),
            [x["content"][:8] for x in pages])
        # 逐页 content 必须等于该页消费区间（去掉行尾换行），不能多也不能少
        chk(tag + "7 逐页 content 可还原原文",
            all(x["content"] == data[x["offset"]: x["offset"] + x["consumed_bytes"]]
                .decode("utf-8").rstrip("\n") for x in pages))
        # 末页带行号：L3/L4 恒为 3/4
        lastp = pages[-1]
        num_page = agent3.op_read({"path": p3, "offset": lastp["offset"],
                                   "start_line": lastp["line_number_base"],
                                   "max_bytes": LIM, "line_numbers": True})
        nums = dict((ln.split("\t", 1)[1], ln.split("\t", 1)[0].strip())
                    for ln in num_page["content"].splitlines() if "\t" in ln)
        chk(tag + "8 L3/L4 行号为 3/4", nums.get("L3") == "3" and nums.get("L4") == "4", nums)
        if n == LIM:                          # 边界页：整行返回，不能判残行
            edge = [x for x in pages if x["content"] == "A" * LIM]
            chk(tag + "9 换行压在边界不判残行",
                len(edge) == 1 and edge[0]["line_fragment"] is False
                and edge[0]["returned_lines"] == 1,
                [(x["line_fragment"], x["returned_lines"]) for x in edge])
        if n == LIM + 1:                      # 真残页：行号基不前进
            frag = [x for x in pages if x["line_fragment"]]
            chk(tag + "10 真残页 line_fragment=true 且行号基不前进",
                len(frag) == 1 and frag[0]["returned_lines"] == 0
                and frag[0]["next_start_line"] == frag[0]["line_number_base"],
                [(x["returned_lines"], x["next_start_line"], x["line_number_base"]) for x in frag])

    # ---------- F. 末尾有/无换行 × 跨页/不跨页（v1.7.17 第三轮收口） ----------
    # 前两轮都在「按 line_fragment / eof 猜页尾换行」上翻车：末尾无换行的
    # 文件多补一个 \n，改成「非 eof 才补」又让末尾有换行的文件少一个 \n。
    # 这里只用客户端可得字段重建，逐字节比对，并校验累计行数。
    for label, data in (
        ("F-noeol-cross", b"A" * (LIM + 1)),
        ("F-eol-cross", b"X" * LIM + b"\nL2\n"),
        ("F-noeol-plain", b"L1\nL2"),
        ("F-eol-plain", b"L1\nL2\n"),
    ):
        pf = os.path.join(share3, label + ".txt")
        with open(pf, "wb") as f:
            f.write(data)
        pages = read_all(agent3, pf, LIM, rounds=10)
        rebuilt = "".join(
            x["content"] + ("\n" if x["content_ends_with_newline"] else "")
            for x in pages).encode("utf-8")
        chk(label + "1 累计 returned_lines == 逻辑行数",
            sum(x["returned_lines"] for x in pages) == len(data.decode().splitlines()),
            [x["returned_lines"] for x in pages])
        chk(label + "2 只用返回字段逐字节还原", rebuilt == data,
            (len(rebuilt), len(data)))
        chk(label + "3 有限轮次内到 EOF", pages[-1]["eof"] is True and len(pages) <= 10)

    # ---------- B. _backup 失败的影响面 ----------
    tgt = os.path.join(share, "t.txt")
    with open(tgt, "w", encoding="utf-8") as f:
        f.write("old")
    src = os.path.join(share, "src.txt")
    with open(src, "w", encoding="utf-8") as f:
        f.write("src")
    orig_copy2 = wa.shutil.copy2

    def boom(*_a, **_k):
        raise OSError("disk full (simulated)")

    wa.shutil.copy2 = boom
    try:
        try:
            agent.op_write({"path": tgt, "content": "new"})
            chk("B1 备份失败拦住写", False)
        except wa.PathError as exc:
            chk("B1 备份失败拦住写", exc.code == "backup_failed", exc.code)
        chk("B1 原文件不变", open(tgt, encoding="utf-8").read() == "old")
        try:
            agent.op_move({"src": src, "dst": tgt, "overwrite": True})
            chk("B2 备份失败拦住覆盖移动", False)
        except wa.PathError as exc:
            chk("B2 备份失败拦住覆盖移动", exc.code == "backup_failed", exc.code)
        chk("B2 源与目标都未变",
            os.path.isfile(src) and open(tgt, encoding="utf-8").read() == "old")
        try:
            agent.op_write({"path": tgt, "content": "new", "backup": False})
            chk("B3 backup=false 逃生门可用", True)
        except wa.PathError as exc:
            chk("B3 backup=false 逃生门可用", False, exc.code)
    finally:
        wa.shutil.copy2 = orig_copy2

    # ---------- C. op_append 边界（max_write_bytes=100） ----------
    a = os.path.join(share, "a.txt")
    with open(a, "wb") as f:
        f.write(b"x" * 99)
    agent.op_append({"path": a, "content": "y"})
    chk("C1 恰好 100 字节允许", os.path.getsize(a) == 100, os.path.getsize(a))
    try:
        agent.op_append({"path": a, "content": "z"})
        chk("C2 101 字节拒绝", False)
    except wa.PathError as exc:
        chk("C2 101 字节拒绝", exc.code == "too_large", exc.code)
    chk("C2 拒绝后文件仍 100 字节", os.path.getsize(a) == 100)

    shutil.rmtree(base, ignore_errors=True)
    shutil.rmtree(base2, ignore_errors=True)
    shutil.rmtree(base3, ignore_errors=True)
    print("ALL PASS" if not fails else "FAIL: " + ", ".join(fails))
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
