# 飞书仓库绑定/导航命令优化方案（卡片化 + 编号选择）

> 状态：设计方案（v0.1，待评审）｜ 分支：new-version2
> 痛点：仓库多了之后，绑定新仓库需手动复制粘贴 `owner/repo`，体验差；命令回复均为纯文本。

## 1. 现状梳理

### 1.1 当前绑定流程
| 入口 | 机制 |
|---|---|
| `/bind owner/repo` | `process_command_async` 直接 `INSERT OR REPLACE INTO repo_im_group_binding`（handler_async.py:113-127） |
| 自然语言绑定 | `detect_bind_intent` 识别"绑定到 owner/repo"→ `resolve_repo` 返回 repo（handler_async.py:46-84） |
| `/unbind` `/status` `/refresh` | 解绑 / 查当前绑定 / 同步用户映射 |

**痛点**：
1. `/bind` 必须**手打完整 `owner/repo`**，仓库多时需复制粘贴、易打错；
2. 无"先看有哪些仓库再选"的交互；
3. 命令回复全是纯文本（`reply_text`），不可点、不直观。

### 1.2 现有会话切换（上轮已实现，本次将被 `/chat`/`/task` 取代并删除）
- `/对话` `/convs` `/list`：列出该用户参与仓库下的对话（纯文本列表，handler_async.py:147）。
- `/加入 <conversation_id>`：原子切换 im_channel_id 指针（handler_async.py:176，复用 `join_feishu_chat`）。
- 同样为纯文本、需复制 conversation_id（虽然支持短前缀）。

### 1.3 数据源
| 数据 | 来源 |
|---|---|
| 用户仓库列表（**已托管**） | `get_managed_repos(user_id)`（`managed_repos` JOIN `user_repos`，即该用户名下**已托管**仓库；未托管不显示，避免绑定后事件/聊天全错） |
| 用户会话列表 | `get_user_conversations(user_id, repo_key)`（chat_conversations，含 conv_type/issue_number/im_channel_id） |
| 用户任务列表 | `tasks` 表（`user_id` + `repo_key`，含 issue_number/title/status/conversation_id） |

## 2. 目标

1. **卡片化命令回复**：所有命令用 `reply_card` 回复（lark_md 子集，编号有序列表）。
2. **编号选择交互**：命令列候选，用户回复编号即执行（`/repo` 选仓库、`/chat` 选会话、`/task` 选任务）。
3. 保留并兼容既有 `/bind /unbind /status /refresh /autodev /notify /clean`。

## 3. 命令设计

| 命令 | 行为 | 候选 → 编号动作 |
|---|---|---|
| `/repo` | 卡片列出**该用户名下已托管仓库**（编号 + full_name + 角色[owner/member]）；**未托管不显示**；**当前已绑定仓库标注「绑定中」** | 回复编号 → 绑定该仓库（写 repo_im_group_binding，卡片确认） |
| `/chat` | 卡片列出**当前绑定仓库下**的会话（编号 + 类型[一般/任务] + 标题 + 时间） | 回复编号 → `join_feishu_chat` 加入该会话（卡片确认） |
| `/task` | 卡片列出**当前绑定仓库下**的任务（编号 + Issue# + 标题 + 状态） | 回复编号 → 定位/加入该任务的会话（卡片确认） |
| `/new` | **当前绑定仓库下新建会话并立即加入** | —（卡片确认：会话标题 + 已加入） |
| `/where` | **当前位置概览**：绑定仓库 + 当前加入的会话（conv_type/Issue#）+ 引擎 | —（卡片） |
| `/cancel` | **取消当前待选状态**（清掉 pending） | —（卡片确认） |
| `/clean` | 清除当前会话历史与缓存（沿用现有逻辑） | 卡片确认结果 |
| `/help` | 卡片列出全部命令及用法 | — |

> `/chat` 取代旧 `/对话 /convs /list`；`/repo` 取代旧 `/bind`；`/where` 承担旧 `/status` 的定位职能。旧 `/bind /unbind /status /refresh /autodev /notify` 及 `/对话 /加入 /leave` **一律删除**，不再兼容保留。
>
> **绑定即重置会话位置**：`/repo` 编号绑定与自然语言"绑定到 X"在写入 `repo_im_group_binding` 的同时，**清空当前 chat 旧会话的 `im_channel_id` 指针**，下次消息在新仓库下新建会话。修复"群绑定仓库 ≠ 会话所属仓库"发散 bug（2026-08-16，见 §5.8）。
>
> **`/repo` 延迟建会话（不立即建）**：绑定只清指针、**不创建新会话**；下一条消息时 `get_conversation_by_im` 为空 → `create_conversation(新repo)` 自动新建并绑定（等价"延迟的 `/new`"）。保持延迟的理由：① 换绑不产生空会话、Web 会话列表不堆积空"新对话"；② 与"未绑定仓库首条消息自动建会话"既有语义一致；③ `/new` 保留为**显式**"立即建+加入"，用户想马上开新会话就发 `/new`，想恢复旧会话就 `/chat` 选择。
>
> **关于 `/leave`（不采用）**：`/new` 的 `join_feishu_chat` "清旧设新"会原子清空旧会话的 im_channel_id，等于"离开旧会话 + 进入新会话"一步完成；若只要"离开不加入"，下一次消息也会自动新建 general 会话。故 `/leave` 与 `/new` 功能几乎等同、价值极低，不单设。

### 3.1 翻页
- 候选超 **20 条**时卡片分页：命令可带页码，如 `/repo 2`；或在卡片末尾提示 `回复 <数字> 选择；输入 /repo N 翻页`。
- 每页 **≤20 条**，编号在该页内 `1..20`（跨页不续号，避免歧义）。

### 3.2 数字选择状态机
- 新增内存态 `_pending_selections: dict[chat_id → {action, repo_key, items, expires}]`（TTL 120s）。
- 命令发出卡片时写入 pending（`/repo /chat /task`）。
- 用户回复**纯数字**：
  - **`0`**：有 pending → 视作取消（等效 `/cancel`），清除 pending + 卡片确认。
  - **`1..N`**：有未过期 pending → 查表执行（`/repo`: bind；`/chat`: join；`/task`: join task 会话）→ 卡片确认 → 清除 pending。
  - 无 pending 或已过期 → 数字当作普通 chat 消息处理（不吞消息）。
- 同 chat 同一时刻仅一个 pending；新命令覆盖旧 pending。

### 3.3 卡片结构（lark_md 子集）
```
📁 已托管仓库列表（共 N 个，第 1 页）
1. Jason7602/hello-calculator2（owner）
2. Jason7602/gitcode-pr-assistant（member）【绑定中】
3. ...
回复数字选择仓库；/repo 2 翻页
```
- 表头加粗（`**`），候选用有序编号行（纯文本 `1. ...` 或 lark_md 有序列表）。
- **当前已绑定仓库追加「绑定中」标记**（等价现有 `/对话` 的 `👈当前` pending 语义，表示该仓库当前指向本飞书聊天）。
- **只列已托管仓库**（`get_managed_repos`），未托管仓库不进入候选，避免绑定后无 webhook/agent 而全错。
- 内容 ≤2000 字符（复用 `to_lark_elements` 的卡片能力）。

## 4. 关键变更点

| 模块 | 变更 |
|---|---|
| `src/im/handler_async.py` | ① `_pending_selections` 状态机；② 主流程数字输入先查 pending（含 `0` 取消）；③ `/repo` `/chat` `/task` `/new` `/where` `/cancel` 命令；④ `/help` 卡片化；⑤ 命令结果统一走 `reply_card` |
| `src/im/lark_md.py` | 新增候选列表 → 卡片文本/元素的帮助函数（编号行 + 分页 + 绑定中标记） |
| `src/session_manager.py` | （可选）新增 `list_user_tasks(user_id, repo_key)`（按 tasks 表 + conversation 反查） |
| 无 DB 变更 | 绑定表/会话表/任务表均已存在 |

### 4.1 数字分发接入点
- `process_chat_async`（handler_async.py:280 附近）：`text.strip().isdigit()` 时先查 `_pending_selections[chat_id]`，命中则执行选择并 `return`，否则走正常 chat。
- 命令分支在 `process_command_async` 中，卡片回复改为 `await api.reply_card(ctx, msg_id, card_text, title="DevOps Bot")`。

## 5. 边界与风险

1. **数字吞消息**：仅"纯数字 + pending 未过期"才拦截；TTL 120s 内用户若想发普通数字消息，可先发任意非数字或等待过期。可接受。
2. **pending 内存态**：重启丢失（用户重发命令即可）；单进程内存，无需持久化。
3. **多 bot/多 chat 隔离**：pending 以 `chat_id`（P2P 即 open_id；群即 oc_id）为键，天然按聊天隔离。
4. **仓库列表=已托管**：`/repo` 用 `get_managed_repos(uid)`（managed_repos ∩ 用户可见），未托管不显示；`user_repos` 为缓存表可能滞后，`/repo` 可提示"如列表不新请刷新"。
5. **任务无会话**：任务未建 conversation 时，`/task` 选择后按 `issue_number` 查找/创建 `Issue #N` 会话再 join（复用 tasks.py 的会话创建逻辑）。
6. **命令收敛**：`/repo` 取代 `/bind`、`/chat` 取代 `/对话 /convs /list`、`/where` 承担 `/status` 定位职能；旧 `/bind /unbind /status /refresh /autodev /notify /加入 /leave` **全部删除**（设计只保留 `/repo /chat /task /new /where /cancel /clean /help`）。`/repo` 的「绑定中」标记沿用 `repo_im_group_binding`/`im_channel_id` 判定，无新状态。
7. **`/new` 会话标题**：新建会话默认无标题（与 Web 一致），卡片确认时提示"已加入新对话"；首次消息触发 `_auto_title`。
8. **绑定/会话发散（已修复）**：`repo_im_group_binding`（群绑仓库）与 `chat_conversations.im_channel_id`（加入的会话）是两个独立状态，改绑不自动清指针会导致消息落错仓库会话（2026-08-16 事故：群绑 hello-calculator3 但会话 f96c72a7 属 gitcode-pr-assistant）。修复：**`/repo` 绑定与自然语言绑定时清空当前 chat 旧会话指针**；`/where` 对"群绑定仓库 ≠ 会话所属仓库"给出 ⚠️ 提示。

## 6. 测试用例设计

| # | 用例 | 前置 | 断言 |
|---|---|---|---|
| C01 | `/repo` 生成候选卡片 | 用户有 ≥2 仓库 | 卡片含编号+full_name+角色；当前绑定仓库含「绑定中」；pending 已写入 |
| C02 | 回复编号绑定仓库 | 有 pending | 写 repo_im_group_binding；卡片确认；pending 清除 |
| C03 | 无 pending 时纯数字走正常 chat | 无 pending | 数字不被吞，进入 chat 流程 |
| C04 | pending 过期后数字不拦截 | TTL 过期 | 走正常 chat |
| C05 | `/chat` 列出当前仓库会话 | 已绑定仓库 + 有会话 | 编号+类型+标题；回复编号 → join_feishu_chat 生效 |
| C06 | `/task` 列出任务并进入任务会话 | 已绑定 + 有任务 | 编号+Issue#+状态；回复编号 → 定位/创建并 join 任务会话 |
| C07 | 翻页 | >10 候选 | `/repo 2` 返回第 2 页；页内编号 1..10 |
| C08 | `/help` 卡片列出命令 | — | 卡片含全部命令说明 |
| C09 | `/clean` 卡片确认 | — | 清除会话缓存 + 卡片确认 |
| C10 | 同 chat 新命令覆盖旧 pending | 有旧 pending | 发新命令后旧 pending 被替换 |
| C11 | `/new` 新建会话并加入 | 已绑定仓库 | 新建 chat_conversations 行 + im_channel_id 指向新会话（旧会话指针被清空） |
| C12 | `/where` 显示当前位置 | 已绑定 + 已加入会话 | 卡片含仓库 + 会话标题/Issue#；未加入时提示"未加入会话" |
| C13 | `/cancel` 清除 pending | 有 pending | pending 清除 + 卡片确认 |
| C14 | 回复 `0` 取消选择 | 有 pending | 等效 `/cancel`：pending 清除，不执行任何绑定/加入 |
| C15 | 重绑清旧会话指针 | 已绑 repo-a + 已 join 其会话 | `/repo` 选 repo-b → binding 更新 + 旧会话 im_channel_id 清空 + get_conversation_by_im 返回 None |
| C16 | 自然语言绑定清旧指针 | 已 join 旧会话 | "绑定到 X" → 旧会话指针清空 |
| C17 | `/where` 仓库不一致提示 | 群绑 repo-a + 会话属 repo-b | 卡片含 ⚠️ 不一致提示 |
| C18 | `/where` 一致不提示 | 群绑与会话同仓库 | 无 ⚠️ 提示 |
| C19 | `/repo` 只显示已托管仓库 | 用户有托管+未托管各若干 | 候选仅含托管仓库，未托管不出现；未托管无法绑定 |

## 7. 实施步骤（每步独立可测）

1. **pending 状态机 + 数字分发**：`_pending_selections`、TTL、纯数字拦截/放行（含 `0` 取消）。
2. **`/repo` 卡片 + 编号绑定**：候选来自 `get_managed_repos(uid)`（仅已托管，含「绑定中」）、绑定、卡片确认。
3. **`/chat` 卡片 + 编号加入**：复用 `join_feishu_chat`。
4. **`/task` 卡片 + 编号进入任务会话**：任务列表 + 会话定位/创建。
5. **`/new` `/where` `/cancel` + `/help` 卡片化**。
6. 测试（C01-C14）+ 回归（handler/chat/lark_md）→ 部署。

## 8. 结论

方案将"复制粘贴绑仓库"升级为"卡片 + 编号选择"的沉浸式导航：`/repo → 编号绑仓库`、`/chat → 编号加会话`、`/task → 编号进任务`，辅以 `/new`（新建并加入）、`/where`（位置概览）、`/cancel`（含回复 `0` 取消待选）、`/clean`、`/help`，全部命令卡片化回复。改动集中在 `handler_async.py` + `lark_md.py`，无 DB 变更，可逐步实施。
