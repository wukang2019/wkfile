#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""纯标准库的自签证书生成 —— 不装任何东西，也不依赖 openssl

为什么手写
----------
需求是"纯 IP 的自签证书"，在 Windows 服务器上一键生成。可选路径只有三条：

1. 依赖 openssl.exe —— 裸 Windows Server 不一定有，也不该让用户去装。
2. 单键 PowerShell `New-SelfSignedCertificate` + `Export-PfxCertificate` ——
   拿到的是 PKCS#12（.pfx），而 Python 标准库**没有**解析 PKCS#12 的能力，
   转 PEM 又得回去找 openssl。死路。
3. pip 装 cryptography —— 直接违背"单文件绿色版、服务器上不装 Python 依赖"的初衷。

所以只剩自己按 RFC 5280 / RFC 8017 拼 DER。这个文件只做这一件事。

为什么用 RSA 而不是 ECDSA
-------------------------
ECDSA 签名要一个一次性随机数 k，**k 用错（重复/可预测）会直接泄露私钥**，
而这类 bug 在外面看不出来。RSA 的 PKCS#1 v1.5 签名是**确定性**的：
不引入随机数，也就没有这一整类风险。代价是生成密钥慢几秒（一次性）。

生成出来的东西靠三条硬验证兜底（见 .build/features_e2e.py 的 O 节）：
  1. `ssl.SSLContext.load_cert_chain()` 能加载；
  2. 真起一次 TLS 握手能成功；
  3. 把它当信任锚做 `CERT_REQUIRED` 校验能通过 —— 这一步证明**签名本身是对的**，
     而不只是"格式能读"。

用法
----
    from wkfile_selfcert import generate_pair, write_pair

    cert_pem, key_pem = generate_pair(cn="wkfile", ips=["203.0.113.7"])
    write_pair("srv.crt", "srv.key", cn="wkfile", ips=["203.0.113.7"])
"""

from __future__ import annotations

import base64
import datetime
import hashlib
import ipaddress
import math
import os
import secrets

__all__ = ["generate_pair", "write_pair", "generate_rsa_key", "DEFAULT_DAYS"]


# ---------------------------------------------------------------------------
# 小素数筛（先过一遍，省掉大部分 Miller-Rabin）
# ---------------------------------------------------------------------------

def _small_primes(limit: int = 6000):
    sieve = bytearray([1]) * limit
    sieve[0:2] = b"\x00\x00"
    for i in range(2, int(limit ** 0.5) + 1):
        if sieve[i]:
            sieve[i * i::i] = bytearray(len(sieve[i * i::i]))
    return [i for i in range(limit) if sieve[i]]


_SMALL_PRIMES = _small_primes()


# ---------------------------------------------------------------------------
# ASN.1 DER 基本编码
# ---------------------------------------------------------------------------

def _der_len(n: int) -> bytes:
    if n < 0x80:
        return bytes([n])
    body = n.to_bytes((n.bit_length() + 7) // 8, "big")
    return bytes([0x80 | len(body)]) + body


def _der(tag: int, body: bytes) -> bytes:
    return bytes([tag]) + _der_len(len(body)) + body


def _der_int(value: int) -> bytes:
    if value == 0:
        body = b"\x00"
    else:
        body = value.to_bytes((value.bit_length() + 7) // 8, "big")
        if body[0] & 0x80:
            body = b"\x00" + body          # DER INTEGER 是带符号的，最高位为 1 要补 0
    return _der(0x02, body)


def _der_seq(*items: bytes) -> bytes:
    return _der(0x30, b"".join(items))


def _der_set(*items: bytes) -> bytes:
    return _der(0x31, b"".join(items))


def _der_null() -> bytes:
    return b"\x05\x00"


def _der_bool(v: bool) -> bytes:
    return _der(0x01, b"\xff" if v else b"\x00")


def _der_octet(data: bytes) -> bytes:
    return _der(0x04, data)


def _der_utf8(s: str) -> bytes:
    return _der(0x0c, s.encode("utf-8"))


def _der_bitstring(data: bytes, unused: int = 0) -> bytes:
    return _der(0x03, bytes([unused]) + data)


def _der_ctx(n: int, body: bytes, constructed: bool = True) -> bytes:
    return _der((0xA0 if constructed else 0x80) + n, body)


def _der_oid(dotted: str) -> bytes:
    parts = [int(x) for x in dotted.split(".")]
    body = bytearray([parts[0] * 40 + parts[1]])
    for p in parts[2:]:
        if p < 0x80:
            body.append(p)
            continue
        chunk = []
        while p:
            chunk.append(p & 0x7F)
            p >>= 7
        chunk.reverse()
        body.extend(c | 0x80 for c in chunk[:-1])
        body.append(chunk[-1])
    return _der(0x06, bytes(body))


def _der_utctime(dt: datetime.datetime) -> bytes:
    # UTCTime 只覆盖 1950-2049；超出范围必须换 GeneralizedTime
    if 1950 <= dt.year <= 2049:
        return _der(0x17, dt.strftime("%y%m%d%H%M%SZ").encode("ascii"))
    return _der(0x18, dt.strftime("%Y%m%d%H%M%SZ").encode("ascii"))


def _der_key_usage(positions) -> bytes:
    """keyUsage 的 extnValue（一个 BIT STRING）。"""
    highest = max(positions)
    buf = bytearray(highest // 8 + 1)
    for p in positions:
        buf[p // 8] |= 1 << (7 - (p % 8))
    unused = len(buf) * 8 - (highest + 1)
    return _der(0x03, bytes([unused]) + bytes(buf))


# ---------------------------------------------------------------------------
# OID
# ---------------------------------------------------------------------------

OID_CN = "2.5.4.3"
OID_RSA_ENCRYPTION = "1.2.840.113549.1.1.1"
OID_SHA256_WITH_RSA = "1.2.840.113549.1.1.11"
OID_SHA256 = "2.16.840.1.101.3.4.2.1"
OID_EXT_BASIC_CONSTRAINTS = "2.5.29.19"
OID_EXT_KEY_USAGE = "2.5.29.15"
OID_EXT_SUBJECT_ALT_NAME = "2.5.29.17"
OID_EXT_SUBJECT_KEY_ID = "2.5.29.14"


# ---------------------------------------------------------------------------
# RSA 密钥生成与签名
# ---------------------------------------------------------------------------

def _is_probable_prime(n: int, rounds: int = 24) -> bool:
    if n < 2:
        return False
    for p in _SMALL_PRIMES:
        if n % p == 0:
            return n == p
    d = n - 1
    r = 0
    while d % 2 == 0:
        d //= 2
        r += 1
    for _ in range(rounds):
        a = secrets.randbelow(n - 3) + 2
        x = pow(a, d, n)
        if x == 1 or x == n - 1:
            continue
        for _ in range(r - 1):
            x = x * x % n
            if x == n - 1:
                break
        else:
            return False
    return True


def _gen_prime(bits: int) -> int:
    while True:
        cand = secrets.randbits(bits) | (1 << (bits - 1)) | 1   # 保证位长、保证奇数
        if any(cand % p == 0 for p in _SMALL_PRIMES):
            continue
        if _is_probable_prime(cand):
            return cand


def generate_rsa_key(bits: int = 2048, exponent: int = 65537) -> dict:
    """生成 RSA 密钥。bits=2048 时典型耗时几秒（一次性操作）。"""
    if bits < 2048:
        raise ValueError("位长至少 2048")
    if bits % 8:
        raise ValueError("位长必须是 8 的倍数")
    half = bits // 2
    while True:
        p = _gen_prime(half)
        q = _gen_prime(bits - half)
        if p == q:
            continue
        n = p * q
        if n.bit_length() != bits:
            continue
        phi = (p - 1) * (q - 1)
        if math.gcd(exponent, phi) != 1:
            continue
        d = pow(exponent, -1, phi)
        return {"n": n, "e": exponent, "d": d, "p": p, "q": q}


def _rsa_public_der(k: dict) -> bytes:
    return _der_seq(_der_int(k["n"]), _der_int(k["e"]))


def _spki(k: dict) -> bytes:
    return _der_seq(_der_seq(_der_oid(OID_RSA_ENCRYPTION), _der_null()),
                    _der_bitstring(_rsa_public_der(k)))


def _pkcs1_private(k: dict) -> bytes:
    dp = k["d"] % (k["p"] - 1)
    dq = k["d"] % (k["q"] - 1)
    qinv = pow(k["q"], -1, k["p"])
    return _der_seq(
        _der_int(0), _der_int(k["n"]), _der_int(k["e"]), _der_int(k["d"]),
        _der_int(k["p"]), _der_int(k["q"]), _der_int(dp), _der_int(dq), _der_int(qinv),
    )


def _sign_sha256(k: dict, data: bytes) -> bytes:
    """RSASSA-PKCS1-v1_5 + SHA-256，确定性签名。"""
    digest_info = _der_seq(
        _der_seq(_der_oid(OID_SHA256), _der_null()),
        _der_octet(hashlib.sha256(data).digest()),
    )
    klen = (k["n"].bit_length() + 7) // 8
    pad = klen - len(digest_info) - 3
    if pad < 8:
        raise ValueError("密钥太短，无法容纳 SHA-256 签名")
    em = b"\x00\x01" + b"\xff" * pad + b"\x00" + digest_info
    return pow(int.from_bytes(em, "big"), k["d"], k["n"]).to_bytes(klen, "big")


# ---------------------------------------------------------------------------
# X.509 组装
# ---------------------------------------------------------------------------

def _name(cn: str) -> bytes:
    # CN 用 UTF8String（RFC 5280 里 DirectoryString 允许），中文主机名也能写
    return _der_seq(_der_set(_der_seq(_der_oid(OID_CN), _der_utf8(cn))))


def _extension(oid: str, critical: bool, value_der: bytes) -> bytes:
    items = [_der_oid(oid)]
    if critical:
        items.append(_der_bool(True))
    items.append(_der_octet(value_der))
    return _der_seq(*items)


def _subject_alt_name(ips, dns) -> bytes:
    items = []
    for name in dns:
        items.append(_der_ctx(2, name.encode("ascii"), constructed=False))
    for ip in ips:
        items.append(_der_ctx(7, ipaddress.ip_address(ip).packed, constructed=False))
    return _der_seq(*items)


DEFAULT_DAYS = 3650
_MIN_DAYS = 1


def generate_pair(cn: str = "wkfile", ips=(), dns=(), days: int = DEFAULT_DAYS,
                  bits: int = 2048) -> tuple:
    """生成 (证书 PEM, 私钥 PEM)。

    ips  必须是字面量 IP（会做合法性校验）—— 纯 IP 场景就靠它，
         少了 SAN 里的 IP 条目，客户端按 IP 连接时一定校验失败。
    dns  可选的域名，可以和 ips 一起给。
    """
    cn = (cn or "wkfile").strip() or "wkfile"
    ips = [str(x).strip() for x in (ips or []) if str(x).strip()]
    dns = [str(x).strip() for x in (dns or []) if str(x).strip()]
    for ip in ips:
        ipaddress.ip_address(ip)          # 非法就抛 ValueError，别写出个没用的证书
    if not ips and not dns:
        raise ValueError("至少要有一个 IP 或域名，否则证书校验必定失败")
    days = max(_MIN_DAYS, int(days))

    key = generate_rsa_key(bits)
    now = datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0)
    not_before = now - datetime.timedelta(minutes=5)   # 容忍客户端时钟稍慢
    not_after = now + datetime.timedelta(days=days)
    serial = secrets.randbits(64) | 1
    name = _name(cn)

    ski = hashlib.sha1(_rsa_public_der(key)).digest()
    extensions = _der_seq(
        _extension(OID_EXT_BASIC_CONSTRAINTS, True, _der_seq(_der_bool(True))),
        # digitalSignature(0) + keyCertSign(5)：既是自己的信任锚，也是叶子证书
        _extension(OID_EXT_KEY_USAGE, True, _der_key_usage([0, 5])),
        _extension(OID_EXT_SUBJECT_KEY_ID, False, _der_octet(ski)),
        _extension(OID_EXT_SUBJECT_ALT_NAME, False, _subject_alt_name(ips, dns)),
    )

    tbs = _der_seq(
        _der_ctx(0, _der_int(2)),                      # version = v3
        _der_int(serial),
        _der_seq(_der_oid(OID_SHA256_WITH_RSA), _der_null()),
        name,                                          # issuer（自签：与 subject 相同）
        _der_seq(_der_utctime(not_before), _der_utctime(not_after)),
        name,
        _spki(key),
        _der_ctx(3, extensions),
    )
    cert = _der_seq(tbs,
                    _der_seq(_der_oid(OID_SHA256_WITH_RSA), _der_null()),
                    _der_bitstring(_sign_sha256(key, tbs)))
    return _pem("CERTIFICATE", cert), _pem("RSA PRIVATE KEY", _pkcs1_private(key))


def _pem(label: str, der: bytes) -> str:
    b64 = base64.b64encode(der).decode("ascii")
    body = "\n".join(b64[i:i + 64] for i in range(0, len(b64), 64))
    return "-----BEGIN %s-----\n%s\n-----END %s-----\n" % (label, body, label)


def write_pair(cert_path: str, key_path: str, *, cn: str = "wkfile", ips=(),
               dns=(), days: int = DEFAULT_DAYS, bits: int = 2048) -> tuple:
    """生成并落盘，返回 (证书路径, 私钥路径)。"""
    cert_pem, key_pem = generate_pair(cn=cn, ips=ips, dns=dns, days=days, bits=bits)
    for path, text in ((cert_path, cert_pem), (key_path, key_pem)):
        parent = os.path.dirname(os.path.abspath(path))
        if parent:
            os.makedirs(parent, exist_ok=True)
        with open(path, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(text)
    try:
        os.chmod(key_path, 0o600)          # 私钥尽量别让所有人可读（Windows 上是尽力而为）
    except OSError:
        pass
    return os.path.abspath(cert_path), os.path.abspath(key_path)


if __name__ == "__main__":  # 便于手工试：python wkfile_selfcert.py 127.0.0.1
    import sys
    import time as _time

    _ips = sys.argv[1:] or ["127.0.0.1"]
    _t0 = _time.time()
    c, k = write_pair(os.path.join(os.getcwd(), "selfcert.crt"),
                      os.path.join(os.getcwd(), "selfcert.key"),
                      cn="wkfile-selfcert", ips=_ips)
    print("生成完成，用时 %.1f 秒" % (_time.time() - _t0))
    print("  证书：%s" % c)
    print("  私钥：%s" % k)
