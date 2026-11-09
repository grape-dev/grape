# Spec: Issue #1005 — grape git-repo issues update milestone_id 静默失效

> 评级：ready | 目标开发分支：weekly-mys | 本文件为仓库内实现索引副本（Issue 已发布 Spec 评论 198696314）。

## Objective
让 `grape git-repo issues update --milestone_id` 的里程碑指派可靠落地，失败时给出可区分的明确反馈；
里程碑列表/文档给出唯一且可直接使用的取值口径。

## 实测复核（本轮）
- 目标基线 **weekly-mys 已包含 #640 两笔**（`24f11752` 映射 upstream `milestone`、`a32e3d07` 回读校验），
  PR base 与 main 行为一致，不会回归"200 但 milestone 空"。
- `git-repo milestones` 仍无 `id`：weekly-03 仅 `number=850012`，真实 id 只在 `url` 尾部（`…/milestones/3`）。
- `issues update` 的旧回读失败统一返回 502，未区分「不存在 / 无权限 / 上游忽略」。

## Scope
### In Scope
- `milestones` 列表/创建/更新补 `id`（取 `url` 尾部数字），保留 `number`。
- `issues update` 的 `milestone_id` 先解析（**id 优先、number 兼容**）再写上游。
- 失败语义：不存在 → 400；无权限 → 403；上游 200 但回读不一致 → 422（不再 502）。
- `issues get` 回显 milestone 并补 `id`（供 QA 断言）。
- 契约（OpenAPI 描述）与 help/docs 文档同步取值口径。

### Out of Scope
- 里程碑创建接口 / 版本排期策略 / 自动分配逻辑。
- 跨仓 grape-skills 的 runbook 资产（另仓跟进）。

## Acceptance Criteria
- [ ] `milestones` 返回非空 `id`，与 `url` 尾部一致（weekly-03 → `3`）。
- [ ] `milestone_id` 传 `id` 或 `number` 均可命中；id 与 number 冲突时 id 优先。
- [ ] 无效值：400（不存在）+ 明确原因；无权限：403；上游忽略：422。
- [ ] 传有效值后 `issues get` 可读回 `milestone`（含 id/number/title）。
- [ ] 契约/help/docs 标注唯一取值口径。
- [ ] 现有测试通过，新增解析/回读/错误分支覆盖。

## Risks & Mitigations
| Risk | Impact | Mitigation |
|------|--------|------------|
| id 与 number 混用破坏既有调用方 | Med | 保留 number；id 优先、number 兜底 |
| 上游 PATCH 参数实际期望 number | Med | 解析后以目标里程碑 number 写上游（沿用 #640 已验证映射） |
| 额外列表请求增加延迟 | Low | 仅含 milestone_id 的 update 触发；分页上限 21×100 |