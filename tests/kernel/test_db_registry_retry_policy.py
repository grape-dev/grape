"""`db_registry` 连接失败策略：临时错误退避重试，只有配置/鉴权错误才永久停用。

回归背景（2026-11-15）：本地 runtime MySQL 打满 `max_connections`（1040 Too many
connections）后，`domain_db()` 把错误统一塞进 `_forbidden` → 整个进程后续所有跨域读
静默回落到进程库，`meta` / `scene_category` / `skill` 全部 1146，直到进程重启才恢复。
现在只有「重试也不会成功」的错误（鉴权/库不存在/域名错）才停用，其余按 5s 退避重试。
"""
import pytest

from src.kernel import db_registry


@pytest.fixture(autouse=True)
def _clean_state():
    db_registry.reset(close=False)
    yield
    db_registry.reset(close=False)


def _enable_lib(monkeypatch, domain="runtime"):
    monkeypatch.setattr(db_registry, "lib_enabled", lambda: True)
    monkeypatch.setattr(db_registry, "dsn_of", lambda d: f"mysql://u:p@h:3306/db" if d == domain else "")
    monkeypatch.delenv("DEVOPS_DB_NAME", raising=False)


@pytest.mark.parametrize("errno_,msg", [
    (1040, "Too many connections"),
    (2003, "Can't connect to MySQL server on 'x'"),
    (2013, "Lost connection to MySQL server during query"),
])
def test_transient_errors_are_retried(monkeypatch, errno_, msg):
    _enable_lib(monkeypatch)
    calls = {"n": 0}

    def _boom(domain, autocommit=True):
        calls["n"] += 1
        raise RuntimeError(errno_, msg)

    monkeypatch.setattr(db_registry, "_new_conn", _boom)
    assert db_registry.domain_db("runtime") is None
    assert "runtime" not in db_registry._forbidden      # 不能永久拉黑
    assert db_registry._retry_after.get("runtime", 0) > 0

    # 退避窗口内直接返回 None，不去打数据库
    assert db_registry.domain_db("runtime") is None
    assert calls["n"] == 1

    # 退避过后会再试一次
    db_registry._retry_after["runtime"] = 0.0
    assert db_registry.domain_db("runtime") is None
    assert calls["n"] == 2


@pytest.mark.parametrize("errno_,msg", [
    (1045, "Access denied for user 'x'@'y' (using password: YES)"),
    (1049, "Unknown database 'grape_stage_runtime'"),
])
def test_permanent_errors_disable_domain(monkeypatch, errno_, msg):
    _enable_lib(monkeypatch)
    calls = {"n": 0}

    def _boom(domain, autocommit=True):
        calls["n"] += 1
        raise RuntimeError(errno_, msg)

    monkeypatch.setattr(db_registry, "_new_conn", _boom)
    assert db_registry.domain_db("runtime") is None
    assert "runtime" in db_registry._forbidden
    # 停用后不再打数据库
    assert db_registry.domain_db("runtime") is None
    assert calls["n"] == 1


def test_successful_connect_clears_backoff(monkeypatch):
    _enable_lib(monkeypatch)
    state = {"fail": True}

    class _Conn:
        def close(self):
            pass

    def _maybe(domain, autocommit=True):
        if state["fail"]:
            raise RuntimeError(1040, "Too many connections")
        return _Conn()

    monkeypatch.setattr(db_registry, "_new_conn", _maybe)
    assert db_registry.domain_db("runtime") is None
    state["fail"] = False
    db_registry._retry_after["runtime"] = 0.0
    assert db_registry.domain_db("runtime") is not None
    assert "runtime" not in db_registry._retry_after