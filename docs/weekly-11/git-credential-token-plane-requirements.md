# 需求：git-credential 的凭据取用与错误码

> 2026-11-21。关联 Issue #372（用户 token 主线）/#990（会话凭据）/D90（token 平面）。
> 设计实现见 `docs/weekly-11/git-credential-token-plane-revision.md`。

---

## 1. 问题陈述

托管仓库的 git 操作（clone / push）由 agent 代为执行，凭据经
`GET /api/repos/{owner}/{repo}/git-credential` 一次性下发。当前实现有四个问题：

```
① 取错接口     _bot() 用 read_stored_token：逐策略 _fetch_stored，带 path-B 守卫且
               UserToken 只读 OAuth → gitee（只有 PAT）这条腿恒空
② 判据错位     平面由建仓时写死的 managed_repos.host_user_id 单向决定：
               之后管理员再配仓库级/全局 bot 也永远用不到（与 R2.1「bot 优先」相反）
③ 兜底过窄     _user() 只看当前会话用户；agent 的 tk_ 会话 user_id 常为 0/空
               → 凭据域 PAT 分支被跳过（if user_id: 才查）→ 整条链断
④ 错误码误导   全部失败都返回 502 Bad Gateway，把「未配置凭据」伪装成「上游故障」
```

现场证据：gitee 托管仓库 `git-credential` 恒 502；`gitee:yang-aiwu-new/demo`
无 rc bot 行、host_user_id=`629a7971-…`、归属者 PAT 存在（len=32），
但用会话 user_id（空）取返回空 → 502。

## 2. 目标

```
G1  配了 bot 的仓库就用 bot（回到 R2.1：repo bot → global bot → 归属者用户凭据）
G2  没配 bot 时按归属者降级，不再因会话 user_id=0 断链
G3  bot 与用户身份永不混用；显式配了 bot 但不可用时报错，不静默换身份
G4  配置问题与上游故障用不同错误码区分，运维可自助定位
G5  chat（人机对话）的身份永远是被对话的人，不因仓库归属改变
G6  不改动执行链（pipeline / clone / push）的既有平面语义
```

## 3. 需求条目

### R1  取用顺序按会话类型分序

```
R1.1  devops（事件驱动，非 chat）
        ① 显式配置的仓库 bot（grape_t_repo_credentials, cred_type='bot'）
        ② 归属者 host_user_id 的用户凭据（凭据域 PAT 优先，再 external_identities OAuth）
        ③ 当前会话用户的凭据
        ④ 全空 → 见 R4 错误码

R1.2  chat（agent_type='chat'）
        ① 当前会话用户的凭据
        ② 归属者凭据
        ③ 显式配置的仓库 bot
        约束：chat 不得用仓库归属者的身份代签 —— 否则非 owner 在对话里操作该仓库
              会拿到 owner 的 PAT（权限放大 + 审计失真）。

R1.3  无显式仓库 bot 时，插入既有标准策略链（RepoBotToken → GlobalBotToken → UserToken）：
        · path-A 仓库（host_user_id 为空）→ 全局 bot 命中（存量 12 仓照旧可用）
        · path-B 仓库 → 两层 bot 被守卫跳过 → 落到 R1.1 ② / R1.2 ② 的归属者档
```

### R2  bot 解禁必须有闸（双信号）

```
R2.1  闸 = 两个独立信号，不得混用：
        _has_bot_row  该仓库是否显式配置了 rc bot 行   → 决定「能不能用 bot」
        _gate_bot     该行 value 是否非空（可用）      → 决定「现在用不用它」
R2.2  开闸后只认仓库级 bot：直取其 value，不经 TokenResolver。
       理由：bypass_path_b 是整链解锁（RepoBot + GlobalBot 两级），
             用它会让「该仓没配 bot、平台配了全局 bot」的 path-B 私仓落到全局 bot，
             而全局 bot 通常不是该私仓协作者 → git push 403。
R2.3  行存在但 value 不可用 → 报 424 BOT_CREDENTIAL_INVALID，不静默降级到用户身份。
R2.4  不得改动 db 层 _repo_bot_token_value 的 path-B 守卫（执行链保护继续生效）。
R2.5  过期短路必须保留：bot 值可用但 managed_repos.bot_token_status='expired' → 视为不可用 → 424。
       状态位不在 rc 表，而是 managed_repos.bot_token_status（R18.2.2 原始落点）。
       git-credential 直取 rc 值的路径必须自己读它，否则绕过短路。
R2.6  三态必须可区分：无行 / 行存在但密文解不开 / 行存在且可用。
       解不开 → 424 BOT_CREDENTIAL_INVALID（detail.stage='undecryptable'），
       **不得**与「无行」同返回 None —— 否则静默降到用户身份（身份混用）。
R2.7  TokenResolver 不得暴露 bypass_path_b 入口（2026-11-21 撤除）：
       它会同时解锁 RepoBot + GlobalBot 两级，调用方裸传 True 会把未配 rc bot 的
       path-B 私仓推到平台全局 bot（通常非协作者 → push 403）。
       「回帖必须来自 bot 账号」由策略级 get_bot_identity_token 承担。

R2.8  表归属副本只可「检测 + 告警」，默认不得自动删除：
       非 owner 库里若残留 owner 表的 BASE TABLE 副本，必须可观测
       （[TABLE_OWNERSHIP_AUDIT]）。建表路径**不得**逐表插入 information_schema 查询
       （2026-11-21 撤除 db_setup._foreign_copy_warning）——那是运维诊断，不是启动的活。
       理由：副本 0 行会让裸 SQL 得到「0 行 = 没配置」的错结论（本轮两次误判），
       而非 0 行副本可能正是该部署的真源（227 的 rc 基表在 runtime）。
R2.9  清理只能作用于 0 行副本，且必须由 GRAPE_TABLE_COPY_CLEANUP=1 显式开启；
       非 0 行副本永远拒绝删除（数据可能只在这一份里）；表名须过标识符白名单。
R2.10 迁移域判定必须是三级链，不得只依赖 env：
       ① GRAPE_MIGRATION_OWNER 显式且合法 → 用之
       ② 缺省/脏值 → 由本进程库名推断（DEVOPS_DB_NAME → 域）
       ③ 仍判不出 → 由进程角色映射（server/main=mgmt，event=runtime，insight=insight）
       单库模式（GRAPE_DOMAIN_LIB 非 1）仍返回空串 = 跑全量，历史行为不变。
       理由：owner 为空 = 不筛域，mgmt 会跑全量把别域 DDL 建进本域库，event/insight
       则直接不迁移 → 实测「迁移 202611200006 建在 mgmt、runtime 缺列、引擎注册 500」。
R2.11 域迁移入口必须先拦 worker（与 event 共库），不得并发跑同一批 DDL；
       该拦截要发生在域推断之前（库名推断会把 worker 也判成 runtime）。
R2.12 meta 写入的存在性只能由主键回答，不得用 UPDATE 的 rowcount：
       必须是一条 `INSERT ... ON CONFLICT(...) DO UPDATE`（方言层翻 `ON DUPLICATE KEY UPDATE`）。
       实测：MySQL 同值重写 rowcount=0 → 旧两段式回落 INSERT → 1062 meta.PRIMARY，
       每次启动刷一条；SQLite 计匹配行故单测全绿。
R2.13 OAuth 渠道定义的写入只走 `grape_t_oauth_channels`，不再抄进 meta：
       表已承载全套字段（auth_url/token_url/scope/pkce/id_field/client_*_enc/enabled），
       且 providers.py 读取顺序为「文件卡 → meta 逐键覆盖 → 表整卡覆盖」，表是运行时真源。
       meta 的读取层保留（老部署表未就绪时的兼容），但写入路径删除。
R2.14 取卡通道故障必须与「平台未注册」分码，不得退化成一个错误码：
       服务端为避免 5xx 以 `{cards:{}, error:"platform_cards_unavailable"}` 报告读表失败，
       客户端此前只取 cards（`{}` 是合法值）→ 装配层回落 builtin（只有 gitcode）
       → 任务看到 `platform_not_registered`，把系统性故障伪装成"没配卡"。
       契约：fetch_cards 消费 error / 5xx → `PlatformCardUnavailableError`；
       装配层记录 degraded 原因并随缓存同生命周期；clone 端 code=`platform_card_contract_unavailable`；
       默认超时 10.0s（取卡只在缓存未命中时发生），仅传输类失败重试 1 次，
       401/403/404 与响应格式非法不重试。
```

### R3  用户档的兜底面

```
R3.1  归属者凭据按 host_user_id 定位（凭据域 PAT 按 user_id 命中，不依赖登录名）。
R3.2  get_host_vcs_username 为空时回落仓库 owner 段（parse_repo_key 第 2 段），
       避免 OAuth-only 用户因 username 缺失匹配不到。
R3.3  归属者查询异常不得打断整条链：记 debug 日志后继续落会话用户档。
```

### R4  错误码

```
R4.1  424 CREDENTIAL_NOT_CONFIGURED   该仓库未配置任何可用凭据（无 bot + 无用户凭据）
          —— 可自助修复，不是上游故障
R4.2  424 BOT_CREDENTIAL_INVALID      显式配置了 bot 行但值不可用（被清空/解密失败）
          —— 运维动作：轮换凭据
R4.3  502                              仅用于真实异常（stage/exc 记录具体阶段与异常类型）
R4.4  403                              非会话级 token（API Key / Web JWT）调本端点，保持既有语义
R4.5  响应体带 code + stage（502 时）+ 可读 message；不得返回空串冒充成功。
```

### R5  不得改动

```
R5.1  执行链（event_pipeline / repocgit / 接管）的 token 取用语义与顺序
R5.2  pipeline / clone / push 的 path-B 平面判定
R5.3  read_stored_token 的既有用途（A2A 鉴权 / session 写入）
R5.4  TokenResolver 的链序（架构常量，变更需契约评审）
```

## 4. 验收标准

```
A1  gitee 托管仓库（无 rc bot 行）git-credential 返回归属者 PAT，不再 502
A2  配了 rc bot 行的仓库 → 返回该 bot，且不解析任何用户凭据
A3  rc 行存在但 value 为空 → 424 BOT_CREDENTIAL_INVALID，且不返回全局 bot
A4  chat 会话由非 owner 发起 → 用的是会话用户凭据，不是归属者
A5  path-A 仓库（12 个存量）→ 全局 bot 照旧可用，行为与改前一致
A6  全空 → 424 CREDENTIAL_NOT_CONFIGURED（不再 502）
A7  归属者查询抛错 → 不影响会话用户档
A8  源码级：_bot() 不含 .read_stored_token(、不含 bypass_path_b=True
A9  用例：tests/api/test_git_credential_token_plane.py 全绿（14 条）
A10 行存在 + 值非空 + bot_token_status='expired' → 424 BOT_CREDENTIAL_INVALID(stage=expired)
    （保留 R18.2.2 过期短路；未过期时正常返回 bot 值，短路不误伤）
A11 行存在 + 密文解不开 → 424 BOT_CREDENTIAL_INVALID(stage=undecryptable)，
    不构造 resolver、不调用用户档
A12 真库六组合（rc 有值 / 值空 / expired / 解不开 / 无 rc+path-B / 无 rc+path-A）
    × chat/devops 结果如上；每条都要有 git ls-remote 通过 + git push --dry-run 不 403
    （只看 HTTP 200 不算通过：username 错时 200 也会在推时 403）
A13 表归属副本审计：非 owner 库的 owner 表 BASE TABLE 副本
    → 0 行分类 empty_copy、非 0 行分类 divergent_copy；VIEW 形态不报；本域表不报
A14 清理安全：默认只告警不删（S9）；GRAPE_TABLE_COPY_CLEANUP=1 时只删 empty_copy，
    divergent 一律保留并告警（S10）；非法表名拒绝执行（S8）
A15 迁移域三级判定：显式 env 优先；env 缺失/脏值 → 库名推断；判不出 → 角色映射；
    单库模式仍返回空串（S1–S6）
A16 worker 角色不执行域迁移（S7），但域判定本身仍返回 runtime
R2.15 `session_key` 不得被写成代码级唯一契约（2026-11-21 修正，原实现已还原）：
       原设计允许多枚并存；签发原语必须是「裸 INSERT 优先 → 撞唯一键（SQLite
       `UNIQUE constraint` / MySQL `1062`）才降级 UPDATE」，**不得**用无条件 upsert
       —— 那会把某一部署的物理约束升成全局语义，抹掉本合法的多行形态（chat 的 conv 级 key 就靠它）。
       迁移 `202611210004` 只做 MySQL 列宽对齐（TEXT → VARCHAR(191)，因为部署侧可能的
       `uniq_session_key` 要求定长列），**不得**去重、**不得**建 uniq_session_key；
       已建过索引的部署保留现状（代码按「可能存在的物理约束」兼容），要还原原设计需人工
       `ALTER TABLE session_api_tokens DROP INDEX uniq_session_key`。
       存在性判据仍不得用 UPDATE 的 rowcount（R2.6 家族）。

A17 meta upsert 幂等：同值重写两次不抛异常、行数恒 1（SQLite S1/S2 + MySQL 真库探针）
A18 方言翻译：meta upsert 翻成 ON DUPLICATE KEY UPDATE 且 KEY 不被反引号化（S1–S4）；
    OAuth 渠道同步不再调用任何 meta 写入（S4 源码锁）
A19 取卡故障分码：服务端 error / 5xx → `platform_card_contract_unavailable`，
    卡正常时派生结果一字不变；默认超时 10.0s，只重试传输类失败（S1–S9）
```