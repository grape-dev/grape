# Chat 类任务 Prompt 现状分析与优化方案

> 状态：A 档已实施（2026-10-08）；B/C 档暂缓留档
> 生成日期：2026-10-08
> 分支：weekly-06-lyz
> 代码位置：`src/prompt_builder.py`（build_issue_chat_prompt / build_general_chat_prompt）
> 分流点：`src/api/routes/chat.py`（6 处 conv_type 判断）
> 关联文档：`docs/new-version2/chat-prompt-overview.md`、`docs/new-version2/task-vs-general-chat-flow-analysis.md`

---

## 0. 核心决策（v2 更新）

**决策 R1 — 重命名 `build_task_chat_prompt` → `build_issue_chat_prompt`**：
- 本质是**围绕该 Issue 的相关讨论**（咨询状态、评审方案、解读背景、澄清需求），而非"任务澄清"单一路径
- 接受原 `conv_type='task'` 会话维度判定不变（`conv_type` 命名不迁移，避免 DB 变更），仅 prompt 构造函数改名
- `Scene: task` → `Scene: issue` 同步；skill 场景识别信号同步更新

**决策 R2 — 去掉 task 描述，改为注入 Issue 上下文**：
- ❌ 移除 `## Task Description`（`_get_task_description` 查询 tasks/issue 表的 task_description 段）
- ✅ 注入 Issue 完整上下文：`title + body + 事件/评论摘要`（`get_issue_summary` 系），让 Chat 围绕该 Issue 自由讨论
- ✅ Role 从"澄清已分配任务"放宽为"围绕该 Issue 提供讨论"，保留红线（不实现/不部署/不写代码/不改仓库状态）

---

## 1. 现状总览

### 1.1 两条 prompt 构建路径（按会话类型分流）

```
conv_type == 'task'  →  build_issue_chat_prompt()  （原 build_task_chat_prompt，本次改名的目标）
否则                 →  build_general_chat_prompt()  通用会话（自由咨询）
```

分流点在 `chat.py` 6 处（`598/732/1072/1433/1797` 等），飞书入口 `handler_async.py:576` 同样分流。
> 改名仅影响函数名与注入内容，不改变分流条件（conv_type 判定）。

### 1.2 `build_issue_chat_prompt`（原 `build_task_chat_prompt`，prompt_builder.py:880）

> 见 `task-vs-general-chat-flow-analysis.md §2.1.3`，注：历史文档引用的行号（454/555）已偏移，现为 880；函数名将改为 `build_issue_chat_prompt`。

**何时触发（触发链路）**：

`conv_type == "task"` 的会话（chat_conversations 表）发送消息时走此 prompt。task 会话由以下入口创建/复用：

| 入口 | 位置 | 说明 |
|------|------|------|
| **Web 需求中心「去对话」** | `chat.py` `/conversations/ensure-issue` → `_ensure_task_conversation`（issues_center.py:144） | 用户从需求中心点某个 Issue 的「去对话 ↗」→ 查找/新建 `Issue #N` 会话（conv_type=task）→ 发消息即走 issue prompt |
| **任务分配自动建档** | `ensure_issue_task`（issues_center.py:181） | notify 分配任务时通过 `_ensure_task_conversation(write_context_on_reuse=True)` 预建 task 会话并写入上下文消息 |
| **飞书 `/task` 会话** | `handler_async.py:466/474` | 飞书 /task 命令按 Issue 编号创建/复用 conv_type=task 会话 |
| **task 会话命令（done/resume 等）** | `chat.py:232/553` | 仅 task 会话才响应 done/resume 指令 |

**6 个 `build_issue_chat_prompt`（原 task）调用点**：`chat.py:598/732/1072/1433/1797`（Web send/send_stream 等）+ `handler_async.py:583`（飞书）。

> 核心信号：**会话绑定了一个具体 Issue（conv_type=task + issue_number）的对话，而非自由咨询**。
> 一旦 conv_type 不是 task（general），则走 `build_general_chat_prompt`。

**组装顺序（v2 目标：去 task 描述 + 注入 Issue 上下文）**：

```
## Role Definition          ← build_issue_role_definition()
└─ DevOps 助手领域声明
└─ ## Current Role (issue conversation)
     · 职责：围绕该 Issue 进行讨论（咨询/评审/澄清/解读背景）
     · 可：查询 Issue/PR 状态、回答技术问题、评审方案、
           澄清需求后将结论回帖到 Issue（用户身份）
     · 禁止：实现/部署/写代码/git 命令/修改仓库状态
└─ SECURITY（只用注入 token / 禁读内部数据 / 403 即停 / 不写 token）
└─ Image analysis rule（图片分析例外）

[user_info]                ← 飞书侧个人身份（可选）

## Scene: issue
## Issue Context
Repository: {repo_key}
Issue: #{issue_number}
Title: {conv.title}
Body: {issue.body}          ← 新增：Issue 原文 body（v2）

Runtime engine: {engine} (runtime_id={runtime_id})   ← 可选

{issue_summary}            ← get_issue_summary（事件/评论/进展摘要，可选）

## Issue Processing Summary ← 新增：前序 Agent 处理摘要（可选）
## Issue Agent Progress     ← 含 [XXX Agent] 的进展（可选）
## Chat History             ← has_valid_session? 最近3条 : 最近10条（可选）

## Current User Message
{user_text}

## Current Skill: chat
Skill file: {{SKILL_DIR}}/chat/SKILL.md
**MANDATORY**: Read this SKILL.md ...（读并遵循）
```

> ⚙️ 移除部分：`## Task Description`（`_get_task_description` 与 `_build_task_context_message` 的 task_description 段，见 §3.5 迁移）。

### 1.3 `build_general_chat_prompt`（通用会话，prompt_builder.py:788）

```
## Role Definition          ← build_chat_role_definition()（通用角色，含"创建Issue转链表"导向）
[Runtime engine: {backend}] 或 [Runtime engine: {engine_name}]   ← 可选
[user_info]                 ← 可选
IMPORTANT: You are working in repository: {repo_key}   ← 可选

Repository event context:   ← 事件摘要（可选）
{summary}

[## Issue Agent Progress]   ← 可选
[## Chat History]           ← 可选

[## Current Skill: {skill}] ← skill_name/skill_id 解析
 ...
User (in {repo_key}): {message}
Assistant:                  ← _CHAT_PROMPT_SUFFIX
```

### 1.4 配套 skill：独立仓库 `grape-skills`，两级结构（chat/im/*）

> ⚠️ 官方技能已迁出本仓库，由 `scripts/sync_skills.sh` 从独立 `grape-dev/grape-skills` 仓库铺入 `skills/`（gitignored 运行时目录）。
> 本地开发对应仓库：`D:\Developer\grape-dev\grape-skills`，chat 相关位于 `grape-skills\skills\chat\im\`。

**当前结构（已分级/CLI 化）：**

```
grape-skills/skills/chat/
└── im/
    ├── chat/            ← official skill_name="chat"（skill_path="chat/im/chat"）
    │   ├── SKILL.md
    │   └── references/{implementation-start, inquiry-handling, task-handling, vcs-api-reference}.md
    ├── chat-cli/        ← NEW official skill_name="chat-cli"（skill_path="chat/im/chat-cli"）
    │   ├── SKILL.md
    │   └── references/{grape-cli-reference, task-feedback, task-inquiry, task-review}.md
    └── im-notify/       ← official skill_name="im-notify"（skill_path="chat/im/im-notify"）
```

**生产 DB 实测（skill 表）：**

| id | skill_name | agent_type | skill_path | scope |
|----|-----------|-----------|-----------|-------|
| 4314 | im-notify  | chat | chat/im/im-notify | official |
| 4315 | chat       | chat | chat/im/chat   | official |
| 4367 | chat-cli   | chat | chat/im/chat-cli | official |

**chat skill 内容（`grape-skills/skills/chat/im/chat/SKILL.md`）：**
- 场景识别表：**issue**（信号 `场景: issue`，v2 起替代 `场景: task`）→ 讨论/澄清流程文件；**inquiry**；**implementation-start**
- 安全硬约束：只用注入 token / 禁读 data、configs、sessions.db / 403 即停 / token 不落盘
- 操作边界表：
  - 查询 Issue/PR/commit/技术问答：✅ 可
  - 创建 Issue（需求引导明确后）：✅ 可（注明提交人）
  - 发澄清评论：✅ 可
  - 修改 GitHub/GitCode 状态、标签、负责人：❌ 禁止
  - 泄露 token/密码/AK/SK：❌ 禁止

> chat-cli（4367）：新 CLI 化技能，references 含 task-inquiry / task-review / task-feedback，
> 与老 chat skill 的澄清+回帖流程需明确职责边界。

---

## 2. 现状问题分析（优化切入点）

| # | 问题 | 证据 | 影响 |
|---|------|------|------|
| P1 | **task prompt 结尾缺 `\nAssistant:` 生成 token** | general 有 `_CHAT_PROMPT_SUFFIX`（`"\nAssistant:"`），task **没有** | task 输出结束符依赖 executor 兜底；直出 LLM（某些 worker 路径）可能不稳定 |
| P2 | **两份 role definition 90% 重复** | `build_chat_role_definition` 与 `build_task_role_definition` 仅"当前职责 + 创建Issue导向"不同 | 维护漂移风险；后续改措辞需同步两处 |
| P3 | **task prompt 缺少 `repo_key`/平台上下文冗余** | task 有 `Repository: {repo_key}`，但通用段（VCS token env 名、平台名）由 role 内插 | 语义够但可读性一般；audit 时难一眼定位 |
| P4 | **issue 会话 skill 固定 `chat`，无 skill_name/skill_id 注入** | 目标函数 `build_issue_chat_prompt` 签名没有 skill 参数 | 场景沿用 chat skill（合理），但若需按 Issue 场景定制流程文件时缺少钩子 |
| P5 | **`## Current User Message` 无后缀结束符 + 无"回复格式"约束** | 对比 general 有 `\nAssistant:`；task-handling 虽有流程文件但 prompt 内无显式"按用户语言回复/简洁/聚焦" | 部分模型可能输出流程外内容 |
| P6 | **注释/文档行号与函数名漂移** | `task-vs-general-chat-flow-analysis.md` 引用 `:454/:555`，实际 `:880`；且函数将改名 `build_issue_chat_prompt` | 排查者误读，改名后旧文档名失效 |
| P7 | **skill 内场景识别依赖 `场景: task` 字符串** | SKILL.md `| task | prompt 含「## 任务分配 · Issue #N」或 `场景: task` |` | 改名 `Scene: issue` 后需同步 skill 判定（脆耦合） |
| P8 | **history 注入策略可配置性弱** | `_append_chat_history` 硬编码 3/10 条、500 字符 | 长任务上下文不足或冗余 |
| P9 | **skill 路径与两级结构脱节** | `build_issue_chat_prompt` 硬编码 `{{SKILL_DIR}}/chat/SKILL.md`（prompt_builder.py:930）；官方 `chat` skill 的 `skill_path` 实为 `chat/im/chat`；新 `chat-cli`（im/chat-cli）存在 | worker 物化后 `.skills/` 下的落地目录名若与 `{{SKILL_DIR}}/chat` 不一致，skill 读取失败 / 读错 skill；chat 与 chat-cli 职责易混 |
| P10 | **issue 会话 skill 选择未显式** | 目标函数固定 chat skill，未声明用 `chat` 还是 `chat-cli` | 澄清/讨论式 vs CLI 操作式若选错 skill，流程走偏 |
| P11 | **只注 task_description、缺 Issue 原文 body** | 现状注入 `## Task Description`（用户建需求表单字段），**不含 Issue body**；用户咨询/评审时无法看到完整 Issue 内容 | Chat 无法正确回答"这个 Issue 到底是什么/讨论了什么"；咨询、评审场景信息不足（v2 决策 R2 修复） |

---

## 3. 优化方向（分档）

### 3.1 A 档：低风险微调（直接可做）

- **A0（决策 R1 落地）**：`build_task_chat_prompt` → `build_issue_chat_prompt` 重命名（函数 + 5 处 chat.py + 1 处 handler_async + tests + docs），含 `Scene: task → Scene: issue`
- **A1**：目标函数结尾补 `_CHAT_PROMPT_SUFFIX`（`"\nAssistant:"`），与 general 对齐稳定收尾
- **A2**：`## Current User Message` 加显式回复约束一行：
  `Reply in the same language the user used, concise, aligned with your current Issue discussion.`
- **A3**：修正 `docs/new-version2/task-vs-general-chat-flow-analysis.md` 的行号引用 → 880 与函数新名
- **A4**：抽取公共 role 基底（`_build_base_chat_role`），general/issue 各自叠加差异段，杜绝措辞漂移
- **A5（决策 R2 落地）**：去掉 `## Task Description`（`_get_task_description` 调用链），改为注入 Issue 上下文（title + body + summary）

> ⏸ B 档 / C 档 **暂不实施**（2026-10-08 评审）：以下保留作未来迭代参考，本期仅做 A 档（重命名 + 去 task 描述 + 围绕 Issue 讨论）。

### 3.2 B 档（暂缓）：结构优化（留档）

- **B1**：issue prompt 增加 `## Issue Boundaries`（显式列出 可咨询/可评审/可澄清回帖/禁实现/禁部署/禁写代码），与 skill 解耦
- **B2**：统一"项目知识基线"注入策略：issue/general 都从 `project-info` 基线段注入
- **B3**：skill 场景识别改为结构信号：prompt 顶部 `## Scene: issue`，SKILL.md 优先读显式字段
- **B4**：`_append_chat_history` 支持可配置窗口（后续从 config/meta 读取）
- **B5**：**skill 路径解析对齐两级结构**：不再硬编码 `{{SKILL_DIR}}/chat/SKILL.md`，改用 `resolve_skill_id('chat')`/`get_skill_path()` 得真实 `skill_path`
- **B6**：**issue 会话 skill 显式化**：conv 记录 skill_name（chat vs chat-cli），prompt 注入实际 skill 名
- **B7**：注入 Issue 原文 body（§1.2 组装顺序已列）——若 summary 已含全量正文可复用，否则从 events/VCS 拉取

### 3.3 C 档（暂缓）：能力扩展（留档）

- **C1**：issue prompt 支持 `skill_name/skill_id` 参数，允许场景自定义流程文件
- **C2**：注入 Issue 处理进度查询能力（prompt 加"可用工具清单"段：查 Issue 状态/事件流）
- **C3**：讨论/澄清结论的结构化输出（结论先 JSON 后回帖，减少无效发帖）
- **C4**：附件/图片结构化注入（`attachments: list` 参数）

---

## 3.4 参考 devops-agent prompt 的优化对照

> 对照基准：`build_devops_agent_prompt`（src/prompt_builder.py:560，事件流水线 Agent 用）vs
> `build_issue_chat_prompt`（原 build_task_chat_prompt，src/prompt_builder.py:880，Issue 会话用）。

### 对比差异表

| # | 结构 | devops-agent | chat task | 差距/风险 |
|---|------|-------------|-----------|-----------|
| D1 | **输出契约（收尾结构）** | `_build_output_contract`：Task Completion 二选一（`grape tasks complete` 或 JSON `actions/conclusion/artifacts/next_step/issue_summary`）| **无** | chat 澄清结论没有结构化收尾，回帖质量依赖模型自觉 |
| D2 | **Skill 执行指令强度** | `Execute the instructions directly - do not analyze or propose; just execute.` + `If SKILL.md instructs reading references/**, read them as needed` | 仅 `MANDATORY: Read this SKILL.md ...` | chat 未引导读 references（task-handling.md），可能直接凭名字猜流程 |
| D3 | **Runtime Engine 绑定语义** | 注入 `bound engine: {name}` + `403 is a normal security restriction, should not block subsequent scheduling` | 注入 `Runtime engine: {name}`，无 403 语义说明 | chat 遇 403 可能卡住/误停 |
| D4 | **附件/图片注入** | `_inject_attachments_by_role`（vod/issue/coding 分级）| 不入参；仅图片走 `_understand_image` 拼在 prompt 尾 | chat 不能注入附图上下文；多图/附件无结构 |
| D5 | **历史事件规模提示** | `This repository has {N} past events recorded.` | 无 | chat 无全局事件上下文感知 |
| D6 | **关联仓库（多仓）** | `## 关联仓库` + `$GRAPE_LINKED_REPOS/WORKTREES` 指引 | 无 | chat 任务若跨仓无法感知 |
| D7 | **Role 规则编号** | 统一编号 1-7，含「用用户语言回复」「MEDIA EXCEPTION」| issue role 松散编号，缺语言跟随规则 | 用户用英文问却可能被回中文 |
| D8 | **场景进阶选项（环境变量）** | `## Scene Custom Options` → `printenv GRAPE_WORK_BRANCH` | 无 | chat 不感知工作分支/自定义配置 |
| D9 | **子任务/多任务上下文** | `## Subtask Context` / `## Multi-Task Mode` | 无 | chat 不清楚父/子任务关联 |
| D10 | **A2A 上下文注入** | `## Prior Agent Processing Summary` + `## Task Issue` | 无 | chat 无前置 Agent 处理摘要 |

### 建议借鉴点（并入优化档）

| 借鉴 | 归属档 | 说明 |
|------|--------|------|
| 引入轻量输出契约（chat 专版）| **B** | `## Task Completion`：澄清完成须给出「结论 + 是否回帖 Issue#N」，简化版（不套 devops 的 artifacts 全量）|
| Skill 指令补 references 引导 | **A** | task prompt 的 `## Current Skill` 追加 `If SKILL.md references further files (e.g. task-handling.md), read them before proceeding.` |
| Runtime 403 语义说明 | **A** | task 注入的 `Runtime engine` 追加 `A 403 from an API is a normal security restriction; report to the user, do not retry with other tokens.` |
| 注入 Issue 前置处理摘要 | **B** | task 会话查 `events` 的 Issue 事件流，注入 `## Issue Processing Summary`（仿 D1/A2A）|
| 附件/图片结构化注入 | **C** | `build_issue_chat_prompt` 支持 `attachments: list` 参数，注入 `## User Attachments`（文件名+URL/已分析结果）|
| 回复语言跟随用户 | **A** | issue role 补 `Reply in the same language the user used.`（与 devops 对齐）|
| 事件规模提示 | **B** | task prompt 加 `This Issue has {N} events recorded.`（get_issue_summary 附带计数）|

### 结论
chat task prompt 相对 devops-agent 的最大短板是 **没有输出契约 + 未引导读 references + 缺 Issue 前置处理摘要**。
这三项影响回帖质量与流程衔接，建议优先按上表 A/B 档落地。

---

## 3.5 重命名 + 去 task 描述：迁移清单（决策 R1/R2 实施范围）

| # | 项 | 文件 | 动作 |
|---|----|------|------|
| M1 | 函数定义 | src/prompt_builder.py:880 | `build_task_chat_prompt` → `build_issue_chat_prompt`；`Scene: task` → `Scene: issue`；去掉 `## Task Description` 段与 `_get_task_description` 调用；Role 改 `build_issue_role_definition`（围绕 Issue 讨论），注入 Issue body |
| M2 | Role 定义 | src/prompt_builder.py:363 | `build_task_role_definition` → `build_issue_role_definition`：放宽为「可查询/可咨询/可评审/可澄清回帖」，保留红线 |
| M3 | `_get_task_description` | src/prompt_builder.py:850 | 移除（去 task 描述）；若无其它使用则删除，否则保留兼容注释 |
| M4 | chat.py 调用 | src/api/routes/chat.py:599/733/1073/1434/1798 | import + 调用改 `build_issue_chat_prompt` |
| M5 | 飞书调用 | src/im/handler_async.py:563/576/583 | import + 调用改新名 |
| M6 | 测试 | tests/test_chat_task_scene.py + test_chat_routes.py:171 | 改名；`test_build_task_chat_prompt_injects_task_description` → `test_build_issue_chat_prompt_injects_issue_context`（断言 body/摘要 而非 task_description）；补"咨询/评审"场景断言 |
| M7 | skill 场景信号 | grape-skills/skills/chat/im/chat/SKILL.md + chat-cli | `场景: task` → `场景: issue`；task-handling.md 语义扩展为"围绕 Issue 讨论"（澄清=回帖子路径，咨询/评审=直接回答） |
| M8 | 上下文消息 | issues_center.py `_build_task_context_message`（646）| 消息标题可保留「📋 任务分配 · Issue #N」兼容存量会话；prompt 注入改用 issue body 而非常规 task_description |
| M9 | 文档 | chat-prompt-overview.md §2.2、task-vs-general-chat-flow-analysis.md、本方案 | 同步函数新名、场景标记、注入内容说明 |

---

## 4. 建议实施顺序

1. **A档（本周内）**：
   - A0 重命名（R1）+ A5 去 task 描述/注入 Issue 上下文（R2）+ A1 收尾符 + A2 回复约束 + A3 文档修正 + A4 角色去重
   - 借鉴 D2（Skill references 引导）、D3（Runtime 403 语义）、D7（回复语言跟随用户）
   → 低风险，配 `test_prompt_builder` + `test_chat_task_scene` 回归
2. **B档（暂缓，留档）**：
   - B1 Issue 边界显式化 + B2 知识基线统一 + B3 skill 结构信号（Scene: issue）+ B4 history 窗口 + B5/B6 skill 路径与选择 + B7 Issue body
   - 借鉴 D1（轻量输出契约）、D5（事件数提示）、D10（Issue 前置处理摘要）
3. **C档（暂缓，留档）**：
   - C1 skill 注入 + C2 进度能力 + C3 结构化结论 + C4 附件结构化注入

> 所有改动须同步更新 `chat-prompt-overview.md`（§2.2）与 skill 文档（M7），避免 P6 式漂移。

---

## 5. 验证方式

- 单元：`tests/test_prompt_builder.py`（现有覆盖 general/issue 结构断言）
- 行为：`tests/test_chat_task_scene.py`（场景标记/Issue 边界/上下文注入）——改名后断言脚本同步
- 端到端：真实 issue 会话发消息（澄清 / 咨询状态 / 评审方案 三类样本），检查 prompt 落盘日志（chat.py 有 `log.info` 可观察 user_msg 与 backend/skill）
- prompt 差分：构建前后各渲染一份完整 prompt 对比 diff，人工核对结构（重点：Issue body 注入 + 无 task 描述段）
- skill 联动：确认 `Scene: issue` 在新 skill 仓库 SKILL.md 生效（M7）

---

## 附：当前关键代码位置（避免行号漂移）

| 符号 | 位置 |
|------|------|
| `build_chat_role_definition` | src/prompt_builder.py:339 |
| `build_task_role_definition`（→将改 build_issue_role_definition）| src/prompt_builder.py:363 |
| `_append_chat_history` | src/prompt_builder.py:762 |
| `build_general_chat_prompt` | src/prompt_builder.py:788 |
| `_get_task_description`（→将移除）| src/prompt_builder.py:850 |
| `build_task_chat_prompt`（→将改 build_issue_chat_prompt）| src/prompt_builder.py:880 |
| `_CHAT_PROMPT_SUFFIX` | src/prompt_builder.py:77 |
| chat 分流（conv_type==task） | src/api/routes/chat.py:598/732/1072/1433/1797 |
| 飞书分流 | src/im/handler_async.py:563/576/583 |
| `_build_task_context_message`（task_description 上下文）| src/api/issues_center.py:646 |
| skill（运行时铺入目录，gitignored） | skills/chat/... → 由 sync_skills.sh 从 grape-skills 拉取 |
| 配套 skill 源仓库 | `grape-dev/grape-skills` → `skills/chat/im/{chat, chat-cli, im-notify}` |
| 生产 DB official chat skills | skill 表 id 4314/4315/4367（im-notify/chat/chat-cli） |
| `{{SKILL_DIR}}/chat/SKILL.md` 硬编码 | src/prompt_builder.py:930 |