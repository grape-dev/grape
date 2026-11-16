# Plan: Issue #646 实现方案

## 分层实现顺序
1. `src/api/engine_uid.py`（新）：uuid 派生纯函数，无 DB 依赖
2. `src/api/db_setup.py`：
   - 六表 DDL 加列（agent_runtimes.engine_uid/credential_uid、runtime_credentials.credential_uid、
     agent_type_runtime_pool.engine_uid、engine_capabilities.engine_uid、
     agent_tasks.engine_uid/engine_name_snapshot/pool_engine_uids、task_messages.engine_uid）
   - `_create_indexes` 补 UNIQUE 索引（NULL 多行合法，孤儿行 uid=NULL 不冲突）
   - `register_migration` 三条：加列(safe) → 回填(backfill, 分批+孤儿报告) → MySQL FK(safe, SQLite 跳过)；
     FK 一律 **ON DELETE RESTRICT**（复审裁定），删除走显式清理链
3. `src/user/db.py`：`save_agent_runtimes` 派生/写入 engine_uid（本机行 credential_uid=''）；
   helper：`get_engine_uid_by_runtime_id` / `get_runtime_id_by_engine_uid` / `resolve_engine_refs` /
   `engine_ref_snapshot`；
   统一改绑/显式清理例程 `_rebind_runtime_references`（公开入口 `rebind_runtime_references` /
   `explicit_cleanup_runtime_references`）：去重路径改绑引用 + uid 镜像指向保留行；删除路径
   （吊 revoke / devstage / scope 切换重复行 / 刷新过期行）显式清理池/能力/会话/绑定/场景引用，
   agent_tasks 快照语义保留，均输出对账报告（RESTRICT 兜底防漏）
4. `src/api/routes/runtime.py`：注册时查/补 credential_uid，派生 engine_uid，uid 优先 upsert，
   返回 payload 双带 id+uid（旧 worker 忽略 uid，兼容混跑）
5. `src/runtime/service.py` + `src/api/event_pipeline.py`：`_TASK_COLUMNS` 加 3 列并在三处 INSERT
   组装处经 `engine_ref_snapshot`（try/except 兜底 NULL/''，防旧库缺列失败）
6. `src/api/routes/scenes.py` / `agents.py`：engine_runtime_ids 支持 uid 字符串（解析失败 400 fail-fast）

## 迁移报告
- 进程内 `_ISSUE_646_UID_MIGRATION_REPORT` + `get_issue_646_uid_migration_report()`；
- 孤儿引用逐条 `log.warning`（engine/credential/pool/capability/tasks/messages/pool_json）。

## 测试策略
`tests/atomic/test_issue_646_engine_uid.py`：真实 SQLite tmp 库直调迁移 fn（幂等两跑）、uid 派生确定性、
孤儿置 NULL、save_agent_runtimes 重复注册 uid/id 不漂移、表重建后 uid 不变、credential 改名不影响 uid、
双读解析 helper、agent_tasks 快照组装、FK DDL RESTRICT 断言、显式清理链（池/能力删除 + 会话置 0 +
任务快照保留 + uid 侧引用清理）与改绑 uid 镜像、刷新过期行清理链、吊销级联清理回归（tests/test_issue_417）。
