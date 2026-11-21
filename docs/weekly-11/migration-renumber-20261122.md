# 迁移撞号处置：20261122 号段换号（A 方案）

> 现场：部署启动 8 条 ERROR「迁移版本号撞号（同号不同名）」，两台一致，本次 02:49 起出现。
> 结论：采用 **A（改号）**。B（手工执行 + 盖章）只解一时，下一分支仍会撞，且会把
> 「已执行」的判断写死在我们无法控制的号上。

## 1. 换号映射（已落代码）

| 旧号 | 迁移 | 新号 |
|---|---|---|
| 202611170009 | chat-conversations-status-index | **202611220001** |
| 202611200002 | plugin-create-seven-tables-b1 | **202611220002**（并折入分类种子） |
| 202611200003 | issue2423-env-extra-encrypt-backfill | **202611220003** |
| 202611200004 | issue2423-create-ov-credential-cells | **202611220004** |
| 202611210004 | session-token-key-column-width | **202611220005** |

选 20261122 号段的依据：全仓 `src/` 与两个目标库台账此前均无该号段（只读核对见下）；
换号后按版本排序在既有迁移之后执行 —— 这 5 条都是「建表 / 加列 / 回填 / 建索引」，
全部幂等，晚执行不改变语义。

顺带加固：`plugin-create-seven-tables-b1` 的执行体折入了分类种子（INSERT OR IGNORE）。
原因：目标库上 `202611200005 plugin-market-category-seed-b1` 早于建表执行且已盖章，
只补建表会留下「表有了、种子没有」的半状态。

## 2. 部署前只读核对（在每台目标库执行，必须全部为 0 行）

```sql
SELECT version, name, applied_at
FROM grape_t_schema_migrations
WHERE version IN ('202611220001','202611220002','202611220003','202611220004','202611220005');
```

若有行 → 说明该号段在目标库已被占用，停止部署，改号段（不要硬上）。

## 3. 部署步骤

```
1) 两台同步本次提交（db_setup.py + 两处测试字面量 + 本文档）
2) 两台各自重启四单元（mgmt 先起，迁移在 mgmt 启动时执行）
3) 启动日志核对：
   · 不再出现「迁移版本号撞号（同号不同名）」ERROR
   · 出现 grape_t_schema_migrations 的 pending 计数包含这 5 条
   · 结束后 0 pending
```

## 4. 验收（对象级，逐条可执行）

```sql
-- ① 插件表应 7 张
SELECT COUNT(*) FROM information_schema.tables
 WHERE table_schema=DATABASE() AND table_name IN (
   'grape_t_plugin','grape_t_plugin_category','grape_t_plugin_market',
   'grape_t_plugin_version','grape_t_plugin_verified_uris',
   'grape_t_plugin_install','grape_t_plugin_likes');   -- 期望 7

-- ①b 分类种子非空
SELECT COUNT(*) FROM grape_t_plugin_category;          -- 期望 > 0

-- ② ov 凭据格子列存在
SELECT COUNT(*) FROM information_schema.columns
 WHERE table_schema=DATABASE() AND table_name='grape_t_ov_credential_cells'
   AND column_name IN ('user_id','org_id','base_url','vault_ref','created_by');  -- 期望 5

-- ③ env_extra 加密回填：明文残留应为 0
SELECT COUNT(*) FROM agent_tasks
 WHERE env_extra IS NOT NULL AND env_extra <> '' AND env_extra NOT LIKE '%:%:%'; -- 期望 0（按实现口径复核）

-- ④ chat_conversations 索引
SELECT index_name FROM information_schema.statistics
 WHERE table_schema=DATABASE() AND table_name='chat_conversations';             -- 期望含 idx_chat_conv_status

-- ⑤ session_key 列宽/NOT NULL
SELECT column_name, column_type, is_nullable FROM information_schema.columns
 WHERE table_schema=DATABASE() AND table_name='session_api_tokens' AND column_name='session_key';
```

## 5. 回滚

```
代码回滚：revert 本次提交（号回到旧号）——但旧号在目标库已被他人占用，
         回滚后这 5 条迁移会重新变成「撞号跳过」，不是回到可用状态。
数据回滚：这 5 条不改既有列语义（除 session_key 的 NOT NULL 对齐），
         逐条逆操作见各自迁移 fn 的注释；插件表如需撤，按 7 表 DROP 的固定顺序。
结论：回滚意义有限，出问题优先前向修复（再换号 / 补 repair 迁移）。
```

## 6. 为什么不用 B

```
· B 只解决本次：把 5 条执行完 + 给一个新名字盖章；下一分支若再用这些号，台账里
  「同号不同名」的判断依旧成立，问题复现。
· B 需要手工写 DDL/DML 并手工盖章，两类风险都在人手上（漏执行 / 盖章名与代码不一致，
  之后 checksum 漂移、reconcile 判定都会混）。
· A 是框架给的修复指引（ERROR 原文就写着「renumber 到未占用号」），可复现、可评审、可推上游。
```