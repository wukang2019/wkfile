"""wkfile_sqlsrv.py —— 纯标准库的 SQL Server（TDS 协议）驱动。

与 wkfile_mysql.py 同一套接口约定：
    * DBError(code, message)          —— 异常结构与 MySQL 驱动一致
    * classify_sql(sql)               —— (level, keyword, reason)
    * SQLServerSource                 —— run / tables / schema / test
    * dsn_summary(profile)            —— 给人看的连接摘要（绝不含密码）

设计边界（用之前先读，写在这里是为了不当场踩坑）：

1. 只支持 **SQL 身份验证**（用户名 + 密码）。Windows 集成认证要 NTLM/SSPI，
   纯标准库做不了，明确不支持，登录时报错也说清楚。
2. 只支持 **全链路加密**：prelogin 发 ENCRYPT_ON(0x01)，服务器回
   ON(0x01)/REQ(0x03) 才继续。服务器回 OFF（只加密登录包的模式）时直接
   报错 —— 那条路要登录后再做 SSL 关闭协商回到明文，复杂度高收益低；
   而默认安装的 SQL Server 都带自签名证书，会正常回 ON。
3. 目标 **TDS 7.4**（SQL Server 2012+）。更老的服务器会在 LOGINACK 里回
   更低版本：7.2/7.3 可以继续用（UserType 4 字节、rowcount 8 字节从 7.2
   起；NBCROW 只有 7.3+ 会发）；低于 7.2 直接报"服务器太老"。
4. TLS 握手的特殊之处：握手报文要包在 TDS 0x12 包里传输，握手完成后
   整条连接切成纯 TLS 流（TDS 包作为 TLS 应用数据）。这里用
   ssl.wrap_bio + 手工搬运实现，是本驱动最核心的技巧。
5. 不支持：MARS、TV/RPC 参数、cursor/ALTROW/COMPUTE 结果集、
   Windows 认证、旧版 Sybase。TEXT/IMAGE 大值整体读入（有字节上限）。
6. 协议事实核对来源：MS-TDS 官方规范 + FreeTDS 生产级 C 实现
   （login.c / data.c / tds.h 逐行核对过字段顺序与编码宽度）。
"""

from __future__ import annotations

import os
import re
import socket
import ssl
import struct
import time
from datetime import datetime, timedelta

MAX_TIMEOUT_SEC = 300.0

_WHITESPACE = re.compile(r"\s+")


# ===========================================================================
# 一、异常
# ===========================================================================

class DBError(Exception):
    """与 wkfile_mysql.DBError 同构：code + message。"""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message

    def __str__(self):
        return self.message


# ===========================================================================
# 二、SQL 定性（T-SQL 方言）
# ===========================================================================

def strip_sql_noise(sql: str) -> str:
    """去掉注释，把字符串字面量与 [方括号标识符] 的内容替换成空格。

    判定关键字与危险形态前必须先做这件事，否则
    `SELECT 'xp_cmdshell'` 会被当成危险语句、`WHERE c='a;b'` 会被当成
    两条语句（与 wkfile_mysql.strip_sql_noise 同一哲学）。
    """
    out = []
    i, n = 0, len(sql)
    while i < n:
        ch = sql[i]
        if ch in "'\"":
            quote = ch
            out.append(" ")
            i += 1
            while i < n:
                if sql[i] == quote:
                    if i + 1 < n and sql[i + 1] == quote:   # 连写两个引号 = 转义
                        i += 2
                        continue
                    i += 1
                    break
                i += 1
            continue
        if ch == "[":
            out.append(" ")
            i += 1
            while i < n:
                if sql[i] == "]":
                    if i + 1 < n and sql[i + 1] == "]":     # ]] = 转义的 ]
                        i += 2
                        continue
                    i += 1
                    break
                i += 1
            continue
        if sql[i:i + 2] == "--":
            j = sql.find("\n", i)
            i = n if j < 0 else j
            out.append(" ")
            continue
        if sql[i:i + 2] == "/*":
            j = sql.find("*/", i + 2)
            i = n if j < 0 else j + 2
            out.append(" ")
            continue
        out.append(ch)
        i += 1
    return "".join(out)


def split_statements(sql: str) -> list:
    """按分号（以及单独成行的 GO）切分，返回非空语句列表。"""
    cleaned = strip_sql_noise(sql)
    # GO 是 sqlcmd/SSMS 的批分隔符，用户手写 SQL 时很常见
    cleaned = re.sub(r"(?im)^go\s*$", ";", cleaned)
    parts = [p.strip() for p in cleaned.split(";")]
    return [p for p in parts if p]


# 危险形态：无论只读还是写通道都一律拒绝（与 mysql 驱动的 READ_FORBIDDEN
# 同一思路）。模式做子串匹配，都带空格/完整词形，避免误伤列名。
_DENY_PATTERNS = [
    ("xp_cmdshell", "xp_cmdshell 可以执行系统命令，永远禁止"),
    ("sp_configure", "sp_configure 修改服务器配置，永远禁止"),
    ("sp_oa", "sp_oa 系列（OLE 自动化）可以执行任意 COM，永远禁止"),
    ("openrowset", "OPENROWSET 可以访问任意外部数据源，永远禁止"),
    ("opendatasource", "OPENDATASOURCE 可以访问任意外部数据源，永远禁止"),
    ("openquery", "OPENQUERY 会在远端执行任意 SQL，永远禁止"),
    ("bulk insert", "BULK INSERT 从任意文件载入数据，永远禁止"),
    ("shutdown", "SHUTDOWN 会直接停掉数据库，永远禁止"),
    ("sp_send_dbmail", "sp_send_dbmail 发外部邮件，永远禁止"),
    ("sp_start_job", "sp_start_job 启动代理作业，永远禁止"),
    ("sp_replwritetovarbin", "sp_replwritetovarbin 是已知提权向量，永远禁止"),
    ("sp_executesql", "sp_executesql 执行动态 SQL，无法静态判定性质，永远禁止"),
    ("backup ", "BACKUP 属于运维操作，本服务不开放"),
    ("restore ", "RESTORE 属于运维操作，本服务不开放"),
    ("kill ", "KILL 会杀掉其他会话，永远禁止"),
]

_READ_KEYWORDS = {"select", "with", "use"}


def classify_sql(sql: str):
    """给一条 T-SQL 定性，返回 (level, keyword, reason)。

    level = "read" 只读 / "write" 写（需 allow_write）/ "deny" 一律拒绝。
    """
    if not sql or not sql.strip():
        return "deny", "", "SQL 不能为空"

    statements = split_statements(sql)
    if not statements:
        return "deny", "", "SQL 不能为空"
    if len(statements) > 1:
        return ("deny", "", f"一次只允许执行一条 SQL（检测到 {len(statements)} 条）。"
                            f"多条语句请分开调用，或用 -- 注释掉多余的")

    norm = _WHITESPACE.sub(" ", statements[0]).strip().lower()

    for pattern, why in _DENY_PATTERNS:
        if pattern in norm:
            return "deny", "", why or f"语句中包含被禁止的形态：{pattern}"

    m = re.match(r"[a-z_]+", norm)
    if not m:
        return "deny", "", "无法识别的 SQL 起始关键字"
    keyword = m.group(0)

    if keyword not in _READ_KEYWORDS:
        return "write", keyword, f"{keyword.upper()} 属于写操作"

    if keyword == "select":
        # SELECT ... INTO 会建表，是穿着查询外衣的写操作
        if re.search(r"\binto\b", norm):
            return "deny", "select", "SELECT ... INTO 会新建表，属于写操作，请改用 win_db_exec"
        return "read", keyword, "只读查询"

    if keyword == "with":
        body = norm[len("with"):]
        if re.search(r"\b(update|delete|insert|merge)\b", body):
            return "write", "with", "WITH 子句里包含写操作"
        if not re.search(r"\bselect\b", body):
            return "deny", "with", "WITH 子句里没有 SELECT，无法确认是只读查询"
        return "read", keyword, "只读查询"

    if keyword == "use":
        return "read", keyword, "切换当前数据库"

    return "read", keyword, "只读查询"


# ===========================================================================
# 三、超时工具（与 mysql 驱动同语义：dns_failed / timeout / conn_failed）
# ===========================================================================

def _clamp_timeout(value, default: float) -> float:
    """把配置里的秒数夹到安全区间；None/坏值/0/负数都用默认值。"""
    try:
        if isinstance(value, bool):
            return default
        sec = float(value)
    except (TypeError, ValueError):
        return default
    if not (sec > 0):
        return default
    return min(sec, MAX_TIMEOUT_SEC)


def _connect_bounded(host: str, port: int, timeout: float):
    """在 timeout 秒总预算内完成 DNS + TCP，每个地址只用剩余预算。"""
    started = time.monotonic()
    deadline = started + timeout
    try:
        infos = socket.getaddrinfo(host, port, 0, socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise DBError("dns_failed", f"域名解析失败：{host}（{exc}）")
    if not infos:
        raise DBError("dns_failed", f"域名没有解析出可用地址：{host}")

    timed_out = False
    last_exc = None
    for af, socktype, proto, _canon, sa in infos:
        remain = deadline - time.monotonic()
        if remain <= 0:
            timed_out = True
            break
        try:
            sock = socket.socket(af, socktype, proto)
        except OSError as exc:
            last_exc = exc
            continue
        try:
            sock.settimeout(remain)
            sock.connect(sa)
        except (socket.timeout, TimeoutError) as exc:
            timed_out = True
            last_exc = exc
            sock.close()
            continue
        except OSError as exc:
            last_exc = exc
            sock.close()
            continue
        return sock

    elapsed = time.monotonic() - started
    if timed_out:
        raise DBError(
            "timeout",
            f"连接 {host}:{port} 超时（预算 {timeout:g}s 已用完，"
            f"实际等了 {elapsed:.1f}s；可能是端口不通、防火墙拦截，"
            f"或该主机有解析得出但连不上的地址）",
        )
    raise DBError("conn_failed", f"连接 {host}:{port} 失败：{last_exc}")


# ===========================================================================
# 四、TDS 协议常量与编码（全部可单测的纯函数）
# ===========================================================================

# 包类型
PKT_SQL = 0x01          # SQL Batch
PKT_REPLY = 0x04        # 服务端响应（prelogin 响应/结果流都是它）
PKT_LOGIN7 = 0x10       # TDS7 登录
PKT_PRELOGIN = 0x12     # 7.2+ 的 prelogin / TLS 握手载体

# token 类型（MS-TDS 2.2.7.x；与 FreeTDS proto.h 核对）
TOK_RETURNSTATUS = 0x79
TOK_COLMETADATA = 0x81
TOK_ERROR = 0xAA
TOK_INFO = 0xAB
TOK_LOGINACK = 0xAD
TOK_ORDER = 0xA9
TOK_TABNAME = 0xA4
TOK_COLINFO = 0xA5
TOK_ENVCHANGE = 0xE3
TOK_RETURNVALUE = 0xAC
TOK_ROW = 0xD1
TOK_NBCROW = 0xD2
TOK_DONE = 0xFD
TOK_DONEPROC = 0xFE
TOK_DONEINPROC = 0xFF

# DONE 状态位（FreeTDS proto.h 核对）
DONE_MORE = 0x01
DONE_ERROR = 0x02
DONE_COUNT = 0x10

# TDS 版本（LOGIN7 TDSVersion 字段值）
TDS74_VERSION_VALUE = 0x74000004

# 列类型 token -> 人读名字
_TYPE_NAMES = {
    0x1F: "null", 0x22: "image", 0x23: "text", 0x24: "uniqueidentifier",
    0x26: "int", 0x28: "date", 0x29: "time", 0x2A: "datetime2",
    0x2B: "datetimeoffset", 0x30: "tinyint", 0x32: "bit", 0x34: "smallint",
    0x38: "int", 0x3A: "smalldatetime", 0x3B: "real", 0x3C: "money",
    0x3D: "datetime", 0x3E: "float", 0x62: "sql_variant", 0x63: "ntext",
    0x68: "bit", 0x6A: "decimal", 0x6C: "numeric", 0x6D: "float",
    0x6E: "money", 0x6F: "datetime", 0x7A: "smallmoney", 0x7F: "bigint",
    0xA5: "varbinary", 0xA7: "varchar", 0xAD: "binary", 0xAF: "char",
    0xE7: "nvarchar", 0xEF: "nchar", 0xF0: "udt", 0xF1: "xml",
}

# 无 metadata、无长度前缀的定长类型 -> 数据字节数
_FIXED_TYPES = {
    0x30: 1,    # tinyint
    0x32: 1,    # bit
    0x34: 2,    # smallint
    0x38: 4,    # int
    0x3A: 4,    # smalldatetime
    0x3B: 4,    # real
    0x3C: 8,    # money
    0x3D: 8,    # datetime
    0x3E: 8,    # float
    0x7A: 4,    # smallmoney
    0x7F: 8,    # bigint
}

# 带 1 字节 size metadata 的可空数值类型
_SIZED_TYPES = {0x24, 0x26, 0x68, 0x6D, 0x6E, 0x6F}

# collation 的 locale id -> 大致 codepage（BIGVARCHAR/BIGCHAR 解码用）
_LCID_TO_CP = {
    0x0804: "cp936", 0x0404: "cp950", 0x0c04: "cp950", 0x1004: "cp936",
    0x0411: "cp932", 0x0412: "cp949",
    0x0419: "cp1251", 0x0422: "cp1251", 0x0423: "cp1251",
    0x0408: "cp1253",
    0x0405: "cp1250", 0x040e: "cp1250", 0x0415: "cp1250", 0x0418: "cp1250",
    0x041b: "cp1250", 0x041a: "cp1250",
    0x041f: "cp1254", 0x040d: "cp1255", 0x0406: "cp1252",
}

# 旧式 SQL 排序规则（locale_id == 0 时）的 charset id -> codepage
_SQL_CHARSET_CP = {
    52: "cp1252", 53: "cp1250", 54: "cp1251", 55: "cp1253", 56: "cp1254",
    57: "cp1255", 58: "cp1256", 59: "cp1257", 60: "cp1258",
    49: "cp850", 48: "cp437",
}


def _collation_codepage(collation: bytes) -> str:
    """从 5 字节 collation 里尽力推断 codepage，推不出按 cp1252。"""
    if not collation or len(collation) < 5:
        return "cp1252"
    lcid = struct.unpack_from("<H", collation, 0)[0]
    if lcid:
        return _LCID_TO_CP.get(lcid, "cp1252")
    return _SQL_CHARSET_CP.get(collation[4], "cp1252")


def obfuscate_password(raw: bytes) -> bytes:
    """LOGIN7 密码混淆：每个字节高低半字节交换后异或 0xA5。

    与 FreeTDS tds7_crypt_pass 逐位一致（注意不是简单的 XOR 0x5A ——
    网上流传的那份说法是错的，这里按生产实现核对过）。
    """
    out = bytearray(len(raw))
    for i, b in enumerate(raw):
        swapped = ((b & 0x0F) << 4) | ((b & 0xF0) >> 4)
        out[i] = swapped ^ 0xA5
    return bytes(out)


def build_prelogin(version: int = TDS74_VERSION_VALUE, encrypt: int = 0x01) -> bytes:
    """构造客户端 PRELOGIN 数据（不含 TDS 包头）。

    TLV：token(1B) + offset(2B 大端，相对数据区) + length(2B 大端)。
    VERSION(0x00) 必须是第一项，我们只发 VERSION + ENCRYPTION + 终止符。
    """
    entries = [
        (0x00, struct.pack("<I", version)),
        (0x01, bytes([encrypt])),
    ]
    blob = b""
    directory = b""
    for token, value in entries:
        directory += bytes([token]) + struct.pack(">HH", len(blob), len(value))
        blob += value
    return directory + b"\xff" + blob


def parse_prelogin(payload: bytes) -> dict:
    """解析服务端 PRELOGIN 响应，返回 {token: bytes}。

    目录项：token(1B) + offset(2B 大端，相对数据区) + length(2B 大端)；
    数据区从 0xFF 终止符之后开始。
    """
    options = {}
    pos = 0
    end = len(payload)
    while pos < end:
        token = payload[pos]
        if token == 0xFF:
            break
        if pos + 5 > end:
            raise DBError("protocol", "服务端 PRELOGIN 响应残缺")
        off, length = struct.unpack_from(">HH", payload, pos + 1)
        base = _prelogin_data_base(payload)
        value = payload[base + off: base + off + length]
        if len(value) != length:
            raise DBError("protocol", "服务端 PRELOGIN 选项越界")
        options[token] = value
        pos += 5
    return options


def _prelogin_data_base(payload: bytes) -> int:
    """算出 PRELOGIN 数据区起点（目录区结束、紧跟 0xFF 终止符之后）。"""
    pos = 0
    while pos < len(payload):
        token = payload[pos]
        if token == 0xFF:
            return pos + 1
        pos += 5
    return len(payload)


def build_login7(*, host_name: str, server_name: str, user: str, password: str,
                 app_name: str = "wkfile", library: str = "wkfile",
                 database: str = "", packet_size: int = 4096) -> bytes:
    """构造完整的 LOGIN7 记录（不含 TDS 包头）。

    固定头 94 字节（TDS 7.2+），随后是 UTF-16LE 字符串数据区。
    字段顺序、OffsetLength 对的宽度（USHORT 偏移 + USHORT 字符数）、
    ClientID 6 裸字节、cbSSPILong 收尾 —— 全部与 FreeTDS tds7_send_login
    逐字段核对；86+8=94 这个尺寸本身也是两处独立推导的一致结果。
    """
    # 数据区字段顺序与 OffsetLength 对一一对应：
    # HostName, UserName, Password, AppName, ServerName, ServerSPN,
    # CltIntName, Language, Database（随后是 ClientID/SSPI/DBFile/ChangePassword）
    pwd_raw = (password or "")[:128].encode("utf-16-le")
    strings = [
        (host_name or "")[:128].encode("utf-16-le"),
        (user or "")[:128].encode("utf-16-le"),
        obfuscate_password(pwd_raw),
        (app_name or "wkfile")[:128].encode("utf-16-le"),
        (server_name or "")[:128].encode("utf-16-le"),
        b"",                                        # ServerSPN（SQL 认证留空）
        (library or "wkfile")[:128].encode("utf-16-le"),
        b"",                                        # Language
        (database or "")[:128].encode("utf-16-le"),
    ]

    header = bytearray(94)
    data_len = sum(len(s) for s in strings)
    struct.pack_into("<I", header, 0, 94 + data_len)     # Length
    struct.pack_into("<I", header, 4, TDS74_VERSION_VALUE)
    struct.pack_into("<I", header, 8, packet_size)       # PacketSize
    struct.pack_into("<I", header, 12, 0x07000000)       # ClientProgVer
    struct.pack_into("<I", header, 16, os.getpid() & 0xFFFFFFFF)
    struct.pack_into("<I", header, 20, 0)                # ConnectionID
    header[24] = 0xF0     # OptionFlags1: SET_LANG|USE_DB_NOTIFY|INIT_DB_FATAL|DUMPLOAD_OFF
    header[25] = 0x00     # OptionFlags2: SQL 登录
    header[26] = 0x00     # TypeFlags
    header[27] = 0x08     # OptionFlags3: UNKNOWN_COLLATION_HANDLING（7.3+）
    # 28..35: ClientTimeZone / ClientLCID = 0

    # OffsetLength 对区（USHORT 偏移从记录头 0 算起 + USHORT 字符数）
    pairs_at = 36
    client_id_at = 72
    sspi_at = 78
    pos = 36
    blob = bytearray()

    def put_pair(offset_value: int, byte_len: int):
        nonlocal pos
        struct.pack_into("<HH", header, pos, offset_value, byte_len // 2)
        pos += 4

    for s in strings:
        put_pair(94 + len(blob), len(s))
        blob += s
    # ClientID：6 裸字节（本机 MAC 不可靠也不必要，全零即可，服务端只做展示）
    header[client_id_at:client_id_at + 6] = b"\x00" * 6
    # ibSSPI/cchSSPI：SQL 认证时指向数据区末尾、长度 0（与 FreeTDS 行为一致）
    struct.pack_into("<HH", header, sspi_at, 94 + data_len, 0)
    # ibAtchDBFile / ibChangePassword：空
    struct.pack_into("<HH", header, 82, 0, 0)
    struct.pack_into("<HH", header, 86, 0, 0)
    struct.pack_into("<I", header, 90, 0)    # cbSSPILong

    return bytes(header) + bytes(blob)


def build_all_headers() -> bytes:
    """SQL Batch 前的事务描述头（MS-TDS 2.2.7.3.1 ALL_HEADERS）。

    TotalLength=22（含自身 4 字节）、HeaderLength=18、HeaderType 为
    USHORT（2 字节！）=0x0002、事务描述符 8 字节全零、计数 1。
    """
    return (struct.pack("<II", 22, 18) + struct.pack("<H", 2)
            + b"\x00" * 8 + struct.pack("<I", 1))


def packetize(ptype: int, payload: bytes, packet_size: int, start_pid: int = 1) -> bytes:
    """把 payload 切成 ≤packet_size 的 TDS 包序列（含包头，长度大端）。"""
    size = max(512, min(int(packet_size) or 4096, 32767))
    chunks = [payload[i:i + size] for i in range(0, len(payload), size)] or [b""]
    out = []
    for i, chunk in enumerate(chunks):
        status = 0x01 if i == len(chunks) - 1 else 0x00   # EOM
        pid = (start_pid - 1 + i) % 255 + 1
        out.append(struct.pack(">BBHHBB", ptype, status, 8 + len(chunk),
                                0, pid, 0) + chunk)
    return b"".join(out)


# ===========================================================================
# 五、字节读取与 token 解析
# ===========================================================================

class _Reader:
    """带 fill 回调的字节读取器：fill(n) 保证返回 n 字节或抛异常。"""

    def __init__(self, fill):
        self._fill = fill
        self._buf = b""

    def bytes(self, n: int) -> bytes:
        while len(self._buf) < n:
            self._buf += self._fill(max(n - len(self._buf), 4096))
        out, self._buf = self._buf[:n], self._buf[n:]
        return out

    def u8(self) -> int:
        return self.bytes(1)[0]

    def u16(self) -> int:
        return struct.unpack("<H", self.bytes(2))[0]

    def i16(self) -> int:
        return struct.unpack("<h", self.bytes(2))[0]

    def u16be(self) -> int:
        return struct.unpack(">H", self.bytes(2))[0]

    def u32(self) -> int:
        return struct.unpack("<I", self.bytes(4))[0]

    def i32(self) -> int:
        return struct.unpack("<i", self.bytes(4))[0]

    def u64(self) -> int:
        return struct.unpack("<Q", self.bytes(8))[0]

    def i64(self) -> int:
        return struct.unpack("<q", self.bytes(8))[0]

    def skip(self, n: int):
        if n > 0:
            self.bytes(n)

    def us_varchar(self) -> str:
        """BYTE 长度前缀 + 字节（ASCII/服务器名等用）。"""
        n = self.u8()
        return self.bytes(n).decode("latin-1", "replace")

    def ushort_varchar_utf16(self) -> str:
        """USHORT 长度前缀 + UTF-16LE（ERROR/INFO 的 MsgText 用）。"""
        n = self.u16()
        return self.bytes(n).decode("utf-16-le", "replace")


class _PacketFeeder:
    """把底层明文读取重组成 TDS 包，再按需供字节。

    fill(n)：直到攒够 n 字节 payload 为止（自动跨包重组）。
    last_packet_info 保留最近一个完整包的 (type, status, spid)。
    """

    def __init__(self, read_exact):
        self._re = read_exact
        self._pending = b""
        self.last_packet_info = (PKT_REPLY, 0, 0)

    def _next_packet(self):
        payload = b""
        while True:
            hdr = self._re(8)
            ptype, status, length, spid, _pid, _win = struct.unpack(">BBHHBB", hdr)
            if length < 8 or length > 65535:
                raise DBError("protocol", f"服务端包长非法：{length}")
            need = length - 8
            data = self._re(need) if need else b""
            payload += data
            if status & 0x01:      # EOM
                self.last_packet_info = (ptype, status, spid)
                return payload

    def fill(self, n: int) -> bytes:
        while len(self._pending) < n:
            self._pending += self._next_packet()
        out, self._pending = self._pending[:n], self._pending[n:]
        return out


# ---------- 列类型与取值解码 ----------

def _parse_type_info(r: _Reader):
    """读一列的 TYPE_INFO，返回 (type_id, meta)。"""
    t = r.u8()
    meta = {}
    if t == 0x1F or t in _FIXED_TYPES or t == 0x28:
        pass                                    # 无 metadata
    elif t in _SIZED_TYPES:
        meta["size"] = r.u8()
    elif t in (0x29, 0x2A, 0x2B):
        meta["scale"] = r.u8()
    elif t in (0x6A, 0x6C):
        meta["size"] = r.u8()
        meta["precision"] = r.u8()
        meta["scale"] = r.u8()
    elif t in (0xA5, 0xAD):
        meta["maxlen"] = r.u16()
    elif t in (0xA7, 0xAF, 0xE7, 0xEF):
        meta["maxlen"] = r.u16()
        meta["collation"] = r.bytes(5)
    elif t in (0x22, 0x62):
        meta["maxlen"] = r.u32()
    elif t in (0x23, 0x63):
        meta["maxlen"] = r.u32()
        meta["collation"] = r.bytes(5)
    elif t == 0xF1:                             # XML
        flag = r.u8()
        if flag & 0x01:
            # 带架构信息：库名.架构名.集合名（各 BYTE 长度 + UTF-16）
            r.us_varchar()
            r.us_varchar()
            r.us_varchar()
    elif t == 0xF0:                             # UDT
        r.u16()
        r.us_varchar()
        r.us_varchar()
        r.us_varchar()
        r.us_varchar()
    else:
        raise DBError("protocol", f"暂不支持的列类型 0x{t:02X}")
    return t, meta


def _plp_value(r: _Reader, max_bytes: int):
    """PLP（max 大对象）取值：8B 长度 + 分块流（4B 长度，0 结束）。"""
    total = r.u64()
    if total == 0xFFFFFFFFFFFFFFFF:
        return None
    out = bytearray()
    while True:
        chunk_len = r.u32()
        if chunk_len == 0:
            break
        out += r.bytes(min(chunk_len, max_bytes))
        if len(out) > max_bytes:
            raise DBError("protocol", f"大对象超过 {max_bytes} 字节上限")
        if chunk_len > max_bytes:
            r.skip(chunk_len - min(chunk_len, max_bytes))
    return bytes(out)


def _read_value(r: _Reader, t: int, meta: dict, max_bytes: int):
    """按列类型读一个值（ROW 与 NBCROW 共用；NULL 由长度前缀表达）。"""
    if t == 0x1F:
        return None
    if t in _FIXED_TYPES:
        raw = r.bytes(_FIXED_TYPES[t])
        if t == 0x30:
            return raw[0]
        if t == 0x32:
            return raw[0] != 0
        if t == 0x34:
            return struct.unpack("<h", raw)[0]
        if t == 0x38:
            return struct.unpack("<i", raw)[0]
        if t == 0x3A:                       # smalldatetime
            days, minutes = struct.unpack("<HH", raw)
            return str(datetime(1900, 1, 1) + timedelta(days=days, minutes=minutes))
        if t == 0x3B:
            return struct.unpack("<f", raw)[0]
        if t == 0x3C:                       # money：高位 dword 在前
            hi, lo = struct.unpack("<iI", raw)
            value = (hi << 32) | lo
            if value % 10000 == 0:
                return value // 10000
            return value / 10000.0
        if t == 0x3D:                       # datetime：天数 + 1/300 秒
            days, ticks = struct.unpack("<iI", raw)
            secs, frac = divmod(ticks, 300)
            return str(datetime(1900, 1, 1)
                      + timedelta(days=days, seconds=secs,
                                  microseconds=round(frac * 10000 / 3)))
        if t == 0x3E:
            return struct.unpack("<d", raw)[0]
        if t == 0x7A:                       # smallmoney
            return struct.unpack("<i", raw)[0] / 10000.0
        if t == 0x7F:
            return struct.unpack("<q", raw)[0]

    if t in _SIZED_TYPES:
        size = r.u8()
        if size == 0:
            return None
        raw = r.bytes(size)
        if t == 0x24:                       # uniqueidentifier
            d1, d2, d3 = struct.unpack_from("<IHH", raw, 0)
            return "%08X-%04X-%04X-%s-%s" % (
                d1, d2, d3, raw[8:10].hex().upper(), raw[10:16].hex().upper())
        if t == 0x68:                       # bitn
            return raw[0] != 0
        if t == 0x26:                       # intn
            return int.from_bytes(raw, "little", signed=True)
        if t == 0x6D:                       # fltn
            return struct.unpack("<f" if size == 4 else "<d",
                                  raw[:struct.calcsize("<f" if size == 4 else "<d")])[0]
        if t == 0x6E:                       # moneyn
            if size == 4:
                return struct.unpack("<i", raw)[0] / 10000.0
            hi, lo = struct.unpack("<iI", raw)
            value = (hi << 32) | lo
            if value % 10000 == 0:
                return value // 10000
            return value / 10000.0
        if t == 0x6F:                       # datetimn
            if size == 4:
                days, minutes = struct.unpack("<HH", raw)
                return str(datetime(1900, 1, 1) + timedelta(days=days, minutes=minutes))
            days, ticks = struct.unpack("<iI", raw)
            secs, frac = divmod(ticks, 300)
            return str(datetime(1900, 1, 1)
                      + timedelta(days=days, seconds=secs,
                                  microseconds=round(frac * 10000 / 3)))

    if t == 0x28:                           # date：1B 前缀 + 3B 天数
        n = r.u8()
        if n == 0:
            return None
        days = int.from_bytes(r.bytes(3), "little")
        try:
            return str(datetime(1, 1, 1) + timedelta(days=days))[:10]
        except OverflowError:
            return f"date+{days}d"

    if t in (0x29, 0x2A, 0x2B):            # time / datetime2 / datetimeoffset
        n = r.u8()
        if n == 0:
            return None
        scale = meta.get("scale", 7)
        tbytes = 3 if scale <= 2 else (4 if scale <= 4 else 5)
        ticks = int.from_bytes(r.bytes(tbytes), "little")
        if t == 0x29:
            return _format_time(ticks, scale)
        date_days = int.from_bytes(r.bytes(3), "little")
        date_str = str(datetime(1, 1, 1) + timedelta(days=date_days))[:10]
        if t == 0x2A:
            return f"{date_str} {_format_time(ticks, scale)}"
        offset = r.i16()
        sign = "+" if offset >= 0 else "-"
        return (f"{date_str} {_format_time(ticks, scale)}"
                f"{sign}{abs(offset) // 60:02d}:{abs(offset) % 60:02d}")

    if t in (0x6A, 0x6C):                   # decimal / numeric
        n = r.u8()
        if n == 0:
            return None
        sign = r.u8()
        digits = r.bytes(n - 1)
        return _format_decimal(0 if not digits else
                               int.from_bytes(digits, "big", signed=False),
                               meta.get("scale", 0), sign != 0)

    if t in (0xA5, 0xA7, 0xAD, 0xAF, 0xE7, 0xEF):
        if meta.get("maxlen") == 0xFFFF:    # varchar(max) 系：PLP
            raw = _plp_value(r, max_bytes)
            if raw is None:
                return None
            if t == 0xE7:
                return raw.decode("utf-16-le", "replace")
            if t == 0xA7:
                return raw.decode(_collation_codepage(meta.get("collation", b"")), "replace")
            return "0x" + raw.hex()
        n = r.u16()
        if n == 0xFFFF:
            return None
        raw = r.bytes(n)
        if t in (0xE7, 0xEF):
            return raw.decode("utf-16-le", "replace")
        if t in (0xA7, 0xAF):
            return raw.decode(_collation_codepage(meta.get("collation", b"")), "replace")
        return "0x" + raw.hex()

    if t in (0x22, 0x23, 0x63):             # image / text / ntext
        ptr_len = r.u8()
        if ptr_len == 0:
            return None
        r.skip(16 + 8)                      # textptr + timestamp
        n = r.u32()
        raw = r.bytes(min(n, max_bytes))
        if n > max_bytes:
            raise DBError("protocol", f"text/image 值超过 {max_bytes} 字节上限")
        if t == 0x63:
            return raw.decode("utf-16-le", "replace")
        if t == 0x23:
            return raw.decode(_collation_codepage(meta.get("collation", b"")), "replace")
        return "0x" + raw.hex()

    if t == 0xF1:                           # xml：PLP
        raw = _plp_value(r, max_bytes)
        return None if raw is None else raw.decode("utf-16-le", "replace")

    if t == 0x62:                           # sql_variant
        raise DBError("protocol", "sql_variant 类型暂不支持读取（请在 SQL 里显式 CAST）")

    raise DBError("protocol", f"暂不支持的列类型取值 0x{t:02X}")


def _format_time(ticks: int, scale: int) -> str:
    """TIME/DATETIME2 的时间部分。ticks 单位 10^scale-7 秒。"""
    total_ns = ticks * (10 ** (7 - scale))
    secs, ns = divmod(total_ns, 10 ** 7)
    h, rem = divmod(secs, 3600)
    m, s = divmod(rem, 60)
    if scale <= 0:
        return f"{h:02d}:{m:02d}:{s:02d}"
    frac = round(ns / 10 ** (7 - scale))
    digits = min(scale, 7)
    return f"{h:02d}:{m:02d}:{s:02d}.{frac:0{digits}d}"


def _format_decimal(digits: int, scale: int, positive: bool) -> str:
    """DECIMAL/NUMERIC 精确字符串（绝不经浮点，避免精度损失）。"""
    text = str(digits)
    if scale <= 0:
        return text if positive else "-" + text
    text = text.rjust(scale + 1, "0")
    out = text[:-scale] + "." + text[-scale:]
    return out if positive else "-" + out


# ---------- token 流解析 ----------

class _ResponseParser:
    """解析服务端 token 流。fill(n) 供字节；tds_ge_72 决定 UserType/RowCount 宽度。"""

    def __init__(self, fill, tds_ge_72: bool, notes: list,
                 on_envchange=None, max_value_bytes: int = 4 * 1024 * 1024,
                 pending=None):
        self.r = _Reader(fill)
        self.tds_ge_72 = tds_ge_72
        self.notes = notes if notes is not None else []
        self._on_envchange = on_envchange
        self._max_value_bytes = max_value_bytes
        self._pending = pending      # 返回缓冲区剩余字节数（登录收尾用）
        self.columns = []
        self.rows = []
        self.affected_rows = None
        self.truncated = False

    # -- 登录响应：到 LOGINACK（并排空缓冲内尾部 token）或 ERROR 为止 --

    def process_login(self) -> dict:
        r = self.r
        info = {}
        while True:
            # LOGINACK 已见到且缓冲里没有多余字节 → 登录响应消费完毕。
            # 缓冲里若还有（如 LOGINACK 后跟着的 INFO），继续消费掉，
            # 避免脏数据漏进下一条查询的 token 流。
            if info and self._pending is not None and self._pending() == 0:
                return info
            token = r.u8()
            if token == TOK_ENVCHANGE:
                self._envchange()
            elif token == TOK_INFO:
                number, state, level, msg = _read_error_token(r)
                self.notes.append(f"SQL Server 提示 [{level}/{state}] {msg}")
            elif token == TOK_LOGINACK:
                interface = r.u8()
                ver = r.u16be()
                prog = r.us_varchar()
                prog_ver = r.u32()
                info = {
                    "interface": interface,
                    "tds_version": (ver >> 8, ver & 0xFF),
                    "program": prog,
                    "program_version": (prog_ver >> 24, (prog_ver >> 16) & 0xFF,
                                        prog_ver & 0xFFFF),
                }
                return info
            elif token == TOK_ERROR:
                number, state, level, msg = _read_error_token(r)
                code = "access_denied" if number in (18456, 18452, 4064) else "auth_failed"
                raise DBError(code, f"登录被拒绝（{number}/{level}/{state}）：{msg}")
            elif token in (TOK_DONE, TOK_DONEPROC, TOK_DONEINPROC):
                status, _cmd, rowcount = self._done()
                if status & DONE_ERROR:
                    raise DBError("auth_failed", "登录阶段服务器返回错误状态")
                return info
            else:
                raise DBError("protocol", f"登录响应里出现意外 token 0x{token:02X}")

    # -- 查询响应：到 DONE / DONEPROC 为止 --

    def process_query(self, max_rows: int, max_bytes: int) -> dict:
        r = self.r
        while True:
            token = r.u8()
            if token == TOK_COLMETADATA:
                self._colmetadata()
            elif token == TOK_ROW:
                self._row(max_rows, max_bytes)
            elif token == TOK_NBCROW:
                self._nbcrow(max_rows, max_bytes)
            elif token == TOK_ENVCHANGE:
                self._envchange()
            elif token == TOK_INFO:
                number, state, level, msg = _read_error_token(r)
                self.notes.append(f"SQL Server 提示 [{level}/{state}] {msg}")
            elif token == TOK_ERROR:
                number, state, level, msg = _read_error_token(r)
                raise DBError("sql_error",
                              f"Msg {number}, Level {level}, State {state}：{msg}")
            elif token == TOK_ORDER:
                count = r.u16()
                r.skip(count * 2)
            elif token == TOK_RETURNSTATUS:
                r.i32()
            elif token in (TOK_DONE, TOK_DONEPROC):
                status, _cmd, rowcount = self._done()
                if status & DONE_COUNT:
                    self.affected_rows = rowcount
                return self._result()
            elif token == TOK_DONEINPROC:
                status, _cmd, rowcount = self._done()
                if status & DONE_COUNT and self.affected_rows is None:
                    self.affected_rows = rowcount
            else:
                raise DBError(
                    "protocol",
                    f"暂不支持的返回 token 0x{token:02X}"
                    f"（该结果集形态本驱动不支持，请改写 SQL）")

    # -- 内部 --

    def _colmetadata(self):
        r = self.r
        count = r.u16()
        self.columns = []
        if count == 0xFFFF:          # 无列信息（如 EXEC 无结果集）
            return
        for _ in range(count):
            if self.tds_ge_72:
                r.u32()              # UserType
            else:
                r.u16()
            r.u16()                  # Flags
            t, meta = _parse_type_info(r)
            nlen = r.u8()
            name = r.bytes(nlen).decode("utf-16-le", "replace")
            self.columns.append({"name": name, "type_id": t, "meta": meta,
                                 "type_name": _TYPE_NAMES.get(t, f"0x{t:02X}")})

    def _row(self, max_rows: int, max_bytes: int):
        r = self.r
        row = []
        total = 0
        for col in self.columns:
            value = _read_value(r, col["type_id"], col["meta"], self._max_value_bytes)
            row.append(value)
            total += len(str(value)) if value is not None else 4
        if len(self.rows) >= max_rows:
            self.truncated = True
            return
        self.rows.append(row)
        if total > max_bytes:
            self.truncated = True

    def _nbcrow(self, max_rows: int, max_bytes: int):
        r = self.r
        ncols = len(self.columns)
        bitmap = r.bytes((ncols + 7) // 8)
        row = []
        total = 0
        for i, col in enumerate(self.columns):
            if bitmap[i // 8] & (1 << (i % 8)):
                row.append(None)
                total += 4
                continue
            value = _read_value(r, col["type_id"], col["meta"], self._max_value_bytes)
            row.append(value)
            total += len(str(value)) if value is not None else 4
        if len(self.rows) >= max_rows:
            self.truncated = True
            return
        self.rows.append(row)
        if total > max_bytes:
            self.truncated = True

    def _done(self):
        r = self.r
        status = r.u16()
        cmd = r.u16()
        rowcount = r.i64() if self.tds_ge_72 else r.i32()
        return status, cmd, rowcount

    def _envchange(self):
        r = self.r
        etype = r.u8()
        length = r.u16()
        blob = r.bytes(length)
        # 已知类型解码，未知类型整块跳过（Length 字段保证不越界）
        if etype == 0x01:                     # Database：UTF-16LE
            if self._on_envchange:
                self._on_envchange("database", _env_str(blob, "utf-16-le"))
        elif etype == 0x04:                   # PacketSize：ASCII 数字
            if self._on_envchange:
                self._on_envchange("packet_size", _env_str(blob, "latin-1"))

    def _result(self):
        return {
            "columns": [c["name"] for c in self.columns],
            "column_types": [c["type_name"] for c in self.columns],
            "rows": self.rows,
            "row_count": len(self.rows),
            "affected_rows": self.affected_rows,
            "insert_id": None,
            "truncated": self.truncated,
        }


def _env_str(blob: bytes, encoding: str):
    """ENVCHANGE 的 NewValue：BYTE 长度前缀 + 指定编码的字符串。"""
    if not blob:
        return None
    n = blob[0]
    return blob[1:1 + n].decode(encoding, "replace")


def _read_error_token(r: _Reader):
    number = r.i32()
    state = r.u8()
    level = r.u8()
    msg = r.ushort_varchar_utf16()
    server = r.us_varchar()
    proc = r.us_varchar()
    line = r.u16()
    return number, state, level, msg


# ===========================================================================
# 六、连接（prelogin -> TLS -> LOGIN7 -> 查询）
# ===========================================================================

class SQLServerConn:
    """一次性 SQL Server 连接（与 MySQLConn 同哲学：用后即关，不池化）。"""

    def __init__(self, host, port, user, password, database="",
                 connect_timeout=8.0, query_timeout=30.0, notes=None):
        self.host = host
        self.port = int(port)
        self.user = user
        self.password = password.encode("utf-8") if isinstance(password, str) else password
        self.database = database or ""
        self.connect_timeout = _clamp_timeout(connect_timeout, 8.0)
        self.query_timeout = _clamp_timeout(query_timeout, 30.0)
        self.notes = notes if notes is not None else []
        self.sock = None
        self._ssl = None
        self._feeder = None
        self._packet_size = 4096
        self._pkt_id = 1
        self.server_version = ""
        self.spid = 0
        self.current_database = ""
        self._tds_ge_72 = True

    # ---------- 明文阶段（prelogin / TLS 握手期） ----------

    def _plain_send(self, raw: bytes):
        try:
            self.sock.sendall(raw)
        except socket.timeout:
            raise DBError("timeout", f"与 {self.host}:{self.port} 通信超时")
        except OSError as exc:
            raise DBError("conn_failed", f"与数据库通信中断：{exc}")

    def _plain_read_exact(self, n: int) -> bytes:
        buf = b""
        while len(buf) < n:
            try:
                chunk = self.sock.recv(n - len(buf))
            except socket.timeout:
                raise DBError("timeout", f"与 {self.host}:{self.port} 通信超时")
            except OSError as exc:
                raise DBError("conn_failed", f"与数据库通信中断：{exc}")
            if not chunk:
                raise DBError("conn_closed", "连接被数据库关闭")
            buf += chunk
        return buf

    def _plain_next_packet(self):
        payload = b""
        while True:
            hdr = self._plain_read_exact(8)
            ptype, status, length, spid, _pid, _win = struct.unpack(">BBHHBB", hdr)
            if length < 8 or length > 65535:
                raise DBError("protocol", f"服务端包长非法：{length}")
            need = length - 8
            payload += self._plain_read_exact(need) if need else b""
            if status & 0x01:
                return ptype, status, spid, payload

    # ---------- TLS 阶段 ----------

    def _tls_context(self):
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        try:
            # 允许降到 TLS 1.0：老版本 SQL Server（2008 时代）可能只有它
            ctx.minimum_version = ssl.TLSVersion.TLSv1
        except (AttributeError, ssl.SSLError, ValueError):
            pass
        return ctx

    def _handshake(self):
        """TLS 握手：握手报文包在 TDS 0x12 包里来回搬运，完成后切纯 TLS。"""
        ctx = self._tls_context()
        self._ssl = ctx.wrap_bio(ssl.MemoryBIO(), ssl.MemoryBIO())
        while True:
            want = None
            try:
                self._ssl.do_handshake()
            except ssl.SSLWantReadError:
                want = "read"
            except ssl.SSLWantWriteError:
                want = "write"
            except (ssl.SSLError, OSError) as exc:
                raise DBError("tls_failed", f"TLS 握手失败：{exc}")
            else:
                self._flush_tls_wrapped()
                return
            self._flush_tls_wrapped()
            if want == "read":
                ptype, _status, _spid, payload = self._plain_next_packet()
                self._ssl.bio_write(payload)

    def _flush_tls_wrapped(self):
        """把 TLS 引擎待发数据以 TDS 0x12 包形式发出去（握手期专用）。"""
        while True:
            try:
                out = self._ssl.bio_read(65536)
            except ssl.SSLWantReadError:
                return
            except ssl.SSLError as exc:
                raise DBError("tls_failed", f"TLS 数据搬运失败：{exc}")
            self._plain_send(packetize(PKT_PRELOGIN, out, 4096))

    def _tls_send(self, raw: bytes):
        for i in range(0, len(raw), 65536):
            self._ssl.write(raw[i:i + 65536])
            while True:
                try:
                    out = self._ssl.bio_read(65536)
                except ssl.SSLWantReadError:
                    break
                self._plain_send(out)

    def _tls_read_exact(self, n: int) -> bytes:
        out = b""
        while len(out) < n:
            try:
                chunk = self._ssl.read(n - len(out))
            except ssl.SSLWantReadError:
                # TLS 引擎要密文：从 socket 收并喂进去
                try:
                    wire = self.sock.recv(65536)
                except socket.timeout:
                    raise DBError("timeout", f"与 {self.host}:{self.port} 通信超时")
                except OSError as exc:
                    raise DBError("conn_failed", f"与数据库通信中断：{exc}")
                if not wire:
                    raise DBError("conn_closed", "连接被数据库关闭")
                self._ssl.bio_write(wire)
                continue
            except ssl.SSLZeroReturnError:
                raise DBError("conn_closed", "连接被数据库关闭（TLS 关闭通知）")
            except ssl.SSLError as exc:
                raise DBError("conn_failed", f"TLS 读取失败：{exc}")
            if chunk:
                out += chunk
        return out

    # ---------- TDS 发送 ----------

    def _send_tds(self, ptype: int, payload: bytes):
        raw = packetize(ptype, payload, self._packet_size, self._pkt_id)
        self._pkt_id = (self._pkt_id + 1) % 255 or 1
        self._tls_send(raw)

    # ---------- 连接主流程 ----------

    def connect(self):
        started = time.monotonic()
        self.sock = _connect_bounded(self.host, self.port, self.connect_timeout)
        try:
            self.sock.settimeout(self.connect_timeout)
            self._negotiate_prelogin()
            self._handshake()
            self._login()
            self.sock.settimeout(self.query_timeout)
        except Exception:
            self.close()
            raise
        self.connect_ms = round((time.monotonic() - started) * 1000, 1)
        return self

    def _negotiate_prelogin(self):
        self._plain_send(packetize(PKT_PRELOGIN, build_prelogin(), 4096))
        ptype, _status, _spid, payload = self._plain_next_packet()
        if ptype != PKT_REPLY:
            raise DBError("protocol", f"prelogin 响应包类型异常：0x{ptype:02X}")
        options = parse_prelogin(payload)
        enc = options.get(0x01)
        if enc == b"\x01" or enc == b"\x03":
            return                       # ON / REQ：整条 TLS，继续
        if enc == b"\x00":
            raise DBError(
                "tls_required",
                "服务器协商结果为 ENCRYPT_OFF（仅加密登录包），本驱动只支持"
                "全链路加密。请在 SQL Server 端允许加密（默认自签名证书即可），"
                "或换一个开启了加密的数据源。",
            )
        raise DBError("tls_required", "服务器不支持加密（ENCRYPT_NOT_SUP）")

    def _login(self):
        record = build_login7(
            host_name="wkfile",
            server_name=self.host,
            user=self.user,
            password=self.password.decode("utf-8", "replace"),
            database=self.database,
        )
        self._send_tds(PKT_LOGIN7, record)
        feeder = _PacketFeeder(self._tls_read_exact)
        self._feeder = feeder
        parser = _ResponseParser(feeder.fill, True, self.notes,
                                 on_envchange=self._on_envchange,
                                 pending=lambda: len(feeder._pending))
        info = parser.process_login()
        major, minor = info.get("tds_version", (7, 4))
        if (major, minor) < (7, 2):
            raise DBError(
                "server_unsupported",
                f"服务器 TDS 版本过旧（{major}.{minor}），本驱动要求 SQL Server 2008+",
            )
        self._tds_ge_72 = True
        prog = info.get("program") or "SQL Server"
        pmaj, pmin, pbuild = info.get("program_version", (0, 0, 0))
        self.server_version = f"{prog} {pmaj}.{pmin}.{pbuild} (TDS {major}.{minor})"
        self.spid = feeder.last_packet_info[2]

    def _on_envchange(self, kind, value):
        if kind == "database":
            self.current_database = value or ""
        elif kind == "packet_size":
            try:
                self._packet_size = max(512, min(int(value), 32767))
            except (TypeError, ValueError):
                pass

    # ---------- 查询 ----------

    def set_readonly_session(self):
        """T-SQL 没有会话级只读开关，说明一下即可（拦截靠 SQL 分类器）。"""
        self.notes.append("SQL Server 无会话级只读模式，写拦截依赖 SQL 分类器")

    def query(self, sql: str, *, max_rows=200, max_bytes=4 * 1024 * 1024):
        """执行一条 SQL batch，返回与 MySQLConn.query 同构的结果。"""
        payload = build_all_headers() + sql.encode("utf-16-le")
        self._send_tds(PKT_SQL, payload)
        parser = _ResponseParser(self._feeder.fill, self._tds_ge_72, self.notes,
                                 on_envchange=self._on_envchange,
                                 max_value_bytes=max_bytes)
        return parser.process_query(max_rows, max_bytes)

    def close(self):
        sock, self.sock = self.sock, None
        self._ssl = None
        if sock is None:
            return
        try:
            sock.close()
        except Exception:  # noqa: BLE001
            pass

    def __enter__(self):
        return self.connect()

    def __exit__(self, *exc):
        self.close()


# ===========================================================================
# 七、面向 wkfile 的高层封装
# ===========================================================================

def dsn_summary(profile: dict) -> str:
    """给人看的连接摘要，绝不包含密码。"""
    host = profile.get("host") or "127.0.0.1"
    port = profile.get("port") or 1433
    user = profile.get("user") or "sa"
    db = profile.get("database") or ""
    return f"sqlserver://{user}@{host}:{port}/{db}"


_IDENT_RE = re.compile(r"^[A-Za-z0-9_@$#]+$")


def _bracket(name: str) -> str:
    """标识符转 [name]；只放行安全字符，其余一律拒绝（防注入）。"""
    name = str(name or "").strip()
    if not _IDENT_RE.match(name):
        raise DBError("bad_request", f"标识符不合法：{name!r}")
    return f"[{name}]"


def _nstr(text: str) -> str:
    """字符串转 N'...' 字面量。"""
    return "N'" + str(text).replace("'", "''") + "'"


class SQLServerSource:
    """一个已配置的 SQL Server 数据源。"""

    def __init__(self, profile: dict):
        self.profile = profile or {}
        self.name = str(self.profile.get("name") or "sqlserver")
        self.allow_write = bool(self.profile.get("allow_write"))
        self.max_rows = max(1, min(int(self.profile.get("max_rows") or 200), 5000))
        self.timeout = _clamp_timeout(self.profile.get("timeout_sec"), 30.0)
        self.connect_timeout = _clamp_timeout(
            self.profile.get("connect_timeout_sec"), 8.0)

    def _conn(self) -> SQLServerConn:
        p = self.profile
        return SQLServerConn(
            host=p.get("host") or "127.0.0.1",
            port=p.get("port") or 1433,
            user=p.get("user") or "sa",
            password=p.get("password") or "",
            database=p.get("database") or "",
            connect_timeout=self.connect_timeout,
            query_timeout=self.timeout,
        )

    def run(self, sql: str, *, max_rows=None, force_readonly=False) -> dict:
        level, keyword, reason = classify_sql(sql)
        if level == "deny":
            raise DBError("sql_rejected", reason)
        if level == "write":
            if force_readonly:
                raise DBError(
                    "read_only",
                    f"{reason}。本次用的是只读 Token，数据库写操作一律拒绝。",
                )
            if not self.allow_write:
                raise DBError(
                    "write_disabled",
                    f"{reason}。数据源「{self.name}」未开启允许写入"
                    f"（界面④数据库里的「允许写入」开关，或配置文件 databases[].allow_write）。",
                )
        conn = self._conn()
        try:
            conn.connect()
            if level == "read":
                conn.set_readonly_session()
            result = conn.query(sql, max_rows=max_rows or self.max_rows)
        finally:
            conn.close()
        result.update({
            "source": self.name,
            "sql": sql.strip(),
            "level": level,
            "server_version": conn.server_version,
            "elapsed_ms": getattr(conn, "connect_ms", None),
            "notes": conn.notes,
        })
        return result

    def tables(self, schema: str = None) -> dict:
        db = schema or self.profile.get("database") or ""
        if db:
            b = _bracket(db)
            sql = (
                f"SELECT s.name AS [schema], t.name AS [table], "
                f"SUM(CASE WHEN p.index_id IN (0,1) THEN p.rows ELSE 0 END) AS [rows_est], "
                f"CAST(SUM(p.reserved_page_count) * 8 AS BIGINT) AS [kb] "
                f"FROM {b}.sys.tables t "
                f"JOIN {b}.sys.schemas s ON s.schema_id = t.schema_id "
                f"LEFT JOIN {b}.sys.partitions p ON p.object_id = t.object_id "
                f"GROUP BY s.name, t.name ORDER BY s.name, t.name"
            )
        else:
            sql = ("SELECT name AS [db], state_desc AS [state] "
                   "FROM sys.databases ORDER BY name")
        return self.run(sql, max_rows=max(self.max_rows, 1000))

    def schema(self, table: str, schema: str = None) -> dict:
        if not table:
            raise DBError("bad_request", "必须指定表名")
        db = schema or self.profile.get("database") or ""
        prefix = f"{_bracket(db)}." if db else ""
        # 表名可带 schema：[dbo].[foo] / dbo.foo / foo
        parts = [p.strip() for p in str(table).split(".") if p.strip()]
        if len(parts) > 2:
            raise DBError("bad_request", "表名最多 库.表 / schema.表 两段")
        if len(parts) == 2:
            table_schema, table_only = parts
        else:
            table_schema, table_only = "dbo", parts[0]
        where = f"TABLE_NAME = {_nstr(table_only)}"
        if table_schema:
            where += f" AND TABLE_SCHEMA = {_nstr(table_schema)}"
        cols_sql = (
            f"SELECT COLUMN_NAME AS [column], DATA_TYPE AS [type], "
            f"CHARACTER_MAXIMUM_LENGTH AS [len], NUMERIC_PRECISION AS [precision], "
            f"NUMERIC_SCALE AS [scale], IS_NULLABLE AS [nullable], "
            f"COLUMN_DEFAULT AS [default] "
            f"FROM {prefix}INFORMATION_SCHEMA.COLUMNS WHERE {where} "
            f"ORDER BY ORDINAL_POSITION"
        )
        full = ".".join([_bracket(p) for p in
                         ([db, table_schema, table_only] if db
                          else [table_schema, table_only])])
        idx_sql = (
            f"SELECT i.name AS [index], "
            f"CASE WHEN i.is_primary_key = 1 THEN 0 ELSE 1 END AS [non_unique], "
            f"STUFF((SELECT N',' + c.name FROM {prefix}sys.index_columns ic "
            f"JOIN {prefix}sys.columns c ON c.object_id = ic.object_id "
            f"AND c.column_id = ic.column_id "
            f"WHERE ic.object_id = i.object_id AND ic.index_id = i.index_id "
            f"ORDER BY ic.key_ordinal FOR XML PATH(N'')), 1, 1, N'') AS [columns] "
            f"FROM {prefix}sys.indexes i "
            f"WHERE i.object_id = OBJECT_ID({_nstr(full)}) AND i.name IS NOT NULL"
        )
        out = {"table": table, "database": db}
        out["columns"] = self.run(cols_sql, max_rows=1000)
        out["indexes"] = self.run(idx_sql, max_rows=200)
        return out

    def test(self) -> dict:
        """连通性自检：身份 + 库列表 + 服务器版本。"""
        info = {"source": self.name, "dsn": dsn_summary(self.profile)}
        conn = self._conn()
        try:
            conn.connect()
            info["server_version"] = conn.server_version
            info["auth_plugin"] = "sql_auth"
            info["connection_id"] = conn.spid
            info["notes"] = conn.notes
            r = conn.query("SELECT @@SERVERNAME AS [server], SUSER_SNAME() AS [who], "
                           "DB_NAME() AS [db], @@VERSION AS [ver]")
            info["identity"] = r["rows"][0] if r["rows"] else None
            try:
                dbs = conn.query("SELECT name FROM sys.databases "
                                 "ORDER BY name", max_rows=200)
                info["databases"] = [row[0] for row in dbs["rows"]]
            except DBError:
                info["databases"] = []
            info["ok"] = True
        finally:
            conn.close()
        return info
