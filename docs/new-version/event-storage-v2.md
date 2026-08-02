# Event 事件存储方案 v2

> 日期：2026-08-02
> 状态：待实施

## 一、背景与问题

当前事件存储将所有进入 pipeline 的事件（路由成功即 `append_event`）都写入 `events` 表，并存储完整 `payload`（原始 webhook body）和 `agent_logs`（执行全过程）。

**问题**：
1. 大量事件无实质产出（如 bot 自评论被回发、Agent 被调用但未产出）也被记录，噪音大
2. `payload` 含原始评论、token 等敏感信息，且体积大
3. `agent_logs` 记录执行过程而非结论，前端展示过程而非结果
4. 路由失败（无 Agent 匹配）事件静默丢弃，无审计
5. 历史数据含大量 issue_comment bot 自评论噪音

## 二、核心语义

| 事件来源 | 存储条件 | 存储内容 |
|---------|---------|---------|
| Webhook | 路由到 Agent **且** Agent 实际产出（按 agent_type 产出信号判定） | **答复结论**（纯文本） |
| A2A | 传给 Agent **且** Agent 最终答复 | 处理结果 |
| VOD IM | 建议被采纳且创建了 Issue | Issue 结论 |
| Webhook 路由失败 | 无 Agent 匹配 | `event_skipped` 审计记录 |
| Agent 无产出 | 被调用但未产出 | `event_skipped` 审计记录 |

**原则**：event 主要记录**答复的结论**，而非过程。

## 三、产出信号定义（按 agent_type）

从 stream_agent 的 `tool_use` 事件（bash 命令）和 `done` 的 `final_text` 检测：

| agent_type | 产出信号（有明确答复） | 结论提取 |
|-----------|------------------------|---------|
| coding | final_text 含 `merge_requests/\d+` 或 `/pulls/\d+`；或 tool_use 含 `POST .../pulls` | `创建了 PR #{n}: {title}` |
| issue | tool_use 含 `POST /issues/\d+/comments`、`POST /issues`、`PATCH labels` 等写操作 | `评论了 Issue #{n}` / `打标签` |
| qa / pr | final_text 含审查结论（`审查通过`/`需修改`）；或 tool_use 含 `POST .../comments` | 审查结论摘要 |
| sre | tool_use 含部署/构建写操作；final_text 含结果 | 部署/构建结果 |
| vod | final_text 含 `/issues/\d+`（已实现） | 创建了 Issue #{n} |

**判定函数**：`evaluate_agent_output(agent_type, tool_uses, final_text) -> (has_output: bool, result: str)`

## 四、表结构

### 4.1 events 表（瘦身 + 新增 result）

```sql
-- 保留字段
seq INTEGER PRIMARY KEY AUTOINCREMENT,   -- 事件号
repo_key TEXT NOT NULL,                  -- owner/repo
issue_id TEXT DEFAULT '',                -- Issue/PR 编号
event_type TEXT NOT NULL,                -- 事件类型
source TEXT DEFAULT 'gitcode',           -- gitcode/a2a/vod_im
agent_type TEXT DEFAULT '',              -- 处理的 Agent
title TEXT DEFAULT '',                   -- 标题
actor TEXT DEFAULT '',                   -- 执行者
summary TEXT DEFAULT '',                 -- 摘要
status TEXT DEFAULT 'completed',         -- completed/failed
backend TEXT DEFAULT '',                 -- 执行引擎
skill TEXT DEFAULT '',                   -- 技能
exit_code INTEGER DEFAULT NULL,
duration REAL DEFAULT NULL,
result TEXT DEFAULT '',                  -- ★ 答复结论（纯文本）
created_at TEXT DEFAULT (datetime('now')),
started_at TEXT DEFAULT '',
completed_at TEXT DEFAULT ''

-- 移除（不再写入，历史列可保留或迁移时清理）
payload TEXT DEFAULT '{}',               -- 原始 body（含敏感信息）
agent_logs TEXT DEFAULT '[]'             -- 执行过程
```

### 4.2 event_skipped（独立审计表）

```sql
CREATE TABLE IF NOT EXISTS event_skipped (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    repo_key TEXT DEFAULT '',
    event_type TEXT DEFAULT '',
    reason TEXT DEFAULT '',              -- no_route / no_output / bot_self_comment
    detail TEXT DEFAULT '',
    created_at TEXT DEFAULT (datetime('now'))
)
```

## 五、写入时序改造

### 5.1 Webhook 主链路（event_pipeline.run_pipeline）

```
webhook → 归一化 → 自循环检测 → 路由
  ├─ bot 自评论 → 不处理（不存 skipped，正常业务跳转）
  ├─ 路由失败 → INSERT event_skipped(reason='no_route')
  ├─ 路由成功 → 不立即 append_event
  │     ├─ 启动后台 _execute_agent
  │     │   ├─ 有产出 → append_event(status='completed', result=结论)
  │     │   └─ 无产出 → INSERT event_skipped(reason='no_output')
  │     └─ return（seq 可能为 0，等异步执行后才有事件）
```

### 5.2 A2A（webhook.py）

```
a2a 请求 → 传给目标 Agent → _execute_agent
  ├─ 有产出 → append_event(a2a_event, result=结论)
  └─ 无产出 → event_skipped(reason='no_output')
```

### 5.3 VOD（vod_handler_async.py，维持现有）

```
final_text 含 /issues/\d+ → append_event(vod_signal, result='创建了 Issue #{n}')
否则 → 不存储
```

## 六、前端适配

- `events.js` 详情页：移除 agent_logs 展示，改为展示 `result`（答复结论）
- 事件列表：新增 `skipped` 视图（读 event_skipped）

## 七、历史数据清理

- 删除 `events` 表中 bot 自评论 / 无产出 / 卡 running 的历史噪音记录
- 保留有实际产出（completed 且含 result 或有效结论）的记录
- 清理后 `seq` 重新从 1 开始（或保留自增不重置，待定）

## 八、实施清单

1. db_setup.py：events 加 `result` 列 + 新建 event_skipped 表 + 迁移清理
2. event_store.py：`evaluate_agent_output` + `append_skipped_event` + `append_event` 支持 result
3. event_pipeline.py：路由失败存 skipped、有产出才 append、写入时序改造
4. webhook.py：A2A 有产出才存 event
5. vod_handler_async.py：result 纯文本
6. 前端 events.js：展示 result
7. 清理历史数据 + 部署验证
