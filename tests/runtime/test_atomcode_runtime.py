"""AtomCode CLI protocol and executor integration, without model credentials."""
import json
import os
import sys
import subprocess
from pathlib import Path

import pytest

from src.domains.runtime.engine.atomcode import AtomCodeRuntime


@pytest.fixture
def runtime():
    return AtomCodeRuntime()


def test_prompt_file_lifetime_and_concurrency(runtime, tmp_path):
    prompt = '中文\n"quoted" $HOME `literal`\n' * 1000
    with runtime.prepare_prompt(prompt, str(tmp_path)) as first:
        with runtime.prepare_prompt("second", str(tmp_path)) as second:
            assert first != second
            assert Path(first).parent != tmp_path
            assert Path(first).read_text() == prompt
            assert Path(second).read_text() == "second"
            args = runtime.build_args(first, "session-123")
            assert args[:2] == ["--prompt-file", first]
            assert args[-2:] == ["--resume", "session-123"]
            assert prompt not in args
        assert not Path(second).exists()
    assert not Path(first).exists()
    with pytest.raises(RuntimeError):
        with runtime.prepare_prompt("failed", str(tmp_path)) as failed:
            raise RuntimeError("startup failed")
    assert not Path(failed).exists()


@pytest.mark.parametrize("line", ["bad json", "[]", "null", "42", ""])
def test_noise(runtime, line):
    assert runtime.parse_output(line) is None


@pytest.mark.parametrize("cached", [0, 60, 100, 150])
def test_usage_dashboard_total_counts_cache_once(runtime, cached):
    from src.domains.insight.observability.llm_usage import normalize_tokens
    from src.domains.insight.observability.tokens import tokens_of
    from src.domains.runtime.engine.usage_collect import UsageCollector
    parsed = runtime.parse_output(json.dumps({
        "type": "usage", "prompt_tokens": 100, "completion_tokens": 20, "cached_tokens": cached,
    }))
    collector = UsageCollector("atomcode")
    collector.feed("usage", parsed["usage"])
    usage = collector.finish()[0]
    assert usage["input"] == 100 - min(100, cached)
    assert usage["cache_read"] == min(100, cached)
    assert tokens_of(normalize_tokens(usage))["total"] == parsed["tokens"] == 120


def test_protocol(runtime):
    def parse(kind, **fields):
        return runtime.parse_output(json.dumps({"type": kind, **fields}))
    assert parse("run.started", schema_version=1, session_id="s1")["sessionID"] == "s1"
    assert parse("run.started", schema_version=1)["sessionID"] == ""
    assert parse("run.started", schema_version=99)["type"] == "error"
    assert parse("message.delta", text="片段")["text"] == "片段"
    assert parse("reasoning.delta", text="思考")["type"] == "reasoning"
    assert parse("tool.started", name="read", arguments='{"path":"a"}')["input"] == {"path": "a"}
    assert parse("tool.started", arguments="incomplete")["input"] == {"raw": "incomplete"}
    assert parse("tool.completed", content="denied", is_error=True)["text"] == "Error: denied"
    usage = parse("usage", prompt_tokens=10, completion_tokens=3, cached_tokens=4)
    assert usage["tokens"] == 13
    assert usage["usage"] == {"input": 6, "output": 3, "cache_read": 4}
    assert parse("turn.completed", exit_code=0, total_tokens=13)["type"] == "other"
    assert parse("turn.completed", exit_code=1, stop_reason="error")["type"] == "error"
    assert parse("turn.completed", exit_code=0, snapshot_error="disk full")["type"] == "error"
    assert parse("run.failed", message="failed")["terminal"]


@pytest.fixture
def fake_cli(runtime, tmp_path, monkeypatch):
    from src.domains.runtime.agents import executor
    from src.domains.runtime.engine import os_adapter
    script = tmp_path / "fake_atomcode.py"
    script.write_text('''import json, os, sys
from pathlib import Path
args = sys.argv[1:]
prompt = Path(args[args.index('--prompt-file') + 1]).read_text(encoding='utf-8')
assert prompt == '中文\\nlong prompt\\n'
mode = os.environ.get('ATOMCODE_TEST_MODE', 'ok')
sid = args[args.index('--resume') + 1] if '--resume' in args else 'native-session'
def emit(kind, **fields):
    print(json.dumps({'type': kind, **fields}), flush=True)
emit('run.started', schema_version=1, session_id=sid)
if mode in ('empty', 'empty_with_tool'):
    if mode == 'empty_with_tool': emit('tool.started', name='read', arguments='{}')
    emit('turn.completed', exit_code=0, stop_reason='end_turn')
    sys.exit(0)
if mode == 'hang':
    import time
    time.sleep(30)
if mode == 'resume_error':
    emit('run.failed', message='session not found')
    sys.exit(1)
if mode == 'no_usage':
    # 进程被提前终止：没有每轮 usage 事件，只有 turn.completed 的整轮汇总
    emit('message.delta', text='你')
    emit('message.delta', text='好\\n世界')
    emit('turn.completed', exit_code=0, stop_reason='end_turn',
         prompt_tokens=100, completion_tokens=20, cached_tokens=40)
    sys.exit(0)
emit('message.delta', text='你')
emit('message.delta', text='好\\n世界')
emit('usage', prompt_tokens=10, completion_tokens=3, cached_tokens=4)
if mode != 'truncated':
    emit('turn.completed', exit_code=1 if mode == 'failed' else 0, stop_reason=mode)
''', encoding="utf-8")
    build_args = runtime.build_args
    paths = []
    def invocation(prompt, session_id=""):
        paths.append(Path(prompt))
        return [str(script)] + build_args(prompt, session_id)
    monkeypatch.setattr(runtime, "build_args", invocation)
    monkeypatch.setattr(executor, "_atomcode_test_paths", paths, raising=False)
    monkeypatch.setitem(executor._BACKENDS, "atomcode", runtime)
    monkeypatch.setattr(os_adapter, "find_cli", lambda name: sys.executable)
    monkeypatch.setattr(executor, "_assemble_env", lambda *a, **k: None)
    import src.kernel.config
    monkeypatch.setattr(src.kernel.config, "GRAPE_AGENT_SANDBOX", False)
    return executor


@pytest.mark.parametrize("mode", ["ok", "failed", "truncated", "resume_error"])
def test_executor(fake_cli, tmp_path, mode):
    events = list(fake_cli.stream_agent(
        "中文\nlong prompt\n", str(tmp_path), session_id="resume-this",
        backend_name="atomcode", env_extra={"ATOMCODE_TEST_MODE": mode},
    ))
    done = [data for kind, data in events if kind == "done"]
    assert len(done) == 1
    assert done[0]["oc_session_id"] == "resume-this"
    assert done[0]["exit_code"] == (0 if mode == "ok" else 1)
    assert len([kind for kind, _ in events if kind == "step_start"]) == 1  # no fresh retry
    if mode != "resume_error":
        assert done[0]["text"] == "你好\n世界"
        assert [data for kind, data in events if kind == "usage"] == [
            {"input": 6, "output": 3, "cache_read": 4}]
    assert fake_cli._atomcode_test_paths
    assert all(not path.exists() for path in fake_cli._atomcode_test_paths)


def test_spawn_failure_cleans_prompt(fake_cli, tmp_path, monkeypatch):
    def fail(*args, **kwargs):
        raise OSError("cannot spawn")
    monkeypatch.setattr(fake_cli.subprocess, "Popen", fail)
    with pytest.raises(OSError):
        list(fake_cli.stream_agent("prompt", str(tmp_path), backend_name="atomcode"))
    assert all(not path.exists() for path in fake_cli._atomcode_test_paths)


@pytest.mark.parametrize("session_id", ["", "original-session"])
@pytest.mark.parametrize("mode", ["empty", "empty_with_tool"])
def test_empty_completion_fails_without_new_session(fake_cli, tmp_path, session_id, mode):
    events = list(fake_cli.stream_agent(
        "中文\nlong prompt\n", str(tmp_path), session_id=session_id,
        backend_name="atomcode", env_extra={"ATOMCODE_TEST_MODE": mode},
    ))
    done = [data for kind, data in events if kind == "done"]
    assert len(done) == 1 and done[0]["exit_code"] == 1
    assert done[0]["oc_session_id"] == (session_id or "native-session")
    assert "session preserved" in done[0]["errors"][-1]
    assert len(fake_cli._atomcode_test_paths) == 1  # no second invocation
    assert all(not path.exists() for path in fake_cli._atomcode_test_paths)


def test_cancel_terminates_owned_cli_and_cleans_prompt(fake_cli, tmp_path):
    from src.domains.runtime.engine.agent import _kill_stale_engine
    stream = fake_cli.stream_agent(
        "中文\nlong prompt\n", str(tmp_path), backend_name="atomcode",
        env_extra={"ATOMCODE_TEST_MODE": "hang"},
    )
    try:
        assert next(stream)[0] == "step_start"
        procs = fake_cli.active_task_processes("atomcode", str(tmp_path))
        assert len(procs) == 1
        _kill_stale_engine({"cwd": str(tmp_path)}, "atomcode")
        assert procs[0].poll() is not None
        done = next(data for kind, data in stream if kind == "done")
        assert done["exit_code"] != 0
    finally:
        stream.close()
    assert not fake_cli.active_task_processes("atomcode", str(tmp_path))
    assert all(not path.exists() for path in fake_cli._atomcode_test_paths)


@pytest.mark.parametrize("returncode", [0, 1])
def test_windows_cancel_uses_owned_pid_and_checks_taskkill(monkeypatch, tmp_path, returncode):
    from types import SimpleNamespace
    from unittest.mock import Mock
    from src.domains.runtime.agents import executor
    from src.domains.runtime.engine import os_adapter
    from src.domains.runtime.engine.agent import _kill_stale_engine
    monkeypatch.setattr(executor, "_active_procs", set())
    monkeypatch.setattr(os_adapter, "_IS_WINDOWS", True)
    commands = []
    def run(command, **kwargs):
        commands.append(command)
        return SimpleNamespace(returncode=returncode)
    monkeypatch.setattr(subprocess, "run", run)
    target = Mock(pid=123, returncode=None)
    target.poll.side_effect = lambda: target.returncode
    target.wait.side_effect = lambda **kwargs: setattr(target, "returncode", -1)
    others = [Mock(pid=124), Mock(pid=125)]
    for other in others:
        other.poll.return_value = None
    executor._track_proc(target, "atomcode", str(tmp_path))
    executor._track_proc(others[0], "atomcode", str(tmp_path / "other"))
    executor._track_proc(others[1], "opencode", str(tmp_path))
    assert os_adapter.find_process_by_cwd("atomcode", str(tmp_path)) == [123]
    if returncode:
        with pytest.raises(RuntimeError, match="failed to terminate process tree"):
            _kill_stale_engine({"cwd": str(tmp_path)}, "atomcode")
        assert target in executor._active_procs
    else:
        _kill_stale_engine({"cwd": str(tmp_path)}, "atomcode")
        target.wait.assert_called_once()
        assert target not in executor._active_procs
    assert commands == [["taskkill", "/T", "/F", "/PID", "123"]]
    assert all(other in executor._active_procs for other in others)


def test_timeout_cleans_prompt(fake_cli, tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_SILENT_EXTEND_CAP", "0.05")
    events = list(fake_cli.stream_agent(
        "中文\nlong prompt\n", str(tmp_path), backend_name="atomcode", max_exec_seconds=0.05,
        env_extra={"ATOMCODE_TEST_MODE": "hang", "AGENT_SILENT_WINDOW": "0.05"},
    ))
    done = next(data for kind, data in events if kind == "done")
    assert done["exit_code"] != 0
    assert done["errors"]
    assert all(not path.exists() for path in fake_cli._atomcode_test_paths)


def test_sandbox_mounts_only_prompt_readonly(fake_cli, tmp_path, monkeypatch):
    import src.kernel.config
    from src.domains.runtime.agents import sandbox
    monkeypatch.setattr(src.kernel.config, "GRAPE_AGENT_SANDBOX", True)
    monkeypatch.setattr(sandbox, "build_bwrap_cmd", lambda *a, **k: ["fake-bwrap", "--"])
    popen = fake_cli.subprocess.Popen
    seen = []
    def spawn(command, **kwargs):
        assert command[:2] == ["fake-bwrap", "--ro-bind"]
        assert command[2] == command[3]
        assert Path(command[2]).read_text() == "中文\nlong prompt\n"
        assert command[4] == "--"
        seen.append(command[2])
        return popen(command[5:], **kwargs)
    monkeypatch.setattr(fake_cli.subprocess, "Popen", spawn)
    events = list(fake_cli.stream_agent("中文\nlong prompt\n", str(tmp_path), backend_name="atomcode"))
    assert next(data for kind, data in events if kind == "done")["exit_code"] == 0
    assert len(seen) == 1 and not Path(seen[0]).exists()


def test_registration_skills_and_usage(monkeypatch, tmp_path):
    from src.domains.runtime.agents.executor import discover_backends, get_backend
    from src.domains.runtime.engine.agent import _backend_for_engine
    from src.domains.runtime.engine.descriptor import get_registry
    from src.domains.runtime.engine.os_adapter import engine_skill_dirs
    from src.domains.runtime.engine.engine_select import engine_role_tags
    from src.domains.runtime.engine.usage_collect import UsageCollector
    from src.domains.runtime.engine import os_adapter
    monkeypatch.setattr(os_adapter, "find_cli", lambda name: "/bin/atomcode" if name == "atomcode" else "")
    discovered = next(e for e in discover_backends() if e["name"] == "atomcode")
    assert discovered["installed"]
    assert discovered["pip_package"] == "@atomgit.com/atomcode"
    assert isinstance(get_backend("atomcode"), AtomCodeRuntime)
    assert _backend_for_engine("atomcode-worker-1") == "atomcode"
    probe_cmds = get_registry().probe_commands()  # [B5] 表已消，经 registry 派生
    # [R3 口径] 可选开关不在 probe.command（老 CLI 收到未知 flag 会 exit=2 误报
    # healthy 引擎为 fail）——基础命令保持最小，开关经 args_hook 按 --help 能力
    # 追加；此处断言声明位齐备 + 装配路径生效（help 支持时 cmd 含开关）。
    from src.domains.runtime.engine.descriptor import get_registry as _rg
    _ap = _rg().get("atomcode").get("probe", {})
    assert _ap["args_hook"] == "atomcode_help_flags"
    assert "--ephemeral" in _ap["optional_flags"] and "--no-tools" in _ap["optional_flags"]
    from src.domains.runtime.engine.probe_dispatch import _append_optional_flags_by_help
    _cmd = _append_optional_flags_by_help(["atomcode", "-p", "Reply OK"], _ap["optional_flags"])
    assert _cmd == ["atomcode", "-p", "Reply OK", "--ephemeral", "--no-tools"]
    # Issue #658 修订（2026-11-03 用户裁定）：引擎角色不按类型默认映射，
    # 仅显式声明 role_tags 时才硬过滤——atomcode 默认不限角色。
    assert engine_role_tags("atomcode") is None
    assert engine_role_tags("atomcode", '["coding"]') == ["coding"]
    monkeypatch.setenv("ATOMCODE_HOME", str(tmp_path))
    assert engine_skill_dirs("atomcode") == [str(tmp_path / "skills")]
    collector = UsageCollector("atomcode")
    collector.feed("usage", {"input": 6, "output": 3, "cache_read": 4})
    assert collector.finish()[0]["output"] == 3


# Issue #1884：安装脚本已整体删除 --engine / install_engine()，原先按脚本切块执行
# install_engine 的参数化用例（4 条）随之删除——它们断言的正是被移除的能力。
# 「脚本不再安装引擎」的契约由 tests/runtime/test_worker_install_ux.py 看护。


def test_server_side_registration_is_complete():
    """服务端也要认得 atomcode：类型派生 + 探活命令表缺一不可。

    前端筛选标签由后端 avail_types 派生（settings._engine_type_of /
    _device_engine_types），服务端探活走 observability.engine_probe。
    任一处漏登记 → 引擎已注册却无法按类型筛选 / 探活恒为 skip。
    """
    from src.domains.mgmt.routes.settings import _device_engine_types, _engine_type_of
    from src.domains.runtime.engine.descriptor import get_registry
    from src.domains.insight.observability.engine_probe import _probe_engine

    assert _engine_type_of({"name": "atomcode-worker-1"}) == "atomcode"
    assert _engine_type_of({"name": "atomcode"}) == "atomcode"
    assert _device_engine_types({"runtimes": [{"name": "atomcode-w1"}]}) == ["atomcode"]

    probe_cmds = get_registry().probe_commands()  # [B5] 表已消，经 registry 派生
    assert "atomcode" in probe_cmds
    probe = probe_cmds["atomcode"]
    # [R3 口径] 基础命令最小化；可选开关经 args_hook 按 help 能力追加（见上侧
    # test_discovery 注释）——此处断言声明齐备即可，装配路径同上被覆盖。
    _ap = get_registry().get("atomcode").get("probe", {})
    assert probe == ["atomcode", "-p", "Reply OK"]
    assert _ap["args_hook"] == "atomcode_help_flags"
    assert {"--ephemeral", "--no-tools"} <= set(_ap["optional_flags"])
    # 有命令模板就不能是 skip（否则探活恒 skip，引擎永远显示未探活）
    assert _probe_engine("atomcode", "")["status"] != "skip"


def test_kill_stale_engine_safe_swallows_failure(monkeypatch):
    """_kill_stale_engine_safe：杀引擎失败只记日志，不向上抛。

    Windows taskkill 失败会抛 RuntimeError；该异常若逸出 worker 主循环的
    取消/超时路径会击穿 worker 进程——杀不掉引擎不该让 worker 整体退出。
    """
    from src.domains.runtime.engine import agent as ra

    def boom(task, backend="opencode"):
        raise RuntimeError("failed to terminate process tree pid=1")

    monkeypatch.setattr(ra, "_kill_stale_engine", boom)
    ra._kill_stale_engine_safe({"cwd": "/tmp/x"}, "atomcode")  # 不抛异常即通过


def test_safe_terminate_swallows_failure(monkeypatch):
    """_safe_terminate：终止失败只记日志，不向上抛。

    Windows 下 taskkill /T /F 失败会抛 RuntimeError；该异常若从 worker 主循环的
    取消/超时路径逸出，会击穿 worker 进程——杀不掉引擎不该让 worker 整体退出。
    """
    from src.domains.runtime.agents import executor

    monkeypatch.setattr(executor, "_terminate_proc",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("taskkill failed")))
    assert executor._safe_terminate(object(), "label") is False

    monkeypatch.setattr(executor, "_terminate_proc", lambda *a, **k: None)
    assert executor._safe_terminate(object(), "label") is True


def test_credential_revoked_path_uses_safe_kill_wrapper():
    """凭据吊销路径与终态中断路径必须走容错包装，不能让杀进程异常顶掉原异常。

    _terminate_proc 在 Windows 下 taskkill 失败会抛 RuntimeError；若不被吞掉，
    该异常会顶掉 CredentialRevokedError，调用方依赖的"上抛让 worker 退出"语义丢失。
    _abort_task_terminal 同理：异常逸出会跳过 res.dispose()/_cleanup_task_workdir()
    并顶掉返回的 _error 契约。
    """
    import ast
    from pathlib import Path

    src = Path(__file__).parents[2] / "src" / "domains" / "runtime" / "engine" / "agent.py"
    tree = ast.parse(src.read_text(encoding="utf-8"))

    def called_names(node):
        out = set()
        for sub in ast.walk(node):
            if isinstance(sub, ast.Call):
                fn = sub.func
                if isinstance(fn, ast.Name):
                    out.add(fn.id)
                elif isinstance(fn, ast.Attribute):
                    out.add(fn.attr)
        return out

    handlers = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ExceptHandler) and node.type is not None:
            t = node.type
            name = t.id if isinstance(t, ast.Name) else getattr(t, "attr", "")
            if name == "CredentialRevokedError":
                handlers.append(node)
    assert handlers, "未找到 CredentialRevokedError 处理块"
    for h in handlers:
        names = called_names(h)
        # 杀引擎的处理器必须走容错包装；纯重抛的处理器不含杀引擎调用，无需约束
        if "_kill_stale_engine" in names:
            assert "_kill_stale_engine_safe" in names, \
                "凭据吊销路径未使用 _kill_stale_engine_safe，杀进程异常会顶掉原异常"

    aborts = [n for n in ast.walk(tree)
              if isinstance(n, ast.FunctionDef) and n.name == "_abort_task_terminal"]
    assert aborts, "未找到 _abort_task_terminal"
    abort_names = called_names(aborts[0])
    assert "_kill_stale_engine" not in abort_names, \
        "_abort_task_terminal 不应裸调 _kill_stale_engine，异常会跳过 res.dispose/_cleanup_task_workdir"
    assert "_kill_stale_engine_safe" in abort_names, \
        "_abort_task_terminal 必须使用 _kill_stale_engine_safe，杀进程异常不能顶掉 _error 契约"


def test_acp_backends_use_safe_terminate():
    """ACP 后端（hermes/hwcloud/aishell 等全部 *_acp.py）必须用容错终止。

    裸调 _terminate_proc 时，Windows 下 taskkill 失败抛出的 RuntimeError 会逸出：
    except subprocess.TimeoutExpired 分支内被吞掉超时原因、finally 内覆盖 try 块
    原始异常并跳过后续资源释放。自动遍历 agents/*_acp.py，防新增 ACP 后端再漏。
    """
    import ast
    from pathlib import Path

    root = Path(__file__).parents[2] / "src" / "domains" / "runtime" / "agents"
    acp_files = sorted(root.glob("*_acp.py"))
    assert acp_files, "no *_acp.py files found under src/domains/runtime/agents"
    for path in acp_files:
        name = path.name
        tree = ast.parse(path.read_text(encoding="utf-8"))
        calls = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                fn = node.func
                fname = fn.id if isinstance(fn, ast.Name) else getattr(fn, "attr", "")
                if fname == "_terminate_proc":
                    calls.append(node.lineno)
        assert not calls, f"{name} 仍在裸调 _terminate_proc（行号 {calls}），应改用 _safe_terminate"


def test_shared_backend_inference_covers_atomcode():
    """服务端取消/回收路径用共享推导，必须认得 atomcode 实例名。

    这两处原先内联复制了只含 hermes/codearts 的判断，atomcode 任务落到
    "opencode" 默认值 → pgrep 匹配不到 → 引擎进程不被终止（僵尸进程继续烧 token）。
    """
    from src.domains.runtime.engine.agent import _backend_for_engine

    assert _backend_for_engine("atomcode-worker-1") == "atomcode"
    assert _backend_for_engine("atomcode") == "atomcode"
    assert _backend_for_engine("hermes-w1") == "hermes"
    assert _backend_for_engine("codearts-w1") == "codearts"
    assert _backend_for_engine("unknown-w1") == "opencode"


def test_usage_fallback_uses_turn_totals_when_usage_event_missing():
    """进程被提前终止导致 usage 事件缺失时，用 turn.completed 汇总兜底。

    正常路径每轮一个 usage 事件；兜底只在**没有任何 usage 事件**时补一条，
    避免与每轮直报重复计费。
    """
    from src.domains.runtime.engine.atomcode import AtomCodeRuntime
    from src.domains.runtime.engine.usage_collect import UsageCollector

    rt = AtomCodeRuntime()
    parsed = rt.parse_output(json.dumps({
        "type": "turn.completed", "exit_code": 0, "stop_reason": "end_turn",
        "prompt_tokens": 100, "completion_tokens": 20, "cached_tokens": 40,
    }))
    assert parsed["usage_total"] == {"input": 60, "output": 20, "cache_read": 40}

    collector = UsageCollector("atomcode")
    collector.feed("usage", parsed["usage_total"])
    usage = collector.finish()[0]
    assert usage["input"] == 60 and usage["output"] == 20 and usage["cache_read"] == 40


def test_executor_emits_usage_fallback_only_without_usage_events(fake_cli, tmp_path):
    """有 usage 事件时不补兜底；没有时补一条 turn.completed 汇总。"""
    # fake CLI 的 "ok" 模式会发 usage 事件 → 只应有那一条 usage
    events = list(fake_cli.stream_agent(
        "中文\nlong prompt\n", str(tmp_path), backend_name="atomcode",
        env_extra={"ATOMCODE_TEST_MODE": "ok"},
    ))
    usages = [data for kind, data in events if kind == "usage"]
    assert usages == [{"input": 6, "output": 3, "cache_read": 4}]


def test_executor_emits_usage_fallback_when_usage_event_absent(fake_cli, tmp_path):
    """无 usage 事件但有 turn.completed 汇总时，补发一条兜底 usage。"""
    events = list(fake_cli.stream_agent(
        "中文\nlong prompt\n", str(tmp_path), backend_name="atomcode",
        env_extra={"ATOMCODE_TEST_MODE": "no_usage"},
    ))
    usages = [data for kind, data in events if kind == "usage"]
    assert len(usages) == 1
    assert usages[0] == {"input": 60, "output": 20, "cache_read": 40}


def test_abort_task_terminal_survives_kill_failure(monkeypatch):
    """终态中断路径杀引擎失败时，仍须释放资源并保留 _error/_started 契约。

    Windows 下 taskkill /T /F 失败会抛 RuntimeError（executor._terminate_proc 新语义）；
    _abort_task_terminal 裸调 _kill_stale_engine 时该异常逸出 → res.dispose() 与
    _cleanup_task_workdir() 被跳过，且 {"_error": …, "_started": …} 返回契约被顶掉
    （PR #1102 复审 Required / PR #1957）。AST 契约断言只看调用形态，本用例补运行时行为。
    """
    from src.domains.runtime.engine import agent as ra

    seen = {"kill": 0, "dispose": 0, "cleanup": 0}

    class _Res:
        def dispose(self):
            seen["dispose"] += 1

    def _boom(task, backend="opencode"):
        seen["kill"] += 1
        raise RuntimeError("failed to terminate process tree pid=1")

    monkeypatch.setattr(ra, "_kill_stale_engine", _boom)
    monkeypatch.setattr(ra, "_cleanup_task_workdir",
                        lambda task, env=None: seen.__setitem__("cleanup", seen["cleanup"] + 1))

    ret = ra._abort_task_terminal({"id": "t_probe"}, "atomcode", {}, _Res(), True, "probe")

    assert seen == {"kill": 1, "dispose": 1, "cleanup": 1}
    assert ret == {"_error": "task terminated by server (aborted probe)", "_started": True}
