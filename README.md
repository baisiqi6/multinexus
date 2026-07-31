# MultiNexus

> 本仓库基于 [`baisiqi6/discord-nexus`](https://github.com/baisiqi6/discord-nexus) 继续维护。原项目 README 标注为 MIT License，但没有独立 LICENSE 文件。来源与维护说明见 [docs/provenance.md](docs/provenance.md)。

MultiNexus 是一个 agent 执行织物，用于将可替换的托管和外部 agent 运行时 — 包括 Claude Code、Codex、OpenCode、Hermes 和 OMP — 连接到持久的项目工作。

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

第二种模式需要 Coordinate 控制面已部署并正确配置 `coordinator_cli_path`，不作为默认开箱即用路径。

---

## 架构

```
multinexus.py --platform discord --config agents.toml
      │
      └── multinexus/client.py  DiscordBridge → [每个 agent 一个 DiscordClient]
            │   每个 agent 是自己的 Discord 身份
            │
            ├── multinexus/adapters/        Claude / Codex / OpenCode / OMP / local LLM
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
  - [OpenCode](https://opencode.ai/)、OMP 或 Hermes CLI
- 使用 Coordinate-managed profile 时，还需安装
  [Coordinate](https://github.com/baisiqi6/coordinate)；standalone profile 不需要。

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
| 会话持久化 | Per-scope 的 Claude/Codex 会话在后续消息中恢复 |
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
