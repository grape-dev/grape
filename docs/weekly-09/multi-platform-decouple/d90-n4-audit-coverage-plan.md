# D90-N4 实施方案：bot token 消费审计覆盖（拍板 1A/2B/3A + MySQL 场景）

**来源**：N2 派生挂账 (b)。用户拍板：1A（做审计）/2B（rc 加 UUID）/3A（全量记）。
**硬约束**：现网生产是 MySQL——所有 DDL/DML 必须双方言安全（N1 rowid 教训）。

**状态**：已确认并落地——c5c94354a（DDL 迁移 202611070003 + 回填步骤 4 +
RepoBotToken 两分支接线）；性能补强 b55268434（索引/保留期周期化/异步批量，
含 sqlite3 同连接跨线程共享事务态真根因修正）+ 4202f89c9（清理周期化 30 天
节流五条细节）；测试 test_n4_audit_coverage（UC35/UC36）。

---

## 一、变更清单

### 1. grape_t_repo_credentials 加 UUID 主键（拍板 2B）

**DDL**（盖章迁移，双方言安全——无 SQLite 专属语法）：
```sql
ALTER TABLE grape_t_repo_credentials ADD COLUMN id TEXT DEFAULT ''
```
- 复合主键 `(repo_key, cred_type)` **保留不动**（唯一性语义继续由它承载，
  id 列做锚点不做主键——MySQL 下 TEXT 列无法 ALTER 成主键且改主键需
  重建表，违反"不动存量行为"原则）
- id 唯一性：应用层保证（uuid4 全局碰撞概率忽略）+ 可选 UNIQUE INDEX
  （本批不加——回填完成前旧行 id 为空串会唯一冲突，等回填后批次再加）
- 表注释登记：id 为审计锚点，复合键为业务唯一键

**回填**（启动幂等，credentials_backfill_n1 步骤 4）：
`SELECT repo_key, cred_type WHERE id='' OR id IS NULL` → 复合键定位 UPDATE
（与 N1 UUID 回填同款双方言形态）

**读方零变更**：全部读方走 `get_repo_credential_value`（复合键查询，不碰 id）。

### 2. usage_log 扩两列（拍板 1A + 锚点承载）

```sql
ALTER TABLE credential_usage_log ADD COLUMN repo_key TEXT DEFAULT ''
```
- bot 级审计行：credential_id = rc 行 UUID（回填后非空）；
  repo_key 冗余存（审计行自足，不用 JOIN 反查）
- 用户级审计行：repo_key=''（现状不变）
- DDL 幂等（列存在即跳过，sqlite/MySQL 同形态）

### 3. 消费点接线（拍板 3A 全量）

唯一收口点 = `RepoBotToken._fetch` 命中处（rc 优先命中 + 历史列回落命中
两分支都记——铁律 9：高频审计走 Core insert 豁免通道，`log_usage` 已是
Core 形态）。
- credential_id = rc 行 id（读 id 与读 value 同事务同查询）
- credential_kind = 'repo_credentials'
- user_id = 仓库归属者（host_user_id，0=路径 A/bot 身份）
- executor_context = 'token_resolve'，operation = 'repo_bot_hit'

### 4. MySQL 场景专项

| 项 | 措施 |
|---|---|
| DDL 方言 | 标准语法（ALTER ADD COLUMN + TEXT DEFAULT ''），翻译层已验证同形态（UC 建表先例） |
| 回填定位键 | 复合键 WHERE（非 rowid——N1 教训直接规避） |
| 大表回填 | MySQL 下 rc 行数 = 托管仓库数（量级 <1万，单事务可行；分批按 500 LIMIT 循环预留） |
| datetime | DEFAULT datetime('now') 翻译层已处理（既有 DDL 先例）；新代码应用层时间戳 |
| 生产实弹 | 本地 SQLite 全流程实弹 + translate_sql 断言 MySQL 形态；**生产 MySQL 回填在部署后首启自动执行**（幂等可重跑），部署 checklist 加一条"观察启动日志 backfill 行数" |

## 二、验证面

- 实弹：明文/无 id 存量行 → 启动回填 → id/加密双就位 → 命中审计行落库
  （credential_id=UUID + repo_key 冗余）→ 幂等重跑
- translate_sql 断言：3 条新 DDL/DML 的 MySQL 形态
- 宽域回归：repos 链/token 链/守卫族（UC23① 白名单加 backfill 文件已就位）

## 三、提交切分

单 commit：DDL 迁移 + 回填步骤 4 + 接线 + 测试 + 文档（原子变更，
拆开会有中间态不可运行）。
