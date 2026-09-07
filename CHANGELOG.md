# 变更日志

multinexus 的所有重要变更都记录在这里。

---

## [0.2.0] — 2026-09-07

- 新增 ZCode direct adapter 与显式 opt-in 的 app-server 路线，支持固定 native bundle、逐次权限决策、精确 session 恢复及受保护的 Windows launcher；默认 headless。
- 新增可选 loopback Runtime HTTP transport；agentd 在 claim 前校验 Coordinate v0.4.0 契约能力，支持 claim key 重放、agent reconcile 和权限不确定时停止新 claim。
- managed Discord 准入改为 Coordinate channel binding authority；standalone 继续使用静态 channels allowlist，不依赖 Coordinate。
- 新增带身份/CAS 校验的 context cursor、durable runtime reply outbox 和可选 agentd 健康投影；保留既有 machine outcome 与公开错误处理契约。
- 本地 sessions SQLite 增加三个 context cursor 字段，context SQLite 新建 runtime_reply_outbox，保留旧行。升级顺序为 Coordinate -> 备份本地 DB -> MultiNexus；回退前处理 outbox，旧版无法消费新投影，不保证无损降级。

## [0.1.3] — 2026-08-02

- 新增 generic ACP v1 adapter（`adapter = "acp"`）：通过 stdio 连接任意 ACP v1 agent
  server，支持 fresh session 与按 provider capability 声明的 resume/load 文本会话；
  默认拒绝 permission request，且不向 provider 声明 filesystem、terminal 或
  terminal-auth 能力，也不执行任何 tool；
- 新增 Qoder（`adapter = "qoder"`）与 Grok Build（`adapter = "grok"`）direct JSON
  adapters，支持 fresh 与显式 `--resume` 文本会话；Grok 固定使用 `--no-memory` 关闭
  provider-native 跨 session memory，其 JSON 中的 `thought` 字段不转发；
- 固定依赖 `agent-client-protocol==0.11.1`；
- 现有 direct adapters（Claude/Codex/OpenCode/OMP/Hermes）继续保留。

## [0.1.2] — 2026-07-30

- 新增 `python -m multinexus.setup`，引导首次用户完成 Standalone + Discord + 单 agent 配置；
- 新增 `python -m multinexus.setup --check`，在不连接 Discord/provider 的前提下检查本地配置；
- Bot Token 通过隐藏输入读取，只写入 owner-only `.env`，不进入 argv、TOML 或输出；
- 检测到现有 `agents.toml` 时默认不覆盖，转为 sanitized check。

## [0.1.1] — 2026-07-29

- 修复 Coordinate-managed direct console-script 路径下 `coordinator_db_path` 未传给
  `MULTI_AGENT_COORDINATOR_DB`、可能静默落入默认数据库的问题；
- 保留 `MAC_DB` 以兼容既有 wrapper，并增加 subprocess env 回归测试；
- 补充 Coordinate fresh install、绝对 DB 路径和不连接 Discord/不调用 provider 的
  no-send 一次性验证说明。

## [0.1.0] — 2026-07-29

首个 MultiNexus clean export：

- 发布 Discord bridge、Coordinate-managed `agentd`、Claude/Codex/OpenCode/OMP/Hermes adapters；
- 发布 session/context、channel binding、execution context/lease/binding/capacity 契约；
- 恢复 KOOK transient message purge，并加入回归测试；
- 公开配置只包含 synthetic identity 与 secret-free 模板；
- 私有 task evidence、生产脚本、host-specific adapter、session/log/data 与 Git 历史未进入公开仓库；
- 文档明确区分当前 bridge 能力、可复用 library 与未来 wiring。

## 上游历史记录

以下条目来自 `discord-nexus` 上游历史，只说明来源基线，不代表所有功能都已接入
当前 MultiNexus bridge。当前能力以 README、`docs/agents.md` 与代码为准。

### [upstream-0.2.0] — 2026-04-22

### 功能

- **Claude shell 访问** — Claude 现在通过 `--dangerously-skip-permissions` 拥有完整的工具访问权限（Bash、Edit、Read 等），与 Codex 的能力相匹配
- **会话持久化** — Claude 和 Codex 会话按 thread 持久化；后续消息恢复相同的 CLI 会话而不是重新开始，跨轮次保留上下文
- **THEN 屏障** — 阶段之间带屏障关键词（`THEN`、`AFTER`、`NEXT`、`WAIT`、`WHEN DONE`、`ONCE DONE` 等）的顺序多 agent 执行；阶段内的 agent 仍并行运行
- **Per-agent prompt 拆分** — 多 agent 消息只给每个 agent 其自己的部分，而不是向所有人广播完整消息
- **列表引用展开** — 像 `do (1)`、`#2`、`step 3`、`task 1` 这样的简写 prompt 自动展开为上一个 assistant 消息中编号项目的完整文本；如果找不到列表则要求澄清
- **可配置活跃超时** — Codex `activity_timeout` 可通过 `agents.toml` 配置，并可通过 `-t <seconds>` 标志按命令覆盖（例如 `!g -t 1800 ./gradlew spotlessCheck`）
- **附件处理** — 跨所有路由路径（bang 命令、@role 提及、@team）对文件附件进行文本提取和视觉块处理
- **私有 wiki 提升按钮** — 为 agent 编写的私有 wiki 草稿提供内联 Promote/Reject 按钮

### 修复

- 修复了 `turn.completed` token 元数据的 Codex 事件解析
- 修复了 `/wiki-private` slash 命令的使用文本
- Codex session ID 现在可以正确从 `session_meta` 事件中提取

---

### [upstream-0.1.0] — 2026-04-19

首次公开发布。

### 功能

- 通过角色提及（`@Agent`）和 slash 命令（`/claude`、`/codex`、`/local-agent`）进行多 agent 路由
- `@team` 角色广播 — 提及可配置角色以并行调用所有 agent
- Agent 交接 — agent 通过响应中的 `@AgentName <task>` 相互委派
- SQLite (aiosqlite) 中的 per-thread 对话历史
- Per-thread agent 工作区（scratch 笔记跨轮次保留）
- 实时流式传输 — Claude 和 Codex 在生成时将部分输出流式传输到 Discord 占位符
- Thread 支持 — webhook 路由在论坛帖子和 thread 频道中正常工作
- `/stop` — 在生成中途取消正在运行的 agent
- Cron 调度器 — `/cron add|list|delete|enable|disable` 用于周期性 agent prompt
- 公共 wiki — 带自动接入和 agent 写入标签的平文件 Markdown wiki
- 私有 wiki — 敏感内容的独立层级，已 gitignore 并存储在仓库外
- 记忆洗衣机（`washer.py`）— 使用本地 LLM 从对话历史中提取持久记忆的夜间管道
- 私有审查队列 — 敏感记忆提取保留供人工批准
- 发现 — agent 通过 `<!-- DISCOVERY: -->` 标签将重要发现发布到共享频道
- 网络研究 — 用于搜索查询的可选 researcher agent
- 机密脱敏 — 所有 agent 输出在发布到 Discord 之前都会被扫描
- 限流回退 — Claude 被限流 → 回退到 Codex → 本地 LLM
- 健康仪表板 — `/dashboard` 发布带 agent 和系统状态的实时更新 embed
- 跨平台 — 支持 Windows（codex.cmd、CREATE_NO_WINDOW）和 Mac/Linux
- PM2 就绪 — 包含 `ecosystem.config.js`，用于带自动重启的持久运行
