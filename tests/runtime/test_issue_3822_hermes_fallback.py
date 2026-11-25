"""Issue #3822 轨 C-2：hermes `fallback_providers` 校验覆盖 + 占位 key / 多网关不误拒。

背景（SRE 轨 C 交办，评论 202862302）：
  #1986 的 hermes 分支只校验 `model.default` + `model_aliases` + `custom_providers`，
  **不覆盖 `fallback_providers`**——本机 2 个 404 id（`deepseek-v4-flash-0731`、`glm-5`）
  正是从该缺口漏过。另外 worker venv 无 pyyaml，降级解析器不支持列表 → 列表形态的
  `fallback_providers` 会让整份 config 被判「无法解析」而跳过校验。

本用例锁定公开契约：
  1. `read_hermes_model_config` 收敛 `fallback_providers`（列表/单对象）与旧键 `fallback_model`；
  2. 降级解析器（无 pyyaml）支持块序列，与 pyyaml 路径产出同样的模型信息；
  3. 多网关分组：一个网关不可达 + 另一个对齐 → 放行；任一网关真缺失 → misaligned；
  4. `key_env` 指向未定义环境变量 → 跳过该网关并 warning，不误判 misaligned。
"""
import json
import os
import sys
import textwrap

ROOT = os.path.join(os.path.dirname(__file__), "../..")
sys.path.insert(0, ROOT)

from src.domains.runtime.engine import engine_model

BASE = "https://gw.example.com/v1"
AVAILABLE = ["glm-5.3", "glm-5.3-flash", "deepseek-v4.1-flash-1017", "deepseek-v4-pro-0813"]


def _home(tmp_path, text, name="h"):
    home = tmp_path / name
    home.mkdir()
    (home / "config.yaml").write_text(textwrap.dedent(text), encoding="utf-8")
    return str(home)


def _fetch_available():
    def _fetch(url, api_key, timeout):
        return 200, json.dumps({"data": [{"id": m} for m in AVAILABLE]})
    return _fetch


FALLBACK_FIXTURE = """\
    model:
      default: deepseek-v4.1-flash-1017
      base_url: https://gw.example.com/v1
      api_key: ${HERMES_KEY_3822}
    fallback_providers:
      - provider: custom
        model: deepseek-v4-flash-0731
        base_url: https://gw.example.com/v1
        api_key: ${HERMES_KEY_3822}
      - provider: custom
        model: glm-5.3
        base_url: https://gw.example.com/v1
        api_key: ${HERMES_KEY_3822}
"""


class TestHermesFallbackProviders:
    def _cfg(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HERMES_KEY_3822", "sk-3822")
        return engine_model.read_hermes_model_config(_home(tmp_path, FALLBACK_FIXTURE),
                                                     env={"HERMES_KEY_3822": "sk-3822"})

    def test_fallback_models_collected(self, tmp_path, monkeypatch):
        cfg = self._cfg(tmp_path, monkeypatch)
        models = cfg["targets"][0]["models"]
        assert "deepseek-v4.1-flash-1017" in models          # primary 仍在
        assert "deepseek-v4-flash-0731" in models            # fallback #1
        assert "glm-5.3" in models                           # fallback #2

    def test_legacy_fallback_model_key_covered(self, tmp_path):
        text = """\
            model:
              default: deepseek-v4.1-flash-1017
              base_url: https://gw.example.com/v1
              api_key: sk-3822
            fallback_model:
              - provider: custom
                model: glm-5
                base_url: https://gw.example.com/v1
        """
        cfg = engine_model.read_hermes_model_config(_home(tmp_path, text), env={})
        assert "glm-5" in cfg["targets"][0]["models"]

    def test_legacy_fallback_model_scalar_covered(self, tmp_path):
        """旧键 `fallback_model` 也可能写成裸模型名标量，不能静默跳过校验。"""
        text = """\
            model:
              default: deepseek-v4.1-flash-1017
              base_url: https://gw.example.com/v1
              api_key: sk-3822
            fallback_model: glm-5
        """
        cfg = engine_model.read_hermes_model_config(_home(tmp_path, text), env={})
        assert "glm-5" in cfg["targets"][0]["models"]
        assert not any("fallback_model" in w and "跳过" in w for w in cfg["warnings"])

    def test_fallback_key_env_resolved(self, tmp_path):
        text = """\
            model:
              default: deepseek-v4.1-flash-1017
              base_url: https://gw.example.com/v1
              api_key: sk-primary
            fallback_providers:
              - provider: backup
                model: glm-5.3
                base_url: https://gw-backup.example.com/v1
                key_env: HERMES_BACKUP_KEY
        """
        cfg = engine_model.read_hermes_model_config(
            _home(tmp_path, text), env={"HERMES_BACKUP_KEY": "sk-backup"})
        backup = [t for t in cfg["targets"] if t["base_url"] == "https://gw-backup.example.com/v1"]
        assert backup, "fallback 自带 base_url + key_env 必须生成独立校验 target"
        assert backup[0]["api_key"] == "sk-backup"

    def test_duplicate_entries_deduped(self, tmp_path):
        text = """\
            model:
              default: deepseek-v4.1-flash-1017
              base_url: https://gw.example.com/v1
              api_key: sk-primary
            fallback_providers:
              - {provider: custom, model: glm-5.3, base_url: https://gw.example.com/v1}
              - {provider: custom, model: glm-5.3, base_url: https://gw.example.com/v1}
        """
        cfg = engine_model.read_hermes_model_config(_home(tmp_path, text), env={})
        assert cfg["targets"][0]["models"].count("glm-5.3") == 1

    def test_misaligned_fallback_model_fails_validation(self, tmp_path, monkeypatch):
        """本机实测形态：primary 对齐、fallback 里 2 个 404 id → 必须判 misaligned。"""
        monkeypatch.setenv("HERMES_KEY_3822", "sk-3822")
        res = engine_model.validate_engine_model(
            "hermes", config_path=_home(tmp_path, FALLBACK_FIXTURE),
            fetch=_fetch_available())
        assert res["status"] == "misaligned"
        assert "deepseek-v4-flash-0731" in res["message"]
        assert "glm-5.3" in res["message"]
        assert "deepseek-v4.1-flash-1017" in res["message"]     # 网关可用列表


class TestFallbackParserSequences:
    def test_sequence_supported_without_pyyaml(self, tmp_path, monkeypatch):
        """无 pyyaml 时，列表形态的 fallback_providers 不得让整份配置「无法解析」。"""
        monkeypatch.setattr(engine_model, "_yaml", None)
        cfg = engine_model.read_hermes_model_config(
            _home(tmp_path, FALLBACK_FIXTURE),
            env={"HERMES_KEY_3822": "sk-3822"})
        assert cfg["default_model"] == "deepseek-v4.1-flash-1017"
        assert "deepseek-v4-flash-0731" in cfg["targets"][0]["models"]
        assert cfg["warnings"] == [] or all("cannot parse" not in w for w in cfg["warnings"])

    def test_sequence_parity_with_pyyaml(self, tmp_path, monkeypatch):
        def _models(name):
            cfg = engine_model.read_hermes_model_config(
                _home(tmp_path, FALLBACK_FIXTURE, name=name),
                env={"HERMES_KEY_3822": "sk-3822"})
            return sorted(cfg["targets"][0]["models"]), cfg["default_model"]

        with_yaml = _models("parity-a")
        monkeypatch.setattr(engine_model, "_yaml", None)
        without_yaml = _models("parity-b")
        assert with_yaml == without_yaml

    def test_block_sequence_with_nested_mapping(self):
        text = textwrap.dedent("""\
            model:
              default: m0
              base_url: https://gw/v1
              api_key: k
            fallback_providers:
              - provider: p1
                model: m1
              - provider: p2
                model: m2
            other:
              plain: 1
        """)
        parsed = engine_model._fallback_parse(text)
        assert isinstance(parsed["fallback_providers"], list)
        assert parsed["fallback_providers"][0]["model"] == "m1"
        assert parsed["fallback_providers"][1]["provider"] == "p2"
        assert parsed["other"]["plain"] == "1"

    def test_scalar_sequence_supported(self):
        parsed = engine_model._fallback_parse("items:\n  - a\n  - b\n")
        assert parsed["items"] == ["a", "b"]


class TestNoFalseRejection:
    def test_multi_gateway_one_unreachable_does_not_reject(self, tmp_path):
        text = """\
            model:
              default: deepseek-v4.1-flash-1017
              base_url: https://gw.example.com/v1
              api_key: sk-a
            fallback_providers:
              - provider: backup
                model: glm-5.3
                base_url: https://gw-down.example.com/v1
                api_key: sk-b
        """
        def _fetch(url, api_key, timeout):
            if "gw-down" in url:
                raise ConnectionError("gateway down")
            return 200, json.dumps({"data": [{"id": m} for m in AVAILABLE]})

        res = engine_model.validate_engine_model(
            "hermes", config_path=_home(tmp_path, text), env={}, fetch=_fetch)
        assert res["status"] == "unreachable"      # 放行，不因一个网关不可达判死

    def test_multi_gateway_one_misaligned_rejects(self, tmp_path):
        text = """\
            model:
              default: deepseek-v4.1-flash-1017
              base_url: https://gw.example.com/v1
              api_key: sk-a
            fallback_providers:
              - provider: backup
                model: glm-5
                base_url: https://gw-backup.example.com/v1
                api_key: sk-b
        """
        def _fetch(url, api_key, timeout):
            return 200, json.dumps({"data": [{"id": m} for m in AVAILABLE]})

        res = engine_model.validate_engine_model(
            "hermes", config_path=_home(tmp_path, text), env={}, fetch=_fetch)
        assert res["status"] == "misaligned"       # 真缺失才拒
        assert "glm-5" in res["message"]

    def test_undefined_key_env_skips_target_without_rejecting(self, tmp_path):
        text = """\
            model:
              default: deepseek-v4.1-flash-1017
              base_url: https://gw.example.com/v1
              api_key: sk-a
            custom_providers:
              ds:
                model: glm-5
                base_url: https://gw-unkeyed.example.com/v1
                key_env: HERMES_UNDEFINED_KEY_3822
        """
        res = engine_model.validate_engine_model(
            "hermes", config_path=_home(tmp_path, text), env={}, fetch=_fetch_available())
        assert res["status"] != "misaligned"
        assert "HERMES_UNDEFINED_KEY_3822" not in res["message"] or "api_key" in res["message"]

    def test_primary_key_not_sent_to_foreign_fallback_gateway(self, tmp_path):
        """fallback 自带第三方 base_url 且无 key → 跳过该网关，绝不把主网关 key 串用过去。"""
        text = """\
            model:
              default: deepseek-v4.1-flash-1017
              base_url: https://gw.example.com/v1
              api_key: sk-primary
            fallback_providers:
              - provider: vendor
                model: glm-5.3
                base_url: https://third-party.example.com/v1
        """
        cfg = engine_model.read_hermes_model_config(_home(tmp_path, text), env={})
        urls = {t["base_url"]: t["api_key"] for t in cfg["targets"]}
        assert "https://third-party.example.com/v1" not in urls, \
            "第三方网关没有自己的 key 时不得生成 target（避免凭证串用）"
        assert any("third-party" in w and "api_key" in w for w in cfg["warnings"])