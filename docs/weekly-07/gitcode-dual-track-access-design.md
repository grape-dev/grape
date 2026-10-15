# GitCode 访问双轨架构设计文档

> Issue #343 | 分支 feat/issue-343-oauth-token-and-issue-user-id-migration
> 基线：grape-dev/weekly-07-clean

## 1. 问题概述

两个线上故障：

1. **OAuth token 短命**：GitCode OAuth access_token 实测寿命 15-80 分钟，平台当 15 天用，不存过期时间，预刷新阈值 100 分钟远大于实际寿命，403 不处理直接 500。
2. **账号迁移漏表**：迁移脚本漏 issue 表，老需求 user_id 全串号（已数据修复，但迁移脚本未补全）。

本方案采用双轨架构根治问题一，并补全问题二的迁移脚本。

## 2. 架构设计：读写分离

### 2.1 写链路：仓库 Bot Token

所有变更类操作（建 issue、编辑 issue、发评论、标签变更）走仓库维度 bot token。

**当前状态**：pipeline 路径（event_pipeline.py）已全部走 bot token（`get_gitcode_token(repo_key=...)`），只有用户交互路径（issues_center.py create_task_via_issue + issues.py clarify_issue）还在走用户 OAuth token。

**需要切换的写操作**（3 处）：

| 位置 | 操作 | 当前 token | 改为 |
|------|------|-----------|------|
| issues_center.py:1271,1305 create_task_via_issue | 建 issue | get_user_token_with_refresh | get_gitcode_token(repo_key) |
| issues_center.py:1395 create_task_via_issue | patch issue body | _gitcode_api_with_refresh | bot token 直接调用 |
| issues.py:1074,1106 clarify_issue | 发评论 | get_user_token_with_refresh + _gitcode_api_with_refresh | get_gitcode_token(repo_key) |

**不需要改的写操作**（已走 bot token）：
- event_pipeline.py 所有写操作（_post_result_to_issue、comment posting 等）已用 bot_token
- create_standard_task（onboarding 路径）已用 bot_token 参数传入

**业务补偿（#337 防复现）**：
- issue 表 user_id 已在建档时写入真实操作人（issues_center.py:95 INSERT 带 user_id），不依赖 GitCode creator 字段
- UI / 统计层已按本地 user_id 展示，不读 GitCode 返回的 creator

### 2.2 读链路：用户 OAuth Token + 统一包装器 with_gitcode_token

仅读场景（拉仓库列表、查用户信息、查分支等）走用户 OAuth token，带统一预刷新 + 401/403 处理。

**当前读操作散落位置**：

| 文件 | 行 | 操作 | 当前方式 |
|------|-----|------|---------|
| repos.py:42,52,284,409,519,690,891,1025,1227,1283,1439 | 多处 | 拉仓库列表/查仓库信息/查分支 | get_user_token_with_refresh（裸用） |
| scenes.py:524,607 | 2处 | 查用户 gitcode login | get_user_token_with_refresh（裸用） |
| templates.py:166 | 1处 | 查用户 gitcode login | get_user_token_with_refresh（裸用） |
| chat.py:558,723,1462,1826 | 4处 | 注入 user_gitcode_token 到会话 | get_user_token_with_refresh（裸用） |
| settings.py:60 | 1处 | 读凭据状态 | get_user_token（无刷新） |
| vcs/providers/gitcode.py:19 | 1处 | 中性 provider 接口 | get_user_token_with_refresh |

**设计**：实现 `with_gitcode_token(user, fn, *args, **kwargs)` 包装器，所有读操作强制走此包装器。

## 3. 详细改动

### 3.1 with_gitcode_token 包装器

**新文件**：`src/api/gitcode_token.py`

```python
"""GitCode OAuth token 统一包装器。

所有 GitCode 读 API 必须走此包装器，禁止裸读数据库 token 直接调用 API。
职责：预刷新 + 401/403 错误处理 + 降级提示。
"""
import time
from src.config import log
from src.api.deps import (
    get_user_token,
    try_refresh_gitcode_token,
    _get_user_refresh_lock,
)


def _is_token_error(err: Exception) -> bool:
    """判断异常是否为 GitCode token 失效（401/403）。

    403 谨慎处理：只匹配 403 + "token"/"forbidden" 关键词，
    不把所有 403 都当 token 失效（403 也可能是正常的仓库权限拒绝）。
    """
    msg = str(err)
    if "401" in msg or "Bad credentials" in msg:
        return True
    if "403" in msg and ("token" in msg.lower() or "forbidden" in msg.lower()):
        return True
    return False


def _check_and_refresh(user_id: int) -> str:
    """获取 token，临期主动预刷新。返回 token 或空字符串。"""
    from src.user.db import get_credential
    import time as _time

    acc = _get_user_account(user_id)
    token = acc.get("gitcode_api_token", "") if acc else ""
    if not token:
        return ""

    cred = get_credential(user_id, "gitcode_oauth")
    if cred:
        token_expires_at = cred.get("token_expires_at", 0)
        now = int(_time.time())
        need_refresh = False
        if token_expires_at:
            need_refresh = (token_expires_at - now) < 300  # 5 分钟余量
        else:
            fetched_at = cred.get("fetched_at", 0)
            age = now - fetched_at if fetched_at else 0
            need_refresh = age > 3600  # 降级：60 分钟
        if need_refresh:
            new_token = try_refresh_gitcode_token(user_id)
            return new_token or token
    return token


def with_gitcode_token(user: dict, api_call_fn, *args, **kwargs):
    """包装 GitCode 读 API 调用。

    流程：
    1. 预刷新：token 临近过期时主动刷新
    2. 执行请求
    3. 401/403 → 刷新 token 重试一次
    4. 重试后仍 403 → 业务权限问题，不提示重新授权
    5. refresh 失败 → 返回"请重新授权"

    user: {id, username}
    api_call_fn: 第一个位置参数是 token
    """
    from fastapi import HTTPException

    user_id = user["id"]
    token = _check_and_refresh(user_id)

    if not token:
        raise HTTPException(status_code=401, detail={
            "code": 401,
            "message": "GitCode 授权已过期，请重新登录授权",
            "action": "reauth_required"
        })

    try:
        return api_call_fn(token, *args, **kwargs)
    except Exception as e:
        if not _is_token_error(e):
            raise  # 非 token 错误不处理

        # token 失效 → 尝试刷新
        new_token = try_refresh_gitcode_token(user_id)
        if not new_token:
            raise HTTPException(status_code=401, detail={
                "code": 401,
                "message": "GitCode 授权已过期，请重新登录授权",
                "action": "reauth_required"
            })

        # 刷新成功 → 重试一次
        try:
            return api_call_fn(new_token, *args, **kwargs)
        except Exception as e2:
            if _is_token_error(e2):
                # 重试后仍 401/403(token) → refresh_token 也失效
                raise HTTPException(status_code=401, detail={
                    "code": 401,
                    "message": "GitCode 授权已过期，请重新登录授权",
                    "action": "reauth_required"
                })
            # 重试后 403 但不含 token 关键词 → 业务权限不足
            if "403" in str(e2):
                raise HTTPException(status_code=403, detail={
                    "code": 403,
                    "message": "无权限执行此操作"
                })
            raise  # 其他错误正常抛出
```

### 3.2 expires_in 存储（4 处）

| 文件 | 位置 | 改动 |
|------|------|------|
| auth.py:163 | GitCode callback | 读 expires_in，传给 link_gitcode_user |
| auth.py:325 | GitHub callback | 同步 |
| db.py:263 | link_gitcode_user | 加 expires_in 参数，存 token_expires_at |
| db.py:294 | link_github_user | 同步 |
| db.py:1584 | refresh_gitcode_oauth_token | 刷新后存 token_expires_at |

config_json 新增字段：
```python
"token_expires_at": int(time.time()) + expires_in if expires_in else 0,
"expires_in": expires_in,
```

### 3.3 写操作切换 bot token（3 处）

#### 3.3.1 create_task_via_issue（issues_center.py:1271）

```python
# 当前：
api_token = get_user_token_with_refresh(user["username"], user_id=user["id"])
# ...
resp = _gitcode_api_with_refresh(user, _post_gitcode_issue, owner, rname, ...)

# 改为：
from src.user.db import get_gitcode_token
from src.repo_key import to_repo_key
api_token = get_gitcode_token(repo_key=to_repo_key(body.repo_key))
if not api_token:
    raise HTTPException(status_code=400, detail={"code": 400, "message": "仓库 Bot Token 未配置"})
# ...
resp = _post_gitcode_issue(api_token, owner, rname, body.title.strip(),
                           _desc_with_attach, assignee=assignee)
```

issue 表 user_id 仍写入 user["id"]（真实操作人），不受 token 切换影响。

#### 3.3.2 patch issue body（issues_center.py:1395）

```python
# 当前：
_gitcode_api_with_refresh(user, _patch_issue_body, owner, rname, issue_num, ...)

# 改为：
from src.user.db import get_gitcode_token
_patch_issue_body(get_gitcode_token(repo_key=repo_key), owner, rname, issue_num, ...)
```

#### 3.3.3 clarify_issue（issues.py:1074,1106）

```python
# 当前：
api_token = get_user_token_with_refresh(user["username"], user_id=user["id"])
# ...
_gitcode_api_with_refresh(user, _post_comment)

# 改为：
from src.user.db import get_gitcode_token
from src.repo_key import to_repo_key
api_token = get_gitcode_token(repo_key=to_repo_key(repo_key))
if not api_token:
    raise HTTPException(status_code=400, detail={"code": 400, "message": "仓库 Bot Token 未配置"})
# ...
_post_comment(api_token)
```

### 3.4 读操作接入 with_gitcode_token

将散落的 get_user_token_with_refresh + 裸调用模式替换为 with_gitcode_token 包装器。

示例（repos.py 拉仓库列表）：
```python
# 当前：
api_token = get_user_token_with_refresh(user["username"], user.get("id", 0))
data = _req("GET", f"https://gitcode.com/api/v5/repos/{login}/{name}", api_token)

# 改为：
from src.api.gitcode_token import with_gitcode_token
def _fetch_repo(token, login, name):
    return _req("GET", f"https://gitcode.com/api/v5/repos/{login}/{name}", token)
data = with_gitcode_token(user, _fetch_repo, login, name)
```

**chat.py 特殊处理**：chat.py 注入 user_gitcode_token 到会话（agent_tasks.user_gitcode_token），这不是直接调 API，而是把 token 传给后端 agent。改为用 with_gitcode_token 的 _check_and_refresh 获取 token（只做预刷新，不做 401 重试，因为重试逻辑在 agent 侧不适用）。

### 3.5 删除旧 _gitcode_api_with_refresh + _is_gitcode_401

写操作切 bot token 后，_gitcode_api_with_refresh 和 _is_gitcode_401 不再有调用方，删除：
- `src/api/issues_center.py:1126-1168`（_is_gitcode_401 + _gitcode_api_with_refresh）
- `src/api/routes/issues.py` 中对 _is_gitcode_401 / _gitcode_api_with_refresh 的 import
- `src/api/routes/tasks.py:61-62` 中对 _gitcode_api_with_refresh / _is_gitcode_401 的 import

### 3.6 get_user_token_with_refresh 改用 token_expires_at

**文件**：`src/api/deps.py:318-344`

保留此函数（chat.py 等注入场景仍需要），但预刷新逻辑改为用 token_expires_at：
- 有 token_expires_at：距过期不足 5 分钟时刷新
- 无 token_expires_at（历史数据降级）：fetched_at + 60 分钟

### 3.7 迁移脚本补全（全量用户归属列）

**背景**：深度排查发现 20+ 张表带用户归属列，迁移脚本只覆盖了 4 张。数据已逐表手动修复，但脚本源头还在，其他环境重跑会复现。

**文件**：`scripts/migrate_user_accounts_to_grape.py`（从 git history 777641b8f 恢复 .py 源文件）

#### 全量用户归属列清单

按列类型分两类：

**INTEGER 类型（存 user_id，需 id 映射）**：

| 表 | 列 | 当前状态 |
|---|---|---|
| user_repos | user_id | ✅ 已在迁移脚本 |
| chat_conversations | user_id | ✅ 已在迁移脚本 |
| organization | owner_id | ✅ 已在迁移脚本 |
| org_member | user_id | ✅ 已在迁移脚本 |
| issue | user_id | ❌ 遗漏（已数据修复 396 行） |
| issue | scene_user_id | ❌ 遗漏（已数据修复 49 行） |
| issue_state | scene_user_id | ❌ 遗漏 |
| agent_tasks | user_id | ❌ 遗漏（已数据修复 2 行） |
| scene_market | source_user_id | ❌ 遗漏（已数据修复 1 行） |
| scene_likes | user_id | ❌ 遗漏 |
| skill_likes | user_id | ❌ 遗漏 |
| session_api_tokens | user_id | ❌ 遗漏（已数据修复 1 行） |
| api_tokens | user_id | ❌ 遗漏 |
| user_feedback | user_id | ❌ 遗漏 |
| org_invitation | invitee_user_id | ❌ 遗漏 |
| audit_logs | user_id | ❌ 遗漏 |
| exchange_codes | user_id | ❌ 遗漏 |
| autopilot_tasks | created_by | ❌ 遗漏 |
| grape_accounts | created_by_oauth | ❌ 遗漏（内部审计字段） |
| community_posts | user_id | ❌ 遗漏 |
| community_comments | user_id | ❌ 遗漏 |
| community_post_likes | user_id | ❌ 遗漏 |
| external_identities | grape_user_id | ❌ 遗漏（但迁移阶段1已直接写新ID） |
| unified_user_mapping | user_id | ❌ 遗漏（已在 02:00 单独刷新） |
| feishu_onboard_task | user_id | ❌ 遗漏 |
| binding_tickets | initiating_user_id | ❌ 遗漏 |
| binding_tickets | original_user_id | ❌ 遗漏 |
| organization | owner_id | ✅ 已在迁移脚本（重复列出提醒） |

**TEXT 类型（存 username，非 user_id）**：

| 表 | 列 | 说明 |
|---|---|---|
| skill | owner_id | TEXT（username），不走 id 映射 |
| skill | created_by | VARCHAR(64)（username） |
| scene_market | created_by | VARCHAR(255)（username） |
| agent_runtimes | owner_id | TEXT（username 或 user_id 字符串） |
| backend_agents | owner_id | TEXT |

TEXT 类型列存的是 username 不是 user_id，不需要 id 映射——但如果旧 username 和新 username 不同（本次迁移 username 不变），则需要 username 映射。本次迁移 username 保持不变，所以 TEXT 列不需要改。

#### 改动

`_update_related_tables` 的 tables 列表补全为：

```python
tables = [
    # 原有 4 张
    ("user_repos", "user_id"),
    ("chat_conversations", "user_id"),
    ("organization", "owner_id"),
    ("org_member", "user_id"),
    # 新增 INTEGER 类型用户归属列
    ("issue", "user_id"),
    ("issue", "scene_user_id"),
    ("issue_state", "scene_user_id"),
    ("agent_tasks", "user_id"),
    ("scene_market", "source_user_id"),
    ("scene_likes", "user_id"),
    ("skill_likes", "user_id"),
    ("session_api_tokens", "user_id"),
    ("api_tokens", "user_id"),
    ("user_feedback", "user_id"),
    ("org_invitation", "invitee_user_id"),
    ("audit_logs", "user_id"),
    ("exchange_codes", "user_id"),
    ("autopilot_tasks", "created_by"),
    ("grape_accounts", "created_by_oauth"),
    ("community_posts", "user_id"),
    ("community_comments", "user_id"),
    ("community_post_likes", "user_id"),
    ("external_identities", "grape_user_id"),
    ("unified_user_mapping", "user_id"),
    ("feishu_onboard_task", "user_id"),
    ("binding_tickets", "initiating_user_id"),
    ("binding_tickets", "original_user_id"),
]
```

#### "NOT IN grape_accounts" 防护盲区修复

当前逻辑：`WHERE {column} = ? AND {column} NOT IN (SELECT id FROM grape_accounts)`

盲区：旧 ID 恰好撞上新表另一个用户的 ID → 被静默跳过，不报错，不迁移。issue 表 54 条、chat 表部分行就是这么漏的。

修复：在跳过时检查是否因为"撞 ID"跳过，如果是则打 WARNING：

```python
for old_id, new_id in id_map.items():
    with db_lock:
        c = db.cursor()
        # 查旧 ID 的行数
        c.execute(f"SELECT COUNT(*) FROM {table} WHERE {column} = ?", (old_id,))
        total = c.fetchone()[0]
        # 查旧 ID 的行数中，不在 grape_accounts 里的（真正需要迁移的）
        c.execute(
            f"SELECT COUNT(*) FROM {table} "
            f"WHERE {column} = ? "
            f"AND {column} NOT IN (SELECT id FROM grape_accounts)",
            (old_id,)
        )
        migratable = c.fetchone()[0]

        if migratable == 0:
            if total > 0:
                # 旧 ID 撞上了新表另一个用户 → 这些行无法自动迁移
                log.warning(
                    f"  [WARNING] {table}.{column}: {total} 行 old_id={old_id} "
                    f"撞上新用户 ID，无法自动迁移，需人工核对"
                )
            skipped += 1
            continue

        if dry_run:
            print(f"  [DRY-RUN] {table}.{column}: {migratable} 行 {old_id} → {new_id}")
            skipped += 1
            continue

        c.execute(
            f"UPDATE {table} SET {column} = ? "
            f"WHERE {column} = ? "
            f"AND {column} NOT IN (SELECT id FROM grape_accounts)",
            (new_id, old_id)
        )
        db.commit()
        updated += migratable

print(f"  {table}.{column}: 更新 {updated} 行, 跳过 {skipped} 行")
```

#### 新增：迁移后全表孤儿 ID 扫描

迁移完成后自动扫描所有表中的孤儿 user_id（不在 grape_accounts 中的 ID）：

```python
def _scan_orphan_ids() -> list[dict]:
    """扫描所有用户归属列中的孤儿 ID（不在 grape_accounts 中的 user_id）。"""
    orphan_tables = [
        # 与 tables 列表一致
    ]
    orphans = []
    for table, column in orphan_tables:
        with db_lock:
            c = db.cursor()
            c.execute(
                f"SELECT {column}, COUNT(*) FROM {table} "
                f"WHERE {column} != 0 "
                f"AND {column} NOT IN (SELECT id FROM grape_accounts) "
                f"GROUP BY {column}"
            )
            for row in c.fetchall():
                orphans.append({"table": table, "column": column,
                                "orphan_id": row[0], "count": row[1]})
    return orphans
```

迁移脚本最后调用，打印全量孤儿 ID 报告。有孤儿 → 迁移不完整，需人工处理。

## 4. 调用点全量清单

### 写操作（全部切 bot token）

| # | 文件:行 | 操作 | 当前 | 改为 |
|---|---------|------|------|------|
| W1 | issues_center.py:1271,1305 | 建 issue | user token + _gitcode_api_with_refresh | get_repo_bot_token(repo_key) |
| W2 | issues_center.py:1395 | patch issue body | _gitcode_api_with_refresh | get_repo_bot_token(repo_key) |
| W3 | issues.py:1074,1106 | 发评论 | user token + _gitcode_api_with_refresh | get_repo_bot_token(repo_key) |
| W4 | issues.py:539 | 关 issue | user token only | get_repo_bot_token 优先 + user token fallback |
| W5 | issues_center.py:1255 | assignee | grape username | external_identities external_login |
| W6 | issues_center.py:1274 | 幂等查重失败 | except Exception: return None（静默放行） | raise RuntimeError，调用方返回 502 + "Bot Token 可能失效" |

注意：写操作使用 get_repo_bot_token（跳过 devops_agent_session token），不用 get_gitcode_token（后者第一优先级是 agent session token，scope 可能受限）。

已走 bot token（不改）：
- event_pipeline.py 所有写操作
- create_standard_task（onboarding）

### 读操作（全部接入 with_gitcode_token）

| # | 文件:行 | 操作 |
|---|---------|------|
| R1 | repos.py:42,52 | 拉仓库列表（_get_user_provider_and_token） |
| R2 | repos.py:284,409 | 查仓库信息 |
| R3 | repos.py:485,519 | 查仓库存在性 |
| R4 | repos.py:690,891 | 查 collaborator |
| R5 | repos.py:1025,1227,1283 | 查仓库详情 |
| R6 | repos.py:1439 | token provider 选择 |
| R7 | scenes.py:524,607 | 查用户 gitcode login |
| R8 | templates.py:166 | 查用户 gitcode login |
| R9 | chat.py:558,723,1462,1826 | 注入会话 token（用 _check_and_refresh） |
| R10 | settings.py:60 | 读凭据状态 |
| R11 | vcs/providers/gitcode.py:19 | 中性 provider 接口 |

### Token 存储修复

| # | 文件:行 | 改动 |
|---|---------|------|
| S1 | auth.py:163 | GitCode callback 读 expires_in |
| S2 | auth.py:325 | GitHub callback 读 expires_in |
| S3 | db.py:263 | link_gitcode_user 存 token_expires_at |
| S4 | db.py:294 | link_github_user 存 token_expires_at |
| S5 | db.py:1584 | refresh_gitcode_oauth_token 存 token_expires_at |
| S6 | deps.py:318 | get_user_token_with_refresh 用 token_expires_at |

### 迁移脚本

| # | 文件 | 改动 |
|---|------|------|
| M1 | scripts/migrate_user_accounts_to_grape.py | tables 补全 27 张表（含 issue/issue_state/agent_tasks/scene_market/session_api_tokens 等） |
| M2 | scripts/migrate_user_accounts_to_grape.py | "NOT IN grape_accounts" 盲区修复：撞 ID 跳过时打 WARNING |
| M3 | scripts/migrate_user_accounts_to_grape.py | 新增迁移后全表孤儿 ID 扫描步骤 |

### 清理

| # | 文件 | 改动 |
|---|------|------|
| C1 | issues_center.py | 删除 _is_gitcode_401 + _gitcode_api_with_refresh |
| C2 | issues.py | 删除对 C1 的 import |
| C3 | tasks.py | 删除对 C1 的 import |

## 5. 分阶段上线

### 阶段 1：Token 存储修复 + 预刷新（S1-S6）
- 补齐 token_expires_at 存储
- get_user_token_with_refresh 用实际过期时间
- 立即缓解 token 短命问题

### 阶段 2：写操作切 bot token（W1-W3 + C1-C3）
- 3 处写操作切 bot token
- 删除旧的 _gitcode_api_with_refresh
- 流水线写操作不再受用户 OAuth 生命周期影响

### 阶段 3：读操作接入包装器（R1-R11）
- 实现 with_gitcode_token
- 逐个接入读操作
- 消除散落的 token 逻辑

### 阶段 4：迁移脚本补全（M1-M4）+ 其他修复
- tables 列表从 4 张补全到 29 张（含 user_credentials）
- "NOT IN grape_accounts" 盲区修复：撞 ID 时打 WARNING
- 逐行 UPDATE + try/except：撞 UNIQUE 键时跳过单条继续
- 阶段 1 撞 ID 防护：_ensure_user_credentials 写入前核对 username 一致性
- 新增迁移后全表孤儿 ID 扫描 + 撞 ID 错位检测
- autopilot_subscriptions 建表
- JWT username/id 一致性校验 + token_version 止血 SQL
- 删除个人组织启动迁移（_migrate_create_personal_orgs）
- P0: get_github_token 改用 get_user_identities_with_tokens
- P1: _sync_send_async 补 user_token 赋值
- P1: 预刷新降级阈值 60→30 分钟

## 6. 403 处理流程（关键细节）

```
收到 403
  ├─ 含 "token"/"forbidden" 关键词？
  │   ├─ 是 → 尝试 refresh token
  │   │   ├─ refresh 成功 → 重试 API
  │   │   │   ├─ 重试成功 → 返回结果
  │   │   │   ├─ 重试仍 403(token) → refresh_token 也失效 → "请重新授权"
  │   │   │   └─ 重试 403(无 token 关键词) → 业务权限不足 → "无权限"
  │   │   └─ refresh 失败 → "请重新授权"
  │   └─ 否 → 业务权限不足 → "无权限"（不触发刷新）
```

核心原则：不能只要看到 403 就判定 token 失效。refresh 成功但重试仍 403，说明 token 有效，是用户没有该操作权限。

## 7. 不改的部分

- 不改 DB 表结构（token_expires_at 存在 config_json JSON 里）
- 不改前端（reauth_required action 前端已有处理逻辑）
- 不改 pipeline 路径（已全部走 bot token）
- 不改 bot token 链路（get_gitcode_token / get_bot_token_by_repo 不变）
- 不改 issue 表结构
- 不改 /issues/mine 接口逻辑

## 8. 验证计划

### 阶段 1 验证
1. OAuth 授权后查 DB 确认 token_expires_at 有值
2. 手动改 token_expires_at 为过去时间 → get_user_token_with_refresh 触发刷新
3. 单元测试：_is_token_error 匹配 403+forbidden，不匹配纯 403

### 阶段 2 验证
1. JWT 登录用户建 issue → GitCode 上 creator 显示 grape_bot，本地 issue.user_id = 真实用户
2. JWT 登录用户发评论 → 评论成功，不依赖用户 OAuth token
3. 用户 OAuth token 失效 → 建 issue 仍成功（走 bot token）

### 阶段 3 验证
1. 读操作 token 过期 → 自动刷新成功
2. refresh_token 也失效 → 返回 401 + reauth_required，不抛 500
3. 403 权限不足 → 返回 403 "无权限"，不提示重新授权

## 9. 深度审查修复（P0/P1/P2）

### P0-1: token_expires_at 格式不一致

**问题**：external_identities.token_expires_at 列存 TEXT 格式（`strftime("%Y-%m-%d %H:%M:%S", gmtime())`），user_credentials config_json 存 epoch int（`int(time.time()) + expires_in`）。deps.py 读 user_credentials 做 `(epoch - now) < 600` 数值比较，如果后续开发者从 external_identities 读 TEXT 格式做同样的数值比较会 TypeError。

**修复**：deps.py `get_user_token_with_refresh` 和 `get_user_github_token_with_refresh` 统一加 `isinstance(str)` 判断 + `strptime` 转换，兼容两种格式。设计上保持 user_credentials 存 int（JSON dict 更自然），external_identities 存 TEXT（DDL 已定义为 TEXT 列），通过读取侧归一化消除差异。

### P0-2: with_oauth_token GitHub 路径无预刷新

**问题**：oauth_token.py GitHub 分支用 `get_user_github_token(user_id)`（不带预刷新），和 Issue #343 的"预刷新提前 10 分钟"目标矛盾。GitCode 路径走了 `get_user_token_with_refresh`（有预刷新），GitHub 路径没有。

**修复**：新增 `deps.py get_user_github_token_with_refresh(username, user_id)` 函数，和 GitCode 版完全对称。oauth_token.py GitHub 分支改用此函数。

### P1-1: repos.py 双重获取 token

**问题**：list_member_repos / sync_repos 先预获取 token 放入 `api_tokens` dict，又用 `with_oauth_token` 包装（内部再获取一次）。预获取的 token 只用于 `_probe_stale_repos`，双重获取浪费且探活路径无 token 保护。

**修复**：改为从 `get_user_identities` 查绑定平台列表（不获取 token），`_probe_stale_repos` 也走 `with_oauth_token` 获取 token。

### P1-2: check_market_scene_experience 吞 401 异常

**问题**：scenes.py `check_market_scene_experience` catch 所有异常返回 `{"repo_exists": False}`，token 过期时用户看到"仓库不存在"假数据而非"请重新授权"。

**修复**：移除 try/except，让 with_oauth_token 的 401 HTTPException 正常抛给前端。

### P1-3: _try_close_issue_for_repo fallback 无 with_oauth_token 保护

**问题**：bot token 失败后 fallback 到 `get_user_token_with_refresh` 获取用户 token，但直接传给 VcsApiClient.close_issue，无 401 重试保护。

**修复**：拆分 bot token 路径和 user token fallback 路径。user token fallback 走 `with_oauth_token` 包装，获得预刷新 + 401 重试能力。

### P1-4: 两个同名 get_user_github_token 签名不同

**问题**：`deps.py get_user_github_token(username: str)` 和 `user/db.py get_user_github_token(user_id: int)` 同名不同签名，极易混淆。

**修复**：user/db.py 新增 `get_user_github_token_by_id(user_id)` 函数，保留 `get_user_github_token` 作为向后兼容别名。调用方逐步迁移到新名。

### P1-5: 迁移脚本 _get_pk_columns MySQL 兼容

**问题**：`_get_pk_columns` 只用 PRAGMA table_info，MySQL 环境如果 mysql_compat 翻译不完美会返回空列表 → fallback 到批量 UPDATE → 回到"撞 UNIQUE 键整个异常终止"问题。

**修复**：PRAGMA 失败时 fallback 到 `SHOW KEYS FROM table WHERE Key_name='PRIMARY'`，双保险。

### P2-1: GitCode 幂等查重改 state=open

**问题**：GitCode 查重用 `state=all` 再 Python 过滤，效率低（大量 closed issue 时翻很多页才找到 open 的）。

**修复**：改为 `state=open`，API 层直接过滤。保留 Python state 检查作为双重保障。

## 10. 平台账号名被当成外部登录名（问题十一）

### 背景

用户 grape 平台账号名（`grape_accounts.username`）和 OAuth 绑定的 GitCode/GitHub 登录名（`external_identities.external_login`）可以不一致。例如 97 号用户平台名 `wangmengmeng`，GitCode 登录名 `gcw_hbTFPDA0`。全库 99 个用户中 2 个不一致。

### 出错路径

建单流程 `create_task_via_issue` 中，assignee 已在 1257-1272 行通过 `get_user_identities` 查到真实 external_login。但后续 3 处仍 fallback 到 `user["username"]`（平台名）：

1. **pipeline payload 创建人**（issues_center.py:1410）：`_assign = assignee or user["username"]` → pipeline 的 `user.login`/`user.username` 填平台名 → `ensure_issue_task` → `_get_user_id_by_gitcode("wangmengmeng")` 查 unified_user_mapping 查不到 → user_id=0

2. **scene_user_id 查询**（issues_center.py:1345）：`_repo_user_id(repo_key, issue_creator=user["username"])` → `_repo_user_id` 查 `unified_user_mapping.gitcode_user` → 传了平台名而非 GitCode 登录名 → 查不到 → scene_user_id=0

3. **建单 API 403/422 未捕获**（issues_center.py:1300-1306）：`_post_gitcode_issue` / `_post_github_issue` 抛 RuntimeError 时无 try/catch → 500

### 修复方案

1. pipeline payload 创建人：`_assign = assignee`（去掉 `user["username"]` fallback；assignee 为空时不填创建人字段）
2. scene_user_id：`_repo_user_id(repo_key, issue_creator=assignee)`（用 external_login 而非平台名）
3. 建单调用处加 try/except：从 RuntimeError 消息提取 HTTP code，403 → "处理人无效或无权限"，422 → "参数错误"

## 11. 父代理深度审计修复（P1-1 ~ P1-6）

### P1-1: with_oauth_token 被包装函数吞 RuntimeError

**问题**：get_user_gitcode_login 内部 `except RuntimeError: return ""`，_check_collaborator_admin 内部 `except RuntimeError: return False`。当 GitCode 返回 401 时，这些函数吞掉异常返回空值，with_oauth_token 的被动 401 重试永远看不到 401 → 不触发 token 刷新。

**修复（返工）**：之前尝试在调用点包 wrapper 把空返回转为 RuntimeError，但新消息不含 (NNN) 格式 → _is_token_error 匹配不到 → 无效。返工改为：wrapper 直接调 `_req` 绕过吞异常的函数，让原始 401 RuntimeError（含 `(401)` 格式）穿透到 with_oauth_token。涉及 scenes.py 两处 + repos.py 一处。

### P1-6: sync_repos + list_member_repos 单平台 401 软失败

**问题**：两个 for 循环的 `except Exception as e:` 会捕获 with_oauth_token 抛的 HTTPException(401 reauth_required)，用户只看到"同步/拉取失败"而非"需要重新授权"。

**修复**：改为软失败模式——401 时记录 `_reauth_provider` 但不中断循环，继续拉其他平台。循环结束后：
- 有结果 + 某平台 401 → 返回部分结果 + `_reauth_provider` 标记
- 无结果 + 某平台 401 → 抛 401 让前端提示重新授权
- 非 401 的 HTTPException → 仍然直接抛出

### P1-2: GitHub close_issue fallback 绕过 with_oauth_token

**问题**：issues.py _try_close_issue_for_repo GitHub 路径，bot token 为空时获取用户 token `token = _get_ugh(user["id"])`，token 非空 → `if _use_user_token_fallback and not token:` 条件不满足 → 跳过 with_oauth_token 路径，走到 bot token 直接调用路径用用户 token 但无保护。

**修复**：GitHub 路径和 GitCode 对称——只设 flag 不获取 token，让 with_oauth_token 内部用 get_user_github_token_with_refresh 获取（有预刷新）。

### P1-3: created_by_oauth 布尔标志被当 user_id 迁移

**问题**：grape_accounts.created_by_oauth 是布尔标志（0/1），不是 user_id。迁移脚本对它做 old_id→new_id 映射，如果 old_id=1 且 grape_accounts 无 id=1 用户，所有 created_by_oauth=1 的行会被改成 new_id。同时触发 MySQL 1093 自引用 UPDATE 错误。

**修复**：从 tables 列表和 orphan_tables 列表同时删除 `("grape_accounts", "created_by_oauth")`。

### P1-4: restore_binding_in_tx / restore_external_identity 缺 expires_in

**问题**：两个恢复绑定函数不接受 expires_in，不更新 token_expires_at。用户解绑后重新绑定时过期时间不更新。

**修复**：两个函数新增 expires_in 参数，UPDATE/INSERT 语句写入 token_expires_at。调用方（grape_auth.py 两处）同步传参。

### P1-5: repos.py 裸调用覆盖率不足（后续迭代）

**问题**：设计文档声称"所有读操作走包装器"，实际 repos.py 约 28 处仍用裸调用。但其中多数是写操作（add_collaborator / setup_webhook 等），实际需接入的读操作约 10-15 处。

**决策**：后续迭代逐个接入，不阻塞上线。当前覆盖率约 30%，未接入的路径是"未改善"而非"回归"。

## 12. 部署流程修复（P1-1 ~ P1-3 + P2-2/P2-3）

### P1-1: D32 迁移改为 yield 前同步等待

**问题**：D32 迁移异步执行，lifespan 不等待就 yield。迁移期间用户请求读到旧 user_id 导致数据错位。

**修复**：lifespan 中改为 `await asyncio.to_thread(run_migration, False)` 同步等待完成后再 yield。/readyz 增加 migration 状态检查。幂等——已迁移环境秒返回。

### P1-2: 迁移脚本 _ensure_external_identity INSERT 补 token_expires_at

**问题**：INSERT 语句缺 token_expires_at 列，迁移创建的行过期时间为空。

**修复**：INSERT 补 `token_expires_at` 列，DEFAULT ''。

### P1-3: token_version+1 集成到 init_database

**问题**：依赖人工执行 SQL，窗口期旧 JWT 仍有效。

**修复**：init_database 末尾用 meta 表 key='issue343_jwt_invalidated' 标记，自动执行 `UPDATE grape_accounts SET token_version = token_version + 1`，幂等只执行一次。SQL 文件保留为手动备份。

### P2-2: docstring 两阶段改三阶段

### P2-3: _repair_missing_credentials 传 source_username

补迁移路径也启用撞 ID 防护。
