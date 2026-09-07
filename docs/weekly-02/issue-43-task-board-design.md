# Issue #43 技术方案：任务中心按阶段看板呈现（已实施）

> Issue: https://gitcode.com/grape-dev/grape/issues/43
> 需求：将所有任务按 VOD/ISSUE/Coding/QA/SRE 五列呈现，任务到了哪个阶段就进入哪个列，点击查看详情，进展一目了然。
> 状态：**已实施并推送**（commit 2c180e9a，multitask 分支）

## 1. 现状分析

### 1.1 原始任务中心

- 前端 `tasks.js`：列表工作流风格（左列表 + 右详情面板），任务按 all/active/completed 三个 Tab 分组
- 后端 `GET /api/tasks/mine`：查 `tasks` 表，按 status（active/processing/completed）排序
- 任务没有显式的"阶段"字段——阶段信息在 `issue_state` 表的 `stage` 字段（vod/issue/coding/qa/sre/done）

### 1.2 阶段数据来源

- `issue_state` 表：`repo_key + issue_id → stage`（vod/issue/coding/qa/sre/done）
- `agent_tasks` 表：`agent_type` 字段（vod/issue/coding/qa/sre）记录每个 Agent 的执行任务
- `tasks` 表：用户可见的任务，通过 `repo_key + issue_number` 关联到 `issue_state`

### 1.3 Multica 看板参考

- `board-view.tsx`：DnD 拖拽看板，按 status/project/assignee 分组
- `board-column.tsx`：固定列宽 280px（`BOARD_COL_WIDTH = 280`），列独立虚拟滚动
- `board-card.tsx`：0.5px border + rounded-lg + 微弱阴影，卡片结构：优先级+ID → 标题(2行截断) → 标签行 → 元信息行
- `issues-header.tsx`：Filter 图标统一筛选下拉，排序/状态/属性归一到 popover

## 2. 设计目标（已实现）

- 任务中心新增"看板"视图（与"列表"视图通过 popover 切换）
- 看板按 5 列呈现：VOD → ISSUE → Coding → QA → SRE
- 每列展示当前阶段在该列的任务卡片
- 点击卡片 → 当前页全屏打开任务详情（"返回看板"按钮回退）
- 卡片展示：Issue#、状态指示器、标题（2行截断）、仓库+日期
- 支持搜索过滤（搜索框居中放在看板正上方）
- 筛选按钮（状态+排序归一到 popover，参考 multica issues-header）

## 3. 实施方案（实际实现）

### 3.1 后端 API 改造

#### 3.1.1 新增 `stage` 字段到 `/api/tasks/mine` 返回

在 `my_tasks` 函数中，已有 `get_issue_stage` 调用获取 scene_id。同时读取 `stage` 字段：

```python
# tasks.py my_tasks 函数中
_st = get_issue_stage(_rk, _issue_id)
_issue_scene_id = (_st.get("scene_id") or "") if _st else ""
_stage = (_st.get("stage") or "") if _st else ""
```

返回的 task dict 新增 `"stage": _stage` 字段。

#### 3.1.2 未新增 `view` 参数

实际实现中后端不需要 `view` 参数——前端按 `stage` 字段分组渲染，后端只需返回 `stage` 即可。SQL 查询不变。

#### 3.1.3 Agent 类型回退

前端处理：如果 `issue_state.stage` 为空（新 Issue 尚未 VOD），默认归入 `vod` 列。`stage=done` 不在看板中显示。

### 3.2 前端看板视图

#### 3.2.1 视图切换（popover 风格，参考 multica）

不再使用两个并列按钮，改为 popover 下拉选择：

```javascript
// 视图切换 popover（苹果风格，带图标+勾选）
<button onclick="togglePopover('taskViewPopover')">
  <span>{_taskView==='board'?'▦':'☰'}</span> {当前视图名称} <span>▾</span>
</button>
<div id="taskViewPopover" class="popover">
  <!-- 列表选项：☰ 图标 + "列表" + 选中时 ✓ -->
  <!-- 看板选项：▦ 图标 + "看板" + 选中时 ✓ -->
</div>
```

#### 3.2.2 筛选下拉（参考 multica issues-header）

状态 Tab（全部/待处理/已完成）和排序（状态优先/时间序/升降序）归一到统一筛选 popover：

```javascript
// 筛选 popover
<button onclick="togglePopover('taskFilterPopover')">
  <span>⌥</span> 筛选
  {有活跃筛选时显示蓝色圆点}
</button>
<div id="taskFilterPopover" class="popover">
  <!-- 状态分组：radio 风格选择（全部/待处理/已完成 + 计数） -->
  <!-- 排序分组：状态优先/时间序 按钮切换 + 升降序按钮 -->
</div>
```

#### 3.2.3 看板布局（苹果风格 + multica 列宽参考）

```
┌──────────────────────────────────────────────────────────────┐
│  [+ 新建任务] [⌥ 筛选]                    [☰/▦ 视图 ▾]      │
│                    ┌──────────────────────┐                  │
│                    │  🔍 搜索任务...       │                  │
│                    └──────────────────────┘                  │
├──────────┬──────────┬──────────┬──────────┬──────────┤
│  ● VOD   │ ● ISSUE  │ ● Coding │  ● QA    │  ● SRE   │
│  (2)     │  (1)     │  (3)     │  (1)     │  (0)     │
├──────────┼──────────┼──────────┼──────────┼──────────┤
│ ┌──────┐ │ ┌──────┐ │ ┌──────┐ │ ┌──────┐ │          │
│ │#5 待 │ │ │#3 待 │ │ │#5 进行│ │ │#8 完成│ │  无任务  │
│ │标题..│ │ │标题..│ │ │标题..│ │ │标题..│ │          │
│ │repo  │ │ │repo  │ │ │repo  │ │ │repo  │ │          │
│ └──────┘ │ └──────┘ │ └──────┘ │ └──────┘ │          │
└──────────┴──────────┴──────────┴──────────┴──────────┘
```

- 固定列宽 280px（参考 multica `BOARD_COL_WIDTH = 280`）
- 列背景：`rgba(0,0,0,.02)` + `border-radius:14px` + `0.5px border`
- 列头：颜色圆点(7px, 带发光阴影) + 阶段名称 + 计数胶囊(`rgba(0,0,0,.05)` 背景)
- 列独立垂直滚动，水平滚动条在最底部
- 看板区域 `flex:1` 占满剩余高度

#### 3.2.4 卡片设计（苹果风格 + multica board-card 参考）

卡片三行结构（参考 multica board-card.tsx 的 Row1/Row2/Row3 布局）：

```javascript
// 卡片：0.5px border + rounded-lg(10px) + 微弱阴影 + cubic-bezier 过渡
<div onclick="_openBoardDetail(id)" style="
  background:var(--c,#fff);
  border:0.5px solid var(--b,#e5e5ea);
  border-radius:10px;
  padding:10px 12px;
  box-shadow:0 1px 2px rgba(0,0,0,.03);
  transition:all .2s cubic-bezier(.4,0,.2,1);
">
  <!-- Row 1: Issue# (左) + 状态指示器(右) -->
  <div style="display:flex;justify-content:space-between">
    <span style="font-size:11px;color:#86868b">#5</span>
    <span style="color:#3b82f6">● 进行中</span>
  </div>
  <!-- Row 2: 标题（2行截断，-webkit-line-clamp:2） -->
  <p style="font-size:13px;font-weight:600;line-clamp:2">任务标题...</p>
  <!-- Row 3: 仓库(左) + 日期(右) -->
  <div style="display:flex;font-size:10px;color:#86868b">
    <span>repo_key</span>
    <span style="margin-left:auto">09-04</span>
  </div>
</div>
```

- Hover：`border-color: rgba(59,130,246,.3)` + `box-shadow: 0 2px 8px rgba(0,0,0,.06)`
- 选中：`border-color:#3b82f6` + `box-shadow: 0 0 0 3px rgba(59,130,246,.12)`
- 字体：`-apple-system, BlinkMacSystemFont, 'SF Pro Text', sans-serif`

#### 3.2.5 点击卡片 → 当前页详情

点击卡片调用 `_openBoardDetail(taskId)`，替换 `pageContent` 为详情页（不是浮动框/侧边栏）：

```javascript
window._openBoardDetail = function(taskId) {
  const t = window._tasksById[taskId];
  el.innerHTML = `
    <div>
      <div style="display:flex;align-items:center;gap:12px;border-bottom:1px solid var(--b)">
        <button onclick="loadTasks()">← 返回看板</button>
        <h1>${t.title}</h1>
      </div>
      ${_detailHtml(t)}
    </div>`;
};
```

#### 3.2.6 搜索框（苹果风格）

搜索框放在看板正上方居中，苹果风格设计：

```javascript
// 搜索框：圆角12px + 0.5px border + focus 高亮 + 左侧🔍图标
<div style="position:relative;max-width:400px">
  <span style="position:absolute;left:14px;top:50%;transform:translateY(-50%)">🔍</span>
  <input style="
    padding:9px 14px 9px 36px;
    border:0.5px solid var(--b,#e5e5ea);
    border-radius:12px;
    font-family:-apple-system,BlinkMacSystemFont,sans-serif;
    box-shadow:0 1px 2px rgba(0,0,0,.03);
    transition:all .2s cubic-bezier(.4,0,.2,1);
  " onfocus="border-color:rgba(59,130,246,.4);box-shadow:0 0 0 3px rgba(59,130,246,.08)"
    onblur="border-color:var(--b);box-shadow:0 1px 2px rgba(0,0,0,.03)">
</div>
```

### 3.3 国际化

```json
// zh/tasks.json 新增
"view_list": "列表",
"view_board": "看板",
"stage_vod": "VOD",
"stage_issue": "ISSUE",
"stage_coding": "Coding",
"stage_qa": "QA",
"stage_sre": "SRE",
"detail_title": "任务详情",
"back_to_board": "返回看板",
"filter": "筛选",
"filter_status": "状态"

// en/tasks.json 新增
"view_list": "List",
"view_board": "Board",
"stage_vod": "VOD",
"stage_issue": "ISSUE",
"stage_coding": "Coding",
"stage_qa": "QA",
"stage_sre": "SRE",
"detail_title": "Task Detail",
"back_to_board": "Back to Board",
"filter": "Filter",
"filter_status": "Status"
```

### 3.4 版本号

- `tasks.js?v=50`
- `index.html` 对应版本号同步

## 4. 数据流

```
tasks 表 (user_id, repo_key, issue_number)
    ↓ JOIN
issue_state 表 (repo_key, issue_id, stage)
    ↓ 返回 stage
/api/tasks/mine → task.stage
    ↓ 前端 _renderBoard 按 stage 分组
看板 5 列 (vod/issue/coding/qa/sre)
    ↓ 点击卡片
_openBoardDetail → 当前页全屏详情
```

## 5. 边界情况

| 场景 | 处理 |
|------|------|
| issue_state 无记录（新 Issue） | stage 默认 `vod`（前端 `t.stage \|\| 'vod'`） |
| stage=done | 不在看板中显示（`if (stage === 'done') return`） |
| 未知 stage | 回退到 `vod` 列（`if (!grouped[stage]) stage = 'vod'`） |
| 多任务模式子任务 | 每个子任务有独立 issue_state，各自归列 |
| 搜索无结果 | 看板各列显示"无任务"占位 |
| 任务无 issue_number | Issue# 显示为空字符串 |

## 6. 实施记录

| 步骤 | 文件 | 改动 |
|------|------|------|
| 1. 后端 stage 字段 | `src/api/routes/tasks.py` | `my_tasks` 返回 `stage` 字段（1 行新增） |
| 2. 看板视图 | `src/api/static/js/pages/tasks.js` | `_renderBoard` + `_renderBoardCard` + `_openBoardDetail` |
| 3. 筛选 popover | `src/api/static/js/pages/tasks.js` | `_filterPanel`（状态+排序归一） |
| 4. 视图 popover | `src/api/static/js/pages/tasks.js` | 视图切换下拉（图标+勾选） |
| 5. 搜索框 | `src/api/static/js/pages/tasks.js` | 苹果风格搜索框（居中看板正上方） |
| 6. 布局优化 | `src/api/static/js/pages/tasks.js` | flex 布局占满高度，滚动条最底部 |
| 7. i18n | `zh/tasks.json` + `en/tasks.json` | 新增 12 个 key |
| 8. 版本号 | `index.html` | tasks.js?v=50 |

## 7. 验证结果

- **tasks.py 语法**: OK（`py_compile`）
- **tasks.js 语法**: OK（`node -e "new Function(...)"`）
- **JSON 有效性**: zh/en tasks.json 均通过 `json.load`
- **pytest**: 79 passed, 0 failed, 1 deselected
- **API 验证**: `/api/tasks/mine` 返回 `stage` 字段（已验证 10 个任务全部包含 stage）
- **提交**: 2c180e9a → grape-dev/multitask

## 8. 与原方案差异说明

| 原方案 | 实际实现 | 原因 |
|--------|----------|------|
| 两个并列按钮切换视图 | popover 下拉选择（图标+勾选） | 参考 multica，更简洁优雅 |
| 右侧详情面板 / 底部浮动框 | 当前页全屏详情 + 返回按钮 | 用户要求"在当前窗口打开新页面" |
| 状态 Tab 直接展示 | 筛选 popover 归一 | 用户要求"学习 multica 归类放到统一筛选" |
| 5 列等宽 flex:1 | 固定列宽 280px | 参考 multica BOARD_COL_WIDTH，视觉一致 |
| 卡片单行标题截断 | 卡片 2 行截断(-webkit-line-clamp:2) | 参考 multica board-card，展示更多信息 |
| 搜索框在 tsk-controls 内 | 搜索框居中放在看板正上方 | 用户要求"放到看板的正上方" |
| 搜索框用 .tsk-search CSS class | 苹果风格自定义 input | 用户要求"学习苹果样式，让输入更优雅" |
