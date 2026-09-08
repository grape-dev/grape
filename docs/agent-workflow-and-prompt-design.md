# Grape Agent 工作路径与 Prompt 构建技术文档

> 本文档梳理 Grape 项目中 **DevOps Agent** 和 **Chat Agent** 两条核心执行路径的完整工作流程，包括事件入口、路由逻辑、场景配置、Prompt 组成、任务分派、引擎执行等环节。

---

## 一、DevOps Agent 工作路径

### 1.1 入口：Webhook → `run_pipeline`

**文件**：`src/api/event_pipeline.py` · `run_pipeline()` (L661)

GitCode webhook 到达后，`run_pipeline` 执行以下步骤：

| 步骤 | 逻辑 | 说明 |
|------|------|------|
| 归一化 | `get_event_header()` + `normalize_event()` | 将原始 webhook 转为统一 `evt` dict，含 `type`/`repository`/`payload`/`actor` |
| 自循环检测 | actor 是 bot 用户名 → 跳过 | 例外：子任务 Issue、引导 Issue 放行 |
| 系统回帖检测 | 评论含 `<!-- grape-auto-comment -->` → 跳过 | 避免 bot 回复 bot |
| L1 事件准入 | `evaluate_event_action()` | 判定 skip / side_effect / continue |
| 去重检查 | 同 issue + 同 agent_type 已有 running/dispatched 任务 → 跳过 | 防止重复执行 |

### 1.2 事件路由到 agent_type

**路由逻辑**（`event_pipeline.py` L773-813）：

- `issue_opened` → **恒给 vod**（D8 规则）
- 其他事件 → `_resolve_issue_stage_agent()` (L436) 按阶段矩阵路由

**阶段矩阵 `_STAGE_EVENT_AGENT_MAP`**（L423）：

从 `issue_state.stage` 查当前 Issue 处于哪个阶段，按 `event_type × stage` 查表得出目标 agent_type：

| 事件类型 | vod | issue | coding | qa | sre |
|----------|-----|-------|--------|----|-----|
| issue_opened | vod | issue | issue | issue | issue |
| issue_comment | vod | issue | issue | issue | issue |
| pull_request_opened | issue | issue | coding | qa | sre |
| pull_request_updated | issue | issue | qa | qa | sre |
| pull_request_merged | issue | issue | coding | qa | sre |

**兜底机制**：仓库级事件订阅 `_match_repo_subscription()` (L798)，与主路由不同时双路径并行。

### 1.3 场景配置决定 skill 和 agent 链路

**文件**：`src/api/pipeline_config.py`

每个场景（scene）包含若干 **node**，每个 node 定义：

| 字段 | 说明 |
|------|------|
| `tag` | agent_type（vod/issue/coding/qa/sre） |
| `skill` | 该 agent 执行的技能名 |
| `mode` | emit |
| `next` | 链路下一节点 |
| `handoff` | a2a（交给下一 Agent）/ complete（结束） |
| `engine_runtime_ids` | 绑定的引擎 ID 列表 |

**查询链路**（优先级从高到低）：

```
get_pipeline_config(scene_id, repo_key)
  → _query_scene_by_id(scene_id, repo_key)       # DB: devops_pipeline_scene 表
  → _load_official_scene_from_market(scene_id)    # 回退1: scene_market 官方模板
  → _load_pipelines_file()                        # 回退2: configs/pipelines.json
```

**场景优先级**（L917-953）：
1. Issue 级场景（`issue_state.scene_id`）优先
2. 仓库默认场景（`managed_repos.dev_scene`）回退
3. plugin 模式下用 Issue 场景的 skill 覆盖 session skill

**Skill 解析**：

```
resolve_skill_from_pipeline(repo_key, agent_type, scene_id, user_id)
  → get_node_by_tag(scene_id, agent_type, repo_key)
  → node.get("skill")
```

### 1.4 Prompt 构建：`build_devops_agent_prompt`

**文件**：`src/prompt_builder.py` · `build_devops_agent_prompt()` (L402)

```
build_devops_agent_prompt(evt, history, oc_session_id, skills, feishu_ctx,
                          skill_path, runtime_id, agent_type)
```

Prompt 组成部分（按拼装顺序）：

| # | 组件 | 函数/来源 | 说明 |
|---|------|-----------|------|
| 1 | A2A 上下文 | `evt["_a2a_context"]` | 前序 Agent 处理摘要 + Issue 编号 |
| 2 | 事件信息 | `evt["type"]` / `payload` | comment → 评论内容；vod_signal → 用户消息；其他 → title/body |
| 3 | 历史事件摘要 | `get_issue_summary(repo_key)` | 仓库 Issue 聚类上下文 |
| 4 | 项目知识基线 | `_build_project_info_section(repo_key, agent_type)` | project-graph.md + project-capabilities.md 内联截断 + `{{PROJECT_INFO_DIR}}` 占位符 + Agent 专属知识路径指引。**VOD 场景列表在此内部注入**：`_inject_vod_scenes()` 在无基线(L78)和有基线(L98)两个分支末尾都调用 |
| 5 | 场景高级选项 | `_build_scene_advanced_options(repo_key)` | 仅 coding/qa/sre：工作分支 + 自定义配置（读 managed_repos.dev_scene → devops_pipeline_scene.advanced_options） |
| 6 | 子任务上下文 | `session_store.get_parent_issue()` | 父任务/多任务模式上下文 |
| 7 | Skill 指令 | `{{SKILL_DIR}}/{skill}/SKILL.md` 占位符 | 要求先 read SKILL.md 再执行；含 references/ 目录读取指引 + "不要分析或提议——直接执行" |
| 8 | 飞书上下文 | `_build_feishu_context_section()` | 仅 im-notify 技能：飞书 token/协作者/群成员/通知开关 |
| 9 | 角色定义 | `build_role_definition(skills, agent_type)` | DevOps 助手域定义 + 安全规则（coding 允许写代码，其他禁止）+ 图片/视频 MEDIA EXCEPTION 规则 |
| 10 | A2A 协议 | `configs/agent-description.json` | 可用 Agent 列表 + curl 调用方式 + 字段规则（issue_number 必填、pr_number 有则必填、严禁混用）+ context 构造规则 |
| 11 | 角色专属 A2A 规则 | `_a2a_role_rules[agent_type]` | qa（审查通过→sre/不通过→coding）、coding（有PR→qa/无PR→sre）、issue（按标签→sre/coding）各有交办链路规则 |
| 12 | 处理记录输出要求 + 行为约束 | 硬编码 JSON 契约 | actions/conclusion/artifacts/next_step/issue_summary + 任务结束规则 + 内部思考 vs 对外结论分离 + 评论内容边界（[角色名 Agent]前缀 + 只写结论）+ 发布前自检 |
| 13 | `{{SERVER_URL}}` | `_resolve_server_url(runtime_id)` | 替换为服务端可达地址（A2A/tasks 等对外调用） |

> 注：`event_pipeline.py` 中调用的 `build_prompt` 是 `build_devops_agent_prompt` 的废弃别名（`prompt_builder.py` L946: `build_prompt = build_devops_agent_prompt`）。

### 1.5 任务 Claim 和执行

**文件**：`src/runtime/service.py`

#### 执行模式判定

```
runtime_execution_kind(runtime_id) → "direct" | "queue_remote"
```

| 条件 | 模式 | 说明 |
|------|------|------|
| `credential_id == 0`（本机引擎） | **DIRECT** | 本机线程池直接执行 |
| `credential_id > 0` 且引擎在线 | **QUEUE_REMOTE** | 入队 → 远端 worker claim |
| 远端引擎离线 | 降级 **DIRECT** | 回退本机执行 |

#### 分派逻辑（`event_pipeline.py` L1098-1147）

```
engine_id = pick_engine(repo_key, agent_type, fallback_runtime_id, scene_id, user_id)
kind = runtime_execution_kind(engine_id)

if kind != EXEC_MODE_DIRECT:
    _enqueue_pipeline_task(...)    # → enqueue_task → agent_tasks(queued) → 远端 claim
elif kind == EXEC_MODE_DIRECT:
    _create_direct_task(...)       # 先建 agent_tasks(queued) + started_at
    start_direct_agent(_execute_agent, ...)   # 提交到 _DIRECT_POOL 线程池
```

#### DIRECT 模式执行

**函数**：`_execute_agent` → `_execute_agent_inner`（`event_pipeline.py` L1660 / L1701）

1. 构建 prompt：`build_prompt(evt, [], "", [skill_name], skill_path, runtime_id, agent_type)`
2. 物化工位：`_materialize_direct_workdir()` → 将 project-info/skill/knowledge 落地到 ws 目录
3. 替换占位符：`{{SKILL_DIR}}` → 服务端 SKILLS_DIR；`{{PROJECT_INFO_DIR}}` → ws/.project-info
4. 调用 `stream_devops_agent(prompt, ws, session_id, ..., backend_name, bot_token, env_extra)`
5. 执行完成后：更新 event status/result/oc_session_id；自动回帖到 Issue；同步到任务会话

#### QUEUE_REMOTE 模式执行

**入队函数**：`_enqueue_pipeline_task`（`event_pipeline.py` L1279）

1. 构建 prompt（同上 `build_prompt`）
2. 打包 skill_files（`_pack_skill_dir` 或 OBS zip）
3. 打包 project_info_files + knowledge_files
4. 注入 env_extra：
   - `REPO_FULL_NAME` / `ACCOUNT` / `GRAPE_SERVER_URL`
   - 华为云 AK/SK（按仓库 owner 查 `runtime_credentials`）
   - `GRAPE_WORK_BRANCH`（场景工作分支）
   - `ISSUE_NUMBER` / `PR_NUMBER`
5. `enqueue_task()` → INSERT agent_tasks(status=queued, pool_tag=agent_type)

**Claim 逻辑**：`service.py` L837 `claim_by_tag(tag, runtime_id)`

- worker 端 `agent.py` main() 循环：遍历 runtime_ids → `client.claim_by_tag(rid, tag)`
- 服务端原子抢单：`UPDATE agent_tasks SET status='dispatched' WHERE id=? AND status='queued'`
- 引擎级并发槽检查：`busy_slots < max_concurrent` 才允许 claim
- 串行槽检查：`NOT EXISTS(... issue_session_id 匹配的 dispatched/running 任务)`
- scope 隔离：global / repo / user

**Worker 执行**：`agent.py` L904 `_execute_task(client, task)`

1. 落地技能到 `cwd/.skills/`（OBS zip 解压或 skill_files 文本）
2. 落地 project-info 到 `cwd/.project-info/`
3. 落地 knowledge 到 `cwd/.knowledge/`
4. 替换占位符：`{{SKILL_DIR}}` / `{{PROJECT_INFO_DIR}}` / `{{KNOWLEDGE_DIR}}` / `{{SERVER_URL}}`
5. 调用 `stream_runtime()` → `stream_agent()` → `stream_devops_agent()`
6. 回报结果：`report_messages` + `report_task_complete`

### 1.6 最终执行引擎

**文件**：`src/agent/executor.py` · `stream_devops_agent()` (L279) → `stream_agent()` (L302)

| 后端 | 执行方式 | 说明 |
|------|----------|------|
| hermes | `_stream_hermes_acp()` | ACP JSON-RPC 协议，不走 CLI 文本解析 |
| opencode | CLI 子进程 + NDJSON | 流式输出 |
| codearts | 每次启动独立 serve + CLI | 独立进程 |

**环境变量注入**（`_assemble_env` L209）：
- **devops 模式**：只注入 `BOT_GITCODE_TOKEN`（仓库身份）
- 用户云凭据（华为云 AK/SK）按 user_id 注入，与 GitCode token 无关

---

## 二、Chat Agent 工作路径

### 2.1 路由入口

**文件**：`src/api/routes/chat.py`

| 端点 | 说明 |
|------|------|
| `POST /api/chat/send` (L476) | 非流式 |
| `POST /api/chat/send/stream` (L605) | SSE 流式 |

### 2.2 用户消息触发 Agent 执行

**`send_message` 流程**（L476-602）：

1. 解析 conversation（已有 or 新建）
2. 获取用户 GitCode token（`get_user_token_with_refresh`）
3. 保存用户消息到 DB（`add_message`）
4. 任务会话 → `mark_task_processing_by_conversation`（active→processing）
5. 读取 chat 配置（`_get_chat_config` → backend/skill/skill_id/runtime_id）
6. 获取对话历史（`get_messages(limit=20)`）
7. 解析 runtime_session_id（`ses_` 开头才有效）
8. **构建 prompt**（见下节）
9. 图片/视频理解注入（`_understand_image` / `_understand_video`）
10. 判定执行模式：
    - 无引擎 → 返回错误
    - `kind != DIRECT` → `_enqueue_chat()`（入队 + 轮询）
    - `kind == DIRECT` → `stream_chat_agent()` 直接执行

**`send_message_stream` 流程**（L605-930）：
- 同上 1-9
- DIRECT → 后台线程 `agent_worker` + SSE Queue 桥接
- QUEUE_REMOTE → `enqueue_task` + `wait_task_terminal` + SSE 终态返回

### 2.3 Chat Agent Prompt 构建

#### 通用会话 `build_general_chat_prompt`

**文件**：`src/prompt_builder.py` (L705)

```
build_general_chat_prompt(message, repo_key, history, skill_name, skill_id,
                          runtime_id, backend_name, user_info, ...)
```

| # | 组件 | 来源 | 说明 |
|---|------|------|------|
| 1 | 角色定义 | `build_chat_role_definition()` (L329) | DevOps 助手域 + 安全规则，禁止写代码/git |
| 2 | 引擎信息 | `Runtime engine: {backend_name}` | — |
| 3 | 用户信息 | `_build_web_user_info()` | GitCode login + 昵称 |
| 4 | 仓库上下文 | `IMPORTANT: You are working in repository: {repo_key}` | — |
| 5 | 事件上下文 | `get_issue_summary(repo_key)` | — |
| 6 | 项目知识基线 | `_build_project_info_section(repo_key, "chat")` | — |
| 7 | 对话历史 | `_append_chat_history()` | 最近 3 条（有 session）或 10 条（无 session）+ [XXX Agent] 进展 |
| 8 | Skill 指令 | `{{SKILL_DIR}}/{skill}/SKILL.md` 或绝对路径（custom skill） | 含"必须先执行 read" 强制指令 |
| 9 | GitCode API 说明 | `USER_GITCODE_TOKEN` 环境变量使用说明 | 含 curl 示例 + "Never print or reveal the token" |
| 10 | 用户消息 | `User: {message}\nAssistant:` | prompt 结尾格式 |

#### 任务会话 `build_task_chat_prompt`

**文件**：`src/prompt_builder.py` (L808)

```
build_task_chat_prompt(user_text, repo_key, history, conv, account,
                       runtime_id, user_info, has_valid_session)
```

| # | 组件 | 说明 |
|---|------|------|
| 1 | 角色定义 | `build_task_role_definition()` (L351) — 澄清 Issue 需求 + 回帖，不实施代码；含 SECURITY 规则 + 图片分析规则 |
| 2 | 场景标记 | `## 场景: task` |
| 3 | 任务背景 | 仓库 + Issue 编号 + 标题 |
| 4 | 引擎信息 | `Runtime engine: {name} (runtime_id={id})` |
| 5 | 任务描述 | `_get_task_description()` 从 tasks 表反查（按 conversation_id 或 repo_key+issue_number） |
| 6 | Issue 摘要 | `get_issue_summary(repo_key)` |
| 7 | 项目知识基线 | `_build_project_info_section(repo_key, "chat")` |
| 8 | 对话历史 | `_append_chat_history()` |
| 9 | 用户消息 | `## 本次用户消息\n{user_text}` |
| 10 | Skill 指令 | `{{SKILL_DIR}}/chat/SKILL.md` 含"必须先执行 read" 强制指令 |

### 2.4 Chat Agent 引擎执行

**文件**：`src/agent/executor.py` · `stream_chat_agent()` (L257)

```python
stream_chat_agent(prompt, cwd, session_id, account, user_id, user_token,
                  env_extra, agent_name, agent_label, backend_name)
```

- 调用 `stream_agent()`，`event_type="chat"`
- `_assemble_env(kind="chat")`：只注入 `USER_GITCODE_TOKEN`，**不注入** `BOT_GITCODE_TOKEN`
- 工作目录：`_get_chat_cwd(user_id, repo_key, conv_id)` → `WORKSPACE_DIR/{owner}/{repo}/chat/{uid}/{conv_id}`

### 2.5 Session 管理

| 维度 | 说明 |
|------|------|
| 粒度 | conversation 级，`chat_conversations` 表，每对话独立 `runtime_session_id` |
| 有效性判定 | `has_valid_session = runtime_session_id.startswith("ses_")` |
| 历史注入策略 | 有 valid session → 最近 3 条兜底；无 → 最近 10 条 |
| Session 更新 | 执行成功后回写 `update_conversation(conv_id, runtime_session_id=new_sid)` |
| 过期处理 | 旧 session 过期（opencode 静默 EOF）时用新 session 覆盖 |

---

## 三、DevOps Agent vs Chat Agent 核心差异

| 维度 | DevOps Agent | Chat Agent (general) | Chat Agent (task) |
|------|-------------|----------------------|-------------------|
| **触发方式** | Webhook 事件驱动 | 用户主动发送消息 | 用户在任务会话中发送消息 |
| **Token 类型** | `BOT_GITCODE_TOKEN`（仓库身份） | `USER_GITCODE_TOKEN`（用户身份） | `USER_GITCODE_TOKEN` |
| **Skill 来源** | 场景配置 pipeline_config → node.skill | chat 配置 `_get_chat_config` | 固定 `chat` skill |
| **A2A 协议** | 有，完整多 Agent 链路 + 角色交办规则 | 无 | 无 |
| **JSON 契约** | 必须输出处理记录 + 行为约束（思考/结论分离、评论边界、自检） | 无 | 无 |
| **角色定义** | coding 允许写代码，其他只读 + MEDIA EXCEPTION | 禁止写代码/git + MEDIA EXCEPTION | 澄清需求+回帖，不实施 + SECURITY + 图片规则 |
| **对话历史** | 不注入（由 evt payload 承载） | 最近 3/10 条 | 最近 3/10 条 |
| **VOD 场景列表** | 仅 vod agent（在项目知识基线内部注入） | 无 | 无 |
| **场景高级选项** | coding/qa/sre 注入工作分支 | 无 | 无 |
| **项目知识** | `_build_project_info_section(agent_type)` | `agent_type="chat"` | `agent_type="chat"` |
| **执行器** | `stream_devops_agent` | `stream_chat_agent` | `stream_chat_agent` |
| **工作目录** | `issues/{num}/{agent_type}/` | `chat/{uid}/{conv_id}/` | `chat/{uid}/{conv_id}/` |
| **Session 隔离** | Issue 级 `issue_session` | conversation 级 | conversation 级 |
| **Server URL** | `{{SERVER_URL}}` 替换 | 不含 | 不含 |
| **飞书上下文** | im-notify 技能有 | 无 | 无 |
| **子任务上下文** | parent_issue / multi-task | 无 | 无 |

---

## 四、完整流程图

### DevOps Agent

```
GitCode Webhook
  │
  ▼
run_pipeline()                          [event_pipeline.py L661]
  ├─ 归一化 → evt dict
  ├─ 自循环检测 / 系统回帖检测 / L1 过滤 / 去重
  │
  ├─ 路由: _resolve_issue_stage_agent()  [阶段矩阵 event_type × stage → agent_type]
  ├─ 场景: resolve_skill_from_pipeline() [pipeline_config.py → node.skill]
  ├─ 引擎: pick_engine()                 [engine_select.py → 按标签+scope选引擎]
  │
  ├─ DIRECT 模式 (本机引擎)
  │   └─ _execute_agent_inner()
  │       ├─ build_prompt()              [prompt_builder.py]
  │       ├─ _materialize_direct_workdir() [占位符替换]
  │       └─ stream_devops_agent()       [executor.py]
  │           ├─ hermes → _stream_hermes_acp()
  │           ├─ opencode → CLI + NDJSON
  │           └─ codearts → serve + CLI
  │
  └─ QUEUE_REMOTE 模式 (远端 worker)
      └─ _enqueue_pipeline_task()
          ├─ build_prompt()
          ├─ 打包 skill / project-info / knowledge
          ├─ enqueue_task() → agent_tasks(queued, pool_tag=agent_type)
          │
          └─ Worker 端:
              ├─ claim_by_tag()           [原子抢单 + 并发槽检查]
              ├─ _execute_task()
              │   ├─ 落地 skill/knowledge 到 cwd
              │   ├─ 替换占位符
              │   └─ stream_devops_agent()
              └─ report_task_complete()
```

### Chat Agent

```
用户发送消息
  │
  ▼
POST /api/chat/send[/stream]             [chat.py]
  ├─ 解析/创建 conversation
  ├─ 获取用户 GitCode token
  ├─ 保存用户消息到 DB
  ├─ 读取 chat 配置 (backend/skill/runtime_id)
  ├─ 获取对话历史 (最近 20 条)
  │
  ├─ 判定会话类型:
  │   ├─ 通用会话 → build_general_chat_prompt()   [prompt_builder.py L705]
  │   └─ 任务会话 → build_task_chat_prompt()      [prompt_builder.py L808]
  │
  ├─ 图片/视频理解注入
  │
  ├─ DIRECT 模式
  │   └─ stream_chat_agent()             [executor.py L257]
  │       ├─ _assemble_env(kind="chat")  [注入 USER_GITCODE_TOKEN]
  │       └─ stream_agent()
  │           ├─ hermes → _stream_hermes_acp()
  │           ├─ opencode → CLI + NDJSON
  │           └─ codearts → serve + CLI
  │
  └─ QUEUE_REMOTE 模式
      ├─ enqueue_task()
      └─ worker claim → stream_chat_agent() → 回报结果
```

---

## 五、关键文件索引

| 文件 | 职责 |
|------|------|
| `src/api/event_pipeline.py` | DevOps 事件流水线入口、路由、分派、direct 执行 |
| `src/api/routes/chat.py` | Chat Agent 路由入口、会话管理 |
| `src/prompt_builder.py` | Prompt 构建（DevOps + Chat 通用/任务） |
| `src/api/pipeline_config.py` | 场景配置读取、skill 解析 |
| `src/runtime/service.py` | 任务 claim/dispatch、执行模式判定、并发控制 |
| `src/runtime/agent.py` | Worker 端任务执行、skill/knowledge 落地 |
| `src/runtime/engine_select.py` | 引擎选择（按标签 + scope 过滤） |
| `src/agent/executor.py` | 最终执行器（hermes/opencode/codearts 后端适配） |
| `src/agent/hermes_acp.py` | Hermes ACP JSON-RPC 协议实现 |
| `src/storage/scene_sync.py` | 场景市场同步（从 grape-skills 仓库） |
