"""Issue #3588：SQLite WAL/SHM 失效检测与自动重连（overlayfs 容器场景）。

场景复现（copy-up 语义）：overlayfs 以「删除旧文件 + 新建同名文件」替换
``sessions.db-wal`` / ``sessions.db-shm``，进程内长连接仍持有指向已删除
inode 的 fd → ``disk I/O error`` 持续 500。本组用例验证：

1. ``SelfHealingSQLiteDB`` 在 WAL/SHM 被替换后 execute 自动重建并恢复；
2. ``db_health()`` 报告失效与恢复信号（db_status / wal_valid）；
3. ``bulk_read.reader()`` 短连接在 WAL 被替换场景下成功返回；
4. outbox 经 ``engine_wiring._connect`` 的 stale-aware 连接自动恢复；
5. journal_mode 部署兼容选项生效（默认 wal 不变）。
"""
from __future__ import annotations

import os
import sqlite3
import sys
import tempfile

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "src")))

from src.kernel import db_health  # noqa: E402


def _make_db(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(path, check_same_thread=False)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("CREATE TABLE IF NOT EXISTS probe (id INTEGER PRIMARY KEY, v TEXT)")
    conn.commit()
    return conn


def _wal_path(path: str) -> str:
    return path + "-wal"


def _shm_path(path: str) -> str:
    return path + "-shm"


def _force_wal_files(path: str):
    """强制 WAL/SHM 文件落盘出现（写入后不 clean close，WAL 文件保持存在）。"""
    conn = sqlite3.connect(path, check_same_thread=False)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("CREATE TABLE IF NOT EXISTS probe (id INTEGER PRIMARY KEY, v TEXT)")
    conn.execute("INSERT INTO probe (v) VALUES ('seed')")
    conn.commit()
    conn.close()


def _replace_wal_shm(path: str):
    """模拟 overlayfs copy-up：删除旧 WAL/SHM 文件，再新建同名新文件。"""
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.close()
    for p in (_wal_path(path), _shm_path(path)):
        if os.path.exists(p):
            os.remove(p)
        with open(p, "wb") as f:
            f.write(b"")  # 新 inode 的 0 字节文件（与事故报告一致）


def test_wal_replacement_recovers(tmp_path):
    path = str(tmp_path / "sessions.db")
    _force_wal_files(path)
    healed = db_health.SelfHealingSQLiteDB(lambda: _make_db(path), path, check_ttl=0)
    assert healed.execute("SELECT count(*) FROM probe").fetchone()[0] == 1
    _replace_wal_shm(path)
    # 失效后同一代理对象上 execute 自动重建连接并成功（不再持续 disk I/O error）
    row = healed.execute("SELECT count(*) FROM probe").fetchone()
    assert row[0] == 1


def test_wal_replacement_recovers_via_cursor(tmp_path):
    path = str(tmp_path / "sessions.db")
    _force_wal_files(path)
    healed = db_health.SelfHealingSQLiteDB(lambda: _make_db(path), path, check_ttl=0)
    cur = healed.cursor()
    cur.execute("SELECT count(*) FROM probe")
    assert cur.fetchone()[0] == 1
    _replace_wal_shm(path)
    cur.execute("SELECT count(*) FROM probe")
    assert cur.fetchone()[0] == 1


def test_db_health_reports_stale_and_recovery(tmp_path):
    path = str(tmp_path / "sessions.db")
    _force_wal_files(path)
    healed = db_health.SelfHealingSQLiteDB(lambda: _make_db(path), path, check_ttl=0)
    healed.execute("SELECT 1").fetchone()
    h1 = healed.health()
    assert h1["db_status"] == "ok"
    assert h1["wal_valid"] is True
    _replace_wal_shm(path)
    # 显式触发周期校验 → 标记失效
    assert healed._check_stale(force=True) is True
    h2 = healed.health()
    assert h2["db_status"] == "stale"
    assert h2["wal_valid"] is False
    # 恢复路径
    healed.execute("SELECT count(*) FROM probe").fetchone()
    h3 = healed.health()
    assert h3["db_status"] == "recovered"
    assert h3["wal_valid"] is True
    assert h3["reconnects"] >= 1


def test_is_stale_error():
    assert db_health.is_stale_error(sqlite3.OperationalError("disk I/O error"))
    assert db_health.is_stale_error(sqlite3.DatabaseError("database disk image is malformed"))
    assert db_health.is_stale_error(sqlite3.OperationalError("unable to open database file"))
    assert not db_health.is_stale_error(sqlite3.OperationalError("database is locked"))
    assert not db_health.is_stale_error(ValueError("x"))


def test_bulk_read_reader_recovers_after_wal_replacement(tmp_path, monkeypatch):
    path = str(tmp_path / "sessions.db")
    _force_wal_files(path)
    monkeypatch.setenv("DEVOPS_DB_PATH", path)
    monkeypatch.setattr("src.kernel.session_manager._db_path", path)
    from src.kernel import bulk_read
    assert bulk_read.read_one("SELECT count(*) FROM probe") == (1,)
    _replace_wal_shm(path)
    assert bulk_read.read_one("SELECT count(*) FROM probe") == (1,)


def test_outbox_conn_recovers_after_wal_replacement(tmp_path, monkeypatch):
    path = str(tmp_path / "sessions.db")
    _force_wal_files(path)
    monkeypatch.setenv("DEVOPS_DB_PATH", path)
    monkeypatch.setenv("DEVOPS_DB_BACKEND", "sqlite")
    import importlib
    from src.domains.runtime.im import engine_wiring
    importlib.reload(engine_wiring)
    from src.domains.runtime.im import outbox
    importlib.reload(outbox)
    conn = outbox._conn()
    try:
        conn.execute("SELECT count(*) FROM probe").fetchone()
    finally:
        conn.close()
    _replace_wal_shm(path)
    conn = outbox._conn()
    try:
        row = conn.execute("SELECT count(*) FROM probe").fetchone()
        assert row[0] == 1
    finally:
        conn.close()


def test_outbox_conn_is_stale_aware_proxy(tmp_path, monkeypatch):
    """engine_wiring SQLite 分支返回 stale-aware 代理（outbox 失效重连的接入点）。"""
    path = str(tmp_path / "sessions.db")
    _force_wal_files(path)
    monkeypatch.setenv("DEVOPS_DB_PATH", path)
    monkeypatch.setenv("DEVOPS_DB_BACKEND", "sqlite")
    import importlib
    from src.domains.runtime.im import engine_wiring
    importlib.reload(engine_wiring)
    conn = engine_wiring._connect(path)
    assert isinstance(conn, db_health.SelfHealingSQLiteDB)
    try:
        # 走 stale-aware 路径（check_ttl=0）：WAL/SHM 被替换后同对象 execute 恢复
        object.__setattr__(conn, "_check_ttl", 0.0)
        conn.execute("SELECT count(*) FROM probe").fetchone()
        _replace_wal_shm(path)
        row = conn.execute("SELECT count(*) FROM probe").fetchone()
        assert row[0] == 1
    finally:
        conn.close()


def test_commit_after_stale_raises_instead_of_silent_recover(tmp_path):
    """commit 命中 stale：不得在重建连接上执行空 commit 并误报 recovered——
    旧事务写入已随失效连接丢弃，必须显式抛错让上层重试整笔事务。

    （stale 异常注入在 commit seam：WAL 替换与 commit 之间的竞态窗口在单机
    sqlite 上常被 C 层自愈吞掉，事故报告中的 disk I/O error 用替身确定性复现。）
    """

    class _StaleCommitConn:
        """替身连接：只让 commit 抛 stale 类异常（disk I/O error），其余委托真连接。"""

        def __init__(self, conn):
            self._c = conn

        def execute(self, *a, **k):
            return self._c.execute(*a, **k)

        def executemany(self, *a, **k):
            return self._c.executemany(*a, **k)

        def rollback(self):
            return self._c.rollback()

        def commit(self):
            raise sqlite3.OperationalError("disk I/O error")

        def close(self):
            self._c.close()

    path = str(tmp_path / "sessions.db")
    _force_wal_files(path)
    healed = db_health.SelfHealingSQLiteDB(lambda: _make_db(path), path)
    healed.execute("INSERT INTO probe (v) VALUES ('tx1')")
    object.__setattr__(healed, "_inner_conn", _StaleCommitConn(healed._inner()))
    with pytest.raises(sqlite3.Error):
        healed.commit()
    # 不误报 recovered：失效路径标记 stale，且未提交写入不静默丢失——表里没有 tx1
    assert healed.health()["db_status"] == "stale"
    assert healed.execute("SELECT count(*) FROM probe WHERE v='tx1'").fetchone()[0] == 0
    # 下一笔事务（重试整笔）可正常提交
    healed.execute("INSERT INTO probe (v) VALUES ('tx1')")
    healed.commit()
    assert healed.execute("SELECT count(*) FROM probe WHERE v='tx1'").fetchone()[0] == 1


def test_rollback_after_stale_discards_silently(tmp_path):
    """rollback 命中 stale：重建后即为已回滚（丢弃是 rollback 的语义），不再误报恢复失败。"""
    path = str(tmp_path / "sessions.db")
    _force_wal_files(path)
    healed = db_health.SelfHealingSQLiteDB(lambda: _make_db(path), path)
    healed.execute("INSERT INTO probe (v) VALUES ('tx2')")
    _replace_wal_shm(path)
    healed.rollback()  # 不抛错：旧事务已随重建丢弃
    assert healed.execute("SELECT count(*) FROM probe WHERE v='tx2'").fetchone()[0] == 0


def test_close_then_execute_recovers(tmp_path):
    """close() 后代理仍自愈（docstring「下次访问自动重建」）。"""
    path = str(tmp_path / "sessions.db")
    _force_wal_files(path)
    # 默认 TTL 下周期校验不触发：必须由异常驱动路径识别「closed database」并重建
    healed2 = db_health.SelfHealingSQLiteDB(lambda: _make_db(path), path)  # 默认 TTL=5s
    healed2.execute("SELECT 1").fetchone()
    healed2.close()
    assert healed2.execute("SELECT count(*) FROM probe").fetchone()[0] == 1


def test_consumer_backoff_ladder():
    """outbox 消费者退避：连续失败 1x/2x/4x 封顶（不出现内联幻数）。"""
    from src.domains.runtime.im import outbox as OB
    assert OB.consumer_backoff_seconds(1.0, 1) == 1.0
    assert OB.consumer_backoff_seconds(1.0, 2) == 2.0
    assert OB.consumer_backoff_seconds(1.0, 3) == 4.0
    assert OB.consumer_backoff_seconds(1.0, 10) == 4.0


def test_consumer_backoff_resets_on_success():
    """退避成功即复位：_fail_streak 构造器初始化、成功一轮归零。"""
    import asyncio
    from src.domains.runtime.im import outbox as OB
    consumer = OB.OutboxConsumer(None, "inst-x", poll_interval=0.01)
    assert consumer._fail_streak == 0
    calls: list[int] = []

    async def _fake_once():
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("db stale round")
        consumer._stopped.set()
        return 0

    consumer.run_once = _fake_once
    asyncio.run(consumer.run())
    assert len(calls) == 2
    assert consumer._fail_streak == 0, "成功后 _fail_streak 必须复位"


def test_journal_mode_default_and_override(monkeypatch):
    """部署兼容选项：DEVOPS_DB_JOURNAL_MODE 可配，默认 wal 不变。"""
    from src.kernel import session_manager as sm
    monkeypatch.delenv("DEVOPS_DB_JOURNAL_MODE", raising=False)
    assert sm._sqlite_journal_mode() == "wal"
    monkeypatch.setenv("DEVOPS_DB_JOURNAL_MODE", "TRUNCATE")
    assert sm._sqlite_journal_mode() == "truncate"


def test_journal_mode_option_applied(tmp_path, monkeypatch):
    path = str(tmp_path / "sessions.db")
    monkeypatch.setenv("DEVOPS_DB_JOURNAL_MODE", "delete")
    monkeypatch.setattr("src.kernel.session_manager._db_path", path)
    from src.kernel import session_manager as sm
    conn = sm._build_sqlite_db(path)
    try:
        mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
        assert str(mode).lower() == "delete"
    finally:
        conn.close()


# ── 外部评审复核意见：事务保护 + 重放读写区分（先写失败测试） ──────────────


def test_is_read_sql_classification():
    """评审②判据：SELECT/PRAGMA/EXPLAIN 前缀（含前导注释）为读；写语句一律非读。"""
    assert db_health.is_read_sql("SELECT 1")
    assert db_health.is_read_sql("select count(*) from t")
    assert db_health.is_read_sql("PRAGMA journal_mode")
    assert db_health.is_read_sql("EXPLAIN SELECT 1")
    assert db_health.is_read_sql("/* c */ -- 注释\nSELECT 1")
    assert not db_health.is_read_sql("INSERT INTO t VALUES (1)")
    assert not db_health.is_read_sql("UPDATE t SET a=1")
    assert not db_health.is_read_sql("DELETE FROM t")
    assert not db_health.is_read_sql("REPLACE INTO t VALUES (1)")
    assert not db_health.is_read_sql("CREATE TABLE t(a)")
    assert not db_health.is_read_sql("WITH x AS (SELECT 1) INSERT INTO t SELECT * FROM x")
    assert not db_health.is_read_sql("")


def test_precheck_in_transaction_raises_instead_of_silent_drop(tmp_path):
    """评审①：事务进行中周期预检命中 stale 不得静默重建丢写——重建后必须抛错。

    复现评审路径：execute(INSERT)（未提交）→ 周期校验（TTL 0）命中 inode 变化
    → execute(UPDATE) 不得在新连接上「正常返回」而丢掉 INSERT。
    """
    path = str(tmp_path / "sessions.db")
    _force_wal_files(path)
    healed = db_health.SelfHealingSQLiteDB(lambda: _make_db(path), path, check_ttl=0)
    healed.execute("INSERT INTO probe (v) VALUES ('tx')")
    _replace_wal_shm(path)
    with pytest.raises(sqlite3.OperationalError):
        healed.execute("UPDATE probe SET v='tx2' WHERE v='tx'")
    # 调用方收到异常后重试整笔事务可正常提交（新连接上）
    healed.execute("INSERT INTO probe (v) VALUES ('tx')")
    healed.commit()
    assert healed.execute("SELECT count(*) FROM probe WHERE v='tx'").fetchone()[0] == 1


def test_cursor_precheck_in_transaction_raises(tmp_path):
    """评审①：cursor() 入口的事务保护与 execute() 同口径。"""
    path = str(tmp_path / "sessions.db")
    _force_wal_files(path)
    healed = db_health.SelfHealingSQLiteDB(lambda: _make_db(path), path, check_ttl=0)
    healed.execute("INSERT INTO probe (v) VALUES ('tx')")
    _replace_wal_shm(path)
    with pytest.raises(sqlite3.OperationalError):
        healed.cursor()


class _StaleExecuteConn:
    """替身连接：execute 系列抛 stale 类异常（disk I/O error），close 委托真连接。"""

    def __init__(self, conn, *, delegate_select: bool = False):
        self._c = conn
        self._delegate_select = delegate_select

    @property
    def in_transaction(self):
        return self._c.in_transaction

    def _maybe_delegate(self, sql):
        if self._delegate_select and str(sql).lstrip().upper().startswith("SELECT"):
            return self._c.execute(sql)
        raise sqlite3.OperationalError("disk I/O error")

    def execute(self, sql, params=()):
        return self._maybe_delegate(sql)

    def executemany(self, sql, seq):
        raise sqlite3.OperationalError("disk I/O error")

    def executescript(self, sql):
        raise sqlite3.OperationalError("disk I/O error")

    def close(self):
        self._c.close()


def test_write_stale_rebuilt_and_raises_not_replayed(tmp_path):
    """评审②：写语句命中 stale 只重建不重放（防双写），抛错交上层重试。"""
    path = str(tmp_path / "sessions.db")
    _force_wal_files(path)
    healed = db_health.SelfHealingSQLiteDB(lambda: _make_db(path), path)
    healed.execute("INSERT INTO probe (v) VALUES ('w1')")
    healed.commit()
    n0 = healed.execute("SELECT count(*) FROM probe").fetchone()[0]
    object.__setattr__(healed, "_inner_conn", _StaleExecuteConn(healed._inner()))
    with pytest.raises(sqlite3.Error):
        healed.execute("INSERT INTO probe (v) VALUES ('w2')")
    # 重建后的新连接上没有 w2（未重放），表行数不变
    assert healed.execute("SELECT count(*) FROM probe WHERE v='w2'").fetchone()[0] == 0
    assert healed.execute("SELECT count(*) FROM probe").fetchone()[0] == n0


def test_executemany_write_stale_not_replayed(tmp_path):
    """评审②：executemany 部分行成功后重放范围不可界定——命中 stale 重建后抛错。"""
    path = str(tmp_path / "sessions.db")
    _force_wal_files(path)
    healed = db_health.SelfHealingSQLiteDB(lambda: _make_db(path), path)
    healed.commit()
    n0 = healed.execute("SELECT count(*) FROM probe").fetchone()[0]
    object.__setattr__(healed, "_inner_conn", _StaleExecuteConn(healed._inner()))
    with pytest.raises(sqlite3.Error):
        healed.executemany("INSERT INTO probe (v) VALUES (?)", [("a",), ("b",)])
    # 重建后的新连接上没有 'a'/'b'（未重放），表行数不变
    assert healed.execute("SELECT count(*) FROM probe WHERE v IN ('a','b')").fetchone()[0] == 0
    assert healed.execute("SELECT count(*) FROM probe").fetchone()[0] == n0


def test_executescript_stale_not_replayed(tmp_path):
    """评审②：executescript 有隐式 COMMIT 前缀语义——命中 stale 重建后抛错，不重放。"""
    path = str(tmp_path / "sessions.db")
    _force_wal_files(path)
    healed = db_health.SelfHealingSQLiteDB(lambda: _make_db(path), path)
    object.__setattr__(healed, "_inner_conn", _StaleExecuteConn(healed._inner()))
    with pytest.raises(sqlite3.Error):
        healed.executescript("INSERT INTO probe (v) VALUES ('s');")
    assert healed.execute("SELECT count(*) FROM probe WHERE v='s'").fetchone()[0] == 0


def test_read_stale_in_transaction_raises_not_replayed(tmp_path):
    """评审①+②：事务进行中即使读语句命中 stale 也不重放——事务已随失效连接
    丢失，重放读会掩盖丢写（后续 commit 变成空提交），必须抛错交上层重试。"""
    path = str(tmp_path / "sessions.db")
    _force_wal_files(path)
    healed = db_health.SelfHealingSQLiteDB(lambda: _make_db(path), path)
    healed.execute("INSERT INTO probe (v) VALUES ('tx')")
    object.__setattr__(healed, "_inner_conn", _StaleExecuteConn(healed._inner()))
    with pytest.raises(sqlite3.Error):
        healed.execute("SELECT count(*) FROM probe").fetchone()


def test_read_stale_outside_transaction_still_replayed(tmp_path):
    """评审②回归：非事务中的读语句仍重放一次（自愈语义不回退）。"""
    path = str(tmp_path / "sessions.db")
    _force_wal_files(path)
    healed = db_health.SelfHealingSQLiteDB(lambda: _make_db(path), path)
    object.__setattr__(healed, "_inner_conn",
                       _StaleExecuteConn(healed._inner(), delegate_select=True))
    # SELECT 在替身上抛 stale → 代理重建后在真连接上重放读语句并成功返回
    assert healed.execute("SELECT count(*) FROM probe").fetchone()[0] == 1


# ── 外部评审复审：_StaleAwareCursor 缺 __iter__（隐式迭代 TypeError）─────────


def test_execute_result_supports_implicit_iteration(tmp_path):
    """复审缺陷：execute() 返回的游标包装可被 for 直接迭代
    （活体调用点 src/domains/mgmt/routes/im.py:423 的 `for ... in conn.execute(...)` 用法）。"""
    path = str(tmp_path / "sessions.db")
    _force_wal_files(path)
    healed = db_health.SelfHealingSQLiteDB(lambda: _make_db(path), path)
    rows = [row[0] for row in healed.execute("SELECT v FROM probe")]
    assert rows == ["seed"]


def test_iterate_after_stale_rebuild(tmp_path):
    """stale 后重建重放读语句得到的游标包装仍可迭代（包装语义连续不回退）。"""
    path = str(tmp_path / "sessions.db")
    _force_wal_files(path)
    healed = db_health.SelfHealingSQLiteDB(lambda: _make_db(path), path, check_ttl=0)
    assert healed.execute("SELECT count(*) FROM probe").fetchone()[0] == 1
    _replace_wal_shm(path)
    rows = [row[0] for row in healed.execute("SELECT v FROM probe")]
    assert rows == ["seed"]
