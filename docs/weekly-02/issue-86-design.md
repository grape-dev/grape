# Issue #86: JWT Token 主动刷新设计

## 1. 问题

access_token 15 分钟过期，前端只在 401 后被动刷新。用户感知到请求中断（401 → 等 refresh → 重试）。

## 2. 方案：A+B 组合

统一入口 `ensureTokenValid()`，定时器(A)和请求前(B)都调此函数。

### A（定时器）
- 登录成功后解析 JWT exp，在过期前 2 分钟设 setTimeout
- 到点调 `ensureTokenValid()`，刷新后重新设定时器
- 覆盖长时间空闲场景

### B（请求前检查）
- 每次 API 请求前 `_makeHeaders()` 调 `await ensureTokenValid()`
- token 剩余 > 阈值 → 返回已 resolve 的 Promise，微任务级开销
- token 剩余 ≤ 阈值 → 调 `doRefresh()` 刷新
- 覆盖定时器被浏览器节流/暂停的场景

### 阈值
- `ACCESS_TOKEN_TTL = 15 * 60`（15 分钟）
- `_REFRESH_THRESHOLD_MS = 2 * 60 * 1000`（2 分钟）
- 2min / 15min = 13%，在 10%~20% 最佳实践范围内
- delay 下限 30 秒：`Math.max(remaining - threshold, 30000)`

## 3. 核心函数

### ensureTokenValid()

```
1. 取 localStorage token
2. 解析 JWT exp，算剩余有效期
3. remaining > 阈值 → 返回 token（已 resolve Promise）
4. remaining ≤ 阈值 → 调 doRefresh()（复用 _refreshPromise 单例）
5. doRefresh 失败 → 重试 1 次（500ms 后）
6. 重试仍失败 → 返回原 token（401 被动刷新兜底）
```

注意：`ensureTokenValid()` 是 async，永远返回 Promise。token 充足时返回的是已 resolve 的 Promise，`await` 只经历一次微任务调度，没有网络等待。

### scheduleProactiveRefresh()

```
1. clearTimeout 旧定时器
2. 取 token，无 token → return
3. 算 remaining，已过期 → return（等 401 兜底）
4. delay = max(remaining - 2min, 30s)
5. setTimeout → ensureTokenValid() → 成功后递归 scheduleProactiveRefresh()
```

定时器回调包 try/catch，失败静默（401 兜底）。

## 4. 不循环

401 拦截器（`handle401`）直接调 `doRefresh()`，不调 `ensureTokenValid()`。重发请求时 `_get` → `_makeHeaders` → `ensureTokenValid()`，但 token 刚刷新过（remaining > 阈值），直接返回不刷新——不循环。

## 5. 并发安全

`doRefresh` 第 77 行 `if (_refreshPromise) return _refreshPromise`——并发请求/定时器同时触发刷新时，只发一次 refresh 请求，所有调用方拿同一个 Promise。

`_refreshPromise` 在 `finally`（第 108 行）里清空——重试时发起新请求，不会拿到失败的 Promise。

## 6. 失败处理

| 层级 | 失败处理 |
|------|---------|
| ensureTokenValid 重试 | 500ms 后重试 1 次，仍失败返回原 token |
| 401 被动刷新 | handle401 → doRefresh → 失败跳登录 |
| refresh_token 过期 | doRefresh 返回 401/403 → 清 token → 跳登录 |

只有 401 被动刷新也失败才跳登录——核心兜底没丢。

## 7. 时钟偏差

JWT exp 按客户端本地时间算。若客户端时间不准，`remaining` 会偏。已有 401 被动刷新兜底，可接受。

## 8. 改动文件

| 文件 | 改动 |
|------|------|
| `src/api/static/js/api.js` | `ensureTokenValid` + `scheduleProactiveRefresh` + `_makeHeaders` + 导出 + upload 走 ensureTokenValid + storage 事件重设定时器 + 删旧 headers() |
| `src/api/static/js/app.js` | 登录/OAuth/页面恢复时启动定时器，logout 时清定时器 |
| `src/api/static/index.html` | api.js?v=24, app.js?v=67 |
| `.gitignore` | 加 `src/api/static/uploads/` 防误提交 |

## 9. 不改的点

- `jwt_utils.py`：ACCESS_TOKEN_TTL 不变
- `auth.py`：`/auth/refresh` 端点不变
- `_refreshPromise` 单例机制复用
- `storage` 事件多 Tab 同步复用（补 scheduleProactiveRefresh 调用）
- 401 被动刷新保留作为最终兜底
- observability/chat/events 等页面的直接 fetch（已有 401 兜底，不在 #86 范围）

## 10. 边界处理

### 10.1 upload 函数

upload 用 XMLHttpRequest，原先直接读 `localStorage.getItem('token')` 不走 `ensureTokenValid`。
改为 `ensureTokenValid().then(token => ...)`——上传前确保 token 有效。

### 10.2 多 Tab / 后台 Tab 定时器失效

两种场景会导致定时器失效：

**多 Tab**：其他 Tab 刷新 token 后，当前 Tab 的旧定时器按旧 token 的 exp 算时间。旧定时器触发时发现新 token 还很充足，`ensureTokenValid` 直接返回不刷新也不重设——定时器停摆。

**后台 Tab**：浏览器节流/暂停后台 Tab 的 setTimeout。Tab 切回前台时定时器可能已延迟很久没触发。

修复（3 个触发点都调 `scheduleProactiveRefresh`）：
- `storage` 事件：其他 Tab 更新 token 时重设
- `visibilitychange` 事件：Tab 切回前台时重设
- `ensureTokenValid` 成功后递归重设

`scheduleProactiveRefresh` 幂等——先 `clearTimeout` 再 `setTimeout`，多次调用不会多定时器并存。

### 10.3 ensureTokenValid 的 async 语义

`ensureTokenValid()` 是 `async`，永远返回 Promise。token 充足时返回的是已 resolve 的 Promise，`await` 只经历一次微任务调度（微秒级），没有网络等待。不是"同步返回"，而是"异步开销极小可忽略"。

### 10.4 _refreshPromise 清空确认

`doRefresh` 的 `finally`（第 108 行）清空 `_refreshPromise = null`。`ensureTokenValid` 重试时 `_refreshPromise` 已是 null，第二次 `doRefresh` 会发起新请求——重试生效。

## 11. 验收标准

- [x] 过期前 2 分钟自动刷新——A 定时器
- [x] 长时间空闲后首次请求不 401——B 请求前检查兜底
- [x] 并发只触发一次——复用 `_refreshPromise`
- [x] 多 Tab 同步——storage 事件 + scheduleProactiveRefresh 重设定时器
- [x] refresh_token 过期仍跳登录——401 兜底 + doRefresh 失败跳登录
- [x] upload 走 ensureTokenValid——上传前确保 token 有效
- [x] 定时器不停摆——storage 事件重设定时器

## 12. Playwright 验证证据

- token remaining = 899s，delay = 779s（12min 后触发）✅
- B 模式 remaining > 阈值：token 不变 ✅
- B 模式 remaining < 阈值：token 变 + exp 延长 ✅
- refresh API：HTTP 200 + 新 token ✅
- refresh 后 token 字符串不同，iat/exp 都更新 ✅
- console errors: 0 ✅
