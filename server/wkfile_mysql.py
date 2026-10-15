#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
wkfile 数据库支持 —— 纯标准库 MySQL / MariaDB 客户端
===================================================

为什么自己写协议而不用 PyMySQL
------------------------------
wkfile 的立身之本是「零第三方依赖」：目标服务器什么都不用装、不用 pip install。
引入任何一个数据库驱动都会打破这个前提（而且 PyMySQL 在 Python 3.13 上还要
额外装 cryptography 才能连 MySQL 8 —— 依赖链更长）。

本模块只用 socket / struct / hashlib / base64 实现 MySQL wire protocol：

  * 初始握手（protocol v10）解析
  * mysql_native_password（SHA1 scramble）
  * caching_sha2_password（SHA256 scramble）
      - fast auth：服务端有缓存时直接放行
      - full auth ：**MySQL 8 首次连接的常态**。无 SSL 时必须向服务端索取
        RSA 公钥，再用 RSAES-OAEP 加密 (password XOR nonce) 回传。
        注意 OAEP 的哈希是 **SHA-1** 不是 SHA-256 —— MySQL 用的是 OpenSSL 的
        RSA_PKCS1_OAEP_PADDING 默认参数。用 SHA-256 会得到
        "Access denied ... (using password: YES)"，而密钥、nonce、流程全是对的，
        极难排查。这一条是本模块最值得记住的坑。
  * AuthSwitchRequest（服务端要求换插件，MySQL 8 常见：先报 native 再切 sha2）
  * COM_QUERY + 文本结果集解析（含类型化解码）

安全模型（三层，任何一层单独失效都不至于出事）
----------------------------------------------
  1. SQL 分类器：白名单判定只读语句，拦截 INTO OUTFILE / LOAD_FILE 等高危形态
  2. 会话级只读：连接后执行 SET SESSION TRANSACTION READ ONLY，
     由数据库自己兜底 —— 就算分类器被骗过，写入也会被 MySQL 拒绝
  3. 结果限额：行数上限 + 累计字节上限 + 查询超时，防止一个 SELECT 把服务拖死

依赖：仅标准库。
"""

from __future__ import annotations

import base64
import hashlib
import os
import re
import socket
import struct
import time

# ---------------------------------------------------------------------------
# 能力位
# ---------------------------------------------------------------------------

CLIENT_LONG_PASSWORD = 0x00000001
CLIENT_FOUND_ROWS = 0x00000002
CLIENT_LONG_FLAG = 0x00000004
CLIENT_CONNECT_WITH_DB = 0x00000008
CLIENT_LOCAL_FILES = 0x00000080
CLIENT_PROTOCOL_41 = 0x00000200
CLIENT_SSL = 0x00000800
CLIENT_TRANSACTIONS = 0x00002000
CLIENT_SECURE_CONNECTION = 0x00008000
CLIENT_MULTI_STATEMENTS = 0x00010000
CLIENT_MULTI_RESULTS = 0x00020000
CLIENT_PLUGIN_AUTH = 0x00080000
CLIENT_CONNECT_ATTRS = 0x00100000
CLIENT_PLUGIN_AUTH_LENENC_CLIENT_DATA = 0x00200000
CLIENT_DEPRECATE_EOF = 0x01000000

MAX_PACKET = 16 * 1024 * 1024

# ---------------------------------------------------------------------------
# 列类型（用于把文本协议返回的字符串还原成合适的数据类型）
# ---------------------------------------------------------------------------

FIELD_TYPE_DECIMAL = 0
FIELD_TYPE_TINY = 1
FIELD_TYPE_SHORT = 2
FIELD_TYPE_LONG = 3
FIELD_TYPE_FLOAT = 4
FIELD_TYPE_DOUBLE = 5
FIELD_TYPE_NULL = 6
FIELD_TYPE_TIMESTAMP = 7
FIELD_TYPE_LONGLONG = 8
FIELD_TYPE_INT24 = 9
FIELD_TYPE_DATE = 10
FIELD_TYPE_TIME = 11
FIELD_TYPE_DATETIME = 12
FIELD_TYPE_YEAR = 13
FIELD_TYPE_NEWDATE = 14
FIELD_TYPE_VARCHAR = 15
FIELD_TYPE_BIT = 16
FIELD_TYPE_JSON = 245
FIELD_TYPE_NEWDECIMAL = 246
FIELD_TYPE_ENUM = 247
FIELD_TYPE_SET = 248
FIELD_TYPE_TINY_BLOB = 249
FIELD_TYPE_MEDIUM_BLOB = 250
FIELD_TYPE_LONG_BLOB = 251
FIELD_TYPE_BLOB = 252
FIELD_TYPE_VAR_STRING = 253
FIELD_TYPE_STRING = 254
FIELD_TYPE_GEOMETRY = 255

_INT_TYPES = frozenset({
    FIELD_TYPE_TINY, FIELD_TYPE_SHORT, FIELD_TYPE_LONG,
    FIELD_TYPE_LONGLONG, FIELD_TYPE_INT24, FIELD_TYPE_YEAR,
})
_FLOAT_TYPES = frozenset({FIELD_TYPE_FLOAT, FIELD_TYPE_DOUBLE})
_BLOB_TYPES = frozenset({
    FIELD_TYPE_TINY_BLOB, FIELD_TYPE_MEDIUM_BLOB,
    FIELD_TYPE_LONG_BLOB, FIELD_TYPE_BLOB,
})

# 这些类型即使内容是数字也保持字符串（超过 JS/Python 安全整数范围是常态）
_KEEP_STRING_TYPES = frozenset({FIELD_TYPE_NEWDECIMAL, FIELD_TYPE_DECIMAL})

UNSIGNED_FLAG = 0x0020
NOT_NULL_FLAG = 0x0001
PRI_KEY_FLAG = 0x0002
AUTO_INCREMENT_FLAG = 0x0200


class DBError(Exception):
    """数据库层的统一异常。code 用于给模型/界面看，message 给人看。"""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message

    def __str__(self):
        return self.message


# ===========================================================================
# 一、SQL 安全分类
# ===========================================================================

_LINE_COMMENT_DASH = re.compile(r"--(?=[\s\u00a0])[^\n]*")
_LINE_COMMENT_HASH = re.compile(r"#[^\n]*")
_BLOCK_COMMENT = re.compile(r"/\*.*?\*/", re.S)
_WHITESPACE = re.compile(r"\s+")

# 只读语句的起始关键字（白名单 —— 不在里面的都按写处理）
_READ_KEYWORDS = frozenset({
    "select", "show", "desc", "describe", "explain", "table", "values",
    "help", "use", "with",
})

# 即使语句以只读关键字开头也必须拒绝的形态。
# 这些不是"写不写"的问题，而是能读写服务器文件系统或拖垮实例。
READ_FORBIDDEN = (
    ("into outfile", "SELECT ... INTO OUTFILE 会往服务器磁盘写文件，属于写操作"),
    ("into dumpfile", "SELECT ... INTO DUMPFILE 会往服务器磁盘写文件，属于写操作"),
    ("load_file", "LOAD_FILE() 能读取服务器上的任意文件，一律拒绝"),
    ("load data", "LOAD DATA 会往表里灌数据，属于写操作"),
    ("for update", "SELECT ... FOR UPDATE 会加排他锁、阻塞线上业务"),
    ("lock in share mode", "LOCK IN SHARE MODE 会加共享锁、阻塞线上业务"),
    ("get_lock", "GET_LOCK() 会占用命名锁，可能影响线上业务"),
    ("sleep(", "SLEEP() 会主动挂起连接，疑似探测注入"),
    ("benchmark(", "BENCHMARK() 会制造高 CPU 负载"),
    ("information_schema.user_privileges", None),  # None = 用通用文案
)


def strip_sql_noise(sql: str) -> str:
    """去掉注释与字符串字面量，只留下结构。

    判定关键字前必须先做这件事，否则 `SELECT 'drop table x'` 会被当成删表，
    而 `/* x */ DROP` 又会被漏掉。
    """
    out = []
    i = 0
    n = len(sql)
    while i < n:
        ch = sql[i]
        # 字符串字面量：'...' 或 "..."（反引号是标识符，内容保留但去掉反引号）
        if ch in "'\"":
            quote = ch
            out.append(" ")
            i += 1
            while i < n:
                if sql[i] == "\\" and quote != "`":
                    i += 2
                    continue
                if sql[i] == quote:
                    if i + 1 < n and sql[i + 1] == quote:  # 连写两个引号 = 转义
                        i += 2
                        continue
                    i += 1
                    break
                i += 1
            continue
        if ch == "`":
            i += 1
            continue
        # 行注释
        if ch == "-" and i + 1 < n and sql[i + 1] == "-":
            if i + 2 >= n or sql[i + 2] in " \t\r\n":
                j = sql.find("\n", i)
                i = n if j < 0 else j + 1
                out.append(" ")
                continue
        if ch == "#":
            j = sql.find("\n", i)
            i = n if j < 0 else j + 1
            out.append(" ")
            continue
        # 块注释
        if ch == "/" and i + 1 < n and sql[i + 1] == "*":
            j = sql.find("*/", i + 2)
            i = n if j < 0 else j + 2
            out.append(" ")
            continue
        out.append(ch)
        i += 1
    return "".join(out)


def split_statements(sql: str) -> list:
    """按分号切分（已剔除注释与字面量之后的版本才可靠）。

    返回去掉空白的语句列表；末尾分号不会产生多余元素。
    """
    cleaned = strip_sql_noise(sql)
    parts = [p.strip() for p in cleaned.split(";")]
    return [p for p in parts if p]


def classify_sql(sql: str):
    """给一条 SQL 定性。

    返回 (level, keyword, reason)：
        level = "read"  只读，可以直接放行
                "write" 写操作，需要该数据源开启 allow_write
                "deny"  一律拒绝（危险形态或结构非法）
    """
    if not sql or not sql.strip():
        return "deny", "", "SQL 不能为空"

    statements = split_statements(sql)
    if not statements:
        return "deny", "", "SQL 不能为空"
    # 多语句是注入的经典载体，也从根上避免"只看第一条就放行"的漏判
    if len(statements) > 1:
        return ("deny", "", f"一次只允许执行一条 SQL（检测到 {len(statements)} 条）。"
                            f"多条语句请分开调用，或用 -- 注释掉多余的")

    head = statements[0]
    # 判定用的规范文本：小写、压缩空白
    norm = _WHITESPACE.sub(" ", head).strip().lower()

    # ---- 危险形态：先于关键字白名单判定 ----
    for pattern, why in READ_FORBIDDEN:
        if pattern in norm:
            reason = why or f"语句中包含被禁止的形态：{pattern}"
            return "deny", "", reason

    # ---- 起始关键字 ----
    m = re.match(r"[a-z_]+", norm)
    if not m:
        return "deny", "", "无法识别的 SQL 起始关键字"
    keyword = m.group(0)

    if keyword not in _READ_KEYWORDS:
        return "write", keyword, f"{keyword.upper()} 属于写操作"

    if keyword == "with":
        # CTE 可能是 WITH ... SELECT（只读），也可能是 MySQL 8 的
        # WITH ... UPDATE / DELETE（写）。按里面的实际动作定性。
        body = norm[len("with"):]
        if re.search(r"\b(update|delete|insert|replace)\b", body):
            return "write", "with", "WITH 子句里包含写操作"
        if not re.search(r"\bselect\b", body):
            return "deny", "with", "WITH 子句里没有 SELECT，无法确认是只读查询"

    if keyword == "use":
        # USE 只切库，不改变数据，但会改变后续语句的作用域，单独标注
        return "read", keyword, "切换当前数据库"

    if keyword == "explain":
        # EXPLAIN ANALYZE 在 MySQL 8 会真正执行语句 —— 可能是 UPDATE
        if "explain analyze" in norm:
            tail = norm.split("analyze", 1)[1]
            inner = re.match(r"\s*([a-z_]+)", tail)
            if inner and inner.group(1) not in ("select", "with", "table"):
                return "write", "explain", "EXPLAIN ANALYZE 会真正执行该语句"

    return "read", keyword, "只读查询"


# ===========================================================================
# 二、协议底层
# ===========================================================================

def _xor(a: bytes, b: bytes) -> bytes:
    return bytes(x ^ y for x, y in zip(a, b))


def _mgf1(seed: bytes, length: int, md=hashlib.sha1) -> bytes:
    out = b""
    counter = 0
    while len(out) < length:
        out += md(seed + struct.pack(">I", counter)).digest()
        counter += 1
    return out[:length]


def rsa_oaep_encrypt(n: int, e: int, message: bytes, md=hashlib.sha1) -> bytes:
    """RSAES-OAEP 加密。md 默认 SHA-1 —— 见模块头注释，这不是可选参数。"""
    hlen = md().digest_size
    k = (n.bit_length() + 7) // 8
    if len(message) > k - 2 * hlen - 2:
        raise DBError("crypto", "待加密内容超出 RSA 密钥长度")
    lhash = md(b"").digest()
    ps = b"\x00" * (k - len(message) - 2 * hlen - 2)
    db = lhash + ps + b"\x01" + message
    seed = os.urandom(hlen)
    masked_db = _xor(db, _mgf1(seed, k - hlen - 1, md))
    masked_seed = _xor(seed, _mgf1(masked_db, hlen, md))
    em = b"\x00" + masked_seed + masked_db
    return pow(int.from_bytes(em, "big"), e, n).to_bytes(k, "big")


def _der_tlv(data: bytes, pos: int):
    """返回 (tag, content, content_start, next_pos)。"""
    tag = data[pos]
    pos += 1
    length = data[pos]
    pos += 1
    if length & 0x80:
        nbytes = length & 0x7F
        length = int.from_bytes(data[pos:pos + nbytes], "big")
        pos += nbytes
    return tag, data[pos:pos + length], pos, pos + length


def pem_to_der(pem: str) -> bytes:
    lines = []
    inside = False
    for line in pem.splitlines():
        line = line.strip()
        if line.startswith("-----BEGIN"):
            inside = True
            continue
        if line.startswith("-----END"):
            break
        if inside and line:
            lines.append(line)
    if not lines:
        raise DBError("crypto", "服务端下发的公钥不是合法 PEM")
    return base64.b64decode("".join(lines))


def rsa_public_numbers(pem: str):
    """从 PEM 公钥取出 (n, e)，支持 SPKI 与 PKCS#1 两种封装。"""
    der = pem_to_der(pem)
    tag, _body, cstart, _end = _der_tlv(der, 0)
    if tag != 0x30:
        raise DBError("crypto", "公钥不是 SEQUENCE")
    tag_first, _b, _c, next1 = _der_tlv(der, cstart)
    if tag_first == 0x30:
        # SPKI: SEQ { SEQ{OID,NULL}, BIT STRING{0x00, SEQ{INT n, INT e}} }
        tag_bits, bits, _cb, _nb = _der_tlv(der, next1)
        if tag_bits != 0x03:
            raise DBError("crypto", "SPKI 里缺少 BIT STRING")
        inner = bits[1:]  # 去掉 unused-bits 计数字节
        # 进入 RSAPublicKey 这一层 SEQ —— 漏掉这层会把 SEQ 头当成 n 的开头，
        # 解析出 270bit 的假模数，最后表现为 Access denied
        tag_seq, _sc, seq_cstart, _sn = _der_tlv(inner, 0)
        if tag_seq != 0x30:
            raise DBError("crypto", "RSAPublicKey 不是 SEQUENCE")
        tag_n, n_body, _cn, next_n = _der_tlv(inner, seq_cstart)
        tag_e, e_body, _ce, _ne = _der_tlv(inner, next_n)
    else:
        tag_n, n_body, _cn, next_n = _der_tlv(der, cstart)
        tag_e, e_body, _ce, _ne = _der_tlv(der, next_n)
    if tag_n != 0x02 or tag_e != 0x02:
        raise DBError("crypto", "RSAPublicKey 结构异常")
    return int.from_bytes(n_body, "big"), int.from_bytes(e_body, "big")


def scramble_native(password: bytes, nonce: bytes) -> bytes:
    stage1 = hashlib.sha1(password).digest()
    stage2 = hashlib.sha1(stage1).digest()
    return _xor(stage1, hashlib.sha1(nonce + stage2).digest())


def scramble_sha2(password: bytes, nonce: bytes) -> bytes:
    stage1 = hashlib.sha256(password).digest()
    stage2 = hashlib.sha256(stage1).digest()
    return _xor(stage1, hashlib.sha256(stage2 + nonce).digest())


def _auth_response(plugin: str, password: bytes, nonce: bytes) -> bytes:
    if not password:
        return b""
    if plugin == "caching_sha2_password":
        return scramble_sha2(password, nonce)
    if plugin == "mysql_native_password":
        return scramble_native(password, nonce)
    if plugin == "sha256_password":
        # 老式 sha256_password：仅被 AuthSwitch 到且开了 SSL 时才可能用到
        return b"\x01" + password + b"\x00"
    raise DBError("auth_unsupported", f"不支持的认证插件：{plugin}")


def parse_server_error(payload: bytes) -> str:
    code = int.from_bytes(payload[1:3], "little")
    pos = 3
    state = ""
    if payload[pos:pos + 1] == b"#":
        state = payload[pos + 1:pos + 6].decode("ascii", "replace") + " "
        pos += 6
    return f"[{code}] {state}{payload[pos:].decode('utf-8', 'replace')}"


# ===========================================================================
# 三、MySQL 连接
# ===========================================================================

# 秒数配置的合法区间。
# 下限不是 0：settimeout(0) 是"非阻塞"，socket 会立刻抛 BlockingIOError，
# 那既不是 DBError 也不是超时，会一路冒到工具层变成"内部错误"。
# 上限是为了防手滑：配置里写个 999999，服务端线程就会被一个死连接长期占住。
MIN_TIMEOUT_SEC = 0.5
MAX_TIMEOUT_SEC = 300.0


def _clamp_timeout(value, default: float) -> float:
    """把配置里的秒数夹到安全区间。

    配置是手写 JSON，什么值都可能出现，而 `float(...)` 不报错不代表能用：

      * None / "" / "abc" / True -> 用默认值；
      * 0 或负数                -> 用默认值（负数必须挡：sock.settimeout(-1)
                                   会抛 ValueError，不是 DBError，
                                   会一路冒成工具层的"内部错误"）；
      * 超上限                  -> 夹到 MAX_TIMEOUT_SEC。
    """
    try:
        if isinstance(value, bool):      # JSON 里的 true 不是"1 秒"，是填错了
            return default
        sec = float(value)
    except (TypeError, ValueError):
        return default
    if not (sec > 0):          # 同时挡掉 0、负数与 NaN（NaN 的比较永远是 False）
        return default
    return min(sec, MAX_TIMEOUT_SEC)


def _connect_bounded(host: str, port: int, timeout: float):
    """在 timeout 秒的**总预算**内完成 DNS + TCP 连接，返回已连上的 socket。

    为什么不直接用 socket.create_connection：
    它对 getaddrinfo 返回的**每个**地址都用完整的 timeout 各试一次。
    一台既有 A 又有 AAAA 的机器，只要 IPv6 是黑洞（很常见：能解析、
    但路由不通），"配置里写 8 秒"实际要等 16 秒才报错 ——
    这正是使用者能直接感觉到的"卡很久"。

    这里改成：自己解析一次，然后每个地址只用"剩余预算"。
    局限：DNS 解析本身仍在预算之外（getaddrinfo 不可中断，纯标准库
    没法给它设超时），所以真正的卡点只可能发生在 DNS 上；
    地址级重试不再叠加，是这一版能拿到的确定收益。

    异常约定与原先一致：dns_failed / timeout / conn_failed。
    """
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
        except OSError as exc:                      # 该协议族不被支持，跳过
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


class MySQLConn:
    """一次性的 MySQL 连接（用后即关，不做池化 —— MCP 调用频率低，
    池化带来的状态残留风险大于省下的那点握手开销）。"""

    def __init__(self, host, port, user, password, database="", charset_id=45,
                 connect_timeout=8.0, query_timeout=30.0, notes=None):
        self.host = host
        self.port = int(port)
        self.user = user
        self.password = password.encode("utf-8") if isinstance(password, str) else password
        self.database = database or ""
        self.charset_id = charset_id
        self.connect_timeout = _clamp_timeout(connect_timeout, 8.0)
        self.query_timeout = _clamp_timeout(query_timeout, 30.0)
        self.notes = notes if notes is not None else []
        self.sock = None
        self.server_version = ""
        self.auth_plugin = ""
        self.server_caps = 0
        self.nonce = b""
        self.conn_id = 0
        self._seq = 0
        self.server_public_key_pem = None

    # ---- 底层 IO ----

    def _recv_exact(self, n: int) -> bytes:
        buf = b""
        while len(buf) < n:
            chunk = self.sock.recv(n - len(buf))
            if not chunk:
                raise DBError("conn_closed", "连接被数据库关闭")
            buf += chunk
        return buf

    def _read_packet(self):
        hdr = self._recv_exact(4)
        length = hdr[0] | (hdr[1] << 8) | (hdr[2] << 16)
        self._seq = hdr[3]
        return self._recv_exact(length)

    def _send_packet(self, payload: bytes, seq: int = None):
        if seq is None:
            seq = (self._seq + 1) & 0xFF
        hdr = struct.pack("<I", len(payload))[:3] + bytes([seq & 0xFF])
        self.sock.sendall(hdr + payload)
        self._seq = seq

    # ---- 连接与认证 ----

    def connect(self):
        started = time.time()
        # 解析 + 逐个地址连接，全部在 connect_timeout 这一个预算里完成
        # （不能用 socket.create_connection：它给每个地址都重发一整份 timeout）
        self.sock = _connect_bounded(self.host, self.port, self.connect_timeout)
        self.sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        self.sock.settimeout(self.query_timeout)
        try:
            self._parse_handshake(self._read_packet())
            self._send_handshake_response()
            self._handle_auth()
        except DBError:
            self.close()
            raise
        except (socket.timeout, TimeoutError):
            self.close()
            raise DBError("timeout", f"与 {self.host}:{self.port} 通信超时")
        except (EOFError, OSError) as exc:
            self.close()
            raise DBError("conn_failed", f"与数据库通信中断：{exc}")
        self.connect_ms = int((time.time() - started) * 1000)
        return self

    def _parse_handshake(self, p: bytes):
        if not p:
            raise DBError("bad_handshake", "服务端没有返回握手包")
        version = p[0]
        if version == 0xFF:
            raise DBError("handshake_error", parse_server_error(p))
        if version != 10:
            raise DBError("bad_handshake",
                          f"不支持的协议版本 {version}（只支持 MySQL 协议 v10）")
        pos = 1
        end = p.index(b"\x00", pos)
        self.server_version = p[pos:end].decode("utf-8", "replace")
        pos = end + 1
        self.conn_id = int.from_bytes(p[pos:pos + 4], "little")
        pos += 4
        nonce1 = p[pos:pos + 8]
        pos += 9  # 8 字节 scramble + 1 字节 filler
        self.server_caps = int.from_bytes(p[pos:pos + 2], "little")
        pos += 2
        nonce2 = b""
        self.auth_plugin = "mysql_native_password"
        if len(p) > pos:
            pos += 1  # character set
            pos += 2  # status flags
            self.server_caps |= int.from_bytes(p[pos:pos + 2], "little") << 16
            pos += 2
            auth_len = p[pos]
            pos += 1
            pos += 10  # reserved
            part2_len = max(13, auth_len - 8)
            nonce2 = p[pos:pos + part2_len]
            pos += part2_len
            if self.server_caps & CLIENT_PLUGIN_AUTH and len(p) > pos:
                end = p.index(b"\x00", pos) if b"\x00" in p[pos:] else len(p)
                self.auth_plugin = p[pos:end].decode("ascii", "replace") or self.auth_plugin
        self.nonce = (nonce1 + nonce2)[:20]
        self.notes.append(f"server={self.server_version} plugin={self.auth_plugin}")

    def _send_handshake_response(self):
        caps = (CLIENT_LONG_PASSWORD | CLIENT_LONG_FLAG | CLIENT_PROTOCOL_41
                | CLIENT_TRANSACTIONS | CLIENT_SECURE_CONNECTION
                | CLIENT_PLUGIN_AUTH | CLIENT_PLUGIN_AUTH_LENENC_CLIENT_DATA
                | CLIENT_MULTI_RESULTS)
        # 不声明 MULTI_STATEMENTS：多语句本身就是被拒绝的形态
        caps &= self.server_caps | (CLIENT_LONG_PASSWORD | CLIENT_LONG_FLAG)
        if self.database and (self.server_caps & CLIENT_CONNECT_WITH_DB):
            caps |= CLIENT_CONNECT_WITH_DB
        self.client_caps = caps
        auth = _auth_response(self.auth_plugin, self.password, self.nonce)
        body = struct.pack("<IIB", caps, MAX_PACKET, self.charset_id)
        body += b"\x00" * 23
        body += self.user.encode("utf-8") + b"\x00"
        body += bytes([len(auth)]) + auth
        if caps & CLIENT_CONNECT_WITH_DB:
            body += self.database.encode("utf-8") + b"\x00"
        body += self.auth_plugin.encode("ascii") + b"\x00"
        self._send_packet(body, 1)

    def _handle_auth(self):
        payload = self._read_packet()
        for _ in range(8):  # 认证往返次数上限，防止死循环
            head = payload[0] if payload else 0
            if head == 0x00:
                return
            if head == 0xFF:
                raise DBError("access_denied", parse_server_error(payload))
            if head == 0xFE and len(payload) > 1:
                # AuthSwitchRequest：MySQL 8 常见，初始声明 native、随后切到 sha2
                pos = 1
                end = payload.index(b"\x00", pos)
                plugin = payload[pos:end].decode("ascii", "replace")
                data = payload[end + 1:]
                if data.endswith(b"\x00"):
                    data = data[:-1]
                self.auth_plugin = plugin
                self.nonce = data[:20]
                self.notes.append(f"认证插件切换为 {plugin}")
                self._send_packet(_auth_response(plugin, self.password, self.nonce))
                payload = self._read_packet()
                continue
            if head == 0x01:
                data = payload[1:]
                if data == b"\x03":
                    payload = self._read_packet()  # fast auth 成功，等 OK 包
                    continue
                if data == b"\x04":
                    # full auth：向服务端索取 RSA 公钥（无 SSL 时的唯一路径）
                    self._send_packet(b"\x02")
                    payload = self._read_packet()
                    continue
                if data.startswith(b"-----BEGIN"):
                    self.server_public_key_pem = data.decode("ascii", "replace")
                    n, e = rsa_public_numbers(self.server_public_key_pem)
                    self.notes.append(f"用服务端 RSA 公钥加密密码（{n.bit_length()}bit）")
                    obfuscated = _xor(self.password + b"\x00",
                                      (self.nonce * 8)[:len(self.password) + 1])
                    self._send_packet(rsa_oaep_encrypt(n, e, obfuscated))
                    payload = self._read_packet()
                    continue
                raise DBError("auth_protocol", f"认证阶段收到未知响应：{data[:48]!r}")
            raise DBError("auth_protocol", f"认证阶段收到未知包 0x{head:02x}")
        raise DBError("auth_protocol", "认证往返次数超出上限")

    # ---- 查询 ----

    def set_readonly_session(self):
        """会话级只读 —— 让数据库自己兜底，比任何客户端检查都可靠。"""
        try:
            self.query("SET SESSION TRANSACTION READ ONLY")
        except DBError as exc:
            # 老版本（MySQL 5.6 之前）或权限受限时不致命，降级为纯客户端拦截
            self.notes.append(f"未能开启会话只读（{exc.code}），改用 SQL 分类器拦截")

    def query(self, sql: str, *, max_rows=200, max_bytes=4 * 1024 * 1024, raw=False):
        """执行一条 SQL。raw=True 表示调用方已自行做过安全判定（内部使用）。"""
        self._send_packet(b"\x03" + sql.encode("utf-8"), 0)
        payload = self._read_packet()
        if not payload:
            return {"columns": [], "rows": [], "row_count": 0, "affected_rows": None}
        if payload[0] == 0xFF:
            raise DBError("sql_error", parse_server_error(payload))
        if payload[0] == 0x00:
            affected, pos = _read_lenenc_int(payload, 1)
            insert_id, _ = _read_lenenc_int(payload, pos)
            return {"columns": [], "rows": [], "row_count": 0,
                    "affected_rows": affected, "insert_id": insert_id}
        if payload[0] == 0xFB:
            return {"columns": [], "rows": [], "row_count": 0, "local_infile": True}

        col_count, _pos = _read_lenenc_int(payload, 0)
        columns = []
        for _ in range(col_count):
            cpkt = self._read_packet()
            columns.append(_parse_column(cpkt))
        eof = self._read_packet()
        if not eof or eof[0] != 0xFE:
            raise DBError("protocol", f"列定义后应为 EOF 包，实际 0x{eof[0]:02x}")

        rows = []
        truncated = False
        total_bytes = 0
        while True:
            pkt = self._read_packet()
            if not pkt:
                break
            if pkt[0] == 0xFE and len(pkt) < 9:
                break  # 结果集结束 EOF
            if pkt[0] == 0xFF:
                raise DBError("sql_error", parse_server_error(pkt))
            if len(rows) >= max_rows:
                truncated = True
                # 还得把剩余行读干净，否则连接状态错乱（反正要关，但读干净更稳）
                continue
            row = _parse_row(pkt, columns)
            total_bytes += sum(len(str(v)) for v in row)
            rows.append(row)
            if total_bytes > max_bytes:
                truncated = True
        return {
            "columns": [c["name"] for c in columns],
            "column_types": [c["type_name"] for c in columns],
            "rows": rows,
            "row_count": len(rows),
            "affected_rows": None,
            "truncated": truncated,
        }

    def close(self):
        if not self.sock:
            return
        try:
            self._send_packet(b"\x01", 0)  # COM_QUIT
        except Exception:  # noqa: BLE001
            pass
        try:
            self.sock.close()
        except Exception:  # noqa: BLE001
            pass
        self.sock = None

    def __enter__(self):
        return self.connect()

    def __exit__(self, *exc):
        self.close()
        return False


# ---- 包解析辅助 ----

_TYPE_NAMES = {
    FIELD_TYPE_DECIMAL: "decimal", FIELD_TYPE_TINY: "tinyint", FIELD_TYPE_SHORT: "smallint",
    FIELD_TYPE_LONG: "int", FIELD_TYPE_FLOAT: "float", FIELD_TYPE_DOUBLE: "double",
    FIELD_TYPE_NULL: "null", FIELD_TYPE_TIMESTAMP: "timestamp",
    FIELD_TYPE_LONGLONG: "bigint", FIELD_TYPE_INT24: "mediumint", FIELD_TYPE_DATE: "date",
    FIELD_TYPE_TIME: "time", FIELD_TYPE_DATETIME: "datetime", FIELD_TYPE_YEAR: "year",
    FIELD_TYPE_NEWDATE: "date", FIELD_TYPE_VARCHAR: "varchar", FIELD_TYPE_BIT: "bit",
    FIELD_TYPE_JSON: "json", FIELD_TYPE_NEWDECIMAL: "decimal", FIELD_TYPE_ENUM: "enum",
    FIELD_TYPE_SET: "set", FIELD_TYPE_TINY_BLOB: "tinyblob",
    FIELD_TYPE_MEDIUM_BLOB: "mediumblob", FIELD_TYPE_LONG_BLOB: "longblob",
    FIELD_TYPE_BLOB: "blob", FIELD_TYPE_VAR_STRING: "varchar", FIELD_TYPE_STRING: "char",
    FIELD_TYPE_GEOMETRY: "geometry",
}


def _read_lenenc_int(data: bytes, pos: int):
    if pos >= len(data):
        return None, pos
    first = data[pos]
    if first < 0xFB:
        return first, pos + 1
    if first == 0xFC:
        return int.from_bytes(data[pos + 1:pos + 3], "little"), pos + 3
    if first == 0xFD:
        return int.from_bytes(data[pos + 1:pos + 4], "little"), pos + 4
    if first == 0xFE:
        return int.from_bytes(data[pos + 1:pos + 9], "little"), pos + 9
    return None, pos + 1


def _read_lenenc_str(data: bytes, pos: int):
    length, pos = _read_lenenc_int(data, pos)
    if length is None:
        return None, pos
    return data[pos:pos + length], pos + length


def _parse_column(pkt: bytes) -> dict:
    """列定义包：catalog, schema, table, org_table, name, org_name, ..."""
    pos = 0
    fields = []
    for _ in range(6):
        val, pos = _read_lenenc_str(pkt, pos)
        fields.append(val.decode("utf-8", "replace") if val else "")
    _fixed_len, pos = _read_lenenc_int(pkt, pos)
    charset = int.from_bytes(pkt[pos:pos + 2], "little")
    pos += 2
    col_len = int.from_bytes(pkt[pos:pos + 4], "little")
    pos += 4
    ftype = pkt[pos] if pos < len(pkt) else FIELD_TYPE_VAR_STRING
    pos += 1
    flags = int.from_bytes(pkt[pos:pos + 2], "little") if pos + 2 <= len(pkt) else 0
    return {
        "name": fields[4],
        "table": fields[3] or fields[2],
        "database": fields[1],
        "type": ftype,
        "type_name": _TYPE_NAMES.get(ftype, f"unknown({ftype})"),
        "charset": charset,
        "length": col_len,
        "flags": flags,
        "unsigned": bool(flags & UNSIGNED_FLAG),
        "not_null": bool(flags & NOT_NULL_FLAG),
        "primary_key": bool(flags & PRI_KEY_FLAG),
        "auto_increment": bool(flags & AUTO_INCREMENT_FLAG),
    }


def _decode_value(raw, col: dict):
    if raw is None:
        return None
    ftype = col["type"]
    text = raw.decode("utf-8", "replace") if isinstance(raw, bytes) else raw
    if ftype in _INT_TYPES:
        try:
            return int(text)
        except ValueError:
            return text
    if ftype in _FLOAT_TYPES:
        try:
            return float(text)
        except ValueError:
            return text
    if ftype in _BLOB_TYPES:
        # BLOB 里可能是文本也可能是二进制，试一次解码
        if isinstance(raw, (bytes, bytearray)) and b"\x00" in raw:
            return f"<binary {len(raw)} bytes: {base64.b64encode(bytes(raw[:48])).decode()}...>"
        return text
    if ftype in _KEEP_STRING_TYPES:
        # DECIMAL / NEWDECIMAL 精度敏感（金额、超长 ID）。只有能无损转 float
        # 的时候才转 —— 否则保持字符串，免得被科学计数法或舍入悄悄毁掉精度。
        # 例："2.5" → 2.5；"2.50" 与 "12345678901234567890.12" 保持字符串。
        try:
            as_float = float(text)
        except (TypeError, ValueError):
            return text
        return as_float if str(as_float) == text else text
    return text


def _parse_row(pkt: bytes, columns: list) -> list:
    row = []
    pos = 0
    for col in columns:
        raw, pos = _read_lenenc_str(pkt, pos)
        row.append(_decode_value(raw, col))
    return row


# ===========================================================================
# 四、面向 wkfile 的高层封装
# ===========================================================================

def dsn_summary(profile: dict) -> str:
    """给人看的连接摘要，绝不包含密码。"""
    host = profile.get("host") or "127.0.0.1"
    port = profile.get("port") or 3306
    user = profile.get("user") or "root"
    db = profile.get("database") or ""
    return f"mysql://{user}@{host}:{port}/{db}"


class MySQLSource:
    """一个已配置的 MySQL 数据源。"""

    def __init__(self, profile: dict):
        self.profile = profile or {}
        self.name = str(self.profile.get("name") or "mysql")
        self.allow_write = bool(self.profile.get("allow_write"))
        self.max_rows = max(1, min(int(self.profile.get("max_rows") or 200), 5000))
        self.timeout = _clamp_timeout(self.profile.get("timeout_sec"), 30.0)
        self.connect_timeout = _clamp_timeout(
            self.profile.get("connect_timeout_sec"), 8.0)

    def _conn(self) -> MySQLConn:
        p = self.profile
        return MySQLConn(
            host=p.get("host") or "127.0.0.1",
            port=p.get("port") or 3306,
            user=p.get("user") or "root",
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
            if level == "read" or force_readonly or not self.allow_write:
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
            sql = ("SELECT TABLE_NAME AS `table`, TABLE_TYPE AS `type`, "
                   "ENGINE AS `engine`, TABLE_ROWS AS `rows_est`, "
                   "ROUND((DATA_LENGTH+INDEX_LENGTH)/1024) AS `kb`, "
                   "TABLE_COMMENT AS `comment` "
                   "FROM information_schema.TABLES WHERE TABLE_SCHEMA = "
                   f"'{_escape_literal(db)}' ORDER BY TABLE_NAME")
        else:
            sql = ("SELECT TABLE_SCHEMA AS `db`, COUNT(*) AS `tables` "
                   "FROM information_schema.TABLES "
                   "WHERE TABLE_SCHEMA NOT IN ('mysql','information_schema',"
                   "'performance_schema','sys') GROUP BY TABLE_SCHEMA ORDER BY TABLE_SCHEMA")
        return self.run(sql, max_rows=max(self.max_rows, 1000))

    def schema(self, table: str, schema: str = None) -> dict:
        if not table:
            raise DBError("bad_request", "必须指定表名")
        db = schema or self.profile.get("database") or ""
        cols_sql = (
            "SELECT COLUMN_NAME AS `column`, COLUMN_TYPE AS `type`, "
            "IS_NULLABLE AS `nullable`, COLUMN_KEY AS `key`, "
            "COLUMN_DEFAULT AS `default`, EXTRA AS `extra`, COLUMN_COMMENT AS `comment` "
            "FROM information_schema.COLUMNS WHERE TABLE_NAME = "
            f"'{_escape_literal(table)}'"
            + (f" AND TABLE_SCHEMA = '{_escape_literal(db)}'" if db else "")
            + " ORDER BY ORDINAL_POSITION"
        )
        idx_sql = (
            "SELECT INDEX_NAME AS `index`, NON_UNIQUE AS `non_unique`, "
            "GROUP_CONCAT(COLUMN_NAME ORDER BY SEQ_IN_INDEX) AS `columns` "
            "FROM information_schema.STATISTICS WHERE TABLE_NAME = "
            f"'{_escape_literal(table)}'"
            + (f" AND TABLE_SCHEMA = '{_escape_literal(db)}'" if db else "")
            + " GROUP BY INDEX_NAME, NON_UNIQUE"
        )
        out = {"table": table, "database": db}
        out["columns"] = self.run(cols_sql, max_rows=1000)
        out["indexes"] = self.run(idx_sql, max_rows=200)
        return out

    def test(self) -> dict:
        """连通性自检 —— 同时用两条语句确认读写权限的真实状态。"""
        info = {"source": self.name, "dsn": dsn_summary(self.profile)}
        conn = self._conn()
        try:
            conn.connect()
            info["server_version"] = conn.server_version
            info["auth_plugin"] = conn.auth_plugin
            info["connection_id"] = conn.conn_id
            info["notes"] = conn.notes
            r = conn.query("SELECT DATABASE() AS db, CURRENT_USER() AS who, "
                           "VERSION() AS ver, @@read_only AS srv_read_only")
            info["identity"] = r["rows"][0] if r["rows"] else None
            try:
                dbs = conn.query("SHOW DATABASES", max_rows=200)
                info["databases"] = [row[0] for row in dbs["rows"]]
            except DBError:
                info["databases"] = []
            info["ok"] = True
        finally:
            conn.close()
        return info


def _escape_literal(value: str) -> str:
    """用于 information_schema 查询里的字符串字面量。"""
    return str(value).replace("\\", "\\\\").replace("'", "\\'")
