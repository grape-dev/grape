# Plan: Issue #473 — runtime 列表范围筛选实现方案

## 测试 seam
- 后端公开端点：`GET /settings/agent-runtimes`、`GET /settings/runtime-devices`（直接调用路由函数，新增 `range` 查询参数）。
- 前端：`runtimes.js` 源断言测试（对齐 tests/js/test_issue_364 风格）。

## 后端（src/api/routes/settings.py）
1. 新增共享 helper `_rt_in_range(scope, owner, is_local, rt_range, uname)`：
   - `rt_range` 空/`all`/`global` → True（现有全量口径兜底）
   - `repo` → `scope == 'repo'`（配合既有 #277 `repo` 参数收窄到当前仓库）
   - `user` → `scope == 'user' and owner == uname`，或本机引擎 `is_local and owner == uname`
2. `get_agent_runtimes(..., range: str = "")`：可见性过滤后，先对 `visible_runtimes` 与 `devices` 施加范围过滤，再走既有 scope 筛选 → avail_scopes → 分页（先范围后搜索后分页；吊销过滤在可见性阶段已排除）。
3. `get_runtime_devices(..., range: str = "")`：`avail_scopes/avail_types` 取全量可见（#245 口径，防筛选标签消失），随后 **范围 → type → q → scope → 分页** 依次过滤，total 为组合筛选后总数。
4. 参数名 `range` 与既有 `type` 同风格（Query 参数，不与前端 scope 标签冲突）。

## 前端（src/api/static/js/pages/runtimes.js）
1. 新增状态 `_rtRange = 'all'`（all/repo/user）与 `_RT_RANGE_LABELS`。
2. `_loadRuntimesData` / `_loadRuntimeDevices` 透传 `&range=`（非 all 时）。
3. 筛选栏最前新增「范围」分组 chips（全部/仓库/个人），高亮当前选中即选中范围提示；`_rtSetRange()` 切换后重置页码并按当前 tab 重载；`_rtClearFilters` 一并清除范围。

## 测试（tests/test_issue_473_range_filter.py + tests/js/test_issue_473_range_filter.js）
- Python（seed 同 #371 风格：user scope + repo scope + global scope 三凭据）：
  - range=repo / user / 空 / all 各口径（engines + devices 两端点）
  - range + repo 参数组合（#277 不回退）
  - range + q 搜索 + scope + 分页组合，total 正确
  - 空结果返回空列表（#436 不回退）
- JS：源断言（状态变量、参数透传、chips 渲染、清除联动）。

## PR #668 审查修复（追加）
1. `_rt_in_range` 新增 `local_owner` 参数：is_local 条目以引擎 DB 行 owner_id 判定归属（cid=0 输出 owner 恒空，原判定不可达）；远端条目沿用输出 owner + scope==user。
2. `_build_runtime_devices`：本机设备行（含挂到主控机凭据设备的情况）补充 `local_owner_id`。
3. 两端点 range 参数升级 `RTRange = Literal["", "all", "repo", "user"]`：非法值 422，不静默回落全量。
4. 测试：tests/test_issue_473_local_engine_range.py（cid=0 本人/他人/无主 + 非法 range 422 + 合法值回归）；tests/js/test_issue_473_range_filter.js 断言同步。
5. CI：windows-smoke.yml 新增 linux-backend job（pytest #473 两文件 + node 源断言）。

## TDD 提交拆分
1. `test(#473): 范围筛选失败测试`（红）
2. `feat(#473): 后端范围过滤 + 前端范围控件`（绿）
3. `test(#473): PR #668 审查修复先红——本机引擎归属 + 非法 range 422`（红）
4. `fix(#473): PR #668 审查修复——本机引擎 DB 行 owner_id 归属 + Literal 校验 + CI 接入`（绿）
