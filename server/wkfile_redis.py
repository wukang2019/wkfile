#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
wkfile 数据库支持 —— 纯标准库 Redis 客户端
==========================================

用 RESP2 协议直连 Redis / 兼容实现（KeyDB、Dragonfly、腾讯云/阿里云 Redis 等），
同样零第三方依赖（不用 redis-py）。

安全模型与 MySQL 侧一致，另外多两条针对 Redis 的硬规则：

  * **白名单而非黑名单**：只放行明确列出的读命令；不在读白名单里的命令一律
    按写处理（还要数据源开了 allow_write 才放行），危险命令永远拒绝。
    Redis 的命令空间太野（EVAL 能跑 Lua、CONFIG 能改持久化、MODULE 能加载 .so），
    黑名单必然漏，只有白名单是安全的。
  * **KEYS 被禁用**：它会阻塞整个实例。列 key 走 SCAN 游标，天然可限量。
  * **PUBLISH / SUBSCRIBE / MULTI 被禁用**：它们会改变连接的应答模型，
    与"一问一答"的请求-响应模式冲突，留着会卡死服务端线程。

依赖：仅标准库。
"""

from __future__ import annotations

import socket
import time

CRLF = b"\r\n"
DEFAULT_MAX_VALUE_BYTES = 256 * 1024


class RedisError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message

    def __str__(self):
        return self.message


# ===========================================================================
# 一、命令分级
# ===========================================================================

# 只读命令白名单 —— 不在这里的一律按写处理
READ_ONLY_COMMANDS = frozenset({
    # string
    "get", "mget", "strlen", "getrange", "substr", "getbit", "bitcount", "bitpos",
    # 通用
    "exists", "type", "ttl", "pttl", "expiretime", "pexpiretime", "touch",
    "scan", "sscan", "hscan", "zscan", "randomkey", "dbsize", "keys_taken_over",
    # hash
    "hget", "hmget", "hgetall", "hkeys", "hvals", "hlen", "hexists", "hstrlen",
    # list
    "llen", "lrange", "lindex", "lpos",
    # set
    "scard", "sismember", "smembers", "srandmember", "smismember", "sinter", "sunion", "sdiff",
    # zset
    "zcard", "zscore", "zmscore", "zrange", "zrangebyscore", "zrevrange",
    "zrangebylex", "zrevrangebylex", "zrank", "zrevrank", "zcount", "zlexcount",
    # hyperloglog / geo / stream
    "pfcount", "geodist", "geopos", "geohash", "geosearch", "xrange", "xrevrange",
    "xlen", "xinfo", "xpending",
    # 实例信息
    "info", "time", "lastsave", "memory", "object", "dump", "command", "lolwut", "lcs",
    "slowlog",
})

# 明确可写的命令 —— 需要数据源开启 allow_write
WRITE_COMMANDS = frozenset({
    "set", "setnx", "setex", "psetex", "mset", "msetnx", "append", "setrange",
    "incr", "decr", "incrby", "decrby", "incrbyfloat", "getset", "getdel", "getex",
    "del", "unlink", "expire", "pexpire", "expireat", "pexpireat", "persist",
    "rename", "renamenx", "move", "copy", "restore_taken_over",
    "hset", "hmset", "hsetnx", "hdel", "hincrby", "hincrbyfloat",
    "lpush", "rpush", "lpushx", "rpushx", "lpop", "rpop", "linsert", "lset",
    "ltrim", "lrem", "rpoplpush", "lmove",
    "sadd", "srem", "spop", "smove", "sinterstore", "sunionstore", "sdiffstore",
    "zadd", "zincrby", "zrem", "zremrangebyrank", "zremrangebyscore",
    "zremrangebylex", "zpopmin", "zpopmax", "zunionstore", "zinterstore", "zdiffstore",
    "zrangestore", "bzpopmin", "bzpopmax",
    "setbit", "bitop", "bitfield", "pfadd", "pfmerge",
    "xadd", "xtrim", "xdel", "xack", "xgroup", "xclaim", "xautoclaim", "xsetid",
    "geoadd", "georadius", "geosearchstore", "pfdebug",
})

# 一律拒绝：破坏性、管理类、或会改变连接应答模型
DENY_COMMANDS = {
    "flushall": "清空整个实例的数据，不可恢复",
    "flushdb": "清空当前库的数据，不可恢复",
    "shutdown": "会关停 Redis 服务",
    "config": "能改写实例配置（含持久化与安全相关项）",
    "debug": "能触发崩溃/内存转储等调试行为",
    "slaveof": "能改变复制拓扑",
    "replicaof": "能改变复制拓扑",
    "eval": "能执行任意 Lua 脚本（等于把服务器交给调用方）",
    "evalsha": "能执行任意 Lua 脚本",
    "eval_ro": "能执行任意 Lua 脚本",
    "evalsha_ro": "能执行任意 Lua 脚本",
    "fcall": "能执行任意 Functions 脚本",
    "fcall_ro": "能执行任意 Functions 脚本",
    "function": "能加载/管理服务端函数",
    "script": "能管理脚本缓存",
    "module": "能加载本地动态库",
    "monitor": "会长期占用连接并输出全部命令流",
    "subscribe": "订阅会改变连接的应答模型，与请求-响应模式冲突",
    "unsubscribe": "订阅会改变连接的应答模型",
    "psubscribe": "订阅会改变连接的应答模型",
    "punsubscribe": "订阅会改变连接的应答模型",
    "ssubscribe": "订阅会改变连接的应答模型",
    "sunsubscribe": "订阅会改变连接的应答模型",
    "publish": "需要订阅端配合，本服务不承担消息通道",
    "spublish": "需要订阅端配合，本服务不承担消息通道",
    "psync": "复制协议内部命令",
    "sync": "复制协议内部命令",
    "replconf": "复制协议内部命令",
    "migrate": "能把 key 迁到别的实例并删除本地 key",
    "restore": "能用序列化数据覆盖 key",
    "bgsave": "会触发全量持久化，抢占磁盘与 fork",
    "bgrewriteaof": "会触发 AOF 重写，抢占磁盘与 fork",
    "save": "会阻塞实例做全量持久化",
    "cluster": "集群管理命令",
    "failover": "能触发主从切换",
    "swapdb": "能整体交换两个库的数据",
    "acl": "权限管理命令",
    "client": "能 kill 掉其他连接",
    "wait": "阻塞等待副本确认",
    "waitaof": "阻塞等待副本确认",
    "keys": "KEYS 会阻塞整个实例，请改用 win_redis_keys（基于 SCAN）",
    "multi": "事务会改变连接的应答模型",
    "exec": "事务会改变连接的应答模型",
    "discard": "事务会改变连接的应答模型",
    "watch": "事务会改变连接的应答模型",
    "unwatch": "事务会改变连接的应答模型",
    "reset": "会重置连接状态",
    "hello": "协议协商内部命令",
    "auth": "认证由本服务统一处理",
    "select": "库切换由本服务统一处理",
    "latency": "诊断命令，可能阻塞",
    "readonly": "集群模式内部命令",
    "readwrite": "集群模式内部命令",
    "asking": "集群模式内部命令",
    "xread": "可能阻塞连接",
    "xreadgroup": "会变更消费者组状态且可能阻塞",
    "blpop": "阻塞式弹出会占住连接",
    "brpop": "阻塞式弹出会占住连接",
    "brpoplpush": "阻塞式弹出会占住连接",
    "blmove": "阻塞式弹出会占住连接",
    "blmpop": "阻塞式弹出会占住连接",
    "bzmpop": "阻塞式弹出会占住连接",
    "subscribe_keyspace": "会改变连接的应答模型",
    "post": "内部命令",
    "host:": "内部命令",
}


def classify_command(name: str, args: list = None) -> tuple:
    """返回 (level, reason)：level ∈ {"read", "write", "deny"}。"""
    cmd = (name or "").strip().lower()
    if not cmd:
        return "deny", "命令不能为空"
    if cmd in DENY_COMMANDS:
        return "deny", f"{cmd.upper()} 被禁用：{DENY_COMMANDS[cmd]}"
    if cmd == "sort":
        # SORT 不带 STORE 是只读；带 STORE 会写目标 key
        for a in (args or []):
            if str(a).upper() == "STORE":
                return "write", "SORT ... STORE 会写入目标 key"
        return "read", "只读排序"
    if cmd in READ_ONLY_COMMANDS:
        return "read", "只读命令"
    if cmd in WRITE_COMMANDS:
        return "write", f"{cmd.upper()} 属于写命令"
    # 未知命令：按写处理（宁严勿松），用户可以在配置里白名单之外自行判断
    return "write", f"{cmd.upper()} 不在已知的只读命令清单里，按写命令处理"


# ===========================================================================
# 二、RESP2 协议
# ===========================================================================

def _encode_command(args) -> bytes:
    parts = [b"*" + str(len(args)).encode() + CRLF]
    for a in args:
        if isinstance(a, bytes):
            raw = a
        elif isinstance(a, bool):
            raw = b"1" if a else b"0"
        elif isinstance(a, (int, float)):
            raw = str(a).encode()
        else:
            raw = str(a).encode("utf-8")
        parts.append(b"$" + str(len(raw)).encode() + CRLF + raw + CRLF)
    return b"".join(parts)


def _to_text(raw, limit: int = DEFAULT_MAX_VALUE_BYTES):
    if raw is None:
        return None
    if isinstance(raw, bytes):
        if len(raw) > limit:
            return f"<{len(raw)} bytes, 仅显示前 {limit} 字节> " + \
                   raw[:limit].decode("utf-8", "replace")
        try:
            return raw.decode("utf-8")
        except UnicodeDecodeError:
            import base64
            return "<binary> " + base64.b64encode(raw[:64]).decode()
    return raw


# 秒数配置的合法区间，理由与 wkfile_mysql.py 里同名常量完全一致：
# 0 是"非阻塞"（立刻抛 BlockingIOError，不是超时）、负数会让 settimeout
# 直接抛 ValueError（不是 RedisError，会被当成工具层的内部错误）。
# 两个模块刻意各自持有一份，不互相 import —— 它们要能单独拷走使用。
MIN_TIMEOUT_SEC = 0.5
MAX_TIMEOUT_SEC = 300.0


def _clamp_timeout(value, default: float) -> float:
    """把配置里的秒数夹到安全区间（详见 wkfile_mysql._clamp_timeout）。"""
    try:
        if isinstance(value, bool):      # JSON 里的 true 不是"1 秒"，是填错了
            return default
        sec = float(value)
    except (TypeError, ValueError):
        return default
    if not (sec > 0):          # 同时挡掉 0、负数与 NaN
        return default
    return min(sec, MAX_TIMEOUT_SEC)


def _connect_bounded(host: str, port: int, timeout: float):
    """在 timeout 秒的总预算内完成 DNS + TCP 连接（同 wkfile_mysql 版）。

    socket.create_connection 对每个解析出的地址都用一整份 timeout，
    双栈机器上 IPv6 不通时 8 秒会变成 16 秒；这里改成共享一个预算。
    """
    started = time.monotonic()
    deadline = started + timeout
    try:
        infos = socket.getaddrinfo(host, port, 0, socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise RedisError("dns_failed", f"域名解析失败：{host}（{exc}）")
    if not infos:
        raise RedisError("dns_failed", f"域名没有解析出可用地址：{host}")

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
        raise RedisError(
            "timeout",
            f"连接 {host}:{port} 超时（预算 {timeout:g}s 已用完，"
            f"实际等了 {elapsed:.1f}s）",
        )
    raise RedisError("conn_failed", f"连接 {host}:{port} 失败：{last_exc}")


class RedisConn:
    def __init__(self, host, port, password="", username="", db=0,
                 connect_timeout=8.0, timeout=15.0, notes=None):
        self.host = host
        self.port = int(port)
        self.password = password or ""
        self.username = username or ""
        self.db = int(db or 0)
        self.connect_timeout = _clamp_timeout(connect_timeout, 8.0)
        self.timeout = _clamp_timeout(timeout, 15.0)
        self.notes = notes if notes is not None else []
        self.sock = None
        self.fp = None
        self.server_version = ""

    def connect(self):
        # 与 MySQL 侧同源：解析 + 逐个地址连接共享一个 connect_timeout 预算
        self.sock = _connect_bounded(self.host, self.port, self.connect_timeout)
        self.sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        self.sock.settimeout(self.timeout)
        self.fp = self.sock.makefile("rb")
        try:
            if self.password:
                if self.username:
                    self._raw_exec(["AUTH", self.username, self.password])
                else:
                    self._raw_exec(["AUTH", self.password])
            if self.db:
                self._raw_exec(["SELECT", str(self.db)])
            info = self._raw_exec(["INFO", "server"])
            # 注意：bulk string 解出来是 bytes，必须走 _to_text 解码。
            # 直接用 str(info) 会得到 "b'# Server\\n...'" 这种表示，
            # 于是 redis_version 永远匹配不上（曾经真的踩过）。
            for line in (_to_text(info) or "").splitlines():
                if line.startswith("redis_version:"):
                    self.server_version = line.split(":", 1)[1].strip()
                    break
        except RedisError:
            self.close()
            raise
        except (OSError, socket.timeout) as exc:
            self.close()
            raise RedisError("conn_failed", f"与 Redis 通信失败：{exc}")
        return self

    # ---- 底层 ----

    def _raw_exec(self, args):
        if not self.sock:
            raise RedisError("conn_closed", "连接尚未建立")
        try:
            self.sock.sendall(_encode_command(args))
        except OSError as exc:
            raise RedisError("conn_failed", f"发送命令失败：{exc}")
        return self._read_reply(0)

    def _read_reply(self, depth: int):
        try:
            line = self.fp.readline()
        except (socket.timeout, TimeoutError):
            raise RedisError("timeout", f"等待 Redis 响应超时（{self.timeout:g}s）")
        except OSError as exc:
            raise RedisError("conn_failed", f"读取响应失败：{exc}")
        if not line:
            raise RedisError("conn_closed", "Redis 关闭了连接")
        prefix = line[:1]
        body = line[1:-2] if line.endswith(CRLF) else line[1:]
        if prefix == b"+":
            return body.decode("utf-8", "replace")
        if prefix == b"-":
            raise RedisError("redis_error", body.decode("utf-8", "replace"))
        if prefix == b":":
            return int(body)
        if prefix == b"$":
            n = int(body)
            if n == -1:
                return None
            data = self.fp.read(n + 2)
            return data[:-2] if data.endswith(CRLF) else data
        if prefix == b"*":
            n = int(body)
            if n == -1:
                return None
            if depth > 4:
                raise RedisError("protocol", "响应嵌套过深")
            return [self._read_reply(depth + 1) for _ in range(n)]
        if prefix == b"_":  # RESP3 null
            return None
        raise RedisError("protocol", f"无法识别的响应类型：{prefix!r}")

    def execute(self, args):
        return self._raw_exec(args)

    def close(self):
        for obj in (self.fp, self.sock):
            try:
                if obj:
                    obj.close()
            except Exception:  # noqa: BLE001
                pass
        self.fp = None
        self.sock = None


# ===========================================================================
# 三、面向 wkfile 的高层封装
# ===========================================================================

def dsn_summary(profile: dict) -> str:
    host = profile.get("host") or "127.0.0.1"
    port = profile.get("port") or 6379
    db = profile.get("db") or 0
    user = profile.get("username") or ""
    who = f"{user}@" if user else ""
    return f"redis://{who}{host}:{port}/{db}"


class RedisSource:
    def __init__(self, profile: dict):
        self.profile = profile or {}
        self.name = str(self.profile.get("name") or "redis")
        self.allow_write = bool(self.profile.get("allow_write"))
        self.timeout = _clamp_timeout(self.profile.get("timeout_sec"), 15.0)
        self.connect_timeout = _clamp_timeout(
            self.profile.get("connect_timeout_sec"), 8.0)
        self.max_keys = max(1, min(int(self.profile.get("max_keys") or 200), 5000))

    def _conn(self) -> RedisConn:
        p = self.profile
        return RedisConn(
            host=p.get("host") or "127.0.0.1",
            port=p.get("port") or 6379,
            password=p.get("password") or "",
            username=p.get("username") or "",
            db=p.get("db") or 0,
            connect_timeout=self.connect_timeout,
            timeout=self.timeout,
        )

    # ---- 通用命令执行 ----

    def run(self, args, *, force_readonly=False) -> dict:
        if not args:
            raise RedisError("bad_request", "命令不能为空")
        name = str(args[0])
        level, reason = classify_command(name, args[1:])
        if level == "deny":
            raise RedisError("command_rejected", reason)
        if level == "write":
            if force_readonly:
                raise RedisError("read_only",
                                 f"{reason}。本次用的是只读 Token，写命令一律拒绝。")
            if not self.allow_write:
                raise RedisError(
                    "write_disabled",
                    f"{reason}。数据源「{self.name}」未开启允许写入"
                    f"（界面④数据库里的「允许写入」开关，或配置文件 databases[].allow_write）。",
                )
        started = time.time()
        conn = self._conn()
        try:
            conn.connect()
            raw = conn.execute([str(a) for a in args])
        finally:
            conn.close()
        return {
            "source": self.name,
            "command": " ".join(str(a) for a in args),
            "level": level,
            "result": _to_text(raw) if not isinstance(raw, list) else
                      [_to_text(v) for v in raw],
            "server_version": conn.server_version,
            "elapsed_ms": int((time.time() - started) * 1000),
            "notes": conn.notes,
        }

    # ---- 结构化操作 ----

    def keys(self, pattern: str = "*", *, limit=None, cursor: int = 0,
             scan_count: int = 200) -> dict:
        """SCAN 游标遍历 —— 绝不用 KEYS。"""
        limit = max(1, min(int(limit or self.max_keys), 5000))
        conn = self._conn()
        found = []
        try:
            conn.connect()
            next_cursor = int(cursor or 0)
            rounds = 0
            while len(found) < limit and rounds < 200:
                rounds += 1
                raw = conn.execute(["SCAN", str(next_cursor), "MATCH", pattern,
                                    "COUNT", str(max(10, min(int(scan_count), 500)))])
                next_cursor = int(raw[0])
                for k in raw[1]:
                    found.append(_to_text(k))
                if next_cursor == 0:
                    break
        finally:
            conn.close()
        return {
            "source": self.name,
            "pattern": pattern,
            "keys": found[:limit],
            "count": len(found[:limit]),
            "next_cursor": next_cursor,
            "complete": next_cursor == 0,
            "truncated": len(found) >= limit or next_cursor != 0,
            "server_version": conn.server_version,
            "hint": "SCAN 只保证遍历期间稳定存在的 key 都会返回至少一次，可能有重复；"
                    "next_cursor 不为 0 时用同一 cursor 继续。",
        }

    def read_key(self, key: str, *, limit: int = 50) -> dict:
        if not key:
            raise RedisError("bad_request", "必须指定 key")
        limit = max(1, min(int(limit or 50), 1000))
        conn = self._conn()
        try:
            conn.connect()
            rtype = _to_text(conn.execute(["TYPE", key]))
            if rtype == "none":
                return {"source": self.name, "key": key, "exists": False,
                        "server_version": conn.server_version}
            ttl = conn.execute(["TTL", key])
            out = {"source": self.name, "key": key, "exists": True, "type": rtype,
                   "ttl_sec": ttl, "server_version": conn.server_version}
            if rtype == "string":
                raw = conn.execute(["GET", key])
                out["value"] = _to_text(raw)
                out["bytes"] = len(raw) if isinstance(raw, bytes) else None
                out["strlen"] = conn.execute(["STRLEN", key])
            elif rtype == "hash":
                raw = conn.execute(["HGETALL", key])
                pairs = [_to_text(v) for v in raw]
                items = list(zip(pairs[0::2], pairs[1::2]))
                out["length"] = conn.execute(["HLEN", key])
                out["value"] = dict(items[:limit])
                out["truncated"] = len(items) > limit
            elif rtype == "list":
                out["length"] = conn.execute(["LLEN", key])
                out["value"] = [_to_text(v) for v in
                                conn.execute(["LRANGE", key, "0", str(limit - 1)])]
                out["truncated"] = out["length"] > limit
            elif rtype == "set":
                out["length"] = conn.execute(["SCARD", key])
                raw = conn.execute(["SSCAN", key, "0", "COUNT", str(limit)])
                out["value"] = [_to_text(v) for v in raw[1]]
                out["truncated"] = out["length"] > len(out["value"])
            elif rtype == "zset":
                out["length"] = conn.execute(["ZCARD", key])
                raw = conn.execute(["ZRANGE", key, "0", str(limit - 1), "WITHSCORES"])
                vals = [_to_text(v) for v in raw]
                out["value"] = [{"member": m, "score": s}
                                for m, s in zip(vals[0::2], vals[1::2])]
                out["truncated"] = out["length"] > limit
            elif rtype == "stream":
                out["length"] = conn.execute(["XLEN", key])
                raw = conn.execute(["XRANGE", key, "-", "+", "COUNT", str(limit)])
                out["value"] = [[_to_text(x) for x in pair] for pair in raw]
                out["truncated"] = out["length"] > limit
            else:
                out["value"] = None
                out["note"] = f"暂不支持展示 {rtype} 类型的值"
            return out
        finally:
            conn.close()

    def info(self) -> dict:
        conn = self._conn()
        try:
            conn.connect()
            text = _to_text(conn.execute(["INFO"])) or ""
            sections = {}
            cur = None
            for line in text.splitlines():
                if line.startswith("#"):
                    cur = line.lstrip("# ").strip()
                    sections[cur] = {}
                elif ":" in line and cur:
                    k, v = line.split(":", 1)
                    sections[cur][k.strip()] = v.strip()
            server = sections.get("Server", {})
            clients = sections.get("Clients", {})
            mem = sections.get("Memory", {})
            stats = sections.get("Stats", {})
            keyspace = sections.get("Keyspace", {})
            return {
                "source": self.name,
                "ok": True,
                "server_version": server.get("redis_version", conn.server_version),
                "mode": server.get("redis_mode"),
                "os": server.get("os"),
                "uptime_days": server.get("uptime_in_days"),
                "connected_clients": clients.get("connected_clients"),
                "used_memory_human": mem.get("used_memory_human"),
                "maxmemory_human": mem.get("maxmemory_human"),
                "total_commands_processed": stats.get("total_commands_processed"),
                "keyspace": keyspace,
                "dbsize": conn.execute(["DBSIZE"]),
                "notes": conn.notes,
            }
        finally:
            conn.close()

    def test(self) -> dict:
        info = {"source": self.name, "dsn": dsn_summary(self.profile)}
        conn = self._conn()
        try:
            conn.connect()
            info["ok"] = True
            info["server_version"] = conn.server_version
            info["ping"] = _to_text(conn.execute(["PING"]))
            info["dbsize"] = conn.execute(["DBSIZE"])
            repl = _to_text(conn.execute(["INFO", "replication"])) or ""
            info["role"] = _first_line(repl, "role")
            info["notes"] = conn.notes
        finally:
            conn.close()
        return info


def _first_line(text: str, key: str):
    for line in text.splitlines():
        if line.startswith(key + ":"):
            return line.split(":", 1)[1].strip()
    return None
