"""域拓扑自检的纯逻辑用例（不连库）。

锁定三件事：
1. 单库/共库拓扑（域库名相同或未声明）绝不告警、绝不拒绝启动；
2. 三库拓扑缺 `GRAPE_DSN_<DOMAIN>` 必须 fail-closed（exit 6），且报错点名缺哪个；
3. 显式 `GRAPE_CROSS_DOMAIN_STRICT=0` 可以放行（离线脚本/迁移期）。
"""
import pytest

from src.kernel import domain_topology as topo

_ENV_KEYS = [
    "DEVOPS_DB_NAME", "GRAPE_PROCESS_DOMAIN", "GRAPE_CROSS_DOMAIN_STRICT",
    "GRAPE_MGMT_DB_NAME", "GRAPE_RUNTIME_DB_NAME", "GRAPE_INSIGHT_DB_NAME",
    "GRAPE_DSN_MGMT", "GRAPE_DSN_RUNTIME", "GRAPE_DSN_INSIGHT",
]


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for key in _ENV_KEYS:
        monkeypatch.delenv(key, raising=False)
    yield


def _three_db(monkeypatch, with_dsn=()):
    monkeypatch.setenv("DEVOPS_DB_NAME", "grape_insight_db")
    monkeypatch.setenv("GRAPE_MGMT_DB_NAME", "grape_mgmt_db")
    monkeypatch.setenv("GRAPE_RUNTIME_DB_NAME", "grape_dev_runtime")
    monkeypatch.setenv("GRAPE_INSIGHT_DB_NAME", "grape_insight_db")
    for dom in with_dsn:
        monkeypatch.setenv(f"GRAPE_DSN_{dom.upper()}", f"mysql://u:p@h:3306/grape_dev_{dom}")


def test_single_db_topology_is_quiet(monkeypatch):
    monkeypatch.setenv("DEVOPS_DB_NAME", "grape_mgmt_db")
    monkeypatch.setenv("GRAPE_MGMT_DB_NAME", "grape_mgmt_db")
    assert topo.multi_db_mode() is False
    assert topo.missing_dsns() == []
    assert topo.wiring_problems() == []
    topo.assert_cross_domain_wiring()          # 不抛


def test_three_db_missing_dsns_are_named(monkeypatch):
    _three_db(monkeypatch, with_dsn=("insight",))
    assert topo.own_domain() == "insight"
    assert topo.missing_dsns() == ["mgmt", "runtime"]
    problems = topo.wiring_problems()
    assert len(problems) == 2
    assert "GRAPE_DSN_MGMT" in problems[0] and "grape_mgmt_db" in problems[0]


def test_three_db_complete_wiring_passes(monkeypatch):
    _three_db(monkeypatch, with_dsn=("mgmt", "runtime", "insight"))
    assert topo.missing_dsns() == []
    topo.assert_cross_domain_wiring()          # 不抛


def test_missing_dsn_refuses_to_start(monkeypatch):
    _three_db(monkeypatch, with_dsn=("insight",))
    with pytest.raises(SystemExit) as exc:
        topo.assert_cross_domain_wiring()
    assert exc.value.code == 6


def test_strict_opt_out_allows_start(monkeypatch):
    _three_db(monkeypatch, with_dsn=("insight",))
    monkeypatch.setenv("GRAPE_CROSS_DOMAIN_STRICT", "0")
    topo.assert_cross_domain_wiring()          # 放行（显式降级）


def test_own_domain_from_explicit_env(monkeypatch):
    _three_db(monkeypatch, with_dsn=("mgmt", "runtime", "insight"))
    # 进程角色名（event/worker）不是域，显式值非法时回落到库名反查
    monkeypatch.setenv("GRAPE_PROCESS_DOMAIN", "event")
    assert topo.own_domain() == "insight"
    monkeypatch.setenv("GRAPE_PROCESS_DOMAIN", "runtime")
    assert topo.own_domain() == "runtime"


def test_unknown_own_db_is_a_problem(monkeypatch):
    _three_db(monkeypatch, with_dsn=("mgmt", "runtime", "insight"))
    monkeypatch.setenv("DEVOPS_DB_NAME", "grape_scratch")
    assert topo.own_domain() == ""
    assert "不在声明的域库集合" in topo.wiring_problems()[0]