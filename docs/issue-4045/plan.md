# Issue #4045 方案设计（plan）

> Spec：`docs/issue-4045/spec.md`。目标合入分支 `arch-weekly-11-ljt`，开发分支 `feat/issue-4045`。

## 改动面

| 文件 | 改动 | 说明 |
|---|---|---|
| `src/domains/runtime/public/__init__.py` | 修改 | ① `purge_runtime_credential()` 同事务摘绑 `chat_agent_session`（`runtime_id=0`，保留 `engine_uid`）并计数；② 新增具名查询 `agent_runtime_row_by_uid`（id+name）与 `chat_config_runtime_id_backfill`（行级回写） |
| `src/kernel/session_manager.py` | 修改 | `_chat_config_row_engine()` 返回 `(name, usable, reason)` + uid 重解析回写；`_pick_chat_config_row()` 透出 reason；`get_chat_config()` 出参补 `bound`/`reason`；悬空 WARNING 进程内去重（上限 256） |
| `src/domains/runtime/routes/chat.py` | 修改 | `GET /api/chat/config` 透出 `bound` / `reason`（新增字段，不改既有字段语义） |
| `scripts/migrations/009_chat_config_dangling_runtime.py` | 新增 | 存量悬空 `runtime_id` 置 0（保留 `engine_uid`），dry-run 默认、`--apply` 前备份、幂等 |
| `tests/runtime/test_issue_4045_chat_config_dangling_runtime.py` | 新增 | 删除侧/读路径/去重/迁移幂等 |
| `scripts/diag/chat_config_rows_{audit,migrate}.py` | 修改 | 随 `_pick_chat_config_row` / `_chat_config_row_engine` 返回值元数调整解包 |

## 关键设计

### 1. 删除侧（revoke）同事务收敛

`purge_runtime_credential()` 的 `_any_tx()` 内，在删引擎行之前追加：

```sql
UPDATE chat_agent_session SET runtime_id=0 WHERE runtime_id IN (...)
```

**不写 `engine_uid`**（issue agent 定稿：保留 uid 作为可审计/可重绑定线索）。同一事务内已有
`devops_agent_session` 摘绑、`engine_capabilities` 删除、`agent_runtimes` 删除、凭据行删除；
任一步失败整体回滚，不留半态。返回报表新增 `chat_sessions` 计数，便于日志核对
（`revoke <cid>: engines=… sessions=… chat_sessions=…`）。
场景侧 `engine_runtime_ids` / `engine_runtime_uids` 由既有 `cleanup_scene_engine_bindings`
（runtime 域）/ `_cleanup_scene_engine_runtime_ids`（mgmt 侧）收尾，本次不改其触发点。

### 2. 读路径三态

`_chat_config_row_engine(runtime_id, engine_uid="", row_id=None)` → `(backend_name, usable, reason)`：

| 情形 | usable | reason | 行为 |
|---|---|---|---|
| `engine_uid` 命中且引擎名已知 | True | `""` | 命中行 `runtime_id` 与解析 id 不一致且给了 `row_id` → 回写 `runtime_id`（uid 重解析自愈） |
| `engine_uid` 失效/未知 → 回落 `runtime_id` 且命中已知引擎 | True | 回落成功时 `""`（并回写 uid 轨道由既有 mirror 负责，本函数只补 `runtime_id`） |
| `runtime_id=0` 且无 uid | True | `unbound` | 真未配置：保持既有本机直连兜底，但出参可辨识 |
| `runtime_id>0` 且引擎行不存在 | False | `engine_missing` | 候选不可用 → `_pick_chat_config_row` 继续找下一候选，全不可用则首候选置 0 |
| `runtime_id>0` 但引擎名未知 | False | `engine_missing` | 与「引擎行已删」同归因（都是「配置指向的引擎不可用」） |

`_pick_chat_config_row(user_id)` → `(row_id, row, backend_name, reason)`：命中可用候选时 reason 取该候选
的解析结果（`""` 或 `unbound`）；全不可用时取首候选的 `engine_missing`；无候选时 `(None, None, "", "")`。

`get_chat_config(user_id)` 出参新增：

- `bound`：**配置行的绑定是否有效**（与兜底 `runtime_id` 解耦）。`reason in ("engine_missing","unbound")`
  → `bound=False`；否则 `bound = runtime_id > 0`；
- `reason`：`""` / `engine_missing` / `unbound`。

「引擎失效」与「真未绑定」因此可区分，而 `runtime_id` 的既有语义（本机直连兜底 id）保持不变。

### 3. WARNING 收敛

模块级 `_DANGLING_WARN_SEEN`（dict，容量上限 256，超限整体清空以保持有界）+ 锁：

```python
def _warn_dangling_runtime_once(runtime_id):
    first = _mark_warned(runtime_id)      # 锁内判定
    (log.warning if first else log.debug)(f"get_chat_config: runtime_id={runtime_id} 不存在（引擎行已删除），按未配置处理")
```

首条 `warning`（保持既有检索口径 `get_chat_config: runtime_id=… 不存在`），其后 `debug`。
迁移上线后总量归零，残留仅每实例首条。

### 4. 迁移 009

按 008 范式：默认 dry-run；`--apply` 先建备份表 `chat_agent_session_dangling_bak_<UTC>`
（id / user_id / runtime_id / engine_uid / backed_up_at），逐行 `UPDATE … WHERE id=? AND runtime_id=?`；
结束后复核 0 悬空，否则返回 1。二次执行 0 变更（幂等）。旧库缺 `engine_uid` 列时降级为
不含该列的扫描/备份（记录到输出，不静默）。

## 测试策略（TDD：先红后绿）

`tests/runtime/test_issue_4045_chat_config_dangling_runtime.py`（复用 `tests/conftest.py` 的 sqlite 测试库，
用例内种子/清理并快照回滚 `agent_runtimes`）：

1. `test_revoke_detaches_chat_config_and_keeps_engine_uid`：造凭据 + 引擎 + chat 配置行 + devops 会话 →
   `purge_runtime_credential` → 引擎行/凭据行删除、chat `runtime_id=0`、`engine_uid` 保留、
   报告 `chat_sessions>=1`；
2. `test_uid_reresolve_backfills_runtime_id`：配置行 `runtime_id` 指向已删 id，`engine_uid` 指向现存引擎 →
   `get_chat_config` 返回该引擎且 `bound=True`，且 DB 行 `runtime_id` 已被回写；
3. `test_dangling_without_uid_is_engine_missing`：悬空 + 无 uid → `bound=False, reason=engine_missing`；
4. `test_unbound_row_reason`：`runtime_id=0` 无 uid → `reason="unbound"`、`bound=False`；
5. `test_dangling_warning_deduped`：同一 `runtime_id` 连续两次解析 → 仅 1 条 `warning`，其后 `debug`；
6. `test_migration_009_idempotent`：临时 sqlite 表（含悬空/正常行）→ dry-run 不改、apply 置 0 保留 uid、
   二次 apply 0 变更。

## 风险与回滚

| 风险 | 缓解 |
|---|---|
| 读路径新增回写带来写压力 | 仅当「uid 重解析 id ≠ 行内 runtime_id」才写一次，之后行内已一致不再写 |
| 出参新增字段影响前端 | 纯新增字段，旧调用方忽略；`runtime_id`/`backend` 语义不变 |
| 迁移误伤正常行 | 只命中 `runtime_id>0 AND NOT EXISTS(agent_runtimes.id)`；`--apply` 前整表备份；二次执行 0 变更 |
| 与 #250 清理重复 | 场景侧沿用既有函数，不新增第二处 UPDATE |