# Pipeline 页面设计：Issue 进展追踪 + 责任人 + Agent 处理事件

> 状态：待实施
> 日期：2026-08-04
> 关联：docs/new-version/user-workflow-overview.md §2.3 Pipeline 页面

## 1. 背景

当前 Pipeline 页面已能按 Issue 聚合展示 VOD → Issue → Coding → QA → SRE 五阶段的工作流状态，但缺少：
- **当前处理责任人**（机器人 Agent 还是项目组成员）
- **每个 Agent 针对该 Issue 的处理事件明细**（title + status）

用户需求（针对 user-workflow-overview.md §2.3）：

> Pipeline 页面作为每个 Issue 状态跟进的展示页面，列出项目下所有 Issue 的进展信息；
> 例如这个 Issue 现在进行到哪个阶段（VOD/Issue/Coding 阶段等）；
> 另外需要展示这个 Issue 当前的处理责任人（可能是机器人、也可能是项目组成员）；
> 点击每个 Issue，可以看到上面每个 Agent 的处理 Event 的 title 和 status。

## 2. 设计决策（已确认）

1. **Agent 卡片直接显示**该 Agent 对该 Issue 的**最新一条 event**（title + status），替代当前的阶段描述占位
2. **active 区域显示当前阶段责任人**（"当前责任人：XXX"）
3. 点击下方历史表 Issue → 上方 Agent 卡片区切换为该 Issue 的各 Agent event（现有 `selPipe` 交互）
4. 责任人判定：**只显示当前阶段**（正在处理的 agent）
   - event 的 `actor` 是 bot（`get_all_bot_usernames`）→ 显示 Agent display_name（如 "QA Agent"）
   - actor 是用户 → 显示用户名（项目组成员）

## 3. 数据模型

### 3.1 现有 events 表相关字段

| 字段 | 说明 |
|------|------|
| `seq` | 事件唯一 ID |
| `issue_id` | 关联 Issue（`#N` 或 `#0`）|
| `event_type` | issue_opened / issue_comment / pull_request_opened / a2a_event 等 |
| `agent_type` | vod / issue / coding / qa / sre |
| `title` | 事件标题 |
| `status` | running / completed / failed |
| `actor` | 操作者（用户 username 或 bot 用户名）|
| `created_at` | 创建时间 |

### 3.2 增强后的 Pipeline 返回结构

```json
{
  "repo": "owner/repo",
  "active": {
    "issue_id": "#6",
    "title": "新增任务时间设置",
    "stages": [
      {"agent": "issue", "status": 2, "latest_event": {"seq": 251, "title": "issue_opened", "status": "completed", "actor": "Jason7602"}},
      {"agent": "coding", "status": 2, "latest_event": {"seq": 253, "title": "A2A: coding", "status": "completed", "actor": "grape_bot"}}
    ],
    "statuses": [2, 2, 0, 0, 0],
    "result": "Processing",
    "agents": ["Issue", "Coding"],
    "current_owner": "Coding Agent",   // 当前阶段责任人
    "event_count": 3
  },
  "history": [...],
  "active_count": 1,
  "today_count": 3
}
```

## 4. 接口设计

### GET /api/pipeline/{owner}/{repo}

**现有**：返回 active + history，含各 stage 状态（`_get_issue_pipeline`）。

**增强**：
1. 查询 SQL 增加 `actor` 字段
2. `_get_issue_pipeline` 每个 agent 阶段补充 `latest_event`（该 agent 最新一条 event 的 `{seq, title, status, actor}`）
3. 顶层补充 `current_owner`（当前阶段责任人）

### current_owner 判定逻辑

```
取该 Issue 下所有事件，按 agent 分组
→ 找到"当前阶段"：最后一个状态非 pending 的 agent（即最近被处理/正在处理的 agent）
→ 取该 agent 最新 event 的 actor
→ 若 actor ∈ get_all_bot_usernames() → current_owner = get_agent_display_name(agent_type)（如 "QA Agent"）
→ 否则 → current_owner = actor（项目组成员用户名）
```

## 5. 前后端改动

### 5.1 后端 `src/api/routes/pipeline.py`

1. **`get_pipeline` 查询 SQL**：`SELECT seq, issue_id, event_type, agent_type, title, summary, status, created_at, actor FROM events ...`
2. **`_get_issue_pipeline` 增强**：
   - 每个 agent 阶段：取该 agent 对该 issue 的所有 events，`latest_event` = 最后一条
   - `stage_desc2` 用 latest_event 的 title/status 展示
   - 计算 `current_owner`（见上）
   - 返回结构增加 `latest_event` per stage + `current_owner`

### 5.2 前端 `src/api/static/js/pages/pipeline.js`

1. **Agent 卡片区**（当前 stages 渲染处）：
   - 卡片显示该 Agent 的 `latest_event`：title + status
   - 无事件 → 显示 "—" / Pending
2. **active 区域标题下**：显示 `当前责任人：{current_owner}`

### 5.3 文档 `docs/new-version/user-workflow-overview.md` §2.3

更新 ASCII 图和说明，反映：
- Agent 卡片显示最新 event（title + status）
- active 区域显示当前责任人
- 点击 Issue 切换展示

## 6. 责任人展示示例

| 选中 Issue | 当前阶段 | 责任人 | 判定 |
|-----------|---------|--------|------|
| #342 Login timeout | QA | QA Agent | actor=grape_bot（bot）→ Agent 名 |
| #338 Webhook retry | SRE | SRE Agent | actor=grape_bot（bot）→ Agent 名 |
| #331 API rate limit | Issue | Jason7602 | actor=Jason7602（成员）→ 用户名 |

## 7. 测试用例设计

### 7.1 测试文件

新建 `tests/test_pipeline.py`，直接测试 `src/api/routes/pipeline.py` 的 `_get_issue_pipeline` 纯函数（不依赖 HTTP，便于单元测试）。

### 7.2 测试数据构造

```python
# 模拟 events 表查询结果（与 get_pipeline 的 SQL 列序一致）
def _ev(seq, issue_id, event_type, agent_type, title, status, actor, created_at="2026-08-04 10:00:00"):
    return {"seq": seq, "issue_id": issue_id, "event_type": event_type,
            "agent_type": agent_type, "title": title, "status": status,
            "created_at": created_at, "actor": actor}
```

### 7.3 用例列表

| # | 用例名 | 输入 | 断言 |
|---|--------|------|------|
| 1 | `test_single_agent_completed` | 1 个 issue，1 个 issue agent completed 事件 | `statuses=[2,0,0,0,0]`；`latest_event.title='issue_opened'`；`current_owner='Issue Agent'`（actor=grape_bot）|
| 2 | `test_bot_actor_returns_agent_name` | issue agent 事件 actor='grape_bot' | `current_owner == 'Issue Agent'` |
| 3 | `test_user_actor_returns_username` | issue agent 事件 actor='Jason7602' | `current_owner == 'Jason7602'` |
| 4 | `test_multiple_agents_chain` | issue+coding+qa 三个 agent 各有事件 | `statuses=[2,2,2,0,0]`；`agents=['Issue','Coding','QA']`；`result='Processing'`（qa 未全完成则看状态）|
| 5 | `test_running_stage_is_current_owner` | 最后一个 agent（qa）事件 status='running' | `current_owner` = qa 阶段责任人（bot→'QA Agent'）|
| 6 | `test_no_events_returns_none` | issue_id 无任何事件 | `_get_issue_pipeline` 返回 `None` |
| 7 | `test_latest_event_is_last` | 同一 agent 多条事件（seq 递增）| `latest_event.seq == max(seq)`（最新一条）|
| 8 | `test_failed_stage` | 某 agent 事件 status='failed' | `result == 'Failed'`；该阶段 status=3 |
| 9 | `test_all_done_released` | 5 个 agent 全部 completed | `result == 'Released'` |
| 10 | `test_missing_agent_stage` | 某 agent 无事件（如 SRE）| 该阶段 `statuses[i]==0`，卡片显示 "—"/Pending |

### 7.4 关键断言示例

```python
def test_bot_actor_returns_agent_name():
    from src.api.routes.pipeline import _get_issue_pipeline
    events = [_ev(1, "#1", "issue_opened", "issue", "issue_opened", "completed", "grape_bot")]
    p = _get_issue_pipeline("owner/repo", "#1", events)
    assert p is not None
    assert p["statuses"] == [2, 0, 0, 0, 0]
    assert p["current_owner"] == "Issue Agent"   # bot → Agent display_name

def test_user_actor_returns_username():
    events = [_ev(1, "#1", "issue_opened", "issue", "issue_opened", "completed", "Jason7602")]
    p = _get_issue_pipeline("owner/repo", "#1", events)
    assert p["current_owner"] == "Jason7602"     # 成员 → 用户名

def test_latest_event_is_last():
    events = [
        _ev(1, "#1", "issue_opened", "issue", "issue_opened", "completed", "grape_bot"),
        _ev(2, "#1", "issue_comment", "issue", "评论处理", "completed", "Jason7602"),
    ]
    p = _get_issue_pipeline("owner/repo", "#1", events)
    assert p["stages"][1]["latest_event"]["seq"] == 2   # 最新一条
```

### 7.5 Mock 依赖

- `get_all_bot_usernames`（`src/user/db.py`）：mock 返回 `["grape_bot"]`
- `get_agent_display_name`（`src/api/event_pipeline.py`）：mock 或真实（读 agent-description.json）

```python
from unittest.mock import patch

with patch("src.api.routes.pipeline.get_all_bot_usernames", return_value=["grape_bot"]), \
     patch("src.api.routes.pipeline.get_agent_display_name", side_effect=lambda at: {
         "issue": "Issue Agent", "qa": "QA Agent"}.get(at, at)):
    p = _get_issue_pipeline(...)
```

> 注：`_get_issue_pipeline` 需从 `get_all_bot_usernames`/`get_agent_display_name` 导入（在函数内 import，便于 mock）。

### 7.6 运行命令

```bash
python -m pytest tests/test_pipeline.py -v
```

## 8. 验证

1. 后端单测：`_get_issue_pipeline` 返回含 `latest_event` + `current_owner`；bot actor → Agent 名、用户 actor → 用户名
2. 前端 JS 语法检查（`node --check`）
3. 部署后：进入 Pipeline 页
   - Agent 卡片显示最新 event（title + status）
   - active 区域显示当前责任人
   - 点击历史 Issue → Agent 卡片切换为该 Issue 的 event
