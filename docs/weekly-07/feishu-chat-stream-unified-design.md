# 飞书 Chat 动态回复（飞书端动态更新卡片）

> 状态：**✅ Step 1~2 已实施 / ⛔ Step 3 已关闭**（2026-10-18 修订）
> - Step 1（task_id 写库）、Step 2（ChatTask 缓冲 / Web 对话框动态）**保留**。
> - **Step 3（飞书端「打字机」动态卡片）默认关闭**：占位卡与 `update_message` 节流增量不再生效，
>   env `FEISHU_CHAT_DYNAMIC_CARD=1` 可临时重新开启（见 §Step 3 修订）。
> - 关闭原因：① 过程行（`step_start/step_finish`）含 opencode session id 与 token 计数，被原样投到飞书；
>   ② 占位卡「⏳ 正在思考...」在终态回复后**未撤回**，留在会话里造成「还有后续回复」的错觉。
> - 同步收敛受理反馈：移除独立「⏳」气泡，仅保留「OK」表情作为唯一受理确认（正式回复只发终态卡片）。
> 代码 `src/im/handler_async.py`；测试 `tests/test_feishu_chat_stream.py` UT-F1~F9（F5/F6 改默认关闭口径）。
> 飞书 `update_message`（feishu_async.py:638）已实现可复用，本方案仅接入 chat 主流程 + 节流。

---

## 〇、现状两条链路（差异根因）

```
Web 发消息（有 stream）：
  chat_submit ──▶ user 消息写 DB（task_id + generate_status=1）          [chat.py:1460]
      │  返回 task_id
  chat.js switchConversation ──▶ 检测最后条 user 消息有 task_id（chat.js:496）
      │                          └▶ /task/{id}/status 若 running ──▶ streamTask() SSE
  chat_task_stream（chat.py:1686）── 内存有 ChatTask：重放缓冲 + 实时追加
                                   └ 内存无（跨机器/已结束）：轮询 chat_messages WHERE task_id （DB 兜底）
  ChatTaskManager.submit ──▶ 后台线程跑 agent_fn，push_event 逐条进 deque + subscribers
  agent_fn: 视频分析→stream_chat_agent→逐事件 yield（text/step_start/step_finish/...）→ done

飞书发消息→Web 对话框（无 stream）：
  handler_async.process_chat_async ──▶ _process_chat_inner_async
      direct：consume_stream_async 逐事件消费，但只取 done 的 final_text（handler_async.py:1780-1796）
      remote：enqueue_task + wait_task_terminal 阻塞等终态（handler_async.py:1763-1771）
      ├─ user 消息 add_message 无 task_id / generate_status（handler_async.py:1638-1640）★
      ├─ agent 回复 add_message 无 task_id（handler_async.py:1830）★
      └─ 最终 send_card_to_chat 一次性发飞书（handler_async.py:1824）★
  → Web 前端找不到带 task_id 的 user 消息 → 不发起 streamTask → 对话框只见一次性最终回复
```

**根因一句话**：Web 的动态渲染完全依赖「user 消息行有 `task_id` → SSE 订阅」这条链；
飞书 handler 的并行通道**没给消息行写 `task_id`、也没把执行事件投进可订阅的缓冲/`task_messages`**，
因此即使共享同一对话，飞书回复在 Web 端也不具备 stream。

---

## 一、目标语义

| 场景 | 现状 | 目标（本期 = Step 1~3） |
|------|------|------|
| 用户在飞书发消息（direct 本机直连） | 飞书 `⏳`+终态卡片；Web 对话框只见最终回复 | 飞书端**动态更新卡片**（打字机/步骤行），终态落卡片；Web 对话框**同款 stream 动态** |
| 用户在飞书发消息（queue_remote 远端 worker） | 飞书 `⏳`+终态卡片 | 飞书 `⏳`+终态卡片维持；Web 对话框可见飞书会话回复过程（DB 兜底，Step 1 task_id 关联） |
| 用户在 Web 发消息（P2P 已接入飞书） | Web 有 stream；飞书无推送更新 | 保持 Web stream；飞书端可选同步动态卡片（Step 3 覆盖） |
| 断线/跨机器（执行在 worker） | Web 走 DB 轮询兜底可拿到终态 | 保留 DB 兜底；补报/终态间隙统一覆盖 |

> Web 端点 `/api/chat/task/{task_id}/stream`（chat.py:1686）**已经是通用的**：
> 内存无任务时自动轮询 `chat_messages WHERE task_id=?`（chat.py:1703-1733），
> 等 agent 回复出现即 `done` event 返回。**前端 `switchConversation` 的探测逻辑（chat.js:496-510）
> 对任何带 task_id 的会话都生效，无需改前端。** 因此最小改造点不在 Web，而在飞书 handler。

---

## 二、方案（Step 1~3，顺序可并行）

### Step 1（飞书 handler：写入 task_id + generate_status）【✅ 已实施】

`src/im/handler_async.py` `_process_chat_inner_async`：

1. **入口生成 chat task_id 并写用户消息**（照抄 chat.py:1459-1460 的写法）：
   - `import uuid; chat_task_id = "chat_" + uuid.uuid4().hex[:12]`
   - `add_message(conv_id, "user", text..., task_id=chat_task_id, generate_status=1)`
   - 替换现 handler_async.py:1638-1640 无 task_id 的 `add_message`。
2. **agent 回复写库带同一 task_id + 终态 status**（handler_async.py:1825-1832）：
   - `add_message(conv_id, "agent", output, task_id=chat_task_id, generate_status=2 if exit_code==0 else 3)`
3. 不影响飞书现有 `send_card_to_chat` 终态推送（保持兼容）。

效果：Web `switchConversation` 探测到该会话最后条 user 消息有 `task_id` →
`/status` running 期间 `streamTask()` 走 **DB 轮询兜底**（chat.py:1703），
agent 回复写库（generate_status=2）后返回 `done` event → **Web 对话框即可看到该飞书会话的动态到终态全过程**（无需改前端）。

### Step 2（飞书 handler：direct 模式事件进缓冲，实现真流式）【✅ 已实施】

Step 1 只给 Web 端「DB 轮询」级动态（1s 粒度、text 一次到位）。要达到与 Web 等同的打字机/步骤滚动，
需把 direct 模式的**逐事件增量落进可订阅缓冲**：

1. **direct 分支事件进 ChatTask 缓冲**（已实施，实现方式与草案略有差异——handler 需同步等结果回飞书，
   故不用 `ChatTaskManager.submit` 后台线程，改为**外部驱动**新入口，见 `src/api/chat_task_manager.py`）：
   - `ChatTaskManager.register_idle(chat_task_id, conv_id, user_id, "")`：登记任务（不启动线程）
   - handler 的 `async for consume_stream_async(...)` 循环内逐事件 `ChatTaskManager.publish(chat_task_id, evt_type, data)`
     ——自动落 `task.events` deque + flush 订阅者（线程安全，与 submit 的 push_event 同构）
   - 结束 `ChatTaskManager.finish_external(chat_task_id, final_text, error)`——写终态 + `notify_done`
   - Web 的 `chat_task_stream`（chat.py:1741-1760）订阅该 task_id → 重放缓冲 + 实时追加 → **真打字机/步骤滚动**。
2. **事件写 `task_messages`（可选增强，本期未实施）**：
   - 草案提及逐事件 INSERT task_messages 供断线重放；实际交付未做——内存 deque（maxlen=500，
     chat_task_manager.py:33）+ `chat_task_stream` 的 DB 兜底（chat.py:1703）已覆盖常规断线/跨机器场景。
   - 后续如需「断线完整回放」再按需补（对齐 event_pipeline/agent.py 落库写法）。
3. **remote 分支（现有 enqueue_task）+ run_token/终态补充**【✅ 已实施（经 Step 1）】：
   - 保持 enqueue_task + wait_task_terminal；`add_message(agent, task_id=chat_task_id, generate_status=...)`
     由 Step 1 的统一写库完成（remote 分支最后同样走该路径）。
   - worker 侧 `task_messages` 已随执行实时落库（agent.py messages 上报）——
     Web 端若在该任务 still queued/运行时打开对话框，靠 `chat.py:1703` DB 轮询即可收到终态。

### Step 3（飞书端动态更新卡片—direct 模式）【⛔ 已关闭，2026-10-18】

> **修订（2026-10-18）**：飞书端「打字机」动态卡片**默认关闭**。占位卡 `send_card_to_user("⏳ 正在思考...")`
> 不再发送；`update_message` 节流增量段不再执行。direct 分支仅保留：受理「OK」表情 + 终态
> `send_card_to_chat`（+ Step 1/Step 2 的 task_id 写库与 ChatTask 缓冲，供 Web 对话框动态，飞书侧不可见）。
> 开关：env `FEISHU_CHAT_DYNAMIC_CARD=1` 临时恢复，1.~4. 原行为全部回归。

关闭动因：
1. **过程信息直投飞书**：`step_start/step_finish` 事件自带 opencode session id（`ses_…`）与 token 计数，
   被 `▸`/`✓` 前缀拼进 `_inline_buf` 原样刷新到飞书卡片，向用户暴露执行态细节。
2. **占位卡滞留**：`⏳ 正在思考...` 在终态 `send_card_to_chat` 后未更新/未撤回，留在会话造成「还有后续答复」误判。

原设计（仅当 `FEISHU_CHAT_DYNAMIC_CARD=1` 时生效）：
飞书渠道侧实现「打字机」动态更新（非阻塞）。**仅 direct（本机直连，credential_id=0）分支接入；
remote（queue_remote）保持现状**（原因：remote 事件的逐条增量在 worker 侧，飞书 handler 侧无事件流，本期不做回放桥接）。

已落地代码：`handler_async.py _process_chat_inner_async` 的 direct 分支（`consume_stream_async` 循环内）。

1. **优先卡片**：进入 direct 执行前 `await api.send_card_to_user(ctx, open_id, "⏳ 正在思考...")` 拿 `message_id`。
2. **流式更新**：direct 分支 `consume_stream_async` 循环内把 `text`/`step_start`/`step_finish`/`reasoning`
   累积为 `_inline_buf`，按节流调用 `api.update_message(ctx, _first_msg_id, 增量文本[:1500])`（feishu_async.py:638）。
3. **节流与上限**：常量 `_UPDATE_MIN_INTERVAL=0.8`（s）+ `_MAX_INLINE_LEN=1500`（chars）已落地（handler_async.py:63-64）；
   达上限置 `_inline_capped` 停止增量，终态由现有 `send_card_to_chat` 落最终卡片（保持消息完整可复制）。
4. **失败容错**：`update_message` 异常/False 仅降级（忽略该次增量），不影响事件收集与终态卡片（try/except 包裹）。

> 代码事实（已核对）：`FeishuAsync.update_message`（feishu_async.py:638-659）：PATCH `/im/v1/messages/{message_id}`，
> 卡片 JSON，返回 bool。关闭后该能力仍保留（供 `FEISHU_CHAT_DYNAMIC_CARD=1` 复用）。

---

## 三、各模块改动

| 模块 | 改动 | 状态 |
|------|------|------|
| `src/im/handler_async.py` | Step 1：`_process_chat_inner_async` 写 user/agent 消息带 `task_id`+`generate_status`（`chat_` 前缀 uuid + 1/2/3） | ✅ 已实施 |
| `src/im/handler_async.py` | Step 2：direct 分支每事件 `ChatTaskManager.publish`+`finish_external`（外部驱动 ChatTask） | ✅ 已实施 |
| `src/api/chat_task_manager.py` | 新增 `register_idle` / `publish` / `finish_external`（外部驱动任务缓冲，不启动线程） | ✅ 已实施 |
| `src/api/routes/chat.py` | 无需改（`chat_task_stream` 的 DB 兜底与内存流已通用） | ✅ 现有能力复用 |
| `src/api/static/js/pages/chat.js` | 无需改（`switchConversation` 探测已对带 task_id 会话生效） | ✅ 现有能力复用 |
| `src/im/handler_async.py` / `feishu_async.py` | Step 3：占位 `send_card_to_user` + `update_message` 节流增量（0.8s/1500）+ 终态 `send_card_to_chat`（`_MAX_INLINE_LEN`/`_UPDATE_MIN_INTERVAL` 常量） | ⛔ 已关闭（env `FEISHU_CHAT_DYNAMIC_CARD=1` 可临时开启） |
| `src/im/handler_async.py` | 受理反馈收敛：移除独立「⏳」气泡 `reply_text(msg_id,"⏳")`；保留 `add_reaction("OK")` 表情为唯一受理确认 | ✅ 已修订 |
| `tests/test_feishu_chat_stream.py` | UT-F1~F9：task_id 写入、ChatTask 缓冲、占位→更新→终态、失败降级、超长截断、无 message_id 跳过增量。Step 3 相关（F5/F6~F9）改跑「默认关闭」口径 + env=1 时回归原行为 | ✅ 已实施（默认关闭口径通过） |

---

## 四、验证设计

- **UT（已交付）**：`tests/test_feishu_chat_stream.py` 9 项通过（见 §六）；相关回归 100 passed。
- **E2E（待生产联调）**：真实飞书 P2P + Web 对话框验证：

| 编号 | 用例 | 断言 |
|------|------|------|
| E2E-F1 | 飞书发消息 → Web Chat 对话框打开该会话 | user 消息带 task_id；agent 回复写库后对话框出现**完整的动态→终态过程**（至少最终结果，best 是步骤滚动） |
| E2E-F2 | 飞书直接消息 → 飞书端（默认关闭） | 只收「OK」表情确认 + 终态卡片；无 `⏳` 占位、无过程行、无悬浮「正在思考」 | 目标态 |
| E2E-F2b | 飞书直接消息 → 飞书端（`FEISHU_CHAT_DYNAMIC_CARD=1`） | 恢复打字机：`⏳` 占位 → `update_message` 增量 → 终态卡（仅联调开关，不推荐常态化） | 可选 |
| E2E-F3 | 飞书发消息（queue_remote）→ Web 对话框 | worker 执行中若用户打开对话框，DB 轮询拿到终态；不重跑不误标 |
| E2E-F4 | Web 发消息（接入飞书会话） | Web 保持 stream；飞书端看到动态/终态 |
| E2E-F5 | 断线重连 | SSE 断开-重连重放缓冲（内存 deque；task_messages 落库未做，超 30min 缓冲清理后靠 DB 兜底终态） |

---

## 五、影响面 / 风险

| 项 | 说明 |
|----|------|
| Web 侧无改动 | 前端探测逻辑（chat.js:496）与 stream 端点（chat.py:1686）天然兼容带 task_id 的会话 |
| 飞书 handler 兼容 | `send_card_to_chat` 终态保持；只增加 user/agent 消息的 task_id 字段与新的事件中转 |
| ✋ ChatTaskManager 并发配额共享 | `register_idle` 不占用 `Semaphore(20)`（不启动后台线程），Web submit 仍独占并发额度——**低成本**；若未来飞书 direct 也走 submit 线程才共享配额（本期未采用） |
| task_id 生命周期 | `chat_` 前缀与 Web 一致；`_chat_task_cleanup` 30min 清理已完成内存任务，DB 兜底不受影响 |
| 增量消息上限 | 飞书 update_message 节流（0.8s）+上限（1500 chars）已落地，防 QPS 限流/超长 |
| remote 模式本质 | worker 执行时 Web 端能见到的粒度由 `task_messages` 落库决定（events.js 已用 `task-logs/{id}/stream`）；chat 分支 DB 兜底拿终态 |
| task_messages 落库未做 | Step 2.2 可选增强本期未实施——内存 deque（maxlen=500）+ DB 兜底已覆盖常规断线；超 30min 缓冲清理后仅剩终态（E2E-F5 标注） |
| 双写一致性 | 写 `chat_messages` 与 `task_messages` 同事务/同 task_id，避免 Web 端轮询到空（本期 task_messages 未写，故该项不生效，待 Step 2.2 落地时对齐） |

---

## 六、测试用例设计

> 风格对齐现有 `tests/test_chat_handler.py`：AsyncMock 驱动 `_process_chat_inner_async`、
> `patch("src.im.handler_async.xxx")` 隔离 DB/引擎；`tests/test_feishu_async.py` 已有
> `update_message` HTTP 层（200/非200/无token）覆盖，本表不再重复。

| 编号 | 用例 | 断言 | 覆盖 | 实现 |
|------|------|------|------|------|
| UT-F1 | 飞书 user 消息写 `task_id`+`generate_status=1` | `chat_messages` user 行 task_id 非空 `chat_` 前缀、gen=1 | Step 1 | ✅ |
| UT-F2 | agent 回复写同一 task_id + 终态 status | agent 行 task_id==user、gen=2（成功）/3（失败） | Step 1 | ✅ |
| UT-F3 | remote 模式 agent 回复同带 task_id | mock enqueue+wait；agent 行 task_id 非空、gen 按 exit_code | Step 1 | ✅ |
| UT-F4 | ChatTask 缓冲含 text/step + final_text | `publish` 后 `task.events` 含 text 增量、`final_text`=done、status=done | Step 2 | ✅ |
| UT-F5 | direct 不重复终态卡片（默认关闭） | `send_card_to_user` 0 次、`update_message` 0 次、`send_card_to_chat` 1 次（仅终态单卡，占位/增量不再发） | Step 2/3 | ✅ |
| UT-F6 | 受理确认 = OK 表情 | `add_reaction(msg_id,"OK")` 恰好 1 次；源不含独立「⏳」气泡 `reply_text(msg_id,"⏳")` | Step 3 收敛 | ✅ |
| UT-F7 | `FEISHU_CHAT_DYNAMIC_CARD=1` 恢复打字机 | 占位 `send_card_to_user` 1 次 + `update_message` 至少 1 次 + 终态卡含全文（开关回归） | Step 3 | ✅ |
| UT-F8 | `update_message` 失败降级（开关下） | 返回 False → 不中断、终态卡片照发 | Step 3 | ✅ |
| UT-F9 | 超长转终态（开关下） | 2000+ chars → 增量截断、终态卡片 ≤2000 且含内容 | Step 3 | ✅ |
| UT-F10 | 视频/图片前置步骤进动态 | image_description 注入 → 先「正在分析」再 text | Step 2/3 | ⬜ 未覆盖（逻辑已被 F4/F7 路径覆盖，属于组合场景，可后补） |

E2E（生产/联调，接 §四）：

| 编号 | 用例 | 前置 / 步骤 | 断言 |
|------|------|-------------|------|
| E2E-F2 | 飞书 direct 动态卡片 | 真实飞书 P2P 发消息（本机直连引擎） | 看到 `⏳` → 同一条卡片内容逐步增长（打字机）→ 终态完整卡片 |
| E2E-F1 | 飞书→Web 对话框 | 联调时打开 Web Chat 该会话 | 对话框展示动态→终态过程（至少最终结果，best 是步骤滚动）|

---

## 参考

- `feishu-web-chat-unified-design.md`：飞书/Web 共享 conversation 的既有机制（§3.2）
- `src/api/routes/chat.py` `chat_submit`/`chat_task_stream`：submit/stream 分离 + DB 兜底范本
- `src/api/chat_task_manager.py`：ChatTask 缓冲/订阅/线程安全实现
- `src/im/feishu_async.py` `update_message`：飞书消息更新能力（本方案已接入 chat 主流程 + 节流）