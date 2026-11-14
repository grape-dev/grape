# GSB 按方法路由 P95 压测报告（Issue #2479）

- 生成时间：2026-11-14 08:31:14（耗时 21s）
- 采样口径：同进程、同参数、同数据量；每接口 lib/remote 各 200 次（预热 20 次后采样），P95 = 95 分位单次耗时
- lib 侧：Local 契约实现直调（sqlite 会话库，单机/共库生产同路径）
- remote 侧：真实 loopback HTTP（127.0.0.1:38997，生产 /internal/* 路由 + HMAC 服务令牌全链路校验），GsbClient 含服务发现（环境变量命中态）+ HMAC + JSON 序列化；remote 客户端按代理持有句柄口径池化
- public 原语：本期无 remote 传输通道（lib-locked）；lib P95 经进程内 MySQL→sqlite 方言适配器实测（生产三库三机为 MySQL 直连）
- 判定阈值：remote 候选 P95 增量 ≤10ms 且相对增幅 ≤20%；热路径四项基线锁 lib

## 1. 契约方法（23）lib vs remote P95 增量表

| route_key | 基线 mode | lib P95(ms) | remote P95(ms) | 增量(ms) | 增幅 | 判定 |
|---|---|---|---|---|---|---|
| mgmt.get_repo_webhook_config | lib | 0.030 | 1.465 | +1.435 | +4730.4% | ❌ 超阈值 → 基线锁 lib（实测裁决 = lib） |
| mgmt.get_agent_session | lib | 0.026 | 1.457 | +1.431 | +5492.6% | ❌ 超阈值 → 基线锁 lib（实测裁决 = lib） |
| mgmt.get_scene_config | lib | 0.015 | 1.365 | +1.350 | +8933.1% | ❌ 超阈值 → 基线锁 lib（实测裁决 = lib） |
| mgmt.get_scene_pool_engine_ids | lib | 0.031 | 1.469 | +1.438 | +4703.2% | ❌ 超阈值 → 基线锁 lib（实测裁决 = lib） |
| mgmt.verify_session_token | lib | 0.012 | 1.724 | +1.712 | +14286.2% | 🔒 lib-locked（热路径，基线 lib） |
| mgmt.get_bot_config | lib | 0.032 | 1.520 | +1.488 | +4594.4% | ❌ 超阈值 → 基线锁 lib（实测裁决 = lib） |
| mgmt.get_skill | lib | 0.026 | 1.563 | +1.537 | +5842.2% | ❌ 超阈值 → 基线锁 lib（实测裁决 = lib） |
| runtime.get_events | lib | 0.247 | 2.117 | +1.870 | +756.3% | ❌ 超阈值 → 基线锁 lib（实测裁决 = lib） |
| runtime.get_event_detail | lib | 0.023 | 1.600 | +1.577 | +6711.0% | ❌ 超阈值 → 基线锁 lib（实测裁决 = lib） |
| runtime.get_tasks | lib | 0.036 | 1.699 | +1.663 | +4618.2% | ❌ 超阈值 → 基线锁 lib（实测裁决 = lib） |
| runtime.get_task_messages | lib | 0.019 | 1.682 | +1.663 | +8601.7% | ❌ 超阈值 → 基线锁 lib（实测裁决 = lib） |
| runtime.get_runtimes | lib | 0.020 | 1.563 | +1.543 | +7668.2% | ❌ 超阈值 → 基线锁 lib（实测裁决 = lib） |
| runtime.get_issue_state | remote | ERR | ERR | - | - | ERROR: OperationalError: no such table: issue_state |
| runtime.update_issue_state | remote | ERR | ERR | - | - | ERROR: OperationalError: no such table: issue_state |
| runtime.get_chat_conversations | remote | ERR | ERR | - | - | ERROR: OperationalError: no such column: backend_session_id |
| runtime.get_chat_messages | lib | 1.578 | 3.507 | +1.929 | +122.2% | ❌ 超阈值 → 基线锁 lib（实测裁决 = lib） |
| insight.log_obs | lib | 1.283 | 4.028 | +2.744 | +213.8% | 🔒 lib-locked（热路径，基线 lib） |
| insight.log_llm_usage | lib | 1.278 | 3.622 | +2.344 | +183.5% | 🔒 lib-locked（热路径，基线 lib） |
| insight.log_api_latency | lib | 1.650 | 4.024 | +2.374 | +143.9% | 🔒 lib-locked（热路径，基线 lib） |
| insight.query_metrics | remote | ERR | ERR | - | - | ERROR: OperationalError: no such column: metric_name |
| insight.query_slo | remote | ERR | ERR | - | - | ERROR: OperationalError: no such column: repo_key |
| insight.query_alerts | lib | 0.016 | 1.363 | +1.348 | +8695.0% | ❌ 超阈值 → 基线锁 lib（实测裁决 = lib） |
| insight.aggregate_runtime_stats | lib | 3.269 | 4.724 | +1.455 | +44.5% | ❌ 超阈值 → 基线锁 lib（实测裁决 = lib） |

## 2. public 数据原语 lib P95（remote 无传输通道 → N/A）

| route_key | mode | lib P95(ms) | 备注 |
|---|---|---|---|
| mgmt.fetch_rows | lib | 0.028 | lib-locked |
| mgmt.fetch_one | lib | 0.025 | lib-locked |
| mgmt.get_repo_field | lib | 0.022 | lib-locked |
| mgmt.set_repo_fields | lib | 0.022 | lib-locked |
| mgmt.ensure_repo_row | lib | 0.025 | lib-locked |
| mgmt.get_repo_org_id | lib | 0.023 | lib-locked |
| mgmt.list_user_ids_by_open_id | lib | 0.019 | lib-locked |
| mgmt.get_feishu_open_id | lib | 0.020 | lib-locked |
| mgmt.list_all_user_ids | lib | 0.015 | lib-locked |
| mgmt.list_baseline_knowledge_files | lib | 1.716 | lib-locked |
| mgmt.list_knowledge_obs_keys | lib | 0.246 | lib-locked |
| mgmt.get_autopilot_task_brief | lib | 0.019 | lib-locked |
| mgmt.mark_default_featured | lib | 0.054 | lib-locked |
| mgmt.set_scene_deploy_url | lib | 0.020 | lib-locked |
| mgmt.set_scene_nodes | lib | 0.023 | lib-locked |
| mgmt.upsert_user_mapping_feishu | lib | 0.043 | lib-locked |
| mgmt.clear_user_mapping_feishu | lib | 0.027 | lib-locked |
| mgmt.set_user_mapping_nickname | lib | 0.039 | lib-locked |
| mgmt.delete_feishu_onboard_task | lib | 0.014 | lib-locked |
| mgmt.mark_external_identity_expired | lib | 0.022 | lib-locked |
| mgmt.set_autopilot_run_info | lib | 0.022 | lib-locked |
| mgmt.set_autopilot_engine_ids | lib | 0.018 | lib-locked |
| mgmt.upsert_baseline_knowledge | lib | 0.271 | lib-locked |
| mgmt.ensure_scene_knowledge_ref | lib | 0.033 | lib-locked |
| mgmt.delete_event_knowledge | lib | 0.461 | lib-locked |
| mgmt.replace_scene_skill_ref | lib | 0.015 | lib-locked |
| mgmt.upsert_scene_content | lib | 0.094 | lib-locked |
| mgmt.set_scene_icon | lib | 0.044 | lib-locked |
| mgmt.get_official_scene_id | lib | 0.042 | lib-locked |
| mgmt.count_rows | lib | 0.013 | lib-locked |
| mgmt.count_official_scenes | lib | 0.044 | lib-locked |
| mgmt.list_enabled_autopilot_tasks | lib | 0.045 | lib-locked |
| mgmt.get_account_id_by_username | lib | - | N/A (OperationalError: no such column: username_lower) |
| mgmt.get_account_username_by_id | lib | 0.019 | lib-locked |
| mgmt.find_repo_key_in | lib | 0.021 | lib-locked |
| mgmt.list_alert_user_ids | lib | 0.031 | lib-locked |
| mgmt.get_user_id_by_mapping_column | lib | 0.030 | lib-locked |
| mgmt.read_one | lib | 0.023 | lib-locked |
| mgmt.read_all | lib | 0.018 | lib-locked |
| runtime.list_runtimes | lib | - | N/A (OperationalError: no such column: runtime_type) |
| runtime.list_runtimes_rows | lib | 0.099 | lib-locked |
| runtime.get_runtime | lib | 0.033 | lib-locked |
| runtime.task_stats | lib | 0.021 | lib-locked |
| runtime.append_task_message | lib | 0.024 | lib-locked |
| runtime.list_credential_rows | lib | 0.032 | lib-locked |
| runtime.latest_task_for_event | lib | 0.047 | lib-locked |
| runtime.task_brief | lib | 0.023 | lib-locked |
| runtime.get_skill_by_id | lib | 0.037 | lib-locked |
| runtime.get_skill_content_hash | lib | 0.021 | lib-locked |
| runtime.count_skills | lib | 0.040 | lib-locked |
| runtime.aggregate_counts | lib | 3.225 | lib-locked |
| runtime.list_repo_credentials | lib | 0.021 | lib-locked |
| runtime.has_engine_capability | lib | 0.017 | lib-locked |
| runtime.list_engine_capabilities | lib | 0.023 | lib-locked |
| runtime.list_engine_tags | lib | 0.025 | lib-locked |
| runtime.query_events | lib | 0.031 | lib-locked |
| runtime.group_events | lib | 15.490 | lib-locked |
| runtime.count_events | lib | 2.301 | lib-locked |
| runtime.get_autopilot_execution | lib | 0.023 | lib-locked |
| runtime.upsert_engine_capability | lib | 0.076 | lib-locked |
| runtime.update_engine_capability | lib | 0.032 | lib-locked |
| runtime.delete_engine_capability | lib | 0.015 | lib-locked |
| runtime.acquire_channel_lease | lib | 0.067 | lib-locked |
| runtime.release_channel_lease | lib | 0.037 | lib-locked |
| runtime.heartbeat_instance | lib | 0.072 | lib-locked |
| runtime.list_active_instances | lib | 0.038 | lib-locked |
| runtime.release_all_leases | lib | 0.044 | lib-locked |
| runtime.renew_channel_lease | lib | 0.052 | lib-locked |
| runtime.purge_user_rows | lib | 0.057 | lib-locked |
| runtime.delete_chat_conversations | lib | 0.000 | lib-locked |
| runtime.delete_vod_conversations | lib | 0.018 | lib-locked |
| runtime.detach_agent_sessions | lib | 0.020 | lib-locked |
| runtime.clear_agent_session_runtime | lib | 0.027 | lib-locked |
| runtime.ensure_agent_session | lib | 0.033 | lib-locked |
| runtime.stamp_task_traces | lib | 0.023 | lib-locked |
| runtime.mark_detach_rebound | lib | 0.033 | lib-locked |
| runtime.delete_autopilot_executions | lib | 0.015 | lib-locked |
| runtime.set_autopilot_findings | lib | 0.018 | lib-locked |
| runtime.set_repo_im_notifications | lib | 0.020 | lib-locked |
| runtime.add_event_action_filter | lib | - | N/A (unique write: repeated calls with identical parameters are n) |
| runtime.update_event_action_filter | lib | 0.024 | lib-locked |
| runtime.delete_event_action_filter | lib | 0.014 | lib-locked |
| runtime.update_skill_content | lib | 0.017 | lib-locked |
| runtime.update_skill_obs | lib | 0.056 | lib-locked |
| runtime.insert_skill | lib | - | N/A (unique write: repeated calls with identical parameters are n) |
| runtime.create_skill_row | lib | 0.064 | lib-locked |
| runtime.delete_skill_row | lib | 0.017 | lib-locked |
| runtime.delete_skill_by_id | lib | 0.011 | lib-locked |
| runtime.adjust_skill_like_count | lib | 0.015 | lib-locked |
| runtime.insert_runtime_credential | lib | 0.033 | lib-locked |
| runtime.rename_runtime_credential | lib | 0.018 | lib-locked |
| runtime.set_runtime_credential_scope | lib | 0.021 | lib-locked |
| runtime.delete_runtime_credential | lib | 0.014 | lib-locked |
| runtime.upsert_repo_credential | lib | 0.024 | lib-locked |
| runtime.clear_repo_credential | lib | 0.023 | lib-locked |
| runtime.ensure_device_credential | lib | 0.050 | lib-locked |
| runtime.touch_credential_metrics | lib | 0.043 | lib-locked |
| runtime.upsert_local_runtimes | lib | 2.080 | lib-locked |
| runtime.ensure_default_runtime | lib | 0.071 | lib-locked |
| runtime.set_agent_tags | lib | 0.024 | lib-locked |
| runtime.purge_runtime_credential | lib | 0.036 | lib-locked |
| runtime.change_credential_scope | lib | 0.073 | lib-locked |
| runtime.reassign_repo_org_id | lib | 0.099 | lib-locked |
| runtime.read_one | lib | 0.031 | lib-locked |
| runtime.read_all | lib | 0.030 | lib-locked |

## 3. 结论

- 契约 23 方法：lib-locked 热路径 4 项；remote 候选达标 0 项；超阈值 14 项（基线锁 lib，与代码内置基线一致）；采样错误 5 项（基线维持 remote）。
- public 原语：实测 101 项 lib P95；N/A 4 项（方言适配/表结构差异，生产 MySQL 拓扑直跑）。
- get_service() 句柄构造成本 A/B（QA Required#2）：修复前逐次新建代理+Local/Remote 实例 11.491ms/次 → 进程级代理句柄复用后 0.056ms/次（同方法 mgmt.get_bot_config，含一次 remote 真实调用）。
- 热路径四项在基线/矩阵/一键回滚三口径下均恒 lib（见 tests/kernel/test_gsb_route.py）。
- 阈值判读：本拓扑为 loopback HTTP + sqlite 共库——lib P95 基线多在亚毫秒级，remote 固定传输成本约 1.3~4.7ms（**绝对增量全部 ≤10ms 达标**，相对增幅 >20% 不达标）。在「不影响性能」前提下本拓扑 remote 候选=0；**超阈值 14 项已按实测裁决内置基线锁 lib**（QA 审查 Required#1 修复：空表部署与代码行为一致），需要放开的接口经配置表逐 key 显式配 remote（改表 ≤30s 生效，无需重启）；生产三库三机（MySQL 跨机）拓扑对 lib P95 ≥15ms 的重查询逐接口实测达标后，再逐 key 放开 remote。
- ERROR 行均为**存量** schema 漂移（非本改动引入）：runtime.get_issue_state/update_issue_state（issue_state 表已被 Phase C5 迁移收编进 issue 单表后 DROP）、runtime.get_chat_conversations（chat_conversations 缺 backend_session_id 列）、insight.query_metrics/query_slo（obs_metric_rollup/obs_slo 列名漂移）。已单列供后续修复。
