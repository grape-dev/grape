# 事件处理流程图与数据流

## 目录

1. [端到端流程](#端到端流程)
2. [数据流详解](#数据流详解)
3. [并发模型](#并发模型)
4. [Webhook 签名验证](#webhook签名验证)

---

## 端到端流程

```
┌──────────────┐     ┌──────────────┐     ┌──────────────┐
│  GitCode     │     │  GitHub      │     │  Jenkins     │
│  Webhook     │     │  Webhook     │     │  Webhook     │
└──────┬───────┘     └──────┬───────┘     └──────┬───────┘
       │                    │                    │
       ▼                    ▼                    ▼
┌──────────────────────────────────────────────────────────┐
│                  HTTP Server (:8080)                      │
│  /webhook/gitcode  /webhook/github  /webhook/jenkins     │
└──────────────────────────┬───────────────────────────────┘
                           │
                    [1] 签名验证
                           │
                    [2] Source.Normalize()
                           │
                           ▼
                  ┌─────────────────┐
                  │ NormalizedEvent │
                  │ source          │
                  │ type            │
                  │ repository      │
                  │ branch          │
                  │ commit          │
                  │ actor           │
                  │ payload         │
                  │ raw_payload     │
                  └────────┬────────┘
                           │
                    [3] Router.Match()
                           │
                    ┌──────┴──────┐
                    │ matched?    │
                    ├─否→ 忽略+日志│
                    └─是→         │
                           │
                    [4] Session.GetOrCreate(repo_key)
                           │
                    ┌──────┴──────┐
                    │ 已存在?     │
                    ├─否→ 新建    │
                    └─是→ 复用    │
                           │
                    [5] PromptBuilder.Build()
                    │  = Session.ContextSummary()
                    │  + 当前事件模板渲染
                           │
                    [6] Agent.Execute(CLI)
                    │  传入: prompt, workspace,
                    │        DEVOPS_SKILLS_DIR,
                    │        DEVOPS_SESSION_ID
                           │
                    ┌──────┴──────┐
                    │ 成功?       │
                    ├─是→ RecordSuccess()
                    │      Session→idle
                    └─否→ RecordFailure()
                           │
                    [7] FailureHandler
                    │  构造失败Prompt
                    │  → Agent.Execute()
                           │
                    ┌──────┴──────┐
                    │ 自愈成功?   │
                    ├─是→ RecordSuccess()  │
                    └─否→ Session→failed
                           │
                           ▼
                      人工介入
```

---

## 数据流详解

### NormalizedEvent 结构

| 字段 | 类型 | 来源 | 说明 |
|------|------|------|------|
| id | string | Bot 生成 | 全局唯一事件ID |
| source | SourceType | Webhook 路径 | github/gitlab/gitcode/jenkins/spanner |
| type | EventType | Source.Normalize() | push/pull_request_opened/build_failure/... |
| timestamp | time | Bot 接收时间 | 事件到达Bot的时间 |
| repository | string | 从payload提取 | owner/repo 格式 |
| branch | string | 从payload提取 | 事件关联分支 |
| commit | string | 从payload提取 | 关联commit SHA |
| actor | string | 从payload提取 | 触发者 |
| payload | map | 精简关键字段 | 结构化关键信息 |
| raw_payload | json | 原始JSON | 完整原始payload |

### Session 结构

| 字段 | 类型 | 说明 |
|------|------|------|
| id | string | Session唯一ID |
| repo_key | string | source:repository（Session复用键） |
| source | SourceType | 事件源 |
| repository | string | 仓库标识 |
| agent_name | string | 负责的Agent |
| status | Status | idle/active/failed |
| workspace | string | 工作目录 |
| event_history | []EventRecord | 所有已处理事件记录 |
| current_event | *EventRecord | 当前正在处理的事件 |
| created_at | time | Session创建时间 |
| last_active_at | time | 最后活跃时间 |
| retry_count | int | 失败重试次数 |
| fail_reason | string | 最近一次失败原因 |

### EventRecord 结构

| 字段 | 类型 | 说明 |
|------|------|------|
| id | string | 事件记录ID |
| event | NormalizedEvent | 原始事件 |
| prompt | string | 发给Agent的Prompt |
| stdout | string | Agent标准输出 |
| stderr | string | Agent标准错误 |
| exit_code | int | Agent退出码 |
| success | bool | 是否成功 |
| duration | duration | 执行耗时 |
| processed_at | time | 处理完成时间 |

---

## 并发模型

```
                    Webhook 事件到达
                         │
                    go processEvent()
                         │
                  Acquire Semaphore
                  (max_concurrent=10)
                         │
                    ┌────┴────┐
                    │ 有槽位? │
                    ├─否→ 报错│
                    └─是→     │
                         │
                    处理事件
                         │
                  Release Semaphore
```

- 最大并发 Session 数由 `config.yaml` 中 `session.max_concurrent` 控制
- 超出并发限制的事件会被丢弃（可改进为排队）
- 每个 `processEvent` 在独立 goroutine 中运行

---

## Webhook 签名验证

每个事件源在 `config.yaml` 中配置 secret（推荐通过环境变量注入）：

| 事件源 | 验证方式 | Header |
|--------|---------|--------|
| GitHub | HMAC-SHA256 | `X-Hub-Signature-256` |
| GitLab | Token 比对 | `X-GitLab-Token` |
| GitCode | Token 或 HMAC-SHA256 | `X-GitCode-Token` / `X-GitCode-Signature` |
| Jenkins | Token 比对 | Query param `token` |
| Spanner | Token 比对 | `X-Spanner-Token` |

**安全铁律**：
- 生产环境必须配置 secret
- 验证签名后再处理事件，防止伪造请求
- Webhook URL 必须使用 HTTPS
