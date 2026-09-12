# tasks complete（任务自报完成）接口处理流程分析

> 接口：`POST /api/tasks/complete`（operation_id=`tasks_complete`，grape CLI `tasks complete`）
> 位置：`src/api/routes/tasks.py:1153`（`self_complete_task`）→ `src/runtime/service.py:1468`（`complete_task`）
> 目的：Agent 动作收尾后显式自报完成，落终态（不经过 evaluate_agent_output 启发）
> 日期：2026-09-11

---

## 一、入口层（`src/api/routes/tasks.py` `self_complete_task`）

### 1.1 任务定位（3 通道认证分流）

| 认证类型 | task_id 来源 | body.task_id 是否必须 |
|----------|-------------|----------------------|
| 会话 token（`tk_`） | `user.session_key`（M2 签发时以 task_id 作为 session_key） | 可省 |
| JWT / API Key（用户级） | `body.task_id` | **必填**（否则 400） |
| 两者皆无 | — | 400「缺少 task_id」 |

校验：查 `agent_tasks` 无此任务 → **404**。

### 1.2 作用域校验

- **会话 token**：`user.session_key == task_id` **且** `user.repo_key == task.repo_key` → 否则 **403**（作用域不匹配）
- **JWT/API Key**：任务 `repo_key` 必须在该用户 `managed_repos` 集合内 → 否则 **403**（非本人托管仓库）

### 1.3 幂等

任务已终态（`completed` / `failed` / `cancelled`）→ 直接返回 `{ok, task_id, status, already: true}`，**不覆盖**。

### 1.4 结果文本组装

- **无结构化字段** → 用 `result_text` 原文；兜底「任务自报完成[ / next_step]」
- **有结构化字段**（`actions` / `conclusion` / `artifacts` / `issue_summary`）→ JSON 序列化为 `result_text`，
  供下游 `parse_agent_output_json` 识别（结构化 `actions+conclusion` 是后续"完成与否"的强信号）

→ 调 `complete_task(task_id, result_text, 0)`

## 二、核心落库（`src/runtime/service.py` `complete_task`，L1468）

| 步骤 | 逻辑 | 说明 |
|------|------|------|
| 1. 幂等保护 | 任务已 `failed/cancelled` | 有完整结构化输出(`actions+conclusion`) → 覆盖为 completed；否则跳过 |
| 2. 结构化解析 | `parse_agent_output_json(result_text)` | 提取 `output`（actions/conclusion/...） |
| 3. exit_code 修复 | 结构化完整但 `exit_code!=0`（如 hermes SIGTERM → proc.returncode=-15） | **强制归 0** |
| 4. 业务失败检测 | `exit_code==0` 但 `_detect_business_failure(agent_type, output, result)` 命中 | **覆盖 exit_code=1**（进程成功≠业务成功） |
| 5. 定状态 | `completed`(0) / `failed`(非0) | — |
| 6. `finalize_event_archive` | 归档事件（status/exit_code/duration/output/tokens/issue_num_hint） | 事件持久化 |
| 7. `update_event_complete` | 更新事件：status/exit_code/duration/completed_at/result | 事件终态 |
| 8. **pending_review（Issue #51）** | 完成的 agent 是场景 pipeline **最后节点（next=done）** 且 `current_stage==agent` | `set_issue_stage("pending_review", agent)` 等开发者审核 |
| 9. `update_task_status` | 任务置终态 + 写 result_text / usage_json | agent_tasks 终态 |
| 10. 会话续接指针 | 回写 issue 级执行会话 runtime_session_id | 供后续会话续接 |

## 三、关键设计点

1. **结构化输出是"完成与否"的强信号**：即使任务曾被标 failed，只要 `result_text` 含 `actions+conclusion`
   就覆盖为 completed——防 hermes ACP 进程被 SIGTERM 清理后（returncode=-15）误判失败。
2. **`_detect_business_failure` 双保险**：进程退出码 0 ≠ 业务成功；agent 输出含失败特征时覆写 exit_code=1。
3. **不经过 `evaluate_agent_output`**：自报完成是"agent 明确收尾"，直接落终态，不走启发式评估
   （区别于 agent 结束后端对输出做启发评估的路径）。
4. **Last-node → pending_review 联动**：最后 agent 完成后置 `pending_review`，与 Issue #51
   state machine 联动，等开发者审核后再进入 closer。
5. **会话续接**：完成时刷新 issue 级执行会话的 runtime_session_id，后续 agent 可基于该会话续接。

## 四、错误矩阵

| 状态 | 场景 |
|------|------|
| 200 | 完成成功 / 终态幂等（`already: true`） |
| 400 | 缺 task_id（非会话 token 且 body 未传） |
| 403 | 作用域不匹配 / 非本人托管仓库 |
| 404 | 任务不存在 |
| 401 | 未认证 |

## 五、关键代码位置

| 位置 | 说明 |
|------|------|
| `src/api/routes/tasks.py:1153` | `self_complete_task`（认证分流/校验/幂等/组装 → 调 complete_task） |
| `src/api/routes/tasks.py:1142` | `TaskSelfCompleteBody`（请求体模型） |
| `src/runtime/service.py:1468` | `complete_task`（解析/失败检测/finalize/update_event/pending_review） |
| `src/api/event_store.py` | `parse_agent_output_json` / `update_event_complete` |
| `src/api/event_archive.py` | `finalize_event_archive` |
| `src/api/session_store.py` | `set_issue_stage`（pending_review 状态机） |