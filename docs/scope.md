# MultiNexus 范围

> **状态：当前 MultiNexus 仓库边界。** 共享产品定位定义在
> [product-definition.md](product-definition.md)。

## 项目

MultiNexus 是一个 agent 执行织物。它将可替换的托管和外部 agent 运行时连接到
Operator 或 Coordinate 发起的工作，同时保留每个 executor 原生的 subagent、
team、skill、plugin 和 workflow。

Discord 和 KOOK 是当前的可见 adapter。它们不是产品边界，也不是持久化项目
状态权威。

## 范围内

- Claude Code、Codex、OpenCode、Hermes、OMP 和兼容运行时的托管 agent adapter。
- 外部网关 agent 注册和平台提及路由。
- 平台无关的 `AgentRequest`、`AgentResponse` 和 adapter 结果 envelope。
- Agent 调用、恢复、超时、健康、进度和结构化结果返回。
- Per-agent 会话持久化和作用域对话上下文。
- N+M 运行时拓扑：平台 bridge 为每个托管 agent 身份共享一个 agentd。
- Discord 和 KOOK bridge 行为、过滤、身份映射、命令和投递。
- Coordinate 托管交接和 job 的接入。
- 用于对话协作的内部交接解析。
- 可移植的运行时配置 schema 与示例。

## 范围外

- 产品级规划、验收、审查判断或永久 Operator 行为。
- Coordinate 拥有的 events、jobs、deliveries、runtime claims、task mirrors 和 forge gates。
- 定义或直接编辑可复用的 harness 协议。
- 重新实现复合 executor 的内部 workflow 图。
- 拥有 Git commits、PRs、CI 结果、review 决策或项目完成。
- 要求每个内部 subagent 行动都成为顶层托管 job。
- 把平台消息记录或 agent 会话记忆当作项目真相。
- 主机特定的部署脚本、service 文件、SSH 配置和生产拓扑。

## 边界

- MultiNexus 拥有 agent 运行时调用和 agent 本地会话状态。
- Coordinate 拥有托管运行时 job 记录和持久化跨运行时执行事件。
- 当前人类或 agent Operator 选择委派什么工作以及如何评估。
- Harness 拥有已接受的项目意图、计划、验收标准和粗粒度工作流。
- 内部委派仍是父 Executor 的责任，除非它需要独立的生命周期。
- 托管委派在 Coordinate 注册，并可通过 MultiNexus agentd 执行。
- 公开仓库不包含托管集成项目的 harness 目录；运行时通过配置指定 harness_root。
- 外部/上游代码仓库使用 sidecar `harness_root`；像 `/opt/multinexus` 这样的部署副本不是开发权威来源。
