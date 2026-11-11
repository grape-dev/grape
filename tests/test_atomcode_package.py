"""Offline worker distribution must contain a usable AtomCode runtime."""
import json
import os
import subprocess
import sys
import tarfile


def test_packaged_atomcode_imports_and_executes_offline(tmp_path, monkeypatch):
    import src.worker_package as package

    cache = tmp_path / "cache"
    wheels = tmp_path / "wheels"
    wheels.mkdir()
    monkeypatch.setattr(package, "CACHE_DIR", str(cache))
    monkeypatch.setattr(package, "PKG_PATH", str(cache / "worker.tar.gz"))
    monkeypatch.setattr(package, "VERSION_PATH", str(cache / "VERSION"))
    monkeypatch.setattr(package, "_find_pythons", lambda: {})
    monkeypatch.setattr(package, "_scan_pythons", lambda: {})
    monkeypatch.setattr(package, "_ensure_wheels", lambda *args: str(wheels))
    monkeypatch.setattr(package, "_ensure_grape_cli_wheel", lambda path: path)
    with tarfile.open(package.build_package()) as archive:
        for relative in ("src/runtime/atomcode.py", "src/agent/atomcode.py",
                         "src/runtime/atomcode_probe.py"):
            assert f"grape-worker/{relative}" in archive.getnames()
        archive.extractall(tmp_path / "extracted", filter="data")

    root = tmp_path / "extracted" / "grape-worker"
    cli = tmp_path / "atomcode"
    events = [
        {"type": "run.started", "schema_version": 1},
        {"type": "tool.started", "name": "read_file", "arguments": {"path": "example"}},
        {"type": "tool.completed", "name": "read_file", "content": "example"},
        {"type": "message.delta", "text": "package "},
        {"type": "message.delta", "text": "works"},
        {"type": "usage", "prompt_tokens": 11, "completion_tokens": 2},
        {"type": "turn.completed", "stop_reason": "Stopped", "exit_code": 0},
    ]
    cli.write_text(f"#!{sys.executable}\nimport sys\n"
                   "assert sys.stdin.read() == 'private packaged prompt'\n"
                   f"print({json.dumps(chr(10).join(json.dumps(event) for event in events))})\n")
    cli.chmod(0o700)
    code = """
import json, pathlib, sys
sys.path.insert(0, sys.argv[1])
import src.runtime.agent
from src.runtime import get_runtime
from src.runtime import os_adapter
from src.agent import executor
from src.runtime.usage_collect import UsageCollector
assert pathlib.Path(executor.__file__).is_relative_to(pathlib.Path(sys.argv[1]))
assert get_runtime('atomcode').name == 'atomcode'
os_adapter.find_cli = lambda name: sys.argv[2] if name == 'atomcode' else ''
executor._assemble_env = lambda *args, **kwargs: None
events = list(executor.stream_agent('private packaged prompt', sys.argv[3],
                                    backend_name='atomcode', max_exec_seconds=10))
done = [value for kind, value in events if kind == 'done']
assert len(done) == 1 and done[0]['exit_code'] == 0, done
assert done[0]['text'] == 'package works', done
assert any(kind == 'tool_use' for kind, value in events)
assert any(kind == 'tool_result' for kind, value in events)
collector = UsageCollector('atomcode', cwd=sys.argv[3])
collector.start()
for kind, value in events:
    collector.feed(kind, value)
usage = collector.finish()
assert len(usage) == 1 and usage[0]['input'] == 11, usage
print('PACKAGED_ATOMCODE_OK')
"""
    env = {**os.environ, "DEVOPS_DB_PATH": str(tmp_path / "isolated.db"),
           "DEVOPS_DB_BACKEND": "sqlite", "DEVOPS_WORKSPACE_DIR": str(tmp_path / "workspace"),
           "GRAPE_AGENT_SANDBOX": "off", "GITCODE_API_TOKEN": "test_token"}
    result = subprocess.run([sys.executable, "-I", "-c", code, str(root), str(cli), str(tmp_path)],
                            cwd=tmp_path, env=env, text=True, capture_output=True, timeout=30)
    assert result.returncode == 0, result.stderr[-3000:] + result.stdout[-1000:]
    assert "PACKAGED_ATOMCODE_OK" in result.stdout
