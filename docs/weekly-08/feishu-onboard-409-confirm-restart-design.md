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

### 关联场景：已配置机器人（connected）后再次扫码「换绑」

- 用户**已成功配置**过一个机器人（`connected`）→ 想换绑/更换应用 → 再次点「扫码配置」。
- 现状：`start_onboard` 并发检查只挡 `pending/waiting_confirm`，**connected 是终态不拦截** → 无任何提示，静默进入新扫码；扫码成功后 `_apply_chat_credentials` **自动释放旧 app、清理旧会话、登记新 app**——**旧绑定不可逆失效，且无二次确认**。
- 本文档补充：**该「换绑」场景应在前端明确提示并确认**（纯前端，后端仅「未进行中」语义即可，无需改后端）。

---

## 一、目标语义

| 场景 | 现状 | 目标 |
|------|------|------|
| 用户点「扫码配置」，已有进行中任务（pending/waiting_confirm） | 直接弹 toast「已有扫码配置正在进行」 | 弹**确认框**：「当前已有飞书机器人正在配置中，是否放弃并重新开始配置？」 |
| 用户选「重新开始配置」 | 无入口 | **先作废旧任务，再启动全新的扫码配置流程**；不再等待上一次配置 |
| 用户选「取消」 | 无入口 | 关闭弹窗，维持现状（不触碰旧任务） |
| 旧任务已 connected（扫码已完成） | 无提示、静默新扫码 | **前端弹换绑确认**：提示已配置机器人，确认更换将使旧配置失效且不可恢复（**纯前端提示**；后端不因 connected 拦截） |
| 旧任务 connected + 用户确认换绑 | — | 继续新扫码；成功后自动换绑（复用现有 `_apply_chat_credentials` 的 release/登记逻辑） |

> 原则：**「进行中但未完成」可主动放弃重开（409 弹窗）；「已完成配置」再做「换绑」必须是用户确认的显式动作（前端确认框），且后端不视为「进行中」而 409。**

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

### Step 1.4 换绑前确认（场景②，纯前端提示）

**目的**：用户**已成功配置**（connected）过机器人、再次点「扫码配置」想换绑时，必须显式提示「将替换现有绑定」，避免静默覆盖 + 旧绑定不可逆失效。

**范围（纯前端）**：后端语义**保持不变**——`start_onboard` 仍只以 `pending/waiting_confirm` 判断「进行中」，**不对 connected 做 409 拦截**（换绑是合法需求，扫码成功后 `_apply_chat_credentials` 本就自动 release/清理/登记）。

**实现**（前端 `startFeishuOnboard`，Chat Agent 配置页 & IM 渠道页共用入口）：
1. `startFeishuOnboard(botType, repoKey)` 入口，先 `API.get('/chat/config')` 取 `im.feishu.app_id`（或 chat 用 `_chatConfigData.im.feishu.app_id`）。
2. 若已存在 `app_id`（已配置）→ `app.showConfirm("当前已绑定飞书机器人（<app_id 前8位>）。更换后将使旧配置失效且不可恢复，确认继续扫码换绑？", onConfirm, {danger:true})`。
3. 确认 → 继续现有 start 流程（新二维码）；取消 → 不动作。
4. 无已配置（`app_id` 为空）→ 直接 start，无提示。

> 与 Step 1.2 的关系：Step 1.2 处理**进行中（409）**；Step 1.4 处理**已完成（connected）**。二者互斥独立——已 connected 时无 409，前端走 Step 1.4 换绑确认；进行中时 409 走 Step 1.2 重开确认。

---

## 三、各模块改动

| 模块 | 改动 | 状态 |
|------|------|------|
| `src/api/routes/feishu_onboard.py` | `start_onboard` 409 detail 增加 `task_id` 字段 | ✅ 已实施 |
| `src/api/routes/feishu_onboard.py` | `start_onboard` 并发检查增加 `STALE_TTL`（超时自动作废旧任务） | ✅ 已实施 |
| `src/api/static/js/pages/chat.js` | `startFeishuOnboard` 409 → `showConfirm` 确认重开；确认后 cancel 旧任务 + 重启流程 | ✅ 已实施 |
| `src/api/static/js/pages/chat.js` | **场景②：`startFeishuOnboard` 入口查 `/chat/config`，已配置 `im.feishu.app_id` → `showConfirm` 换绑确认（纯前端）** | ⬜ 待实施 |
| `src/api/static/js/app.js` | `showConfirm` 已存在（复用，无需改） | ✅ 现有能力复用 |
| `src/api/static/locales/{zh,en}/chat.json` | 新增 `chat.onboard.confirm_restart`（409 重开文案）；场景②换绑文案 key（待补） | ✅ 409 / ⬜ 换绑 |
| 测试 | UT：409 detail 含 task_id；cancel 后再 start 成功；stale 自动作废放行；connected 走换绑确认（UT-01~06） | ✅ 01-05 / ⬜ 06 |

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
| 换绑（场景②） | 已 connected 时不走 409、不改后端；**前端弹确认**，明确「旧配置失效且不可恢复」。确认后才换绑（复用 `_apply_chat_credentials` release/清理/登记） |
| 跨进程 | cancel 置 failed + DB 更新不依赖后台协程存活，进程重启后仍可作废旧任务 |
| 网络竞态 | 用户在确认弹窗期间他处再触发：cancel 后再 start 走 DB 最新状态，`COUNT` 原子性防止并发双建 |
| 文案 | ① 409 重开：「当前已有未完成的飞书机器人配置，是否放弃并重新开始？（旧配置不会保存）」；② 换绑：「当前已绑定机器人，更换后将使旧配置失效且不可恢复，确认继续扫码换绑？」 |

---

## 六、测试用例设计

| 编号 | 用例 | 前置 | 断言 |
|------|------|------|------|
| UT-01 | 409 附带旧 task_id | 用户已有 waiting_confirm 任务，再 start | 409 `detail.task_id` == 旧任务 id |
| UT-02 | 确认重开：cancel 旧 + start 新 | 前端确认回调 mock cancel 成功 | 旧任务 `failed`；新任务成功创建（唯一 pending 任务） |
| UT-03 | cancel 幂等 | 旧任务 connected / failed 时调 cancel | 返回「已结束」，不覆盖已保存配置 |
| UT-04 | stale 自动作废 | 旧 waiting_confirm 任务 created_at > STALE_TTL | start 自动置旧任务 failed 并放行新任务 |
| UT-05 | 已 connected：`start_onboard` 不 409 | 用户已有 connected 任务（chat_agent_session.im_app_id 非空），再 start | 返回 200（新二维码）——**后端不因 connected 拦截**（对应前端换绑确认，见 UT-06） |
| UT-06 | 换绑确认（前端）：已配置 → 弹换绑确认 | mock `/chat/config` 返回 `im.feishu.app_id` 非空；触发 startFeishuOnboard | `showConfirm` 被调、文案含「换绑/不可恢复」、danger；确认后继续 start；取消不动作 |
| E2E-01 | 集群/单机联调 | 模拟一条 waiting_confirm 残留 → 前端点扫码 | 弹确认框→确认→新二维码；两次 start 均有 QR |
| E2E-02 | 换绑联调（前端） | 已成功配置机器人 → 再点扫码 | 弹换绑确认→确认→新二维码；扫码成功后被替换 |
| E2E-01 | 集群/单机联调 | 模拟一条 waiting_confirm 残留 → 前端点扫码 | 弹确认框→确认→新二维码；两次 start 均有 QR |

> UT-05 语义需与产品确认：connected 任务存在时，用户再点扫码应提示「已配置成功」而非「重开」——避免破坏已完成绑定。若确需支持「换绑」，另走换绑流程（不在本次范围）。

---

## 七、参考

- `src/api/routes/feishu_onboard.py`：start / status / cancel / 并发限制（现 409 无 task_id）+ `_apply_chat_credentials` 换绑语义（release/清理/登记）
- `src/api/static/js/pages/chat.js`：`startFeishuOnboard` 409 catch + 场景②换绑确认入口
- `src/api/static/js/app.js`：`showConfirm`（复用现成确认弹窗）
- `src/api/static/js/api.js`：错误对象 `e.detail` / `e.status` 透传（409 detail 扩展无损）
- `src/api/static/js/pages/im_channel.js` / `src/api/routes/chat.py` `GET /chat/config`：`im.feishu.app_id`（场景②前端判断「已配置」的数据源）