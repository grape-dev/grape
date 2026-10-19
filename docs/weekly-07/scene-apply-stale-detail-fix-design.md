# 方案：市场场景应用后详情页「场景不存在」修复（前端陈旧数据 + 导航竞态）

> 状态：**✅ 已实施**（2026-10-18；代码 `dashboard.js` + `scene-market.js` + `index.html ?v=`；测试
> `tests/js/test_scene_apply_detail_refresh.js` 5/5 通过）｜目标分支：weekly-07-lyz（生产部署分支）
> 动因：从「场景中心」应用某市场场景后，跳到项目场景详情页首屏显示「场景不存在」；
> 切换页面再切回即可看到刚应用的场景。后端已确认数据正确（同步落库、无缓存），
> 根因在**前端陈旧 `window._dashboardData` 复用 + 切仓库导航竞态**。

---

## 一、问题现象

- 场景中心 → 应用场景（选目标仓库）→ 自动跳转该场景详情页 → **首屏「场景不存在」**。
- 用户手动切走再切回（或刷新），目标场景正常显示。
- 生产环境实测可复现。

## 二、根因分析

### 2.1 后端无问题（先排除）

- `POST /scenes/market/{market_id}/apply`（`scenes.py:375`）**同步**创建仓库场景：
  `create_custom_scene`（`pipeline_config.py:379`）直接 INSERT（`scene_id = market scene 的 name`，`is_active=1`）后即返回 `scene_id`。
- 场景列表 `get_scenes`（`pipeline_config.py:266`）→ `_query_scenes_from_db` 直读 `devops_pipeline_scene`，**无任何内存/HTTP 缓存**。
- 因此「应用后第一次查询不包含新场景」不是后端问题。

### 2.2 前端问题：数据源陈旧 + 导航顺序错误

**链路（`scene-market.js _doApplySceneWithOptions → _navAfterApply`）：**

```js
var _navAfterApply = function(extraMsg) { ...;
  var curFull = _currentRepoFullName();
  if (curFull !== repoFull) {
    _selectRepoAndNav(repoFull, owner, repoName);   // ① 异步：GET /repos/managed → app.switchRepo()
  }
  app.nav('scene-detail-local', sceneIdLocal);        // ② 同步立即执行（不等 ① 完成）
};
```

- `_selectRepoAndNav`（`scene-market.js:810`）内部先 `API.get('/repos/managed')` 再 `app.switchRepo(fullName)`，是**异步**的；
  `_navAfterApply` 里 **② 未被 await/回调串行** → `app.nav('scene-detail-local', sceneIdLocal)` 在仓库切换完成前执行。
- 首次渲染时 `app.currentRepo()` 仍是**旧仓库**，`window._dashboardData` 仍是**旧仓库/旧时点**的数据。

**详情页判定（`dashboard.js renderSceneDetailLocal`）：**

```js
const data = window._dashboardData || {};
...
const dashPromise = (data.agents ? Promise.resolve(data)                 // ★ 复用旧数据，跳过重新请求
                                 : API.get('/dashboard/' + owner + '/' + name));
const pipeCfgPromise = API.get('/scenes/with-pipeline?repo=' + owner + '/' + name);
Promise.all([dashPromise, pipeCfgPromise]).then(([dashData, scenesData]) => {
  const scenes = dashData._scenes || (scenesData.scenes || []).map(...);  // ★ 陈旧 _scenes 短路掉新鲜 scenesData
  const scene = scenes.find(s => s.id === sceneId);
  if (!scene) { el.innerHTML = '<div class="error">场景不存在</div>'; return; }  // ← 误报点
```

两处叠加：
1. **`data.agents ? Promise.resolve(data)`**：只要 `window._dashboardData` 存在且带 `agents`，就复用旧快照（其 `_scenes` 早于应用时点）——不请求新数据。
2. **`dashData._scenes || (scenesData.scenes||[]).map(...)`**：即使刚请求了**新鲜的** `scenesData.scenes`（包含新场景），也会被陈旧的 `dashData._scenes` 用 `||` 短路掉。

**「切走再切回就好了」的原因**：仓库切换 / dashboard 刷新完成后，`currentRepo` 变成目标仓库、`window._dashboardData` 被 `dashboard.js:58/765/1911` 覆盖为最新 → `_scenes` 含新场景 → 判定通过。

### 2.3 命中条件

| 场景 | 是否命中 | 原因 |
|------|----------|------|
| 应用到「非当前」仓库 | 是 | 切仓库异步未 await，首帧用旧仓库 + 旧 `_scenes` |
| 应用到「当前」仓库 | 是 | `window._dashboardData._scenes` 为应用前快照，`||` 短路吞掉新鲜 `scenesData` |
| 手动点卡片进详情（非应用后跳转） | 一般否（数据是最新时） | 无“刚应用”这一窗口期，但复用旧 `_dashboardData` 仍可能读到旧数据 |

---

## 三、方案设计

### 3.1 详情页数据源修正（`dashboard.js renderSceneDetailLocal`）

**A. 去掉「复用快照」捷径——首屏一律请求权威数据**

```js
// 原：const dashPromise = (data.agents ? Promise.resolve(data) : API.get('/dashboard/' + owner + '/' + name));
const dashPromise = API.get('/dashboard/' + owner + '/' + name);
```

- `/dashboard/{owner}/{repo}` 本身轻量且是权威源（含最新 `_scenes`）；去掉 `Promise.resolve(data)` 捷径，杜绝陈旧快照复用。
- 若仍需降载，可仅当场景判定用新鲜 `scenesData` 时保留快照，但**不推荐**（多一层状态难收敛）。

**B. 场景列表判定「新鲜优先」**

```js
// 原：const scenes = dashData._scenes || (scenesData.scenes || []).map(...);
const scenes = ((scenesData.scenes && scenesData.scenes.length)
  ? scenesData.scenes.map(s => ({id: s.id, name: s.name, display_name: s.display_name, scope: 'repo'}))
  : (dashData._scenes || []) );
```

- 以**本次请求**的 `scenesData.scenes` 为准（fresh 优先）；`dashData._scenes` 仅作兜底（scenesData 为空时）。
- 后端场景列表即截图 `get_scenes`（`/scenes/with-pipeline`），与详情判定同源，可覆盖“刚应用”场景。

> 说明：`scenesData.scenes`（`/scenes/with-pipeline`）来自同一 `get_scenes`（无缓存），保证包含刚 apply 的场景；
> `dashData._scenes`（dashboard）也同源，但可能捕获于 apply 之前——因此一律以 fresh 为准。

### 3.2 应用后导航顺序（`scene-market.js _navAfterApply`）

让 `_selectRepoAndNav` 可 await（返回 Promise，`app.switchRepo` 后 resolve），并在其完成后才跳详情：

```js
function _selectRepoAndNav(fullName, owner, name) {
  return API.get('/repos/managed').then(function(d) {
    const repos = d.repos || [];
    const sel = document.getElementById('repoSelect');
    if (sel) { sel.innerHTML = repos.map(...).join('') + '<option value="__add__">…</option>'; }
    app.switchRepo(fullName);
  }).catch(function() { app.switchRepo(fullName); });
}
```

```js
var _navAfterApply = function(extraMsg) { ...;
  var curFull = _currentRepoFullName();
  if (curFull !== repoFull) {
    _selectRepoAndNav(repoFull, owner, repoName).then(function() {
      app.nav('scene-detail-local', sceneIdLocal);
    });
  } else {
    app.nav('scene-detail-local', sceneIdLocal);
  }
};
```

- 仓库切换完成后 `app.switchRepo` 已同步写 `sessionStorage.currentRepo` 并 `nav('dash')`；随后进入详情页时 `app.currentRepo()` 即目标仓库。
- 配合 3.1 的 fresh 判定，首帧即可命中新场景（`window._dashboardData` 即便尚未刷新，判定也只信 fresh `scenesData`）。

### 3.3 关键决策

| 决策 | 依据 |
|------|------|
| 去掉 `data.agents ? Promise.resolve(data)` 捷径 | 单一数据源、状态收敛简单；dashboard 接口轻量，多一次请求可接受 |
| 场景判定 fresh 优先、`_scenes` 兜底 | `/scenes/with-pipeline` 与详情页同源（同一 `get_scenes`），且保证 apply 后即时可见 |
| `_selectRepoAndNav` 改为返回 Promise | 复用既有实现，仅串行化，最小改动 |
| 不改后端 / 不加后端缓存 | 后端数据本已一致，问题纯前端；改动面最小 |
| 兼容其它入口 | 点场景卡片 `nav('scene-detail-local', id)` 同样走 `renderSceneDetailLocal`（fresh 判定对普通浏览无副作用；固定重拉 dashboard 只是略多一次请求） |

---

## 四、各模块改动

| 模块 | 改动 |
|------|------|
| `src/api/static/js/pages/dashboard.js` | `renderSceneDetailLocal`：① `dashPromise` 一律 `API.get('/dashboard/…')`（去 `Promise.resolve` 捷径）；② `scenes` 判定改为 `scenesData.scenes` 优先、`_scenes` 兜底 |
| `src/api/static/js/pages/scene-market.js` | `_selectRepoAndNav` 返回 Promise；`_navAfterApply` 跨仓库分支 `.then(…)` 后再 `nav('scene-detail-local')` |
| `tests/js/` | 新增 `test_scene_apply_detail_refresh.js`（见 §六） |

**未改动**：后端（scenes/dashboard/pipeline_config）、前端 `app.js`、场景中心列表。

---

## 五、影响面 / 风险

| 项 | 说明 |
|----|------|
| 行为变化 | 仅「场景详情页数据判定」与「应用后导航顺序」；场景中心列表/其它页面零影响 |
| 多一次请求 | 详情页首屏固定请求 `/dashboard/{owner}/{repo}`（原有时会复用快照）；接口轻量，可接受；后续可再优化为「带版本/失效标记复用」 |
| 兼容性 | 点卡片、深链进入详情均走同一 render 路径；fresh 判定对普通浏览无回归 |
| 缓存无感日期问题 | 无需清理任何缓存；`window._dashboardData` 仍是普通 dashboard 场景的缓存，只是不再被详情页用作唯一/优先数据源 |
| 切仓库失败降级 | `_selectRepoAndNav` 原 `.catch` → `app.switchRepo(fullName)` 兜底；返回 Promise 后同样传导（catch 内已切换，external .then 仍会执行 nav） |

---

## 六、验证（测试先行）

> 风格对齐 `tests/js/*` 现有 harness（vm 沙箱 + 源级/行为断言）。

| 用例 | 断言 |
|------|------|
| `renderSceneDetailLocal` 不再走 `Promise.resolve(data)` 捷径 | 源中不存在 `data.agents ? Promise.resolve(data)`；`API.get('/dashboard/'+owner+'/'+name)` 恒被调用 |
| 场景判定 fresh 优先 | 构造 `scenesData.scenes` 含新 sceneId、`dashData._scenes` 不含：`scene` 命中新 sceneId（不被 `_scenes` 短路） |
| 场景判定兜底 | `scenesData.scenes` 为空时回退 `dashData._scenes`（行为兼容） |
| `_navAfterApply` 跨仓库串行 | 源中交叉仓库分支为 `_selectRepoAndNav(...).then(function(){ app.nav('scene-detail-local', sceneIdLocal); })`；同仓库分支直接 nav |
| `_selectRepoAndNav` 返回 Promise | 函数含 `return API.get('/repos/managed').then(…)`（或 catch 分支也 return） |
| 回归 | 现有 `tests/js/*` 场景市场/仪表盘用例全绿；手动 E2E：场景中心应用 → 详情页首屏即见场景，无「场景不存在」 |

---

## 七、相关文档 / 提交约定

- 场景列表/详情入口：`dashboard.js renderSceneDetailLocal`、`scene-market.js _doApplySceneWithOptions`。
- 相关设计：`docs/new-version3/` 场景中心、`docs/weekly-02/scene-advanced-options-design.md`（apply + advanced-options 流程）。
- 提交（Conventional Commits，`git commit -s`）：`fix(web): scene detail uses fresh scene list after market apply (stale "场景不存在")`
- 前端改动记得提升 `src/api/static/index.html` 中 JS 的 `?v=` 缓存版本号（feishu/dashboard JS 归并时一起 bump）。