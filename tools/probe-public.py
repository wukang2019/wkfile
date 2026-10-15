# -*- coding: utf-8 -*-
"""从开发机探测服务器公网 IP 的可达性"""
import socket
import subprocess
import sys
import urllib.request

TARGET = sys.argv[1] if len(sys.argv) > 1 else "1.2.3.4"
PORTS = [22, 3389, 8788, 10087, 80, 443, 8080]


def tcp(host, port, timeout=4.0):
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(timeout)
        rc = s.connect_ex((host, port))
        s.close()
        return rc == 0, rc
    except Exception as exc:  # noqa: BLE001
        return False, str(exc)


def ping(host, count=4):
    try:
        p = subprocess.run(
            ["ping", "-n", str(count), "-w", "1500", host],
            capture_output=True, text=True, encoding="utf-8",
            errors="replace", timeout=40,
        )
        return p.stdout or ""
    except Exception as exc:  # noqa: BLE001
        return "ping 调用失败: %s" % exc


def no_proxy_get(url, timeout=8.0, limit=400):
    """绕过本机代理直连，避免代理干扰判断"""
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "curl/8.0"})
        with opener.open(req, timeout=timeout) as resp:
            return resp.status, resp.read(limit).decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read(limit).decode("utf-8", "replace")
    except Exception as exc:  # noqa: BLE001
        return None, str(exc)


print("=" * 58)
print("  从开发机探测服务器  " + TARGET)
print("=" * 58)

code, body = no_proxy_get("https://api.ipify.org")
print("\n[本机公网出口 IP] " + (str(body).strip() if code == 200 else "查询失败: " + str(body)[:60]))
if code == 200 and str(body).strip() == TARGET:
    print("  ! 本机出口 IP 和目标相同 —— 说明你在服务器所在网络内部")

print("\n[ICMP ping]")
out = ping(TARGET)
for line in out.splitlines():
    if any(k in line for k in ("来自", "回复", "Reply", "丢失", "Lost", "无法访问", "timed out", "超时")):
        print("  " + line.strip())

print("\n[TCP 端口探测]")
for port in PORTS:
    ok, detail = tcp(TARGET, port)
    mark = "开放 <<<" if ok else "不通"
    err = "" if ok else "  (%s)" % detail
    print("  %-6s %s%s" % (port, mark, err))

print("\n[HTTP 指纹]  （v1.5 起 /health 免鉴权只回最小信息，/ 需要 Token）")
for port in (8788, 10087, 80, 8080):
    code, body = no_proxy_get("http://%s:%d/health" % (TARGET, port), timeout=6, limit=300)
    if code is None:
        print("  :%-6d 无响应 (%s)" % (port, str(body)[:70]))
    else:
        print("  :%-6d HTTP %s  %s" % (port, code, str(body)[:110].replace("\n", " ")))

print("\n" + "=" * 58)
