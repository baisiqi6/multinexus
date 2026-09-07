# ZCode 原生逐次权限路线

默认 `zcode_transport = "headless"` 保留现有 `--prompt --json` 行为。受控编辑和测试需要明确启用 `app-server`。此路线固定到已审查 ZCode 0.16.5 CJS SHA-256 `e9f1868c0fdb863537ed910ee3828b9be96b8c2fd805473f63b439e1113266b8`；升级 bundle 后会拒绝启动，需要重新核验协议。

## 配置与启动边界

可以在 ZCode agent 的 TOML 小节配置：

```toml
zcode_transport = "app-server"
zcode_context_root = "/Users/<worker>/Library/Application Support/MultiNexus/zcode-contexts"
zcode_permission_commands = ["python -m unittest discover -s tests"]
zcode_permission_mode = "build"
```

`zcode_context_root` 必须是 workspace 外的显式绝对本地路径，父目录需已存在。客户端新建的根目录及子目录采用 owner-only 权限；既存不安全目录会被拒绝。Windows 使用当前服务身份的 owner-only ACL，并拒绝 junction/reparse；不能使用 UNC、设备路径或 alternate data stream。不要把 context root 设为工作目录的祖先或子目录。

为避免修改共享 `agents.toml`，可通过该 ZCode service 的私有 process environment 覆盖：

| 环境变量 | 值 |
|---|---|
| `MULTINEXUS_ZCODE_TRANSPORT` | `headless` 或 `app-server` |
| `MULTINEXUS_ZCODE_CONTEXT_ROOT` | 显式绝对目录 |
| `MULTINEXUS_ZCODE_PERMISSION_COMMANDS` | 严格 JSON string array，例如 `["python -m unittest discover -s tests"]` |

共享 `.env` 无权注入这三个覆盖项。命令列表保留完整字符串，不按逗号拆分，不隐式去掉空格；空列表拒绝所有 Bash 权限请求。模型 prompt、工具 input 和回复不能更改此策略。未知 transport、错误类型、非 build mode 或未知配置结构会明确失败。

app-server 当前只支持普通任务文本，不支持 slash/custom commands。客户端对最终发送文本按 JavaScript trimStart 的空白/BOM 规则检查：以 `/` 开头则在创建 context、读取 provider 配置或启动进程之前拒绝，cold resume 也一样。正文中普通 slash 不会被误拦。原因是 vendor 的自定义命令可在模型 turn 和权限 broker 之前展开 shell；这条路线不承担命令控制面，headless 行为保持不变。

Mac 可继续直接使用 executable CJS 的 `zcode_bin`，不要求新增 `zcode_node_bin`。Windows 继续使用 `zcode_node_bin` + `zcode_bin` 两个独立 argv。app-server 路线会查找实际 Node 并验证 child 的 native homedir 和独立 DB 路径；不会修改用户 shell 的 HOME。

## Provider 配置及私有 session

原 `zcode_home_dir` 在这条路线中是 **worker provider 配置源**，不再用作 native runtime HOME；未配置时从当前宿主用户 home 读取源 `.zcode/cli/config.json`。只支持 top-level `model` 为 `provider/model` 字符串、`provider` 为映射的已知格式。当前选中 provider 必须 enabled，kind 为 `anthropic` / `openai` / `openai-compatible`，options 仅包含显式 `apiKey` 与 `baseURL`。额外认证 headers、model.options、复杂 reasoning/providerOptionsByLevel 等未知结构会被拒绝，不会换模型或复制整份源配置。

仅当前选中模型的 `limit.context/output`、`modalities.input`、`reasoning` 的 boolean 或 `enabled/levels/defaultLevel` 被保留；desktop 配置的 `enabled/variants/defaultVariant` 仅按同名档位转换为该结构，混合两种字段或默认档位不在列表内会拒绝；其他模型、`zcode` UI metadata、任意 extras 均不进入 child 配置。客户端用 native snapshot 的 provider/model 校验源选择，并将观测来源标为 `native_snapshot.settings.model.current`；这证明原生 runtime 的选择，不证明实际下游 provider 没有转发或 fallback。

每个逻辑 session 都在私有根下新建独立 home/config/storage/空 session DB。固定隔离配置关闭 hooks、plugins、MCP、subagent、skills、memory、自动问答与 native search，且 `allowedTools=[]`。不会传 `toolAllowlist`，因为 vendor 会把它也解释为自动批准列表；`mcpServers=[]` 也不能用来禁用继承 MCP。

冷恢复通过原生 `session/resume.runtimeModel` 为新进程重建同一 provider/model 的临时目录。仅复用前述受限提取的静态 apiKey、baseURL、kind 和模型 metadata；apiKey 只进入本地 stdin 与原生进程内存，不写入 locator、结果或日志，也不读取额外认证来源。推理配置须能明确映射到原生 enabled/levels/defaultLevel，含糊的缺省配置会拒绝冷恢复。原生模型仍不可用时直接失败，不切换模型，也不假报 `headersApplied=true`。

派生 credential config 以私有权限写入，子进程退出、失败、超时或取消后移除，源文件不变。DB、无凭据 context binding、session locator 和有限 permission evidence 保留，供精确恢复与审查；locator 不记录 Harness task acceptance。运行中每个 context 有排他 claim；并发恢复、unknown session、workspace/provider/policy 变化、配置漂移、linked locator/DB 均失败，不自动创建替代 session。异常进程崩溃留下的 claim/credential config 需 Operator 先确认无存活进程后处理，客户端不会猜测并解锁。

## 权限与结果

- `Write` / `Edit` 只允许明确 schema 和 work_dir 内的实际文件目标。拒绝越界、symlink、junction/reparse、hardlink，以及 `.git`、`zcode.json`、`.zcode` 配置路径。启动及每次授权时按 vendor 规则检查工作目录到最近 Git root 的项目配置，发现即拒绝此路线；不会删除用户配置。
- `Bash` 只允许 Operator 列出的逐字相同完整 command，cwd 必须等于 work_dir。只支持已知 command/description/cwd/timeout/run_in_background 字段；禁止后台执行和未知参数。修改参数、前后缀、额外拼接或重定向导致命令不再匹配时拒绝。
- 收到的未知工具权限请求被拒绝。vendor 的普通只读和低风险 session 操作可能不产生 permission callback，因此这不是 OS sandbox，也不是所有工具调用的全面审计。Owner 授权的测试程序本身可以有其他副作用；精确字符串匹配不约束其全部行为。
- permission 按 `sessionId/requestId` 只决策一次；相同重发会答复当前 wire id，变化重发或未知 callback 会停止 owned process group。回应仅 `decision/reason`，不发送 `permissionUpdates` 或持久化批准。
- `session/send` 的 accepted 和 `state.updated` 的 `prompt_completed` 都不代表执行完成。只有本次 inputId 的 turn.started 建立 sessionId/turnId 绑定后，匹配的 terminal `response/resultType` 才可生成结果；随后通过 `session/read` 确认同一会话已处理该终态、没有活跃 turn 或待处理权限，成功结果要求 idle，再关闭进程并检查正常退出。取消、错误、提前 EOF、无终态、错配和超时不会成为成功；不重放不确定是否已执行的 prompt。不解析或保存 reasoning。

`provider_evidence.permission_decisions` 与 context 内 `permissions-input_*.json` 保存决策和输入摘要（编辑内容用 SHA-256，测试命令保留完整字符串并做已知凭据替换）。结果和进度不输出 credential config、原始 transcript 或 provider error payload。一次 native turn success 与 Harness task acceptance 仍是不同事实。

## 验证与回退

测试覆盖协议交错、去重/冲突、权限路径、session 恢复、配置漂移、失败/超时/取消、凭据清理和脱敏。部署到自己的环境后，应分别验证实际 provider 配置、前台运行和服务账户运行；macOS 测试不能替代 Windows 服务身份验证，fake native 测试也不能证明 provider 可用。

升级后重启使用 ZCode adapter 的进程；若修改共享配置，逐一核对所有读取该配置的消费者并按本地运维策略重新加载。关闭 `MULTINEXUS_ZCODE_TRANSPORT` opt-in 或设回 `headless` 即恢复原路线；这不会让 headless 自动具备受控写入能力。不要直接改 vendor DB、用户权限规则或切换 yolo/edit 作为回退。

Windows 原生保存配置可能继承父目录的 ACL。仅文件 owner 为当前进程账户、所有有效 ACE 只授权该账户（GetFileSecurityW 可能不返回继承 ID 标记），且直接父目录仍通过受保护的 owner-only ACL 校验时接受；目录权限仍必须受保护。默认 owner 为 Administrators 组的父进程通过下文的独立 launcher 处理；直接创建的既存错误 owner 文件仍拒绝，不自动修复。


原生 build mode 会自动允许部分低风险 Bash（例如 echo）。本客户端在任何模型输入前，向自己创建的私有 session DB 写入唯一的 `ask Bash` 限制，使这些命令也必须经过逐次回调；只允许与受保护配置完全相等的测试命令。此适配固定到已审计 bundle SHA，仅操作自身 context 的 `local_setting` 单行，不触及用户 ZCode DB、Coordinate DB或 legacy permission 表，不持久化 allow 授权。用户 hooks 继续关闭。

该 bundle 首次输入时才持久化 session，因此初始化仅接受空 session/permission/ruleset baseline，并从已验证 workspace 按固定原生算法派生 project id；写入采用 IMMEDIATE 事务和 FULL 同步，再由新连接读回。冷恢复必须已存在同一 session、workspace 与精确限制规则，不能补造；policy v2 明确拒绝旧 locator。异常、SQL锁、额外权限行/授权或内容漂移均拒绝，终态证据含规则 SHA 与 session/project 绑定。vendor 升级必须重新审查此兼容层。

Windows 的 context 根目录应保持较短；原生文件 API 在 275 字符的权限审计临时路径上已实测失败。Bash 工具使用原生 Bash 命令语法，配置 Windows 测试命令时应使用 forward slash 路径和 POSIX quoting；服务 registry 自身的 argv 编码仍按 Windows 处理。权限回调不能约束已批准测试程序自身的全部副作用，批准的程序应视为受信代码。

Windows app-server 使用独立 Python 启动入口，并以 `-I` 隔离导入路径。该子进程先将自己的默认 TokenOwner 设置并核验为已有 TokenUser SID，再启动原生程序，覆盖 NSSM LocalSystem 默认 owner 为 Administrators 的情形；不修改 agentd 主进程 token、服务账户、privileges 或 DACL。Windows 的私有 SQLite 初始化和核验也在这种有界子进程内执行，因为只读 WAL 连接也可能创建辅助文件。数据库操作仍执行既有 schema、owner、规则和 session 绑定检查；不修复旧 context、不放宽 ACL，单次子进程限时 5 秒。原生入口保留字节流和退出码，等待 native 退出后才结束，既有进程树终止覆盖其子进程。owner 设置或数据库操作失败时不会发送模型输入。
