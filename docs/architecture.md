# MultiNexus 架构

> **状态：当前运行时架构。** 共享产品角色和权威规则定义在
> [product-definition.md](product-definition.md)。

## 在系统中的位置

```text
人类或 agent Operator
        │
        ├── 直接交互 ───────────────┐
        │                                     ▼
        └── Coordinate 托管 job ──> MultiNexus 执行织物
                                              ├── agentd / session / context
                                              ├── Claude Code / Codex / OpenCode / OMP
                                              ├── 外部 OpenClaw / Hermes 网关
                                              └── Discord / 未来 bridge
```

MultiNexus 执行 agent 工作并返回结构化运行时结果。它不拥有项目生命周期决策、
持久化 Coordinate jobs、harness 验收或 forge 真相。

## 入口点

- `python multinexus.py --config agents.toml --platform discord` — 当前的 per-platform Discord bridge，托管 N 个已配置的 agent 身份。
- `python multinexus.py --config agents.toml --agent <id>` — 为一个 agent 托管一个 Discord 客户端的兼容模式。
- `python -m multinexus.agentd --config agents.toml --agent <id>` — 每个托管 agent 身份一个连接 Coordinate 的 worker daemon。

## 模块映射

```text
multinexus.py                         Discord bridge/兼容入口点
multinexus/
  client.py                          DiscordBridge + DiscordClient
  config.py                          agents.toml 加载和验证
  models.py                          agent 和对等方配置
  protocol.py                        平台无关的请求/响应 envelope
  coordinator_handoff.py             托管交接接入和生命周期粘合
  commands.py / embeds.py            可见 operator 命令和视图
  handoff.py / routing/               交接解析和提及解析
  adapters/                           executor 特定的调用/恢复/健康 adapter
  agentd/                             连接 Coordinate 的 agent worker 运行时
  context/                            作用域可见对话上下文
  sessions/                           executor 会话持久化
  security/                           operator allowlist 和子进程环境过滤
persistence/                          SQLite 历史、jobs、记忆、workspaces
services/wiki.py                      可复用 WikiStore（当前 bridge 尚未 wiring）
washer.py                             可选独立记忆提取管道
```

## N+M 运行时

平台 bridge 和 agent 运行时分离，这样一个 agent 身份不需要每个平台一个进程。

```text
Discord bridge ──┐
                 ├──> Coordinate 运行时 job ──> agentd (host-codex)  ──> Codex adapter
                 │                          └────> agentd (host-claude) ──> Claude adapter
```

- **Bridge：** 平台 Gateway/轮询、身份、过滤、提及路由、渲染。
- **Coordinate：** 托管 job、认领、尝试、活跃性、结果和投递记录。
- **Agentd：** adapter 调用、恢复、超时、进度、结果报告。
- **Adapter：** executor 特定的 CLI 或 API 行为。

兼容路径可以直接从 bridge 调用 adapter。新的托管项目执行在需要持久化生命
周期和恢复时，应使用 Coordinate 加 agentd。

## 执行流程

### 直接交互

```text
平台消息
  → bridge 过滤和路由
  → 作用域上下文构建
  → adapter 调用/恢复
  → 结构化 AdapterResult
  → 响应渲染和上下文持久化
```

这适用于对话交互，或生命周期仍由当前 agent 会话负责的执行。

### Coordinate 托管执行

```text
Coordinate job
  → agentd 带 attempt token 认领
  → 在配置的 workspace 中 adapter 调用/恢复
  → 心跳/进度
  → 结构化 AgentResponse/report
  → Coordinate 记录终态事件和可见 delivery
```

这适用于工作需要独立恢复、重新分配、取消、审查、权限或跨主机执行的情况。

### Executor 内部委派

Adapter 可以调用创建自己 subagent 或 workflow 的复合 executor。
MultiNexus 不扁平化该内部图。父调用负责返回一致的结果，除非子任务被
显式提升为 Coordinate 托管 job。

## 运行时状态所有权

| 状态 | 所有者 |
|---|---|
| Adapter 调用和原生会话 id | MultiNexus / executor 运行时 |
| 作用域可见对话上下文 | MultiNexus context 存储 |
| 托管 job、尝试、活跃性和终态事件 | Coordinate DB |
| 项目计划、验收和粗粒度工作流 | Harness 文件 |
| 代码和 forge 状态 | Git / 配置的 forge |
| 平台消息记录 | 平台，作为可见的非权威记录 |

## 关键参考

- 共享产品定义：`docs/product-definition.md`
- 仓库范围：`docs/scope.md`
- 运行时实体：`docs/domain-model.md`
- 工作流模式：`docs/workflow-modes.md`
- Agent 配置：`docs/agents.md`
- 平台设置：`docs/platform-setup.md`
- 来源与维护：`docs/provenance.md`

历史设计与任务证据不再进入当前架构导航；它们保留在私有开发历史中。
