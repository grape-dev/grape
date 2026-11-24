# Spec: Issue #3738 — meta upsert 在 MySQL 下必失败（set_meta_value_any 依赖 rowcount 判断行存在性）

> 来源：Issue #3738 正文 + VOD Agent 评论 #202824460 + Issue Agent Spec 评论 #202824574。

## 目标

`set_meta_value_any()`（`src/domains/runtime/public/__init__.py:4046-4054`）改为**单语句**
幂等 upsert：执行 `_any_sql("meta_upsert")`（``INSERT INTO meta (`key`, value) VALUES (?, ?)
ON CONFLICT(`key`) DO UPDATE SET value=excluded.value``，SQL 定义见同文件 `:3072-3073`；
MySQL 方言由 `src/kernel/mysql_compat.py:441-456` 翻译为 `ON DUPLICATE KEY UPDATE`）。
存在性只由主键回答，彻底不依赖 `UPDATE` 的 `rowcount`。

## 根因

- 旧实现「UPDATE → rowcount=0 则 INSERT」：MySQL 默认连接（未开 `CLIENT_FOUND_ROWS`）下
  同值 UPDATE 的 `rowcount=0`，被误判为「行不存在」，随后 INSERT 撞 `meta.PRIMARY` → 1062。
- 调用方 `src/domains/mgmt/user/db.py:3285-3288` 把 1062 吞成一条 `set_meta_value failed` WARNING，
  写入静默失败（生产 12 条/小时、212 条/24 小时），`global_*_bot_token_status` 等关键状态位
  停在旧值，影响 token 轮换与 reauth 判定；并发首次写入时丢值。
- 同文件批量版 `set_meta_values_any()`（`:4062` 起）已是 `meta_insert_absent + meta_upsert_update`
  的单事务写法（注释明确记录了同一坑）；本次修复改用更短的单语句 `meta_upsert`，语义等价。

## 范围（In）

- `set_meta_value_any()` 的 upsert 实现（`_any_cursor(write=True)` 内单语句 `meta_upsert`）。
- 新增 `tests/runtime/test_3738_meta_upsert_idempotent.py`：同值幂等、并发首写、
  「不依赖 rowcount / 不出现 UPDATE→INSERT 回退」护栏、批量版行为不变。

## 范围（Out）

- 不动批量版 `set_meta_values_any()`（保留既有 `meta_insert_absent + meta_upsert_update` 实现）。
- 不动 `config_store.py` / `mgmt user db.py` 调用链、`meta` 表 schema、
  `meta_upsert_update` / `meta_upsert_insert` SQL 文本。

## 验收标准

1. 同 key 同值连写两次：无异常、无 `set_meta_value failed` WARNING，最终值正确（sqlite 实跑；
   MySQL rowcount 语义用假连接模拟覆盖）。
2. 两连接并发首写：均成功、最终值等于后写者、无 1062 外泄。
3. 不再依赖 rowcount 判断（代码 + 用例双重确认：一次写入仅一条带冲突处理的 upsert 语句，
   不出现 UPDATE 或不带冲突处理的 INSERT）。
4. 批量版 `set_meta_values_any` 既有行为与用例保持不变。
5. 现有测试通过。
6. 上线后 1 小时内生产 `set_meta_value failed` 归零（部署侧核对）。
