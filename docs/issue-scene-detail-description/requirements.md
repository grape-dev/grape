# 需求：场景详情页描述恒为空（前端白名单映射丢失 description）

- 基线：`origin/weekly-10`（`972a88af4`，核实点 `00835d2e0` 起引入缺陷）
- 缺陷类型：前端字段白名单导致的展示回归（后端数据正常）

## 背景

### 现象

打开任意场景详情页，「描述」一栏恒为空，只显示灰色占位「描述（可选）」；而场景列表卡片上的描述正常。所有仓库、所有场景一致复现。

### 已核实根因（基线 `origin/weekly-10`，行号仅参考、一律以函数名锚定）

1. `src/api/static/js/pages/dashboard.js::renderSceneDetailLocal` 在「新鲜优先」改造后，把新鲜数据做了**字段白名单映射**：

```js
const scenes = (scenesData.scenes && scenesData.scenes.length
  ? scenesData.scenes.map(s => ({id: s.id, name: s.name, display_name: s.display_name,
                                 scope: 'repo', pr_subscription_enabled: s.pr_subscription_enabled}))
  : (dashData._scenes || []));
const scene = scenes.find(s => s.id === sceneId);
```

映射结果**不含 `description`**，随后 `_renderLocalSceneDetailPage` 里 `const sceneDesc = scene.description || ''` 恒为空串。

2. 改造前的优先级是 `dashData._scenes || …`——`dashData._scenes` 由 `src/api/pipeline_config.py::get_scenes()` 生成，**带 `description`**，所以详情页此前有描述。
3. 引入提交：`00835d2e0`（2026-10-19，`backport: restore lyz-specific features after origin/main merge`）把优先源反转为 `scenesData.scenes`（映射对象），自此详情页描述全空。
4. 后端无缺陷：`src/api/routes/scenes.py` 的 `scenes_with_pipeline` 返回 `"description": s.get("description") or ""`；`get_scenes()` 同样返回。列表卡片用原始对象（`s.description`），因此不受影响。
5. 同一处映射还吞掉了 `icon` / `work_branch` / `created_by` / `linked_repos` / `scene_type`，并把 `scope` 硬编码为 `'repo'`。

### 关键事实（避免误改）

- `/scenes/with-pipeline` 只返回仓库级场景，其 `scope` 后端已是 `"repo"`，前端覆盖为 `'repo'` 属无副作用的历史写法；本单**保留**该覆盖，不顺手改语义。
- 单场景详情接口 `GET /api/scenes/{id}` **不返回 `description`**（契约如此），本单不改该接口。
- 仓库无 JS 单测基建，验收以静态断言 + 人工浏览器步骤为准。

## 改动范围

严格 2 组共 3 项；多改一处即打回。**采用 B 方案：整体透传，不再手写白名单。**

### A 组：前端透传与缓存版本（2 项）

- A1 `src/api/static/js/pages/dashboard.js::renderSceneDetailLocal`：把上面那行白名单映射改为

```js
? scenesData.scenes.map(s => Object.assign({}, s, {scope: 'repo'}))
```

  仅此一处表达式替换；`dashData._scenes` 兜底分支、`find`、`pipeline` 取值、后续渲染一律不动。
- A2 `src/api/static/index.html`：`<script src="/static/js/pages/dashboard.js?v=108" defer></script>` 的版本号改为 `?v=109`（只动这一行，其它资源版本号不动）。

### B 组：验收（1 项）

- B1 静态断言（提交前必须满足）：
  1. `dashboard.js` 中 `Object.assign({}, s, {scope: 'repo'})` 恰好出现 1 次；
  2. `renderSceneDetailLocal` 内不再出现 `description:` 字面映射（`grep -n "id: s.id, name: s.name"` 无命中）；
  3. `index.html` 中 `dashboard.js?v=109` 存在、`v=108` 不存在。
- B2 人工浏览器步骤（由主人验收，交付说明里列出，不自行截图）：
  1. 打开任一有描述的场景详情 → 描述正常显示；
  2. 打开无描述的场景 → 仍显示占位「描述（可选）」，不报错；
  3. 场景列表卡片描述、图标、工作分支显示与改动前一致；
  4. 自定义场景徽标（`isCustomScene`）与改动前一致；
  5. 编辑模式下「场景描述」输入框预填原描述，保存后回显正常。

## 明确禁止

1. 不改后端：`routes/scenes.py`、`routes/dashboard.py`、`pipeline_config.get_scenes()` 一律不动。
2. 不改详情页其它渲染（节点卡、统计块、相关流水线表格、编辑模式逻辑）。
3. 不改 `scene-market.js`、不改 Vue 前端（不在本分支）。
4. 不重启服务进程（前端静态改动只需 bump `?v=`）；不 `pkill`。
5. 不新增依赖、不顺手重构、不改无关资源版本号。
6. **不 push、不建 MR、不部署**；只提交到本地工作分支并标注「待人工审批」。

## 验收标准

1. 静态核对：`git diff` 只含 `dashboard.js`（1 行）与 `index.html`（1 行），无其它文件。
2. B1 三条静态断言全部通过。
3. B2 五条人工步骤全部通过（改动前后对照，列表与详情描述一致）。
4. 浏览器 Network 面板确认加载的是 `dashboard.js?v=109`（缓存未命中旧文件）。
5. 无 JS 控制台报错；`Object.assign` 透传后多余字段（如 `nodes`）不影响详情页渲染。

## 参考资料

- 根因证据：`00835d2e0` 的 diff 显示优先源由 `dashData._scenes` 反转为 `scenesData.scenes`，且映射对象仅 5 字段。
- 后端字段来源：`scenes_with_pipeline` 返回 `description/work_branch/created_by/icon/scene_type/pr_subscription_enabled/linked_repos`；`get_scenes()` 同口径。
- 兼容性：`Object.assign({}, s, {scope: 'repo'})` 只增字段、不删字段；即使后端某字段缺失，行为与现状一致（`|| ''` 兜底仍在渲染层）。
- 规模预估：生产改动 **2 行**（1 行 JS + 1 行 HTML 版本号），无测试文件（无 JS 测试基建）。
- 验收口径：静态断言 + 人工浏览器对照；不做自动化截图。