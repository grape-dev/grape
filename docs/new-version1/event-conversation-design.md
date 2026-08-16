# Event 对话窗口设计（Feedback 重构：与 DevOps Agent 就事件对话）

> 版本：v1.0
> 日期：2026-08-12
> 状态：待评审
> 前置：`user-workflow-overview.md`（§2.7 Event Detail）、`vod-skill-upgrade-plan.md`（§3.0 issue_state 阶段机）、`a2a-queue-refactor-design.md`（A2A 队列化）、`concurrency-design.md`（串行槽）
> 触发背景：现状「Feedback」是空壳（只构造 a2a_event 字典返回、不执行），用户期望升级为**以事件为锚点、与该事件所属 devops agent 的多轮对话窗口**，支持：①解释处理逻辑与思考过程；②用户建议沉淀为 project/issue/issue_agent 级共享信息（文件落点，参考 project-info/event.json）；③流程干预（暂停/回退/重新派发）；④仅当 Issue pipeline 未结束时开放评论。

---

## 〇、现状与问题

### 0.1 当前 Feedback 实现（空壳）

| 位置 | 现状 |
|---|---|
| `src/api/static/js/pages/events.js:73` `submitFeedback` | Event Detail 右侧表单（标题+描述）→ `POST /chat/feedback` |
| `src/api/routes/chat.py:489` `submit_feedback` | 查 `events` 行后**仅构造 `a2a_event` dict**（`type:"user_feedback", target:"vod", payload:{source_seq, feedback_type, comment, user}`）并 return，**从不派发执行** |
| 前端反馈 | 仅 `showToast('反馈已提交')` + 清空表单，无任何 agent 处理结果 |

**核心矛盾**：用户想要"与 agent 对话"，现状既没有对话存储、没有真正执行、也没有回复回显，且 `target` 写死为 `vod`，与事件实际所属 agent 无关。

### 0.2 缺失的能力

1. **对话存储**：无事件↔用户↔agent 的消息表；
2. **对话执行**：用户消息 → 该事件 agent 运行 → 回复，全链路缺失；
3. **共享信息（记忆）**：无 project/issue/issue_agent 级知识沉淀（工作区文件）与注入机制；
4. **流程干预**：无受控的"暂停/回退/重新派发"入口；
5. **门禁**：无"Issue 流程结束则关闭评论"的判定。

---

## 一、目标

1. **对话窗口**：用户在 Event Detail 右侧可对该事件的 agent 多轮提问/反馈，agent 基于事件上下文作答并回显；
2. **解释能力**：agent 说明"为什么这么处理、思考过程"（需求 1）；
3. **共享信息**：用户建议由 agent 判断是否沉淀为 project/issue/issue_agent 级共享知识（工作区文件，参考 project-info/event.json 存放），后续任务自动参考（需求 2）；
4. **流程干预**：用户发现需求理解有误时，agent 可执行"暂停下游 → 回退阶段 → 重新梳理澄清 → 重新派发"，回到正常 pipeline（需求 3）；
5. **门禁**：关联 Issue 的 pipeline 结束（`issue_state.stage='done'` / Issue 关闭）后关闭评论（需求 4）。

---

## 二、现状代码定位（对话复用的既有能力）

| 能力 | 位置 | 复用点 |
|---|---|---|
| 事件数据 | `events` 表（seq / repo_key / issue_id / agent_type / result / agent_logs） | 对话锚点与上下文 |
| 事件详情 API | `src/api/routes/events.py:8` `get_event_detail`（含 event.json archive 结构化产出） | 前端对话页数据源 |
| Issue 阶段机 | `issue_state`（`db_setup.py:293`）、`src/api/session_store.py:57` `set_issue_stage` | 门禁判定 + 阶段回退 |
| Pipeline 阶段 | `src/api/routes/pipeline.py:130` `stage`（vod/issue/coding/qa/sre/done） | 展示与门禁 |
| Agent 执行 | `src/api/event_pipeline.py` `_execute_agent` / `_enqueue_pipeline_task` / `enqueue_task` | 对话触发的 agent 运行走同一链路 |
| Agent↔Agent | `src/api/routes/webhook.py:69` `a2a_endpoint`（bot token 鉴权 + `set_issue_stage`） | 澄清后重新派发 |
| 任务取消 | `src/runtime/service.py` `cancel_task` | 暂停下游（queue 模式） |
| Prompt 构建 | `src/prompt_builder.py` `build_prompt` | 注入共享知识 |
| SSE/轮询回复 | `src/api/routes/chat.py`（chat send/stream） | 对话回复展示范式 |

---

## 三、方案设计

### 3.1 数据模型（新增 2 张表）

**`event_thread`（对话线程）**

```sql
CREATE TABLE IF NOT EXISTS event_thread (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    anchor_seq  INTEGER NOT NULL,           -- 锚点事件 seq（对话对象 = 处理该事件的 agent）
    repo_key    TEXT NOT NULL,
    issue_id    TEXT DEFAULT '',            -- 关联 Issue（如 '#5'；无关联事件为 ''）
    agent_type  TEXT NOT NULL,              -- vod/issue/coding/qa/sre
    role        TEXT NOT NULL,              -- user / agent
    content     TEXT NOT NULL,
    created_by  TEXT DEFAULT '',
    created_at  TEXT DEFAULT (datetime('now')),
    INDEX idx_event_thread_anchor (anchor_seq)
);
```

**`agent_shared_knowledge`（共享信息索引；正文=工作区文件，见 3.1.1）**

```sql
CREATE TABLE IF NOT EXISTS agent_shared_knowledge (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    scope       TEXT NOT NULL,              -- project(仓库) / issue / issue_agent
    repo_key    TEXT NOT NULL,              -- 所属仓库（知识均在仓库范围内）
    agent_type  TEXT DEFAULT '',            -- scope=issue_agent 时必填
    issue_num   INTEGER DEFAULT 0,          -- scope=issue / issue_agent 时必填
    file_path   TEXT NOT NULL,              -- 知识正文文件（相对 WORKSPACE_DIR，见 3.1.1）
    status      TEXT DEFAULT 'accepted',    -- accepted / rejected
    source_seq  INTEGER DEFAULT 0,          -- 来源事件 seq
    created_by  TEXT DEFAULT '',
    created_at  TEXT DEFAULT (datetime('now'))
);
```

### 3.1.1 知识落点（文件，参考 project-info / event.json 存放）

**原则**：知识正文以**工作区文件**为唯一正体（对齐 `project-info`、`event.json` 的存放与注入/交付机制），DB 表仅做索引/状态/审计。

**scope → 文件路径映射**（`WORKSPACE_DIR/{owner}/{repo}` 下）：

| 知识 scope | 文件路径 | 可见范围 | 参考现有 |
|---|---|---|---|
| **project（仓库）** | `{owner}/{repo}/project-info/knowledge.md`（或同层 `knowledge/`） | 全仓库所有 agent、所有 issue | `project-info/` |
| **issue** | `{owner}/{repo}/issues/{n}/knowledge.md` | 该 issue 所有 agent | `issues/{n}/event.json` |
| **issue_agent** | `{owner}/{repo}/{agent}/issues/{n}/knowledge.md` | 仅该 agent 处理该 issue | `{agent}/issues/{n}/` workdir |

**与现有机制对齐**：
- **注入**：prompt 新增 `_build_knowledge_section(repo_key, agent_type, issue_num)`，按 scope 读对应文件，拼"共享知识"段落（与 `_build_project_info_section` / `_build_history_file_guide` 并列）；**注入 = project + issue + issue_agent 三者合并**（范围从大到小）。
- **远端交付**：`enqueue_task` 增加 `knowledge_files` 快照（参照 `project_info_files`），worker 侧 materialize 到 cwd 并替换 `{{KNOWLEDGE_DIR}}` 占位符（参照 `.project-info` 机制，`runtime/service.py:508-519`）。
- **生命周期**：issue 级知识随 issue 目录存在/清理；project 级随仓库托管存在。

**共享信息语义**：
- `scope=project`：该仓库所有 agent、所有 issue 的后续任务注入（如"此仓库 PR 走内部评审"）；
- `scope=issue`：该 issue 各阶段 agent 的后续任务注入（如"该 issue 用户澄清：验收标准 = 接口可跑通"）；
- `scope=issue_agent`：仅该 agent 处理该 issue 时注入（如"VOD 对该 issue 的口径：按场景聚类"）；
- 仅 `status='accepted'` 注入 prompt。

### 3.2 对话执行链路（核心）

新端点（用户登录 `get_current_user`）：

```
GET  /api/events/{seq}/thread        → 对话历史 + 门禁状态 + 事件上下文
POST /api/events/{seq}/chat          → 发送消息，SSE/轮询返回 agent 回复
POST /api/events/{seq}/knowledge     → agent 写入共享知识（bot token）
POST /api/events/{seq}/flow/reset    → agent 流程控制（bot token）
```

`POST /api/events/{seq}/chat` 服务端流程：

1. 取事件 `seq` → 得 `repo_key / issue_id / agent_type`（**对话对象 = 事件所属 agent**，替代现写死 `vod`）；
2. **门禁**（见 3.5）：`issue_state.stage=='done'` 或 Issue 已关闭 → 409"该 Issue 流程已结束，评论已关闭"；
3. **会话复用**（见 3.2.1）：复用该 agent 的 **issue 级会话**（`issue_agent_session.runtime_session_id`），无关联 Issue 时回退 repo 级会话——不新建空白会话；
4. 组装 prompt（见 3.2.2）：用户消息 + **对话 skill 指令（SKILL.md）** + 事件上下文（`event.json` 结构化产出 + `agent_logs` 摘要 + 当前 stage/owner）+ `event_thread` 最近 N 条历史 + 已 accepted 的共享知识；
5. 复用执行链路运行该 agent（`_execute_agent` 直接模式 / `enqueue_task` 队列模式，随当前执行模式与绑定 runtime），注入上述 `runtime_session_id`；
6. agent 回复写入 `event_thread`（role=agent），并将新的 `oc_session_id` 持久化回 issue 会话（`update_issue_session`），SSE/轮询回传前端。

### 3.2.1 会话复用（沿用 agent 的 issue 会话）

**原则**：对话必须延续 agent 对该 issue 已有的 LLM 上下文，否则 agent"失忆"，无法基于之前的分析作答、也无法判断"理解有误"。

- **有关联 Issue**：`sess = get_or_create_issue_session(repo_key, agent_type, issue_id)`（`session_store.py:278`），取 `sess["runtime_session_id"]` 作为本次对话的 opencode session；结束后 `update_issue_session(sess["id"], runtime_session_id=new_oc_session_id)` 持久化（与 `_execute_agent` 现有做法一致，必要时镜像到 `devops_agent_session`）。
- **无关联 Issue（push/#0）**：回退复用 `devops_agent_session.runtime_session_id`（repo 级），与 `_execute_agent` 的 fallback 语义一致。
- **效果**："对话 → 重新梳理 → 澄清 → 再派发"全程共享同一上下文，agent 记得整个来龙去脉。

### 3.2.2 对话 Skill（agent 按 skill 与用户对话）

**原则**：仓库是 skill 驱动设计（每个 agent 行为由 SKILL.md 定义）；对话行为（解释决策、判断沉淀、判断流程干预、硬约束）应固化为一个通用 skill，而非散写在 prompt 组装代码里。

**新增 `skills/event-dialogue/SKILL.md`（或 `agent-feedback`）**：

```markdown
# Event 对话（Event Dialogue）
## 目标
就指定事件与用户展开多轮对话，帮助用户理解 agent 的处理并接收反馈。
## 执行步骤
1. 先读事件上下文（结构化产出 actions/conclusion/artifacts/next_step + 关键 agent_logs），
   向用户解释"为什么这样处理、思考过程"，用中文简洁回答；
2. 用户提出建议/意见时，判断是否值得沉淀为共享信息：
   - 值得 → 调 POST /api/events/{seq}/knowledge（BOT_GITCODE_TOKEN 鉴权），
     按"影响范围"判定 scope 并写入对应工作区文件（见 3.1.1）：
       project（仓库级：影响该仓库所有 agent/issue）
       issue（该 issue 级：影响该 issue 各阶段 agent）
       issue_agent（仅本 agent 处理该 issue 时参考）
     status=accepted，并回复"已采纳为 {project|issue|agent} 级共享知识"；
   - 不值得 → 回复"暂不采纳"+ 理由（status=rejected 备查）；
3. 用户指出对需求理解有误、需要重新梳理时（如 pipeline 已在 coding 阶段但 VOD 理解仍有问题）：
   - 先与用户澄清确认意图；
   - 确认后调 POST /api/events/{seq}/flow/reset（回退 vod）→ 与用户继续澄清 → 澄清完成后 A2A 交办 issue，走正常流程；
4. 不发起流程干预时，仅对话，不改任何状态。
## 硬约束
- 仅对话 / 沉淀共享知识 / 流程控制，不修改代码、不创建 PR、不直接改动 Issue 状态；
- 不泄露 token / 内部数据；不越权调用非对话端点。
```

**注册与注入**：
- 对话 skill **跨全部 devops agent（vod/issue/coding/qa/sre）通用**：注册时 `agent_type` 置空或用通用标记，对话链路按名直接加载 `skill_path`，**不走 agent_type 过滤**；
- 对话 prompt 由专用 `build_conversation_prompt` 组装（见 3.2 步骤 4），对话 skill 是"会话元技能"，不替代 agent 的业务 skill（如 vod 的 `vod` skill 仅作背景）。
- **收益**：行为一致、改 SKILL.md 即可调对话策略、与既有 skill 管理（官方/仓库/个人）打通。

### 3.3 三种对话能力

#### ① 解释型（为什么这么处理）

- prompt 注入：`actions / conclusion / artifacts / next_step / issue_summary` + 关键 agent_logs；
- agent 解释决策过程，回复仅写线程，不改任何状态。

#### ② 建议 → 共享信息（文件落点 + DB 索引）

- 用户提建议 → agent 判断是否值得沉淀；
- 值得：调 `POST /api/events/{seq}/knowledge`（带 `BOT_GITCODE_TOKEN`），服务端按 `scope` 写入对应**工作区文件**（见 3.1.1）并登记 `agent_shared_knowledge` 索引（`status=accepted`），回复"已采纳为 {project|issue|agent} 级共享知识"；
- 不值得：回复"暂不采纳" + 理由（登记 `status=rejected` 备查）；
- `prompt_builder` 新增 `_build_knowledge_section(repo_key, agent_type, issue_num)`，**合并注入** project + issue + issue_agent 三级 accepted 知识：
  ```
  ### 用户反馈沉淀的共享知识（仅 accepted）
  [project 级] <content>
  [issue 级]   <content>
  [agent 级]   <content>
  后续任务应遵循以上知识，除非用户当前明确推翻。
  ```
- **远端交付**：`enqueue_task` 增加 `knowledge_files` 快照（参照 `project_info_files`），worker 侧 materialize 到 cwd 并替换 `{{KNOWLEDGE_DIR}}` 占位符，使远端 worker 也能读到知识文件。

#### ③ 流程干预（暂停/回退/重新派发）

用户："VOD 对这个需求的理解仍有问题" → VOD agent 确认后调用受控端点 `POST /api/events/{seq}/flow/reset`（bot token 鉴权），服务端封装为**原子流程**：

```
1) cancel：取消该 issue 在 agent_tasks 中 queued/dispatched/running 的任务
   （service.cancel_task，按 repo_key+issue_id 过滤；直接模式线程无法强杀，
    仅能阻断后续阶段，见 §六风险）
2) 回退：set_issue_stage(repo_key, issue_id, 'vod', 'vod')   → pipeline 重置回 VOD
3) 重新梳理：VOD agent 继续与用户多轮澄清（走对话链路）
4) 澄清完成：A2A 交办 issue（set_issue_stage 'issue' + 触发 issue agent）→ 正常流程
```

> **安全约束**：`flow/reset` 仅接受 `BOT_GITCODE_TOKEN`（与 `a2a_endpoint` 同款鉴权），用户不可直接调用；服务端对回退目标做白名单（仅允许回退到更早阶段，禁止越级/乱写 stage）。

### 3.4 前端（`events.js` renderEventDetail）

Event Detail 右侧「Feedback」区块 → **双区布局（右=过程，左=结果）**：

- **右栏「💬 与 {agent} 对话」（会话本体/过程）**：
  - 完整消息列表（user/agent 气泡，时间戳，`role=user` 与 `role=agent` 分色）；
  - 输入框 + 发送；agent 回复流式展示（复用 chat SSE）；
  - 顶部门禁状态条：`✔ 评论开放中` / `✖ 该 Issue 流程已结束，评论已关闭`（done 时隐藏输入框、禁用发送）；
- **左栏「💬 对话产出」（结果摘要，与右栏不重复）**，随对话实时更新：
  - **知识沉淀**：本次对话新增的 `agent_shared_knowledge` 条目（scope project/issue/issue_agent + 文件路径 + accepted/rejected）；
  - **流程操作**：对话触发的 `flow/reset` 回退、任务取消（暂停）、A2A 重新派发等动作留痕；
  - **结论**：agent 与用户达成的明确结论（agent 结构化输出）。
- 保留"发起新话题/清空会话"等轻量操作（可选）。

### 3.5 门禁规则（需求 4）

- 开启条件：`issue_state.stage != 'done'` 且 Issue 未关闭；
- `stage=='done'`（`session_store.get_issue_stage` 判定；cleanup 已以 `stage='done'` 作为终态依据）或 GitCode issue state=closed（可后续叠加 API 校验）→ 关闭评论；
- 前端隐藏输入框 + 后端 409 双重门禁。

### 3.6 并发与串行

- 同一 `anchor_seq` 线程的对话消息串行处理（复用 D9 串行槽语义：同 issue 的对话触发的 agent 任务互斥，避免多轮上下文错位）。

---

## 四、改动点清单

| # | 文件 | 改动 | 说明 |
|---|---|---|---|
| 1 | `src/api/db_setup.py` | 新增 `event_thread` / `agent_shared_knowledge` DDL | 数据模型 |
| 2 | `src/api/routes/events.py` | 新增 `GET /events/{seq}/thread`、`POST /events/{seq}/chat` | 对话端点（用户鉴权 + 门禁 + 组装 prompt + 执行 + 回写线程） |
| 3 | `src/api/routes/events.py` 或新文件 | 新增 `POST /events/{seq}/knowledge`、`POST /events/{seq}/flow/reset` | agent 端点（bot token；写共享知识 / 流程控制） |
| 4 | `src/api/routes/chat.py:489` | `submit_feedback` 废弃（或改造为转发对话入口） | 移除空壳 |
| 5 | `src/prompt_builder.py` | 新增 `build_conversation_prompt`（对话 skill 指令 + 事件上下文 + 线程历史 + 共享知识）；新增 `_build_knowledge_section`（读知识文件、三级合并注入）；`enqueue_task` 增 `knowledge_files` 交付（`runtime/service.py`） | 需求 2 + 3.1.1 + 3.2.2 |
| 6 | `src/api/event_pipeline.py` / `src/runtime/service.py` | 流程控制封装：`cancel_tasks_for_issue` + `set_issue_stage` + 可选派发 | 需求 3 |
| 7 | `src/api/session_store.py`（复用） | 对话执行复用 `get_or_create_issue_session` + `update_issue_session`（不新建会话） | 3.2.1 |
| 8 | `src/api/static/js/pages/events.js` | `renderEventDetail` 右侧改对话窗口 + 门禁 UI + 流式回复 | 前端 |
| 9 | `skills/event-dialogue/SKILL.md`（新增） | 对话行为 skill；注册进 `skill` 表（跨 agent 通用，不走 agent_type 过滤） | 3.2.2 |
| 10 | `tests/`（新增 `test_event_conversation.py`） | 见 §五 | 覆盖 |

### 关键实现注意

- 对话触发的 agent 执行**不新增事件行**（对话不是 pipeline 事件，仅写 `event_thread`）；若需留痕可加 `role=system` 审计消息。
- `flow/reset` 的 cancel 与回退需在 `db_lock` 内原子执行。
- 门禁判定统一收敛到一个 helper（`_thread_gate(repo_key, issue_id)`），前端展示与后端校验共用同一语义。
- **会话复用是硬要求**：对话必须走 `get_or_create_issue_session` 的 `runtime_session_id`（无 Issue 回退 repo 级），结束回写新 `oc_session_id`；否则 agent 无上下文记忆。
- **对话 skill 为元技能**：加载时不覆盖 agent 业务 skill 上下文，仅以 SKILL.md 指令约束对话行为。

---

## 五、验证方案

### 5.1 单元测试（新增 `tests/test_event_conversation.py`，参考 `test_tasks.py` 隔离写法）

| # | 用例 | 前置 | 断言 |
|---|---|---|---|
| T1 | 门禁开启 | issue_state.stage='coding' | `_thread_gate` 通过；`POST chat` 200 |
| T2 | 门禁关闭 | issue_state.stage='done' | `_thread_gate` 拒绝；`POST chat` 409；前端隐藏输入（UI 测试另计） |
| T3 | 对话回复写线程 | 用户发消息 | `event_thread` 新增 user + agent 两行（role 正确，anchor_seq 正确） |
| T4 | 共享知识注入（三级合并） | project/issue/issue_agent 各有 1 条 accepted 知识文件 | `_build_knowledge_section` 输出含三级全部知识；rejected 不注入 |
| T5 | 共享知识写入（文件 + 索引） | agent 调 `POST knowledge {scope:'issue'}` | 对应工作区文件写入（`{owner}/{repo}/issues/{n}/knowledge.md`）+ `agent_shared_knowledge` 新增行（scope/file_path/source_seq） |
| T5b | scope→文件落点 | 分别写 project / issue / issue_agent 三种 scope | 文件落在 3.1.1 对应路径 |
| T6 | 流程回退 | stage='coding' + 有 queued task | `POST flow/reset {to:'vod'}` → task 取消 + stage 回 vod + 记录；无权限 token → 403 |
| T7 | 对话对象 = 事件 agent | event.agent_type='issue' | 对话端点以 issue agent 执行（非写死 vod） |
| T8 | 串行 | 同线程并发两消息 | 按序处理（不交错上下文） |
| T9 | 会话复用 | issue 已有 `issue_agent_session`（runtime_session_id='ses_xxx'） | 对话执行沿用 'ses_xxx'，结束后 `update_issue_session` 回写新 id；不新建空白会话 |
| T10 | 对话 skill 注入 | 已注册 event-dialogue skill | `build_conversation_prompt` 输出含 event-dialogue SKILL.md 指令段落 |

### 5.2 端到端

1. 对未结束 Issue 的某事件打开对话 → 问"为什么这样处理" → 收到 agent 解释（流式）；
2. 提建议 → agent 采纳为 project 级知识 → 触发该 repo 新事件 → 新事件 prompt 含该知识；
3. 指出需求理解有误 → agent 执行 `flow/reset` → pipeline 回 vod → 澄清后再派发 issue → 正常走完；
4. 将 Issue 置 done → 对话输入框消失、后端拒绝。

### 5.3 回归范围

- 既有 `test_tasks.py` / `test_pipeline.py` / `test_runtime*.py` / `test_chat_handler.py` 全量通过；
- Event Detail 页面其余功能（结构化产出、Event Info、返回）不回归。

---

## 六、影响与风险

| 项 | 说明 |
|---|---|
| 行为变化 | Event Detail 右侧 Feedback 表单 → 对话窗口；`submit_feedback` 废弃（前端无旧入口残留） |
| 能力边界 | 直接（direct）模式运行中的 agent 线程**无法强杀**，"暂停 coding"只能取消队列任务 + 回退 stage 阻断后续；队列模式可真正 cancel |
| 误采纳风险 | 共享知识由 agent 自主判定，建议配套"人工可删除/驳回"管理入口（`agent_shared_knowledge` 可直接改库或加管理 API） |
| 流程回退强度 | `flow/reset` 是强操作：agent 须明确确认用户意图（prompt 约束 + 服务端白名单/校验） |
| 并发 | 同线程串行处理，避免上下文错位；与既有 D9 串行槽语义一致 |
| 权限 | 对话=用户登录；共享信息写入/流程控制=bot token，用户不可越权改库 |
| 回滚 | 新增端点/表不触碰既有事件链路；`submit_feedback` 保留旧行为可回退 |

---

## 七、附带观察（非本次范围）

- **共享知识的生命周期**：采纳后如何验证有效、如何失效/迭代（如被后续反馈推翻），可另文设计；
- **GitCode issue 状态门禁**：`stage='done'` 本地判定为主，是否叠加 GitCode API 校验 issue closed，评估成本后决定；
- **对话上下文上限**：线程历史截断长度、以及是否支持"重新整理上下文"（重置后从事件重讲），可后续优化；
- **多用户协作**：同一事件线程是否允许仓库成员共同参与（当前按登录用户），可扩展。

---

*本文为 Event 对话窗口重构方案，待评审后实施。*
