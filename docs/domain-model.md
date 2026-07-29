# MultiNexus 领域模型

> **状态：当前 MultiNexus 拥有的运行时实体。** 产品级 Operator、Coordinate、
> Harness 和 Executor 定义在 [product-definition.md](product-definition.md)。

## 运行时实体

### AgentConfig

一个托管 agent 身份和 adapter 的配置。它包括运行时身份、adapter 类型、
工作目录、超时、平台身份、已知对等方、会话行为和 agentd 设置。
配置不授予产品级 Operator 权限。

### KnownAgentMention

托管或外部对等方的平台路由身份。别名和平台 ID 使交接能够解析为原生提及。
这是路由元数据，不是持久化项目所有权的 agent 注册表。

### AgentRequest

平台无关的调用 envelope。它携带请求 ID、目标 agent、prompt、来源/目标
元数据、作者、会话作用域和可选的托管交接上下文。

### AgentResponse

平台无关的结果 envelope。它携带请求/agent 身份、输出文本、会话 ID、
成功分类、交接/报告行、时序和结构化运行时元数据。它是执行证据，
不是自动接受或完成。

### AdapterResult

由 adapter 规范化的 executor 特定调用/恢复结果。当前通用字段包括文本、
会话 ID、恢复状态和元数据。Adapter 可以在元数据中保留更丰富的厂商能力，
而不是将每个 executor 都扁平化为 prompt/text。

### AgentdWorker

一个托管 agent 身份的长期运行 worker。它向 Coordinate 注册，认领符合条件
的 job，调用 adapter，报告活跃性/进度，并返回结构化终态结果。
Coordinate 对托管 job 保持权威。

### Bridge

平台特定的入站和出站运行时。Bridge 处理 Gateway 或轮询、身份、allowlist、
提及解析、消息格式化和可见投递。它不拥有项目工作流状态。

### Session

从 `(scope_id, agent_id)` 到 executor 原生会话标识符和运行时元数据的映射。
它支持跨可见轮次的恢复，但仍是可替换的 scratch 状态；没有它项目也必须
可恢复。

### ContextMessage

为 prompt 构建而存储的可见对话记录，带 TTL 和预算限制。它可以改善连续性，
但不是项目完成的权威来源。

## Agent 种类

| 种类 | 调用所有权 | 会话处理 | 示例 |
|---|---|---|---|
| 托管 | MultiNexus adapter/agentd | MultiNexus 加 executor 原生会话 | Claude Code、Codex、OpenCode、OMP |
| 外部网关 | 外部运行时 | 外部运行时 | OpenClaw、Hermes 网关 |
| 复合 executor | 父托管/外部运行时 | Executor 内部定义 | Agent team、subagent 图、原生 workflow |

## 关系

```text
AgentConfig
  ├── 选择 Adapter
  ├── 标识 AgentdWorker
  ├── 拥有作用域 Session
  └── 引用 KnownAgentMention 对等方

Bridge
  ├── 创建 AgentRequest
  ├── 可通过 Coordinate 提交托管 job
  └── 将 AgentResponse 渲染到平台

AgentdWorker
  ├── 认领 Coordinate Job
  ├── 调用 Adapter
  └── 报告进度和 AgentResponse
```

## 所有权摘要

| 事实 | 权威来源 |
|---|---|
| Adapter 调用、恢复、原生会话、本地运行时健康 | MultiNexus/运行时 |
| 托管 job 和尝试生命周期 | Coordinate DB |
| 平台身份和路由配置 | MultiNexus 配置 |
| 项目计划、验收、任务完成 | Harness 加必需证据 |
| 内部复合 agent 图 | 所属 Executor，除非提升为托管委派 |
