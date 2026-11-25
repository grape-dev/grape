# Issue #3904 任务拆解（Task）

| # | 任务 | 文件 | 验证 |
|---|------|------|------|
| T1 | 同源清单常量：`_OVERLAY_NOT_MIRRORED` / `_OVERLAY_REQUIRED_ENTRIES`，合并内联跳过元组与网关控制文件清单 | `src/domains/runtime/agents/hermes_acp.py` | 常量交集为空（用例 1） |
| T2 | `profiles` 归入只读共享资产（symlink/junction/复制）；构建按 `_OVERLAY_NOT_MIRRORED` 跳过 | 同上 | 含 profiles 的 src → overlay 可用（用例 2） |
| T3 | 自检改为「必需 − 跳过」求值，拆出 `_overlay_unusable_reasons`，`_overlay_looks_usable` 布尔签名不变 | 同上 | 缺 tools/installs 仍不可用（用例 4） |
| T4 | 构建返回梯次备注；回退分支单行结构化日志 + `overlay_fallback_total()` 计数 | 同上 | 用例 3/5 + 源码断言 |
| T5 | usage_collect 回退感知：overlay → 共享 `HERMES_HOME` 两候选；缺失降 INFO/DEBUG | `src/domains/runtime/engine/usage_collect.py` | 用例 6/7 |
| T6 | 文档：spec/plan/task + Issue 评论 | `docs/issue-3904/` | — |
| T7 | 自审查（5 轴 + 开源前置增量检查）、建 PR、A2A 交 QA | — | `make lint`、`pytest`、`scripts/check-arch.py` |

## 顺序

T1→T2→T3→T4（同文件一次改完，按 TDD 先落用例再改实现），T5 独立，T6 并行，T7 收尾。