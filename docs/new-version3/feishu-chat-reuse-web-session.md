# 飞书 Chat 复用 Web Chat 会话（session_id）统一方案

> 版本：v1.0
> 日期：2026-08-31
> 状态：**已分析，待实施**
> 关联：`docs/new-version3/web-chat-no-response-cwd-fix.md`（web cwd 修复，已上线）
> 目标：同一对话（conversation）下，飞书与 Web Chat 复用**同一个 opencode session_id + 同一个工作目录**，根治"飞书收不到实质回复 / 两边不一致"。

---

## 1. 背景与现状问题

### 1.1 已有修复（web 侧，已上线）

- Web Chat cwd 已统一为 `WORKSPACE_DIR/{owner}/{repo}/chat/{uid}/{conv_id}`；
- Web 的 opencode session（`runtime_session_id`）存在 `chat_conversations.runtime_session_id`；
- 因此 Web 侧：同 conv = 同 session + 同 cwd → 一致 → 正常。

### 1.2 飞书侧仍未对齐（本次要修）

飞书 `process_chat_async` 执行路径：

```
conv_id = get_conversation_by_im(chat_id) 或新建   ← 已拿到 conversation（与 web 同一个）
wsd = WORKSPACE_DIR/{owner}/{repo}/chat/{uid}/{conv_id}   ← 已与 web 同目录（新代码）
oc_sid = get_or_create_session(repo_key).runtime_session_id   ← ❌ 仍是 repo 级旧会话
```

**问题**：飞书虽然已复用 web 的 `conversation`（如 61e267e9），但 **opencode session 用的是 repo 级 `chat_agent_session.runtime_session_id`（旧 `ses_fdcb68`）**，而不是 conversation 的 `runtime_session_id`。

由于 `wsd` 随 conv 变化、而 repo 级 session（directory）是固定/滞后的，二者经常不一致 → opencode 无法按 `--session` 恢复历史 → **回复空（`reply_text ok: text=?`）**，用户收不到实质回复。

## 2. 根因

**飞书与 Web 没有共享同一个 opencode session 来源**：

| 维度 | Web Chat | 飞书 IM（现状） |
|---|---|---|
| conv_id | conversation.id | conversation.id（**已一致**） |
| cwd | `WORKSPACE_DIR/{owner}/{repo}/chat/{uid}/{conv_id}` | 同（**已一致**） |
| oc_sid 来源 | `conversation.runtime_session_id` | `chat_agent_session.runtime_session_id`（**repo 级，不一致**） |
| oc_sid 写回 | conversation.runtime_session_id | chat_agent_session.runtime_session_id |

只要飞书改用 **conversation.runtime_session_id**，两边 session 与 cwd 就完全同源。

## 3. 修复方案（飞书复用 conversation 的 session）

### 3.1 核心改动：飞书 handler 的 oc_sid 来源切换

**文件**：`src/im/handler_async.py`（`_process_chat_inner_async`）

1. **读取**：拿到 `conv_id` 后，读取 `conversation.runtime_session_id` 作为 `oc_sid`（若 `is_new` 或为空则沿用现有 repo 级获取逻辑作兜底，但**优先 conversation**）。
2. **写回**：每次执行结束后，将 `new_oc_session_id` 写回 `conversation.runtime_session_id`（web 同款行为），而不再只写 repo 级 `chat_agent_session`。
3. **cwd**：保持已实现的 `wsd = WORKSPACE_DIR/{owner}/{repo}/chat/{uid}/{conv_id}` 不变。

伪代码：

```python
# 读取（优先 conversation；空则兜底 repo 级）
oc_sid = conv.get("runtime_session_id") or ""   # conversation 优先
if not oc_sid:
    _, _, repo_oc, cc_sid, _ = get_or_create_session(repo_key, source, repository)
    oc_sid = repo_oc

# 执行（同 web：stream_agent / consume_stream_async(..., session_id=oc_sid, cwd=wsd)）

# 写回（同 web）
if new_oc_session_id:
    update_conversation(conv_id, runtime_session_id=new_oc_session_id)
```

### 3.2 保留 repo 级会话作兜底（不破坏现有逻辑）

- 若 `conv.runtime_session_id` 为空（首次/新 conversation），回退到 `chat_agent_session.runtime_session_id`；
- 飞书首次创建 conversation 时若尚未有 session，仍走 repo 级创建并写回 conversation。
- 这样旧的 repo 级会话只作为兜底，不再成为主路径。

### 3.3 存量数据修正（一次性）

| 项 | 处理 |
|---|---|
| 旧 `ses_fdcb68`（repo 级目录是 `feishu/...`） | 该会话若已不被 conversation 引用，保留不动（孤儿即可）；若被引用则以 conversation 的 session 为准 |
| 存量 conversation 中 `runtime_session_id` 为空 | 由 3.1 兜底逻辑在下次消息时自动补写；无需人工迁移 |
| opencode.db 中已迁移到 `chat/7/518a...` 的会话 | 与 web 已一致，保持即可 |

> 结论：**无需额外迁移**，飞书下次发消息即会走 conversation session 新路径并自动对齐。

### 3.4 与 web cwd 修复的关系

- 本方案是 `web-chat-no-response-cwd-fix.md` 的**飞书侧补齐**：
  - web：session + cwd 都在 conversation → 已修复；
  - 飞书：cwd 已对齐 conversation，但 session 仍 repo 级 → 本次对齐 session。
- 修复后同一 conversation 在飞书 / web 两侧 = 同一 opencode session + 同一工作目录 → 完整一致。

## 4. 涉及文件

| 文件 | 改动 |
|---|---|
| `src/im/handler_async.py` | `_process_chat_inner_async`：oc_sid 读取/写回改为 conversation.runtime_session_id（优先），repo 级作兜底 |
| `src/im/handler_async.py` | 执行后 `update_conversation(conv_id, runtime_session_id=...)` |
| （可选）`src/session_manager.py` | `get_conversation` / `update_conversation` 已支持 runtime_session_id，无需改 |

## 5. 验证

1. 飞书发消息 → 日志出现 `opencode args ... --session 同一 id` + 后续有实质文本；`reply_text / send_card` 内容非 `?`。
2. 同一 conversation 先 web 后飞书（或反之）→ **两边使用同一个 session_id、同一 cwd**（日志核对）。
3. `conversation.runtime_session_id` 在飞书执行后被写入/更新。
4. 新 conversation 首次走 devops 兜底创建 session 并写回 conversation，后续消息复用。

## 6. 测试用例看护设计

> 目标：把"飞书与 Web 复用同一会话/目录"固化为自动测试。实现上可将 oc_sid 解析抽成可测纯函数（如 `_resolve_oc_sid(conv, repo_oc_sid)`），便于单测。

### 6.1 单元测试（`tests/test_feishu_session_unify.py`）

| 用例 | 断言 |
|---|---|
| conv.runtime_session_id 非空时飞书 oc_sid = conv 值 | `_resolve_oc_sid({'runtime_session_id':'ses_web1'}, 'ses_repoX')` → `'ses_web1'`（不回 repo 级） |
| conv.runtime_session_id 为空时回退 repo 级 | `_resolve_oc_sid({'runtime_session_id':''}, 'ses_repoX')` → `'ses_repoX'` |
| conv.runtime_session_id 为 None/缺失时回退 | `_resolve_oc_sid({}, 'ses_repoX')` → `'ses_repoX'` |
| 空会话名兜底为空串 | `_resolve_oc_sid({'runtime_session_id':'ses_a'}, '')` → `'ses_a'` |
| 执行后写回 conversation | mock `update_conversation`，断言以 `new_oc_session_id` 调用（不写 repo 级） |
| web 与飞书同 conv 的 session 一致 | 同一 conv 读取 `runtime_session_id` 两边相等 |

### 6.2 契约测试（`tests/test_feishu_web_conv_contract.py`）

| 用例 | 断言 |
|---|---|
| web 首次 submit 建 conv 并写入 runtime_session_id | conv 执行后 `runtime_session_id` 以 `ses_` 开头 |
| 飞书消息在同 conv 复用该 session | mock handler，断言 `consume_stream_async` 实参 session_id == conv.runtime_session_id |
| 飞书执行后写回新 session | mock `update_conversation`，断言 `runtime_session_id` 更新为新值 |
| 飞书新 conv 空 session → 走 repo 级兜底并写回 | 首次消息后 conv.runtime_session_id 非空且与执行所用一致 |

### 6.3 运行级回归（人工/脚本，`scripts/verify_feishu_web_conv.sh`）

1. Web 发一条消息 → 记下 conv_id 与日志 `--session <sesX>`。
2. 同一对话在飞书发一条消息 → 日志 `--session` 应**等于 sesX**（非旧 repo 会话）。
3. 飞书收到**有实质内容**的回复（非 `?`/空）。
4. `conversation.runtime_session_id` 一致且随执行更新。

### 6.4 运行命令

```
python -m pytest tests/test_feishu_session_unify.py tests/test_feishu_web_conv_contract.py -q
```

## 7. 测试用程序化示例（供实施参考）

```python
# tests/test_feishu_session_unify.py
def _resolve_oc_sid(conv, repo_oc_sid):
    # 优先 conversation 会话，空则回退 repo 级（与实现一致）
    conv_sid = (conv or {}).get("runtime_session_id") or ""
    return conv_sid or repo_oc_sid

def test_conv_priority():
    assert _resolve_oc_sid({"runtime_session_id": "ses_web1"}, "ses_repoX") == "ses_web1"

def test_fallback_repo():
    assert _resolve_oc_sid({"runtime_session_id": ""}, "ses_repoX") == "ses_repoX"
    assert _resolve_oc_sid({}, "ses_repoX") == "ses_repoX"

def test_writeback_uses_new_session(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr("src.im.handler_async.update_conversation",
                        lambda conv_id, **kw: calls.append((conv_id, kw)))
    new_sid = "ses_new123"
    # 模拟执行后写回
    ...  # 由实施时用真实调用填充
    assert calls and calls[-1][1].get("runtime_session_id") == new_sid
```

## 附：关键代码位置

- `src/im/handler_async.py:828-1056` `_process_chat_inner_async`（oc_sid 来源/写回）
- `src/api/routes/chat.py:297` `runtime_session_id = conv.get("runtime_session_id")`（web 参考实现）
- `src/api/chat_task_manager.py`（web 后台线程执行；飞书 direct 走 consume_stream_async）
- `docs/new-version3/web-chat-no-response-cwd-fix.md`（Web 侧 cwd 方案，已上线）