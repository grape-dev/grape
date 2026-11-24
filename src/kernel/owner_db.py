"""跨域 owner 连接代理：让「域 A 的代码在域 B 的进程里跑」时仍写/读 owner 库。

背景（三库独立 A1/A3）：拆库后同一段代码可能被非 owner 进程调用——
例如 insight 的可观测性模块被 mgmt 进程的定时器/路由调用。它若继续用
`src.kernel.session_manager.db`（进程本域连接），SQL 就会打到 mgmt 库，
得到 `1146 Table 'grape_stage_mgmt.obs_*' doesn't exist`（被 except 吞成空结果）。

用法（替换函数内的局部 import，语义不变）：

    from src.kernel.owner_db import db_pair
    db, db_lock = db_pair("insight")

    c = db.cursor()
    c.execute("SELECT ... FROM <本域表> WHERE ...")

解析规则（与 db_registry.domain_db 一致，线程安全）：
  1) GRAPE_DOMAIN_LIB=1 且设了 GRAPE_DSN_<DOMAIN> → 该域的（线程本地）自有连接，
     方言翻译由 db_registry 的 _CompatConn 负责（? → %s 等）；
  2) 当前进程域 == 目标域（或单库拓扑无 DSN）→ 回落 session_manager.db（零回归）；
  3) 其它 → 同样回落进程库（宁可保持旧行为，也不抛错阻断调用方）。

锁：每个域一把进程级 RLock（与 session_manager.db_lock 的用法对齐），
用于保护「多语句 + commit」这类需要串行化的段落；单条读写不需要它。
"""
from __future__ import annotations

import threading
from typing import Any

from src.kernel.db_lock_metrics import TrackedRLock

_VALID = ("mgmt", "runtime", "insight")

_locks: dict[str, "TrackedRLock"] = {}
_locks_guard = threading.Lock()


class _OwnerDB:
    """db 兼容壳：cursor()/execute()/commit()/rollback() 转发到 owner 域连接。"""

    __slots__ = ("domain",)

    def __init__(self, domain: str):
        self.domain = domain

    # ---- 连接解析 ----
    def _conn(self) -> Any:
        from src.kernel import db_registry
        try:
            conn = db_registry.domain_db(self.domain)
        except Exception:  # noqa: BLE001 - 解析失败一律回落进程库
            conn = None
        if conn is not None:
            return conn
        from src.kernel.session_manager import db as _process_db
        return _process_db

    # ---- db 接口 ----
    def cursor(self, *args, **kwargs):
        return _RetryCursor(self)

    def execute(self, sql: str, params=None):
        """与 MySQLCompatDB.execute 同语义：返回 cursor（调用方常用 cur.rowcount）。"""
        c = _RetryCursor(self)
        c.execute(sql, params)
        return c

    def commit(self):
        return self._conn().commit()

    def rollback(self):
        try:
            return self._conn().rollback()
        except Exception:  # noqa: BLE001 - 与 session_manager 一样对回滚失败宽容
            return None

    def __getattr__(self, item):  # pragma: no cover - 兜底转发（如 executescript）
        return getattr(self._conn(), item)

    def __repr__(self) -> str:
        return f"<owner_db {self.domain}>"


def _is_disconnect(exc: BaseException) -> bool:
    """pymysql 断线类异常：InterfaceError(0, '')、OperationalError 2006/2013、ConnectionError。"""
    try:
        import pymysql
    except Exception:  # pragma: no cover - 无 pymysql 时按类型名兜底
        pymysql = None  # type: ignore[assignment]
    if pymysql is not None:
        if isinstance(exc, (pymysql.err.InterfaceError, pymysql.err.OperationalError)):
            code = exc.args[0] if exc.args else None
            if code in (0, 2006, 2013, 2014):
                return True
        if isinstance(exc, pymysql.err.InterfaceError):
            return True
    name = type(exc).__name__
    return name in ("InterfaceError", "ConnectionError", "ConnectionRefusedError",
                    "ConnectionResetError", "BrokenPipeError")


class _RetryCursor:
    """游标包装：遇到「连接已断」类错误时重建该域连接并重试一次。

    为什么需要：域连接是线程本地缓存 + 长连接，MySQL 按 wait_timeout 断开空闲连接后，
    下一次执行必然抛 InterfaceError(0, '')（线上表现：/api/chat/submit 500，
    日志里先出现 "get_meta_value failed: (0, '')"）。domain_db 的空闲 ping 能覆盖大多数
    情况，但「ping 之后、执行之前」断线仍可能发生，这一层保证业务调用不会直接 500。
    """

    __slots__ = ("_proxy", "_cur")

    def __init__(self, proxy: "_OwnerDB"):
        self._proxy = proxy
        self._cur = None

    def _cursor(self):
        if self._cur is None:
            self._cur = self._proxy._conn().cursor()
        return self._cur

    def _reopen(self):
        from src.kernel import db_registry
        db_registry.reset_domain(self._proxy.domain)
        self._cur = None

    def execute(self, sql: str, params=None):
        try:
            c = self._cursor()
            return c.execute(sql) if params is None else c.execute(sql, params)
        except Exception as exc:  # noqa: BLE001 - 仅在断线类错误上重试
            if not _is_disconnect(exc):
                raise
            self._reopen()
            c = self._cursor()
            return c.execute(sql) if params is None else c.execute(sql, params)

    def executemany(self, sql: str, seq):
        try:
            return self._cursor().executemany(sql, seq)
        except Exception as exc:  # noqa: BLE001
            if not _is_disconnect(exc):
                raise
            self._reopen()
            return self._cursor().executemany(sql, seq)

    def __getattr__(self, item):
        return getattr(self._cursor(), item)


_proxies: dict[str, _OwnerDB] = {}


def owner_db(domain: str) -> _OwnerDB:
    """取该域的 db 兼容代理（进程内单例）。"""
    dom = (domain or "").strip().lower()
    if dom not in _VALID:
        raise ValueError(f"unknown domain: {domain}")
    if dom not in _proxies:
        _proxies[dom] = _OwnerDB(dom)
    return _proxies[dom]


def owner_lock(domain: str) -> "TrackedRLock":
    """该域配套的进程级可重入锁（TrackedRLock：与 RLock 接口一致，额外记录等待/持有）。"""
    dom = (domain or "").strip().lower()
    if dom not in _VALID:
        raise ValueError(f"unknown domain: {domain}")
    with _locks_guard:
        if dom not in _locks:
            _locks[dom] = TrackedRLock()
        return _locks[dom]


def db_pair(domain: str) -> tuple[_OwnerDB, "TrackedRLock"]:
    """便捷组合：`db, db_lock = db_pair("insight")`。"""
    return owner_db(domain), owner_lock(domain)


__all__ = ["owner_db", "owner_lock", "db_pair"]