# Agent Adapters

当前 Discord bridge 和 `agentd` 通过 `multinexus/adapters/factory.py` 支持八类
managed adapter：

| `adapter` | Executor | 会话恢复 |
|---|---|---|
| `claude` | Claude Code CLI | 支持 |
| `codex` | Codex CLI | 支持 |
| `opencode` | OpenCode CLI | 支持 |
| `omp` | Oh My Pi CLI | 支持 |
| `hermes` | Hermes CLI | 当前为 one-shot |
| `acp` | 任意 stdio ACP v1 agent server | 支持 |
| `qoder` | Qoder CLI direct JSON 子进程 | 支持 |
| `grok` | Grok Build CLI direct JSON 子进程 | 支持 |

每个 `[[agents]]` 条目代表一个独立的平台 bot 身份；`token_env` 指向该身份的
Discord token 环境变量。只配置已安装并已认证的 executor。

所有 adapter 都返回同一个 `AdapterResult`。`text` 只承载用户可见结果或安全失败摘要；机器终态由
`outcome`（`success` / `failed` / `timed_out`）和稳定的 `error_category` 决定，不依赖文案前缀。
`diagnostic` 是最多 4096 UTF-8 bytes 的内部诊断，不自动成为 Discord 回复。旧第三方 adapter 的
`AdapterResult(text=...)` 仍受兼容，但新失败路径应显式写入 outcome/category，不应建立第二套 result/status 模型。

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

## 通用 ACP v1 adapter

`ACPAdapter` 保留现有 `AgentAdapter` / `AdapterResult` 抽象，只把 ACP 当作一个统一的
transport implementation；现有 direct adapters 不会因此删除。

```toml
adapter = "acp"
acp_command = "/absolute/path/to/acp-cli"
acp_args = ["acp"]
```

当前默认 permission policy 是 deny，且不向 provider 声明 filesystem、terminal 或
terminal-auth capability。它适合文本通信与 session/resume 验证；coding tool
authorization 尚未开放。最小配置、登录与故障排查见
[`platform-setup.md#通用-acp-v1-agent`](platform-setup.md#通用-acp-v1-agent)。

## Qoder 与 Grok Build direct adapters

当前 Qoder 1.1.x 与 Grok Build 0.2.x CLI 没有暴露稳定的 stdio ACP server，所以两者暂时走
direct adapter：

- `QoderAdapter` 使用 `qodercli -p --output-format json`；
- `GrokAdapter` 使用 `grok --single ... --output-format json`；
- fresh turn 返回 provider session ID，后续通过显式 `--resume <session-id>` 恢复；
- 两者都返回统一的 `AdapterResult`，上层 session、agentd 和 bridge 不需要 provider 分支；
- 默认 permission mode 为 fail-closed，只有用户在 `agents.toml` 显式修改时才放宽；
- Grok provider-native memory 默认关闭（固定 `--no-memory`），避免跨项目污染，但不关闭
  其内部 subagent 能力；
- Grok JSON 中可能出现的 `thought` 字段会被忽略，不进入结果、进度、metadata 或日志。

```toml
adapter = "qoder"
qoder_bin = "qodercli"
qoder_permission_mode = "dont_ask"
```

```toml
adapter = "grok"
grok_bin = "grok"
grok_permission_mode = "dontAsk"
```

这不是另起一套协议。`AgentAdapter` 是稳定端口，ACP 与 direct adapters 是可替换的 transport
implementation。未来 provider 真正暴露可靠的 ACP server 时，可以逐个验证迁移；direct
adapter 继续作为兼容与后备路径。

完整配置见
[`platform-setup.md#qoder-cli`](platform-setup.md#qoder-cli) 和
[`platform-setup.md#grok-build-cli`](platform-setup.md#grok-build-cli)。

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
