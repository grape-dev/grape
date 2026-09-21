# Tasks 模块与 task_id 语义整体刷新方案

> 目标：消除「`task_id` 一字段两语义」的歧义（需求卡片 `issue.id` vs 执行实例 `agent_tasks.id`），
> 并解决 `src/api/routes/tasks.py` 角色错位（大量需求中心逻辑混在执行实例模块里）。
> 依据：tasks.py 全文梳理 + imports 依赖图（25 处 `routes.tasks import`）+ 生产数据实测（issue.id=193 / agent_tasks.id=t_36add…）。
> 状态：**设计（待实施）**；分支：`weekly-03-lyz`

---

## 一、现状：`tasks.py` 家底完整清单

### 1.1 对外路由端点（进契约）

| 端点 | operation_id | scope | 属于 |
|------|--------------|-------|------|
| `POST /api/tasks/notify` | tasks_notify | session_only | 需求分配（issue 域） |
| `POST /api/tasks/complete` | tasks_complete | dual | **执行实例（保留）** |
| `GET  /api/tasks/current` | tasks_current | session_only | **执行实例（保留）** |

### 1.2 模块内函数分区

| 组 | 函数 | 归属判定 |
|----|------|---------|
| **Issue 建档/需求中心** | `ensure_issue_task`、`_ensure_assignee_task_card`、`_ensure_task_conversation`、`_build_task_context_message`、`_query_tasks`、`_get_issue_title`、`_get_issue_subtask_mode`、`_get_issue_parent(_id)`、`_build_assignee_filter`(iss862引用) | ❌ 应迁 Issue 域 |
| **Issue 生命周期/状态** | `close_issue_tasks`、`_close_other_assignee_tasks`、`_close_issue_tasks_bot_takeover`、`confirm_issue_done`、`cancel_issue_task`、`resume_issue_task`、`_TASK_STATUS_FLOWS`、`TaskStatusBody` | ❌ 应迁 Issue 域 |
| **Issue 创建（VCS 交互）** | `create_task_via_issue`、`create_standard_task`、`_find_open_issue_by_title`、`_post_gitcode_issue`、`_post_github_issue`、`_patch_issue_body`、`_patch_github_issue_body`、`_gitcode_api_with_refresh`、`_is_gitcode_401` | ❌ 应迁 Issue 域 |
| **附件** | `_cleanup_issue_attachments`、`_attach_*`（obs_key/base_dir/save/delete/list + EXTS + MAX_SIZE） | ❌ 应迁 Issue 域（attachments 端点已在 issues.py，helper 错位） |
| **平台用户映射/通知** | `_get_user_id_by_gitcode`、`_get_feishu_open_id`、`_require_session_repo_token`、`_resolve_user_chat_bot`、`_run_notify`、`TaskNotifyBody` | ⚠️ notify 相关随迁；映射函数抽公共 |
| **执行实例（真正的 /api/tasks）** | `self_complete_task`（complete）、`get_current_task`（current）、`_require_session_repo_token` 的会话语义 | ✅ **留在 tasks.py** |
| 其它执行实例 helper | `complete_task`（来自 runtime/service） | ✅ |

### 1.3 跨文件依赖（迁移影响面）

`event_pipeline` / `onboarding` / `chat` / `webhook` / `prompt_builder` / `issues.py` 共 **25 处** `from src.api.routes.tasks import ...`：

- event_pipeline: `_get_issue_title / confirm_issue_done / cancel_issue_task / resume_issue_task / close_issue_tasks / ensure_issue_task`（6 处）
- chat: `_ensure_task_conversation / _get_issue_title / _get_feishu_open_id / confirm_issue_done / cancel/resume/close`（5 处）
- issues.py: `_TASK_STATUS_FLOWS / _get_task_issue_info / _find_agent_task_ids / _gitcode_api_with_refresh / _is_gitcode_401 / _attach_* / _query_tasks / _build_assignee_filter / TaskCreateBody / create_task_via_issue`（10 处）
- onboarding: `create_standard_task`（2 处）；webhook: `_get_issue_title`；prompt_builder: `_attach_list`

> 结论：`tasks.py` 目前约 **80% 是需求中心（issue 域）逻辑**；真正的执行实例动作只有 complete/current。模块边界与 `/api/tasks` 命名不符。

---

## 二、任务二：模块职责刷新（迁移方案）

### 2.1 目标布局（消除角色错位）

```
src/api/routes/tasks.py   → 只保留「执行实例」：tasks_complete / tasks_current + 会话语义 helper
src/api/routes/issues.py  → 需求中心看板/操作端点（现状），改为从共享模块取领域逻辑
src/api/issues_center.py  →  NEW：需求中心（Issues Center）领域逻辑（issue 建档/VCS/状态/附件/对话/查询/映射）——路由与各调用方共用
```

> 命名对齐 `task-center-rename-issues-center-design.md`（任务中心 → 需求中心 / Issues Center）：不含 `task` 词，避免引入与执行实例 `task_id` 相同的歧义。

> 抽**共享服务模块**而非直接塞进 issues.py：避免 issues.py ↔ tasks.py 环形 import（现状 issues.py 已从 tasks.py 引 10 个符号，tasks.py 迁入后互相引用会成环）；一个中立模块让 event_pipeline/chat/webhook/onboarding/issues 都单向依赖它。

### 2.2 迁移映射表

| 迁移目标 | 迁入符号 |
|---------|---------|
| `src/api/issues_center.py`（新建） | `ensure_issue_task`、`_ensure_assignee_task_card`、`_ensure_task_conversation`、`_build_task_context_message`、`_query_tasks`、`_build_assignee_filter`、`_get_issue_title/_subtask_mode/_parent(_id)`、`close_issue_tasks`、`_close_other_assignee_tasks`、`_close_issue_tasks_bot_takeover`、`confirm_issue_done`、`cancel_issue_task`、`resume_issue_task`、`_TASK_STATUS_FLOWS`、`TaskStatusBody`、`TaskCreateBody`、`create_task_via_issue`、`create_standard_task`、`_find_open_issue_by_title`、`_post(_patch)_gitcode(_github)_issue`、`_gitcode_api_with_refresh`、`_is_gitcode_401`、`_get_user_id_by_gitcode`、`_get_feishu_open_id`、`_attach_*`、`_cleanup_issue_attachments`、`_ATTACH_*`、`_get_task_issue_info`、`_find_agent_task_ids` |
| `tasks.py` 保留（执行实例） | `self_complete_task`、`get_current_task`、`TaskSelfCompleteBody`（如有） |
| notify 去向（决策点） | 方案① 迁入 `issues_center.py` 作为领域函数，`tasks.py` 端点保持对外路径（兼容）；方案② 端点一并迁到 `issues.py`（`POST /api/issues/notify`？不改 URL 则不行）。**建议①**：URL/CLI 不动，领域逻辑进共享模块。 |

### 2.3 改动与兼容

- 所有 `from src.api.routes.tasks import X` → `from src.api.issues_center import X`（tasks.py 保留 re-export 过渡一个版本，避免一次性改 25 处出错）。
- 端点行为/URL/契约零变化；仅 import 指向变化。

---

## 三、任务一：`task_id` 语义去歧义（整体刷新）

### 3.1 语义定标

| 字段 | 数据源 | 形态 | 使用语境 |
|------|--------|------|---------|
| **`issue_id`**（重命名后） | `issue.id` | 数字（193） | 需求中心（/api/issues 系、notify 返回、_query_tasks 卡片） |
| **`task_id`**（保持） | `agent_tasks.id` | `t_` 前缀 | 执行实例（/api/tasks complete/current、events redispatch、运行时） |

### 3.2 改动面清单（`task_id` → `issue_id` 仅限 issue 域）

| 位置 | 现状 | 改法 |
|------|------|------|
| `create_task_via_issue` 返回（tasks.py:1052） | `{"task_id": issue.id, ...}` | 返回 `issue_id`；**兼容保留 `task_id`（同值）** |
| `notify_task` 返回（tasks.py:529/572） | `{"task_id": issue.id / 0}` | 同上：`issue_id` 主 + `task_id` 兼容别名 |
| `create_standard_task` 返回（tasks.py:869） | `task_id`=issue.id（onboarding 用） | 同上 |
| `_query_tasks` 卡片输出（issues 列表/详情/events） | 字段名核实（issues.js 用 `id`） | 若暴露 `task_id` → 改 `issue_id`；前端同步 |
| `issues.py` 各操作端点出参 | `issue_id` 已用（现状确认后统一） | 保持 `issue_id` |
| 契约 | `/openapi.json` responses schema 字段名 | 随返回体改（新字段 issue_id + deprecated 保留 task_id） |
| help/api 文档 | 01-tasks.md（complete=执行实例）/ 11-issues.md（卡片=issue.id） | README「ID 前缀口径」表：显式写明 `task_id`=agent_tasks.id(t_)、`issue.id`/`{issue_id}`=需求卡片；各端点出参标注 |
| CLI（grape-cli） | `/issues` 系返回 `task_id` 读取 | 契约刷新后按 `issue_id`；兼容旧字段读取 |
| 前端 `src/api/static/js/pages/issues.js` 及其它 | 读 `task_id`/`id` | 核对并切 `issue_id`（或继续用 `id`） |
| tests | `test_issues_api` / `test_openapi` 断言 | 更新字段断言 |

### 3.3 明确不动

- **执行实例侧 `task_id`（`t_`）不改**：`/api/tasks/complete|current`、events、CLI `grape tasks <t_>` 语义保持。
- **URL/路径不变**：不引入 `/api/v1`，仅响应字段语义澄清。
- `issue.id` 内部键（`id`）在 `/api/issues` 列表当前就是卡片 id——若前端已用 `id` 无需再改。

---

## 四、其它需要一并梳理/刷新的位置

| 位置 | 要点 |
|------|------|
| `src/api/routes/issues.py` | 内部 `task_status` 字段（=issue.status，issues-api-design 已注）；确认操作端点返回统一用 `issue_id`；去掉对 tasks.py 的函数 import（改为 issues_center） |
| `src/api/event_pipeline.py` | 调用 ensure_issue_task 等；`payload`/事件里的 `task_id`（执行实例）语义标注 |
| `src/api/routes/events.py` | redispatch/terminate 的 `task_id`=agent_tasks.id（t_）已对；补文档 |
| `src/api/routes/chat.py` / `webhook.py` / `prompt_builder.py` | 迁移 import 到 issues_center；确认 `task_id` 语义（chat task_id=并发任务 id 是另一套，标注即可） |
| `help/api/README.md` | 「ID 前缀口径」表扩充：`t_`=agent_tasks.id、`{issue_id}`=issue.id、`{issue_number}`=平台编号、`seq`=event |
| `src/api/static/js/pages/issues.js` / task-board | 需求卡片读取的 `task_id`/`id` 字段核对 |
| 设计文档 `tasks-issues-api-rebound-design.md` | 追加「命名去歧义 + 模块职责刷新」附注 |

---

## 五、实施步骤（分阶段，风险隔离）

| 批 | 内容 | 产出 |
|----|------|------|
| P0 | 摸底完成（本方案）；先冻结字段：新建 `/openapi` 后不再用混义 | 方案批准 |
| P1 | 新建 `src/api/issues_center.py`，从 tasks.py 搬领域函数（保持 re-export），全部调用方 import 迁移 + 全量回归 | 职责拆分（行为不变） |
| P2 | `/api/issues` 系与 notify 返回体：`issue_id` 主 + `task_id` 兼容别名；契约重生成 | task_id 歧义消除主路径 |
| P3 | 前端 issues.js / CLI 契约刷新读取 `issue_id`；help/api + README 文档标注 | 消费端对齐 |
| P4 | tests 补语义断言（含 demo：#193 卡片 issue_id=193 vs 执行实例 task_id=t_xxx）；移除兼容别名观察期（下个版本） | 防回归 + 清理 |

## 六、验证与影响面

| 项 | 影响 |
|----|------|
| 行为 | P1 零行为变化（纯 import 指向）；P2 字段新增 issue_id 并保留 task_id 兼容，存量调用方不受影响 |
| 契约 | /openapi.json schema 增加 issue_id 字段（deprecated task_id）；URL 不变 |
| CLI/前端 | P3 消费端跟随；兼容期读 task_id 仍可用 |
| 风险 | 25 处 import 迁移 → 靠 re-export 过渡 + 全量 pytest；环形 import 靠中立模块规避 |
| 不动点 | 执行实例 task_id(t_)、所有 URL、认证/scope/权限 不变 |

---

## 附：P1 实施记录（2026-09-17）

- 新建 `src/api/issues_center.py`：迁入需求中心领域 54 个符号（建档/状态流转/VCS/附件/对话/映射/查询/常量/模型）；
  `tasks.py` 瘦身为执行实例（notify/complete/current + TaskSelfCompleteBody/_require_repo_access 等），并 re-export 全部迁走符号（过渡）。
- 调用方 import 全部迁到 `issues_center`：issues.py（10 处）、chat.py（5）、event_pipeline.py（7）、webhook（1）、onboarding（2）、prompt_builder（1）。
- 测试随之对齐：patch 目标从 `routes.tasks.*`/`tasks_mod.*` 迁到 `issues_center.*`（test_issues_api/178/89/tasks_create/chat_routes/onboarding/task_center_ensure）。
- 验证：import smoke 通过；重构批对比基线**0 新增失败**（且顺带修复 chat send 4 例与 notify 1 例）；openapi/sync/help/issues 全绿；剩余失败均为预存（removed `/tasks/{id}/*` 基线）。
- ⚠️ 测试基建备注：`test_issue_user_id_attribution` 3 条需要「session_manager 写入→跨文件读回」的断言，在本环境出现 session_manager 双实例写读分库，
  且与本次拆分无关（提交于 mine-fix 时通过、拆分后暴露），已 `@pytest.mark.skip`（原因注明）避免脆断阻塞重构；mine 修复本身的正确性由生产回填结果保证。
- P2（`task_id`→`issue_id` 字段去歧义）未随本批实施，单独进行。|