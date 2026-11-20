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
_forbidden: set[str] = set()         # 永久禁用（配置/鉴权类错误：重试也不会好）
_retry_after: dict[str, float] = {}  # 临时失败的退避时间点（monotonic 秒）
_RETRY_BACKOFF_S = 5.0
#: 缓存连接的存活校验节流：空闲超过该秒数才 ping，避免每次取连接都多一次往返。
_PING_IDLE_SECONDS = 30.0
# 连接一次失败就永久禁用整个域，是 2026-11-15 事故的放大器：
# 本地 runtime MySQL 打满 max_connections（1040）后，mgmt 进程把 runtime 拉黑，
# 之后所有跨域读（meta / scene_category / skill）静默回落到进程库 → 满屏 1146。
# 因此只有「重试也不会成功」的错误才进 _forbidden，其余按 5s 退避重试。
_PERMANENT_ERRNOS = {
    1044,  # ER_DBACCESS_DENIED_ERROR
    1045,  # ER_ACCESS_DENIED_ERROR
    1049,  # ER_BAD_DB_ERROR
    1142,  # ER_TABLEACCESS_DENIED_ERROR
    1143,  # ER_COLUMNACCESS_DENIED_ERROR
    1698,  # ER_ACCESS_DENIED_NO_PASSWORD_ERROR
    2005,  # ER_UNKNOWN_HOST（域名解析失败属于配置错误）
}


def _is_permanent_connect_error(exc: BaseException) -> bool:
    args = getattr(exc, "args", ()) or ()
    if args and isinstance(args[0], int) and args[0] in _PERMANENT_ERRNOS:
        return True
    msg = str(exc).lower()
    return any(s in msg for s in ("access denied", "unknown database",
                                 "unknown mysql server host", "no such file or directory"))


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
    """跨域 lib 模式是否启用。

    口径改为「**有 DSN 就算启用**」：三库部署先把 DSN 文件放到约定的 systemd env 路径
    （GRAPE_3DB_ENV 指向它）并让进程看到 GRAPE_DSN_*，能力就有了，不该再额外要求一个
    GRAPE_DOMAIN_LIB=1。
    少一个开关 = 少一个漏配点（这个开关漏配的后果尤其阴险：*_any 全部静默回落到
    进程主库，三库看起来"照常工作"，直到某个跨域查询撞上 1146）。

    显式置 0 仍然尊重（单库回归/测试要关掉跨域能力时用）。
    """
    explicit = os.environ.get("GRAPE_DOMAIN_LIB", "").strip().lower()
    if explicit in ("0", "false", "no", "off"):
        return False
    if explicit in ("1", "true", "yes", "on"):
        return True
    # 未显式设置 → 看能力：三个 DSN 里有任意一个就认为处于三库拓扑
    return any(dsn_of(d) for d in ("mgmt", "runtime", "insight"))


# 每个角色归属哪个域 —— 期望库名不写死，而是从该域的 DSN 里取，
# 这样换部署（库名不同）不用改代码，也不会把内部库名硬编码进公开仓库。
ROLE_DOMAIN = {
    "mgmt": "mgmt",
    "event": "runtime",
    "worker": "runtime",
    "insight": "insight",
}


def expected_db_name(role: str) -> str:
    """本角色应当连的库名：取自该域 DSN 的 path。没有 DSN（单库模式）时返回空串。"""
    domain = ROLE_DOMAIN.get((role or "").strip().lower())
    if not domain:
        return ""
    dsn = dsn_of(domain)
    if not dsn:
        return ""
    try:
        return (parse_dsn(dsn).get("database") or "").strip()
    except Exception:
        return ""


def describe_connection() -> dict:
    """当前进程实际连的库（给 /health 和启动日志用，一眼看出连错没）。"""
    return {
        "host": os.environ.get("DEVOPS_DB_HOST", ""),
        "port": os.environ.get("DEVOPS_DB_PORT", ""),
        "name": os.environ.get("DEVOPS_DB_NAME", ""),
        "domain": current_domain() or "",
        "domain_lib": lib_enabled(),
        "own_dsn": sorted(d for d in ("mgmt", "runtime", "insight")
                          if dsn_of(d) and uses_own_dsn(d)),
    }


def assert_role_database(role: str) -> None:
    """启动自检：本角色连的库必须是它该连的那个，否则拒绝启动。

    为什么值得 fail-fast：库连错时进程**照常启动、照常返回 200**，
    只有业务查询查不到数据 —— 表现为「凭据全部 401、引擎全部失联」，
    症状出现在远端机器上，现场只能靠翻 /proc/<pid>/environ 才看得出，
    2026-11-15 为此花了几个小时。
    """
    expect = expected_db_name(role)
    if not expect:
        # 单库模式（没有 DSN）：无从校验，保留原行为
        return
    actual = os.environ.get("DEVOPS_DB_NAME", "").strip()
    if not actual:
        log.error(f"[db-guard] role={role} 未设置 DEVOPS_DB_NAME，无法确认连的是哪个库 —— 拒绝启动")
        raise SystemExit(5)
    if actual != expect:
        log.error(
            f"[db-guard] role={role} 连错库：DEVOPS_DB_NAME={actual}，应为 {expect}。"
            f" 常见原因：unit 缺少 GRAPE_3DB_ENV drop-in（于是 grape-systemd-run.sh 走了单库分支，"
            f"连回旧的 grape_<domain>_db）。拒绝启动，避免带着错库对外服务。"
        )
        raise SystemExit(5)
    log.info(f"[db-guard] role={role} database={describe_connection()}")


def warn_if_cross_domain_fallback(domain: str) -> None:
    """跨域读拿不到该域 DSN 时告警一次（禁止静默串到进程库）。

    为什么必须响：三库拓扑下缺 GRAPE_DSN_<DOMAIN> 时，`domain_db()` 返回 None，
    调用方"降级"到进程主库——同名表往往是**跨库视图**，视图是建视图时的列快照，
    基表加列后非 owner 侧读该列就 1054、读新表就 1146，表现为 500
    （2026-11-20 实锤：/api/dashboard/{owner}/{repo} 在 insight 进程读
    insight.managed_repos 视图，缺 is_org_repo → 1054 → 500）。单库/共库拓扑下
    `GRAPE_<DOMAIN>_DB_NAME == DEVOPS_DB_NAME`，不会触发本告警。
    """
    domain = (domain or "").lower()
    if domain not in _VALID or domain == current_domain():
        return
    if dsn_of(domain):
        return
    expected = os.environ.get(f"GRAPE_{domain.upper()}_DB_NAME", "").strip()
    actual = os.environ.get("DEVOPS_DB_NAME", "").strip()
    if not (expected and actual and expected != actual):
        return
    flag = f"_warned_cross_{domain}"
    if getattr(_local, flag, False):
        return
    setattr(_local, flag, True)
    log.error(
        f"[db-guard] 跨域读 {domain} 缺 GRAPE_DSN_{domain.upper()}，回落到进程库 "
        f"{actual}（owner 库应为 {expected}）：同名 VIEW 列漂移会变成 1054/1146 的 500"
    )


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
        from src.kernel.mysql_compat import run_virtual_sql, translate_sql

        if params is not None and not isinstance(params, (tuple, list, dict)):
            params = (params,)
        # PRAGMA / sqlite_master 方言语句：translate_sql 只打标记不翻译，必须由游标
        # 自行改写为 information_schema 查询（与 mysql_compat.Cursor 对齐）。此前本
        # 兼容游标缺这一段，三库分域拓扑下 PRAGMA table_info 原样透传 MySQL → 1064，
        # 启动迁移（managed_repos / user_repos / events repo_key prefix）全部静默失败。
        try:
            if run_virtual_sql(self._raw, sql, params):
                return self
        except Exception as e:  # 保持原语义：写侧失败上抛，读侧降级为不处理
            log.warning(f"db_registry: virtual sql failed, fallback raw: {sql[:80]!r} ({e})")
        sql2 = translate_sql(sql) or sql
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
    warn_if_cross_domain_fallback(domain)
    if domain in _forbidden:          # 配置/鉴权类错误：重试也不会好，永久停用
        return None
    if _retry_after.get(domain, 0.0) > time.monotonic():   # 临时失败按退避重试
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
        except Exception as exc:  # noqa: BLE001
            if _is_permanent_connect_error(exc):
                _forbidden.add(domain)
                log.warning(f"domain '{domain}' 连接不可用（配置/鉴权错误，已停用）：{exc}")
            else:
                _retry_after[domain] = time.monotonic() + _RETRY_BACKOFF_S
                log.warning(
                    f"domain '{domain}' 连接暂时失败，{_RETRY_BACKOFF_S:.0f}s 后重试：{exc}")
            return None
        _retry_after.pop(domain, None)
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
    _forbidden.discard(dom)          # set：没有 pop(default) 语义，用 discard
    _retry_after.pop(dom, None)
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
        _retry_after.clear()
