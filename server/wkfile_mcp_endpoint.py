#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
wkfile MCP 端点 —— 让 Trae / Cursor / Claude Desktop 等 MCP 客户端直连服务本身

与本地 stdio 桥接的分工（桥接已废弃，源码移至 deprecated/client/winfile_mcp.py）
-----------------------------------------------------------------------------
* 本地 stdio 桥接：跑在客户端那台机器上，把 MCP 请求转成
  HTTP 打到远程服务。需要本机装 Python（或另打包成 exe）。**现已用不到**。
* 本模块是服务进程内的协议层：直接调用 Agent.dispatch()，不经过网络回环，
  也不需要在客户端装任何东西 —— 用 Streamable HTTP / SSE 就能连上。
  —— 当前唯一推荐的接入方式。

对外传输
--------
* Streamable HTTP  —— 端点 /mcp   （客户端配置 type = "http"）
* HTTP + SSE（旧）  —— 端点 /sse   （客户端配置 type = "sse"）

本文件只负责协议构造，不碰 socket；HTTP 细节在 wkfile_agent.py 的 Handler 里。
"""

from __future__ import annotations

import json
import traceback

MCP_SERVICE = "wkfile-mcp"

# 按新到旧排列，客户端要哪个就给哪个（都没有则回落到 DEFAULT_PROTOCOL）
SUPPORTED_PROTOCOLS = ["2025-06-18", "2025-03-26", "2024-11-05", "2024-10-07"]
DEFAULT_PROTOCOL = "2025-03-26"

# 响应正文只发一份：structuredContent 省略重复 content 的阈值。
# initialize 下发的「运行时契约」直接引用这两个常量，避免说明文字与代码各写一份数字。
STRUCTURED_CONTENT_OMIT_BYTES = 4096
# win_write_b64 的引导阈值（base64 膨胀约 33%，再大容易被 AI 客户端静默截断）
BASE64_UPLOAD_HINT_BYTES = 8 * 1024

# 数据库结果被行数上限截断时，给 AI 的「真分页」SQL 模板。
# max_rows 只是响应里最多带几行，数据库仍然算完整结果集；要省 DB 资源、
# 要翻页，必须把分页写进 SQL。模板只有这一份：契约、schema、page_hint 共用。
DB_PAGE_TEMPLATES = {
    "mysql": "SELECT ... FROM ... ORDER BY <稳定且唯一的列> LIMIT {rows} OFFSET {offset}",
    "tsql": ("SELECT ... FROM ... ORDER BY <稳定且唯一的列> "
             "OFFSET {offset} ROWS FETCH NEXT {rows} ROWS ONLY"),
}


def db_page_hint(dialect: str, rows: int) -> dict:
    """结果被截断时返回该数据源方言的分页模板（纯函数，便于测试与契约复用）。"""
    d = "tsql" if str(dialect).lower() in ("tsql", "sqlserver", "mssql") else "mysql"
    n = max(1, min(int(rows or 200), 5000))
    return {
        "dialect": d,
        "rows": n,
        "template": DB_PAGE_TEMPLATES[d].format(rows=n, offset=0),
        "next_offset": n,
        "note": ("max_rows 只限制响应行数，不是 SQL LIMIT，数据库仍会算出完整结果集；"
                 "翻页请按模板改写 SQL 并递增 OFFSET。必须有稳定唯一的 ORDER BY，"
                 "否则两次查询之间数据变动会重复或漏行。"),
    }


def human_bytes(n) -> str:
    """把字节数写成人类可读的短标签，专给协议说明用。"""
    try:
        n = int(n)
    except (TypeError, ValueError):
        return "?"
    if n <= 0:
        return "0 B"
    if n % (1024 * 1024) == 0:
        return f"{n // (1024 * 1024)} MB"
    if n % 1024 == 0:
        return f"{n // 1024} KB"
    return f"{n} B"

# ---------------------------------------------------------------------------
# 工具风险标注
# ---------------------------------------------------------------------------
# MCP 规范的默认值是反直觉的：不标注时客户端按
#     readOnlyHint=false / destructiveHint=true / idempotentHint=false / openWorldHint=true
# 来对待每一个工具 —— 连 win_read 都会被当成"破坏性 + 面向开放世界"，
# 导致客户端（Trae / Cursor / Claude Desktop…）的安全提示全部失真。所以必须逐个显式标注。

_READ_ONLY = {
    "readOnlyHint": True,
    "destructiveHint": False,
    "idempotentHint": True,
    "openWorldHint": False,
}

_WRITE = {
    "readOnlyHint": False,
    "destructiveHint": True,
    "idempotentHint": False,
    "openWorldHint": False,
}

# 写入组里少数工具的幂等性与默认不同，单独覆盖：
#   win_mkdir  重复建目录无副作用
#   win_copy   同源同目标重复执行结果一致（仍可能覆盖目标，故保留 destructiveHint）
_WRITE_IDEMPOTENT = dict(_WRITE, idempotentHint=True)
#   win_append 重复执行会追加两次 —— 必须保持 idempotentHint=False，否则客户端可能自动重试

# ---------------------------------------------------------------------------
# 工具定义（与 stdio 桥接版本保持一致）
# ---------------------------------------------------------------------------

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
        "description": "查看单个文件或目录的详情（大小、修改时间、读写权限；文件可加 hash 取 sha256）。只读内容时若顺便要 sha256，用 win_read(hash=true) 一次拿全，不必再单独调本工具。",
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
        "description": "读取文本文件内容（带行号）。大文件按返回的 next_offset / next_start_line 续读，是否读完只看 eof=true（不要用 returned_lines==0 或 truncated 判断结束）。某一页的 returned_lines 可能为 0：整行长度超过本页字节上限，该页只装了残行片段（line_fragment=true），此时 next_offset 仍会前进，继续读即可。拼接多页正文时：仅当本页 content_ends_with_newline=true 时在页尾补一个 \\n（该字段精确表示本页最后一行在原文里是否由换行终止），否则直接接下一页；不要靠 line_fragment / eof 猜，末尾有无换行的文件会各错一半。这样可逐字节无损还原原文，且各页 returned_lines 之和等于逻辑行数。二进制文件请用 win_read_b64。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "文件绝对路径"},
                "offset": {"type": "integer", "description": "字节游标：填上一次返回的 next_offset，从该字节续读（优先于 start_line，默认 0）"},
                "start_line": {"type": "integer", "description": "行号游标（从 1 计，默认 1）；与 offset 同时传时只作为行号编号基，不切片"},
                "max_lines": {"type": "integer", "description": "最多返回多少行；0 或省略 = 本段全部（总量仍受 max_bytes 限制）"},
                "max_bytes": {"type": "integer", "description": "本页最多返回多少字节（硬上限为服务端 max_read_bytes）；传小了只影响本页大小，长行会分成多个残行页（line_fragment=true），按 eof 续读到尾即可"},
                "encoding": {"type": "string", "description": "编码，默认 auto（自动识别 UTF-8 / GBK）"},
                "line_numbers": {"type": "boolean", "description": "是否带行号，默认 true"},
                "hash": {"type": "boolean", "description": "顺带返回整个文件的 sha256（默认 false，零开销），写回时可直接传给 win_write / win_replace 的 expected_sha256 防并发覆盖；文件过大时会返回 sha256=null 与原因，不影响读取"},
            },
            "required": ["path"],
        },
    },
    {
        "name": "win_write",
        "description": "写入/覆盖文本文件（自动建父目录，写前默认自动备份原文件）。覆盖前建议先用 win_read(hash=true) 读一遍，把返回的 sha256 传给 expected_sha256 以防并发覆盖；不匹配会拒绝写入，并在错误里给出当前 sha256 / 大小 / 修改时间，确认后可据此重试。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "文件绝对路径"},
                "content": {"type": "string", "description": "完整的新内容"},
                "encoding": {"type": "string", "description": "编码，默认 utf-8"},
                "create_dirs": {"type": "boolean", "description": "自动创建父目录，默认 true"},
                "expected_sha256": {"type": "string", "description": "可选：文件当前 sha256（用 win_read(hash=true) 一次拿到；不是 win_read 默认返回值），不匹配则拒绝写入并在错误里给出当前值"},
                "backup": {"type": "boolean", "description": "写前备份原文件，默认 true（服务端 backup_required=true 时不允许跳过）。频繁覆盖同一小文件（如 .env 改一行）时可传 false 少留历史版本"},
            },
            "required": ["path", "content"],
        },
    },
    {
        "name": "win_replace",
        "description": "在文件里做精确字符串替换（也可用正则），只替换命中部分，适合改配置项。默认写前自动备份。\n"
                       "先用 dry_run=true 可以只看改动预览（返回 unified diff，不落盘），确认无误再正式执行。\n"
                       "注意：不传 count 时，如果 old 在文件里出现多次，本次调用会被拒绝（避免一次改错多处）——"
                       "此时请给 old 补上更长的唯一上下文，或显式传 count。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "文件绝对路径"},
                "old": {"type": "string", "description": "要被替换的原文（regex=true 时是正则）；请带上足够的上下文保证唯一"},
                "new": {"type": "string", "description": "替换成什么，默认空串"},
                "regex": {"type": "boolean", "description": "old 是否按正则解释，默认 false"},
                "count": {"type": "integer", "description": "替换几处。不传或 0 表示全部，但仅当匹配唯一时才允许；匹配多处时必须显式指定"},
                "ignore_case": {"type": "boolean", "description": "正则模式下是否忽略大小写"},
                "dry_run": {"type": "boolean", "description": "只预览不落盘：返回 unified diff，文件保持不动。默认 false"},
                "backup": {"type": "boolean", "description": "写前备份原文件，默认 true（服务端 backup_required=true 时不允许跳过）；传 false 可跳过备份（频繁改动同一文件时少留历史版本）"},
                "expected_sha256": {"type": "string", "description": "可选：文件当前 sha256（用 win_read(hash=true) 一次拿到），不匹配则拒绝替换，防止读取与替换之间别人改动被静默覆盖"},
            },
            "required": ["path", "old"],
        },
    },
    {
        "name": "win_append",
        "description": "在文件末尾追加内容（文件不存在则创建），适合写日志。非幂等：重复执行会追加两次。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "文件绝对路径"},
                "content": {"type": "string", "description": "要追加的内容"},
                "encoding": {"type": "string", "description": "编码，默认 utf-8"},
                "backup": {"type": "boolean", "description": "写前备份原文件，默认 true（服务端 backup_required=true 时不允许跳过）；高频写日志建议传 false，避免每次全量复制原文件"},
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
        "description": "删除文件/目录 —— 不是真删，而是移动到服务端 wkfile\\trash 回收站，可用 win_trash_restore 恢复。删目录且非空时需要 recursive=true。",
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
                "regex": {"type": "string", "description": "正则表达式，如 \"ERROR|Exception\"（也接受参数名 pattern 作为别名）"},
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
        "description": "用 base64 内容写入二进制文件（自动建父目录，写前备份）。"
                       f"⚠️ 大文件（>{BASE64_UPLOAD_HINT_BYTES // 1024}KB）慎用：base64 会让参数膨胀 33%，超长会被 AI 客户端静默截断"
                       "而服务端无法察觉——请务必同时传 expected_sha256（按解码后原始内容计算），"
                       "不匹配会 409 拒写；更大的文件（如整个 PHP 类文件）建议改走 HTTP 直传通道"
                       " POST /api/upload（原始字节，用法见 tools/upload.py 或 README）。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "文件绝对路径"},
                "base64": {"type": "string", "description": "base64 编码的内容"},
                "expected_sha256": {"type": "string", "description": "强烈建议提供：解码后原始内容的 sha256 十六进制值，防客户端截断写坏文件"},
                "backup": {"type": "boolean", "description": "写前备份原文件，默认 true（服务端 backup_required=true 时不允许跳过）"},
            },
            "required": ["path", "base64"],
        },
    },
    {
        "name": "win_tail",
        "description": "只看文件末尾若干行（默认 100 行）—— 查日志、看最新报错就用它，不用把整个文件读进来。"
                       "win_exec 返回的 output_file（logs\\exec-*.txt）也能直接传进来读尾部报错，无需先逐页 win_read。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "文件绝对路径"},
                "lines": {"type": "integer", "description": "看末尾多少行，默认 100，最多 5000"},
            },
            "required": ["path"],
        },
    },
    {
        "name": "win_tree",
        "description": "一次拿到目录树概览（缩进列表，含大小），比反复 win_list 逐层翻要快得多。适合先摸清一个项目的结构。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "从哪个目录开始，默认第一个根目录"},
                "depth": {"type": "integer", "description": "往下展几层，默认 3，最多 8"},
                "max_nodes": {"type": "integer", "description": "最多返回多少条，默认 500"},
                "show_size": {"type": "boolean", "description": "文件是否带大小，默认 true"},
                "include_hidden": {"type": "boolean", "description": "是否包含点开头的隐藏项，默认 false"},
            },
            "required": [],
        },
    },
    {
        "name": "win_multi_read",
        "description": "一次读取多个文件（最多 20 个）。单个文件失败不影响其它文件，逐个返回状态。\n"
                       "需要同时看几个相关文件（配置+日志+代码）时用它，比一个文件一次调用省事得多。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "paths": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "要读的文件绝对路径列表",
                },
                "max_bytes": {"type": "integer", "description": "单个文件最多读多少字节"},
                "max_total_bytes": {"type": "integer", "description": "本次合计最多读多少字节，默认 8MB"},
            },
            "required": ["paths"],
        },
    },
    {
        "name": "win_backup_list",
        "description": "列出服务器上的文件备份（每次 win_write / win_replace 覆盖前都会自动存一份）。\n"
                       "用来查「这个文件以前长什么样」，也用来挑要回滚到哪一版。\n"
                       "返回的 policy 是只读的保留策略快照（总量/份数/天龄阈值、当前备份总数与总体积、"
                       "按策略会清掉几份）。备份清理由服务端配置自动执行，不提供删除工具 —— "
                       "需要手动清理请让运维改配置或直接在服务器上处理。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "可选：只看某个原文件路径的备份历史"},
                "max_entries": {"type": "integer", "description": "最多返回多少条，默认 200"},
            },
            "required": [],
        },
    },
    {
        "name": "win_backup_restore",
        "description": "把文件回滚到之前的某个备份版本。故意改坏了文件时用它自救。\n"
                       "二选一给参数：backup_path（win_backup_list 返回的备份文件路径，精确回滚某一版）"
                       "或 original_path（自动取该文件最新的一份备份）。\n"
                       "回滚前会把当前位置的文件也备份一份，所以回滚本身同样可撤销。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "backup_path": {"type": "string", "description": "备份文件的绝对路径（来自 win_backup_list）"},
                "original_path": {"type": "string", "description": "原文件路径；自动使用它最新的一份备份"},
                "dry_run": {"type": "boolean", "description": "只检查会回滚哪个文件、不动磁盘，默认 false"},
            },
            "required": [],
        },
    },
    # ---------------- 数据库 / 缓存 ----------------
    {
        "name": "win_db_list",
        "description": "列出远程服务器上已配置的数据库/缓存数据源（MySQL、SQL Server、Redis）及其可写状态。**操作数据库前先调用它**，确认有哪些数据源可用、名字叫什么、这次请求有没有写权限。",
        "inputSchema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "win_db_query",
        "description": "在 MySQL/MariaDB/SQL Server 上执行一条**只读** SQL 查询（SELECT / SHOW / DESC / EXPLAIN / WITH...SELECT），返回表格数据。写语句（INSERT/UPDATE/DELETE/DDL/EXEC）会被拒绝，请改用 win_db_exec。max_rows 只限制响应行数（数据库仍算完整结果集）；被截断时返回 truncated=true 与 page_hint（含该数据源方言的分页 SQL 模板、下一页 OFFSET 和稳定 ORDER BY 提醒），按它改写 SQL 才能真分页，不要靠反复调大 max_rows。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "source": {"type": "string", "description": "数据源名称（来自 win_db_list）；只配置了一个时可省略"},
                "sql": {"type": "string", "description": "要执行的只读 SQL，一次只能一条，不要带结尾分号"},
                "max_rows": {"type": "integer", "description": "本次最多返回多少行，默认取数据源配置（通常 200）"},
            },
            "required": ["sql"],
        },
    },
    {
        "name": "win_db_exec",
        "description": "在 MySQL/MariaDB/SQL Server 上执行**写** SQL（INSERT / UPDATE / DELETE / CREATE / ALTER / EXEC 等）。需要数据源开启「允许写入」、服务端非只读、且使用完整权限 Token，否则会被拒绝。危险形态（INTO OUTFILE、LOAD_FILE、xp_cmdshell、多语句）永远不允许。执行前建议先用 win_db_query 确认影响范围。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "source": {"type": "string", "description": "数据源名称（来自 win_db_list）"},
                "sql": {"type": "string", "description": "要执行的写 SQL，一次只能一条"},
                "max_rows": {"type": "integer", "description": "若语句会返回结果集，最多返回多少行"},
            },
            "required": ["sql"],
        },
    },
    {
        "name": "win_db_tables",
        "description": "列出 MySQL/MariaDB/SQL Server 中的表。指定 database 时列出该库的表（MySQL 含引擎/注释，SQL Server 含架构/估算行数/占用空间）；不指定时 MySQL 按库汇总表数量、SQL Server 列出全部库。用于在查数据前摸清结构。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "source": {"type": "string", "description": "数据源名称（来自 win_db_list）"},
                "database": {"type": "string", "description": "库名；留空则用数据源配置里的默认库"},
            },
            "required": [],
        },
    },
    {
        "name": "win_db_schema",
        "description": "查看 MySQL/MariaDB/SQL Server 某张表的结构：所有列的字段名、类型、是否可空、默认值，以及索引组成。写 SQL 前用它确认列名，避免猜错字段。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "source": {"type": "string", "description": "数据源名称（来自 win_db_list）"},
                "table": {"type": "string", "description": "表名"},
                "database": {"type": "string", "description": "库名；留空则用数据源配置里的默认库"},
            },
            "required": ["table"],
        },
    },
    {
        "name": "win_db_test",
        "description": "测试某个数据源的连通性，返回服务端版本、认证方式、当前身份和可见的库。排查「连不上数据库」时先跑它，错误信息会直接给出是网络、认证还是权限问题。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "source": {"type": "string", "description": "数据源名称（来自 win_db_list）；只配置了一个时可省略"},
            },
            "required": [],
        },
    },
    {
        "name": "win_redis_info",
        "description": "查看 Redis 实例概况：版本、运行模式、客户端连接数、内存占用、总命令数、各库 key 数量。用于掌握缓存实例的整体状态。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "source": {"type": "string", "description": "数据源名称（来自 win_db_list）"},
            },
            "required": [],
        },
    },
    {
        "name": "win_redis_keys",
        "description": "按模式列出 Redis 的 key（基于 SCAN 游标，绝不使用会阻塞实例的 KEYS）。返回结果可能因 limit 被截断，next_cursor 不为 0 时可用同一 pattern 带上该 cursor 继续翻页。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "source": {"type": "string", "description": "数据源名称（来自 win_db_list）"},
                "pattern": {"type": "string", "description": "匹配模式，如 user:*；默认 * 表示全部"},
                "limit": {"type": "integer", "description": "最多返回多少个 key，默认 200"},
                "cursor": {"type": "integer", "description": "续扫游标，来自上一次返回的 next_cursor；首次传 0"},
            },
            "required": [],
        },
    },
    {
        "name": "win_redis_get",
        "description": "读取 Redis 单个 key 的值，自动按实际类型展开（string 直接取值；hash 取字段；list/set/zset 取前若干元素；stream 取区间），并附带 TTL 与元素总数。key 不存在时返回 exists=false。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "source": {"type": "string", "description": "数据源名称（来自 win_db_list）"},
                "key": {"type": "string", "description": "要读取的 key"},
                "limit": {"type": "integer", "description": "集合类 key 最多展开多少元素，默认 50"},
            },
            "required": ["key"],
        },
    },
    {
        "name": "win_redis_write",
        "description": "在 Redis 上执行写命令（SET / DEL / EXPIRE / HSET / LPUSH 等）。需要数据源开启「允许写入」、服务端非只读、且使用完整权限 Token。危险命令（FLUSHALL、CONFIG、EVAL、KEYS 等）永远拒绝。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "source": {"type": "string", "description": "数据源名称（来自 win_db_list）"},
                "args": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "命令参数数组，如 [\"SET\",\"user:1\",\"tom\"]；推荐用这种写法，参数里带空格也不会被切错",
                },
                "command": {"type": "string", "description": "命令字符串（如 \"SET user:1 tom\"），按空白切分；仅在不含带空格参数时使用"},
            },
            "required": [],
        },
    },
    {
        "name": "win_exec",
        "description": "在远程服务器上执行命令（运维利器，但**默认关闭**：需要服务端把 exec_enabled 改为 true 并配置程序白名单）。"
                       "适合 composer 安装依赖、php 执行脚本、清缓存这类操作，省去「写临时脚本→web 触发→删脚本」的三步弯路。"
                       "白名单只查整条命令的首程序（去掉 cmd /c 前缀后第一个程序，php 等价 php.exe），"
                       "后面的参数、&&、||、| 管道、2>&1 重定向、带引号的字符串都原样交给 cmd 处理。"
                       "要跑一段脚本（引号嵌套难写）优先用 stdin 参数直喂，别硬拼 php -r \"...\"。"
                       "安全围栏：首程序必须在白名单 exec_allowed_commands 里（改白名单不用重启，"
                       "保存配置文件后下次调用即生效）；工作目录必须在授权根目录内；"
                       "超时默认 60 秒，服务端可配上限 exec_timeout_max_sec（默认 300），长任务请拆成小批。"
                       "围栏是尽力而为的静态检查，不是安全边界：cmd 的 if / for / 括号 / 重定向 / "
                       "变量展开无法穷举，已知不拦的写法见 README「已知限制」；仅给可信操作者使用。"
                       "输出超限时只回传「头 20 行 + 尾 40 行」压缩预览（output_truncated=true），"
                       "全文落盘到 output_file，用 win_tail 直接看尾部报错、win_read 看全文；"
                       "要更省 token 就传小一点的 max_output_bytes / head_lines / tail_lines。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "command": {"type": "string", "description": "要执行的命令，如 \"php -v\" 或 \"composer require phpspreadsheet\""},
                "cwd": {"type": "string", "description": "工作目录（必须在授权根目录内），默认第一个根目录"},
                "timeout": {"type": "integer", "description": "超时秒数，默认取服务端 exec_timeout_sec（通常 60）；上限由服务端 exec_timeout_max_sec 决定（默认 300），实际值见 initialize 的运行时契约。长任务请拆批，不要靠拉长超时硬扛"},
                "stdin": {"type": "string", "description": "喂给命令标准输入的文本（如整段 PHP/Python 脚本），免引号转义地狱"},
                "max_output_bytes": {"type": "integer", "description": "本次响应里最多带多少字节输出，默认取服务端 exec_max_output_bytes（通常 65536）；只能调小不能调大，用来省 token"},
                "head_lines": {"type": "integer", "description": "输出超限时预览保留开头多少行，默认 20，上限 500；启动上下文和早期报错在这里"},
                "tail_lines": {"type": "integer", "description": "输出超限时预览保留结尾多少行，默认 40，上限 500；命令真正的失败原因通常在最后几行"},
            },
            "required": ["command"],
        },
    },
    {
        "name": "win_validate",
        "description": "写前校验（不落盘）：一次检查路径白名单、乐观锁、old 是否命中、体积上限与目标文件格式语法。只读 Token 也能调用。建议在 win_patch / win_write 之前跑一次，避免写完才发现语法错误或 old 没命中。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "单文件模式：目标文件绝对路径"},
                "content": {"type": "string", "description": "单文件模式：打算整文件写入的内容"},
                "old": {"type": "string", "description": "单文件模式：打算精确替换的原文"},
                "new": {"type": "string", "description": "单文件模式：替换后的新文本"},
                "count": {"type": "integer", "description": "old 命中多处时限定替换次数；0 = 要求唯一命中"},
                "encoding": {"type": "string", "description": "编码，默认 utf-8"},
                "expected_sha256": {"type": "string", "description": "文件当前 sha256，用于确认没被别人改过"},
                "changes": {
                    "type": "array",
                    "description": "多文件模式：每项 {path, content 或 old/new, expected_sha256?}。与 path/content 单文件参数二选一",
                    "items": {"type": "object"},
                },
            },
        },
    },
    {
        "name": "win_patch",
        "description": "多文件原子补丁：全部文件预检通过才写入，任一文件失败就把已写的整体回滚。每项支持 content 整文件覆盖，或 old/new 精确替换（可带 expected_sha256 防并发覆盖）。一次改多个文件时优先用它，别逐个 win_write 留下半成品。失败或超时后不要盲目重试：先用 win_read(hash=true) 对照响应里的 original_sha256，或在下一次调用带 expected_sha256 让服务端拦下对已改文件的重复应用。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "changes": {
                    "type": "array",
                    "description": "补丁条目数组，每项 {path, content} 或 {path, old, new, count?, expected_sha256?, encoding?}；最多 20 项",
                    "items": {"type": "object"},
                },
                "dry_run": {"type": "boolean", "description": "只预览不落盘，返回每项校验结论与写入字节数，默认 false"},
                "backup": {"type": "boolean", "description": "每个文件写前备份，默认 true；传 false 可跳过（服务端 backup_required=true 时会被拒）"},
            },
            "required": ["changes"],
        },
    },
    {
        "name": "win_probe",
        "description": "服务自检：一次返回版本、配置分层来源、各授权目录可达性、审计日志健康（是否在降级缓冲）、备份目录与增量去重统计、TLS/exec/MCP 开关、被禁用工具与具体问题清单。只读、不写文件、不输出 Token。连不上或行为异常时先调它。",
        "inputSchema": {
            "type": "object",
            "properties": {},
        },
    },

]

# 工具名 -> (中文短标题, 风险标注)
# 与 TOOLS 一一对应：漏一个或写错名字会立刻抛 KeyError，不会静默降级成"未标注"。
_TOOL_META = {
    "win_roots":         ("列出可操作范围", _READ_ONLY),
    "win_disk":          ("查看磁盘分区",   _READ_ONLY),
    "win_list":          ("列出目录",       _READ_ONLY),
    "win_info":          ("查看路径详情",   _READ_ONLY),
    "win_read":          ("读取文本文件",   _READ_ONLY),
    "win_search":        ("按文件名查找",   _READ_ONLY),
    "win_grep":          ("搜索文件内容",   _READ_ONLY),
    "win_read_b64":      ("读取二进制文件", _READ_ONLY),
    "win_trash_list":    ("列出回收站",     _READ_ONLY),
    "win_tail":          ("读文件末尾",     _READ_ONLY),
    "win_tree":          ("查看目录树",     _READ_ONLY),
    "win_multi_read":    ("批量读取文件",   _READ_ONLY),
    "win_backup_list":   ("列出文件备份",   _READ_ONLY),
    "win_write":         ("写入/覆盖文件",  _WRITE),
    "win_write_b64":     ("写入二进制文件", _WRITE),
    "win_append":        ("追加文件内容",   _WRITE),
    "win_replace":       ("精确替换文本",   _WRITE),
    "win_trash":         ("删除到回收站",   _WRITE),
    "win_trash_restore": ("还原回收站条目", _WRITE),
    "win_backup_restore": ("回滚到备份版本", _WRITE),
    "win_move":          ("移动/重命名",    _WRITE),
    "win_mkdir":         ("创建目录",       _WRITE_IDEMPOTENT),
    "win_copy":          ("复制文件或目录", _WRITE_IDEMPOTENT),
    # ---- 数据库 / 缓存 ----
    "win_db_list":       ("列出数据源",     _READ_ONLY),
    "win_db_query":      ("执行只读查询",   _READ_ONLY),
    "win_db_tables":     ("列出数据表",     _READ_ONLY),
    "win_db_schema":     ("查看表结构",     _READ_ONLY),
    "win_db_test":       ("测试数据库连接", _READ_ONLY),
    "win_redis_info":    ("查看Redis概况",  _READ_ONLY),
    "win_redis_keys":    ("列出Redis键",    _READ_ONLY),
    "win_redis_get":     ("读取Redis键",    _READ_ONLY),
    # 写操作。注意 destructiveHint 保持 True：写 SQL 能删数据、写 Redis 能清 key，
    # 客户端据此弹确认框是应该的。
    "win_db_exec":       ("执行写SQL",      _WRITE),
    "win_redis_write":   ("执行Redis写命令", _WRITE),
    # win_exec 权限最高（远程命令执行），保持 destructiveHint=True：
    # 默认服务端不开 exec_enabled，客户端弹确认是完全应该的
    "win_exec":          ("执行远程命令",   _WRITE),
    "win_validate":      ("写前校验",       _READ_ONLY),
    "win_probe":         ("服务自检",       _READ_ONLY),
    "win_patch":         ("多文件原子补丁", _WRITE),
}

_unknown = sorted(set(_TOOL_META) - {t["name"] for t in TOOLS})
if _unknown:
    raise RuntimeError(f"_TOOL_META 里有 TOOLS 中不存在的工具：{_unknown}")

for _tool in TOOLS:
    _title, _ann = _TOOL_META[_tool["name"]]
    _tool["title"] = _title
    _tool["annotations"] = dict(_ann)
del _tool, _title, _ann

TOOL_MAP = {t["name"]: t for t in TOOLS}

# 只读工具集合（供服务端在只读模式下做一致性校验，以及给界面展示分类）
READ_ONLY_TOOLS = frozenset(
    t["name"] for t in TOOLS if t["annotations"]["readOnlyHint"]
)
WRITE_TOOLS = frozenset(t["name"] for t in TOOLS) - READ_ONLY_TOOLS

# 工具名 → 服务端接口名
ROUTE_MAP = {
    "win_roots": "roots",
    "win_disk": "disk",
    "win_list": "list",
    "win_info": "stat",
    "win_read": "read",
    "win_write": "write",
    "win_replace": "replace",
    "win_append": "append",
    "win_mkdir": "mkdir",
    "win_move": "move",
    "win_copy": "copy",
    "win_trash": "trash",
    "win_trash_list": "trash_list",
    "win_trash_restore": "trash_restore",
    "win_search": "search",
    "win_grep": "grep",
    "win_read_b64": "read_b64",
    "win_write_b64": "write_b64",
    "win_tail": "tail",
    "win_tree": "tree",
    "win_multi_read": "multi_read",
    "win_backup_list": "backup_list",
    "win_backup_restore": "backup_restore",
    # 数据库 / 缓存
    "win_db_list": "db_list",
    "win_db_query": "db_query",
    "win_db_exec": "db_exec",
    "win_db_tables": "db_tables",
    "win_db_schema": "db_schema",
    "win_db_test": "db_test",
    "win_redis_info": "redis_info",
    "win_redis_keys": "redis_keys",
    "win_redis_get": "redis_get",
    "win_redis_write": "redis_write",
    "win_exec": "exec",
    "win_validate": "validate",
    "win_patch": "patch",
    "win_probe": "probe",
}


# ---------------------------------------------------------------------------
# 结果渲染
# ---------------------------------------------------------------------------


def _display_width(text: str) -> int:
    """按终端里的实际显示宽度算长度 —— 中文/全角字符占两格。

    直接用 len() 会让含中文注释的表（很常见）整体错位。
    """
    import unicodedata

    return sum(2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1
               for ch in text)


def _pad(text: str, width: int) -> str:
    return text + " " * max(0, width - _display_width(text))


def _table_text(columns, rows, max_col_width: int = 40) -> str:
    """把查询结果渲染成对齐的文本表格 —— 比 JSON 更省 token，模型也更好读。"""
    if not columns:
        return ""
    cells = [[("NULL" if v is None else str(v)) for v in row] for row in (rows or [])]
    widths = []
    for i, name in enumerate(columns):
        width = _display_width(str(name))
        for row in cells:
            if i < len(row):
                width = max(width, min(_display_width(row[i]), max_col_width))
        widths.append(width)

    def fmt(values):
        out = []
        for i, value in enumerate(values):
            value = value.replace("\n", "\\n")
            # 截断也按显示宽度来，否则中文列还是会撑破对齐
            if _display_width(value) > max_col_width:
                trimmed = ""
                for ch in value:
                    if _display_width(trimmed + ch) > max_col_width - 1:
                        break
                    trimmed += ch
                value = trimmed + "…"
            out.append(_pad(value, widths[i]))
        return " | ".join(out)

    lines = [fmt([str(c) for c in columns]),
             "-+-".join("-" * w for w in widths)]
    lines += [fmt(row) for row in cells]
    return "\n".join(lines)


def render(tool_name: str, data: dict) -> str:
    """把服务返回的数据渲染成适合模型阅读的文本。"""
    if tool_name == "win_read":
        head = (
            f"{data.get('path')}  [{data.get('encoding')}, {data.get('size_human')}, "
            f"{'本段 ' if data.get('total_lines_partial') else ''}共 {data.get('total_lines')} 行, "
            f"本次第 {data.get('start_line')} 行起 "
            f"{data.get('returned_lines')} 行"
            f"{', 已截断' if data.get('truncated') else ''}]"
        )
        # v1.7.12：游标分两种——字节层截断必须用 offset 续读（行号游标
        # 在超限文件上够不着尾部，旧版这里会误导 AI 以为读完了）
        if data.get("next_offset"):
            head += (f"\n⚠️ 文件还有未读内容（字节层截断）：下次调用传 "
                     f"offset={data['next_offset']}"
                     f" + start_line={data.get('next_start_line')} 续读，直至 eof=true")
        elif data.get("next_start_line"):
            head += f"\n⚠️ 还有未读内容：下次调用传 start_line={data['next_start_line']} 续读"
        # v1.7.20：hash=true 时把全文件 sha256 也放进文本头——纯文本客户端
        # 不读 structuredContent，不写进这里等于这个能力对它们不存在。
        # 明确标「全文件」，避免 AI 误以为是本页内容的 hash。
        if "sha256" in data:
            if data.get("sha256"):
                head += (f"\n全文件 sha256={data['sha256']}"
                         "（写回时传给 expected_sha256 可防并发覆盖）")
            else:
                head += f"\n全文件 sha256 未计算：{data.get('sha256_note') or '文件超过哈希上限'}"
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
        lines = ["本工具在远程服务器上可操作的目录："]
        for r in data.get("roots", []):
            lines.append(f"  {r.get('path')}  {'(存在)' if r.get('exists') else '(不存在)'}")
        lines.append(
            f"只读模式：{data.get('read_only')}   允许删除：{data.get('allow_delete')}   "
            f"允许移动：{data.get('allow_move')}   写前自动备份：{data.get('auto_backup')}"
        )
        if data.get("token_scope") == "readonly":
            lines.append("注意：当前用的是**只读 Token**，只能查看，任何写入/删除都会被拒绝。")
        return "\n".join(lines)

    if tool_name == "win_tail":
        head = (
            f"{data.get('path')}  [{data.get('encoding')}, {data.get('size_human')}, "
            f"末尾 {data.get('returned_lines')} 行"
            f"{', 已截断' if data.get('truncated') else ''}]"
        )
        return head + "\n" + ("-" * 60) + "\n" + str(data.get("content", ""))

    if tool_name == "win_tree":
        lines = [
            f"目录树：{data.get('root')}   深度 {data.get('depth')}   "
            f"{data.get('dirs')} 个目录 / {data.get('files')} 个文件"
        ]
        for e in data.get("entries", []):
            pad = "  " * (int(e.get("depth") or 0) + 1)
            if e.get("type") == "dir":
                lines.append(f"{pad}[DIR ] {e.get('name')}/")
            else:
                size = e.get("size_human") or ""
                lines.append(f"{pad}[FILE] {size:>10}  {e.get('name')}")
        if data.get("truncated"):
            lines.append("  ...（已达条数上限，结果被截断）")
        if not data.get("entries"):
            lines.append("  （空目录）")
        return "\n".join(lines)

    if tool_name == "win_multi_read":
        lines = [
            f"批量读取 {data.get('count')} 个文件，成功 {data.get('ok_count')} 个，"
            f"合计 {data.get('total_bytes')} 字节"
        ]
        for r in data.get("results", []):
            if not r.get("ok"):
                lines.append(f"\n=== {r.get('path')}  [失败 {r.get('error')}] {r.get('message')}")
                continue
            flag = "（已截断）" if r.get("truncated") else ""
            lines.append(
                f"\n=== {r.get('resolved_path')}  [{r.get('encoding')}, {r.get('size')} 字节]{flag}"
            )
            lines.append("-" * 60)
            lines.append(str(r.get("content", "")))
        if data.get("skipped"):
            lines.append(f"\n（另有 {data.get('skipped')} 个文件超出单次数量上限，未读取）")
        return "\n".join(lines)

    if tool_name == "win_replace":
        if data.get("dry_run"):
            return (
                f"{data.get('path')}  预览（文件未改动）：将替换 {data.get('replaced')} 处\n"
                + "-" * 60 + "\n" + str(data.get("diff") or "（无差异）")
            )

    if tool_name == "win_exec":
        state = "超时被杀" if data.get("timed_out") else f"退出码 {data.get('exit_code')}"
        head = (
            f"命令：{data.get('command')}\n"
            f"目录：{data.get('cwd')}   {state}   耗时 {data.get('duration_ms')}ms"
        )
        if data.get("output_file"):
            head += (f"\n完整输出：{data.get('output_file')}（超限已落盘，"
                     "可用 win_read 读全文、win_tail 直接看尾部报错）")
        flag = "（输出已截断）" if data.get("output_truncated") else ""
        return head + "\n" + ("-" * 60) + flag + "\n" + str(data.get("output") or "（无输出）")

    if tool_name == "win_validate":
        lines = ["写前校验：" + ("全部通过" if data.get("ok") else "未通过")
                 + f"（共 {data.get('checked')} 项，磁盘未改动）"]
        for it in data.get("results", []):
            mark = "OK  " if it.get("ok") else "FAIL"
            lines.append(f"  {mark} {it.get('path')}")
            if not it.get("ok"):
                lines.append("       " + "；".join(it.get("errors") or []))
        for err in data.get("errors", []):
            lines.append(f"  FAIL {err}")
        return "\n".join(lines)

    if tool_name == "win_patch":
        if data.get("dry_run"):
            lines = [f"预览：{data.get('would_apply')} 个文件可应用，磁盘未改动（原子补丁）"]
            for it in data.get("changes", []):
                lines.append(f"  {it.get('path')}  {it.get('bytes')} 字节  "
                             f"{it.get('mode')}  替换 {it.get('replacements', 0)} 处")
            return "\n".join(lines)
        lines = [f"原子补丁已应用 {data.get('count')} 个文件（任一失败会整体回滚）："]
        for it in data.get("changes", []):
            lines.append(f"  {it.get('path')}  {it.get('bytes')} 字节  "
                         f"sha256={it.get('sha256')}  备份={it.get('backup') or '无'}")
        # v1.7.22：固定一行，不随文件数增长。写前摘要放 structuredContent，
        # 客户端重试前据此判断「这一处是不是已经改过了」。
        lines.append("重试前先用 win_read(hash=true) 核对；各项 original_sha256 / "
                     "original_bytes 见 structuredContent.changes。")
        return "\n".join(lines)

    if tool_name == "win_probe":
        lay = (data.get("config") or {}).get("layers") or {}
        by = lay.get("by_layer") or {}
        lines = [f"wkfile {data.get('version')} 自检："
                 + ("正常" if data.get("ok") else "发现问题") + f"  {data.get('time')}"]
        lines.append(f"配置分层 默认<文件<环境变量：默认 {by.get('default', 0)} 项 / "
                     f"文件 {by.get('file', 0)} 项 / 环境变量 {by.get('env', 0)} 项"
                     + (f"；生效 {', '.join(lay.get('env_applied') or [])}"
                        if lay.get("env_applied") else ""))
        for r in data.get("roots", []):
            lines.append(f"授权目录 {r.get('path')}  "
                         f"{'存在' if r.get('exists') else '不存在'}  "
                         f"{'可读' if r.get('readable') else '不可读'}  "
                         f"{'可写' if r.get('writable') else '不可写'}")
        a = data.get("audit") or {}
        lines.append(f"审计：{a.get('state')}  缓冲 {a.get('buffered')}/{a.get('buffer_max')} 条  "
                     f"失败 {a.get('failures')} 次  已补写 {a.get('flushed')} 条  "
                     f"丢弃 {a.get('dropped')} 条")
        # v1.7.23：丢账不能只躺在计数里。alerts 为空时这一行根本不出现，
        # 不给常态自检增加 token。
        if a.get("alerts"):
            lines.append("审计告警：" + "；".join(a["alerts"]))
        b = data.get("backup") or {}
        d = b.get("dedup") or {}
        lines.append(f"备份目录 {'可写' if b.get('writable') else '不可写'}；"
                     f"增量去重 {'开' if d.get('enabled') else '关'}  "
                     f"命中 {d.get('hits')} 次 / 省下 {d.get('saved_human')}")
        s = data.get("security") or {}
        lines.append(f"安全：只读={s.get('read_only')}  本次 Token={s.get('token_scope')}  "
                     f"TLS={s.get('tls')}  exec={s.get('exec_enabled')}  "
                     f"MCP={s.get('mcp_enabled')}")
        issues = data.get("issues") or []
        lines.append("问题清单：" + ("无" if not issues else ""))
        for msg in issues:
            lines.append(f"  - {msg}")
        return "\n".join(lines)

    if tool_name == "win_backup_list":
        # v1.7.18：策略快照也写进文本块 —— 只看 content[0].text 的客户端
        # 同样能看到「积了多少、配了什么」，不必去翻 structuredContent。
        pol = data.get("policy") or {}
        scope = f"（筛选：{data.get('filter')}）" if data.get("filter") else ""
        policy_line = ""
        if pol:
            policy_line = (
                f"\n保留策略：总量上限 {pol.get('max_bytes_human')} / 天龄 "
                f"{pol.get('retain_days')} 天 / 份数上限 {pol.get('retain_count')} 份"
                f"（{'已启用' if pol.get('enabled') else '全为 0，未启用'}）；"
                f"当前共 {pol.get('total_count')} 份、{pol.get('total_bytes_human')}，"
                f"按策略会清掉 {pol.get('would_remove')} 份。清理由服务端配置执行，"
                "本工具不提供删除。"
            )
            if int(pol.get("orphan_objects", 0) or 0) > 0:
                policy_line += (
                    f"\n另有 {pol.get('orphan_objects')} 个无版本引用的内容实体"
                    f"（{pol.get('orphan_bytes_human', '?')}）：清理时先回收，"
                    "不计入体积上限，也不会因此删掉有效备份。")
        if not data.get("items"):
            return (f"没有找到任何备份{scope}。备份目录：{data.get('backup_root')}"
                    + policy_line)
        lines = [
            f"共 {data.get('count')} 份备份，目录：{data.get('backup_root')}" + policy_line
        ]
        for it in data.get("items", []):
            lines.append(
                f"  {it.get('stamp')}  {it.get('size_human'):>10}  "
                f"原文件 {it.get('original_path')}"
            )
            lines.append(f"      备份文件：{it.get('backup_path')}")
        if data.get("truncated"):
            lines.append("  ...（已达上限，结果被截断）")
        return "\n".join(lines)

    if tool_name == "win_backup_restore":
        if data.get("dry_run"):
            return (
                f"预览：将把 {data.get('target')} 回滚为备份 {data.get('backup_path')}"
                f"（目标当前{'存在' if data.get('target_exists') else '不存在'}）。磁盘未改动。"
            )
        lines = [f"已回滚：{data.get('target')}"]
        lines.append(f"  来源备份：{data.get('from_backup')}")
        if data.get("previous_version_backup"):
            lines.append(f"  回滚前的版本已另存：{data.get('previous_version_backup')}")
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

    if tool_name == "win_db_list":
        if not data.get("sources"):
            return ("还没有配置任何数据源。请在服务端的界面④「数据库」页添加一条，"
                    "或往 wkfile.config.json 的 databases 数组里填写。")
        lines = [f"已配置 {data.get('count')} 个数据源："]
        for s in data.get("sources", []):
            lines.append(
                f"  {s.get('name')}  [{s.get('type')}]  {s.get('dsn')}  "
                f"{'可写' if s.get('writable') else '只读'}"
            )
        if data.get("read_only_request"):
            lines.append("  注意：本次请求用的是只读 Token，所有数据源一律只读。")
        lines.append("  写权限需要同时满足：数据源开启允许写入 + 服务端非只读 + 使用主 Token。")
        return "\n".join(lines)

    if tool_name in ("win_db_query", "win_db_exec", "win_db_tables"):
        head = f"数据源 {data.get('source')}"
        if data.get("server_version"):
            head += f"（{data.get('server_version')}）"
        if data.get("rows") is not None:
            head += f"  返回 {data.get('row_count')} 行"
        if data.get("affected_rows") is not None:
            head += f"  影响 {data.get('affected_rows')} 行"
        if data.get("truncated"):
            head += "（已达行数上限，结果被截断，翻页请按下方模板改写 SQL）"
        body = _table_text(data.get("columns"), data.get("rows") or [])
        hint = data.get("page_hint")
        if hint:
            body += (f"\n\n翻页模板（{hint.get('dialect')}）：{hint.get('template', '')}"
                     f"\n下一页 OFFSET = {hint.get('next_offset')}"
                     f"\n{hint.get('note', '')}")
        return head + ("\n" + body if body else "")

    if tool_name == "win_db_schema":
        out = [f"表 {data.get('database')}.{data.get('table')} 的结构"]
        cols = data.get("columns") or {}
        out.append("\n列：")
        out.append(_table_text(cols.get("columns"), cols.get("rows") or []))
        idx = data.get("indexes") or {}
        if idx.get("rows"):
            out.append("\n索引：")
            out.append(_table_text(idx.get("columns"), idx.get("rows") or []))
        return "\n".join(out)

    if tool_name == "win_db_test":
        if not data.get("ok"):
            return f"连接失败：{data.get('error') or data}"
        out = [f"数据源 {data.get('source')} 连接正常（{data.get('dsn')}）",
               f"  服务端版本：{data.get('server_version')}",
               f"  认证方式：{data.get('auth_plugin')}",
               f"  当前身份：{data.get('identity')}"]
        if data.get("databases"):
            out.append(f"  可见的库（{len(data['databases'])} 个）："
                       + ", ".join(data["databases"]))
        return "\n".join(out)

    if tool_name == "win_redis_info":
        out = [f"数据源 {data.get('source')}（Redis {data.get('server_version')}）",
               f"  运行模式：{data.get('mode')}   客户端连接：{data.get('connected_clients')}",
               f"  内存占用：{data.get('used_memory_human')}"
               f"（上限 {data.get('maxmemory_human') or '未设置'}）",
               f"  累计命令数：{data.get('total_commands_processed')}"
               f"   当前库 key 数：{data.get('dbsize')}"]
        ks = data.get("keyspace") or {}
        if ks:
            out.append("  各库分布：" + "  ".join(f"{k}={v.split(',')[0]}"
                                                 for k, v in ks.items()))
        return "\n".join(out)

    if tool_name == "win_redis_keys":
        head = (f"数据源 {data.get('source')}  模式 {data.get('pattern')}  "
                f"返回 {data.get('count')} 个 key")
        if data.get("truncated"):
            head += f"（已截断，用 cursor={data.get('next_cursor')} 继续）"
        lines = [head]
        lines += [f"  {k}" for k in (data.get("keys") or [])]
        return "\n".join(lines)

    if tool_name == "win_redis_get":
        if not data.get("exists"):
            return f"{data.get('key')} 不存在。"
        head = (f"{data.get('key')}  [{data.get('type')}"
                f"{', 不过期' if data.get('ttl_sec') == -1 else f', TTL {data.get("ttl_sec")}s'}"
                f"{f', 共 {data.get("length")} 个元素' if data.get('length') is not None else ''}]")
        val = data.get("value")
        if isinstance(val, (dict, list)):
            body = json.dumps(val, ensure_ascii=False, indent=2)
        else:
            body = str(val)
        if data.get("truncated"):
            body += "\n（已截断，可用 limit 参数加大）"
        return head + "\n" + body

    if tool_name == "win_redis_write":
        return (f"数据源 {data.get('source')}  已执行：{data.get('command')}\n"
                f"  返回：{json.dumps(data.get('result'), ensure_ascii=False)}")

    return json.dumps(data, ensure_ascii=False, indent=2)


# ---------------------------------------------------------------------------
# JSON-RPC / MCP 协议构造
# ---------------------------------------------------------------------------


def ok_response(msg_id, result) -> dict:
    return {"jsonrpc": "2.0", "id": msg_id, "result": result}


def error_response(msg_id, code: int, message: str, data=None) -> dict:
    err = {"code": code, "message": message}
    if data is not None:
        err["data"] = data
    return {"jsonrpc": "2.0", "id": msg_id, "error": err}


def runtime_contract(info: dict) -> str:
    """按「当前这次握手的实际配置」生成给智能体的运行时契约。

    只写会随配置变、且 tools/list 的 schema 里查不到的约定；工具级细节仍以
    schema 为准。纯函数：不读文件、不联网、不持有服务端对象，单测可直接调。
    所有数字都从 info / 模块常量取，避免说明文字与实现各写一份而漂移。
    """
    info = info or {}
    limits = info.get("limits") or {}
    flags = info.get("flags") or {}
    read_max = human_bytes(limits.get("max_read_bytes"))
    write_max = human_bytes(limits.get("max_write_bytes"))
    omit = human_bytes(limits.get("structured_omit_bytes",
                                 STRUCTURED_CONTENT_OMIT_BYTES))
    upload = human_bytes(limits.get("upload_hint_bytes",
                                   BASE64_UPLOAD_HINT_BYTES))
    scope = info.get("token_scope") or "full"

    lines = [
        "【运行时契约·随本次连接的实际配置生成，以此为准】",
        f"版本 {info.get('version') or '?'} / 凭据权限 {scope}"
        "（readonly=只读 Token，写、删、执行会被拒绝）。",
        f"上限：单次读 {read_max}、单次写与请求体 {write_max}；"
        f"win_write_b64 超过 {upload} 请改用 POST /api/upload（原始字节 + sha256 校验）；"
        f"响应正文超过 {omit} 时 structuredContent 省略重复 content"
        "（标记 content_omitted），全文取 content[0].text。",
        "win_read 读完只看 eof=true，用 next_offset / next_start_line 续读；"
        "拼接多页正文时，仅当该页 content_ends_with_newline=true 才在页尾补一个 \\n"
        "（该字段说明本页末行在原文里是否由换行终止），否则直接接下一页；"
        "不要按 line_fragment / eof 猜。",
        "自动备份" + ("开" if flags.get("auto_backup", True) else "关")
        + "：备份失败时覆盖/替换/移动/回滚整体中止且原文件不动；"
          "写类工具均可传 backup=false 跳过。",
        ("删除走 win_trash（可恢复）；" if flags.get("allow_delete", True)
         else "当前禁止删除；")
        + "encoding 不接受 utf-8-sig（不静默写 BOM），请用 utf-8。",
        "win_db_query 的 max_rows 只限制响应行数（DB 仍算完整结果集）；"
        "truncated=true 时看 page_hint，按里面的方言模板改写 SQL 真分页，"
        "必须带稳定唯一 ORDER BY，否则翻页会重复或漏行。",
        "win_exec 输出超限只回头尾预览（output_truncated），全文在 output_file；"
        "win_tail 看尾报错，max_output_bytes 只能调小。",
    ]
    allowed = [str(x) for x in (flags.get("exec_allowed_commands") or [])]
    if flags.get("exec_enabled"):
        shown = "、".join(allowed[:6]) or "（空）"
        if len(allowed) > 6:
            shown += f" 等 {len(allowed)} 项"
        cap = flags.get("exec_timeout_max_sec", 300)
        sec = flags.get("exec_timeout_sec", 60)
        exec_line = (
            f"win_exec 已开启：白名单 {shown}；只查首程序；"
            f"超时 {sec} 秒、上限 {cap} 秒"
            + ("；严格链式校验开" if flags.get("exec_strict_chain") else "")
            + "。围栏不是安全边界，仅可信者用。"
        )
        lines.append(exec_line)
    else:
        lines.append("win_exec 默认关闭且当前未开启（tools/list 也不会列出该工具）。")
        exec_line = ""
    text = "\n".join(lines)
    # 契约有 1.5KB 护栏：长白名单 + 严格链式时明细会把整体撑爆。
    # 退化成「N 项」只丢白名单明细，其余契约（含限制提示）一字不动。
    if exec_line and len(text.encode("utf-8")) >= 1536:
        fallback = (
            f"win_exec 已开启：白名单 {len(allowed)} 项（明细略）；只查首程序；"
            f"超时 {sec} 秒、上限 {cap} 秒"
            + ("；严格链式校验开" if flags.get("exec_strict_chain") else "")
            + "。围栏不是安全边界，仅可信者用。"
        )
        text = text.replace(exec_line, fallback)
        if len(text.encode("utf-8")) >= 1536:
            text = text.replace(
                fallback, "win_exec 已开启。围栏不是安全边界，仅可信者用。")
    return text


def build_initialize(params: dict, *, info: dict) -> dict:
    """info 由服务端提供：server_name / version / roots / read_only /
    allow_delete / auto_backup / tools_disabled / token_scope / limits / flags"""
    want = (params or {}).get("protocolVersion")
    proto = want if want in SUPPORTED_PROTOCOLS else DEFAULT_PROTOCOL
    host = info.get("server_name") or "windows-server"
    roots = info.get("roots") or []

    lines = [
        "这些工具操作的是**远程 Windows 服务器**上的真实文件（服务器：" + host + "），不是本地文件。",
        "建议流程：",
        "1) 先调用 win_roots 确认可操作的目录范围；",
        "2) 用 win_list / win_search / win_grep 定位文件，不要凭猜测直接写；",
        "3) 改文件前先 win_read 看清当前内容，能用 win_replace 做精确替换就别整文件覆盖；",
        "4) 需要删除时用 win_trash（进服务器回收站，可恢复），它会返回 trash_id；",
        "5) 写入时服务端默认会先备份原文件（是否开启、能否跳过见下方运行时契约），"
        "返回里的 backup 字段就是备份路径；",
        "   改坏了可以用 win_backup_list 找到旧版本、win_backup_restore 回滚；",
        "6) 拿不准改动结果时，先用 win_replace 的 dry_run=true 看一眼 unified diff 预览再动手。",
        "路径一律使用服务器上的绝对路径，例如 D:\\share\\app\\config.ini。",
    ]
    if roots:
        lines.append("当前可操作的根目录：" + "；".join(str(r) for r in roots))
    if info.get("read_only"):
        lines.append("注意：服务当前是**只读模式**，写操作会被拒绝。")
    disabled = info.get("tools_disabled") or []
    if disabled:
        lines.append("注意：以下工具已被服务端配置禁用，不要调用：" + "、".join(str(x) for x in disabled))
    # v1.7.16：把「会随配置变的运行时约定」在握手时一次下发。协议必经、
    # 零额外调用，且不新增工具 / 不新增包内文件（README 是给人看的，也会漂移）。
    lines.append(runtime_contract(info))

    return {
        "protocolVersion": proto,
        "capabilities": {"tools": {"listChanged": False}},
        "serverInfo": {
            "name": MCP_SERVICE,
            "version": str(info.get("version") or "0"),
            "title": f"远程文件管理 · {host}",
        },
        "instructions": "\n".join(lines),
    }


def build_tools_list(*, disabled=()) -> dict:
    """disabled：被服务端配置禁用的工具名集合，直接从列表里摘掉。"""
    if not disabled:
        return {"tools": TOOLS}
    blocked = set(disabled)
    return {"tools": [t for t in TOOLS if t["name"] not in blocked]}


def call_tool(dispatch, params: dict, *, disabled=()) -> dict:
    """dispatch 是 Agent.dispatch：dispatch(action, payload) -> dict"""
    name = (params or {}).get("name") or ""
    args = (params or {}).get("arguments") or {}
    if not isinstance(args, dict):
        args = {}
    if name not in TOOL_MAP:
        return {
            "content": [{"type": "text", "text": f"未知工具：{name}"}],
            "isError": True,
        }
    if name in set(disabled):
        return {
            "content": [{
                "type": "text",
                "text": f"工具 {name} 当前不可用（被 tools_disable 禁用，或该能力未开启）。"
                        f"win_exec 需要在服务端配置 exec_enabled=true 并设置命令白名单；"
                        f"其它工具请在服务器界面的「安全」页检查后重启服务。",
            }],
            "isError": True,
        }
    action = ROUTE_MAP[name]
    payload = {k: v for k, v in args.items() if v is not None}
    # BOM 防误用：AI 给写文件工具传 utf-8-sig 十有八九是想"兼容 BOM"，
    # 实际效果是往 <?php / import 前塞 3 个 BOM 字节，直接把脚本写崩
    # （PHP Fatal: Namespace declaration statement has to be the very first...）。
    # 需要真正写 BOM 的场景让用户走服务端 API，MCP 面向 AI 一律拦下。
    if name in ("win_write", "win_append") and "encoding" in payload:
        if str(payload["encoding"]).strip().lower().replace("-", "").replace("_", "") == "utf8sig":
            return {
                "content": [{
                    "type": "text",
                    "text": "已拒绝：encoding=utf-8-sig 会把 BOM 写进文件开头，"
                            "PHP 等脚本会直接 Fatal。MCP 接口只允许无 BOM 编码"
                            "（推荐 utf-8；旧文件是 GBK 就传 gbk）。",
                }],
                "isError": True,
            }
    try:
        data = dispatch(action, payload)
        result = {"content": [{"type": "text", "text": render(name, data)}],
                  "isError": False}
        # MCP structuredContent（2025-06-18 规范字段，旧客户端自动忽略）：
        # v1.7.9 起给 win_exec 附机器可读字段（exit_code/duration_ms/output_file），
        # v1.7.11 起扩展到全部工具——win_read 的 next_start_line 游标等
        # 结构化字段直接可读，AI 不用再从文本头部解析
        # v1.7.13：正文只发一份。content[0].text 里已经有 render() 渲染的全文，
        # structuredContent 再原样带一遍 data["content"] 会把同一段大文本重复
        # 塞进响应（win_read 读满 4MB 就等于 8MB 开销）。大正文时只留结构化
        # 元数据，并显式标记；客户端仍能从文本块拿全文，旧客户端忽略该字段。
        sc = data
        if isinstance(data, dict):
            body = data.get("content")
            if isinstance(body, str):
                n = len(body.encode("utf-8", "replace"))
                if n > STRUCTURED_CONTENT_OMIT_BYTES:
                    sc = dict(data)
                    sc.pop("content", None)
                    sc["content_omitted"] = True
                    sc["content_bytes"] = n
        result["structuredContent"] = sc
        return result
    except Exception as exc:  # noqa: BLE001 —— 任何异常都要变成工具错误，不能让连接断
        msg = getattr(exc, "message", None) or str(exc)
        code = getattr(exc, "code", "") or exc.__class__.__name__
        return {
            "content": [{"type": "text", "text": f"调用失败 [{code}]：{msg}"}],
            "isError": True,
        }


def handle_message(dispatch, msg, *, info: dict, disabled=()):
    """
    处理单条 JSON-RPC 消息。
    返回响应 dict；返回 None 表示这是通知（notification），无需响应。
    """
    if not isinstance(msg, dict):
        return error_response(None, -32600, "无效的 JSON-RPC 消息")

    msg_id = msg.get("id")
    method = msg.get("method")
    params = msg.get("params") or {}

    # 客户端发来的通知 / 响应：不需要回复
    if msg_id is None and method is not None:
        return None
    if method is None:
        return error_response(msg_id, -32600, "缺少 method 字段")

    try:
        if method == "initialize":
            return ok_response(msg_id, build_initialize(params, info=info))
        if method in ("notifications/initialized", "initialized"):
            return None
        if method == "ping":
            return ok_response(msg_id, {})
        if method == "tools/list":
            return ok_response(msg_id, build_tools_list(disabled=disabled))
        if method == "tools/call":
            return ok_response(msg_id, call_tool(dispatch, params, disabled=disabled))
        if method == "logging/setLevel":
            return ok_response(msg_id, {})
        if method == "resources/list":
            return ok_response(msg_id, {"resources": []})
        if method == "prompts/list":
            return ok_response(msg_id, {"prompts": []})
        return error_response(msg_id, -32601, f"不支持的方法：{method}")
    except Exception as exc:  # noqa: BLE001
        return error_response(
            msg_id, -32603, f"服务端内部错误：{exc}",
            data=traceback.format_exc()[-800:],
        )


def handle_batch(dispatch, msgs: list, *, info: dict, disabled=()):
    """批量消息：返回响应列表（全部是通知时返回空列表）。"""
    out = []
    for m in msgs:
        r = handle_message(dispatch, m, info=info, disabled=disabled)
        if r is not None:
            out.append(r)
    return out
