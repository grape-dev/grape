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
    """流式执行 Agent，yield 事件。agent_label 用于日志区分（如 [Agent:issue|repo:owner/repo]）。

    **会话失效自动重建**：当 opencode 会话在服务端已不存在（如被清理/误删）时，
    opencode 会以 exit=1 + stderr "Session not found" 快速失败。
    本函数检测到该情况自动降级为「不带 session」重跑一次，用新会话继续，
    避免 agent 因旧 session 失效而整体失败（无回复）。
    """
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

    agent_cmd = shutil.which(backend.binary) or shutil.which(backend.binary + ".cmd")
    if not agent_cmd:
        log.error(f"{label}{backend.name} CLI not found in PATH")
        yield ("done", {
            "exit_code": -1, "text": "", "oc_session_id": session_id,
            "errors": [f"{backend.name} not found"], "stderr": ""
        })
        return

    # 单次进程执行：返回 (full_text, new_session_id, exit_code, stderr, error_events)
    def _run_once(use_session_id: str):
        args = backend.build_args(prompt, use_session_id)
        log.info(f"{label}{backend.name} args: {' '.join(args)[:2000]}...")
        log.info(f"{label}{backend.name} cwd: {cwd}")

        stdin_pipe = subprocess.PIPE if backend.prompt_via_stdin else None
        _popen_kwargs = {}
        if hasattr(os, "getpgid"):  # POSIX：独立进程组，便于整组优雅终止
            _popen_kwargs["start_new_session"] = True

        # 纯管道启动（与 main 一致；script 包装为临时对照，已按分析移除）
        proc = subprocess.Popen(
            [agent_cmd] + args,
            stdin=stdin_pipe, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            cwd=cwd, env=env, bufsize=0, **_popen_kwargs
        )
        if backend.prompt_via_stdin:
            proc.stdin.write(prompt.encode("utf-8"))
            proc.stdin.close()

        collected_text = []
        new_session_id = use_session_id
        error_events = []
        step_count = 0
        _last_output = time.time()
        _reader_total = 0

        import threading
        _line_queue = []
        _line_queue_lock = threading.Lock()
        _reader_done = False

        def _stdout_reader():
            nonlocal _reader_done, _reader_total
            try:
                for raw_line in iter(proc.stdout.readline, b""):
                    _reader_total += 1
                    with _line_queue_lock:
                        _line_queue.append(raw_line)
            except Exception as e:
                log.warning(f"{label}{backend.name} reader exception: {e}")
            finally:
                log.info(f"{label}{backend.name} reader EOF, total_lines={_reader_total}")
                _reader_done = True

        _reader_thread = threading.Thread(target=_stdout_reader, daemon=True)
        _reader_thread.start()

        _raw_count = 0
        _parsed_count = 0
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
                        _raw_count += 1
                        # opencode 会话失效错误经 stdout 以 ANSI 原始行输出（非 NDJSON），
                        # 计入 error_events 使下方 session_missing 检测能命中（重跑无 session）
                        if "session not found" in line.lower():
                            error_events.append(_redact(line, _tokens))
                        yield ("raw", _redact(line, _tokens))
                        continue
                    _parsed_count += 1
                    et = parsed["type"]
                    if et == "step_start":
                        step_count += 1
                        if parsed.get("sessionID"):
                            new_session_id = parsed["sessionID"]
                        yield ("step_start", f"Step {step_count} started (session: {new_session_id})")
                    elif et == "text":
                        collected_text.append(parsed["text"])
                        yield ("text", _redact(parsed["text"], _tokens))
                    elif et == "reasoning":
                        yield ("reasoning", _redact(parsed["text"], _tokens))
                    elif et == "tool_use":
                        # M1 结构化：yield dict（tool/input），供 evaluate 与档案记录使用
                        yield ("tool_use", {
                            "tool": parsed.get("tool", ""),
                            "input": _redact(json.dumps(parsed.get("input", {}), ensure_ascii=False, default=str), _tokens),
                        })
                    elif et == "step_finish":
                        yield ("step_finish", f"Step {step_count} done, tokens={parsed['tokens']}")
                    elif et == "error":
                        error_events.append(_redact(parsed["message"], _tokens))
                        yield ("error", _redact(parsed["message"], _tokens))
                    else:
                        yield ("other", et)
            elif _reader_done:
                log.info(f"{label}{backend.name} loop break on reader EOF: raw={_raw_count} parsed={_parsed_count} total_lines={_reader_total}")
                break
            else:
                elapsed_silent = time.time() - _last_output
                if elapsed_silent > max_exec_seconds:
                    log.warning(f"{label}{backend.name} no output for {max_exec_seconds}s, terminating")
                    # 先 SIGTERM 给 agent 收尾窗口（_terminate_proc 内部 grace_seconds=10 等待优雅退出）
                    _terminate_proc(proc, f"{label}{backend.name}")
                    # 收尾窗口内尝试读取 SIGTERM 后 opencode 冲刷的最终输出（避免把已产出的收尾当卡死）
                    _grace_tail = ""
                    try:
                        proc.stdout.settimeout(3)
                        while True:
                            try:
                                line = proc.stdout.readline()
                            except Exception:
                                break
                            if not line:
                                break
                            _grace_tail += line.decode("utf-8", errors="replace")
                            _last_output = time.time()
                    except Exception:
                        pass
                    for _gl in _grace_tail.strip().split("\n"):
                        _gl = _gl.strip()
                        if not _gl:
                            continue
                        _parsed = backend.parse_output(_gl)
                        if _parsed:
                            _parsed_count += 1
                            if _parsed["type"] == "text":
                                collected_text.append(_parsed["text"])
                            elif _parsed["type"] == "tool_use":
                                pass
                    # 若收尾窗口内收到输出，视为 agent 已收尾（exit 以进程实际状态为准，不再强制 -9）
                    _killed = "-9" if _grace_tail.strip() == "" else "grace"
                    log.info(f"{label}{backend.name} hang fallback: tail_chars={len(_grace_tail)} outcome={_killed}")
                    yield ("done", {
                        "exit_code": -9 if _grace_tail.strip() == "" else (proc.returncode if proc.returncode is not None else -1),
                        "text": _redact("\n".join(collected_text), _tokens),
                        "oc_session_id": new_session_id,
                        "errors": error_events + ([f"no output for {max_exec_seconds}s, killed"] if _grace_tail.strip() == "" else []),
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
        except Exception:
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
        except Exception as e:
            log.warning(f"{label}{backend.name} stderr read err: {e}")

        log.info(f"{label}{backend.name} done: exit={proc.returncode} text_len={len(full_text)} stderr={stderr_output[:120]!r}")

        yield ("done", {
            "exit_code": proc.returncode, "text": _redact(full_text, _tokens),
            "oc_session_id": new_session_id,
            "errors": error_events, "stderr": _redact(stderr_output, _tokens),
            "backend": backend.name,
        })

    # 执行一次；若因会话失效失败（stderr 含 Session not found），降级重跑一次（不带 session）
    # 注意：run 内部产生的 done 不立即转发，先判定是否需重跑；避免调用方收到多个 done
    run_gen = _run_once(session_id)
    done = None
    _forwarded_done = False
    for evt_type, data in run_gen:
        if evt_type == "done":
            done = data
        else:
            yield (evt_type, data)
    if done is None:
        return

    session_missing = (
        done.get("exit_code", 0) != 0
        and session_id
        and ("session not found" in (done.get("stderr") or "").lower()
             or any("session not found" in (e or "").lower() for e in (done.get("errors") or [])))
    )
    if session_missing:
        log.warning(f"{label}{backend.name} session {session_id[:20]}... not found on server, retrying without session")
        run_gen2 = _run_once("")
        done2 = None
        for evt_type, data in run_gen2:
            if evt_type == "done":
                done2 = data
            else:
                yield (evt_type, data)
        if done2 is not None:
            done = done2
    yield ("done", done)


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
