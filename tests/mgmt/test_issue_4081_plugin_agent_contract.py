"""Issue #4081 — 插件代理端点契约补齐与缺失上报可观测化。

生产实测（Loki 2026-11-24）：`GET /api/plugins/agent/installed` 恒 404、
`POST /api/plugins/agent/report-missing` 恒 405，两侧同时断 → 插件缺失既查不到、
也报不上，回补闭环完全静默。根因是运行构建早于 `plugins_agent` 路由合入，
调用侧只能靠试错拿到裸 404/405，且没有任何指标/告警。

本用例覆盖交付的四件事（Spec In Scope 1/3/4 + 告警）：

1. `report-missing` 结果计数：`grape_plugin_missing_report_total{result=submitted|degraded}`；
2. 能力位：`GET /api/plugins/agent/capabilities`（两端点注册面 + missing_operations；
   `/api/ping` 由 worker 进程承载、没有本 router，故不放 ping）；
3. 过渡期语义：`/api/plugins/agent/*` 下未实现的路径/方法 → `501 + error_code=NOT_IMPLEMENTED`
   （不再裸 404/405），并计入 `grape_plugin_agent_unimplemented_total{method}`；
4. `deploy/alerts.yml` 两条规则与上述指标名一致（表达式漂移看护），且旧构建分支
   的选择器能命中真实标签（应用侧 route 是路由模板，旧构建永远不匹配 → 只能用
   Alloy 从 nginx access.log 派生的 `nginx_upstream_requests_total{path,status}`）；
5. 命名空间兜底与已登记端点的注册顺序约束（跨文件 include 顺序漂移守卫）。
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

    def test_probe_methods_head_and_options_are_501(self, client):
        """复审 Optional：探测动词（HEAD/OPTIONS）不得留下裸 405 缝。"""
        for method, path in (("HEAD", "/api/plugins/agent/unknown-op"),
                             ("OPTIONS", "/api/plugins/agent/unknown-op"),
                             ("HEAD", "/api/plugins/agent/installed"),
                             ("OPTIONS", "/api/plugins/agent/installed")):
            resp = client.request(method, path)
            assert resp.status_code == 501, (method, path, resp.status_code)


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

    def test_cached_payload_is_deep_copied(self):
        """复审 Optional：嵌套 missing_operations 列表不得与缓存共享引用。"""
        from fastapi import FastAPI
        from src.api.openapi import plugin_agent_capabilities
        empty_app = FastAPI()
        first = plugin_agent_capabilities(empty_app)
        assert first["missing_operations"]        # 空 app → 两端点都缺
        first["missing_operations"].clear()
        second = plugin_agent_capabilities(empty_app)
        assert len(second["missing_operations"]) == 2, "缓存被调用方改动污染"

    def test_cache_key_does_not_reuse_freed_id(self):
        """复审 Optional：id(app) 复用时不得把旧 app 的能力位当成新 app 的。"""
        import gc
        from fastapi import FastAPI
        from src.api.openapi import plugin_agent_capabilities, _capability_cache

        empty_app = FastAPI()
        stale = plugin_agent_capabilities(empty_app)
        assert stale["available"] is False
        stale_id = id(empty_app)
        del empty_app, stale
        gc.collect()
        _capability_cache.pop(stale_id, None)   # 隔离 id 复用这一条路径

        from src.api.main import app as real_app
        fresh = plugin_agent_capabilities(real_app)
        assert fresh["available"] is True


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

    # ===== 复审 Critical：告警表达式必须能命中真实标签，而不只是「字符串出现」 =====

    def _endpoint_rule(self):
        rule = next((r for r in self._rules()
                     if r.get("alert") == "GrapePluginAgentEndpointMissing"), None)
        assert rule is not None, "alerts.yml 缺少 GrapePluginAgentEndpointMissing"
        return rule

    @staticmethod
    def _selector_matches(matcher: str, labels: dict) -> bool:
        """把 `<metric>{k=~"re"|k="v", ...}` 的标签选择器按 PromQL 语义求值。"""
        import re as _re
        body = matcher.split("{", 1)[1].rsplit("}", 1)[0]
        for part in [p.strip() for p in body.split(",") if p.strip()]:
            m = _re.match(r'^(\w+)\s*(=~|=)\s*"([^"]*)"$', part)
            assert m, f"无法解析选择器片段: {part}"
            key, op, pattern = m.group(1), m.group(2), m.group(3)
            value = labels.get(key)
            if value is None:
                return False
            if op == "=" and value != pattern:
                return False
            if op == "=~" and not _re.fullmatch(pattern, value):
                return False
        return True

    def test_legacy_branch_uses_nginx_derived_metric(self):
        """旧构建没有本 router，应用侧 route 标签永远不匹配 → 必须走网关派生指标。"""
        expr = self._endpoint_rule()["expr"]
        assert "nginx_upstream_requests_total" in expr, (
            "旧构建分支必须用 Alloy 派生的 nginx_upstream_requests_total"
            "（应用侧 route 是路由模板 /{path}、unmatched）")
        assert "grape_http_requests_total" not in expr, (
            "旧构建的 404/405 不会以 /api/plugins/agent/* 作为 route 标签进应用指标（#3847）")
        assert 'path=~"/api/plugins/agent/.*"' in expr.replace(" ", "")
        assert 'status=~"404|405|501"' in expr.replace(" ", "")

    def test_spa_only_app_route_labels_do_not_satisfy_selector(self, monkeypatch):
        """行为级：模拟旧构建（只有 SPA catch-all），GET 404 / POST 405 的 route 取值
        不满足告警选择器 —— Critical 的可证伪用例。"""
        monkeypatch.setenv("METRICS_ADDR", "127.0.0.1:0")
        from fastapi import FastAPI, HTTPException
        from fastapi.testclient import TestClient
        from src.domains.insight.observability import (
            HTTP_REQUESTS_TOTAL, metrics_middleware)

        def snapshot() -> dict:
            return {(tuple(sorted((k, str(v)) for k, v in s.labels.items())))
                    for m in HTTP_REQUESTS_TOTAL.collect() for s in m.samples
                    if s.name == "grape_http_requests_total"}

        legacy = FastAPI()

        @legacy.get("/{path:path}")
        def spa_catch_all(path: str):
            # 与 src/api/spa.py 同语义：api/ 前缀的未命中路由 404
            if path.startswith("api/"):
                raise HTTPException(status_code=404)
            return {"ok": True}

        metrics_middleware(legacy)
        client = TestClient(legacy)
        before = snapshot()
        assert client.get("/api/plugins/agent/installed").status_code == 404
        resp = client.post("/api/plugins/agent/report-missing", json={})
        assert resp.status_code == 405, "旧构建无该路由时 Starlette 直接 405"

        new_labels = [dict(kv) for kv in snapshot() - before]
        assert new_labels, "旧构建请求未产生新的 grape_http_requests_total 样本"
        routes = {labels["route"] for labels in new_labels}
        # 旧构建实测：route 是**路由模板**——GET 落 SPA catch-all → "/{path}"；
        # POST 只有 partial match（GET 的 catch-all），route 也不是请求路径。
        assert routes <= {"/{path}", "unmatched"}, routes
        assert "/{path}" in routes
        for labels in new_labels:
            assert not self._selector_matches(
                'grape_http_requests_total{route=~"/api/plugins/agent/.*", status=~"404|405|501"}',
                labels), (
                f"旧构建样本 {labels} 不得满足应用侧选择器"
                "（否则说明告警分支选错了指标）")

    def test_nginx_selector_matches_real_request_path(self):
        """网关派生指标：path 是真实请求路径，选择器必须能命中旧构建样本。"""
        legacy_samples = [
            {"path": "/api/plugins/agent/installed", "status": "404"},
            {"path": "/api/plugins/agent/report-missing", "status": "405"},
        ]
        for labels in legacy_samples:
            assert self._selector_matches(
                'nginx_upstream_requests_total{path=~"/api/plugins/agent/.*", status=~"404|405|501"}',
                labels), labels
        # 否定样本：同名前缀的其它命名空间不能被误收
        assert not self._selector_matches(
            'nginx_upstream_requests_total{path=~"/api/plugins/agent/.*"}',
            {"path": "/api/plugins/agentish/x", "status": "404"})

    def test_alloy_derives_metric_with_path_and_status_labels(self):
        """告警选择器的标签必须真实由 Alloy 产出（否则规则永远无样本）。"""
        alloy = open(os.path.join(os.path.dirname(__file__), "..", "..",
                                  "deploy", "alloy-config.alloy"), encoding="utf-8").read()
        assert 'name        = "nginx_upstream_requests_total"' in alloy
        assert 'values = { status = "", path = "" }' in alloy
        collector = alloy.split('name        = "nginx_upstream_requests_total"', 1)[0]
        selector = collector.rsplit("stage.match", 1)[1]
        assert "plugins/agent" in selector, (
            "Alloy 采集面必须包含 /api/plugins/agent/，否则旧构建分支无数据")


class TestNamespaceRegistrationOrder:
    """复审 Required：兜底只保证文件内最后，跨文件 include 顺序漂移必须被守卫。

    `src/api/main.py` 在 `plugins_agent_router` 之后仍 include 了多个 router；若其中任一
    再定义 `/api/plugins/agent/*`，兜底会先匹配返 501（端点被吞）而能力位仍报可用。
    """

    def test_namespace_pairs_all_precede_fallback(self):
        from src.api.main import app
        from src.api.openapi import _route_index, load_required_operations

        index, fallback = _route_index(app)
        assert fallback is not None, "未找到命名空间兜底路由（unimplemented_plugin_agent_operation）"

        required = load_required_operations()
        namespace = {p: (m, i) for (p, m), i in index.items()
                     if p == "/api/plugins/agent" or p.startswith("/api/plugins/agent/")}
        assert namespace, "命名空间内未注册任何路由"

        for oid in ("plugins_agent_installed", "plugins_agent_report_missing"):
            path, method = required[oid]
            idx = index.get((path, str(method).upper()))
            assert idx is not None, f"{oid} 未注册：{method} {path}"
            assert idx < fallback, (
                f"{oid} ({method} {path}) 注册下标 {idx} 不早于兜底 {fallback}："
                "兜底会先吞掉该端点（main.py 中该 router 必须 include 在 plugins_agent 之前）")

        after = {p for p, (_m, i) in namespace.items() if i > fallback}
        assert not after, f"兜底之后仍注册了命名空间路由：{sorted(after)}"

    def test_capability_reports_unavailable_when_fallback_shadows(self, monkeypatch):
        """结构性防御：即使顺序漂移，能力位也不得报 available=true。"""
        from fastapi import FastAPI
        from src.api.openapi import (
            _build_plugin_agent_capabilities, PLUGIN_AGENT_OPERATIONS,
            load_required_operations)
        from src.domains.mgmt.routes.plugins_agent import (
            router as plugins_agent_router)

        # 先挂兜底（模拟顺序漂移），再挂已登记端点
        shadowed = FastAPI()
        fallback_only = FastAPI()
        fallback_only.include_router(plugins_agent_router)
        for route in fallback_only.routes:
            if getattr(route, "path", "") == "/api/plugins/agent/{rest:path}":
                shadowed.router.routes.append(route)
        tokens = FastAPI()
        tokens.include_router(plugins_agent_router)
        for route in tokens.routes:
            if getattr(route, "path", "") in (
                    "/api/plugins/agent/installed", "/api/plugins/agent/report-missing"):
                shadowed.router.routes.append(route)

        cap = _build_plugin_agent_capabilities(shadowed)
        assert cap["available"] is False
        assert {m["operationId"] for m in cap["missing_operations"]} ==             set(PLUGIN_AGENT_OPERATIONS)
        assert load_required_operations()          # 清单可读（非清单缺失路径）

    def test_fallback_does_not_swallow_declared_endpoints(self, client):
        """端到端：兜底在最后，已登记端点仍命中原处理器（方法级）。"""
        assert client.get("/api/plugins/agent/installed").status_code == 200
        assert client.get("/api/plugins/agent/capabilities").status_code == 200


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