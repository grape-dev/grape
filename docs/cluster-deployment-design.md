# Grape 集群部署与健康监控方案

## 背景

grape 当前是单实例部署，缺少健康监控、任务取消实时性、集群扩展能力。
参照 multica-ai/multica 的架构设计，结合 grape 现有技术栈（Python + MySQL + OBS），制定分阶段演进方案。

## grape 当前状态

**已有：**
- 单实例 Backend（uvicorn 3000 主服务 + 8080 webhook）
- 外部 MySQL RDS（127.0.0.1）
- 外部 OBS 对象存储（obs-grape-dev-file）
- Worker 独立进程（天然分布式）
- nginx 反代

**缺少：**
- 健康监控（无健康端点 / Dashboard / 告警）
- 任务取消实时性（看门狗 60 秒轮询，延迟大）
- Backend 多实例支持
- 容器化部署（Docker / Helm）

## multica 参考架构

### multica 的 4 层可观测性

| 层 | 实现 | 依赖 | grape 可直接用 |
|---|------|------|:---:|
| Dashboard API | 6 个 GET 端点查数据库聚合 | 只依赖数据库 | ✅ |
| 健康端点 | /health/realtime 实时状态 | Redis（可选） | ✅（去掉 Redis） |
| Prometheus 指标 | /metrics + Counter/Gauge/Histogram | Prometheus Server | ❌ 需加组件 |
| 失败监控 | 定时扫描失败任务 | 只依赖数据库 | ✅ |

### multica 的通信架构

- **WebSocket（主通道）**：服务端推送任务唤醒 + RPC（claim/取消）+ 实时通知
- **HTTP（兜底）**：WebSocket 不可用时回退 HTTP
- **Redis Stream（集群）**：多实例间 WebSocket 消息转发（8 分片）
- **Redis ZSET**：任务回收精确调度
- **Redis 缓存**：runtime/skill/model 数据缓存

### multica 集群部署方式

| 阶段 | 部署方式 | 组件 |
|------|---------|------|
| 单机 | Docker Compose | PostgreSQL + Backend + Frontend |
| 小集群 | Helm Chart replicas=1 | 外部 PG + 多 Worker |
| 多实例 | Helm Chart replicas>1 | 外部 PG + Redis + S3 + 多 Worker |
| 大规模 | Helm + Prometheus | PG 集群 + Redis 集群 + Prometheus |

### multica 集群关键设计

- **数据库迁移锁**：PostgreSQL advisory lock，多实例排队迁移
- **任务防重复 claim**：UPDATE WHERE status='queued' 行锁
- **防双 claim**：WebSocket 断连不确定时不回退 HTTP（errWSRPCUncertain）
- **Worker GC**：30 秒扫描 + 心跳超时标记 offline + 7 天清理
- **Redis 可选**：单实例不用 Redis，集群时加

## grape 分阶段方案

### 阶段 1：单机增强（零新依赖）

**目标：** 加健康监控 + 任务取消实时性 + 前端 Dashboard

#### 1.1 健康端点（分两层）

**调用场景：**

| 场景 | 频率 | 需要鉴权 |
|------|------|:---:|
| 前端系统监控页面 | 30 秒一次 | 需要（用户已登录） |
| K8s livenessProbe/readinessProbe（阶段 4） | 10 秒一次 | 不需要（探针不带 token） |
| nginx 健康检查 | 定期 | 不需要 |
| 外部监控工具（Prometheus/Uptime Robot） | 定期 | 不需要 |
| 运维手动 curl | 排查时 | 不需要 |

**冲突：** 免鉴权会暴露 Worker 数量等系统规模信息；需要鉴权则 K8s 探针/监控工具配置复杂（token 过期探针失败 → K8s 不断重启 pod）。

**方案：分两层端点**

| 端点 | 鉴权 | 返回内容 | 用途 |
|------|:---:|---------|------|
| GET /api/health | 免鉴权 | `{"server":"ok"}` | K8s 探针 / nginx / 监控工具 / 运维 curl |
| GET /api/health/detail | 需要登录 | 完整状态（DB/OBS/Worker 数量/槽位） | 前端 Dashboard |

**GET /api/health（免鉴权）：**
```json
{"server": "ok"}
```
- 不查数据库、不含凭据/版本/IP/路径
- 比 grape 已有的 `GET /`（返回完整 HTML）暴露的信息更少
- 不增加额外风险（攻击者能访问 /api/health 就能访问 /）
- DDoS 影响小（只返回 JSON 字符串）

**GET /api/health/detail（需登录）：**
```json
{
  "server": "ok",
  "database": "ok",
  "obs": { "enabled": true, "reachable": true },
  "workers": { "total": 3, "online": 2, "offline": 1, "busy_slots": 5, "max_concurrent": 12 },
  "timestamp": "2026-08-25T12:00:00Z"
}
```

| 网元 | 检查方式 |
|------|---------|
| 服务端 | 200 OK |
| 数据库 | SELECT 1 |
| OBS | OBS_ENABLED + get_storage() != None |
| Worker | 查 agent_runtimes 表在线/离线数 |

**改动文件：** 新增 src/api/routes/health.py（2 个端点）

#### 1.2 Dashboard API（5 个聚合端点，需登录 + 用户隔离）

对标 multica 的 6 个 Dashboard 端点，直接查 events/agent_tasks 表聚合。

**鉴权：** 所有 Dashboard API 都需要登录（`Depends(get_current_user)`）。

**用户隔离：** ?repo_key= 过滤时只返回用户有权限的仓库统计：
- 用户管理的仓库（user_repos 表 user_id=当前用户）
- 不传 repo_key 时返回全部仓库统计（已登录用户可见全局统计）

| 端点 | SQL | 说明 |
|------|-----|------|
| GET /api/dashboard/runtime/status | SELECT COUNT(*), SUM(status='online') FROM agent_runtimes | Worker 在线/离线/槽位 |
| GET /api/dashboard/task/daily | SELECT DATE(created_at), agent_type, COUNT(*), SUM(status='completed'), SUM(status='failed'), AVG(duration) FROM events GROUP BY DATE(created_at), agent_type | 每日任务统计 |
| GET /api/dashboard/task/by-agent | SELECT agent_type, COUNT(*), SUM(status='completed'), SUM(status='failed'), AVG(duration) FROM events GROUP BY agent_type | 按 agent 统计 |
| GET /api/dashboard/failures/daily | SELECT DATE(created_at), exit_code, COUNT(*) FROM events WHERE status='failed' GROUP BY DATE(created_at), exit_code | 每日失败 |
| GET /api/dashboard/failures/by-agent | SELECT agent_type, exit_code, COUNT(*) FROM events WHERE status='failed' GROUP BY agent_type, exit_code | 按 agent 失败 |

参数：`?days=N`（默认 7，最大 30）+ `?repo_key=`（可选仓库过滤，只返回用户有权限的仓库）

**改动文件：** 新增 src/api/routes/dashboard.py

#### 1.3 任务取消实时性（方案 1+2 三层保险）

**问题：** 用户点终止后 worker 最长 60 秒才发现（看门狗轮询）。

**方案：** 复用已有 HTTP 通道，响应里带取消信号：

| 层级 | 复用通道 | 延迟 | 场景 |
|------|---------|------|------|
| 第 1 层 | report_messages 响应带 task_status | 1-5 秒 | 正常情况（每 step 上报） |
| 第 2 层 | heartbeat 响应带取消任务列表 | 15 秒 | step 很长时（pip install / 大文件） |
| 第 3 层 | 看门狗（已实现） | 60 秒 | 网络异常时兜底 |

**改动 1：report_messages 响应加 task_status**
- 服务端 runtime.py task_messages 端点：响应里加 `task_status` 字段
- worker agent.py report_messages 调用后检查 task_status
- 发现 cancelled → 调 _kill_stale_engine 杀进程

**改动 2：heartbeat 响应带取消任务列表**
- 服务端 runtime.py heartbeat 端点：查该 worker 的 active_tasks 有没有 cancelled
- 响应里加 `cancelled_tasks: ["task_id1", "task_id2"]`
- worker agent.py heartbeat 调用后检查列表
- 发现取消 → 杀进程

**改动文件：**
- src/api/routes/runtime.py（report_messages + heartbeat 响应）
- src/runtime/agent.py（检查响应里的取消信号 + 杀进程）
- src/runtime/client.py（report_messages + heartbeat 返回值解析）

#### 1.4 前端系统监控页面

当前已有 nav-dash（仓库级 Dashboard——某个仓库的事件/任务统计）。
新增 nav-health（系统级 System Health——跨仓库的网元健康/Worker 状态/全局任务统计）。

**不改动已有的 nav-dash。**

| 模块 | 内容 |
|------|------|
| 系统健康概览 | 服务端/DB/OBS/Worker 状态灯（绿/红） |
| 任务统计 | 成功率/失败率/平均时长图表（按天） |
| Worker 列表 | 在线/离线/槽位利用率 |
| 失败分析 | 按 agent/exit_code 分类的失败统计 |

**改动文件：**
- src/api/static/js/pages/health.js（新增——系统监控前端页面）
- src/api/static/index.html（新增导航项"系统监控" nav-health，不改动已有 nav-dash）
- src/api/static/js/app.js（新增 nav('health') 分支——调 /api/health + /api/dashboard/* + renderHealth）
- src/api/static/css/apple-theme.css（系统监控页面样式）

**导航结构（改动后）：**
```
Workspace
  ■ Dashboard（已有——仓库级）
  ▶ Pipeline（已有）

DevOps Agents
  ● VOD Agent（已有）
  ● Issue Agent（已有）
  ● Coding Agent（已有）
  ● QA Agent（已有）
  ● SRE Agent（已有）

Knowledge
  ◈ DevOps Skills（已有）
  ❖ Project Knowledge（已有）

System（新增分组）
  ⚕ System Health（新增——系统级健康监控）
```

#### 1.5 改动汇总

**新增文件（4 个）：**

| # | 文件 | 内容 |
|---|------|------|
| 1 | src/api/routes/health.py | GET /api/health——服务端+DB+OBS+Worker 健康状态 |
| 2 | src/api/routes/dashboard.py | 5 个聚合端点（runtime/status + task/daily + task/by-agent + failures/daily + failures/by-agent） |
| 3 | src/api/static/js/pages/health.js | 系统监控前端页面（健康概览 + 任务统计 + Worker 列表 + 失败分析） |
| 4 | src/api/routes/main.py 注册路由（或 src/api/main.py） | 注册 health + dashboard 路由 |

**修改文件（5 个）：**

| # | 文件 | 改动位置 | 改动内容 |
|---|------|---------|---------|
| 5 | src/api/routes/runtime.py | 第 311 行 runtime_task_messages | 响应里加 task_status 字段（查数据库当前任务状态） |
| 6 | src/api/routes/runtime.py | 第 172 行 runtime_heartbeat | 响应里加 cancelled_tasks 列表（查该 worker 的 active_tasks 有没有 cancelled） |
| 7 | src/runtime/client.py | 第 127 行 report_messages | 返回值解析 task_status 字段 |
| 8 | src/runtime/client.py | 第 104 行 heartbeat | 返回值解析 cancelled_tasks 字段 |
| 9 | src/runtime/agent.py | 第 506 行 report_messages 调用后 | 检查 task_status，发现 cancelled → 调 _kill_stale_engine 杀进程 |
| 10 | src/runtime/agent.py | heartbeat 调用后（第 544 行 _run_loop 内） | 检查 cancelled_tasks 列表，发现取消 → 杀进程 |
| 11 | src/api/static/index.html | 导航栏 | 新增"System"分组 + "系统监控"导航项 nav-health |
| 12 | src/api/static/js/app.js | nav() 函数 | 新增 nav('health') 分支——调 /api/health + /api/dashboard/* + renderHealth |
| 13 | src/api/static/css/apple-theme.css | 样式 | 系统监控页面样式（状态灯/图表/Worker 列表） |

**不改动：**
- src/runtime/agent.py 第 718 行看门狗——保留作为第 3 层兜底
- src/api/routes/runtime.py cancel_task 接口——不改（只改 worker 感知方式）
- 数据库——不加表/字段（查已有 events/agent_tasks/agent_runtimes 表）
- 已有 nav-dash（仓库级 Dashboard）——不改动

**零新依赖。**

---

### 阶段 2：容器化部署（Docker）

**目标：** Docker 化，支持快速部署和迁移

#### 2.1 Dockerfile（Backend）

```dockerfile
FROM python:3.14-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt
COPY . .
EXPOSE 3000 8080
CMD ["uvicorn", "src.api.main:app", "--host", "0.0.0.0", "--port", "3000"]
```

#### 2.2 docker-compose.yml

```yaml
services:
  backend:
    build: .
    ports:
      - "127.0.0.1:3000:3000"
      - "127.0.0.1:8080:8080"
    environment:
      - OBS_ENABLED=true
      - OBS_BUCKET=obs-grape-dev-file
      # AK/SK 通过 .env 注入
    depends_on:
      - nginx
  nginx:
    image: nginx:alpine
    ports:
      - "80:80"
    volumes:
      - ./nginx.conf:/etc/nginx/nginx.conf
```

#### 2.3 数据库迁移锁

db_setup.py 加 MySQL GET_LOCK 防多实例同时迁移：

```python
# 获取迁移锁（多实例排队）
cursor.execute("SELECT GET_LOCK('grape_migrate', 300)")
lock_acquired = cursor.fetchone()[0]
if lock_acquired:
    try:
        run_migrations()
    finally:
        cursor.execute("SELECT RELEASE_LOCK('grape_migrate')")
```

**改动文件：**
- 新增 Dockerfile
- 新增 docker-compose.yml
- src/api/db_setup.py 加迁移锁

---

### 阶段 3：多实例集群（加 Redis）

**目标：** Backend 水平扩展，支持高可用

#### 3.1 引入 Redis

| 用途 | 说明 |
|------|------|
| 消息总线 | cancel 通知跨实例转发（Redis Stream） |
| 任务回收 | ZSET 精确调度（替代 60 秒看门狗轮询） |
| 缓存 | runtime/skill/model 数据缓存 |

#### 3.2 WebSocket 端点（可选）

- 服务端加 WebSocket 端点（FastAPI WebSocket）
- worker 连 WebSocket 接收实时通知（cancel/wakeup）
- HTTP 作为兜底（WebSocket 断了回退 HTTP）

#### 3.3 Backend replicas>1

```nginx
upstream grape_backend {
  server 127.0.0.1:3001;
  server 127.0.0.1:3002;
  server 127.0.0.1:3003;
}
```

- nginx 负载均衡多个 backend 实例
- 共享 MySQL + Redis + OBS
- 不需要 PVC（用 OBS 替代本地存储）

#### 3.4 集群兼容保证

| 操作 | 怎么保证 | 已有？ |
|------|---------|:---:|
| 任务不重复 claim | MySQL 行锁 UPDATE WHERE status='queued' | ✅ |
| 任务不重复完成 | UPDATE WHERE status='running' 幂等 | ✅ |
| 取消通知跨实例 | Redis Stream 转发 | 阶段 3 加 |
| webhook 不重复 | nginx 随机打一个实例 | ✅ |
| 心跳不冲突 | UPDATE last_seen_at 幂等 | ✅ |
| 迁移不冲突 | MySQL GET_LOCK | 阶段 2 加 |

**改动文件：**
- 新增 Redis 依赖
- src/api/routes/ 加 WebSocket 端点
- src/runtime/agent.py 加 WebSocket 客户端
- docker-compose.yml 加 Redis
- nginx.conf 加 upstream

---

### 阶段 4：K8s 集群（Helm）

**目标：** 生产级 K8s 部署

#### 4.1 Helm Chart

```
deploy/helm/grape/
├── Chart.yaml
├── values.yaml
└── templates/
    ├── backend.yaml       # Deployment（replicas 可配）
    ├── configmap.yaml     # 非敏感配置
    ├── ingress.yaml       # TLS + 路由
    └── prometheusrule.yaml # 告警规则
```

#### 4.2 健康检查

```yaml
livenessProbe:
  httpGet:
    path: /api/health
    port: 3000
readinessProbe:
  httpGet:
    path: /api/health
    port: 3000
```

#### 4.3 Prometheus 指标

- prometheus_client Python SDK
- /metrics 端点
- 任务/Worker/HTTP 指标（Counter/Gauge/Histogram）
- PrometheusRule 告警规则

#### 4.4 部署拓扑

```
                    ┌─────────────┐
                    │   Ingress    │
                    │  (nginx/TLS) │
                    └──────┬───────┘
                           │
            ┌──────────────┼──────────────┐
            │              │              │
     ┌──────▼──────┐ ┌────▼─────┐ ┌──────▼──────┐
     │ Backend A   │ │ Backend B │ │ Backend C   │
     │ (uvicorn)   │ │ (uvicorn) │ │ (uvicorn)   │
     └──────┬──────┘ └────┬─────┘ └──────┬──────┘
            │              │              │
            ┌──────────────┼──────────────┐
                           │
              ┌────────────┼────────────┐
              │            │            │
        ┌─────▼─────┐ ┌───▼────┐ ┌────▼─────┐
        │ MySQL RDS │ │ Redis  │ │   OBS    │
        │ (共享)     │ │ (共享) │ │ (共享)   │
        └───────────┘ └────────┘ └──────────┘
```

**改动文件：**
- 新增 deploy/helm/
- 新增 prometheus_client 依赖
- src/api/routes/metrics.py（/metrics 端点）

---

## 优先级

| 阶段 | 优先级 | 新依赖 | 价值 |
|------|--------|--------|------|
| 阶段 1 | 立即做 | 无 | 健康监控 + 取消实时性 |
| 阶段 2 | 近期做 | 无 | 容器化方便部署 |
| 阶段 3 | 规模上来做 | Redis | 水平扩展 |
| 阶段 4 | K8s 基础设施有了做 | Prometheus | 生产级可观测性 |

## grape 已有优势（不用做）

- 外部 MySQL RDS——不用像 multica 内置 PostgreSQL
- 外部 OBS——不用像 multica 搞 PVC/S3
- Worker 天然分布式——不用改

## 已知限制演进

| 限制 | 阶段 1 | 阶段 3 | 阶段 4 |
|------|--------|--------|--------|
| 取消延迟 | 1-5 秒 | 0 秒（WebSocket） | 0 秒 |
| 看门狗 | 60 秒轮询 | Redis ZSET | Redis ZSET |
| 缓存 | 无 | Redis 缓存 | Redis 缓存 |
| 指标 | 数据库聚合 | 数据库聚合 | Prometheus |
| 告警 | 无 | 无 | PrometheusRule + 飞书 |
