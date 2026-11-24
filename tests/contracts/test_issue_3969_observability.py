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

class TestAlloy502Filter:
    """复审 R3：502 计数按 status label 过滤，不能用行过滤（bytes/UA 假阳性）。"""

    def test_uses_status_label_not_line_filter(self):
        conf = _read("deploy/alloy-config.alloy")
        assert 'selector = "{job=\\"nginx-access\\", status=\\"502\\"}"' in conf
        assert '|~ \\" 502 \\"' not in conf

    def test_path_label_ids_are_normalized(self):
        # 复审 R8：runtimes/tasks 的 id 归一，避免 label/流基数随业务增长
        conf = _read("deploy/alloy-config.alloy")
        assert "stage.replace" in conf
        assert "(runtimes|tasks)/[^/]+" in conf


class TestAlloySelfScrape:
    """复审 R4：Alloy 自身 /metrics 必须被抓取，否则派生指标永远没有数据。"""

    def test_alloy_self_scrape_configured(self):
        conf = _read("deploy/alloy-config.alloy")
        block = conf.split('prometheus.scrape "alloy_self"', 1)[1].split("prometheus.scrape", 1)[0]
        assert "127.0.0.1:12345" in block
        assert 'metrics_path = "/metrics"' in block
        assert "prometheus.remote_write.default.receiver" in block


class TestRuntime502BurstAlert:
    def test_short_window_absolute_alert(self):
        # 复审 R9：低流量端点 clamp_min 稀释 + 5m 窗口滞后，补短窗绝对量告警
        conf = _read("deploy/alerts.yml")
        assert "GrapeRuntime502Burst" in conf
        rule = conf.split("alert: GrapeRuntime502Burst", 1)[1].split("- alert:", 1)[0]
        assert "increase(nginx_upstream_502_total" in rule
        assert "[2m]" in rule
        assert "> 50" in rule
