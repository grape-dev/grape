"""Issue #3820：opencode 配置解析与 `/v1/models` 对齐校验（engine_model 公共函数）。

公开契约：
  - `read_opencode_model_config()`：解析 ~/.config/opencode/opencode.json[jc]，
    取 default 模型（`provider/model-id` → 裸 model-id）、网关 baseURL、apiKey
    （`{env:VAR}` / `${VAR}` 解析）、该网关已配置模型全集；
  - `validate_engine_model()`：启动/注册阶段用配置 key 调 `/v1/models`，
    misaligned 时给出「配置模型名 vs 可用列表」差异；不可达降级放行（不误拒）；
  - `probe_engine_model_call()`：探活最小调用（max_tokens=1），失败富化差异文案；
  - `is_model_not_found_error()`：404 + model not found 双条件共用判定。
"""
import json
import os
import sys

ROOT = os.path.join(os.path.dirname(__file__), "..", "..")
sys.path.insert(0, ROOT)

from src.domains.runtime.engine import engine_model


def _write_config(tmp_path, model="custom/deepseek-v4-flash", *, jsonc=False,
                  base_url="https://gw.example.com/v1"):
    cfg = {
        "$schema": "https://opencode.ai/config.json",
        "model": model,
        "provider": {
            "custom": {
                "npm": "@ai-sdk/openai-compatible",
                "name": "custom",
                "options": {"baseURL": base_url, "apiKey": "{env:OPENCODE_API_KEY}"},
                "models": {"deepseek-v4-flash": {"name": "deepseek-v4-flash"}},
            }
        },
    }
    raw = json.dumps(cfg, indent=2)
    if jsonc:
        raw = "// opencode config (jsonc)\n" + raw  # JSONC 行注释
    path = tmp_path / ("opencode.jsonc" if jsonc else "opencode.json")
    path.write_text(raw, encoding="utf-8")
    return str(path)


def _env():
    return {"OPENCODE_API_KEY": "sk-test-3820"}


class TestReadOpencodeModelConfig:
    def test_default_model_and_gateway_extracted(self, tmp_path):
        path = _write_config(tmp_path)
        cfg = engine_model.read_opencode_model_config(path, env=_env())
        assert cfg["default_model"] == "deepseek-v4-flash"   # provider 前缀已剥离
        target = cfg["targets"][0]
        assert target["base_url"] == "https://gw.example.com/v1"
        assert target["api_key"] == "sk-test-3820"           # {env:VAR} 已解析
        assert "deepseek-v4-flash" in target["models"]

    def test_jsonc_comments_tolerated(self, tmp_path):
        path = _write_config(tmp_path, jsonc=True)
        cfg = engine_model.read_opencode_model_config(path, env=_env())
        assert cfg["default_model"] == "deepseek-v4-flash"

    def test_missing_config_returns_none(self, tmp_path):
        assert engine_model.read_opencode_model_config(str(tmp_path / "absent.json")) is None

    def test_unresolved_key_skips_target_with_warning(self, tmp_path):
        path = _write_config(tmp_path)
        cfg = engine_model.read_opencode_model_config(path, env={})
        assert cfg["targets"] == []
        assert any("api_key" in w for w in cfg["warnings"])

    def test_default_provider_target_ordered_first(self, tmp_path):
        """探活固定打 targets[0] → default 模型所在网关必须排首位（多 provider 配置）。"""
        cfg = {
            "model": "second/m2",
            "provider": {
                "first": {"options": {"baseURL": "https://gw-one/v1", "apiKey": "k1"},
                          "models": {"m1": {}}},
                "second": {"options": {"baseURL": "https://gw-two/v1", "apiKey": "k2"},
                           "models": {"m2": {}}},
            },
        }
        path = tmp_path / "opencode.json"
        path.write_text(json.dumps(cfg), encoding="utf-8")
        parsed = engine_model.read_opencode_model_config(str(path), env={})
        assert parsed["default_model"] == "m2"
        assert parsed["targets"][0]["base_url"] == "https://gw-two/v1"


class TestValidateEngineModel:
    def _fetch_missing(self):
        def _fetch(url, api_key, timeout):
            assert url == "https://gw.example.com/v1/models"
            return 200, json.dumps({"data": [{"id": "other-model"}]})
        return _fetch

    def test_misaligned_reports_configured_vs_available(self, tmp_path):
        path = _write_config(tmp_path)
        res = engine_model.validate_engine_model(
            "opencode", config_path=path, env=_env(), fetch=self._fetch_missing())
        assert res["status"] == "misaligned"
        assert res["default_model"] == "deepseek-v4-flash"
        assert "deepseek-v4-flash" in res["message"]     # 配置模型名
        assert "other-model" in res["message"]           # /v1/models 可用列表
        assert "https://gw.example.com/v1/models" in res["message"]

    def test_aligned_status_ok(self, tmp_path):
        path = _write_config(tmp_path)

        def _fetch(url, api_key, timeout):
            return 200, json.dumps({"data": [{"id": "deepseek-v4-flash"}]})

        res = engine_model.validate_engine_model(
            "opencode", config_path=path, env=_env(), fetch=_fetch)
        assert res["status"] == "ok"

    def test_gateway_unreachable_degrades_without_rejecting(self, tmp_path):
        path = _write_config(tmp_path)

        def _fetch(url, api_key, timeout):
            raise ConnectionError("gateway down")

        res = engine_model.validate_engine_model(
            "opencode", config_path=path, env=_env(), fetch=_fetch)
        assert res["status"] == "unreachable"
        assert "skipped" in res["message"].lower() or "不可达" in res["message"]

    def test_disabled_by_env(self, tmp_path, monkeypatch):
        path = _write_config(tmp_path)
        monkeypatch.setenv(engine_model.MODEL_CHECK_DISABLED_ENV, "1")
        res = engine_model.validate_engine_model(
            "opencode", config_path=path, env=_env(), fetch=self._fetch_missing())
        assert res["status"] == "disabled"

    def test_unknown_engine_not_applicable(self):
        res = engine_model.validate_engine_model("codex")
        assert res["status"] == "not_applicable"


class TestProbeEngineModelCall:
    def test_404_model_not_found_marks_unhealthy_with_diff(self, tmp_path):
        path = _write_config(tmp_path)
        ok, detail, err = engine_model.probe_engine_model_call(
            "opencode", config_path=path, env=_env(), fetch=self._fetch(),
            post=lambda url, key, timeout, body: (404, '{"error":"Model not found"}'))
        assert ok is False
        assert "model_not_found" in detail["model_call"]
        assert "deepseek-v4-flash" in err
        assert "other-model" in err          # 差异文案带可用列表（运行时页展示）

    def test_ok_call_keeps_healthy(self, tmp_path):
        path = _write_config(tmp_path)
        ok, detail, err = engine_model.probe_engine_model_call(
            "opencode", config_path=path, env=_env(), fetch=self._fetch(),
            post=lambda url, key, timeout, body: (200, '{"choices":[]}'))
        assert ok is True and detail["model_call"] == "ok" and err == ""

    def test_network_error_does_not_misjudge(self, tmp_path):
        path = _write_config(tmp_path)

        def _boom(url, key, timeout, body):
            raise TimeoutError("connect timeout")

        ok, detail, err = engine_model.probe_engine_model_call(
            "opencode", config_path=path, env=_env(), fetch=self._fetch(), post=_boom)
        assert ok is True and "skipped" in detail["model_call"] and err == ""

    def _fetch(self):
        def _fetch(url, api_key, timeout):
            return 200, json.dumps({"data": [{"id": "other-model"}]})
        return _fetch


class TestModelNotFoundDetector:
    def test_duel_condition_matches(self):
        assert engine_model.is_model_not_found_error("Not Found: Model not found")
        assert engine_model.is_model_not_found_error("HTTP 404: Model not found")
        assert engine_model.is_model_not_found_error('{"code":"model_not_found"}')

    def test_plain_text_not_matched(self):
        assert not engine_model.is_model_not_found_error("tests failed: model not found in expected list")
        assert not engine_model.is_model_not_found_error("connection reset by peer")