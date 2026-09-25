# Implementation Plan: Issue #245 — 设备分页筛选后总条数不更新

## Overview
将 Workers 设备列表的筛选逻辑从前端二次过滤迁移到后端过滤 + 后端返回筛选后 total，使 `_rtDevTotal` 始终与实际展示数据一致。

## Architecture Decisions
- **后端过滤**：`get_runtime_devices()` 新增 `scope`/`type`/`q` 查询参数，在 `_build_runtime_devices()` 返回后、分页切片前过滤。
- **scope 语义**：`local` → `credential_id==0`；其余取 `d.scope`（与前端原逻辑一致）。
- **type 匹配**：设备承载的任一引擎名（小写）包含 type 字符串。
- **q 匹配**：`name`/`device_name`/`owner`/`worker_ip`/`runtimes[].name` 拼接后小写 includes。
- **avail_scopes**：后端返回筛选后设备的 scope 分布，前端 Workers tab 用 `_rtDevAvailScopes` 独立存储（不影响 Engines tab）。
- **前端移除本地过滤**：`renderWorkers()` 仅保留排序（健康 → 名称），不再做 scope/type/q 过滤。

## Task List

### Phase 1: 后端
- [ ] Task 1: `get_runtime_devices` 新增 `scope`/`type`/`q` 参数 + 过滤逻辑 + `avail_scopes` 返回（`settings.py`）
### Phase 2: 前端
- [ ] Task 2: `_loadRuntimeDevices` 传 scope/type/q + 存储 `_rtDevAvailScopes`（`runtimes.js`）
- [ ] Task 3: `renderRuntimes` Workers tab 用 `_rtDevAvailScopes`；`renderWorkers` 移除本地过滤仅保留排序
### Phase 3: 测试与交付
- [ ] Task 4: 新增 runtime-devices 筛选 + total 一致性测试（`test_api_routes.py`）
- [ ] Task 5: 运行测试套件 + 自审查 → commit → push → PR → Issue 评论 → A2A QA

## Risk
| Risk | Impact | Mitigation |
|------|--------|------------|
| 后端过滤语义与前端原逻辑不一致 | Med | scope/type/q 匹配字段与前端原代码逐一对齐 |
| avail_scopes 共享导致 Engines/Workers 串扰 | Low | 前端用独立变量 `_rtDevAvailScopes`，按 tab 切换 |
| 排序 mutate 原数组 | Low | `renderWorkers` 用 `.slice()` 拷贝后再排序 |

## Verification
- [ ] `python -m pytest tests/test_api_routes.py tests/test_openapi.py -q`
- [ ] 新增测试覆盖 scope/type/q 过滤 + total 一致性 + avail_scopes
