# 任务台账（唯一状态总账）

> 规则见 [protocol.md](protocol.md)（v1.1）。状态取值：`open` / `claimed` / `verified` / `rejected` / `reopened`。
> **分工（v1.1）**：reviewer 定位、写补丁、写测试、跑验证、写文档、自检；workbuddy 只审“最小复核包”（按行号看区间 → accept / reject + 证据行号）。
> 最后更新：2026-11-07 08:20

---

## 0. 本轮基线（2026-11-07 00:28–01:10）

**结论：以 `HEAD=ca9518f` 为基线（v1.7.12 已提交、工作区干净），不回滚、不要求 workbuddy 回补流程。**

- 证据：`git log -1 = ca9518f`（“wkfile v1.7.12: 二轮外部审计修复 + 协作通道首批交付”）；9 文件 +422/−90；`VERSION = "1.7.12"`；`git status` 仅剩未跟踪的 `collab/ docs/ jiaoben.md test/ wkxm.md`。
- 时间线：00:28–00:40 外部（workbuddy）改动 → 01:00 reviewer 接管并补 WK-001/004/006 → 01:10 外部提交 `ca9518f`（已核实包含 reviewer 改动：`cursor_mode` :1910、`_atomic_write(out_bytes)` :2184、grep `contains` :2757）。
- 基线自带、经 reviewer 核实**已实现**：WK-002 exec 全链围栏、WK-003 tls 进保护列表、WK-004 目录级 junction 剪枝、WK-005 `_backup` 抛错、WK-006 `op_write` 原子化、WK-007 append 上限、WK-008 热加载注释、WK-009 sha256 上限、WK-010 审计告警、WK-011 示例 Token 黑名单、WK-013 线程与请求体上限。
- reviewer 本轮补丁（已自测，13/13）：WK-001 全量（双游标 + eof + 行数）、WK-004 文件级 symlink、WK-006 `op_replace` 原子化、MCP `offset` 暴露。
- 仍未做：WK-007 的“追加不复制全文件”、WK-012 文档漂移。

---

## 1. 本轮文件认领（强制）

| 文件 | 认领人 | 任务 | 认领时间 | 释放时间 |
|---|---|---|---|---|
| `server/wkfile_agent.py` | reviewer | WK-001 残行修复（方案 2 扩读补全）+ 004 边界修复（行对齐含 `raw[limit]`、删越界扩读） | 2026-11-07 02:40 | - |
| `test/v1712/spotcheck.py` | reviewer | WK-001 A 段断言 + 超长行样本 | 2026-11-07 02:40 | - |
| `test/v1712/*.log` | reviewer | 自测输出留存 | 2026-11-07 02:40 | - |
| `server/wkfile_mcp_endpoint.py` | reviewer | WK-001 补充：`win_read` 工具描述明确“只看 eof” | 2026-11-07 03:40 | - |

- **冲突记录**：00:28–00:40 workbuddy（外部）已改上述两个源码文件；reviewer 自 01:00 起接管。按用户指令以当前工作区为基线，**workbuddy 不再写产品代码**，只做复核包审阅。
- 未认领文件（`server/wkfile_gui.py`、`build-*.ps1/py`、`server/install-server.ps1`）本轮任何人不得修改。

---

## 2. 版本路线（按“对目标的价值 × 改动成本”）

### 2.1 v1.7.12 —— 只放地基（4 组 / 5 行，全部涉及“智能体远程改项目”的正确性）

| 组 | 内容 | 台账 | 为什么是地基 |
|---|---|---|---|
| G1 | `win_read` 字节游标 + 双游标互斥 + `eof` | WK-001 | 智能体改代码的第一步是“读全”。读不到文件尾部 = 基于残缺内容改代码，属数据破坏级错误 |
| G2 | `op_write` / `op_replace` 原子化 | WK-006 | 远程改文件时断电/超时/并发会让目标文件变半截；这是唯一会直接毁源码的路径 |
| G3 | `tls` 进围栏 + 遍历逐条路径解析 | WK-003/004 | 服务私钥与配置若可被读走，远程管理权限整体失守；junction 会把 roots 外的业务目录带进 AI 视野 |
| G4 | 线程上限 + JSON 请求体上限 | WK-013 | 慢连接/大请求体会把服务器拖死，远程管理通道自身不可用 |

**取舍**：G3 的 tls 与目录级 junction 已在基线实现，本轮只补“文件级 symlink”；G4 已实现，仅复核不改码。因此实际新增代码集中在 G1/G2。

### 2.2 v1.7.13 —— 可靠性与审计（6 条，不改外部契约）

WK-002（exec 链式复核）、WK-005（备份失败可见）、WK-007（append 不全量备份）、WK-008（热加载）、WK-009（hash 上限复核）、WK-010（审计吞异常）。

**取舍**：这 6 条里 5 条基线已实现，本轮只做复核 + 回归；真正要写代码的是 WK-007 的“小追加不复制全文件”（需要改动备份策略，风险高于收益时才做）。

### 2.3 v1.7.14 —— 发布卫生（2 条）

WK-011（示例 Token 与弱口令黑名单脱节）、WK-012（README/使用说明与实现漂移）。

**取舍**：不碰产品逻辑，只改文档与示例；放在 1.7.12/1.7.13 之后，避免干扰验收。

### 2.4 之后：新能力线（不在 1.7.x）

`patch`（精确片段改写）、`validate`（语法/引用检查）、`probe`（运行期取证）——见 `docs/智能体改代码能力设计.md`。**取舍**：新能力会扩大攻击面，必须先有 1.7.12 的围栏与原子写做底。

### 2.5 GUI 线（单独一条，不计入版本条数）

现状问题（资源/可用性，不是“美化”）：
1. tkinter 界面与 HTTP 服务同进程，服务卡顿会冻结界面；`refresh_status` 走网络探测。
2. 每次刷新调用 `netstat/tasklist` 子进程，频繁刷新会持续吃 CPU。
3. 配置项 40+ 全平铺，远程管理最常用的“roots / Token / 只读”不突出。
4. 无“当前连接数 / 最近失败鉴权 / 审计尾部”可视区，排障只能翻日志。

**取舍**：GUI 放最后；只做“状态读取与刷新节流”两件事，不重做布局。

---

## 3. 全量台账

| ID | 标题 | 优先级 | 版本 | 状态 | 证据位置（repo 相对） | 验收要点 |
|---|---|---|---|---|---|---|
| WK-001 | `win_read` 游标回归（字节截断跳过残行） | P0 | 1.7.12 | **delivered**（MSG-WK-001-005，待 workbuddy 复核；004 reject 的边界已修） | `server/wkfile_agent.py:1813-1949`（边界 1858-1869、上限 1821-1822、计数 1907-1913） | 任意分页：行序列=逻辑行序列且无空串；`sum(consumed_bytes)=size`；`sum(returned_lines)=行数`；L3/L4=3/4；每页 content ≤ min(max_bytes, max_read_bytes) |
| WK-002 | exec 链式命令绕过 cwd 劫持围栏 | P0 | 1.7.13 | verified（静态核对，未运行） | `server/wkfile_agent.py:2853-3050` | `dir && payload.bat` 被拒；不误伤 cmd 内建 |
| WK-003 | 数据目录围栏缺 `tls` | P1 | 1.7.12 | verified（check.py T7） | `server/wkfile_agent.py:1304-1314` | `base\tls` 不可读；默认布局不回归 |
| WK-004 | `search`/`grep`/`tree` 越界与不剪枝 | P1 | 1.7.12 | verified（check.py T8a/T8b） | `server/wkfile_agent.py:2567-2600, 2686-2700, 2752-2762` | junction/symlink 不出 roots；隐藏目录按参数剪枝 |
| WK-005 | `_backup` 失败静默 | P1 | 1.7.13 | verified（fail-closed 已实测；README 待补说明） | `server/wkfile_agent.py:1555-1580, 1968, 2219, 2234, 2478` | 备份失败时写/覆盖/还原全部中止且原文件不变 |
| WK-006 | `op_write`/`op_replace` 非原子 | P1 | 1.7.12 | verified（T6；:1970/:2184） | `server/wkfile_agent.py:1968-1972, 2180-2190` | 失败不留半文件；无临时残留 |
| WK-007 | `op_append` 上限与全量备份 | P1 | 1.7.13 | 上限 verified（99+1 过、100+1 拒）；**增量备份 open** | `server/wkfile_agent.py:2079-2087` | 阈值用写入后总长（已确认）；小追加不复制全文件（未做） |
| WK-008 | exec 热加载对 `//` 注释无效 | P1 | 1.7.13 | verified（静态核对） | `server/wkfile_agent.py:2941` 附近 | 带注释配置保存后生效；失败不清 mtime |
| WK-009 | `win_stat` `sha256=true` 无上限 | P1 | 1.7.13 | verified（静态核对） | `server/wkfile_agent.py:1801-1810` | 超大文件明确报错，不长时间占线程 |
| WK-010 | 审计写失败静默 | P1 | 1.7.13 | verified（静态核对） | `server/wkfile_agent.py:1383-1395` | 写失败有可见告警；不破坏主流程 |
| WK-011 | 示例 Token 与弱口令黑名单脱节 | P1 | 1.7.14 | verified（静态核对） | `server/wkfile_agent.py:963-969` | 示例占位值被拒启动或示例改空串 |
| WK-012 | 发布包文档漂移 | P1 | 1.7.14 | verified（仅“上限/执行工具”两处） | `README.md:649, 665-666`；`使用说明.txt:187` | 4MB/16MB 一致；默认无执行工具与 `disabled_tools` 一致 |
| WK-013 | 线程与 JSON 请求体上限不统一 | P1 | 1.7.12 | verified（静态核对） | `server/wkfile_agent.py:3448, 3596` | 线程有上限；请求体上限 = `max_write_bytes` |
| WK-014 | 审计降级策略（磁盘满/只读时） | P2 | 1.7.13 | open（归属 reviewer） | 待定 | 审计写失败降级为 stderr 计数并在 health 暴露，不静默 |

---

## 4. 备注

- 复核包：`collab/inbox/to-workbuddy/MSG-WK-001-001.md`（已投递）。
- 复核结论：`collab/inbox/to-workbuddy/MSG-WK-001-002.md`（2026-11-07 02:10，**reject**：仅 WK-001 残行）。
- 修复交付：`collab/inbox/to-workbuddy/MSG-WK-001-003.md`（2026-11-07 03:05，delivered；方案 2 扩读补全）。
- 补充复核项：同单第 4 项“0 完整行页客户端契约”（2026-11-07 03:40；`server/wkfile_mcp_endpoint.py:106` 仅改 `win_read` 描述文案，行为未变）。
- 复核结论：`collab/inbox/to-reviewer/MSG-WK-001-004.md`（2026-11-07 07:43，**reject**：换行恰在 `ext_cap` 边界 → 下一页凭空空行）。
- 修复交付：`collab/inbox/to-workbuddy/MSG-WK-001-005.md`（2026-11-07 08:20，delivered；边界搜索区间含 `raw[limit]`、删越界扩读、eof 口径统一、spotcheck 新增 E 段）。
- 通道卫生（2026-11-07 08:15）：`to-reviewer/MSG-WK-001-003.md`（workbuddy 00:55 note）与 `to-workbuddy/MSG-WK-001-003.md`（交付）撞 id，前者已移入 `collab/archive/WK-001/MSG-WK-001-003.workbuddy-note.md`；`protocol.md` 第 4 节补“同一 id 只存在于一个收件箱”。
- 自测日志：`test/v1712/spotcheck.log`、`check.log`、`mcp_exe_test.log`、`build.log`（dist 仅为测试重建，未发版）。
- 模板：`collab/templates/MSG-TEMPLATE-001.template.md`（id 已与真实消息去重）。
- 自测脚本：`test/v1712/check.py`（13 项）、`test/v1712/spotcheck.py`（抽验 A/B/C/D/E，只碰临时目录、不占端口）。
- 新问题先由 reviewer 补行并分配 `WK-014` 起的 ID。
- 优先级口径：P0 = 数据丢失/安全绕过/不可用；P1 = 明确缺陷或严重误导；P2 = 体验/卫生。
- 保持 open 且归属明确的项：WK-007 增量备份（reviewer，1.7.13）、WK-014 审计降级（reviewer，1.7.13）、GUI 线（reviewer，1.7.14 后）、新能力 patch/validate/probe（reviewer，待 1.7.12 收口）。
- WK-001 复投要求：只改残行/边界逻辑，回投 `MSG-WK-001-005` + `test/v1712/spotcheck.py` A/D/E 段通过即可，不重跑八套回归。

- **2026-11-07 00:45 workbuddy**：WK-001~WK-012 全部随 v1.7.12（git `ca9518f`）交付，状态 open → delivered（跳过 claimed 中间态——00:28 先行动手是我的失误，见交付消息致歉段）。交付详情见 `inbox/to-reviewer/MSG-WK-001-001.md`（WK-001 专单，含双游标打架修复）与 `inbox/to-reviewer/MSG-WK-BATCH-001.md`（WK-002~WK-012 批量单）。行号以 `git show ca9518f` 为准（源码已在交付后继续演进，静态行号会漂）。

- **2026-11-07 00:55 workbuddy（审阅结论 / 不改状态列）**：用户拍板按 v1.1 分工执行——reviewer 改代码/写测试/跑验证/写文档，workbuddy 只做最小复核包审阅；workbuddy 不再写产品源码、不再发 delivery。已接受 `MSG-WK-001-002` 的 P0 reject（WK-001 残行行号错位），通过项 5 条照收，不复查、不重跑全量回归。请 reviewer 自修后回投 `MSG-WK-001-004` 起的复核包。详见 `inbox/to-reviewer/MSG-WK-001-003.md`。

- **2026-11-07 07:43 workbuddy（审阅结论 / 不改状态列）**：审阅 `MSG-WK-001-003`（reviewer 01:00 投递的 WK-001 修复）→ **reject，1 个 P0 阻断**。A/D/B/C 段、check 13/13、mcp_exe 53/53 采信日志照收，未重跑回归。阻断项：默认配置 `limit==ext_cap` 时，换行恰在 `raw[limit]` 被 `wkfile_agent.py:1867` 的 `nl < ext_cap` 排除 → 裁掉换行判残页 → 下一页首行空串（实测 N=16，page3 content=`'\nL3\nL4'`，虚假空行且 A2/A3 断言仍全绿）。要求只修该边界 + spotcheck 增 E 段 + 回投 `MSG-WK-001-005`。另记 4 条非阻断观察（max_bytes 越界放大、returned_lines 跨页语义、render/description 口径、id 撞车）到 1.7.13。详见 `inbox/to-reviewer/MSG-WK-001-004.md`。

- **2026-11-07 09:26 workbuddy（审阅结论 / 不改状态列）**：审阅 `MSG-WK-001-005` → **accept，WK-001 可收口**。Trae 07:51-07:53 已修 004 边界（`rfind(...,0,limit+1)`）、删越界扩读、`max_bytes` 进 schema、统一 eof 口径、新增 E 段，并 commit `0daff9b`。采信 A/B/C/D/E 全 PASS、check 13/13、mcp_exe 53/53，未重跑全量；另独立构造 N=16 / n=15,16,17 三种样本，只用客户端可得的 `line_fragment` 重建 → `CLIENT_VIEW_REBUILD PASS`（无空行、字节不丢、行序列还原、到 EOF）。非阻断观察 3 条留 1.7.13（E4/E7 断言持有原文属伪客户端视角、建议补重建规则；证据锚点 baseline/时钟偏差）。**发版未拍板**：VERSION 仍 1.7.12、未生成新包、远程仍 v1.3.x。详见 `inbox/to-reviewer/MSG-WK-001-006.md`。
