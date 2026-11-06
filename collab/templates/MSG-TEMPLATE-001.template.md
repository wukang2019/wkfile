---
id: MSG-TEMPLATE-001
task_id: WK-XXX
from: reviewer
to: workbuddy
type: review_request
status: open
priority: P0
created_at: 2026-11-07T00:00:00+08:00
updated_at: 2026-11-07T00:00:00+08:00
related_issue: WK-XXX
reply_to: "-"
baseline: "当前工作区未提交代码（写明版本与范围）"
change_summary:
  - path: server/wkfile_agent.py
    lines: "0000-0000"
    what: "一句话说明这段改了什么"
verify_commands:
  - command: "python test\\xxx\\check.py"
    expected: "ALL PASS"
three_questions:
  contract_changed: "是/否 + 字段与兼容性"
  boundary_loosened: "是/否 + 具体围栏"
  failure_paths_covered: "是/否 + 覆盖了哪些失败路径"
---

## 结论
## 改动摘要（只列 path + 行号 + 一句话）
## 验证记录（命令 + 关键输出 ≤20 行）
## 三个必查问题（逐条回答 + 证据行号）
## 请重点看这 3 处
## 不用看

## 变更记录

- 2026-11-07T00:00:00+08:00 reviewer: 创建模板（非投递）
