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
可见权口径：`mgmt.get_managed_repos(user_id)` 包含目标 repo_key（JOIN user_repos，
成员也可见）。判定不出（异常/无 user_id）一律按无权。
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