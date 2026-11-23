"""Issue #3969：nginx 层 /api/runtime/ 502 率指标与告警、滚动重启 SOP、SLO 基线。

seam（配置契约）：Alloy 从 nginx access.log 生成 502 计数与总请求计数；
alerts.yml 提供 >1%/5min 告警；deploy/README.md 与 SLO 文档落库基线。
"""
import os

_REPO_ROOT = os.path.join(os.path.dirname(__file__), "..", "..")


def _read(rel: str) -> str:
    with open(os.path.join(_REPO_ROOT, rel), encoding="utf-8") as f:
        return f.read()


class TestAlloyMetric:
    def test_nginx_502_metric_pipeline(self):
        conf = _read("deploy/alloy-config.alloy")
        assert "loki.process" in conf
        assert "nginx_upstream_502_total" in conf
        assert "nginx_upstream_requests_total" in conf
        assert "metric.counter" in conf
        assert "stage.labels" in conf


class TestAlertRule:
    def test_runtime_502_rate_alert(self):
        conf = _read("deploy/alerts.yml")
        assert "GrapeRuntime502RateHigh" in conf
        rule = conf.split("alert: GrapeRuntime502RateHigh", 1)[1].split("- alert:", 1)[0]
        assert "nginx_upstream_502_total" in rule
        assert "/api/runtime/" in rule
        assert "0.01" in rule
        assert "for: 5m" in rule


class TestRollingRestartSop:
    def test_readme_has_drain_and_wait_sop(self):
        readme = _read("deploy/README.md")
        assert "滚动重启" in readme
        assert "摘流" in readme
        # 逐台 + 等 502 归零再下一台
        assert "逐台" in readme
        assert "502" in readme
        assert "worker-restart-slo.md" in readme


class TestSloBaseline:
    def test_slo_doc_records_production_baseline(self):
        doc = _read("docs/deployment/worker-restart-slo.md")
        for token in ("1,178", "91", "42", "30"):
            assert token in doc, token
        assert "/api/runtime/" in doc
        assert "502" in doc