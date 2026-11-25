# Task: Issue #3788 无场景门禁分支

| # | 任务 | 产出 | 状态 |
|---|------|------|------|
| 1 | 判官新增「无场景」显式入参 `scene_scope=None`（`source=no_scene`、`expected_scope=""`） | `src/api/scene_skill_scope.py` | done |
| 2 | 派发侧门禁场景三态（`evt` → Issue 真实绑定 → 未判定按 repo） | `src/domains/runtime/pipeline/event_pipeline.py` | done |
| 3 | 放行可观测：结构化 INFO `decision=no_scene` | `event_pipeline.py` | done |
| 4 | 单测：无场景放行 / 场景拦截 / 查询异常拦截 / 空串不回落 | `tests/runtime/test_issue_3788_no_scene_skill_gate.py` | done |
| 5 | 回归：`test_issue_1758_scene_skill_scope.py`、`test_issue_657_skill_scope_validation.py` | pytest 结果 | done |
| 6 | 自审查（5 轴 + 开源前置增量检查）、建 PR、A2A 交 QA | PR / 评论 | done |

## 验证命令

```bash
python3 -m pytest tests/runtime/test_issue_3788_no_scene_skill_gate.py \
  tests/mgmt/test_issue_1758_scene_skill_scope.py -q
make lint
python3 scripts/check-arch.py
```