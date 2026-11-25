# Issue #4339 方案设计

## 1. 改动面

| 文件 | 改动 |
|------|------|
| `src/domains/runtime/schema_ddl.py` | `_EVENTS_TABLE_DDL`：`payload TEXT DEFAULT '{}'` → `payload MEDIUMTEXT DEFAULT '{}'` |
| `src/domains/runtime/migrations_ddl.py` | 新增 `ALTER_EVENTS__EVENTS_PAYLOAD_MEDIUMTEXT`（含 `MODIFY COLUMN`） |
| `src/domains/runtime/migrations/events_payload_mediumtext.py` | 新增幂等迁移（探测短路 + `_destructive_ddl` + ALTER 后复检 + post_check） |
| `src/api/db_setup.py` | 注册 `202611250003`（runtime 域，原 202611250001，门禁复审 renumber）与 `202611250002`（mgmt 域） |
| `src/domains/runtime/event_store.py` | `_insert_event_row` 前置字节守卫；判定抽成可复用 `payload_size_exceeded` / `payload_size_guard` |
| `src/domains/runtime/pipeline/event_pipeline.py` | 去重快路径裸 INSERT 前置同一份守卫（R1）；`seq==0` fail-closed 不派发（R2） |
| `src/domains/mgmt/schema_ddl.py` | `_CHANNEL_OUTBOUND_CARD_DDL`：`payload TEXT NOT NULL DEFAULT '{}'` → `MEDIUMTEXT` |
| `src/domains/mgmt/migrations_ddl.py` | 新增卡片表 `MODIFY COLUMN payload MEDIUMTEXT NOT NULL DEFAULT '{}'` |
| `src/domains/mgmt/migrations/channel_outbound_card_payload_mediumtext.py` | 同形态幂等迁移 |
| `tests/runtime/test_event_payload_mediumtext.py` | 新增用例（DDL/迁移探测/守卫/超 64KB 落库可解析） |

`tests/arch/migration_versions_baseline.json` **不改**：该文件是「已发布迁移快照」，
文件头与 `scripts/gen-migration-baseline.py` 都规定只在发布评审时整体刷新并单独提交；
且 `--write` 会把本分支全部未发布迁移（`202611231001`~`202611240010`）一次性写成
「已发布」，语义不成立。`tests/arch/test_migration_version_integrity.py` 的 R3 只要求
**新号大于基线 max（202611230012）**，`202611250003` / `202611250002` 天然满足，因此不改基线也能全绿。

`_EVENTS_TABLE_DDL` 由 `src/api/db_setup.py` 的 `_NEW_TABLES` 注册（`("events", _EVENTS_TABLE_DDL)`），
单点改动即双端生效；SQLite 无长度语义，`MEDIUMTEXT` 只是类型名，行为无变化。

## 2. 幂等存量迁移（runtime）

`migrate_events_payload_mediumtext()`：

1. **非 owner 判定**：`_table_present` 失败或 `_is_base_table` 为假 → 抛 `_NonOwnerObject`。
   执行器按「跳过且不盖章」处理，由 owner 域（runtime）进程收敛；对视图直接 ALTER 会 1346/1347。
2. **SQLite 短路**：SQLite 无列长上限，新装库口径由 `schema_ddl` 保证 → 直接返回，不发 DDL。
3. **MySQL 类型探测**：`information_schema.columns.DATA_TYPE` 已是 `mediumtext`/`longtext` → 直接返回
   （**重复启动零 ALTER**，避免 `MODIFY COLUMN` 的 COPY 锁表在每次启动重放）。
4. **ALTER**：`_destructive_ddl(c, ALTER_EVENTS__EVENTS_PAYLOAD_MEDIUMTEXT, kind_hint="cleanup",
   version=MIGRATION_VERSION)`。返回 `False`（`min_code_version` 门槛拦截）→ 抛 `RuntimeError`，不盖章。
5. **ALTER 后复检**：`_destructive_ddl` 对非 MDL 异常 fail-open，DDL 没报错不等于生效 →
   再查 `DATA_TYPE`，仍不是 `mediumtext`/`longtext` → 抛错不盖章。
6. **post_check**：`verify_events_payload_mediumtext()` 只做只读探测（MySQL），非目标态抛错。

为什么常量必须带 `COLUMN`：`src/api/db_setup.py::_destructive_ddl` 的调用点是迁移执行器按
「语句是否含 `MODIFY COLUMN`」路由的；历史先例 `ALTER_AGENT_TASKS = "ALTER TABLE agent_tasks MODIFY
skill_files MEDIUMTEXT"` 缺 `COLUMN`，会走裸 `db.execute` 绕过 `min_code_version` 门槛，失败也被
外层 `except` 吞掉。本条不复用该先例的写法，只在注释中保留差异说明。

## 3. 写入口字节守卫（event_store + event_pipeline）

判定函数收口在 `event_store`，两个写入口共用同一份：

```
payload_size_exceeded(bytes) -> bool          # 纯判定，无副作用
payload_size_guard(bytes, event_type, repo_key, issue_id) -> bool
                                              # 判定 + 留痕；超限 ERROR 返回 False
```

`bytes = len(json.dumps(payload).encode("utf-8"))`；阈值 16,777,215（`MEDIUMTEXT` 上限）、
WARNING 阈值 1,048,576。SQLite 同样按该阈值拒绝，保持两端行为一致。

两个写入口（QA 审查 R1：只挂一处即仍有静默截断）：

| 写入口 | 命中场景 | 守卫位置 |
|--------|----------|----------|
| `event_store._insert_event_row`（`append_event` 路径） | a2a_event / issue_comment / 无 issue_id 或 agent_type | `json.dumps` 后、任一 INSERT 前，返回 0 |
| `event_pipeline.run_pipeline` 去重快路径的裸 INSERT | `issue_opened`/`issue_updated`/`pull_request_*` 且 issue_id + agent_type 齐备 | INSERT 前调 `payload_size_guard`，超限 fail-closed |

拒绝语义（R2）：`append_event` 超限返回 0，与「瞬时写失败」不可区分。`run_pipeline` 在
`seq == 0` 处统一 fail-closed —— 释放已抢占的 `event_active_guard` 行、落
`event_skipped`（`reason=event_payload_rejected`；非超限写失败为 `event_insert_failed`）、
`result.ok=False` 且**不** pick_engine / 入队，避免「事件行不存在但任务照常入队」的
event_seq=0 孤儿任务。守卫行必须释放，否则同 (issue, agent) 的后续事件被残留守卫永久
dedup 掉。

- 告警前缀 `event payload too large` / `event payload large` 可 grep，且与
  `event payload view: parse failed`（现象告警）区分。
- 不写截断值：守卫在 INSERT 前返回，库中不会出现半截 JSON。

## 4. 卡片表同批整改（mgmt）

`grape_t_channel_outbound_card.payload` 同为 `TEXT`，IM 卡片模板（markdown 卡片体）超长同样会被
静默截断，且该表是外发重试的唯一事实源——截断后重试发出去的是坏卡片。因此**同批整改**，
不另立批次。迁移形态与 runtime 完全一致（探测短路 + `MODIFY COLUMN` + 复检 + post_check），
但独立版本号与独立模块（表归 mgmt 域）。

## 5. 存量坏行处置口径

只读盘点 SQL（不进代码、写进 PR 描述）：

```sql
SELECT seq, event_type, repo_key, issue_id,
       LENGTH(payload) AS bytes, JSON_VALID(payload) AS valid, created_at
FROM events
WHERE JSON_VALID(payload) = 0 OR LENGTH(payload) >= 65535
ORDER BY seq DESC;
```

结论：`seq 107279` / `107291` 的原始 payload 已物理丢弃（原文不可恢复），处置 = **接受缺失 +
按需重放上游**：需要上下文时用 `source='gitcode'` 的事件 key 重新触发一次 `issue_opened` /
`issue_updated`（`entity_type` + `repo_key` + `issue_id` 已保留），无需回填。DDL 变更不涉及
历史行改写，二者分提交。

## 6. 迁移版本号

| 版本号 | 名称 | 域 |
|--------|------|----|
| `202611250003` | `issue4339-events-payload-mediumtext` | runtime |
| `202611250002` | `issue4339-channel-outbound-card-payload-mediumtext` | mgmt |

基线已发布最大号 `202611230012`，本分支来源最大已注册号 `202611240010`，两个新号顺延至
`202611250003`/`202611250002`。合并前须人工对目标环境 `grape_t_schema_migrations` 复核（同号不同名会被
执行器 ERROR 并跳过）。

## 7. 测试

- 静态：DDL 文本（events / card）、ALTER 常量含 `MODIFY COLUMN`、两条迁移已注册且域正确。
- 迁移行为（假 cursor，无需 MySQL）：已目标态零 DDL；`text` 触发 ALTER 且复检通过；
  门槛拦截抛错；ALTER 后仍为 `text` 抛错；非 owner（视图/缺表）抛 `_NonOwnerObject`；SQLite 分支零 DDL。
- 写入口（真 SQLite 测试库）：>64KB payload 落库 `LENGTH>65535` 且可 JSON 解析、视图可投影；
  >1MB 落库且留 WARNING；>16MB 返回 0、留 ERROR、无新行。
- 管道级（R1/R2，真 `run_pipeline`）：`issue_opened` 超 16MB → 去重快路径拒绝，无事件行、
  无 `agent_tasks`、守卫行已释放、`event_skipped(reason=event_payload_rejected)`、
  `result.ok=False`；`issue_comment` 超 16MB（走 `append_event`）→ `seq=0` fail-closed，同样不派发。
- 收口守卫：机械扫描 `src/` 下所有含 events INSERT 的模块，要求插入点之前存在
  `if not payload_size_guard(` 调用（新增裸 INSERT 漏接守卫时直接变红）。
- **覆盖边界**：>64KB roundtrip 跑在 SQLite（自身无 64KB 列上限），回归的是「守卫/迁移不误杀、
  视图可解析」；MySQL 侧截断回归由 DDL 文本断言 + `information_schema` 探测分支兜底，
  真 MySQL 门禁需外部实例（不在本仓库单测范围内）。