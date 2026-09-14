# Spec: Issue #135 — 健康分加入心跳因素

## Objective
在 #134 健康度模块（`src/runtime/health_scorer.py`）基础上，将心跳新鲜度纳入健康分计算。
引擎若最近无心跳（`last_seen_at` 超过阈值或为 NULL），健康分直接为 0，不参与派发候选。

## 背景
- #134 实现了 `health_scorer.py`，健康分 = round(100 * (1 - failure_rate)) - min(consecutive_failures * 5, 20)
- 心跳判定已存在于 `engine_select.py._health_ok()`（`_RUNTIME_STALE_SECONDS=300s`）和 `health.py.derive_runtime_health()`（5min）
- **缺口**：`health_scorer._compute_health_score()` 不考虑心跳。引擎无心跳但无近期任务时，total=0 返回满分 100，与失联状态矛盾

## 技术方案
1. 在 `health_scorer.py` 新增 `_fetch_heartbeat(runtime_id)` 函数，查询 `agent_runtimes` 表的 `status` + `last_seen_at`
2. 在 `_compute_and_persist()` 中先检查心跳：超时或无记录 → 健康分=0，跳过失败率计算
3. 心跳阈值复用 `engine_select._RUNTIME_STALE_SECONDS=300s`（环境变量 `RUNTIME_STALE_SECONDS` 可调）
4. `_empty_score()` 返回的满分数据不变（runtime_id=0 广播/未绑定不参与过滤）
5. 返回数据新增 `last_seen_at` + `status` 字段，便于前端/分析查看

## 验收标准
- [ ] `health_scorer._compute_and_persist` 中增加心跳检查逻辑
- [ ] 心跳超时（`last_seen_at` 超过 300s 阈值）健康分 = 0
- [ ] 无心跳记录（`last_seen_at` 为 NULL）健康分 = 0
- [ ] 心跳正常时，健康分仍按原失败率公式计算
- [ ] `is_healthy()` / `filter_or_fallback()` 行为正确：无心跳引擎被过滤
- [ ] 新增测试覆盖心跳超时 / 无心跳记录 / 心跳正常三种场景
- [ ] 现有测试全部通过，广播/派发机制无回归

## 代码风格
- Python 3.12，类型标注 `int | None`
- 日志用 `from src.config import log`
- DB 访问用 `_lock()` + `_db().cursor()` 模式
- 时间用 `datetime.now(timezone.utc)`

## 测试策略
- 框架：pytest
- 测试文件：`tests/test_health_scorer.py`（已有，扩展）
- 覆盖：心跳超时→0分、无心跳→0分、心跳正常→正常分、status 非 online→0分
- mock DB 查询验证 `_fetch_heartbeat` 逻辑

## 边界
- Always: 运行测试后提交，不改变广播/派发语义
- Never: 修改非 Issue 相关代码，修改 DB schema
