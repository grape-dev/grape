"""Provider 读路径必须按部署形态自适应域，不能把落库位置写死。

背景：三库拓扑下 `grape_t_sandbox_provider_config` / `grape_t_cloud_sandboxes`
的归属在仓库清单里记 runtime、在 beta 实况里在 mgmt。读路径一律走
`_fetch_across_domains`：只在 1146 / 表不存在时切域，其它异常原样抛（不得
静默降级成空计数，否则流控被绕过）。
"""
import types

import pytest

import src.kernel.bulk_read as br
from src.domains.runtime.routes import runtime as rt


class _TableMissing(Exception):
    def __str__(self):
        return "(1146, \"Table 'grape_prod_runtime.grape_t_cloud_sandboxes' doesn't exist\")"


class _Cursor:
    def __init__(self, rows, sink):
        self._rows = rows
        self._sink = sink

    def execute(self, sql, params=()):
        self._sink.append(sql)

    def fetchall(self):
        return self._rows


class _Ctx:
    def __init__(self, rows, sink):
        self._rows = rows
        self._sink = sink

    def __enter__(self):
        return _Cursor(self._rows, self._sink)

    def __exit__(self, *a):
        return False


def test_switches_domain_only_on_missing_table(monkeypatch):
    seen = []

    def fake_reader(domain=""):
        seen.append(domain)
        if domain == "runtime":
            raise _TableMissing()
        return _Ctx([("hit",)], seen)

    monkeypatch.setattr(br, "reader", fake_reader)
    rows = rt._fetch_across_domains("SELECT 1", (), ("runtime", "mgmt"))
    assert rows == [("hit",)]
    # 先试 runtime（清单口径），1146 后切 mgmt 并在该域真正执行查询
    assert seen[:2] == ["runtime", "mgmt"]
    assert "SELECT 1" in seen


def test_other_errors_are_not_swallowed(monkeypatch):
    def fake_reader(domain=""):
        raise RuntimeError("connection reset")

    monkeypatch.setattr(br, "reader", fake_reader)
    with pytest.raises(RuntimeError):
        rt._fetch_across_domains("SELECT 1", (), ("runtime", "mgmt"))


def test_active_sandboxes_403_when_provider_not_owned(monkeypatch):
    monkeypatch.setattr(rt, "_fetch_across_domains", lambda *a, **k: [])
    with pytest.raises(Exception) as exc:
        rt.runtime_provider_active_sandboxes("prov-1", types.SimpleNamespace(id=7))
    assert getattr(exc.value, "status_code", None) == 403


def test_active_sandboxes_reads_both_queries_across_domains(monkeypatch):
    calls = []

    def fake(sql, params=(), domains=("runtime", "mgmt")):
        calls.append((sql, params, domains))
        if "max_sandboxes" in sql:
            return [(25,)]
        return [(3,)]

    monkeypatch.setattr(rt, "_fetch_across_domains", fake)
    out = rt.runtime_provider_active_sandboxes("prov-1", types.SimpleNamespace(id=7))
    assert out == {"active_count": 3, "max_sandboxes": 25}
    # 两条查询各自按归属域优先：provider_config 三方口径 mgmt → mgmt 优先；
    # cloud_sandboxes 归属未裁定 → runtime 优先。都不是写死单域。
    assert len(calls) == 2
    by_table = {("provider_config" if "provider_config" in sql else "cloud_sandboxes"): dom
                for sql, _, dom in calls}
    assert by_table["provider_config"] == ("mgmt", "runtime")
    assert by_table["cloud_sandboxes"] == ("runtime", "mgmt")
    assert calls[0][1] == ("prov-1", 7)
