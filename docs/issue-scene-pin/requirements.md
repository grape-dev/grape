# 项目场景列表「置顶」功能 — 需求 + 实现规格

- 目标分支：`weekly-mys`（本单跑在「运行域-任务编排专用开发流水线」，work_branch=weekly-mys）
- 参考基线：`origin/weekly-mys` @ `7875cb4d1`（另与 `origin/weekly-10` @ `09ae47a04` 交叉核对；两分支 `pipeline_config.py` 内容一致）
- 行号仅作参考，**一律以函数名/字段名锚定**；开工时以 `git log --oneline -1` 的实际基线为准

---

## 背景

「项目场景」主页面（dashboard 的场景中心列表）目前只能按 `id` 或创建时间排序，无法把常用场景固定在列表最上方。
已核对现状（只读）：

- 前端列表数据源：`GET /scenes/with-pipeline?repo=<owner/repo>&page=&page_size=&q=&sort=`（`app.js` 的 `loadDash` 调用；渲染在 `dashboard.js` 的 `_renderSceneCards` / `_renderLocalSceneCard`）
- 两条列表接口（`/scenes`、`/scenes/with-pipeline`）共用同一个查询函数：`src/api/pipeline_config.py` 的 `_query_scenes_from_db`，其排序写死在函数内 `_order = "ORDER BY id"` / `"ORDER BY created_at DESC, id DESC"`
- 表 `devops_pipeline_scene` 目前**没有**任何置顶/精选字段；全仓 `pin` 检索无相关实现
- 场景市场（`scene_market`）的 `is_featured`/`featured_only` 是**另一套东西**（官方模板精选，顺序读 admin 库 `admin_featured_config`），本单不复用、不修改
- 现网 `devops_pipeline_scene` 行数约 3792，单仓库最多约 25 行 → 排序开销可忽略，不需要索引

## 已定口径（主人拍板，不要再改）

1. **仓库共享置顶**：谁置顶，该项目所有成员看到同一顺序；不建 per-user 表
2. **置顶内部顺序**：按置顶时间倒序（`pinned_at DESC`），最新置顶的排最前；不做拖拽排序、不引入 `pin_order`
3. 置顶权限与现有「编辑/删除场景」同档：沿用 `require_no_session_token` + 现有 repo_key 解析，**不新增权限判断**
4. 置顶在任意 `sort` 下都优先（`sort=id` 与 `sort=latest` 都以置顶优先）

## 期望效果

- 每个场景卡片右侧出现置顶按钮：未置顶显示空心 📌「置顶」，已置顶显示实心 📌 + 名称旁「置顶」药丸徽章，再点即取消
- 点击按钮不跳转场景详情（阻止事件冒泡），操作成功后列表局部刷新
- 置顶场景永远排在列表最前，第 1 页可见；搜索 `q` 过滤时置顶命中项同样排前
- 老库（列尚未补上）不报错：接口正常返回、`is_pinned` 取 0、排序退回原逻辑

---

## 改动范围

严格 4 组共 15 项；多改一处即打回。

### A. 数据库与迁移（`src/api/db_setup.py`）

- A1. `_PIPELINE_SCENE_DDL`（`CREATE TABLE IF NOT EXISTS devops_pipeline_scene`）表定义末尾追加两列：
  - `is_pinned INTEGER NOT NULL DEFAULT 0`
  - `pinned_at VARCHAR(32) DEFAULT NULL`
- A2. 新增一条注册式迁移：`register_migration(version="202611100001", name="scene-pin-columns", kind="safe", fn=_fn_scene_pin_columns)`；
  `_fn_scene_pin_columns()` 内用 `_ensure_column(c, "devops_pipeline_scene", "is_pinned", "INTEGER NOT NULL DEFAULT 0")` 与
  `_ensure_column(c, "devops_pipeline_scene", "pinned_at", "VARCHAR(32) DEFAULT NULL")` 补列，**异常必须抛出**（禁止 try/except 吞掉）。
  - 版本号 `202611100001` 已核对未占用：源码最新 `202611090001`，现网库 `grape-dev-db` 最新 `202611090002`、`grape_test_mys` 最新 `202611080003`。**不得改号，不得重排。**

### B. 读取与排序（`src/api/pipeline_config.py`）

- B1. `_query_scenes_from_db` 增加列探测：`_has_pin = "is_pinned" in _cols`、`_has_pinat = "pinned_at" in _cols`；在 `_extra` 末尾（`linked_repos` 之后、`created_at` 之前或之后均可，但必须固定在最后追加，禁止插到既有列中间）追加 `, is_pinned, pinned_at`。
- B2. `_query_scenes_from_db` 的 `_order` 改为：
  - 列存在且 `sort == "latest"` → `ORDER BY is_pinned DESC, created_at DESC, id DESC`
  - 列存在且其它 sort → `ORDER BY is_pinned DESC, id`
  - 列不存在 → 保持原字符串（`ORDER BY id` / `ORDER BY created_at DESC, id DESC`），实现缺列降级
- B3. `_query_scenes_from_db` 行解析：在既有列偏移顺序**尾部**依次解析 `is_pinned`（int，缺省 0）、`pinned_at`（str，缺省 ""），并加入返回 dict；`col_offset` 递增顺序必须与 `_extra` 拼接顺序严格一致。
- B4. `get_scenes` 的返回 dict 透传 `"is_pinned": int(...)`、`"pinned_at": ...`（其它字段不动）。
- B5. 新增 `set_scene_pinned(scene_id: str, repo_key: str, pinned: bool) -> int`：
  - 返回受影响行数（`c.rowcount`），`WHERE scene_id=? AND repo_key=? AND is_active=1`
  - `pinned=True`：`is_pinned=1`，`pinned_at` 用 `CASE WHEN is_pinned=1 THEN pinned_at ELSE ? END`（已置顶再置顶**不刷新** `pinned_at`，避免顺序抖动）；时间取 `datetime.now(timezone.utc).isoformat()`，与 `update_custom_scene` 同风格
  - `pinned=False`：`is_pinned=0, pinned_at=NULL`
  - 占位符风格与 `update_custom_scene` 一致（`?`，由现有 DB 适配层转换），不引入新的 SQL 拼接方式

### C. HTTP 接口（`src/api/routes/scenes.py`）

- C1. 新增请求模型 `class PinSceneRequest(BaseModel): pinned: bool`（放在 `UpdateSceneRequest` 之后）。
- C2. 新增 `@router.post("/{scene_id}/pin", operation_id="scenes_pin")`：
  - repo_key 解析逻辑与 `update_scene` **逐行同构**（`repo` 参数优先，否则 `app_current_repo()`）
  - 先 `_query_scene_by_id(scene_id, repo_key=repo_key)`，不存在 → HTTPException 404 `{"code":404,"message":"场景不存在"}`
  - 调 `set_scene_pinned(...)`，`rowcount == 0` 同样 404
  - 返回 `{"message": "置顶状态已更新", "scene_id": scene_id, "is_pinned": 1 if body.pinned else 0}`；`log.info` 一条
- C3. `list_scenes_with_pipeline` 的每项追加 `"is_pinned": int(s.get("is_pinned") or 0)`、`"pinned_at": s.get("pinned_at") or ""`（字段位置无要求，禁止改动既有字段）。
- C4. `/scenes` 列表响应同样带上 `is_pinned`/`pinned_at`（由 B3/B4 透传，路由层只透传，不加逻辑）。

### D. 前端（`src/api/static/js/pages/dashboard.js`、`src/api/static/index.html`、`src/api/static/locales/{zh,en}/dashboard.json`）

- D1. `_renderLocalSceneCard`：
  - 名称旁（`createdBy` 之后）在 `s.is_pinned` 为真时渲染「置顶」药丸徽章（沿用 999px 药丸、低饱和莫兰迪配色，与 PR 订阅徽章同尺寸）
  - 行右侧 `.scn-row-right` 内 `prBadge` 之前加置顶按钮：未置顶 `📌`（浅色边框、透明底），已置顶 `📌` 实心蓝 + `title` 用 `I18N.t('dashboard.scene_unpin')`
  - 按钮 `onclick="event.stopPropagation(); window._toggleScenePin('<scene_id>', event)"`，**不得**把 onclick 写在整行上或改变整行原有跳转
- D2. 新增 `window._toggleScenePin = function(sceneId, ev)`：从 `window._dashboardData._scenes`（或 `scenes` 闭包不可见时用 `_dashScenePipelines` 无关）解析当前 `is_pinned` → 取反 → `API.post('/scenes/' + encodeURIComponent(sceneId) + '/pin?repo=' + encodeURIComponent(owner + '/' + name), {pinned: target})`；成功后 `window._sceneReload && window._sceneReload()`；失败 `app.showToast(I18N.t('dashboard.scene_pin_failed', {msg: ...}))`，并**不做**乐观更新（不做本地先改状态再回滚）。
  - owner/name 取值用 `window._dashboardData._owner/_name`（`loadDash` 已写入），不要新读 URL
- D3. 置顶行视觉：`.scn-row.is-pinned` 加 `background:rgba(255,255,255,.78)`，行首 `scn-row-icon` 不变；**禁止**新增左侧色条/描边/圆角变化等超出此一条的样式改动。
- D4. `locales/zh/dashboard.json` 与 `locales/en/dashboard.json` 各加 4 个键：`scene_pin`（置顶 / Pin）、`scene_pinned`（已置顶 / Pinned）、`scene_unpin`（取消置顶 / Unpin）、`scene_pin_failed`（置顶操作失败: {{msg}} / Pin failed: {{msg}}）；中文文案必须是中文。
- D5. `src/api/static/index.html` 的 `dashboard.js?v=111` → `v=112`（若开工时值已不是 111，则按当前值 +1，只加不减）。

### 测试（新增文件，随单交付）

- T1. `tests/test_scene_pin.py`（若已知本单 issue 号，也可命名 `tests/test_issue_<n>_scene_pin.py`）：仿 `tests/test_issue_461_scene_deploy_url.py` 的 alice/bob + `dependency_overrides` 模式，覆盖验收 2–7。
- T2. `tests/js/test_scene_pin.js`：仿 `tests/js/test_issue_461_scene_deploy_url.js` 的静态断言模式（node 直接跑，无浏览器）。

---

## 明确禁止

1. 不改 `scene_market` 表结构/接口/前端（`scene-market.js`）、不复用 `src/featured_order.py`、不读 admin 库
2. 不改 `issues_center.py`、`prompt_builder.py`、`src/user/db.py` 中调用 `get_scenes` 的逻辑（排序变化是自然结果，不许顺手加参数或改筛选）
3. 不引入 `pin_order` 字段、拖拽排序、per-user 置顶表、置顶有效期/过期逻辑
4. 不改 `GET /scenes/{scene_id}` 详情接口，不改 `_query_scene_by_id` 的现有返回字段
5. 不给 `is_pinned`/`pinned_at` 建索引，不改分页语义（`page`/`page_size`/`total` 含义与上限不变）
6. 不改 `devops_pipeline_scene` 既有列的拼接顺序、不改 `nodes_json` 写入语义、不改 `advanced_options`/`created_by`/`linked_repos` 等解析顺序
7. 迁移 fn 不得吞异常；不得把新迁移写进老轨 `_run_all_migrations()`；不得改动 `LEGACY_TRACK_FROZEN` 相关逻辑（如该分支存在）
8. 不改 `PUT /scenes/{scene_id}` 的校验链（引擎/skill scope/PR 订阅校验一行不动）
9. 不顺手重构、不改既有日志级别、不新增第三方依赖、不新增数据库连接配置

## 反诱人错解（别这么做）

- ❌ 用 `scene_market.is_featured` 或 admin `featured_order` 冒充置顶
- ❌ 只改前端排序（`scenes.sort(...)`）：分页下第 2 页会错位，必须在 SQL 层排序
- ❌ 把 `is_pinned` 插到 `_extra` 中间导致 `col_offset` 全错位
- ❌ 用 `try: ... except Exception: pass` 包住迁移，列没建成本次照样"成功"
- ❌ 置顶后前端乐观更新：解置顶失败时界面与后端不一致

---

## 验收标准（可执行，逐条断言）

1. **基线**：`git log --oneline -1` 记录 SHA；`src/api/db_setup.py` 中 `_PIPELINE_SCENE_DDL` 含 `is_pinned`、`pinned_at`；`grep -n 'version="202611100001"'` 命中且全仓无第二个同号。
2. **迁移幂等**：sqlite 内存库/临时文件库连续执行迁移 fn 两次，无异常；`PRAGMA table_info(devops_pipeline_scene)` 中两列各只出现一次，`is_pinned` 默认 0。
3. **排序·默认 sort**：seed 同仓库 3 个场景（id 升序 a、b、c）→ `GET /api/scenes?repo=...` 顺序 a,b,c；置顶 c 后顺序 c,a,b；再置顶 b → b,c,a（`pinned_at` 倒序）；全部取消 → a,b,c。
4. **排序·sort=latest**：置顶项仍在最前，置顶内部按 `pinned_at DESC`；未置顶部分按 `created_at DESC, id DESC` 与原行为一致。
5. **幂等**：对已置顶场景再次 `POST .../pin {"pinned": true}` 返回 200 且 `pinned_at` 与首次相同（不刷新）；`{"pinned": false}` 两次后 `is_pinned=0, pinned_at IS NULL`。
6. **缺列降级**：模拟列缺失（打桩 `SHOW COLUMNS` 返回不含 `is_pinned`，或删列后的临时库）→ `_query_scenes_from_db` 不抛错、SQL 中不含 `is_pinned`、返回项 `is_pinned=0`。
7. **归属校验**：bob 的仓库不存在该场景 → 404；bob 不持有 alice 仓库权限（按现有 `check_repo_access` 口径）时不得 200 改到别人仓库的场景。
8. **接口契约**：`GET /api/scenes/with-pipeline?repo=...` 每项含 `is_pinned`（0/1 与 DB 一致）；`GET /api/scenes?repo=...` 同样含；`POST .../pin` 返回体含 `scene_id`、`is_pinned`。
9. **前端静态断言**（`node tests/js/test_scene_pin.js` 全绿）：`stopPropagation` 出现在置顶按钮 onclick 内；`_toggleScenePin` 存在且调用 `/pin` 端点；`is_pinned` 在 `_renderLocalSceneCard` 中出现（徽章 + 按钮两处）；`dashboard.js` 可被 `new Function` 解析；`index.html` 版本号已递增；zh/en 四个 i18n 键齐全且 zh 值为中文。
10. **回归**：`tests/js/` 全量通过；场景相关后端测试（`test_issue_239_scene_display_name.py`、`test_issue_461_scene_deploy_url.py`、`test_issue955_scene_node_role_validation.py`、`test_issue307_create_scene_linked_repos.py`）全绿；`tests/atomic/test_issue_536_migrations.py` 通过。
11. **性能**：`_query_scenes_from_db` 仍为单条 SQL + 一次 `SHOW COLUMNS`（列探测不在循环内），无逐场景查询（可用 sqlite 1000 行 seed + 计时对比，断言不出现 N+1）。分页 `page=2` 顺序连续、无重复场景 id。
12. **线上观察（只写步骤，不执行部署）**：置顶某场景 → 刷新 dashboard 看徽章与排序 → 解置顶回落 → 换另一账号登录同项目确认顺序一致（仓库共享）。

## 参考资料

- 关键锚点（函数名优先）：`dashboard.js::_renderSceneCards` / `_renderLocalSceneCard` / `window._sceneReload`；`app.js::loadDash`；`scenes.py::list_scenes` / `list_scenes_with_pipeline` / `update_scene`；`pipeline_config.py::_query_scenes_from_db` / `get_scenes` / `update_custom_scene` / `create_custom_scene`；`db_setup.py::_ensure_column` / `register_migration` / `_PIPELINE_SCENE_DDL`
- 不得破坏的现存行为：`sort` 参数只允许 `id|latest`（路由层 `pattern` 校验）；`_scene_search_where` 为 `q` 共享 WHERE 构建器（count 与 list 必须同源）；`get_pipeline_config` 进程级缓存 60s（本单不涉及，勿动）；分页 `page_size` 上限 100
- 测试口径：sqlite 临时/内存库 + 打桩，不压现网、不连远端 MySQL；不新增网络依赖
- 规模预估：业务代码约 +140 行（db_setup ≈ 25、pipeline_config ≈ 45、scenes.py ≈ 45、dashboard.js ≈ 40、locales 4、index.html 1）；测试约 +200 行