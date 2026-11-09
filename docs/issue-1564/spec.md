# Spec: Issue #1564 — A2A 派发应在流水线保存时校验技能作用域

> 来源：user feedback #2601 | P1 bug | 交付基线：weekly-mys

## 背景
Issue #657 已落地 repo 场景的 create/update/apply 保存期技能 scope 校验与运行期
`_skill_scope_mismatch` 兜底。但场景市场（模板）的保存与「应用模板」路径未校验：
用户可通过模板应用绕过保存拦截，冲突仍在 A2A 派发期才暴露；400 缺少结构化
`violations[]`，前端无法按节点标红；存量场景缺少一次性只读体检。

## 需求
- **R1** 市场模板 create/update 保存即强制拒绝 user scope（含创建者本人）。
  update 无 repo 入参，repo_key 取市场行自身 `repo_key`。
- **R2** 应用市场模板前以应用者身份复核：本人 user 技能放行（派发身份即本人），
  他人 user 技能 400；保留既有「技能已删除」校验。
- **R3** scope 校验失败的 400 detail 一次性返回全部冲突节点
  `violations[{node_id,node_tag,skill,actual_scope,expected_scope}]`，
  并保留顶层 `skill/actual_scope/expected_scope` 兼容既有调用方。
- **R4** 前端场景编辑/创建保存失败时按 `violations[].node_id`（回退 `node_tag`）
  对节点卡片标红并展示提示，修正技能后即时清除。
- **R5** 新增只读接口 `GET /scenes/skill-scope-audit?repo={owner/repo}&limit=`，
  遍历仓库 `devops_pipeline_scene`（is_active=1）输出冲突清单与汇总计数，不改写数据。
- **R6** 运行期 `_skill_scope_mismatch` 兜底保留不变。

## 验收标准
- [x] 市场模板创建/更新引用 user 技能返回 400（含创建者本人），文案含技能名/实际/期望 scope
- [x] 应用市场模板：他人 user 技能 400；本人按 #657 口径放行
- [x] 既有 #657 场景 create/update/apply 行为不变，无回归
- [x] 400 响应含 `violations[]`，一次返回全部冲突节点
- [x] 前端保存失败对应节点卡片标红并展示提示，修正后清除
- [x] `GET /scenes/skill-scope-audit` 列出存量冲突，只读且计数正确
- [x] 运行期 `_skill_scope_mismatch` 兜底保留并通过回归

## 场景技能 scope 校验口径（保存期 vs 派发期）
| 阶段 | 校验位置 | user scope 口径 | 失败处理 |
|------|----------|-----------------|----------|
| repo 场景保存 | `_validate_scene_skill_scope`（create/update） | 本人 user 技能放行，他人拒绝 | 400 + violations |
| repo 场景应用 | `apply_scene` / `apply_market_scene` | 以应用者身份：本人放行，他人拒绝 | 400 + violations |
| 市场模板保存 | `create_market_scene` / `update_market_scene` | **一律拒绝（含创建者本人）** | 400 + violations |
| 存量体检 | `GET /scenes/skill-scope-audit` | 严格模式：user scope 一律计冲突 | 只读清单 |
| 运行期派发 | `_skill_scope_mismatch`（webhook/event_pipeline） | 兜底 fail-fast | `skill_resolve_failed` |

市场模板是全仓共享配置，若允许 creator 本人 user 技能入库，模板被其他用户应用时
必然解析失败，因此在保存期即拒绝；repo 场景的「本人 user 技能放行」保留，因为
派发身份即场景创建/应用者本人，可正常解析。

## 不做
技能表结构变更、历史数据自动改写、运行期派发逻辑重构、市场模板跨仓分发语义调整。

## 测试
`tests/test_issue_1564_skill_scope_market_paths.py`（沿用 #657 fixture），
并回归 `tests/test_issue_657_skill_scope_validation.py`。