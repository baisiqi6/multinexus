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

Coordinate-managed 运行需要另行设置 `coordinator_cli_path` 和 `agentd_mode = true`，并在每个 agent 主机上运行 `python -m multinexus.agentd --agent <id>`。

### Coordinate-managed 本地 no-send 验证

先按 [Coordinate README](https://github.com/baisiqi6/coordinate) 在本机完成 fresh install。
Coordinate-managed profile 的三个关键值是：

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
