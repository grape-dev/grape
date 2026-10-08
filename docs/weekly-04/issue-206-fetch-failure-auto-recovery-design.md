# Issue #206 技术方案：Grape 空闲后 Failed to fetch 自动恢复

> 状态：技术方案（基于 Issue #206 + Spec ready，2026-09-24）｜分支 weekly-04-lyz
> 关联：Issue #206（页面空闲后 Failed to fetch，需手动刷新恢复）、#63（fetch 超时/网络错误处理）、#86（token 滚动续期）

---

## 1. 背景与现状

### 1.1 问题

Grape 页面**一段时间不操作**后，再次操作出现 `Failed to fetch`，手动刷新后才恢复。验收要求：自动检测并恢复，不中断进行中操作。

### 1.2 现状代码核对（api.js，2026-09-24）

`src/api/static/js/api.js` 已具备部分能力，与 Spec 差异明显：

| 能力 | 现状（api.js） | 与 Spec 差距 |
|------|--------------|-------------|
| TypeError 重试 | ✅ L278-280：网络错误重试 **1 次（500ms）** | Spec 要求 3 次指数退避（1s/2s/4s） |
| 超时 abort | ✅ L249-253：30s abort（`_timedOut` 区分） | 满足 |
| 外部取消静默 | ✅ L275-276：nav/tab signal | 满足 |
| token 滚动续期 | ✅ #86：JWT exp 前主动刷新 + 401 被动刷新 | 满足（已排除 token 过期为主因） |
| **会话保活 heartbeat** | ❌ 无 | Spec 核心缺失 |
| **自动恢复/自动刷新** | ❌ 重试仍失败无兜底 | Spec 核心缺失 |
| **unhandledrejection 兜底** | ❌ 无全局拦截 | Spec 核心缺失 |

### 1.3 根因实证（生产环境，2026-09-24）

- nginx：`proxy_read_timeout 300s`（5min）已配置；但 **`keepalive_timeout` 未配置 → 默认 75s**，上游也无 `keepalive` 连接池。
- 机制：浏览器 ↔ nginx **空闲 keep-alive 超过 75s 后服务端关闭连接**；用户回来复用已死 socket → `TypeError: Failed to fetch`。手动刷新强制新建连接 → 恢复。与 issue 描述完全吻合。
- api.js L278 的 1 次重试应能恢复多数场景，但以下路径**不走该重试**（仍会冒泡 Failed to fetch）：
  - `upload`（XHR，L363-396 无重试）
  - `navigator.sendBeacon`（L339，时延上报无重试）
  - 首帧并发多个请求、或业务 `.catch` 未包住的调用
  - 重试 1 次在部分网络层（如 NAT/代理更长的空闲超时）下仍可能失败

---

## 2. 方案概述（三层恢复，v2 向业界主流收敛）

> 业界实践校准（2026-09-24）：**请求层「指数退避重试 + 幂等过滤」是核心最佳实践**；
> heartbeat 主要用于 WebSocket/SSE 保活，对 REST fetch 是辅助探测而非防断连；
> **自动整页刷新属反模式**（丢状态、放大压力），主流是「内联错误 + 手动重试」。
> v2 调整为：**重试为核心 → 心跳降级为探测 → auto-reload 默认关闭（仅提示+重试）**。

### 2.1 连接探测 heartbeat（辅助，不指望它防断连）

- 新增 `/api/ping` 轻量端点（后端，无鉴权、零 IO、浏览器可达——区别于仅 loopback 的 `/health`）。
- 前端每 60s ping 一次；**失败即视为「连接已断」信号**，供 `unhandledrejection`/失败恢复评估使用。
- 页面 `visibilitychange` 隐藏时暂停。
- 定位：不是"保持连接活着"（浏览器自动管理连接池，代理回收空闲连接是常态），而是**低成本探活**，缩短「断线→感知」延迟，避免用户操作时才碰钉子。

### 2.2 重试增强（api.js `_fetchWithTimeout`）——核心最佳实践

- 升级为**指数退避重试：初次 + 2 次重试（共 3 次尝试）**，间隔 500ms / 1500ms。
- **仅幂等方法重试**（GET/PUT/DELETE）；POST 不重试（避免重复提交副作用）——这是业界标准（axios-retry 同款语义）。
- 重试前检查 `document.visibilityState`：页面隐藏时不重试，直接抛。
- 重试仍失败 → 进入 2.3 的提示/恢复通道。

### 2.3 失败提示 + 手动重试（默认），auto-reload 显式开启（可选）

- `window.addEventListener('unhandledrejection')`：拦截未被业务 catch 的 `TypeError: Failed to fetch`，统一进入恢复流程。
- 恢复流程分级（v2）：
  1. **连接预检**：静默 `fetch('/api/ping')`；成功 → 瞬断已自愈，静默返回（请求层已重试过，此处不需要动作）。
  2. **仍断 → 默认行为 = 非阻塞提示 + 手动重试**：
     - Toast「连接已断开，点击重试」，用户点击后 `location.reload()`；
     - 不自动刷新页面、不中断正在输入的内容（业界主流：展示状态 + 用户可控恢复）。
  3. **auto-reload 显式开启时**（`localStorage.grapeAutoReload === '1'` 或页面 `data-auto-reload`）才自动刷新：
     - 仍做关键操作保护：正在输入（`<input>/<textarea>` focus、`contenteditable`）→ 不自动刷；
     - `[data-critical]` 关键页 → 不自动刷；
     - 满足条件 → `3s + random(0,3s)` 抖动后 `location.reload()`；
     - sessionStorage 5s 防循环标记，断网不无限刷。
- **写操作保护**：POST/PUT/DELETE 失败只提示，绝不自动刷新（防止重复提交/丢数据）。

### 2.3.1 系统压力评估（v2）

| 维度 | 评估 |
|------|------|
| 单次（手动/自动）刷新成本 | 低：首页 2-4 个内网 GET，毫秒级 |
| 并发触发面 | **默认不自动刷新** → 无齐刷风险；仅显式开启时才有，且 3s+抖动 + `data-critical` 保护 + 防循环三重限制 |
| 相比现状 | 手动刷新成本等同；本方案仅是「提示 + 可点重试」，非新增负载源 |
| heartbeat | 60s 一次 `/api/ping`，已排除日志/时延上报，开销可忽略 |

**配套建议（长期）**：nginx `keepalive_timeout` 75s→180s，从源头减少空闲断连（不依赖前端保活）。

### 2.4 后端 `/api/ping`

- 主 API 增加 `GET /api/ping`（无鉴权、不查 DB），返回 `{"ok":true, "ts":...}`。
- `_LOG_SKIP_PREFIXES` 加 `/api/ping`（高频探活不污染请求日志）。

---

## 3. 各模块改动

| 模块 | 改动 | 说明 |
|------|------|------|
| `src/api/main.py` | ✅ 修改 | 新增 `GET /api/ping`（无鉴权、零 IO、浏览器可达）+ `_LOG_SKIP_PREFIXES` 加 `/api/ping` |
| `src/api/static/js/api.js` | 修改 | `_fetchWithTimeout` 重试升级指数退避 + 仅幂等方法 |
| `src/api/static/js/auto-recovery.js`（新增） | 新增 | `/api/ping` 60s 探活 + unhandledrejection 拦截 + 默认 Toast「点击重试」+ auto-reload 显式开启才自动刷新（3s+抖动 + 关键操作保护 + 防循环） |
| `src/api/static/index.html` | 修改 | 引入 `auto-recovery.js`（`?v=` 版本化） |
| nginx（配套建议，配置期改） | 修改 | `keepalive_timeout 75s→180s`（源头减少空闲断连，不依赖前端） |

## 4. 关键设计决策

| 决策 | 选项 | 选择 | 理由 |
|------|------|------|------|
| 断线感知 | heartbeat 保活 / 重试失败时感知 / **ping 探活（60s）** | ping 探活 | 浏览器连接池自动管理，heartbeat 无法"防"代理回收；ping 只做低成本感知 |
| 请求级恢复 | 1次500ms / **指数退避3次** / 不重试 | 3次（500/1500ms） | 业界核心最佳实践；覆盖空闲连接老化 |
| 重试范围 | 全部 / **仅幂等** | 仅 GET+幂等写 | 避免 POST/PUT 重复提交（业务副作用） |
| 整页刷新 | **默认不自动** / 全自动 | **默认「提示+手动重试」，auto-reload 显式开启才自动** | 自动整页刷新是反模式（丢状态/放大压力）；业界主流是手动重试 |
| 关键操作保护 | 全局状态 / DOM 聚焦+标记 | DOM 聚焦 + `data-critical` | 低侵入；auto-reload 模式下不打断输入/关键页 |
| 刷新延迟（仅 auto-reload） | 固定3s / 3s+随机抖动 | **3s + random(0,3s)** | 打散齐刷 |
| 防循环刷新 | 无 / sessionStorage | sessionStorage 5s 窗口 | 断网不无限刷 |

## 5. 兼容与安全

- 现状 1 次重试对所有方法生效；改为仅幂等方法重试是**收紧**——非幂等失败行为不变（直接抛错提示），无回归。
- ping 60s 一次轻量请求，页面隐藏暂停；`_LOG_SKIP_PREFIXES` 排除日志污染。
- **默认不自动刷新** → 无丢状态/齐刷压力；auto-reload 是显式 opt-in。
- 自动刷新（如启用）受三重保护：聚焦输入不刷、`data-critical` 不刷、sessionStorage 5s 防循环。
- 写操作（POST/PUT/DELETE）失败只提示不刷新。
- 后端 `/api/ping` 不可用 → 探活失败计入断线信号，走统一恢复（不强依赖）。

## 6. 测试计划

- 前端：逻辑收敛在 `auto-recovery.js`（probe 调度、关键状态判定、防循环、auto-reload opt-in 判定）与 `api.js`（退避重试、幂等方法过滤）——人工验证 + 纯函数 node 断言。
- 后端：`test_ping_endpoint.py`——`GET /api/ping` 200 `{"ok":true}`（无鉴权、任意来源），且 `/health` 403 对照（证明 ping 是为浏览器设计的）。
- 回归：现有前端正常加载、登录续期（#86）、上传不受影响。

## 7. 验收对照

| 验收项（Spec） | 方案覆盖 |
|--------|---------|
| 空闲后加载失败自动恢复，无需人工 | ✅ 请求层重试 3 次自愈；仍失败默认 Toast「点击重试」，auto-reload 开启时自动恢复 |
| 不中断进行中输入/关键操作 | ✅ 默认不自动刷；auto-reload 模式含聚焦 + `data-critical` 保护 |
| 不再出现未处理 Failed to fetch | ✅ 2.2 重试增强 + 2.3 unhandledrejection 兜底（Toast 而非裸报错） |

---

## 8. 备注

- 根因（keepalive 75s 空闲断连）已在本机 nginx 实证；请求层重试是核心，ping 探活辅助，**nginx 长期调优**（keepalive_timeout 提升）为配套项。
- 业界校准：自动整页刷新默认关闭，与主流（axios-retry 式请求重试 + 内联错误/手动重试）一致；auto-reload 为可选。