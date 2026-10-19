# 方案：飞书扫码「已有配置进行中」→ 确认后重新开始（409 确认重开）

> 状态：**方案定稿（待实施）** ｜ 分支：`weekly-08-lyz`
> 关联：`feishu_qr_app_onboarding`（飞书扫码一键创建应用）/ 扫码配置并发限制
> 敏感信息：本文不含任何真实 IP、域名、口令、账号、token；示例一律占位符。

---

## 〇、问题现象

用户点击「扫码配置」绑定飞书时，页面反复提示 **「已有扫码配置正在进行，请稍候」**，**登出后重新登录仍无法解决**。

两个现象背后的根因：
1. **提示来源**：后端并发限制——同一用户同时仅允许一个进行中的扫码任务；存在 `pending`/`waiting_confirm` 状态任务时，新 start 一律拒绝（HTTP 409）。
2. **登出无效**：409 判定基于**数据库任务行**（按 `user_id` + `status`），与登录会话（JWT）无关；登出只清会话，不清 DB 行，故只要 DB 残留一条未终结任务，任何重试都被拒。

### 为什么任务会「滞留」在 waiting_confirm

- 扫码流程：start → 后台协程生成二维码（回写 `waiting_confirm` + `qr_url`）→ 用户扫码确认 → `connected`；或失败 → `failed`。
- `waiting_confirm` 只有在「确认成功 / 失败 / 调用取消 / 超时」时才离开该状态。
- 滞留场景：二维码生成后用户未扫码、或二维码过期后无自动终结路径；若承载后台协程的进程在 `waiting_confirm` 之后被重启，**进程内协程丢失但 DB 行仍停在 `waiting_confirm`**，且无人清理 → 永久堵塞该用户的扫码入口。

---

## 一、目标语义

| 场景 | 现状 | 目标 |
|------|------|------|
| 用户点「扫码配置」，已有进行中任务（pending/waiting_confirm） | 直接弹 toast「已有扫码配置正在进行」 | 弹**确认框**：「当前已有飞书机器人正在配置中，是否放弃并重新开始配置？」 |
| 用户选「重新开始配置」 | 无入口 | **先作废旧任务，再启动全新的扫码配置流程**；不再等待上一次配置 |
| 用户选「取消」 | 无入口 | 关闭弹窗，维持现状（不触碰旧任务） |
| 旧任务已 connected（扫码已完成） | — | 不弹确认（connected 是终态，属历史成功配置，不覆盖） |

> 原则：**「进行中但未完成」的配置可被用户主动放弃重开；「已完成配置」不可被意外覆盖。**

---

## 二、方案（Step 1：确认重开 + 旧任务作废）

### Step 1.1 后端：409 响应附带旧任务 task_id

`onboard_start` 在并发检查命中旧任务时，`HTTPException(409, detail={code:409, message, task_id: <旧task_id>})`——供前端直接取到旧任务作废，**无需新增查询接口**。

- 现状异常只带 `message`，前端无法知道该作废哪个任务。
- 顺带把旧任务的 `task_id` 放入 detail，前端可用 `e.detail.task_id` 直接调取消。

### Step 1.2 前端：409 时改为「确认框」而非直接 toast

`startFeishuOnboard`（chat.js）的 `catch` 分支：
- 收到 409 → `app.showConfirm("当前已有飞书机器人正在配置中，是否放弃并重新开始配置？", onConfirm, {danger:true})`；
- `onConfirm`（用户选「重新开始配置」）：
  1. `POST /feishu/onboard/cancel {task_id: e.detail.task_id}` ——置旧任务 `failed`（**cancel 已实现且幂等**：仅取消 pending/waiting_confirm，connected/failed 直接返回「已结束」）；
  2. 成功后重新调用 `startFeishuOnboard(botType, repoKey)` 走全新流程。
- 用户选「取消」→ 仅关闭弹窗，旧任务保留。

### Step 1.3 兜底（可选建议，建议同批实施）：stale 自动作废

即使有确认重开，仍存在「用户不知道有旧任务、或反复遇到提示」的残留。建议 `onboard_start` 并发检查**追加 stale 判定**：

> 若用户旧任务 `created_at` 距今超过 `STALE_TTL`（建议 10 分钟）且状态仍为 `pending`/`waiting_confirm` → **自动置 `failed`（reason=`stale onboard expired`）并放行新任务**，不再等用户确认。

- 目的：从源头杜绝「进程重启丢协程 → waiting_confirm 永久滞留」这一类无限阻塞（本次线下/集群现场就是该形态）。
- 与 Step 1.2 互补：Step 1.2 覆盖「用户在场主动重开」；Step 1.3 覆盖「用户不在场/忘记，stale 自动清理」。

---

## 三、各模块改动

| 模块 | 改动 | 状态 |
|------|------|------|
| `src/api/routes/feishu_onboard.py` | `start_onboard` 409 detail 增加 `task_id` 字段 | ⬜ 待实施 |
| `src/api/routes/feishu_onboard.py` | `start_onboard` 并发检查增加 `STALE_TTL`（超时自动作废旧任务） | ⬜ 待实施（建议） |
| `src/api/static/js/pages/chat.js` | `startFeishuOnboard` 409 → `showConfirm` 确认重开；确认后 cancel 旧任务 + 重启流程 | ⬜ 待实施 |
| `src/api/static/js/app.js` | `showConfirm` 已存在（复用，无需改） | ✅ 现有能力复用 |
| 测试 | UT：409 detail 含 task_id；cancel 后再 start 成功；stale 自动作废放行；connected 任务不触发确认 | ⬜ |

---

## 四、关键实现要点（已核实代码）

| 点 | 依据 |
|----|------|
| 前端能拿旧 task_id | API 错误对象已带 `e.detail`（`{code,message,...}`）；后端 409 detail 加 `task_id` 后 `e.detail.task_id` 可取 |
| cancel 已实现 | `cancel_onboard`（feishu_onboard.py）置 pending/waiting_confirm → `failed`；connected/failed 幂等返回「已结束」，跨进程安全（不依赖协程存活） |
| 确认弹窗已实现 | `app.showConfirm(message, onConfirm, {danger})`（app.js）已验证可用 |
| 并发安全 | start 的 `COUNT(user_id, status∈(pending,waiting_confirm))` 在事务锁内，cancel→start 竞态不会双建 |
| 集群分支 | 目标分支 `weekly-08-lyz`；同结构与单机一致，改动可同步 |

---

## 五、边界 / 风险

| 项 | 说明 |
|----|------|
| 放弃进行中任务的后果 | 用户选「重新开始」会作废**未完成**的旧扫码（不丢失已完成配置）；文案需明确「旧配置不会保存」 |
| connected 状态 | 终态，不触发确认框；不覆盖已有成功绑定 |
| 跨进程 | cancel 置 failed + DB 更新不依赖后台协程存活，进程重启后仍可作废旧任务 |
| 网络竞态 | 用户在确认弹窗期间他处再触发：cancel 后再 start 走 DB 最新状态，`COUNT` 原子性防止并发双建 |
| 文案 | 建议「当前已有未完成的飞书机器人配置，是否放弃并重新开始？（旧配置不会保存）」 |

---

## 六、测试用例设计

| 编号 | 用例 | 前置 | 断言 |
|------|------|------|------|
| UT-01 | 409 附带旧 task_id | 用户已有 waiting_confirm 任务，再 start | 409 `detail.task_id` == 旧任务 id |
| UT-02 | 确认重开：cancel 旧 + start 新 | 前端确认回调 mock cancel 成功 | 旧任务 `failed`；新任务成功创建（唯一 pending 任务） |
| UT-03 | cancel 幂等 | 旧任务 connected / failed 时调 cancel | 返回「已结束」，不覆盖已保存配置 |
| UT-04 | stale 自动作废 | 旧 waiting_confirm 任务 created_at > STALE_TTL | start 自动置旧任务 failed 并放行新任务 |
| UT-05 | connected 不触发确认 | 用户已有 connected 任务 | 新 start？→（按现状仍 409 提示，但**不弹确认重开**，避免误覆盖） |
| E2E-01 | 集群/单机联调 | 模拟一条 waiting_confirm 残留 → 前端点扫码 | 弹确认框→确认→新二维码；两次 start 均有 QR |

> UT-05 语义需与产品确认：connected 任务存在时，用户再点扫码应提示「已配置成功」而非「重开」——避免破坏已完成绑定。若确需支持「换绑」，另走换绑流程（不在本次范围）。

---

## 七、参考

- `src/api/routes/feishu_onboard.py`：start / status / cancel / 并发限制（现 409 无 task_id）
- `src/api/static/js/pages/chat.js`：`startFeishuOnboard` 409 catch（现直接 toast）
- `src/api/static/js/app.js`：`showConfirm`（复用现成确认弹窗）
- `src/api/static/js/api.js`：错误对象 `e.detail` / `e.status` 透传（409 detail 扩展无损）