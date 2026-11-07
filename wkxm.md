# wkxm.md —— wkfile 项目指引

> 给下一个接手的人 / AI：**先读完这份再动代码**。
> 本文首选 2026-11-06 对 `server/` 源码的静态通读整理；2026-11-07/08 按 v1.7.16/v1.7.21/v1.7.22/v1.7.23/v1.7.24/v1.7.25/v1.7.26/v1.7.27/v1.7.28 九次复核，最后一轮基线是 **v1.7.27 → v1.7.28**。
> 行号以 v1.7.29（`VERSION = "1.7.29"`，`wkfile_agent.py:111`）源码为准，改动后要同步更新。
> **v1.7.28 复核结论摘要**：包真伪全过（14 文件逐个哈希、50/50 键、CRCRLF=0、LICENSE 1072 B、exe 1.7.28.0）；exe 包内**原地**实测 `/health`=1.7.28、`tools/list`=36、`/api/patch` 200 无 `original`、`/mcp win_patch` 200 `isError=false`；并把 exe 二进制本身跑热加载写 `0`，确认 `/api/health.hotload_warnings` 有值、`echo hi && whoami` 变 200 —— 证明 1.7.28 逻辑确实进了包，不只是版本号。**头号验收通过**：热加载 `exec_strict_chain` 的 `0`/`"0"`/`"false"`/`1`/`"true"` 全部**先规范化成布尔再写**（0/`"false"`→False、1/`"true"`→True），且告警**同时进 run log、`/api/health.hotload_warnings`（数量+键名）与 `/api/probe.issues`+`config.hotload_warnings`**；`null`/`"garbage"`/`2` 丢弃保留旧值并告警——不再有"静默关/静默无效"。**回归消红**：`tests/v1724_test.py` 由 43/47 → **51/51**，4 条改为**断言行为**（不再 monkeypatch 内部函数），`_prune_backup_objects` 死代码已删除。**两套 0 语义统一**：`patch_max_total_bytes=0` 与 `patch_max_rollback_bytes=0` 都回退内置 8MB（9MB 补丁 400「超过预算 8.0 MB」），README/使用说明都已写明"0 永不表示不限"；**残余**：probe 只为 rollback 报 issue，total=0 回退无告警。仍残留：`type .\deploy.ps1`/`copy .\payload.bat x` 仍 400（`.\` 带路径属有意拦截，但 README:415 "加白名单就能放行" 的说法不成立）、`tls_cert:null` 仍静默关 TLS、`ip_allowlist:[""]` 仍全 403 无告警、`--init` 仍 exit 5、env `WKFILE_READ_ONLY=yes` 仍静默 True、`port` 写字符串已拒启但生成配置注释仍写"回退 8788"、备份每个版本目录仍 2 次遍历、`test_docs` 仍只捕获 211（README 里 195/195 不被识别）、README 回归状态段仍写 "v1.7.26 / v1724 47 项"、`build-release.py` 无测试门禁。详见坑 45-51 与第 16.0 节。
> 两条铁律：**只做分析与改文档的轮次不要顺手改代码/重建包**；**任何结论要么有源码行号，要么有可复现的实测命令**。

---

## 1. 项目是什么

wkfile（版本 **1.7.29**，`server/wkfile_agent.py:111`）是一个 **Windows 单文件文件操作 MCP 服务**：

- 一个 exe（`wkfile.exe`）里同时包含 **tkinter 图形界面** 和 **服务进程**；双击是界面，`--run` 是服务（源码路线 `wkfile_agent.py` 本身**不认** `--run`，由 `wkfile_gui.py` 接住再转调）。
- 服务对外同时提供两套协议：
  1. **自有 JSON HTTP API**（`POST /api/<action>`，如 `/api/write`、`/api/list`），老客户端和自检脚本在用。
  2. **MCP**（`POST /mcp`，Streamable HTTP；另有 `GET /sse` + `POST /messages` 旧版 SSE 传输），给 Trae / Cursor / Claude Desktop 等客户端。
- 能力：读/写/追加/替换/补丁/校验/环境探针/列目录/搜索/grep/复制/移动/回收站/备份回滚/树/批量读/哈希/磁盘信息，外加 MySQL / MariaDB / Redis / SQL Server 查询，以及默认关闭的 `win_exec`。
- 当前工具数 **37**（`win_exec` 默认关，`tools/list` 实际 36）：v1.7.19 新增 `win_validate` / `win_patch` / `win_probe` 三个（`wkfile_mcp_endpoint.py` 的 `TOOLS`，`len(TOOLS)=37` 已实测）。
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
  wkfile_core.py            ★ v1.7.19 抽出的纯逻辑：token 强度、配置分层合并、补丁计划。
                             **无 I/O、无第三方依赖**；exe 已确认把该模块打进去
  wkfile_mcp_endpoint.py    ★ MCP 协议层：TOOLS 定义、call_tool、initialize/tools/list/tools/call
  wkfile_gui.py             ★ tkinter 界面：6 个页签、启停服务、Token/TLS/自签证书/数据库配置
  wkfile_mysql.py           手写 MySQL/MariaDB 客户端
  wkfile_redis.py           手写 Redis RESP 客户端
  wkfile_sqlsrv.py          手写 SQL Server TDS 客户端（含 TLS 包装）
  wkfile_selfcert.py        纯标准库 RSA + X.509 自签证书（含 SAN / 证书链）
  connect-check.py          连接自检（读本地配置里的 Token，按项探测 HTTP/MCP/TLS/数据库）
  install-server.ps1        纯 Python 路线的服务器部署脚本（仅 ASCII，PowerShell 5.1 兼容）
  wkfile.config.example.json 随包示例配置（v1.7.25 实测与 DEFAULT_CONFIG **50/50** 对齐，键集与类型全一致）
  连接自检.bat               调 connect-check.py 的包装
  wkfile/                   ★ 运行时数据目录（本地开发用，已被 .gitignore；内含真实 Token，别提交/别打包）
tests/                      集成测试（固定端口、临时目录，详见第 11 节）
test/                       按版本分的临时复核脚本（v1712 / v1721，见 jiaoben.md 与第 11 节）
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

### 4.1 `wkfile_agent.py`（约 4700 行，核心）

| 区块 | 位置（v1.7.28 实测行号） | 说明 |
|---|---|---|
| 常量 / `DEFAULT_CONFIG` | 111（`VERSION`）/ 133 起（**50 键**） | 所有配置默认值。新增配置项必须同时改这里、GUI 表单、`wkfile.config.example.json`、README。v1.7.23 新增 `backup_required`（默认 false）；v1.7.26 把 `token_ttl_days` 默认值改成 `0.0`（float），避免 GUI 写入 `30.0` 被 int 校验判脏后回退 0 |
| `data_dir` / `backup_root` / `trash_root` | 286 / 317 / 323 | 数据目录三选一；备份/回收站都挂在数据目录下 |
| `_dir_size` | 329 | 直接累加文件大小，**不认硬链接**；只用于 `logical_size` 展示，真实占用由 `backup_inode_sizes` 按 inode 去重（`wkfile_core.py:445-476`） |
| `Config` | `__init__` 974 / `save` 1070 / `load_config` 1229 | 支持 `//` 行注释；环境变量层 `WKFILE_*`；`save()` tmp+replace 原子写并保留 `_extra_keys`。**安全开关**（`validate_safety_config`，`wkfile_core.py:488`）先于普通键校验：类型错 → `ConfigSafetyError` 拒启（exit 5），可识别写法规范化并记 `config_warnings`。**普通键门禁**（v1.7.27）：`port` 脏值/`null`/字符串 → 拒启；`host:null`/非法值 → 回落 `0.0.0.0` 但写 `config_warnings`（横幅+health+probe 都可见）；**`tls_cert:null` 仍无告警**（坑 43）。之后才走 `validate_config_types`（`wkfile_core.py:385`）对普通键回退默认值。⚠ 生成配置里 `port` 的注释（1199-1200）仍写"写字符串会回退 8788"，与 v1.7.27 起的拒启行为不符（坑 51） |
| `Guard` | 1277 起 | `resolve()` 做 roots 围栏 + 数据目录保护；`contains()`；`_deny_data_dir` 保护 `wkfile`、`logs`、`backups`、`trash`、`tls`、pid、配置文件 |
| `Audit` | 1357 起 | JSONL 审计；写失败进内存缓冲、恢复补写；`health()`（1449）回 `state` + `integrity`（lossy/intact）+ `alerts`（`dropped>0` 告警） |
| `Agent` | `__init__` 1539、`dispatch` 4473 | 所有 `op_*`；`dispatch` 统一记审计；`__init__` 里有一次启动清理（`cleanup_backups`；v1.7.24 起会先回收孤儿） |
| `_backup()` / `_backup_dedup` | `_require_backup` / `_backup` / `_backup_dedup` | 备份目录 `<stamp>-<ms>`；失败抛 `backup_failed`（fail-closed）。同卷走 `.objects/<sha256>` 硬链接去重（≤`backup_dedup_max_bytes`，默认 64MB）。**v1.7.28 已删除 `_prune_backup_objects` 死代码**，清理改为在 `cleanup_backups` 内联删 `self._backup_orphan_paths`（坑 46 关闭） |
| 备份计数/清理 | `_backup_entries` 1818、`cleanup_backups` 1885 | `physical` 只含被版本引用的 inode；回 `orphan_objects`/`orphan_bytes`/`unreclaimable_bytes`；plan 里 `fixed = total - reclaimable` 且**至少保留 1 份**。**`.objects` 只枚举一次**（1865-1872 记下孤儿路径复用）；但每个版本目录仍被 `os.walk`（1851）+ `_dir_size`（1882）各走一次（2 次/目录，坑 40 仍部分残留） |
| 补丁三件套 | `_patch_check` / `op_validate` / `_rollback_patch` / `_patch_rollback_budget` / `_patch_max_total_bytes` 2152 / `op_patch` | 批量 `replace`/`write`；单文件 `max_write_bytes`、合计 `patch_max_total_bytes`（**v1.7.28：0/负数 → 回退内置 8MB，0 永不表示不限**；9MB 单批实测 400「超过预算 8.0 MB」）、回滚预算 `patch_max_rollback_bytes`（同样 0/负数→8MB）、最多 20 文件；响应只给 `original_sha256`/`original_bytes`。**残留**：probe 只对 rollback<=0 报 issue，total<=0 回退无告警（坑 48） |
| `op_probe` | 2292 | 版本/上限/根目录/分层/审计健康/备份与去重；只读 Token 下 roots 打码 `<root#N>`、备份路径 `<redacted>`、清 `audit.last_error`；`issues` 已接 `dropped`/`patch_max_rollback_bytes<=0`/`backup_required` 冲突/`config_warnings`；v1.7.26 新增 `security` 块（effective `read_only`、`server_read_only`、`backup_required`、`exec_enabled` 等，不含 `allow_delete`/`allow_move`/`auto_backup` —— 去 `/api/roots` 读） |
| `op_roots` | 2379 | **故意不脱敏**：读 Token 也能拿全量绝对路径（README 明确这不是权限边界，坑 33）；要验证 `allow_delete="false"` 是否生效就看这里的 `allow_delete` |
| `op_stat` | 2501 | `sha256=true` 时按 `max_read_bytes` 封顶，超限明确报错 |
| `op_read` | **2519-2672** | 双游标 + `line_fragment`/`continues_previous_line` + `content_ends_with_newline`；本页硬上限 `max(1, min(显式 max_bytes, max_read_bytes))`；`eof` 是唯一"读完"判据 |
| `op_read_b64` | 2674 | 二进制分页（同样走 `max_read_bytes`） |
| `op_write` / `_atomic_write` | 2690 / 2751 | 均走原子写；临时名 `pid+随机后缀`；`_require_backup` 守 `backup_required` |
| `op_write_b64` / `op_upload` | 2770 / 2800 | 同样过 `_require_backup` 与原子写 |
| `op_append` | 2843 | 上限按「追加后总长」判；`_require_backup` 守强制备份 |
| `op_replace` | 2866 | 先拒截断文件；落盘前 `_require_backup` |
| 回收站 | `op_trash` 3036 / `op_trash_list` 3071 | `wkfile/trash/<时间戳>-<6hex>/files/...` + `manifest.json` + `trash_id`；`trash_retain_days` 清理（`cleanup_trash` 1954 附近） |
| `op_trash_restore` | 3085 | 只认新格式（`trash_id` + manifest），v1.3.x 的 `.winfile-trash` 读不了 |
| `_resolve_exec_output` | 3088 附近 | 只放行 `<数据目录>/logs/exec-*.txt` |
| `op_backup_list` / `op_backup_restore` | 3184 / 3248 | 列备份 / 回滚；回滚走 `copy2`，不会把硬链接带进业务目录；v1.7.24 起文本块会报孤儿个数与不可回收占用 |
| `op_tree` / `op_search` / `op_grep` | 3343 / 3464 / 3524 | 遍历时**逐条 `guard.contains`**，junction/symlink 越界条目被跳过 |
| exec 围栏 | `_exec_is_dynamic` 3733、`_exec_seg_programs` 3746、`_exec_hijack_probe` 3781、`_exec_guard_hijack` 3816 | v1.7.24 **明确降级为"尽力而为、不是安全边界"**：`if`/括号/重定向/变量展开不拦，docstring 与 README「已知限制」逐条写明。**v1.7.26/27/28 实测仍误伤 `type .\deploy.ps1` / `copy .\payload.bat x`**（收窄只覆盖裸文件名；v1.7.28 实测 `type payload.bat`/`copy payload.bat x` 200，带 `.\` 则 400，坑 26/49） |
| `_hotload_exec_cfg` / `_validate_hot_key` | `_EXEC_HOT_KEYS` 3883 附近、`_hotload_exec_cfg` 3883、`_validate_hot_key` 3947、`_record_hotload_warning` 3939 | **v1.7.28**：`exec_strict_chain` 一律先 `coerce_safety_bool` **规范化成布尔再写**（`0`/`"0"`/`"false"`/`False`→False；`1`/`"true"`/`True`→True），规范化动作记一条 `action=coerced` 告警；`null`/垃圾字符串/`2` 丢弃保留旧值，记 `action=skipped`。`exec_allowed_commands` 非数组仍丢弃。告警同时进 **run log + `/api/health.hotload_warnings`（count+keys）+ `/api/probe.issues` 与 `config.hotload_warnings`**（含 key/action/to/reason）。**残余**：`_hotload_warnings` 是**无上限 append**，反复改配置会让 probe 响应线性增长（坑 50） |
| `op_exec` | 4046 起 | 白名单只查整条命令首程序（除非 `exec_strict_chain=true` 逐段查）；超时 `taskkill /T /F`；输出落盘 + head/tail 预览。**带 `.\` 的脚本/文件引用仍被 cwd 同名检查拦下**（`type .\deploy.ps1` 即便 `type` 已在白名单也 400，坑 26） |
| `Handler` | 4441 附近 | 路由、鉴权、MCP 挂载、SSE、上传原始字节；`timeout = 120`；JSON 请求体上限 = `max_write_bytes`（`_read_body`）；原始字节 `_read_raw_body` |
| `_mcp_info` | 4705 附近 | 给 `initialize` 的运行时契约快照（上限/开关/白名单/Token 作用域/exec 边界说明）；**不含 config_warnings** |
| `_health_minimal` | 4969 | `public_health` 时给免鉴权 `/api/health`；v1.7.25 起回 `config_warnings` 的**数量 + 键名**（不回值）；**v1.7.28 起同结构新增 `hotload_warnings`（count+keys+hint）** |
| `do_POST` | 4935 附近 | HTTP 入口；`/mcp` 转 `wkfile_mcp_endpoint.handle_message`；**网络收尾（耗时写文件）不可省** |
| `main()` | 5187 | CLI：`--config` / `--init` / `--gen-token` / `--gen-readonly-token` / `--renew-token` / `--gen-selfcert` / `--allow-unsafe` 等；服务进程本身不认 `--run`（由 GUI 接住）；启动横幅打印 `config_warnings` 明细（`format_config_warnings`）；`ConfigSafetyError` → exit 5。**安全开关脏值仍会让 `--init`/`--renew-token` 失败**（v1.7.28 复测 exit 5，坑 43） |

### 4.2 `wkfile_mcp_endpoint.py`

- `TOOLS`：**37** 个工具（`win_exec` 默认关 → `tools/list` 36；v1.7.19 新增 `win_validate` / `win_patch` / `win_probe`）；`win_read` 的 schema 含 `max_bytes`（本页上限，硬顶为服务端 `max_read_bytes`）。
- `ROUTE_MAP`：MCP 工具名 → Agent action；`_READ` / `_WRITE` 决定只读模式下的过滤（`win_patch` 在写集合，`win_validate`/`win_probe` 在只读集合）。
- `call_tool()`（1275）：过滤 `None` 参数后直接 `dispatch`，**不做 JSON Schema 校验**；异常统一转成 `isError`，错误数据里带 traceback 末尾 800 字符。**它只负责"渲染文本"，不会替你验证 `structuredContent` 能否 JSON 序列化**——v1.7.21 的 `win_patch` 就死在这。
- `handle_message()`（1346）支持单条和批量 JSON-RPC；`initialize` 返回 `serverInfo`，instructions 末尾追加 `runtime_contract(info)`（1165 起，`initialize` 组装在 1223）：把实际读写上限、`win_exec` 是否开放、白名单程序、Token 作用域直接告诉客户端。v1.7.18~21 未改这段，契约仍是 1517 B（默认配置）。
- `structuredContent`：正文 ≤ `STRUCTURED_CONTENT_OMIT_BYTES`（4096，`wkfile_mcp_endpoint.py:33-35`）时重复一份；超过时**省略 `content` 并置 `content_omitted=true`**（1329-1344），大文件响应不再翻倍（v1.7.21 实测 9KB 正文时 `content_omitted=true`、JSON 总体 ≈1 倍）。
- 工具在 `tools/list` 中是**构造时快照**：`read_only`、`tools_disable`、`exec_enabled` 改了要重启才反映到清单。

### 4.3 `wkfile_gui.py`

- 页签：① 状态 ② 服务/Token ③ HTTPS/证书 ④ 数据库 ⑤ 其它（审计、日志、自启）⑥ 关于。
- 启停服务：`subprocess.Popen([exe, "--run", "--config", ...], cwd=数据目录, DETACHED_PROCESS|CREATE_NO_WINDOW)`；PID 写 `wkfile.pid`。
- **启动前**会检查端口占用（`netstat`/`tasklist` 放后台线程，`after(0)` 回主线程，`:1991`）+ `/api/health`；**停止时只按 PID `taskkill /PID <pid> /T /F`**（`:2577-2615`），v1.7.11 起去掉了 `/IM wkfile.exe` 兜底，不会再误杀 GUI 自己。
- 状态刷新是异步的（`refresh_status` `:2366` → `daemon` 线程探测 → `after(0)` 调 `_apply_status` `:2424`），`STATUS_MIN_INTERVAL = 0.4s`（`:204`）做合并节流，UI 线程不再直接跑网络探测。
- 成功/信息/警告只落状态条 + 日志，不弹模态框；只有错误与确认弹窗。

### 4.4 数据库三件套

- **MySQL**：手写握手 + `caching_sha2_password`（含 RSA-OAEP，注意 MySQL 用 **SHA-1** 作为 OAEP 摘要，不是 SHA-256，这是踩过的坑）。
- **Redis**：手写 RESP2；命令分 read/write，`CONFIG`/`EVAL` 等危险命令在黑名单；`allow_write=false` 时只放行读命令。
- **SQL Server**：手写 TDS 7.4（PRELOGIN/LOGIN7/SQL batch）；仅 SQL 身份验证；TLS 默认不校验证书（内网自签场景）。
- 三者的权限是**三重闸**：`cfg.read_only`（服务级） + 请求 Token 是否只读 + 数据源 `allow_write`。三者任一为真/假限制，写就被拒。
- **每次查询新建连接**，没有连接池（见 12 节性能项）。

---

## 5. 运行方式与关键默认值

```powershell
# 源码跑服务（开发）——注意：v1.7.21 的 wkfile_agent.py 自己不吃 --run
python server\wkfile_agent.py --config server\wkfile\wkfile.config.json

# 打包后的 exe 才用 --run（GUI 接住后转服务）
wkfile.exe --run --config D:\wkfile\wkfile.config.json

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
| `host` / `port` | `0.0.0.0` / `8788` | TLS 开启后是**同一个端口**，不是 8789。**v1.7.27**：`port` 脏值/`null` → 拒绝启动（exit 5）；`host:null`/非法值 → 回落 `0.0.0.0` 但写 `config_warnings`（实测横幅+health+probe 均可见）。**v1.7.28 实测**：`port` 写字符串 `"19110"` 同样 **exit 5 拒启**（不是回退 8788），但生成配置里的注释仍写"会回退到内置 8788"（坑 51） |
| `token` | 空，首次启动自动生成（`secrets.token_urlsafe(36)`，48 个 base64url 字符，无前缀） | 非 loopback + 空/弱 Token 拒绝启动（`main()` 5185-5195；`--allow-unsafe` 可强行启动，exit 3） |
| `readonly_token` | 空 | 用它认证的请求一律只读；v1.3.x 没有这个概念 |
| `token_ttl_days` | `0.0`（float，不过期） | 过期后需 `--renew-token`。**v1.7.26 把默认值从 `0`(int) 改成 `0.0`(float)**：GUI 存 `30.0` 时不再被 int 类型校验判脏回退 0（v1.7.25 的坑：Token 悄悄变永久有效）；实测 `30.0` 无告警、值保留 |
| `roots` | `[]` 时必须配置才能用 | 服务只允许 roots 内的路径 |
| `auto_backup` | `true` | 覆盖/移动/删除前备份；单次调用可传 `backup=false` 关闭，除非服务端开了 `backup_required` |
| `backup_required` | `false` | **v1.7.23 新增**：`true` 时 `backup=false` 一律拒（`backup_required`），`auto_backup=false` 冲突也拒写；默认 false 保持兼容。四个写工具都过 `_require_backup`（1642）。**v1.7.26 实测**：写 `"true"`（字符串）被严格解析成 `True` 并写启动横幅告警；`/api/patch backup=false` 被拒、默认备份仍生成（行为正确）。`使用说明.txt` 仍只在变更日志提到、无配置项说明（坑 34） |
| `max_read_bytes` | 4 MB | 读文件上限（也是 `win_read.max_bytes` 的硬顶、`hash=true` 的封顶） |
| `max_write_bytes` | 16 MB | 写/上传/JSON 请求体上限；`win_patch` 单文件上限 |
| `max_list_entries` | 3000 | 列表上限 |
| `grep_extensions` | 约 50 种文本扩展名 | 不在名单里的文件 grep 会跳过（除非显式关闭） |
| `backup_max_bytes` | 2 GB | 超了从最旧备份目录整目录删；v1.7.24 起**先 prune 孤儿再按 `physical`（只含被引用的 inode）判**，且至少保留 1 份（坑 27/37 已修）。`fixed`（不可回收部分）本身就 ≥ 上限时，体积维度自动停用。**v1.7.25 起脏值 `"2GB"` 回退为内置默认 2GB 并告警**，启动不再崩（坑 35 已修；实测） |
| `backup_retain_days` / `backup_retain_count` | 0 / 0（关） | v1.7.18 新增；README 建议配合 `backup_max_bytes` 用 30 天 |
| `backup_dedup_max_bytes` | 64 MB | v1.7.21 新增；同卷且 ≤ 此值的备份走硬链接去重 |
| `trash_retain_days` | 30 | 回收站保留天数 |
| `audit_max_bytes` / `audit_backup_count` / `audit_buffer_max` | 5 MB / 3 / 200 条 | v1.7.18 起审计写失败进缓冲、恢复补写 |
| `run_log_max_bytes` / `run_log_backup_count` | 2 MB / 3 | |
| `exec_enabled` | `false` | 打开后才有 `win_exec` 工具 |
| `exec_timeout_sec` / `exec_timeout_max_sec` | 60 s / 300 s | 超时 `taskkill /T /F`；v1.7.19 起允许客户端上调到 300 s |
| `exec_max_output_bytes` | 64 KB | 超输出落盘到 `logs/exec-*.txt`，响应只给 head/tail 预览 |
| `exec_strict_chain` | `false` | 打开后命令链每段首程序都要过白名单（默认只查整条命令首程序） |
| `patch_max_files` / `patch_max_total_bytes` | 20 / 8 MB | **v1.7.28 语义统一**：`patch_max_total_bytes` 与 `patch_max_rollback_bytes` 的 `0`/负数都**回退内置 8MB**，0 永不表示"不限"（实测：single 9MB 补丁 → 400「超过预算 8.0 MB」，与默认一致）；README/使用说明均已写明。**残留**：`probe.issues` 只为 rollback<=0 报警，total<=0 静默回退（坑 31/48） |
| `patch_max_rollback_bytes` | 8 MB | v1.7.22 新增：写模式 patch 为回滚保留的原文上限（单文件与单次总量共用）；0/负数 = 回退内置 8MB 并让 probe 报配置异常。**v1.7.25 起 `"8MB"` 这类脏值在加载期回退为默认 8MB**，`op_probe`/`op_patch` 不再抛 ValueError（坑 35 已修；实测四种写法均不崩） |
| `preflight_check` | `true` | 写前语法校验 `.py/.json/.jsonl/.xml/.toml` |
| `public_health` | `true` | `/health`（别名 `/api/health`）免鉴权，只返回服务名/版本；**v1.7.25 起还回 `config_warnings` 的数量与键名**（不回值）。注意它**不受 `ip_allowlist` 过滤**（配 `[""]` 时仍 200，坑 44） |
| `enable_mcp` | `true` | |
| **安全开关（v1.7.26 新分类）** | — | `SAFETY_BOOL_KEYS` = `read_only`/`allow_delete`/`allow_move`/`auto_backup`/`backup_required`/`exec_enabled`/`exec_strict_chain`/`enable_mcp`/`preflight_check`/`public_health`；`SAFETY_LIST_KEYS` = `ip_allowlist`/`tools_disable`。文件层：认 `true/false/1/0`（不分大小写、容忍空白）→ 规范化 + 告警；**认不出（`"yes"`/`null`/`2`/`1.0`/单字符串数组/`[1]`）→ 拒绝启动 exit 5**，端口不监听、不改写配置。env 层：`WKFILE_*` 脏值同样 fatal，但 `yes/on/no/off` 仍被接受（宽严不一致，坑 44）。**仅覆盖启动路径**；`exec_strict_chain` 还能被热加载绕过（坑 42） |

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
  backups\               覆盖前自动备份（v1.7.21 起另有 .objects\ 硬链接对象库）
  trash\                 回收站（<时间戳>-<6hex>\manifest.json + files\）
  tls\                   自签证书 / fullchain.pem
```

**配置里的 `base_dir` 是"配置文件所在目录"**，不一定是 `data_dir()`。`--config D:\share\cfg\wkfile.config.json` 时，日志/备份/回收站/TLS 都在 `D:\share\cfg` 下，而 `data_dir()` 仍是 exe 旁的 `wkfile\`。这个差异会让部分围栏失效（见 12 节）。

### 6.1 配置分层（v1.7.19 起）

`load_config`（1117）按「默认值 → 配置文件 → 环境变量 `WKFILE_*`」三层合并，`win_probe` 的 `config.layers` 只回报名（不含值）：

- 环境变量层可以覆盖 `WKFILE_ROOTS` / `WKFILE_TOKEN` / `WKFILE_READ_ONLY` / `WKFILE_EXEC_ENABLED` 等；**改的是运行时值，不写回文件**。
- 排查"配置明明改了却不生效"时，先看 `win_probe` 的 `config.layers.env`，再看进程环境。
- 配置键名是下划线风格，环境变量是大写（`backup_dedup_max_bytes` → `WKFILE_BACKUP_DEDUP_MAX_BYTES`）。

### 6.2 从 v1.3.x 升级到 v1.7.21 的不兼容点（有证据的）

> 证据：v1.3.1 包 `.build/pack/WinFile-Server-v1.3.1/`（README-快速上手.txt、`server/agent.config.example.json`）+ 当前源码。没证据的写"未验证"。

| 项 | v1.3.x 实况 | v1.7.21 实况 | 是否破坏升级 | 建议 |
|---|---|---|---|---|
| 可执行文件 | `WinFileServer.exe`，另有 `WinFileMcp.exe` | 单文件 `wkfile.exe`（无 Mcp.exe） | **是**：桌面快捷方式/计划任务/服务包装/客户端里写死 `WinFileMcp.exe` 的 stdio 配置全部失效 | 重建快捷方式；MCP 客户端改用 HTTP `/mcp` + Token |
| 配置文件名/位置 | 与 exe 同目录的 `agent.config.json` | `wkfile\wkfile.config.json`（`data_dir()`，`wkfile_agent.py:268`） | **是**：新版不会读旧文件，会当首次运行**自动生成新 Token**，所有客户端 401 | 手工把旧键拷进新文件，或 `--config` 指向旧文件（键名兼容） |
| 配置键 | 18 个键：host/port/token/roots/read_only/allow_delete/allow_move/auto_backup/ip_allowlist/max_read_bytes/max_write_bytes/max_list_entries/max_grep_file_bytes/audit_log/tls_cert/tls_key/auth_fail_limit/auth_fail_window_sec | `DEFAULT_CONFIG` 48 键 | **否**：旧 18 键全部保留，新增键有默认值（实测 `set(old)-set(new)` 为空） | 旧文件可直接当 `--config` 用，但建议跑一次 GUI 保存以补齐新键 |
| 数据目录结构 | 配置/日志/审计/备份/回收站都在 exe 目录（`logs\agent.out.log`、`logs\winfile-agent-audit.jsonl`、`.winfile-backups\`、`.winfile-trash\`） | 全在 `wkfile\` 下：`logs\wkfile.log`、`logs\audit.jsonl`、`backups\`（含 `.objects\`）、`trash\` | **是（数据可见性）**：旧日志/审计不再被 GUI 读；旧备份/回收站不会被列出 | 需要旧历史就先手工搬 `<exe>\logs\*` → `wkfile\logs\`；旧的 `.winfile-*` 只能人工翻文件 |
| 回收站格式 | 普通文件目录 `.winfile-trash\` | `<时间戳>-<6hex>\manifest.json` + `trash_id`（`op_trash_restore` 2726） | **是**：旧回收站无法用 `win_trash_restore` 恢复 | 升级前把要留的文件从旧回收站挪回业务目录 |
| 备份格式 | `.winfile-backups\` 平铺副本 | `backups\<stamp>-<ms>\<root key>\<相对路径>`（+ 硬链接对象库） | **是**：旧备份 `win_backup_list/restore` 看不到 | 同上，先手工恢复需要的版本 |
| Token 作用域 | 只有 1 个 token，无"只读 Token"概念 | 新增 `readonly_token`、`token_ttl_days`；主 Token 语义不变 | **否（增量）** | 需要只读客户端时再单独生成 |
| Token 强度闸 | 示例 Token 是占位长句，可启动 | 非 loopback + 空/占位/过短 Token **拒绝启动** | **是（若沿用旧占位串）**：服务起不来 | 用 `--gen-token`/GUI 重新生成 |
| `win_exec` | 不存在 | 默认 `exec_enabled=false`，`tools/list` 里没有 `win_exec` | **否** | 需要时再开，并开 `exec_strict_chain=true`（见坑 26） |
| TLS 证书 | `tls_cert`/`tls_key` 键已存在，路径可相对 | 仍支持这两个键，但相对路径按**进程工作目录**（GUI 启动时 = 数据目录 `wkfile\`）解析（`tls_enabled`/`validate_tls_pair`，配置路径与 `wkfile_gui.py:2563` 的 `cwd=DATA_DIR`） | **是（相对路径）**：旧 `server.crt`/`server.key` 找不到 → HTTPS 起不来 | 改成绝对路径，或把证书拷到 `wkfile\tls\` 并改配置；新格式多一个 `tls_chain`（可选） |
| 端口 | 8788 | 8788 | 否 | — |
| MCP 传输/鉴权 | `/mcp` + `Authorization: Bearer` | 同 | 否（协议版本可能被客户端提升） | 先用 `tools/mcp-call-test.py` 验一遍 |
| 审计字段 | `audit_log` 指向 `logs\winfile-agent-audit.jsonl` | `audit_log` 默认 `logs/audit.jsonl`，JSONL 字段有扩展 | 升级前旧审计不会合并 | 如需留档先备份 |
| 备份去重/硬链接 | 无 | v1.7.21 新格式（`.objects`） | 旧目录不会被识别，**未验证**旧备份目录手工拷进新结构后能否被 `win_backup_restore`（未做实验） | 不建议手工改结构 |

其余（数据库配置 `databases`、`grep_extensions` 等）v1.3.1 没有或未验证，按"未验证"处理。

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
11. **`op_*` 的返回值必须是可 JSON 序列化的纯数据**（str/int/list/dict/None）。不要塞 `bytes`、`Path`、异常对象——`call_tool` 的文本渲染不会报错，但 HTTP/JSON-RPC 最后一步 `json.dumps` 会炸，结果是"文件已经写了、客户端收到 500/断连"（v1.7.21 的 `win_patch` 就是这样，见坑 25）。改动返回值后**必须过一遍 `json.dumps(result)`**。
12. **分页/截断的终止条件只保留一种说法**：`win_read` 只看 `eof`；不要引入"返回 0 行即结束"之类的第二套语义（v1.7.13 的教训，见坑 5）。

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
- **exec**：默认关；白名单只查首程序；`exec_strict_chain=true` 恢复逐段检查；超时杀进程树；输出落盘。v1.7.23 起会穿透 `call`/`start`/`cmd`，**程序位置含 `%`/`!`/`^` 一律拒**，`set X=payload.bat\ncall %X%`、`for %i in (...) do call %i` 已堵；裸 `type/copy` 参数不再误伤。**但 `if exist x.bat x.bat`、`(x.bat)`、`>nul x.bat` 可绕过**（坑 38），带路径的 `type .\x.ps1` 仍误伤。不要把它当完整沙箱。
- **备份**：默认强制，单次可传 `backup=false` 跳过；v1.7.23 新增 `backup_required`（默认 false）可在服务端强制写前必备份（坑 28）。
- **审计**：所有 API/MCP 调用 JSONL，含结果/耗时/错误；轮转保留。

**注意：这是"API 层围栏"，不是操作系统沙箱。** AI 拿到的权限 = 服务账号权限；roots 只是限制 API 能碰的路径。README 明确写了这一点，别在文档里说成"沙箱"。

---

## 9. 已踩过的坑（真实记录，改相关代码前先看）

> 标注规则：**已修 / 部分修 / 未修** 以 **v1.7.16** 为准；条目里写 v1.7.11 的"实测"是当时的发布包结果，保留作回归对照。

1. **【v1.7.11 新坑，v1.7.12 已修】**`win_read` 的 `next_start_line` 在字节截断时会丢内容（v1.7.16 复核：`op_read` 1813-1949，`next_offset` 字节游标 + 残页不推行号基；`test/v1712/spotcheck.py` A/D/E 锁住）：`read_text_file` 按**字节**截断（`:616-623`，`fh.read(limit+1)` 后 `raw[:limit]`），`op_read` 再把截断后的文本 `splitlines()`，于是最后一行是**残行**却被算进 `returned_lines/total_lines`，`next_start_line = start-1+len(selected)+1`（`:1806-1818`）直接跳过这行剩下的几十万字符。
   - 实测（v1.7.11 exe，4,403,100 字节、4100 行的文件，默认 4MB）：返回 `returned_lines=4001, total_lines=4001, truncated=true, next_start_line=4002`；按 4002 续读得到 0 行，文件尾部标记再也没有出现。
   - 正确做法：游标应按**字节偏移**给（`next_offset`），或在截断时把最后一行**排除**在 `selected` 之外并让游标回到该行；`total_lines` 也不能用截断前缀的行数冒充全文行数。
2. **【已修】`win_replace` 大文件截断**：`op_replace` 现在接住 `truncated` 并直接报 `too_large`（`:2000-2008`），实测 >4MB 文件返回 `too_large`、文件未被裁掉。**但落盘仍是非原子**（`:2078-2079`）。
3. **【已修】`_read_raw_body` 重复定义**：现在只有一个定义（`:3464-3483`），上限统一为 `max_write_bytes`（默认 16MB），超限走 413 语义。
4. **【v1.7.12 已修】配置里带 `//` 注释 → exec 热加载永远不生效**：`_hotload_exec_cfg` 现在先 `_strip_json_comments` 再解析（`wkfile_agent.py:2994`）
5. **【未修】`norm()` 不是 realpath**（`:579` 附近，docstring 却写 realpath）：root 是 junction / 符号链接 / 8.3 短名时，`resolve` 用 realpath 得到的目标与 root 的 abspath 前缀对不上 → 误报 `out_of_scope`；`_deny_data_dir` 的保护路径比较也会错位。
6. **【v1.7.12 已修】`search/grep/tree` 不走逐条 `guard.resolve`**：v1.7.16 复核 `op_tree` 2561 / `op_search` 2682 / `op_grep` 2742 均逐条解析、junction 不出 roots，`check.py` T8a/T8b 锁住
7. **【v1.7.11 新坑，v1.7.12 已修】exec 第七重围栏只挡"首程序"，链式命令可绕过**：`_exec_guard_hijack`（`wkfile_agent.py:2935`）现在对命令链每一段的首程序都做投放检测，`dir && payload.bat` 被拒
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
18. **【v1.7.11 新坑，v1.7.12 已修】示例配置里的占位 Token 曾被判为强口令**：v1.7.16 复核 `token_strength("在这里填一个长随机串；用图形界面启动时点「生成」按钮即可")` 返回 `bad`，`--run` 拒绝启动。历史记录：`WEAK_TOKENS`（`wkfile_agent.py:966` 附近）曾只收录旧文案
   - 好在这串含中文，HTTP header 传不进去（`Invoke-WebRequest` 直接报"无效的控制字符"），所以更像"用户永远连不上"的坑，而不是直接鉴权绕过。
   - 当时 README 第 7 节写的"Token 是示例占位值会被直接拒绝启动"对 v1.7.11 的随包示例不成立；v1.7.12 起示例 Token 已进黑名单，表述与实际一致。
19. **【v1.7.11 新坑，v1.7.13 已修】MCP `structuredContent` 让 `win_read` 响应翻倍**：正文 > 4096 字节时省略重复 `content` 并置 `content_omitted=true`（v1.7.21 行号：`wkfile_mcp_endpoint.py:33-35`、`1329-1344`）；≤ 4KB 仍重复一份（旧客户端兼容）。v1.7.21 实测 9KB 正文 `content_omitted=true`、JSON 总量 ≈1 倍。
20. **【v1.7.11 文档坑，v1.7.16 大部分已修】发布包里的文档与产品不一致**：v1.7.11 曾写"刻意没有提供任何执行工具"、"请求体 32 MB"、"使用说明 187 行 16MB vs JSON API 64MB"；v1.7.16 复核：执行工具默认关闭的表述与 4MB/16MB 上限已对齐，**只剩用例数自相矛盾**（见坑 24）。
21. **【v1.7.13 教训】断言全绿 ≠ 契约正确**：`win_read` 的 A/D 段断言（`sum(consumed)==size`、`sum(returned_lines)==行数`、`L3/L4=3/4`）在"换行符恰好压在读取窗口最后一个字节"时**全部通过**，但客户端看到多一个空行、行号整体错位（第 3 页 content 以 `\n` 开头）。
   - 复现条件：文件 `b"L1\n" + b"A"*N + b"\n" + b"L3\nL4\n"`，且 `max_bytes = max_read_bytes = N`，按 `next_offset` 逐页续读 → 边界页被误判成 `line_fragment=true`，换行符留到下一页，`splitlines()` 凭空产生空串。
   - 教训：分页断言必须再加两条——「逐页 content == 该页字节区间去掉行尾换行」「任何一页 content 都不得以 `\n` 开头」；只比对总量会放过错位。见 `test/v1712/spotcheck.py` E 段。
22. **【v1.7.16 存在，v1.7.21 已修】`win_read` 的「最后一行无换行且跨页」不再丢行**：`op_read`（v1.7.23 为 2354-2508）改成 `consumed_bytes` 只算完整行、`returned_lines` 按完整行计，EOF 时不扣减；`test/v1721/read_audit.py` 用「末尾有换行 / 末尾无换行 / 单行 200 字节无换行」三组跑通四条不变式（`sum(consumed)==size`、`sum(returned_lines)==逻辑行数`、逐字节还原、全局行号连续）。
   - 历史表现（v1.7.16）：尾页 `starts_mid_line=true` 且不以换行结束时 `complete` 被减 1（旧 `1907-1913`），该逻辑行永远进不了 `returned_lines`。
   - 现在的契约：**只看 `eof`**；`content_ends_with_newline=true` 时客户端补一个 `\n` 即可逐字节还原，`false` 表示文件末尾本来没有换行（空文件也是 `false`）。
23. **【v1.7.16 新增暴露面，v1.7.22 未回退】`initialize` 把服务端安全配置回给客户端**：`runtime_contract(info)` 追加在 instructions 末尾（`wkfile_mcp_endpoint.py:1169-1216`，`initialize` 组装在 `:1227`，`_mcp_info` 在 `wkfile_agent.py:4336`），内容包括单次读写上限、`exec_enabled`、`exec_allowed_commands` 的前几个程序名、Token 作用域。只读 Token 也能看到；属低敏感信息，但白名单枚举等于告诉对手哪些程序可用，新增配置项时别再往里加敏感值。v1.7.21/22 契约 1517 B（默认配置）。
24. **【v1.7.22 部分修，v1.7.25 改为"浮动值"，v1.7.27 仍未锁真值】README 用例数**：包内 README 写"195 或 211 用例（随本机有无 openssl 浮动）"。**v1.7.27 实测（本机有 openssl）是 `通过 195 / 195，跳过 2`，两条跳过都是真实 DB 链接（MySQL/Redis），与 openssl 无关**——"195=无 openssl"的归因是错的。
   - **仍属"断言绿但契约错"**：`v1723_test.py::test_docs` 的正则只捕获 **211**（"195 或 211 用例"里 195 后面不是"用例"），`nums={211}` 通过；`v1725_test.py` 第 7 组只查"同时出现 195/211"，**两套测试都没锁真实数字**（坑 41/47）。
25. **【v1.7.21 新坑，v1.7.22 已修】`win_patch` 成功后响应用了 `bytes`，HTTP/MCP 序列化直接炸**：v1.7.22 的 `op_patch`（1953）不再返回 `original` 字节，改为 `original_sha256` / `original_bytes`；原文放在内部 `_original` 键里，返回前剥掉；并新增回滚预算 `patch_max_rollback_bytes`（`_patch_rollback_budget` 1921，默认 8MB，0=不限）。
   - 历史实测（v1.7.21，临时实例 18931）：`POST /api/patch` → **500**、`/mcp tools/call` → **断连**，但文件已改。
   - v1.7.24 exe 原地实测（`test/v1724/exe_inplace.py`，端口 18961，包内原地）：`/api/patch` 200、无 `original`、`/mcp win_patch` 200 且 `isError=false`、文件正确改写；包目录已还原、端口已释放。
26. **【v1.7.22 部分修，v1.7.23 基本修好，v1.7.24 明确"已知限制"，v1.7.25/26 文档仍未限定】"全链投放检测"曾只查每段首程序**：v1.7.23 在 `_exec_is_dynamic`（v1.7.26: 3688）里把程序位置含 `%`/`!`/`^` 的一律判 `exec_dynamic_program`，`set X=...\ncall %X%`、`for %i in (...) do call %i`、`call !X!`、`cmd /c %X%` 全部堵死。
   - **误伤收窄**：段内兜底（v1.7.26: 3825-3832）只查「程序位置」和「带 `\`/`/` 的脚本引用」，`type deploy.ps1`、`type payload.bat`、`copy payload.bat x` 已放行。
   - **残余误伤（P1，v1.7.27 实测仍在）**：`type .\deploy.ps1`、`copy .\payload.bat x` **仍被拒**（`test/v1727/leftover_probe.py` B 段，code=`exec_hijack_suspected`）；无路径变体 `type deploy.ps1`、`copy payload.bat payload2.bat` 已放行。README 与 `使用说明.txt` 的"不再误拦 type/copy"仍属**未限定纯文件名**的过度承诺。
   - **v1.7.24 的处理**：不再加围栏，`_exec_guard_hijack`（v1.7.26: 3771）docstring 明确写"不是安全边界/不再加固"，README「已知限制」、`win_exec` schema、运行时契约同步。
   - **v1.7.25 文档修正**：`使用说明.txt:224/238` 已改成正确指向"清单在随包 README.md 里，不在本说明里"，**自指错误已消除**；但使用说明自身仍没有逐条清单（可接受）。
   - 前提仍是 `exec_enabled=true`；`exec_strict_chain=true` 可拦住 `call`/`start` 本身。
27. **【v1.7.21 新坑，v1.7.23 已修】备份去重后体积统计不认硬链接**：v1.7.23 把统计外提成 `backup_inode_sizes`（`wkfile_core.py:445-476`），按 `(st_dev, st_ino)` 去重、`reclaim` 归最后引用者；`_backup_entries`（1738）现在回 `size`（删后可释放）/`logical_size`/`physical`，`cleanup_backups`（1791）用 `physical` 判 `backup_max_bytes`。两个版本共享 2MB 实体时 `physical=2MB`、`logical=4MB`（`tests/v1723_test.py` 第 4 组）。`op_backup_restore` 仍走 `copy2`，不会把硬链接带回业务目录。
   - **代价**：`_dir_size`（323）仍保留给 `logical_size` 展示。
   - **v1.7.24 修正**：`.objects` 孤儿**不再计入 `physical`**（`_backup_entries` 1749 只统计被版本引用的 inode），并单独回 `orphan_objects`/`orphan_bytes`/`unreclaimable_bytes`，坑 37 由此修掉。
28. **【v1.7.19 起的契约变化，v1.7.23 已给约束】备份不再是"绝对强制"（默认仍可拆）**：v1.7.23 新增 `backup_required`（默认 `false`），开 `true` 后 `backup=false` 直接拒（`backup_required`），`auto_backup=false` 冲突也拒写，四个写工具都过 `_require_backup`（1642）。**但默认值不设防**：升级后不显式打开，AI 仍可自己传 `backup=false` 拆保险丝；`使用说明.txt` 完全没提这个开关（只在 README 变更说明里），运维很难发现。
   - **v1.7.26 复测**：`使用说明.txt` 里 `backup_required` 只出现在变更日志段落；**v1.7.27 已在配置项说明处补上"强制先备份再写"**（本轮已实测命中），文档缺口关闭；字符串 `"true"` 自 v1.7.26 起严格解析。
29. **【v1.7.21 测试坑，v1.7.22 已修】空断言/只断言存在**：v1.7.22 把 `tests/v1721_test.py:190`、`tests/optimistic_lock_test.py:83`、`tests/features_e2e.py:612` 的 `check(..., True)` 换成了真实条件，并在 `v1722_test.py` 里对所有工具结果做 `json.dumps` + `/api/patch`、`/mcp win_patch` 双通道断言。
   - 仍偏弱：`v1722_test.py` 的"所有工具响应可序列化"是拿空参数打一圈（大量工具直接 `isError`），成功路径并未逐个覆盖；`win_patch` schema 用例只做字符串包含（`重试`/`expected_sha256`）。
30. **【长期存在，v1.7.23 已修，v1.7.24~28 复测仍 0】发布包文本文件的换行是 `\r\r\n`**：`build-release.py` 的 `write_text` 已改为先 `\r\n → \n` 再按 `newline="\r\n"` 落地。v1.7.23 包内实测 CRCRLF 全为 0；**v1.7.24/25/26/27/28 复测均为 0**（`test/v1728/pkg_verify.py`，遍扫 14 个文件），保持 CRLF。
31. **【v1.7.19 起的坑，v1.7.28 行为已统一，残留 P2】`patch_max_total_bytes=0` 与回滚预算**：v1.7.28 起两者**都是 0/负数 → 回退内置 8MB**，`patch_plan` 的"<=0 不限制"前提已由调用方 `_patch_max_total_bytes`（2152）夹掉（`wkfile_core.py:671` 的注释仍是旧的，看代码会被误导）。**实测**（`test/v1728/patch_zero_probe.py`，端口 19103）：两项都写 0 时，小补丁 200；单批 **9MB → 400「超过预算 8.0 MB」**，与默认一致；`probe.issues` 只有 rollback 一条，total=0 的回退**没有告警**。
   - **建议**：给 total<=0 也补一条 issue（或统一"0=默认，不告警"两处都别报），并删掉 core 里过时注释。
32. **【v1.7.22 新坑，v1.7.23 已修】审计缓冲的 `dropped` 只可见不告警**：`Audit.health()` 现在 `dropped>0` 时回 `alerts` + `integrity="lossy"`，`op_probe.issues` 带"审计日志丢弃"文案，`ok` 变 false；`state` 只表示当前是否写盘成功（`buffered`/`ok`），不再被误读成历史完整性（`tests/v1723_test.py` 第 5 组）。
33. **【v1.7.22 未改，v1.7.23 部分修，v1.7.24 定性为"不是边界"，P2】根目录绝对路径对只读 Token**：`op_probe` 只读时把 roots 打码成 `<root#N>`、`backup.path="<redacted>"`、清空 `audit.last_error`；**但 `op_roots` 故意不脱敏**（AI 构造路径必需），读 Token 调一次 `win_roots` 就拿到全量绝对路径。**v1.7.24 已在 README 明确写"probe 脱敏不是权限边界"**，源码层面 `op_roots` 维持不脱敏 —— 结论：**这是有意设计，不再当作漏洞**；`test_probe_redaction` 只断言 probe 不含绝对路径，不能当权限断言用。
34. **【v1.7.22 环境限制，v1.7.23 已绕开，v1.7.24~28 沿用】本机应用控制策略只拦"复制出去的未签名 exe"**：把 `wkfile.exe` 复制到 `%TEMP%`/`.build` 再启动仍报 `WinError 4551`；但**在包目录内原地执行（`cwd=包目录`，exe 路径不复制）放行**。**v1.7.28 用 `test/v1728/exe_inplace.py`（端口 19001→本轮 19101）再次拿到真实 wire**：`/health`=1.7.28、`tools/list` 36、`/api/patch` 200 无 `original`、`/mcp win_patch` 200 `isError=false`；另用 `exe_hotload_check.py`（19115）确认 exe 二进制带 1.7.28 热加载规范化；跑完 `alive=False`、清包内新建文件、`pkg_restored=True`、无 LISTEN。
   - **但包内 README 仍保留 "WinError 4551" 整段与 "v1.7.18 ~ v1.7.25 均未改动 GUI 代码"**（后者本身不算错，但整段的语境是"本轮源码级回归已覆盖"却停在 v1.7.24 清单）—— P2，v1.7.28 仍未删（坑 47）。
35. **【v1.7.23 新坑，v1.7.25 已真修，P2】脏数值配置会让 `win_probe` / `op_patch` / 启动清理抛 ValueError**：v1.7.25 新增 `validate_config_types`（`wkfile_core.py:385`，`_config_type_ok` 362）在 `Config`（`load_config`）加载期做类型校验，**类型不符的项回退为内置默认值**并记 `config_warnings`；`op_probe` 把它落进 `issues`，`/api/health` 报数量+键名。
   - **实测四种写法**（`test/v1725/dirty_config_probe.py`，源码路线）：`backup_max_bytes="2GB"`、`patch_max_rollback_bytes="8MB"`、`backup_retain_days="30d"`、三项同时脏 —— **均正常启动、无 500**；`/api/health` 回 1 条警告（键名正确）、`/api/probe` 200 且 `issues` 指名该键、`op_patch` 200、端口正常。
   - **新问题转移到"回退方向"**：布尔/数组类型错时回退到默认值可能 fail-open（见坑 39）。
36. **【v1.7.23 新坑，v1.7.24 已修，P2→已修】`win_backup_list` 的 `would_remove` 与真实清理口径不一致**：v1.7.24 的 `_backup_policy`（1911）已改为 `_backup_cleanup_plan(..., total_bytes=physical)`，与 `cleanup_backups`（1883）同口径，并回 `unreclaimable_bytes`/`orphan_objects`/`orphan_bytes`/`dedup`；`tests/v1724_test.py` 有 policy/cleanup 一致性断言。
37. **【v1.7.23 新坑，v1.7.24 已真修，P1】`.objects` 孤儿 + 启动清理会超删全部备份**：v1.7.24 的 `cleanup_backups`（1883）**先调 `_prune_backup_objects()`（1728）再统计**，`_backup_entries`（1819）的 `physical` 只含被版本引用的 inode，`_backup_cleanup_plan`（1781）用 `fixed = total - reclaimable` 且循环里保底 **至少留 1 份**。
   - **复测证据**（`test/v1723/cleanup_orphan_probe.py`，v1.7.24 源码）：2MB 孤儿 + 两个有效版本 + `backup_max_bytes=1.5MB` → **两个版本都保留、孤儿被 prune、`would_remove=0`**。
   - 触发面仍在（手动删备份目录、上次清理中途崩溃、`os.link` 失败留下的 obj），但现在会自愈。
38. **【v1.7.23 新坑，v1.7.24 改为"已知限制"，P1→降级为已知风险】兜底收窄后，`if`/`(...)`/重定向前缀可重新绕过投放检测**：`dir && if exist payload.bat payload.bat`、`dir && if exist payload.bat call payload.bat`、`dir && (payload.bat)`、`dir && >nul payload.bat` 四种写法在 v1.7.24/25/26 **仍全部放行**（源码未改 `_exec_seg_programs`（v1.7.26: 3701）/ 兜底 3825-3832）。
   - **v1.7.24 的处理是"承认而非修复"**：`_exec_guard_hijack`（v1.7.26: 3771）docstring 写明"尽力而为、不是安全边界、不再加固"，README「已知限制」、`win_exec` schema、运行时契约同步；v1.7.25 已把 `使用说明.txt` 的自指改为正确指向 README。
   - 前提：`exec_enabled=true` + 链首程序在白名单（`dir`/`echo` 很常见）+ cwd 可写。**生产建议：除非确认不跑 untrusted 内容，否则保持 `exec_enabled=false`；若必须开，`exec_strict_chain=true` 且把可写目录从 cwd 挪走。**
39. **【v1.7.25 引入，v1.7.26 已真修（启动路径），P1→已修】配置类型回退对安全开关曾 fail-open**：v1.7.26 新增 `ConfigSafetyError`（`wkfile_core.py:437`）、`SAFETY_BOOL_KEYS`（453）、`SAFETY_LIST_KEYS`（458）、`coerce_safety_bool`（467）、`validate_safety_config`（488）、`safety_env_errors`（545）、`format_config_warnings`（572）；`Config.__init__`（`wkfile_agent.py:984-1030`）先校验安全开关，认不出就 exit 5，**绝不回退默认值**。
   - **v1.7.26 实测（`test/v1726/safety_switch_probe.py`，逐例起服务）**：`read_only:"true"` → `/api/roots` `read_only=true`、横幅 `[readonly ON]` + 告警行；`allow_delete:"false"` → `allow_delete=false`、`[delete OFF]`；`backup_required:"true"` → probe `backup_required=true`；`exec_enabled:"false"` → `exec_enabled=false`；`read_only:" TRUE "`/`1` → 规范化 + 告警；**混合 `read_only:"true"+allow_delete:"false"` → 两者都正确生效**。
   - **认不出的写法一律拒绝启动（exit 5、端口不监听、不改配置）**：`read_only:"yes"`、`read_only:null`、`allow_delete:2`、`allow_delete:1.0`、`ip_allowlist:"10.0.0.0/8"`、`ip_allowlist:""`、`ip_allowlist:[1]`、`tools_disable:"win_exec"`、env `WKFILE_READ_ONLY=maybe`、env `WKFILE_IP_ALLOWLIST=10.0.0.0/8`。
   - **`port:"10087"` 在 v1.7.26 回退 8788（port 不是安全键）；v1.7.27 已改为拒绝启动**（见上一条）。
   - **行为层也验了**（`test/v1726/safety_effect_check.py`）：read_only 下写/删均被拒；`allow_delete=false` 下 trash 被拒且文件仍在；`backup_required=true` 下 `backup=false` 被拒、默认写仍生成备份；`exec_enabled=false` 下 `tools/list` 无 `win_exec`、`/api/exec` 被拒。
   - **v1.7.27 补门禁**：`port` 脏值/`null`/字符串已改为**拒绝启动**（与安全开关同级）；`host:null` 回落 `0.0.0.0` 但会写 `config_warnings`（横幅/health/probe 都可见，实测）。**v1.7.28 复测无变化**：`port:"19110"` 仍 exit 5；**`tls_cert:null` 仍静默关 TLS、无任何告警**（坑 43 剩余部分）。
   - **仍不覆盖**：env 层 `yes/on` 与 `ip_allowlist:[""]`（坑 44）。
40. **【v1.7.24 新坑延续，v1.7.28 部分修，P2】备份枚举 IO 放大**：v1.7.27 把 `.objects` 的枚举并进 `_backup_entries` 同一次 `os.walk`，`cleanup_backups` 改为内联删孤儿路径 → 从 3 次全量降到 1 次全量。**但每个版本目录仍被 `os.walk`（1851）和 `_dir_size`（1882）各走一次（2 次/版本目录）**——`groups[st]` 里已经有文件大小，`_dir_size` 完全可以省。`win_backup_list` 每次列举仍重扫。
   - **建议**：`logical_size` 直接用 walk 时累加的大小，删掉 `_dir_size` 调用。
41. **【v1.7.25 引入，v1.7.26~28 未改，P2】`test_docs` 仍是"形状断言"**：`tests/v1723_test.py` 的 `test_docs` 用 `features_e2e\.py[^\n]*?(\d{3})\s*用例` 抓数字，**只捕获 211**（"195 或 211 用例"里 195 后面不是"用例"），于是 `nums={211}`、`len(nums)==1` 通过。**v1.7.28 实测**：README 同时含 `195/195` 与 `211/211`，脚本解析结果仍是 `{211}`（`python -c` 复算），检查照样绿。
   - **前提纠正（v1.7.27 结论有误）**：v1.7.27 报告写"本机有 openssl，所以 README 归因错误"——**实际本机 `openssl` 不在 PATH**（`Get-Command openssl` 为空）。因此 `features_e2e` 的 **195/195 + 跳过 2**（R3 MySQL 真实链路、R3n Redis 真实链路）与 README"无 openssl 时 195"**一致**；211 是"有 openssl"分支，本机无法验证。别再拿"有 openssl"当结论。
   - **建议**：测试直接跑 `features_e2e`（或解析汇总行）再比对 README；至少把"实测 pass/total/skip"写死为环境事实。
42. **【v1.7.26 引入，v1.7.27 部分修，v1.7.28 已修】安全开关热加载旁路**：v1.7.28 起 `_validate_hot_key` 对 `exec_strict_chain` **先 `coerce_safety_bool` 规范化再 `setattr`**（`0`/`"0"`/`"false"`/`False` → `False`；`1`/`"true"`/`True` → `True`），认不出的（`null`/`"garbage"`/`2`）丢弃保留旧值；`exec_allowed_commands` 非数组仍丢弃。
   - **v1.7.28 实测**（`test/v1728/hotload_matrix.py`，端口 19102，控制组 `["echo","dir"]` + `echo hi && whoami`）：
     - `0` → **200**（规范化 False）+ run log 一条 + `health.hotload_warnings`（count=1, keys）+ `probe.issues`/`config.hotload_warnings` —— 不再"静默关"。
     - `"0"`/`"false"`/`false` → 200（False）+ 告警（`false` 是原生布尔，**不告警**，符合预期）。
     - `1`/`"true"`/`true` → 400（True）+ 告警（`true` 原值不告警）。
     - `null`/`"garbage"`/`2` → **写入值被丢弃、保留旧值 + 三渠道告警**；本组前置生效值是 `false`，所以 `/api/exec` 返回 **200**（若旧值是 True 则为 400）。判据是"值被拒 + 保留旧值 + 告警"，不是固定的 400。
     - `exec_allowed_commands: 123` → `/api/exec` **200**（保留旧白名单），不再 500。
   - 结论：**"静默关/静默无效"已消除**，告警不再只进 run log；**残余**是告警数组无上限（坑 50）。
43. **【v1.7.26 残留，v1.7.27 部分修，v1.7.28 未改，P2】普通键 `null` 与 `--init` 自愈**：
   - `"port": null` / `"port": "10087"` → 拒绝启动（exit 5），v1.7.28 复测一致。
   - `"host": null` → 回落 **0.0.0.0**，但会写 `config_warnings`（横幅 `[!] 配置告警` + health 数量/键名 + probe issues）。
   - **`"tls_cert": null` 静默关 TLS**：v1.7.28 实测 `/api/probe` 的 `security.tls=false`，`issues` 为空、`config_warnings` 为空（`test/v1728/leftover_probe.py` B 段）→ P2 剩余。**v1.7.29 已修**：`tls_cert` / `tls_key` / `tls_chain` 显式写成 `null` / 非字符串，或 `cert`/`key` 只配一个，均写 `config_warnings`（横幅 + health 数量/键名 + probe issues 都可见），不再静默 fail-open；想关 TLS 请显式写空串 `""`（`tests/v1729_test.py` 组 A 全绿）。
   - 现有配置含脏安全开关时，`--init` / `--renew-token` 仍会在 `load_config` 阶段 exit 5，**没法用 CLI 自愈**（v1.7.28 D 段实测 `init_exit=5`）。
   - 生成配置里 `port` 的注释仍写"写成字符串会回退 8788"，与行为相反（坑 51）。
44. **【v1.7.26 残留，v1.7.27/28 未改，P2】env 层宽严不一致 + `ip_allowlist:[""]` 全拒**：
   - `WKFILE_READ_ONLY=yes` 仍被 `coerce_env_value` 当成 True，**v1.7.28 实测 `/api/probe` 的 `issues` 与 `env_warnings` 均为空**（`leftover_probe.py` E 段）→ 与文件层 `"read_only":"yes"` 拒启不一致。
   - `"ip_allowlist": [""]` 仍能启动 → `/api/*` **全 403（含本机）**、`/health` 仍 200、**无任何告警**（v1.7.28 C 段复测一致）。
45. **【v1.7.27 新坑，v1.7.28 已修】热加载"接受"≠"规范化"**：v1.7.27 只做"能不能解析"，通过后 `setattr` 原值（字符串 `"false"` 落库成真值、整数 `0` 静默关严格链）。**v1.7.28 已改为规范化后写**，`tests/v1727_test.py` 从 73 → **83** 项，覆盖了规范化与 health/probe 暴露。
46. **【v1.7.27 新坑，v1.7.28 已修】`tests/v1724_test.py` 43/47**：v1.7.28 删除了 `_prune_backup_objects` 死代码，并把 4 条用例改成**行为断言**（构造真实孤儿/真实删不掉的对象，而不是 monkeypatch 内部函数）；复跑 **51/51**。`tests/v1724_test.py` 里已无 `monkeypatch`/`_prune_backup_objects` 引用。
47. **【v1.7.27 仍未改，v1.7.28 部分修，P2】文档口径**：
   - README 仍留"exe 被 WinError 4551 拦截"整段与 "v1.7.18 ~ v1.7.25 均未改动 GUI 代码"（v1.7.26/27/28 都改过 GUI）——v1.7.28 未删。
   - `使用说明.txt` **已有 `1.7.28` 条目**（标题"v1.7.27 / v1.7.28 的关键变更"，含热加载规范化与 patch 0 语义）——v1.7.27 报告自相矛盾的问题到此确认：**有**。
   - README 的"当前状态（v1.7.26 源码级回归）… v1724 47 项 … v1726"段**未更新**，仍是旧计数、且没有 v1727/v1728。
   - README `:415` 说 `type .\deploy.ps1` "把 type 加进白名单就能过"——**实测加进白名单仍 400**（坑 49）。
48. **【v1.7.28 新坑，P2】total=0 静默回退**：`patch_max_total_bytes<=0` 现在会回退 8MB（行为已统一），但 `op_probe.issues` 只对 `patch_max_rollback_bytes<=0` 报警。**实测**：两项都写 0 时 issues 只有 rollback 一条，total 静默。运维会以为 total 的 0 也被看见。
   - **建议**：补同款 issue，或两处都不报、只在文档写"0=默认"。
49. **【v1.7.28 复核，P2，"断言绿但契约错"】`type .\deploy.ps1` / `copy .\payload.bat x` 仍 400**：`test/v1728/leftover_probe.py` A 段实测——
   - `type .\deploy.ps1` → **400**（`deploy.ps1` 在 cwd 存在 → 被"带路径脚本引用"规则拦），即使 `type` 已在白名单；
   - `copy .\payload.bat x` → **400**；而 `type payload.bat` / `copy payload.bat x`（无 `.\`）→ **200**。
   - `tests/v1724_test.py` 的"不误伤"用例用的是**无路径形式**，所以 51/51 全绿——但用户实际写在文档里的 `.\` 形式仍被拒。**测试绿 ≠ 契约对**的典型。
   - **建议**：要么把只读程序（type/more/findstr 等）的 `.\xxx` 参数排除出 cwd 劫持检查，要么在 README `:415` 明确"带 `.\` 会被拦，请写成裸文件名"。
50. **【v1.7.28 新坑，P2】`_hotload_warnings` 无上限增长**：`_record_hotload_warning`（3939）只 `append`，不裁剪、不去重。反复改配置（每次 1 条）会让 `/api/probe` 的 `config.hotload_warnings` 与 `issues` 线性变大，进程内存也一直涨；`/api/health` 只回 count 所以影响小。
   - **建议**：`deque(maxlen=20)` 或"同 key+action 只保留最后一条"。
51. **【v1.7.28 复核，P2】发布流程与注释口径**：
   - `build-release.py` **没有任何测试门禁**（不跑 `tests/*`、不校验 `tests` 全绿），所以 v1.7.27 能带 43/47 的套装发版，v1.7.28 也没加门禁——**同样的事还会再发生**。
   - 生成配置里 `port` 的注释（1199-1200）仍写"写成字符串会回退 8788"，而 v1.7.27 起是拒启；`start_banner` 的告警只覆盖 `config_warnings`，不含热加载（热加载发生在启动之后，这点合理）。
   - README 回归状态段仍是 "v1.7.26 / v1724 47 项"。
   - **建议**：`build-release.py` 起手跑 `tests/v1724_test.py` + `tests/v1727_test.py`，任一非零就拒绝打包；顺手删两处过时注释/段落。

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

1. `server/wkfile_agent.py:111` `VERSION = "1.7.28"`（发布目录/zip 名、界面标题、`/health` 都读它）——**唯一真实来源**
2. `build-exe.ps1` 的 `$AppVer` —— 仍是死变量，没有任何地方使用（v1.7.17 起 build 脚本里已把版本写成 1.7.17 附近的值，改版本时要一起搜）
3. `build-exe.ps1` 生成 `.build/version_server.txt` 作为 `--version-file`；v1.7.28 实测包内 exe 的 FileVersion/ProductVersion 都是 **1.7.28.0**（历史坑：内联 `filevers=(1,6,0,0)` 是死代码）

**v1.7.28 发布包实测**（详见第 16 节）：

- `build-release.py` 打 zip 时带顶层目录，v1.7.28 的 14 个条目全部以 `wkfile-v1.7.28/` 开头。
- `LAYOUT` 的 11 个条目全部在包内；实际 14 个文件 = 11 + `以管理员身份启动.bat` + `文件夹说明.txt` + `wkfile\数据目录说明.txt`（后三个由脚本生成）。LICENSE 1072 字节；**14 个文件逐个与 zip 内 SHA256 相同**。
- `build-release.py` 只检查 `wkfile.config.json` / `agent.config.json` / `mcp.json` 三种文件名，**不校验 exe 新旧、不扫绝对路径/内网信息/凭据、不跑任何测试**（坑 51）。
- `README.md` 是整份从开发仓库拷过去的，包含 `server\`、`dist\`、`tests\`、`.trae\mcp.json`（"含真实 Token，勿提交 git"）等**用户侧不该看到的开发结构**。
- 文本文件换行正常（坑 30）：v1.7.28 包内所有文本 `\r\r\n` 计数为 0。

发布前检查清单：

- [ ] `VERSION` 与 `build-exe.ps1` 的可执行路径对齐（`build-exe.ps1` 里 `$AppVer`/`filevers`/`FileVersion` 三处仍是手写同步）
- [ ] exe 是**本次源码重新打的**（`build-release.py` 不校验 exe 版本/时间戳，旧 exe 会被装进新版本目录；v1.7.28 已实测包内 exe 属性=1.7.28.0，且在包目录内原地跑通 `/health` + `/mcp` + `/api/patch` + 热加载写 0）
- [ ] `dist\wkfile.exe` 的文件属性版本 = `VERSION`
- [ ] 发布目录不含 `wkfile.config.json`、Token、证书私钥、`.build/`
- [ ] 包内文档与产品一致：工具数、默认值、请求体上限、是否暴露 `win_exec`、用例数（见第 16 节清单）
- [x] `wkfile.config.example.json` 与 `DEFAULT_CONFIG` 键集对齐（v1.7.28 实测 **50/50**，键与类型全一致）
- [x] zip 里带顶层目录（v1.7.28 实测 14 个条目全部以 `wkfile-v1.7.28/` 开头）
- [x] LICENSE 随包（v1.7.28 包内 1072 字节）
- [x] `使用说明.txt` / `连接自检.bat` 的换行别再产出 `\r\r\n`（v1.7.28 实测 CRCRLF=0，见坑 30）
- [ ] **安全开关实测**：`read_only` / `allow_delete` / `backup_required` / `exec_enabled` / `ip_allowlist` / `tools_disable` 类型错必须"拒绝启动或严格解析 + 启动横幅告警"，不能静默回退（见坑 39）
- [ ] **热加载安全键实测**：运行期改 `exec_strict_chain` / `exec_allowed_commands` 不得静默放宽；规范化与拒绝都必须出现在 run log + `/api/health.hotload_warnings` + `probe.issues`（v1.7.28 已满足，见坑 42）
- [ ] **回归全绿**（v1.7.28 起）：`tests/v1724_test.py` **51/51**、`tests/v1727_test.py` **83/83**、`v1721~v1726` 全绿、`features_e2e` 195/195（+2 DB 跳过）
- [ ] **给 `build-release.py` 加测试门禁**（当前完全没有；v1.7.27 带 43/47 发版就是缺这个闸，坑 51）
- [ ] `git tag v1.x.y` 并推送（仓库仍没有 tag）

---

## 11. 测试地图

| 脚本 | 覆盖 | 运行方式 / 注意 |
|---|---|---|
| `selftest.py` | 老版 HTTP API（约 53 项）+ 已废弃 stdio 桥接的 MCP 冒烟 | `python selftest.py`；固定端口 **8899**；用 `_selftest` 临时目录；`--keep` 保留 |
| `tests/features_e2e.py` | 主回归：围栏、备份、回收站、编码、TLS 校验、只读、exec、数据库错误路径等。**v1.7.23~28 实测均为 `通过 195 / 195，跳过 2`**；两条跳过是 **R3 MySQL 真实链路 / R3n Redis 真实链路**（不是 openssl） | `python tests\features_e2e.py`；大量 `mkdtemp`；固定端口在文件顶部。**本机 `openssl` 不在 PATH**，所以 195 就是"无 openssl 分支"；README 的"211/211（有 openssl）"本机无法验证，别当成实测结论（坑 41） |
| `tests/mcp_e2e.py` | MCP 协议（初始化/工具/批量/SSE/错误） | 固定端口，需先起测试服务（脚本内自己起） |
| `tests/local_e2e.py` | 本机直连小回归 | 固定端口 |
| `tests/mcp_exe_test.py` | 对 `dist\wkfile.exe` 的黑盒测试（v1.7.21 源码里 59 项；本轮未跑，exe 侧改由 `test/v1721/exe_blackbox.py` 抽查） | **先 `build-exe.ps1`**，否则测的是旧 exe |
| `tests/mcp_contract_test.py` | MCP 运行时契约（initialize 文案 + **逐页还原行为**），v1.7.21 实测 65/65 | `python tests\mcp_contract_test.py`；无端口 |
| `tests/exec_output_test.py` / `backup_retention_test.py` / `write_backup_timeout_test.py` / `optimistic_lock_test.py` | exec 输出截断、备份保留策略、写超时/备份失败、读乐观锁（v1.7.22 实测 46/46） | 逐个跑；v1.7.22 起 `optimistic_lock_test.py` 已无空断言 |
| `tests/v1721_test.py` | v1.7.21 五合一回归；v1.7.22 实测 88/88 | `python tests\v1721_test.py` |
| `tests/v1722_test.py` | v1.7.22 契约回归（P0 wire 序列化、回滚预算、exec 转发前缀、空断言清理）；实测 105/105 | `python tests\v1722_test.py`；全部工具结果过一次 `json.dumps` |
| `tests/v1723_test.py` | v1.7.23 八组回归：动态程序名 9 拦 + 6 不误伤、rollback=0 回退、`backup_required`、inode 去重、`dropped` 告警、probe 脱敏、文档口径、`json_safe`；**v1.7.28 实测 64/64**（v1.7.27 时 59/59） | `python tests\v1723_test.py`；无端口。注意 `test_docs` 仍是形状断言：README 同时有 195/211 时只捕获 211（坑 41） |
| `tests/v1724_test.py` | v1.7.24 回归：备份孤儿清理后不超删 + `physical` 排除孤儿 + 至少留 1 份、`_backup_policy` 与 cleanup 同口径、`orphan_objects`/`unreclaimable_bytes` 口径、exec 已知限制文案、`json_safe` | `python tests\v1724_test.py`；**v1.7.28 实测 51/51**（v1.7.27 曾 43/47）；已改为**行为断言**（无 monkeypatch、无 `_prune_backup_objects`）；无端口。⚠ "不误伤"用例只测**无 `.\` 的裸文件名**，带路径形式仍 400（坑 49） |
| `tests/v1725_test.py` | v1.7.25 回归（九组）：数值脏值回退 + 不崩、`config_warnings` 落 health/probe、类型 OK 的配置不被误伤、env 层仍走原告警、`_config_type_ok` 边界、启动清理不因脏值崩、README 用例数措辞、`使用说明` 指向 README、`json_safe` | `python tests\v1725_test.py`；**v1.7.28 实测 54/54**；无端口。**第 7 组只锁"195/211 同时出现"，不锁真实计数**（坑 41）；**未覆盖布尔/数组的 fail-open 方向**（坑 39） |
| `tests/v1726_test.py` | v1.7.26 回归（安全开关严格化）：`ConfigSafetyError`/`SAFETY_BOOL_KEYS`/`SAFETY_LIST_KEYS`、`coerce_safety_bool` 取值边界、`validate_safety_config` 三态（fatal/coerced/ok）、`safety_env_errors`、`format_config_warnings`、`token_ttl_days` 浮点默认值、普通键仍回退默认值 | `python tests\v1726_test.py`；**v1.7.28 实测 115/115**；无端口。**不覆盖热加载旁路**（坑 42，由 v1727/v1728 覆盖） |
| `tests/v1727_test.py` | v1.7.27 回归：`_validate_hot_key` 的规范化/丢弃分支（v1.7.28 扩充）、`coerce_safety_bool` 协议、`patch_plan` 0 语义、port/host 门禁、热加载告警进 health/probe | `python tests\v1727_test.py`；**v1.7.28 实测 83/83**（v1.7.27 时 73/73）；无端口。仍不覆盖"告警无上限增长"（坑 50） |
| `test/v1712/check.py` / `spotcheck.py` | v1.7.12 定向锁：游标、原子写、tls 围栏、junction；spotcheck 61 项（v1.7.21 实测 61 通过） | 无端口、纯临时目录 |
| `test/v1721/*.py` | v1.7.21 复核：读不变式（`read_audit`）、围栏/append（`fence_audit`）、exec 变体（`hijack_variants`）、wire 序列化（`patch_wire`）、包内 exe 黑盒（`exe_blackbox`）、exe 标记（`exe_markers`） | 见 `jiaoben.md` 3.1；`patch_wire`/`exe_blackbox` 会用临时端口 18931/18932，结束自动 kill |
| `test/v1722/*.py` | v1.7.22 复核：源码 wire（`wire_audit`，端口 18941）、exec 残余绕过/误伤（`guard_probe`）、exe 静态标记（`exe_strings`）、exe wire（`exe_wire`，复制 exe 被策略拦，未跑通） | 见 `jiaoben.md` 3.1 |
| `test/v1723/*.py` | v1.7.23 复核：**包内 exe 原地 wire**（`exe_inplace`，端口 18951，跑完清 `wkfile.pid`）、源码语义探针（`semantics_probe`，24 项，1 项因脚本预期写错）、孤儿清理复现（`cleanup_orphan_probe`） | 见 `jiaoben.md` 3.1；exe 原地跑法见坑 34。**`cleanup_orphan_probe.py` 在 v1.7.24 复跑已由"全删"变"全保留"** |
| `test/v1724/exe_inplace.py` | v1.7.24 复核：**包内 exe 原地 wire**（不复制、不挪走），端口 **18961**；验 `/health`（1.7.24）、`initialize.serverInfo.version`、`tools/list`（36）、`/api/patch`、`/mcp win_patch`、`/api/probe` 的 `backup` 键；finally 中 terminate/kill + 对比并清理包内新建文件 + 打印 `pkg_restored` | `python test\v1724\exe_inplace.py`；**跑前确认包目录无在跑实例**；只允许原地跑（复制出去仍会被 WinError 4551 拦） |
| `test/v1725/*.py` | v1.7.25 复核：包内 exe 原地 wire（`exe_inplace`，端口 **18971**）、包真伪（`pkg_verify`：目录/zip 逐文件哈希、键集、CRCRLF）、脏配置四例（`dirty_config_probe`，源码路线，端口 18972）、类型回退方向（`failopen_probe`，无端口）、exec 带路径误伤 + `patch_max_total_bytes=0`（`regression_probe`，无端口） | 见 `jiaoben.md` 3.1；全部只读、临时目录；跑完 kill + 还原包目录 |
| `test/v1726/*.py` | v1.7.26 复核（九个脚本，全部只读）：`pkg_verify`（16 项包真伪）、`exe_inplace`（包内原地 wire，端口 **18981**）、`safety_switch_probe`（安全开关 20+ 例，端口 18982/10087/8788）、`safety_effect_check`（生效性验证，端口 18985）、`leftover_probe`（`patch_max_total_bytes=0` + exec 路径误伤 + 未知键，端口 18983）、`null_failopen_probe`（`host/tls_cert/token` 为 null，端口 18984）、`misc_checks`（ttl 浮点/env 宽严/`[""]`/`port:null`，端口 18990/8788/18991）、`hotload_bypass_probe`（热改 `exec_strict_chain` 旁路，端口 18989）、`exe_safety_check`（exe 内安全开关，端口 18986/18987） | 见 `jiaoben.md` 3.1；每个脚本单独临时端口，跑完 kill + 等端口释放 + 还原包目录 |
| `test/v1727/*.py` | v1.7.27 复核（六个脚本，全部只读）：`pkg_verify`（含文档口径检查）、`exe_inplace`（包内原地 wire，端口 **19001**）、`hotload_probe`（首版热加载探针，端口 19011）、`hotload_matrix`（**确定性矩阵**：null/`"false"`/0/1/`"garbage"`/false/true + `exec_allowed_commands:123`，端口 **19012**）、`null_probe`（host/tls_cert null、`ip_allowlist:[""]`、三项安全开关回归，端口 19021-19026）、`leftover_probe`（patch 0、exec 路径误伤、`--init` 阻塞，端口 19031/19032） | 源码路线跑在临时副本里（data 目录=副本/wkfile，不碰真实数据目录）；每例独立端口，跑完 kill + 确认无 LISTEN |
| `test/v1728/*.py` | v1.7.28 复核（六个脚本，全部只读）：`pkg_verify`（14 文件/exe 属性/zip 逐哈希/键集/CRCRLF + 文档口径）、`exe_inplace`（包内原地 wire，端口 **19101**）、`exe_hotload_check`（**exe 二进制**热改 `0` → health.hotload_warnings 有值，端口 19115）、`hotload_matrix`（**8 种写法矩阵**：0/1/`"0"`/`"false"`/false/null/`"garbage"`/2 + `exec_allowed_commands:123`，端口 19102）、`patch_zero_probe`（两套 0 语义 + 9MB 拒绝，端口 19103）、`leftover_probe`（exec `.\` 误伤、tls_cert null、`ip_allowlist [""]`、`--init`、env 宽严、port 字符串，端口随机） | 见 `jiaoben.md` 3.1；exe 两个脚本都**原地跑**并在 finally 还原包目录 |
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

## 12. 高风险项状态（按 v1.7.28 复核）

> 结论口径：**已修 / 部分修 / 未修 / 新引入 / 已知限制**。行号已按 **v1.7.28** 源码刷新；标 v1.7.11/v1.7.16/v1.7.22/v1.7.23/v1.7.24/v1.7.25/v1.7.26/v1.7.27 的实测证据保留原样（那是当时的 exe/源码结果）。
> 2026-11-06 的原始清单保留编号，避免和设计文档里的 B1-B7 失联。v1.7.21 新增问题用 `N5` 起编号，v1.7.22 用 `N9` 起，v1.7.23 用 `N12` 起，v1.7.24 用 `N16` 起，v1.7.25 用 `N18` 起，v1.7.26 用 `N20` 起，v1.7.27 用 `N23` 起，**v1.7.28 用 `N25` 起**。

### P0（不改会出事）

| # | 问题 | 状态 | 证据 |
|---|---|---|---|
| P0-1 | 明文凭据（`.build`） | **已修（v1.7.12）** | `.build/gh_askpass.sh` 改读 `GH_TOKEN`；`.build/sqlsrv_variants.py:14` 改读 `SA_PWD`。历史 PAT 仍建议轮换 |
| P0-2 | `win_replace` >4MB 截断 | **已修** | `op_replace`（2578）接住 `truncated` → `too_large`；落盘走 `_atomic_write`（2463） |
| P0-3 | exec 白名单 cwd 劫持 | **已知限制（v1.7.24 明确不修，需配置兜底）** | `_exec_guard_hijack`（3693）+ `_exec_seg_programs`（3623）：`dir && call payload.bat`、`set X=payload.bat\ncall %X%`、`for %i in (...) do call %i`、`cmd /c %X%` 均拒（`tests/v1723_test.py` 第 1 组）。**已知不拦**：`dir && if exist payload.bat payload.bat`、`dir && (payload.bat)`、`dir && >nul payload.bat`（坑 38）；docstring 3700-3708 + README「已知限制」已写明"不是安全边界、不再加固"。**残余误伤**：`type .\deploy.ps1`、`copy .\payload.bat x`。生产靠 `exec_enabled=false`（默认）或 `exec_strict_chain=true` |
| P0-4 | 超时 / 无界线程 / 超大 body | **已修** | `BoundedThreadingHTTPServer`（默认 64 槽）；`Handler.timeout=120`（4371）；JSON 请求体上限 = `max_write_bytes`（`_read_body` 4479）；原始字节上传 `_read_raw_body` 4599 同口径 |
| P0-5 | GUI 停止误杀 | **已修** | `wkfile_gui.py:2602` 只按 PID `taskkill /PID ... /T /F` |
| **P0-6** | **`win_patch` 成功响应含 `bytes` → HTTP 500 / MCP 断连，文件却已写入** | **已修并已 exe 实测（v1.7.23 + v1.7.24）** | `op_patch`（2095）只回 `original_sha256`/`original_bytes`。v1.7.24 包内 exe **原地**实测（`test/v1724/exe_inplace.py`，端口 18961）：`/api/patch` 200、响应无 `original`、`/mcp win_patch` 200 且 `isError=false`、文件正确改写；`alive_after_kill=False`、`pkg_restored=True`。见坑 25/34 |
| **P0-7** | **`.objects` 孤儿 + 启动清理把全部备份删光** | **已修（v1.7.24）** | `cleanup_backups`（1813）先 `_prune_backup_objects()` 再统计；`_backup_entries`（1749）`physical` 只含被版本引用的 inode；`_backup_cleanup_plan`（1711）`fixed = total - reclaimable` 且保底留 1 份。复测 `test/v1723/cleanup_orphan_probe.py`：2MB 孤儿 + 2 个有效版本 + `backup_max_bytes=1.5MB` → 两版本都保留、孤儿被 prune、`would_remove=0`。见坑 37 |

### P1（高优先级）

| # | 问题 | 状态 | 证据 |
|---|---|---|---|
| 6 | 数据目录围栏缺 `tls` | **已修（v1.7.12）** | `_deny_data_dir`（1231）保护列表含 `tls`；本轮实测 `tls/wkfile.key` 的 read/list/write 全被拒 |
| 6b | `search/grep/tree` 逐条 resolve | **已修（v1.7.12）** | `op_tree` 3055 / `op_search` 3176 / `op_grep` 3236 均逐条解析；junction 实测不出 roots |
| 7 | `_backup` 失败静默 | **已修（v1.7.12）** | `_backup` 1539-1568 失败抛 `backup_failed`；`spotcheck.py` B 段 |
| 8 | 临时文件名固定 | **已修（v1.7.12）** | `_atomic_write`（2463）临时名 = `pid + token_hex` |
| 9 | `write_b64` 非原子 | **已修** | `op_write_b64`（2482）走 `_atomic_write` |
| 10 | `op_append` 无上限/全量备份 | **已修（上限）/ 部分修（备份）** | `op_append`（2555）按「追加后总长」判；备份仍整份，但可 `backup=false` 跳过 |
| 11 | `Config.save` 非原子/丢键 | **已修** | `load_config` 1125，`save`（998）tmp+replace + `_extra_keys` |
| 12 | exec 热加载解析注释失败 | **已修（v1.7.12）** | `_hotload_exec_cfg`（3555）先 `_strip_json_comments` |
| 13 | `win_read` 默认行数契约 | **已修（v1.7.21 完整）** | `op_read`（2231-2385）双游标 + 残页 + `content_ends_with_newline`；`test/v1721/read_audit.py` 四条不变式全过 |
| 14 | `sha256=true` 无大小上限 | **已修** | `op_stat`（2213）超限报错；`win_read hash=true` 同样封顶 |
| 15 | `_read_raw_body` 重复定义 | **已修** | 只剩一处（4390） |
| 16 | 审计写失败静默 | **已修（v1.7.18 增强）** | `Audit.write`（1307）失败进缓冲、恢复补写；`health()` 暴露 `dropped`（但 `issues` 不告警，见 N10） |
| **P1-6** | **`win_patch` 为回滚整份读取目标文件，内存无上限** | **已修并加固（v1.7.23），v1.7.25 补齐类型校验** | `_patch_rollback_budget`（2077）按 `patch_max_rollback_bytes` 预检，单文件与总量都判；**`0`/负数回退内置 8MB**并在 probe 报异常。**v1.7.25 起 `"8MB"` 在加载期回退为默认 8MB**，`op_probe`/`op_patch` 不再抛 ValueError（坑 35 已修；四种脏写法实测均不崩） |
| **P1-7** | **`op_probe`/`win_probe` 把根目录绝对路径与配置分层回给任何 Token** | **已定性（v1.7.24：不是权限边界，P2）** | `op_probe`（2193）只读时 roots 打码、备份路径 `<redacted>`、清 `audit.last_error`；`op_roots`（2279）**故意不脱敏**（AI 构造路径必需），README 明确写"probe 脱敏不是权限边界"（坑 33） |

### 原「v1.7.11 新增高风险项」复核

| # | 问题 | 状态 | 证据 |
|---|---|---|---|
| N1 | `win_read` 字节截断跳过残行 | **已修（v1.7.12，v1.7.21 完整）** | `spotcheck.py` A/D/E + `test/v1721/read_audit.py` |
| N2 | exec 链式命令绕过第七重围栏 | **改为已知限制（v1.7.24）** | 直呼脚本、`call`/`start`、`cmd /c`、变量/for 间接引用均已堵；**`if`/`(...)`/重定向前缀仍绕过**，已明确写进 README/契约/docstring（见 P0-3、坑 38） |
| N3 | MCP `structuredContent` 响应翻倍 | **已修（v1.7.13）** | >4096 字节省略 `content` 并置 `content_omitted`（`wkfile_mcp_endpoint.py:1334-1344`）；v1.7.22+ 未改 |
| N4 | 示例占位 Token 未进黑名单 | **已修（v1.7.12）** | `token_strength` 判 `bad`，非 loopback 拒绝启动 |
| **N5** | **备份硬链接去重与体积策略口径不一致** | **已修（v1.7.23/v1.7.24）** | `backup_inode_sizes` 按 inode 去重；`physical` 只含被版本引用的 inode、孤儿单列（坑 27/37） |
| **N6** | **`backup=false` 让"写前必备份"变成可选** | **已给约束（v1.7.23，默认关）** | 新增 `backup_required`，开启后四写工具拒 `backup=false`、`auto_backup=false` 冲突拒写；**默认 false，不显式开仍可拆**；`使用说明.txt` 未提（坑 28，v1.7.25 复测仍无） |
| **N7** | **`win_patch`/`win_validate` 的测试只调 Agent，不过 JSON** | **已修（v1.7.22/23/24）** | 空断言已换真实条件；`tests/v1722_test.py` 全工具 `json.dumps`；v1.7.23/v1.7.24 两轮 exe 原地实测补上 `/api/patch` + `/mcp win_patch` |
| N8 | 发布包文档与产品矛盾 | **部分修（v1.7.25 仍 P2）** | README 用例数已改为"195 或 211（浮动）"；但**本机实测仍是 195+2 跳过**、211 未证实（坑 24/41）；`使用说明.txt:206` 仍把带路径的 `type/copy` 说成"不再误拦"（坑 26） |
| **N9** | **exec 段内兜底误伤 `type/copy` 脚本文件** | **已修（v1.7.23，v1.7.24 未改）** | `type deploy.ps1`、`copy payload.bat x`、`type x > p.bat`、`echo 100% done` 已放行；**带路径的 `type .\deploy.ps1` 仍误伤**（坑 26） |
| **N10** | **审计 `dropped` 只暴露不告警** | **已修（v1.7.23）** | `health()` 回 `alerts` + `integrity=lossy`，`probe.issues` 告警、`ok=false`（坑 32） |
| **N11** | **`win_probe` 只读 Token 可见完整 roots** | **已定性（v1.7.24，P2）** | probe 打码 + README 明确不是权限边界，`op_roots` 故意不脱敏（坑 33） |
| **N12** | **脏数值配置让 `win_probe` 500** | **已真修（v1.7.25，P2→已修）** | `validate_config_types`（core 385）加载期回退默认值 + `config_warnings`；`op_probe` 2240-2246 落 `issues`，`/api/health` 4822-4828 报数量/键名。实测 `"2GB"`/`"8MB"`/`"30d"`/三项同脏：启动、probe、patch 全部 200（坑 35） |
| **N13** | **`win_backup_list.would_remove` 与真实清理不一致** | **已修（v1.7.24，P2→已修）** | `_backup_policy`（1857）已传 `total_bytes=physical`，与 cleanup 同口径（坑 36） |
| **N14** | **`.objects` 孤儿导致启动清理超删备份** | **已修（v1.7.24，原 P1）** | 见 P0-7、坑 37；`cleanup_orphan_probe.py` 复测两版本全保留 |
| **N15** | **围栏收窄后 `if`/`(...)`/重定向绕过** | **改为已知限制（v1.7.24，风险仍在）** | 见 P0-3、坑 38；README/schema/契约/docstring 已同步"不是安全边界"；v1.7.25 `使用说明` 已改为正确指向 README |
| **N16** | **`使用说明.txt` 缺"已知限制"清单与 `backup_required`；README 用例数与实测不符** | **部分修（v1.7.28 复测，P2）** | v1.7.25 已修自指与用例数措辞；v1.7.27 补 `backup_required`。**v1.7.28**：`使用说明.txt` 已有 **1.7.28 条目**（"v1.7.27 / v1.7.28 的关键变更"，含热加载规范化、patch 0 语义），`backup_required` 仍在。**仍缺**：README 的 4551 段与"v1.7.18~v1.7.25 未改 GUI"未删；README 的"当前状态（v1.7.26 … v1724 47 项）"整段停在旧版本。`features_e2e` 实测 **195/195 + 跳过 2（DB 链接）**，**本机无 openssl**，README "无 openssl 时 195" 这一支是**对的**（v1.7.27 报告"本机有 openssl"的前提错误，已更正，坑 41/47） |
| **N17** | **备份枚举 IO 放大** | **部分修（v1.7.27 起，v1.7.28 未动，P2）** | `_backup_entries`（1818）已把 `.objects` 枚举并进一次 `os.walk`，孤儿路径（1865-1872）复用；但每个版本目录仍 `os.walk`（1851）+ `_dir_size`（1882）**2 次/目录**。`cleanup_backups`（1885）不再额外 prune 全树，整体为"1 次全量 + 每目录 2 次"。见坑 40 |
| **N18** | **配置类型回退对安全开关 fail-open** | **已修（v1.7.26 启动路径；v1.7.27 普通键门禁；v1.7.28 回归通过）** | `ConfigSafetyError` / `SAFETY_BOOL_KEYS` / `SAFETY_LIST_KEYS` / `coerce_safety_bool` / `validate_safety_config` / `safety_env_errors` / `format_config_warnings` 均在 `wkfile_core.py`；`Config.__init__`（974）先于普通键校验。**v1.7.28 复测**：`"read_only":"true"`→true+告警、`"allow_delete":"false"`→false+告警、`ip_allowlist` 单字符串→exit 5（`test/v1728/leftover_probe.py` 随机端口），三项未被改坏；`port:"19110"` → exit 5 拒启。**残余**：热加载（已由 N24 修复）与 `tls_cert:null` 静默（N21）、env 宽严（N22） |
| **N19** | **`test_docs` 把未证实的 211 锁成"唯一口径"** | **未修（v1.7.28 复测仍为形状断言，P2）** | `tests/v1723_test.py` 的正则 `features_e2e\.py[^\n]*?(\d{3})\s*用例` **只捕获 211**；v1.7.28 用 `python -c` 复算：README 同时含 `195/195` 与 `211/211`，解析结果仍是 `{211}`，检查绿。典型"断言绿但契约错"（坑 41） |
| **N20** | **`exec_strict_chain` 可被热加载改成脏值绕过** | **已修（v1.7.28，原 P1）** | `_validate_hot_key`（3947）改为**先 `coerce_safety_bool` 规范化再写**。**实测**（`test/v1728/hotload_matrix.py`，端口 19102，白名单 `["echo","dir"]` + `echo hi && whoami`）：`0`/`"0"`/`"false"`/`false` → 200（False）+ 规范化告警（原生 `false` 不告警）；`1`/`"true"`/`true` → 400（True）；`null`/`"garbage"`/`2` → 400 + 丢弃保留旧值 + 告警。告警**同时进 run log、`/api/health.hotload_warnings`、`/api/probe.issues`+`config.hotload_warnings`**。`exec_allowed_commands:123` → 200（保留旧白名单），不再 500。**exe 二进制同样验证**（`exe_hotload_check.py`，19115）。残余：告警数组无上限（N25） |
| **N21** | **`null` 与普通键的静默回退** | **已修（v1.7.29；port/host 部分修于 v1.7.27）** | `port:null`/脏值/字符串 → 拒启；`host:null` → 回落 `0.0.0.0` + `config_warnings`（横幅+health+probe）。**v1.7.29 补上同源的 TLS 分支**：`tls_cert`/`tls_key`/`tls_chain` 显式写成 `null`/非字符串、或 cert/key 只配一个，均写 `config_warnings`（横幅+health+probe issues 都可见），不再静默 fail-open 成明文 HTTP；`--init` 在脏安全开关下仍 exit 5 属另一类（N21 剩余，非本 fix）。`tests/v1729_test.py` 组 A 全绿 |
| **N22** | **env 层宽严与文件层不一致；`ip_allowlist:[""]` 全拒** | **未修（v1.7.28 复测，P2）** | `WKFILE_READ_ONLY=yes` → read_only=True，`probe.issues` 与 `env_warnings` **均为空**（`leftover_probe.py` E 段）；`ip_allowlist:[""]` → `/api/*` 全 403（含本机）、`/health` 200、无告警（C 段）。见坑 44 |
| **N23** | **v1.7.27 把 prune 内联，`_prune_backup_objects` 成死代码 → `tests/v1724_test.py` 4 项红** | **已修（v1.7.28）** | 死代码已删除，4 条用例改为**行为断言**（无 monkeypatch）；复跑 **51/51**。其余套件 v1.7.28 全绿：v1721 88/88、v1722 105/105、v1723 64/64、v1725 54/54、v1726 115/115、v1727 83/83、features_e2e 195/195（跳过 2）。见坑 46 |
| **N24** | **热加载"接受"≠"规范化"：整数 `0` 静默关闭 `exec_strict_chain`** | **已修（v1.7.28）** | 现已规范化 + 三处告警（N20 证据）。见坑 42/45 |
| **N25** | **`_hotload_warnings` 无上限 append** | **新引入（v1.7.28，P2）** | `_record_hotload_warning`（3939）只 append，不裁剪不去重；反复改配置会让 `/api/probe.config.hotload_warnings`、`issues` 线性增长，进程内存同涨。建议 `deque(maxlen=20)` 或按 key+action 去重（坑 50） |
| **N26** | **`type .\deploy.ps1` / `copy .\payload.bat x` 仍 400，且 README 说"加白名单就能过"** | **已修（v1.7.29；行为未变，纠的是 README 误导）** | `type .\deploy.ps1` 被拦的真实原因是**围栏⑦ cwd 劫持防护**（`.\deploy.ps1` 是 cwd 优先于 PATH 的脚本引用，cwd 里有同名脚本就拒，`exec_hijack_suspected`），**不是白名单**；即便 `type` 已在 `exec_allowed_commands` 也照样被拦——按文档加白名单是白费功夫。v1.7.29 已改 README `:416` 段：说明这是 cwd 劫持、加白名单绕不过、正确绕法是绝对路径或文件 API。`tests/v1729_test.py` 组 B 用「type 已在白名单」为前提证明 cwd 劫持仍拦，且裸文件名 `type deploy.ps1` / 绝对路径不拦（`leftover_probe.py` A 段现象不变，只是文档不再误导） |
| **N27** | **`build-release.py` 无测试门禁** | **未修（v1.7.28，P2 流程）** | 脚本只做文件搬运/zip，不跑 `tests/*`、不校验回归是否全绿；v1.7.27 就是带 43/47 发的版。v1.7.28 虽已消红，但闸门仍缺，同类问题会复发（坑 51） |

---

## 13. 改动前 checklist

- [ ] 已读本文件第 7、9、12 节。
- [ ] 改动在现有成果上做，不另起一套；临时测试脚本放 `test/` 或 `tests/`，用完清理。
- [ ] 新增/修改脚本已登记到 `jiaoben.md`。
- [ ] 新增配置项：`DEFAULT_CONFIG` + `wkfile.config.example.json` + GUI 表单 + README + `使用说明.txt` 五处同步（v1.7.28 实测示例 **50/50**；`backup_required` 已进 `使用说明.txt`）。
- [ ] 改 exec/已知限制：README「已知限制」、`win_exec` schema、运行时契约、`_exec_guard_hijack` docstring、`使用说明.txt` **五处同步**（v1.7.25：前四处已同步；`使用说明` 已改为正确指向 README 的清单，自身不再声称逐条列出）。
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
# 源码启动服务（注意：wkfile_agent.py 不吃 --run）
python server\wkfile_agent.py --config server\wkfile\wkfile.config.json
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

## 16. 发布包说明与自检

> 最新：**v1.7.28（2026-11-08 复核）**见 16.0；16.0.1 的 v1.7.27、16.0.2 的 v1.7.26、16.0.3 的 v1.7.25 与 16.1 之后的 v1.7.11 记录仅作历史对照。
> 原则：**能对 exe 实测的就实测，测不了的明确写"需源码确认/未验证"**，不要用源码结论替代包内事实。

### 16.0 v1.7.28 发布包复核（2026-11-08）

- 目录 `D:\deving\bigmodelstudy\trae-winfile\wkfile-v1.7.28\`（实际路径，**14 个文件**），同名 zip 14 个条目且全部以 `wkfile-v1.7.28/` 开头。
- `wkfile.exe`：FileVersion = ProductVersion = **1.7.28.0**；目录内与 zip 内 **14 个文件逐个 SHA256 相同**（`test/v1728/pkg_verify.py`，全 PASS）。
- `server/wkfile_agent.py:111` `VERSION = "1.7.28"`，与包名、`/health` 一致。
- 文件清单与 `build-release.py` 的 `LAYOUT` 吻合（LAYOUT 11 项 + 脚本生成的 3 个 = 14）；LICENSE 1072 B；**无** config/Token/证书/日志/`.build`/测试脚本；示例配置 **50/50** 键对齐；文本 `\r\r\n` = **0**。
- **exe 真机 wire（原地执行，第六轮）**：`test/v1728/exe_inplace.py`（端口 **19101**，`cwd=包目录`、exe 不复制不挪走）：`/health` 版本 1.7.28、`initialize.serverInfo.version`=1.7.28、`tools/list` **36**（含 `win_patch`/`win_probe`，不含 `win_exec`）、`/api/patch` 200 无 `original`、`/mcp win_patch` 200 且 `isError=false`、`/api/probe` 有 `security` 块；跑完 `pkg_restored=True`、`wkfile.pid` 已清、无 LISTEN（只剩 TIME_WAIT）。
- **exe 二进制含本轮逻辑**（不是只改版本号）：`test/v1728/exe_hotload_check.py`（端口 19115）对**包内 exe** 启动 `exec_strict_chain=true`，运行中写 `0` → `/api/exec` **200**，`/api/health.hotload_warnings` 返回 count/keys；包目录已还原。
- **头号验收：热加载 `exec_strict_chain` 全写法矩阵**（`test/v1728/hotload_matrix.py`，端口 19102；白名单 `["echo","dir"]`，探针命令 `echo hi && whoami`；strict=true→400，false→200）：

  | 运行中写入 | `/api/exec` | 实际生效值 | 告警位置 |
  |---|---|---|---|
  | `0` | **200** | **False**（规范化） | run log + health + probe |
  | `1` | 400 | True（规范化） | run log + health + probe |
  | `"0"` | **200** | **False**（规范化） | run log + health + probe |
  | `"false"` | **200** | **False**（规范化，不再"静默无效"） | run log + health + probe |
  | `false` | 200 | False（原值） | 无（符合预期） |
  | `null` | 200（旧值 False） | 保留旧值（丢弃） | run log + health + probe |
  | `"garbage"` | 200（旧值 False） | 保留旧值（丢弃） | run log + health + probe |
  | `2` | 200（旧值 False） | 保留旧值（丢弃） | run log + health + probe |
  | `exec_allowed_commands: 123` | `/api/exec` **200** | 保留旧白名单 | run log（丢弃） |

  → **`0` 与 `"false"` 不再"静默关"或"静默无效"**：都真正生效为 False，且三处告警；拒绝类（null/垃圾/2）也是三处告警。
- **回归套件（v1.7.28 全绿）**：`tests/v1724_test.py` **51/51**（上版 43/47；4 条已改**行为断言**，文件内无 `monkeypatch`/`_prune_backup_objects`；死代码 `_prune_backup_objects` 已删除）；v1721 88/88、v1722 105/105、v1723 **64/64**、v1725 54/54、v1726 115/115、v1727 **83/83**、`features_e2e` **195/195 + 跳过 2**（R3 MySQL / R3n Redis 真库链路；**本机无 openssl**）。
- **两套 "0" 语义（`test/v1728/patch_zero_probe.py`，端口 19103）**：`patch_max_total_bytes=0` 与 `patch_max_rollback_bytes=0` **都已统一为"回退内置 8MB"**——小补丁 200；单批 9MB → **400「超过预算 8.0 MB」**，文件未被改写；2×4MB 合计 8MB 边界按 8MB 判。README `:918` 与 `使用说明.txt`（1.7.28 条目）都写明"0 永不表示不限"。**残余**：`probe.issues` 只为 rollback<=0 报一条，total=0 静默回退（坑 48）。
- **其余 1.7.27 遗留（`test/v1728/leftover_probe.py`）**：
  - `type .\deploy.ps1` / `copy .\payload.bat x` → **仍 400**；`type payload.bat` / `copy payload.bat x` → 200；README `:415` "加白名单就能过"不成立（坑 49）。
  - `tls_cert:null` → TLS 关闭，`probe.issues`/`config_warnings` **均空**（仍静默）。
  - `ip_allowlist:[""]` → `/api/*` 全 403（含本机）、`/health` 200、无告警。
  - `--init` + 脏安全开关 → **exit 5**（仍无法自愈）。
  - `WKFILE_READ_ONLY=yes`（env）→ read_only=True，`issues`/`env_warnings` 均空（仍宽）。
  - `port:"19110"` → **exit 5 拒启**；但生成配置注释仍写"会回退 8788"（坑 51）。
  - 备份枚举仍 **2 次/版本目录**（`os.walk` + `_dir_size`）。
  - `test_docs` 仍只捕获 211（README 里的 195/195 不被识别）。
  - `build-release.py` 仍**无测试门禁**（坑 51）。
- **这一版真修好的**：①热加载 `0`/`"0"`/`"false"` 规范化 + 三处告警（N20/N24 关闭）；②告警进 health/probe，不再只进 run log；③`tests/v1724_test.py` 51/51，死代码删除；④两套 0 语义统一 + 文档写明；⑤exe 二进制实测带新逻辑；⑥`使用说明.txt` 有 1.7.28 条目。
- **新引入的风险**：①`_hotload_warnings` 无上限 append（N25）；②README/配置注释仍有旧口径（`port` 回退 8788、"v1.7.18~v1.7.25 未改 GUI"、v1724 47 项）；③仍未加测试门禁，回归红了照样能发（N27）。
- 仍残留（P2）：N26 exec `.\` 误伤与 README `:415` 矛盾、N21 `tls_cert:null` 静默、N22 env/`[""]`、`--init` 阻塞、N17 备份 2 次遍历、N19 `test_docs` 形状断言。

### 16.0.1 v1.7.27 发布包复核（2026-11-08，历史）

- 目录 `D:\deving\bigmodelstudy\trae-winfile\wkfile-v1.7.27\`（实际路径，**14 个文件**），同名 zip 14 个条目且全部以 `wkfile-v1.7.27/` 开头。
- `wkfile.exe`：FileVersion = ProductVersion = **1.7.27.0**；目录内与 zip 内 **14 个文件逐个 SHA256 相同**（`test/v1727/pkg_verify.py`）。
- `server/wkfile_agent.py:111` `VERSION = "1.7.27"`，与包名、`/health` 一致。
- 文件清单与 `build-release.py` 的 `LAYOUT` 吻合（LAYOUT 11 项 + 脚本生成的 3 个 = 14）；LICENSE 1072 B；**无** config/Token/证书/日志/`.build`/测试脚本；示例配置 **50/50** 键对齐；文本 `\r\r\n` = **0**。
- **exe 真机 wire（原地执行，第五轮）**：`test/v1727/exe_inplace.py`（端口 **19001**，`cwd=包目录`、exe 不复制不挪走）：`/health` 版本 1.7.27、`initialize.serverInfo.version`=1.7.27、`tools/list` **36**（含 `win_patch`/`win_probe`，不含 `win_exec`）、`/api/patch` 200 无 `original`、`/mcp win_patch` 200 且 `isError=false`、`/api/probe` 有 `security` 块；跑完 `pkg_restored=True`、`wkfile.pid` 已清、`Get-NetTCPConnection -State Listen` 无残留。
- **头号验收（实测生效值，非代码推断）**：`test/v1727/hotload_matrix.py`（端口 19012；启动 `exec_enabled=true` + `exec_strict_chain=true`，白名单 `["echo","dir"]`，探针命令 `echo hi && whoami`）——
  | 运行中写入 | `/api/exec` | 告警 |
  |---|---|---|
  | `exec_strict_chain: null` | **400**（严格链仍开） | run log 一条"热加载被拒" |
  | `exec_strict_chain: "false"` | **400**（字符串被接受但按真值存，改动**静默无效**） | **无** |
  | `exec_strict_chain: 0`（整数） | **200 → whoami 被执行**（严格链被静默关闭） | **无（log/health/probe/横幅全无）** |
  | `exec_strict_chain: 1` | 400 | 无 |
  | `exec_strict_chain: "garbage"` | 400（丢弃保留旧值） | run log 告警 |
  | `exec_allowed_commands: 123` | `/api/exec` **200**（保留旧白名单） | 无；**不再 500、不再泄漏 `'int' object is not iterable`** |
- **null / 回归实测**（`test/v1727/null_probe.py`，端口 19021-19026）：`host:null` → 实际监听 **0.0.0.0**，且**横幅 `[!] 配置告警` + `/api/health` + `/api/probe.issues` 三处都有**；`tls_cert:null` → TLS **仍静默关闭**、三处均无告警；`ip_allowlist:[""]` → `/api/*` **仍全 403**（含本机）、`/health` 200、**无告警**；回归 `"read_only":"true"`→true+告警、`"allow_delete":"false"`→false+告警、`ip_allowlist` 单字符串→**exit 5**，三项未被改坏。
- **1.7.26 遗留实测**（`test/v1727/leftover_probe.py`）：`patch_max_total_bytes=0` 现为**"不限制"**（`/api/patch` 200、文件真的改了）但**无任何告警**，与 `patch_max_rollback_bytes=0`（回退 8MB + probe issue）口径仍不一致；`type .\deploy.ps1` / `copy .\payload.bat x` **仍误伤**；脏安全开关下 `--init` 仍 **exit 5**（无法用官方入口自愈）。
- **源码回归**：`tests/v1727_test.py` **73/73**、`tests/v1726_test.py` **115/115**（§16.0.1 记的 110/110 是当时快照）、`tests/v1725_test.py` 54/54、`tests/v1723_test.py` 59/59、`tests/v1722_test.py` 105/105、`tests/v1721_test.py` 88/88、`tests/features_e2e.py` **195/195 + 2 跳过**（跳过是真实 DB 链接，不是"无 openssl"）。**`tests/v1724_test.py` 43/47 红**（见下）。
- **这一版真修好的**：热加载 `exec_strict_chain` 写 `null`/垃圾值不再放行（丢弃保留旧值，N20 主体收口）；`exec_allowed_commands:123` 不再 500、异常文案不再外泄；`port` 脏值/`null` 改为拒启；`host:null` 回落同时进 `config_warnings`（三处可见）；`使用说明.txt` 补上 `backup_required`；`.objects` 枚举合并进 `_backup_entries`（N17 部分修）。
- **新引入**：①N23 `cleanup_backups` 内联删孤儿后 `_prune_backup_objects` 成死代码，`tests/v1724_test.py` 由 47/47 跌到 **43/47**；②N24 热加载"接受≠规范化"——`"false"` 静默无效、**整数 `0` 静默关掉严格链且无告警**，热加载告警只进 run log，不进 health/probe/横幅。
- 仍残留（P2）：`tls_cert:null` 静默关 TLS；`ip_allowlist:[""]` 全 403 无告警；`patch_max_total_bytes=0` 不限且无告警；`type .\deploy.ps1` 误伤；`--init` 阻塞；env 宽严不一致；`test_docs` 形状断言；README 4551 段与 GUI 版本区间过时；`使用说明` 无 1.7.27 更新条目；备份仍 2 次/目录。

### 16.0.2 v1.7.26 发布包复核（2026-11-08，历史）

- 目录 `D:\deving\bigmodelstudy\trae-winfile\wkfile-v1.7.26\`（实际路径；**14 个文件**），同名 zip 14 个条目且全部以 `wkfile-v1.7.26/` 开头。
- `wkfile.exe`：FileVersion = ProductVersion = **1.7.26.0**；目录内与 zip 内 **14 个文件逐个 SHA256 相同**（`test/v1726/pkg_verify.py`，16 项全过）。
- `server/wkfile_agent.py:111` `VERSION = "1.7.26"`，与包名、`/health` 一致。
- 文件清单与 `build-release.py` 的 `LAYOUT` 吻合（LAYOUT 11 项 + 脚本生成的 `以管理员身份启动.bat`/`文件夹说明.txt`/`wkfile\数据目录说明.txt` = 14）；LICENSE 1072 B；无 config/Token/证书/日志/`.build`/测试脚本；示例配置 **50/50** 键对齐；文本 `\r\r\n` = **0**。
- **exe 真机 wire（原地执行，第四轮）**：`test/v1726/exe_inplace.py`（端口 18981，`cwd=包目录`、exe 不复制）：`/health` 版本 1.7.26、`initialize.serverInfo.version`=1.7.26、`tools/list` **36**（含 `win_patch`/`win_probe`，不含 `win_exec`）、`/api/patch` 200 无 `original` 且回 `original_sha256`、`/mcp win_patch` 200 且 `isError=false` 带 `structuredContent`、`/api/probe` 有 `security` 块；跑完 `pkg_restored=True`、`wkfile.pid` 清理、端口释放。
- **头号验收（实测，非代码推断）**：`test/v1726/safety_switch_probe.py`（源码路线，逐例起服务）——`"read_only":"true"`→`/api/roots.read_only=true`+横幅 `[readonly ON]`+`配置告警：1 项 / read_only / file`；`"allow_delete":"false"`→`allow_delete=false`+`[delete OFF]`；`"backup_required":"true"`→`/api/probe.security.backup_required=true`+`[backup ON]`；`"exec_enabled":"false"`→false；`" TRUE "`/`1` 规范化；混合两脏值都正确。认不出的写法（`"yes"`/`null`/`2`/`1.0`/`ip_allowlist` 单字符串/`[""]`/`tools_disable` 单字符串/env `WKFILE_READ_ONLY=maybe`）**全部 exit 5 拒绝启动**，横幅含"拒绝启动/安全开关/[X]"。`"port":"10087"` → 普通键仍回退 8788（横幅有 port 告警并提示"按 listening 行端口连"）。
- **生效性验证**：`test/v1726/safety_effect_check.py`（端口 18985）——`read_only` 下 `/api/write`、`/api/trash` 均 400 `read_only`；`allow_delete:false` 下 trash 被拒且文件仍在、write 200；`backup_required:true` 下 `patch backup=false` 被拒、默认 patch 生成 1 个备份目录；`exec_enabled:false` 下 tools_count=35 无 `win_exec`、`/api/exec` 被拒。exe 内 `read_only:"true"`/`"yes"` 复测一致（`exe_safety_check.py`，拒启例端口未监听且包目录还原）。
- **源码回归**：`tests/v1726_test.py` **110/110**、`tests/v1725_test.py` **54/54**（旧文档错记为 89/89）、`tests/v1724_test.py` 47/47、`tests/v1723_test.py` 59/59、`tests/features_e2e.py` **195/195 + 2 跳过**（本机无 openssl）。
- **这一版真修好的**：N18 安全开关 fail-open（启动路径全部收口 + 启动横幅可见）；`token_ttl_days` 默认 `0.0` 修掉 GUI 写 30.0 被判脏回退 0 的旧坑；README 用例数措辞不再写死 211（实测 195/195+2）。
- **新引入/未收口**：N20 热加载 `exec_strict_chain` 旁路（P1，实测写 `null` 后 `&&` 复合命令放行、`exec_allowed_commands:123` → 500）；N21 普通键 `null` 静默回退 + `--init` 被脏安全开关阻塞；N22 env 宽严不一致 + `ip_allowlist:[""]` 全拒。
- 仍残留（P2）：`type .\deploy.ps1` / `copy .\payload.bat x` 仍误伤；`patch_max_total_bytes=0` 全拒且无告警；README 4551 过时段与"v1.7.18~25 未改 GUI"过时；`使用说明` 仍无 `backup_required` 配置说明；`test_docs` 仍是形状断言；备份清理 3 次遍历。

### 16.0.3 v1.7.25 发布包复核（2026-11-07，历史）

- 目录 `D:\deving\bigmodelstudy\trae-winfile\wkfile-v1.7.25\`（14 个文件），同名 zip 14 个条目且全部以 `wkfile-v1.7.25/` 开头。
- `wkfile.exe`：FileVersion = ProductVersion = **1.7.25.0**；目录内与 zip 内 **14 个文件逐个 SHA256 相同**（`test/v1725/pkg_verify.py`）。
- `server/wkfile_agent.py:109` `VERSION = "1.7.25"`，与包名、`/health` 一致。
- 文件清单与 `build-release.py` 的 `LAYOUT` 吻合；LICENSE 1072 B；无 config/Token/证书/日志/`.build`/测试脚本；示例配置 **50/50** 键对齐；文本 `\r\r\n` = **0**。
- **exe 真机 wire（原地执行，第三轮）**：`test/v1725/exe_inplace.py`（端口 18971，`cwd=包目录`、exe 不复制）：`/health` 版本 1.7.25、`initialize.serverInfo.version`=1.7.25、`tools/list` 36、`/api/patch` 200 且无 `original`、`/mcp win_patch` 200 且 `isError=false`、文件正确改写、`/api/probe` 带 `config.layers.config_warnings`；跑完 `alive_after_kill=False`、清 `wkfile.pid`、`pkg_restored=True`、端口释放。
- **源码回归**：`tests/v1725_test.py` **89/89**、`tests/v1724_test.py` 47/47、`tests/v1723_test.py` 59/59、`tests/features_e2e.py` **195/195 + 2 跳过**（本机无 openssl）。
- **脏配置实测**（`test/v1725/dirty_config_probe.py`）：`"2GB"` / `"8MB"` / `"30d"` / 三项同时脏 → 均正常启动；`/api/health` 报 1 条 `config_warnings`（数量+键名）、`/api/probe` 200 且 issues 指名键、`op_patch` 200。
- **这一版真修好的**：脏数值崩溃（N12/P1-6 残余）、README 用例数写死 211、`使用说明` 自指；包真伪/CRCRLF/50 键/exe wire 保持。
- **新引入**：配置类型回退对安全开关 **fail-open**（N18/P1，实测 `read_only:"true"`→False、`ip_allowlist` 字符串→[]）。
- 仍残留（P2）：`backup_required` 未进 `使用说明`；README `:935-941` exe 过时说明未删；`patch_max_total_bytes=0` 语义与错误码未说明；`type .\x.ps1` 误伤；备份清理 3 次遍历；`test_docs` 仍把 211 锁成唯一口径。

### 16.1 v1.7.11 包结构（历史）

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
