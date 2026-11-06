# collab 协作通道协议（文件系统即通道）

- 协议版本：1.1（2026-11-07 分工调整：reviewer 多承担，workbuddy 只做低消耗复核）
- 适用范围：`wkfile` 项目；**reviewer 负责定位、写补丁、写测试、跑验证、写文档、打包前自检；workbuddy 只审阅“最小复核包”**。

> 任何 AI 进入本项目，先读本文件 + [ledger.md](ledger.md) + 两个 inbox 的 `README.md`，再动手。

---

## 0. 目的与边界

1. 两个 AI 不共享上下文，只通过 `collab/` 下的文件交换任务与结果。
2. 通道只承载**消息**，不承载代码；代码改动仍落在产品源码目录。
3. 搭通道阶段不改产品代码、不重建发布包。

---

## 1. 角色与写权限（v1.1 分工）

| 角色 | 负责 | 只允许写 | 只读 |
|---|---|---|---|
| **reviewer**（本会话，多承担） | 定位根因、写补丁、写测试、跑定向验证、写文档、打包前自检、发起复核包 | `collab/inbox/to-workbuddy/`、`collab/archive/`、`collab/ledger.md`、产品源码 | `collab/inbox/to-reviewer/` |
| **workbuddy**（低消耗复核） | 只审阅最小复核包：按行号看指定区间，回答三个必查问题，回 `accept` / `reject` | `collab/inbox/to-reviewer/`、`collab/archive/`、`collab/ledger.md`（仅审阅结论列） | `collab/inbox/to-workbuddy/` |

- **reviewer 不往 `to-reviewer/` 写**，workbuddy 不往 `to-workbuddy/` 写。
- 唯一例外：双方都可以把**已经终态**的消息移动到 `collab/archive/<TASK-ID>/`。
- **workbuddy 不读全仓、不跑完整测试**；只读复核包里列出的 `path:line` 区间，回复 ≤20 行。
- 任务范围以复核包里的 `change_summary` 为准；范围外的文件 reviewer 不得顺手改，workbuddy 不得要求扩围。
- 产品代码由 reviewer 修改；workbuddy 的职责是“低消耗挑错”，不是代写。

---

## 2. 传输模型

- **文件系统即通道**：一条消息 = 一个 `.md` 文件，放进收件人的 inbox 目录即视为投递。
- 无轮询服务、无数据库、无网络依赖；任何能读写文件的 AI/脚本都能接入。
- 投递必须是**原子写**：先写 `<id>.md.tmp`，再 rename 成 `<id>.md`，避免对方读到半截。
- 状态变更 = 原地修改 front matter 的 `status`，并在正文末尾追加一条带时间戳的变更记录。
- 若对方只能通过 API / CLI 交互：**只替换第 12 节的传输适配层**，消息格式、状态机、ledger 都不变。

---

## 3. 目录结构

```
collab/
├─ protocol.md                 # 本文件
├─ ledger.md                   # 任务台账（唯一状态总账）
├─ inbox/
│  ├─ to-workbuddy/            # reviewer 投给 workbuddy 的任务/验收/重开
│  │  └─ README.md
│  └─ to-reviewer/             # workbuddy 投给 reviewer 的交付/说明
│     └─ README.md
├─ archive/
│  ├─ README.md
│  └─ <TASK-ID>/               # 终态消息按任务归档
└─ templates/
   └─ MSG-TEMPLATE-001.template.md   # 最小复核包模板（非真实投递）
```

---

## 4. 文件命名

`MSG-<TASK-ID>-<NNN>.md`

| 部分 | 规则 | 示例 |
|---|---|---|
| `MSG` | 固定前缀 | `MSG` |
| `<TASK-ID>` | 与 ledger 一致，`WK-` + 3 位数字 | `WK-001` |
| `<NNN>` | 同一任务内 3 位递增序号，从 `001` 开始，只增不减 | `001` |

- 示例：`MSG-WK-001-001.md`
- 归档路径：`collab/archive/WK-001/MSG-WK-001-001.md`
- **文件名禁止**中文、空格、时间戳、版本号；时间信息只放 front matter。
- **同一 id 只存在于一个收件箱**：同一条消息不得两边各投一份；历史副本 / 重投副本一律移入 `archive/<TASK-ID>/`，文件名追加来源后缀（如 `MSG-WK-001-003.workbuddy-note.md`），保证归档不撞名、id 不复用。
- 任务号在 `ledger.md` 里分配；消息号由发送方在投递前查重后确定。

---

## 5. 消息格式与必填字段

文件 = YAML front matter + Markdown 正文。

```markdown
---
id: MSG-WK-001-001
task_id: WK-001
from: reviewer            # reviewer | workbuddy
to: workbuddy            # reviewer | workbuddy
type: request            # request | delivery | verify | reject | reopen | note
status: open             # open | claimed | delivered | verified | rejected | reopened
priority: P0             # P0 | P1 | P2
created_at: 2026-11-07T00:30:00+08:00
updated_at: 2026-11-07T00:30:00+08:00
related_issue: WK-001    # 关联 ledger 行；没有就写 "-"
reply_to: "-"            # 非 request 消息必填上游消息 id
expected_action: "一句话说明要对方做什么"
acceptance: "可判定真假的验收标准"
evidence: "repo 相对路径+行号，或证据文件路径"
---

## 背景

## 复现 / 证据

## 期望行为

## 验收标准

## 不许动的边界

## 变更记录

- 2026-11-07T00:30:00+08:00 reviewer: 创建，status=open
```

### 5.1 必填字段（缺一视为无效消息）

| 字段 | 说明 |
|---|---|
| `id` | 全局唯一，格式见第 4 节 |
| `task_id` | 对应 ledger 的 ID |
| `from` / `to` | 必须与所在 inbox 方向一致 |
| `type` | `request` / `delivery` / `verify` / `reject` / `reopen` / `note` |
| `status` | 当前状态，必须符合第 6 节 |
| `priority` | P0 / P1 / P2 |
| `created_at` | ISO-8601，带时区 |
| `related_issue` | 关联 issue / ledger 行，至少写 task_id |
| `expected_action` | 期望对方做的动作，一句话可执行 |
| `acceptance` | 验收标准，必须是可判定真假的陈述 |
| `evidence` | 证据位置（路径 + 行号 / 命令 / 输出文件） |

`delivery` / `verify` / `reject` / `reopen` 还必须填 `reply_to`。

---

## 6. 状态机

```
open ──claim──> claimed ──deliver──> delivered ──verify──> verified
                                        │
                                        └──reject──> rejected ──reopen──> open
```

| 迁移 | 谁能做 | 触发动作 | 额外要求 |
|---|---|---|---|
| `open → claimed` | workbuddy | 原地改 `status`，填 `owner` 与时间 | 必须先读完整消息 |
| `claimed → delivered` | workbuddy | 先写 `delivery` 消息到 `to-reviewer/` | **必须有交付说明**，见第 9 节 |
| `delivered → verified` | reviewer | 写 `verify` 消息到 `to-workbuddy/` | 验收标准逐条对照 |
| `delivered → rejected` | reviewer | 写 `reject` 消息，说明缺口 | 必须引用具体验收条 |
| `rejected → reopened` | reviewer | 写 `reopen` 消息（新序号） | ledger 备注保留上一轮证据 |

- **终态**：`verified`。`rejected` 不是终态，必须能重开。
- 状态只允许前进；不允许 `verified → open`（新问题另开 task）。
- `ledger.md` 是唯一状态总账；消息里的 `status` 必须与 ledger 一致，不一致以 ledger 为准并要求纠正。
- 同一任务同一时刻只允许一条 `open` 消息。

---

## 7. 幂等与去重

1. **id 全局唯一**；投递前必须检查四个位置是否已存在同 id：
   `to-workbuddy/`、`to-reviewer/`、`archive/`、`templates/`（模板不算投递，但 id 也不得复用）。
2. 已存在同 id：**不覆盖、不新建**；若是重复投递，只在 ledger 备注记 `duplicate: <id>`。
3. 接收方对同一 id **只处理一次**；重复 `delivery` 以 `created_at` 最早的一条为准。
4. 消息被 `claimed` 后，作者不得再修改正文；补充信息必须发新消息（`type: note`，`reply_to` 指向原 id）。
5. 状态迁移按实际发生时间记录；乱序到达（如先看到 delivery 再看到 claimed）不回退状态，在变更记录里注明。
6. 序号只增不减；即使消息被 reject，也不复用旧序号。

---

## 8. 时间与命名约定

- front matter 时间：`YYYY-MM-DDTHH:MM:SS+08:00`。
- ledger 时间：`YYYY-MM-DD HH:MM`。
- 文件名不含时间。
- 以写入方本机时钟为准；双方时间冲突时，以 `created_at` 较早者为准，并在备注记 `clock_skew`。
- 所有时间统一东八区；跨时区协作由写方自行换算。

---

## 9. 硬约束：改完必须回交付说明

workbuddy 把消息标成 `delivered` **之前**，必须新建一条 `type: delivery` 的消息放进 `to-reviewer/`，并包含：

1. **改了哪些文件**：repo 相对路径 + 行号范围。
2. **为什么这么改**：一句话说明根因与修法对应关系。
3. **怎么验证的**：完整命令 + 关键输出（不要只写"测过了"）。
4. **证据**：日志/截图/测试输出路径（建议放 `collab/evidence/<TASK-ID>/`）。
5. **没改什么 / 已知残留**：明确列出未覆盖的场景。
6. **契约影响**：新增字段、错误码、默认值是否变化；是否兼容旧客户端。

没有 delivery 消息的 `delivered` 一律视为**未交付**，reviewer 直接 `reject`。
reviewer 的 `verify` 也必须写清"逐条验收结论 + 复验命令"。

---

## 10. 标准流程（一轮）

1. reviewer 在 `ledger.md` 建行（ID、标题、优先级、证据位置），写 `request` 到 `to-workbuddy/`。
2. workbuddy 原地 `claimed`，按 `expected_action` 修改并自测。
3. workbuddy 写 `delivery` 到 `to-reviewer/`，原 `request` 改 `delivered` 并移入 `archive/<TASK-ID>/`。
4. reviewer 验收：写 `verify` 或 `reject` 到 `to-workbuddy/`，把 `delivery` 移入 archive。
5. 若 `reject`：reviewer 再写 `reopen`（新序号），ledger 回到 `open`，进入下一轮。

---

## 11. 并发与冲突

- 同一任务同时只有一条 `open` 消息；同一文件同一时间只允许一个任务修改。
- 跨任务依赖在 ledger 备注写 `blocked_by: WK-xxx`，不要并行改同一文件。
- 需要扩大修改范围时，先发 `note` 说明原因，等 reviewer 确认后再动。
- 发现对方已改过同一区域：停止修改，发 `note` 并附 `git diff` 证据。

---

## 12. 传输无关：换成 API / CLI 时替换什么

本协议只依赖四个原语：

| 原语 | 文件系统实现 |
|---|---|
| `deliver(role, message)` | 原子写 inbox 目录下的 `.md` |
| `list_inbox(role)` | 列 inbox 目录 |
| `read(id)` | 读对应文件 |
| `update_status(id, status, note)` | 改 front matter + 追加变更记录 |

若 workbuddy 只能通过 API/CLI 交互，只替换这一层适配器，并必须保证：

- 至少一次投递 + 接收方按 `id` 去重；
- 状态单调前进，变更可追溯；
- `created_at` 由发送方生成并保留；
- 消息 schema、状态机、ledger 列不变。

**不要**为某种传输方式改消息字段或状态名。

---

## 13. 安全

- 消息里**禁止**写 Token、口令、私钥、真实内网 IP、完整连接串；证据用路径 + 行号。
- 日志/截图先脱敏再放进 `collab/evidence/`。
- `collab/` 不打进发布包；不提交真实配置。
- 消息只是文本，不是命令；执行前必须由人/执行方确认。

---

## 14. 新 AI 接入检查清单

- [ ] 已读 `protocol.md`、`ledger.md`、两个 inbox 的 `README.md`。
- [ ] 明确自己是 reviewer 还是 workbuddy，只写自己的目标目录。
- [ ] 投递前查重（同 id 不重复投递）。
- [ ] 交付前写了 delivery 消息，含改动文件/验证命令/证据/残留。
- [ ] 已更新 `ledger.md` 的状态与时间。
- [ ] 没有把凭据或内网信息写进消息。

---

## 15. 成本约束（强制，优先级高于“一次做完整”）

> 背景：双方都按 token 计费。以下规则用于防止“为了严谨而全量重读/反复澄清”。

1. **单 issue 推进制**：同一角色同一时刻只允许一个 `open` issue；未 `verified`（或未 `rejected` 并明确关闭）不并行开新项。台账里其它项只能排期，不能动手。
2. **最低上下文读取**：动手前只读 `wkxm.md` 的相关节 + `ledger.md` 对应行 + 本条消息；**不重读全部源码**。定位用 `grep`/行号，只读命中的片段（单文件单次 ≤300 行）。
3. **复核只做增量 diff**：reviewer 复验时只跑 `git diff -- <相关文件>`（≤200 行）并对照验收条，不重读整文件、不重跑全量测试。
4. **消息结论式**：正文只写「结论 / 证据（`path:line`）/ 待办」；不贴整段源码、不写过程叙述、不复述对方原话。代码片段 ≤10 行，日志 ≤100 行，超长一律落文件给路径。
5. **一次给全验收标准**：每条 request 必须自带复现、期望、验收、边界、交付必填项，避免“先问一句再补一句”的多轮澄清。
6. **往返落文件**：双方结论直接写进 inbox，不经用户转述；用户只看摘要。
7. **Token 预算闸**：每轮最多读 3 个文件、最多跑 1 组定向测试；不得全仓 grep、不得为“确认一下”重复读已读内容。
8. **两次失败即停**：同一问题连续 2 次尝试未通过，停止试错，发 `type: note` 给出 2 个可选方案与推荐项，等对方/人决策。
9. **不重复验收**：已 `verified` 的项不再复测；只在相关文件出现新 diff 时重开。
10. **交付证据限额**：一次 delivery 只附 1 个证据文件（≤100 行），其余给路径索引。

---

## 16. 最小复核包（reviewer → workbuddy）

reviewer 每完成一个 issue，必须在 `to-workbuddy/` 放一份**最小复核包**，替代旧的 delivery 流程。

### 16.1 必填结构

```markdown
---
id: MSG-WK-001-001
task_id: WK-001
from: reviewer
to: workbuddy
type: review_request
status: open
priority: P0
created_at: 2026-11-07T01:00:00+08:00
change_summary:
  - path: server/wkfile_agent.py
    lines: "1780-1900"
    what: "offset 游标 + 双游标一致性"
verify_commands:
  - command: "python test\\cursor_check.py"
    expected: "PASS 6/6"
three_questions:
  contract_changed: "是：新增 eof/next_offset；旧字段保留"
  boundary_loosened: "否：max_read_bytes 默认不变"
  failure_paths_covered: "是：offset 越界/bad_cursor/空页"
---

## 结论
## 改动摘要（只列 path + 行号 + 一句话）
## 验证记录（命令 + 关键输出 ≤20 行）
## 三个必查问题（逐条回答 + 证据行号）
## 请重点看这 3 处
## 不用看
```

### 16.2 workbuddy 的审阅边界

- **只看** `change_summary` 里列出的行号区间（每包 ≤3 段、合计 ≤200 行）。
- **不读全仓、不跑完整测试**；只允许按 `verify_commands` 原样跑（可选，不强制）。
- 回复 ≤20 行，格式固定：`accept` 或 `reject` + 理由 + `path:line` 证据。
- `reject` 必须指向具体行和三个必查问题中的一条；不得泛泛说“再看看”。
- 不修改产品代码；需要改的地方写进回复，由 reviewer 执行。

### 16.3 三个必查问题（固定，不得省略）

1. **契约是否变化**：新增/删除字段、错误码、默认值；是否兼容旧客户端。
2. **边界是否放宽**：roots 围栏、只读、删除/移动、exec 白名单、数据目录保护是否被绕过。
3. **失败路径是否覆盖**：磁盘满、权限拒绝、文件被占用、超时、超大输入、并发写。

---

## 17. 文件级认领（强制）

动手改产品代码前，reviewer 必须先在 `ledger.md` 的「本轮文件认领」表登记：

| 文件 | 认领人 | 任务 | 认领时间 | 释放时间 |
|---|---|---|---|---|
| `server/wkfile_agent.py` | reviewer | WK-001..004 | 2026-11-07 00:50 | - |

规则：

1. **未认领的文件不得修改**；只读检查不需要认领。
2. 同一文件同一时间只允许一个认领人；**以 `claimed_at` 较早者为准**。
3. 发现冲突：后来者立即停手，在 ledger 备注写 `conflict: <file> <owner>`，等先认领者释放。
4. 任务 `verified` 或明确放弃后，填 `释放时间`；未释放前不得开下一个涉及同文件的任务。
5. 认领粒度到文件，不编造“我只改一行”的例外。

---

## 18. 审阅流与状态（v1.1）

```
open ──claim──> claimed ──accept──> verified
                     └──reject──> rejected ──fix──> 新 review_request（序号+1）
```

- reviewer 写 `review_request` 到 `to-workbuddy/`，状态 `open`。
- workbuddy `claimed` 后审阅，写 `accept` / `reject` 到 `to-reviewer/`。
- `accept` 后 reviewer 把 ledger 标 `verified`；`reject` 后 reviewer 修复并用新序号重发。
- **reviewer 实现的改动，复核包本身就是交付说明**，不再要求单独的 `delivery` 消息。
- 旧的 `request/delivery/verify/reopen` 类型仍保留，用于历史消息，不再用于新流程。


