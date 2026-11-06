# wkxm.md —— wkfile 项目指引

> 给下一个接手的人 / AI：**先读完这份再动代码**。
> 本文首选 2026-11-06 对 `server/` 源码的静态通读整理，2026-11-07 按 **v1.7.11 实际发布包**做了复核与实测。
> 行号以 v1.7.11（`VERSION = "1.7.11"`）源码为准，改动后要同步更新。
> 两条铁律：**只做分析与改文档的轮次不要顺手改代码/重建包**；**任何结论要么有源码行号，要么有可复现的实测命令**。

---

## 1. 项目是什么

wkfile（版本 **1.7.11**，`server/wkfile_agent.py:95`）是一个 **Windows 单文件文件操作 MCP 服务**：

- 一个 exe（`wkfile.exe`）里同时包含 **tkinter 图形界面** 和 **服务进程**；双击是界面，`--run` 是服务。
- 服务对外同时提供两套协议：
  1. **自有 JSON HTTP API**（`POST /api/<action>`，如 `/api/write`、`/api/list`），老客户端和自检脚本在用。
  2. **MCP**（`POST /mcp`，Streamable HTTP；另有 `GET /sse` + `POST /messages` 旧版 SSE 传输），给 Trae / Cursor / Claude Desktop 等客户端。
- 能力：读/写/追加/替换/列目录/搜索/grep/复制/移动/回收站/备份回滚/树/批量读/哈希/磁盘信息，外加 MySQL / MariaDB / Redis / SQL Server 查询，以及默认关闭的 `win_exec`。
- 当前工具数 **34**：只读 21 + 写入 13（`server/wkfile_mcp_endpoint.py` 的 `TOOLS`）。
- **硬约束：零第三方依赖，只用 Python 标准库**。MySQL 协议、Redis RESP、SQL Server TDS 7.4、RSA/ASN.1 自签证书都是手写的（见第 4 节）。不要因为"顺手"引入 pymysql / redis / pyodbc / cryptography，会破坏单文件绿色部署这条产品线。

运行环境：Windows + Python 3.8+（源码路线）/ 打包后的 exe 不需要 Python。

---

## 2. 一页架构

```
┌──────────────┐   Popen(--run)        ┌───────────────────────────────────────────┐
│ wkfile_gui   │ ────────────────────> │ wkfile_agent.py  main()                    │
│ (tkinter)    │ <──── /api/health ─── │  ├─ Config.load/save                      │
└──────────────┘                       │  ├─ Guard   (roots / 数据目录围栏)          │
                                       │  ├─ Audit   (JSONL 审计 + 轮转)            │
                                       │  ├─ Agent   (op_* 业务实现 + dispatch)     │
                                       │  └─ Handler (BaseHTTPRequestHandler)       │
                                       │        ├─ /api/*  → Agent.dispatch         │
                                       │        ├─ /mcp    → wkfile_mcp_endpoint    │
                                       │        └─ /sse    → 旧版 SSE 传输          │
                                       └──────────────┬────────────────────────────┘
                                                      │ 同一套 Agent/Guard
                    ┌─────────────────────────────────┼──────────────────────────┐
                    │                                 │                          │
             wkfile_mysql.py                   wkfile_redis.py            wkfile_sqlsrv.py
             (手写 MySQL 协议)                  (手写 RESP)                (手写 TDS 7.4)
                    │                                 │                          │
               MySQL/MariaDB                        Redis                 SQL Server 2008+
```

关键点：

- 所有写操作都走 `Agent.op_*`，MCP 层只是参数转换 + 结果渲染（`wkfile_mcp_endpoint.call_tool`），**不要在 MCP 层另写一套文件逻辑**。
- `Agent` 是单实例、长驻（`Handler.agent` 共享），因此 `_backup_cleanup_count`、`_exec_cfg_mtime` 这类实例状态是跨请求的；`ThreadingHTTPServer` 每个请求一个线程，**共享可变状态必须加锁**。
- 请求级"是否只读 Token"放在 `threading.local`（`set_request_mode`），每个成功鉴权的请求都会重设，不要在别处依赖它的默认值。

---

## 3. 目录与文件

```
server/                     源码（发布时只取里面的 .py）
  wkfile_agent.py           ★ 主程序：配置/路径围栏/备份/回收站/全部 op_*/HTTP/MCP 挂载/CLI
  wkfile_mcp_endpoint.py    ★ MCP 协议层：TOOLS 定义、call_tool、initialize/tools/list/tools/call
  wkfile_gui.py             ★ tkinter 界面：6 个页签、启停服务、Token/TLS/自签证书/数据库配置
  wkfile_mysql.py           手写 MySQL/MariaDB 客户端
  wkfile_redis.py           手写 Redis RESP 客户端
  wkfile_sqlsrv.py          手写 SQL Server TDS 客户端（含 TLS 包装）
  wkfile_selfcert.py        纯标准库 RSA + X.509 自签证书（含 SAN / 证书链）
  connect-check.py          连接自检（读本地配置里的 Token，按项探测 HTTP/MCP/TLS/数据库）
  install-server.ps1        纯 Python 路线的服务器部署脚本（仅 ASCII，PowerShell 5.1 兼容）
  wkfile.config.example.json 随包示例配置（注意：比 DEFAULT_CONFIG 旧，见第 12 节）
  连接自检.bat               调 connect-check.py 的包装
  wkfile/                   ★ 运行时数据目录（本地开发用，已被 .gitignore；内含真实 Token，别提交/别打包）
tests/                      集成测试（固定端口、临时目录，详见第 11 节）
tools/                      发布包里带的客户端小工具（upload / MCP 连通性 / 公网探测）
deprecated/                 已废弃的 SSH 隧道 + stdio 桥接方案，仅存档
docs/竞品学习与改造清单.md    v1.5.0~v1.6.4 的设计与改造记录（被 .gitignore 有意排除，只在本地）
.build/                     ★ 构建缓存 + 历史临时脚本（gitignore；含明文凭据，见第 12 节）
dist/wkfile.exe              PyInstaller 产物
wkfile-v1.x.y/ + .zip       历史发布包（12 个版本，gitignore，约占 290 MB）
selftest.py                 老版 HTTP API 自检（53 项左右）
build-exe.ps1               打包 exe
build-release.py            生成发布目录 + zip
启动界面.bat                 源码路线启动 GUI（pythonw）
```

Git 里只跟踪 33 个文件；`.build/`、`dist/`、`wkfile-v*/`、`*.zip`、`server/wkfile/`、`.trae/`、`logs/` 全部忽略。**不要把数据目录或历史发布包加进 git。**

---

## 4. 模块职责与实现要点

### 4.1 `wkfile_agent.py`（约 4000 行，核心）

| 区块 | 位置（v1.7.11 实测行号） | 说明 |
|---|---|---|
| 常量 / `DEFAULT_CONFIG` | 95-202 | 所有配置默认值。新增配置项必须同时改这里、GUI 表单、`wkfile.config.example.json`、README |
| `Config` | 1069-1103 | `load_config` 支持 `//` 行注释；**1.7.11 起 `save()` 是 tmp+replace 原子写，且用 `_extra_keys` 保留非默认键**（`:1085-1103`） |
| `Guard` | 1237-1325 | `resolve()` 做 roots 围栏 + 数据目录保护；`norm()` 只做 `normcase(abspath)`，**没有 realpath**；`_deny_data_dir` 保护列表仍缺 `tls`（`:1294-1297`） |
| `Audit` | 1327-1372 | JSONL 审计；轮转参数在构造时读取；`write()` 仍 `except OSError: pass`（`:1369-1372`） |
| `Agent` | 1404-3280 | 所有 `op_*`；`ROUTES` 是 action→方法映射；`dispatch` 统一记审计（`:3251`） |
| `_backup()` | 1531-1550 | 备份目录 `<stamp>-<ms>`；`except OSError: return None` **仍静默**（`:1545-1548`） |
| 回收站 | 约 2160-2260 | `wkfile/trash/<时间戳>-<6hex>/files/...` + `manifest.json`；`trash_retain_days` 清理 |
| `op_backup_list` | 约 2290-2320 | 列备份 / 按路径查备份 / 回滚；每次都扫全量备份目录（性能项） |
| `op_read` | 1776-1820 | **1.7.11 新增 `next_start_line`，但字节截断时游标会跳过残行（见第 9 节坑 1）** |
| `op_write` / `_atomic_write` | 1838-1903 | `op_write` 仍用固定 `full + ".wkfile-tmp"`（`:1871`）；`_atomic_write` 只在 b64/upload 用 |
| `op_replace` | 1989-2087 | 1.7.11 已拒绝截断文件（`:2000-2008`）；落盘仍是 `open(full,"wb")`（`:2078-2079`），**非原子** |
| exec 白名单 / 劫持围栏 | 2708-2804 | `_exec_head_program` :2708、`_exec_whitelisted` :2756、`_exec_guard_hijack` :2764；**链式命令只查首程序** |
| `_hotload_exec_cfg` | 2806-2826 | 仍裸 `json.load`（`:2821`），带 `//` 注释的配置解析失败 |
| `op_exec` | 2857-约 2990 | `cmd /d /s /c`；超时 taskkill 进程树；超长输出落盘 `logs/exec-*.txt` |
| `Handler` | 3282-3773 | 路由、鉴权、MCP 挂载、SSE、上传原始字节；`timeout = 120`（`:3290`）；`_read_body` 上限 64MB（`:3382`）；`_read_raw_body` 上限 `max_write_bytes`（`:3464-3483`） |
| `main()` | 3848-约 4070 | CLI：`--run` / `--config` / `--init` / `--gen-token` / `--gen-readonly-token` / `--renew-token` / `--gen-selfcert` / `--allow-unsafe` 等；启动前有弱 Token 拒绝闸（`:3945-3957`） |

### 4.2 `wkfile_mcp_endpoint.py`

- `TOOLS`：34 个工具的 name / title / description / inputSchema / annotations（`readOnlyHint` 等）。
- `ROUTE_MAP`：MCP 工具名 → Agent action；`_READ` / `_WRITE` 决定只读模式下的过滤。
- `call_tool()`：过滤 `None` 参数后直接 `dispatch`，**不做 JSON Schema 校验**；异常统一转成 `isError`，错误数据里带 traceback 末尾 800 字符。
- `handle_message()` 支持单条和批量 JSON-RPC；`initialize` 返回 `serverInfo.name = "wkfile-mcp"`，协议版本 `2025-06-18`。
- 工具在 `tools/list` 中是**构造时快照**：`read_only`、`tools_disable`、`exec_enabled` 改了要重启才反映到清单。

### 4.3 `wkfile_gui.py`

- 页签：① 状态 ② 服务/Token ③ HTTPS/证书 ④ 数据库 ⑤ 其它（审计、日志、自启）⑥ 关于。
- 启停服务：`subprocess.Popen([exe, "--run", "--config", ...], cwd=数据目录, DETACHED_PROCESS|CREATE_NO_WINDOW)`；PID 写 `wkfile.pid`。
- **启动前**会检查端口占用 + `/api/health`；**停止时**优先用 PID，PID 文件缺失时退化为 `taskkill /IM wkfile.exe`（有坑，见 12 节）。
- 保存配置时直接覆盖 `wkfile.config.json`；配置损坏时回退到默认值并提示（会丢 Token/roots 的风险，见 12 节）。

### 4.4 数据库三件套

- **MySQL**：手写握手 + `caching_sha2_password`（含 RSA-OAEP，注意 MySQL 用 **SHA-1** 作为 OAEP 摘要，不是 SHA-256，这是踩过的坑）。
- **Redis**：手写 RESP2；命令分 read/write，`CONFIG`/`EVAL` 等危险命令在黑名单；`allow_write=false` 时只放行读命令。
- **SQL Server**：手写 TDS 7.4（PRELOGIN/LOGIN7/SQL batch）；仅 SQL 身份验证；TLS 默认不校验证书（内网自签场景）。
- 三者的权限是**三重闸**：`cfg.read_only`（服务级） + 请求 Token 是否只读 + 数据源 `allow_write`。三者任一为真/假限制，写就被拒。
- **每次查询新建连接**，没有连接池（见 12 节性能项）。

---

## 5. 运行方式与关键默认值

```powershell
# 源码跑服务（开发）
python server\wkfile_agent.py --run --config server\wkfile\wkfile.config.json

# 源码跑界面
python server\wkfile_gui.py        # 或双击 启动界面.bat

# 打包
powershell -ExecutionPolicy Bypass -File .\build-exe.ps1
python build-release.py

# 生成 Token / 证书
wkfile.exe --gen-token
wkfile.exe --gen-selfcert --cn wkfile --ip 1.2.3.4
```

默认值（`DEFAULT_CONFIG`）：

| 项 | 默认 | 备注 |
|---|---|---|
| `host` / `port` | `0.0.0.0` / `8788` | TLS 开启后是**同一个端口**，不是 8789 |
| `token` | 空，首次启动自动生成（`secrets.token_urlsafe(36)`，48 个 base64url 字符，无前缀） | 非 loopback + 空 Token 拒绝启动；`:3845-3850` |
| `readonly_token` | 空 | 用它认证的请求一律只读 |
| `roots` | `[]` 时必须配置才能用 | 服务只允许 roots 内的路径 |
| `auto_backup` | `true` | 覆盖/移动/删除前备份 |
| `max_read_bytes` | 4 MB | 读文件上限 |
| `max_write_bytes` | 16 MB | 写/上传上限 |
| `max_list_entries` | 3000 | 列表上限 |
| `grep_extensions` | 约 50 种文本扩展名 | 不在名单里的文件 grep 会跳过（除非显式关闭） |
| `backup_max_bytes` | 2 GB | 超了从最旧备份目录整目录删 |
| `trash_retain_days` | 30 | 回收站保留天数 |
| `audit_max_bytes` / `audit_backup_count` | 5 MB / 3 | |
| `run_log_max_bytes` / `run_log_backup_count` | 2 MB / 3 | |
| `exec_enabled` | `false` | 打开后才有 `win_exec` 工具 |
| `exec_timeout_sec` / `exec_max_output_bytes` | 60 s / 64 KB | 超时 `taskkill /T /F`；超输出落盘 |
| `preflight_check` | `true` | 写前语法校验 `.py/.json/.jsonl/.xml/.toml` |
| `public_health` | `true` | `/health` 免鉴权，只返回服务名/版本 |
| `enable_mcp` | `true` | |

---

## 6. 数据目录与配置

默认数据目录（`data_dir()`）：

1. exe 所在目录本身就叫 `wkfile` → 直接用（不套娃）；
2. exe 同级的 `wkfile\`；
3. `%PROGRAMDATA%\wkfile\`（exe 装在 Program Files 等只读位置时的兜底）。

源码路线下 `server\wkfile\` 就是这个目录（本地已有真实 Token，勿提交）。

```
wkfile\
  wkfile.config.json     配置（含 Token / 数据库密码，权限敏感）
  wkfile.pid             服务 PID
  logs\                  wkfile.log（运行日志）、audit.jsonl（审计）、exec-*.txt
  backups\               覆盖前自动备份
  trash\                 回收站（manifest.json + files\）
  tls\                   自签证书 / fullchain.pem
```

**配置里的 `base_dir` 是"配置文件所在目录"**，不一定是 `data_dir()`。`--config D:\share\cfg\wkfile.config.json` 时，日志/备份/回收站/TLS 都在 `D:\share\cfg` 下，而 `data_dir()` 仍是 exe 旁的 `wkfile\`。这个差异会让部分围栏失效（见 12 节）。

配置特点：

- 自动生成的模板带 `// 注释`；`load_config` 会先剥注释再 `json.loads`。
- `save()` 只写 `DEFAULT_CONFIG` 的键，手写的额外键在保存后会消失。
- `--renew-token` / GUI 保存都会重写整个文件。
- 修改 `databases`、`roots`、`read_only` 等**需要重启**；只有 `exec_allowed_commands` / `exec_timeout_sec` / `exec_max_output_bytes` / `exec_strict_chain` 是设计上的热加载键。

---

## 7. 核心约定（改代码必须遵守）

1. **路径一律先过 `guard.resolve(path)`**，不要直接 `os.path.*` 用户传入的路径。写操作前还要 `_require_write()`。
2. **错误统一抛 `PathError(code, message)`**，由 Handler 映射成 HTTP/JSON-RPC 错误。不要让 `FileExistsError` / `PermissionError` 裸露成 500。
3. **任何覆盖/移动/删除前先 `_backup()`**（除非请求显式 `backup=false` 且该操作支持）。备份目录结构不能改，`win_backup_list/restore` 依赖它。
4. **写文件先 `_preflight()` 再备份，再原子落盘**（`tmp + os.replace`）。注意 `op_write_b64`、`op_replace` 目前不是原子的（12 节）。
5. **只读三层**：服务级 `read_only`、请求级只读 Token、数据源 `allow_write`。新增写工具时必须同时检查前两层。
6. **新增 MCP 工具要改三处**：`TOOLS`（含 annotations）、`ROUTE_MAP`/`_WRITE` 集合、Agent 的 `ROUTES` + `op_*`；只读工具必须标 `readOnlyHint: True`，否则客户端会当破坏性工具。
7. **审计**：新 action 通过 `dispatch` 自动记账；直接走 `op_*`（测试/内部）不会记。不要让异常绕过审计。
8. **编码**：读用 `decode_text`（自动 UTF-8/GBK/latin-1），写默认 UTF-8；`win_replace` 会沿用原文件编码。`subprocess` / 日志统一 `PYTHONIOENCODING=utf-8`。
9. **不要给 `wkfile.exe` 增加需要第三方 DLL 的功能**；PyInstaller onefile 体积和"绿色部署"是这个项目的卖点。
10. **数据目录内的文件永远不要通过 API 暴露**（配置含 Token）。

---

## 8. 安全围栏全景

- **Token**：`Authorization: Bearer <token>`；`secrets.compare_digest`；失败 20 次/300 秒锁定；长度/占位符有弱口令警告；`token_ttl_days` 控制过期；`readonly_token` 只读。
- **IP 白名单**：`ip_allowlist`，只认 socket 对端地址，**不读 `X-Forwarded-For`**（反代场景会失效）。
- **启动闸**：非 loopback + 空 Token → 拒绝启动（`main()`）；`--allow-unsafe` 可绕过。
- **roots 围栏**：`Guard.resolve` 要求目标在某个 root 内；拒绝 `\\?\`、路径中的 `:`（ADS）。
- **数据目录**：`wkfile.config.json`、`pid`、`logs`、`backups`、`trash`、`base/wkfile` 被 `_deny_data_dir` 挡住；默认布局下整个 `base` 都挡。
- **工具级禁用**：`tools_disable` + `read_only` 在 MCP 层过滤。
- **删除/移动开关**：`allow_delete` / `allow_move`。
- **写前校验**：`preflight_check`（语法）。
- **exec**：默认关；白名单只查首程序；`exec_strict_chain=true` 恢复逐段检查；超时杀进程树；输出落盘。
- **审计**：所有 API/MCP 调用 JSONL，含结果/耗时/错误；轮转保留。

**注意：这是"API 层围栏"，不是操作系统沙箱。** AI 拿到的权限 = 服务账号权限；roots 只是限制 API 能碰的路径。README 明确写了这一点，别在文档里说成"沙箱"。

---

## 9. 已踩过的坑（真实记录，改相关代码前先看）

> 标注规则：**已修 / 部分修 / 未修** 都是对 **v1.7.11** 的判断；"实测"指用发布包 exe 跑出来的结果。

1. **【v1.7.11 新坑，未修】`win_read` 的 `next_start_line` 在字节截断时会丢内容**：`read_text_file` 按**字节**截断（`:616-623`，`fh.read(limit+1)` 后 `raw[:limit]`），`op_read` 再把截断后的文本 `splitlines()`，于是最后一行是**残行**却被算进 `returned_lines/total_lines`，`next_start_line = start-1+len(selected)+1`（`:1806-1818`）直接跳过这行剩下的几十万字符。
   - 实测（v1.7.11 exe，4,403,100 字节、4100 行的文件，默认 4MB）：返回 `returned_lines=4001, total_lines=4001, truncated=true, next_start_line=4002`；按 4002 续读得到 0 行，文件尾部标记再也没有出现。
   - 正确做法：游标应按**字节偏移**给（`next_offset`），或在截断时把最后一行**排除**在 `selected` 之外并让游标回到该行；`total_lines` 也不能用截断前缀的行数冒充全文行数。
2. **【已修】`win_replace` 大文件截断**：`op_replace` 现在接住 `truncated` 并直接报 `too_large`（`:2000-2008`），实测 >4MB 文件返回 `too_large`、文件未被裁掉。**但落盘仍是非原子**（`:2078-2079`）。
3. **【已修】`_read_raw_body` 重复定义**：现在只有一个定义（`:3464-3483`），上限统一为 `max_write_bytes`（默认 16MB），超限走 413 语义。
4. **【未修】配置里带 `//` 注释 → exec 热加载永远不生效**：`_hotload_exec_cfg` 仍是裸 `json.load`（`:2821`），而自动生成的配置带注释；解析失败静默返回且已记录 mtime，之后不再重试。`load_config` 才有剥注释逻辑。
5. **【未修】`norm()` 不是 realpath**（`:579` 附近，docstring 却写 realpath）：root 是 junction / 符号链接 / 8.3 短名时，`resolve` 用 realpath 得到的目标与 root 的 abspath 前缀对不上 → 误报 `out_of_scope`；`_deny_data_dir` 的保护路径比较也会错位。
6. **【未修】`search/grep/tree` 不走逐条 `guard.resolve`**：只按目录名过滤 `wkfile`，既会扫 `.git/.venv`，也可能把 `--config` 放在 roots 内时的 `logs/backups/tls` 读出来，或顺着 junction 出 roots。
7. **【v1.7.11 新坑，部分修】exec 第七重围栏只挡"首程序"，链式命令可绕过**：`_exec_guard_hijack`（`:2764-2798`）只检查 `_exec_head_program`；`exec_strict_chain=false`（默认）时 `dir && payload.bat` 的首程序是 `dir`，白名单通过，后面的 `payload.bat` 从 cwd 直接执行。
   - **实测**（v1.7.11 exe，`exec_allowed_commands=["dir"]`，share 目录放 `payload.bat`）：`win_exec("dir && payload.bat")` 返回 exit=0，输出里出现 `CHAIN_EXECUTED_MARKER`。
   - 同一围栏对 **cmd 内建命令会误报**：cwd 里有 `echo.bat` 时 `win_exec("echo x")` 被拒，但 cmd 内建 `echo` 根本不会被该文件劫持（`tests/mcp_exe_test.py` 新增用例把这种误报当成了预期行为）。
   - 对**绝对路径程序**也有误判面：`program="C:\php\php.exe"` 时 `os.path.join(cwd, "C:\php\php.bat")` 直接返回绝对路径，等于去查程序自己目录里的同名 `.bat`。
   - 结论：想真正挡住，必须把"整条命令里所有会从 cwd 解析的可执行名"都过一遍（或强制 `NoDefaultCurrentDirectoryInExePath=1` + 绝对路径解析 + 禁止 `-c/-r` 类解释器直执行）。
8. **测试差点删系统临时目录**：`tests/features_e2e.py:624-627` 曾多套一层 `dirname`，`rmtree` 到系统 TEMP；现在只删自己那一层，并加了注释。写清理逻辑务必只删自己创建的目录。
9. **SQL Server TDS 解析**：LOGIN7 的 TLS 包长度、COLMETADATA/ROW 的 token 对齐、`sys.databases` 的类型边界都踩过；`.build/sqlsrv_*` 就是当时的探针，别删（但里面有真实凭据，见 12 节）。
10. **MySQL `caching_sha2_password`**：RSA 加密口令时 OAEP 摘要必须是 SHA-1，用 SHA-256 服务器会拒。
11. **TLS 自签证书**：浏览器/客户端要求 SAN（IP/DNS），只有 CN 会报 `NET::ERR_CERT_COMMON_NAME_INVALID`；三件套（cert + chain + key）要拼 `fullchain.pem` 再喂 TLS。
12. **`cmd` 引号/复合命令**：v1.7.7 的逐段白名单把 `set a=b&&php -v`、`2>&1`、带引号参数全误伤；v1.7.8 改成只查首程序，并加了 `exec_strict_chain` 回退开关。**这条设计就是第 7 条绕过面的来源**。
13. **时间戳精度**：备份目录只到毫秒，同一毫秒并发写同一文件会撞目录；回收站用秒 + 6 hex 区分。
14. **Windows 编码**：PowerShell 5.1 读无 BOM 的 `.ps1` 按 ANSI/GBK，非 ASCII 会乱码甚至解析失败——`install-server.ps1` 因此全英文。`.bat` 里统一 `chcp 65001` + `PYTHONIOENCODING/PYTHONUTF8`。
15. **`rmtree` 与只读文件**：Windows 上回收站/备份清理可能因只读属性失败；现有代码用 `ignore_errors=True`，会留下残目录。
16. **`wkfile.exe` 的 `--check-gui`**：用于无界面自检；PyInstaller onefile 会先解包到临时目录，bootloader 父进程常驻，不能用"exe 进程数"判断服务是否在跑。
17. **数据目录探测有副作用**：`data_dir()` 会尝试创建目录并写探针文件（实测会生成并回收 `.wk-write-test`）；不要在只读环境频繁调用。
18. **【v1.7.11 新坑】示例配置里的占位 Token 不在弱口令黑名单里**：`WEAK_TOKENS` 里放的是旧文案「在这里填一个长随机串，或运行 python wkfile_agent.py --init 自动生成」（`wkfile_agent.py:966`），而随包 `wkfile.config.example.json` 已改成「在这里填一个长随机串；用图形界面启动时点「生成」按钮即可」——文案不同，`token_strength` 判为 `ok`，**监听 0.0.0.0 也能启动**（实测 `--run` 正常起服务）。
   - 好在这串含中文，HTTP header 传不进去（`Invoke-WebRequest` 直接报"无效的控制字符"），所以更像"用户永远连不上"的坑，而不是直接鉴权绕过。
   - 但 README 第 7 节写的"Token 是示例占位值会被直接拒绝启动"（`README.md:191`、`:625`）对**当前随包示例**不成立；修法是让示例 Token 进 `WEAK_TOKENS`，或直接把它设成空串。
19. **【v1.7.11 新坑】MCP `structuredContent` 让 `win_read` 响应翻倍**：`call_tool` 对所有工具都塞 `structuredContent = data`（`wkfile_mcp_endpoint.py:1046-1050`），而 `render()` 的文本里已经包含 `content`；读一个 4MB 文件，MCP 响应体约 8MB，客户端容易超限/变慢。
20. **【v1.7.11 文档坑】发布包里的文档与产品不一致**：`README.md` 仍写"刻意没有提供任何执行工具"（约 665/675 行）、"请求体 32 MB"（649 行）、"227 用例"与"207/207"同页（115/718 行）；`使用说明.txt` 第 187 行写"请求体上限统一 16MB"，实际 JSON API 是 64MB。详见第 16 节。
21. **【v1.7.13 教训】断言全绿 ≠ 契约正确**：`win_read` 的 A/D 段断言（`sum(consumed)==size`、`sum(returned_lines)==行数`、`L3/L4=3/4`）在"换行符恰好压在读取窗口最后一个字节"时**全部通过**，但客户端看到多一个空行、行号整体错位（第 3 页 content 以 `\n` 开头）。
   - 复现条件：文件 `b"L1\n" + b"A"*N + b"\n" + b"L3\nL4\n"`，且 `max_bytes = max_read_bytes = N`，按 `next_offset` 逐页续读 → 边界页被误判成 `line_fragment=true`，换行符留到下一页，`splitlines()` 凭空产生空串。
   - 教训：分页断言必须再加两条——「逐页 content == 该页字节区间去掉行尾换行」「任何一页 content 都不得以 `\n` 开头」；只比对总量会放过错位。见 `test/v1712/spotcheck.py` E 段。

---

## 10. 构建与发布

```
build-exe.ps1
  └─ .build\venv 里装 PyInstaller → dist\wkfile.exe（--onefile --windowed，带图标/版本资源）

build-release.py
  ├─ 从 server/wkfile_agent.py 读 VERSION
  ├─ 清空并重建 wkfile-v<VERSION>\
  ├─ 拷 wkfile.exe / README / 快速上手 / 示例配置 / connect-check / 连接自检.bat / tools/
  ├─ 生成 使用说明.txt、文件夹说明.txt、wkfile\数据目录说明.txt
  ├─ 敏感信息扫描（真实 IP / 域名 / 用户目录等）
  └─ 打 wkfile-v<VERSION>.zip（UTF-8 文件名标志）
```

**版本号目前有三处，必须手动对齐**：

1. `server/wkfile_agent.py:95` `VERSION = "1.7.11"`（发布目录/zip 名、界面标题、`/health` 都读它）——**唯一真实来源**
2. `build-exe.ps1:29` `$AppVer = "1.7.11"` —— **仍是死变量，没有任何地方使用**
3. `build-exe.ps1` 里内联的 `filevers=(1,6,0,0)` 仍是旧值，但实际 `--version-file` 由 `.build/version_server.txt` 生成；v1.7.11 实测 exe 的 FileVersion/ProductVersion 都是 **1.7.11.0**，所以那段内联模板是死代码（别被它误导）

**v1.7.11 发布包实测补充**（详见第 16 节）：

- `build-release.py` 打 zip 时 `arc = relpath(full, folder)`（`:285`），**zip 内没有顶层 `wkfile-v1.7.11/` 目录**，解压会把 13 个文件直接倒在当前目录。
- `LAYOUT`（`:45-56`）里**没有 LICENSE**，但包内 README 的目录树却写了 `LICENSE # MIT`；根目录有 LICENSE（1072 字节）。
- `build-release.py` 只检查 `wkfile.config.json` / `agent.config.json` / `mcp.json` 三种文件名（`:297-308`），**不校验 exe 新旧、不扫绝对路径/内网信息/凭据**。
- `README.md` 是整份从开发仓库拷过去的，包含 `server\`、`dist\`、`tests\`、`.trae\mcp.json`（"含真实 Token，勿提交 git"）等**用户侧不该看到的开发结构**。

发布前检查清单：

- [ ] `VERSION` 与 `build-exe.ps1` 的可执行路径对齐（`$AppVer`/内联 `filevers` 建议删掉，统一从 `VERSION` 注入）
- [ ] exe 是**本次源码重新打的**（`build-release.py` 不校验 exe 版本/时间戳，旧 exe 会被装进新版本目录）
- [ ] `dist\wkfile.exe` 的文件属性版本 = `VERSION`（v1.7.11 实测为 1.7.11.0，已对上）
- [ ] 发布目录不含 `wkfile.config.json`、Token、证书私钥、`.build/`
- [ ] 包内文档与产品一致：工具数、默认值、请求体上限、是否暴露 `win_exec`、用例数（见第 16 节清单）
- [ ] `wkfile.config.example.json` 与 `DEFAULT_CONFIG` 键集对齐（当前缺 `author` / `backup_max_bytes` / `trash_retain_days` / `grep_extensions`）
- [ ] zip 里带顶层目录（现在没有），或明确在文档里写"解压到空目录"
- [ ] 决定 LICENSE 是否随包（README 承诺 MIT，包里没有）
- [ ] `git tag v1.x.y` 并推送（仓库仍没有 tag）

---

## 11. 测试地图

| 脚本 | 覆盖 | 运行方式 / 注意 |
|---|---|---|
| `selftest.py` | 老版 HTTP API（约 53 项）+ 已废弃 stdio 桥接的 MCP 冒烟 | `python selftest.py`；固定端口 **8899**；用 `_selftest` 临时目录；`--keep` 保留 |
| `tests/features_e2e.py` | 主回归（200+ 项）：围栏、备份、回收站、编码、TLS 校验、只读、exec、数据库错误路径等 | `python tests\features_e2e.py`；大量 `mkdtemp`；固定端口在文件顶部 |
| `tests/mcp_e2e.py` | MCP 协议（初始化/工具/批量/SSE/错误） | 固定端口，需先起测试服务（脚本内自己起） |
| `tests/local_e2e.py` | 本机直连小回归 | 固定端口 |
| `tests/mcp_exe_test.py` | 对 `dist\wkfile.exe` 的黑盒测试 | **先 `build-exe.ps1`**，否则测的是旧 exe |
| `tests/tls_e2e.py` | HTTPS + 证书链 + 自签 | 需要可用证书/openssl 环境 |
| `tests/sqlsrv_proto_test.py` | TDS 协议离线用例 | 不需要真实 SQL Server |
| `tools/upload.py` | 上传原始字节到 `/api/upload` | 发布包自带 |
| `tools/mcp-call-test.py` / `mcp-public-test.py` / `probe-public.py` | MCP/公网连通性 | 发布包自带 |

约定与坑：

- 测试**全部用固定端口**（8815/8790/8799/8899/18789/18796/18801 等），**不能并行跑**；跑之前确认没有残留进程。
- 大多数测试自己 `mkdtemp` 并在末尾清理；`selftest.py --keep`、`features_e2e` 失败时会保留临时目录便于排障。
- 测试会写临时配置，**不要指向真实数据目录**。
- 没有一个统一的 `pytest` 入口；每个脚本自己 `SystemExit(非0)`。CI 里要逐个跑。
- `.build/` 下有同名的**旧副本**（`features_e2e.py`、`mcp_e2e.py`、`mcp_exe_test.py`、`local_e2e.py`、`tls_e2e.py`），与 `tests/` 已经分叉（`mcp_exe_test.py` 差了 12 KB），**以 `tests/` 为准，不要跑 `.build/` 的**。

---

## 12. 高风险项状态（按 v1.7.11 复核）

> 结论口径：**已修 / 部分修 / 未修**；证据优先给 v1.7.11 源码行号，其次给发布包 exe 实测结果。
> 2026-11-06 的原始清单保留编号，避免和设计文档里的 B1-B7 失联。

### P0（不改会出事）

| # | 问题 | 状态 | 证据 |
|---|---|---|---|
| P0-1 | 明文凭据（`.build`） | **部分修** | SQL 探针多数已改读环境变量；`.build/sqlsrv_variants.py:18` 仍有 18 位字面口令；`.build/gh_askpass.sh` 仍含 GitHub PAT 明文（111 字节）。**仍需轮换 + 清理** |
| P0-2 | `win_replace` >4MB 截断 | **已修** | `wkfile_agent.py:2000-2008` 接住 `truncated` → `too_large`；exe 实测不再裁文件 |
| P0-3 | exec 白名单 cwd 劫持 | **部分修（仍可绕过）** | 新增 `_exec_guard_hijack`（`:2764-2798`）挡住直接同名劫持；但只查首程序，实测 `win_exec("dir && payload.bat")` 在 `exec_allowed_commands=["dir"]` 下成功执行 cwd 里的 `payload.bat`；对 cmd 内建（`echo`）是误报 |
| P0-4 | 超时 / 无界线程 / 超大 body | **部分修** | `Handler.timeout=120`（`:3290`）已加；`ThreadingHTTPServer` 仍无并发上限（`:3978-3979`）；JSON API `_read_body` 仍 64MB（`:3382`）；MCP/upload 走 `_read_raw_body` = `max_write_bytes`（`:3477`） |
| P0-5 | GUI 停止误杀 | **已修** | `wkfile_gui.py` `cmd_stop` 已按 PID 校验、检查 `taskkill` 返回码，不再 `/IM wkfile.exe` |

### P1（高优先级）

| # | 问题 | 状态 | 证据 |
|---|---|---|---|
| 6 | 数据目录围栏缺 `tls` | **未修** | `_deny_data_dir` 保护列表仍只有 PID/LOG/BACKUP/TRASH/APP（`:1294-1297`）；`--config` 放别处时 `tls\` 可被读 |
| 6b | `search/grep/tree` 逐条 resolve | **未修** | `op_tree:2427`、`op_search:2540`、`op_grep:2592` 仍只按目录名过滤 |
| 7 | `_backup` 失败静默 | **未修** | `except OSError: return None`（`:1545-1548`） |
| 8 | 临时文件名固定 | **部分修** | `op_write` 仍 `full + ".wkfile-tmp"`（`:1871`）；`write_b64`/`upload` 已改 `_atomic_write`（`:1885-1902`） |
| 9 | `write_b64` 非原子 | **已修** | `:1931` 调 `_atomic_write` |
| 10 | `op_append` 无上限/全量备份 | **未修** | `:1977-1987` 仍 `open(...,"ab")`、不校验 `max_write_bytes`、每次 `_backup` 全文件 |
| 11 | `Config.save` 非原子/丢键 | **已修** | `:1085-1103` tmp+replace，`_extra_keys` 保留自定义键 |
| 12 | exec 热加载解析注释失败 | **未修** | 仍裸 `json.load`（`:2821`）；失败前已更新 `_exec_cfg_mtime` |
| 13 | `win_read` 默认行数契约 | **未修（且更糟）** | `wkfile_mcp_endpoint.py:112` 仍写"默认 400"，实现默认 0=全部；v1.7.11 新增的 `next_start_line` 在字节截断时丢内容（第 9 节坑 1） |
| 14 | `sha256=true` 无大小上限 | **未修** | `op_stat:1763` → `sha256_of:626` 仍整文件流式读完，无阈值 |
| 15 | `_read_raw_body` 重复定义 | **已修** | 只剩一个定义（`:3464-3483`） |
| 16 | 审计写失败静默 | **未修** | `Audit.write` 仍 `except OSError: pass`（`:1369-1372`） |

### 本轮新增的高风险项（v1.7.11 引入）

| # | 问题 | 证据 |
|---|---|---|
| N1 | `win_read` 游标在 4MB 字节截断时跳过残行，尾部内容永久读不到 | `:616-623` + `:1806-1818`；exe 实测 `next_start_line=4002` 后 0 行 |
| N2 | exec 链式命令绕过第七重围栏 | exe 实测 `dir && payload.bat` 成功，见第 9 节坑 7 |
| N3 | MCP `structuredContent` 让 `win_read` 响应约翻倍 | `wkfile_mcp_endpoint.py:1046-1050` |
| N4 | 示例配置占位 Token 不在弱口令黑名单，0.0.0.0 也能启动 | `wkfile_agent.py:966` vs `wkfile.config.example.json:4`；exe 实测 |
| N5 | 发布包文档与产品矛盾（"没有执行工具"、32MB/16MB、227/207） | 第 16 节清单 |

---

## 13. 改动前 checklist

- [ ] 已读本文件第 7、9、12 节。
- [ ] 改动在现有成果上做，不另起一套；临时测试脚本放 `test/` 或 `tests/`，用完清理。
- [ ] 新增/修改脚本已登记到 `jiaoben.md`。
- [ ] 新增配置项：`DEFAULT_CONFIG` + `wkfile.config.example.json` + GUI 表单 + README 四处同步（当前示例已落后 4 个键，见第 16 节）。
- [ ] 新增 MCP 工具：`TOOLS`（含 annotations）+ `ROUTE_MAP`/`_READ`/`_WRITE` + `Agent.ROUTES` + `op_*` 四处同步。
- [ ] 写路径仍走 `guard.resolve` + `_require_write`；覆盖仍先 `_backup`；落盘仍 `tmp + os.replace`（`op_write`/`op_replace` 还没做到）。
- [ ] 改了版本相关：agent `VERSION`、`build-exe.ps1`、README/使用说明 版本号与"本次更新"条目一致。
- [ ] 改了能力/默认值/上限：同步 README 第 7、9 节与 `使用说明.txt`（历史上最容易漂移）。
- [ ] 已跑至少 `python selftest.py` + `python tests\features_e2e.py` + `python tests\mcp_exe_test.py`（串行，先确认端口空闲）。
- [ ] 发布前按第 10 节清单 + 第 16 节"包内自检"过一遍。
- [ ] 没有把 `server/wkfile/`、Token、证书私钥、`.build` 带进提交或发布包。

---

## 14. 常用命令速查

```powershell
# 查端口占用
Get-NetTCPConnection -LocalPort 8788 -ErrorAction SilentlyContinue | Select-Object OwningProcess
# 查服务健康
curl.exe -s http://127.0.0.1:8788/health
# 带 Token 调 API
curl.exe -s -H "Authorization: Bearer <token>" -H "Content-Type: application/json" `
  -d '{"path":"D:\\share\\a.txt"}' http://127.0.0.1:8788/api/read
# 源码启动服务
python server\wkfile_agent.py --run --config server\wkfile\wkfile.config.json
# 看最近审计
Get-Content server\wkfile\logs\audit.jsonl -Tail 20
```

---

## 15. 相关设计文档

| 文档 | 内容 | 状态 |
|---|---|---|
| [docs/智能体改代码能力设计.md](docs/智能体改代码能力设计.md) | 不放开 `win_exec` 的前提下，AI 改远程代码的工具级方案：现状 12 个卡点（含行号）、`win_read` 游标、`win_patch`、`win_diff_apply`、`win_validate`、`win_tx` 契约、三处同步清单、与 `win_exec` 的攻击面/失败模式对比、分阶段落地与测试覆盖；第 12 节为调试四层（L0 只读诊断 / L1 静态校验 / L2 预登记 probe / L3 `win_exec`），含 probe 信任边界与 Job Object 资源闸 | 设计稿，未实现 |

> 该文档里的 B1-B7 与本文第 12 节的高风险项对应；实现任何一条后，请同步更新本文第 4、7、9、12 节与 `jiaoben.md`。
>
> **v1.7.11 对方案的影响**：`win_read` 已加游标字段（但实现有 bug，方案里的"按字节 `next_offset`"仍然必要）；`win_replace` 已拒绝截断文件（方案 4.6 的前置条件已满足）；`win_write_b64`/`upload` 已原子化（4.7 可降级）；`Config.save` 已原子化（配置层不再是 blocker）；但 `op_write`/`op_replace` 仍非原子，`win_tx` 的"提交阶段用 `os.replace`"依然要自己实现。exec 侧第七重围栏证明"只查首程序"不够，方案里"不放开 `win_exec`、用 `win_patch`/`win_tx` 替代"的论证反而更强了。

---

## 16. 发布包（v1.7.11）说明与自检

> 研究对象：`D:\deving\bigmodelstudy\trae-winfile\wkfile-v1.7.11\`（13 个文件，约 12.14 MB）及其同名 zip。
> 原则：**能对 exe 实测的就实测，测不了的明确写"需源码确认"**，不要用源码结论替代包内事实。

### 16.1 包结构

```
wkfile-v1.7.11\
├─ wkfile.exe                 12,593,153 字节；FileVersion/ProductVersion = 1.7.11.0
├─ README.md                  55 KB（整份从开发仓库拷贝，含 server/dist/tests 结构）
├─ 使用说明.txt                27 KB（含 1.7.11 更新说明 7 条）
├─ 文件夹说明.txt               7.8 KB（部署包导览）
├─ wkfile.config.example.json  1.8 KB（36 个键）
├─ connect-check.py            25 KB（只读；靠"自己旁边"找数据目录）
├─ 连接自检.bat / 以管理员身份启动.bat
├─ tools\ (4 个：probe-public / mcp-public-test / mcp-call-test / upload)
└─ wkfile\数据目录说明.txt       （空数据目录，仅说明）
```

- zip 与目录内容一致：`wkfile.exe` SHA256 相同（`f46d9dd6…a82d`），zip 11.82 MB，共 13 个条目。
- **zip 内没有顶层 `wkfile-v1.7.11/` 目录**（`build-release.py:285` 用相对路径做 arcname），解压会散落到当前目录。
- **没有 LICENSE / CHANGELOG / VERSION 文件**；根目录有 LICENSE（MIT，1072 字节），但 `LAYOUT` 没带进包。
- 二进制扫描无 `deving` / `kanghui` / `github_pat_` / `ghp_` / `C:\Users\wk200` 明文；包内无日志、备份、回收站、真实配置、`.build`、证书私钥。**卫生总体合格。**

### 16.2 发布包问题清单（分级）

| 级别 | 问题 | 包内可验证 / 需源码 | 证据 |
|---|---|---|---|
| P0 | README 安全章节仍称"本服务刻意没有提供任何执行工具""本工具比它们多的是：没有执行工具" | **包内可验证** | `README.md:665`、`:675`；同包 `使用说明.txt` 1.7.7 段和 `win_exec` 实际存在（exec 开启后 `tools/list`=34） |
| P1 | `使用说明.txt` 称"请求体上限统一 16MB"不实 | **包内可验证** | JSON API 40MB JSON → HTTP 200；MCP 17MB → 连接被重置（WinError 10053），不是干净的 413；源码 `_read_body` 64MB（`:3382`）、`_read_raw_body`=`max_write_bytes`（`:3477`） |
| P1 | README 称"请求体 32MB"，与上一条、与实现三方不一致 | **包内可验证** | `README.md:649` |
| P1 | README 同页写"227 用例"和"207/207 全通过" | **包内可验证** | `README.md:115`、`:718` |
| P1 | 示例配置可被原样启动：`0.0.0.0` + 已知占位 Token + `allow_delete/allow_move=true`；README 承诺的"示例占位值拒绝启动"不生效 | **包内可验证（启动成功）+ 需源码确认判定逻辑** | 示例 `token` 文案 ≠ `WEAK_TOKENS`（`wkfile_agent.py:966`）；实测 `--run` 正常起服务 |
| P1 | 示例配置落后 `DEFAULT_CONFIG` 4 个键：`author` / `backup_max_bytes` / `trash_retain_days` / `grep_extensions` | **需源码确认**（键集对比） | `DEFAULT_CONFIG` 40 键 vs 示例 36 键；缺的键会回落到默认值，但用户看不到这些选项 |
| P1 | `tools\mcp-call-test.py` 无参直接 `IndexError`，没有用法提示 | **包内可验证** | 实测 `sys.argv[1]` 越界；脚本内只在 docstring 写用法 |
| P1 | `probe-public.py` / `mcp-public-test.py` 无参时直接对占位地址 `1.2.3.4` 发起真实探测（probe 还会 ping 4 次），既不提示也不快速失败 | **包内可验证** | 实测无参运行会等超时；`upload.py` 用 argparse 则表现正常 |
| P2 | README 混入开发仓库结构（`server\`、`dist\`、`tests\`、`.trae\mcp.json`"含真实 Token，勿提交 git"），对最终用户是噪音/误导 | **包内可验证** | `README.md` 第 2 节目录树、`:84-130` |
| P2 | README 承诺 `LICENSE # MIT`，包里没有 LICENSE | **包内可验证** | `README.md` 目录树 vs 包内 13 个文件 |
| P2 | README 出现开发机绝对路径和业务目录名：`d:\deving\kh\app\xxx.php`、`E:\kanghui\app\xxx.php` | **包内可验证** | `README.md:392-393`（示例用途，但泄露内部命名） |
| P2 | `文件夹说明.txt` 的 `tools\` 只列了 3 个脚本，实际有 4 个（漏 `upload.py`） | **包内可验证** | `文件夹说明.txt` 文件说明段 |
| P2 | 没有 CHANGELOG/VERSION 文件，版本只能看 exe 属性或 `文件夹说明` 标题 | **包内可验证** | 包内文件清单 |
| P2 | zip 无顶层目录，解压易与现有文件混在一起 | **包内可验证** | zip 条目为 `README.md`、`wkfile.exe`…（无前缀） |

### 16.3 关键 P0 项的 exe 侧实测结果

| 项 | 实测结果 | 结论 |
|---|---|---|
| `win_read` 默认行数 | 默认返回全部 1000 行（不是 MCP 描述里的 400） | **契约不符**；`max_lines` 描述需改，实际默认 0=全部 |
| `win_read` >4MB 续读 | `returned_lines=4001, total_lines=4001, next_start_line=4002`；按 4002 续读 0 行，尾部标记未出现 | **v1.7.11 新回归**（第 9 节坑 1） |
| `win_replace` 大文件 | 返回 `too_large`，文件未被截断 | **已修** |
| 请求体上限 | JSON API 40MB → 200；MCP 17MB → 连接重置（10053） | **未统一**，且 MCP 超限不是干净 413 |
| `win_exec` 是否暴露 | 默认 33 个工具（`win_exec` 隐藏）；`exec_enabled=true` 后 34 个且可调用 | **存在且可开启**，与 README"没有执行工具"矛盾 |
| exec cwd 劫持围栏 | `exec_allowed_commands=["dir"]` 下 `dir && payload.bat` 成功执行 cwd 里的 `payload.bat` | **可绕过**（第 9 节坑 7） |

**需要源码/额外环境才能确认的**：无界线程上限、`tls` 目录围栏、`_backup` 静默失败、`append` 体积上限、`hash` 无上限、审计吞异常、GUI 停止服务行为（需 GUI 交互）、`structuredContent` 对具体客户端的影响。

### 16.4 包内脚本可运行性

- `connect-check.py`：**干净环境可运行**（无服务时给出 `[FAIL] 端口 8788 连不上`、`[WARN] 没找到 wkfile.config.json`、防火墙/MCP 建议），仅标准库、只读、无硬编码 Token。默认端口 8788 是模板值，符合文档。
- `tools\*.py`：仅标准库；默认目标是 `1.2.3.4` / `192.168.1.10` 占位，无真实凭据；`upload.py --help` 正常，其余三个无参健壮性差（见 16.2）。
- 包内 `tools\` 4 个脚本与根目录 `tools\` 的 SHA256 **完全一致**；`connect-check.py` 与 `server\connect-check.py` 也完全一致。

### 16.5 包内与源码一致性

- 一致：exe 行为（`too_large`、33/34 工具、MCP 16MB、游标 bug、`structuredContent`）与 v1.7.11 源码对应；exe 版本 1.7.11.0 与 `VERSION="1.7.11"` 一致。
- 不一致：**文档**。包内 `README.md` 是开发仓库的旧文档（"没有执行工具"、"32MB"、227/207），与 1.7.11 的实际能力脱节；`使用说明.txt` 的 1.7.11 更新条目有 2 条不准确（"统一 16MB"、"游标可靠"）。

### 16.6 复核方法（可复现）

```powershell
# 1) 复制 exe 到临时目录，写测试配置（临时端口，别动生产服务）
#    host=127.0.0.1  port=18911  roots=临时share  exec_enabled=true
# 2) 启动
Start-Process .\wkfile.exe -ArgumentList '--run','--config',".\wkfile\wkfile.config.json"
# 3) 健康检查
curl.exe -s http://127.0.0.1:18911/health
# 4) 用包内脚本调 MCP
python tools\mcp-call-test.py http://127.0.0.1:18911 <token> win_roots
# 5) 结束后 Stop-Process 掉临时实例
```

> 注意：exe 是 windowed 编译，直接双击/无参启动是 GUI；要跑服务必须 `--run --config <path>`。
> 临时测试配置请放 `test\` 下，复测完删除；不要改 `server\wkfile\` 里的真实配置。

---

## 17. 协作通道（collab/）

本项目支持两个 AI 通过**文件信箱**协作。**v1.1 分工（2026-11-07）**：reviewer 承担定位、写补丁、写测试、跑验证、写文档、打包前自检；workbuddy 因积分有限，只做“最小复核包”审阅（按行号看指定区间，回 accept / reject + 证据行号）。

- 协议：[collab/protocol.md](collab/protocol.md)（角色权限、命名、必填字段、状态机、幂等、成本约束、最小复核包、文件级认领）。
- 台账：[collab/ledger.md](collab/ledger.md)（本轮基线、文件认领、三条版本路线、12+1 条全量状态）。
- 收件箱：`collab/inbox/to-workbuddy/`（reviewer 写）、`collab/inbox/to-reviewer/`（workbuddy 写）。
- 归档：`collab/archive/<TASK-ID>/`。
- 复核包模板：[collab/templates/MSG-TEMPLATE-001.template.md](collab/templates/MSG-TEMPLATE-001.template.md)（非投递，id 不复用真实消息）。
- 已投递：`collab/inbox/to-workbuddy/MSG-WK-001-001.md`（WK-001，status=open）。

**接入方式**：新 AI 先读 `protocol.md` + `ledger.md` + 两个 inbox 的 `README.md`，只写自己方向的目录，投递前按 `id` 查重。**改产品代码前必须在 ledger 登记文件认领**，未认领不得修改。
**成本约束**：单 issue 推进、动手前只读本文件相关节、复核只做增量 diff、消息结论式（结论 + 行号 + 待办），不贴整段源码。
**传输无关**：通道只依赖“投递 / 列收件箱 / 读取 / 改状态”四个原语；若对方只能走 API/CLI，只替换 `protocol.md` 第 12 节的适配层，消息格式与状态机不变。
**边界**：`collab/` 不进发布包，消息里不写凭据/内网信息。
