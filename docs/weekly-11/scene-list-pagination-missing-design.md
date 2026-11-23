# 项目场景页「翻页不生效」记录与修复（GET /api/scenes/with-pipeline 不分页）

状态：后端已修复并已部署到四台主控；前端缺口未修（见第 5 节）。

## 1. 现象

项目场景页（Dashboard 场景中心）场景数量超过一屏出现分页条，点击第 2/3 页后仍显示第一页；同时
打开场景页/翻页时会把整个仓库全部场景（含每个场景的 nodes json 与服务名解析）拉一遍，
在场景多的仓库上明显变慢，并且与其他页面的请求一起排队。

## 2. 根因

后端 `src/domains/mgmt/routes/scenes.py::list_scenes_with_pipeline` 只接受 `repo` 一个参数：

    def list_scenes_with_pipeline(user=Depends(...), repo: str = ""):
        scene_list = get_scenes(repo_key=repo_key)     # 全量
        ...
        _total = len(result)                            # total = 当前数组长度

前端确实传了 `page` / `page_size`（`static-dist` 构建产物与 `shell/js/app.js` 都会拼
`&page=N&page_size=20`），但后端完全无视这两个参数：不 LIMIT/OFFSET、不做 COUNT，
`total` 恒等于「本次返回的条数」。于是：

- 每次响应都是全量，页大小由前端决定不了；
- 分页条总页数由 `total` 算，而 `total` 永远是全量条数 —— 分页条看起来正常，
  但翻页后的数据仍然是第一页（因为查询没有位移）；
- 场景多的仓库上，翻页 = 再拉一遍全量 + 再做一遍技能显示名解析。

归因（git）：分页最初由 Issue #501 提交 `caded1c4c` 加在旧路径 `src/api/routes/scenes.py`，
`/scenes` 拿到分页、`with-pipeline` 没有。Issue #699（11-03）把路由重组到 `src/domains/`
时，`with-pipeline` 搬的是更早的版本，分页/检索/排序参数整体丢失；
`origin/weekly-11-UI` 分支同样是缺失状态。仓库里 `tests/mgmt/test_scene_list_pagination.py`
已经把这份契约写成用例，但 `with-pipeline` 那 4 条在 Vue 迁移时被一并 `skip` 掉了，
所以回归一直没人发现。

补充证据（接口时延观测）：该接口在场景较多的仓库上单次响应可达秒级到数十秒，并出现客户端超时；
全量返回 + 逐场景解析是这部分耗时的直接来源。

## 3. 修复（后端，本次）

`list_scenes_with_pipeline` 接受与 `/scenes` 完全相同的参数与语义：

- `page`（可选，`ge=1`）：不传 = 全量返回（老调用方零改动）；传了 = SQL `LIMIT ? OFFSET ?`
- `page_size`（默认 20，`1..100`）
- `q`（默认空，`max_length=100`）：与 `/scenes` 同 LIKE 语义（name/display_name/description）
- `sort`（`id` | `latest`）：与 `/scenes` 同序（含置顶序 is_pinned/pinned_at）
- `total` 恒返回，取 `_count_scenes_from_db(repo_key, q=q)`（COUNT 不拉 nodes_json），
  COUNT 为 0 时回退 `len(result)` 兜底
- 技能显示名解析（enrich）只对当前页执行，翻页不再为看不到的行付成本
- FastAPI 直调（测试）时 `Query` 默认值是对象而非 int，函数内做类型规范化，避免 `page` 判真出错

不改前端即可生效的前提是：前端必须发 `page`。当前部署的 Vue 构建产物**不发**，见第 5 节。

## 4. 验证

- `tests/mgmt/test_scene_list_pagination.py`：4 条被 skip 的 `with-pipeline` 契约用例恢复执行
  （分页条数/total、q 同语义、分页 enrich 与全量一致、探针字段已移除），全部通过；
  另修正 2 条因 P1 模块归位后仍读 `src/api/pipeline_config.py` 别名而失败的用例，改读 canonical
  `src/domains/mgmt/pipeline_config.py`。
  结果：26 passed / 23 skipped（基线 22 passed / 27 skipped）。
- 基线对照（同一提交导出到临时 worktree，detached `8abf65ba8`）：`test_scene_pin.py` 2 条失败在基线上同样失败，
  与本次改动无关（DDL/审计表断言，环境性）。

## 5. 遗留：前端仍然不会翻页（需要单独一步）

部署的 `static-dist/viewjs/dashboard-view.js` 里场景列表请求是

    /scenes/with-pipeline?repo=<owner/name>        # 不带 page / page_size

而 `sceneGotoPage(p)` 只是把 `window._sceneListPage = p` 然后 `window._sceneReload()` 重载，
重载后的请求依旧不带 page；模板又是 `v-for="(s, idx) in scenes"` 直接渲染全部，不做本地切片。
因此即使后端现在能分页，前端仍会每次取第 1 页。要真正修好，前端需要：

1. 场景列表请求带上 `&page=` + `&page_size=`（读 `window._sceneListPage` / `_sceneListPageSize`，
   与 `shell/js/app.js` 的写法一致）；
2. 依赖后端返回的 `total` 渲染分页条（已经是 `_scenesTotal`，无需改）；
3. 重新构建前端（`npm ci && vite build` → 覆盖 `src/api/frontend/static-dist`）后发版。

## 6. 影响面

- 只动 `list_scenes_with_pipeline` 一个路由函数，签名新增 4 个可选参数；不带 `page` 时行为与线上一致
  （全量 + total=条数），其余 4 处 `with-pipeline` 调用点（技能链补拉、编辑弹窗、删除后刷新）不受影响。
- `GET /api/scenes` 未改动。
- 未改 `get_scenes` / `_count_scenes_from_db`（mgmt 域实现已支持 limit/offset/q/sort 与置顶序）。
