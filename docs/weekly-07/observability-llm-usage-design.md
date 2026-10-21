# 可观测性 · 「模型用量与 Token 消耗」设计方案

> 状态：**待评审**（副设计文档）
> 主文档：`docs/weekly-07/observability-redesign-design.md`
> 总览图表：`docs/weekly-07/observability-overview-page-design.md`（本文件为其图表 ⑧⑨ 的展开）
> 结论前置：**这块目前等于从零开始** —— 管道修了一半、没人用，且现有字段被别的语义占着

---

## 1. 现状核查（不是"没做"，是"做了一半且坏了"）

我把相关代码和数据都核了一遍，结论比"没设计"更具体：

| 环节 | 现状 | 证据 |
|---|---|---|
| 上报端点 | ✅ 已存在 | `POST /api/runtime/tasks/{task_id}/usage`，`UsageBody{provider,model,input,output,cache}`（`src/api/routes/runtime.py:96,622`） |
| worker 侧客户端 | ✅ 已存在 | `RuntimeClient.report_usage()`（`src/runtime/client.py:249`） |
| 指标埋点 | ✅ 已存在 | `record_llm_usage()` → `grape_llm_tokens_total{provider,model,token_type}`（`src/observability/__init__.py:347`） |
| **调用方** | ❌ **一个都没有** | `report_usage` 全仓只有定义处，无第二处引用；`/metrics` 里 4 个 llm 指标**只有 HELP/TYPE 行，零样本** |
| **引擎侧解析** | ⚠️ 只有 codex，且只用于界面展示 | `src/runtime/codex.py:91` 解析 `turn.completed.usage.{input_tokens,output_tokens,reasoning_output_tokens}` → 映射成 `step_finish.tokens`，**没有回传用量台账**。ACP 系（hermes/hwcloud/aishell/opencode/codearts）**完全没解析**（`hermes_acp.py` 只读 `stopReason`，不读 usage） |
| **存储字段** | ❌ **语义被占用，不能复用** | `agent_tasks.usage_json` 实际存的是**会话续接指针** `{session_id, work_dir, engine}`（由 `complete_task(usage=...)` 写，`service.py:1895`）。307 个任务中 229 条有值，**含 token 字段的 0 条**。且该字段被续接逻辑消费（`routes/events.py:1481` 读 `usage.session_id`） |
| **副作用 Bug** | 🔴 现存 | `/usage` 端点执行 `UPDATE agent_tasks SET usage_json=?` —— **一旦真有引擎调用，会把会话指针冲掉**，破坏多轮续接（B2 引擎守卫也会读到错值） |
| 成本/定价 | ❌ 无 | 全仓无模型单价表、无成本计算 |
| 展示 | ⚠️ 仅 codex 任务逐轮显示 tokens | 无台账、无聚合、无模型维度视图 |

**一句话**：管道（端点+客户端+指标）建好了但**两端都没接上**，存储字段还被续接指针占着，而且那个端点带着一个会冲掉指针的 bug。所以设计的第一件事不是加功能，是**把这条断链接对，并且不碰 `usage_json`**。

---

## 2. 目标

面向用户回答四个问题：

1. **我用的是哪些模型？** —— 模型维度占比与排行
2. **各消耗了多少 token？** —— input / output / cache / reasoning 分开看
3. **花了多少钱？** —— 按模型单价折算成本（未定价模型显式标注，不假装是 0）
4. **钱花在哪了？** —— 按仓库 / 场景 / 引擎 / 用户下钻

**明确不做**：不做平台级账单/配额扣费（那是 L2 admin 的事）、不做模型路由优化建议、不做与外部财务系统对接。

---

## 3. 采集设计（断链接对）

### 3.1 采集点（**已实测完成**，2026-10-17）

统一入口是 worker 侧的 `report_usage`。**各引擎的 usage 从哪来，已逐个查证**（不是推测，证据列在下面）：

| 引擎 | 采集点 | 字段 | 语义 | 结论 |
|---|---|---|---|---|
| **codex** | 事件流 `turn.completed.usage` | `input_tokens` / `output_tokens` / `reasoning_output_tokens` | **每轮增量** | ✅ 已在 `codex.py:91` 解析，接上报即可 |
| **opencode** | 引擎本地 SQLite `opencode.db` → `message.data`（JSON），按 `session_id` 聚合 | `tokens{total,input,output,reasoning,cache{read,write}}` + `cost` + `modelID` + `providerID` | **每条 assistant 消息一份增量**（多轮需 SUM） | ✅ 实测：本地 1157 个会话有 token 数据 |
| **hermes** | 引擎本地 SQLite `state.db` → **`session_model_usage`** 表（隔离 HOME 内） | `input_tokens` / `output_tokens` / `cache_read_tokens` / `cache_write_tokens` / `reasoning_tokens` / `api_call_count` / `estimated_cost_usd` / `actual_cost_usd` / `cost_status` / 模型 / provider / **task**（区分主任务与 `title_generation` 等辅助调用） | **累计值**（表内已按 (session,model,provider,task) 累加） | ✅ 实测：每个 grape 任务的隔离 HOME 里都有行，数字真实（例：47 次调用 / input 58,170 / output 15,046 / cache_read **2,147,456**） |
| **hermes（另一条路）** | `hermes -z --usage-file <path>` | per-run JSON usage report | 单次运行 | ✅ 存在（`hermes_cli/oneshot.py:142`），但**仅一次性模式**，ACP 模式不可用 |
| **hwcloud / aishell**（同一引擎，aishell 是 hwcloud 的换名） | 引擎**隔离目录**内的日志 `<iso_home>/logs/hwcloud.log`（JSON 行） | `model` / `prompt_tokens` / `completion_tokens` / `total_tokens` | **每次 agent run 增量** | ✅ 实测：3201 行日志中 694 行带 token，如 `deepseek-v4-flash-0731 · 7501/74/7575`。⚠️ 无 cache 字段 |
| **hwcloud 的 ACP `usage_update`** | ACP `session/update` | `{cost:{amount,currency}, used, size}` | 每次 turn | ✅ **已实测抓包**：`{cost:{amount:0,currency:"USD"}, used:7499, size:1000000}` —— **只有成本与上下文压力，没有 input/output 拆分**，不足以做 token 台账。⇒ hwcloud 仍以日志为准 |
| ~~aishell~~ | 同 hwcloud | — | — | ✅ 已澄清：aishell 即 hwcloud 换名，采集路径同 hwcloud |

**归属问题已解决**：hermes 与 hwcloud 都用**按 cwd-hash 分片的隔离目录**，grape 知道每个任务的 cwd → **能精确定位该任务的引擎侧账本**；opencode 直接用 `usage_json.session_id` 关联。

hwcloud 的隔离实测（本次抓包副产物）：设 `OPENAGENT_CLI_CONFIG=<per-task settings.json>` 后，**整个工作集被重定位**——测试跑完在隔离目录里生成了 `logs/hwcloud.log` 与 `memory/memory.db`，而共享的 `~/.hwcloud/logs/hwcloud.log` **一个字节都没动**。这就是 hwcloud 的归属答案：读 `<iso_home>/logs/hwcloud.log` 即可，不存在"多任务混在一个日志里分不清"的问题。
⚠️ **例外**：`hwcloud_acp.py:214` 有分支——**若没有任何 provider 配置则不重定位**（让 hwcloud 走设备默认配置链）。这种情况下日志落在设备默认位置、per-task 隔离失效，采集要能识别并降级为"用量未知"。

**hwcloud 的 `used` 可以交叉校准**：ACP 报 `used=7499`，同一次运行的日志报 `prompt_tokens=7499` —— 两者一致；源码也印证了这一点（`acp/server.go:2468`：`SendUsageUpdate(usage.PromptTokens, cw, s.usageCost(...))`，即 `used = PromptTokens`、`size = 上下文窗口`）。**`used` 是 prompt 侧压力，不是 total**。

#### hwcloud 的上游版本核对（`yusheng-g/openagent-go`，MIT）

| 项 | 结果 |
|---|---|
| 仓库活跃度 | 最后 push **2026-10-16**（活跃）；**无 tag / 无 release** ⇒ 版本就是 commit SHA |
| 本机二进制版本 | ACP `agentInfo.version = 8b49d913` = 上游 commit `8b49d91373`（**2026-10-09**）⇒ **落后 8 天 / 8 个提交 / 4 个 PR** |
| 落后的提交 | SIGHUP 配置热重载（#125）、reasoning-effort 按模型词表约束（#124）、keyring 读华为云凭据（#123）、openviking 记忆归属修复（#122） |
| **是否影响采集** | **不影响**。逐文件核对：产生 token 日志的 `hooks/slog/hook.go` **未改**；`acp/sdk/types.go` 的 `usage_update` 结构 **未改**（`Cost` / `Used` / `Size` 三字段） |

⇒ **采集方案无需调整**，且这不是"等新版本会补"的问题：`usage_update` 的字段定义里**本来就没有 input/output 拆分**，只能走日志。

**但发现一条成本侧的互补路径**：openagent 支持在 model config 里配单价（`input_cost_per_million` / `input_cache_cost_per_million` / `output_cost_per_million`），注释写明"feed usage reporting (ACP usage_update cost)"。即**在引擎侧配好单价后，ACP `usage_update.cost` 会给出真实成本**（现在报 0 是因为我们用的自定义 provider `opengw` 没配单价）。
- 可用性：我们自己的单价表（§5.2）算一份、引擎侧算一份，**交叉校验**
- 代价：低成本——把 `hwcloud_acp.py:16` 丢弃 `usage_update` 的行为改成收下 `cost` 即可

**对升级的建议**：采集不依赖版本，所以**不强制升级**；但本机二进制落后 8 天，若要升，注意 #124 改过 `acp/server.go`（reasoning-effort 词表），升完要跑一次 §9 验收 1 的用量对账。

#### 🔴 结构性发现（比字段位置更重要）

**ACP 协议本身不携带计费 token。** 两个 ACP 引擎的 `usage_update` 都验证过了：

| 引擎 | ACP `usage_update` 内容 | 够不够做台账 |
|---|---|---|
| hermes | `{size: 上下文窗口, used: 估算请求压力}`（源码注释明说是给 Zed 的 context indicator） | ❌ 只有压力 |
| hwcloud | `{cost:{amount,currency}, used, size}` | ❌ 有成本，无 input/output 拆分 |

推论：**ACP 引擎的 token 用量不能靠解析 ACP 事件流拿到**，必须走"引擎自有账本"（本地 DB / 隔离目录日志 / 专用导出）。这直接决定了采集架构：

1. **不能**做一个"统一 ACP 事件流解析器"就完事 —— 那条路对 token 计数是死的
2. 要按引擎各写一个**账本读取适配器**，在任务终态时由 worker 汇总上报
3. 顺带修一个现成的浪费：`hwcloud_acp.py:16` 把 `usage_update` 当噪音事件**丢掉了**——它带的 `cost` 字段应当收下（配上单价表就能直接用）
4. 长期应推动上游在两个引擎的 `usage_update` 里补 input/output 计费字段，但**不能等它**

#### hermes 账本写入时机（已核实）

问题：任务终态那一刻，`session_model_usage` 是否已落盘？**是。**

| 机制 | 细节 |
|---|---|
| 写入方式 | 合批后台线程 `session-db-token-writer`（daemon） |
| 空闲阈值 | `_TOKEN_WRITER_IDLE_SECONDS = 30.0` —— 队列空 + 空闲 30s 后**先排干再退出** |
| 退出排干 | `close()` → `_stop_token_writer(join_timeout=10.0)`；另有 **`atexit` 强引用排干钩子** |
| grape 的终止方式 | `_terminate_proc`：**SIGTERM → 等 ≤10s → 超时才 SIGKILL** ⇒ 正常路径给了 atexit 排干机会 |

**实测（决定性）**：30 个 overlay 的 `state.db` 全都有 usage 数据，且**最后写入时间距今均 > 1 小时（进程早已退出），数据仍在盘上**；`state.db` 的 mtime 与 `last_seen` 相差在 ±131s 内（负值由 WAL checkpoint 解释），**没有看到尾部丢失**。

**残余风险（要写进实现）**：
- 若 SIGTERM 的 10s 宽限被耗尽 → SIGKILL → 内存里最多 ~30s 的增量丢失。表现为**小幅少计**，不是全丢；可接受，但要在文档里标明
- **WAL 陷阱**：隔离目录里有 `state.db` + `state.db-wal` + `state.db-shm` 三件套。进程已退出且未 checkpoint 时，只读连接读 WAL 可能失败 ⇒ **实现要先把三件套拷到临时目录再读**，不要直接对现场的 db 开只读连接

#### 待办（P0 内）

- [x] ~~确认 aishell 的 usage 来源~~ → aishell = hwcloud，同路径
- [x] ~~确认 hwcloud ACP `usage_update` 是否带计费 token~~ → 带 cost，但不带 input/output 拆分 ⇒ 日志为准
- [x] ~~确认 `session_model_usage` 写入时机~~ → 已落盘（§上）

#### 语义核实结果（原 R3 风险）

| 引擎 | 语义 | 处理 |
|---|---|---|
| codex | 每轮增量 | SUM |
| opencode | 每条消息增量 | SUM |
| hwcloud 日志 | 每次 run 增量 | SUM |
| hermes `session_model_usage` | **累计值**（表内已累加） | **取快照差值**，不能 SUM（SUM 会因多次写入重复计数） |

⚠️ **这是最容易被写错的地方**：hermes 与其余三家语义相反。同一套聚合代码直接套用会让 hermes 用量翻好几倍。

#### 待办（P0 内）

- [ ] 装一台 aishell（或用远端 worker 实测）确认其 usage 来源
- [ ] 确认 hwcloud ACP `usage_update` 是否携带计费 token（若带，则 hwcloud 无需读日志）
- [ ] 确认 `session_model_usage` 的写入时机（是否在每次 API 调用后即写、任务终态时是否已刷盘）

### 3.2 上报协议（扩展既有端点，改为追加语义）

```jsonc
POST /api/runtime/tasks/{task_id}/usage
{
  "provider": "anthropic",       // 归一化前的原始值
  "model": "claude-sonnet-4-5",  // 引擎报告的模型名
  "input": 12034,
  "output": 842,
  "cache_read": 9000,            // 新增：与 input 定价不同，必须分开
  "cache_write": 1200,           // 新增
  "reasoning": 300,              // 新增：codex 的 reasoning_output_tokens
  "turn": 1,                     // 新增：同一任务第几轮（多轮累加的基础）
  "seq": 7                       // 新增：幂等键（worker 重试不重复计数）
}
```

**三条硬规则**：
1. **追加不覆盖** —— 一个任务可能多轮、多模型（主模型 + 小模型做摘要），必须 INSERT 多行。现有端点的 `UPDATE ... SET usage_json=` 语义是错的，**必须改掉**（这也顺手修掉 §1 那个冲掉指针的 bug）
2. **绝不写 `usage_json`** —— 该字段是会话续接指针，归 `complete_task` 独占
3. **幂等** —— 以 `(task_id, seq)` 唯一键去重，防止 worker 重试把用量翻倍

### 3.2.1 主控机内运行的采集（2026-10-17 审计补齐，决定 B）

**缺口**：3.1 的采集器原本只在 **worker 侧**（`src/runtime/agent.py`）被调用 → 主控机内直接跑的引擎
（chat agent、IM bot、devops agent、视频理解）**一条 token 都没进台账**。引擎原生账本在主控侧同样存在，
采集代码（`UsageCollector`）完全可复用，缺的只是"谁来调"。

**归属口径（决定 B）**：这类运行没有 `agent_tasks` 行，因此

| 字段 | 取值 |
|---|---|
| `run_kind` | `chat` / `devops` / `im` / `wiki`（worker 任务固定 `task`） |
| `run_id` | 运行主体：`task` 类 = `task_id`；其它 = `chat:<chat_task_id>`、`im:<随机短id>` 等 |
| `task_id` | **空串**（只有 worker 任务才有） |
| 幂等键 `dedup_key` | `task` 类**沿用老格式** `{task_id}:{seq}`（升级后重报仍命中老行，不会翻倍）；其它 `{run_kind}:{run_id}:{seq}` |
| 归属 `repo_key`/`scene_id`/`agent_type`/`user_id`/`engine_id` | 由调用方经 `MasterRun(attr=...)` 传入 —— **只允许内部代码**，不走 HTTP 上报口（否则成本口径可被伪造） |

**实现**：`src/observability/master_usage.py`

```python
_mu = MasterRun("chat", chat_task_id, engine=backend_name, cwd=cwd,
                attr={"repo_key": repo_key, "scene_id": conv_id,
                      "agent_type": "chat", "user_id": user["id"]})
_mu.start()
try:
    for evt_type, data in stream_...():
        _mu.feed(evt_type, data)
        ...
finally:
    _mu.finish(session_id=_new_sid)     # 直接写台账（不走 HTTP），幂等 + 异常全吞
```

已接线（4 个入口家族全覆盖）：
1. **chat 三个本机直连入口**（`agent_fn` / 非流式 `run_agent` / SSE `agent_worker`），`run_kind='chat'`；
2. **IM 直连**（`im/handler_async.py::_direct_exec`），`run_kind='im'`；
3. **事件流水线 / autopilot 三个 DIRECT 入口**（`api/event_pipeline.py`、`api/routes/events.py` 事件对话重跑、
   `runtime/scheduler.py::_execute_autopilot_direct`）—— 用 `for_task()`：**有 `agent_tasks` 行就按
   `run_kind='task'`** 记（归属自动反查、幂等键与 worker 侧一致 → 跨本机/worker 不重复计）；
   只有无 task 行的 `d_<seq>` 兜底路径落 `run_kind='devops'`；
4. **VOD Bot**（`im/vod_handler_async.py`），`run_kind='vod'`。

worker 侧的采集路径不变（原本就有）。

**读取口径**：`task_usage(task_id)` 只出 `run_kind='task'`；`run_usage(run_kind, run_id)` 读单次非任务运行；
`usage_by_kind(since)` 按类型聚合（对账/报表用）。总览与 rollup 的 token 口径：**有 repo_key 的行进仓库维度，
没有的（如非仓库会话的 chat）只进 global** —— 全局 token 不再漏掉这部分真实开销。

### 3.3 归属维度（不信任 worker 上报）

`repo_key / scene_id / agent_type / user_id / engine_id` **由主控从 `agent_tasks` 反查**，不接受 worker 上报——避免用量归属被伪造或写错。worker 只负责**报 token 和模型**。

---

## 4. 存储设计

### 4.1 明细表 `obs_llm_usage`

```sql
CREATE TABLE IF NOT EXISTS obs_llm_usage (
    id           BIGINT AUTO_INCREMENT PRIMARY KEY,
    task_id      TEXT NOT NULL,
    seq          INTEGER NOT NULL DEFAULT 0,      -- 幂等键，与 task_id 联合唯一
    turn         INTEGER NOT NULL DEFAULT 0,
    repo_key     TEXT DEFAULT '',
    scene_id     TEXT DEFAULT '',
    agent_type   TEXT DEFAULT '',
    engine_id    INTEGER DEFAULT 0,
    user_id      INTEGER DEFAULT 0,
    provider_raw TEXT DEFAULT '',                  -- 上报原值（排查归一化问题用）
    model_raw    TEXT DEFAULT '',
    model_key    TEXT DEFAULT '',                  -- 归一化后的规范名（聚合用）
    input_tokens      INTEGER NOT NULL DEFAULT 0,
    output_tokens     INTEGER NOT NULL DEFAULT 0,
    cache_read_tokens INTEGER NOT NULL DEFAULT 0,
    cache_write_tokens INTEGER NOT NULL DEFAULT 0,
    reasoning_tokens  INTEGER NOT NULL DEFAULT 0,
    created_at   TEXT DEFAULT (datetime('now')),
    UNIQUE KEY uk_task_seq (task_id(64), seq)
);
CREATE INDEX ix_llm_usage_time  ON obs_llm_usage (created_at);
CREATE INDEX ix_llm_usage_model ON obs_llm_usage (model_key, created_at);
CREATE INDEX ix_llm_usage_repo  ON obs_llm_usage (repo_key, created_at);
```

> **变更（2026-10-17，决定 B）**：明细表新增 `run_id TEXT`、`run_kind TEXT DEFAULT 'task'`
> （索引 `idx_obs_llm_run(run_kind, run_id)`），并把"非任务运行"纳入台账口径；
> 表结构、幂等键规则与归属传递方式见 §3.2.1。归档表 `obs_llm_usage_archive` 同步两列。

### 4.2 为什么不复用 `usage_json`
- 语义已被会话指针占用且被消费（续接、B2 引擎守卫）
- `MEDIUMTEXT` 存 JSON 无法聚合查询（要做 `GROUP BY model` 得全表扫 + 应用层解析）
- 一个任务一行无法表达"多轮多模型"

### 4.3 聚合表 `obs_llm_rollup_day`（可选，P1）
数据量预估：307 个任务 → 日增几十~几百行/仓库，**量很小**，P0 直接对明细表 `GROUP BY` 即可。
只有当"7d/30d 全维度聚合"变慢时再加按日聚合表（维度：`date × model_key × repo_key × engine_id`）。

---

## 5. 模型归一化与定价

### 5.1 模型名归一化表 `obs_model_map`（**必须有**）

同一个模型各引擎写法不同：`claude-sonnet-4.5` / `claude-sonnet-4-5` / `anthropic/claude-sonnet-4.5` / `Claude Sonnet 4.5`。
不归一化的话，占比图会碎成几十条，成本也算不准。

```sql
CREATE TABLE IF NOT EXISTS obs_model_map (
    alias       TEXT PRIMARY KEY,     -- 引擎上报的原始名（小写归一后）
    model_key   TEXT NOT NULL,        -- 规范名，如 claude-sonnet-4.5
    provider    TEXT NOT NULL,        -- anthropic / openai / zhipu / ...
    display_name TEXT DEFAULT '',
    price_in          REAL DEFAULT NULL,  -- 元/百万 token，NULL = 未定价
    price_out         REAL DEFAULT NULL,
    price_cache_read  REAL DEFAULT NULL,
    price_cache_write REAL DEFAULT NULL,
    currency    TEXT DEFAULT 'CNY',
    updated_at  TEXT DEFAULT (datetime('now'))
);
```

**未命中 alias 时**：`model_key` 取原始名（小写去空格），并**在页面上提示"有 N 个模型未归一化"**，引导补映射——不静默归到"其他"。

### 5.2 成本口径

```
cost = input/1e6*price_in
     + output/1e6*price_out
     + cache_read/1e6*price_cache_read
     + cache_write/1e6*price_cache_write
     (+ reasoning 计入 output 还是单列，可配置；默认**并入 output**，因为多数厂商按 output 计价)
```

**未定价模型的处理（重要）**：`cost` 显示为 `—`，并在汇总处标注 **"成本仅含已定价的 N 个模型，另有 M 个模型未定价"**。
绝不能把未定价当 0 —— 那会让用户以为"这个月只花了 3 块钱"。

---

## 6. 展示设计

### 6.1 位置：**不新增侧栏项**

成本的关注节奏是"每天扫一眼"，属于总览级；明细维度多（模型/仓库/场景/引擎/时间），属于详情级。所以：

- **总览页**：新增一个「用量与成本」卡片区（2 张图 + 1 个 KPI）
- **点击展开**：「用量与成本」详情（**抽屉或子页，不占侧栏**），维度可切
- **任务详情**：显示该任务这一轮的 token 用量（把现在只有 codex 有的能力推广到所有引擎）

> 若日后觉得该给它一个侧栏位，加一行即可（可观测性分区从 3 变 4）。

### 6.2 总览新增图表

**⑧ Token 消耗趋势**（6 列）
- 图型：**堆叠柱 + 折线（双轴）**
- 堆叠柱：`input` / `output` / `cache_read+write`（三色）
- 折线（右轴）：**成本（元）**
- X：时间桶；hover 显示各分类 token + 该时段成本
- 点击柱 → 「用量」详情并带上时间窗

**⑨ 模型用量占比**（6 列）
- 图型：**横向条形**（不是饼图——饼图超过 5 类就无法比较）
- Y：`model_key`（Top 6 + 其他），X：token 总量；条上标注占比 %
- 副信息：该模型的成本
- 交互：点击 → 「用量」详情并筛该模型

**KPI 卡片（第 5 个）**：`本期 Token` 主值 + `≈ ¥xx.xx`，迷你 sparkline，较上一同时长涨跌（**涨标红**）。

### 6.3 「用量与成本」详情视图

```
[维度切换: 模型 | 仓库 | 场景 | 引擎 | 用户]   [时间: 1d|7d|30d|自定义]   [导出 CSV]
┌──────────────────────────────────────────────────────────────────────┐
│ 趋势图（按选中维度堆叠）                                                │
├──────────────────────────────────────────────────────────────────────┤
│ 汇总：总 token  ·  总成本  ·  请求数  ·  未定价模型提示                  │
├──────────────────────────────────────────────────────────────────────┤
│ 表格：维度值 | 请求数 | input | output | cache | 合计 | 占比 | 成本      │
│  （可排序，默认按成本降序；未定价行的成本列显示 —）                       │
└──────────────────────────────────────────────────────────────────────┘
```

### 6.4 任务详情内的用量
- 展示：`模型 · input N / output N / cache N · ≈¥x.xx`（多轮则分行或折叠汇总）
- 若该任务未采集到用量：显示 **"用量未知"**（灰色），**不显示 0** —— 0 与"未知"是两件事

---

## 7. 边界与安全

| 项 | 规则 |
|---|---|
| 可见范围 | 用户只看到**自己可见仓库/任务**的用量；过滤在 SQL 层（复用主文档 §5.3 的 `resolve_visible_scope`） |
| 平台总账 | **不在用户面**出现全平台 token 总量/总成本 → 归 admin 健康中心（L2） |
| 越权 | 按他人 repo 查用量 → 404 |
| 脱敏 | 不回显 prompt/response 内容，只回显数值与模型名（用量视图不涉及正文） |
| 定价可见性 | 单价对用户可见（含自建模型的自定义价），但不暴露平台的采购价/折扣 |

---

## 8. 分期

> **实施状态（2026-10-17 核实）**
> - ✅ **P0 全部完成**：`/usage` 追加+幂等、`obs_llm_usage` 表、四引擎账本适配器（codex 事件流 /
>   opencode DB 增量 / hermes DB 取差值 / hwcloud 日志）、Token KPI 与总览 Token 图、任务详情用量
>   （含"用量未知"态）。真机验证过（本机 + 某 worker worker）。
> - 🟡 **模型归一化**：`obs_llm_usage.model_key` 已按**轻量规则**归一（去 provider 前缀、大小写、
>   点/横线统一），**§5.1 的 `obs_model_map` 别名合并表尚未建**（真机只有 5 个来源、模型未碎成几十条，
>   暂不急）；`llm_usage.py` 里已留注释指向本表。
> - ⏸ **定价（P1 的 ⑦⑧ 成本部分）**：**用户 2026-10-17 明确"先不实现"** —— 保留 `cost_usd` 字段与
>   §5.2 口径设计，不建单价表、不算成本；未定价模型继续显示 `—`。**成本相关的 P2 ⑪⑫ 一并顺延**。
> - ⬜ **P1 余下**：「用量与成本」详情视图（维度切换 + 表格）、CSV 导出。

| 期 | 内容 |
|---|---|
| **P0** ✅ | ~~① 确认各引擎 usage 字段~~ **已完成（§3.1）**；② 改 `/usage` 端点：去掉 `usage_json` 写入、改追加 + 幂等 ③ 建 `obs_llm_usage` ④ 写**引擎账本适配器**（codex 事件流 / opencode DB / hermes DB 取差值 / hwcloud 日志或 ACP）并接上报 ⑤ 总览 ⑧⑨ 图 + Token KPI ⑥ 任务详情显示用量（含"用量未知"态） —— **全部完成** |
| **P1** | ⑦ `obs_model_map` 归一化（**未做，轻量归一已够用**）+ 内置常见模型单价（**⏸ 用户暂缓**） ⑧ 成本计算与"未定价"提示（**⏸**） ⑨「用量与成本」详情视图（维度切换 + 表格）⬜ ⑩ CSV 导出 ⬜ |
| **P2** | ⑪ 预算告警（月成本超阈值 → 复用告警中心）（**⏸ 随定价顺延**） ⑫ 按场景/流水线的成本归因（**⏸**） ⑬ 单价用户自定义 UI（**⏸**） |

---

## 9. 验收标准

1. 真实跑一个 codex 任务 + 一个 ACP 任务，**两者的 token 都能在总览 ⑧ 图里看到**（不再有"永远 0"）
2. 一个任务多轮多模型 → 明细表**多行**，总览数字为**累加**而非最后一轮覆盖
3. worker 重试同一 seq → 用量**不翻倍**（幂等生效）
4. 触发真调用 `/usage` 后，`agent_tasks.usage_json` 的 `session_id`/`engine` **不被冲掉**（回归验证 B2 守卫与多轮续接）
5. 未定价模型：成本列显示 `—`，汇总处出现"另有 N 个模型未定价"提示（**不是 0**）
6. 未采集到用量的任务，任务详情显示"用量未知"而非 0
7. 用户 A 无法查到用户 B 仓库的用量（404）
8. 同一模型 3 种写法（`claude-sonnet-4.5` 等）归一化后**合并为同一条**，占比不碎

---

## 10. 风险

| # | 风险 | 应对 |
|---|---|---|
| R1 | **某引擎压根不吐 usage**（私有/裁剪版） | 已核实：**ACP 协议不带计费 token**（§3.1），所以统一走"引擎本地账本"；对确实无账本的引擎（如 aishell 若查证无），用量显示"未知"而非 0，并在采集矩阵标注"不支持" |
| R3 | ~~累计值 vs 增量值语义不同~~ **已核实**（§3.1） | hermes `session_model_usage` 是**累计值**（取差值），codex / opencode / hwcloud 是**增量**（SUM）。同一套聚合代码不可通用——按引擎分支处理，并写单测钉死 |
| R6 | **上游引擎升级导致账本结构变化**（DB schema / 日志字段改名） | 适配器读取失败时"降级为无用量"而非报错崩任务；每次升级后跑一次用量对账（§9 验收 1） |
| R2 | 采集需改 worker → 老 worker 不上报 | 端点向后兼容；老 worker 静默无用量；升级链（已有自动升级）会自然补齐 |
| R4 | 模型单价频繁变动 | 单价表带 `updated_at`，页面标注"单价更新于 xx"；历史成本按当时单价快照算（P2） |
| R5 | 缓存 token 计费口径各家不同 | 分开记 cache_read / cache_write，不合并；单价格子独立填写 |

> R3 是最容易踩的坑：不同引擎对 `usage` 的语义不一样（有的报"本次增量"，有的报"累计到目前"）。抓原始流时必须把这点一起确认。