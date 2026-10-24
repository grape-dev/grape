# Grape 引入「查看运行中 Agent 实时日志」功能 — Multica 实现剖析与移植方案

> 分析对象：github.com/multica-ai/multica（浅克隆于 /home/grape/multica，commit 8cf2acb）
> 分析日期：2026-08-17
> 结论：multica 的日志机制 = **结构化任务消息（TaskMessage）**：worker 端把 agent 进程输出解析为
> 结构化事件流 → 批量上报 → 服务端持久化 + 实时广播 → 前端按 seq 去重合并渲染 timeline。

---

## 一、Multica 端到端链路（5 段）

### 1. 进程输出 → 结构化消息（worker 端，server/pkg/agent/）

每个引擎（opencode / claude / codex / hermes / qwen / kimi / dsh …）各自实现解析器，把
CLI 的 NDJSON/文本输出流统一解析为 `agent.Message` 结构体：

```go
type Message struct {
    Type      MessageType    // text | thinking | tool-use | tool-result | status | error | log
    Content   string         // text/error 内容
    Tool      string         // 工具名（tool-use / tool-result）
    CallID    string         // 工具调用 ID（用于 tool-use/tool-result 配对）
    Input     map[string]any // 工具入参 JSON（tool-use）
    Output    string         // 工具输出（tool-result）
    Status    string         // agent 状态串（status）
    SessionID string         // 后端会话 id（status，用于崩溃恢复 pin）
}
```

关键实现点：`newAgentStreamScanner` 统一 32MiB 行长上限 + 1MiB 初始缓冲（防超长 NDJSON
单行（Codex thread/resume 整线程序列化）把 bufio.Scanner 打爆）。

### 2. 消息 drain 循环（worker 端，server/internal/daemon/daemon.go ~L7551-7810）

agent 进程启动后，drain goroutine 从 `session.Messages` channel 消费并转换：

| agent.Message | 处理 |
|---|---|
| text / thinking | **累积到 pending buffer**（避免流式片段碎片化），flush 时合成一条 |
| tool-use | 立即成条（Tool + 脱敏后的 Input JSON） |
| tool-result | 立即成条（Output 截断 8192 字符） |
| error | 立即成条 |
| status | 不建消息，仅 pin session_id（崩溃恢复指针） |

**批量上报策略（核心）**：
- 500ms ticker 定期 flush + 任务结束前 final flush（`waitForDrain` 最多等 22s 保证尾部落库）
- 每批 `POST /api/daemon/tasks/{taskId}/messages`（5s 超时，失败仅 debug 日志，不阻塞执行）
- **双端脱敏**：worker 端 redact.InputMap 先脱敏（防 .env / 凭据内容出进程），服务端再脱敏一次

### 3. 服务端持久化 + 广播（server/internal/handler/daemon.go ReportTaskMessages）

```
POST /api/daemon/tasks/{taskId}/messages   {messages: [{seq, type, tool, content, input, output}]}
  → requireDaemonTaskAccess（校验 daemon 对该 task 的归属）
  → redact（服务端二次脱敏）
  → INSERT task_messages（task_id, seq, type, tool, content, input JSON, output）
  → publishTask(EventTaskMessage="task:message", workspaceID, taskID, payload)
      → events.Bus（进程内同步 pub/sub）
      → registerListeners 的 SubscribeAll → BroadcastToWorkspace（WebSocket /ws fanout）
```

广播设计：`Event` 带 `WorkspaceID` + 可选 `TaskID`/`ChatSessionID` scope hint，Bus → Hub 可
按 scope 路由（task 级房间协议已实现但默认走 workspace fanout，为横向扩展预留）。

### 4. 前端实时消费（packages/core/realtime/use-realtime-sync.ts）

- 单条 WebSocket 连接（/ws?workspace_id=…），连接即自动订阅 workspace + user scope
- `ws.on("task:message")` → `mergeTaskMessagesBySeq`（按 seq 去重合并）→ **直接写
  TanStack Query 缓存**（不触发 refetch）
- 终态事件（task:completed / task:failed）→ invalidate → 让 DB 权威回源
- 断线重连补数据：`GET /api/tasks/{taskId}/messages?since=<lastSeq>` 增量拉取

### 5. 前端渲染（packages/views/common/task-transcript/）

- `buildTimeline`：TaskMessagePayload[] → TimelineItem[]（按 seq 排序）
- `coalesceTimelineItems`：**相邻 text/thinking 片段合并**（补偿 500ms flush 造成的碎片）
- `AgentTranscriptDialog`：对话式渲染（thinking 可折叠、tool_use 展示 input、tool_result 展示 output）
- `transcript-follow`：实时跟随状态机——用户滚动离开底部超阈值自动停止跟随，
  回到边缘区重新跟随（区分用户滚动与系统 prepend 位移，纯函数可单测）

## 二、Multica 核心设计决策（值得抄的部分）

1. **结构化消息而非原始 stdout**：UI 能折叠 thinking、展开 tool 入参/输出，比 tail -f 强
2. **worker 主动 push 而非服务端拉取**：worker 在 agent 进程旁，天然拿到结构化事件；
   服务端只做存储 + fanout，无文件系统耦合
3. **批处理 + 单调 seq**：500ms 时间触发（不是条数触发，保证实时性）；seq 提供顺序、
   去重、增量拉取（since）三合一
4. **DB 唯一事实源 + 实时事件只写缓存**：实时事件进 query cache，终态事件 invalidate
   refetch，无双重事实源
5. **双端脱敏**：敏感信息不出 worker 进程（部署顺序不可控，每端各自安全）

## 三、Grape 现状盘点

### 已有（骨架已在，缺最后一公里）

| 环节 | 现状 | 位置 |
|---|---|---|
| worker 结构化事件流 | ✅ step_start/text/reasoning/tool_use/step_finish/done | src/runtime/agent.py `_execute_task` |
| worker 批量上报 | ✅ 每 50 条 `client.report_messages(tid, batch)` | agent.py L424-429 + client.py L123 |
| 服务端接收端点 | ✅ `POST /api/runtime/tasks/{task_id}/messages`（rta_ 鉴权） | src/api/routes/runtime.py L290 |
| 异步 ingestion | ✅ 异步队列（50 条/0.2s 批处理） | src/runtime/service.py `enqueue_async_report` |
| 落档 | ✅ `append_event_archive_log` → event.json 的 pipeline 段 logs（脱敏） | src/api/event_archive.py L174 |
| 静态查询 | ✅ `GET /api/events/detail/{seq}` 返回 archive.logs + agent_logs | src/api/routes/events.py L12 |
| SSE 通道先例 | ✅ chat.py `send_message_stream`（text/event-stream） | src/api/routes/chat.py L270 |

### 缺失（= 要引入的）

1. **结构化 task_messages 表**：目前只有 JSON 文件归档（event.json logs 数组），无
   (task_id, seq) 维度，无法增量拉取 / 按 task 查询 / 按工具检索
2. **实时推送**：进行中任务的消息只躺在归档 JSON 里，前端不刷新就看
3. **前端实时渲染**：无 timeline/transcript 组件；详情页 archive 是打开时的静态快照
4. **增量 catch-up**：无 `?since=<seq>` 语义（断线/新开页补数据）
5. **时间触发批处理**：worker 端 50 条才 flush——text 流式输出片段短时可能很久凑不满，
   实时性差（multica 用 500ms ticker 双触发）
6. **双端脱敏**：grape 只在服务端 _redact（事件离开 worker 进程时明文）

## 四、引入方案（对齐 grape 现有架构，最小侵入）

### 阶段 0：结构化表 + 双写（服务端）

**DDL（src/api/db_setup.py，两后端自动一致——MySQL/SQLite 同步）**：

```sql
CREATE TABLE IF NOT EXISTS task_messages (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  task_id     TEXT NOT NULL,          -- agent_tasks.id
  seq         INTEGER NOT NULL,       -- worker 侧单调序号
  type        TEXT NOT NULL,          -- step_start|text|reasoning|tool_use|step_finish|error|done
  tool        TEXT DEFAULT '',        -- tool_use 工具名
  content     TEXT DEFAULT '',        -- text/reasoning 内容
  input_json  TEXT DEFAULT '',        -- tool_use 入参 JSON
  output      TEXT DEFAULT '',        -- tool_result 输出（截断 8192）
  created_at  TEXT DEFAULT (datetime('now')),
  UNIQUE(task_id, seq)
);
```

`service.report_messages` 改双写：保留现有 `append_event_archive_log`（归档不动），
新增 `INSERT OR IGNORE INTO task_messages`（幂等，防重放）。

### 阶段 1：SSE 实时推送（服务端，复用 chat.py 同构模式）

新增端点 `GET /api/tasks/{task_id}/messages/stream?since=<seq>`（登录鉴权）：

- 服务端维护进程内订阅表 `task_id → set[asyncio.Queue]`（module 级 dict + asyncio.Lock）
- `report_messages` 写库后向该 task 的订阅队列 fanout（`put_nowait`，队列满丢弃保活）
- SSE 响应循环：① 先发 `since` 之后存量（SELECT）→ ② 阻塞读订阅队列 → yield `data: {...}`
- 客户端断开（`request.is_disconnected()`）→ 取消协程 → 清理订阅表
- 与 chat.py `send_message_stream` 完全同构，无新依赖

### 阶段 2：前端实时日志面板

- `events.js` 详情面板（Event Info 下方）新增「实时日志」区：
  - 打开详情时 `new EventSource('/api/tasks/{tid}/messages/stream?since=0')`
  - 增量事件按 seq 合并排序（对齐 multica `mergeTaskMessagesBySeq`）
  - 渲染：text/step 逐行、tool_use 显示 🔧 工具名 + 可展开 input、error 红色
  - **自动跟随**：新消息滚动到底；用户向上滚动 >120px 停止跟随，回底部恢复
- 事件终态（completed/failed）时关闭 EventSource（由 detail 接口轮询兜底）
- **必须 bump index.html 的 events.js?v=N**

### 阶段 3（可选增强）

- worker 端 batch 触发改「50 条 **或** 500ms」双触发（agent.py `_execute_task` 加
  `asyncio.wait_for(queue.get(), timeout=0.5)` 或 ticker 同款模式）——提高 text 流实时性
- worker 端脱敏前置（server 端已脱敏，双端各自安全）
- 横扩预留：若未来多实例部署，SSE fanout 换 Redis Pub/Sub（对应 redis-cache-analysis.md
  的 P1「SSE Pub/Sub 广播」项，此功能落地后该项可直接兑现）

## 五、测试与验证

- `tests/test_task_messages.py`（新增）：双后端（sqlite 隔离库 + MySQL 语法走 mysql_compat）
  断言 INSERT 幂等 / since 增量 / SSE 流式 yield / 断开清理
- 端到端：真实 opencode 任务跑起来，前端详情页实时看到 step/text/tool_use 滚动
- 回归：全量 pytest（注意 conftest 强制 sqlite，task_messages 走 db_setup 迁移自动双端一致）

## 六、Grape 特有坑位清单

1. **schema 双端一致**：只改 db_setup.py（DDL + `_ensure_column`），禁止手改 MySQL；
   改完对比 `PRAGMA table_info` / `SHOW COLUMNS`
2. **db_lock**：`report_messages` 在 ingestion 循环内已持 `with db_lock`，INSERT 放同一锁内；
   SSE fanout 在锁外（避免长锁）
3. **前端缓存**：events.js 改动必须 bump index.html `?v=N`
4. **SSE 连接泄漏**：必须处理 `request.is_disconnected()` + finally 清理订阅表
   （grape 已有 chat.py SSE 先例可抄）
5. **消息量**：`UNIQUE(task_id, seq)` + `INSERT OR IGNORE` 防 worker 重放重复
6. **脱敏**：沿用 `_redact(data, tokens)`（event_archive 同款），tool_use input 必须脱敏
   （可能含 .env 内容）

## 七、工作量评估

| 阶段 | 改动面 | 估时 |
|---|---|---|
| 0 结构化表 | db_setup.py + service.py report_messages | 0.5d |
| 1 SSE 推送 | runtime.py 新端点 + 订阅表 + 测试 | 0.5-1d |
| 2 前端面板 | events.js + index.html bump + 样式 | 0.5-1d |
| 3 增强（可选） | agent.py 双触发 + worker 脱敏 | 0.5d |

合计约 2-3 天，核心价值集中在阶段 0+1（打通实时通道），阶段 2 是纯前端体验。
