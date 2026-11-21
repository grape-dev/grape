"""派发身份解析服务：ORM 读账号 + 短 TTL 缓存 + 开关语义。

性能约定（每次派发只做一次判定）：
  · mode=off → 不进任何解析、不查库；
  · 默认 ON（未设置即生效）；显式 shadow 可观察、off 可回滚；
    同一账号在同一分钟内只查一次库；
  · 账号读取走 ORM（AccountRepository + 域引擎），不在本层写裸 SQL。
"""
from __future__ import annotations

import os
import threading
import time

from src.kernel.config import log
from src.domains.mgmt.auth.dispatch_identity.models import (
    DispatchIdentity, DispatchMode, IdentitySource)

try:  # 只用于分类：缺 DSN 与“查不到账号”必须区分（否则静默降级无从发现）
    from src.kernel.persistence.engine import DomainEngineUnavailable as _DSN_UNAVAILABLE
except Exception:  # pragma: no cover - 极端导入失败时不阻断
    class _DSN_UNAVAILABLE(RuntimeError):  # type: ignore[no-redef]
        domain = ""

_warned_domains: "set[str]" = set()


def _warn_unavailable_once(exc: Exception) -> None:
    """按域只告警一次：漏配 DSN 是部署问题，不该每次派发都刷日志。"""
    key = str(getattr(exc, "domain", "") or type(exc).__name__)
    if key in _warned_domains:
        return
    _warned_domains.add(key)
    log.warning(f"[dispatch-identity] account lookup degraded (once per domain): {exc}")


_INVALID_IDS = ("", "0", "none", "null", "undefined")
_MODE_ENV = "GRAPE_DISPATCH_USER_ID"
_cache: "dict[str, tuple[str, float]]" = {}
_cache_lock = threading.Lock()
_CACHE_MAX = 1024


def user_id_is_valid(value) -> bool:
    """有效 id：空 / '0' / None 等哨兵都算没有（与 D105 的 ''/'0' 口径一致）。"""
    return str(value or "").strip().lower() not in _INVALID_IDS


def current_mode() -> DispatchMode:
    return DispatchMode.parse(os.environ.get(_MODE_ENV))


def _cache_ttl() -> float:
    try:
        return max(0.0, float(os.environ.get("GRAPE_IDENTITY_CACHE_TTL", "60") or 60))
    except ValueError:
        return 60.0


def _account_id_by_username(username: str) -> str:
    """账号名 → 账号 id（ORM，读 mgmt 域引擎）；查不到返回空串。异常不打断派发。"""
    name = str(username or "").strip()
    if not name:
        return ""
    key = name.lower()
    now = time.monotonic()
    ttl = _cache_ttl()
    with _cache_lock:
        hit = _cache.get(key)
        if hit and hit[1] > now:
            return hit[0]
    uid = ""
    try:
        from src.domains.mgmt.user.account_session import account_session
        from src.kernel.persistence.repositories import AccountRepository

        # 连接选择属于 mgmt 域自己的数据访问实现（account_session 内部按域路由），
        # 契约调用方（runtime）不感知引擎/DSN。
        with account_session() as session:
            account = AccountRepository().get_by_username(session, name)
            uid = str((account.id if account else "") or "")
    except _DSN_UNAVAILABLE as e:  # 三库漏配：可见告警一次，仍降级为“空身份”
        _warn_unavailable_once(e)
        return ""
    except Exception as e:  # noqa: BLE001 - 认人失败不得打断派发
        log.debug(f"[dispatch-identity] account lookup failed name={name}: {e}")
        return ""
    if ttl > 0:
        with _cache_lock:
            if len(_cache) > _CACHE_MAX:
                _cache.clear()
            _cache[key] = (uid, now + ttl)
    return uid


def resolve_identity(*, task_user_id="", account_name="", actor_name="",
                     mode: "DispatchMode | None" = None) -> DispatchIdentity:
    """解析「这次派发该用谁的身份」；不看开关是否生效（mode 只进结果）。"""
    resolved_mode = mode or current_mode()
    if resolved_mode is DispatchMode.OFF:
        return DispatchIdentity(account_name=str(account_name or ""), mode=resolved_mode)
    if user_id_is_valid(task_user_id):
        return DispatchIdentity(user_id=str(task_user_id).strip(), source=IdentitySource.TASK,
                                account_name=str(account_name or ""), mode=resolved_mode)
    for name, source in ((account_name, IdentitySource.ACCOUNT), (actor_name, IdentitySource.ACTOR)):
        uid = _account_id_by_username(name)
        if uid:
            return DispatchIdentity(user_id=uid, source=source,
                                    account_name=str(account_name or ""), mode=resolved_mode)
    return DispatchIdentity(account_name=str(account_name or ""), mode=resolved_mode)


def dispatch_identity(*, task_user_id="", account_name="", actor_name="") -> DispatchIdentity:
    """派发侧入口：ON（默认）返回可写 id 并记一条绑定日志；SHADOW 只记录；OFF 静默。"""
    ident = resolve_identity(task_user_id=task_user_id, account_name=account_name,
                             actor_name=actor_name)
    if ident.has_candidate:
        if ident.mode is DispatchMode.SHADOW:
            log.info(f"[dispatch-identity] would bind user_id={ident.user_id} "
                     f"source={ident.source.value} account={ident.account_name or '-'} "
                     f"actor={actor_name or '-'} (mode=shadow, 不生效；开 on 才写 task.user_id)")
        elif ident.mode is DispatchMode.ON:
            log.info(f"[dispatch-identity] bound user_id={ident.user_id} "
                     f"source={ident.source.value} account={ident.account_name or '-'}")
    return ident
