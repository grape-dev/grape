# Plan: Issue #472 全局统计口径

## 方案
1. 后端 `GET /settings/agent-runtimes`（src/api/routes/settings.py::get_agent_runtimes）：
   - 新增查询参数 `q` / `type` / `scene`（逗号分隔多选）/ `tag`（逗号分隔多选），谓词与前端 `_filterEngines` 一致（haystack=alias/instance_name/name/worker_name/owner/host_name/agent_tags/local_skills；type 匹配 instance_name||name；scene/tag 任一命中，tag 含 'all' 豁免）。
   - 在 scope 过滤后、分页前应用；`avail_scopes/avail_types/avail_scenes/avail_tags` 从全量可见集派生（筛选标签不随筛选消失）。
   - 新增返回 `health_stats = {total, online, recently_lost, offline, about_to_gc}`（筛选后全集聚合）。
   - 参数为空时行为与旧版完全一致（page_size=0 旧调用方 autopilot/agents 不受影响）。
2. 后端 `GET /settings/runtime-devices`：在 scope/type/q 过滤后、分页前计算 `health_stats` 并返回。
3. 前端 `src/api/static/js/pages/runtimes.js`：
   - `_loadRuntimesData` 请求带上 q/type/scene/tag；`_loadRuntimeDevices` 保存 `_rtDevHealthStats`。
   - `_summaryStatsBar` 数据源切换：engines→`data.health_stats`，workers→`_rtDevHealthStats`；无 health_stats 时回退旧口径（兼容缓存数据）。
   - `renderEngines` 不再本地过滤（后端已过滤）；删除 `_filterEngines`；`refreshRuntimes` 后台补数据同样带筛选参数。
   - Engines 筛选标签改用后端返回的 avail_types/avail_scenes/avail_tags。

## 测试 seam
- 后端 HTTP 接口（TestClient）：/api/settings/agent-runtimes、/api/settings/runtime-devices 的 health_stats 与筛选参数。
- 前端静态断言（沿用 #432 的 tests/test_runtimes_stats_i18n.py 模式）。

## 任务拆解（TDD）
1. test: 新增 tests/test_issue_472_global_stats.py 失败测试（红）——health_stats 全局口径 + 筛选参数 + 稳定性。
2. feat: 后端两端点实现 health_stats + engines 筛选参数（绿）。
3. test: 更新 test_runtimes_stats_i18n.py 断言为新口径（红）。
4. feat: 前端 runtimes.js 切换数据源（绿）。
5. 回归：test_api_routes.py 相关用例 + test_runtimes_stats_i18n.py 全量。
