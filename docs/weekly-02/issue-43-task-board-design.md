# Issue #43 技术方案：任务中心按阶段看板呈现

> Issue: https://gitcode.com/grape-dev/grape/issues/43
> 需求：将所有任务按 VOD/ISSUE/Coding/QA/SRE 五列呈现，任务到了哪个阶段就进入哪个列，点击查看详情，进展一目了然。

## 1. 现状分析

### 1.1 当前任务中心

- 前端 `tasks.js`：列表工作流风格（左列表 + 右详情面板），任务按 all/active/completed 三个 Tab 分组
- 后端 `GET /api/tasks/mine`：查 `tasks` 表，按 status（active/processing/completed）排序
- 任务没有显式的"阶段"字段——阶段信息在 `issue_state` 表的 `stage` 字段（vod/issue/coding/qa/sre/done）

### 1.2 阶段数据来源

- `issue_state` 表：`repo_key + issue_id → stage`（vod/issue/coding/qa/sre/done）
- `agent_tasks` 表：`agent_type` 字段（vod/issue/coding/qa/sre）记录每个 Agent 的执行任务
- `tasks` 表：用户可见的任务，通过 `repo_key + issue_number` 关联到 `issue_state`

### 1.3 流水线页面已有阶段视图

- `pipeline.js`：按 issue 展示流水线，每个 issue 的 agents 按 vod→issue→coding→qa→sre 顺序排列
- 但流水线页面是按 Issue 组织的，不是按任务组织的

## 2. 设计目标

- 任务中心新增"看板"视图（与现有"列表"视图并列切换）
- 看板按 5 列呈现：VOD → ISSUE → Coding → QA → SRE
- 每列展示当前阶段在该列的任务卡片
- 点击卡片 → 右侧详情面板（复用现有详情 UI）
- 卡片展示：任务标题、Issue 编号、状态标签、Agent 类型、更新时间
- 支持拖拽移动（仅视觉，不改后端 stage——stage 由 Agent A2A 推进）
- 支持搜索过滤（复用现有搜索能力）

## 3. 技术方案

### 3.1 后端 API 改造

#### 3.1.1 新增 `stage` 字段到 `/api/tasks/mine` 返回

在 `my_tasks` 函数中，已有 `get_issue_stage` 调用获取 scene_id。同时读取 `stage` 字段：

```python
# tasks.py my_tasks 函数中
_st = get_issue_stage(_rk, _issue_id)
_stage = (_st.get("stage") or "") if _st else ""
```

返回的 task dict 新增：
```json
{
  "stage": "coding",  // vod/issue/coding/qa/sre/done
  ...
}
```

#### 3.1.2 新增 `view` 参数

```python
@router.get("/mine")
async def my_tasks(
    status: str = Query(""),
    repo_key: str = Query(""),
    sort_order: str = Query("desc"),
    mode: str = Query("default"),
    view: str = Query("list", pattern="^(list|board)$"),  # 新增
    user: dict = Depends(get_current_user),
):
```

`view=board` 时不改变 SQL 查询（仍返回全部任务），前端按 `stage` 分列。后端只需确保每个 task 返回 `stage` 字段。

#### 3.1.3 Agent 类型回退

如果 `issue_state.stage` 为空（新 Issue 尚未 VOD），默认归入 `vod` 列。

### 3.2 前端看板视图

#### 3.2.1 视图切换

在任务中心顶部增加视图切换按钮：

```html
<button class="bt ${_taskView==='list'?'bt-p':'bt-o'}" onclick="_taskView='list';_render()">列表</button>
<button class="bt ${_taskView==='board'?'bt-p':'bt-o'}" onclick="_taskView='board';_render()">看板</button>
```

#### 3.2.2 看板布局

```
┌─────────┬─────────┬─────────┬─────────┬─────────┐
│   VOD   │  ISSUE  │ Coding  │   QA    │   SRE   │
│  (2)    │  (1)    │  (3)    │  (1)    │  (0)    │
├─────────┼─────────┼─────────┼─────────┼─────────┤
│ ┌─────┐ │ ┌─────┐ │ ┌─────┐ │ ┌─────┐ │         │
│ │Task1│ │ │Task3│ │ │Task5│ │ │Task8│ │  (空)   │
│ └─────┘ │ └─────┘ │ └─────┘ │ └─────┘ │         │
│ ┌─────┐ │         │ ┌─────┐ │         │         │
│ │Task2│ │         │ │Task6│ │         │         │
│ └─────┘ │         │ └─────┘ │         │         │
└─────────┴─────────┴─────────┴─────────┴─────────┘
```

- 5 列等宽，`overflow-y: auto` 独立滚动
- 列头：阶段名称 + 任务计数
- 卡片：圆角、阴影、hover 高亮
- 卡片内容：标题、Issue#、状态标签、Agent 类型、更新时间
- 点击卡片 → 右侧详情面板滑出（或底部弹窗）

#### 3.2.3 核心渲染函数

```javascript
let _taskView = 'list'; // 'list' | 'board'

function _renderBoard() {
  const stages = ['vod', 'issue', 'coding', 'qa', 'sre'];
  const stageLabels = {
    vod: 'VOD', issue: 'ISSUE', coding: 'Coding', qa: 'QA', sre: 'SRE'
  };
  const stageColors = {
    vod: 'var(--pk)', issue: 'var(--r)', coding: 'var(--a)',
    qa: 'var(--pu)', sre: 'var(--or)'
  };

  // 按阶段分组
  const grouped = {};
  stages.forEach(s => grouped[s] = []);
  _filteredTasks().forEach(t => {
    const stage = t.stage || 'vod';
    if (grouped[stage]) grouped[stage].push(t);
  });

  // 渲染 5 列
  let html = '<div style="display:flex;gap:12px;height:calc(100vh - 200px);overflow-x:auto">';
  stages.forEach(s => {
    const list = grouped[s];
    html += `<div style="flex:1;min-width:240px;display:flex;flex-direction:column">
      <div style="display:flex;align-items:center;gap:6px;margin-bottom:8px">
        <span style="width:8px;height:8px;border-radius:50%;background:${stageColors[s]}"></span>
        <span style="font-weight:600;font-size:13px">${stageLabels[s]}</span>
        <span style="color:var(--t3);font-size:12px">(${list.length})</span>
      </div>
      <div style="flex:1;overflow-y:auto;display:flex;flex-direction:column;gap:8px">
        ${list.map(t => _renderBoardCard(t)).join('')}
      </div>
    </div>`;
  });
  html += '</div>';
  return html;
}

function _renderBoardCard(t) {
  const statusColor = t.status === 'completed' ? 'var(--g)' : t.status === 'processing' ? 'var(--a)' : 'var(--t3)';
  return `<div onclick="_taskSel={id:${t.id}};_render()" style="background:var(--c);border:1px solid var(--b);border-radius:10px;padding:10px 12px;cursor:pointer;transition:all .15s" onmouseover="this.style.borderColor='var(--a)'" onmouseout="this.style.borderColor='var(--b)'">
    <div style="font-weight:600;font-size:13px;color:var(--t1);margin-bottom:4px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${t.title}</div>
    <div style="display:flex;align-items:center;gap:6px;font-size:11px;color:var(--t3)">
      <span style="color:var(--a)">${t.issue_number ? '#'+t.issue_number : ''}</span>
      <span style="width:6px;height:6px;border-radius:50%;background:${statusColor}"></span>
      <span>${t.status}</span>
      <span style="margin-left:auto">${relTime(t.updated_at)}</span>
    </div>
  </div>`;
}
```

#### 3.2.4 视图切换逻辑

在 `_render` 函数中：
```javascript
if (_taskView === 'board') {
  // 渲染看板布局
  el.innerHTML = `...搜索栏 + ${_renderBoard()} ...`;
} else {
  // 现有列表布局
  el.innerHTML = `...列表...`;
}
```

### 3.3 国际化

```json
// zh/tasks.json
"view_list": "列表",
"view_board": "看板",
"stage_vod": "VOD",
"stage_issue": "ISSUE",
"stage_coding": "Coding",
"stage_qa": "QA",
"stage_sre": "SRE"

// en/tasks.json
"view_list": "List",
"view_board": "Board",
"stage_vod": "VOD",
"stage_issue": "ISSUE",
"stage_coding": "Coding",
"stage_qa": "QA",
"stage_sre": "SRE"
```

### 3.4 搜索过滤适配

看板视图复用现有搜索框（onkeyup Enter 触发）。`_filteredTasks()` 同时适配列表和看板——搜索结果按 stage 分组后渲染到各列。

### 3.5 版本号

- `tasks.js?v=N` bump
- `index.html` 对应版本号同步
- `zh/tasks.json` + `en/tasks.json` 新增 key

## 4. 数据流

```
tasks 表 (user_id, repo_key, issue_number)
    ↓ JOIN
issue_state 表 (repo_key, issue_id, stage)
    ↓ 返回 stage
/api/tasks/mine → task.stage
    ↓ 前端分组
看板 5 列 (vod/issue/coding/qa/sre)
```

## 5. 边界情况

| 场景 | 处理 |
|------|------|
| issue_state 无记录（新 Issue） | stage 默认 `vod` |
| stage=done | 不在看板中显示（已完成任务切到 completed Tab 查看） |
| 多任务模式子任务 | 每个子任务有独立 issue_state，各自归列 |
| 搜索无结果 | 看板各列显示"无任务"占位 |
| 任务无 issue_number | 归入 vod 列（stage 为空时默认） |

## 6. 实施计划

1. 后端：`tasks.py` `my_tasks` 返回 `stage` 字段（1 行改动）
2. 前端：`tasks.js` 新增 `_taskView` + `_renderBoard` + `_renderBoardCard`
3. 前端：`_render` 函数分支渲染 list/board
4. i18n：`tasks.json` 新增 view_list/view_board/stage_* key
5. 版本号：tasks.js bump + index.html 同步
6. 测试：pytest + 前端语法检查

## 7. 风险评估

- **低风险**：后端只新增 1 个返回字段，不改查询逻辑
- **低风险**：前端新增视图分支，不影响现有列表视图
- **中风险**：看板布局在不同屏幕尺寸下的响应式适配（建议 min-width: 240px/列，水平滚动）
