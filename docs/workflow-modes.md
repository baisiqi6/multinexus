# Workflow Modes

MultiNexus 是执行织物，不拥有项目级验收判断。使用它的项目 Harness 应只保留两种
交付模式，避免把普通改动升级成不必要的流程实体。

## Ordinary mode

适用于低风险、范围明确、可快速回滚的改动：

```text
task spec → worker → independent review → tests
```

- task spec 写明允许修改的范围和可验证成功条件；
- worker 与 reviewer 使用独立 session；
- 测试与 review 通过后，由当前 Operator 按项目 authority 收口。

## High-risk mode

适用于生产 mutation、权限/authority/schema 变化、不可逆数据操作或恢复协议：

```text
plan gate → reviewed bootstrap → bounded mutation → independent result review
→ deploy/recovery verification → closeout
```

- 每次 mutation 都绑定明确 authority、输入版本、验证和 rollback/recovery 边界；
- 计划或候选内容改变后，旧 approval 不得复用；
- provider-native JSONL 可用于判断 worker 活跃性，但不承诺暴露私有思维链；
- 生产状态在时间间隔后必须重新读取，不能依赖旧快照；
- deletion/decommission/publication 使用各自的 gate，不共享一个万能 token。

## 选择规则

只有出现以下边界时才升级到 high-risk：

- authority 改变；
- 外部可见或生产副作用；
- schema/data migration；
- 可独立回滚或恢复的风险单元；
- 需要不同权限的 worker/reviewer/operator。

否则使用 ordinary mode，不新增 packet、receipt 或中间状态。
