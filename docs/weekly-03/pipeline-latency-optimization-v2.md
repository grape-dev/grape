# 流水线页面加载时延优化方案（第二轮）

## 一、问题定位

### 1.1 第一轮优化效果

第一轮已修复：
- N+1 标题查询 → 批量查询（11ms → 2ms）
- 去除 payload 字段（7.1MB → 200KB）

### 1.2 当前瓶颈

精确计时结果：

| 步骤 | 耗时 | 说明 |
|------|------|------|
| events 查询 | 11ms | 已优化 |
| 分组 | 1ms | - |
| 批量标题查询 | 2ms | 已优化 |
| **build pipelines（186 次 _get_issue_pipeline）** | **1017ms** | **瓶颈** |
| running issues 查询 | 1ms | - |
| tasks_without_events | 2ms | - |
| 排序 | 1ms | - |
| **总计** | **1034ms** | |

### 1.3 _get_issue_pipeline 内部的 N+1 查询

`_get_issue_pipeline()` 对每个 Issue 执行以下 DB 查询（无缓存）：

| 子调用 | 调用次数 | 总耗时 | 查询内容 |
|--------|---------|--------|---------|
| get_issue_stage() | 187 次 | 203ms | `SELECT ... FROM issue WHERE repo_key=? AND issue_number=?` |
| get_issue_scene() | 187 次 | 203ms | 内部再调 get_issue_stage()（重复查询） |
| get_pipeline_config() | 187 次 | 396ms | `SELECT ... FROM devops_pipeline_scene WHERE scene_id=? AND repo_key=?` |
| get_all_bot_usernames() | 187 次 | 124ms | `SELECT bot_username FROM managed_repos WHERE repo_key=?` |
| _resolve_feishu_name() | 187 次 | 65ms | 查 unified_user_mapping |
| **合计** | ~930 次 | **991ms** | |

**核心问题：虽然标题查询的 N+1 已修复，但 _get_issue_pipeline 内部仍有 5 类 N+1 查询，每次请求执行约 930 次 DB 查询。**

---

## 二、优化方案

### 2.1 批量查 issue stage（替代 get_issue_stage 逐条查询）

在 `get_pipeline()` 中一次性查出所有 Issue 的 stage/scene_id/assignee 等字段，传入 `_get_issue_pipeline()`。

文件：`src/api/routes/pipeline.py`，函数 `get_pipeline`

```python
# 批量查 issue stage（消除 _get_issue_pipeline 内部的 get_issue_stage N+1）
_stage_map = {}
if issue_ids:
    _iss_nums = [int(iid.lstrip('#')) for iid in issue_ids if iid.lstrip('#').isdigit()]
    if _iss_nums:
        with db_lock:
            c = db.cursor()
            ph = ",".join(["%s"] * len(_iss_nums))
            c.execute(
                f"SELECT issue_number, stage, owner_agent, assignee, scene_id, "
                f"scene_user_id, parent_issue_id, is_subtask_mode "
                f"FROM issue WHERE repo_key=%s AND issue_number IN ({ph})",
                (repo_key, *_iss_nums)
            )
            for r in c.fetchall():
                _stage_map[f"#{r[0]}"] = {
                    "stage": r[1] or "", "owner_agent": r[2] or "",
                    "assignee": r[3] or "", "scene_id": r[4] or "",
                    "scene_user_id": r[5] or 0, "parent_issue_id": r[6] or "",
                    "is_subtask_mode": r[7] or 0,
                }
```

修改 `_get_issue_pipeline()` 接受 `stage_map` 参数，优先从 map 取，不再逐条查 DB：

```python
def _get_issue_pipeline(repo_key, issue_id, events, title_map=None, stage_map=None):
    _st = (stage_map or {}).get(issue_id) or {}
    # 后续逻辑直接用 _st，不再调 get_issue_stage / get_issue_scene
    _scene_id = _st.get("scene_id", "")
    if not _scene_id:
        _scene_id = ""  # 不再回退 get_issue_scene（它内部就是调 get_issue_stage）
    ...
```

**预期收益：203ms + 203ms → 2ms，节省约 400ms**

### 2.2 缓存 get_pipeline_config（场景配置去重）

同一个仓库的场景数量有限（通常 1-5 个），但 186 个 Issue 可能引用同一个 scene_id，导致 `get_pipeline_config()` 被重复调用 186 次。

在 `get_pipeline()` 中按 scene_id 去重，缓存查询结果：

```python
# 场景配置缓存（同仓库内 scene_id 去重）
_pipe_cfg_cache = {}
def _get_pipe_cfg(scene_id):
    if scene_id not in _pipe_cfg_cache:
        _pipe_cfg_cache[scene_id] = get_pipeline_config(scene_id, repo_key=repo_key) if scene_id else None
    return _pipe_cfg_cache[scene_id]
```

修改 `_get_issue_pipeline()` 接受 `pipe_cfg_cache` 或 `get_pipe_cfg` 回调：

```python
# 在 _get_issue_pipeline 内部：
_pipe_cfg = _get_pipe_cfg(_scene_id) if _scene_id else None
```

**预期收益：396ms → 5ms（5 个场景 × 1 次查询），节省约 390ms**

### 2.3 缓存 get_all_bot_usernames（全局缓存）

`get_all_bot_usernames()` 查的是 `managed_repos` 表，同一个仓库的 bot_username 列表在同一请求内不变。在 `get_pipeline()` 中查一次传入：

```python
# 一次性查 bot usernames
_bot_usernames = set()
try:
    with db_lock:
        c = db.cursor()
        c.execute("SELECT bot_username FROM managed_repos WHERE repo_key=%s AND bot_username!=''", 
                  (repo_key,))
        _bot_usernames = {r[0] for r in c.fetchall() if r[0]}
except Exception:
    pass
```

修改 `_get_issue_pipeline()` 接受 `bot_usernames` 参数。

**预期收益：124ms → 1ms，节省约 120ms**

### 2.4 缓存 _resolve_feishu_name（用户名去重）

同一个仓库的 Issue 责任人通常只有几个（songspace/grape_bot 等），但 186 个 Issue 每个都调一次。在请求级缓存：

```python
# 飞书用户名缓存（请求级）
_feishu_name_cache = {}
def _resolve_feishu_name_cached(name):
    if name not in _feishu_name_cache:
        _feishu_name_cache[name] = _resolve_feishu_name(name)
    return _feishu_name_cache[name]
```

**预期收益：65ms → 2ms，节省约 60ms**

---

## 三、预期效果

| 步骤 | 优化前 | 优化后 | 节省 |
|------|--------|--------|------|
| events 查询 | 11ms | 11ms | - |
| 批量标题 | 2ms | 2ms | - |
| get_issue_stage × 187 | 203ms | 2ms（批量） | 200ms |
| get_issue_scene × 187 | 203ms | 0ms（从 stage_map 取） | 203ms |
| get_pipeline_config × 187 | 396ms | 5ms（去重缓存） | 391ms |
| get_all_bot_usernames × 187 | 124ms | 1ms（一次查） | 123ms |
| _resolve_feishu_name × 187 | 65ms | 2ms（去重缓存） | 63ms |
| **总计** | **1034ms** | **~25ms** | **~1000ms** |

---

## 四、修改清单

| 步骤 | 文件 | 修改内容 | 预期节省 |
|------|------|---------|---------|
| 1 | src/api/routes/pipeline.py | 批量查 issue stage，传入 _get_issue_pipeline | 400ms |
| 2 | src/api/routes/pipeline.py | 场景配置按 scene_id 去重缓存 | 390ms |
| 3 | src/api/routes/pipeline.py | bot_usernames 一次查询传入 | 120ms |
| 4 | src/api/routes/pipeline.py | _resolve_feishu_name 请求级缓存 | 60ms |
| 5 | src/api/routes/pipeline.py | _get_issue_pipeline 接受新参数 | - |
| 合计 | | | ~970ms |

---

## 五、验证

1. 对比优化前后 `GET /api/pipeline/grape-dev/grape` 的 `duration_ms`
2. 确认返回数据内容不变（标题、状态、场景、责任人）
3. 确认 DB 查询数从 ~930 次降到 ~5 次
4. 多次刷新确认无内存泄露（缓存是请求级的，请求结束即释放）
