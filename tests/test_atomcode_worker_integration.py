"""AtomCode Worker registration, probing and display contracts."""

import json
import subprocess
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from src.runtime.atomcode_probe import cli_support_error as real_cli_support_error


def _result(*events, code=0):
    return SimpleNamespace(returncode=code, stdout="\n".join(json.dumps(e) for e in events), stderr="")


START = {"type": "run.started", "schema_version": 1,
         "provider": "AtomGit-glm5.3-flash", "model": "glm5.3-flash"}
END = {"type": "turn.completed", "stop_reason": "Stopped", "exit_code": 0}


def test_worker_routes_atomcode_and_scans_user_skills(tmp_path, monkeypatch):
    from src.runtime import agent, os_adapter

    skill = tmp_path / ".atomcode" / "skills" / "my-skill"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text("---\nname: my-skill\ndescription: example\n---\n")
    monkeypatch.setattr(os_adapter, "home_dir", lambda: str(tmp_path))
    assert agent._backend_for_engine("atomcode-mac-123") == "atomcode"
    assert {"engine": "atomcode", "skills": ["my-skill"]} in agent._scan_local_skills()


def test_version_alone_does_not_report_atomcode_healthy():
    from src.runtime import agent

    runtime = {"name": "atomcode", "cli_path": "/opt/atomcode", "installed": True}
    with patch("src.runtime.registry.discover_runtimes", return_value=[runtime]), \
         patch("subprocess.run", return_value=SimpleNamespace(returncode=0, stdout="atomcode 5.2.5")), \
         patch.dict(agent._worker_probe_results, {}, clear=True):
        found = agent._discover_local_runtimes()[0]
    assert found["installed"] is True
    assert found["healthy"] is False


def test_atomcode_probe_runs_real_selected_model_without_tools(monkeypatch):
    from src.runtime.atomcode_probe import probe_atomcode

    monkeypatch.setenv("GRAPE_ATOMCODE_PROVIDER", "AtomGit-glm5.3-flash")
    monkeypatch.setenv("GRAPE_ATOMCODE_MODEL", "glm5.3-flash")
    with patch("subprocess.run", return_value=_result(START, {"type": "message.delta", "text": "READY"}, END)) as run:
        result = probe_atomcode("/opt/atomcode")
    command = run.call_args.args[0]
    assert result["status"] == "ok"
    assert command[0] == "/opt/atomcode"
    assert all(flag in command for flag in ("--ephemeral", "--no-tools", "--dev", "--no-telemetry", "--output-format"))
    assert command[command.index("--provider") + 1] == "AtomGit-glm5.3-flash"
    assert command[command.index("--model") + 1] == "glm5.3-flash"
    assert "--dangerously-skip-permissions" not in command
    assert run.call_args.kwargs["timeout"] == 60


@pytest.mark.parametrize("events", [
    [START],
    [START, {"type": "turn.completed", "stop_reason": "RateLimited", "exit_code": 0}],
    [START, {"type": "run.failed", "exit_code": 1, "message": "auth failed"}],
    [START, {"type": "turn.completed", "stop_reason": "Stopped", "exit_code": 1}],
])
def test_atomcode_probe_fails_closed_on_bad_or_missing_terminal(events):
    from src.runtime.atomcode_probe import probe_atomcode

    with patch("subprocess.run", return_value=_result(*events)):
        assert probe_atomcode("/opt/atomcode")["status"] == "fail"


def test_atomcode_probe_handles_timeout():
    from src.runtime.atomcode_probe import probe_atomcode

    with patch("subprocess.run", side_effect=subprocess.TimeoutExpired("atomcode", 60)):
        assert probe_atomcode("/opt/atomcode")["status"] == "fail"


def test_worker_and_server_use_atomcode_probe():
    from src.runtime import agent
    from src.observability import engine_probe

    expected = {"status": "ok", "latency": 0.1, "error": ""}
    with patch("src.runtime.atomcode_probe.probe_atomcode", return_value=expected) as probe:
        assert agent._probe_engine_worker("atomcode", "/opt/atomcode") == expected
        assert engine_probe._probe_engine("atomcode", "/opt/atomcode") == expected
    assert probe.call_count == 2


def test_runtime_filters_recognize_atomcode():
    from src.api.routes.settings import _device_engine_types, _engine_type_of

    assert _engine_type_of({"instance_name": "atomcode-leo-mac"}) == "atomcode"
    assert _device_engine_types({"runtimes": [{"name": "atomcode"}]}) == ["atomcode"]


def test_atomcode_cancel_targets_atomcode_not_opencode(monkeypatch):
    from src.runtime import agent

    with patch("subprocess.run", return_value=SimpleNamespace(stdout="")) as run:
        agent._kill_stale_engine({"cwd": "/tmp/work"}, "atomcode")
    assert any("atomcode" in str(call.args[0]) for call in run.call_args_list)


def test_atomcode_home_overrides_default_skill_root(tmp_path, monkeypatch):
    from src.runtime.os_adapter import engine_skill_dirs

    monkeypatch.setenv("ATOMCODE_HOME", str(tmp_path / "custom-home"))
    assert engine_skill_dirs("atomcode") == [str(tmp_path / "custom-home" / "skills")]


def test_local_probe_uses_shared_model_result_without_version_check():
    from src.runtime.local_probe import _probe_one

    with patch("src.runtime.atomcode_probe.cached_atomcode_probe", return_value={"status": "fail"}) as probe:
        with patch("subprocess.run") as run:
            result = _probe_one({"name": "atomcode", "cli_path": "/opt/atomcode", "installed": True})
    assert result["healthy"] is False
    probe.assert_called_once_with("/opt/atomcode")
    run.assert_not_called()


def test_discovery_reuses_cached_model_probe(monkeypatch):
    import threading
    from src.runtime import atomcode_probe

    monkeypatch.setenv("ATOMCODE_HOME", "/cache-test")
    entered, release, finished = threading.Event(), threading.Event(), threading.Event()
    expected = {"status": "ok", "latency": 0.1, "error": ""}
    def fake_probe(*args, **kwargs):
        entered.set()
        assert release.wait(3)
        return expected
    original = atomcode_probe.probe_atomcode
    def tracked_probe(*args, **kwargs):
        try:
            return original(*args, **kwargs)
        finally:
            finished.set()
    with patch.object(atomcode_probe, "_run_probe", side_effect=fake_probe) as probe, \
         patch.object(atomcode_probe, "probe_atomcode", side_effect=tracked_probe):
        assert atomcode_probe.cached_atomcode_probe("/cache-test-cli")["status"] == "pending"
        assert entered.wait(3)
        assert atomcode_probe.cached_atomcode_probe("/cache-test-cli")["status"] == "pending"
        release.set()
        assert finished.wait(3)
        assert atomcode_probe.cached_atomcode_probe("/cache-test-cli") == expected
    assert probe.call_count == 1


def test_transient_retry_does_not_requeue_atomcode():
    from unittest.mock import MagicMock
    from src.runtime import transient_retry

    cursor = MagicMock()
    cursor.execute.return_value.fetchone.side_effect = [
        ("running", 0, "", 10), ("atomcode",),
    ]
    with patch.object(transient_retry, "db") as db, patch("threading.Timer") as timer:
        db.cursor.return_value = cursor
        assert transient_retry.retry_transient_task(123, 1, "connection reset") is False
        timer.assert_not_called()


def test_atomcode_probe_rejects_duplicate_terminal():
    from src.runtime.atomcode_probe import probe_atomcode

    with patch("subprocess.run", return_value=_result(START, {"type": "message.delta", "text": "READY"}, END, END)):
        assert probe_atomcode("/opt/atomcode")["status"] == "fail"


@pytest.fixture(autouse=True)
def compatible_atomcode(monkeypatch):
    monkeypatch.setattr("src.runtime.atomcode_probe.cli_support_error", lambda cli: "")


@pytest.mark.parametrize("events", [
    [START, {"type": "error", "message": "fatal"}, END],
    [{"type": "message.delta", "text": "before start"}, START, END],
    [START, END, {"type": "message.delta", "text": "after terminal"}],
    [{**START, "schema_version": True}, END],
])
def test_probe_rejects_invalid_event_sequence(events):
    from src.runtime.atomcode_probe import probe_atomcode
    with patch("subprocess.run", return_value=_result(*events)):
        assert probe_atomcode("/opt/atomcode")["status"] == "fail"


def test_atomcode_timeout_does_not_replay_task():
    from src.runtime.agent import _timeout_retry_limit
    with patch("src.runtime.task_timeout.timeout_retry_count", return_value=3) as count:
        assert _timeout_retry_limit({}, "atomcode-worker-123") == 0
        count.assert_not_called()
        assert _timeout_retry_limit({}, "codex-worker-123") == 3


@pytest.mark.parametrize("help_text,expected", [
    ("--ephemeral --no-tools --prompt-file --output-format --dev --no-telemetry", ""),
    ("old atomcode --prompt-file", "lacks required headless flags"),
])
def test_cli_feature_detection(help_text, expected):
    with patch("subprocess.run", return_value=SimpleNamespace(returncode=0, stdout=help_text)):
        assert expected in real_cli_support_error("/opt/atomcode")


def test_probe_windows_is_explicitly_unhealthy():
    from src.runtime.atomcode_probe import probe_atomcode
    with patch("src.runtime.atomcode_probe.os.name", "nt"), patch("subprocess.run") as run:
        assert "macOS or Linux" in probe_atomcode("/opt/atomcode")["error"]
        run.assert_not_called()


@pytest.mark.parametrize("bad_line", ["invalid json", "[]", "null"])
def test_probe_rejects_malformed_protocol_line(bad_line):
    from src.runtime.atomcode_probe import probe_atomcode
    result = _result(START, {"type": "message.delta", "text": "READY"}, END)
    result.stdout = result.stdout + "\n" + bad_line
    with patch("subprocess.run", return_value=result):
        assert probe_atomcode("/opt/atomcode")["status"] == "fail"


def test_explicit_and_discovery_probes_share_one_model_call(monkeypatch):
    import threading
    from concurrent.futures import ThreadPoolExecutor
    from src.runtime import atomcode_probe

    entered, waiting, release = threading.Event(), threading.Event(), threading.Event()
    class FlightEvent(threading.Event):
        def wait(self, timeout=None):
            waiting.set()
            return super().wait(timeout)
    expected = {"status": "ok", "latency": 0.1, "error": ""}
    def fake_probe(*args, **kwargs):
        entered.set()
        assert release.wait(3)
        return expected
    monkeypatch.setattr(atomcode_probe, "_ProbeEvent", FlightEvent)
    with patch.object(atomcode_probe, "_run_probe", side_effect=fake_probe) as probe:
        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(atomcode_probe.probe_atomcode, "/singleflight-cli")
            assert entered.wait(3)
            second = pool.submit(atomcode_probe.probe_atomcode, "/singleflight-cli")
            assert waiting.wait(3)
            release.set()
            assert first.result(3) == second.result(3) == expected
        assert probe.call_count == 1
