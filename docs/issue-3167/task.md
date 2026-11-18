# Issue #3167 任务拆解

| # | 任务 | 文件 | 状态 |
|---|------|------|------|
| 1 | 新增 `_org_key(v)` 归一函数（哨兵族折叠 + 去空白字符串） | `src/domains/mgmt/routes/settings.py` | 已完成 |
| 2 | `_org_unassigned()` 改为 `_org_key(v) == ""`（消除 `.strip()` 崩溃点） | `src/domains/mgmt/routes/settings.py` | 已完成 |
| 3 | `get_agent_runtimes` 可见性比较统一为字符串口径（含删除 `in ("0", 0)` 手工折叠） | `src/domains/mgmt/routes/settings.py` | 已完成 |
| 4 | `get_runtime_devices` 可见性比较统一为字符串口径（cred_map / cached 两处） | `src/domains/mgmt/routes/settings.py` | 已完成 |
| 5 | `_org_unassigned` 六值单测 + `_org_key` int/str 等价 | `tests/mgmt/test_issue_3167_org_unassigned_int.py` | 已完成 |
| 6 | 端点级回归：个人组织 + 历史 int `org_id` 行 → 200 且可见 | `tests/mgmt/test_issue_3167_org_unassigned_int.py` | 已完成 |
| 7 | 相关既有用例回归（org 隔离 / range 筛选 / settings） | `tests/` | 已完成 |
