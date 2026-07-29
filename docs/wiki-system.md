# WikiStore 组件

> **状态：可复用存储组件，尚未接入当前 Discord bridge。**

`services/wiki.py` 提供一个异步、file-backed 的 Markdown wiki 存储层。它支持
公共与私有层级、draft/promotion、索引、关键词检索、discovery 日志接入和写入前
secret scrubbing。当前 `multinexus/client.py` 不会实例化 `WikiStore`，也没有注册
`/wiki` slash commands；因此本页描述的是可供集成方调用的 library API，不是开箱
即用的 Discord 功能。

## 存储布局

```text
wiki/
├── index.md
├── pages/
├── drafts/
└── private/
    ├── index.md
    ├── pages/
    └── drafts/
```

- 公共页面位于 `pages/`，公共草稿位于 `drafts/`。
- 私有页面与草稿位于 `private/`；仓库 `.gitignore` 排除了 `wiki/private/`。
- `WikiStore` 会在写入前应用自己的 secret scrubbing 规则，但调用方仍需执行权限
  判断，不能把 scrubbing 当作 authorization。

## 最小使用方式

```python
from pathlib import Path

from services.wiki import WikiStore

wiki = WikiStore(Path("wiki"))
```

主要能力以 `services/wiki.py` 中的公开方法为准，包括页面写入、读取、检索、
draft promotion/demotion、私有层级操作和 `ingest_discoveries(...)`。接入前应为调用方
增加明确的身份/权限校验，并用测试覆盖 path traversal、private-tier 隔离与并发写入。

## 与 `agents.toml` 的关系

`AgentConfig` 当前接受以下字段：

```toml
[defaults]
wiki_enabled = false
wiki_path = "wiki"
```

这些字段为 bridge 集成保留，但仅把它们设为 `true` 并不会让当前 Discord client
自动创建 `WikiStore`、解析 `<!-- WIKI: ... -->` 标签或注册 `/wiki` 命令。集成方必须
显式完成 wiring。

## 私有数据边界

- `wiki/private/` 只是本地持久化位置；不要将真实私有页面复制进 clean export。
- `PRIVATE_DB_PATH` 属于其他私有 SQLite 状态的路径配置，不会改变 `WikiStore` 的
  Markdown 根目录。
- 若把 wiki 接入平台消息，调用方需要另外定义谁可写私有层、谁可读私有层，以及
  失败时是否 fail-closed。

## 当前不应声称的能力

在 bridge wiring 与行为测试落地前，公开文档和部署说明不得声称以下能力已开箱可用：

- `/wiki write|read|list|search|promote|demote` slash commands；
- 自动解析 agent 响应中的 `WIKI` / `WIKI-PRIVATE` 标签；
- 自动策展或定时 promotion；
- 所有 agent 都能自动读取或写入 wiki；
- private wiki 已由平台权限边界完整保护。

这些可以作为后续 bounded integration package 实现，但不属于 v0.1.0 clean export
的发布承诺。
