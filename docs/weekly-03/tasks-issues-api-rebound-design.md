# tasks → issues 接口全量归位方案（A 列表 + B 操作类，无兼容直接迁移）

> 分支：`weekly-03-lyz`；状态：**A 类已实施，B 类方案（待实施）**；日期：2026-09-16
> 关联：`issues-api-design.md`（§2.1.2 已有 Issue/Event/Task 归属对照）；
> 决策：**`/api/tasks` 下实际操作 `issue` 表的端点全部归位 `/api/issues`**；
> **不保留旧 `/api/tasks/*` 兼容**（当前用户量很小，直接迁移，前端/CLI 同步改）。
>
> 📌 **术语**：产品/前端已统一为「**需求中心**」（`pages/issues.js`，Account ▸ 需求中心），
> 下文「任务（task）」指 API 层的 `issue` 卡片行，「需求中心」指产品层页面。

---

## 一、关键事实：`task` 就是 `issue`，`task_id` 实为 `issue.id`

- `issue` 表是**权威卡片**：`id / user_id / repo_key / issue_number / title / status / stage / assignee / conversation_id / scene_id / parent_issue_id ...`
- `agent_tasks` 表是**每次执行实例**：`id / repo_key / issue_id / event_seq / status / conversation_id ...`
- **「需求中心」里用户看到的一条需求（task） = 一行 `issue` 记录**；`task_id = issue.id`（数值）。
- `agent_tasks.id`（字符串/会话 token）是**另一套 id**，仅在 `tasks/complete` 中出现。

> ⚠️ **命名论证（用户确认）**：B 类端点路径参数叫 `task_id`，实际全部用作 `issue.id`
> 查 `issue` 表（`tasks.py` L573/623/648/705/1349/1376/1398/1422/1449/1537/1722，11 处无一例外）；
> 唯一直查 `agent_tasks` 的是 `complete`（L1495）。**这构成双重误导**：
> 1. `task_id` 是 `issue.id`——放在 `/api/tasks` 下名不副实；
> 2. 与 `agent_tasks.id`（`complete` 用）共用 `/api/tasks/{id}` 前缀，同前缀下 id 体系分裂，
>    CLI 契约 `{task_id}` 无区分，极易混用。

---

## 二、tasks 端点归属分析（19 端点 → 三档）

| 端点 | 数据源 | 实际操作对象 | 归属 |
|------|--------|--------------|:---:|
| `GET /mine` | `FROM issue WHERE user_id=?` | 个人 Issue 列表 | **Issue · A 列表** ✅ |
| `GET /repo/{owner}/{repo}` | `FROM issue WHERE repo_key=?` | 仓库 Issue 列表 | **Issue · A 列表** ✅ |
| `POST /{task_id}/start` | `FROM issue WHERE id=?` | 打开 Issue 会话 | **Issue · B 操作** |
| `GET/PUT /{task_id}/status` | `FROM issue WHERE id=?` | Issue 状态轮询/迁移 | **Issue · B 操作** |
| `POST /{task_id}/confirm-done` | `UPDATE issue SET status=completed` | 开发者确认 Issue 完结 | **Issue · B 操作** |
| `POST /{task_id}/cancel` | `UPDATE issue SET status=cancelled` | 取消 Issue/停流水线 | **Issue · B 操作** |
| `POST /{task_id}/resume` | `UPDATE issue SET status=active` | 恢复 Issue | **Issue · B 操作** |
| `GET /{task_id}/pipeline` | `issue` + pipeline_config + events | pipeline 路线图 | **Issue · B 操作** |
| `GET /{task_id}/clarifications` | `issue` → agent_tasks → messages | 澄清消息 | **Issue · B 操作** |
| `POST /{task_id}/clarify` | 发 GitCode Issue 评论 | 开发者回复 Issue | **Issue · B 操作** |
| `upload/list/delete/{task_id}/attachments` | `issue` → 附件目录 | Issue 附件 | **Issue · B 操作** |
| `POST /complete` | `FROM agent_tasks WHERE id=session_key` | 某次执行实例完成 | **Event 执行** ❗ |
| `POST /notify` | bot-token 守卫，建 issue 卡片 | Agent 分配/换人通知 | **Agent 动作** |
| `POST /close` | bot-token 守卫，收尾 active | Agent 关闭回调 | **Agent 动作** |

**三档归属**：
- **A 列表（已迁）**：`mine`、`repo/{owner}/{repo}` → `/api/issues/mine`、`/api/issues/repo/{owner}/{repo}`
- **B 操作（本方案迁）**：`start/status/confirm-done/cancel/resume/pipeline/clarifications/clarify/attachments`（11 端点）
  → `/api/issues/{issue_id}/...`
- **C 保留 `/api/tasks`**：`complete`（Event 执行，`agent_tasks.id`）与 `notify`/`close`（Agent 动作，bot-token）

> 迁移后 `/api/tasks` 前缀专指「执行实例 + agent 动作」（`complete/notify/close`），
> `/api/issues` 指「需求（Issue）实体」；`task_id` 与 `agent_tasks.id` 不再混。✔️

---

## 三、方案：B 操作类迁到 `/api/issues`

### 3.1 端点映射（保留 `{issue_id}` = `issue.id` 单参定位，零 SQL 改动）

| 现状 `/api/tasks/...` | 迁移后 `/api/issues/...` | 方法 | scope |
|----------------------|--------------------------|:---:|:---:|
| `/{task_id}/start` | `/{issue_id}/start` | POST | dual |
| `/{task_id}/status` | `/{issue_id}/status` | GET | dual |
| `/{task_id}/status` | `/{issue_id}/status` | PUT | dual |
| `/{task_id}/confirm-done` | `/{issue_id}/confirm-done` | POST | user_only |
| `/{task_id}/cancel` | `/{issue_id}/cancel` | POST | user_only |
| `/{task_id}/resume` | `/{issue_id}/resume` | POST | user_only |
| `/{task_id}/pipeline` | `/{issue_id}/pipeline` | GET | user_only |
| `/{task_id}/clarifications` | `/{issue_id}/clarifications` | GET | user_only |
| `/{task_id}/clarify` | `/{issue_id}/clarify` | POST | user_only |
| `/{task_id}/attachments` | `/{issue_id}/attachments` | POST/GET | dual |
| `attachments/pre-upload` | `attachments/pre-upload`（Query repo_key） | POST | Web-only |
| `attachments/{filename}` | `/{issue_id}/attachments/{filename}` | DELETE | Web-only |

- **定位键不变**：全是 `WHERE issue.id=?`，`{issue_id}` 即原 `{task_id}` → **SQL 与函数逻辑零改动**，仅迁移路由装饰器 + 入参改名（`task_id`→`issue_id`）。
- operation_id 重命名：`tasks_start`→`issues_start`、`tasks_status`→`issues_status`、`tasks_status_get`→`issues_status_get`、
  `confirm_task_done_...`→`issues_confirm_done`、`cancel_task_api_...`→`issues_cancel`、`resume_task_api_...`→`issues_resume`、
  `get_task_pipeline_...`→`issues_pipeline`、`get_task_clarifications_...`→`issues_clarifications`、
  `reply_to_clarification_...`→`issues_clarify`、`tasks_list_attachments`→`issues_list_attachments` 等。
  scope 保持原样（user_only/dual 各自不变）。
- **路径冲突检查**：`/api/issues/{issue_id}` 为**单数值段**，与 `mine`（字面量）/`repo`（含子路径族）/
  `{owner}/{repo}`（两段）/`{owner}/{repo}/{number}`（三段）均可区分；`{issue_id}/...` 子路径与
  `repo/{owner}/{repo}`（repo 起始）不冲突。

### 3.2 A 类已实施（本轮已合入代码）

`GET /api/issues/mine`（`issues_mine`，dual）+ `GET /api/issues/repo/{owner}/{repo}`（`issues_repo_all`，dual）
已完成（`src/api/routes/issues.py`，复用 `tasks._query_tasks` / `_require_repo_access` / `_build_assignee_filter`）。

### 3.3 迁移策略：无兼容，直接移除旧端点

- **B 类迁移后，从 `tasks.py` 删除旧的 `/api/tasks/{task_id}/...` 路由**（不再保留别名）。
- `complete`/`notify`/`close` 保留在 `/api/tasks`（C 类），`/api/tasks` 前缀语义收窄。
- 前端 `issues.js`：B 类 6 处前缀 `/tasks/` → `/issues/`（L303 pipeline / L862 attachments / L903 upload /
  L918 delete / L1049 confirm-done / L1063 cancel / L1071 resume / L1206 status）。
- CLI（grape-cli）：契约注册表 operation_id 同步；`grape tasks wait` 改用 `GET /api/issues/{id}/status`。

### 3.4 契约与测试（L1/L2/L3 强制）

- `test_openapi.py`：`EXPECTED_PUBLIC` 移除 `tasks_status/status_get/start` 等 B 类 operation_id，
  **新增** `issues_start/issues_status/issues_status_get/issues_confirm_done/issues_cancel/issues_resume/
  issues_pipeline/issues_clarifications/issues_clarify/issues_list_attachments`（`/api/issues/{issue_id}/...`）等；
  scope 抽查：`confirm-done/cancel/resume/pipeline/clarify`=user_only、`status/start`=dual、`mine/repo`=dual。
- `EXPECTED_SCOPE`：B 类与原 tasks 一致（user_only/dual 各归各位）。

### 3.5 文档

- `11-issues.md`：新增「单 Issue 操作」小节（start/status/confirm-done/cancel/resume/pipeline/clarify/attachments）。
- `README.md`「各文档 scope 一览」：issues 行补 B 操作；tasks 行收窄为 complete/notify/close。
- `01-tasks.md`：**重写为只剩** `complete` / `notify` / `close`（C 类），标注 B 类已迁移。

---

## 四、影响评估

| 项 | 说明 |
|----|------|
| 兼容 | **不保留旧 `/api/tasks/{task_id}` 路由**（用户量小，直接迁移；前端/CLI 同步改） |
| 数据 | 定位键不变（`issue.id`），SQL/函数逻辑零改动；仅迁路由 + 入参改名 |
| 契约 | 移除 11 个 `tasks_*` operation_id，新增 11 个 `issues_*`；scope 保持 |
| 前端 | `issues.js` B 类 8 处 URL 前缀 `/tasks/`→`/issues/`（其余走 issues 不变） |
| CLI | `grape-cli` 契约注册表 operation_id 同步；`tasks wait` 指向 `/issues/{id}/status` |
| 语义 | `/api/tasks` 收窄为「执行实例 + agent 动作」，`/api/issues` = 需求实体；消除 id 双关 |

## 五、验收

1. B 类迁移后：`POST /api/issues/{issue_id}/confirm-done` 等 200，行为与旧 `tasks/{id}/confirm-done` 一致。
2. scope 回归：confirm-done/cancel/resume/pipeline/clarify → session 403、api_key/jwt 200；status/start → 三通道 200。
3. `/api/tasks/{task_id}/...` 旧路径 **404**（已移除，无兼容）。
4. 契约 `test_openapi` 全绿；`/api/tasks` 仅剩 `complete/notify/close`。
5. 前端需求中心操作回归：确认完成/取消/恢复/状态/附件/流水线全部正常。

---

## 附：scope 修正（2026-09-16 评审）

> 评审意见：`status` 状态变更是需求中心用户视角状态（`awaiting_input/completed/cancelled` 可用户直接设置，
> 高优先级覆盖），放行 `tk_` 会话 token（Agent 执行期）任意改动会与 pipeline 驱动 `stage` 脱钩，制造冲突/不一致。
> 修正后（代码已改，`issues.py` 认证守卫变更，openapi 重新派生）：

| 端点 | 修正前 | 修正后 |
|------|:---:|:---:|
| `POST /api/issues/{issue_id}/start`（打开会话，纯导航预检，不翻转状态） | dual | **user_only** |
| `GET  /api/issues/{issue_id}/status`（轮询 wait，读） | dual | dual（保持） |
| `PUT  /api/issues/{issue_id}/status`（状态迁移，写） | dual | **user_only** |

`./scripts/regenerate_openapi.py` 重生成契约；`test_openapi` 增加 user_only 抽样 + session→403 运行时断言。