# Agent Adapters

当前 Discord bridge 和 `agentd` 通过 `multinexus/adapters/factory.py` 支持五类
managed adapter：

| `adapter` | Executor | 会话恢复 |
|---|---|---|
| `claude` | Claude Code CLI | 支持 |
| `codex` | Codex CLI | 支持 |
| `opencode` | OpenCode CLI | 支持 |
| `omp` | Oh My Pi CLI | 支持 |
| `hermes` | Hermes CLI | 当前为 one-shot |

每个 `[[agents]]` 条目代表一个独立的平台 bot 身份；`token_env` 指向该身份的
Discord token 环境变量。只配置已安装并已认证的 executor。

## 通用示例

```toml
[[agents]]
id = "host-claude"
adapter = "claude"
display_name = "Claude"
aliases = ["Claude"]
token_env = "DISCORD_CLAUDE_TOKEN"
work_dir = "."
timeout = 360
claude_bin = "claude"
```

`system_prompt`、timeout、context 与 Coordinate 字段可放在 `[defaults]`，并由
agent 条目覆盖。完整最小模板见仓库根目录 `agents.toml.example`。

## Executor-specific 字段

### Claude Code

```toml
adapter = "claude"
claude_bin = "claude"
claude_dangerously_skip_permissions = false
```

通过 stdin 调用 CLI，并从 provider-native JSON/stream 事件提取公开进度、结果和
session id。权限模式由本机 Claude Code 配置与上面的显式开关共同决定。

### Codex

```toml
adapter = "codex"
codex_bin = "codex"
codex_sandbox = "workspace-write"
codex_dangerously_bypass_approvals_and_sandbox = false
# codex_fallback_model = "<model>"
```

### OpenCode

```toml
adapter = "opencode"
opencode_bin = "opencode"
opencode_dangerously_skip_permissions = false
# model = "<provider/model>"
```

### OMP

```toml
adapter = "omp"
omp_bin = "omp"
omp_auto_approve = false
# omp_model = "<provider/model>"
# omp_thinking = "high"
```

`omp_auto_approve` 会扩大 executor 权限；公开模板不应默认启用，生产配置应由
Operator 结合 workspace sandbox 与任务风险决定。

### Hermes

```toml
adapter = "hermes"
hermes_bin = "hermes"
# model = "<model>"
# hermes_provider = "<provider>"
# hermes_toolsets = "<toolsets>"
hermes_accept_hooks = false
```

## External agents

`[[external_agents]]` 只提供提及/路由身份；MultiNexus 不启动其 runtime，也不为其
持有 session。外部 OpenClaw、Hermes gateway 等可以通过自己的平台身份参与。

```toml
[[external_agents]]
id = "external-hermes"
display_name = "Hermes"
aliases = ["Hermes"]
discord_user_id = 100000000000000001
```

## `agents/` 兼容组件

仓库顶层 `agents/` 仍包含旧版 `ClaudeAgent`、`CodexAgent`、`LocalLLMAgent`、
`OpenClawRelayAgent` 与 `ResearcherAgent` library。当前 `multinexus.py` bridge 不会
从该包加载 adapter，也不会自动解析其 research/wiki 标签。它们属于兼容/复用
组件，不应写进当前 bridge 的 capability list。

## 添加自定义 adapter

1. 在 `multinexus/adapters/` 新建继承 `AgentAdapter` 的实现；
2. 返回 `AdapterResult`，实现 `call(...)`，需要恢复时实现 `resume(...)`；
3. 在 `multinexus/adapters/factory.py` 显式注册 adapter 名称；
4. 增加 subprocess ownership、timeout/cancellation、env filtering、session 与
   health-check 测试；
5. 对会产生外部副作用的权限默认 fail closed。

不要仅在 `routing/dispatcher.py` 添加名字；当前 bridge 的真实加载 authority 是
`multinexus/adapters/factory.py`。
