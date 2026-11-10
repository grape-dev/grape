# Spec: Issue #1806 — sweeper 折叠任务 A2A context 四要素契约

> 来源：Issue Agent Spec（Issue 评论 199602626）| Coding 按 weekly-mys 基线落地

## Objective
sweeper-v2 折叠任务（idempotency_key 形如 `sweeper-v2-pr754-fold620`）派发时，
`payload.context` 必须显式携带源 PR / 目标 PR / 落点文件 / 折叠原因四要素；
A2A 端点对空/缺项 context 做兜底拦截，承接方无需从 idempotency_key 反推落点。

## Scope
### In Scope
- 折叠任务识别单源判定函数：`sweeper-v2-<任意>-fold<数字>`。
- context 四要素完整性校验：`source_pr` / `target_pr` / `target_files` / `fold_reason`。
- 空/缺项 context 默认 400 拒绝，错误体含 idempotency_key、缺失字段清单、schema 示例。
- 配置开关 `GRAPE_A2A_FOLD_CONTEXT_ENFORCE=0` 降级为放行 + 告警留痕。
- 三分支单测：折叠任务空 context 拦截、折叠任务合法 context 放行、非折叠任务不变。

### Out of Scope
- 非折叠任务 payload 结构与既有幂等语义（同仓同键仍返回已有事件）。
- sweeper 折叠算法重构；coding-runbook 段落（属 grape-skills 侧资产）。

## 验收标准
- [x] 折叠任务 context 四要素齐全时放行，入库 context 保留四要素——本仓范围可验证；
      「承接方仅凭 context 即可确定落点」属 sweeper-v2 承接链路，不在本仓验证范围。
- [x] 空/缺项 context 被 400 拦截；降级时输出含 task 标识与 idempotency_key 的结构化告警事件。
- [x] 已存在 fold 事件同键重试返回已有事件（守卫位于幂等命中短路之后）；非折叠任务同仓同键重复请求仍返回已有事件、不重复派发。
- [x] 判定逻辑单源，端点与测试共用；新增测试通过且现有测试全绿。