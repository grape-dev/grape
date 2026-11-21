# 需求：OAuth 令牌过期续期与三入口 401 重授权分流（Issue #3563，Spec v2）

> 适用：GitHub / GitCode OAuth 用户令牌（8 小时过期型）。
> 协议基线（v2，Issue 评论 202381001 / 202380151 裁定）：**不新增 `error_code`**，
> 复用既有 `HTTP 401 + detail.action="reauth_required"`，并补 `provider` 字段。
> 来源：本分支移植 PR #1862（原始单 #3563）；实现提交 `c978937cc`。

## 1. 问题陈述

三个入口都依赖「调用方自己的 OAuth 身份」，但令牌过期时它们给出的证据不一致：

```
入口                          现状（令牌过期 + 刷新不可用）
/api/repos/check-name         502「检查失败: GitHub API GET <upstream>/user failed (401)…」
                              → 零信息量 + 内部 URL 外泄
模板一键创建                   502「无法获取当前 GitHub 用户身份」
                              → 真实原因是令牌过期，文案指向「没有身份」
托管权限判断                   401 有 action=reauth_required，但 detail 缺 provider
                              → 前端只能猜平台，GitHub 用户会被导到 GitCode 授权页
```

三者处置完全不同（过期→重新授权；无权限→换账号；真的没身份→先绑定），
必须给出可判定的信号，且消息不得含内部 endpoint。

## 2. 目标

```
G1  令牌到期前自动续期，用户无感（A 侧既有能力，本单只补验证）
G2  续期不可用时，三入口统一 401 + detail{action=reauth_required, provider, message}
G3  message 统一「{平台} 授权已过期，请重新登录授权」，不含内部 URL
G4  其它上游错误保留状态码语义（502/500…），消息脱敏短句化
G5  前端按 provider 分流重授权；GitHub OAuth 未配置时提示 + 直达凭据页
G6  既有契约不变：不新增表/列，原登录名 helper 仍返回字符串
```

## 3. 需求条目

```
R1  统一 401 出口：oauth_token 提供 reauth_required_detail / raise_reauth_required，
    detail 恒为 {code:401, message, action:"reauth_required", provider}，**不含 error_code**
R2  平台显示名映射：github→GitHub / gitcode→GitCode / gitee→Gitee / gitlab→GitLab，
    不得用 capitalize()（会得到 Github）
R3  三条 401 出口（with_oauth_token 三处）全部走 R1，不再各自拼 detail
R4  check-repo-name：身份获取经 with_oauth_token（401 → 刷新重试 → 仍失败 401 reauth），
    并用**刷新后实际生效的 token** 查仓库存在性（外层 token 已过期）
R5  模板一键创建：身份改用 verify_token 穿透（原 get_user_login 吞 401 返空 → 502 无信息），
    create_repo/push 使用实际生效 token
R6  托管权限判断：401 detail 补 provider（其余语义不变）
R7  上游错误脱敏：用户可见 message 剥离 `https?://...`，保留状态码与短描述，≤300 字符；
    原文仍进日志（不落响应）
R8  登录名 helper 增加 (login, status) 变体，status: 0 成功 / >=400 上游码 / -1 网络未知；
    原 helper 变薄包装，失败返回空串的契约不变（20+ 调用点半行不改）
R9  前端：api.js 按 detail.provider 分流 /auth/login/<provider>；GitHub OAuth 未配置时
    提示 + 跳「账号设置 → 凭据」；repos/templates 入口 catch 先判 status/action；
    i18n 补 github_reauth_required / github_reauth_token_hint（zh/en）
R10 不新增表/列；刷新沿用分布式锁 oauth_refresh:{user_id}:github；成功/失败各有独立日志行
```

## 4. 验收标准

```
A1  令牌 expires_in=60s + 刷新可用 → 三入口均正常返回（无 401），且使用刷新后的新 token
A2  刷新不可用 → 三入口均 401，detail.action=reauth_required、provider 正确、无 error_code
A3  A2 的响应体不含 <upstream> 等内部 URL；message 为 G3 文案
A4  GitHub 过期用户身份落 status=4（mark_identity_expired），可被下次登录刷新
A5  非令牌类上游错误（5xx）→ 502 语义保留，message 脱敏（无 URL，保留状态码）
A6  verify_token 成功但响应无 login → 保留「无法获取当前用户身份」502
A7  正常令牌回归：check-name 200 exists / 404 → exists=false
A8  provider 显示名映射四平台；detail 结构固定（无 error_code、含 provider）
A9  前端契约：api.js 存在 provider 分流分支；zh/en 两个 i18n 键均存在
A10 既有用例零改动全绿；不新增表/列（DDL 无新语句）
```

## 5. 场景用例（详见设计文档 §5 的 S1–S10 与
`tests/mgmt/test_oauth_reauth_scenarios.py`）

```
S1 GitHub 过期 + 刷新可用 → check-name 200（用新 token）
S2 GitHub 过期 + 刷新可用 → 托管权限判断正常（非 401）
S3 GitHub 过期 + 刷新不可用 → check-name 401 reauth（provider=github，无 URL）
S4 同上 → 托管权限判断 401 reauth
S5 GitHub 过期 + 刷新不可用 → 模板创建 401 reauth（不落 502 无信息文案）
S6 身份取不到（verify_token 无 login）→ 502「无法获取当前用户身份」（保留原文案）
S7 非令牌类上游 5xx → 502 脱敏（无 URL、保留 500）
S8 GitHub 正常令牌 → check-name 200 / 404 exists=false（回归）
S9 前端契约：api.js provider 分流 + i18n 双语键存在
S10 契约锁定：detail 无 error_code；oauth_token 三处 401 出口不得各拼 detail
```

## 6. 非目标

```
· 不改 db.py::refresh_oauth_token 的迁移期 user_credentials fallback（波及 gitcode 存量凭据）
· 不新增 error_code（避免与 action 两套口径并存）
· 不做 settings.py token_expired 真实化（由 #3543 承接）
· 不改 OAuth App 配置与前端整体改版
```