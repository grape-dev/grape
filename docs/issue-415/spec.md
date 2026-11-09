# Spec: Issue #415 — ChatAgent 对话创建的 Issue 未绑定场景，「流水线场景」显示 --

## Objective
- 修复 ChatAgent/VOD 对话通道创建 Issue 时未写入场景绑定的问题，使需求详情页「流水线场景」显示实际场景名称而非 --。
- 用户价值：对话反馈通道创建的需求可正常进入场景流水线路由（平台自身核心链路）。
- 成功标准：对话创建的新 Issue scene_id 非空或展示侧正确回退仓库默认场景；存量 #406 有处理策略。

## Background（现状确认）
- `get_issue_scene`（src/api/session_store.py）：PR 伪 id 直接解析；否则 issue 表 scene_id；都没有 → ("", 0)，由调用方决定走智能推荐。**未回退仓库默认场景**（managed_repos.dev_scene）。
- `create_task_via_issue`（src/api/issues_center.py）：仅当显式传 scene_id（非空且非 vod-auto）才写 scene；空/智能推荐 → 不写 → 依赖 issue_opened 的智能推荐流程。对话通道（VOD/Chat）创建时未带 scene_id 且当日未走推荐流程 → 详情页显示 --。
- 需求详情/列表展示 `_query_tasks`（issues_center.py）：Issue 级场景为空时注释声称回退仓库默认场景，但实际 `_scene_id=""`（未读取 dev_scene），前端渲染 `sceneName = t.scene_id ? ... : '—'`。
- 存量 Issue #406（需求 #3436）：scene_id 为空，详情页显示 --。

## Scope
### In Scope
1. 创建侧：对话通道/无显式场景建 Issue 时写入场景绑定（缺省回退仓库默认场景 managed_repos.dev_scene）；
2. 展示侧：`get_issue_scene` 在 Issue 级场景为空时回退仓库默认场景；`_query_tasks` 空场景回退仓库默认场景展示；
3. 存量数据：一次性补绑脚本（默认 dry-run）处理无场景 Issue，如 #406。

### Out of Scope
- 前端「流水线场景」组件样式重构；
- 场景推荐算法本身的优化。

## Technical Approach
### 方案概述
创建链路补写 scene 字段 + 展示查询侧空值回退 + 一次性存量补绑。
### 实现路径
1. `get_issue_scene` 空值 → 回退 `get_repo_scene(repo_key)`（managed_repos.dev_scene）再返回；
2. `create_task_via_issue`：无显式场景（空 / vod-auto）时，若仓库配置默认场景则写入 scene_id；
3. `_query_tasks`：Issue 级场景为空时用仓库默认场景填充 scene_id/scene_name 展示；
4. 新增 `scripts/backfill_issue_scene.py`：列出存量无场景 Issue（先 dry-run），`--apply` 时按仓库默认场景补绑。

### 关键设计决策
| 决策 | 选项 | 选择 | 理由 |
|------|------|------|------|
| 绑定时机 | 仅展示回退 vs 创建写入+展示回退 | 双管齐下 | 数据侧治本 + 展示侧兜底，覆盖存量与增量 |
| 存量处理 | 自动迁移 vs 补绑脚本 | 补绑脚本（可审计） | 避免误绑，保留人工确认空间 |
| 智能推荐兼容 | 空值一律回退 vs 仅在无默认场景时走推荐 | 无默认场景时走推荐 | 仓库未配置默认场景时不改变智能推荐既有链路 |

## Acceptance Criteria
- [ ] `get_issue_scene`：Issue 级场景为空且仓库有默认场景 → 返回仓库默认场景
- [ ] `create_task_via_issue`：无显式场景且仓库有默认场景 → 写入 scene_id
- [ ] `_query_tasks`：无场景 Issue 详情/列表 scene_id/scene_name 显示仓库默认场景
- [ ] 补绑脚本默认 dry-run，`--apply` 支持存量补绑（#406 作为验证样本）
- [ ] 所有现有测试通过；新增测试覆盖：get_issue_scene 回退、创建写入、查询展示、脚本 dry-run

## Risks & Mitigations
| Risk | Impact | Mitigation |
|------|--------|------------|
| 存量误绑错误场景 | Med | 补绑脚本先 dry-run 输出清单再执行 |
| 仓库默认场景缺失的边界 | Low | 回退链路再兜底为空并保留 -- 语义 |
| 智能推荐路径被默认场景提前短路 | Low | 仅当仓库存在 dev_scene 时回退；无默认场景仍走智能推荐 |

## Open Questions
- 无