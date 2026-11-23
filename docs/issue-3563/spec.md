# Issue #3563 Spec：GitHub OAuth 8h 过期型令牌自动续期 + 401 reauth 分流（v2）

> 来源：Issue #3563（Spec Document 评论 202380288 + 用户补充验收 202380151 + Issue Agent Spec v2 修订 202381001）｜目标分支 `arch-weekly-11-ljt`｜优先级 P1
> ⚠️ **v2 修订（生效）**：用户裁定 + Issue Agent 采纳——**不新增 `error_code=REAUTH_REQUIRED`**，
> 复用既有 `HTTP 401 + detail.action="reauth_required"` 协议并补 `provider` 分流字段；
> 前端联动（provider 分流 + 入口 toast 判定 + i18n 键）一并纳入本单。

## 背景（生产实测）

GitHub OAuth App 用户令牌 8 小时过期；到期后建仓重名检查 / 模板一键创建 /
托管权限判断三入口统一报零信息量 `502 检查失败: 无法获取当前用户身份`。
`external_identities` 中 `provider='github' AND status=1` 46 行里 45 行已过
登记到期时间；重新授权可恢复 8 小时（自助手段不可持续）。

## 代码现状（目标分支 arch-weekly-11-ljt 已核对）

- **A（续期判定）已落地（D100，不重写）**：`src/api/deps.py::get_user_token_for`
  的预刷新判定读 `external_identities.token_expires_at`（经
  `token_expires_at_any`），距过期 <600s 经注册表（provider_link）调
  `_try_refresh_github_token`（分布式锁 `oauth_refresh:{user_id}:github`），
  失败 `mark_identity_expired` 落 status=4。本单只补 2 条单测 + grep 断言。
- **B（401 分流）缺失**：
  - `check_repo_name`（`src/domains/mgmt/routes/repos.py:1037/:1048`）：
    `create_provider().verify_token` 直取，上游 401 RuntimeError 落入
    `except Exception` → `502 检查失败: {e}`，且 `{e}` 含
    `https://api.github.com/user` 内部 URL（信息泄露）。
  - 模板一键创建（`src/domains/mgmt/routes/templates.py:211-213`）：
    `GitHubProvider.get_user_login` 委托 `get_user_github_login`，后者
    `except RuntimeError: return ""` 吞掉 401 → 空 login →
    `502 无法获取当前 {provider} 用户身份`；create_repo 异常原文亦透出 URL。
  - 托管权限判断（repos.py:1690 / :2418）已走 `with_oauth_token`
    （401 语义已具备），但 401 detail 缺 `provider` 字段。
- `with_oauth_token`（`src/api/oauth_token.py`）已具备 401 语义，当前
  detail 仅 `{code, message, action: reauth_required}`。

## 目标

- 到期前（<600s）自动续期用户无感；续期不可用（refresh_token 失效）时三入口
  统一 `401 + detail={action: reauth_required, provider, message}`，
  message「{Provider} 授权已过期，请重新登录授权」，响应不含内部 URL；
- 复用既有 `action=reauth_required` 协议（前端 api.js:286/312 已消费），
  **不新增 error_code**（用户裁定：避免两套口径并存）；
- 401 detail 带 `provider` 字段供前端分流（评论 202380151）；
- 其它上游错误保留状态码语义，消息脱敏（剥 URL）短句化；
- 仅确实取不到身份时保留现有「无法获取当前用户身份」文案。

## 范围

### In
1. 统一 401 出口 helper（`src/api/oauth_token.py`，与 with_oauth_token 同文件）；
2. check-name / 模板一键创建 / 托管权限判断三入口 401 分流；
3. `repo_manager` 上游状态码透出（新增 `(login, status)` 变体，保持不抛契约）；
4. A 侧 2 条单测 + grep 断言（预刷新判定链不读 `user_credentials.<p>_oauth`）；
5. 前端联动（Spec v2 B2/B3）：api.js 按 `detail.provider` 分流重授权
   （GitHub OAuth 未配置时提示 + 直达「账号设置 → 凭据」）；repos/templates
   入口 toast 先判 status/action；i18n 补 `github_reauth_required`（zh/en）。

### Out
- 不新增表/列；
- 不改 `db.py::refresh_oauth_token` 的迁移期 user_credentials fallback 与同步写
  （provider 泛化，动它波及 gitcode_oauth 74 行现存凭据，PR 说明中注明）；
- 不动启动期 user_credentials 清理/补种、OAuth App 配置、前端改版；
- 可选项 C（settings.py 的 `token_expired` 按到期时间计算）不纳入，由 #3543 承接。

## 验收标准（可执行）

- [ ] 单测：`token_expires_at=now+60s` 时 `get_user_github_token_with_refresh`
      触发 refresh 恰一次、返回新 token、行被更新；
- [ ] 单测：refresh 失败时 `external_identities.status=4`，接口链返回
      401 + `action=reauth_required`（Spec v2：无 error_code，不再是 502 零信息文案）；
- [ ] 三入口过期令牌均 401 reauth_required（detail 含 provider），响应体不含内部 API URL；
- [ ] 前端：GitHub 过期账号三入口触发 → 出现「GitHub 授权已过期，请重新登录授权」
      并进入 GitHub 重授权流程（OAuth App 未配置时落到凭据页 GitHub Token 输入），
      不出现「检查失败: 无法获取当前用户身份」；GitCode 用户行为无变化；
- [ ] grep 断言：`src/api/deps.py` 预刷新判定链不再读写
      `user_credentials.<p>_oauth`（deps.py 内 `github_oauth` 命中数 = 0）；
- [ ] 不新增表/列；刷新沿用 `oauth_refresh:{user_id}:github` 锁；成功/失败
      各有独立日志行；
- [ ] 现有测试全绿（本分支预存失败项不新增），新增用例覆盖新行为。
