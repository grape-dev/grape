# Chat 技能场景拆分与任务对话独立 Prompt 设计方案

> 版本：v1.0
> 日期：2026-08-12
> 状态：待评审
> 需求：chat 技能（IM 对话处理）目前把多个场景流程混在单一 `skills/chat/SKILL.md` 中，且任务场景（任务中心分配 Issue 后的"去对话"）与一般问询共用同一套 prompt 构建，未做场景区分。要求：① 将任务处理、意见反馈、实施启动确认三个流程从 SKILL.md 拆分，各自独立到 `references/` 文件；② SKILL.md 作为入口，根据上下文识别场景并调度到对应流程；③ 任务首次"去对话"单独创建 session，拥有独立的 prompt（注入 Issue 上下文 + 明确场景标识），不再复用通用 chat prompt。
> 前置：`skills/chat/SKILL.md`、`src/api/routes/tasks.py`、`src/api/routes/chat.py`、`src/prompt_builder.py::build_chat_prompt`、`src/im/handler_async.py`、`src/session_manager.py`（chat_conversations CRUD）。

---

## 一、现状与问题

### 1.1 当前场景分布（单一 SKILL.md，294 行）

| 章节 | 行号 | 场景 |
|---|---|---|
| 「场景」+「能力边界」 | 21-42 | 混杂描述两类能力 |
| 「实施启动确认」 | 44-91 | **实施启动**（审批 → PATCH 标签触发 implementation） |
| 「任务处理：澄清被分配 Issue」 | 93-160 | **任务场景**（澄清需求 → 回帖到 Issue） |
| 「意见反馈处理」 | 162-221 | **一般问询**（需求引导 → 创建 Issue） |
| 「响应规则」+「操作边界」+「输出格式」+「参考」 | 223-294 | 通用约束 |

### 1.2 三个关键问题

**问题 A：三个流程混在一个文件**
- 任务处理（回帖到已分配 Issue）、意见反馈（创建新 Issue）、实施启动（PATCH 标签）三者处理方式完全不同，却在同一文件顺序编排，Agent 易混淆该走哪条流程。

**问题 B：任务对话没有独立 session/prompt**
- `notify_task`（tasks.py:244-266）创建/复用 `chat_conversations` 会话，title=「Issue #N」，并把「📋 任务分配 · Issue #N」写入作为一条 user 消息。
- 但用户后续"去对话"走 `chat.py send_message` → `_build_chat_prompt`（prompt_builder.py:448），**不识别任务上下文**——只是 history 里多了一条"📋 任务分配"消息，靠 Agent 从历史嗅探场景。
- `_build_task_prompt`（tasks.py:138，带 Issue 上下文）**仅用于后台 notify 一次性执行**（`_run_notify`），用户去对话时不使用，两套 prompt 不统一。

**问题 C：SKILL 无法被明确调度**
- 通用 prompt 未注入"场景"字段，Agent 需自行从杂糅内容判断场景，易误走流程。

### 1.3 数据模型现状

`chat_conversations`（db_setup.py:168-182）：`id / agent_session_id / user_id / backend_session_id / user_gitcode_token / repo_key / im_channel_id / title / status / created_at / updated_at`——**无会话类型字段**（无法区分任务会话 / 一般会话）。

---

## 二、设计目标与原则

1. **场景隔离**：任务处理、意见反馈、实施启动确认三个流程各自独立成 `references/` 文件，互不干扰。
2. **SKILL.md 作调度入口**：统一场景识别规则（上下文信号 → 场景），按场景要求读取对应流程文件执行。
3. **任务对话独立 session + 独立 prompt**：任务首次"去对话"创建带类型标记的会话，prompt 注入完整 Issue 上下文并显式标注场景，Agent 直接走任务处理流程，不再嗅探。
4. **复用既有 API**：流程文件中的 GitCode 调用沿用现有 `$USER_GITCODE_TOKEN` / `{{SERVER_URL}}` 机制。
5. **默认零回归**：一般问询（IM 直接提问、Chat Agent 页新建对话）行为不变；既有历史会话兼容。

---

## 三、Skill 结构调整

```
skills/chat/
├── SKILL.md                      # 入口：场景识别 + 通用约束 + 调度
└── references/
    ├── gitcode-api-reference.md  # 已有：GitCode API 参考
    ├── task-handling.md          # 【新增】任务场景：澄清被分配 Issue → 回帖
    ├── inquiry-handling.md       # 【新增】一般问询：需求/建议引导 → 创建 Issue
    └── implementation-start.md   # 【新增】实施启动确认：审批 → PATCH 标签触发 implementation
```

### 3.1 `SKILL.md`（重构为调度器）

保留通用部分（安全硬约束、能力边界、链接格式、@用户、输出格式、@mention、参考），将三个流程替换为「场景识别」+「调度」：

```markdown
---
name: chat
version: 2.0.0
description: |
  飞书等 IM 对话处理指南。当用户在飞书/Slack/其他 IM 中与 Grape 对话时使用此技能。
  入口职责：识别当前对话属于哪一场景，并按对应流程文件处理。
  场景：task（任务处理）、inquiry（意见反馈/需求引导）、implementation-start（实施启动）。
  不适用于：GitCode 网页上的 Issue/PR 评论直接触发（用 issue skill）。
---

# IM 对话处理

## 安全硬约束（必须遵守）
（保留现有 §14-19 原样）

## 场景识别（必须先判定）

读取 prompt 上下文，按下述优先级判定当前场景：

| 场景 | 判定信号 | 流程文件 |
|------|---------|---------|
| **task 任务处理** | prompt 含「📋 任务分配 · Issue #N」或 `场景: task`；或用户明确「帮我看看/处理 Issue #N」 | `references/task-handling.md` |
| **implementation-start 实施启动** | 用户回复「同意启动/启动/确认/开始/批准」且是针对机器人发出的实施启动批准请求；或 prompt 含 `场景: implementation-start` | `references/implementation-start.md` |
| **inquiry 意见反馈** | 用户提出建议/新需求/问题反馈；或 prompt 含 `场景: inquiry`；或以上均不命中且为 DevOps 相关话题 | `references/inquiry-handling.md` |

**判定顺序**：先查 prompt 显式 `场景:` 字段 → 再按上下文信号识别 → 兜底 inquiry。
**无关话题**：非 DevOps 话题按「能力边界」礼貌说明（不读任何流程文件）。

## 能力边界（保留现有 §26-42）

## 调度执行
- 判定场景后，**先用 read tool 读取对应 references 流程文件**，严格执行其中步骤。
- 流程文件内的 GitCode 调用使用 `$USER_GITCODE_TOKEN`，服务端地址用 `{{SERVER_URL}}`。

## 响应规则 / 操作边界 / 输出格式 / @用户 / 链接格式（保留现有 §223-294 原样）
```

### 3.2 `references/task-handling.md`（源自现 §93-160）

任务场景独立流程：判断进入澄清 → 读取 Issue 背景 → 分析不清晰点 → 精准引导补充 → 回帖到 Issue → 回复用户。内容照搬现有「任务处理：澄清被分配 Issue」章节（含 Bug/功能类的缺失检查表、回帖 body 模板）。

### 3.3 `references/inquiry-handling.md`（源自现 §162-221）

意见反馈独立流程：判断建议归属 → 引导补充信息（功能/问题/优化三类模板）→ 创建 Issue（不带标签）→ 回复用户。内容照搬现有「意见反馈处理」章节。

### 3.4 `references/implementation-start.md`（源自现 §44-91）

实施启动独立流程：0 检查自动实现开关 → 1 查询待启动 Feature → 2 筛选标准 → 3 启动实施（PATCH feature,triaged,PATCH 标签）→ 4 回复用户 → 5 未找到时的兜底。内容照搬现有「实施启动确认」章节。

---

## 四、任务对话独立 Session + 独立 Prompt

### 4.1 数据模型：`chat_conversations` 增加 `conv_type`

```sql
ALTER TABLE chat_conversations ADD COLUMN conv_type TEXT DEFAULT 'general';  -- general | task
```

- `general`：一般问询对话（IM / Chat 页新建，默认）。
- `task`：任务中心"去对话"创建的会话，`title=Issue #N`，可绑定 `issue_number`。

### 4.2 DB 迁移（`db_setup.py` `_migrate_tables` 幂等区）

```python
try:
    with db_lock:
        c = db.cursor()
        _ensure_column(c, "chat_conversations", "conv_type", "TEXT DEFAULT 'general'")
        _ensure_column(c, "chat_conversations", "issue_number", "INTEGER DEFAULT 0")
        db.commit()
        log.info("Added conv_type / issue_number to chat_conversations")
except Exception as e:
    log.warning(f"Add chat conversation type failed: {e}")
```

### 4.3 `notify_task`（tasks.py:244-266）：创建任务会话时标记类型

```python
if not conv_id:
    conv_id = create_conversation(user_id, body.repo_key,
                                  title=f"Issue #{body.issue_number}",
                                  conv_type="task", issue_number=body.issue_number)
else:
    update_conversation(conv_id, conv_type="task", issue_number=body.issue_number)
```

- `session_manager.create_conversation` 增加可选参 `conv_type` / `issue_number`（INSERT 加两列）。
- 已有「Issue #N」title 的会话复用并补标记，兼容存量。

### 4.4 `chat.py send_message`：按会话类型选择 prompt 构建

```python
conv_type = (conv or {}).get("conv_type", "general")

if conv_type == "task":
    # 任务会话：独立 prompt，注入 Issue 上下文 + 场景=task
    prompt = _build_task_chat_prompt(body.message, repo_key, history, skill_name, skill_id,
                                     conv, gitcode_token)
else:
    # 一般会话：原通用 prompt
    prompt = _build_chat_prompt(body.message, repo_key, history, skill_name, skill_id)
```

#### 4.4.1 新增 `_build_task_chat_prompt`（prompt_builder.py）

复用 `_build_task_prompt`（tasks.py:138）的 Issue 上下文组装，合并用户消息与历史：

```python
def build_task_chat_prompt(user_text, repo_key, history, conv, account="", runtime_id=0):
    issue_number = conv.get("issue_number") or 0
    parts = [f"## 场景: task", f"## 任务 Issue", f"仓库: {repo_key}", f"Issue: #{issue_number}"]
    if conv.get("title"):
        parts.append(f"标题: {conv['title']}")
    # 注入 issue 摘要（与 _build_task_prompt 一致）
    try:
        from src.api.event_store import get_issue_summary
        issue_summary = get_issue_summary(repo_key)
        if issue_summary:
            parts.append(f"\n{issue_summary}")
    except Exception:
        pass
    # 历史消息（用户澄清过程）
    if history:
        parts.append("\n## 对话历史")
        for m in history[-10:]:
            parts.append(f"[{m.get('role')}] {m.get('content', '')[:500]}")
    parts.append(f"\n## 用户消息\n{user_text}")
    # chat skill 注入（同 build_chat_prompt 尾部）
    if skills:
        parts.append(f"Skills: chat")
        _sp = f"{{{{SKILL_DIR}}}}/chat/SKILL.md"
        parts.append(f"Skill 文件：{_sp}\n**必须先执行**：用 read tool 读取该 SKILL.md，并严格执行其中的指令。")
    parts.append(build_chat_role_definition())
    return "\n".join(parts)
```

> `场景: task` 显式注入 → SKILL.md 场景识别直接命中任务场景，Agent 走 `references/task-handling.md`。

### 4.5 前端「去对话」定位（chat.js `loadConversations`）

现有逻辑（chat.js:88-120）：`_openConvId` 定位任务指定会话，`repoKey` 传空以跨仓库定位。保持不变即可——任务会话带 `conv_type=task` 与 `issue_number`，后端据此构建独立 prompt。

### 4.6 IM 渠道（`im/handler_async.py`）兼容

- IM 群聊/私聊创建会话默认 `conv_type=general`（不触发任务 prompt）。
- 若 IM 消息上下文含「任务分配 · Issue #N」（如用户转发），SKILL 场景识别按上下文信号走任务流程，无需改 handler 代码。

---

## 五、代码改动清单汇总

| 文件 | 变更 |
|---|---|
| `skills/chat/SKILL.md` | 重构为调度器：场景识别 + 调度，三流程移出 |
| `skills/chat/references/task-handling.md` | 新增（源自现 §93-160） |
| `skills/chat/references/inquiry-handling.md` | 新增（源自现 §162-221） |
| `skills/chat/references/implementation-start.md` | 新增（源自现 §44-91） |
| `src/api/db_setup.py` | `chat_conversations` 加 `conv_type`/`issue_number` 列 |
| `src/session_manager.py` | `create_conversation`/`get_conversation`/`update_conversation` 支持新列 |
| `src/api/routes/tasks.py` | `notify_task` 标记会话类型 |
| `src/prompt_builder.py` | 新增 `build_task_chat_prompt` |
| `src/api/routes/chat.py` | `send_message` 按 `conv_type` 选择 prompt |

---

## 六、用户可见效果

1. **任务中心"去对话"**：进入后即携带 Issue 上下文 + 任务场景标记，Agent 直接澄清被分配 Issue 的需求，回帖到该 Issue——不再需要 Agent 从历史消息里猜场景。
2. **一般问询**：IM 直接提问 / Chat 页新建对话行为不变（general 场景，需求引导流程）。
3. **实施启动**：明确审批意图时走独立流程，避免与一般问询混淆。
4. **历史兼容**：存量 `Issue #N` 标题会话复用并补 `conv_type=task` 标记，无数据丢失。

---

## 七、边界与取舍

| 项 | 决策 | 理由 |
|---|---|---|
| 会话类型识别 | `conv_type` 列（general/task），`issue_number` 可选 | 显式标记优于靠 title 嗅探；存量 title=Issue #N 会话迁移时补标记 |
| 场景判定优先级 | 显式 `场景:` 字段 → 上下文信号 → 兜底 inquiry | 显式字段最可靠；上下文信号覆盖 IM 渠道；兜底保证不遗漏 |
| 任务 prompt 复用 | 新增 `build_task_chat_prompt`，与 `_build_task_prompt` 共用 Issue 上下文逻辑 | 两处场景各自独立；后台 notify 与用户对话分离，各司其职 |
| 前端改动 | 最小（保持现有 `_openConvId` 定位） | 会话类型与 prompt 由后端决定 |
| IM 渠道 | 默认 general，任务上下文靠 SKILL 信号识别 | IM 无任务会话概念，靠上下文信号即可 |
| 实施启动流程归属 | 独立文件 `implementation-start.md` | 与任务/问询处理方式不同，独立更清晰 |

---

## 八、测试计划

| 用例 | 断言 |
|---|---|
| `chat_conversations` 迁移 | 新库/旧库均含 `conv_type`（默认 general）与 `issue_number` 列 |
| `notify_task` 创建任务会话 | 新会话 `conv_type=task`、`issue_number` 正确；复用存量 Issue #N 会话时补标记 |
| `send_message` 任务会话 | 走 `build_task_chat_prompt`，prompt 含 `场景: task` + Issue 上下文 |
| `send_message` 一般会话 | 走 `build_chat_prompt`，prompt 无 `场景: task` |
| SKILL 场景识别 | prompt 含「任务分配 · Issue #N」→ 命中 task 场景；含"同意启动"审批信号 → implementation-start；其余 → inquiry |
| references 文件存在性 | 三个流程文件存在且被 SKILL 正确引用 |
| 历史兼容 | 存量「Issue #N」会话迁移后仍可进入，prompt 正确 |
| 现有回归 | IM 一般对话、Chat 页新建对话行为不变 |

---

## 九、实施顺序（后续排期）

1. `skills/chat/SKILL.md` 重构 + 三个 references 流程文件拆分。
2. `db_setup.py` / `session_manager.py`：`chat_conversations` 加列与 CRUD 支持。
3. `prompt_builder.py`：新增 `build_task_chat_prompt`。
4. `tasks.py` `notify_task` 标记会话类型；`chat.py` `send_message` 按类型选 prompt。
5. 测试落地（§八）+ 全量回归（chat / task / im 相关）。
