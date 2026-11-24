# Plan: Issue #3738 — set_meta_value_any 幂等 upsert 修复

## 方案（初版，已被下方「基线合并口径」取代）

> 最终落地为**单语句** `meta_upsert`（见文末「基线合并口径」），下述两段式仅作历史记录。

与批量版 `set_meta_values_any()` 同构的单事务写法：

```python
def set_meta_value_any(key: str, value: str) -> None:
    with _any_tx() as c:
        c.execute(_any_sql("meta_insert_absent"), (key, value))
        c.execute(_any_sql("meta_upsert_update"), (value, key))
```

- `_any_tx()`：自有 DSN → runtime 域真事务连接；单库/测试拓扑 → 进程库 + db_lock +
  commit/rollback，与批量版语义完全一致。
- `meta_insert_absent`（`INSERT OR IGNORE`）在 MySQL 方言下由 `mysql_compat` 翻译为
  `INSERT IGNORE`，行已存在时静默忽略，不依赖任何 rowcount 语义。
- 异常向外抛出，不新增静默 `except`（调用链 `db.py:3437` 现有 except 行为不变，
  但修复后不再触发 1062）。

## 测试 seam

公开接口 `set_meta_value_any()` / `set_meta_values_any()` / `get_meta_value_any()`
（用户可见行为），不测内部 cursor 细节；MySQL rowcount 语义用假连接在
`db_registry` seam 上模拟（同 `test_issue_990_mysql_lock.py` 假连接约定）。

## 任务拆解（TDD 红/绿）

1. 红：`tests/runtime/test_3738_meta_upsert_idempotent.py`
   - 假连接模拟 MySQL rowcount=0-on-same-value：行已存在时 UPDATE 返回 rowcount=0，
     断言 `set_meta_value_any` 不抛 1062（当前实现必抛 → 红）。
2. 绿：最小实现（上述两行式 upsert），用例转绿。
3. 回归护栏：spy 假连接断言执行序列恰为
   `[meta_insert_absent, meta_upsert_update]`，永不出现「UPDATE 后按 rowcount INSERT」。
4. sqlite 实跑：同值幂等 ×2、两线程并发首写、批量版行为不变。

## 风险

- 无 schema 变更、无 SQL 文本变更、无调用链变更；批量版写法已在线上验证。
- 并发首写在单库拓扑由 `db_lock` 串行化；自有 DSN 拓扑由事务隔离 + INSERT IGNORE 保证。

## 基线合并口径（2026-11-24，合并 arch-weekly-11-ljt fe1d51a33）

目标分支已独立修复同一缺陷（`ac92dc382`，2026-11-21）：`set_meta_value_any()`
收敛为**单语句** upsert（`meta_upsert` = `INSERT ... ON CONFLICT(key) DO UPDATE SET
value=excluded.value`，列名 key 带反引号，MySQL 侧由 `mysql_compat` 翻译成
`ON DUPLICATE KEY UPDATE`）。冲突解决取目标分支实现，理由：

- 语义等价：存在性只由主键回答，同值重写与首写均幂等，不再触发 1062；
- 单语句更短更原子（1 次往返），且不再用 `INSERT IGNORE` 连其它告警一起吞掉；
- 不把已合入的更优实现反向改回两段式。

回归用例随之改锁**行为契约**（不再锁 `[insert-absent, update]` 语句对形状）：
一次写入 = 一条带冲突处理的 upsert 语句；序列里出现 UPDATE 或不带冲突处理的
INSERT 即判失败；另保留 MySQL rowcount 语义模拟、sqlite 实跑（同值幂等 / 并发首写）
与批量版行为不变断言。目标分支既有的
`tests/api/test_meta_upsert_idempotent.py::test_s3_source_never_uses_rowcount`
继续锁源码口径（函数体无 rowcount、`meta_upsert` 走 ON CONFLICT）。
