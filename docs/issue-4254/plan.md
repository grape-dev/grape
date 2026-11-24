# Issue #4254 · 方案设计

## 后端
- `src/domains/mgmt/auth/signup_policy.py`
  - `_signup_whitelist_match(email)`：三态返回（True 命中/无管控、False 未命中、None 查询异常）。
  - `check_beta_eligibility(account, identities)`：单一资格判定函数，复用 `login_whitelist`（`is_user_allowed`）与 `signup_whitelist`。
- `src/domains/mgmt/routes/auth.py`
  - `/profile` 返回 `created_by_oauth`。
  - 新增 `GET /beta-eligibility`（`operation_id=auth_beta_eligibility`），读取账号 + 活跃身份后委派判定。

## 前端
- `AccountView.vue`：`onMounted` 拉取 `/auth/beta-eligibility`，`showBetaHint` 计算属性控制 OAuth 区/PAT 区提示条；入口调用壳内 `G.app.openBetaApply()`（降级跳转 `/static-vue/beta-apply`）。
- `PatDialog.vue` / `BindDialog.vue`：新增 `betaHint` prop（默认 false，其他宿主不受影响），弹窗顶部同步提示条。
- i18n：`app.account.beta_hint_text` / `app.account.beta_hint_apply`（zh/en）。

## 缓存/契约
- 页面 chunk 与 `all-views.css` 版本递增，同步缓存棘轮登记与 i18n cacheVer。
- 重新生成 OpenAPI 契约与基线（新增 1 个 endpoint）。

## 测试
- `tests/api/test_issue4254_beta_eligibility.py`：判定分支 + 路由 + profile 字段。
- 既有 auth/契约/缓存棘轮测试回归。
