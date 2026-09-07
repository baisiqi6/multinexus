# 平台设置

可移植的前台设置说明。

---

## 前置条件

- Python 3.11 或更高版本
- Node.js 18+（用于 Claude Code CLI 和/或 Codex CLI）
- Git
- 带 bot token 的 Discord 应用

---

## 步骤 1：Discord 应用

1. 前往 [discord.com/developers/applications](https://discord.com/developers/applications)
2. 点击 **New Application** — 随意命名
3. 前往 **Bot** → 点击 **Add Bot**
4. 在 **Privileged Gateway Intents** 下，启用：
   - **Message Content Intent**
   - **Server Members Intent**（如果使用 allowlist 功能）
5. 复制 **Token** — 你会把它放到 `.env` 中

### 邀请 URL

在 **OAuth2 → URL Generator** 中，选择：
- Scopes：`bot`、`applications.commands`
- 权限：`Send Messages`、`Manage Webhooks`、`Read Message History`、`Embed Links`、`Add Reactions`

打开生成的 URL 并将 bot 添加到你的服务器。

### 获取 ID

在 Discord 中启用 **Developer Mode**（用户设置 → 高级 → 开发者模式）。
右键点击任何服务器、频道或用户以复制其 ID。

---

## 步骤 2：克隆和安装

```bash
git clone https://github.com/baisiqi6/multinexus.git
cd multinexus
python -m venv .venv
```

**Windows：**
```cmd
.venv\Scripts\activate
```

**Mac/Linux：**
```bash
source .venv/bin/activate
```

```bash
pip install -r requirements.txt
```

---

## 步骤 3：配置

### 推荐：Standalone 单 agent 向导

第一次试用时，优先从最小路径开始：一个 Discord Bot、一个授权频道、一个授权用户和一个
本机 executor。进入仓库并激活 virtualenv 后运行：

```bash
python -m multinexus.setup
```

向导会逐项询问 agent、executor、绝对 work dir、channel ID、user ID，并通过隐藏输入读取
Bot Token。它只在当前目录生成 `.env` 和 `agents.toml`；不会创建 Discord Application、邀请
Bot、验证远端 token 或调用 agent provider。若检测到现有 `agents.toml`，它不会覆盖，而是转为
sanitized check。

配置后可重复运行只读检查：

```bash
python -m multinexus.setup --check
```

检查成功后启动：

```bash
python multinexus.py --platform discord --config agents.toml
```

向导当前只覆盖 Standalone + Discord + 单 agent。需要 Coordinate-managed、多 agent 或多宿主机
拓扑时，继续使用下面的手工配置路径。

### 高级：手工配置

```bash
cp .env.example .env
cp agents.toml.example agents.toml
```

### .env

```
DISCORD_CLAUDE_TOKEN=your_bot_token_here
# LMSTUDIO_API_KEY=       # 可选，LM Studio / Ollama 留空
# OPENCLAW_GATEWAY_TOKEN= # 可选
# PRIVATE_DB_PATH=        # 可选，私有 SQLite DB 文件的绝对路径
```

### agents.toml

编辑 `agents.toml`，至少配置一个 agent，例如 `claude`：

```toml
[defaults]
agentd_mode = false
work_dir = "."

[[agents]]
id = "claude"
adapter = "claude"
display_name = "Claude"
aliases = ["Claude"]
token_env = "DISCORD_CLAUDE_TOKEN"
work_dir = "."
claude_bin = "claude"
```

Coordinate-managed 运行需要 Coordinate v0.4.0 的契约能力、`agentd_mode = true`，并在每个 agent 主机上运行 `python -m multinexus.agentd --agent <id>`。默认 CLI 设置 `coordinator_cli_path`；可选 HTTP 见下文。

### Coordinate-managed 本地 no-send 验证

先按 [Coordinate README](https://github.com/baisiqi6/coordinate) 在本机完成 fresh install。
以下默认 CLI transport 的三个关键值是：

- `agentd_mode = true`；
- `coordinator_cli_path`：本机安装生成的 `coordinate` console script 绝对路径；
- `coordinator_db_path`：当前宿主机独占访问的绝对 SQLite 路径。

下面用全新的临时目录、临时 DB 和临时配置注册本地记录，并对空队列执行一次 claim。它不会启动
长期 agentd、调用 provider、读取 Discord token、连接或发送 Discord：

```bash
COORDINATE_REPO="/absolute/path/to/coordinate"
MULTINEXUS_REPO="$(pwd -P)"
COORDINATE_CLI="$COORDINATE_REPO/.venv/bin/coordinate"
A2_ROOT="$(mktemp -d)"
COORDINATE_DB="$A2_ROOT/coordinator.sqlite3"
HARNESS_ROOT="$A2_ROOT/harness"
A2_CONFIG="$A2_ROOT/agents.toml"

mkdir -p "$HARNESS_ROOT"
cat > "$A2_CONFIG" <<EOF
[defaults]
agentd_mode = true
coordinator_cli_path = "$COORDINATE_CLI"
coordinator_db_path = "$COORDINATE_DB"
work_dir = "$MULTINEXUS_REPO"

[[agents]]
id = "claude"
adapter = "claude"
display_name = "Claude"
aliases = ["Claude"]
token_env = "DISCORD_CLAUDE_TOKEN"
work_dir = "$MULTINEXUS_REPO"
claude_bin = "claude"
EOF

"$COORDINATE_CLI" --db "$COORDINATE_DB" workspace add a2-local \
  --path "$MULTINEXUS_REPO" --harness-root "$HARNESS_ROOT"
"$COORDINATE_CLI" --db "$COORDINATE_DB" workspace host-profile set a2-local \
  --host-id local-a2 --workspace-path "$MULTINEXUS_REPO" \
  --harness-root "$HARNESS_ROOT" \
  --coordinator-cli-path "$COORDINATE_CLI" \
  --coordinator-db-path "$COORDINATE_DB"
"$COORDINATE_CLI" --db "$COORDINATE_DB" runtime agent register \
  --agent-id claude --host-id local-a2 --client-type agentd

.venv/bin/python - "$A2_CONFIG" <<'PY'
import asyncio
import sys

from multinexus.agentd.coordinate_client import CoordinateRuntimeClient
from multinexus.config import load_config

cfg = load_config(["--config", sys.argv[1], "--agent", "claude"], require_token=False)
result = asyncio.run(CoordinateRuntimeClient(
    cli_path=cfg.coordinator_cli_path,
    db_path=cfg.coordinator_db_path,
).claim_job(agent_id=cfg.id))
assert result.get("claimed") is False, result
print("NO_SEND_CLAIM_OK", result)
PY
```

若结果不是 `claimed=false`，立即停止：这表示你没有使用预期的全新 DB，或其中已经存在 job。
不要把这个 smoke 改成长时间运行的 agentd，也不要在本步骤填入真实 token。

---

## 步骤 4：安装 CLI Agents（可选）

### Claude Code CLI

```bash
npm install -g @anthropic-ai/claude-code
claude login
```

### Codex CLI

```bash
npm install -g @openai/codex
# 将 OPENAI_API_KEY 添加到 .env
```

### Qoder CLI

Qoder 当前通过 direct JSON adapter 接入。先在 MultiNexus 外完成 Qoder 登录，再确认：

```bash
command -v qodercli
qodercli --version
qodercli --list-models
```

model 名必须使用 `--list-models` 返回的当前精确值：

```toml
[[agents]]
id = "qoder"
adapter = "qoder"
display_name = "Qoder"
token_env = "DISCORD_QODER_TOKEN"
work_dir = "."
qoder_bin = "/absolute/path/to/qodercli"
model = "<model-id-from-qodercli --list-models>"
qoder_reasoning_effort = "high"
qoder_permission_mode = "dont_ask"
```

`dont_ask` 是非交互 fail-closed，不是自动批准。若任务需要写文件或运行命令，必须由用户在
本机配置中显式选择更宽的 Qoder permission mode，并把该配置视为执行 authority；不要把
真实 token 写进 TOML。

### Grok Build CLI

Grok Build 当前也通过 direct JSON adapter 接入。先在 MultiNexus 外完成登录并检查当前模型：

```bash
command -v grok
grok --version
grok models
grok inspect
```

`grok inspect` 会显示当前目录实际加载的 rules、skills、plugins、MCP 和 permissions；它不一定
是 bare runtime。最小配置：

```toml
[[agents]]
id = "grok"
adapter = "grok"
display_name = "Grok"
token_env = "DISCORD_GROK_TOKEN"
work_dir = "."
grok_bin = "/absolute/path/to/grok"
model = "<model-id-from-grok-models>"
grok_reasoning_effort = "high"
grok_permission_mode = "dontAsk"
```

adapter 固定添加 `--no-memory`，避免 provider-native 跨 session memory 污染其他项目；它不会
关闭 Grok 的内部 subagent。默认 `dontAsk` fail-closed，放宽 permission 同样必须是本地显式
决策。Grok 的 JSON 可能包含 `thought`，MultiNexus 只读取最终 `text`、session、stop reason
和 bounded model evidence，不保存或转发私有思考。

Qoder/Grok 当前版本没有 stdio ACP server，因此不能只把它们的普通 CLI command 填进
`acp_command`。未来 provider 提供稳定 ACP 后，再按通用 ACP 路径逐个迁移验证。

### 通用 ACP v1 Agent

MultiNexus 可以把任意提供 stdio ACP v1 server 的 CLI 作为 `adapter = "acp"` 接入，而不为
每个 provider 新增专属 adapter：

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

配置规则：

- `acp_command` 使用宿主机上的绝对 executable 路径；
- `acp_args` 只放启动 ACP server 所需参数，不放 token；
- provider 登录在 MultiNexus 外人工完成；Kimi Code 示例为 `kimi acp --login`；
- health check 只检查 executable 是否可解析，不连接 provider，也不回显参数；
- adapter 固定协商 ACP v1，并只按 provider 声明的 capability 执行 resume/load；
- 默认不声明 filesystem、terminal、terminal-auth capability，并拒绝所有 permission request。

最后一条意味着当前 ACP 以安全文本通信为目标，不自动授权 coding tools。需要工具执行时继续
使用现有 direct adapter；后续只有在具备显式 allowlist 与独立审查后，才会增加 ACP permission
policy。

只读检查：

```bash
command -v kimi
kimi --version
kimi acp --help
```

如果握手成功但返回 `(no response)`，先用 provider 自己的非交互命令检查登录和额度。例如：

```bash
kimi -p '只回复 OK' --output-format text
```

MultiNexus 不会把 provider 的 raw stderr、启动参数或私有 thought 转发到 Discord；因此
provider 额度/认证错误应先在 provider CLI 中诊断。

> 以上 Qoder、Grok Build 与通用 ACP v1 均不在 `python -m multinexus.setup` 的 Standalone
> 向导选项内；手工配置后统一运行 `python -m multinexus.setup --check` 做只读检查。

### 本地 LLM (LM Studio)

1. 下载 [LM Studio](https://lmstudio.ai/)
2. 在 Discover 标签页下载模型
3. 前往 **Local Server** → 点击 **Start Server**
4. 默认端口是 1234

### 本地 LLM (Ollama)

```bash
# Mac/Linux：
curl -fsSL https://ollama.com/install.sh | sh
ollama pull llama3
ollama serve

# Windows：从 ollama.com 下载安装程序
```

---

## 步骤 5：运行

```bash
python multinexus.py --platform discord --config agents.toml
```

首次运行时，bot 会：
- 创建 `data/` 目录
- 创建 SQLite 数据库
- 与 Discord 同步 slash 命令（全球传播可能需要长达一小时）

---

## 持久运行（可选）

本仓库默认只提供前台入口。长期运行由你选择的 service manager 处理，例如 systemd、launchd、PM2 或 Windows Task Scheduler。

一个最小 systemd user service 示例（需自行调整路径）：

```ini
# ~/.config/systemd/user/multinexus-discord-bridge.service
[Unit]
Description=MultiNexus Discord bridge

[Service]
Type=simple
WorkingDirectory=/path/to/multinexus
ExecStart=/path/to/multinexus/.venv/bin/python multinexus.py --platform discord --config agents.toml
Restart=on-failure
RestartSec=30

[Install]
WantedBy=default.target
```

launchd、PM2、Task Scheduler 等类似。生产级脚本、SSH 部署、定时任务和 host-specific service 文件不属于本公开仓库范围。

---

## 私有 Wiki / DB 设置

`services/wiki.py` 支持把私有 wiki 页面写到 `wiki/private/`，该目录已 gitignore。
当前 Discord bridge 尚未接入 `WikiStore`，不会自动创建或维护这些页面；使用前需由
集成方显式实例化并完成权限 wiring。

`PRIVATE_DB_PATH` 控制 `washer.py` 使用的*私有 SQLite 数据库*（私有记忆审查队列）
位置。运行 `washer.py` 时该变量是必需的，不会默认为仓库内文件。

要将私有 DB 存储在自定义位置（例如，在任何同步文件夹之外）：

```
# .env
PRIVATE_DB_PATH=/home/you/.private/multinexus/private.db
```

路径必须位于仓库之外，且父目录名必须为 `multinexus`；Windows 上会尝试使用
`icacls` 收紧文件权限。

---

## 故障排除

### Bot 在线但不响应

- 检查 Discord Developer Portal 中是否启用了 **Message Content Intent**
- 验证频道 ID 是否在 `agents.toml` 的 `channels` 中
- 检查 bot 是否有权限读取该频道中的消息

### Agent 离线

- 在 Discord 中运行 `health`（文本命令）检查 agent 健康
- 检查 bot 日志：`logs/` 目录或你配置的日志路径
- 对于 CLI agent：验证 `claude --version` 或 `codex --version` 在相同环境中工作
- 对于本地 LLM：验证服务器正在运行且模型已加载

### Windows：agent 运行时出现控制台窗口

正常操作中不应发生这种情况。如果发生，验证 adapter 层（`multinexus/adapters/`）
是否以 `_NO_WINDOW` 标志加载 CLI agent（在 `agents/cli.py` 中设置）。此标志仅在 `sys.platform == "win32"` 时应用。

### 限流回退不工作

回退链需要配置多个 agent 并在线。
检查 `agents`（文本命令）以查看哪些 agent 可用。

## Runtime HTTP 与 v0.2.0 升级

先安装 Coordinate v0.4.0；使用 HTTP 时按 Coordinate 文档安装 `runtime-http` extra，配置 listener 与 client role。agentd 启动会校验 `contract_version=1` 与 `claim_fencing`、`agent_reconcile`、`managed_lease`、`terminal_report` 四项能力。版本字符串本身不构成能力证明；旧服务或缺少能力时启动失败且不会 claim。operator recoverable claim 仍只支持具备对应能力的 CLI。

在本机 TOML `[defaults]` 或单个 agent 小节中配置：

```toml
agentd_mode = true
coordinate_transport = "http"
coordinate_http_base_url = "http://127.0.0.1:8765"
coordinate_http_client_id = "<client-id>"
coordinate_http_token_file = "/absolute/private/path/runtime-token"
```

HTTP URL 必须是数字 loopback 地址且包含端口，不能用 DNS、远程地址、userinfo、query 或非根路径。跨主机使用经过认证的 SSH local-forward。桥与 agentd 需要各自对应的 bridge/agentd role credential，不应共用一个身份。token 放在显式绝对路径的普通非 symlink 文件中；POSIX 不得 world-readable 或 group/world-writable，Windows 校验可信账户 DACL。不会把 token 放在 TOML、argv 或日志中。HTTP client 不读取服务器 DB；CLI 使用本机绝对 `coordinator_db_path`，同一进程只使用一个 transport。

服务管理器可以设置 `MULTINEXUS_COORDINATE_TRANSPORT`、`MULTINEXUS_COORDINATE_HTTP_BASE_URL`、`MULTINEXUS_COORDINATE_HTTP_CLIENT_ID`、`MULTINEXUS_COORDINATE_HTTP_TOKEN_FILE`。这四项只接受真实 process environment 覆盖，共享 `.env` 不具备该 authority。ZCode 的三个 service override 见 [权限说明](zcode-permissions.md)。

升级次序：先验证 Coordinate 能力和 managed Discord channel binding；停止访问本地 context/session DB 的进程并备份 SQLite；升级 MultiNexus；以原配置的单一 transport 启动 agentd，再启动 bridge。managed Discord 消息和 slash-command 准入使用 Coordinate binding，standalone 仍使用静态 `channels`，`allowed_user_ids` 仍限制 Operator 命令。

首次打开本地库保留旧行并新增 sessions 的 `context_generation`、`context_cursor_order_token`、`context_cursor_message_id` 和 context 的 `runtime_reply_outbox`。cursor 身份不完整时回退完整历史，不猜测增量已消费。旧版不理解 cursor/outbox，回退前应核对并处理待投递记录，不能保证直接降级不丢失或重复消息；恢复升级前备份会丢弃之后的记录。不要手工把 outbox 标记为已投递。

可选 `MULTINEXUS_AGENTD_HEALTH_FILE` 输出本地健康 JSON（父目录和权限由 Operator 配置）；它描述进程观测，不能替代 Coordinate job/lease authority。`latched` 表示停止新 claim，不能仅靠进程 PID 宣称 ready。权限不确定时先核对 job/lease 和旧进程，再按 Coordinate recovery 契约处理。
