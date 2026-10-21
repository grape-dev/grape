# Plan: Issue #404 — 场景绑定引擎全部离线缺乏告警

## 实现路径

### 1. 告警侧链（src/api/event_pipeline.py）

**新增常量与状态**：
```python
import time as _time
_SCENE_OFFLINE_ALERT_TTL = 3600  # 1h 静默期（spec 设计决策）
_scene_offline_alert_sent: dict[str, float] = {}  # scene_id -> 上次告警时间戳
_scene_offline_alert_lock = threading.Lock()
```

**新增 `_emit_scene_offline_alert(scene_id, agent_type, reason, repo_key="")`**：
- TTL 去重：`_scene_offline_alert_sent` 内查 `scene_id`，若 `now - ts < TTL` 则跳过（返回 False）。
- 命中则写时间戳，构造合成 `rule`/`event` dict，调用 `src.observability.alerts._notify_webhook`。
- `_notify_webhook` 读 `OBS_ALERT_WEBHOOK_URL`，未配置则只 log（与现有告警降级一致）。
- 飞书通道：尝试在事件循环里 `asyncio.run_coroutine_threadsafe` 调用 `_send_event_text`；
  无运行中循环则降级为 webhook-only（避免阻塞 pipeline 主线程）。
- 异常吞掉（告警侧链永不影响主流程），只 log。

**修改 `_fail_scene_all_offline`**：
- 在 `_fail_event(seq, _reason)` 之后追加 `_emit_scene_offline_alert(scene_id, agent_type, _reason, repo_key=repo_key)`。

### 2. 引擎恢复钩子（src/observability/engine_probe.py）

**新增 `_on_engine_recovery(engine_name: str)`**：
- 反查绑定该引擎的场景：扫 `devops_pipeline_scene` 表的 `nodes_json`，找 `engine_runtime_ids` 含该引擎
  （或引擎名匹配——engine_name 对应 runtime_id 需经 `agent_runtimes` 表反查）的场景。
- 对每个命中场景，调 `get_scene_pool_engine_ids(scene_id, agent_type, repo_key)` 判定 pool 是否非 None
  （非 None = 引擎已恢复）。
- pool 非 None 时：log "scene recovered, N failed events redispatchable"，统计该场景下
  `events.status='failed' AND events.result LIKE '%场景绑定的引擎全部离线%'` 的事件数。
- 不自动重派（spec：手动一键）。

**修改 `_probe_loop`**：
- 在 `_probe_results[eng["name"]] = result` 前记录 `_prev_status`。
- 若 `_prev_status == "fail"` 且 `result["status"] == "ok"` → 调 `_on_engine_recovery(eng["name"])`。
- 同样在 `probe_all_now`（手动探测）中接入。

### 3. 一键重派列表端点（src/api/routes/events.py）

**新增 `GET /events/redispatchable`**：
- Query 参数：`scene_id`（可选）、`agent_type`（可选）、`repo_key`（可选）、`limit`（默认 50）。
- 查 `events.status='failed' AND events.result LIKE '%场景绑定的引擎全部离线%'`。
- 逐条校验：调 `get_scene_pool_engine_ids(scene_id, agent_type, repo_key)`，非 None → 可重派。
- 返回 `{"items": [{seq, scene_id, agent_type, issue_id, repo_key, failed_at, result}], "total": N}`。
- 实际重派走现有 `POST /events/{seq}/redispatch`（events.py:379/762，内部已重读 pool 校验）。

### 4. 场景配置校验（src/api/routes/scenes.py）

**修改 `update_scene`**：
- 在 `update_custom_scene(...)` 调用前，遍历 `nodes_json`：
  - 对每个有 `engine_runtime_ids` 的 node，调 `get_scene_pool_engine_ids(scene_id, node.tag, repo_key)`。
  - 返回 `None`（全灭）→ 收集 warning：`"节点 {tag} 绑定的引擎当前全部离线，建议检查或更换绑定"`。
- `update_custom_scene` 正常执行（不阻断）。
- 返回体改为 `{"message": "场景更新成功", "warnings": [...]}`（空数组 = 无 warning）。

### 5. 测试（tests/test_issue_404_scene_offline_alert.py）

5 条验收单测，DB 隔离模式参考 `tests/test_issue_259_offline_dispatch.py`：
1. `test_scene_all_offline_fails_event_with_scene_and_agent` — 全灭 → 事件 failed，result 含 scene_id + agent_type。
2. `test_redispatch_after_engine_recovery` — 引擎恢复 online → `GET /events/redispatchable` 返回该事件；
   调 `POST /events/{seq}/redispatch` → 新任务派发成功，原事件终态不变。
3. `test_alert_dedup_within_1h` — 同场景两次 `_fail_scene_all_offline`（间隔 <1h）→ `_notify_webhook` 只调一次。
4. `test_pool_with_fallback_engine_claim_ok` — pool 有兜底在线引擎 → `get_scene_pool_engine_ids` 非 None，
   claim 正常路径不受影响（回归 #259 已有断言）。
5. `test_empty_scene_no_false_offline_alert` — 空场景（无 engine_runtime_ids）→
   `get_scene_pool_engine_ids` 返回 ""，`_emit_scene_offline_alert` 不被触发（断言告警字典为空）。

## 原子提交计划
- commit 1: `feat: #404 离线告警侧链 + TTL 去重`（event_pipeline.py）
- commit 2: `feat: #404 引擎恢复钩子`（engine_probe.py）
- commit 3: `feat: #404 一键重派列表端点`（events.py）
- commit 4: `feat: #404 场景配置离线校验 warning`（scenes.py）
- commit 5: `test: #404 5 条验收单测`（tests/）

## 风险与缓解
- **告警侧链阻塞主流程**：全部 try/except 吞掉，飞书异步投递用 `run_coroutine_threadsafe`，无事件循环时降级 webhook-only。
- **内存字典重启丢失**：spec 设计决策已允许（"内存字典重启丢失可接受降级"）；重启后首次离线会重新告警一次（可接受）。
- **`_on_engine_recovery` 反查场景性能**：扫 `devops_pipeline_scene` 全表，行数有限（场景表通常 <1000 行）；
  仅在探测恢复时触发（10min 周期），非热路径。
- **`GET /events/redispatchable` 逐条校验 pool**：limit 50，每条一次 `get_scene_pool_engine_ids`（带缓存可优化，
  首版不加缓存）。
