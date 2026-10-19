# 项目场景页面改造方案

> 将 Dashboard 页（场景中心）从「下拉框 + 直接展开 Agent 卡片」改造为
> 「横向滚动场景卡片网格 + 点击进入详情页」模式，仿照场景市场（scene-market）的交互风格。

---

## 一、现状分析

### 当前 Dashboard 页结构（dashboard.js）
```
┌─ Hero 区域（仓库名 / 角色 / 统计数字）──────────────────────┐
│                                                              │
├─ 场景选择器卡片 ────────────────────────────────────────────┤
│  [场景下拉框▾]  [配置引擎]  [高级选项]  [删除]              │
│                                                              │
│  ┌─ Agent 卡片 ─┐ ┌─ Agent 卡片 ─┐ ┌─ Agent 卡片 ─┐ ...    │
│  │ VOD Agent    │ │ Issue Agent  │ │ Coding Agent │         │
│  │ 技能: xxx    │ │ 技能: xxx    │ │ 技能: xxx    │         │
│  │ 引擎: 广播   │ │ 引擎: 广播   │ │ 引擎: 广播   │         │
│  └──────────────┘ └──────────────┘ └──────────────┘        │
└──────────────────────────────────────────────────────────────┘
```

**问题**：所有场景挤在一个下拉框里，用户看不到全局，切换场景后页面内容直接替换，
没有「浏览全部场景 → 选中查看详情」的探索流程。

### 场景市场（scene-market.js）的交互模式（参照目标）
```
┌─ 精选场景横向滚动 ──────────────────────────────────────────┐
│  ┌─卡片─┐ ┌─卡片─┐ ┌─卡片─┐ ┌─卡片─┐ →                   │
│  │ ⚙    │ │ 🔍   │ │ 💻   │ │ ⚙    │                      │
│  │ 名称  │ │ 名称  │ │ 名称  │ │ 名称  │                      │
│  │ 描述  │ │ 描述  │ │ 描述  │ │ 描述  │                      │
│  │ 技能链│ │ 技能链│ │ 技能链│ │ 技能链│                      │
│  │ ❤ 3  │ │ ❤ 12 │ │ ❤ 5  │ │ ❤ 8  │                      │
│  │[应用] │ │[应用] │ │[体验] │ │[应用] │                      │
│  └──────┘ └──────┘ └──────┘ └──────┘                       │
└──────────────────────────────────────────────────────────────┘
点击卡片 → 进入详情页（返回按钮 + 大图标 + 执行流程图 + 技能套件 + 模板）
```

---

## 二、改造目标

把 Dashboard 的场景选择器区域改造成场景市场的卡片网格风格：
- 场景以**横向滚动卡片**排列（复用 `.scn-scroll-track` 样式）
- 每张卡片展示：场景名 / Agent 技能链 / 当前应用标记 / 应用按钮
- 点击卡片**主体** → 进入详情页（替换 Dashboard 主体内容，保留返回按钮）
- 详情页包含：Agent 流程卡片 / 技能配置 / 引擎配置 / 高级选项 / Issue 流水线
- Hero 区域保留在卡片网格上方

---

## 三、改造后页面结构

### 3.1 卡片网格视图（默认视图）

```
┌─ Hero 区域 ─────────────────────────────────────────────────┐
│  ■  owner/name  [Maintainer]                                │
│     活跃 3 · 事件 42 · 失败 2                               │
└──────────────────────────────────────────────────────────────┘

┌─ 场景中心卡片区域 ──────────────────────────────────────────┐
│  ┌─场景卡片─┐ ┌─场景卡片─┐ ┌─场景卡片─┐ ┌─创建卡片─┐ →    │
│  │ ▎默认    │ │ ▎自定义  │ │ ▎自定义  │ │           │      │
│  │ ⚙ 默认   │ │ 🔍 搜索  │ │ 💻 开发  │ │  ➕       │      │
│  │ 场景     │ │ 场景     │ │ 场景     │ │  创建新   │      │
│  │          │ │          │ │          │ │  场景     │      │
│  │ VOD→Issue│ │ VOD→QA  │ │ Coding   │ │           │      │
│  │ →Coding  │ │ →SRE    │ │ →QA→SRE  │ │           │      │
│  │ →QA→SRE  │ │          │ │          │ │           │      │
│  │          │ │          │ │          │ │           │      │
│  │ ✓ 当前   │ │ [应用]   │ │ [应用]   │ │           │      │
│  │    应用  │ │          │ │          │ │           │      │
│  └──────────┘ └──────────┘ └──────────┘ └──────────┘      │
│                                                              │
│  ◀ 滚动按钮                              滚动按钮 ▶        │
└──────────────────────────────────────────────────────────────┘
```

**卡片元素**：
- 顶部彩色条（复用 `.scn-card-bar` + `.bg-default` 等渐变）
- 场景图标（emoji，区分 official / custom）
- 场景名称（display_name）
- Agent 技能链（`VOD → Issue → Coding → QA → SRE` 格式，复用 `.scn-chain`）
- 底部操作区：
  - 当前应用的场景：显示 `✓ 当前应用` 标记（绿色胶囊）
  - 未应用的场景：显示 `应用` 按钮（点击直接切换场景）
- 最后一张固定卡片：`➕ 创建新场景`（虚线边框样式）

**交互**：
- 点击卡片主体（非按钮区域）→ 进入详情页
- 点击 `应用` 按钮 → 直接切换场景（event.stopPropagation 防止冒泡）
- 点击 `创建新场景` → 进入创建模式（复用现有 enterCreateMode）

### 3.2 场景详情页（点击卡片后）

```
┌─ 返回按钮 ──────────────────────────────────────────────────┐
│  ← 返回场景列表                                              │
└──────────────────────────────────────────────────────────────┘

┌─ 场景头部卡片 ──────────────────────────────────────────────┐
│  ┌──┐  默认开发场景          [repo] [v1.0]                  │
│  │⚙ │  VOD→Issue→Coding→QA→SRE 全链路 Agent                 │
│  └──┘  默认 DevOps 流水线，覆盖完整开发流程                  │
│                                                              │
│  ──────────────────────────────────────────────────────     │
│  ✓ 当前应用        3 技能 · 2 仓库在用    [应用] [编辑]    │
└──────────────────────────────────────────────────────────────┘

┌─ Agent 执行流程 ────────────────────────────────────────────┐
│  ┌─Agent卡片─┐ → ┌─Agent卡片─┐ → ┌─Agent卡片─┐ → ...      │
│  │ 1 VOD     │   │ 2 Issue   │   │ 3 Coding  │             │
│  │ 需求分析  │   │ Issue 分发│   │ 代码编写  │             │
│  │ 技能: xxx │   │ 技能: xxx │   │ 技能: xxx │             │
│  │ 引擎:广播 │   │ 引擎:广播 │   │ 引擎:广播 │             │
│  └───────────┘   └───────────┘   └───────────┘             │
└──────────────────────────────────────────────────────────────┘

┌─ 技能配置 ──────────────────────────────────────────────────┐
│  (现有编辑模式的内容，展示当前场景每个 Agent 的技能绑定)    │
│  VOD Agent    → 技能: vod-skill         [编辑]              │
│  Issue Agent  → 技能: issue-triage      [编辑]              │
│  ...                                                         │
└──────────────────────────────────────────────────────────────┘

┌─ 引擎配置 ──────────────────────────────────────────────────┐
│  VOD Agent    → 广播                        [配置]          │
│  Issue Agent  → opencode-worker-1           [配置]          │
│  ...                                                         │
└──────────────────────────────────────────────────────────────┘

┌─ 高级选项 ──────────────────────────────────────────────────┐
│  工作分支: feature/dev   触发模式: emit   路由: a2a         │
│  [展开配置]                                                  │
└──────────────────────────────────────────────────────────────┘

┌─ Issue 流水线 ──────────────────────────────────────────────┐
│  (复用 pipeline.js 的流程图 + 表格)                         │
│  ┌─Issue#12─┐ ┌─Issue#11─┐ ┌─Issue#10─┐                   │
│  │ VOD✓     │ │ VOD✓     │ │ VOD✓     │                   │
│  │ Issue⚡  │ │ Issue✓   │ │ Issue✓   │                   │
│  │ Coding○  │ │ Coding✓  │ │ Coding✓  │                   │
│  └──────────┘ └──────────┘ └──────────┘                   │
│  Issue  Title           阶段    场景    负责人              │
│  #12    修复登录bug     coding  default alice               │
│  #11    添加缓存        done    default bob                 │
└──────────────────────────────────────────────────────────────┘
```

**详情页分区**：

| 区域 | 内容来源 | 说明 |
|------|----------|------|
| 场景头部 | scene + pipeline_config | 大图标、名称、描述、应用状态、操作按钮 |
| Agent 执行流程 | scene_nodes + agent_engines | 水平卡片链，复用现有 `.det-card` 样式 |
| 技能配置 | scene_nodes[].skill | 每个 Agent 的技能绑定，可编辑（自定义场景） |
| 引擎配置 | agent_engines | 每个 Agent 的引擎绑定，点击可配置 |
| 高级选项 | scene_nodes[].mode/next/handoff/skip | 工作分支、触发模式、路由等 |
| Issue 流水线 | /api/pipeline 数据 | 复用 pipeline.js 的流程图 + 表格 |

---

## 四、涉及文件与改动范围

### 4.1 前端（主要改动）

| 文件 | 改动 | 说明 |
|------|------|------|
| `dashboard.js` | **重写 renderDashboard** | 把场景选择器替换为卡片网格渲染逻辑 |
| `dashboard.js` | **新增 renderSceneDetail(sceneId)** | 场景详情页渲染函数 |
| `dashboard.js` | **新增 _renderSceneCard_local(s)** | 本地场景卡片渲染（区别于市场卡片的点赞/体验） |
| `app.js` | nav 路由扩展 | 支持 `app.nav('scene-detail-local', sceneId)` |
| `app.css` | 新增样式 | 创建卡片虚线边框、当前应用标记样式 |

### 4.2 后端（无新增 API，复用现有）

| API | 用途 |
|-----|------|
| `GET /api/scenes?repo=xxx` | 场景列表（已有，返回 scenes 数组） |
| `GET /api/scenes/with-pipeline?repo=xxx` | 场景列表 + pipeline_config（已有） |
| `GET /api/dashboard/{owner}/{name}` | Agent 配置 + 引擎 + 统计（已有） |
| `GET /api/pipeline/{owner}/{name}` | Issue 流水线数据（已有） |
| `POST /api/scenes/apply/{owner}/{name}` | 应用场景（已有） |

### 4.3 不改动的部分

- Hero 区域：保持现有样式和数据
- 空状态卡片：仓库无场景时的空状态保持不变
- 编辑模式（enterCreateMode / toggleEditMode）：逻辑保持不变，在详情页内触发
- 场景市场（scene-market.js）：完全不动
- Pipeline 页（pipeline.js）：完全不动，详情页内通过 iframe 或内联调用复用

---

## 五、交互流程

### 5.1 正常浏览流程

```
用户进入 Dashboard
  → 加载 /api/dashboard/{owner}/{name}
  → 同时加载 /api/scenes?repo=xxx（获取场景列表）
  → 渲染 Hero + 场景卡片网格
  → 当前应用场景的卡片显示 ✓ 标记

用户点击某张场景卡片
  → app.nav('scene-detail-local', sceneId)
  → 加载该场景的 pipeline_config + agent 配置
  → 渲染详情页（替换 pageContent）
  → 详情页内加载 Issue 流水线数据

用户点击「返回」
  → app.nav('dash')
  → 回到卡片网格视图
```

### 5.2 场景切换流程

```
用户在卡片上点击「应用」
  → event.stopPropagation()（不进入详情）
  → POST /api/scenes/apply/{owner}/{name} {scene_id}
  → 成功后刷新 Dashboard
  → 新应用的卡片显示 ✓ 标记
  → 之前应用的卡片变为 [应用] 按钮
```

### 5.3 创建新场景流程

```
用户点击「➕ 创建新场景」卡片
  → enterCreateMode()（复用现有逻辑）
  → 在详情页框架内渲染创建表单
  → 保存后回到卡片网格
```

---

## 六、样式设计

### 6.1 场景卡片（复用场景市场样式 + 本地定制）

```
.scn-card          → 复用（圆角18px、hover上浮、阴影）
.scn-card-bar      → 复用（顶部4px彩色条）
.scn-card-body     → 复用（padding:20px、flex column）
.scn-chain         → 复用（技能链标签）
.scn-arr           → 复用（→ 箭头）
.scn-scroll-track  → 复用（横向滚动容器）
.scn-scroll-btn    → 复用（左右滚动按钮）
```

**新增样式**：

```css
/* 当前应用标记 */
.scn-applied-badge {
  font-size:10px; font-weight:600;
  padding:3px 10px; border-radius:999px;
  background:rgba(52,199,89,.12); color:#34c759;
}

/* 创建新场景卡片（虚线边框） */
.scn-create-card {
  border:2px dashed #d2d2d7; background:transparent;
  display:flex; align-items:center; justify-content:center;
  color:#86868b; font-size:13px; cursor:pointer;
}
.scn-create-card:hover {
  border-color:#0071e3; color:#0071e3;
  background:rgba(0,113,227,.02);
}
```

### 6.2 详情页（复用场景市场详情页样式）

```
.det-card-wrap     → 复用（白底圆角容器）
.det-grid          → 复用（水平卡片布局）
.det-card          → 复用（Agent 卡片）
.det-card-top      → 复用（彩色条）
.det-arrow         → 复用（→ 箭头）
.det-suite-grid    → 复用（技能套件网格）
.det-suite-card    → 复用（技能套件卡片）
```

---

## 七、数据流

### 7.1 卡片网格视图

```
renderDashboard(data)
  ├── data._scenes          → 场景列表 [{id, name, display_name, scope}]
  ├── data.dev_scene        → 当前应用场景 ID
  ├── data.scene_nodes      → 当前场景的 Agent 节点配置
  ├── data.agents           → Agent 列表（技能/引擎）
  ├── data.agent_engines    → 引擎绑定
  └── data.metrics          → 统计数字
```

需要额外加载每个场景的技能链（用于卡片上的 `VOD→Issue→Coding` 展示）：
- 方案：调 `GET /api/scenes/with-pipeline?repo=xxx` 一次性拿到所有场景的 pipeline_config
- 每个 pipeline_config.nodes 里提取 tag → 映射为 Agent 标签

### 7.2 详情页

```
renderSceneDetail(sceneId)
  ├── scene pipeline_config   → Agent 节点列表
  ├── dashboard data.agents   → 技能/引擎绑定
  ├── pipeline data           → Issue 流水线
  └── scene meta              → 名称/描述/图标
```

---

## 八、改造前后对比

| 维度 | 改造前 | 改造后 |
|------|--------|--------|
| 场景浏览 | 下拉框，一次只看一个 | 卡片网格，全部可见 |
| 场景切换 | 下拉选 → 自动应用 | 卡片上点「应用」按钮 |
| 详情查看 | 直接展开 Agent 卡片 | 点击卡片进入独立详情页 |
| 信息层次 | 平铺，所有信息一屏 | 两级：卡片概览 → 详情全貌 |
| 视觉风格 | 功能导向 | Apple 卡片风格（与场景市场一致） |
| Issue 流水线 | 独立页面（左侧导航） | 集成在场景详情页内 |

---

## 九、风险与注意事项

1. **场景列表数据**：当前 `data._scenes` 只有 `{id, name, display_name, scope}`，
   没有技能链信息。需要额外调 `with-pipeline` API 或在 dashboard API 中补充。
   **建议**：在 `renderDashboard` 时并行调 `/api/scenes/with-pipeline`，
   把每个场景的 nodes 存入 `_scenes[i].nodes`。

2. **Issue 流水线复用**：pipeline.js 的 `renderPipeline` 直接写 `pageContent.innerHTML`，
   在详情页中需要改为写入指定容器（子 div），避免覆盖整个页面。
   **方案**：详情页预留 `<div id="scenePipelineContainer">`，调用 `renderPipeline` 的
   子函数或传一个容器参数。

3. **编辑模式兼容**：现有 `enterCreateMode` / `toggleEditMode` 直接操作 `pageContent`，
   改造后需确保在详情页上下文中正确渲染。
   **方案**：编辑模式在详情页内替换「Agent 执行流程」区域，不覆盖整个详情页。

4. **性能**：场景通常 1-3 个，卡片网格渲染无性能压力。Issue 流水线复用现有分页。

5. **回退**：所有改动集中在 `dashboard.js`（+ 少量 `app.css` / `app.js`），
   不涉及后端改动，回退只需还原前端文件。
