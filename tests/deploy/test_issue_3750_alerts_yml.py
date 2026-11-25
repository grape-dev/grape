"""Issue #3750：`deploy/alerts.yml` 的 GrapeWebhookTokenMismatch 与代码侧种子规则一致性。

两条告警通道口径必须对齐（阈值/窗口/等级同语义），否则「应用内告警静默、Prometheus 也不响」
或反之。反向也钉死：种子规则的 `seed_source` 必须在 alerts.yml 里真实存在（可追溯）。
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

ROOT = os.path.join(os.path.dirname(__file__), "..", "..")
ALERTS = os.path.join(ROOT, "deploy", "alerts.yml")
RULE = "webhook_token_mismatch_rate"


def _alerts():
    pytest.importorskip("yaml")
    import yaml
    with open(ALERTS) as f:
        return yaml.safe_load(f)


def _rules():
    return {r["alert"]: r for g in _alerts()["groups"] for r in g["rules"]}


class TestPrometheusRule:
    def test_rule_present_with_warning_severity(self):
        rule = _rules()["GrapeWebhookTokenMismatch"]
        assert rule["labels"]["severity"] == "warning"
        assert rule["for"] == "5m"

    def test_expr_consumes_the_counter_per_repo(self):
        expr = _rules()["GrapeWebhookTokenMismatch"]["expr"]
        assert "grape_webhook_token_mismatch_total" in expr, expr
        assert "sum by (repo)" in expr, "必须按仓库聚合，全局聚合会被正常流量稀释"
        assert "> 10" in expr, "阈值与种子规则一致（10 次/分钟）"

    def test_counter_is_registered_in_observability(self):
        pytest.importorskip("prometheus_client")
        from prometheus_client import generate_latest
        from src.domains.insight.observability import WEBHOOK_TOKEN_MISMATCH_TOTAL, registry
        WEBHOOK_TOKEN_MISMATCH_TOTAL.labels(repo="gitcode:test/reg-3750").inc()
        body = generate_latest(registry).decode()
        assert "grape_webhook_token_mismatch_total{" in body
        assert 'repo="gitcode:test/reg-3750"' in body


class TestSeedSourceTraceability:
    def test_seed_source_exists_in_alerts_yml(self):
        from src.domains.insight.observability import alerts as al
        rule = next(r for r in al.SEED_RULES if r["name"] == RULE)
        assert rule["seed_source"] in _rules(), rule["seed_source"]

    def test_thresholds_match_seed_rule(self):
        from src.domains.insight.observability import alerts as al
        seed = next(r for r in al.SEED_RULES if r["name"] == RULE)
        prom = _rules()["GrapeWebhookTokenMismatch"]
        assert seed["threshold"] == 10
        assert seed["window_sec"] == 60
        for_sec = int(str(prom["for"]).rstrip("m")) * 60
        assert for_sec == seed["for_sec"], "Prometheus for 与应用内 for_sec 必须同语义（300s）"