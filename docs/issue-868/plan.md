# Plan: Issue #868

## 切片（TDD 红/绿分提交）

1. **S1 迁移**（红→绿）：
   - `src/api/db_setup.py`：`_AGENT_SESSIONS_TABLE_DDL` 加 `engine_uid VARCHAR(36) DEFAULT NULL`；新增 `_ISSUE_868_SESSION_UID_COLUMNS/_INDEXES` + `_migration_868_add_session_uid_columns`（202611030005 safe：_ensure_column + 幂等索引 + 列核验不盖章）+ `_migration_868_backfill_session_uids`（202611030006 backfill：按 agent_runtimes.engine_uid 映射回填；孤儿/未绑定 runtime_id → uid 置 NULL 入报告）+ `get_issue_868_session_uid_migration_report`。
   - `src/session_manager.py`：chat_agent_session 兼容列 ALTER（该表既有列全走此模式）。
2. **S2 双写双读（session_store / session_manager / chat.py）**（红→绿）：
   - `session_store.get_or_create_agent_session`：SELECT/INSERT 带 engine_uid（列缺失降级旧 SQL）；返回含 engine_uid。
   - `session_store.upsert_agent_session`：UPDATE/INSERT 同步 engine_uid 镜像。
   - `session_manager._write_chat_config_row / _chat_config_candidate_rows / _chat_config_row_engine / get_chat_config`：双写 + uid 优先解析（uid 失效回退 runtime_id），缺列降级。
   - `chat.py _enqueue_chat`：cfg.engine_uid 优先解析 runtime（id 兜底）。
3. **S3 对账**（红→绿）：`user/db._rebind_runtime_references` 步骤 3 扩展 uid 轨道：改绑→保留行 uid；删除/孤儿→ NULL；报告新增 sessions_uid_rebound / sessions_uid_nulled / chat_sessions_uid_rebound / chat_sessions_uid_nulled；chat_agent_session 同步处理（缺列/缺表容错）。
4. **S4 文档**：`docs/issue-868/claim-by-uid-v2-protocol-spec.md`、`docs/issue-868/mysql-upgrade-runbook.md`。
5. **S5 CI**：windows-smoke.yml linux-backend 增加 `tests/atomic/test_issue_868_session_engine_uid.py` 步骤（沿用 #890 的 066c6e8f9 模式）。

## 关键设计决策
| 决策 | 选择 | 理由 |
|---|---|---|
| 迁移版本号 | 202611030005/6（全局唯一序列，两线共用一套号段语义） | arch 线已用 202611030001~4；取下一号避免碰撞，weekly 线合入时同号幂等不漂移 |
| uid 读取优先级 | engine_uid 命中且有效 → 用 uid 解析；uid 空/失效 → runtime_id 兜底 | Spec：uid 优先 id 兜底 |
| 旧库缺列 | 列存在性探测（PRAGMA table_info）失败即回退旧 SQL | 不崩溃、行为与迁移前一致 |
| 孤儿 uid | 置 NULL + 报告 | Spec：不静默丢弃 |

## 风险
- chat_agent_session 历史库无 runtime_id 列：对账块整体 try/except 容错（模式同 runtime_bindings）。
- 双写增加写路径开销：单行 UPDATE 多一列，忽略不计；索引仅普通索引。
