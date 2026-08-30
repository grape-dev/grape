# Engine 评价知识库需求文档

## 1. 背景与现状

### 1.1 当前 Event 详情页

Event 详情页（`events.js` 的 `renderEventDetail`）展示一次 agent 工作的完整信息：
- Seq、Engine（引擎类型名 + 设备名 + IP）、Skill、Duration、Status
- 关联 task 信息（task_id、runtime_id、task_status）
- 接收上下文（prompt）、对话线程（thread）、产出结果

当前没有用户反馈机制 — 用户无法对 agent 的工作质量表达满意或不满意。

### 1.2 当前 Engine 识别

Event 详情页通过多级回退解析实际执行引擎：
1. `agent_tasks.runtime_id`（queue/池模式实际执行引擎 ID）
2. `devops_agent_session.runtime_id`（direct 模式绑定实例 ID）
3. `events.runtime`（引擎类型名，如 "opencode"）

`get_event_detail` 返回 `task.runtime_id`（>0 时为具体引擎实例 ID）和 `engine_instance`（实例名）。

### 1.3 当前知识库体系

现有知识库（`agent_shared_knowledge` 表 + 工作区文件）按 project/issue/issue_agent 三层 scope 组织，由 agent 通过 `POST /api/events/{seq}/knowledge` 接口写入。与用户反馈无关。

## 2. 需求目标

### 2.1 核心功能

在 Event 详情页增加"点赞/踩"功能按钮：
- 用户可以对每次 agent 工作进行点赞（👍）或踩（👎）
- 点赞/踩是标签级的（点赞 = +1，踩 = -1）
- 记录在独立文件中，维护每个 engine 在做某个工作时的表现数值
- 初始值为 0，赞 +1，踩 -1
- 一个 event 只能赞或踩一次，不能同时赞和踩
- 用户可以取消已赞/已踩（恢复中性），也可以从赞切换到踩（反之亦然）

### 2.2 评价知识库

评价数据独立存储，不复用现有 `agent_shared_knowledge` 体系：
- 以文件为正体（与现有知识库风格一致）
- 文件路径：`WORKSPACE/engine-ratings/{runtime_id}.json`
- 文件内容：每个 engine 的评价记录列表 + 累计分数

### 2.3 工作过程中的点赞影响知识积累

如果 event 处于 `running` 状态时用户点赞，代表用户对当前工作流程的赞赏：
- 在 event thread 中留痕（"👍 用户对本次工作流程点赞"）
- 在该 event 的 issue 级知识文件中追加标记（`- [user-thumb-up] 本次工作流程获用户点赞`）
- 踩同理（`- [user-thumb-down] 本次工作流程获用户踩`）

已完成（completed/failed）的 event 点赞/踩只更新评价数值，不影响知识积累。

## 3. 数据设计

### 3.1 评价文件结构

文件路径：`WORKSPACE/engine-ratings/{runtime_id}.json`

```json
{
  "runtime_id": 75,
  "engine_name": "hermes",
  "engine_instance": "worker-ecs-hd-ai-work-00-0006/hermes",
  "total_score": 3,
  "thumbs_up": 5,
  "thumbs_down": 2,
  "records": [
    {
      "event_seq": 142,
      "repo_key": "gitcode:XOcto/grape",
      "agent_type": "coding",
      "action": "up",
      "user": "XOcto",
      "timestamp": "2026-08-27T10:30:00",
      "event_status": "completed"
    },
    {
      "event_seq": 138,
      "repo_key": "gitcode:XOcto/grape",
      "agent_type": "qa",
      "action": "down",
      "user": "XOcto",
      "timestamp": "2026-08-27T09:15:00",
      "event_status": "completed"
    }
  ]
}
```

- `total_score` = `thumbs_up` - `thumbs_down`，每次操作实时重算
- `records` 按时间倒序排列（最新在前）
- runtime_id=0 或无法识别时，文件名为 `unknown.json`

### 3.2 DB 索引表 — engine_ratings

```sql
CREATE TABLE IF NOT EXISTS engine_ratings (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    event_seq   INTEGER NOT NULL,          -- 关联事件
    runtime_id  INTEGER DEFAULT 0,         -- 引擎实例 ID
    repo_key    TEXT DEFAULT '',
    agent_type  TEXT DEFAULT '',
    action      TEXT NOT NULL,             -- 'up' / 'down'
    user        TEXT DEFAULT '',
    event_status TEXT DEFAULT '',           -- 点赞时 event 的状态
    created_at  TEXT DEFAULT (datetime('now')),
    UNIQUE (event_seq, user)               -- 一个用户对一个 event 只能操作一次
)
```

UNIQUE 约束确保同一用户对同一 event 只有一条记录。切换赞/踩时 UPDATE action 字段。

### 3.3 评价数据读取

`read_engine_rating(runtime_id: int) -> dict`：读 JSON 文件返回评价摘要。
- 文件不存在时返回 `{"runtime_id": ..., "total_score": 0, "thumbs_up": 0, "thumbs_down": 0, "records": []}`

`read_event_rating(event_seq: int, user: str) -> dict | None`：查 DB 索引返回当前用户对该 event 的评价。
- 返回 `{"action": "up"}` 或 `{"action": "down"}` 或 `None`（未评价）

## 4. 改动逻辑

### 4.1 新增模块 — src/api/engine_rating.py

独立模块，不依赖 knowledge.py：

```python
import os, json
from src.config import WORKSPACE_DIR
from src.session_manager import db, db_lock

RATINGS_DIR = os.path.join(WORKSPACE_DIR, "engine-ratings")

def _rating_path(runtime_id: int) -> str:
    rid = runtime_id if runtime_id and runtime_id > 0 else "unknown"
    return os.path.join(RATINGS_DIR, f"{rid}.json")

def read_engine_rating(runtime_id: int) -> dict:
    """读 engine 评价摘要。文件不存在返回零值。"""
    p = _rating_path(runtime_id)
    try:
        with open(p, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {"runtime_id": runtime_id, "total_score": 0,
                "thumbs_up": 0, "thumbs_down": 0, "records": []}

def read_event_rating(event_seq: int, user: str = "") -> dict | None:
    """查 DB：当前用户对该 event 的评价。"""
    with db_lock:
        c = db.cursor()
        c.execute("SELECT action FROM engine_ratings WHERE event_seq=? AND user=?",
                  (event_seq, user))
        row = c.fetchone()
    return {"action": row[0]} if row else None

def write_rating(*, event_seq: int, runtime_id: int, repo_key: str,
                 agent_type: str, action: str, user: str,
                 event_status: str, engine_name: str = "",
                 engine_instance: str = "") -> dict:
    """写入评价：DB 索引 upsert + JSON 文件重算。返回更新后的 engine 评价摘要。"""
    # 1. DB upsert（UNIQUE(event_seq, user) 约束）
    with db_lock:
        c = db.cursor()
        c.execute("SELECT id, action FROM engine_ratings WHERE event_seq=? AND user=?",
                  (event_seq, user))
        existing = c.fetchone()
        if existing:
            # 已有记录：如果 action 相同则取消（删除），不同则更新
            old_action = existing[1]
            if old_action == action:
                # 取消评价 → 删除记录
                c.execute("DELETE FROM engine_ratings WHERE id=?", (existing[0],))
            else:
                # 切换评价 → 更新 action
                c.execute("UPDATE engine_ratings SET action=?, created_at=datetime('now') WHERE id=?",
                          (action, existing[0]))
            db.commit()
        else:
            # 新评价 → 插入
            c.execute(
                "INSERT INTO engine_ratings (event_seq, runtime_id, repo_key, agent_type, action, user, event_status) "
                "VALUES (?,?,?,?,?,?,?)",
                (event_seq, runtime_id, repo_key, agent_type, action, user, event_status))
            db.commit()

    # 2. 重算 JSON 文件（从 DB 聚合该 runtime_id 的所有记录）
    _rebuild_rating_file(runtime_id, engine_name, engine_instance)
    return read_engine_rating(runtime_id)

def _rebuild_rating_file(runtime_id: int, engine_name: str = "", engine_instance: str = ""):
    """从 DB 重建该 engine 的评价 JSON 文件。"""
    with db_lock:
        c = db.cursor()
        c.execute("SELECT event_seq, repo_key, agent_type, action, user, created_at, event_status "
                  "FROM engine_ratings WHERE runtime_id=? ORDER BY created_at DESC", (runtime_id,))
        rows = c.fetchall()
    records = [{
        "event_seq": r[0], "repo_key": r[1], "agent_type": r[2],
        "action": r[3], "user": r[4], "timestamp": r[5], "event_status": r[6] or ""
    } for r in rows]
    up = sum(1 for r in records if r["action"] == "up")
    down = sum(1 for r in records if r["action"] == "down")
    data = {
        "runtime_id": runtime_id,
        "engine_name": engine_name,
        "engine_instance": engine_instance,
        "total_score": up - down,
        "thumbs_up": up,
        "thumbs_down": down,
        "records": records,
    }
    os.makedirs(RATINGS_DIR, exist_ok=True)
    with open(_rating_path(runtime_id), "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
```

### 4.2 DB schema — engine_ratings 表 — db_setup.py

```python
_ENGINE_RATINGS_DDL = """
CREATE TABLE IF NOT EXISTS engine_ratings (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    event_seq   INTEGER NOT NULL,
    runtime_id  INTEGER DEFAULT 0,
    repo_key    TEXT DEFAULT '',
    agent_type  TEXT DEFAULT '',
    action      TEXT NOT NULL,
    user        TEXT DEFAULT '',
    event_status TEXT DEFAULT '',
    created_at  TEXT DEFAULT (datetime('now')),
    UNIQUE (event_seq, user)
)
"""
```

注册到 `_NEW_TABLES` 列表。

### 4.3 新增 API — events.py

#### `POST /api/events/{seq}/rate`

```python
class RateBody(BaseModel):
    action: str   # 'up' or 'down'

@router.post("/{seq}/rate")
async def rate_event(seq: int, body: RateBody, user: dict = Depends(get_current_user)):
    """对 event 进行点赞/踩。同一用户同一 event：相同 action 取消，不同 action 切换。"""
    if body.action not in ("up", "down"):
        raise HTTPException(status_code=400, ...)

    # 加载 event 信息
    evt = _load_event(seq)
    runtime_id = _resolve_event_runtime_id(seq, evt)  # 从 agent_tasks / session 解析
    event_status = evt["status"]

    from src.api.engine_rating import write_rating, read_event_rating
    result = write_rating(
        event_seq=seq, runtime_id=runtime_id,
        repo_key=evt["repo_key"], agent_type=evt["agent_type"],
        action=body.action, user=user["username"],
        event_status=event_status,
        engine_name=evt.get("runtime", ""),
        engine_instance=evt.get("engine_instance", ""))

    # 如果 event 处于 running 状态，影响知识积累
    if event_status == "running":
        _rating_to_knowledge(seq, evt, body.action, user["username"])

    # 线程留痕
    _add_thread_message(seq, evt["repo_key"], evt["issue_id"], evt["agent_type"],
                        "system", f"{'👍' if body.action == 'up' else '👎'} {user['username']} {'点赞' if body.action == 'up' else '踩'}了本次工作")

    return {"ok": True, "engine_rating": result,
            "my_rating": read_event_rating(seq, user["username"])}
```

#### `GET /api/events/{seq}/rating`

```python
@router.get("/{seq}/rating")
async def get_event_rating(seq: int, user: dict = Depends(get_current_user)):
    """返回当前用户对该 event 的评价 + engine 整体评价摘要。"""
    from src.api.engine_rating import read_event_rating, read_engine_rating
    evt = _load_event(seq)
    runtime_id = _resolve_event_runtime_id(seq, evt)
    return {
        "my_rating": read_event_rating(seq, user["username"]),
        "engine_rating": read_engine_rating(runtime_id),
    }
```

#### `_resolve_event_runtime_id` 辅助函数

```python
def _resolve_event_runtime_id(seq: int, evt: dict) -> int:
    """从 agent_tasks / devops_agent_session 解析 event 的实际 runtime_id。"""
    with db_lock:
        c = db.cursor()
        c.execute("SELECT runtime_id FROM agent_tasks WHERE event_seq=? AND runtime_id>0 ORDER BY created_at DESC LIMIT 1", (seq,))
        row = c.fetchone()
        if row:
            return row[0]
        # 回退：devops_agent_session
        c.execute("SELECT runtime_id FROM devops_agent_session WHERE repo_key=? AND agent_type=? AND runtime_id>0 LIMIT 1",
                  (evt.get("repo_key",""), evt.get("agent_type","")))
        row = c.fetchone()
        if row:
            return row[0]
    return 0
```

#### `_rating_to_knowledge` 知识积累

```python
def _rating_to_knowledge(seq: int, evt: dict, action: str, username: str):
    """running 状态下的点赞/踩写入 issue 级知识文件。"""
    from src.api.knowledge import write_knowledge, _issue_num_from
    issue_num = _issue_num_from(evt["issue_id"])
    content = f"用户 {username} {'点赞' if action == 'up' else '踩'}了本次工作流程"
    write_knowledge(repo_key=evt["repo_key"], scope="issue",
                    content=content, status="accepted",
                    agent_type=evt["agent_type"], issue_num=issue_num,
                    source_seq=seq, created_by=username)
```

### 4.4 Event 详情 API 扩展 — events.py

`get_event_detail` 返回值增加评价信息：

```python
# 在 get_event_detail 末尾
from src.api.engine_rating import read_event_rating, read_engine_rating
runtime_id = task_info.get("runtime_id") or 0
result["my_rating"] = read_event_rating(seq, uname)
result["engine_rating"] = read_engine_rating(runtime_id)
```

### 4.5 前端 — events.js

#### Event 详情页增加点赞/踩按钮

在 Event Info 区块（Status 行下方）增加：

```javascript
// 在 renderEventDetail 中，Status 行后追加
const myRating = data.my_rating;  // null / {action: "up"} / {action: "down"}
const engineScore = data.engine_rating ? data.engine_rating.total_score : 0;
html += `<div style="margin-top:6px;display:flex;align-items:center;gap:8px">
    <span style="color:var(--t3)">评价:</span>
    <button id="thumbUpBtn" class="bt bt-o bt-s" style="font-size:12px;padding:3px 10px;${myRating && myRating.action === 'up' ? 'color:var(--g);border-color:var(--g);font-weight:600' : ''}" onclick="rateEvent(${data.seq}, 'up')">👍</button>
    <button id="thumbDownBtn" class="bt bt-o bt-s" style="font-size:12px;padding:3px 10px;${myRating && myRating.action === 'down' ? 'color:var(--r);border-color:var(--r);font-weight:600' : ''}" onclick="rateEvent(${data.seq}, 'down')">👎</button>
    <span class="ts t3" style="font-size:11px">Engine 评分: <b style="color:${engineScore > 0 ? 'var(--g)' : engineScore < 0 ? 'var(--r)' : 'var(--t2)'}">${engineScore > 0 ? '+' : ''}${engineScore}</b></span>
</div>`;
```

#### 点赞/踩交互函数

```javascript
function rateEvent(seq, action) {
    API.post('/events/' + seq + '/rate', {action: action}).then(d => {
        if (d.ok) {
            // 更新按钮高亮状态
            const upBtn = document.getElementById('thumbUpBtn');
            const downBtn = document.getElementById('thumbDownBtn');
            const myRating = d.my_rating;

            upBtn.style.color = '';
            upBtn.style.borderColor = '';
            upBtn.style.fontWeight = '';
            downBtn.style.color = '';
            downBtn.style.borderColor = '';
            downBtn.style.fontWeight = '';

            if (myRating) {
                if (myRating.action === 'up') {
                    upBtn.style.color = 'var(--g)';
                    upBtn.style.borderColor = 'var(--g)';
                    upBtn.style.fontWeight = '600';
                } else {
                    downBtn.style.color = 'var(--r)';
                    downBtn.style.borderColor = 'var(--r)';
                    downBtn.style.fontWeight = '600';
                }
            }

            // 更新 engine 评分
            const score = d.engine_rating ? d.engine_rating.total_score : 0;
            // 更新评分显示（如果有评分元素的话）
            app.showToast(myRating ? `已${myRating.action === 'up' ? '点赞' : '踩'}` : '已取消评价');
        }
    }).catch(e => app.showToast('操作失败: ' + e.message));
}
```

## 5. 改动文件清单

| # | 文件 | 改动类型 | 改动内容 |
|---|------|---------|---------|
| 1 | src/api/engine_rating.py | **新增** | 评价模块：read/write/rebuild 评价数据 + DB 索引操作 |
| 2 | src/api/db_setup.py | DDL | 新增 `engine_ratings` 表 + 注册到 `_NEW_TABLES` |
| 3 | src/api/routes/events.py | 新增 API + 扩展 | `POST/GET /{seq}/rate`、`/{seq}/rating`；`get_event_detail` 返回评价信息；`_resolve_event_runtime_id()`、`_rating_to_knowledge()` |
| 4 | src/api/static/js/pages/events.js | UI | Event 详情页点赞/踩按钮 + `rateEvent()` 函数 |

## 6. 对现有逻辑的影响

### 6.1 不影响的部分

| 模块 | 原因 |
|------|------|
| 知识库体系（knowledge.py） | 评价数据独立存储在 `engine-ratings/` 目录，不复用 `agent_shared_knowledge` 表。只在 running 状态点赞时调用 `write_knowledge()` 追加一条 issue 级知识，这是现有接口的正常调用，不改其逻辑 |
| Engine 选择（engine_select.py） | 评价分数不参与 engine 选择/调度，纯展示用 |
| 任务入队/执行（service.py / agent.py） | 不涉及评价数据，worker 不感知 |
| Engine 角色标签 | 评价与标签是完全独立的功能 |
| Worker skill 扫描 | 不涉及 |

### 6.2 轻微影响的部分

| 模块 | 影响 | 说明 |
|------|------|------|
| `get_event_detail` API | 返回体增加 `my_rating` 和 `engine_rating` 字段 | 新增字段，前端不消费时忽略，向后兼容 |
| `write_knowledge()` | running 状态点赞时被调用一次 | 现有函数的正常调用，不改其实现 |
| event_thread | 点赞/踩时追加一条 system 消息 | 调用现有 `_add_thread_message()`，不改其实现 |
| events.js | renderEventDetail 增加一行评价 UI | 纯 DOM 追加，不改现有渲染逻辑 |

## 7. 数据流

### 7.1 点赞/踩完整流程

```
用户在 Event 详情页点 👍/👎
  → POST /api/events/{seq}/rate {action: "up"/"down"}
  → 服务端加载 event 信息（repo_key, agent_type, status）
  → _resolve_event_runtime_id(seq) 解析 runtime_id
  → write_rating()：
    → DB upsert（UNIQUE(event_seq, user) 约束）
      → 相同 action → DELETE（取消）
      → 不同 action → UPDATE（切换）
      → 无记录 → INSERT
    → _rebuild_rating_file() 从 DB 聚合重算 JSON 文件
  → 如果 event_status == "running"：
    → _rating_to_knowledge() → write_knowledge(scope="issue")
  → _add_thread_message() 线程留痕
  → 返回 {ok, engine_rating, my_rating}
  → 前端更新按钮高亮 + engine 评分显示
```

### 7.2 Event 详情页加载

```
用户打开 Event 详情页
  → GET /api/events/detail/{seq}
  → get_event_detail() 返回 event 信息 + my_rating + engine_rating
  → 前端渲染评价按钮（根据 my_rating 高亮当前选择）
  → 前端展示 engine 评分（engine_rating.total_score）
```

## 8. 边界与设计决策

| 决策 | 说明 |
|------|------|
| 文件 vs DB 为正体 | 与现有知识库风格一致，文件为正体，DB 存索引。但评价的"正体"是 DB 记录（UNIQUE 约束保证一致性），JSON 文件是聚合快照（从 DB 重建） |
| runtime_id=0 | 无法识别引擎实例时存 0，文件名 `unknown.json`。评价仍然记录，只是不按 engine 分组 |
| 取消评价 | 相同 action 再点一次 = 取消（DELETE），不是 toggle 为中性再点 |
| running 状态影响知识 | 只在 `status == "running"` 时写入知识文件。completed/failed 状态只更新评价数值 |
| 评分不参与调度 | 评价分数纯展示，不影响 engine 选择。避免低分 engine 被排挤导致无可用引擎 |
| 评价不可删除 | 用户只能取消自己的评价（DELETE），不能删除他人的。admin 可通过 DB 直接管理 |
| 并发安全 | DB UNIQUE 约束 + db_lock 保证一致性。JSON 文件写入由 `_rebuild_rating_file` 全量重建，不存在部分更新问题 |

## 9. 测试用例

### 9.1 评价写入测试 — test_engine_rating.py

| 用例 ID | 用例名 | 操作 | 预期结果 |
|---------|--------|------|---------|
| T001 | 新建点赞 | write_rating(action="up", user="A") | DB 有 1 条记录；JSON total_score=1, up=1, down=0 |
| T002 | 新建踩 | write_rating(action="down", user="B") | DB 有 1 条记录；JSON total_score=-1, up=0, down=1 |
| T003 | 切换赞→踩 | 先 up 再 down（同 user 同 event） | DB UPDATE action="down"；JSON total_score=-1 |
| T004 | 取消赞 | 先 up 再 up（同 user 同 event） | DB DELETE；JSON total_score=0, up=0, down=0 |
| T005 | 不同用户独立 | user A up + user B down（同 event） | DB 有 2 条记录；JSON total_score=0, up=1, down=1 |
| T006 | 不同 event 独立 | user A 对 event#1 up + 对 event#2 down（同 runtime_id） | DB 有 2 条记录；JSON total_score=0, up=1, down=1 |
| T007 | 读取不存在的 engine | read_engine_rating(99999) | 返回 {total_score:0, up:0, down:0, records:[]} |
| T008 | 读取未评价 event | read_event_rating(seq=999, user="A") | 返回 None |
| T009 | JSON 文件重建 | 多次操作后 _rebuild_rating_file | JSON records 数量与 DB 一致；total_score 正确 |

### 9.2 API 测试 — test_event_rating.py

| 用例 ID | 用例名 | 操作 | 预期结果 |
|---------|--------|------|---------|
| T010 | POST rate 点赞 | POST /events/{seq}/rate {action:"up"} | 200；返回 engine_rating.total_score=1 |
| T011 | POST rate 踩 | POST /events/{seq}/rate {action:"down"} | 200；返回 engine_rating.total_score=-1 |
| T012 | POST rate 取消 | 先 up 再 POST {action:"up"} | 200；返回 my_rating=null, total_score=0 |
| T013 | POST rate 切换 | 先 up 再 POST {action:"down"} | 200；返回 my_rating.action="down" |
| T014 | POST rate 非法 action | POST {action:"sideways"} | 400 |
| T015 | GET rating 无评价 | GET /events/{seq}/rating | 200；my_rating=null, engine_rating.total_score=0 |
| T016 | GET rating 有评价 | 先 POST up 再 GET | 200；my_rating.action="up" |
| T017 | event detail 含评价 | GET /events/detail/{seq} | 返回体含 my_rating + engine_rating 字段 |

### 9.3 知识积累测试

| 用例 ID | 用例名 | 前置条件 | 操作 | 预期结果 |
|---------|--------|---------|------|---------|
| T020 | running 状态点赞写入知识 | event status=running | POST rate {action:"up"} | issue 级 knowledge.md 追加一行；event_thread 有 system 消息 |
| T021 | completed 状态点赞不写知识 | event status=completed | POST rate {action:"up"} | knowledge.md 不变；event_thread 有 system 消息 |
| T022 | running 状态踩写入知识 | event status=running | POST rate {action:"down"} | issue 级 knowledge.md 追加一行（含"踩"） |

### 9.4 前端手动验证

| 用例 ID | 用例名 | 操作 | 预期结果 |
|---------|--------|------|---------|
| T030 | 详情页显示评价按钮 | 打开任意 event 详情 | Status 行下方有 👍 👎 按钮 + Engine 评分 |
| T031 | 点赞高亮 | 点 👍 | 👍 按钮变绿高亮；评分显示 +1 |
| T032 | 切换为踩 | 再点 👎 | 👎 按钮变红高亮，👍 取消高亮；评分显示 -1 |
| T033 | 取消评价 | 再点 👎 | 👎 取消高亮；评分显示 0 |
| T034 | 重新打开详情页回显 | 点赞后关闭再打开 | 👍 按钮高亮（状态持久化） |
| T035 | 不同 event 评分独立 | engine A 在 event#1 +1，event#2 -1 | Engine 评分显示 0（累计） |
| T036 | running 状态点赞 toast | event 运行中点 👍 | toast "已点赞"；线程有 system 消息 |

### 9.5 端到端集成测试

| 用例 ID | 用例名 | 步骤 | 预期结果 |
|---------|--------|------|---------|
| T040 | 完整点赞流程 | 1. 打开 event 详情<br>2. 点 👍<br>3. 查看引擎评分<br>4. 关闭详情<br>5. 重新打开 | 评分正确；按钮高亮状态持久化 |
| T041 | running 点赞影响知识 | 1. event 处于 running<br>2. 点 👍<br>3. 查看该 issue 的 knowledge.md | knowledge.md 有追加行 |
| T042 | 多用户独立评价 | 1. user A 点 👍<br>2. user B 点 👎<br>3. 查看 engine 评分 | 评分=0（+1-1）；各自按钮高亮独立 |
| T043 | engine 评分跨 event 累计 | 1. engine A 执行 event#1 得 👍<br>2. engine A 执行 event#2 得 👍<br>3. 查看 engine A 评分 | 评分=+2 |

## 10. 向后兼容性

| 场景 | 兼容方案 |
|------|---------|
| 老库无 engine_ratings 表 | 迁移建表，不影响现有数据 |
| get_event_detail 返回新字段 | 老前端忽略 `my_rating` / `engine_rating` 字段，不报错 |
| 评价文件不存在 | read_engine_rating 返回零值，不报错 |
| runtime_id=0（无法识别引擎） | 评价记录在 `unknown.json`，不影响功能 |
| 现有知识库 | running 状态点赞调用现有 `write_knowledge()`，是正常调用不改其逻辑 |
| event_thread | 调用现有 `_add_thread_message()`，不改其逻辑 |
