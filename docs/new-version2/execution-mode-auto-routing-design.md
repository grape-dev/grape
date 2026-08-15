# 执行模式按绑定引擎宿主自动推导改造设计（Direct / Queue 自动识别）

> 版本：v1.1（评审修订：废除 queue_local，取值域收敛为 auto/direct/queue_remote，见 engine-default-selection-design.md §9）
> 日期：2026-08-11
> 状态：待评审
> 前置：`agent-runtime-event-design.md`（M2 执行模式）、`multi-runtime-instance-design.md`（多实例/确定路由 §3-§4）、`concurrency-design.md`（队列/串行槽）、`runtime-admin-two-view-design.md`（Engines/Workers 双视图）
> 触发背景：测试环境（120.46.90.173）中 VOD Agent 已绑定远端引擎 `opencode@vod-worker1`，但全局执行模式为 `direct`，事件在**服务器本机**执行，vod-worker1 收不到任何任务、无执行日志；反之若全局切 `queue_remote`，绑定本机引擎（credential_id=0）的任务又无人 claim 会卡死。根因是「全局模式开关」与「per-agent 引擎绑定」两套配置脱钩。

---

## 〇、问题现象与根因

### 0.1 现象

- `mm6667/test1` 的 VOD Agent 会话绑定 `runtime_id=11`（= opencode，宿主 worker `vod-worker1` / credential 8）；
- 但 VOD 事件（seq 61/63/64/65）全部在 **120.46.90.173 服务器本机**执行：
  - `event_pipeline.py:432` 走的是 direct 分支（`Pipeline: started agent execution`）；
  - `[Agent:vod] spawning opencode` + `cwd=/opt/grape/data/workspaces/...`（本机文件系统）；
  - `agent_tasks` 表中该仓库无任何 vod 任务行；
- `vod-worker1`（IP 1.92.115.229，另一台 build-0002 机器）的 RuntimeAgent 始终 claim 不到任务、无执行日志。

### 0.2 根因（全局开关与 per-agent 引擎绑定脱钩）

| 维度 | 绑定引擎（配置） | 实际执行通道（运行） |
|---|---|---|
| 决策依据 | `devops_agent_session.runtime_id` → `agent_runtimes` 行 → 宿主 worker | **全局** `meta.execution_mode`（auto/direct/queue_remote） |
| 数据来源 | 用户在 Agent 配置页选的 engine | 与绑定**无关**，一刀切 |

- **direct 模式在架构上无法执行到远端 worker**：`_execute_agent`（`event_pipeline.py:702`）→ `stream_agent(..., backend_name=backend)` 按**引擎类型名**解析本机 `AgentRuntime`（opencode/hermes）并用**服务器本机 CLI** 跑；远端 worker 唯一的消费通道是 `enqueue_task(runtime_id)` → RuntimeAgent `claim(runtime_id)`（`runtime/agent.py:168-171`、`runtime/service.py:211`）。
- 反之，全局 `queue_remote` 时，绑定**本机引擎**（`credential_id=0`）的任务会被入队到无任何 worker claim 的 runtime_id → 永久卡 `queued`（本机 legacy 引擎行是孤儿，RuntimeAgent 只 claim 自己凭据下注册的引擎行）。

**结论**：`agent 绑定的 runtime_id → 引擎宿主 credential_id` 已经唯一决定了「该任务应该本地执行还是交给某台 worker」，但分发条件用的是全局开关，二者不匹配。

### 0.3 次生问题：queue 路径内 runtime_id 解析不一致（可能派错 worker）

部分入口**不**使用会话配置的 runtime_id，而是用 `get_runtime_id_by_name(backend)`（`user/db.py:1580`）在「所有同名引擎里挑 online + 最近心跳的一台」：

| 入口 | runtime_id 来源 | 结果 |
|---|---|---|
| `event_pipeline.py:294-297`（webhook 事件） | ✅ 会话配置的 `runtime_id` | 正确：绑定谁就派给谁 |
| `vod_handler_async.py:214`（IM/VOD） | ❌ `get_runtime_id_by_name(backend)` | 可能派给**另一台** opencode worker |
| `handler_async.py:469`（IM/Chat） | ❌ `get_runtime_id_by_name(backend)` | 同上 |
| `chat.py:233 / 396`（Web Chat） | ❌ `get_runtime_id_by_name(backend)` | 同上 |

即便全局切到 queue_remote，VOD(IM) 也可能把任务派给「最近心跳的 opencode worker」而不是用户绑定的那台。

---

## 一、目标

1. **执行通道按「绑定的引擎宿主 worker」自动推导**：`credential_id > 0`（远端 worker）→ 入队给该 worker；`credential_id = 0`（本机引擎）→ 服务器本机直接执行；
2. 全局 `execution_mode` 降级为「强制覆盖 / auto 兜底」，不再是唯一判定依据；
3. **顺带收敛** queue 路径的 runtime_id 解析，全部统一使用会话配置的 `runtime_id`，杜绝派错 worker；
4. 不破坏既有能力：确定路由 D3、串行槽 D9、技能/凭据/project-info 打包下发、任务可观测。

---

## 二、现状代码定位

| 位置 | 现状 |
|---|---|
| `src/runtime/service.py:15-19` | `EXEC_MODE_DIRECT/LOCAL/REMOTE` 常量 + `_DEFAULT_MODE`（默认 `direct`） |
| `src/runtime/service.py:22` `get_execution_mode` | 三层取值：env `DEVOPS_EXECUTION_MODE` → meta `execution_mode` → 默认 `direct` |
| `src/runtime/service.py:35` `set_execution_mode` | 写 meta 表 `execution_mode` |
| `src/runtime/service.py:158` `enqueue_task` | 入队入口（确定路由 D3 + 技能打包 + project-info + 凭据注入） |
| `src/runtime/service.py:211` `claim` | 按 runtime_id 原子抢单 + 串行槽 |
| `src/api/event_pipeline.py:294-297` | 从会话读 `runtime_id`（正确来源） |
| `src/api/event_pipeline.py:423-432` | 分发：`get_execution_mode() != "direct"` → 入队；否则本地线程 |
| `src/api/event_pipeline.py:485` `_enqueue_pipeline_task` | webhook 侧入队完整封装 |
| `src/api/event_pipeline.py:594` `_execute_agent` | direct 本地执行（`stream_agent`，按引擎类型名用本机 CLI） |
| `src/im/vod_handler_async.py:44` | 会话 SELECT 已含 `runtime_id`（但 214 行被 `get_runtime_id_by_name` 覆盖） |
| `src/im/vod_handler_async.py:209-214` | 模式判断 + runtime_id 解析（次生问题） |
| `src/im/handler_async.py:463-469` | 模式判断 + runtime_id 解析（次生问题） |
| `src/api/routes/chat.py:126/223/385` | 模式判断（`get_execution_mode() != "direct"`） |
| `src/api/routes/chat.py:232-233/396-397` | runtime_id 解析（次生问题） |
| `src/api/routes/webhook.py:202-215` | A2A：`get_execution_mode() == "direct"` → 线程；否则入队 |
| `src/prompt_builder.py:54-60` | `_build_history_file_guide`：`get_execution_mode()=="queue_remote"` 时远端历史不可达 |
| `src/runtime/agent.py:162-196` | RuntimeAgent `_run_loop`：对每个已注册 runtime_id 轮询 claim |
| `src/api/routes/settings.py:477` | PUT `/execution-mode` 校验白名单只有 auto/direct/queue_remote（`queue_local` 已废除） |

---

## 三、方案设计

### 3.1 核心：新增 `runtime_execution_kind(runtime_id)` 按宿主推导

在 `src/runtime/service.py` 新增两个 helper：

```python
def runtime_credential_id(runtime_id: int):
    """按 runtime_id 反查引擎宿主 credential_id；查不到返回 None。"""
    if not runtime_id:
        return None
    with db_lock:
        c = db.cursor()
        c.execute("SELECT credential_id FROM agent_runtimes WHERE id=?", (runtime_id,))
        row = c.fetchone()
    return row[0] or 0 if row else None


def runtime_execution_kind(runtime_id: int) -> str:
    """按绑定引擎宿主推导该任务的执行通道（auto 语义下的有效模式）：
    - credential_id == 0  → 本机引擎（服务器装着的 CLI）        → EXEC_MODE_DIRECT
    - credential_id >  0  → 远端 worker 承载的引擎             → EXEC_MODE_REMOTE
    - runtime_id=0 / 查不到（未绑定）→ 回退全局强制模式；全局为
      auto 时 → EXEC_MODE_DIRECT（未绑定默认本机执行，见 3.6）
    """
    cid = runtime_credential_id(runtime_id)
    if cid is None:
        mode = get_execution_mode()
        return EXEC_MODE_DIRECT if mode == "auto" else mode
    return EXEC_MODE_REMOTE if cid > 0 else EXEC_MODE_DIRECT
```

> **依据**：`agent_runtimes.credential_id` 即为引擎宿主凭据；>0 表示该引擎由远端 RuntimeAgent 注册（`runtime/agent.py:219` register 后拿到该凭据下的 runtime_id），=0 表示服务器本机 legacy/发现引擎。worker claim 按 runtime_id 定向，所以「识别宿主 → 决定入队与否」无需新增任何通道。
>
> **未绑定回退语义（关键）**：`auto` 下 `runtime_id=0`（Agent 未配置引擎）应回退 **direct**（本机默认），而不是回退全局值——否则全局若被设为 queue_remote，未配置的 Agent（尤其 Chat）会被入队到无 worker claim 的引擎而卡死。

### 3.2 全局开关语义调整：新增 `auto`（默认），旧值仍为强制覆盖

- `get_execution_mode()` 取值域扩展为：`auto` / `direct` / `queue_remote`（`queue_local` 已废除，见 `engine-default-selection-design.md` §9）；
- `_DEFAULT_MODE` 改为 `"auto"`（`service.py:19`），**开箱即用**：绑定了远端 worker 的 Agent 自动入队，绑定本机引擎的自动本地执行；
- 显式配置 `direct/queue_remote` 时仍为**强制模式**（向后兼容，运维可锁定全局行为）；
- `PUT /execution-mode` 校验白名单加入 `"auto"`（`settings.py:477`），同时移除 `queue_local`。

**统一分发写法（所有分发点复用）**：

```python
mode = get_execution_mode()
kind = runtime_execution_kind(runtime_id) if mode == "auto" else mode
if kind == EXEC_MODE_REMOTE and backend and skill_name and seq > 0:
    _enqueue_pipeline_task(...)   # 入队：远端 worker 按 runtime_id claim
elif backend and skill_name and seq > 0:
    threading.Thread(target=_execute_agent, ...)   # direct：本机进程内执行
```

> 说明：auto 只产出 `direct`（本地引擎）与 `queue_remote`（远端 worker）两态；`queue_local` 已废除（现状为无消费者的空壳，强制选择即卡 queued）。本机引擎一律 `direct`（并发控制见 `engine-default-selection-design.md` §9）。未绑定的兜底见 3.6。

### 3.3 各分发点改造

| # | 文件:行 | 改造 |
|---|---|---|
| 1 | `event_pipeline.py:424` | `mode != "direct"` → 3.2 的 `kind` 判断（`runtime_id` 已是 294-297 行解析好的会话值） |
| 2 | `vod_handler_async.py:210` | 同上；并改为使用会话 `agent_session["runtime_id"]`（见 3.4） |
| 3 | `handler_async.py:464` | 同上；Chat(IM) 用 chat 会话的 `runtime_id`（见 3.4） |
| 4 | `chat.py:126/223/385` | 同上；Chat(Web) 用 chat 会话 `runtime_id`（见 3.4） |
| 5 | `webhook.py:203`（A2A） | `get_execution_mode()=="direct"` → `kind == EXEC_MODE_DIRECT`（A2A 已解析 runtime_id） |
| 6 | `prompt_builder.py:57` | `_build_history_file_guide` 增加 `runtime_id` 入参（或 `remote_engine: bool`），改为 `runtime_execution_kind(runtime_id) == "queue_remote"` 判定远端历史不可达 |
| 7 | `settings.py:477` | 白名单加入 `"auto"` |

### 3.4 收敛 runtime_id 解析（修「派错 worker」次生问题）

统一原则：**queue 分支的 runtime_id 一律取会话配置值**，不再用 `get_runtime_id_by_name(backend)` 现挑；仅在 `runtime_id == 0`（未绑定）时保留 `get_runtime_id_by_name(backend) or ensure_default_runtime(backend) or 0` 兜底。

| # | 位置 | 现状 | 改为 |
|---|---|---|---|
| 1 | `vod_handler_async.py:214` | `get_runtime_id_by_name(backend) ...` | `agent_session.get("runtime_id") or get_runtime_id_by_name(backend) or ...` |
| 2 | `handler_async.py:469` | `get_runtime_id_by_name(backend) ...` | chat 会话的 `runtime_id`（`chat_agent_session`）或 `get_runtime_id_by_name(backend)` 兜底 |
| 3 | `chat.py:232-233 / 396-397` | `get_runtime_id_by_name(backend_name) ...` | 同上 |
| 4 | `event_pipeline.py:294-297` | 已用会话 runtime_id | 不动（作为正确范式） |

### 3.5 远端 worker 离线/吊销的兜底（防任务卡死）

auto 模式下绑定引擎若在远端 worker 且该 worker 离线/凭据吊销，入队后任务将无人 claim：

- 入队前检查：`runtime_credential_id(runtime_id)` 对应的凭据 `status != 'active'` 或引擎 `status` 非 online 时，**记告警日志并拒绝入队**（或按配置降级本机 direct 兜底）；
- 队列侧兜底（可选、可另文）：`agent_tasks` 中 `queued` 超时（如 > N 分钟）自动置 failed + 通知，避免静默积压。

> 本方案先落「入队前校验 + 告警」，超时重投作为附带观察（§七）。

### 3.6 Chat 特化：默认 direct + 绑定引擎 auto 路由

**目标语义**（与 3.1 auto 一致，且未绑定默认本机执行）：

| Chat 配置 | 判定 | 执行通道 |
|---|---|---|
| 未配置 / `runtime_id=0` | auto 下回退 | **direct**（本机引擎，直接用） |
| 配置本机引擎（`credential_id=0`） | auto | **direct** |
| 配置个人 worker 引擎（`scope=user`，`credential_id>0`） | auto | **queue_remote**（入队给该 worker） |

> **Chat 引擎选择面**：`GET /chat/config` 只提供 `scope='user'` 的个人引擎（`chat.py:536-540`），这类引擎均为某台远端 RuntimeAgent 凭据注册（`credential_id>0`）。所以「用户配置了其它 worker 的引擎」在 UI 上**已支持**，缺的是**路由**——direct 模式无视它、queue 模式又按名现挑。

**现状 3 个断点**：

| # | 位置 | 断点 |
|---|---|---|
| 1 | `session_manager.py:574` `get_chat_config` | 只返回 `backend` 类型名，**丢失用户配置的 `runtime_id`** → 无法判定宿主、无法定向入队 |
| 2 | `chat.py:233/397`、`_enqueue_chat:397` | 用 `get_runtime_id_by_name(backend_name)` **按名现挑**（online+最近心跳），不是用户配置的那台 |
| 3 | `chat.py:126/223` | 分支用全局 `get_execution_mode() != "direct"`，与绑定无关 |

**落地改动（叠加到 §四 改动 6）**：

| # | 位置 | 改动 |
|---|---|---|
| 1 | `session_manager.py:574` | `get_chat_config` 额外返回 `runtime_id`（读 `chat_agent_session.runtime_id`，即 `set_chat_config` 585-594 行已解析存储的值） |
| 2 | `chat.py:126/223` 分支 | 改为 `kind = runtime_execution_kind(runtime_id)`：`remote` → 入队；否则 → direct |
| 3 | `chat.py:233/397` + `_enqueue_chat:397` | 解析/入队优先用 `cfg.runtime_id`；仅 `runtime_id=0` 时保留 `get_runtime_id_by_name(backend)` 兜底 |

**边界**：

- 配置的 worker 引擎离线/凭据吊销：Chat 建议**降级本机 direct + 告警**（或直接报「所选引擎离线」），避免任务静默卡 `queued`（结合 3.5 校验）；
- direct 路径无需改：`stream_agent(backend_name)` 用的即本机 CLI，本地引擎时语义正确。

---

## 四、改动点清单

| # | 文件 | 改动 | 说明 |
|---|---|---|---|
| 1 | `src/runtime/service.py` | 新增 `runtime_credential_id` / `runtime_execution_kind`；`_DEFAULT_MODE` 改 `"auto"` | 核心 |
| 2 | `src/api/event_pipeline.py:424` | 分发条件改用 3.2 的 `kind` | 核心 |
| 3 | `src/api/routes/settings.py:477` | 白名单加 `"auto"` | 配置入口 |
| 4 | `src/im/vod_handler_async.py:210-214` | 模式分支 + runtime_id 收敛 | 修次生问题 |
| 5 | `src/im/handler_async.py:464-469` | 同上 | 修次生问题 |
| 6 | `src/api/routes/chat.py:126/223/385 + 232/396` | 分支改 `runtime_execution_kind`；解析/入队优先 `cfg.runtime_id` | Chat 特化（3.6） |
| 7 | `src/session_manager.py:574` | `get_chat_config` 返回 `runtime_id` | Chat 特化（3.6） |
| 8 | `src/api/routes/webhook.py:203` | A2A 分支改用 `kind` | 统一 |
| 9 | `src/prompt_builder.py:54-60` | 传 runtime_id，远端判定改 `runtime_execution_kind` | prompt 一致性 |
| 10 | `src/runtime/service.py`（3.5） | 入队前引擎/凭据状态校验 + 告警 | 防卡死 |
| 11 | `tests/`（新增 `test_runtime_auto_mode.py`） | 见 §五 | 覆盖 |

### 关键实现注意

- `runtime_execution_kind` 需在 `src/runtime/__init__.py` 导出（现有 `get_execution_mode/set_execution_mode` 同款）。
- 分发点注意保留 `backend and skill_name and seq > 0` 的前提，避免空配置空跑。
- `_enqueue_pipeline_task` 内已有 `runtime_id` 为空时 `ensure_default_runtime("opencode")` 兜底（`event_pipeline.py:498-503`），与 3.4 的兜底语义不冲突。

---

## 五、验证方案

### 5.1 单元测试（新增 `tests/test_runtime_auto_mode.py`，参考 `test_m2_execution_mode.py` 隔离写法：env/meta 切换 + `_clean()` 清表）

| # | 用例 | 前置 | 步骤 | 断言 |
|---|---|---|---|---|
| T1 | **远端引擎 → queue_remote** | 会话 runtime_id 指向 `credential_id>0` 的引擎 | `runtime_execution_kind(rid)` | `== "queue_remote"` |
| T2 | **本地引擎 → direct** | runtime_id 指向 `credential_id=0` 的引擎 | `runtime_execution_kind(rid)` | `== "direct"` |
| T3 | **未绑定回退** | runtime_id=0 / 引擎不存在 | `runtime_execution_kind(rid)` | auto 下回退 `"direct"`；强制模式下回退该强制值 |
| T4 | **auto + 远端绑定 → 入队** | mode=auto，会话绑远端引擎 | `run_pipeline(issue_opened)` | `agent_tasks` 出现行（status=queued，event_seq/issue_id/runtime_id 正确） |
| T5 | **auto + 本地绑定 → 本地执行** | mode=auto，会话绑本地引擎 | `run_pipeline(issue_opened)` | `agent_tasks` **无**新行；`events` 走 direct 完成 |
| T6 | **强制模式覆盖 auto** | mode=direct / queue_remote | 同 T4 | 行为与强制模式一致（覆盖推导） |
| T7 | **runtime_id 收敛** | 会话绑 vod-worker1 的 opencode | 走 `vod_handler_async` 入队分支 | `agent_tasks.runtime_id == 会话 runtime_id`（非 `get_runtime_id_by_name` 现挑的 id） |
| T8 | **离线 worker 拒绝入队** | 绑定引擎 status=offline / 凭据 revoked | 触发入队 | 拒入队 + 告警日志；无 queued 积压 |
| T9 | **Chat 未配置 → direct** | mode=auto，用户无 chat 配置（`runtime_id=0`） | `runtime_execution_kind(0)` / chat send | `== "direct"`；本机执行，不入队 |
| T10 | **Chat 配置个人 worker 引擎 → 入队给该 worker** | mode=auto，`chat_agent_session.runtime_id` 指向 `credential_id>0` 引擎 | chat send | `agent_tasks` 新增行，`runtime_id == 会话配置值`（非 `get_runtime_id_by_name` 现挑的 id） |
| T11 | **Chat 配置的 worker 离线 → 降级/报错** | `chat_agent_session.runtime_id` 指向 offline 引擎 | chat send | 不静默卡 queued（降级本机 direct + 告警，或明确报错） |

### 5.2 端到端

1. 环境 meta `execution_mode=auto`（默认）→ 触发 `mm6667/test1` 新 VOD 事件；
2. 断言本机日志出现 `Pipeline: enqueued task=...`（而非 `started agent execution`）；`agent_tasks` 出现对应行；
3. vod-worker1（1.92.115.229）上出现 claim 与执行日志；
4. 回归：将绑定切回本机引擎（credential_id=0），再触发事件 → 走本机 direct 执行，无 queued 积压。

### 5.3 回归范围

- 既有 `tests/test_m2_execution_mode.py`、`test_runtime.py`、`test_runtime_protocol.py`、`test_pipeline.py`、`test_a2a_queue.py` 全量通过；
- 确认显式 `direct` / `queue_remote` 强制模式行为与改造前一致。

---

## 六、影响与风险

| 项 | 说明 |
|---|---|
| 行为变化 | 默认 mode 从 `direct` 变为 `auto`：绑定远端 worker 的 Agent 由「本机直跑」改为「入队 + worker claim」（引入 ~3s 轮询延迟，可接受）；绑定本机引擎的 Agent 行为不变 |
| Chat 默认语义 | Chat 未配置默认**本机 direct 执行**（与现状一致）；仅当用户配置了个人 worker 引擎时才走队列——对既有 Chat 用户行为不回归，并修复「配了 worker 引擎却本机执行」的错位 |
| 向后兼容 | 已有显式 `execution_mode` 配置（direct/queue_remote）仍为强制值，不感知变化；仅「未显式配置」的环境默认值变化 |
| 派错 worker | 收敛 runtime_id 后，任务严格按会话绑定 worker 派发；`get_runtime_id_by_name` 仅保留为未绑定兜底 |
| 任务卡死 | auto 模式下远端引擎离线时任务拒入队 + 告警；避免 queued 永久积压 |
| 回滚 | 核心改动集中在 service.py helper + 各分发点分支条件；回滚即恢复 `get_execution_mode()` 判断 |
| 数据 | 无表结构变更；meta `execution_mode` 值域扩展（加 `auto`） |

---

## 七、附带观察（非本次改动范围）

- **`queue_local` 已废除**：auto 只区分「本地引擎→direct / 远端引擎→queue_remote」，不再有本地入队模式（详见 `engine-default-selection-design.md` §9）。
- **`set_execution_mode` 注释「改键值 + 重启生效」不准确**：`get_execution_mode()` 每次调用实时读 meta，无需重启；本文档沿此语义（运维改 meta 即生效）。
- **任务级超时/重投**：`agent_tasks.queued` 长时间未 claim 的检测与重投机制（超出本次离线校验范围），可作为独立能力规划（`concurrency-design.md` 语境）。
- **引擎健康与自动迁移**：绑定引擎持续 offline 时，是否自动迁移到同类型在线引擎（S2 健康选择），建议结合 `runtime-admin-two-view-design.md` 的告警做产品级决策，本文不改。

---

*本文为执行模式自动路由改造方案，待评审后实施。*
