# D100 凭据类型 Schema 驱动 + UPC 退役 + grape_t_system_credentials — 方案稿（v2，评审中）

> 状态：**方案先行，未实施**。基于参考稿（外部讨论整合）+ 本库代码实况修正。
> v2 修订（2026-11-08 外部评审 9 条意见全采纳）：写操作审计入批 1、注册表形态
> 定为代码常量、系统凭据盘点全景化（§1.4）、时间戳 NOT NULL 无 DEFAULT、
> scope_id 理由修正（MySQL UNIQUE 与 NULL）、crypto key 共用挂账、门面预留
> env→DB 切换模式 + 受控使用入口、global bot token 归属理由修正。

## 〇、裁定基线（用户已拍板，本文的公理）

1. **UPC（user_platform_credentials）方案整体废弃**——不是改进、不是并存。
2. **用户级凭据权威 = 原 user_credentials 表**（不退役，改为改进）。
3. **仓库级凭据权威 = 原 grape_t_repo_credentials 表**（D99 目标态已建对，本批不动结构）。
4. 凭据类型 **Schema 驱动**：cred_type 复合名 + **代码内注册表**（非 DB 元数据表）。
5. **OAuth 快照清退**（user_credentials 中 `<p>_oauth` 副本行退役）。
6. **凭据管理是即将交付的产品能力**，Org/System 层即将有真实消费方——
   grape_t_system_credentials 表进本批（批 1 建能力、零消费方切换、存量各带触发
   条件分批收编；global bot token 不属收编范围，理由见 §1.4 分类）。

## 一、存储面终态：三业务表 + 一审计表

### 1.0 存储边界声明（2026-11-08，外部"登录 vs 授权"分析 + 业界对标收口）

凭据管理库（uc/rc/sc）管理**长期授权凭据**（PAT/AK/SK/系统密钥）；
**OAuth token（access/refresh）持久化于 external_identities（登录域）**——
grape 存在"代表用户离线调用第三方 API"场景（agent 任务执行），token 必须
持久化，但一律加密存储、消费走运行时链。纯身份认证场景验证完成即建本地
会话（JWT，属会话管理域，不归凭据库）。

**ei（external_identities）定性：登录域凭据存储，域内安全能力自持**
（AES-GCM 加密/status 生命周期/失效链/token_version 轮换——具备登录域
所需的安全与周期能力；与 uc/rc/sc 治理能力**不要求同构**）。分域存储
=爆炸半径隔离（登录域凭据泄露不波及用户显式配置的 PAT/AKSK），存储边界
跟随信任边界（S116 守卫），不为"少一张表"合并——业界多存储现实下
"合并很少成功"教训 + blast-radius 隔离共识。

**"统一凭据管理"的正确落法 = 治理控制面，不是合并存储**：统一审计
schema/加密信封/轮换钩子/secret reference 收敛到**挂账 #5 闸门 ③
（凭据存储适配层）**——它是治理控制面的接口先行版，**不是迁移预备**；
物理存储形态与写入主体各自保留。把"统一治理"误读为"合并存储"是业界
反复警告的反模式。

**消费域边界（P2 收口实证，vcs-env-token-boundary-audit-design.md）**：
agent/worker **不持有任何平台凭据**——平台长期凭据（BOT_*/USER_*）
一律不再注入执行环境，全程只在服务端进程内解密流转。执行侧持有
**tk_ 会话凭证**（auth_token.issue_session_token：TTL 6h / sha256 落库
明文不落盘 / scope 三重限定 session_key+repo_key+agent_type / WORKER
模式过滤 CLI 命令面），所有 VCS 操作经 grape CLI 携带 tk_ 转发服务端。
tk_ 对外部平台 API **完全无效**（服务端自签自验）——即 octobroker
"消费方最多持有 broker 自己的可撤销 key"原则的已落地形态：
**resolve() 短时句柄模型已由 tk_ 实现等价物，无需凭据库中转**，
"refresh_token 迁 uc + resolve 发句柄"方案无必要（2026-11-08 外部
架构稿处方驳回的实证依据）。

**控制面（闸门③）启动触发条件（量化三选一，任一出现即立项）**：
① 跨域轮换预案需求（ei 与 uc/rc/sc 需要统一轮换编排）；
② 统一泄露应急需求（单个 secret 判定泄露需跨域批量吊销传播）；
③ 审计跨域关联查询需求（登录事件与配置事件需一致 schema 关联分析）。
条件未到不预建服务层（与 credential_types DB 表同一分寸）。

**门面签名精化（批 2 接位时统一）**：use/resolve 入参升级为显式
`purpose`（意图声明，如 "issue_create"）——现 executor_context 自由串
承担此职责，批 2 消费方接位时统一参数面，门面据此限流与审计归因。

**注册表精化（本批已落）**：fields 三元组 (name, required, sensitive)——
审计脱敏按 sensitive 标记路由（不靠字段名猜，业界 __sensitive_fields__
模式）；storage_mode 形态位（encrypted 出示型=现全量 / hashed 验证型 /
external_ref 闸门③ 外部引用）——把"能不能解密"从实现细节升为类型契约。

```
user_credentials        用户级（User scope）      —— 本批改造对象
grape_t_repo_credentials        仓库级（Repo scope）      —— D99 目标态，不动
grape_t_system_credentials      系统+组织级（本批新增）    —— 批 1 建能力
credential_usage_log    审计（全 scope 共用）      —— credential_kind 值域扩展
```

不建 credential_types DB 表（注册表=代码常量，§二）。多租户 Org 层语义预留
（scope_type 值域先锁 {'system'}，放开走独立评审）。

### 1.1 user_credentials（现状结构保留，改进不加列）

```sql
CREATE TABLE user_credentials (
    id          TEXT PRIMARY KEY,        -- UUID 36 字符（N1 已落）
    user_id     INTEGER NOT NULL,
    provider    TEXT NOT NULL,           -- 复合名承载（三族值域，见 §二）
    config_json MEDIUMTEXT NOT NULL,     -- JSON dict，整体 AES-GCM 加密（N1 已落）
    created_at  TEXT DEFAULT '',
    updated_at  TEXT DEFAULT '',
    UNIQUE (user_id, provider)
)
```

**裁定记录**：参考稿 2.1 的 `cred_type` + `value`（单值）+ `source` 三列形态
**不采纳**——与实况表结构冲突（provider 已承载复合名、config_json 已是 JSON
多字段、加密已整体落列）。D100"cred_type 复合名"在现表的落地形态 =
**provider 单列复合名**（零结构变更），类型语义由代码内注册表解释。

### 1.2 grape_t_repo_credentials（D99 目标态，零改动）

`id VARCHAR(256) PK` + `UNIQUE(repo_key, cred_type)`，cred_type ∈ {'bot','webhook'}
（归一值域，D99 已收敛 12 处读写点）。参考稿 2.2 的 Alembic batch_alter_table
路径**不采纳**（本项目 DDL 唯一出口=盖章迁移，且目标态已在场）。

### 1.3 grape_t_system_credentials（本批新增）

```sql
CREATE TABLE grape_t_system_credentials (
    id          VARCHAR(36) PRIMARY KEY,     -- UUID 36，应用层生成（表设计铁律 1）
    scope_type  VARCHAR(16) NOT NULL,        -- 值域 {'system'}；org 预留不实现
    scope_id    VARCHAR(64) NOT NULL,        -- system 固定 ''（见下）；org 时=org id
    cred_type   VARCHAR(64) NOT NULL,        -- 注册表值域白名单（§二 scope=system 段）
    value       TEXT NOT NULL,               -- 通道加密（credential_crypto 复用，同 rc）
    created_at  TEXT NOT NULL,               -- 应用层维护，无 DEFAULT（评审修正 4：
    updated_at  TEXT NOT NULL,               --   DEFAULT '' 让漏传变静默空串，时间审计失真）
    UNIQUE (scope_type, scope_id, cred_type)
)
```

设计裁定与理由：

- **scope_id 用 '' 不用 NULL——硬理由是 MySQL UNIQUE 语义**（评审修正 5）：
  MySQL 唯一索引对 NULL 视为互不相等，`(system, NULL, oauth_client)` 可插
  多行，UNIQUE 失效；'' 保证唯一约束真正生效。created_at/updated_at 同理
  NOT NULL 无 DEFAULT，由门面强制显式赋值（铁律 2 应用层维护 + 杜绝静默漏传）。
- **多字段类型走 value 存 JSON dict**（同 uc config_json 先例），注册表
  fields 约定解释；单值类型存明文串后整体加密——加密统一在写入口完成，
  门面读侧统一解密（消费方零感知，与 N1/N3 通道加密同模式）。
- **软删除裁定（评审 Minor-2）**：删除=物理删行，审计靠 usage_log 历史
  credential_id 悬空定位（软引用设计预期，审计只增不改）。**不做 stale 标记**——
  管理面语义"删除即失效"，撤销传播走消费点读不到行即空（与 rc unbind 链
  同语义）；失效保留需求出现时再加 status 列（零消费方不预建）。
- **索引（评审 Minor-3）**：UNIQUE 三列之外加 `INDEX(cred_type)`——管理面
  "列出某类型全部凭据"与批 2 迁移探针均按 cred_type 单查。
- **crypto key 共用挂账（评审修正 6）**：sc 与 uc/rc 共用 credential_crypto
  同一把 key——批 1 合理简化，但**密钥治理（独立 key / KMS / 轮转 / 双解密
  窗口 / 泄露预案）显式挂账**：当前 key 泄露影响面=全平台所有凭据。与参考稿
  缺口清单第 3 项合并跟踪，不因批 1 复用而视为不存在。
- **crypto 主密钥本身不进 sc**（评审修正 3 附带）：credential_crypto 主密钥
  由 security.json/env 管理（GRAPE_CREDENTIAL_KEYS，security_config.py），
  属部署基础设施——鸡生蛋问题（用它加密它自己），显式排除出收编范围。

### 1.4 系统级凭据现状全景盘点（收编对象裁定，逐条实锤）

原"五处"盘点缺漏，评审修正 3 补全为八处，并按**归属域分类**——不是所有
系统级凭据都归 sc 收编，分类标准=**凭据的消费语义属于哪个域**：

**A 类：归 sc 收编——【用户裁定 2026-11-08：全部暂缓，本批零数据迁移】**

值域 key 照常登记进注册表（定义≠收编）；真实 secret 留在 meta/env 现状，
迁移挂账待触发条件：

| # | 凭据 | 现存位置（实锤） | 问题 | 迁移触发条件（触发前不动） |
|---|---|---|---|---|
| A1 | OAuth client_id/secret（全平台） | meta 表 `oauth_<p>_client_id/secret`（providers.py L440 直读）+ oauth-providers.json 双写 | **secret 明文**（set_meta_value 无加密）；双源漂移 | OAuth client 运行时管理页立项时 |
| A2 | VLM API key（opengw） | env `VLM_API_KEY` > configs/vlm-config.json（image_understanding.py L165-186） | DB 外双源、无审计 | VLM 配置需运行时变更时 |
| A3 | OBS ACCESS_KEY/SECRET_KEY | env（config.py L156-157） | DB 外、无审计 | OBS 配置多环境/轮转需求时（华为云 ECS 部署环境终态=IAM 委托，不经 sc） |
| A4 | RESEND_API_KEY / ADMIN_BETA_APPLY_API_KEY | env（config.py L209/840） | DB 外、无审计 | 邮件配置管理页立项时 |

A 类共同前置问句（"必须存在"追问）：迁移前先评估能否以动态短期凭据/联邦
身份替代——能替代的直接走替代路线不经 sc；终态方向统一挂挂账 #5 评估
闸门 ③（凭据存储适配层），不新增独立挂账。

**B 类：不属于 sc 收编范围（边界裁定，防续审误报）**

| # | 凭据 | 现存位置 | 归属裁定 |
|---|---|---|---|
| B1 | 全局 bot token（gitcode/github） | meta 表 `global_bot_<p>_token` | **本质是仓库 bot token 的全局默认值**——消费语义属 repo 域（TokenResolver 第三级是 RepoBotToken 的缺省），D98 已由 rc swap 承载（cred_type='bot'，host_user_id=0）。它不是"不值得迁"，是**本来就不该归 system 级** |
| B2 | webhook secret / per-repo webhook token | managed_repos.webhook_secret + rc cred_type='webhook' | **per-repo 凭据**，归属 repo 域（已走 rc 通道加密 + N4 审计） |
| B3 | JWT_SECRET / JWT_SECRET_KEYS | env + configs/security.json（jwt_utils.py L36 / security_config.py） | **平台签名密钥属部署基础设施**——多实例共享+轮换池语义与进程启动绑定，DB 化反而引入启动依赖环。排除，与 crypto 主密钥同口径 |
| B4 | DB 连接串密码（DEVOPS_DB_PASSWORD） | env / configs | 部署基础设施，同 B3 排除。**无 Redis/队列**（全仓 grep 零命中，redis 仅出现在函数名 redispatch） |

A 类四项批 1 全部**只建能力不迁移**；B 类四项写进本节即为边界裁定，续审
遇到"为什么没收编 X"以此作答。

## 二、凭据类型代码内注册表（形态已定：代码常量，评审阻断项 2 收口）

### 2.1 形态裁定

**代码常量模块**（`src/user/credential_registry.py`，纯常量+纯函数，无 DB
依赖）——**不是物理表**。理由：与 rc 的 {'bot','webhook'} 白名单同构，批 1
只建一张表（sc），守卫在应用层；物理注册表是为"运行时动态扩类型"设计的
机制，当前零消费方（纪律：用不到不设计），挂触发条件见 2.4。

```python
@dataclass(frozen=True)
class CredentialTypeSpec:
    key: str                 # 复合名，如 "gitcode_pat" / "oauth_client"
    scope: str               # "user" | "repo" | "system"
    fields: tuple            # ((name, required), ...) —— JSON dict 字段约定
    description: str
    deprecated: bool = False # True = 清退过渡期值域（<p>_oauth）
```

### 2.2 值域三段（注册表逐条显式登记）

| 段 | 键形态 | 落表 | 状态 |
|---|---|---|---|
| user | 云凭据裸名（huawei_cloud/codearts，fields=ak/sk）+ `<p>_pat` | user_credentials.provider | 在役 |
| user-deprecated | `<p>_oauth` | user_credentials.provider | **本批清退**（§三） |
| repo | {'bot','webhook'} | grape_t_repo_credentials.cred_type | D99 已收敛，注册表仅登记对齐 |
| system | `oauth_client`（多平台同型，scope 维度无需按平台分键）、`vlm_api_key`、`obs_aksk`（fields=ak/sk）、`resend_api_key`、`admin_beta_api_key` | grape_t_system_credentials.cred_type | 批 1 登记，消费方批 2+ 接 |

注册表派生规则与 db.py L396-400 DDL 注释三族描述**同源对齐**（一处定义，
DDL 注释改为指向注册表，消灭双处维护）。

### 2.3 注册表消费点（grep 验证过的真实读取方，零预建）

1. `set_credential` / `get_credential` 写读白名单守卫（现按前缀约定，改为注册表查表+未登记 key 拒绝+log.warning）
2. settings.py PAT 入口（L507-557 `_p_pat` 写链）值域校验
3. sc 门面写入口 cred_type 白名单（§1.3）
4. rc 侧 {'bot','webhook'} 文档对齐（rc 与 uc/sc 注册表分域互斥）
5. 审计 log_usage 调用点 credential_kind 取值来源

### 2.4 credential_types DB 表触发条件（挂账）

出现"运行时动态扩类型"真实需求（如运营后台自助接入新凭据类型、无需发版）
时再立项。届时注册表常量降级为种子数据，写权限/迁移设计独立评审。

## 三、OAuth 快照清退（`<p>_oauth` 退役，三读一写全收敛）

现状事实链（全部 grep 实锤）：

| # | 位置 | 动作 | 清退处置 |
|---|---|---|---|
| W1 | db.py L3443-3456 `refresh_oauth_token` 步 5 | refresh 成功后**双写回** `<p>_oauth` 快照（注释自认"迁移期保留"） | 删除双写段 |
| R1 | deps.py L367 `get_user_token_with_refresh` | 读快照 `token_expires_at/fetched_at` 做预刷新判定 | 改读 external_identities.expires_at（权威存储已有同语义列） |
| R2 | db.py L3312 `_read_refresh_token_from_credentials` | ei 解密失败时的迁移期 fallback | 删除函数+调用点（L3344） |
| R3 | routes/auth.py L88-89 | 绑定判定回落读快照 | 删回落（bindings 已含同语义判定） |

存量快照行：**【用户拍板 P1=a 冻结不删】**——W1 写点消灭后行自然死数据，
不建清理迁移（删除无消费收益）；续审遇 uc 中 `<p>_oauth` 残留行以此作答。

清退后不变式：**OAuth token 权威=external_identities（加密），user_credentials
只承载用户显式配置的凭据（云 AK/SK + PAT）**。

## 四、UPC 退役清单（逐件处置，全部 grep 实锤定位）

| # | 已落地件 | 位置 | 处置 |
|---|---|---|---|
| R1 | 建表迁移 202611070001 | db_setup.py L3600-3617 | **退役**（fn 改 no-op 壳+注释，版本号保留防盖章断链）。**无需 DROP 迁移**——upc 从未上生产（D90 引入于本分支，一周内废弃），R1 退役后无任何库会产生此表；本地测试库走删库重建 |
| R2 | ORM 模型 | models_platform_credential.py | 删除文件 |
| R3 | DTO + Repository | dto.py L74 / repositories.py L268-338 | 删除 |
| R4 | provisioner 写 upc | platform_credential_provisioner.py L53-66 | **去向裁定 R7**（倾向 b，见下） |
| R5 | 登录域 4 处接线 | grape_auth.py L1674/1888/2316/2439 | 随 R4 |
| R6 | usage_log 存量 kind='user_platform_credentials' 行 | 审计表 | 保留（审计只增不改；kind 值域对新写入收窄） |
| R7 | S123/S124 测试 | tests/test_s123/s124 | 删除（s126 usage_log 测试中 kind 断言同步收窄） |
| R8 | 06-ledger 挂账 #9 P-2c/P-8 段 | 06-ledger.md | 改写为"UPC 已废弃（D100），P-8 观察口径作废" |

**不受影响保留**：P-3A PAT 优先单序（token_resolver UserToken）、P-6 401
兜底（oauth_401_retry）、S116 守卫语义（禁"登录链直写仓库表"，与 upc 存废正交）。

### R7 裁定点：provision_from_oauth 去向——【用户拍板 P2=(a) 整体退役】

- **(a) 整体退役（已拍板）**：函数+4 处接线删除，登录成功后零额外落库——
  与基线行为完全一致（基线本无授权记录实体，provisioner 是本分支 D90 新建，
  `git show grape-dev/weekly-08:src/vcs/platform_credential_provisioner.py`
  不存在实锤）。绑定事件本身已有 auth_audit 留痕，审计无损。
- ~~(b) 改锚 external_identities~~（评审时倾向，被 (a) 取代）
- ~~(c) 冻结~~（与废弃裁定矛盾）

**原始目的链存档（为何曾有此逻辑）**：D10"三合一"承诺（身份绑定+仓库凭据
自动填充+生命周期托管）→ S117 续审发现②未接线（注册表零消费）→ D90 契约
评审定 provisioner 端口+P-2c 建表 → 实现落地仅"记账壳"（upc 指针行+审计，
零读取方）。②"自动填充"的实质需求由用时取用链（get_user_token_for 实时
读 ei）承载——用户无感知，落库记录是为闭合文档承诺而生的数据，非功能。

**D10"三合一"表述收口（随 (a) 生效）**：①身份绑定（external_identities）+
③生命周期托管（刷新注册表）+②用时取用（lazy 等效）——"落库填充"从承诺
移除，②的实现路径正式定为 lazy，不留"待切换"口子。挂账 #9 措辞随 R8 改写。

## 五、upc 三处逻辑错误裁定（按代码实据重建；原拍板原文不在本库会话，评审时对照确认）

1. **冗余绑定事实**：upc.external_identity_id 是"指针的指针"——ei 本身就是
   权威绑定关系，upc 以 UNIQUE(user_id, provider) 再表达一次同一事实，双表可漂移。
2. **零消费写入**：provision 落库的 oauth_fill 行，全仓 grep 无任何
   get/list 读取方——授权记录语义无消费者，纯死数据制造器。
3. **审计锚点错位**：usage_log 记 kind='user_platform_credentials'+upc.id，
   而 PAT 优先链实际消费对象是 user_credentials/external_identities——审计
   指向的不是被消费的凭据。

## 六、消费链（不动项，锁定表述）

- TokenResolver 固定四级：session → repo_bot → global_bot → user（架构常量，
  N2②：source/cred_type 只做元数据不驱动排序）。参考稿 1.3"按执行主体分链"
  的 Org/System 形态是未决项 1 的派生物，单租户下**现状四级即其落地形态**。
- UserToken 级内 PAT 优先（P-3A 已落，S125 守卫在册）。

## 七、grape_t_system_credentials 门面与批 1 范围（评审修正 1/7/8 落点）

### 7.1 批 1 交付清单（零消费方切换）

| 件 | 内容 |
|---|---|
| 表 | sc 建表迁移（盖章迁移唯一出口；双方言：SQLite TEXT/MySQL VARCHAR 显式） |
| 注册表 | credential_registry.py 常量模块（三段值域） |
| ORM | 三件套（models/dto/repositories，S79 同构先例） |
| 门面 | src/user/grape_t_system_credentials.py：create/update/delete/list/get + **use（受控使用入口，见 7.3）** |
| 写闸门 | 门面写入口挂 require_admin 语义 + **写操作审计（见 7.2）** |
| 测试 | UC42⑥ 系列（见 §八） |

### 7.2 写操作审计（安全阻断项 1，批 1 范围非挂账）

消费审计（usage_log：谁用了凭据）与**变更审计**（auth_audit：谁改了凭据）
分离。门面 create/update/delete 强制写 `auth_audit.log_audit`：

- event_type=`system_credential_create/update/delete`
- operator=admin id（require_admin 注入）、ip
- detail 只记 scope_type/cred_type/field 名与**值指纹（尾 4 位，同 admin
  config_store 脱敏先例）**，不落明文——admin 账号被盗改凭据可事后归因，
  且知道改了什么类型、值是否变化（旧值指纹随 update 记录）。

### 7.3 受控使用入口（评审修正 7：批 1 零消费方下审计链可测）

门面暴露内部 API（非 HTTP）`use(cred_type, actor_id, context) -> 解密值`：
读行→解密→写 usage_log(credential_kind='system_credentials', initiating_actor_id,
executor_context)→返回。批 1 测试直接调它断言审计落库（不 mock）；批 2
每个消费方迁移统一改走此入口，杜绝消费点各自解密绕过审计。

### 7.4 env→DB 统一切换/回滚模式（评审修正 8：批 1 预留能力）

门面提供统一读取语义：**`resolve(cred_type)`：DB 命中即权威；DB 空 → 回落
env/文件（按注册表登记的 fallback 源）**。批 2 每个消费方迁移=同一套动作：
写入 sc → 观察期 resolve 走 DB → 清除 env 源；回滚=删 sc 行（resolve 自动
回落 env，零代码变更）。不加 feature flag——resolve 的回落语义天然是开关，
双开关是过度设计（flag 先例：P-3A 的 flag 已被用户拍板删除）。

### 7.5 批 1 零迁移裁定（用户拍板 2026-11-08）

A1-A4 全部暂缓（§1.4）：sc 建表后为空表，真实 secret 留 meta/env 现状。
空表非死表——门面五入口（create/update/delete/use/resolve）+ 双审计 +
回落机制全部经注册表登记的测试型 cred_type 实测覆盖（UC42⑥），消费方
接位时零结构性改动，只做数据搬迁。

## 八、未决项清单（评审逐项拍板）

| # | 未决项 | 影响 | 建议 |
|---|---|---|---|
| 1 | 系统是否多租户 | Org 层放开牵动全链 org_id 派生 | 独立评审；本批 scope_type 锁 {'system'} 不受影响 |
| 2 | ~~OAuth 快照存量行~~ | §三 R6 | **已拍板 P1=a 冻结不删**（2026-11-08） |
| 3 | ~~provisioner 去向~~ | §四 R7 | **已拍板 P2=(a) 整体退役**（2026-11-08） |
| 4 | ~~upc 存量表~~ | 新装库不建（R1 no-op） | **已拍板 P3=a；修正（2026-11-08）：无需 DROP 迁移**——upc 从未上生产，R1 no-op 化即闭环，无存量库需清理 |
| 5 | ~~usage_log 存量 upc kind 行~~ | 审计连续性 | **已确认 P4=保留**（只增不改铁律） |
| 6 | ~~uc provider 值域收紧时机~~ | 注册表守卫上线存量兼容 | **已确认 P5=warning 期一轮**（零未登记命中后再收紧） |
| 7 | ~~crypto key 独立/轮转~~ | sc 落地后共用 key 泄露影响面=全平台；现兜底链含"从 JWT_SECRET 派生"（security_config.py L83-93），轮换 JWT 连带凭据解密域 | **已确认 P8=挂账**，KMS 评估与挂账 #5 闸门 ③ 合并 |
| 8 | ~~A1-A4 迁移时机~~ | sc 空表→消费方接位 | **已确认（用户裁定 A 类全部暂缓）**：各带触发条件（§1.4），"必须存在"追问为收编前置问句 |

P6（提交切分）已拍板 **b**：D99 先行独立提交，本方案稿随 D100 提交。
P7（多租户）：已确认——本批 scope_type 锁 {'system'}，Org 放开走独立评审。

**全部未决项收口完毕（2026-11-08），方案冻结，进入实施。**

## 九、测试设计（UC42+，TDD 要点）

- UC42① 注册表守卫：未登记 provider key 写入被拒+告警（RED 先行）
- UC42② PAT 三平台经注册表写入读出等价（改造前后行为等价）
- UC42③ OAuth 快照清退：W1 双写消灭、R1 改道后过期判定走 ei.expires_at
- UC42④ upc 退役：新装库零 upc 表；provision 路径（若 R7=b）审计行
  kind='external_identities' 且 upc 零写入
- UC42⑤ rc/uc/sc 分域互斥：uc 注册表 key 不出现在 rc cred_type 值域
- UC42⑥ sc 批 1：CRUD 门面（admin 闸门：非 admin 路径写入被拒）+ 值加密
  落库（库内行非明文）+ use 入口审计三断言（usage_log 行/auth_audit 行/
  解密等值）+ resolve 回落（DB 空走 env fallback）+ 唯一约束（scope_id=''
  下同 type 重复写入被 UNIQUE 拒）+ 注册表未登记 cred_type 拒绝
- 回归面：S121/S122/S125（PAT 优先链）、S126（usage_log kind 收窄）、
  auth 页绑定判定（R3 删回落）

## 十、参考稿偏差修正记录（评审对照表）

| # | 参考稿原文 | 修正 | 依据 |
|---|---|---|---|
| 1 | 2.1 uc 表 = cred_type+value+source | provider 复合名 + config_json（现状） | db.py L394-410 |
| 2 | 2.2 rc id 走 Alembic batch_alter_table | 无 Alembic；D99 目标态已在场 | db_setup 盖章迁移唯一出口 |
| 3 | "PAT/OAuth 单字段够用，AK/SK 是 JSON 化信号" | config_json 已 JSON 化，AK/SK 已落地 | N1 整体加密 + L429 |
| 4 | 未决项 5"bot token 是否明文" | 已闭合（N3 通道加密） | f0442c2e8 |
| 5 | 未决项 6"bot token 是否进审计" | 已闭合（N4 1A/2B/3A） | c5c94354a |
| 6 | 2.5 credential_types DB 表 | 代码常量注册表（§二，形态已定稿） | 用户拍板 + 零消费方纪律 |
| 7 | 1.3 分链表（Repo→Org→System…） | 现状四级即单租户形态，Org 挂未决项 1 | N2② 链序常量 |
| 8 | usage_log 列名 result | 实际为 executor_context + operation | db_setup L3621-3632 |
| 9 | 2.1 UNIQUE [讨论中]（基于 upc 替代表） | UNIQUE 保留（uc 即唯一权威） | 裁定基线 1/2 |
| 10 | 2.3 grape_t_system_credentials"整张表[讨论中]" | 本批建（裁定基线 6），时间戳/scope_id 修正见 §1.3 | 评审修正 4/5 |
| 11 | 未决项 4"grape_t_system_credentials 是否本批建" | 已闭合（本批批 1） | 用户拍板 |
