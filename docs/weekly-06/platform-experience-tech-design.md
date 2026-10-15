# Grape 平台体验优化 — 技术设计文档

> 需求文档：docs/weekly-06/platform-experience-requirements.md（需求 26-30）
> 关联设计文档：docs/weekly-06/grape-account-system-tech-design-v2.md（需求 1-25，已实施 D1-D49）
> 分支：feat/grape-account-system

---

## 1. 架构总览

### 1.1 现状

需求 1-25 已全部实施完成，账号体系基础设施就绪：

- 外部身份绑定表 `external_identities` 已支持 OAuth 绑定/解绑（加密存储 token）
- 用户中心（account.js）已实现四个 tab：个人资料/安全/API Keys/危险区域
- 通用设置（settings.js）三个 tab：凭据/通知/外观
- 项目页（repos.js）已有平台切换栏、项目卡片品牌图标
- 后端 `settings.py` 已支持 `github_token` 手动输入并验证
- 后端 `repos.py` 的 `create_new_repo` 用 `_get_user_provider_and_token` 自动选 provider
- `host_repo` 已支持 provider 参数（D44 修复）

### 1.2 本次改动范围

| 需求 | 前端 | 后端 | 改动类型 |
|------|------|------|---------|
| R28 品牌图标 | repos.js / app.js / account.js | — | 重构：提取共享函数 |
| R29 用户设置重组 | account.js / settings.js / app.js / index.html | — | 重组：移 tab + 改文案 |
| R27 绑定双模式 | account.js | settings.py | 新增：后端 gitcode_token + 前端弹窗 |
| R30 体验优化 | account.js / app.js | — | 增强：反馈文案 + loading |
| R26 平台选择 | repos.js | repos.py | 新增：前端选择 + 后端传参 |

### 1.3 设计原则

1. **不破坏现有功能**：已有的 OAuth 绑定/解绑流程不变，在其基础上增加手动令牌模式
2. **复用现有基础设施**：`external_identities` 表、`credential_crypto` 加密、`get_external_identity` 查询——手动令牌绑定写入同一张表
3. **前端函数不膨胀**：`platformIcon` 提取为独立工具函数，不在每个 JS 文件里重复 SVG path
4. **后端 API 对称**：GitCode 手动令牌验证逻辑和 GitHub 手动令牌验证逻辑对称（已有 GitHub 实现，新增 GitCode）
5. **i18n 全覆盖**：所有新增文案中英文双语

---

## 2. R28 品牌视觉统一 — 平台图标工具函数

### 2.1 设计方案

提取全局函数 `platformIcon(provider, size)` 到 repos.js（因为 repos.js 是第一个加载的页面 JS，且 `_repoPlatformIcon` 已在此文件中）。

**函数签名：**

```javascript
/**
 * 渲染平台品牌 SVG 图标。
 * @param {string} provider — 'gitcode' | 'github'
 * @param {number} size — 像素尺寸，默认 14
 * @returns {string} SVG HTML 字符串
 */
function platformIcon(provider, size) {
  size = size || 14;
  var style = 'vertical-align:-2px;flex-shrink:0';
  if (provider === 'github') {
    return '<svg width="' + size + '" height="' + size + '" viewBox="0 0 1024 1024" fill="currentColor" style="' + style + '">'
      + '<path d="M512 42.666667A464.64 464.64 ...（现有 GitHub SVG path）..."/>'
      + '</svg>';
  }
  // gitcode
  return '<svg width="' + size + '" height="' + size + '" viewBox="0 0 1024 1024" fill="currentColor" style="' + style + '">'
    + '<path d="M472.746667 896.853333c-72.533333 ...（现有 GitCode SVG path）..."/>'
    + '</svg>';
}
```

### 2.2 改造 `_repoPlatformIcon`

现有 `_repoPlatformIcon(r)` 接受 repo 对象，从 `r.repo_key` 推断 provider。改为委托 `platformIcon`：

```javascript
function _repoPlatformIcon(r) {
  var rk = r.repo_key || '';
  var provider = (rk.indexOf('github:') === 0) ? 'github' : 'gitcode';
  return platformIcon(provider, 14);
}
```

### 2.3 替换 emoji 的位置

| 文件 | 位置 | 改前 | 改后 |
|------|------|------|------|
| repos.js:157-158 | `_getAvailablePlatforms` | `icon: '🟠'` / `icon: '🐙'` | 删掉 icon 字段，渲染时调 `platformIcon(key, 14)` |
| repos.js:162-173 | `_buildPlatformBar` | 拼 emoji 字符串 | 拼 `platformIcon(p.key, 14)` |
| app.js:826 | `_renderOnboardingShell` | `var icon = p.provider === 'github' ? '🐙' : '🟠'` | `var icon = platformIcon(p.provider, 36)` |
| account.js:167-181 | `_accountRenderProviderRow` | 无图标 | 行首加 `platformIcon(provider, 20)` |

### 2.4 CSS 适配

品牌 SVG 用 `fill="currentColor"`，自动跟随父元素文字色。深色模式和浅色模式无需额外处理——`currentColor` 继承 `var(--t)` 或 `var(--t1)`。

---

## 3. R29 用户设置页面重组

### 3.1 改名：用户中心 → 用户设置

**改动清单（grep "用户中心" 全量替换）：**

| 文件 | 位置 | 改前 | 改后 |
|------|------|------|------|
| index.html:147 | 侧边栏分组标题 `data-i18n="app.sidebar.account"` | 用户中心 | 用户设置 |
| app.js:1324 | nav('account') 标题 | '用户中心' | '用户设置' |
| app.js:512-535 | 头像下拉菜单 | '用户中心' | '用户设置' |
| app.js:898 | 新手引导页按钮 | '前往用户中心' | '前往用户设置' |
| app.js:782 | OAuth 补全提示 | '去设置' | '去用户设置' |
| account.js:29 | 页面标题 | '用户中心' | '用户设置' |
| i18n.js | `app.account.center_title` 等 key | '用户中心' / 'Account Center' | '用户设置' / 'User Settings' |

**英文文案：** 'Account Center' → 'User Settings'

### 3.2 外观 tab 迁移

从 settings.js 迁移到 account.js：

**account.js 新增 tab：**

```
tabs = [
  {id:'profile', label: 个人资料},
  {id:'security', label: 账号安全},
  {id:'appearance', label: 外观},      ← 新增
  {id:'apikeys', label: API Keys},
  {id:'danger', label: 危险区域},
]
```

**迁移内容：**

- `selectTheme()` / `selectThemeMode()` / `selectLang()` 函数从 settings.js 移到 account.js
- 函数前缀改为 `_account`：`_accountSelectTheme()` / `_accountSelectThemeMode()` / `_accountSelectLang()`
- 主题选择卡片 HTML 模板移到 `_accountLoadAppearance()` 函数
- `_langSwitching` 全局变量移到 account.js

**settings.js 精简后 tabs：**

```
tabs = [
  {id:'credentials', label: 项目凭据},
  {id:'notifications', label: 通知},
]
```

settings.js 的 `selectTheme` / `selectThemeMode` / `selectLang` 函数删除（已移到 account.js）。但保留 `saveCredentials()` 中华为云 AK/SK 保存逻辑。

### 3.3 VCS Token 从通用设置移到用户设置

**当前状态：** settings.js 的 `credentials` tab 包含：
1. VCS API Token 输入框（GitHub Token）
2. 华为云 AK/SK 输入框
3. CodeArts AK/SK（如果有）

**改造方案：**

settings.js 的 `credentials` tab 只保留华为云 AK/SK + CodeArts。删除 VCS Token 输入框和 `saveCredentials()` 中的 `github_token` 字段。

VCS Token 管理统一到 account.js 的 `security` tab → 第三方账号区域（R27 绑定双模式实现）。

**向后兼容：** 后端 `PUT /api/settings/credentials` 保持 `github_token` 字段支持（已有用户可能直接调 API）。但前端不再从 settings.js 调用此字段。新增 `gitcode_token` 字段（R27 后端）。

**历史数据迁移（前端层面）：**

用户之前在通用设置中保存过 GitHub Token → 已写入 `external_identities` 表。用户设置的第三方账号列表调 `GET /auth/me` 返回 `bindings` 包含 `github` → 自动显示"已绑定"。无需后端迁移。

### 3.4 改动文件清单

| 文件 | 改动 |
|------|------|
| `src/api/static/js/pages/account.js` | 新增 appearance tab + 主题/语言函数迁移 + 文案改名 |
| `src/api/static/js/pages/settings.js` | 删除 appearance tab + 删除 VCS Token UI + 精简 tabs |
| `src/api/static/js/app.js` | 文案"用户中心"→"用户设置" |
| `src/api/static/index.html` | 侧边栏分组文案 |
| `src/api/static/js/i18n.js` | i18n key 更新 |
| index.html 版本号 bump | account.js?v=6, settings.js?v=25, app.js?v=130, i18n.js?v=10 |

---

## 4. R27 平台绑定双模式

### 4.1 后端设计

#### 4.1.1 新增 `gitcode_token` 字段

`UpdateCredentials` 模型增加字段：

```python
class UpdateCredentials(BaseModel):
    huawei_cloud_ak: str = ""
    huawei_cloud_sk: str = ""
    codearts_ak: str = ""
    codearts_sk: str = ""
    github_token: str = ""    # 已有
    gitcode_token: str = ""   # 新增
    repo_key: str = ""
```

#### 4.1.2 `update_credentials` 新增 GitCode token 验证逻辑

和 GitHub token 验证逻辑对称：

```python
if "gitcode_token" in explicit:
    _gc_tok = (explicit["gitcode_token"] or "").strip()
    if _gc_tok and not _is_placeholder(_gc_tok):
        # 验证 token：调 GitCode API /api/v5/user
        try:
            import requests as _req
            _resp = _req.get("https://gitcode.com/api/v5/user",
                             headers={"Authorization": f"token {_gc_tok}"},
                             timeout=10)
            if _resp.status_code == 200:
                _gc_user = _resp.json()
                gc_id = str(_gc_user.get("id", ""))
                gc_login = _gc_user.get("login", "")
                # 写入 external_identities（和 GitHub 逻辑对称）
                from src.auth.grape_account_db import (
                    get_external_identity, create_external_identity,
                    update_external_identity_tokens
                )
                existing = get_external_identity("gitcode", gc_id)
                if existing:
                    if existing["grape_user_id"] != user["id"]:
                        raise HTTPException(409, "该平台账号已被其他 Grape 账号绑定")
                    update_external_identity_tokens(
                        existing["grape_user_id"], "gitcode",
                        access_token=_gc_tok,
                        external_name=_gc_user.get("name", ""))
                else:
                    create_external_identity(
                        grape_user_id=user["id"], provider="gitcode",
                        external_id=gc_id, external_login=gc_login,
                        external_name=_gc_user.get("name", ""),
                        access_token=_gc_tok)
            elif _resp.status_code == 401:
                raise HTTPException(401, "令牌无效或已过期")
            else:
                raise HTTPException(502, f"GitCode 验证失败: {_resp.status_code}")
        except HTTPException:
            raise
        except Exception as _e:
            log.warning(f"GitCode token validation error: {_e}")
            raise HTTPException(502, f"验证失败: {_e}")
    elif not _gc_tok:
        # 清除：从 external_identities 删除 GitCode 绑定
        from src.auth.grape_account_db import delete_external_identity
        delete_external_identity(user["id"], "gitcode")
```

#### 4.1.3 错误码设计

手动令牌绑定的 API 错误响应需要前端能区分失败类型：

| HTTP Status | code 字段 | 含义 | 前端展示 |
|------------|----------|------|---------|
| 401 | 401 | 令牌无效/已过期 | "令牌无效或已过期，请在 [平台] 重新生成" |
| 409 | 409 | 该平台账号已被其他 Grape 账号绑定 | "该平台账号已被其他 Grape 账号绑定" |
| 502 | 502 | 平台 API 不可达 | "网络连接失败，请检查网络后重试" |
| 400 | 400 | 令牌为空/格式错误 | "令牌格式不正确" |

后端返回格式：`{"code": N, "message": "..."}`，前端从 `e.detail.code` 和 `e.detail.message` 读取。

#### 4.1.4 复用现有 API 路径

手动令牌绑定复用 `PUT /api/settings/credentials`，不新建路由。前端传 `gitcode_token` 或 `github_token` 字段。

### 4.2 前端设计

#### 4.2.1 绑定方式选择弹窗

用户点击"绑定"后，展示选择弹窗（不是直接跳转 OAuth）：

```
_accountShowBindOptions(provider)
  → 弹窗内容：
     ┌─────────────────────────────┐
     │  [平台图标] 绑定 [平台名]      │
     │                              │
     │  ┌─ OAuth 授权 ─┐ ┌─ 手动令牌 ─┐  │  ← 两个选项卡
     │  │              │ │            │  │
     │  │  [前往授权]   │ │ [令牌输入框] │  │
     │  │              │ │ [获取步骤▼] │  │
     │  │              │ │ [确认绑定]  │  │
     │  └──────────────┘ └────────────┘  │
     └──────────────────────────────────┘
```

**实现方式：** 复用 account.js 的 `_accountShowModal(html)` 弹窗。

#### 4.2.2 OAuth 授权 tab（默认选中）

```javascript
function _accountShowBindOptions(provider) {
  var isZh = _accountIsZh();
  var pName = provider === 'github' ? 'GitHub' : 'GitCode';
  var icon = platformIcon(provider, 24);

  var html = '<h3>' + icon + ' ' + (isZh ? '绑定 ' : 'Link ') + pName + '</h3>';
  
  // 选项卡
  html += '<div class="stabs" style="margin-bottom:16px">';
  html += '<a class="ac" id="bindTabOauth" onclick="_accountSwitchBindTab(\'oauth\')">' + (isZh ? 'OAuth 授权' : 'OAuth') + '</a>';
  html += '<a id="bindTabToken" onclick="_accountSwitchBindTab(\'token\')">' + (isZh ? '手动输入令牌' : 'Token') + '</a>';
  html += '</div>';

  // OAuth panel
  html += '<div id="bindPanelOauth">';
  html += '<p class="tx t2" style="margin-bottom:16px">' + (isZh ? '点击下方按钮跳转到 ' + pName + ' 授权页面' : 'Click below to go to ' + pName + ' authorization') + '</p>';
  html += '<button class="bt bt-p" style="width:100%" onclick="_accountDoOauthBind(\'' + provider + '\')">' + (isZh ? '前往授权' : 'Authorize') + '</button>';
  html += '</div>';

  // Token panel (hidden by default)
  html += '<div id="bindPanelToken" style="display:none">';
  html += _accountManualBindForm(provider);
  html += '</div>';

  _accountShowModal(html);
}
```

#### 4.2.3 手动令牌 panel

```javascript
function _accountManualBindForm(provider) {
  var isZh = _accountIsZh();
  var helpSteps = provider === 'github'
    ? (isZh ? '1. 前往 github.com → Settings<br>2. Developer settings → Personal access tokens<br>3. 生成新令牌（需 repo 权限）' : '...')
    : (isZh ? '1. 前往 gitcode.com → 设置<br>2. 私人令牌 → 新建令牌<br>3. 勾选项目读写权限' : '...');

  return '<div class="srow"><label>' + (isZh ? '访问令牌' : 'Access Token') + '</label>'
    + '<input type="password" id="manualTokenInput" class="kb-input" placeholder="' + (isZh ? '粘贴令牌' : 'Paste token') + '">'
    + '</div>'
    + '<div id="manualBindError" style="display:none;color:var(--r);font-size:12px;padding:4px 0"></div>'
    + '<details style="margin:8px 0"><summary style="cursor:pointer;font-size:12px;color:var(--a)">' + (isZh ? '如何获取令牌？' : 'How to get a token?') + '</summary>'
    + '<div style="font-size:12px;color:var(--t2);padding:8px 0;line-height:1.6">' + helpSteps + '</div></details>'
    + '<button class="bt bt-p" style="width:100%" id="manualBindBtn" onclick="_accountDoManualBind(\'' + provider + '\')">'
    + (isZh ? '确认绑定' : 'Confirm Bind') + '</button>';
}
```

#### 4.2.4 手动令牌提交逻辑

```javascript
function _accountDoManualBind(provider) {
  var isZh = _accountIsZh();
  var token = document.getElementById('manualTokenInput').value.trim();
  var errEl = document.getElementById('manualBindError');
  var btn = document.getElementById('manualBindBtn');
  
  if (!token) {
    if (errEl) { errEl.textContent = isZh ? '请输入令牌' : 'Please enter token'; errEl.style.display = 'block'; }
    return;
  }

  // loading 状态
  btn.disabled = true;
  var origText = btn.textContent;
  btn.textContent = isZh ? '验证中...' : 'Verifying...';

  var field = provider === 'github' ? 'github_token' : 'gitcode_token';
  var body = {};
  body[field] = token;

  API.put('/settings/credentials', body, 15000).then(function(d) {
    // 成功
    _accountCloseModal();
    var pName = provider === 'github' ? 'GitHub' : 'GitCode';
    app.showToast(isZh ? pName + ' 绑定成功' : pName + ' linked',
                  isZh ? '现在可以在项目页管理该平台的仓库' : 'You can now manage repos from this platform');
    // 清除仓库缓存
    localStorage.removeItem('repo_member_cache');
    // 刷新 bindings
    try {
      var _u = JSON.parse(localStorage.getItem('user') || '{}');
      if (!_u.bindings) _u.bindings = [];
      if (_u.bindings.indexOf(provider) < 0) _u.bindings.push(provider);
      localStorage.setItem('user', JSON.stringify(_u));
    } catch(e) {}
    renderAccount('security');
  }).catch(function(e) {
    // 失败——不关闭弹窗，在弹窗内显示错误
    btn.disabled = false;
    btn.textContent = origText;
    var msg = _accountTranslateBindError(e, provider);
    if (errEl) { errEl.textContent = msg; errEl.style.display = 'block'; }
  });
}
```

#### 4.2.5 错误翻译函数

```javascript
function _accountTranslateBindError(e, provider) {
  var isZh = _accountIsZh();
  var pName = provider === 'github' ? 'GitHub' : 'GitCode';
  var code = (e.detail && e.detail.code) || e.status || 0;
  switch (code) {
    case 401:
      return isZh ? '令牌无效或已过期，请在' + pName + '重新生成' : 'Token invalid or expired, please regenerate on ' + pName;
    case 409:
      return isZh ? '该平台账号已被其他 Grape 账号绑定' : 'This platform account is already bound to another Grape account';
    case 502:
      return isZh ? '网络连接失败，请检查网络后重试' : 'Network error, please try again';
    default:
      return e.message || (isZh ? '绑定失败，请重试' : 'Bind failed, please try again');
  }
}
```

#### 4.2.6 已绑定平台更新令牌

需求 27 规则：用户已通过 OAuth 绑定了某平台，再用手动令牌绑定同一平台时，提示"该平台已绑定，是否更新令牌？"。

在 `_accountShowBindOptions(provider)` 中检查绑定状态：

```javascript
// 如果已绑定，弹窗标题改为"更新令牌"
var bindings = (_accountProfile && _accountProfile.bindings) || [];
var alreadyBound = bindings.indexOf(provider) >= 0;
if (alreadyBound) {
  // 标题改为"更新 [平台名] 令牌"
  // OAuth tab 文案改为"重新授权"
  // 手动令牌 tab 文案不变
}
```

### 4.3 OAuth 绑定流程保持不变

现有 `_accountBindProvider(provider)` → `GET /auth/bind/{provider}` → redirect_url → OAuth 跳转 → 回调的流程不变。

改造点：`_accountBindProvider` 改为从绑定方式选择弹窗中触发（`_accountDoOauthBind`），不再是点击"绑定"按钮直接跳转。

### 4.4 令牌获取指引内容

| 平台 | 中文指引 | 英文指引 |
|------|---------|---------|
| GitCode | 1. 前往 gitcode.com → 设置<br>2. 私人令牌 → 新建令牌<br>3. 勾选项目读写权限 | 1. Go to gitcode.com → Settings<br>2. Personal Access Tokens → New<br>3. Select project read/write scope |
| GitHub | 1. 前往 github.com → Settings<br>2. Developer settings → Personal access tokens<br>3. 生成新令牌（需 repo 权限） | 1. Go to github.com → Settings<br>2. Developer settings → Personal access tokens<br>3. Generate new token (repo scope) |

---

## 5. R30 绑定与解绑体验优化

### 5.1 OAuth 回调反馈增强

**现有逻辑（app.js:475-490）：**

```javascript
// 成功：showToast('绑定成功', '可用该平台快捷登录')
// 失败：showToast('绑定失败', error_msg)
```

**增强后：**

```javascript
// 成功
var pName = bindSuccess === 'github' ? 'GitHub' : 'GitCode';
showToast(isZh ? pName + ' 绑定成功' : pName + ' linked',
          isZh ? '现在可以在项目页管理该平台的仓库，可用该平台快捷登录' : 'Manage repos from this platform, quick login enabled');

// 失败——区分错误类型
var failMsg = decodeURIComponent(bindError.replace(/\+/g, ' '));
var userFriendlyMsg = _translateOAuthError(failMsg, isZh);
showToast(isZh ? '绑定失败' : 'Bind failed', userFriendlyMsg);
```

**OAuth 错误翻译函数：**

```javascript
function _translateOAuthError(msg, isZh) {
  if (msg.indexOf('access_denied') >= 0 || msg.indexOf('denied') >= 0)
    return isZh ? '授权已取消，绑定未完成。您可以重新绑定或选择手动输入令牌' : 'Authorization cancelled. Try again or use manual token.';
  if (msg.indexOf('invalid_state') >= 0)
    return isZh ? '授权回调异常，请重新绑定。如多次失败可尝试手动输入令牌' : 'Callback error. Try again or use manual token.';
  if (msg.indexOf('already_bound') >= 0 || msg.indexOf('already') >= 0)
    return isZh ? '该平台账号已被其他 Grape 账号绑定' : 'Already bound to another account';
  return msg || (isZh ? '绑定失败，请重试' : 'Bind failed');
}
```

### 5.2 解绑反馈增强

**现有 `_accountUnbindProvider` 成功提示：**

```javascript
showToast(isZh ? '已解绑' : 'Unbound', isZh ? '该平台登录已失效' : 'Login via this provider is disabled');
```

**增强后——包含受影响仓库数量：**

后端 `DELETE /auth/bind/{provider}` 返回中增加 `affected_repos` 字段（仓库数量）：

```python
# 后端返回
return {
    "bindings": remaining_bindings,
    "affected_repos": len(affected_warehouses) if affected_warehouses else 0,
}
```

前端：

```javascript
var affectedCount = (d && d.affected_repos) || 0;
if (affectedCount > 0) {
  showToast(isZh ? pName + ' 已解绑' : pName + ' unbound',
            isZh ? '该平台登录已失效，' + affectedCount + ' 个仓库已不再可用。如需恢复请重新绑定'
                 : affectedCount + ' repos affected. Re-link to restore.');
} else {
  showToast(isZh ? pName + ' 已解绑' : pName + ' unbound',
            isZh ? '该平台登录已失效，该平台下的仓库不再可用'
                 : 'Platform login disabled, repos no longer available');
}
```

### 5.3 Loading 状态

所有绑定/解绑操作的按钮增加 loading 态：

```javascript
// 按钮加载态工具函数
function _accountSetBtnLoading(btnId, loading, loadingText) {
  var btn = document.getElementById(btnId);
  if (!btn) return;
  if (loading) {
    btn.disabled = true;
    btn._origText = btn.textContent;
    btn.textContent = loadingText || '...';
    // 可选：加 CSS spinner class
    btn.classList.add('kb-btn-loading');
  } else {
    btn.disabled = false;
    if (btn._origText) btn.textContent = btn._origText;
    btn.classList.remove('kb-btn-loading');
  }
}
```

CSS（app.css 追加）：

```css
.kb-btn-loading { pointer-events: none; opacity: .7; }
.kb-btn-loading::after {
  content: ''; display: inline-block; width: 12px; height: 12px;
  margin-left: 6px; border: 1.5px solid currentColor;
  border-top-color: transparent; border-radius: 50%;
  animation: kb-spin .6s linear infinite; vertical-align: middle;
}
@keyframes kb-spin { to { transform: rotate(360deg); } }
```

---

## 6. R26 项目创建与工作空间平台选择

### 6.1 后端：CreateRepoBody 增加 provider

```python
class CreateRepoBody(BaseModel):
    name: str
    description: str = ""
    private: bool = False
    visibility: str = ""
    auto_init: bool = True
    default_branch: str = "main"
    org: str = ""
    host_after_create: bool = False
    provider: str = ""  # 新增：用户指定创建到哪个平台
```

`create_new_repo` 优先用 `body.provider`：

```python
if body.provider:
    provider = body.provider
    if provider == "github":
        api_token = get_user_github_token(user["username"])
    else:
        api_token = get_user_token(user["username"])
else:
    provider, api_token = _get_user_provider_and_token(user["username"], user.get("login_provider", ""))
```

### 6.2 前端：新建项目弹窗平台选择

`openCreateRepoModal()` 改为 `openCreateRepoModal(presetProvider)`：

```javascript
function openCreateRepoModal(presetProvider) {
  // ... 现有逻辑 ...
  
  // 平台选择区域（仅多平台时显示）
  var platforms = _getAvailablePlatforms();
  var platformSelectHtml = '';
  if (platforms.length > 1) {
    var selectedProvider = presetProvider || platforms[0].key;
    platformSelectHtml = '<div class="create-repo-field">'
      + '<label>' + (isZh ? '目标平台' : 'Platform') + '</label>'
      + '<div class="create-repo-platforms">';
    platforms.forEach(function(p) {
      var cls = p.key === selectedProvider ? 'cr-platform cr-platform-act' : 'cr-platform';
      platformSelectHtml += '<button type="button" class="' + cls + '" data-provider="' + p.key + '" onclick="_selectCreatePlatform(this)">'
        + platformIcon(p.key, 20) + ' ' + p.name + '</button>';
    });
    platformSelectHtml += '</div></div>';
  }
  // 插入到表单顶部
  // ...
}
```

`submitCreateRepo` 传 provider：

```javascript
function submitCreateRepo(e) {
  e.preventDefault();
  // ...
  var platformBtn = document.querySelector('.cr-platform-act');
  var provider = platformBtn ? platformBtn.getAttribute('data-provider') : '';
  
  API.post('/repos/create', {name, description: desc, visibility, provider}, 120000)
    .then(r => {
      var pName = provider === 'github' ? 'GitHub' : 'GitCode';
      app.showToast(I18N.t('repos.create.success', {name: r.full_name || name}));
      // ...
    })
}
```

### 6.3 前端：新建项目按钮并列

`_buildProjectListHtml` 中的新建项目卡片改为按平台并列：

```javascript
// 单平台：一个按钮带平台图标
// 多平台：两个并列按钮
var platforms = _getAvailablePlatforms();
var addCardHtml;
if (platforms.length > 1) {
  addCardHtml = '<div class="project-card-add-row">';
  platforms.forEach(function(p) {
    addCardHtml += '<div class="project-card-add" onclick="openCreateRepoModal(\'' + p.key + '\')">'
      + '<span class="pca-ic">' + platformIcon(p.key, 24) + '</span>'
      + '<b>' + (isZh ? '新建 ' : 'New ') + p.name + '</b>'
      + '<span class="pca-tip">' + (isZh ? '在 ' + p.name + ' 上创建' : 'Create on ' + p.name) + '</span>'
      + '</div>';
  });
  addCardHtml += '</div>';
} else {
  var p = platforms[0] || {key: 'gitcode', name: 'GitCode'};
  addCardHtml = '<div class="project-card-add" onclick="openCreateRepoModal(\'' + p.key + '\')">'
    + '<span class="pca-ic">' + platformIcon(p.key, 24) + '</span>'
    + '<b>' + I18N.t('repos.lobby.new_project_btn') + '</b>'
    + '<span class="pca-tip">' + I18N.t('repos.lobby.new_project_tip') + '</span>'
    + '</div>';
}
```

### 6.4 前端：工作空间平台选择

`switchSidebarTab('workspace')` 改为：

```javascript
function switchSidebarTab(tab) {
  if (tab === 'workspace') {
    var platforms = _getAvailablePlatforms();
    if (platforms.length > 1) {
      // 多平台 → 显示平台选择
      _renderWorkspacePlatformSelect(platforms);
    } else {
      // 单平台 → 现有逻辑
      _enterWorkspaceForPlatform(platforms.length === 1 ? platforms[0].key : '');
    }
  }
  // ... 其他 tab 逻辑不变 ...
}

function _renderWorkspacePlatformSelect(platforms) {
  // 查每个平台的已托管仓库数量
  API.get('/repos/member').then(function(d) {
    var repos = d.repos || [];
    var cards = platforms.map(function(p) {
      var hosted = repos.filter(function(r) {
        var rk = r.repo_key || '';
        return p.key === 'github' ? rk.indexOf('github:') === 0 : rk.indexOf('github:') !== 0;
      }).filter(function(r) { return r.is_hosted; });
      return '<div class="ws-platform-card" onclick="_enterWorkspaceForPlatform(\'' + p.key + '\')">'
        + platformIcon(p.key, 36)
        + '<h3>' + p.name + '</h3>'
        + '<p>' + hosted.length + ' ' + (isZh ? '个已托管仓库' : 'hosted repos') + '</p>'
        + '</div>';
    }).join('');
    renderSelectionShell('workspace', '<div class="ws-platform-select">' + cards + '</div>');
  });
}

function _enterWorkspaceForPlatform(provider) {
  API.get('/repos/member' + (provider ? '?provider=' + provider : '')).then(function(d) {
    var repos = d.repos || [];
    var hosted = repos.filter(function(r) { return r.is_hosted; });
    if (hosted.length > 0) {
      var saved = null;
      try { saved = app.currentRepo(); } catch(e) { saved = null; }
      var matched = saved && hosted.find(function(r) { return r.full_name === saved.fullName; });
      var target = matched || hosted[0];
      selectRepo(target.full_name, target.owner, target.name, target.repo_key);
    } else {
      _renderWorkspaceEmpty();
    }
  });
}
```

---

## 7. 文件改动清单

### 7.1 前端

| 文件 | 改动 | 版本号 bump |
|------|------|------------|
| `src/api/static/js/pages/repos.js` | 提取 platformIcon + 替换 emoji + 新建项目弹窗平台选择 + 新建项目按钮并列 + 工作空间平台选择 | v48 |
| `src/api/static/js/pages/account.js` | 改名"用户设置" + 新增 appearance tab + 绑定双模式弹窗 + 体验优化反馈 + loading 态 | v6 |
| `src/api/static/js/pages/settings.js` | 删除 appearance tab + 删除 VCS Token UI + 精简 tabs | v25 |
| `src/api/static/js/app.js` | 文案改名 + OAuth 回调反馈增强 + platformIcon 替换 emoji | v130 |
| `src/api/static/index.html` | 侧边栏分组文案改名 | — |
| `src/api/static/js/i18n.js` | 新增/修改 i18n key | v10 |
| `src/api/static/css/app.css` | 追加 .kb-btn-loading 等样式 | — |

### 7.2 后端

| 文件 | 改动 |
|------|------|
| `src/api/routes/repos.py` | CreateRepoBody 增加 provider 字段 + create_new_repo 优先用 body.provider |
| `src/api/routes/settings.py` | UpdateCredentials 增加 gitcode_token 字段 + update_credentials 新增 GitCode token 验证逻辑 |
| `src/api/routes/grape_auth.py` | DELETE /auth/bind/{provider} 返回增加 affected_repos 字段 |

### 7.3 新增 i18n key

```
app.account.user_settings        — 用户设置 / User Settings
app.account.bind_title           — 绑定 {name} / Link {name}
app.account.bind_tab_oauth       — OAuth 授权 / OAuth
app.account.bind_tab_token       — 手动输入令牌 / Manual Token
app.account.bind_goto_auth       — 前往授权 / Authorize
app.account.bind_token_label     — 访问令牌 / Access Token
app.account.bind_token_placeholder — 粘贴令牌 / Paste token
app.account.bind_help_link       — 如何获取令牌？ / How to get a token?
app.account.bind_confirm         — 确认绑定 / Confirm Bind
app.account.bind_verifying       — 验证中... / Verifying...
app.account.bind_success         — {name} 绑定成功 / {name} linked
app.account.bind_success_desc    — 现在可以在项目页管理该平台的仓库 / Manage repos from this platform
app.account.bind_err_token       — 令牌无效或已过期 / Token invalid or expired
app.account.bind_err_bound       — 该平台账号已被其他 Grape 账号绑定 / Already bound to another account
app.account.bind_err_network     — 网络连接失败 / Network error
app.account.unbind_success_desc  — 该平台登录已失效，{n} 个仓库已不再可用 / {n} repos affected
repos.create.platform_label      — 目标平台 / Platform
repos.create.success_platform    — 项目已在 {platform} 创建成功 / Created on {platform}
```

---

## 8. 验证计划

### 8.1 单元测试

新增测试（mock DB + mock 邮件，遵循 D49 测试原则）：

| 测试文件 | 测试内容 |
|---------|---------|
| tests/test_settings.py | `update_credentials` 传 `gitcode_token` → 验证 GitCode API 被调用 + external_identities 写入 |
| tests/test_settings.py | `gitcode_token` 无效 → 401 |
| tests/test_settings.py | `gitcode_token` 已被其他账号绑定 → 409 |
| tests/test_repos.py | `create_new_repo` 传 `provider=github` → 用 GitHub API 创建 |
| tests/test_repos.py | `create_new_repo` 传 `provider=gitcode` → 用 GitCode API 创建 |
| tests/test_repos.py | `create_new_repo` 不传 provider → fallback `_get_user_provider_and_token` |

### 8.2 E2E 验证（Playwright）

| 场景 | 验证点 |
|------|--------|
| 品牌图标 | 项目页平台切换栏、新手引导页、用户设置第三方账号列表 — SVG 图标存在 |
| 用户设置改名 | 侧边栏"用户设置"文案 + 头像下拉菜单 + 页面标题 |
| 外观 tab 迁移 | 用户设置页面有 5 个 tab，外观 tab 可切换主题/语言 |
| 通用设置精简 | 通用设置只有 2 个 tab（项目凭据 + 通知），无 VCS Token 输入框 |
| 绑定方式选择 | 点击"绑定"弹出选项卡（OAuth + 手动令牌） |
| 手动令牌绑定 | 输入无效令牌 → 弹窗内显示错误，不关闭 |
| 新建项目平台选择 | 多平台时弹窗有平台选择卡片，单平台时不显示 |
| 新建项目按钮并列 | 多平台时项目列表显示两个并列的新建按钮 |
| 工作空间平台选择 | 多平台时点击工作台显示平台选择卡片 |
| Loading 状态 | 点击确认绑定后按钮变 loading |

### 8.3 回归测试

```bash
# 现有 79 测试全通过
.venv/bin/python -m pytest tests/test_grape_auth.py tests/test_auth_routes.py tests/test_auth_token.py --tb=short -q
```

---

## 9. 实施顺序

按需求间依赖关系：

```
Step 1: R28 品牌图标（platformIcon 提取 + emoji 替换）
  ↓ （其他需求依赖统一的 platformIcon）
Step 2: R29 用户设置重组（改名 + 外观 tab 迁移 + VCS Token 移除）
  ↓ （绑定双模式在用户设置框架内实现）
Step 3: R27 绑定双模式（后端 gitcode_token + 前端弹窗）
  ↓ （体验优化基于双模式）
Step 4: R30 体验优化（反馈增强 + loading 态）
  ↓ （独立于绑定流程）
Step 5: R26 平台选择（新建项目 + 工作空间）
  ↓
Step 6: 测试验证（单元 + E2E）
```

每步完成后 bump 版本号 + git commit，不 push（等用户确认后统一 push）。

---

## 10. 实施记录（2026-10-12）

### 10.1 提交链

| 提交 | 需求 | 内容 |
|------|------|------|
| 7430ffe84 | R28 | 提取全局 platformIcon(provider, size) 函数，_repoPlatformIcon 委托它；替换 repos.js/app.js/account.js 中所有 emoji 为品牌 SVG |
| 6779e56c8 | R29 | "用户中心"→"用户设置"全量文案替换；account.js 新增 appearance tab（主题/语言函数加 _account 前缀）；settings.js tabs 精简为 credentials+notifications，删除 VCS Token UI |
| e9b21e126 | R27 | 后端 UpdateCredentials 增加 gitcode_token + 验证逻辑（调 gitcode.com/api/v5/user）；前端 _accountBindProvider 改为绑定方式选择弹窗（OAuth + 手动令牌选项卡）+ _accountDoManualBind + _accountTranslateBindError |
| e7e24022c | R30 | OAuth 回调 _translateOAuthError 翻译错误码；解绑 _accountShowUnbindSuccess 含受影响仓库数量；后端 unbind_provider 返回 affected_repos；.kb-btn-loading CSS |
| 6881e0120 | R26 | 后端 CreateRepoBody 增加 provider 字段；前端 openCreateRepoModal(presetProvider) 弹窗平台选择卡片；新建项目按钮多平台并列；switchSidebarTab('workspace') 多平台显示 _renderWorkspacePlatformSelect |

### 10.2 版本号 bump 清单

| 文件 | 旧版本 | 新版本 |
|------|--------|--------|
| repos.js | v47 | v49 |
| account.js | v5 | v8 |
| settings.js | v24 | v25 |
| app.js | v129 | v131 |
| i18n.js | v9 | v9（未改） |

### 10.3 验证结果

- 单元测试：70 passed, 0 failed
- E2E Playwright：
  - 用户设置 5 tab ✅
  - 外观 tab 主题卡片 + 语言按钮 ✅
  - 第三方账号列表 SVG 图标 ✅
  - 通用设置 2 tab + 无 githubToken ✅
- 后端模块加载：settings.py + grape_auth.py + repos.py 全部 OK
- JS 语法：repos.js + account.js + settings.js + app.js 全部 OK

### 10.4 与设计文档的差异

| 设计方案 | 实际实施 | 差异原因 |
|---------|---------|---------|
| settings.js 的 selectTheme/selectLang 函数移到 account.js | 函数保留在 settings.js（死代码），account.js 新建 _account 前缀的独立函数 | 避免函数迁移导致断裂；settings.js 的 appearance tab 已从 tabs 数组移除，函数不会被触发 |
| 新增 i18n key（platformIcon/bind_title 等） | 未新增 i18n key，文案直接用 isZh 三元表达式 | 和现有 account.js 代码风格一致（account.js 全部用 isZh 三元，不用 i18n key） |
| app.js bindProvider 改为跳转到用户设置 | app.js bindProvider 保持直接 OAuth 跳转 | 引导页首次绑定直接 OAuth 是合理的，手动令牌绑定在用户设置中提供 |

### 10.5 通用设置凭据页去除代码仓库 API 区域（设计决策）

**决策：** 通用设置凭据页不再展示"代码仓库 API"区域（API Token 状态 + 账户名 + 跳转链接），只保留华为云 AK/SK。

**理由：**
1. 平台绑定信息已有归属——用户设置的账号安全 tab 完整展示（图标 + 用户名 + 绑定时间 + 更新/解绑按钮）
2. 通用设置的职责是项目级配置（华为云 AK/SK），平台绑定是账号级的事
3. 只读展示 + 跳转链接没有操作价值，用户在这里什么也做不了，增加认知负荷
4. 业界对标：GitHub 的 Access Tokens 在 Security 页面，不和 Actions Secrets 混在一起

**变更记录：**
- R29 初版：删除 VCS Token 输入框，但保留了只读展示 + 跳转链接
- 本次：彻底删除只读区域，通用设置凭据页只保留华为云 AK/SK
