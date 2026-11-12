# D106 凭据存储终态化第一批（F4）设计分片

- 日期：2026-11-11
- 状态：✅ 已实施（F4b 于 2026-11-11 在 r3 集成树 feat/weekly-10-yaw-dev-r3 重实现并本地 commit；
  第一批落地于本树演化形态——`_upsert_repo_credential` 助手复用、`update_managed_repo_bot_token`
  provider 改 parse_repo_key 解析、`clear_repo_bot_token` db 晚绑定、s80/s82 存量升级库模拟补列；
  第二批 gitcode/github 列退役另立项）
- 需求行：S128（requirements 场景用例表）
- 验收线（r3 集成树实测）：test_s128 9 用例全绿（三条凭据验收用例=冷启动/rc 单写/回填+置空+resolver）+
  test_s82 全绿 + L1 批 156/0 + 凭据关联宽批（59 文件）双侧失败集 diff 零新增红；
  预存红记档（与基线同红，非本批引入）：372_r14/r8 源码扫描审计族 11、_rebuild_webhook 卡驱动缺失 3、
  s63_swap/s63_guard 2——rebuild_webhook 族登记下一批修复

## 1. 问题与终态口径

S82 已把 managed_repos 8 平台凭据列写冻结、grape_t_repo_credentials（rc）设为唯一权威，
但列的「物理存在」仍在：冷启动建库仍自动补 gitee/gitlab 四列
（`_ensure_platform_storage_columns` 按卡派生列名建列），ORM 模型仍声明，
写路径 `_update_repo_bot_token`/hosting 流对卡平台仍走列写——「权威在 rc、
幽灵列还在」的双轨状态。

F4 第一批终态（用户/评审四项新增要求并入）：

1. **gitee/gitlab 四列撤出 DDL + 卡片留空**：卡 `token_storage_field`/
   `webhook_token_field` 显式 `""`（构建层显式空串语义，键缺席≠空串），
   ORM 模型撤列，冷启动零列；gitcode/github 保持派生列（归第二批）。
2. **写路径 rc 单写**：托管流对卡列空平台传归一逻辑键 `bot`/`webhook`
   （`repo_cred_write_keys` 归一）；`_update_repo_bot_token` 卡列空分支只写
   rc（'bot'+'webhook' 两行，wt=bot 对齐列时代语义）+ 基线列
   （host_user_id/webhook_secret/bot_token_status/webhook_id）。
3. **渐进收敛（评审①）**：写 rc 成功后**同一事务**置空 managed_repos 对应
   平台 token 列（每次轮换自动完成一条迁移）；只置空本次实际写过的 cred
   种类对应列（防误清未回填的另类历史值）；置空 SQL 精确限定
   `UPDATE managed_repos`——meta 存在同名全局键 `gitcode_webhook_token`，
   严禁误伤（UC79④ 行为锁）。
4. **存量列回填迁移**：`202611110001 backfill-gitee-gitlab-token-cols-to-rc`
   （safe，幂等只填空）：四列值（N3 密文/历史明文）→ 解密→加密归一回填 rc；
   列存在性守卫（冷启动库零命中）；物理列不 DROP（历史回落面保留）。
5. **rc 单表契约（评审③）**：每仓库（repo_key）每 cred_type 至多一行
   （UNIQUE 钉死）——写成 rc 表注释 + `configs/platforms/SCHEMA.md` 卡片
   schema 显式契约；**不加 cred_slot**（未来需要时单表轻迁移）。
6. **legacy 回落显式标记（评审④）**：RepoBotToken/_verify_single_repo_token
   列回落分支标记 LEGACY + 独立消费审计 `repo_bot_hit_legacy_column`
   （回填期旧列消费不留审计盲区）；退出 checklist（回填→置空→日志零命中
   →删分支+评估 DROP）写入代码注释与迁移 docstring。

## 2. 关键修法清单

| # | 文件 | 改动 |
|---|---|---|
| 1 | src/vcs/platform_card.py | `_explicit_or_default`：显式 `""` 尊重（无物理列），键缺席才派生；gitlab 代码默认卡置空 |
| 2 | configs/platforms/{gitee,gitlab}.json | 两卡 `token_storage_field`/`webhook_token_field` 置空 |
| 3 | configs/platforms/SCHEMA.md | 卡片 schema 凭据存储契约（新增） |
| 4 | src/persistence/models.py | ORM ManagedRepoModel 撤 gitee/gitlab 四列 |
| 5 | src/api/db_setup.py | `_ensure_platform_storage_columns` docstring 终态修订；rc DDL 表注释契约；backfill 列存在守卫；新迁移 202611110001 |
| 6 | src/user/db.py | `repo_cred_write_keys`；add_managed_repo 归一逻辑键路由 + rc upsert 两步法（修 S82 基线 3 红：裸 INSERT 撞 UNIQUE）+ 轮换置空；`_update_repo_bot_token` 卡列空分支 rc 单写+置空；`update_managed_repo_bot_token` 关联仓检测 rc 化；`_query_path_b_repos` rc 化（NOT EXISTS）；`clear_repo_bot_token` db 层统一清除入口 |
| 7 | src/token_resolver.py | RepoBotToken：卡列空跳过列回落；legacy 标记+独立审计+退出 checklist |
| 8 | src/runtime/bot_token_health.py | `_verify_single_repo_token` rc 优先，列回落仅 rc 空（legacy 命中告警） |
| 9 | src/admin/config_store.py | 全局换发步骤 3：N3 随机 nonce 密文等值恒不中（旧实现换发空转实锤）→ 解密比对；host_user_id 哨兵宽容形态（''/'0'/0/NULL） |
| 10 | src/api/routes/repos.py | 三处托管写点键归一；清除路径走 `clear_repo_bot_token`（rc 写入点白名单保持 S119 UC23① 收敛） |
| 11 | src/api/routes/settings.py | has_repo_bot_token：卡列空 → rc 判定 |
| 12 | tests | 新增 test_s128（9 用例：冷启动子进程/卡终态/ORM 撤列/rc 单写+upsert/回填+置空+resolver 仅凭 rc/不误清未写种类/meta 不误伤/_update_repo_bot_token 终态）；s82 3 红按 D99 归一 cred_type+解密口径重写；s64/s58/s63 涉改 7 处陈旧断言对齐现语义；s119 uc23④ 签名漂移（int→str）对齐；n4 审计 op 名同步 |

## 3. repo_key 侧顺手核（评审附带）

写路径产 key 全部走 `src/repo_key.py`（`to_repo_key`/`parse_repo_key`），
无手拼 `f"{provider}:{owner}/{repo}"` 直写 DB 路径（grep 守卫复查通过）。

## 4. 语义边界与不变量

- meta 运行时覆盖卡未显式声明两字段 → 派生列名（列平台语义，向后兼容），
  仅**发版默认卡**（configs/platforms + gitlab 代码兜底）置空。
- 有列平台（gitcode/github）：`_update_repo_bot_token` 保持列+rc 双写
  （列=历史回落面），add_managed_repo 仍 rc 单写（S82 冻结不变）。
- rc 写入点白名单不变：user/db.py、admin/config_store.py、api/db_setup.py、
  user/credentials_backfill_n1.py（S119 UC23① 守卫绿）。
- 置空只在「本次实际写过对应 cred 种类」时发生；未写种类历史列值保留
  （UC79 防误清锁）。

## 5. 第二批（挂账）

- gitcode/github 四列同款撤出（卡片留空+回填+置空收敛）。
- legacy 回落分支退出（checklist ③日志零命中后删分支）+ 评估 DROP COLUMN。
- s63_swap/s63_guard 两处预存量红（锁 pre-D99/N3 语义）：已于 G3 整改
  （swap 改 grape_t_repo_credentials 口径重写、guard 随 settings 契约开关收口转绿）。

## 6. 回滚预案（退出 checklist 附带项）

回滚到 weekly-10 基线（或任何 D106 之前的代码）时**不能只回退代码**——
D106 已把 gitee/gitlab 的 bot/webhook token 物理列在写入侧退役
（`_update_repo_bot_token` rc 单写+列置空、`add_managed_repo` rc 单写），
旧代码不认 `grape_t_repo_credentials` 表：

- **读侧凭据消失**：旧码读 `managed_repos.bot_gitee_token` /
  `bot_gitlab_token`（及 webhook 列），而新码已把这些列置空（值只活在
  rc，N3 密文）。回滚后仓库级 bot token 读侧直接为空。
- **恢复动作（必做）**：回滚后逐仓**重录 bot token**（管理面或
  `update_managed_repo_bot_token`），或从 rc 导出解密后回写旧列
  （rc 表仍在库中：`SELECT repo_key, cred_type, value FROM
  grape_t_repo_credentials`，`cred_type` 归一值 `bot`/`webhook`，value 为 N3
  密文需 `_decrypt_repo_secret`）。
- **schema 兼容性**：rc 表/迁移台账（`grape_t_schema_migrations` 的
  202611110001-3 盖章行）对旧码无害（旧码不读）；旧列未 DROP，无 DDL
  级不可逆操作。
- **退出 checklist 顺序**（正常退出，非回滚）：①回填（202611110001）
  →②置空（写点单写 rc）→③`repo_bot_hit_legacy_column` 日志零命中
  →④删 legacy 回落分支 + 评估 DROP COLUMN。回滚是 ③ 不成立时的应急路径。
