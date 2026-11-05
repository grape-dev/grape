# Grape 融合实施方案 — 三域四进程 + 分 Schema + Monorepo

> **版本**：v2.2（chat 表归运行域 + GSB 统一服务通信框架 + 跨语言接入版）
> **日期**：2026-11-02
> **基础仓**：`/home/grape/grape-new-version2`
> **融合来源**：grape-three-schema-split-design.md、grape-three-process-split-design.md、grape-monorepo-design.md、grape-three-dimension-analysis.md
> **架构基线**：grape架构演进分析报告.html（Phase 0-4）
> **核心变更**：Insight 域从"暂合入 Mgmt 进程"升级为独立进程（grape_insight_server :38083），实现真正的三域四进程独立运行。

---

## 目录

1. [概述](#1-概述)
2. [当前结构问题](#2-当前结构问题)
3. [Monorepo 目录优化](#3-monorepo-目录优化)
4. [分 Schema 设计](#4-分-schema-设计)
5. [四进程设计](#5-四进程设计)
6. [_execute_agent 迁移](#6-_execute_agent-迁移)
7. [域间契约接口](#7-域间契约接口)
8. [sweep_loop 拆分](#8-sweep_loop-拆分)
9. [共享 DB 表协作](#9-共享-db-表协作)
10. [nginx 反代配置](#10-nginx-反代配置)
11. [部署双模式](#11-部署双模式)
12. [完整迁移路径](#12-完整迁移路径)
13. [风险与缓解](#13-风险与缓解)
14. [详细验证用例](#14-详细验证用例)

---

## 1. 概述

### 1.1 三域定义

Grape 系统按职责划分为三个域，每域一个独立进程组：

| 域 | 核心职责 | Schema | 进程数 |
|----|----------|--------|--------|
| **Mgmt（管理域）** | 用户交互、身份认证、组织管理、配置管理、数据展示 | grape_mgmt | 1 |
| **Runtime（运行域）** | Webhook 接收、事件处理、任务入队、Worker 执行 | grape_runtime | 2（Event + Worker） |
| **Insight（数据域）** | 可观测性、看板、SLO、告警、LLM 用量、延迟监控 | grape_insight | 1 |

### 1.2 四进程定义

| 进程 | 入口文件 | 端口 | 域 | uvicorn workers | 连接池 | 职责 |
|------|----------|------|-----|-----------------|--------|------|
| grape_server | grape_server.py | :38080 | Mgmt | 2 | min=2, max=16 | 用户面 API、身份认证、配置管理、IM WS、前端托管 |
| grape_event_server | grape_event_server.py | :38081 | Runtime | 1 | min=1, max=8 | Webhook 接收、事件归一化、路由判定、任务入队 |
| grape_worker_server | grape_worker_server.py | :38082 | Runtime | 2 | min=2, max=32 | 任务认领、Agent 执行（direct + remote）、心跳、结果回传 |
| grape_insight_server | grape_insight_server.py | :38083 | Insight | 1 | min=1, max=8 | 可观测性面板、SLO/告警、LLM 用量、延迟监控、全局搜索 |

### 1.3 设计原则

| 原则 | 说明 |
|------|------|
| **分 Schema 不分库** | 三个 schema 在同一 MySQL 实例，跨 schema 事务可用。SQLite 用表前缀模拟 |
| **DB 驱动协作** | 进程间通信通过 DB 表状态流转完成（B 写 `agent_tasks.queued` → C `claim` 抢单 → C `complete` 回写），不引入额外 RPC |
| **契约接口双模式** | 跨域数据访问通过 `src/domains/contracts/` 接口定义，支持 LocalProvider（跨 schema SQL）和 RemoteProvider（HTTP API）双模式，由 GRAPE_MODE/GRAPE_PROVIDER_MODE 环境变量切换，业务代码无感。 |
| **单机零依赖** | SQLite + 表前缀，四进程同主机，无 Redis/Kafka/etcd |
| **渐进式** | Phase 0-6 逐步演进，每阶段可独立验证和回滚 |
| **不引入消息中间件** | `agent_tasks` 表即任务队列，`claim_by_tag` 即消费端抢单 |

### 1.4 与 grape架构演进分析报告.html 对齐

| 架构 Phase | 本方案配合动作 | 依赖关系 |
|-----------|---------------|----------|
| Phase 0：域内聚 | src/ 重组为 domains/{mgmt,runtime,insight}/ + kernel/；check-arch CI 红牌 | 域目录是 Phase 0 的物理基础 |
| Phase 1：内核底座 | kernel/ 包就位；auth_sdk 下沉；db_migrations 版本化；config 净化 | kernel/ 目录已创建，逐步填充接口 |
| Phase 2：进程边界契约 | contracts/ 接口定义；apps/ 入口可独立加载路由 | apps/ 入口 + contracts/ 接口是代码基础 |
| Phase 3：三域独立进程 | 四入口独立启动；deploy/systemd/ service 文件；configs/profiles/ 双模式 | 目录结构已支持独立加载 |
| Phase 4：微服务治理 | kernel/ 接口替换为真实中间件；contracts/ 接口不变 | kernel/ 抽象层是 Phase 4 替换的基础 |

### 1.5 部署拓扑图

```
                    用户/浏览器    VCS Webhook    Worker 引擎    前端延迟上报
                         │              │              │              │
                    [nginx :443]
                    /    |    |    \
              :38080  :38081  :38082  :38083
                |       |       |       |
           Mgmt 进程  Event   Worker  Insight
          (2 workers)(1 wk)  (2 wk)  (1 wk)
                |       |       |       |
                +---+---+---+---+---+---+
                    |
             [MySQL 单实例]
             ├── grape_mgmt schema     (45 张表)
             ├── grape_runtime schema  (25 张表)
             └── grape_insight schema  (11 张表)

单机模式：SQLite 单文件 + 表前缀（mgmt_/rt_/ins_），四进程同主机
```

### 2.1 目录混乱（基于 weekly-09 代码分析）

基于 grape-dev/grape weekly-09 分支实际代码，当前仓库混合了多种职责：

```
grape/
├── src/              # 后端代码（~190 个 .py，112K 行）
│   ├── api/          # API 服务端（34 个 .py）
│   │   ├── routes/   # 29 个路由文件
│   │   ├── static/   # 前端静态资源（HTML/JS/CSS）
│   │   ├── event_pipeline.py  # 5000+ 行巨型文件
│   │   ├── main.py / run.py / run_webhook.py  # 三份手写组装入口
│   │   └── ...        # event_store, db_setup, deps, jwt_utils 等
│   ├── runtime/      # Worker 运行时（38 个 .py）
│   ├── agent/        # Agent 适配层（13 个 .py）
│   ├── auth/         # 认证体系（13 个 .py）
│   ├── vcs/          # VCS 适配（9 个 .py + providers/）
│   ├── observability/  # 可观测性（20 个 .py）
│   ├── im/           # IM 集成（8 个 .py）
│   ├── storage/      # 对象存储（6 个 .py）
│   ├── user/         # 用户管理（4 个 .py）
│   ├── admin/        # 管理接口（5 个 .py）
│   ├── project_info/ # 项目信息（2 个 .py）
│   └── 顶层 18 个共享模块  # config.py, session_manager.py, lock.py 等
├── admin/            # 空目录（旧版残留，内容已清空）
├── configs/          # 配置文件（27 个 JSON + scenes/）
├── contracts/        # API 契约
├── data/             # 运行时数据
├── deploy/           # 部署配置（仅 alerts.yml）
├── docs/             # 文档（898 个文件）
├── examples/         # 示例项目（10 个）
├── help/             # API 帮助文档
├── scenes/           # 场景定义（与 configs/scenes/ 重复，9 个场景目录）
├── scripts/          # 工具脚本（50+ 个）
├── skills/           # 技能库（185 个 SKILL.md，8 个 Agent 类型目录）
├── tasks/            # 任务计划文档（21 个 .md）
├── tests/            # 测试（445 .py + 78 .js）
├── wheels/           # 离线 wheel 包（10 个 .whl）
├── .grape/skills/    # 内置技能（qa/ai-coding-review/grape-architecture-guardian + qa/openspec-pr-reviewer）
├── .opencode/skills/ # OpenCode 技能（grape-dev-workflow）
├── .skills/          # CLI 技能（coding-cli）
├── requirements*.txt # 4 个依赖文件（96 行总计）
├── docker-compose.yml
├── nginx-grape.conf
├── start_server.py / start-worker.sh
└── README.md / README.zh.md / CONTRIBUTING.md / SECURITY.md
```

### 2.2 核心痛点

| 痛点 | 影响 | 严重度 |
|------|------|--------|
| **src/ 按技术分层不按域分层** | 无法支撑 Phase 0 域内聚改造 | 高 |
| **src/ 顶层 18 个共享模块与子模块平级** | config.py 被引用 130 次、session_manager.py 99 次，但没有独立包边界 | 高 |
| **4 套技能目录**（skills/ + .grape/skills/ + .opencode/skills/ + .skills/） | 重复维护、同步困难 | 中 |
| **event_pipeline.py 5000+ 行** | 路由判定、入队、执行、结果回写全在一个文件 | 高 |
| **85 张表无 schema 边界** | 跨域数据访问无约束，裸 SQL 散落各处 | 高 |
| **db_lock 跨进程约束** | SQLite `threading.Lock` 只在进程内有效，三进程各自持有独立实例 | 高 |
| **可观测性定时任务与用户请求争抢资源** | obs_rollup/obs_sample/obs_alert/server_metrics 等重计算任务与用户面 API 共进程 | 高 |
| scenes/ 与 configs/scenes/ 重复 | 9 个场景目录数据冗余、不一致风险 | 中 |
| admin/ 空目录残留 | 旧版内嵌 admin 内容已清空但目录未删 | 低 |
| tasks/ 21 个计划文档散落 | 与 docs/ 职责重叠 | 低 |
| 4 个 requirements*.txt 手动维护 | 依赖版本漂移（96 行分散在 4 个文件） | 中 |
| 无迁移目录 | DDL 散落 db_setup.py/user.db/admin.db 三处 | 中 |
| 无 check-arch CI | 依赖环无法自动检测 | 中 |
| wheels/ 10 个 .whl 入库 | 离线包应 .gitignore 不入库 | 低 |

---

## 3. Monorepo 目录优化

### 3.1 目标目录结构

```
grape/
├── apps/                             # 可独立运行的入口
│   ├── mgmt/                         # Mgmt 进程入口 (:38080)
│   │   ├── main.py                   # grape_server 启动
│   │   └── README.md
│   ├── runtime/                      # Runtime 域入口
│   │   ├── webhook/                  # Event 进程入口 (:38081)
│   │   │   ├── main.py               # grape_event_server 启动
│   │   │   └── README.md
│   │   └── worker/                   # Worker 进程入口 (:38082)
│   │       ├── main.py               # grape_worker_server 启动
│   │       └── README.md
│   └── insight/                      # Insight 进程入口 (:38083)
│       ├── main.py                   # grape_insight_server 启动
│       └── README.md
│
├── src/
│   ├── kernel/                       # 内核底座
│   │   ├── __init__.py
│   │   ├── config.py                 # 全局配置（原 src/config.py）
│   │   ├── session_manager.py        # DB 连接管理
│   │   ├── db.py                     # schema 管理（新增）
│   │   ├── lock.py                   # 分布式锁
│   │   ├── repo_key.py / repo.py / repo_cleanup.py / repo_migrate.py
│   │   ├── mysql_compat.py
│   │   ├── token_service.py / token_resolver.py
│   │   ├── prompt_builder.py / worker_package.py
│   │   ├── localfs.py / outbound_ledger.py
│   │   ├── scene_custom_env.py / featured_order.py
│   │   ├── help_docs_sync.py / help_page_gen.py
│   │   ├── db_migrations/            # 版本化迁移目录
│   │   │   ├── 001_initial.sql
│   │   │   ├── 002_grape_accounts.sql
│   │   │   └── ...
│   │   └── auth_sdk/                 # 鉴权 SDK
│   │       ├── __init__.py           # authenticate(request) 接口
│   │       ├── jwt_utils.py
│   │       ├── auth_token.py
│   │       └── cookie_utils.py
│   │
│   ├── domains/                      # 按域分层
│   │   ├── mgmt/                     # 管理域
│   │   │   ├── auth/                 # 认证体系（原 src/auth/）
│   │   │   ├── user/                 # 用户管理（原 src/user/）
│   │   │   ├── admin/                # 管理接口（原 src/admin/）
│   │   │   └── routes/               # 管理域路由
│   │   │       ├── grape_auth.py / auth.py / orgs.py
│   │   │       ├── settings.py / feedback.py
│   │   │       ├── scenes.py / skills.py / templates.py
│   │   │       ├── repos.py / autopilot.py / agents.py
│   │   │       ├── community.py / devstage.py
│   │   │       └── feishu_onboard.py / feishu_card.py
│   │   │
│   │   ├── runtime/                  # 运行域
│   │   │   ├── pipeline/             # 事件流水线
│   │   │   │   ├── event_pipeline.py # 事件处理（瘦身：_execute_agent 迁出）
│   │   │   │   ├── event_store.py / event_bus.py / event_archive.py
│   │   │   │   ├── event_rule.py / pipeline_config.py
│   │   │   │   ├── pipeline_executor.py / pipeline_status.py
│   │   │   │   ├── issues_center.py / session_store.py
│   │   │   │   ├── pr_merged_sre_retry.py
│   │   │   │   └── feedback_issue_converter.py
│   │   │   ├── engine/               # 引擎运行时（原 src/runtime/）
│   │   │   │   ├── direct_executor.py  # ← _execute_agent 迁移目标
│   │   │   │   ├── agent.py / service.py / local_claimer.py
│   │   │   │   ├── engine_select.py / executor.py
│   │   │   │   ├── health.py / health_scorer.py / scheduler.py
│   │   │   │   ├── cleanup.py / client.py / instances.py
│   │   │   │   ├── task_lock.py / task_log_stream.py
│   │   │   │   ├── context_compaction.py / usage_collect.py
│   │   │   │   ├── bot_token_health.py / checkpoint.py
│   │   │   │   ├── task_timeout.py / table_maintenance.py
│   │   │   │   └── local_probe.py
│   │   │   ├── agents/               # Agent 适配层（原 src/agent/）
│   │   │   ├── im/                   # IM 集成（原 src/im/）
│   │   │   ├── vcs/                  # VCS 适配（原 src/vcs/）
│   │   │   ├── storage/              # 对象存储（原 src/storage/）
│   │   │   ├── project_info/         # 项目信息
│   │   │   └── routes/               # 运行域路由
│   │   │       ├── webhook.py / events.py / issues.py
│   │   │       ├── pipeline.py / tasks.py
│   │   │       ├── runtime.py / chat.py
│   │   │       ├── task_logs.py
│   │   │       └── runtime_view.py   # runtime.py GET 端点（Mgmt 加载）
│   │   │
│   │   ├── insight/                  # 数据域
│   │   │   ├── observability/        # 可观测性（原 src/observability/）
│   │   │   │   ├── alerts.py / alert_policy.py
│   │   │   │   ├── engine_probe.py / events.py / events_view.py
│   │   │   │   ├── logs.py / slo.py
│   │   │   │   ├── metric_rollup.py / rollup.py / loops.py
│   │   │   │   ├── llm_usage.py / master_usage.py
│   │   │   │   ├── overview.py / redact_issue_body.py
│   │   │   │   ├── tokens.py / trace.py
│   │   │   │   └── ...
│   │   │   └── routes/               # 数据域路由
│   │   │       ├── dashboard.py      # 运维仪表盘
│   │   │       ├── observability.py  # 可观测性面板
│   │   │       ├── search.py         # 全局搜索
│   │   │       ├── latency.py        # 延迟上报
│   │   │       └── health.py         # 健康检查
│   │   │
│   │   └── contracts/                # 域间契约
│   │       ├── __init__.py
│   │       ├── mgmt_service.py       # MgmtService 接口
│   │       ├── runtime_service.py    # RuntimeService 接口
│   │       ├── insight_service.py    # InsightService 接口
│   │       └── event_types.py        # 领域事件类型定义
│   │
│   ├── api/                          # API Gateway 层（逐步瘦身）
│   │   ├── main.py / run.py / run_webhook.py
│   │   ├── deps.py / routing.py / db_setup.py
│   │   ├── knowledge.py / git_repo_api.py
│   │   └── static/                   # 前端静态资源
│   │
│   └── __init__.py                   # re-export 兼容层
│
├── .grape/skills/                    # 统一技能目录（按 Agent 类型分类）
│   ├── vod/                          # VOD 智能推荐技能
│   │   ├── blog-vod/
│   │   ├── vod/
│   │   ├── vod-cli/
│   │   ├── repo-onboarding/
│   │   ├── event-dialogue/
│   │   ├── iac-vod/
│   │   └── feedback-loop/
│   ├── issue/                        # Issue 处理技能
│   ├── coding/                       # 编码技能
│   │   └── coding-cli/              # 原 .skills/coding-cli/
│   ├── qa/                           # 质量保证技能
│   │   ├── code-quality/
│   │   ├── ai-coding-review/        # 原 .grape/skills/qa/ai-coding-review/
│   │   │   └── grape-architecture-guardian/
│   │   └── openspec-pr-reviewer/    # 原 .grape/skills/qa/openspec-pr-reviewer/
│   ├── sre/                          # SRE 运维技能
│   ├── repo/                         # 仓库管理技能
│   ├── chat/                         # 聊天技能
│   ├── _template/                    # 技能模板
│   ├── custom/                       # 用户自定义技能
│   └── dev-workflow/                 # 开发流程技能（原 .opencode/skills/grape-dev-workflow/）
│
├── configs/
│   ├── scenes/                       # 唯一场景来源
│   ├── profiles/                     # 部署双模式 profile
│   │   ├── single.conf               # 单机模式
│   │   └── cluster.conf              # 集群模式
│   └── *.json
│
├── deploy/
│   ├── nginx-grape.conf
│   ├── Dockerfile
│   ├── docker-compose.yml
│   ├── alerts.yml
│   └── systemd/
│       ├── grape-server.service
│       ├── grape-event-server.service
│       ├── grape-worker-server.service
│       └── grape-insight-server.service
│
├── tests/
│   ├── arch/                         # 架构约束测试
│   │   ├── test_no_circular_deps.py
│   │   └── test_domain_boundaries.py
│   └── ...
│
├── scripts/
│   ├── check-arch.py                 # 架构约束检查
│   └── ...
│
├── pyproject.toml                    # 统一依赖管理
├── Makefile                          # 统一构建入口
└── README.md
```

### 3.2 apps/ 下四个入口

| 入口 | 路径 | 端口 | 域 | 说明 |
|------|------|------|-----|------|
| mgmt | apps/mgmt/main.py | :38080 | Mgmt | `from src.api.grape_server import app` |
| runtime/webhook | apps/runtime/webhook/main.py | :38081 | Runtime(Event) | `from src.api.grape_event_server import app` |
| runtime/worker | apps/runtime/worker/main.py | :38082 | Runtime(Worker) | `from src.api.grape_worker_server import app` |
| insight | apps/insight/main.py | :38083 | Insight | `from src.api.grape_insight_server import app` |

### 3.3 src/ 顶层 16 个共享模块移入 src/kernel/

| 原位置 | 目标位置 | 引用次数 |
|--------|----------|----------|
| src/config.py | src/kernel/config.py | 130 |
| src/session_manager.py | src/kernel/session_manager.py | 99 |
| src/lock.py | src/kernel/lock.py | — |
| src/repo_key.py | src/kernel/repo_key.py | — |
| src/repo.py | src/kernel/repo.py | — |
| src/repo_cleanup.py | src/kernel/repo_cleanup.py | — |
| src/repo_migrate.py | src/kernel/repo_migrate.py | — |
| src/mysql_compat.py | src/kernel/mysql_compat.py | — |
| src/token_service.py | src/kernel/token_service.py | — |
| src/token_resolver.py | src/kernel/token_resolver.py | — |
| src/prompt_builder.py | src/kernel/prompt_builder.py | — |
| src/worker_package.py | src/kernel/worker_package.py | — |
| src/localfs.py | src/kernel/localfs.py | — |
| src/outbound_ledger.py | src/kernel/outbound_ledger.py | — |
| src/scene_custom_env.py | src/kernel/scene_custom_env.py | — |
| src/featured_order.py | src/kernel/featured_order.py | — |
| src/help_docs_sync.py | src/kernel/help_docs_sync.py | — |
| src/help_page_gen.py | src/kernel/help_page_gen.py | — |

同时下沉鉴权 SDK：`src/api/jwt_utils.py` → `src/kernel/auth_sdk/jwt_utils.py`、`src/api/auth_token.py` → `src/kernel/auth_sdk/auth_token.py`、`src/auth/cookie_utils.py` → `src/kernel/auth_sdk/cookie_utils.py`。

### 3.4 技能目录统一到 .grape/skills/

**现状**：4 套技能目录，按来源分散：
- skills/（185 个 SKILL.md）——按 Agent 类型分：vod/issue/coding/qa/sre/repo/chat/_template/custom
- .grape/skills/（2 个）——qa/ai-coding-review/grape-architecture-guardian、qa/openspec-pr-reviewer
- .opencode/skills/（1 个）——grape-dev-workflow
- .skills/（1 个）——coding-cli

**变更**：统一到 .grape/skills/ 下，按 Agent 类型分类（与 skills/ 原有分类对齐），不按来源分类：

| 原目录 | 目标位置 | 说明 |
|--------|----------|------|
| skills/vod/ | .grape/skills/vod/ | VOD 智能推荐 |
| skills/issue/ | .grape/skills/issue/ | Issue 处理 |
| skills/coding/ | .grape/skills/coding/ | 编码技能 |
| skills/qa/ | .grape/skills/qa/ | 质量保证 |
| skills/sre/ | .grape/skills/sre/ | SRE 运维 |
| skills/repo/ | .grape/skills/repo/ | 仓库管理 |
| skills/chat/ | .grape/skills/chat/ | 聊天技能 |
| skills/_template/ | .grape/skills/_template/ | 技能模板 |
| skills/custom/ | .grape/skills/custom/ | 用户自定义 |
| .grape/skills/qa/ai-coding-review/ | .grape/skills/qa/ai-coding-review/ | 原地不动（已在 qa/ 下） |
| .grape/skills/qa/openspec-pr-reviewer/ | .grape/skills/qa/openspec-pr-reviewer/ | 原地不动（已在 qa/ 下） |
| .opencode/skills/grape-dev-workflow/ | .grape/skills/dev-workflow/ | 归入 dev-workflow |
| .skills/coding-cli/ | .grape/skills/coding/coding-cli/ | 归入 coding/ |
| 顶层 skills/ | 删除 | 全部内容已迁入 .grape/skills/ |
| .opencode/skills/ | 删除 | 内容已迁入 .grape/skills/ |
| .skills/ | 删除 | 内容已迁入 .grape/skills/ |

统一后的 .grape/skills/ 目录结构：
```
.grape/skills/
├── vod/              # VOD 智能推荐（blog-vod, vod, vod-cli, repo-onboarding, event-dialogue, iac-vod, feedback-loop）
├── issue/            # Issue 处理
├── coding/           # 编码技能（含 coding-cli）
├── qa/               # 质量保证（code-quality, ai-coding-review/grape-architecture-guardian, openspec-pr-reviewer）
├── sre/              # SRE 运维
├── repo/             # 仓库管理
├── chat/             # 聊天技能
├── _template/        # 技能模板
├── custom/           # 用户自定义技能
└── dev-workflow/     # 开发流程技能
```

**设计理由**：按 Agent 类型分类而非按来源分类，因为：
1. skills/ 已有 vod/issue/coding/qa/sre/repo/chat 分类，这是 Grape 的 Agent 类型体系
2. .grape/skills/ 的内置技能也是按 Agent 类型归类的（qa/ai-coding-review、qa/openspec-pr-reviewer）
3. .opencode/skills/ 和 .skills/ 的技能可以自然归入对应类型（dev-workflow 独立、coding-cli 归 coding）
4. skill_sync.py 同步时按 Agent 类型扫描，分类方式与同步逻辑一致

需同步更新：`config.py SKILLS_DIR`（改为 .grape/skills/）、`skill_sync.py`、`scene_sync.py`、`worker_package.py MANIFEST`。

**注意**：运行时工作区中的 `.skills/`（event_pipeline.py 中 `ws/.skills/{skill_name}/`）是任务执行时动态创建的，不是仓库级技能目录，不受此变更影响。

### 3.5 删除重复目录

| 目录 | 操作 | 原因 |
|------|------|------|
| scenes/ | 删除 | 与 configs/scenes/ 重复 |
| admin/（顶层） | 删除 | 旧版残留 |
| tasks/ | 移入 docs/plans/ | 归入文档 |
| nginx-grape.conf / Dockerfile / docker-compose.yml | 移入 deploy/ | 部署文件归拢 |

### 3.6 pyproject.toml 统一依赖

合并 4 个 requirements*.txt 为 pyproject.toml + 可选依赖组：

```toml
[project]
name = "grape"
version = "2.2.0"
dependencies = [
    "fastapi>=0.100",
    "uvicorn[standard]>=0.23",
    "httpx",
    "aiosqlite",
    "pysqlite3-binary; python_version<'3.12'",
    # ... 核心依赖
]

[project.optional-dependencies]
mysql = ["pymysql", "cryptography"]
obs = ["obsuite"]
im = ["feishu-sdk"]
dev = ["pytest", "pytest-asyncio", "httpx", "playwright"]
```

### 3.7 域归属对照表

| 原目录 | 目标位置 | 域 |
|--------|----------|-----|
| src/config.py | src/kernel/config.py | kernel |
| src/session_manager.py | src/kernel/session_manager.py | kernel |
| src/lock.py | src/kernel/lock.py | kernel |
| src/api/jwt_utils.py | src/kernel/auth_sdk/jwt_utils.py | kernel |
| src/api/auth_token.py | src/kernel/auth_sdk/auth_token.py | kernel |
| src/auth/cookie_utils.py | src/kernel/auth_sdk/cookie_utils.py | kernel |
| src/auth/（其余） | src/domains/mgmt/auth/ | mgmt |
| src/user/ | src/domains/mgmt/user/ | mgmt |
| src/admin/ | src/domains/mgmt/admin/ | mgmt |
| src/api/routes/grape_auth.py | src/domains/mgmt/routes/ | mgmt |
| src/api/routes/orgs.py | src/domains/mgmt/routes/ | mgmt |
| src/api/routes/settings.py | src/domains/mgmt/routes/ | mgmt |
| src/api/routes/scenes.py | src/domains/mgmt/routes/ | mgmt |
| src/api/routes/skills.py | src/domains/mgmt/routes/ | mgmt |
| src/api/routes/repos.py | src/domains/mgmt/routes/ | mgmt |
| src/api/routes/community.py | src/domains/mgmt/routes/ | mgmt |
| src/api/routes/feedback.py | src/domains/mgmt/routes/ | mgmt |
| src/api/routes/feishu_onboard.py | src/domains/mgmt/routes/ | mgmt |
| src/api/routes/feishu_card.py | src/domains/mgmt/routes/ | mgmt |
| src/api/routes/autopilot.py | src/domains/mgmt/routes/ | mgmt |
| src/api/routes/agents.py | src/domains/mgmt/routes/ | mgmt |
| src/api/routes/templates.py | src/domains/mgmt/routes/ | mgmt |
| src/api/routes/devstage.py | src/domains/mgmt/routes/ | mgmt |
| src/api/event_pipeline.py | src/domains/runtime/pipeline/ | runtime |
| src/runtime/ | src/domains/runtime/engine/ | runtime |
| src/agent/ | src/domains/runtime/agents/ | runtime |
| src/im/ | src/domains/runtime/im/ | runtime |
| src/vcs/ | src/domains/runtime/vcs/ | runtime |
| src/storage/ | src/domains/runtime/storage/ | runtime |
| src/api/routes/webhook.py | src/domains/runtime/routes/ | runtime |
| src/api/routes/events.py | src/domains/runtime/routes/ | runtime |
| src/api/routes/issues.py | src/domains/runtime/routes/ | runtime |
| src/api/routes/runtime.py | src/domains/runtime/routes/ | runtime |
| src/api/routes/tasks.py | src/domains/runtime/routes/ | runtime |
| src/api/routes/chat.py | src/domains/runtime/routes/ | runtime |
| src/api/routes/pipeline.py | src/domains/runtime/routes/ | runtime |
| src/api/routes/task_logs.py | src/domains/runtime/routes/ | runtime |
| src/observability/ | src/domains/insight/observability/ | insight |
| src/api/routes/dashboard.py | src/domains/insight/routes/ | insight |
| src/api/routes/observability.py | src/domains/insight/routes/ | insight |
| src/api/routes/search.py | src/domains/insight/routes/ | insight |
| src/api/routes/latency.py | src/domains/insight/routes/ | insight |
| src/api/routes/health.py | src/domains/insight/routes/ | insight |
| 顶层 16 个共享模块 | src/kernel/ | kernel |
| skills/ | .grape/skills/{vod,issue,coding,qa,sre,...}/ | — | 技能统一到 .grape/skills/ 按 Agent 类型分类 |
| .grape/skills/ | .grape/skills/qa/（原地不动） | — | 已在正确位置 |
| .opencode/skills/ | .grape/skills/dev-workflow/ | — | 归入 dev-workflow |
| .skills/ | .grape/skills/coding/coding-cli/ | — | 归入 coding |
| scenes/ | 删除 | — |
| admin/（顶层） | 删除 | — |
| tasks/ | docs/plans/ | — |

---

## 4. 分 Schema 设计

### 4.1 grape_mgmt schema（45 张表）— 管理域

| # | 表名 | 说明 | 跨域访问方 |
|---|------|------|-----------|
| 1 | grape_accounts | 用户账号 | Runtime 读（user_id 关联） |
| 2 | external_identities | 第三方身份绑定 | — |
| 3 | verification_codes | 验证码 | — |
| 4 | signup_whitelist | 注册白名单 | — |
| 5 | login_whitelist | 登录白名单 | — |
| 6 | login_failures | 登录失败记录 | — |
| 7 | exchange_codes | OAuth exchange code | — |
| 8 | binding_tickets | 账号绑定票据 | — |
| 9 | audit_logs | 审计日志 | — |
| 10 | admin_accounts | 管理员账号 | — |
| 11 | api_tokens | API Key | Runtime 读（鉴权） |
| 12 | session_api_tokens | 会话级临时 token | Runtime 读/写（Worker 鉴权） |
| 13 | organization | 组织 | Runtime 读（org_id 隔离） |
| 14 | org_member | 组织成员 | Runtime 读 |
| 15 | org_invitation | 组织邀请 | — |
| 16 | managed_repos | 托管仓库 | Runtime 读（webhook_token, org_id） |
| 17 | user_repos | 用户仓库缓存 | — |
| 18 | runtime_credentials | Worker 凭据 | Runtime 读/写（注册/心跳） |
| 19 | devops_agent_session | Agent 配置会话 | Runtime 读（路由判定） |
| 20 | devops_pipeline_scene | 仓库场景配置 | Runtime 读（场景节点） |
| 21 | scene_market | 场景市场 | — |
| 22 | scene_category | 场景分类 | — |
| 23 | scene_likes | 场景点赞 | — |
| 24 | scene_skill_ref | 场景-技能关联 | — |
| 25 | scene_engine_detach_log | 引擎脱离日志 | — |
| 26 | skill | 技能 | Runtime 读（技能加载） |
| 27 | skill_category | 技能分类 | — |
| 28 | skill_likes | 技能点赞 | — |
| 29 | featured_templates | 推荐模板 | — |
| 30 | vod_conversations | VOD 对话 | — |
| 31 | community_posts | 社区帖子 | — |
| 32 | community_comments | 社区评论 | — |
| 33 | community_post_likes | 社区点赞 | — |
| 34 | user_feedback | 用户反馈 | — |
| 35 | uploaded_files | 上传文件 | — |
| 36 | knowledge_write_queue | 知识写入队列 | — |
| 37 | feishu_onboard_task | 飞书入驻任务 | — |
| 38 | global_im_bot_config | IM Bot 配置 | Runtime 读（IM 集成） |
| 39 | grape_im_instance | IM 实例 | — |
| 40 | bot_seen_messages | Bot 消息去重 | Runtime 读/写 |
| 41 | repo_im_group_binding | 仓库-IM 群绑定 | — |
| 42 | autopilot_tasks | Autopilot 定时任务定义 | Runtime 读（调度） |
| 43 | autopilot_executions | Autopilot 执行记录 | Runtime 写 |
| 44 | autopilot_subscriptions | Autopilot 订阅 | — |
| 45 | oauth_states | OAuth 状态 | — |
| — | user_credentials | 用户凭据 | — |
| — | user_accounts | 旧版用户账号（兼容） | — |
| — | outbound_content_ledger | 代发内容台账 | Runtime 读/写 |
| — | grape_t_credential_reveal_log | 凭据查看日志 | — |
| — | grape_t_schema_migrations | Schema 迁移记录 | — |
| — | unified_user_mapping | 统一用户映射 | — |

> 注：编号 1-48 为核心表，"—"编号的为附加/兼容表，总计约 55 张归属于管理域。

### 4.2 grape_runtime schema（25 张表）— 运行域

| # | 表名 | 说明 | 跨域访问方 |
|---|------|------|-----------|
| 1 | events | 事件记录 | Mgmt 读（用户面展示）、Insight 聚合 |
| 2 | event_skipped | 被跳过事件 | Mgmt 读 |
| 3 | event_active_guard | 事件活跃守卫 | — |
| 4 | event_thread | 事件线程 | Mgmt 读 |
| 5 | event_action_filter | 事件动作过滤 | — |
| 6 | event_route_config | 事件路由配置 | — |
| 7 | agent_tasks | 任务队列 | Mgmt 读（任务列表）、Insight 聚合 |
| 8 | task_messages | 任务消息 | Mgmt 读（SSE 流） |
| 9 | issue | Issue 记录 | Mgmt 读/写（用户操作） |
| 10 | issue_state | Issue 阶段状态 | Mgmt 读/写（状态流转） |
| 11 | issue_agent_session | Issue 级 Agent 会话 | — |
| 12 | pipeline_instances | 流水线实例 | Mgmt 读 |
| 13 | agent_runtimes | 引擎注册 | Mgmt 读（引擎列表）、Insight 读 |
| 14 | engine_capabilities | 引擎标签能力 | — |
| 15 | agent_type_runtime_pool | Agent 类型→引擎池 | — |
| 16 | agent_shared_knowledge | 共享知识 | — |
| 17 | scene_knowledge_ref | 场景知识引用 | — |
| 18 | agent_type | Agent 类型定义 | — |
| 19 | backend_agents | 旧版后端 Agent | — |
| 20 | distributed_locks | 分布式锁 | 四进程共用 |
| 21 | meta | 元数据键值对 | Mgmt 读 |
| 22 | devops_agent_session（runtime 视图） | Agent 会话（运行态） | — |
| 23 | chat_conversations | 聊天对话 | Mgmt 读（用户面展示） |
| 24 | chat_messages | 聊天消息 | Mgmt 读（SSE 流）、Insight 聚合 |
| 25 | chat_agent_session | 聊天 Agent 会话 | — |

### 4.3 grape_insight schema（11 张表）— 数据域

| # | 表名 | 说明 | 跨域访问方 |
|---|------|------|-----------|
| 1 | obs_log | 可观测性日志 | Runtime 写日志、Mgmt 读（日志面板） |
| 2 | obs_metric_rollup | 指标聚合 | Mgmt 读 |
| 3 | obs_runtime_samples | 设备资源采样 | Mgmt 读 |
| 4 | obs_slo | SLO 定义 | Mgmt 读 |
| 5 | obs_slo_snapshots | SLO 快照 | Mgmt 读 |
| 6 | obs_alert_rules | 告警规则 | Mgmt 读/写 |
| 7 | obs_alert_events | 告警事件 | Mgmt 读 |
| 8 | obs_llm_usage | LLM 用量 | Mgmt 读 |
| 9 | obs_llm_usage_archive | LLM 用量归档 | — |
| 10 | api_latency_log | API 延迟日志 | Mgmt 写（前端上报） |
| 11 | runtime_health_scores | 引擎健康分 | Mgmt + Runtime 读 |

### 4.4 跨域共享表分析（12 张）

12 张表同时有 org_id + repo_key，是跨域协作的关键：

| 共享表 | 写入方 | 读取方 | 分 Schema 后的访问方式 |
|--------|--------|--------|----------------------|
| managed_repos | Mgmt 写 | Runtime 读（B 校验 token） | Runtime 通过 MgmtService.get_repo_webhook_config() |
| devops_agent_session | Mgmt 写 | Runtime 读（B 路由判定） | Runtime 通过 MgmtService.get_agent_session() |
| devops_pipeline_scene | Mgmt 写 | Runtime 读（场景节点） | Runtime 通过 MgmtService.get_scene_config() |
| runtime_credentials | Mgmt 写 | Runtime 读/写（注册/心跳） | Runtime 直接写 grape_mgmt（同实例跨 schema） |
| agent_runtimes | Runtime 写 | Mgmt 读（引擎列表） | Mgmt 通过 RuntimeService.get_runtimes() |
| agent_tasks | Runtime 写（B 入队） | Mgmt 读（任务列表） | Mgmt 通过 RuntimeService.get_tasks() |
| events | Runtime 写（B） | Mgmt 读（展示）、Insight 聚合 | Mgmt 通过 RuntimeService.get_events() |
| issue | Runtime 写 | Mgmt 读/写 | Mgmt 直接读写 grape_runtime（同实例跨 schema） |
| issue_state | Runtime 写 | Mgmt 读/写 | Mgmt 通过 RuntimeService 操作 |
| task_messages | Runtime 写（C） | Mgmt 读（SSE 流） | Mgmt 通过 RuntimeService.get_task_messages() |
| session_api_tokens | Mgmt 写 | Runtime 读/写 | Runtime 直接读写 grape_mgmt |
| chat_conversations | Runtime 写 | Mgmt 读（用户面展示） | Mgmt 通过 RuntimeService.get_chat_conversations() |

### 4.5 SQLite 兼容方案

SQLite 不支持 schema。单机模式下用表前缀模拟：

```python
# src/kernel/db.py
"""内核 DB 访问层——管理三个 schema 的连接"""
import os

# schema 名常量
SCHEMA_MGMT = "grape_mgmt"
SCHEMA_RUNTIME = "grape_runtime"
SCHEMA_INSIGHT = "grape_insight"

def table(schema: str, table_name: str) -> str:
    """返回带 schema 前缀的完整表名"""
    backend = os.environ.get("DEVOPS_DB_BACKEND", "sqlite")
    if backend == "sqlite":
        # SQLite 不支持 schema，用前缀模拟
        prefix = {"grape_mgmt": "mgmt_", "grape_runtime": "rt_", "grape_insight": "ins_"}
        return f"{prefix.get(schema, '')}{table_name}"
    else:
        return f"{schema}.{table_name}"

# 便捷函数
def t_mgmt(table_name: str) -> str:
    return table(SCHEMA_MGMT, table_name)

def t_runtime(table_name: str) -> str:
    return table(SCHEMA_RUNTIME, table_name)

def t_insight(table_name: str) -> str:
    return table(SCHEMA_INSIGHT, table_name)
```

迁移辅助——所有 SQL 中的表名改为通过 `t_mgmt()` / `t_runtime()` / `t_insight()` 调用：

```python
# 变更前
c.execute("SELECT * FROM managed_repos WHERE repo_key=?", (repo_key,))

# 变更后
from src.kernel.db import t_mgmt
c.execute(f"SELECT * FROM {t_mgmt('managed_repos')} WHERE repo_key=?", (repo_key,))
```

### 4.6 kernel/db.py schema 管理代码

完整的 schema 管理包括：
- `table(schema, name)` — 表名前缀/限定
- `t_mgmt(name)` / `t_runtime(name)` / `t_insight(name)` — 便捷函数
- `ensure_schemas()` — MySQL `CREATE SCHEMA IF NOT EXISTS` / SQLite 表前缀兼容
- `migrate_table_to_schema(table, target_schema)` — 迁移单表到目标 schema

---

## 5. 四进程设计

### 5.1 Mgmt 进程（grape_server.py :38080）

```python
# apps/mgmt/main.py → src/api/grape_server.py
"""Mgmt 进程——工作台服务"""
from fastapi import FastAPI
from contextlib import asynccontextmanager

@asynccontextmanager
async def lifespan(app: FastAPI):
    # Mgmt 定时任务
    start_knowledge_sync()           # 知识库同步（每小时）
    start_skill_sync()               # 技能同步（30min）
    start_active_user_tracker()      # 用户活跃追踪（30s）
    start_cooldown_scheduler()       # 冷静期调度（每天）
    start_feedback_reconcile()       # 反馈→Issue 转换
    start_upload_cleanup()           # 上传文件清理（每小时）
    start_lease_sweep()              # IM Bot lease 清理
    # IM WS BotManager 启动
    # 安全门禁 / 模板自愈 / help.html
    yield

app = FastAPI(title="Grape Mgmt Server", version="2.2.0", lifespan=lifespan)

# 注册管理域路由（25 个用户面路由）
from src.domains.mgmt.routes import (
    grape_auth, auth, orgs, settings, feedback,
    community, feishu_onboard, feishu_card,
    scenes, skills, templates, repos, autopilot, agents, devstage,
)
for router in [grape_auth.router, auth.router, orgs.router, ...]:
    app.include_router(router, prefix="/api")

# 注册运行域只读路由（GET 端点）
from src.domains.runtime.routes import runtime_view, task_logs
app.include_router(runtime_view.router)       # runtime GET 端点
app.include_router(task_logs.router)          # 任务日志查看/SSE

# issues/events 的 GET 端点（用户面查看）
# issues.py: Mgmt 注册 GET/PUT
# events.py: Mgmt 注册 GET /detail/{seq}

# 静态文件（前端）
app.mount("/", StaticFiles(...))
```

**配置**：
- uvicorn workers: 2（用户请求并发）
- 连接池: min=2, max=16
- 访问 schema: grape_mgmt（读写）+ grape_runtime（通过 RuntimeService 只读）

**路由清单（25 个用户面路由 + admin + git-repo）**：

| # | 路由文件 | prefix | 职责 |
|---|---------|--------|------|
| 1 | auth.py | /api/auth | JWT 刷新、用户信息、登出 |
| 2 | grape_auth.py | /api/auth | 注册/登录/OAuth 回调 |
| 3 | repos.py | /api/repos | 仓库托管、Bot Token 管理 |
| 4 | dashboard.py | /api/dashboard | 运维仪表盘、仓库面板 |
| 5 | agents.py | /api/agents | Agent 配置查看与修改 |
| 6 | chat.py | /api/chat | 聊天对话、SSE 流式发送 |
| 7 | settings.py | /api/settings | 凭据管理、API Key、引擎设备列表 |
| 8 | skills.py | /api/skills | 技能市场管理 |
| 9 | search.py | /api/search | 全局搜索 |
| 10 | pipeline.py | /api/pipeline | 仓库流水线展示 |
| 11 | scenes.py | /api/scenes | 场景管理、场景市场 |
| 12 | templates.py | /api/project-templates | 项目模板 |
| 13 | issues.py (GET/PUT) | /api/issues | Issue 生命周期、需求中心 |
| 14 | feishu_onboard.py | /api/feishu/onboard | 飞书扫码入驻 |
| 15 | feishu_card.py | /api/feishu/card-action | 飞书卡片回调 |
| 16 | task_logs.py | /api/task-logs | 任务日志查看、SSE 流 |
| 17 | knowledge.py | /api/knowledge | 知识库 CRUD |
| 18 | health.py | (无 prefix) | 健康检查 |
| 19 | devstage.py | /api/devstage | DevStage 开发环境 |
| 20 | latency.py | /api/latency | 前端延迟上报 |
| 21 | feedback.py | /api | 用户反馈 |
| 22 | autopilot.py | /api/autopilot | Autopilot 定时任务管理（用户面 CRUD） |
| 23 | orgs.py | /api/orgs | 组织管理 |
| 24 | community.py | /api/community | 社区帖子、评论 |
| 25 | runtime_view.py | /api/runtime | runtime GET 端点（machine-health/tasks/install） |
| — | admin/routes.py | /api/admin | 管理后台 |
| — | git_repo_api.py | /api/git-repo | Git 仓库统一接口 |

**定时任务清单**：

| 定时任务 | 频率 | 模块 |
|---------|------|------|
| knowledge_sync_loop | 每小时 | src.api.knowledge |
| knowledge_write_worker | 持续 | src.api.knowledge |
| skill_sync_scheduler | 30min | src.storage.skill_sync |
| feedback_reconcile_scheduler | 定时 | src.api.feedback_issue_converter |
| upload_cleanup_scheduler | 每小时 | src.storage.upload_cleanup |
| cooldown_scheduler | 每天 | src.admin.cooldown |
| active_user_tracker | 30s | 自定义 |
| lease_sweep_loop | 循环 | bot_manager |
| knowledge_migrate | 一次性 | src.api.db_setup |
| D32 migration | 一次性 | scripts.migrate_user_accounts_to_grape |

### 5.2 Event 进程（grape_event_server.py :38081）

```python
# apps/runtime/webhook/main.py → src/api/grape_event_server.py
"""Event 进程——Git 仓库事件处理"""
from fastapi import FastAPI
from contextlib import asynccontextmanager

@asynccontextmanager
async def lifespan(app: FastAPI):
    init_database()
    _load_accounts()
    # Event 定时任务
    start_sweep_events()           # 僵尸事件回收（15min）
    start_bot_token_health()       # Bot Token 健康探活（5min）
    yield

app = FastAPI(title="Grape Event Server", version="2.2.0", lifespan=lifespan)

# 注册事件处理路由
from src.domains.runtime.routes import webhook, events
app.include_router(webhook.router)
app.include_router(events.router, prefix="/api")
```

**配置**：
- uvicorn workers: 1（事件处理强串行，event_pipeline 用线程池并行处理）
- 连接池: min=1, max=8
- 访问 schema: grape_runtime（读写）+ grape_mgmt（通过 MgmtService 只读）

**路由清单**：

| # | 路由文件 | prefix | 职责 |
|---|---------|--------|------|
| 1 | webhook.py | (无 prefix) | Webhook 接收（/webhook/gitcode、/webhook/github）、A2A 交接（/api/a2a） |
| 2 | events.py（操作端点） | /api/events | POST /{seq}/terminate、POST /{seq}/redispatch |
| 3 | issues.py（POST 入队） | /api/issues | POST /{owner}/{repo}（事件入队） |

**定时任务清单**：

| 定时任务 | 频率 | 模块 |
|---------|------|------|
| sweep_events_loop | 15min | src.runtime.cleanup.sweep_events_loop |
| bot_token_health | 5min | src.runtime.bot_token_health |

### 5.3 Worker 进程（grape_worker_server.py :38082）

```python
# apps/runtime/worker/main.py → src/api/grape_worker_server.py
"""Worker 进程——Worker 执行态"""
from fastapi import FastAPI
from contextlib import asynccontextmanager

@asynccontextmanager
async def lifespan(app: FastAPI):
    # Worker 定时任务
    start_local_claimer()          # 本机引擎认领（3s）
    start_scheduler()              # Autopilot 调度（60s）
    start_health_refresh()         # 健康度刷新（5min）
    start_local_probe()            # 本机探测（10min）
    start_reap_stale()             # 僵尸引擎回收（60s）
    start_sweep_tasks()            # 僵尸任务回收（15min）
    start_table_maintenance_tasks() # 表维护 agent_tasks 部分（6h）
    yield

app = FastAPI(title="Grape Worker Server", version="2.2.0", lifespan=lifespan)

# 注册 Worker API 路由
from src.domains.runtime.routes import runtime, tasks
app.include_router(runtime.router, prefix="/api/runtime")
app.include_router(tasks.router, prefix="/api")
```

**配置**：
- uvicorn workers: 2（任务执行可并行，内部 `_DIRECT_POOL` max 20）
- 连接池: min=2, max=32
- 访问 schema: grape_runtime（读写）+ grape_mgmt（通过 MgmtService 读写 session_api_tokens 等）

**路由清单**：

| # | 路由文件 | prefix | 职责 |
|---|---------|--------|------|
| 1 | runtime.py（POST 端点） | /api/runtime | Worker 注册、心跳、任务认领、执行回调、结果上报 |
| 2 | tasks.py | /api/tasks | Agent 自报完成、通知、当前任务自省 |
| 3 | chat.py（执行端点） | /api/chat | POST /send/stream（Worker 执行聊天任务） |

**定时任务清单**：

| 定时任务 | 频率 | 模块 |
|---------|------|------|
| local_claimer | 3s | src.runtime.local_claimer |
| scheduler/autopilot | 60s | src.runtime.scheduler |
| health_refresh | 5min | src.runtime.health_scorer |
| local_probe | 10min | src.runtime.local_probe |
| reap_stale | 60s | src.runtime.service.reap_stale_runtimes |
| sweep_tasks_loop | 15min | src.runtime.cleanup.sweep_tasks_loop |
| table_maintenance (agent_tasks) | 6h | cleanup_old_agent_tasks_fields + delete_old_agent_tasks |

### 5.4 Insight 进程（grape_insight_server.py :38083）— 新增独立进程

```python
# apps/insight/main.py → src/api/grape_insight_server.py
"""Insight 进程——可观测性、看板、SLO、告警"""
from fastapi import FastAPI
from contextlib import asynccontextmanager

@asynccontextmanager
async def lifespan(app: FastAPI):
    # Insight 定时任务
    start_obs_rollup()             # 指标聚合（60s）
    start_obs_sample()             # 设备资源采样（60s）
    start_obs_alert()              # 告警求值（30s）
    start_server_metrics()         # 主机指标采集（15s）
    start_install_master_logging() # 主控 WARNING/ERROR 落库（持续）
    start_table_maintenance_msgs() # task_messages 清理（6h）
    yield

app = FastAPI(title="Grape Insight Server", version="2.2.0", lifespan=lifespan)

# 注册数据域路由
from src.domains.insight.routes import (
    dashboard, observability, search, latency, health,
)
for router in [dashboard.router, observability.router, search.router,
               latency.router, health.router]:
    app.include_router(router, prefix="/api")
```

**配置**：
- uvicorn workers: 1（可观测性计算为主，无需高并发）
- 连接池: min=1, max=8
- 访问 schema: grape_insight（读写）+ grape_runtime（通过 RuntimeService 只读聚合）

**路由清单**：

| # | 路由文件 | prefix | 职责 |
|---|---------|--------|------|
| 1 | dashboard.py | /api/dashboard | 运维仪表盘、仓库面板 |
| 2 | observability.py | /api/obs | 可观测性面板、告警规则、SLO |
| 3 | search.py | /api/search | 全局搜索 |
| 4 | latency.py | /api/latency | 前端延迟上报 |
| 5 | health.py | (无 prefix) | 健康检查 |

**定时任务清单**：

| 定时任务 | 频率 | 模块 |
|---------|------|------|
| obs_rollup_loop | 60s | src.observability.metric_rollup |
| obs_sample_loop | 60s | src.observability.rollup |
| obs_alert_loop | 30s | src.observability.alerts |
| server_metrics_loop | 15s | 自定义（psutil 采集） |
| install_master_logging | 持续 | src.observability.logs |
| table_maintenance (task_messages) | 6h | cleanup_old_task_messages |

**关键变更说明**：原方案中 Insight 域的定时任务（obs_rollup、obs_sample、obs_alert、server_metrics、install_master_logging、table_maintenance task_messages 部分）合并在 Mgmt 进程中运行。本方案将其独立到 Insight 进程，解决**可观测性定时任务与用户请求争抢资源**的核心痛点。

### 5.5 跨进程路由处理

部分路由文件需要跨进程注册——不同进程加载同一路由文件的不同端点：

| 路由文件 | Mgmt (:38080) | Event (:38081) | Worker (:38082) | Insight (:38083) | 说明 |
|---------|---------------|----------------|-----------------|------------------|------|
| issues.py | GET/PUT | POST 入队 | — | — | Mgmt 注册用户面查看/操作；Event 注册事件入队 |
| chat.py | GET/POST 用户面 | — | POST 执行端点 | — | Mgmt 发起聊天；Worker 执行流式响应 |
| events.py | GET /detail/{seq} | POST terminate/redispatch | — | — | Mgmt 查看；Event 操作 |
| runtime.py | GET（runtime_view.py） | — | POST | — | Mgmt 查看 install/machine-health；Worker POST 端点 |

**实现方式**：通过 router 的 include 过滤或拆分为两个 APIRouter 实例。例如 `runtime.py` 拆分为 `runtime.py`（POST 端点，Worker 加载）和 `runtime_view.py`（GET 端点，Mgmt 加载）。

---

## 6. _execute_agent 迁移

### 6.1 当前位置

`_execute_agent` 及其相关函数当前位于 `src/api/event_pipeline.py`（Event 维度核心文件，5000+ 行），但其本质是 Worker 维度的 direct 执行路径。

| 函数/变量 | 位置 | 行号 | 说明 |
|-----------|------|------|------|
| `_DIRECT_POOL` | event_pipeline.py | L13-16 | 有界线程池（max_workers=20） |
| `_DIRECT_FUTURES` | event_pipeline.py | L18 | `{event_seq: Future}` 映射 |
| `_DIRECT_FUTURES_LOCK` | event_pipeline.py | L19 | Futures 字典锁 |
| `start_direct_agent` | event_pipeline.py | L348-356 | 提交任务到 `_DIRECT_POOL` |
| `_execute_agent` | event_pipeline.py | L3668-3704 | 顶层 try/except 兜底 + finally 清理 |
| `_execute_agent_inner` | event_pipeline.py | L3816+ | 实际执行逻辑（构建 prompt → stream_agent → 结果回写） |

### 6.2 迁移目标

**新建文件**：`src/domains/runtime/engine/direct_executor.py`

### 6.3 迁移内容

```python
# src/domains/runtime/engine/direct_executor.py

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

### 6.4 调用链变更前后对比

**迁移前（当前）**：

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

**迁移后**：

```
event_pipeline.py (Event 进程):
  run_pipeline()
    └── 所有路径 → enqueue_task() → agent_tasks(status=queued)
        （不再调用 start_direct_agent / _execute_agent）

direct_executor.py (Worker 进程):
  start_direct_agent(fn, args)
    └── _DIRECT_POOL.submit → _execute_agent → _execute_agent_inner

local_claimer.py (Worker 进程):
  _execute_claimed_locally()
    └── from src.domains.runtime.engine.direct_executor import _execute_agent, start_direct_agent
        └── start_direct_agent(_execute_agent, ...)
```

### 6.5 5 处调用点改为 enqueue_task

| 行号 | 当前逻辑 | 迁移后 |
|------|---------|--------|
| L1091 | `start_direct_agent(_execute_agent, ...)` | `enqueue_task(...)` （status=queued，等待 Worker 认领） |
| L1193 | `start_direct_agent(_execute_agent, ...)` | `enqueue_task(...)` |
| L1327 | `start_direct_agent(_execute_agent, ...)` | `enqueue_task(...)` |
| L2589 | `start_direct_agent(_execute_agent, ...)` | `enqueue_task(...)` |
| L4978 | `start_direct_agent(_execute_agent, ...)` | `enqueue_task(...)` |

**关键变更**：`runtime_execution_kind` 返回 `EXEC_MODE_DIRECT` 时，不再走 `start_direct_agent`，而是走 `enqueue_task` 入队。Worker 进程的 `local_claimer` 会在 3s 内认领并执行。

> **注意**：这会引入约 3s 的认领延迟（原 direct 是立即执行）。对于用户面手动触发（如 chat send）的场景，可接受。如果需要零延迟，Worker 的 `local_claimer` 可通过 `affinity_runtime_id` 优先认领。

### 6.6 local_claimer 改 import 路径

```python
# src/domains/runtime/engine/local_claimer.py — 修改 import
# 迁移前:
from src.api.event_pipeline import _execute_agent, start_direct_agent

# 迁移后:
from src.domains.runtime.engine.direct_executor import _execute_agent, start_direct_agent
```

### 6.7 影响的调用链

| 调用方 | 当前 import 来源 | 迁移后 import 来源 | 影响 |
|--------|------------------|-------------------|------|
| `event_pipeline.run_pipeline` | 自身（同文件） | 删除 direct 调用，改用 `enqueue_task` | 逻辑变更 |
| `event_pipeline._dispatch_pr_subscription_extra` | 自身 | 删除 direct 调用 | 逻辑变更 |
| `event_pipeline._on_handoff_dispatch` | 自身 | 删除 direct 调用 | 逻辑变更 |
| `local_claimer._execute_claimed_locally` | `from src.api.event_pipeline` | `from src.domains.runtime.engine.direct_executor` | import 路径变更 |
| `cleanup.sweep_stuck_events_once` | 无直接依赖 | 需从 `direct_executor` import `_DIRECT_FUTURES` | import 路径变更 |
| `event_pipeline._cancel_direct_future` | 自身 | 从 `direct_executor` import | import 路径变更 |

---

## 7. 域间契约接口

### 7.1 设计理念：双模式 Provider

域间契约接口采用 **Provider 抽象模式**——同一接口定义，两种实现可切换：

- **LocalProvider**（同实例模式）：直接执行跨 schema SQL，适用于单机/同实例部署
- **RemoteProvider**（HTTP API 模式）：通过 HTTP 调用目标域进程的 `/internal/*` 端点，适用于分库/跨实例部署

通过 `GRAPE_MODE` 环境变量 + 配置自动选择 Provider，业务代码无感切换：

```python
# src/kernel/provider.py
"""域间通信 Provider 工厂——根据部署模式自动选择实现"""

import os
from src.config import log

GRAPE_MODE = os.environ.get("GRAPE_MODE", "single")  # single | cluster

def get_mgmt_service():
    """获取 MgmtService 实现"""
    if GRAPE_MODE == "cluster":
        from src.domains.contracts.remote.mgmt_remote import MgmtServiceRemote
        return MgmtServiceRemote()
    else:
        from src.domains.contracts.local.mgmt_local import MgmtServiceLocal
        return MgmtServiceLocal()

def get_runtime_service():
    """获取 RuntimeService 实现"""
    if GRAPE_MODE == "cluster":
        from src.domains.contracts.remote.runtime_remote import RuntimeServiceRemote
        return RuntimeServiceRemote()
    else:
        from src.domains.contracts.local.runtime_local import RuntimeServiceLocal
        return RuntimeServiceLocal()

def get_insight_service():
    """获取 InsightService 实现"""
    if GRAPE_MODE == "cluster":
        from src.domains.contracts.remote.insight_remote import InsightServiceRemote
        return InsightServiceRemote()
    else:
        from src.domains.contracts.local.insight_local import InsightServiceLocal
        return InsightServiceLocal()
```

### 7.2 目录结构

```
src/domains/contracts/
├── __init__.py
├── mgmt_service.py              # MgmtService 抽象接口（Protocol）
├── runtime_service.py           # RuntimeService 抽象接口（Protocol）
├── insight_service.py           # InsightService 抽象接口（Protocol）
├── event_types.py               # 领域事件类型定义
├── local/                       # LocalProvider 实现（跨 schema SQL）
│   ├── __init__.py
│   ├── mgmt_local.py            # MgmtServiceLocal
│   ├── runtime_local.py         # RuntimeServiceLocal
│   └── insight_local.py         # InsightServiceLocal
└── remote/                      # RemoteProvider 实现（HTTP API）
    ├── __init__.py
    ├── mgmt_remote.py           # MgmtServiceRemote
    ├── runtime_remote.py        # RuntimeServiceRemote
    ├── insight_remote.py        # InsightServiceRemote
    └── http_client.py           # 共享 HTTP 客户端（连接池、超时、重试）
```

### 7.3 MgmtService 接口定义

```python
# src/domains/contracts/mgmt_service.py
"""管理域服务接口——供 Runtime 域调用"""
from typing import Protocol

class MgmtService(Protocol):
    """管理域服务接口——LocalProvider 走跨 schema SQL，RemoteProvider 走 HTTP API"""

    def get_repo_webhook_config(self, repo_key: str) -> dict:
        """B 校验 webhook 时调用——返回 webhook_token, bot_username, org_id"""
        ...

    def get_agent_session(self, repo_key: str, agent_type: str) -> dict:
        """B 路由判定时调用——返回 runtime_id, skill, skill_id"""
        ...

    def get_scene_config(self, repo_key: str) -> list:
        """B 读取场景节点——返回 nodes_json, work_branch"""
        ...

    def get_scene_pool_engine_ids(self, scene_id: str) -> list:
        """C 引擎选择时调用——返回场景绑定的引擎 ID 列表"""
        ...

    def verify_session_token(self, token_hash: str) -> dict:
        """C Worker 鉴权时调用——返回 credential_id, scope, owner_id"""
        ...

    def get_bot_config(self, repo_key: str) -> dict:
        """B/C 读取 Bot 配置——bot_token, bot_username"""
        ...
```

#### LocalProvider 实现

```python
# src/domains/contracts/local/mgmt_local.py
"""MgmtService LocalProvider——同实例跨 schema SQL 实现"""
from src.kernel.db import t_mgmt
from src.kernel.session_manager import db, db_lock

class MgmtServiceLocal:
    """同实例模式：直接查 grape_mgmt schema（SQLite 用 mgmt_ 前缀）"""

    def get_repo_webhook_config(self, repo_key: str) -> dict:
        with db_lock:
            c = db.cursor()
            c.execute(
                f"SELECT repo_key, webhook_token, bot_username, org_id "
                f"FROM {t_mgmt('managed_repos')} WHERE repo_key=?",
                (repo_key,)
            )
            row = c.fetchone()
            return dict(row) if row else {}

    def get_agent_session(self, repo_key: str, agent_type: str) -> dict:
        with db_lock:
            c = db.cursor()
            c.execute(
                f"SELECT runtime_id, skill, skill_id "
                f"FROM {t_mgmt('devops_agent_session')} "
                f"WHERE repo_key=? AND agent_type=?",
                (repo_key, agent_type)
            )
            row = c.fetchone()
            return dict(row) if row else {}

    def get_scene_config(self, repo_key: str) -> list:
        with db_lock:
            c = db.cursor()
            c.execute(
                f"SELECT scene_id, nodes_json, work_branch, deploy_url "
                f"FROM {t_mgmt('devops_pipeline_scene')} WHERE repo_key=?",
                (repo_key,)
            )
            return [dict(r) for r in c.fetchall()]

    def get_scene_pool_engine_ids(self, scene_id: str) -> list:
        with db_lock:
            c = db.cursor()
            c.execute(
                f"SELECT engine_runtime_ids "
                f"FROM {t_mgmt('devops_pipeline_scene')} WHERE scene_id=?",
                (scene_id,)
            )
            row = c.fetchone()
            return row[0] if row else []

    def verify_session_token(self, token_hash: str) -> dict:
        with db_lock:
            c = db.cursor()
            c.execute(
                f"SELECT credential_id, scope, owner_id, expires_at "
                f"FROM {t_mgmt('session_api_tokens')} WHERE token_hash=?",
                (token_hash,)
            )
            row = c.fetchone()
            return dict(row) if row else {}

    def get_bot_config(self, repo_key: str) -> dict:
        with db_lock:
            c = db.cursor()
            c.execute(
                f"SELECT bot_gitcode_token, bot_username "
                f"FROM {t_mgmt('managed_repos')} WHERE repo_key=?",
                (repo_key,)
            )
            row = c.fetchone()
            return dict(row) if row else {}
```

#### RemoteProvider 实现

```python
# src/domains/contracts/remote/mgmt_remote.py
"""MgmtService RemoteProvider——HTTP API 调用 Mgmt 进程"""
from src.domains.contracts.remote.http_client import ServiceHttpClient

class MgmtServiceRemote:
    """集群模式：通过 HTTP API 调用 Mgmt 进程的 /internal/* 端点"""

    def __init__(self):
        self.client = ServiceHttpClient("mgmt", base_url="${GRAPE_MGMT_URL}")

    def get_repo_webhook_config(self, repo_key: str) -> dict:
        return self.client.get(f"/internal/repos/{repo_key}/webhook-config")

    def get_agent_session(self, repo_key: str, agent_type: str) -> dict:
        return self.client.get(f"/internal/sessions/{repo_key}/{agent_type}")

    def get_scene_config(self, repo_key: str) -> list:
        return self.client.get(f"/internal/scenes/{repo_key}")

    def get_scene_pool_engine_ids(self, scene_id: str) -> list:
        return self.client.get(f"/internal/scenes/{scene_id}/engine-ids")

    def verify_session_token(self, token_hash: str) -> dict:
        return self.client.post("/internal/tokens/verify", {"token_hash": token_hash})

    def get_bot_config(self, repo_key: str) -> dict:
        return self.client.get(f"/internal/repos/{repo_key}/bot-config")
```

### 7.4 RuntimeService 接口定义

```python
# src/domains/contracts/runtime_service.py
"""运行域服务接口——供 Mgmt 域和 Insight 域调用"""
from typing import Protocol, Iterator

class RuntimeService(Protocol):
    """运行域服务接口——LocalProvider 走跨 schema SQL，RemoteProvider 走 HTTP API"""

    def get_tasks(self, repo_key: str, filters: dict) -> list:
        """A 用户面任务列表"""
        ...

    def get_task_detail(self, task_id: str) -> dict:
        """A 任务详情"""
        ...

    def get_task_messages(self, task_id: str, limit: int) -> list:
        """A 任务消息"""
        ...

    def stream_task_messages(self, task_id: str) -> Iterator[dict]:
        """A SSE 流式读取任务消息"""
        ...

    def get_events(self, repo_key: str, filters: dict) -> list:
        """A 事件列表"""
        ...

    def get_event_detail(self, seq: int) -> dict:
        """A 事件详情"""
        ...

    def get_issues(self, repo_key: str, filters: dict) -> list:
        """A 需求列表"""
        ...

    def get_issue_state(self, repo_key: str, issue_id: str) -> dict:
        """A Issue 状态"""
        ...

    def get_runtimes(self, org_id: int) -> list:
        """A 引擎列表"""
        ...

    def get_pipeline_instances(self, repo_key: str) -> list:
        """A 流水线实例"""
        ...

    def confirm_issue_done(self, issue_id: int, user_id: int) -> dict:
        """A 确认需求完成"""
        ...

    def cancel_issue(self, issue_id: int, user_id: int) -> dict:
        """A 取消需求"""
        ...

    def terminate_event(self, seq: int, user_id: int) -> dict:
        """A 终止事件"""
        ...

    def redispatch_event(self, seq: int, user_id: int) -> dict:
        """A 重派事件"""
        ...
```

#### LocalProvider 实现

```python
# src/domains/contracts/local/runtime_local.py
"""RuntimeService LocalProvider——同实例跨 schema SQL 实现"""
from src.kernel.db import t_runtime
from src.kernel.session_manager import db, db_lock

class RuntimeServiceLocal:
    """同实例模式：直接查 grape_runtime schema（SQLite 用 rt_ 前缀）"""

    def get_tasks(self, repo_key: str, filters: dict) -> list:
        with db_lock:
            c = db.cursor()
            # 构建 WHERE 子句
            conditions = ["repo_key=?"]
            params = [repo_key]
            if "status" in filters:
                conditions.append("status=?")
                params.append(filters["status"])
            sql = f"SELECT * FROM {t_runtime('agent_tasks')} WHERE {' AND '.join(conditions)} ORDER BY created_at DESC LIMIT 100"
            c.execute(sql, params)
            return [dict(r) for r in c.fetchall()]

    def get_task_detail(self, task_id: str) -> dict:
        with db_lock:
            c = db.cursor()
            c.execute(f"SELECT * FROM {t_runtime('agent_tasks')} WHERE id=?", (task_id,))
            row = c.fetchone()
            return dict(row) if row else {}

    def get_task_messages(self, task_id: str, limit: int = 50) -> list:
        with db_lock:
            c = db.cursor()
            c.execute(
                f"SELECT * FROM {t_runtime('task_messages')} "
                f"WHERE task_id=? ORDER BY seq ASC LIMIT ?",
                (task_id, limit)
            )
            return [dict(r) for r in c.fetchall()]

    def stream_task_messages(self, task_id: str) -> Iterator[dict]:
        """SSE 流——LocalProvider 直接查 DB 轮询新消息"""
        import time
        last_seq = 0
        deadline = time.time() + 300  # 5 分钟超时
        while time.time() < deadline:
            with db_lock:
                c = db.cursor()
                c.execute(
                    f"SELECT * FROM {t_runtime('task_messages')} "
                    f"WHERE task_id=? AND seq>? ORDER BY seq ASC",
                    (task_id, last_seq)
                )
                rows = c.fetchall()
            for row in rows:
                yield dict(row)
                last_seq = row["seq"]
            time.sleep(1)  # 1s 轮询

    def get_events(self, repo_key: str, filters: dict) -> list:
        with db_lock:
            c = db.cursor()
            conditions = ["repo_key=?"]
            params = [repo_key]
            if "status" in filters:
                conditions.append("status=?")
                params.append(filters["status"])
            sql = f"SELECT * FROM {t_runtime('events')} WHERE {' AND '.join(conditions)} ORDER BY seq DESC LIMIT 100"
            c.execute(sql, params)
            return [dict(r) for r in c.fetchall()]

    def get_event_detail(self, seq: int) -> dict:
        with db_lock:
            c = db.cursor()
            c.execute(f"SELECT * FROM {t_runtime('events')} WHERE seq=?", (seq,))
            row = c.fetchone()
            return dict(row) if row else {}

    def get_issues(self, repo_key: str, filters: dict) -> list:
        with db_lock:
            c = db.cursor()
            conditions = ["repo_key=?"]
            params = [repo_key]
            if "status" in filters:
                conditions.append("status=?")
                params.append(filters["status"])
            sql = f"SELECT * FROM {t_runtime('issue')} WHERE {' AND '.join(conditions)} ORDER BY created_at DESC"
            c.execute(sql, params)
            return [dict(r) for r in c.fetchall()]

    def get_issue_state(self, repo_key: str, issue_id: str) -> dict:
        with db_lock:
            c = db.cursor()
            c.execute(
                f"SELECT * FROM {t_runtime('issue_state')} "
                f"WHERE repo_key=? AND issue_id=?",
                (repo_key, issue_id)
            )
            row = c.fetchone()
            return dict(row) if row else {}

    def get_runtimes(self, org_id: int) -> list:
        with db_lock:
            c = db.cursor()
            c.execute(
                f"SELECT * FROM {t_runtime('agent_runtimes')} WHERE org_id=?",
                (org_id,)
            )
            return [dict(r) for r in c.fetchall()]

    def get_pipeline_instances(self, repo_key: str) -> list:
        with db_lock:
            c = db.cursor()
            c.execute(
                f"SELECT * FROM {t_runtime('pipeline_instances')} WHERE repo_key=?",
                (repo_key,)
            )
            return [dict(r) for r in c.fetchall()]

    def confirm_issue_done(self, issue_id: int, user_id: int) -> dict:
        """写 grape_runtime.issue + issue_state"""
        with db_lock:
            c = db.cursor()
            c.execute(
                f"UPDATE {t_runtime('issue')} SET status='completed' "
                f"WHERE issue_id=? AND (user_id=? OR user_id=0)",
                (issue_id, user_id)
            )
            c.execute(
                f"UPDATE {t_runtime('issue_state')} SET stage='done' "
                f"WHERE issue_id=?",
                (issue_id,)
            )
            db.commit()
            return {"ok": True, "issue_id": issue_id}

    def cancel_issue(self, issue_id: int, user_id: int) -> dict:
        with db_lock:
            c = db.cursor()
            c.execute(
                f"UPDATE {t_runtime('issue')} SET status='cancelled' WHERE issue_id=?",
                (issue_id,)
            )
            db.commit()
            return {"ok": True, "issue_id": issue_id}

    def terminate_event(self, seq: int, user_id: int) -> dict:
        with db_lock:
            c = db.cursor()
            c.execute(
                f"UPDATE {t_runtime('events')} SET status='terminated' WHERE seq=?",
                (seq,)
            )
            db.commit()
            return {"ok": True, "seq": seq}

    def redispatch_event(self, seq: int, user_id: int) -> dict:
        """重派事件——写 events + agent_tasks"""
        with db_lock:
            c = db.cursor()
            # 读取原事件
            c.execute(f"SELECT * FROM {t_runtime('events')} WHERE seq=?", (seq,))
            evt = c.fetchone()
            if not evt:
                return {"ok": False, "error": "event not found"}
            # 创建新事件 + 入队（简化版，实际逻辑更复杂）
            # ...
            db.commit()
            return {"ok": True, "seq": seq}
```

#### RemoteProvider 实现

```python
# src/domains/contracts/remote/runtime_remote.py
"""RuntimeService RemoteProvider——HTTP API 调用 Runtime 进程"""
from src.domains.contracts.remote.http_client import ServiceHttpClient

class RuntimeServiceRemote:
    """集群模式：通过 HTTP API 调用 Runtime 进程的 /internal/* 端点"""

    def __init__(self):
        self.client = ServiceHttpClient("runtime", base_url="${GRAPE_RUNTIME_URL}")

    def get_tasks(self, repo_key: str, filters: dict) -> list:
        return self.client.get(f"/internal/tasks", params={"repo_key": repo_key, **filters})

    def get_task_detail(self, task_id: str) -> dict:
        return self.client.get(f"/internal/tasks/{task_id}")

    def get_task_messages(self, task_id: str, limit: int = 50) -> list:
        return self.client.get(f"/internal/tasks/{task_id}/messages", params={"limit": limit})

    def stream_task_messages(self, task_id: str) -> Iterator[dict]:
        """SSE 流——RemoteProvider 代理 Runtime 进程的 SSE 端点"""
        return self.client.stream_sse(f"/internal/tasks/{task_id}/stream")

    def get_events(self, repo_key: str, filters: dict) -> list:
        return self.client.get(f"/internal/events", params={"repo_key": repo_key, **filters})

    def get_event_detail(self, seq: int) -> dict:
        return self.client.get(f"/internal/events/{seq}")

    def get_issues(self, repo_key: str, filters: dict) -> list:
        return self.client.get(f"/internal/issues", params={"repo_key": repo_key, **filters})

    def get_issue_state(self, repo_key: str, issue_id: str) -> dict:
        return self.client.get(f"/internal/issues/{repo_key}/{issue_id}/state")

    def get_runtimes(self, org_id: int) -> list:
        return self.client.get(f"/internal/runtimes", params={"org_id": org_id})

    def get_pipeline_instances(self, repo_key: str) -> list:
        return self.client.get(f"/internal/pipelines/{repo_key}")

    def confirm_issue_done(self, issue_id: int, user_id: int) -> dict:
        return self.client.post(f"/internal/issues/{issue_id}/confirm-done", {"user_id": user_id})

    def cancel_issue(self, issue_id: int, user_id: int) -> dict:
        return self.client.post(f"/internal/issues/{issue_id}/cancel", {"user_id": user_id})

    def terminate_event(self, seq: int, user_id: int) -> dict:
        return self.client.post(f"/internal/events/{seq}/terminate", {"user_id": user_id})

    def redispatch_event(self, seq: int, user_id: int) -> dict:
        return self.client.post(f"/internal/events/{seq}/redispatch", {"user_id": user_id})
```

### 7.5 InsightService 接口定义

```python
# src/domains/contracts/insight_service.py
"""数据域服务接口——供 Mgmt 域和 Runtime 域调用"""
from typing import Protocol

class InsightService(Protocol):
    """数据域服务接口——LocalProvider 走跨 schema SQL，RemoteProvider 走 HTTP API"""

    def get_dashboard_overview(self, repo_key: str) -> dict:
        """A 仪表盘"""
        ...

    def get_slo(self, repo_key: str) -> list:
        """A SLO 面板"""
        ...

    def get_alerts(self, org_id: int) -> list:
        """A 告警列表"""
        ...

    def log_obs(self, source: str, level: str, msg: str, **extra) -> None:
        """Runtime 写日志"""
        ...

    def report_latency(self, data: dict) -> None:
        """A 前端延迟上报"""
        ...

    def get_llm_usage(self, filters: dict) -> dict:
        """A LLM 用量查询"""
        ...
```

#### LocalProvider 实现

```python
# src/domains/contracts/local/insight_local.py
"""InsightService LocalProvider——同实例跨 schema SQL 实现"""
from src.kernel.db import t_insight
from src.kernel.session_manager import db, db_lock

class InsightServiceLocal:
    """同实例模式：直接查 grape_insight schema（SQLite 用 ins_ 前缀）"""

    def get_dashboard_overview(self, repo_key: str) -> dict:
        with db_lock:
            c = db.cursor()
            # 聚合查询：obs_log + obs_metric_rollup + grape_runtime 数据
            c.execute(
                f"SELECT COUNT(*) as total, "
                f"SUM(CASE WHEN level='ERROR' THEN 1 ELSE 0 END) as errors "
                f"FROM {t_insight('obs_log')} WHERE repo_key=? "
                f"AND created_at > datetime('now', '-24 hours')",
                (repo_key,)
            )
            row = c.fetchone()
            return dict(row) if row else {}

    def get_slo(self, repo_key: str) -> list:
        with db_lock:
            c = db.cursor()
            c.execute(
                f"SELECT * FROM {t_insight('obs_slo')} WHERE repo_key=?",
                (repo_key,)
            )
            return [dict(r) for r in c.fetchall()]

    def get_alerts(self, org_id: int) -> list:
        with db_lock:
            c = db.cursor()
            c.execute(
                f"SELECT * FROM {t_insight('obs_alert_events')} "
                f"WHERE org_id=? AND status='active' ORDER BY created_at DESC",
                (org_id,)
            )
            return [dict(r) for r in c.fetchall()]

    def log_obs(self, source: str, level: str, msg: str, **extra) -> None:
        with db_lock:
            c = db.cursor()
            c.execute(
                f"INSERT INTO {t_insight('obs_log')} (source, level, message, extra) "
                f"VALUES (?, ?, ?, ?)",
                (source, level, msg, str(extra))
            )
            db.commit()

    def report_latency(self, data: dict) -> None:
        with db_lock:
            c = db.cursor()
            c.execute(
                f"INSERT INTO {t_insight('api_latency_log')} "
                f"(endpoint, method, latency_ms, status_code) VALUES (?, ?, ?, ?)",
                (data.get("endpoint"), data.get("method"),
                 data.get("latency_ms"), data.get("status_code"))
            )
            db.commit()

    def get_llm_usage(self, filters: dict) -> dict:
        with db_lock:
            c = db.cursor()
            c.execute(
                f"SELECT model, SUM(prompt_tokens) as prompt, "
                f"SUM(completion_tokens) as completion "
                f"FROM {t_insight('obs_llm_usage')} "
                f"WHERE created_at > datetime('now', '-30 days') "
                f"GROUP BY model"
            )
            return {"usage": [dict(r) for r in c.fetchall()]}
```

#### RemoteProvider 实现

```python
# src/domains/contracts/remote/insight_remote.py
"""InsightService RemoteProvider——HTTP API 调用 Insight 进程"""
from src.domains.contracts.remote.http_client import ServiceHttpClient

class InsightServiceRemote:
    """集群模式：通过 HTTP API 调用 Insight 进程的 /internal/* 端点"""

    def __init__(self):
        self.client = ServiceHttpClient("insight", base_url="${GRAPE_INSIGHT_URL}")

    def get_dashboard_overview(self, repo_key: str) -> dict:
        return self.client.get(f"/internal/dashboard/{repo_key}")

    def get_slo(self, repo_key: str) -> list:
        return self.client.get(f"/internal/slo/{repo_key}")

    def get_alerts(self, org_id: int) -> list:
        return self.client.get(f"/internal/alerts", params={"org_id": org_id})

    def log_obs(self, source: str, level: str, msg: str, **extra) -> None:
        self.client.post("/internal/logs", {
            "source": source, "level": level, "message": msg, **extra
        })

    def report_latency(self, data: dict) -> None:
        self.client.post("/internal/latency", data)

    def get_llm_usage(self, filters: dict) -> dict:
        return self.client.get("/internal/llm-usage", params=filters)
```

### 7.6 共享 HTTP 客户端

```python
# src/domains/contracts/remote/http_client.py
"""RemoteProvider 共享 HTTP 客户端——连接池、超时、重试"""
import os
import httpx
from src.config import log

class ServiceHttpClient:
    """域间 HTTP API 客户端"""

    def __init__(self, service_name: str, base_url: str = None):
        self.service_name = service_name
        # 解析环境变量（如 ${GRAPE_MGMT_URL} → http://mgmt:38080）
        if base_url and base_url.startswith("${"):
            env_key = base_url[2:-1]
            self.base_url = os.environ.get(env_key, f"http://localhost:3808{service_name[0]}")
        else:
            self.base_url = base_url or f"http://localhost:38080"
        self.client = httpx.Client(
            base_url=self.base_url,
            timeout=httpx.Timeout(10.0, connect=5.0),
            transport=httpx.HTTPTransport(retries=2),
            headers={"X-Internal-Call": "true", "X-Service": service_name},
        )
        log.info(f"ServiceHttpClient({service_name}) → {self.base_url}")

    def get(self, path: str, params: dict = None) -> dict | list:
        r = self.client.get(path, params=params)
        r.raise_for_status()
        return r.json()

    def post(self, path: str, json: dict = None) -> dict:
        r = self.client.post(path, json=json)
        r.raise_for_status()
        return r.json()

    def stream_sse(self, path: str) -> 'SSEIterator':
        """SSE 流式读取"""
        return SSEIterator(self.client, path)


class SSEIterator:
    """SSE 流迭代器——用于 task_messages 流式读取"""

    def __init__(self, client: httpx.Client, path: str):
        self.client = client
        self.path = path

    def __iter__(self):
        with self.client.stream("GET", self.path) as response:
            for line in response.iter_lines():
                if line.startswith("data: "):
                    import json
                    yield json.loads(line[6:])
```

### 7.7 /internal/* 端点定义

每个域进程需要暴露 `/internal/*` 端点供 RemoteProvider 调用（集群模式才加载）：

```python
# src/domains/runtime/routes/internal.py
"""Runtime 进程内部端点——仅供其他域 RemoteProvider 调用"""
from fastapi import APIRouter, Header

router = APIRouter(prefix="/internal")

@router.get("/tasks")
async def list_tasks(repo_key: str, status: str = None):
    """供 MgmtService.get_tasks() 调用"""
    from src.kernel.provider import get_runtime_service
    svc = get_runtime_service()  # Runtime 进程内走 Local
    return svc.get_tasks(repo_key, {"status": status} if status else {})

@router.get("/tasks/{task_id}")
async def get_task(task_id: str):
    return get_runtime_service().get_task_detail(task_id)

@router.get("/tasks/{task_id}/messages")
async def get_messages(task_id: str, limit: int = 50):
    return get_runtime_service().get_task_messages(task_id, limit)

@router.get("/tasks/{task_id}/stream")
async def stream_messages(task_id: str):
    """SSE 流"""
    from fastapi.responses import StreamingResponse
    import json
    def generate():
        for msg in get_runtime_service().stream_task_messages(task_id):
            yield f"data: {json.dumps(msg)}\n\n"
    return StreamingResponse(generate(), media_type="text/event-stream")

@router.get("/events")
async def list_events(repo_key: str, status: str = None):
    return get_runtime_service().get_events(repo_key, {"status": status} if status else {})

@router.get("/events/{seq}")
async def get_event(seq: int):
    return get_runtime_service().get_event_detail(seq)

@router.post("/events/{seq}/terminate")
async def terminate_event(seq: int, user_id: int = None):
    return get_runtime_service().terminate_event(seq, user_id)

@router.post("/events/{seq}/redispatch")
async def redispatch_event(seq: int, user_id: int = None):
    return get_runtime_service().redispatch_event(seq, user_id)

@router.get("/issues")
async def list_issues(repo_key: str, status: str = None):
    return get_runtime_service().get_issues(repo_key, {"status": status} if status else {})

@router.post("/issues/{issue_id}/confirm-done")
async def confirm_done(issue_id: int, user_id: int = None):
    return get_runtime_service().confirm_issue_done(issue_id, user_id)

@router.post("/issues/{issue_id}/cancel")
async def cancel_issue(issue_id: int, user_id: int = None):
    return get_runtime_service().cancel_issue(issue_id, user_id)

@router.get("/runtimes")
async def list_runtimes(org_id: int):
    return get_runtime_service().get_runtimes(org_id)

@router.get("/pipelines/{repo_key}")
async def get_pipelines(repo_key: str):
    return get_runtime_service().get_pipeline_instances(repo_key)
```

### 7.8 Grape Service Bus (GSB)——统一服务通信框架

域间通信采用自研轻量框架 GSB（Grape Service Bus），基于 FastAPI + httpx + DB 注册表，不引入任何新依赖。新增服务只需 3 步。

#### 7.8.1 设计理念

| 设计点 | 选择 | 理由 |
|--------|------|------|
| 通信协议 | HTTP + JSON | 与 FastAPI 原生集成，curl 可调试，无编译链 |
| 契约定义 | Python Protocol | IDE 类型提示，无 .proto 编译 |
| 服务发现 | DB 表（gsb_service_registry） | 零外部依赖，单机模式天然支持 |
| 认证 | HMAC-SHA256 服务令牌 | 短期 TTL 防重放，常量时间比较防时序攻击 |
| Provider 切换 | Local/Remote 双模式 | GRAPE_MODE 环境变量驱动，业务代码无感 |
| 新增服务 | 3 步（Protocol + Provider + GsbService） | 低扩展成本 |
| 对比 gRPC | 不采用 | gRPC 留给未来引擎接入层（Phase 3 远期万级设备），域间通信是低频管理操作，不需要 gRPC 的双向流和强类型 |

#### 7.8.2 GSB 目录结构

```
src/kernel/gsb/                    # Grape Service Bus
├── __init__.py
├── registry.py                    # 服务注册表（DB 驱动）
├── client.py                      # GsbClient（httpx + 发现 + 认证 + 重试）
├── router.py                      # GsbService 基类（注册 + 心跳 + 中间件）
├── provider.py                    # Provider 工厂（Local/Remote 切换）
└── health.py                      # 服务健康检查端点
```

#### 7.8.3 服务注册表（DB 驱动，零外部依赖）

```python
# src/kernel/gsb/registry.py
"""Grape Service Bus——服务注册表（DB 驱动）"""

import time
from src.kernel.session_manager import db, db_lock

_REGISTRY_DDL = """
CREATE TABLE IF NOT EXISTS gsb_service_registry (
    service_name   TEXT PRIMARY KEY,    -- mgmt / runtime / insight / future-service
    base_url       TEXT NOT NULL,        -- http://mgmt:38080
    health_url     TEXT,                 -- /api/health
    version        TEXT DEFAULT '1',     -- 服务版本
    status         TEXT DEFAULT 'online',-- online / offline / draining
    last_heartbeat REAL DEFAULT 0,       -- Unix timestamp
    metadata       TEXT DEFAULT '{}'     -- JSON: {description, endpoints, ...}
);
"""

def register_service(name: str, base_url: str, health_url: str = "/api/health",
                     metadata: dict = None):
    """服务启动时注册自己"""
    with db_lock:
        c = db.cursor()
        c.execute(_REGISTRY_DDL)
        c.execute(
            "INSERT OR REPLACE INTO gsb_service_registry "
            "(service_name, base_url, health_url, status, last_heartbeat, metadata) "
            "VALUES (?, ?, ?, 'online', ?, ?)",
            (name, base_url, health_url, time.time(), str(metadata or {}))
        )
        db.commit()

def heartbeat_service(name: str):
    """定时心跳更新（30s）"""
    with db_lock:
        c = db.cursor()
        c.execute(
            "UPDATE gsb_service_registry SET last_heartbeat=?, status='online' "
            "WHERE service_name=?",
            (time.time(), name)
        )
        db.commit()

def discover_service(name: str) -> dict:
    """发现服务地址——带健康检查"""
    with db_lock:
        c = db.cursor()
        c.execute(
            "SELECT * FROM gsb_service_registry WHERE service_name=? AND status='online'",
            (name,)
        )
        row = c.fetchone()
        if not row:
            return {}
        # 心跳超时检查（90s = 3 个心跳周期）
        if time.time() - row["last_heartbeat"] > 90:
            return {}
        return dict(row)

def list_services() -> list:
    """列出所有在线服务"""
    with db_lock:
        c = db.cursor()
        c.execute(
            "SELECT * FROM gsb_service_registry WHERE status='online' "
            "AND last_heartbeat > ? ORDER BY service_name",
            (time.time() - 90,)
        )
        return [dict(r) for r in c.fetchall()]

def deregister_service(name: str):
    """服务关闭时注销"""
    with db_lock:
        c = db.cursor()
        c.execute(
            "UPDATE gsb_service_registry SET status='offline' WHERE service_name=?",
            (name,)
        )
        db.commit()
```

#### 7.8.4 统一服务客户端

```python
# src/kernel/gsb/client.py
"""Grape Service Bus——统一服务客户端"""

import os
import time
import httpx
from src.kernel.gsb.registry import discover_service
from src.kernel.auth_sdk.service_token import generate_service_token
from src.config import log

# 环境变量优先级高于 DB 注册表（支持静态配置）
_ENV_URL_MAP = {
    "mgmt": "GRAPE_MGMT_URL",
    "runtime": "GRAPE_RUNTIME_URL",
    "insight": "GRAPE_INSIGHT_URL",
}

class GsbClient:
    """统一域间通信客户端——自动服务发现 + 令牌认证 + 重试"""

    def __init__(self, service_name: str, caller_name: str = ""):
        self.service_name = service_name
        self.caller_name = caller_name or os.environ.get("GRAPE_SERVICE_NAME", "unknown")
        self._cached_url = None
        self._cached_at = 0
        self._http = httpx.Client(
            timeout=httpx.Timeout(10.0, connect=5.0),
            transport=httpx.HTTPTransport(retries=2),
        )

    def _resolve_url(self) -> str:
        """解析服务地址——环境变量优先，DB 注册表兜底，单机回退 localhost"""
        if self._cached_url and time.time() - self._cached_at < 30:
            return self._cached_url

        # 1. 环境变量
        env_key = _ENV_URL_MAP.get(self.service_name)
        if env_key:
            url = os.environ.get(env_key)
            if url:
                self._cached_url, self._cached_at = url, time.time()
                return url

        # 2. DB 服务注册表
        svc = discover_service(self.service_name)
        if svc:
            self._cached_url, self._cached_at = svc["base_url"], time.time()
            return svc["base_url"]

        # 3. 单机模式回退
        port_map = {"mgmt": 38080, "runtime": 38081, "insight": 38083}
        fallback = f"http://localhost:{port_map.get(self.service_name, 38080)}"
        self._cached_url, self._cached_at = fallback, time.time()
        return fallback

    def _headers(self) -> dict:
        """认证头——每次请求重新生成令牌（TTL 60s）"""
        return {
            "X-Internal-Call": "true",
            "X-Service": self.caller_name,
            "X-Service-Token": generate_service_token(self.caller_name),
        }

    def get(self, path: str, params: dict = None) -> dict | list:
        url = self._resolve_url()
        r = self._http.get(f"{url}{path}", params=params, headers=self._headers())
        r.raise_for_status()
        return r.json()

    def post(self, path: str, json: dict = None) -> dict:
        url = self._resolve_url()
        r = self._http.post(f"{url}{path}", json=json, headers=self._headers())
        r.raise_for_status()
        return r.json()

    def stream_sse(self, path: str):
        """SSE 流式读取"""
        url = self._resolve_url()
        with self._http.stream("GET", f"{url}{path}", headers=self._headers()) as resp:
            for line in resp.iter_lines():
                if line.startswith("data: "):
                    import json
                    yield json.loads(line[6:])
```

#### 7.8.5 统一服务路由基类

```python
# src/kernel/gsb/router.py
"""Grape Service Bus——统一服务路由基类"""

from fastapi import APIRouter, FastAPI
from src.kernel.gsb.registry import register_service, heartbeat_service, deregister_service
from src.kernel.auth_sdk.internal_middleware import InternalAuthMiddleware
from src.config import log
import asyncio

class GsbService:
    """服务基类——每个域进程继承此类，自动获得注册/心跳/认证能力"""

    def __init__(self, name: str, port: int, app: FastAPI):
        self.name = name
        self.port = port
        self.app = app
        self.base_url = f"http://localhost:{port}"
        app.add_middleware(InternalAuthMiddleware)
        self.internal_router = APIRouter(prefix="/internal")

    def mount(self):
        """启动时调用：注册服务 + 挂载内部路由 + 启动心跳"""
        self.app.include_router(self.internal_router)
        register_service(
            name=self.name, base_url=self.base_url,
            health_url="/api/health", metadata=self._metadata()
        )

        @self.app.on_event("startup")
        async def _start_heartbeat():
            asyncio.create_task(self._heartbeat_loop())

        @self.app.on_event("shutdown")
        async def _deregister():
            deregister_service(self.name)

        log.info(f"GSB service '{self.name}' mounted at {self.base_url}")

    async def _heartbeat_loop(self):
        """30s 心跳"""
        while True:
            try:
                heartbeat_service(self.name)
            except Exception as e:
                log.warning(f"GSB heartbeat failed: {e}")
            await asyncio.sleep(30)

    def _metadata(self) -> dict:
        return {"description": self.name, "version": "1"}
```

#### 7.8.6 Provider 工厂（动态注册，支持未来新增服务）

```python
# src/kernel/gsb/provider.py
"""Grape Service Bus——Provider 工厂"""

import os

_PROVIDERS = {}

def register_provider(name: str, local_cls, remote_cls=None):
    """注册服务的 Local/Remote Provider。
    
    在模块加载时调用（如 src/kernel/gsb/__init__.py）。
    新增服务时在自己的模块中调用此函数注册。
    """
    _PROVIDERS[name] = {"local": local_cls, "remote": remote_cls}

def get_service(name: str):
    """获取服务实现——根据部署模式自动选择 Local 或 Remote"""
    if name not in _PROVIDERS:
        raise ValueError(f"Unknown service: {name}. Did you register_provider('{name}', ...)?")
    provider_mode = os.environ.get("GRAPE_PROVIDER_MODE", "")
    use_remote = (provider_mode == "remote"
                  or (os.environ.get("GRAPE_MODE") == "cluster"
                      and _PROVIDERS[name]["remote"] is not None))
    if use_remote:
        return _PROVIDERS[name]["remote"]()
    return _PROVIDERS[name]["local"]()

# 便捷函数——预注册的三个域
def get_mgmt_service():
    return get_service("mgmt")

def get_runtime_service():
    return get_service("runtime")

def get_insight_service():
    return get_service("insight")
```

#### 7.8.7 HTTP API 安全保障

RemoteProvider 模式下，域间通信通过 HTTP API 调用，需要完整的安全保障体系：

##### 服务令牌（Service Token）

每个域进程在启动时生成或从配置加载一个服务令牌，用于域间互信：

```python
# src/kernel/auth_sdk/service_token.py
"""域间服务令牌——用于 /internal/* 端点互信认证"""

import os, hmac, hashlib, time

SERVICE_SECRET = os.environ.get("GRAPE_SERVICE_SECRET", "")

def generate_service_token(service_name: str, target_service: str = "") -> str:
    """生成短期服务令牌（TTL 60s）。
    格式：{source}.{target}.{timestamp}.{hmac_sha256}
    """
    if not SERVICE_SECRET:
        return ""
    ts = str(int(time.time()))
    payload = f"{service_name}.{target_service}.{ts}"
    sig = hmac.new(SERVICE_SECRET.encode(), payload.encode(), hashlib.sha256).hexdigest()
    return f"{payload}.{sig}"

def verify_service_token(token: str, expected_target: str = "") -> bool:
    """验证服务令牌（TTL 60s + HMAC + 常量时间比较）"""
    if not SERVICE_SECRET:
        return True  # 单机模式跳过
    if not token or token.count(".") != 3:
        return False
    source, target, ts, sig = token.split(".")
    try:
        age = time.time() - int(ts)
    except ValueError:
        return False
    if age > 60 or age < -5:  # 60s TTL + 5s 时钟漂移
        return False
    if expected_target and target != expected_target:
        return False
    payload = f"{source}.{target}.{ts}"
    expected_sig = hmac.new(SERVICE_SECRET.encode(), payload.encode(), hashlib.sha256).hexdigest()
    return hmac.compare_digest(sig, expected_sig)
```

##### /internal/* 认证中间件

```python
# src/kernel/auth_sdk/internal_middleware.py
from fastapi import Request, HTTPException
from starlette.middleware.base import BaseHTTPMiddleware
from src.kernel.auth_sdk.service_token import verify_service_token, SERVICE_SECRET

class InternalAuthMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        if not request.url.path.startswith("/internal/"):
            return await call_next(request)
        if not SERVICE_SECRET:  # 单机模式跳过
            return await call_next(request)
        token = request.headers.get("X-Service-Token", "")
        source = request.headers.get("X-Service", "")
        if not verify_service_token(token):
            raise HTTPException(401, {"code": "SERVICE_TOKEN_INVALID"})
        request.state.service_caller = source
        return await call_next(request)
```

##### 网络层隔离

```nginx
# nginx——/internal/* 端点不对外暴露
location /internal/ {
    deny all;
    return 404;
}
```

```yaml
# docker-compose——域间通信走内部网络
networks:
  grape-internal:
    driver: bridge
    internal: true  # 不可路由到外部
```

##### 安全保障总表

| 安全层 | 机制 | 说明 |
|--------|------|------|
| 服务令牌认证 | HMAC-SHA256 + TTL 60s | 防重放，`GRAPE_SERVICE_SECRET` 不硬编码 |
| 签名校验 | `hmac.compare_digest` | 常量时间比较，防时序攻击 |
| 时钟漂移容忍 | -5s ~ +60s | 防多机时钟不同步 |
| 目标校验 | expected_target | 防令牌被用于非预期域 |
| 认证中间件 | InternalAuthMiddleware | `/internal/*` 强制认证，单机自动跳过 |
| 网络隔离 | Docker internal network | `/internal/*` 外部不可达 |
| nginx 拒绝 | `deny all` | 双保险 |
| 来源标记 | `X-Service` 头 | 审计日志追踪 |
| 单机降级 | SERVICE_SECRET 为空跳过 | 零配置 |

##### 部署配置

```bash
# 单机模式——无需服务令牌
GRAPE_MODE=single
# GRAPE_SERVICE_SECRET 不设置

# 集群-共库模式——可选
GRAPE_MODE=cluster
GRAPE_SERVICE_SECRET=""  # 共库走 Local，不走 HTTP

# 集群-分库模式——必须
GRAPE_MODE=cluster
GRAPE_PROVIDER_MODE=remote
GRAPE_SERVICE_SECRET="$(openssl rand -hex 32)"
GRAPE_MGMT_URL=http://mgmt:38080
GRAPE_RUNTIME_URL=http://runtime:38081
GRAPE_INSIGHT_URL=http://insight:38083
```

#### 7.8.8 新增服务只需 3 步

以未来新增 "grape-notify" 通知服务为例：

```python
# 步骤 1：定义契约接口（src/domains/contracts/notify_service.py）
from typing import Protocol

class NotifyService(Protocol):
    def send_notification(self, user_id: int, title: str, body: str) -> dict: ...
    def get_notifications(self, user_id: int, limit: int = 20) -> list: ...
    def mark_read(self, notification_id: int) -> dict: ...


# 步骤 2：实现 Local + Remote Provider
# src/domains/contracts/local/notify_local.py
class NotifyServiceLocal:
    def send_notification(self, user_id, title, body):
        # 直接写 DB
        ...

# src/domains/contracts/remote/notify_remote.py
class NotifyServiceRemote:
    def __init__(self):
        self.client = GsbClient("notify", caller_name="mgmt")
    def send_notification(self, user_id, title, body):
        return self.client.post("/internal/notifications", {
            "user_id": user_id, "title": title, "body": body
        })


# 步骤 3：创建服务进程（apps/notify/main.py）
class NotifyServiceApp(GsbService):
    def __init__(self):
        app = FastAPI(title="Grape Notify Server")
        super().__init__("notify", 38084, app)

        @self.internal_router.post("/notifications")
        async def send(user_id: int, title: str, body: str):
            return NotifyServiceLocal().send_notification(user_id, title, body)

        @app.get("/api/notifications")
        async def list_notifications(user_id: int):
            return NotifyServiceLocal().get_notifications(user_id)

        # 注册 Provider
        from src.kernel.gsb.provider import register_provider
        register_provider("notify", NotifyServiceLocal, NotifyServiceRemote)

app = NotifyServiceApp().app
```

其他域调用新服务：

```python
from src.kernel.gsb.provider import get_service
notify = get_service("notify")  # 自动 Local 或 Remote
notify.send_notification(user_id=1, title="部署完成", body="PR #123 已合并")
```

#### 7.8.9 模式切换与配置

| 模式 | GRAPE_MODE | Provider | 通信方式 | 事务 | 适用场景 |
|------|-----------|----------|---------|-----|---------|
| 单机 | single | LocalProvider | 跨 schema SQL | 跨 schema 事务 | 开发/测试/小规模 |
| 集群-共库 | cluster | LocalProvider | 跨 schema SQL | 跨 schema 事务 | 中规模（MySQL 三 schema 同实例） |
| 集群-分库 | cluster | RemoteProvider | GSB HTTP API | saga/outbox | 大规模（各域独立 DB） |

`GRAPE_PROVIDER_MODE=remote` 优先级高于 `GRAPE_MODE`，允许在共库场景下提前切换到 HTTP API 模式进行测试。

#### 7.8.10 GSB 对比 gRPC 的取舍

| 维度 | GSB（推荐） | gRPC |
|------|------------|------|
| 新依赖 | 无 | grpcio + protobuf + 编译工具链 |
| 契约定义 | Python Protocol | .proto 文件（需 protoc 编译） |
| 单机降级 | LocalProvider 天然支持 | 需额外适配 |
| SSE 流 | httpx.stream | gRPC 双向流（更强但更复杂） |
| 调试 | HTTP + JSON（curl 可调） | 需 gRPC 工具 |
| 新增服务 | 3 步 | 5 步 |
| 与 FastAPI 集成 | 原生 | 需 grpc-gateway 或双端口 |
| 适用场景 | 域间低频管理调用 | 引擎接入层高频实时流 |

gRPC 留给 grape架构演进分析报告.html Phase 3 中"万级引擎 gRPC 双向流"的远期目标，不用于域间通信。域间调用是低频管理操作（查任务、查事件、写日志），GSB 足够。

#### 7.8.11 跨语言服务接入

GSB 的核心通信层（HTTP + JSON + DB 注册表 + HMAC 令牌）是语言无关的。只有契约定义层（Python Protocol）和路由基类（GsbService）是 Python 特有的。其他语言只需实现相同的通信协议即可接入。

##### 跨语言兼容矩阵

| GSB 组件 | Python 实现 | Java（grape-admin） | Go | Node.js/TS |
|----------|------------|---------------------|-----|------------|
| 服务注册表 | DB INSERT/SELECT（registry.py） | JDBC INSERT/SELECT | database/sql | pg/mysql2 |
| 服务发现 | DB SELECT + 30s 缓存 | JDBC + Caffeine 缓存 | sqlx + sync.Map | mysql2 + lru-cache |
| 服务令牌 | hmac.new(SHA256) | javax.crypto.Mac(SHA256) | crypto/hmac | crypto.createHmac |
| HTTP 客户端 | httpx | OkHttp / Java HttpClient | net/http | fetch / axios |
| 认证中间件 | FastAPI Middleware | Spring Filter / Interceptor | middleware func | Express middleware |
| 契约定义 | Python Protocol | OpenAPI codegen Java 接口 | oapi-codegen | openapi-typescript |
| 路由基类 | GsbService class | @RestController + @Component | http.ServeMux | Express router |

##### Java 接入示例（grape-admin）

```java
// 1. 服务注册 + 心跳
@Component
public class GsbRegistry {
    @Value("${grape.service.name:admin}")
    private String serviceName;
    @Value("${grape.service.url:http://admin:38085}")
    private String baseUrl;
    @Autowired
    private JdbcTemplate jdbcTemplate;

    @PostConstruct
    public void register() {
        jdbcTemplate.update(
            "INSERT OR REPLACE INTO gsb_service_registry " +
            "(service_name, base_url, health_url, status, last_heartbeat) " +
            "VALUES (?, ?, '/api/health', 'online', ?)",
            serviceName, baseUrl, System.currentTimeMillis() / 1000
        );
    }

    @Scheduled(fixedRate = 30000)  // 30s 心跳
    public void heartbeat() {
        jdbcTemplate.update(
            "UPDATE gsb_service_registry SET last_heartbeat=?, status='online' " +
            "WHERE service_name=?",
            System.currentTimeMillis() / 1000, serviceName
        );
    }

    @PreDestroy
    public void deregister() {
        jdbcTemplate.update(
            "UPDATE gsb_service_registry SET status='offline' WHERE service_name=?",
            serviceName
        );
    }
}

// 2. 服务令牌——与 Python service_token.py 完全一致的 HMAC-SHA256
@Component
public class GsbTokenService {
    @Value("${grape.service.secret:}")
    private String serviceSecret;

    public String generateToken(String serviceName) {
        if (serviceSecret.isEmpty()) return "";
        String ts = String.valueOf(System.currentTimeMillis() / 1000);
        String payload = serviceName + "." + ts;
        String sig = hmacSha256(serviceSecret, payload);
        return serviceName + "." + ts + "." + sig;
    }

    public boolean verifyToken(String token) {
        if (serviceSecret.isEmpty()) return true;
        if (token == null || token.split("\\.").length != 3) return false;
        String[] parts = token.split("\\.");
        long age = System.currentTimeMillis() / 1000 - Long.parseLong(parts[1]);
        if (age > 60 || age < -5) return false;
        String payload = parts[0] + "." + parts[1];
        String expected = hmacSha256(serviceSecret, payload);
        return MessageDigest.isEqual(expected.getBytes(), parts[2].getBytes());
    }

    private String hmacSha256(String secret, String payload) {
        try {
            Mac mac = Mac.getInstance("HmacSHA256");
            mac.init(new SecretKeySpec(secret.getBytes(), "HmacSHA256"));
            return HexFormat.of().formatHex(mac.doFinal(payload.getBytes()));
        } catch (Exception e) { throw new RuntimeException(e); }
    }
}

// 3. 认证过滤器——拦截 /internal/*
@Component
public class GsbAuthFilter implements Filter {
    @Autowired
    private GsbTokenService tokenService;

    @Override
    public void doFilter(ServletRequest req, ServletResponse res, FilterChain chain)
            throws IOException, ServletException {
        HttpServletRequest request = (HttpServletRequest) req;
        if (!request.getRequestURI().startsWith("/internal/")) {
            chain.doFilter(req, res);
            return;
        }
        String token = request.getHeader("X-Service-Token");
        if (!tokenService.verifyToken(token)) {
            HttpServletResponse resp = (HttpServletResponse) res;
            resp.setStatus(401);
            resp.setContentType("application/json");
            resp.getWriter().write("{\"code\":\"SERVICE_TOKEN_INVALID\"}");
            return;
        }
        chain.doFilter(req, res);
    }
}

// 4. 调用其他域服务——服务发现 + 令牌 + HTTP
@Service
public class GsbClient {
    @Autowired
    private JdbcTemplate jdbcTemplate;
    @Autowired
    private GsbTokenService tokenService;

    private final HttpClient httpClient = HttpClient.newBuilder()
        .connectTimeout(Duration.ofSeconds(5)).build();
    private final Map<String, String> urlCache = new ConcurrentHashMap<>();

    public Map<String, Object> callService(String serviceName, String path, String method,
                                           Map<String, Object> body) {
        String baseUrl = resolveUrl(serviceName);
        String token = tokenService.generateToken("admin");
        HttpRequest.Builder builder = HttpRequest.newBuilder()
            .uri(URI.create(baseUrl + path))
            .header("X-Service", "admin")
            .header("X-Service-Token", token)
            .header("Content-Type", "application/json");

        if ("POST".equals(method)) {
            builder.POST(BodyPublishers.ofString(new ObjectMapper().writeValueAsString(body)));
        } else {
            builder.GET();
        }

        HttpResponse<String> resp = httpClient.send(builder.build(),
            BodyHandlers.ofString());
        return new ObjectMapper().readValue(resp.body(), Map.class);
    }

    private String resolveUrl(String serviceName) {
        return urlCache.computeIfAbsent(serviceName, name -> {
            // DB 服务发现
            try {
                return jdbcTemplate.queryForObject(
                    "SELECT base_url FROM gsb_service_registry " +
                    "WHERE service_name=? AND status='online' " +
                    "AND last_heartbeat > ?",
                    String.class, name, System.currentTimeMillis() / 1000 - 90
                );
            } catch (EmptyResultDataAccessException e) {
                // 回退到环境变量或 localhost
                String envUrl = System.getenv("GRAPE_" + name.toUpperCase() + "_URL");
                return envUrl != null ? envUrl : "http://localhost:38080";
            }
        });
    }
}

// 5. 使用示例
@RestController
@RequestMapping("/api/admin")
public class AdminController {
    @Autowired
    private GsbClient gsbClient;

    @GetMapping("/dashboard/{repoKey}")
    public Map<String, Object> dashboard(@PathVariable String repoKey) {
        // 调用 Runtime 域获取任务列表
        return gsbClient.callService("runtime",
            "/internal/tasks?repo_key=" + repoKey, "GET", null);
    }
}
```

##### OpenAPI 契约桥接

Python Protocol 契约通过 FastAPI 自动生成 OpenAPI schema，其他语言通过 codegen 生成类型安全的客户端：

```bash
# 1. 导出 OpenAPI schema（Python 侧）
python -c "from src.api.main import app; import json; print(json.dumps(app.openapi()))" > openapi.json

# 2. Java 客户端生成
openapi-generator-cli generate \
  -i openapi.json \
  -g java-client \
  -o grape-admin/src/generated/gsb-client \
  --additional-properties=library=okhttp,dateLibrary=java8

# 3. Go 客户端生成
oapi-codegen -package gsb -o grape-cli/gsb/client.go openapi.json

# 4. TypeScript 客户端生成
openapi-typescript openapi.json --output grape-admin/frontend/src/gsb-client.ts
```

生成的 Java 客户端使用示例：

```java
// 自动生成的类型安全客户端——不需要手写 HTTP 调用
GsbClient client = new GsbClientBuilder().basePath("http://runtime:38081").build();
RuntimeServiceApi api = client.getRuntimeServiceApi();

// 类型安全的方法调用
List<Task> tasks = api.listTasks("gitcode:grape-dev/grape", null);
TaskDetail detail = api.getTaskDetail("12345");
api.confirmIssueDone(678, 1);
```

##### 跨语言接入清单

非 Python 服务接入 GSB 需要实现的 5 个组件：

| # | 组件 | 必须实现 | 说明 |
|---|------|---------|------|
| 1 | 服务注册 | 是 | 启动时 INSERT gsb_service_registry，30s 心跳 UPDATE，关闭时 UPDATE status=offline |
| 2 | 令牌生成 | 是 | HMAC-SHA256，格式 `{source}.{timestamp}.{hmac}`，TTL 60s |
| 3 | 令牌验证 | 是（暴露 /internal/* 时） | 验证 X-Service-Token 头，常量时间比较 |
| 4 | 服务发现 | 是（调用其他域时） | SELECT gsb_service_registry WHERE status=online AND last_heartbeat > now-90s |
| 5 | HTTP 客户端 | 是 | 携带 X-Service + X-Service-Token 头，10s 超时，2 次重试 |

契约定义可选——非 Python 语言可以用 OpenAPI codegen 生成类型安全客户端，也可以直接用 HTTP + JSON 手写调用。

##### grape-admin 接入路线

grape-admin（Spring Boot + Vue3）是最先需要接入的非 Python 服务：

1. 在 grape-admin 后端实现 GsbRegistry（服务注册 + 心跳）
2. 实现 GsbTokenService（令牌生成 + 验证）
3. 实现 GsbAuthFilter（/internal/* 认证过滤器）
4. 实现 GsbClient（服务发现 + HTTP 调用）
5. 从 Python 侧导出 OpenAPI schema，Java 侧 codegen 生成客户端
6. grape-admin 的 RuntimeCredentialService.java 从直读 grape DB 表改为通过 GSB 调用 Runtime 域 /internal/* 端点

---

#### 7.8.12 落地状态（2026-11-04，weekly-09-ljt）

此前 GSB 只有实现、没有接线：`register_service()` 的唯一调用点是 `GsbService.mount()`，
而四个进程入口从未实例化 `GsbService`，因此注册表连建表都没跑过（`gsb_service_registry`
不存在），服务发现实际靠 `.env` 静态 URL + `/health` 探活。

本分支已完成接线（commit `feat(gsb): 接线服务总线…`）：

| 项目 | 落地内容 |
|------|---------|
| 进程接线 | `mgmt` / `event` / `worker`(服务名 `runtime`) / `insight` 四个入口在 import 期调用 `init_gsb(app, role)`，lifespan 里 `await gsb.startup()` / `await gsb.shutdown()` |
| 注册地址 | `base_url` 支持 `GRAPE_GSB_BASE_URL` > `GRAPE_{MGMT,EVENT,RUNTIME,INSIGHT}_URL` > `http://127.0.0.1:<实际端口>`；端口支持 `GRAPE_GSB_PORT` 覆盖，不再写死 localhost:38080 |
| 健康语义 | `health_url` 默认 `/api/ping`（全进程可达、免鉴权、不查库），避开 `/health` 的 loopback-only 限制 |
| 自愈 | 注册失败只告警、不阻断进程启动；下一次心跳（30s）自动重试注册 |
| 运维端点 | `GET /internal/gsb/services`（全量 + `age_seconds` / `alive`）、`GET /internal/gsb/services/{name}`（单服务发现，过期返回 404），集群模式走 `X-Service-Token` 认证 |
| 注册表能力 | 新增 `list_all_services()`（含离线/过期行）与 `purge_stale()`（心跳超时收敛为 offline，保留行便于排查） |
| 客户端 | 新增 `event` 映射与四进程实际端口回退；传输层故障（ConnectError/超时/协议错）按 `env → 注册表 → 本机` 顺序切换候选地址，HTTP 4xx/5xx 不重试 |
| 测试 | `tests/kernel/test_gsb_registry.py` + `tests/kernel/test_gsb_service.py`，共 22 例（注册/心跳/超时/注销/端点/地址解析/故障切换/旧库补列/非幂等不重放） |
| 旧库升级 | `_ensure_table()` 对早期无 `version` 列的注册表补一次 `ADD COLUMN`（列已存在时忽略），避免旧库上注册被吞成无限重试 |
| 重放安全 | 非幂等请求（POST/PUT/PATCH/DELETE）只在连接期错误（ConnectError/ConnectTimeout）时切换候选；读超时/协议错误直接抛出，防止 `update_issue_state`、`log_obs` 等写操作被重复执行 |
| 状态收敛 | 心跳循环每 20 拍（≈10min）调用一次 `purge_stale()`，兜住 `kill -9` 后残留的 online 行 |
| 多机隔离 | 可选 `GRAPE_GSB_NAMESPACE`：设置后注册键为 `<service>@<namespace>`（如 `mgmt@node-a`），避免两台机器写同一行互相覆盖；客户端优先查本机命名空间行，再回退裸服务名。未设置时保持旧行为 |

实测（本机四进程重启后）：

```
service    base_url                  health     version  status  age
event      http://127.0.0.1:38082    /api/ping  2.2.0    online  <30s
insight    http://127.0.0.1:38084    /api/ping  2.2.0    online  <30s
mgmt       http://127.0.0.1:38081    /api/ping  2.2.0    online  <30s
runtime    http://127.0.0.1:38083    /api/ping  2.2.0    online  <30s
```

`/internal/gsb/services` 无令牌返回 401，带 HMAC 令牌返回 4 条 online 记录；
SIGINT 关闭时打印 `GSB deregistered` 并把行置为 offline，重启后重新 online。

**已知限制（多节点共用一套 DB 时）**：注册表主键是 `service_name`，两个节点
（如本机与兄弟机）同时注册同名服务时会互相覆盖，只剩最后一次心跳。多实例部署前
需要把主键扩为 `(service_name, instance_id)`（`instance_id` = `host:port`），
发现接口按 `local-first → 同机房 → 轮询` 选择实例。本分支尚未改动该语义，
兄弟机也仍是未接线版本。

---

## 8. sweep_loop 拆分

### 8.1 当前结构

```python
# src/runtime/cleanup.py — 当前的 sweep_once()
def sweep_once() -> int:
    cleaned = 0
    cleaned += sweep_stuck_events_once()        # B 维度：回收卡 running 的 events
    cleaned += sweep_stuck_agent_tasks_once()   # C 维度：回收 queued 无认领 / running 僵尸
    cleaned += purge_stale_instances(older_than_hours=24)  # IM 实例清理
    for repo_key, issue_id in _done_issues():   # workdir 清理（依赖 issue_state）
        repogit.cleanup_issue_worktree(...)
    return cleaned

async def sweep_loop():
    while True:
        await asyncio.to_thread(sweep_once)
        await asyncio.sleep(900)  # 15min
```

### 8.2 拆分后

#### Event 进程 — event sweeper

```python
# src/runtime/cleanup.py — 新增
async def sweep_events_loop():
    """Event 进程：僵尸事件回收 + IM 实例清理 + workdir 清理。
    workdir 清理依赖 issue_state（Event 独占表），放在 Event 进程合理。
    """
    while True:
        try:
            cleaned = 0
            cleaned += sweep_stuck_events_once()
            try:
                from src.im.bot_lease import purge_stale_instances
                cleaned += purge_stale_instances(older_than_hours=24)
            except Exception:
                pass
            # workdir 清理（读 issue_state）
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

#### Worker 进程 — task sweeper

```python
# src/runtime/cleanup.py — 新增
async def sweep_tasks_loop():
    """Worker 进程：悬空 agent_tasks 回收。"""
    while True:
        try:
            cleaned = sweep_stuck_agent_tasks_once()
            if cleaned:
                log.info(f"Task sweeper: cleaned {cleaned} stuck tasks")
        except Exception as e:
            log.warning(f"Task sweeper error: {e}")
        await asyncio.sleep(900)  # 15min
```

### 8.3 注意事项

- `sweep_stuck_events_once` 内部会取消 `_DIRECT_FUTURES` 中的排队 Future——迁移后需从 `direct_executor` import `_DIRECT_FUTURES`
- `sweep_stuck_agent_tasks_once` 内部会重派 queued 任务——依赖 Worker 的 `local_claimer` 在线认领，放在 Worker 进程合理
- workdir 清理依赖 `issue_state`（Event 独占表），放在 Event 进程（与事件生命周期强相关）

---

## 9. 共享 DB 表协作

### 9.1 agent_tasks — Event 写入 / Worker 消费 / Mgmt 查看

```
Event 进程                Worker 进程              Mgmt 进程
     │                        │                        │
     │ enqueue_task()         │                        │
     │ INSERT status=queued   │                        │
     │──────────────────►     │                        │
     │                        │ claim() / claim_by_tag │
     │                        │ UPDATE status=running  │
     │                        │                        │
     │                        │ complete_task()        │
     │                        │ UPDATE status=completed│
     │                        │──────────────────►     │
     │                        │                        │ GET /tasks (查看)
     │                        │                        │ POST /cancel (取消)
     │                        │                        │
     │                        │ sweep_stuck_agent_tasks│
     │                        │ (回收僵尸)              │
```

| 操作方 | 操作 | 时机 |
|--------|------|------|
| **Event** | `enqueue_task()` INSERT `queued` | webhook 事件路由后 |
| **Worker** | `claim()` / `claim_by_tag()` UPDATE `queued→running` | local_claimer 认领 |
| **Worker** | `complete_task()` UPDATE `→completed` | Agent 执行完成 |
| **Worker** | `fail_task()` UPDATE `→failed` | Agent 执行失败 |
| **Worker** | `requeue_task()` UPDATE `→queued` | 任务重排队 |
| **Mgmt** | `batch_recover_stale_tasks()` UPDATE | 管理员批量恢复 |
| **Mgmt** | `cancel_task()` UPDATE `→cancelled` | 用户取消 |
| **Worker** | `sweep_stuck_agent_tasks_once()` 回收 | 15min 定时 |

### 9.2 events — Event 写入 / Worker 更新 / Mgmt 展示 / Insight 聚合

```
Event 进程            Worker 进程           Mgmt 进程         Insight 进程
     │                    │                    │                  │
     │ append_event()     │                    │                  │
     │ INSERT → seq       │                    │                  │
     │──────────────►     │                    │                  │
     │                    │ update_event_status│                  │
     │                    │ UPDATE → running   │                  │
     │                    │ update_event_complete                  │
     │                    │ UPDATE → completed │                  │
     │                    │──────────────►     │                  │
     │                    │                    │ GET /detail/{seq}│
     │                    │                    │ POST /terminate  │
     │                    │                    │                  │
     │ sweep_stuck_events │                    │                  │ 聚合统计
     │ (回收卡 running)    │                    │                  │ obs_rollup
     │                    │                    │                  │ ← 读 events
```

### 9.3 managed_repos — Mgmt 配置 / Event 校验 / Worker 读 org_id

| 操作方 | 操作 | 时机 |
|--------|------|------|
| **Mgmt** | POST /host 托管仓库、PUT /bot-token 设置 token | 用户配置 |
| **Event** | webhook token 校验（读取 webhook_token） | webhook 到达 |
| **Event** | 读取 bot_username、deploy_url | 事件处理 |
| **Worker** | 读取 org_id（引擎选择隔离） | 引擎选择 |

### 9.4 issue_state — Event 写入阶段 / Mgmt 查看/操作

| 操作方 | 操作 | 时机 |
|--------|------|------|
| **Event** | `set_issue_stage()` 阶段初始化与推进 | event_pipeline 路由判定后 |
| **Mgmt** | GET status | 用户查看 Issue 状态 |
| **Mgmt** | PUT status（confirm-done/cancel） | 用户面状态流转 |
| **Worker** | 读取活跃 session 判断可否清理 workdir | sweep 时读取 |

### 9.5 distributed_locks — 四进程共用

| 锁 key | 使用方 | TTL | 归属进程 |
|--------|--------|-----|---------|
| `table_maintenance_tasks` | Worker table_maintenance | 3600s | Worker |
| `table_maintenance_messages` | Insight table_maintenance | 3600s | Insight |
| `autopilot_scheduler` | scheduler.py | 120s | Worker |
| `feedback_reconcile` | feedback_issue_converter.py | 300s | Mgmt |
| `token_health_check` | bot_token_health.py | LOCK_TTL | Event |
| knowledge CRUD locks | knowledge.py | 60-300s | Mgmt |
| deps.py refresh locks | deps.py | 30s | Mgmt + Worker |

> **跨进程锁安全**：`distributed_locks` 表四进程共用，通过 DB 行级锁 + `db_lock`（SQLite）保证原子性。MySQL 后端使用 `SELECT ... FOR UPDATE` 或 `INSERT OR IGNORE` 实现分布式锁语义。

### 9.6 obs_log — Runtime 写日志 / Insight 聚合

| 操作方 | 操作 | 时机 |
|--------|------|------|
| **Runtime（Event/Worker）** | `log_obs()` INSERT | 事件处理/任务执行日志 |
| **Insight** | `obs_rollup` 聚合 | 60s 定时 |
| **Insight** | `obs_alert` 求值 | 30s 定时 |
| **Mgmt** | GET 面板展示 | 用户查看（通过 InsightService） |

---

## 10. nginx 反代配置

按路径分流到四个进程端口：

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
upstream grape_insight_server {
    server 127.0.0.1:38083;
}

server {
    listen 443 ssl;
    server_name grape.example.com;

    # gzip 压缩
    gzip on;
    gzip_types application/json;
    gzip_min_length 10k;
    gzip_comp_level 5;
    gzip_vary on;

    # 多图上传放行
    client_max_body_size 50m;

    # ===== Worker 进程 (:38082) =====
    # Worker API（注册/心跳/认领/完成等 POST 端点）
    location /api/runtime/ {
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
    }

    # ===== Event 进程 (:38081) =====
    # Webhook 接收
    location /webhook/ {
        proxy_pass http://grape_event_server;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }

    # A2A 交接
    location /api/a2a {
        proxy_pass http://grape_event_server;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
    }

    # events 操作端点（terminate/redispatch）
    location ~ ^/api/events/.*/(terminate|redispatch) {
        proxy_pass http://grape_event_server;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
    }

    # ===== Insight 进程 (:38083) =====
    # 可观测性面板
    location /api/obs/ {
        proxy_pass http://grape_insight_server;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
    }

    # 仪表盘
    location /api/dashboard/ {
        proxy_pass http://grape_insight_server;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
    }

    # 全局搜索
    location /api/search {
        proxy_pass http://grape_insight_server;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
    }

    # 延迟上报
    location /api/latency {
        proxy_pass http://grape_insight_server;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
    }

    # ===== Mgmt 进程 (:38080) — 默认 =====
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

    # SSE 流（Mgmt 代理 Runtime 的 task_messages）
    location /api/task-logs/ {
        proxy_pass http://grape_server;
        proxy_http_version 1.1;
        proxy_set_header Connection "";
        proxy_buffering off;
    }
}
```

**路径分流汇总**：

| 路径 | 目标端口 | 进程 |
|------|---------|------|
| /api/runtime/ | :38082 | Worker |
| /api/tasks/(notify\|complete\|current) | :38082 | Worker |
| /webhook/ | :38081 | Event |
| /api/a2a | :38081 | Event |
| /api/events/.*/(terminate\|redispatch) | :38081 | Event |
| /api/obs/ | :38083 | Insight |
| /api/dashboard/ | :38083 | Insight |
| /api/search | :38083 | Insight |
| /api/latency | :38083 | Insight |
| /api/task-logs/ | :38080 | Mgmt（SSE 代理） |
| / (默认) | :38080 | Mgmt |

---

## 11. 部署双模式

### 11.1 单机模式（GRAPE_MODE=single）

```bash
GRAPE_MODE=single
DEVOPS_DB_BACKEND=sqlite  # 单库 + 表前缀

# 启动四进程
python apps/mgmt/main.py --port 38080 &
python apps/runtime/webhook/main.py --port 38081 &
python apps/runtime/worker/main.py --port 38082 &
python apps/insight/main.py --port 38083 &
```

- SQLite 单文件，表前缀模拟 schema（mgmt_/rt_/ins_）
- 无 Redis / Kafka / etcd
- 四进程同主机
- 契约接口实现 = 同实例跨前缀 SQL 查询
- `db_lock` 只保证进程内线程安全，跨进程由 SQLite 文件锁兜底（`PRAGMA journal_mode=WAL` + `busy_timeout ≥ 5000ms`）

### 11.2 集群模式（GRAPE_MODE=cluster）

```bash
GRAPE_MODE=cluster
DEVOPS_DB_BACKEND=mysql
DEVOPS_DB_HOST=mysql.internal
DEVOPS_DB_NAME=grape  # 三 schema 在同一 DB

# 每个进程独立部署
# Mgmt: 2 副本
# Event: 1 副本
# Worker: 按引擎负载扩缩
# Insight: 1 副本
```

- MySQL 单实例三 schema
- 契约接口实现 = 同实例跨 schema SQL 查询
- 未来分库时改为 HTTP API

### 11.3 systemd service 文件（4 个）

#### grape-server.service（Mgmt 进程）

```ini
# /etc/systemd/system/grape-server.service
[Unit]
Description=Grape Server (Mgmt - Workbench API)
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

#### grape-event-server.service（Event 进程）

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

#### grape-worker-server.service（Worker 进程）

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
    --host 0.0.0.0 --port 38082 --workers 2
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

#### grape-insight-server.service（Insight 进程）

```ini
# /etc/systemd/system/grape-insight-server.service
[Unit]
Description=Grape Insight Server (Observability + Dashboard + SLO)
After=network.target grape-server.service

[Service]
User=grape
WorkingDirectory=/opt/grape
Environment=PYTHONUNBUFFERED=1
Environment=GRAPE_PROCESS_ROLE=insight
Environment=GRAPE_PORT=38083
Environment=OBS_LOG_DB=1
EnvironmentFile=/etc/grape/grape.env
ExecStart=/opt/grape/.venv/bin/uvicorn src.api.grape_insight_server:app \
    --host 0.0.0.0 --port 38083 --workers 1
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

#### 环境变量文件

```bash
# /etc/grape/grape.env — 四进程共用
GRAPE_DB_PATH=/opt/grape/data/grape.db
SERVER_BASE_URL=https://grape.example.com
JWT_SECRET=<your-secret>
BOT_LEASE_DISABLED=false
OBS_LOG_DB=1
METRICS_ADDR=9090
SWEEPER_INTERVAL_SECONDS=900
```

#### 启动/停止命令

```bash
# 启动四进程
systemctl daemon-reload
systemctl enable --now grape-server grape-event-server grape-worker-server grape-insight-server

# 停止
systemctl stop grape-server grape-event-server grape-worker-server grape-insight-server

# 重启单个进程
systemctl restart grape-insight-server

# 查看状态
systemctl is-active grape-server grape-event-server grape-worker-server grape-insight-server

# 健康检查
curl -s http://127.0.0.1:38080/health    # {"status":"ok","service":"server"}
curl -s http://127.0.0.1:38081/health    # {"status":"ok","service":"event-server"}
curl -s http://127.0.0.1:38082/health    # {"status":"ok","service":"worker-server"}
curl -s http://127.0.0.1:38083/health    # {"status":"ok","service":"insight-server"}
```

### 11.4 Docker Compose 配置

```yaml
# deploy/docker-compose.yml
version: "3.8"

services:
  grape-mgmt:
    build: .
    command: uvicorn src.api.grape_server:app --host 0.0.0.0 --port 38080 --workers 2
    ports:
      - "38080:38080"
    environment:
      - GRAPE_PROCESS_ROLE=server
      - GRAPE_MODE=single
      - DEVOPS_DB_BACKEND=sqlite
      - GRAPE_DB_PATH=/data/grape.db
    volumes:
      - ./data:/data
      - ./configs:/app/configs
    restart: always

  grape-event:
    build: .
    command: uvicorn src.api.grape_event_server:app --host 0.0.0.0 --port 38081 --workers 1
    ports:
      - "38081:38081"
    environment:
      - GRAPE_PROCESS_ROLE=event
      - GRAPE_MODE=single
      - DEVOPS_DB_BACKEND=sqlite
      - GRAPE_DB_PATH=/data/grape.db
    volumes:
      - ./data:/data
      - ./configs:/app/configs
    restart: always

  grape-worker:
    build: .
    command: uvicorn src.api.grape_worker_server:app --host 0.0.0.0 --port 38082 --workers 2
    ports:
      - "38082:38082"
    environment:
      - GRAPE_PROCESS_ROLE=worker
      - GRAPE_LOCAL_CLAIMER=1
      - RUNTIME_MAX_CONCURRENT_TASKS=20
      - GRAPE_MODE=single
      - DEVOPS_DB_BACKEND=sqlite
      - GRAPE_DB_PATH=/data/grape.db
    volumes:
      - ./data:/data
      - ./configs:/app/configs
    restart: always

  grape-insight:
    build: .
    command: uvicorn src.api.grape_insight_server:app --host 0.0.0.0 --port 38083 --workers 1
    ports:
      - "38083:38083"
    environment:
      - GRAPE_PROCESS_ROLE=insight
      - OBS_LOG_DB=1
      - GRAPE_MODE=single
      - DEVOPS_DB_BACKEND=sqlite
      - GRAPE_DB_PATH=/data/grape.db
    volumes:
      - ./data:/data
      - ./configs:/app/configs
    restart: always

  nginx:
    image: nginx:alpine
    ports:
      - "443:443"
    volumes:
      - ./deploy/nginx-grape.conf:/etc/nginx/conf.d/grape.conf
    depends_on:
      - grape-mgmt
      - grape-event
      - grape-worker
      - grape-insight
    restart: always
```

### 11.5 开发模式启动脚本

```bash
#!/bin/bash
# scripts/dev_four_process.sh — 开发模式同时启动四进程

cd /home/grape/grape-new-version2

# 终端 1：Mgmt 进程
uvicorn src.api.grape_server:app --host 0.0.0.0 --port 38080 --reload &

# 终端 2：Event 进程
uvicorn src.api.grape_event_server:app --host 0.0.0.0 --port 38081 --reload &

# 终端 3：Worker 进程
uvicorn src.api.grape_worker_server:app --host 0.0.0.0 --port 38082 --reload &

# 终端 4：Insight 进程
uvicorn src.api.grape_insight_server:app --host 0.0.0.0 --port 38083 --reload &

wait
```

---

## 12. 完整迁移路径

| Phase | 内容 | 预估工作量 | 依赖 |
|-------|------|-----------|------|
| **Phase 0** | 清理去重 + 技能统一 | 3-5 天 | 无 |
| **Phase 1** | kernel/ 提取 + domains/ 域内聚 | 8-12 天 | Phase 0 完成 |
| **Phase 2** | 分 Schema + 表迁移 | 5-7 天 | Phase 1 完成 |
| **Phase 3** | _execute_agent 迁移 | 2-3 天 | Phase 1 完成 |
| **Phase 4** | 四进程拆分（Mgmt + Event + Worker + Insight） | 5-7 天 | Phase 2 + Phase 3 完成 |
| **Phase 5** | 契约接口填充 | 3-5 天 | Phase 4 完成 |
| **Phase 6** | 部署双模式验证 | 2-3 天 | Phase 5 完成 |

### Phase 0：清理去重 + 技能统一

1. 删除 admin/（旧版残留）
2. 删除 scenes/（与 configs/scenes/ 重复）
3. tasks/ 内容移入 docs/plans/
4. nginx-grape.conf、Dockerfile、docker-compose.yml 移入 deploy/
5. data/ 和 wheels/ 加 .gitkeep + 确认 .gitignore
6. 创建 Makefile 统一构建入口
7. 创建 .grape/skills/{official,builtin,opencode,cli}/
8. skills/ → .grape/skills/official/
9. .grape/skills/ 原有内容 → .grape/skills/builtin/
10. .opencode/skills/ → .grape/skills/opencode/
11. .skills/ → .grape/skills/cli/
12. 删除顶层 skills/、.opencode/skills/、.skills/
13. 更新 config.py SKILLS_DIR、skill_sync.py、scene_sync.py、worker_package.py MANIFEST
14. 创建 pyproject.toml，合并 4 个 requirements 文件

### Phase 1：kernel/ 提取 + domains/ 域内聚

1. 创建 src/kernel/ 目录
2. 移动 16 个共享模块到 src/kernel/
3. 创建 src/kernel/auth_sdk/，下沉 jwt_utils/auth_token/cookie_utils
4. 创建 src/kernel/db_migrations/ 目录（空壳）
5. 创建 src/domains/{mgmt,runtime,insight,contracts}/ 目录结构
6. auth/ → domains/mgmt/auth/
7. user/ → domains/mgmt/user/
8. admin/ → domains/mgmt/admin/
9. runtime/ → domains/runtime/engine/
10. agent/ → domains/runtime/agents/
11. im/ → domains/runtime/im/
12. vcs/ → domains/runtime/vcs/
13. storage/ → domains/runtime/storage/
14. observability/ → domains/insight/observability/
15. event_pipeline.py → domains/runtime/pipeline/
16. 路由文件按域归入 domains/*/routes/
17. 在 src/ 顶层创建 re-export 兼容层
18. 创建 scripts/check-arch.py + tests/arch/
19. 运行全量测试

### Phase 2：分 Schema + 表迁移

1. 创建 src/kernel/db.py — schema 前缀管理
2. 创建 src/domains/contracts/ — 空壳接口定义
3. 编写 schema 迁移脚本 — MySQL `CREATE SCHEMA` + `RENAME TABLE`
4. 编写 SQLite 兼容层 — 表前缀映射
5. 执行 MySQL schema 迁移（85 张表分到 3 个 schema）
6. 全量替换 SQL 中的表名为 `t_mgmt()` / `t_runtime()` / `t_insight()` 调用
7. 验证跨 schema 查询和事务
8. 全量测试

### Phase 3：_execute_agent 迁移

1. 新建 src/domains/runtime/engine/direct_executor.py
2. _execute_agent / _execute_agent_inner / start_direct_agent / _DIRECT_POOL / _DIRECT_FUTURES 迁移
3. event_pipeline.py 中 5 处 start_direct_agent 调用改为 enqueue_task
4. local_claimer import 改为 from src.domains.runtime.engine.direct_executor
5. cleanup.sweep_stuck_events_once 的 _DIRECT_FUTURES import 更新
6. 全量测试

### Phase 4：四进程拆分（Mgmt + Event + Worker + Insight）

1. 创建 apps/mgmt/main.py、apps/runtime/webhook/main.py、apps/runtime/worker/main.py、apps/insight/main.py
2. 创建 src/api/grape_server.py（Mgmt 入口）
3. 创建 src/api/grape_event_server.py（Event 入口）
4. 创建 src/api/grape_worker_server.py（Worker 入口）
5. 创建 src/api/grape_insight_server.py（Insight 入口）
6. 路由按进程分配（含跨进程路由处理）
7. 定时任务按进程分配（含 Insight 定时任务独立）
8. sweep_loop 拆分为 sweep_events_loop + sweep_tasks_loop
9. table_maintenance 拆分（agent_tasks → Worker，task_messages → Insight）
10. nginx 反代配置（四端口）
11. systemd service 文件（4 个）
12. 验证四进程独立启动 + 协作

### Phase 5：契约接口填充

1. MgmtService 实现类填充
2. RuntimeService 实现类填充
3. InsightService 实现类填充
4. 跨 schema SQL 调用收口到契约接口
5. 消除裸跨 schema SQL
6. 全量测试

### Phase 6：部署双模式验证

1. 单机模式（SQLite + 表前缀）验证
2. 集群模式（MySQL + 三 schema）验证
3. Docker Compose 配置
4. 部署文档
5. 回归测试

---

## 13. 风险与缓解

| 风险 | 等级 | 缓解 |
|------|------|------|
| 85 张表迁移到 3 schema 的工作量大 | 中 | 自动化迁移脚本 + 分批迁移 |
| 跨 schema SQL 替换为 t_mgmt()/t_runtime() 涉及大量代码 | 中 | 批量替换脚本 + re-export 兼容 |
| SQLite 不支持 schema，表前缀方案增加复杂度 | 中 | kernel/db.py 统一封装，业务代码无感 |
| 跨 schema 事务在 MySQL 中可用但语义需验证 | 低 | 同实例跨 schema 事务 MySQL 原生支持 |
| issues.py / chat.py 跨进程路由注册 | 中 | router include 过滤，按端点 method 分配 |
| 契约接口填充前跨 schema SQL 仍然散落 | 低 | Phase 5 统一收口 |
| 四进程同时写 distributed_locks 可能冲突 | 低 | distributed_locks 表用原子 UPDATE，不依赖 db_lock |
| events 表四角共享（B 写/C 更新/A 查看/Insight 聚合） | 中 | status 字段原子 UPDATE（WHERE seq=? AND status=?），B sweeper 只回收 30min+ running 事件 |
| issue_state 竞态（B 推进阶段 vs A 用户操作） | 中 | SQLite db_lock 串行化；MySQL 行级锁 + 乐观锁；语义上 B 推进与 A 操作有时序 |
| managed_repos 配置一致性 | 低 | B 每次 webhook 直查 DB（不缓存），A 修改后下次 webhook 自然读到新值 |
| event_pipeline.py 拆分复杂度（5000+ 行） | 高 | Phase 3 先迁移 _execute_agent（约 200 行），不改动 run_pipeline 路由逻辑 |
| db_lock 跨进程约束 | 高 | SQLite WAL 模式 + busy_timeout ≥ 5000ms；MySQL 退化为 no-op；跨进程互斥用 distributed_locks 表 |
| local_claimer 认领延迟（3s） | 中 | affinity_runtime_id 优先认领；用户面手动触发可接受 |
| IM WebSocket 归属 | 低 | 归 Mgmt 进程（用户面聊天依赖），bot_manager 只在 Mgmt 启动 |
| Insight 进程独立后 obs_log 写入跨进程 | 中 | Runtime 通过 InsightService.log_obs() 写入（同实例跨 schema SQL），未来改 HTTP API |
| domains/ 重组导致 import 断裂 | 高 | re-export 兼容层，逐步迁移 |
| kernel/ 提取影响 130+ 处引用 | 中 | 先做 config.py 验证通过再批量迁移 |
| 技能路径变更导致同步失败 | 中 | 先更新 sync 脚本再移动文件 |
| Worker MANIFEST 路径失效 | 中 | 同步更新 MANIFEST + 测试 |

---

## 14. 详细验证用例

每个验证用例包含：用例编号、验证目标、前置条件、操作步骤、预期结果、验证命令/脚本。

### Phase 0 验证用例（5 个）

#### TC-0.1：重复目录删除验证

| 项 | 内容 |
|----|------|
| **用例编号** | TC-0.1 |
| **验证目标** | 删除 scenes/、admin/、tasks/ 归入 docs/plans/ 后无残留引用 |
| **前置条件** | Phase 0 清理完成 |
| **操作步骤** | 1. 确认 scenes/ 目录不存在 2. 确认 admin/ 顶层目录不存在 3. 确认 tasks/ 内容在 docs/plans/ 4. 全局搜索 `scenes/` 路径引用 |
| **预期结果** | 目录已删除/迁移，无残留代码引用 |
| **验证命令** | `test ! -d scenes/ && echo "OK" && grep -r "scenes/" src/ --include="*.py" \| grep -v configs/scenes` |

#### TC-0.2：技能目录统一验证

| 项 | 内容 |
|----|------|
| **用例编号** | TC-0.2 |
| **验证目标** | 4 套技能目录合并到 .grape/skills/ 后技能加载正常 |
| **前置条件** | Phase 0 技能统一完成，config.py SKILLS_DIR 已更新 |
| **操作步骤** | 1. 确认 .grape/skills/{official,builtin,opencode,cli}/ 存在 2. 确认顶层 skills/、.opencode/skills/、.skills/ 已删除 3. 启动服务 4. 查询技能列表 API |
| **预期结果** | 技能列表完整，包含原有全部技能 |
| **验证命令** | `ls .grape/skills/ && curl -s http://127.0.0.1:38080/api/skills \| jq length` |

#### TC-0.3：部署文件归入 deploy/ 验证

| 项 | 内容 |
|----|------|
| **用例编号** | TC-0.3 |
| **验证目标** | nginx-grape.conf、Dockerfile、docker-compose.yml 移入 deploy/ |
| **前置条件** | Phase 0 文件迁移完成 |
| **操作步骤** | 1. 确认 deploy/nginx-grape.conf 存在 2. 确认 deploy/Dockerfile 存在 3. 确认 deploy/docker-compose.yml 存在 4. 确认顶层无这些文件 |
| **预期结果** | 部署文件全部在 deploy/ 目录 |
| **验证命令** | `ls deploy/nginx-grape.conf deploy/Dockerfile deploy/docker-compose.yml && test ! -f nginx-grape.conf` |

#### TC-0.4：pyproject.toml 统一依赖验证

| 项 | 内容 |
|----|------|
| **用例编号** | TC-0.4 |
| **验证目标** | 4 个 requirements*.txt 合并为 pyproject.toml 后依赖安装正常 |
| **前置条件** | Phase 0 依赖统一完成 |
| **操作步骤** | 1. 确认 pyproject.toml 存在 2. 确认 requirements*.txt 已删除 3. 创建 venv 4. `pip install -e .` 5. 启动服务 |
| **预期结果** | 依赖安装成功，服务正常启动 |
| **验证命令** | `test -f pyproject.toml && pip install -e . 2>&1 \| tail -5 && python -c "import fastapi; import uvicorn; print('OK')"` |

#### TC-0.5：Makefile 构建入口验证

| 项 | 内容 |
|----|------|
| **用例编号** | TC-0.5 |
| **验证目标** | Makefile 统一构建入口可用 |
| **前置条件** | Phase 0 Makefile 创建完成 |
| **操作步骤** | 1. 确认 Makefile 存在 2. 执行 `make help` 3. 执行 `make test` |
| **预期结果** | Makefile 命令可用，测试通过 |
| **验证命令** | `test -f Makefile && make help && make test 2>&1 \| tail -10` |

### Phase 1 验证用例（6 个）

#### TC-1.1：kernel/ 提取验证

| 项 | 内容 |
|----|------|
| **用例编号** | TC-1.1 |
| **验证目标** | 16 个共享模块移入 src/kernel/ 后 import 兼容 |
| **前置条件** | Phase 1 kernel/ 提取完成 |
| **操作步骤** | 1. 确认 src/kernel/config.py 存在 2. 确认 src/kernel/session_manager.py 存在 3. 测试 `from src.kernel.config import log` 4. 测试 `from src.config import log`（re-export 兼容） |
| **预期结果** | 新旧 import 路径均可正常导入 |
| **验证命令** | `python -c "from src.kernel.config import log; print('new OK')" && python -c "from src.config import log; print('old OK')"` |

#### TC-1.2：auth_sdk 下沉验证

| 项 | 内容 |
|----|------|
| **用例编号** | TC-1.2 |
| **验证目标** | jwt_utils/auth_token/cookie_utils 下沉到 kernel/auth_sdk/ |
| **前置条件** | Phase 1 auth_sdk 下沉完成 |
| **操作步骤** | 1. 确认 src/kernel/auth_sdk/jwt_utils.py 存在 2. 测试 import 3. 测试 JWT 生成/验证 |
| **预期结果** | auth_sdk 模块可用，JWT 功能正常 |
| **验证命令** | `python -c "from src.kernel.auth_sdk.jwt_utils import create_jwt; print('OK')"` |

#### TC-1.3：domains/ 域内聚验证

| 项 | 内容 |
|----|------|
| **用例编号** | TC-1.3 |
| **验证目标** | src/ 重组为 domains/{mgmt,runtime,insight}/ 后代码可加载 |
| **前置条件** | Phase 1 域内聚完成 |
| **操作步骤** | 1. 确认 src/domains/mgmt/auth/ 存在 2. 确认 src/domains/runtime/engine/ 存在 3. 确认 src/domains/insight/observability/ 存在 4. 启动服务 |
| **预期结果** | 域目录结构就位，服务正常启动 |
| **验证命令** | `ls src/domains/mgmt/auth/ src/domains/runtime/engine/ src/domains/insight/observability/ && python -c "from src.api.main import app; print('OK')"` |

#### TC-1.4：check-arch CI 红牌验证

| 项 | 内容 |
|----|------|
| **用例编号** | TC-1.4 |
| **验证目标** | scripts/check-arch.py 依赖环检测可用 |
| **前置条件** | Phase 1 check-arch 脚本创建完成 |
| **操作步骤** | 1. 执行 `python scripts/check-arch.py` 2. 确认检测 R1~R6 规则 3. 确认白名单机制 |
| **预期结果** | 检测脚本运行，输出依赖环报告 |
| **验证命令** | `python scripts/check-arch.py 2>&1 \| head -20` |

#### TC-1.5：re-export 兼容层验证

| 项 | 内容 |
|----|------|
| **用例编号** | TC-1.5 |
| **验证目标** | src/ 顶层 re-export 文件保证旧 import 路径可用 |
| **前置条件** | Phase 1 re-export 兼容层创建完成 |
| **操作步骤** | 1. 测试 `from src.auth.audit import *` 2. 测试 `from src.domains.mgmt.auth.audit import *` 3. 全量测试通过 |
| **预期结果** | 新旧 import 路径均可正常导入 |
| **验证命令** | `python -c "from src.auth.audit import *; print('old OK')" && python -c "from src.domains.mgmt.auth.audit import *; print('new OK')"` |

#### TC-1.6：全量测试回归验证

| 项 | 内容 |
|----|------|
| **用例编号** | TC-1.6 |
| **验证目标** | Phase 1 完成后全量测试无回归 |
| **前置条件** | Phase 1 全部完成 |
| **操作步骤** | 1. 运行 `pytest tests/ -v` 2. 检查测试通过率 3. 检查无新增 import 错误 |
| **预期结果** | 全量测试通过率 ≥ Phase 0 基线 |
| **验证命令** | `pytest tests/ -v 2>&1 \| tail -20` |

### Phase 2 验证用例（7 个）

#### TC-2.1：kernel/db.py schema 管理验证

| 项 | 内容 |
|----|------|
| **用例编号** | TC-2.1 |
| **验证目标** | kernel/db.py 的 table()/t_mgmt()/t_runtime()/t_insight() 函数可用 |
| **前置条件** | Phase 2 kernel/db.py 创建完成 |
| **操作步骤** | 1. 测试 SQLite 模式：`t_mgmt('managed_repos')` 返回 `mgmt_managed_repos` 2. 测试 MySQL 模式：`t_mgmt('managed_repos')` 返回 `grape_mgmt.managed_repos` |
| **预期结果** | 两种模式下表名前缀/限定正确 |
| **验证命令** | `DEVOPS_DB_BACKEND=sqlite python -c "from src.kernel.db import t_mgmt; assert t_mgmt('managed_repos')=='mgmt_managed_repos'; print('OK')"` |

#### TC-2.2：MySQL schema 创建验证

| 项 | 内容 |
|----|------|
| **用例编号** | TC-2.2 |
| **验证目标** | MySQL `CREATE SCHEMA grape_mgmt / grape_runtime / grape_insight` 成功 |
| **前置条件** | MySQL 实例可用，schema 迁移脚本就绪 |
| **操作步骤** | 1. 执行 schema 迁移脚本 2. 确认三个 schema 存在 3. 确认表已分配到对应 schema |
| **预期结果** | 三个 schema 创建成功，85 张表正确分配 |
| **验证命令** | `mysql -e "SELECT TABLE_SCHEMA, COUNT(*) FROM information_schema.tables WHERE TABLE_SCHEMA LIKE 'grape_%' GROUP BY TABLE_SCHEMA;"` |

#### TC-2.3：SQLite 表前缀兼容验证

| 项 | 内容 |
|----|------|
| **用例编号** | TC-2.3 |
| **验证目标** | SQLite 模式下表前缀（mgmt_/rt_/ins_）正确工作 |
| **前置条件** | SQLite 兼容层创建完成 |
| **操作步骤** | 1. 使用 SQLite 后端启动 2. 确认 `mgmt_managed_repos` 表存在 3. 确认 `rt_events` 表存在 4. 确认 `ins_obs_log` 表存在 |
| **预期结果** | 表前缀正确应用 |
| **验证命令** | `sqlite3 data/grape.db ".tables" \| grep -E "mgmt_|rt_|ins_"` |

#### TC-2.4：跨 schema 查询验证

| 项 | 内容 |
|----|------|
| **用例编号** | TC-2.4 |
| **验证目标** | MySQL 跨 schema SQL 查询正常 |
| **前置条件** | MySQL 三 schema 表迁移完成 |
| **操作步骤** | 1. 执行跨 schema JOIN：`SELECT * FROM grape_runtime.events e JOIN grape_mgmt.managed_repos r ON e.repo_key=r.repo_key` 2. 确认查询成功 |
| **预期结果** | 跨 schema 查询返回正确结果 |
| **验证命令** | `mysql -e "SELECT COUNT(*) FROM grape_runtime.events e JOIN grape_mgmt.managed_repos r ON e.repo_key=r.repo_key;"` |

#### TC-2.5：跨 schema 事务验证

| 项 | 内容 |
|----|------|
| **用例编号** | TC-2.5 |
| **验证目标** | MySQL 跨 schema 事务（BEGIN...COMMIT）正常 |
| **前置条件** | MySQL 三 schema 就绪 |
| **操作步骤** | 1. 执行跨 schema 事务：`BEGIN; UPDATE grape_mgmt.managed_repos SET ...; INSERT INTO grape_runtime.events ...; COMMIT;` 2. 确认两表都更新成功 |
| **预期结果** | 事务原子性保证 |
| **验证命令** | `mysql -e "BEGIN; UPDATE grape_mgmt.managed_repos SET bot_username='test' WHERE repo_key='test'; INSERT INTO grape_runtime.events(...) VALUES(...); COMMIT;"` |

#### TC-2.6：contracts/ 空壳接口验证

| 项 | 内容 |
|----|------|
| **用例编号** | TC-2.6 |
| **验证目标** | src/domains/contracts/ 空壳接口定义就位 |
| **前置条件** | Phase 2 contracts/ 创建完成 |
| **操作步骤** | 1. 确认 mgmt_service.py 存在 2. 确认 runtime_service.py 存在 3. 确认 insight_service.py 存在 4. 测试 import |
| **预期结果** | 三个契约接口文件可导入 |
| **验证命令** | `python -c "from src.domains.contracts.mgmt_service import MgmtService; from src.domains.contracts.runtime_service import RuntimeService; from src.domains.contracts.insight_service import InsightService; print('OK')"` |

#### TC-2.7：全量 SQL 替换验证

| 项 | 内容 |
|----|------|
| **用例编号** | TC-2.7 |
| **验证目标** | 所有 SQL 中的表名已替换为 t_mgmt()/t_runtime()/t_insight() 调用 |
| **前置条件** | Phase 2 全量替换完成 |
| **操作步骤** | 1. 全局搜索裸表名 SQL（不含 t_mgmt/t_runtime/t_insight） 2. 确认无遗漏 |
| **预期结果** | 无裸表名 SQL（除 re-export 兼容层） |
| **验证命令** | `grep -rn "FROM managed_repos" src/ --include="*.py" \| grep -v "t_mgmt\|t_runtime\|t_insight\|__init__"` |

### Phase 3 验证用例（5 个）

#### TC-3.1：direct_executor.py 创建验证

| 项 | 内容 |
|----|------|
| **用例编号** | TC-3.1 |
| **验证目标** | direct_executor.py 包含 _execute_agent 系列函数 |
| **前置条件** | Phase 3 迁移完成 |
| **操作步骤** | 1. 确认文件存在 2. 确认 _DIRECT_POOL 定义 3. 确认 _execute_agent 定义 4. 确认 _execute_agent_inner 定义 |
| **预期结果** | 所有函数和变量已迁移 |
| **验证命令** | `grep -n "_DIRECT_POOL\|_execute_agent\|_execute_agent_inner\|start_direct_agent" src/domains/runtime/engine/direct_executor.py` |

#### TC-3.2：event_pipeline.py direct 调用移除验证

| 项 | 内容 |
|----|------|
| **用例编号** | TC-3.2 |
| **验证目标** | event_pipeline.py 中 5 处 start_direct_agent 调用已改为 enqueue_task |
| **前置条件** | Phase 3 迁移完成 |
| **操作步骤** | 1. 搜索 event_pipeline.py 中的 `start_direct_agent` 调用 2. 确认已全部移除 3. 搜索 `enqueue_task` 调用 4. 确认 5 处新增 |
| **预期结果** | 无 start_direct_agent 调用，5 处 enqueue_task 替代 |
| **验证命令** | `grep -c "start_direct_agent" src/domains/runtime/pipeline/event_pipeline.py && grep -c "enqueue_task" src/domains/runtime/pipeline/event_pipeline.py` |

#### TC-3.3：local_claimer import 路径验证

| 项 | 内容 |
|----|------|
| **用例编号** | TC-3.3 |
| **验证目标** | local_claimer.py import 改为从 direct_executor 导入 |
| **前置条件** | Phase 3 迁移完成 |
| **操作步骤** | 1. 检查 local_claimer.py 的 import 语句 2. 确认 `from src.domains.runtime.engine.direct_executor import` 3. 确认无 `from src.api.event_pipeline import _execute_agent` |
| **预期结果** | import 路径正确 |
| **验证命令** | `grep "import.*_execute_agent\|import.*start_direct_agent" src/domains/runtime/engine/local_claimer.py` |

#### TC-3.4：webhook → enqueue → claim → execute 链路验证

| 项 | 内容 |
|----|------|
| **用例编号** | TC-3.4 |
| **验证目标** | webhook 事件入队后 local_claimer 认领并执行 |
| **前置条件** | Phase 3 完成，单进程模式运行 |
| **操作步骤** | 1. 发送 webhook 2. 确认 agent_tasks 表有 queued 行 3. 等待 3s 4. 确认状态变为 running 5. 等待执行完成 6. 确认状态变为 completed + events.status=completed |
| **预期结果** | 全链路正常 |
| **验证命令** | `curl -X POST http://127.0.0.1:38080/webhook/gitcode -H "X-Gitcode-Token: ..." -d '...' && sleep 5 && sqlite3 data/grape.db "SELECT status FROM rt_agent_tasks ORDER BY id DESC LIMIT 1;"` |

#### TC-3.5：_DIRECT_FUTURES 取消验证

| 项 | 内容 |
|----|------|
| **用例编号** | TC-3.5 |
| **验证目标** | sweep_stuck_events_once 能取消 _DIRECT_FUTURES 中的排队 Future |
| **前置条件** | Phase 3 完成 |
| **操作步骤** | 1. 制造一个卡 running 事件 2. 等待 15min（或手动触发 sweep） 3. 确认 _DIRECT_FUTURES 中的 Future 被取消 |
| **预期结果** | 排队 Future 正确取消 |
| **验证命令** | `python -c "from src.domains.runtime.engine.direct_executor import _DIRECT_FUTURES; print(len(_DIRECT_FUTURES))"` |

### Phase 4 验证用例（12 个）

#### TC-4.1：四进程独立启动验证

| 项 | 内容 |
|----|------|
| **用例编号** | TC-4.1 |
| **验证目标** | 四进程可独立启动，各自监听正确端口 |
| **前置条件** | Phase 4 四进程入口创建完成 |
| **操作步骤** | 1. 启动 grape_server (:38080) 2. 启动 grape_event_server (:38081) 3. 启动 grape_worker_server (:38082) 4. 启动 grape_insight_server (:38083) 5. 分别健康检查 |
| **预期结果** | 四进程各自返回 `{"status":"ok"}` |
| **验证命令** | `curl -s http://127.0.0.1:38080/health && curl -s http://127.0.0.1:38081/health && curl -s http://127.0.0.1:38082/health && curl -s http://127.0.0.1:38083/health` |

#### TC-4.2：Mgmt 进程路由可达验证

| 项 | 内容 |
|----|------|
| **用例编号** | TC-4.2 |
| **验证目标** | Mgmt 进程 25 个用户面路由可达 |
| **前置条件** | 四进程启动 |
| **操作步骤** | 1. 测试 /api/auth/profile 2. 测试 /api/repos/member 3. 测试 /api/dashboard/ops/overview 4. 测试 /api/settings/credentials 5. 测试 /api/scenes |
| **预期结果** | 所有 Mgmt 路由返回正确响应 |
| **验证命令** | `curl -s http://127.0.0.1:38080/api/health && curl -s http://127.0.0.1:38080/api/repos/member && curl -s http://127.0.0.1:38080/api/dashboard/ops/overview` |

#### TC-4.3：Event 进程路由可达验证

| 项 | 内容 |
|----|------|
| **用例编号** | TC-4.3 |
| **验证目标** | Event 进程 webhook + events 操作端点可达 |
| **前置条件** | 四进程启动 |
| **操作步骤** | 1. 测试 /health 2. 测试 POST /webhook/gitcode（带 token） 3. 测试 GET /api/a2a/status |
| **预期结果** | webhook 接收成功返回 200，A2A status 可达 |
| **验证命令** | `curl -s http://127.0.0.1:38081/health && curl -s http://127.0.0.1:38081/api/a2a/status` |

#### TC-4.4：Worker 进程路由可达验证

| 项 | 内容 |
|----|------|
| **用例编号** | TC-4.4 |
| **验证目标** | Worker 进程 POST 端点（register/heartbeat/claim）可达 |
| **前置条件** | 四进程启动 |
| **操作步骤** | 1. 测试 /health 2. 测试 POST /api/runtime/register 3. 测试 POST /api/runtime/heartbeat |
| **预期结果** | Worker API 端点可达 |
| **验证命令** | `curl -s http://127.0.0.1:38082/health && curl -s -X POST http://127.0.0.1:38082/api/runtime/register -H "Content-Type: application/json" -d '{}'` |

#### TC-4.5：Insight 进程路由可达验证（新增）

| 项 | 内容 |
|----|------|
| **用例编号** | TC-4.5 |
| **验证目标** | Insight 进程 5 个路由（dashboard/observability/search/latency/health）可达 |
| **前置条件** | 四进程启动 |
| **操作步骤** | 1. 测试 /health 2. 测试 GET /api/dashboard/ops/overview 3. 测试 GET /api/obs/rules 4. 测试 GET /api/search?q=test 5. 测试 POST /api/latency/report |
| **预期结果** | Insight 路由全部可达，返回正确响应 |
| **验证命令** | `curl -s http://127.0.0.1:38083/health && curl -s http://127.0.0.1:38083/api/dashboard/ops/overview && curl -s "http://127.0.0.1:38083/api/search?q=test"` |

#### TC-4.6：nginx 反代分流验证（含 :38083）

| 项 | 内容 |
|----|------|
| **用例编号** | TC-4.6 |
| **验证目标** | nginx 按路径分流到四个进程 |
| **前置条件** | 四进程启动 + nginx 配置完成 |
| **操作步骤** | 1. 通过 nginx 访问 /api/runtime/ → 确认到达 Worker 2. 通过 nginx 访问 /webhook/ → 确认到达 Event 3. 通过 nginx 访问 /api/obs/ → 确认到达 Insight 4. 通过 nginx 访问 /api/dashboard/ → 确认到达 Insight 5. 通过 nginx 访问 / → 确认到达 Mgmt |
| **预期结果** | 路径分流正确 |
| **验证命令** | `curl -s http://127.0.0.1/api/health && curl -s http://127.0.0.1/api/obs/rules && curl -s http://127.0.0.1/api/dashboard/ops/overview` |

#### TC-4.7：定时任务按进程分配验证（含 Insight 定时任务）

| 项 | 内容 |
|----|------|
| **用例编号** | TC-4.7 |
| **验证目标** | 定时任务正确分配到四进程 |
| **前置条件** | 四进程启动 60s+ |
| **操作步骤** | 1. 检查 Mgmt 进程日志确认 knowledge_sync/active_user_tracker 运行 2. 检查 Event 进程日志确认 sweep_events 运行 3. 检查 Worker 进程日志确认 local_claimer/scheduler 运行 4. 检查 Insight 进程日志确认 obs_rollup/obs_alert/server_metrics 运行 |
| **预期结果** | 各进程定时任务按分配表运行 |
| **验证命令** | `journalctl -u grape-server --since "1 min ago" \| grep -i "knowledge\|active_user" && journalctl -u grape-insight-server --since "1 min ago" \| grep -i "obs_rollup\|obs_alert\|server_metrics"` |

#### TC-4.8：单进程宕机不影响其他进程验证

| 项 | 内容 |
|----|------|
| **用例编号** | TC-4.8 |
| **验证目标** | 任一进程宕机后其他进程正常 |
| **前置条件** | 四进程运行中 |
| **操作步骤** | 1. `systemctl stop grape-insight-server` 2. 确认 Mgmt/Event/Worker 健康 3. `systemctl stop grape-event-server` 4. 确认 Mgmt/Worker 健康 5. 重启全部 |
| **预期结果** | 单进程宕机不影响其他 |
| **验证命令** | `systemctl stop grape-insight-server && curl -s http://127.0.0.1:38080/health && curl -s http://127.0.0.1:38081/health && curl -s http://127.0.0.1:38082/health && systemctl start grape-insight-server` |

#### TC-4.9：webhook → 事件入队 → worker 认领 → 执行 → 结果回写 → 用户查看 全链路验证

| 项 | 内容 |
|----|------|
| **用例编号** | TC-4.9 |
| **验证目标** | 四进程协作完成完整事件处理链路 |
| **前置条件** | 四进程运行，有测试仓库 |
| **操作步骤** | 1. 通过 nginx 发送 webhook 到 Event 2. 确认 Event 写入 events 表 + agent_tasks(queued) 3. 等待 3s 确认 Worker 认领(running) 4. 等待执行完成(completed) 5. 确认 events.status=completed 6. 通过 Mgmt 查看 GET /api/events/detail/{seq} |
| **预期结果** | 全链路正常，用户可查看结果 |
| **验证命令** | `curl -X POST https://localhost/webhook/gitcode -H "X-Gitcode-Token: ..." -d '{"...":"..."}' && sleep 10 && sqlite3 data/grape.db "SELECT status FROM rt_events ORDER BY seq DESC LIMIT 1;" && curl -s http://127.0.0.1:38080/api/events/detail/1` |

#### TC-4.10：SSE 任务日志流跨进程验证

| 项 | 内容 |
|----|------|
| **用例编号** | TC-4.10 |
| **验证目标** | Mgmt 进程 SSE 代理 Worker 的 task_messages |
| **前置条件** | 有运行中的任务 |
| **操作步骤** | 1. 通过 Mgmt GET /api/task-logs/{task_id}/stream 2. 确认 SSE 流持续推送 3. 确认消息内容与 task_messages 表一致 |
| **预期结果** | SSE 流正常，跨进程读取 task_messages |
| **验证命令** | `curl -N http://127.0.0.1:38080/api/task-logs/{task_id}/stream --max-time 30` |

#### TC-4.11：distributed_locks 四进程共用验证

| 项 | 内容 |
|----|------|
| **用例编号** | TC-4.11 |
| **验证目标** | 四进程共用 distributed_locks 表无冲突 |
| **前置条件** | 四进程运行 |
| **操作步骤** | 1. 查看 distributed_locks 表 2. 确认不同进程持有不同锁 key 3. 确认无死锁 |
| **预期结果** | 锁机制正常，无冲突 |
| **验证命令** | `sqlite3 data/grape.db "SELECT lock_key, owner, acquired_at FROM rt_distributed_locks ORDER BY acquired_at DESC LIMIT 10;"` |

#### TC-4.12：Insight 进程可观测性面板独立验证（新增）

| 项 | 内容 |
|----|------|
| **用例编号** | TC-4.12 |
| **验证目标** | Insight 进程独立运行可观测性面板，不受 Mgmt 负载影响 |
| **前置条件** | 四进程运行，有历史数据 |
| **操作步骤** | 1. 在 Mgmt 进程制造高负载（并发请求 100 个） 2. 同时访问 Insight 进程 GET /api/obs/rules 3. 确认 Insight 响应时间不受 Mgmt 负载影响 4. 确认 obs_rollup 定时任务正常执行 5. 确认 obs_alert 求值正常 |
| **预期结果** | Insight 响应时间稳定，定时任务不受 Mgmt 负载影响 |
| **验证命令** | `ab -n 100 -c 10 http://127.0.0.1:38080/api/repos/member & curl -s -w "%{time_total}" http://127.0.0.1:38083/api/obs/rules && journalctl -u grape-insight-server --since "5 min ago" | grep -i "obs_rollup\|obs_alert"` |

### Phase 5 验证用例（4 个）

#### TC-5.1：MgmtService 契约接口验证

| 项 | 内容 |
|----|------|
| **用例编号** | TC-5.1 |
| **验证目标** | MgmtService 接口实现类可正确查询 grape_mgmt schema |
| **前置条件** | Phase 5 MgmtService 实现填充完成 |
| **操作步骤** | 1. 测试 get_repo_webhook_config(repo_key) 2. 测试 get_agent_session(repo_key, agent_type) 3. 测试 get_scene_config(repo_key) 4. 测试 verify_session_token(token_hash) |
| **预期结果** | 所有方法返回正确数据 |
| **验证命令** | `python -c "from src.domains.contracts.mgmt_service import MgmtService; svc = MgmtService(); print(svc.get_repo_webhook_config('test'))"` |

#### TC-5.2：RuntimeService 契约接口验证

| 项 | 内容 |
|----|------|
| **用例编号** | TC-5.2 |
| **验证目标** | RuntimeService 接口实现类可正确查询 grape_runtime schema |
| **前置条件** | Phase 5 RuntimeService 实现填充完成 |
| **操作步骤** | 1. 测试 get_tasks(repo_key, {}) 2. 测试 get_events(repo_key, {}) 3. 测试 get_runtimes(org_id) 4. 测试 confirm_issue_done(issue_id, user_id) |
| **预期结果** | 所有方法返回正确数据 |
| **验证命令** | `python -c "from src.domains.contracts.runtime_service import RuntimeService; svc = RuntimeService(); print(svc.get_tasks('test', {}))"` |

#### TC-5.3：InsightService 契约接口验证

| 项 | 内容 |
|----|------|
| **用例编号** | TC-5.3 |
| **验证目标** | InsightService 接口实现类可正确查询 grape_insight schema |
| **前置条件** | Phase 5 InsightService 实现填充完成 |
| **操作步骤** | 1. 测试 get_dashboard_overview(repo_key) 2. 测试 get_slo(repo_key) 3. 测试 get_alerts(org_id) 4. 测试 log_obs("test", "info", "msg") 5. 测试 report_latency({...}) |
| **预期结果** | 所有方法返回正确数据 |
| **验证命令** | `python -c "from src.domains.contracts.insight_service import InsightService; svc = InsightService(); svc.log_obs('test','info','test msg'); print('OK')"` |

#### TC-5.4：裸跨 schema SQL 消除验证

| 项 | 内容 |
|----|------|
| **用例编号** | TC-5.4 |
| **验证目标** | 所有跨 schema SQL 调用已收口到契约接口 |
| **前置条件** | Phase 5 完成 |
| **操作步骤** | 1. 全局搜索 Mgmt 进程中的 `grape_runtime.` SQL 2. 全局搜索 Worker 进程中的 `grape_mgmt.` SQL（除 contracts/） 3. 确认无裸跨 schema SQL |
| **预期结果** | 跨 schema SQL 全部通过契约接口 |
| **验证命令** | `grep -rn "grape_runtime\." src/domains/mgmt/ --include="*.py" | grep -v contracts/ && grep -rn "grape_mgmt\." src/domains/runtime/ --include="*.py" | grep -v contracts/` |

### Phase 6 验证用例（5 个）

#### TC-6.1：单机模式（SQLite + 表前缀）验证

| 项 | 内容 |
|----|------|
| **用例编号** | TC-6.1 |
| **验证目标** | 单机模式四进程同主机运行，SQLite + 表前缀正常 |
| **前置条件** | Phase 6 单机模式部署 |
| **操作步骤** | 1. 设置 GRAPE_MODE=single, DEVOPS_DB_BACKEND=sqlite 2. 启动四进程 3. 发送 webhook 4. 确认全链路 5. 确认无 `database is locked` 错误 |
| **预期结果** | 单机模式正常运行 |
| **验证命令** | `GRAPE_MODE=single DEVOPS_DB_BACKEND=sqlite ./scripts/dev_four_process.sh & sleep 10 && curl -s http://127.0.0.1:38080/health && journalctl -u grape-* --since "5 min ago" | grep -i "locked"` |

#### TC-6.2：集群模式（MySQL + 三 schema）验证

| 项 | 内容 |
|----|------|
| **用例编号** | TC-6.2 |
| **验证目标** | 集群模式 MySQL 三 schema 正常 |
| **前置条件** | MySQL 实例可用，三 schema 已创建 |
| **操作步骤** | 1. 设置 GRAPE_MODE=cluster, DEVOPS_DB_BACKEND=mysql 2. 启动四进程 3. 发送 webhook 4. 确认全链路 5. 确认跨 schema 查询正常 |
| **预期结果** | 集群模式正常运行 |
| **验证命令** | `GRAPE_MODE=cluster DEVOPS_DB_BACKEND=mysql ./scripts/dev_four_process.sh & sleep 10 && curl -s http://127.0.0.1:38080/health && mysql -e "SELECT COUNT(*) FROM grape_runtime.events;"` |

#### TC-6.3：Docker Compose 验证

| 项 | 内容 |
|----|------|
| **用例编号** | TC-6.3 |
| **验证目标** | Docker Compose 四容器 + nginx 正常 |
| **前置条件** | Docker + docker-compose 可用 |
| **操作步骤** | 1. `docker-compose -f deploy/docker-compose.yml up -d` 2. 等待启动 3. 通过 nginx 健康检查四进程 4. 发送 webhook 验证 |
| **预期结果** | 四容器 + nginx 正常运行 |
| **验证命令** | `docker-compose -f deploy/docker-compose.yml up -d && sleep 30 && docker-compose ps && curl -s https://localhost/api/health` |

#### TC-6.4：systemd 服务管理验证

| 项 | 内容 |
|----|------|
| **用例编号** | TC-6.4 |
| **验证目标** | 四个 systemd service 可独立管理 |
| **前置条件** | systemd service 文件已安装 |
| **操作步骤** | 1. `systemctl start grape-server grape-event-server grape-worker-server grape-insight-server` 2. `systemctl is-active grape-*` 3. `systemctl restart grape-insight-server` 4. 确认其他进程不受影响 |
| **预期结果** | systemd 管理正常 |
| **验证命令** | `systemctl start grape-server grape-event-server grape-worker-server grape-insight-server && systemctl is-active grape-server grape-event-server grape-worker-server grape-insight-server` |

#### TC-6.5：全量回归测试验证

| 项 | 内容 |
|----|------|
| **用例编号** | TC-6.5 |
| **验证目标** | Phase 0-6 全部完成后全量回归 |
| **前置条件** | 所有 Phase 完成 |
| **操作步骤** | 1. 运行全量 pytest 2. 运行功能验证清单（15 项） 3. 运行性能验证（5 项） 4. 运行可靠性验证（7 项） |
| **预期结果** | 全量通过 |
| **验证命令** | `pytest tests/ -v 2>&1 | tail -20 && ./scripts/verify_all.sh` |

---

## 附录：文件变更清单

| 文件 | 操作 | 说明 |
|------|------|------|
| `src/domains/runtime/engine/direct_executor.py` | **新建** | 从 event_pipeline.py 迁移 _execute_agent 系列 |
| `src/api/grape_server.py` | **新建** | Mgmt 进程入口 |
| `src/api/grape_event_server.py` | **新建** | Event 进程入口 |
| `src/api/grape_worker_server.py` | **新建** | Worker 进程入口 |
| `src/api/grape_insight_server.py` | **新建** | Insight 进程入口（新增） |
| `src/api/routes/runtime_view.py` | **新建** | runtime.py GET 端点（Mgmt 加载） |
| `src/kernel/db.py` | **新建** | schema 前缀管理 |
| `src/domains/contracts/mgmt_service.py` | **新建** | MgmtService 接口 |
| `src/domains/contracts/runtime_service.py` | **新建** | RuntimeService 接口 |
| `src/domains/contracts/insight_service.py` | **新建** | InsightService 接口 |
| `src/domains/runtime/pipeline/event_pipeline.py` | **修改** | 删除 _execute_agent 系列；direct 路径改用 enqueue_task |
| `src/domains/runtime/engine/local_claimer.py` | **修改** | import 改为 from direct_executor |
| `src/domains/runtime/engine/cleanup.py` | **修改** | 新增 sweep_events_loop + sweep_tasks_loop |
| `src/api/main.py` | **保留** | 向后兼容（单进程模式仍可用） |
| `deploy/nginx-grape.conf` | **修改** | 四端口反代配置（新增 :38083） |
| `deploy/systemd/grape-server.service` | **新建** | Mgmt systemd |
| `deploy/systemd/grape-event-server.service` | **新建** | Event systemd |
| `deploy/systemd/grape-worker-server.service` | **新建** | Worker systemd |
| `deploy/systemd/grape-insight-server.service` | **新建** | Insight systemd（新增） |
| `deploy/docker-compose.yml` | **新建** | 四容器 + nginx |
| `pyproject.toml` | **新建** | 统一依赖 |
| `Makefile` | **新建** | 统一构建入口 |
| `scripts/check-arch.py` | **新建** | 架构约束检查 |
| `scripts/dev_four_process.sh` | **新建** | 开发模式四进程启动 |
| `configs/profiles/single.conf` | **新建** | 单机模式 profile |
| `configs/profiles/cluster.conf` | **新建** | 集群模式 profile |

---

## 附录：定时任务完整分配表

| 定时任务 | 频率 | 归属进程 | 域 | 模块 |
|---------|------|---------|-----|------|
| knowledge_sync_loop | 每小时 | Mgmt | mgmt | src.api.knowledge |
| knowledge_write_worker | 持续 | Mgmt | mgmt | src.api.knowledge |
| skill_sync_scheduler | 30min | Mgmt | mgmt | src.storage.skill_sync |
| feedback_reconcile_scheduler | 定时 | Mgmt | mgmt | src.api.feedback_issue_converter |
| upload_cleanup_scheduler | 每小时 | Mgmt | mgmt | src.storage.upload_cleanup |
| cooldown_scheduler | 每天 | Mgmt | mgmt | src.admin.cooldown |
| active_user_tracker | 30s | Mgmt | mgmt | 自定义 |
| lease_sweep_loop | 循环 | Mgmt | mgmt | bot_manager |
| knowledge_migrate | 一次性 | Mgmt | mgmt | src.api.db_setup |
| D32 migration | 一次性 | Mgmt | mgmt | scripts.migrate_user_accounts_to_grape |
| sweep_events_loop | 15min | Event | runtime | src.runtime.cleanup |
| bot_token_health | 5min | Event | runtime | src.runtime.bot_token_health |
| local_claimer | 3s | Worker | runtime | src.runtime.local_claimer |
| scheduler/autopilot | 60s | Worker | runtime | src.runtime.scheduler |
| health_refresh | 5min | Worker | runtime | src.runtime.health_scorer |
| local_probe | 10min | Worker | runtime | src.runtime.local_probe |
| reap_stale | 60s | Worker | runtime | src.runtime.service |
| sweep_tasks_loop | 15min | Worker | runtime | src.runtime.cleanup |
| table_maintenance (agent_tasks) | 6h | Worker | runtime | cleanup_old_agent_tasks |
| **obs_rollup_loop** | **60s** | **Insight** | **insight** | **src.observability.metric_rollup** |
| **obs_sample_loop** | **60s** | **Insight** | **insight** | **src.observability.rollup** |
| **obs_alert_loop** | **30s** | **Insight** | **insight** | **src.observability.alerts** |
| **server_metrics_loop** | **15s** | **Insight** | **insight** | **自定义（psutil）** |
| **install_master_logging** | **持续** | **Insight** | **insight** | **src.observability.logs** |
| **table_maintenance (task_messages)** | **6h** | **Insight** | **insight** | **cleanup_old_task_messages** |

> **加粗项**为本方案的关键变更：从 Mgmt 进程迁移到 Insight 独立进程。

#### 7.8.13 多实例负载均衡（2026-11 增补）

##### 背景

命名空间（§7.8.12）解决了多机注册行互相覆盖的问题，但 `discover_service()` 仍是
`SELECT ... WHERE service_name=?` + `fetchone()`，一次只能拿一条；同机多副本
（未来多 worker）或跨机同名服务都不会被调度。本次在客户端补上实例级调度。

##### 数据侧

`registry.list_instances(service)` 一次取出 `service` 与 `service@namespace` 两类键的全部行，
补充 `service` / `instance` / `age_seconds` / `alive` 字段；跳过 `status!='online'`
或心跳超过 90s 的行（`include_offline=True` 可供运维排查）。

##### 客户端策略

`GRAPE_GSB_LB`（默认 `local_first`）：

| 策略 | 行为 |
|------|------|
| `local_first` | 本机命名空间实例优先，同命名空间内轮询；失败后跨节点兜底（保持旧行为语义） |
| `round_robin` | 忽略本机偏好，跨全部实例轮询 |
| `random` | 随机顺序 |
| `least_inflight` | 选在途请求最少的实例（客户端本地计数） |

配套：

- `GRAPE_GSB_NAMESPACE`：本机命名空间，用于 local_first 判定；
- `GRAPE_GSB_LB_COOLDOWN`（默认 30s）：实例传输失败后短期剔除，避免连续打坏实例；
- `GRAPE_GSB_LB_CACHE_TTL`（默认 5s）：实例列表缓存，避免每次调用查库；
- 环境变量 `GRAPE_{MGMT,RUNTIME,INSIGHT,EVENT}_URL` 仍为最高优先级（静态钉住单个目标，
  便于排障与灰度），此时负载均衡只作用于后续兜底候选。

##### 安全语义不变

- 只有传输层错误（连接失败/超时/协议错误）才切换候选；
- 非幂等请求（POST/PUT/DELETE）只在"连接期失败"时切换——读超时后不重放，
  避免 `update_issue_state`、`log_obs` 等写操作重复执行。

##### 可观测

`grape_gsb_client_requests_total` / `grape_gsb_client_duration_seconds` 增加 `instance`
维度，可直接看各实例的 QPS、错误率与延迟分布。
