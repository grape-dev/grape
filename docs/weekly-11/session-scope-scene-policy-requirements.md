# 需求：会话作用域的「场景化」判定（错误码 + chat 单独处理）

> 触发：#3713 评论里 `POST /api/tasks/notify → 403 会话 token 仓库作用域不匹配`（调用方是 chat），
> 以及 #1946 换券作用域收紧后暴露的同类面。
> 结论：不是某处写错，是**十来个端点各自用「token.repo_key == 目标仓」写了一遍判定**，
> 而不同场景（换券 / 派任务 / 自省 / 跨仓下派 / chat）本来就该有不同的授权依据。

## 1. 问题

```
现状   13 处作用域判定（repos / tasks / issues / skills / plugins / webhook / events），
       每处各写一套：有的 `==`，有的只在有值时比，403 文案还各不相同
后果 ① chat 的会话 token 是 `chat:<user_id>` 伪作用域（chat.py:1055/1362/2297），
        与任何真实 repo_key 永不相等 → notify / 换券必 403，且文案看不出"该去绑仓"
     ② 跨仓下派（源仓 agent 给子 issue 所在仓派任务）同样 403
     ③ 调用方无法区分「token 没绑仓」「真换了仓」「跨仓但关系不成立」「平台不同」
     ④ #3713 的取券口径分叉（rc 显式优先 vs 策略链）也是同一类问题的另一面
```

## 2. 目标

```
G1  一个判定点：场景（枚举）→ 规则（作用域 / 跨仓 / 错误码 / 取券口径）查表，调用点只声明场景
G2  chat 单独规则：伪作用域在「任务平面」按"用户对目标仓可见"放行；在「同仓场景」拒绝并提示绑仓
G3  跨仓下派显式化：允许，但必须有用户可见权（关系证明），否则 CROSS_REPO_DENIED
G4  错误码可判定：SESSION_NOT_REPO_BOUND / REPO_SCOPE_MISMATCH / PROVIDER_SCOPE_MISMATCH /
    CROSS_REPO_DENIED / SCENE_NOT_REGISTERED
G5  兼容：历史 token 没有 repo_key 的行（本仓实测 0 行）保持旧行为，策略开关可一键收口
G6  为 #3713 留位：场景同时声明取券口径（explicit_rc_first / resolver_chain / none）
```

## 3. 需求条目

```
R1  场景枚举 Scene（git_credential / project_info / issue_op / skill_upload / event_stream /
    task_notify / task_access / dispatch），每个调用点必须显式声明，未登记 = 拒绝
R2  策略表 SCENE_POLICIES[scene] = {scope, credential_source, allow_cross_repo,
    allow_pseudo_scope, legacy_unbound_ok, require_same_provider, 4 个错误码}
R3  同仓判定按 owner/repo 比较（去掉 provider 前缀），provider 单独比：
    同 owner/repo + 不同平台 = PROVIDER_SCOPE_MISMATCH（同名不算同仓，两套凭据）
R4  伪作用域（空 / `chat:`）：repo_bound 场景拒绝并给 SESSION_NOT_REPO_BOUND（带"先绑仓"语义）；
    task_plane 场景（task_notify / dispatch）按用户可见权放行
R5  空 repo_key 的历史 token：repo_bound 场景由 legacy_unbound_ok 保留旧行为，存量清零后置 False
R6  任务平面跨仓：必须传 access_check（用户对目标仓的可见权），未传或返回 False → 拒绝
R7  错误响应同时带 code（403）与 error_code（场景错误码）+ message（含具体原因）
R8  取券口径随场景声明（credential_source），为 #3713「换券与建树同源」提供唯一入口
```

## 4. 验收标准

```
A1  GIT_CREDENTIAL：同仓放行；跨仓 REPO_SCOPE_MISMATCH；同仓不同平台 PROVIDER_SCOPE_MISMATCH；
    chat:<uid> SESSION_NOT_REPO_BOUND；空 repo_key 维持旧行为
A2  PROJECT_INFO：同仓放行，跨仓拒绝
A3  TASK_NOTIFY：同仓放行；跨仓 + 用户可见 → 放行（cross_repo=true）；
    跨仓 + 不可见 → CROSS_REPO_DENIED；chat 伪作用域 + 可见 → 放行（S1）；
    chat 伪作用域 + 不可见 → SESSION_NOT_REPO_BOUND（S2）
A4  TASK_ACCESS（自省）：即便用户可见目标仓也不跨仓（repo_bound）
A5  未登记场景 → SCENE_NOT_REGISTERED
A6  非法/缺失 token → 403，文案区分"需会话 token"/"会话 token 无效"
A7  原有 18 条 token 平面用例与 9 条换券授权用例保持全绿
A8  响应体 shape 不变（code/error_code/message），唯一新增 error_code 字段
```

## 5. 场景表（首付三处已改，其余登记迁移）

```
场景              作用域        允许跨仓  伪作用域      取券口径            现状
git_credential    repo_bound    否        拒绝           explicit_rc_first   已改（repos.py）
project_info      repo_bound    否        拒绝(空兼容)   none                已改（repos.py）
task_notify       task_plane    是(可见权) 是(可见权)     none                已改（issues_center）
task_access       repo_bound    否        拒绝           none                （自省类，维持）
issue_op          repo_bound    否        拒绝           resolver_chain      待迁移
skill_upload      repo_bound    否        拒绝           none                待迁移
event_stream      repo_bound    否        拒绝           none                待迁移
dispatch          task_plane    是(关系)  是(可见权)     none                待迁移（#2112）
```

## 6. 非目标

```
· 不在本单改造 30 处取券消费方（#3713 单独立项，本单只提供 credential_source 字段）
· 不引入 token 签发表变更（scope_kind 字段属下一步，本单用前缀判定并留注释）
· 不改 403→401 语义，不改前端协议
```