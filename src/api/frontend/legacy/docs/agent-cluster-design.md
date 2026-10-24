# Grape Agent 集群化设计方案（Agent Cluster Design）

> 版本：v0.1（设计稿）
> 日期：2026-08-12
> 目标读者：Grape 服务端/边端开发、运维
> 关联文档：`multi-runtime-instance-design.md`（多实例注册/路由）、`add-new-worker-guide.md`（Worker 接入）、`concurrency-design.md`（并发/串行槽）、`multi-issue-isolation-design.md`（Issue 隔离/worktree）、`skill-delivery-remote.md`（技能远程交付）、`runtime-admin-two-view-design.md`（Engines/Workers 管理）、`agent-runtime-event-tech-impl.md`（任务队列技术实现）

---

## 1. 背景与现状

### 1.1 需求

让 **VOD agent、issue agent、coding agent、qa agent、sre agent** 等各 Agent 具备**集群执行能力**：

- 一个 Agent 类型可绑定 **1..N 个 Engine 引擎**（引擎 = 某台 Worker 机器上注册的某个 runtime 实例，如 `worker-1 的 opencode`）；
- 任务到达时，调度器从绑定池中**选择空闲的引擎**执行；
- 引擎离线/繁忙时任务可等待或转移，Agent 不因单点故障而不可用。

### 1.2 现状盘点（2026-08-12 代码实测）

集群基础设施已有相当部分落地，本方案在其上补齐"Agent 维度集群化"。

| 能力 | 现状 | 位置 |
|------|------|------|
| Worker 边端进程 | 已实施：`discover → register → recover_orphans → claim 循环 → 执行 → report/complete`，仅外连 | `src/runtime/agent.py` |
| 服务端协议端点 | 已实施：`/api/runtime/register|heartbeat|claim|report|complete|fail`，`rta_` token 认证（sha256 存哈希） | `src/api/routes/runtime.py` |
| 任务队列 | 已实施：`agent_tasks` 30 列快照（prompt/cwd/token/env/skill_files/project_info/server_url/issue_session_id） | `src/runtime/service.py` |
| 原子抢单 + 串行槽 | 已实施：`claim()` 两步（SELECT 候选 + UPDATE 影响行数判定），按 `issue_session_id` 严格串行、跨 Issue 并行 | `src/runtime/service.py:209` |
| 执行模式 | 已实施：`direct / queue_local / queue_remote`（env `DEVOPS_EXECUTION_MODE` 或 meta `execution_mode`） | `src/runtime/service.py:13-42` |
| 技能远程交付 | 已实施：服务端 `_pack_skill_dir` 打包 → worker 落地 `.skills/` + 占位符替换 | `src/runtime/service.py:71` / `agent.py:74` |
| project-info 交付 | 已实施：服务端权威基线 → worker 落地 `.project-info/`（多 VM 一致） | `service.py:116` / `agent.py:88` |
| git worktree（coding/qa/sre） | 已实施：按 Issue 建 worktree + `GIT_ASKPASS` | `service.py:438` / `repogit.py` |
| 多实例注册键 | 设计已定：`UNIQUE(credential_id, name, scope, owner_id)`，每 Worker 每引擎一行 | `multi-runtime-instance-design.md §2/§3` |
| 一键安装 | 已实施：`/api/runtime/install` 脚本（稀疏拉取 + venv + opencode + systemd） | `add-new-worker-guide.md` |
| 异步回报落库 | 已实施：`_ingestion_loop` 攒批写库 | `service.py:493` |
| 管理端视图 | 已设计：Engines / Workers 两视图 | `runtime-admin-two-view-design.md` |

### 1.3 差距清单（本方案要解决的）

| # | 差距 | 现状后果 |
|---|------|---------|
| G1 | **Agent 只能绑单个 runtime**：`agent_type.default_runtime_id` 单值（现 5 个 Agent 全绑 id=1），`devops_agent_session.runtime_id` 也是单值 | 无法"1..N 引擎池"；单引擎故障该 Agent 即停摆；负载无法在多个 Worker 间分摊 |
| G2 | **无空闲选择调度**：`claim(runtime_id)` 只按单实例拉队列 | 引擎池内无法选"当前最空闲"的引擎执行 |
| G3 | **绑定无 Agent 维度视角**：绑定分散在 `agent_type.default_runtime_id` + `devops_agent_session.runtime_id`（repo 级）两层，无统一"Agent 类型 → 引擎池"配置面 | 管理端无法直观配置"coding 用哪几台 Worker" |
| G4 | **A2A 交办 / 任务中心 / 项目接入探测仍本地直连**（`webhook.py:197`、`tasks.py:202`、`onboarding.py:68`） | 这些入口不尊重 `execution_mode`，集群化后仍打回单机 |
| G5 | **会话续接跨 Worker 断裂**：`runtime_session_id`（opencode session）存在 Worker 本地，任务换引擎后 session 丢失 | 长任务（coding/qa 多轮）重路由会丢失上下文，只能重跑 |
| G6 | **无心跳超时任务回收的状态机**：`recover_orphans` 只在本机启动时清自己实例的 dispatched/running | Worker 宕机后任务永久卡在 running，无自动回收/重试 |
| G7 | **无池化调度观测**：看不到每个 Agent 的引擎池负载、队列深度、分布 | 无法做容量规划与排障 |
| G8 | **Worker 侧无并发上限语义**：`RUNTIME_MAX_CONCURRENT_TASKS` 只是边端进程内线程池参数，服务端不知 Worker 剩余槽位 | 调度"空闲"判定只能靠猜测（拉队列试探），无法精确选空闲 |

---

## 2. 目标与非目标

### 2.1 目标

1. **引擎池绑定**：任一 Agent 类型可绑定 1..N 个引擎实例（跨 Worker），管理端可视化配置。
2. **空闲调度**：任务入队后由调度器从池中选**健康 + 有空闲槽位 + 满足串行约束**的引擎执行；优先"最空闲"。
3. **容错**：引擎心跳超时 → 自动摘除；其上 dispatched/running 任务自动回收重排；单引擎/单 Worker 故障不中断 Agent 服务。
4. **多 Agent 差异化**：vod/issue（短任务、分析型）与 coding/qa（长任务、需 worktree）可配不同池与并发策略。
5. **兼容演进**：现有 `direct / queue_local / queue_remote` 模式、`rta_` 协议、`agent_tasks` 队列全部保留，集群化是"调度层的增强"而非推翻。

### 2.2 非目标（本期不做）

- 不引入 Redis/MQ/K8s：队列与调度仍基于 MySQL（共享 RDS 已具备，天然支持多节点并发抢单）。
- 不做引擎热迁移（任务执行中跨 Worker 迁移）：以"任务快照 + 会话亲和"保证连续性（见 §4.5）。
- 不做自动弹性扩缩容（按队列深度自动拉起云主机 Worker）：本期提供指标与手动扩容入口，自动扩缩列为 P2。
- 不改边端协议主体（`register/heartbeat/claim/report` body 兼容）。

---

## 3. 总体架构

```
                     ┌──────────────────────────────────────────────┐
                     │               Grape 服务端（控制面）            │
                     │  ┌───────────┐  ┌───────────┐  ┌───────────┐  │
   GitCode webhook ──▶│ 事件流水线   │─▶│  调度器     │─▶│ agent_tasks│  │
   飞书/WebChat  ───▶│ (pipeline)  │  │ Scheduler  │  │   队列     │  │
   A2A/任务中心 ────▶│            │  │ (引擎池选择)│  │ (MySQL)   │  │
                     │  └───────────┘  └───────────┘  └─────┬─────┘  │
                     │                                      │        │
                     │  ┌───────────────────────────────────▼─────┐  │
                     │  │ /api/runtime/* (register/heartbeat/claim│  │
                     │  │  /report/complete/fail)  rta_ 认证       │  │
                     │  └─────────────────────────────────────────┘  │
                     └──────────────────────┬───────────────────────┘
                                            │ HTTPS (仅外连)
        ┌───────────────────┬───────────────┼───────────────┬───────────────────┐
        ▼                   ▼               ▼               ▼                   ▼
   ┌─────────┐        ┌─────────┐      ┌─────────┐    ┌─────────┐         ┌─────────┐
   │ Worker-1│        │ Worker-2│      │ Worker-3│    │ Worker-4│   ...   │ Worker-N│
   │ runtime.│        │ runtime.│      │ runtime.│    │ runtime.│         │ runtime.│
   │ agent   │        │ agent   │      │ agent   │    │ agent   │         │ agent   │
   │ opencode│        │ opencode│      │ opencode│    │ opencode│         │ codearts│
   │ codearts│        │         │      │         │    │         │         │         │
   └─────────┘        └─────────┘      └─────────┘    └─────────┘         └─────────┘
    设备 A（NAT 后）     设备 B             设备 C          设备 D              设备 E

数据面：共享 MySQL RDS（agent_tasks / events / agent_runtimes / runtime_credentials / agent_type_runtime_pool）
控制面：仅 Grape 服务端；边端只做"注册-心跳-抢单-回报"四件事。
```

**关键原则**：
- 调度器在**服务端**，选引擎是一次**数据库原子操作**（抢单），天然支持服务端多实例部署（多 uvicorn 节点共享同一 RDS 也能正确调度）。
- 边端无状态化：Worker 不知道自己属于哪个池，只按 `runtime_id` 抢自己队列的任务。
- 池与调度只存在于服务端绑定表 + 调度器逻辑中。

---

## 4. 核心设计

### 4.1 绑定模型：Agent 类型 → 引擎池（1..N）

#### 4.1.1 新表 `agent_type_runtime_pool`

```
CREATE TABLE agent_type_runtime_pool (
  id            INT AUTO_INCREMENT PRIMARY KEY,
  agent_type    VARCHAR(64)  NOT NULL,          -- vod / issue / coding / qa / sre / chat
  runtime_id    INT          NOT NULL,          -- agent_runtimes.id（Worker 上的引擎实例）
  weight        INT          NOT NULL DEFAULT 1, -- 调度权重（≥1），加权随机/加权轮询用
  priority      INT          NOT NULL DEFAULT 5, -- 池内优先级（1 高），同槽位先取高优先
  enabled       TINYINT      NOT NULL DEFAULT 1, -- 软摘除（不删绑定，临时停用）
  remark        VARCHAR(255) DEFAULT '',
  created_at    DATETIME     NOT NULL,
  UNIQUE KEY uk_pool (agent_type, runtime_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
```

- **一行 = "某个 Agent 类型可用某个引擎"**。`agent_type` 与 `runtime_id` 为 N:M。
- 例：coding 绑 {worker-1/opencode(id=3), worker-2/opencode(id=5), worker-4/codearts(id=9)} → 三行。
- `agent_type.default_runtime_id` 保留为**兼容/兜底字段**（池为空时回退使用，见 §4.2.4）。

#### 4.1.2 兼容层

- 现有 `devops_agent_session.runtime_id`（repo 级显式绑定）**优先级最高**（显式 > 池 > 兜底），语义不变：某仓库某 Agent 明确绑死某引擎时不进池。
- `enqueue_task(runtime_id=...)` 现有调用全部兼容；新增 `enqueue_task(pool=agent_type)` 走池调度。

### 4.2 调度器：空闲引擎选择

#### 4.2.1 空闲判定（G8 解决）

引擎"空闲" = **在线 + 健康 + 未达并发上限 + 满足串行槽**。为让服务端精确判定，给 `agent_runtimes` 增加：

```
busy_slots      INT NOT NULL DEFAULT 0,   -- 当前 dispatched+running 任务数（服务端维护）
max_concurrent  INT NOT NULL DEFAULT 4,   -- 该引擎并发上限（注册/管理端可调；边端 RUNTIME_MAX_CONCURRENT_TASKS 应 ≤ 此值）
```

- `claim` 成功后 `busy_slots += 1`；`complete/fail/cancel`（或回收）时 `busy_slots -= 1`（`busy_slots` 与 `status` 更新同事务）。
- 心跳置 `status=online/last_seen_at`；超过 `RUNTIME_OFFLINE_AFTER`（默认 30s）未心跳 → 调度器视为 offline，跳过。

#### 4.2.2 选择算法（池内）

任务入队（`enqueue_task(pool=agent_type)`）时**不固定 runtime_id**，调度器在每次 claim 时实时选择。两种实现路径：

**路径 A（推荐，改 claim 为"池 claim"）**——服务端调度器线程（或 worker 抢单时带 pool 参数）执行：

```
候选 = SELECT r.* FROM agent_type_runtime_pool p
       JOIN agent_runtimes r ON r.id = p.runtime_id
       WHERE p.agent_type = ? AND p.enabled = 1
         AND r.status = 'online' AND r.healthy = 1
         AND r.busy_slots < r.max_concurrent
       ORDER BY (r.busy_slots / r.max_concurrent) ASC,   -- 最少占用率优先（最空闲）
                p.priority ASC, p.weight DESC, r.busy_slots ASC
       LIMIT 1
```

- **最少占用率优先** = 选当前最空闲的引擎；同占用率按池内 priority/weight 打平。
- 命中后对该 `runtime_id` 执行现有 `claim()`（原子抢单，两 worker 同时抢同一引擎时由 UPDATE 影响行数保证只有一个成功）。
- 串行约束在候选 SQL 中追加 NOT EXISTS 子查询（沿用现有串行槽逻辑，`issue_session_id` 维度）。

**路径 B（简化，零新线程）**——把池展开为多个"候选 claim"：worker 带 `pool` 请求时，服务端按候选顺序依次尝试 `claim(runtime_id)`，第一个抢到即返回。实现最简（复用现有端点），但空闲排序精度略低（按 busy_slots 排序仍可）。

> 建议：v1 先做**路径 B**（改动小、可复用现有 claim），v2 演进为**路径 A**（调度器主动选择 + 池内加权），两者对外行为一致。

#### 4.2.3 优先级与加权

- **池内 priority**：同 Agent 类型多引擎时，priority 高的引擎优先被选中（如"主力机"priority=1、"备用机"priority=9）。
- **weight**：负载相近时加权轮询/加权随机，把流量按机器能力（如 GPU/大内存机 weight=3）分配。
- **Agent 类型间**：不跨池抢资源——vod 池与 coding 池互不干扰（除非同一引擎被同时加入两个池，此时 busy_slots 全局计数自然限流）。

#### 4.2.4 兜底链

```
1) repo 级显式绑定（devops_agent_session.runtime_id ≠ 0 且在线） ──▶ 用该实例
2) 引擎池（agent_type_runtime_pool 有 enabled 且在线的候选）    ──▶ 池调度
3) agent_type.default_runtime_id（兼容旧配置）                 ──▶ 用该实例
4) 全空/全离线                                          ──▶ 任务保持 queued，等待；告警
```

- 池内全部繁忙：任务留在队列（queued），不失败；任一引擎释放槽位后自动被取走（天然背压）。
- 池内全部离线：任务 queued + 管理端告警"coding 引擎池离线"，不丢任务。

### 4.3 任务生命周期与状态机

```
                    ┌─────────┐
   enqueue ────────▶│ queued  │◀─────────── 回收重排（§4.3.3）
                    └────┬────┘
                         │ 池调度 claim（原子）
                         ▼
                    ┌───────────┐
                    │ dispatched│ ── 心跳期间由 Worker 执行
                    └─────┬─────┘
                          │ Worker 开始执行
                          ▼
                    ┌──────────┐
                    │ running  │ ── report_messages / progress 持续回报
                    └─────┬────┘
                          │ done/fail
              ┌───────────┼───────────┐
              ▼           ▼           ▼
        ┌─────────┐ ┌─────────┐ ┌─────────┐
        │completed│ │ failed  │ │cancelled│
        └─────────┘ └─────────┘ └─────────┘
   （终态幂等：update_task_status 已终态不再更新，重放安全）
```

#### 4.3.1 心跳与离线判定

- Worker 每 `RUNTIME_HEARTBEAT_INTERVAL`（默认 15s）心跳：`runtime_ids → status=online, last_seen_at=now`。
- 服务端懒判定：调度/回收时 `WHERE last_seen_at > NOW() - 30s` 视为在线；不引入常驻扫描线程（v1），管理端页面轮询时顺带标记。

#### 4.3.2 槽位计数一致性

`busy_slots` 的增减与状态更新同事务；`recover_orphans` 重排任务时同步 `busy_slots = dispatched+running 实际数`（自愈，防漂移）。

#### 4.3.3 故障回收（G6 解决）

- 触发条件：任务 `status IN (dispatched, running)` 且所属引擎 `last_seen_at` 超时（离线）且任务 `started_at` 超过 `TASK_STALE_AFTER`（默认 120s，避免误杀慢任务）。
- 动作：`dispatched → queued`（重排，attempts+=1）；`running → queued`（attempts+=1，result_text 保留前缀，追加"[已从离线引擎回收]"）。
- `attempts`（新列，默认 0）≥ `MAX_ATTEMPTS`（默认 3）→ 置 `failed`（"引擎离线且重试耗尽"），事件标记失败，不无限重排。
- 回收动作在**调度器线程**或**任意 claim 请求**中顺带执行（懒回收，无独立守护进程）。

#### 4.3.4 幂等与重放

- 终态幂等已有（`update_task_status`）；回收重排天然幂等（只有非终态才被重排）。
- Worker 侧：执行前 `claim` 成功后若进程崩溃，服务端回收重排后其他引擎重跑；重跑语义 = 同一 prompt 快照（任务快照在队列里），结果覆盖写入（events 表按 seq 幂等更新）。

### 4.4 串行语义与并发

- **同 Issue 严格串行**（沿用）：`issue_session_id` 相同 → 不并行；**跨 Issue 并行**；不同 Agent 类型（vod 与 qa 同 Issue）按 `issue_session_id` 共享串行约束（防同 Issue 并发写评论/冲突）。
- **引擎内并发**：`max_concurrent` 决定单引擎并行任务数；`busy_slots < max_concurrent` 才可被选中。
- **全局背压**：所有引擎槽位占满 → 队列排队，Web/IM 侧有"排队中"提示（`wait_task_terminal` 已有轮询，可加 queue 位置返回）。

### 4.5 会话连续性与工作区（G5 解决）

#### 4.5.1 会话亲和（不换引擎）

- 任务带 `runtime_session_id`（opencode session）时，**优先选择上次执行该 session 的引擎**（池候选排序加 `AND r.id = 上次runtime_id 优先`）。
- 实现：`agent_tasks` 已有 `runtime_session_id`；`devops_agent_session.runtime_session_id` 记录 repo 级续接指针。调度候选 SQL：`ORDER BY (r.id = ?上次引擎) DESC, ...`。
- 效果：多轮任务（vod 澄清 → 继续）默认留在同一 Worker，session 与工作区热续接。

#### 4.5.2 会话缺失降级

- 亲和引擎离线/满 → 允许换引擎：worker 落地技能/project-info/worktree 后**无 session 重跑**（`stream_agent` 已有 "Session not found → 无 session 重跑" 兜底），提示"上下文为新会话"。
- 长任务（coding 多轮实现）建议绑定单引擎（`devops_agent_session.runtime_id` 显式），避免中途换机。

#### 4.5.3 工作区（cwd）规范化

- 现状：`cwd` 是 Worker 本地路径（`data/workspaces/<owner>/<repo>/...`）。
- 集群化要求：同一仓库任务可能落在不同 Worker → **cwd 路径模板在 Worker 侧派生**（`<WORKSPACE_ROOT>/<owner>/<repo>/<issue>`），服务端任务只传相对路径 + repo_key；Worker 端统一 `RUNTIME_WORKSPACE`（默认 `data/workspaces`）。
- 仓库克隆/同步：首次任务前 `git clone`（`RUNTIME_REPO_CACHE` 缓存裸仓），之后 `fetch + worktree`（复用 `repogit.ensure_issue_worktree`，已支持 token 注入）。
- 清理：`runtime/cleanup.py` 已有，按 Worker 本地空闲回收旧 worktree/工作区。

### 4.6 技能与依赖交付

- 技能：沿用 `_pack_skill_dir` 打包随任务下发（已有，无需改）；增量优化（P2）：按技能 hash 缓存，未变不重复下发。
- project-info：沿用服务端权威基线随任务下发（已有）。
- 引擎依赖（CLI/模型 Key）：Worker 侧安装（`/api/runtime/install` 一键装 opencode；`HERMES_CUSTOM_API_*` 模型 Key 由运维在 Worker env 配置）。服务端只负责**校验**：register 时上报 CLI 版本/healthy，不健康引擎不参与调度。

### 4.7 安全

| 项 | 设计 |
|----|------|
| 边端认证 | 沿用 `rta_` token（服务端只存 sha256）；吊销 = `runtime_credentials.status → revoked`，心跳/抢单即 401，调度器跳过该凭据下所有引擎 |
| 池绑定权限 | 管理端 API 需登录 + 管理员；Worker 凭据无法改池配置（池是控制面数据） |
| 最小数据面 | Worker 只能 claim 自己 `runtime_id` 的任务（claim SQL 按 runtime_id 过滤，天然隔离）；任务快照含 repo token，仅随任务下发到绑定的 Worker |
| 网络 | 边端仅外连 HTTPS；服务端无需对 Worker 开放入站（NAT 后可用，已有） |
| 审计 | 池绑定变更写 `admin_audit_log`（沿用 admin 子系统约定）；任务回收/换引擎记入 task errors/result_text |

### 4.8 可观测与管理端（G7 解决）

- **Worker 视图**（已有两视图设计）：新增列 `busy_slots / max_concurrent / 池成员(agent_type 列表)`。
- **池视图**：按 agent_type 展示绑定引擎、各自 busy_slots、队列深度（`COUNT(*) WHERE status='queued' AND 归属池`）、最近任务分布。
- **指标**（`/api/runtime/stats`，管理端轮询）：每池 {queued, dispatched, running, failed_24h, avg_duration}；每引擎 {busy_slots, max_concurrent, last_seen, 成功率}。
- 告警（P2）：池全离线 / 队列深度 > 阈值 / 回收次数 > 阈值 → IM/邮件通知。

---

## 5. Agent 类型接入矩阵

| Agent | 任务特征 | 池策略建议 | worktree | 并发建议 | 亲和性 |
|-------|---------|-----------|----------|---------|--------|
| **vod** | 短任务（分钟级）、需求分析/澄清、读库读 API | 池绑定 2..N 台；`max_concurrent` 高（短任务吞吐优先） | 否 | 8~16 | 弱（换机代价小） |
| **issue** | 中短任务、Issue 处理/评论/标签 | 池绑定 2..N 台 | 否 | 4~8 | 中（同 Issue 续接） |
| **coding** | 长任务（10min+）、代码实现、多轮迭代 | **池绑定 1..N 台（核心场景）**；单引擎 `max_concurrent` 低（1~2，长任务串行稳妥） | **是** | 1~2/引擎 | **强（尽量不换机）** |
| **qa** | 中长任务、PR/Issue 审查、只读 + 评论 | 池绑定 2..N 台 | 是 | 2~4 | 中 |
| **sre** | 按需、环境类 | 池绑定 1..N 台 | 是 | 2 | 弱 |
| **chat** | 对话、短任务 | 复用池或独立池 | 否 | 4 | 弱 |

> 说明：矩阵为**默认建议**，全部可管理端按仓库/Agent 覆盖（`devops_agent_session.runtime_id` 显式绑定优先于池）。

---

## 6. 数据模型变更（DDL 汇总）

```sql
-- 1) 引擎池绑定（核心）
CREATE TABLE agent_type_runtime_pool (
  id INT AUTO_INCREMENT PRIMARY KEY,
  agent_type VARCHAR(64) NOT NULL,
  runtime_id INT NOT NULL,
  weight INT NOT NULL DEFAULT 1,
  priority INT NOT NULL DEFAULT 5,
  enabled TINYINT NOT NULL DEFAULT 1,
  remark VARCHAR(255) DEFAULT '',
  created_at DATETIME NOT NULL,
  UNIQUE KEY uk_pool (agent_type, runtime_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- 2) agent_runtimes 增列（槽位与在线判定）
ALTER TABLE agent_runtimes
  ADD COLUMN busy_slots INT NOT NULL DEFAULT 0,
  ADD COLUMN max_concurrent INT NOT NULL DEFAULT 4,
  ADD COLUMN last_seen_at DATETIME NULL;

-- 3) agent_tasks 增列（重试与池调度标记）
ALTER TABLE agent_tasks
  ADD COLUMN attempts INT NOT NULL DEFAULT 0,
  ADD COLUMN pool_agent_type VARCHAR(64) DEFAULT '',   -- 走池调度的任务标记；非空时 claim 按池选引擎
  ADD COLUMN affinity_runtime_id INT DEFAULT 0;        -- 会话亲和：上次执行引擎（0=无）

-- 4) agent_type 保留 default_runtime_id（兜底），语义不变
```

## 7. API 变更

| 端点 | 变更 |
|------|------|
| `POST /api/runtime/register` | 边端 body 增加可选 `max_concurrent`（上报本机并发上限）；服务端写入/更新 |
| `POST /api/runtime/claim` | body 增加可选 `pool`（agent_type）；服务端按池候选顺序尝试抢单（路径 B）；返回任务 + `runtime_id` |
| `POST /api/runtime/complete|fail` | 顺带 `busy_slots -= 1`（同事务） |
| `POST /api/runtime/heartbeat` | 顺带懒回收：本凭据引擎的过期任务重排（返回重排数，日志） |
| 管理端 `GET/PUT /api/admin/pools`（或复用 `/api/settings`） | 池 CRUD：`(agent_type, runtime_id, weight, priority, enabled)`；校验 runtime 存在且属于该凭据 |
| `GET /api/runtime/stats` | 池/引擎指标（§4.8） |

> 边端协议兼容：`register/claim` 的 body 字段全为**可选新增**，老 Worker 无需升级即可共存（不传 pool → 行为等同现状单实例 claim）。

## 8. 实施路线图

### M0：现状对齐（0.5 天）
- 验证 `queue_remote` 模式端到端（现有 Worker 123.249.12.67 或本机跑边端）。
- A2A 交办 / 任务中心 / 项目接入探测三条路径对齐 `execution_mode`（G4，`webhook.py:197` / `tasks.py:202` / `onboarding.py:68`）。

### M1：引擎池数据面（1~2 天）
- DDL：`agent_type_runtime_pool` + `agent_runtimes.busy_slots/max_concurrent/last_seen_at` + `agent_tasks.attempts/pool_agent_type/affinity_runtime_id`。
- `claim` 支持 `pool` 参数（路径 B 实现）；`complete/fail` 槽位释放；懒回收（heartbeat 顺带 + claim 顺带）。
- 测试：`test_runtime_pool.py`（池选引擎、并发抢单原子性、回收重排、attempts 耗尽）。

### M2：Agent 接入（1~2 天）
- `enqueue_task` 支持 `pool_agent_type`；事件流水线 / 飞书 / WebChat / A2A / 任务中心全部入队走池。
- 会话亲和：调度候选优先 `affinity_runtime_id`；`devops_agent_session.runtime_id` 显式绑定优先级高于池。
- cwd 路径模板化（Worker 侧派生，任务只传相对路径）。
- 测试：各 Agent 类型在 2 台 Worker 上的分派、同 Issue 串行、亲和续接。

### M3：管理端与观测（1~2 天）
- 池 CRUD API + Engines/Workers 视图扩列（busy_slots/max_concurrent/池成员）。
- `/api/runtime/stats` 指标 + 前端池视图。
- 一键安装脚本同步 `max_concurrent` 上报。

### M4：增强（P2，按需）
- 调度器线程（路径 A：主动选择 + 加权轮询/最少占用率）。
- 技能/依赖增量下发缓存；队列深度告警；自动扩缩容。

## 9. 风险与对策

| 风险 | 对策 |
|------|------|
| 任务换引擎丢 session 上下文 | 会话亲和优先 + 显式绑定长任务 + "无 session 重跑"兜底 + 提示文案 |
| 多 Worker 写共享 RDS 抢单竞争 | 现有 claim 两步法已原子；池候选 SQL 与 UPDATE 同事务；压测验证 |
| 槽位计数漂移（complete 丢失） | `recover_orphans` 重排时按实际 dispatched+running 重算 busy_slots；终态幂等 |
| 引擎假死（进程在、agent 卡死） | 心跳 + 任务级超时（`max_exec_seconds` 已有）+ 静默超时扩展上限（`AGENT_SILENT_EXTEND_CAP` 已有）；回收仅针对"心跳也断了"的引擎，假死靠任务超时兜底 |
| 池配置错误导致任务堆积 | 兜底链（显式 > 池 > default_runtime_id）+ 池离线告警 + 管理端一键软摘除（enabled=0） |
| 老 Worker 不升级 | 协议字段全可选，老 Worker 行为等同现状；管理端提示升级 |

## 10. 验收标准

1. **池绑定**：管理端可将 coding 绑定 2 台 Worker 的 opencode 引擎（池内 2 行），vod 绑定另外 2 台；保存后任务路由正确。
2. **空闲调度**：coding 池 2 引擎，一台 `busy_slots=2/2`（满）另一台 `0/2`（空）→ 新任务落在空闲引擎；两引擎都满 → 任务 queued 不失败，释放槽位后自动执行。
3. **容错**：执行中 kill 掉 Worker 进程（心跳断）→ 120s 后任务自动重排到池内另一引擎并成功完成；`attempts` 达上限后任务 failed 且事件有明确失败原因。
4. **串行保持**：同 Issue 两个事件（如评论 + 更新）不并行（即使池内 2 引擎），跨 Issue 并行。
5. **会话续接**：同 Issue 第二轮任务默认回到上次引擎（亲和），`runtime_session_id` 续接有效；亲和引擎离线时降级新会话可完成。
6. **兼容**：`direct` 模式行为不变；无池配置的 Agent 走 `default_runtime_id` 兜底；老 Worker（不传新字段）可正常共存。
7. **全量回归**：`pytest tests/ -q` 通过（新增 §11 测试文件 + 既有 ~577 用例），MySQL 后端下 `test_runtime_pool.py` 全绿。

---

## 11. 测试用例设计

> 目的：在实施前把集群能力的正确性边界钉死，防止"能跑但选错引擎/丢任务/重复执行"类问题上线。
> 分层：单元（纯逻辑/选择算法）→ 集成（DB 层：claim/回收/槽位）→ API（管理端）→ 端到端（双 Worker 模拟，门控）。

### 11.1 测试文件与约定

| 文件 | 覆盖 | 阶段 |
|------|------|------|
| `tests/test_runtime_pool.py` | 池绑定 CRUD、空闲调度选择、槽位计数、兜底链、兼容性 | M1 |
| `tests/test_runtime_failover.py` | 心跳超时回收、attempts 上限、懒回收、幂等 | M1 |
| `tests/test_runtime_affinity.py` | 会话亲和、显式绑定覆盖、续接回写 | M2 |
| `tests/test_runtime_pool_api.py` | 池 CRUD API、权限、stats 指标 | M3 |
| `tests/test_runtime_pool_e2e.py` | 双 Worker 模拟端到端（`DEVOPS_E2E=1` 门控） | M2 |

约定（沿用既有测试规范）：
- `conftest.py` 已强制 `DEVOPS_DB_BACKEND=sqlite`，测试**绝不连 RDS**；跑单文件前 `rm -f /tmp/grape-test.db`。
- fixture：autouse 清理 `agent_tasks` / `agent_type_runtime_pool` / `agent_runtimes`（agent_runtimes 直接 INSERT 模拟引擎行，不依赖真实 Worker）。
- 模拟引擎行最小字段：`id, name, credential_id, status(online/offline), healthy(0/1), busy_slots, max_concurrent, last_seen_at`。
- 并发原子性用例用 `threading` 多线程同时 `claim`，断言"每任务恰被 claim 一次"。

### 11.2 用例明细

#### T1 引擎池绑定（test_runtime_pool.py）

| # | 用例 | 前置 | 步骤 | 期望 |
|---|------|------|------|------|
| T1.1 | test_pool_add_binding | 空池 | add_binding(coding, runtime_id=3, weight=1, priority=5) | 行存在，字段正确 |
| T1.2 | test_pool_duplicate_binding_rejected | 已有 (coding,3) | 重复 add_binding(coding,3) | 幂等 upsert 或明确报错，不产生重复行 |
| T1.3 | test_pool_disable_skips_in_schedule | 池 2 行，A enabled=1, B enabled=0 | claim(pool=coding) | 只可能选中 A，B 永不命中 |
| T1.4 | test_pool_remove_binding | 池 1 行 | remove_binding(coding,3) | 行删除；再 claim 走兜底链 |
| T1.5 | test_pool_rejects_unknown_runtime | — | add_binding(coding, runtime_id=99999) | 拒绝（校验 runtime 存在） |
| T1.6 | test_pool_multi_engine_one_agent | — | coding 绑 3 个不同引擎 | 3 行；claim 可在三者间选择 |

#### T2 空闲调度选择（test_runtime_pool.py）

| # | 用例 | 前置 | 步骤 | 期望 |
|---|------|------|------|------|
| T2.1 | test_claim_picks_idle_engine | 池 A(2/2 满) + B(0/2 空) | 入队 1 任务，claim(pool=coding) | 任务被 B 领取（B.runtime_id） |
| T2.2 | test_claim_skips_offline_engine | A offline(last_seen 超时), B online | claim(pool=coding) | 选 B；A 永不命中 |
| T2.3 | test_claim_skips_unhealthy_engine | A healthy=0, B healthy=1 | claim(pool=coding) | 选 B |
| T2.4 | test_claim_least_loaded_wins | A 1/4, B 2/4 | claim(pool=coding) | 选 A（占用率低优先） |
| T2.5 | test_claim_priority_breaks_tie | A/B 均 0/4；A priority=1, B priority=9 | claim(pool=coding) | 选 A |
| T2.6 | test_claim_weight_distribution | A/B 均 0/4 同 priority；weight 3 vs 1 | 循环 claim 多任务 | 统计上 A 命中次数 > B（宽松断言：两者都出现过） |
| T2.7 | test_claim_all_busy_keeps_queued | A 2/2, B 2/2 | claim(pool=coding) | 返回 None；任务仍 queued（不失败不丢失） |
| T2.8 | test_claim_all_offline_keeps_queued | A/B 均 offline | claim(pool=coding) | None + queued；管理端应有告警数据 |
| T2.9 | test_claim_serial_slot_across_engines | 同 issue_session_id 任务 T1 已被 A 领取(running) | 入队 T2(同 Issue)，claim(pool) | 即使 B 空闲也不领取 T2（跨引擎串行） |
| T2.10 | test_claim_parallel_across_issues | 两个不同 issue_session_id 的任务 | 两线程 claim(pool) | 两个任务被（可能不同的）引擎领取，并行 |
| T2.11 | test_claim_no_pool_param_legacy | 任务不带 pool_agent_type | claim(runtime_id=3) | 行为与现状一致（回归锚点） |

#### T3 槽位计数一致性（test_runtime_pool.py / test_runtime_failover.py）

| # | 用例 | 前置 | 步骤 | 期望 |
|---|------|------|------|------|
| T3.1 | test_busy_slots_increment_on_claim | A busy_slots=0 | claim 成功 1 任务 | A.busy_slots == 1 |
| T3.2 | test_busy_slots_decrement_on_complete | A.busy_slots=1 | complete_task | A.busy_slots == 0 |
| T3.3 | test_busy_slots_decrement_on_fail | A.busy_slots=1 | fail_task | A.busy_slots == 0 |
| T3.4 | test_busy_slots_recompute_on_recover | busy_slots=5 但实际 dispatched+running=2 | recover_orphans | busy_slots == 2（自愈防漂移） |
| T3.5 | test_busy_slots_never_negative | busy_slots=0 | 异常路径 complete 两次 | 不为负（clamp 0） |

#### T4 故障回收（test_runtime_failover.py）

| # | 用例 | 前置 | 步骤 | 期望 |
|---|------|------|------|------|
| T4.1 | test_recover_expired_dispatched | 任务 dispatched，引擎离线，started_at 超 TASK_STALE_AFTER | 触发回收 | 任务 → queued，attempts=1 |
| T4.2 | test_recover_running_task | 任务 running，引擎离线超时 | 触发回收 | → queued，attempts=1 |
| T4.3 | test_not_recover_recent_task | started_at 未超 TASK_STALE_AFTER | 触发回收 | 不回收（防误杀慢任务） |
| T4.4 | test_not_recover_online_engine | 引擎 last_seen 在线 | 触发回收 | 不回收 |
| T4.5 | test_attempts_exhausted_marks_failed | attempts=MAX_ATTEMPTS | 再次回收 | → failed，errors 含"引擎离线且重试耗尽"，events 终态 failed |
| T4.6 | test_recover_idempotent | 任务已被回收为 queued | 重复触发回收 | 状态不再变化（终态幂等） |
| T4.7 | test_lazy_recover_on_heartbeat | 有过期 dispatched 任务 | 该引擎心跳请求 | 心跳响应顺带重排；任务 queued |
| T4.8 | test_recover_preserves_result_prefix | running 任务已有 result_text 前缀 | 回收 | result_text 保留并追加"[已从离线引擎回收]"标记 |
| T4.9 | test_recover_after_requeue_can_complete | 任务被回收重排后 | 另一引擎 claim → 执行完成 | events 终态 completed（换机可完成） |

#### T5 会话亲和（test_runtime_affinity.py）

| # | 用例 | 前置 | 步骤 | 期望 |
|---|------|------|------|------|
| T5.1 | test_affinity_prefers_last_engine | 任务 affinity_runtime_id=B；B 在线有空位，A 更空闲 | claim(pool=coding) | 选 B（亲和优先于最空闲） |
| T5.2 | test_affinity_offline_falls_back | affinity=B 但 B 离线 | claim(pool=coding) | 选池内其他引擎（降级新会话可执行） |
| T5.3 | test_affinity_written_on_complete | 任务由 B 完成，usage.session_id 有值 | complete_task | devops_agent_session.runtime_session_id 回写；下一轮 affinity=B |
| T5.4 | test_no_affinity_uses_pool | affinity_runtime_id=0 | claim(pool) | 纯池调度（无偏好） |
| T5.5 | test_explicit_session_binding_overrides_pool | devops_agent_session.runtime_id=9（显式）且 9 不在池 | 入队 + claim | 只路由到 9，不进池（显式 > 池） |

#### T6 兜底链（test_runtime_pool.py）

| # | 用例 | 前置 | 步骤 | 期望 |
|---|------|------|------|------|
| T6.1 | test_fallback_default_runtime | 池为空；agent_type.default_runtime_id=3 | 入队 + claim | 路由到 3 |
| T6.2 | test_fallback_order | 显式绑定=9、池有 3/5、default=3 | 构造三层 | 选择顺序：9 > 池 > 3 |
| T6.3 | test_no_pool_no_default_queued | 池空且 default=0 | 入队 | 任务 queued 不炸；无引擎可选时等待 |

#### T7 兼容性（test_runtime_pool.py / test_m2_execution_mode.py）

| # | 用例 | 前置 | 步骤 | 期望 |
|---|------|------|------|------|
| T7.1 | test_legacy_claim_signature | — | claim(runtime_id) 不带 pool | 返回该引擎任务（现状语义） |
| T7.2 | test_direct_mode_unchanged | DEVOPS_EXECUTION_MODE=direct | 全流程 | 就地 stream_agent，不入队（既有用例保持绿） |
| T7.3 | test_register_without_max_concurrent | 老 Worker 不传新字段 | register | 默认 max_concurrent 生效，不报错 |
| T7.4 | test_register_with_max_concurrent | 新 Worker 上报 max_concurrent=8 | register | agent_runtimes.max_concurrent==8 |
| T7.5 | test_pool_engine_credential_scope | 池绑定某凭据的引擎 | 该凭据吊销 | 其下引擎被跳过（不参与调度） |

#### T8 管理端 API（test_runtime_pool_api.py）

| # | 用例 | 前置 | 步骤 | 期望 |
|---|------|------|------|------|
| T8.1 | test_pool_api_crud | 管理员登录（mock） | PUT 池绑定 → GET 列表 → DELETE | 增查删闭环 |
| T8.2 | test_pool_api_requires_admin | 普通用户 | 写池 API | 403 |
| T8.3 | test_pool_api_rejects_bad_agent_type | 管理员 | 绑定未知 agent_type | 400 |
| T8.4 | test_stats_shape | 池 + 任务若干 | GET /api/runtime/stats | 每池含 queued/running/引擎 busy_slots/max_concurrent；字段齐全 |

#### T9 端到端（test_runtime_pool_e2e.py，`DEVOPS_E2E=1` 门控）

| # | 用例 | 前置 | 步骤 | 期望 |
|---|------|------|------|------|
| T9.1 | test_two_workers_distribute_no_dup | 池 A/B 两引擎；fake worker 各自循环 claim(pool) | 入队 10 任务 | 10 任务全完成、无重复执行（每 task 恰好一个引擎完成）、两引擎都有工作量 |
| T9.2 | test_worker_dies_task_moves | A 领取任务后"崩溃"（停止 claim） | 超时回收 → B 领取 → 完成 | events 终态 completed，attempts==1，result 有效 |
| T9.3 | test_serial_across_engines_e2e | 同 Issue 两事件 | 两引擎同时 claim | 两个任务完成时间不重叠（串行） |
| T9.4 | test_full_pool_backpressure | 池全部 busy | 连续入队 | 队列深度增长；释放后自动消化，无失败 |

#### C 并发/原子性专项（并入 T2/T3 文件，threading）

| # | 用例 | 步骤 | 期望 |
|---|------|------|------|
| C1 | test_concurrent_claim_atomic | 10 线程同时 claim 同一池 10 个任务 | 每任务恰被 claim 一次（无重复、无丢失） |
| C2 | test_concurrent_complete_slot_consistent | 多线程并发 complete/fail 后 | busy_slots == 终态任务数（无漂移） |
| C3 | test_concurrent_pool_edit_during_claim | claim 进行中同时改池绑定 | 不崩溃；后续 claim 按新绑定生效 |

### 11.3 运行方式与回归看护

```bash
# 新增用例（M1 起每次合入前必跑）
rm -f /tmp/grape-test.db
DEVOPS_DB_PATH=/tmp/grape-test.db .venv/bin/python -m pytest \
  tests/test_runtime_pool.py tests/test_runtime_failover.py \
  tests/test_runtime_affinity.py tests/test_runtime_pool_api.py -q

# 全量回归（合入前必跑）
rm -f /tmp/grape-test.db
DEVOPS_DB_PATH=/tmp/grape-test.db .venv/bin/python -m pytest tests/ -q --ignore=tests/ui_scenario_test.py

# E2E（可选，需双 Worker 环境）
DEVOPS_E2E=1 DEVOPS_DB_PATH=/tmp/grape-e2e.db .venv/bin/python -m pytest tests/test_runtime_pool_e2e.py -q
```

看护约定（沿用既有规范）：
- **复用测试库导致误报**：全量跑前必须删测试 DB；单用例失败先隔离重跑（新 DB）再判回归。
- **已知环境失败**（非回归，保留清单）：`test_agent_executor.py::test_graceful_then_kill`（fake pid 不存在）、`test_api_routes.py::test_host_strict_mode_check`（需真实 GitCode token）、`tests/ui_scenario_test.py`（SSH 部署机，门控）。
- **conftest 强制 sqlite**：集群用例同样不得连 RDS；MySQL 后端行为差异（如 `last_seen_at` 时间函数）在 `test_db_backend_switch.py` 的 canonical 逻辑测试中覆盖，不在业务用例里连真库。

### 11.4 验收门槛

- M1 合入门槛：T1/T2/T3/T4 + C1/C2 全绿（含既有 runtime 用例回归）。
- M2 合入门槛：T5/T6/T9 全绿；A2A/任务中心/接入探测三条路径入队化用例补齐。
- M3 合入门槛：T7/T8 全绿；管理端手工验收（池配置 → 调度 → 故障 → 恢复全流程走查）。
- 任一阶段：全量 `pytest tests/ -q` 通过（已知环境失败清单除外）。
