# -*- coding: utf-8 -*-
"""v1.7.12 定向自测（WK-001 游标 / WK-004 围栏 / WK-006 原子写）。

只碰临时目录，不启动 HTTP 服务、不使用固定端口。
运行：python test/v1712/check.py
"""
import os
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
SERVER = os.path.abspath(os.path.join(HERE, "..", "..", "server"))
sys.path.insert(0, SERVER)
import wkfile_agent as wa  # noqa: E402

fails = []


def chk(name, cond, detail=""):
    print(("PASS " if cond else "FAIL ") + name + (("  " + str(detail)) if detail and not cond else ""))
    if not cond:
        fails.append(name)


def main():
    base = tempfile.mkdtemp(prefix="wk-v1712-")
    share = os.path.join(base, "share")
    os.makedirs(share)
    cfg = wa.Config({"roots": [share], "max_read_bytes": 4096, "max_write_bytes": 1 << 20},
                    os.path.join(base, "wkfile.config.json"))
    agent = wa.Agent(cfg)

    n = 1000
    lines = [f"L{i:04d} " + "x" * 30 for i in range(1, n + 1)]
    p = os.path.join(share, "lines.txt")
    with open(p, "w", encoding="utf-8", newline="\n") as f:
        f.write("\n".join(lines) + "\n")

    # T1 字节游标续读到 EOF，行数不丢
    page = agent.op_read({"path": p, "max_bytes": 4096, "line_numbers": False})
    parts, guard = [], 0
    while True:
        parts.append(page["content"])
        if page["eof"]:
            break
        page = agent.op_read({"path": p, "offset": page["next_offset"],
                              "start_line": page["next_start_line"],
                              "max_bytes": 4096, "line_numbers": False})
        guard += 1
        if guard > 200:
            break
    got = "\n".join(parts).splitlines()
    chk("T1 字节游标续读至 EOF 行数完整", got == lines, f"{len(got)}/{len(lines)}")
    chk("T1 首页 total_lines_partial=true",
        agent.op_read({"path": p, "max_bytes": 4096})["total_lines_partial"] is True)

    # T2 双游标（offset + start_line 同时传）不切空、行号连续
    p1 = agent.op_read({"path": p, "max_bytes": 4096})
    p2 = agent.op_read({"path": p, "offset": p1["next_offset"],
                        "start_line": p1["next_start_line"], "max_bytes": 4096})
    first = p2["content"].splitlines()[0] if p2["content"] else ""
    chk("T2 offset+start_line 不切空", p2["returned_lines"] > 0)
    chk("T2 行号连续", first.startswith(f"{p1['next_start_line']:>6}\t"), first[:24])

    # T3 max_lines 分页（行游标 + 字节游标）无丢行
    page = agent.op_read({"path": p, "max_lines": 10, "max_bytes": 1 << 20, "line_numbers": False})
    acc, guard = [], 0
    while True:
        acc += page["content"].splitlines()
        if page["eof"]:
            break
        page = agent.op_read({"path": p, "offset": page["next_offset"],
                              "start_line": page["next_start_line"],
                              "max_lines": 10, "max_bytes": 1 << 20, "line_numbers": False})
        guard += 1
        if guard > 500:
            break
    chk("T3 max_lines 分页无丢行", acc == lines, f"{len(acc)}/{len(lines)}")

    # T4 小文件 eof / total_lines 准确
    small = os.path.join(share, "small.txt")
    with open(small, "w", encoding="utf-8") as f:
        f.write("a\nb\nc\n")
    r = agent.op_read({"path": small})
    chk("T4 小文件 eof/total 准确",
        r["eof"] is True and r["total_lines"] == 3 and r["total_lines_partial"] is False)

    # T5 offset==size 合法 EOF；offset>size 报 bad_request
    r = agent.op_read({"path": small, "offset": os.path.getsize(small)})
    chk("T5 offset==size 返回空页", r["eof"] is True and r["returned_lines"] == 0)
    try:
        agent.op_read({"path": small, "offset": 9999})
        ok = False
    except wa.PathError as exc:
        ok = exc.code == "bad_request"
    chk("T5 offset>size 报 bad_request", ok)

    # T6 op_replace 原子写、无临时文件残留
    tgt = os.path.join(share, "rep.txt")
    with open(tgt, "w", encoding="utf-8") as f:
        f.write("hello old world\n")
    rr = agent.op_replace({"path": tgt, "old": "old", "new": "new"})
    with open(tgt, encoding="utf-8") as f:
        body = f.read()
    leftovers = [x for x in os.listdir(share) if "wkfile-tmp" in x]
    chk("T6 op_replace 生效", rr["replaced"] == 1 and body == "hello new world\n")
    chk("T6 无临时文件残留", not leftovers, leftovers)

    # T7 tls 目录进入围栏
    tls = os.path.join(base, "tls")
    os.makedirs(tls)
    with open(os.path.join(tls, "secret.txt"), "w", encoding="utf-8") as f:
        f.write("SECRET")
    try:
        agent.op_read({"path": os.path.join(tls, "secret.txt")})
        ok = False
    except wa.PathError as exc:
        ok = exc.code in ("denied", "out_of_scope")
    chk("T7 tls 目录被围栏拦截", ok)

    # T8 junction 不得把 roots 外的内容带出来
    link = os.path.join(share, "link_out")
    try:
        subprocess.run(["cmd", "/c", "mklink", "/J", link, base],
                       capture_output=True, check=True)
        tree = agent.op_tree({"path": share, "depth": 2})
        names = [e["name"] for e in tree["entries"]]
        hit = agent.op_grep({"root": share, "pattern": "SECRET",
                             "only_known_extensions": False})
        chk("T8a tree 不列 junction", "link_out" not in names, names)
        chk("T8b grep 不读 junction", hit["count"] == 0, hit["count"])
    except Exception as exc:  # noqa: BLE001
        print("SKIP T8 junction 创建失败：", exc)

    shutil.rmtree(base, ignore_errors=True)
    print("ALL PASS" if not fails else "FAIL: " + ", ".join(fails))
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
