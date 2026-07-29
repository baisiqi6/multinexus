# 项目来源与维护说明

本项目基于 GitHub 上的 `baisiqi6/discord-nexus` 继续维护。

原始仓库：
https://github.com/baisiqi6/discord-nexus

上游源头：
https://github.com/mikatachan/discord-nexus

分叉基线：
`97dc071f83f67c1a38b91f4d468ff014e1bbdf76`（`v0.2.0`，`Update CHANGELOG and README for v0.2.0`）

## 为什么单独维护

原项目已经较长时间没有继续更新。当前需求已经从单一 Discord bot 扩展到：

- OpenClaw CLI agent，而不是旧的 OpenClaw HTTP relay。
- Claude、Codex、小龙虾在同一频道里的 multi-agent 协作。
- 更好的 managed context：TTL、compaction、summary + recent raw history。
- 后续接入 Windows 主机和云服务器上的 remote agent runner。

这些改动会逐步偏离原项目，所以使用独立仓库维护更清晰。

## 授权与署名

原项目 README 中标注 License 为 MIT，但仓库内没有独立的 LICENSE 文件。
本仓库使用标准 MIT 正文，署名如下：

```
Copyright (c) 2026 mikatachan and discord-nexus contributors
Copyright (c) 2026 baisiqi6 and MultiNexus contributors
```

约定：

- 原始代码归原作者与 contributors 所有。
- 本仓库新增和修改的代码归当前维护者所有。
- 后续发布、分享或二次分发时保留 MIT License 与本来源说明。

## Clean Export

当前公开候选是从私有开发历史导出的 clean export，不携带私有开发历史、
task evidence、生产部署脚本或 host-specific 配置。公开版本只包含可理解、
可安装、可前台运行、可测试的产品代码与稳定文档。

## 本地配置不要入库

以下文件只用于本机运行，不应提交：

- `.env`
- `.env.*`
- `agents.toml`
- `agents.toml.bak`
- `data/`
- `*.db`
- `wiki/private/`
- `logs/`

可提交的是 `.env.example`、`agents.toml.example` 和 `config/agent-registry.example.toml`，
用于记录可公开的模板。
