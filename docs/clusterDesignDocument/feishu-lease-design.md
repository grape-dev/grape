# 飞书 Bot Lease 机制改造方案

## 背景

集群部署后两台实例同时连同一批飞书 WebSocket，重复收消息重复回复。需要用 MySQL 行级 CAS 保证同一个 bot 同一时刻只有一个实例连 WebSocket。参照 multica 的 Supervisor + LeaseStore 模式。

## 一、DB 变更

global_im_bot_config 表新增 3 个字段：

```sql
ALTER TABLE global_im_bot_config
  ADD COLUMN lease_token VARCHAR(128) DEFAULT NULL COMMENT 'lease 锁令牌',
  ADD COLUMN lease_expires_at DATETIME DEFAULT NULL COMMENT 'lease 过期时间（UTC）',
  ADD COLUMN lease_owner VARCHAR(128) DEFAULT NULL COMMENT '持有 lease 的实例 ID';
```

DDL 同步修改（新库建表时包含 3 个字段）。

字段类型用 DATETIME 而非 TIMESTAMP——避免 MySQL TIMESTAMP 自动转 UTC 的时区坑。统一存 UTC 时间。

### CAS SQL

抢锁/续约（原子）：
```sql
UPDATE global_im_bot_config
SET lease_token = ?, lease_expires_at = DATE_ADD(UTC_TIMESTAMP(), INTERVAL ? SECOND), lease_owner = ?
WHERE bot_app_id = ? AND is_active = 1
  AND (lease_token IS NULL OR lease_expires_at < UTC_TIMESTAMP() OR lease_token = ?);
-- affected_rows=1 = 抢到/续约成功
```

释放：
```sql
UPDATE global_im_bot_config
SET lease_token = NULL, lease_expires_at = NULL, lease_owner = NULL
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

BOT_LEASE_DISABLED=true 时退回原有模式（无条件启动所有 bot），不启动 sweep 循环。用于单实例部署或调试。

默认 false（lease 默认开启）——性能开销可忽略（约 5ms/10 秒，CPU 0.05%）。

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
| 1 | src/api/db_setup.py | global_im_bot_config DDL + migration 加 lease_token / lease_expires_at / lease_owner |
| 2 | src/im/bot_lease.py（新增） | BotLeaseStore：try_acquire / renew / release / list_expired_or_unheld |
| 3 | src/im/feishu_async.py | BotManager 改造：load_eligible_bots + lease_sweep_loop + run_bot 加续约 + stop 释放 lease |
| 4 | src/api/main.py | lifespan 改造：创建 lease_store + 启动 sweep_task + 关闭时 cancel + 释放 |
| 5 | src/config.py | 新增 4 个环境变量读取 |

## 九、不需要改动

- bot_seen_messages 去重保留（额外保险）
- nginx 不影响飞书 WebSocket（不经过 nginx）
- worker 侧不受影响
- 飞书 API 调用不受影响（任何实例都能 reply，用 tenant_token）

## 十、上线顺序

**重要：两台必须同时部署 lease 代码再同时启用。**

如果只有一台部署了 lease（BOT_LEASE_DISABLED=false），另一台没有 lease 代码——没有 lease 的那台会无条件连所有 bot，导致重复。

正确顺序：
1. 两台都部署 lease 代码 + BOT_LEASE_DISABLED=true（先不启用）
2. 验证两台都退回旧模式正常工作
3. 两台同时改 BOT_LEASE_DISABLED=false 重启
4. 验证只有一个实例连每个 bot

## 十一、数据库新增字段

| # | 表.字段 | 类型 | 默认值 | 用途 |
|---|---------|------|--------|------|
| 1 | global_im_bot_config.lease_token | VARCHAR(128) | NULL | lease 锁令牌 |
| 2 | global_im_bot_config.lease_expires_at | DATETIME | NULL | lease 过期时间（UTC） |
| 3 | global_im_bot_config.lease_owner | VARCHAR(128) | NULL | 持有 lease 的实例 ID |
