"""域连接注册表（lib 模式的地基）。

目标：每个进程可以有多个域的连接池；域 lib 用它自己的 DSN，而不是"进程当前库"。
这样可以做到：mgmt 进程连 mgmt(.59) + runtime(.243) + insight(.164)，代码不改。

配置：
    GRAPE_DSN_MGMT=mysql://user:pass@host:3306/db
    GRAPE_DSN_RUNTIME=...
    GRAPE_DSN_INSIGHT=...
未配置时：
    - 若请求的域 == 当前进程域（DEVOPS_DB_NAME / GRAPE_PROCESS_ROLE）→ 复用 session_manager.db（存量零回归）
    - 否则 → 返回 None，调用方必须显式降级（禁止静默串库）

开关：
    GRAPE_DOMAIN_LIB=1 启用 lib 模式的跨域连接（默认 0，保持"仅本域"行为）
"""
from __future__ import annotations

import os
import threading
from contextlib import contextmanager
from typing import Any
from urllib.parse import unquote, urlparse

_VALID = ("mgmt", "runtime", "insight")
_lock = threading.RLock()
_conns: dict[str, Any] = {}
_forbidden: set[str] = set()


def _domain_of(name: str) -> str | None:
    low = (name or "").strip().lower()
    if not low:
        return None
    if low.startswith("grape_dev_"):
        low = low[len("grape_dev_"):]
    if low.startswith("grape_") and low.endswith("_db"):
        low = low[len("grape_"):-len("_db")]
    for d in _VALID:
        if low == d or low.startswith(d + "_") or low.endswith("_" + d):
            return d
    return None


def current_domain() -> str | None:
    for key in ("DEVOPS_DB_NAME", "GRAPE_PROCESS_ROLE"):
        d = _domain_of(os.environ.get(key, ""))
        if d:
            return d
    return None


def lib_enabled() -> bool:
    return os.environ.get("GRAPE_DOMAIN_LIB", "0").strip().lower() in ("1", "true", "yes", "on")


def dsn_of(domain: str) -> str:
    return os.environ.get(f"GRAPE_DSN_{domain.upper()}", "").strip()


def parse_dsn(dsn: str) -> dict:
    """mysql://user:pass@host:port/db?charset=utf8mb4 → kwargs。"""
    u = urlparse(dsn)
    if u.scheme not in ("mysql", ""):
        raise ValueError(f"unsupported DSN scheme: {u.scheme}")
    return {
        "host": u.hostname or "127.0.0.1",
        "port": int(u.port or 3306),
        "user": unquote(u.username or ""),
        "password": unquote(u.password or ""),
        "database": (u.path or "/").lstrip("/").split("?")[0],
        "charset": "utf8mb4",
    }


def _new_conn(domain: str):
    import pymysql

    cfg = parse_dsn(dsn_of(domain))
    conn = pymysql.connect(autocommit=True, connect_timeout=8, **cfg)
    return conn


def domain_db(domain: str):
    """取该域的连接；不可用时返回 None（调用方必须显式处理）。

    规则：
      1) 有 GRAPE_DSN_<DOMAIN> 且 GRAPE_DOMAIN_LIB=1 → 用该 DSN 建（缓存）连接
      2) 域 == 当前进程域 → 复用 session_manager.db（零回归）
      3) 其它 → None
    """
    domain = (domain or "").lower()
    if domain not in _VALID:
        raise ValueError(f"unknown domain: {domain}")
    if domain in _forbidden:
        return None

    with _lock:
        if domain in _conns:
            return _conns[domain]

    if lib_enabled() and dsn_of(domain):
        try:
            conn = _new_conn(domain)
        except Exception:  # noqa: BLE001 - 记录禁用，避免每次调用都重试
            _forbidden.add(domain)
            return None
        with _lock:
            _conns[domain] = conn
        return conn

    if domain == current_domain():
        from src.kernel import session_manager

        return session_manager.db

    return None


def domain_lock(domain: str) -> Any:
    """与该域连接配套的锁（跨域写需要串行化的地方用）。"""
    with _lock:
        key = f"lock:{domain}"
        if key not in _conns:
            _conns[key] = threading.RLock()
        return _conns[key]


@contextmanager
def cursor(domain: str):
    conn = domain_db(domain)
    if conn is None:
        raise RuntimeError(
            f"domain '{domain}' 连接不可用：需要设置 GRAPE_DSN_{domain.upper()} 且 GRAPE_DOMAIN_LIB=1"
        )
    c = conn.cursor()
    try:
        yield c
    finally:
        try:
            c.close()
        except Exception:  # noqa: BLE001
            pass


def health() -> dict:
    """给 /api/health 或自检用：每个域的连通性。"""
    out = {}
    for d in _VALID:
        conn = domain_db(d)
        if conn is None:
            out[d] = {"configured": bool(dsn_of(d)), "ok": False}
            continue
        try:
            c = conn.cursor()
            c.execute("SELECT 1")
            c.fetchone()
            c.close()
            out[d] = {"configured": bool(dsn_of(d)), "ok": True}
        except Exception as exc:  # noqa: BLE001
            out[d] = {"configured": bool(dsn_of(d)), "ok": False, "error": str(exc)[:120]}
    return out


def reset(close: bool = True) -> None:
    """测试/排障用：清缓存（可顺带关闭自建连接）。"""
    with _lock:
        for k, v in list(_conns.items()):
            if not k.startswith("lock:") and close:
                try:
                    v.close()
                except Exception:  # noqa: BLE001
                    pass
        _conns.clear()
        _forbidden.clear()