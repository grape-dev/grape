# 跨流水线续接产物标准路径（Runbook 章节）

> Issue #729：duplicate_a2a / duplicate_handoff 拦截响应携带原任务产物引用后的
> 续接标准路径。适用于 issue / coding 等技能执行走查表的「A2A 交办被拦截」环节。

## 1. 背景

重复 A2A 交办（同 Issue 同 Agent 已有活跃事件）与跨流水线移交（同 Issue 在目标
场景已有活跃事件）会被服务端去重拦截。此前拦截响应只带 `skipped` / `existing_seq`，
下游无法定位原任务产物，必须重新定位上下文（用户反馈 id 366）。

自 Issue #729 起，拦截响应新增 `original_event` 字段，携带原事件的任务产物引用：

```json
{
  "status": "ok", "seq": 0, "agent": "coding",
  "skipped": "duplicate_a2a", "existing_seq": 41892,
  "original_event": {
    "seq": 41892, "agent": "coding", "status": "running",
    "issue_id": "#729", "created_at": "2026-11-02T12:32:14+00:00",
    "task_ref": {
      "task_id": "t_a92396f72233",
      "task_status": "running",
      "query_path": "/api/runtime/tasks/t_a92396f72233/status"
    },
    "result_ref": { "status": "pending" }
  }
}
```

字段语义（与流水线 `segments[].output.task_ref / result_ref` 同一套，Issue #539）：

| 字段 | 说明 |
|------|------|
| `original_event.seq` | 被视为重复的原事件 seq（= `existing_seq`） |
| `original_event.task_ref.task_id` | 原事件关联任务 id（最后创建的一条） |
| `original_event.task_ref.query_path` | 授权轮询路径（任务状态 + 完整结论） |
| `original_event.result_ref.status` | `pending` / `available` / `unavailable` |
| `original_event.result_ref.summary` | `available` 时的脱敏结论摘要（≤240 字符） |

`result_ref` 状态边界：
- `pending`：原任务在途（queued/dispatched/running）——**仅有 task_ref 可用，
  凭 `query_path` 轮询等待结果，不要解析 summary（不存在）**；
- `available`：原任务有可读结论（附脱敏摘要）；
- `unavailable`：无关联任务 / 结果不可读（显式标注，勿重试推断）。

## 2. 标准续接路径（所有场景通用）

1. A2A 交办返回 `skipped: "duplicate_a2a"` / `skipped: "duplicate_handoff"` 时，
   **不重发交办、不新建任务**；
2. 读取 `original_event.task_ref`：
   - `result_ref.status == "available"` → 直接采用 `summary` 作为上轮结论，
     需要完整结论时走 `query_path`（授权调用方）；
   - `result_ref.status == "pending"` → 以 `query_path` 轮询（建议 30–60s 间隔），
     任务终态后再取结论；
   - `result_ref.status == "unavailable"` → 原事件无任务关联，按普通拦截处理
     （评论说明、等待原事件收敛）；
3. 在 Issue/PR 评论中注明「已由 duplicate_a2a 拦截，原事件 seq=…，凭
   task_ref 续接」，避免下游重复定位。

## 3. 场景一：重复交办（同 Issue 同 Agent）

流水线中 QA 与 Issue Agent 同时向 sre 发 A2A，第二个请求被拦截：

```
POST /api/a2a  {"agent": "sre", "payload": {"issue_number": 729, ...}}
→ 200 {"skipped": "duplicate_a2a", "existing_seq": 43107,
       "original_event": {"seq": 43107, "agent": "sre", "status": "running",
         "task_ref": {"task_id": "t_5c81...", "task_status": "running",
                      "query_path": "/api/runtime/tasks/t_5c81.../status"},
         "result_ref": {"status": "pending"}}}
```

续接动作（调用方 Agent）：
- 原任务 running → 不再创建部署任务，凭 `query_path` 轮询至终态；
- 评论告知用户「sre 已在处理（seq=43107），本轮 A2A 被去重拦截」。

## 4. 场景二：跨流水线移交（duplicate_handoff）

公共流水线向目标场景再次移交同需求，目标场景已有活跃事件被拦截：

```
POST /api/a2a  {"agent": "vod", "payload": {
    "issue_number": 729, "target_scene_id": "<目标场景>", "target_node": "vod", ...}}
→ 200 {"skipped": "duplicate_handoff",
       "original_event": {"seq": 41865, "agent": "issue", "status": "running",
         "issue_id": "#729",
         "task_ref": {"task_id": "t_a6aa8d2cc19f", "task_status": "completed",
                      "query_path": "/api/runtime/tasks/t_a6aa8d2cc19f/status"},
         "result_ref": {"status": "available",
                        "summary": "Issue #729（P1/bug，spec-ready）跨流水线移交确认完成…"}}}
```

续接动作（目标流水线下游 Agent）：
- `result_ref.status == "available"` → 直接以 `summary` 为上轮结论续接
  （上例：Spec 已确认、coding 节点已校验，从 coding 阶段继续）；
- 需要完整结论（含 actions/artifacts 等）时走 `query_path`；
- 事件链以 `issue_id` 为锚点回挂原 Issue，续接评论引用 `original_event.seq`。

## 5. 注意事项

- `original_event` 为新增可选字段，旧调用方可忽略（向后兼容）；
- 引用仅指向「产物引用」：产物内容获取 API（#650）落地前，`query_path`
  是唯一授权读取通道；
- 本路径与 #665（重复事件通知已运行 coding）互补：#665 解决「要不要通知」，
  本章节解决「凭引用去哪续接」。
