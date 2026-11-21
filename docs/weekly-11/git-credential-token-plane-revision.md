# git-credential 的 token 平面修订（D90 / R18.2.x / S118）

> 2026-11-21。起因：gitee 托管仓库 `git-credential` 恒 502，现场判据是
> `Missing token` / `仓库 token 解析失败`。本文把「初始设计 → 现状实现」的三层漂移写清，
> 给出修订条款与已落地的实现，供 D90 / 04-token-resolver / S118 三处对齐。

---

## 1. 初始设计（2026-10-20，db2a86a90 同时入库的两份文档）

### 1.1 需求主线 `docs/weekly-08/issue-372-user-token-only-requirements.md`

```
R1.1/R1.5  路径 A（有全局 Bot Token，向前兼容）→ 走原流程
           路径 B（无全局 Bot Token，降级）→ 跳过 bot 邀请/接受/自查，用户 token 建 webhook
           判定：global_bot_token = get_global_gitcode_bot_token()
                 if global_bot_token: A  else: B
R1.3       路径 A 托管 → host_user_id=0；路径 B 托管 → host_user_id=当前用户 ID
R2.1       get_repo_token_with_refresh(repo_key)：
             1. managed_repos.bot_gitcode_token（仓库级 bot，有值即返回，不刷新）
             2. _get_global_bot_token()（全局 bot，有值即返回，不刷新）
             3. managed_repos.host_user_id → get_user_token_with_refresh（带刷新）
R2.3       task 行 repo_gitcode_token 路径 B 写空，运行时实时获取
```

**关键语义**：`host_user_id` 只回答"降级时用谁的用户 token"，**不是"禁止使用 bot"的开关**；
取用顺序是 `repo bot → global bot → 归属者 user token → 空`。

### 1.2 支线提案 `docs/weekly-08/token-config/04-token-resolver.md`

```
R18.2.2  RepoBotToken   — 含 host_user_id 守卫 + 过期短路
R18.2.3  GlobalBotToken — 含 host_user_id 守卫 + 过期短路
R18.2.4  UserToken      — 含 OAuth 自动刷新 + status=4 不短路（best-effort）
R18.3.3  支持跳过特定策略（如 get_repo_bot_token 跳过第 1 级）
```

支线把 `host_user_id` 从"降级身份"升级成"平面守卫"，正好把需求主线的 bot-first 反过来。
**两份文档同属一个 commit，口径相反；代码最终按支线实现。**

---

## 2. 三层漂移（实测证据，均可在当前 head 复验）

### 2.1 第一层：策略层给 bot 加 path-B 守卫

```
src/kernel/token_resolver.py:941   GlobalBotToken._fetch()
    # 路径 B 仓库跳过全局 bot token（§13.1）；回帖身份解析允许 bypass。
    if self._is_path_b(repo_key) and not self.bypass_path_b:
        return ""

src/domains/mgmt/user/db.py:2279    _repo_bot_token_value()
    # R12.12 同口径：路径 B 仓库跳过仓库级 bot token（降级场景脏写入防护）
    if str(get_host_user_id(repo_key) or "") not in ("", "0"):
        return ""
```

注意拦截面不同：**仓库级是 db 层 + 策略层双重拦截，全局级只有策略层**。
绕过 resolver 直接调 `get_global_bot_token_for()` 仍能拿到全局 bot。

### 2.2 第二层：A/B 从"运行时判定"固化成"建仓快照"

- 写 `host_user_id` 的那一刻就定了平面；
- 之后管理员再配仓库级/全局 bot，`_is_path_b()` 仍为真 → bot 永远用不到；
- `managed_repos` 写入层还专门为此加了"路径 A 成功时清空 host_user_id"（db.py:1765 `R14.15`），
  说明这个字段确实被读取层当平面用。

### 2.3 第三层：git-credential 取错接口（67ed59fb4，2026-11-13）

```
src/domains/mgmt/routes/repos.py:3086（改前）
    return TokenResolver(create_provider(provider)).read_stored_token(rk) or "", "", None
```

`read_stored_token` 的两个语义都不适合 git 凭据：

```
① 它按标准策略链跑（RepoBot/GlobalBot 带 path-B 守卫）→ path-B 仓库恒空；
② UserToken._fetch_stored 只读 external_identities.access_token（OAuth），
   gitee 这类只有 PAT、无 OAuth 身份的平台恒空；
③ 契约本身写明"不检查过期、不触发 refresh"，而 git 推送最怕过期 token。
```

同时 `_user()` 只看**当前会话用户**，而 agent 的 `tk_` 会话 `user_id` 常为 0 →
`get_user_token_for(..., user_id="")` 直接跳过凭据域 PAT 分支 → 整条链断。

---

## 3. 修订条款

### D90 新增：N-「git 凭据的平面判定以凭据可用性为准」

```
N-x.1  平台 token 平面（bot / user）不是仓库的固有属性，必须在**每次取用时**按
       凭据可用性判定；不得由建仓/托管那一刻写下的字段（managed_repos.host_user_id）
       单向决定。
N-x.2  host_user_id 的语义限定为「降级身份提示」：当且仅当**没有该档凭据**时，
       用它指向的账号取用户凭据。
       限定：「不可用」不等于「没有」——显式配了 rc bot 行但值取不出/已过期时，
       报 424，**不进入降级**（否则机器身份与用户身份混用，审计与署名失真）。
N-x.3  代理型操作的 bot 解禁必须有**闸**，且闸是双信号：
         `_has_bot_row` —— 该仓是否显式配置了 rc bot 行（显式意图）
         `_gate_bot`   —— 该值此刻是否可用（value 非空）
       开闸后**只认仓库级 bot**（直取其值），**不得经策略链**：策略链会把
       RepoBot 与 GlobalBot 两级一起解锁，而全局 bot 通常不是该私仓协作者
       → git push 403；且因返回的是非空 token，「失效即报错」的分支不会触发，
       失败推迟到上游，比 502 更难定位（看着走得通，身份错了）。
       行存在但值不可用 → 报 424，不静默降级到用户身份（机器/用户身份不得混用）。
N-x.4  read_stored_token 的用途不变（A2A 鉴权 / session 写入）：它逐策略调
       `_fetch_stored`，不刷新、不看过期，UserToken 只读 external_identities 的
       OAuth access_token。git 凭据不得使用该接口（gitee 这类只有 PAT 的平台恒空）。
N-x.5  身份归属按**会话语义**分序，不按仓库归属分序：
         chat（人机对话）  会话用户 → 归属者 → bot        「谁在说话用谁的凭据」
         devops（事件驱动）仓库 bot → 归属者 → 会话用户    「事件归属仓库」
       两者共用同一批取用函数，仅顺序不同（chat_first 开关）；chat 不得替仓库
       owner 代签（否则等于权限放大，审计上看起来是 owner 干的）。
```

### R18.2.2 / R18.2.3 修订

```
原：RepoBotToken  — 含 host_user_id 守卫 + 过期短路
新：RepoBotToken  — 含过期短路；path-B 守卫在 bypass_path_b=False（默认）时生效。
    **git-credential / clone / push 不使用该参数**：它们要么走标准链（无显式 rc bot 时），
    要么直取 rc 值（有显式 rc bot 时），见 N-x.3 与 §4。
    bypass_path_b 的唯一合法用途是回帖链 get_bot_identity_token（评论必须来自 bot 账号）。

原：GlobalBotToken — 含 host_user_id 守卫 + 过期短路
新：GlobalBotToken — 同上；并明确「全局 bot 守卫只在本层，db 层不重复拦截」
    （当前仓库级在 db 层也拦了一次，两处语义必须同口径，否则绕过 resolver 行为不一致）
```

### R18.3 补充条款

```
R18.3.4  git-credential 的取用顺序（显式四档，任一层命中即返回）：
          devops（事件） ① 显式 rc bot（直取值，不经策略链）
                         ② 归属者 host_user_id 的用户凭据（PAT 优先，再 OAuth）
                         ③ 当前会话用户的凭据
          chat（对话）   ① 当前会话用户  ② 归属者  ③ 显式 rc bot
          无显式 rc bot 时，①/② 之间插入既有标准链（path-A 由全局 bot 命中，
          path-B 因守卫跳过两级 bot）——这条路径与改前完全一致。
          全空 → 424 CREDENTIAL_NOT_CONFIGURED
          rc 行存在但值不可用 → 424 BOT_CREDENTIAL_INVALID（不降级、不落全局 bot）
```

### S118 用例改法

```
原 S118 结构守卫    断言「host_user_id 非空时 RepoBot/GlobalBot 返回空」
新 S118 结构守卫    分两态：
                      bypass_path_b=False → 仍返回空（执行链语义不变）
                      bypass_path_b=True  → 必须返回 bot（代理型语义）
新增 S118-x          git-credential 四档顺序用例：
                      有 bot                 → bot，且不解析用户凭据
                      有 bot 但值不可用/过期   → 424 BOT_CREDENTIAL_INVALID（不降级）
                      无 bot + 有归属者        → 归属者凭据（user_id = host_user_id）
                      无 bot + 无归属者        → 会话用户凭据
                      全空                   → 424 CREDENTIAL_NOT_CONFIGURED
                      归属者查询抛错           → 不打断，继续落会话用户
```

---

## 4. 本次落地（已实现的代码）

`src/domains/mgmt/routes/repos.py`（config-only 平台分支，gitee/gitlab）

```python
# 双信号闸：行存在 = 显式意图；value 非空 = 值此刻可用
_bot_meta  = get_repo_credential_meta(rk, "bot") or {}
_bot_value = str(_bot_meta.get("value") or "")
_has_bot_row = bool(_bot_meta)
_gate_bot    = bool(_bot_value)

def _bot_expired(repo_key):                 # R18.2.2 过期短路（原始落点）
    return str(repo_bot_token_status_any(repo_key) or "") == "expired"

def _bot():
    if _gate_bot and _bot_expired(rk): return "", "", None   # 已标过期 → 外层 424
    if _gate_bot:      return _bot_value, "", None      # 直取 rc，不经策略链
    if _has_bot_row:   return "", "", None             # 配了但不可用 → 外层 424
    return TokenResolver(create_provider(provider)).get_token(rk) or "", "", None
    #     ↑ 无 rc：path-A 走全局 bot（12 个存量仓照旧活），path-B 由守卫跳过两级 bot

def _owner_user():     # 归属者：host_user_id → PAT/OAuth；username 空则回落仓库 owner 段
def _session_user():   # 当前会话用户
def _user(chat_first=False):
    _first, _second = ((_session_user, _owner_user) if chat_first
                       else (_owner_user, _session_user))
    ...
```

外层顺序（**真实控制流，不是 `or` 链**）：

```python
# _bot() / _user() 返回 (tok, stage, exc) 三元组 —— 元组恒真，
# 写成 `_user(...) or _bot()` 会让第二项永不执行，这是上一版文档的错处。
if user.get("agent_type") == "chat":
    tok, stage, exc = _user(chat_first=True)          # 会话用户优先（chat/Web）
    if not tok:                                       # ← 显式分支
        tok, stage2, exc2 = _bot()
        if not tok and not stage:
            stage, exc = stage2, exc2
else:                                                 # devops / 事件
    tok, stage, exc = _bot()
    if not tok and _has_bot_row:                      # 显式配了但不可用 → 不降级
        raise HTTPException(424, detail={
            "code": "BOT_CREDENTIAL_INVALID",
            "stage": stage or "empty",                # undecryptable / expired / empty
        })
    if not tok:                                       # 无 rc bot 行 → 用户档
        tok, stage2, exc2 = _user()
        if not tok and not stage:
            stage, exc = stage2, exc2

if tok:        log.debug("git_credential_ok", extra={"token_stage": stage})   # 审计：值可能同源
elif exc:      502（上游故障，带 stage）
else:          424 CREDENTIAL_NOT_CONFIGURED
```

**为什么不动 db 层守卫 / 不给 TokenResolver 裸传 bypass**：`_repo_bot_token_value` 的
拦截服务于「执行链不用非协作者 bot 打私有仓」，这条保护要继续；而 `bypass_path_b`
是整链解锁（RepoBot + GlobalBot 两级），粒度太粗 —— 用它等于把「换另一个 bot」
当成降级，与初始设计（拿不到 bot 才降级到用户）和 D90（机器/用户身份不混）都冲突。
本次的做法是**只有显式配置的仓库 bot 才能用，且用它就直接取值**，全局 bot 不参与
path-B 的任何分支。

## 5. 验收

```
单元用例  tests/api/test_git_credential_token_plane.py   14 passed
            源码锁：不含 .read_stored_token(、不含 bypass_path_b=True、闸双信号在
            显式 bot 值可用 → 直取，且 TokenResolver 零构造
            行存在 + value='' → 424 BOT_CREDENTIAL_INVALID(stage=empty)，resolver 未构造
            行存在 + 密文坏 → 424 BOT_CREDENTIAL_INVALID(stage=undecryptable)，不落用户档
            行存在 + status='expired' → 424 BOT_CREDENTIAL_INVALID(stage=expired)
            行存在 + 状态为空 → 正常返回 bot 值（短路不误伤）
            无 bot + devops → 归属者（user_id=host_user_id, provider=gitee）
            无 bot + chat  → 会话用户，不替归属者代签
            无 bot + path-A → 全局 bot（12 个存量仓照旧活）
            无 bot 无归属者 → 会话用户；全空 → 424 CREDENTIAL_NOT_CONFIGURED
            归属者 username 空 → 回落仓库 owner 段；归属者查询抛错 → 不打断
            get_repo_credential_meta 自身三态：无行=None / 解不开=undecryptable / 可用=value
```

真实库六组合（`grape_lfr_insight@192.168.0.164`，arch11 `:38080` 进程内 `bind_all()` 后实跑；
脚本打印实际连接的 `TABLE_SCHEMA`，因为 mgmt 库里那张同名表是空壳）：

```
case                              coding            chat            返回值
① rc 有值                          rc_bot            rc_bot          len=32 尾 60f6
② rc 行存在、value=''              424 empty         424 empty      —
③ rc 有值 + status='expired'       424 expired       424 expired    —
⑥ rc 密文损坏                      424 undecryptable 424 undecryptable —
④ 无 rc 行 + path-B(host 非空)      user_token        user_token     归属者 PAT len=32
⑤ 无 rc 行 + path-A(host 空)        global_bot        global_bot     平台卡 len=32
```

硬断言（只看 HTTP 200 不够 —— username 错时 200 也会在推的时候 403）：

```
git ls-remote  https://gitee.com/yang-aiwu-new/demo.git   （username=yang-aiwu-new + 返回 token）
               → rc=0，读到 HEAD 6b805e7 / refs/heads/master / feat/issue-...
git push --dry-run HEAD:refs/heads/probe-cred-check
               → rc=0，`* [new branch]`（dry-run，未落远端）
```

环境限制（必须与结论一起读）：

```
gitee 全环境只有一个 PAT（user 629a7971-...，尾 60f6），而
  demo 的 rc bot 值（2026-11-21 09:00:15 人工写入）= 该 PAT
  归属者 PAT                                     = 该 PAT
  平台卡 bot_token_enc（85 字符密文）解密后        = 该 PAT
三者同源 → 六组合的返回值完全相同，**档位只能靠逐策略命中判定**
（分别调 RepoBotToken / GlobalBotToken / UserToken 的 get_token），不能靠值区分。
所以本环境无法证明「bot 身份 ≠ 用户身份」这一点；要证须另配一个独立 gitee 账号的 PAT。
```

复原核对（本轮实测后已执行）：

```
测试仓 probe-owner / probe-nohost → DELETE（managed_repos 只剩 demo）
demo rc value → 复原为原 85 字符密文（解密 32 字符，undecryptable=False）
demo bot_token_status → 复原 ''
临时凭据文件（askpass / token）→ 已删
```

## 6. 相邻缺陷：表归属副本审计（2026-11-21）

本轮两次把 `grape_t_repo_credentials` 从 **mgmt 库** 查成「0 行」，据此判断「该仓没配 bot」——
实际数据在 insight 库（`grape_lfr_insight@192.168.0.164`）。**代码走契约是对的，错的是举证走裸 SQL。**

```
契约链    mgmt.get_repo_credential_meta → insight.public.repo_credential_meta
          → insight read_one_any('repo_cred_value_id')（连 insight 自己的 DSN）
裸 SQL    grape_lfr_mgmt.grape_t_repo_credentials   BASE TABLE，0 行（空副本，不报错）
          grape_lfr_insight.grape_t_repo_credentials 2 行（真数据）
```

副本是结构性的：单库时代每进程建全表 + `_migration_conn` 目标域 DSN 不可用时回落进程库 +
`_owned_skip` 只拦新建。落地两个原语：

```
src/kernel/table_ownership.py
  _domain_from(dbname)               库名/角色名 → 域（既有函数，副本审计与 DDL 守卫共用，
                                     不新增判定规则）；判不出域 → 返回空、不审计
  audit_foreign_copies(conn, dbname) 分类 empty_copy / divergent_copy，视图与本域表不报
  drop_empty_copy(conn, table)       只删 0 行，非 0 行抛错，表名走标识符白名单

src/api/db_setup.py
  _fn_table_ownership_copy_audit()   迁移 202611210005，默认只告警；
                                     GRAPE_TABLE_COPY_CLEANUP=1 才清理 0 行副本
```

真库结果（`grape_lfr_mgmt`）：别域副本 11 张 —— 10 张 `empty_copy`（含 `grape_t_repo_credentials`、
`agent_runtimes`、`takeover_sessions` 等）+ 1 张 `divergent_copy`（`meta`，4 行，owner=runtime）。
`meta` 那 4 个键 runtime 里都有且当前值一致，但历史上「往 mgmt 写 meta 不生效」踩过一次 ——
所以它禁止自动删。迁移盖章 `202611210005 / 2026-11-21 18:46:47`。

场景与证据全文：`docs/weekly-11/table-ownership-copy-audit-scenarios.md`（S1–S10）。

## 7. 相邻缺陷：迁移域判定只依赖 env（2026-11-21）

三库模式的迁移归属原本只由 `GRAPE_MIGRATION_OWNER` 一个 env 决定。缺失时
`current_migration_domain()` 返回空串 = **不筛域**：

```
mgmt 进程      以「全量迁移」形态跑，把别域 DDL 建进自己的库
event/insight  run_domain_migrations() 看到 owner 为空 → 直接 return，本域库永不迁移
```

实测后果：迁移 `202611200006`（`agent_runtimes.desired_plugins`，`domains=("runtime",)`）
把列建在 `grape_lfr_mgmt`（该表 0 行），而 `grape_lfr_runtime` 有 19 行却缺这一列，
连台账表都没有 → 引擎注册连续 `500 (1054, "Unknown column 'desired_plugins'")`。
mgmt 账本 18:14:27 却盖了章，形成「盖章在错库、列没建」的假绿。

落地（`src/api/db_setup.py`）：

```
current_migration_domain()  三级判定：显式 env → 库名推断（db_registry）→ 角色映射
run_domain_migrations()     ① 先拦角色 worker（与 event 共库，避免并发重复 DDL）
                            ② domain 改用推断版，不再裸读 env
单库模式                    仍返回 ""（跑全量），历史行为不变
```

场景与真库判据：`docs/weekly-11/migration-domain-inference-scenarios.md`（S1–S7）。
用例：`tests/api/test_migration_domain_inference.py`（7 passed）。

## 8. 相邻缺陷：meta 写入幂等与 OAuth 渠道真源（2026-11-21）

两条缺陷互为因果，实测症状是每次 mgmt 启动刷一条：

```
migrate_oauth_providers: sync from oauth-providers.json failed:
  (1062, "Duplicate entry 'oauth_providers_config' for key 'meta.PRIMARY'")
```

### 8.1 公共原语 `set_meta_value_any` 用 rowcount 判存在性

`src/domains/runtime/public/__init__.py`

```
旧：UPDATE meta SET value=? WHERE key=?  → rowcount==0 → INSERT → 1062
    MySQL 计「实际改变的行」：同值重写 = 0；SQLite 计「匹配行」→ 单测全绿
新：INSERT INTO meta (`key`, value) VALUES (?, ?)
      ON CONFLICT(`key`) DO UPDATE SET value=excluded.value
    （`key` 是 MySQL 保留字，必须反引号；方言层翻 ON DUPLICATE KEY UPDATE）
```

### 8.2 渠道定义被重复抄进 meta（1062 的触发者）

`grape_t_oauth_channels` 已承载全套字段：

```
provider / name / type / auth_url / token_url / user_url / emails_endpoint /
scope / pkce_enabled / id_field / login_field / name_field / avatar_field /
redirect_uri / brand_color / accept_header / client_id_enc / client_secret_enc /
repo_links_json / source / config_hash / enabled
```

而 `providers.py::_load_providers` 的顺序是「文件卡 → meta 逐键覆盖 → **表整卡覆盖**」
（D109：表就绪后 DB 表是运行时真源）。所以往 meta 写 `oauth_providers_config` 与
`oauth_<p>_client_id/secret` 只是空转，正是 8.1 的触发者。`_sync_providers_from_config`
改为只做一次来源声明（不再写 meta）；meta 的**读取**层保留给表未就绪的老部署。

### 8.3 真库才暴露的方言坑

单语句 upsert 上线 MySQL 后立刻 1064：

```
near '`key` UPDATE value=VALUES(value)'
```

`_backtick_key()` 给 meta 表 SQL 里的 `key` 加反引号以避保留字，保护了
`PRIMARY/UNIQUE/FOREIGN/INDEX KEY`，漏了翻译产物 `ON DUPLICATE KEY` →
变成 ``ON DUPLICATE `key` UPDATE``。已把 `ON\s+DUPLICATE\s+KEY` 并入保护集。

这条再次印证「SQLite 通过 ≠ 生产通过」：SQLite 根本不走这段翻译，
只有把翻译结果本身断言下来才挡得住（`tests/kernel/test_mysql_compat_upsert.py`）。

### 8.4 验证

```
单测  tests/api/test_meta_upsert_idempotent.py   4 passed（同值重写/改值/源码锁/不写 meta）
      tests/kernel/test_mysql_compat_upsert.py    4 passed（翻译断言含 ON DUPLICATE KEY）
真库  MySQL 8.0.43 探针：同值写两次 OK、改值写 OK、行数恒 1、回读正确
重启  12:34 起日志只有「OAuth providers source of truth is grape_t_oauth_channels」，
      最后一条 1062 停在 11:19（修复前）
```

## 9. 未做（需评审）

```
① 把 db 层 _repo_bot_token_value 的守卫改成"仅当没有任何 bot 凭据时生效"
   —— 会同时改变回帖/webhook/worktree clone 三处平面，需契约评审
② managed_repos 增加 credential_plane: auto|bot|user 显式位（立场 3）
   —— 状态稳定可审计，但要求一次性数据迁移与后台 UI
③ ~~rc 表增加状态列~~ —— **不需要**。状态位一直在 managed_repos.bot_token_status
   （R18.2.2 落点），由 event 进程的 bot_token_health 每小时探活产生、执行链 401 旁路
   写入，本轮已在 _bot() 里接上（_bot_expired）。剩下的只是它的固有特性：
   标记粘性（最长 1 小时 + 抖动才自动清）、rc 无行时不产生、event 进程不在时该轮无状态。
④ S118 结构守卫改造（按 §3 的新两态写法）
```

## 10. 关联

```
需求主线   docs/weekly-08/issue-372-user-token-only-requirements.md  R1.x / R2.1 / R2.3
支线提案   docs/weekly-08/token-config/04-token-resolver.md          R18.2.x / R18.3
部署契约   docs/weekly-11/arch11-four-process-env-ops.md
会话凭据   docs/weekly-11/session-token-idempotent-issue.md
```