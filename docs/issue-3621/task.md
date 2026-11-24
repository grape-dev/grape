# Task: Issue #3621 — 建单端点 `trigger_pipeline` 开关

- [x] T1 `TaskCreateBody` 增 `trigger_pipeline: bool = True`（issues_center.py）
- [x] T2 建单入口 `issues_create` 透传 `trigger_pipeline`（routes/issues.py）
- [x] T3 `create_task_via_issue` 按开关分支：true 现状不变；false 写 `stage=created`（status 保持 active）跳过 `run_pipeline`
- [x] T4 抽取 `_issue_opened_pipeline_trigger` 共用 helper（原生 payload / internal trigger / to_thread 语义保持）
- [x] T5 新增 `trigger_pending_issue_pipeline`（挂单首次触发：复用 issue_opened 路径，stage/事件双重幂等 409；同锁事务 + CAS 认领；失败/无事件回滚 created）
- [x] T5b 新增 HTTP 入口 `POST /api/issues/{issue_id}/pipeline/trigger`（user_only）接线 T5
- [x] T6 返回体增 `trigger_pipeline` 回显 + 挂单文案
- [x] T7 文档登记 `docs/openapi/11-issues.md`（body/出参/stage 取值）
- [x] T8 测试：默认/显式 true 回归、false 挂单、后续触发入链 + 幂等、非 created 409
      （`tests/runtime/test_issue_3621_trigger_pipeline_switch.py`，5 用例全绿）
- [x] T9 关联既有源码断言迁移（`tests/runtime/test_event_active_guard.py` 方案 D 断言指向新 helper，断言内容不变）
- [x] T10 HTTP 入口层测试（trigger 路由接线 / create 布尔显式解析 / 无效值 400）+ 失败回滚/无事件重试断言

## 验证
- `python3 -m pytest tests/runtime/test_issue_3621_trigger_pipeline_switch.py -q` → 5 passed
- `python3 -m pytest tests/runtime/test_event_active_guard.py -q` → 全绿
- 基线已知失败（与本改动无关，weekly-mys 基线即红）：`test_issue_89_data_consistency.py::TestCreateTaskTriggersEvent`
  （mock 的 `get_user_token_with_refresh` 与现行 `get_user_token_for_repo` 取 token 链不匹配 → 400）、
  `test_issue_415_scene_binding.py` 场景回退 2 例、`test_issue_453_outbound_ledger.py` 1 例、
  `test_issue_178_task_creator.py` 7 例 error（stash 后复跑同样失败，已比对确认）。
