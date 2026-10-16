# Spec: Issue #357 — 按照系统健康分析方案实施系统健康度修复

> 基准分支：`lvjuntao-clean` | 方案源：`docs/weekly-07/system-health-analysis-20261016.md`
> P0 生产参数项由 SRE 执行；P1/P2 代码项由 Coding Agent 实现（本 Spec）

## Objective

按已发布的系统健康分析方案分 P0/P1/P2 三级实施修复，消除"整体卡"根因
（全局 db_lock 串行 + 单进程 uvicorn + 连接 churn + 大表/索引/死锁隐患），
将全站平均延迟从 1208ms 降至 P1 后 <500ms、P2 后 <300ms。

## Scope

### In Scope (P1/P2 code items)

**P1（结构性修复）**
- P1-A: MySQLCompatDB 连接池化（DBUtils PooledDB，池 16~32，DB_POOL_DISABLED=1 回滚）
- P1-B: get_pipeline_config 进程级缓存（TTL 60s + SHOW COLUMNS 一次性 + 循环外批量化）
- P1-C: experience 外部调用出锁 + 10s 超时（asyncio.wait_for + asyncio.to_thread）
- P1-D: 定时任务分布式锁（scene_sync/skill_sync/upload_cleanup/autopilot 仅 1 worker 执行）
- P1-E: issues 列表响应瘦身（去 agent_statuses/task_description，latest_event 仅 4 字段）

**P2（两周内）**
- P2-A: user_credentials 死锁 DELETE 改低峰分批（LIMIT 100 循环 + 派生表包装）
- P2-B: events(repo_key, issue_id) 复合索引
- P2-C: agent_tasks completed>30d 大字段置空 + task_messages>90d 分批删除（定时维护任务）
- P2-D: grape-worker.tar.gz 改 nginx X-Accel-Redirect 静态直出（GRAPE_WORKER_NGINX_REDIRECT=1）
- P2-E: 前端 issues.js 轮询 15s→30s

### Out of Scope
- P0 生产参数项（performance_schema、慢日志阈值 0.5s）—— SRE 执行
- uvicorn --workers 部署参数 —— SRE 部署时配置
- api_latency_log 汇总看板与 CloudEye 告警 —— 后续迭代

## Acceptance Criteria
- V2-3: _query_tasks < 0.2s、接口 < 0.4s、响应体 < 200KB
- V2-5: experience 外部调用 10s 超时返回
- V2-6/V3-1: 连接 churn < 0.1/s（PooledDB 生效后）
- V3-2: agent_tasks completed 超 30 天大字段已置空
- V3-3: events 复合索引 EXPLAIN 走 idx_events_repo_issue
- V3-4: 分批 DELETE 无死锁
- V3-6: 轮询间隔 30s
- 所有现有测试通过；新增 14 个测试覆盖连接池/缓存/分布式锁/表维护
