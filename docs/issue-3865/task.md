# Task: Issue #3865

| # | 切片 | 产出 | 提交 |
|---|---|---|---|
| 1 | 文档 | `docs/issue-3865/{spec,plan,task}.md` | `docs(#3865)` |
| 2 | 失败测试（红） | `tests/runtime/test_3865_runtime_domain_reads.py` | `test(#3865)` |
| 3 | service.py org-context 域读 + `context_degraded` | `src/domains/runtime/engine/service.py` | `feat(#3865)` |
| 4 | webhook.py 去重/取消/跨仓域读 + 降级标记 | `src/domains/runtime/routes/webhook.py` | `feat(#3865)` |
| 5 | 全量回归 + 自审查 + PR | pytest / PR | — |

验收钩子：三库/单库回归用例（A–H）全绿；AST 守门确认读取点无 `db.cursor()`。