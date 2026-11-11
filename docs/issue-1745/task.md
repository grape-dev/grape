# Task: Issue #1745 任务拆解

| # | 任务 | 状态 | 验证 |
|---|------|------|------|
| 1 | 定位注入侧陈旧来源：普通事件重派无条件传事件行 scene 作 override；worker 最新 env 不校正 scene | 完成 | 代码走查 + Issue 场景链路分析 |
| 2 | `src/api/routes/events.py` 新增 `_redispatch_scene_override`，普通事件丢弃陈旧 override，PR 订阅保留 | 完成 | `test_redispatch_*` 用例 |
| 3 | `src/api/event_pipeline.py` 新增真实场景解析、诊断文案、PR 订阅识别、执行前校正 | 完成 | `test_reconcile_*` / `test_scene_env_mismatch_diagnostic_*` |
| 4 | `src/api/routes/runtime.py` task env 接口返回前执行场景校正 | 完成 | `test_issue_990_session_token_snapshot.py` 回归 |
| 5 | help 文档固化「以 `issues detail.scene_id` 为权威」口径 | 完成 | 文档检查 |
| 6 | 新增回归测试 `tests/test_issue_1745_scene_env_truth.py` | 完成 | 11 passed |
| 7 | 既有回归：`test_scene_env.py` / `test_issue_568_*` / `test_issue_990_*` | 完成 | 34 + 20 passed |
| 8 | 交付 `docs/issue-1745/{spec,plan,task}.md` | 完成 | 文件存在 |
| 9 | 建 PR + Issue 评论 + A2A 通知 QA | 待执行 | PR 链接 |
| 10 | runbook 口径同步（`grape-dev/grape-skills`） | 待协同仓处理 | 跨仓交付 |

## 验收对照
- 注入一致性：重派后 `GRAPE_SCENE_ID` = Issue 当前归属场景（普通事件不再沿用源场景）。
- 陈旧变量：执行前校正覆盖陈旧快照，目标场景节点可取且可成功 A2A。
- 诊断：`scene_env_mismatch_diagnostic` 含注入值/真实值/`issues detail` 与 `scenes detail` 恢复指引。
- 回归：单场景、PR 订阅（#427）、scenes detail / handoff 既有行为不变。
