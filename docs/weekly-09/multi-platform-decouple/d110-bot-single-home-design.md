# D110 bot 单一归属：落到仓库平台行，退役 grape_t_bot_accounts

2026-11-14。配套文档：`d109-platform-object-model-design.md`（被本稿修订的是 §4 / §6 / §18.2 / §18.3 / §18.7 / §262 职责表）。
状态：**已实施**（step1~3 已落地：迁移 013/014/016 + 读链统一 + 表与代码删除 + 前端重建；无 DROP 迁移）；拍板口径见 §9。

## 1. 复盘：原设计错在哪（先认账，再给修法）

事实（实查，HEAD 39b3e2f3e）：

1. `grape_t_bot_accounts` 全仓只有 5 处引用——`platform/capabilities.py`、`platform/admin_sync.py`、
   `platform/migration.py`、`mgmt/admin/bot_account_routes.py`、`mgmt/admin/repo_platform_routes.py`；
   `src/domains/runtime/` 下 0 处。`token_enc` 无任何运行时读取者。
2. 运行时取 bot token 的实际链是三级：`RepoBotToken`（仓库级凭据）→ `GlobalBotToken`
   （meta `global_bot_<p>_token`）→ `UserToken`（用户 PAT），见 `src/kernel/token_resolver.py:1062-1067`。
3. 运行时取 bot 身份的实际链是：`managed_repos.bot_username` → 全局 username 来源/平台卡
   （`src/domains/mgmt/user/repo_manager.py:1121-1149`、`src/domains/runtime/pipeline/event_pipeline.py:857-880`）。
4. 管理面 bot 页写入的是 `grape_t_bot_accounts`；`is_default` 与 `get_default()`（`repositories_platform_object.py:424-431`）
   零调用者；`mark_token_status` / `touch_last_used` 零调用者。

三个设计错误（不是实现缺陷）：

- **归属错**：bot 的家选成了"平台级账号目录"，但 bot 在系统里是"平台默认身份 + 仓库级覆盖"。
  目录不在取 token 的任何一级里，写进去等于写进死胡同。
- **粒度错**：设计成"一平台多账号 + is_default"，而三级链的第 2 级只要求"该平台一个默认身份"。
  多账号选号从未实现，`is_default` 没有消费者 → 多账号是纸面能力，还额外造出"只能加不能撤"
  （`upsert_account` 只在 `is_default=True` 分支清别人，`False` 不清本行）。
- **消费错位**：写入口（管理面 bot 页）与读入口（meta / 仓库级凭据）指向两个存储；
  同时 82502db1f 把凭证页的全局 bot 输入删掉、口头把"bot 凭据"交给平台菜单 →
  真源从此无 UI，新存储无读者。
- **连带假阳性**：`capabilities.py:123` 把 `supports_bot_token` 派生为 `bool(bot_accounts)`，
  于是"表里有行"= 支持 bot，与 token 是否可用无关；`bot_username` 字段恒返回 `""`（`:126`）。

结论：修法不是给 `bot_accounts` 补列/补消费者，而是**把这个对象挪到平台行上、删掉目录表**。

## 2. 目标模型

`grape_t_repo_platforms` = 平台定义 + 该平台唯一的 bot（一个平台一个 bot）。

新增 3 列（全部 `NOT NULL DEFAULT ''`）：

| 列 | 语义 | 写者 |
|---|---|---|
| `bot_token_enc` | 该平台全局 bot PAT **密文**（空 = 无全局 bot，托管回落用户 PAT） | 管理面 / 迁移 |
| `bot_token_status` | `ok` \| `expired` \| `unknown`（巡检写入，替代 meta `global_<p>_bot_token_status`） | 巡检 |
| `bot_checked_at` | 最近一次探活时间 | 巡检 |

复用 1 列：`bot_username`（已存在）语义从"配置声明的期望值"升级为"该平台实际 bot 账号名"。

删除：`grape_t_bot_accounts` 表 + `BotAccountModel` + `BotAccountRepo` + `/bot-accounts` 路由。

不变：仓库级覆盖链（`grape_t_repo_credentials(repo_key,'bot')`、`managed_repos.bot_username`）；
三级取 token 链的级别与顺序；`webhook_url` 归属（部署级设置，不进模型）。

不建：仓库↔账号绑定表（一仓库一 bot 由"平台一个 bot + 仓库级覆盖唯一键"天然保证）；
平台级 webhook secret 表（d109 §18.7 维持）；多账号/池/选号。

## 3. 字段映射（`configs/global-bot-token.example.json`；中间版本名 `platform-bots.example.json` 兼容）

| 模板字段 | 新归属 | 模板字段处置 |
|---|---|---|
| `gitcode_api_token` | `repo_platforms(gitcode).bot_token_enc` | 保留为迁移种子 |
| `github_bot_token` | `repo_platforms(github).bot_token_enc` | 同上 |
| `github_bot_username` | `repo_platforms(github).bot_username` | 同上 |
| `bot_usernames` | 不需要（一平台一 bot；名单 = 平台行 `bot_username`） | 删除 |
| `gitcode_oauth_client_id/secret` | `oauth_channels.client_id_enc/client_secret_enc`（现状，不动） | 保留为迁移种子 |
| `github_oauth_client_id/secret` | 同上 | 保留为迁移种子 |
| `*_oauth_redirect_uri` | 渠道卡 `redirect_uri`（登录不读 meta） | 删除 |
| `webhook_url` | 系统设置（`server-config.json` > meta，不进模型） | 删除 |
| `gitcode_webhook_token` | 不需要（验签按仓库 `managed_repos.webhook_token_field`） | 删除 |

判定依据（逐字段实查）：真在用 8 个（两个 bot token、两个 bot username 来源、四个 OAuth 凭据、webhook_url）；
已死 4 个（两个 redirect_uri、`bot_usernames` 池、`gitcode_webhook_token`）。

## 4. 读写契约

读 bot token（四级，前级命中即返回）：

1. `grape_t_repo_credentials(repo_key, 'bot')` / `managed_repos.{token_storage_field}` 存量列（现状）
2. `repo_platforms.bot_token_enc`（新）
3. meta `global_bot_<p>_token`（兼容期）
4. 种子文件 `configs/global-bot-token.json`（兼容名 `platform-bots.json` 亦认；仅当迁移标记未置位）

读 bot 身份：`managed_repos.bot_username` → `repo_platforms.bot_username` → 平台卡 `bot_username` → meta（兼容期）。

写（收敛到聚合根）：

- 新端点 `PUT /admin/repo-platforms/{provider}/bot`：body `{username, token?, status?}`；
  `token` 空 = 不改；落库前 `encrypt`；响应永不含 token。
  `DELETE /admin/repo-platforms/{provider}/bot`：清 token（username 保留，用于身份展示）。
- 旧 `PUT /admin/config` 的 bot 字段保留为兼容壳，内部转写平台行；保留 swap / path-B 升级触发。
- 巡检写 `repo_platforms.bot_token_status/bot_checked_at`；meta 状态键保留只读一段。

导入写保护与播种（防"下次导入把 token 清空"）：

- `_PROTECTED`（`repositories_platform_object.py:32`）追加 `bot_token_enc`、`bot_username`、
  `bot_token_status`、`bot_checked_at`。
- 播种规则：写保护列**仅当当前值为空**时接受配置文件/平台卡的值，之后导入永不覆盖。
  理由：`_upsert_config_row` 的 insert 分支同样跳过 `_PROTECTED`，没有播种规则则新库 `bot_username` 恒空。

## 5. 迁移批次（版本号 + 方言）

| 版本 | 名称 | kind | 内容 |
|---|---|---|---|
| 202611170013 | add-platform-bot-columns | safe | 平台表加 3 列；SQLite `ALTER TABLE ... ADD COLUMN`，MySQL 逐条 ALTER（信息模式判列；TEXT 不带 DEFAULT） |
| 202611170014 | merge-bot-into-platforms | backfill | meta `global_bot_<p>_token` 加密 → 平台行；`bot_accounts` 兜底合并；导出 `data/bot_accounts-backup-<ts>.json`；置 `bot_platform_migrated=1`；幂等（已有密文则跳过） |
| 202611170017 | seed-platform-bot-from-config-file | backfill | 读种子文件（新 `platform_bot[]` 数组 + 旧扁平键，别名 `platform_type`/`bit_token` 兼容），按 provider 加密写入平台行空位；幂等；文件缺失/平台行未导入则跳过 |
| 202611170016 | widen-repo-credential-derived-from | backfill | MySQL `derived_from VARCHAR(32)` → `VARCHAR(36)`：该列存账号 id，`uuid7_str()` 实测 36 字符，32 宽在 strict 模式写入即 1406 |

回退点：013 可回退（空列无害）；014 不改 meta、只写平台行（meta 仍是第 3 级来源）。

**不写 DROP 迁移（2026-11-14 用户裁定）**：`grape_t_bot_accounts` 是本分支设计期自带的表，
真实发布面（arch-optimization / main 线）从未建过，为它留一条永久 cleanup 迁移属于垃圾代码。
删除动作只发生在代码层（模型/仓储/路由/DDL），开发/验证库里残留的空表手工清即可：

    mysql -uroot <db> -e "DROP TABLE IF EXISTS grape_t_bot_accounts"
    sqlite3 <db> "DROP TABLE IF EXISTS grape_t_bot_accounts"

014 里保留"若存在 bot_accounts 则兜底取默认行"的分支，仅为跑过本分支旧代码的库服务；
表不存在时按不存在处理（实测冷启动即此路径）。

## 6. 代码触点（实施时逐项核销）

模型/DDL/迁移：`kernel/persistence/models_platform_object.py`（3 列 + 注释）、`api/platform_object_ddl.py`、
`api/db_setup.py`（4 条迁移）、`kernel/persistence/repositories_platform_object.py`（`_PROTECTED` + 平台 bot 读写）。

读口：`kernel/token_resolver.py:889-905`（`GlobalBotToken` → 改名 `PlatformBotToken` 读平台行）、
`mgmt/user/db.py:2439-2450`（两个 token 入口）、`db.py:5540-5548`（`get_global_bot_token_for`）、
`db.py:2452-2487`（两个 username 入口）、`db.py:3325-3331`（bot 名单）、
`mgmt/user/repo_manager.py:722-780`（托管配置）、`runtime/engine/bot_token_health.py:62-160`（读 + 写状态）、
`runtime/pipeline/event_pipeline.py:857-905`（`BOT_USERNAME`）。

写口：`mgmt/admin/repo_platform_routes.py`（新增 bot 子资源）、`mgmt/admin/bot_account_routes.py`（删除）、
`mgmt/admin/config_store.py:192-280`（改落平台行 + 保留 swap/upgrade）。

退役面：`platform/admin_sync.py:44-56`、`platform/migration.py:119-172`、`platform/capabilities.py:123/135-145`、
`BotAccountModel`/`BotAccountRepo`、`repo_platform_routes.py:37` 的 `bot_accounts` 摘要、
前端 `views/AdminPlatforms.vue:75-120/230-280`（多行 + 设为默认 → 单行 bot）。

## 7. 风险与最坏伤害半径

- **R1（最高）轮换断事件**：path A 托管把全局 bot token 同时写成仓库 webhook 验签 token
  （`mgmt/routes/repos.py:976/986`）。写平台行 `bot_token_enc` 时必须仍触发 `global_bot_token_swap`
  （`admin/config_store.py:432-519`，取值改读平台行），否则所有老仓库 webhook 立刻 403。
- **R2 导入覆盖**：`_PROTECTED` 漏一列 + 无播种规则 → token 被清空，全部写操作降级用户 PAT 且不报错。
- **R3 残留表**：不写 DROP 迁移；开发/验证库手工清空表（命令见 §5），发布面从未建过该表。
- **R4 方言/守卫**：模型、DDL、迁移三处列定义必须逐字一致（s78 schema-drift 守卫逐列对账）。
- **R5 数据合并歧义**：`bot_accounts` 多账号时只能保留一个（规则见 §9-1），其余丢弃并记录清单。

## 8. 验收口径

单测：读链四级优先级；写保护 + 播种规则；`/bot` 端点响应不含 token；删表后下游降级不报错；
`supports_bot_token` 与"平台行有 token 或 username"一致。

门禁：SQLite + MySQL 冷启动建表（3 列存在、类型正确）→ 013~016 各跑两遍幂等 → schema-drift 守卫绿。

实弹（验证实例）：平台页写 token → 库里为密文 → `token_resolver` 命中级 2 → 托管建仓走 path A →
轮换 token 后老仓库 webhook token 同步刷新 → 老托管仓事件仍 200。

## 9. 待拍板（3 项）

1. 合并优先级：meta 优先（推荐，它是当前运行时真源）还是 `bot_accounts` 优先？
2. `bot_usernames` 池：直接丢弃（推荐，池读链已退役）还是保留多账号（与"一平台一 bot"矛盾）？
3. 平台行 `bot_username` 被管理面改过之后，平台卡文件的值是否允许再次覆盖？（推荐：只在空值时播种，之后不覆盖。）

## 10. d109 文档修订清单（实施时同步）

§4 表清单（删 `grape_t_bot_accounts` 行、`repo_platforms` 加 3 列）、§6「bot：一个对象 + 一处绑定」（262-380 重写）、
§18.2 实体表（bot 行改平台行）、§18.3 关系图（删 bot_accounts 节点）、§18.7（补"不建绑定表"）、§20.4、
以及 §4 的"平台表不含凭据/账号"纪律——改为"凭据列不进配置文件，加密列由管理面/迁移写"（与 `oauth_channels` 同构）。

## 11. 实施记录（2026-11-14）

- 迁移：`202611170013`（加 3 列，双方言）、`202611170014`（meta→平台行播种，密文直搬）、
  `202611170016`（`derived_from` 32→36）。**无 DROP 迁移**：该表从未进入发布面，残留表手工清。
- 读链：`get_global_bot_token_for` / `get_global_gitcode_bot_token` / `get_global_github_bot_token` /
  `get_global_bot_usernames(_for)` / `get_global_github_bot_username` 全部改为"平台行 → meta → 文件"。
- 写链：`config_store.save_config` 的 bot 字段、`admin_sync`、管理面新端点
  `PUT|DELETE /admin/repo-platforms/{provider}/bot` 三处统一写平台行（加密、响应不含 token）。
- 巡检：`bot_token_health` 同时写平台行 `bot_token_status/bot_checked_at`。
- 删除：`BotAccountModel` / `BotAccountRepo` / `bot_account_routes.py` / `BotAccountBody` /
  DDL 的 bot 表与部分唯一索引及其 MySQL 生成列等价实现 / 前端"多账号 + 设为默认"面板；
  `credential_caps` 兼容垫片（`supports_bot_token` 等四键）一并删除。
- 拍板：池 `bot_usernames` 直接丢弃；平台卡 `bot_username` 只在空值时播种；合并以 meta 为准（它仍是迁移期第 3 级来源）。
- 验证：SQLite / MySQL 双冷启动（列齐、`derived_from varchar(36)`、013~016 盖章、幂等）；
  用例 `tests/mgmt/test_d110_platform_bot.py` 5 条 + D109 相关套件 77 passed，
  仅剩两条基线同红（`test_user_repo_platform_link_lifecycle`、`test_s57_fail_task_uses_unified_global_token`）。

## 12. 场景用例（UC-D110-01 ~ 08；与需求文档 FR-32 逐条对应）

| UC | 场景 | 期望 | 证据 |
|---|---|---|---|
| UC-D110-01 | 冷启动建库（SQLite / MySQL 各一次） | 平台表有 `bot_username/bot_token_enc/bot_token_status/bot_checked_at`；`grape_t_bot_accounts` 不存在；`derived_from` = varchar(36)/TEXT 且 013、014、016 盖章、重跑幂等 | 双库实跑（本机）+ `test_platform_ddl_has_bot_columns` |
| UC-D110-02 | 某平台既有 meta 全局 bot，又有平台行 bot；仓库无仓库级凭据 | 取 token 命中**平台行**（不是 meta）；平台行清空后回落 meta；两者都空再回落到文件（仅迁移标记未置位时） | `test_token_read_priority_platform_then_meta`；冷启动后 `_platform_bot_token` 实读明文 |
| UC-D110-03 | 管理面改 bot 名字后，配置导入再跑一次；以及首次导入 | 首次导入时 `bot_username` 由平台卡播种；管理面改名后再次导入**不被覆盖**；`enabled=0` 不被配置复位 | `test_config_import_seeds_bot_username_only_when_empty`、`test_enabled_is_not_revived_by_config` |
| UC-D110-04 | 平台 bot token 轮换（管理面端点 / 旧 config 保存） | 旧值非空且新值不同 → 触发 `global_bot_token_swap`（path A 仓库 webhook 验签 token 同步刷新）；首次配置（旧值空）与同值**不触发** | `test_rotation_triggers_swap_only_when_token_changes`；swap 本体沿用既有 R21.1 用例 |
| UC-D110-05 | 平台有 bot、平台卡也声明了 bot 名；同时请求 bot 名单 | 名单 = 平台行 username ∪ 卡 username（去重）；`bot_usernames` 池不再参与 | `get_global_bot_usernames` 实码 + 名单相关回归 |
| UC-D110-06 | 巡检对平台 bot 探活返回 401 / 200 | 401 → 平台行 `bot_token_status=expired` 且 `bot_checked_at` 刷新（meta 状态键兼容双写）；200 → 清空 expired | `_mark_platform_bot_status` 路径 + `test_bot_token_health` 回归 |
| UC-D110-07 | 平台详情/能力视图取值 | `bot_accounts` 摘要来自平台行（一行；有 token → active，无 token → disabled）；`bot_username` 回显平台行；无行时 `supports_bot_token=false` | `test_d109_admin_api_contract`（摘要不含 token）+ 能力视图用例 |
| UC-D110-08 | 退役面巡检：全仓搜 `BotAccountRepo` / `BotAccountModel` / 对 `grape_t_bot_accounts` 的 SQL | 除 `db_setup` 迁移里的"存在才取默认行"分支外零命中（注释提及不算，SQL 引用算） | `test_retired_catalog_has_no_runtime_readers` |

附：本批同车修复的两条评审项
- **UC-86（P1-4）**：`link()` 的方言 upsert 后，同 session 内已加载的实例必须过期——`test_user_repo_platform_link_lifecycle` 由红转绿（原先 1 failed/10 passed → 11 passed）。
- **UC-87（守卫盲区）**：`LIKE '{provider}:%'` 内嵌形态已由第二守卫覆盖，唯一豁免 `_path_b_prefix_clause` 两处并按数量登记；红绿实证=塞一行即红、还原即绿。

## 13. 种子文件格式（2026-11-14 用户定稿）

`configs/global-bot-token.json`（`.gitignore`；示例 `configs/global-bot-token.example.json`；
`platform-bots.json` 是中间版本名，只作兼容读、一个发布周期后可删——名字承载的是"全局 bot 凭据"，改它不给运维任何收益）
支持两种写法，解析器只有一处 `src/kernel/config.py::read_bot_seed_file()`：

```json
{
  "platform_bot": [
    {"provider": "gitcode", "bot_name": "grape-bot", "bot_token": "..."},
    {"provider": "gitee",   "bot_name": "gitee-bot", "bot_token": "..."}
  ],
  "gitcode_api_token": "",
  "github_bot_token": "",
  "github_bot_username": ""
}
```

- 别名容错：`provider` ← `platform_type|platform`；`bot_name` ← `username`；`bot_token` ← `token|bit_token`。
- 数组与扁平键同字段冲突时**数组优先**。
- 用途边界：只作初始化种子（迁移 202611170017 加密写平台行，只填空位）；运行时不读；管理面写入优先于文件。

## 14. 展示层契约：命名、图标与「配对设置」下线（2026-11-15 用户裁定）

### 14.1 命名

- 渠道/平台卡的显示名一律走 `t('provider_name.<provider>')`（zh/en 两语言包）；配置与 meta 里的 `name`
  只作最后一档兜底。详情页头曾直接渲染 `current.name`（配置原名），中文名因此漏进英文界面——已改
  `channelName(current)`，列表与详情共用同一取数口径。
- **huawei 定名**：zh `华为` / en `Huawei`（原标"华为云"）。依据：该渠道的 `token_url` 是
  `oauth-login.cloud.huawei.com`（华为统一 OAuth），承载的是"华为账号登录"，不是华为云产品线；
  配置文件 `configs/oauth-providers.json` 与线上 meta 的 `name` 同步为中性名 `Huawei`（中文只放语言包，
  避免中文硬编码漏到英文界面）。

### 14.2 图标

- 资产路径：仓库 `src/api/frontend/shell/img/<provider>.svg`，服务路径 `/static/img/<provider>.svg`。
  管理台两页（登录渠道 / 代码托管平台）共用同一个 `logoFor()` 口径：列表 20px、详情 28px。
- **独立 svg 文件必须自带 `xmlns="http://www.w3.org/2000/svg"` 与 `viewBox`**。`shell/js/oauth-buttons.js`
  里的品牌图标是内联 SVG（HTML 内联不需要 xmlns），原样拷成文件后浏览器拒绝解码 → 破图
  （2026-11-15 实锤：gitee / huawei 两枚坏，带 xmlns 的 gitlab / gitcode / github 正常）。
  `gitee` / `huawei` 现直接复用登录页同一枚图形（红圆 + G / 红盘 + 字标），不是手绘近似。
- 没有品牌图的 provider 回退纯文字，不画近似图形冒充 logo（避免"图标错位"的假品牌）。
- 图标内容变更必须带 `?v=` 查询串（当前 `?v=3`）并 bump `admin-view.js`，否则浏览器继续复用旧（破图）缓存。

### 14.3 配对设置下线（用户拍板）

- 原"可接入的代码托管平台"分节整段移除：三段解释（同域天然可接入 / 跨名需显式配对 / 未配置配对不可接入）
  与"选平台 + 保存 / 删除"操作一并删除。裁定原话：**讲不清的规则不如不显示，也不要支持用户设置**。
- 随之删除：`linkEdit` 状态、`selfLink`/`crossLinks`/`platformOptions`/`isSelfDomain`/`linksText`/`saveLink`/`delLink`
  辅助函数、列表副标题的配对字样（只留 provider）、页面侧 `/repo-platforms` 目录拉取，以及只为该段存在的
  语言包键（can_drive / can_drive_hint / self_pill / login_only / same_domain_warn / save_link / del_link /
  pick_platform / src_config / src_manual / self_domain_short / drive_label / need_platform）。
- **不变的部分（必须保留）**：`grape_t_channel_repo_link` 仍是运行时关系真源；`configs/channel-repo-links.json`
  仍是导入源；同域隐式配对语义不变；后端 PUT/DELETE `/api/admin/channel-repo-links/*` 端点保留（供脚本/导入），
  只是管理台不再提供人工增删入口。

### 14.4 版本位纪律（本轮末值）

改语言包 → 四页 `__I18N_CACHE_VER__/I18N.init` + `LOCALE_FINGERPRINT`；改管理台视图 → `admin-view.js?v`；
改图标 → `?v=`。本轮终态：index `49` / admin `20261114-19` / register·forgot `41`；`admin-view.js?v=41`；
指纹 `47296b080cba037d`；图标 `?v=3`。

### 14.5 文档与源码不得含真实凭据（2026-11-15 用户要求）

- 判据：**写进仓库的示例值只能是占位形态**（`<REDACTED_...>` / `<your_...>` / `CHANGE_ME` / `xxx` /
  `${ENV}` / `example`），真实 token、webhook secret、口令一律不进设计稿、需求稿与源码。
- 历史清理：`docs/weekly-06/oauth-provider-plugin-design.md` 曾把线上 gitcode bot token 与 webhook secret
  原文写在"回退后的配置文件示例"里（`docs/weekly-08/issue-372-design.md` 有一处引用），2026-11-15 已替换为
  `<REDACTED_REAL_TOKEN>` / `<REDACTED_WEBHOOK_SECRET>`。
- 守卫：`tests/api/test_no_secrets_in_docs_and_src.py` 扫 `docs/**/*.md`、`src/**/*.py|js|vue`，
  按"值像不像真凭据"判定（已知前缀 ghp_/glpat-/github_pat_ 直接命中；`*_token`/`*_secret`/`password`
  等键的值是 16+ 位随机串即命中；占位形态与纯小写下划线键名放行）。
- **注意**：脱敏只能清理当前树，git 历史里仍有原文——真凭据必须**轮换**才算闭环（本轮已提示 gitcode bot token）。

## 15. 场景用例（UC-D110-09 ~ 12）

| UC | 场景 | 期望 | 证据 |
|---|---|---|---|
| UC-D110-09 | 管理台加载品牌图标（登录渠道页 / 代码托管平台页） | 每个 `LOGO_KNOWN` provider 的 svg 都能被浏览器当独立图片解码（xmlns + viewBox 齐备，含 `?v=`）；缺图 provider 回退文字不破图 | `tests/api/test_admin_brand_icons.py::test_every_logo_provider_has_renderable_svg` + 真实 Chrome `new Image()` 五枚全 ok |
| UC-D110-10 | 中/英界面看渠道卡名称；huawei 渠道 | 名称取语言包（zh 华为 / en Huawei），配置 name 只兜底；详情页头等价于列表（不再出现配置原名） | `test_channel_detail_header_uses_localized_name`、`test_provider_names_i18n_covers_every_configured_channel`；线上 `/static/locales/{zh,en}/admin.json` 实测 |
| UC-D110-11 | 有人想在渠道页加回"配对设置" | 反向守卫直接红：管理台 vue 不得出现 `channel-repo-links` 写入，也不得再引用配对分节文案键 | `test_channel_page_offers_no_pairing_settings` |
| UC-D110-12 | 改语言包/图标/视图 | 缓存版本位同批 bump，两张 ratchet 全绿（避免老用户持旧缓存看到破图或旧口径） | `tests/api/test_i18n_locale_cache_ratchet.py` + `test_frontend_cache_version_ratchet.py`（各 3 条） |
| UC-D110-13 | 有人把真实 token / secret / 口令写进设计稿、需求稿或源码 | 扫描守卫直接红；示例只允许占位形态（`<REDACTED_...>` / `<your_...>` / CHANGE_ME / ${ENV}）；纯小写下划线键名与占位值放行 | `tests/api/test_no_secrets_in_docs_and_src.py`（探针自测：塞真值必红、写占位不红） |

## 16. 部署级平台可见性与「公网基址」口径（2026-11-15 用户裁定）

### 16.1 可见性语义：方案甲（真源=平台行 `enabled`，名单退役）

- 真源：**`grape_t_repo_platforms.enabled`**（行级、DB 权威、导入永不覆盖、管理面有开关 UI）。
  上一版的 `meta.vcs_platforms_hidden` 名单**已退役**：迁移 `202611170021` 把名单里的平台写成
  `enabled=0` 并删除该 meta 键（线上原值 `["gitlab"]`）。理由：名单没有管理面入口（只能改 meta/SQL），
  与行级开关语义重叠，两套并存只会让运维不知道该用哪个。
- 判据收敛：`src/domains/platform/visibility.py::is_platform_visible()` 读平台行 `enabled`，三个消费点共用——
  用户面平台目录（`GET /settings/vcs/platforms`）、`GET /repos/member` 的显式/合并两条路径、管理面平台列表。
- 用户面行为（严格语义）：
  - 目录不返回 `enabled=0` 的平台；
  - `GET /api/repos/member?provider=<disabled>` → 403 `PLATFORM_DISABLED`；
  - 不带 provider 的合并列表不发起 disabled 平台拉取。
  - **存量已关联**用户的该平台仓库同样不再返回：不做"保留显示"的折中——那会形成
    "看得到但配不了凭据（目录里没有该平台 → 没有 PAT/绑定入口，token 过期即死路）"的半可用态。
- 内网/脚本面：托管/建仓/bot/webhook 注册照旧可用，方言/host 仍从平台卡解析（`platform_card`
  的 `enabled_only` 过滤**保留**：disabled 平台不用表行覆盖卡）——关的是"用户面可见"，不是平台能力。
- 管理面：`GET /admin/repo-platforms` 照常列出，每项带 `disabled: true`。
- **关平台要做影响面预检**：`PUT /admin/repo-platforms/{p}/enabled {enabled:false}` 无 `?confirm=1`
  且影响面 > 0 → 409 `PLATFORM_DISABLE_NEEDS_CONFIRM` + 统计（受影响仓库数/用户数/托管数/PAT 用户数）。
- **顺序纪律（线上实跑抓到的 bug）**：403 判定必须排在「无绑定 → 返回 200 `_no_vcs_binding`」之前，
  否则无绑定的用户点 disabled 平台会收到"你没绑"，把"已关闭"解释错了。已修并补回归用例。
- **registry 哨兵（"全关变全开"的坑）**：`_descriptors_from_tables()` 旧实现把"过滤后为空"当成"表不可用"，
  返回 `None` → `platform_descriptors()` 回退平台卡 → 四个平台又全部出现。修法是把语义拆开：
  表里没有行 → `None`（允许回退卡片）；有行但全 disabled → `[]`（用户面空目录的**明确答案**）。
### 16.4 默认不展示：写在初始化代码（2026-11-15 用户裁定 B）

- 取舍：曾考虑在平台卡加 `"enabled": false`（借助 `PlatformCard` 字段 + 播种语义），被裁定否定——
  **卡片只描述能力，可见性归 DB 行**；默认值这种部署口径写死在初始化代码里更直白。
- 实现：`src/kernel/persistence/repositories_platform_object.py::DEFAULT_HIDDEN_PLATFORMS = {"gitlab"}`。
  生效点只有两处：
  1. **新建平台行**（`upsert_from_config` insert 分支）→ `enabled = 0 if provider in 集合 else 1`；
  2. **迁移 `202611170021`** → 对既有行补一次默认（`WHERE enabled=1`），同时把退役的
     `meta.vcs_platforms_hidden` 名单搬成 `enabled=0` 并删除该键。
- 不生效的地方（有意为之）：**导入更新**（`enabled` 属 `_PROTECTED`，配置永不覆盖）、
  **orphaned 恢复**（配置里又出现该平台 → 视为可用，`enabled=1`，避免"挪文件把已开的平台关掉"）。
- 因此：新环境部署开箱即得"gitlab 不展示"；既有实例升级时 021 补默认一次；之后一律以管理面开关为准。
- 运维界面：`PUT /admin/repo-platforms/{p}/enabled`，关闭需 `?confirm=1`（见 §16.1 影响面预检）。

### 16.2 webhook_url：只补注释，不改行为（用户裁定）

- 结论：**不合并** `webhook_url` 与 `server_base_url`。合并要动 6 个读取点、3 个自动探测落库点、
  历史列迁移、管理面字段与 i18n、两张 ratchet，换来的行为与现状一致（平台路径本来就由平台卡决定），
  属"改了没收益、还多一层部署风险"。
- 语义写明（代码注释 + 配置示例 + 管理面提示）：
  - `get_webhook_url()` 返回**公网基址**（历史键名），不是某平台回调全路径；
  - 平台路径一律由平台卡 `webhook_path` 追加（`/webhook/gitcode`、`/webhook/gitlab`…），
    `_webhook_url_for_provider()` 会**整体替换**调用方给的 path ——填 `https://x/webhook/gitcode`
    与填 `https://x` 等价；
  - 读取优先级：`configs/server-config.json` → `meta.webhook_url` → 空串（不兜底 localhost，
    缺失时托管注册/回调返回 422 + guidance）；
  - `get_server_base_url()` 是同一事实的另一键（优先 `server_base_url`，会把 `webhook_url` 剥后缀用）；
    线上两键当前一致（`https://grape-dev.topxtopx.com` / 同 + `/webhook/gitcode`）。
- 落点：`user/db.py::get_webhook_url` 与 `_webhook_url_for_provider` 的 docstring、
  `configs/server-config.example.json` 的 `_comment_server_base_url`、`configs/global-bot-token.example.json`
  的 `_fields.webhook_url`、管理面 `config.webhook_url_hint`（zh/en）。

### 16.3 场景用例

| UC | 场景 | 期望 | 证据 |
|---|---|---|---|
| UC-D110-14 | 用户面请求 disabled 平台（gitlab） | 目录不返回；`?provider=gitlab` → 403 `PLATFORM_DISABLED`；不带 provider 的合并列表不发起该平台拉取；管理面项 `disabled=true` | `tests/mgmt/test_platform_visibility.py`（显式 403 / 无绑定也 403 / 合并不拉取 / 判据） |
| UC-D110-18 | 全新部署 / 升级 / 恢复三种时机 | 新建平台行：gitlab 落 `enabled=0`、其余 1；迁移 021 对既有行补默认一次并退役 meta 名单；配置里删掉再加回（orphaned 恢复）→ `enabled=1`（视为可用）；管理面开过后再导入不被覆盖 | `test_default_hidden_platforms_listed_in_init_code`、`test_new_platform_row_is_seeded_with_default_visibility`、`test_migration_021_applies_initial_default_and_retires_meta_list`、`test_orphaned_soft_delete_and_restore_without_hash` |
| UC-D110-16 | 把某平台 `enabled` 关掉 | 用户面目录不再返回它；`?provider=` → 403 `PLATFORM_DISABLED`（无绑定也 403）；合并列表不拉取；管理面项带 `disabled`；无 `confirm=1` 且影响面 > 0 → 409 + 统计 | `tests/mgmt/test_platform_visibility.py`（判据/403 两条/合并不拉取/预检两条） |
| UC-D110-17 | 把**所有**平台都关掉 | 用户面目录为空，**不得**回退平台卡（旧实现"全关=全开"） | `test_registry_returns_empty_when_every_platform_is_disabled` + `test_registry_filters_disabled_rows_from_real_table`（对照：表无行才回退卡片） |
| UC-D110-15 | 配置 `webhook_url` 时写了平台路径 | 该路径被平台卡替换（同一基址对不同平台得到各自回调地址），配置里只应写基址 | `test_webhook_base_is_host_only_and_platform_path_comes_from_card`、`test_get_webhook_url_returns_configured_base_unchanged` |

## 17. 管理面交互契约：说明气泡、关闭确认与 `enabled` 的双语义（2026-11-15）

本节的每一条都来自当天在真实浏览器里量出来的问题，不是风格偏好。

### 17.1 字段说明气泡必须脱离容器裁切

- 现象：渠道页/平台页的 "?" 点开后**只显示上半截**，`elementFromPoint` 命中的是页面容器 ——
  看着像"被遮罩"。实测裁剪祖先是 `.ap-group { overflow: hidden }`（圆角裁切），
  而气泡是 `position: absolute; z-index: 50` 挂在行内。
- 契约：气泡一律 **Teleport 到 body + `position: fixed`**，位置按触发器按钮的
  `getBoundingClientRect()` 计算（下方放不下翻到上方、左右夹在视口内），
  `z-index: 3000`（高于页面浮层 2000），滚动/缩放时重算。
- 守卫：`tests/api/test_admin_help_popover.py`（禁 absolute、必须有 Teleport/fixed/重算监听）。

### 17.2 关闭平台的确认必须是页面内 Vue 弹层

- 现象两连：①第一版**没有确认**，开关直接 409 失败；②第二版用了原生 `window.confirm`，
  不进设计体系、带不了影响面数字；③第三版借 `AGModal`，但它的样式依赖设计令牌
  （`--glass-bg-strong/--s-5/--r-card`），而 admin 壳从未加载 `ag-ui.css` → 遮罩失去
  `position: fixed`，弹层**掉到页面外面**（真浏览器实测：mask 无 fixed、dialog 不在视口内）。
- 契约：确认弹层 = 页面内 Vue 模板 + `Teleport to="body"` + **自带定位样式**（`position: fixed;
  inset: 0` 遮罩、居中对话框、`z-index: 2000`），按钮用 ghost/danger 两态；文案含影响面数字
  （仓库数/用户数/托管数/PAT 用户数，来自后端 409 的 `impact`），确认后带 `?confirm=1` 重发，
  取消则把开关拨回。
- 后端配套：**关闭一律要确认**（影响面为 0 也 409），授权口径见 §16.1。
- 守卫：`test_platform_visibility.py::test_admin_page_shows_confirm_dialog_before_retrying`
  （断言页面内弹层、`position: fixed`/`inset: 0`、`confirm=1`，且**不得**出现 `window.confirm`）。

### 17.3 `enabled` 的双语义与文案分叉

- 登录渠道：`enabled` = **该渠道是否允许登录**。门禁在 `grape_auth.py:1342`
  （`is_channel_enabled()` → 403 `CHANNEL_DISABLED`）；已登录用户不受影响。
- 代码托管平台：`enabled` = **该平台是否在用户面显示**（§16）。关闭后用户面不可见，
  内网/脚本与托管链路仍可用。
- 两页原来共用 `field.enabled` 一条说明（"该对象是否启用"），在平台页已经失真；
  现按页面映射：`field.enabled_channel` / `field.enabled_platform`（zh/en 各一份，
  平台口径写明"用户面 + 内网仍可用"）。
- 守卫：`tests/api/test_admin_enabled_semantics.py`（两键都存在且文案不同、
  页面 docRow 各自映射、平台文案必须提到"用户面/内网/托管"）。

