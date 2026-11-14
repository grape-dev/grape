# Task: Issue #2479 — 任务拆解与执行清单

| # | 任务 | 产出 | 状态 |
|---|------|------|------|
| 1 | 需求文档 | docs/issue-2479/spec.md | done |
| 2 | 方案设计 | docs/issue-2479/plan.md | done |
| 3 | 路由配置表 + 缓存 + key registry | src/kernel/gsb/route_config.py | done |
| 4 | provider 按方法路由（矩阵守卫/代理/fail-fast） | src/kernel/gsb/provider.py | done |
| 5 | client 错误码 + metrics 区分 | src/kernel/gsb/client.py | done |
| 6 | 单测（路由/热切换/矩阵/fail-fast/回滚/同步/扫描） | tests/kernel/test_gsb_route.py | done |
| 7 | CI 静态扫描 + 接入门禁 | scripts/check_gsb_route_matrix.py, scripts/check-all.sh | done |
| 8 | 建表迁移脚本 | scripts/migrations/006_gsb_provider_route.py | done |
| 9 | 压测脚本 + 报告 | scripts/bench_gsb_route_p95.py, docs/issue-2479/bench-report.md | done |
| 10 | 设计文档更新（按方法路由与禁止矩阵章节） | docs/lib-3db-3host-design.md | done |
| 11 | 自审查 + 测试全绿 + 提交 | feat/issue-2479 | done |

执行顺序遵循 TDD：先 route_config 测试（红）→ 实现（绿）→ provider/client（红→绿）
→ 扫描/迁移/压测收尾。
