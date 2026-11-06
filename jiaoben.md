# jiaoben.md —— 脚本登记簿

> 项目规矩：**新增/修改任何脚本，都要在这里登记一行**；删除脚本时保留记录并标注"已删除"。
> 状态说明：`长期` = 产品/构建/测试链路在用，必须维护；`临时` = 一次性排障/验证，可随时删；
> `历史` = 旧版本遗留，仅存档；`废弃` = 已归档不再使用；`无关` = 不属于本项目。
> 本表核对日期：2026-11-07（v1.7.11 发布包复核；当轮只做分析、改文档与临时端口实测）。

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
| `server/wkfile_agent.py` | 长期 | **主程序**：配置、路径围栏、备份/回收站、全部 `op_*`、HTTP API、MCP 挂载、SSE、CLI | `--run` 起服务；`--gen-token` / `--gen-readonly-token` / `--renew-token` / `--init` / `--gen-selfcert` / `--allow-unsafe` / `--check-gui`；详见 `wkxm.md` |
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
| `tests/features_e2e.py` | 长期 | **主回归**（200+ 项）：围栏、备份、回收站、编码、只读、exec、TLS、数据库错误路径 | `python tests\features_e2e.py`；固定端口；`mkdtemp`；失败保留现场 |
| `tests/mcp_e2e.py` | 长期 | MCP 协议回归：initialize / tools/list / tools/call / 批量 / SSE / 错误 | 固定端口；脚本自己起测试服务 |
| `tests/local_e2e.py` | 长期 | 本机直连小回归 | 固定端口 |
| `tests/mcp_exe_test.py` | 长期 | 对 `dist\wkfile.exe` 的黑盒测试（46 项左右） | **必须先 `build-exe.ps1`**，否则测到的是旧 exe |
| `tests/tls_e2e.py` | 长期 | HTTPS / 证书链 / 自签证书回归 | 需要证书环境；固定端口 |
| `tests/sqlsrv_proto_test.py` | 长期 | TDS 协议离线用例（不需要真实 SQL Server） | 可直接跑 |
| `.build/features_e2e.py`、`.build/local_e2e.py`、`.build/mcp_e2e.py`、`.build/mcp_exe_test.py`、`.build/tls_e2e.py` | 历史 | 早期测试副本，已与 `tests/` 分叉（`mcp_exe_test.py` 差 12 KB） | **勿运行、勿修改；以 `tests/` 为准**，建议后续删除 |

### 3.1 临时发布包复核脚本（`test/`，2026-11-07）

| 脚本 | 状态 | 用途 | 运行方式 / 注意 |
|---|---|---|---|
| `test/pkgcheck/gen.py` | 临时 | 生成复核用样本：1000 行 `lines.txt`、4,403,100 字节 `big.txt`（第 4001 行超长，尾部标记在 4MB 之后） | `python test\pkgcheck\gen.py`；只写 `test/pkgcheck/share` |
| `test/pkgcheck/client.py` | 临时 | 对临时 exe 实例发 MCP `initialize/tools/list/tools/call`，验证 `win_read` 默认行数、游标、`win_replace` 大文件、请求体上限 | 先按 wkxm 第 16.6 节起临时实例（端口 18911），再 `python test\pkgcheck\client.py` |
| `test/pkgcheck2/`、`test/pkgcheck3/` | 临时 | 示例配置原样启动验证 / exec 链式绕过验证的 exe + 配置夹具（**各含 12.6MB exe 副本**） | 只用于复现，复现完可整目录删除；`pkgcheck3` 内 `share\payload.bat` 是链式绕过样本 |
| `test/v1712/check.py` | 临时 | v1.7.12 定向自测：WK-001 游标（续读到 EOF、双游标、max_lines 分页、eof）、WK-006 `op_replace` 原子写、WK-003 tls 围栏、WK-004 junction/symlink | `python test\v1712\check.py`；只建临时目录、不占端口、不起服务；当前 13 项 ALL PASS |
| `test/v1712/spotcheck.py` | 临时 | reviewer 抽验：残行（A 段断言 sum(consumed)=size、sum(returned_lines)=4、L3/L4=3/4）、6MB 单行超 max_read_bytes（D 段）、换行压在窗口边界 limit-1/limit/limit+1 四条不变式（E 段）、`_backup` 失败影响面（B）、`op_append` 边界（C） | `python test\v1712\spotcheck.py`；只建临时目录、monkeypatch `copy2` 模拟磁盘满；输出重定向到 `test\v1712/spotcheck.log`；E 段是 v1.7.13「断言全绿但契约错误」的回归锁 |
| `test/v1712/*.log` | 临时 | 自测输出留存：`spotcheck.log`、`check.log`、`mcp_exe_test.log`、`build.log` | 仅存档，可随时删；`mcp_exe_test.log` 需先重建 `dist` |
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
| `.build/version_server.txt` | 构建产物 | `build-exe.ps1` 每次构建时用脚本里的 `$verText` 重新生成，作为 PyInstaller `--version-file`；其中 `filevers` 仍是 1.6.0.0 | 不要手改（下次构建会被覆盖），要改版本请改 `build-exe.ps1` |
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
| `wkfile-v1.x.y/`、`wkfile-v1.x.y.zip` | 发布产物 | 12 个历史版本，gitignore，约占 290 MB；建议只留最新一版到 `release/` 或外部存储 |
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
