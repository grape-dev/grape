# Task Chat Agent 与 General Chat Agent 流程对比分析

> 版本: v1.0
> 日期: 2026-08-29
> 代码位置: `src/api/routes/chat.py` / `src/im/handler_async.py` / `src/agent/executor.py`

## 1. 结论速览

Task 会话与 General 会话**共用同一个入口与执行引擎**，差异仅在 **prompt 构建、会话定位、工作目录、返回信息** 四个维度。两条路径在「调用 runtime / 复用 session」上基本一致；在「工作目录 / 返回信息」上存在实质性区别。

| 维度 | Task Chat | General Chat |
|---|---|---|
| runtime 调用 | 同一 `stream_agent` / `consume_stream_async` / 入队 | 相同 |
| runtime 工作目录 | `/home/grape/chat/{user_id}/{conv_id}`（web）、`WORKSPACE_DIR/feishu/{repo}/{sess尾}`（飞书） | **相同** |
| 复用 session_id | conv 级 `runtime_session_id`（web） / repo 级 `oc_sid`（飞书） | 相同机制 |
| 返回信息 | 回复 + conversation_id | 相同 |
| **局部差异** | 额外注入 Issue 上下文/任务描述；打开时同步 Issue 评论 | 无 |

---

## 2. 入口与调用链

```
Web UI / 飞书 IM
   │  消息进入
   ▼
chat.py: send_message / send_message_stream / _enqueue_chat     ← web
handler_async.py: _process_chat_inner_async                      ← 飞书
   │  按 conv_type 分支
   ├── conv_type == "task" → build_task_chat_prompt()（Issue 上下文 + 任务描述）
   └── 否则               → build_general_chat_prompt()（通用）
   │
   ▼  选择执行模式
   ├── direct（本机直连）: stream_agent() / consume_stream_async()
   └── queue（远端 worker）: enqueue_task() → wait_task_terminal()
```

---

### 2.1 `build_task_chat_prompt` vs `build_general_chat_prompt` 源码对照

#### 2.1.1 函数签名

```python
# build_general_chat_prompt（src/prompt_builder.py:454）
def build_general_chat_prompt(message, repo_key="", history=None,
                              skill_name="", skill_id=0, runtime_id=0,
                              backend_name="", user_info="", skills=None, account=""):
    # 注：web 走 skill_name/skill_id/backend_name；飞书/tasks 走 skills/runtime_id/user_info

# build_task_chat_prompt（src/prompt_builder.py:555）
def build_task_chat_prompt(user_text, repo_key, history, conv=None,
                           account="", runtime_id=0, user_info=""):
    # 多一个 conv 参数（携带 issue_number/title/tasks 关联）
```

#### 2.1.2 `build_general_chat_prompt` 源码

```python
parts = [build_chat_role_definition()]                 # ① Role（general 专属，见 §2.1.6）
if backend_name:                                       # ② Runtime engine
    parts.append(f"Runtime engine: {backend_name}")
elif runtime_id:
    ...engine_name(runtime_id) → parts.append(f"Runtime engine: {_be}")
if user_info:                                          # ③ 用户基本信息
    parts.append(user_info)
if repo_key:                                           # ④ 仓库
    parts.append(f"\nIMPORTANT: You are working in repository: {repo_key}")
if repo_key:                                           # ⑤ 事件上下文（Issue 摘要）
    summary = get_issue_summary(repo_key)
    if summary: parts.append(f"\nRepository event context:\n{summary}")
parts.append(_build_project_info_section(repo_key, agent_type="chat"))  # ⑥ 项目知识基线
if skill_name or skill_id:                             # ⑦ 使用技能
    ...官方技能→"## 使用技能 / {{SKILL_DIR}}/<skill>/SKILL.md"
        custom(user skill)→"## Current Skill: 绝对路径"
parts.append("## GitCode API Access ...")              # ⑧ GitCode API 说明
parts.append(f"\nUser{repo_note}: {message}\nAssistant:")  # ⑨ 用户消息→Assistant
```

> general Role（`build_chat_role_definition`）已精简：不含「创建 Issue 转交流水线」流程、
> 不含 ISSUE COMMENT FORMAT（回帖格式）——该流程/规则改由 skill 承载（§2.1.6）。

#### 2.1.3 `build_task_chat_prompt` 源码

```python
conv = conv or {}; issue_number = conv.get("issue_number") or 0
parts = []
parts.append(build_task_role_definition())             # ① Role（task 专属，见 §2.1.6）
if user_info:                                          # ② 用户基本信息
    parts.append(user_info)
parts.extend([f"## 场景: task", f"## 任务背景",          # ③ 场景 + 任务背景
              f"仓库: {repo_key}", f"Issue: #{issue_number}"])
if runtime_id:                                         # ④ Runtime engine
    ...parts.append(f"Runtime engine: {_be} (runtime_id={runtime_id})")
parts.append("### 任务边界 ... "                       # ⑤ 任务边界（澄清+回帖+不实施）
             "严禁 clone/部署/起服务/修改文件/执行 git 命令")
task_desc = _get_task_description(conv)                 # ⑥ 任务描述（tasks 表）
if task_desc: parts.append(f"\n### 任务描述\n{task_desc}")
if conv.get("title"): parts.append(f"标题: {conv['title']}")     # ⑦ 标题（紧随任务描述）
try:                                                     # ⑧ Issue 摘要
    issue_summary = get_issue_summary(repo_key)
    if issue_summary: parts.append(f"\n{issue_summary}")
parts.append(_build_project_info_section(repo_key, agent_type="chat"))  # ⑨ 项目知识基线
if history:                                             # ⑩ 对话历史
    parts.append("\n## 对话历史")
    for m in history[-10:]: parts.append(f"[{m.get('role')}] {m.get('content','')[:500]}")
parts.append(f"\n## 本次用户消息\n{user_text}")          # ⑪ 用户消息
parts.append("## 使用技能 / Skills: chat /            # ⑫ 使用技能（恒 chat 占位符）
             {{SKILL_DIR}}/chat/SKILL.md ...")
```

#### 2.1.4 逐段差异对照

| # | 段落 | general | task | 差异 |
|---|---|---|---|---|
| ① | Role Definition | `build_chat_role_definition()` | `build_task_role_definition()` | **各自专属**（task 无"建 Issue"流程，见 §2.1.6） |
| ② | 用户信息 | `user_info` 注入 | `user_info` 注入 | **一致** ✅ |
| ③ | 场景/背景 | 无 | `## 场景: task` + `## 任务背景` + 仓库 + Issue | **task 独有** |
| ④ | Runtime engine | `backend_name`（web）或 `engine_name(runtime_id)` | `engine_name(runtime_id) + (runtime_id=n)` | 信息相同，格式不同（general 走 backend_name 优先） |
| ⑤ | 事件上下文 | `Repository event context:` + issue_summary | `{issue_summary}`（无前缀标题） | 同数据源，标题格式不同 |
| ⑥ | 项目知识基线 | `_build_project_info_section(agent_type="chat")` | 同 | **一致** ✅ |
| ⑦ | 任务边界 | 无 | `### 任务边界`（澄清+回帖+不实施） | **task 独有**（A2A 交办规则已移出到 skill） |
| ⑧ | 标题 | 无 | `标题: {conv.title}`（位于任务描述之后） | **task 独有** |
| ⑨ | 任务描述 | 无 | `### 任务描述`（tasks 表，标题紧随其后） | **task 独有**（重点） |
| ⑩ | 对话历史 | 无 | `## 对话历史`（history[-10:]） | **task 独有**（general 已按 v1.6 移除） |
| ⑪ | 使用技能 | 官方→占位符 / custom→绝对路径 | 恒 `## 使用技能 / Skills: chat / {{SKILL_DIR}}/chat/SKILL.md` | general 支持动态/自定义技能；task 恒 chat |
| ⑫ | 用户消息 | `User (in repo): msg\nAssistant:` | `## 本次用户消息\nmsg` | 标题格式不同 |
| ⑬ | GitCode API 说明 | 有 `## GitCode API Access` | 无 | **general 独有** |
| ⑭ | 回帖格式/建 Issue 流程/A2A 交办 | ❌ 已移出（skill 承载） | ❌ 已移出（skill 承载） | **一致：均不在 prompt**（§2.1.6） |

#### 2.1.5 关键差异总结（功能层面）

1. **task 独有**：`场景: task`、任务背景、任务边界、标题、任务描述（`### 任务描述`）、对话历史 —— 面向"澄清被分配 Issue 需求并回帖"的职责。
2. **general 独有**：`GitCode API Access` 段、动态技能（skill_name/skill_id，可指向 custom/repo 用户技能）。
3. **task 恒用 chat 技能**、general 可用 web 配置的技能/自定义技能。
4. **共同段**：用户信息、项目知识基线、仓库、Issue 摘要均一致。
5. **均由 skill 承载（不在 prompt）**：回帖格式、创建 Issue 流程、A2A 交办规则（§2.1.6）。

#### 2.1.6 Role 语义修正（2026-08-29 处理）

**原问题**：`build_task_chat_prompt` 原先复用 general 的 `build_chat_role_definition`，
其 Role 含「接收任务需求 → **创建 Issue 转交流水线**」流程——但 task 会话是**已被分配 Issue**
的对话（`conv.issue_number` 已存在），职责是澄清该 Issue 需求并回帖，不是再建新 Issue，
两者语义错位（已在 §1 讨论中确认）。

**处理（含本轮 4 点精简）**：
1. 新增 `build_task_role_definition()`（task 专属 Role，去掉"创建 Issue"流程，突出澄清+回帖边界）；`build_task_chat_prompt` 改用它；general 仍用 `build_chat_role_definition()`。
2. **删除 general Role 的「任务处理（创建 Issue 转交流水线）」**：该流程改由 skill 承载（避免与 skill 冲突/重复）。
3. **删除 prompt 中的 ISSUE COMMENT FORMAT（回帖[/RoleName Agent/ 前缀]规则）**：回帖格式在 skill 中已有，prompt 不重复规定，以免与 skill 冲突。
4. **删除 task 的「A2A 交办规则」段**：A2A 引导由 skill 承载。
5. **task 标题移到「任务描述」之后**（原在任务边界后）。

#### 2.1.7 处理后两个 Role 的实际 Prompt 内容（代码产出，真实样例）

> 注入参数：`repo_key=org/repo`、`user_info="## 用户基本信息 / GitCode login: u1 / 飞书 昵称：F"`、
> general: `skill_name=chat, backend_name=opencode`；task: `conv={issue_number:7, title:'优化登录页'}`。

**① general（`build_general_chat_prompt`，前段 Role = `build_chat_role_definition`）**

```
You are a DevOps operations assistant. Your domain:
- Code review, build diagnosis, deployment guard, incident triage
- Issue/PR analysis, changelog generation, repository management
- Repository (GitCode/GitHub) API operations

You have no abilities outside DevOps (no weather, email, documents, PDF, Excel, writing, meetings, voice, etc.).

Reply rules:
1. DevOps topic: analyze and reply
2. NOT DevOps topic: politely explain you only handle DevOps
3. Never write code, modify files, or execute git commands
4. Always reply in Chinese. Never include internal reasoning or process steps.

SECURITY (MUST FOLLOW):
1. Use ONLY the GitCode token provided in this prompt (USER_GITCODE_TOKEN). NEVER read, guess, or use any other token from the database, config files, logs, or history.
2. NEVER access the bot's internal data/ and configs/ directories, sessions.db, user_accounts table, or any bot-internal data.
3. If the GitCode API returns 403 (no permission): STOP that operation and tell the user the action needs the required permission (configure it manually in GitCode). NEVER try other tokens or workarounds.
4. NEVER write any token into files, logs, or comments.
Runtime engine: opencode
## 用户基本信息
  GitCode login: u1
  飞书 昵称：F

IMPORTANT: You are working in repository: org/repo

## 项目知识基线（repo 全局）
[无项目知识基线] 允许降级分析：可基于 README 与 GitCode API 获取基础信息，相关结论标注 [degraded]。
> 项目级成员知识：{{PROJECT_INFO_DIR}}/member/，存在则用 read tool 读取并遵循。
> 本 Agent 专属知识：{{PROJECT_INFO_DIR}}/agent/chat/，存在则用 read tool 读取并遵循。
## 使用技能
Skills: chat
Skill 文件：{{SKILL_DIR}}/chat/SKILL.md
**必须先执行**：用 read tool 读取该 SKILL.md，并严格执行其中的指令去完成任务（不得跳过读取或凭空猜测）。

## GitCode API Access
Your token is available as the USER_GITCODE_TOKEN environment variable.
  curl -s -H "Authorization: Bearer $USER_GITCODE_TOKEN" \
    https://api.gitcode.com/api/v5/repos/{owner}/{repo}/issues
Never print or reveal the token value.


User (in org/repo): hello
Assistant:
```

**② task（`build_task_chat_prompt`，前段 Role = `build_task_role_definition`）**

```
You are a DevOps operations assistant. Your domain:
- Code review, build diagnosis, deployment guard, incident triage
- Issue/PR analysis, changelog generation, repository management
- Repository (GitCode/GitHub) API operations

You have no abilities outside DevOps (no weather, email, documents, PDF, Excel, writing, meetings, voice, etc.).

## 当前职责（任务对话）
你正参与一个**已被分配 Issue** 的需求澄清对话。你的职责：
1. 围绕该 Issue 的需求与用户多轮沟通，澄清关键参数与实现方案；
2. 将澄清结论回帖到对应 Issue（GitCode 评论）；
3. 不实施 / 不部署 / 不写代码——实现由 pipeline（coding/sre）在回帖后执行。

Reply rules:
1. DevOps 主题：分析并简洁中文回复
2. 非 DevOps 话题：礼貌说明只处理 DevOps
3. 绝不写代码、改文件、执行 git 命令
4. 永远用中文回复，不输出内部推理或过程步骤。

SECURITY (MUST FOLLOW):
1. 只使用本 prompt 提供的 GitCode token（USER_GITCODE_TOKEN）。
2. 绝不访问 bot 内部 data/、configs/、sessions.db、user_accounts。
3. GitCode API 403：停止并用政策性话术告知用户需要权限。
4. 绝不把 token 写入文件、日志、评论。
## 用户基本信息
  GitCode login: u1
  飞书 昵称：F
## 场景: task
## 任务背景
仓库: org/repo
Issue: #7
### 任务边界
本对话的职责是【澄清被分配 Issue 的需求并回帖】，不是实施/部署/写代码。
用户选择的部署方式/实现方案属于需求澄清内容，回帖后由 pipeline（coding/sre）执行。
严禁 clone/部署/起服务/修改文件/执行 git 命令。
标题: 优化登录页

## 项目知识基线（repo 全局）
[无项目知识基线] 允许降级分析：可基于 README 与 GitCode API 获取基础信息，相关结论标注 [degraded]。
> 项目级成员知识：{{PROJECT_INFO_DIR}}/member/，存在则用 read tool 读取并遵循。
> 本 Agent 专属知识：{{PROJECT_INFO_DIR}}/agent/chat/，存在则用 read tool 读取并遵循。

## 本次用户消息
hello
## 使用技能
Skills: chat
Skill 文件：{{SKILL_DIR}}/chat/SKILL.md
**必须先执行**：用 read tool 读取该 SKILL.md，并严格执行其中的指令去完成任务（不得跳过读取或凭空猜测）。
```

> 注：1）真实 task 会话中 `### 任务描述`（tasks 表，标题紧随其后）与 `## 对话历史` 会出现（见 §2.1.4 ⑦⑩）。
> 2）A2A 交办规则、回帖 [/RoleName Agent/] 前缀、创建 Issue 流程均已移出 prompt，由对应 skill 承载（§2.1.6）。

## 3. 调用 runtime：两条路径完全一致

### 3.1 runtime_id 解析（共用）

| 步骤 | 逻辑 |
|---|---|
| 1. 用户配置 | `get_chat_config(user_id)` → `chat_agent_session` 行（web 与飞书同源） |
| 2. 未绑定兜底 | `resolve_local_engine()`（本机直连，credential_id=0 → direct） |
| 3. 执行模式 | `runtime_execution_kind(runtime_id)` → DIRECT / REMOTE |
| 4. 远端兜底 | 本机直连行在 queue_remote 下按 backend 名 `_resolve_chat_runtime_id()` 解析在线远端引擎 |
| 5. 无引擎 | 明确返回"无可用引擎"，不隐式跑 opencode |

> 无 task/general 之分——web `chat.py` 与飞书 `handler_async.py` 的 runtime 解析代码**同源**（均有 queue 黑洞兜底）。

### 3.2 执行方式（共用）

| 模式 | web | 飞书 |
|---|---|---|
| direct | `stream_agent(prompt, cwd, session_id, backend) ` (chat.py:321 / :537) | `consume_stream_async(prompt, wsd, oc_sid, ...)` (handler:807) |
| queue | `enqueue_task(agent_session_id=f"chat:{user_id}", runtime_session_id=conv's)` (chat.py:650) | `enqueue_task(agent_session_id=sess, runtime_session_id=oc_sid)` (handler:769) |

**差异点**：仅 agent_session_id 命名与 runtime_session_id 来源不同（详见 §5），引擎调用本身一致。

---

## 4. runtime 工作目录

### 4.1 web（`_get_chat_cwd`, chat.py:12）
```python
path = /home/grape/chat/{user_id}/{conv_id}
```
- **web 侧统一按 conversation 隔离**：每个 conv 独立目录，task 与 general 只是同一目录树下不同 conv_id
- 逻辑基准 `/home/grape` 由远端 worker `_remap_cwd` 映射到各自用户 home

### 4.2 飞书（handler:726）
```python
wsd = WORKSPACE_DIR/feishu/{repository.replace("/","_")}/{sess.split("-")[-1]}
```
- **按 repo + session 特征隔离**（非按 user/conv），`WORKSPACE_DIR=/opt/grape-workspaces`
- task 会话复用同一 repo 的 session 特征目录

> **task/general 对比**：web 下两者目录规则相同（均按 conv_id）；飞书下两者也相同（均按 repo+sess）。**两条路径的 cwd 规则没有 task/general 差别**，差别只在 web vs 飞书的整体 cwd 策略。

---

## 5. 复用 session_id

### 5.1 web（`chat.py`）
```python
runtime_session_id = conv.get("runtime_session_id", "")   # chat_conversations 行
```
- **会话级（conversation）**：每个 conv 独立 opencode session
- 首次为空 → opencode 不带 `--session` 新开；成功后回写 `update_conversation(runtime_session_id=新会话)`
- task / general 都存到同一个 conv 的 `runtime_session_id` 字段

### 5.2 飞书（`handler_async.py`）
```python
sess, history, oc_sid, cc_sid, is_new = get_or_create_session(repo_key, src, repo)
```
- **仓库级（chat_agent_session 按 repo_key）**：同一 repo 的所有对话共用 session
- `oc_sid` 持久在会话中，opencode 带 `--session ses_xxx` 续聊

### 5.3 task/general 差异
| | web | 飞书 |
|---|---|---|
| task | conv 级 session（同 general） | repo 级 session（同 general） |
| general | conv 级 session（同 task） | repo 级 session（同 task） |

> **结论：task 与 general 在该维度无差别**——差异只存在于 web vs 飞书（会话粒度不同）。

### 5.4 会话失效兜底（executor）
- 带 `--session` 启动后 exit≠0 + 无输出（新版 opencode 静默 EOF）→ 判定会话失效 → **不带 session 重跑**
- 成功后通过 `oc_session_id` 回写，替换过期值（chat.py 已去掉 not-runtime_session_id 限制恒回写）

---

## 6. 返回对话框的信息

### 6.1 web `send_message`（:358-369）
```python
add_message(conv_id, "agent", reply)      # 写入 chat_messages
return {"reply": reply, "conversation_id": conv_id}
```

### 6.2 web `send_message_stream`（SSE, :526-621）
事件流：`思考中` → 增量 `生成中`(text) → 最终 `done{text, conversation_id}`；同时 `add_message` 入库 + `_push_reply_to_feishu`（若 feishu 接入）。

### 6.3 飞书 `_process_chat_inner_async`（:819+）
- `add_message` + 返回 `final_text`，另 `update_session(sess, history, "idle", new_oc_session_id, backend)` 持久会话
- web→飞书推送由 `_push_reply_to_feishu` 完成

### 6.4 task 特有返回增强
| 行为 | 说明 |
|---|---|
| 打开时同步 Issue 评论 | `GET /messages/{conv_id}`（chat.py:136）task 会话自动调 `_sync_issue_comments_to_conversation` 补拉 agent 在 Issue 上的评论 |
| 创建/复用会话写上下文 | `_ensure_task_conversation` 写入 `_build_task_context_message` 上下文消息 |
| prompt 注入 | `build_task_chat_prompt` 注入 Issue 标题/任务描述/A2A 规则 |

> **差异**：返回结构（reply/conversation_id / SSE done）task 与 general **相同**；task 额外多了「Issue 评论同步」「上下文消息」这类回话内容增强。

---

## 7. 流程对比（逐步）

### 7.1 Web Chat 通用（general）
```
消息 → send_message(_stream)
  → get_conversation(conv_id), get_messages(history)
  → build_general_chat_prompt(通用 Role + user_info + repo + 项目知识基线 + 技能)
  → runtime 解析（本机直连/远端）
  → direct: stream_agent(cwd=/{user}/{conv}, session=conv.runtime_session_id)
    or queue: enqueue_task(agent_session=chat:{uid}, session=conv.runtime_session_id)
  → add_message(conv, reply) → {reply, conversation_id}
```

### 7.2 Web Chat 任务（task）
```
消息 → send_message(_stream)
  → get_conversation(conv_id, conv_type=task, issue_number=N)
  → build_task_chat_prompt(同一 Role + 场景:task + Issue 背景 + 任务描述 + A2A)
  → runtime/session/cwd → 与 general 相同
  → [进入会话时若打开过] 自动从 GitCode Issue 同步 agent 评论
  → add_message → 返回同 general
```

### 7.3 飞书（task 与 general 同流程，仅 prompt 分支不同）
```
消息 → _process_chat_inner_async
  → get_or_create_session(repo_key) → oc_sid(仓库级 session)
  → _build_feishu_chat_prompt → conv_type? task/build_task_chat_prompt : general
  → runtime 解析（同 web）; cwd=/WORKSPACE_DIR/feishu/{repo}/{sess尾}
  → direct: consume_stream_async / queue: enqueue_task
  → add_message + update_session → 回复
```

---

## 8. 差异归纳

### 8.1 真正的差异（task vs general）
1. **Prompt 内容**（§2.2 有任务描述/场景，§2.1 通用）
2. **会话内容增强**：task 打开时同步 Issue 评论、任务上下文消息
3. 无其他结构性差异

### 8.2 非 task/general、而是 web vs 飞书的差异
1. session 粒度（web conv 级 / 飞书 repo 级）
2. cwd 规则（web `/home/grape/chat/{uid}/{conv}` / 飞书 `WORKSPACE_DIR/feishu/{repo}/{sess尾}`）
3. worker agent_session_id 命名（web `chat:{user}` / 飞书 repo session）

---

## 9. 潜在风险点

1. **飞书 session 粒度大**：同一 repo 所有对话共用 `oc_sid`，任务会话与通用会话互踢上下文（`--continue` 续的是同一会话历史），可能串上下文。
2. **web 首次 conv 无 session**：新 conv `runtime_session_id` 空 → opencode 新开会话（无历史），task 首轮不含先前 Issue 对话记忆（靠 prompt 注入 Issue 上下文弥补）。
3. **task 评论同步依赖 gitcode token**：`_sync_issue_comments_to_conversation` 需要仓库 token，缺失时静默跳过。
4. **executor 会话失效兜底**：依赖 parsed_count≥1 判定，极端下可能把「进程异常但无输出」误判为会话失效而重跑。

---

## 10. 参考

- 代码：`src/api/routes/chat.py`、`src/im/handler_async.py`、`src/agent/executor.py`
- 设计：`docs/new-version2/chat-prompt-overview.md`
- 相关：`docs/new-version2/skill-directory-categorization-plan.md`（技能目录重组）、`docs/new-version2/skills-standalone-repo-build-time-pull.md`