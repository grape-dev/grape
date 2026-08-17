# 多 Runtime 实例设计（Multi-Runtime Instance Design）

> 版本：v0.3（设计稿→已实施核心：D1 注册键/DDL、D2 路由 S2、D4 清理/吊销；D3 UI 绑定与 fallback 待做）
> 日期：2026-08-09（v0.3 更新同日：D1/D2/D4 已落地并配套 R1-R7 测试）
> 背景：远端 RuntimeAgent（123.249.12.67）接入后，`agent_runtimes` 单行被远端 register 覆盖、本机 runtime 从设备视图消失；为支持"不同 Agent 使用不同 Runtime、多 worker 并存、降低单 runtime 负担"而设计本方案。

---

## 1. 背景与问题

### 1.1 现状快照（2026-08-09 实测）

- `agent_runtimes` 以 **`name+scope('global')+owner_id('')`** 为唯一键（`db_setup.py:146`），即"每个引擎名全库一行"：`id=1 opencode`、`id=2 codearts`。
- 远端 worker 注册时 `POST /api/runtime/register` **按 name upsert 覆盖该行**（`runtime.py:101-118`）：`cli_path`、`version`、`credential_id`、`status`、`last_seen_at` 全部改为 worker 的值。本机 opencode（`/usr/local/bin/opencode` 1.17.7）的身份被吸收，行内只剩 worker 的 `/usr/bin/opencode` 1.18.15。
- 设备视图（`settings.py:194-224`）：本地引擎（`credential_id=0`）只在 **`devices` 为空**时作为"本机 Runtime"渲染（`if orphan_ids and not devices:`，`:215`）；有 active 凭据后本地设备不再显示。
- `queue_remote` 下，服务器本机**没有运行边端进程**（`grape.service` 只跑 API），本机 runtime 不消费任务。
- **本机 opencode 已停用（2026-08-09 测试时改名 `opencode.bak`）**：`/usr/local/bin/opencode` 已不在 PATH；`agent_runtimes` id=1 的 `cli_path` 现指向已改名的路径（dangling，此前被本机 refresh 覆盖回 `/usr/local/bin/opencode`）。
- **队列消费现状（强化）**：所有入口（pipeline / web chat / 飞书 IM）均已入队，且**只有远端 worker（123.249.12.67）消费**；服务器本机仍无边端进程。

### 1.2 问题清单

| # | 问题 | 后果 |
|---|------|------|
| P1 | register 按 name 全局唯一键 upsert | 多 worker 注册同名引擎互相覆盖身份；本机/远端不能并存 |
| P2 | `agent_runtimes` 每引擎只有一行 | 无法把不同 Agent 绑定到不同执行实例 |
| P3 | 设备视图仅零凭据时显示本机 | 接入后本机 runtime 从 UI 消失（信息隐藏） |
| P4 | 路由解析按 name 取单行 | 多实例后 `get_runtime_id_by_name` / `ensure_default_runtime` 失效语义 |

### 1.3 目标

1. **多实例共存**：每台 worker（凭据）对每个引擎名独占一行，同时在线，互不覆盖。
2. **Agent 分流**：不同 `agent_type`（vod/issue/coding/qa/sre）或 repo 可绑定不同 runtime 实例，任务路由到指定 worker，降低单 runtime 负担。
3. **本机可注册**：服务器本机也可作为一台边端注册，与远端共存。
4. **兼容现有协议**：`/api/runtime/*` 端点语义基本不变，改动集中在服务端路由解析与注册键。

### 1.4 非目标（本期不做）

- 不引入 MQ/Redis，任务队列仍落 SQLite。
- 不改 `src/runtime/agent.py` / `client.py` 的边端协议主体（register body 不变）。
- 不做运行时热迁移/弹性扩缩（离线实例的任务保持 queued 的语义由绑定策略决定，见 §5）。

### 1.5 落地进展（2026-08-09 配套基础，与本设计上下游相关）

> 多实例键（§2/§3）**尚未实施**；以下为让"远端执行真正生效"而落地的配套基础，均已在生产（grape 203.0.113.2 + worker 123.249.12.67）部署验证。

**已实施（2026-08-09）：**

| 改动 | 位置 | 说明 |
|------|------|------|
| 飞书 chat bot 非 direct 入队 | `src/im/handler_async.py` | `mode != direct` → `enqueue_task` + `wait_task_terminal`，回复取任务 `result_text` |
| 飞书 VOD bot 非 direct 入队 | `src/im/vod_handler_async.py` | 同上 |
| Web chat SSE stream 非 direct 入队 | `src/api/routes/chat.py` `send_message_stream` | 入队 + 轮询后以 SSE done 返回终态结果 |
| 轮询助手 | `src/runtime/service.py:553` `wait_task_terminal` | 异步轮询 `get_task` 至终态，超时抛 `TimeoutError` |
| 服务端逐步日志 | `src/runtime/service.py:282` `report_messages` | 消费 worker 消息批量时打印 `[task] step_*` |
| worker 逐步日志 | `src/runtime/agent.py` `_execute_task` | 执行中打印 `▶ Step N` / `💬 text` 等 |
| stdout "Session not found" 重跑修复 | `src/agent/executor.py` | 远端 opencode 会话失效错误经 stdout ANSI 原始行输出，计入 `error_events` 触发无 session 重跑 |

**仍本地直连（未尊重 execution_mode，待对齐，见 §8）：**
- A2A 交办：`src/api/routes/webhook.py:197` → `_execute_agent`
- 任务中心：`src/api/routes/tasks.py:202`
- 项目接入探测：`src/api/onboarding.py:68`

**测试看护**（2026-08-09 新增）：`test_agent_executor.py`（stdout 重跑）、`test_runtime.py`（`TestWaitTaskTerminal`）、`test_chat_handler.py` / `test_vod_handler_queue.py`（IM queue/direct 分支）、`test_web_chat_tooluse.py`（web stream queue 模式）。

---

## 2. 核心设计决策

### 2.1 实例唯一键：`(credential_id, name)`

**结论：不用 `IP + seq` 入键，用 `credential_id` 作为实例标识。**

| 候选字段 | 是否入键 | 理由 |
|----------|----------|------|
| `name` | ✅ | 引擎名（opencode/codearts/…），任务类型维度 |
| `scope` | ✅ | 保留租户语义（现恒为 global） |
| `owner_id` | ✅ | 保留（现恒为 ''） |
| `credential_id` | ✅ | **天然唯一的 worker 序列号**：每 worker 一个 `rta_` token → `runtime_credentials.id`（AUTOINCREMENT），稳定、可吊销、管理端可读、设备视图已按它分组 |
| 执行机 IP | ❌ 只作记录 | ① 经 nginx 反代拿真实来源 IP 需信任 `X-Forwarded-For`；② NAT/同网关多 worker 同公网 IP 会撞键；③ 动态 IP 导致行孤儿累积；④ 同机多边端进程 IP 相同。IP 写入 `runtime_credentials.last_ip` 仅展示 |
| worker 序列号 | ❌ 已由 credential_id 承担 | 重复设计 |

`UNIQUE(credential_id, name, scope, owner_id)`（兼容现有索引，`credential_id=0` 的历史行特殊处理，见 §7 迁移）。

### 2.2 语义变化

- `agent_runtimes` 从"引擎类型"变成"**引擎实例**"：`name` 仍表达引擎类型，行身份 = 具体 worker 上的该引擎。
- `runtime_id` 从"引擎类型 id"变成"**实例 id**"：`claim(runtime_id)` 天然按实例隔离队列（`service.py:209`）。
- 绑定（`devops_agent_session.runtime_id`、`chat_agent_session.runtime_id`、`agent_type.default_runtime_id`）从"绑引擎类型"变成"绑实例"或"绑引擎类型 + 选择策略"（§4）。

---

## 3. 注册与心跳改造

### 3.1 register（`runtime.py:101-118`）

现状：`SELECT ... WHERE lower(name)=? AND scope='global' AND owner_id=''` → 命中则 UPDATE 该行。

改为（键 = `credential_id+name+scope+owner_id`）：

```
SELECT id FROM agent_runtimes
WHERE credential_id=? AND lower(name)=? AND scope=? AND owner_id=? LIMIT 1
```

- 命中 → 更新 version/healthy/status/cli_path/last_seen_at（幂等，同凭据重复注册安全）。
- 未命中 → INSERT 新实例行（`credential_id` 写入）。
- 返回值仍为 `{"runtimes":[{"id","name"}], "server_url"}`（协议不变，边端无需改）。

**效果**：每台 worker 对每个已装引擎各自成行；多 worker 的 opencode 行并存；服务器本机若跑边端（独立凭据）即得自己的行。

### 3.2 凭据回填 IP（可选展示字段）

- 边端 `register` body 增加可选 `ip` 字段（由边端主动上报自身公网 IP），或服务端读 `request.client.host`（需在信任 nginx 的 `X-Forwarded-For` 后使用）。
- 写入 `runtime_credentials.last_ip`（DDL 加列），仅设备视图展示，**不入唯一键**。

### 3.3 heartbeat（`runtime.py:124-135`）

现状按 `runtime_ids IN (...)` 置 online —— 不变。实例化后每 worker 心跳只更新自己的行，天然精确。

### 3.4 recover-orphans（`runtime.py:196-204`）

现状按 `runtime_id` 把 dispatched/running 置回 queued —— 不变且语义变准：**每台 worker 只清自己的实例行**，消除"一台启动重置另一台进行中任务"的隐患。

---

## 4. 路由解析改造（核心）

### 4.1 两层绑定模型

```
绑定层（确定"要什么引擎/实例"）         路由解析层（把绑定翻译成 runtime_id）
─────────────────────────────            ─────────────────────────────────
A) 显式实例绑定：runtime_id 直接存实例 id   ──> 直接用（无选择）
   devops_agent_session.runtime_id        事件流水线（event_pipeline.py:278）
   chat_agent_session.runtime_id          chat（session_manager.py:557）
B) 引擎类型绑定：只有 name                  ──> 需在健康实例中选择（见 §4.3）
   ensure_default_runtime("opencode")
   get_runtime_id_by_name("opencode")
   agent_type.default_runtime_id（展示/默认）
```

### 4.2 显式实例绑定（推荐主路径）

事件流水线已存 `devops_agent_session.runtime_id`（repo+agent_type → 实例 id）。多实例后：

- 管理端为每个（repo, agent_type）绑定到**具体 worker 实例**（UI：选设备 → 选该设备的引擎）。
- 任务入队 `enqueue_task(runtime_id=该实例id)`（`service.py:158`），该实例 claim 其队列 → **不同 Agent 分到不同 worker，负载自然分担，且严格确定路由**。

### 4.3 引擎类型绑定（默认/兜底）需选择策略

`ensure_default_runtime`（`user/db.py:1595`）与 `get_runtime_id_by_name`（`user/db.py:1578`）目前"按 name 取单行"——多实例后**单行不唯一**，需二选一：

| 策略 | 行为 | 适用 |
|------|------|------|
| S1 首个实例 | 返回该 name 的最小/首个 id（固定） | 简单，但无健康感知 |
| S2 健康实例 | 在 `status='online'` 且最近心跳的实例中选（如最早 last_seen 或最小负载） | 默认推荐；需在线状态参与选择 |
| S3 全部分发 | 返回不唯一（破坏现有 `runtime_id` 单值语义） | 不采用 |

**决策（待评审）**：默认 `ensure_default_runtime(name)` 改为 S2 —— `WHERE lower(name)=? AND status='online' AND scope=? AND owner_id=? ORDER BY last_seen_at DESC LIMIT 1`（同 name 下无 online 实例时回退任意一个，保证可入队）。

### 4.4 可用性策略（关键决策）

实例化后，任务严格路由到实例 id；若该实例离线，`claim` 无人消费 → 任务滞留 `queued`。两种策略：

| 策略 | 语义 | 取舍 |
|------|------|------|
| **严格绑定** | 任务只等绑定的实例；离线则排队 | 确定性最强；离线即积压 |
| **按名 fallback** | 入队时若绑定实例 offline，改为该 name 的另一健康实例 | 高可用；路由非 100% 确定，需在 `enqueue_task` 增加 fallback 解析 |

**推荐**：默认**严格绑定**（与"确定路由 D3"设计一致）；fallback 作为管理端可选项（按 agent_type 开关）后续演进。

### 4.5 chat 侧

`session_manager.py:557`：`get_runtime_id_by_name(data["backend"]) or ensure_default_backend(...)` → 同样走 §4.3 选择策略（backend 视为引擎类型绑定）。

**注意（2026-08-09 落地后）**：chat 相关执行入口共三处，均做 type-B 引擎类型绑定解析，多实例化后**全部**受 §4.3 S2 策略影响：
- Web chat send：`session_manager.py:557`（`_enqueue_chat` 复用）
- Web chat SSE stream：`src/api/routes/chat.py`（本地 `get_runtime_id_by_name`/`ensure_default_runtime`）
- 飞书 chat / VOD bot：`src/im/handler_async.py`、`src/im/vod_handler_async.py`（同一解析）

---

## 5. 数据一致性 / 清理

### 5.1 `save_agent_runtimes`（refresh，`user/db.py:1516-1550`）防误删

现状按 `scope+owner_id` 删除不在本机发现列表的行（`:1550`）——多实例后会**误删远端注册的行**。

改为：删除条件加 `credential_id=0`（只清理本机未绑定凭据的行）；凭据绑定行（远端/本机边端注册）一律保留。

### 5.2 凭据吊销级联

`runtime_credentials.status='revoked'` 时（`settings.py:342` revoke）：其绑定的 `agent_runtimes` 行置 `status='offline'` 或删除（供设备视图不显示已吊销设备）。选项：**保留行 + 标记 offline**（审计友好）优先。

### 5.3 心跳超时 / GC

沿用 `derive_runtime_health`（`health.py`）四态；`about_to_gc` 行由 sweeper 或凭据管理清理（本期不新增 GC 任务）。

---

## 6. 设备视图与健康态

- 设备视图（`settings.py:194-224`）已按 `credential_id` 分组 —— **实例化后无需大改**，每个 worker 设备下列出各自的引擎行。
- 恢复"本机 runtime"展示：服务器本机若跑边端（自己的凭据），即以设备形式出现；或保留 `if orphan_ids and not devices` 兜底逻辑（多实例后 devices 恒非空，该分支自然不再命中，属预期）。
- `_runtime_health`（`settings.py:227-237`）：`credential_id>0` 走心跳四态，`=0` 走本机 installed/healthy —— 不变。

---

## 7. 迁移方案（存量数据）

> **状态：已实施**（`_rebuild_agent_runtimes_inst_key`，老库启动时自动执行；幂等，新库 DDL 已含新唯一键）。

现状数据（实测）：`agent_runtimes` id=1 opencode（已被 worker 占用 credential_id=1）、id=2 codearts（credential_id=0）。服务器本机 opencode 二进制已改名 `opencode.bak`（停用），id=1 行 `cli_path` 为 dangling（指向 `/usr/local/bin/opencode`）。

1. **DDL**：`agent_runtimes` 唯一键改为 `UNIQUE(credential_id, name, scope, owner_id)`（新库 DDL + 老库重建）。
2. **拆分被占用的行**：
   - 若 `credential_id>0` 的行（worker 注册覆盖的）→ 保留为该 worker 的实例行。
   - 服务器本机引擎（opencode/codearts，`credential_id=0`）若希望继续展示 → 本机跑边端后自动注册新实例行，或迁移时保留一条 `credential_id=0` 行作占位。
3. **存量绑定**：`devops_agent_session.runtime_id` / `chat_agent_session.runtime_id` 现有值指向旧行 id → 迁移后指向对应实例行（若 id 保留则无需改）。
4. **唯一冲突**：`credential_id=0` 的多行同名冲突（历史迁移遗留）→ 清理只留一行。

---

## 8. 改动清单（核心多实例键待实施；标注 ✅ 为 2026-08-09 已落地配套）

| 文件 | 位置 | 改动 | 状态 |
|------|------|------|------|
| `src/api/db_setup.py` | `:146` DDL | `UNIQUE(credential_id,name,scope,owner_id)`；`runtime_credentials` 加 `last_ip`；老库重建迁移 `_rebuild_agent_runtimes_inst_key` | ✅ 已实施 |
| `src/api/routes/runtime.py` | register | upsert 键改 `credential_id+name+scope+owner_id`；回填 `last_ip`（XFF 优先） | ✅ 已实施 |
| `src/api/routes/runtime.py` | heartbeat | 同步回填 `last_ip` | ✅ 已实施 |
| `src/api/routes/runtime.py` | `:19-31` auth | 不变（仍按 rta_ 校验） | — |
| `src/api/routes/settings.py` | revoke | 级联其 `agent_runtimes` 行置 offline | ✅ 已实施 |
| `src/api/routes/settings.py` | `:194-224` 设备视图 | 展示 `last_ip`（待 UI 补字段） | 部分待办 |
| `src/user/db.py` | `:1578` `get_runtime_id_by_name` | S2 健康实例选择（online 优先 + 最近心跳） | ✅ 已实施 |
| `src/user/db.py` | `:1595` `ensure_default_runtime` | S2 健康实例选择 | ✅ 已实施 |
| `src/user/db.py` | `:1516-1550` `save_agent_runtimes` | upsert/删除仅限 `credential_id=0` 行 | ✅ 已实施 |
| `src/runtime/service.py` | `:158` `enqueue_task` | （若启用 fallback）离线实例 fallback 解析 | 待实施（默认严格绑定） |
| `src/api/routes/agents.py` | `:13-30` agent_type | `default_runtime_id` 支持指向实例（UI 联动） | 待实施 |
| 前端 | `src/api/static/js/pages/*` | 设备/引擎绑定 UI（选实例）+ last_ip 展示 | 待实施 |
| `src/im/handler_async.py` | chat bot 执行分支 | 非 direct 入队 + 轮询 | ✅ 已实施 |
| `src/im/vod_handler_async.py` | VOD bot 执行分支 | 非 direct 入队 + 轮询 | ✅ 已实施 |
| `src/api/routes/chat.py` | `send_message_stream` | 非 direct 入队 + 轮询（SSE done） | ✅ 已实施 |
| `src/runtime/service.py` | `:553` | `wait_task_terminal` 轮询助手 | ✅ 已实施 |
| `src/agent/executor.py` | `_run_once` raw 行 | stdout "Session not found" 计入 error_events → 无 session 重跑 | ✅ 已实施 |
| `src/runtime/agent.py` | `_execute_task` | worker 逐步日志 | ✅ 已实施 |
| `src/api/routes/webhook.py` | `:197` A2A | 非 direct 入队（旁路对齐） | 待实施 |
| `src/api/routes/tasks.py` | `:202` | 非 direct 入队（旁路对齐） | 待实施 |
| `src/api/onboarding.py` | `:68` | 非 direct 入队（旁路对齐） | 待实施 |

**边端协议（`src/runtime/agent.py` / `client.py`）**：实例键改法下 register body 与协议不变，仅服务端按新键 upsert —— 无需改动；agent.py 本次仅加逐步日志（观察用途），与实例键无关。

---

## 9. 验收要点

1. **并存**：两台以上 worker（含本机边端）各注册 opencode → `agent_runtimes` 出现多行 opencode，各带不同 `credential_id`，均 online。*前置：本机 opencode 目前停用（`opencode.bak`），并存验证需先恢复本机二进制，或用两台远端 worker。*
2. **不覆盖**：worker A 注册后 worker B 注册，A 的 cli_path/version/credential 不被改写。
3. **分流**：repo+agent_type A 绑 worker1、B 绑 worker2 → 任务分别由对应 worker claim 执行（`agent_tasks.runtime_id` 各不相同）。
4. **选择**：引擎类型绑定（默认）在健康实例间选择；无 online 实例时回退可入队。
5. **清理**：refresh 不删除凭据绑定行；revoke 后设备下线且行标 offline。
6. **回滚**：可切回 `direct`；旧数据迁移可逆（备份 DB）。

---

## 10. 风险与待决策

| 决策 | 选项 | 建议 |
|------|------|------|
| 引擎类型绑定的实例选择 | S1 首个 / S2 健康 / S3 全分发 | **S2** |
| 离线实例的任务 | 严格绑定排队 / 按名 fallback | 默认严格绑定，fallback 作可选演进 |
| IP 采集 | 边端上报 / 服务端 X-Forwarded-For | 边端主动上报（避免反代信任问题） |
| 本机 runtime 形态 | 服务器跑边端（独立凭据）/ 保留 credential_id=0 占位 | 推荐本机跑边端，与远端统一 |

> 待评审后进入实现计划（可拆为：D1 注册键+DDL；D2 路由解析；D3 UI 绑定；D4 清理/吊销）。

---

## 11. 测试用例设计

### 11.1 已落地测试（2026-08-09，看护"远端执行 + 队列路由"现有链路）

| 文件 | 用例 | 看护点 |
|------|------|--------|
| `tests/test_agent_executor.py` | `TestStreamAgentSessionAutoRetry`（+2） | ① stdout ANSI 原始行 `Session not found` → 无 session 重跑（远端场景）；② stdout 无关错误不重跑 |
| `tests/test_runtime.py` | `TestWaitTaskTerminal`（+4） | 轮询返回终态 / 超时 `TimeoutError` / 轮询至迟完成 / failed 透传 |
| `tests/test_chat_handler.py` | `TestChatHandlerQueueMode`（+2） | 飞书 chat bot：非 direct 入队+轮询回复 / direct 本地流式 |
| `tests/test_vod_handler_queue.py` | `TestVodHandlerQueueMode`（+2，新文件） | 飞书 VOD bot：queue 入队 / direct 本地流式 |
| `tests/test_web_chat_tooluse.py` | `TestWebChatToolUseRegression`（+2） | web SSE：queue 模式入队后 SSE done 返回结果 / direct 仍走 stream_agent |

> 通用约定：执行模式用 `monkeypatch.setenv("DEVOPS_EXECUTION_MODE", ...)`（`get_execution_mode` 每次读 env），避免 teardown 污染共享 env；patch 局部 `from src.runtime.service import enqueue_task, wait_task_terminal` 命中源模块即可。

### 11.2 待实施核心改动（§2/§3/§4/§5）测试设计

实施时按以下用例补测（与 §9 验收一一对应）：

**R1 register 按 `(credential_id, name)` upsert**（入 `tests/test_runtime_protocol.py`）
- `test_multi_credential_same_name_coexist`：凭据 A、B 各注册 `opencode` → `agent_runtimes` 出现 2 行，`credential_id` 各 1/2，`cli_path`/`version` 互不覆盖（A 的 `/a/opencode` v1 不被 B 的 `/b/opencode` v2 改写）。
- `test_same_credential_register_idempotent`：同一凭据重复注册 → 行数不变，字段更新（幂等）。
- `test_revoked_old_credential_new_row`：凭据 A revoke 后新凭据 C 注册同名 → 新行出现，旧行保留 `offline`。

**R2 路由解析 S2 健康实例选择**（入 `tests/test_runtime.py` 或新 `tests/test_runtime_multi.py`）
- `test_pick_online_instance`：同名两实例（id1 online / id2 offline）→ `get_runtime_id_by_name("opencode")` 返回 online 的 id。
- `test_fallback_when_all_offline`：同名全 offline → 回退任意实例（保证非 None，可入队）。
- `test_ensure_default_no_dup`：同名已有多实例 → `ensure_default_runtime` 不重复插入，返回现有 id。

**R3 任务分流（实例级隔离队列）**（入 `tests/test_runtime.py`）
- `test_claim_only_own_runtime`：任务绑 `runtime_id=worker1` → `claim(1)` 返回、`claim(2)` 返回 None（跨实例隔离）。
- `test_agent_to_worker_routing`：repo+agent A 绑实例 1、B 绑实例 2 → `enqueue_task` 的 `runtime_id` 各自正确；两 worker 并发 claim 各得各的。

**R4 refresh 防误删**（`save_agent_runtimes`，入 `tests/test_runtime_protocol.py` 或新文件）
- `test_refresh_keeps_credential_rows`：存在 credential_id>0 的远端行 + credential_id=0 本机行 → refresh 后远端行保留，仅清本机不在发现列表的行。

**R5 revoke 级联**（settings revoke，入 `tests/test_runtime_protocol.py`）
- `test_revoke_marks_runtime_offline`：revoke 凭据 → 其绑定的 `agent_runtimes` 行 `status='offline'`（或删除），`GET /api/settings/agent-runtimes` 设备视图不再展示该设备。

**R6 设备视图多实例分组**（`GET /api/settings/agent-runtimes`）
- `test_devices_grouped_by_credential`：两凭据两实例 → `devices` 两组，各列各自引擎；`runtimes` 含全部实例行。

**R7 迁移（§7）**（入 `tests/test_api_routes.py` 或迁移专项）
- `test_migration_unique_key`：存量单行（credential_id=0 opencode）+ worker 占用行 → 建 `UNIQUE(credential_id,name,scope,owner_id)` 无冲突；被 worker 占用的行保留为该实例行。
- `test_migration_binding_unchanged`：存量 `devops_agent_session.runtime_id` / `chat_agent_session.runtime_id` 迁移后指向正确实例行（id 保留则断言不变）。

> 测试数据隔离沿用 conftest（`DEVOPS_DB_PATH=data/test_sessions.db`）；用例 fixture 需清理 `agent_runtimes` / `runtime_credentials` / `agent_tasks`（参照 `test_runtime_protocol.py::_clean`）。
