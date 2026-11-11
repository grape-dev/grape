# Phase 4 Spec（Issue #699）：四进程拆分（Mgmt + Event + Worker + Insight）

## 目标

把当前单进程（`src/api/main.py`，:3000 主 API + :8080 webhook）拆分为四进程独立运行：

| 进程 | 入口 | 端口 | 职责 |
|------|------|------|------|
| Mgmt | `apps/mgmt/main.py` → `src/api/grape_server.py` | :38080 | 用户面 API、身份认证、配置管理、IM WS、前端托管 |
| Event | `apps/runtime/webhook/main.py` → `src/api/grape_event_server.py` | :38081 | Webhook 接收、事件归一化、路由判定、任务入队 |
| Worker | `apps/runtime/worker/main.py` → `src/api/grape_worker_server.py` | :38082 | 任务认领、Agent 执行（direct + remote）、心跳、结果回传 |
| Insight | `apps/insight/main.py` → `src/api/grape_insight_server.py` | :38083 | 可观测性面板、SLO/告警、LLM 用量、延迟监控、全局搜索 |

设计依据：docs/weekly-09/grape-unified-implementation-design.md §5（四进程设计）、
§8（sweep_loop 拆分）、§9（共享 DB 表协作）、§10（nginx 反代）、§11（部署双模式 + systemd）。

## 范围

1. 创建 4 个进程服务模块：`src/api/grape_server.py` / `grape_event_server.py` /
   `grape_worker_server.py` / `grape_insight_server.py`。
2. 创建 4 个启动入口：`apps/mgmt/main.py`、`apps/runtime/webhook/main.py`、
   `apps/runtime/worker/main.py`、`apps/insight/main.py`。
3. 共享脚手架下沉 `src/api/server_common.py`（build_app / CORS / metrics /
   request_id+log 中间件 / 异常处理 / health-readiness / 前端静态托管）。
4. 共享进程任务启动器下沉 `src/api/server_tasks.py`，main.py lifespan 收敛为
   “四组进程任务并集”，行为与单进程现状等价。
5. 路由按进程分配（§5.5）：跨进程路由（issues / chat / events / runtime）
   通过方法过滤复用同一 APIRouter，不复制 handler。
6. 定时任务按进程分配（§5.1~5.4），Insight 定时任务独立成进程。
7. `sweep_loop` 拆分为 `sweep_events_loop`（Event）+ `sweep_tasks_loop`（Worker）（§8）。
8. `table_maintenance` 拆分：agent_tasks 维护 → Worker（锁 `table_maintenance_tasks`），
   task_messages 维护 → Insight（锁 `table_maintenance_messages`）（§9.5）。
9. nginx 四端口反代配置 + 4 个 systemd service 文件（§10/§11.3）。
10. 验收测试 TC-4.1~TC-4.12（可单测部分：独立启动/路由可达/任务分配/拆分；部署级部分以文档+静态校验覆盖）。

## 非目标

- 契约接口（MgmtService/RuntimeService/InsightService）实现填充（Phase 5）。
- 跨 schema SQL 收口到契约接口（Phase 5）。
- 部署双模式 Docker Compose 实际验证（Phase 6）。
- 单进程模式 `src/api/main.py` 删除——保留为兼容入口（默认单进程全量模式）。

## 路由分配（§5.5 + §10）

| 路由文件 | Mgmt :38080 | Event :38081 | Worker :38082 | Insight :38083 |
|---------|-------------|--------------|---------------|----------------|
| mgmt 15 个用户面路由 + admin + git-repo | ✅ | — | — | — |
| issues.py | GET/PUT + 用户面 | POST 入队 | — | — |
| chat.py | 全量用户面 | — | POST 执行端点 | — |
| events.py | GET/PUT/DELETE + 用户面 POST | POST terminate/redispatch | — | — |
| runtime.py | GET 只读（runtime_view 语义） | — | 全量（POST+GET） | — |
| tasks.py | — | — | 全量 | — |
| task_logs.py | ✅（SSE 代理） | — | — | — |
| webhook.py | — | ✅ | — | — |
| dashboard / observability / search / latency / health | ✅（用户面兜底） | — | — | ✅（nginx 主路由） |

> 说明：`runtime.py` 未物理拆分为 `runtime_view.py`，而是用方法过滤（GET-only include）
> 在 Mgmt 进程挂载只读端点，Worker 进程挂载全量——与设计“拆分两个 APIRouter 实例”
> 功能等价且不复制 handler；nginx 将所有 `/api/runtime/*` 路由到 Worker，
> Mgmt 挂载的只读副本仅作直连/内部兜底。

## 定时任务分配（§5.1~5.4）

- **Mgmt**：knowledge_sync_loop / knowledge_write_worker / skill_sync_scheduler /
  feedback_reconcile_scheduler / upload_cleanup_scheduler / cooldown_scheduler /
  active_user_tracker（含 session token 清理）/ lease_sweep_loop / IM WS BotManager /
  engine_probe / templates 自愈 / help.html / OpenAPI 契约落盘 / knowledge_migrate /
  D32 migration。
- **Event**：sweep_events_loop（15min）/ bot_token_health（5min）。
- **Worker**：local_claimer（3s）/ scheduler·autopilot（60s）/ health_refresh（5min）/
  local_probe（10min）/ reap_stale（60s）/ sweep_tasks_loop（15min）/
  table_maintenance（agent_tasks，6h）。
- **Insight**：obs_rollup（60s）/ obs_sample（60s）/ obs_alert + SLO（30s/300s）/
  server_metrics（15s）/ install_master_logging / trace stamp_missing /
  table_maintenance（task_messages，6h）。

## 关键设计决策

- **server_common 共享脚手架**：中间件（CORS/metrics/request_id+log）、异常处理、
  /api/ping、/health（liveness，按 service 名返回 `{"status":"ok","service":...}`）、
  /healthz+readyz（仅 Mgmt）、前端静态/SPA 托管（仅 Mgmt）由 server_common 提供；
  main.py 与四个新服务模块复用同一实现，避免 400+ 行重复。
- **server_tasks 共享任务启动器**：main.py 的 lifespan 大块任务代码原样迁入
  `server_tasks.start_*_tasks()` 四组函数；main.py lifespan 调用四组并集（单进程兼容），
  各进程服务只调用本组。保证单进程与四进程共用同一份任务实现（单一事实源）。
- **跨进程路由方法过滤**：`include_selected(app, router, methods, paths)` 基于
  APIRoute 复用按 HTTP method/路径子串挂载，不复制 handler、不改共享路由文件。
- **连续演进兼容**：`sweep_loop`/`sweep_once`、旧 `table_maintenance` 组合锁（`table_maintenance`）
  保留在单进程 main.py 路径；四进程用新拆分函数与新锁 key（互斥部署模式，不会同跑冲突）。
- **distributed_locks 分 key**：Worker 用 `table_maintenance_tasks`，Insight 用
  `table_maintenance_messages`（§9.5），避免两进程同 key 互等。

## 验收（TC-4.1~TC-4.12，设计 §Phase 4 验证用例）

- TC-4.1 四进程模块可导入、apps 入口可加载、/health 各返回 `{"status":"ok"}`。
- TC-4.2 Mgmt 进程用户面路由可达（/api/repos/member、/api/scenes、/api/health 等非 404）。
- TC-4.3 Event 进程 webhook + events 操作端点可达（/webhook/gitcode、/api/a2a/status）。
- TC-4.4 Worker 进程 /api/runtime/register、/api/runtime/heartbeat、/api/tasks/current 可达。
- TC-4.5 Insight 进程 /api/dashboard/ops/overview、/api/obs/rules、/api/search、/api/latency/report 可达。
- TC-4.6 nginx 四端口配置包含 4 个 upstream 与路径分流 location。
- TC-4.7 定时任务按进程分配：server_tasks 四组函数边界 + 各进程 lifespan 引用断言。
- TC-4.8 systemd 4 个 service 文件存在且端口/ExecStart 正确（部署级，静态校验）。
- TC-4.9 webhook→入队→认领→执行→回写→查看 全链路：单测覆盖 Event 入队端点 +
  Worker 认领端点 + local_claimer 认领函数链（部署级全链路由 Phase 6 双模式验证）。
- TC-4.10 SSE 任务日志流：Mgmt 注册 task_logs.router（/api/task-logs/{id}/stream 存在）。
- TC-4.11 四进程共用 distributed_locks：拆分后锁 key 互异（table_maintenance_tasks /
  table_maintenance_messages），原子 UPDATE 语义不变。
- TC-4.12 Insight 独立面板：/api/obs/* 路由仅在 Insight 进程注册 + nginx 分流（静态校验）。

## 风险与缓解

| 风险 | 等级 | 缓解 |
|------|------|------|
| main.py 脚手架重构回归 | 中 | 纯移动 + 全量路由/中间件顺序不变；跑 arch/回归批次/冒烟 |
| 跨进程路由方法过滤导致 endpoint 丢失 | 低 | include_selected 单测 + 每个进程断言关键路径存在 |
| 四进程同时访问 SQLite 冲突 | 中 | SQLite WAL + busy_timeout（设计既有一致性保障）；表维护/调度均走 distributed_locks |
| 拆分后单进程模式行为漂移 | 中 | main.py 保持“四组任务并集 + 原有路由”不退化为最小集 |
| 新文件落入 worker 包 | 低 | server 文件不在 MANIFEST 白名单（worker 包不包含服务端入口，守卫测试覆盖） |