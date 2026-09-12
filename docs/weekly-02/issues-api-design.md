# Issues 对外接口设计 + Issue/Event/Task 概念澄清

> 目标：新增 `/api/issues` 对外接口，以 Issue 为中心整体开放「阶段 + 事件列表 + pipeline」；
> 同时澄清 Issue / Event / Task 三个易混淆概念（含 "task complete 实为 event complete"）。
> 目标仓库：grape 主仓库（`src/api/routes/` + `help/api/` OpenAPI 契约）
> 状态：**设计文档，待实施**
> 日期：2026-09-11

---

## 一、概念澄清（Issue / Event / Task）

### 1.1 三者本质区别

| 概念 | 本质 | 载体 | 生命周期 | 判别关键 |
|------|------|------|----------|----------|
| **Issue** | GitCode 上的需求/任务单元（`#N`），pipeline 的**主对象** | `issue_state` 表 + GitCode | 创建 → 各阶段 → done/关闭 | **`stage`**（vod/issue/coding/qa/sre/done/pending_review） |
| **Event** | 一次**触发动作**（webhook/A2A），pipeline 的**执行单元** | `events` 表（`seq` 唯一） | 触发 → running → complete/fail | **`seq`** |
| **Task** | 一次 **Agent 执行实例**（引擎会话），事件的具体落执行 | `agent_tasks` 表（`task_id`, `event_seq` 关联） | queued → running → completed/cancelled | **`task_id`**（`t_` 前缀） |

### 1.2 层级关系

```
Issue (#5, stage=qa)
  └── Event (seq=102, agent=qa, event_type=pull_request_updated)
        └── Task (t_xxx, event_seq=102)      ← 引擎实际执行
        └── Task (t_yyy, 重派/重试)            ← 同一事件可多次 task
```

### 1.3 关键澄清：task complete ≠ issue complete

> **`POST /api/tasks/complete` 的 `task_id` 是会话 token 的 `session_key`（M2 以 task_id 签发），
> 它完成的是「agent_tasks 的那一次执行实例」，对应 `event_seq` —— 本质是「某次 Event 执行完成」，
> 而非「Issue 的任务完成 / Issue 完结」。**

| 完成层级 | 表达 | 触发 |
|----------|------|------|
| **Task** 完成 | `agent_tasks.status → completed` | `complete_task`（会话 token） |
| **Event** 完成 | `events.status → completed`（含 result/agent_logs） | `update_event_complete` |
| **Issue 阶段推进** | `issue_state.stage` 更新（owner_agent 流转） | `set_issue_stage` |
| **Issue 完结** | **最后节点（next=done）complete → `pending_review`** → 审核 → done | 状态机 + 开发者审核 |

### 1.4 当前对外 API 的错位（本设计要解决）

- `01-tasks.md` 的 "task" 混用了两种语义：`agent_tasks`（执行实例 id）+ `tasks` 表（Issue 级任务卡片，含
  issue_number/is_subtask_mode/parent_issue_number）。**API 侧 task_id 更多指执行实例，与 Issue 卡片表并列时易混淆。**
- `08-events.md` 是**平铺事件列表**（按 repo 列 seq），**不聚合 Issue 的 stage / 状态 / pipeline**。
- **缺少「按 Issue 看 pipeline」的对外整体视角** —— 正是本设计的缺口。

---

## 二、新增 Issues 接口（/api/issues）

### 2.1 设计目标

以 **Issue 为中心** 对外整体开放：
- Issue 的**阶段**（stage）、当前 agent（owner_agent）、场景（scene_id）
- 该 Issue 对应的**事件列表**（event 流）
- Issue 的 **pipeline 路线图**（event.json 结构化链）

### 2.1.1 归属澄清：`/api/tasks/create` 本质是「Issues 创建」

> 现有 `POST /api/tasks/create`（`01-tasks.md:18`）虽然挂在 `/api/tasks`，但：
> - body = `{repo_key, title, description, assignee, scene_id, is_subtask_mode}` —— 即**创建 GitCode Issue** 的输入
> - 行为：调 GitCode 建 Issue + 建档 `tasks` 行（`ensure_issue_task`）→ 触发 pipeline
> - **结论：这是「Issue 创建」接口，语义上应归属 `/api/issues`，而非 task。**

**设计建议**：
- **新增 `POST /api/issues`（创建）**：作为对外统一入口，语义清晰（对应 /api/issues 列表/详情）。
- `POST /api/tasks/create` **保留为兼容别名**（文档标注「实为创建 Issue，建议改用 /api/issues」），避免破坏现有调用（CLI/Agent 已用）。
- 实现可复用同一逻辑（`ensure_issue_task` / GitCode create），仅路由层包装。

### 2.1.2 Issue/Event/Task 归属对照（接口层）

| 对外接口 | 实际操作对象 | 语义 |
|----------|-------------|------|
| `POST /api/tasks/create`（现） | 创建 GitCode Issue + 建档 | **Issue 创建**（→ 建议 /api/issues） |
| `GET /api/issues/...`（新） | Issue 聚合（stage/events/pipeline） | **Issue 视角** |
| `POST /api/tasks/complete` | 某次执行实例完成（session_key→event_seq） | **Event 执行完成**（非 Issue 完结） |
| `POST /api/tasks/{id}/confirm-done` | 开发者确认 Issue 阶段 → done | **Issue 完结确认**（stage: pending_review→done） |
| `POST /api/tasks/{id}/cancel` | 取消任务 → 置 cancelled + 停流水线 | 任务暂停/取消 |

### 2.2 端点设计

```
GET  /api/issues/{owner}/{repo}                    # 仓库下的 Issue 列表（含 stage 聚合）
POST /api/issues/{owner}/{repo}                    # 创建 Issue（新建，语义归位；复用 tasks/create 逻辑）
GET  /api/issues/{owner}/{repo}/{number}           # 单个 Issue 详情（stage + events + 摘要）
GET  /api/issues/{owner}/{repo}/{number}/events    # 该 Issue 的事件列表
GET  /api/issues/{owner}/{repo}/{number}/pipeline  # Issue pipeline 路线图（event.json 内容）
```

### 2.3 数据来源映射

| 字段 | 来源 | 说明 |
|------|------|------|
| issue 基本信息（title/status/created） | `events` 表首条 issue 事件 或 GitCode API | 首次发生时的标题 |
| **stage** | `issue_state.stage` | 当前阶段（vod/issue/coding/qa/sre/done/pending_review） |
| **owner_agent** | `issue_state.owner_agent` | 当前执行的 agent |
| **a2a_seq** | `issue_state.a2a_seq` | 触发当前 stage 的 A2A 事件号 |
| **scene_id** | `issue_state.scene_id` | 场景 |
| assignee / subtask | `issue_state.assignee / is_subtask_mode / parent_issue_id` | 责任人、子任务 |
| event 列表 | `get_events_grouped_by_issue(repo)` 按 issue_id 过滤 | 全部事件（seq/type/agent/status/时间） |
| pipeline 段 | `load_event_json(repo, number)` | event.json 完整链（各段 structured output/logs） |
| 摘要 | `get_issue_summary(repo)` 对应段 | 概览 |

### 2.4 现有可复用能力（改动小）

| 能力 | 位置 | 用途 |
|------|------|------|
| `get_events_grouped_by_issue(repo_key)` | `event_store.py:505` | 按 issue 聚类事件 |
| `get_issue_stage(repo_key, issue_id)` | `session_store.py:42` | stage/owner/assignee/a2a_seq/scene |
| `load_event_json(repo_key, number)` | `event_archive.py:249` | pipeline 段 / 结构化产出 |
| `get_issue_summary(repo_key)` | `event_store.py:531` | Issue 摘要文本 |
| `normalize_event_time` | `event_store.py` | 时间格式化 |

### 2.5 认证与契约

- 认证：`get_current_user_or_api_key`（三通道：JWT / `tk_` 会话 token / `grape_` API Key）
- 白名单：进 `custom_openapi`，CLI `grape issues ...` 可用
- 文档：新增 `help/api/11-issues.md`，更新 `README.md` 目录 + `tests/test_help_directory.py` `EXPECTED_API_FILES`

### 2.6 出参示例

```json
// GET /api/issues/{owner}/{repo}
{
  "issues": [
    {
      "number": 5,
      "title": "优化前端UI",
      "stage": "qa",
      "owner_agent": "qa",
      "scene_id": "default",
      "assignee": "grape_bot",
      "status": "running",
      "events_count": 6,
      "latest_event": {"seq": 102, "agent": "qa", "status": "completed", "created_at": "..."}
    }
  ],
  "total": 1
}

// GET /api/issues/{owner}/{repo}/5
{
  "number": 5, "title": "优化前端UI", "stage": "qa", "owner_agent": "qa",
  "scene_id": "default", "assignee": "grape_bot", "is_subtask_mode": 0,
  "events": [
    {"seq": 96, "event_type": "issue_opened", "agent": "vod", "status": "completed", "created_at": "..."},
    {"seq": 102, "event_type": "pull_request_updated", "agent": "qa", "status": "completed", "created_at": "..."}
  ],
  "pipeline": {
    "issue": {"number": 5, "title": "...", "status": "running"},
    "summary": "已按审查意见修复 | 待 QA 重新审查",
    "status": "running",
    "segments": [
      {"seq": 96, "agent": "vod", "status": "completed", "output": {"conclusion": "...", "next_step": "a2a_issue"}},
      {"seq": 102, "agent": "qa", "status": "running", "output": {}},
      {"seq": 107, "agent": "sre", "status": "pending", "output": {}}
    ]
  }
}
```

---

## 三、实施范围

| # | 项 | 说明 | 优先级 |
|---|----|------|--------|
| 1 | 新增 `src/api/routes/issues.py` | `/api/issues` 5 个端点（list/create/detail/events/pipeline） | 必须 |
| 2 | 新增 `POST /api/issues/{owner}/{repo}`（创建） | 复用 tasks/create 逻辑（ensure_issue_task + GitCode create）；标记为 Issue 创建官方入口 | 必须 |
| 3 | `POST /api/tasks/create` 标记为兼容别名 | 标注「实为创建 Issue，建议改用 /api/issues」；行为不变 | 必须 |
| 4 | `main.py` 挂载 router | `include_router` | 必须 |
| 5 | `help/api/11-issues.md` | 接口文档 + **概念澄清表**（Issue/Event/Task + task complete≠issue complete + tasks/create 归属） | 必须 |
| 6 | `help/api/01-tasks.md` | 修正 create 措辞：标注「实为创建 Issue」 | 必须 |
| 7 | `help/api/README.md` | 目录加第 11 项 | 必须 |
| 8 | `tests/test_help_directory.py` | `EXPECTED_API_FILES` 加 `11-issues.md` | 必须 |
| 9 | `tests/test_issues_api.py`（新增） | stage/events/pipeline 聚合 + create 断言 | 必须 |
| 10 | OpenAPI 契约 | `custom_openapi` 自动导出 | 自动 |
| 11 | CLI `grape issues` | 从契约生成 | 独立 |

---

## 四、验收标准

- [ ] `GET /api/issues/{owner}/{repo}` 返回仓库 Issue 列表（含 stage/owner_agent/events_count/latest_event）
- [ ] `POST /api/issues/{owner}/{repo}` 创建 Issue（复用 tasks/create 逻辑，触发 pipeline）
- [ ] `GET /api/issues/{owner}/{repo}/{number}` 返回 stage + events + pipeline 聚合
- [ ] `GET .../{number}/events` 仅返回该 Issue 的事件列表
- [ ] `GET .../{number}/pipeline` 返回 event.json 结构化链（含各段 conclusion/next_step）
- [ ] `POST /api/tasks/create` 行为不变（兼容别名），文档标注「实为创建 Issue」
- [ ] **文档 11-issues.md 含概念澄清表**：Issue/Event/Task 三层级、task complete ≠ issue complete、tasks/create 归属
- [ ] 认证三通道可用；契约导出；CLI `grape issues` 可用
- [ ] 现有 events/tasks 接口不回归

---

## 五、测试用例设计

> 基础设施：沿用现有 API 测试模式（`tests/test_api_routes.py` / `test_auth_routes.py`）——
> `TestClient(app)` + 测试 DB + `_jwt_access_token` / `session_token` 注入 + monkeypatch 平台调用。
> 建议新增 `tests/test_issues_api.py`。用 `_mk_issue_state(repo, num, stage, ...)` / `_mk_event(...)` 构造数据。

### 5.1 仓库 Issue 列表（GET /api/issues/{owner}/{repo}）

| # | 用例 | 验证点 | 数据准备 |
|---|------|--------|----------|
| T1 | `test_list_empty_repo` | 无事件/无 issue_state 时返回 `{issues:[], total:0}` | 空仓库 |
| T2 | `test_list_aggregates_stage` | 返回的 issue 含 `stage/owner_agent/scene_id/assignee/events_count/latest_event` | issue_state×2 + 各自 events×2 |
| T3 | `test_list_only_issue_scoped_events` | `events_count` 只统计该 issue 的事件（非全仓库） | issue A events×2 + issue B events×1 |
| T4 | `test_list_sorted_by_latest` | 按最新事件时间倒序；无事件 issue 的排序兜底 | 跨 issue 时间分布 |
| T5 | `test_list_requires_auth` | 未认证 401 | — |

### 5.2 创建 Issue（POST /api/issues/{owner}/{repo}）

| # | 用例 | 验证点 | 数据准备 |
|---|------|--------|----------|
| T6 | `test_create_ok` | 返回 issue_number + 建档 `tasks` + GitCode create 被调 | mock GitCode create_issue |
| T7 | `test_create_missing_fields` | 缺 repo_key/title/description → 400 | — |
| T8 | `test_create_duplicate_title` | 同标题已存在 open Issue → 409 | 已有同标题 event |
| T9 | `test_create_gitcode_fail` | GitCode 建 Issue 失败 → 502 | mock 抛异常 |
| T10 | `test_create_trigger_pipeline` | 创建后触发 pipeline（event_seq 产生 / stage=vod） | mock webhook/事件入队 |

### 5.3 单个 Issue 详情（GET /api/issues/{owner}/{repo}/{number}）

| # | 用例 | 验证点 | 数据准备 |
|---|------|--------|----------|
| T11 | `test_detail_not_found` | 无该 issue 数据 → 404 | — |
| T12 | `test_detail_aggregate` | 返回 stage + owner_agent + scene_id + events[] + pipeline{} | issue_state + events×2 + event.json |
| T13 | `test_detail_events_linked` | events[] 只含该 issue 的事件（seq/event_type/agent/status/time） | 混入其他 issue 事件 |
| T14 | `test_detail_no_issue_state` | 无 issue_state 但 events 存在 → stage 兜底 `vod` 或空 | 仅 events |
| T15 | `test_detail_pipeline_from_json` | pipeline 来自 `load_event_json`（各段 conclusion/next_step） | event.json 已生成 |

### 5.4 Issue 事件列表（GET /api/issues/{owner}/{repo}/{number}/events）

| # | 用例 | 验证点 | 数据准备 |
|---|------|--------|----------|
| T16 | `test_events_scope` | 仅返回该 issue 的事件；按 seq 升序 | 混合 issue |
| T17 | `test_events_empty` | 无事件 → `{events:[], total:0}` | 有 issue 无 events |
| T18 | `test_events_issue_not_found` | issue 无任何记录 → 404 | — |

### 5.5 Issue pipeline（GET /api/issues/{owner}/{repo}/{number}/pipeline）

| # | 用例 | 验证点 | 数据准备 |
|---|------|--------|----------|
| T19 | `test_pipeline_from_archive` | 返回 event.json 链（summary/status/segments[]） | 已归档 event.json |
| T20 | `test_pipeline_missing_archive` | 无 event.json → 降级：由 events 表重构成 segments 或返回空 pipeline | 仅 events 无文件 |
| T21 | `test_pipeline_segment_fields` | 每段含 seq/agent/status/conclusion/next_step（output） | 结构化 output 归档 |

### 5.6 兼容与回归

| # | 用例 | 验证点 |
|---|------|--------|
| T22 | `test_tasks_create_alias_unchanged` | `POST /api/tasks/create` 行为不变（兼容别名） |
| T23 | `test_events_api_unchanged` | `/api/events` 平铺接口不回归 |
| T24 | `test_tasks_api_unchanged` | `/api/tasks` 既有接口不回归（complete/cancel/confirm-done） |
| T25 | `test_openapi_contract_exported` | `issues` 操作出现在 `openapi.json`（`gitrepo` 或 `/api/issues` 路径） |

### 5.7 必跑命令

```bash
python -m pytest tests/test_issues_api.py -v
python -m pytest tests/test_api_routes.py tests/test_auth_routes.py -q     # 回归
python -m pytest tests/test_help_directory.py -q                          # 文档契约
```

---

## 六、影响评估

| 项 | 说明 |
|----|------|
| 视角 | 从「按事件平铺」升级为「按 Issue 看 pipeline」，补全对外整体视图 |
| 概念 | 文档澄清 Issue/Event/Task，消除 task_id 混用歧义 |
| 工作量 | 中偏小：复用 `get_events_grouped_by_issue` / `get_issue_stage` / `load_event_json` |
| 兼容性 | 新增端点，`/api/events`、`/api/tasks` 不动 |

## 七、关键代码位置

| 位置 | 说明 |
|------|------|
| `src/api/routes/events.py` | 现有事件接口（平铺；不聚合 issue） |
| `src/api/event_store.py:505` | `get_events_grouped_by_issue`（新接口聚合基础） |
| `src/api/session_store.py:42` | `get_issue_stage`（stage/owner/a2a_seq/scene） |
| `src/api/event_archive.py:249` | `load_event_json`（pipeline 段） |
| `src/api/event_store.py:531` | `get_issue_summary`（摘要） |
| `src/api/main.py` | 路由挂载 |
| `help/api/README.md` | 目录（加第 11 项） |
| `tests/test_help_directory.py` | `EXPECTED_API_FILES` |