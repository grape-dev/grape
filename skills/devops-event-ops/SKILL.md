---
name: devops-event-ops
version: 1.0.0
description: |
  DevOps 事件运营全链路：事件接收 → 归一化 → 规则路由 → Session 复用 → Agent 调度 → 结果记录 → 失败自愈。
  当 DevOps Bot 收到任何平台事件（GitHub/GitLab/GitCode/Jenkins/Spanner Webhook）、
  需要处理 Issue Triage、PR Review、构建失败诊断、部署守卫、事件分诊、安全扫描时，
  必须使用此技能。即使事件看起来简单，也要按本技能的流程走，不能跳过归一化和路由。
  不适用于：Agent 内部逻辑实现、非 DevOps 领域的通用编程任务。
triggers:
  - event_type: push
  - event_type: pull_request_opened
  - event_type: pull_request_updated
  - event_type: issue_opened
  - event_type: issue_comment
  - event_type: build_failure
  - event_type: deploy_start
  - event_type: release_created
tools:
  - curl
  - git
---

# DevOps Event Ops — 事件运营全链路


## 事件处理铁律

1. **事件到达即行动。** Webhook 通知不是 FYI，是行动触发。即使 payload 里没有"自动处理"标志，也要按本流程做首反：归一化 → 路由 → 创建/复用 Session → 调度 Agent。

2. **外部 claim 一律先验证。** PR body / commit message / 评论里写的 `fixes #NNN`、`tests pass`、`small fix` 都只是 claim，不是真相源。Agent 处理时应先打开原对象核对，没核过就不能拿它做决策依据。

3. **已有 PR 修此 issue → 默认任务是 review，不是重新实现。** 如果关联 PR 已存在，Agent 的下一步应该是 `review-existing-pr`，不是从零修。只有 PR 方向不对、质量不达标、或作者明确放弃时，才另开实现。

4. **Session 按 repo 维度复用。** 同一 repo 的所有事件进入同一 Session，Agent 可看到项目全部上下文。不同 repo 的 Session 完全隔离。

5. **失败不是重试同一操作。** 失败后将失败上下文交给 Agent，让 Agent 理解失败并决策下一步。Agent 可能选择：换策略重试、降级处理、请求人工介入。

## 场景路由

你是所有 GitCode 事件的统一入口。根据事件类型决定处理流程：

| 触发事件 | 处理方式 | 加载 Skill |
|---------|---------|-----------|
| issue_opened | Issue 创建全流程 | `issue` — 分类、打标签、分配、里程碑、评论 |
| issue_comment | Issue 评论回复 | `issue` — 分析评论、更新标签/状态 |
| pull_request_opened | PR 创建全流程 | `pr` — 关联 Issue、代码审查、安全扫描、合并检查 |
| pull_request_updated | PR 代码更新 | `pr` — 增量审查 + 扫描 |
| push | 代码推送 | `pr` — 简要审查（非 PR）或 `build-debug`（CI 相关） |
| note_hook_pr | PR 评论 | `pr` — 分析并回复 |
| build_failure | 构建失败诊断 | `build-debug`, `incident-triage` |
| build_success | 构建检查 | `deploy-guard` |
| deploy_failure | 部署失败分诊 | `incident-triage`, `deploy-guard` |
| deploy_start | 部署前检查 | `deploy-guard` |
| release_created | 发布处理 | `changelog-gen` |

### 路由执行指令（必须遵守）

**收到 `issue_opened` 或 `issue_comment` 事件：**
1. 立即打开并完整阅读 `skills/issue/SKILL.md`
2. 严格按照其中的强制流程执行每一步
3. 不得跳过标签设置、分配、评论回复步骤

**收到 `pull_request_opened`、`pull_request_updated` 或 PR 评论事件：**
1. 立即打开并完整阅读 `skills/pr/SKILL.md`
2. 严格按照其中流程执行

需要调用 GitCode API 时，读取当前目录下的 `references/gitcode-api-reference.md`。

## 处理流程

### 阶段 1：事件接收与归一化

每个事件源（GitHub/GitLab/GitCode/Jenkins/Spanner）的 Webhook payload 格式不同。Bot 的 Source 层负责将异构事件转为统一的 `NormalizedEvent`：

```
原始 Webhook payload → Source.Normalize() → NormalizedEvent
```

统一字段：`source`, `type`, `repository`, `branch`, `commit`, `actor`, `payload`, `raw_payload`

**关键：归一化是所有后续处理的前提。** 未归一化的事件不能进入路由。

### 阶段 2：规则路由

`rules.yaml` 中声明式定义路由规则。路由匹配 `source` + `event type`，确定：
- 使用哪个 Agent
- 使用哪个 Prompt 模板
- 是否需要 clone workspace

未匹配任何规则的事件被忽略并记录日志。

### 阶段 3：Session 复用

```
repo_key = "{source}:{repository}"  // 如 "gitcode:owner/repo"
```

- 同一 `repo_key` 的所有事件进入同一 Session
- Session 包含完整事件历史（`EventHistory`）
- 新事件到达时，Agent 收到的 Prompt 包含：
  1. Session 上下文摘要（repo、历史事件数、最近 10 条事件）
  2. 当前事件的完整指令

### 阶段 4：Agent 调度

Bot 通过 CLI 调用 Agent，传入：
- `--prompt`：包含 Session 上下文 + 当前事件指令的完整 Prompt
- `--workspace`：repo 的工作目录
- `DEVOPS_SKILLS_DIR`：Skills 目录路径（Agent 自行发现和触发技能）
- `DEVOPS_SESSION_ID`：当前 Session ID

Agent 启动后：
1. 读取 `DEVOPS_SKILLS_DIR` 下的技能目录
2. 根据事件类型和上下文，自行匹配并加载需要的 Skills
3. 执行技能中定义的步骤
4. 返回处理结果

### 阶段 5：结果记录

- 成功：事件记录追加到 `EventHistory`，Session 状态 → `idle`
- 失败：进入失败自愈流程

### 关于自循环

Bot 收到的事件如果是 Bot 自己发出的评论（`[DevOps Bot]` 前缀 + Bot 账号发布），Bot 已在代码层面过滤掉，不会发给 Agent。Agent 无需处理自循环问题。

### 阶段 6：失败自愈

```
Agent 执行失败
  → 构造失败 Prompt（包含：Session 上下文 + 失败原因 + 错误输出 + 原始事件）
  → 再次调用同一 Agent
  → Agent 分析失败并决策下一步
  → 成功 → 记录 / 仍失败 → Session 标记 failed，人工介入
```

## 关键原则

1. **Maintainer 目标优先**：处理社区事件的第一职责是守住项目质量；外部请求是输入信号，不是任务分派单。每个 Issue/PR 都先问：对项目有益吗？实际改了什么？值得 merge 吗？

2. **Issue accept 是 Merge 前提**：无 accepted issue 不得 merge 社区 PR。

3. **一条线不断裂**：事件接收 → 归一化 → 路由 → Session → Agent → 结果记录，每一步的输出是下一步的输入，不能跳步。

4. **Consult freely, decide carefully**：Agent 可以自主拉取上下文做评估/review，但 merge/close/roadmap 等硬决策需要人工确认。

5. **守门不修 bug**：事件首反可以打标签、发评论、路由到处理者；不要因为 bug 看起来好修就直接开修。只有明确授权或 P0 安全事故需要止血时，才允许直接进入实现。

6. **Session 是 Agent 的记忆**：Session 中的 EventHistory 让 Agent 理解项目全貌，避免重复分析和矛盾决策。

## Common Mistakes

| 错误 | 后果 | 修复 |
|------|------|------|
| 只看事件摘要就回复"无法处理" | 漏掉守门职责，Issue/PR 悬空 | 事件到达即加载本 skill，打开原对象做首反 |
| 把社区伙伴写的 feat 号 / issue 号当事实 | 伪锚点污染真相源，后续跳过方向判断 | 外部 claim 一律打开原对象核验 |
| 看到 bug 就直接开修 | 守门变成工程线程，失去路由控制 | 先首反 + 路由；实现分发到独立处理者 |
| 社区已有 PR 修 issue，却写成"请修复" | 接球者误以为要重写，绕过社区贡献 | linked PR 存在时，下一步默认是 review |
| 活跃 contributor 刚提 issue，就立刻内部修 | 抢掉社区贡献机会，制造重复 PR | 先问作者是否计划自提 PR；短 SLA 后再转内部 |
| 不判断事件和现有 Session 历史的关系 | 重复处理、矛盾决策 | Agent 处理前先读 Session.EventHistory |
| 失败后盲目重试同一操作 | 反复失败，浪费资源 | 失败后让 Agent 理解失败原因再决策 |
| 给 Agent 注入 Skills 内容而非路径 | Bot 耦合了 Skills 实现，新增 Skill 需改 Bot | 只传 DEVOPS_SKILLS_DIR，Agent 自行发现 |

## 和其他 Skill 的关系

本 skill 是**编排层**，不实现具体逻辑。具体能力由 Agent 加载的 Skills 提供：

| 场景 | 加载 Skill |
|------|-----------|
| Issue 全生命周期 | `issue` — 分类、标签、分配、里程碑、评论 |
| PR 全生命周期 | `pr` — 代码审查、安全扫描、合并检查、评论 |
| 构建失败诊断 | `build-debug` |
| 部署守卫 | `deploy-guard` |
| 事件分诊 | `incident-triage` |
| 变更日志 | `changelog-gen` |

## 进阶参考

- `references/event-flow.md` — 完整事件处理流程图与数据流
- `references/session-lifecycle.md` — Session 生命周期与状态机
- `references/gitcode-api-reference.md` — GitCode API 端点与参数参考
