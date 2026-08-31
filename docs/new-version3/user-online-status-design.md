# 工作台用户登录状态方案（最终版）

> 分支：`feat/admin-style-optimization`

## 需求

工作台用户页面展示用户当前登录状态（在线/离线），支持按状态筛选。

## 设计

### 原理

grape 主服务每次 API 请求都会调 `get_current_user` 验证 JWT token。在这个函数里把用户 id 加到内存 set，后台定时任务每 30 秒批量 UPDATE `user_accounts.last_active_at`。admin 后台查询时判断 `last_active_at` 在 30 分钟内 = 在线，否则 = 离线。

### 数据流

```
用户请求 API
  → get_current_user 验证 JWT
  → _pending_active.add(uid)     ← O(1)，不阻塞
  → 返回用户信息

后台定时任务（每 30 秒）
  → 取出 _pending_active 所有 uid
  → 清空 _pending_active
  → 分批 UPDATE（每批 1000）
  → UPDATE user_accounts SET last_active_at=NOW() WHERE id IN (1,2,3...)

admin 后台查询
  → SELECT id, username, ..., last_active_at FROM user_accounts
  → last_active_at >= NOW()-1800s → 在线
  → last_active_at < NOW()-1800s 或 NULL → 离线
```

### 集群并发

- 每个实例各自维护 `_pending_active` set + 定时 flush
- 同一个 uid 可能被多实例 add → DB 重复 UPDATE
- `last_active_at=NOW()` 幂等——多次写结果一样，无冲突
- 3 个实例 30 秒内最多 3 次批量 UPDATE，MySQL 无压力

### 性能

| 指标 | 18 用户 | 10万用户（1万在线） |
|------|---------|-------------------|
| 内存（set） | 72 字节 | 40KB |
| DB 写频率 | 30秒1次 | 30秒10批×1000 |
| DB 写耗时 | <1ms | ~100ms |
| API 响应影响 | 零（set.add O(1)） | 零 |
| 查询（有索引） | <1ms | 1-5ms |

## 改动点

### 1. 数据库——加字段 + 索引

```sql
ALTER TABLE user_accounts ADD COLUMN last_active_at DATETIME DEFAULT NULL COMMENT '最后活跃时间';
ALTER TABLE user_accounts ADD INDEX idx_last_active (last_active_at);
```

### 2. grape 主服务——deps.py

```python
import time
from collections import Set

_pending_active: Set[int] = set()

def get_current_user(authorization: str = Header(None)) -> dict:
    ...
    # 原有 token 验证逻辑不变
    db_ver = user_db.get_token_version(uid)
    ...
    # 加入待更新集合——O(1)，不阻塞
    _pending_active.add(uid)
    return {"id": uid, ...}
```

### 3. grape 主服务——定时 flush 任务

```python
import asyncio

async def flush_active_users():
    """每 30 秒批量更新用户活跃时间"""
    if not _pending_active:
        return
    uids = list(_pending_active)
    _pending_active.clear()
    # 分批，每批 1000
    for i in range(0, len(uids), 1000):
        batch = uids[i:i+1000]
        placeholders = ','.join(['?'] * len(batch))
        user_db.execute(
            f"UPDATE user_accounts SET last_active_at=NOW() WHERE id IN ({placeholders})",
            batch
        )

# 在 app 启动时注册定时任务
async def active_user_tracker():
    while True:
        await asyncio.sleep(30)
        await flush_active_users()
```

### 4. grape 主服务——app.py 注册定时任务

```python
@app.on_event("startup")
async def startup_event():
    asyncio.create_task(active_user_tracker())
```

### 5. user_db.py——加批量更新方法

```python
def batch_update_last_active(uids: list):
    if not uids:
        return
    placeholders = ','.join(['?'] * len(uids))
    execute(f"UPDATE user_accounts SET last_active_at=NOW() WHERE id IN ({placeholders})", uids)
```

### 6. admin 后端——ConsoleUserController.java

列表 API 加 `last_active_at` 字段返回，前端判断在线/离线：

```java
// SQL 加 last_active_at 字段
"SELECT id, username, nickname, gitcode_id, created_at, last_active_at, ..."

// 返回时判断在线状态
String lastActive = rs.getString("last_active_at");
boolean online = false;
if (lastActive != null) {
    // 30 分钟内 = 在线
    online = java.time.Duration.between(
        java.time.LocalDateTime.parse(lastActive.substring(0, 19).replace(' ', 'T')),
        java.time.LocalDateTime.now()
    ).toMinutes() < 30;
}
u.put("lastActiveAt", lastActive);
u.put("online", online);
```

### 7. admin 前端——ConsoleUsers.vue

统计卡改为：用户总数 / 在线 / 离线 / GitCode 登录 / 其他

徽章行改为：全部 / 在线 / 离线

表格加"登录状态"列

后端 stats 加 onlineCount / offlineCount

## 改动文件

| # | 文件 | 改动 |
|---|------|------|
| 1 | 数据库 user_accounts | 加 last_active_at 字段 + 索引 |
| 2 | src/api/deps.py | _pending_active.add(uid) |
| 3 | src/api/main.py | 注册定时 flush 任务 |
| 4 | src/api/user_db.py | batch_update_last_active 方法 |
| 5 | ConsoleUserController.java | SQL 加 last_active_at + 返回 online 字段 + stats 加在线/离线计数 |
| 6 | ConsoleUsers.vue | 统计卡/徽章行/表格列改为在线/离线 |

## 不改的

- JWT token 机制
- 登录/登出逻辑
- 其他 API
- 其他页面
