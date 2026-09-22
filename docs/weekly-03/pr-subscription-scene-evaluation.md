# PR 订阅场景功能 — 影响评估文档（v3）

## 一、功能概述

新增「PR 订阅场景」类型：当一个 PR 与任意 Issue 都无关时，触发此场景进行工作。
该场景只包含 QA 和 SRE Agent（QA 必选，SRE 可选），不含 VOD/Issue Agent。
一个仓库最多一个 PR 订阅场景。

## 二、主人确认的设计决策

1. **QA Prompt 上下文**：只需加载必要内容（项目知识 + 对应技能），无需完整 Issue 上下文
2. **审查结果回帖**：回帖到 PR 下作评论
3. **Agent Session 关系**：在 run_pipeline 中直接从场景 nodes 解析 skill，不走 agent_session 查询
4. **PR 后续关联 Issue 的竞态**：良性，两个场景验证的是不同的问题，不需要特殊处理
5. **高级选项**：PR 订阅场景无工作分支意义，取消高级选项
6. **详情页执行流程**：只显示 QA 和 SRE 两个 Agent，QA 必须有，其余 Agent（vod/issue/coding）根本不显示
7. **触发条件**：只有新 commit 才触发（L1 已过滤无 commit 的 PR update），同时确认该 PR 没有关联 Issue
8. **SRE 角色**：由定义场景的人决定是否加入 SRE，已有 A2A 逻辑支持 QA→SRE 交接
9. **onboarding 字段**：见下文分析，对 PR 订阅场景无实际意义

## 三、关于兜底场景的澄清

主人指的"默认场景"是智能推荐规则里的**兜底场景**（`fallback_scene`）。

代码位置：
- 前端弹窗：`dashboard.js` 第 1674 行 `_openSceneRecommendModal()` — 兜底场景下拉框 + 规则列表
- API：`repos.py` 第 1138 行 `save_scene_recommend_config()` — 保存兜底场景 + 路由规则
- 读取：`prompt_builder.py` 第 1183 行 `_load_scene_recommend_config()` — 从 `managed_repos.scene_recommend_config` 读取

兜底场景的下拉选项来源：`/scenes/with-pipeline` 返回的全量场景列表（第 1721-1726 行）。

**要求**：PR 订阅场景不出现在兜底场景下拉选项中。
**实现**：前端渲染下拉时过滤 `scene_type === 'pr_subscription'` 的场景，或后端 `with-pipeline` 接口加参数过滤。

## 三点五、关于 onboarding 字段

`onboarding` 字段存在 `devops_pipeline_scene` 表中，默认值 `repo-onboarding`。

实际用途：仓库首次托管时触发 `_create_onboarding_tasks()`（`onboarding.py` 第 256 行），按 `onboarding-tasks.json` 模板创建引导 Issue。模板加载时支持按 `scene_id` 键命中（第 174 行：`if scene_id and isinstance(data.get(scene_id), dict)`）。

但关键点是：
- onboarding 只在仓库**首次托管**时触发一次（幂等标记 `_mark_onboarding_started`）
- `event_pipeline.py` 中注释明确写了 `onboarding 已不使用，Issue #46`（第 1678 行）
- onboarding 任务创建后会绑定到 Issue（第 280 行读 `managed_repos.dev_scene`），与场景的 onboarding 字段关联很弱

**结论**：onboarding 字段对 PR 订阅场景无实际意义。PR 订阅场景创建时 onboarding 保持默认值 `repo-onboarding` 即可，不需要特殊处理，也不影响任何功能。

---

## 四、PR 关联 Issue 的当前行为（回答主人第 4 个问题）

### 当前代码行为：PR 有关联 Issue 时，会触发对应 Issue 的流水线

完整流程：

```
PR webhook 到达
  ↓
resolve_pr_issue_id() 尝试从 3 个来源提取关联 Issue：
  1. PR title 中的 #N（如 "fix: 修复登录 #123"）
  2. payload.issues 列表（GitCode 直接给出的关联 Issue）
  3. 分支名 issue-N（如 feature/issue-123）
  4. 以上都没有 → 同步调 GitCode/GitHub PR 详情 API 查 title/分支
  ↓
找到关联 Issue ID
  ↓
_resolve_issue_stage_agent() 查 Issue 的当前 stage
  ↓
按 _STAGE_EVENT_AGENT_MAP 路由：
  pull_request_opened + stage=vod    → issue agent
  pull_request_opened + stage=coding → coding agent
  pull_request_opened + stage=qa     → qa agent
  pull_request_opened + stage=sre    → sre agent
  pull_request_updated + stage=coding → qa agent（复审）
  pull_request_merged + stage=coding  → coding agent
  ...
```

**关键结论**：
- PR 有关联 Issue → 按 Issue 的当前阶段路由到对应 Agent（走 Issue 流水线）
- PR 无关联 Issue → `PR_DEFAULT_AGENT = ""` → 事件被 skip（**这就是 PR 订阅场景要接入的缺口**）

### 第 4 个问题的回答

**当前代码中 PR 关联 Issue 后会触发对应 Issue 的流水线**。

所以竞态问题是真实存在的：
1. PR opened webhook 到达，此时无关联 Issue → PR 订阅场景触发 QA
2. 用户随后在 PR 描述中加了 `#123` 关联 Issue
3. PR updated webhook 到达 → `resolve_pr_issue_id` 找到 #123 → 按 Issue stage 路由 → 可能再次触发 QA

**但这其实不是严重问题**：PR updated 是新事件，走的是 Issue 流水线的正常路由。两次 QA 审查的上下文不同（第一次是无 Issue 的独立审查，第二次是 Issue 流水线中的 QA 复审），属于合理的重复审查。如果要避免，可以在 PR 订阅场景触发后记录 PR 号，后续 PR updated 事件检查是否已审查过。但这个优化不是必须的，可以作为后续迭代。

---

## 五、改动影响点清单（更新版）

### 改动点 1：DB Schema — 新增 scene_type 列

**文件**：`src/api/db_setup.py`

**改动**：给 `devops_pipeline_scene` 表加 `scene_type TEXT DEFAULT 'standard'` 列。

**风险等级：🟢 低** — 纯增量列，默认值兼容旧数据。

---

### 改动点 2：场景创建 API — 类型选择 + 唯一约束

**文件**：`src/api/routes/scenes.py`、`src/api/pipeline_config.py`

**改动**：
1. `CreateSceneRequest` 加 `scene_type: str = "standard"`
2. `create_scene()` 校验：
   - `scene_type == "pr_subscription"` → 查同仓库是否已有 PR 订阅场景 → 已有则 409
   - `scene_type == "pr_subscription"` → nodes 只允许包含 `qa` 和 `sre`，QA 必须启用
3. `create_custom_scene()` 持久化 `scene_type`
4. `update_scene()` 不允许修改 `scene_type`

**风险等级：🟡 中** — 唯一约束是业务层校验，存在极低概率并发竞态。

---

### 改动点 3：场景列表/详情 API — 返回 scene_type

**文件**：`src/api/pipeline_config.py`、`src/api/routes/scenes.py`

**改动**：
1. `_query_scenes_from_db()` SELECT 加 `scene_type`
2. `get_scenes()` / `list_scenes_with_pipeline()` / `get_scene_detail()` 返回 `scene_type`
3. `with-pipeline` 接口可选过滤 PR 订阅场景（兜底场景下拉不用）

**风险等级：🟢 低** — 纯读取层。

---

### 改动点 4：前端 — 创建场景弹窗

**文件**：`src/api/static/js/pages/dashboard.js`

**改动**：
1. `enterCreateMode()` 改为先弹类型选择弹窗
2. 选标准场景 → 当前 `enterCreateMode()` 逻辑（5 个 Agent，vod/issue 不可禁用）
3. 选 PR 订阅场景 → 新函数 `enterCreatePRSubscriptionMode()`：
   - `allAgentTypes = ['qa', 'sre']`（只 2 个）
   - QA 不可禁用：`canToggle = (tag) => tag !== 'qa'`
   - 不渲染高级选项区域（工作分支无意义）
   - 保存时传 `scene_type: 'pr_subscription'`

**风险等级：🟡 中** — 创建流程变化是用户可感知的。

---

### 改动点 5：前端 — 场景列表特殊标志

**文件**：`src/api/static/js/pages/dashboard.js`

**改动**：`_renderLocalSceneCard()` 根据 `scene_type` 显示"PR 订阅"药丸标签。

**风险等级：🟢 低** — 纯渲染层。

---

### 改动点 6：前端 — 详情页适配

**文件**：`src/api/static/js/pages/dashboard.js`

**改动**：
1. `_renderLocalSceneDetailPage()`：
   - PR 订阅场景 → `agentOrder = ['qa', 'sre']`，不显示 vod/issue/coding
   - 不渲染高级选项区域
2. `_renderSceneEditMode()`：
   - PR 订阅场景 → `allAgentTypes = ['qa', 'sre']`
   - QA 不可禁用：`canToggle = (tag) => tag !== 'qa'`
3. `_saveSceneEdit()` / `_saveCreateScene()`：
   - PR 订阅场景校验 QA 必须启用且有 skill
   - 不保存高级选项

**需修改的硬编码位置**（`allAgentTypes` / `canToggle`）：
- 第 899 行：`_renderSceneEditMode`
- 第 1036 行：`renderAgentCards` canToggle
- 第 1314 行：`enterCreateMode`
- 第 1329 行：`enterCreateMode` canToggle
- 第 1577 行：`renderAgentCards` canToggle

**风险等级：🟡 中** — 散落多处硬编码，需逐一修改。

---

### 改动点 7：事件管道 — PR 无关联 Issue 时触发 PR 订阅场景

**文件**：`src/api/event_pipeline.py`

**最高风险改动。**

**当前流程**（第 920-950 行）：
```
PR 事件 → resolve_pr_issue_id() → 无 Issue → PR_DEFAULT_AGENT="" → skip
```

**改动后流程**：
```
PR 事件 → resolve_pr_issue_id() → 无 Issue → 查仓库是否有 PR 订阅场景
  → 有 → 从场景 nodes 取 QA skill → 分派 QA 任务（agent_type="qa"）
  → 无 → 维持当前 skip
```

**具体改动**：

1. 在第 936 行 `if not agent_type:` 块之前增加 PR 订阅场景判断
2. 当 `event_type in PR_EVENT_TYPES` 且 `agent_type == ""` 时：
   - 查 `devops_pipeline_scene WHERE repo_key=? AND scene_type='pr_subscription' AND is_active=1`
   - 从 `nodes_json` 中提取 QA node 的 skill
   - 设置 `agent_type = "qa"` + `skill_name` + `skill_id` + `skill_path`
3. issue_id 保持为空/`#0`（无关联 Issue）
4. 不走 `set_issue_stage`（无 Issue 生命周期）

**关键风险点**：

#### 7a. 去重逻辑缺口
当前去重（第 1182 行）：`if issue_id and agent_type` — issue_id 为空时不去重。
同一 PR 多次 push → 重复触发 QA。

**解决方案**：PR 订阅场景的去重改为按 `repo_key + PR number + agent_type`。
从 payload 提取 PR number（`object_attributes.iid`），在去重检查中用 `issue_id = "#pr_{pr_number}"` 作为伪 issue_id 参与去重。

#### 7b. Skill 解析（已确认方案）
主人确认：在 run_pipeline 中直接从场景 nodes 解析 skill，不走 agent_session 查询。

**实现**：
- 查到 PR 订阅场景后，从 `nodes_json` 中提取 QA node 的 `skill` 字段
- 直接设置 `skill_name` / `skill_id` / `skill_path`，跳过第 1045 行的 `devops_agent_session` 查询
- 不需要创建 agent_session，不需要走 apply_scene

#### 7c. Prompt 构建
主人确认：只需加载项目知识 + 技能，无需完整 Issue 上下文。
`build_devops_agent_prompt()` 依赖 issue_id 拉 Issue 信息。

**解决方案**：
- PR 订阅场景触发时，prompt 基于 PR 信息（title/body/commits）+ 项目知识构建
- 需检查 `build_devops_agent_prompt()` 在 issue_id 为空时的行为
- 可能需要新的 prompt 构建逻辑或参数标记

#### 7d. 审查结果回帖到 PR
主人确认：回帖到 PR 评论。
当前 `_exec_agent` 的回帖逻辑（第 2523 行）依赖 `issue_number` 回帖到 Issue。

**解决方案**：
- PR 订阅场景的 task 记录 PR number
- 执行完成后回帖到 PR 评论（调 GitCode/GitHub PR comment API）
- 需新增 PR 回帖逻辑或复用现有 VCS adapter

**风险等级：🔴 高** — 核心路由路径 + 多个下游环节适配。

---

### 改动点 8：apply_scene 适配

**文件**：`src/api/routes/scenes.py`

**改动**：
1. PR 订阅场景不走 `apply_scene`（不应设为仓库默认场景）
2. PR 订阅场景不需要创建 agent_session（已确认：run_pipeline 直接从场景 nodes 解析 skill）
3. `apply_market_scene()` 拒绝 PR 订阅类型
4. 场景市场不出现 PR 订阅场景

**风险等级：🟢 低** — 比 v2 降级，因为不需要自动创建 agent_session 了。

---

### 改动点 9：兜底场景排除

**文件**：`src/api/static/js/pages/dashboard.js`

**改动**：
- 第 1721-1726 行兜底场景下拉渲染时，过滤掉 `scene_type === 'pr_subscription'` 的场景
- `_load_scene_recommend_config()` / `_sanitize_recommend_config()` 读取时过滤 PR 订阅场景

**风险等级：🟢 低**

---

### 改动点 10：Dashboard 首个场景选中

**文件**：`src/api/routes/dashboard.py`

**改动**：
- 第 1054-1056 行 `dev_scene = scenes[0]["id"]` → 跳过 PR 订阅场景，选第一个标准场景

**风险等级：🟢 低**

---

## 六、风险汇总矩阵

| # | 改动点 | 风险 | 影响面 | 可回退性 |
|---|--------|------|--------|----------|
| 1 | DB Schema 加 scene_type 列 | 🟢 低 | DB 迁移 | 可回退 |
| 2 | 创建 API + 唯一约束 + nodes 校验 | 🟡 中 | 创建接口 | 可回退 |
| 3 | 列表/详情 API 返回 scene_type | 🟢 低 | 读取接口 | 可回退 |
| 4 | 前端创建弹窗 | 🟡 中 | 创建流程 | 可回退 |
| 5 | 前端列表标志 | 🟢 低 | 渲染层 | 可回退 |
| 6 | 前端详情页适配 | 🟡 中 | 详情/编辑 | 可回退 |
| 7 | **事件管道 PR 路由** | 🔴 高 | **核心路由** | 需 feature flag |
| 8 | apply_scene 适配 | 🟢 低 | 场景应用 | 可回退 |
| 9 | 兜底场景排除 | 🟢 低 | 推荐配置 | 可回退 |
| 10 | Dashboard 首个场景选中 | 🟢 低 | Dashboard | 可回退 |

---

## 七、建议实施顺序

```
Phase 1: DB + API 基础（改动点 1/2/3）
  → 加列 + 创建校验 + 列表返回
  → 可独立测试，不影响现有功能

Phase 2: 前端适配（改动点 4/5/6/9/10）
  → 创建弹窗 + 列表标志 + 详情页 + 兜底排除 + Dashboard
  → 此时 PR 订阅场景可创建但不会触发

Phase 3: apply_scene 适配（改动点 8）
  → PR 订阅场景不走 apply_scene + 市场排除
  → 不需要创建 agent_session（直接从场景 nodes 解析 skill）

Phase 4: 事件管道改动（改动点 7）← 最后做，风险最高
  → PR 无关联 Issue 时触发 QA
  → 包括：去重（伪 issue_id = #pr_{pr_number}）、prompt 构建、PR 评论回帖
  → skill 从场景 nodes 直接解析，不走 agent_session
  → 建议加 feature flag 控制
```

---

## 八、测试要点

1. 标准场景创建/编辑/删除 → 不受影响
2. PR 订阅场景创建 → 弹窗选择 → 只显示 QA + SRE → QA 必选
3. 同仓库创建第二个 PR 订阅场景 → 409 报错
4. PR 订阅场景不出现在兜底场景下拉
5. PR 订阅场景不出现在场景市场
6. PR 订阅场景详情页 → 只有 QA + SRE，无高级选项
7. 正常 PR + 有关联 Issue → 走标准 Issue 流水线，PR 订阅场景不触发
8. PR 无关联 Issue + 有 PR 订阅场景 → QA 触发
9. PR 无关联 Issue + 无 PR 订阅场景 → 维持 skip
10. 同一 PR 多次 push → 不重复触发 QA（去重生效）
11. QA 审查结果 → 回帖到 PR 评论
12. Dashboard 首个场景 → 不是 PR 订阅场景

---

## 九、已确认事项汇总

所有待确认问题已解决，无遗留：

1. **触发条件**：只有新 commit 才触发（L1 已过滤无 commit 的 PR update），同时确认该 PR 没有关联 Issue。`pull_request_merged` 也可以触发（如果有新 commit）。
2. **SRE 角色**：由定义场景的人决定是否加入 SRE。已有 A2A 逻辑支持 QA→SRE 交接，不需要额外开发。
3. **onboarding 字段**：对 PR 订阅场景无实际意义。event_pipeline.py 已注释 `onboarding 已不使用，Issue #46`。保持默认值即可。
4. **Agent Session**：run_pipeline 中直接从场景 nodes 解析 skill，不创建 agent_session。
5. **PR 后续关联 Issue 竞态**：良性，两个场景验证不同问题，不做特殊处理。
