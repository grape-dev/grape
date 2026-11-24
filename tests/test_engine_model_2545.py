"""Issue #2545：hermes 模型名统一读取（src/runtime/engine_model.py，Seam A-1）。

公开接口 read_hermes_model_config()：从 hermes config.yaml 提取
model.default / model_aliases / custom_providers 的模型名与网关（base_url+api_key），
作为安装期校验与探针最小调用的统一模型名来源。

worker 侧依赖仅 stdlib（requirements-worker.txt 无 pyyaml）→ 模块内置降级解析器，
pyyaml 可用时优先使用；两种解析路径必须产出一致的模型信息。
"""
import os
import sys
import textwrap

import pytest

ROOT = os.path.join(os.path.dirname(__file__), "..")
sys.path.insert(0, ROOT)

from src.domains.runtime.engine import engine_model

FIXTURE = textwrap.dedent("""\
    model:
      default: deepseek-v4-flash-0731
      provider: custom
      base_url: https://gw.example.com/v1
      api_key: ${HERMES_TEST_KEY}
    agent:
      max_turns: 150
    model_aliases:
      glm-5.3: {model: glm-5.3, provider: custom, base_url: "https://gw.example.com/v1"}
      glm-5.3-flash: {model: glm-5.3-flash, provider: custom, base_url: "https://gw.example.com/v1"}
    custom_providers:
      deepseek:
        model: deepseek-v4-flash-0731
        base_url: https://api.deepseek.com/v1
        api_key: sk-literal-key
    providers:
      opengw:
        api: https://gw.example.com/v1
        name: opengw
        key_env: HERMES_TEST_KEY
        models:
          glm-5.3: {}
          glm-5.3-flash: {}
        default_model: glm-5.3
    _config_version: 52
""")


@pytest.fixture()
def hermes_home(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_TEST_KEY", "sk-test-env-key")
    home = tmp_path / "hermes-home"
    home.mkdir()
    (home / "config.yaml").write_text(FIXTURE, encoding="utf-8")
    return str(home)


class TestReadHermesModelConfig:
    def test_default_model_and_gateway_extracted(self, hermes_home):
        """default 模型名 + 网关地址 + api_key（${ENV} 解析为环境变量值）。"""
        cfg = engine_model.read_hermes_model_config(hermes_home)
        assert cfg["default_model"] == "deepseek-v4-flash-0731"
        first = cfg["targets"][0]
        assert first["base_url"] == "https://gw.example.com/v1"
        assert first["api_key"] == "sk-test-env-key"   # ${HERMES_TEST_KEY} 已解析
        assert "deepseek-v4-flash-0731" in first["models"]

    def test_alias_models_grouped_into_same_gateway_target(self, hermes_home):
        """同网关的 model_aliases 模型并入同一 target 的 models 全集。"""
        cfg = engine_model.read_hermes_model_config(hermes_home)
        first = cfg["targets"][0]
        assert "glm-5.3" in first["models"] and "glm-5.3-flash" in first["models"]

    def test_custom_provider_becomes_own_target(self, hermes_home):
        """custom_providers 自带 base_url/api_key → 独立 target（不同网关单独校验）。"""
        cfg = engine_model.read_hermes_model_config(hermes_home)
        ds = [t for t in cfg["targets"] if t["base_url"] == "https://api.deepseek.com/v1"]
        assert ds, "custom_providers 网关必须生成独立校验 target"
        assert ds[0]["api_key"] == "sk-literal-key"
        assert "deepseek-v4-flash-0731" in ds[0]["models"]

    def test_missing_config_returns_none(self, tmp_path):
        """config.yaml 不存在 → None（调用方决定跳过校验，不抛错）。"""
        empty = tmp_path / "no-hermes"
        empty.mkdir()
        assert engine_model.read_hermes_model_config(str(empty)) is None

    def test_key_env_via_providers_section_resolved(self, tmp_path, monkeypatch):
        """model.api_key 缺失时经 providers.<name>.key_env 解析（hermes 常见形态）。"""
        monkeypatch.setenv("HERMES_TEST_KEY", "sk-env-key")
        cfg_text = textwrap.dedent("""\
            model:
              default: glm-5.3
              provider: opengw
            providers:
              opengw:
                api: https://gw.example.com/v1
                key_env: HERMES_TEST_KEY
        """)
        home = tmp_path / "h2"
        home.mkdir()
        (home / "config.yaml").write_text(cfg_text, encoding="utf-8")
        cfg = engine_model.read_hermes_model_config(str(home))
        assert cfg["default_model"] == "glm-5.3"
        assert cfg["targets"][0]["api_key"] == "sk-env-key"
        assert cfg["targets"][0]["base_url"] == "https://gw.example.com/v1"

    def test_custom_provider_key_env_fallback_resolved(self, tmp_path, monkeypatch):
        """custom_providers 条目缺 api_key 时经 key_env 解析（小助手提取前锁定该路径行为）。"""
        monkeypatch.setenv("HERMES_CUSTOM_KEY", "sk-custom-env-key")
        cfg_text = textwrap.dedent("""\
            model:
              default: glm-5.3
              base_url: https://gw.example.com/v1
              api_key: sk-k
            custom_providers:
              ds:
                model: deepseek-v4-flash-0731
                base_url: https://api.deepseek.com/v1
                key_env: HERMES_CUSTOM_KEY
        """)
        home = tmp_path / "h3"
        home.mkdir()
        (home / "config.yaml").write_text(cfg_text, encoding="utf-8")
        cfg = engine_model.read_hermes_model_config(str(home))
        ds = [t for t in cfg["targets"] if t["base_url"] == "https://api.deepseek.com/v1"]
        assert ds, "custom_providers 仅含 key_env 的条目也必须生成独立校验 target"
        assert ds[0]["api_key"] == "sk-custom-env-key"


class TestFallbackParserParity:
    def test_parse_without_pyyaml_yields_same_model_info(self, hermes_home, monkeypatch):
        """降级解析器（无 pyyaml 环境）与 pyyaml 产出一致的模型信息。

        worker 安装机 venv 无 pyyaml（requirements-worker.txt），降级路径必须可用。
        """
        cfg_with_yaml = engine_model.read_hermes_model_config(hermes_home)
        monkeypatch.setattr(engine_model, "_yaml", None)   # 强制降级解析器
        cfg_fallback = engine_model.read_hermes_model_config(hermes_home)
        assert cfg_fallback == cfg_with_yaml


# ===== Seam A-2：网关 /v1/models 三分支校验 + CLI 退出码（Issue #2545 验收）=====

def _make_cfg(base_url="https://gw.example.com/v1", api_key="sk-k", models=("deepseek-v4-flash-0731",)):
    return {"config_path": "/tmp/config.yaml", "default_model": models[0],
            "targets": [{"base_url": base_url, "api_key": api_key, "models": list(models)}],
            "warnings": []}


class TestGatewayModelsUrl:
    def test_base_url_with_v1_suffix(self):
        """base_url 已含 /v1 → 直接补 /models（真实网关形态 opengw.../v1）。"""
        assert engine_model.gateway_models_url("https://gw.example.com/v1") == \
            "https://gw.example.com/v1/models"

    def test_base_url_without_v1_suffix(self):
        """Spec 形态 {base}/v1/models：base 不带 /v1 时自动补全。"""
        assert engine_model.gateway_models_url("https://gw.example.com") == \
            "https://gw.example.com/v1/models"

    def test_trailing_slash_normalized(self):
        assert engine_model.gateway_models_url("https://gw.example.com/v1/") == \
            "https://gw.example.com/v1/models"


class TestCheckModelsAgainstGateway:
    """合法 / 非法 / 不可达三分支（fetch 注入，免真实网络）。"""

    def _fetch_ok(self, models=("deepseek-v4-flash-0731", "glm-5.3", "glm-5.3-flash")):
        import json as _json
        payload = _json.dumps({"data": [{"id": m} for m in models]})
        return lambda url, key, timeout: (200, payload)

    def test_legal_model_in_list(self):
        """合法：配置模型在 /v1/models 列表 → status=ok。"""
        result = engine_model.check_models_against_gateway(_make_cfg(), fetch=self._fetch_ok())
        assert result["status"] == "ok"
        assert result["detail"][0]["missing"] == []

    def test_illegal_model_not_in_list_reports_comparison(self):
        """非法：模型名不在列表 → status=misaligned，detail 含 configured/missing/available 对照。"""
        cfg = _make_cfg(models=("deepseek-v4-flash-0731", "glm-5.3"))
        result = engine_model.check_models_against_gateway(
            cfg, fetch=self._fetch_ok(models=("glm-5.3", "glm-5.3-flash")))
        assert result["status"] == "misaligned"
        d = result["detail"][0]
        assert d["missing"] == ["deepseek-v4-flash-0731"]
        assert "glm-5.3" in d["available"] and d["available"] != []

    def test_unreachable_gateway_degrades(self):
        """不可达：fetch 抛异常 → status=unreachable（安装期降级告警，不硬失败）。"""
        def _boom(url, key, timeout):
            raise ConnectionRefusedError("no route to host")
        result = engine_model.check_models_against_gateway(_make_cfg(), fetch=_boom)
        assert result["status"] == "unreachable"
        assert result["detail"][0]["status"] == "unreachable"

    def test_gateway_5xx_treated_as_unreachable(self):
        """网关 5xx/限流（408/429）按不可达降级，不误判为配置错。"""
        result = engine_model.check_models_against_gateway(
            _make_cfg(), fetch=lambda u, k, t: (502, "bad gateway"))
        assert result["status"] == "unreachable"

    def test_non_json_payload_not_misjudged(self):
        """/v1/models 返回非 JSON（网关兼容差异）→ invalid_response 告警，不误杀。"""
        result = engine_model.check_models_against_gateway(
            _make_cfg(), fetch=lambda u, k, t: (200, "<html>gateway login page</html>"))
        assert result["status"] != "misaligned"
        assert result["detail"][0]["status"] == "invalid_response"

    def test_missing_targets_is_noop(self):
        """无 resolvable target（如配置缺 key）→ 不校验、不误判。"""
        result = engine_model.check_models_against_gateway(
            {"targets": [], "warnings": ["no key"]}, fetch=self._fetch_ok())
        assert result["status"] == "no_validatable_target"
        assert result["warnings"] == ["no key"]


class TestGatewayChatUrl:
    """chat/completions URL 与 gateway_models_url 同口径归一化（QA Spec1：探活不得打错路由）。"""

    def test_base_url_with_v1_suffix(self):
        assert engine_model.gateway_chat_url("https://gw.example.com/v1") == \
            "https://gw.example.com/v1/chat/completions"

    def test_base_url_without_v1_suffix(self):
        """base_url 不含 /v1（gateway_models_url 兼容的同一形态）→ 补 /v1/chat/completions。"""
        assert engine_model.gateway_chat_url("https://gw.example.com") == \
            "https://gw.example.com/v1/chat/completions"

    def test_trailing_slash_normalized(self):
        assert engine_model.gateway_chat_url("https://gw.example.com/v1/") == \
            "https://gw.example.com/v1/chat/completions"


class TestMinimalModelCall:
    """探活期 1-token 最小补全（Seam B 后端）：404/模型不存在才置不健康。"""

    def _probe_cfg(self):
        return _make_cfg()

    def test_ok_response(self):
        ok, detail = engine_model.minimal_model_call(
            self._probe_cfg(), post=lambda u, k, t, b: (200, '{"choices":[{"message":{}}]}'))
        assert ok is True and detail["model_call"] == "ok"

    def test_404_model_not_found_marks_unhealthy(self):
        ok, detail = engine_model.minimal_model_call(
            self._probe_cfg(), post=lambda u, k, t, b: (404, '{"error":"Model not found"}'))
        assert ok is False
        assert detail["model_call"] == "model_not_found: deepseek-v4-flash-0731"

    def test_404_with_custom_error_body_marks_model_not_found(self):
        """404 + 网关自定义错误文案（非 'Model not found' 字面量）→ 仍按模型不存在处理。"""
        ok, detail = engine_model.minimal_model_call(
            self._probe_cfg(), post=lambda u, k, t, b: (404, '{"error":{"code":"model_disabled"}}'))
        assert ok is False and "model_not_found" in detail["model_call"]

    def test_network_error_keeps_healthy(self):
        """网络不可达不误判探针为不健康（降级 skip）。"""
        def _boom(u, k, t, b):
            raise TimeoutError("connect timeout")
        ok, detail = engine_model.minimal_model_call(self._probe_cfg(), post=_boom)
        assert ok is True and "skipped" in detail["model_call"]

    def test_other_http_status_ignored(self):
        """非 404 的其它状态（如 401 限钥/配额）不越界判死模型。"""
        ok, detail = engine_model.minimal_model_call(
            self._probe_cfg(), post=lambda u, k, t, b: (401, '{"error":"unauthorized"}'))
        assert ok is True and "ignored" in detail["model_call"]

    def test_no_api_key_skips(self):
        cfg = _make_cfg(api_key="")
        ok, detail = engine_model.minimal_model_call(cfg)
        assert ok is True and "skipped" in detail["model_call"]

    def test_chat_url_normalized_when_base_url_has_no_v1(self):
        """QA Spec1：base_url 不含 /v1 时探活必须打 /v1/chat/completions，
        否则普通路由 404 会被误判 model_not_found → healthy=False 误报。"""
        seen = {}

        def _post(url, key, to, body):
            seen["url"] = url
            return 200, '{"choices":[{"message":{}}]}'

        cfg = _make_cfg(base_url="https://gw.example.com")   # 无 /v1 形态
        ok, detail = engine_model.minimal_model_call(cfg, post=_post)
        assert ok is True and detail["model_call"] == "ok"
        assert seen["url"] == "https://gw.example.com/v1/chat/completions"

    def test_chat_url_kept_when_base_url_has_v1(self):
        """base_url 已含 /v1 → /v1/chat/completions（不重复拼 /v1）。"""
        seen = {}

        def _post(url, key, to, body):
            seen["url"] = url
            return 200, '{"choices":[{"message":{}}]}'

        ok, _ = engine_model.minimal_model_call(_make_cfg(), post=_post)
        assert ok is True
        assert seen["url"] == "https://gw.example.com/v1/chat/completions"


class TestCliMain:
    """`python -m src.domains.runtime.engine.engine_model --check` 退出码（安装脚本依赖的可观察行为）。

    用 hermes_home fixture（真读 config.yaml），注入 _default_fetch 模拟网关三分支。
    """

    @staticmethod
    def _fetch_ok():
        import json as _json
        payload = _json.dumps({"data": [{"id": "deepseek-v4-flash-0731"},
                                        {"id": "glm-5.3"}, {"id": "glm-5.3-flash"}]})
        return lambda u, k, t: (200, payload)

    def _run(self, monkeypatch, hermes_home, fetch=None):
        if fetch is not None:
            monkeypatch.setattr(engine_model, "_default_fetch", fetch)
        return engine_model.main(["--check", "--hermes-home", hermes_home])

    def test_cli_exit_zero_when_aligned(self, hermes_home, monkeypatch, capsys):
        rc = self._run(monkeypatch, hermes_home, fetch=self._fetch_ok())
        assert rc == 0
        assert "OK" in capsys.readouterr().out

    def test_cli_exit_one_when_misaligned_prints_comparison(self, hermes_home, monkeypatch, capsys):
        """非法分支：exit 1 且输出「配置模型名 vs 可用列表」对照（Spec 验收）。"""
        rc = self._run(monkeypatch, hermes_home,
                       fetch=lambda u, k, t: (200, '{"data":[{"id":"glm-5.3"}]}'))
        out = capsys.readouterr().out
        assert rc == 1
        assert "deepseek-v4-flash-0731" in out and "glm-5.3" in out

    def test_cli_exit_zero_when_unreachable(self, hermes_home, monkeypatch, capsys):
        """不可达分支：exit 0 + 降级告警（不误伤离线安装）。"""
        def _boom(u, k, t):
            raise OSError("network down")
        rc = self._run(monkeypatch, hermes_home, fetch=_boom)
        out = capsys.readouterr().out
        assert rc == 0
        assert "SKIPPED" in out

    def test_cli_no_config_exit_zero(self, tmp_path, capsys):
        empty = tmp_path / "empty"
        empty.mkdir()
        assert engine_model.main(["--check", "--hermes-home", str(empty)]) == 0
        assert "No hermes config" in capsys.readouterr().out


# ===== Issue #3874：降级解析器遇 sequences 不再异常（非阻断 warning）=====

SEQ_FIXTURE = textwrap.dedent("""\
    model:
      default: deepseek-v4-flash-0731
      provider: custom
      base_url: https://gw.example.com/v1
      api_key: ${HERMES_TEST_KEY}
    custom_providers:
      - model: glm-5.3
        base_url: https://gw.example.com/v1
        api_key: sk-list-key
""")


class TestFallbackParserSequencesNonBlocking:
    def test_fallback_parser_does_not_raise_on_sequences(self):
        """Issue #3874：降级解析器遇 `- ` 列表行不再抛异常，返回已解析映射 + warning。"""
        warnings: list[str] = []
        cfg = engine_model._fallback_parse(SEQ_FIXTURE, warnings)
        assert cfg["model"]["default"] == "deepseek-v4-flash-0731"
        assert any("sequences" in w for w in warnings)

    def test_read_config_keeps_default_model_with_sequences(self, tmp_path, monkeypatch):
        """缺 pyyaml + config 含列表：读取不中断，默认模型 target 仍可解析，附非阻断 warning。"""
        monkeypatch.setenv("HERMES_TEST_KEY", "sk-test-env-key")
        home = tmp_path / "hermes-seq"
        home.mkdir()
        (home / "config.yaml").write_text(SEQ_FIXTURE, encoding="utf-8")
        monkeypatch.setattr(engine_model, "_yaml", None)
        cfg = engine_model.read_hermes_model_config(str(home))
        assert cfg is not None
        assert cfg["default_model"] == "deepseek-v4-flash-0731"
        assert cfg["targets"], "默认模型仍应产生可校验 target"
        assert any("sequences" in w for w in cfg["warnings"])

    def test_flow_sequence_is_non_blocking_warning(self):
        """Issue #3874：内联 `[a, b]` 流式列表同样降级为 warning，不抛异常。"""
        warnings: list[str] = []
        cfg = engine_model._fallback_parse("key: [a, b]\nother: ok\n", warnings)
        assert cfg["other"] == "ok"
        assert any("flow sequences" in w for w in warnings)

    def test_fallback_parser_without_warnings_list_signals_stderr(self, capsys, monkeypatch):
        """Issue #3874 审查建议：未传 warnings 列表时序列块不得静默丢弃，须留 stderr 信号。"""
        monkeypatch.setattr(engine_model, "_yaml", None)
        cfg = engine_model.parse_hermes_config(SEQ_FIXTURE)
        assert cfg["model"]["default"] == "deepseek-v4-flash-0731"
        assert "sequences" in capsys.readouterr().err, "缺 warnings 列表时降级提示必须写 stderr"
