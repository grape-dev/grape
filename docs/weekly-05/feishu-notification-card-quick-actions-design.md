# 方案：飞书通知卡片快捷按钮 —— 「进入任务会话」直达处理

> 状态：技术方案（2026-10-07）｜目标分支：weekly-05-lyz
> 动机：三条静态飞书通知卡（任务通知 / 需求待澄清 / 需求待审核）只有文本无按钮，用户「看到通知但没法一键进到任务会话去处理」。
> 目标：通知卡增加**单枚主按钮「💬 进入任务会话」＋一枚只读「查看 Issue ↗」链接**，点击即在飞书内直达该需求的任务会话，可直接回复继续处理；「查看 Issue ↗」打开 GitCode/GitHub 公开 Issue 页做只读参考。
> 参考：现有快捷按钮机制（`feishu_async._parse_card_action` value.cmd → card.action.trigger → 命令分发，`/repo /chat /task` 多步编号流）。

---

## 一、现状与动机

### 1.1 三条通知卡现状：无按钮

| 卡片 | 发送函数 | title | 触发点 |
|------|----------|-------|--------|
| 任务通知 | `_run_notify`（issues_center.py:590） | 任务通知 | `POST /api/tasks/notify` |
| 需求待澄清 | `_run_awaiting_notify`（issues_center.py:626） | 需求待澄清 | `POST /api/issues/{id}/awaiting-input` |
| 需求待审核 | `_run_pending_review_notify`（issues_center.py:657） | 需求待审核 | pending_review 阶段 |

三条均走 `send_card_to_user`（feishu_async.py:537）：`{"elements": to_lark_elements(text,2000), "header":{...}}`，**无 `actions`**。发送对象 = assignee **本人 chat bot** 的 P2P 私聊（`_resolve_user_chat_bot`，issues_center.py:690）。

### 1.2 现有可复用按钮机制

- 卡片按钮：`{"tag":"button","text":{...},"value":{"cmd":"/task"}}` → 点击 → 飞书推 `card.action.trigger` → `_parse_card_action`（feishu_async.py:872）把 `cmd` 当用户命令文本 → WS / HTTP 兜底（feishu_card.py，`/api/feishu/card-action/{app_id}`，两通道 `_seen_msg_ids` 去重）→ `_on_message`（main.py:82）→ `process_chat_async` → handler_async 命令分发。
- 进入任务会话已存在路径：`/task` → 编号卡 → `/join N` → `_handle_selection action=="task"`（handler_async.py:606）解析 `conversation` → `join_feishu_chat`（session_manager.py:634，校验 `chat_conversations.user_id==点击者`）。
- **局限**：`_parse_card_action` 只透传 `value.cmd`，**丢弃其它 value 字段**；`/task` 需先弹列表再选号，**两步非直达**。

### 1.3 结论

「仓库」「Issue」「待处理内容」都汇聚于任务会话（会话绑定 repo_key+issue_number，prompt 带 Repository/Issue/描述/摘要，澄清/审核问题已 `add_message` 写入该会话；用户后续回复经 webhook 路由回 vod/issue 继续处理）。
→ **主按钮只做一枚「进入任务会话」即可闭环**；仓库不再给处理按钮（会话内可达），**同时配一枚只读「查看 Issue ↗」URL 链接**（需求在会话内可达，此链接用于直接看 Issue 网页原文/评论，只读参考，非处理路径）。

---

## 二、方案设计

### 2.1 按钮结构（通知卡新追加一个 `action` 元素）

```json
{"tag":"action","actions":[
  {"tag":"button",
   "text":{"tag":"plain_text","content":"💬 进入任务会话"},
   "value":{"cmd":"/task-open","repo_key":"gitcode:<owner>/<repo>",
            "issue_number":"#7","conv_id":"<conversation_id>"}},
  {"tag":"button",
   "text":{"tag":"plain_text","content":"查看 Issue ↗"},
   "url":"https://gitcode.com/<owner>/<repo>/issues/7"}
]}
```

- 主按钮走 `cmd` 机制；携带目标上下文（`repo_key / issue_number / conv_id`）实现**一键直达**。
- 「查看 Issue ↗」为 `url` 按钮（飞书原生打开浏览器），跳 GitCode/GitHub **公开 Issue 页**（无需登录、只读参考），**不是**处理路径。
- 两枚按钮同置一个 `action` 元素（主按钮在左、查看链接在右）。

### 2.2 发送侧：`send_card_*` 增加可选 `actions`

- `feishu_async.py:537 send_card_to_user(ctx, open_id, text, title="DevOps Bot", actions=None)`：`actions` 非空时在 `elements` 末尾追加 `{"tag":"action","actions":...}`（默认 None → 行为与现状完全一致，兼容全部存量调用）。
- `feishu_async.py:427 send_card_to_chat` 同理（预留，当前三条 notify 用 P2P user，不发群）。
- 三条 `_run_*_notify`（issues_center.py:590/626/657）构造同一份 `_build_notify_actions(repo_key, issue_number, conv_id)` helper 传入，返回两枚按钮：
  - 「💬 进入任务会话」：`value={"cmd":"/task-open","repo_key":...,"issue_number":...,"conv_id":...}`；
  - 「查看 Issue ↗」：`url=https://<vcs>/<owner>/<repo>/issues/<n>`（`split_owner_repo(from_repo_key(repo_key))` → `(owner, repo)`；`repo_key.startswith("github:")` → GitHub URL，否则 GitCode URL）；
  - `conv_id` 为空时按钮 `value.conv_id` 留空，由回调侧按 `(repo_key, issue_number)` 兜底（2.4）。

### 2.3 回调侧数据透传：`_card_value`

`feishu_async.py _parse_card_action`（:872）解析 `value` 后，除 `text=cmd` 外，把**完整 `value` 字典**挂到返回消息上：

```python
msg = {..., "text": cmd, "message_type": "text"}
msg["_card_value"] = value if isinstance(value, dict) else {}
```

WS 与 HTTP 两通道都经此函数（主.py:85-86 / feishu_card.py:49），`_card_value` 随 message dict 一路到 `process_chat_async` → handler_async；**无需改路由**。

### 2.4 命令处理：新增 `/task-open`

`handler_async.py` 命令分发（`:875 startswith("/")` 分支、`/bind`/`/join` 同区，`:441-475`）新增：

```
cmd == "/task-open":
  value = message.get("_card_value")              # 未命中（纯文本输入 /task-open）→ 回退现有 /task 列表流
  if not value.repo_key or not value.issue_number → 回退 /task 列表流（提示「请先使用 /task 选择」）
  uid = get_user_id_by_app_id(ctx.app_id)          # P2P 本人 bot = assignee
  若 value.conv_id 非空 → join_feishu_chat(chat_id, conv_id, uid)
  否则按 (uid, repo_key, issue_number) 查/建 task conversation（复用 `_handle_selection action=="task"` 的 fallback，handler_async.py:613-624）→ join
  join 成功 → 清空先前 pending 选择 + 发「✅ 已进入任务会话 Issue #N · 标题」卡（复用 :628-631 卡片）
  join 失败（conv 不属于该 uid，多为群卡片点击者≠属主）→ 发「❌ 无权/失败」卡（复用 :633-635）
```

- 群广播卡（若未来 `send_card_to_chat` 带 actions）点击者可能非属主：`join_feishu_chat` 校验 `conv.user_id==uid` 天然拦下 → 优雅失败卡，不泄内容。

### 2.5 兼容性 / 回退

- 旧通知卡（已发出的无 actions 卡）不受影响。
- 用户手动输入 `/task-open`（无 `_card_value`）→ 回退现有 `/task` 编号流，体验不变。
- 机器人快捷菜单、`/task`、`/chat`、`/repo` 全量行为不变。

---

## 三、各模块改动

| 模块 | 改动 |
|------|------|
| `src/im/feishu_async.py` | `send_card_to_user` / `send_card_to_chat` 加 `actions=None` 参数；`_parse_card_action` 增加 `_card_value` 透传 |
| `src/api/issues_center.py` | 新增 `_build_notify_actions(repo_key, issue_number, conv_id)` + 三条 `_run_*_notify` 传入 actions |
| `src/im/handler_async.py` | 新增 `/task-open` 命令分支（join_feishu_chat + fallback 查建会话 + 成功/失败卡） |
| `tests/` | 新增用例（见 §五） |
| `help/` | （可选）命令帮助补充 `/task-open`（内部命令，可不对外） |

---

## 四、影响面 / 安全边界

| 项 | 说明 |
|----|------|
| 行为兼容 | `send_card_*` actions 默认 None，存量调用零改动；旧通知卡按钮增补仅对新发卡生效 |
| 安全 | `join_feishu_chat`（session_manager.py:646）强制 `chat_conversations.user_id == uid`，越权点击被拒 |
| 静态原则 | 通知仍不触发 ChatAgent / 不跑 LLM；点击仅做会话指针切换 + 确认卡（与 `/join N` 同语义） |
| URL 按钮 | 仅指向 GitCode/GitHub 公开 Issue 页，无私有 token 泄露 |
| `_card_value` | 只随按钮事件回传，不入库不落日志细节（日志沿用 cmd 截断） |

---

## 五、验证

| 用例 | 断言 |
|------|------|
| UT-01 `send_card_to_user(ctx, open_id, text, actions=[...])` 出参 JSON 末尾含对应 action 元素；不传 actions → 无 action | 发送侧 |
| UT-02 三条 `_run_*_notify` 构造的 actions 含两枚按钮：`cmd=/task-open`+repo_key/issue_number/conv_id（conv_id 空时留空）与 `url=` 指向对应 VCS 公开 Issue 页 | helper |
| UT-03 `_parse_card_action` 返回消息含 `_card_value`（cmd 仍在 text） | 透传 |
| UT-04 `/task-open` 带 `_card_value` → `join_feishu_chat` 被调且走成功卡（mock handler） | 主路径 |
| UT-05 conv 属主非点击者 → join 失败 → 失败卡（无泄露） | 越权 |
| UT-06 `/task-open` 无 `_card_value`（纯文本输入）→ 回退提示 /task | 回退 |
| UT-07 conv_id 空 → 按 (uid, repo_key, issue_number) 查/建会话后 join | fallback |
| 回归 | `/task /chat /repo` 编号流、机器人快捷菜单、`/help` 不变 |

---

## 六、落档修订说明

- 主按钮一枚「进入任务会话」＋一枚只读「查看 Issue ↗」链接（**均随通知卡发出**），不拆三枚：任务会话已聚合仓库/Issue/待办上下文，单主按钮即可闭环处理，查看链接只读参考处理原文。
- 按钮机制完全复用现有 `value.cmd → card.action.trigger → 命令分发` 链路，仅增 `_card_value` 透传与一个新命令分支，无新通道/新路由。