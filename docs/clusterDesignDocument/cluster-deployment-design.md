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

**两种部署方式共存：** 容器化部署和老方式部署都支持，共用同一套代码。

| 部署方式 | 启动命令 | 端口 | 适用场景 |
|---------|---------|------|---------|
| 老方式（非容器） | `python src/api/run.py` | 3000+8080 双进程 | 开发/测试/快速部署 |
| 新方式（容器） | `docker compose up -d` | 3000 单进程 | 生产/标准化部署 |

**老方式不受影响：** run.py 没改，迁移锁单实例无竞争，nginx 不容器化。

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

### 阶段 1：单机增强（零新依赖）——✅ 已完成+验证

**目标：** 任务取消实时性 + Worker repo_key 字段

**注意：** System Health 监控页面已去掉——监控是独立展示层，不影响集群和 Worker 运行。仅保留 GET /api/health 免鉴权端点（Docker healthcheck / K8s livenessProbe 用）。

#### 1.1 健康端点（免鉴权——Docker/K8s 用）

GET /api/health（免鉴权）：返回 `{"server":"ok"}`
- 供 Docker healthcheck / K8s livenessProbe / nginx 健康检查使用
- 不查数据库、不暴露系统规模

**改动文件：** src/api/routes/health.py（1 个端点）

#### 1.2 任务取消实时性（方案 1+2 三层保险）

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

#### 1.3 Worker repo_key 字段

Worker 通过 credential_id → runtime_credentials.scope + owner_id 确定归属范围。

数据库改动：
1. runtime_credentials 表加 `repo_key VARCHAR(191) DEFAULT NULL`
2. agent_runtimes 表加 `repo_key VARCHAR(191) DEFAULT NULL`

字段使用规则：
- scope=global：repo_key=NULL
- scope=admin：repo_key=NULL
- scope=repo：repo_key=项目 repo_key
- scope=user：repo_key=创建凭据时指定的项目 repo_key

改动点：
1. 前端 runtimes.js：scope=user 也传 repo_key
2. settings.py create_runtime_credential：scope=user 时也存 repo_key
3. runtime.py runtime_register：Worker 注册时从凭据复制 repo_key 到 agent_runtimes
4. db_setup.py：DDL + migration 加 repo_key 字段

现有逻辑不感知：claim/heartbeat/任务分配/cancel 不读 repo_key，老数据留 NULL。

#### 1.4 改动汇总

**新增文件：**
- src/api/routes/health.py——GET /api/health 免鉴权端点

**修改文件：**

| # | 文件 | 改动内容 |
|---|------|---------|
| 1 | src/api/routes/runtime.py | report_messages 响应加 task_status + heartbeat 响应加 cancelled_tasks |
| 2 | src/runtime/client.py | heartbeat 返回值从 bool 改为 dict + report_messages 解析 task_status |
| 3 | src/runtime/agent.py | heartbeat 检查 cancelled_tasks + report_messages 检查 task_status + 杀进程 |
| 4 | src/api/routes/settings.py | create_runtime_credential scope=user 时也存 repo_key |
| 5 | src/api/routes/runtime.py | runtime_register 从凭据复制 repo_key 到 agent_runtimes |
| 6 | src/api/db_setup.py | runtime_credentials + agent_runtimes DDL + migration 加 repo_key |
| 7 | src/api/static/js/pages/runtimes.js | scope=user 也传 repo_key |

**已去掉（不在本次需求范围）：**
- ~~System Health 监控页面~~（health.js / nav('health') / System 导航菜单）
- ~~Dashboard 系统级聚合端点~~（runtime/status / task/daily / task/by-agent / failures/*）
- ~~health/detail 端点~~
- dashboard.py 恢复 dev 分支原有仓库级路由

**不改动：**
- src/runtime/agent.py 第 718 行看门狗——保留作为第 3 层兜底
- src/api/routes/runtime.py cancel_task 接口——不改（只改 worker 感知方式）
- 数据库——不加表/字段（查已有 events/agent_tasks/agent_runtimes 表）
- 已有 nav-dash（仓库级 Dashboard）——不改动

**零新依赖。**

---

### 阶段 2：容器化部署（Docker）——✅ 已完成+验证

**目标：** Docker 化 backend，支持快速部署和迁移。nginx 不容器化——保留宿主机。

详细方案见 `docs/phase2-detailed-design.md`。

#### 2.1 Dockerfile

```dockerfile
FROM python:3.14-slim
WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends gcc g++ && rm -rf /var/lib/apt/lists/*
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
ENV TZ=Asia/Shanghai
RUN ln -snf /usr/share/zoneinfo/$TZ /etc/localtime && echo $TZ > /etc/timezone
EXPOSE 3000
CMD ["uvicorn", "src.api.main:app", "--host", "0.0.0.0", "--port", "3000"]
```

- 只启动 3000——main:app 已注册所有路由（Web 页面 + API + runtime + webhook + A2A）
- 不启动 8080——run_webhook:app 是冗余的，main:app 已有 webhook_router
- hcloud CLI 不装——backend 容器不执行 hcloud，由 worker 在宿主机执行

#### 2.2 .dockerignore

排除 .git/.venv/data/logs/__pycache__/docs/skills/tools 等不需要的目录。
configs/ 整个目录通过 docker-compose volume 挂载——不打进镜像（运行时从宿主机读取）。
保留 configs/*.example.json 作模板打进镜像。

#### 2.3 docker-compose.yml

**只容器化 backend——nginx 保留宿主机。**

```yaml
services:
  backend:
    build: .
    container_name: grape-backend
    ports:
      - "127.0.0.1:3000:3000"   # 宿主机 nginx 反代到容器 3000
    env_file: [.env]
    environment:
      - TZ=Asia/Shanghai
    volumes:
      - grape-data:/app/data
      - ./configs:/app/configs
    restart: unless-stopped
    logging:
      driver: json-file
      options:
        max-size: "50m"
        max-file: "3"
    healthcheck:
      test: ["CMD", "python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:3000/api/health')"]
      interval: 30s
      timeout: 5s
      retries: 3
      start_period: 30s
volumes:
  grape-data:
```

**架构设计：**

```
当前（容器化后）：
  用户 → nginx:80（宿主机）→ 127.0.0.1:3000（容器）→ uvicorn main:app

后续去掉 nginx：
  用户 → ELB 443 → 宿主机 3000（容器）→ uvicorn main:app
```

| 端口 | 绑定 | 服务 | 对外 | 说明 |
|------|------|------|:---:|------|
| 80 | 0.0.0.0 | 宿主机 nginx | ✅ | TLS 终止 + 反代到 3000 |
| 443 | 0.0.0.0 | 宿主机 nginx | ✅ | HTTPS（证书在宿主机 nginx） |
| 3000 | 127.0.0.1 | 容器 backend | ❌ | 宿主机 nginx 反代到容器 |

**为什么 nginx 不容器化：**
- nginx 是基础设施不是 grape 服务——不应该耦合进服务部署
- 宿主机已有 nginx 在跑——再起 nginx 容器 80 端口冲突
- 后续可以去掉 nginx——ELB 直接转发到 backend
- nginx 的作用（TLS 终止/反代/静态文件）ELB 和 FastAPI 都能做

**Worker 连接方式：**

| Worker 位置 | RUNTIME_SERVER_URL | 路径 |
|------------|-------------------|------|
| 本机 | http://localhost:3000 | 宿主机 3000 → 容器 3000 |
| 其他虚机 | http://域名或IP | ELB → nginx:80 → 容器:3000 |

- env_file: .env——敏感信息注入
- healthcheck 用阶段 1 的 /api/health
- restart: unless-stopped
- data/logs 持久化 volume

#### 2.4 .env.example

敏感信息模板（DB/OBS/Server 配置），.env 不提交 git。

#### 2.5 数据库迁移锁（双后端兼容）

db_setup.py init_database() 加迁移锁：
- MySQL：GET_LOCK('grape_migrate', 300) 防多实例同时迁移，finally RELEASE_LOCK
- SQLite：跳过锁（单实例不需要）

**改动文件：**
- 新增 Dockerfile
- 新增 .dockerignore
- 新增 docker-compose.yml
- 新增 .env.example
- src/api/db_setup.py 加迁移锁

**零新依赖。** nginx 不容器化——保留宿主机。Worker 连 localhost:3000 不变。

---

### 阶段 3：多实例集群（JWT 会话共享 + 可选 Redis）——✅ 已完成+验证

**目标：** Backend 水平扩展，支持高可用

**基于本地验证结论：** 多实例核心能力（迁移锁/claim/取消感知/负载均衡/JWT 跨实例/token_version 跨实例撤销）已验证通过——不需要 Redis 也能多实例运行。所有状态在 MySQL 共享数据库，实例间不需要通信。

#### 3.1 会话共享（JWT 无状态——必须做）

**问题：** 当前用户登录会话是进程内内存（`_user_tokens` dict）——多实例时用户在实例 A 登录，请求打到实例 B 会 401。

**这是阶段 3 唯一必须做的改动。**

**multica 的方案：** JWT 无状态会话
- 登录时用 JWT_SECRET 签发 JWT（含 user_id + 过期时间）
- token 存 HttpOnly cookie
- 每次请求用 JWT_SECRET 验证签名——不查数据库、不存内存
- 所有实例配同一个 JWT_SECRET——任何实例都能验证

**grape 的方案：**

| 会话类型 | 当前 | 改后 | 需要改？ |
|---------|------|------|:---:|
| 用户登录 | 进程内内存（_user_tokens） | JWT 无状态 | 改 |
| Worker 认证 | 数据库（runtime_credentials） | 不变 | 不改 |
| Agent 任务会话 | 数据库（agent_tasks.runtime_session_id） | 不变 | 不改 |

改动：
1. `src/api/jwt_utils.py` create_access_token（15分钟）+ create_refresh_token（7天滚动 + 30天绝对上限 max_exp）
2. `src/api/routes/auth.py` login() 返回双 token + POST /auth/refresh 换新 access_token + 滚动续期 refresh_token + OAuth 回调签发双 token + logout token_version+1
3. `src/api/deps.py` get_current_user() 改为 verify_access_token + token_version 校验
4. 新增 `JWT_SECRET` 环境变量（所有实例共享）
5. 新增 `python-jose` 依赖
6. 前端 api.js：401 时自动 refresh + Promise 队列防并发 + 多 Tab storage 同步
7. `src/user/db.py` 新增 get_token_version / increment_token_version
8. `src/api/db_setup.py` user_accounts DDL + migration 加 token_version 字段

**双 token 机制：**

| token | 有效期 | 用途 | 续期策略 | 存储 |
|-------|--------|------|---------|------|
| access_token | 15 分钟 | API 请求 | 过期后用 refresh_token 换新 | localStorage（JS 可读） |
| refresh_token | 7 天滚动 + 30 天绝对上限 | 换新 access_token | 每次换新时滚动续期 7 天，max_exp 30 天不更新 | HttpOnly cookie（JS 不可读） |

**滚动续期 + 绝对上限逻辑：**

refresh_token payload 含两个时间字段：
- `exp`：滚动过期时间（每次 refresh 重新算 now + 7 天）
- `max_exp`：绝对上限（首次签发时 = now + 30 天，refresh 时继承不更新）

verify 时 4 个条件全通过才有效：签名 + exp + max_exp + token_version

效果：
- 用户一直操作 → access_token 15分钟过期 → 自动 refresh → 拿到新 access + 新 refresh（续期 7 天，max_exp 不变）→ 30 天内永不踢出
- 用户 7 天不操作 → refresh_token exp 过期 → 下次操作时 refresh 失败 → 跳登录页
- 用户 30 天后 → max_exp 到达 → 即使 exp 没过期也强制登录

**token_version 撤销机制：**

user_accounts 表有 token_version INT DEFAULT 0 字段。
- 签发 token 时：JWT payload 写入 ver=当前 token_version
- 验证 token 时：对比 payload.ver 和数据库 token_version——不匹配则 401
- logout 时：token_version+1 → 所有旧 token 立即失效（access + refresh）
- 改密码时：token_version+1 → 同上
- 攻击者无法伪造：改 ver 需要重新签名，没有 JWT_SECRET 不能签名

**refresh_token HttpOnly cookie + CSRF 保护：**

- refresh_token 存 HttpOnly cookie（HttpOnly + Secure + SameSite=Lax + Path=/api/auth）
- refresh 端点从 cookie 读 refresh_token（JS 不接触）
- CSRF 保护：X-Requested-With: XMLHttpRequest header（跨站伪造无法加自定义 header）
- 业务接口继续用 Bearer header——不需要 CSRF

**并发 401 Promise 队列：**

- 多个请求同时 401 时，第一个创建 _refreshPromise，其他请求 await 同一个 Promise
- refresh 完成后所有请求用新 token 重发——不丢失请求
- refresh 本身 401 时队列全部请求跳登录页

**多 Tab 同步：**

- 监听 storage 事件——Tab A 刷新 token 后其他 Tab 自动同步
- 定位：体验优化不是安全屏障——兜底有 401 + Promise 队列
- 局限：只同步 access（refresh 在 cookie JS 读不到）

**待优化项（本次一并实施）：**

1. **并发 401 请求丢失**
   - 问题：页面同时发多个请求，access_token 过期时多个请求同时 401，_refreshing 锁导致部分请求直接 reject 不重试
   - 优化：用 Promise 队列——第一个 refresh 时其他请求等待同一个 Promise，refresh 完成后全部重发
   - 边界处理：refresh 本身返回 401（refresh 失效）时队列全部请求直接抛出登录失效跳登录页，不无限等待

2. **OAuth 登录没有 refresh_token**
   - 问题：OAuth 回调只返回 access_token，15 分钟后被踢
   - 优化：OAuth 回调也签发 refresh_token，通过 cookie 传给前端

3. **旧 refresh_token 仍然有效（token_version 机制）**
   - 问题：refresh_token 续期后旧的没失效——被盗后无法撤销
   - 优化：user_accounts 表加 token_version INT DEFAULT 0
     - JWT payload 里带 ver=token_version
     - verify 时对比数据库的 token_version——不匹配则失效
     - 改密码时 token_version+1 → 所有旧 token 失效
     - 用户主动"踢出所有设备"时 token_version+1

4. **logout 后 token 仍然有效（token_version 撤销）**
   - 问题：JWT 无状态 → logout 后旧 token 在过期前仍然有效
   - 优化：logout 时 token_version+1 → 当前用户所有 token 失效
     - 比 Redis 黑名单更轻量——不需要新依赖
     - 副作用：logout 会踢出所有设备（多设备同时被踢）
     - 可接受——用户主动登出时踢所有设备是合理行为

5. **refresh_token 存 localStorage 有 XSS 风险**
   - 问题：access_token 和 refresh_token 都存 localStorage → XSS 可以偷到 refresh_token → 7 天内无限刷新
   - 优化：refresh_token 改存 HttpOnly cookie → XSS 偷不到
     - access_token 仍存 localStorage（需要 JS 读取加到 header）
     - refresh_token 只用于 POST /auth/refresh → 服务端从 cookie 读 → JS 不需要接触
     - 需要加 CSRF 保护：refresh 端点校验 SameSite=Strict cookie + 自定义 header（X-Requested-With）
     - 登录时 Set-Cookie refresh_token（HttpOnly + Secure + SameSite=Strict + Path=/api/auth）
     - logout 时 Set-Cookie 清除 refresh_token cookie

6. **滚动续期绝对上限（防 token 永久存活）**
   - 问题：refresh_token 每次刷新续期 7 天，用户一直操作可能永久不重新登录——token 永久存活有安全风险
   - 优化：设置最大绝对生命周期 30 天
     - 相对滚动：每次刷新 exp = now + 7 天
     - 绝对上限：不能超过首次签发 + 30 天，到时间强制登录
     - 实现：max_exp 写在 refresh_token payload 里（不写 access——access 只有 15 分钟，30 天上限管控对象是 refresh 长令牌）
     - 首次登录时生成 max_exp = now + 30 天
     - refresh 换新 refresh 时继承旧 max_exp，不重新生成
     - verify 时 4 个条件全通过才下发新 token：签名 + exp + max_exp + token_version
   - 边界：改密码触发 token_version+1 优先走 version 失效，不用等到 max_exp

7. **多 Tab token 同步**
   - 问题：Tab A 刷新 token 成功，Tab B 还拿旧 access_token 触发 401 再次刷新——多 Tab 场景 token 不同步
   - 优化：监听 storage 事件——Tab A 刷新 token 后其他 Tab 通过 storage event 同步新 token
     - window.addEventListener('storage', ...) 监听 localStorage['token'] 变化
     - 检测到变化时更新当前 Tab 的请求 header
   - 定位：体验优化，不是安全屏障——不能强依赖 storage 事件
   - 局限：storage 事件只同步 access（refresh 在 HttpOnly cookie 里 JS 读不到）
   - 兜底：即使同步失败，旧 access 401 → Promise 队列 refresh → 正常工作，只是多一次刷新流程

**配套约束：**

1. **Cookie 完整配置（refresh cookie）**
   - Set-Cookie: refresh_token=xxx; HttpOnly; Secure; SameSite=Lax; Path=/api/auth
   - Path 限制只在刷新接口发送，减少 cookie 随业务接口到处携带
   - 生产环境必须 HTTPS（Secure=True）

2. **refresh 接口 CSRF**
   - 只有 refresh 接口需要 CSRF 校验——从 Cookie 拿 refresh_token + 请求头 X-Requested-With
   - 业务接口继续沿用 Bearer Header，不需要 CSRF
   - 贴合现有架构——Bearer 鉴权兼容 Web/CLI/Daemon/第三方调用

3. **登出逻辑**
   - DB 执行原子 token_version += 1，使现存 refresh/access 全部失效
   - Set-Cookie 指令清除 refresh_token HttpOnly Cookie

**性能优化（后续按需实施）：**

8. **token_version 每次查数据库**
   - 问题：每次 API 请求都查数据库 user_accounts.token_version——高 QPS 时有压力
   - 优化：低 QPS B 端系统完全没问题；高 QPS 时加 Redis 缓存 user_id → token_version，TTL 60 秒
   - 当前不改——B 端系统 QPS 不高，查一个整数字段很轻

**不采纳的建议：**

- access_token 改 HttpOnly cookie：会变成 multica 单 token 方案，所有请求都要 CSRF 保护，不能用 Bearer header，和现有架构不兼容。XSS 风险靠 CSP 策略 + access 15分钟短有效期缓解
- CSRF 用独立 token 不用 X-Requested-With：X-Requested-With + SameSite=Strict 是有效的 CSRF 防护——跨站请求无法加自定义 header，B 端系统够用

JWT_SECRET 安全（参照 multica）：
- 生产环境不能用默认 secret
- 启动时校验——空值或已知默认值拒绝启动

**multica 方案对比：** multica 单 token 存 HttpOnly cookie，TTL 30 天固定过期，无滚动续期，无 refresh 端点。30 天后不管用不用都必须重新登录。grape 更灵活——7 天滚动续期（活跃用户不踢出）+ 30 天绝对上限（防止永久存活）。

#### 3.2 Redis（可选优化——规模上来再做）

**不是多实例的必要条件。** grape 用 HTTP 短连接 + 共享 MySQL，不需要实例间通信。

multica 需要 Redis 是因为 WebSocket 长连接绑定实例——实例间必须通过 Redis 转发消息。grape 用 HTTP 不绑定实例——不需要。

| 用途 | 没有 Redis | 有 Redis | 什么时候需要 |
|------|-----------|---------|------------|
| 取消通知 | 15 秒（heartbeat 查 MySQL） | 0 秒（Redis Stream 转发） | 取消频繁且需实时 |
| 任务回收 | 60 秒（看门狗轮询） | 精确调度（ZSET） | 任务量大时 |
| 缓存 | 每次查数据库 | 缓存命中 | 查询量大时 |

**判断标准：** 数据库扛得住就不需要 Redis。等查询慢了或取消延迟不够用再加。

#### 3.3 WebSocket 端点（可选优化——不推荐）

**不推荐。** grape 用 HTTP 短连接比 WebSocket 长连接更适合多实例架构：
- HTTP 不绑定实例——nginx 负载均衡自然分发
- WebSocket 绑定实例——需要 Redis 转发实例间消息
- HTTP 简单可靠——WebSocket 需要处理断线重连

如果未来需要 0 秒取消延迟，优先加 Redis Stream（3.2）而不是 WebSocket。

#### 3.4 Backend replicas>1（已验证可行）

```nginx
upstream grape_backend {
  server 127.0.0.1:3001;
  server 127.0.0.1:3002;
  server 127.0.0.1:3003;
}
```

本地验证已证明：nginx 负载均衡 + 多实例 + 共享 MySQL 工作正常。

#### 3.5 集群兼容保证（已验证）

| 操作 | 怎么保证 | 验证状态 |
|------|---------|:---:|
| 用户会话共享 | JWT 无状态（JWT_SECRET 共享） | ✅ 已验证 |
| token_version 跨实例撤销 | 共享数据库 token_version | ✅ 已验证 |
| refresh 跨实例 | 同一个 JWT_SECRET + 共享 token_version | ✅ 已验证 |
| 任务不重复 claim | MySQL 行锁 UPDATE WHERE status='queued' | ✅ 已验证 |
| 任务不重复完成 | UPDATE WHERE status='running' 幂等 | ✅ |
| 取消通知跨实例 | heartbeat 查 MySQL cancelled_tasks | ✅ 已验证 |
| webhook 不重复 | nginx 随机打一个实例 | ✅ |
| 心跳不冲突 | UPDATE last_seen_at 幂等 | ✅ |
| 迁移不冲突 | MySQL GET_LOCK | ✅ 已验证 |

**多集群验证结果（全部通过）：**

JWT 跨实例验证（11 项）：
- 实例 A 登录的 token 实例 B 能用 ✅
- 实例 A refresh 的新 token 实例 B 能用 ✅
- 实例 B logout 后实例 A 的 token 也失效 ✅（token_version 跨实例）
- 实例 B logout 后实例 A 的 refresh 也失效 ✅
- 迁移锁两个实例同时启动不冲突 ✅

容器化集群验证（15 项）：
- 两个容器实例只启动 3000（不启动 8080）✅
- JWT 跨容器验证 ✅
- token_version 跨容器撤销 ✅
- Worker 跨容器注册/心跳/claim ✅
- webhook 在 main:app 里（每个容器都有）✅
- Docker healthcheck 端点正常 ✅
- 迁移锁防多容器同时迁移 ✅
- nginx 负载均衡 ✅

实例切换验证（A↔B 互切）：
- A 启动→停 B→Worker 自动转 A→任务不中断 ✅
- B 启动→停 A→Worker 自动转 B→任务不中断 ✅
- Worker 走 nginx 80→实例停了自动转→无感知 ✅
- Issue #24 任务在实例切换过程中继续执行 ✅

**Worker 连接方式（重要）：**

Worker 必须连 nginx 80（负载均衡），不能直连某个实例：
```
RUNTIME_SERVER_URL=http://127.0.0.1:80  （或 http://公网IP）
```
- 直连某个实例（如 3001）→ 该实例停了 Worker 无法重连
- 连 nginx 80 → 实例停了 nginx 自动转 → Worker 自动重连

**10 分钟宕机数据丢失分析：**

| 数据 | 会丢吗 | 说明 |
|------|:---:|------|
| 任务状态 | ❌ 不丢 | 在共享 MySQL |
| queued 任务 | ❌ 不丢 | 在共享 MySQL |
| 正在执行的任务 | ❌ 不中断 | Worker 进程没停，引擎继续执行 |
| Worker 本地日志 | ❌ 不丢 | /tmp/worker.log 继续写 |
| heartbeat | ❌ 不丢 | 恢复后继续 |
| step 日志 | ⚠️ 可能丢部分 | report_messages 失败的日志丢失（retries 3 次后丢弃） |

**改动文件：**
- src/api/jwt_utils.py（JWT 签发+验证工具——access/refresh/max_exp/token_version）
- src/api/routes/auth.py（login/refresh/logout/OAuth——双 token + HttpOnly cookie + token_version+1）
- src/api/deps.py（get_current_user——verify_access_token + token_version 校验）
- src/user/db.py（get_token_version / increment_token_version / verify_user 返回 token_version）
- src/api/db_setup.py（user_accounts DDL + migration 加 token_version）
- src/api/static/js/api.js（Promise 队列 + refresh 从 cookie + CSRF header + 多 Tab storage 同步）
- src/api/static/js/app.js（登录/logout 不操作 refresh_token——cookie 管理）
- .env.example 加 JWT_SECRET
- requirements.txt 加 python-jose[cryptography]
- 12 个测试文件适配 JWT（_jwt_access_token helper）

**不需要：** Redis / WebSocket / Redis Stream / docker-compose 加 Redis / nginx 改 upstream（已有）

---

### 阶段 4：K8s 集群（Helm）——❌ 未开始

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

---

## 本地验证方案——模拟多实例集群（零新依赖）

在没装 Docker 的情况下，通过启动多个 uvicorn 进程模拟集群部署，验证集群兼容性。

### 验证环境

```
nginx:80（upstream 负载均衡）
  ├── 实例 A: 127.0.0.1:3001（uvicorn main:app）
  └── 实例 B: 127.0.0.1:3002（uvicorn main:app）

共享：MySQL RDS + OBS
```

### 验证项

#### 1. 迁移锁验证

**目的：** 两个实例同时启动时，迁移锁是否生效——防止同时建表/改表。

**步骤：**
1. 同时启动实例 A（3001）和实例 B（3002）
2. 观察日志——实例 A 拿到 GET_LOCK，实例 B 等待
3. 实例 A 迁移完释放锁，实例 B 拿到锁发现表已存在（no-op）

**预期：**
- 日志出现 `GET_LOCK('grape_migrate')` 
- 两个实例不会同时执行 `_create_tables()`
- 不会报 "Table already exists" 错误

#### 2. 任务不重复 claim 验证

**目的：** 两个实例同时 claim 任务——MySQL 行锁是否防止重复。

**步骤：**
1. 创建一个测试任务（status=queued）
2. 实例 A 和实例 B 同时调 `POST /api/runtime/runtimes/{id}/tasks/claim`
3. 检查任务 status——只有一个实例 claim 成功

**预期：**
- 只有一个实例拿到任务（status=dispatched）
- 另一个实例返回空（no task）

#### 3. 任务取消跨实例感知验证

**目的：** 实例 A 取消任务——实例 B 的 Worker 是否通过 heartbeat 感知。

**步骤：**
1. 实例 A 的 Worker 认领任务
2. 通过实例 B 调 `POST /api/runtime/tasks/{id}/cancel`
3. 实例 A 的 Worker 下次 heartbeat 时收到 `cancelled_tasks`
4. Worker 杀引擎进程

**预期：**
- heartbeat 响应包含 `cancelled_tasks: [task_id]`
- Worker 日志出现 `heartbeat: task {id} cancelled, killing engine`
- 任务 status 变为 cancelled

#### 4. nginx 负载均衡验证

**目的：** 请求是否分发到两个实例。

**步骤：**
1. nginx upstream 配置 3001 + 3002
2. 连续调 `GET /api/health` 多次
3. 检查两个实例的日志——都有请求

**预期：**
- 两个实例都收到请求
- 负载均衡正常分发

### nginx 配置（验证用）

在现有 nginx 配置里加 upstream：

```nginx
upstream grape_cluster {
    server 127.0.0.1:3001;
    server 127.0.0.1:3002;
}

server {
    listen 80;
    server_name 1.92.115.229;

    location /api/ {
        proxy_pass http://grape_cluster;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    }
}
```

### 启动命令

```bash
# 实例 A
OBS_ENABLED=true OBS_BUCKET=obs-grape-dev-file \
  OBS_ACCESS_KEY=*** OBS_SECRET_KEY=*** \
  OBS_ENDPOINT=https://obs.cn-north-4.myhuaweicloud.com \
  OBS_PREFIX=grape/prod PYTHONPATH=/home/yangaiwu/grape \
  .venv/bin/uvicorn src.api.main:app --host 127.0.0.1 --port 3001

# 实例 B
OBS_ENABLED=true OBS_BUCKET=obs-grape-dev-file \
  OBS_ACCESS_KEY=*** OBS_SECRET_KEY=*** \
  OBS_ENDPOINT=https://obs.cn-north-4.myhuaweicloud.com \
  OBS_PREFIX=grape/prod PYTHONPATH=/home/yangaiwu/grape \
  .venv/bin/uvicorn src.api.main:app --host 127.0.0.1 --port 3002
```

### 注意事项

1. 两个实例共用同一个 MySQL + OBS——环境变量相同
2. 两个实例各自有进程内 `_user_tokens`——登录 token 不互通（阶段 3 JWT 解决）
3. Worker 只连一个实例——验证时手动指定 RUNTIME_SERVER_URL
4. 验证完后恢复单实例部署
5. nginx 配置验证完后恢复原配置
