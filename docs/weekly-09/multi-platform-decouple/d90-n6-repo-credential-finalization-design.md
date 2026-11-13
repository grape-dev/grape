# D90-N6：仓库凭据存储终态化设计（2026-11-11 用户拍板）

**来源**：用户 2026-11-11 对仓库凭据存储终态化八条语义的完整拍板（D90-N2/N3
契约评审与 D99 rc 语义重设计的收口终态）。
**状态**：语义已拍板，代码收口由 F4b 于 2026-11-11 在 r3 集成树交付（第一批）；
验收用例=tests/test_s128_gitee_gitlab_token_col_retirement.py（9 用例全绿，§8 已实名）。

---

## §1 拍板记录总表（八条语义）

| # | 拍板语义 | 一句话口径 |
|---|---|---|
| ① | **rc 唯一权威 + 同事务置空** | grape_t_repo_credentials(repo_key, cred_type∈{bot,webhook}, value) 是仓库级凭据唯一权威；读 rc 优先无条件，managed_repos 平台 token 列=legacy 读回落；写 rc 单写，且**同事务内置空 managed_repos 对应列**——每次轮换自动完成一条迁移（渐进收敛） |
| ② | **每仓库每 cred_type 至多一条** | UNIQUE(repo_key, cred_type) 是显式契约，写进表注释与卡片 schema；不加 cred_slot；未来若需多 webhook，单表加 cred_slot DEFAULT 'default' 属轻迁移 |
| ③ | **gitee/gitlab 四列撤出 DDL** | bot_gitee_token/gitee_webhook_token/bot_gitlab_token/gitlab_webhook_token 四列撤出 DDL 与动态补列白名单；卡片 token_storage_field 留空=无托管列存储；gitcode/github 基线列保留为读回落，物理退役走后续批次 |
| ④ | **回填幂等只填空** | backfill_repo_credentials_from_columns 幂等：rc 有值不覆盖；列值已加密（no_plaintext 用例锁） |
| ⑤ | **legacy 回落显式标注 + 独立消费审计** | resolver 读回落分支显式标 legacy；独立消费审计统计列读命中；退出 checklist 见 §5 |
| ⑥ | **repo_key 单一产出/解析** | repo_key 由 src/repo_key.py 单一产出/解析（to_repo_key/parse_repo_key）；回填直搬同表字符串，join 字节一致 |
| ⑦ | **admin 隔离** | admin config_store 读写 meta 全局凭据（global_bot_*）与 bot_token_status 状态列，与本批 repo 级凭据无关；managed_repos 列与 meta 存在**同名键**（gitcode_webhook_token）——同名不同表，脚本严禁按名误伤 meta |
| ⑧ | **措辞口径限定** | "接新平台零 DDL"指新平台接入层；grape_t_repo_credentials 本身仍是唯一演进表；"调用方零分支"指经 resolver 接口 |

---

## §2 存储契约与实码锚点

### 表契约（写入表注释与 ORM 模型注释）

- 表结构：`id VARCHAR(256) PK / repo_key TEXT / cred_type TEXT / value TEXT /
  updated_at TEXT / UNIQUE(repo_key, cred_type)`——cred_type 值域归一
  {'bot','webhook'}（平台语义由 repo_key 前缀承载，D99）。
- **② 唯一性显式契约注释（本批拍板追加）**：每仓库每 cred_type 至多一条。
  若未来需要多 webhook（如同仓多回调地址），单表加
  `cred_slot TEXT NOT NULL DEFAULT 'default'` 并把唯一约束扩为
  `(repo_key, cred_type, cred_slot)`——属轻迁移（默认值使存量行零改写），
  届时再评审，本批不预置。
- 实码锚点（branch HEAD 06fdcdb8b 实测）：

| 锚点 | 位置 | 现状 |
|---|---|---|
| rc DDL + UNIQUE | src/api/db_setup.py:3984-3992（_fn_create_repo_credentials_table） | 已是目标态（含 UNIQUE(repo_key, cred_type)）；表注释补"每仓库每 cred_type 至多一条"契约句 |
| ORM 唯一约束 | src/persistence/models.py:121-125（uq_rc_repo_credtype） | 已在；模型注释同步契约句 |
| rc 读口 | src/user/db.py:4963（get_repo_credential_value） | rc 优先无条件，空串=无值，读侧统一解密（N3） |
| 列回落读口 | src/user/db.py:1744（get_bot_token_by_repo）/ 2054（get_webhook_verify_token）/ 2577（get_webhook_token_by_repo） | 列回落在前+rc 兜底（S85 形态）；分支注释需显式标 legacy（⑤） |
| webhook 列名派生 | src/user/db.py:4976+（_webhook_token_col_for） | gitcode/github 二列常量；gitee/gitlab 不入（③落地的读侧前提） |
| repo_key 单一产出 | src/repo_key.py:10（to_repo_key）/ :26（parse_repo_key） | ⑥ 锚点；回填直搬 managed_repos.repo_key 同表字符串，join 字节一致 |

### 卡片 schema（token_storage_field）

- 语义：token_storage_field 空 = 无托管列存储（凭据只落 rc）。
- 实码锚点：src/vcs/platform_card.py:359 当前默认
  `f"bot_{provider}_token"`——③ 落地时 gitee/gitlab 卡置空（配置卡与
  builtin 默认），gitcode/github 维持基线列名（读回落继续可用）。
- 卡片 schema 注释同步 ②：托管列若存在，列内值只可能是 legacy 残留
  （写入冻结+置空语义），权威在 rc。

---

## §3 gitee/gitlab 四列撤出与退役清单

| 列 | 处置 | 批次 |
|---|---|---|
| bot_gitee_token / gitee_webhook_token / bot_gitlab_token / gitlab_webhook_token | 撤出 DDL 与 _ensure_platform_storage_columns 白名单（src/api/db_setup.py:2337；白名单现列于 db_setup.py:4004-4005 一带）、撤出 backfill _CRED_COLS、撤出 ORM 模型列声明（src/persistence/models.py:104-107） | 本批（F 系） |
| gitcode/github 基线四列（bot_gitcode_token/gitcode_webhook_token/bot_github_token/github_webhook_token） | 保留为读回落；**物理删列走后续批次**，顺序铁律=删回落先删列不可倒置（D60④ 空值回落依赖写死） | 后续批次 |

注意：撤出后冷启动建库（新装 DDL）应无 gitee/gitlab token 列——
_ensure_platform_storage_columns 的"无卡不加/白名单"纪律（S64/S71）与
本批白名单收缩叠加生效。

---

## §4 回填与渐进收敛

- **回填（存量一次性）**：backfill_repo_credentials_from_columns
  （src/api/db_setup.py:3995-4022）幂等只填空——`INSERT OR IGNORE` +
  rc 有值不覆盖；列值经 N3 通道加密，no_plaintext 用例锁（回填产物
  落库必为密文形态）。
- **渐进收敛（常态写入路径，①核心）**：托管/轮换写 rc 单写，且
  **同事务内**置空 managed_repos 对应列——每次凭据轮换自动完成一条
  "列→rc"迁移，无需专门数据搬家窗口；列清零进度即可作为退出判据。
- repo_key 口径（⑥）：回填直搬 managed_repos.repo_key 同表字符串，
  不重算不归一——join 字节一致，迁移零失配。

---

## §5 legacy 读回落与退出 checklist

resolver 读回落分支（get_bot_token_by_repo / get_webhook_token_by_repo /
get_webhook_verify_token / RepoBotToken._fetch 列路径）：

1. 分支显式标注 legacy（注释 + 观测面独立消费审计：列读命中计数/日志
   单独通道，不与 rc 读混计）。
2. **退出 checklist（按序执行，顺序不可倒置）**：
   1. 回填完成：backfill 全量跑过，rc 行覆盖所有列有值仓（对账零缺行）；
   2. 置空完成：渐进收敛后列全空（含存量列残留扫描为零）；
   3. 关回落：删 legacy 读分支（消费审计零命中为前置判据）；
   4. 删分支后物理删列（gitcode/github 基线列，§3 后续批次）。

---

## §6 admin 隔离

- admin config_store 读写的是 **meta 全局凭据**（global_bot_* 键族，
  src/admin/config_store.py:86-91）与 bot_token_status 状态列——
  **全局级**对象，与本批 repo 级凭据（rc/managed_repos）无关，互不动。
- **同名键陷阱（⑦红线）**：meta 存在键 `gitcode_webhook_token`，与
  managed_repos 列 `gitcode_webhook_token` **同名不同表**。任何按列名
  收缩/置空/退役脚本**严禁按名误伤 meta**——脚本必须显式限定目标表
  （`UPDATE managed_repos SET ...`），且以表名+列名双因子定位，禁止
  全库键名扫描式改写。

---

## §7 措辞口径（对外/文档统一用语）

| 口径 | 准确含义 | 不指 |
|---|---|---|
| "接新平台零 DDL" | 新平台**接入层**（平台卡/方言/端点）零 DDL | grape_t_repo_credentials 本身——它是唯一演进表，演进（如 cred_slot）走表内迁移 |
| "调用方零分支" | 经 **resolver 接口**（TokenResolver/统一入口）取凭据零平台分支 | resolver 内部存储分层（rc 优先/列回落）不是分支债 |

---

## §8 验收用例（待 F4 交付）

| # | 用例 | 断言要点 | 测试锚点 |
|---|---|---|---|
| ① | 冷启动建库 | 新装库 DDL 无 gitee/gitlab token 列（schema 断言） | test_s128_gitee_gitlab_token_col_retirement.py::test_s128_cold_start_no_gl_columns（+ 卡终态/ORM 撤列/补列跳过三卫星用例） |
| ② | 新托管仓 token 只落 rc | 写入后 rc 有行、managed_repos 对应列为空（同事务置空） | test_s128_gitee_gitlab_token_col_retirement.py::test_s128_new_repo_rc_only_and_upsert_rotation（+ 列名键兼容路由用例） |
| ③ | 存量仓回填+置空后 resolver 仅凭 rc | 回填幂等只填空、置空后 resolver 返回正确值且列读零命中 | test_s128_gitee_gitlab_token_col_retirement.py::test_s128_backfill_then_rotation_blanks_and_resolver_reads_rc（+ 不误清未写种类/meta 不误伤/`_update_repo_bot_token` 终态三防误伤用例）+ no_plaintext 用例锁（N3） |

---

## §9 需求/用例登记索引

- 需求侧：multi-platform-decouple-requirements.md 新增 **FR-27**（仓库凭据
  存储终态化，FR-26 顺延）。
- 用例侧：11-usecases-guards.md 新增 **UC79**（编号自 UC78 顺延）。
- 映射侧：13-uc-test-mapping.md 追加 UC79↔测试映射（指向 F4 验收用例，
  文件名以 F4 交付为准）。
