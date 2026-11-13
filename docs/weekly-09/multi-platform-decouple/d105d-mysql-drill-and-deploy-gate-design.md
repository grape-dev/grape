# D105d MySQL 演练与部署门禁——需求 + 设计（2026-11-09）

> 前置：D105b（账号域外键 TEXT 化）/ D105c（全系统深扫全修）。本文记录「现网 MySQL 实存」确认后的演练与门禁自动化设计与实施。
> 配套：deploy-checklist-mysql-live-prod.md（部署硬门槛）、scripts/deploy_mysql_gate.py（门禁脚本）。

## 一、需求（用户裁定）

1. 确认现网 MySQL 实存且有历史真实流量后，**部署门槛自动化**：备份 → 影子库演练+自动校验 → 生产应用（--allow-prod 显式授权）→ 冒烟，一条命令完成。
2. **基于测试环境数据库建立 grape-drill-db（自建演练库名，见部署配置） 测试库，本地完成 MySQL 迁移演练**——演练只允许在自建草稿库进行，严禁打坏测试/生产库（边界：源库只读 dump，草稿库每轮 drop 重建）。
3. 演练发现的问题当轮修掉（实弹驱动，不允许"记下批再说"的致命项）。

## 二、演练实施记录（grape-drill-db（自建演练库名，见部署配置），四轮递进）

| 轮 | 动作 | 结果 |
|---|------|------|
| 1 | 源库 dump（81 表/3 账号）→ 恢复 → 跑全迁移链 | 全链 17 迁移盖章；**暴露 9 张表建表失败**（TEXT DEFAULT 1101）+ G1 前的 fresh 50 列漏建 |
| 2 | D105b DDL 改 VARCHAR(36) 后重跑 | 9 表中 2 表修复；**暴露 7 张表整段 DDL 为 SQLite 风格**（TEXT DEFAULT ''/datetime('now') 默认）——建表失败与 user_id 无关，属翻译层根因 |
| 3 | 行级清扫 28 行残留 TEXT 声明 | 仍有 7 表失败 → 定位到翻译层 `DEFAULT ('')` 包装 + datetime('now') 默认值问题 |
| 4 | G4 翻译层根修（TEXT 字面量 DEFAULT 剥除） | 目标：零建表失败（验收中/已完成见 ledger） |

**演练确认的核心结论**：MySQL 8 下 TEXT/MEDIUMTEXT 列不接受字面量 DEFAULT（1101）；表达式 DEFAULT 需括号且 datetime('now') 必须先经翻译链。SQLite 开发全链无法暴露这一类问题——**MySQL 演练必须成为标准流程**。

## 三、部署门禁设计（G1-G3 实施）

```
python scripts/deploy_mysql_gate.py [--allow-prod] [--target-host H ...]
  ① backup   mysqldump --single-transaction → 时间戳文件 + 完整性校验
  ② shadow   drop/create grape_upgrade_sim → 恢复备份 → 迁移链 → 自动校验：
             逐表行数对账 / id 保值（'1''2''3' str 形态）/ 引用列全 VARCHAR 不变式
             任一红 → exit 非零，保留现场，绝不触碰生产
  ③ apply    仅 --allow-prod；幂等（grape_t_schema_migrations 盖章去重）
  ④ smoke    关键表/盖章表 SQL 自检；--smoke-api 可选 API 闭环
```

支撑代码：
- **G1**：启动迁移链 MySQL 分支补全（typeof/sqlite_master/PRAGMA 三类点的跳过/等价 ALTER/分派前置；fresh 演练暴露 50 列漏建 → legacy 首跑上下文修复）
- **G3**：`run_migrations_for_mysql(host,user,password,database)` 连接注入——门禁主路径直连目标库（G2 真机演练实锤：占位入口走应用默认连接落 SQLite，fail-closed 中止是正确安全行为）；`_mysql_target_swap` 上下文换绑全局连接 + env 逐键还原
- **G4**：翻译层根修——TEXT 列字面量 DEFAULT 剥除（一处修全部建表失败）

## 四、场景用例

| # | 场景 | 前置 | 动作 | 预期 | 用例 |
|---|------|------|------|------|------|
| UC62 | 门禁脚本阶段编排 | 模拟 mysqldump/mysql | 跑 backup/shadow/apply/smoke | 校验失败中止保现场；--allow-prod 缺省只打 plan | test_deploy_mysql_gate |
| UC63 | 存量 MySQL 库全链迁移演练 | 真实 dump 恢复的 81 表库 | 跑迁移链 | 零 1064/零 MANUAL REVIEW/零建表失败；id 保值 str；引用列全 VARCHAR；二次 0 pending | test_mysql_gate_migration_chain（GRAPE_GATE_TEST=1 真机） |
| UC64 | 新装 MySQL 库 DDL 正确性 | 空库 | init_database | 全部表建成零 1101/1064（含 TEXT-DEFAULT 剥除后的 7 表） | test_mysql_gate_migration_chain + test_mysql_dialect_compat |

## 五、验收与边界

- 验收：影子演练全绿（行数对账/保值/不变式/幂等）+ SQLite 迁移四件套回归 + 门禁单测
- 边界：organization/org_id 第三批不动；生产库执行必须人工 --allow-prod；dev RDS（内网地址见 configs/db-backend.json，勿入文档）口令到位后补真实 dev 库演练轮


## 六、真实 dev 库演练实施记录（2026-11-09，59 RDS）

- 源：dev RDS 实例（地址/端口/库名/账号一律见部署配置，不入文档）全量 mysqldump（--set-gtid-purged=OFF，RDS 无 SUPER 必需）
- 演练库：同实例 `grape-drill-db（自建演练库名，见部署配置）`（drop/重建，源库只读）
- 迁移链：全链完成零失败零 1064；123 表终态；40 个真实账号 id 字符串化保形（'24'/'200' 零丢失）；issue ident/binding 票/user_id 族全 VARCHAR；盖章 31 条至 202611091200；二次启动 0 pending
- 已代码化：test_mysql_gate_migration_chain 扩展 GRAPE_GATE_REAL_DATA=1 门控用例（行数保值/str 形态/INT 残留清扫断言），对 59 实跑 5 passed——真实 dev 库演练从此可一条命令重复
- 已知非致命告警（挂账随小批）：login_whitelist.github_username 唯一索引遇存量字面 'NULL' 值 1062 跳过
- 生产（<PROD_DB_HOST> grape_system_db）：本沙箱网络不可达（与 sandbox-N 实测一致），接入走 node-a/node-b + 本清单
