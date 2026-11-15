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

_VALID = ("mgmt", "runtime", "insight")

_locks: dict[str, threading.RLock] = {}
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
        return self._conn().cursor(*args, **kwargs)

    def execute(self, sql: str, params=None):
        """与 MySQLCompatDB.execute 同语义：返回 cursor（调用方常用 cur.rowcount）。"""
        c = self._conn().cursor()
        if params is None:
            c.execute(sql)
        else:
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


_proxies: dict[str, _OwnerDB] = {}


def owner_db(domain: str) -> _OwnerDB:
    """取该域的 db 兼容代理（进程内单例）。"""
    dom = (domain or "").strip().lower()
    if dom not in _VALID:
        raise ValueError(f"unknown domain: {domain}")
    if dom not in _proxies:
        _proxies[dom] = _OwnerDB(dom)
    return _proxies[dom]


def owner_lock(domain: str) -> threading.RLock:
    """该域配套的进程级可重入锁。"""
    dom = (domain or "").strip().lower()
    if dom not in _VALID:
        raise ValueError(f"unknown domain: {domain}")
    with _locks_guard:
        if dom not in _locks:
            _locks[dom] = threading.RLock()
        return _locks[dom]


def db_pair(domain: str) -> tuple[_OwnerDB, threading.RLock]:
    """便捷组合：`db, db_lock = db_pair("insight")`。"""
    return owner_db(domain), owner_lock(domain)


__all__ = ["owner_db", "owner_lock", "db_pair"]