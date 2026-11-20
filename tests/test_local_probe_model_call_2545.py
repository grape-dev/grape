"""Issue #2545：hermes 探针最小模型调用（src/runtime/local_probe.py，Seam B）。

公开接口 probe_local_engines()：hermes 探测在 ACP 握手通过后增加一次 1-token
最小模型调用——模型名不在该 key 所属网关可用列表时，探针必须 healthy=False 并
带原因（probe_detail.model_call="model_not_found: <配置模型名>"），而非
healthy 但实际 chat 404。

三分支（验收）：
  1. 最小调用 404/模型不存在 → healthy=False + model_not_found 原因
  2. 最小调用 200            → healthy=True（不影响既有握手口径）
  3. 网络不可达               → 不误判（healthy 保持握手结果，probe_detail 记录 skipped）

单个失败引擎不得影响其他引擎探测（沿用并发探针语义）。
"""
import os
import sys

ROOT = os.path.join(os.path.dirname(__file__), "..")
sys.path.insert(0, ROOT)
os.environ.setdefault("DEVOPS_DB_PATH", os.path.join(ROOT, "data", "test_sessions.db"))

import src.domains.runtime.engine.local_probe as local_probe
import src.domains.runtime.engine.engine_model as engine_model
from src.domains.runtime.engine import acp_bootstrap


def _hermes_engine():
    return {"name": "hermes", "cli_path": sys.executable, "installed": True}


def _patch_acp_ok(monkeypatch):
    monkeypatch.setattr(acp_bootstrap, "_acp_check", lambda cli, timeout=None: True)


class TestHermesModelCallProbe:
    def test_model_not_found_marks_unhealthy_with_reason(self, monkeypatch):
        """最小调用 404 → healthy=False，probe_detail 带 model_not_found: <模型名>。"""
        _patch_acp_ok(monkeypatch)
        monkeypatch.setattr(engine_model, "read_hermes_model_config", lambda home=None: {
            "config_path": "/tmp/x/config.yaml", "default_model": "deepseek-v4-flash-0731",
            "targets": [{"base_url": "https://gw/v1", "api_key": "sk-k",
                         "models": ["deepseek-v4-flash-0731"]}], "warnings": []})
        monkeypatch.setattr(
            engine_model, "minimal_model_call",
            lambda cfg, timeout=None, post=None: (False, {"model_call": "model_not_found: deepseek-v4-flash-0731"}))
        out = local_probe.probe_local_engines([_hermes_engine()])[0]
        assert out["healthy"] is False
        assert out["probe_detail"]["model_call"] == "model_not_found: deepseek-v4-flash-0731"

    def test_model_call_ok_keeps_healthy(self, monkeypatch):
        """最小调用 200 → healthy=True（且不写 model_not_found 误报）。"""
        _patch_acp_ok(monkeypatch)
        monkeypatch.setattr(engine_model, "read_hermes_model_config",
                            lambda home=None: None)  # 配置缺失→引擎探测照常
        monkeypatch.setattr(
            engine_model, "minimal_model_call",
            lambda cfg, timeout=None, post=None: (True, {"model_call": "ok", "model": "glm-5.3"}))
        out = local_probe.probe_local_engines([_hermes_engine()])[0]
        assert out["healthy"] is True
        assert "model_not_found" not in str(out.get("probe_detail", {}))

    def test_network_unreachable_not_misjudged(self, monkeypatch):
        """网络不可达 → 探针不误判（healthy 保持 ACP 握手结果 + skipped 记录）。"""
        _patch_acp_ok(monkeypatch)
        monkeypatch.setattr(engine_model, "read_hermes_model_config", lambda home=None: {
            "config_path": "/tmp/x/config.yaml", "default_model": "glm-5.3",
            "targets": [{"base_url": "https://gw/v1", "api_key": "sk-k", "models": ["glm-5.3"]}],
            "warnings": []})
        monkeypatch.setattr(
            engine_model, "minimal_model_call",
            lambda cfg, timeout=None, post=None: (True, {"model_call": "skipped: network unreachable"}))
        out = local_probe.probe_local_engines([_hermes_engine()])[0]
        assert out["healthy"] is True
        assert "skipped" in out["probe_detail"]["model_call"]

    def test_failed_hermes_does_not_affect_other_engines(self, monkeypatch):
        """单个引擎模型调用失败不得影响其他引擎探测（并发语义保持）。"""
        _patch_acp_ok(monkeypatch)
        monkeypatch.setattr(engine_model, "read_hermes_model_config", lambda home=None: {
            "config_path": "/tmp/x/config.yaml", "default_model": "m",
            "targets": [{"base_url": "https://gw/v1", "api_key": "k", "models": ["m"]}],
            "warnings": []})
        monkeypatch.setattr(
            engine_model, "minimal_model_call",
            lambda cfg, timeout=None, post=None: (False, {"model_call": "model_not_found: m"}))
        others = [
            {"name": "probe-ok-1", "cli_path": sys.executable, "installed": True},
            {"name": "probe-ok-2", "cli_path": sys.executable, "installed": True},
        ]
        out = local_probe.probe_local_engines([_hermes_engine(), *others])
        by_name = {r["name"]: r for r in out}
        assert by_name["hermes"]["healthy"] is False
        assert by_name["probe-ok-1"]["healthy"] is True
        assert by_name["probe-ok-2"]["healthy"] is True

    def test_acp_fail_short_circuits_without_model_call(self, monkeypatch):
        """ACP 握手失败 → 不做最小模型调用（健康仍按握手口径 False）。"""
        monkeypatch.setattr(acp_bootstrap, "_acp_check", lambda cli: False)
        called = {"n": 0}

        def _should_not_call(cfg, timeout=None, post=None):
            called["n"] += 1
            return True, {}
        monkeypatch.setattr(engine_model, "read_hermes_model_config", lambda home=None: None)
        monkeypatch.setattr(engine_model, "minimal_model_call", _should_not_call)
        out = local_probe.probe_local_engines([_hermes_engine()])[0]
        assert out["healthy"] is False
        assert called["n"] == 0

    def test_model_call_url_normalized_when_base_url_has_no_v1(self, monkeypatch):
        """QA Spec1：base_url 不含 /v1 的配置，探活须打 /v1/chat/completions——
        打错路由时普通路由 404 会被误判 model_not_found → healthy=False 误报。"""
        _patch_acp_ok(monkeypatch)
        monkeypatch.setattr(engine_model, "read_hermes_model_config", lambda home=None: {
            "config_path": "/tmp/x/config.yaml", "default_model": "glm-5.3",
            "targets": [{"base_url": "https://gw", "api_key": "sk-k",   # 无 /v1
                         "models": ["glm-5.3"]}], "warnings": []})
        seen = {}

        def _fetch(url, api_key, timeout, method="GET", body=None):
            seen["url"] = url
            if url == "https://gw/v1/chat/completions":
                return 200, '{"choices":[{"message":{}}]}'
            return 404, '{"error":"Model not found"}'   # 普通路由 404（打错路由时的真实网关行为）

        monkeypatch.setattr(engine_model, "_default_fetch", _fetch)
        out = local_probe.probe_local_engines([_hermes_engine()])[0]
        assert seen["url"] == "https://gw/v1/chat/completions"
        assert out["healthy"] is True, "base_url 无 /v1 的探活不得被误判为不健康"
