# Issue #605 实现方案（plan）

## 根因分析（代码层取证结论）

派发链路走查（`src/api/event_pipeline.py` → `src/runtime/engine_select.py` → `src/runtime/service.py`）：

1. plugin 模式下 `pick_engine` 统一返回 0（广播），实际派发范围由
   `get_scene_pool_engine_ids()` 从场景 node 的 `engine_runtime_ids` 读取并按
   `_health_ok`（status=online + last_seen_at 新鲜度）过滤后写入 `agent_tasks.pool_engine_ids`。
2. worker 侧 `claim_by_tag()` 认领时，若本引擎不在 `pool_engine_ids` 名单内 → **静默跳过该任务**
   （`src/runtime/service.py` claim_by_tag / claim 的 pool 过滤分支，无任何日志）。
3. 因此 hermes 0 派发的候选解释空间收敛为：
   - a) 健康信号误判（`_health_ok` 的 stale_heartbeat / healthy=0 / status!=online 把 17004 过滤出 pool）；
   - b) 候选过滤缺陷（tags/scope/健康分过滤对 hermes 类引擎误杀）；
   - c) 场景 node `engine_runtime_ids` 未绑定 17004（任务创建时绑定策略）。
   前两者发生在 `get_scene_pool_engine_ids`/`pick_engine`，后者是场景配置数据。
4. **可观测性缺口（本 PR 修复的主缺陷）**：以上每一处过滤都是静默的——
   `get_scene_pool_engine_ids` 过滤无日志、`pick_engine` 各档位过滤无日志、
   `claim_by_tag` pool 名单不匹配跳过无日志。这正是 Issue #605「需主控补 engine_select 证据」
   无法回答的直接原因：服务端没有任何决策留痕。

## 改动（对应 Spec 实现路径 1/2/3）

| 模块 | 改动 | 说明 |
|------|------|------|
| `src/runtime/engine_select.py` | 新增 `_decision_log`、`_candidate_reject_reasons`、`explain_engine_selection`；`pick_engine`/`get_scene_pool_engine_ids` 全路径决策留痕 | 结构化单行 JSON 日志 `engine_select_decision`：候选列表 + 每个候选被过滤原因 + 选中理由（档位/priority/rule）或广播 pool |
| `src/runtime/service.py` | `claim_by_tag` 与 legacy `claim` 的 pool 名单跳过分支补结构化日志（带 60s 去重防刷屏） | 消除「引擎不在广播名单被静默跳过」不可观测问题（#605 取证关键缺口） |
| `src/api/routes/dashboard.py` | 新增只读接口 `GET /api/dashboard/ops/engine-select-explain` | 主控侧取证工具：输入 repo/agent_type/scene_id，返回候选全集 + 每引擎过滤原因 + 健康分 + pool 结果，直接回答 Spec 取证问题 1/2 |

不改动派发语义（广播协议/过滤条件/调度顺序均保持不变），仅补观测 + 取证接口。

## 验证

- 新增 `tests/test_engine_select_decision_log.py`：候选过滤原因（stale heartbeat / healthy=0 / status offline）在决策日志与 explain 结果中可见；claim_by_tag pool 跳过日志；正常派发选中理由日志。
- 回归：`tests/test_engine_select.py`、`test_claim_by_tag_sql_fix.py`、`test_issue_253_engine_binding.py`、`test_local_claimer.py` 全量通过。
- 生产侧回归（部署后）：派发一批 `agent:vod` 任务，`devops-bot.log` 中 `engine_select_decision` 行可直接解释每个任务 hermes 是否在候选、被什么条件过滤；claim 跳过日志解释 worker 侧视角。
