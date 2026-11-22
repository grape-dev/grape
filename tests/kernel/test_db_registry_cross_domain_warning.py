"""跨域读缺 DSN 的告警守卫（2026-11-20 500 事故的机制化收口）。

事故链：三库四进程下 `/etc/grape/*.env` 没配 `GRAPE_DSN_<DOMAIN>` →
`db_registry.domain_db()` 返回 None / mgmt 代理回落进程主库 →
跨域读到同名 **VIEW**（建视图时的列快照）→ 基表加列后非 owner 侧 1054 →
`/api/dashboard/{owner}/{repo}` 直接 500，页面上只显示「加载失败: 请求失败 (500)」。

这里锁两件事：单库/共库拓扑（库名相同）不告警；三库缺 DSN 时必须响亮，且每域只报一次。
"""
import logging

import pytest

from src.kernel import db_registry


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    for key in ("DEVOPS_DB_NAME", "GRAPE_MGMT_DB_NAME", "GRAPE_RUNTIME_DB_NAME",
                "GRAPE_INSIGHT_DB_NAME", "GRAPE_DSN_MGMT", "GRAPE_PROCESS_ROLE"):
        monkeypatch.delenv(key, raising=False)
    # 告警节流标记在线程本地，逐用例重置
    for dom in ("mgmt", "runtime", "insight"):
        monkeypatch.setattr(db_registry._local, f"_warned_cross_{dom}", False, raising=False)
    yield


def _run(monkeypatch, caplog, domain="mgmt"):
    with caplog.at_level(logging.ERROR):
        db_registry.warn_if_cross_domain_fallback(domain)
    return [r for r in caplog.records if "[db-guard]" in r.getMessage()]


def test_no_warning_in_single_db_topology(monkeypatch, caplog):
    monkeypatch.setenv("DEVOPS_DB_NAME", "grape_dev_mgmt")
    monkeypatch.setenv("GRAPE_MGMT_DB_NAME", "grape_dev_mgmt")
    assert _run(monkeypatch, caplog) == []


def test_warns_when_three_db_topology_lacks_dsn(monkeypatch, caplog):
    monkeypatch.setenv("DEVOPS_DB_NAME", "grape_dev_insight")
    monkeypatch.setenv("GRAPE_MGMT_DB_NAME", "grape_dev_mgmt")
    records = _run(monkeypatch, caplog)
    assert len(records) == 1
    msg = records[0].getMessage()
    assert "GRAPE_DSN_MGMT" in msg and "grape_dev_insight" in msg and "grape_dev_mgmt" in msg


def test_warning_is_throttled_per_domain(monkeypatch, caplog):
    monkeypatch.setenv("DEVOPS_DB_NAME", "grape_dev_insight")
    monkeypatch.setenv("GRAPE_MGMT_DB_NAME", "grape_dev_mgmt")
    assert len(_run(monkeypatch, caplog)) == 1
    caplog.clear()
    assert _run(monkeypatch, caplog) == []          # 同一进程内不刷屏


def test_no_warning_when_dsn_present(monkeypatch, caplog):
    monkeypatch.setenv("DEVOPS_DB_NAME", "grape_dev_insight")
    monkeypatch.setenv("GRAPE_MGMT_DB_NAME", "grape_dev_mgmt")
    monkeypatch.setenv("GRAPE_DSN_MGMT", "mysql://u:p@127.0.0.1:3306/grape_dev_mgmt")
    assert _run(monkeypatch, caplog) == []


def test_same_domain_call_never_warns(monkeypatch, caplog):
    monkeypatch.setenv("DEVOPS_DB_NAME", "grape_dev_mgmt")
    monkeypatch.setenv("GRAPE_MGMT_DB_NAME", "grape_dev_mgmt")
    assert _run(monkeypatch, caplog, domain="mgmt") == []