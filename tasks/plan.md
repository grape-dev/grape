# Implementation Plan: Issue #135 — 健康分加入心跳因素

## Overview
在 `health_scorer.py` 的健康分计算中增加心跳检查：引擎 `last_seen_at` 超时或为 NULL 时健康分直接为 0，优先于失败率计算。

## Architecture Decisions
- **心跳检查优先于失败率**：心跳不在线的引擎直接返回 0 分，不参与失败率计算
- **阈值统一**：复用 `engine_select._RUNTIME_STALE_SECONDS=300s`，通过环境变量 `RUNTIME_STALE_SECONDS` 可调
- **status 也纳入判定**：`status != 'online'` 且 `status != 'active'` 时也视为不在线（与 `health.py.derive_runtime_health` 一致）
- **不改变公共接口签名**：`get_health_score` / `is_healthy` / `filter_or_fallback` 签名不变，返回数据新增 `last_seen_at` + `status` 字段

## Task List

### Phase 1: 核心实现
- [ ] Task 1: 新增 `_fetch_heartbeat()` 函数 + 心跳阈值常量
- [ ] Task 2: 在 `_compute_and_persist()` 集成心跳检查
- [ ] Task 3: `_empty_score()` 和返回数据加入 `last_seen_at` / `status` 字段

### Phase 2: 测试
- [ ] Task 4: 新增心跳相关测试用例（超时/无心跳/正常/非online状态）
- [ ] Task 5: 确保现有测试全部通过

### Checkpoint: Complete
- [ ] 所有验收标准满足
- [ ] 现有测试无回归

## Risks and Mitigations
| Risk | Impact | Mitigation |
|------|--------|------------|
| 心跳查询增加 DB 开销 | Low | 与 `_fetch_task_stats` 合并在同一 lock 内执行 |
| 现有 mock 测试受影响 | Low | 心跳查询 mock 为返回正常心跳，保持原行为 |
