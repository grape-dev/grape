"""db_registry 与 runtime public lib 的单测（不连库，全部 monkeypatch）。"""
import threading

import pytest

from src.kernel import db_registry


@pytest.fixture(autouse=True)
def _reset():
    db_registry.reset(close=False)
    yield
    db_registry.reset(close=False)


def test_parse_dsn_basic():
    cfg = db_registry.parse_dsn("mysql://u:p@10.0.0.1:3307/grape_dev_runtime")
    assert cfg == {"host": "10.0.0.1", "port": 3307, "user": "u", "password": "p",
                   "database": "grape_dev_runtime", "charset": "utf8mb4"}


def test_parse_dsn_escaped_and_default_port():
    cfg = db_registry.parse_dsn("mysql://a%40b:p%23w@host/grape_dev_mgmt")
    assert cfg["user"] == "a@b"
    assert cfg["password"] == "p#w"
    assert cfg["port"] == 3306
    assert cfg["database"] == "grape_dev_mgmt"


def test_parse_dsn_rejects_other_scheme():
    with pytest.raises(ValueError):
        db_registry.parse_dsn("postgres://u:p@h/db")


@pytest.mark.parametrize("raw,expected", [
    ("grape_dev_mgmt", "mgmt"), ("grape_dev_runtime", "runtime"), ("grape_dev_insight", "insight"),
    ("grape_runtime_db", "runtime"), ("mgmt", "mgmt"), ("", None), ("other", None),
])
def test_domain_of(raw, expected):
    assert db_registry._domain_of(raw) == expected


def test_current_domain_prefers_db_name(monkeypatch):
    monkeypatch.setenv("DEVOPS_DB_NAME", "grape_dev_insight")
    monkeypatch.setenv("GRAPE_PROCESS_ROLE", "mgmt")
    assert db_registry.current_domain() == "insight"


def test_lib_flag(monkeypatch):
    monkeypatch.delenv("GRAPE_DOMAIN_LIB", raising=False)
    assert db_registry.lib_enabled() is False
    for v in ("1", "true", "YES", "on"):
        monkeypatch.setenv("GRAPE_DOMAIN_LIB", v)
        assert db_registry.lib_enabled() is True
    monkeypatch.setenv("GRAPE_DOMAIN_LIB", "0")
    assert db_registry.lib_enabled() is False


def test_foreign_domain_without_dsn_returns_none(monkeypatch):
    monkeypatch.setenv("DEVOPS_DB_NAME", "grape_dev_mgmt")
    monkeypatch.delenv("GRAPE_DSN_RUNTIME", raising=False)
    assert db_registry.domain_db("runtime") is None


def test_cursor_raises_when_unavailable(monkeypatch):
    monkeypatch.setenv("DEVOPS_DB_NAME", "grape_dev_mgmt")
    monkeypatch.delenv("GRAPE_DSN_RUNTIME", raising=False)
    with pytest.raises(RuntimeError):
        with db_registry.cursor("runtime"):
            pass


def test_unknown_domain_rejected():
    with pytest.raises(ValueError):
        db_registry.domain_db("nope")


def test_own_domain_reuses_session_db(monkeypatch):
    class FakeDB:
        marker = "session-db"

    import src.kernel.session_manager as sm

    monkeypatch.setattr(sm, "db", FakeDB(), raising=False)
    monkeypatch.setenv("DEVOPS_DB_NAME", "grape_dev_mgmt")
    assert db_registry.domain_db("mgmt").marker == "session-db"


def test_domain_lock_is_reentrant_and_cached(monkeypatch):
    a = db_registry.domain_lock("runtime")
    b = db_registry.domain_lock("runtime")
    assert a is b
    assert isinstance(a, type(threading.RLock()))
    assert a.acquire(timeout=0.1)
    assert a.acquire(timeout=0.1)  # 可重入
    a.release()
    a.release()


def test_health_shape(monkeypatch):
    monkeypatch.setenv("DEVOPS_DB_NAME", "grape_dev_mgmt")
    for d in ("RUNTIME", "INSIGHT"):
        monkeypatch.delenv(f"GRAPE_DSN_{d}", raising=False)
    out = db_registry.health()
    assert set(out) == {"mgmt", "runtime", "insight"}
    assert out["runtime"]["ok"] is False
    assert out["runtime"]["configured"] is False