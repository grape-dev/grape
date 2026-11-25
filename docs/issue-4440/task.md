# Task: Issue #4440 — 任务拆解与完成记录

| # | 任务 | 状态 |
|---|------|------|
| 1 | 新增统一交付核实入口 `delivery_verify.py`（核实清单 / PR 查询 / fail-closed 判定） | ✅ |
| 2 | worker 收尾路径挂钩：`service.complete_task` 成功记录前核实，未通过改 failed + 原因 + 错误码，关闭结构化复活通道 | ✅ |
| 3 | direct 收尾路径挂钩：`direct_executor._execute_agent_inner` 终态写入前同口径核实 | ✅ |
| 4 | 未通过不进 #403 瞬时重试（两条路径），直接判死留痕 | ✅ |
| 5 | 回归测试：三类场景 × 两条路径 + 未命中清单零改动 + 源级共用入口守卫 | ✅ |
| 6 | 门禁：新增测试 25 passed；make lint 通过；check-arch PASS；AGENTS.md 回归套件通过（存量失败已基线复核） | ✅ |
