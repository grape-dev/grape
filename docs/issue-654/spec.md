# Spec: Issue #654 — 切换 credential scope 触发引擎重新注册（引擎 id 变更 + priority 静默重置）

> 来源：Issue Agent 已发布 Spec（评论 #197528747），本文为开发侧落地副本。
> 目标分支：`weekly-08-mys`（场景注入权威值；后续并入 weekly-09 迭代）。

## Objective

- 修复 runtime credential 在 repo/user 间切换 scope 后引擎 id 全量变更（17002-17006 → 17042-17046）、priority/标签静默重置为默认 50 的缺陷，保证引用旧引擎 id 的配置（场景 engine_runtime_ids、agent backend）不失效。
- 成功标准：scope 往返切换后引擎 id、priority、标签不变；变更产生可追溯事件（日志含 cid、影响引擎数、旧→新 id 映射）。

## Scope

### In Scope
- `PUT /api/settings/runtime-credentials/{cid}/scope` 与 worker register 路径的引擎行一致性（id 稳定）
- engine_capabilities（tag/priority/max_concurrent/enabled）与 tags_user_set 的继承
- 引擎行替换时的引用 remap 与变更事件/日志

### Out of Scope
- #646 全量表外部稳定标识改造
- 已漂移历史数据的自动修复
- 前端通知中心改造

## 根因（代码级）

1. `change_credential_scope`（src/api/routes/settings.py）：切到 user scope 时引擎行 owner_id 置空；
2. `runtime_register`（src/api/routes/runtime.py）：upsert 查找键为 (credential_id, lower(name), scope, owner_id)，user scope 时 owner_id 按 owner_name 从 grape_accounts 重算 → 与置空行不匹配 → INSERT 新行（新自增 id）；
3. 新行无 engine_capabilities → 写默认标签 'all'/priority=50 → 原配置（如 100）丢失；
4. 切回 repo 时去重 DELETE（按 lower(name) 保留 MAX(id)）删除旧行 → 旧 id 消失、engine_runtime_ids/agent backend 引用悬空，全程无事件。

## 验收标准

- [ ] scope repo→user→repo 往返后引擎 id 不变（register 复用既有行）
- [ ] 往返后 engine_capabilities 的 priority/标签与 tags_user_set 保留原值（不回默认 50）
- [ ] 引擎行被替换的兼容路径中，engine_runtime_ids / agent backend 引用被 remap，不悬空
- [ ] scope 切换/引擎替换输出可追溯事件（日志含 cid、影响引擎数、旧→新 id 映射）
- [ ] 所有现有测试通过；新增上述场景回归测试
