"""MySQLCompatDB 池化连接健活校验回归用例（2026-11 全站卡顿）。

现象：跨网段链路上的池化长连接被中间设备静默丢流（对端 ACK、包没到 DB），
MySQL 侧 processlist 只有 Sleep，客户端线程却挂在 pymysql `_read_bytes` 上几十秒
到几分钟；因为所有请求共用全局 `db`/`db_lock`，整站一起冻住，前端表现为
「点编辑要等很久」「请求 30s 后 499」。

不变量：
1. 池化连接借出前，空闲超过阈值必须做一次**带 socket 超时**的 ping；
2. ping 判死时丢弃并重取，不能把死连接交给业务查询；
3. keepalive / 读写超时参数确实下发给 pymysql（池与裸连接两条路径）。
"""
import pytest

from src.kernel import mysql_compat


class _FakeSock:
    def __init__(self):
        self.timeout = 10.0

    def gettimeout(self):
        return self.timeout

    def settimeout(self, v):
        self.timeout = v


class _FakeConn:
    """最小 pymysql 连接桩：ping 行为可控。"""

    def __init__(self, alive=True):
        self.alive = alive
        self.ping_calls = 0
        self.closed = False
        self._sock = _FakeSock()

    def ping(self, reconnect=True):
        self.ping_calls += 1
        if not self.alive:
            raise RuntimeError("stale connection")
        return True

    def cursor(self):
        return object()

    def close(self):
        self.closed = True


class _FakePool:
    def __init__(self, conns):
        self._conns = list(conns)
        self.borrowed = 0

    def connection(self):
        self.borrowed += 1
        return self._conns.pop(0)


@pytest.fixture()
def compat_db():
    db = mysql_compat.MySQLCompatDB(host="127.0.0.1", port=3306, user="u",
                                    password="p", database="d")
    db._pool_disabled = True  # 防止测试真去建池
    return db


def test_stale_pooled_conn_is_dropped_and_replaced(compat_db):
    dead = _FakeConn(alive=False)
    fresh = _FakeConn(alive=True)
    compat_db._pool = _FakePool([fresh])
    compat_db._local.conn = dead
    compat_db._local.conn_alive_at = 0.0  # 空闲足够久 → 必须 ping

    compat_db._raw_cursor()

    assert dead.ping_calls == 1, "死连接必须被 ping 探一次"
    assert dead.closed is True, "判死后必须归还/关闭"
    assert compat_db._local.conn is fresh, "必须换成池里取的新连接"
    assert compat_db._pool.borrowed == 1


def test_recently_used_conn_skips_ping(compat_db):
    """空闲未超阈值时不做 ping，避免每个查询多一次往返。"""
    import time as _t

    conn = _FakeConn(alive=True)
    compat_db._pool = _FakePool([])
    compat_db._local.conn = conn
    compat_db._local.conn_alive_at = _t.time()

    compat_db._raw_cursor()

    assert conn.ping_calls == 0
    assert compat_db._pool.borrowed == 0


def test_ping_sets_socket_timeout_and_restores(compat_db):
    """ping 期间必须用短超时（否则死连接上 ping 自己也挂死）。"""
    conn = _FakeConn(alive=True)
    seen = {}

    def _record(reconnect=True):
        seen["timeout"] = conn._sock.gettimeout()
        return True

    conn.ping = _record
    compat_db._local.conn = conn
    compat_db._local.conn_alive_at = 0.0

    assert compat_db._conn_alive(conn) is True
    assert seen["timeout"] == pytest.approx(mysql_compat._DB_PING_TIMEOUT_SECONDS)
    assert conn._sock.gettimeout() == 10.0, "调用后必须还原原超时"


def test_dedicated_connect_params_carry_timeouts():
    params = mysql_compat._dedicated_connect_params({"host": "h", "user": "u"})
    assert params["read_timeout"] == mysql_compat._DB_READ_TIMEOUT
    assert params["write_timeout"] == mysql_compat._DB_WRITE_TIMEOUT
    assert params["connect_timeout"] == 10


def test_keepalive_enabled_on_connect(monkeypatch):
    """建连工厂必须打开 TCP keepalive（静默丢流靠它兜底判死）。"""
    applied = []
    fake_conn = type("C", (), {"_sock": type("S", (), {
        "setsockopt": staticmethod(lambda *a: applied.append(a))})()})

    monkeypatch.setattr(mysql_compat.pymysql, "connect", lambda **kw: fake_conn)
    monkeypatch.setattr(mysql_compat, "_DB_TCP_KEEPALIVE", True)

    assert mysql_compat._pymysql_connect(host="h") is fake_conn
    assert applied, "必须设置 SO_KEEPALIVE"

# ── db_registry 域连接（跨域读）同类保护 ─────────────────────────────────────
def test_domain_conn_new_conn_sets_timeouts_and_keepalive(monkeypatch):
    """域连接建连必须带读写超时 + keepalive（同 MySQLCompatDB 口径）。"""
    from src.kernel import db_registry

    captured = {}
    applied = []
    fake_conn = type("C", (), {"_sock": type("S", (), {
        "setsockopt": staticmethod(lambda *a: applied.append(a))})()})

    monkeypatch.setattr(db_registry, "dsn_of", lambda d: "mysql://u:p@h:3306/db")
    monkeypatch.setattr(db_registry, "parse_dsn", lambda dsn: {"host": "h", "user": "u"})
    import pymysql as _pm
    monkeypatch.setattr(_pm, "connect", lambda **kw: captured.update(kw) or fake_conn)

    db_registry._new_conn("mgmt")

    assert captured["read_timeout"] == mysql_compat._DB_READ_TIMEOUT
    assert captured["write_timeout"] == mysql_compat._DB_WRITE_TIMEOUT
    assert applied, "域连接也必须开 SO_KEEPALIVE"


def test_bounded_ping_uses_timeout_and_restores(monkeypatch):
    """域连接探活必须限时（死连接上的裸 ping 会挂死）。"""
    from src.kernel import db_registry

    conn = _FakeConn(alive=True)
    seen = {}

    def _record(reconnect=True):
        seen["timeout"] = conn._sock.gettimeout()
        return True

    conn.ping = _record
    db_registry._bounded_ping(conn, 2.5)

    assert seen["timeout"] == pytest.approx(2.5)
    assert conn._sock.gettimeout() == 10.0


def test_bounded_ping_raises_on_dead_conn():
    from src.kernel import db_registry

    conn = _FakeConn(alive=False)
    with pytest.raises(RuntimeError):
        db_registry._bounded_ping(conn, 2.5)
    assert conn._sock.gettimeout() == 10.0, "异常路径也必须还原超时"
