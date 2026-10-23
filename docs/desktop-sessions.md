# 原生桌面会话通信

此入口显式连接两个已存在的 Mac 桌面会话。普通 `multinexus.channel_cli read/send/wait`
继续只执行频道 I/O；调用 `multinexus.desktop.cli send` 才在确认频道 delivery 后提交既有
Coordinate runtime job。专用 `desktop-native` agentd 使用既有 native inbox，不启动新的
CLI Agent、替代聊天、Bot、订阅器、后台频道轮询器、调度器或第二套状态数据库。

代码与隔离测试不等于生产采用或原生验收。首轮范围为 macOS，DSH Host 针对
`0.2.1-rc.2`；Windows 原生 adapter 尚不支持。安装、服务配置与原会话验收应分别保留证据。

## 交付和完成含义

```text
desktop send → channel_send confirmed sent → flat runtime_request_submit
  → dedicated capacity=1 worker → fenced durable native_enqueue_started
  → exact original native inbox → recipient reply via active Unix endpoint
  → trusted recipient channel_send confirmed sent → receiver_acknowledged
```

runtime key 固定为 `desktop-delivery-v1:<channel-delivery-id>`。target、binding、原始正文和
origin 都是精确重放参数，不能通过换 recipient 给同一 delivery 再造 job。同一 key 改 target
必须冲突。通信 job 不带 `task_id`、persona 或 logical session，origin/reply 的 platform 都是
`none`，destination 非空，origin 另带显式 `session_scope_id`。因此 runtime 不会重复发布最终
正文；可信 reply 是独立的 channel delivery，使用 `desktop-reply-v1:<job_id>` 和原消息 reply-to。

`done` 的唯一条件是接收方回执经可信 worker 的渠道 SDK 确认为 `sent`，且身份
`verified`、`binding_current=true`、actual author 与接收方自己的预期 Bot 一致：
`result.desktop.state=receiver_acknowledged`。`native_turn_completed` 固定为 `false`；原生 queue
accepted、admitted 或模型 turn 完成都不能替代 ACK。终态 `text/response_text/summary` 只含
固定安全摘要；正文只作为 channel embed 和原生通信数据传递，不能变成审查/交接/生命周期指令。
Coordinate 仍会产生一个普通 `agent.reported`，这不授予任务决策权。

## 私有绑定与凭据角色

以下所有值都是占位示例。配置和 token 使用当前用户持有的普通文件、`0600`、绝对路径，
路径各级不得是 symlink。不要将真实 session locator、正文、token 或业务目录提交到 Git。
profile 绑定的 cwd 必须是原会话当前 cwd，原 session ID 必须由官方只读 inspect 精确核对；
不能靠标题搜索或另建会话替代。alias 可以自定，但两个 agent_id 和 session_id 各自唯一。

推荐名称是 **Agent Instance Binding（Agent 实例绑定）**；强调渠道归属时也可称
Agent Identity Binding。Agent 实例是稳定身份，session 是该实例当前绑定的具体会话，model
是原客户端的执行配置。三者不能互相替代。`adapter` 负责原生传输，`agentd` 负责 job/lease/
progress，identity binding 负责 own principal → canonical Agent → Bot 的归属。

每个原生实例只持有自己的 **version-2** profile。下面是 Codex 示例；DSH 使用另一个文件，
固定 `local_alias: "dsh"`、仅包含 `channel_profiles.dsh`，并引用 DSH 自己的 lifecycle 文件。
两者共享的只是非秘密 binding/expectation 元数据；每个实例都不包含对方 channel 配置路径。

```json
{
  "version": 2,
  "local_alias": "codex",
  "workspace_id": "project-a",
  "channel_id": "123456",
  "mcp_url": "https://control.example/mcp",
  "channel_profiles": {"codex": "/absolute/private/codex-channel.json"},
  "lifecycle_profile": "/absolute/private/codex-lifecycle.json",
  "sender_expectations": {
    "codex": {"client_id": "desktop-codex-channel", "agent_id": "mac-codex", "discord_user_id": "1001"},
    "dsh": {"client_id": "desktop-dsh-channel", "agent_id": "example-dsh-agent", "discord_user_id": "1002"}
  },
  "bindings": {
    "codex": {
      "kind": "codex",
      "agent_id": "desktop-codex",
      "session_id": "11111111-1111-1111-1111-111111111111",
      "cwd": "/absolute/original/shared-workspace",
      "revision": "binding-v2"
    },
    "dsh": {
      "kind": "dsh",
      "agent_id": "desktop-dsh",
      "session_id": "session-22222222-2222-2222-2222-222222222222",
      "cwd": "/absolute/original/shared-workspace",
      "revision": "binding-v2"
    }
  }
}
```

`1001/1002` 仅用于说明结构。实际 `sender_expectations` 必须从已有 canonical
`config/agent-registry.toml` 与已批准 MCP policy 核对生成，不能另维护一份可编辑 roster。
expectation 的 `agent_id` 是可见 Agent 身份；`bindings[].agent_id` 是专用 runtime executor，
例如 `desktop-dsh`，两者职责不同。keys 必须与 bindings 完全一致，每项严格为
`client_id/agent_id/discord_user_id`。新增 Agent 沿用相同 canonical registry、固定 principal、
own profile 模式；不增加 provider 专用身份框架。

每个实例的 channel config 只引用它自己的既有 MCP capability：

```json
{
  "workspace_id": "project-a",
  "channel_id": "123456",
  "client": "codex",
  "client_id": "desktop-codex-channel",
  "url": "https://control.example/mcp",
  "token_file": "/absolute/private/codex-channel.token",
  "tools": ["coordinate.channel_send"]
}
```

DSH 使用自己的 client_id/token_file；`--to` 只选接收方，不改变发送身份。可信 receiver
worker 读取自己的 channel config 完成 ACK，反向唤醒也使用 receiver 自己的 principal。
文件 scope 不是服务端授权证明；远端 principal 仍需 workspace、Discord、tool grant 和当前
channel binding。`coordinate.channel_read/wait` 可使用该实例既有的独立读取授权验收 author。

每个实例另有自己的薄 lifecycle principal，仅允许这两种 flat 调用，不复制 broad Operator
credential，也不借用 peer capability。Codex 示例（DSH 使用独立 client_id/token_file）：

```json
{
  "client_id": "desktop-codex-lifecycle",
  "url": "https://control.example/mcp",
  "token_file": "/absolute/private/codex-lifecycle.token",
  "workspace_ids": ["project-a"],
  "platforms": ["none"],
  "tools": ["coordinate.runtime_request_submit", "coordinate.runtime_job_get"]
}
```

真实远端授权也必须包含 workspace/none 和这两个工具。它是配置纪律，不是新 lifecycle
broker 或 API。channel 使用 `{input: ...}`，lifecycle 使用 flat 参数；endpoint 要求 HTTPS
（隔离本机测试可用 loopback HTTP），同一 profile 的 own channel/lifecycle URL 与 mcp_url 一致。

服务端 `McpPrincipal.channel_agent_id` 固定绑定该 channel client 到 canonical 可见 Agent；
调用参数不能选择 sender/Bot/token。服务启动由 `COORDINATE_CHANNEL_IDENTITIES_FILE` 指向一个
私有 JSON，严格形状为：

```json
{
  "version": 1,
  "registry_path": "/absolute/deployed/multinexus/config/agent-registry.toml",
  "credentials": {
    "mac-codex": {"token_file": "/absolute/service/secrets/codex.bot-token"},
    "example-dsh-agent": {"token_file": "/absolute/service/secrets/dsh.bot-token"}
  }
}
```

Bot token 只归服务持有，身份 config 不包含 Bot ID 或 inline token。registry 是已有 canonical
文件或已核验的部署产物；配置启动时冻结一个 generation，变更须受控 reload/restart。默认缺失
绑定/凭据、disabled Agent、错误 self-ID 均在新 transport 操作前失败，不回退共享 Bot。
每次操作的 history/bus 使用同一 token；先核验 `/users/@me` 的实际 Bot ID，再检查权限，POST
响应的 `author.id/bot` 必须匹配才记为 sent。POST 后 author 缺失/不符保持 unknown；同 key
只检查原记录，不能换 key 或自动补发。绑定漂移不改写历史归属；legacy delivery 保持
`legacy_unattributed`。同 Bot token 轮换或无关 registry 版本变化不改变历史 fingerprint。

`sender_identity`、`actual_author_bot_id` 和 desktop v2 origin 是由回执保留的关联证据；
origin 本身不是针对恶意已授权 lifecycle Operator 的新认证。独立平台验收还须由 receiver
自己的 read capability 读取实际消息 author。伪 HTTP 的本地测试不能证明真实 token、author、
服务 policy 或已安装 registry。可见 DiscordClient 的启动也必须在 canonical self-ID 与
`bot=true` 一致后才注册命令、开放 handler/router/recovery。

agentd runtime 凭据继续使用既有 CLI/SSH 或 HTTP worker 机制，与 channel、薄 lifecycle、
Host token 分开。Host 只读取自己 v2 profile 的本地 DSH binding 和自身 Host token，不读取
channel/lifecycle token；请求 packet 只带 receiver 自己固定 profile 的路径和临时 ACK capability。
显式选择 `desktop-native` 时 loader 跳过共享 dotenv/Bot token；Codex 三个子进程只继承已列明
的 OS/App 路径与 locale 环境，不带渠道、runtime、provider 凭据或 model/access override。
`0600`、固定入口与环境过滤防止误用，不提供对抗同 UID 非沙箱进程的 OS 隔离。

旧 v1 多能力 profile、v1 job/ACK 仅保留为历史/离线检查材料；新 send/enqueue/ACK 必须拒绝，
不推断 local_alias、不改写旧记录、不自动迁移。已有 latch/不确定结果仍保留原 key 和 descriptor。
profile/wire origin/ACK packet 当前为 v2；Host config 与 Host Unix wire、progress descriptor/result
各自仍为 v1，`desktop-delivery-v1` / `desktop-reply-v1` key 也不变。各层版本独立，不能统一加一。

## 专用 worker 与 DSH Host

先按现有 [平台设置](platform-setup.md) 注册专用 agent/runner profile、typed executor
binding（adapter=`desktop-native`）与每 worker `max_concurrent_jobs=1`。这些是受信 Operator
配置，不能来自消息正文。不要改原有通用 worker、模型、权限或业务任务。

Coordinate 的 host identity 存在已注册 agent 行和 workspace host-profile 中；当前
`AgentConfig` **没有 `host_id` 字段**。`agentd_host` 是本地绑定地址，默认 `127.0.0.1`，不能
用它设置 Coordinate host。现有 `multinexus.agentd` launcher/worker 启动不会自动注册 agent，
也没有 hostname fallback。不要让外部 bootstrap/包装脚本重新注册到另一个默认 host。
在单独获准的配置采用中，用已有 Operator 注册入口显式传入同一个 host，例如：

```sh
/absolute/coordinate --db /absolute/coordinate.sqlite3 workspace host-profile set project-a \
  --host-id desktop-host --workspace-path /absolute/original/shared-workspace \
  --harness-root /absolute/original/shared-workspace/harness
/absolute/coordinate --db /absolute/coordinate.sqlite3 runtime agent register \
  --agent-id desktop-codex --host-id desktop-host --client-type agentd
/absolute/coordinate --db /absolute/coordinate.sqlite3 runtime agent register \
  --agent-id desktop-dsh --host-id desktop-host --client-type agentd
```

此处 host/路径都是占位值；先核对已有记录，只新增自己拥有的项。两 worker 注册后再同步
typed executor/capacity 目录。当前 desktop send 不接受 worktree/cwd 覆盖，所以 runtime 从
workspace+registered host 解析的 cwd 必须等于 recipient 的原 cwd；同 workspace/host 的两个
binding 必须符合该同一 host-profile，不能通过改原会话 cwd 来凑配置。不同则停止并核对绑定。

普通 agents TOML 的最小 DSH entry 示例：

```toml
[[agents]]
id = "desktop-dsh"
adapter = "desktop-native"
agentd_mode = true
desktop_profile_file = "/absolute/private/dsh-profile-v2.json"
desktop_recipient = "dsh"
desktop_runtime_dir = "/private/tmp"
desktop_host_socket = "/absolute/private/host/host.sock"
desktop_host_token_file = "/absolute/private/host-worker.token"
timeout = 120
coordinate_transport = "cli"
coordinator_cli_path = "/absolute/path/to/existing-coordinate-wrapper"
coordinator_db_path = "/absolute/path/to/coordinate.sqlite3"
```

复用本机现有 runtime transport 配置；远程 SQLite 路径只通过既有 wrapper 使用，不共享 DB。
HTTP worker 的字段见 [平台设置的 Runtime HTTP 配置](platform-setup.md#runtime-http-与-v020-升级)。
`desktop_profile_file`、`desktop_recipient` 必填；binding.agent_id 必须等于 entry.id。
`work_dir` 可省略，若填写必须精确等于 binding.cwd。`model`、`system_prompt` 必须为空或省略；
还需确认 defaults 没有继承这些覆盖。此 worker 不构造 SessionStore、不走 provider prompt/resume。

HTTP transport 的替代配置使用既有已认证 runtime listener；不要与 CLI 示例同时配置两套调用：

```toml
coordinate_transport = "http"
coordinate_http_base_url = "http://127.0.0.1:8765"
coordinate_http_client_id = "desktop-dsh-worker"
coordinate_http_token_file = "/absolute/private/desktop-dsh-runtime.token"
```

这四项也支持进程环境覆盖（优先于 TOML），例如在独立 service manager 中：

```sh
export MULTINEXUS_COORDINATE_TRANSPORT=http
export MULTINEXUS_COORDINATE_HTTP_BASE_URL=http://127.0.0.1:8765
export MULTINEXUS_COORDINATE_HTTP_CLIENT_ID=desktop-dsh-worker
export MULTINEXUS_COORDINATE_HTTP_TOKEN_FILE=/absolute/private/desktop-dsh-runtime.token
```

不得放进共享 `.env`；loader 会恢复这些字段的原进程环境。Codex worker 使用自己的
client_id/token_file，与 DSH worker 分开。HTTP 只允许 numeric loopback、显式端口、根路径且
无 userinfo/query/fragment；跨宿主机沿用已批准 SSH local-forward，不直接对外开放 runtime。
HTTP 不提供 recoverable claim，显式恢复保留已有 CLI/SSH 门禁。CLI transport 则需显式排除
旧进程中的这些 HTTP 环境覆盖；`coordinator_cli_path` 是可执行入口，不是含参数的命令字符串。

Codex entry 将 id/recipient 换为其固定绑定，省略 DSH Host 字段，并可设置
`desktop_codex_bin`（默认 `/Applications/ChatGPT.app/Contents/Resources/codex-cli/bin/codex`）。
它必须是支持 `queue --thread <original-UUID> --message <envelope>` 的绝对 App 二进制。
启动只检查 queue capability 和原 thread 只读 metadata，不执行 exec/resume 或 daemon bootstrap。
metadata 可读不能证明 Desktop 已加载或会实际处理 inbox。

`desktop_runtime_dir` 默认 `/private/tmp`，要求现有、canonical、无 symlink 的绝对目录，
路径最多 65 filesystem bytes；macOS `/tmp` 是 symlink，不能替代。worker 在其下创建 `0700`
临时子目录及 `0600` socket/packet，受 Mac Unix socket 长度约束，没有 TCP fallback。
这些能力文件是瞬时传输资料，不能作为恢复账本。文件模式隔离其他 UID，同 UID 代码属于信任域。

常规启动明确不启用 recoverable，并可使用既有 no-reap 入口：

```sh
/absolute/python -m multinexus.agentd --config /absolute/private/desktop-agents.toml \
  --agent desktop-dsh --reap-mode none --reap-reason desktop-dedicated-worker
```

DSH Host 私有默认配置位置为 `~/.config/multinexus/dsh-desktop-host.json`，严格四字段：

```json
{
  "version": 1,
  "profile_file": "/absolute/private/dsh-profile-v2.json",
  "socket_path": "/absolute/private/host/host.sock",
  "worker_token_file": "/absolute/private/host-worker.token"
}
```

socket 父目录先存在且为 owner-only `0700`，socket 为 `0600`。Host 拒绝已有节点/旧 socket，
不接管监听器，不自动清理未知文件。替换 inode 或清理冲突须人工检查，不能递归删除重试。
协议与已测来源见 [Host README](../integrations/dsh-desktop-host/README.md)。

Host 是 Desktop profile 的全局插件，但只接受 allowlist 中的原 DSH session，拒绝子 Agent。
采用必须从已审源码制作私有稳定 bundle 副本，保留代码字节，仅改 `cordis.patch.yml`：

```diff
       config:
-        enabled: false
+        enabled: true
```

保存源 commit、完整 diff、所有文件 hashes，并确认代码字节一致。仓库默认继续 disabled。
当前 Host 没有配置 UI；官方 bundle On/Enable now 只选择 bundle layer，row On 只改变 disabled，
都不会将 `config.enabled` 设为 true。不要直接编辑 raw Desktop profile，也不要增加配置框架。

存在旧 one-shot probe 时，先通过官方 Plugins 找到精确旧 package，执行 Off，刷新并核对
runtime 已停止；仅 saved Off 不足以证明停止。确认后才从官方 Add plugin 输入冻结私有副本路径，
核对 source/package/version，安装后按实际 UI 通过 Enable now/On 启用。restart-required、failed、
overridden 或不明确状态需停止后续步骤并保留证据。active 也不等于 socket ready，更不等于原生
验收；不得为通过检查自动重启、重新安装或恢复旧 probe。

## 发送、状态和原 cwd 下回复

安装现有可选 [requirements-channel-tools.txt](../requirements-channel-tools.txt) 后：

每个私有 launcher 固定自己的 profile；例如 Codex 文件内容：

```python
import sys
sys.path.insert(0, "/absolute/frozen/multinexus-source")
from multinexus.desktop.cli import bound_main
raise SystemExit(bound_main("/absolute/private/codex-profile-v2.json"))
```

DSH launcher 固定 `/absolute/private/dsh-profile-v2.json`。由已冻结 Python/源码环境调用，
原生 Agent 暴露的命令没有 `--profile`/`--client`，重复、equals 形式也会拒绝：

```sh
/absolute/frozen/python /absolute/private/codex-desktop.py send \
  --to dsh --send-key exchange-01 --text-file /absolute/private/message.txt
/absolute/frozen/python /absolute/private/codex-desktop.py status --job-id request:example
```

send 可用 `--text` 或 UTF-8 `--text-file` 二选一，可加 `--reply-to-message-id`，正文最多 1800
UTF-16 code units。它返回 safe channel receipt、job_id、status、job_created，不等待对方执行。
status 只返回 job_id/status。除 help 外 stdout 为单个 `{ok,data,error}` JSON；0 成功、1 业务/传输
错误、2 参数错误、130 取消。不自动分片、换 key 或重试；失败可能已有 channel delivery/job，
必须保留同一 send-key 和原文核查既有记录。

worker 实际入队的 envelope 会携带以下绝对命令；必须使用它提供的当前 packet 路径：

```sh
/absolute/frozen/python /absolute/frozen/multinexus/desktop/cli.py reply \
  --request-file /private/tmp/mn-example/request.json --text-file /absolute/private/reply.txt
```

该命令可直接从原业务 cwd 运行，无需改变 cwd、PYTHONPATH 或全局 console script。采用前应固定
Python 与源码路径，在活动请求期间保留它们。reply 只向当前 worker Unix endpoint 提交受限
text/scope/capability；只有 worker 的渠道发送确认才形成 ACK。reply text 同样限 1800 UTF-16 units。

自动反向唤醒是单独、显式选择：在上述 reply 命令末尾加 `--wake-source`。CLI 使用 recipient
固定 channel 身份和同一 `desktop-reply-v1:<job_id>` 重放 SDK delivery，再将别名反向提交
runtime job；立即返回反向 job 状态，不等待，也不自行继续对话循环。

ACK 与反向 submit 是两个步骤。若后者失败，JSON 返回 `reply_acknowledged: true`、
`reverse_status: "unknown"`、原 `reverse_send_key`，错误为 `delivery_uncertain`；不能把它当作
原 reply 未发送。旧 packet 可能已删除，重复 stale reply 命令不是恢复方式。人工核查后可用显式
desktop send，使用 receiver 自己固定的 launcher、将 `--to` 指向原 source，保留相同 reply 原文、`desktop-reply-v1:<original-job-id>`
send-key 和原 incoming message 的 reply-to id。不能换 key 或凭空认定反向 job 未创建。

## 不确定结果与原生边界

没有 ACK、原生调用异常或观察超时均为 `timed_out`、`error_category=timeout`、
`timeout.kind=native_delivery_uncertain`。durable latch 是在当前 attempt/lease 下 await 并精确
回读的原始 descriptor，必须先于 native queue。已有、无效、截断 progress 或 reclaimed attempt
都不能重新入队。普通 agentd 不带 `--recoverable`，也不自动消费这种 timed_out job。

已有有效 descriptor 的显式 Operator 恢复可沿现有 `--recoverable --recovery-reason ...
--prior-process-stopped` 门禁执行；先停止原 worker/自动重启并确认旧进程已停止，再获取新 fence。
恢复只注册当前观察范围、查询原 native_request_id 并结束自己的观察；保留原 descriptor、
session/context 和内容摘要，不 fresh、不 reenqueue、不接管冲突旧 scope。
`resume_allowed=true` 是允许这一受限观察的元数据，不是重新执行原生 turn 的授权。

worker 退出、lease loss 或 timeout 会使旧 ACK capability 失效；即使原模型后来完成，也可能永远
保持 unknown。不会重新启用旧 capability 或从 native completion 推断 ACK。lease loss 只停止本次
观察，不能取消原生 turn；已发往渠道的在途请求无法召回，迟到结果不能设置 ACK/成功。

DSH 使用现有 sessionController prompt 与只读 inspect，不调用可能激活 cold session 的 follow。
loaded/idle 是首轮受控验收目标；busy、unknown、reload 必须保存原请求与 turn 的实际关联，
不能把另一个 busy turn 的完成当成功。cold/notLoaded、Interrupted、App closed、重连后效果及
Windows 保持未验收，不以人工点击/读消息/等待命令补起接收方后声称原生自动往返。

没有提供模型/权限/cwd 覆盖入口。绑定和 inspect 的 metadata 能证明精确原 session/cwd，不能
独立证明原客户端实际生效的模型、权限或执行策略；这些仍需原生客户端/实际执行证据。

## 隔离契约回归

`tests/test_desktop_integration.py` 可选依赖完整公开 Coordinate 0.6.0 源码，固定 commit
`3d94bad10e367ac68c349acd62c17e9085cc845a`。独立 archive 根目录需要
`.source-provenance.json` 的 `commit` 字段为该值；显式传错来源会失败，不回退到别的安装。

```sh
COORDINATE_CANONICAL_ROOT=/absolute/coordinate-canonical-archive \
  PYTHONPATH=/absolute/coordinate-canonical-archive/src:/absolute/multinexus \
  PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -B -m pytest tests/test_desktop_integration.py -q
node --test integrations/dsh-desktop-host/host.test.js
```

缺省未设置该环境变量时跨仓库模块会明确 skip，不能把这种运行当集成验收。
运行整个 Python suite 时也保留相同 `COORDINATE_CANONICAL_ROOT` 和 `PYTHONPATH`；后者让既有
persona/Coordinate 集成测试启动的 CLI 子进程同样导入固定源码，避免仅父进程可导入的环境差异。
临时 DB 使用公开 typed catalog/capacity/lease API；实际执行 ChannelMessageService、
ChannelIdentityResolver、ChannelSender、DiscordBus 与两个已绑定 McpPrincipal。仅替换外部 HTTP、
MCP 网络 seam 与 native 端点，执行实际 submit/claim/progress/report、ACK、终态打包和 parser。
验证两种 POST Authorization/self-ID/author、receiver-own read、reverse own lifecycle、unknown
重放、legacy 原始行完整保留和 lease fencing；不证明真实 Discord 或原生 App。
独立原始 approve/reject 阳性对照证明 parser 可产生 review 事件；安全路径检验它不会这样做。
handoff/lifecycle 的无副作用只针对这条实际执行链，不代表未启动业务 daemon 的全局验证。
本地 facade 测试不证明 Remote MCP principal 的真实授权，也不证明任何安装、原生往返或业务验收。

历史 one-shot/native probe 与旧 v1 candidate 的既有报告保持历史事实；本次身份修正不重跑、
重写或采用它们。旧共享身份 candidate 不具备本契约的采用资格；新源码通过 review 仍需单独
批准 credential/config 安装、服务重启及原桌面双方的实际 author-ID/原生往返验收。
