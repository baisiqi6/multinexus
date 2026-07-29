# Coordinate + MultiNexus 产品定义

> **状态：规范跨仓库产品定义。**
>
> 本文档定义 Coordinate 和 MultiNexus 的共享产品定位和职责边界。仓库特定
> 文档可以添加实现细节，但不得重新定义这些术语，也不得将本文档复制为
> 第二个可编辑的权威来源。

私有开发 canonical 中的 active plan 可以安排工作，但不能重新定义本文档中的
稳定产品角色或权威规则。公开仓库不携带内部 roadmap、task packets 或运行证据。

## 使命

**产品初心：打造一个支持多宿主机、多 agent 通信、协作与编排的顶层
harness 系统。**

Coordinate 和 MultiNexus 构成一个 agent 无关的项目执行层。任何被授权的
人类或 agent 都可以临时充当 Operator，使用相同的持久化 harness 和执行
账本，并将工作委派给可替换的复合 agent，如 Claude Code、Codex、OpenCode、
OpenClaw、Hermes 或 OMP。

当 Operator 和 executor 可以更换而不丢失以下内容时，系统就是成功的：

- 项目意图和验收标准；
- 当前执行状态和恢复信息；
- 审查和 closeout 的责任；
- 物化行动的因果和审计线索。

持久化项目执行循环就是产品。通过 stdin、plugin、skill、SDK、IM 提及或
子进程进行的跨 agent 调用只是执行原语。

## “顶层”的含义

“顶层”描述的是编排作用域，不是智能排名、固定 coordinator 或新的中央实体。
本文所称“顶层 harness 系统”是 Coordinate、MultiNexus、项目 Harness 和当前
Operator 共同形成的架构层级，不是第四套状态系统。

- 它跨越单个 agent、session、宿主机和厂商 runtime 的生命周期，保留项目连续性。
- 它把 Claude Code、Codex、OpenClaw、Hermes、OMP 等 agent 及其原生 harness
  视为可替换的复合 Executor，而不是只把它们压平为一次模型调用。
- 它负责跨边界的目标委派、权威状态、运行监督、恢复、证据和安全收口；具体推理、
  工具调用、subagent、agent team 和内部 workflow 仍由下层 Executor 负责。
- 下层 agent 可以继续递归委派。除非子工作需要独立生命周期，顶层只管理父
  Executor 的边界承诺和结构化结果，不复制其内部执行图。
- 当厂商能力、调用接口或宿主机拓扑改变时，优先调整 adapter 和部署配置，不重写
  项目级协议，也不把某个旗舰 agent 固化为系统中心。

因此，下层 agent 自身增加 multi-agent orchestration 或 dynamic workflow，不会
取代本系统；这些能力会成为顶层可以直接组合的更强执行单元。

## 第一性原理与奥卡姆剃刀

系统真正需要解决的问题只有三个：

1. agent 跨 session 后仍能恢复关键上下文；
2. Operator 能明确知道当前权威状态和下一步；
3. 任务能被执行、监督、验证和安全收口。

凡是不直接服务这三个目标的 packet、字段、状态、副本和流程，都没有天然存在的
理由。设计和演进遵循以下简化规则：

- 能复用现有实体，就不新增实体。
- 能用一个权威来源，就不维护两个。
- ordinary 任务不走 high-risk 仪式。
- historical evidence 不进入当前导航。
- skill 只描述稳定规则，不复制完整 CLI 手册。
- Coordinate 已实现的能力，不在 Harness 中重新实现一遍。
- 只有能显著降低真实故障概率的复杂度才保留。

## 核心不变量

1. **Operator 和 executor 是可替换的。** 没有单个 agent 会话拥有项目。
2. **Coordinate 不是永久 coordinator。** 它记录并强制执行确定性协调机制；
   当前 Operator 做判断。
3. **嵌套 agent 系统默认保持不透明。** Claude Code agent team 或 Codex
   workflow 可以被视为一个复合 executor，除非其子工作需要独立的生命周期。
4. **每个事实只有一个权威。** 用于显示、缓存或恢复的副本是投影，必须可
   重建或显式对账。
5. **可见对话不是持久化项目状态。** Discord、KOOK、终端和聊天记录是交互
   面，不是完成的最终权威。
6. **完成是基于证据的。** Worker 响应不等同于已接受、已审查、已合并或
   已关闭的工作。
7. **厂商能力是可组合的。** 原生 subagent、agent team、skill、plugin 和
   workflow 引擎是 executor 的能力，不是竞争的控制面。

## 角色和组件

### Operator

Operator 是当前被授权的决策者。它可以是人类或 agent。Operator 读取持久化
状态，选择下一个行动，委派工作，处理升级，并决定证据何时足以推进 gate。

Operator 角色是临时和有作用域的。更换 Operator 不得要求从上一个 agent 的
对话中移动或重建项目真相。

### Coordinator

Coordinator 是一个运行时角色，不是永久分配的产品组件。当 Operator 分解、
路由、审查或推进工作时，它充当 coordinator。该角色可以在人类、Codex、
Claude Code 或其他有能力的 agent 之间转移。

使用 `coordinator` 的现有源标识符是兼容性名称。它们不意味着 Coordinate
拥有 AI 判断。

### Coordinate

Coordinate 是确定性协调内核和持久化控制面工具包。它拥有以下机制：

- workspace、job、event、delivery、runtime claim 和 runner 记录；
- 幂等生命周期转换和恢复；
- 到可见面的持久化 outbox 投递；
- runner 调度和托管交接记录；
- GitHub branch、PR、CI、review 和 merge-gate 证据；
- 对账和 drift 报告。

Coordinate 可以暴露供 Operator 使用的工具，但不得静默成为自主的产品级
决策者。可替换的 Operator 或显式策略后端提供判断。

### MultiNexus

MultiNexus 是 agent 执行织物。它通过可替换的 adapter 和平台 bridge 将
托管和外部 agent 运行时连接到项目工作。它拥有：

- agent adapter 调用、恢复、超时和健康；
- agent 本地会话和对话上下文；
- 平台无关的请求和响应 envelope；
- Discord、KOOK 和未来的交互 bridge；
- 托管交接的接入和结构化执行结果的返回。

Discord 和 KOOK 是有用的可见面，不是 MultiNexus 或整个产品的边界。
直接 CLI、API、plugin 或未来平台可以使用相同的执行织物。

### Harness

Harness 是持久化项目记忆和项目级协议。它记录已接受的范围、约束、计划、
验收标准、人类可读的进度和任务产物。它必须在不访问特定 agent 会话的
情况下保持可理解。

机器可读的 harness 状态是规范 harness 文件的投影，不是额外的手动编辑
真相。

### Executor

Executor 是执行委派工作的可替换 agent 运行时。它可以是单个 agent 进程，
也可以是拥有自己 subagent、team、skill 和 workflow 引擎的复合系统。
Executor 仅因拥有内部执行图并不拥有项目级真相。

## 委派边界

系统支持两种委派模式。

### 内部委派

内部委派保留在父 Executor 内部：

- 父方保留对结果的责任；
- 子步骤不需要独立恢复或权限；
- Coordinate 可以只记录父运行及其最终结构化结果；
- Executor 可以自由使用原生 subagent、agent team 或 workflow。

### 托管委派

当子工作需要以下任何一项时，委派变为托管：

- 独立的生命周期、租约、预算或权限作用域；
- 在另一个主机或运行时上执行；
- 外部可见的副作用；
- 单独的审查或验收；
- 父会话消失后的恢复；
- Operator 直接检查、取消、重试或重新分配。

托管委派通过 Coordinate 注册为子 job/run，并通过 MultiNexus 或其他
runner adapter 执行。区别在于职责和生命周期，而不是调用是通过 stdin、
plugin、SDK 还是网络发生。

## 权威来源归属

允许为审计、缓存和展示进行冗余。不允许重复权威。

| 事实 | 权威 | 派生或非权威视图 |
|---|---|---|
| 产品使命、共享角色、跨仓库边界 | 本文档 | README 摘要、prompt、图表 |
| 项目范围、约束、计划、验收、粗粒度任务工作流、assignment owner/lease | 规范 harness 文件 | `harness-state.json`、packets、仪表板 |
| 运行时 jobs、claim token、runner 尝试、活跃性、deliveries、持久化执行事件 | Coordinate 数据库 | CLI 输出、状态页、IM 通知 |
| 代码、commits、branches | Git | Coordinate 引用和镜像 |
| PR、CI、review、merge 状态 | GitHub 或配置的 forge | Coordinate 最后已知的 gate 证据 |
| Agent 调用、会话恢复、运行时健康、本地上下文 | MultiNexus 或所属运行时 | Coordinate job 摘要 |
| 人类可见的对话 | 所说内容的平台消息记录 | 上下文摘要和记忆提取 |
| 产品/项目完成 | Harness 验收加必需的运行时和 forge 证据 | Worker 声明、聊天确认 |

所有投影的规则：

1. 投影必须声明其权威和刷新路径。
2. 对账可以修复投影或报告 drift；不得静默覆盖权威来源。
3. 禁止对同一事实进行双向自由格式同步。
4. 历史文档必须标记为历史，并排除在当前导航路径之外。

## 仓库边界

### `coordinate`

Coordinate 仓库拥有确定性协调内核、其 schema、服务、策略、adapter、
运维工具和实现文档。它不拥有 MultiNexus 运行时内部或跨仓库产品定义。

### `multinexus`

MultiNexus 私有开发 canonical 拥有 agent 执行织物、adapter、bridge、会话、
运行时 envelope、部署说明及其 workspace-local active harness。公开仓库只发布
经过审查的稳定代码和文档，不携带 active harness 或过程证据；该放置规则不使
MultiNexus 成为控制面。

## 非目标

- 重新实现每个厂商的内部多 agent workflow。
- 将所有 agent 扁平化为最低共同标准的 prompt-to-text 接口。
- 要求 Discord 或 KOOK 进行项目执行。
- 使每个内部 subagent 对顶层控制面可见。
- 让 Coordinate、MultiNexus 或一个旗舰 agent 成为项目真相的唯一所有者。
- 把活跃、成功的进程退出或貌似合理的响应当作完成。

## 决策测试

在添加组件、状态字段、文档或工作流之前，问：

1. 它是否改善了持久化职责、恢复、权威、证据或 executor 可替换性？
2. 它拥有哪个事实，该事实今天的权威在哪里？
3. 现有投影或 adapter 能否在不创造另一个真相的情况下解决需求？
4. 它是否保留了使用厂商原生复合 agent 能力的可能性？

如果这些问题没有具体答案，该添加就不应进入核心。
