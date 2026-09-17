# 流水线页面加载时延优化方案

## 一、问题定位

### 1.1 实测数据

| 仓库 | 事件数 | Issue 数 | 接口平均耗时 | P99 |
|------|--------|---------|-------------|-----|
| grape-dev/grape | 842 | 182 | **1003ms** | 1016ms |
| grape-dev/grape-admin | - | - | 308ms | - |
| developer-skill/huaweicloud-skills-dev | - | - | 230ms | 252ms |
| dongzhiri/scene-test | - | - | 76ms | 140ms |
| developer-skill/common-skill-dev | - | - | 14ms | 21ms |

grape-dev/grape 仓库的流水线接口耗时约 1 秒，是其他仓库的 5-10 倍。

### 1.2 根因分析

**根因一：N+1 查询（主要瓶颈）**

`get_pipeline()` 接口流程：
1. 一次查全量 events（842 条，含 payload 总量 7.1MB）
2. 按 issue_id 分组得到 182 个 Issue
3. 对每个 Issue 调用 `_get_issue_pipeline()`，内部执行 `SELECT title FROM issue WHERE repo_key=? AND issue_number=?` 查标题

→ **182 次独立 DB 查询**（N+1 问题），每次约 5ms，累计约 900ms

**根因二：全量 payload 传输**

events 表查询 `SELECT seq, issue_id, event_type, agent_type, title, summary, status, created_at, actor, payload, duration, repo_key` 把 842 条事件的 payload 字段全查出来了。payload 平均 8436 字节、总量 7.1MB，但 pipeline 列表展示只需要 title/status/agent_type/created_at/seq，payload 完全用不到。

**根因三：前端串行加载**

进入 #pipe 页面时 `app.js` 先调 `loadDash()`（dashboard 接口）再调 pipeline 接口，串行执行。dashboard 接口约 50-80ms，pipeline 接口约 1000ms，加上 scenes/with-pipeline 约 10ms，总加载时间约 1100ms。

---

## 二、优化方案

### 2.1 消除 N+1：批量查标题

文件：`src/api/routes/pipeline.py`，函数 `get_pipeline`

在遍历 issue_ids 之前，一次性查出所有 Issue 的标题：

```python
# 批量查标题（消除 N+1）
_title_map = {}
if issue_ids:
    _iss_nums = []
    for iid in issue_ids:
        _n = str(iid).lstrip("#")
        if _n.isdigit():
            _iss_nums.append(int(_n))
    if _iss_nums:
        with db_lock:
            c = db.cursor()
            _ph = ",".join(["%s"] * len(_iss_nums)) if _iss_nums else ""
            c.execute(
                f"SELECT issue_number, title FROM issue WHERE repo_key=%s AND issue_number IN ({_ph})",
                (repo_key, *_iss_nums)
            )
            for r in c.fetchall():
                _title_map[f"#{r[0]}"] = r[1] or ""
```

然后修改 `_get_issue_pipeline()` 接受 `_title_map` 参数，优先从 map 取标题，不再逐条查 DB：

```python
def _get_issue_pipeline(repo_key, issue_id, events, title_map=None):
    # 优先从批量 map 取标题
    title = ""
    if title_map:
        title = title_map.get(issue_id, "")
    # 占位符检测 + events 兜底（原有逻辑）
    ...
```

**预期收益：182 次 DB 查询 → 1 次，节省约 900ms**

### 2.2 去除无用 payload 字段

文件：`src/api/routes/pipeline.py`，函数 `get_pipeline`

pipeline 列表展示只需要 seq/issue_id/event_type/agent_type/title/summary/status/created_at/actor/duration/repo_key。payload 字段仅在 `_get_issue_pipeline` 内部做标题兜底时用到（从 issue_opened 的 payload 提取标题），但根因一修复后标题已有批量查询兜底，payload 兜底几乎不会触发。

修改 events 查询，去掉 payload：

```python
c.execute(
    "SELECT seq, issue_id, event_type, agent_type, title, summary, status, "
    "created_at, actor, duration, repo_key "
    "FROM events WHERE repo_key IN ({}) AND issue_id != '' ORDER BY seq ASC".format(
        ",".join(["%s"] * len(_repo_keys))
    ),
    _repo_keys
)
```

**预期收益：传输数据从 7.1MB → 约 200KB，节省网络和序列化时间约 100-200ms**

### 2.3 前端并行加载

文件：`src/api/static/js/app.js`

当前进入 pipe 页面时串行调用 dashboard + pipeline + scenes。改为并行：

```js
} else if (view === 'pipe') {
    title.textContent = I18N.t('app.current_project');
    // 并行加载 pipeline + dashboard
    API.get('/pipeline/' + repo.owner + '/' + repo.name).then(d => {
        if (window._currentView !== 'pipe') return;
        renderPipeline(d, param);
    }).catch(e => { ... });
    // dashboard 不阻塞 pipeline 渲染
    loadDash();
    _refreshTimer = setInterval(loadDash, 30000);
}
```

当前实际已经是这样（pipeline 和 loadDash 并行调用），但 `renderPipeline` 内部如果依赖 dashboard 数据则需要确认。从代码看 `renderPipeline` 只用 `_pipeData`（pipeline 接口返回），不依赖 dashboard，所以已并行。

**此项无需改动，瓶颈在后端。**

### 2.4 pipeline 数据缓存（可选增强）

如果优化后仍需进一步提速，可在后端增加 5 秒短缓存：

```python
# pipeline.py
import time as _time
_pipeline_cache = {}  # {repo_key: (data, expires)}

@router.get("/{owner}/{repo}")
async def get_pipeline(owner, repo, user):
    repo_key = to_repo_key(f"{owner}/{repo}")
    
    # 检查缓存
    cached = _pipeline_cache.get(repo_key)
    if cached and _time.time() < cached[1]:
        return cached[0]
    
    # ... 原有逻辑 ...
    result = {...}
    
    # 写入缓存（5 秒 TTL）
    _pipeline_cache[repo_key] = (result, _time.time() + 5)
    return result
```

**注意：** 缓存需在任务状态变更时失效（任务终态、A2A 事件创建时清除对应 repo 的缓存）。

---

## 三、修改清单

| 步骤 | 文件 | 修改内容 | 预期收益 |
|------|------|---------|---------|
| 1 | src/api/routes/pipeline.py | 批量查标题消除 N+1 | -900ms |
| 2 | src/api/routes/pipeline.py | events 查询去掉 payload 字段 | -100~200ms |
| 3 | src/api/routes/pipeline.py | _get_issue_pipeline 接受 title_map 参数 | 配合步骤 1 |
| 4 | (可选) src/api/routes/pipeline.py | 5 秒短缓存 | 进一步减少重复请求 |

---

## 四、预期效果

| 指标 | 优化前 | 优化后 |
|------|--------|--------|
| grape-dev/grape pipeline 接口耗时 | ~1000ms | ~100-150ms |
| DB 查询次数 | 183 次（1 + 182） | 2-3 次 |
| 数据传输量 | ~7.1MB | ~200KB |
| 页体感加载时间 | ~1.1s | ~200ms |

---

## 五、验证方案

1. 对比优化前后 `GET /api/pipeline/grape-dev/grape` 的 `duration_ms`
2. 确认 pipeline 页面展示内容不变（标题、状态、事件历史）
3. 确认无 payload 时标题仍正确展示（从 issue 表批量查询获取）
4. 压测：连续刷新 10 次，确认无 DB 连接泄露
