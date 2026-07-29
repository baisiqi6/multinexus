# Agent-Report 协议

`agent-report` 是 MultiNexus managed agent 在 Coordinate handoff 期间返回的结构化
状态行。它提供可见性和摄取输入，不直接改变项目完成状态。

## 格式

```text
[agent-report] action=<action> workspace_id=<workspace> task_id=<task> [summary=<text>] [reason=<text>]
```

- `action`：`accept`、`progress`、`blocker` 或 `done`；
- `workspace_id`、`task_id`：必填；
- `summary`、`reason`：可选，含空格时使用 shell quoting；
- report 必须从新行行首开始；普通人类摘要可以放在 report 之前。

Review handoff 还接受：

```text
[agent-report] decision=approve workspace_id=<workspace> task_id=<task> summary=<text>
[agent-report] decision=reject workspace_id=<workspace> task_id=<task> reason=<text>
```

准确的 parser/build 行为以 `multinexus/handoff_handler.py` 为准。

## 生命周期语义

| Report | 含义 |
|---|---|
| `accept` | runtime 已完成 assignment accept；不是执行完成 |
| `progress` | worker 报告阶段性进展；不推进终态 |
| `blocker` | worker 无法继续或需要 Operator 决策 |
| `done` | worker 认为执行已完成并请求 review；不等于 accepted/closed |
| `decision=approve/reject` | reviewer 的结构化意见；最终 gate 仍由 Coordinate/Harness authority 决定 |

`done` 不会自动执行 closeout、mark-done、merge、deploy 或 publication。

## Auto-Accept

收到受支持的 Coordinate handoff 后，runtime 会：

1. 解析并验证 handoff；managed mode 还要求完整 v1 authority 与 channel/workspace 绑定；
2. 通过配置的 Coordinate CLI 执行一次 `assignment.accept`；
3. 成功后发送 `action=accept`，解析 bootstrap 并调用 adapter；
4. 失败、authority 不完整或 bootstrap 缺失时 fail closed，发送 `action=blocker`；
5. adapter 返回而没有 execution report 时，发送 bounded fallback report。

Runtime 自动 mutation 只限于允许的 handoff action。其他生命周期、forge 或生产
操作由当前 Operator 通过 Coordinate/Harness 的明确 authority 执行。

## 可见性与安全

- 自动 report 使用 `AllowedMentions.none()`，避免报告文本触发其他 bot；
- report 是平台消息，因此不是持久化项目真相；Coordinate 摄取、Harness 验收或
  forge 状态才是相应事实的 authority；
- `summary` / `reason` 不得包含 token、credential、完整私有 prompt 或 provider
  private reasoning；
- provider-native JSONL 可帮助 Operator 判断活跃性，但 report 只携带必要的外部
  可观察摘要。
