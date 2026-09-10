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
| `src/api/static/js/api.js` | `ensureTokenValid` + `scheduleProactiveRefresh` + `_makeHeaders` + 导出 |
| `src/api/static/js/app.js` | 登录/OAuth/页面恢复时启动定时器，logout 时清定时器 |
| `src/api/static/index.html` | api.js?v=23, app.js?v=67 |

## 9. 不改的点

- `jwt_utils.py`：ACCESS_TOKEN_TTL 不变
- `auth.py`：`/auth/refresh` 端点不变
- `_refreshPromise` 单例机制复用
- `storage` 事件多 Tab 同步复用
- 401 被动刷新保留作为最终兜底

## 10. 验收标准

- [x] 过期前 2 分钟自动刷新——A 定时器
- [x] 长时间空闲后首次请求不 401——B 请求前检查兜底
- [x] 并发只触发一次——复用 `_refreshPromise`
- [x] 多 Tab 同步——复用 `storage` 事件
- [x] refresh_token 过期仍跳登录——401 兜底 + doRefresh 失败跳登录

## 11. Playwright 验证证据

- token remaining = 899s，delay = 779s（12min 后触发）✅
- B 模式 remaining > 阈值：token 不变 ✅
- B 模式 remaining < 阈值：token 变 + exp 延长 ✅
- refresh API：HTTP 200 + 新 token ✅
- console errors: 0 ✅
