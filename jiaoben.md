# jiaoben.md —— 脚本登记簿

> 项目规矩：**新增/修改任何脚本，都要在这里登记一行**；删除脚本时保留记录并标注"已删除"。
> 状态说明：`长期` = 产品/构建/测试链路在用，必须维护；`临时` = 一次性排障/验证，可随时删；
> `历史` = 旧版本遗留，仅存档；`废弃` = 已归档不再使用；`无关` = 不属于本项目。
> 本表核对日期：2026-11-08（**v1.7.28 发布包增量复核**；当轮只做只读分析、改文档、临时端口在包目录内原地跑 exe）。

## 0. 安全红线（先看）

- `.build/gh_askpass.sh` 含**明文 GitHub PAT**；`.build/sqlsrv_variants.py:18` 仍含 18 位字面口令（其余 SQL 探针已改读环境变量）。这些文件已被 `.gitignore` 排除、git 历史中也没有（已核实），但仍以明文躺在磁盘上。**不要打包/外发 `.build`；建议先轮换口令并吊销 Token，再清理文件。**
- **v1.7.11 发布包本身已核实干净**：无配置/Token/证书/日志/`.build`，exe 内也无开发机路径或凭据明文。
- `server/wkfile/wkfile.config.json`、`server/wkfile/tls/`、`.trae/mcp.json` 含真实 Token/私钥，**永远不要提交、不要放进发布包**。
- 测试脚本会写临时配置和临时目录，**不要指向真实数据目录**；跑测试前确认固定端口空闲。

---

## 1. 构建与发布（长期）

| 脚本 | 状态 | 用途 | 运行方式 / 注意 |
|---|---|---|---|
| `build-exe.ps1` | 长期 | 用 `.build/venv` 里的 PyInstaller 把 `server/wkfile_gui.py` 打成 `dist/wkfile.exe`（onefile + windowed + 图标 + 版本资源） | `powershell -ExecutionPolicy Bypass -File .\build-exe.ps1`；版本号三处（`$AppVer`、`filevers`、`FileVersion` 字符串）目前需手动对齐，`$AppVer` 实际未被使用 |
| `build-release.py` | 长期 | 从 agent 的 `VERSION` 生成 `wkfile-v<版本>\` + zip，做敏感信息扫描、生成说明文件 | `python build-release.py`；会**直接 rmtree 同名发布目录**；不校验 `dist/wkfile.exe` 是否为新版 |
| `server/install-server.ps1` | 长期 | 纯 Python 路线的服务器部署：装运行时、写配置、注册计划任务、开防火墙 | 管理员 PowerShell：`.\install-server.ps1 -Roots "D:\share"`；文件刻意全 ASCII（PowerShell 5.1 编码坑）；`-TryDownloadPython` 下载 Python 无哈希校验 |
| `启动界面.bat` | 长期 | 源码路线启动 tkinter 界面（优先 `pythonw`，静默启动） | 双击；需本机 Python + tkinter |

## 2. 运行入口与服务端模块（长期）

| 脚本 | 状态 | 用途 | 说明 |
|---|---|---|---|
| `server/wkfile_agent.py` | 长期 | **主程序**：配置、路径围栏、备份/回收站、全部 `op_*`、HTTP API、MCP 挂载、SSE、CLI | **源码路线不认 `--run`**（`--run` 由 `wkfile_gui.py` 接住再转调本模块）；`--gen-token` / `--gen-readonly-token` / `--renew-token` / `--init` / `--gen-selfcert` / `--allow-unsafe` / `--check-gui`；详见 `wkxm.md` |
| `server/wkfile_core.py` | 长期 | v1.7.21 抽出的纯逻辑层：Token 强度/有效期、配置三层合并、编码/哈希、超时夹取、去重路径、补丁结构预检 | 被 agent/GUI/MCP 层 import；**不得反向 import `wkfile_agent`（会成环）**；无 I/O、无第三方依赖 |
| `server/wkfile_gui.py` | 长期 | tkinter 图形界面：状态、服务/Token、HTTPS/证书、数据库、其它、关于 | 直接 `python server\wkfile_gui.py`；打包后是 `wkfile.exe` 的默认入口（带 `--run` 时转服务） |
| `server/wkfile_mcp_endpoint.py` | 长期 | MCP 协议层（被 agent import，不单独运行） | 工具定义/路由/JSON-RPC；新增工具要同步 `TOOLS` + `ROUTE_MAP` + `Agent.ROUTES` |
| `server/wkfile_mysql.py` | 长期 | 手写 MySQL/MariaDB 协议客户端（含 caching_sha2 + RSA-OAEP SHA-1） | 被 agent import；无连接池 |
| `server/wkfile_redis.py` | 长期 | 手写 Redis RESP2 客户端，读写命令分类 + 危险命令黑名单 | 被 agent import |
| `server/wkfile_sqlsrv.py` | 长期 | 手写 SQL Server TDS 7.4 客户端（PRELOGIN/LOGIN7/TLS/SQL batch） | 被 agent import；TLS 默认不校验证书 |
| `server/wkfile_selfcert.py` | 长期 | 纯标准库 RSA + X.509 自签证书（SAN、证书链合并） | 被 agent/GUI import；私钥写盘后权限未收紧 |
| `server/connect-check.py` | 长期 | 连接自检：读本地配置，探测 Python 版本、端口、HTTP、MCP、TLS、数据库 | `python server\connect-check.py`；优先读同目录 `wkfile\wkfile.config.json` |
| `server/连接自检.bat` | 长期 | `connect-check.py` 的双击包装 | 双击；UTF-8 代码页 |

## 3. 测试脚本

| 脚本 | 状态 | 用途 | 运行方式 / 注意 |
|---|---|---|---|
| `selftest.py` | 长期（部分历史） | 老版 HTTP API 回归（约 53 项）+ 已废弃 stdio 桥接的 MCP 冒烟 | `python selftest.py`；固定端口 8899；`--keep` 保留临时目录；docstring 里的 `.build/*.py` 路径已过时 |
| `tests/features_e2e.py` | 长期 | **主回归**：围栏、备份、回收站、编码、只读、exec、TLS、数据库错误路径 | `python tests\features_e2e.py`；固定端口；`mkdtemp`；失败保留现场。**v1.7.23~v1.7.27 实测均为 `195/195`，另 2 项跳过**——跳过的是**真实 DB 连接用例**（本机有 openssl，不是"无 openssl 跳过"，v1.7.27 已更正归因）；README 现写"195 或 211（随 openssl 浮动）"，211 代码自洽但本机未证实（坑 47） |
| `tests/mcp_e2e.py` | 长期 | MCP 协议回归：initialize / tools/list / tools/call / 批量 / SSE / 错误 | 固定端口；脚本自己起测试服务 |
| `tests/local_e2e.py` | 长期 | 本机直连小回归 | 固定端口 |
| `tests/mcp_exe_test.py` | 长期 | 对 `dist\wkfile.exe` 的黑盒测试（v1.7.16 运行时报约 56 项） | **必须先 `build-exe.ps1`**，否则测到的是旧 exe |
| `tests/mcp_contract_test.py` | 长期 | v1.7.13 新增：MCP 运行时契约静态断言（initialize instructions 里的上限/执行工具说明、`win_read` 续读文案、`structuredContent` 去重阈值） | `python tests\mcp_contract_test.py`（v1.7.21 实测 65 项全绿）；**只比对文案字符串，不验证行为**——契约类改动必须再配行为用例 |
| `tests/gui_refresh_test.py` | 长期 | v1.7.15 新增：GUI 状态刷新节流与异步探测的离线检查（不启界面，验证合并/节流/`after(0)` 路径） | `python tests\gui_refresh_test.py`；需要 tkinter，无显示环境会跳过 |
| `tests/backup_retention_test.py` | 长期 | v1.7.18 新增：备份保留策略（`backup_retain_days/count` + `backup_max_bytes` 三线清理）+ 真实 Agent 目录清理 | 无服务无窗口，纯函数 + 临时目录；`python tests\backup_retention_test.py` |
| `tests/exec_output_test.py` | 长期 | v1.7.18 新增：`win_exec` 输出 head/tail 预览 + `win_tail` 读 `logs/exec-*.txt` 白名单口回归 | `python tests\exec_output_test.py`；不启服务 |
| `tests/write_backup_timeout_test.py` | 长期 | v1.7.19 新增：写类工具 `backup=false` 一致性（数备份目录文件）+ `exec_timeout_max_sec` 可配且只夹不放大 | `python tests\write_backup_timeout_test.py`；真实调 `op_append/op_replace/op_exec` |
| `tests/optimistic_lock_test.py` | 长期 | v1.7.20 新增：乐观锁闭环（`win_read` 回 `sha256`、冲突错误回当前 hash、`win_replace` 支持 `expected_sha256`） | `python tests\optimistic_lock_test.py`（v1.7.22 实测 46/46）；v1.7.22 起原来的空断言已换成真实条件 |
| `tests/v1721_test.py` | 长期 | v1.7.21 新增：五合一行为回归（A 纯逻辑外提 / B 配置分层 / C 审计降级 / D 增量备份 / E `win_validate`·`win_patch`·`win_probe` 双通道） | `python tests\v1721_test.py`（v1.7.22 实测 88/88）；空断言已在 v1.7.22 修掉 |
| `tests/v1722_test.py` | 长期 | v1.7.22 新增：契约回归（P0 `win_patch` wire 序列化、回滚预算、exec 转发前缀、空断言清理）；对全部工具结果做 `json.dumps` | `python tests\v1722_test.py`（v1.7.22 实测 105/105）；仍偏弱：工具冒烟用空参数，成功路径未逐个覆盖 |
| `tests/v1723_test.py` | 长期 | v1.7.23 新增：八组契约回归（exec 动态程序名 9 拦 + 6 不误伤、`patch_max_rollback_bytes=0` 回退、`backup_required`、inode 去重、审计 `dropped` 告警、probe 脱敏、文档口径、`json_safe`） | `python tests\v1723_test.py`（**v1.7.28 实测 64/64**；v1.7.27 时 59/59）；无端口；`test_docs` 只断言数字唯一，不校验数字与实测一致（README 同时有 195/211 时只捕获 211，坑 41） |
| `tests/v1724_test.py` | 长期 | v1.7.24 新增：备份孤儿 prune 后不超删 + `physical` 排除孤儿 + 至少留 1 份、`_backup_policy` 与 cleanup 同口径、`orphan_objects`/`unreclaimable_bytes`、exec 已知限制文案、`json_safe`；含"再放一次孤儿"防假绿；**v1.7.28 起 4 条孤儿用例改为行为断言**（不再 monkeypatch 内部函数） | `python tests\v1724_test.py`（**v1.7.28 实测 51/51**；v1.7.27 曾 43/47，原因是 `_prune_backup_objects` 死代码让 monkeypatch 失效，坑 46 已关闭）；无端口；**不校验 README 用例数**；"不误伤"只测裸文件名（坑 49） |
| `tests/v1725_test.py` | 长期 | v1.7.25 新增（九组）：数值脏值回退 + 不崩、`config_warnings` 落 health/probe、类型正确的配置不被误伤、env 层仍走原告警、`_config_type_ok` 边界、启动清理不因脏值崩、README 用例数措辞、`使用说明` 指向 README、`json_safe` | `python tests\v1725_test.py`（**v1.7.26 复跑实测 54/54**；旧记录 89/89 是错记，已更正）；无端口；**第 7 组不锁真实计数、未覆盖布尔/数组 fail-open 方向**（坑 39/41） |
| `tests/v1726_test.py` | 长期 | v1.7.26 新增：安全开关严格化回归——`ConfigSafetyError` / `SAFETY_BOOL_KEYS` / `SAFETY_LIST_KEYS`、`coerce_safety_bool` 取值边界、`validate_safety_config` 三态（fatal/coerced/ok）、`safety_env_errors`、`format_config_warnings`、`token_ttl_days` 浮点默认值、普通键仍回退默认值 | `python tests\v1726_test.py`（**v1.7.27 复跑实测 115/115**；发布当时记的 110/110）；无端口；**不覆盖热加载旁路**（坑 42/45） |
| `tests/v1727_test.py` | 长期 | v1.7.27 新增、**v1.7.28 扩充**：热加载守门回归——`_validate_hot_key` 对 `exec_strict_chain` 的规范化（0/1/`"0"`/`"false"`）与丢弃分支、`exec_allowed_commands` 非数组、告警进 health/probe、`port` 拒启、`host:null` 告警、`patch_max_total_bytes<=0` 语义、备份内联 prune | `python tests\v1727_test.py`（**v1.7.28 实测 83/83**；v1.7.27 时 73/73）；无端口；仍不覆盖"`_hotload_warnings` 无上限增长"（坑 50） |
| `tests/tls_e2e.py` | 长期 | HTTPS / 证书链 / 自签证书回归 | 需要证书环境；固定端口 |
| `tests/sqlsrv_proto_test.py` | 长期 | TDS 协议离线用例（不需要真实 SQL Server） | 可直接跑 |
| `.build/features_e2e.py`、`.build/local_e2e.py`、`.build/mcp_e2e.py`、`.build/mcp_exe_test.py`、`.build/tls_e2e.py` | 历史 | 早期测试副本，已与 `tests/` 分叉（`mcp_exe_test.py` 差 12 KB） | **勿运行、勿修改；以 `tests/` 为准**，建议后续删除 |

### 3.1 临时发布包复核脚本（`test/`，2026-11-07 ~ 2026-11-08）

| 脚本 | 状态 | 用途 | 运行方式 / 注意 |
|---|---|---|---|
| `test/pkgcheck/gen.py` | 临时 | 生成复核用样本：1000 行 `lines.txt`、4,403,100 字节 `big.txt`（第 4001 行超长，尾部标记在 4MB 之后） | `python test\pkgcheck\gen.py`；只写 `test/pkgcheck/share` |
| `test/pkgcheck/client.py` | 临时 | 对临时 exe 实例发 MCP `initialize/tools/list/tools/call`，验证 `win_read` 默认行数、游标、`win_replace` 大文件、请求体上限 | 先按 wkxm 第 16.6 节起临时实例（端口 18911），再 `python test\pkgcheck\client.py` |
| `test/pkgcheck2/`、`test/pkgcheck3/` | 临时 | 示例配置原样启动验证 / exec 链式绕过验证的 exe + 配置夹具（**各含 12.6MB exe 副本**） | 只用于复现，复现完可整目录删除；`pkgcheck3` 内 `share\payload.bat` 是链式绕过样本 |
| `test/v1712/check.py` | 临时 | v1.7.12 定向自测：WK-001 游标（续读到 EOF、双游标、max_lines 分页、eof）、WK-006 `op_replace` 原子写、WK-003 tls 围栏、WK-004 junction/symlink | `python test\v1712\check.py`；只建临时目录、不占端口、不起服务；当前 13 项 ALL PASS |
| `test/v1712/spotcheck.py` | 临时 | reviewer 抽验：残行（A 段断言 sum(consumed)=size、sum(returned_lines)=4、L3/L4=3/4）、6MB 单行超 max_read_bytes（D 段）、换行压在窗口边界 limit-1/limit/limit+1 四条不变式（E 段）、`_backup` 失败影响面（B）、`op_append` 边界（C） | `python test\v1712\spotcheck.py`；只建临时目录、monkeypatch `copy2` 模拟磁盘满；输出重定向到 `test\v1712/spotcheck.log`；E 段是 v1.7.13「断言全绿但契约错误」的回归锁 |
| `test/v1712/*.log` | 临时 | 自测输出留存：`spotcheck.log`、`check.log`、`mcp_exe_test.log`、`build.log` | 仅存档，可随时删；`mcp_exe_test.log` 需先重建 `dist` |
| `test/v1721/read_audit.py` | 临时 | v1.7.21 `win_read` 分页不变式：A 末尾有/无换行与单行/空文件、B 换行压边界 `limit±1`、C `max_bytes` 不被放大、D 无换行长行 `line_fragment` 与 `next_start_line` 不前进 | `python test\v1721\read_audit.py`；只建临时目录，不起服务；日志 `test\v1721\_audit\read.log` |
| `test/v1721/fence_audit.py` | 临时 | v1.7.21 围栏复核：exec 桩化（替换 `subprocess.Popen`）、`tls` 目录禁读、备份目录禁读、junction 遍历、`append` 总长上限与整份备份 | `python test\v1721\fence_audit.py`；日志 `_audit\fence.log`；有 2 条"预期失败"（`call`/`start` 放行） |
| `test/v1721/hijack_variants.py` | 临时 | 链式劫持变体：`dir && payload.bat` / `%cd%\payload.bat` 应拒，`dir && call payload.bat` / `dir && start payload.bat` 实测放行（P1 证据） | `python test\v1721\hijack_variants.py`；日志 `_audit\hijack.log` |
| `test/v1721/patch_wire.py` | 临时 | `win_patch` 线上证据（v1.7.21 时期）：临时实例（端口 **18931**）下 `/api/patch` 返回 500、`/mcp tools/call win_patch` 断连，但文件已改、审计记成功；**v1.7.22 已修**，复跑应得 200 | `python test\v1721\patch_wire.py`；脚本自起自停；日志 `_audit\wire.log` |
| `test/v1721/exe_blackbox.py` | 临时 | 发布包内 exe 黑盒：复制 exe 到临时目录（端口 **18932**），验 `/health`、`initialize` 版本 1.7.21、`tools/list`=36、`win_read` 的 `content_ends_with_newline` 有/无换行 | `python test\v1721\exe_blackbox.py`；脚本自行 kill 并校验 `alive_after_kill=False`；日志 `_audit\exebox.log` |
| `test/v1721/exe_markers.py` | 临时 | 检查 exe 内是否打入 `wkfile_core`（PyInstaller TOC 计数） | `python test\v1721\exe_markers.py`；日志 `_audit\exe.log` |
| `test/v1721/_audit/*.log` | 临时 | v1.7.21 复核全部证据日志：`read/fence/hijack/wire/exebox/exe/pkg/pkg2/git/compat/lines/map/spotcheck/v1721test/contract` | 仅存档；结论引用的行号/数字出处都在这里 |
| `test/v1722/wire_audit.py` | 临时 | v1.7.22 源码 wire：临时实例（端口 **18941**）验证 `/api/patch` 200、响应无 `original` bytes、`/mcp win_patch` 200 且带 `structuredContent`；P0-6 已修的证据 | `python test\v1722\wire_audit.py`；脚本自起自停并校验 `server_alive_after_kill=False` |
| `test/v1722/exe_wire.py` | 临时 | v1.7.22 包内 exe 黑盒（端口 18942）：复制 exe 起服务后测 `/api/patch` 与 `/mcp win_patch` | 本机应用控制策略（WinError 4551）拦截复制出的 exe，**本轮未跑通**；换机器或加白名单后可复用 |
| `test/v1722/guard_probe.py` | 临时 | v1.7.22 exec 围栏：14 个变体（`call`/`start`/`cmd /c`/相对路径应拒；变量与 for 间接引用实测仍放行；`type/copy` 脚本误伤） | `python test\v1722\guard_probe.py`；只调 `_exec_guard_hijack`，不执行命令、不启服务 |
| `test/v1722/exe_strings.py` | 临时 | v1.7.22 exe 静态标记：扫描原始字节 + 解压 zlib 条目，确认含 `original_sha256` / `patch_max_rollback_bytes` / `_exec_seg_programs` | `python test\v1722\exe_strings.py`；只读 exe |
| `test/v1723/exe_inplace.py` | 临时 | **v1.7.23 exe 真机 wire（首次跑通）**：在 `wkfile-v1.7.23\` 包目录内**原地**执行 exe（不复制、不挪走），临时配置/共享目录放 `%TEMP%`；验 `/health` 版本、`tools/list`、`/api/patch`、`/mcp win_patch`，结束 kill 并清理包内新建的 `wkfile.pid`，校验 `pkg_restored` | `python test\v1723\exe_inplace.py`；端口 **18951**；**跑前确认包目录无在跑实例**；只允许原地跑（复制出去仍会被 WinError 4551 拦） |
| `test/v1723/semantics_probe.py` | 临时 | v1.7.23 源码语义探针：围栏 15 例、inode 去重、`backup_required`、patch 上限、probe 脱敏 | `python test\v1723\semantics_probe.py`；不起服务、只调 Agent；实测 23/24，唯一 FAIL 是脚本把 `patch_max_total_bytes=0` 的期望写成 `too_large`（实际 `bad_request`） |
| `test/v1723/cleanup_orphan_probe.py` | 临时 | `.objects` 孤儿 + 启动清理（`Agent.__init__`）是否把有效备份版本全部删光；同时打印 `_backup_policy().would_remove` | `python test\v1723\cleanup_orphan_probe.py`；临时目录、不起服务。**v1.7.23 输出"两个有效版本被超删、孤儿还在、would_remove=0"（P1 复现）；v1.7.24 复跑已变成"两版本全保留、孤儿被 prune、would_remove=0"（修复证据）** |
| `test/v1724/exe_inplace.py` | 临时 | **v1.7.24 exe 真机 wire**：在 `wkfile-v1.7.24\` 包目录内**原地**执行 exe（不复制、不挪走），临时配置/共享目录放 `%TEMP%`；验 `/health` 版本 1.7.24、`initialize.serverInfo.version`、`tools/list`=36、`/api/patch`、`/mcp win_patch`、`/api/probe` 的 `backup` 键；finally terminate/kill + 对比并清理包内新建文件 + 打印 `pkg_restored` | `python test\v1724\exe_inplace.py`；端口 **18961**；**跑前确认包目录无在跑实例**；只允许原地跑（复制出去仍被 WinError 4551 拦） |
| `test/v1725/exe_inplace.py` | 临时 | **v1.7.25 exe 真机 wire**：包目录内原地执行（不复制、不挪走），端口 18971；验 `/health`=1.7.25、`initialize.serverInfo.version`、`tools/list`=36、`/api/patch`（无 `original`）、`/mcp win_patch`（`isError=false`）、`/api/probe` 带 `config.layers.config_warnings`；finally kill + 清理包内新建文件 + `pkg_restored` | `python test\v1725\exe_inplace.py`；**跑前确认包目录无在跑实例**；只允许原地跑 |
| `test/v1725/pkg_verify.py` | 临时 | 包真伪：目录 vs zip 逐文件 SHA256、zip 条目前缀、示例配置键集 vs `DEFAULT_CONFIG`、敏感文件、LICENSE、CRCRLF、`VERSION` | `python test\v1725\pkg_verify.py`；只读包与 zip |
| `test/v1725/dirty_config_probe.py` | 临时 | 脏数值配置四例（`"2GB"`/`"8MB"`/`"30d"`/三项同脏）单独起服务，验进程存活、`/api/health` 的 `config_warnings`、`/api/probe` 的 `issues`、`/api/patch` 是否 200 | `python test\v1725\dirty_config_probe.py`；源码路线、端口 **18972**；每例结束 kill |
| `test/v1725/failopen_probe.py` | 临时 | 类型回退方向审计：`read_only:"true"`、`allow_delete:"false"`、`ip_allowlist:"10.0.0.0/8"`、`backup_required:"true"`、`exec_enabled:"false"`、`port:"10087"` 的最终生效值与告警 | `python test\v1725\failopen_probe.py`；无端口、不写盘；**发现安全开关 fail-open**（坑 39） |
| `test/v1725/regression_probe.py` | 临时 | 两处遗留项实测：`type .\deploy.ps1` / `copy .\payload.bat x` 是否仍误伤；`patch_max_total_bytes=0` 的错误码与文件是否被改写 | `python test\v1725\regression_probe.py`；无端口、临时目录 |
| `test/v1726/pkg_verify.py` | 临时 | v1.7.26 包真伪（16 项）：exe 属性版本、`VERSION`、目录 vs zip 逐文件 SHA256、zip 前缀、文件数=14、示例配置 50/50、敏感文件、LICENSE、CRCRLF | `python test\v1726\pkg_verify.py`；只读包与 zip；日志 `test\v1726\pkg.log` |
| `test/v1726/exe_inplace.py` | 临时 | **v1.7.26 exe 真机 wire**：包目录内**原地**执行（不复制、不挪走），端口 **18981**；验 `/health`=1.7.26、`initialize.serverInfo.version`、`tools/list`=36、`/api/patch`（无 `original` 且有 `original_sha256`）、`/mcp win_patch`（`isError=false` + `structuredContent`）、`/api/probe.security`；finally kill + 清 `wkfile.pid` + 还原目录 + 端口释放 | `python test\v1726\exe_inplace.py`；**跑前确认包目录无在跑实例**；只允许原地跑；日志 `exe_inplace.log` |
| `test/v1726/safety_switch_probe.py` | 临时 | **头号验收**：安全开关类型错逐例起服务观察生效值——`read_only:"true"`、`allow_delete:"false"`、`backup_required:"true"`、`exec_enabled:"false"`、`" TRUE "`/`1`、混合脏值、`port:"10087"`，以及 `"yes"`/`null`/`2`/`1.0`/`ip_allowlist` 单字符串/`[""]`/`tools_disable` 单字符串/env 脏值是否 exit 5 拒启；同时抓启动横幅告警 | `python test\v1726\safety_switch_probe.py`；源码路线，端口 18982/10087/8788；每例结束 kill；日志 `safety.log` |
| `test/v1726/safety_effect_check.py` | 临时 | 生效性验证（不只看字段）：`read_only` 下 `/api/write`+`/api/trash` 全拒；`allow_delete:false` 下 trash 拒且文件仍在、write 200；`backup_required:true` 下 `patch backup=false` 拒、默认 patch 生成备份；`exec_enabled:false` 下无 `win_exec` 且 `/api/exec` 拒 | `python test\v1726\safety_effect_check.py`；端口 **18985**；日志 `safety_effect.log` |
| `test/v1726/leftover_probe.py` | 临时 | v1.7.25 遗留逐条判死活：A `patch_max_total_bytes=0`（错误码/文件是否改/告警）；B `type .\deploy.ps1`、`copy .\payload.bat x`、无路径变体是否仍误伤；C 未知键 `require_tls` 是否静默忽略 | `python test\v1726\leftover_probe.py`；端口 **18983**；日志 `leftover.log` |
| `test/v1726/null_failopen_probe.py` | 临时 | `null` 类 fail-open：`host:null`（监听地址）、`host:123`、`tls_cert:null`（TLS 是否静默关）、`token:null`/`token:12345`（是否自动生成而非免鉴权） | `python test\v1726\null_failopen_probe.py`；端口 **18984**；日志 `null_failopen.log` |
| `test/v1726/misc_checks.py` | 临时 | 残余项：`token_ttl_days=30.0` 是否保留、env `WKFILE_READ_ONLY=yes` 宽严、`port:null` 回退、`ip_allowlist:[""]` 是否全拒且 `/health` 不受 IP 过滤 | `python test\v1726\misc_checks.py`；端口 18990/8788/18991；日志 `misc.log` |
| `test/v1726/hotload_bypass_probe.py` | 临时 | **热加载旁路（新风险 N20）**：启动态 `exec_strict_chain=true` 时 `echo hi && whoami` 被拒；热改配置为 `null` 后同命令是否放行；热改 `exec_allowed_commands:123` 是否 500 且泄漏异常文案；health/横幅是否有告警 | `python test\v1726\hotload_bypass_probe.py`；端口 **18989**；日志 `hotload.log` |
| `test/v1726/exe_safety_check.py` | 临时 | exe 内安全开关实测：`read_only:"true"` 起服务后 `/api/roots.read_only=true` + 横幅告警；`read_only:"yes"` 是否 exit 5 拒启、端口未监听；每例独立端口并 `wait_port_free` + 还原包目录 | `python test\v1726\exe_safety_check.py`；端口 18986/18987；日志 `exe_safety.log` |
| `test/v1727/pkg_verify.py` | 临时 | v1.7.27 包真伪 + 文档口径：exe 属性版本、`VERSION`、目录 vs zip 逐文件 SHA256、zip 前缀、文件数=14、示例配置 50/50、敏感文件、LICENSE、CRCRLF；另查 `使用说明.txt` 是否提 `backup_required`、README 的 patch 0 语义 | `python test\v1727\pkg_verify.py`；只读包与 zip；日志 `test\v1727\pkg.log` |
| `test/v1727/exe_inplace.py` | 临时 | **v1.7.27 exe 真机 wire**：包目录内**原地**执行（不复制、不挪走），端口 **19001**；验 `/health`=1.7.27、`initialize.serverInfo.version`、`tools/list`=36、`/api/patch`（无 `original`）、`/mcp win_patch`（`isError=false`）、`/api/probe.security`；finally kill + 清 `wkfile.pid` + 还原目录 + 端口释放 | `python test\v1727\exe_inplace.py`；**跑前确认包目录无在跑实例**；只允许原地跑；日志 `exe_inplace.log` |
| `test/v1727/hotload_probe.py` | 临时 | 首版热加载探针（已被 `hotload_matrix.py` 取代）：控制组白名单误含 `whoami`，导致 `strict=true` 时基线也 200，无法作负控 | 仅存档，**不要引用其结论**；端口 19011 |
| `test/v1727/hotload_matrix.py` | 临时 | **v1.7.27 头号验收（确定性矩阵）**：启动 `exec_enabled=true`+`exec_strict_chain=true`，白名单 `["echo","dir"]`，运行中依次写 `null` / `"false"` / `0` / `1` / `"garbage"` / `false` / `true` 并发 `echo hi && whoami`，再写 `exec_allowed_commands:123`；逐例打印磁盘值、`mtime_ns`、状态码、日志增量告警、health/probe alerts | `python test\v1727\hotload_matrix.py`；源码路线，端口 **19012**；写后 `fsync` + 真实 mtime + 1.3s 间隔（避免时序错位）；日志 `hotload_matrix.log` |
| `test/v1727/null_probe.py` | 临时 | v1.7.27 null 语义 + 回归：`host:null`（实际监听 0.0.0.0 + 三处告警）、`tls_cert:null`（是否静默关 TLS）、`ip_allowlist:[""]`（是否全 403 且无告警）、`"read_only":"true"` / `"allow_delete":"false"` / `ip_allowlist` 单字符串三项回归 | `python test\v1727\null_probe.py`；源码路线，端口 **19021-19026**；每例结束 kill；日志 `null.log` |
| `test/v1727/leftover_probe.py` | 临时 | v1.7.26 遗留逐条判死活：A `patch_max_total_bytes=0` + `patch_max_rollback_bytes=0`（是否改写/回退/告警）；B `type .\deploy.ps1`、`copy .\payload.bat x` 是否仍误伤；C 脏安全开关下 `--init` 是否被阻塞 | `python test\v1727\leftover_probe.py`；源码路线，端口 19031/19032；`--init` 例不监听；日志 `leftover.log` |
| `test/v1728/pkg_verify.py` | 临时 | **v1.7.28 包真伪 + 文档口径（14 项）**：exe 属性版本=1.7.28.0、`VERSION`、目录 vs zip 逐文件 SHA256、zip 前缀 `wkfile-v1.7.28/`、文件数=14、LAYOUT、示例配置 50/50 键集与类型、敏感文件、LICENSE=1072B、CRCRLF=0；另查 `使用说明.txt` 是否含 1.7.28 条目、README 的 patch 0 语义、4551 段与 GUI 版本区间 | `python test\v1728\pkg_verify.py`；只读包与 zip；日志 `test\v1728\pkg.log` |
| `test/v1728/exe_inplace.py` | 临时 | **v1.7.28 exe 真机 wire**：包目录内**原地**执行（不复制、不挪走），端口 **19101**；验 `/health`=1.7.28、`initialize.serverInfo.version`、`tools/list`=36、`/api/patch`（200 且无 `original`）、`/mcp win_patch`（`isError=false`）、`/api/probe.security`；finally kill + 清 `wkfile.pid` + 还原目录 + 端口释放 | `python test\v1728\exe_inplace.py`；**跑前确认包目录无在跑实例**；只允许原地跑；日志 `exe_inplace.log` |
| `test/v1728/exe_hotload_check.py` | 临时 | **证明 1.7.28 逻辑确实进了 exe 二进制**（不只是版本号）：对包内 exe 以 `exec_strict_chain=true` 启动，运行中把配置写成 `0`，再发 `echo hi && whoami`，检查 `/api/exec` 200 且 `/api/health.hotload_warnings` 有 count/keys；finally kill + 还原目录 | `python test\v1728\exe_hotload_check.py`；包内原地，端口 **19115**；日志 `exe_hotload.log` |
| `test/v1728/hotload_matrix.py` | 临时 | **v1.7.28 头号验收（确定性矩阵，8 种写法）**：启动 `exec_enabled=true`+`exec_strict_chain=true`，白名单 `["echo","dir"]`，运行中依次写 `0` / `1` / `"0"` / `"false"` / `false` / `null` / `"garbage"` / `2` 并发 `echo hi && whoami`；逐例打印是否被拒、实际生效值、run log 增量、`health.hotload_warnings`、`probe.issues`/`config.hotload_warnings`；另测 `exec_allowed_commands:123` | `python test\v1728\hotload_matrix.py`；源码路线，端口 **19102**；写后 `fsync`+真实 mtime+间隔；日志 `hotload_matrix.log` |
| `test\v1728/patch_zero_probe.py` | 临时 | **两套 0 语义**：`patch_max_total_bytes=0` 与 `patch_max_rollback_bytes=0` 同写后用 1KB 小补丁（200）、9MB 大补丁（预期 400「超过预算 8.0 MB」）、2×4MB 边界；并读 `/api/probe.issues` 看哪一项报 issue | `python test\v1728\patch_zero_probe.py`；源码路线，端口 **19103**；日志 `patch_zero.log` |
| `test\v1728/leftover_probe.py` | 临时 | v1.7.27 遗留逐条判死活：A `type .\deploy.ps1`/`copy .\payload.bat x`（仍 400）与裸文件名（200）；B `tls_cert:null`（静默关 TLS、无告警）；C `ip_allowlist:[""]`（`/api/*` 全 403、`/health` 200、无告警）；D 脏安全开关下 `--init`（exit 5）；E env `WKFILE_READ_ONLY=yes`（静默 True、无 env_warnings）；F `port:"19110"`（exit 5 拒启） | `python test\v1728\leftover_probe.py`；源码路线，随机端口；`--init`/拒启例不监听；日志 `leftover.log` |
| `build-exe.ps1` | 长期 | 重建 `dist\wkfile.exe`（PyInstaller onefile + windowed，版本号写死在脚本 `$AppVer`） | `powershell -ExecutionPolicy Bypass -File .\build-exe.ps1`；只产 dist，不产发布包；发版另走 `build-release.py` |

## 4. 发布包自带工具（长期）

| 脚本 | 状态 | 用途 | 运行方式 / 注意 |
|---|---|---|---|
| `tools/upload.py` | 长期 | 直接 POST 原始字节到 `/api/upload`，用于大文件/二进制上传 | `python tools\upload.py --url http://host:8788 --token <token> --from 本地文件 --to 目标路径`；`--no-backup` 可跳过备份 |
| `tools/mcp-call-test.py` | 长期 | 用 JSON-RPC 调一次 MCP 工具，验证连通与鉴权 | 位置参数：`python tools\mcp-call-test.py <base_url> <token> <tool_name> [args_json]` |
| `tools/mcp-public-test.py` | 长期 | 对公网/HTTPS MCP 端点做一轮工具调用测试 | 位置参数：`<base_url> [token]`；默认样例端口 10087 是历史值，按实际改 |
| `tools/probe-public.py` | 长期 | 公网可达性探测（ping + 常用端口 TCP 连通） | 位置参数：`<host>` |

## 5. 构建辅助与历史临时脚本（`.build/`）

> `.build/` 整个目录被 git 忽略，内容是构建缓存 + 一次性排障脚本。**默认不要再运行**；需要用时先读代码确认副作用与凭据。

| 脚本/文件 | 状态 | 用途 | 注意 |
|---|---|---|---|
| `.build/backup_capability_check.py` | 临时 | 抽查备份/列表/回滚能力 | 会创建测试数据目录 |
| `.build/exec_capability_check.py` | 临时 | 抽查 `win_exec` 白名单、超时、输出落盘 | 会真的执行命令 |
| `.build/gh_release.py` | 临时 | 调 GitHub API 创建 Release 并上传 zip | 需要凭据；`gh_askpass.sh` 含明文 PAT，**先吊销** |
| `.build/gh_askpass.sh` | 临时 | 给 git/gh 提供凭据的 askpass 脚本 | **含明文 GitHub PAT，禁止外发** |
| `.build/publish_sanitize.py` | 临时 | 发布前把文档里的内网地址/账号替换成占位符 | **会原地改写工作区文件**，运行前先备份/确认 |
| `.build/sqlsrv_exe_probe.py` | 临时 | 用打包 exe 探测 SQL Server 连接/TDS 行为 | **含真实 IP + sa 明文口令** |
| `.build/sqlsrv_live_test.py` | 临时 | 对真实 SQL Server 做在线查询验证 | 同上 |
| `.build/sqlsrv_offline_query.py` | 临时 | 离线回放/构造 TDS 查询包 | 无真实连接 |
| `.build/sqlsrv_offline_repro.py` | 临时 | 离线复现 TDS 解析问题 | 无真实连接 |
| `.build/sqlsrv_tap.py` | 临时 | 抓取/打印 TDS 交互字节流 | 可能含凭据 |
| `.build/sqlsrv_tap2.py` | 临时 | TDS 抓包第二版 | **含真实凭据** |
| `.build/sqlsrv_variants.py` | 临时 | 对比多种 TDS 登录/加密参数组合 | **含真实凭据** |
| `.build/sqlsrv_variants2.py` | 临时 | 上述的扩展版 | **含真实凭据** |
| `.build/wechat_draft.py` | 临时 | 生成/提交微信公众号文章草稿（内容营销） | 读环境变量里的微信凭据；与产品运行无关 |
| `.build/wechat_check.py` | 临时 | 检查微信草稿/素材状态 | 同上 |
| `.build/wechat_post_wkfile.md` | 临时 | 公众号文章正文 | 文案素材 |
| `.build/wechat_preview.html` / `.build/wechat_preview.png` | 临时 | 公众号文章预览页/截图 | 素材 |
| `.build/Flat_modern_tech_illustration__2026-10-18T03-44-02.png` | 临时 | 文章封面图 | 素材 |
| `.build/commit-msg-v1710.txt` / `v178.txt` / `v179.txt` | 临时 | 各版本提交信息草稿 | 可删 |
| `.build/version_server.txt` | 构建产物 | `build-exe.ps1` 每次构建时用脚本里的 `$verText` 重新生成，作为 PyInstaller `--version-file`；**v1.7.28 实测包内 exe 的 FileVersion/ProductVersion = 1.7.28.0** | 不要手改（下次构建会被覆盖），要改版本请改 `build-exe.ps1` |
| `.build/version_mcp.txt` | 历史 | 旧 `WinFileMcp.exe` 的版本资源（filevers 1.5.0.0） | 已不参与当前构建 |
| `.build/build.log` | 历史 | 旧构建日志 | 排障参考 |
| `.build/linger_wkfile.log` | 临时 | PyInstaller onefile 父进程常驻问题的排查记录 | 解释"为什么 exe 退了进程还在" |
| `.build/bootloader_probe.out` | 临时 | bootloader 探针输出（空文件） | 可删 |
| `.build/probes/chain_merge_probe.py` | 临时 | 证书链合并验证 | 读 `leaf_from_server.pem` / `wotrus-inter.pem` |
| `.build/probes/chain_verify_probe.js` | 临时 | Node 侧验证证书链 | 需要 node |
| `.build/probes/db_agent_probe.py` | 临时 | 通过 agent 走数据库工具的探针 | 会连测试库 |
| `.build/probes/db_module_probe.py` | 临时 | 直接调用 `wkfile_mysql/redis/sqlsrv` 的探针 | 会连测试库 |
| `.build/probes/db_timeout_probe.py` | 临时 | 验证连接/查询超时 | 会连测试库 |
| `.build/probes/mysql_stdlib_probe.py` | 临时 | MySQL 协议逐包调试（握手/认证/查询） | 需要测试 MySQL |
| `.build/probes/exe_exit_probe.py` | 临时 | 验证 exe 启动/退出码 | 调 `dist\wkfile.exe` |
| `.build/probes/exe_linger_probe.py` | 临时 | 验证 onefile 残留进程 | 同上 |
| `.build/probes/exe_selftest_probe.py` | 临时 | 验证 exe 内 `--check-gui` | 同上 |
| `.build/probes/gui_close_probe.py` | 临时 | 验证关窗后服务是否退出 | 需要桌面会话 |
| `.build/probes/gui_db_scroll_probe.py` | 临时 | GUI 数据库页滚动布局验证 | 需要桌面会话 |
| `.build/probes/gui_footer_probe.py` | 临时 | GUI 底部状态栏布局验证 | 需要桌面会话 |
| `.build/probes/gui_layout_probe.py` | 临时 | GUI 整体布局/缩放验证 | 需要桌面会话 |
| `.build/probes/gui_selfcert_probe.py` | 临时 | GUI 自签证书流程验证 | 会写测试证书 |
| `.build/probes/node_roots_probe.js` | 临时 | Node 侧 roots/路径行为验证 | 需要 node |
| `.build/probes/tls_client_probe.js` | 临时 | Node 侧 TLS 客户端验证 | 需要 node |
| `.build/probes/leaf_from_server.pem` / `wotrus-inter.pem` | 临时 | 证书链测试样本 | 不是私钥 |
| `.build/probes/pi_probe/` | 临时 | PyInstaller 最小复现工程（hello/pair + build/dist） | 可整目录删 |

### `.build/` 下的目录（不是脚本，登记备查）

| 目录 | 状态 | 内容/用途 |
|---|---|---|
| `.build/venv/` | 长期（构建依赖） | `build-exe.ps1` 使用的 PyInstaller 虚拟环境，**不要删**（删了要重建） |
| `.build/spec/` | 历史 | `WinFileMcp.spec` / `WinFileServer.spec` / `wkfile.spec`，旧打包 spec |
| `.build/work-server/`、`.build/work-mcp/` | 历史 | 旧 PyInstaller 工作目录 |
| `.build/pack/` | 历史 | 旧 `WinFile-Server-v1.3.1/v1.4.0/v1.5.0` 发布包 |
| `.build/frozen-sim/` | 临时 | 模拟 frozen 目录结构（wkfile / wkfile-server） |
| `.build/smoke-wkfile/` | 临时 | 放一份 exe 做冒烟 |
| `.build/localrun/`、`.build/mcpexetest/`、`.build/sqlsrv-exe-run/`、`.build/tls-run/` | 临时 | 各测试运行时的数据目录（share/backups/trash/logs/config），含测试 Token/口令，**勿外发** |

## 6. 已废弃（`deprecated/`，仅存档，不要运行）

| 脚本 | 状态 | 原用途 | 废弃原因 |
|---|---|---|---|
| `deprecated/client/winfile_mcp.py` | 废弃 | 本地 stdio ↔ 远程 HTTP 的 MCP 桥接（旧接入方式） | 现在客户端直连 `/mcp`；仅 `selftest.py` 第 2 部分还在用 |
| `deprecated/client/mcp.config.json` | 废弃 | 上述桥接的配置样例 | 同上 |
| `deprecated/隧道启动.bat` | 废弃 | 用 `ssh -L` 把服务器端口转发到本地 | 能连 22 就能直连 8788，隧道多此一举；端口号也已过时 |
| `deprecated/enable-openssh-server.ps1` | 废弃 | 在服务器上装/开 OpenSSH Server 配合隧道 | 同上 |
| `deprecated/README.txt` | 废弃 | 归档说明 | 保留即可 |

## 7. 其它/无关文件

| 文件 | 状态 | 说明 |
|---|---|---|
| `通过vLLM部署Qwen2.5-1.5B-Instruct.ipynb` | 无关 | 与本项目无关的笔记本，未被 git 跟踪，建议移出仓库根目录 |
| `wkfile-v1.x.y/`、`wkfile-v1.x.y.zip` | 发布产物 | 十余个历史版本（最新 **v1.7.28**，14 个文件 / 14 个 zip 条目），gitignore；建议只留最新一版到 `release/` 或外部存储 |
| `dist/wkfile.exe` | 构建产物 | 当前 exe；`mcp_exe_test.py` 依赖它 |
| `docs/竞品学习与改造清单.md` | 历史文档 | v1.5.0~v1.6.4 的设计/改造记录，被 `.gitignore` 有意排除（含内部信息），只在本地 |
| `wkxm.md` / `jiaoben.md` | 长期 | 本文件与项目指引，必须随代码更新 |

---

## 8. 新增脚本登记模板

```
| `路径/脚本名` | 长期/临时/历史 | 一句话用途 | 运行方式；副作用/前置条件/凭据 |
```

登记时至少写清：**谁调用、做什么、会不会改文件/连库/执行命令、需要什么凭据、产物在哪**。

---

## 9. 协作通道（collab/）

当前 **没有新增可执行脚本**：`collab/` 是纯 Markdown 文件信箱（协议、台账、消息、归档），不需要登记。
以后若为通道写轮询/投递脚本（如 `collab/tools/*.py`），必须在本节补登，并写清：

- 谁运行（reviewer / workbuddy / 人）；
- 读写哪个目录、是否会移动/覆盖消息；
- 是否联网、是否需要凭据；
- 如何按 `protocol.md` 的 `id` 去重。
