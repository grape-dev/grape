# 任务分配「去对话」消息角色修正设计（Task Conversation Role Fix）

> 背景：任务中心「去对话」会话中，任务分配通知被预写入为 `role="user"`，
> 与事实（grape_bot 下发任务）相反，导致 UI 显示在用户侧、且 agent 对话语境错乱。
> 本文分析现状并给出修正方案。**仅设计，未改代码。**

## 1. 现状流程

### 1.1 任务分配链路

Issue Agent 确定责任人后调 `POST /api/tasks/notify`（`src/api/routes/tasks.py:215`）：

```
Issue Agent ──/api/tasks/notify──▶ 建任务 + 建/复用 conv(task) + 预写入消息
                                        │
                         ┌──────────────┴──────────────┐
                         ▼                              ▼
                  后台 _run_notify                 飞书通知
                  （chat-agent 执行 + 追加消息）
                                         │
Task Center ──[去对话]──▶ app.nav('chat', convId)
                                         │
              loadConversations(不过滤repo) → 选中会话 → 加载消息
                                         │
用户发消息 ──▶ build_task_chat_prompt(场景:task) ──▶ chat-agent 处理
```

### 1.2 关键代码（现状）

`tasks.py:264-269`（notify 内，预写入）：
```python
ctx_msg = _build_task_context_message(title, body.task_description, body.repo_key, body.issue_number)
add_message(conv_id, "user", ctx_msg)          # ← role=user
```

`tasks.py:125-135`（上下文消息内容）：
```python
def _build_task_context_message(title, task_description, repo_key, issue_number):
    lines = [f"📋 任务分配 · Issue #{issue_number}"]
    lines.append(f"仓库: {repo_key}")
    if title:
        lines.append(f"Issue 标题: {title}")
    lines.append("")
    lines.append(f"任务描述: {task_description}")
    lines.append("")
    lines.append("请在下方与我沟通处理进度，或提出疑问。")
    return "\n".join(lines)
```

`tasks.py:177-179`（`_run_notify` 后台执行后追加）：
```python
add_message(conv_id, "user", task_description)   # ← 再写一条 user 任务描述（重复）
if final_text:
    add_message(conv_id, "agent", final_text)
```

## 2. 问题分析

### 2.1 预写入 role 与事实相反

- 任务由 **grape_bot 分配**，预写入的"📋 任务分配 · Issue #N…"是 **bot 主动下发的通知**，
  不是用户发言。
- 当前 `role="user"` → 前端 `chat.js:160` 按 `m.role === 'user' ? 'l' : 'r'` 渲染：
  - `user` → **左气泡（用户侧）**
  - 非 user → **右气泡（agent/系统侧）**
- 结果：任务通知显示在**用户自己的左侧气泡**，用户会困惑"我什么时候发了这条"。

### 2.2 `_run_notify` 重复写任务描述

- 预写入已含完整任务上下文（含 task_description）。
- `_run_notify:177` 又 `add_message(conv_id, "user", task_description)` → **任务描述出现两次**，
  且第二条也是 `user` 角色，进一步加重"用户自己说了任务"的误导。

### 2.3 对话语境影响

`build_task_chat_prompt`（`prompt_builder.py:475`）会把历史消息注入 agent：
- 当前 role 颠倒 → agent 的首个回复基于"用户已发言"的语境，
  而非"bot 已通知、等待用户确认"的正确语境，影响首轮交互质量。

## 3. 修正方案

### 3.1 预写入 role：`user` → `agent`

`tasks.py:267`：
```python
add_message(conv_id, "agent", ctx_msg)
```

- 语义：bot（Chat Agent）下发的任务通知，UI 显示在**右侧 agent 气泡**。
- 内容可保留（"📋 任务分配 … 请在下方与我沟通处理进度"），语气本就是 bot 通知。
- 可选：将结尾改为更贴合下发语义，如"请确认是否开始处理，或直接提出疑问。"（非必须）。

### 3.2 删除 `_run_notify` 重复的 user 消息

`tasks.py:177`：
```python
# 删除（预写入已含完整任务上下文，避免任务描述出现两次）
# add_message(conv_id, "user", task_description)
if final_text:
    add_message(conv_id, "agent", final_text)
```

### 3.3 修正后的会话时序（期望）

```
[agent] 📋 任务分配 · Issue #N
        仓库: org/test
        Issue 标题: ...
        任务描述: ...
        请确认是否开始处理，或直接提出疑问。
[user]  好的，我开始处理            ← 用户答复确认
[agent]  收到，请按 … 处理          ← chat-agent 继续
```

## 3.4 优化后的整体链路

修正后，从任务分配到「去对话」的端到端流程：

```
① 任务分配（Issue Agent 侧，服务端）
   Issue Agent 确定责任人 → POST /api/tasks/notify
     ├─ 校验 assignee 是"人" + 解析 user_id + 换人关旧任务
     ├─ 建任务（tasks: active）
     ├─ 建/复用 Web conversation（conv_type=task, issue_number=N）
     ├─ 预写入 agent 消息：add_message(conv, "agent", 任务上下文)   ← 修正点①
     └─ 后台 _run_notify：chat-agent 执行
            └─ 追加 agent 回复（不再重复写 user 任务描述）        ← 修正点②
        + 飞书通知

② 用户在 Task Center 点「去对话」（前端）
   tasks.js openTaskConversation(conv_id) → app.nav('chat', conv_id)
     └─ chat.js renderChat(openConvId): 记录 _openConvId
           └─ loadConversations(): _openConvId 存在时不过滤 repo
                 → 跨仓库定位该任务会话 → 选中并加载消息

③ 会话呈现（修正后）
   [agent] 📋 任务分配 · Issue #N ...        ← 右侧气泡，bot 下发
   [user]  用户的答复确认                      ← 左侧气泡，用户发言
   [agent]  chat-agent 基于 task prompt 回复    ← 右侧气泡

④ 持续对话
   用户每条消息 → sendChatStream → 后端用 build_task_chat_prompt(场景:task)
     ├─ 注入 Issue 上下文（仓库/编号/标题/摘要）+ 对话历史
     └─ chat-agent 处理 → 追加 agent 消息 + （可选）回写 Issue/任务状态
```

关键差异（优化前后）：
| 环节 | 优化前 | 优化后 |
|---|---|---|
| 预写入角色 | `user`（显示在用户侧） | `agent`（bot 下发，右侧气泡） |
| 任务描述出现次数 | 2 次（预写入 + `_run_notify`） | 1 次（仅预写入） |
| agent 首轮语境 | 误以为"用户已发言" | 正确等待用户确认 |

## 4. 涉及改动

| 文件 | 改动 |
|---|---|
| `src/api/routes/tasks.py` | `:267` role 改 `agent`；`:177` 删除重复 user 消息 |
| 前端 | **无需改**（`chat.js` 已按 role 渲染，role 修正后自动正确显示） |
| 测试（可选增强） | `tests/test_tasks.py` 补断言：notify 后 `chat_messages` 首条 `role=="agent"` 且内容含"任务分配" |

## 5. Onboarding 任务通知（同样受此修正覆盖）

### 5.1 触发方式

Onboarding 的任务通知**不由后端代码触发**，而是由 `repo-onboarding` skill 通过
curl 调用同一端点 `POST /api/tasks/notify`（`skills/repo-onboarding/SKILL.md:43-56`）：

```
repo-onboarding skill（experience / explore 各一次）
   └─ 步骤 4：curl POST /api/tasks/notify
        assignee=仓库 owner
        issue_number=新建的引导 Issue 号（experience=#1, explore=#2）
        task_description=简要任务描述
```

`src/api/onboarding.py` 后端**无任何 notify 调用**（`_finalize_onboarding`/`_execute_agent_wrapper`
只处理 Issue 阶段落库），任务通知完全依赖 skill 里的 curl。

### 5.2 同一端点 → 复用同一链路（含 role 问题）

repo-onboarding 的 curl 进入 `tasks.py` 的 `/api/tasks/notify`（与 Issue 分配**同一个端点**），
因此**同样走**：

1. 建任务 + 建/复用 conversation（`conv_type=task`）
2. **预写入 `role="user"` 的任务上下文消息**（`tasks.py:267`）→ 同样存在 role 颠倒问题
3. 后台 `_run_notify` 追加 agent 回复

**结论：本修正改的是 `tasks.py` 端点本身（预写入 role 改 `agent` + 删重复 user 消息），
对包括 onboarding 在内的所有 notify 调用方统一生效，onboarding 无需单独改代码。**

### 5.3 Onboarding 特有差异点

| 差异 | 说明 |
|---|---|
| assignee 是 owner | notify 的 `assignee=owner`。若 owner 未注册平台（`unified_user_mapping` 无记录），`_get_user_id_by_gitcode` 返回 None → `ok:false`，任务不创建、无对话（skill 步骤 4 已有"若 ok:false 在 Issue 评论说明"降级）。 |
| owner 可能未绑飞书 | `_run_notify` 里 `open_id` 空 → 跳过飞书通知，但仍建任务 + 对话 + chat-agent 执行。 |
| 并发两次 | `trigger_onboarding` 对 experience/explore 各执行一次，每个 target 各调一次 notify → 两个任务、两个对话（各关联 #1/#2）。 |
| 对话标题匹配 | 复用逻辑按「用户 + 仓库 + 标题 `Issue #{N}`」，onboarding Issue 编号从 1 递增，标题匹配正常。 |

### 5.4 修正后的 onboarding 任务对话（期望）

```
[agent] 📋 任务分配 · Issue #1        ← 右侧气泡，bot 下发
        仓库: owner/repo
        Issue 标题: 【体验】快速部署并体验本项目的核心功能
        任务描述: ...
        请确认是否开始处理，或直接提出疑问。
[user]  好的，我来体验一下              ← 用户答复确认
[agent]  收到，请按 … 体验              ← chat-agent 继续
```

## 6. 影响与风险

| 项 | 说明 |
|---|---|
| UI 展示 | 任务通知从用户左气泡 → agent 右气泡，符合"bot 下发"语义 |
| 对话 prompt | role 修正后，agent 首个回复基于"等待用户确认"正确语境 |
| 重复消息 | 任务描述从出现两次 → 一次（消除冗余） |
| 兼容性 | `chat_messages.role` 仅存字符串，改 role 无 schema 变更；存量会话中已错标的旧消息不迁移（不影响新任务） |
| 风险 | 低——改动点小、无前端/DB 结构变更；现有 `tests/test_tasks.py` 不断言消息 role，不破坏现有测试 |

## 7. 测试用例看护设计

> 在 `tests/test_tasks.py`（现有 `TestNotify` 类）中补充断言，防止 role 修正与
> 去重逻辑回归。沿用现有测试基础设施：`TestClient`、`_bot_headers()`、`setup_module`
> 中预建的 `younger_eric` 映射 + `gitcode:org/test` 托管仓库。

### 7.1 用例 1：notify 预写入消息 role=agent

```python
def test_notify_prewrite_role_is_agent(self):
    """任务分配预写入消息应为 agent（bot 下发），而非 user；任务描述只出现一次"""
    _clean_tasks()

    def _swallow(coro, *a, **k):
        try:
            coro.close()
        except Exception:
            pass
        return None

    with patch.object(tasks_mod.asyncio, "create_task", side_effect=_swallow):
        r = client.post("/api/tasks/notify", headers=_bot_headers(), json={
            "assignee": "younger_eric", "repo_key": "org/test",
            "issue_number": 5, "task_description": "处理登录超时"})
    assert r.status_code == 200
    conv_id = r.json()["conversation_id"]

    with db_lock:
        c = db.cursor()
        rows = c.execute(
            "SELECT role, content FROM chat_messages WHERE conversation_id=? ORDER BY id",
            (conv_id,)).fetchall()
    assert rows, "会话应有消息"
    # 首条（预写入的任务上下文）必须是 agent
    first_role, first_content = rows[0]
    assert first_role == "agent", f"预写入消息 role 应为 agent，实际 {first_role}"
    assert "任务分配" in first_content
    # 任务描述在整个会话中只出现一次（无 _run_notify 重复写入）
    desc_count = sum(1 for _, content in rows if "处理登录超时" in content)
    assert desc_count == 1, f"任务描述应只出现一次，实际 {desc_count}"
```

### 7.2 用例 2：存量 role=user 的回归护栏（反向断言）

```python
def test_notify_message_roles_order(self):
    """会话消息顺序：agent(任务分配) → user(用户答复) → agent(chat-agent 回复)"""
    _clean_tasks()

    def _swallow(coro, *a, **k):
        try:
            coro.close()
        except Exception:
            pass
        return None

    with patch.object(tasks_mod.asyncio, "create_task", side_effect=_swallow):
        r = client.post("/api/tasks/notify", headers=_bot_headers(), json={
            "assignee": "younger_eric", "repo_key": "org/test",
            "issue_number": 6, "task_description": "描述B"})
    assert r.status_code == 200
    conv_id = r.json()["conversation_id"]

    # 追加一条用户答复（模拟"去对话"后用户确认）
    from src.session_manager import add_message
    add_message(conv_id, "user", "好的，我开始处理")

    with db_lock:
        c = db.cursor()
        roles = [row[0] for row in c.execute(
            "SELECT role FROM chat_messages WHERE conversation_id=? ORDER BY id",
            (conv_id,)).fetchall()]
    # 修正后：至少 [agent, user, ...]；不允许以 user 开头（旧 bug 特征）
    assert roles and roles[0] == "agent", f"首条必须是 agent，实际 {roles}"
    assert "user" in roles, "后续应有用户答复"
```

### 7.3 用例 3：_build_task_context_message 内容断言（单测）

```python
def test_build_task_context_message_content(self):
    """任务上下文消息内容：含 Issue 号/仓库/标题/任务描述，结尾为下发询问语气"""
    msg = tasks_mod._build_task_context_message(
        title="登录超时", task_description="处理登录超时",
        repo_key="org/test", issue_number=5)
    assert "任务分配" in msg
    assert "#5" in msg
    assert "org/test" in msg
    assert "登录超时" in msg
    assert "处理登录超时" in msg
    # 结尾为 bot 下发询问（等待用户确认），而非用户自述
    assert "沟通" in msg or "确认" in msg or "疑问" in msg
```

### 7.4 用例 4：onboarding notify 复用（集成级，可选）

```python
def test_onboarding_notify_reuses_same_endpoint_role(self):
    """repo-onboarding skill 的 notify 与 Issue 分配走同一端点：role=agent 同样生效。
    （onboarding 无独立端点，本用例验证 notify 对任意 repo+assignee 的角色语义一致。）"""
    # 复用用例 1 的 notify 调用即可覆盖：notify 端点不区分调用来源，
    # onboarding skill 的 curl 与 Issue Agent 分配进入同一处理路径。
    # 此用例作为显式占位，防止未来 onboarding 引入独立端点时遗漏角色语义。
    pass
```

### 7.5 说明

- 用例 1/2 会因 `_run_notify` 被 mock 掉（`create_task` 吞掉），只验证 **notify 同步预写入**
  的行为（role + 去重），不依赖后台 chat-agent 实际执行——确定性高、无网络依赖。
- 用例 3 为纯函数单测，直接验证 `_build_task_context_message`。
- 用例 4 为防回归占位：若未来 onboarding 新增独立任务通知端点，需同步保证 role=agent。
- 现有测试（`test_normal_path_creates_task_and_conversation` 等）不断言消息 role，
  补充用例不破坏现有断言。

## 8. 验收标准

1. 新建任务分配后，任务会话打开即见**右侧 agent 气泡**显示"📋 任务分配 · Issue #N…"。
2. 任务描述在会话中**只出现一次**。
3. 用户发送答复后，agent 回复基于"用户已确认"语境。
4. `tests/test_tasks.py` 新增看护用例（§7 用例 1/2/3）通过。

---

*本文为设计文档，纳入 git（docs/new-version2/task-conversation-role-fix.md）。*
