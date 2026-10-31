# Grape 系统三进程拆分详细设计文档

> **版本**：v1.0  
> **日期**：2026-10-31  
> **基础仓**：`/home/grape/grape-new-version2`  
> **前置文档**：`grape-three-dimension-analysis.md`（三维度分析报告）

---

## 目录

1. [概述](#1-概述)
2. [进程定义](#2-进程定义)
3. [P0 — _execute_agent 迁移](#3-p0--_execute_agent-迁移)
4. [P1 — 进程拆分](#4-p1--进程拆分)
5. [P2 — sweep_loop 拆分](#5-p2--sweep_loop-拆分)
6. [P3 — 定时任务归属](#6-p3--定时任务归属)
7. [共享 DB 表协作](#7-共享-db-表协作)
8. [nginx 反代配置](#8-nginx-反代配置)
9. [启动/部署脚本](#9-启动部署脚本)
10. [风险与缓解](#10-风险与缓解)
11. [验证清单](#11-验证清单)

---

## 1. 概述

### 1.1 三维度定义

| 维度 | 名称 | 进程 | 核心职责 |
|------|------|------|----------|
| **A** | 工作台（用户面向） | `grape_server.py` (:38080) | 用户交互、配置管理、数据展示、IM 集成、可观测性面板 |
| **B** | 事件处理 | `grape_event_server.py` (:38081) | Webhook 接收、事件归一化、路由判定、任务入队（不直接执行） |
| **C** | Worker 执行态 | `grape_worker_server.py` (:38082) | 任务认领、Agent 执行（direct + remote）、心跳、结果回传、引擎健康 |

### 1.2 拆分目标

1. **故障隔离**：单进程宕机不影响其他进程。Webhook 事件积压不阻塞用户面；Worker 执行卡死不影响事件入队。
2. **独立扩缩容**：B 进程可针对 webhook 高并发独立扩 worker 数；C 进程可按引擎负载调整线程池大小。
3. **职责清晰**：`event_pipeline.py` 5000+ 行巨型文件按维度拆解，B 只管入队、C 只管执行。
4. **运维可观测**：三进程独立健康检查、独立日志、独立重启策略。

### 1.3 设计原则

| 原则 | 说明 |
|------|------|
| **共库** | 三进程共享同一个 SQLite/MySQL 数据库，不拆库。通过 `db_lock`（SQLite）或行级锁（MySQL）保证并发安全。 |
| **DB 驱动协作** | 进程间通信通过 DB 表状态流转完成（B 写 `agent_tasks.queued` → C `claim` 抢单 → C `complete` 回写），不引入额外 RPC。 |
| **不引入消息中间件** | 不使用 Redis Stream / RabbitMQ。`agent_tasks` 表即任务队列，`claim_by_tag` 即消费端抢单。保持部署简单（单机零外部依赖）。 |
| **渐进式拆分** | P0 先迁移 `_execute_agent`（最小改动、最大解耦），再 P1 拆进程入口，最后 P2/P3 归属定时任务。 |

---

## 2. 进程定义

### 2.1 进程 A — 工作台（grape_server.py）

| 属性 | 值 |
|------|-----|
| **进程名称** | grape-server |
| **入口文件** | `src/api/grape_server.py`（新建） |
| **监听端口** | 38080 |
| **uvicorn workers** | `--workers 2`（用户面 IO 密集，2 worker 足够；IM WS 长连接需 sticky） |

#### 加载的路由文件清单（25 个用户面路由 + admin + git-repo）

| # | 路由文件 | prefix | 职责 |
|---|---------|--------|------|
| 1 | `auth.py` | `/api/auth` | JWT 刷新、用户信息、登出 |
| 2 | `grape_auth.py` | `/api/auth` | 注册/登录/OAuth 回调 |
| 3 | `repos.py` | `/api/repos` | 仓库托管、Bot Token 管理 |
| 4 | `dashboard.py` | `/api/dashboard` | 运维仪表盘、仓库面板 |
| 5 | `agents.py` | `/api/agents` | Agent 配置查看与修改 |
| 6 | `chat.py` | `/api/chat` | 聊天对话、SSE 流式发送 |
| 7 | `settings.py` | `/api/settings` | 凭据管理、API Key、引擎设备列表 |
| 8 | `skills.py` | `/api/skills` | 技能市场管理 |
| 9 | `search.py` | `/api/search` | 全局搜索 |
| 10 | `pipeline.py` | `/api/pipeline` | 仓库流水线展示 |
| 11 | `scenes.py` | `/api/scenes` | 场景管理、场景市场 |
| 12 | `templates.py` | `/api/project-templates` | 项目模板 |
| 13 | `issues.py` | `/api/issues` | Issue 生命周期、需求中心 |
| 14 | `feishu_onboard.py` | `/api/feishu/onboard` | 飞书扫码入驻 |
| 15 | `feishu_card.py` | `/api/feishu/card-action` | 飞书卡片回调 |
| 16 | `task_logs.py` | `/api/task-logs` | 任务日志查看、SSE 流 |
| 17 | `knowledge.py` | `/api/knowledge` | 知识库 CRUD |
| 18 | `health.py` | (无 prefix) | 健康检查 |
| 19 | `devstage.py` | `/api/devstage` | DevStage 开发环境 |
| 20 | `latency.py` | `/api/latency` | 前端延迟上报 |
| 21 | `feedback.py` | `/api` | 用户反馈 |
| 22 | `autopilot.py` | `/api/autopilot` | Autopilot 定时任务管理（用户面 CRUD） |
| 23 | `orgs.py` | `/api/orgs` | 组织管理 |
| 24 | `community.py` | `/api/community` | 社区帖子、评论 |
| 25 | `observability.py` | `/api/obs` | 可观测性面板、告警规则、SLO |
| — | `admin/routes.py` | `/api/admin` | 管理后台 |
| — | `git_repo_api.py` | `/api/git-repo` | Git 仓库统一接口 |

> **注**：`events.py` 的 GET `/detail/{seq}`（查看事件详情）归 A；terminate/redispatch 操作端点也归 A（用户面操作）。但 `events.py` 中的重派逻辑会调用 `run_pipeline`，此时通过进程内函数调用 B 的 event_pipeline 模块（B 模块作为共享库加载，不经过 HTTP）。

> **注**：`runtime.py` 的 GET 端点（`/admin/machine-health`、`/runtimes/{id}/tasks`、`/tasks/{id}/status`、`/install` 等）归 A；POST 端点（`/register`、`/heartbeat`、`/tasks/claim` 等）归 C。需要在路由文件内按端点拆分或通过 `include_router` + 路径过滤。

#### 加载的定时任务清单

| 定时任务 | 频率 | 模块 |
|---------|------|------|
| `_lease_sweep_loop` | 循环 | `bot_manager.lease_sweep_loop()` |
| `_knowledge_sync_loop` | 每小时 | `src.api.knowledge.knowledge_sync_loop` |
| `knowledge_write_worker` | 持续 | `src.api.knowledge.knowledge_write_worker` |
| `start_skill_sync_scheduler` | 30min | `src.storage.skill_sync` |
| `start_feedback_reconcile_scheduler` | 定时 | `src.api.feedback_issue_converter` |
| `start_upload_cleanup_scheduler` | 每小时 | `src.storage.upload_cleanup` |
| `_table_maintenance_worker`（task_messages 部分） | 6h | `src.runtime.table_maintenance.cleanup_old_task_messages` |
| `start_cooldown_scheduler` | 每天 | `src.admin.cooldown` |
| `_active_user_tracker` | 30s | 自定义（`src.api.deps` + `src.user.db`） |
| `_server_metrics_loop` | 15s | 自定义（psutil 采集写入 `runtime_credentials`） |
| `_obs_alert_loop` | 30s | `src.observability.alerts` |
| `_obs_rollup_loop` | 60s | `src.observability.metric_rollup` |
| `_obs_sample_loop` | 60s | `src.observability.rollup` |
| `install_master_logging` | 持续 | `src.observability.logs` |
| `_knowledge_migrate_wrapper` | 一次性 | `src.api.db_setup._migrate_knowledge` |
| `D32 migration` | 一次性 | `scripts.migrate_user_accounts_to_grape` |

#### 独占的表

`grape_accounts`, `external_identities`, `verification_codes`, `signup_whitelist`, `login_whitelist`, `login_failures`, `exchange_codes`, `binding_tickets`, `audit_logs`, `grape_t_credential_reveal_log`, `session_api_tokens`, `api_tokens`, `chat_conversations`, `chat_messages`, `vod_conversations`, `feishu_onboard_task`, `global_im_bot_config`, `grape_im_instance`, `bot_seen_messages`, `community_posts`, `community_comments`, `community_post_likes`, `user_feedback`, `uploaded_files`, `knowledge_write_queue`, `organization`, `org_member`, `org_invitation`, `scene_market`, `scene_category`, `scene_likes`, `skill`, `skill_category`, `skill_likes`, `scene_skill_ref`, `featured_templates`, `api_latency_log`, `autopilot_tasks`, `autopilot_executions`, `autopilot_subscriptions`, `devstage` 相关, `obs_alert_rules`, `obs_alert_events`, `obs_llm_usage`, `obs_llm_usage_archive`, `obs_runtime_samples`, `obs_metric_rollup`, `obs_slo`, `obs_slo_snapshots`, `obs_log`

#### 共享的表

| 表 | 权限 | 说明 |
|----|------|------|
| `managed_repos` | 读写 | A 写入配置（host/bot-token/deploy-url），B/C 读取 |
| `agent_runtimes` | 读写 | A 注册引擎、刷新技能；C 更新在线态/健康度 |
| `runtime_credentials` | 读写 | A 注册设备；C 心跳更新；A 采集指标更新 |
| `agent_tasks` | 只读 | A 查看 task 列表、状态、cancel 操作 |
| `events` | 只读 | A 查看事件详情、terminate/redispatch |
| `task_messages` | 只读 | A 查看任务消息日志 |
| `issue_state` | 读写 | A 用户操作状态流转（confirm-done/cancel） |
| `devops_agent_session` | 读写 | A 修改引擎/技能绑定 |
| `devops_pipeline_scene` | 读写 | A CRUD 场景配置 |
| `distributed_locks` | 读写 | 分布式锁（table_maintenance、feedback_reconcile 等） |
| `issue` | 读写 | A 查看/操作 Issue |
| `issue_agent_session` | 只读 | A 查看 Issue 会话 |
| `event_active_guard` | 读写 | A 的 `_active_user_tracker` 清理 stale guard |
| `engine_capabilities` | 只读 | A 引擎列表展示 |
| `agent_type_runtime_pool` | 只读 | A 引擎池配置展示 |

---

### 2.2 进程 B — 事件处理（grape_event_server.py）

| 属性 | 值 |
|------|-----|
| **进程名称** | grape-event-server |
| **入口文件** | `src/api/grape_event_server.py`（新建） |
| **监听端口** | 38081 |
| **uvicorn workers** | `--workers 1`（单 worker 足够，event_pipeline 本身用线程池并行处理；多 worker 会导致 `_DIRECT_FUTURES` 内存不共享——P0 迁移后此问题消除） |

#### 加载的路由文件清单

| # | 路由文件 | prefix | 职责 |
|---|---------|--------|------|
| 1 | `webhook.py` | (无 prefix) | Webhook 接收（`/webhook/gitcode`、`/webhook/github`）、A2A 交接（`/api/a2a`、`/api/a2a/status`） |
| 2 | `events.py`（操作端点） | `/api/events` | `POST /{seq}/terminate`、`POST /{seq}/redispatch`（重派调用 `run_pipeline`） |

> **注**：`events.py` 的 GET `/detail/{seq}` 归 A 进程；terminate/redispatch 归 B（因为它们直接操作事件处理层数据并触发 pipeline 重派）。可通过在 B 进程中只 include 操作端点实现。

> **注**：`event_pipeline.py` 作为共享库被 B 进程 import，但 B 只调用 `run_pipeline`（入队逻辑），不调用 `_execute_agent`（P0 迁移后已移出）。

#### 加载的定时任务清单

| 定时任务 | 频率 | 模块 |
|---------|------|------|
| `sweep_events_loop`（拆分后） | 15min | `src.runtime.cleanup.sweep_stuck_events_once` |
| `start_bot_token_health` | 5min | `src.runtime.bot_token_health` |

#### 独占的表

`events`, `event_skipped`, `event_active_guard`, `event_thread`, `event_action_filter`, `event_route_config`, `issue`, `issue_state`（B 写入阶段）, `pipeline_instances`, `issue_agent_session`

#### 共享的表

| 表 | 权限 | 说明 |
|----|------|------|
| `agent_tasks` | 读写 | B 通过 `enqueue_task()` 写入 `queued` |
| `managed_repos` | 只读 | B 校验 webhook token、读取 bot_username/deploy_url |
| `devops_agent_session` | 只读 | B 读取 runtime_id/skill/skill_id 路由判定 |
| `devops_pipeline_scene` | 只读 | B 读取场景 nodes/work_branch/skill |
| `agent_runtimes` | 只读 | B 的 `pick_engine` 读取 online+healthy 引擎 |
| `runtime_credentials` | 只读 | B 读取引擎凭据信息 |
| `distributed_locks` | 读写 | 分布式锁（bot_token_health） |
| `engine_capabilities` | 只读 | B 引擎选择查询标签能力 |
| `agent_type_runtime_pool` | 只读 | B 引擎池查询 |

---

### 2.3 进程 C — Worker 执行态（grape_worker_server.py）

| 属性 | 值 |
|------|-----|
| **进程名称** | grape-worker-server |
| **入口文件** | `src/api/grape_worker_server.py`（新建） |
| **监听端口** | 38082 |
| **uvicorn workers** | `--workers 1`（单 worker + 内部 `_DIRECT_POOL` 线程池 max 20 并发执行） |

#### 加载的路由文件清单

| # | 路由文件 | prefix | 职责 |
|---|---------|--------|------|
| 1 | `runtime.py`（POST 端点） | `/api/runtime` | Worker 注册、心跳、任务认领、执行回调、结果上报 |
| 2 | `tasks.py` | `/api/tasks` | Agent 自报完成、通知、当前任务自省 |

> **注**：`runtime.py` 需拆分——POST 端点（register/heartbeat/claim/start/complete/fail/requeue/cancel）归 C；GET 端点（machine-health/tasks 列表/install 脚本分发）归 A。拆分方式：将 `runtime.py` 的 GET 端点提取到 `runtime_view.py`（A 加载），POST 端点保留在 `runtime.py`（C 加载）。或者通过两个 APIRouter 实例分别 include。

> **注**：`runtime.py` 中的 `/install`、`/grape-worker.tar.gz` 等静态分发端点归 A（用户面下载入口），不归 C。

#### 加载的定时任务清单

| 定时任务 | 频率 | 模块 |
|---------|------|------|
| `start_local_claimer` | 3s | `src.runtime.local_claimer` |
| `start_scheduler` (autopilot) | 60s | `src.runtime.scheduler` |
| `start_health_refresh_scheduler` | 5min | `src.runtime.health_scorer` |
| `_local_probe_loop` | 10min | `src.runtime.local_probe` |
| `_reap_stale_loop` | 60s | `src.runtime.service.reap_stale_runtimes` |
| `sweep_tasks_loop`（拆分后） | 15min | `src.runtime.cleanup.sweep_stuck_agent_tasks_once` |
| `_table_maintenance_worker`（agent_tasks 部分） | 6h | `src.runtime.table_maintenance.cleanup_old_agent_tasks_fields` + `delete_old_agent_tasks` |

#### 独占的表

`agent_tasks`（C 主消费方）, `task_messages`, `agent_shared_knowledge`, `scene_knowledge_ref`, `agent_type`, `agent_type_config`, `backend_agents`

#### 共享的表

| 表 | 权限 | 说明 |
|----|------|------|
| `events` | 读写 | C 更新事件状态（`update_event_status`/`update_event_complete`） |
| `issue_state` | 只读 | C 读取 Issue 阶段判断可否清理 workdir |
| `managed_repos` | 只读 | C 间接依赖 org_id 做引擎选择隔离 |
| `agent_runtimes` | 读写 | C 更新引擎在线态/健康度/probe 结果 |
| `runtime_credentials` | 读写 | C 心跳更新 CPU/内存/磁盘/带宽/负载 |
| `devops_agent_session` | 只读 | C 读取 Agent 配置会话 |
| `devops_pipeline_scene` | 只读 | C 读取场景 deploy_url |
| `engine_capabilities` | 读写 | C 注册/更新引擎标签能力 |
| `agent_type_runtime_pool` | 读写 | C 更新引擎池映射 |
| `distributed_locks` | 读写 | 分布式锁（autopilot_scheduler、table_maintenance） |
| `autopilot_tasks` | 读写 | C 调度器读取/触发 |
| `autopilot_executions` | 读写 | C 调度器写入执行记录 |
| `meta` | 读写 | C 读写元数据 |

---

## 3. P0 — _execute_agent 迁移

### 3.1 迁移概述

`_execute_agent` 及其相关函数当前位于 `src/api/event_pipeline.py`（B 维度核心文件），但其本质是 C 维度的 direct 执行路径。将其迁移到 `src/runtime/direct_executor.py`，使 B 的 `run_pipeline` 只负责入队，不再直接执行。

### 3.2 当前位置

| 函数/变量 | 位置 | 行号 | 说明 |
|-----------|------|------|------|
| `_DIRECT_POOL` | `src/api/event_pipeline.py` | L13-16 | 有界线程池（max_workers=20） |
| `_DIRECT_FUTURES` | `src/api/event_pipeline.py` | L18 | `{event_seq: Future}` 映射 |
| `_DIRECT_FUTURES_LOCK` | `src/api/event_pipeline.py` | L19 | Futures 字典锁 |
| `start_direct_agent` | `src/api/event_pipeline.py` | L348-356 | 提交任务到 `_DIRECT_POOL` |
| `_execute_agent` | `src/api/event_pipeline.py` | L3668-3704 | 顶层 try/except 兜底 + finally 清理 |
| `_execute_agent_inner` | `src/api/event_pipeline.py` | L3816+ | 实际执行逻辑（构建 prompt → stream_agent → 结果回写） |

### 3.3 迁移目标

**新建文件**：`src/runtime/direct_executor.py`

### 3.4 迁移内容

```python
# src/runtime/direct_executor.py

"""本机 direct 执行器——从 event_pipeline.py 迁移。
负责本机引擎的 Agent 执行（stream_devops_agent → 结果回写 → 事件更新）。
"""
import os, threading
from concurrent.futures import ThreadPoolExecutor, Future
from src.config import log

# 有界并发池
_DIRECT_POOL = ThreadPoolExecutor(
    max_workers=int(os.environ.get("RUNTIME_MAX_CONCURRENT_TASKS", "20")),
    thread_name_prefix="ag-direct",
)

# {event_seq: Future} — 供 sweeper 取消排队中的 direct 任务
_DIRECT_FUTURES: dict[int, Future] = {}
_DIRECT_FUTURES_LOCK = threading.Lock()


def start_direct_agent(fn, args, name=""):
    """提交一个本机 direct 执行任务到有界池。"""
    future = _DIRECT_POOL.submit(fn, *args)
    seq = args[1] if len(args) > 1 else 0
    if seq:
        with _DIRECT_FUTURES_LOCK:
            _DIRECT_FUTURES[seq] = future
    return future


def _execute_agent(repo_key, seq, agent_type, backend, skill_name, evt,
                   runtime_id=0, task_id=""):
    """后台执行 Agent：顶层 try/except 兜底。"""
    # ... 从 event_pipeline.py L3668-3704 原样迁移


def _execute_agent_inner(repo_key, seq, agent_type, backend, skill_name, evt,
                         runtime_id=0, task_id=""):
    """_execute_agent 的实际逻辑。"""
    # ... 从 event_pipeline.py L3816+ 原样迁移
    # 包含：update_task_status → stream_devops_agent → update_event_complete
    #       → _post_result_to_issue/_post_result_to_pr → A2A 交接触发
```

### 3.5 迁移后的调用关系

#### 迁移前（当前）

```
event_pipeline.py:
  run_pipeline()
    ├── kind=direct → start_direct_agent(_execute_agent, ...)
    │                    └── _DIRECT_POOL.submit → _execute_agent → _execute_agent_inner
    └── kind=remote → enqueue_task() → agent_tasks(status=queued)

local_claimer.py:
  _execute_claimed_locally()
    └── from src.api.event_pipeline import _execute_agent, start_direct_agent
        └── start_direct_agent(_execute_agent, ...)
```

#### 迁移后

```
event_pipeline.py (B 进程):
  run_pipeline()
    └── 所有路径 → enqueue_task() → agent_tasks(status=queued)
        （不再调用 start_direct_agent / _execute_agent）

direct_executor.py (C 进程):
  start_direct_agent(fn, args)
    └── _DIRECT_POOL.submit → _execute_agent → _execute_agent_inner

local_claimer.py (C 进程):
  _execute_claimed_locally()
    └── from src.runtime.direct_executor import _execute_agent, start_direct_agent
        └── start_direct_agent(_execute_agent, ...)
```

### 3.6 迁移后 B 的 run_pipeline 变更

`run_pipeline` 中的 direct 执行分派路径（当前 4 处调用 `start_direct_agent`）全部改为 `enqueue_task`：

| 行号 | 当前逻辑 | 迁移后 |
|------|---------|--------|
| L1091 | `start_direct_agent(_execute_agent, ...)` | `enqueue_task(...)` （status=queued，等待 C 认领） |
| L1193 | `start_direct_agent(_execute_agent, ...)` | `enqueue_task(...)` |
| L1327 | `start_direct_agent(_execute_agent, ...)` | `enqueue_task(...)` |
| L2589 | `start_direct_agent(_execute_agent, ...)` | `enqueue_task(...)` |
| L4978 | `start_direct_agent(_execute_agent, ...)` | `enqueue_task(...)` |

**关键变更**：`runtime_execution_kind` 返回 `EXEC_MODE_DIRECT` 时，不再走 `start_direct_agent`，而是走 `enqueue_task` 入队。C 进程的 `local_claimer` 会在 3s 内认领并执行。

> **注意**：这会引入约 3s 的认领延迟（原 direct 是立即执行）。对于用户面手动触发（如 chat send）的场景，可接受。如果需要零延迟，C 进程的 `local_claimer` 可通过 `affinity_runtime_id` 优先认领。

### 3.7 迁移后 C 的 local_claimer 变更

```python
# src/runtime/local_claimer.py — 修改 import
# 迁移前:
from src.api.event_pipeline import _execute_agent, start_direct_agent

# 迁移后:
from src.runtime.direct_executor import _execute_agent, start_direct_agent
```

### 3.8 影响的调用链

| 调用方 | 当前 import 来源 | 迁移后 import 来源 | 影响 |
|--------|------------------|-------------------|------|
| `event_pipeline.run_pipeline` | 自身（同文件） | 删除 direct 调用，改用 `enqueue_task` | 逻辑变更 |
| `event_pipeline._dispatch_pr_subscription_extra` | 自身 | 删除 direct 调用 | 逻辑变更 |
| `event_pipeline._on_handoff_dispatch` | 自身 | 删除 direct 调用 | 逻辑变更 |
| `local_claimer._execute_claimed_locally` | `from src.api.event_pipeline` | `from src.runtime.direct_executor` | import 路径变更 |
| `cleanup.sweep_stuck_events_once` | 无直接依赖（操作 `_DIRECT_FUTURES` 取消） | 需从 `direct_executor` import `_DIRECT_FUTURES` | import 路径变更 |
| `event_pipeline._cancel_direct_future` | 自身 | 从 `direct_executor` import | import 路径变更 |

### 3.9 验证方法

1. **单元测试**：现有 `test_direct_executor*.py`（如有）需更新 import 路径。
2. **集成测试**：
   - 发送 webhook → 确认 `agent_tasks` 表有 `queued` 行（B 入队）
   - 确认 3s 内 `local_claimer` 认领 → `status=running`（C 认领）
   - 确认 Agent 执行完成 → `status=completed` + `events.status=completed`（C 回写）
3. **回归验证**：
   - 手动触发 chat send → 确认走 enqueue → claim → execute 链路
   - A2A handoff → 确认下游 agent 通过 enqueue 触发
   - sweep_stuck_events_once → 确认能取消 `_DIRECT_FUTURES` 中的排队 Future

---

## 4. P1 — 进程拆分

### 4.1 三个入口文件设计

#### 4.1.1 grape_server.py（A 进程）

```python
# src/api/grape_server.py
"""Grape 工作台服务 — 用户面向 API (port 38080)"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from contextlib import asynccontextmanager

# 路由导入（25 个用户面路由 + admin + git-repo）
from src.api.routes import (
    auth, grape_auth, repos, dashboard, agents, events, chat, settings,
    skills, search, pipeline, scenes, templates, issues, feishu_onboard,
    feishu_card, task_logs, knowledge, health, devstage, latency,
    feedback, autopilot, orgs, community, observability,
)
from src.admin.routes import router as admin_router
from src.api.git_repo_api import router as git_repo_router
# runtime.py 只加载 GET 端点（用户面查看）
from src.api.routes.runtime_view import router as runtime_view_router


@asynccontextmanager
async def lifespan(app: FastAPI):
    # ... A 进程定时任务启动（见 §6）
    # IM WS BotManager 启动
    # 安全门禁
    # 模板自愈 / help.html / OpenAPI 落盘
    # D32 迁移
    # knowledge_migrate
    yield
    # ... 优雅关闭


app = FastAPI(
    title="Agentic DevOps API",
    version="2.2.0",
    lifespan=lifespan,
    docs_url="/docs",
    redoc_url="/redoc",
    openapi_url="/openapi.json",
)

app.add_middleware(CORSMiddleware, allow_origins=["*"], ...)

# 注册路由
for r in [grape_auth.router, auth.router, repos.router, dashboard.router,
          agents.router, chat.router, settings.router, skills.router,
          search.router, pipeline.router, scenes.router, templates.router,
          issues.router, feishu_onboard.router, feishu_card.router,
          task_logs.router, knowledge.router, health.router,
          devstage.router, latency.router, feedback.router,
          autopilot.router, orgs.router, community.router,
          observability.router, admin_router, git_repo_router,
          runtime_view_router]:
    app.include_router(r)

# 静态文件 + SPA
# ... /static mount, issue_attachments, community_uploads
```

#### 4.1.2 grape_event_server.py（B 进程）

```python
# src/api/grape_event_server.py
"""Grape 事件处理服务 — Webhook 接收与事件入队 (port 38081)"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from fastapi import FastAPI
from contextlib import asynccontextmanager

from src.api.routes.webhook import router as webhook_router
from src.api.routes.events import router as events_router  # 操作端点
from src.api.db_setup import init_database
from src.config import _load_accounts, log


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_database()
    _load_accounts()
    # B 进程定时任务（见 §6）
    # sweep_events_loop
    # bot_token_health
    yield


app = FastAPI(title="Grape Event Server", version="2.2.0", lifespan=lifespan)
app.include_router(webhook_router)
app.include_router(events_router)


@app.get("/health")
async def health():
    return {"status": "ok", "service": "event-server"}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("src.api.grape_event_server:app",
                host="0.0.0.0", port=38081, workers=1)
```

#### 4.1.3 grape_worker_server.py（C 进程）

```python
# src/api/grape_worker_server.py
"""Grape Worker 执行态服务 — 任务认领与 Agent 执行 (port 38082)"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from fastapi import FastAPI
from contextlib import asynccontextmanager

from src.api.routes.runtime import router as runtime_router  # POST 端点
from src.api.routes.tasks import router as tasks_router
from src.config import log


@asynccontextmanager
async def lifespan(app: FastAPI):
    # C 进程定时任务（见 §6）
    # local_claimer
    # scheduler/autopilot
    # health_refresh
    # local_probe
    # reap_stale
    # sweep_tasks_loop
    # table_maintenance (agent_tasks 部分)
    yield


app = FastAPI(title="Grape Worker Server", version="2.2.0", lifespan=lifespan)
app.include_router(runtime_router)
app.include_router(tasks_router)


@app.get("/health")
async def health():
    return {"status": "ok", "service": "worker-server"}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("src.api.grape_worker_server:app",
                host="0.0.0.0", port=38082, workers=1)
```

### 4.2 路由注册清单

| 路由文件 | prefix | A (38080) | B (38081) | C (38082) | 说明 |
|---------|--------|-----------|-----------|-----------|------|
| `auth.py` | `/api/auth` | ✅ | | | JWT 刷新/登出 |
| `grape_auth.py` | `/api/auth` | ✅ | | | 注册/登录/OAuth |
| `repos.py` | `/api/repos` | ✅ | | | 仓库托管 |
| `dashboard.py` | `/api/dashboard` | ✅ | | | 仪表盘 |
| `agents.py` | `/api/agents` | ✅ | | | Agent 配置 |
| `chat.py` | `/api/chat` | ✅ | | | 聊天 |
| `settings.py` | `/api/settings` | ✅ | | | 凭据/引擎 |
| `skills.py` | `/api/skills` | ✅ | | | 技能市场 |
| `search.py` | `/api/search` | ✅ | | | 搜索 |
| `pipeline.py` | `/api/pipeline` | ✅ | | | 流水线展示 |
| `scenes.py` | `/api/scenes` | ✅ | | | 场景管理 |
| `templates.py` | `/api/project-templates` | ✅ | | | 项目模板 |
| `issues.py` | `/api/issues` | ✅ | | | Issue 生命周期 |
| `feishu_onboard.py` | `/api/feishu/onboard` | ✅ | | | 飞书入驻 |
| `feishu_card.py` | `/api/feishu/card-action` | ✅ | | | 飞书卡片 |
| `task_logs.py` | `/api/task-logs` | ✅ | | | 任务日志 |
| `knowledge.py` | `/api/knowledge` | ✅ | | | 知识库 |
| `health.py` | (无) | ✅ | | | 健康检查 |
| `devstage.py` | `/api/devstage` | ✅ | | | DevStage |
| `latency.py` | `/api/latency` | ✅ | | | 延迟上报 |
| `feedback.py` | `/api` | ✅ | | | 反馈 |
| `autopilot.py` | `/api/autopilot` | ✅ | | | Autopilot CRUD |
| `orgs.py` | `/api/orgs` | ✅ | | | 组织 |
| `community.py` | `/api/community` | ✅ | | | 社区 |
| `observability.py` | `/api/obs` | ✅ | | | 可观测性 |
| `admin/routes.py` | `/api/admin` | ✅ | | | 管理后台 |
| `git_repo_api.py` | `/api/git-repo` | ✅ | | | Git 仓库接口 |
| `webhook.py` | (无) | | ✅ | | Webhook + A2A |
| `events.py`（操作端点） | `/api/events` | | ✅ | | terminate/redispatch |
| `events.py`（GET 详情） | `/api/events` | ✅ | | | 事件详情查看 |
| `runtime.py`（POST 端点） | `/api/runtime` | | | ✅ | register/heartbeat/claim/complete |
| `runtime.py`（GET 端点） | `/api/runtime` | ✅ | | | machine-health/tasks/install |
| `tasks.py` | `/api/tasks` | | | ✅ | notify/complete/current |

> **runtime.py 拆分方案**：新建 `src/api/routes/runtime_view.py`，将 `runtime.py` 中的所有 `@router.get` 端点迁移过去。`runtime.py` 只保留 `@router.post` 端点。两个文件各自定义独立的 `APIRouter`。

### 4.3 定时任务注册清单

详见 [§6 — 定时任务归属](#6-p3--定时任务归属)。

### 4.4 环境变量配置

| 环境变量 | A 进程 | B 进程 | C 进程 | 说明 |
|---------|--------|--------|--------|------|
| `GRAPE_PORT` | 38080 | 38081 | 38082 | 各进程监听端口 |
| `GRAPE_PROCESS_ROLE` | `server` | `event` | `worker` | 进程角色标识（供日志/监控区分） |
| `RUNTIME_MAX_CONCURRENT_TASKS` | — | — | 20 | C 进程 `_DIRECT_POOL` 最大并发 |
| `GRAPE_LOCAL_CLAIMER` | — | — | 1 | C 进程开启本机认领器 |
| `SWEEPER_INTERVAL_SECONDS` | — | 900 | 900 | B/C 各自 sweeper 频率 |
| `BOT_LEASE_DISABLED` | false | — | — | A 进程 IM lease 开关 |
| `METRICS_ADDR` | (可选) | (可选) | (可选) | Prometheus metrics 端口 |
| `OBS_LOG_DB` | 1 | 0 | 0 | 仅 A 进程落库可观测性日志 |

### 4.5 nginx 反代配置

见 [§8 — nginx 反代配置](#8-nginx-反代配置)。

### 4.6 启动/停止脚本

见 [§9 — 启动/部署脚本](#9-启动部署脚本)。

### 4.7 健康检查端点

| 进程 | 端点 | 响应 |
|------|------|------|
| A | `GET /health` | `{"status": "ok", "service": "server"}` |
| A | `GET /api/ping` | `{"ok": true, "ts": "..."}` |
| B | `GET /health` | `{"status": "ok", "service": "event-server"}` |
| C | `GET /health` | `{"status": "ok", "service": "worker-server"}` |

---

## 5. P2 — sweep_loop 拆分

### 5.1 当前 sweep_once() 结构

```python
# src/runtime/cleanup.py — 当前的 sweep_once()
def sweep_once() -> int:
    cleaned = 0
    # 1. 僵尸事件回收（B 维度）
    cleaned += sweep_stuck_events_once()        # L287 — 回收卡 running 的 events
    # 2. 悬空任务回收（C 维度）
    cleaned += sweep_stuck_agent_tasks_once()   # L426 — 回收 queued 无认领 / running 僵尸
    # 3. IM 实例清理
    cleaned += purge_stale_instances(older_than_hours=24)
    # 4. workdir 清理（依赖 issue_state，B+C 跨维度）
    for repo_key, issue_id in _done_issues():
        repogit.cleanup_issue_worktree(...)
    return cleaned

async def sweep_loop():
    while True:
        await asyncio.to_thread(sweep_once)
        await asyncio.sleep(900)  # 15min
```

### 5.2 拆分后

#### B 进程 — event sweeper

```python
# src/runtime/cleanup.py — 新增
async def sweep_events_loop():
    """B 进程：僵尸事件回收 + IM 实例清理 + workdir 清理。
    workdir 清理依赖 issue_state（B 独占），放在 B 进程合理。
    """
    import asyncio
    while True:
        try:
            cleaned = 0
            cleaned += sweep_stuck_events_once()
            try:
                from src.im.bot_lease import purge_stale_instances
                cleaned += purge_stale_instances(older_than_hours=24)
            except Exception:
                pass
            # workdir 清理（读 issue_state，B 独占表）
            from src.runtime.cleanup import _active_issue_sessions, _done_issues, _GIT_AGENTS
            from src.runtime import repogit
            active = _active_issue_sessions()
            for repo_key, issue_id in _done_issues():
                num = str(issue_id or "").lstrip("#")
                if not num or num == "0":
                    continue
                for agent_type in _GIT_AGENTS:
                    if (repo_key, agent_type, num) in active:
                        continue
                    issue_dir = repogit.issue_dir_for(repo_key, agent_type, num)
                    if os.path.isdir(issue_dir):
                        repogit.cleanup_issue_worktree(repo_key, agent_type, num)
                        cleaned += 1
            if cleaned:
                log.info(f"Event sweeper: cleaned {cleaned} items")
        except Exception as e:
            log.warning(f"Event sweeper error: {e}")
        await asyncio.sleep(900)  # 15min
```

#### C 进程 — task sweeper

```python
# src/runtime/cleanup.py — 新增
async def sweep_tasks_loop():
    """C 进程：悬空 agent_tasks 回收。"""
    import asyncio
    while True:
        try:
            cleaned = sweep_stuck_agent_tasks_once()
            if cleaned:
                log.info(f"Task sweeper: cleaned {cleaned} stuck tasks")
        except Exception as e:
            log.warning(f"Task sweeper error: {e}")
        await asyncio.sleep(900)  # 15min
```

### 5.3 频率

两个 sweeper 均保持 **15 分钟**（`SWEEPER_INTERVAL_SECONDS=900`），与当前一致。

### 5.4 注意事项

- `sweep_stuck_events_once` 内部会取消 `_DIRECT_FUTURES` 中的排队 Future——P0 迁移后需从 `direct_executor` import `_DIRECT_FUTURES`。
- `sweep_stuck_agent_tasks_once` 内部会重派 queued 任务——依赖 C 进程的 `local_claimer` 在线认领，放在 C 进程合理。
- workdir 清理依赖 `issue_state`（B 独占表），但 B 和 C 共享 DB，C 也可读取。最终决定放在 B（因为 workdir 清理与事件生命周期强相关）。

---

## 6. P3 — 定时任务归属

### 6.1 完整定时任务分配表

| 定时任务 | 频率 | 归属进程 | 模块 | 说明 |
|---------|------|---------|------|------|
| `_lease_sweep_loop` | 循环 | **A** | `bot_manager.lease_sweep_loop` | IM Bot lease 过期清理 |
| `_knowledge_sync_loop` | 每小时 | **A** | `src.api.knowledge.knowledge_sync_loop` | 知识库 OBS 同步迁移 |
| `knowledge_write_worker` | 持续 | **A** | `src.api.knowledge.knowledge_write_worker` | 知识写入队列消费者 |
| `start_skill_sync_scheduler` | 30min | **A** | `src.storage.skill_sync` | 官方技能 OBS 自动同步 |
| `start_feedback_reconcile_scheduler` | 定时 | **A** | `src.api.feedback_issue_converter` | 反馈→Issue 转换 |
| `start_upload_cleanup_scheduler` | 每小时 | **A** | `src.storage.upload_cleanup` | 临时上传文件清理 |
| `_table_maintenance`（task_messages 部分） | 6h | **A** | `cleanup_old_task_messages` | 旧消息清理 |
| `start_cooldown_scheduler` | 每天 | **A** | `src.admin.cooldown` | 冷静期账号匿名化 |
| `_active_user_tracker` | 30s | **A** | 自定义 | 用户活跃状态更新 + session token TTL + event guard 清理 |
| `_server_metrics_loop` | 15s | **A** | 自定义（psutil） | 主控机 CPU/内存/磁盘/带宽采集 |
| `_obs_alert_loop` | 30s | **A** | `src.observability.alerts` | 告警规则求值、SLO 评估 |
| `_obs_rollup_loop` | 60s | **A** | `src.observability.metric_rollup` | 业务指标聚合 |
| `_obs_sample_loop` | 60s | **A** | `src.observability.rollup` | 设备资源采样、日志留存清理 |
| `install_master_logging` | 持续 | **A** | `src.observability.logs` | 主控 WARNING/ERROR 落库 |
| `_knowledge_migrate_wrapper` | 一次性 | **A** | `src.api.db_setup._migrate_knowledge` | 知识库迁移 |
| `D32 migration` | 一次性 | **A** | `scripts.migrate_user_accounts_to_grape` | 账号迁移 |
| `sweep_events_loop`（拆分后） | 15min | **B** | `src.runtime.cleanup.sweep_events_loop` | 僵尸事件回收 + workdir 清理 |
| `start_bot_token_health` | 5min | **B** | `src.runtime.bot_token_health` | Bot Token 健康探活 |
| `start_local_claimer` | 3s | **C** | `src.runtime.local_claimer` | 本机 direct 引擎认领 |
| `start_scheduler` (autopilot) | 60s | **C** | `src.runtime.scheduler` | Autopilot 定时调度 |
| `start_health_refresh_scheduler` | 5min | **C** | `src.runtime.health_scorer` | 引擎健康度刷新 |
| `_local_probe_loop` | 10min | **C** | `src.runtime.local_probe` | 本机引擎 `--version` 重探 |
| `_reap_stale_loop` | 60s | **C** | `src.runtime.service.reap_stale_runtimes` | 远端 runtime 僵尸清理 |
| `sweep_tasks_loop`（拆分后） | 15min | **C** | `src.runtime.cleanup.sweep_tasks_loop` | 悬空 agent_tasks 回收 |
| `_table_maintenance`（agent_tasks 部分） | 6h | **C** | `cleanup_old_agent_tasks_fields` + `delete_old_agent_tasks` | 旧任务字段清理 + 90 天前终态任务删除 |

### 6.2 table_maintenance 拆分

当前 `_table_maintenance_worker` 在一个分布式锁内串行执行三项清理：

```python
# 当前（main.py L253-273）
async def _table_maintenance_worker():
    while True:
        async with run_with_distributed_lock("table_maintenance", ttl=3600):
            cleanup_old_agent_tasks_fields()    # C 维度
            delete_old_agent_tasks()            # C 维度
            cleanup_old_task_messages()         # A 维度
        await asyncio.sleep(6 * 3600)
```

拆分后：

| 清理函数 | 归属 | 锁 key | 说明 |
|---------|------|--------|------|
| `cleanup_old_agent_tasks_fields` | **C** | `table_maintenance_tasks` | 清理 agent_tasks 旧字段 NULL |
| `delete_old_agent_tasks` | **C** | `table_maintenance_tasks` | 删除 90 天前终态 agent_tasks |
| `cleanup_old_task_messages` | **A** | `table_maintenance_messages` | 删除旧 task_messages |

两个锁 key 独立，A 和 C 可并行执行各自的清理。

---

## 7. 共享 DB 表协作

### 7.1 agent_tasks — B 写入 / C 消费 / A 查看

```
B (event_pipeline)               C (runtime/service)              A (routes)
     │                                │                              │
     │ enqueue_task()                 │                              │
     │ INSERT status=queued           │                              │
     │───────────────────────────►    │                              │
     │                                │ claim() / claim_by_tag()     │
     │                                │ UPDATE status=running        │
     │                                │◄───────────────────────────►│
     │                                │                              │
     │                                │ complete_task()              │
     │                                │ UPDATE status=completed      │
     │                                │──────────────────────────►   │
     │                                │                              │ GET /tasks (查看)
     │                                │                              │ POST /cancel (取消)
     │                                │                              │
     │                                │ sweep_stuck_agent_tasks_once │
     │                                │ (回收僵尸)                    │
```

| 操作方 | 操作 | 时机 |
|--------|------|------|
| **B** | `enqueue_task()` INSERT `queued` | webhook 事件路由后 |
| **B** | `_create_direct_task()` INSERT+running（P0 后废弃，改用 enqueue） | — |
| **C** | `claim()` / `claim_by_tag()` UPDATE `queued→running` | worker / local_claimer 认领 |
| **C** | `complete_task()` UPDATE `→completed` | Agent 执行完成 |
| **C** | `fail_task()` UPDATE `→failed` | Agent 执行失败 |
| **C** | `requeue_task()` UPDATE `→queued` | 任务重排队 |
| **A** | `batch_recover_stale_tasks()` UPDATE | 管理员批量恢复 |
| **A** | `cancel_task()` UPDATE `→cancelled` | 用户取消 |
| **C** | `sweep_stuck_agent_tasks_once()` 回收 | 15min 定时 |
| **C** | `table_maintenance` 清理旧字段/删除 | 6h 定时 |

### 7.2 events — B 写入 / C 更新 / A 查看

```
B (event_pipeline)               C (direct_executor / service)     A (routes)
     │                                │                              │
     │ append_event()                 │                              │
     │ INSERT INTO events → seq       │                              │
     │───────────────────────────►    │                              │
     │                                │ update_event_status()        │
     │                                │ UPDATE status=running        │
     │                                │ update_event_complete()      │
     │                                │ UPDATE status=completed/     │
     │                                │        failed                │
     │                                │──────────────────────────►   │
     │                                │                              │ GET /events/detail/{seq}
     │                                │                              │ POST /events/{seq}/terminate
     │                                │                              │ POST /events/{seq}/redispatch
     │                                │                              │
     │ B sweeper                      │                              │
     │ sweep_stuck_events_once()      │                              │
     │ (回收卡 running 事件)           │                              │
```

| 操作方 | 操作 | 时机 |
|--------|------|------|
| **B** | `append_event()` / INSERT | webhook 事件写入 |
| **B** | `_fail_event()` UPDATE `→failed` | 事件处理失败 |
| **C** | `update_event_status()` UPDATE `→running` | Agent 开始执行 |
| **C** | `update_event_complete()` UPDATE `→completed/failed` | Agent 执行完成 |
| **A** | GET `/events/detail/{seq}` | 用户查看 |
| **A/B** | POST `/events/{seq}/terminate` | 用户终止（A 路由 → B 逻辑） |
| **A/B** | POST `/events/{seq}/redispatch` | 用户重派（A 路由 → B 逻辑 `run_pipeline`） |
| **B** | `sweep_stuck_events_once()` | 15min 定时 |

### 7.3 managed_repos — A 配置 / B 校验 / C 间接依赖

```
A (routes/repos.py)             B (webhook.py + event_pipeline)    C (event_pipeline + bot_token_health)
     │                                │                              │
     │ POST /host (托管仓库)           │                              │
     │ PUT /bot-token (设置 token)     │                              │
     │ PUT /deploy-url                 │                              │
     │───────────────────────────►    │                              │
     │                                │ webhook token 校验（读取）     │
     │                                │ bot_username 读取             │
     │                                │ deploy_url 登记               │
     │                                │                              │ org_id 读取（引擎选择隔离）
     │                                │                              │
```

### 7.4 issue_state — B 写入阶段 / A 查看/操作

| 操作方 | 操作 | 时机 |
|--------|------|------|
| **B** | `set_issue_stage()` 阶段初始化与推进 | event_pipeline 路由判定后 |
| **A** | GET status | 用户查看 Issue 状态 |
| **A** | PUT status（confirm-done/cancel） | 用户面状态流转 |
| **A** | dashboard 查看仓库面板 | 仪表盘展示 |
| **C** | 读取活跃 session 判断可否清理 workdir | sweep 时读取 |

### 7.5 distributed_locks — 三进程共用

| 锁 key | 使用方 | TTL | 归属进程 |
|--------|--------|-----|---------|
| `table_maintenance_tasks` | C 进程 table_maintenance | 3600s | C |
| `table_maintenance_messages` | A 进程 table_maintenance | 3600s | A |
| `autopilot_scheduler` | scheduler.py `scheduler_loop` | 120s | C |
| `feedback_reconcile` | feedback_issue_converter.py | 300s | A |
| `token_health_check` | bot_token_health.py | LOCK_TTL | B |
| knowledge CRUD locks | knowledge.py | 60-300s | A |
| deps.py refresh locks | deps.py | 30s | A+C |

> **跨进程锁安全**：`distributed_locks` 表三进程共用，通过 DB 行级锁 + `db_lock`（SQLite）保证原子性。MySQL 后端使用 `SELECT ... FOR UPDATE` 或 `INSERT OR IGNORE` 实现分布式锁语义。

---

## 8. nginx 反代配置

```nginx
# /etc/nginx/conf.d/grape.conf

upstream grape_server {
    server 127.0.0.1:38080;
}

upstream grape_event_server {
    server 127.0.0.1:38081;
}

upstream grape_worker_server {
    server 127.0.0.1:38082;
}

server {
    listen 80;
    server_name _;

    # gzip 压缩（API JSON 响应）
    gzip on;
    gzip_types application/json;
    gzip_min_length 10k;
    gzip_comp_level 5;
    gzip_vary on;

    # 多图上传放行
    client_max_body_size 50m;

    # ===== B 进程：Webhook + A2A =====
    location /webhook/ {
        proxy_pass http://grape_event_server;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }

    location /api/a2a {
        proxy_pass http://grape_event_server;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }

    # ===== C 进程：Worker API =====
    # Worker 注册/心跳/认领/完成等 POST 端点
    location ~ ^/api/runtime/(register|heartbeat|runtimes/.*/tasks/claim|runtimes/.*/tasks/claim-by-tag|tasks/.*/start|tasks/.*/progress|tasks/.*/messages|tasks/.*/usage|tasks/.*/complete|tasks/.*/fail|tasks/.*/requeue|tasks/.*/cancel|runtimes/.*/recover-orphans|runtimes/.*/skills-report|runtimes/.*/refresh-skills) {
        proxy_pass http://grape_worker_server;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_read_timeout 300s;
        proxy_send_timeout 300s;
    }

    # tasks notify/complete/current — Agent runtime 调用
    location ~ ^/api/tasks/(notify|complete|current|awaiting-input) {
        proxy_pass http://grape_worker_server;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }

    # ===== B 进程：events 操作端点 =====
    # terminate / redispatch（用户操作事件，触发 pipeline 重派）
    location ~ ^/api/events/.*/(terminate|redispatch) {
        proxy_pass http://grape_event_server;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }

    # ===== A 进程：其余所有请求（默认） =====
    location / {
        proxy_pass http://grape_server;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;

        # WebSocket support（IM WS / SSE 流）
        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "upgrade";

        proxy_read_timeout 300s;
        proxy_send_timeout 300s;
    }
}
```

> **简化方案**：如果不想用复杂的正则分流，可以用更简单的路径前缀：
> - `/webhook/` → B (38081)
> - `/api/a2a` → B (38081)
> - `/api/runtime/register`、`/api/runtime/heartbeat`、`/api/runtime/tasks/`、`/api/runtime/runtimes/` → C (38082)
> - 其余 → A (38080)
>
> 但这需要 A 进程也加载 `runtime.py` 的 GET 端点（install 下载、machine-health 等），确保不被 C 独占。

---

## 9. 启动/部署脚本

### 9.1 systemd 服务文件

#### grape-server.service（A 进程）

```ini
# /etc/systemd/system/grape-server.service
[Unit]
Description=Grape Server (Workbench API)
After=network.target

[Service]
User=grape
WorkingDirectory=/opt/grape
Environment=PYTHONUNBUFFERED=1
Environment=GRAPE_PROCESS_ROLE=server
Environment=GRAPE_PORT=38080
EnvironmentFile=/etc/grape/grape.env
ExecStart=/opt/grape/.venv/bin/uvicorn src.api.grape_server:app \
    --host 0.0.0.0 --port 38080 --workers 2
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

#### grape-event-server.service（B 进程）

```ini
# /etc/systemd/system/grape-event-server.service
[Unit]
Description=Grape Event Server (Webhook + Event Pipeline)
After=network.target grape-server.service

[Service]
User=grape
WorkingDirectory=/opt/grape
Environment=PYTHONUNBUFFERED=1
Environment=GRAPE_PROCESS_ROLE=event
Environment=GRAPE_PORT=38081
EnvironmentFile=/etc/grape/grape.env
ExecStart=/opt/grape/.venv/bin/uvicorn src.api.grape_event_server:app \
    --host 0.0.0.0 --port 38081 --workers 1
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

#### grape-worker-server.service（C 进程）

```ini
# /etc/systemd/system/grape-worker-server.service
[Unit]
Description=Grape Worker Server (Task Execution)
After=network.target grape-server.service

[Service]
User=grape
WorkingDirectory=/opt/grape
Environment=PYTHONUNBUFFERED=1
Environment=GRAPE_PROCESS_ROLE=worker
Environment=GRAPE_PORT=38082
Environment=GRAPE_LOCAL_CLAIMER=1
Environment=RUNTIME_MAX_CONCURRENT_TASKS=20
EnvironmentFile=/etc/grape/grape.env
ExecStart=/opt/grape/.venv/bin/uvicorn src.api.grape_worker_server:app \
    --host 0.0.0.0 --port 38082 --workers 1
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

### 9.2 环境变量文件（grape.env）

```bash
# /etc/grape/grape.env — 三进程共用
# 数据库
GRAPE_DB_PATH=/opt/grape/data/grape.db
# 对外基址
SERVER_BASE_URL=https://grape.example.com
# JWT
JWT_SECRET=<your-secret>
# IM
BOT_LEASE_DISABLED=false
# 可观测性
OBS_LOG_DB=1
METRICS_ADDR=9090
# Sweeper
SWEEPER_INTERVAL_SECONDS=900
# 其他原有配置...
```

### 9.3 启动/停止命令

```bash
# 启动三进程
systemctl daemon-reload
systemctl enable --now grape-server grape-event-server grape-worker-server

# 停止
systemctl stop grape-server grape-event-server grape-worker-server

# 重启单个进程
systemctl restart grape-event-server

# 查看状态
systemctl is-active grape-server grape-event-server grape-worker-server

# 健康检查
curl -s http://127.0.0.1:38080/health    # {"status":"ok","service":"server"}
curl -s http://127.0.0.1:38081/health    # {"status":"ok","service":"event-server"}
curl -s http://127.0.0.1:38082/health    # {"status":"ok","service":"worker-server"}
```

### 9.4 开发模式启动脚本

```bash
#!/bin/bash
# scripts/dev_three_process.sh — 开发模式同时启动三进程

cd /home/grape/grape-new-version2

# 终端 1：A 进程
uvicorn src.api.grape_server:app --host 0.0.0.0 --port 38080 --reload &

# 终端 2：B 进程
uvicorn src.api.grape_event_server:app --host 0.0.0.0 --port 38081 --reload &

# 终端 3：C 进程
uvicorn src.api.grape_worker_server:app --host 0.0.0.0 --port 38082 --reload &

wait
```

---

## 10. 风险与缓解

### 10.1 events 表三角共享

**风险**：B 写入、C 更新状态、A 查看。三进程并发操作同一张表，可能出现：
- B 写入 `running` 状态后 C 同时更新为 `completed`，A 查到中间态。
- C 更新事件状态时 B 的 sweeper 同时标记为 `failed`。

**缓解**：
- events 表的 `status` 字段使用原子 UPDATE（`WHERE seq=? AND status=?`），保证状态流转单向性。
- B 的 sweeper 只回收 `status='running'` 且超过 30 分钟的事件，不会与 C 的正常更新冲突。
- A 只读，不修改 status（terminate 是显式操作，走独立路径）。

### 10.2 issue_state 竞态

**风险**：B 的 `set_issue_stage()` 和 A 的 `PUT /status`（confirm-done/cancel）可能并发修改同一 issue 的阶段。

**缓解**：
- 当前已通过 `db_lock`（SQLite）串行化所有 DB 写操作，不存在真正的并发。
- MySQL 后端使用行级锁 + 乐观锁（`WHERE stage=?` 条件更新）。
- 语义上 B 推进阶段（vod→coding→qa）与 A 用户操作（confirm-done）有明确时序：B 先设 `coding`，用户确认后 A 设 `done`，不会同时操作。

### 10.3 managed_repos 配置一致性

**风险**：A 修改 webhook token 后 B 需即时感知。当前 B 每次 webhook 都直查 DB（不缓存），所以不存在缓存过期问题。但如果未来加缓存则需注意。

**缓解**：
- 保持 B 对 `managed_repos` 的直查 DB 行为（不做缓存或极短 TTL ≤ 5s）。
- A 修改 token 后无需通知 B——下次 webhook 到达时 B 自然读到新值。

### 10.4 event_pipeline.py 拆分复杂度

**风险**：`event_pipeline.py` 是 5000+ 行巨型文件，包含路由判定、引擎选择、direct 执行、结果回写、A2A 交接等。直接拆分容易引入回归。

**缓解**：
- **P0 先行**：只迁移 `_execute_agent` 系列（约 200 行），不改动 `run_pipeline` 的路由/选择逻辑。
- **P1 渐进**：进程拆分时 `event_pipeline.py` 作为共享库被 B 进程 import，不需要拆文件——只拆入口。
- 后续可逐步将 `run_pipeline` 中的路由判定、引擎选择、prompt 构建等拆为独立模块。

### 10.5 db_lock 跨进程

**风险**：SQLite 的 `db_lock`（`threading.Lock`）只在进程内有效。三进程各自持有独立的 `db_lock` 实例，无法跨进程互斥。

**缓解**：
- **SQLite 后端**：依赖 SQLite 自身的文件级锁（`WAL` 模式下读写并发 + 写串行）。`db_lock` 只保证进程内线程安全，跨进程由 SQLite 文件锁兜底。需确保 `PRAGMA journal_mode=WAL` 和合理的 `busy_timeout`（≥ 5000ms）。
- **MySQL 后端**：不依赖 `db_lock`，直接使用 MySQL 的事务隔离 + 行级锁。`db_lock` 在 MySQL 模式下退化为 no-op（或保留做进程内线程安全）。
- **分布式锁**：跨进程的互斥操作（如 table_maintenance、autopilot_scheduler）已通过 `distributed_locks` 表实现，不依赖 `db_lock`。

### 10.6 local_claimer 认领延迟

**风险**：P0 迁移后，原 direct 立即执行改为 enqueue → claim → execute，引入约 3s 认领延迟。

**缓解**：
- `local_claimer` 的 `CLAIM_INTERVAL=3s` + `CLAIM_BACKSTOP_DELAY=15s`。
- 对于本机引擎，`affinity_runtime_id` 命中可优先认领（跳过 backstop 让步）。
- 用户面手动触发（chat send）的场景可接受 3s 延迟。
- 如果需零延迟，可在 C 进程保留一个「fast path」：`run_pipeline` 返回 task_id 后，C 进程主动 poll 1s 内的任务并优先认领。

### 10.7 IM WebSocket 归属

**风险**：IM WS（飞书 Bot 长连接）目前绑定在主进程 `main.py` 的 lifespan 中。拆分后需决定归属 A 还是 B。

**缓解**：
- IM WS 归 **A 进程**：用户面聊天依赖 IM 消息处理（`process_chat_async`），且 `chat.py` 路由在 A。
- IM WS 的 `bot_manager` 只在 A 进程启动，B/C 不启动。
- `bot_token_health` 定时探活归 B（因为它探活的是 webhook 用的 bot token，与 IM WS 无关）。

---

## 11. 验证清单

### 11.1 功能验证

| # | 验证项 | 步骤 | 预期结果 |
|---|--------|------|---------|
| 1 | Webhook → 事件入队 | 发送 GitCode webhook → 检查 `events` 表有新行 + `agent_tasks` 有 `queued` 行 | B 正常入队 |
| 2 | Worker 认领 → 执行 | 确认 `agent_tasks` 状态从 `queued` → `running`（≤3s） | C local_claimer 正常 |
| 3 | 执行 → 结果回写 | 确认 `agent_tasks` → `completed` + `events.status=completed` | C direct_executor 正常 |
| 4 | 用户查看结果 | A 进程 `GET /api/events/detail/{seq}` 返回正确 result | A 正常读取 |
| 5 | Chat 发送 → 执行 | 前端发送聊天消息 → 走 enqueue → claim → execute → SSE 回传 | 全链路通 |
| 6 | A2A 交接 | Agent 完成 → 触发 A2A handoff → 下游 agent 事件入队 → 执行 | B+C 协作正常 |
| 7 | 事件终止 | A 进程 `POST /api/events/{seq}/terminate` → 事件标记 failed + task cancelled | A→B→C 协作正常 |
| 8 | 事件重派 | A 进程 `POST /api/events/{seq}/redispatch` → 重新入队 → 执行 | A→B 协作正常 |
| 9 | Autopilot 定时触发 | C 进程 scheduler 到期 → 创建 Issue → event_pipeline 入队 → 执行 | C→B→C 链路正常 |
| 10 | 僵尸事件回收 | 手动制造卡 running 事件 → 等待 15min → B sweeper 回收 | B sweeper 正常 |
| 11 | 僵尸任务回收 | 手动制造 queued 无认领任务 → 等待 15min → C sweeper 回收 | C sweeper 正常 |
| 12 | 引擎健康刷新 | C 进程 `health_refresh_scheduler` 5min 刷新 → `agent_runtimes.healthy` 更新 | C 定时任务正常 |
| 13 | 可观测性告警 | A 进程 `_obs_alert_loop` 30s 求值 → 告警事件写入 → 通知发送 | A 定时任务正常 |
| 14 | 知识库同步 | A 进程 `knowledge_sync_loop` → OBS 同步 | A 定时任务正常 |
| 15 | 表维护 | A 清理 task_messages + C 清理 agent_tasks → 6h 后执行 | table_maintenance 拆分正常 |

### 11.2 性能验证

| # | 验证项 | 指标 | 目标 |
|---|--------|------|------|
| 1 | Webhook 响应时间 | webhook POST → 200 响应延迟 | < 200ms（B 只入队不执行） |
| 2 | 用户面 API 响应时间 | dashboard/issues/chat GET 响应 | < 500ms（A 不被 webhook/worker 阻塞） |
| 3 | 任务认领延迟 | enqueue → claim 间隔 | < 3s（local_claimer 轮询间隔） |
| 4 | 并发 webhook | 10 并发 webhook → 全部入队 | B 单 worker 可处理（event_pipeline 用线程池） |
| 5 | 并发执行 | 20 并发任务 → `_DIRECT_POOL` max 20 | C 不超限（排队等待） |

### 11.3 可靠性验证

| # | 验证项 | 步骤 | 预期结果 |
|---|--------|------|---------|
| 1 | B 进程宕机 | `systemctl stop grape-event-server` → A/C 正常运行 | 用户面可用、已认领任务继续执行 |
| 2 | C 进程宕机 | `systemctl stop grape-worker-server` → A/B 正常运行 | webhook 正常入队（queued 堆积），用户面可用 |
| 3 | A 进程宕机 | `systemctl stop grape-server` → B/C 正常运行 | webhook 正常处理、worker 正常执行 |
| 4 | B 进程重启 | 重启 B → queued 任务不被丢 | C 继续认领堆积的 queued 任务 |
| 5 | C 进程重启 | 重启 C → running 任务变孤儿 | C 的 `reap_stale_runtimes` 60s 内标记 offline + 重新派发 |
| 6 | 三进程同时重启 | `systemctl restart grape-*` → 全部恢复 | DB 状态一致，无数据丢失 |
| 7 | SQLite WAL 并发 | 三进程同时写 DB | 无 `database is locked` 错误（`busy_timeout` ≥ 5000ms） |

---

## 附录 A：实施路线图

| 阶段 | 内容 | 预估工作量 | 依赖 |
|------|------|-----------|------|
| **P0** | `_execute_agent` 迁移到 `src/runtime/direct_executor.py` | 1-2 天 | 无 |
| **P1** | 三个入口文件 + runtime.py 拆分 + nginx 配置 | 3-5 天 | P0 完成 |
| **P2** | `sweep_once()` 拆分为 `sweep_events_loop` + `sweep_tasks_loop` | 1 天 | P1 完成 |
| **P3** | 定时任务归属调整 + table_maintenance 拆分 | 1-2 天 | P1 完成 |
| **验证** | 全量回归测试 + 性能验证 + 可靠性验证 | 3-5 天 | P0-P3 完成 |

## 附录 B：文件变更清单

| 文件 | 操作 | 说明 |
|------|------|------|
| `src/runtime/direct_executor.py` | **新建** | 从 event_pipeline.py 迁移 `_execute_agent` 系列 |
| `src/api/grape_server.py` | **新建** | A 进程入口 |
| `src/api/grape_event_server.py` | **新建** | B 进程入口 |
| `src/api/grape_worker_server.py` | **新建** | C 进程入口 |
| `src/api/routes/runtime_view.py` | **新建** | runtime.py 的 GET 端点（A 加载） |
| `src/api/event_pipeline.py` | **修改** | 删除 `_execute_agent` 系列；direct 路径改用 `enqueue_task` |
| `src/runtime/local_claimer.py` | **修改** | import 改为 `from src.runtime.direct_executor` |
| `src/runtime/cleanup.py` | **修改** | 新增 `sweep_events_loop` + `sweep_tasks_loop`；`sweep_once` 保留兼容 |
| `src/api/main.py` | **保留** | 向后兼容（单进程模式仍可用） |
| `nginx-grape.conf` | **修改** | 三端口反代配置 |
| systemd 服务文件 | **新建** | 三个 `.service` 文件 |
