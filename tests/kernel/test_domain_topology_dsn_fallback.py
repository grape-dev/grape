"""三库安全网的拓扑判定要认 DSN 接线，否则闸门在纯 DSN 部署下静默失效。

回归背景：domain_dbs() 只读 GRAPE_<DOM>_DB_NAME，按 GRAPE_DSN_<DOM> 接线的部署
会被判成单库 → 启动拒绝/视图自检/跨域回落告警三道闸门全部 no-op。
"""
from __future__ import annotations

import pytest

from src.kernel import domain_topology as t


@pytest.fixture()
def clean_env(monkeypatch):
    for key in list(t.DB_ENV.values()) + list(t.DSN_ENV.values()) + [
        "DEVOPS_DB_NAME", "GRAPE_PROCESS_DOMAIN",
    ]:
        monkeypatch.delenv(key, raising=False)
    return monkeypatch


def _dsn(db: str) -> str:
    return f"mysql://u:p@db-host:3306/{db}?charset=utf8mb4"


def test_multi_db_mode_detected_from_dsns_only(clean_env):
    clean_env.setenv("DEVOPS_DB_NAME", "grape_runtime")
    clean_env.setenv("GRAPE_DSN_MGMT", _dsn("grape_mgmt"))
    clean_env.setenv("GRAPE_DSN_RUNTIME", _dsn("grape_runtime"))
    clean_env.setenv("GRAPE_DSN_INSIGHT", _dsn("grape_insight"))

    assert t.domain_dbs() == {
        "mgmt": "grape_mgmt", "runtime": "grape_runtime", "insight": "grape_insight"}
    assert t.multi_db_mode() is True
    assert t.own_domain() == "runtime"


def test_explicit_db_name_wins_over_dsn(clean_env):
    clean_env.setenv("DEVOPS_DB_NAME", "own_db")
    clean_env.setenv("GRAPE_MGMT_DB_NAME", "explicit_mgmt")
    clean_env.setenv("GRAPE_DSN_MGMT", _dsn("dsn_mgmt"))
    assert t.domain_dbs()["mgmt"] == "explicit_mgmt"


def test_single_db_stays_single(clean_env):
    """只有进程主库、没有任何域声明 → 保持单库语义（不做三库判定）。"""
    clean_env.setenv("DEVOPS_DB_NAME", "one_db")
    assert t.domain_dbs() == {}
    assert t.multi_db_mode() is False
    assert t.wiring_problems() == []


def test_db_from_dsn_shapes():
    assert t._db_from_dsn(_dsn("x1")) == "x1"
    assert t._db_from_dsn("postgres://u:p@h:5432/x2") == "x2"
    assert t._db_from_dsn("mysql://u:p@h:3306/x3?ssl=1") == "x3"
    assert t._db_from_dsn("") == ""
    assert t._db_from_dsn("not-a-dsn") == ""