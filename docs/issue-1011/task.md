# Issue #1011 任务拆解（Task）

| # | 切片 | 交付 | 状态 |
|---|---|---|---|
| T0 | anchor 快照列迁移 + 注册路径持久化锚 | db_setup.py / runtime.py register | ✅ |
| T1 | 存量重派生回填迁移（幂等 + 对账报告落盘） | db_setup.py | ✅ |
| T2 | 已盖章库迁移报告补写（646/868/1011） | db_setup.py | ✅ |
| T3 | v2 claim 系端点 ×5（claim/by-tag/bound/by-tag-bound/capabilities）+ uid 归属校验 | routes/runtime.py | ✅ |
| T4 | heartbeat-v2（engine_uids 优先 + 回退 ids + 全失效不造行） | routes/runtime.py | ✅ |
| T5 | 上报 v2（complete/fail/progress 按 uid 校验归属） | routes/runtime.py | ✅ |
| T6 | autopilot 后端姊妹字段 + scheduler uid 池路径 + 升格脚本 | routes/autopilot.py / scheduler.py / scripts/ | ✅ |
| T7 | autopilot 前端 uid 优先 id 兜底 | static/js/pages/autopilot.js | ✅ |
| T8 | v2 spec §2.1/2.2 勘误 | docs/issue-868/claim-by-uid-v2-protocol-spec.md | ✅ |
| T9 | worker v2-first/v1-fallback + 404 二分 + uid 持久化 | runtime/client.py / agent.py | ✅ |
| T10 | 原子守卫测试 + linux-backend CI 挂载 | tests/atomic / windows-smoke.yml | ✅ |

验证：`python3 -m pytest tests/atomic/test_issue_1011_*.py`（详见各测试文件头部）。
