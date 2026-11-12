# D99：grape_t_repo_credentials 表语义重设计——cred_type 归一 + id 主键化（方案 v3，2026-11-08）

> **本文件替换原 dual-authority-fix-design.md（D98 方案，已废弃）**。
> v2 错误：假设存在"8 种旧 cred_type 存量数据需要迁移"——实查证伪：rc 表是
> 本分支新建（S80，2026-11-04），分支未合入 weekly-08 基线，生产库从未有过
> 这张表，**没有任何存量数据**。不存在数据迁移，表直接按目标态建。

**来源**：用户质疑链——①`bot_gitcode_token` 写法不合理；②一个仓库只属
一个平台，cred_type 编码平台名是冗余；③id 就是主键，查询/引用以 id 为准
（varchar(256) 足够 UUID）。核对 managed_repos 分支前形态（13 列单平台
设计，凭据只有 gitcode 一对）后确认：按平台长列/列名编码进类型值是存储
演化的痕迹，不是合理设计。

---

## 一、根因定性

- S80"列式等价收口"把 managed_repos 的 8 个平台凭据列**机械直迁**成
  rc 行（列名→cred_type），把"按平台分列"的存储痕迹固化进了类型语义；
- 一个仓库只属一个平台（repo_key 前缀唯一确定），平台信息在 repo_key 里
  已有——cred_type 再编码平台名是冗余编码；
- 后果：cred_type 值域随平台数无限膨胀（N 平台=2N 种"类型"）、读者被迫
  平台枚举（`_has_rc_cred` or 链、`LIKE 'bot\_%'` 爬表）、id 名不副实
  （非主键可空、写入打空→审计引用链断）。

## 二、目标态（表语义重设计）

```sql
CREATE TABLE grape_t_repo_credentials (
    id         VARCHAR(256) PRIMARY KEY,   -- UUID（36 字符），查询/引用以 id 为准
    repo_key   TEXT NOT NULL,              -- 平台前缀承载平台语义（gitcode:o/r）
    cred_type  TEXT NOT NULL,              -- 归一值域：'bot' | 'webhook'（凭据种类，无平台名）
    value      TEXT NOT NULL DEFAULT '',   -- 密文（N3 通道）
    updated_at TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (repo_key, cred_type)           -- 业务唯一约束（一仓库一类一条）
)
```

- **cred_type 归一**：值域固定 `{bot, webhook}` 两种；新平台接入表结构
  零变化；
- **id 主键化**：VARCHAR(256) PK（UUID 36 字符），首写生成、更新不碰；
  usage_log.credential_id 引用链以 id 为准；
- **写冻结语义不变**：本表仍是仓库凭据唯一权威（D58 范围修正：全部
  token 写入入口统一写 rc，见 §四）。

## 三、建表（无数据迁移）

rc 表是本分支新建、从未上生产——**零存量数据，零数据迁移**。直接把
S80 建表迁移（202611050001）与 N4 补列迁移（202611070003）的目标态
改为本 DDL，分支合入前的所有环境（含测试库）一次建对。

- S80 建表迁移体替换为本 DDL（迁移版本号不变——本分支私有，无已盖章
  环境；若本地测试库已按旧 DDL 建表，删库重建即可——测试库本就每次
  pytest 重建）；
- N4 的"补 id 列"迁移（202611070003）退役（id 已在建表 DDL 中）；
- backfill_repo_credentials_from_columns（managed_repos 列→rc 行）：
  **保留但改形态**——列名→cred_type 归一映射（bot_*_token→bot、
  *_webhook_token→webhook），供开发/测试环境从 managed_repos 列造数，
  生产合入时 managed_repos 列本就为空（写冻结），零行。

## 四、读写点改造清单（全部 cred_type 消费点，行级）

| 位置 | 现状 | 改后 |
|---|---|---|
| `token_resolver.py` RepoBotToken._fetch（L928） | cred_type=token_storage_field()（列名） | cred_type=`'bot'` |
| `config_store.py` swap 步骤 6（L472） | 列名两 cred_type 写 | `'bot'`/`'webhook'` 两行（两步法 UPDATE→rowcount==0→INSERT，同事务） |
| `bot_token_health.py` L164 `LIKE 'bot\_%'` | 模糊匹配 | `cred_type='bot'` 精确 |
| `db.py` add_managed_repo rc 单写（L1329） | 列名 executemany | provider 派生→'bot'/'webhook' 两行 |
| `db.py` _update_repo_bot_token | 只写列不同步 rc | 补写 rc（cred_type='bot'，两步法，id 首写 uuid/更新不碰） |
| `db.py` get_repo_credential_value / get_repo_credential_meta | 复合键查 | 签名不变（repo_key+cred_type），语义归一；新增 get_repo_credential_by_id（id 反查，审计回溯） |
| `db_setup.py` backfill | 列名直迁 | 归一映射（见 §三） |
| `credentials_backfill_n1.py` 步骤 3/4 | 空 id 回填 | id 建表自带（主键）→ 步骤 4 退役；步骤 3（明文加密回填）保留（rc 行 value 加密语义不变） |
| `persistence/models.py` RepoCredentialModel | 复合 PK | id PK + UNIQUE(repo_key, cred_type) |
| `persistence/reconcile.py` | — | 跟随模型 |
| `admin/config_store.py` L388 值域白名单 | 8 值域守卫 | `{'bot','webhook'}` 值域守卫（D90-N2① 值域隔离语义不变，禁 *_pat 保留） |
| `_has_rc_cred`（repos.py） | 平台枚举 | cred_type='bot' 单查（无枚举） |

## 五、场景用例（UC41，tests/test_uc99_rc_schema.py）

| # | 场景 | 断言面 |
|---|---|---|
| ① | 表结构守卫 | id 为 PK、UNIQUE(repo_key,cred_type) 在册（PRAGMA 实测） |
| ② | cred_type 值域 | 非法值（如 bot_gitcode_token）写入被守卫拒绝；仅 'bot'/'webhook' 合法 |
| ③ | id 主键语义 | 每行 id 唯一非空 UUID；usage_log.credential_id 引用不悬空 |
| ④ | TokenResolver 读链 | repo bot token 命中 rc（cred_type='bot'）行为不变（回归锁） |
| ⑤ | 写入口统一 | add_managed_repo / 手动设置 / swap 三个入口全部落 rc（归一形态） |
| ⑥ | bot_token_health | `cred_type='bot'` 精确查询、第 5 平台自动纳入 |
| ⑦ | backfill 归一映射 | managed_repos 列造数 → backfill 后 cred_type 为归一值 |

## 六、实施顺序（先测后改）

1. UC41①② 结构守卫 RED（现表结构红）；
2. S80 建表 DDL 替换为目标态 + 202611070003 退役 + backfill 归一；
3. 读写点按 §四逐点改（每点附对应用例）；
4. UC41③-⑦ 绿 + 宽域回归（凭据域/token 域/守卫域）；
5. managed_repos 8 列写冻结现状不变（挂账 #12 收口另行批次）。

## 七、明确不动项

- managed_repos 表结构与 8 列（挂账 #12 终局另批）；
- usage_log 表结构（credential_id 软引用语义不变，引用值即 rc 主键 id）；
- /settings/gitcode-repo 路由 URL 与 DOM 命名（契约面，登记不动）；
- user_credentials / user_platform_credentials（用户级凭据，另表另语义）。
