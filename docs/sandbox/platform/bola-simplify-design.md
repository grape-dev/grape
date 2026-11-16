# BOLA 简化 + source/scope 规范化方案

## 1. 背景

当前 BOLA（Broken Object Level Authorization）设计限制太多，导致现网 worker 认领任务被拦截（BOLA blocked claim: task_user=1 cred_user=0）。系统维度架构尚未稳定，限制太多会影响现网功能。

决策：当前阶段简化 BOLA，后续架构稳定后再加强。

## 2. 现有问题

### 2.1 scope 枚举混乱

当前 scope 枚举值：`user / repo / global / system`，4 个值语义重叠：

| scope | 含义 | 问题 |
|-------|------|------|
| `global` | 全局可认领 | 和 `system` 语义重叠——都是"不限制" |
| `system` | 系统级不限制 | 和 `global` 语义重叠 |
| `repo` | 绑定仓库 | 功能未完整实现（_check_task_access 里直接 403） |
| `user` | 绑定用户 | 功能实现了但和 BOLA 拦截冲突 |

### 2.2 system scope 硬编码 4 处

4 个接口只允许 `scope == 'system'` 访问：
- `recover-orphans`：非 system 只能操作自己的 runtime_id
- `provider-configs`：只 system 可访问（含明文 api_key）
- `sandbox-paths`：只 system 可访问
- `skill-egress`：只 system 可访问

但现网 worker 的 scope 是 `global`（不是 `system`），导致这 4 个接口对现网 worker 返回 403。

### 2.3 _check_task_access 过度校验

- `scope='system'` 要校验 `scope_detail` JSON（unrestricted/pools）——过度设计
- `scope='repo'` 直接 403 "not implemented"——未实现还拦着
- `scope='user'` 校验 `task.user_id == cred.owner_id`——导致现网 worker（global scope，owner_id 为空）被 BOLA 拦截

### 2.4 _check_pool_engines_reachable 里 scope 检查（基线已有，本次不改动）

在判断 pool 引擎是否可达时也做了 scope 检查——`user` scope 要匹配 user_id，`repo` scope 要匹配 repo_key。逻辑正确（global 放行 / repo 校验仓库 / user 校验用户），不影响现网功能，本次不改动。

### 2.5 DB 结构缺 source 字段

`runtime_credentials` 表没有 `source` 字段——无法区分凭据来源是系统签发还是用户签发。

## 3. 方案

### 3.1 新增 source 字段

`runtime_credentials` 表新增 `source` 列：

```sql
ALTER TABLE runtime_credentials ADD COLUMN source VARCHAR(32) NOT NULL DEFAULT 'system' 
  COMMENT 'system=系统签发 user=用户签发';
```

| source | 含义 |
|--------|------|
| `system` | 系统管理员签发的凭据（如一键安装脚本生成的全局 worker） |
| `user` | 用户通过 Web UI 签发的凭据 |

现有数据迁移规则（按 scope 设置 source）：

```sql
-- global scope → source=system
UPDATE runtime_credentials SET source='system' WHERE scope='global';
-- repo / user scope → source=user
UPDATE runtime_credentials SET source='user' WHERE scope IN ('repo', 'user');
-- system scope → 统一改为 global + source=system
UPDATE runtime_credentials SET source='system', scope='global' WHERE scope='system';
```

规则：`repo` / `user` scope 的凭据必然是个人创建的，source 必须 `user`；`global` scope 是系统签发的，source 为 `system`。

### 3.2 规范化 scope 和 source

scope 表示认领范围，source 表示凭据来源。

**scope 枚举（3 个值，去掉 system）：**

| scope | 含义 | 认领范围 |
|-------|------|---------|
| `global` | 全局 | 可认领所有 queued 任务 |
| `repo` | 绑定仓库 | 只认领该仓库的 queued 任务 |
| `user` | 绑定用户 | 只认领该用户的 queued 任务 |

**source 枚举（2 个值）：**

| source | 含义 |
|--------|------|
| `system` | 系统签发（管理员通过安装脚本/后台创建） |
| `user` | 用户签发（用户通过 Web UI 创建） |

**scope 和 source 的绑定关系：**

| scope | source | 说明 |
|-------|--------|------|
| `global` | `system` | 全局 worker 由系统签发 |
| `repo` | `user` | 仓库级 worker 由用户签发 |
| `user` | `user` | 个人 worker 由用户签发 |

注册时 scope+source 组合校验：当前阶段不做校验，后续架构稳定后补充增强，不影响现网功能。

### 3.3 简化 _check_task_access

当前阶段：**global 直接放行，repo/user 校验后放行**。

现有代码的问题：
- `scope='repo'` → 直接 403 "not implemented"——worker 认领任务后无法调 task_start/task_complete，任务卡死
- `scope='user'` → `cred.owner_id` 为空时 `int("")=0`，和 `task.user_id` 不匹配 → 403——worker 无法操作任务

```python
def _check_task_access(task_id: str, cred: CredentialContext) -> int:
    """BOLA 校验。当前阶段简化：global 放行，repo/user 校验后放行。
    
    核心原则：worker 认领了任务（claim_by_tag 已做 scope 过滤），
    后续操作（start/complete/fail/progress）必须放行，否则任务卡死。
    """
    if cred.scope == 'global':
        return _get_task_user_id(task_id)
    
    if cred.scope == 'repo':
        # repo scope：校验 task 的 repo_key 和 credential 的 repo_key 匹配
        # claim_by_tag 已做过仓库过滤，这里二次校验防止越权
        # 兼容两种格式：带前缀（gitcode:owner/repo）和不带前缀（owner/repo）
        _task_repo = _get_task_repo_key(task_id)
        if cred.repo_key and _task_repo:
            from src.repo_key import from_repo_key
            _cred_norm = (from_repo_key(cred.repo_key) or cred.repo_key).strip()
            _task_norm = (from_repo_key(_task_repo) or _task_repo).strip()
            if _cred_norm != _task_norm:
                raise HTTPException(status_code=403, detail={"code": 403, "message": "forbidden"})
        return _get_task_user_id(task_id)
    
    if cred.scope == 'user':
        _task_user_id = _get_task_user_id(task_id)
        try:
            _cred_user_id = int(cred.owner_id or 0)
        except (ValueError, TypeError):
            _cred_user_id = 0
        # owner_id 为空时宽松放行（兼容全局 worker fallback 为 user scope）
        if _cred_user_id and _task_user_id and _cred_user_id != _task_user_id:
            raise HTTPException(status_code=403, detail={"code": 403, "message": "forbidden"})
        return _task_user_id
    
    # 未知 scope → 按 global 放行
    return _get_task_user_id(task_id)
```

关键改动：
- 删除 `scope='system'` 分支和 scope_detail JSON 校验
- `repo` scope：校验 repo_key 匹配（替代直接 403），兼容带前缀和不带前缀两种格式
- `user` scope：owner_id 为空时宽松放行，非空时校验匹配
- 新增 `_get_task_repo_key` 辅助函数

需新增辅助函数：
```python
def _get_task_repo_key(task_id: str) -> str:
    """查 task 的 repo_key。"""
    from src.session_manager import db, db_lock
    with db_lock:
        c = db.cursor()
        c.execute("SELECT repo_key FROM agent_tasks WHERE id=?", (task_id,))
        row = c.fetchone()
    return row[0] or "" if row else ""
```

### 3.4 修复 scope='system' 硬编码 + claim BOLA 拦截

**4 处接口调整：**

| 接口 | 现有逻辑 | 改为 |
|------|---------|------|
| `recover-orphans` | 非 system 只能操作自己的 runtime_id | 非 global 才校验（保留） |
| `provider-configs` | 只 system 可访问 | 只 source=system 可访问（含明文 api_key，敏感信息） |
| `sandbox-paths` | 只 system 可访问 | 所有 scope 可访问（后续加强） |
| `skill-egress` | 只 system 可访问 | 所有 scope 可访问（后续加强） |

provider-configs 用 `source` 限制而非 `scope`——因为含明文 api_key 是凭据来源维度的问题（用户签发的凭据不应看到系统级 api_key），不是认领范围维度的问题。

**2 处 claim BOLA 拦截删除：**

claim 接口（claim / claim-by-tag）里的 BOLA 拦截逻辑（L723/L858）删除。claim_by_tag 已做 scope 过滤（global/repo/user 三分支），不需要在 API 层再拦截。

不兼容旧值——数据迁移时 `scope='system'` 统一改为 `global`，代码直接用 `global` 判断。

### 3.5 claim_by_tag 注释更新

claim_by_tag 里的 scope 过滤逻辑不变（global/repo/user 三分支），但：
- 删除 `system` scope 的隐式处理（fallback 到 global 分支）
- 注释更新为 `global / repo / user` 三值

### 3.6 CredentialContext 更新

```python
class CredentialContext:
    id: int
    source: str           # system / user（新增）
    scope: str            # global / repo / user（去掉 system）
    owner_name: str
    owner_id: str
    repo_key: str
    # scope_detail 字段删除（分支引入，非基线）
```

### 3.7 _auth_credential 更新

```python
return CredentialContext(
    id=row[0],
    source=row[7] or "system",  # 新增 source
    scope=_scope,
    owner_name=row[2] or "",
    owner_id=_owner_id,
    repo_key=row[4] or "",
)
```

SQL 查询加 source 列：
```python
c.execute(
    "SELECT rc.id, rc.scope, rc.owner_name, rc.owner_id, rc.repo_key, rc.source, "
    "ua.id "
    "FROM runtime_credentials rc "
    "LEFT JOIN user_accounts ua ON rc.owner_name = ua.username "
    "WHERE rc.token_hash=? AND rc.status='active'",
    (h,))
```

注意：去掉 `rc.scope_detail` 列（分支引入，删除），加 `rc.source` 列。row 索引相应调整。

### 3.8 删除 scope_detail（分支引入）

`scope_detail` 字段是 feat/devbox-sandbox-integration 分支引入的（commit ebae7f67），基线 weekly-05 没有。直接删除，不保留迁移逻辑。

- DB：`ALTER TABLE runtime_credentials DROP COLUMN scope_detail;`（一条 SQL，不在代码里体现建表逻辑）
- `src/api/db_setup.py`：删除 scope_detail 的 `_ensure_column` + 初始化逻辑（第 3991-4004 行）
- `src/api/routes/runtime.py`：删除 CredentialContext 里的 scope_detail 字段、_auth_credential 查询里的 scope_detail 列、_check_task_access 里的 scope_detail 校验逻辑
- `tests/`：3 个测试文件删 scope_detail，加 source，scope=system 改为 scope=global

### 3.9 db_setup.py 新增 source 建表逻辑

```python
# ===== runtime_credentials.source（凭据来源：system/user） =====
try:
    with db_lock:
        c = db.cursor()
        _ensure_column(c, "runtime_credentials", "source", "VARCHAR(32) NOT NULL DEFAULT 'system'")
        db.commit()
    log.info("Added source to runtime_credentials")
except Exception as e:
    log.warning(f"Add source to runtime_credentials failed: {e}")
```

## 4. 改动范围

| 文件/位置 | 改动 |
|-----------|------|
| DB | `runtime_credentials` 加 `source` 列，删 `scope_detail` 列，按 scope 迁移数据 |
| `src/api/db_setup.py` | 删 scope_detail 建表逻辑，加 source 建表逻辑 |
| `src/api/routes/runtime.py` CredentialContext | 加 source 字段，删 scope_detail 字段，scope 去掉 system |
| `src/api/routes/runtime.py` _auth_credential | SQL 删 scope_detail 列加 source 列，row 索引调整，返回 CredentialContext 带 source |
| `src/api/routes/runtime.py` _check_task_access | 删 system 分支 + scope_detail 校验，global 放行，repo 校验 repo_key（替代 403），user owner_id 空宽松放行 |
| `src/api/routes/runtime.py` 新增 _get_task_repo_key | 辅助函数，查 task 的 repo_key |
| `src/api/routes/runtime.py` 4 处 scope='system' | provider-configs 改 source=system 限制；sandbox-paths/skill-egress 放开；recover-orphans 改 global |
| `src/api/routes/runtime.py` 2 处 claim BOLA 拦截 | 删除 claim/claim-by-tag 里的 BOLA 拦截逻辑，宽松放行 |
| `src/runtime/service.py` claim_by_tag | 注释更新，scope 枚举改为 global/repo/user |
| `tests/test_fencing_and_double_signal.py` | 删 scope_detail，加 source |
| `tests/test_provider_config_dispatch.py` | 删 scope_detail，加 source，system 改 global |
| `tests/test_runtime_protocol.py` | 删 scope_detail，加 source，system 改 global，BOLA 测试用例更新 |

## 5. 向后兼容

- 现有 `scope='system'` 的凭据：数据迁移统一改为 `global` + `source=system`
- 现有 `scope='global'` 的凭据：数据迁移设 `source=system`，行为不变（global 直接放行）
- 现有 `scope='repo'`/`scope='user'` 的凭据：数据迁移设 `source=user`
- `scope_detail` 字段：分支引入，直接删除

## 6. 后续加强方向（架构稳定后）

1. **scope+source 组合校验**：注册时校验 scope 和 source 匹配关系，不匹配拒绝注册
2. **source 权限分离**：system source 可访问 provider-configs 等敏感接口，user source 不可
3. **BOLA 严格校验**：user scope 严格校验 task.user_id == cred.owner_id，空 owner_id 拒绝
4. **repo scope 严格匹配**：task 的 repo_key 和 credential 的 repo_key 严格匹配

## 7. 验证

#### 验证环境

- 主控：<controller-host>（内网）/<public-host>（公网），port 3000，nginx 80 反代到 3000
- worker：<worker-host>，tmux 会话，<repo-dir> 代码 + <venv>/bin/python
- DB：MySQL <db-host>，user=<db-user> pwd=<DB_PASSWORD> db=<your-db-name>
- worker token：<rta_worker_token>（credential_id=<id>, scope=global, source=system）
- runtime_id：<rid-hermes>(hermes)/<rid-opencode>(opencode)/<rid-codearts>(codearts)

#### 验证结果（2026-10-09 通过）

| 场景 | 配置 | 预期 | 实际 | 结果 |
|------|------|------|------|------|
| 11. global scope claim | scope=global, source=system | claim 不报 BOLA blocked | claim-by-tag 全部 200 OK | ✅ |
| 12. global scope task 操作 | scope=global 调 task_start | 放行不 403 | task_start 全部 200 OK | ✅ |
| 24. claim 不拦截 | scope=global claim 任务 | 不报 BOLA blocked，正常认领 | claim 200 OK | ✅ |

三引擎 A2A 任务全部 completed，无 BOLA 403 拦截。
