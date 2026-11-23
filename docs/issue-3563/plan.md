# Issue #3563 方案设计（v2，与 spec.md 同步修订）

## 核心思路

复用 `with_oauth_token` 既有的「预刷新 + 401 刷新重试 + 仍失败→401 reauth_required」
语义，把绕开它的三入口直取调用收敛到同一错误出口；401 detail 统一由
`oauth_token.py` 的 helper 产出（`action=reauth_required` + `provider` 分流字段，
**不含 error_code**——Spec v2/用户裁定，复用既有协议）。

## 1. 统一 401 出口（src/api/oauth_token.py）

```python
REAUTH_REQUIRED_ACTION = "reauth_required"

def reauth_required_detail(provider: str) -> dict
    # {"code": 401, "message": "{Provider} 授权已过期，请重新登录授权",
    #  "action": "reauth_required", "provider": provider}
    # 平台显示名映射 github→GitHub / gitcode→GitCode（capitalize() 会得到 Github）

def raise_reauth_required(provider: str)  # 抛 HTTPException(401, detail=...)

def sanitize_upstream_error(err) -> str
    # 剥 https?://... 内部 URL、压缩空白、截断 300 字符——上游短消息化
```

`with_oauth_token` 内 3 处 401 detail 全部改经 `raise_reauth_required`
（统一出口，Spec v2 B1）。

## 2. 三入口分流

| 入口 | 现状 | 改动 |
|------|------|------|
| check_repo_name（repos.py:1023） | verify_token 直取；401 → `502 检查失败: {e}`（含 URL） | verify_token 经 `with_oauth_token` 包装 → 401 自动 reauth_required（带 provider）；捕获实际生效 token 重查 repo；其余异常经 `sanitize_upstream_error` 脱敏 |
| create_from_template（templates.py:206-224） | get_user_login 直取（GitHub 实现吞 401 返空）→ `502 无法获取当前 {provider} 用户身份` | 身份获取改 `with_oauth_token` + `verify_token`（401 穿透刷新链）；空 login（真实取不到）保留 502；create_repo/push 用实际生效 token；异常消息脱敏，409/422 冲突检测语义不变 |
| 托管权限判断（repos.py:1706 / :2418 `_ensure_vcs_maintainer`） | 已走 with_oauth_token，401 语义在 | 由 ① 自动补齐 provider 字段；「无法获取当前用户身份」保留为真实取不到身份的兜底（不再被过期 token 命中） |

## 3. repo_manager 状态码透出（保持不抛契约）

`get_user_github_login` / `get_user_gitcode_login` 各拆出
`*_login_status(api_token) -> tuple[str, int]` 变体：

- 返回 `(login, status)`：status=0 成功；401/403/5xx 上游状态码；-1 网络/未知；
- 原函数变薄包装（`(login, _s)` 取 login），既有调用点零改动；
- 成功/失败各留独立日志行（login/status/result）。

## 4. A 侧单测（tests/mgmt/test_issue_3563_github_token_refresh.py）

1. **预刷新成功链**：真实 grape_accounts + external_identities 行
   （expires_in=60 → token_expires_at=now+60s），monkeypatch
   `src.api.deps._try_refresh_user_token`（注册表派发点）记录调用次数并调用
   生产代码 `update_external_identity_tokens` 回写新 token → 断言 refresh 恰一次、
   返回新 token、行已更新（token_expires_at 前移）；
2. **预刷新失败链**：同上但 refresh 返回 None → 断言
   `external_identities.status=4`（mark_identity_expired 生产链）且旧 token 返回；
   再经 `with_oauth_token` 包装一个必 401 的 API 调用 → 断言 HTTPException 401 +
   `action=reauth_required` + `provider=github`（无 error_code，不再是 502）；
3. **grep 断言**：deps.py 源码 `github_oauth` 命中数 = 0。

## 5. 三入口 401 分流用例（tests/mgmt/test_issue_3563_reauth_401_split.py）

- check_repo_name：过期令牌（verify_token 401、refresh 失败）→ 401
  reauth_required（provider=github），detail 无内部 URL；status=4 落库；
  正常令牌 200 exists 语义回归；上游 5xx 保留 502 且脱敏；404→exists=false；
- create_from_template：同链路 → 401 reauth_required；成功路径 200（身份+建仓
  token 一致性断言）；建仓 5xx 502 脱敏；422/already exists 冲突复用语义回归；
- _ensure_vcs_maintainer / host_repo GitCode 托管身份段：401 detail
  含 action + provider + message 三字段（v2 契约）；
- repo_manager `*_login_status` 变体（0 / 401 / -1）与薄包装契约；
- oauth_token helper 契约（detail 字段集、raise、sanitize 剥 URL）。

## 6. 前端联动（Spec v2 B2/B3）

- **api.js**：两处 401 reauth 分支改走 `_redirectToProviderAuth(detail)`——
  provider 缺省/gitcode 保持 `_redirectToGitcodeAuth()` 原行为（回归安全）；
  github/gitee 等按 `GET /auth/login/<provider>` 路由；GitHub OAuth 未配置
  （无 redirect_url/400）→ 提示 `common.github_reauth_token_hint` +
  `location.hash='#account/credentials'`（凭据页 GitHub Token 输入），
  不跳空白授权页；抛出的错误附 `status/detail/reauth_required` 供入口判定；
- **ReposView.vue / TemplatesView.vue**：check-name 与 create 的 catch 先判
  `status===401 && detail.action==='reauth_required'` → 走全局流程不透传
  「检查失败/创建失败」文案；其它错误保持原文案；
- **i18n**：zh/en `common.json` 补 `github_reauth_required` +
  `github_reauth_token_hint`；index/register/forgot 的 api.js 版本串 36→37、
  i18n 缓存版本 bump（74→75 / 55→56）、repos-view bundle 64→65。

## 关键设计决策（Spec v2，不赘述）

- **不新增 error_code**：复用 `action=reauth_required`（用户裁定 + Issue Agent
  v2 修订，评论 202380151/202381001——前端 api.js 早已按 action 消费）；
- 401 detail 增加 `provider` 字段（前端按 provider 跳
  `/auth/login/<provider>`，避免 GitHub 用户被导去 GitCode 授权页）；
- A 侧复用 D100 实现只补测；db.py 迁移期 fallback 不动；
- repo_manager 不改抛契约，仅附加状态信息。

## 风险与缓解

| 风险 | 缓解 |
|------|------|
| GitHub OAuth App 未开 refresh | 退化为「标记过期 + 401 提示重新授权」，无功能倒退 |
| 401 分流误伤其它上游错误 | 仅 `_is_token_error` 命中（401 / 403+token / Bad credentials）才 reauth；其余保留状态码 + 脱敏短消息 |
| 前端已按 action 消费 | action 字段原样保留（协议字段不变，仅补 provider） |
| repo_manager 契约回归 | 原函数签名/返回不变，仅新增变体 + 薄包装 |
| templates 身份获取改 verify_token 影响存量 mock | test_project_templates 存量用例在本分支预存失败（UUID 化 grape_accounts 后 setup 造数失效），本单新增用例覆盖成功/冲突路径 |
