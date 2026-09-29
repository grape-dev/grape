# Issue #219 配套：当前 Prompt 构建流程与基线示例（专项优化输入）

> 状态：基线快照（2026-09-22）｜分支 weekly-04-lyz
> 用途：作为 Issue #219 prompt 专项优化的输入基线——描述**当前** `prompt_builder.py` 的构建流程，并附一份真实生成示例。
> 关联实现：commit `fcf31b9d`（去 curl）+ `1e0118d5`（Option B 去上下文/协议段）；设计总案见 `issue-219-devops-agent-prompt-optimization-design.md`。

---

## 1. 总览：三类 Prompt 构建入口

| 入口函数 | 使用场景 | 关键差异 |
|---------|---------|---------|
| `build_devops_agent_prompt` | 事件流水线（vod→issue→coding→…）驱动 agent | 事件正文 + 附件 + Issue Pipeline 摘要 + Skill 指引 + 输出契约 |
| `build_general_chat_prompt` | Web / 飞书通用 Chat | Role + 事件摘要 + 对话历史 + Skill，末尾追加 `\nAssistant:` |
| `build_task_chat_prompt` | 任务中心「去对话」（已分配 Issue） | task 专属 Role + 任务描述 + Issue 摘要 + chat 技能 |

以下按代码行号（`src/prompt_builder.py`，commit `1e0118d5`）记录各入口的**实际注入顺序**。

---

## 2. `build_devops_agent_prompt` 构建流程

入参：`evt, history, oc_session_id, skills, feishu_ctx, skill_path, runtime_id, agent_type`。输出为 `"\n".join(parts)` 后把 `{{SERVER_URL}}` 替换为 `_resolve_server_url(runtime_id)`（幂等）。

| 步骤 | 代码位置 | 注入段 | 触发条件 | 内容来源 |
|------|---------|--------|---------|---------|
| 1 | L579-590 | A2A 前序上下文 | evt 含 `_a2a_context` | `## 任务 Issue` + `## 前序 Agent 处理摘要` + 目标 PR |
| 2 | L592-623 | 事件正文（按 evt_type 三分支） | 恒注入 | 见 §2.1 |
| 3 | L626 | 用户附件 | vod/issue/coding 且有附件 | `_inject_attachments_by_role` |
| 4 | L628-629 | 历史事件计数 | history 非空 | `This repository has N past events recorded.` |
| 5 | L631-638 | Issue Pipeline 摘要 | `get_issue_summary` 有返回 | 见 §2.2 |
| 6 | L643-647 | 场景高级选项 | 仅 coding/qa/sre 且仓库有配置 | `_build_scene_advanced_options`（值走 env，prompt 只给指引） |
| 7 | L650-671 | 子任务 / 多任务上下文 | repo 有父任务或启多任务模式 | session_store 表 |
| 8 | L673-682 | Skill 指引段 | skills 非空 | `{{SKILL_DIR}}/<skill>/SKILL.md` 占位符 + 必须先读指令 |
| 9 | L684-686 | 飞书上下文 | feishu_ctx 且 skill==`im-notify` | `_build_feishu_context_section`（含飞书 curl，保留） |
| 10 | L688 | 角色定义 | 恒注入 | `build_role_definition`，见 §2.3 |
| 11 | L691-703 | 执行引擎绑定 | runtime_id>0 | agent_runtimes 表 |
| 12 | L705 | 输出契约 | 恒注入 | `_build_output_contract`（收尾二选一），见 §2.4 |
| 13 | L707 | `{{SERVER_URL}}` 替换 | 恒执行 | `_resolve_server_url` |

### 2.1 事件正文三分支（步骤 2）

- **issue_comment**：`User commented on issue #N of repo` → 评论内容（截 1000）→ `@bot` 标记 → 缺编号时「必须定位评论所属 Issue」引导 → 「必须回复评论」+ 评论格式规范（`[角色名 Agent]` 前缀）。
- **vod_signal**：`## 用户消息`（来自谁、消息内容截 2000）。
- **else（issue_opened / pr_* 等）**：`Event: {type} #{number} on {repo}` → `Title:` → `Body:`（body 截 4000）。

### 2.2 Issue Pipeline 摘要（步骤 5）

`get_issue_summary(repo_key, max_issues=10)` 按 Issue 聚类最近事件流：

```
Issue Pipeline:
  {iid} {title} — {agent}:{event_type}{✓/▶/✗/○} → …          # 事件流，状态图标
    最新结论: {最新 completed 事件的 result，截 300}            # 仅 completed 事件
```

### 2.3 角色定义 `build_role_definition`（步骤 10）

固定 4 条 SECURITY（token 环境变量 `BOT_*_TOKEN`、禁读内部数据、403 处理、禁写 token），再按 `agent_type` 追加：
- `coding`：`3. You MAY write code…`；非 coding：`3. Never write code…`
- 恒追加 `4. Always reply in Chinese…`
- 恒追加 `5. MEDIA EXCEPTION…`

⚠️ 已知瑕疵：因为「3.」分两条追加路径，最终输出会出现**两个 `3.` 和两个 `4.`**（编号错乱），见 §6.1。

### 2.4 输出契约 `_build_output_contract`（步骤 12）

五个子段：`## 处理记录输出要求`（收尾二选一）→ `## 任务结束` → `## 内部思考 vs 对外结论` → 评论内容边界 → 发布前自检。

> 2026-09-22 评审：前两个标题拟改为 `## 任务完成`（原「处理记录输出要求」）/ `## 评论答复`（原「任务结束」），且「评论答复」段不再重复收尾说明（见 §8 方向 D）。

---

## 3. `build_general_chat_prompt` 构建流程

| 步骤 | 代码位置 | 注入段 | 触发条件 |
|------|---------|--------|---------|
| 1 | L760 | Role（`build_chat_role_definition`） | 恒注入 |
| 2 | L761-770 | Runtime engine | backend_name 或 runtime_id |
| 3 | L771-772 | user_info | 非空 |
| 4 | L773-774 | 仓库标识 | repo_key 非空 |
| 5 | L776-783 | Repository event context（Issue Pipeline） | `get_issue_summary` 有返回 |
| 6 | L785 | 对话历史 / Issue Agent 进展 | history 非空 |
| 7 | L787-805 | 使用技能 | skill_name/skill_id/skills |
| 8 | L806-807 | `User (in repo): {message}\nAssistant:` | 恒注入 |

---

## 4. `build_task_chat_prompt` 构建流程

| 步骤 | 代码位置 | 注入段 | 触发条件 |
|------|---------|--------|---------|
| 1 | L855 | Role（`build_task_role_definition`） | 恒注入 |
| 2 | L858-859 | user_info | 非空 |
| 3 | L861-863 | `## 场景: task` + 仓库 + Issue + 标题 | 恒注入 |
| 4 | L865-872 | Runtime engine | runtime_id |
| 5 | L874-877 | `### 任务描述` | tasks 表有 task_description |
| 6 | L879-884 | Issue Pipeline 摘要 | `get_issue_summary` 有返回 |
| 7 | L887 | 对话历史 / Issue Agent 进展 | history 非空 |
| 8 | L889 | `## 本次用户消息` | 恒注入 |
| 9 | L891-897 | 使用技能（chat） | 恒注入 |

---

## 5. 基线 Prompt 示例（真实输出）

> 场景：仓库 `Jason7602/ai-campus-event-planner`、事件 `issue_opened #5`、技能 `vod-cli`、无 A2A 上下文、`runtime_id=0`。
> 由 `build_devops_agent_prompt` 生成，以下为完整 prompt 文本（对齐 §2 步骤 2→5→8→10→12 组合）：

```
Event: issue_opened #5 on Jason7602/ai-campus-event-planner
Title: "AI校园活动指的是什么"
Issue Pipeline:
  #5 AI校园活动指的是什么 — vod:issue_opened▶
  #4 这个项目有什么特点 — vod:issue_opened✓
    最新结论: 结论：Issue #4「这个项目有什么特点」为纯咨询类，无需开发动作，已基于 README 完成项目特点回复。  动作： • 回复项目特点评论：基于 README 的项目亮点与核心特性，说明多智能体协同、并行执行、知识库驱动、配置化、四份交付物、云上一键部署、支持二次开发等 7 项特点  产出： - 评论 id=191072011（项目特点介绍）  摘要：纯咨询已答复，无后续开发需求。
  #3 介绍一下这个项目 — vod:issue_opened✓
    最新结论: 结论：Issue #3「介绍一下这个项目」为纯咨询类，无需开发动作，已基于 README 完成项目介绍回复。  动作： • 回复项目介绍评论：基于 README 说明项目定位、核心亮点、技术栈与部署方式  产出： - 评论 id=191025555（项目介绍）  摘要：纯咨询已答复，无后续开发需求。
  #2 【二次开发】值得探索的 4 个开发方向 — vod:issue_opened✗
  #1 【体验】快速部署并体验本项目的核心功能 — vod:issue_opened✗
## Skill: vod-cli
Skill 文件：{{SKILL_DIR}}/vod-cli/SKILL.md
**必须先执行**：用 read tool 读取该 SKILL.md，并严格执行其中的指令（不得跳过读取或凭名称猜测）。
SKILL.md 中如指引读取 references/ 目录下的文件，请按需读取后再执行。
执行以上指令，不要分析或提议——直接执行。
You are a DevOps operations assistant. Your domain:
- Code review, build diagnosis, deployment guard, incident triage
- Issue/PR analysis, changelog generation, repository management
- Repository (GitCode/GitHub) API operations


SECURITY (MUST FOLLOW):
1. Use ONLY the GitCode token provided in this prompt (BOT_GITCODE_TOKEN). NEVER read, guess, or use any other token from the database, config files, logs, or history.
2. NEVER access the bot's internal data/ and configs/ directories, sessions.db, user_accounts table, or any bot-internal data.3. If the GitCode API returns 403 (no permission): STOP that operation and tell the user the action needs admin/maintainer permission (configure it manually in GitCode). NEVER try other tokens or workarounds.
4. NEVER write any token into files, logs, or comments.
3. Never write code, modify files, or execute git commands
4. Always reply in Chinese. Never include internal reasoning, process steps, or English analysis in your reply.
5. MEDIA EXCEPTION (overrides rule 2): When prompt contains "## 用户上传的图片分析" or "## 用户上传的视频分析" section, the image/video has already been analyzed by the system. You MUST answer the user's question based on that analysis. This includes ANY question about the image/video — UI/UX, design, layout, content, code shown in screenshot, architecture, video content, etc. These are NOT "non-DevOps topics". Do NOT refuse. Do NOT say you cannot help with design or UI. Just answer based on the analysis provided.


## 处理记录输出要求


完成当前任务后，**以你当前技能（SKILL.md）的收尾流程为准**，按以下二选一执行：


**情形一：技能提供 `grape tasks complete` 结构化收尾（采用 grape-cli 的技能）**
- 调完 `grape tasks complete`（结构化直传 actions/conclusion/artifacts/next_step/issue_summary）即结束任务。
- **不要再重复输出下方 JSON 块**——内容已随 complete 结构化上报服务端。
- ⚠️ 若任务已终态，complete 返回 `already:true` 属正常，忽略即可。


**情形二：技能未提供 `grape tasks complete` 收尾**
- 在最终答复末尾输出以下 JSON 块（系统会解析写入该 Issue 的 event.json 档案）：


{
  "actions": ["做了什么，如 labeled:bug / assigned:@bob / 提交了PR / 发布了审查评论"],
  "conclusion": "处理结论/分析结果（简要）",
  "artifacts": ["产生的产物：PR#N / 评论 / 标签 / 部署 / Issue"],
  "next_step": "下一步建议；如需交办其他 Agent 则说明调用 A2A 的目标",
  "issue_summary": "Issue 最新聚合结论（覆盖档案 summary；紧凑文本，用 | 分隔）"
}


注意：
- 两路**二选一**，不得同时输出 JSON 块并在结尾再调 `grape tasks complete`（内容重复）。
- 情形二下只输出一个 JSON 块，放在答复末尾；纯 JSON 结构，不得与叙述混排，且只能是最终答复末尾唯一的追加内容。
- 若无法确定动作或结论，相应字段填空数组/空字符串，不要伪造。
- `issue_summary` 应反映截至当前链尾的最新状态，供下游 Agent 快速浏览。


## 任务结束


按上述情形完成收尾（调 `grape tasks complete` 或输出 JSON 块）后**结束任务**。
不要继续思考、不要等待用户输入、不要追加额外内容；任务以你的最终输出为终点。


⚠️ 思考过程禁止出现在对外输出：任何思考过程叙述（如"让我…""现在我…""Let me…""Now I…"）
只能出现在工具调用前的自然语言里，绝不能出现在最终答复、报告或 GitCode 评论 body 中。
最终答复只输出：动作结果结论 + 收尾产物（`grape tasks complete` 结构化上报或处理记录 JSON 块，二选一）。


## 内部思考 vs 对外结论（必须分清）


内部思考（只存在于思考/工具调用过程，绝不进最终答复与评论）：
  ❌ "让我分析一下这个问题……"   ❌ "现在我理解了代码库……"
  ❌ "我需要先获取环境变量……"   ❌ "Let me analyze the Issue……"
  ❌ "Now I have a thorough understanding……"


对外结论（最终答复与评论唯一允许的正文）：
  ✅ "已定位 #38 泄漏根因：result_text 一鱼两吃导致 JSON 块入评。"
  ✅ "完成：分类=bug，标签=bug/P1，已 A2A 交办 coding。"


规则：最终答复/评论中的每个句子，都必须能在"给用户看的提交记录"里站得住——只陈述完成的事与结果。


⚠️ 评论内容边界（必须遵守）：发评论时，body 只保留 [角色名 Agent] 前缀 +
对外结论（做了什么 / 结果 / 下一步 / 必要的原文引用）。
禁止把思考过程（"让我…""现在…""我需要先…"这类叙述）、
处理记录 JSON 块（actions/conclusion/...）以及任何 HTML 注释标记
（如 <!-- xxx -->）写进评论正文。处理记录 JSON 只放在最终答复末尾供系统
存档，绝不通过评论 API 发送。


⚠️ 发布前自检：把评论 body 当作**对外发布文案**朗读一遍，
出现任何"我"的思考叙述即为不合格，改写后再发布。
```

---

## 6. 观察点（基线中的已知瑕疵，可作为专项优化候选）

| # | 现象 | 位置 | 说明 |
|---|------|------|------|
| 1 | **SECURITY 规则 2/3 粘连（真实 bug，缺逗号）**：真实输出 `...any bot-internal data.3. If the GitCode API returns 403...` 无换行，403 规则被吞进规则 2，无独立编号 | `prompt_builder.py:231-232` | L231 字符串结尾**缺逗号**，与 L232 相邻字符串字面量被 Python 隐式拼接 → `rules[7] == "2. ...data." + "3. If ...workarounds."`。此例无 `RuntimeError` 故测试未暴露；真实 prompt 编号序列为 `1、2(+3粘连)、4、3、4、5` |
| 2 | 角色定义编号错乱：后续条件追加的「3. Never write code / 4. Always reply in Chinese / 5. MEDIA」沿用 3 起头编号，与 SECURITY 的 3/4 冲突 | §2.3 | `build_role_definition` 条件插入「3.」时未对既有编号重排；叠加 #1 后同一 prompt 内出现两个 `3.`、两个 `4.` |
| 3 | 输出含重复信息：SECURITY「Never write code」与 Role 域描述、输出契约等存在语义重叠 | 示例 | Option B 移除上下文段后，role + output contract 仍各带一套边界约束，有压缩空间 |
| 4 | 空行偏多：`_build_output_contract` f-string 以 `\n` 开头、段间多空行 | L417 | 输出契约整体前置换行导致 prompt 顶部大段空白 |
| 5 | 本 Issue（#5）也出现在 Issue Pipeline 中但无「最新结论」（running 无 result） | §2.2 | 属预期行为，但可考虑当前 Issue 是否需要更显式的自我状态 |
| 6 | 双语文案混排（英文事件/角色 + 中文指令） | 全篇 | 依 `code style` 与回复规则，对外中文、内部英文的边界是否要收紧待定 |

（以上记录为基线快照观察，不构成优化方向；具体优化以你后续给出的方向为准。）

---

## 7. 验证方式

- 单测：`tests/test_prompt_builder.py`（`TestNoCurlTemplates` + `TestConditionalClosingFlow`）。
- 手工产物：按 §5 场景构造 event 后调用 `build_devops_agent_prompt` 即可复现示例；引擎侧注入链见 `event_pipeline.py` → `executor`（env 不变）。

---

## 8. 专项优化方向（评审已确认，2026-09-22）

> 以下为对 §5 基线的四条评审方向（A/B/C/D + E），均已确认并**于 2026-09-22 实施**（commit 待提交，见 §10 实施记录）。

### 方向 A：当前 Issue 不应出现在 Issue Pipeline 中（对应评审 L112）

**问题**：`get_issue_summary` 将仓库 events 表内**所有** Issue 聚合注入，当前 Issue（#5）以 `vod:issue_opened▶` 出现在 pipeline 里；agent 已持有自身事件正文，看到自己是噪音且易混淆（自身状态与他人干扰混排）。

**改动**：
| 文件 | 改动 |
|------|------|
| `src/api/event_store.py` `get_issue_summary` | 新增参数 `exclude_issue: str = ""`，聚类后跳过该 issue_id（或在 `get_events_grouped_by_issue` 前置过滤） |
| `src/prompt_builder.py` `build_devops_agent_prompt` L631-636 | 调用时传当前 issue id（`evt.get("issue_id")` 或 `str(number)`） |
| `src/prompt_builder.py` `build_task_chat_prompt` L878-884 | 传 `conv["issue_number"]` |

**边界**：
- `build_general_chat_prompt`（Web/飞书对话）无「当前 Issue」概念，**保留注入全部**，不排除。
- `event_pipeline.py:1872` / `vod_handler_async.py:193` 为其它上下文，本方向不动。
- 若排除后 repo 无其它 Issue → 摘要为空，整段省略即可。

**测试**：`tests/test_prompt_builder.py` 新增断言——devops prompt 的 Issue Pipeline 段不含当前 issue；`get_issue_summary(..., exclude_issue=...)` 单测。

---

### 方向 B：角色段加空行 + `## 角色定义` 标题（对应评审 L124）

**问题**：Skill 段与角色段无缝相接（`...直接执行。\nYou are a DevOps...`），且角色无章节标题，与其它 `##` 段落风格不一致。

**改动**：`src/prompt_builder.py` `build_devops_agent_prompt` 角色注入处（L688）改为：
```python
role_def = build_role_definition(skills, agent_type=agent_type, repo_key=_rk)
parts.append("\n## 角色定义\n" + role_def)
```
- 前置 `\n` 在 join 后产生一个空行；`## 角色定义` 作为独立标题行。
- （可选一致性：`build_general_chat_prompt` / `build_task_chat_prompt` 的 role 段同步加 `## 角色定义`，当前它们各自位于 prompt 首部。）

**测试**：新增断言——devops prompt 含 `\n\n## 角色定义\n`，角色首行 `You are` 前有标题。

---

### 方向 C：SECURITY 段与后续规则合并为连续编号（对应评审 L130）

**问题**：角色定义内两块规则独立编号导致错乱（§6 观察 1/2），并叠加缺逗号粘连 bug。评审要求**合并**。

**改动**：重写 `src/prompt_builder.py` `build_role_definition`（L218-240）为**单一连续编号**规则表：
```text
## 角色定义                              ← 标题由调用方加（方向 B）
You are a DevOps operations assistant. Your domain:
- ...

## SECURITY (MUST FOLLOW):
1. 只用 {vcs} token（{_bot_token_env}），绝不读/猜其它 token
2. 绝不访问 bot 内部 data/ configs/ sessions.db / user_accounts
3. {vcs} API 403 → 停止并告知需管理员权限，不换 token 不绕路
4. 绝不把 token 写入文件/日志/评论
5. [coding] You MAY write code… / [其它] Never write code, modify files, or execute git commands
6. Always reply in Chinese. Never include internal reasoning, process steps, or English analysis.
7. MEDIA EXCEPTION（图片/视频分析规则，覆盖规则 2）
```
- 编号 1-7 连续，天然消除 §6-1/§6-2（含缺逗号 bug，因该行重写）。
- `coding` 分支仅换规则 5 文案，编号不变。

**测试影响**：
- `test_default_has_never_write_code` / `test_implementation_allows_code` / `test_other_skill_blocks_code`：断言短语 `Never write code`、`MAY write code` 仍在，**无需改**（注意 coding 分支全文不得再出现 `Never write code` 字串）。
- `test_devops_prompt_keeps_token_env_name`（`BOT_GITCODE_TOKEN`）、`TestNoCurlTemplates` 不受影响。

---

### 方向 D：输出契约分节改名 + 去重复（对应评审 L139 / L171，映射按批注纠正版）

**问题**：`_build_output_contract` 的 `## 处理记录输出要求` 与 `## 任务结束` 命名与语义不符。

**改动**（`src/prompt_builder.py` `_build_output_contract`）：
- `## 处理记录输出要求` → `## 任务完成`（收尾二选一 + JSON 块 + 结束任务说明归此段）。
- `## 任务结束` → `## 评论答复`（思考禁止 / 内部思考 vs 对外结论 / 评论内容边界 / 发布前自检归此段）。
- 「评论答复」段开头删除「按上述情形完成收尾（调 `grape tasks complete` 或输出 JSON 块）后**结束任务**…」整段——收尾流程已由「任务完成」段承载；「最终答复只输出：动作结果结论 + 收尾产物（…二选一）」同步简化为「最终答复只输出：动作结果结论 + 收尾产物」。

**测试影响**（`tests/test_prompt_builder.py`）：
| 用例 | 现状断言 | 改后 |
|------|---------|------|
| `TestTaskEndInstruction::test_b1` | `"任务结束" in prompt` | 改 `"任务完成" in prompt` |
| `TestTaskEndInstruction::test_b2` | `"处理记录输出要求" in prompt`、`index(...) < index("任务结束")` | 改 `"任务完成" in prompt`（首段）、`index("任务完成") < index("评论答复")` |
| `TestConditionalClosingFlow::test_task_end_section_aligned` | 断言正文 `...后**结束任务**` | 「结束任务」改由「任务完成」段承载，断言该段含「即结束任务 / 二选一」且在「评论答复」之前 |

---

### 方向 E：prompt 指令文本全部改为英文（2026-09-22 评审新增）

**问题**：prompt 为**中英混排**（§6-6）：事件/角色为英文、指令/契约/边界为中文。混合语言降低大模型指令遵循的一致性，且与 `prompt_builder` 生态一致化目标相悖。

**改动**：把 **prompt 指令文本**（`prompt_builder.py` 内所有硬编码段落）全部译为英文。**数据不动**：事件 Title/Body、Issue Pipeline 的标题与「最新结论」为业务数据，保留原语言。

| # | 区块 | 现（中文） | 改（英文） |
|---|------|-----------|-----------|
| 1 | Skill 指引段 | `## Skill: vod-cli` + 必须先执行… | `## Skill: vod-cli` + MANDATORY: Read SKILL.md… |
| 2 | 角色定义 | 中文 MEDIA 例外描述 | 全英文（§9.2） |
| 3 | 输出契约 · 任务完成 | `## 任务完成`（二选一 / JSON 块） | `## Task Completion` |
| 4 | 输出契约 · 评论答复 | `## 评论答复`（思考禁止 / 评论边界 / 发布前自检） | `## Comment Reply` |
| 5 | 内部思考 vs 对外结论 | 中英正反例混排 | 全英文正反例（保留中英两种思考叙述示例字串） |
| 6 | `## 前序 Agent 处理摘要` / 子任务 / 多任务 / 场景自定义配置 / 用户附件 | 中文标题 | 英文标题（`## Prior Agent Summary` 等） |
| 7 | `build_task_role_definition`（全中文 role） | 中文 | 全英文，语义不变 |

**关键决策（不随英文化改变）**：
- **回复语言规则保留**：`Always reply in Chinese.` —— 指令文本改英文，**对外答复仍中文**（业务要求不变）。
- **MEDIA EXCEPTION 的章节标记** `## 用户上传的图片分析`/`## 用户上传的视频分析` 是外部注入契约的**字面匹配串**，规则文本译英文时这些**标记名原样保留**。
- **Issue Pipeline 标签**（`最新结论` 等）属于注入数据段，不改（见上文边界）。

**测试影响**：`tests/test_prompt_builder.py` 所有断言**中文指令串**的用例需同步译为英文断言：
- `TestThinkingCommentSeparation`（4 项中文断言）
- `TestConditionalClosingFlow`（4 项：`以你当前技能…收尾流程为准` / `不要再重复输出…` / `情形二…` / `二选一` → 英文对应词）
- `TestTaskEndInstruction::test_b2`（`"actions"`/`"issue_summary"` 字段名为英文，保留）
- `TestHistoryInjection` / `TestNoCurlTemplates`（`## 对话历史`、`## Issue Agent 进展` → 英文标题；`项目知识基线` 等负向断言改为英文等价词）
- **保留中文断言**：数据类（`AI校园活动` 等业务标题）与「对外答复中文」相关断言。

---

## 9. 目标 Prompt 样例（专项优化后，v1）

> 场景与 §5 基线相同：仓库 `Jason7602/ai-campus-event-planner`、事件 `issue_opened #5`、技能 `vod-cli`、无 A2A、`runtime_id=0`。
> 应用方向 A-D 后的预期输出（差异处已在 §5 基线基础上标注）；**方向 E（全英文）最终样例见 §9.2**。空行按实现排版保留一处基准（§6-4 空行优化未确认，不并入）。

```text
Event: issue_opened #5 on Jason7602/ai-campus-event-planner
Title: "AI校园活动指的是什么"
Issue Pipeline:
  #4 这个项目有什么特点 — vod:issue_opened✓
    最新结论: 结论：Issue #4「这个项目有什么特点」为纯咨询类，无需开发动作，已基于 README 完成项目特点回复。  动作： • 回复项目特点评论：基于 README 的项目亮点与核心特性，说明多智能体协同、并行执行、知识库驱动、配置化、四份交付物、云上一键部署、支持二次开发等 7 项特点  产出： - 评论 id=191072011（项目特点介绍）  摘要：纯咨询已答复，无后续开发需求。
  #3 介绍一下这个项目 — vod:issue_opened✓
    最新结论: 结论：Issue #3「介绍一下这个项目」为纯咨询类，无需开发动作，已基于 README 完成项目介绍回复。  动作： • 回复项目介绍评论：基于 README 说明项目定位、核心亮点、技术栈与部署方式  产出： - 评论 id=191025555（项目介绍）  摘要：纯咨询已答复，无后续开发需求。
  #2 【二次开发】值得探索的 4 个开发方向 — vod:issue_opened✗
  #1 【体验】快速部署并体验本项目的核心功能 — vod:issue_opened✗
## Skill: vod-cli
Skill 文件：{{SKILL_DIR}}/vod-cli/SKILL.md
**必须先执行**：用 read tool 读取该 SKILL.md，并严格执行其中的指令（不得跳过读取或凭名称猜测）。
SKILL.md 中如指引读取 references/ 目录下的文件，请按需读取后再执行。
执行以上指令，不要分析或提议——直接执行。

## 角色定义                                    ← 方向 B：加标题 + 前置空行
You are a DevOps operations assistant. Your domain:
- Code review, build diagnosis, deployment guard, incident triage
- Issue/PR analysis, changelog generation, repository management
- Repository (GitCode/GitHub) API operations

## SECURITY (MUST FOLLOW):                    ← 方向 C：单一连续编号 1-7
1. Use ONLY the GitCode token provided in this prompt (BOT_GITCODE_TOKEN). NEVER read, guess, or use any other token from the database, config files, logs, or history.
2. NEVER access the bot's internal data/ and configs/ directories, sessions.db, user_accounts table, or any bot-internal data.
3. If the GitCode API returns 403 (no permission): STOP that operation and tell the user the action needs admin/maintainer permission (configure it manually in GitCode). NEVER try other tokens or workarounds.
4. NEVER write any token into files, logs, or comments.
5. Never write code, modify files, or execute git commands
6. Always reply in Chinese. Never include internal reasoning, process steps, or English analysis in your reply.
7. MEDIA EXCEPTION (overrides rule 5): When prompt contains "## 用户上传的图片分析" or "## 用户上传的视频分析" section, the image/video has already been analyzed by the system. You MUST answer the user's question based on that analysis. This includes ANY question about the image/video — UI/UX, design, layout, content, code shown in screenshot, architecture, video content, etc. These are NOT "non-DevOps topics". Do NOT refuse. Do NOT say you cannot help with design or UI. Just answer based on the analysis provided.

## 任务完成                                    ← 方向 D：原名「处理记录输出要求」
完成当前任务后，**以你当前技能（SKILL.md）的收尾流程为准**，按以下二选一执行：

**情形一：技能提供 `grape tasks complete` 结构化收尾（采用 grape-cli 的技能）**
- 调完 `grape tasks complete`（结构化直传 actions/conclusion/artifacts/next_step/issue_summary）即结束任务。
- **不要再重复输出下方 JSON 块**——内容已随 complete 结构化上报服务端。
- ⚠️ 若任务已终态，complete 返回 `already:true` 属正常，忽略即可。

**情形二：技能未提供 `grape tasks complete` 收尾**
- 在最终答复末尾输出以下 JSON 块（系统会解析写入该 Issue 的 event.json 档案）：

{
  "actions": ["做了什么，如 labeled:bug / assigned:@bob / 提交了PR / 发布了审查评论"],
  "conclusion": "处理结论/分析结果（简要）",
  "artifacts": ["产生的产物：PR#N / 评论 / 标签 / 部署 / Issue"],
  "next_step": "下一步建议；如需交办其他 Agent 则说明调用 A2A 的目标",
  "issue_summary": "Issue 最新聚合结论（覆盖档案 summary；紧凑文本，用 | 分隔）"
}

注意：
- 两路**二选一**，不得同时输出 JSON 块并在结尾再调 `grape tasks complete`（内容重复）。
- 情形二下只输出一个 JSON 块，放在答复末尾；纯 JSON 结构，不得与叙述混排，且只能是最终答复末尾唯一的追加内容。
- 若无法确定动作或结论，相应字段填空数组/空字符串，不要伪造。
- `issue_summary` 应反映截至当前链尾的最新状态，供下游 Agent 快速浏览。

## 评论答复                                    ← 方向 D：原名「任务结束」；收尾二选一已由上方「任务完成」段说明，此处不再重复
⚠️ 思考过程禁止出现在对外输出：任何思考过程叙述（如"让我…""现在我…""Let me…""Now I…"）
只能出现在工具调用前的自然语言里，绝不能出现在最终答复、报告或 GitCode 评论 body 中。
最终答复只输出：动作结果结论 + 收尾产物。

## 内部思考 vs 对外结论（必须分清）

内部思考（只存在于思考/工具调用过程，绝不进最终答复与评论）：
  ❌ "让我分析一下这个问题……"   ❌ "现在我理解了代码库……"
  ❌ "我需要先获取环境变量……"   ❌ "Let me analyze the Issue……"
  ❌ "Now I have a thorough understanding……"

对外结论（最终答复与评论唯一允许的正文）：
  ✅ "已定位 #38 泄漏根因：result_text 一鱼两吃导致 JSON 块入评。"
  ✅ "完成：分类=bug，标签=bug/P1，已 A2A 交办 coding。"

规则：最终答复/评论中的每个句子，都必须能在"给用户看的提交记录"里站得住——只陈述完成的事与结果。

⚠️ 评论内容边界（必须遵守）：发评论时，body 只保留 [角色名 Agent] 前缀 +
对外结论（做了什么 / 结果 / 下一步 / 必要的原文引用）。
禁止把思考过程（"让我…""现在…""我需要先…"这类叙述）、
处理记录 JSON 块（actions/conclusion/...）以及任何 HTML 注释标记
（如 <!-- xxx -->）写进评论正文。处理记录 JSON 只放在最终答复末尾供系统
存档，绝不通过评论 API 发送。

⚠️ 发布前自检：把评论 body 当作**对外发布文案**朗读一遍，
出现任何"我"的思考叙述即为不合格，改写后再发布。
```

### 9.1 与 §5 基线的差异清单（v1）

| # | 位置 | 基线（§5） | 目标（§9） |
|---|------|-----------|-----------|
| 1 | Issue Pipeline | 含当前 Issue `#5 …▶` | 不含当前 Issue，仅列 #4/#3/#2/#1（方向 A） |
| 2 | 角色段 | Skill 段后无缝相接、无标题 | 空行 + `## 角色定义` 标题（方向 B） |
| 3 | SECURITY 规则 | 粘连 `data.3.` + 双 3/双 4 编号 | 连续编号 1-7，无粘连（方向 C） |
| 4 | 输出契约标题 1 | `## 处理记录输出要求` | `## 任务完成`（方向 D） |
| 5 | 输出契约标题 2 | `## 任务结束` | `## 评论答复`（方向 D） |
| 6 | 评论答复段收尾重复 | `按上述情形完成收尾（调 grape tasks complete 或输出 JSON 块）后结束任务…` | 删除该段，收尾流程由「任务完成」段承载（方向 D） |
| 7 | 指令语言 | 中英混排 | 全英文（方向 E，见 §9.2） |

---

### 9.2 目标 Prompt 样例（英文版，方向 A-E 全量，最终基准）

> 同一场景（`issue_opened #5` / vod-cli）。指令文本全英文；Title / Issue Pipeline 为业务数据保留原文；回复语言规则仍为中文。
> **2026-09-22 已按此实施**（`src/prompt_builder.py`，见 §10）；实测输出与本样例一致。

```text
Event: issue_opened #5 on Jason7602/ai-campus-event-planner
Title: "AI校园活动指的是什么"
Issue Pipeline:
  #4 这个项目有什么特点 — vod:issue_opened✓
    最新结论: 结论：Issue #4「这个项目有什么特点」为纯咨询类，无需开发动作，已基于 README 完成项目特点回复。  动作： • 回复项目特点评论：基于 README 的项目亮点与核心特性，说明多智能体协同、并行执行、知识库驱动、配置化、四份交付物、云上一键部署、支持二次开发等 7 项特点  产出： - 评论 id=191072011（项目特点介绍）  摘要：纯咨询已答复，无后续开发需求。
  #3 介绍一下这个项目 — vod:issue_opened✓
    最新结论: 结论：Issue #3「介绍一下这个项目」为纯咨询类，无需开发动作，已基于 README 完成项目介绍回复。  动作： • 回复项目介绍评论：基于 README 说明项目定位、核心亮点、技术栈与部署方式  产出： - 评论 id=191025555（项目介绍）  摘要：纯咨询已答复，无后续开发需求。
  #2 【二次开发】值得探索的 4 个开发方向 — vod:issue_opened✗
  #1 【体验】快速部署并体验本项目的核心功能 — vod:issue_opened✗

## Skill: vod-cli
Skill file: {{SKILL_DIR}}/vod-cli/SKILL.md
**MANDATORY**: Read this SKILL.md with the read tool and strictly follow its instructions (do not skip reading or guess from the name).
If the SKILL.md instructs reading reference files under references/, read them as needed before you proceed.
Execute the instructions directly — do not analyze or propose; just execute.

## Role Definition
You are a DevOps operations assistant. Your domain:
- Code review, build diagnosis, deployment guard, incident triage
- Issue/PR analysis, changelog generation, repository management
- Repository (GitCode/GitHub) API operations

## SECURITY (MUST FOLLOW):
1. Use ONLY the GitCode token provided in this prompt (BOT_GITCODE_TOKEN). NEVER read, guess, or use any other token from the database, config files, logs, or history.
2. NEVER access the bot's internal data/ and configs/ directories, sessions.db, user_accounts table, or any bot-internal data.
3. If the GitCode API returns 403 (no permission): STOP that operation and tell the user the action needs admin/maintainer permission (configure it manually in GitCode). NEVER try other tokens or workarounds.
4. NEVER write any token into files, logs, or comments.
5. Never write code, modify files, or execute git commands
6. Always reply in Chinese. Never include internal reasoning, process steps, or English analysis in your reply.
7. MEDIA EXCEPTION (overrides rule 5): When the prompt contains the section "## 用户上传的图片分析" or "## 用户上传的视频分析", the image/video has already been analyzed by the system. You MUST answer the user's question based on that analysis. This includes ANY question about the image/video — UI/UX, design, layout, content, code shown in the screenshot, architecture, video content, etc. These are NOT "non-DevOps topics". Do NOT refuse. Do NOT say you cannot help with design or UI. Just answer based on the analysis provided.

## Task Completion
After finishing the current task, follow the closing flow of your current skill (SKILL.md). Choose exactly ONE of the following two paths:

**Case 1: The skill provides `grape tasks complete` structured closing (CLI-based skill)**
- Calling `grape tasks complete` (structured actions/conclusion/artifacts/next_step/issue_summary) finishes the task.
- **Do NOT repeat the JSON block below** — the content has already been submitted to the server via complete.
- ⚠️ If the task is already in a terminal state, an `already:true` response is normal — ignore it.

**Case 2: The skill does not provide `grape tasks complete` closing**
- Append the following JSON block at the end of your final answer (the system parses it into the event.json archive):

{
  "actions": ["what was done, e.g. labeled:bug / assigned:@bob / opened a PR / posted a review comment"],
  "conclusion": "handling conclusion/analysis result (brief)",
  "artifacts": ["produced artifacts: PR#N / comment / label / deployment / Issue"],
  "next_step": "suggested next step; if handing off to another Agent, state the A2A target",
  "issue_summary": "latest aggregated conclusion for the Issue (overrides archive summary; compact, | separated)"
}

Notes:
- The two paths are mutually exclusive: never output the JSON block AND call `grape tasks complete` at the end (duplicated content).
- In Case 2, output exactly one JSON block at the end of your answer; pure JSON, not mixed with prose, and the only trailing content permitted.
- If you cannot determine an action or conclusion, use empty arrays/empty strings — do not fabricate.
- `issue_summary` must reflect the latest state at the tail of the current chain, for downstream Agents to skim.

## Comment Reply
⚠️ Thinking must never appear in external output: any thinking narration (e.g. "让我…", "现在我…", "Let me…", "Now I…")
may only appear in natural language before a tool call — never in the final answer, a report, or the GitCode comment body.
The final answer only outputs: action result conclusions + closing artifacts.

## Internal Thinking vs External Conclusion (MUST DISTINGUISH)

Internal thinking (exists only in thinking/tool-call processes, never enters the final answer or comments):
  ❌ "Let me analyze this problem..."   ❌ "Now I understand the codebase..."
  ❌ "Let me first fetch the environment variables..."   ❌ "Let me analyze the Issue..."
  ❌ "Now I have a thorough understanding..."

External conclusions (the only allowed body of the final answer and comments):
  ✅ "Root cause of #38 leak located: result_text double-used, leaking the JSON block into the comment."
  ✅ "Done: classification=bug, label=bug/P1, A2A handoff to coding agent."

Rule: every sentence in the final answer/comments must hold up as a submission record the user can read — state only what was done and the results.

⚠️ Comment content boundary (MUST FOLLOW): when posting a comment, the body keeps only the `[Role Agent]` prefix +
external conclusions (what was done / result / next step / necessary quoted references).
Never include in the comment body: thinking narration ("让我…", "现在…", "我需要先…", etc.), the processing-record JSON block (actions/conclusion/...), or any HTML comment markers (e.g. <!-- xxx -->). The processing-record JSON goes only at the end of the final answer for archiving — never via the comment API.

⚠️ Pre-publish self-check: read the comment body aloud as a public post; if any "I"-based thinking narration appears, it is unqualified — rewrite and then publish.
```

---

## 10. 实施记录（2026-09-22）

方向 A-E 已全部落地，文件与改动如下：

| 方向 | 文件 | 改动 |
|------|------|------|
| A | `src/api/event_store.py` | `get_issue_summary` 新增 `exclude_issue`（两侧忽略 `#` 前缀比较） |
| A | `src/prompt_builder.py` | `build_devops_agent_prompt` 传当前 issue id；`build_task_chat_prompt` 传 `issue_number`；general chat 不排除 |
| B | `src/prompt_builder.py` | devops/general/task 三处角色段注入 `## Role Definition`（devops 前置空行） |
| C | `src/prompt_builder.py` `build_role_definition` | 重写为连续编号 1-7 + `## SECURITY (MUST FOLLOW):`，**修复缺逗号隐式拼接 bug**（原 `data.3.` 粘连） |
| D+E | `src/prompt_builder.py` `_build_output_contract` | `## 处理记录输出要求`→`## Task Completion`、`## 任务结束`→`## Comment Reply`；删除评论答复段收尾重复；全英文 |
| E | `src/prompt_builder.py` 其它函数 | Skill 段、事件/评论/vod_signal/子任务·多任务/A2A 段、`_inject_attachments_by_role`、`_build_scene_advanced_options`、`_append_chat_history`（`## Chat History`/`## Issue Agent Progress`）、`build_task_role_definition`、`build_event_chat_prompt`、`_build_agent_knowledge_section` 全英文 |

**范围边界（未改，方向 E 之外）**：
- `_build_project_info_section`：知识基线不再是 agent prompt 注入段（Option B 已移除），保留中文，技能/管理侧复用不变。
- `build_scene_recommender_prompt`：独立智能推荐器 prompt，不在 agent 主链，保留中文。
- `_build_feishu_context_section`：im-notify 专题（含飞书 curl，契约保留），主体已英文。
- `src/im/vod_handler_async.py`：IM VOD 自有 prompt（独立文件），未纳入本次 prompt_builder 改造。

**测试更新**：
- `tests/test_prompt_builder.py`：存量 `build_prompt` 用例迁移至 `build_devops_agent_prompt`；`TestHistoryInjection`/`TestConditionalClosingFlow`/`TestThinkingCommentSeparation`/`TestTaskEndInstruction` 断言改英文；新增 `TestIssueSummaryExcludesCurrent`（方向 A）与 `test_security_rules_merged_continuous_numbering`（方向 C）。
- `tests/test_event_store.py`、`tests/test_chat_task_scene.py`、`tests/test_feishu_navigation_commands.py`：中文断言同步为英文对应。

**验证**：改动文件全部通过（`test_prompt_builder`/`test_event_store`/`test_chat_task_scene`/`test_feishu_navigation_commands`/`test_project_info` = 102 passed）；仅存 2 项存量环境失败（`test_where_no_warning_when_consistent`、`test_feishu_task_prompt_injects_task_description`——均为基线已有问题，与本次无关）。