# Issue #345：bot token 单点故障风险 + 双主配置不一致

> 状态：方案设计
> 基线：weekly-07-clean（47391547c）
> 分支：feat/issue-345-bot-token-spo-failure
> 关联：#343 双轨 OAuth token 架构

## 一、问题

#343 把写操作（建单/关单/评论）从用户 OAuth token 切到 bot token 链路后：
- 所有写操作可用性集中在 bot token 一条腿上
- bot PAT 吊销时全平台静默瘫痪
- grape 是双主集群部署，global-bot-token.json 靠 scp 手工同步
- 评论/关单路径 401 降级提示不完整（建单已做，评论/关单未做）

## 二、部署架构确认

| 项 | 实际情况 | 证据 |
|---|---|---|
| 部署模式 | 双主集群 | docs/weekly-06/grape-platform-incident-fix-plan "双主需同步" |
| 集群实例数 | ≥2（文档提到 4 个 master 进程） | docs/weekly-07/cluster-seq-11130 "集群 4 个 master 进程" |
| 数据库 | 共享外部 MySQL | cluster-deployment-design.md "所有状态在 MySQL 共享数据库" |
| 配置分发 | 本地文件 + scp 手工同步 | 文档多处"双主 scp 同步配置一致" |
| 分布式锁 | MySQL GET_LOCK（已有先例） | cluster-deployment-design.md "GET_LOCK('grape_migrate', 300)" |
| Redis | 无 | cluster-deployment-design.md "不需要 Redis 也能多实例运行" |

## 三、修复范围（三个方向）

### 方向 3（P0）：写操作 401 降级提示——立即做

**问题**：建单路径已识别 401 并返回"Bot Token 可能失效"提示（issues_center.py line 1336-1340），但评论和关单路径未做。

**现状**：

| 路径 | 文件位置 | 异常类型 | 401 处理 |
|---|---|---|---|
| 建单-查重 | issues_center.py:1286 | RuntimeError | ✅ 已做 |
| 建单-创建 | issues_center.py:1320 | RuntimeError | ✅ 已做 |
| 评论 | issues.py:1263 _post_comment | RuntimeError | ❌ 只返回"GitCode 评论失败" |
| 关单 | issues.py:596 client.close_issue | dict {"error":"HTTP 401"} | ❌ 只返回"failed" |

**方案**：

1. 新增公共函数 `src/vcs/token_error.py`：

```python
import re

BOT_TOKEN_INVALID_MSG = "Bot Token 可能失效，请检查仓库 Bot Token 配置"

def extract_http_status_from_exc(exc: Exception) -> int | None:
    """从 RuntimeError 消息提取 HTTP status code。
    
    异常消息格式: "GitCode API ... failed (401): ..."
    """
    msg = str(exc)
    codes = re.findall(r'\((\d{3})\)', msg)
    return int(codes[0]) if codes else None

def extract_http_status_from_result(result: dict) -> int | None:
    """从 VcsApiClient 返回的 dict 提取 HTTP status code。
    
    格式: {"error": "HTTP 401", "detail": "..."}
    """
    err = str(result.get("error", ""))
    m = re.match(r'HTTP (\d{3})', err)
    return int(m.group(1)) if m else None

def is_auth_error_exc(exc: Exception) -> bool:
    """判断异常是否为 401 认证失败。"""
    return extract_http_status_from_exc(exc) == 401

def is_auth_error_result(result: dict) -> bool:
    """判断 VcsApiClient 返回值是否为 401 认证失败。"""
    if not isinstance(result, dict) or "error" not in result:
        return False
    return extract_http_status_from_result(result) == 401
```

2. 评论路径补 401 识别（issues.py `_post_comment` except 块）：

```python
# 现状（issues.py:1269）：
except Exception as e:
    raise HTTPException(status_code=502, detail={"code": 502,
                                                 "message": f"GitCode 评论失败: {str(e)[:200]}"})
# 改为：
except Exception as e:
    from src.vcs.token_error import is_auth_error_exc, BOT_TOKEN_INVALID_MSG
    if is_auth_error_exc(e):
        raise HTTPException(status_code=502, detail={"code": 502, "message": f"评论失败：{BOT_TOKEN_INVALID_MSG}"})
    raise HTTPException(status_code=502, detail={"code": 502, "message": f"GitCode 评论失败: {str(e)[:200]}"})
```

3. 关单路径补 401 识别（issues.py `_try_close_issue_for_repo` bot token 路径）：

```python
# 现状（issues.py:597）：
resp = client.close_issue(issue_number)
if isinstance(resp, dict) and "error" not in resp:
    return "success"
log.warning(f"confirm-done: close_issue ... → {resp}")
return "failed"
# 改为：
resp = client.close_issue(issue_number)
if isinstance(resp, dict) and "error" not in resp:
    return "success"
from src.vcs.token_error import is_auth_error_result
if is_auth_error_result(resp):
    log.warning(f"confirm-done: close_issue {repo_key}#{issue_number} → Bot Token 可能失效")
    return "bot_token_failed"
log.warning(f"confirm-done: close_issue ... → {resp}")
return "failed"
```

4. 建单路径已有 401 处理改为引用公共常量（issues_center.py line 1337-1339）：

```python
# 现状：
"message": "创建 Issue 失败：Bot Token 可能失效，请检查仓库 Bot Token 配置"
# 改为：
from src.vcs.token_error import BOT_TOKEN_INVALID_MSG
"message": f"创建 Issue 失败：{BOT_TOKEN_INVALID_MSG}"
```

**改动文件**：
- 新增 `src/vcs/token_error.py`
- 改 `src/api/routes/issues.py`（评论 + 关单）
- 改 `src/api/issues_center.py`（建单引用常量）
- 新增 `tests/test_token_error.py`

### 方向 2（P1）：全局 bot token 迁 DB meta 表

**问题**：global-bot-token.json 是本地文件，双主靠 scp 同步，不一致概率高。代码中 10+ 处硬编码路径读取。

**现状**：
- managed_repos.bot_gitcode_token 已在 DB（明文存储）
- meta 表已有 (key, value) 两列，get_meta_value/set_meta_value 已可用
- _get_global_bot_token() 读文件（src/user/db.py:1616）
- get_global_bot_credential() 读文件（src/user/db.py:1456）
- admin config_store.save_config() 写文件（src/admin/config_store.py:104）
- 10+ 处 os.path.join 硬编码路径散落在 repos.py/scenes.py/templates.py/db_setup.py/feishu_async.py

**方案**：

1. meta 表存储 key 设计：

```
key = 'global_bot_gitcode_token'     → value = gitcode_api_token 明文
key = 'global_bot_github_token'      → value = github_bot_token 明文
key = 'global_bot_github_username'   → value = github_bot_username 明文
key = 'global_bot_usernames'         → value = JSON 数组 '["bot1","bot2"]'
key = 'global_gitcode_webhook_token'  → value = gitcode_webhook_token
key = 'global_webhook_url'           → value = webhook_url
```

加密策略：明文存储，和 managed_repos.bot_gitcode_token 保持一致（已确认 DDL 为 `bot_gitcode_token TEXT DEFAULT ''`，无加密，读取时不解密）。当前选择明文，与现有字段一致；未来如统一加密，两处一起改（见 Issue #349）。

2. 统一读取入口（改 src/user/db.py）——DB only，60s TTL 缓存：

```python
_global_bot_cache: dict[str, tuple] = {}  # key → (value, expire_ts)
_GLOBAL_BOT_CACHE_TTL = 60

def _cached_get_meta(key: str) -> str:
    """带 TTL 缓存的 meta 读取。"""
    # 缓存命中 → 直接返回；未命中 → 查 DB 并缓存

def get_global_gitcode_bot_token() -> str:
    return _cached_get_meta("global_bot_gitcode_token")

def get_global_github_bot_token() -> str:
    return _cached_get_meta("global_bot_github_token")

def get_global_github_bot_username() -> str:
    return _cached_get_meta("global_bot_github_username")

def get_global_bot_usernames() -> list[str]:
    raw = _cached_get_meta("global_bot_usernames")
    # JSON 解析为 list

def _invalidate_global_bot_cache():
    """admin 改配置后清缓存（当前实例立即生效，其他实例最多 60s TTL 后生效）"""
    _global_bot_cache.clear()
```

3. 启动迁移逻辑（db_setup.py 启动时执行，幂等）：

**集群竞态处理**：N 个实例同时启动都检测到 DB 无值、文件有值，都执行迁移。用 src/lock.py 的 acquire_lock 保护迁移过程，且 set_meta_value 本身是 `INSERT OR REPLACE`（UPSERT 语义），多实例并发写同一值结果一致。

```python
def _migrate_global_bot_token_to_db():
    """启动时如果 DB 无值、文件有值，自动迁移到 DB。幂等。"""
    from src.lock import acquire_lock, release_lock
    if not acquire_lock("migrate_global_bot_token", ttl=60):
        return  # 其他实例正在迁移，跳过
    try:
        if get_meta_value("global_bot_gitcode_token"):
            return  # 已迁移
        # 读文件（迁移源，迁移后不再被运行时访问）
        # 写 DB（set_meta_value = INSERT OR REPLACE，UPSERT 语义）
    finally:
        release_lock("migrate_global_bot_token")
```

**文件保留**：global-bot-token.json 不删除，每次重启都检查迁移（DB 无值时从文件恢复）。

4. admin config_store.save_config() 只写 DB，不写文件：

```python
if bot_payload:
    # 写 DB meta（唯一权威源）
    for field, meta_key in _BOT_META_MAP.items():
        if field in bot_payload:
            set_meta_value(meta_key, ...)
    # 清除本实例缓存
    _invalidate_global_bot_cache()
```

5. 硬编码路径清理——所有读 global-bot-token.json 的地方改为调统一入口：

| 文件 | 改动 |
|---|---|
| src/user/db.py: _get_global_bot_token / get_global_bot_credential / get_github_token fallback / get_all_bot_usernames | 读文件 → DB 统一入口 |
| src/api/routes/repos.py: 6 处 GitCode token + 2 处 GitHub username | 直接读文件 → 统一入口 |
| src/api/routes/scenes.py: 2 处 | 直接读文件 → 统一入口 |
| src/api/routes/templates.py: 1 处 | 直接读文件 → 统一入口 |
| src/api/db_setup.py: 1 处 bot_username 迁移 | 读文件 → 统一入口 |
| src/im/feishu_async.py: 1 处 | 直接读文件 → 统一入口 |
- 改 `src/api/routes/scenes.py`（2 处）
- 改 `src/api/routes/templates.py`（1 处）
- 改 `src/im/feishu_async.py`（1 处）
- 新增 `tests/test_global_bot_token_db.py`

**迁移策略**：
- 启动时 DB 无值 + 文件有值 → 自动迁移到 DB（幂等）
- admin only writes DB (no transitional dual-write; DB is the sole authority from day one)
- global-bot-token.json preserved as read-only migration source and fallback

### 方向 1（P2）：定时探活 + 健康状态

**问题**：bot PAT 吊销后静默瘫痪，无主动发现机制。

**方案**：

1. 探活执行——复用 grape 已有 distributed_locks 表分布式锁（src/lock.py）：

**不使用 MySQL GET_LOCK**——GET_LOCK 的 timeout 参数是获取等待超时不是持有 TTL，连接池环境下锁语义不可靠，实例崩溃后锁不释放。

grape 已有 src/lock.py 基于 distributed_locks 表的分布式锁，有真正 TTL（expires_at 时间戳），实例崩溃后锁自动过期被其他实例抢占，已在 skill_sync/upload_cleanup/project_info/knowledge/db_setup 迁移等 5+ 处生产使用。

```python
# src/runtime/bot_token_health.py

async def bot_token_health_loop():
    """每 5 分钟探活全局 bot token，集群下只一个实例执行。"""
    while True:
        try:
            if acquire_lock("bot_token_health", ttl=240):
                try:
                    healthy = _verify_global_bot_token()
                    _update_health_status(healthy)
                finally:
                    release_lock("bot_token_health")
        except Exception as e:
            log.warning(f"bot_token_health loop error: {e}")
        await asyncio.sleep(300 + random.uniform(0, 30))
```

锁的 TTL=240s < 探活间隔=300s，确保探活完成前锁不释放，下次探活前锁已过期可供其他实例抢。
锁持有者崩溃后，expires_at 到期自动释放，其他实例可抢占，不会死锁。

def _verify_global_bot_token() -> dict:
    """调 GitCode /user 验证 token 有效性。"""
    from src.user.db import get_global_bot_token
    token = get_global_bot_token()
    if not token:
        return {"healthy": False, "error": "token not configured"}
    try:
        from src.user.repo_manager import _req
        _req("GET", "https://gitcode.com/api/v5/user", token)
        return {"healthy": True, "error": ""}
    except Exception as e:
        return {"healthy": False, "error": str(e)[:200]}

def _update_health_status(result: dict):
    """写 DB meta 表，所有实例共享读。"""
    from src.user.db import set_meta_value
    import time, json
    status = {
        "healthy": result["healthy"],
        "checked_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "error": result["error"],
    }
    set_meta_value("bot_token_health", json.dumps(status))
```

2. 连续失败阈值（3 次），失败计数按平台独立存 DB（集群下每次探活可能是不同实例）：

**设计变更（审查修正）**：聚合策略从"任一平台失败 → 整体不健康"改为三态 + 分平台明细。

原设计的问题：
- 单环境只配 GitCode token（GitHub 未配），探活报 config 失败 → 整体永远 unhealthy
- 失败计数全局合并，GitHub 连续失败 3 次拖累 GitCode 正常的整体判定
- admin 只看到拼接字符串，不能快速定位哪个平台、什么问题

修正后的聚合规则：

| GitHub | GitCode | overall | 含义 |
|---|---|---|---|
| healthy | healthy | healthy | 全部正常 |
| healthy | degraded/unhealthy | degraded | 部分可用 |
| degraded/unhealthy | healthy | degraded | 部分可用 |
| unhealthy | unhealthy | unhealthy | 全部不可用 |
| config (未配) | healthy | healthy | 跳过未配平台 |
| config (未配) | degraded/unhealthy | degraded | 已配平台部分故障 |

关键规则：
- **config 类错误不计入整体不健康**：平台未配 token 是正常的部署状态，不是故障。探活跳过未配 token 的平台，不探活、不计数、不纳入聚合。
- **失败计数按平台独立**：每个平台有自己的 consecutive_failures，互不影响。
- **WorstOf 聚合**：overall 取所有已配平台中最差的状态。degraded = 部分平台故障，unhealthy = 全部已配平台都故障。
- **分平台明细**：status JSON 包含 platforms 字段，每个平台独立展示 healthy/error_type/consecutive_failures/error。

```python
def _update_health_status(result: dict):
    # 读上次状态（从 DB meta 表）
    prev = _get_health_status()
    prev_platforms = (prev or {}).get("platforms", {})

    # 按平台独立计数
    platforms_out = {}
    for prov in ("gitcode", "github"):
        pr = result["platforms"].get(prov, {})
        if pr.get("error_type") == "config":
            continue  # 未配 token 的平台跳过，不计数、不纳入聚合
        prev_p = prev_platforms.get(prov, {})
        prev_failures = prev_p.get("consecutive_failures", 0) if not prev_p.get("healthy") else 0
        if pr["healthy"]:
            failures = 0
        else:
            failures = prev_failures + 1
        platforms_out[prov] = {
            "healthy": pr["healthy"],
            "degraded": not pr["healthy"] and failures < FAILURE_THRESHOLD,
            "unhealthy": not pr["healthy"] and failures >= FAILURE_THRESHOLD,
            "consecutive_failures": failures,
            "error_type": pr.get("error_type", ""),
            "error": pr.get("error", ""),
        }

    # WorstOf 聚合：全部已配平台都 unhealthy 才整体 unhealthy
    if not platforms_out:
        overall = "healthy"  # 没有平台配置 token
    elif all(p["healthy"] for p in platforms_out.values()):
        overall = "healthy"
    elif all(p["unhealthy"] for p in platforms_out.values()):
        overall = "unhealthy"  # 全部已配平台都达到 unhealthy 阈值
    else:
        overall = "degraded"  # 部分平台故障（含 healthy+unhealthy 混合）

    status = {
        "overall": overall,
        "platforms": platforms_out,
        "checked_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
    set_meta_value("bot_token_health", json.dumps(status))
```

3. admin API 暴露健康状态（分平台明细）：

```python
# src/admin/routes.py 新增端点
@router.get("/bot-token-health", operation_id="admin_bot_token_health")
def get_bot_token_health(admin: dict = Depends(require_admin)):
    from src.user.db import get_meta_value
    import json
    raw = get_meta_value("bot_token_health")
    if raw:
        return json.loads(raw)
    return {"overall": "healthy", "platforms": {},
            "checked_at": "", "note": "尚未执行探活"}
```

4. 在 main.py lifespan 中启动探活任务：

```python
# main.py lifespan 中新增
try:
    from src.runtime.bot_token_health import start_bot_token_health
    start_bot_token_health()
except Exception as e:
    log.warning(f"Bot token health check start failed: {e}")
```

5. 频率与限流：
- 默认 5 分钟（300s）+ jitter（0-30s）
- 只一个实例执行（GET_LOCK），API 调用量 = 288 次/天
- 探活失败时不立即重试，等下次周期
- 如果 GitCode API 有限流，调大间隔到 10-15 分钟（运行时可调）

6. 职责边界：
- 方向1 只做发现 + 暴露，不干预写操作
- 写操作的 401 识别由方向3 负责
- 两者职责分离，互不依赖

**改动文件**：
- 新增 `src/runtime/bot_token_health.py`
- 改 `src/api/main.py`（lifespan 启动探活）
- 改 `src/admin/routes.py`（新增健康状态端点）
- 新增 `tests/test_bot_token_health.py`

## 四、改动点汇总

| 方向 | 文件 | 类型 | 改动 |
|---|---|---|---|
| 3 | src/vcs/token_error.py | 新增 | 公共 401 识别函数 + 常量 |
| 3 | src/api/routes/issues.py | 改 | 评论 + 关单 401 识别 |
| 3 | src/api/issues_center.py | 改 | 建单引用公共常量 |
| 3 | tests/test_token_error.py | 新增 | 单元测试 |
| 2 | src/user/db.py | 改 | 统一入口 + DB 读取 |
| 2 | src/admin/config_store.py | 改 | save_config 写 DB |
| 2 | src/api/db_setup.py | 改 | 启动迁移 |
| 2 | src/api/routes/repos.py | 改 | 6 处硬编码改统一入口 |
| 2 | src/api/routes/scenes.py | 改 | 2 处硬编码改统一入口 |
| 2 | src/api/routes/templates.py | 改 | 1 处硬编码改统一入口 |
| 2 | src/im/feishu_async.py | 改 | 1 处硬编码改统一入口 |
| 2 | tests/test_global_bot_token_db.py | 新增 | 单元测试 |
| 1 | src/runtime/bot_token_health.py | 新增 | 探活 + GET_LOCK + 健康状态 |
| 1 | src/api/main.py | 改 | lifespan 启动探活 |
| 1 | src/admin/routes.py | 改 | 健康状态端点 |
| 1 | tests/test_bot_token_health.py | 新增 | 单元测试 |

## 五、实施顺序

1. 方向3（P0）：公共 401 识别 + 评论/关单降级提示 → 独立 commit
2. 方向2（P1）：全局 token 迁 DB + 硬编码清理 → 独立 commit
3. 方向1（P2）：定时探活 + 健康状态 → 独立 commit

每个方向独立 commit，可独立 review 和回滚。

## 六、不做什么

- 不引入 Redis（grape 集群设计已明确不需要）
- 不引入 Vault/KMS（后续安全加固统一做）
- 不给全局 token 加密（和 managed_repos.bot_gitcode_token 保持一致明文）
- 不做自动降级/备用 token 切换（只有一个全局 token，停用比用坏 token 更糟）
- 不做 Prometheus/Alertmanager（当前无监控基础设施，log.warning + admin API 可查够用）
- 不删除 global-bot-token.json 文件（保留作为备份/回退）

## 七、确认点

1. 全局 token 明文存 DB meta 表，和 managed_repos.bot_gitcode_token 一致（已确认 DDL 无加密）——已实现
2. admin only writes DB; runtime reads DB with file fallback before migration completes (ensures system availability if migration fails); global-bot-token.json preserved as read-only migration source — implemented
3. 探活用 src/lock.py distributed_locks 表（已有 5+ 处生产使用），TTL=240s < 间隔=300s——已实现
4. 连续 3 次失败才标记 unhealthy，期间 degraded；失败计数存 DB meta JSON；error_type 区分 auth/network/config——已实现
5. 探活只发现不干预写操作，写操作 401 由方向3 处理——已实现
6. 启动迁移用 src/lock.py acquire_lock 保护 + set_meta_value UPSERT 幂等——已实现
7. 60s TTL 内存缓存减少 DB 查询；admin 改配置后清缓存——已实现

## 八、向前兼容性分析

### 场景1：新代码部署到现有环境，不动配置文件

- 启动时 `_migrate_global_bot_token_to_db` 读文件写入 DB
- 运行时 `get_global_gitcode_bot_token()` → DB 有值 → 返回 DB 值
- 如果迁移失败（锁没抢到/异常）→ DB 空 → file_fallback_fn 读文件 → 系统仍可用
- **结论：正常 ✅**

### 场景2：admin 通过 API 改配置后

- `save_config` 写 DB（新值）+ 清缓存（当前实例立即生效）
- 其他实例最多 60s TTL 后缓存过期，读到 DB 新值
- 文件还是旧值，但不会被读到（DB 优先）
- **结论：正常 ✅**

### 场景3：滚动升级，旧实例和新实例同时运行

- 旧实例代码直接读文件（不调统一入口），新实例调统一入口 DB + 文件 fallback
- 两者读取路径完全独立，不交叉
- admin 在新实例改配置 → 写 DB → 旧实例不知道 → 旧实例还读文件旧值
- 这是滚动升级的固有问题（旧实例没有新代码无法读 DB），不是代码 bug
- **结论：正常（滚动升级窗口可接受）✅**

### 场景4：DB 换了新库（空库），文件还在

- 启动迁移：DB 无值 → 读文件 → 写 DB
- 运行时：DB 有值 → 返回
- **结论：正常 ✅**

### 场景5：文件被删了，DB 有值

- 启动迁移：DB 有值 → 跳过
- 运行时：DB 有值 → 返回（file_fallback_fn 不会被调用）
- **结论：正常 ✅**

### 场景6：文件被删了，DB 也没值（全新部署没配文件）

- 启动迁移：文件不存在 → return
- 运行时：DB 空 → file_fallback_fn 读文件 → 文件不存在 → 返回空
- repos.py 抛 503 "全局 Bot Token 未配置"
- **结论：正常（正确拒绝）✅**

### 场景7：webhook_url meta key 一致性

- 迁移写 `meta.webhook_url`（不加 global_ 前缀，与已有 get_webhook_url 一致）
- `get_webhook_url()` 读 `meta.webhook_url` → key 一致
- **结论：正常 ✅**

### 场景8：GitHub 仓 bot_username 被错填成 GitCode bot name（历史 bug）

- 迁移逻辑执行跨平台纠正：`UPDATE managed_repos SET bot_username=github_bot WHERE bot_username=gitcode_bot AND repo_key LIKE 'github:%'`
- 前提条件：`github_bot_username` 有配置值（文件或 DB）
- 如果文件没配 github_bot_username → 纠正不执行 → 需 admin 通过配置中心设置后重启
- **结论：有条件正常（依赖 github_bot_username 有值）⚠️**

### 场景9：gitcode_webhook_token 文件里字段名是 webhook_secret

- 迁移逻辑兼容两种字段名：`data.get("gitcode_webhook_token") or data.get("webhook_secret")`
- **结论：正常 ✅**

## 九、配置文件模板

`configs/global-bot-token.example.json` 是配置模板，包含所有字段和说明：

| 字段 | 用途 | 敏感 |
|------|------|------|
| gitcode_api_token | GitCode bot PAT（写操作：建单/关单/评论） | 是 |
| github_bot_token | GitHub bot PAT（写操作） | 是 |
| github_bot_username | GitHub bot 账号用户名（协作者管理） | 否 |
| gitcode_oauth_client_id | GitCode OAuth 登录 client_id | 否 |
| gitcode_oauth_client_secret | GitCode OAuth 登录 client_secret | 是 |
| gitcode_oauth_redirect_uri | GitCode OAuth 回调地址（留空自动推断） | 否 |
| github_oauth_client_id | GitHub OAuth 登录 client_id | 否 |
| github_oauth_client_secret | GitHub OAuth 登录 client_secret | 是 |
| github_oauth_redirect_uri | GitHub OAuth 回调地址（留空自动推断） | 否 |
| bot_usernames | GitCode bot 用户名列表（self-comment 检测，数组） | 否 |
| webhook_url | GitCode webhook 回调地址 | 否 |
| gitcode_webhook_token | GitCode webhook 签名密钥（也接受字段名 webhook_secret） | 是 |

部署时复制为 `global-bot-token.json` 并填入真实值。启动后所有字段迁移到 DB meta 表，DB 成为运行时权威源，文件保留只读作为迁移源和 fallback。
