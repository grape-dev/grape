# D105 主键 UUID 化设计——账号/仓库/凭据体系（2026-11-09）

## 一、需求与口径（用户裁定）

账号体系、仓库体系、凭据体系的表主键改为 **UUID 字符串**形式；**存量行主键值原样保留**（整型值字符串化为 `"24"` 形态，不重编号）。主键直接 VARCHAR/TEXT 化（非锚点列方案）——无双读过渡态、无退役债。

## 二、可行性结论（已论证）

- 直接改字符串主键**可行**：全列同型（TEXT），老值 `"24"` 与新值 UUID 共存无混型。
- "替换主键但老值保持整型"不可行（同列混型，排序/比较/分页语义崩）——已排除。
- 凭据域先例：rc（D99 `id VARCHAR(256) PK`）、sc（D100 `VARCHAR(36) PK`）、uc（N1 `id TEXT PK`）——**凭据域已全部 UUID 化，本设计只剩账号域**。

## 三、四个硬性前提（每批的通用检查单）

1. **传参规范化**：SQLite TEXT 亲和自动兼容数字参数；**MySQL 侧 `varchar=N` 全表扫**——所有按 id 传参点必须 `str()` 化（代码清扫主体）。
2. **排序语义**：`ORDER BY id` 在 UUID 下失效——SQLite 用 `rowid`（保留自增），MySQL 补 `created_at`/seq 列；清扫所有 `ORDER BY id`。
3. **整型强转点清扫**：`int(id)`、API schema `id: int` 等逐点改字符串语义。
4. **写入路径**：AUTOINCREMENT/lastrowid 退役，应用层 `uuid4()` 生成、插入即所得。

## 四、分批计划

| 批 | 范围 | 状态 |
|---|---|---|
| 第一批（本稿实施） | `external_identities`（引用面最小：仅 persistence 一处 `int()` 强转 + 3 处 raw INSERT） | ✅ 202611090001 |
| 第二批 | `grape_accounts`（user_id 引用 1770 处，分域清扫） | ✅ 202611090002（UuidPkMigration 收敛）；引用列 TEXT 归一收口见 d105b-account-fk-user-id-text-design.md（202611091000） |
| 第三批 | `organization`/`org_member`（org_id 436 处） | 待排期 |
| 不动 | managed_repos（业务键主键本就非整型）、user_repos（复合键，user_id 随第二批） | — |

## 五、第一批实施记录（external_identities）

- 新装库 DDL：`id TEXT PRIMARY KEY`（db_setup.py）。
- 存量库迁移 `202611090001 rebuild-external-identities-uuid-pk`（safe）：
  SQLite 重建表（`CAST(id AS TEXT)` 原值保留、UNIQUE 双键原样）；MySQL `ALTER MODIFY id VARCHAR(36)`（隐式转字符串）；幂等（sqlite_master/information_schema 形态检测）。
- 写入路径：ORM `ExternalIdentityModel.id` String(36) PK、`create()/get_or_create()` 应用层 uuid4；3 处 raw INSERT（grape_account_db L1006/1137/1747）补 `(id, ...)` 列与 uuid4 参数。
- 跨表引用 `grape_user_id` 本批不动（值语义不变，`str()` 规范化随第二批做）。

## 六、边界（不做的）

- 不回填审计/日志表里的历史整型 id（双形态等值匹配天然兼容，纯数字字符串 vs 整型参数在 TEXT 亲和/MySQL 隐式转换下均可命中）。
- 不做 MySQL 生产切换演练（本分支私有迁移未上生产；上线前按 deploy-checklist 走）。

## 七、第二批方案细化——grape_accounts.id UUID 化（侦察稿 2026-11-09，未实施）

### 7.1 侦察结论（src 实测，2026-11-09 @ 2d67591b4）

- `user_id`/`grape_user_id` 引用共 **1342 处**（src *.py，非设计稿早先口径 1770——该数含测试与重复计数）。
- 分布重灾区：user/db.py 155、auth/grape_account_db.py 129、session_manager.py 92、api/routes/grape_auth.py 71、api/issues_center.py 68、api/db_setup.py 51、api/deps.py 44。
- 携带 user-id 列的表共 **29 张**（CREATE TABLE 全库扫描）：issue、chat_conversations、agent_tasks、api_tokens、session_api_tokens、audit_logs、exchange_codes、user_credentials、user_repos、org_member、external_identities(grape_user_id)、scene_market(source_user_id)、managed_repos(host_user_id)、community_* 3 张、*_likes 3 张、obs_llm_usage(+archive)、feishu_onboard_task、api_latency_log、credential_usage_log、unified_user_mapping、user_feedback 等。
- `user_id: int` 函数签名约 **200 处**（jwt_utils/auth_token/grape_account_db/admin/org_db/deps 等全链）。
- `int(user_id)` 强转点 **15 处**（persistence/repositories.py 9、grape_account_db.py 2、handler_async.py 1、engine_select.py 1 等）。
- `ORDER BY id` 全库 60+ 处，但**涉及 grape_accounts 的仅 user/db.py:114（user_accounts 死表，已 deprecated）**——主表无 ORDER BY id 时序依赖，第二级排序风险集中在各业务表自身 id（本批不动那些表）。
- `lastrowid` 依赖：grape_account_db.py:99/138（create 账号返回自增 id）、admin/db.py:410——第二批必须改 uuid4 插入即所得。

### 7.2 分层方案（推荐 B：主键先行、引用列放宽、调用方渐进）

- **B1 主键层**：grape_accounts.id → TEXT PK；新装 DDL 直改；存量迁移 202611090002（SQLite 重建表 CAST 保值 / MySQL MODIFY VARCHAR(36)），复用第一批形态。
- **B2 引用列层**：29 张表 user_id 列统一 `ALTER MODIFY VARCHAR(36)`（MySQL）/ SQLite 不动（INTEGER 亲和列可存 TEXT，纯数字自动转回 int，读侧 str() 规范化即可）。列放宽对存量数据零损。
- **B3 调用方层（渐进，不阻塞上线）**：
  - 必改：15 处 `int(user_id)` 强转、grape_account_db.py:99/138 与 admin/db.py:410 的 lastrowid→uuid4、jwt_utils create_access_token 的 sub 传参、admin/routes.py 路径参数 `user_id: int` → str。
  - 可渐进：约 200 处 `user_id: int` 类型注解仅是提示，SQLite/MySQL 双侧隐式转换下不炸——按域分批把注解改 str，配合 mypy/mypy 插件或 grep 守卫收敛。
- **B4 外延**：org_member.user_id 随本批放宽列型；organization.id 整型主键化仍留第三批。

### 7.3 风险与判据

- JWT sub 载荷从 int 变 str：老 token（int sub）与新校验代码兼容性——`get_status_and_token_version` 按值等值查询，SQLite TEXT 亲和 + MySQL 隐式转换下双形态可命中（与第一批边界同款判据），但必须加行为锁用例（老 int token 能过鉴权）。
- 读侧规范化点必须收敛为单一出口（get_current_user 一处 str() 化），禁止散点 str()——否则 1342 处引用永远清不完。
- 前端/管理页对 user_id 的数字假设（排序、编辑 URL）需过一遍；本批先实弹扫 admin 用户管理页。
- 不做的：不重编号、不回填审计日志、不动 29 张业务表自身的主键。

### 7.4 悬置决策（已拍板 2026-11-09）

- **决策 1**：B3 调用方层清理节奏 → **已拍板：一次性清完**（约 200 签名 + 15 强转，大 MR；用户裁定）。
- **决策 2**：user_id 引用列列型 → **已拍板：VARCHAR(36)**，与 grape_t_system_credentials 对齐。
- **决策 3**（v7，2026-11-09 追加拍板）：主键生成从 uuid4 切 **UUIDv7**（RFC 9562；业界口径"v7 用于数据库主键，v4 用于其他"）——成本≈0（生成点已收敛）、恢复 ORDER BY id 近似时序、新老共存无碍（存量值原样保留，格式同为 36 字符标准 UUID）。**范围纪律**：仅账号/仓库/凭据域 DB 主键走 v7；session token/trace id/临时文件名等非 DB 标识符保持 v4（不可预测性优先）。

### 7.5 第二批实施记录（2026-11-09）

- 迁移 `202611090002 uuid-pk-grape-accounts-and-user-id-cols`（safe，幂等）：grape_accounts.id 整型→字符串（SQLite 重建表 CAST 保值 / MySQL MODIFY VARCHAR(36)）+ 29 张引用表 user_id 族列 MySQL INTEGER→VARCHAR(36)（保留 NOT NULL；SQLite 列型动态不动）。已在验证实例 DB 盖章，老账号 '1'/'2'… 零丢失。
- 生成点：`src/id_gen.py uuid7_str()`（stdlib uuid7 优先 / uuid6 库 / uuid4 兜底）；grape_account_db（注册×2+绑定 raw INSERT×3）、persistence/repositories（identity/repo_credential/system_credential）、user/db（user_credentials、grape_t_repo_credentials×2）、credentials_backfill_n1、credential_usage_log、admin/db、db_setup 回填，共 11 处切 v7。
- 调用方清扫：63 文件——user_id 族签名 int→str 全量、15 处 int() 强转清零、lastrowid 3 处退役、JWT sub 三入口（get_current_user/auth refresh/grape_auth 绑定流）str 化、engine_select 稳定取模改 crc32（int 取模对 UUID 不再成立）。
- 行为锁：tests/test_d105_batch2_account_uuid_pk.py 6 用例（新装 TEXT PK / 迁移保值 / 幂等 / uuid36+v7 version 位 / 老 int sub 过鉴权 / 引用列 UUID 往返）。定向回归 70 passed 2 xfailed。
- 已知说明：迁移后 Issue #343 一次性 token_version+1（既有设计）使存量用户需重新登录一次，非本批引入。

## 八、集群场景（2026-11-09 核查）

**已有设施覆盖的部分（无需新增代码）**
- 多实例迁移互斥：本迁移挂在 init_database 迁移链内，MySQL 侧走既有 per-database GET_LOCK（专用连接、分片超时、断连告警，db_setup.py:3804+）——两实例同时启动只有一个执行 DDL。
- 锁不可用降级：GET_LOCK 拿不到时无锁继续 + 幂等兜底——两实例真并发时，后跑方 information_schema 查到 id 已 varchar 即零写入跳过；引用列放宽逐列判定，半迁移状态幂等。
- SQLite 不集群（单实例），无并发迁移面。

**滚动升级窗口的真实风险（操作守则，非代码问题）**
- 窗口内"老代码实例 × 新代码产生的 UUID 行"：老代码残留的 `int(user_id)` 强转对 UUID 值抛 ValueError，相关接口 500。新代码已把强转清零，但**窗口内老实例跑的仍是旧代码**。
- 数值字符串化存量行（"24"）对老代码无影响（int("24") 正常）——风险只落在窗口期新建的 UUID 行上。
- 守则：本迁移上 MySQL 集群时，二选一——① 快速滚动（分钟级窗口，期间新建账号/绑定/凭据的少数接口可能报错，可接受则无视）；② 严格窗口（停写 → 全实例停 → 任一实例启动完成迁移 → 全量起新代码）。grape 当前验证/生产规模推荐 ①。
- MySQL ALTER COPY 期间表级锁写（秒级/表），窗口内另一实例的写请求排队等待——低峰执行即可，无需 pt-osc（单表到百万行级再评估）。

**第三批（organization）沿用同一 UuidPkMigration 声明式入口，集群语义自动继承。**

**实施检查清单（外部评审复核采纳，2026-11-09）**
- MySQL 形态检测必须含 `CHARACTER_MAXIMUM_LENGTH>=36`：只查 DATA_TYPE='varchar' 会把"历史手改的 varchar(20)"误判已迁移 → UUID 静默截断（非严格模式截断比报错更危险）。已实装（uuid_pk_migration.py `_run_mysql`）。
- COPY 逐表阻塞写入：放宽前逐表 COUNT 预检打日志（已实装），行数异常大的表提前暴露；执行前逐表确认数据量，避免多表串行阻塞叠加；必要时按业务模块分批执行而非一次串行跑完。
- DATA_TYPE 只给类型名、COLUMN_TYPE 才含长度——按检测意图选字段：判"是否字符串"用 DATA_TYPE 够；判"是否 varchar(36)"须 COLUMN_TYPE/CHARACTER_MAXIMUM_LENGTH。
- **自省模式升级（2026-11-09 追加）**：sqlite_create_sql/copy_sql 可省略——缺省时 `_auto_sqlite_plan` 从 sqlite_master DDL 原文改造（INTEGER PRIMARY KEY AUTOINCREMENT→TEXT PRIMARY KEY，约束/默认值/UNIQUE 零丢失）+ PRAGMA 列清单自动生成拷贝（仅 id 套 CAST）+ 显式索引原文重建（sqlite_autoindex 系跳过）。第二批已切自省模式（声明缩至 4 行），第一批保留声明式作对照——两种模式均有行为锁。**第三批 organization 接入只需：`UuidPkMigration(pk_table="organization", uid_widen_cols=(...)).run()` + register_migration 一行。**
- **测试顺序污染裁决口径（实锤）**：DB 切换型测试文件（test_d105_uuid/batch2）的 in-test/teardown 模块 reload 会使同进程后续文件（如 test_grape_auth）collection 期捕获的函数对象失配 → no-such-table 型失败——**第一批文件单独复现（uuid→grape_auth 13 failed），非第二批/自省模式引入**。裁决按仓库既有口径：逐文件隔离绿（batch2 6 绿、uuid+admin+grape_auth 54 绿、batch2→admin/credentials 绿）+ 全量失败集 diff 对账；组合顺序污染不作为第二批回归信号。
- **rowid 别名回归修复（202611090003，实弹回归抓漏）**：宽回归中 test_tasks_lifecycle 17 errors 实锤——`unified_user_mapping.user_id INTEGER PRIMARY KEY` 是 **rowid 别名列，SQLite 强类型**（"SQLite 引用列不动"对普通列成立、对 rowid 别名不成立，UUID 插入 datatype mismatch）。修复：UuidPkMigration 支持此类列的 TEXT PK 重建（全库 grep 仅此一例命中），迁移 202611090003 幂等（已盖章库不重跑）；实弹 data/sessions.db 重建后 25 行零丢失、'1'/'11'/'12' 字符串化保值。行为锁 tests/test_d105_rowid_alias_uid_pk.py。残余 16 failed 均为 405（confirm-done 路由 2026-09-16 迁移后测试未跟上，先于 D105 的既有欠账）。宽回归其余失败集：api_routes 99 failed 为 401 鉴权族既有失败（修复前后失败集完全一致，0 例 datatype mismatch）；issue_537 等组合失败为顺序污染（隔离即绿）。

## 九、关联扫雷与 MySQL 实弹记录（2026-11-09）

### 9.1 issue ident 族全链路（D102-O 同族收口，实弹 IKTVZQ 驱动）

gitee 仓库 issue 编号为字母 ident，建需求后 create_task 同步触发流水线被
int(issue_num) 炸（payload 构造处，D102-O 当时只收了 git-repo 传输层）。
三路并行扫雷收口：后端 9 文件（int/isdigit 假设，PR/milestone/comment 按
D102-O 裁定保持 int）、SQL 层（迁移 202611090004：issue_number 混型归一
TEXT——DDL 原文改造整表重建，修复三实现缺陷；ORDER BY LENGTH 双键；
_query_tasks 二级排序键 id 补齐 5 处）、前端（chat.js ensure-issue/
observability 时间线 ident 保形，版本 bump）。结构守卫：int(issue_num)
与裸 ORDER BY issue_number 均 CI 即红（test_string_ident_pipeline_trigger_guard）。

### 9.2 MySQL 实弹 P0（外部评审发现，已修）

information_schema 探针 TABLE_NAME 不带引号 → error 1054 → 202611090002 永远
"失败跳过不盖章" → 32 张引用表 user_id 留 INT → 写 UUID 全 500（Incorrect
integer value）。修复：探针表名加引号（uuid_pk_migration.py 两处）。
根因：tests/conftest.py 强制 sqlite 后端，MySQL 路径零 CI 覆盖——此类问题
只能实弹暴露。挂账：MySQL 后端可选测试开关（env 驱动）。
形态偏离挂账：fresh MySQL 的 grape_accounts.id 为 VARCHAR(191)
（mysql_compat TEXT→VARCHAR(191) 翻译），功能无碍（阈值 >=36），后续归一。

### 9.3 MySQL 部署实弹复验清单（head 8f4992900+）

1. init_database 后日志出现 202611090002/202611090003 盖章且 0 pending
2. information_schema 实查：32 表 user_id 族列 = varchar(36)
3. POST /api/settings/api-keys → 200（修复前 500）
4. 注册新账号 → grape_accounts.id 为 UUIDv7 字符串
