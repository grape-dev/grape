# Task: Issue #3356 — 执行记录

| # | 任务 | 状态 |
|---|------|------|
| T1 | 新增 SWR / 并行 / singleflight 测试 | ✅ |
| T2 | `repos.py` stale-while-revalidate + 线程池并行 + singleflight | ✅ |
| T3 | 更新既有 member 相关测试并回归 | ✅ |
| T4 | 自审查（正确性/范围/测试/回归/安全） | ✅ |
| T5 | commit + push + PR #1747 + Issue 评论 + A2A QA | ✅ |

验收核对（对应 Spec Acceptance Criteria）：
- 缓存过期首个请求返回旧缓存、后台刷新写回 ✅
- `refresh=1` 不产生同步秒级等待 ✅
- 多 provider 并行回源，耗时≈最慢单平台 ✅
- 刷新期间返回正常列表；失败保留旧值并记日志 ✅
- 首次无缓存并行回源，不劣于现状 ✅
- 新增三条路径测试并通过，既有相关测试通过 ✅