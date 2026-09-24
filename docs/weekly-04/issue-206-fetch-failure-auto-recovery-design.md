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

## 2. 方案概述（三层恢复，与本仓库现状对齐）

在现基础上补 Spec 缺失的三层：**会话保活（预防）→ 重试增强（请求级）→ unhandledrejection + 关键操作感知刷新（兜底）**。

### 2.1 会话保活 heartbeat（新增）

- 新增 `/api/health` 轻量端点（后端，返回 `{"ok":true}`），或复用现有无需鉴权的健康检查。
- 前端每 60s `navigator.sendBeacon`/`API.get('/health', shortTimeout)` 一次；**失败不处理**，仅用于在 keep-alive 窗口内保持连接活跃、并验证连接是否已断（失败即为断线信号触发恢复评估）。
- 只在**页面可见 + 用户在线**时保活；`visibilitychange` 隐藏时暂停（省资源）。
- 60s < nginx keepalive 75s → 可保持连接不断。

### 2.2 重试增强（api.js `_fetchWithTimeout`）

- 现 L277-280「首次重试一次」升级为 **指数退避重试：最多 3 次（初次 + 2 次退避）**，间隔 `500ms / 1500ms`（Spec 建议 1s/2s/4s，本项目用更保守 500/1500 平衡等待）。
- **仅对 GET 与幂等写**重试；非幂等（POST/PUT 已触发服务端副作用）**不重试**（避免重复提交）。现状对所有方法都重试（L278 `e instanceof TypeError` 无方法区分）——需按 Spec 决策修正。
- 重试前检测 `document.visibilityState`：隐藏时不重试（页面不可见，直接抛）。
- `upload`（XHR）补 1 次重试（上传是幂等重建路径，安全）；`sendBeacon` 由 heartbeat 兜底不重试。

### 2.3 unhandledrejection + 关键操作感知自动恢复（新增 auto-recovery 逻辑）

- `window.addEventListener('unhandledrejection')`：捕获未被业务 catch 的 `TypeError: Failed to fetch`，统一走恢复流程。
- 恢复流程分级：
  1. **连接预检**：静默 `fetch('/api/health')`；成功 → 通知用户"已恢复"不做动作（连接瞬断）。
  2. **仍断**：检测关键状态——
     - 有 `<input>/<textarea>` 聚焦（正在输入）→ 不刷新，Toast「连接已断开，点击重试」
     - 页面有 `[data-critical]`（任务/会话等关键页）→ Toast + 按钮，不自动刷
     - **非关键页** → 延迟 `3s + random(0,3s)` 后 `location.reload()`（抖动打散，避免多名用户同时恢复时请求齐刷）
- **防循环**：刷新后 5s 内不自动再刷（sessionStorage 标记），避免断网时无限刷新。
- **幂等写保护**：自动恢复仅发生在「读/静默」请求失败后；写操作失败只提示不刷新。

### 2.3.1 系统压力评估（补充 2026-09-24）

| 维度 | 评估 |
|------|------|
| 单次刷新成本 | 低：首页初始约 2-4 个轻量 GET（/repos/member、/dashboard、/scenes、/pipeline），127.0.0.1 内网毫秒级 |
| 并发触发面 | 受控：非关键页才刷 + 3s**抖动**（+0~3s 随机）自然错开；`data-critical` 不自动刷；sessionStorage 5s 防循环封死"断网无限刷" |
| 相比现状 | 手动刷新也是同样加载成本，auto-recovery 只是自动化 + 有条件化，**非新增负载源** |
| 多人同时恢复 | 抖动使齐刷概率低；即使几十人同时，每用户 2-4 个 GET 总量仅数百，主服务可扛 |
| heartbeat | 60s 一次 `/api/health`，已排除时延上报高频路径，开销可忽略 |

**配套建议（长期）**：nginx `keepalive_timeout` 75s→180s，减少空闲断连发生频次，自动刷新自然更少被触达（不阻塞代码）。

### 2.4 后端 `/api/health`

- 主 API 增加 `GET /api/health`（无需鉴权），返回 `{"ok": true, "ts": ...}`。
- nginx 放行该路径（无 Auth 层拦截）；排除时延上报（`_LATENCY_SKIP_PREFIXES` 已含 /latency，health 同理跳过）。

---

## 3. 各模块改动

| 模块 | 改动 | 说明 |
|------|------|------|
| `src/api/routes/health.py`（新增）或 复用 | 新增 `GET /api/health` | 无鉴权健康探测，返回 `{"ok":true}` |
| `src/api/main.py` | 注册 health 路由 | 无鉴权（或 `require_no_session_token` 但 token 可空） |
| `src/api/static/js/api.js` | 修改 | `_fetchWithTimeout` 重试升级为指数退避 + 仅幂等方法重试；`upload` 补 1 次重试 |
| `src/api/static/js/auto-recovery.js`（新增） | 新增 | heartbeat 60s + unhandledrejection 拦截 + 关键操作感知刷新调度（3s+随机抖动）+ Toast + 防循环 |
| `src/api/static/index.html` | 修改 | 引入 `auto-recovery.js`（`?v=` 版本化） |
| nginx（配套建议，配置期改） | 修改 | `keepalive_timeout 180s` 或 `keepalive_timeout 75s` + 上游 `keepalive 32`（长期）——不作为代码依赖，前端 heartbeat 兜底 |

## 4. 关键设计决策

| 决策 | 选项 | 选择 | 理由 |
|------|------|------|------|
| 保活间隔 | 30s/60/120 | 60s | 小于 nginx 默认 75s keepalive，避免断连 |
| 重试次数/退避 | 1次500ms / 3次指数 / 不重试 | 3次（500/1500ms） | 覆盖空闲连接老化；比 Spec 1s/2s/4s 更保守减少等待 |
| 重试范围 | 全部 / 仅幂等 | 仅 GET+幂等写 | 避免 POST/PUT 重复提交（业务副作用） |
| 关键操作检测 | 全局状态 / DOM 聚焦+标记 | DOM 聚焦 + `data-critical` | 低侵入，不改各页面 |
| 刷新方式 | location.reload / SPA 重挂载 | location.reload | 简单可靠（Spec 同） |
| 刷新延迟 | 固定3s / 3s+随机抖动 | **3s + random(0,3s)** | 打散多人同时恢复的齐刷，防请求尖峰 |
| 防循环刷新 | 无 / sessionStorage 标记 | sessionStorage 5s 窗口 | 断网时不无限刷 |

## 5. 兼容与安全

- 现状 1 次重试对所有方法生效；改为仅幂等方法重试是**收紧**——非幂等失败行为不变（直接抛错提示），无回归。
- heartbeat 60s 一次轻量请求，忽略不计；页面隐藏暂停。
- `data-critical` 默认不存在 → 绝大多数页自动刷新；任务/会话关键页需标记（后续跟进）。
- 自动恢复只发生在读/静默失败；写失败仅提示。
- 若后端 `/api/health` 不可用，heartbeat 失败计入断线信号，走统一恢复（不强依赖）。

## 6. 测试计划

- 前端：无单测基建，逻辑收敛在 `auto-recovery.js`（heartbeat 调度、关键状态判定、防循环标记）与 `api.js`（退避重试、幂等方法过滤）——以人工验证为主；可抽取纯函数用 node 快速断言。
- 后端：`test_health_endpoint.py`——`GET /api/health` 200 `{"ok":true}`（无鉴权）。
- 回归：现有前端页面正常加载、登录续期（#86）、上传不受影响。

## 7. 验收对照

| 验收项（Spec） | 方案覆盖 |
|--------|---------|
| 空闲后加载失败自动刷新/恢复，无需人工 | ✅ 2.3 自动恢复（非关键页 3s 刷新） |
| 不中断进行中输入/关键操作 | ✅ 2.3 DOM 聚焦 + data-critical → Toast 不刷 |
| 不再出现未处理 Failed to fetch | ✅ 2.2 重试增强 + 2.3 unhandledrejection 兜底 |

---

## 8. 备注

- 根因（keepalive 75s 空闲断连）已在本机 nginx 实证；前端 heartbeat+重试是应用层兜底，**nginx 长期调优**（keepalive_timeout 提升/连接池）为配套项，非代码阻塞。
- 本方案为技术设计，待实施后按 grape-dev-workflow 提交 `weekly-04-lyz`。