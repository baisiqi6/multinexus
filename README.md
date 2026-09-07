# MultiNexus

> 本仓库是 MultiNexus 受支持的公开稳定版本与社区入口。已停止维护的 [`baisiqi6/discord-nexus`](https://github.com/baisiqi6/discord-nexus) 只作为历史 lineage 保留；来源与授权说明见 [docs/provenance.md](docs/provenance.md)。

MultiNexus 是一个 agent 执行织物，用于将可替换的托管和外部 agent 运行时 — 包括 Claude Code、Codex、Qoder、Grok Build、OpenCode、Hermes、OMP、ZCode 和支持 ACP v1 的 agent — 连接到持久的项目工作。

它顶层与 Coordinate 项目协调内核、项目 Harness 和当前 Operator 共同工作：

- **Coordinate**：确定性控制面机制，记录 job、claim、delivery、恢复和审查证据；
- **MultiNexus**：agent 运行时调用、恢复、健康、会话上下文、平台无关 envelope 和 bridge 投递；
- **Operator**：当前被授权的决策者，可能是人类或 agent；
- **Harness**：持久化项目意图、范围、计划、验收标准和粗粒度工作流。

“顶层”是编排作用域，不是新增中央实体。系统组合各 agent 已有的 subagent、agent team、skill、plugin 和 workflow 能力，而不重新实现它们。

Discord 是可见的交互 adapter，不是产品边界。未来可以增加其他 bridge。

---

## 它是什么

MultiNexus 让 Operator 或 Coordinate 托管的 job 调用复合 agent 运行时，同时保留其原生能力。通过 Discord 界面，agent 可以：

- 通过角色提及响应消息（`@Claude`、`@Local Agent`、`@Codex`）
- 通过 `[handoff]` 协议相互交接任务
- 维护 per-thread 的对话历史和会话
- 使用仓库提供的 `WikiStore` 组件管理共享/私有 Markdown 知识；当前 Discord bridge 尚未直接接入该组件
- 从对话历史中提取和注入持久记忆（通过 `washer.py`）

每个 agent 表现为独立的平台身份。直接 CLI、API、插件和未来的 bridge 入口点可以使用相同的 adapter 和 agentd 层。

MultiNexus 支持两种运行模式：

1. **standalone direct-adapter**：`multinexus.py --platform discord` 直接调用本地 adapter；适合单主机、对话驱动的使用；
2. **Coordinate-managed profile**：`multinexus.py` bridge 提交 job 到 Coordinate，由 `python -m multinexus.agentd --agent <id>` 在各个 agent 身份上认领执行；适合需要跨主机恢复、审查和独立生命周期的项目工作。

第二种模式需要先安装 [Coordinate v0.4.0](https://github.com/baisiqi6/coordinate/releases/tag/v0.4.0)，并选择 CLI（默认）或 loopback HTTP transport。agentd 启动前校验实际契约能力，缺失时拒绝 claim。Standalone 不依赖 Coordinate。

### v0.2.0 升级

本版新增 ZCode direct/app-server adapter、Runtime HTTP、managed context cursor、回复 outbox 与可选 agentd 健康文件。managed Discord 的消息和 slash-command 准入现在以 Coordinate **channel binding** 为权威，配置中的静态 `channels` 只约束 standalone；升级前检查绑定。

先升级 Coordinate，再停用访问本地 context/session SQLite 的进程并备份数据库，最后升级 MultiNexus。首次打开数据库会为 sessions 增加 `context_generation`、`context_cursor_order_token`、`context_cursor_message_id`，并创建 `runtime_reply_outbox`，保留旧行。回退前须核查并处理待投递 outbox；旧版不理解新 cursor/outbox，不能承诺无损降级。恢复备份会舍弃备份后的记录。完整配置见 [平台设置](docs/platform-setup.md#runtime-http-与-v020-升级)。

ZCode 默认 `headless`；受控写入/测试需显式启用 `app-server`，并安装已核验的固定 native bundle。权限与恢复限制见 [ZCode 权限说明](docs/zcode-permissions.md)。

---

## 架构

```
multinexus.py --platform discord --config agents.toml
      │
      └── multinexus/client.py  DiscordBridge → [每个 agent 一个 DiscordClient]
            │   每个 agent 是自己的 Discord 身份
            │
            ├── multinexus/adapters/        ACP / Claude / Codex / Qoder / Grok / OpenCode / OMP / ZCode 网关
            ├── multinexus/agentd/          Coordinate agent worker 运行时
            ├── multinexus/sessions/        per-scope 会话持久化
            ├── multinexus/commands.py      operator 命令处理器（文本）
            ├── multinexus/embeds.py        /health /agents /session status 的 embed 构建器
            ├── multinexus/handoff.py       交接消息解析
            ├── persistence/db.py           SQLite (aiosqlite) — 历史、jobs、记忆、workspaces
            └── services/wiki.py            可复用的平文件 WikiStore（尚未接入当前 bridge）

washer.py（可选独立记忆提取）
      │
      ├── 读取 conversations + conversations_archive（基于水位线）
      ├── 调用本地 LLM（OpenAI 兼容 HTTP）进行记忆提取
      ├── memory/content_validator.py  — 过滤机密 + 验证类型
      └── 路由到：
            ├── persistence/db.py → memories          (fact)
            └── persistence/db.py → memory_promotions (preference/context)
```

> 兼容入口：`multinexus.py --agent <id>` 为一个 agent 托管单个 Discord 客户端。

当前 bridge 会解析 handoff/report 等运行时协议并分块投递。`services/wiki.py` 与
`security/filter.py` 是可复用组件，但尚未全局接入 Discord 发布路径；不要把它们
视为对全部 agent 输出的自动内容治理或机密防泄漏保证。

---

## 快速开始

### 1. 前置条件

- Python 3.11+
- Discord 应用和 bot token（[discord.com/developers](https://discord.com/developers)）
- 至少一个受支持的 CLI executor：
  - [Claude Code CLI](https://docs.anthropic.com/claude-code)（`npm install -g @anthropic-ai/claude-code`）
  - [Codex CLI](https://github.com/openai/codex)（`npm install -g @openai/codex`）
  - Qoder CLI 或 Grok Build CLI（可选，当前走 direct JSON adapter）
  - 任意提供 stdio ACP v1 server 的 agent CLI（可选）
  - [OpenCode](https://opencode.ai/)、OMP 或 Hermes CLI
- 使用 Coordinate-managed profile 时，还需安装
  [Coordinate v0.4.0](https://github.com/baisiqi6/coordinate/releases/tag/v0.4.0)；standalone profile 不需要。

### 2. 克隆和安装

```bash
git clone https://github.com/baisiqi6/multinexus.git
cd multinexus
python -m venv .venv
# Windows:  .venv\Scripts\activate
# Mac/Linux: source .venv/bin/activate
pip install -r requirements.txt
```

### 3. 配置

**推荐：Standalone 首次配置向导**

如果你只想先连接一个 Discord Bot 和一个本机 agent CLI，运行：

```bash
python -m multinexus.setup
```

向导会提示你在 Discord Developer Portal 完成人工步骤，隐藏读取 Bot Token，生成单 agent
`agents.toml` 与 owner-only `.env`，并在本地执行只读检查。它不会替你创建或邀请 Bot，也不会
调用 Discord/provider API。配置完成后可随时复查：

```bash
python -m multinexus.setup --check
```

**高级：Coordinate-managed / 多 agent 手工配置**

```bash
cp .env.example .env
cp agents.toml.example agents.toml
```

编辑 `.env` — 填入你的 Discord bot token。
编辑 `agents.toml` — 按示例配置一个 `claude` agent。

Coordinate-managed 运行还需要：

```toml
[defaults]
agentd_mode = true
coordinator_cli_path = "/absolute/path/to/coordinate/.venv/bin/coordinate"
coordinator_db_path = "/absolute/path/to/coordinate/data/coordinator.sqlite3"
```

`coordinator_db_path` 必须是当前宿主机可访问的绝对路径，不能由多台宿主机直接共享。
本地安装和不发送消息的一次性验证见
[`docs/platform-setup.md`](docs/platform-setup.md#coordinate-managed-本地-no-send-验证)。

完整配置与演练见 [`docs/platform-setup.md`](docs/platform-setup.md)。

**通用 ACP v1 adapter（当前不在 Standalone 向导内）：**

```toml
[[agents]]
id = "kimi-acp"
adapter = "acp"
display_name = "Kimi via ACP"
token_env = "DISCORD_KIMI_ACP_TOKEN"
work_dir = "."
acp_command = "/absolute/path/to/kimi"
acp_args = ["acp"]
```

`acp_command` 应使用绝对路径；`acp_args` 是参数数组，不会出现在 health payload 中。当前
adapter 固定使用 ACP v1，支持 fresh session 和由 provider capability 声明的 resume/load，
默认拒绝 permission request，也不向 provider 声明 filesystem、terminal 或 terminal-auth
能力。因此这一版适合先验证安全的文本通信；需要 agent 执行工具的场景仍应使用现有 direct
adapter，直到后续引入显式、可审计的 ACP permission policy。

provider 的登录流程由用户在 MultiNexus 外完成，不由 adapter 自动执行。例如 Kimi Code 可先运行：

```bash
kimi acp --login
```

详细说明与故障排查见 [`docs/platform-setup.md`](docs/platform-setup.md#通用-acp-v1-agent)。

**Qoder / Grok Build（当前不在 Standalone 向导内）：**

当前 Qoder 1.1.x 和 Grok Build 0.2.x 没有 stdio ACP server，分别使用 `adapter = "qoder"`
与 `adapter = "grok"` 的 direct JSON 路径。它们与 ACP 共用 `AgentAdapter` / `AdapterResult`
上层契约，并支持显式 session resume（fresh 会话返回 provider session ID，后续以
`--resume <session-id>` 恢复）；direct adapter 不会因 ACP 存在而删除。安装、登录、
安全 permission 默认值与配置示例见
[`docs/platform-setup.md`](docs/platform-setup.md#qoder-cli)。

> 上述三类 adapter 不在 `python -m multinexus.setup` 的 Standalone 向导选项内；手工配置后
> 可随时用 `python -m multinexus.setup --check` 做只读复查。

### 4. 运行

```bash
python multinexus.py --platform discord --config agents.toml
```

### 5. 邀请 bot

在 Discord Developer Portal 中，启用 **Message Content Intent** 并生成邀请 URL：
- `bot` scope
- `applications.commands` scope
- 权限：Send Messages、Manage Webhooks、Read Message History、Embed Links、Add Reactions

---

## 功能

| 功能 | 描述 |
|---|---|
| 多 agent 路由 | 每个 agent 是自己的 Discord 身份；消息路由到配置的 agent |
| `[handoff]` 协议 | Agent 通过响应中的 `[handoff] <@agent>` 行相互交接任务 |
| 会话持久化 | Per-scope 的 Claude/Codex/Qoder/Grok/ACP 会话在后续消息中恢复 |
| Per-thread 历史 | 对话历史按 thread/channel 存储在 SQLite 中 |
| 分块输出 | 响应在发布前被拆分为 Discord 大小的块 |
| Operator 命令 | 文本命令：`agents`（列表）、`health`（检查）、`session status`、`session reset` |
| WikiStore 组件 | 提供共享/私有 Markdown 存储、检索和 secret scrubbing；当前 bridge 集成仍需调用方接入 |
| 持久记忆 | `washer.py` 通过本地 LLM 从历史中提取事实/偏好/上下文 |
| 机密过滤组件 | `security/filter.py` 提供 defense-in-depth redaction；当前并非所有发布路径都会自动调用 |
| 跨平台 | 支持 Windows 和 Mac/Linux |

---

## 记忆洗衣机

`washer.py` 是一个可选的记忆提取管道，使用本地 LLM（LM Studio / Ollama）从你的对话历史中收获持久记忆。

它从 `conversations` 和 `conversations_archive` 读取，调用本地模型进行提取，并将结果路由到两个层级：

- **共享记忆**（`fact` 类型）— 注入到所有 agent prompt
- **共享提升**（`preference` / `context`）— 注入前排队等待审查

私有条目可以通过配置单独的私有数据库路由；`PRIVATE_DB_PATH` 控制其存储位置。

**设置：**
```
# .env
TARGET_USER_ID=your_discord_user_id
USER_DISPLAY_NAME=YourName
E4B_BASE_URL=http://localhost:1234/v1
E4B_MODEL=gemma-3-4b-it
```

自行用 cron、Windows Task Scheduler 或 systemd timer 调度：

```bash
# 例如每天 2am
0 2 * * * cd /path/to/multinexus && /path/to/venv/bin/python washer.py
```

记忆洗衣机概念来自 **Mark Kashef** — ["I Tried OpenClaw and Hermes. I Kept Claude Code."](https://youtu.be/rVzGu5OYYS0)（时间戳 10:57）。

---

## Agents

| Agent | 类型 | 必需 |
|---|---|---|
| `claude` | Claude Code CLI 子进程 | 否 |
| `codex` | Codex CLI 子进程 | 否 |
| `opencode` | OpenCode CLI 子进程 | 否 |
| `omp` | Oh My Pi CLI 子进程 | 否 |
| `hermes` | Hermes CLI one-shot 子进程 | 否 |
| `qoder` | Qoder CLI direct JSON 子进程 | 否 |
| `grok` | Grok Build CLI direct JSON 子进程 | 否 |
| `acp` | 任意 stdio ACP v1 agent server | 否 |

必须至少配置一个 agent 并在线。见 [`docs/agents.md`](docs/agents.md)。

---

## 文档

- [产品定义](docs/product-definition.md) — 共享使命、角色、委派边界和权威来源归属
- [运行时架构](docs/architecture.md) — 执行织物、agentd、adapters、bridges 和托管执行
- [范围](docs/scope.md) / [领域模型](docs/domain-model.md) — 仓库边界和运行时实体
- [工作流模式](docs/workflow-modes.md) — ordinary / high-risk 两种执行模式
- [多 Agent 协作](docs/multi-agent-collaboration.md) — 协作工作流的当前文档入口点
- [Agents](docs/agents.md) — 配置每种 agent 类型，添加自定义 agent
- [WikiStore 组件](docs/wiki-system.md) — 已实现的存储 API、当前集成边界与后续接入方式
- [平台设置](docs/platform-setup.md) — 可移植前台安装指南

---

## 数据与隐私

| Agent | 推理运行位置 | 数据离开你的机器？ |
|---|---|---|
| `claude` | Anthropic API（云端） | 是 — prompt 发送到 Anthropic |
| `codex` | OpenAI API（云端） | 是 — prompt 发送到 OpenAI |
| `qoder` | 取决于 Qoder 当前 provider/model | 通常是 |
| `grok` | 取决于 Grok Build 当前 provider/model | 通常是 |
| `acp` | 取决于你配置的 ACP provider | 取决于 provider |
| `opencode` / `omp` / `hermes` | 取决于对应 CLI 的 provider 配置 | 可能；请检查所选 provider |

**无论你使用哪些 agent，都保持本地的内容：**
- 对话历史（你机器上的 SQLite 数据库）
- wiki（`wiki/pages/`、`wiki/private/`）
- 所有配置、机密和 bot 状态

是否使用云端推理由所选 executor/provider 决定；MultiNexus 不会替 provider 作出
隐私保证。请同时检查 CLI 配置、prompt 内容和目标 workspace。

---

## 安全说明

- Bot token 和 API 密钥从 `.env` 读取 — 永远不要提交这个文件
- 私有 wiki 页面应位于 `wiki/private/`（已 gitignore）；`PRIVATE_DB_PATH` 控制私有 SQLite DB 的存储位置
- 在 Windows 上，私有 DB 目录在首次运行时用 `icacls` 加固
- `security/filter.py` 和 `WikiStore` 写入前 scrub 是 defense-in-depth，不替代最小权限、env 隔离和发布前审查；当前 Discord bridge 不承诺全局自动扫描所有输出
- allowlist 控制谁可以使用 `session reset` 和其他特权 operator 命令

---

## 致谢

可选的 `OpenClawRelayAgent` 设计用于与 Light Heart Labs 的 [Dream Server](https://github.com/Light-Heart-Labs/DreamServer) 配合工作 — 一个完全本地的 AI 技术栈（LLM 推理、agents、语音、工作流、RAG），可以用单个命令部署在你自己的硬件上。如果你想要完整的自托管设置，它是 MultiNexus 的自然配套。

---

## 许可证

MIT — 见 [LICENSE](LICENSE)。
