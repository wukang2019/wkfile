#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
WinFile MCP —— 把远程 Windows 服务器上的 winfile-agent 包装成 MCP 工具

Trae（或任何支持 MCP 的客户端）会以 stdio 方式拉起本进程，
本进程再把每次工具调用转发给服务器上的 winfile-agent。

配置来源（按优先级）
--------------------
1. 环境变量：WINFILES_BASE_URL / WINFILES_TOKEN / WINFILES_TIMEOUT / WINFILES_VERIFY_TLS
2. 配置文件：默认同目录 mcp.config.json，可用 WINFILES_CONFIG 指定

自检
----
    python winfile_mcp.py --check        # 检查连通性并打印可操作范围
    python winfile_mcp.py --list-tools   # 打印工具清单（JSON）
"""

from __future__ import annotations

import json
import os
import ssl
import sys
import traceback
import urllib.error
import urllib.request

VERSION = "1.0.0"
SERVER_NAME = "winfile-mcp"
SUPPORTED_PROTOCOLS = ["2025-06-18", "2025-03-26", "2024-11-05", "2024-10-07"]
DEFAULT_PROTOCOL = "2024-11-05"

HERE = os.path.dirname(os.path.abspath(__file__))

# --------------------------------------------------------------------------------------
# 配置
# --------------------------------------------------------------------------------------


def _truthy(v) -> bool:
    return str(v).strip().lower() not in ("0", "false", "no", "off", "")


def load_settings() -> dict:
    cfg = {
        "base_url": "",
        "token": "",
        "timeout": 60,
        "verify_tls": True,
        "name": "windows-server",
    }
    path = os.environ.get("WINFILES_CONFIG") or os.path.join(HERE, "mcp.config.json")
    if os.path.isfile(path):
        try:
            with open(path, "r", encoding="utf-8-sig") as fh:
                cfg.update(json.load(fh) or {})
        except (OSError, json.JSONDecodeError) as exc:
            log(f"[warn] 配置文件读取失败 {path}: {exc}")

    if os.environ.get("WINFILES_BASE_URL"):
        cfg["base_url"] = os.environ["WINFILES_BASE_URL"]
    if os.environ.get("WINFILES_TOKEN"):
        cfg["token"] = os.environ["WINFILES_TOKEN"]
    if os.environ.get("WINFILES_TIMEOUT"):
        cfg["timeout"] = int(float(os.environ["WINFILES_TIMEOUT"]))
    if os.environ.get("WINFILES_VERIFY_TLS") is not None:
        cfg["verify_tls"] = _truthy(os.environ["WINFILES_VERIFY_TLS"])
    if os.environ.get("WINFILES_SERVER_NAME"):
        cfg["name"] = os.environ["WINFILES_SERVER_NAME"]

    cfg["base_url"] = str(cfg.get("base_url") or "").rstrip("/")
    return cfg


SETTINGS: dict = {}


def log(msg: str) -> None:
    """日志只能进 stderr —— stdout 是 MCP 协议通道。"""
    try:
        sys.stderr.write(str(msg) + "\n")
        sys.stderr.flush()
    except Exception:
        pass


# --------------------------------------------------------------------------------------
# 调用远程 agent
# --------------------------------------------------------------------------------------


class RemoteError(Exception):
    pass


def call_agent(action: str, payload: dict) -> dict:
    settings = SETTINGS
    if not settings.get("base_url"):
        raise RemoteError(
            "还没有配置服务器地址。请编辑 client/mcp.config.json 的 base_url，"
            "或设置环境变量 WINFILES_BASE_URL。"
        )
    if not settings.get("token"):
        raise RemoteError(
            "还没有配置 Token。请把服务器上的 token 填进 client/mcp.config.json，"
            "或设置环境变量 WINFILES_TOKEN。"
        )

    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        f"{settings['base_url']}/api/{action}",
        data=body,
        method="POST",
        headers={
            "Content-Type": "application/json; charset=utf-8",
            "Authorization": f"Bearer {settings['token']}",
            "User-Agent": f"{SERVER_NAME}/{VERSION}",
        },
    )
    ctx = None
    if str(settings["base_url"]).lower().startswith("https") and not settings.get("verify_tls", True):
        ctx = ssl._create_unverified_context()

    try:
        with urllib.request.urlopen(req, timeout=float(settings.get("timeout", 60)), context=ctx) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        try:
            parsed = json.loads(raw)
            err = parsed.get("error") or {}
            raise RemoteError(f"[{err.get('code') or exc.code}] {err.get('message') or raw[:300]}")
        except RemoteError:
            raise
        except Exception:
            raise RemoteError(f"HTTP {exc.code}: {raw[:300]}")
    except urllib.error.URLError as exc:
        raise RemoteError(
            f"连不上服务器 {settings['base_url']}（{exc.reason}）。"
            "检查：agent 是否在跑、端口是否放行、base_url 是否正确。"
        )
    except TimeoutError:
        raise RemoteError("请求超时，服务器可能未响应")

    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        raise RemoteError(f"服务端返回了非 JSON 内容：{raw[:300]}")

    if not parsed.get("ok"):
        err = parsed.get("error") or {}
        raise RemoteError(f"[{err.get('code', 'error')}] {err.get('message', '未知错误')}")
    return parsed.get("data") or {}


# --------------------------------------------------------------------------------------
# 工具定义
# --------------------------------------------------------------------------------------

TOOLS = [
    {
        "name": "win_roots",
        "description": "查看远程 Windows 服务器上本工具被允许操作的根目录、是否只读、是否允许删除。**动手前先调用它**，确认能碰哪些目录。",
        "inputSchema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "win_disk",
        "description": "查看远程服务器的磁盘分区及各盘总容量/剩余空间，用于判断文件放哪、能不能写。",
        "inputSchema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "win_list",
        "description": "列出目录内容（文件+子目录，含大小、修改时间、是否只读）。不传 path 时列出所有可操作根目录。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "目录绝对路径，如 D:\\share\\logs；留空则列出根目录列表"},
                "recursive": {"type": "boolean", "description": "是否递归列出，默认 false"},
                "max_depth": {"type": "integer", "description": "递归深度，默认 1"},
                "pattern": {"type": "string", "description": "按名字过滤，支持通配符，如 *.log"},
                "include_hidden": {"type": "boolean", "description": "是否显示点号开头的隐藏项，默认 false"},
                "max_entries": {"type": "integer", "description": "最多返回多少条，默认 500"},
            },
            "required": [],
        },
    },
    {
        "name": "win_info",
        "description": "查看单个文件或目录的详情（大小、修改时间、读写权限；文件可加 hash 取 sha256）。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "文件或目录绝对路径"},
                "hash": {"type": "boolean", "description": "是否计算 sha256，默认 false"},
            },
            "required": ["path"],
        },
    },
    {
        "name": "win_read",
        "description": "读取文本文件内容（带行号）。大文件用 start_line/max_lines 分段读。二进制文件请用 win_read_b64。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "文件绝对路径"},
                "start_line": {"type": "integer", "description": "从第几行开始（从 1 计），默认 1"},
                "max_lines": {"type": "integer", "description": "最多读多少行，默认 400"},
                "encoding": {"type": "string", "description": "编码，默认 auto（自动识别 UTF-8 / GBK）"},
                "line_numbers": {"type": "boolean", "description": "是否带行号，默认 true"},
            },
            "required": ["path"],
        },
    },
    {
        "name": "win_write",
        "description": "写入/覆盖文本文件（自动建父目录，写前默认自动备份原文件）。覆盖前建议先用 win_read 读一遍，并把返回的 sha256 传给 expected_sha256 以防并发覆盖。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "文件绝对路径"},
                "content": {"type": "string", "description": "完整的新内容"},
                "encoding": {"type": "string", "description": "编码，默认 utf-8"},
                "create_dirs": {"type": "boolean", "description": "自动创建父目录，默认 true"},
                "expected_sha256": {"type": "string", "description": "可选：文件当前 sha256，不匹配则拒绝写入"},
            },
            "required": ["path", "content"],
        },
    },
    {
        "name": "win_replace",
        "description": "在文件里做精确字符串替换（也可用正则），只替换命中部分，适合改配置项。默认写前自动备份。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "文件绝对路径"},
                "old": {"type": "string", "description": "要被替换的原文（regex=true 时是正则）"},
                "new": {"type": "string", "description": "替换成什么，默认空串"},
                "regex": {"type": "boolean", "description": "old 是否按正则解释，默认 false"},
                "count": {"type": "integer", "description": "最多替换几处，0 或不传表示全部"},
                "ignore_case": {"type": "boolean", "description": "正则模式下是否忽略大小写"},
            },
            "required": ["path", "old"],
        },
    },
    {
        "name": "win_append",
        "description": "在文件末尾追加内容（文件不存在则创建），适合写日志。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "文件绝对路径"},
                "content": {"type": "string", "description": "要追加的内容"},
                "encoding": {"type": "string", "description": "编码，默认 utf-8"},
            },
            "required": ["path", "content"],
        },
    },
    {
        "name": "win_mkdir",
        "description": "创建目录（默认递归创建）。",
        "inputSchema": {
            "type": "object",
            "properties": {"path": {"type": "string", "description": "目录绝对路径"}},
            "required": ["path"],
        },
    },
    {
        "name": "win_move",
        "description": "移动或重命名文件/目录。目标已存在时默认报错，除非 overwrite=true。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "src": {"type": "string", "description": "源路径"},
                "dst": {"type": "string", "description": "目标路径"},
                "overwrite": {"type": "boolean", "description": "是否允许覆盖，默认 false"},
            },
            "required": ["src", "dst"],
        },
    },
    {
        "name": "win_copy",
        "description": "复制文件或目录（目录递归复制）。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "src": {"type": "string", "description": "源路径"},
                "dst": {"type": "string", "description": "目标路径"},
                "overwrite": {"type": "boolean", "description": "是否允许覆盖，默认 false"},
            },
            "required": ["src", "dst"],
        },
    },
    {
        "name": "win_trash",
        "description": "删除文件/目录 —— 实际是移动到服务器上的回收站目录（.winfile-trash），可用 win_trash_restore 恢复。删目录且非空时需要 recursive=true。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "要删除的路径"},
                "recursive": {"type": "boolean", "description": "目录非空时是否连内容一起删，默认 false"},
            },
            "required": ["path"],
        },
    },
    {
        "name": "win_trash_list",
        "description": "列出服务器回收站里的条目（含 trash_id、原路径、删除时间），用于找回误删。",
        "inputSchema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "win_trash_restore",
        "description": "把回收站里的某个条目还原回原路径。",
        "inputSchema": {
            "type": "object",
            "properties": {"trash_id": {"type": "string", "description": "win_trash_list 返回的 trash_id"}},
            "required": ["trash_id"],
        },
    },
    {
        "name": "win_search",
        "description": "按文件名在远程服务器上递归查找（支持通配符 * ? 或正则），比 win_list 递归更适合找文件。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "pattern": {"type": "string", "description": "文件名模式，如 *.log 或 app?.ini"},
                "root": {"type": "string", "description": "从哪个目录开始找，默认第一个根目录"},
                "kind": {"type": "string", "enum": ["any", "file", "dir"], "description": "只要文件/只要目录，默认 any"},
                "regex": {"type": "boolean", "description": "pattern 无通配符时是否按正则解释，默认 false"},
                "max_results": {"type": "integer", "description": "最多返回多少条，默认 200"},
            },
            "required": ["pattern"],
        },
    },
    {
        "name": "win_grep",
        "description": "在远程服务器上的文本文件里按正则搜索内容（返回 文件/行号/该行文本），适合查日志、找报错、定位配置。先用这个再动手改，比盲改安全。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "regex": {"type": "string", "description": "正则表达式，如 \"ERROR|Exception\""},
                "root": {"type": "string", "description": "搜索根目录，默认第一个根目录"},
                "glob": {"type": "string", "description": "只搜匹配的文件名，默认 *"},
                "ignore_case": {"type": "boolean", "description": "是否忽略大小写，默认 true"},
                "context_lines": {"type": "integer", "description": "每条命中附带上下几行，默认 0"},
                "max_results": {"type": "integer", "description": "最多返回多少条，默认 100"},
                "only_known_extensions": {"type": "boolean", "description": "只搜常见文本后缀，默认 true"},
            },
            "required": ["regex"],
        },
    },
    {
        "name": "win_read_b64",
        "description": "以 base64 读取较小的二进制文件（图片/压缩包/证书等），默认最多 1MB。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "文件绝对路径"},
                "max_bytes": {"type": "integer", "description": "最多读多少字节，默认 1048576"},
            },
            "required": ["path"],
        },
    },
    {
        "name": "win_write_b64",
        "description": "用 base64 内容写入二进制文件（自动建父目录，写前备份）。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "文件绝对路径"},
                "base64": {"type": "string", "description": "base64 编码的内容"},
            },
            "required": ["path", "base64"],
        },
    },
]

TOOL_MAP = {t["name"]: t for t in TOOLS}

# 工具名 → (agent 接口, 参数名映射)
ROUTE_MAP = {
    "win_roots": ("roots", {}),
    "win_disk": ("disk", {}),
    "win_list": ("list", {}),
    "win_info": ("stat", {"hash": "hash"}),
    "win_read": ("read", {}),
    "win_write": ("write", {}),
    "win_replace": ("replace", {}),
    "win_append": ("append", {}),
    "win_mkdir": ("mkdir", {}),
    "win_move": ("move", {}),
    "win_copy": ("copy", {}),
    "win_trash": ("trash", {}),
    "win_trash_list": ("trash_list", {}),
    "win_trash_restore": ("trash_restore", {}),
    "win_search": ("search", {}),
    "win_grep": ("grep", {}),
    "win_read_b64": ("read_b64", {}),
    "win_write_b64": ("write_b64", {}),
}


def render(tool_name: str, data: dict) -> str:
    """把 agent 返回的数据渲染成适合模型阅读的文本。"""
    if tool_name == "win_read":
        head = (
            f"{data.get('path')}  [{data.get('encoding')}, {data.get('size_human')}, "
            f"共 {data.get('total_lines')} 行, 本次第 {data.get('start_line')} 行起 "
            f"{data.get('returned_lines')} 行"
            f"{', 已截断' if data.get('truncated') else ''}]"
        )
        return head + "\n" + ("-" * 60) + "\n" + str(data.get("content", ""))

    if tool_name == "win_list":
        lines = [f"目录：{data.get('path') or '(根目录列表)'}   共 {data.get('count')} 项"]
        for e in data.get("entries", []):
            if e.get("type") == "dir":
                lines.append(f"  [DIR ] {e.get('mtime', '')}  {e.get('path')}")
            elif e.get("type") == "file":
                lines.append(
                    f"  [FILE] {e.get('mtime', '')}  {e.get('size_human', ''):>10}  {e.get('path')}"
                )
            else:
                lines.append(f"  [??  ] {e}")
        if data.get("truncated"):
            lines.append("  ...（已达上限，结果被截断）")
        return "\n".join(lines)

    if tool_name == "win_grep":
        lines = [
            f"在 {data.get('root')} 下按 /{data.get('regex')}/ 搜索，"
            f"扫描 {data.get('files_scanned')} 个文件，命中 {data.get('count')} 处"
        ]
        for m in data.get("matches", []):
            lines.append(f"  {m.get('rel')}:{m.get('line')}: {m.get('text')}")
            for c in m.get("context") or []:
                mark = ">" if c.get("line") == m.get("line") else " "
                lines.append(f"     {mark} {c.get('line')}: {c.get('text')}")
        if data.get("truncated"):
            lines.append("  ...（已达上限，结果被截断）")
        if not data.get("matches"):
            lines.append("  （没有命中）")
        return "\n".join(lines)

    if tool_name == "win_search":
        lines = [f"在 {data.get('root')} 下查找 {data.get('pattern')}，命中 {data.get('count')} 个"]
        for e in data.get("results", []):
            kind = "DIR " if e.get("type") == "dir" else "FILE"
            lines.append(f"  [{kind}] {e.get('path')}")
        if data.get("truncated"):
            lines.append("  ...（已达上限，结果被截断）")
        if not data.get("results"):
            lines.append("  （没有命中）")
        return "\n".join(lines)

    if tool_name == "win_disk":
        lines = ["远程服务器磁盘："]
        for d in data.get("drives", []):
            flag = "★可操作" if d.get("allowed") else ""
            lines.append(
                f"  {d.get('path')}  总 {d.get('total_human')}  剩余 {d.get('free_human')}  {flag}"
            )
        return "\n".join(lines)

    if tool_name == "win_roots":
        lines = [
            "本工具在远程服务器上可操作的目录：",
        ]
        for r in data.get("roots", []):
            lines.append(f"  {r.get('path')}  {'(存在)' if r.get('exists') else '(不存在)'}")
        lines.append(
            f"只读模式：{data.get('read_only')}   允许删除：{data.get('allow_delete')}   "
            f"允许移动：{data.get('allow_move')}   写前自动备份：{data.get('auto_backup')}"
        )
        return "\n".join(lines)

    if tool_name == "win_trash_list":
        if not data.get("items"):
            return "回收站是空的。"
        lines = [f"回收站共 {data.get('count')} 条："]
        for it in data.get("items", []):
            lines.append(
                f"  {it.get('trash_id')}  {it.get('deleted_at')}  {it.get('type')}  "
                f"原位置 {it.get('original_path')}"
            )
        return "\n".join(lines)

    return json.dumps(data, ensure_ascii=False, indent=2)


# --------------------------------------------------------------------------------------
# MCP 协议
# --------------------------------------------------------------------------------------


def write_message(obj: dict) -> None:
    raw = json.dumps(obj, ensure_ascii=False).encode("utf-8")
    out = sys.stdout.buffer
    out.write(raw + b"\n")
    out.flush()


def reply(msg_id, result=None, error=None) -> None:
    msg = {"jsonrpc": "2.0", "id": msg_id}
    if error is not None:
        msg["error"] = error
    else:
        msg["result"] = result
    write_message(msg)


def handle_initialize(params: dict) -> dict:
    want = (params or {}).get("protocolVersion")
    proto = want if want in SUPPORTED_PROTOCOLS else DEFAULT_PROTOCOL
    server_name = SETTINGS.get("name") or "windows-server"
    return {
        "protocolVersion": proto,
        "capabilities": {"tools": {"listChanged": False}},
        "serverInfo": {
            "name": SERVER_NAME,
            "version": VERSION,
            "title": f"远程文件管理 · {server_name}",
        },
        "instructions": (
            "这些工具操作的是**远程 Windows 服务器**上的真实文件，不是本地文件。\n"
            "建议流程：\n"
            "1) 先调用 win_roots 确认可操作的目录范围；\n"
            "2) 用 win_list / win_search / win_grep 定位文件，不要凭猜测直接写；\n"
            "3) 改文件前先 win_read 看清当前内容，能用 win_replace 做精确替换就别整文件覆盖；\n"
            "4) 需要删除时用 win_trash（进服务器回收站，可恢复），它会返回 trash_id；\n"
            "5) 写入成功后服务端会自动备份原文件，返回里的 backup 字段就是备份路径。\n"
            "路径一律使用服务器上的绝对路径，例如 D:\\share\\app\\config.ini。"
        ),
    }


def handle_tools_list() -> dict:
    return {"tools": TOOLS}


def handle_tools_call(params: dict) -> dict:
    name = (params or {}).get("name") or ""
    args = (params or {}).get("arguments") or {}
    if name not in TOOL_MAP:
        return {
            "content": [{"type": "text", "text": f"未知工具：{name}"}],
            "isError": True,
        }
    action, _ = ROUTE_MAP[name]
    try:
        payload = {k: v for k, v in args.items() if v is not None}
        data = call_agent(action, payload)
        return {"content": [{"type": "text", "text": render(name, data)}], "isError": False}
    except RemoteError as exc:
        return {"content": [{"type": "text", "text": f"调用失败：{exc}"}], "isError": True}
    except Exception as exc:  # noqa: BLE001
        log(traceback.format_exc())
        return {"content": [{"type": "text", "text": f"内部错误：{exc}"}], "isError": True}


def serve() -> int:
    log(f"[{SERVER_NAME}] starting, base_url={SETTINGS.get('base_url') or '(未配置)'}")
    for line in sys.stdin.buffer:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            log(f"[warn] 无法解析的消息：{exc}")
            continue

        if not isinstance(msg, dict):
            continue
        method = msg.get("method")
        msg_id = msg.get("id")
        params = msg.get("params") or {}

        if msg_id is None:
            # 通知，无需响应
            continue

        try:
            if method == "initialize":
                reply(msg_id, handle_initialize(params))
            elif method == "tools/list":
                reply(msg_id, handle_tools_list())
            elif method == "tools/call":
                reply(msg_id, handle_tools_call(params))
            elif method == "ping":
                reply(msg_id, {})
            elif method in ("resources/list", "prompts/list", "resources/templates/list"):
                key = method.split("/")[0]
                reply(msg_id, {key: []})
            elif method in ("logging/setLevel", "notifications/initialized"):
                reply(msg_id, {})
            else:
                reply(msg_id, error={"code": -32601, "message": f"Method not found: {method}"})
        except Exception as exc:  # noqa: BLE001
            log(traceback.format_exc())
            reply(msg_id, error={"code": -32603, "message": f"Internal error: {exc}"})
    log(f"[{SERVER_NAME}] stdin closed, exiting")
    return 0


# --------------------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------------------


def cli() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

    SETTINGS.update(load_settings())

    if "--list-tools" in sys.argv:
        print(json.dumps(TOOLS, ensure_ascii=False, indent=2))
        return 0

    if "--check" in sys.argv:
        print(f"base_url = {SETTINGS.get('base_url') or '(未配置)'}")
        print(f"token    = {'*' * 8 + SETTINGS['token'][-4:] if SETTINGS.get('token') else '(未配置)'}")
        try:
            roots = call_agent("roots", {})
            disks = call_agent("disk", {})
            print("[OK] 连接成功")
            print(render("win_roots", roots))
            print(render("win_disk", disks))
            return 0
        except RemoteError as exc:
            print(f"[X] {exc}")
            return 1

    return serve()


if __name__ == "__main__":
    raise SystemExit(cli())
