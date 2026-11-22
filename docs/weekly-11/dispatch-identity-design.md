# 设计：派发身份按 id 认人（默认 on，可切 shadow 观察）

> 需求：`dispatch-identity-requirements.md`｜用例：`tests/mgmt/test_dispatch_identity_scenarios.py`（23 条）

## 1. 落点（领域建模 + ORM + 契约 + 小文件）

```
mgmt/auth/dispatch_identity/           领域包（模型与服务分离，单文件 ≤ 104 行）
  models.py   DispatchMode / IdentitySource / DispatchIdentity(frozen dataclass 值对象)
              · applies = mode==ON and user_id 非空（是否生效只看它，调用方不再自己拼条件）
              · parse() 对非法开关值回落 SHADOW
  service.py  resolve_identity() / dispatch_identity() / current_mode()
              · 账号读取走 ORM：AccountRepository.get_by_username(session, name)
              · 引擎：kernel.persistence.engine.get_domain_engine("mgmt")
                （本进程域=mgmt → 进程主库；其它进程 → GRAPE_DSN_MGMT；单库 → 回落）
              · username→id 进程内 TTL 缓存（默认 60s，GRAPE_IDENTITY_CACHE_TTL 可调，上限 1024）
              · mode=off 不查库；查库异常 → 空身份（不打断派发）

mgmt/user/task_token_identity.py       取令牌侧（53 行）
  user_identity_token(task, repo_key) → (token, source)
  · 只有 ON 且 applies 才取；github → get_user_github_token_by_id，gitcode → get_user_gitcode_token
  · 其它平台返回空（不猜），由调用方回落原链

runtime/pipeline/dispatch_identity.py  runtime 适配（28 行，唯一消费 mgmt 契约处）
  identity_for_actor(account, actor) → (可写 uid, source)
  · shadow/off 返回空（行值仍 0）；异常不外抛

改动面（全部小改）
  mgmt/user/db.py::get_task_token          +20 行（链首 rc_bot 探测 + 派发身份调用 + 兜底注释）
  runtime/pipeline/event_pipeline.py       +30 行（含 _dispatch_ident 的 docstring），业务只一行
  mgmt/public/__init__.py                  +27 行（三个契约函数，返回值对象）
  kernel/persistence/engine.py             +35 行（get_domain_engine：跨域 ORM 的域引擎）
  新文件合计 243 行，最大单文件 104 行（无巨石）
```

## 1.1 性能取舍

```
· 每次派发最多一次判定；mode=off 直接返回，零查询
· 同一账号 TTL 内只查一次库（派发高峰下把 N 次查询压到按账号数计）
· ORM 引擎按域缓存（pool_size=2, max_overflow=2, pool_pre_ping），不复用会话
· 缓存上限 1024 条，超出整体清空（简单、可预期；不做 LRU 是为了少一把锁）
```

## 2. 为什么默认 on（而不是先 shadow）

```
按 id 认人是 Issue #21 的直接修复项：默认 shadow 会让 hello-blog 依旧取不到令牌、推不了分支，
也就是"改了但没用"。所以默认 on：
  · 未设置 → ON（部署重启即生效，不需要额外环境变量）
  · 显式 shadow → 只记录（想先看 source 分布时用）
  · 显式 off → 一键回滚（不解析、不写 id、不取个人令牌）
  · 无法识别的值 → shadow（故障安全：拼错宁可不生效）

风险控制（on 默认下仍然成立）：
  · 解析不到 id（任务无 user_id 且账号反查不到）→ 回落原链，行为与改动前一致
  · 顺序固定：任务行 token → 链首 rc_bot（显式配置）→ 派发人令牌 → 原链（仓库 bot → 平台 bot → 归属者）
  · user_id 只影响"取谁的令牌 + 派发落库值"，不改技能归属/调度的既有语义
  · 账号查询走 60s TTL 缓存，默认 on 不会把每次派发都变成一次 DB 查询
```

## 3. 解析顺序与理由

```
1) task.user_id        显式派发，最可信（但历史多为 0，所以不能只靠它）
2) account_name        事件发起人（a2a_event 走 payload.account→actor→Issue creator；
                       其它事件走 actor→payload.account→Issue creator —— 沿用既有 resolve_actor_account，
                       不在本模块重写优先级）
3) actor_name          与 2 同源的第二候选（account 为空时）
4) ""                  交回原链（仓库 bot → 平台 bot → 归属者）
```

注意：**不**按 repo_key 的 owner 名字反查 —— 那正是 Issue #21 的错因（owner 名 = yangaiwu，
命中 id=33「有名字无身份」，而真正有 GitHub 身份的是 id=2）。

## 4. 风险与回滚

```
风险 1  on 默认下每个 direct 任务一条 bound 日志（量可控，含 id/source）
风险 2  历史任务 user_id=0 占比高（≈11%）
       → 只对新派发生效；老任务仍走原链
风险 3  on 模式下个人令牌是 PAT，可能被平台判为"非 bot 身份"
       → 保留原链作为回落；回写过期标记仍走 RepoBotTokenHandler（rc_bot 同一列）
回滚    GRAPE_DISPATCH_USER_ID=off（无需改代码、无需重启以外的操作）
```

## 5. 用例映射（23 条）

```
S0 未设置=on 且生效            S1 off 空返回无日志
S2 显式 shadow 只记日志         S3 on 生效                     S3b 非法值=shadow
S4 task 优先                   S5 account→actor 顺序       S6 全空
S7 派发侧 on 三元组            S8 派发侧 shadow 行值为 0   S9 认人抛错不阻断
S10 shadow 取令牌走仓库 bot    S11 on 个人令牌优先         S12 on 无个人令牌回落
S12b gitee 不猜直接回落        S13 public 契约两面可用
另含：S6b 哨兵判定、S8b 适配器默认给 id、S10b 默认走个人令牌、S13b 缓存 TTL=0
     S14 ORM 失败降级、S15 契约返回值对象
```

## 6. 非目标

```
· 不改技能归属/引擎调度/统计对 user_id 的既有用法（只把值填对，语义不动）
· 不回填历史任务（新派发生效即可）
· 不在本模块做平台级 fallback 结构改造（github/gitcode 的 getters 保持现状）
```

## 7. 3库4进程与跨域契约（本实现如何满足）

```
分层     dispatch_identity/ 包放在 mgmt/auth（能力归宿主域：账号数据在 mgmt，不放大到 kernel）
        连接选择封在 mgmt/user/account_session.py（owner 自己的数据访问实现）：
        契约调用方（runtime）只 import mgmt.public，不知道域/DSN/引擎的存在
         runtime 侧（event_pipeline）只 `from src.domains.mgmt.public import dispatch_identity`
         全仓无 new kernel 依赖、无跨域直连表
连接     解析走 mgmt 自己的 grape_account_db → _mg_db 代理：
           本进程域=mgmt → 进程主库；其它进程 → GRAPE_DSN_MGMT；单库 → 回落
         契约调用方（runtime）不持有 mgmt 连接、不写裸 SQL
写面     agent_tasks 属 runtime 域，由 runtime 自己写；本改动不产生任何跨域写
进程面   mgmt 进程承载账号解析（契约内部路由），runtime 进程承载派发与任务落库；
         四进程拓扑、重启面无变化
```

### 7.1 实测（runtime 进程视角，注入三库 DSN，只读）

```
current_domain = runtime
mode(未设置)    = on
resolve(account=conv_user) = user_id=5 source=account   ← 契约跨进程取到 mgmt 真实账号
resolve(task_id='t-1')     = source=task
dispatch(默认)  = user_id=5 applies=True + 日志：
  [dispatch-identity] bound user_id=5 source=account account=conv_user
```

### 7.2 契约面清单

```
mgmt.public.dispatch_user_id_mode()     开关读取（off/shadow/on）
mgmt.public.resolve_actor_user_id(...)  纯解析（不看开关）
mgmt.public.dispatch_identity(...)      派发侧入口（默认 on 生效；shadow 只记日志）
调用点：runtime/pipeline/event_pipeline._dispatch_ident（唯一消费方）
取令牌侧：mgmt/user/db.get_task_token（同域，不跨域）
```


## 8. 评审修复（2026-11-22，A1/A2）

```
A1 取令牌顺序倒置 → 已修
   前：repo_gitcode_token（空）→ 派发人个人令牌 → 原链（链首才读 rc bot）
       已正确配置 rc bot 的仓库会改用派发人 PAT（PR 作者从 bot 变个人）
   后：get_task_token 先用链首 ExplicitRepoBotToken.resolve 精确读一次 rc 行：
       可用 → 直接返回；不可用（expired/undecryptable/empty）→ 记 warning 后继续看派发人令牌
       （不在此硬失败：真因仍由建树面 resolve_exec_token 归因，避免抢走失败分类）
   用例：tests/mgmt/test_task_token_order_scenarios.py S1–S6

A2 种子失败被吞 → 已修
   前：建表迁移 try: seed except warning → 种子失败仍盖章 → 「表有、分类没有」且不再重试
   后：种子失败直接抛出（框架不盖章 → 下次启动重试）
   用例：同文件 S7
```

C3 过期→任务 failed → 已按“不能直接失败”回滚（2026-11-22 二次评审）
   前：链首 ExplicitRepoBotToken 遇 expired 返回 stage=expired → 建树归因 → 任务 failed
       （一次标记滞后就会把本来能跑的任务判死）
   后：expired = 「跳过本级」——resolve()/stage() 返回空，落到下一档（RepoBot/GlobalBot/归属者），
       并记一条 warning。基线语义本来就是这样（RepoBotToken 遇 expired return ""）。
   配套：鉴权面（get_repo_bot_token_no_global）单独查 expired()，仍把过期旧值挡在门外
       （read_stored_token 不查过期，这一面不能跟着松）。
   用例：guard5（两入口都落到归属者、无 stage）+ task 侧两条（降级不归因 / 下一档也空则如实归因）

D1 缺 DSN 静默回落 → 已修（2026-11-22 二次评审）
   前：get_domain_engine 缺 DSN → 回落 get_engine()（进程主库）→ 跨域读打到错库 →
       查不到账号 → 返回空身份；日志只有 debug，与“这人没账号”不可区分。
   后：缺 DSN 且进程域 ≠ 目标域 → 抛 DomainEngineUnavailable（**不回落主库**）；
       契约层（dispatch_identity.service）捕获 → 按域只告警一次（warning）+ 降级空身份。
       单库拓扑（无进程域）与本域进程仍走主库 —— 那本来就是对的。
   用例：tests/mgmt/test_account_contract_routing.py S1–S5（路由/缺 DSN/一次告警）
        + S6 架构棘轮：runtime 只允许 mgmt.public，基线已有的 6 个违约点登记在案、不得新增
