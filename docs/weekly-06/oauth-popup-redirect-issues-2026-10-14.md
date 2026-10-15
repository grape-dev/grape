# OAuth 绑定弹窗重定向问题清单

> 分支：feat/oauth-security-hardening-330
> 日期：2026-10-14
> 关联设计文档：docs/weekly-06/grape-account-system-tech-design-v2.md（需求 3/6）
> 关联审计文档：docs/weekly-06/grape-account-system-audit-2026-10-14.md（P1-5 OAuth 回调路由）

## 问题现象

绑定 GitCode/GitHub 时，弹窗（popup）有时不正常关闭/回传结果给父窗口，而是弹窗内页面重定向到用户设置页面（`/?xxx#account`），在弹窗里加载了完整的主站账号设置界面。用户看到的是一个多余的弹窗页面，而非自动关闭。

问题是间歇性的——"有时候"出现。

---

## 问题 1（P1）：popup-callback.html 降级路径加载完整主站页面

### 是什么

`oauth-popup-callback.html` 第 124-148 行降级逻辑：当 `window.opener` 为 null 时，popup 自身跳转到 `/?bind_success=gitcode#account` 等主站 URL，在弹窗内加载完整主站页面。

### 影响

1. 弹窗显示完整主站账号设置页面，用户困惑（弹窗里出现了和主窗口一样的页面）
2. 父窗口的轮询逻辑（account.js 第 587-595 行 `popup.closed` 检测）虽然能兜底确认绑定状态，但弹窗未关闭，用户体验差
3. 弹窗加载完整 SPA 页面（重、慢），降级路径本应轻量

### 触发条件

OAuth 流程中 popup 经历跨域跳转：
1. 父窗口 `https://grape.example.com` 打开 popup
2. popup 跳到 `https://gitcode.com/oauth/authorize?...`（跨域）
3. gitcode 回调 popup 到 `https://grape.example.com/api/auth/callback/gitcode`
4. 服务端 302 到 `/oauth-popup-callback.html?bind_success=gitcode`
5. popup-callback.html 执行 `window.opener.postMessage(...)`

步骤 2 的跨域跳转会导致部分浏览器**切断 `window.opener` 引用**：
- Safari ITP（Intelligent Tracking Prevention）
- 移动端浏览器（iOS/Android 部分版本）
- 某些隐私设置下的 Chrome/Firefox
- 浏览器设置了 `rel=noopener` 等值的链接跳转

此时 `window.opener` 为 null，进入降级路径。

### 涉及文件

- `src/api/static/oauth-popup-callback.html` 第 124-148 行
- `src/api/static/js/pages/account.js` 第 585-595 行（轮询兜底）

### 根因

降级路径设计为"跳回主站让首页 URL 参数处理逻辑恢复状态"，但这会在弹窗里加载完整主站页面。应该降级为只显示一个静态提示页面（"授权已完成，请返回主窗口"），不跳转主站。popup-callback.html 第 97-104 行已实现 `showFallback` 提示组件，但仅用于 `window.close()` 失效时的提示，未用于 opener 不可用的降级场景。

---

## 问题 2（P2）：oauth_callback 早期错误路径未传 popup 参数

### 是什么

`grape_auth.py` 的 `oauth_callback` 函数中，三个早期错误分支未传 `popup=True`，导致 popup 模式的请求被当作非 popup 模式处理，服务端 302 重定向到主站页面而非中间页。

### 影响

popup 模式的请求在早期错误分支中被当作非 popup 模式处理：
- 服务端 302 重定向到 `/?oauth_error=xxx`（主站页面）而非 `/oauth-popup-callback.html?oauth_error=xxx`（中间页）
- 弹窗加载完整主站页面

### 触发条件

三个分支：

| 行号 | 条件 | 能否读 popup | 现状 |
|------|------|-------------|------|
| 1697-1701 | state_data 为 None（state 过期/重复消费） | 不能（state 已无效） | 未传 popup |
| 1705-1706 | state_provider 为空 | **能**（state_data 存在） | 未传 popup |
| 1707-1709 | provider 不匹配 | **能**（state_data 存在） | 未传 popup |

- 行 1705-1709：`popup` 变量在 第 1712 行才从 `state_data` 中提取，但 provider 校验在第 1703-1709 行就提前返回了，**完全有能力读 popup 标记但没读**
- 行 1697-1701：state 已过期或被重复消费，确实无法从 state_data 得知是否 popup 模式

### 涉及文件

- `src/api/routes/grape_auth.py` 第 1697-1709 行（早期错误分支）
- `src/api/routes/grape_auth.py` 第 1712 行（popup 变量提取位置）

### 根因

`popup = bool(state_data.get("popup"))` 的提取位置在 provider 校验之后，应提前到 state 校验之后立即提取。对于 state_data 为 None 的情况，需要从其他方式判断是否 popup 模式（或统一用 popup-callback.html 兜底）。

---

## 触发场景汇总

| 场景 | 问题 | 触发条件 | 频率 |
|------|------|----------|------|
| 弹窗跨域后 window.opener 被浏览器切断 | 问题 1 | Safari/移动端/隐私模式 | 高频 |
| state 过期或被重复消费 | 问题 2 (1697-1701) | 用户刷新/浏览器预取/网络重试 | 低频 |
| provider 校验失败 | 问题 2 (1705-1709) | state 数据异常 | 极低频 |

---

## 修复方向（待确认）

### 问题 1 修复方向

popup-callback.html 降级路径改为只显示静态提示页面（复用已有的 `showFallback`），不跳转主站。父窗口的轮询逻辑（`popup.closed` 检测）已能兜底确认绑定状态，无需弹窗跳回主站。

### 问题 2 修复方向

1. 将 `popup = bool(state_data.get("popup"))` 提前到 provider 校验之前（state_data 存在的所有分支都能读到）
2. 对于 state_data 为 None 的情况，考虑从 URL referer 或其他方式判断，或统一用 popup-callback.html 兜底（无 harm：非 popup 模式下 popup-callback.html 的 postMessage 找不到 opener，走降级提示页也不影响）

---

## 关联文件索引

| 文件 | 关键行 | 说明 |
|------|--------|------|
| `src/api/routes/grape_auth.py` | 1676-1785 | oauth_callback 主函数 |
| `src/api/routes/grape_auth.py` | 1108-1132 | _build_redirect_url（popup 重定向构造） |
| `src/api/routes/grape_auth.py` | 1135-1148 | _oauth_error_redirect / _bind_error_redirect |
| `src/api/routes/grape_auth.py` | 1348-1500 | _handle_oauth_bind（绑定成功/失败重定向） |
| `src/api/static/oauth-popup-callback.html` | 106-152 | postMessage + 降级跳转逻辑 |
| `src/api/static/js/pages/account.js` | 454-465 | _oauthOpenCenteredPopup（弹窗打开） |
| `src/api/static/js/pages/account.js` | 471-618 | _accountDoOauthBind（绑定发起 + 消息监听） |
| `src/api/static/js/pages/account.js` | 585-595 | popup.closed 轮询兜底 |
| `src/api/static/js/app.js` | 489-531 | _checkBindSuccess（主站 URL 参数处理） |
| `src/auth/providers.py` | 386-436 | get_redirect_uri / _get_trusted_host |

---

## 问题 6（P2）：绑定模态框被 OAuth popup 窗口遮挡

### 是什么

用户在账号安全页点击"绑定 GitCode/GitHub"，弹出绑定方式选择模态框（`_accountShowModal`，z-index:300）。用户点击"前往授权"后，`_accountDoOauthBind` 打开浏览器新窗口（`window.open`）跳转到 GitCode 授权页。

popup 窗口打开后**抢占操作系统焦点**，覆盖在主窗口之上。主窗口内的模态框（"授权进行中"spinner 状态）虽然仍在 DOM 中，但视觉上被 popup 浏览器窗口遮挡，用户感觉"弹出层突然看不到了，被隐藏到浏览器后面了"。

### 影响

1. 用户看不到"授权进行中"提示，以为主窗口出了问题
2. 如果 popup 走降级路径（问题 1），popup 关闭后主窗口模态框仍在，但用户可能已经困惑操作了其他东西
3. 用户体验割裂——模态框说"请在弹窗中完成授权"，但弹窗打开后模态框本身看不到了

### 触发条件

桌面端点击"前往授权"打开 OAuth popup 时必定出现。移动端不走 popup（第 487-494 行直接全页跳转），无此问题。

### 涉及文件

- `src/api/static/js/pages/account.js` 第 499 行（`_oauthOpenCenteredPopup('')` 打开 popup）
- `src/api/static/js/pages/account.js` 第 512-533 行（模态框替换为"授权进行中"状态）
- `src/api/static/js/pages/account.js` 第 1233-1248 行（`_accountShowModal` overlay z-index:300）

### 根因

这是浏览器 popup 模式的固有行为——`window.open` 打开的新窗口会抢占焦点并覆盖在主窗口之上。模态框在主窗口 DOM 内，无法显示在独立浏览器窗口之上。

### 注意

此问题与问题 1（降级路径重定向）叠加后更严重：popup 打开 → 遮挡主窗口 → popup 内走降级路径跳到主站 → 用户在 popup 里看到主站页面 → 主窗口模态框还在"授权进行中"但用户已经不在这里了。

---

## 问题 7（P0）：绑定 popup 模式引入过多中间步骤导致卡顿

### 是什么

对比登录页（app.js 第 1524-1532 行）和旧版绑定，新版 popup 模式步骤从 2 步膨胀到 7 步，用户感觉明显卡顿。

### 登录页 vs 绑定页流程对比

**登录页（app.js 第 1524-1532 行）—— 2 步，流畅：**

```
用户点击 GitCode 登录
  → API.get('/auth/login/gitcode')          // 1 次后端请求
  → window.location.href = d.redirect_url   // 直接全页跳转到 GitCode
```

**绑定旧版（改前）—— 同样 2 步，流畅：**

```
用户点击"前往授权"
  → API.get('/auth/bind/gitcode')           // 1 次后端请求
  → window.location.href = d.redirect_url   // 直接全页跳转
```

**绑定新版（当前）—— 7 步，卡顿：**

```
用户点击"前往授权"
  → _oauthOpenCenteredPopup('')             // ① 先同步打开空白弹窗
  → 模态框 innerHTML 替换为 spinner          // ② DOM 操作：替换模态框内容
  → overlay.cloneNode(true) + replaceChild  // ③ DOM 操作：克隆整个 overlay 节点
  → popup.document.write(...)               // ④ 往空白弹窗写 loading HTML
  → API.get('/auth/bind/gitcode?popup=true') // ⑤ 后端请求
  → popup.location.href = d.redirect_url    // ⑥ 弹窗跳转到 GitCode
  → 注册 message handler + setInterval 轮询  // ⑦ 事件监听 + 500ms 轮询定时器
```

### 卡顿原因

| 原因 | 位置 | 说明 |
|------|------|------|
| 先开空白弹窗再异步请求 URL | 第 499 行 vs 第 545 行 | 用户先看到空白弹窗弹出，等 API 返回后才跳转，有视觉延迟 |
| cloneNode 克隆整个 overlay | 第 529 行 | 深拷贝整个 DOM 节点再 replaceChild，触发重排重绘，视觉闪烁 |
| popup.document.write 同步写入 | 第 537-541 行 | 阻塞 popup 解析，弹窗从白屏变 loading 再跳转，多一次视觉切换 |
| 视觉切换次数 3 次 | 整体 | 弹窗弹出(空) → loading → GitCode 跳转，旧版只有 1 次跳转 |

### 核心差异

| 方面 | 登录页/旧版绑定 | 新版绑定 |
|------|----------------|----------|
| 流程 | 1 次 API → 直接跳转 | 开空白弹窗 → DOM 操作 ×2 → document.write → API → 弹窗跳转 → 注册监听+轮询 |
| 用户看到 | 点击后直接跳到 GitCode | 点击后弹窗弹出(空)→ 变 loading → 跳转到 GitCode |
| DOM 操作 | 0 次 | 3 次 |
| 定时器 | 0 | 1 个 500ms 轮询 + 1 个 60s 超时 |
| 视觉切换 | 1 次（跳转） | 3 次（弹窗弹出 → loading → 跳转） |

### 涉及文件

- `src/api/static/js/app.js` 第 1524-1532 行（登录页 — 对比基准，保持不变）
- `src/api/static/js/pages/account.js` 第 471-547 行（绑定 — 需优化）

---

## 架构优化方案

### 设计原则

1. **登录场景：保持全页跳转**（业界标准，兼容性最强，用户体验好）——当前代码已最优，不动
2. **绑定场景：用弹出层但需优化**——用户在已登录的设置页面，不应丢失当前页面状态

### 优化点 1：先请求 URL 再开弹窗（消除空白弹窗阶段）

当前：先开空白弹窗 → 异步请求 URL → 设置 popup.location.href
优化：先异步请求 URL → 拿到 URL 后再 window.open(redirect_url) → 弹窗直接打开 GitCode

注意：window.open 在异步回调中可能被浏览器拦截（不在用户同步点击上下文中）。折中方案：保留同步开空白弹窗，但消除后续 DOM 操作。

### 优化点 2：消除 cloneNode 操作（第 529 行）

当前用 cloneNode(true) + replaceChild 移除点击关闭监听。
优化：不克隆节点，用 dataset 属性控制：

```js
overlay.addEventListener('click', function(e) {
    if (e.target === overlay && !this.dataset.locked) _accountCloseModal();
});
// 授权进行时：
overlay.dataset.locked = '1';  // 一个属性赋值，不触发 DOM 重建
```

### 优化点 3：消除 popup.document.write（第 537-541 行）

当前往空白弹窗写入 loading HTML。
优化：用 about:blank 默认白页，或用静态 loading.html 文件作为 popup 初始 URL，避免 document.write 阻塞。

### 优化点 4：消除模态框内容替换（第 514-523 行）

当前把绑定选择模态框内容替换为"授权进行中"spinner。
优化：不替换模态框内容，只在按钮上显示 loading 状态（按钮已有 spinner 逻辑）。

### 优化后流程（4 步，减少 3 步）

```
用户点击"前往授权"
  → API.get('/auth/bind/{provider}?popup=true')  // ① 后端请求（按钮显示 spinner）
  → window.open(redirect_url, ...)               // ② 弹窗直接打开 GitCode
  → 注册 message handler                         // ③ 事件监听
  → setInterval 轮询 popup.closed                // ④ 轮询兜底
```

视觉切换：1 次（弹窗直接打开 GitCode 授权页）。

### 降级路径优化（问题 1 修复）

popup-callback.html 的降级路径改为只显示静态提示页面（复用 showFallback），不跳转主站。父窗口的 popup.closed 轮询已能兜底确认绑定状态。

### popup 遮挡问题处理（问题 6）

- 弹窗尺寸缩小（600×700 → 480×640），减少遮挡面积
- 弹窗位置偏移到屏幕一侧（不居中），让主窗口露出部分
- 主窗口显示轻量顶部条幅"授权进行中，请在弹窗中完成"（不依赖模态框）
