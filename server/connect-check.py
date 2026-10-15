# -*- coding: utf-8 -*-
"""wkfile 连接自检 —— 在【服务器】上运行

用途：一眼看出 MCP 客户端（Trae / Cursor / Claude Desktop 等）到底该怎么填、
现在还差哪一步。
只读，不修改任何配置、不写任何文件。
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))

CONFIG_CANDIDATES = [
    # 数据目录统一叫 wkfile：exe 旁边就是 wkfile\wkfile.config.json
    os.path.join(HERE, "wkfile", "wkfile.config.json"),
    # exe 自己就放在 wkfile\ 目录里时，配置与 exe 同级
    os.path.join(HERE, "wkfile.config.json"),
    # 脚本放在 server\ 子目录、而 exe 在上一级时（开发布局）也要找得到，
    # 否则会静默回退到 FALLBACK_CFG 的 8788，给出完全错误的结论
    os.path.join(os.path.dirname(HERE), "wkfile", "wkfile.config.json"),
    os.path.join(os.path.dirname(HERE), "wkfile.config.json"),
    os.path.join(os.getcwd(), "wkfile", "wkfile.config.json"),
    # 兼容早期版本遗留的配置文件名
    os.path.join(HERE, "agent.config.json"),
    os.path.join(os.path.dirname(HERE), "agent.config.json"),
]

FALLBACK_CFG = {"host": "0.0.0.0", "port": 8788, "token": "", "enable_mcp": True,
                "tls_cert": "", "tls_key": "", "tls_chain": "", "readonly_token": ""}

# 服务端配了证书就按 https 探测；main() 里按真实配置覆盖
SCHEME = "http"

BAR = "=" * 62


def hr(title: str = "") -> None:
    if title:
        print("\n" + BAR)
        print("  " + title)
        print(BAR)
    else:
        print(BAR)


def ok(msg: str) -> None:
    print("  [ OK ] " + msg)


def bad(msg: str) -> None:
    print("  [FAIL] " + msg)


def warn(msg: str) -> None:
    print("  [WARN] " + msg)


def info(msg: str) -> None:
    print("  [ .. ] " + msg)


# --------------------------------------------------------------------------
# 配置
# --------------------------------------------------------------------------

def load_config():
    for path in CONFIG_CANDIDATES:
        if os.path.isfile(path):
            try:
                with open(path, "r", encoding="utf-8-sig") as f:
                    raw = json.load(f)
                cfg = dict(FALLBACK_CFG)
                cfg.update({k: v for k, v in raw.items() if v is not None})
                return cfg, path
            except Exception as exc:  # noqa: BLE001
                warn(f"读取 {path} 失败：{exc}")
    return dict(FALLBACK_CFG), None


# --------------------------------------------------------------------------
# 本机 IP
# --------------------------------------------------------------------------

def ps_json(script: str):
    """调用 PowerShell 取 JSON；失败返回 None。"""
    try:
        proc = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=25,
        )
    except Exception:  # noqa: BLE001
        return None
    out = (proc.stdout or "").strip()
    if not out:
        return None
    try:
        return json.loads(out)
    except Exception:  # noqa: BLE001
        return None


def list_ipv4():
    """返回 [(网卡名, IP, 前缀长度)]，忽略回环与 APIPA。"""
    data = ps_json(
        "Get-NetIPAddress -AddressFamily IPv4 | "
        "Where-Object { $_.IPAddress -notlike '127.*' -and $_.IPAddress -notlike '169.254.*' } | "
        "Select-Object InterfaceAlias,IPAddress,PrefixLength | ConvertTo-Json -Compress"
    )
    rows = []
    if isinstance(data, dict):
        data = [data]
    if isinstance(data, list):
        for item in data:
            try:
                rows.append(
                    (
                        str(item.get("InterfaceAlias", "?")),
                        str(item.get("IPAddress", "")),
                        int(item.get("PrefixLength", 0) or 0),
                    )
                )
            except Exception:  # noqa: BLE001
                continue

    if not rows:
        # 退化方案：UDP trick 拿默认出口 IP
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.connect(("8.8.8.8", 80))
            rows.append(("默认出口", s.getsockname()[0], 0))
            s.close()
        except Exception:  # noqa: BLE001
            pass
    return rows


VIRTUAL_HINTS = (
    "vmware",
    "virtualbox",
    "hyper-v",
    "vethernet",
    "wsl",
    "docker",
    "npcap",
    "tap-",
    "loopback",
    "virtual",
)


def rank_ips(rows):
    """给 IP 排推荐次序：组网网卡最优先，虚拟网卡直接沉底。"""
    scored = []
    for alias, ip, prefix in rows:
        low = alias.lower()
        tag = ""
        if "zerotier" in low or "tailscale" in low:
            score = 300
            tag = "组网虚拟网卡 —— 跨网络首选"
        elif any(h in low for h in VIRTUAL_HINTS):
            score = -1000
            tag = "虚拟网卡（WSL/虚拟机等），不要选"
        elif _is_private(ip):
            score = 100
            tag = "局域网地址"
        else:
            score = 40
            tag = "看起来是公网地址"
        scored.append((score, alias, ip, prefix, tag))
    scored.sort(key=lambda x: -x[0])
    return scored


def _is_private(ip: str) -> bool:
    if ip.startswith("10.") or ip.startswith("192.168."):
        return True
    return _is_172_private(ip)


def _is_172_private(ip: str) -> bool:
    parts = ip.split(".")
    if len(parts) != 4 or parts[0] != "172":
        return False
    try:
        return 16 <= int(parts[1]) <= 31
    except ValueError:
        return False


def is_public_ip(ip: str) -> bool:
    """是不是一个「可以填进客户端配置」的公网 IPv4。"""
    import ipaddress

    try:
        addr = ipaddress.ip_address(str(ip).strip())
    except ValueError:
        return False
    return (addr.version == 4 and not (addr.is_private or addr.is_loopback
                                       or addr.is_link_local or addr.is_reserved
                                       or addr.is_multicast or addr.is_unspecified))


# 公网 IP 回显服务：返回的都是近纯文本，顺序按「国内可达 + 响应干净」排
PUBIP_ENDPOINTS = (
    "https://ip.3322.net/",
    "https://api.ipify.org",
    "https://ipv4.icanhazip.com",
    "https://myip.ipip.net",
)


def detect_public_ip(timeout: float = 4.0) -> str:
    """探测本机公网 IPv4；拿不到返回空串。

    两个刻意的选择：
      1. 绕开系统代理 —— 走代理拿到的是代理服务器的 IP，那是错的答案；
      2. 不信任服务返回的格式，只从正文里捞第一个合法公网 IPv4，
         这样 ipify 的裸 IP 和 ipip 的「当前 IP：x.x.x.x 来自于…」都能吃。
    NAT 后面的机器这里拿到的仍是出口公网 IP —— 只要做过端口映射，
    那正好就是客户端该填的地址。
    """
    import re as _re
    import urllib.request

    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    for url in PUBIP_ENDPOINTS:
        try:
            with opener.open(url, timeout=timeout) as resp:
                body = resp.read(4096).decode("utf-8", "replace")
        except Exception:  # noqa: BLE001
            continue
        for token in _re.findall(r"\d{1,3}(?:\.\d{1,3}){3}", body):
            if is_public_ip(token):
                return token
    return ""


# --------------------------------------------------------------------------
# 服务状态
# --------------------------------------------------------------------------

def count_cert_chain(cert_path: str) -> int:
    """数证书文件里带了几张证书。

    PEM 按 BEGIN CERTIFICATE 块数；读不到或二进制 DER 一律按 1 张算
    （DER 通常就是单张，按 1 张算会走「链不完整」的提示，是安全的一侧）。
    """
    try:
        with open(os.path.abspath(str(cert_path or "")), "rb") as fh:
            blob = fh.read()
    except OSError:
        return 0
    return blob.count(b"-----BEGIN CERTIFICATE-----") or 1


def port_listening(port: int) -> bool:
    """本机连本机，不涉及 SO_REUSEADDR 抢占问题，结果可信。"""
    for host in ("127.0.0.1", "::1"):
        family = socket.AF_INET6 if ":" in host else socket.AF_INET
        try:
            s = socket.socket(family, socket.SOCK_STREAM)
            s.settimeout(2.0)
            if s.connect_ex((host, port)) == 0:
                s.close()
                return True
            s.close()
        except Exception:  # noqa: BLE001
            continue
    return False


def opener():
    """统一走这个 opener：绕开系统代理，并在服务端启用了 HTTPS 时接受自签证书。

    本脚本只是判断"通不通"，不承担证书信任判断 —— 自签证书也要能连上，
    否则每个没买证书的人都会看到一个假的"连不上"。
    """
    import ssl

    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return urllib.request.build_opener(
        urllib.request.ProxyHandler({}),
        urllib.request.HTTPSHandler(context=ctx),
    )


def http_get(base: str, path: str, token: str, timeout: float = 5.0):
    url = base.rstrip("/") + path
    req = urllib.request.Request(url, method="GET")
    if token:
        req.add_header("Authorization", "Bearer " + token)
    try:
        with opener().open(req, timeout=timeout) as resp:
            return resp.status, resp.read(200000).decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read(200000).decode("utf-8", "replace")
    except Exception as exc:  # noqa: BLE001
        return None, str(exc)


def mcp_initialize(base: str, token: str, timeout: float = 8.0):
    """POST /mcp 发一次 initialize，确认 MCP 协议层真的可用。"""
    body = json.dumps(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "connect-check", "version": "1.0"},
            },
        }
    ).encode("utf-8")

    req = urllib.request.Request(base.rstrip("/") + "/mcp", data=body, method="POST")
    req.add_header("Content-Type", "application/json")
    req.add_header("Accept", "application/json, text/event-stream")
    if token:
        req.add_header("Authorization", "Bearer " + token)
    try:
        with opener().open(req, timeout=timeout) as resp:
            text = resp.read(200000).decode("utf-8", "replace")
            sid = resp.headers.get("Mcp-Session-Id", "")
            return resp.status, text, sid
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read(200000).decode("utf-8", "replace"), ""
    except Exception as exc:  # noqa: BLE001
        return None, str(exc), ""


def parse_payload(text: str):
    """兼容 JSON 与 SSE 单条事件两种返回。"""
    text = (text or "").strip()
    if not text:
        return None
    if text.startswith("event:") or "\ndata:" in text or text.startswith("data:"):
        for line in text.splitlines():
            line = line.strip()
            if line.startswith("data:"):
                chunk = line[5:].strip()
                try:
                    return json.loads(chunk)
                except Exception:  # noqa: BLE001
                    continue
        return None
    try:
        return json.loads(text)
    except Exception:  # noqa: BLE001
        return None


FW_RULE = "wkfile"


def firewall_report(port: int):
    """一次调用拿到防火墙开关状态 + wkfile 放行规则。

    按 DisplayName 精确查（CIM 侧过滤，很快），不做全量枚举——
    全量枚举几千条规则会直接超时，之前的实现就栽在这里。
    """
    script = (
        "$profiles = Get-NetFirewallProfile | Select-Object Name,Enabled; "
        "$rule = Get-NetFirewallRule -DisplayName '" + FW_RULE + "' -ErrorAction SilentlyContinue | "
        "ForEach-Object { $p = $_ | Get-NetFirewallPortFilter; "
        "'{0}|{1}|{2}|{3}' -f $_.Enabled, $_.Direction, $_.Action, $p.LocalPort }; "
        "[pscustomobject]@{ profiles = @($profiles); rule = @($rule) } "
        "| ConvertTo-Json -Depth 4 -Compress"
    )
    data = ps_json(script)
    if not isinstance(data, dict):
        return None, None, "查不到防火墙信息（可能权限不足）"

    profiles = data.get("profiles") or []
    if isinstance(profiles, dict):
        profiles = [profiles]

    rules = data.get("rule") or []
    if isinstance(rules, str):  # PS 5.1 单元素数组会退化成标量
        rules = [rules]

    return profiles, rules, None


# --------------------------------------------------------------------------
# 主流程
# --------------------------------------------------------------------------

def main() -> int:
    print(BAR)
    print("  wkfile 连接自检  ——  AI 远程管文件，还差哪一步")
    print(BAR)
    print("  主机名：" + socket.gethostname())
    print("  时间  ：" + __import__("time").strftime("%Y-%m-%d %H:%M:%S"))

    cfg, cfg_path = load_config()

    hr("1. 服务端配置")
    if cfg_path:
        ok("配置文件：" + cfg_path)
    else:
        warn("没找到 wkfile.config.json，按默认值推断（仅供参考）")
    port = int(cfg.get("port") or 8788)
    host = str(cfg.get("host") or "0.0.0.0")
    token = str(cfg.get("token") or "")
    enable_mcp = bool(cfg.get("enable_mcp", True))
    global SCHEME
    use_tls = bool(str(cfg.get("tls_cert") or "").strip()
                   and str(cfg.get("tls_key") or "").strip())
    SCHEME = "https" if use_tls else "http"
    has_ro_token = bool(str(cfg.get("readonly_token") or "").strip())
    print("      监听地址 : " + host)
    print("      监听端口 : " + str(port))
    print("      MCP 端点 : " + ("已启用" if enable_mcp else "已关闭  <-- 关掉的话客户端连不上"))
    if token:
        ok("Token 已设置（长度 %d）" % len(token))
    else:
        bad("Token 为空！任何人都能读写你的文件，强烈建议在界面里点「生成」")

    hr("2. 服务是否在跑")
    alive = port_listening(port)
    if alive:
        ok("端口 %d 可以连上，服务在运行" % port)
    else:
        bad("端口 %d 连不上 —— 服务没启动，或端口号和配置里的不一致" % port)
        print("      解决：回到界面页签③点「启动服务」，再点「刷新状态」")

    if use_tls:
        ok("服务端已启用 HTTPS（TLS 证书），本机探测走 %s" % SCHEME)
        tls_chain = str(cfg.get("tls_chain") or "").strip()
        n_cert = count_cert_chain(str(cfg.get("tls_cert") or ""))
        n_chain = count_cert_chain(tls_chain) if tls_chain else 0
        if tls_chain and n_chain == 0:
            bad("tls_chain 指向的文件里没读到证书：%s" % tls_chain)
        total = n_cert + n_chain
        if total <= 1:
            bad("证书链不完整：能往外发的只有 %d 张证书，没带中间证书" % total)
            print("      这是「浏览器能开、客户端连不上」的典型原因：")
            print("        浏览器和 Windows 会顺着证书里的 AIA 地址自动把中间证书补回来，")
            print("        所以你在浏览器里试是正常的、完全看不出问题；")
            print("        但 Trae / Cursor 这类 Electron 客户端用 Node 的 TLS 栈，不补链，")
            print("        会直接报 unable to verify the first certificate 而连不上。")
            print("      两个改法（任选其一，私钥都不用换）：")
            print("        ① 把 tls_cert 换成证书商给的「证书链 / public chain / fullchain」")
            print("           文件（里面通常有 2~3 张证书）；")
            print("        ② 证书商把「公钥证书」和「中间链」分成两个文件给的时候，把链文件")
            print("           填进配置的 tls_chain（界面页③「中间证书链（可选）」那一栏）。")
        else:
            src = ("%d + %d 合并" % (n_cert, n_chain)) if tls_chain else "单文件 %d 张" % n_cert
            ok("证书链共 %d 张证书（%s），客户端可自行完成验证" % (total, src))
    if has_ro_token:
        info("配置里有只读 Token —— 用它的客户端只能读，不能改")

    base = "%s://127.0.0.1:%d" % (SCHEME, port)
    http_ok = False
    tools_count = None
    if alive:
        status, text = http_get(base, "/", token)
        payload = parse_payload(text)
        if status == 200 and payload:
            data = payload.get("data") if isinstance(payload, dict) else None
            if isinstance(data, dict) and data.get("service") in ("wkfile", "winfile-agent"):
                http_ok = True
                ver = data.get("version", "?")
                mcp_info = data.get("mcp") or {}
                tools_count = mcp_info.get("tools")
                ok("HTTP 服务正常响应，版本 %s" % ver)
                if tools_count:
                    ok("MCP 工具数量：%s 个" % tools_count)
                if mcp_info.get("enabled") is False:
                    bad("服务端 MCP 处于关闭状态")
        elif status == 401:
            bad("401 —— Token 不对（配置里的 token 和请求用的不一致）")
        elif status == 404:
            warn("根路径返回 404，但不一定是坏事，继续测 MCP")

        if enable_mcp:
            status, text, sid = mcp_initialize(base, token)
            payload = parse_payload(text)
            if status == 200 and isinstance(payload, dict) and "result" in payload:
                sname = (payload["result"].get("serverInfo") or {}).get("name", "?")
                ok("MCP initialize 成功（%s，会话 %s）" % (sname, (sid[:8] + "…") if sid else "无"))
                print("      这一侧完全就绪：客户端只要网络能通，就能连上")
            elif status == 401:
                bad("MCP initialize 返回 401 —— Token 不正确")
            else:
                bad("MCP initialize 失败：HTTP %s  %s" % (status, (text or "")[:160]))

    hr("3. 本机有哪些 IP（决定客户端该填哪个）")
    rows = list_ipv4()
    ranked = []
    best_ip = None
    if not rows:
        warn("取不到网卡信息，手动执行 ipconfig 看一下")
    else:
        ranked = rank_ips(rows)
        for idx, (score, alias, ip, prefix, tag) in enumerate(ranked):
            mark = ""
            if idx == 0 and score > 0:
                mark = "  <== 内网推荐填这个"
                best_ip = ip
            line = "      %-16s %s" % (ip, alias)
            if prefix:
                line += " /%d" % prefix
            if tag:
                line += "   [" + tag + "]"
            print(line + mark)
        if best_ip is None:
            warn("上面没有可用的局域网地址，全是虚拟网卡或公网地址")
        has_overlay = any(
            "zerotier" in a.lower() or "tailscale" in a.lower() for _, a, _, _, _ in ranked
        )
        if not has_overlay:
            print()
            info("没看到组网虚拟网卡（ZeroTier / Tailscale）")
            print("      如果开发机和服务器不在同一张网里，上面这些地址在开发机那边都不通。")
            print("      要跨网络连上，最省事的是两边都装 ZeroTier 加入同一网络。")

    # 客户端在别的机器/别的网络时，内网 IP 根本到不了 —— 优先探公网 IP
    print()
    info("正在探测本机公网 IP（判断客户端该填公网还是内网）…")
    pub_ip = detect_public_ip()
    if pub_ip:
        ok("公网 IP：%s" % pub_ip)
        print("      客户端不在同一局域网时，MCP 配置里就填这个地址；")
        print("      前提是把 %d 端口在路由器/安全组里映射到本机（映射不通就填了也没用）。" % port)
    else:
        warn("没探到公网 IP（服务器没有直接出网，或被防火墙/代理挡住了）")
        print("      内网机器这很正常：公网地址要看你路由器的宽带 IP，或云控制台里的弹性 IP。")
        print("      只有确认过端口映射/安全组放行之后，才把公网 IP 填进客户端配置。")

    hr("4. 防火墙（服务端本机）")
    profiles, fw_rules, fw_err = firewall_report(port)
    if fw_err:
        warn(fw_err + "，可在界面里点「放行当前端口」再试")
    else:
        for p in profiles:
            state = "已开启" if p.get("Enabled") in (True, 1, "True") else "已关闭"
            print("      防火墙 %-8s : %s" % (str(p.get("Name", "?")), state))

        allowed_ports = set()
        for r in fw_rules:
            parts = [x.strip() for x in str(r).split("|")]
            if len(parts) != 4:
                continue
            en, dr, ac, lp = parts
            allowed = ac.lower() == "allow" and en.lower() == "true"
            print(
                "      规则 %s：%s，方向 %s，端口 %s"
                % (FW_RULE, "已放行" if allowed else "未放行", dr, lp)
            )
            if allowed:
                allowed_ports.update(x.strip() for x in str(lp).split(","))

        if not fw_rules:
            warn("没找到名为 %s 的放行规则" % FW_RULE)
            print("      解决：界面页签③点「放行当前端口」（需管理员身份运行）")
        elif str(port) not in allowed_ports and "Any" not in allowed_ports:
            warn("放行规则里没有 %d 端口，和当前配置对不上" % port)
            print("      解决：界面页签③重新点一次「放行当前端口」")
        else:
            ok("端口 %d 已在放行列表里" % port)

    hr("5. MCP 客户端里该填什么")
    # 公网优先：客户端在另一台电脑/另一个网络时，内网 IP 一定到不了
    target_ip = pub_ip or best_ip or "服务器IP"
    target_kind = "公网" if pub_ip else ("内网" if best_ip else "待确认")
    print("  在 MCP 客户端（Trae / Cursor / Claude Desktop 等）的配置里加这一段：")
    print()
    snippet = {
        "mcpServers": {
            "wkfile": {
                "type": "http",
                "url": "%s://%s:%d/mcp" % (SCHEME, target_ip, port),
                "headers": {"Authorization": "Bearer " + (token or "在服务器上生成的Token")},
            }
        }
    }
    print(json.dumps(snippet, ensure_ascii=False, indent=2))
    print()
    print("  上面填的是【%s】地址：%s" % (target_kind, target_ip))
    if pub_ip and best_ip and pub_ip != best_ip:
        print("      客户端与服务器在同一个局域网时，把地址换成内网 IP 更快也更稳：")
        print("          %s://%s:%d/mcp" % (SCHEME, best_ip, port))
    elif not pub_ip and best_ip:
        print("      没探到公网 IP，所以这里先给内网地址 —— 客户端不在同一网络时连不上。")
        print("      跨网络请改成公网 IP（前提：端口映射/安全组已放行），或两边都装 ZeroTier。")
    print()
    print("  如果客户端只认 SSE：type 改成 \"sse\"，url 末尾 /mcp 换成 /sse")
    if use_tls:
        print("  服务端用了自签证书的话，客户端首次连接会提示证书不受信任，确认一次即可。")
    if has_ro_token:
        print("  只想让 AI 看、不让它改：上面这段的 Token 换成配置里的 readonly_token 即可。")

    hr("6. 下一步：先解决网络，再谈其他")
    print("  在【开发机】上跑这条命令，判断能不能到服务器：")
    print()
    print("      Test-NetConnection %s -Port %d" % (target_ip, port))
    print()
    print("  通了 -> 客户端直接填上面的配置，收工，不需要 sshd、不需要隧道。")
    print()
    print("  关于 SSH 隧道（只有这一种情况才值得用）：")
    print("      22 端口能连、%d 端口连不上时才需要隧道，" % port)
    print("      典型场景是云服务器安全组只放行了 22。")
    print("      两个端口都能连的话，隧道纯属多余，别折腾。")
    print("  不通 -> 三选一（按推荐度）：")
    print("      A. 让开发机连上服务器同一个路由器/WiFi（最省事）")
    print("      B. 两边都装 ZeroTier 加入同一网络，用它的虚拟 IP（跨网络首选）")
    print("      C. 服务器有公网IP就做端口映射，客户端填公网IP（务必开 Token + IP 白名单）")

    hr()
    print("  服务器本机这一侧：" + ("已就绪" if (alive and http_ok) else "还有问题，见上面 FAIL 项"))
    print(BAR)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\n已中断")
        sys.exit(1)
