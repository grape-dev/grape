"""Agent 执行器：流式子进程调度 (NDJSON)，不绑定具体后端。"""
import os, subprocess, json, shutil, time, signal
from typing import Generator, AsyncGenerator
from src.config import log
from .base import AgentBackend
from .opencode import OpencodeBackend
from .codearts import CodeArtsBackend


_BACKENDS = {
    "opencode": OpencodeBackend(),
    "codearts": CodeArtsBackend(),
}


def register_backend(name: str, backend: AgentBackend):
    """注册第三方 Agent 后端（插件接口，~20 行代码即可接入新引擎）"""
    _BACKENDS[name] = backend
    log.info(f"Backend registered: {name} -> {backend.__class__.__name__}")


def get_backend(name: str) -> AgentBackend:
    return _BACKENDS.get(name, _BACKENDS["opencode"])


def discover_backends() -> list[dict]:
    """动态发现本地已安装的 Agent 引擎"""
    known = {
        "opencode": {"cli": "opencode", "pip": "opencode"},
        "codearts": {"cli": "codearts", "pip": "codearts-cli"},
        "hermes": {"cli": "hermes", "pip": "hermes"},
        "cursor": {"cli": "cursor", "pip": "cursor"},
    }
    results = []
    for name, info in known.items():
        cli_path = shutil.which(info["cli"])
        results.append({
            "name": name,
            "installed": cli_path is not None,
            "cli_path": cli_path or "",
            "pip_package": info["pip"],
        })
    return results


def _redact(text, tokens):
    """将文本中的敏感 token 替换为 [REDACTED]（用于日志/事件脱敏）"""
    if not text or not tokens:
        return text
    s = str(text)
    for t in tokens:
        if t:
            s = s.replace(t, "[REDACTED]")
    return s


def _terminate_proc(proc, label: str = "", grace_seconds: float = 10.0):
    """优雅终止 agent 进程树。

    流程：SIGTERM 整个进程组 → 等待 ≤ grace_seconds → 超时 SIGKILL 兜底 → proc.wait() 确认回收。
    agent 子进程由 start_new_session=True 放入独立进程组，killpg 可连带清理其 fork 出的
    node/git/shell 等孙进程，避免残留。Windows 无进程组概念时 fallback terminate/kill。
    """
    if proc.poll() is not None:
        return
    pgid = None
    if hasattr(os, "getpgid"):
        try:
            pgid = os.getpgid(proc.pid)
        except Exception:
            pgid = None
    try:
        if pgid is not None:
            os.killpg(pgid, signal.SIGTERM)
        else:
            proc.terminate()
        try:
            proc.wait(timeout=grace_seconds)
            log.info(f"{label} terminated gracefully (SIGTERM)")
            return
        except subprocess.TimeoutExpired:
            pass
    except Exception:
        try:
            proc.terminate()
        except Exception:
            pass
    # 兜底：强制杀死整个进程组
    try:
        if pgid is not None:
            os.killpg(pgid, signal.SIGKILL)
        else:
            proc.kill()
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass
    try:
        proc.wait(timeout=5)
    except Exception:
        pass
    log.warning(f"{label} force killed (SIGKILL) after grace {grace_seconds}s")


def stream_agent(prompt: str, cwd: str, session_id: str = "",
                 event_type: str = "", backend_name: str = "opencode",
                 account: str = "", user_id: int = 0,
                 env_extra: dict | None = None,
                 bot_token: str = "", user_token: str = "",
                 agent_name: str = "",
                 agent_label: str = "",
                 max_exec_seconds: int = 600) -> Generator:
    """流式执行 Agent，yield 事件。agent_label 用于日志区分（如 [Agent:issue|repo:owner/repo]）"""
    label = f"{agent_label} " if agent_label else ""
    backend = get_backend(backend_name)
    env = backend.get_env(account=account, user_id=user_id)
    env["DEVOPS_EVENT_TYPE"] = event_type
    # GitCode token 通过子进程环境变量注入（不写入 prompt，避免 token 进会话历史/日志）
    if bot_token:
        env["BOT_GITCODE_TOKEN"] = bot_token
    if user_token:
        env["USER_GITCODE_TOKEN"] = user_token
    # Agent 显示名（skill 中 [$AGENT_NAME] 使用，如 "Issue Agent" / "QA Agent"）
    if agent_name:
        env["AGENT_NAME"] = agent_name
    if env_extra:
        env.update(env_extra)

    # 注入用户凭据
    if user_id > 0:
        try:
            from src.user.db import get_credentials
            for c in get_credentials(user_id):
                for k, v in c.get("config", {}).items():
                    env[k.upper()] = str(v)
        except Exception as e:
            log.warning(f"Failed to inject user credentials: {e}")

    # 需要脱敏的 token 列表（用于日志/事件输出）
    _tokens = [t for t in (bot_token, user_token) if t]

    args = backend.build_args(prompt, session_id)
    agent_cmd = shutil.which(backend.binary) or shutil.which(backend.binary + ".cmd")
    if not agent_cmd:
        log.error(f"{label}{backend.name} CLI not found in PATH")
        yield ("done", {
            "exit_code": -1, "text": "", "oc_session_id": session_id,
            "errors": [f"{backend.name} not found"], "stderr": ""
        })
        return

    log.info(f"{label}{backend.name} args: {' '.join(args)[:2000]}...")
    log.info(f"{label}{backend.name} cwd: {cwd}")

    stdin_pipe = subprocess.PIPE if backend.prompt_via_stdin else None
    _popen_kwargs = {}
    if hasattr(os, "getpgid"):  # POSIX：独立进程组，便于整组优雅终止
        _popen_kwargs["start_new_session"] = True
    proc = subprocess.Popen(
        [agent_cmd] + args,
        stdin=stdin_pipe, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        cwd=cwd, env=env, bufsize=0, **_popen_kwargs
    )
    if backend.prompt_via_stdin:
        proc.stdin.write(prompt.encode("utf-8"))
        proc.stdin.close()

    collected_text = []
    new_session_id = session_id
    error_events = []
    step_count = 0
    _last_output = time.time()

    import threading
    _line_queue = []
    _line_queue_lock = threading.Lock()
    _reader_done = False

    def _stdout_reader():
        nonlocal _reader_done
        try:
            for raw_line in iter(proc.stdout.readline, b""):
                with _line_queue_lock:
                    _line_queue.append(raw_line)
        except Exception:
            pass
        finally:
            _reader_done = True

    _reader_thread = threading.Thread(target=_stdout_reader, daemon=True)
    _reader_thread.start()

    while True:
        with _line_queue_lock:
            if _line_queue:
                raw_lines = _line_queue[:]
                _line_queue.clear()
            else:
                raw_lines = []
        if raw_lines:
            _last_output = time.time()
            for raw_line in raw_lines:
                line = raw_line.decode("utf-8", errors="replace").strip()
                if not line:
                    continue
                parsed = backend.parse_output(line)
                if parsed is None:
                    yield ("raw", _redact(line[:200], _tokens))
                    continue
                et = parsed["type"]
                if et == "step_start":
                    step_count += 1
                    if parsed.get("sessionID"):
                        new_session_id = parsed["sessionID"]
                    yield ("step_start", f"Step {step_count} started (session: {new_session_id})")
                elif et == "text":
                    collected_text.append(parsed["text"])
                    yield ("text", _redact(parsed["text"][:150], _tokens).replace("\n", " "))
                elif et == "reasoning":
                    yield ("reasoning", _redact(parsed["text"][:200], _tokens))
                elif et == "tool_use":
                    yield ("tool_use", _redact(f"Tool: {parsed['tool']} {json.dumps(parsed.get('input', {}))[:200]}", _tokens))
                elif et == "step_finish":
                    yield ("step_finish", f"Step {step_count} done, tokens={parsed['tokens']}")
                elif et == "error":
                    error_events.append(_redact(parsed["message"], _tokens))
                    yield ("error", _redact(parsed["message"], _tokens))
                else:
                    yield ("other", et)
        elif _reader_done:
            break
        else:
            elapsed_silent = time.time() - _last_output
            if elapsed_silent > max_exec_seconds:
                log.warning(f"{label}{backend.name} no output for {max_exec_seconds}s, terminating")
                _terminate_proc(proc, f"{label}{backend.name}")
                yield ("done", {
                    "exit_code": -9, "text": _redact("\n".join(collected_text), _tokens),
                    "oc_session_id": new_session_id,
                    "errors": error_events + [f"no output for {max_exec_seconds}s, killed"],
                    "stderr": "",
                })
                return
            time.sleep(0.1)

    try:
        proc.wait(timeout=120)
    except subprocess.TimeoutExpired:
        log.warning(f"{label}{backend.name} did not exit after stdout EOF, terminating")
        _terminate_proc(proc, f"{label}{backend.name}")
        _reader_thread.join(timeout=5)
        yield ("done", {
            "exit_code": -1, "text": "", "oc_session_id": new_session_id,
            "errors": [f"{backend.name} timed out"], "stderr": ""
        })
        return

    _reader_thread.join(timeout=5)

    stdout_remainder = ""
    try:
        stdout_remainder = proc.stdout.read().decode("utf-8", errors="replace")
    except:
        pass

    if stdout_remainder:
        for line in stdout_remainder.strip().split("\n"):
            line = line.strip()
            if not line:
                continue
            parsed = backend.parse_output(line)
            if parsed and parsed["type"] == "text":
                collected_text.append(parsed["text"])

    full_text = "\n".join(collected_text)
    stderr_output = ""
    try:
        stderr_output = proc.stderr.read().decode("utf-8", errors="replace")[:500]
    except:
        pass

    yield ("done", {
        "exit_code": proc.returncode, "text": _redact(full_text, _tokens),
        "oc_session_id": new_session_id,
        "errors": error_events, "stderr": _redact(stderr_output, _tokens),
        "backend": backend.name,
    })


def stream_opencode(prompt, cwd, oc_session_id, event_type="", bot_token="", agent_name=""):
    """向后兼容包装器，默认使用 opencode 后端。"""
    yield from stream_agent(prompt, cwd, oc_session_id, event_type, backend_name="opencode", bot_token=bot_token, agent_name=agent_name)


async def consume_stream_async(prompt: str, cwd: str, session_id: str = "",
                               event_type: str = "", backend_name: str = "opencode",
                               account: str = "", user_id: int = 0,
                               env_extra: dict | None = None,
                               bot_token: str = "", user_token: str = "",
                               agent_name: str = "", agent_label: str = "") -> AsyncGenerator:
    """async 桥接：在线程池中消费 stream_agent 生成器，逐事件桥接回 event loop。

    避免 agent 的同步生成器阻塞整个 event loop（否则 WS ping 超时掉线、其他消息/API 全卡住）。
    stream_agent 内部的"无输出 10 分钟优雅终止"保护在后台线程中照常生效。
    """
    import asyncio
    q = asyncio.Queue()
    loop = asyncio.get_running_loop()

    def _run():
        try:
            for evt_type, data in stream_agent(
                prompt, cwd, session_id=session_id, event_type=event_type,
                backend_name=backend_name, account=account, user_id=user_id,
                env_extra=env_extra, bot_token=bot_token, user_token=user_token,
                agent_name=agent_name, agent_label=agent_label,
            ):
                loop.call_soon_threadsafe(q.put_nowait, (evt_type, data))
        except Exception as e:
            loop.call_soon_threadsafe(q.put_nowait, ("error", str(e)))
        finally:
            loop.call_soon_threadsafe(q.put_nowait, (None, None))

    loop.run_in_executor(None, _run)
    while True:
        evt_type, data = await q.get()
        if evt_type is None:
            break
        yield evt_type, data
