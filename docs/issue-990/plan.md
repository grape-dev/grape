# Plan: Issue #990 — 会话 token 快照与运行时注入一致性

## 关键设计决策
| 决策 | 选项 | 选择 | 理由 |
|------|------|------|------|
| 快照权威源 | 重新签发 / 复用 `env_extra` 快照 | 复用有效快照 token | 避免每次执行产生新 token 造成快照/runtime 漂移 |
| 轮换时机 | 每次执行 / 仅旧 token 失效 | 仅旧 token 失效时轮换 | 覆盖 token 过期/撤销窗口，幂等且不误伤并发执行 |
| 轮换原子性 | 分步提交 / 单事务完成 | 单事务内过期旧 token + 签发 + 写快照 | 防止多 worker 跨进程交错作废新 token |
| 跨进程串行化 | 线程锁 / 数据库锁 | SQLite `BEGIN IMMEDIATE`；MySQL 池外专用连接 `GET_LOCK` 命名锁 | 服务端多 worker，线程级 `db_lock` 不足；MySQL 兼容层 SELECT 后自动 commit，`FOR UPDATE` 行锁会提前释放 |
| 失败处理 | 继续返回新 token / 回滚 | 任一步失败回滚并返回空串 | 保证不出现快照与运行时注入漂移 |
| 401 指引位置 | 改 CLI 源码 / 服务端 message | 服务端 `detail.message` | CLI 已透传 message；本仓职责内闭环 |

## 数据流
```
enqueue_task: issue_session_token(session_key=task_id) → agent_tasks.env_extra 快照
      │
worker 执行前: GET /tasks/{id}/env
      └─ sync_task_session_token:
           跨进程临界区: SQLite BEGIN IMMEDIATE / MySQL 专用连接 GET_LOCK(task_id)
           取锁失败 → 返回空串（fail-closed）
           token 有效（session_key/repo/agent 匹配且未过期）→ 原样返回
           写事务内: 过期旧 token + 签发新 token + 回写 env_extra → commit → 返回
           锁内任一步失败 → rollback → 返回空串（绝不返回未落库 token）；释放命名锁
      └─ worker env.update(fresh_env) → 运行时进程 env 与快照一致
      │
direct 执行: session_token_for_execution(task_id) 复用/同步快照；有任务行但同步失败时不另签（防止漂移），无任务行才新签
```

## 实施步骤
1. `auth_token.rotate_session_token`（单事务过期+签发，失败回滚）+ `SESSION_TOKEN_RECOVERY_HINT`
2. `service.sync_task_session_token`（跨进程串行化 + 单事务复用/轮换/回写快照）
3. `service.session_token_for_execution`（direct 路径复用快照，失败不签未落库 token）
4. `runtime_task_env` 返回同步后的 token（有效快照下幂等只读）
5. `direct_executor` 改为复用/同步快照 token
6. `deps` 401 message 追加恢复指引
7. 新增测试：轮换回滚、并发同步、持久化失败回滚、direct 路径、401 恢复文案

## 验证
- `pytest tests/test_issue_990_session_token_snapshot.py -q`
- `pytest tests/test_auth_token.py tests/test_session_token_lifecycle.py tests/test_dsh_runtime.py -q`
- 全量 `pytest` 回归

## 回滚
改动均为增量函数与调用切换，回滚对应 commit 即恢复旧行为。