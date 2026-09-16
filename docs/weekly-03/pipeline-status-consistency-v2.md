# 流水线状态一致性优化方案

## 一、问题概述

任务详情页（任务中心 → 点击卡片 → 流水线区域）与菜单流水线页（左侧导航 → 流水线）展示同一任务时，节点状态不一致。

**优化原则：以菜单流水线页的"事件状态法"为基准**，任务详情页对齐到这套逻辑。新增 cancelled(4) 和 skipped(5) 状态码。

---

## 二、基准逻辑（菜单流水线页现状）

菜单流水线页的状态计算清晰直接：查每个 agent 的最新一条 event，按状态映射为数字码。

```python
STAGE_STATUS_MAP = {
    "pending": 0,
    "running": 1,
    "completed": 2,
    "failed": 3,
}
```

前端展示：

```js
var statusText = sc === 2 ? '完成' : sc === 1 ? '进行中' : sc === 3 ? '失败' : '待处理';
var dotc = sc === 2 ? '#34c759' : sc === 1 ? '#0071e3' : sc === 3 ? '#ff3b30' : '#86868b';
```

**以此为基础扩展 cancelled(4) 和 skipped(5)，任务详情页对齐到同一套逻辑。**

---

## 三、扩展后的状态码定义

| 状态码 | 含义 | event status 来源 | 前端颜色 | 前端文案 |
|--------|------|-------------------|---------|---------|
| 0 | 待处理 | 无 event / pending | #86868b 灰 | 待处理 |
| 1 | 进行中 | running | #0071e3 蓝 | 进行中 |
| 2 | 已完成 | completed | #34c759 绿 | 完成 |
| 3 | 失败 | failed | #ff3b30 红 | 失败 |
| 4 | 已取消 | cancelled | #86868b 灰 | 已取消 |
| 5 | 已跳过 | 无 event 且在已完成阶段之后 | #c7c7cc 浅灰 | 已跳过 |

新增 cancelled(4) 和 skipped(5) 的判定规则：
- **cancelled(4)：** 节点的最新 event 状态为 cancelled，且流水线未进入 done/pending_review
- **skipped(5)：** 流水线已进入 done/pending_review，但该节点从未有过 event（场景中配置了但实际没执行的节点）

在 done/pending_review 阶段，cancelled 事件视为 completed（曾执行过，流水线已结束，取消不影响最终结论）。

---

## 四、统一计算模块

新建 `src/api/pipeline_status.py`：

```python
"""流水线节点状态统一计算模块。

以菜单流水线页的"事件状态法"为基准，任务详情页对齐到同一套逻辑。
新增 cancelled(4) 和 skipped(5) 状态码。
"""

# 状态码（与菜单流水线页现有 0/1/2/3 兼容，新增 4/5）
STATUS_PENDING = 0
STATUS_RUNNING = 1
STATUS_COMPLETED = 2
STATUS_FAILED = 3
STATUS_CANCELLED = 4    # 新增
STATUS_SKIPPED = 5      # 新增

# event status → node status 映射
EVENT_STATUS_MAP = {
    "pending": STATUS_PENDING,
    "running": STATUS_RUNNING,
    "completed": STATUS_COMPLETED,
    "failed": STATUS_FAILED,
    "cancelled": STATUS_CANCELLED,
}

# 流水线终态阶段
DONE_STAGES = ("done", "pending_review")


def compute_node_statuses(nodes: list, issue_events: list, current_stage: str):
    """统一计算流水线节点状态。

    参数：
        nodes: 节点列表（来自场景配置或 AGENT_ORDER 兜底）
        issue_events: 该 Issue 的事件列表（按 seq ASC）
        current_stage: issue_state.stage

    返回：(nodes, statuses)
        nodes: 带 node_status 和 latest_event 的节点列表
        statuses: 状态码列表
    """
    is_done = current_stage in DONE_STAGES

    # 按 agent_type 分组，保留最新一条事件（后覆盖前）
    events_by_agent = {}
    for e in issue_events:
        at = e.get("agent_type", "")
        if at:
            events_by_agent[at] = e

    statuses = []
    for n in nodes:
        nid = n.get("id", "")
        latest_event = events_by_agent.get(nid)

        if latest_event:
            # 有事件：按最新 event 状态映射
            evt_status = latest_event.get("status", "pending")
            status = EVENT_STATUS_MAP.get(evt_status, STATUS_PENDING)

            # done 阶段：cancelled 视为 completed
            if is_done and status == STATUS_CANCELLED:
                status = STATUS_COMPLETED
        else:
            # 无事件
            if is_done:
                status = STATUS_SKIPPED  # 流水线已结束但该节点未执行
            else:
                status = STATUS_PENDING

        n["node_status"] = status
        n["latest_event"] = latest_event
        statuses.append(status)

    return nodes, statuses
```

---

## 五、修改清单

### 5.1 菜单流水线页 — 接入统一模块 + 补 cancelled/skipped

文件：`src/api/routes/pipeline.py`

修改前：
```python
STAGE_STATUS_MAP = {
    "pending": 0, "running": 1, "completed": 2, "failed": 3,
}

for agent in AGENT_ORDER:
    agent_events = [e for e in issue_events if e.get("agent_type") == agent]
    if not agent_events:
        stages.append({"agent": agent, "latest_event": None})
        stage_statuses.append(0)
        continue
    latest = agent_events[-1]
    status = STAGE_STATUS_MAP.get(latest.get("status", "pending"), 0)
    stages.append({"agent": agent, "latest_event": latest_event})
    stage_statuses.append(status)
```

修改后：
```python
from src.api.pipeline_status import compute_node_statuses

# 节点列表：优先场景配置，兜底 AGENT_ORDER
from src.api.pipeline_config import get_pipeline_config
_scene_id = (_st.get("scene_id") if _st else "") or ""
_pipe_cfg = get_pipeline_config(_scene_id, repo_key=repo_key) if _scene_id else None
_scene_nodes = (_pipe_cfg or {}).get("nodes", [])
if not _scene_nodes:
    _scene_nodes = [{"id": a, "tag": a} for a in AGENT_ORDER]

nodes, stage_statuses = compute_node_statuses(_scene_nodes, issue_events, stage)
stages = [{"agent": n.get("id", ""), "latest_event": n.get("latest_event")} for n in nodes]
```

同时删除 `STAGE_STATUS_MAP` 常量（已移入 pipeline_status.py）。

### 5.2 任务详情页 — 对齐到事件状态法

文件：`src/api/routes/tasks.py`，函数 `get_task_pipeline`

修改前（位置推断法）：
```python
_stage_order = [n.get("id", "") for n in nodes]
_cur_idx = _stage_order.index(_current_stage) if _current_stage in _stage_order else -1
for i, n in enumerate(nodes):
    if _current_stage in ("done", "pending_review"):
        n["node_status"] = "completed"
    elif i < _cur_idx:
        n["node_status"] = "completed"
    elif i == _cur_idx:
        n["node_status"] = "current"
    else:
        n["node_status"] = "pending"

_events_by_agent = {}
for e in events:
    _at = e.get("agent_type", "")
    if _at and _at not in _events_by_agent:
        _events_by_agent[_at] = e
for n in nodes:
    n["latest_event"] = _events_by_agent.get(n.get("id", ""), None)
```

修改后：
```python
from src.api.pipeline_status import compute_node_statuses

nodes, _ = compute_node_statuses(nodes, events, _current_stage)
```

### 5.3 菜单流水线页前端 — 增加 cancelled/skipped 展示

文件：`src/api/static/js/pages/pipeline.js`

修改前：
```js
var statusText = sc === 2 ? I18N.t('pipeline.status_done')
  : sc === 1 ? I18N.t('pipeline.status_in_progress')
  : sc === 3 ? I18N.t('pipeline.status_failed')
  : I18N.t('pipeline.status_pending');

var dotc = sc === 2 ? '#34c759' : sc === 1 ? '#0071e3' : sc === 3 ? '#ff3b30' : '#86868b';
```

修改后：
```js
var statusText = sc === 2 ? I18N.t('pipeline.status_done')
  : sc === 1 ? I18N.t('pipeline.status_in_progress')
  : sc === 3 ? I18N.t('pipeline.status_failed')
  : sc === 4 ? (I18N.t('pipeline.status_cancelled') || '已取消')
  : sc === 5 ? (I18N.t('pipeline.status_skipped') || '已跳过')
  : I18N.t('pipeline.status_pending');

var dotc = sc === 2 ? '#34c759'    // 完成 - 绿
  : sc === 1 ? '#0071e3'           // 进行中 - 蓝
  : sc === 3 ? '#ff3b30'           // 失败 - 红
  : sc === 4 ? '#86868b'           // 取消 - 灰
  : sc === 5 ? '#c7c7cc'           // 跳过 - 浅灰
  : '#86868b';                     // 待处理 - 灰
```

两处（列表视图约第 90 行、详情视图约第 197 行）都需修改。

### 5.4 任务详情页前端 — 对齐到数字状态码

文件：`src/api/static/js/pages/tasks.js`，函数 `_detailHtml` 中的流水线节点渲染

修改前（字符串状态）：
```js
if (n.node_status === 'completed') { /* 绿色 ✓ */ }
else if (n.node_status === 'current') { /* 蓝色脉冲 */ }
else { /* 灰色 pending */ }
```

修改后（数字状态码，与菜单流水线页一致）：
```js
var ns = n.node_status;
var statusText = ns === 2 ? I18N.t('pipeline.status_done')
  : ns === 1 ? I18N.t('pipeline.status_in_progress')
  : ns === 3 ? I18N.t('pipeline.status_failed')
  : ns === 4 ? (I18N.t('pipeline.status_cancelled') || '已取消')
  : ns === 5 ? (I18N.t('pipeline.status_skipped') || '已跳过')
  : I18N.t('pipeline.status_pending');

var dotc = ns === 2 ? '#34c759'
  : ns === 1 ? '#0071e3'
  : ns === 3 ? '#ff3b30'
  : ns === 4 ? '#86868b'
  : ns === 5 ? '#c7c7cc'
  : '#86868b';
```

### 5.5 i18n 文案补充

文件：`src/api/static/locales/zh/pipeline.json` 和 `src/api/static/locales/en/pipeline.json`

新增：
```json
{
  "status_cancelled": "已取消",
  "status_skipped": "已跳过"
}
```

英文：
```json
{
  "status_cancelled": "Cancelled",
  "status_skipped": "Skipped"
}
```

---

## 六、一致性保证

修改后两个视图的数据流：

```
                    ┌─────────────────────────┐
                    │  pipeline_status.py     │
                    │  compute_node_statuses() │
                    │  (统一计算逻辑)          │
                    └────────┬────────────────┘
                             │
              ┌──────────────┼──────────────┐
              ↓                             ↓
   /api/tasks/{id}/pipeline        /api/pipeline
   (任务详情页)                     (菜单流水线页)
              │                             │
              ↓                             ↓
   nodes[].node_status (数字)      stages + statuses (数字)
   nodes[].latest_event            stages[].latest_event
              │                             │
              ↓                             ↓
   tasks.js (数字状态码渲染)       pipeline.js (数字状态码渲染)
```

**一致性保证点：**
1. 两个接口都调用 `compute_node_statuses()`，同一套计算逻辑
2. 节点状态都用数字码 0-5，同一套映射
3. 事件关联都取最新一条（`events_by_agent[at] = e` 后覆盖前）
4. 节点列表都来自场景配置（兜底 AGENT_ORDER）
5. cancelled/skipped 在两个视图中展示一致

---

## 七、验证方案

### 7.1 接口级一致性验证

对同一 Issue 同时调用两个接口，对比返回的节点状态：

```bash
# 任务详情页
curl /api/tasks/{task_id}/pipeline

# 菜单流水线页
curl /api/pipeline?repo_key=gitcode:grape-dev/grape&issue_id=#124
```

验证：每个对应节点的 `node_status` / `status` 值一致。

### 7.2 场景验证

| 场景 | 预期状态码 | 前端展示 |
|------|-----------|---------|
| vod 正常完成 | 2 | 绿色"完成" |
| coding 正在执行 | 1 | 蓝色"进行中" |
| coding 失败未重试 | 3 | 红色"失败" |
| qa 被取消 | 4 | 灰色"已取消" |
| done 阶段，sre 未执行 | 5 | 浅灰"已跳过" |
| done 阶段，qa 曾取消但最终成功 | 2 | 绿色"完成" |

### 7.3 前端一致性验证

1. 打开任务中心，点击任务卡片进入详情，记录流水线节点状态
2. 返回菜单，进入流水线页面，找到同一 Issue，记录节点状态
3. 对比：节点数量、每个节点状态文案、颜色、最新事件 一致

---

## 八、实施计划

| 步骤 | 内容 | 文件 | 工作量 |
|------|------|------|--------|
| 1 | 新建 pipeline_status.py | src/api/pipeline_status.py | 1h |
| 2 | 菜单流水线页接入统一模块 | src/api/routes/pipeline.py | 1h |
| 3 | 任务详情页对齐到事件状态法 | src/api/routes/tasks.py | 1h |
| 4 | 菜单流水线页前端增加 cancelled/skipped | src/api/static/js/pages/pipeline.js | 0.5h |
| 5 | 任务详情页前端对齐数字状态码 | src/api/static/js/pages/tasks.js | 1h |
| 6 | i18n 补充 cancelled/skipped 文案 | locales/zh+en/pipeline.json | 0.5h |
| 7 | 线上验证 | - | 1h |
| 合计 | | | 6h |
