# 流水线状态一致性问题分析与设计方案

## 一、问题概述

用户在两个入口查看同一任务的流水线状态时，看到的结果不一致：

1. **任务详情页**（任务中心 → 点击任务卡片 → 流水线区域）：来自 `/api/tasks/{task_id}/pipeline` 接口
2. **菜单流水线页**（左侧导航 → 流水线）：来自 `/api/pipeline` 接口

两个接口使用不同的数据源和不同的节点状态计算逻辑，导致同一任务在两个视图中的节点状态展示不一致。

---

## 二、两个视图的数据来源对比

### 2.1 任务详情页 — `/api/tasks/{task_id}/pipeline`

文件：`src/api/routes/tasks.py` 函数 `get_task_pipeline`

**数据来源：**
- 节点列表：从 `get_pipeline_config(scene_id)` 读取场景配置的 nodes
- 当前阶段：从 `get_issue_stage(repo_key, issue_id)` 读取 `issue_state.stage`
- 节点状态计算：基于 `_current_stage` 在 `_stage_order` 中的位置推断

**节点状态计算逻辑：**
```python
_stage_order = [n.get("id") for n in nodes]  # 场景配置的节点顺序
_cur_idx = _stage_order.index(_current_stage) if _current_stage in _stage_order else -1

for i, n in enumerate(nodes):
    if _current_stage in ("done", "pending_review"):
        n["node_status"] = "completed"          # 所有节点标完成
    elif i < _cur_idx:
        n["node_status"] = "completed"          # 当前阶段之前的标完成
    elif i == _cur_idx:
        n["node_status"] = "current"            # 当前阶段标进行中
    else:
        n["node_status"] = "pending"            # 后续阶段标待处理
```

**最新事件关联：**
```python
_events_by_agent = {}
for e in events:
    _at = e.get("agent_type", "")
    if _at and _at not in _events_by_agent:  # 取第一个匹配的（最旧的）
        _events_by_agent[_at] = e
```

### 2.2 菜单流水线页 — `/api/pipeline`

文件：`src/api/routes/pipeline.py` 函数 `_build_pipe_data`

**数据来源：**
- 节点列表：硬编码 `AGENT_ORDER = ["vod", "issue", "coding", "qa", "sre"]`（不读场景配置）
- 节点状态：从 events 表查每个 agent_type 的最新事件状态

**节点状态计算逻辑：**
```python
STAGE_STATUS_MAP = {
    "pending": 0,
    "running": 1,
    "completed": 2,
    "failed": 3,
}

for agent in AGENT_ORDER:
    agent_events = [e for e in issue_events if e.get("agent_type") == agent]
    latest = agent_events[-1]  # 取最新一条事件
    status = STAGE_STATUS_MAP.get(latest.get("status", "pending"), 0)
```

**最新事件关联：**
```python
latest = agent_events[-1]  # 取最新一条（最新的在前）
```

### 2.3 差异对比表

| 维度 | 任务详情页 | 菜单流水线页 |
|------|-----------|-------------|
| 节点来源 | 场景配置 nodes（动态） | 硬编码 AGENT_ORDER（固定 5 个） |
| 节点状态 | 基于 issue_state.stage 位置推断 | 基于 events 表最新事件状态 |
| 事件关联 | 取第一个匹配（最旧的） `if _at not in _events_by_agent` | 取最新一条 `agent_events[-1]` |
| cancelled 处理 | 不识别 cancelled（无映射） | 不识别 cancelled（映射为 0=pending） |
| failed 节点 | 前面的节点全标 completed（即使曾 failed） | 有 failed 事件的节点标 failed（3） |
| done 阶段 | 所有节点标 completed | 按 event 状态独立判定 |
| 重试场景 | 只看当前 stage 位置，不看历史失败 | 看最新 event 状态， cancelled/failed 的最新事件影响展示 |

---

## 三、实际不一致案例

### 3.1 案例：#124 任务

**issue_state:** stage=pending_review

**events 状态：**
| seq | agent | status | 说明 |
|-----|-------|--------|------|
| 5255 | vod | completed | 首次 VOD |
| 5257 | issue | completed | Issue 分析 |
| 5258 | coding | failed | 第一次 coding 失败 |
| 5277 | coding | failed | 重试 coding 失败 |
| 5289 | coding | completed | 再次重试 coding 成功 |
| 5278 | qa | completed | 第一次 QA |
| 5290 | qa | completed | 重试 QA |
| 5279 | sre | cancelled | SRE 取消 |
| 5292 | sre | completed | SRE 重试成功 |

**任务详情页展示（基于 stage 位置推断）：**
- stage=pending_review → 所有节点标 completed
- coding 节点标 completed（即使之前 failed 过 2 次）
- 结果：全部绿色 ✓

**菜单流水线页展示（基于最新 event 状态）：**
- vod: 最新 event completed → 状态 2（完成）
- issue: 最新 event completed → 状态 2（完成）
- coding: 最新 event completed → 状态 2（完成）
- qa: 最新 event completed → 状态 2（完成）
- sre: 最新 event completed → 状态 2（完成）
- 结果：全部绿色 ✓

**但中间过程中（coding 重试时）两个视图不一致：**
- 任务详情页：stage=coding → coding 标 current，之前的标 completed（不管前面有没有 failed）
- 菜单流水线页：coding 最新 event=failed → 标 failed（红色），但 issue_state.stage 已经推进到 coding

### 3.2 常见不一致场景

| 场景 | 任务详情页 | 菜单流水线页 | 不一致 |
|------|-----------|-------------|--------|
| coding 失败后重试 | coding 标 current（基于 stage） | coding 标 failed（基于最新 event=failed） | 是 |
| qa 取消后重试 | qa 标 current | qa 标 pending（cancelled 未映射） | 是 |
| sre 取消 | sre 标 pending 或 current | sre 标 pending（cancelled→0） | 是 |
| done 阶段但 done event failed | 全部 completed | done 无 event，按各自最新状态 | 是 |
| 事件有 cancelled 但有更新的 completed | 基于位置推断 | 基于最新 event 状态 | 可能 |

---

## 四、根因分析

### 4.1 两套独立的节点状态计算逻辑

- 任务详情页用"位置推断法"：根据 issue_state.stage 在节点顺序中的位置，前面的全标完成，当前的标进行中，后面的标待处理。**不看实际 event 状态**。
- 菜单流水线页用"事件状态法"：查每个 agent 的最新 event 状态直接映射。**不看 issue_state.stage**。

### 4.2 cancelled 状态未映射

两个视图都不处理 cancelled 状态：
- `STAGE_STATUS_MAP` 只有 pending/running/completed/failed，没有 cancelled
- cancelled 事件被映射为 0（pending），导致看起来"从未执行过"

### 4.3 事件关联方向相反

- 任务详情页：`if _at not in _events_by_agent` → 取**第一个**匹配的事件（最旧的，因为 events 按 seq ASC 排列）
- 菜单流水线页：`agent_events[-1]` → 取**最后一个**匹配的事件（最新的）

### 4.4 节点列表来源不同

- 任务详情页：读场景配置的 nodes（可能有自定义节点顺序或额外节点）
- 菜单流水线页：硬编码 5 个固定 agent（vod/issue/coding/qa/sre），不读场景配置

---

## 五、解决方案设计

### 5.1 统一节点状态计算逻辑

将两处独立的状态计算替换为共享函数，确保两个视图使用同一套逻辑。

新建 `src/api/pipeline_status.py`：

```python
"""流水线节点状态统一计算模块。

消除任务详情页与菜单流水线页的节点状态不一致问题。
两个视图共享同一套计算逻辑，确保同一任务在任何入口看到的状态一致。
"""
from src.api.session_store import get_issue_stage
from src.api.event_store import get_events

# 节点状态枚举（前端展示用）
NODE_PENDING = 0      # 待处理
NODE_RUNNING = 1      # 进行中
NODE_COMPLETED = 2    # 已完成
NODE_FAILED = 3       # 失败
NODE_CANCELLED = 4    # 已取消
NODE_SKIPPED = 5      # 已跳过

# event status → node status 映射
EVENT_STATUS_MAP = {
    "pending": NODE_PENDING,
    "running": NODE_RUNNING,
    "completed": NODE_COMPLETED,
    "failed": NODE_FAILED,
    "cancelled": NODE_CANCELLED,
}

# issue_state.stage 值
DONE_STAGES = ("done", "pending_review")


def compute_node_statuses(scene_nodes: list, issue_events: list, 
                          current_stage: str) -> tuple[list, list]:
    """统一计算流水线节点状态。
    
    参数：
        scene_nodes: 场景配置的 nodes 列表（含 id/tag/skill 等）
        issue_events: 该 Issue 的所有事件列表（按 seq ASC）
        current_stage: issue_state.stage 当前阶段
    
    返回：
        (nodes, statuses)
        nodes: 带 node_status 和 latest_event 的节点列表
        statuses: 状态码列表（与 nodes 一一对应）
    """
    node_ids = [n.get("id", "") for n in scene_nodes]
    cur_idx = node_ids.index(current_stage) if current_stage in node_ids else -1
    is_done = current_stage in DONE_STAGES
    
    # 按 agent_type 分组事件（保留顺序，取最新一条）
    events_by_agent = {}
    for e in issue_events:
        at = e.get("agent_type", "")
        if at:
            events_by_agent[at] = e  # 后覆盖前，最终保留最新
    
    statuses = []
    for i, n in enumerate(scene_nodes):
        nid = n.get("id", "")
        latest_event = events_by_agent.get(nid)
        
        if is_done:
            # 流水线已结束：按最新 event 状态判定
            if latest_event:
                status = EVENT_STATUS_MAP.get(latest_event.get("status", ""), NODE_COMPLETED)
                # cancelled 在 done 阶段视为已完成（曾执行过）
                if status == NODE_CANCELLED:
                    status = NODE_COMPLETED
            else:
                status = NODE_SKIPPED  # 节点未执行
        elif i < cur_idx:
            # 当前阶段之前的节点：按最新 event 状态判定
            if latest_event:
                status = EVENT_STATUS_MAP.get(latest_event.get("status", ""), NODE_COMPLETED)
                # failed 但已被后续重试覆盖 → 看是否有更新的 completed
                # events_by_agent 已保留最新，如果最新是 failed 就是 failed
            else:
                status = NODE_SKIPPED  # 节点未执行（可能是场景中间跳过的节点）
        elif i == cur_idx:
            # 当前阶段
            if latest_event:
                evt_status = latest_event.get("status", "")
                if evt_status == "completed":
                    # 最新事件已完成但 stage 还没推进 → 可能 A2A 尚未触发下一阶段
                    status = NODE_RUNNING  # 标为进行中（等待下一阶段）
                elif evt_status == "failed":
                    status = NODE_FAILED
                elif evt_status == "cancelled":
                    status = NODE_CANCELLED
                elif evt_status == "running":
                    status = NODE_RUNNING
                else:
                    status = NODE_RUNNING
            else:
                status = NODE_RUNNING  # stage 到了但事件还没创建
        else:
            # 当前阶段之后
            status = NODE_PENDING
        
        n["node_status"] = status
        n["latest_event"] = latest_event
        statuses.append(status)
    
    return scene_nodes, statuses
```

### 5.2 任务详情页接入统一逻辑

文件：`src/api/routes/tasks.py`，函数 `get_task_pipeline`

修改前：
```python
# 标记每个节点状态（位置推断法）
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

# 事件关联（取第一个匹配 = 最旧的）
_events_by_agent = {}
for e in events:
    _at = e.get("agent_type", "")
    if _at and _at not in _events_by_agent:
        _events_by_agent[_at] = e
for n in nodes:
    _nid = n.get("id", "")
    n["latest_event"] = _events_by_agent.get(_nid, None)
```

修改后：
```python
from src.api.pipeline_status import compute_node_statuses

nodes, _ = compute_node_statuses(nodes, events, _current_stage)
```

### 5.3 菜单流水线页接入统一逻辑

文件：`src/api/routes/pipeline.py`，函数 `_build_pipe_data`

修改前：
```python
for agent in AGENT_ORDER:
    agent_events = [e for e in issue_events if e.get("agent_type") == agent]
    latest = agent_events[-1]
    status = STAGE_STATUS_MAP.get(latest.get("status", "pending"), 0)
    stages.append({"agent": agent, "latest_event": latest_event})
    stage_statuses.append(status)
```

修改后：
```python
from src.api.pipeline_status import compute_node_statuses, NODE_PENDING, NODE_RUNNING, NODE_COMPLETED, NODE_FAILED, NODE_CANCELLED, NODE_SKIPPED

# 从场景配置读取节点（不再硬编码 AGENT_ORDER）
from src.api.pipeline_config import get_pipeline_config
_scene_id = (_st.get("scene_id") if _st else "") or ""
_pipe_cfg = get_pipeline_config(_scene_id, repo_key=repo_key) if _scene_id else None
_scene_nodes = (_pipe_cfg or {}).get("nodes", []) or [{"id": a, "tag": a} for a in AGENT_ORDER]

# 使用统一计算
nodes, stage_statuses = compute_node_statuses(_scene_nodes, issue_events, stage)

# 转换为原有数据格式（兼容前端）
stages = [{"agent": n.get("id", ""), "latest_event": n.get("latest_event")} for n in nodes]
```

### 5.4 前端适配

#### 5.4.1 任务详情页前端（tasks.js）

当前 `node_status` 使用字符串值 `"completed"/"current"/"pending"`，需要适配新的数字状态码：

```js
// 修改前：
if (n.node_status === 'completed') { ... }
else if (n.node_status === 'current') { ... }
else { ... }  // pending

// 修改后（兼容数字和字符串）：
var ns = n.node_status;
var isDone = ns === 'completed' || ns === 2;
var isCurrent = ns === 'current' || ns === 1;
var isFailed = ns === 'failed' || ns === 3;
var isCancelled = ns === 'cancelled' || ns === 4;
var isSkipped = ns === 'skipped' || ns === 5;
```

#### 5.4.2 菜单流水线页前端（pipeline.js）

当前 `sc` 使用数字 `0/1/2/3`，需要增加 `4`（cancelled）和 `5`（skipped）的展示：

```js
// 修改前：
var statusText = sc === 2 ? '完成' : sc === 1 ? '进行中' : sc === 3 ? '失败' : '待处理';

// 修改后：
var statusText = sc === 2 ? I18N.t('pipeline.status_done') 
  : sc === 1 ? I18N.t('pipeline.status_in_progress')
  : sc === 3 ? I18N.t('pipeline.status_failed')
  : sc === 4 ? I18N.t('pipeline.status_cancelled') || '已取消'
  : sc === 5 ? I18N.t('pipeline.status_skipped') || '已跳过'
  : I18N.t('pipeline.status_pending');

// 状态颜色增加 cancelled/skipped
var dotc = sc === 2 ? '#34c759'      // 完成 - 绿
  : sc === 1 ? '#0071e3'              // 进行中 - 蓝
  : sc === 3 ? '#ff3b30'              // 失败 - 红
  : sc === 4 ? '#86868b'              // 取消 - 灰
  : sc === 5 ? '#c7c7cc'              // 跳过 - 浅灰
  : '#86868b';                        // 待处理 - 灰
```

### 5.5 cancelled 状态处理

当前 cancelled 事件被映射为 0（pending），看起来"从未执行过"。修改后：
- cancelled 映射为 4（NODE_CANCELLED），前端展示"已取消"（灰色）
- 在 done/pending_review 阶段，cancelled 视为 completed（曾执行过，最终被取消但流水线已结束）

---

## 六、验证方案

### 6.1 一致性验证

对同一任务同时调用两个接口，对比节点状态：

```bash
# 任务详情页接口
curl /api/tasks/{task_id}/pipeline

# 菜单流水线页接口（同 issue）
curl /api/pipeline?repo_key=gitcode:grape-dev/grape&issue_id=#124
```

验证点：
1. 节点数量一致（都来自场景配置）
2. 每个节点的状态码一致
3. 每个节点的 latest_event 一致（都取最新一条）
4. cancelled 节点展示"已取消"而非"待处理"

### 6.2 边界场景验证

| 场景 | 预期状态 |
|------|---------|
| coding 失败后重试成功 | coding 标 completed（最新 event=completed） |
| coding 失败未重试 | coding 标 failed（最新 event=failed） |
| qa 被取消后重试 | qa 标 cancelled 或 completed（取决于最新 event） |
| done 阶段但有 cancelled 事件 | cancelled 节点标 completed |
| 场景中间节点未执行 | 标 skipped |

---

## 七、实施计划

| 步骤 | 内容 | 文件 | 工作量 |
|------|------|------|--------|
| 1 | 新建 pipeline_status.py 统一计算模块 | src/api/pipeline_status.py | 2h |
| 2 | 任务详情页接入统一逻辑 | src/api/routes/tasks.py | 1h |
| 3 | 菜单流水线页接入统一逻辑 | src/api/routes/pipeline.py | 1h |
| 4 | 任务详情页前端适配 | src/api/static/js/pages/tasks.js | 1h |
| 5 | 菜单流水线页前端适配 | src/api/static/js/pages/pipeline.js | 1h |
| 6 | i18n 增加 cancelled/skipped 文案 | src/api/static/locales/ | 0.5h |
| 7 | 线上验证 | - | 1h |
| 合计 | | | 7.5h |
