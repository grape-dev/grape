# Task: Issue #404 — 任务拆解

## T1 离线告警侧链（event_pipeline.py） [high]
- [x] 新增 `_SCENE_OFFLINE_ALERT_TTL` / `_scene_offline_alert_sent` / `_scene_offline_alert_lock`
- [x] 新增 `_emit_scene_offline_alert(scene_id, agent_type, reason, repo_key)`
- [x] `_fail_scene_all_offline` 末尾接入 `_emit_scene_all_offline_alert`
- [x] 告警侧链异常吞掉（不影响主流程）

## T2 引擎恢复钩子（engine_probe.py） [high]
- [x] 新增 `_on_engine_recovery(engine_name)`：反查绑定场景 + pool 校验 + log 可重派事件数
- [x] `_probe_loop` 接入 fail→ok 转换检测
- [x] `probe_all_now` 同步接入

## T3 一键重派列表端点（events.py） [high]
- [x] 新增 `GET /events/redispatchable`
- [x] 查 failed + result LIKE '%场景绑定的引擎全部离线%'
- [x] 逐条 `get_scene_pool_engine_ids` 校验非 None
- [x] 返回 `{items, total}`

## T4 场景配置离线校验（scenes.py） [medium]
- [x] `update_scene` 遍历 nodes，调 `get_scene_pool_engine_ids`
- [x] 全灭 → 收集 warning，不阻断保存
- [x] 返回体增 `warnings` 字段

## T5 单测（tests/test_issue_404_scene_offline_alert.py） [high]
- [x] test 1: 全灭 → 事件 failed + result 含 scene/agent
- [x] test 2: 恢复 → redispatchable 列表 + redispatch 成功 + 原事件终态不变
- [x] test 3: 1h 内重复 → 告警只发一次
- [x] test 4: pool 有兜底 → claim 正常
- [x] test 5: 空场景 → 不误报

## T6 自审查 + PR [high]
- [ ] 5 轴自审查（code-review-and-quality.md）
- [ ] 增量敏感扫描（pre-pr-release-check.md）
- [ ] 5 条单测全绿
- [ ] 建 PR base=weekly-clean-mys
- [ ] Issue 评论告知
- [ ] A2A 通知 QA
