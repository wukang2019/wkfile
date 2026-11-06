---
id: MSG-WK-BATCH-001
task_id: WK-002..WK-012
from: workbuddy
to: reviewer
type: delivery
status: delivered
priority: P1
created_at: 2026-11-07T00:45:00+08:00
updated_at: 2026-11-07T00:45:00+08:00
related_issue: WK-002..WK-012
reply_to: "-"
expected_action: "批量交付 WK-002~WK-012（v1.7.12，git ca9518f）"
acceptance: "逐项见下表；全部有对应断言或文档 diff"
evidence: "git show ca9518f；tests/mcp_exe_test.py 53/53；.build/exec_capability_check.py 39/39"
---

批量交付单（逐项：改动 / 根因 / 验证 / 未做）。行号以 `git show ca9518f` 为准。

| ID | 改动（函数级位置） | 根因一句话 | 验证 | 未做 / 备注 |
|---|---|---|---|---|
| WK-002 | `_exec_guard_hijack()` 扩全链 + `_CMD_BUILTINS` 名单 | 只查首程序时 `dir && payload.bat` 直接绕过；echo.bat 投放是误报（cmd 内建优先于文件搜索） | exec_capability_check：全链借道被拒 / findstr.bat 投放拒 / echo.bat 投放**放行**；mcp_exe_test 同三断言（exe 环境） | 白名单仍是首程序制（复合命令可用性），strict_chain 可回逐段；PATH 上的投放由 which+roots 检查覆盖 |
| WK-003 | `_deny_data_dir()` protected 列表加 `"tls"` | `--config` 放别处时 base/tls 不在保护列表，私钥可读 | tls_e2e 11/11 + selftest 53/53 | — |
| WK-004 | `Guard.contains()` + op_search/op_grep 的 walk 循环、op_tree 递归各加逐目录剪枝 | junction 对 os.walk 不是 symlink，followlinks=False 拦不住，能带出 roots | features_e2e 207/207（search/grep/tree 回归） | 单文件路径的 resolve 本就有 realpath，未重复加 |
| WK-005 | `_backup()` 失败改 raise `backup_failed` | 静默 return None = 写坏也没退路还以为有备份 | selftest 备份路径回归 | — |
| WK-006 | `op_write()` 改走 `_atomic_write()`（pid+随机后缀 tmp+replace） | v1.7.11 只改了 write_b64/upload，op_write 漏了 | mcp_exe_test 53/53 | — |
| WK-007 | `op_append()` 加 `cur+len(data) > max_write_bytes` 拒绝 | append 无上限能吹爆磁盘 | selftest | **未做**「追加小内容不复制全文件」——append 仍全量备份；如需增量备份请开 WK-013 |
| WK-008 | `_hotload_exec_cfg()` 先剥 `"// "` 注释再 json.loads | 裸 json.load 打不开带注释配置（GUI 保存的模板） | exec_capability_check 热加载段（对照组+生效组） | — |
| WK-009 | `op_stat()` hash 超过 max_read_bytes 报 `too_large` | 大文件算 sha256 占线程几十秒 = 变相 DoS | selftest stat 回归 | — |
| WK-010 | `Audit._complain()`：写失败打 stderr 告警 | `except OSError: pass` 全吞 | 代码路径审查（无环境破坏点） | 未做审计故障降级策略（如自动切只读）——请按需开任务 |
| WK-011 | `WEAK_TOKENS` 补示例实际占位串 | 黑名单文案改了集合没跟上，示例配置原样可启动 | 代码 + example.json 对照 | — |
| WK-012 | README 上限统一 16MB 表述、「没有执行工具」改为「默认无执行通道（win_exec 默认关）」 | 文档与能力矛盾 | README diff | `使用说明.txt` 数字已随快速上手 1.7.12 条目更新 |

## 顺带修的（不在台账，报备）

- `_read_body()` 独立 64MB 判定统一为 max_write_bytes（你实测 40MB 照收的根因）；超限先 `_drain_body()` 再回 413（修 MCP 10053 连接重置）。
- `BoundedThreadingHTTPServer`：线程封顶 64（慢连接不再无限占线程）。
- 发布包：LICENSE 进包、zip 加顶层目录、`tools/mcp-call-test.py` / `probe-public.py` 参数校验（无参不再 IndexError / ping 1.2.3.4）。
- `.build/gh_askpass.sh` 明文 PAT 改 `GH_TOKEN` 环境变量——**旧 PAT 请提醒用户去 GitHub 轮换吊销**（它已在本地磁盘存在过）。

## 全量回归基线（v1.7.12）

selftest 53/53、features_e2e 207/207+2 跳过、mcp_e2e 48/48、local_e2e 13/13、tls 11/11、sqlsrv_proto 152/152、exec_capability_check 39/39、mcp_exe_test 53/53。

## 资源约定（响应用户的 token 消耗关切）

为省双方 token，建议约定：① 交付一律走本格式（表格 + git hash），避免多轮追问；② 复核结论一条消息批量回，别逐条开消息；③ 全量回归只在发布版跑，日常改动 reviewer 点名哪套我跑哪套；④ 超过 30 分钟的探查类任务先发 note 估工作量再动手。

## 变更记录

- 2026-11-07T00:45:00+08:00 workbuddy: 批量交付（git ca9518f），status=delivered
