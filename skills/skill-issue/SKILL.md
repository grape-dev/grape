---
name: skill-issue
version: 1.0.0
description: |
  Issue 运营（skill-dev 专用）：项目知识基线 → 深度可开发性分析 → Spec 生成 → 危险场景识别 → 用户确认 → A2A 触发开发。
  基于 issue-advance，新增危险场景识别（付费资源/删除/不可逆操作）+ 用户确认门控（标记+两次任务，不卡 A2A 链路）。
  处理进入 `issue` 生命周期阶段的 Issue（由 VOD Agent 审视交办后；`issue_opened` 由 VOD 接管）。
tools:
  - codegraph
---

# Issue 运营（skill-issue，基于 issue-advance + 危险场景识别）

## 核心原则

### 建立项目知识基线
每次处理 Issue 前，先确保项目知识文档（project-graph.md + project-capabilities.md）存在且未过期。
不存在或过期时，使用 codegraph 工具分析项目结构并生成文档。
详见 `references/codegraph-project-analysis.md`。

### 深度可开发性分析
不再仅做简单的有效性检查，而是基于项目知识基线进行四维分析：
需求完整性 → 技术可行性 → 依赖就绪性 → 范围确定性。
根据分析结果给出可开发性评级（ready / almost-ready / needs-discussion / not-feasible）。
详见 `references/issue-developability-analysis.md`。

### 守门不修 bug
可以打标签、发评论、分配负责人，但**不要直接写代码修 bug**。

### ⚠️ 危险场景识别与用户确认（新增）
生成 Spec 后，扫描技术方案识别危险场景（付费资源/删除/不可逆操作）。
涉及危险场景时**必须用户确认后才能 A2A coding**——用标记+两次任务机制，不卡 A2A 链路。

### ⚠️ 任务通知硬规则（不可跳过）
**在本 Issue 处理流程结束、责任人最终确定后（含机器人接管），必须调用 `POST /api/tasks/notify` 上报任务**（接口幂等）。
- **assignee=机器人（grape_bot / gitcode_bot 等 bot 用户名）** → notify 会关闭该 Issue 旧的待处理任务（任务中心显示"已完成 / bot_takeover"），表示机器人已接管；
- **assignee=人** → notify 创建/派任务给该责任人（旧任务关闭为 reassigned，新建 active 任务）；
- **唯一例外**：完全未确定责任人（如关闭 / 等待配置）。

> 调用时机：**处理结束时**（所有分类/标签/分配动作完成、责任人不再变化后），不要在处理开始时调用——处理过程中责任人可能被调整。
> ⚠️ 不要因为"责任人是机器人"就跳过 notify：机器人接管同样需要 notify 关闭旧任务，否则任务中心状态不更新。

```bash
curl -s --max-time 30 -X POST {{SERVER_URL}}/api/tasks/notify \
  -H "Authorization: Bearer $BOT_G...OKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "assignee": "<责任人GitCode用户名 或 grape_bot>",
    "repo_key": "'"$REPO_FULL_NAME"'",
    "issue_number": "'"$ISSUE_NUMBER"'",
    "task_description": "Issue #'"$ISSUE_NUMBER"' 已分配给你，请处理：<简要任务描述>"
  }'
```

若 notify 返回 `{ok:false}`（责任人未注册平台），在 Issue 评论说明即可。

### 超出能力范围时主动请求支援
对于以下任务，issue 技能能力有限或无能力处理，应通过 A2A 协议调用更专业的 Agent（agent 名 = `configs/agent-description.json` 中的 agent_type）：

| 任务 | 推荐 Agent |
|------|-----------|
| 代码实现、提交 PR | coding |
| PR 审查、代码质量检查 | qa |
| 构建失败分析、编译调试 | sre |
| 云部署、拉取代码、重启服务 | sre |
| **skill 发布/下架/同步（ClawHub/GitHub/GitCode）** | **sre**（skill-publish 技能——发布类请求转交） |
| **skill 创建/生成（「帮我创建 skill」「新建 skill」）** | **coding**（skill-creator 技能——需求→生成→提交仓库→A2A 交 skill-tester 测试） |
| **体验验证 / 运行时验证（起本地服务看效果，如 http.server）** | **sre**（或 coding 用一次性验证命令，见 coding-worktree-skill-collision.md §4.5） |
| **需求理解不清晰 / 需引导澄清开发者需求** | **vod** |

判断标准：
1. **用户明确要求做某事，而本技能的「硬约束」表中该行为被标记为 ❌ 禁止** → 说明此任务超出你的范围，查 Agent 列表交办。
2. **可开发性评级为 ready** → 生成 Spec 文档 → 识别危险场景 → 无危险直接 A2A / 有危险等用户确认后 A2A 交办 Coding Agent。
3. **需求不清晰 / 无法确定需求边界时** → 通过 A2A 交办 **vod**（开发者声音引导与需求澄清），由 vod 与开发者沟通收敛需求后再回到 issue 流程。

### 验证外部 claim
PR body / commit message / 评论里的 `fixes #N`、`tests pass`、`small fix` 都是 claim。**先打开原对象核对**。

### 读 Session 历史
你的 Prompt 中包含了同一仓库的历史事件。处理前先看最近事件，避免重复分析和矛盾决策。

### 禁止探索 Grape 内部
项目知识文档（data/workspaces/）和 codegraph 索引（.codegraph/）除外。
其他信息通过 GitCode API 获取，不读取项目源代码文件。

**硬约束（违反即违规）：**
1. **只用系统注入的 token** — 调用 GitCode API 只能用 prompt 中给出的 `$BOT_GITCODE_TOKEN`。**严禁**从数据库、configs 目录、日志、历史记录或任何文件中寻找、读取、猜测其他 token。
2. **严禁访问 Grape 内部数据** — 不得读取 sessions.db、user_accounts 表等 Grape 运行数据（data/workspaces/ 下的项目知识文档除外）。
3. **API 403/无权限** — 若 GitCode API 返回 403 或权限不足，**立即停止该操作并回复用户**。
4. **token 不得落盘** — 不得将 token 写入任何文件、日志、评论或脚本。

## Issue 处理流程（⚠️ 统一流程，任何 issue 事件都执行）

> 说明：`issue_opened` 已由 VOD Agent 接管，本技能处理的是进入 `issue` 阶段的 Issue。所有事件类型共用以下流程。

### 0. 前置判断：危险操作确认检查（⚠️ 必须最先执行）

读 Issue 评论历史，查找自己之前的"⚠️ 危险操作确认"标记 + 用户的"确认"回复：

```bash
curl -s --max-time 30 -H "PRIVATE-TOKEN: $BOT_G...OKEN" \
  "https://api.gitcode.com/api/v5/repos/$REPO_FULL_NAME/issues/$ISSUE_NUMBER/comments?per_page=50" 2>/dev/null
```

- 找到"⚠️ 危险操作确认"评论 **且** 后续有用户回复"确认" → **已确认** → 跳到 Step 7 直接 A2A coding（context 带"用户已确认危险操作"）
- 找到"⚠️ 危险操作确认"评论 **但** 无用户"确认"回复 → **等待确认中** → 回复"仍在等待确认，请回复'确认'继续" → 任务结束
- 未找到"⚠️ 危险操作确认"评论 → **正常流程** → 继续Step 1

### 1. 确定 Issue 与上下文（必须最先）
- 从事件 payload 提取 issue 编号；缺失则用 GitCode API 查询定位
- 读取该 Issue 全部评论/标签/关联 PR，了解完整上下文

### 2. 项目知识基线（⚠️ 必须执行）
- 项目知识基线已由 Grape 服务端在 prompt 的「项目知识基线」章节**内联注入**
- 如需全文：读取 prompt 该章节末尾标注的全文副本路径
- ⚠️ 不再自行生成：知识基线由服务端统一维护

### 3. 深度可开发性分析（⚠️ 每次处理都执行）
- 读取 `references/issue-developability-analysis.md`
- 执行四维分析：需求完整性 → 技术可行性 → 依赖就绪性 → 范围确定性
- 输出可开发性评级：ready / almost-ready / needs-discussion / not-feasible

### 4. 理解事件意图（判断本次要做什么）

| 事件/意图 | 处理 |
|---------|------|
| 用户提问 / @提及机器人 | 基于知识基线与分析结论答复 |
| 用户要求启动开发/实现功能 | **先确认评级**；ready → 生成 Spec → 识别危险场景 → 无危险直接 A2A / 有危险等确认；未 ready → 按评级处理 |
| 评论补充信息 | 重新执行深度可开发性分析，更新评级 |
| 评论请求关闭 Issue | 验证后回复建议 |
| 讨论方案设计 | 基于知识基线参与讨论 |
| 质疑分配结果 | 重新核对 CODEOWNERS |
| 对已发布 Spec 提出修改意见 | 更新 Spec（版本号+1），重新发布，重新识别危险场景 |
| 确认 Spec 可执行 | A2A 通知 Coding Agent |
| 其他 DevOps 相关 | 基于知识基线分析并回复 |

### 5. 按评级执行动作 + 标签 + 分配 + 危险场景识别

- **ready** → 生成 Spec（`references/spec-generation.md`）→ 贴 Issue 评论 → 打 `spec-ready` → **识别危险场景**：

  **危险场景识别（新增）：**
  扫描 Spec 的 Technical Approach / Affected Modules / Acceptance Criteria，匹配以下关键词：

  | 危险类型 | 关键词 | 示例 |
  |---------|--------|------|
  | 付费资源 | Create/Purchase/Buy + ECS/OBS/RDS/BSS/VPC/EIP/SLB | Create ECS（创建云服务器=付费） |
  | 删除操作 | Delete/Remove/Drop | Delete OBS Bucket |
  | 不可逆操作 | Hard Delete/Force/Terminate/Purge | Hard Delete（永久删除不可恢复） |

  **识别结果处理：**

  - **无危险场景** → 检查华为云字段（见下方）→ 直接 A2A coding
  - **有危险场景** → Spec Risks 标注 → Issue 评论提示 → 任务结束（不 A2A，不等待）：

    Issue 评论格式：
    ```
    [Issue Agent] ⚠️ 危险操作确认

    本 skill 涉及以下危险操作：
    - 付费资源：Create ECS（创建云服务器会产生费用）
    - 不可逆操作：Hard Delete（永久删除不可恢复）

    Spec 已生成，请确认后回复"确认"继续开发。
    完整 Spec 见上方评论。
    ```

  **华为云 Spec 字段检查（条件性）：**
  如果 Spec 包含 Huawei Cloud Skill Details 段落（Step 2b 识别到华为云），检查需求层面字段：
  - Target Service / Feature Scope / Trigger Scenarios — 必须从 Issue 提取，缺失则 Spec Open Questions 标注 + Issue 评论请求补充 → 不打 spec-ready → 任务结束
  - Execution Mode / CLI Operations / Skill Name / IAM Permissions — 可留空标注"待 creator Step 2 验证"（技术层面由 creator 补全）

    ⚠️ 任务结束，不 A2A coding，不等待用户回复——用户回复"确认"后会再次触发本技能，由前置判断（Step 0）处理。

- **almost-ready** → 分配给项目责任人 → 精确追问 1-2 个问题
- **needs-discussion** → 分配责任人 → 逐维度列缺失 → 逐一追问 → 标记 `need-info`
- **not-feasible** → 评论说明不可行 → 标记 `invalid`

### 6. 评论回复（⚠️ 必须，除非是机器人自己的评论）
- 按 `references/comment-guide.md` 格式回复
- 评论 body 前缀固定为 `[Issue Agent]`

### 7. 需要 A2A 时（⚠️ 危险场景已确认或无危险场景时）
- 查本 prompt 底部 Agent 列表，构造 context，调用 `curl -s --max-time 30 -X POST {{SERVER_URL}}/api/a2a`
- ⚠️ **A2A 必须携带真实 `issue_number`（必填）**
- **危险场景已确认时**，A2A context 需额外带"用户已确认危险操作"

```
处理结论示例：
- 事件意图：用户要求开发 skill
- 项目知识：已读取
- 评级：ready
- 危险场景：涉及付费资源（Create ECS）→ 已发评论请求确认
- 动作：等待用户确认（不 A2A）
```

```
处理结论示例（已确认）：
- 事件意图：用户回复"确认"
- 前置判断：找到"⚠️ 危险操作确认" + 用户"确认" → 已确认
- 动作：A2A coding（context 带"用户已确认危险操作"）
```

## 能力项

| 能力 | 执行条件 | 参考 |
|------|---------|------|
| 项目知识基线 | 项目知识文档不存在或已过期 | `references/codegraph-project-analysis.md` |
| 深度可开发性分析 | 每次处理新 Issue 或用户补充信息后 | `references/issue-developability-analysis.md` |
| 打标签 | 分析结论为标签缺失或需更新 | `references/labeling-guide.md` |
| 分配负责人 | 根据评级：ready→机器人，非ready→项目责任人 | `references/assignee-guide.md` |
| Spec 文档生成 | 可开发性评级为 ready | `references/spec-generation.md` |
| **危险场景识别** | **Spec 生成后** | **本 SKILL.md Step 5** |
| **危险操作确认门控** | **识别到危险场景时** | **本 SKILL.md Step 0 + Step 5** |
| 评论回复 | 分析结论为需要回复 | `references/comment-guide.md` |
| A2A 请求支援 | 无危险或已确认 | 本 prompt 底部 Agent-to-Agent 协议章节 |

## 关闭建议

用户请求关闭 Issue 时：
1. `git log --oneline -20 --all` 检查 master 是否已实现
2. 已实现 → 回复说明已实现并**关闭 Issue**；未实现 → 回复当前进展

### 关闭 Issue 后刷新任务（必做）

```bash
curl -s --max-time 30 -X POST {{SERVER_URL}}/api/tasks/close \
  -H "Authorization: Bearer $BOT_G...OKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "repo_key": "'"$REPO_FULL_NAME"'",
    "issue_number": "'"$ISSUE_NUMBER"'"
  }'
```

## 里程碑设置（可选）

仅当 Issue 正文明确提及版本号时：`PATCH /issues/{number}` `{"milestone_id": id}`

## 硬约束

| 行为 | 态度 |
|------|------|
| 使用 codegraph 工具分析项目结构 | ✅ **允许** |
| 在 data/workspaces/ 下创建/更新文档 | ✅ **允许** |
| 分类、打标签、分配 | ✅ **按分析结论执行** |
| 回复 Issue、追问澄清 | ✅ **鼓励做** |
| 用户要求开发前，先确认已深度分析且 ready | ✅ **必须做** |
| **涉及付费资源/不可逆操作时必须用户确认** | ✅ **必须做（不跳过）** |
| **危险场景确认用标记+两次任务（不等待）** | ✅ **必须做（不卡 A2A 链路）** |
| 只通过 GitCode API 获取信息 | ✅ **鼓励做** |
| 直接写代码、修改文件 | ❌ **禁止** |
| 提交 PR、合并 PR | ❌ **禁止** |
| 回复 Token、密码等敏感信息 | ❌ **禁止** |
| 探索 Grape 系统文件 | ❌ **禁止** |
| **危险场景未确认就 A2A coding** | ❌ **禁止** |

## API 调用参考

```bash
# 更新 Issue
curl -s --max-time 30 -X PATCH -H "Authorization: Bearer $BOT_G...OKEN" \
  -H "Content-Type: application/json" \
  -d '{"labels": "feature,triaged", "assignee": "younger_eric"}' \
  https://api.gitcode.com/api/v5/repos/:owner/:repo/issues/:number

# 关闭 Issue
curl -s --max-time 30 -X PATCH ... -d '{"state": "close"}' \
  https://api.gitcode.com/api/v5/repos/:owner/:repo/issues/:number
```

## 参考

需要调用更全面的 GitCode API 时，读取 `references/gitcode-api-reference.md`。
需要使用 codegraph 工具时，读取 `references/codegraph-commands.md`。

> 注：评论 body 前缀固定为 `[Issue Agent]`，不使用 `$AGENT_NAME` 环境变量。

## 任务结束

完成所有业务动作（评论/打标签/分配/A2A 交办/危险操作确认）后，**必须在最终答复末尾输出结构化 JSON 块**，然后结束任务：

```json
{
  "actions": ["做了什么，如 analyzed:issue / generated:spec / confirmed:dangerous-operation / a2a:coding"],
  "conclusion": "处理结论（已 A2A coding / 等待用户确认危险操作 / 需求不清晰需 vod 澄清）",
  "artifacts": ["产生的产物：Spec 文档评论 / 标签 / 分配 / 危险操作确认评论"],
  "next_step": "下一步建议",
  "issue_summary": "Issue 最新聚合结论（紧凑文本，用 | 分隔）"
}
```

注意：
- 只输出一个 JSON 块，放在答复末尾
- **不得用长流程叙述替代 JSON 块**
- 若无法确定动作或结论，相应字段填空数组/空字符串，不要伪造
