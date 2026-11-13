# 部署门槛清单：现网 MySQL（grape 库）接入本分支操作说明（2026-11-09，G2 自动化版）

> 状态：**硬门槛已自动化**——门槛执行收口到 `scripts/deploy_mysql_gate.py` 单命令编排。
> 人工只剩两个动作：演练后**查看校验结论**、生产执行时**显式传 `--allow-prod` 并观察日志**。
> 实测基线（2026-11-09，information_schema 实查）：MySQL 侧 `grape` 库存在且曾有真实流量
> （账号/表/时间戳画像略——现网数据细节不入公开仓；
> events 0 行、issue 0 行、最近 18 天无新写入）。
> schema 形态：**全旧**——grape_accounts.id 及全部 user_id 族 INT、issue_number INT、
> 无 grape_t_schema_migrations 表（分支迁移体系从未在此库执行）、patrol 表不存在。

## 使用方法

```bash
# ① 演练（默认，绝不触碰生产 grape 库）：备份 → 重建演练库 → 恢复 → 迁移链 → 自动校验
python3 scripts/deploy_mysql_gate.py

# ② 生产应用（显式确认门禁）：仅当 ① 全绿后才允许；随后自动 SQL 冒烟
python3 scripts/deploy_mysql_gate.py --allow-prod

# ③ 冒烟附带 API 闭环（health 必过；新用户闭环当前仅占位，网络失败标 SKIP）
python3 scripts/deploy_mysql_gate.py --allow-prod --smoke-api http://127.0.0.1:8787
```

前置：本机 MySQL 管理凭据按运维规范注入（勿在文档携带免密姿态）；演练库 `grape_upgrade_sim` 由脚本每次 drop/create 重建（幂等）；
备份落在 `$GRAPE_GATE_BACKUP_DIR/mysql-gate-backup/`（默认 `./mysql-gate-backup/`，脚本校验非空且含 CREATE TABLE）。

> **G1 对接占位**：迁移链入口按候选顺序探测——① `--migration-cmd`（或 env
> `GRAPE_GATE_MIGRATION_CMD`，env `GRAPE_GATE_TARGET_DB` 传目标库）；②
> `src.api.db_setup.run_pending_migrations`（要求应用连接已指向目标 MySQL 库，
> 脚本用"目标库出现 grape_t_schema_migrations"做红线校验，未落到目标库即 fail-closed 中止）。
> G1 定稿后替换候选 ② 为 MySQL 连接注入接口即可。

## 演练验收状态（2026-11-09）

- [x] 门槛二演练已实测完成：本机残留库 4 轮 + 真实 dev 库（实例与规模数据见部署配置，不入文档）各一轮，全链迁移零失败、id 保值、幂等 0 pending
- [x] 演练已代码化：GRAPE_GATE_TEST=1（门控）+ GRAPE_GATE_REAL_DATA=1（真实数据验收）一条命令可重复，见 tests/test_mysql_gate_migration_chain.py
- [ ] 生产库执行：待 --allow-prod 授权窗口（工具就绪）

## 门槛一：迁移链 MySQL 分支（G1）→ 由脚本在演练库自动执行

原四条人工清单（db_setup 第 4 号迁移归一、uuid_pk 重建链、账号域外键 TEXT 化、盖章表落 legacy 戳）
改为：**G1 代码完成后，脚本对演练库 `grape_upgrade_sim` 跑完整迁移链（全部已注册版本，直至最新 202611091100）**。
脚本红线：迁移未落到目标 MySQL 库（盖章表缺失）→ 立即中止并保留演练库现场。

## 门槛二：存量数据迁移演练 → 脚本自动校验（任一红即 exit 非零、保留现场）

- [x] `mysqldump --single-transaction grape` 全量备份（脚本阶段 1，校验非空 + CREATE TABLE）
- [x] 演练库恢复备份后跑完整迁移链（脚本阶段 2）
- [x] 自动校验（脚本内置，人工无需再手工对账）：
  - 逐表行数对账（源 vs 演练，逐表全量比对）
  - id 保值抽查：`grape_accounts` '1'/'2'/'3' 在场且形态为 str
  - 不变式：全库 `user_id` 族列必须为 VARCHAR/CHAR（information_schema 实查）
- [x] 校验全绿才允许对生产执行（`--allow-prod` 是唯一入口）；执行窗口内停写

## 门槛三：SQL 语义修复随分支带入（F3 批次）

不变——仍按 F3 进度跟踪（`||` 拼接翻译、UTC 时钟基线已修复；wal_checkpoint 白名单、
cid:int→str 进行中）。本脚本不覆盖此项；迁移完成前禁止现网运行代码含坏 SQL。

## 门槛四：部署后验证 → 脚本阶段 4 自动冒烟 + 人工观察

- [x] SQL 级自检（脚本自动）：关键表（grape_accounts / grape_t_schema_migrations）存在、盖章表记录非空
- [ ] API 闭环（`--smoke-api`）：health 必过；新用户（uuid id）注册→建 issue→流水线→凭据写入断言为占位，待接口定稿补全；网络失败标 SKIP 不判红
- [ ] 人工观察：老账号（'1'/'2'/'3'）登录、历史数据可见、归属判定正确；迁移日志零 1064/零 fallback warning

## 明确不做

- 不在演练校验全绿前传 `--allow-prod`（脚本无此参数时只打 plan 并退出 0）
- 不对生产库做在线 ALTER 试验（脚本只在演练库做破坏性 drop/create）
- 不用任何环境变量/配置绕过脚本把新分支指向生产 MySQL

## 结构化日志

全程 JSON 行输出到 stdout，并落盘 `mysql-gate-backup/logs/<ts>.log`；
关键事件：`backup_ok / shadow_ok / shadow_preserved / plan_only / apply_ok / sql_smoke_ok / gate_pass / gate_fail`。
