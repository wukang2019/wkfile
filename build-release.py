#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""生成「服务器部署包」文件夹 + 同名 zip。

    python build-release.py

产出（都在项目根目录）：
    wkfile-<版本>\\        整套拷到服务器就能用的文件夹
    wkfile-<版本>.zip      同样内容的压缩包

为什么不用 PowerShell 的 Compress-Archive：PS 5.1 会用系统代码页写 zip 里的
文件名，中文名在别的解压工具里会变乱码。这里用 stdlib zipfile 并显式打开
UTF-8 标志位（0x800），到哪都正常。

发布包的布局是**扁平的**：connect-check.py 必须与 exe 同级 ——
它靠"自己旁边"去找数据目录，放进子目录会静默回退到错误默认值。

不会打进包里的东西：wkfile.config.json（含真实 Token）、.trae/mcp.json、
wkfile\\（运行时数据目录）、以及任何 build 中间产物。
"""

from __future__ import annotations

import os
import re
import shutil
import sys
import zipfile

ROOT = os.path.dirname(os.path.abspath(__file__))

APP = "wkfile"


def read_version() -> str:
    path = os.path.join(ROOT, "server", "wkfile_agent.py")
    with open(path, "r", encoding="utf-8") as fh:
        m = re.search(r'^VERSION\s*=\s*"([^"]+)"', fh.read(), re.M)
    if not m:
        raise SystemExit("读不到 server/wkfile_agent.py 里的 VERSION")
    return m.group(1)


# 目标相对路径 -> 源相对路径（None 表示由本脚本生成）
LAYOUT = [
    (f"{APP}.exe",                       f"dist/{APP}.exe"),
    ("使用说明.txt",                       "README-快速上手.txt"),
    ("README.md",                        "README.md"),
    ("connect-check.py",                 "server/connect-check.py"),
    ("连接自检.bat",                       "server/连接自检.bat"),
    (f"{APP}.config.example.json",        f"server/{APP}.config.example.json"),
    ("tools/probe-public.py",            "tools/probe-public.py"),
    ("tools/mcp-public-test.py",         "tools/mcp-public-test.py"),
    ("tools/mcp-call-test.py",           "tools/mcp-call-test.py"),
]

# 这些后缀在包里统一成 CRLF：.bat 是为了 cmd.exe，.txt 是为了记事本双击可读
CRLF_EXTS = (".bat", ".cmd", ".ps1", ".txt")

# 预建的数据目录里放的说明（程序第一次运行也会自动建，这里是让人一眼看懂）
DATA_NOTE = """这个文件夹是 wkfile 自己的数据目录 —— 运行时唯一会被创建的地方。
====================================================================

程序启动后，所有状态都收在这里，不会往别处（尤其不会往你允许 AI
操作的业务目录里）撒任何文件：

    wkfile.config.json      配置（含访问 Token，别外传、别提交版本库）
    wkfile.pid              正在运行的服务进程号
    logs\\wkfile.log         运行日志（默认超 2 MB 轮转，保留 3 份）
    logs\\audit.jsonl       审计日志（默认超 5 MB 轮转，保留 3 份）
    backups\\               改写文件前的自动备份，按时间戳分目录
                            （AI 可以用 win_backup_list / win_backup_restore 查看与回滚）
    trash\\                 删掉的东西（可用 trash_id 恢复，不是真删）
    tls\\                   启用 HTTPS 时，用界面上的「生成自签证书」签出来的
                            wkfile.crt / wkfile.key 默认放这里（私钥别外传）

想整体搬走或彻底清干净，停掉服务后删掉本文件夹即可（配置会一起没）。
"""

LAUNCHER_BAT = """@echo off
chcp 65001 >nul
rem ---------------------------------------------------------------------------
rem  Start wkfile.exe with Administrator rights.
rem  Needed for: install/uninstall auto-start (scheduled task) and firewall rules.
rem  For plain configuration you can just double-click wkfile.exe instead.
rem ---------------------------------------------------------------------------
powershell -NoProfile -Command "$p = Join-Path '%~dp0' 'wkfile.exe'; if (-not (Test-Path -LiteralPath $p)) { Write-Host '[X] wkfile.exe not found next to this script.'; exit 1 }; try { Start-Process -FilePath $p -Verb RunAs -ErrorAction Stop } catch { Write-Host '[X] Elevation denied or failed. Close this window and retry as admin.'; exit 1 }"
if errorlevel 1 pause
"""

FOLDER_README = """wkfile v{ver} · 部署包
==========================================================

这个文件夹里的东西【整套拷到 Windows 服务器】就能用。
不用装 Python、不用装 sshd、不用开隧道、不用在开发机装任何东西。

  Q: 服务器上到底要不要装 Python？
  A: 一条都不用。Python 解释器已经打进 wkfile.exe 内部了，拷过去双击就跑。
     本项目还【零第三方依赖】，任何情况下都不需要 pip install；
     连自签证书都是程序自己算出来的，不需要 openssl；
     连 MySQL / Redis 驱动也是自己手写的协议实现，不需要 PyMySQL / redis-py，
     也不需要机器上装 mysql.exe / redis-cli。
     （只有"源码版" python wkfile_agent.py 才要求机器上装 Python 3.8+，
       同样不需要 pip install。源码版一般用不到。）
     系统前提：Windows 10 / Server 2016 及以上。

跑 AI 客户端（Trae / Cursor / Claude Desktop 等）的那台电脑，
只需要在客户端里粘一段 JSON。

程序运行时只会创建一个文件夹：就是这里的 wkfile\\ ——
配置、日志、审计、备份、回收站全在里面，不会往别处乱放文件。


--------------------------------------------------------
先看什么
--------------------------------------------------------

  使用说明.txt     ← 先读这个，从零到跑通的全流程（纯文本，双击可看）
  README.md        ← 完整文档（含安全清单、自带 HTTPS 怎么配、常见问题）


--------------------------------------------------------
怎么用（三步）
--------------------------------------------------------

  1. 把整个文件夹拷到服务器，例如 D:\\wkfile-server\\

  2. 双击【以管理员身份启动.bat】打开配置界面
     （装开机自启、放行防火墙需要管理员权限；
       只是看看配置的话，直接双击 wkfile.exe 也行）

       ① 基础设置 → 添加允许 AI 操作的目录（越窄越好）+ 填端口
       ② 安全与权限 → 点「生成」拿 Token；按需勾只读 / 工具级开关
                     （想让 AI「只看不改」，在这页再生成一个「只读 Token」）
       ③ 安装与运行 → 保存配置 → 启动服务 → 安装开机自启 → 放行当前端口
                     想让 Token 不明文过公网，在这页勾上「HTTPS」——
                     没域名就点「生成自签证书」，程序按本机 IP 现场签一张
                     （纯本地运算，不装 openssl、不联网），自动填好并检查一遍；
                     有域名就用正式证书：证书商给你几份，就对着填几个框
                       · crt + key 两份       → 第 1 行填 crt，第 3 行填 key
                       · public + chain + key → 三份依次填进三个框，自动合并
                       · 一份 fullchain       → 第 1 行填它，中间链留空
                     选错证书文件是"浏览器能开、客户端连不上"的头号原因，
                     界面点【检查证书】会当场告诉你链全不全
       ④ 数据库       → 可选。想让 AI 除了文件还能看数据库，就在这页点
                     【＋ MySQL】/【＋ Redis】加一条，填好地址账号，
                     点【测试连接】验一次，再保存配置重启服务。
                     默认只读（只能查询）；【允许写入】要放开写操作，
                     且还需服务端非只读 + 用主 Token，三个条件缺一不可。
                     「连接超时」管拨号、「查询超时」管等结果，是两个框：
                     连不上的等待上限默认 8 秒，填 0/负数/乱码会自动退回默认。
                     不填任何数据源 = 这个能力完全关闭，不影响别的功能。
                     建议单独建一个只读数据库账号，别直接填 root。
                     不想用界面也行：记事本打开 wkfile\\wkfile.config.json，
                     里面 databases 那一段自带注释和可照抄的示例。
                     （程序第一次运行就会生成这个文件，不用自己建。）
       ⑤ 关于         → 作者 吴康，微信 wukang（有一键复制）。
                     纯展示页，不联网、不影响任何功能。署名也印在窗口最底部。

  3. 点【复制 MCP 配置】，把剪贴板里的 JSON 粘进客户端
     （Trae：设置 → MCP → 添加 → 手动配置 → 原始配置）
     地址会自动填好：程序启动时探测本机公网 IP 写进去
     （客户端在另一个网络时内网 IP 一定连不上），也可以手改


--------------------------------------------------------
文件说明
--------------------------------------------------------

  wkfile.exe                   ★ 服务器端主程序：图形配置界面 + 常驻文件服务
                                 单文件绿色版，卸载 = 删掉
  以管理员身份启动.bat           带 UAC 提权地打开上面这个界面
  使用说明.txt / README.md      文档

  connect-check.py            连接自检脚本（只读，不改任何东西）
  连接自检.bat                 双击运行它；报告配置/端口/IP/防火墙/MCP 是否正常
                               ※ 必须和 exe 同目录，它靠"自己旁边"找数据目录

  wkfile.config.example.json  配置样例。真配置是程序自动生成的
                              wkfile\\wkfile.config.json（含 Token，不要外传）

  wkfile\\                      运行时数据目录（配置/日志/备份/回收站都在这）
                               ※ 目录名就叫 wkfile，程序只创建这一个文件夹

  tools\\                      从任意机器跑的连通性诊断脚本（需要那台机器有 Python）
    probe-public.py             探端口开没开
    mcp-public-test.py          验 MCP 端点（initialize + tools/list）
    mcp-call-test.py            真调一个工具（如 win_roots / win_list）


--------------------------------------------------------
运行时会产生什么（全部在 wkfile\\ 里）
--------------------------------------------------------

  wkfile\\wkfile.config.json     配置（含 Token，务必别外传）
  wkfile\\wkfile.pid             服务进程号
  wkfile\\logs\\wkfile.log        运行日志（超 2 MB 自动轮转，保留 3 份）
  wkfile\\logs\\audit.jsonl       审计日志（超 5 MB 自动轮转，保留 3 份）
                              每次操作都记，含 MCP 工具调用；用只读 Token 的请求带 scope 标记
  wkfile\\backups\\              改写文件前的自动备份（AI 可查看、可回滚）
  wkfile\\trash\\                删除的东西（可恢复，不是真删）
  wkfile\\tls\\                  用「生成自签证书」签出来的证书与私钥默认放这里；
                              填了「中间证书链」时，合并出来的 fullchain.pem 也在这里
                              （服务实际加载的是它，出问题时可以打开核对）


--------------------------------------------------------
卸载
--------------------------------------------------------

  1. 打开程序：先「停止服务」，再「卸载开机自启」和「关闭放行」
  2. 删掉本文件夹即可
  3. 客户端侧把 MCP 配置里的 wkfile 项删掉
"""


def is_crlf_target(name: str) -> bool:
    return name.lower().endswith(CRLF_EXTS)


def write_text(path: str, text: str) -> None:
    newline = "\r\n" if is_crlf_target(path) else "\n"
    with open(path, "w", encoding="utf-8", newline=newline) as fh:
        fh.write(text.replace("\r\n", "\n").replace("\n", newline))


def main() -> int:
    ver = read_version()
    name = f"{APP}-v{ver}"
    folder = os.path.join(ROOT, name)
    zip_path = os.path.join(ROOT, name + ".zip")

    if os.path.isdir(folder):
        shutil.rmtree(folder)
    os.makedirs(folder)

    missing = []
    for rel_dst, rel_src in LAYOUT:
        src = os.path.join(ROOT, rel_src.replace("/", os.sep))
        dst = os.path.join(folder, rel_dst.replace("/", os.sep))
        if not os.path.isfile(src):
            missing.append(rel_src)
            continue
        os.makedirs(os.path.dirname(dst) or folder, exist_ok=True)
        if is_crlf_target(rel_dst):
            # .bat 用 LF 结尾时 cmd.exe 在带标签/跳转的脚本上可能出错；
            # .txt 用 LF 结尾时旧版记事本会挤成一行
            with open(src, "r", encoding="utf-8", errors="replace") as fh:
                body = fh.read()
            write_text(dst, body)
        else:
            shutil.copy2(src, dst)
        print("  + %-34s <- %s" % (rel_dst, rel_src))

    if missing:
        print("\n[X] 缺少源文件，发布包不完整：")
        for m in missing:
            print("     - " + m)
        return 2

    # 预建运行时数据目录（含说明），让人一眼看懂"东西都收在这里"
    data_dir = os.path.join(folder, APP)
    os.makedirs(data_dir, exist_ok=True)
    write_text(os.path.join(data_dir, "数据目录说明.txt"), DATA_NOTE)
    print("  + %-34s <- (生成)" % f"{APP}\\数据目录说明.txt")

    for rel_dst, text in (
        ("以管理员身份启动.bat", LAUNCHER_BAT),
        ("文件夹说明.txt", FOLDER_README.format(ver=ver)),
    ):
        write_text(os.path.join(folder, rel_dst), text)
        print("  + %-34s <- (生成)" % rel_dst)

    # 打包：显式设 UTF-8 名字标志，避免解压端按本地代码页解读
    if os.path.isfile(zip_path):
        os.remove(zip_path)
    count = 0
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as z:
        for dirpath, dirnames, filenames in os.walk(folder):
            dirnames.sort()
            for fn in sorted(filenames):
                full = os.path.join(dirpath, fn)
                arc = os.path.relpath(full, folder).replace(os.sep, "/")
                info = zipfile.ZipInfo.from_file(full, arc)
                info.flag_bits |= 0x800
                info.compress_type = zipfile.ZIP_DEFLATED
                with open(full, "rb") as fh, z.open(info, "w") as out:
                    out.write(fh.read())
                count += 1

    print("\n文件夹 : %s" % folder)
    print("压缩包 : %s  (%.2f MB, %d 个文件)" % (
        zip_path, os.path.getsize(zip_path) / 1048576, count))

    # 自检：包里绝不能出现含 Token 的真实配置 / 本地 MCP 配置
    bad = []
    for dirpath, _dn, filenames in os.walk(folder):
        for fn in filenames:
            if fn in (f"{APP}.config.json", "agent.config.json", "mcp.json"):
                bad.append(os.path.join(dirpath, fn))
    if bad:
        print("\n[X] 包内出现疑似含 Token 的文件，请检查：")
        for b in bad:
            print("     - " + b)
        return 3
    print("敏感文件检查：干净（无真实配置 / mcp.json）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
