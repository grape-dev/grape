# 方案：飞书任务会话已进入时不再被「未绑定仓库」拦截（群绑定为空场景）

> 状态：**✅ 已实施**（2026-10-18，`src/im/handler_async.py` + `tests/test_feishu_resolve_repo_conversation.py`，UT 6 passed，相关回归 96 passed）｜ 目标分支：`weekly-07-lyz`
> 关联：feishu 消息分发 / 任务会话 repo 对齐 / 会话派生执行仓库
> 敏感信息：本文不含任何真实账号、IP、仓库名、口令；示例一律用 `<owner>/<repo>`、`oc_<chat_id>` 等占位符。

---

## 〇、问题现象

用户流程：
1. IM 渠道（Feishu）配置完成，但**群未绑定任何仓库**（`repo_im_group_binding` 无该 `im_chat_id` 记录）。
2. 一条任务通知「需求待澄清」经飞书送达，用户点击通知卡 → **已进入对应任务会话**（会话带 `repo_key` 且 `im_channel_id=<chat_id>`）。
3. 用户在会话内答复 → 仍收到 **「未绑定仓库 · 选择仓库」** 卡片，无法正常对话。

**不合理点**：用户已处在一个**有仓库归属的任务会话**中（会话级 repo 已存在），本该直接在该仓库上下文中与 Agent 交互；却被「群绑定为空」这一**群级前置条件**拦截。

---

## 一、现状链路与根因

### 1.1 入口拦截点

`src/im/handler_async.py` `process_chat_async`（约 L1464-1499）：

```python
repo_key = resolve_repo(text, chat_id, parsed.get("sender_id", ""), db, account)
if not repo_key:
    # 未绑定仓库 → 自动触发 /repo 选择卡片，直接 return（拦截）
    if await _send_repo_card(ctx, api, chat_id, msg_id, uid, account, 1, unbound_hint=True):
        return
    ...
    return
```

### 1.2 `resolve_repo` 为什么不看会话

`resolve_repo`（handler_async.py:287-300）只做三层解析，**完全不查当前会话**：

| # | 来源 | 说明 |
|---|------|------|
| ① | `detect_bind_intent(text)` | 文本「绑定到 X / 使用 X」→ 绑定意图 |
| ② | 正则匹配 `owner/repo` | 消息正文直接含仓库串 |
| ③ | `_current_bound_repo(chat_id, …)` | **只查 `repo_im_group_binding` 群绑定表** |

会话仓库存在 `chat_conversations.im_channel_id=<chat_id>` + `chat_conversations.repo_key`，但 `resolve_repo` 对该表无感知。

### 1.3 会话仓库能力已存在，但位置在其后

`_resolve_task_conversation(chat_id, user_id)`（handler_async.py:313）与 `_resolve_feishu_exec_repo(repo_key, chat_id, user_id)`（handler_async.py:324）**正是**「群已 join task 会话时以会话仓库为准」的实现，并在入口第 2 段（L1520-1527）被调用。但：

- 它位于 `if not repo_key: return` **拦截之后**；
- 它的 docstring 约定「仅当外层 `resolve_repo` 已给出某仓库且本群已 join task 会话时对齐」——即它**只负责纠偏**（群绑定 ≠ 会话仓库时改为会话仓库），**不负责救场**（群绑定为空时提供仓库）。

### 1.4 定性

> 入口把「群绑定仓库」（群级全局定位）误当作进入任何交互的**唯一前置**；而**「当前已进入的会话」本身就是一个足够的仓库上下文**。会话级 repo 信息已有现成查询（`get_conversation_by_im` / `_resolve_task_conversation`），只是没有被纳入「拦截判定前」的 fallback。群绑定为空 + 已入会话 → 本可正常对话，却被错误拦截。

---

## 二、目标语义

| 场景 | 现状 | 目标 |
|------|------|------|
| 群已绑定仓库 | 正常 | 不变 |
| 群未绑定，但**当前已加入有 repo 的会话**（含任务会话） | 弹「未绑定」拦截 | **放行，以会话仓库为执行 repo** |
| 群未绑定，且无任何已加入会话 | 弹「未绑定」 | 不变（仍需先选仓库/新建会话） |
| 群未绑定，会话存在但会话无 repo | 弹「未绑定」 | 不变（无 repo 无法定位上下文） |

> 原则：**拦截判定 = 群绑定 repo ∪ 当前会话 repo**。只要任一存在即放行；右侧优先级更高（会话是当前粒度最精确的上下文）。

---

## 三、方案（Step 1，入口 fallback + 会话归属校验）

### Step 1（核心）【✅ 已实施】：`resolve_repo` 增加「当前会话 repo」兜底

已落地实现：`resolve_repo` ④ 层——`_current_bound_repo` 返回空时，查询 `get_conversation_by_im(chat_id, user_id)`，命中且 `repo_key` 非空 → 返回该会话 repo。

- **`resolve_repo` 签名**：新增 `user_id: int = 0`（默认 0 兼容既有调用；>0 才启用会话 fallback）。
- **归属校验**：`get_conversation_by_im(im_channel_id, user_id)` 已在 SQL 层按 `user_id` 过滤（session_manager.py:622-623），**越权会话查不到 → 返回空**，天然满足方案「归属安全」要求。
- **conv_type 不限制**：任务/普通会话有 repo 均放行（「进入会话即具备仓库上下文」语义）；无 repo 返回空。
- **不改群绑定表**：fallback 只读会话，不写 `repo_im_group_binding`（保持「进入会话」与「群绑定」两态分离）。
- **入口**：`process_chat_async` 在 `resolve_repo` 调用前取 `uid`，一并传入；`if not repo_key` 拦截对「已入有 repo 会话」不再触发。

**改动位置**：
- `src/im/handler_async.py` `resolve_repo`：签名 + 第④层。
- `src/im/handler_async.py` `process_chat_async`：调用处传 `uid`（`user_id=_chat_uid`）。
- 测试：`tests/test_feishu_resolve_repo_conversation.py`（UT-01~06）。

### 边界说明

- **不进 `/repo` 卡片**：fallback 命中即在 `resolve_repo` 内返回，入口 `if not repo_key` 为 False → 走正常 `_process_chat_inner_async`，不再触发 `_send_repo_card`。
- **后续对齐逻辑照常**：即便 `resolve_repo` 返回会话 repo，L1520-1527 的 `_resolve_feishu_exec_repo` 仍会把执行仓库与最新会话 repo 对齐（幂等），行为不冲突。
- **群绑定为空但会话有 repo**：会话 repo 生效；若用户后续 `/repo` 绑群，群绑定接管（`_current_bound_repo` 优先）。

---

## 四、各模块改动

| 模块 | 改动 | 状态 |
|------|------|------|
| `src/im/handler_async.py` `resolve_repo` | 签名加 `user_id`；`_current_bound_repo` 为空时查当前会话 repo（`get_conversation_by_im` 已按 uid 归属过滤）作 fallback | ✅ 已实施 |
| `src/im/handler_async.py` `process_chat_async` | `resolve_repo(...)` 调用处传 `user_id=_chat_uid`（拦截前取 uid） | ✅ 已实施 |
| `tests/test_feishu_resolve_repo_conversation.py` | UT-01~06 | ✅ 已实施（6 passed） |

> 覆盖面说明：`resolve_repo` 是 chat 入口统一解析点，改它一处即覆盖「普通消息 / 会话内消息」；`/repo`、`/where` 等命令走 `process_command_async` 独立路径，不受影响（本次不改命令语义）。

---

## 五、风险 / 影响面

| 项 | 说明 |
|----|------|
| 行为变化 | 仅「群未绑定 + 已入有 repo 会话」从拦截 → 放行；其余路径不变 |
| 越权 | 会话归属校验 `conv.user_id == uid`，杜绝拿他人会话 repo 定位 |
| 绑定分离 | 不写入群绑定，进入会话 ≠ 绑定群；后续 `/repo` 仍可用 |
| 兼容 | `_resolve_feishu_exec_repo` 对齐逻辑保持幂等；已绑定群路径零变化 |
| 远端/本机 | fallback 结果同样进入既有执行链路（direct/queue_remote 一致） |

---

## 六、测试用例设计

| 编号 | 用例 | 前置 / 步骤 | 断言 |
|------|------|-------------|------|
| UT-01 | 群未绑定 + 已入任务会话 | `repo_im_group_binding` 空；`chat_conversations.im_channel_id=chat` 且 `repo_key=<owner>/<repo>`；消息不含绑定意图/仓库串 | `resolve_repo` 返回会话 repo；入口不再弹「未绑定」，正常进入执行 |
| UT-02 | 群未绑定 + 会话属于他人 | 会话 `user_id` ≠ 当前 uid | fallback 拒绝，仍弹「未绑定」 |
| UT-03 | 群未绑定 + 会话无 repo | `repo_key` 空 | 仍弹「未绑定」 |
| UT-04 | 群已绑定（回归） | 正常绑定 | 行为不变（群绑定 repo 生效） |
| UT-05 | 群未绑定 + 无会话 | 无任何会话 | 仍弹「未绑定」 |
| E2E-01 | 生产/联调 | IM 配好飞书；任务通知 → 点进入任务会话 → 会话内答复 | 直接得到 Agent 回复，不再出「未绑定仓库」卡片 |

---

## 七、参考

- `src/im/handler_async.py`：`resolve_repo`（L287-300）、`process_chat_async`（L1464-1499）、`_resolve_task_conversation`（L313）、`_resolve_feishu_exec_repo`（L324）
- `src/session_manager.py`：`get_conversation_by_im`（会话按 im_channel_id 查询，会话 repo 单一来源）
- 既有对齐语义：`_resolve_feishu_exec_repo` 的「群绑定 → 会话仓库」纠偏（handler_async.py:324-347），本方案补其「救场」缺口