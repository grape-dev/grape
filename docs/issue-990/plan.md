# Plan: Issue #990 — 会话 token 快照与运行时注入一致性

## 关键设计决策
| 决策 | 选项 | 选择 | 理由 |
|------|------|------|------|
| 快照权威源 | 重新签发 / 复用 `env_extra` 快照 | 复用有效快照 token | 避免每次执行产生新 token 造成快照/runtime 漂移 |
| 轮换时机 | 每次执行 / 仅旧 token 失效 | 仅旧 token 失效时轮换 | 覆盖 token 过期/撤销窗口，幂等且不误伤并发执行 |
| 轮换原子性 | 分步提交 / 锁内完成 | 锁内过期旧 token + 写快照 | 防止并发刷新互相作废 |
| 401 指引位置 | 改 CLI 源码 / 服务端 message | 服务端 `detail.message` | CLI 已透传 message；本仓职责内闭环 |

## 数据流
```
enqueue_task: issue_session_token(session_key=task_id) → agent_tasks.env_extra 快照
      │
worker 执行前: GET /tasks/{id}/env
      └─ sync_task_session_token:
           token 有效（session_key/repo/agent 匹配且未过期）→ 原样返回
           token 失效/不匹配 → rotate（过期旧 token + 签发新 token）→ 回写 env_extra → 返回
      └─ worker env.update(fresh_env) → 运行时进程 env 与快照一致
      │
direct 执行: 优先 sync_task_session_token(task_id)（无任务行才新签）
```

## 实施步骤
1. `auth_token.rotate_session_token` + `SESSION_TOKEN_RECOVERY_HINT`
2. `service.sync_task_session_token`（复用 / 轮换 / 回写快照）
3. `runtime_task_env` 返回同步后的 token
4. `direct_executor` 改为复用快照 token
5. `deps` 401 message 追加恢复指引
6. 新增测试：轮换一致性、快照复用、401 恢复文案

## 验证
- `pytest tests/test_issue_990_session_token_snapshot.py -q`
- `pytest tests/test_auth_token.py tests/test_session_token_lifecycle.py tests/test_dsh_runtime.py -q`
- 全量 `pytest` 回归

## 回滚
改动均为增量函数与调用切换，回滚对应 commit 即恢复旧行为。