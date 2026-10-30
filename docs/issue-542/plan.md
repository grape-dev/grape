# Issue #542 方案设计

基线：`weekly-09-ljt`，开发分支 `feat/issue-542`。全部改动为纯读路径 + 配置，不改变既有响应字段语义（仅新增可选字段/参数）。

## 1. pipeline 索引修复（src/api/routes/pipeline.py）

### 1.1 `_get_issue_pipeline()` 增加 `events_by_issue` 参数

```python
def _get_issue_pipeline(repo_key, issue_id, events, ..., events_by_issue: dict = None):
    if events_by_issue is not None:
        issue_events = events_by_issue.get(issue_id) or []
    else:
        issue_events = [e for e in events if e.get("issue_id") == issue_id]  # 旧调用兼容
```

- 调用方 `get_pipeline()` 传入已预建的 `_events_by_issue`。
- 复杂度：766 issue × 3457 events 全量扫描（≈265 万次）→ 每事件一次 dict.get（O(events)）。
- PR 订阅伪 ID 跨 issue 标题兜底：有索引时按索引键匹配 `#pr_{num}_`（O(issues)），无索引时保留原全量遍历。

### 1.2 history 截断

`get_pipeline()` 新增 `history_limit: int = Query(50, ge=1, le=500)`：

- `history = pipelines[1:][:history_limit]`；响应新增 `history_total`（全量条数）与 `history_limit`。
- `today_count`/`failed_count`/`active_count` 仍按全量 pipelines 统计（统计口径不变）。
- 前端 pipeline.js 本来就对 history 客户端分页（`_PIPE_PER_PAGE=10`），截断兼容。

## 2. issues 真分页 + 终态截断（src/api/routes/issues.py + src/api/issues_center.py）

### 2.1 端点签名

```python
def list_repo_issues(owner, repo, status=..., sort_order=..., mode=..., assignees=...,
                     page: int = Query(1, ge=1),
                     per_page: int = Query(0, ge=0, le=500),      # 0=不分页（默认，兼容旧前端）
                     recent_terminal: int = Query(30, ge=0),      # 终态（completed/cancelled）截断条数，0=全量
                     user=...)
```

### 2.2 三种模式

| 模式 | 触发 | 行为 |
|---|---|---|
| 真分页 | `per_page > 0` | `COUNT(*)` + `LIMIT ? OFFSET ?`；响应 `{issues, total, page, per_page, has_more}` |
| 终态截断（默认） | `per_page=0` 且无 `status` 过滤且 `recent_terminal>0` | 两次查询：非终态全量（status IN active/processing/awaiting_input/pending_review）+ 终态按 created_at DESC LIMIT N；合并后按原排序语义排序 |
| 全量（legacy） | `recent_terminal=0` 或显式 `status` 过滤 | 原单查询全量返回（显式过滤 = 调用方明确要该状态全量） |

- 排序语义保持：`mode=default`（状态优先级 CASE 0-4 + created_at）时两段拼接即全局序；`mode=time` 时合并后按 created_at 重排。
- `total`：分页模式 = COUNT（issue 行数，不含 PR 伪卡片）；非分页模式 = len(issues)（与现状一致）。
- PR 订阅伪卡片在分页模式下只混入第 1 页（避免逐页重复）；`has_more` 按 issue 行数计算（不受 PR 卡片影响）。

### 2.3 事件聚合收窄（性能关键）

- `_group_events_by_issue(repo_keys, numbers=None)`：`numbers` 非空时 SQL 加 `AND issue_id IN ('#n1',...)`，只格式化返回集合的事件（events 表 issue_id 规范写路径恒为 `#N`，PR 事件 `#pr_...` 由独立函数处理不受影响）。
- `_issue_stage_map(repo_keys, numbers=None)`：同理加 `AND issue_number IN (...)`（issue 单表）/ `AND issue_id IN ('#n1',...)`（issue_state 兜底）。
- `_query_tasks(..., limit=None, offset=0)`：追加 `LIMIT ? OFFSET ?`（None=不限）。
- PR 订阅伪卡片 `qa_history` 每卡截最近 50 条（原为全量事件历史，是 854KB 响应的重要组成）。

### 2.4 前端兼容

- 前端 issues.js 调用不带新参数 → 走默认终态截断，响应形状不变（`issues`+`total`），客户端分页/筛选逻辑天然兼容有界数组。
- 需要全量历史的调用方传 `recent_terminal=0`；需要翻页的调用方传 `page`/`per_page`。

## 3. branches TTL 缓存（src/api/routes/scenes.py）

- 进程内缓存：`{f"{provider}:{owner}/{repo}": (timestamp, payload)}`，TTL 90s（文档建议 60~120s 取中值），`threading.Lock` 保护（同步端点跑线程池）。
- 命中：直接返回缓存 payload + `"cached": true`（新增字段，前端只读 `branches`，不受影响）。
- 未命中/过期：穿透外部 API，成功才入缓存；502/503 错误不入缓存。
- `refresh=1` 查询参数：绕过缓存强制刷新（手动刷新入口）。
- `create_repo_branch` 成功后：失效该 owner/repo 两平台缓存键（建分支后立即可见，前端建分支后会立刻重新拉取）。

## 4. nginx gzip（nginx-grape.conf）

```nginx
gzip on;
gzip_types application/json;
gzip_min_length 10k;
gzip_comp_level 5;
gzip_vary on;
```

仅压缩 application/json（text/html 为 nginx 内置默认），避免与既有中间件冲突；预期 1.4MB → 100~150KB。

## 5. 风险与缓解

| 风险 | 等级 | 缓解 |
|---|---|---|
| 分页/截断改变数据可见性 | Med | 默认仅截断终态（completed/cancelled 旧数据），非终态全量；`recent_terminal=0`/显式 status 可取全量；响应形状不变 |
| 索引传参引入行为差异 | Low | 无索引时保留原线性过滤路径（旧调用兼容）；单测对比两路径结果一致 |
| branches 缓存陈旧 | Low | TTL 90s + `refresh=1` + 建分支主动失效 |
| gzip 与已有中间件冲突 | Low | 仅 application/json，sre 验证响应头 |
| `total` 语义分叉 | Low | 非分页模式与现状一致（len），分页模式为 COUNT（新语义仅在新参数下出现） |

## 6. 测试计划

- `tests/test_pipeline.py` 新增：索引路径与线性路径结果一致性、索引缺失 issue 返回 None、PR 跨 issue 标题兜底走索引、`history_limit` 截断 + `history_total`。
- `tests/test_issues_api.py` 新增：默认终态截断（35 completed → 30）、`recent_terminal=0` 全量、显式 status 不过滤、`per_page` 真分页（total/has_more/offset）、排序语义（mode=time 合并重排）。
- `tests/test_scenes_branches_cache.py` 新增：缓存命中（mock 外部 API 仅调一次）、`refresh=1` 绕过、建分支失效、TTL 过期重取。
