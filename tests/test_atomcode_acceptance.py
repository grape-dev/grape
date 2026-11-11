"""Exercise real pipe and process-group behavior without model requests."""
import os
import subprocess
import sys
import threading
import time

import pytest


def _wait_for(predicate, timeout=8):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.02)
    pytest.fail("fake AtomCode process did not reach the expected state")


def _process_running(pid):
    result = subprocess.run(["ps", "-p", str(pid), "-o", "stat="],
                            text=True, capture_output=True, timeout=3)
    return bool(result.stdout.strip()) and not result.stdout.strip().startswith("Z")


@pytest.mark.skipif(os.name == "nt", reason="AtomCode v1 supports macOS and Linux")
def test_cancel_atomcode_only_terminates_matching_cwd(tmp_path, monkeypatch):
    from src.agent.executor import stream_agent, terminate_tracked_agents

    cli = tmp_path / "atomcode"
    cli.write_text(f"#!{sys.executable}\n" + """
import json, os, subprocess, sys, time
from pathlib import Path
sys.stdin.read()
child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])
Path('ready').write_text(json.dumps([os.getpid(), child.pid]))
print(json.dumps({'type': 'run.started', 'schema_version': 1}), flush=True)
time.sleep(60)
""")
    cli.chmod(0o700)
    monkeypatch.setattr("src.runtime.os_adapter.find_cli", lambda name: str(cli))
    monkeypatch.setattr("src.agent.executor._assemble_env", lambda *args, **kwargs: None)
    monkeypatch.setattr("src.config.GRAPE_AGENT_SANDBOX", False)
    workspaces = [tmp_path / "one", tmp_path / "two"]
    results = [[], []]
    errors = []

    def run(index):
        try:
            results[index].extend(stream_agent("wait", str(workspaces[index]),
                                                backend_name="atomcode", max_exec_seconds=20))
        except Exception as error:
            errors.append(error)

    for cwd in workspaces:
        cwd.mkdir()
    threads = [threading.Thread(target=run, args=(index,), daemon=True) for index in range(2)]
    try:
        for thread in threads:
            thread.start()
        _wait_for(lambda: all((cwd / "ready").exists() for cwd in workspaces) or bool(errors))
        assert not errors
        import json
        processes = [json.loads((cwd / "ready").read_text()) for cwd in workspaces]
        terminate_tracked_agents("atomcode", str(workspaces[0]))
        threads[0].join(timeout=5)
        assert not threads[0].is_alive()
        assert threads[1].is_alive()
        assert all(_process_running(pid) for pid in processes[1])
        _wait_for(lambda: all(not _process_running(pid) for pid in processes[0]))
        done = [value for kind, value in results[0] if kind == "done"]
        assert len(done) == 1 and done[0]["exit_code"] != 0
    finally:
        for cwd in workspaces:
            terminate_tracked_agents("atomcode", str(cwd))
        for thread in threads:
            thread.join(timeout=5)
    assert not errors
