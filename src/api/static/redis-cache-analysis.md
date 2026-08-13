# Grape 多机部署 Redis 缓存引入分析建议

> 版本：2026-08-13（基于 dev 分支 8d222942）
> 适用场景：① 各 agent 分机器部署（coding/qa/vod/issue 等 agent 独立机器）；
> ② 用户控制台与运营管理台分机器部署。
> 目标：给出哪些位置需要引入 Redis 缓存/协调，以及优先级与实施路线。

---

## 1. 背景与现状

当前 Grape 为**单机单体**部署：

| 组件 | 现状 |
|---|---|
| Web 服务 | 单进程 uvicorn（`src.api.main:app`，0.0.0.0:38080） |
| 数据库 | MySQL RDS（生产；`DEVOPS_DB_BACKEND` 可切 sqlite 测试） |
| 锁 | 进程内 `threading.RLock`（`src/session_manager.py: db_lock`） |
| 队列 | `agent_tasks` 表 + worker 轮询 claim（DB 驱动） |
| 运行时状态 | `agent_runtimes` 表（心跳/槽位） |
| 会话 | `sessions` 表（DB） |
| OAuth 状态 | **进程内内存 dict**（`src/user/db.py: _oauth_store`） |
| 定时任务 | 进程内 `asyncio.create_task(sweep_loop())` |
| IM | 进程内 BotManager（长连接 WebSocket） |
| 静态资源 | uvicorn 静态目录（`src/api/static/`） |
| 本地缓存 | 文件缓存（worker 安装包 `data/cache/`） |

**结论：当前架构零 Redis。** 单机时进程内锁/内存状态/单实例定时任务均可用；
一旦按「agent 分机器 + 控制台分机器」拆分部署，**同一套数据库被多个进程同时访问**，
以下位置将出现一致性 / 互斥 / 状态共享问题，需要引入 Redis（或等价分布式组件）。

---

## 2. 分机器部署后需要引入 Redis 的位置（按优先级分级）

### P0 —— 必须引入（不做则多机部署直接出问题）

#### 2.1 分布式锁（替代进程内 db_lock）
- **现状**：`src/session_manager.py` 的 `db_lock = threading.RLock()` 保护全部 DB 写路径
  （事件、任务、会话、凭据…）。多实例部署后每个进程各持一把锁，**互不感知**。
- **问题**：
  - 并发写同一行（如事件状态流转、`busy_slots` 槽位增减）产生竞态；
  - `claim` 用 `UPDATE agent_tasks SET status='dispatched' ... WHERE id=? AND status='queued'`
    是原子 UPDATE（DB 层可防抢单），但 `busy_slots+1/-1` 是**独立 UPDATE，非同一事务**，
    多实例并发时槽位计数可能错乱（此前单机已出现 busy_slots 泄漏 23 的案例）。
- **方案**：
  - 通用写路径：Redis 分布式锁（`SET key val NX PX 30000`，Redlock 可后置），
    key 按行粒度 `lock:event:{seq}` / `lock:task:{id}` / `lock:runtime:{id}`；
  - **更优**：业务写尽量收敛为单条原子 SQL（如 claim 已做到），
    仅对「读-改-写」复合操作（sweep 回收、槽位修正、A2A 交办）加分布式锁。
- **Redis 结构**：`SET lock:<resource> <uuid> NX PX 30000`；释放用 Lua 比对 uuid 防误删。

#### 2.2 OAuth state 内存态 → Redis（重要）
- **现状**：`src/user/db.py: _oauth_store` 是**进程内 dict**，存 GitCode/Feishu 扫码的
  `state → provider/data`，TTL 600s。
- **问题**：分机部署后，用户在第 A 台机器发起扫码（写入 A 的内存），
  回调/轮询落到第 B 台机器（读 B 的内存）→ **state 找不到，扫码失败**。
- **方案**：OAuth state 直接存 Redis：`SET oauth:state:{state} {json} EX 600`。
- **Redis 结构**：String + EX 过期（天然对齐现有 TTL 语义）。

#### 2.3 定时任务互斥（sweep_loop 多实例重复执行）
- **现状**：`src/api/main.py` 启动 `sweep_loop()`（约 15 分钟周期），
  执行僵尸事件回收 `sweep_stuck_events_once`、孤儿槽位清理等。
- **问题**：N 台机器各起一个 sweep_loop → 同一事件被多实例同时回收/置 failed，
  且槽位修正互相覆盖。
- **方案**：Redis 分布式锁做 **leader 选举**：`SET lock:sweep <uuid> NX PX 1200`
  抢到的实例执行，其余跳过本轮。后续若做「指定实例专属任务」可升级为选主（续约）。
- **Redis 结构**：String + NX + 续约（或 SETEX 简单版）。

#### 2.4 IM BotManager 单实例限制
- **现状**：BotManager 在进程内维护 IM 长连接（WebSocket）。
- **问题**：分机部署后多个进程各自建立 IM 连接 → 重复收消息/重复回复/重复推送。
- **方案**（两选一）：
  - a) **IM 网关单实例**：IM 长连接只允许一个进程持有，其他实例不启动 BotManager
    （配置 `IM_ENABLED=1` 仅主实例开）；主实例由 Redis 锁选举；
  - b) 多实例各自连接 + Redis Pub/Sub 广播去重（复杂，不推荐起步）。
- **建议**：方案 a（锁选主），配合 2.3 的 leader 机制统一实现。

### P1 —— 建议引入（多机后性能/体验明显受益）

#### 2.5 会话 token 校验缓存
- **现状**：`get_current_user` 每请求查 `sessions` 表（RDS）。
- **问题**：多控制台分机 + 前端轮询（workers/events 页 3s 轮询）→ RDS 读放大。
- **方案**：登录态缓存 `sess:{token} → user_id`，TTL 与 session 有效期对齐
  （Redis 不存在时回源 DB 并回填；登出/吊销时 DEL）。
- **Redis 结构**：String + TTL；失效走主动 DEL（登出）+ 被动过期。

#### 2.6 热路径查询缓存（控制台列表页）
- **现状**：events / agent_tasks / agent_runtimes 列表页每轮询全表查询。
- **问题**：控制台与 agent 分机后，worker claim（高频写）+ 控制台轮询（高频读）
  争抢同一组表 → RDS 压力与慢查询。
- **方案**：
  - runtime 在线状态/槽位：worker 心跳时写 Redis（`runtime:{id}` Hash，
    字段 status/healthy/busy_slots/ts），控制台读 Redis；DB 仍落地审计；
  - 事件列表：仅缓存**统计聚合**（按状态计数，如 `stat:events:status` 计数缓存 5-10s），
    明细列表不做缓存（一致性敏感，且分页偏移复杂）。
- **Redis 结构**：Hash（runtime 状态）+ String/INCR（计数）。

#### 2.7 SSE / 事件推送广播（Event 对话、任务状态变更）
- **现状**：Event 对话 SSE 连接挂在发起请求的那台进程上；agent 完成任务的
  状态更新（DB）后，由**哪个进程**把结果推给浏览器取决于「谁在等」。
- **问题**：任务被 A 机 worker 执行完写 DB，浏览器 SSE 连接在 B 机 →
  若状态轮询/推送机制是进程内事件，B 机感知不到 → 对话/进度不刷新。
- **方案**：
  - a) 前端轮询兜底（现已有轮询，SSE 是增强）——不加 Redis 也能用，但体验降级；
  - b) Redis Pub/Sub：任务终态/对话回复发布到 `channel:events`，
    所有实例订阅后推给本机持有的 SSE 连接（目标连接按连接 ID 路由）。
- **Redis 结构**：Pub/Sub（或 Stream 带 ACK，可靠投递时用）。

#### 2.8 Webhook 幂等去重
- **现状**：GitCode webhook 事件入口（`/webhook/gitcode`）依赖「先查事件是否存在」
  防重复建事件。
- **问题**：多实例同时收到同一 webhook（GitCode 重试/双发）→ 查-插非原子 → 重复事件。
- **方案**：Redis `SETNX webhook:dedup:{hook_id} 1 EX 300`，命中即丢弃。
- **Redis 结构**：String + SETNX + EX。

#### 2.9 仓库/凭据类元数据缓存
- **现状**：`repo_key → bot_token`（`runtime_credentials` / 仓库配置）每次任务下发
  查库；GitCode API 调用（PR 详情、issue 评论）无缓存。
- **方案**：凭据映射缓存 5 分钟（变更时主动 DEL）；GitCode API 响应按 URL 缓存
  30-60s（TTL 短，防一致性敏感）。
- **Redis 结构**：String + TTL。

### P2 —— 可选（当前阶段可不做）

| 位置 | 说明 |
|---|---|
| 静态资源缓存 | 归 Nginx/CDN 层（Cache-Control），不需要 Redis |
| worker 安装包分发 | 本地 `data/cache/` → 改对象存储/共享盘 + Nginx 直出，Redis 不适用 |
| 全量事件/任务明细缓存 | 一致性敏感、写多读多，缓存收益低且容易脏读，不做 |
| 数据库查询结果缓存 | RDS 合理索引 + 慢查询优化优先于 Redis 缓存 |

---

## 3. Redis 部署建议

### 3.1 拓扑
- 起步：**单节点 Redis 7.x**（2-4GB 内存足够，key 量级 ≤ 10^5）；
- 控制台分机后建议**主从（1 主 1 从，哨兵或 Redis Cluster 视规模）**，
  主写从读，故障自动切换；
- 与 RDS 同 VPC/同内网，避免跨公网访问。

### 3.2 数据结构清单汇总

| 用途 | Key 模式 | 类型 | TTL | 说明 |
|---|---|---|---|---|
| 分布式锁 | `lock:<resource>` | String(NX PX) | 30s | 事件/任务/槽位/sweep/IM 选主 |
| OAuth state | `oauth:state:<state>` | String | 600s | GitCode/Feishu 扫码 |
| 会话缓存 | `sess:<token>` | String | 对齐 session | 校验回源+回填 |
| Runtime 状态 | `runtime:<id>` | Hash | 60s（心跳续期） | 控制台读 |
| 事件统计 | `stat:events:<status>` | String | 5-10s | 列表页徽章/计数 |
| Pub/Sub 推送 | `channel:events` | Pub/Sub | - | SSE/进度广播 |
| Webhook 去重 | `webhook:dedup:<id>` | String(SETNX) | 300s | 幂等 |
| 元数据缓存 | `meta:repo:<key>` | String | 5min | 凭据/仓库 |

### 3.3 可靠性要点
- **持久化**：AOF（appendfsync everysec）+ RDB 快照；锁/去重类可容忍丢，会话/状态类
  丢失会回源 DB（设计上**Redis 只做加速与协调，DB 仍是唯一事实源**）；
- **淘汰策略**：`allkeys-lru`（缓存类可淘汰）；锁/去重 key 带 TTL 不受影响；
- **降级**：Redis 不可用时全部路径**回源 DB + 退化为进程内锁**（保持单机语义），
  通过 `redis_client` 封装统一 try/except，禁止 Redis 故障导致主链路不可用。

---

## 4. 实施路线图

| 阶段 | 内容 | 交付物 |
|---|---|---|
| 阶段 1（P0） | ① Redis 客户端封装（连接池+降级）② 分布式锁工具 ③ OAuth state 迁 Redis ④ sweep/IM 选主 | 多机部署「能跑、不错乱」 |
| 阶段 2（P1） | ⑤ 会话缓存 ⑥ runtime 状态/事件统计缓存 ⑦ webhook 去重 ⑧ 元数据缓存 | 控制台轮询不再压 RDS，响应提升 |
| 阶段 3（P1+） | ⑨ SSE/进度推送 Pub/Sub 广播 | 对话/进度实时性 |
| 后续（P2） | 静态资源 CDN、worker 包对象存储、Redis Cluster 扩容 | 规模化 |

**建议阶段 1 与「agent 分机部署」同步实施**（分机即引入，避免先乱后改）。

---

## 5. 风险与权衡

1. **Redis 本身单点**：锁/状态类 key 丢失 → 锁提前释放导致并发（可用 PX 短 + 业务幂等
   兜底）；主从切换瞬间写丢失 → 会话/去重类可回源，业务可接受。
2. **双写一致性**：缓存与 DB 采用 **Cache-Aside（旁路缓存）**：读 miss 回源 DB 回填，
   写路径先 DB 后 DEL 缓存（或延迟双删），避免强一致承诺。
3. **复杂度上升**：每引入一个 Redis 依赖都要评估「是否必须」——
   P2 清单明确不做明细缓存，控制复杂度。
4. **替代方案**（暂不引入 Redis 的过渡）：DB 行级锁（`SELECT ... FOR UPDATE`）、
   文件锁（单机）、`GET_LOCK()`（MySQL 命名锁，多实例互斥但加重 RDS）；
   Redis 是**一致性 + 性能**兼顾的最优解，且本项目状态模型（DB 为事实源）天然适配
   旁路缓存模式，迁移成本低。

---

## 6. 结论

多机部署（agent 分机器 + 控制台分机器）后，**必须引入 Redis 的共 4 处**（P0）：
分布式锁、OAuth state、定时任务/IM 选主；**建议引入 5 处**（P1）：会话缓存、
runtime 状态/事件统计、SSE 广播、webhook 去重、元数据缓存。
设计原则：**DB 是唯一事实源，Redis 只做加速与协调，所有路径可降级回源**。
建议阶段 1（P0）与 agent 分机部署同步落地，阶段 2/3（P1）随控制台分机与压测推进。
