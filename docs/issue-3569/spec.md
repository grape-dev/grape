# Issue #3569 需求文档：跨平台 OAuth reauth 跳转闭环

## 背景

任一平台 OAuth 令牌失效时，前端 `shell/js/api.js` 收到 `401 + detail.action=reauth_required` 后
一律调用 `_redirectToGitcodeAuth()`，固定请求 `/api/auth/login/gitcode`。GitHub 令牌失效被送去
GitCode 重新授权，回调后仍 401，页面整页重载再触发同一请求，形成「3~5 秒一轮」的整页重载闭环；
OAuth 入口被限流打满后 429 与 200 交替，闭环自我加速，退出登录也无法脱困。

## 目标

1. 401 的结构化 detail 带上 `provider`，前端按平台跳对应授权入口（GitHub → `/api/auth/login/github`）。
2. 同一 provider 一次跳转后不再重复跳转（跨整页重载仍生效），闭环消失。
3. 目标平台 OAuth 未配置（400）/ 入口限流（429）只提示，不新增整页导航。
4. 重新绑定成功后清除标记，正向回归（GitCode 令牌失效 → 跳 GitCode → 回大厅）不被标记拦住。

## 范围

- 后端：`src/api/oauth_token.py` 三处 401 detail 补 `provider`（`repos.py:845-849` 已带，保持不丢）。
- 前端：`src/api/frontend/shell/js/api.js` 新增 `_redirectToOAuth(provider)`、sessionStorage
  防循环标记（provider + 时间戳，60 秒窗口）、内联提示条（含 `#account/profile` 入口）。
- 前端文案：`locales/{zh,en}/app.json` 各补 5 个 `error.reauth_*` key（zh/en 集合一致）。
- 不改：`src/api/deps.py`（基线 `arch-weekly-11-ljt` 已无 `rowid`，`main` 线需单独移除，见 §交付说明）。

## 验收标准（同 Spec）

1. GitHub 令牌失效 + 无仓库缓存 + 打开 `/app/`：30 秒内 `/api/repos/member` 401 ≤ 1 次、
   `/api/auth/login/gitcode` ≤ 1 次，页面停止整页重载，出现「GitHub 授权已过期，去账号设置重新绑定」提示，
   期间无 `/login` 302 整页跳转。
2. 该状态下点「退出登录」：`POST /api/auth/logout` 200 后 30 秒内不再出现 `/api/auth/callback/gitcode`
   与 `/app/?bind_success=gitcode` 重登。
3. 同一 provider 连续 401 的第 2 次起不再触发任何 OAuth 跳转（标记跨整页重载生效）。
4. 正向回归：GitCode 令牌失效仍正确跳 GitCode 授权，授权成功回到大厅，不被标记拦住；
   目标平台 OAuth 未配置（400）只提示不跳转；429 只提示 + 退避，不整页导航。
5. MySQL 部署下 `Unknown column 'rowid'` 为 0（发布线为 `main` 时需同步移除 `main:deps.py:344` 的 `rowid`）。