"""Issue #3820：注册阶段拒绝 misaligned opencode + 探活最小模型调用接线。

锁定两处公开 seam：
  - `registry.validate_registration_model()` 委托 engine_model 校验；
  - worker `_discover_local_runtimes()`：opencode 模型名不在 `/v1/models` 可用列表时
    healthy=False（服务端 engine_select 不派单 = 拒绝健康注册）；
  - 本机直连探活 `probe_local_engines()`：opencode 最小模型调用 404 → healthy=False，
    `probe_error` / `probe_detail` 带差异原因（运行时页展示）。
"""
import os
import sys

ROOT = os.path.join(os.path.dirname(__file__), "..", "..")
sys.path.insert(0, ROOT)

from src.domains.runtime.engine import engine_model, local_probe, registry


def test_registry_validate_registration_model_delegates(monkeypatch):
    seen = {}

    def _fake(engine_name, config_path=None, env=None, fetch=None, timeout=None):
        seen.update({"engine": engine_name, "timeout": timeout})
        return {"engine": engine_name, "status": "misaligned", "message": "diff"}

    monkeypatch.setattr(engine_model, "validate_engine_model", _fake)
    res = registry.validate_registration_model("opencode")
    assert res["status"] == "misaligned"
    assert seen["engine"] == "opencode" and seen["timeout"] is not None


def test_discover_local_runtimes_rejects_misaligned_opencode(monkeypatch):
    from src.domains.runtime.engine import agent as agent_mod

    monkeypatch.setattr(registry, "discover_runtimes", lambda: [
        {"name": "opencode", "installed": True, "cli_path": sys.executable},
        {"name": "codex", "installed": True, "cli_path": sys.executable},
    ])
    monkeypatch.setattr(registry, "validate_registration_model", lambda name, **kw: {
        "engine": name, "status": "misaligned",
        "message": "opencode 配置模型名 m 不在网关可用列表（可用: other）"})

    out = {r["name"]: r for r in agent_mod._discover_local_runtimes()}
    assert out["opencode"]["healthy"] is False, "misaligned opencode 不得以 healthy 注册"
    assert out["codex"]["healthy"] is True, "其它引擎不受 opencode 模型配置影响"


def test_discover_local_runtimes_keeps_opencode_when_gateway_unreachable(monkeypatch):
    from src.domains.runtime.engine import agent as agent_mod

    monkeypatch.setattr(registry, "discover_runtimes", lambda: [
        {"name": "opencode", "installed": True, "cli_path": sys.executable},
    ])
    monkeypatch.setattr(registry, "validate_registration_model", lambda name, **kw: {
        "engine": name, "status": "unreachable", "message": "gateway down, skipped"})

    out = {r["name"]: r for r in agent_mod._discover_local_runtimes()}
    assert out["opencode"]["healthy"] is True, "网关不可达时不得误拒注册"


def test_local_probe_opencode_model_not_found_marks_unhealthy(monkeypatch):
    monkeypatch.setattr(engine_model, "probe_engine_model_call",
                        lambda name, **kw: (False, {"model_call": "model_not_found: m"},
                                            "opencode 模型配置错误：配置 m / 可用 other"))
    out = local_probe.probe_local_engines(
        [{"name": "opencode", "cli_path": sys.executable, "installed": True}])[0]
    assert out["healthy"] is False
    assert out["probe_status"] == "failed"
    assert out["probe_detail"]["model_call"] == "model_not_found: m"
    assert "模型配置错误" in out["probe_error"]


def test_local_probe_opencode_model_ok_keeps_healthy(monkeypatch):
    monkeypatch.setattr(engine_model, "probe_engine_model_call",
                        lambda name, **kw: (True, {"model_call": "ok", "model": "m"}, ""))
    out = local_probe.probe_local_engines(
        [{"name": "opencode", "cli_path": sys.executable, "installed": True}])[0]
    assert out["healthy"] is True
    assert "model_not_found" not in str(out.get("probe_detail", {}))
    assert out.get("probe_error", "") == ""


def test_worker_probe_short_circuits_opencode_model_error(monkeypatch):
    from src.domains.runtime.engine import agent as agent_mod

    monkeypatch.setattr(engine_model, "probe_engine_model_call",
                        lambda name, **kw: (False, {"model_call": "model_not_found: m"},
                                            "opencode 模型配置错误：配置 m / 可用 other"))
    res = agent_mod._probe_engine_worker("opencode", sys.executable)
    assert res["status"] == "fail"
    assert "模型配置错误" in res["error"]