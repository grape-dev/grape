# 飞书 Bot Lease 机制改造方案

## 背景

集群部署后两台实例同时连同一批飞书 WebSocket，重复收消息重复回复。需要用 MySQL 行级 CAS 保证同一个 bot 同一时刻只有一个实例连 WebSocket。参照 multica 的 Supervisor + LeaseStore 模式。

## 一、DB 变更

global_im_bot_config 表新增 5 个字段 + 修改 1 个字段类型：

```sql
-- 新增字段
ALTER TABLE global_im_bot_config
  ADD COLUMN bot_name VARCHAR(128) DEFAULT '' COMMENT 'bot 名称',
  ADD COLUMN lease_token VARCHAR(128) DEFAULT NULL COMMENT 'lease 锁令牌',
  ADD COLUMN lease_expires_at DATETIME DEFAULT NULL COMMENT 'lease 过期时间（UTC）',
  ADD COLUMN lease_owner VARCHAR(128) DEFAULT NULL COMMENT '持有 lease 的实例 ID',
  ADD COLUMN lease_acquired_at DATETIME DEFAULT NULL COMMENT 'lease 获取时间（UTC）',
  ADD COLUMN updated_at DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP COMMENT '更新时间';

-- 修改字段类型
ALTER TABLE global_im_bot_config
  MODIFY COLUMN created_at DATETIME DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间';
```

DDL 同步修改（新库建表时包含所有字段）。

字段类型用 DATETIME 而非 TIMESTAMP——避免 MySQL TIMESTAMP 自动转 UTC 的时区坑。统一存 UTC 时间。

### CAS SQL

抢锁/续约（原子）：
```sql
UPDATE global_im_bot_config
SET lease_token = ?, lease_expires_at = DATE_ADD(UTC_TIMESTAMP(), INTERVAL ? SECOND),
    lease_owner = ?, lease_acquired_at = UTC_TIMESTAMP()
WHERE bot_app_id = ? AND is_active = 1
  AND (lease_token IS NULL OR lease_expires_at < UTC_TIMESTAMP() OR lease_token = ?);
-- affected_rows=1 = 抢到/续约成功
```

注意：续约时不更新 lease_acquired_at（保持首次抢锁时间），只更新 lease_expires_at。

释放：
```sql
UPDATE global_im_bot_config
SET lease_token = NULL, lease_expires_at = NULL, lease_owner = NULL, lease_acquired_at = NULL
WHERE bot_app_id = ? AND lease_token = ?;
```

查询可抢的 bot：
```sql
SELECT bot_app_id FROM global_im_bot_config
WHERE is_active = 1
  AND (lease_token IS NULL OR lease_expires_at < UTC_TIMESTAMP());
```

## 二、新增模块 src/im/bot_lease.py

```python
class BotLeaseStore:
    def __init__(self, instance_id, ttl=60, renew_interval=20, sweep_interval=10)

    def try_acquire(self, app_id, token) -> bool    # CAS 抢锁
    def renew(self, app_id, token) -> bool           # 续约（同 CAS SQL）
    def release(self, app_id, token)                 # 释放
    def list_expired_or_unheld(self) -> list[str]    # 查可抢的 bot
```

- instance_id = hostname + "-" + secrets.token_hex(4)，进程启动生成一次
- token = secrets.token_hex(16)，每次 try_acquire 生成新 token

## 三、改造 BotManager（src/im/feishu_async.py）

### 当前问题

`load_from_db()` 查 DB 后直接 `add_bot()` 连 WebSocket——无条件连接。两个实例同时启动，同一个 bot 被两个实例同时连。

### 改造

**load_from_db() → load_eligible_bots()**

当前：查 DB → 直接 add_bot 连 WebSocket
改后：查 DB → 只登记 bot 配置（state=IDLE）不连接

原因：连接由 lease sweep 决定——抢到锁的才连。如果无条件连接，两个实例重复连。

**新增 lease_sweep_loop()**

每 10 秒扫描：
- IDLE bot → try_acquire → 抢到则 add_bot 连 WebSocket
- CONNECTED bot → 检查续约状态 → 续约失败则 remove_bot 断开

原因：需要后台循环不断扫描——发现可以接管的 bot（lease 过期或无人持有）。没有这个循环，实例 A 挂了之后实例 B 不知道该接管。

注意：try_acquire 成功后如果 WebSocket 连接失败（网络问题）需要释放 lease——否则其他实例抢不到。

**run_bot() 加续约协程**

每 20 秒续约一次——把 lease_expires_at 刷新为 now + TTL。

原因：如果不续约，lease 60 秒后过期，其他实例会抢走。续约告诉其他实例"我还活着"。

续约失败处理：
- 第 0 秒续约成功（lease 续到 60 秒）
- 第 20 秒续约失败（网络抖动）→ lease 还有 40 秒
- 第 40 秒续约重试成功 → 恢复
- 如果第 40 秒也失败 → 第 60 秒 lease 过期 → 10 秒后 sweep 抢走
- 续约有 2 次重试机会

**stop() 加释放 lease**

实例正常停止时释放所有 lease（lease_token 清空），让其他实例立即接管。

原因：如果不释放，其他实例要等 60 秒 lease 过期才能接管。释放后 10 秒内就能接管。

## 四、改造 lifespan（src/api/main.py）

### 当前

```python
bot_manager = BotManager(max_concurrent_per_bot=10)
await bot_manager.start()
await bot_manager.load_from_db()
```

### 改后

```python
lease_store = BotLeaseStore(instance_id=generate_instance_id())
bot_manager = BotManager(lease_store=lease_store, max_concurrent_per_bot=10)
await bot_manager.start()
await bot_manager.load_eligible_bots()
lease_task = asyncio.create_task(bot_manager.lease_sweep_loop())
```

关闭：
```python
lease_task.cancel()
await bot_manager.stop()  # 停 bot + 释放所有 lease
```

原因：需要创建 lease_store 传给 BotManager + 启动 sweep 后台任务 + 关闭时 cancel + 释放 lease。

## 五、单实例兼容

默认 false（lease 默认开启）——方式 A。

**方式 A：默认开启（BOT_LEASE_DISABLED=false）**
- 升级后自动启用 lease
- 单实例也能跑（无竞争，每次抢锁都成功）
- 不需要改配置
- 单实例多了 lease 开销（每 20 秒续约 + 每 10 秒 sweep）——性能消耗 < 0.05% CPU，可忽略
- 集群部署时不需要额外配置——自动生效

BOT_LEASE_DISABLED=true 时退回原有模式（无条件启动所有 bot），不启动 sweep 循环。用于调试场景。

## 六、参数

| 参数 | 默认值 | 环境变量 | 含义 |
|------|--------|---------|------|
| lease TTL | 60s | BOT_LEASE_TTL | lease 存活时间——实例挂了后 lease 多久过期 |
| 续约间隔 | 20s | BOT_LEASE_RENEW_INTERVAL | 持有 lease 的实例多久续约一次——告诉其他实例"还活着" |
| sweep 间隔 | 10s | BOT_LEASE_SWEEP_INTERVAL | 多久扫描一次发现可抢的 bot——决定发现过期 lease 的延迟 |
| 禁用 lease | false | BOT_LEASE_DISABLED | true 退回旧模式（无条件连所有 bot） |

### 参数关系

- TTL > RENEW_INTERVAL × 2（保证续约失败后还有时间重试）
- RENEW_INTERVAL < TTL（续约在过期前）
- SWEEP_INTERVAL 决定故障转移的额外延迟

### 故障转移时序

```
实例 A 挂了（停止续约）
→ 0 秒：lease 还有效
→ 最多 60 秒：lease 过期（TTL）
→ 最多 10 秒：实例 B 下次 sweep 发现并抢锁（SWEEP_INTERVAL）
→ 抢锁成功 → 连 WebSocket → 恢复
→ 最慢无响应 = 60 + 10 = 70 秒
```

70 秒无响应对飞书消息可接受——用户发消息后最多等 1 分钟多就有回复。

### 性能消耗

| 开销 | 频率 | 耗时 | CPU 占比 |
|------|------|------|---------|
| 续约 | 每 20 秒 | 2.24ms（8 bot） | 0.01% |
| sweep | 每 10 秒 | 2.41ms | 0.02% |
| DB 查询 | 4-5 次/30秒 | — | — |
| **合计** | **约 5ms/10秒** | **< 0.05%** | |

对比：Worker claim 每 3 秒查 1 次 DB（10 次/30秒），JWT token_version 每次 API 请求查 1 次 DB。lease 的 DB 开销最小。

## 七、src/config.py 新增环境变量

```python
BOT_LEASE_DISABLED = os.environ.get("BOT_LEASE_DISABLED", "false").lower() == "true"
BOT_LEASE_TTL = int(os.environ.get("BOT_LEASE_TTL", "60"))
BOT_LEASE_RENEW_INTERVAL = int(os.environ.get("BOT_LEASE_RENEW_INTERVAL", "20"))
BOT_LEASE_SWEEP_INTERVAL = int(os.environ.get("BOT_LEASE_SWEEP_INTERVAL", "10"))
```

## 八、改动文件清单

| # | 文件 | 改动内容 |
|---|------|---------|
| 1 | src/api/db_setup.py | global_im_bot_config DDL 加 6 个字段 + migration ALTER TABLE + created_at 改 DATETIME |
| 2 | src/im/bot_lease.py（新增） | BotLeaseStore：try_acquire / renew / release / list_expired_or_unheld，CAS SQL 含 lease_acquired_at |
| 3 | src/im/feishu_async.py | load_eligible_bots 查询加 bot_name + _disable_bot 释放 lease + BotManager 改造 + run_bot 加续约 |
| 4 | src/api/main.py | lifespan 改造：创建 lease_store + 启动 sweep_task + 关闭时 cancel + 释放 |
| 5 | src/config.py | 新增 4 个环境变量读取 |
| 6 | src/api/routes/chat.py | INSERT 加 bot_name 字段（先留空 ''） |
| 7 | src/api/routes/agents.py | INSERT 加 bot_name 字段（先留空 ''） |
| 8 | src/api/routes/feishu_onboard.py | INSERT 加 bot_name 字段（先留空 ''） |

### INSERT OR REPLACE 副作用说明

当前 3 个地方用 `INSERT OR REPLACE INTO global_im_bot_config (bot_app_id, bot_type) VALUES (?, ?)` 保存 bot 配置。INSERT OR REPLACE 的行为是：如果 bot_app_id 已存在，先删旧行再插新行。

换绑时（同一个 app_id 换 app_secret）→ 旧行被删（lease 字段清空）→ 新行 lease 全是 NULL → 正在连着的 bot 的 lease 突然没了 → 其他实例可能抢锁。

这个行为合理——换绑时断开重连没问题，不需要改。

### bot_name 说明

bot_name 字段先留空 ''。当前前端没有传 bot 名称，也没有展示 bot 列表的页面。后续如果需要可以补充。

## 九、遗漏修复（实施时必须处理）

### 1. sweep 抢锁后 WebSocket 连接失败要释放 lease

问题：lease_sweep_loop 里 try_acquire 成功 → add_bot 连 WebSocket。但如果连接失败（飞书 API 不可达、凭证错误），lease 已经被抢了但没有实际连上。其他实例看到 lease 被持有不会抢——这个 bot 60 秒内无人处理。

修复：try_acquire 成功 → add_bot → 如果连接失败 → release lease → 其他实例下次 sweep 可以抢。

### 2. 续约失败后要 release lease 再断开

问题：续约失败后如果直接 _running=False 断开，lease 可能还在 DB 里（续约失败不代表 lease 立即过期——还有 40 秒余量）。其他实例看到 lease 被持有不会抢。

修复：续约失败 2 次（40 秒）后 release lease + _running=False 断开。让其他实例立即接管，不用等 lease 自然过期。

### 3. _disable_bot 永久停用后要释放 lease

问题：_disable_bot 把 is_active=0 永久停用 bot。如果这个 bot 持有 lease——lease 没被释放，其他实例看到 is_active=0 不会抢，但 lease 还在 DB 里——脏数据。

修复：_disable_bot 时同时释放 lease（lease_token=NULL, lease_expires_at=NULL, lease_owner=NULL）。

### 4. load_eligible_bots 时确保所有 bot 登记到 global_im_bot_config

问题：load_from_db 从 3 张表查 bot 配置（chat_agent_session / global_im_bot_config / devops_agent_session）。如果某个用户在 chat_agent_session 配了新 bot 但没在 global_im_bot_config 登记——这个 bot 的 lease 字段查不到，lease 管不到。

当前数据没这个问题（所有 bot 都登记了），但代码上需要防御。

修复：load_eligible_bots 从 3 张表查到所有 bot 配置后，如果某个 app_id 不在 global_im_bot_config 里，INSERT 登记一条（bot_app_id + bot_type + is_active=1），确保 lease 字段一定有地方存。

## 十、待优化（实施时一并处理）

### 1. sweep 批量查询 + 游标分页

当前 sweep 每次循环对每个 bot 单独查询 + 单独续约（7 bot = 十几次 DB 查询）。

优化：批量查询 + 游标分页（cursor-based pagination）。

**查询——游标分页：**
```sql
SELECT bot_app_id, is_active, lease_token, lease_expires_at, lease_owner
FROM global_im_bot_config
WHERE is_active = 1
  AND (lease_token IS NULL OR lease_expires_at < UTC_TIMESTAMP())
  AND bot_app_id > ?    -- 上一次处理的最后一个 bot_app_id
ORDER BY bot_app_id
LIMIT 500
```

- bot_app_id 是主键 → WHERE bot_app_id > ? 走索引 → O(1) 定位
- 不漏数据 → 游标推进直到查完
- 不重叠 → 同一轮 sweep 内处理完
- 每批 500 条 → 内存不爆
- 性能恒定 → 不随数据量增长变慢

**续约——批量 UPDATE：**
```sql
UPDATE global_im_bot_config
SET lease_expires_at = DATE_ADD(UTC_TIMESTAMP(), INTERVAL ? SECOND)
WHERE lease_owner = ? AND lease_token = ? AND is_active = 1
```
一条 SQL 更新本实例持有的所有 lease。

**实现逻辑：**
```python
async def lease_sweep_loop():
    while True:
        cursor = ''  # 每轮 sweep 从头开始
        while True:
            bots = list_expired_or_unheld(cursor=cursor, limit=500)
            if not bots:
                break
            for bot in bots:
                try_acquire(bot.app_id)
            cursor = bots[-1].app_id  # 游标推进
            if len(bots) < 500:
                break  # 没有更多了
        # 批量续约
        batch_renew()
        # 检查 is_active=0 的 bot 断开
        check_inactive_bots()
        await asyncio.sleep(sweep_interval)
```

**优化前后对比：**

| 指标 | 优化前 | 优化后 |
|------|--------|--------|
| DB 查询次数 | 十几次（循环单行） | 2 次（1 查 + 1 批量续约） |
| 耗时 | 2.57ms | 0.49ms |
| CPU | 0.026% | 0.005% |
| 数据量扩展 | 线性增长 | 恒定（游标分页） |

### 2. health_check 加 lease 状态

当前 health_check 返回 bot 的 state/metrics。集群后需要知道每个 bot 的 lease 状态（谁持有、何时过期）。

优化：health_check 加 lease_owner / lease_expires_at。

### 3. 热加载 lease 感知

当前 add_bot / remove_bot 支持运行时热加载。lease 模式下：
- add_bot 不应该直接连——应该先登记 IDLE，由 sweep 决定是否连
- remove_bot 应该同时释放 lease

### 4. try_acquire 竞争失败打 info 日志

两个实例同时 sweep 同时 try_acquire 同一个 bot——CAS 保证只有一个成功。失败的那个打 info 日志（不是 warning，正常竞争不是错误；也不是静默，静默不合理无法排查）。

日志格式：`[lease] bot {app_id} acquire failed, held by {lease_owner}`

## 十一、全链路场景分析（新发现问题）

### 问题 A：WebSocket 重连退避超过 TTL 导致 lease 过期

场景：WebSocket 断开 → run_bot 指数退避重连（5s → 10s → 20s → 40s → 80s → ...）。重连到第 5 次 80 秒 > TTL 60 秒 → lease 过期 → 其他实例抢 → 实例 A 重连成功但 lease 被抢 → 两个实例同时连。

修复：重连成功后检查 lease 是否还在自己手里——如果不在就主动断开。续约走 DB 不依赖 WebSocket，重连期间保持续约——只要 DB 没断 lease 就不会过期。

### 问题 B：运行时新增 bot 不会被发现

场景：用户新增 bot 配置 → chat_agent_session 插入新行 → 但 load_eligible_bots 只在启动时调一次 → 运行时新增的 bot 不会被 sweep 发现。

修复：sweep_loop 里定期重新加载 bot 配置（每 5 分钟调一次 load_eligible_bots），发现新 bot 登记 IDLE 并纳入 lease 管理。

### 问题 C：正在处理消息时 lease 被抢导致消息中断

场景：实例 A 正在处理耗时消息（如 VOD 分析）→ 续约失败 → lease 被实例 B 抢 → 实例 A 断开 → stop() 强制 cancel timeout=10 → 正在处理的消息被中断 → 用户收不到回复。

修复：续约失败后给宽限期（30 秒）等正在处理的消息完成再断开。宽限期内 lease 已释放，其他实例可以抢 WebSocket——但实例 A 的 WebSocket 还连着会重复收消息，bot_seen_messages 去重兜底。

### 问题 D：DB 断连时不应立即断开 bot

场景：DB 连接断 → 续约失败 2 次 → release lease + 断开 → 所有 bot 断开。但 DB 断了应该等恢复，不是断 bot。

修复：区分续约失败原因：
- DB 错误（连接断/查询超时）→ 保持 WebSocket 连接，等 DB 恢复后重试续约
- CAS 失败（lease 被其他实例抢走）→ release lease + 断开

### 场景验证总结（68 个场景）

| # | 场景 | 结果 | 说明 |
|---|------|:---:|------|
| 1 | 正常消息处理 | ✅ | 只有持有 lease 的实例收消息，reply 走 API 不依赖 WebSocket |
| 2 | 实例挂了实例 B 接管 | ✅ | 70 秒恢复，过渡期消息靠飞书重试 |
| 3 | 处理消息时 lease 被抢 | ⚠️ | 问题 C——宽限期 30 秒等处理完 |
| 4 | 两个实例同时启动 | ✅ | CAS 保证只有一个成功 |
| 5 | 正常重启 | ✅ | stop 释放 lease，10 秒内接管 |
| 6 | DB 连接断了 | ⚠️ | 问题 D——DB 错误保持连接等恢复 |
| 7 | 飞书 API 不可达 | ✅ | 连接失败 release lease 已处理 |
| 8 | WebSocket 断开重连 | ⚠️ | 问题 A——重连后检查 lease 是否还在 |
| 9 | 运行时新增 bot | ⚠️ | 问题 B——sweep 定期重新加载配置 |
| 10 | kill -9 | ✅ | lease 自然过期，70 秒接管 |
| 11 | tenant_token 缓存 | ✅ | 各自获取不冲突 |
| 12 | VOD bot lease | ✅ | 管 WebSocket 不关 repo |
| 13 | enqueue_task 下发任务到 Worker | ✅ | lease 管 WebSocket 不影响任务下发 |
| 14 | 任务回调飞书回复 | ✅ | consume_stream_async 在 bot 实例同步执行，不跨实例回复 |
| 15 | seen_msg_ids 跨实例共享 | ✅ | 共享 MySQL，毫秒级窗口重复概率极低可接受 |
| 16 | 飞书 ack 机制 | ✅ | WebSocket 层面，只有持有 bot 的实例 ack |
| 17 | PING/PONG 保活 | ✅ | 续约走 DB 不依赖 WebSocket，重连后检查 lease |
| 18 | semaphore 限流 | ✅ | lease 切换新建 BotContext，宽限期等处理完 |
| 19 | _periodic_cleanup | ✅ | DB DELETE 共享 MySQL |
| 20 | bot 配置删除 | ✅ | sweep 5 分钟内重新加载 |
| 21 | 多用户同 app_id 去重 | ✅ | 按 app_id 去重 + lease 按 app_id 管理 |
| 22 | lifespan 关闭顺序 | ⚠️ | 问题 H——先 cancel lease_task 再 stop bot_manager |
| 23 | stop timeout 和宽限期冲突 | ⚠️ | 问题 I——正常重启增大 timeout，lease 被抢用独立断开逻辑 |
| 24 | sweeper 和 lease sweep 命名 | ⚠️ | 问题 J——变量名用 _lease_sweep_task |
| 25 | bot_manager 启动失败 | ✅ | lease_store 创建在 try 块里，失败整体失败 |
| 26 | webhook 和飞书 IM 关系 | ✅ | webhook 不直接调飞书 reply，不依赖 WebSocket |
| 27 | 跨实例配置变更热加载 | ⚠️ | 问题 K——remove_bot 跨实例无效，sweep 检查 is_active=0 立即断开 |
| 28 | lease 状态变更日志 | ⚠️ | 问题 L——续约/release 打 info 日志 |
| 29 | lease 异常监控 | ⚠️ | 问题 N——health_check 加 lease 切换次数 |
| 30 | sweep 和 add_bot/remove_bot 竞态 | ✅ | CAS 原子操作 |
| 31 | sweep 和续约竞态 | ✅ | WHERE 条件保证 |
| 32 | 两实例同时 sweep+续约 | ✅ | CAS + WHERE lease_owner 各自隔离 |
| 33 | bot_app_id 为空或异常 | ✅ | 当前代码 if not bot_app_id: continue |
| 34 | lease_token 冲突 | ✅ | secrets.token_hex(16) 冲突概率 1/2^128 |
| 35 | DB 字段缺失 | ⚠️ | 问题 M——migration 失败退回旧模式 |
| 36 | instance_id 重复 | ✅ | lease_token 区分，不会误续约 |
| 37 | 回滚 | ✅ | 旧代码不读 lease 字段，不影响 |
| 38 | migration 失败 | ⚠️ | 问题 M——退回旧模式 |
| 39 | nginx 配置变更 | ✅ | lease 自动适应 |
| 40 | lease_token 安全性 | ✅ | 只是锁令牌不是用户凭证 |
| 41 | SQL 注入 | ✅ | 参数化查询 |
| 42 | DB 查询阻塞 asyncio | ✅ | 0.73ms/10s，和现有代码同模式 |
| 43 | BotContext 内存占用 | ✅ | 700KB/7bot，lease 切换时清理 |
| 44 | sweep_loop 异常退出 | ⚠️ | 问题 O——加 try-except 保护 |
| 45 | _seen_msg_ids lease 切换后清理 | ✅ | 共享 DB，新实例重新加载 |
| 46 | lease_expires_at 时区 | ✅ | UTC_TIMESTAMP() 时区无关 |
| 47 | 续约时间精度 | ✅ | 1 秒误差 << 20 秒续约间隔 |
| 48 | chat_bot 和 vod_bot lease 差异 | ✅ | 按 app_id 管理，不关 bot_type |
| 49 | bot_type 为空或异常 | ✅ | 不匹配走 chat 默认逻辑 |
| 50 | lease 负载不均衡 | ⚠️ | 问题 P——当前不管，后续按需加 max_bots_per_instance |
| 51 | lease 查询缺少索引 | ⚠️ | 问题 Q——当前 7 行不需要，后续加 idx_lease_owner + idx_is_active |
| 52 | lease 测试缺失 | ⚠️ | 问题 R——新增 test_bot_lease.py |
| 53 | lease 状态 API 暴露 | ⚠️ | 问题 S——加 GET /api/im/status 路由 |
| 54 | 飞书 API 限流 | ✅ | token 缓存 2 小时，频率极低 |
| 55 | 飞书 WebSocket 连接数限制 | ✅ | lease 保证唯一连接 |
| 56 | 网络分区 split-brain | ✅ | 单 MySQL 实例不存在分区 |
| 57 | 时钟漂移 | ✅ | NTP 保证，10 秒 << TTL 60 秒 |
| 58 | SIGTERM vs SIGKILL | ⚠️ | 问题 T——uvicorn grace period 冲突 |
| 59 | 配置热更新 | ✅ | 需重启是正常行为 |
| 60 | 日志量和轮转 | ✅ | 1.7-4.3 MB/天，日志轮转处理 |
| 61 | instance_id 重启后变化 | ✅ | 旧 lease 60 秒过期，新实例 CAS 成功 |
| 62 | load_eligible_bots 部分失败 | ✅ | DB 查询原子，要么全成功要么全失败 |
| 63 | DB 连接池耗尽 | ⚠️ | 问题 U——后续优化独立连接池 |
| 64 | onboarding 注册期间 lease | ✅ | 注册完才写 DB，sweep 不会提前发现 |
| 65 | 注册过程用户取消 | ✅ | bot 不创建，DB 无行，lease 不管 |
| 66 | 多租户 bot 管理 | ✅ | 按 app_id 管理，不区分 org |
| 67 | CAS UPDATE 行锁竞争 | ✅ | 0.24ms 锁持有极短 |
| 68 | ALTER TABLE 期间 lease 查询 | ✅ | migration 在 GET_LOCK 里，时序保证 |

### 问题 U：DB 连接池耗尽时 lease 查询无优先级

场景：API 高负载时 DB 连接池耗尽 → lease 查询拿不到连接 → 续约失败 → lease 过期 → 其他实例抢但也连不上 DB → 所有 bot 断开。

当前可接受——db_lock 共享连接，lease 查询 0.49ms 极快，不会长时间占用连接。后续优化：lease DB 查询用独立连接或优先级连接。

### 问题 T：uvicorn graceful shutdown timeout 和 stop() timeout 冲突

场景：uvicorn 默认 timeout_graceful_shutdown=5 秒，但 stop() 的 timeout=30 秒（问题 I 改的）→ uvicorn 5 秒后强制退出 → lease 没释放完。

修复：uvicorn 启动参数加 `--timeout-graceful-shutdown 30`，或 systemd 配置 `TimeoutStopSec=35`。确保 uvicorn grace period >= stop() timeout。

### 问题 P：lease 负载不均衡

场景：sweep 抢锁，谁先 sweep 谁抢到所有 bot → 一个实例扛所有 bot，另一个闲着。lease 只保证"不重复"不保证"均衡"。

当前 7 个 bot 消息量小，一个实例完全扛得住。lease 的目标是"不重复"不是"均衡"。后续如果消息量大再加 max_bots_per_instance 限制（环境变量 BOT_LEASE_MAX_BOTS 配置，7 bot / 2 实例 = 每个最多 4 个）。

### 问题 Q：lease 查询缺少索引

场景：lease 查询 WHERE is_active=1 AND (lease_token IS NULL OR lease_expires_at < UTC_TIMESTAMP()) 不走主键索引 → 全表扫描。当前 7 行 0.25ms 没问题，数据量增长后需要索引。

后续 bot 数量增长加索引：idx_lease_owner (lease_owner) + idx_is_active (is_active)。

### 问题 R：lease 测试缺失

场景：当前没有 lease 机制的测试。

修复：新增 test_bot_lease.py 单元测试：
- try_acquire 成功/失败
- renew 成功/失败
- release
- list_expired_or_unheld
- 游标分页
- 批量续约

集成测试：
- 两个实例抢同一个 bot
- lease 过期后被其他实例抢走
- 续约失败后断开

### 问题 S：lease 状态 API 暴露

场景：health_check 是 BotManager 的方法但没有 API 路由暴露 lease 状态。

修复：加 GET /api/im/status 路由，返回 health_check + lease 状态（lease_owner / lease_expires_at / lease_acquired_at）。

### 问题 L：lease 状态变更日志不完整

场景：try_acquire 成功/失败有日志，但续约成功/失败和 release 没有明确日志。

修复：每次 lease 状态变更都打 info 日志：
- 续约成功：`[lease] bot {app_id} renewed by {instance_id}`
- 续约失败：`[lease] bot {app_id} renew failed: {reason}`
- release：`[lease] bot {app_id} lease released by {instance_id}`

### 问题 M：migration 失败时 lease 代码防御

场景：ALTER TABLE 失败（字段已存在或 DB 权限问题）→ lease 字段不存在 → lease 代码报错。

修复：lease 代码加防御——查询时如果字段不存在（catch 异常），自动退回旧模式（BOT_LEASE_DISABLED=true），log warning 提示 migration 失败。

### 问题 N：lease 频繁切换监控

场景：lease 频繁切换（每分钟换一次）说明网络问题或实例不稳定，需要可观测。

修复：health_check 加 lease 统计：
- lease 切换次数（lease_acquired_at 变化次数）
- 当前每个 bot 的 lease_owner / lease_expires_at / lease_acquired_at
- 告警后续按需加

### 问题 O：lease_sweep_loop 异常退出没有保护

场景：lease_sweep_loop 如果抛异常（未 catch）→ task 静默退出 → lease 不再续约 → 所有 bot 最终断开。

修复：lease_sweep_loop 加 try-except 包住每轮 sweep——异常时 log error + 继续 sleep + 下一轮重试，不让一个异常杀死整个 sweep 循环。

```python
async def lease_sweep_loop():
    while True:
        try:
            # 游标分页查询 + try_acquire
            # 批量续约
            # 检查 is_active=0 断开
        except Exception as e:
            log.error(f"[lease] sweep error: {e}")
        await asyncio.sleep(sweep_interval)
```

### 问题 H：lifespan 关闭顺序

场景：实例关闭时先 cancel lease_task 还是先 stop bot_manager？如果先 stop bot_manager → stop 里释放 lease → 但 lease_task 还在跑可能又抢新锁。

修复：关闭顺序——先 cancel lease_task（不再抢新锁）→ 再 stop bot_manager（停 bot + 释放 lease）→ 其他清理。

### 问题 I：stop() timeout=10 和宽限期 30 秒冲突

场景：问题 C 说续约失败后给宽限期 30 秒等消息处理完。但 stop() 的 timeout=10 会强制 cancel——宽限期内被 stop 强制 cancel。

修复：区分两种场景：
- 正常 stop（实例重启）：增大 timeout 到 30 秒，等消息处理完
- lease 被抢（续约失败）：用独立的断开逻辑（宽限期 30 秒后断开），不走 stop()

### 问题 J：变量名混淆

场景：当前已有 _sweep_task（Issue workdir 清理），新增 lease sweep 也叫 sweep 容易混淆。

修复：lease sweep 变量名用 _lease_sweep_task，和 _sweep_task 区分。

### 问题 K：跨实例配置变更热加载

场景：用户在前端改飞书配置 → 请求打到 nginx → 随机分到实例 A → 实例 A 调 bot_manager.remove_bot + add_bot。但 bot 在实例 B → 实例 A remove_bot 无操作（没有 bot）→ 实例 B 的 bot 还在连。

三种场景的修复：

| 场景 | 机制 | 恢复时间 |
|------|------|:---:|
| 删除 bot | sweep 每 10 秒检查 is_active=0 或行被删 → 立即断开 + 释放 lease | 10 秒 |
| 新增 bot | add_bot 登记 IDLE → sweep try_acquire → 抢到就连 | 10 秒 |
| 换绑 bot（换 secret） | INSERT OR REPLACE 清空 lease → 实例 B 续约 CAS 失败立即断开 → 实例 A sweep 10 秒抢到用新 secret 连 | 10 秒 |

换绑场景关键：INSERT OR REPLACE 副作用清空 lease（前面确认合理），持有 bot 的实例下次续约发现 lease_token 不匹配（CAS 失败），按问题 D 修复方案立即断开。其他实例下次 sweep 抢到后用新 secret 连。不需要跨实例更新 BotContext。

## 十二、不需要改动

- bot_seen_messages 去重保留（额外保险）
- nginx 不影响飞书 WebSocket（不经过 nginx）
- worker 侧不受影响
- 飞书 API 调用不受影响（任何实例都能 reply，用 tenant_token）

## 十三、上线顺序

**按正常顺序逐台部署即可，不需要强制同时重启。**

### 部署过渡期处理

新旧代码混用期间（一台部署了 lease 代码，另一台还是旧代码）会有短暂重复——旧代码的实例无条件连所有 bot，新代码的实例 try_acquire 后也连上。重复消息靠 bot_seen_messages 去重兜底，不影响功能。

两台都部署完成后 lease 机制自动生效——只有抢到 lease 的实例连 bot。

### 部署步骤

1. 实例 A 部署 lease 代码 + 重启（BOT_LEASE_DISABLED=false 默认开启）
   - 实例 A 启用 lease，try_acquire 成功的 bot 连 WebSocket
   - 实例 B 还是旧代码，无条件连所有 bot
   - 过渡期：两台可能同时连同一个 bot，bot_seen_messages 去重兜底
2. 实例 B 部署 lease 代码 + 重启
   - 实例 B 启用 lease，try_acquire 时发现 lease 被实例 A 持有 → 跳过
   - lease 机制自动生效——只有抢到 lease 的实例连 bot
3. 验证：只有一个实例连每个 bot

### 不需要同时部署的原因

- lease 代码向下兼容——旧代码的实例不影响新代码运行
- 过渡期重复靠 bot_seen_messages 去重兜底
- 两台都部署完成后 lease 自动生效
- 无法保证集群同时重启——实际部署都是逐台滚动

## 十四、数据库新增字段

| # | 表.字段 | 类型 | 默认值 | 用途 |
|---|---------|------|--------|------|
| 1 | global_im_bot_config.bot_name | VARCHAR(128) | '' | bot 可读名称，日志/前端友好 |
| 2 | global_im_bot_config.lease_token | VARCHAR(128) | NULL | lease 锁令牌 |
| 3 | global_im_bot_config.lease_expires_at | DATETIME | NULL | lease 过期时间（UTC） |
| 4 | global_im_bot_config.lease_owner | VARCHAR(128) | NULL | 持有 lease 的实例 ID |
| 5 | global_im_bot_config.lease_acquired_at | DATETIME | NULL | lease 获取时间（UTC）——排查"lease 持有多久" |
| 6 | global_im_bot_config.updated_at | DATETIME | CURRENT_TIMESTAMP ON UPDATE | 更新时间 |

字段类型修改：
| 字段 | 原类型 | 新类型 | 原因 |
|------|--------|--------|------|
| created_at | text | DATETIME DEFAULT CURRENT_TIMESTAMP | text 不能做时间范围查询 |
