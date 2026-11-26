"""Issue #4445：worker 节点 /tmp 指标（心跳落库 → 控制面 scrape 采样）回归。

远端 worker 最小依赖不含 prometheus_client、agent 进程也没有 /metrics 端点，
节点 /tmp 水位与 janitor 累计量只能靠心跳进 runtime_credentials，再由控制面 /metrics
按 host 暴露为 grape_node_tmp_*。这组用例守住该通路（同 Issue #3773 的 acp_state_db_*）。
"""
import pytest

from src.domains.insight.observability import tmp_usage


def test_collect_node_tmp_metrics_uses_best_row_per_host(monkeypatch):
    from src.domains.runtime import public as rt_public

    rows = [
        # 同一 host 的两行：控制面进程的 0 值行 + worker 上报的真实值行 → 取后者
        ("nodeA", "api", 0.0, 0, 0.0),
        ("nodeA", "worker", 93.5, 42, 1048576.0),
        ("nodeB", "worker", 55.0, 7, 2048.0),
    ]
    monkeypatch.setattr(rt_public, "read_all_any", lambda _name: rows)

    tmp_usage._collect_node_tmp_metrics()

    assert tmp_usage._node_ratio_gauge.labels(host="nodeA")._value.get() == pytest.approx(0.935)
    assert tmp_usage._node_janitor_items_gauge.labels(host="nodeA")._value.get() == 42
    assert tmp_usage._node_janitor_bytes_gauge.labels(host="nodeA")._value.get() == 1048576.0
    assert tmp_usage._node_ratio_gauge.labels(host="nodeB")._value.get() == pytest.approx(0.55)


def test_collect_node_tmp_metrics_tolerates_bad_rows(monkeypatch):
    """缺列/脏值不得抛异常（否则整个 /metrics 采样链被带崩）。"""
    from src.domains.runtime import public as rt_public

    monkeypatch.setattr(rt_public, "read_all_any",
                        lambda _name: [(None, None, "not-a-number", None, None), ()])
    tmp_usage._collect_node_tmp_metrics()
    assert tmp_usage._node_ratio_gauge.labels(host="unknown")._value.get() == 0.0


def test_collect_node_tmp_metrics_swallows_query_failure(monkeypatch):
    from src.domains.runtime import public as rt_public

    def _boom(_name):
        raise RuntimeError("db down")

    monkeypatch.setattr(rt_public, "read_all_any", _boom)
    tmp_usage._collect_node_tmp_metrics()  # 不抛


def test_node_series_are_exposed_in_metrics_payload():
    """上线验收口径：/metrics 文本里必须出现节点 /tmp 系列名（可供 Prometheus 抓取）。"""
    pytest.importorskip("prometheus_client")
    from prometheus_client import generate_latest

    from src.domains.insight import observability as obs

    body = generate_latest(obs.registry).decode("utf-8", "replace")
    assert "grape_node_tmp_used_ratio" in body
    assert "grape_node_tmp_janitor_removed_items_total" in body
    assert "grape_node_tmp_janitor_removed_bytes_total" in body
    assert "grape_tmpfs_used_ratio" in body