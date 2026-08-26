# 阶段 3 详细设计方案：多实例集群（JWT 会话共享 + 可选 Redis）

## 背景

阶段 1（任务取消实时性 + Worker repo_key）和阶段 2（容器化部署）已完成。
阶段 3 将 grape Backend 支持多实例水平扩展——JWT 无状态会话共享。

**基于本地验证结论：** 多实例核心能力（迁移锁/claim/取消感知/负载均衡）已验证通过——不需要 Redis 也能多实例运行。所有状态在 MySQL 共享数据库，实例间不需要通信。

## 目标

Backend 水平扩展，支持高可用。多实例共享同一个 JWT_SECRET + 共享 MySQL。

## 两种部署方式共存

| 部署方式 | 启动命令 | 端口 | 适用场景 |
|---------|---------|------|---------|
| 老方式（非容器） | `python src/api/run.py` | 3000+8080 双进程 | 开发/测试/快速部署 |
| 新方式（容器） | `docker compose up -d` | 3000 单进程 | 生产/标准化部署 |

两种方式共用同一套代码/数据库/nginx，老方式不受影响。

---

## 改动点

### 1. JWT 双 token 机制

| token | 有效期 | 用途 | 续期策略 | 存储 |
|-------|--------|------|---------|------|
| access_token | 15 分钟 | API 请求 | 过期后用 refresh_token 换新 | localStorage（JS 可读） |
| refresh_token | 7 天滚动 + 30 天绝对上限 | 换新 access_token | 每次换新时滚动续期 7 天，max_exp 30 天不更新 | HttpOnly cookie（JS 不可读） |

**会话类型对照：**

| 会话类型 | 当前 | 改后 | 需要改？ |
|---------|------|------|:---:|
| 用户登录 | 进程内内存（_user_tokens） | JWT 无状态 | 改 |
| Worker 认证 | 数据库（runtime_credentials） | 不变 | 不改 |
| Agent 任务会话 | 数据库（agent_tasks.runtime_session_id） | 不变 | 不改 |

### 2. 滚动续期 + 绝对上限

refresh_token payload 含两个时间字段：
- `exp`：滚动过期时间（每次 refresh 重新算 now + 7 天）
- `max_exp`：绝对上限（首次签发时 = now + 30 天，refresh 时继承不更新）

verify 时 4 个条件全通过才有效：签名 + exp + max_exp + token_version

效果：
- 用户一直操作 → access_token 15分钟过期 → 自动 refresh → 拿到新 access + 新 refresh（续期 7 天，max_exp 不变）→ 30 天内永不踢出
- 用户 7 天不操作 → refresh_token exp 过期 → 下次操作时 refresh 失败 → 跳登录页
- 用户 30 天后 → max_exp 到达 → 即使 exp 没过期也强制登录

### 3. token_version 撤销机制

user_accounts 表有 token_version INT DEFAULT 0 字段。

- 签发 token 时：JWT payload 写入 ver=当前 token_version
- 验证 token 时：对比 payload.ver 和数据库 token_version——不匹配则 401
- logout 时：token_version+1 → 所有旧 token 立即失效（access + refresh）
- 改密码时：token_version+1 → 同上
- 攻击者无法伪造：改 ver 需要重新签名，没有 JWT_SECRET 不能签名

token_version 会一直增加但不需要重置——值大小没有意义，只关心是否相等。INTEGER 最大值 2^31-1 = 21 亿，够用几百万年。

### 4. refresh_token HttpOnly cookie + CSRF 保护

- refresh_token 存 HttpOnly cookie（HttpOnly + Secure + SameSite=Lax + Path=/api/auth）
- refresh 端点从 cookie 读 refresh_token（JS 不接触）
- CSRF 保护：X-Requested-With: XMLHttpRequest header（跨站伪造无法加自定义 header）
- 业务接口继续用 Bearer header——不需要 CSRF

### 5. 并发 401 Promise 队列

- 多个请求同时 401 时，第一个创建 _refreshPromise，其他请求 await 同一个 Promise
- refresh 完成后所有请求用新 token 重发——不丢失请求
- refresh 本身 401 时队列全部请求跳登录页

### 6. 多 Tab 同步

- 监听 storage 事件——Tab A 刷新 token 后其他 Tab 自动同步
- 定位：体验优化不是安全屏障——兜底有 401 + Promise 队列
- 局限：只同步 access（refresh 在 cookie JS 读不到）

---

## 完整流程

### 登录

```
用户输入用户名密码 → POST /api/auth/login
→ 服务端验证密码
→ 读取数据库 token_version（比如 0）
→ 签发 access_token（ver=0, 15分钟）+ refresh_token（ver=0, 7天, max_exp=now+30天）
→ access_token 返回 body → 前端存 localStorage
→ refresh_token Set-Cookie → HttpOnly + SameSite=Lax + Path=/api/auth
→ 服务端不存任何 token
```

### API 请求

```
前端从 localStorage 读 access_token → Authorization: Bearer header
→ 服务端 verify_access_token：签名+过期+type=access
→ 对比 payload.ver=0 和 db.token_version=0 → 0==0 → 通过
→ 返回数据
```

### access_token 过期（15分钟）

```
→ API 返回 401
→ 前端调 POST /api/auth/refresh
→ 浏览器自动带 cookie 里的 refresh_token
→ CSRF 校验：X-Requested-With: XMLHttpRequest header
→ 服务端 verify_refresh_token：签名+过期+type=refresh+max_exp
→ 对比 payload.ver=0 和 db.token_version=0 → 0==0 → 通过
→ 签发新 access_token（ver=0, 15分钟）+ 新 refresh_token（ver=0, 7天滚动, max_exp 继承旧值）
→ 新 refresh_token Set-Cookie 覆盖旧的
→ 前端用新 access_token 重发原请求——用户无感知
```

### logout

```
→ POST /api/auth/logout（需登录）
→ 服务端 UPDATE token_version=token_version+1（0→1）
→ 清除 refresh_token cookie
→ 前端清 localStorage（access_token + user）
→ 跳登录页

logout 后旧 token 再请求：
  第一步：签名+过期 → 通过（token 没被篡改没过期）
  第二步：payload.ver=0 对比 db.token_version=1 → 0≠1 → 401 失效

logout 后旧 refresh_token 再 refresh：
  第一步：签名+过期 → 通过
  第二步：payload.ver=0 对比 db.token_version=1 → 0≠1 → 401 失效

→ access_token + refresh_token 全部立即失效
```

---

## 优化项

### 1. 并发 401 请求丢失

- 问题：页面同时发多个请求，access_token 过期时多个请求同时 401，_refreshing 锁导致部分请求直接 reject 不重试
- 优化：用 Promise 队列——第一个 refresh 时其他请求等待同一个 Promise，refresh 完成后全部重发
- 边界处理：refresh 本身返回 401（refresh 失效）时队列全部请求直接抛出登录失效跳登录页，不无限等待

### 2. OAuth 登录没有 refresh_token

- 问题：OAuth 回调只返回 access_token，15 分钟后被踢
- 优化：OAuth 回调也签发 refresh_token，通过 cookie 传给前端

### 3. 旧 refresh_token 仍然有效（token_version 机制）

- 问题：refresh_token 续期后旧的没失效——被盗后无法撤销
- 优化：user_accounts 表加 token_version INT DEFAULT 0
  - JWT payload 里带 ver=token_version
  - verify 时对比数据库的 token_version——不匹配则失效
  - 改密码时 token_version+1 → 所有旧 token 失效

### 4. logout 后 token 仍然有效（token_version 撤销）

- 问题：JWT 无状态 → logout 后旧 token 在过期前仍然有效
- 优化：logout 时 token_version+1 → 当前用户所有 token 失效
  - 比 Redis 黑名单更轻量——不需要新依赖
  - 副作用：logout 会踢出所有设备（多设备同时被踢）
  - 可接受——用户主动登出时踢所有设备是合理行为

### 5. refresh_token 存 localStorage 有 XSS 风险

- 问题：refresh_token 存 localStorage → XSS 可以偷到 → 7 天内无限刷新
- 优化：refresh_token 改存 HttpOnly cookie → XSS 偷不到
  - access_token 仍存 localStorage（需要 JS 读取加到 header）
  - refresh_token 只用于 POST /auth/refresh → 服务端从 cookie 读
  - CSRF 保护：SameSite=Strict cookie + X-Requested-With header

### 6. 滚动续期绝对上限（防 token 永久存活）

- 问题：refresh_token 每次刷新续期 7 天，用户一直操作可能永久不重新登录
- 优化：设置最大绝对生命周期 30 天
  - 相对滚动：每次刷新 exp = now + 7 天
  - 绝对上限：不能超过首次签发 + 30 天
  - 实现：max_exp 写在 refresh_token payload 里，首次签发时生成，refresh 时继承不更新
  - 边界：改密码触发 token_version+1 优先走 version 失效，不用等到 max_exp

### 7. 多 Tab token 同步

- 问题：Tab A 刷新 token 成功，Tab B 还拿旧 access_token 触发 401
- 优化：监听 storage 事件——Tab A 刷新 token 后其他 Tab 自动同步
  - 定位：体验优化，不是安全屏障
  - 局限：只同步 access（refresh 在 cookie JS 读不到）
  - 兜底：即使同步失败，旧 access 401 → Promise 队列 refresh → 正常工作

---

## 配套约束

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

---

## 性能优化（后续按需实施）

**token_version 每次查数据库**
- 问题：每次 API 请求都查数据库 user_accounts.token_version——高 QPS 时有压力
- 优化：低 QPS B 端系统完全没问题；高 QPS 时加 Redis 缓存 user_id → token_version，TTL 60 秒
- Redis 缓存失效模式：token_version+1 时 DELETE key（不是 SET 新值），下次请求回源 DB
- 当前不改——B 端系统 QPS 不高，查一个整数字段很轻

---

## 不采纳的建议

- **access_token 改 HttpOnly cookie**：会变成 multica 单 token 方案，所有请求都要 CSRF 保护，不能用 Bearer header，和现有架构不兼容。XSS 风险靠 CSP 策略 + access 15分钟短有效期缓解
- **CSRF 用独立 token 不用 X-Requested-With**：X-Requested-With + SameSite=Strict 是有效的 CSRF 防护——跨站请求无法加自定义 header，B 端系统够用

---

## JWT_SECRET 安全

- 生产环境不能用默认 secret
- 启动时校验——空值或已知默认值拒绝启动
- 所有实例配同一个 JWT_SECRET——任何实例都能验证

---

## multica 方案对比

| 维度 | multica | grape |
|------|---------|-------|
| token 存储 | HttpOnly cookie | access→localStorage / refresh→cookie |
| token 数量 | 单 token | 双 token |
| TTL | 长（30天固定） | access 15分钟 / refresh 7天滚动 + 30天绝对上限 |
| refresh 端点 | 无 | 有 |
| token_version | 无 | 有 |
| logout | 只清 cookie | 清 cookie + token_version+1 |
| CSRF | ValidateCSRF（所有请求） | X-Requested-With（只 refresh） |

multica 不需要 token_version——token 在 HttpOnly cookie 里 JS 偷不到，清 cookie 就够。
grape 需要 token_version——access_token 在 localStorage 里 JS 能读到，清 cookie 管不了。

---

## Redis（可选优化——规模上来再做）

不是多实例的必要条件。grape 用 HTTP 短连接 + 共享 MySQL，不需要实例间通信。

| 用途 | 没有 Redis | 有 Redis | 什么时候需要 |
|------|-----------|---------|------------|
| 取消通知 | 15 秒（heartbeat 查 MySQL） | 0 秒（Redis Stream 转发） | 取消频繁且需实时 |
| 任务回收 | 60 秒（看门狗轮询） | 精确调度（ZSET） | 任务量大时 |
| 缓存 | 每次查数据库 | 缓存命中 | 查询量大时 |

判断标准：数据库扛得住就不需要 Redis。等查询慢了或取消延迟不够用再加。

---

## WebSocket（不推荐）

grape 用 HTTP 短连接比 WebSocket 长连接更适合多实例架构：
- HTTP 不绑定实例——nginx 负载均衡自然分发
- WebSocket 绑定实例——需要 Redis 转发实例间消息
- HTTP 简单可靠——WebSocket 需要处理断线重连

---

## 集群兼容保证（已验证）

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

---

## 验证结果

### JWT 跨实例验证（11 项）

- 实例 A 登录的 token 实例 B 能用 ✅
- 实例 A refresh 的新 token 实例 B 能用 ✅
- 实例 B logout 后实例 A 的 token 也失效 ✅（token_version 跨实例）
- 实例 B logout 后实例 A 的 refresh 也失效 ✅
- 迁移锁两个实例同时启动不冲突 ✅

### 容器化集群验证（15 项）

- 两个容器实例只启动 3000（不启动 8080）✅
- JWT 跨容器验证 ✅
- token_version 跨容器撤销 ✅
- Worker 跨容器注册/心跳/claim ✅
- webhook 在 main:app 里（每个容器都有）✅
- Docker healthcheck 端点正常 ✅
- 迁移锁防多容器同时迁移 ✅
- nginx 负载均衡 ✅

### 实例切换验证（A↔B 互切）

- A 启动→停 B→Worker 自动转 A→任务不中断 ✅
- B 启动→停 A→Worker 自动转 B→任务不中断 ✅
- Worker 走 nginx 80→实例停了自动转→无感知 ✅
- Issue #24 任务在实例切换过程中继续执行 ✅

### Worker 连接方式（重要）

Worker 必须连 nginx 80（负载均衡），不能直连某个实例：
```
RUNTIME_SERVER_URL=http://127.0.0.1:80  （或 http://公网IP）
```
- 直连某个实例（如 3001）→ 该实例停了 Worker 无法重连
- 连 nginx 80 → 实例停了 nginx 自动转 → Worker 自动重连

### 10 分钟宕机数据丢失分析

| 数据 | 会丢吗 | 说明 |
|------|:---:|------|
| 任务状态 | ❌ 不丢 | 在共享 MySQL |
| queued 任务 | ❌ 不丢 | 在共享 MySQL |
| 正在执行的任务 | ❌ 不中断 | Worker 进程没停，引擎继续执行 |
| Worker 本地日志 | ❌ 不丢 | /tmp/worker.log 继续写 |
| heartbeat | ❌ 不丢 | 恢复后继续 |
| step 日志 | ⚠️ 可能丢部分 | report_messages 失败的日志丢失（retries 3 次后丢弃） |

---

## 改动文件

| # | 文件 | 改动内容 |
|---|------|---------|
| 1 | src/api/jwt_utils.py（新增） | JWT 双 token：access 15分钟 + refresh 7天滚动+30天 max_exp + token_version |
| 2 | src/api/routes/auth.py | login 双 token + refresh 从 cookie + CSRF + 滚动续期 + logout token_version+1 + OAuth |
| 3 | src/api/deps.py | verify_access_token + token_version 校验 |
| 4 | src/user/db.py | token_version 字段 + get/increment_token_version + verify_user 返回 token_version |
| 5 | src/api/db_setup.py | user_accounts migration 加 token_version |
| 6 | src/api/static/js/api.js | Promise 队列 + cookie refresh + CSRF header + 多 Tab storage 同步 |
| 7 | src/api/static/js/app.js | 登录/logout 不操作 refresh_token——cookie 管理 |
| 8 | .env.example | 加 JWT_SECRET |
| 9 | requirements.txt | 加 python-jose[cryptography] |
| 10 | 12 个测试文件 | create_session_token → _jwt_access_token + token_version + heartbeat mock bool→dict |

---

## 数据库新增字段

| # | 表.字段 | 类型 | 默认值 | 用途 |
|---|---------|------|--------|------|
| 1 | user_accounts.token_version | INT | 0 | JWT 撤销版本号——logout/改密码时 +1，旧 token 立即失效 |

**不需要：** Redis / WebSocket / Redis Stream / docker-compose 加 Redis / nginx 改 upstream（已有）
