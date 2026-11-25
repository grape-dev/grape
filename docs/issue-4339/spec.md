# Issue #4339 需求文档：events.payload 升 MEDIUMTEXT，消除 64KB 静默截断

> 来源：Issue #4339 正文 + Issue Agent 发布的 Spec（评论）。本文只固化需求口径，不重复 Issue 原文。

## 1. 问题

`events.payload` 列类型是 `TEXT`（65,535 字节），而同表 `agent_logs` / `result` /
`structured_output` 都是 `MEDIUMTEXT`（16,777,215 字节）。超过 64KB 的事件 payload
在 MySQL 非严格 `sql_mode` 下被**静默截断**，落库成半截 JSON：

```
seq 107279 | LENGTH=65535 | JSON_VALID=0 | issue_opened
seq 107291 | LENGTH=65535 | JSON_VALID=0 | issue_updated
```

`src/kernel/event_payload_view.py::payload_view_from_json` 解析失败后返回空视图，
下游 prompt 组装、架构守卫影响面、前端事件详情全部拿不到上下文——**静默数据丢失**。

## 2. 目标与非目标

目标：

1. `events.payload` 在 MySQL 上为 `mediumtext`（新装库由建表 DDL 保证，存量库由幂等迁移收敛）；
   SQLite 侧 DDL 文本同步，避免 schema 漂移守卫报红。
2. 写入口按后端上限显式校验字节数：超限 `ERROR` + 拒绝写入，**绝不写截断值**。
3. 同库同类大字段审计：`grape_t_channel_outbound_card.payload` 同批升 `mediumtext`。
4. 存量坏行（107279/107291）给出只读盘点结论与处置口径（写入 PR 描述，不混进 DDL 提交）。

非目标：

- 不改 `payload_view_from_json` 的降级语义（解析失败仍返回空视图，只是不再被截断触发）。
- 不做历史坏行数据修复/回填（原文已丢，无法恢复）。
- 不动已是 `MEDIUMTEXT` 的 `result` / `agent_logs` / `structured_output`。
- 不动 `event_pipeline` / `event_store` 中「读旧 payload 再追加小字段」的 UPDATE 路径
  （每次只新增一个键，不会把已合法行推到上限之上）。

## 3. 验收标准

| # | 判据 |
|---|------|
| A1 | `information_schema.columns` 中 `events.payload` 的 `DATA_TYPE=mediumtext`；SQLite `_EVENTS_TABLE_DDL` 同步 |
| A2 | payload > 64KB 的 `issue_opened` 落库后 `LENGTH(payload) > 65535`、`JSON_VALID=1`、`payload_view_from_json` 可解析 |
| A3 | payload > 16,777,215 字节被显式拒绝（`append_event` 返回 0），日志含 `event payload too large` + 字节数 + event_type/repo/issue，库中无截断行 |
| A4 | 迁移幂等：已是 mediumtext 的库重复启动零 ALTER；`min_code_version` 门槛拦截时不盖章（抛错） |
| A5 | 跨域只读视图（`TABLE_TYPE != BASE TABLE`）跳过且不盖章，交 owner 域收敛 |
| A6 | `grape_t_channel_outbound_card.payload` 达 `mediumtext` |
| A7 | 存量坏行盘点口径写入 PR 描述，与 DDL 变更分离 |
| A8 | `tests/runtime/test_event_store.py`、`tests/kernel/test_migration_parity_stage1.py`、`tests/api/test_destructive_ddl_stamp_scenarios.py`、`tests/arch/test_migration_version_integrity.py` 全绿，并新增守卫/迁移探测用例 |

## 4. 风险

| 风险 | 缓解 |
|------|------|
| `MODIFY COLUMN` 是 COPY 算法，锁写整张 `events` 表 | 类型探测短路，只在存量库首启执行一次；PR 中注明首启宜安排在低峰/维护窗口 |
| 迁移器 `except: pass` 吞失败导致列永远停在 TEXT | ALTER 后复检 `DATA_TYPE`，未变即抛错不盖章 |
| 迁移执行器只把含 `MODIFY COLUMN` 的语句路由到 `_destructive_ddl` | 常量显式带 `COLUMN` 关键字（并有用例锁定） |
| 拒绝写入导致上游事件丢失 | `ERROR` 带 event_type/repo/issue/字节数，可定向重放；16MB 阈值远高于实际 payload |
| MySQL TEXT 族不接受字面量 DEFAULT | 复用 `mysql_compat` 的 `DEFAULT 'x'` → `DEFAULT ('x')` 翻译（既有先例：`skill_files` 升 MEDIUMTEXT） |