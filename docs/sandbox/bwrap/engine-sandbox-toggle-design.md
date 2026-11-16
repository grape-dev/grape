# bwrap 沙箱引擎维度可配置方案

## 1. 需求

1. bwrap 沙箱开关支持引擎维度配置：hermes/opencode/codex/codearts 各自独立开/关
2. 配置通过接口获取，全局开关和引擎维度开关都在 DB 里，动态可配
3. 未配置的引擎 fallback 到全局开关（DB engine_type='global' 行）；DB 无数据时 fallback 环境变量 GRAPE_AGENT_SANDBOX
4. 权限：worker 注册的 token 都能访问，不判断 scope
5. bwrap 安装了才走沙箱，没装降级到非沙箱

## 2. 现有链路

```
主控 env GRAPE_AGENT_SANDBOX=on/off
  → 心跳响应 sandbox_config.agent_sandbox（全局 on/off）
    → Worker ConfigSync.apply_remote() 合并
      → SandboxManager.prepare() 查 ConfigSync 判断开/关
        → 开 + bwrap 可用 → BwrapSandbox
        → 开 + bwrap 不可用 → 降级 NullSandbox 或 拒绝执行（按 mode）
        → 关 → NullSandbox
```

现在只有全局一个开关，不区分引擎。

## 3. 方案

### 3.1 新增 DB 表

```sql
CREATE TABLE engine_sandbox_toggle (
    id          INT AUTO_INCREMENT PRIMARY KEY,
    engine_type VARCHAR(64)  NOT NULL UNIQUE,  -- global / hermes / opencode / codex / codearts
    sandbox_on  TINYINT(1)   NOT NULL DEFAULT 1 COMMENT '0=off 1=on, 默认开启',
    created_at  TEXT,
    updated_at  TEXT
);
```

engine_type 枚举值：

| engine_type | 含义 |
|-------------|------|
| `global` | 全局开关，未配引擎的 fallback 值 |
| `hermes` | hermes 引擎独立开关 |
| `opencode` | opencode 引擎独立开关 |
| `codex` | codex 引擎独立开关 |
| `codearts` | codearts 引擎独立开关 |

- `global` 行：全局开关，默认 on。DB 查不到时 fallback 环境变量 `GRAPE_AGENT_SANDBOX`。
- 引擎行：引擎维度独立开关。未配的引擎 fallback 到 `global` 行的值。
- 一张表搞定全局 + 引擎维度，一次 SQL 查出。

### 3.2 主控：心跳下发 + 查询接口

#### 心跳响应

`src/api/routes/runtime.py` 心跳响应：一次 SQL 查出 global 行 + 引擎行，内存分组。

```python
# 沙箱开关从 engine_sandbox_toggle 表读取（global 行 + 引擎行），一次 SQL
_global_on = True
_engine_overrides = {}
try:
    from src.session_manager import db, db_lock
    with db_lock:
        c = db.cursor()
        c.execute("SELECT engine_type, sandbox_on FROM engine_sandbox_toggle")
        for _et, _on in c.fetchall():
            if _et == "global":
                _global_on = bool(_on)
            else:
                _engine_overrides[_et] = "on" if _on else "off"
except Exception:
    _global_on = os.environ.get("GRAPE_AGENT_SANDBOX", "on").lower() in ("1", "true", "on", "yes")

_new_cfg = {
    "agent_sandbox": "on" if _global_on else "off",
    "sandbox_mode": os.environ.get("GRAPE_SANDBOX_MODE", "preferred").lower(),
    "audit_path": os.environ.get("GRAPE_SANDBOX_AUDIT_PATH", "/var/log/grape/sandbox-audit.jsonl"),
    "ext_audit_path": os.environ.get("GRAPE_EXTERNAL_API_AUDIT_PATH", "/var/log/grape/external-api-audit.jsonl"),
    "extra_whitelist": os.environ.get("GRAPE_SANDBOX_EXTRA_WHITELIST", ""),
}
# config_version 在配置变更时递增（全局配置或引擎维度配置变更都触发）
global _sandbox_config_version, _sandbox_config_cache
_new_full_cfg = {**_new_cfg, "engine_overrides": _engine_overrides}
if _sandbox_config_cache != _new_full_cfg:
    _sandbox_config_cache = dict(_new_full_cfg)
    _sandbox_config_version += 1
_sandbox_config = {
    "config_version": _sandbox_config_version,
    **_new_cfg,
    "engine_overrides": _engine_overrides,
}
```

#### 查询接口

```python
@router.get("/admin/sandbox/engines", operation_id="runtime_sandbox_engines",
            summary="查询引擎维度沙箱开关")
def get_engine_sandbox_config(user: dict = Depends(require_no_session_token)):
    """查询沙箱开关配置（全局 + 引擎维度）。

    worker 注册的 token 均可访问，不判断 scope。
    当前不支持接口更新，配置通过 DB 直接操作。

    Returns:
        global_sandbox_on: 全局开关，默认 True。优先读 DB global 行，fallback 环境变量。
        engines: 引擎维度配置列表，未配的引擎 fallback 到全局
    """
    from src.session_manager import db, db_lock
    # 一次 SQL 查出全局 + 引擎维度
    _global_on = True
    _engines = []
    try:
        with db_lock:
            c = db.cursor()
            c.execute("SELECT engine_type, sandbox_on FROM engine_sandbox_toggle")
            for _et, _on in c.fetchall():
                if _et == "global":
                    _global_on = bool(_on)
                else:
                    _engines.append({"engine_type": _et, "sandbox_on": bool(_on)})
    except Exception:
        _global_on = os.environ.get("GRAPE_AGENT_SANDBOX", "on").lower() in ("1", "true", "on", "yes")
    return {
        "global_sandbox_on": _global_on,
        "engines": _engines,
    }
```

### 3.3 Worker：ConfigSync 按引擎合并

`src/agent/sandbox/config_sync.py`：

#### _SECURITY_DEFAULTS 增加 engine_overrides

```python
_SECURITY_DEFAULTS = {
    "agent_sandbox": "on",
    "sandbox_mode": "required",
    "extra_whitelist": "",
    "engine_overrides": {},  # 引擎维度沙箱开关默认空，未配的引擎 fallback 到全局 agent_sandbox
}
```

#### apply_remote 增加 engine_overrides

```python
def apply_remote(self, config: dict, version: int) -> bool:
    ...
    new_snapshot["agent_sandbox"] = self._merge_agent_sandbox(config)
    new_snapshot["sandbox_mode"] = self._merge_sandbox_mode(config)
    new_snapshot["extra_whitelist"] = self._merge_whitelist(config)
    new_snapshot["engine_overrides"] = self._merge_engine_overrides(config)
    ...
```

#### _merge_engine_overrides

```python
def _merge_engine_overrides(self, remote: dict) -> dict[str, str]:
    """合并引擎维度开关。

    直接用远程下发的 engine_overrides，不做本地覆盖。
    全局开关只在引擎未配置时作为 fallback（在 get_engine_sandbox 里处理）。
    """
    remote_overrides = remote.get("engine_overrides", {})
    if not isinstance(remote_overrides, dict):
        return {}
    return {k: v.lower() for k, v in remote_overrides.items()}
```

#### 新增 get_engine_sandbox

```python
def get_engine_sandbox(self, engine_type: str) -> str:
    """查指定引擎的沙箱开关。

    1. engine_overrides 里有该引擎 → 返回该值
    2. 未配置 → fallback 到全局 agent_sandbox（默认 on）

    全局开关只影响未配置的引擎，已配置的引擎按自己的值走。
    TTL 过期时：_SECURITY_DEFAULTS 里 engine_overrides={}，
    所有引擎 fallback 到全局 agent_sandbox=on（安全默认）。
    """
    with self._lock:
        if self._remote_fail and (time.time() - self._last_good_ts > _TTL):
            _defaults = _SECURITY_DEFAULTS
            overrides = _defaults.get("engine_overrides", {})
            if engine_type in overrides:
                return overrides[engine_type]
            return _defaults.get("agent_sandbox", "on")

        overrides = self._snapshot.get("engine_overrides", {})
        if engine_type in overrides:
            return overrides[engine_type]
        return self._snapshot.get("agent_sandbox", "on")
```

### 3.4 实时感知 + 不影响执行中任务

#### 实时感知

- worker 心跳间隔默认 15 秒（`RUNTIME_HEARTBEAT_INTERVAL=15`）
- 主控在心跳响应中下发 `engine_overrides` + `agent_sandbox`（全局）
- worker 收到后通过 `ConfigSync.apply_remote()` 原子更新快照
- config_version 在配置变更时递增，worker 检测到版本变化才 apply
- 配置变更最多 15 秒后对新任务生效

#### 不影响已执行任务

- `sandbox_manager.prepare()` 在**每个任务启动前**调一次
- sandbox 生命周期 = 任务生命周期（`__enter__` → 任务执行 → `__exit__`）
- 开关变更只影响**后续新认领的任务**，正在执行的任务不受影响

#### 远程不可达时的降级

- 心跳失败：`ConfigSync.on_heartbeat_fail()` 标记远程不可达，保持上次已知良好配置
- 超过 TTL（60 秒）：fallback 到 `_SECURITY_DEFAULTS`（engine_overrides={}，全局 on）
- 含义：远程不可达时，所有引擎按全局开关走（已配置的引擎也回到全局默认）

#### 已知限制（基线已有，本次不改动）

1. **TTL 硬编码 60 秒**：`_TTL = 60.0` 是常量，未按心跳间隔比例设值。心跳 15 秒时容忍 4 次失败，合理但偏紧。后续应改为 `N × heartbeat_interval`（N=3~5），或通过环境变量配置。

2. **失败类型不区分**：`on_heartbeat_fail()` 对 TCP 连不上、超时、5xx、4xx 一视同仁。正确做法：
   - TCP 连不上 → 本机网络抖动，不降级（或延长 TTL）
   - 连上但超时 → 服务过载，降级
   - 5xx → 服务故障，降级
   - 4xx → 配置请求本身有 bug，报错不降级（否则掩盖 bug）

   本次改动复用现有 `on_heartbeat_fail()` 和 TTL 机制，engine_overrides 的降级行为和现有 agent_sandbox 完全一致，不引入新的降级路径。这两个问题应作为独立 issue 修复，不在本次范围。

### 3.5 Worker：SandboxManager 按引擎决策

`src/agent/sandbox/manager.py` 的 `prepare()`：**只改第 2 步（开关检查），mode 决策和 fail-closed 逻辑不变。**

现有 prepare() 流程：
1. `get_sandbox_mode()` → 查 mode（REQUIRED/PREFERRED/DISABLED）
2. **开关检查**（本次改动点）
3. Provider 解析 + bwrap 可用性检测
4. fail-closed 决策（REQUIRED+bwrap不可用→拒绝，PREFERRED+bwrap不可用→降级）

改动：第 2 步从查全局 `agent_sandbox` 改为查引擎维度 `get_engine_sandbox(exec_backend)`：

```python
# 2. 沙箱开关——从 ConfigSync 读引擎维度开关（未配的引擎 fallback 到全局）
from src.agent.sandbox.config_sync import get_config_sync
_cs = get_config_sync()
_engine_sandbox = _cs.get_engine_sandbox(exec_backend).lower()
if _engine_sandbox not in ("1", "true", "on", "yes"):
    log.warning(
        f"sandbox disabled by config (engine_sandbox={_engine_sandbox} "
        f"backend={exec_backend}, mode={mode.value})")
    self._audit.log(AuditEvent(
        ts=self._now(), task_id=ctx.task_id,
        kind=AuditKind.POLICY_DECISION, outcome="degrade",
        detail={"reason": f"engine_sandbox=off (engine={exec_backend})",
                "backend": exec_backend, "mode": mode.value}))
    return self._create_null(f"engine_sandbox=off ({exec_backend})")
```

第 3-4 步（Provider 解析 + fail-closed 决策）不变。bwrap 没装时：
- mode=REQUIRED → raise SandboxUnavailableError（拒绝执行）
- mode=PREFERRED → NullSandbox（降级 + 审计）

### 3.6 完整决策链

```
查 engine_sandbox_toggle 表 → 该引擎配了 on/off？
  - 配了 on → 走沙箱
  - 配了 off → 不走沙箱
  - 没配 → fallback 全局（DB global 行）
    - global on → 走沙箱
    - global off → 不走沙箱
    - DB 无 global 行 → fallback 环境变量 GRAPE_AGENT_SANDBOX
↓ 确定要走沙箱后
检测 bwrap 是否安装
  - 安装了 → BwrapSandbox
  - 没装 + mode=REQUIRED → 拒绝执行
  - 没装 + mode=PREFERRED → 降级 NullSandbox
```

全局开关只影响"没配的引擎"。配了的引擎按自己的值走，全局开关管不到。

### 3.7 改动范围

| 文件 | 改动 |
|------|------|
| DB | 新建 engine_sandbox_toggle 表（engine_type 含 global + 引擎枚举） |
| `src/api/routes/runtime.py` | 心跳响应加 engine_overrides + 全局开关读 DB + 新增查询接口 |
| `src/agent/sandbox/config_sync.py` | _SECURITY_DEFAULTS 加 engine_overrides + _merge_engine_overrides + get_engine_sandbox() |
| `src/agent/sandbox/manager.py` | prepare() 第 2 步：全局开关 → 引擎维度开关 |

### 3.8 向后兼容

- engine_sandbox_toggle 表为空 → 一次 SQL 返回空 → 全局 fallback 环境变量 GRAPE_AGENT_SANDBOX → 行为和现在一致
- 旧 worker 不读 engine_overrides → 行为不变
- DB 有 global 行时，环境变量不再生效（DB 优先）；DB 无 global 行时，环境变量 fallback

### 3.9 验证

#### 验证环境

- 主控：<controller-host>（内网）/<public-host>（公网），port 3000，nginx 80 反代到 3000
- worker：<worker-host>，tmux 会话，<repo-dir> 代码 + <venv>/bin/python
- DB：MySQL <db-host>，user=<db-user> pwd=<DB_PASSWORD> db=<your-db-name>
- worker token：<rta_worker_token>（credential_id=<id>, scope=global, source=system）
- runtime_id：<rid-hermes>(hermes)/<rid-opencode>(opencode)/<rid-codearts>(codearts)
- DB 数据：global=1(on), hermes=1(on), codearts=0(off), codex=0(off), opencode 未配(fallback global on)

#### 验证结果（2026-10-09 通过）

| 场景 | 配置 | 预期 | 实际 | 结果 |
|------|------|------|------|------|
| 1. 引擎显式开 | hermes=1 | bwrap 沙箱 | hermes 在 bwrap 沙箱内执行 | ✅ |
| 2. 引擎显式关 | codearts=0 | 无沙箱 | codearts 执行无沙箱，task_start 200 | ✅ |
| 3. 引擎未配 fallback 全局开 | opencode 未配, global=1 | bwrap 沙箱 | opencode 在 bwrap 沙箱内执行 | ✅ |
| 4. 查询接口 | GET /admin/sandbox/engines | 返回 global+engines | {global:true, hermes:true, codearts:false, codex:false} | ✅ |

三引擎 A2A 任务（#91/#92/#93）全部 completed，无 BOLA 拦截，无 403 权限错误。

#### 验证中发现的 BUG：UnboundLocalError（commit 52257ba1 已修复）

**问题**：`runtime_heartbeat` 和 `get_engine_sandbox_config` 函数内有 `from src.session_manager import db, db_lock` 局部导入，导致 Python 将 `db_lock` 视为局部变量。函数内在 import 行之前使用 `db_lock`（心跳函数 line 419 `with db_lock:`）时报 `UnboundLocalError: cannot access local variable 'db_lock' where it is not associated with a value`。

**根因**：Python 作用域规则——函数内任何赋值（含 import）使变量变局部。模块级已导入 `from src.session_manager import db, db_lock`（line 15），函数内不需要重复局部导入。

**修复**：删除 `runtime_heartbeat` 和 `get_engine_sandbox_config` 中的 `from src.session_manager import db, db_lock` 局部导入，直接使用模块级导入。

#### 验证中发现的坑：worker RUNTIME_SERVER_URL 必须走公网

**问题**：worker `RUNTIME_SERVER_URL` 配成内网 `http://192.168.0.115:3000` 时，bwrap 沙箱内 grape CLI 调主控 API 被 NetworkProxy deny list 拦截（内网 IP 段 192.168.0.0/16 在 default deny list 中），返回 403。

**根因**：bwrap NetworkProxy 有 IP deny list 拦截私有网络地址。白名单虽包含 GRAPE_SERVER_URL 的 hostname，但 deny list 优先级更高。

**修复**：worker 必须走公网 `http://1.92.115.229`（nginx 80 反代到 3000），不能走内网 IP。验证前确认 nginx 已启动且 229:80 可达。
