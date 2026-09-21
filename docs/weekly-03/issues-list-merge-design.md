# issues 仓库列表接口归一方案（`GET /{owner}/{repo}` 收编 `repo/{owner}/{repo}`）

> 分支：`weekly-03-lyz`；状态：**方案（待实施）**；日期：2026-09-16
> 触发：文档评审发现 `GET /api/issues/{owner}/{repo}` 与 `GET /api/issues/repo/{owner}/{repo}`
> 同是「仓库 Issue 列表」，语义重复（两个入口）。

---

## 一、现状：两接口差异

| 维度 | `GET /{owner}/{repo}`（`issues_list`） | `GET /repo/{owner}/{repo}`（`issues_repo_all`） |
|------|:---:|:---:|
| 数据路径 | `_issue_stage_map`+`_task_status_map`+`_group_events_by_issue`+`_assemble_issue` | `tasks._query_tasks`（issue 单表直查 + 富化） |
| 返回字段 | number/title/stage/task_status/owner_agent/scene_id/assignee/is_subtask_mode/parent_issue_id/**events_count/latest_event** | id/issue_number/repo_key/title/status/**scene_id/scene_name/scene_source/stage/is_running/agent_statuses**/assignee/... |
| 排序 | 按最新事件时间倒序 | `created_at` + 状态优先级（`mode`） |
| 结果集 | `stage_map ∪ status_map ∪ group_map`（有事件的 Issue） | `repo_key=?`（全部卡片） |
| 防 IDOR | 无 | `_require_repo_access` |
| scope | dual | dual |

**前端依赖**（repo_all tab 硬性使用）：`scene_id/scene_name/scene_source`（徽标）、`is_running`（动画）、
`agent_statuses`（详情）、`assignee/status`（筛选）。`issues_list` 目前**不返回**这些字段。

---

## 二、方案 A：`issues_list` 收编 repo_all，下线 `/repo/{owner}/{repo}`

### 2.1 改造 `GET /api/issues/{owner}/{repo}`（`issues_list`）

把实现从「事件聚合」改为复用 `tasks._query_tasks`（同 repo_all），**并保留事件聚合字段**：

```python
@router.get("/{owner}/{repo}", operation_id="issues_list", summary="仓库 Issue 列表（含阶段/状态聚合）")
def list_repo_issues(owner, repo, status=None, sort_order="desc", mode="default",
                     assignees="", user=$guard):
    repo_key = _repo_key_of(owner, repo)
    from src.api.routes.tasks import _query_tasks, _require_repo_access, _build_assignee_filter
    _require_repo_access(user, repo_key)            # 防 IDOR（原 repo_all 的）
    # 过滤/排序逻辑 = 原 repo_all 全量搬迁
    ...
    issues = _query_tasks(where, params, order_by, include_user_id=True)
    # 保留事件聚合字段：按 repo_key+issue_number 批量补 events_count/latest_event
    group_map = _group_events_by_issue(repo_key)
    for it in issues:
        evs = group_map.get(str(it["issue_number"])) or []
        it["events_count"] = len(evs)
        it["latest_event"] = evs[-1] if evs else None
    return {"issues": issues, "total": len(issues)}
```

- 返回项为**超集**：`_query_tasks` 的 `issue_number` 对应原 `number`（前端已用 `t.issue_number`），
  额外带 `events_count/latest_event`（兼容原 `issues_list` 消费方）。
- scope 推导不变：认证仍 `get_current_user_or_api_key` → **dual**（`_require_repo_access` 是体内校验）。
- 契约：`issues_list` operation_id 与路径不变，仅返回结构增强。

### 2.2 下线 `GET /api/issues/repo/{owner}/{repo}`（`issues_repo_all`）

- 删除 `demo.issues_repo_all` 路由与函数（`issues.py`）。
- 契约：`issues_repo_all` operation_id 移除 → `test_openapi.EXPECTED_PUBLIC` 删条目；
  scope 抽查（repo_all 曾 dual）随之移除/替换。

### 2.3 前端（`issues.js`）切数据源

- 仓库全部 tab（`_tasksViewMode==='repo_all'`）改调 `GET /api/issues/{owner}/{repo}`；
  个人 tab（mine）已走 `/issues/mine` 不变。
- `loadTasks()`：repo_all 分支 URL 从 `/issues/repo/{owner}/{repo}` → `/issues/{owner}/{repo}`；
  返回解析 `d.tasks` → `d.issues`（需与 mine/新 list 统一为 `d.issues`）——统一 `resolve` 用 `d.issues || d.tasks`。

### 2.4 契约测试

- `test_openapi.py`：`EXPECTED_PUBLIC` 移除 `issues_repo_all`（`/api/issues/repo/{owner}/{repo}`）；
  scope 抽查中的 `repo/{owner}/{repo}` dual 项改指 `/{owner}/{repo}`（仍 dual）。

### 2.5 文档

- `11-issues.md`：`repo/{owner}/{repo}` 条目删除；`/{owner}/{repo}` 条目补富化字段说明
  （scene/stage/is_running/agent_statuses + events_count/latest_event + scope dual + 403 无权）。
- `README.md` id一一览表：issues 行去掉 repo/{owner}/{repo}。

---

## 三、影响与兼容

| 项 | 影响 |
|----|------|
| API | `issues_list` 返回字段增强（超集），原字段仍在 → 兼容；`issues_repo_all` 下线（404） |
| 前端 | 仓库全部 tab 改 URL + 字段解析（`d.issues`），一处改动；mine 不受影响 |
| CLI | `grape issues` 契约注册表移除 repo_all 命令；list 命令仍可用（字段增强） |
| 契约 | 路径 -1（repo_all），operation_id -1；`test_openapi` 同步 |
| scope | list/repo_all 均为 dual，无变化 |

## 四、验收

1. `GET /api/issues/{owner}/{repo}`：返回含富化字段（scene_name/is_running/agent_statuses）+ events_count/latest_event；无权仓库 403。
2. `/api/issues/repo/{owner}/{repo}` 返回 404（已下线）。
3. 前端需求中心「仓库全部」tab 正常渲染（场景徽标/雷达/详情/筛选）。
4. `test_openapi` 全绿；契约路径数 -1。
5. `mine`（user_only）与 `create`（user_only）回归不破坏。

> TODO（实施时）：issues.py 改 list + 删 repo_all → 前端 issues.js 切源 → test_openapi → 文档 → 回归。