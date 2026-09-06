# Issue #46 修复方案：Onboarding 任务冗余通知 + 任务/Issue 不一致

> Issue：https://gitcode.com/grape-dev/grape/issues/46
> 类型：P1 Bug（必现现象） | 状态：spec-ready（Spec v2 重构方案已发布）
> 作者：VOD Agent 根因分析 + Issue Agent Spec v2 + 本地代码复核
> 日期：2026-09-06
> 方向：**采用 Spec v2 重构方案**（用户澄清确认），非 v1 补丁方案。

## 一、问题概述

从项目模板创建项目会进入 onboarding 引导阶段。用户反馈两个问题：

1. **冗余通知**：任务中心对应 chat agent 对话里**固定出现 3 个任务通知**，预期只需要 1 个任务分配信息。
2. **行为不一致**：onboarding 任务与手动创建任务不一致——onboarding 任务创建了，但 **GitCode 上马上看不到对应 Issue 创建**。

## 二、根因分析（VOD 分析 + 本地代码复核）

### 2.1 现有 onboarding 机制（§11 v1.5）

`templates.create_from_template` → `trigger_onboarding`（onboarding.py:329）→ 对 `experience`/`explore` 两个 target 各执行一次：
构造**模拟 `issue_opened` webhook 等价体**（`_submit_onboarding_webhook` onboarding.py:244）→ `run_pipeline` 完全走普通事件链路 → VOD Agent 用 `repo-onboarding` 技能执行 → Agent 通过 GitCode API 创建真实 Issue → `_finalize_onboarding`（onboarding.py:202）校准 issue_id + 补子事件。

关键点：主事件 **入参即携带预分配 Issue 号 `#1`/`#2`**（`_onboarding_issue_id` onboarding.py:184），在 VOD Agent 真正创建 GitCode Issue **之前**，pipeline 就已按 `#1`/`#2` 建档。

### 2.2 问题一：任务会话冗余通知（3+ 条）

单条 onboarding Issue 的任务对话（`chat_conversations`，title=`Issue #N`）被多处写入：

| # | 写入点 | 位置 | 内容 |
|---|--------|------|------|
| 1 | `ensure_issue_task` → `_ensure_task_conversation` | tasks.py:121-127 | 「任务分配 · Issue #N」上下文消息（新建/重分配各 1 条） |
| 2 | `_post_result_to_issue` 内部 `add_message` | event_pipeline.py:2120 | `[AGENT Agent] {result_text}` |
| 3 | `_execute_agent` 收尾直接 `add_message` | event_pipeline.py:2073 | `[AGENT Agent] {result_text}`（与 #2 **内容完全相同**） |
| 4 | `_sync_issue_comments_to_conversation` 补拉 | event_pipeline.py:2084 | Agent 通过 curl 发的 Issue 评论（去重后写入） |

**2 与 3 是同一份 `result_text` 双写**：`_post_result_to_issue`（发 GitCode 评论）内部 `add_message` 同步，同时 `_execute_agent` 收尾（event_pipeline.py:2060-2076）又独立 `add_message` 一次。二者依赖注入函数相同、条件重叠，导致每次执行多 1 条重复消息。加上 #1 与 #4，用户感知为「固定 3 个任务通知」。

> 该双写为**通用缺陷**，不止 onboarding（任何存在 robot 评论 + 任务会话的事件都会 `.repeat`）。onboarding 因双 target 各自建档，放大为明显问题。

### 2.3 问题二：任务可见但 GitCode 无 Issue

- **手动创建任务路径**：用户在任务中心建任务 → `_post_gitcode_issue`（tasks.py:549）**先创建真实 GitCode Issue** → 拿到真实 number → `ensure_issue_task`（tasks.py:136）建档。建档时 GitCode 中 Issue 已存在。前端通过 GitCode Issue 展示。
- **onboarding 路径**：模拟 webhook 用预分配 `#1/#2` → pipeline 在建档/落库事件阶段就把任务按预分配号建了（event_pipeline.py:839 `ensure_issue_task`），**此时 GitCode 尚无任何 Issue**。VOD Agent 随后才真正创建 Issue（通常晚数秒到数十秒），所以用户看到「任务已创建」但点进 GitCode 没有对应 Issue。

### 2.4 根因小结

1. 模拟 `issue_opened` 事件机制 + 预分配 Issue 号 = 问题二的**结构根因**（任务早于真实 Issue 落库）。
2. `result_text` 双写（`_post_result_to_issue` + `_execute_agent` 收尾）+ 任务上下文消息 + 评论补拉 = 问题一的**消息冗余根因**。

## 三、方案选择（用户澄清 → Spec v2 重构）

用户通过任务处理对话明确选择**重构方案**（替代 v1 补丁修复）：

> 放弃「模拟 issue_opened 事件触发后续流程」的做法，改为**直接在任务中心创建标准任务**（真实 Issue 先创建 → 建档，与手动创建任务完全一致）。**创建哪些 onboarding 任务由 onboarding 模板决定**——可能只有体验任务，也可能包含二次开发等扩展任务；**默认模板应同时包含「体验部署」与「二次开发」两个任务**。模板获取策略：先检查仓库根目录自定义模板，无则用配置默认值。

**模板驱动要点（本次澄清补充）**：
- 任务集合**不是代码硬编码**的 `experience`/`explore` 两个固定 target，而是由 `onboarding-tasks.json` 的 `tasks` 列表决定。
- 默认模板 = 2 个任务（体验部署 + 二次开发）；仓库可自定义为 1 个（如仅体验）或更多。
- 每个任务带「任务类型」与「任务描述」，决定 GitCode Issue 的标题/正文与 Onboarding 完成判定键。

### 方案对比

| 维度 | v1 补丁 | v2 重构（选定） |
|------|---------|----------------|
| 模拟 issue_opened 事件 | 保留 | **完全移除** |
| 预分配 Issue 号 #1/#2 | 保留 | **移除** |
| 任务建档时机 | 推迟到真实 Issue 后 | 由「创建真实 Issue → 建档」标准流程承载 |
| 消息去重 | event_pipeline 加去重 hack | 不再需要（单一创建路径，天然单条通知） |
| 与手动任务一致性 | 局部对齐 | **完全一致** |

## 四、修复方案（落地实现）

### 4.1 [P0] onboarding.py 核心重构：移除模拟事件，改为标准任务创建

**删除/废弃**：
- `_submit_onboarding_webhook`（onboarding.py:244-296）——模拟 webhook 构造 + run_pipeline
- `_await_event_and_finalize`（onboarding.py:299-326）——轮询事件终态
- `_finalize_onboarding`（onboarding.py:202-241）——issue_id 校准 + 子事件补库（真实流程无需）
- `_onboarding_issue_id`（onboarding.py:184-189）、`_onboarding_workdir`（onboarding.py:192-199）——预分配逻辑
- `ONBOARDING_TARGETS = ("experience", "explore")`（onboarding.py:19）与 `ONBOARDING_EVENT_TYPE`（onboarding.py:22）——**任务集合不再由代码常量硬编码**，改由模板 `tasks` 列表决定
- `_target_main_event_done`（onboarding.py:25-51）中按 `onboarding_issue` / `#1/#2` 判完成 → 改为按「每个模板任务的真实 Issue+任务已存在」判定
- `_scene_onboarding_skill`（onboarding.py:54-69）——按场景读 `agent-skill-mapping.json` 的 onboarding 技能；重构后 onboarding 不再走 agent 执行，该函数连同其唯一数据源一起删除

**新增 `_create_onboarding_tasks(repo_key, owner)`**：
1. 读取 onboarding 模板（见 4.2）：仓库根目录自定义 `onboarding-tasks.json` 优先，无则用 `configs/onboarding-templates.json` 默认 → 得到**任务定义列表** `tasks: [{type, title, description, labels}, ...]`。
2. **遍历模板任务列表**（默认 2 个：体验部署 + 二次开发；可能 1 个或更多），对每个任务调用 tasks.py 导出的**标准任务创建原子函数**（见 4.3）：创建真实 GitCode Issue（仓库 bot token，复用 `_post_gitcode_issue` 语义）→ `ensure_issue_task` 建档。全部任务**同步创建**（同 HTTP 请求内完成），每个任务天然 1 条任务上下文消息。
3. 幂等与自愈（分两层，各司其职）：
   - **创建层幂等**：按 `title` 幂等——同标题 open Issue 已存在（`_find_open_issue_by_title`）则跳过创建，防止重复建 Issue/任务。
   - **完成层判定**：按任务 `type` 记录与自愈——`onboarded:{repo_key}` 仅作总闸，另以 `onboarded_task:{repo_key}:{type}` 记每个任务完成态；只补 `type` 未完成的任务。
   - 为什么 completion 键用 `type` 而非 `title`：`title` 是人类可读文本、可被用户/Agent 在 GitCode 编辑，若把它当完成记录键，用户改标题后 self-heal 会丢失"该任务已完成"状态、重复创建。`type` 是稳定代码标识，不受标题变更影响。

**改写 `trigger_onboarding`（onboarding.py:329）**：不再提交模拟 webhook，改为一次性调用 `_create_onboarding_tasks`（内部按任务 `type` 完成自愈，`type` 已完成的任务跳过创建）。

**改写 `retry_onboarding_target`（onboarding.py:382）**：改名为 `retry_onboarding_task(repo_key, owner, task_type)`，仅补建 `type` 未完成的任务（删除其 `_submit_onboarding_webhook` 调用），供重派使用。

### 4.2 [P1] onboarding 模板配置（默认 + 仓库覆盖）

Onboarding 任务集完全由模板决定。模板本质是**任务定义列表**，每项包含任务类型、标题、描述等。

#### 4.2.1 `onboarding-tasks.json` schema（文档设计）

**顶层结构**（支持 `default` 全局 + 按 `scene_id` 覆盖）：

```jsonc
{
  // 可选的全局默认任务列表（所有场景未单独指定时生效）
  "default": {
    "tasks": [
      { "type": "...", "title": "...", "description": "...", "labels": [] }
    ]
  },
  // 可选：按场景名覆盖（场景名取自 managed_repos.dev_scene）
  "cloud-native": {
    "tasks": [ ... ]
  }
}
```

**任务项字段**：

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `type` | string | ✅ | 任务类型标识（**稳定机器键**：用于完成记录 `onboarded_task:{repo}:{type}` 与重派补建，不随标题变更） |
| `title` | string | ✅ | GitCode Issue 标题（唯一性/创建幂等键：创建时按 title 查重，避免重复建 Issue） |
| `description` | string | ✅ | GitCode Issue 正文 / 任务描述 |
| `labels` | string[] | 可选 | 创建 Issue 时附加的标签（空数组/缺省=不打标签） |

> **type 与 title 职责分工**：`type` 是稳定的机器标识（不该被编辑，作完成状态/补建键）；`title` 是人类可读文本、同时也是**创建唯一性**键（同一仓库同一 open title 的 Issue 视为该任务已建）。二者互补，不重复。

> **type 的数据落点（无需新增表字段）**：`type` 是模板配置标识，非任务业务属性。其消费点：
> - 完成记录/幂等标记 → `meta` 表（db_setup.py:123，key=`onboarded_task:{repo_key}:{type}`，value="1"），复用现有 key-value。
> - 重派补建 → `retry_onboarding_task(..., task_type)` 纯函数入参，不落库。
> - 完成任务判定 → 按该 type 的标题查 GitCode Issue + `tasks` 表按 title/issue_number（现有 `_find_open_issue_by_title` / `ensure_issue_task` 语义）。
> - 本轮**不在 `tasks` 表新增 `task_type` 字段**（无此需求）；未来若需前端按 onboarding 类型分组展示任务，再评估加 `task_type TEXT DEFAULT ''`。

**任务类型值域（本轮内置，可扩展）**：

| `type` | 含义 | 默认标题 | 默认描述 |
|--------|------|----------|----------|
| `experience` | 快速部署体验 | `引导·快速部署体验` | `首次托管引导：体验本项目的快速部署流程。` |
| `dev` | 二次开发 | `引导·二次开发` | `首次托管引导：基于本项目进行二次开发，探索扩展方向。` |

> 任务类型在代码中**不写全量枚举**——`type` 仅作稳定标识/完成键，未知类型直接按模板字段创建，天然支持未来扩展。

**仓库级自定义**：仓库根目录 `onboarding-tasks.json`。schema 与上述一致（`{"default": {"tasks": [...]}}` 或 `{"tasks": [...]}` 简写均可）。仅取该文件的 `tasks` 列表，**完全替代**默认模板（不做字段级深合并——自定义模板需要明确列出要创建的全部任务，避免"想删掉某任务却因合并残留"）。

**解析规则（`_load_onboarding_template(repo_key, scene_id)`）**：
1. 仓库根目录存在 `onboarding-tasks.json` → 用之（scene_id 键存在则优先用该场景段，否则用 `default` 段；文件解析失败 → 记 warning 降级到默认）。
2. 否则用 `configs/onboarding-templates.json`，同样优先 `scene_id` 键，缺省用 `default` 段。
3. 返回 = 任务定义列表；空列表 / 缺文件 → 跳过 onboarding（不报错）。

#### 4.2.2 默认模板内容（`configs/onboarding-templates.json`）

```json
{
  "default": {
    "tasks": [
      {
        "type": "experience",
        "title": "引导·快速部署体验",
        "description": "首次托管引导：体验本项目的快速部署流程。",
        "labels": []
      },
      {
        "type": "dev",
        "title": "引导·二次开发",
        "description": "首次托管引导：基于本项目进行二次开发，探索扩展方向。",
        "labels": []
      }
    ]
  }
}
```

> 默认模板**同时包含「体验部署（experience）+ 二次开发（dev）」两个任务**。仓库可在根目录放自定义 `onboarding-tasks.json`，例如仅保留体验任务：
> ```json
> { "tasks": [{ "type": "experience", "title": "引导·快速部署体验", "description": "...", "labels": [] }] }
> ```

### 4.3 [P1] tasks.py 导出标准任务创建原子函数（onboarding 对接）

在 tasks.py 新增（或显式导出）可由 onboarding 调用的纯函数：

```python
def create_standard_task(repo_key: str, assignee: str, title: str,
                         description: str, bot_token: str, labels=None) -> dict:
    """真实 GitCode Issue 创建 + 标准任务建档（与手动创建任务同一原子路径）。
    - 幂等：同仓库同标题 open Issue 已存在 → 不重复创建（复用 _find_open_issue_by_title）
    - 建档：ensure_issue_task（幂等，天然单条任务上下文消息）
    """
```

- 复用 `create_task_via_issue`（tasks.py:593）的既有原子逻辑，抽象出与 user 解耦的版本（token 改为 bot token 注入，assignee=owner）。
- **不额外写入任何消息**——任务上下文消息只由 `ensure_issue_task → _ensure_task_conversation` 产生 1 条（onboarding 不再走 `_post_result_to_issue`，自然无 result_text 双写）。

### 4.4 [P0] event_pipeline.py 清理

1. **移除 §4.5 onboarding 上下文注入**（event_pipeline.py:954-990）：`payload._onboarding` / `ONBOARDING_*` env 覆盖逻辑。onboarding 不再跑 run_pipeline，该分支死代码。（同步清理 onboarding.py 相关引用与 prompt_builder 中 onboarding 环境依赖。）
2. **修复 result_text 双写（问题一通用根因）**：
   - `_post_result_to_issue`（event_pipeline.py:2087）**移除内部 add_message 同步段**（L2110-2123）。该函数职责收敛为「发 GitCode 评论」。
   - task conversation 的 result_text 同步统一由 `_execute_agent` 收尾（event_pipeline.py:2060-2076）承担，作为唯一写入路径。
   - 保留 `_sync_issue_comments_to_conversation` 去重指纹（现有 L2196-2206 指纹逻辑）——agent 自发的 curl 评论仍需补拉，但与 result_text 指纹不同源，不会叠成重复 result_text。

> 说明：Spec v2 提及「event_pipeline.py 移除 onboarding 消息去重 hack / `_sync_issue_comments_to_conversation` onboarding 旁支」，经复核 `_sync_issue_comments_to_conversation` 本身无 onboarding 特判，无需改；真正要清理的是 onboarding 上下文注入（§4.5）与 `_post_result_to_issue` 内 add_message 重复同步。

### 4.6 [P0] 修复：Onboarding 创建 Issue 被 self-comment 检测误杀（上线回归）

**现象（生产验证，2026-09-06）**：`Jason7602/hello-calculator-11` onboarding 创建了 2 个任务（#151/#152，Issue #1/#2），但 **Pipeline 未触发**（events 空、issue_state 空）。手动任务 #153（`/api/tasks/create`）同仓库正常触发。

**根因**：新机制 `create_standard_task` 用 **仓库 bot token** 调 GitCode API 建 Issue → GitCode 回推 `issue_opened` Webhook 的 actor=grape_bot → `run_pipeline` 命中 **self-comment 检测**（event_pipeline.py:693）→ 整事件跳过。手动路径用用户 token，actor=Jason7602，不受影响。

**修复方案（方向 1：按事件类型豁免，最小改动）**：

在 `run_pipeline` 自循环检测段（event_pipeline.py:687-709）**扩展 issue_opened 豁免**，复用现有「多任务子任务 bot 创建」豁免模式（L694-704，body 含 `## 父任务` 放行）：

```python
if actor in bot_usernames:
    # 多任务模式豁免：bot 创建的子任务 issue_opened（body 含「## 父任务」）不跳过
    _allow_through = False
    if event_type == "issue_opened":
        try:
            _payload = evt.get("payload", {}) or {}
            _oa = _payload.get("object_attributes", {}) or {}
            _body = _oa.get("description", "") or ""
            if "## 父任务" in _body:
                _allow_through = True
            # Issue #46：Onboarding 由 bot token 创建引导 Issue（模板 title 前缀）
            # → issue_opened 同样放行（专项入口，避免 bot 建 Issue 后流水线永不触发）
            elif _body.startswith("首次托管引导"):
                _allow_through = True
        except Exception:
            pass
    ...
```

- **判定依据**：onboarding 模板默认描述前缀为「首次托管引导」（`configs/onboarding-templates.json` 的 `experience`/`dev` description）。以该前缀豁免 bot 创建的 `issue_opened`。
- **为何不整体豁免 `issue_opened`**：避免放开「用户手动用 bot 身份建 Issue」等其他场景的自回声防护；按 onboarding 专属标记收口，最小影响面。
- **与 4.1 的协同**：`create_standard_task` 建 Issue 时 description 即模板 description（含「首次托管引导」前缀），天然满足豁免条件，无需额外传参。

**对已建任务的处理（存量自愈）**：
- #151/#152 已建档但流水线未触发 → 修复上线后需**清理 `onboarded_task:{repo}:experience/dev` 两个 meta 标记**并重放 2 条 GitCode Issue Hook（或删 meta 标记后重跑 `retry_onboarding_task`，因创建层按 title 幂等会复用 Issue、只补建 pipeline 事件）。
- 具体运维步骤见部署后验证节。

> 备选方向（不采用本轮）：方向 2「onboarding 建 Issue 带来源标记（X-Grape-Onboarding header / payload 标记）让 pipeline 显式放行」，实现面更大（涉及 HTTP 布尔、webhook 解析、payload 透传三处），且对已上线链路改动深；方向 1 与现有子任务豁免同构、改动 3 行内，优先选用。

### 4.5 [P2] 回归与测试

- 新增 `tests/test_onboarding_standard_task.py`：
  - `_create_onboarding_tasks` 幂等：重复调用不产生重复 Issue/任务。
  - 模板合并：仓库自定义覆盖默认；无自定义用默认。
  - 预分配移除：不再出现 `_onboarding_issue_id` 引用的 `#1/#2` 事件落库。
- 新增/更新 event_pipeline 测试：`_post_result_to_issue` 不再写 task conversation；`_execute_agent` 收尾 result_text 单写。
- 回归：`test_tasks*.py`、`test_webhook_echo_and_rollback.py` 全绿。

## 五、改动清单

| # | 模块 | 改动 | 优先级 |
|---|------|------|--------|
| 1 | `src/api/onboarding.py` | 移除模拟事件/预分配/校准整套机制，新增 `_create_onboarding_tasks` + 模板加载 helper | P0 |
| 2 | `src/api/event_pipeline.py` | 移除 §4.5 onboarding 上下文注入；`_post_result_to_issue` 去掉内部 add_message（result_text 单写） | P0 |
| 3 | `src/api/routes/tasks.py` | 导出 `create_standard_task` 原子函数（GitCode Issue + 建档，幂等） | P1 |
| 4 | `configs/onboarding-templates.json` | 新增默认模板配置（+ example） | P1 |
| 5 | 仓库级模板约定 | 根目录 `onboarding-tasks.json` 自定义覆盖（文档化 schema） | P1 |
| 6 | `configs/agent-skill-mapping.json` | 移除各场景 `onboarding` 字段（重构后无消费方，`_scene_onboarding_skill` 同批删除） | P1 |
| 7 | `src/api/event_pipeline.py` §2 自循环检测 | issue_opened 对「首次托管引导」前缀描述豁免 self-comment（3 行内，方向 1） | P0 |
| 8 | `tests/` | onboarding 标准任务 + event_pipeline 单写回归用例 | P2 |

**存量自愈（部署后运维）**：清理 `hello-calculator-11` 的 `onboarded_task:*` 标记 → 重放 2 条 GitCode Issue Hook，让 #151/#152 关联的流水线补跑。

**不包含**：手动创建任务流程改动、chat agent UI 改动、onboarding UI/UX 重设计（Spec v2 Out of Scope 对齐）。

## 六、验收标准

- [ ] 从项目模板创建项目后，onboarding 按**模板任务列表**在任务中心创建任务（默认模板 = 体验部署 + 二次开发共 2 个），GitCode 同步出现对应数量的真实 Issue（**立即可见**）
- [ ] 仓库自定义 `onboarding-tasks.json` 可决定任务集合（例如仅 1 个体验任务），任务数不写死在代码
- [ ] 每个任务在 chat agent 会话**仅 1 条**任务分配通知（无 result_text 双写、无评论补拉叠写）
- [ ] onboarding 任务与手动创建任务**流程与行为完全一致**
- [ ] onboarding 模板优先读取仓库根目录自定义 `onboarding-tasks.json`，无则用配置默认值
- [ ] `_onboarding_issue_id` / `_submit_onboarding_webhook` / `_await_event_and_finalize` / `_finalize_onboarding` 全部移除，无残留引用
- [ ] `agent-skill-mapping.json` 各场景 `onboarding` 字段与 `_scene_onboarding_skill` 一并移除（随重构同步，避免单独删导致现有 onboarding 静默跳过）
- [ ] `_post_result_to_issue` 不再写 task conversation（result_text 单写）
- [ ] **onboarding 创建的 Issue（描述含「首次托管引导」前缀）能正常触发流水线**（不被 self-comment 跳过），`hello-calculator-11` #151/#152 使用例回归验证通过
- [ ] 手动 `issue_opened`（非「首次托管引导」描述、非「## 父任务」）若由 bot 身份创建仍被 self-comment 跳过（豁免不扩散）
- [ ] 现有任务相关测试全绿 + 新增用例通过

## 七、风险

| 风险 | 影响 | 缓解 |
|------|------|------|
| 移除模拟事件后 onboarding 中断 | High | `_create_onboarding_tasks` 复用手动任务成熟原子路径（`_post_gitcode_issue`+`ensure_issue_task`），纯内部代码改动，无外部依赖 |
| 仓库级自定义模板格式未定义 | Med | 文档化 `onboarding-tasks.json` schema + 提供默认模板示例；解析失败降级到默认 |
| 其它模块引用被删函数（如重派 `retry_onboarding_target`） | Med | 同步改 `retry_onboarding_target` → `retry_onboarding_task(repo_key, owner, task_type)` 走 `_create_onboarding_tasks`；全局 grep 确认无其它引用（已复核 routing.py:29 仅注释） |
| `_post_result_to_issue` 去 add_message 影响非 onboarding 任务可见性 | Low | `_execute_agent` 收尾（2060-2076）仍是同会话单写 path，Chat 页可见性不变，仅去重 |
| 真实 Issue 创建失败（bot token / 网络） | Med | 沿用 `_post_gitcode_issue` 错误处理；失败返回告警，`onboarded` 标记在**全部模板任务成功后**再置位 |
| 仓库自定义模板字段非法（未知 type / 缺 title 等） | Low | 解析容错：缺 title/description 用 type 派生默认值；非法 JSON 降级到默认模板 |
| **bot 建 Issue 的 issue_opened 被 self-comment 误杀（生产已现）** | High | §4.6 方向 1 豁免「首次托管引导」前缀描述；与既有子任务豁免同构，改动 ≤3 行 |
| 豁免条件过宽（非 onboarding 描述也通过） | Low | 收口到「首次托管引导」精确前缀 + description 校验；专项描述可后续迁移到模板字段 |

## 八、处理流水线状态

- ✅ VOD Agent：根因分析已结构化（两问题，3 写入点 / 预分配链路）
- ✅ Issue Agent：深度分析 ready → **Spec v2（重构方案）已发布**（comment id=188337022，用户澄清后替代 v1 补丁方案），已打 `bug/triaged/spec-ready`，assignee=grape_bot
- ✅ **2026-09-06 上线后回归发现**：`hello-calculator-11` onboarding 任务创建成功但流水线未触发——根因 = bot token 建 Issue 后 Webhook 回推被 self-comment 检测跳过（§4.6）
- ⚠️ 修复待实施：方向 1 豁免「首次托管引导」描述前缀 + 存量 #151/#152 的 meta 标记清理与 Issue Hook 重放

## 附：关键代码位置

- onboarding 模拟事件机制：`src/api/onboarding.py` `_submit_onboarding_webhook`(244) / `_await_event_and_finalize`(299) / `_finalize_onboarding`(202) / `_onboarding_issue_id`(184)
- onboarding 触发入口：`src/api/onboarding.py` `trigger_onboarding`(329) / `retry_onboarding_target`(382)→重构为 `retry_onboarding_task`；调用方 `src/api/routes/templates.py` L253
- 预分配落库点：`src/api/event_pipeline.py` L839 `ensure_issue_task`（issue_opened 事件建档）
- onboarding 上下文注入：`src/api/event_pipeline.py` L954-990（§4.5）
- result_text 双写：`src/api/event_pipeline.py` L2087 `_post_result_to_issue` 内 L2110-2123 add_message；`_execute_agent` 收尾 L2060-2076
- self-comment 检测（修复点）：`src/api/event_pipeline.py` L687-709（`resolve_actor`(480) / actor in bot_usernames / issue_opened 子任务豁免 L694-704）；**需新增「首次托管引导」前缀豁免**
- 评论补拉去重：`src/api/event_pipeline.py` `_sync_issue_comments_to_conversation`(2169)
- 手动任务标准路径：`src/api/routes/tasks.py` `create_task_via_issue`(593) → `_post_gitcode_issue`(549) → `ensure_issue_task`(136) → `_ensure_task_conversation`(99-133)