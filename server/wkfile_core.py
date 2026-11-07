"""wkfile 纯逻辑层（v1.7.21）。

这里只放**不依赖 Agent 状态、不碰网络、不抛 PathError** 的函数：
配置分层合并、编码/哈希、Token 有效期、超时夹取、备份去重的路径计算、
补丁请求的结构预检。把它们抽出来的目的有三个：

1. 单测可以直接 import 本模块，不必构造 Agent / 临时目录 / HTTP 服务；
2. server/wkfile_agent.py 不再既是协议层又是工具箱，改动面更清楚；
3. 图形界面、自检脚本、MCP 协议层可以共用同一份实现，避免数字漂移。

**兼容约定**：wkfile_agent 会 `from wkfile_core import ...`，所以
`wkfile_agent.human_size` 等旧名字照常可用，既有测试和外部脚本不用改。
本模块不得反向 import wkfile_agent（会成环）。
"""

from __future__ import annotations

import ipaddress
import json
import math
import os
from datetime import datetime, timedelta

# ---------------------------------------------------------------------------
# 通用小工具
# ---------------------------------------------------------------------------


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


def sha256_under_cap(path: str, cap: int) -> tuple[str | None, str]:
    """带体积上限地算**整个文件**的 sha256。

    几百 MB 的文件会把请求线程占住几十秒，等于变相 DoS。超限不硬算，
    返回 (None, 原因) 让调用方自己决定报错还是降级。乐观锁比对的是
    全文件 hash，不是当前页，所以必须独立于分页读取去算。
    """
    size = os.path.getsize(path)
    if size > cap:
        return None, (f"文件 {human_size(size)} 超过哈希上限 "
                      f"{human_size(cap)}，拒绝计算 sha256。")
    return sha256_of(path), ""


# ---------------------------------------------------------------------------
# win_exec 超时天花板
# ---------------------------------------------------------------------------

EXEC_TIMEOUT_HARD_CAP = 3600


def exec_timeout_cap(value, *, default: int = 300) -> int:
    """把 exec_timeout_max_sec 规整成合法上限。

    配置文件是给人手填的，热加载也会把任意 JSON 值直接 setattr 进来：
    非整数、0、负数、字符串、超大值都必须夹回可运行区间。
    """
    try:
        n = int(value)
    except (TypeError, ValueError):
        return default
    if n < 1:
        return default
    return min(n, EXEC_TIMEOUT_HARD_CAP)


# ---------------------------------------------------------------------------
# 网络地址判断
# ---------------------------------------------------------------------------


def is_loopback_bind(host: str) -> bool:
    """判断监听地址是不是只绑本机。"""
    h = str(host or "").strip().lower()
    return h in ("localhost", "::1") or h.startswith("127.")


def is_private_ip(ip: str) -> bool:
    """是不是内网 / 回环 / 保留地址。"""
    try:
        addr = ipaddress.ip_address(str(ip).strip())
    except ValueError:
        return False
    return bool(addr.is_private or addr.is_loopback or addr.is_link_local
                or addr.is_reserved or addr.is_multicast or addr.is_unspecified)


# ---------------------------------------------------------------------------
# Token 有效期与强度
# ---------------------------------------------------------------------------

WEAK_TOKENS = frozenset({
    "", "test", "token", "changeme", "change-me", "password", "secret",
    "testtoken", "testtoken123456", "123456", "admin",
    "在这里填一个长随机串，或运行 python wkfile_agent.py --init 自动生成",
    "在这里填一个长随机串；用图形界面启动时点「生成」按钮即可",
})

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
    """解析 token_expires_at：返回 (状态, 到期时间)。"""
    s = str(raw or "").strip()
    if not s:
        return "never", None
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return "bad", None
    if dt.tzinfo is None:
        dt = dt.astimezone()
    return "ok", dt


def expiry_state(expires_at) -> tuple[str, object, float]:
    """按到期时间串算状态，返回 (状态, 到期时间, 剩余秒数)。"""
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


# ---------------------------------------------------------------------------
# 配置文件里的整行注释
# ---------------------------------------------------------------------------


def strip_json_comments(text: str) -> str:
    """剥掉 _annotate_config 插进去的 "// " 整行注释。

    只处理**整行**注释（我们只生成这种），不动字符串里的 //。
    """
    lines = []
    for line in text.splitlines():
        if line.lstrip().startswith("//"):
            continue
        lines.append(line)
    return "\n".join(lines)


# 别名：wkfile_agent 里原来叫 _strip_json_comments，旧的调用点不用改。
_strip_json_comments = strip_json_comments


# ---------------------------------------------------------------------------
# P2 配置分层（v1.7.21）
# ---------------------------------------------------------------------------

ENV_PREFIX = "WKFILE_"
_TRUE_WORDS = frozenset({"1", "true", "yes", "on"})
_FALSE_WORDS = frozenset({"0", "false", "no", "off"})


def coerce_env_value(raw, sample):
    """把环境变量字符串按「默认值类型」转换。返回 (值, 错误说明)。

    脏值一律**忽略并告警**，绝不抛异常——配置分层是为了方便部署，
    不能因为拼错一个环境变量就让整个服务起不来。
    """
    if isinstance(sample, bool):
        s = str(raw).strip().lower()
        if s in _TRUE_WORDS:
            return True, ""
        if s in _FALSE_WORDS:
            return False, ""
        return None, "期望 true/false"
    if isinstance(sample, int) and not isinstance(sample, bool):
        try:
            return int(str(raw).strip()), ""
        except (TypeError, ValueError):
            return None, "期望整数"
    if isinstance(sample, float):
        try:
            return float(str(raw).strip()), ""
        except (TypeError, ValueError):
            return None, "期望数字"
    if isinstance(sample, (list, dict)):
        try:
            val = json.loads(str(raw))
        except (TypeError, ValueError):
            return None, "期望 JSON 数组/对象"
        if not isinstance(val, type(sample)):
            return None, f"期望 JSON {type(sample).__name__}"
        return val, ""
    return str(raw), ""


def merge_config_layers(defaults: dict, file_data: dict | None,
                        env: dict | None, *, prefix: str = ENV_PREFIX) -> tuple[dict, dict]:
    """三层配置合并：**内置默认 < 配置文件 < 环境变量**。

    返回 (merged, report)。report 记录每个键最终来源、哪些环境变量生效、
    哪些被忽略（拼错的键或脏值），供 win_probe 和运维排障查看。

    安全约束：环境变量**只能覆盖 DEFAULT_CONFIG 里已存在的键**。拼错的
    `WKFILE_TOEKN` 不会被当成新键静默生效，也不会被 save() 写回文件。
    """
    merged = dict(defaults)
    sources = {k: "default" for k in defaults}
    file_data = {k: v for k, v in (file_data or {}).items() if v is not None}
    merged.update(file_data)
    for key in file_data:
        sources[key] = "file"

    applied, warnings = [], []
    for raw_key, raw_val in sorted((env or {}).items()):
        if not str(raw_key).startswith(prefix):
            continue
        key = str(raw_key)[len(prefix):].lower()
        if key not in defaults:
            warnings.append(f"{raw_key}: 未知配置键，已忽略")
            continue
        val, err = coerce_env_value(raw_val, defaults[key])
        if err:
            warnings.append(f"{raw_key}: {err}，已忽略")
            continue
        merged[key] = val
        sources[key] = "env"
        applied.append(key)

    report = {
        "order": ["default", "file", "env"],
        "sources": sources,
        "env_applied": sorted(applied),
        "env_warnings": warnings,
    }
    return merged, report


def _config_type_ok(val, sample) -> tuple[bool, str]:
    """单个配置值的类型是否可接受。返回 (是否通过, 期望类型的人类说法)。

    bool 必须先于 int 判断（Python 里 bool 是 int 的子类），否则
    auto_backup=1 会被当成合法布尔值一路带到写操作里。
    """
    if isinstance(sample, bool):
        return isinstance(val, bool), "true/false"
    if isinstance(sample, int):
        return isinstance(val, int) and not isinstance(val, bool), "整数"
    if isinstance(sample, float):
        if isinstance(val, bool) or not isinstance(val, (int, float)):
            return False, "数字"
        return math.isfinite(val), "有限数字"
    if isinstance(sample, str):
        return isinstance(val, str), "字符串"
    if isinstance(sample, list):
        return isinstance(val, list), "数组"
    if isinstance(sample, dict):
        return isinstance(val, dict), "对象"
    return True, "任意"          # 默认值为 None 的键不设类型限制


def validate_config_types(defaults: dict, merged: dict,
                          sources: dict) -> tuple[dict, list[dict]]:
    """按内置默认值的类型校验合并后的配置：脏值回退默认 + 结构化告警。

    v1.7.25：**配置文件层此前完全没有类型校验**（只有环境变量层有
    coerce_env_value）。于是 `"backup_max_bytes": "2GB"` 这种很自然的
    写法会一直活到运行时的 int() 才炸 —— 启动清理被 Agent.__init__ 的
    try/except 静默吞掉（保留策略整个失效、没人知道），op_probe /
    op_patch 直接回 500，连唯一能自检的工具都哑了。而手改 JSON 的
    恰恰是运维本人，这不是理论风险。

    这里在加载阶段一次结清。只校验类型与非有限数字，不改语义：
    0 / 负数 / 空串 / 空容器都合法，范围与枚举仍由各自使用点决定
    （例如回滚闸的 0 值脚枪由 op_probe 单独提示）。

    **告警只带键名与类型名，绝不带配置值**：这份告警会进免鉴权的
    /api/health，而同一个配置文件里还有 Token 之类的敏感键。
    未知键原样保留，不静默抹掉用户手写的自定义键。
    """
    cleaned = dict(merged)
    warnings = []
    for key, sample in defaults.items():
        if key not in cleaned:
            continue
        val = cleaned[key]
        ok, expected = _config_type_ok(val, sample)
        if ok:
            continue
        cleaned[key] = sample
        warnings.append({
            "key": key,
            "layer": sources.get(key, "default"),
            "expected": expected,
            "got": type(val).__name__,
            "message": (f"配置项 {key} 类型不对（期望 {expected}，"
                        f"实际 {type(val).__name__}），已回退到内置默认值；"
                        "数值请填纯整数，不要带单位"),
        })
    return cleaned, warnings


# ---------------------------------------------------------------------------
# 备份增量去重（纯计算部分）
# ---------------------------------------------------------------------------


def cas_object_path(objects_dir: str, digest: str) -> str:
    """内容寻址实体路径：objects/<前两位>/<完整 sha256>。"""
    return os.path.join(objects_dir, digest[:2], digest)


def dedup_eligible(size: int, cap: int) -> bool:
    """超过上限的文件跳过内容寻址（算 hash 本身比复制还贵）。"""
    return 0 < size <= cap


# ---------------------------------------------------------------------------
# win_patch 请求预检（纯计算部分）
# ---------------------------------------------------------------------------

PATCH_MAX_FILES = 20
PATCH_MAX_TOTAL_BYTES = 8 * 1024 * 1024


def patch_plan(changes, *, max_files: int = PATCH_MAX_FILES,
               max_total_bytes: int = PATCH_MAX_TOTAL_BYTES) -> tuple[list, list]:
    """校验一次多文件补丁请求的结构与预算，返回 (规范化条目, 错误列表)。

    只做**不碰磁盘**的检查：路径非空、old/new 合法性、条数上限、
    总体积预算。真实存在性 / old 是否命中 / 语法校验由 Agent 在预检阶段做。
    """
    errors: list[str] = []
    if not isinstance(changes, list) or not changes:
        return [], ["changes 必须是非空数组"]
    if len(changes) > max_files:
        return [], [f"一次最多改 {max_files} 个文件，本次 {len(changes)} 个，请拆批"]

    planned, total = [], 0
    for i, item in enumerate(changes):
        tag = f"changes[{i}]"
        if not isinstance(item, dict):
            errors.append(f"{tag} 不是对象")
            continue
        path = str(item.get("path") or "").strip()
        if not path:
            errors.append(f"{tag} 缺 path")
            continue
        old = item.get("old")
        new = item.get("new")
        content = item.get("content")
        if old is None and new is None and content is None:
            errors.append(f"{tag} 需要 old/new（精确替换）或 content（整文件覆盖）")
            continue
        if content is not None and not isinstance(content, str):
            errors.append(f"{tag} content 必须是字符串")
            continue
        if content is not None and (old is not None or new is not None):
            # 两种模式混用是歧义请求：AI 可能以为是「先按 content 写、再替换」，
            # 实现只会取 content。宁可当场说清，也不要静默丢掉一半意图。
            errors.append(f"{tag} content 与 old/new 不能同时传："
                          "要么整文件覆盖（content），要么精确替换（old+new）")
            continue
        if old is not None and not isinstance(old, str):
            errors.append(f"{tag} old 必须是字符串")
            continue
        if new is not None and not isinstance(new, str):
            errors.append(f"{tag} new 必须是字符串")
            continue
        est = len((content if content is not None else (new or "")).encode("utf-8"))
        total += est
        planned.append({
            "path": path,
            "old": old,
            "new": new,
            "content": content,
            "expected_sha256": item.get("expected_sha256"),
            "encoding": item.get("encoding") or "utf-8",
            "count": item.get("count", 0),
        })
    if total > max_total_bytes:
        errors.append(
            f"本次写入合计 {human_size(total)} 超过预算 {human_size(max_total_bytes)}，请拆批")
    return planned, errors


def backup_inode_sizes(groups: dict) -> tuple:
    """按 inode 去重算备份的真实磁盘占用，以及删掉每组能释放多少字节。

    v1.7.23：增量备份用硬链接让多个历史版本指向同一份内容实体，可旧的
    ``_dir_size`` 把每个目录项都按全量计一遍 —— 10 个版本共用 1GB 内容
    会被算成 10GB，``backup_max_bytes`` 于是提前把最旧的版本删掉。

    ``groups`` 形如 ``{".objects": [(inode_key, size), ...],
    "20260101-...": [...], ...}``，一个文件一项。返回值：

    * ``physical``：所有唯一 inode 的字节和（真实占用，每个实体只算一次）；
    * ``reclaim``：每组删掉后能释放的字节。一个 inode 只在**引用它的最后
      一个组**里计入，因为按时间从旧往新删时，只有删到最后一处引用，
      实体才真正少一份目录项（随后由 ``_prune_backup_objects`` 回收）。

    ``.objects`` 只是实体自身的存放处，不参与「谁最后引用」的排序，否则
    它永远排在最后，所有时间戳目录都会被算成 0 可释放。
    """
    all_inodes: dict = {}
    for items in groups.values():
        for key, size in items:
            all_inodes.setdefault(key, int(size))
    physical = sum(all_inodes.values())
    stamps = sorted(k for k in groups if k != ".objects")
    last_owner: dict = {}
    for stamp in stamps:
        for key, _size in groups[stamp]:
            last_owner[key] = stamp
    reclaim = {stamp: 0 for stamp in stamps}
    for key, stamp in last_owner.items():
        reclaim[stamp] += all_inodes[key]
    return physical, reclaim
