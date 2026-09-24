# 场景引擎绑定失效（pool_engine_ids 为空）根因分析与修复方案

## 一、问题现象

GTDTeam/GTD-Portal 仓库的场景"吕军涛的gtd开发流水线"（scene_id=b109183f）配置了 coding/qa/sre 节点指定引擎 5933，但实际任务运行时全部跑到了其他机器上。

| 节点 | 配置引擎 | 实际运行引擎 | pool_engine_ids | 是否匹配 |
|------|---------|-------------|----------------|---------|
| vod | []（空） | 1105 (codearts) | [] | 广播，正常 |
| issue | []（空） | 1089 (opencode) | [] | 广播，正常 |
| coding | [5933] | 1092 (opencode) | **[]** | **不匹配** |
| qa | [5933] | 1101→2639 | **[]** | **不匹配** |
| sre | [5933] | 1094 (hermes) | **[]** | **不匹配** |

## 二、根因分析

### 2.1 A2A 事件的分发路径

A2A 交办（coding → qa → sre）走的是 **event bus → pipeline_executor → _enqueue_via_pipeline** 路径，不是 event_pipeline.py 主分发路径。

```
Issue Agent 完成
  → POST /api/a2a (agent/handoff)
  → event_bus.emit("agent/handoff")
  → webhook.py _on_handoff_dispatch()
  → pipeline_executor.dispatch_node()
  → _dispatch_emit()
  → _enqueue_via_pipeline()    ← 关键
  → _enqueue_pipeline_task()
```

### 2.2 _enqueue_via_pipeline 中的 scene_id 获取逻辑

文件：`src/api/pipeline_executor.py`，函数 `_enqueue_via_pipeline`，第 240-260 行

```python
if is_plugin_mode():
    _scene_id = "default"
    _scene_uid = 0
    try:
        from src.api.session_store import get_issue_scene
        _scene_id, _scene_uid = get_issue_scene(repo_key, issue_id)
        _scene_id = _scene_id or "default"
    except Exception:
        pass
```

`get_issue_scene()` 内部调用 `get_issue_stage()`，从 `issue_state` 表读 `scene_id`。

### 2.3 issue_state 表不存在记录

Issue #5 的 `issue_state` 表中 **NOT FOUND**——没有 scene_id 记录。

```
get_issue_scene() 返回 ("", 0)
→ _scene_id = "" or "default" = "default"
→ get_scene_pool_engine_ids("default", "coding", ...) 
→ default 场景没有 engine_runtime_ids
→ 返回 ""（空字符串）
→ pool_engine_ids="" 写入 agent_tasks
→ 任务变成全量广播
→ 被其他机器的 global 引擎抢走
```

### 2.4 issue_state 为什么不存在

`issue_state` 表在 Phase C3 架构变更后已停写（代码注释说明："issue 单表为权威，issue_state 仅作停写过渡期兜底"）。但 `get_issue_scene()` 仍然从 `issue_state` 读 scene_id，没有回退到 `issue` 表的 `scene_id` 字段。

Issue #5 的 `issue` 表中 `scene_id=b109183f-8105-450f-928c-f34a6cf84549` 是正确的，但 `issue_state` 表没有记录，`get_issue_scene()` 读不到。

### 2.5 对比：主分发路径为何正常

`event_pipeline.py` 主分发路径（第 1503 行 a2a_event 分支）使用的是 `_issue_scene` 变量，这个变量在第 1350 行之前从 `issue` 表读取（而非 issue_state），所以主路径能正确获取 scene_id。但 A2A 事件走的是 pipeline_executor 路径，用的是 `get_issue_scene()` 从 issue_state 读，导致不一致。

### 2.6 影响范围

所有通过 A2A 交办的任务（coding/qa/sre）在 issue_state 表无记录时，都会丢失场景引擎绑定，变成全量广播。vod/issue 节点因为 engine_ids 本来就是空的（广播），所以不受影响。

## 三、修复方案

### 3.1 修复 _enqueue_via_pipeline 中的 scene_id 获取

文件：`src/api/pipeline_executor.py`，函数 `_enqueue_via_pipeline`

修改 `get_issue_scene` 调用，增加从 `issue` 表回退读取：

```python
if is_plugin_mode():
    _scene_id = "default"
    _scene_uid = 0
    try:
        from src.api.session_store import get_issue_scene
        _scene_id, _scene_uid = get_issue_scene(repo_key, issue_id)
        _scene_id = _scene_id or "default"
    except Exception:
        pass
    
    # 修复：issue_state 无记录时，从 issue 表回退读 scene_id
    if _scene_id == "default":
        try:
            from src.session_manager import db, db_lock
            _iss_num = int(str(issue_id).lstrip("#"))
            with db_lock:
                c = db.cursor()
                c.execute(
                    "SELECT scene_id, scene_user_id FROM issue "
                    "WHERE repo_key=? AND issue_number=?",
                    (repo_key, _iss_num)
                )
                _r = c.fetchone()
                if _r and _r[0]:
                    _scene_id = _r[0]
                    _scene_uid = _r[1] or _scene_uid
        except Exception:
            pass
```

### 3.2 同步修复 get_issue_scene 函数（治本）

文件：`src/api/session_store.py`，函数 `get_issue_scene`

增加从 `issue` 表回退读取：

```python
def get_issue_scene(repo_key: str, issue_id: str) -> tuple:
    st = get_issue_stage(repo_key, issue_id)
    if st and st.get("scene_id"):
        return st["scene_id"], st.get("scene_user_id", 0) or 0
    # 修复：issue_state 无记录时，从 issue 表回退
    try:
        from src.session_manager import db, db_lock
        _num = int(str(issue_id or "").lstrip("#"))
        with db_lock:
            c = db.cursor()
            c.execute(
                "SELECT scene_id, scene_user_id FROM issue "
                "WHERE repo_key=? AND issue_number=?",
                (repo_key, _num)
            )
            r = c.fetchone()
            if r and r[0]:
                return r[0], r[1] or 0
    except Exception:
        pass
    return "", 0
```

### 3.3 同步修复 get_issue_stage 函数

文件：`src/api/session_store.py`，函数 `get_issue_stage`

确保从 `issue` 表（权威源）读取，而非仅依赖 `issue_state` 表：

当前代码已经有 issue 表读取逻辑（Phase C3），需确认 scene_id 字段是否在查询列表中。如果不在，需要加上。

## 四、修改清单

| 步骤 | 文件 | 修改内容 | 优先级 |
|------|------|---------|--------|
| 1 | src/api/pipeline_executor.py | _enqueue_via_pipeline 增加 issue 表回退读 scene_id | P0 |
| 2 | src/api/session_store.py | get_issue_scene 增加 issue 表回退 | P1 |
| 3 | src/api/session_store.py | get_issue_stage 确认包含 scene_id 字段 | P1 |

## 五、验证

1. 在 GTDTeam/GTD-Portal 创建新 Issue，触发 A2A 交办 coding
2. 检查 agent_tasks.pool_engine_ids 是否为 `[5933]`（非空）
3. 确认任务被 5933 引擎认领（而非其他 global 引擎抢走）
4. 检查日志 `Pipeline enqueue (broadcast): ... pool_engine_ids=[5933]`
