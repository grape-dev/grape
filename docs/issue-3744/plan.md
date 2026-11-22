# Issue #3744 方案设计

> 目标合入分支：`arch-weekly-11-ljt` | 2026-11-22

## 方案要点

1. **共享常量**（`scheduler.py`）
   - 新增 `_AUTOPILOT_ISSUE_ID_PREFIX = "autopilot-"`，与既有
     `_AUTOPILOT_EVENT_SEQ_BASE = 900_000_000` 相邻；
   - `_autopilot_issue_ref()` / `_execute_autopilot_remote()` 的合成键改用该常量，
     消除字面前缀散落。

2. **统一来源判定**（`event_pipeline.py`）
   ```python
   def _is_autopilot_task(issue_id: str = "", event_seq: int = 0) -> bool:
       # 延迟 import scheduler 常量，避免模块级循环依赖
       ...
       return issue_id.startswith(_AUTOPILOT_ISSUE_ID_PREFIX) \
           or (event_seq and int(event_seq) >= _AUTOPILOT_EVENT_SEQ_BASE)
   ```
   - 前缀为主、seq 常量为辅；常量导入失败时保守返回 False（不误杀真实任务）。

3. **入口短路**（`reconcile_task_scene_env`）
   - 取出 `agent_tasks` 行字段后、`_pr_subscription_state` 之前判定；
   - 命中即 `log.debug("[scene-env] skip scene reconcile: autopilot ... scene_reconciled=false reason=autopilot_source")`
     并返回快照 `GRAPE_SCENE_ID`，不触发 events 查询、不产生 WARNING；
   - `_pr_subscription_state` 及其 `None` 语义零改动。

4. **告警可定位**（非 autopilot）
   - `_pr_subscription_state` / `_is_pr_subscription_task` 追加可选参数
     `task_id` / `repo_key`（默认空，兼容 `routes/events.py` 现有调用）；
   - events 缺行/查询异常 WARNING 增补 `task=<task_id> repo=<repo_key>`，级别与主体不变。

## 测试 seam

- 公开 seam：`reconcile_task_scene_env(task_id, env)` 返回值 + 环境变量；沿用
  `tests/runtime/test_issue_1745_scene_env_truth.py` 的真实 SQLite 夹具。
- 判定 seam：直接单测 `_is_autopilot_task()` 的前缀/常量边界。
- “不查 events”以 monkeypatch `_pr_subscription_state` 未被调用 + WARNING 未产生佐证。