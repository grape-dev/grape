# Task: Issue #1310 — 引擎显示名统一「引擎名（凭据名）」

## 切片清单（一竖切片 = 一测试 + 一实现）

| # | 切片 | 红测试 | 绿实现 |
|---|------|--------|--------|
| 1 | engineDisplayName 统一取值（worker-name.js） | tests/js/test_issue_1310_engine_display_name.js | worker-name.js |
| 2 | runtimes.js 全部展示位（排序/搜索/卡片/复制/chip/详情/alias 回写） | tests/js/test_issue_1310_engine_display_render.js | runtimes.js |
| 3 | agents.js / autopilot.js / dashboard.js 下拉显示 | tests/js/test_issue_1310_engine_display_render.js（扩展） | agents.js / autopilot.js / dashboard.js |
| 4 | 后端下拉数据源补字段（agents.py / dashboard.py / settings.py devices 子列表） | tests/test_issue_1310_engine_display_name.py | 三个后端文件 |

## 实施顺序
1. 切片 1 红 → 绿
2. 切片 2 红 → 绿
3. 切片 3 红 → 绿
4. 切片 4 红 → 绿
5. 回归：`pytest tests/test_issue_1310_engine_display_name.py tests/test_issue_227_engine_modal.py tests/test_api_routes.py -x -q` + 全部 tests/js node 测试
6. 自审查（5 轴 + 增量敏感/无关内容检查）→ 建 PR → 评论告知 → A2A QA → tasks complete