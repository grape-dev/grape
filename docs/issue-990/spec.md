# Spec: Issue #990 — 会话 token 运行时进程与 shell 环境快照不一致导致 grape 全量 401

> 来源：Issue #990 Spec（issue agent 发布，评级 ready）| 目标分支：weekly-mys

## Objective
修复 agent 运行时进程注入的 `GRAPE_SESSION_TOKEN` 与 shell 环境快照（`agent_tasks.env_extra`）
不一致，导致 Agent 用快照中的旧 token 调用 grape CLI 全量 401 并误判凭据失效的问题。

成功标准：token 轮换后快照与运行时注入值一致；偶发 401 时 Agent 可按 CLI 提示自恢复。

## Scope
### In Scope
- 事件投递 / 会话 token 轮换时，同步刷新 shell 环境快照（`agent_tasks.env_extra`）中的
  `GRAPE_SESSION_TOKEN`。
- 执行前 env 刷新（`GET /api/runtime/tasks/{task_id}/env`）返回并持久化同一 token。
- 本地 direct 执行不再盲目新签 token，改为复用快照中的有效 token；快照 token 失效时轮换并回写。
- 服务端 401（`SESSION_TOKEN_INVALID`）消息附带「从 agent 运行时进程环境重新读取
  `GRAPE_SESSION_TOKEN` 后重试」的恢复指引，grape CLI 直接透出该消息。

### Out of Scope
- 不调整 token 的签发规则、有效期与鉴权模型。
- 不改变 Agent 业务流程编排与其他非认证类 CLI 行为。
- 不修改 grape-cli 仓库源码（CLI 已透传服务端 `detail.message`）。

## Technical Approach
### 方案概述
以 `agent_tasks.env_extra` 作为 shell 环境快照的唯一权威：token 有效时复用，失效/轮换时
先作废旧 token 再签发新 token，并在同一处回写快照，确保运行时进程 env 与快照读到的值一致。

### 实现路径
1. `src/api/auth_token.py`
   - 新增 `rotate_session_token()`：先过期同一 `session_key` 的旧 token，再签发新 token。
   - 新增 `SESSION_TOKEN_RECOVERY_HINT`：401 恢复指引文案。
2. `src/runtime/service.py`
   - 新增 `sync_task_session_token(task_id)`：读取任务快照，校验 token（session_key /
     repo_key / agent_type 匹配且未过期）；有效则原样返回，否则轮换并回写 `env_extra`。
3. `src/api/routes/runtime.py`
   - `runtime_task_env` 在原有 AK/SK 实时刷新之外，返回 `sync_task_session_token` 的结果，
     使 worker 执行时的运行时 env 与快照一致。
4. `src/runtime/direct_executor.py`
   - direct 执行优先复用 `sync_task_session_token(task_id)`；无任务行（`d_<seq>`）时才新签。
5. `src/api/deps.py`
   - `SESSION_TOKEN_INVALID` 的 401 message 追加恢复指引（Web 端按 error_code 映射，不受影响）。

## Affected Modules
| 模块 | 改动类型 | 说明 |
|------|---------|------|
| `src/api/auth_token.py` | 修改 | token 轮换 + 401 恢复文案常量 |
| `src/runtime/service.py` | 修改 | 快照 token 同步/轮换 |
| `src/api/routes/runtime.py` | 修改 | 执行前 env 返回同步后的 token |
| `src/runtime/direct_executor.py` | 修改 | 复用快照 token，消除双签发漂移 |
| `src/api/deps.py` | 修改 | 401 恢复指引 |
| `tests/test_issue_990_session_token_snapshot.py` | 新增 | 轮换一致性 + 401 恢复路径 |

## Acceptance Criteria
- [ ] 构造 token 轮换并发起事件，shell 环境快照中的 `GRAPE_SESSION_TOKEN` 与运行时进程注入值一致。
- [ ] 使用快照 token 的场景不再出现全量 401；或 401 时 CLI 输出包含「从运行时进程环境重新读取 token」的明确指引。
- [ ] Agent 遭遇偶发 401 时可按提示自恢复，不误判凭据失效并终止任务。
- [ ] 所有现有测试通过，新增测试覆盖 token 轮换与 401 恢复。

## Risks & Mitigations
| Risk | Impact | Mitigation |
|------|--------|------------|
| 并发刷新导致 token 互相作废 | Med | 仅在快照 token 失效时轮换；轮换与快照回写在同一锁内完成 |
| 401 提示误伤真实凭据失效 | Low | 保留原始错误信息，叠加恢复建议而非替换；Web 端仍按 error_code 展示 |
| 旧 token 残留 | Low | 轮换时先过期同 session_key 的有效 token |