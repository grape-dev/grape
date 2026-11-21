# 设计：会话作用域的场景化判定（Scene → Policy → Decision）

> 需求：`session-scope-scene-policy-requirements.md`｜代码：`src/kernel/scene_policy.py`
> 用例：`tests/kernel/test_scene_policy.py`（S1–S14）、`tests/runtime/test_session_scene_scope.py`（S1–S8）

## 1. 现状盘点（逐行，13 处 + 3 类）

| # | 位置 | 现在的判定 | 场景 | 状态 |
|---|---|---|---|---|
| 1 | `repos.py:3044`（`_repo_platform_token`） | 我们在 11-21 加的 provider+owner/repo 比对 | `GIT_CREDENTIAL` | **已改为 scene_policy** |
| 2 | `repos.py:2871`（project-info） | `split_owner_repo` 逐段比，空作用域跳过 | `PROJECT_INFO` | **已改为 scene_policy**（空作用域仍兼容） |
| 3 | `issues_center.py:111` `_require_session_repo_token` | `verify_session_token_for_repo` 严格 `==` | `TASK_NOTIFY`（默认） | **已改为 scene_policy**；chat/跨仓按可见权 |
| 4 | `tasks.py:488`（complete） | `session_key == task_id` 且 `repo_key == 任务仓` | `TASK_ACCESS`（自省） | 维持（自省不跨仓） |
| 5 | `tasks.py:545`（tasks/current） | `repo_key == 任务仓` | `TASK_ACCESS` | 维持 |
| 6 | `tasks.py:621` `_require_repo_access` | 会话：`repo_key == 目标仓`；用户：托管集合 | `TASK_ACCESS` | 维持 |
| 7 | `issues.py:1773/1824` | 会话 token 缺作用域 / 不匹配 | `ISSUE_OP` | 待迁移 |
| 8 | `issues.py:1880/1889` | 同上（另一组端点） | `ISSUE_OP` | 待迁移 |
| 9 | `skills.py:1194` | 会话 token 仓 ≠ 目标仓 | `SKILL_UPLOAD` | 待迁移 |
| 10 | `plugins.py:1095` | 同上 | `SKILL_UPLOAD` | 待迁移 |
| 11 | `webhook.py:1035-1039` | `verify_session_token_for_repo` | `EVENT_STREAM` | 待迁移 |
| 12 | `events.py:2260-2264` | 同上 | `EVENT_STREAM` | 待迁移 |
| 13 | `org_dispatches`（无校验） | 无 | `DISPATCH` | 待迁移（现状是"该收的没收"） |

现场数据（只读实查，本机）：`session_api_tokens` 空 repo_key **0 行**；
chat 的 token 由 `chat.py:1055/1362/2297` 以 `chat:<user_id>` 签发 —— 这决定了
「空作用域」与「chat 伪作用域」必须分开处理：前者是历史兼容，后者是 chat 的正常形态。

## 2. 设计

### 2.1 三段式：Scene → ScenePolicy → Decision

```python
class Scene(str, enum.Enum):
    GIT_CREDENTIAL / PROJECT_INFO / ISSUE_OP / SKILL_UPLOAD / EVENT_STREAM /
    TASK_NOTIFY / TASK_ACCESS / DISPATCH

@dataclass(frozen=True)
class ScenePolicy:
    scope: str                    # repo_bound | task_plane | service_only
    credential_source: str        # explicit_rc_first | resolver_chain | none  ← #3713 的落点
    allow_cross_repo: bool
    allow_pseudo_scope: bool
    legacy_unbound_ok: bool       # 空 repo_key（历史 token）是否按旧行为放行
    require_same_provider: bool
    code_pseudo / code_mismatch / code_cross / code_provider

decide(scene, session_repo_key, target_repo_key, *, target_provider="", access_check)
    -> Decision(ok, code, message, cross_repo, pseudo_scope)
```

调用点只写一句：`decide(Scene.X, session["repo_key"], target_rk, access_check=...)`，
不再出现任何 `if token.repo_key != ...`。

### 2.2 判定顺序（决定错误码可读性）

```
① 场景未登记            → SCENE_NOT_REGISTERED（逼调用方声明，拒绝隐式默认）
② service_only 场景收 tk_ → SESSION_NOT_ALLOWED_FOR_SERVICE
③ 空作用域 + legacy_unbound_ok → 放行（历史兼容；存量清零后置 False 收口）
④ 同 owner/repo + 同平台 → 放行
⑤ 同 owner/repo + 不同平台 → PROVIDER_SCOPE_MISMATCH（同名不同平台 = 两套凭据）
⑥ 伪作用域（chat:<uid>）
      repo_bound → SESSION_NOT_REPO_BOUND（提示"先绑仓"）
      task_plane + 可见权 → 放行（cross_repo=true, pseudo_scope=true）
      task_plane 无可见权 → SESSION_NOT_REPO_BOUND（带"用户无可见权"说明）
⑦ 真作用域但不同仓
      task_plane + 可见权 → 放行（cross_repo=true）
      task_plane 无可见权 → CROSS_REPO_DENIED（带双方 repo_key）
      repo_bound       → REPO_SCOPE_MISMATCH
```

### 2.3 chat 为什么必须单独一类（而不是"放宽一点"）

```
chat 的两种 token：
  选仓   → repo_key = 真实仓            → 同仓场景照常通过
  未选仓 → repo_key = chat:<user_id>    → 任何 `==` 都不可能命中
所以：
  · 同仓场景（换券/project-info/技能上传）对 chat 伪作用域必须**拒绝**，
    但要给 SESSION_NOT_REPO_BOUND，让 agent 知道"去绑仓"，而不是"作用域不匹配"；
  · 任务平面（notify / 下派）chat 代表用户本人操作，授权依据是**用户对目标仓的可见权**，
    不是 token 的仓绑定 —— 否则 chat 永远派不了任务（本次现场即此）。
可见权口径：宿主域能力 `mgmt.public.user_can_access_repo(user_id, repo_key)`
（内部 = `get_managed_repos` 含目标 repo_key，JOIN user_repos，成员也可见）。
判定不出（异常/无 user_id/无 DSN）一律按无权。
```

### 2.4 取券口径留位（#3713）

`credential_source` 在同一张表里声明：换券 = `explicit_rc_first`，事件/issue = `resolver_chain`。
#3713 的 `ExplicitRepoBotToken` 落地时，链首级按场景选，调用点零改动 —— 这正是"不同场景不同取值逻辑"
收口到一处的位置。

## 3. 已改三处（首付）

```
repos.py::_repo_platform_token      换券：Scene.GIT_CREDENTIAL
  403 body 增加 error_code；chat:<uid> → SESSION_NOT_REPO_BOUND；
  空 repo_key → legacy_unbound_ok（旧行为，存量清零后关）
repos.py::get_repo_project_info     Scene.PROJECT_INFO（同仓；空/伪作用域码可判定）
issues_center.py::_require_session_repo_token
  改为 verify_session_token + decide(默认 TASK_NOTIFY)；
  跨仓/chat 用 access_check（mgmt.get_managed_repos）放行；其余与自省类维持 repo_bound
```

## 4. 用例映射

```
tests/kernel/test_scene_policy.py        S1 同仓 / S2 跨仓 / S3 平台不同 / S4 伪作用域拒绝
                                         S5 project_info / S6 跨仓+可见 / S7 跨仓+不可见
                                         S8 chat+可见放行 / S9 chat+不可见 / S10 自省不跨仓
                                         S11 未登记 / S12 取券口径 / S13 伪作用域识别 / S14 空作用域兼容
tests/runtime/test_session_scene_scope.py S1 chat+可见放行 / S2 chat+不可见 403 /
                                         S3 同仓 / S4 跨仓下派放行 / S5 跨仓拒绝 /
                                         S6 自省不跨仓 / S7 缺 token / S8 非法 token
tests/api/test_git_credential_authorization.py  S1–S10（含 S6 空作用域兼容、S10 chat 拒绝）
```

## 5. 迁移清单（不做一次性铺开）

```
下一批（按风险高低）：
  1) issues.py 1773/1824/1880/1889 → Scene.ISSUE_OP（同仓，纯替换）
  2) skills.py/plugins.py → Scene.SKILL_UPLOAD（同仓）
  3) webhook.py / events.py → Scene.EVENT_STREAM（同仓）
  4) org_dispatches → Scene.DISPATCH（跨仓，需 dispatch 关系 + 可见权）——现状无校验，属补缺口
每批要求：既有断言不动；新增用例对旧码必红（先写下失败再改）
收口信号：13 处全部走 scene_policy，代码里不再出现"token.repo_key =="这类判定
```

## 6. 边界

```
· 不改 token 签发结构（scope_kind 字段下一步）；用 `chat:` 前缀 + 空值区分三类语义
· 不放宽 repo_bound 场景（除了历史空作用域这一条兼容开关）
· access_check 只认"用户可见权"，不认"关系表"；dispatch 关系证明随 #2112 迁移批次补
· 不把 HTTP 语义塞进 worker：任务面只用 error_code 分类，渲染各自做
```

## 7. 3库4进程与跨域契约（本实现如何满足）

```
分层        src/kernel/scene_policy.py = 纯策略（无 DB / 无 domain import，grep 为空）
            → mgmt / runtime / insight 三个进程共用同一份判定，不引入跨域依赖
跨域能力    需要「用户是否可见目标仓」，这属于宿主域（mgmt）的能力：
            新增 src/domains/mgmt/public/__init__.py::user_can_access_repo
            → runtime 只 `from src.domains.mgmt.public import user_can_access_repo`
            内部按域连接路由（db_registry._MgmtDbProxy）：
              本进程域=mgmt → 进程主库；其它进程 → GRAPE_DSN_MGMT；单库 → 回落
            不直接 JOIN managed_repos/user_repos，不写裸表名，不做 DSN 反查猜域
SQL 面      本次改动在 runtime 侧 0 条 SQL；mgmt 侧只新增一个契约函数，无新表
进程面      mgmt 进程承载换券/project-info；runtime 进程承载 notify；
            跨进程取值靠契约 + DSN，四进程拓扑与重启面不变（无新进程/新端点）
```

### 7.1 真机实测（本机 38081 = runtime 进程 env 注入，只读调用）

```
A. 三库 DSN 齐备
   current_domain = runtime ; uses_own_dsn("mgmt") = True
   真实组合(uid=1, managed∩user_repos 共 21 行) → True
   陌生仓 → False ; 空 user_id → False

B. 摘掉 GRAPE_DSN_MGMT（模拟漏配 / 单库拓扑）
   uses_own_dsn("mgmt") = False
   日志：get_managed_repos join failed: no such table: organization
   契约返回 False → 拒绝，而不是静默去本进程库查一张不存在/同名的表

C. mgmt 进程内视角走进程主库（current_domain()=='mgmt'），由运行中的 mgmt 进程保证；
   独立脚本无法复现主库 wiring，故不以此单独下结论（避免把"脚本没连对库"当成缺陷）。
```

### 7.2 复用既有能力（不重造）

```
runtime/im/handler_async.py::_autopilot_user_allowed 原本自己 `get_managed_repos + any()`；
本次改为委托 user_can_access_repo —— 可见性口径只剩宿主域一份实现。
（此前该私有函数是唯一一处，另一处就是本次新增的 notify 守卫：属于重复实现，已合并。）
```


## 8. 附：user_accounts 旧账号表清退（2026-11-22）

现网事实（运维给出）：生产 user_accounts **0 行**；归档库 grape_system_db.user_accounts 94 行（回滚用，
用户名 94/94 在新表可命中）；新表 grape_accounts 232 行；孤儿凭据/悬空身份 0。

本轮删除（纯死代码，零调用/零命中）：

```
user/db.py
  · @_deprecated_user_accounts_fn 装饰器
  · get_user / get_user_by_username / list_users
  · get_user_account / get_all_user_accounts / USER_ACCOUNT_FIELDS
  · get_user_by_gitcode_id / get_user_by_github_id
  · _USER_ACCOUNTS_DDL 及其在 init_db 建表注册
  · init_db 三段死迁移：user_credentials 旧表重映射、managed_repos(users) 重映射、
    user_accounts 废弃字段搬迁（读 webhook_url/oauth/bot_usernames）
  · migrate_accounts_from_json 里的 default 账户迁移块
user/legacy_column_migrations.py
  · _USER_ACCOUNTS_LEGACY_COLUMNS 及其补列计划项
  · remap_orphan_user_ids（引用 user_accounts 与真库不存在的 users 表）
  · cleanup_orphan_credentials 尾部的 managed_repos/users 映射块
```

仍保留、需先回填再切（Step 2，不做在本次）：

```
mgmt/public/__init__.py:  "user_accounts_e2eb04" / "user_account_username_by_id"
                          （autopilot 任务创建者、沙箱 owner 名回显）
mgmt/public/__init__.py:  usernames_by_ids()
mgmt/routes/sandbox_admin.py: JOIN user_accounts 的超管判定
src/api/db_setup.py:      老库升级语句（个人组织迁移 / default 账户 / publish 列 / alter 列表）
configs/table_ownership.json: user_accounts 归属与禁读域（随表 DROP 一并删）
```

判据：现网该表 0 行 ⇒ 上述读路径当前返回空（已破，不是"改了才破"）；切到 grape_accounts
前必须先把历史 created_by/user_id 的旧 id 回填成新 id（映射可用归档库 94 行 × 新表 username 闭合）。


### 8.1 读路径清退（2026-11-22 第二批）

```
mgmt/public  "user_account_username_by_id" 的 SQL 改读 grape_accounts（函数名保留，调用点不改）
             删除死键 "user_accounts_e2eb04"（无消费方）
mgmt/routes  sandbox_admin 超管兜底 JOIN 改读 grape_accounts
db_setup     删除两个死迁移函数（调用点早在 Issue #343 被注释）：
             _migrate_create_personal_orgs / _migrate_data_to_personal_orgs
             删除 default 账户迁移块、publish 列补列块、last_scene 补列块
migrations_ddl 删除 4 个旧表常量与 5 条 ALTER
kernel/db.py   表注册表去掉 user_accounts（与 table_ownership.json 保持一致）
configs/table_ownership.json  去掉 user_accounts 的归属与跨域可见项
```

结果：src 里已无任何 SQL 触达该表（只剩一行历史注释）。
语义变化只有一处：旧小整数 id 若只存在于旧表，现在解析为空 —— 与现网（旧表 0 行）此前表现一致，
且 id 归一由数据侧负责。
