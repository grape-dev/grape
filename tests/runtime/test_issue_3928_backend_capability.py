"""Issue #3928：引擎后端 CLI 能力探针（可用/不可用/降级/未知 + 缓存）。"""
from src.domains.runtime.engine import backend_capability as bc


def test_missing_cli_is_unavailable():
    bc.clear_cache()
    cap = bc.probe_engine_capability("codearts-cred0", which=lambda _cmd: None, force=True)
    assert cap["status"] == bc.STATUS_UNAVAILABLE
    assert cap["command"] == "codearts"
    assert cap["executable"] == ""
    assert "not found" in cap["reason"].lower()


def test_found_cli_is_available():
    bc.clear_cache()
    cap = bc.probe_engine_capability(
        "codearts-cred0", which=lambda _cmd: "/usr/local/bin/codearts", force=True)
    assert cap["status"] == bc.STATUS_AVAILABLE
    assert cap["executable"] == "/usr/local/bin/codearts"


def test_probe_result_is_cached():
    bc.clear_cache()
    calls = []

    def _which(cmd):
        calls.append(cmd)
        return "/opt/bin/codearts"

    first = bc.probe_engine_capability("codearts", which=_which, now=1000.0)
    second = bc.probe_engine_capability("codearts", which=_which, now=1001.0)
    assert first["status"] == bc.STATUS_AVAILABLE
    assert second["status"] == bc.STATUS_AVAILABLE
    assert calls == ["codearts"], "缓存命中时不应重复执行 which 探测"


def test_force_bypasses_cache():
    bc.clear_cache()
    calls = []

    def _which(cmd):
        calls.append(cmd)
        return None

    bc.probe_engine_capability("codearts", which=_which, now=1000.0)
    bc.probe_engine_capability("codearts", which=_which, now=1001.0, force=True)
    assert calls == ["codearts", "codearts"]


def test_unknown_engine_is_not_blocked():
    bc.clear_cache()
    cap = bc.probe_engine_capability("custom-engine-x", which=lambda _cmd: None, force=True)
    assert cap["status"] == bc.STATUS_UNKNOWN


def test_status_from_probe_mapping():
    assert bc.status_from_probe("codearts", "ok", "") == bc.STATUS_AVAILABLE
    assert bc.status_from_probe(
        "codearts", "fail", "codearts CLI not found in PATH") == bc.STATUS_UNAVAILABLE
    assert bc.status_from_probe("codearts", "fail", "exit=1 config broken") == bc.STATUS_DEGRADED
    assert bc.status_from_probe("codearts", "spawn_failed", "EMFILE") == bc.STATUS_DEGRADED
    assert bc.status_from_probe(
        "codearts", "skip", "not installed") == bc.STATUS_UNAVAILABLE
    assert bc.status_from_probe("codearts", "pending", "") == bc.STATUS_UNKNOWN
    assert bc.status_from_probe("custom-engine-x", "fail", "CLI not found") == bc.STATUS_UNKNOWN