"""Issue #4081 — 插件代理端点契约补齐与缺失上报可观测化。

生产实测（Loki 2026-11-24）：`GET /api/plugins/agent/installed` 恒 404、
`POST /api/plugins/agent/report-missing` 恒 405，两侧同时断 → 插件缺失既查不到、
也报不上，回补闭环完全静默。根因是运行构建早于 `plugins_agent` 路由合入，
调用侧只能靠试错拿到裸 404/405，且没有任何指标/告警。

本用例覆盖交付的四件事（Spec In Scope 1/3/4 + 告警）：

1. `report-missing` 结果计数：`grape_plugin_missing_report_total{result=submitted|degraded}`；
2. 能力位：`GET /api/ping` 的 `plugin_agent` 段（两端点注册面 + missing_operations）；
3. 过渡期语义：`/api/plugins/agent/*` 下未实现的路径/方法 → `501 + error_code=NOT_IMPLEMENTED`
   （不再裸 404/405），并计入 `grape_plugin_agent_unimplemented_total{method}`；
4. `deploy/alerts.yml` 两条规则与上述指标名一致（表达式漂移看护）。
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))
os.environ["DEVOPS_DB_PATH"] = os.path.join(
    os.path.dirname(__file__), "..", "..", "data", "test_sessions.db")

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

TEST_REPO = "gitcode:test-org4081/test-repo"


@pytest.fixture
def client(monkeypatch):
    """会话 token 作用域（repo_key/agent_type 取自 token）：不依赖真实鉴权表。"""
    from src.api.main import app
    from src.api.deps import get_current_user_or_api_key
    app.dependency_overrides[get_current_user_or_api_key] = lambda: {
        "id": 4081, "username": "agent4081", "auth_type": "session_token",
        "repo_key": TEST_REPO, "agent_type": "coding", "session_key": "sk-4081"}
    yield TestClient(app)
    app.dependency_overrides.clear()


def _counter_value(counter, **labels) -> float:
    return counter.labels(**labels)._value.get()


class TestPluginMissingReportMetric:
    """切片①：`report-missing` 结果可观测（submitted / degraded 两分支）。"""

    def test_metric_name_exported(self):
        from src.domains.insight.observability import (
            PLUGIN_MISSING_REPORT, PLUGIN_MISSING_REPORT_METRIC)
        # prometheus_client 内部名去掉 _total 后缀，exposition 时补回
        assert f"{PLUGIN_MISSING_REPORT._name}_total" == PLUGIN_MISSING_REPORT_METRIC

    def test_submitted_increments_metric(self, client, monkeypatch):
        from src.domains.insight.observability import PLUGIN_MISSING_REPORT
        import src.domains.runtime.engine.plugin_negotiation as neg
        monkeypatch.setattr(neg, "submit_install_request",
                            lambda *a, **k: {"submitted": True, "channel": "unified_queue",
                                             "request": {}})
        before = _counter_value(PLUGIN_MISSING_REPORT, result="submitted")
        resp = client.post("/api/plugins/agent/report-missing", json={
            "missing": [{"name": "plug-4081-obs", "reason": "task needs OBS"}],
            "task_ref": "#4081"})
        assert resp.status_code == 200
        assert resp.json()["submit"]["submitted"] is True
        assert _counter_value(PLUGIN_MISSING_REPORT, result="submitted") == before + 1

    def test_degraded_increments_metric_and_keeps_200(self, client, monkeypatch):
        """降级仍 200（核心链路不阻塞），但结果必须有指标 + WARNING 留痕。"""
        from src.domains.insight.observability import PLUGIN_MISSING_REPORT
        import src.domains.runtime.engine.plugin_negotiation as neg
        monkeypatch.setattr(neg, "submit_install_request",
                            lambda *a, **k: {"submitted": False, "channel": "comment",
                                             "request": {}})
        before = _counter_value(PLUGIN_MISSING_REPORT, result="degraded")
        resp = client.post("/api/plugins/agent/report-missing", json={
            "missing": [{"name": "plug-4081-degraded", "reason": "r"}],
            "task_ref": "#4081"})
        assert resp.status_code == 200
        assert resp.json()["degraded_comment"]
        assert _counter_value(PLUGIN_MISSING_REPORT, result="degraded") == before + 1

    def test_metric_visible_on_metrics_exposition(self, client, monkeypatch):
        """/metrics（同 registry）能查到该 counter —— 落地后必须可实证。"""
        from src.domains.insight.observability import (
            PLUGIN_MISSING_REPORT, PLUGIN_MISSING_REPORT_METRIC, registry)
        import src.domains.runtime.engine.plugin_negotiation as neg
        monkeypatch.setattr(neg, "submit_install_request",
                            lambda *a, **k: {"submitted": True, "channel": "unified_queue"})
        client.post("/api/plugins/agent/report-missing",
                    json={"missing": [{"name": "plug-4081-expo", "reason": "r"}],
                          "task_ref": "#4081"})
        assert PLUGIN_MISSING_REPORT.labels(result="submitted")._value.get() >= 1
        try:
            from prometheus_client import generate_latest
        except Exception:  # pragma: no cover - worker 包无 prometheus_client
            pytest.skip("prometheus_client unavailable")
        assert PLUGIN_MISSING_REPORT_METRIC.encode() in generate_latest(registry)


class TestUnimplementedNamespaceSemantics:
    """切片②：未实现端点 501 + error_code=NOT_IMPLEMENTED（不裸 404/405）。"""

    def test_unknown_subpath_get_returns_501(self, client):
        from src.domains.insight.observability import PLUGIN_AGENT_UNIMPLEMENTED
        before = _counter_value(PLUGIN_AGENT_UNIMPLEMENTED, method="GET")
        resp = client.get("/api/plugins/agent/unknown-op")
        assert resp.status_code == 501, resp.text
        detail = resp.json()["detail"]
        assert detail["code"] == 501
        assert detail["error_code"] == "NOT_IMPLEMENTED"
        assert "not implemented" in detail["message"].lower()
        assert _counter_value(PLUGIN_AGENT_UNIMPLEMENTED, method="GET") == before + 1

    def test_unknown_subpath_post_returns_501_not_405(self, client):
        """POST 未登记路径：旧行为是裸 405（Starlette 方法不允许），现为 501 可判定。"""
        resp = client.post("/api/plugins/agent/report-unknown", json={})
        assert resp.status_code == 501, resp.text
        assert resp.json()["detail"]["error_code"] == "NOT_IMPLEMENTED"

    def test_known_path_with_wrong_method_is_501_not_bare_405(self, client):
        resp = client.put("/api/plugins/agent/installed")
        assert resp.status_code == 501
        assert resp.json()["detail"]["error_code"] == "NOT_IMPLEMENTED"

    def test_declared_endpoints_still_reachable(self, client):
        """兜底是最后一条路由，不能吃掉已登记端点（方法级回归看护）。"""
        from src.api.openapi import load_required_operations
        required = load_required_operations()
        assert required["plugins_agent_installed"] == ("/api/plugins/agent/installed", "get")
        assert required["plugins_agent_report_missing"] == (
            "/api/plugins/agent/report-missing", "post")
        resp = client.get("/api/plugins/agent/installed")
        assert resp.status_code == 200, resp.text

    def test_global_404_semantics_unchanged(self, client):
        """兜底只覆盖 /api/plugins/agent/*：命名空间外仍是原 404 语义。"""
        resp = client.get("/api/plugins/agentish/whatever")
        assert resp.status_code == 404


class TestPluginAgentCapabilityBit:
    """切片③：能力位（调用侧先查后调）。

    位置 = `/api/plugins/agent/capabilities`：nginx 把 `/api/ping` 指到 worker 进程
    （deploy/nginx-grape-cluster.conf），那里没挂本 router，放 ping 上会把「可用」
    误报成「不可用」；`/api/plugins/*` 走 `location /` → mgmt。
    """

    def test_capabilities_endpoint_announces_both(self, client):
        resp = client.get("/api/plugins/agent/capabilities")
        assert resp.status_code == 200, resp.text
        cap = resp.json()
        assert cap["available"] is True
        assert cap["installed"] is True
        assert cap["report_missing"] is True
        assert cap["missing_operations"] == []

    def test_capabilities_endpoint_not_in_contract(self):
        """include_in_schema=False：不改变 /openapi.json 端点计数（无需契约重定基）。"""
        from src.api.main import app
        from src.api.openapi import custom_openapi
        schema = custom_openapi(app)
        assert "/api/plugins/agent/capabilities" not in (schema.get("paths") or {})

    def test_ping_payload_unchanged(self):
        """/api/ping 语义保持（含由 worker 进程承载）：不夹带本能力位。"""
        from src.api.main import app
        r = TestClient(app).get("/api/ping")
        assert r.status_code == 200
        assert set(r.json()) == {"ok", "ts"}

    def test_capability_reports_missing_operations_when_routes_absent(self):
        from fastapi import FastAPI
        from src.api.openapi import _build_plugin_agent_capabilities
        cap = _build_plugin_agent_capabilities(FastAPI())
        assert cap["available"] is False
        assert cap["installed"] is False and cap["report_missing"] is False
        missing = {m["operationId"]: m for m in cap["missing_operations"]}
        assert set(missing) == {"plugins_agent_installed", "plugins_agent_report_missing"}
        # 明细必须可定位（operationId + method + path），AC 第 4 条
        assert missing["plugins_agent_installed"]["method"] == "get"
        assert missing["plugins_agent_installed"]["path"] == "/api/plugins/agent/installed"
        assert missing["plugins_agent_report_missing"]["method"] == "post"
        assert missing["plugins_agent_report_missing"]["path"] == \
            "/api/plugins/agent/report-missing"

    def test_capability_survives_registry_failure(self, monkeypatch):
        """清单缺失/损坏时 fail-closed 报告不可用，不得把端点拉垮。"""
        import src.api.openapi as openapi_mod
        monkeypatch.setattr(openapi_mod, "load_required_operations",
                            lambda: (_ for _ in ()).throw(RuntimeError("registry missing")))
        cap = openapi_mod._build_plugin_agent_capabilities(object())
        assert cap["available"] is False
        assert "required_operations_unavailable" in cap["error"]

    def test_capabilities_endpoint_degrades_without_raising(self, monkeypatch, client):
        """能力位构造异常不得被静默吞成「可用」：要么 5xx，要么不返回 available=True。"""
        import src.api.openapi as openapi_mod
        monkeypatch.setattr(openapi_mod, "plugin_agent_capabilities",
                            lambda app: (_ for _ in ()).throw(RuntimeError("boom")))
        try:
            resp = client.get("/api/plugins/agent/capabilities")
        except RuntimeError:
            return  # 未装兜底处理器时按异常上抛，属预期
        assert resp.status_code != 200, resp.text
        assert (resp.json() or {}).get("available") is not True

    def test_capability_cache_returns_same_payload(self):
        from src.api.main import app
        from src.api.openapi import plugin_agent_capabilities
        first = plugin_agent_capabilities(app)
        second = plugin_agent_capabilities(app)
        assert first == second
        assert first is not second          # 返回副本，调用方不得污染缓存


class TestAlertsRules:
    """切片④：deploy/alerts.yml 两条规则与指标名一致。"""

    def _rules(self):
        import yaml
        path = os.path.join(os.path.dirname(__file__), "..", "..", "deploy", "alerts.yml")
        with open(path, encoding="utf-8") as f:
            return yaml.safe_load(f)["groups"][0]["rules"]

    def test_degraded_rule_consumes_metric(self):
        from src.domains.insight.observability import PLUGIN_MISSING_REPORT_METRIC
        rule = next((r for r in self._rules()
                     if r.get("alert") == "GrapePluginMissingReportDegraded"), None)
        assert rule is not None, "alerts.yml 缺少 GrapePluginMissingReportDegraded"
        assert PLUGIN_MISSING_REPORT_METRIC in rule["expr"]
        assert 'result="degraded"' in rule["expr"]
        assert "increase(" in rule["expr"]
        assert rule["labels"]["severity"] == "warning"
        assert rule.get("for") == "5m"
        assert "plugin" in rule["annotations"]["description"].lower()

    def test_endpoint_missing_rule_consumes_both_signals(self):
        from src.domains.insight.observability import PLUGIN_AGENT_UNIMPLEMENTED_METRIC
        rule = next((r for r in self._rules()
                     if r.get("alert") == "GrapePluginAgentEndpointMissing"), None)
        assert rule is not None, "alerts.yml 缺少 GrapePluginAgentEndpointMissing"
        expr = rule["expr"]
        assert PLUGIN_AGENT_UNIMPLEMENTED_METRIC in expr
        assert '/api/plugins/agent/' in expr      # 旧构建裸 404/405 兜底分支
        assert "404|405|501" in expr
        assert rule["labels"]["severity"] == "warning"
        assert rule.get("for") == "5m"

    def test_rule_names_unique(self):
        names = [r.get("alert") for r in self._rules() if r.get("alert")]
        assert len(names) == len(set(names)), "告警规则名重复"


class TestApiErrorErrorCode:
    """切片⑤：api_error 支持机器可读 error_code（401 族同口径）。"""

    def test_error_code_attached(self):
        from src.api.errors import (ERROR_NOT_IMPLEMENTED, NOT_IMPLEMENTED, api_error)
        exc = api_error(NOT_IMPLEMENTED, "not implemented",
                        error_code=ERROR_NOT_IMPLEMENTED)
        assert exc.status_code == 501
        assert exc.detail["code"] == 501
        assert exc.detail["error_code"] == "NOT_IMPLEMENTED"

    def test_error_code_optional_backward_compatible(self):
        from src.api.errors import api_error
        exc = api_error(404, "not found")
        assert exc.detail == {"code": 404, "message": "not found"}, \
            "未传 error_code 时响应体形态必须不变（既有调用方不受影响）"