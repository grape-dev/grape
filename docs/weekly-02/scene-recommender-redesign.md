# 智能推荐流程重构方案

## 一、问题分析

### 旧流程

```
issue_opened → VOD Agent（vod skill）
                    ├─ 读取项目知识基线
                    ├─ 读取 Issue 历史
                    ├─ 需求引导漏斗（≤3 轮多轮对话）
                    ├─ 场景推荐（prompt 注入场景列表 → 输出 recommended_scene）
                    ├─ 结构化建/总结 Issue
                    └─ A2A 交办 Issue Agent
```

VOD 同时承担 **需求澄清** + **场景推荐** 两个职责，耦合在一起：
- 场景推荐依赖 VOD 完成全部分析后才能输出，延迟大
- VOD 多轮对话期间场景未确定，后续流水线无法并行准备
- `allow_scene_override` 开关、`recommended_scene` 正则兜底解析等机制复杂且脆弱
- 场景列表注入（`_build_vod_scenes_section`）与项目知识基线绑定，任一异常场景列表消失

### 新流程目标

将场景推荐拆为**独立前置步骤**（仅智能推荐模式触发），轻量执行后立即确定场景，再走完整流水线。用户手动选择场景时不触发推荐，直接走用户选的场景。

---

## 二、推荐触发判断

### 现有机制（复用）

`issue_state.allow_scene_override` 字段已在建任务时由 `tasks.py` 设置：

| 用户操作 | allow_scene_override | issue_state.scene_id | 新流程行为 |
|----------|---------------------|---------------------|-----------|
| 选具体场景 | False（0） | 用户选的 scene_id | **不触发推荐**，直接用该 scene_id 走 VOD |
| 选"智能推荐"（旧 vod-auto） | True（1） | dev_scene 初始值 | **触发推荐**，recommender 输出后覆盖 scene_id |

### 改动

- `allow_scene_override` 字段**保留**，语义从"允许 VOD 覆盖场景"变更为"智能推荐模式开关"
- 前端 `scene_vod_auto` 文案从"VOD 智能推荐"改为"智能推荐"（zh/en 两个 locale 文件）
- 日志/prompt 中所有"VOD 场景推荐"统一改为"智能推荐"

---

## 三、新流程设计

### 流程时序

**智能推荐模式**（allow_scene_override=True）：

```
┌─────────────┐     ┌──────────────────┐     ┌─────────────────┐     ┌──────────────┐
│ issue_opened│────▶│ 智能推荐任务      │────▶│ 系统持久化       │────▶│ VOD (场景技能)│
│ webhook     │     │ (prompt 内联)    │     │ scene_id 到     │     │ 完整需求分析  │
│             │     │                  │     │ issue_state     │     │ A2A → issue  │
│             │     │                  │     │                 │     │ → coding...  │
└─────────────┘     └──────────────────┘     └─────────────────┘     └──────────────┘
                     唯一职责：               自动执行，               场景已确定，
                     匹配 scene_id            无需 A2A                走该场景完整流水线
```

**手动选择模式**（allow_scene_override=False）：

```
┌─────────────┐     ┌──────────────────────────────────────┐
│ issue_opened│────▶│ VOD (用户选定场景的 vod 技能)          │
│ webhook     │     │ scene_id 已在 issue_state 中          │
│             │     │ 完整需求分析 → A2A → issue → coding... │
└─────────────┘     └──────────────────────────────────────┘
                     跳过推荐，直接走流水线
```

### 阶段说明

#### 阶段 0：触发判断（issue_opened 路由后）

```python
if event_type == "issue_opened":
    agent_type = "vod"
    issue_id = resolve_issue_id(payload) or evt.get("issue_id", "")
    # 读取推荐开关
    from src.api.session_store import get_scene_override_flag
    use_recommend = get_scene_override_flag(repo_key, issue_id)
    
    if use_recommend:
        # 智能推荐模式 → 派发 recommender 任务
        skill_name = ""  # 无 skill，纯 prompt 注入
        prompt = build_scene_recommender_prompt(evt, repo_key)
    else:
        # 手动选择模式 → 直接用用户选的 scene_id 的 vod 技能
        scene_id, _ = get_issue_scene(repo_key, issue_id)
        vod_node = get_first_node(scene_id, repo_key)  # 需传 repo_key
        skill_name = vod_node.get("skill", "vod") if vod_node else "vod"
        prompt = build_devops_agent_prompt(evt, ...)  # 正常 VOD prompt
```

#### 阶段 1：智能推荐任务派发（仅推荐模式）

- 触发：`issue_opened` + `allow_scene_override=True`
- 路由：`routing.py` → `agent_type = "vod"`（不变）
- 技能：无 skill 文件，指令全部内联到 prompt（`build_scene_recommender_prompt()`）
- Prompt：极简，只注入场景列表 + Issue 标题/描述 + 完整角色定义/匹配规则/输出格式
- 执行：与普通 agent 任务相同的引擎派发机制（pick_engine + enqueue）
- issue_state 初始化：`set_issue_stage(repo_key, issue_id, "vod", "vod")`（不变）

#### 阶段 2：推荐完成 → 场景持久化 → VOD 重派

在 `event_pipeline.py` 的 `done` 回调中，检测当前任务是否为推荐任务（通过 task prompt 中的特征标记或 task 元数据）：

```python
if evt_type == "done" and agent_type == "vod" and final_text and issue_id:
    # 判断是否为推荐任务：检查 issue_state.allow_scene_override 且 task 无 skill_name
    # 或在 enqueue 时打标 task.metadata = {"is_scene_recommend": True}
    if _is_scene_recommend_task(task_id):
        # 解析 scene_id
        scene_id = _parse_recommender_output(final_text)
        if scene_id:
            set_issue_scene(repo_key, issue_id, scene_id, scene_user_id=...)
            # 重派 VOD（用匹配场景的 vod 技能）
            vod_node = get_first_node(scene_id, repo_key)
            vod_skill = vod_node["skill"]
            _redispatch_vod(repo_key, issue_id, vod_skill, evt, ...)
        else:
            log.info("智能推荐: 未匹配到场景，流水线终止")
        return  # 不走后续 VOD done 逻辑
    # else: 普通 VOD done 逻辑（A2A 解析等，不再有 recommended_scene 解析）
```

#### 阶段 3：VOD 完整工作流（两种模式汇合）

- 智能推荐模式：recommender 完成 → scene_id 已持久化 → 重派 VOD
- 手动选择模式：issue_opened 直接派 VOD（scene_id 已在 issue_state）
- 两种模式汇合：VOD 以正确场景的技能执行完整工作流
- VOD prompt **不再注入场景列表**
- VOD 不再输出 `recommended_scene` / `scene_reason`
- VOD 完成后正常 A2A 交办 Issue Agent → 后续流水线节点按场景 pipeline 执行

---

## 四、旧流程组件评估

### 保留（直接复用）

| 组件 | 文件 | 说明 |
|------|------|------|
| `get_scenes(repo_key)` | pipeline_config.py | 查询仓库已应用场景列表，recommender prompt 构建时复用 |
| `get_pipeline_config(scene_id, repo_key)` | pipeline_config.py | 查询场景 pipeline 配置，重派 VOD 时获取第一个节点 |
| `get_first_node(scene_id)` | pipeline_config.py | 获取场景第一个节点（vod 节点），需补充 repo_key 参数 |
| `set_issue_scene(repo_key, issue_id, scene_id)` | session_store.py | 持久化 scene_id 到 issue_state |
| `get_issue_scene(repo_key, issue_id)` | session_store.py | 读取 issue 级场景（手动模式用） |
| `set_issue_stage(repo_key, issue_id, "vod", "vod")` | session_store.py | issue_opened 时初始化 issue_state |
| `get_scene_override_flag(repo_key, issue_id)` | session_store.py | **保留**，语义变更为"智能推荐模式开关" |
| `set_scene_override_flag(repo_key, issue_id, allowed)` | session_store.py | **保留**，tasks.py 建任务时仍调用 |
| `dispatch_node()` | pipeline_executor.py | 节点派发机制，重派 VOD 时复用 |
| `pick_engine()` | engine_select.py | 引擎选择，推荐任务派发时复用 |
| `_build_project_info_section()` | prompt_builder.py | 项目知识基线注入，VOD prompt 仍需 |
| `build_devops_agent_prompt()` | prompt_builder.py | VOD prompt 构建（删除场景列表注入后复用） |
| VOD SKILL.md（需求分析/引导/A2A 部分） | skills/vod/vod/SKILL.md | 需求引导漏斗、结构化建 Issue、A2A 交办逻辑保留 |
| blog-vod / iac-vod SKILL.md | skills/vod/ | 各场景特化 VOD 技能保留 |

### 删除（不再使用）

| 组件 | 文件 | 位置 | 删除原因 |
|------|------|-----------|----------|
| `_build_vod_scenes_section()` | prompt_builder.py | L95-161 | 场景列表注入 VOD prompt 的逻辑被推荐任务取代 |
| 调用 `_build_vod_scenes_section` 的代码 | prompt_builder.py | L579-582 | VOD prompt 不再注入场景列表 |
| VOD done 场景推荐解析逻辑 | event_pipeline.py | L1954-1997 | `recommended_scene` / `terminate` / `reason` 正则解析全部删除 |
| `recommended_scene` A2A 持久化 | routes/webhook.py | L479-492 | A2A 不再携带 recommended_scene |
| VOD prompt 中 `recommended_scene` / `scene_reason` 输出指令 | prompt_builder.py | L148-156 | 随 `_build_vod_scenes_section` 一起删除 |

### 修改

| 组件 | 文件 | 修改内容 |
|------|------|----------|
| `event_pipeline.py` issue_opened 路由 | ~L820 | 新增推荐触发判断：allow_scene_override=True → 推荐任务；False → 直接 VOD |
| `event_pipeline.py` done 回调 | ~L1940 | 新增推荐完成检测 → 解析 scene_id → 持久化 → 重派 VOD |
| `prompt_builder.py` | 新增 `build_scene_recommender_prompt()` | 纯 prompt 内联，不依赖 skill 文件 |
| `prompt_builder.py` | 删除 `_build_vod_scenes_section()` 及调用 | VOD prompt 不再注入场景列表 |
| `routes/webhook.py` | L479-492 | 删除 `recommended_scene` A2A 持久化 |
| `routes/tasks.py` | L1009-1022 注释 | 注释中"VOD 可覆盖"改为"智能推荐模式" |
| 前端 locale zh | `locales/zh/tasks.json` L85 | `"scene_vod_auto": "VOD 智能推荐"` → `"scene_vod_auto": "智能推荐"` |
| 前端 locale en | `locales/en/tasks.json` L85 | `"scene_vod_auto": "VOD Auto Recommend"` → `"scene_vod_auto": "Smart Recommend"` |
| VOD SKILL.md | skills/vod/vod/SKILL.md | 删除场景推荐相关描述（如有） |

---

## 五、Prompt 设计（重点）

### 5.1 Prompt 三层结构

Prompt 由三层组成，每层职责不同、可改性不同：

```
┌─────────────────────────────────────────────────┐
│ 第一层：系统数据层（不可改，代码自动注入）          │
│  - Issue 标题 / 描述                              │
│  - 仓库已应用场景列表（scene_id / 名称 / 描述 / 技能链）│
├─────────────────────────────────────────────────┤
│ 第二层：用户经验规则层（可改，用户在设置中编辑）      │
│  - 兜底场景：语义匹配无果时走哪个场景              │
│  - 路由规则：遇到什么情况走什么场景（自然语言条件）    │
│  - 可留空（留空时纯靠 LLM 语义匹配 + 系统兜底）     │
├─────────────────────────────────────────────────┤
│ 第三层：固定指令层（不可改，代码内置）              │
│  - 角色定义 + 能力边界 + 匹配优先级                │
│  - 输出格式 + 硬约束                              │
└─────────────────────────────────────────────────┘
```

**为什么需要用户经验规则层：**

场景名称和描述不总是与实际用途相关的 — 一个叫"通用开发"的场景可能实际用于 IaC 任务，一个叫"dev"的场景可能实际用于博客。纯靠 LLM 对名称/描述做语义匹配会翻车。用户经验规则让用户把"什么任务该走什么场景"的判断沉淀下来，作为高优先级指令覆盖 LLM 的自由发挥。

### 5.2 设计原则

1. **纯 prompt 内联，不使用 skill 文件**：三层内容全部写在 `build_scene_recommender_prompt()` 返回的 prompt 中
2. **用户经验规则从 DB 读取**：用户可在仓库设置中编辑推荐规则，存入 DB，构建 prompt 时动态注入
3. **匹配优先级**：用户路由规则 > 语义匹配 > 用户兜底场景 > 系统兜底
4. **极简输入**：只注入场景列表 + Issue 标题/描述 + 用户规则，不注入知识基线/A2A 协议/输出契约/历史事件
5. **单一输出**：只输出一个 JSON 块，包含 scene_id + reason
6. **零副作用**：不调用任何 API、不发评论、不创建 Issue、不 A2A、不读写文件
7. **即时终止**：输出 JSON 后立即结束

### 5.3 能力边界

| 行为 | 允许 | 说明 |
|------|------|------|
| 读取 prompt 中注入的场景列表 | ✅ | 系统注入，无需主动查询 |
| 读取 prompt 中注入的任务标题与描述 | ✅ | 系统注入 Issue title/body |
| 读取 prompt 中注入的用户经验规则 | ✅ | 系统从 DB 读取注入 |
| 分析匹配并输出 scene_id | ✅ 必须 | 唯一职责 |
| 创建/修改 Issue | ❌ 禁止 | 无此能力 |
| 发送 Issue 评论 | ❌ 禁止 | 无此能力 |
| A2A 交办 | ❌ 禁止 | 无此能力 |
| 多轮对话/需求澄清 | ❌ 禁止 | 单轮即结束 |
| 读取项目知识基线 | ❌ 禁止 | 不注入 |
| 调用 GitCode API | ❌ 禁止 | 不注入 token |
| 读写文件 | ❌ 禁止 | 不需要 |
| 输出 JSON 以外的内容 | ❌ 禁止 | 严格 JSON only |

### 5.4 用户经验规则存储

新增 `managed_repos` 表字段 `scene_recommend_config`（JSON TEXT），存储**结构化数据**：

```json
{
  "fallback_scene": "general-dev",
  "rules": [
    {"scene_id": "blog-publish", "condition": "标题或描述涉及博客、文章、内容创作或发布"},
    {"scene_id": "iac", "condition": "标题或描述涉及 Terraform、IaC、基础设施或资源编排"},
    {"scene_id": "skill-dev", "condition": "标题或描述涉及技能开发、Skill 创建或测试"}
  ]
}
```

- `fallback_scene`：兜底场景 scene_id（**必填**，默认当前场景列表排序第一个）
- `rules`：路由规则数组，每条为 `{scene_id, condition}` 结构
  - `scene_id`（key）：**必须从当前仓库场景列表中选择**（下拉框，不可自由输入）
  - `condition`（value）：用户用自然语言描述"什么情况下推荐使用这个场景"
- 用户在项目场景页点击"智能推荐规则"按钮 → 弹窗编辑
- 全部规则留空（rules=[]）= 纯 LLM 语义匹配模式

**存储是结构化的，但 prompt 注入时重构为自然语言段落**（见 5.5 节）。

### 5.5 Prompt 完整结构示例

以下是一个实际注入的 prompt 示例（三层用注释标注来源）：

```
## 任务信息
- 标题：给博客系统添加评论功能
- 描述：需要在博客文章页面底部添加评论区，支持 GitCode 用户登录后评论

## 可用开发场景（该仓库已配置）
| scene_id | 名称 | 描述 | 技能链 |
|----------|------|------|--------|
| general-dev | 通用开发 | 通用 DevOps 开发流程 | vod → issue → coding → qa → sre |
| blog-publish | 博客发布 | 博客内容生成与发布 | blog-vod → blog-issue → ai-blog-generation → blog-publish |
| iac | IaC开发 | 基础设施即代码 | iac-vod → iac-issue → iac-coding → iac-qa → iac-sre |

## 用户经验规则
兜底场景：general-dev
路由规则：
1. 标题或描述涉及博客、文章、内容创作或发布 → 走 blog-publish 场景
2. 标题或描述涉及 Terraform、IaC、基础设施或资源编排 → 走 iac 场景
3. 标题或描述涉及技能开发、Skill 创建或测试 → 走 skill-dev 场景

## 角色
你是智能推荐器。唯一职责：根据任务描述，从上方场景列表中匹配最优场景，返回 scene_id。
不执行任何其他操作。

## 匹配优先级（从高到低）
1. 用户路由规则：检查任务标题/描述是否命中上方「用户经验规则」中的条件，命中则直接走对应场景
2. 语义匹配：任务描述与场景名称/描述的语义相关性
3. 用户兜底场景：以上都未命中时，走用户设置的兜底场景
4. 系统兜底：用户未设兜底场景时，选择技能链最通用的场景

## 能力边界
- 创建/修改/总结 Issue → 禁止
- 发送 Issue 评论 → 禁止
- A2A 交办 → 禁止
- 多轮对话/需求澄清 → 禁止
- 读取项目知识基线 → 禁止
- 调用 GitCode API → 禁止
- 读写文件 → 禁止
- 输出 JSON 以外的内容 → 禁止

## 输出格式
只输出以下 JSON，不输出任何其他内容（无分析、无解释、无叙述）：

{"scene_id": "场景列表中存在的 scene_id", "reason": "一句话选择理由（≤50字）"}

边界情况：
- 仓库无场景 → {"scene_id": "", "reason": "未配置场景"}
- 无法精确匹配 → 走兜底场景，reason 注明"未精确匹配，使用兜底场景"

## 硬约束
1. 只输出 JSON：答复中只能有一个 JSON 块，不得有任何前后文字。
2. 不调用任何 API：不访问 GitCode、不调用 A2A、不发评论、不查 Issue。
3. 不读取任何文件：所有信息已在 prompt 中注入。
4. scene_id 必须来自列表：只能填写 prompt 注入的场景列表中存在的 scene_id。
5. 用户路由规则优先于你的语义判断：规则命中时必须遵从，不要用语义匹配覆盖。
6. 任务立即结束：输出 JSON 后结束，不继续思考、不等待输入。
```

**不注入的内容**（与 `build_devops_agent_prompt` 的差异）：
- ❌ 项目知识基线（`_build_project_info_section`）
- ❌ Feishu 上下文（`_build_feishu_context_section`）
- ❌ A2A 协议说明（`agent-description.json`）
- ❌ 输出契约（`_build_output_contract`）
- ❌ 角色定义（`build_role_definition`）— 用内联的极简角色定义替代
- ❌ Issue 历史/summary
- ❌ 子任务/多任务模式上下文
- ❌ 场景高级选项
- ❌ Skill 文件指引（`{{SKILL_DIR}}`）

### 5.6 输出规范

**唯一合法输出格式**：

```json
{"scene_id": "匹配的场景ID", "reason": "一句话选择理由"}
```

规则：
1. 只输出上述 JSON 块，不输出任何分析、解释、叙述文字
2. `scene_id` 必须是场景列表中存在的 ID，禁止编造
3. `reason` 不超过 50 字，说明匹配依据（命中了哪条用户规则 / 语义匹配了哪个场景 / 走了兜底）
4. 禁止输出 `default`（无 default 场景）

边界情况：
- 仓库无场景 → `{"scene_id": "", "reason": "未配置场景"}`
- 无用户规则且语义匹配不明确 → 走用户兜底场景或系统兜底，reason 注明"未精确匹配"

### 5.7 系统侧 Prompt 构建函数

```python
def build_scene_recommender_prompt(evt, repo_key: str) -> str:
    """构建智能推荐专用 prompt（三层结构：系统数据 + 用户经验规则 + 固定指令）。
    
    用户经验规则从 managed_repos.scene_recommend_config (JSON) 读取。
    用户可在仓库设置页编辑 fallback_scene 和 rules。
    """
    payload = evt.get("payload", {})
    issue_data = {}
    if isinstance(payload.get("issue"), dict):
        issue_data = payload["issue"]
    elif isinstance(payload.get("object_attributes"), dict):
        issue_data = payload["object_attributes"]
    title = issue_data.get("title", "")
    body = issue_data.get("body", issue_data.get("description", ""))[:1000]

    parts = []

    # === 第一层：系统数据（不可改）===

    # 1. 任务信息
    parts.append("## 任务信息")
    parts.append(f"- 标题：{title}")
    parts.append(f"- 描述：{body}")

    # 2. 注入仓库所有已应用场景
    from src.api.pipeline_config import get_scenes, get_pipeline_config
    scenes = get_scenes(repo_key=repo_key)
    if not scenes:
        parts.append("\n## 可用开发场景\n> 该仓库未配置任何开发场景。")
        parts.append('\n输出：{"scene_id": "", "reason": "未配置场景"}')
        parts.append("\n## 硬约束\n1. 只输出 JSON，不得有任何前后文字。\n2. 任务立即结束。")
        return "\n".join(parts)

    parts.append("\n## 可用开发场景（该仓库已配置）")
    parts.append("| scene_id | 名称 | 描述 | 技能链 |")
    parts.append("|----------|------|------|--------|")
    for s in scenes:
        cfg = get_pipeline_config(s["id"], repo_key=repo_key)
        chain = " → ".join(n.get("skill", "") for n in cfg.get("nodes", [])) if cfg else "—"
        disp = s.get("display_name") or s.get("name") or s["id"]
        desc = s.get("description") or ""
        parts.append(f"| {s['id']} | {disp} | {desc} | {chain} |")

    # === 第二层：用户经验规则（可改，从 DB 读取结构化数据，注入时转为自然语言）===

    config = _load_scene_recommend_config(repo_key)
    parts.append("\n## 用户经验规则")
    # 兜底场景
    if config.get("fallback_scene"):
        parts.append(f"兜底场景：{config['fallback_scene']}")
    else:
        parts.append("兜底场景：未设置（由系统自动选择最通用的场景）")
    # 路由规则：结构化 {scene_id, condition} → 自然语言
    rules = config.get("rules") or []
    if rules:
        parts.append("路由规则：")
        for i, rule in enumerate(rules, 1):
            sid = rule.get("scene_id", "")
            cond = rule.get("condition", "")
            parts.append(f"{i}. {cond} → 走 {sid} 场景")
    else:
        parts.append("路由规则：未设置（纯语义匹配模式）")

    # === 第三层：固定指令（不可改，代码内置）===

    # 3. 角色定义
    parts.append("\n## 角色")
    parts.append("你是智能推荐器。唯一职责：根据任务描述，从上方场景列表中匹配最优场景，返回 scene_id。")
    parts.append("不执行任何其他操作。")

    # 4. 匹配优先级
    parts.append("\n## 匹配优先级（从高到低）")
    parts.append("1. 用户路由规则：检查任务标题/描述是否命中上方「用户经验规则」中的条件，命中则直接走对应场景")
    parts.append("2. 语义匹配：任务描述与场景名称/描述的语义相关性")
    parts.append("3. 用户兜底场景：以上都未命中时，走用户设置的兜底场景")
    parts.append("4. 系统兜底：用户未设兜底场景时，选择技能链最通用的场景")

    # 5. 能力边界
    parts.append("\n## 能力边界")
    parts.append("- 创建/修改/总结 Issue → 禁止")
    parts.append("- 发送 Issue 评论 → 禁止")
    parts.append("- A2A 交办 → 禁止")
    parts.append("- 多轮对话/需求澄清 → 禁止")
    parts.append("- 读取项目知识基线 → 禁止")
    parts.append("- 调用 GitCode API → 禁止")
    parts.append("- 读写文件 → 禁止")
    parts.append("- 输出 JSON 以外的内容 → 禁止")

    # 6. 输出格式
    parts.append("\n## 输出格式")
    parts.append('只输出以下 JSON，不输出任何其他内容（无分析、无解释、无叙述）：')
    parts.append('')
    parts.append('{"scene_id": "场景列表中存在的 scene_id", "reason": "一句话选择理由（≤50字）"}')
    parts.append('')
    parts.append("边界情况：")
    parts.append('- 仓库无场景 → {"scene_id": "", "reason": "未配置场景"}')
    parts.append('- 无法精确匹配 → 走兜底场景，reason 注明"未精确匹配，使用兜底场景"')

    # 7. 硬约束
    parts.append("\n## 硬约束")
    parts.append("1. 只输出 JSON：答复中只能有一个 JSON 块，不得有任何前后文字。")
    parts.append("2. 不调用任何 API：不访问 GitCode、不调用 A2A、不发评论、不查 Issue。")
    parts.append("3. 不读取任何文件：所有信息已在 prompt 中注入。")
    parts.append("4. scene_id 必须来自列表：只能填写 prompt 注入的场景列表中存在的 scene_id。")
    parts.append("5. 用户路由规则优先于你的语义判断：规则命中时必须遵从，不要用语义匹配覆盖。")
    parts.append("6. 任务立即结束：输出 JSON 后结束，不继续思考、不等待输入。")

    return "\n".join(parts)


def _load_scene_recommend_config(repo_key: str) -> dict:
    """从 managed_repos.scene_recommend_config 读取用户经验规则（结构化数据）。
    
    返回 {"fallback_scene": "", "rules": [{"scene_id": "", "condition": ""}]}。
    字段不存在或为空时返回默认值（纯语义匹配模式）。
    
    注意：本函数只负责读取原始结构化数据。
    自检清理（删除不存在的 scene_id）由 _sanitize_recommend_config() 在写入时执行，
    读取时也兜底过滤一次（防止旧脏数据注入 prompt）。
    """
    try:
        from src.session_manager import db, db_lock
        with db_lock:
            c = db.cursor()
            # 检查列是否存在
            _cols = {r[1] for r in c.execute("PRAGMA table_info(managed_repos)").fetchall()}
            if "scene_recommend_config" not in _cols:
                return {"fallback_scene": "", "rules": []}
            c.execute("SELECT scene_recommend_config FROM managed_repos WHERE repo_key=?", (repo_key,))
            row = c.fetchone()
            if row and row[0]:
                import json
                cfg = json.loads(row[0])
                fallback = cfg.get("fallback_scene", "") or ""
                rules = cfg.get("rules", []) or []
                # 读取时兜底过滤：规则中 scene_id 不在当前场景列表中的直接丢弃
                from src.api.pipeline_config import get_scenes
                valid_ids = {s["id"] for s in get_scenes(repo_key=repo_key)}
                rules = [r for r in rules if isinstance(r, dict) and r.get("scene_id") in valid_ids]
                # 兜底场景不在列表中也清除（返回空，prompt 中显示"未设置"）
                if fallback and fallback not in valid_ids:
                    fallback = ""
                return {"fallback_scene": fallback, "rules": rules}
    except Exception:
        pass
    return {"fallback_scene": "", "rules": []}
```

---

## 5.8 前端 UI 设计

### 弹窗入口

在项目场景页（dashboard.js 详情页）中，与"高级选项"并列，新增"智能推荐规则"按钮。点击打开弹窗（复用现有 rdp-glass 毛玻璃弹窗风格）。

### 弹窗布局

```
┌─────────────────────────────────────────────────┐
│  智能推荐规则                              [×]   │
├─────────────────────────────────────────────────┤
│                                                 │
│  兜底场景                                       │
│  ┌─────────────────────────────────┐            │
│  │ general-dev (通用开发)      ▼   │  ← 必填    │
│  └─────────────────────────────────┘            │
│  说明：当任务无法精确匹配任何规则时，走此场景      │
│                                                 │
│  ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─   │
│                                                 │
│  路由规则                                       │
│  ┌──────────────┐ ┌────────────────────┐ ┌──┐  │
│  │blog-publish ▼│ │涉及博客、文章等内容 │ │× │  │
│  └──────────────┘ └────────────────────┘ └──┘  │
│  ┌──────────────┐ ┌────────────────────┐ ┌──┐  │
│  │iac          ▼│ │涉及Terraform/IaC   │ │× │  │
│  └──────────────┘ └────────────────────┘ └──┘  │
│                                                 │
│  + 添加规则                                     │
│                                                 │
│  说明：key 从当前仓库已应用场景中选择，          │
│  value 描述什么情况下推荐使用该场景              │
│                                                 │
│                              [取消]  [保存]     │
└─────────────────────────────────────────────────┘
```

- 兜底场景：`<select>` 下拉框，选项来自当前仓库场景列表，默认选排序第一个
- 路由规则：每行一个 key-value 对
  - key：`<select>` 下拉框，选项来自当前仓库场景列表（与兜底场景相同的选项源）
  - value：`<input>` 文本框，用户输入自然语言条件描述
  - ×：删除该行
- "+ 添加规则"：新增一行空 key-value
- 保存时校验：key 不能为空、key 不能重复（同一场景不能有两条规则）

### 前端实现要点

复用现有 `_addCustomFieldInline` / `_saveAdvancedOptionsInline` 的模式：
- 弹窗 DOM 结构参考高级选项的 `det-card-wrap` 风格
- key 下拉框选项从 `get_scenes(repo_key)` API 获取（已有 `/scenes/with-pipeline?repo=xxx` 端点）
- 保存调用新 API：`PUT /repos/{owner}/{name}/scene-recommend-config`
- 加载调用：`GET /repos/{owner}/{name}/scene-recommend-config`

---

## 5.9 自检逻辑

### 触发时机

自检在以下三个时机执行，确保数据一致性：

1. **读取时**（`_load_scene_recommend_config`）：读取后过滤掉 scene_id 不在当前场景列表中的规则和兜底场景（不写回 DB，只在内存中过滤，防止脏数据注入 prompt）
2. **写入时**（`set_scene_recommend_config`）：保存前校验所有 scene_id 是否在当前场景列表中，不在的丢弃后再存
3. **场景删除时**（`delete_custom_scene`）：场景被删除后，触发该仓库的推荐配置自检，清理失效的规则和兜底场景，兜底场景失效时重置为排序第一个

### 自检函数

```python
def _sanitize_recommend_config(repo_key: str) -> dict | None:
    """自检并修复推荐配置：删除指向不存在场景的规则，重置失效的兜底场景。
    
    在场景删除时主动调用。返回修复后的 config（已写回 DB），或 None（无需修复）。
    """
    from src.api.pipeline_config import get_scenes
    from src.session_manager import db, db_lock
    import json
    
    scenes = get_scenes(repo_key=repo_key)
    valid_ids = {s["id"] for s in scenes}
    if not valid_ids:
        return None  # 仓库无场景，不处理
    
    config = _load_scene_recommend_config(repo_key)
    changed = False
    
    # 1. 过滤规则中 scene_id 不在当前场景列表的
    old_rules = config.get("rules") or []
    new_rules = [r for r in old_rules if r.get("scene_id") in valid_ids]
    if len(new_rules) != len(old_rules):
        config["rules"] = new_rules
        changed = True
    
    # 2. 兜底场景不存在 → 重置为排序第一个场景
    fallback = config.get("fallback_scene") or ""
    if fallback not in valid_ids:
        config["fallback_scene"] = scenes[0]["id"] if scenes else ""
        changed = True
    
    if changed:
        # 写回 DB
        try:
            with db_lock:
                c = db.cursor()
                _cols = {r[1] for r in c.execute("PRAGMA table_info(managed_repos)").fetchall()}
                if "scene_recommend_config" in _cols:
                    c.execute(
                        "UPDATE managed_repos SET scene_recommend_config=? WHERE repo_key=?",
                        (json.dumps(config, ensure_ascii=False), repo_key))
                    db.commit()
                    from src.config import log
                    log.info(f"RecommendConfig: auto-sanitized for {repo_key}, "
                             f"fallback={config['fallback_scene']}, rules={len(config['rules'])}")
        except Exception:
            pass
        return config
    return None
```

### 场景删除时调用

在 `pipeline_config.py` 的 `delete_custom_scene()` 末尾追加：

```python
def delete_custom_scene(scene_id: str, repo_key: str):
    # ... 现有删除逻辑 ...
    
    # 自检推荐配置：清理指向被删场景的规则/兜底
    try:
        from src.prompt_builder import _sanitize_recommend_config
        _sanitize_recommend_config(repo_key)
    except Exception:
        pass  # 自检失败不影响删除
```

---

## 六、实现方案

### 6.1 文件变更清单

| # | 文件 | 操作 | 说明 |
|---|------|------|------|
| 1 | `src/prompt_builder.py` | 修改 | 新增 `build_scene_recommender_prompt()` + `_load_scene_recommend_config()` + `_sanitize_recommend_config()`；删除 `_build_vod_scenes_section()` 及调用 |
| 2 | `src/api/event_pipeline.py` | 修改 | issue_opened 新增推荐触发判断；done 回调新增推荐完成处理 + VOD 重派 |
| 3 | `src/api/routes/webhook.py` | 修改 | 删除 `recommended_scene` A2A 持久化逻辑 |
| 4 | `src/api/routes/tasks.py` | 修改 | 注释中"VOD 可覆盖"改为"智能推荐模式" |
| 5 | `src/api/static/locales/zh/tasks.json` | 修改 | `scene_vod_auto` 改为"智能推荐" |
| 6 | `src/api/static/locales/en/tasks.json` | 修改 | `scene_vod_auto` 改为"Smart Recommend" |
| 7 | `src/api/static/locales/zh/dashboard.json` | 修改 | 新增"智能推荐规则"相关文案 |
| 8 | `src/api/static/locales/en/dashboard.json` | 修改 | 新增英文文案 |
| 9 | `skills/vod/vod/SKILL.md` | 修改 | 删除场景推荐相关描述（如有） |
| 10 | `src/api/db_setup.py` | 修改 | `managed_repos` 表新增 `scene_recommend_config` 列（JSON TEXT，默认空） |
| 11 | `src/user/db.py` | 修改 | 新增 `get_scene_recommend_config()` / `set_scene_recommend_config()`（写入时自检） |
| 12 | `src/api/routes/repos.py` | 修改 | 新增 `GET/PUT /repos/{owner}/{name}/scene-recommend-config` 端点 |
| 13 | `src/api/pipeline_config.py` | 修改 | `delete_custom_scene()` 末尾追加自检调用 |
| 14 | `src/api/static/js/pages/dashboard.js` | 修改 | 新增"智能推荐规则"按钮 + 弹窗（兜底场景下拉 + key-value 规则列表） |

### 6.2 event_pipeline.py 改动详情

#### 6.2.1 issue_opened 路由：推荐触发判断

在 `process_event()` 中 `issue_opened` 路由后，skill 解析前：

```python
if event_type == "issue_opened":
    agent_type = "vod"
    _rid = resolve_issue_id(evt.get("payload", {})) or evt.get("issue_id", "")
    
    # === 推荐触发判断 ===
    _use_recommend = False
    if _rid:
        from src.api.session_store import get_scene_override_flag
        _use_recommend = get_scene_override_flag(repo_key, _rid)
    
    if _use_recommend:
        # 智能推荐模式：用推荐 prompt，不绑定 skill
        skill_name = ""  # 无 skill 文件
        _is_scene_recommend = True
        log.info(f"Pipeline: issue_opened -> 智能推荐模式 for {_rid}")
    else:
        # 手动选择模式：用用户选的 scene_id 的 vod 技能
        from src.api.session_store import get_issue_scene
        _scene_id, _ = get_issue_scene(repo_key, _rid)
        if _scene_id:
            from src.api.pipeline_config import get_first_node
            _vod_node = get_first_node(_scene_id)  # 需补充 repo_key 参数
            skill_name = _vod_node.get("skill", "vod") if _vod_node else "vod"
        else:
            skill_name = "vod"  # 兜底
        _is_scene_recommend = False
        log.info(f"Pipeline: issue_opened -> 手动场景 mode, scene={_scene_id} skill={skill_name}")
```

后续 prompt 构建处：

```python
if _is_scene_recommend:
    from src.prompt_builder import build_scene_recommender_prompt
    prompt = build_scene_recommender_prompt(evt, repo_key)
else:
    prompt = build_devops_agent_prompt(evt, [], "", [skill_name], ...)
```

task 入队时打标 `is_scene_recommend`（用于 done 回调判断）：

```python
# 在 _enqueue_pipeline_task 或 task 元数据中增加标记
task_metadata = {"is_scene_recommend": _is_scene_recommend}
```

#### 6.2.2 done 回调：推荐完成 → 重派 VOD

```python
if evt_type == "done" and agent_type == "vod" and final_text and issue_id:
    # 判断是否为推荐任务
    if _is_scene_recommend_task(task_id):
        # === 智能推荐完成 ===
        scene_id = _parse_recommender_output(final_text)
        if scene_id:
            from src.api.session_store import set_issue_scene
            from src.runtime.engine_select import _repo_user_id
            _uid = _repo_user_id(repo_key)
            set_issue_scene(repo_key, issue_id, scene_id, scene_user_id=_uid)
            log.info(f"智能推荐: scene_id={scene_id} set for {issue_id}")
            
            # 重派 VOD（用匹配场景的 vod 技能）
            from src.api.pipeline_config import get_first_node
            vod_node = get_first_node(scene_id)  # 需传 repo_key
            if vod_node:
                vod_skill = vod_node.get("skill", "vod")
                _redispatch_vod(repo_key, issue_id, vod_skill, evt, ...)
        else:
            log.info(f"智能推荐: no scene for {issue_id}, pipeline terminated")
        return  # 不走后续 VOD done 逻辑
    
    # === 普通 VOD done（手动模式 或 推荐后重派的 VOD）===
    # 旧 recommended_scene 解析逻辑已删除
    # VOD 正常 A2A 交办 Issue Agent（A2A 机制不变）
```

#### 6.2.3 辅助函数

```python
def _is_scene_recommend_task(task_id) -> bool:
    """判断 task 是否为智能推荐任务（通过 task 元数据/prompt 特征）。"""
    # 方案 A：task 入队时写入 metadata 字段
    from src.session_manager import db, db_lock
    try:
        with db_lock:
            c = db.cursor()
            # 检查 agent_tasks 是否有 metadata 列，没有则用 prompt 特征判断
            c.execute("SELECT prompt FROM agent_tasks WHERE id=?", (task_id,))
            row = c.fetchone()
            if row and row[0]:
                # prompt 特征：包含 "你是智能推荐器" 且无 {{SKILL_DIR}}
                return "智能推荐器" in row[0] and "{{SKILL_DIR}}" not in row[0]
    except Exception:
        pass
    return False


def _parse_recommender_output(text: str) -> str:
    """从智能推荐输出中解析 scene_id。"""
    import re
    # 优先解析 JSON
    try:
        from src.api.event_store import parse_agent_output_json
        out = parse_agent_output_json(text) or {}
        sid = out.get("scene_id", "")
        if sid:
            return sid
    except Exception:
        pass
    # 正则兜底
    m = re.search(r'"scene_id"\s*:\s*"([^"]*)"', text)
    return m.group(1) if m else ""


def _redispatch_vod(repo_key, issue_id, vod_skill, evt, ...):
    """场景确定后，重派 VOD 任务（用匹配场景的 vod 技能）。"""
    # 1. 用 build_devops_agent_prompt 构建 VOD prompt（不注入场景列表）
    # 2. pick_engine 选引擎
    # 3. _enqueue_pipeline_task 入队
    # 4. 创建新 event（status=running）
    # 注意：不触发 dedup（推荐任务的 event 已 done，新 event 是独立任务）
```

### 6.3 prompt_builder.py 改动详情

#### 删除

```python
# 删除整个函数
def _build_vod_scenes_section(agent_type, repo_key=""):
    ...  # L95-161 全部删除

# 删除调用（build_devops_agent_prompt 中）
# L579-582:
_vod_scenes = _build_vod_scenes_section(agent_type, repo_key)
if _vod_scenes:
    parts.append(_vod_scenes)
# → 全部删除
```

#### 新增

```python
def build_scene_recommender_prompt(evt, repo_key: str) -> str:
    """构建智能推荐专用 prompt（极简，纯内联）。"""
    # 见 5.5 节完整代码
```

### 6.4 webhook.py 改动详情

#### 删除

```python
# L479-492：删除 recommended_scene A2A 持久化
_recommended_scene = payload.get("recommended_scene", "")
if _recommended_scene and issue_number:
    ...  # 全部删除
```

A2A 的 payload 中不再携带 `recommended_scene`（VOD 不再输出此字段）。

### 6.5 前端 locale 改动

`locales/zh/tasks.json` L85:
```json
"scene_vod_auto": "智能推荐"
```

`locales/en/tasks.json` L85:
```json
"scene_vod_auto": "Smart Recommend"
```

---

## 七、边界情况与兼容性

### 7.1 仓库无场景

- 智能推荐输出 `{"scene_id": "", "reason": "未配置场景"}`
- 系统：不设置 scene_id、不重派 VOD、流水线终止
- 与旧流程 `terminate=true` 行为一致

### 7.2 只有一个场景

- 智能推荐直接匹配该场景
- 重派 VOD 用该场景的 vod 技能
- 行为与旧流程一致，但更轻量

### 7.3 手动选择模式但 issue_state 无 scene_id

- 兜底：skill_name 设为 "vod"，走默认通用开发流程
- 记录 warning 日志
- 正常情况不会出现：tasks.py 建任务时已 set_issue_scene

### 7.4 智能推荐输出解析失败

- 正则兜底提取 `scene_id`
- 仍失败 → 不重派 VOD，流水线终止，记录 warning
- 比旧流程更安全：旧流程会 fallthrough 到 VOD done 逻辑继续执行

### 7.5 VOD 重派去重

- 推荐任务的 event 在 done 后状态为 `done`，不会被 dedup 命中
- VOD 重派创建新 event（status=running）
- 重派的 event_type 可标记为 `a2a_event`（复用现有 A2A 机制），天然豁免 dedup
- 或在 dedup 检查中增加 `is_scene_recommend` 豁免标记

### 7.6 issue_opened 无 issue_state 记录（webhook 直接触发，未走 tasks.py 建任务）

- `get_scene_override_flag` 返回 False（默认）
- 走手动选择模式 → `get_issue_scene` 返回 dev_scene
- 用 dev_scene 的 vod 技能走流水线
- 行为合理：未明确选择推荐时，用仓库默认场景

### 7.7 旧库兼容

- `allow_scene_override` 列保留不动，语义变更不影响数据
- 旧数据 `allow_scene_override=1` → 新流程走智能推荐（行为一致）
- 旧数据 `allow_scene_override=0` → 新流程走手动场景（行为一致）
- 无 DDL 变更，无迁移脚本

---

## 八、方案总结

| 维度 | 旧流程 | 新流程 |
|------|--------|--------|
| 推荐触发 | VOD 内部判断（allow_scene_override 控制是否采纳） | issue_opened 时判断 allow_scene_override，True 才触发推荐 |
| 推荐时机 | VOD 完成全部分析后 | issue_opened 后立即执行（前置） |
| 推荐执行者 | VOD Agent（兼任） | 独立推荐任务（纯 prompt，无 skill） |
| 推荐耗时 | 高（需完成需求分析+多轮对话） | 低（单轮 JSON 输出） |
| Prompt 结构 | 单层（全部注入 VOD prompt） | 三层：系统数据 + 用户经验规则 + 固定指令 |
| 用户经验规则 | 无（纯靠 LLM 语义匹配名称/描述） | 有（用户可设兜底场景 + 条件路由规则，优先级高于语义匹配） |
| 匹配准确度 | 低（场景名称/描述可能与用途无关） | 高（用户规则优先，语义匹配兜底） |
| Prompt 实现 | skill 文件 + prompt 注入 | 纯 prompt 内联（无 skill 文件） |
| 输出 | recommended_scene + scene_reason（混在 VOD JSON 中） | 纯 JSON（scene_id + reason only） |
| 手动选择模式 | VOD 仍运行但推荐被忽略 | VOD 直接用用户选的场景，不运行推荐 |
| 组件耦合 | 场景推荐与需求分析耦合 | 完全解耦 |
| 用户可配置性 | 仅开关（allow_scene_override） | 开关 + 兜底场景 + 路由规则（自然语言，DB 持久化） |
| allow_scene_override | "允许 VOD 覆盖场景" | "智能推荐模式开关"（语义更清晰） |
