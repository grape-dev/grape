# 领域库视图 / 仓库忽略规则的复用备注

> 适用：库按域拆分后（`grape_mgmt_db` / `grape_runtime_db` / `grape_insight_db`）+ 迁移链条改动。
> 来源：2026-11 迁移对齐（PR #1244）实操踩坑，配套阅读 `migration-parity-assessment.md`。

## 一、跨库可更新视图是「建视图时的列快照」

### 事实
非 owner 库里的他域表是视图（例如 `grape_mgmt_db.agent_tasks` → `grape_runtime_db.agent_tasks`，
`grape_mgmt_db.runtime_credentials` → runtime 同名表）。视图用**显式列清单**定义：

```sql
SELECT `grape_runtime_db`.`agent_tasks`.`id` AS `id`, ... FROM `grape_runtime_db`.`agent_tasks`
```

列清单在建视图那一刻固定。**owner 基表新增列、或新建表，视图都不会自动跟上**，表现为：
- 读新列 → `1054 Unknown column`
- 写新表 → `1146 Table ... doesn't exist`
- 而迁移链条全绿、盖章表也有记录 → 典型的"假绿"

### 处置
基表做了「加列 / 建表 / 改列类型」之后，必须同步视图：

```bash
DEVOPS_DB_HOST=<host> DEVOPS_DB_USER=root DEVOPS_DB_PASSWORD=<pwd> \
  python3 scripts/sync_domain_views.py --dry-run   # 先看会重建哪些
DEVOPS_DB_HOST=<host> DEVOPS_DB_USER=root DEVOPS_DB_PASSWORD=<pwd> \
  python3 scripts/sync_domain_views.py             # 幂等 CREATE OR REPLACE
```

脚本行为：以 owner（`grape_runtime_db`）基表的实际列为准生成视图；目标库若同名对象是**基表**则拒绝覆盖。
`SYNC_TABLES` 是显式清单，新增 owner 表要加进去。

### 验证 SQL
```sql
-- 视图列数应等于基表列数，且含新列
SELECT table_schema, COUNT(*) cols, SUM(column_name='trace_context') has_col
  FROM information_schema.columns
 WHERE table_name='agent_tasks' AND table_schema LIKE 'grape\_%db' GROUP BY 1;
-- 新表的归属：owner 基表 + 其余库视图
SELECT table_schema, table_type FROM information_schema.tables
 WHERE table_name='grape_t_credential_usage_log' AND table_schema LIKE 'grape\_%db';
```

### 迁移 fn 的写法约束
非 owner 库执行 `ALTER TABLE <视图>` 必然 `1346/1347`。所以迁移里补列前先判定类型并跳过：

```python
row = c.execute("SELECT TABLE_TYPE FROM information_schema.tables "
                "WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME='agent_tasks'").fetchone()
if row and str(row[0]).upper() == "VIEW":
    log.info("...归属 owner 基表，跳过 ALTER"); return
```

否则每次启动都刷 `_ensure_column MANUAL REVIEW`，日志被噪声淹没。

## 二、盖章表是共享视图：三库都盖章 ≠ 三库都执行

`grape_t_schema_migrations` 在 `grape_mgmt_db` 是基表，在 runtime / insight 是**指向 mgmt 的视图**。
因此：

- 只有抢到迁移锁的那个进程真正执行 fn（并写入 mgmt 基表）；
- 其余角色启动时通过视图看到"已执行"，直接跳过。

推论：**不能只看盖章表判断落地**。要断言 `information_schema.tables/columns`。
本文档配套的「阶段一」里，两张凭据表就只建在了 runtime（拿锁进程连的库），mgmt/insight 靠视图转发才对。

## 三、`.gitignore` 会静默吞掉同类新脚本

仓库 `.gitignore` 有一族按前缀忽略的规则：

```
scripts/batch_*.py  scripts/clear_*.py  scripts/find_*.py  scripts/phase1_*.py
scripts/sync_*.py   scripts/trigger_*.py  scripts/upgrade_*.py  scripts/verify_*.py
scripts/check-*-*.py
```

`scripts/sync_domain_views.py` 因此**没被 commit**，而迁移已经在用它的产出——本地一切正常，
远程/新环境缺工具。已加放行：

```
!scripts/sync_domain_views.py
```

### 新增脚本的自检
```bash
git check-ignore -v scripts/<新文件>.py   # 有输出=被忽略，需放行或改名
git status --ignored --short | grep '^!!' # 看还有没有该入库却被忽略的
git ls-files --others --exclude-standard  # 真正的未跟踪清单（应为空）
```
提交前顺手确认新文件真的进了 `git diff --cached --stat`，别只看 `git commit` 成功。

## 四、新增迁移 / 加列的检查清单（照着走）

1. 号段：先在 `grape_t_schema_migrations` 查重，**线上已盖章的号绝不复用**；与其它分支撞号时让出号段
   （本轮：base 占 `202611120001-5`，本线阶段一用 `202611140001-4`，阶段二预留给 `202611150001+`）。
2. 幂等：fn 里一律 `IF NOT EXISTS` / 先查后加；`_ensure_column` 带 DEFAULT 的 TEXT 列会有 MySQL 限制，
   用 `TEXT`（无默认）或 `allow_copy=True`；对盖章后补效果的场景加 `strict=True`。
3. 视图：涉及 owner 基表的加列/建表 → 跟跑 `scripts/sync_domain_views.py`。
4. 干跑：隔离 schema（`CREATE DATABASE grape_mig_probe`）用本仓 DDL 初始化后，每条迁移跑**两遍**，
   第二遍不得报错、不得产生新告警；断言 `SHOW COLUMNS` 效果。
5. 备份：`mysqldump --single-transaction` 三库后再重启落地。
6. 上线后验收：三库盖章、对象存在（`information_schema`）、业务读写往返一次、
   启动日志 `MANUAL REVIEW` / `1146` 均为 0。
7. **脱敏自查（提交前必跑）**：新增脚本/文档的 docstring 与示例同样属于审查面——QA 会按
   「内网 IP / 绝对路径」直接判不符合（PR #1269 第 1 轮即因此打回）：

   ```bash
   # 私有网段用 [.] 写法规避"文档自身被扫描命中"；PAT/口令前缀按需补
   git diff --name-only upstream/<target>...HEAD | xargs -r grep -nE \
     '192[.]168[.]|10[.][0-9]+[.][0-9]+[.]|172[.](1[6-9]|2[0-9]|3[01])[.]|120[.]46[.]|1[.]92[.]|[/]home[/]|[/]opt[/]|<厂商PAT前缀>|<登录口令模式>' || echo "clean"
   ```

   命中即换占位符：`<db-host>` / `<user>` / `<password>` / `<backup-dir>` / `<timestamp>`。
   注意「举个用法示例」也算暴露面；文档要保留运维细节时，只留代码里已有的标识符
   （库表名、迁移号段、校验 SQL），不留环境相关值。
