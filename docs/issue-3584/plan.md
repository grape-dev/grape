# Plan: Issue #3584

## 任务拆解

| # | 任务 | 状态 |
|---|------|------|
| 1 | `_engine_loads` + `_attach_loads` 负载采集（复用 `_real_active_count` 口径） | 完成 |
| 2 | `_pick_decision`：档内最小负载 + seq 取模轮转 + 决策日志 | 完成 |
| 3 | `pick_engine` legacy 多引擎分支替换选择键（SRE 同走通用链，Issue #3396） | 完成 |
| 4 | `explain_engine_selection` 候选补 load | 完成 |
| 5 | 新增测试：分摊/轮转/日志/单点塌陷/SRE 通用链/取证 | 完成 |
| 6 | 回归既有 engine_select / engine_binding 测试集（无新增失败） | 完成 |

## 影响面

- 只改 `src/domains/runtime/engine/engine_select.py` 与新增测试；
  plugin 广播、claim 认领、engine-selection.json 配置语义均不变（rule=order 默认）。

## 回滚

- 单文件 revert 即可恢复旧 `min(priority, runtime_id)` 行为。
