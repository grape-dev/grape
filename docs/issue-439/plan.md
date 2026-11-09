# Plan: Issue #439

## 改动点

1. `src/agent/hermes_acp.py`
   - 新增分类白名单常量 + `_overlay_entry_kind` / `_is_reparse_point_or_link` / `_remove_path` / `ensure_overlay_writable_dirs`。
   - 重写 `_build_overlay_home` 镜像段：按条目类型建真实目录/真实空文件/symlink；构建期清理类型不符的遗留旧链接；循环外确保全部可写目录存在为真实目录。
   - `stream_execute` 在 `_build_overlay_home` 后调用 `ensure_overlay_writable_dirs`（worker 兜底，覆盖存量 home 与竞态）。
2. `src/runtime/health_scorer.py`
   - 新增 `_MIN_SAMPLES = env HEALTH_MIN_SAMPLES（默认 5）`。
   - `_compute_health_score`：小样本（0 < total < N）失败率稀释为 failed/N、连败惩罚豁免。
   - `is_healthy`：连败拒派仅在 total_tasks 已知且 ≥ N 时生效。
3. tests
   - `tests/test_hermes_overlay_home_439.py`：分类构建、遗留链接清理、可写目录缺失时补建、只读资产 symlink/降级。
   - `tests/test_health_scorer.py` 追加小样本豁免用例。

## 顺序（TDD）

1. 先写两个测试文件（RED）→ 2. 实现（GREEN）→ 3. 全量回归 `pytest tests/ -q` → 4. 自审查 → 5. PR。

## 风险与对策

- 未知目录改真实目录导致 Linux 缺资产：只读资产白名单（skills/plugins/venv 等）保留 symlink；现有 Linux 测试全过即无回归。
- 健康分小样本放水真实坏引擎：仅稀释+豁免连败惩罚，失败率仍随失败数上升，≥N 后恢复全量惩罚。
