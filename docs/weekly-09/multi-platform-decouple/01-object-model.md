> ⚠️ 本文为拆分分片（内容唯一权威源）。索引导航：./multi-platform-decouple-design.md
> 来源：multi-platform-decouple-design.md L89-L200（原文原样搬运，零改写）。
> 域定义：领域建模——凭据/身份对象模型、全域地图、双卡分离。
> 全局约束：继承索引页「表设计铁律/实现纪律/验收纪律」（见索引页）。

### D6：两类凭据对象模型——登录凭据 ≠ 仓库凭据

领域模型上它们是两个对象，存储、读写路径、生命周期全部分离：

**LoginCredential（账号域）**
- 身份会话凭据：OAuth access/refresh token（external_identities 表，现状已独立）+ JWT 会话。
- 生命周期：登录产生 → 自动刷新 → 登出/改密吊销。归属主体 = 账号。
- 读写路径：grape_auth 路由 + auth/providers.py。

**RepoCredential（资源域）**
- 仓库访问凭据：token + source(pat | bot) + status/expiry
  （**source=oauth_fill 已随 upc 废弃退役——D100**：仓库域授权走用时取用
  lazy，不落库，见 D10 收口段；source 值域收缩为两态）。
- 生命周期：绑定/配置产生 → PAT/bot 型不自动续期、失效即状态标记；
  OAuth 授权型走 lazy 链（不落库，无本表行）。归属主体 = (user_id, provider)
  或 repo_key。
- 读写路径：仓库绑定/凭据 API + 统一取凭据 facade（D3 合并后的
  get_user_token_with_refresh / Repo.bot_token），不触 user_accounts 登录列。

**实施分两步（保零 DDL 纪律）**
- 批次 0（本批）：逻辑解耦——所有取仓库凭据的路径改走 RepoCredential 语义
  （facade + provider 由 repo_key 推导 + K4 登录兜底删除），存储暂用现表，
  但读写不再碰登录域列（gitcode_api_token 等改由 facade 封装读写，调用方无感）。
- 批次 1（DDL 批，与 managed_repos 泛型化合并设计）：物理分表——新建
  grape_t_repo_credentials 表（user_id, provider, repo_key?, token 加密, source, status,
  updated_at），user_accounts 的 *_api_token 列与 managed_repos bot_* 列
  一次性迁移收列；feishu/huawei 凭据同期评估迁 user_credentials 泛型表。
  登录表最终只剩身份列。

### D7：凭据/身份对象模型总览（全域地图）

全库实测（CREATE TABLE 扫描）共 13 个凭据/身份承载对象，按域归类：

**身份域**
| 对象 | 存储 | 状态 |
|---|---|---|
| GrapeAccount 本地账号 | grape_accounts（零平台字段） | ✓ 分离干净 |
| OAuthIdentity 平台绑定 | external_identities（token 加密、解绑审计） | ✓ 分离干净 |
| AdminAccount | admin_accounts | △ 平行体系（G2，同批既有结论） |
| 旧账号遗留 | user_accounts 上帝表 | ✗ M1，批次 1 收口 |

**凭据域（目标 5 族）**
| 族 | 现状存储 | 状态与归宿 |
|---|---|---|
| LoginCredential | JWT + external_identities | ✓ |
| RepoCredential | user_accounts PAT 列 + managed_repos bot 分列 + meta KV 全局 bot + webhook_secret | ✗ 散 4 处 → D6 批次 0 逻辑收口 / 批次 1 物理收口（grape_t_repo_credentials 表，AES-GCM 加密） |
| RuntimeCredential | runtime_credentials（sha256 哈希、scope 三态、status、last_seen） | ✓ **范本模型** |
| SessionApiToken | session_api_tokens（sha256+过期） | ✓ |
| IntegrationCredential | user_accounts 明文列 + user_credentials 明文 JSON | ✗ M3，IM/云凭据族待建模（并入批次 1 泛型 user_credentials 改造评估） |

- RepoCredential 需回明文调平台 API，不能照抄范本的哈希方案；加密走
  credential_crypto AES-GCM（对齐 v3.58 C 系列），其余字段（scope/status/
  last_seen）照抄 runtime_credentials 范本。
- grape_t_credential_reveal_log（凭据展示审计）已有，作为全部凭据族的统一审计件复用。
- oauth_states 为 OAuth 握手瞬态，不属凭据域。

### D8：全流程凭据触点地图（8 场景阶段）

| 阶段 | 触发对象 | 存储/通道 | 状态 |
|---|---|---|---|
| 1 登录 | password / OAuth / JWT | grape_accounts 哈希 / external_identities 加密 / .jwt_secret | ✓✓ ✗M7（.jwt_secret 未进 .gitignore） |
| 2 仓库绑定/认领 | repo bot / 全局 bot / 用户 PAT | managed_repos / global-bot-token.json+meta / user_accounts | ✗M1/M2 |
| 3 webhook 验签 | webhook_secret | managed_repos 明文 | ✗M4 |
| 4 任务下发 | token 注入 agent/worker 环境 | askpass 脚本（不落盘）+ GRAPE_LINKED_REPO_TOKENS | askpass✓ ✗M8（明文 JSON env） |
| 5 git 传输 | clone extraheader / push askpass | repo_manager URL 内联 | ✓ ✗G4 |
| 6 平台 API | OAuth 读 / bot 写 | oauth_token 包装器 | ✓（K1-K2 待解） |
| 7 IM 通知 | feishu bot | global_im_bot_config 仅 lease 结构（✓）；im_app_secret 明文列 | ✗M9 |
| 8 worker 接入 | rta_ token | runtime_credentials Bearer+sha256+scope | ✓ 范本 |

- 结论：加密/哈希/审计三个安全基元都已各有一个正确范本
  （external_identities 的 AES-GCM / runtime_credentials 的 sha256 / grape_t_credential_reveal_log），
  问题不是缺基元，是基元没有推广到 Repo/Integration 两族——D6/D8 的分族收口即推广路径。
- M7（.jwt_secret 补 .gitignore）一行零风险，随本批修复。
- M8/M9 归批次 1（凭据注入通道收敛 + IntegrationCredential 建模）。

### D9：D6 与既有设计的关系

- D3（K1-K4 消除）是 D6 在"取凭据逻辑"上的投影；D6 是对象模型层的完整表述。
- v3.27 §5.6 的"绑定即写凭据"原表述= OAuth 登录时向 RepoCredential 写入一条
  source=oauth_fill 的引用（指向 LoginCredential，不复制 token），
  刷新由 LoginCredential 域统一完成——两个对象模型靠引用协作，不靠字段混存。
  **D100 收口**：引用协作保留，但"写 RepoCredential 行"不发生——授权关系
  由登录绑定行本身承载（ei），仓库域用时取用，零第二落库点。

### D10：双配置卡分离——登录渠道卡 ≠ 仓库平台卡

D1 的"平台配置卡"拆为两张，对应两条正交轴：

- **oauth_providers_config（登录渠道卡）**：既有机制不变——auth_url/token_url/scope/
  字段映射/whitelist_column。删掉此卡中与仓库无关的字段后，纯登录场景零新增。
  白名单已全链路配置化（实测 db_setup.py:2262 启动时按卡自动 ALTER 加列，
  is_user_allowed 按卡列名查询）——新登录渠道白名单零代码零手工 DDL；
  分列宽表模型属批次 1 死列家族（审计 F3 已挂账），本批不动。
- **vcs_providers_config（仓库平台卡）**：host/dialect/webhook_path/event_header/
  api_base/clone_auth/token_env。**OAuth 字段一概不设**——本卡的存在与是否配置
  对应登录渠道无关（PAT-only 平台只有这张卡）。

- 两卡同名关联（如都有 "gitee"）时 OAuth 登录即完成仓库域授权（用户无需
  手配 PAT）；只有仓库卡时用户手填 PAT（source=pat）。
  **PAT 通道不经过任何 OAuth 代码路径**（S12 验收）——这是 D6 分族的直接推论：
  仓库凭据对象的读写、存储、生命周期均不依赖 external_identities 存在与否。
- **oauth_fill 语义（D10 原拍板 → D100 收口 2026-11-08）**：配了登录渠道卡的
  平台，OAuth 授权成功即三合一——身份绑定（external_identities）+ 仓库域
  授权（**②实现形态 D100 收口：用时取用 lazy**——登录即完成授权，agent
  任务执行时实时从 ei 读+刷新，**不做仓库凭据落库填充**，upc 路径已整体
  废弃）+ 生命周期托管（refresh/失效提示全在登录凭据域）。用户无需手配 PAT。
- **并存优先级（D100-P3A 已实现：显式意图优先）**：同一平台用户手配 PAT 与
  OAuth 授权并存时，PAT 优先（显式配置=明确意图，fetch_user_token 单序
  ①pat→②oauth，S125 守卫）。
- PAT-only 平台新增 = 仓库平台卡一条配置 + 用户填 PAT，零代码零 DDL（批次 0 范围内
  依托 facade 读写现表；批次 1 grape_t_repo_credentials 表落地后连"按平台硬开列"约束也解除）。

### D11：S12 场景对既有设计的反向约束

S12（PAT-only 端到端）要求本批实现顺序调整为：git 层与 webhook 层的配置驱动
（D2/D4）先行，RepoCredential facade（D6 批次 0）次之，K1-K4 解耦（D3）最后——
因为 S12 的全链路（webhook→任务→git push→PR）不经过 OAuth 包装器，
先打通非 OAuth 路径可 earliest 验证对象模型分族是否真正独立。

