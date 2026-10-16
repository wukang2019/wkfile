"""wkfile_sqlsrv 协议层单测 —— 不需要真实 SQL Server。

覆盖：密码混淆、prelogin TLV 编解码、LOGIN7 布局（94 字节固定头 +
USHORT 偏移对）、ALL_HEADERS、包切分、token 流解析（COLMETADATA/
ROW/NBCROW/DONE/ERROR/ENVCHANGE）、SQL 分类器、超时夹取。

运行：python tests\\sqlsrv_proto_test.py
"""

import os
import struct
import sys

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(BASE, "server"))

import wkfile_sqlsrv as srv  # noqa: E402

PASS = 0
FAIL = 0


def check(cond, label, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
    else:
        FAIL += 1
        print(f"[FAIL] {label}  {detail}")


def expect_dberror(code, fn, label):
    global PASS, FAIL
    try:
        fn()
    except srv.DBError as exc:
        if exc.code == code:
            PASS += 1
        else:
            FAIL += 1
            print(f"[FAIL] {label}  错误码应为 {code}，实际 {exc.code}")
        return
    FAIL += 1
    print(f"[FAIL] {label}  应当抛出 DBError({code})，但没有")


# ---------------------------------------------------------------------------
# 1. 密码混淆：半字节交换后异或 0xA5（与 FreeTDS tds7_crypt_pass 一致）
# ---------------------------------------------------------------------------
check(srv.obfuscate_password(b"\x00") == b"\xa5", "obfuscate 0x00")
check(srv.obfuscate_password(b"P") == b"\xa0", "obfuscate 'P'")
# 's' = 0x73 -> swap -> 0x37 -> ^0xA5 -> 0x92
check(srv.obfuscate_password(b"s") == b"\x92", "obfuscate 's'")
check(srv.obfuscate_password(b"") == b"", "obfuscate 空")
# 双向：套两次不还原是正常的（swap 与 xor 复合不自反），
# 但解混淆 = swap(x) ^ 0x5A（FreeTDS 的解密路径），据此交叉验证：
def unobfuscate(x):
    return bytes(((b & 0x0F) << 4 | (b & 0xF0) >> 4) ^ 0x5A for b in x)


check(unobfuscate(srv.obfuscate_password(b"secret1!")) == b"secret1!",
      "obfuscate 可逆性")
pw16 = "密码P@ss".encode("utf-16-le")
check(unobfuscate(srv.obfuscate_password(pw16)) == pw16, "obfuscate utf16 可逆")

# ---------------------------------------------------------------------------
# 2. prelogin TLV 编解码
# ---------------------------------------------------------------------------
pl = srv.build_prelogin()
# 目录：VERSION(0x00) off=11 len=6；ENCRYPTION(0x01) off=17 len=1；0xFF 终止
# offset 是相对 payload 起点的绝对偏移（目录 2 项 10B + 终止符 1B = 11）
# VERSION 数据 = UL_VERSION(4B 大端) + US_SUBBUILD(2B 大端)（[MS-TDS] 2.2.6.5）
check(pl == b"\x00\x00\x0b\x00\x06" + b"\x01\x00\x11\x00\x01" + b"\xff"
      + struct.pack(">IH", 0x74000004, 0) + b"\x01",
      "prelogin 完整字节（绝对偏移）", pl.hex())
check(pl[-7:] == b"\x74\x00\x00\x04\x00\x00\x01", "prelogin 版本大端+子构建号",
      pl[-7:].hex())

# 服务端风格响应：VERSION(6B 大端) + ENCRYPTION + TERMINATOR
resp = (b"\x00\x00\x0b\x00\x06" + b"\x01\x00\x11\x00\x01" + b"\xff"
        + b"\x74\x00\x00\x04\x00\x00" + b"\x01")
opts = srv.parse_prelogin(resp)
check(opts.get(0x01) == b"\x01", "parse_prelogin ENCRYPTION=ON")
check(opts.get(0x00) == b"\x74\x00\x00\x04\x00\x00", "parse_prelogin VERSION 6 字节")

# 真实抓包风格：SQL Server 典型回复（VERSION/ENCRYPTION/INSTOPT/MARS 四项，
# 目录 4×5+1=21B，offset 从 0x15=21 起步 —— 绝对偏移的铁证）
resp3 = (b"\x00\x00\x15\x00\x06"       # VERSION  off=0x15 len=6
         + b"\x01\x00\x1b\x00\x01"     # ENCRYPT  off=0x1b len=1
         + b"\x02\x00\x1c\x00\x01"     # INSTOPT  off=0x1c len=1
         + b"\x04\x00\x1d\x00\x01"     # MARS     off=0x1d len=1
         + b"\xff"
         + b"\x0f\x00\x05\x04\x00\x00" # SQL 2019 (15.0.1105) + subbuild
         + b"\x01" + b"\x00" + b"\x00")
opts3 = srv.parse_prelogin(resp3)
check(opts3.get(0x00) == b"\x0f\x00\x05\x04\x00\x00", "真实抓包 VERSION")
check(opts3.get(0x01) == b"\x01", "真实抓包 ENCRYPTION")
check(opts3.get(0x02) == b"\x00", "真实抓包 INSTOPT=0（实例匹配）")
check(opts3.get(0x04) == b"\x00", "真实抓包 MARS=off")
# 目录项乱序 + 多选项（offset 仍指数据区绝对位置）
resp2 = (b"\x00\x00\x0b\x00\x04" + b"\x03\x00\x0f\x00\x02" + b"\xff"
         + b"\x73\x0b\x00\x03" + b"\x00\x00")
opts2 = srv.parse_prelogin(resp2)
check(opts2.get(0x00) == b"\x73\x0b\x00\x03", "parse_prelogin 乱序 VERSION")
check(opts2.get(0x03) == b"\x00\x00", "parse_prelogin 第三个选项")
expect_dberror("protocol", lambda: srv.parse_prelogin(b"\x00\x00\x00"),
               "parse_prelogin 残缺")
# offset 越界保护（真机曾触发的场景：offset 基准理解错导致读越界）
expect_dberror("protocol",
               lambda: srv.parse_prelogin(b"\x00\x00\xff\xff\x06\xff"),
               "parse_prelogin 选项越界")

# ---------------------------------------------------------------------------
# 3. LOGIN7 布局（固定头 94 字节 + 字符串区）
# ---------------------------------------------------------------------------
rec = srv.build_login7(host_name="wkbox", server_name="sqlsrv.example.com",
                       user="sa", password="P@ssw0rd!", app_name="wkfile",
                       library="wkfile", database="master")
data_len = sum(len(s) for s in [
    "wkbox".encode("utf-16-le"), "sa".encode("utf-16-le"),
    srv.obfuscate_password("P@ssw0rd!".encode("utf-16-le")),
    "wkfile".encode("utf-16-le"), "sqlsrv.example.com".encode("utf-16-le"),
    b"", "wkfile".encode("utf-16-le"), b"", "master".encode("utf-16-le")])
check(len(rec) == 94 + data_len, "LOGIN7 总长", f"{len(rec)} vs {94 + data_len}")
check(struct.unpack_from("<I", rec, 0)[0] == len(rec), "LOGIN7 Length 字段")
# TDSVersion 是 LOGIN7 里唯一的大端字段（线上字节 74 00 00 04）
check(struct.unpack_from(">I", rec, 4)[0] == 0x74000004, "LOGIN7 TDSVersion 大端")
check(rec[4:8] == b"\x74\x00\x00\x04", "LOGIN7 TDSVersion 字节序")
check(struct.unpack_from("<I", rec, 8)[0] == 4096, "LOGIN7 PacketSize")
check(rec[24] == 0xF0, "LOGIN7 OptionFlags1", hex(rec[24]))
check(rec[27] == 0x08, "LOGIN7 OptionFlags3", hex(rec[27]))

# OffsetLength 对：USHORT 偏移（从记录头算）+ USHORT 字符数
offs = [(struct.unpack_from("<HH", rec, 36 + 4 * i)) for i in range(9)]
check(offs[0] == (94, 5), "ibHostName/cch", str(offs[0]))           # 'wkbox' 5 字符
check(offs[1] == (94 + 10, 2), "ibUserName/cch", str(offs[1]))      # 'sa' 2 字符
pw_off, pw_cch = offs[2]
check(pw_cch == 9, "cchPassword 字符数", str(pw_cch))
check(rec[pw_off:pw_off + pw_cch * 2] ==
      srv.obfuscate_password("P@ssw0rd!".encode("utf-16-le")),
      "数据区里的密码已混淆")
check(offs[3][1] == 6, "cchAppName", str(offs[3]))                  # 'wkfile'
# 空字段的偏移指向其（空）数据在数据区的位置，长度才是 0
check(offs[5][1] == 0, "ServerSPN 长度为 0", str(offs[5]))
check(offs[8][1] == 6, "cchDatabase", str(offs[8]))                 # 'master'
# ClientID：72..78 六字节零
check(rec[72:78] == b"\x00" * 6, "ClientID 六字节")
# ibSSPI 指向数据区末尾、长度 0
check(struct.unpack_from("<HH", rec, 78) == (94 + data_len, 0), "ibSSPI/cchSSPI")
check(struct.unpack_from("<HH", rec, 82) == (0, 0), "ibAtchDBFile")
check(struct.unpack_from("<HH", rec, 86) == (0, 0), "ibChangePassword")
check(struct.unpack_from("<I", rec, 90) == (0,), "cbSSPILong")
# 数据区内容抽查
check(rec[offs[0][0]:offs[0][0] + 10] == "wkbox".encode("utf-16-le"), "HostName 数据")
check(rec[offs[8][0]:offs[8][0] + 12] == "master".encode("utf-16-le"), "Database 数据")

# 空密码 / 空库名不出幺蛾子
rec2 = srv.build_login7(host_name="h", server_name="", user="u", password="")
check(struct.unpack_from("<HH", rec2, 44)[1] == 0, "空密码 cch=0")
check(struct.unpack_from("<HH", rec2, 68)[1] == 0, "空 Database cch=0")

# ---------------------------------------------------------------------------
# 4. ALL_HEADERS 黄金字节
# ---------------------------------------------------------------------------
ah = srv.build_all_headers()
check(ah == b"\x16\x00\x00\x00\x12\x00\x00\x00\x02\x00" + b"\x00" * 8
      + b"\x01\x00\x00\x00", "ALL_HEADERS 22 字节", ah.hex())

# ---------------------------------------------------------------------------
# 5. 包切分：大端长度、EOM 只在最后一包
# ---------------------------------------------------------------------------
pkts = srv.packetize(0x01, b"x" * 9000, 4096)
check(len(pkts) == (8 + 4096) + (8 + 4096) + (8 + 808), "切分总长", str(len(pkts)))
h1 = struct.unpack_from(">BBHHBB", pkts, 0)
h2 = struct.unpack_from(">BBHHBB", pkts, 8 + 4096)
h3 = struct.unpack_from(">BBHHBB", pkts, 2 * (8 + 4096))
check(h1[0] == 0x01 and h1[1] == 0x00 and h1[2] == 4104 and h1[4] == 1, "第 1 包头", str(h1))
check(h2[1] == 0x00 and h2[4] == 2, "第 2 包头（无 EOM）", str(h2))
check(h3[1] == 0x01 and h3[2] == 816 and h3[4] == 3, "第 3 包头（EOM）", str(h3))
# 空载荷也至少一包
p0 = srv.packetize(0x12, b"", 4096)
check(struct.unpack_from(">BBH", p0, 0) == (0x12, 0x01, 8), "空载荷单包")

# ---------------------------------------------------------------------------
# 6. token 流解析（内存 fill）
# ---------------------------------------------------------------------------

class ExactFill:
    """精确返回 n 字节的 fill（与 _tls_read_exact 同契约）。"""

    def __init__(self, data):
        self.data = data

    def __call__(self, n):
        if len(self.data) < n:
            raise srv.DBError("conn_closed", "EOF")
        out, self.data = self.data[:n], self.data[n:]
        return out


class MemFill:
    """一次性倒出全部数据的 fill（token 流解析够用即可）。"""

    def __init__(self, data):
        self.data = data

    def __call__(self, n):
        if not self.data:
            raise srv.DBError("conn_closed", "EOF")
        out, self.data = self.data, b""
        return out


def u16(v):
    return struct.pack("<H", v)


def u32(v):
    return struct.pack("<I", v)


def i64(v):
    return struct.pack("<q", v)


def utf16(s):
    return s.encode("utf-16-le")


def colmeta_intn_name():
    """两列：INTN(4) id、NVARCHAR(100) name。"""
    return (b"\x81" + u16(2)
            + struct.pack("<IH", 0, 0) + b"\x26\x04" + b"\x04" + utf16("id")
            + struct.pack("<IH", 0, 0) + b"\xe7" + u16(100) + b"\x00" * 5
            + b"\x08" + utf16("name"))


def done(status=0x10, rowcount=0):
    return b"\xfd" + u16(status) + u16(0) + i64(rowcount)


def parse_stream(data, **kw):
    parser = srv._ResponseParser(MemFill(data), True, [], **kw)
    return parser.process_query(200, 4 * 1024 * 1024)


# 6.1 普通结果集
stream = (colmeta_intn_name()
          + b"\xd1" + b"\x04" + struct.pack("<i", 7) + u16(10) + utf16("Alice")
          + b"\xd1" + b"\x00" + u16(6) + utf16("Bob")
          + done(0x10, 2))
res = parse_stream(stream)
check(res["columns"] == ["id", "name"], "列名", str(res["columns"]))
check(res["column_types"] == ["int", "nvarchar"], "列类型", str(res["column_types"]))
check(res["rows"] == [[7, "Alice"], [None, "Bob"]], "行数据", str(res["rows"]))
check(res["row_count"] == 2 and not res["truncated"], "行数")
check(res["affected_rows"] == 2, "affected（DONE COUNT=2）")

# 6.2 NBCROW：位图第 0 位=1 → 第 0 列 NULL
stream = (colmeta_intn_name()
          + b"\xd2" + b"\x01" + u16(6) + utf16("Bob")
          + done(0x10, 1))
res = parse_stream(stream)
check(res["rows"] == [[None, "Bob"]], "NBCROW 位图 NULL", str(res["rows"]))

# 6.3 写语句：无结果集，只有 DONE
res = parse_stream(b"\xe3\x01" + u16(10) + b"\x08" + utf16("mydb") + b"\x00"
                   + done(0x10, 5))
check(res["rows"] == [] and res["columns"] == [], "写语句无结果集")
check(res["affected_rows"] == 5, "写语句 affected=5")
check(res["insert_id"] is None, "insert_id 恒 None")

# 6.4 ERROR token → sql_error
msg = "除以零错误"
err = (b"\xaa" + struct.pack("<i", 8115) + b"\x02\x10"
       + u16(len(utf16(msg))) + utf16(msg) + b"\x00" + b"\x00" + u16(1))
try:
    parse_stream(err + done(0x02, 0))
    check(False, "ERROR token 应抛异常")
except srv.DBError as exc:
    check(exc.code == "sql_error" and "8115" in exc.message, "ERROR token",
          exc.code)

# 6.5 INFO token → notes
msg = "已更改数据库上下文"
info = (b"\xab" + struct.pack("<i", 5701) + b"\x01\x10"
        + u16(len(utf16(msg))) + utf16(msg) + b"\x00" + b"\x00" + u16(1))
notes = []
parser = srv._ResponseParser(MemFill(info + done(0x00, 0)), True, notes)
res = parser.process_query(200, 4 * 1024 * 1024)
check(len(notes) == 1 and "已更改数据库上下文" in notes[0], "INFO 进 notes",
      str(notes))

# 6.6 ENVCHANGE：包大小回调
changes = []
stream = (b"\xe3\x04" + u16(8) + b"\x04" + b"4096" + b"\x02" + b"40" + done(0x00, 0))
parser = srv._ResponseParser(MemFill(stream), True, [],
                             on_envchange=lambda k, v: changes.append((k, v)))
parser.process_query(200, 4 * 1024 * 1024)
check(changes == [("packet_size", "4096")], "ENVCHANGE 包大小", str(changes))

# 6.7 未知 ENVCHANGE 类型整块跳过
stream = (b"\xe3\x09" + u16(10) + b"\x08" + b"\x01" * 8 + b"\x00" + done(0x00, 0))
res = parse_stream(stream)
check(res["row_count"] == 0, "未知 ENVCHANGE 跳过")

# 6.8 ORDER token 跳过
stream = (b"\xa9" + u16(2) + u16(1) + u16(2) + done(0x00, 0))
res = parse_stream(stream)
check(res["row_count"] == 0, "ORDER token 跳过")

# 6.9 行数上限截断
rows = (b"\xd1" + b"\x04" + struct.pack("<i", 1) + u16(0)) * 5
stream = colmeta_intn_name() + rows + done(0x10, 5)
parser = srv._ResponseParser(MemFill(stream), True, [])
res = parser.process_query(2, 4 * 1024 * 1024)
check(len(res["rows"]) == 2 and res["truncated"], "max_rows 截断", str(len(res["rows"])))

# 6.10 各标量类型解码
def colmeta(t, meta_bytes, name):
    return (b"\x81" + u16(1) + struct.pack("<IH", 0, 0) + bytes([t]) + meta_bytes
            + bytes([len(utf16(name))]) + utf16(name))

cases = [
    # (类型, meta, 行数据, 期望值)
    (0x30, b"", b"\x07", 7),                        # tinyint
    (0x32, b"", b"\x01", True),                     # bit
    (0x34, b"", struct.pack("<h", -3), -3),         # smallint
    (0x38, b"", struct.pack("<i", 123456), 123456), # int
    (0x7F, b"", struct.pack("<q", -2**40), -2**40), # bigint
    (0x3B, b"", struct.pack("<f", 1.5), 1.5),       # real
    (0x3E, b"", struct.pack("<d", 2.25), 2.25),     # float
    (0x3C, b"", struct.pack("<iI", 0, 10000), 1),   # money：高位 dword 在前，=1.00
    (0x3C, b"", struct.pack("<iI", 0, 15000), 1.5), # money 非整
    (0x7A, b"", struct.pack("<i", -10000), -1.0),   # smallmoney
]
for t, meta, rowdata, want in cases:
    stream = colmeta(t, meta, "c") + b"\xd1" + rowdata + done(0x00, 0)
    res = parse_stream(stream)
    got = res["rows"][0][0] if res["rows"] else None
    if isinstance(want, float):
        ok = isinstance(got, (int, float)) and abs(got - want) < 1e-9
    elif isinstance(want, bool):
        ok = got is want
    else:
        ok = got == want and type(got) is type(want)
    check(ok, f"类型解码 0x{t:02X}", f"{got!r} != {want!r}")

# INTN 各宽度
for size, raw, want in [(1, b"\x05", 5), (2, struct.pack("<h", -2), -2),
                        (4, struct.pack("<i", 100), 100),
                        (8, struct.pack("<q", -9), -9)]:
    stream = (colmeta(0x26, bytes([size]), "c") + b"\xd1" + bytes([size]) + raw
              + done(0x00, 0))
    res = parse_stream(stream)
    check(res["rows"][0][0] == want, f"INTN size={size}", str(res["rows"]))

# BITN NULL
stream = colmeta(0x68, b"\x01", "c") + b"\xd1" + b"\x00" + done(0x00, 0)
check(parse_stream(stream)["rows"][0][0] is None, "BITN NULL")

# GUID：Data1(u32LE)+Data2(u16)+Data3(u16)+Data4(8B)
guid_raw = (struct.pack("<IHH", 0x1234ABCD, 0xEF01, 0x2345)
            + bytes.fromhex("6789abcdef012345"))
stream = colmeta(0x24, b"\x10", "c") + b"\xd1" + b"\x10" + guid_raw + done(0x00, 0)
check(parse_stream(stream)["rows"][0][0] == "1234ABCD-EF01-2345-6789-ABCDEF012345",
      "GUID", str(parse_stream(stream)["rows"]))

# DATE：3 字节天数（0001-01-01 起第 737060 天 ≈ 2019-06-30 之后附近，直接算）
days = 738000   # 2020-11-06 左右
stream = colmeta(0x28, b"", "c") + b"\xd1" + b"\x03" + days.to_bytes(3, "little") + done(0x00, 0)
import datetime as _dt
want = str(_dt.datetime(1, 1, 1) + _dt.timedelta(days=days))[:10]
check(parse_stream(stream)["rows"][0][0] == want, "DATE", str(parse_stream(stream)["rows"]))

# TIME scale=7：正午 12:00:00.0000000
ticks = 12 * 3600 * 10**7
stream = (colmeta(0x29, b"\x07", "c") + b"\xd1" + b"\x05"
          + ticks.to_bytes(5, "little") + done(0x00, 0))
check(parse_stream(stream)["rows"][0][0] == "12:00:00.0000000", "TIME s=7")

# DATETIME2 scale=3：时间 + 3 字节日期
stream = (colmeta(0x2A, b"\x03", "c") + b"\xd1" + b"\x07"
          + (ticks // 10**4).to_bytes(4, "little")
          + days.to_bytes(3, "little") + done(0x00, 0))
check(parse_stream(stream)["rows"][0][0] == f"{want} 12:00:00.000", "DATETIME2 s=3")

# DATETIMEOFFSET：+480 分钟
stream = (colmeta(0x2B, b"\x00", "c") + b"\xd1" + b"\x08"
          + (12 * 3600).to_bytes(3, "little")
          + days.to_bytes(3, "little") + struct.pack("<h", 480) + done(0x00, 0))
check(parse_stream(stream)["rows"][0][0] == f"{want} 12:00:00+08:00", "DATETIMEOFFSET")

# DECIMAL：符号字节 0x01=正 / 0x00=负，数字大端，长度按精度档（p≤9 → 5 字节）
stream = (colmeta(0x6A, b"\x05\x05\x02", "c") + b"\xd1" + b"\x05\x01"
          + (12345).to_bytes(4, "big") + done(0x00, 0))
check(parse_stream(stream)["rows"][0][0] == "123.45", "DECIMAL 正数",
      str(parse_stream(stream)["rows"]))
stream = (colmeta(0x6A, b"\x05\x05\x02", "c") + b"\xd1" + b"\x05\x00"
          + (12345).to_bytes(4, "big") + done(0x00, 0))
check(parse_stream(stream)["rows"][0][0] == "-123.45", "DECIMAL 负数（0x00=负）")
stream = (colmeta(0x6A, b"\x05\x09\x02", "c") + b"\xd1" + b"\x05\x01"
          + (5).to_bytes(4, "big") + done(0x00, 0))
check(parse_stream(stream)["rows"][0][0] == "0.05", "DECIMAL 补零")
stream = (colmeta(0x6A, b"\x05\x03\x00", "c") + b"\xd1" + b"\x05\x01"
          + (100).to_bytes(4, "big") + done(0x00, 0))
check(parse_stream(stream)["rows"][0][0] == "100", "DECIMAL scale=0")

# BIGVARCHAR：cp936 中文（collation lcid=0x0804）
vc = "中文".encode("cp936")
collation = struct.pack("<HHB", 0x0804, 0, 0)
stream = (colmeta(0xA7, u16(50) + collation, "c") + b"\xd1" + u16(len(vc)) + vc
          + done(0x00, 0))
check(parse_stream(stream)["rows"][0][0] == "中文", "BIGVARCHAR cp936")

# BIGVARCHAR NULL（0xFFFF）
stream = (colmeta(0xA7, u16(50) + b"\x00" * 5, "c") + b"\xd1" + b"\xff\xff"
          + done(0x00, 0))
check(parse_stream(stream)["rows"][0][0] is None, "BIGVARCHAR NULL=0xFFFF")

# BIGVARBINARY → 0x hex
stream = (colmeta(0xA5, u16(50), "c") + b"\xd1" + u16(2) + b"\xab\xcd"
          + done(0x00, 0))
check(parse_stream(stream)["rows"][0][0] == "0xabcd", "BIGVARBINARY hex")

# PLP（varchar(max)）：未知总长 + 分块（每块 4 字节长度前缀，0 结束）
chunks = u32(4) + b"abcd" + u32(2) + b"ef" + u32(0)
stream = (colmeta(0xA7, b"\xff\xff" + b"\x00" * 5, "c") + b"\xd1"
          + struct.pack("<Q", 0xFFFFFFFFFFFFFFFE) + chunks + done(0x00, 0))
check(parse_stream(stream)["rows"][0][0] == "abcdef", "PLP 分块拼接")

# PLP NULL
stream = (colmeta(0xA7, b"\xff\xff" + b"\x00" * 5, "c") + b"\xd1"
          + struct.pack("<Q", 0xFFFFFFFFFFFFFFFF) + done(0x00, 0))
check(parse_stream(stream)["rows"][0][0] is None, "PLP NULL")

# NVARCHAR(max) PLP：UTF-16 分块
chunks = u32(4) + utf16("AB") + u32(0)
stream = (colmeta(0xE7, b"\xff\xff" + b"\x00" * 5, "c") + b"\xd1"
          + struct.pack("<Q", 4) + chunks + done(0x00, 0))
check(parse_stream(stream)["rows"][0][0] == "AB", "NVARCHAR(max) PLP")

# ---------------------------------------------------------------------------
# 7. 登录响应解析
# ---------------------------------------------------------------------------
prog = "Microsoft SQL Server"
loginack = (b"\xad" + b"\x01" + struct.pack(">H", 0x0704)
            + bytes([len(prog)]) + prog.encode("latin-1")
            + struct.pack("<I", 0x0C0055D3))
env_db = b"\xe3\x01" + u16(14) + b"\x0c" + utf16("master") + b"\x00"
msg = "已更改数据库上下文"
info_tok = (b"\xab" + struct.pack("<i", 5701) + b"\x01\x10"
            + u16(len(utf16(msg))) + utf16(msg) + b"\x00" + b"\x00" + u16(1))
login_stream = env_db + info_tok + loginack

class PendingFill(MemFill):
    def __init__(self, data):
        super().__init__(data)
        self.left = len(data)

    def __call__(self, n):
        out = super().__call__(n)
        self.left -= len(out)
        return out

pf = PendingFill(login_stream)
notes = []
parser = srv._ResponseParser(pf, True, notes, pending=lambda: pf.left)
info = parser.process_login()
check(info["tds_version"] == (7, 4), "LOGINACK 版本", str(info.get("tds_version")))
check("SQL Server" in (info.get("program") or ""), "LOGINACK 程序名")
check(len(notes) == 1, "登录期 INFO 记录", str(notes))

# 登录失败：ERROR 18456
msg = "用户 'sa' 登录失败"
fail = (b"\xaa" + struct.pack("<i", 18456) + b"\x01\x14"
        + u16(len(utf16(msg))) + utf16(msg) + b"\x00" + b"\x00" + u16(1))
pf = PendingFill(fail)
parser = srv._ResponseParser(pf, True, [], pending=lambda: pf.left)
expect_dberror("access_denied", parser.process_login, "登录失败 18456")

# ---------------------------------------------------------------------------
# 8. SQL 分类器
# ---------------------------------------------------------------------------
cases = [
    ("select 1", "read"),
    ("SELECT * FROM t WHERE a=1", "read"),
    ("select * into new_t from t", "deny"),
    ("insert into t values(1)", "write"),
    ("update t set a=1", "write"),
    ("delete from t", "write"),
    ("exec usp_foo", "write"),
    ("EXECUTE usp_foo @p=1", "write"),
    ("create table t(a int)", "write"),
    ("drop table t", "write"),
    ("truncate table t", "write"),
    ("merge into t using s on 1=1", "write"),
    ("with c as (select 1) select * from c", "read"),
    ("with c as (select 1) update t set a=1", "write"),
    ("with c as (select 1) delete from t", "write"),
    ("use mydb", "read"),
    ("select 1; select 2", "deny"),
    ("select 1 -- select 2", "read"),
    ("select 1\ngo\nselect 2", "deny"),
    ("select 1 /* ; insert */", "read"),
    ("select * from t where c='a;b'", "read"),
    ("exec xp_cmdshell 'dir'", "deny"),
    ("select * from sys.objects where name='xp_cmdshell'", "read"),
    ("select 'drop table x' where 1=0", "read"),
    ("select 'it''s; not; sql' ", "read"),
    ("exec sp_executesql N'select 1'", "deny"),
    ("select * from openrowset('x','y','z')", "deny"),
    ("backup database master to disk='x'", "deny"),
    ("", "deny"),
    ("   ", "deny"),
    ("select * from [order]", "read"),
    ("select 'it''s' where a=1", "read"),
]
for sql, want in cases:
    level, kw, reason = srv.classify_sql(sql)
    check(level == want, f"classify {sql[:30]!r}", f"{level} != {want} ({reason})")

# ---------------------------------------------------------------------------
# 9. 杂项
# ---------------------------------------------------------------------------
check(srv._clamp_timeout(None, 8.0) == 8.0, "clamp None")
check(srv._clamp_timeout(0, 8.0) == 8.0, "clamp 0")
check(srv._clamp_timeout(-5, 8.0) == 8.0, "clamp 负数")
check(srv._clamp_timeout(9999, 8.0) == 300.0, "clamp 超上限")
check(srv._clamp_timeout(True, 8.0) == 8.0, "clamp bool")
check(srv._clamp_timeout("5", 8.0) == 5.0, "clamp 字符串")

check(srv.dsn_summary({}) == "sqlserver://sa@127.0.0.1:1433/", "dsn 默认值")
check(srv.dsn_summary({"host": "db.example.com", "port": 1433,
                       "user": "wk", "database": "erp"}) ==
      "sqlserver://wk@db.example.com:1433/erp", "dsn 完整")

check(srv._bracket("dbo") == "[dbo]", "bracket 合法")
expect_dberror("bad_request", lambda: srv._bracket("a b"), "bracket 空格拒绝")
expect_dberror("bad_request", lambda: srv._bracket("a];DROP"), "bracket 注入拒绝")
check(srv._nstr("it's") == "N'it''s'", "nstr 转义")

# _PacketFeeder：跨包重组
payload = b"A" * 100
wire = (struct.pack(">BBHHBB", 0x04, 0x00, 8 + 60, 0, 1, 0) + b"A" * 60
        + struct.pack(">BBHHBB", 0x04, 0x01, 8 + 40, 0, 2, 0) + b"A" * 40)
feeder = srv._PacketFeeder(ExactFill(wire))
check(feeder.fill(100) == payload, "跨包重组")
check(feeder.last_packet_info[0] == 0x04, "包类型记录")

# _conn_closed：断连报错带协议阶段（区分 prelogin / TLS / 登录 / 查询）
conn = srv.SQLServerConn("127.0.0.1", 1433, "sa", "x")
conn._stage = "login7"
e = conn._conn_closed()
check(e.code == "conn_closed", "conn_closed 错误码")
check("登录" in str(e), "报错带登录阶段")
check("关闭" in str(e), "报错带关闭文案")
conn._stage = "prelogin"
check("PRELOGIN" in str(conn._conn_closed()), "报错带 prelogin 阶段")
conn._stage = "query"
check("查询" in str(conn._conn_closed()), "报错带查询阶段")
check("TLS 关闭通知" in str(conn._conn_closed("TLS 关闭通知")), "附加说明拼接")
conn2 = srv.SQLServerConn("h", 1, "u", "p")
check(conn2._stage == "prelogin", "初始阶段为 prelogin")

# ---------------------------------------------------------------------------
# TLS MemoryBIO 泵送回环测试（真实 TLS，本地自签证书 + echo 服务端）
# v1.7.4 修复的正是这条路径：SSLObject 没有 bio_read/bio_write（那是
# pyOpenSSL 的 API），密文搬运必须操作 wrap_bio 传入的 MemoryBIO。
# ---------------------------------------------------------------------------
try:
    import ssl
    import socket
    import tempfile
    import threading
    import shutil
    import wkfile_selfcert as selfcert

    tmpdir = tempfile.mkdtemp(prefix="sqlsrv_tls_")
    cert_path = os.path.join(tmpdir, "c.pem")
    key_path = os.path.join(tmpdir, "k.pem")
    selfcert.write_pair(cert_path, key_path, cn="localhost",
                        ips=("127.0.0.1",))

    server_ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    server_ctx.load_cert_chain(cert_path, key_path)
    lsock = socket.socket()
    lsock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    lsock.bind(("127.0.0.1", 0))
    lsock.listen(1)
    tls_port = lsock.getsockname()[1]

    def _echo_server():
        try:
            c, _ = lsock.accept()
            with c:
                tls = server_ctx.wrap_socket(c, server_side=True)
                with tls:
                    while True:
                        data = tls.recv(65536)
                        if not data:
                            break
                        tls.sendall(data)
        except Exception:
            pass

    threading.Thread(target=_echo_server, daemon=True).start()

    tls_conn = srv.SQLServerConn("127.0.0.1", tls_port, "sa", "x")
    tls_conn.sock = socket.create_connection(("127.0.0.1", tls_port), 5)
    tls_conn.sock.settimeout(10)
    # 旁路 TDS 帧层（帧包装另有 packetize 单测）：握手期密文裸发、裸收
    tls_conn._flush_tls_wrapped = lambda: tls_conn._flush_outgoing()
    tls_conn._plain_send = lambda raw: tls_conn.sock.sendall(raw)
    tls_conn._plain_next_packet = lambda: (0x12, 0x01, 0,
                                           tls_conn.sock.recv(65536))
    try:
        tls_conn._handshake()
        check(True, "TLS MemoryBIO 真实握手")
        msg = b"ping-" * 20000          # 100KB：覆盖 write 分块 + 多 TLS 记录
        tls_conn._tls_send(msg)
        got = tls_conn._tls_read_exact(len(msg))
        check(got == msg, "TLS 回环 echo（100KB 分块）")
        tls_conn._tls_send(b"tiny")
        got2 = tls_conn._tls_read_exact(4)
        check(got2 == b"tiny", "TLS 回环小包")
    except Exception as exc:
        check(False, "TLS MemoryBIO 回环", f"{type(exc).__name__}: {exc}")
    finally:
        tls_conn.close()
        lsock.close()
        shutil.rmtree(tmpdir, ignore_errors=True)
except ImportError:
    print("[SKIP] wkfile_selfcert 不可用，跳过 TLS 回环测试")

print(f"\nSQL Server 驱动协议测试：{PASS} 通过，{FAIL} 失败")
sys.exit(1 if FAIL else 0)
