# Issue-45 补充设计：任务自报完成（grape CLI `tasks complete`）——替代「无产出判失败」启发

> Issue：https://gitcode.com/grape-dev/grape/issues/45
> 类型：Feature（执行收口）| P1 | 状态：spec-ready（待评审）
> 分支：`weekly-02`
> 日期：2026-09-09
> 范围：新增对外「任务完成」端点，让 agent 在动作收尾后用 `grape` CLI 显式自报完成，
> 消除 `evaluate_agent_output`「no output, mark failed」启发式误判。

## 一、背景与问题

事件 #2114 重派后：agent 走完 11 步、`grape` 调 `/api/git-repo/*` 全部 200，但最终只输出纯文字总结（338 字），
未产出结构化 JSON 结果块 → 服务端 `evaluate_agent_output` 判定「无产出」→ `Exec agent: no output, mark failed`
（`src/api/event_pipeline.py:1995`），任务 `failed / exit 0 / 空 result`，且事件行未刷新 → UI 显示失败。

根因：**完成与否依赖对 final_text 的启发式判定**（最优匹配 `"conclusion"` 非空的末尾 JSON 块），
agent 的主观"已完成"无法显式传达给服务端。

## 二、目标

给 agent 一条**显式、可审计、防他写**的成功通道：动作收尾后调用
`grape tasks complete [--task-id x] --json '{result...}'` 自报完成；
服务端据此把任务 + 事件置为成功，**不再依赖启发式**。启发式仅作「未自报」的失败兜底。

## 三、方案

### 3.1 服务端新公开端点

```
POST /api/tasks/complete                operation_id="tasks_complete"（collection 形态，无 path task_id）
Body(JSON): { "task_id": str = "", "result_text": str, "next_step": str = "" }
```

- **认证**：`Depends(get_current_user_or_api_key)`（对外安全路由，进 OpenAPI 契约 → CLI 自动获得命令）。
- **任务定位**：优先 `task_id` 取**会话 token 派生**——`.avu` devops/chat 的会话 token 以 `task_id` 为 `session_key`
  （M2 `enqueue_task`）签发，故服务端 `verify_session_token` 的 `session_key` 即任务 id；
  JWT/API Key 则必须 body 带 `task_id`。
- **作用域鉴权（防他写）**：
  - 会话 token：`session_key == 目标 task_id` 且 `repo_key` 与任务的 `repo_key` 一致 → 只有该任务自己的 token 能完成它。
  - JWT / API Key（用户级）：目标任务的 `repo_key` 必须在该用户的托管仓库（`get_managed_repos(user_id)`）内。
- **幂等**：任务已终态（completed/failed/cancelled）时**不再覆盖**——返回当前状态（200 + `already:true`）。
- **行为**：`complete_task(..., exit_code=0, result_text)` 成功语义：task=completed、result/next_step 写回、
  event 刷为完成；不经过 `evaluate_agent_output`。
- **与 worker/direct 现有路径共存**：worker 侧仍走 `POST /api/runtime/tasks/{id}/complete`（rta_ 鉴权）；
  新端点只是 agent 侧显式完成入口，二者最终都收敛到 `complete_task`。

### 3.2 CLI 用法（契约驱动，零 CLI 代码变更）

端点进契约后自动生成命令：

```bash
# 会话 token 自动定位任务（不传 task_id）
grape tasks complete --json '{"result_text":"已完成审视并回复评论。","next_step":"reply_comment"}'

# JWT / API Key（用户级）显式指定任务
grape tasks complete --json '{"task_id":"t_ea4267aa7f09","result_text":"...","next_step":"reply_comment"}'
```

- token/base_url 取自注入 env（`GRAPE_SESSION_TOKEN` / `GRAPE_SERVER_URL`），无需配置。
- 端点声明为公开 → `grape tasks complete` 自动出现，零 CLI 改动。

### 3.3 技能收尾步骤（vod 试点）

在「输出最终 JSON 块」之后（或之前）追加一步：

```bash
grape tasks complete --json '{"result_text":"已完成 Issue #2 审视与评论，无需交办。","next_step":"reply_comment"}'
```

- 即使任务已 net 无动作（"该 Issue 已处理"），也调用一次以显式收尾成功。
- JSON 结果块（人读总结，含 `conclusion`）仍保留——它是失败兜底的判定材料，同时也是 prompt 要求的
  `recommended_scene`/`scene_reason` 载体。

## 四、防滥用与安全

| 威胁 | 缓解 |
|------|------|
| 他任务/他 agent 补写完成 | 会话 token 作用域校验：`session_key==task_id` 且 `repo_key` 匹配 |
| 未执行完就自报完成 | agent 本身即任务执行者；服务端仅校验作用域，业务正确性由 agent 负责；`next_step` 可审计 |
| API Key（`grape_`，用户级，无 task 绑定）调用 | 允许？→ 校验 body `task_id` 与调用者的托管仓库（用户级 Key 绑 user，可校验该任务属该用户的托管 repo）；否则拒绝写未知任务 |
| 重复完成 | 终态幂等（不覆盖，返回现状） |
| 会话 token 泄露后冒名完成 | 短生命周期（任务结束即失效）+ 作用域校验兜底 |

> 决策点：`grape_` 用户级 Key 是否允许完成任意该用户任务？建议**允许**（绑定用户维度，
> 用 user_id 关联托管 repo 校验），便于人工/三方核对收尾；若收紧，可仅限会话 token。

## 五、改动清单

| # | 模块 | 改动 | 优先级 |
|---|------|------|--------|
| 1 | `src/api/routes/tasks.py` | 新增 `POST /{task_id}/complete`（`tasks_complete`，对外；会话 token 作用域校验 + 终态幂等 → `complete_task`） | P0 |
| 2 | `src/api/auth_token.py` | （可选）`verify_session_token_for_task(token, task_id, repo_key)` 助手 | P0 |
| 3 | `tests/test_openapi.py` | EXPECTED_PUBLIC 增加 `tasks_complete` | P1 |
| 4 | `tests/`（如 `test_agent_internal_auth.py`/新增） | 作用域匹配/不匹配/幂等/401 用例 | P1 |
| 5 | `skills/vod/vod/SKILL.md`（试点） | 收尾步骤：`grape tasks complete --json '{...}'` | P1 |
| 6 | 本文档同步 `issue-45-openapi-api-design.md` 契约端点表 | 记录新端点 | P2 |

> 依赖：会话 token 以 task_id 为 session_key 已实现（M2 `enqueue_task`）；`complete_task` 已存在（worker 完成路径复用）。

## 六、测试用例设计

### 6.1 鉴权与作用域（`tests/test_task_complete.py`）

| # | 用例 | 输入 | 断言 |
|---|------|------|------|
| T1 | 会话 token 自报完成（不传 task_id，token 自动定位） | Bearer tk_（session_key=该任务）+ `{result_text}` | 200；task=completed、exit=0、result 写回；event=completed |
| T2 | 会话 token 只能定位自己的任务（他任务 token） | Bearer 其他会话的 tk_（session_key≠目标） | task_id 由 token 派生 → 查无此任务 → 404（天然防他写，目标任务不受影响） |
| T3 | repo 不匹配（token 属于其他仓库任务） | Bearer 他仓库 token | 403 |
| T4 | JWT/API Key 完成**自己托管仓库**的任务 | Bearer JWT/grape_ + body `task_id` | 200，完成后该任务 completed |
| T5 | JWT/API Key 完成**非本人托管仓库**任务 | Bearer + body `task_id`（他人仓库） | 403 |
| T6 | 终态幂等 | 任务已 completed/failed/cancelled 再 complete | 200 返回现状（`already:true`），状态不回改 |
| T7 | 未认证 | 无 token | 401 |

### 6.2 契约与回归

| # | 用例 | 断言 |
|---|------|------|
| T8 | `test_openapi.py` EXPECETD_PUBLIC | 契约含 `tasks_complete`（`POST /api/tasks/complete`，带 security） |
| T9 | worker `runtime complete` 回归 | 原 `complete_task`/`POST /runtime/tasks/{id}/complete` 不受影响 |
| T10 | 启发兜底回归（未自报） | 无 `grape tasks complete` 时，`evaluate_agent_output` 逻辑不变（仍"no output failed"） |
| T11 | 自报后 executor 不误判 | agent 先 `tasks complete` 再结束（无 JSON 输出块）→ 事件成功（防 `no output, mark failed` 覆盖） |

> 用例覆盖：作用域三通道、幂等、契约、回归、防误判。

## 七、验收标准

- [ ] `grape tasks complete --json '{"result_text":"..."}'`（不带 --task-id，靠会话 token 定位）可将当前任务置为 **completed**，事件刷为成功 + result 写回
- [ ] 其他 agent 的会话 token 完成该任务 → 403（作用域不匹配）
- [ ] 已终态任务再次 complete → 200 幂等返回现状，不改写
- [ ] worker 现有 `runtime complete` 路径不受影响（回归通过）
- [ ] `test_openapi.py` 契约含 `tasks_complete`；作用域/幂等用例全绿
- [ ] vod 试点：重派 #2114 收尾调用 complete 后状态为成功

## 八、风险与顺序

| 风险 | 影响 | 缓解 |
|------|------|------|
| agent 忘记调用 complete | 仍失败 | 保留 `evaluate_agent_output` 兜底（缺省路径不变） |
| 自报与真实结果不一致 | 低 | `next_step`/result 可审计；事件对话可见 |
| 端点被误用于"假成功" | 中 | 作用域校验 + 终态幂等；如需可再加「仅 running/dispatched 可 complete」约束 |

**实现顺序**：① 端点 + 作用域校验 + 幂等 → ② 契约/测试 → ③ 技能收尾 + 部署 → ④ VOD 试点重派验证。

## 附：关键代码位置

- 完成语义：`src/runtime/service.py` `complete_task` / `update_task_status`（终态幂等 L1047）
- 事件写回：`src/api/event_store.py` `update_event_complete`
- 会话 token：`src/api/auth_token.py` `verify_session_token`（M2，session_key=task_id）
- 启发判定：`src/api/event_store.py` `evaluate_agent_output`；`src/api/event_pipeline.py:1993-2004`「no output, mark failed」