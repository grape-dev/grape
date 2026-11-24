"""批量读通道——请求路径长查询免进程锁（G-01 整改，A6/A7 棘轮正解）。

设计依据（scripts/check_async_blocking.py 故障谱）：进程级 `db_lock` 是 SQLite
单连接时代的串行化手段；持锁的批量扫描会让心跳、claim、/tags、tasks/complete
一起排队，实测表现为 nginx 504 与「引擎失联」（该守卫文件头记录已复发四次）。

本模块给「读」提供两条免锁通道，语义与 `with db_lock: c = db.cursor()` 对齐：

- **MySQL 后端**：`MySQLCompatDB` 自身线程安全（池化/每线程连接 + 内部兜底锁），
  不需要进程级 db_lock。指定 ``domain`` 时先走 ``db_registry``（三库拓扑下取目标
  域连接；同域自动复用 session db，跨域用 DSN）；未开域库（单库）时目标表就在
  主库，直接用 session db。
- **SQLite 后端**：session_manager 建库即设 WAL + busy_timeout（跨进程读写安全），
  读侧开**短连接**即可与写侧并发——不共享连接，也就不需要 db_lock。

只用于**读**（SELECT / PRAGMA）。写仍走既有 db_lock 通道；需要跨域写的走
``cross_write`` / 域 public 原语（G-02 另行整改）。

用法（等价替换 ``with db_lock: c = db.cursor()`` 的读块）::

    from src.kernel.bulk_read import reader
    with reader() as c:                    # 本域读
        rows = c.execute("SELECT ...", ()).fetchall()
    with reader("mgmt") as c:               # 跨域读（runtime 进程读 mgmt 归属表）
        rows = c.execute("SELECT ...", ()).fetchall()
"""
from __future__ import annotations

from contextlib import contextmanager
from typing import Iterator


@contextmanager
def reader(domain: str = "") -> Iterator:
    """读游标上下文（免 db_lock）。出块自动关游标/短连接。

    :param domain: 目标表归属域（"mgmt" / "runtime" / "insight"）。空 = 本域。
        三库拓扑下跨域读经 db_registry 走目标域 DSN；同域/单库自动回落 session db。
    """
    from src.kernel.session_manager import _is_mysql_backend
    if _is_mysql_backend():
        if domain:
            try:
                from src.kernel import db_registry as _dbreg
                with _dbreg.cursor(domain) as c:
                    yield c
                return
            except RuntimeError:
                pass  # 未开域库（单库 MySQL）：目标表就在主库，走 session db
        from src.kernel.session_manager import db
        c = db.cursor()
        try:
            yield c
        finally:
            try:
                c.close()
            except Exception:  # noqa: BLE001  # best-effort，勿掩蔽调用方异常
                pass
        return
    # SQLite：短连接读（WAL 读者与写者并发；不与 session 共享连接故无需 db_lock）。
    # Issue #3588：连接期先探测（SELECT 1 触发开库与 WAL 打开，stale 状态在此暴露），
    # 命中失效（disk I/O error / unable to open 等）则换一条全新短连接重试一次——
    # 短连接独立完成、不受主连接 stale WAL 状态干扰。
    import sqlite3
    from src.kernel import session_manager as _sm
    from src.kernel.db_health import is_stale_error
    conn = None
    for _attempt in (1, 2):
        try:
            conn = sqlite3.connect(_sm._db_path, timeout=5)
            conn.execute("SELECT 1").fetchone()
            break
        except sqlite3.Error as _e:
            if conn is not None:
                try:
                    conn.close()
                except Exception:  # noqa: BLE001  # best-effort，勿掩蔽失效异常
                    pass
            conn = None
            if _attempt == 2 or not is_stale_error(_e):
                raise
    try:
        c = conn.cursor()
        try:
            yield c
        finally:
            try:
                c.close()
            except Exception:  # noqa: BLE001
                pass
    finally:
        try:
            conn.close()
        except Exception:  # noqa: BLE001
            pass


def _read_with_retry(sql: str, params, fetch, domain: str = ""):
    """read_all/read_one 共享重试结构：stale 失效 → 换新短连接重放同一语句。"""
    import sqlite3
    from src.kernel.db_health import is_stale_error

    def _once():
        with reader(domain) as c:
            return fetch(c.execute(sql, params or ()))

    try:
        return _once()
    except sqlite3.Error as e:
        if not is_stale_error(e):
            raise
    return _once()


def read_all(sql: str, params=(), domain: str = "") -> list:
    """批量读一行集（免锁通道的便捷封装）。

    Issue #3588：读语句命中 SQLite 存储层失效时换新短连接重试一次。
    """
    return _read_with_retry(sql, params, lambda cur: cur.fetchall(), domain)


def read_one(sql: str, params=(), domain: str = "") -> tuple | None:
    """读单行（免锁通道的便捷封装）。

    Issue #3588：读语句命中 SQLite 存储层失效时换新短连接重试一次。
    """
    return _read_with_retry(sql, params, lambda cur: cur.fetchone(), domain)
