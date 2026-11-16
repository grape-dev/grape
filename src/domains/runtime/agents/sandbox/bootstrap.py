"""沙箱组件公共初始化。

main.py（服务端入口）和 runtime/agent.py（Worker 入口）都调此函数。
保证两个入口走完全一致的沙箱组件初始化链路。

注入的组件：
- FileAuditLogger → sandbox_manager（审计日志）
- AlertEngine → sandbox_manager（告警引擎）
- NullSeccompProgram → sandbox_manager（seccomp 暂时禁用）
- PolicyManager(detector=RuleBasedDetector, guard=RegexCommandGuard, audit=FileAuditLogger)
- executor._audit = FileAuditLogger

幂等：重复调用不重复创建。
"""
from __future__ import annotations

import os
import threading
import time
from typing import Callable

from src.kernel.config import log


class SchemaState:
    """沙箱 schema 降级状态机。

    状态：NORMAL / DEGRADED
    降级后，init_sandbox_components() 调用时触发懒重试（节流 60s，加锁）。
    成功则恢复 NORMAL，重新初始化组件。

    状态机边界：
    - NORMAL → DEGRADED：只在启动时 ensure 失败后 mark_degraded
    - DEGRADED → NORMAL：只通过 try_recover，不提供 mark_recovered
    - 运行中不降级——运行中发现异常走各组件 deny 路径
    - 状态不持久化——重启重新试一次
    """

    RETRY_INTERVAL = 60.0  # 秒

    def __init__(self):
        self._degraded = False
        self._last_retry_ts = 0.0
        self._lock = threading.Lock()

    def mark_degraded(self, reason: str = "") -> None:
        """启动时 ensure 失败 → 标记降级。"""
        with self._lock:
            self._degraded = True
            self._last_retry_ts = time.time()
        log.error(f"sandbox schema degraded: {reason}")

    def is_degraded(self) -> bool:
        """读状态——不加锁（bool 读写原子）。"""
        return self._degraded

    def try_recover(self, ensure_fn: Callable[[], None]) -> bool:
        """懒重试。调用方传 ensure_fn，避免 SchemaState 依赖 DB。

        Returns: True 表示已恢复（需重新初始化组件），False 表示仍降级。
        """
        if not self._degraded:
            return False

        now = time.time()
        with self._lock:
            if not self._degraded:  # 双重检查
                return False
            if now - self._last_retry_ts < self.RETRY_INTERVAL:
                return False  # 节流中
            self._last_retry_ts = now

        # 锁外执行——ensure_fn 可能慢，不占锁
        try:
            ensure_fn()
        except Exception as e:
            from src.domains.runtime.agents.sandbox.schema import SchemaError
            if isinstance(e, SchemaError):
                log.error(f"schema conflict at runtime (needs restart): {e}")
            # TransientError 或其他——保持降级态等下次重试
            return False

        with self._lock:
            self._degraded = False
        log.error("sandbox schema recovered from degraded mode")
        return True

    def reset(self) -> None:
        """仅用于测试——回到初始状态。"""
        with self._lock:
            self._degraded = False
            self._last_retry_ts = 0.0


# 模块级单例——Worker 和 main 共用（app.state 覆盖不到 Worker）
_default_state = SchemaState()


def get_schema_state() -> SchemaState:
    return _default_state


_initialized = False
_sandbox_audit = None
_external_api_audit = None


def _resolve_audit_path(path: str, default_path: str) -> str:
    """审计日志路径三层 fallback。

    1. ConfigSync 给的路径（已校验过根目录/软链/..）
    2. /var/log/grape/（如果可写）
    3. ~/.grape/logs/（用户级）
    4. /tmp/grape-audit/（最后兜底，log.error 告警）

    返回最终路径。创建目录 + 写测试验证。
    """
    path = path or default_path
    audit_dir = os.path.dirname(path)

    # 尝试创建 + 验证可写
    candidates = [
        path,  # env 或默认 /var/log/grape/
        os.path.expanduser("~/.grape/logs/") + os.path.basename(path),
        "/tmp/grape-audit/" + os.path.basename(path),
    ]

    for candidate in candidates:
        try:
            d = os.path.dirname(candidate)
            os.makedirs(d, exist_ok=True)
            # 写测试
            with open(candidate + ".test", "w") as f:
                f.write("test")
            os.unlink(candidate + ".test")
            if candidate != path:
                log.error(f"audit path fallback: {path} not writable, using {candidate}")
            return candidate
        except (PermissionError, OSError):
            continue

    # 所有路径都不可写——最后兜底
    log.error(f"CRITICAL: no writable audit path, using /tmp fallback")
    return "/tmp/grape-audit/" + os.path.basename(default_path)


def init_sandbox_components() -> tuple:
    """初始化沙箱组件。main.py 和 runtime/agent.py 都调此函数。

    返回 (sandbox_audit, external_api_audit) 两个 FileAuditLogger 实例。
    幂等：重复调用返回已创建的实例。
    """
    global _initialized, _sandbox_audit, _external_api_audit
    from src.domains.runtime.agents.sandbox.schema import ensure_sandbox_tables
    _state = get_schema_state()
    if _initialized:
        # 降级态懒重试——已初始化但之前降级了，检查是否可恢复
        if _state.try_recover(lambda: ensure_sandbox_tables()):
            _initialized = False  # 恢复了，重新初始化组件
        else:
            return _sandbox_audit, _external_api_audit

    try:
        # P0: 先确保 DB 表存在（schema 独立模块，bootstrap 只调用）
        from src.domains.runtime.agents.sandbox.schema import SchemaError, TransientError
        import time as _time
        _ensure_ok = False
        for _attempt in range(3):
            try:
                ensure_sandbox_tables()
                _ensure_ok = True
                break
            except SchemaError as e:
                # schema 冲突——不可重试，fail-fast
                log.error(f"SCHEMA ERROR (fail-fast): {e}")
                raise
            except TransientError as e:
                # DB 临时不可用——重试
                log.warning(f"DB transient error (attempt {_attempt+1}/3): {e}")
                if _attempt < 2:
                    _time.sleep(1)
        if not _ensure_ok:
            # 重试 3 次仍失败——安全降级（deny-all）
            _state.mark_degraded("DB unavailable after 3 retries")

        import src.domains.runtime.agents.sandbox as _sb
        from src.domains.runtime.agents.sandbox.audit import FileAuditLogger
        from src.domains.runtime.agents.common.audit import NullAuditLogger
        from src.domains.runtime.agents.sandbox.isolation.seccomp import NullSeccompProgram
        from src.domains.runtime.agents.sandbox.alert import AlertEngine
        from src.domains.runtime.agents.sandbox.config_sync import get_config_sync

        # P0: 审计日志路径——从 ConfigSync 读（审计路径强约束）
        _cs = get_config_sync()
        _audit_path = _resolve_audit_path(
            _cs.get("audit_path"), "/var/log/grape/sandbox-audit.jsonl")
        _sandbox_audit = FileAuditLogger(_audit_path)

        # 注入给 sandbox_manager
        _sb.sandbox_manager = _sb.SandboxManager(
            audit=_sandbox_audit, seccomp=NullSeccompProgram(), alert_engine=AlertEngine())

        # external-api-audit（proxy + git_repo_api 共用）
        _ext_audit_path = _resolve_audit_path(
            _cs.get("ext_audit_path"), "/var/log/grape/external-api-audit.jsonl")
        _external_api_audit = FileAuditLogger(_ext_audit_path)

        log.info(f"sandbox audit logger initialized: {_audit_path}")
        log.info(f"external API audit logger initialized: {_ext_audit_path}")

        # 启动自检
        if isinstance(_sb.sandbox_manager._audit, NullAuditLogger):
            log.error(
                "PRODUCTION RISK: sandbox audit is NullAuditLogger — no audit logging!")

        # 注入 PolicyManager
        from src.domains.runtime.agents.policy import PolicyManager
        from src.domains.runtime.agents.policy.guard.prompt_injection import RuleBasedDetector
        from src.domains.runtime.agents.policy.guard.command_guard import RegexCommandGuard
        import src.domains.runtime.agents.policy as _pol
        _pol.policy_manager = PolicyManager(
            detector=RuleBasedDetector(),
            guard=RegexCommandGuard(),
            audit=_sandbox_audit,
        )
        if _pol.policy_manager.is_null:
            log.warning(
                "policy running in Null mode — no injection detection, no command guard")

        # executor 持有同一审计实例
        import src.domains.runtime.agents.executor as _exec
        _exec._audit = _sandbox_audit

        _initialized = True
        return _sandbox_audit, _external_api_audit

    except Exception as e:
        log.error(f"sandbox/policy init failed (fail-closed): {e}")
        # P0-11 修复：初始化失败时不标记 _initialized=True（was True → fail-open）
        # 系统在无审计/无沙箱/无策略下不应继续运行
        _initialized = False
        raise
