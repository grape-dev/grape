---
name: issue-multitask
version: 1.0.0
description: |
  多任务模式处理（子任务拆分与快速通道）：当 Issue 需求复杂、预估单节点耗时 > 20 分钟时，
  将方案拆分为可独立交付的子任务，创建子 Issue 并行/串行执行；子任务跳过分析与 Spec 生成，
  直接从父任务提取目标并 A2A 交办 coding。
  处理含 `[multi-task-mode]` 标记或「子任务上下文」的 Issue（由 Issue Agent 判定后进入本技能）。
tools: []
---

# 多任务模式处理（子任务拆分 + 子任务快速通道）

## 触发条件

| 触发场景 | 识别标记 | 来源 |
|---------|---------|------|
| 多任务模式（父任务拆分） | Issue body 含 `[multi-task-mode]` 或 prompt 上下文含「多任务模式」 | VOD 识别后 A2A 传入 `is_subtask_mode: true` |
| 子任务快速通道 | Issue body 含「## 父任务」+ Issue 链接，或 prompt 注入 `parent_issue` | 服务端 event_pipeline 解析子任务 parent_issue_id |

## 一、多任务模式——子任务拆分

当 prompt 上下文包含「多任务模式」或 Issue body 含 `[multi-task-mode]` 标记时：

### 1.1 方案设计后评估可拆分性

基于 Spec 评估：
- 任务总预估时间 > 20 分钟/节点？
- 是否可拆分为独立的子任务（每个子任务可独立 PR）？
- 子任务之间是否有强依赖？（有依赖 → 按依赖顺序创建，无依赖 → 并行创建）

### 1.2 创建子 Issue

对每个子任务，调用 GitCode API 创建 Issue：

```bash
curl -X POST "https://api.gitcode.com/api/v5/repos/{owner}/{repo}/issues?access_token=$BOT_G...OKEN" \
  -H "Content-Type: application/x-www-form-urlencoded" \
  -d "title=子任务：数据库迁移 SQLite → MySQL" \
  -d "body=## 父任务\n[Issue #20](https://gitcode.com/{owner}/{repo}/issues/20)\n\n## 目标\n{从 Spec 中提取的子任务目标}\n\n## 验收标准\n{子任务验收标准}\n\n## 依赖\n{如有前置子任务，注明}" \
  -d "assignee=grape_bot"
```

每个子 Issue body 必须包含：
- `## 父任务` + GitCode Issue 链接
- 子任务目标 / 验收标准 / 依赖

### 1.3 更新父 Issue

在父 Issue body 中追加子任务列表：

```bash
curl -X PATCH "https://api.gitcode.com/api/v5/repos/{owner}/{repo}/issues/20?access_token=$BOT_G...OKEN" \
  -H "Content-Type: application/json" \
  -d '{"body": "{原 body}\n\n---\n## 子任务\n- [ ] [#21 数据库迁移](issues/21)\n- [ ] [#22 模板分离](issues/22)\n- [ ] [#23 测试覆盖](issues/23)"}'
```

### 1.4 不 A2A 交办 coding

多任务模式下，Issue Agent **不 A2A 交给 coding**。子 Issue 创建后 webhook 自动触发 VOD 审视，进入正常流水线。

父 Issue 的流水线到此结束（服务端会自动设置 stage=done）。

## 二、子任务快速通道

当 prompt 上下文包含「子任务上下文」（Issue body 含「## 父任务」或 A2A payload `is_subtask=true`）时：

1. **跳过可开发性分析**（父任务已确认）
2. **跳过 Spec 生成**——直接从父 Issue body 中提取本子任务的目标和验收标准
3. **生成本子任务的精简 Spec**（仅目标 + 验收标准 + 实现建议）
4. **A2A 交办 coding**（正常流程）

### 子任务精简 Spec 模板

```
## 子任务 Spec

### 目标
{从父 Issue body 提取的子任务目标}

### 验收标准
{从父 Issue body 提取的验收标准，如无则根据目标自行补充}

### 实现建议
{基于项目知识基线给出 1-3 条建议}

### 父任务
[Issue #{parent_number}](https://gitcode.com/{owner}/{repo}/issues/{parent_number})
```

## 三、VOD 多任务识别规则

VOD Agent 在审视 Issue 时负责识别多任务模式：

### B7 多任务模式识别

检查 Issue body 或 prompt 上下文是否包含 `[multi-task-mode]` 标记或「多任务模式」上下文：
- **是**：正常澄清需求后 A2A 交办 Issue Agent，payload 中增加 `is_subtask_mode: true`
  - Issue Agent 将根据方案拆分为子任务
  - VOD 不需要额外处理，正常走澄清 → A2A 流程
- **否**：正常流程

### B8 子任务快速通道

检查 prompt 上下文是否包含「子任务上下文」（Issue body 含「## 父任务」+ Issue 链接，或 prompt 注入了 parent_issue）：
- **是子任务**：跳过需求澄清（父任务已澄清），直接 A2A 交办 Issue Agent
  - A2A payload 中增加 `is_subtask: true, parent_issue: "#20"`
  - Issue Agent 将跳过方案设计，直接基于父任务 Spec 生成子任务 Spec → A2A coding
- **否**：正常流程

## 硬约束

| 行为 | 态度 |
|------|------|
| 评估可拆分性、创建子 Issue | ✅ 多任务模式下执行 |
| 子任务跳过分析直接 A2A coding | ✅ 子任务快速通道执行 |
| 更新父 Issue 追加子任务列表 | ✅ 创建子 Issue 后执行 |
| 多任务模式下父任务直接 A2A coding | ❌ 禁止（子 Issue 自动触发流水线） |
| 子任务重复生成完整 Spec | ❌ 禁止（用精简 Spec） |
| 直接写代码、修改文件 | ❌ 禁止 |
| 回复 Token、密码等敏感信息 | ❌ 禁止 |

## 服务端协同

| 组件 | 行为 |
|------|------|
| `event_pipeline.run_pipeline` | 解析子任务 `parent_issue_id`；SRE 完成后登记父 Issue；父任务 Issue Agent done 后 `stage=done` |
| `prompt_builder` | 子任务 VOD/Issue prompt 注入父任务上下文 |
| `session_store` | `set/get_parent_issue`, `set/get_subtask_mode`, `is_subtask`, `get_subtask_issues` |
| `webhook` A2A 端点 | 接收 `is_subtask_mode`/`parent_issue` 并持久化 |
| `tasks.py` | `TaskCreateBody` 加 `is_subtask_mode`；创建 Issue body 追加 `[multi-task-mode]` 标记 |
