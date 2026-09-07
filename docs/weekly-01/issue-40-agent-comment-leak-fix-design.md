# Issue #40 修复方案：DevOps Agent 将思考过程/处理记录输出到 GitCode Issue 评论

> Issue：https://gitcode.com/grape-dev/grape/issues/40
> 类型：P1 Bug（必现） | 状态：spec-ready（待 coding 实现）
> 作者：VOD/Issue Agent 根因分析 + 本地代码复核
> 日期：2026-09-05
> 方向修订：**本方案以「Prompt 修复/强化」为核心优化手段**；框架后处理、工具收敛等留待后续重构（见 §七）。

## 一、问题概述

DevOps Agent（VOD/Issue/Coding/QA/SRE 等）在 GitCode Issue 发布评论时，将**内部思考过程**（"让我…""现在我…""我需要先…""Let me…""Now I…"）与**处理记录 JSON 块**（`actions/conclusion/artifacts/next_step/issue_summary`）写入评论正文。

已确认实例（Issue 正文列举 + 本次复核）：
- **#35**：`[VOD Agent] 现在我已经彻底理解了代码库。让我分析一下这个问题并发布我的发现。`
- **#37**：`[ISSUE Agent] 我需要先获取环境变量和 Issue 详情…`
- **#37**：`[VOD Agent] 现在我已经完成了根因分析。让我把结构化总结写入 Issue 并发布评论。`
- **#38**：`[VOD Agent] Now I have a thorough understanding of the codebase. Let me analyze…`
- **#38**：评论泄漏 `actions/conclusion` 等处理记录 JSON 块。
- **#40（自查铁证）**：处理本 Issue 的 ISSUE Agent 总结评论**自己就泄漏了完整 JSON 块**（`actions/conclusion/artifacts/next_step/issue_summary`），正是本 Issue 要求禁止的内容。

## 二、评论发布链路与 Prompt 现状（代码复核）

### 2.1 两条发布通道

| 通道 | 位置 | 说明 | 本轮可控 |
|------|------|------|---------|
| ① 框架回帖 | `src/api/event_pipeline.py:_post_result_to_issue`（L2057-2093） | Agent 执行完成后自动回帖，`f"[{agent_type.upper()} Agent] {result_text[:3000]}"` 直发，无内容清洗 | ✅ |
| ② Agent 技能 curl | Agent 通过技能直接 `POST /issues/{n}/comments` | `_sync_issue_comments_to_conversation`（L2139）注释明确：VOD 等 agent 通过技能 curl 直接在 Issue 上发评论 | 依赖 LLM 遵守 |

### 2.2 现有 Prompt 中的评论/输出指令（原文复核，红线标注）

**角色定义 `build_role_definition`（L156-175，红线 L173）：**

```
Line 173: "4. Always reply in Chinese. Never include internal reasoning, process steps, or English analysis in your reply."
```

→ 已有英文式禁令，但被违反。

**`issue_comment` 事件分支（L371-388，红线 L387-388）：**

```
Line 387: 必须回复该评论：使用 POST /repos/{owner}/{repo}/issues/{n}/comments，
          body 按你的技能规定的评论前缀开头，并引用用户评论原文。
Line 388: ⚠️ **Issue 评论格式规范（必须遵守）**：评论 body 开头必须用 `[角色名 Agent]` 做前缀
          （如 `[Coding Agent]`、`[QA Agent]`、`[SRE Agent]`、`[Issue Agent]`、`[VOD Agent]`），
          角色名首字母大写。**禁止**使用 skill 名、仓库名或其他自定义前缀。
```

→ 仅约束**前缀格式**，无正文边界/思考过程禁令。

**M1 结构化产出契约 + 执行纪律（L544-585，红线 L573-574 与 L579-584）：**

```
Line 544-545: ## 处理记录输出要求
Line 550-556: 完成当前任务后，在最终答复末尾输出以下 JSON 块（系统会解析写入该 Issue 的 event.json 档案）：
  { "actions": [...], "conclusion": "...", "artifacts": [...], "next_step": "...", "issue_summary": "..." }

Line 563-566: ## 任务结束
  ... 输出上述最终 JSON 块并**结束任务**。不要继续思考、不要等待用户输入、不要追加额外内容……

Line 570-571: ## 执行纪律
  你必须在单个对话轮次内完成全部操作：读取文件 → 分析 → 执行动作
  （发评论/加标签/A2A 交办等）→ 输出处理记录 JSON → 结束。

Line 573-574: ⚠️ 禁止"分析完就结束"：如果你在回复中说"Let me..."或"现在我将..."
  但实际没有调用工具执行该操作，任务将被判定为未完成并重新执行。

Line 576-577: ⚠️ 必须调用工具执行实际动作：发评论用 execute_code 调 GitCode API，
  不要只输出评论内容就 end_turn。

Line 579-584: ⚠️ 评论内容边界（必须遵守）：发评论时，body 只保留 [角色名 Agent] 前缀 +
  对外结论（做了什么 / 结果 / 下一步 / 必要的原文引用）。
  禁止把思考过程（"让我…""现在…""我需要先…"这类叙述）、
  处理记录 JSON 块（actions/conclusion/...）以及任何 HTML 注释标记
  （如 <!-- xxx -->）写进评论正文。处理记录 JSON 只放在最终答复末尾供系统
  存档，绝不通过评论 API 发送。
```

**框架回帖 `_post_result_to_issue`（`event_pipeline.py` L2063）：**

```
Line 2063: _comment = f"[{agent_type.upper()} Agent] {result_text[:3000]}"
Line 2064: _comment += "\n<!-- grape-auto-comment -->"
```

### 2.3 Prompt 现状小结

| 位置 | 现状 | 效力 |
|------|------|------|
| 角色定义 L173 | 英文式禁止 internal reasoning | 软约束，无效 |
| issue_comment L388 | 只约束前缀格式 | 不含正文边界 |
| 执行纪律 L579-584 | 评论边界指令与验收标准一致 | 软约束，无效 |
| 执行纪律 L573-574 | "说了 Let me…但没执行"判未完成 → 默许思考叙述出现在最终答复 | 反向放大泄漏 |
| `_post_result_to_issue` L2063 | result_text 原样发评论 | 框架无清洗 |

## 三、根因

1. **评论边界仅依赖 LLM 自觉**：prompt 里其实已有禁令（L579-584 甚至与本 Issue 验收标准一字不差），但 Agent 仍大面积违反（必现）→ 现有 Prompt 的禁令表达无效，需要**重写而非补丁式追加**。
2. **L573-574 反向指令**：把"说了 *Let me…/现在我将…* 但没执行动作"判定为未完成，等于放任/鼓励思考叙述进入最终输出，并随评论发布出去——这是思考类泄漏高频句式的**prompt 端根因**。
3. **正文边界从来不具象**：整个 prompt 只有抽象禁令，没有**正例/反例对照**、没有"评论即对外文案"的具象认知，LLM 无法把"思考流"与"对外结论"在输出层面分离。
4. **`result_text` 一鱼两吃（架构层根因）**：同一份输出既当档案 JSON 载体又当评论正文，末尾 JSON 块被框架照搬发出。**此项留待 §七 重构处理**，本轮用 Prompt 侧约束避免其进入发布通道。

## 四、修复方案（主线：Prompt 修复与强化）

> 方向：**彻底重写评论/思考边界指令**，用「统一章节 + 具象正反例 + 输出结构契约」替换现有散落的软约束；不新增框架代码。风险可控、见效快。

### 4.1 [P0] 重写「执行纪律」中评论边界（`src/prompt_builder.py` L573-584 区段）

- **删除/改写 L573-574 反向指令**：不再以"说了 Let me 判未完成"做判据，改为：
  > "**任何思考过程的叙述（如"让我…""现在我…""Let me…""Now I…"）都只能出现在工具调用前的自然语言中，绝不能出现在最终答复、报告或 GitCode 评论 body 里。**最终答复只输出：动作结果结论 + 处理记录 JSON 块。"
- **强化 L579-584** 为「对外评论硬边界」：
  > - 评论 body = `[角色名 Agent]` 前缀 + **纯对外结论**（做了什么 / 结果 / 下一步 / 必要的原文引用）。
  > - **禁止**：思考过程叙述、处理记录 JSON 块（actions/conclusion/...）、任何 HTML 注释标记（`<!-- xxx -->`）。
  > - **发布前自检**：把评论当**对外发布文案**朗读一遍，出现任何"我"的思考叙述即为不合格，改写后再发布。

### 4.2 [P1] 全量注入「对外结论/内部思考」分离范本（具象正反例）

在 `build_devops_agent_prompt`（`src/prompt_builder.py`）统一区段追加：

```
## 内部思考 vs 对外结论（必须分清）

内部思考（只存在于思考/工具调用过程，绝不进最终答复与评论）：
  ❌ "让我分析一下这个问题……"   ❌ "现在我理解了代码库……"
  ❌ "我需要先获取环境变量……"   ❌ "Let me analyze the Issue……"
  ❌ "Now I have a thorough understanding……"

对外结论（最终答复与评论唯一允许的正文）：
  ✅ "已定位 #38 泄漏根因：result_text 一鱼两吃导致 JSON 块入评。"
  ✅ "完成：分类=bug，标签=bug/P1，已 A2A 交办 coding。"

规则：最终答复/评论中的每个句子，都必须能在"给用户看的提交记录"里站得住——只陈述完成的事与结果。
```

### 4.3 [P1] 收敛 `result_text` 泄漏面（不加框架代码的版本）

- 在 `prompt_builder.py`「处理记录输出要求」（L544-566）强调：**JSON 块语法必须保持纯 JSON、不与叙述混排，且只作为最终答复末尾的唯一追加内容**——便于后续重构期剥离；本轮不落正则/解析逻辑。
- ⚠️ 各 Agent SKILL.md 相关改动**不包含在本仓库**：skills 已迁移至专门仓库，由该仓库单独跟踪（此处不再修改）。

### 4.4 回归测试（Prompt 层）

- 新增 `tests/test_prompt_builder.py` 用例：
  - `build_devops_agent_prompt` 输出**负向断言**：不含反向指令原文（`If you say "Let me..."` 语义消失）。
  - 输出**正向断言**：含对外结论范本、四类禁止项、发布前自检语句。
- 回归：现有 prompt/agent prompt 相关测试全绿。

## 五、改动清单

| # | 模块 | 改动 | 优先级 |
|---|------|------|--------|
| 1 | `src/prompt_builder.py` L573-584 | 重写执行纪律：删除反向指令，强化对外评论硬边界 | P0 |
| 2 | `src/prompt_builder.py` 统一区段 | 注入「内部思考 vs 对外结论」具象正反例范本 | P1 |
| 3 | `src/prompt_builder.py` L544-566 | 处理记录 JSON 保持纯 JSON、末尾唯一追加 | P1 |
| 4 | `tests/test_prompt_builder.py` | 正向/负向断言 + 回归 | P1 |

**不包含（后续重构）**：`src/vcs/comment_sanitize.py` 清洗器、`_post_result_to_issue` 后处理、工具层拦截 curl → `post_issue_comment` 受控工具、`result_text` 与评论正文调用链解耦。

## 六、验收标准

- [ ] 新评论正文不再出现思考过程叙述（让我/现在我/我需要先/Let me/Now I…）
- [ ] 处理记录 JSON 块不进入评论正文（含通道路径①——以 Prompt 约束为主，重构前允许存在残余概率并持续跟踪）
- [ ] HTML 注释标记不进入评论正文
- [ ] 新增 `test_prompt_builder.py` 用例通过（负向：无反向指令；正向：含正反例范本）
- [ ] 现有测试全绿
- [ ] 对 #35/#37/#38/#40 的新评论格式符合规范

## 七、后续重构（out of scope，另开会话跟踪）

| 项 | 目标 | 说明 |
|----|------|------|
| 框架后处理 | `_post_result_to_issue` 前剥离 JSON 块/思考叙述/内部注释（`comment_sanitize` 清洗器） | Prompt 失效时的兜底防线 |
| `result_text` 调用链解耦 | 档案输出与评论正文分离，评论只取结构化结论字段 | 根治一鱼两吃 |
| 工具收敛 | 拦截裸 curl `POST /issues/{n}/comments` → 受控 `post_issue_comment` 工具 | 堵死通道②，使后处理可覆盖全部评论 |

> 本轮为快速见效的 Prompt 治疗；上述为结构性根治，避免与 P0 修复耦合、降低本轮回归风险。

## 八、风险

| 风险 | 影响 | 缓解 |
|------|------|------|
| 纯 Prompt 约束后 LLM 仍偶发泄漏（尤其通道② curl） | 中 | 本轮验收聚焦"新评论规范符合"；残余泄漏由 §七 框架后处理根治，另开会话跟踪 |
| 重写执行纪律可能影响既有判定逻辑（"未完成重执行"） | 中 | 只改判据（思考叙述 ≠ 未完成），保留"必须执行工具动作"主线；测试负向断言护栏 |
| 正反例范本被 Agent 当模板照抄产生雷同评论 | 低 | 范本定位为"格式示范"，正文仍需按 Issue 实际结论写 |

## 九、处理流水线状态

- ✅ VOD Agent：根因分析已结构化（四要素，4 实例）
- ✅ Issue Agent：评级 ready，Spec 已发布（原方案=Prompt 强化 + 框架后处理双保险），已打 `bug/P1/triaged/spec-ready`，A2A 交办 coding
- ⚠️ A2A coding 交办：**本方案修订为「Prompt 修复与强化」主线**，需同步更新 Spec 说明后再实施；框架后处理/工具收敛转 §七 后续重构

## 附：关键代码位置

- 框架回帖：`src/api/event_pipeline.py` `_post_result_to_issue`（L2057-2093），body 拼接在 L2063
- Agent curl 自由发评论：`src/api/event_pipeline.py` `_sync_issue_comments_to_conversation`（L2139）注释即证据
- 回环检测（仅标记判别）：`src/api/event_pipeline.py` L709-715
- 评论前缀 Prompt：`src/prompt_builder.py` L388
- 评论内容边界指令（已有但被违反）：`src/prompt_builder.py` L579-584
- 反向指令（Let me…判未完成）：`src/prompt_builder.py` L573-574
- M1 处理记录 JSON 契约：`src/prompt_builder.py` L544-566
- 角色定义英文禁令：`src/prompt_builder.py` `build_role_definition` L173
- GitCode 评论 API：`POST /api/v5/repos/{owner}/{repo}/issues/{n}/comments`