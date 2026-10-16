#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
wkfile —— 跑在 Windows 服务器上的「文件查看/修改」HTTP 服务

运行时只在自己身边创建【一个】文件夹 wkfile\\，配置、PID、运行日志、
审计日志、改写备份、删除回收站全部收在里面，不会往被操作的目录里撒东西。

设计目标与运行依赖
------------------
* 零第三方依赖：只用 Python 标准库，任何时候都不需要 pip install。
  连自签证书都是自己手写 ASN.1/DER + RSA 生成的 —— 不需要 openssl，
  不需要 cryptography，不需要任何包。
* 要不要装 Python，取决于你走哪条路线（两者能力完全相同，任选其一）：
    ① 单文件 wkfile.exe —— Python 解释器已经打进 exe 里了，
       目标服务器【什么都不用装】，不需要 Python，双击即用（推荐）
    ② 源码 wkfile_agent.py —— 需要目标机器上有 Python 3.8+，
       仍然不需要 pip install；适合没有桌面、或 exe 被杀软/策略拦住的场合
* 版本下限 3.8 的原因只有一个：自签证书模块用了 pow(a, -1, m) 求模逆，
  这个写法 3.8 才支持。其余代码都是 3.7 就有的东西。
* 默认只开放显式配置的根目录（白名单），越界一律拒绝
* 写文件前自动备份；删除默认进回收站（可恢复），不直接抹掉
* 所有请求写审计日志（JSONL）
* Bearer Token 鉴权 + 可选 IP 白名单 + 失败次数锁定

启动
----
    python wkfile_agent.py --init --roots D:\\share            # 生成配置 + Token
    python wkfile_agent.py                                     # 按配置启动
    python wkfile_agent.py --host 0.0.0.0 --port 8788 --token xxx --roots D:\\share

接口一览（全部 POST + JSON，除 health）
----
    GET  /api/health
    POST /api/roots          POST /api/disk
    POST /api/list           POST /api/stat
    POST /api/read           POST /api/read_b64
    POST /api/write          POST /api/write_b64
    POST /api/replace        POST /api/append
    POST /api/mkdir          POST /api/move
    POST /api/copy           POST /api/trash
    POST /api/search         POST /api/grep
    POST /api/trash_list     POST /api/trash_restore
"""

from __future__ import annotations

import argparse
import base64
import difflib
import fnmatch
import hashlib
import hmac
import ipaddress
import json
import os
import re
import queue
import secrets
import shutil
import socket
import ssl
import sys
import tempfile
import threading
import time
import traceback
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

try:
    import wkfile_mcp_endpoint as mcpmod
except Exception:  # noqa: BLE001 —— 缺模块时只是关掉 MCP，服务本身照常可用
    mcpmod = None

# 数据库能力同样是可缺省的：模块不在就只是这几个工具不可用
try:
    import wkfile_mysql as dbmod
except Exception:  # noqa: BLE001
    dbmod = None

try:
    import wkfile_redis as redismod
except Exception:  # noqa: BLE001
    redismod = None

try:
    import wkfile_sqlsrv as sqlsrvmod
except Exception:  # noqa: BLE001
    sqlsrvmod = None

VERSION = "1.7.3"
SERVICE = "wkfile"

# 作者署名。想改成自己的名字/微信号，只改这两行就够了 ——
# 图形界面标题、页⑤「关于」、底部署名、以及配置里的 author 字段都读这几个常量。
AUTHOR = "吴康"
AUTHOR_WECHAT = "wukang"

# 运行时数据目录名：exe 旁边只会出现这一个文件夹，所有状态都在里面
APP_DIRNAME = "wkfile"
CONFIG_FILENAME = "wkfile.config.json"
PID_FILENAME = "wkfile.pid"
LOG_DIRNAME = "logs"
LOG_FILENAME = "wkfile.log"
AUDIT_FILENAME = "audit.jsonl"
BACKUP_DIRNAME = "backups"
TRASH_DIRNAME = "trash"

DEFAULT_CONFIG = {
    "host": "0.0.0.0",
    "port": 8788,
    "token": "",
    # Token 有效期：0 = 永久有效；>0 = 自签发起 N 天后自动失效。
    # token_expires_at 是据此算出的绝对到期时间（ISO），空字符串表示永久。
    "token_ttl_days": 0,
    "token_expires_at": "",
    # 只读专用 Token：用这个 Token 认证的请求一律被当成只读
    # （写/删/移/还原全部拒绝），适合「只让 AI 看看」的场景。留空表示不启用。
    "readonly_token": "",
    "roots": [],
    "read_only": False,
    "allow_delete": True,
    "allow_move": True,
    "auto_backup": True,
    "ip_allowlist": [],
    "max_read_bytes": 4 * 1024 * 1024,
    "max_write_bytes": 16 * 1024 * 1024,
    "max_list_entries": 3000,
    "max_grep_file_bytes": 2 * 1024 * 1024,
    "grep_extensions": [
        ".txt", ".log", ".md", ".json", ".jsonl", ".yaml", ".yml", ".toml", ".ini", ".cfg",
        ".conf", ".csv", ".tsv", ".xml", ".html", ".htm", ".css", ".js", ".ts", ".jsx", ".tsx",
        ".py", ".ps1", ".bat", ".cmd", ".sh", ".java", ".go", ".rs", ".c", ".cpp", ".h", ".hpp",
        ".sql", ".env", ".properties", ".gitignore", ".vue", ".php", ".rb", ".cs", ".kt", ".swift",
    ],
    "audit_log": f"{LOG_DIRNAME}/{AUDIT_FILENAME}",
    # 审计日志轮转：超过 audit_max_bytes 就滚动，最多保留 audit_backup_count 份历史
    # （0 = 不轮转，保持旧行为）
    "audit_max_bytes": 5 * 1024 * 1024,
    "audit_backup_count": 3,
    # 运行日志（logs/wkfile.log，服务自己的 stdout/stderr）轮转。
    # 审计日志有轮转而它没有的话，服务跑上一年这个文件会无限膨胀。
    # 0 = 不轮转
    "run_log_max_bytes": 2 * 1024 * 1024,
    "run_log_backup_count": 3,
    "tls_cert": "",
    "tls_key": "",
    # 中间证书链（可选）。证书商把「公钥证书」和「中间链」分成两个文件给的时候
    # （就是 public + chain + key 那种三件套），把链文件填这里，程序会自动把两份
    # 拼成一个 fullchain 再喂给 TLS —— 不填也能跑，前提是 tls_cert 本身就是
    # 带链的那份（fullchain / 证书链 / ca-bundle）。
    "tls_chain": "",
    # 数据库 / 缓存数据源。为空表示不开放任何数据库能力（默认就是空的）。
    # 每一项形如：
    #   {"name":"prod","type":"mysql","host":"127.0.0.1","port":3306,
    #    "user":"root","password":"","database":"mydb",
    #    "allow_write":false,"max_rows":200,"timeout_sec":30,"connect_timeout_sec":8}
    #   {"name":"cache","type":"redis","host":"127.0.0.1","port":6379,
    #    "password":"","db":0,"allow_write":false}
    # allow_write=false 时只放行查询类语句，写语句直接拒绝；
    # 想给 AI 开写权限，除了这里改 true，还要保证服务端不是只读、且用的是主 Token。
    #
    # ★ 两种改法，效果完全一样，随便挑一种：
    #   ① 图形界面「④ 数据库」页 —— 点【＋ MySQL】/【＋ Redis】加一条，填好保存
    #   ② 直接改本文件（wkfile\wkfile.config.json）—— 记事本打开，照上面格式手填
    #   （只改了 databases 这一段的话，保存后重启服务即可，不用重新配置其它项）
    "databases": [],
    # 作者署名。只用于界面展示，不参与任何鉴权或校验逻辑。
    "author": AUTHOR,    "auth_fail_limit": 20,
    "auth_fail_window_sec": 300,
    # 是否开放 MCP 端点（/mcp 与 /sse），供 Trae / Cursor / Claude 等 MCP 客户端直连
    "enable_mcp": True,
    # 工具级禁用：写在这里的工具名不会出现在 tools/list 里，直接调用也会被拒
    "tools_disable": [],
    # 落盘前对 .py/.json/.jsonl/.xml/.toml 做一次语法校验，不通过则原文件不动
    "preflight_check": True,
    # 是否允许免鉴权访问 /health、/api/health（只返回服务名与版本，不含主机名）
    "public_health": True,
}

# --------------------------------------------------------------------------------------
# 工具函数
# --------------------------------------------------------------------------------------


def program_dir() -> str:
    """程序所在目录。

    打包成 exe 后 __file__ 指向 PyInstaller 的临时解压目录，必须改用 exe 自身路径；
    源码运行时就是本文件所在目录。
    """
    if getattr(sys, "frozen", False):
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.abspath(__file__))


def _dir_writable(path: str) -> bool:
    try:
        os.makedirs(path, exist_ok=True)
        probe = os.path.join(path, ".wk-write-test")
        with open(probe, "w", encoding="utf-8") as fh:
            fh.write("ok")
        os.remove(probe)
        return True
    except OSError:
        return False


def data_dir() -> str:
    """运行时数据目录 —— 配置 / PID / 日志 / 备份 / 回收站全在这里。

    选址顺序：
      1. exe 所在目录本身就叫 wkfile  → 直接用（不套娃）
      2. exe 同级的 wkfile\\ 子目录
      3. %PROGRAMDATA%\\wkfile        （exe 装在 Program Files 等只读位置时的兜底）

    除了这里，服务不会在别处新建任何目录（备份与回收站也在这里面）。
    """
    prog = program_dir()
    if os.path.basename(prog).lower() == APP_DIRNAME and _dir_writable(prog):
        return prog
    cand = os.path.join(prog, APP_DIRNAME)
    if _dir_writable(cand):
        return cand
    alt = os.path.join(os.environ.get("PROGRAMDATA", r"C:\ProgramData"), APP_DIRNAME)
    os.makedirs(alt, exist_ok=True)
    return alt


def default_config_path() -> str:
    return os.path.join(data_dir(), CONFIG_FILENAME)


def logs_dir() -> str:
    path = os.path.join(data_dir(), LOG_DIRNAME)
    os.makedirs(path, exist_ok=True)
    return path


def backup_root(base: str | None = None) -> str:
    path = os.path.join(base or data_dir(), BACKUP_DIRNAME)
    os.makedirs(path, exist_ok=True)
    return path


def trash_root(base: str | None = None) -> str:
    path = os.path.join(base or data_dir(), TRASH_DIRNAME)
    os.makedirs(path, exist_ok=True)
    return path


def root_key(root: str) -> str:
    """把一个根目录映射成稳定的短目录名，避免多个根目录在备份区互相覆盖。"""
    h = hashlib.md5(norm(root).encode("utf-8")).hexdigest()[:8]
    label = re.sub(r"[^0-9A-Za-z\u4e00-\u9fff]+", "_", os.path.basename(os.path.abspath(root)))
    label = (label or "root").strip("_")[:24] or "root"
    return f"{label}-{h}"


def run_log_path(base: str | None = None) -> str:
    return os.path.join(base or data_dir(), LOG_DIRNAME, LOG_FILENAME)


# --------------------------------------------------------------------------------------
# 日志轮转（审计日志与运行日志共用同一套规则）
# --------------------------------------------------------------------------------------


def rotate_log_file(path: str, keep: int) -> None:
    """把 path 滚成 path.1，历史依次后移，丢掉最旧的 keep 份之外的部分。

    keep == 0 表示不留历史，直接删掉当前文件（下次写会重建）。
    Windows 上「打开着的文件不能改名」，所以调用前必须先把写句柄关掉。
    """
    keep = max(0, int(keep or 0))
    if keep == 0:
        try:
            os.remove(path)
        except OSError:
            pass
        return
    oldest = f"{path}.{keep}"
    if os.path.exists(oldest):
        try:
            os.remove(oldest)  # 先丢掉最旧的一份，再整体后移
        except OSError:
            pass
    for idx in range(keep - 1, 0, -1):
        src = f"{path}.{idx}"
        dst = f"{path}.{idx + 1}"
        if os.path.exists(src):
            try:
                os.replace(src, dst)
            except OSError:
                pass
    try:
        os.replace(path, f"{path}.1")
    except OSError:
        pass


class RotatingLogWriter:
    """可当 sys.stdout / sys.stderr 用的日志文件对象，写满就自动轮转。

    为什么不用 logging 的 RotatingFileHandler：界面进程与 --run 子进程都把
    stdout 重定向到这个文件，用最朴素的文件句柄最可控，也方便在 windowed exe
    没有控制台时直接顶替 sys.stdout。
    """

    def __init__(self, path: str, max_bytes: int = 0, keep: int = 0,
                 encoding: str = "utf-8"):
        self.path = os.path.abspath(path)
        self.max_bytes = max(0, int(max_bytes or 0))
        self.keep = max(0, int(keep or 0))
        self.encoding = encoding
        self.errors = "replace"
        self._fh = None
        self._written = 0
        self._lock = threading.Lock()
        self._open()

    # ---- 内部 ----

    def _open(self) -> None:
        try:
            os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
            self._fh = open(self.path, "a", encoding=self.encoding, buffering=1)
        except OSError:
            self._fh = None
            return
        try:
            self._written = os.path.getsize(self.path)
        except OSError:
            self._written = 0

    def _rotate(self) -> None:
        try:
            if self._fh is not None:
                self._fh.flush()
                self._fh.close()
        except (OSError, ValueError):
            pass
        self._fh = None
        rotate_log_file(self.path, self.keep)
        self._open()

    # ---- 文件协议（只实现被用到的部分）----

    def write(self, s) -> int:
        if not s:
            return 0
        if isinstance(s, bytes):  # 万一有人塞字节进来
            s = s.decode(self.encoding, "replace")
        with self._lock:
            if self._fh is None:
                self._open()
            if self._fh is None:
                return 0
            try:
                n = self._fh.write(s)
            except (OSError, ValueError):
                return 0
            self._written += len(s)
            if self.max_bytes > 0 and self._written >= self.max_bytes:
                self._rotate()
            return n

    def writelines(self, lines) -> None:
        for line in lines:
            self.write(line)

    def flush(self) -> None:
        with self._lock:
            if self._fh is not None:
                try:
                    self._fh.flush()
                except (OSError, ValueError):
                    pass

    def close(self) -> None:
        with self._lock:
            if self._fh is not None:
                try:
                    self._fh.close()
                except (OSError, ValueError):
                    pass
                self._fh = None

    def fileno(self) -> int:
        return self._fh.fileno() if self._fh is not None else -1

    def isatty(self) -> bool:
        return False

    def readable(self) -> bool:
        return False

    def writable(self) -> bool:
        return True

    def seekable(self) -> bool:
        return False

    def size(self) -> int:
        try:
            return os.path.getsize(self.path)
        except OSError:
            return 0


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def human_size(n: int) -> str:
    units = ["B", "KB", "MB", "GB", "TB"]
    v = float(n)
    for u in units:
        if v < 1024 or u == units[-1]:
            return f"{v:.1f} {u}" if u != "B" else f"{int(v)} B"
        v /= 1024
    return f"{n} B"


# ---------------------------------------------------------------------------
# 网络地址探测 —— 给「MCP 客户端配置」用
# ---------------------------------------------------------------------------

def local_ipv4() -> str:
    """本机对外 IPv4（UDP 连一下、不真的发包，只为让系统替我选一条路由）。"""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    except Exception:  # noqa: BLE001
        try:
            return socket.gethostbyname(socket.gethostname())
        except Exception:  # noqa: BLE001
            return "127.0.0.1"
    finally:
        s.close()


def is_private_ip(ip: str) -> bool:
    """是不是内网 / 回环 / 保留地址（判断「该不该换成公网 IP」用）。"""
    try:
        addr = ipaddress.ip_address(str(ip).strip())
    except ValueError:
        return False
    return bool(addr.is_private or addr.is_loopback or addr.is_link_local
                or addr.is_reserved or addr.is_multicast or addr.is_unspecified)


# 公网 IP 回显服务：都是纯文本或近纯文本，顺序按「国内可达 + 响应干净」排
_PUBIP_ENDPOINTS = (
    "https://ip.3322.net/",
    "https://api.ipify.org",
    "https://ipv4.icanhazip.com",
    "https://myip.ipip.net",
)


def detect_public_ip(timeout: float = 4.0) -> str:
    """探测本机公网 IPv4；拿不到就返回空串（绝不抛异常）。

    实现上有两个刻意的选择：
      1. 绕开系统代理 —— 走代理拿到的是代理服务器的 IP，那是错的答案；
      2. 不信任服务返回的格式，只从正文里捞第一个合法的公网 IPv4，
         这样 ipify 的裸 IP、ipip 的「当前 IP：x.x.x.x 来自于…」都能吃。
    服务器在 NAT 后面时这里拿到的仍是出口公网 IP —— 端口映射通的情况下正好是
    客户端该填的那个地址。探测失败也不影响任何功能，只是少了一个便利。
    """
    import urllib.request

    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    for url in _PUBIP_ENDPOINTS:
        try:
            with opener.open(url, timeout=timeout) as resp:
                body = resp.read(4096).decode("utf-8", "replace")
        except Exception:  # noqa: BLE001
            continue
        for token in re.findall(r"\d{1,3}(?:\.\d{1,3}){3}", body):
            try:
                addr = ipaddress.ip_address(token)
            except ValueError:
                continue
            if addr.version == 4 and not is_private_ip(token):
                return token
    return ""


def pick_best_ip(prefer_public: bool = True) -> tuple:
    """给配置界面挑一个「客户端最可能填得上」的地址，返回 (地址, 来源说明)。

    prefer_public=True 时先探公网，探不到再退回内网地址 —— 这正是用户的痛点：
    界面默认填的是局域网 IP，而客户端在另一台机器上、根本到不了 192.168.x.x。
    """
    if prefer_public:
        pub = detect_public_ip()
        if pub:
            return pub, "公网"
    return local_ipv4(), "内网"


def _selfcert_module():
    """延迟导入自签证书生成器；模块缺失时返回 None，不影响服务本身。"""
    try:
        import wkfile_selfcert
        return wkfile_selfcert
    except Exception:  # noqa: BLE001
        return None


def default_selfcert_paths() -> tuple:
    """自签证书的默认落盘位置：数据目录\\tls\\ 下，不污染 exe 所在目录。"""
    d = os.path.join(data_dir(), "tls")
    return os.path.join(d, "wkfile.crt"), os.path.join(d, "wkfile.key")


def generate_selfcert(cert_path: str, key_path: str, *, cn: str = "wkfile",
                      ips=(), dns=(), days: int = 3650, bits: int = 2048) -> tuple:
    """生成自签证书并落盘（纯标准库，不依赖 openssl，不装任何包）。

    ips 为空时自动补上 127.0.0.1 + 本机内网 IP + 探测到的公网 IP ——
    SAN 里少了某个 IP，客户端按那个 IP 连就一定校验失败，所以宁可多写几个。
    """
    mod = _selfcert_module()
    if mod is None:
        raise PathError("bad_request", "缺少自签证书模块 wkfile_selfcert.py，无法生成")

    own = [str(x).strip() for x in (ips or []) if str(x).strip()]
    if not own:
        own = ["127.0.0.1"]
        lan = local_ipv4()
        if lan and lan not in own:
            own.append(lan)
        pub = detect_public_ip()
        if pub and pub not in own:
            own.append(pub)
    return mod.write_pair(cert_path, key_path, cn=cn or "wkfile", ips=own,
                          dns=dns or (), days=days, bits=bits)


def norm(p: str) -> str:
    """规范化路径用于比较：realpath + 大小写折叠。"""
    return os.path.normcase(os.path.abspath(p))


def decode_text(raw: bytes, encoding: str = "auto") -> tuple[str, str]:
    """把字节解码成文本，返回 (文本, 实际编码)。"""
    if encoding and encoding.lower() not in ("auto", ""):
        names = [encoding]
    else:
        names = ["utf-8-sig", "gbk", "utf-8", "latin-1"]
    for enc in names:
        try:
            return raw.decode(enc), enc
        except (UnicodeDecodeError, LookupError):
            continue
    return raw.decode("utf-8", errors="replace"), "utf-8"


def looks_binary(raw: bytes) -> bool:
    if not raw:
        return False
    if b"\x00" in raw[:8192]:
        return True
    sample = raw[:8192]
    try:
        sample.decode("utf-8")
        return False
    except UnicodeDecodeError:
        pass
    try:
        sample.decode("gbk")
        return False
    except UnicodeDecodeError:
        return True


def read_text_file(path: str, limit: int) -> tuple[str, str, bool]:
    with open(path, "rb") as fh:
        raw = fh.read(limit + 1)
    truncated = len(raw) > limit
    if truncated:
        raw = raw[:limit]
    text, enc = decode_text(raw)
    return text, enc, truncated


def sha256_of(path: str) -> str:
    import hashlib

    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


# --------------------------------------------------------------------------------------
# 写前语法校验（不通过就原文件不动）
# --------------------------------------------------------------------------------------

# 只校验这些后缀；其它格式放过（二进制、模板、半截配置等）
PREFLIGHT_EXTS = (".py", ".json", ".jsonl", ".xml", ".toml")
# 超过这个体积就跳过校验，避免大文件把一次工具调用拖死
PREFLIGHT_MAX_BYTES = 4 * 1024 * 1024
# .jsonl 逐行校验的行数上限
PREFLIGHT_MAX_JSONL_LINES = 5000


def _decode_for_check(data: bytes) -> str | None:
    for enc in ("utf-8-sig", "gbk"):
        try:
            return data.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue
    return None


def preflight_check(path: str, data: bytes) -> None:
    """落盘前对已知格式做一次语法校验；不通过抛 PathError，调用方负责不写文件。

    校验的是"即将写进去的字节"，所以编码声明不匹配这类问题也能提前发现。
    """
    ext = os.path.splitext(str(path))[1].lower()
    if ext not in PREFLIGHT_EXTS or not data or len(data) > PREFLIGHT_MAX_BYTES:
        return

    if ext == ".py":
        import py_compile
        import tempfile

        tmpdir = tempfile.mkdtemp(prefix="wkfile-preflight-")
        tmp = os.path.join(tmpdir, "check.py")
        try:
            with open(tmp, "wb") as fh:
                fh.write(data)
            py_compile.compile(tmp, cfile=tmp + "c", doraise=True)
        except py_compile.PyCompileError as exc:
            raise PathError("preflight_failed", f"Python 语法校验没通过，文件未改动：{exc}")
        except OSError as exc:
            raise PathError("preflight_failed", f"Python 语法校验无法执行：{exc}")
        finally:
            for p in (tmp, tmp + "c"):
                try:
                    os.remove(p)
                except OSError:
                    pass
            try:
                os.rmdir(tmpdir)
            except OSError:
                pass
        return

    text = _decode_for_check(data)
    if text is None:
        return  # 解不开就不拦，交给真正的写入路径报编码问题
    try:
        if ext == ".json":
            json.loads(text)
        elif ext == ".jsonl":
            for idx, raw_line in enumerate(text.splitlines(), 1):
                if idx > PREFLIGHT_MAX_JSONL_LINES:
                    break
                line = raw_line.strip()
                if not line:
                    continue
                try:
                    json.loads(line)
                except json.JSONDecodeError as exc:
                    raise PathError(
                        "preflight_failed",
                        f"第 {idx} 行不是合法 JSON，文件未改动：{exc}",
                    )
        elif ext == ".xml":
            import xml.etree.ElementTree as ET

            ET.fromstring(text)
        elif ext == ".toml":
            try:
                import tomllib  # Python 3.11+ 才有
            except ImportError:
                return
            tomllib.loads(text)
    except PathError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise PathError("preflight_failed", f"写入前语法校验没通过，文件未改动：{exc}")


def disabled_tools(cfg) -> frozenset:
    """从配置里取出被禁用的工具名（容忍写成字符串或空值）。"""
    raw = getattr(cfg, "tools_disable", None) or []
    if isinstance(raw, str):
        raw = [x for x in re.split(r"[,;\s]+", raw) if x]
    out = set()
    for item in raw:
        name = str(item).strip()
        if name:
            out.add(name if name.startswith("win_") else "win_" + name)
    return frozenset(out)


def is_loopback_bind(host: str) -> bool:
    """判断监听地址是不是只绑本机（这类绑定无需 Token 也不会暴露）。"""
    h = str(host or "").strip().lower()
    if h in ("localhost",):
        return True
    if h.startswith("127."):
        return True
    if h in ("::1",):
        return True
    return False


# --------------------------------------------------------------------------------------
# TLS 证书
# --------------------------------------------------------------------------------------


def tls_enabled(cfg) -> bool:
    return bool(str(getattr(cfg, "tls_cert", "") or "").strip()
                and str(getattr(cfg, "tls_key", "") or "").strip())


def tls_chain_path(cfg) -> str:
    """配置里的中间证书链路径（没配就是空串）。"""
    return str(getattr(cfg, "tls_chain", "") or "").strip()


def _quiet_unlink(path: str) -> None:
    try:
        os.unlink(path)
    except OSError:
        pass


def merge_pem(cert_path: str, chain_path: str, out_path: str) -> str:
    """把「站点证书」和「中间证书链」按顺序拼成一个 PEM 文件，返回 out_path。

    顺序不能反：握手时服务端是照文件里的先后顺序往外发的，第一张必须是站点
    证书，后面才是中间 CA。拼反了客户端会直接报证书错。
    """
    parent = os.path.dirname(os.path.abspath(out_path))
    if parent:
        os.makedirs(parent, exist_ok=True)
    with open(os.path.abspath(cert_path), "rb") as fh:
        head = fh.read()
    with open(os.path.abspath(chain_path), "rb") as fh:
        tail = fh.read()
    for blob_name, blob in (("证书", head), ("中间链", tail)):
        if b"-----BEGIN CERTIFICATE-----" not in blob:
            raise PathError("bad_cert", f"{blob_name}文件里没有找到 PEM 证书内容"
                                       f"（须是文本格式的 .crt/.pem，二进制 DER 请先转格式）")
    # 直接首尾相接会把上一张的 `END CERTIFICATE-----` 和下一张粘成一行，
    # OpenSSL 读第二张时会失败 —— 必须保证前一份以换行结尾。
    if not head.endswith(b"\n"):
        head += b"\n"
    with open(os.path.abspath(out_path), "wb") as fh:
        fh.write(head)
        fh.write(tail)
        if not tail.endswith(b"\n"):
            fh.write(b"\n")
    return os.path.abspath(out_path)


def prepare_cert_file(cert_path: str, chain_path: str = "", *,
                      work_dir: str = "", tag: str = "fullchain") -> str:
    """返回真正该喂给 `load_cert_chain` 的那个文件路径。

    - 没配中间链 → 原样返回 cert_path（用户给的很可能本身就是 fullchain）
    - 配了中间链 → 合并成 `<work_dir>/<tag>.pem` 再返回；work_dir 不可写时
      退到系统临时目录（服务每次启动重新生成，不需要持久化）
    """
    cert = os.path.abspath(str(cert_path or ""))
    chain = str(chain_path or "").strip()
    if not chain:
        return cert
    chain = os.path.abspath(chain)
    wanted = str(work_dir or "").strip()
    if wanted:
        try:
            return merge_pem(cert, chain, os.path.join(wanted, tag + ".pem"))
        except PathError:
            raise                     # 证书内容本身有问题 —— 这个必须原样报上去
        except (OSError, ValueError):
            # 目录只读/被占用/路径非法（比如路径里有非法字符，makedirs 会抛
            # ValueError 而不是 OSError）→ 一律退到临时文件，别让服务起不来
            pass
    fd, tmp_path = tempfile.mkstemp(prefix="wkfile-fullchain-", suffix=".pem")
    os.close(fd)
    return merge_pem(cert, chain, tmp_path)


def _count_pair(cert_path: str, chain_path: str = "") -> int:
    """站点证书 + （可选的）中间链，一共能往外发几张证书。"""
    n = count_cert_chain(cert_path)
    extra = str(chain_path or "").strip()
    if extra:
        n += count_cert_chain(extra)
    return n


def validate_tls_pair(cert_path: str, key_path: str, chain_path: str = "") -> str:
    """校验证书（+可选中间链）与私钥能不能配对；不能则抛 PathError。

    成功返回一句可读摘要。第三件套（public / chain / key 分成三个文件）的
    场景就是靠 chain_path 接进来：这里会先把前两份合成 fullchain 再验。
    """
    raw_cert = str(cert_path or "").strip()
    raw_key = str(key_path or "").strip()
    raw_chain = str(chain_path or "").strip()
    if not raw_cert or not raw_key:
        raise PathError("bad_request", "证书和私钥都要选上")
    cert = os.path.abspath(raw_cert)
    key = os.path.abspath(raw_key)
    checks = [("证书", cert), ("私钥", key)]
    if raw_chain:
        checks.append(("中间证书链", os.path.abspath(raw_chain)))
    for label, p in checks:
        if not os.path.isfile(p):
            raise PathError("not_found", f"{label}文件不存在：{p}")

    load_path = cert
    tmp_path = ""
    if raw_chain:
        try:
            tmp_path = prepare_cert_file(cert, raw_chain)
        except PathError:
            raise
        except OSError as exc:
            raise PathError("bad_cert", f"证书与中间链无法合并：{exc}")
        load_path = tmp_path

    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    try:
        ctx.load_cert_chain(certfile=load_path, keyfile=key)
    except (ssl.SSLError, OSError, ValueError) as exc:
        raise PathError("bad_cert", f"证书与私钥无法配对加载：{exc}")
    finally:
        if tmp_path:
            _quiet_unlink(tmp_path)

    n = _count_pair(cert, raw_chain)
    tail = f"（链 {n} 张）" if raw_chain else ""
    return f"{os.path.basename(cert)}{tail} + {os.path.basename(key)} 可以正常配对"


try:
    from _ssl import _test_decode_cert as _decode_cert  # CPython 私有接口
except ImportError:  # noqa: BLE001 —— 没有就只是拿不到证书详情，不影响功能
    _decode_cert = None


def describe_cert(cert_path: str) -> str:
    """尽量读出证书的 CN 与到期时间，读不出来就返回空串（仅用于界面显示）。"""
    if _decode_cert is None:
        return ""
    try:
        info = _decode_cert(os.path.abspath(str(cert_path or "")))
    except Exception:  # noqa: BLE001
        return ""
    cn = ""
    for rdn in info.get("subject", ()) or ():
        for k, v in rdn:
            if k == "commonName":
                cn = str(v)
    not_after = str(info.get("notAfter") or "")
    parts = []
    if cn:
        parts.append(f"CN={cn}")
    if not_after:
        parts.append(f"到期 {not_after}")
    return " · ".join(parts)


def count_cert_chain(cert_path: str) -> int:
    """数证书文件里带了几张证书。

    PEM 按 `BEGIN CERTIFICATE` 块数；二进制 DER（或读不到）一律按 1 张算。
    """
    try:
        with open(os.path.abspath(str(cert_path or "")), "rb") as fh:
            blob = fh.read()
    except OSError:
        return 0
    n = blob.count(b"-----BEGIN CERTIFICATE-----")
    return n or 1


CHAIN_INCOMPLETE_SHORT = "证书文件里只有 1 张证书，没带中间证书"

CHAIN_INCOMPLETE_LONG = (
    "证书文件里只有 1 张证书（缺中间证书 / CA 链）。\n"
    "  服务端只会把这一张发出去。浏览器和 Windows 自己会顺着证书里的 AIA 地址\n"
    "  把中间证书补回来，所以用浏览器试是「正常」的 —— 但这正是坑：\n"
    "  Trae / Cursor 这类 Electron 客户端走 Node 的 TLS 栈，不会自动补链，\n"
    "  会直接报 unable to verify the first certificate 而连不上。\n"
    "  两种改法（任选其一，私钥文件都不用换）：\n"
    "    ① 把 tls_cert 换成证书商给的「证书链 / chain / fullchain」文件\n"
    "       （里面通常有 2~3 张证书）；\n"
    "    ② 证书商把「公钥证书」和「中间链」分成两个文件给的时候，\n"
    "       把链文件填进 tls_chain（界面上是「中间证书链（可选）」那一栏），\n"
    "       程序会自动合并 —— 这就是 public + chain + key 三件套的填法。"
)


def chain_health(cert_path: str, chain_path: str = "") -> tuple:
    """返回 (链中证书张数, 是否不完整)。

    张数 <= 1 表示没带中间证书，Electron 系客户端（Trae / Cursor）会连不上。
    传了 chain_path 就把「证书 + 中间链」合起来算 —— 三件套分开放也算完整。
    """
    n = _count_pair(cert_path, chain_path)
    return n, n <= 1


# 明显不合格的 Token：示例文件里的占位串、常见弱口令
WEAK_TOKENS = frozenset({
    "", "test", "token", "changeme", "change-me", "password", "secret",
    "testtoken", "testtoken123456", "123456", "admin",
    "在这里填一个长随机串，或运行 python wkfile_agent.py --init 自动生成",
})

# 低于这个长度算"偏弱"（只警告，不拦截）
MIN_TOKEN_LEN = 16


def token_strength(token: str) -> tuple[str, str]:
    """返回 (等级, 说明)；等级取值 ok / weak / bad。"""
    t = str(token or "").strip()
    if not t:
        return "bad", "未设置 Token"
    if t in WEAK_TOKENS:
        return "bad", "用的是示例里的占位值或常见弱口令"
    if len(t) < MIN_TOKEN_LEN:
        return "weak", f"长度只有 {len(t)} 位（建议至少 {MIN_TOKEN_LEN} 位）"
    return "ok", ""


def make_expiry(days) -> str:
    """把「有效天数」换算成绝对到期时间（ISO）；<=0 返回空串 = 永久有效。"""
    try:
        days = float(days or 0)
    except (TypeError, ValueError):
        days = 0
    if days <= 0:
        return ""
    return (datetime.now().astimezone() + timedelta(days=days)).isoformat(timespec="seconds")


def parse_expiry(raw) -> tuple[str, object]:
    """解析 token_expires_at：返回 (状态, 到期时间)。

    状态：never（空 = 永久）/ ok / bad（写坏了，按永久处理但会警告）
    """
    s = str(raw or "").strip()
    if not s:
        return "never", None
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return "bad", None
    if dt.tzinfo is None:
        dt = dt.astimezone()          # 不写时区就按本机时区理解
    return "ok", dt


def expiry_state(expires_at) -> tuple[str, object, float]:
    """按到期时间串算状态，返回 (状态, 到期时间, 剩余秒数)。

    状态：never（永久）/ valid / expiring（不足 3 天）/ expired / bad
    """
    kind, dt = parse_expiry(expires_at)
    if kind == "never":
        return "never", None, float("inf")
    if kind == "bad":
        return "bad", None, 0.0
    remain = (dt - datetime.now(dt.tzinfo)).total_seconds()
    if remain <= 0:
        return "expired", dt, remain
    if remain < 3 * 86400:
        return "expiring", dt, remain
    return "valid", dt, remain


def token_state(cfg) -> tuple[str, object, float]:
    return expiry_state(getattr(cfg, "token_expires_at", ""))


def human_remain(seconds: float) -> str:
    if seconds == float("inf"):
        return "永久"
    if seconds <= 0:
        return "已过期"
    if seconds >= 86400:
        # 刚设完 30 天时剩余是 29.9999 天，四舍五入显示成"30 天"更符合直觉
        return f"{int(round(seconds / 86400))} 天"
    if seconds >= 3600:
        return f"{int(seconds // 3600)} 小时"
    return f"{max(1, int(seconds // 60))} 分钟"


def expiry_text_of(expires_at) -> str:
    """给人看的一句话：某个到期时间的状态。"""
    state, dt, remain = expiry_state(expires_at)
    if state == "never":
        return "永久有效"
    if state == "bad":
        return "有效期字段格式不对（已按永久有效处理）"
    if state == "expired":
        return f"已于 {dt:%Y-%m-%d %H:%M} 过期"
    return f"{dt:%Y-%m-%d %H:%M} 到期（剩 {human_remain(remain)}）"


def expiry_text(cfg) -> str:
    return expiry_text_of(getattr(cfg, "token_expires_at", ""))


# --------------------------------------------------------------------------------------
# 配置
# --------------------------------------------------------------------------------------


class Config:
    def __init__(self, data: dict, path: str):
        merged = dict(DEFAULT_CONFIG)
        merged.update({k: v for k, v in (data or {}).items() if v is not None})
        self.__dict__.update(merged)
        self.config_path = os.path.abspath(path)
        # 日志 / 备份 / 回收站都放在配置文件旁边：默认就是 wkfile\ 数据目录，
        # 用 --config 指定别处时也不会串到其它实例的目录里
        self.base_dir = os.path.dirname(self.config_path) or data_dir()
        self.roots = [os.path.abspath(r) for r in (self.roots or [])]

    def save(self, template: bool = False):
        data = {k: getattr(self, k) for k in DEFAULT_CONFIG}
        body = json.dumps(data, ensure_ascii=False, indent=2)
        if template:
            body = _annotate_config(body)
        with open(self.config_path, "w", encoding="utf-8") as fh:
            fh.write(body)


# 自动生成的配置文件里插进哪些注释。JSON 本身不支持注释，但我们自己写、
# 自己读（读的时候 python 的 json 只认标准语法，所以注释行必须能安全剔除）。
# 做法：注释一律用 "// " 开头，load 时先剥离 —— 这样记事本打开就是一目了然的
# 说明书，程序也不会被自己的注释绊倒。
_CONFIG_NOTES = {
    '"databases"': [
        "// ★ 数据库 / 缓存就在这里加。想开就照着下面这段抄，抄完删掉 // 即可：",
        '//   "databases": [',
        '//     {"name":"prod","type":"mysql","host":"127.0.0.1","port":3306,',
        '//      "user":"root","password":"","database":"mydb","allow_write":false,',
        '//      "max_rows":200,"timeout_sec":30,"connect_timeout_sec":8},',
        '//     {"name":"cache","type":"redis","host":"127.0.0.1","port":6379,',
        '//      "password":"","username":"","db":0,"allow_write":false,',
        '//      "max_keys":200,"timeout_sec":15,"connect_timeout_sec":8},',
        '//     {"name":"erp","type":"sqlserver","host":"127.0.0.1","port":1433,',
        '//      "user":"sa","password":"","database":"erp","allow_write":false,',
        '//      "max_rows":200,"timeout_sec":30,"connect_timeout_sec":8}',
        "//   ],",
        "// 也可以完全不手改 —— 打开图形界面的「④ 数据库」页点两下就行。",
        "// allow_write=false：只放行 SELECT/SHOW 这类查询，写语句一律拒绝。",
        "// 想给 AI 开写权限：① 这里改 true ② 服务端不能是只读 ③ 客户端用主 Token。",
        "// type=sqlserver：仅 SQL 身份验证（sa 等账号），需 SQL Server 2008+，",
        "//                连接全程 TLS 加密（服务器默认自签名证书即可）。",
        "// connect_timeout_sec = 拨号等待上限（连不上最多等多久，默认 8 秒）",
        "// timeout_sec         = 查询/读等待上限（等结果最多等多久）",
        "// 这两项填 0、负数或乱码都会自动退回默认值，不会把服务弄崩。",
    ],
    '"roots"': [
        "// ★ 允许 AI 操作的目录白名单，越窄越安全。例： \"roots\": [\"D:\\\\share\"]",
    ],
    '"token"': [
        "// ★ 访问 Token。留空时程序会自动生成一个；也可以自己填任意长随机串。",
    ],
    '"author"': [
        "// 作者署名，只用于界面展示。",
    ],
}


def _annotate_config(body: str) -> str:
    """给机器生成的 json 插上中文注释（只在我们自己生成模板时用）。"""
    out = []
    for line in body.splitlines():
        out.append(line)
        key = None
        stripped = line.strip()
        for candidate in _CONFIG_NOTES:
            if stripped.startswith(candidate + ":"):
                key = candidate
                break
        if key:
            out.extend(_CONFIG_NOTES[key])
    return "\n".join(out) + "\n"


def _strip_json_comments(text: str) -> str:
    """把 _annotate_config 插进去的 "// " 整行注释剥掉。

    只处理**整行**注释（我们只生成这种），不动字符串里的 //，
    所以不需要一个完整的 JS 词法分析器。行尾注释我们也生成不了。
    """
    lines = []
    for line in text.splitlines():
        if line.lstrip().startswith("//"):
            continue
        lines.append(line)
    return "\n".join(lines)


def load_config(path: str) -> Config:
    """读配置。

    文件不存在时会**自动落一份带完整注释的模板**（而不是只存在内存里）——
    这样"数据库配置没地方写"的困惑就不存在了：打开那个 json 就有现成的
    databases 段和注释照着改。写盘失败（目录只读等）不影响启动，
    此时退化成纯内存默认值，行为与旧版一致。

    读的时候会先剥掉自己写的 "// " 整行注释，所以模板里那些说明文字
    不会把 json 解析弄坏。用户手改过的配置里如果混进了 // 注释，也一样容忍。

    但**真正的语法错误照样报错**（与旧行为一致）：手改坏了配置必须当场知道，
    静默退化成默认值等于把 roots/token 全部清空，那比崩一次危险得多。
    """
    if os.path.isfile(path):
        with open(path, "r", encoding="utf-8-sig") as fh:
            text = fh.read()
        if "//" in text:
            text = _strip_json_comments(text)
        raw = json.loads(text)          # 解析失败会抛 ValueError，向上传播
        return Config(raw, path)

    cfg = Config({}, path)
    try:
        cfg.save(template=True)
    except OSError:
        pass
    return cfg


# --------------------------------------------------------------------------------------
# 路径安全
# --------------------------------------------------------------------------------------


class PathError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


class Guard:
    def __init__(self, cfg: Config):
        self.cfg = cfg

    def resolve(self, raw: str, *, allow_empty: bool = False) -> str:
        if raw is None or str(raw).strip() == "":
            if allow_empty:
                return ""
            raise PathError("bad_path", "路径不能为空")
        p = str(raw)
        # 拒绝 NTFS 备用数据流与设备路径
        head, tail = os.path.splitdrive(p)
        if ":" in tail:
            raise PathError("bad_path", "路径中不允许出现冒号（备用数据流/设备路径）")
        if p.startswith("\\\\?\\") or p.startswith("\\\\.\\"):
            raise PathError("bad_path", "不允许设备路径")
        full = os.path.realpath(os.path.abspath(p))
        self._assert_inside(full)
        return full

    def _assert_inside(self, full: str) -> None:
        if not self.cfg.roots:
            raise PathError("no_roots", "服务端未配置任何可操作目录（roots）")
        self._deny_data_dir(full)
        target = norm(full)
        for root in self.cfg.roots:
            r = norm(root)
            if target == r or target.startswith(r.rstrip("\\") + os.sep):
                return
        raise PathError(
            "out_of_scope",
            "路径不在允许范围内。允许的根目录：" + "; ".join(self.cfg.roots),
        )

    def root_of(self, full: str) -> str:
        target = norm(full)
        best = None
        for root in self.cfg.roots:
            r = norm(root)
            if target == r or target.startswith(r.rstrip("\\") + os.sep):
                if best is None or len(r) > len(norm(best)):
                    best = root
        return best or self.cfg.roots[0]

    def rel_to_root(self, full: str) -> str:
        root = self.root_of(full)
        try:
            return os.path.relpath(full, root)
        except ValueError:
            return os.path.basename(full)

    def _deny_data_dir(self, full: str) -> None:
        """wkfile 自己的配置 / 日志 / 备份 / 回收站永远不可通过接口访问。

        否则 AI 只要读一次 wkfile.config.json 就拿到 Token，鉴权形同虚设。
        """
        base = getattr(self.cfg, "base_dir", None) or data_dir()
        protected = [self.cfg.config_path] + [
            os.path.join(base, x)
            for x in (PID_FILENAME, LOG_DIRNAME, BACKUP_DIRNAME, TRASH_DIRNAME, APP_DIRNAME)
        ]
        # 默认布局下 base 就是 exe 旁边那个 wkfile\ 数据目录，整棵子树都挡掉；
        # 用 --config 把配置放到别处时不整体挡，免得误伤业务目录
        try:
            if norm(base) == norm(data_dir()):
                protected.append(base)
        except OSError:  # noqa: BLE001
            pass
        target = norm(full)
        for item in protected:
            p = norm(item)
            if target == p or target.startswith(p.rstrip("\\") + os.sep):
                raise PathError(
                    "denied",
                    "这是 wkfile 的数据目录（含配置、日志、备份与回收站），不允许通过接口读写",
                )

    def blocked_name(self, name: str) -> bool:
        lowered = name.lower()
        if lowered == APP_DIRNAME:
            return True
        # 兼容早期版本遗留在业务目录里的隐藏目录
        return lowered.startswith(".winfile-")


# --------------------------------------------------------------------------------------
# 审计日志
# --------------------------------------------------------------------------------------


class Audit:
    def __init__(self, cfg: Config):
        self.path = cfg.audit_log
        if self.path:
            if not os.path.isabs(self.path):
                # 相对路径落在配置文件旁边（默认就是 wkfile\logs\audit.jsonl）
                base = getattr(cfg, "base_dir", None) or data_dir()
                self.path = os.path.join(base, self.path)
            self.path = os.path.normpath(self.path)
        self.max_bytes = int(getattr(cfg, "audit_max_bytes", 0) or 0)
        self.backup_count = int(getattr(cfg, "audit_backup_count", 0) or 0)
        self.lock = threading.Lock()
        self._since_check = 0
        # 父目录不存在时必须自己建出来：否则每次写都会 FileNotFoundError，
        # 而下面 write() 把 OSError 一律吞掉 —— 结果就是审计日志悄悄什么都不记。
        self._ensure_dir()

    def _ensure_dir(self) -> None:
        if not self.path:
            return
        try:
            os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        except OSError:
            pass

    def write(self, record: dict):
        if not self.path:
            return
        record = dict(record)
        record.setdefault("ts", now_iso())
        line = json.dumps(record, ensure_ascii=False)
        with self.lock:
            try:
                self._rotate_if_needed()
                with open(self.path, "a", encoding="utf-8") as fh:
                    fh.write(line + "\n")
            except FileNotFoundError:
                # 目录可能是后来被删掉的，重建一次再试
                self._ensure_dir()
                try:
                    with open(self.path, "a", encoding="utf-8") as fh:
                        fh.write(line + "\n")
                except OSError:
                    pass
            except OSError:
                pass

    def size(self) -> int:
        try:
            return os.path.getsize(self.path)
        except OSError:
            return 0

    def _rotate_if_needed(self) -> None:
        """超过 audit_max_bytes 就把当前文件滚成 .1，依次后移并丢掉最旧的。

        每 64 次写才真正查一次体积，避免每次写都 stat 一次磁盘。
        """
        if self.max_bytes <= 0:
            return
        self._since_check += 1
        if self._since_check < 64 and self._since_check != 1:
            return
        self._since_check = 0
        try:
            if os.path.getsize(self.path) < self.max_bytes:
                return
        except OSError:
            return
        rotate_log_file(self.path, self.backup_count)


# --------------------------------------------------------------------------------------
# 业务实现
# --------------------------------------------------------------------------------------


class Agent:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.guard = Guard(cfg)
        self.audit = Audit(cfg)
        self.fail_lock = threading.Lock()
        self.failures: dict[str, list[float]] = {}
        # 每个请求线程各自的权限模式（见 set_request_mode）
        self._req_local = threading.local()
        # 被配置禁用的工具/操作名（MCP 与裸 HTTP 接口共用同一份名单）
        self.disabled_tools = disabled_tools(cfg)
        self.disabled_actions: set[str] = set()
        if mcpmod is not None:
            for tname in self.disabled_tools:
                act = mcpmod.ROUTE_MAP.get(tname)
                if act:
                    self.disabled_actions.add(act)
        else:
            # 没有 MCP 模块时只认得名字本身，退化成直接比对 action
            self.disabled_actions = set(self.disabled_tools)

    # ---- 鉴权 ----

    def token_kind(self, supplied: str) -> str:
        """认一下这个 Token 是什么级别：full（完整权限）/ readonly（只读）/ none。

        两个 Token 都做恒定时间比较；即使只读 Token 先命中也不提前返回，
        避免用响应时间区分"这个 Token 存不存在"。
        """
        given = str(supplied or "")
        hit_full = bool(self.cfg.token) and hmac.compare_digest(given, self.cfg.token)
        ro = str(getattr(self.cfg, "readonly_token", "") or "")
        hit_ro = bool(ro) and hmac.compare_digest(given, ro)
        if hit_full:
            return "full"
        if hit_ro:
            return "readonly"
        return "none"

    def token_ok(self, supplied: str) -> bool:
        return self.token_kind(supplied) != "none"

    # ---- 单次请求的权限模式 ----
    # ThreadingHTTPServer 每个请求一个线程，所以用 thread-local 存"这次请求是不是只读的"，
    # 多个并发请求互不干扰。

    def set_request_mode(self, readonly: bool) -> None:
        if not hasattr(self, "_req_local"):
            self._req_local = threading.local()
        self._req_local.readonly = bool(readonly)

    def request_readonly(self) -> bool:
        return bool(getattr(getattr(self, "_req_local", None), "readonly", False))

    def effective_readonly(self) -> bool:
        return bool(self.cfg.read_only) or self.request_readonly()

    def token_expired(self) -> bool:
        """Token 内容是对的，但已经过了有效期。"""
        return token_state(self.cfg)[0] == "expired"

    def token_expiry_text(self) -> str:
        return expiry_text(self.cfg)

    def ip_ok(self, ip: str) -> bool:
        allow = self.cfg.ip_allowlist or []
        if not allow:
            return True
        for item in allow:
            item = item.strip()
            if not item:
                continue
            if item == ip:
                return True
            if item.endswith("*") and ip.startswith(item[:-1]):
                return True
            if "/" in item:
                try:
                    import ipaddress

                    if ipaddress.ip_address(ip) in ipaddress.ip_network(item, strict=False):
                        return True
                except ValueError:
                    pass
        return False

    def note_failure(self, ip: str) -> None:
        now = time.time()
        window = float(self.cfg.auth_fail_window_sec)
        with self.fail_lock:
            lst = [t for t in self.failures.get(ip, []) if now - t < window]
            lst.append(now)
            self.failures[ip] = lst

    def is_locked(self, ip: str) -> bool:
        now = time.time()
        window = float(self.cfg.auth_fail_window_sec)
        with self.fail_lock:
            lst = [t for t in self.failures.get(ip, []) if now - t < window]
            self.failures[ip] = lst
            return len(lst) >= int(self.cfg.auth_fail_limit)

    def clear_failures(self, ip: str) -> None:
        with self.fail_lock:
            self.failures.pop(ip, None)

    # ---- 通用 ----

    def _require_write(self):
        if self.cfg.read_only:
            raise PathError("read_only", "服务端处于只读模式（read_only=true），不允许写入")
        if self.request_readonly():
            raise PathError(
                "read_only",
                "本次请求用的是只读 Token，不允许写入。"
                "如需修改文件，请改用完整权限的 Token（界面②里的主 Token）。",
            )

    def _backup(self, full: str) -> str | None:
        if not self.cfg.auto_backup or not os.path.exists(full):
            return None
        root = self.guard.root_of(full)
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        rel = self.guard.rel_to_root(full)
        # 备份统一落在 wkfile\backups\<时间戳>\<根目录标识>\<相对路径>，
        # 不往被操作的业务目录里塞东西
        dest = os.path.join(backup_root(self.cfg.base_dir), stamp, root_key(root), rel)
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        try:
            shutil.copy2(full, dest)
        except OSError:
            return None
        return dest

    def _preflight(self, full: str, data: bytes) -> None:
        """写前语法预校验；不通过则抛错，调用方不得落盘。"""
        if not bool(getattr(self.cfg, "preflight_check", True)):
            return
        preflight_check(full, data)

    def _tool_enabled(self, name: str) -> bool:
        return name not in disabled_tools(self.cfg)

    @staticmethod
    def _entry(path: str, base: str | None = None) -> dict:
        st = os.stat(path)
        is_dir = os.path.isdir(path)
        item = {
            "name": os.path.basename(path) or path,
            "path": path,
            "type": "dir" if is_dir else "file",
            "size": 0 if is_dir else st.st_size,
            "size_human": "-" if is_dir else human_size(st.st_size),
            "mtime": datetime.fromtimestamp(st.st_mtime).isoformat(timespec="seconds"),
            "readonly": not os.access(path, os.W_OK),
        }
        if base:
            item["rel"] = os.path.relpath(path, base)
        if not is_dir:
            item["ext"] = os.path.splitext(path)[1].lower()
        return item

    # ---- 各接口 ----

    def op_roots(self, req: dict) -> dict:
        ro = self.effective_readonly()
        return {
            "roots": [
                {"path": r, "exists": os.path.isdir(r)} for r in self.cfg.roots
            ],
            # read_only 反映"本次请求"的实际权限：用只读 Token 连进来时也是 True
            "read_only": ro,
            "server_read_only": bool(self.cfg.read_only),
            "token_scope": "readonly" if self.request_readonly() else "full",
            "allow_delete": self.cfg.allow_delete,
            "allow_move": self.cfg.allow_move,
            "auto_backup": self.cfg.auto_backup,
        }

    def op_disk(self, req: dict) -> dict:
        drives = []
        if os.name == "nt":
            import ctypes

            bitmask = ctypes.windll.kernel32.GetLogicalDrives()
            for i in range(26):
                if bitmask & (1 << i):
                    letter = chr(ord("A") + i) + ":\\"
                    drives.append(letter)
        else:
            drives = ["/"]
        out = []
        for d in drives:
            try:
                usage = shutil.disk_usage(d)
                try:
                    label = os.path.basename(d.rstrip("\\/")) or d
                except Exception:
                    label = d
                out.append(
                    {
                        "path": d,
                        "total": usage.total,
                        "free": usage.free,
                        "used": usage.used,
                        "total_human": human_size(usage.total),
                        "free_human": human_size(usage.free),
                        "allowed": any(
                            norm(d) == norm(r) or norm(d).startswith(norm(r).rstrip("\\") + os.sep)
                            or norm(r).startswith(norm(d).rstrip("\\") + os.sep)
                            for r in self.cfg.roots
                        ),
                    }
                )
            except OSError:
                continue
        return {"drives": out}

    def op_list(self, req: dict) -> dict:
        full = self.guard.resolve(req.get("path"), allow_empty=True)
        if not full:
            # 没给路径 → 列根目录
            entries = []
            for r in self.cfg.roots:
                if os.path.isdir(r):
                    entries.append(self._entry(r))
                else:
                    entries.append(
                        {"name": r, "path": r, "type": "missing", "exists": False}
                    )
            return {"path": "", "entries": entries, "is_root_listing": True}
        if not os.path.exists(full):
            raise PathError("not_found", f"路径不存在：{full}")
        if os.path.isfile(full):
            return {"path": full, "entries": [self._entry(full)], "is_file": True}

        include_hidden = bool(req.get("include_hidden", False))
        pattern = req.get("pattern") or None
        recursive = bool(req.get("recursive", False))
        max_depth = int(req.get("max_depth", 1) if recursive else 1)
        limit = min(int(req.get("max_entries", 500) or 500), int(self.cfg.max_list_entries))

        entries: list[dict] = []
        truncated = False

        def walk(base: str, depth: int):
            nonlocal truncated
            try:
                names = sorted(
                    os.scandir(base), key=lambda e: (not e.is_dir(), e.name.lower())
                )
            except OSError as exc:
                entries.append({"name": os.path.basename(base), "path": base, "type": "error",
                                "error": str(exc)})
                return
            for e in names:
                if len(entries) >= limit:
                    truncated = True
                    return
                if not include_hidden and self.guard.blocked_name(e.name):
                    continue
                if not include_hidden and e.name.startswith("."):
                    continue
                if pattern and not fnmatch.fnmatch(e.name.lower(), pattern.lower()):
                    if not e.is_dir():
                        continue
                try:
                    entries.append(self._entry(e.path, base))
                except OSError as exc:
                    entries.append({"name": e.name, "path": e.path, "type": "error",
                                    "error": str(exc)})
                if recursive and e.is_dir() and depth < max_depth:
                    walk(e.path, depth + 1)

        walk(full, 1)
        dirs = sum(1 for e in entries if e.get("type") == "dir")
        files = sum(1 for e in entries if e.get("type") == "file")
        return {
            "path": full,
            "entries": entries,
            "count": len(entries),
            "dirs": dirs,
            "files": files,
            "truncated": truncated,
        }

    def op_stat(self, req: dict) -> dict:
        full = self.guard.resolve(req.get("path"))
        if not os.path.exists(full):
            return {"path": full, "exists": False}
        info = self._entry(full)
        info["exists"] = True
        info["is_dir"] = os.path.isdir(full)
        if not info["is_dir"]:
            info["sha256"] = req.get("hash") and sha256_of(full) or None
            if info["sha256"] is None:
                info.pop("sha256")
        return info

    def op_read(self, req: dict) -> dict:
        full = self.guard.resolve(req.get("path"))
        if not os.path.isfile(full):
            raise PathError("not_found", f"文件不存在：{full}")
        size = os.path.getsize(full)
        limit = min(int(req.get("max_bytes", self.cfg.max_read_bytes)), int(self.cfg.max_read_bytes))
        with open(full, "rb") as fh:
            probe = fh.read(8192)
        if looks_binary(probe):
            raise PathError(
                "binary_file",
                f"看起来是二进制文件（{human_size(size)}），请用 read_b64，或用 download 下载",
            )
        text, enc, truncated = read_text_file(full, limit)
        lines = text.splitlines()
        total_lines = len(lines)
        start = max(int(req.get("start_line", 1) or 1), 1)
        max_lines = int(req.get("max_lines", 0) or 0)
        selected = lines[start - 1:]
        if max_lines > 0:
            selected = selected[:max_lines]
        numbered = bool(req.get("line_numbers", True))
        body = (
            "\n".join(f"{start + i:>6}\t{ln}" for i, ln in enumerate(selected))
            if numbered
            else "\n".join(selected)
        )
        return {
            "path": full,
            "encoding": enc,
            "size": size,
            "size_human": human_size(size),
            "total_lines": total_lines,
            "returned_lines": len(selected),
            "start_line": start,
            "truncated": truncated or (start - 1 + len(selected)) < total_lines,
            "content": body,
        }

    def op_read_b64(self, req: dict) -> dict:
        full = self.guard.resolve(req.get("path"))
        if not os.path.isfile(full):
            raise PathError("not_found", f"文件不存在：{full}")
        limit = min(int(req.get("max_bytes", 1024 * 1024)), 8 * 1024 * 1024)
        with open(full, "rb") as fh:
            raw = fh.read(limit + 1)
        truncated = len(raw) > limit
        raw = raw[:limit]
        return {
            "path": full,
            "size": os.path.getsize(full),
            "truncated": truncated,
            "base64": base64.b64encode(raw).decode("ascii"),
        }

    def op_write(self, req: dict) -> dict:
        self._require_write()
        full = self.guard.resolve(req.get("path"))
        content = req.get("content")
        if content is None:
            content = ""
        if not isinstance(content, str):
            raise PathError("bad_request", "content 必须是字符串")
        data = content.encode(req.get("encoding") or "utf-8")
        if len(data) > int(self.cfg.max_write_bytes):
            raise PathError(
                "too_large",
                f"内容 {human_size(len(data))} 超过上限 {human_size(int(self.cfg.max_write_bytes))}",
            )
        expected = req.get("expected_sha256")
        if expected and os.path.isfile(full):
            actual = sha256_of(full)
            if actual.lower() != str(expected).lower():
                raise PathError(
                    "conflict",
                    "文件已被其他人修改（sha256 不匹配），请先重新读取再写入",
                )
        if req.get("create_dirs", True):
            os.makedirs(os.path.dirname(full) or ".", exist_ok=True)
        elif not os.path.isdir(os.path.dirname(full)):
            raise PathError("no_parent", f"父目录不存在：{os.path.dirname(full)}")

        # 语法校验通过才继续 —— 不通过则连备份都不做，磁盘上不留任何痕迹
        self._preflight(full, data)
        backup = self._backup(full)
        existed = os.path.isfile(full)
        tmp = full + ".wkfile-tmp"
        with open(tmp, "wb") as fh:
            fh.write(data)
        os.replace(tmp, full)
        st = os.stat(full)
        return {
            "path": full,
            "bytes": len(data),
            "created": not existed,
            "backup": backup,
            "sha256": sha256_of(full),
            "mtime": datetime.fromtimestamp(st.st_mtime).isoformat(timespec="seconds"),
        }

    def op_write_b64(self, req: dict) -> dict:
        self._require_write()
        full = self.guard.resolve(req.get("path"))
        try:
            data = base64.b64decode(req.get("base64") or "", validate=True)
        except Exception:
            raise PathError("bad_request", "base64 内容无法解码")
        if len(data) > int(self.cfg.max_write_bytes):
            raise PathError("too_large", "内容超过上限")
        if req.get("create_dirs", True):
            os.makedirs(os.path.dirname(full) or ".", exist_ok=True)
        self._preflight(full, data)
        backup = self._backup(full)
        with open(full, "wb") as fh:
            fh.write(data)
        return {"path": full, "bytes": len(data), "backup": backup}

    def op_append(self, req: dict) -> dict:
        self._require_write()
        full = self.guard.resolve(req.get("path"))
        data = (req.get("content") or "").encode(req.get("encoding") or "utf-8")
        if req.get("create_dirs", True):
            os.makedirs(os.path.dirname(full) or ".", exist_ok=True)
        backup = self._backup(full)
        with open(full, "ab") as fh:
            fh.write(data)
        return {"path": full, "appended_bytes": len(data), "size": os.path.getsize(full),
                "backup": backup}

    def op_replace(self, req: dict) -> dict:
        self._require_write()
        full = self.guard.resolve(req.get("path"))
        if not os.path.isfile(full):
            raise PathError("not_found", f"文件不存在：{full}")
        old = req.get("old")
        new = req.get("new", "")
        if old is None or old == "":
            raise PathError("bad_request", "old 不能为空")
        text, enc, _ = read_text_file(full, int(self.cfg.max_read_bytes))
        use_regex = bool(req.get("regex", False))
        limit = int(req.get("count", 0) or 0)
        if use_regex:
            flags = re.MULTILINE
            if req.get("ignore_case", False):
                flags |= re.IGNORECASE
            try:
                pattern = re.compile(old, flags)
            except re.error as exc:
                raise PathError("bad_regex", f"正则表达式无效：{exc}")
            occurrences = len(pattern.findall(text))
            # 匹配不唯一且调用方没表态 → 拒绝，避免"一次调用静默改错多处"
            if limit == 0 and occurrences > 1:
                raise PathError(
                    "ambiguous_match",
                    f"该模式在文件里匹配到 {occurrences} 处，而调用未指定 count。"
                    f"请把 old 写得更精确（带上足够上下文），或显式指定 count 来限定替换范围。",
                )
            replaced_text, n = pattern.subn(new, text, count=limit if limit > 0 else 0)
        else:
            occurrences = text.count(old)
            if limit == 0 and occurrences > 1:
                raise PathError(
                    "ambiguous_match",
                    f"该片段在文件里共出现 {occurrences} 次，而调用未指定 count。"
                    f"请给 old 补上更长的唯一上下文，或显式指定 count 来限定替换范围。",
                )
            if limit > 0:
                replaced_text = text.replace(old, new, limit)
                n = min(limit, occurrences)
            else:
                replaced_text = text.replace(old, new)
                n = occurrences
        if n == 0:
            return {"path": full, "replaced": 0, "occurrences": occurrences,
                    "message": "没有找到匹配内容，文件未改动"}

        if req.get("dry_run"):
            # 只看不改：把将要发生的改动以 unified diff 返回，磁盘上什么都不动
            diff = "".join(difflib.unified_diff(
                text.splitlines(keepends=True),
                replaced_text.splitlines(keepends=True),
                fromfile="a/" + os.path.basename(full),
                tofile="b/" + os.path.basename(full),
            ))
            cap = 200000
            return {
                "path": full,
                "dry_run": True,
                "replaced": n,
                "occurrences": occurrences,
                "encoding": enc,
                "diff": diff[:cap],
                "diff_truncated": len(diff) > cap,
                "message": "这是预览，文件未被修改。确认无误后去掉 dry_run 再调一次即可落盘。",
            }

        out_bytes = replaced_text.encode(enc if enc != "utf-8-sig" else "utf-8")
        self._preflight(full, out_bytes)
        backup = self._backup(full)
        with open(full, "wb") as fh:
            fh.write(out_bytes)
        return {
            "path": full,
            "replaced": n,
            "occurrences": occurrences,
            "encoding": enc,
            "backup": backup,
            "size": os.path.getsize(full),
        }

    def op_mkdir(self, req: dict) -> dict:
        self._require_write()
        full = self.guard.resolve(req.get("path"))
        if os.path.isdir(full):
            return {"path": full, "created": False, "message": "目录已存在"}
        if req.get("parents", True):
            os.makedirs(full, exist_ok=True)
        else:
            os.mkdir(full)
        return {"path": full, "created": True}

    def op_move(self, req: dict) -> dict:
        if not self.cfg.allow_move:
            raise PathError("denied", "服务端禁用了移动/重命名（allow_move=false）")
        self._require_write()
        src = self.guard.resolve(req.get("src"))
        dst = self.guard.resolve(req.get("dst"))
        if not os.path.exists(src):
            raise PathError("not_found", f"源路径不存在：{src}")
        if os.path.exists(dst) and not req.get("overwrite", False):
            raise PathError("exists", f"目标已存在：{dst}（如需覆盖请传 overwrite=true）")
        os.makedirs(os.path.dirname(dst) or ".", exist_ok=True)
        shutil.move(src, dst)
        return {"src": src, "dst": dst, "moved": True}

    def op_copy(self, req: dict) -> dict:
        self._require_write()
        src = self.guard.resolve(req.get("src"))
        dst = self.guard.resolve(req.get("dst"))
        if not os.path.exists(src):
            raise PathError("not_found", f"源路径不存在：{src}")
        if os.path.exists(dst) and not req.get("overwrite", False):
            raise PathError("exists", f"目标已存在：{dst}（如需覆盖请传 overwrite=true）")
        os.makedirs(os.path.dirname(dst) or ".", exist_ok=True)
        if os.path.isdir(src):
            shutil.copytree(src, dst, dirs_exist_ok=bool(req.get("overwrite", False)))
        else:
            shutil.copy2(src, dst)
        return {"src": src, "dst": dst, "copied": True}

    def op_trash(self, req: dict) -> dict:
        if not self.cfg.allow_delete:
            raise PathError("denied", "服务端禁用了删除（allow_delete=false）")
        self._require_write()
        full = self.guard.resolve(req.get("path"))
        if not os.path.exists(full):
            raise PathError("not_found", f"路径不存在：{full}")
        root = self.guard.root_of(full)
        if norm(full) == norm(root):
            raise PathError("denied", "不允许删除根目录本身")
        if os.path.isdir(full) and not req.get("recursive", False) and os.listdir(full):
            raise PathError("not_empty", "目录非空；确认要连内容一起删除请传 recursive=true")
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        trash_id = f"{stamp}-{secrets.token_hex(3)}"
        rel = self.guard.rel_to_root(full)
        entry = os.path.join(trash_root(self.cfg.base_dir), trash_id)
        dest = os.path.join(entry, "files", rel)
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        shutil.move(full, dest)
        manifest = {
            "trash_id": trash_id,
            "deleted_at": now_iso(),
            "original_path": full,
            "stored_path": dest,
            "type": "dir" if os.path.isdir(dest) else "file",
        }
        with open(os.path.join(entry, "manifest.json"), "w",
                  encoding="utf-8") as fh:
            json.dump(manifest, fh, ensure_ascii=False, indent=2)
        return manifest

    def op_trash_list(self, req: dict) -> dict:
        items = []
        base = trash_root(self.cfg.base_dir)
        if os.path.isdir(base):
            for tid in sorted(os.listdir(base), reverse=True):
                mf = os.path.join(base, tid, "manifest.json")
                if os.path.isfile(mf):
                    try:
                        with open(mf, "r", encoding="utf-8") as fh:
                            items.append(json.load(fh))
                    except (OSError, json.JSONDecodeError):
                        continue
        return {"count": len(items), "items": items}

    def op_trash_restore(self, req: dict) -> dict:
        self._require_write()
        trash_id = str(req.get("trash_id") or "")
        if not re.fullmatch(r"[0-9]{8}-[0-9]{6}-[0-9a-f]{6}", trash_id):
            raise PathError("bad_request", "trash_id 格式不正确")
        entry = os.path.join(trash_root(self.cfg.base_dir), trash_id)
        mf = os.path.join(entry, "manifest.json")
        if not os.path.isfile(mf):
            raise PathError("not_found", f"找不到回收站条目：{trash_id}")
        with open(mf, "r", encoding="utf-8") as fh:
            manifest = json.load(fh)
        target = manifest["original_path"]
        self.guard.resolve(target)
        if os.path.exists(target):
            raise PathError("exists", f"原位置已存在同名文件：{target}")
        os.makedirs(os.path.dirname(target) or ".", exist_ok=True)
        shutil.move(manifest["stored_path"], target)
        shutil.rmtree(entry, ignore_errors=True)
        return {"restored": True, "path": target, "trash_id": trash_id}

    # ---- 备份（覆盖前自动存的历史版本，AI 也能查看与回滚）----
    # 备份落在 wkfile\backups\<时间戳>\<根目录标识>\<相对路径>。
    # 这些文件在数据目录里，被 Guard 一律挡住，所以这里单独走一套路径校验。

    def _backup_base(self) -> str:
        return norm(backup_root(self.cfg.base_dir))

    def _inside_backup(self, full: str) -> bool:
        base = self._backup_base()
        t = norm(full)
        return t == base or t.startswith(base.rstrip("\\") + os.sep)

    def _resolve_backup_file(self, raw: str) -> str:
        full = os.path.abspath(str(raw or "").strip())
        if not self._inside_backup(full):
            raise PathError(
                "out_of_scope",
                f"只能操作备份目录里的文件：{backup_root(self.cfg.base_dir)}",
            )
        if not os.path.isfile(full):
            raise PathError("not_found", f"备份文件不存在：{full}")
        return full

    def _root_by_key(self, key: str) -> str:
        """把备份目录名里的根标识还原回真实根目录。"""
        for r in self.cfg.roots:
            if root_key(r) == key:
                return os.path.abspath(r)
        raise PathError(
            "not_found",
            f"这份备份来自一个已经不在白名单里的目录（标识 {key}）。"
            f"请先把该目录加回 roots 再还原。",
        )

    def _iter_backups(self):
        """遍历所有备份条目，产出 (stamp, root_key, rel, 备份文件绝对路径, 大小)。"""
        base = backup_root(self.cfg.base_dir)
        if not os.path.isdir(base):
            return
        for stamp in sorted(os.listdir(base), reverse=True):
            stamp_dir = os.path.join(base, stamp)
            if not os.path.isdir(stamp_dir):
                continue
            for key in sorted(os.listdir(stamp_dir)):
                key_dir = os.path.join(stamp_dir, key)
                if not os.path.isdir(key_dir):
                    continue
                for cur, _dirs, files in os.walk(key_dir):
                    for name in files:
                        full = os.path.join(cur, name)
                        rel = os.path.relpath(full, key_dir)
                        try:
                            size = os.path.getsize(full)
                        except OSError:
                            size = -1
                        yield stamp, key, rel, full, size

    def op_backup_list(self, req: dict) -> dict:
        want = str(req.get("path") or "").strip()
        want_norm = norm(want) if want else ""
        limit = min(int(req.get("max_entries", 200) or 200), 2000)
        items = []
        truncated = False
        for stamp, key, rel, full, size in self._iter_backups():
            try:
                original = os.path.join(self._root_by_key(key), rel)
            except PathError:
                original = ""
            if want_norm and norm(original or rel) != want_norm:
                continue
            if len(items) >= limit:
                truncated = True
                break
            try:
                mtime = datetime.fromtimestamp(os.path.getmtime(full)).isoformat(
                    timespec="seconds")
            except OSError:
                mtime = ""
            items.append({
                "stamp": stamp,
                "backup_path": full,
                "original_path": original,
                "rel": rel,
                "size": size,
                "size_human": human_size(size) if size >= 0 else "-",
                "mtime": mtime,
            })
        return {
            "backup_root": backup_root(self.cfg.base_dir),
            "count": len(items),
            "items": items,
            "truncated": truncated,
            "filter": want,
        }

    def _find_backup_for(self, original: str) -> str:
        """按原文件路径找最新的一份备份。"""
        full = self.guard.resolve(original)
        root = self.guard.root_of(full)
        rel = self.guard.rel_to_root(full)
        base = backup_root(self.cfg.base_dir)
        key = root_key(root)
        stamps = []
        try:
            stamps = sorted(os.listdir(base), reverse=True)
        except OSError:
            stamps = []
        for stamp in stamps:
            cand = os.path.join(base, stamp, key, rel)
            if os.path.isfile(cand):
                return cand
        raise PathError(
            "not_found",
            f"没有找到 {full} 的备份。只有被本服务覆盖/改写过的文件才会有备份"
            f"（前提是 auto_backup=true）。",
        )

    def op_backup_restore(self, req: dict) -> dict:
        self._require_write()
        backup_path = str(req.get("backup_path") or "").strip()
        original = str(req.get("original_path") or "").strip()
        if not backup_path and not original:
            raise PathError("bad_request", "backup_path 和 original_path 至少给一个")

        if backup_path:
            src = self._resolve_backup_file(backup_path)
            # 从备份路径反推原文件位置：<base>\<stamp>\<key>\<rel>
            base = backup_root(self.cfg.base_dir)
            rel_parts = os.path.relpath(src, base).split(os.sep)
            if len(rel_parts) < 3:
                raise PathError("bad_request", f"备份路径结构不对：{src}")
            key, rel = rel_parts[1], os.path.join(*rel_parts[2:])
            root = self._root_by_key(key)
            target = self.guard.resolve(os.path.join(root, rel))
        else:
            target = self.guard.resolve(original)
            src = self._find_backup_for(target)

        if req.get("dry_run"):
            try:
                src_size = os.path.getsize(src)
            except OSError:
                src_size = -1
            return {
                "dry_run": True,
                "would_restore": True,
                "backup_path": src,
                "target": target,
                "target_exists": os.path.isfile(target),
                "backup_size": src_size,
            }

        # 还原本身也要可撤销：先把当前位置的文件再备份一份
        pre = self._backup(target)
        os.makedirs(os.path.dirname(target) or ".", exist_ok=True)
        shutil.copy2(src, target)
        return {
            "restored": True,
            "target": target,
            "from_backup": src,
            "previous_version_backup": pre,
            "size": os.path.getsize(target),
        }

    # ---- 排障与批量读取 ----

    def op_tail(self, req: dict) -> dict:
        """只看文件末尾若干行 —— 查日志的主力工具，不用先把整个文件读进来。"""
        full = self.guard.resolve(req.get("path"))
        if not os.path.isfile(full):
            raise PathError("not_found", f"文件不存在：{full}")
        n = max(1, min(int(req.get("lines", 100) or 100), 5000))
        max_bytes = min(int(req.get("max_bytes", self.cfg.max_read_bytes)),
                        int(self.cfg.max_read_bytes))
        with open(full, "rb") as fh:
            fh.seek(0, os.SEEK_END)
            size = fh.tell()
            buf = b""
            pos = size
            block = 65536
            # 从尾部往前一块块回读，攒够 n 个换行或读满上限就停
            while pos > 0:
                step = min(block, pos)
                pos -= step
                fh.seek(pos)
                buf = fh.read(step) + buf
                if buf.count(b"\n") > n or len(buf) >= max_bytes:
                    break
        clipped = len(buf) > max_bytes
        if clipped:
            buf = buf[-max_bytes:]
        text, enc = decode_text(buf)
        picked = text.splitlines()[-n:]
        whole_file = (pos == 0 and not clipped)
        return {
            "path": full,
            "size": size,
            "size_human": human_size(size),
            "encoding": enc,
            "requested_lines": n,
            "returned_lines": len(picked),
            # 整文件都在手里时才能给出准确的起始行号
            "start_line": (len(text.splitlines()) - len(picked) + 1) if whole_file else None,
            "truncated": clipped,
            "content": "\n".join(picked),
        }

    def op_tree(self, req: dict) -> dict:
        """一次拿到目录树概览，省掉逐层 win_list 的来回。"""
        base = self.guard.resolve(req.get("path") or req.get("root"), allow_empty=True)
        if not base:
            base = self.cfg.roots[0]
        if not os.path.isdir(base):
            raise PathError("bad_request", f"不是一个目录：{base}")
        depth = max(0, min(int(req.get("depth", 3) or 3), 8))
        max_nodes = min(int(req.get("max_nodes", 500) or 500), 5000)
        include_hidden = bool(req.get("include_hidden", False))
        show_size = bool(req.get("show_size", True))
        entries: list = []
        state = {"truncated": False, "dirs": 0, "files": 0}

        def walk(cur: str, level: int) -> None:
            if state["truncated"]:
                return
            try:
                names = sorted(os.listdir(cur))
            except OSError:
                return
            subdirs, subfiles = [], []
            for name in names:
                if not include_hidden and name.startswith("."):
                    continue
                if self.guard.blocked_name(name):
                    continue
                p = os.path.join(cur, name)
                if os.path.isdir(p):
                    subdirs.append((name, p))
                else:
                    subfiles.append((name, p))
            for name, p in subdirs:
                if len(entries) >= max_nodes:
                    state["truncated"] = True
                    return
                state["dirs"] += 1
                entries.append({"depth": level, "type": "dir", "name": name, "path": p,
                                "rel": os.path.relpath(p, base)})
                if level + 1 <= depth:
                    walk(p, level + 1)
            for name, p in subfiles:
                if len(entries) >= max_nodes:
                    state["truncated"] = True
                    return
                state["files"] += 1
                item = {"depth": level, "type": "file", "name": name, "path": p,
                        "rel": os.path.relpath(p, base)}
                if show_size:
                    try:
                        item["size"] = os.path.getsize(p)
                        item["size_human"] = human_size(item["size"])
                    except OSError:
                        pass
                entries.append(item)

        walk(base, 0)
        return {
            "root": base,
            "depth": depth,
            "count": len(entries),
            "dirs": state["dirs"],
            "files": state["files"],
            "truncated": state["truncated"],
            "entries": entries,
        }

    def op_multi_read(self, req: dict) -> dict:
        """一次读多个文件。单个失败不影响其它的，逐个报状态。"""
        paths = req.get("paths") or []
        if isinstance(paths, str):
            paths = [p for p in re.split(r"[;\n]", paths) if p.strip()]
        if not isinstance(paths, list) or not paths:
            raise PathError("bad_request", "paths 必须是非空数组")
        max_files = max(1, min(int(req.get("max_files", 20) or 20), 50))
        per_file = min(int(req.get("max_bytes", self.cfg.max_read_bytes)),
                       int(self.cfg.max_read_bytes))
        total_cap = max(1024, min(int(req.get("max_total_bytes", 8 * 1024 * 1024)),
                                  32 * 1024 * 1024))
        results = []
        used = 0
        for raw in list(paths)[:max_files]:
            entry = {"path": str(raw)}
            try:
                full = self.guard.resolve(raw)
                if not os.path.isfile(full):
                    raise PathError("not_found", f"文件不存在：{full}")
                remain = total_cap - used
                if remain <= 0:
                    raise PathError("too_large", "已达到本次批量读取的总量上限，后面的文件没读")
                text, enc, truncated = read_text_file(full, min(per_file, remain))
                used += len(text.encode(enc if enc != "utf-8-sig" else "utf-8"))
                entry.update({
                    "ok": True,
                    "resolved_path": full,
                    "encoding": enc,
                    "size": os.path.getsize(full),
                    "truncated": truncated,
                    "content": text,
                })
            except PathError as exc:
                entry.update({"ok": False, "error": exc.code, "message": exc.message})
            except OSError as exc:
                entry.update({"ok": False, "error": "io_error", "message": str(exc)})
            results.append(entry)
        return {
            "count": len(results),
            "ok_count": sum(1 for r in results if r.get("ok")),
            "total_bytes": used,
            "skipped": max(0, len(paths) - max_files),
            "results": results,
        }

    def op_search(self, req: dict) -> dict:
        root = self.guard.resolve(req.get("root") or req.get("path"), allow_empty=True)
        if not root:
            root = self.cfg.roots[0]
        pattern = str(req.get("pattern") or "*")
        kind = str(req.get("kind") or "any")
        limit = min(int(req.get("max_results", 200) or 200), 2000)
        include_hidden = bool(req.get("include_hidden", False))
        results = []
        truncated = False

        if not os.path.isdir(root):
            raise PathError("not_found", f"目录不存在：{root}")

        pat = pattern.lower()
        has_glob = any(c in pattern for c in "*?[")
        regex = None
        if not has_glob and req.get("regex", False):
            try:
                regex = re.compile(pattern, re.IGNORECASE)
            except re.error as exc:
                raise PathError("bad_regex", f"正则无效：{exc}")

        for cur, dirs, files in os.walk(root):
            dirs[:] = [d for d in dirs if include_hidden or not self.guard.blocked_name(d)]
            if len(results) >= limit:
                truncated = True
                break
            for name in list(dirs) + files:
                if len(results) >= limit:
                    truncated = True
                    break
                full = os.path.join(cur, name)
                is_dir = os.path.isdir(full)
                if kind == "file" and is_dir:
                    continue
                if kind == "dir" and not is_dir:
                    continue
                if has_glob:
                    hit = fnmatch.fnmatch(name.lower(), pat)
                elif regex:
                    hit = bool(regex.search(name))
                else:
                    hit = pat in name.lower()
                if hit:
                    try:
                        results.append(self._entry(full, root))
                    except OSError:
                        continue
        return {"root": root, "pattern": pattern, "count": len(results),
                "truncated": truncated, "results": results}

    def op_grep(self, req: dict) -> dict:
        root = self.guard.resolve(req.get("root") or req.get("path"), allow_empty=True)
        if not root:
            root = self.cfg.roots[0]
        pattern = req.get("regex")
        if not pattern:
            raise PathError("bad_request", "regex 不能为空")
        flags = re.MULTILINE
        if req.get("ignore_case", True):
            flags |= re.IGNORECASE
        try:
            rx = re.compile(pattern, flags)
        except re.error as exc:
            raise PathError("bad_regex", f"正则无效：{exc}")

        glob_pat = req.get("glob") or "*"
        limit = min(int(req.get("max_results", 100) or 100), 1000)
        max_file = min(int(req.get("max_file_bytes", self.cfg.max_grep_file_bytes)),
                       int(self.cfg.max_grep_file_bytes))
        context = int(req.get("context_lines", 0) or 0)
        allowed_ext = set(e.lower() for e in (req.get("extensions") or self.cfg.grep_extensions))
        include_hidden = bool(req.get("include_hidden", False))
        only_ext = bool(req.get("only_known_extensions", True))
        files_scanned = 0
        matches = []
        truncated = False

        for cur, dirs, files in os.walk(root):
            dirs[:] = [d for d in dirs if include_hidden or not self.guard.blocked_name(d)]
            for name in files:
                if len(matches) >= limit:
                    truncated = True
                    break
                if not fnmatch.fnmatch(name.lower(), glob_pat.lower()):
                    continue
                if only_ext and allowed_ext:
                    if os.path.splitext(name)[1].lower() not in allowed_ext:
                        continue
                full = os.path.join(cur, name)
                try:
                    if os.path.getsize(full) > max_file:
                        continue
                except OSError:
                    continue
                try:
                    with open(full, "rb") as fh:
                        raw = fh.read(max_file)
                except OSError:
                    continue
                if looks_binary(raw[:8192]):
                    continue
                files_scanned += 1
                text, _ = decode_text(raw)
                lines = text.splitlines()
                for i, line in enumerate(lines):
                    if len(matches) >= limit:
                        truncated = True
                        break
                    m = rx.search(line)
                    if not m:
                        continue
                    item = {
                        "path": full,
                        "rel": os.path.relpath(full, root),
                        "line": i + 1,
                        "text": line[:400],
                        "column": m.start() + 1,
                    }
                    if context > 0:
                        lo = max(0, i - context)
                        hi = min(len(lines), i + context + 1)
                        item["context"] = [
                            {"line": lo + j + 1, "text": lines[lo + j][:400]}
                            for j in range(hi - lo)
                        ]
                    matches.append(item)
            if truncated:
                break
        return {
            "root": root,
            "regex": pattern,
            "globs": glob_pat,
            "files_scanned": files_scanned,
            "count": len(matches),
            "truncated": truncated,
            "matches": matches,
        }

    # ---- 分发 ----

    # ------------------------------------------------------------------
    # 数据库 / 缓存
    #
    # 这一组的定位：文件维护之外的"数据维护"，让 AI 能在同一套 MCP 连接里
    # 既看代码/配置，又看数据库里的真实数据。
    #
    # 权限是三重叠加的，任何一重为假都不能写：
    #   ① 数据源自己的 allow_write（界面④里的开关）
    #   ② 服务端全局 read_only
    #   ③ 本次请求用的 Token 是不是只读 Token
    # 另外 SQL / Redis 命令还要先过各自模块的安全分类器。
    # ------------------------------------------------------------------

    def _int_arg(self, req: dict, key: str, default=None):
        value = req.get(key)
        if value is None or value == "":
            return default
        try:
            return int(value)
        except (TypeError, ValueError):
            raise PathError("bad_request", f"{key} 必须是整数，收到 {value!r}")

    def _db_call(self, fn, *args, **kwargs):
        """把数据库模块的 DBError / RedisError 统一翻译成 PathError。

        两个模块的异常都是 (code, message) 结构，前端只需要认 code 前缀 db_。
        """
        try:
            return fn(*args, **kwargs)
        except Exception as exc:  # noqa: BLE001
            code = getattr(exc, "code", None)
            if code:
                raise PathError(f"db_{code}", str(exc))
            raise

    def db_source(self, req: dict, kind: str = None):
        """按名字取数据源；只配了一个时可以省略 source。

        kind 取值："mysql" / "sqlserver" / "redis" 精确匹配；
        "sql" 表示任意 SQL 数据库（MySQL 或 SQL Server）。
        """
        if dbmod is None and redismod is None and sqlsrvmod is None:
            raise PathError(
                "db_unavailable",
                "数据库模块没加载起来（server 目录下缺少 wkfile_mysql.py、"
                "wkfile_redis.py 或 wkfile_sqlsrv.py）。若是源码运行，"
                "请确认这些文件与服务同级。",
            )
        items = self.cfg.databases or []
        if not items:
            raise PathError(
                "db_not_configured",
                "还没有配置任何数据源。请在界面④「数据库」页添加一条，"
                "或往 wkfile.config.json 的 databases 数组里填写。",
            )
        want = str(req.get("source") or "").strip()
        chosen = None
        if not want:
            if len(items) == 1:
                chosen = items[0]
            else:
                raise PathError("db_need_source",
                                f"配置了多个数据源，请用 source 指定其中一个："
                                f"{', '.join(str(i.get('name')) for i in items)}")
        else:
            for item in items:
                if str(item.get("name")) == want:
                    chosen = item
                    break
            if chosen is None:
                raise PathError("db_not_found",
                                f"没有名为「{want}」的数据源。可用的有："
                                f"{', '.join(str(i.get('name')) for i in items)}")
        src = self._make_source(chosen)
        # 各模块都可能缺失，不能直接引用 XXXSource 类做 isinstance
        actual = "redis" if (redismod is not None
                             and isinstance(src, redismod.RedisSource)) else (
            "sqlserver" if (sqlsrvmod is not None
                            and isinstance(src, sqlsrvmod.SQLServerSource))
            else "mysql")
        if kind:
            matched = (kind == actual) or (kind == "sql"
                                           and actual in ("mysql", "sqlserver"))
            if not matched:
                raise PathError(
                    "db_wrong_type",
                    f"数据源「{chosen.get('name')}」的类型是 {actual}，"
                    f"而当前工具只能用于 {kind}。请换一个数据源，或改用对应的工具。",
                )
        return src

    def _make_source(self, profile: dict):
        kind = str(profile.get("type") or "mysql").lower()
        if kind in ("mysql", "mariadb"):
            if dbmod is None:
                raise PathError("db_unavailable", "MySQL 模块 wkfile_mysql.py 未加载")
            return dbmod.MySQLSource(profile)
        if kind in ("sqlserver", "sqlsrv", "mssql", "sqsrv"):
            if sqlsrvmod is None:
                raise PathError("db_unavailable",
                                "SQL Server 模块 wkfile_sqlsrv.py 未加载")
            return sqlsrvmod.SQLServerSource(profile)
        if kind == "redis":
            if redismod is None:
                raise PathError("db_unavailable", "Redis 模块 wkfile_redis.py 未加载")
            return redismod.RedisSource(profile)
        raise PathError("db_bad_type",
                        f"不支持的数据源类型：{kind}"
                        f"（目前支持 mysql / mariadb / sqlserver / redis）")

    def op_db_list(self, req: dict) -> dict:
        ro = self.effective_readonly()
        out = []
        for item in (self.cfg.databases or []):
            kind = str(item.get("type") or "mysql").lower()
            if kind == "redis":
                dsn = redismod.dsn_summary(item) if redismod else ""
            elif kind in ("sqlserver", "sqlsrv", "mssql", "sqsrv"):
                dsn = sqlsrvmod.dsn_summary(item) if sqlsrvmod else ""
            else:
                dsn = dbmod.dsn_summary(item) if dbmod else ""
            out.append({
                "name": item.get("name"),
                "type": kind,
                "dsn": dsn,
                "database": item.get("database") or item.get("db"),
                # writable 是"这次请求真的能不能写"，已经把只读 Token / 全局只读算进去了
                "writable": bool(item.get("allow_write")) and not ro,
                "allow_write_config": bool(item.get("allow_write")),
            })
        return {
            "sources": out,
            "count": len(out),
            "read_only_request": ro,
            "hint": ("写操作需要同时满足：数据源开了「允许写入」、服务端不是只读、"
                     "且当前用的是完整权限 Token。"),
        }

    def op_db_query(self, req: dict) -> dict:
        sql = str(req.get("sql") or "").strip()
        if not sql:
            raise PathError("bad_request", "sql 不能为空")
        # 先解析数据源，才能用对路的 SQL 分类器（T-SQL 与 MySQL 方言不同）
        src = self.db_source(req, kind="sql")
        cls_mod = (sqlsrvmod if (sqlsrvmod is not None
                                 and isinstance(src, sqlsrvmod.SQLServerSource))
                   else dbmod)
        # 再在通道这一层挡住写语句，好给出准确的指引。
        # 如果放任它走到 Source 里，只会得到"只读 Token 拒绝写入"这种
        # 误导性提示 —— 明明不是 Token 的问题，是走错了工具。
        level, keyword, reason = cls_mod.classify_sql(sql) if cls_mod else ("read", "", "")
        if level == "deny":
            raise PathError("db_sql_rejected", reason)
        if level == "write":
            raise PathError(
                "db_wrong_channel",
                f"{reason}。win_db_query 只能执行只读查询，"
                f"写语句请改用 win_db_exec（需要数据源已开启允许写入）。",
            )
        # 只读查询：即使用完整 Token，也照样开会话只读
        return self._db_call(src.run, sql, max_rows=self._int_arg(req, "max_rows"),
                             force_readonly=True)

    def op_db_exec(self, req: dict) -> dict:
        self._require_write()
        sql = str(req.get("sql") or "").strip()
        if not sql:
            raise PathError("bad_request", "sql 不能为空")
        src = self.db_source(req, kind="sql")
        return self._db_call(src.run, sql, max_rows=self._int_arg(req, "max_rows"))

    def op_db_tables(self, req: dict) -> dict:
        src = self.db_source(req, kind="sql")
        return self._db_call(src.tables, req.get("database") or None)

    def op_db_schema(self, req: dict) -> dict:
        table = str(req.get("table") or "").strip()
        if not table:
            raise PathError("bad_request", "必须指定 table")
        src = self.db_source(req, kind="sql")
        return self._db_call(src.schema, table, req.get("database") or None)

    def op_db_test(self, req: dict) -> dict:
        src = self.db_source(req)
        return self._db_call(src.test)

    def op_redis_info(self, req: dict) -> dict:
        src = self.db_source(req, kind="redis")
        return self._db_call(src.info)

    def op_redis_keys(self, req: dict) -> dict:
        src = self.db_source(req, kind="redis")
        return self._db_call(
            src.keys,
            req.get("pattern") or "*",
            limit=self._int_arg(req, "limit"),
            cursor=self._int_arg(req, "cursor", 0) or 0,
        )

    def op_redis_get(self, req: dict) -> dict:
        key = str(req.get("key") or "").strip()
        if not key:
            raise PathError("bad_request", "必须指定 key")
        src = self.db_source(req, kind="redis")
        return self._db_call(src.read_key, key, limit=self._int_arg(req, "limit") or 50)

    def op_redis_write(self, req: dict) -> dict:
        self._require_write()
        src = self.db_source(req, kind="redis")
        args = req.get("args")
        if not args:
            command = str(req.get("command") or "").strip()
            if not command:
                raise PathError("bad_request",
                                "必须提供 args（命令参数数组）或 command（命令字符串）")
            # 字符串形式按空白切分；参数里含空格请改用 args 数组
            args = command.split()
        if not isinstance(args, (list, tuple)) or not args:
            raise PathError("bad_request", "args 必须是非空数组，如 [\"SET\",\"k\",\"v\"]")
        return self._db_call(src.run, list(args))

    ROUTES = {
        "roots": op_roots,
        "disk": op_disk,
        "list": op_list,
        "stat": op_stat,
        "read": op_read,
        "read_b64": op_read_b64,
        "write": op_write,
        "write_b64": op_write_b64,
        "append": op_append,
        "replace": op_replace,
        "mkdir": op_mkdir,
        "move": op_move,
        "copy": op_copy,
        "trash": op_trash,
        "trash_list": op_trash_list,
        "trash_restore": op_trash_restore,
        "search": op_search,
        "grep": op_grep,
        "tail": op_tail,
        "tree": op_tree,
        "multi_read": op_multi_read,
        "backup_list": op_backup_list,
        "backup_restore": op_backup_restore,
        # 数据库 / 缓存
        "db_list": op_db_list,
        "db_query": op_db_query,
        "db_exec": op_db_exec,
        "db_tables": op_db_tables,
        "db_schema": op_db_schema,
        "db_test": op_db_test,
        "redis_info": op_redis_info,
        "redis_keys": op_redis_keys,
        "redis_get": op_redis_get,
        "redis_write": op_redis_write,
    }

    def dispatch(self, action: str, req: dict) -> dict:
        fn = self.ROUTES.get(action)
        if fn is None:
            raise PathError("unknown_action", f"未知操作：{action}")
        if action in self.disabled_actions:
            raise PathError(
                "tool_disabled",
                f"操作 {action} 已被服务端配置禁用（wkfile.config.json 的 tools_disable）",
            )
        return fn(self, req)


# --------------------------------------------------------------------------------------
# HTTP 层
# --------------------------------------------------------------------------------------


# --------------------------------------------------------------------------------------
# MCP 会话表（进程内内存即可，重启后客户端重新 initialize）
# --------------------------------------------------------------------------------------

MCP_SESSIONS: dict = {}
MCP_SESSIONS_LOCK = threading.Lock()
MCP_SESSION_TTL = 24 * 3600

# 旧版 HTTP+SSE 传输：session_id -> queue.Queue（服务端把响应推进去，SSE 流取出下发）
SSE_SESSIONS: dict = {}
SSE_SESSIONS_LOCK = threading.Lock()
SSE_IDLE_TIMEOUT = 3600


class Handler(BaseHTTPRequestHandler):
    server_version = f"{SERVICE}/{VERSION}"
    protocol_version = "HTTP/1.1"
    agent: Agent = None  # 由 main 注入

    # 静默默认访问日志（审计日志已经记录）
    def log_message(self, fmt, *args):
        pass

    def _send(self, code: int, payload: dict, raw: bytes = None, ctype: str = "application/json",
              extra: dict = None):
        body = raw if raw is not None else json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", f"{ctype}; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Service", SERVICE)
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _ok(self, data):
        self._send(200, {"ok": True, "data": data})

    def _err(self, code: int, err_code: str, message: str):
        self._send(code, {"ok": False, "error": {"code": err_code, "message": message}})

    def _client_ip(self) -> str:
        return (self.client_address or ("?",))[0]

    def _audit_record(self, action: str, req: dict, ip: str, started: float,
                      exc: Exception = None) -> dict:
        """组装一条审计记录。用只读 Token 的请求会带上 scope=readonly 标记。"""
        req = req or {}
        rec = {
            "ip": ip,
            "op": action,
            "ok": exc is None,
            "path": str(req.get("path") or req.get("src") or req.get("root") or ""),
            "ms": int((time.time() - started) * 1000),
        }
        if exc is not None:
            rec["error"] = getattr(exc, "code", None) or exc.__class__.__name__
        if self.agent.request_readonly():
            rec["scope"] = "readonly"
        return rec

    def _mcp_dispatch(self):
        """把 Agent.dispatch 包一层，让 MCP 工具调用也进审计日志。

        没有这一层的话，客户端走的 /mcp 路径是不落审计的 —— 而它恰恰是主要使用方式，
        等于审计日志把最该记的东西全漏了。
        """
        agent = self.agent
        ip = self._client_ip()

        def dispatch(action: str, req: dict) -> dict:
            started = time.time()
            try:
                data = agent.dispatch(action, req)
            except Exception as exc:  # noqa: BLE001 —— 记完账再把异常原样交给 MCP 层
                agent.audit.write(self._audit_record(action, req, ip, started, exc))
                raise
            agent.audit.write(self._audit_record(action, req, ip, started))
            return data

        return dispatch

    def _read_body(self) -> dict:
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        if length <= 0:
            return {}
        if length > 64 * 1024 * 1024:
            raise PathError("too_large", "请求体过大")
        raw = self.rfile.read(length)
        if not raw:
            return {}
        try:
            return json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise PathError("bad_json", f"请求体不是合法 JSON：{exc}")

    def _auth(self) -> bool:
        agent = self.agent
        ip = self._client_ip()
        if not agent.ip_ok(ip):
            self._err(403, "ip_denied", f"来源 IP 不在白名单内：{ip}")
            return False
        if agent.is_locked(ip):
            self._err(429, "locked", "尝试次数过多，请稍后再试")
            return False
        header = self.headers.get("Authorization") or ""
        token = header[7:].strip() if header.lower().startswith("bearer ") else ""
        kind = agent.token_kind(token)
        if kind == "none":
            agent.note_failure(ip)
            agent.audit.write({"ip": ip, "op": "auth", "ok": False, "path": "", "ms": 0})
            self._err(401, "unauthorized", "Token 无效或缺失")
            return False
        if agent.token_expired():
            agent.note_failure(ip)
            agent.audit.write({"ip": ip, "op": "auth", "ok": False, "path": "",
                               "error": "token_expired", "ms": 0})
            self._err(401, "token_expired",
                      f"Token 已过有效期（{agent.token_expiry_text()}）。"
                      "请在服务器上打开 wkfile，点「生成」换一个新 Token，再同步更新客户端配置。")
            return False
        agent.clear_failures(ip)
        # 用只读 Token 认证的请求，整条链路都被当成只读（见 Agent._require_write）
        agent.set_request_mode(kind == "readonly")
        return True

    # ---------------- MCP（客户端直连） ----------------

    def _mcp_enabled(self) -> bool:
        return mcpmod is not None and bool(getattr(self.agent.cfg, "enable_mcp", True))

    def _require_mcp(self) -> bool:
        if self._mcp_enabled():
            return True
        self._err(404, "mcp_disabled", "本服务未启用 MCP 端点（配置里的 enable_mcp = false）")
        return False

    def _mcp_info(self) -> dict:
        cfg = self.agent.cfg
        return {
            "server_name": socket.gethostname(),
            "version": VERSION,
            "roots": list(cfg.roots or []),
            "read_only": bool(cfg.read_only),
            "allow_delete": bool(cfg.allow_delete),
            "auto_backup": bool(cfg.auto_backup),
            "tools_disabled": sorted(self.agent.disabled_tools),
        }

    def _mcp_new_session(self) -> str:
        sid = secrets.token_urlsafe(24)
        now = time.time()
        with MCP_SESSIONS_LOCK:
            for k in [k for k, t in MCP_SESSIONS.items() if now - t > MCP_SESSION_TTL]:
                MCP_SESSIONS.pop(k, None)
            MCP_SESSIONS[sid] = now
        return sid

    def _mcp_touch(self, sid: str) -> bool:
        """校验会话；没带会话头时放行（宽容模式，便于用 curl / 调试器直接试）"""
        if not sid:
            return True
        with MCP_SESSIONS_LOCK:
            if sid in MCP_SESSIONS:
                MCP_SESSIONS[sid] = time.time()
                return True
        return False

    def _read_raw_body(self) -> bytes:
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        if length <= 0:
            return b""
        if length > 32 * 1024 * 1024:
            raise PathError("too_large", "请求体过大")
        return self.rfile.read(length)

    def _mcp_accepted(self) -> None:
        """202：收到通知，无需响应体"""
        self.send_response(202)
        self.send_header("Content-Length", "0")
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Service", SERVICE)
        self.end_headers()

    def _mcp_send_payload(self, obj, extra: dict = None) -> None:
        """按客户端 Accept 决定用 JSON 还是 SSE 单条事件返回"""
        accept = (self.headers.get("Accept") or "").lower()
        payload = json.dumps(obj, ensure_ascii=False)
        if "text/event-stream" in accept and "application/json" not in accept:
            body = f"event: message\ndata: {payload}\n\n".encode("utf-8")
            self._send(200, None, raw=body, ctype="text/event-stream", extra=extra)
        else:
            self._send(200, None, raw=payload.encode("utf-8"), extra=extra)

    def _handle_mcp(self) -> None:
        """Streamable HTTP 传输：POST /mcp（单条或批量 JSON-RPC）"""
        if not self._auth():
            return
        sid = (self.headers.get("Mcp-Session-Id") or "").strip()
        if not self._mcp_touch(sid):
            self._send(
                404,
                {"jsonrpc": "2.0", "id": None,
                 "error": {"code": -32001, "message": "会话已失效，请重新 initialize"}},
            )
            return

        try:
            raw = self._read_raw_body()
        except PathError as exc:
            self._send(413, {"jsonrpc": "2.0", "id": None,
                             "error": {"code": -32700, "message": exc.message}})
            return
        if not raw:
            self._send(400, {"jsonrpc": "2.0", "id": None,
                             "error": {"code": -32700, "message": "请求体为空"}})
            return
        try:
            msg = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            self._send(400, {"jsonrpc": "2.0", "id": None,
                             "error": {"code": -32700, "message": f"JSON 解析失败：{exc}"}})
            return

        info = self._mcp_info()
        is_init = isinstance(msg, dict) and msg.get("method") == "initialize"
        disabled = self.agent.disabled_tools

        dispatch = self._mcp_dispatch()
        if isinstance(msg, list):
            responses = mcpmod.handle_batch(
                dispatch, msg, info=info, disabled=disabled
            )
            if not responses:
                self._mcp_accepted()
                return
            self._mcp_send_payload(responses)
            return

        resp = mcpmod.handle_message(
            dispatch, msg, info=info, disabled=disabled
        )
        if resp is None:
            self._mcp_accepted()
            return
        extra = {"Mcp-Session-Id": self._mcp_new_session()} if is_init else None
        self._mcp_send_payload(resp, extra=extra)

    def _handle_sse_stream(self) -> None:
        """旧版 HTTP+SSE 传输：GET /sse 建立事件流，随后 POST /messages?sessionId=xxx"""
        if not self._auth():
            return
        sid = secrets.token_urlsafe(16)
        q = queue.Queue()
        with SSE_SESSIONS_LOCK:
            SSE_SESSIONS[sid] = q

        try:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Transfer-Encoding", "chunked")
            self.send_header("X-Service", SERVICE)
            self.end_headers()
            if not self._sse_chunk(f"event: endpoint\ndata: /messages?sessionId={sid}\n\n"):
                return
            deadline = time.time() + SSE_IDLE_TIMEOUT
            while time.time() < deadline:
                try:
                    item = q.get(timeout=15)
                except queue.Empty:
                    if not self._sse_chunk(": keep-alive\n\n"):
                        break
                    continue
                if item is None:
                    break
                payload = json.dumps(item, ensure_ascii=False)
                if not self._sse_chunk(f"event: message\ndata: {payload}\n\n"):
                    break
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        finally:
            with SSE_SESSIONS_LOCK:
                SSE_SESSIONS.pop(sid, None)
            try:
                self.wfile.write(b"0\r\n\r\n")
                self.wfile.flush()
            except Exception:  # noqa: BLE001
                pass
            self.close_connection = True

    def _sse_chunk(self, text: str) -> bool:
        data = text.encode("utf-8")
        try:
            self.wfile.write(f"{len(data):X}\r\n".encode("ascii") + data + b"\r\n")
            self.wfile.flush()
            return True
        except (BrokenPipeError, ConnectionResetError, OSError):
            return False

    def _handle_sse_message(self, query: str) -> None:
        """旧版传输的消息入口：POST /messages?sessionId=xxx"""
        if not self._auth():
            return
        sid = (parse_qs(query or "").get("sessionId") or [""])[0]
        with SSE_SESSIONS_LOCK:
            q = SSE_SESSIONS.get(sid)
        if q is None:
            self._err(404, "no_session", "SSE 会话不存在或已关闭")
            return

        info = self._mcp_info()
        try:
            raw = self._read_raw_body()
            msg = json.loads(raw.decode("utf-8"))
        except Exception as exc:  # noqa: BLE001
            q.put(mcpmod.error_response(None, -32700, f"解析失败：{exc}"))
            self._send(202, None, raw=b"")
            return

        dispatch = self._mcp_dispatch()
        if isinstance(msg, list):
            for r in mcpmod.handle_batch(
                dispatch, msg, info=info, disabled=self.agent.disabled_tools
            ):
                q.put(r)
        else:
            r = mcpmod.handle_message(
                dispatch, msg, info=info, disabled=self.agent.disabled_tools
            )
            if r is not None:
                q.put(r)
        self._send(202, None, raw=b"")

    def _mcp_get_probe(self) -> None:
        """Streamable HTTP 规范允许用 405 表示服务端不提供 GET 推送流"""
        self._send(
            405,
            {"jsonrpc": "2.0", "id": None,
             "error": {"code": -32000, "message": "请使用 POST /mcp 发送 JSON-RPC 消息"}},
            extra={"Allow": "POST"},
        )

    # 免鉴权路径集中写在这里，避免各 handler 各写各的 —— 漏一个就是一处暴露面
    PUBLIC_GET_PATHS = frozenset({"/health", "/api/health"})

    def _health_minimal(self) -> dict:
        """给负载均衡 / 自检脚本看的最小信息：不含主机名、Python 版本、目录数。"""
        return {"service": SERVICE, "version": VERSION}

    def _info_detail(self) -> dict:
        return {
            "service": SERVICE,
            "version": VERSION,
            "platform": sys.platform,
            "python": sys.version.split()[0],
            "read_only": self.agent.cfg.read_only,
            "allow_delete": self.agent.cfg.allow_delete,
            "allow_move": self.agent.cfg.allow_move,
            "roots_count": len(self.agent.cfg.roots),
            "readonly_token": bool(getattr(self.agent.cfg, "readonly_token", "")),
            "tls": tls_enabled(self.agent.cfg),
            "tls_chain": bool(tls_chain_path(self.agent.cfg)),
            "host": socket.gethostname(),
            "mcp": {
                "enabled": self._mcp_enabled(),
                "endpoints": ["/mcp", "/sse"],
                "tools": len(mcpmod.TOOLS) if mcpmod else 0,
                "tools_disabled": sorted(self.agent.disabled_tools),
            },
        }

    def do_GET(self):
        path = urlparse(self.path).path.rstrip("/") or "/"
        if path in self.PUBLIC_GET_PATHS:
            # 默认只回最小信息；public_health=false 时连这个也要鉴权
            if getattr(self.agent.cfg, "public_health", True):
                self._ok(self._health_minimal())
            elif self._auth():
                self._ok(self._health_minimal())
            return
        if path == "/":
            # 详细状态（主机名/版本/目录数）只在鉴权后给出
            if self._auth():
                self._ok(self._info_detail())
            return
        if path in ("/sse", "/mcp/sse"):
            if self._require_mcp():
                self._handle_sse_stream()
            return
        if path == "/mcp":
            if self._require_mcp():
                self._mcp_get_probe()
            return
        self._err(404, "not_found", f"没有这个接口：{self.path}")

    def do_POST(self):
        parsed = urlparse(self.path)
        path = parsed.path.strip("/")
        action = path.split("/")[-1] if path.startswith("api/") or path == "api" else path
        if not action:
            self._err(404, "not_found", "缺少接口名")
            return

        # MCP 直连：Trae / Cursor / Claude 等客户端走这两个端点
        if action == "mcp":
            if self._require_mcp():
                self._handle_mcp()
            return
        if action == "messages":
            if self._require_mcp():
                self._handle_sse_message(parsed.query)
            return

        if action not in Agent.ROUTES:
            self._err(404, "unknown_action", f"未知操作：{action}")
            return
        if not self._auth():
            return
        ip = self._client_ip()
        started = time.time()
        req = {}
        try:
            req = self._read_body()
            data = self.agent.dispatch(action, req)
            self.agent.audit.write(self._audit_record(action, req, ip, started))
            self._ok(data)
        except PathError as exc:
            self.agent.audit.write(self._audit_record(action, req, ip, started, exc))
            code = 400
            if exc.code == "not_found":
                code = 404
            elif exc.code == "out_of_scope":
                code = 403
            elif exc.code == "too_large":
                code = 413
            elif exc.code == "conflict":
                code = 409
            self._err(code, exc.code, exc.message)
        except (PermissionError, OSError) as exc:
            rec = self._audit_record(action, req, ip, started, exc)
            rec["error"] = str(exc)
            self.agent.audit.write(rec)
            self._err(500, "io_error", f"文件系统操作失败：{exc}")
        except Exception as exc:  # noqa: BLE001
            rec = self._audit_record(action, req, ip, started, exc)
            rec["error"] = repr(exc)
            rec["trace"] = traceback.format_exc()[-2000:]
            self.agent.audit.write(rec)
            self._err(500, "internal", f"服务端异常：{exc}")


# --------------------------------------------------------------------------------------
# 入口
# --------------------------------------------------------------------------------------


def build_parser():
    p = argparse.ArgumentParser(description=f"{SERVICE} v{VERSION} —— Windows 远程文件服务")
    p.add_argument("--config", default=None,
                   help="配置文件路径（默认 wkfile\\wkfile.config.json）")
    p.add_argument("--host", default=None, help="监听地址，默认 0.0.0.0")
    p.add_argument("--port", type=int, default=None, help="监听端口，默认 8788")
    p.add_argument("--token", default=None, help="访问 Token（覆盖配置文件）")
    p.add_argument("--roots", default=None, help="可操作根目录，多个用 ; 分隔")
    p.add_argument("--read-only", action="store_true", default=None, help="只读模式")
    p.add_argument("--init", action="store_true", help="生成配置文件（含随机 Token）后退出")
    p.add_argument("--gen-token", action="store_true", help="只打印一个新 Token 后退出")
    p.add_argument("--gen-readonly-token", action="store_true",
                   help="只打印一个「只读 Token」后退出（填进配置的 readonly_token）")
    p.add_argument("--gen-selfcert", action="store_true",
                   help="生成一张自签证书（纯标准库，含本机 IP 的 SAN）后退出")
    p.add_argument("--selfcert-dir", default=None,
                   help="自签证书保存目录（默认 数据目录\\tls）")
    p.add_argument("--selfcert-ip", default=None,
                   help="额外写进证书 SAN 的 IP 或域名，多个用 , 分隔")
    p.add_argument("--selfcert-cn", default=None, help="自签证书的 CN（默认 wkfile）")
    p.add_argument("--selfcert-days", type=int, default=None,
                   help="自签证书有效期天数（默认 3650，即 10 年）")
    p.add_argument("--renew-token", action="store_true",
                   help="重新生成 Token 并写回配置文件（旧 Token 立即失效）后退出")
    p.add_argument("--token-days", type=float, default=None,
                   help="Token 有效天数，0 = 永久有效（配合 --init / --renew-token 使用）")
    p.add_argument("--restrict-local", action="store_true",
                   help="只允许本机访问（等价于绑定 127.0.0.1 且不设 IP 白名单）")
    p.add_argument("--allow-unsafe", action="store_true",
                   help="危险：允许在无有效 Token 的情况下绑定非本机地址（默认拒绝启动）")
    return p


def _cli_gen_selfcert(args) -> int:
    """--gen-selfcert：生成一张自签证书并落盘，打印路径与后续怎么用。"""
    cert_path, key_path = default_selfcert_paths()
    if args.selfcert_dir:
        d = os.path.abspath(args.selfcert_dir)
        cert_path, key_path = os.path.join(d, "wkfile.crt"), os.path.join(d, "wkfile.key")

    extra = [x.strip() for x in (args.selfcert_ip or "").replace(";", ",").split(",")
             if x.strip()]
    days = int(args.selfcert_days) if args.selfcert_days else 3650

    print("[..] 正在生成 2048 位 RSA 密钥并签发证书，约需数秒…")
    t0 = time.time()
    try:
        cert_out, key_out = generate_selfcert(
            cert_path, key_path,
            cn=args.selfcert_cn or "wkfile",
            ips=extra or None,
            days=days,
        )
    except PathError as exc:
        print(f"[X] 生成失败：{exc.message}")
        return 3
    except Exception as exc:  # noqa: BLE001
        print(f"[X] 生成失败：{exc}")
        return 3

    print(f"[OK] 自签证书已生成（用时 {time.time() - t0:.1f} 秒）")
    print(f"     证书 : {cert_out}")
    print(f"     私钥 : {key_out}")
    desc = describe_cert(cert_out)
    if desc:
        print(f"     内容 : {desc}")
    print()
    print("     把这两个路径填进配置的 tls_cert / tls_key（或在界面页③勾上 HTTPS 后选上），")
    print("     保存并重启服务即生效。自签证书的私钥文件不要外传。")
    print("     客户端首次连接会提示证书不受信任，确认一次即可。")
    return 0


def main(argv=None):
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

    args = build_parser().parse_args(argv)
    cfg_path = args.config or default_config_path()

    if args.gen_readonly_token:
        print(secrets.token_urlsafe(36))
        print("# 把上面这串填进配置的 readonly_token，用它的客户端一律只能读、不能写。",
              file=sys.stderr)
        return 0

    if args.gen_selfcert:
        return _cli_gen_selfcert(args)

    if args.gen_token:
        tok = secrets.token_urlsafe(36)
        print(tok)
        if args.token_days is not None:
            print(f"# 有效期：{human_remain(max(0.0, args.token_days) * 86400)}"
                  f"（写到配置里请用 --renew-token --token-days {args.token_days:g}）")
        return 0

    cfg = load_config(cfg_path)

    if args.renew_token:
        cfg.token = secrets.token_urlsafe(36)
        if args.token_days is not None:
            cfg.token_ttl_days = max(0.0, float(args.token_days))
        cfg.token_expires_at = make_expiry(cfg.token_ttl_days)
        cfg.save()
        print(f"[OK] 新 Token 已写入 {cfg_path}（旧 Token 立即失效）")
        print(f"     token  : {cfg.token}")
        print(f"     有效期 : {expiry_text(cfg)}")
        print("     别忘了同步更新客户端的 MCP 配置。")
        return 0

    if args.init:
        if args.host:
            cfg.host = args.host
        if args.port:
            cfg.port = args.port
        if args.roots:
            cfg.roots = [os.path.abspath(r) for r in args.roots.split(";") if r.strip()]
        if args.token_days is not None:
            cfg.token_ttl_days = max(0.0, float(args.token_days))
            cfg.token_expires_at = make_expiry(cfg.token_ttl_days)
        if not cfg.token:
            cfg.token = secrets.token_urlsafe(36)
            if not cfg.token_expires_at:
                cfg.token_expires_at = make_expiry(cfg.token_ttl_days)
        cfg.save()
        print(f"[OK] 配置已写入 {cfg_path}")
        print(f"     host  : {cfg.host}")
        print(f"     port  : {cfg.port}")
        print(f"     roots : {cfg.roots or '(未配置，必须配置后才能用)'}")
        print(f"     token : {cfg.token}")
        print(f"     有效期: {expiry_text(cfg)}")
        print("     把 host / port / token 填进客户端的 mcp.config.json 即可。")
        return 0

    if args.host:
        cfg.host = args.host
    if args.port:
        cfg.port = args.port
    if args.token:
        cfg.token = args.token
    if args.roots:
        cfg.roots = [os.path.abspath(r) for r in args.roots.split(";") if r.strip()]
    if args.read_only is not None:
        cfg.read_only = args.read_only
    if args.restrict_local:
        cfg.host = "127.0.0.1"

    if args.token_days is not None:
        cfg.token_ttl_days = max(0.0, float(args.token_days))
        cfg.token_expires_at = make_expiry(cfg.token_ttl_days)

    if not cfg.token:
        cfg.token = secrets.token_urlsafe(36)
        if not cfg.token_expires_at:
            cfg.token_expires_at = make_expiry(cfg.token_ttl_days)
        cfg.save()
        print(f"[!] 未配置 Token，已自动生成并写入 {cfg_path}")
    elif cfg.token_ttl_days and not cfg.token_expires_at:
        # 老配置升级上来：设了有效天数却没算过到期时间，这里补一次
        cfg.token_expires_at = make_expiry(cfg.token_ttl_days)
        cfg.save()
        print(f"[!] 已按 token_ttl_days={cfg.token_ttl_days:g} 补算 Token 到期时间：{expiry_text(cfg)}")
    if not cfg.roots:
        print("[X] 还没有配置任何可操作目录（roots）。先执行：")
        print('    python wkfile_agent.py --init --roots "D:\\share"')
        return 2

    # ---- 启动前的最后一道闸：绝不把无鉴权的文件读写接口挂到网络上 ----
    expose = not is_loopback_bind(cfg.host)
    level, reason = token_strength(cfg.token)
    if expose and level == "bad" and not args.allow_unsafe:
        print("[X] 拒绝启动：监听地址是 %s（可被别的机器访问），但 %s。" % (cfg.host, reason))
        print("    这等于把「任意文件读写」裸放在网络上，跟发布未授权的远程代码执行没区别。")
        print("    解决其中一个即可：")
        print(f"      ① 在配置里换成随机 Token：python wkfile_agent.py --gen-token")
        print("      ② 或改成只监听本机：加 --restrict-local（仅本机/隧道场景）")
        print("      ③ 确属隔离测试环境，再加 --allow-unsafe 强行启动")
        return 3
    if expose and level == "weak":
        print(f"[!] 警告：监听非本机地址，而 Token {reason}，建议换更长的随机串。")

    cfg.roots = [os.path.abspath(r) for r in cfg.roots]
    missing = [r for r in cfg.roots if not os.path.isdir(r)]
    for r in missing:
        print(f"[!] 警告：根目录不存在 {r}")

    agent = Agent(cfg)
    Handler.agent = agent

    # 证书有问题就别等把它绑上端口才炸：先验一遍，报错时给一句人话
    tls_on = tls_enabled(cfg)
    tls_chain = tls_chain_path(cfg)
    if tls_on:
        try:
            validate_tls_pair(cfg.tls_cert, cfg.tls_key, tls_chain)
        except PathError as exc:
            print(f"[X] HTTPS 证书配置有问题：{exc.message}")
            print("    修好后重新启动；或清空 tls_cert / tls_key / tls_chain 先用 HTTP 跑起来。")
            return 4

    httpd = ThreadingHTTPServer((cfg.host, cfg.port), Handler)
    httpd.daemon_threads = True

    scheme = "http"
    cert_in_use = ""
    if tls_on:
        # 三件套（public + chain + key）时先把前两份合成 fullchain 再加载。
        # 合并结果落在数据目录的 tls\ 下，出问题时能直接打开看。
        cert_in_use = prepare_cert_file(cfg.tls_cert, tls_chain,
                                        work_dir=os.path.join(cfg.base_dir, "tls"))
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(certfile=cert_in_use, keyfile=cfg.tls_key)
        httpd.socket = ctx.wrap_socket(httpd.socket, server_side=True)
        scheme = "https"

    total_tools = len(mcpmod.TOOLS) if mcpmod else 0
    disabled = sorted(agent.disabled_tools)
    enabled_tools = max(0, total_tools - len(disabled))
    tstate, tdt, trem = token_state(cfg)
    token_flag = {
        "never": "[token 永久]",
        "valid": "[token 剩 %s]" % human_remain(trem),
        "expiring": "[token 剩 %s!]" % human_remain(trem),
        "expired": "[token 已过期!]",
        "bad": "[token 有效期格式错]",
    }[tstate]
    flags = [
        "[auth ON]" if cfg.token else "[no auth]",
        token_flag,
        "[ro-token %s]" % ("ON" if getattr(cfg, "readonly_token", "") else "OFF"),
        "[readonly %s]" % ("ON" if cfg.read_only else "OFF"),
        "[delete %s]" % ("ON" if cfg.allow_delete else "OFF"),
        "[move %s]" % ("ON" if cfg.allow_move else "OFF"),
        "[backup %s]" % ("ON" if cfg.auto_backup else "OFF"),
        "[preflight %s]" % ("ON" if getattr(cfg, "preflight_check", True) else "OFF"),
    ]
    print(f"[{SERVICE} {VERSION}]  listening {scheme}://{cfg.host}:{cfg.port}  " + "  ".join(flags))
    if tls_on:
        _desc = describe_cert(cfg.tls_cert)
        _n, _incomplete = chain_health(cfg.tls_cert, tls_chain)
        print(f"                TLS : 已启用（链 {_n} 张）" + (f"  {_desc}" if _desc else ""))
        if tls_chain:
            print(f"                cert: {cert_in_use}")
            print(f"                      ^ 由 {os.path.basename(cfg.tls_cert)} + "
                  f"{os.path.basename(tls_chain)} 合并而来")
        if _incomplete:
            print(f"                [!] {CHAIN_INCOMPLETE_SHORT}")
            print("                    -> Electron 客户端(Trae/Cursor)会报 "
                  "unable to verify the first certificate")
            print("                    -> 改用证书商的 public chain / fullchain 文件，"
                  "或把中间链填进 tls_chain")
    print(f"                roots: {len(cfg.roots)} 个  " + " ; ".join(cfg.roots))
    tools_line = f"                tools: {enabled_tools}/{total_tools} 个"
    if disabled:
        tools_line += "  已禁用：" + ", ".join(disabled)
    print(tools_line)
    print(f"                data : {cfg.base_dir}")
    print(f"                audit: {agent.audit.path}  "
          f"(轮转 {human_size(int(cfg.audit_max_bytes or 0))} × {cfg.audit_backup_count} 份)")
    print(f"                runlog: {run_log_path(cfg.base_dir)}  "
          f"(轮转 {human_size(int(getattr(cfg, 'run_log_max_bytes', 0) or 0))} "
          f"× {int(getattr(cfg, 'run_log_backup_count', 0) or 0)} 份)")
    print(f"                token: {expiry_text(cfg)}")
    if tstate == "expired":
        print(f"[X] 警告：Token 已于 {tdt:%Y-%m-%d %H:%M} 过期，现在所有请求都会被拒绝（401 token_expired）。")
        print(f"    续期：python wkfile_agent.py --renew-token --token-days 30  "
              f"（或打开界面点「生成」重新发一个）")
    elif tstate == "expiring":
        print(f"[!] 提示：Token 还剩 {human_remain(trem)} 就过期，记得提前续期，"
              f"否则客户端会突然连不上。")
    elif tstate == "bad":
        print("[!] 提示：token_expires_at 不是合法时间格式，已按「永久有效」处理；"
              "界面里重新点一次「生成」即可修正。")
    if expose and level == "weak":
        print("[!] 提示：非本机监听 + 偏弱 Token，配合限流可用，但建议换更长的随机串")
    if expose and not cfg.read_only and (cfg.allow_delete or cfg.allow_move):
        print("[!] 警告：非本机监听且允许删除/移动 —— 持有 Token 的人可以改这些目录里的任何文件。")
        print("    只做查看的话，请在界面勾上「只读模式」或取消「允许删除」。")
    base_url = f"{scheme}://{cfg.host}:{cfg.port}"
    if mcpmod is not None and getattr(cfg, "enable_mcp", True):
        print(f"   MCP  : {base_url}/mcp  (客户端里 type=http) |   {base_url}/sse  (type=sse)")
    else:
        print("   MCP  : 已关闭")
    print("   Ctrl+C 停止")
    try:
        # 服务常被重定向到日志文件，此时 stdout 是块缓冲 —— 不主动 flush
        # 的话启动信息要等缓冲区满才落盘，出问题时会看不到任何线索
        sys.stdout.flush()
    except Exception:  # noqa: BLE001
        pass
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止")
    finally:
        httpd.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
