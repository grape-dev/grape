# Issue #605 任务拆解（task）

1. [x] 取证（代码层）：走查 pick_engine / get_scene_pool_engine_ids / claim_by_tag 派发与认领链路，定位静默过滤点
2. [x] engine_select 结构化决策日志（候选 + 过滤原因 + 选中理由）
3. [x] claim / claim_by_tag 的 pool 名单跳过日志（带去重）
4. [x] 取证接口 GET /api/dashboard/ops/engine-select-explain
5. [x] 测试：tests/test_engine_select_decision_log.py + 既有派发链路测试回归
6. [ ] 部署后生产回归：派发 agent:vod 批次确认 hermes 参与派发（需运维执行，随 PR 附说明）
