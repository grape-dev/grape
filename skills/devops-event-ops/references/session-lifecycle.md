# Session 生命周期与状态机

## 目录

1. [状态机](#状态机)
2. [生命周期详解](#生命周期详解)
3. [Session 复用规则](#session-复用规则)
4. [事件历史管理](#事件历史管理)
5. [失败自愈流程](#失败自愈流程)

---

## 状态机

```
                    事件到达
                       │
              GetOrCreate(repo_key)
                       │
              ┌────────┴────────┐
              │ Session 已存在?  │
              ├─否→ status=idle  │ (新建)
              └─是→ 保持当前状态 │ (复用)
                       │
              ┌────────┴────────┐
              │ status=idle?     │
              ├─否→ 事件排队等待 │
              └─是→              │
                       │
                 SetActive()
              status → active
              current_event = evt
                       │
              Agent.Execute()
                       │
              ┌────────┴────────┐
              │ 成功?            │
              ├─是→ RecordSuccess()│
              │     status → idle │
              │     current_event = nil
              │     event_history += record
              └─否→ RecordFailure()│
                    status → failed
                    event_history += record
                       │
              FailureHandler.Handle()
                       │
              ┌────────┴────────┐
              │ 自愈成功?        │
              ├─是→ status → idle │
              └─否→ status → failed
                    retry_count++
                       │
              ┌────────┴────────┐
              │ retry_count < max?│
              ├─是→ 可再次自愈    │
              └─否→ 人工介入      │
```

---

## 生命周期详解

### 新建 Session

触发条件：首次收到某 repo 的事件，`repo_key` 在 Store 中不存在。

```go
Session{
    ID:           "sess-{repo_key}-{timestamp}",
    RepoKey:      "gitcode:owner/repo",
    Source:       "gitcode",
    Repository:   "owner/repo",
    AgentName:    "opencode",       // 来自路由规则
    Status:       "idle",
    Workspace:    "./data/workspaces/owner/repo",
    EventHistory: [],
    CreatedAt:    time.Now(),
    LastActiveAt: time.Now(),
}
```

### 复用 Session

触发条件：同一 `repo_key` 的后续事件到达。

Agent 收到的 Prompt 包含：
1. **Session 摘要**：repo、创建时间、已处理事件数
2. **最近 10 条事件历史**：类型、时间、成功/失败
3. **当前事件**：完整的事件指令

这使得 Agent 能够：
- 知道之前处理过什么
- 避免重复分析
- 保持对项目的连续理解

### Session 销毁

当前设计：Session 不自动销毁，持续存在。未来可增加：
- 空闲超时自动归档（如 7 天无事件 → 归档到冷存储）
- 手动关闭 Session 的 API
- Session 容量限制（EventHistory 超过 N 条时裁剪旧记录）

---

## Session 复用规则

### repo_key 生成规则

```
repo_key = "{source}:{repository}"
```

示例：
- `gitcode:Jason7602/devops-bot-test`
- `github:octocat/hello-world`
- `jenkins:my-ci-project`

### 复用判断

| 条件 | 结果 |
|------|------|
| 同一 source + 同一 repository | 复用同一 Session |
| 同一 repository 但不同 source | 不同 Session（隔离事件源） |
| 不同 repository | 不同 Session（隔离项目） |

### 路由规则变更

如果 `rules.yaml` 中某 repo 匹配的 Agent 发生变更：
- 已有 Session 的 `agent_name` 不会自动更新
- 新事件会使用路由规则中的新 Agent
- 可通过 API 手动更新 Session 的 Agent

---

## 事件历史管理

### EventRecord 追加

每次事件处理完成（无论成功失败），都追加到 `EventHistory`：

```go
EventRecord{
    ID:          "evt-{timestamp}",
    Event:       normalizedEvent,
    Prompt:      renderedPrompt,
    Stdout:      agentStdout,
    Stderr:      agentStderr,
    ExitCode:    agentExitCode,
    Success:     exitCode == 0,
    Duration:    executionDuration,
    ProcessedAt: time.Now(),
}
```

### 历史裁剪策略

当前：不裁剪，全部保留。

未来策略（可选）：
- 保留最近 N 条（如 100 条）
- 按时间窗口裁剪（如最近 30 天）
- 成功记录精简（只保留摘要），失败记录完整保留

### ContextSummary 生成

Agent Prompt 中的 Session 上下文摘要：

```
Session sess-xxx (repo: owner/repo, source: gitcode)
Created: 2024-01-15 10:00:00, Last active: 2024-01-15 15:30:00
Events processed: 12

=== Recent Event History ===
- success push/gitcode (owner/repo) at 2024-01-15 15:30:00
- success push/gitcode (owner/repo) at 2024-01-15 14:20:00
- failed build_failure/jenkins (owner/repo) at 2024-01-15 13:00:00
- success push/gitcode (owner/repo) at 2024-01-15 11:00:00
...
```

---

## 失败自愈流程

### 第一次失败

```
Agent 执行返回非零退出码
  → RecordFailure()
  → Session.status = failed
  → Session.retry_count = 1
  → FailureHandler.Handle()
```

### FailureHandler 构造的 Prompt

```
你之前处理一个DevOps事件时失败了，请分析失败原因并决定下一步行动。

=== 事件信息 ===
事件源: gitcode
事件类型: push
仓库: owner/repo
分支: main
触发者: developer

=== 失败信息 ===
失败原因: agent returned non-zero exit code
错误输出:
error: cannot find module src/main.go

=== 原始事件数据 ===
{...}

请分析失败原因，如果可以修复请给出修复方案，如果无法自动修复请说明需要人工介入的原因。
```

### Agent 可能的自愈决策

| 决策 | 条件 | Agent 行为 |
|------|------|-----------|
| 换策略重试 | 失败原因可理解且有替代方案 | 输出新的处理方案 |
| 降级处理 | 完整处理不可行但部分可行 | 输出降级结果 |
| 请求人工 | 无法自动修复 | 输出人工介入说明 |
| 确认忽略 | 失误是环境/临时问题 | 输出忽略原因 |

### 重试上限

- 默认 `max_retries = 1`（自愈一次）
- 超过上限后 Session 保持 `failed` 状态，等待人工介入
- 可通过 API 手动重置 Session 状态触发重新处理
