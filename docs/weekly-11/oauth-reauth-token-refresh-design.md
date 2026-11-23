# 设计：OAuth 令牌过期续期与三入口 401 重授权分流（Issue #3563，Spec v2）

> 实现提交：`c978937cc`（移植自 PR #1862）｜需求：`oauth-reauth-token-refresh-requirements.md`
> 场景用例代码：`tests/mgmt/test_oauth_reauth_scenarios.py`（S1–S10）

## 1. 现状与根因

```
根因 1  三个入口各自拼错误：令牌过期被包成 502「检查失败 / 无法获取当前用户身份」，
        上游原文还带着 <upstream>/... 的内部 URL。
根因 2  托管权限判断虽然走 with_oauth_token（401 语义已有），但 detail 缺 provider，
        前端无法分流平台，GitHub 用户会被导向 GitCode 授权页。
根因 3  模板创建的身份获取走 get_user_login → get_user_github_login，后者把上游 401
        吞成空串，于是「令牌过期」被表述成「没有身份」。
```

## 2. 设计

### 2.1 统一 401 出口（`src/api/oauth_token.py:20,32,51,57`）

```
REAUTH_REQUIRED_ACTION = "reauth_required"
reauth_required_detail(provider) -> {code:401, message, action, provider}   ← 不带 error_code
raise_reauth_required(provider)  -> 抛 HTTPException(401, detail 同上)
sanitize_upstream_error(err)     -> 剥离 https?://... 、压缩空白、截 300 字符
_provider_label(provider)        -> GitHub / GitCode / Gitee / GitLab（不用 capitalize）
```

`with_oauth_token` 的三处 401（无 token / 刷新失败 / 重试仍 token 错）全部改为
`raise_reauth_required(provider)`，detail 形状唯一。

### 2.2 三入口

```
check-repo-name  repos.py:1024-1060
  身份 = with_oauth_token(user, provider, verify_token)      ← 401 走刷新，再失败 401 reauth
  _used_token 记录**实际生效** token（刷新成功后外层 token 已过期）
  存在性 = VcsApiClient(provider, _used_token, login, name).get_repo_info()
  404 → exists=false；其它 error → 502 + sanitize_upstream_error

模板一键创建     templates.py:205-265
  身份 = verify_token（穿透 401，不再被 get_user_login 吞掉）
  create_repo / push 使用 with_oauth_token 返回的实际 token
  异常 message 全部经 sanitize_upstream_error，409/422 冲突语义不变

托管权限判断     repos.py 既有 with_oauth_token 路径
  仅承接 2.1 的 detail 补齐（provider），判定逻辑零改动
```

### 2.3 登录名与状态透出（`repo_manager.py:371,1243`）

```
get_user_gitcode_login_status / get_user_github_login_status -> (login, status)
  status: 0 成功 / >=400 上游 HTTP 码 / -1 网络或未知
  成功与失败各一条独立日志行
get_user_gitcode_login / get_user_github_login -> 薄包装，返回字符串（契约不变）
```

### 2.4 前端（`shell/js/api.js:286-316`、`app/src/views/{ReposView,TemplatesView}.vue`）

```
api.js           detail.action === 'reauth_required' → _reauthError(detail)
                 → 按 detail.provider 跳 /auth/login/<provider>，_reauthRedirecting 去重
                 GitHub OAuth 未配置（渠道无 client_id）→ toast github_reauth_token_hint
ReposView        入口 catch 先判 status/action，再走原有错误提示
TemplatesView    同上（新增分支）
i18n             zh/en 各补 github_reauth_required / github_reauth_token_hint
资源版本         api.js?v=37（内容变更）；i18n 版本串按页面递增
```

## 3. 时序（失败路径）

```
前端 → GET /api/repos/check-name
  服务端 get_user_token_for（<600s 预刷新；失败 mark_identity_expired status=4）
  verify_token → 401 → with_oauth_token 刷新 → 仍失败
  → 401 {code:401, action:reauth_required, provider:github, message:"GitHub 授权已过期，请重新登录授权"}
  前端 → _reauthError → GitHub OAuth 已配置：跳 /auth/login/github
                       未配置：toast 提示 + 跳「账号设置 → 凭据」
```

## 4. 不变量

```
① 不新增表/列（DDL 无新语句）；刷新沿用 oauth_refresh:{user_id}:github 锁
② 不新增 error_code；detail 形状固定，前端只认 action + provider
③ 响应体不含内部 URL；原文只进日志
④ 原有 helper 契约不变（失败返回空串），调用点零改动
⑤ GitCode 用户行为不变（provider 字段按实际平台填）
```

## 5. 场景用例表（S1–S10）

| # | 场景 | 期望 | 用例 |
|---|---|---|---|
| S1 | GitHub 过期 + 刷新可用 → check-name | 200，且用刷新后的新 token 查存在性 | `test_s1_expired_token_refreshes_on_check_name` |
| S2 | GitHub 过期 + 刷新可用 → 托管权限判断 | 非 401（正常返回/业务错误） | `test_s2_expired_token_refreshes_on_hosted_permission` |
| S3 | GitHub 过期 + 刷新不可用 → check-name | 401 + action/provider/message，无内部 URL，身份落 status=4 | `test_s3_check_name_401_after_refresh_failure` |
| S4 | 同上 → 托管权限判断 | 401 + provider=github | `test_s4_hosted_permission_401_after_refresh_failure` |
| S5 | 同上 → 模板一键创建 | 401（不再是 502 无信息文案） | `test_s5_template_create_401_after_refresh_failure` |
| S6 | verify_token 成功但无 login | 502「无法获取当前用户身份」（保留原文案） | `test_s6_missing_login_keeps_identity_502` |
| S7 | 非令牌类上游 5xx | 502、message 无 URL、保留状态码 | `test_s7_upstream_5xx_is_sanitized` |
| S8 | 正常令牌 → check-name | 200 exists / 404 → exists=false | `test_s8_normal_token_returns_exists` |
| S9 | 前端契约 | api.js 有 provider 分流；zh/en 两个键都在 | `test_s9_frontend_provider_routing_present` |
| S10 | 协议锁定 | detail 无 error_code；三处 401 都走统一出口 | `test_s10_protocol_has_no_error_code` |

真机限制：本机 GitHub OAuth 渠道 client_id/secret 为空、`external_identities`
无 github 绑定，A/S1–S5 的真实上游行为无法在本机复现（用例用 mock 替代上游），
真机验证需先配置 GitHub OAuth 并完成一次浏览器授权。

## 6. 边界（不做）

```
· 不改 db.py::refresh_oauth_token 迁移期 fallback（波及 gitcode 存量凭据）
· 不做 settings.token_expired 真实化（#3543）
· 不改 OAuth App 配置流程与前端整体改版
· 不在本单引入 refresh_token 轮换策略变更
```

## 7. 实现状态（滚动）

```
代码   oauth_token / repos.check_repo_name / templates.create_from_template /
       repo_manager 状态变体 / api.js / 两个 Vue 入口 / i18n     已实现（c978937cc）
用例   PR 自带 22 条（tests/mgmt/test_issue_3563_*）                            19 + 3 passed
       本设计场景用例 tests/mgmt/test_oauth_reauth_scenarios.py                    10 passed
       红相：同一文件跑在父提交 91273db40（未移植）上 → 9 failed / 1 passed，
             说明这 10 条确实看护移植带来的行为，不是恒真断言
文档   issue-3563/spec|plan|task（来源单）+ 本需求/设计                         已同步
真机   GitHub OAuth 未配置（渠道凭据空）+ 无 github 绑定身份 → 无法实测刷新链路
部署   arch11 未同步；前端需 vite build 才会包含 .vue 改动
```