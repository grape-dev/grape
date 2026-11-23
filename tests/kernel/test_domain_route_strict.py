"""跨域连接归属（根因修复）S1–S8。

规则（全仓唯一判定 kernel.db_registry.route_of）：
  单库拓扑 / 本域进程        → 进程主库（local）
  三库拓扑 + 有该域 DSN      → 域连接（domain）
  三库拓扑 + 缺该域 DSN      → DomainUnavailable（**绝不回落主库**）

为什么：回落会让跨域读/写静默打到本进程库 —— 读到同名表/空壳，写入落到非 owner 库
并用错的密钥加密，生成再也解不开的行（gitee rc 毒行事故）。
"""
from __future__ import annotations

import pathlib

import pytest

from src.kernel import db_registry as reg


@pytest.fixture()
def topo(monkeypatch):
    st = {"lib": True, "current": "runtime", "dsn": {"mgmt": "mysql://u:p@h:3306/mgmt_db"}}
    monkeypatch.setattr(reg, "lib_enabled", lambda: st["lib"])
    monkeypatch.setattr(reg, "current_domain", lambda: st["current"])
    monkeypatch.setattr(reg, "dsn_of", lambda d: st["dsn"].get(d, ""))
    return st


def test_s1_single_db_topology_routes_local(topo):
    topo["lib"] = False
    assert reg.route_of("insight") == "local"


def test_s2_own_domain_routes_local(topo):
    topo["current"] = "insight"
    topo["dsn"] = {}
    assert reg.route_of("insight") == "local"


def test_s3_cross_domain_with_dsn_routes_to_domain(topo):
    assert reg.route_of("mgmt") == "domain"


def test_s4_cross_domain_without_dsn_raises(topo):
    with pytest.raises(reg.DomainUnavailable) as ei:
        reg.route_of("insight")
    assert ei.value.domain == "insight"
    assert "GRAPE_DSN_INSIGHT" in str(ei.value)


def test_s5_audit_lists_missing_dsns(topo):
    missing = reg.audit_required_domains()
    assert set(missing) == {"insight"}
    assert missing["insight"] == "GRAPE_DSN_INSIGHT"


def test_s6_insight_write_never_falls_back_to_the_process_database(topo, monkeypatch):
    """缺 DSN 时，insight 写口抛错且**完全不碰** session_manager.db（毒行就是这么产生的）。"""
    import src.kernel.session_manager as sm
    from src.domains.insight import public as ins

    class _Bomb:
        def cursor(self, *a, **kw):  # pragma: no cover - 触达即失败
            raise AssertionError("insight 写口回落到了进程主库（根因回归）")

    monkeypatch.setattr(sm, "db", _Bomb(), raising=False)
    with pytest.raises(reg.DomainUnavailable):
        ins.update_repo_credential_values([("gitee:a/b", "bot", "v")])
    with pytest.raises(reg.DomainUnavailable):
        ins.repo_credential_value("gitee:a/b", "bot")


def test_s7_mgmt_proxy_never_falls_back_to_the_process_database(topo, monkeypatch):
    topo["dsn"] = {}  # 跨域 + 缺 DSN
    import src.kernel.session_manager as sm

    from src.domains.mgmt.user import db as mdb

    class _Bomb:
        def cursor(self, *a, **kw):  # pragma: no cover
            raise AssertionError("mgmt 代理回落到了进程主库（根因回归）")

    monkeypatch.setattr(sm, "db", _Bomb(), raising=False)
    with pytest.raises(reg.DomainUnavailable):
        mdb._mg_db.cursor()


def test_s8_owner_modules_use_the_kernel_primitive():
    """棘轮：域 public 不得再自写 `uses_own_dsn ? DSN : 主库` 的回落。"""
    base = pathlib.Path(__file__).resolve().parents[2] / "src"
    ins = (base / "domains/insight/public/__init__.py").read_text(encoding="utf-8")
    assert "cursor_for(\"insight\"" in ins and "transaction_for(\"insight\"" in ins
    assert "session_manager" not in ins.split("def _any_sql")[0], "insight 游标仍在自行回落主库"
    mg = (base / "domains/mgmt/user/db.py").read_text(encoding="utf-8")
    proxy = mg.split("class _MgmtDbProxy")[1].split("class ")[0]
    assert "route_of(\"mgmt\")" in proxy
    assert "uses_own_dsn" not in proxy
