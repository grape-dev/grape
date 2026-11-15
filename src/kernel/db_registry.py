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
from src.kernel.config import log

import os
import threading
import time
from contextlib import contextmanager
from typing import Any
from urllib.parse import unquote, urlparse

_VALID = ("mgmt", "runtime", "insight")
_lock = threading.RLock()
_local = threading.local()
_conns: dict[str, Any] = {}          # 仅放域级锁（domain_lock），连接改线程本地
#: 连接失败后的禁用表：domain → 失败时间戳。
#: 为什么不是永久集合：首次连接失败常是瞬时故障（启动期 DB 未就绪 / 网络抖动），
#: 永久禁用会让该域在本进程整个生命周期内「静默不可用」，只能重启恢复。
_forbidden: dict[str, float] = {}
_FORBIDDEN_RETRY_SECONDS = 60.0
#: 缓存连接的存活校验节流：空闲超过该秒数才 ping，避免每次取连接都多一次往返。
_PING_IDLE_SECONDS = 30.0


def _thread_conns() -> dict[str, Any]:
    """当前线程的域连接表。

    为什么线程本地：pymysql 连接绑定创建它的线程，共享连接被请求工作线程借用会抛
    InterfaceError(0, '')（典型症状：跨域读的端点偶发「查询失败: (0, '')」）。
    """
    conns = getattr(_local, "conns", None)
    if conns is None:
        conns = {}
        _local.conns = conns
    return conns


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


class _CompatCursor:
    """把 sqlite 方言 SQL（? / INSERT OR IGNORE / PRAGMA）翻译成 MySQL 再执行。"""

    def __init__(self, raw):
        self._raw = raw
        self._pycur = None

    def execute(self, sql: str, params=None):
        """执行语句并返回**游标自身**（sqlite3 语义，与 MySQLCompatCursor 一致）。

        不能返回 pymysql 的 rowcount(int)：调用点普遍写作
        `c.execute(sql, params).fetchone()`（三库 lib 模式下域连接走的就是本兼容游标），
        返回 int 会让分布式锁协议第一步就抛
        `AttributeError: 'int' object has no attribute 'fetchone'`，
        进而让 skill sync / feedback reconcile / table maintenance 每轮都失败。
        需要行数的调用方读 `c.rowcount`（如 runtime public 的 exec_any）。
        """
        from src.kernel.mysql_compat import translate_sql

        sql2 = translate_sql(sql) or sql
        if params is not None and not isinstance(params, (tuple, list, dict)):
            params = (params,)
        if params is None:
            self._raw.execute(sql2)
        else:
            self._raw.execute(sql2, params)
        return self

    def executemany(self, sql: str, seq):
        """同 execute：执行后返回游标自身（sqlite3 语义）。"""
        from src.kernel.mysql_compat import translate_sql

        self._raw.executemany(translate_sql(sql) or sql, seq)
        return self

    def fetchall(self):
        return self._raw.fetchall()

    def fetchone(self):
        return self._raw.fetchone()

    def fetchmany(self, n=1):
        return self._raw.fetchmany(n)

    def close(self):
        return self._raw.close()

    def __getattr__(self, item):
        return getattr(self._raw, item)


class _CompatConn:
    """包装 pymysql 连接，返回带方言翻译的游标。"""

    def __init__(self, raw):
        self._raw = raw

    def cursor(self, *a, **kw):
        return _CompatCursor(self._raw.cursor(*a, **kw))

    def commit(self):
        return self._raw.commit()

    def rollback(self):
        return self._raw.rollback()

    def close(self):
        return self._raw.close()

    def ping(self, *a, **kw):
        return self._raw.ping(*a, **kw)

    def __getattr__(self, item):
        return getattr(self._raw, item)


def _new_conn(domain: str, autocommit: bool = True):
    import pymysql

    cfg = parse_dsn(dsn_of(domain))
    # connect_timeout=8s：pymysql 默认 10s，收紧 2s 与域读超时 10s 留余量
    conn = pymysql.connect(autocommit=autocommit, connect_timeout=8, **cfg)
    return _CompatConn(conn)


def _needs_liveness_check(domain: str) -> bool:
    """该域连接距上次确认存活是否已超过阈值（节流，避免每次多一次往返）。"""
    seen = getattr(_local, "alive_at", None)
    if not seen:
        return True
    return (time.time() - seen.get(domain, 0.0)) >= _PING_IDLE_SECONDS


def _mark_alive(domain: str) -> None:
    seen = getattr(_local, "alive_at", None)
    if seen is None:
        seen = {}
        _local.alive_at = seen
    seen[domain] = time.time()


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
    _forbid_at = _forbidden.get(domain)
    if _forbid_at is not None and (time.time() - _forbid_at) < _FORBIDDEN_RETRY_SECONDS:
        return None

    conns = _thread_conns()
    if domain in conns:
        # 存活校验：MySQL 会按 wait_timeout 断开空闲连接（或 DB 重启/网络抖动），
        # 缓存的连接一旦失效，后续所有调用都会抛 InterfaceError(0, '')——
        # 端点表现为 500（如 /api/chat/submit），且只能重启进程恢复。
        # 这里按空闲节流 ping(reconnect=True)：断开时 pymysql 自动重连。
        if _needs_liveness_check(domain):
            try:
                conns[domain].ping(reconnect=True)
                _mark_alive(domain)
            except Exception as _e:  # noqa: BLE001 - ping 失败即丢弃重建
                log.debug(f"domain conn {domain} ping failed, reconnecting: {_e}")
                try:
                    conns[domain].close()
                except Exception as _ce:
                    log.debug(f"close dead domain conn {domain} failed: {_ce}")
                conns.pop(domain, None)

    if domain in conns:
        return conns[domain]

    if lib_enabled() and dsn_of(domain):
        try:
            conn = _new_conn(domain)
        except Exception:  # noqa: BLE001 - 记录禁用时间戳，过期后重试
            _forbidden[domain] = time.time()
            return None
        _forbidden.pop(domain, None)
        _mark_alive(domain)
        conns[domain] = conn
        return conn

    if domain == current_domain():
        from src.kernel import session_manager

        return session_manager.db

    return None


def uses_own_dsn(domain: str) -> bool:
    """该域是否在用"自己的 DSN"（即表名必须裸写、不能再跨 schema）。"""
    return bool(lib_enabled() and dsn_of(domain) and domain_db(domain) is not None)


def new_connection(domain: str, autocommit: bool = True):
    """新建一条独立的域连接（不走共享缓存）。

    为什么需要：`domain_db()` 返回的是进程级共享连接，pymysql 连接绑定创建它的线程，
    在「进程域 ≠ 表所属域」的跨域路由里被工作线程复用会抛 InterfaceError(0, '')。
    这类调用点应每次取自己的连接，用完即关。
    """
    domain = (domain or "").lower()
    if domain not in _VALID:
        raise ValueError(f"unknown domain: {domain}")
    if not (lib_enabled() and dsn_of(domain)):
        raise RuntimeError(f"domain '{domain}' 无自有 DSN")
    return _new_conn(domain, autocommit=autocommit)


def connection_for(domain: str):
    """返回 (conn, qualifier)。

    qualifier(name) 给出**适配该连接**的表名：
      - 自己的 DSN（连接已绑定该库）→ 裸表名
      - 复用进程库（跨 schema 读）→ 带库前缀（t_<domain>()）
    """
    conn = domain_db(domain)
    if conn is None:
        raise RuntimeError(
            f"domain '{domain}' 连接不可用：需要 GRAPE_DSN_{domain.upper()} + GRAPE_DOMAIN_LIB=1"
        )
    if uses_own_dsn(domain):
        return conn, (lambda name: name)
    from src.kernel import db as _dbmod

    qual = getattr(_dbmod, f"t_{domain}", None)
    if qual is None:  # 未知域兜底：裸名
        return conn, (lambda name: name)
    return conn, qual


def domain_lock(domain: str) -> Any:
    """与该域连接配套的锁（跨域写需要串行化的地方用）。"""
    with _lock:
        key = f"lock:{domain}"
        if key not in _conns:
            _conns[key] = threading.RLock()
        return _conns[key]


@contextmanager
def transaction(domain: str):
    """域连接上的**显式事务**，供需要多语句原子性的组合写原语使用。

    为什么不用缓存的共享连接：`domain_db()` 的连接是进程级共享 + `autocommit=True`，
    在里面 `START TRANSACTION` 会把其它线程刚写的语句卷进本次事务（回滚时连带丢写）。
    这里为一次组合写单独开一条 `autocommit=False` 的连接，用完即关；
    组合写都是低频操作（引擎刷新 / 吊销 / 环境删除），连接开销可接受。
    """
    domain = (domain or "").lower()
    if domain not in _VALID:
        raise ValueError(f"unknown domain: {domain}")

    if lib_enabled() and dsn_of(domain):
        conn = _new_conn(domain, autocommit=False)
        c = conn.cursor()
        try:
            yield c
            conn.commit()
        except Exception:
            try:
                conn.rollback()
            except Exception as _e:
                log.debug(f"src/kernel/db_registry.py best-effort skipped: {_e}")
            raise
        finally:
            for _x in (c, conn):
                try:
                    _x.close()
                except Exception as _e:
                    log.debug(f"src/kernel/db_registry.py best-effort skipped: {_e}")
        return

    if domain == current_domain():
        from src.kernel import session_manager

        conn = session_manager.db
        c = conn.cursor()
        try:
            yield c
            conn.commit()
        except Exception:
            try:
                conn.rollback()
            except Exception as _e:
                log.debug(f"src/kernel/db_registry.py best-effort skipped: {_e}")
            raise
        finally:
            try:
                c.close()
            except Exception as _e:
                log.debug(f"src/kernel/db_registry.py best-effort skipped: {_e}")
        return

    raise RuntimeError(
        f"domain '{domain}' 事务不可用：需要设置 GRAPE_DSN_{domain.upper()} 且 GRAPE_DOMAIN_LIB=1"
    )


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
        except Exception as _e:
            log.debug(f"src/kernel/db_registry.py best-effort skipped: {_e}")


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


def reset_domain(domain: str, close: bool = True) -> None:
    """丢弃当前线程里该域的缓存连接（断线重连用），不影响其它域。"""
    dom = (domain or "").lower()
    conns = _thread_conns()
    conn = conns.pop(dom, None)
    if conn is not None and close:
        try:
            conn.close()
        except Exception as _e:
            log.debug(f"close domain conn {dom} failed: {_e}")
    _forbidden.pop(dom, None)
    seen = getattr(_local, "alive_at", None)
    if seen is not None:
        seen.pop(dom, None)


def reset(close: bool = True) -> None:
    """测试/排障用：清缓存（可顺带关闭自建连接）。"""
    conns = _thread_conns()
    for _k, _v in list(conns.items()):
        if close:
            try:
                _v.close()
            except Exception as _e:
                log.debug(f"src/kernel/db_registry.py best-effort skipped: {_e}")
    conns.clear()
    seen = getattr(_local, "alive_at", None)
    if seen is not None:
        seen.clear()
    with _lock:
        _conns.clear()          # 域级锁对象
        _forbidden.clear()
