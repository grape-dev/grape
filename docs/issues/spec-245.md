# Spec: Issue #245 — 设备分页筛选后总条数不更新

## Objective
修复 Agent 运行时页面 Workers 设备列表在筛选后底部页码总条数不更新的问题。
筛选（scope/type/搜索）后列表显示筛选结果，但 `_rtDevTotal` 仍为后端全量计数，导致分页页码错误。

## Root Cause
- 后端 `get_runtime_devices()`（`settings.py:663`）仅接收 `page`/`page_size`，不接受筛选参数，`total = len(devices)` 恒为全量。
- 前端 `_loadRuntimeDevices()`（`runtimes.js:98`）仅传 `page`/`page_size`，不传筛选条件。
- 前端 `renderWorkers()`（`runtimes.js:374`）在本地对已分页的 devices 做二次过滤（scope/type/q），导致列表收缩但 `_rtDevTotal` 不变。
- 对比：Engines 列表的 `_loadRuntimesData()` 已将 `scope` 传给后端（`agent-runtimes` 端点支持 scope），故 Engines 分页正常。

## Scope (In)
- 后端 `settings.py` `get_runtime_devices()` 新增 `scope`/`type`/`q` 查询参数；分页切片前过滤；`total` 为筛选后总数。
- 前端 `runtimes.js` `_loadRuntimeDevices()` 传筛选参数；`renderWorkers()` 移除本地 scope/type/q 二次过滤，仅保留排序。
- 后端返回 `avail_scopes`（筛选后设备 scope 分布）供前端 Workers tab 生成筛选标签。

## Scope (Out)
- Engines 引擎列表分页（已有后端 scope 过滤，不受影响）。
- Workers UI 布局/卡片样式变更。
- 新增筛选维度。

## Technical Approach
| 决策 | 选择 | 理由 |
|------|------|------|
| 筛选位置 | 后端过滤 | 前端过滤无法得到正确 total，分页失效 |
| scope 语义 | `credential_id==0` → local；否则取 `d.scope` | 与 `renderWorkers` 原前端逻辑一致 |
| type 匹配 | 设备任一引擎名 includes(type)（小写） | 与前端 `_availTypes` 逻辑一致 |
| q 匹配字段 | name/device_name/owner/worker_ip/runtimes[].name | 与前端原搜索 haystack 一致 |

## Affected Modules
| 模块 | 改动 |
|------|------|
| `src/api/routes/settings.py` | `get_runtime_devices` 新增 scope/type/q 参数 + 过滤 + avail_scopes |
| `src/api/static/js/pages/runtimes.js` | `_loadRuntimeDevices` 传筛选参数 + `renderWorkers` 移除本地过滤 + `_rtDevAvailScopes` |
| `tests/test_api_routes.py` | 新增 runtime-devices 筛选 + total 一致性测试 |

## Acceptance Criteria
- [ ] 筛选后底部总条数与实际筛选结果一致
- [ ] 翻页正常，页码反映筛选后总页数
- [ ] 清除筛选后总条数恢复全量
- [ ] 搜索关键字筛选后总条数正确更新
- [ ] Engines 列表分页不受影响（回归）
- [ ] 所有现有测试通过
