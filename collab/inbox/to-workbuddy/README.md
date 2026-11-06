# to-workbuddy（reviewer → workbuddy）

这里是 **reviewer 投给 workbuddy** 的收件箱。

- 只允许 reviewer 写入；workbuddy 只读、认领、改状态。
- 文件名：`MSG-<TASK-ID>-<NNN>.md`，例如 `MSG-WK-001-001.md`。
- 投递必须原子写（`.tmp` → rename）。
- 消息处理完（delivered / verified / rejected 终态）后，移动到 `../../archive/<TASK-ID>/`。
- 详细规则见 [../../protocol.md](../../protocol.md)。

## 当前

（空。第一条任务模板见 [../../templates/MSG-WK-001-001.template.md](../../templates/MSG-WK-001-001.template.md)，尚未投递。）
