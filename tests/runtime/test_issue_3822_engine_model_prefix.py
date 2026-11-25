"""Issue #3822 轨 C-3：`opengw/<id>` 与 `<id>` 前缀归一 + 本地查表类诊断。

生产证据（SRE 轨 A，评论 202862302）：
  - 网关 BYOK key 的 `/v1/models` 只有**裸 id**（`glm-5.3`、`deepseek-v4.1-flash-1017` ...）；
  - 带 provider 前缀写法 `opengw/glm-5.3` → HTTP 404 `Model not found`；
  - `Model not found: <provider>/<model>`（8 条）是 opencode **本地**查表失败。

本用例锁定公开契约：
  1. `split_model_prefix`：已知 provider 前缀才剥，未知前缀原样保留（不误拆含 `/` 的模型名）；
  2. `read_opencode_model_config`：`models` 键重复 provider 前缀 → `local_issues` 的 `prefixed_key`，
     比对口径取裸 id（不产生「网关清单里有却报缺失」的假差异）；
  3. default 裸 id 未被 provider `models` 声明 → `local_issues` 的 `undeclared_default`；
  4. `validate_engine_model`：本地查表诊断 → `misaligned`，文案给出可执行改写建议且附可用列表。
"""
import json
import os
import sys

ROOT = os.path.join(os.path.dirname(__file__), "../..")
sys.path.insert(0, ROOT)

from src.domains.runtime.engine import engine_model


def _write(tmp_path, cfg, name="opencode.json"):
    path = tmp_path / name
    path.write_text(json.dumps(cfg, indent=2), encoding="utf-8")
    return str(path)


BASE = "https://gw.example.com/v1"
MODELS_AVAILABLE = ["glm-5.3", "glm-5.3-flash", "deepseek-v4.1-flash-1017",
                    "deepseek-v4-pro-0813"]


def _env():
    return {"OPENCODE_API_KEY": "sk-test-3822"}


def _fetch_available():
    def _fetch(url, api_key, timeout):
        return 200, json.dumps({"data": [{"id": m} for m in MODELS_AVAILABLE]})
    return _fetch


class TestSplitModelPrefix:
    def test_known_provider_prefix_stripped(self):
        assert engine_model.split_model_prefix("opengw/glm-5.3", {"opengw"}) == ("opengw", "glm-5.3")

    def test_bare_model_untouched(self):
        assert engine_model.split_model_prefix("glm-5.3", {"opengw"}) == ("", "glm-5.3")

    def test_unknown_prefix_not_split(self):
        """首段不是已知 provider 时不拆——避免把自带 `/` 的模型名误归一。"""
        assert engine_model.split_model_prefix("org/model-x", {"opengw"}) == ("", "org/model-x")

    def test_identity_when_no_provider_ids(self):
        assert engine_model.split_model_prefix("opengw/glm-5.3", None) == ("opengw", "glm-5.3")

    def test_provider_prefix_case_insensitive(self):
        """provider 段键大小写与 model 前缀不一致时仍归一（避免误判缺失）。"""
        assert engine_model.split_model_prefix("OpenGW/glm-5.3", {"opengw"}) == ("OpenGW", "glm-5.3")


class TestOpencodePrefixedModelsKey:
    def _cfg(self):
        return {
            "model": "opengw/glm-5.3",
            "provider": {
                "opengw": {
                    "options": {"baseURL": BASE, "apiKey": "{env:OPENCODE_API_KEY}"},
                    # 键重复 provider 前缀 → opencode 本地按裸 id 查表会失败
                    "models": {"opengw/glm-5.3": {}, "opengw/glm-5.3-flash": {}},
                }
            },
        }

    def test_prefixed_key_reported_as_local_issue(self, tmp_path):
        cfg = engine_model.read_opencode_model_config(_write(tmp_path, self._cfg()), env=_env())
        issues = cfg.get("local_issues") or []
        assert any(i.get("code") == "prefixed_key" and i.get("configured") == "opengw/glm-5.3"
                   and i.get("expected") == "glm-5.3" for i in issues)

    def test_comparison_uses_bare_ids(self, tmp_path):
        """比对口径取裸 id：网关清单里有 `glm-5.3` → 不再误报「缺失 glm-5.3」。"""
        cfg = engine_model.read_opencode_model_config(_write(tmp_path, self._cfg()), env=_env())
        models = cfg["targets"][0]["models"]
        assert "glm-5.3" in models and "glm-5.3-flash" in models
        assert "opengw/glm-5.3" not in models

    def test_validate_blocks_with_actionable_message(self, tmp_path):
        res = engine_model.validate_engine_model(
            "opencode", config_path=_write(tmp_path, self._cfg()), env=_env(),
            fetch=_fetch_available())
        assert res["status"] == "misaligned"
        assert "opengw/glm-5.3" in res["message"]        # 配置原值
        assert "glm-5.3" in res["message"]               # 建议的裸 id
        assert "glm-5.3-flash" in res["message"]         # 可用列表（差异可见）

    def test_prefixed_key_in_list_form_models(self, tmp_path):
        cfg = self._cfg()
        cfg["provider"]["opengw"]["models"] = [{"id": "opengw/glm-5.3"}]
        parsed = engine_model.read_opencode_model_config(_write(tmp_path, cfg), env=_env())
        assert any(i.get("code") == "prefixed_key" for i in parsed.get("local_issues") or [])


class TestUndeclaredProviderPrefix:
    """default 的 provider 段未在 `provider` 块声明（opencode 内置 provider 形态）。

    回归锁定：此前 `split_model_prefix` 只在首段属已声明 provider 时才剥，`deepseek/<id>`
    （provider 段未声明 deepseek）会把带前缀的整串当 default_model 发给网关 → 404
    "Model not found" → 探活误判引擎 unhealthy。未声明前缀时仍须以**裸 id** 比对/探活。
    """

    def _cfg(self):
        return {
            "model": "deepseek/deepseek-v4.1-flash-1017",
            "provider": {
                "clouddeveloper": {
                    "options": {"baseURL": BASE, "apiKey": "{env:OPENCODE_API_KEY}"},
                    "models": {"deepseek-v4.1-flash-1017": {}},
                }
            },
        }

    def test_default_model_keeps_bare_id(self, tmp_path):
        cfg = engine_model.read_opencode_model_config(_write(tmp_path, self._cfg()), env=_env())
        assert cfg["default_model"] == "deepseek-v4.1-flash-1017"
        assert not any("deepseek" in w and "not found" in w for w in cfg["warnings"])

    def test_probe_sends_bare_model_id(self, tmp_path):
        seen = {}

        def _post(url, key, timeout, body):
            seen["url"], seen["model"] = url, body["model"]
            return 200, "{}"

        ok, _detail, msg = engine_model.probe_engine_model_call(
            "opencode", config_path=_write(tmp_path, self._cfg()), env=_env(), post=_post)
        assert ok, msg
        assert seen["model"] == "deepseek-v4.1-flash-1017"
        assert seen["url"] == "https://gw.example.com/v1/chat/completions"


class TestOpencodeUndeclaredDefault:
    def _cfg(self):
        return {
            "model": "opengw/deepseek-v4.1-flash-1017",
            "provider": {
                "opengw": {
                    "options": {"baseURL": BASE, "apiKey": "{env:OPENCODE_API_KEY}"},
                    "models": {"glm-5.3": {}},
                }
            },
        }

    def test_undeclared_default_reported(self, tmp_path):
        cfg = engine_model.read_opencode_model_config(_write(tmp_path, self._cfg()), env=_env())
        issues = cfg.get("local_issues") or []
        assert any(i.get("code") == "undeclared_default"
                   and i.get("model") == "deepseek-v4.1-flash-1017" for i in issues)

    def test_validate_rejects_even_when_gateway_list_ok(self, tmp_path):
        """网关清单里模型存在，但 provider 未声明 → 运行期本地查表必失败，注册期拒绝。"""
        res = engine_model.validate_engine_model(
            "opencode", config_path=_write(tmp_path, self._cfg()), env=_env(),
            fetch=_fetch_available())
        assert res["status"] == "misaligned"
        assert "deepseek-v4.1-flash-1017" in res["message"]
        assert "models" in res["message"]                # 可执行建议指向 provider models

    def test_bare_default_single_provider_undeclared(self, tmp_path):
        """default 无 provider 前缀且仅一个 provider：仍应按该 provider 的 models 查表。"""
        cfg = {
            "model": "deepseek-v4.1-flash-1017",
            "provider": {"opengw": {
                "options": {"baseURL": BASE, "apiKey": "{env:OPENCODE_API_KEY}"},
                "models": {"glm-5.3": {}}}},
        }
        parsed = engine_model.read_opencode_model_config(_write(tmp_path, cfg), env=_env())
        issues = parsed.get("local_issues") or []
        assert any(i.get("code") == "undeclared_default"
                   and i.get("provider") == "opengw"
                   and i.get("model") == "deepseek-v4.1-flash-1017" for i in issues)

    def test_provider_without_models_map_not_rejected(self, tmp_path):
        """provider 未声明 models（内置 provider 形态）→ 不做本地查表判定，避免误拒。"""
        cfg = {
            "model": "custom/deepseek-v4.1-flash-1017",
            "provider": {"custom": {"options": {"baseURL": BASE, "apiKey": "{env:OPENCODE_API_KEY}"}}},
        }
        res = engine_model.validate_engine_model(
            "opencode", config_path=_write(tmp_path, cfg), env=_env(),
            fetch=_fetch_available())
        assert res["status"] == "ok"
        assert not (res.get("detail") and any(d.get("status") == "misaligned" for d in res["detail"]))

    def test_declared_default_is_clean(self, tmp_path):
        cfg = self._cfg()
        cfg["provider"]["opengw"]["models"]["deepseek-v4.1-flash-1017"] = {}
        parsed = engine_model.read_opencode_model_config(_write(tmp_path, cfg), env=_env())
        assert parsed.get("local_issues") == []
        res = engine_model.validate_engine_model(
            "opencode", config_path=_write(tmp_path, cfg), env=_env(), fetch=_fetch_available())
        assert res["status"] == "ok"


class TestDualOpencodeConfig:
    """`opencode.json` 与 `opencode.jsonc` 并存：按「有 model 的那份」校验（Issue #3822 实机形态）。"""

    def _home(self, tmp_path, monkeypatch, json_cfg, jsonc_cfg):
        home = tmp_path / "home"
        (home / ".config" / "opencode").mkdir(parents=True)
        (home / ".config" / "opencode" / "opencode.json").write_text(
            json.dumps(json_cfg), encoding="utf-8")
        (home / ".config" / "opencode" / "opencode.jsonc").write_text(
            json.dumps(jsonc_cfg), encoding="utf-8")
        monkeypatch.setenv("HOME", str(home))
        return str(home / ".config" / "opencode" / "opencode.json")

    def _rich(self, model):
        return {
            "model": model,
            "provider": {"opengw": {
                "options": {"baseURL": BASE, "apiKey": "{env:OPENCODE_API_KEY}"},
                "models": {"glm-5.3": {}}}},
        }

    def test_prefers_file_with_model_when_json_is_stub(self, tmp_path, monkeypatch):
        self._home(tmp_path, monkeypatch,
                   json_cfg={"$schema": "https://opencode.ai/config.json"},
                   jsonc_cfg=self._rich("opengw/glm-5.3"))
        cfg = engine_model.read_opencode_model_config(env=_env())
        assert cfg["default_model"] == "glm-5.3"
        assert cfg["config_path"].endswith("opencode.jsonc")
        assert any("两份 opencode 配置" in w for w in cfg["warnings"])

    def test_json_wins_when_both_have_model(self, tmp_path, monkeypatch):
        self._home(tmp_path, monkeypatch,
                   json_cfg=self._rich("opengw/glm-5.3"),
                   jsonc_cfg=self._rich("opengw/glm-5.3-flash"))
        cfg = engine_model.read_opencode_model_config(env=_env())
        assert cfg["default_model"] == "glm-5.3"
        assert cfg["config_path"].endswith("opencode.json")