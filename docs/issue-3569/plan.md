# Issue #3569 方案设计

## 1. 后端：401 detail 补 provider

`src/api/oauth_token.py` 的 `with_oauth_token(user, provider, ...)` 作用域内已有 `provider` 入参，
三处 `HTTPException(401, detail={code,message,action})` 统一补 `"provider": provider`：

| 行 | 触发条件 |
|---|---|
| `:80-84` | 首次取 token 为空（已过期且无法预刷新） |
| `:96-100` | 401 → 刷新后仍无新 token（无 refresh_token / refresh 失败） |
| `:108-112` | 刷新成功但重试仍 401/403(token) |

字段为向后兼容扩展：旧前端忽略，新前端据此选择授权入口。`src/domains/mgmt/routes/repos.py:845-849`
基线已带 `provider`，本次不改动、只在自审查中确认字段未丢。

## 2. 前端：按平台跳转

`api.js`（API IIFE 内）新增：

```js
_REAUTH_PROVIDER_PATHS = {gitcode: '/auth/login/gitcode', github: '/auth/login/github'}
_redirectToOAuth(provider)
```

- provider 归一化：`github` / `gitcode` 精确匹配；**缺省**（旧后端无该字段）回退 `gitcode` 兼容旧行为；
  已知但无授权入口的平台（如 `gitee`）不猜平台 → 只提示「去账号设置重新授权」，避免复制同一缺陷。
- 两个 401 分支（`:286`、`:312`）由 `_redirectToGitcodeAuth()` 改为
  `_redirectToOAuth(err.detail.provider)`；旧函数名不再被调用，删除以免误用。
- `_reauthRedirecting`（页面级变量）保留用于同页并发去重。

## 3. 前端：跨整页重载的防循环标记

- 存储：`sessionStorage['grape_reauth_redirect'] = {provider: ts}`（多平台可并存）。
- 写入时机：拿到 `redirect_url`、真正 `location.href` 跳转前。
- 读取判定：`_recentlyRedirected(provider)` = 同 provider 且 `now - ts < 60_000`。
- 命中判定 → 不跳转，调用 `_showReauthNotice(provider)` 渲染提示：
  `<平台> 授权已过期，请到 账号设置 → 第三方绑定 重新授权`，附「去账号设置」入口
  （`#account/profile`，`app.nav('account','profile')` 可用时直接路由）。
- 清除时机：① 页面加载时 URL 带 `bind_success=<provider>`（同页回调重新绑定成功）；
  ② 其他窗口更新 `localStorage.user`（popup 绑定路径，`storage` 事件）→ 按新 `bindings` 清除对应平台标记。
  保证重新授权后下一次 401 仍可正常跳转（验收 4）。
- 为什么不用 `localStorage`：sessionStorage 是页签级，整页重载仍保留，且不会跨页签互相干扰。

## 4. 前端：400 / 429 只提示

`_redirectToOAuth` 内部 `API.get('/auth/login/<provider>')` 的失败分支按 status 分流：

| status | 行为 |
|---|---|
| 400（目标平台 OAuth 未配置） | `_showReauthNotice` 提示「OAuth 未配置，请联系管理员」，**不跳转** |
| 429（入口限流） | 提示「请求过于频繁，请稍后重试」+ 退避，**不新增任何整页导航** |
| 其他 / 网络错误 | 提示「授权跳转失败」，**不整页导航**（原实现会 `location.href=/login`，属缺陷放大器） |

## 5. 测试

- `tests/js/test_issue_3569_reauth_loop_guard.js`：在桩环境（window/document/fetch/localStorage/
  sessionStorage/location/app/I18N）中 `new Function` 加载真实 `api.js`，覆盖：
  后端 detail 带 provider 时时分别请求 `/auth/login/github` / `/auth/login/gitcode`；
  60 秒内第二次 401 不再发登录请求且渲染提示；标记跨「整页重载」（重新加载模块、保留 sessionStorage）生效；
  `bind_success=github` 加载时清除标记；400/429 不改变 `location.href`；无 provider 回退 GitCode。
- `tests/api/test_issue_3569_reauth_provider.py`：`with_oauth_token` 三条 401 路径的 detail 均含
  `provider`，且 `repos.py` 的 401 分支仍带 `provider`（防回归删除）。

## 6. 交付说明

- 本基线 `arch-weekly-11-ljt` 的 `src/api/deps.py` 已无 `rowid`（全仓 `rowid` 仅剩
  `lastrowid` 于 scripts/tests），无需改动。
- 若发布线为 `main`，`main:src/api/deps.py:344` 的 `ORDER BY updated_at DESC, rowid DESC`
  需单独移除 -> `ORDER BY updated_at DESC`，否则 MySQL 下凭据兜底恒返回 `{}`（同缺陷另一条交付线）。
- 本次不含任何整页 `location.href='/login'` 新增；复用现有 `app.showLogin()` 收敛口径。