"""AtomCode headless protocol and executor safety contract."""
import json
import os
import sys
import uuid
from pathlib import Path

import pytest

import src.runtime  # initialize runtime registry before importing the executor
from src.agent.executor import discover_backends, get_backend, stream_agent
from src.runtime.atomcode import AtomCodeRuntime
from src.runtime.usage_collect import UsageCollector


def line(**event):
    return json.dumps(event, ensure_ascii=False)


def test_registered_and_discovered():
    assert isinstance(get_backend('atomcode'), AtomCodeRuntime)
    assert any(item['name'] == 'atomcode' for item in discover_backends())


def test_secure_args_and_worker_model_selection():
    runtime = AtomCodeRuntime()
    args = runtime.build_args_for_env('private prompt', 'ignored-session', {
        'GRAPE_ATOMCODE_PROVIDER': 'AtomGit-glm5.3-flash',
        'GRAPE_ATOMCODE_MODEL': 'glm5.3-flash',
    })
    assert args[args.index('--prompt-file') + 1] == '/dev/stdin'
    assert args[args.index('--provider') + 1] == 'AtomGit-glm5.3-flash'
    assert args[args.index('--model') + 1] == 'glm5.3-flash'
    assert args[args.index('--output-format') + 1] == 'jsonl'
    assert '--ephemeral' in args and '--dev' in args and '--no-telemetry' in args
    assert 'private prompt' not in args
    assert '--dangerously-skip-permissions' not in args
    assert '-y' not in args
    assert '--resume' not in args


def test_protocol_maps_tools_and_incremental_usage():
    rt = AtomCodeRuntime()
    assert rt.parse_output(line(type='run.started', schema_version=1))['type'] == 'step_start'
    assert rt.parse_output(line(type='message.delta', text='hello'))['text'] == 'hello'
    call = rt.parse_output(line(type='tool.started', name='bash', arguments='{"command":"pwd"}'))
    assert call == {'type': 'tool_use', 'tool': 'bash', 'input': {'command': 'pwd'}}
    assert 'bad' in rt.parse_output(line(type='tool.completed', content='bad', is_error=True))['text']
    usage = rt.parse_output(line(type='usage', prompt_tokens=10, completion_tokens=3, cached_tokens=4))
    assert usage == {'type': 'usage', 'usage': {'input': 10, 'output': 3, 'cache_read': 4}}
    terminal = rt.parse_output(line(type='turn.completed', stop_reason='Stopped', exit_code=0,
                                    prompt_tokens=10, completion_tokens=3))
    assert terminal['type'] == 'terminal' and terminal['ok'] is True
    assert 'usage' not in terminal
    assert rt.parse_output('not json') is None
    assert rt.parse_output('[]') is None


def test_usage_collector_returns_streamed_atomcode_usage_once():
    collector = UsageCollector('atomcode', cwd='/tmp')
    collector.start()
    collector.feed('usage', {'input': 10, 'output': 3, 'cache_read': 4})
    assert collector.finish() == [{'input': 10, 'output': 3, 'cache_read': 4, 'turn': 1}]


@pytest.fixture
def fake_atomcode(tmp_path, monkeypatch):
    script = tmp_path / 'atomcode'
    script.write_text(f'''#!{sys.executable}
import json, os, sys
from pathlib import Path
prompt = sys.stdin.read()
capture = Path(os.environ['FAKE_CAPTURE'])
with capture.open('a') as f: f.write(json.dumps({{'argv': sys.argv[1:], 'prompt': prompt}}) + "\\n")
for event in json.loads(os.environ['FAKE_EVENTS']): print(json.dumps(event), flush=True)
if os.environ.get('FAKE_STDERR'): print(os.environ['FAKE_STDERR'], file=sys.stderr)
sys.exit(int(os.environ.get('FAKE_EXIT', '0')))
''')
    script.chmod(0o755)
    monkeypatch.setattr('src.runtime.os_adapter.find_cli', lambda name: str(script) if name == 'atomcode' else None)
    monkeypatch.setattr('src.agent.executor._assemble_env', lambda *a, **kw: None)
    monkeypatch.setenv('GRAPE_AGENT_SANDBOX', '0')
    return tmp_path / 'capture.jsonl'


def run_fake(fake_atomcode, events, *, stderr='', exit_code=0):
    env = {'FAKE_CAPTURE': str(fake_atomcode), 'FAKE_EVENTS': json.dumps(events),
           'FAKE_STDERR': stderr, 'FAKE_EXIT': str(exit_code),
           'GRAPE_ATOMCODE_PROVIDER': 'AtomGit-glm5.3-flash',
           'GRAPE_ATOMCODE_MODEL': 'glm5.3-flash'}
    output = list(stream_agent('private prompt\nsecond line', str(fake_atomcode.parent),
                               backend_name='atomcode', env_extra=env, max_exec_seconds=20))
    return output, [json.loads(line) for line in fake_atomcode.read_text().splitlines()]


def test_executor_streams_prompt_and_usage_without_duplication(fake_atomcode, caplog):
    events = [
        {'type':'run.started', 'schema_version':1},
        {'type':'message.delta', 'text':'hello '},
        {'type':'message.delta', 'text':'world'},
        {'type':'usage', 'prompt_tokens':10, 'completion_tokens':3, 'cached_tokens':4},
        {'type':'turn.completed', 'stop_reason':'Stopped', 'exit_code':0, 'prompt_tokens':10},
    ]
    output, calls = run_fake(fake_atomcode, events)
    assert len(calls) == 1 and calls[0]['prompt'] == 'private prompt\nsecond line'
    assert not any('private prompt' in arg for arg in calls[0]['argv'])
    assert 'private prompt' not in caplog.text
    done = next(data for typ, data in output if typ == 'done')
    assert done['exit_code'] == 0 and done['text'] == 'hello world'
    assert [data for typ, data in output if typ == 'usage'] == [{'input':10, 'output':3, 'cache_read':4}]


@pytest.mark.parametrize('events,stderr,exit_code', [
    ([{'type':'run.started','schema_version':1}, {'type':'message.delta','text':'partial'}], '', 0),
    ([{'type':'run.started','schema_version':1}, {'type':'turn.completed','stop_reason':'RateLimited','exit_code':0}], '', 0),
    ([{'type':'run.started','schema_version':1}, {'type':'turn.completed','stop_reason':'Stopped','exit_code':0}], '[denied] requires interactive approval', 0),
    ([{'type':'run.started','schema_version':1}, {'type':'run.failed','message':'bad','exit_code':1}], '', 1),
    ([{'type':'run.started','schema_version':99}, {'type':'turn.completed','stop_reason':'Stopped','exit_code':0}], '', 0),
])
def test_executor_rejects_failure_terminal(fake_atomcode, events, stderr, exit_code):
    output, calls = run_fake(fake_atomcode, events, stderr=stderr, exit_code=exit_code)
    done = next(data for typ, data in output if typ == 'done')
    assert done['exit_code'] != 0
    assert done['errors']
    assert len(calls) == 1


def test_tool_execution_followed_by_abrupt_exit_is_not_retried(fake_atomcode):
    events = [
        {'type':'run.started','schema_version':1},
        {'type':'tool.started','name':'bash','arguments':'{"command":"create issue"}'},
    ]
    output, calls = run_fake(fake_atomcode, events, exit_code=1)
    assert len(calls) == 1
    assert next(data for typ, data in output if typ == 'done')['exit_code'] != 0


@pytest.mark.parametrize('env', [
    {'GRAPE_ATOMCODE_PROVIDER': 'provider'}, {'GRAPE_ATOMCODE_MODEL': 'model'},
])
def test_partial_model_configuration_fails(env):
    with pytest.raises(ValueError, match='must be set together'):
        AtomCodeRuntime().build_args_for_env('prompt', '', env)


@pytest.mark.parametrize('events', [
    [{'type': 'turn.completed', 'stop_reason': 'Stopped', 'exit_code': 0}],
    [{'type': 'run.started', 'schema_version': 1},
     {'type': 'turn.completed', 'stop_reason': 'Stopped', 'exit_code': 0},
     {'type': 'turn.completed', 'stop_reason': 'Stopped', 'exit_code': 0}],
    [{'type': 'run.started', 'schema_version': 1},
     {'type': 'turn.completed', 'stop_reason': 'Stopped', 'exit_code': 0},
     {'type': 'message.delta', 'text': 'late'}],
])
def test_protocol_order_is_strict(fake_atomcode, events):
    output, calls = run_fake(fake_atomcode, events)
    assert len(calls) == 1
    assert next(data for typ, data in output if typ == 'done')['exit_code'] != 0


def test_retryable_diagnostic_can_recover_without_replaying_run(fake_atomcode):
    output, calls = run_fake(fake_atomcode, [
        {'type': 'run.started', 'schema_version': 1},
        {'type': 'error', 'retryable': True, 'message': 'temporary'},
        {'type': 'message.delta', 'text': 'recovered'},
        {'type': 'turn.completed', 'stop_reason': 'Stopped', 'exit_code': 0},
    ])
    assert len(calls) == 1
    assert any(typ == 'warning' for typ, _ in output)
    assert next(data for typ, data in output if typ == 'done')['exit_code'] == 0


def test_runtime_auxiliary_protocol_cases():
    rt = AtomCodeRuntime()
    assert rt.parse_output(line(type='reasoning.delta', text='think'))['type'] == 'reasoning'
    assert rt.parse_output(line(type='message.delta', text='')) is None
    assert rt.parse_output(line(type='tool.started', arguments='broken'))['input'] == {'raw': 'broken'}
    assert rt.parse_output(line(type='tool.started', arguments=[]))['input'] == {'raw': []}
    assert rt.parse_output(line(type='usage', prompt_tokens='bad', cached_tokens=-3))['usage']['input'] == 0
    assert rt.parse_output(line(type='error', message='fatal'))['type'] == 'error'
    assert rt.parse_output(line(type='warning', message='note'))['type'] == 'warning'
    assert rt.parse_output(line(type='future.event'))['type'] == 'other'


def test_startup_oserror_is_reported(monkeypatch, tmp_path):
    monkeypatch.setattr('src.runtime.os_adapter.find_cli', lambda _: '/missing/atomcode')
    monkeypatch.setattr('src.agent.executor._assemble_env', lambda *a, **kw: None)
    output = list(stream_agent('prompt', str(tmp_path), backend_name='atomcode'))
    done = next(data for typ, data in output if typ == 'done')
    assert done['exit_code'] != 0 and done['errors']


def test_windows_execution_is_explicitly_unsupported(monkeypatch):
    # Patch only this module's os reference: replacing global os.name breaks pathlib.
    from types import SimpleNamespace
    monkeypatch.setattr('src.runtime.atomcode.os', SimpleNamespace(name='nt', environ={}))
    with pytest.raises(RuntimeError, match='not supported on Windows'):
        AtomCodeRuntime().build_args_for_env('private prompt', '', {})


def test_task_session_token_is_redacted_from_all_output(fake_atomcode, monkeypatch, caplog):
    token = uuid.uuid4().hex
    monkeypatch.setenv('GRAPE_SESSION_TOKEN', token)
    output, calls = run_fake(fake_atomcode, [
        {'type': 'run.started', 'schema_version': 1},
        {'type': 'message.delta', 'text': token},
        {'type': 'tool.started', 'name': 'bash', 'arguments': {'command': token}},
        {'type': 'tool.completed', 'content': token},
        {'type': 'run.failed', 'message': token},
    ], stderr=token)
    assert len(calls) == 1
    assert token not in json.dumps(output)
    assert token not in caplog.text


def test_success_without_conclusion_is_failure_without_retry(fake_atomcode):
    output, calls = run_fake(fake_atomcode, [
        {'type': 'run.started', 'schema_version': 1},
        {'type': 'turn.completed', 'stop_reason': 'Stopped', 'exit_code': 0},
    ])
    assert len(calls) == 1
    assert next(data for typ, data in output if typ == 'done')['exit_code'] != 0


def test_timeout_with_shutdown_text_does_not_become_success(tmp_path, monkeypatch):
    script = tmp_path / 'atomcode'
    script.write_text(f'''#!{sys.executable}
import json, signal, sys, time
sys.stdin.read()
def finish(*_):
    print(json.dumps({{'type': 'message.delta', 'text': 'partial shutdown text'}}), flush=True)
    sys.exit(0)
signal.signal(signal.SIGTERM, finish)
print(json.dumps({{'type': 'run.started', 'schema_version': 1}}), flush=True)
time.sleep(30)
''')
    script.chmod(0o755)
    monkeypatch.setattr('src.runtime.os_adapter.find_cli', lambda _: str(script))
    monkeypatch.setattr('src.agent.executor._assemble_env', lambda *a, **kw: None)
    monkeypatch.setenv('GRAPE_AGENT_SANDBOX', '0')
    monkeypatch.setenv('AGENT_SILENT_EXTEND_CAP', '0')
    output = list(stream_agent('test', str(tmp_path), backend_name='atomcode',
                              env_extra={'AGENT_SILENT_WINDOW': '0.1'}, max_exec_seconds=2))
    done = next(data for typ, data in output if typ == 'done')
    assert done['exit_code'] != 0
    assert done['errors']


def test_stderr_log_redacts_before_truncation(fake_atomcode, monkeypatch, caplog):
    token = uuid.uuid4().hex
    monkeypatch.setenv('GRAPE_SESSION_TOKEN', token)
    caplog.set_level('INFO', logger='devops-bot')
    run_fake(fake_atomcode, [
        {'type': 'run.started', 'schema_version': 1},
        {'type': 'message.delta', 'text': 'done'},
        {'type': 'turn.completed', 'stop_reason': 'Stopped', 'exit_code': 0},
    ], stderr='x' * 110 + token)
    assert token[:10] not in caplog.text
