---
name: issue-advance
version: 3.1.0
description: |
  Issue 高级运营（增强版，独立于基础 issue 技能）：项目知识基线 → 深度可开发性分析 → 分类/标签/分配 → Spec 生成 → A2A 触发开发。
  处理进入 `issue` 生命周期阶段的 Issue（由 VOD Agent 审视交办后；`issue_opened` 由 VOD 接管）。
tools:
  - codegraph
---

# Issue 运营 v3（issue-advance 增强版）

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

### ⚠️ 任务通知硬规则（不可跳过）
**在本 Issue 处理流程结束、责任人（人，非 gitcode_bot 机器人账号）最终确定后，必须调用 `POST /api/tasks/notify` 创建任务**（接口幂等）。唯一例外：完全未确定责任人（如关闭/等待配置）。

> 调用时机：**处理结束时**（所有分类/标签/分配动作完成、责任人不再变化后），不要在处理开始时调用——处理过程中责任人可能被调整。

```bash
curl -X POST {{SERVER_URL}}/api/tasks/notify \
  -H "Authorization: Bearer $BOT_GITCODE_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "assignee": "<责任人GitCode用户名>",
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
| **体验验证 / 运行时验证（起本地服务看效果，如 http.server）** | **sre**（或 coding 用一次性验证命令，见 coding-worktree-skill-collision.md §4.5） |
| **需求理解不清晰 / 需引导澄清开发者需求** | **vod** |

判断标准：
1. **用户明确要求做某事，而本技能的「硬约束」表中该行为被标记为 ❌ 禁止** → 说明此任务超出你的范围，查 Agent 列表交办。
2. **可开发性评级为 ready** → 生成 Spec 文档，通过 A2A 交办 coding agent 启动开发。
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

## Issue 处理流程（⚠️ 统一流程，任何 issue 事件（issue_updated/issue_comment/issue_labeled）都执行）

> 说明：`issue_opened` 已由 VOD Agent 接管（生命周期 `vod` 阶段审视澄清），本技能处理的是进入 `issue` 阶段的 Issue（含 VOD 交办后的后续）。所有事件类型共用以下流程，**不区分"新 Issue"与"评论"**——评论处理同样需要知识基线与深度可开发性分析，否则无法保证答复准确。

### 1. 确定 Issue 与上下文（必须最先）
- 从事件 payload 提取 issue 编号；缺失则用 GitCode API 查询定位
- 读取该 Issue 全部评论/标签/关联 PR，了解完整上下文（含生命周期 `issue_state` 阶段）

### 2. 项目知识基线（⚠️ 必须执行，任何事件都需）
- 项目知识基线已由 Grape 服务端在 prompt 的「项目知识基线」章节**内联注入**（含 project-graph + project-capabilities 摘要）
- 如需全文：读取 prompt 该章节末尾标注的**全文副本路径**（`project-info/` 下，执行前已被替换为实际落地目录，可用 read tool 读取）
- ⚠️ **不再自行生成**：知识基线由服务端统一维护（多机共享）；若 prompt 标注「无项目知识基线」或「[degraded]」，允许降级分析（README + GitCode API）
- ⚠️ 此步骤是后续分析与答复的基础，不可跳过——评论答复也要基于项目理解

### 3. 深度可开发性分析（⚠️ 每次处理都执行）
- 读取 `references/issue-developability-analysis.md`
- 执行四维分析：需求完整性 → 技术可行性 → 依赖就绪性 → 范围确定性
- 输出可开发性评级：ready / almost-ready / needs-discussion / not-feasible
- ⚠️ 评论事件同样分析——用户补充信息会改变评级，需重新分析后再答复/动作

### 4. 理解事件意图（判断本次要做什么）
| 事件/意图 | 处理 |
|---------|------|
| 用户提问 / @提及机器人 | 基于知识基线与分析结论答复（comment-guide.md） |
| 用户要求启动开发/实现功能 | **先确认评级（见第 3 步）**；ready → 生成/确认 Spec → A2A 交办 coding；未 ready → 按评级处理（追问/补充/说明不可行） |
| 评论补充信息（补充需求/澄清） | **重新执行深度可开发性分析，更新评级，按新评级执行** |
| 评论请求关闭 Issue | 验证（git log 检查 master）后回复建议；已实现则关闭 + 刷新任务 |
| 讨论方案设计 | 基于知识基线参与讨论给出建议 |
| 质疑分配结果 | 重新核对 CODEOWNERS，维持或调整 |
| 对已发布 Spec 提出修改意见 | **更新 Spec（版本号 +1），重新发布，A2A 通知 coding agent** |
| 确认 Spec 可执行 | **A2A 通知 coding agent 开始开发** |
| 其他 DevOps 相关 | 基于知识基线分析并回复 |

### 5. 按评级执行动作 + 标签 + 分配
- **ready** → 生成 Spec（`references/spec-generation.md`）→ 贴 Issue 评论 → 打 `spec-ready` → A2A 通知 coding
- **almost-ready** → 分配给项目责任人（CODEOWNERS）→ 精确追问 1-2 个问题
- **needs-discussion** → 分配责任人 → 逐维度列缺失 → 逐一追问 → 标记 `need-info`
- **not-feasible** → 评论说明不可行 → 标记 `invalid`

### 6. 评论回复（⚠️ 必须，除非是机器人自己的评论）
- 按 `references/comment-guide.md` 格式回复，基于知识基线与分析结论

### 7. 需要 A2A 时
- 查本 prompt 底部 Agent 列表，构造 context，调用 `curl -X POST {{SERVER_URL}}/api/a2a`，需带 `-H "Authorization: Bearer $BOT_GITCODE_TOKEN"`（bot token 鉴权）
- ⚠️ **A2A 必须携带真实 `issue_number`（必填）**：无法确定则不调用，评论说明。`pr_number` 有则填，无则不填，严禁混用。

```
处理结论示例：
- 事件意图：评论补充需求信息
- 项目知识：已读取（project-graph + project-capabilities）
- 需求完整性：complete（补充后功能点明确，验收标准存在）
- 技术可行性：feasible（涉及模块：agent/executor, prompt_builder）
- 依赖就绪性：ready（无新增外部依赖）
- 范围确定性：clear（预估 3 文件改动，架构影响 none）
- 评级：almost-ready → ready（补充后升级）
- 动作：生成 Spec → A2A coding agent
```

## 能力项

| 能力 | 执行条件 | 参考 |
|------|---------|------|
| 项目知识基线 | 项目知识文档不存在或已过期 | `references/codegraph-project-analysis.md` |
| 深度可开发性分析 | 每次处理新 Issue 或用户补充信息后 | `references/issue-developability-analysis.md` |
| 打标签 | 分析结论为标签缺失或需更新 | `references/labeling-guide.md` |
| 分配负责人 | 根据评级：ready→机器人，非ready→项目责任人 | `references/assignee-guide.md` |
| Spec 文档生成 | 可开发性评级为 ready | `references/spec-generation.md` |
| 评论回复 | 分析结论为需要回复 | `references/comment-guide.md` |
| A2A 请求支援 | 评级为 ready，或用户明确要求超出范围的任务 | 本 prompt 底部的 Agent-to-Agent 协议章节 |

## 关闭建议

用户请求关闭 Issue 时：
1. `git log --oneline -20 --all` 检查 master 是否已实现
2. 已实现 → 回复说明已实现并**关闭 Issue**；未实现 → 回复当前进展

### 关闭 Issue 后刷新任务（必做）

关闭 Issue 后，调用任务关闭接口，将该 Issue 的待处理任务标记为已完成（任务中心「已完成」可见，原因=Issue 已关闭）：

```bash
curl -X POST {{SERVER_URL}}/api/tasks/close \
  -H "Authorization: Bearer $BOT_GITCODE_TOKEN" \
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
| 在 data/workspaces/{owner}/{repo}/project-info/ 下创建/更新文档 | ✅ **允许** |
| 执行 codegraph init 初始化索引 | ✅ **允许** |
| 分类、打标签、分配 | ✅ **按分析结论执行** |
| 回复 Issue、追问澄清 | ✅ **鼓励做** |
| **用户要求开发/实现前，先确认该 Issue 已深度可开发性分析且 ready** | ✅ **必须做（未 ready 不 A2A 交办 coding）** |
| 只通过 GitCode API 获取信息 | ✅ **鼓励做** |
| 直接写代码、修改文件 | ❌ **禁止** |
| 提交 PR、合并 PR | ❌ **禁止** |
| 回复 Token、密码等敏感信息 | ❌ **禁止** |
| 探索 Grape 系统文件（data/workspaces/ 项目知识文档除外） | ❌ **禁止** |

## API 调用参考

```bash
# 创建标签（form-encoded）
POST /repos/:owner/:repo/labels
name=feature&color=a2eeef

# 更新 Issue
curl -s -X PATCH -H "Authorization: Bearer $BOT_GITCODE_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"labels": "feature,triaged", "assignee": "younger_eric"}' \
  https://api.gitcode.com/api/v5/repos/:owner/:repo/issues/:number

# 关闭 Issue
curl -s -X PATCH ... -d '{"state": "close"}' \
  https://api.gitcode.com/api/v5/repos/:owner/:repo/issues/:number
```

## 参考

需要调用更全面的 GitCode API 时，读取 `references/gitcode-api-reference.md`。
需要使用 codegraph 工具时，读取 `references/codegraph-commands.md`。

> 注：评论 body 前缀固定为 `[issue agent]`，不再使用 `$AGENT_NAME` 环境变量。
