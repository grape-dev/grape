"""Hermes ACP 后端：通过 JSON-RPC over stdin/stdout 与 hermes 通信。

学习 multica 的 hermes ACP 集成模式，替代旧的 `hermes chat -q -v` CLI 文本解析。

核心优势：
1. 结构化 JSON-RPC 通信，不需要正则解析文本输出
2. hermes ACP 模式内部线程模型更简单，不触发 CLI 模式的 futex 死锁
3. state.db 按 conversation 隔离（per-issue 独立文件），不同 issue 间不争锁
4. 工具调用/消息/思考都是结构化事件，无需 guess parse

ACP 协议流程：
  1. 启动 `hermes acp` 子进程
  2. 发 initialize → 握手
  3. 发 session/new（或 session/resume 续接）→ 获取 sessionId
  4. 发 session/prompt → 等 PromptResponse（stopReason）
  5. 期间收 session/update 通知（agent_message_chunk / tool_call / tool_call_update / agent_thought_chunk）
  6. 处理 session/request_permission（自动批准 allow_once）
  7. stdin EOF → hermes 退出

通信格式：每行一个 JSON-RPC 消息（NDJSON over stdin/stdout）
"""
import os
import json
import re
import time
import threading
import subprocess
import shutil
import signal
from typing import Generator
from src.config import log


def _global_timeout_for(max_exec_seconds: float) -> float:
    """hermes ACP 内循环全局墙钟上限（#282 对齐修复）。

    取 RUNTIME_TASK_TIMEOUT 环境变量（与 src/runtime/agent.py 任务级看门狗
    `_env_float("RUNTIME_TASK_TIMEOUT", 3600)` 同一配置源、同默认值），
    解析失败回退 3600；下限保护 max_exec_seconds——全局上限始终 ≥ 静默超时窗口，
    防止配置异常把全局上限压到静默超时之下。
    旧逻辑 `max_exec_seconds * 2`（=1200s 写死倍率）会在长任务 20 分钟处
    硬切仍在正常输出的任务，已废弃。
    """
    try:
        _v = float(os.environ.get("RUNTIME_TASK_TIMEOUT", "3600"))
    except Exception:
        _v = 3600.0
    return max(_v, float(max_exec_seconds))


# 「疑似提前结束」标记：模型输出 text 后静默超过文本静默上限、且累计输出里没有收尾记录
# （actions/conclusion）时打上。语义 = 任务被放行（exit 0）但工作可能没做完；
# 平台侧靠这个标记做可见性（src/runtime/service.py::complete_task 会把它落到
# agent_tasks.errors + 事件 result 前缀），不降 failed、不触发重跑。
PREMATURE_END_MARKER = "[PREMATURE_END_TURN]"


def _text_incomplete_timeout_for() -> float:
    """text 静默且无收尾记录时的等待上限（默认 300s，与 tool 静默窗口对齐）。

    语义：模型输出 text 后 60s 无事件只说明「暂时没输出」，不代表干完了——可能正在
    生成一个大 tool call（补丁/大文件），这期间 hermes 不会发任何 ACP 事件。
    因此：
      · ≤60s 静默：按原逻辑先查完成证据；
      · 60s ~ 本上限：无证据也继续等（不再判完成、不再掐会话）；
      · 超过本上限仍无证据：按「疑似提前结束」收尾（exit 0 + [PREMATURE_END_TURN] 标记）。
    可用 RUNTIME_TEXT_SILENT_TIMEOUT 覆盖（设成 ≤60 即退回旧的 60s 判完成行为）；解析失败回退 300。
    """
    try:
        _v = float(os.environ.get("RUNTIME_TEXT_SILENT_TIMEOUT", "300"))
    except Exception:
        _v = 300.0
    return max(_v, 0.0)


class HermesACPBackend:
    """Hermes ACP 后端 — 不走 build_args/parse_output，直接 stream_execute yield 事件。"""
    name = "hermes"
    binary = "hermes"
    prompt_via_stdin = False  # ACP 模式不用 stdin 传 prompt，用 JSON-RPC

    def get_env(self, account: str = "", user_id: int = 0) -> dict:
        env = os.environ.copy()
        from src.config import SKILLS_DIR
        env["DEVOPS_SKILLS_DIR"] = SKILLS_DIR
        env["DEVOPS_EVENT_TYPE"] = ""
        env["NO_COLOR"] = "1"
        env["TERM"] = "dumb"
        # ACP 模式自动批准工具执行（等效旧 --yolo）
        env["HERMES_YOLO_MODE"] = "1"
        return env

    def stream_execute(
        self,
        prompt: str,
        cwd: str,
        session_id: str = "",
        env: dict | None = None,
        max_exec_seconds: int = 600,
        agent_label: str = "",
    ) -> Generator:
        """ACP 协议执行，yield (event_type, data) 事件。

        事件类型与 stream_agent 兼容：
        - ("step_start", str)
        - ("text", str)
        - ("reasoning", str)
        - ("tool_use", {"tool": str, "input": dict})
        - ("tool_finish", str)
        - ("tool_result", str)
        - ("step_finish", str)
        - ("done", {"exit_code": int, "text": str, "oc_session_id": str, "errors": list, "stderr": str})
        """
        label = f"{agent_label} " if agent_label else ""
        agent_cmd = shutil.which(self.binary) or shutil.which(self.binary + ".cmd")
        if not agent_cmd:
            log.error(f"{label}hermes ACP CLI not found in PATH")
            yield ("done", {
                "exit_code": -1, "text": "", "oc_session_id": session_id,
                "errors": ["hermes not found"], "stderr": ""
            })
            return

        # ── 准备 HERMES_HOME overlay（学习 multica 的 per-task overlay 机制）──
        # state.db 按 cwd 隔离（per-conversation 独立文件），不同会话间不争锁。
        # 使用基于 cwd 的稳定路径（非随机临时目录），使 state.db 在任务结束后持久化，
        # 下次同一 cwd 执行时 session/resume 能找到历史会话数据（chat-ctx-fix §方向一）。
        import hashlib
        _cwd_hash = hashlib.md5((cwd or ".").encode()).hexdigest()[:12]
        # 使用当前用户家目录下的缓存目录，避免 /tmp 多用户权限冲突（issue #238）
        _hermes_homes_root = os.path.join(os.path.expanduser("~"), ".cache", "hermes-acp-homes")
        os.makedirs(_hermes_homes_root, exist_ok=True)
        iso_home = os.path.join(_hermes_homes_root, _cwd_hash)
        os.makedirs(iso_home, exist_ok=True)
        src_home = os.path.expanduser(os.environ.get("HERMES_HOME", "~/.hermes"))

        # symlink 镜像共享 home 的内容（学习 multica mirrorSharedHermesHome）
        # 只覆写 state.db（task-local）和 config.yaml（tool_progress=new）
        _build_overlay_home(iso_home, src_home)

        proc_env = dict(env) if env else os.environ.copy()
        proc_env["HERMES_HOME"] = iso_home
        proc_env["HERMES_YOLO_MODE"] = "1"

        log.info(f"{label}hermes ACP starting: HERMES_HOME={iso_home} cwd={cwd}")

        # ── 启动 hermes acp 子进程 ──
        _popen_kwargs = {}
        if hasattr(os, "getpgid"):
            _popen_kwargs["start_new_session"] = True

        # 组件2：文件系统隔离（GRAPE_AGENT_SANDBOX 开 + bwrap 可用时套白名单锁）
        _acp_cmd = [agent_cmd, "acp"]
        try:
            from src.agent.sandbox import build_bwrap_cmd
            _wrap = build_bwrap_cmd(cwd, backend_name="hermes")
            if _wrap:
                _acp_cmd = _wrap + _acp_cmd
                log.info(f"{label}hermes ACP sandboxed via bwrap (cwd={cwd})")
        except Exception as _bw_e:
            log.warning(f"{label}hermes ACP bwrap wrap failed, running unsandboxed: {_bw_e}")

        proc = subprocess.Popen(
            _acp_cmd,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            cwd=cwd, env=proc_env, bufsize=0, **_popen_kwargs
        )

        # 注册到全局进程表（供 shutdown 时终止）—— 延迟导入避免循环引用
        try:
            from src.agent.executor import _track_proc, _untrack_proc, _terminate_proc
        except ImportError:
            _track_proc = _untrack_proc = lambda p: None
            _terminate_proc = lambda p, l: None

        # TaskResourceStack: unified resource lifecycle (effect/dispose pattern)
        from src.runtime.resource_stack import TaskResourceStack
        _res = TaskResourceStack(task_id="", cwd=cwd)
        _res.track_overlay(iso_home)
        _res.track_proc(proc)

        collected_text = []
        new_session_id = session_id
        error_events = []

        # chat 任务完成检测参数（优化：降低 idle 超时，加快响应）：
        # _CHAT_STABLE_SECONDS: 首次有 text 后至少等 N 秒（给 hermes 时间输出完整回复）
        # _CHAT_IDLE_SECONDS: 最后一条 text 后连续 N 秒无新 text → 认为输出完
        # 优化前：15+10=25s 总延迟；优化后：3+5=8s 总延迟
        _CHAT_STABLE_SECONDS = 3
        _CHAT_IDLE_SECONDS = 5
        _chat_first_text_time = 0.0
        _chat_last_text_time = 0.0
        # 判断是否 chat 任务：env 里有 DEVOPS_EVENT_TYPE=chat 或 AGENT_NAME 含 chat
        _is_chat_task = False
        if env:
            _evt = env.get("DEVOPS_EVENT_TYPE", "") or ""
            _ag = env.get("AGENT_NAME", "") or ""
            _is_chat_task = (_evt == "chat") or ("chat" in _ag.lower())

        # ── ACP JSON-RPC 客户端 ──
        client = _ACPSimpleClient(
            stdin=proc.stdin,
            stdout=proc.stdout,
            label=label,
            on_message=lambda msg_type, content: None,  # 占位，下面重新设
        )

        # 事件队列：reader 线程 → 主循环
        event_queue: list[tuple[str, object]] = []
        queue_lock = threading.Lock()
        reader_done = threading.Event()
        prompt_done = threading.Event()
        prompt_result: dict = {}
        prompt_error: str = ""

        # 文本缓冲区：ACP 每次只发几个 token 的 chunk，需要缓冲到换行或一定长度再 flush
        _text_buf = []
        _think_buf = []
        _BUF_MAX = 200  # 字符上限，避免过长不 flush

        def _flush_text():
            if _text_buf:
                line = "".join(_text_buf)
                _text_buf.clear()
                if line.strip():
                    collected_text.append(line)
                    _enqueue("text", line)

        def _flush_thinking():
            if _think_buf:
                line = "".join(_think_buf)
                _think_buf.clear()
                if line.strip():
                    _enqueue("reasoning", line)

        def _on_message(msg_type: str, content):
            """ACP 通知回调 → 缓冲 chunk，换行或积累到 _BUF_MAX 再 flush"""
            if msg_type == "text":
                _text_buf.append(content)
                if "\n" in content or sum(len(s) for s in _text_buf) >= _BUF_MAX:
                    _flush_text()
                    # chat 任务不输出 JSON 记录块，用 text 输出稳定检测：
                    # 记录首次有 text 的时间，连续 _CHAT_IDLE_SECONDS 无新 text → 认为已完成
                    if _is_chat_task and collected_text and not prompt_done.is_set():
                        nonlocal _chat_first_text_time, _chat_last_text_time
                        if _chat_first_text_time == 0:
                            _chat_first_text_time = time.time()
                        _chat_last_text_time = time.time()
                        _chat_stable = (time.time() - _chat_first_text_time) >= _CHAT_STABLE_SECONDS
                        _chat_idle = (time.time() - _chat_last_text_time) >= _CHAT_IDLE_SECONDS
                        if _chat_stable and _chat_idle:
                            log.info(f"{label}hermes ACP chat task text stable for {_CHAT_IDLE_SECONDS}s, assuming done")
                            prompt_result["stopReason"] = "end_turn"
                            prompt_done.set()
            elif msg_type == "thinking":
                _think_buf.append(content)
                if "\n" in content or sum(len(s) for s in _think_buf) >= _BUF_MAX:
                    _flush_thinking()
            elif msg_type == "tool_use":
                _flush_text()   # 工具调用前 flush 残留文本
                _flush_thinking()
                _enqueue("tool_use", content)
            elif msg_type == "tool_finish":
                _flush_text()
                _flush_thinking()
                _enqueue("tool_finish", content)
            elif msg_type == "tool_result":
                _enqueue("tool_result", content)

        def _enqueue(et, data):
            with queue_lock:
                event_queue.append((et, data))

        client.on_message = _on_message
        client.on_prompt_done = lambda result: (
            prompt_result.update(result),
            prompt_done.set(),
        )
        client.on_prompt_error = lambda err: (
            error_events.append(err),
            prompt_done.set(),
        )

        # ── reader 线程：非阻塞读 stdout（readline 在 hermes acp 下会阻塞）──
        # hermes acp 一次性 flush 多行 JSON-RPC，Python readline 只读第一行就阻塞。
        # 用 select + 非阻塞 read 代替。
        import select as _select
        _fd = proc.stdout.fileno()
        # Windows 没有 fcntl
        import sys as _sys
        if _sys.platform != "win32":
            import fcntl as _fcntl
            _fl = _fcntl.fcntl(_fd, _fcntl.F_GETFL)
            _fcntl.fcntl(_fd, _fcntl.F_SETFL, _fl | os.O_NONBLOCK)

        _read_buf = b""

        def _reader():
            nonlocal _read_buf
            try:
                if _sys.platform == "win32":
                    # Windows: 直接用 Popen.stdout.readline() 阻塞读
                    # reader 在独立线程，阻塞不影响主线程
                    while True:
                        line = proc.stdout.readline()
                        if not line:
                            break
                        line_str = line.decode("utf-8", errors="replace").strip()
                        if not line_str:
                            continue
                        try:
                            client.handle_line(line_str)
                        except Exception as e:
                            log.warning(f"{label}hermes ACP handle_line error: {e}")
                else:
                    # Linux: select 轮询 + 非阻塞 read 批量读
                    while True:
                        readable, _, _ = _select.select([_fd], [], [], 0.5)
                        if not readable:
                            if proc.poll() is not None:
                                break
                            continue
                        try:
                            chunk = proc.stdout.read(4096)
                        except (BlockingIOError, OSError):
                            continue
                        if not chunk:
                            break
                        _read_buf += chunk
                        while b"\n" in _read_buf:
                            line, _read_buf = _read_buf.split(b"\n", 1)
                            line_str = line.decode("utf-8", errors="replace").strip()
                            if not line_str:
                                continue
                            try:
                                client.handle_line(line_str)
                            except Exception as e:
                                log.warning(f"{label}hermes ACP handle_line error: {e}")
            except Exception as e:
                log.warning(f"{label}hermes ACP reader exception: {e}")
            finally:
                reader_done.set()
                log.info(f"{label}hermes ACP reader EOF")

        reader_thread = threading.Thread(target=_reader, daemon=True)
        reader_thread.start()

        # stderr reader 线程：持续排空 stderr 管道，防止管道缓冲区满导致死锁
        # Windows 管道缓冲区约 4KB，hermes ACP 启动时输出大量 stderr 日志
        # 不排空会导致 hermes 进程阻塞在 stderr 写入上，stdout 不再输出 → 管道死锁
        _stderr_chunks = []
        def _stderr_reader():
            try:
                while True:
                    line = proc.stderr.readline()
                    if not line:
                        break
                    _stderr_chunks.append(line.decode("utf-8", errors="replace"))
            except Exception:
                pass
        stderr_thread = threading.Thread(target=_stderr_reader, daemon=True)
        stderr_thread.start()

        try:
            # ── 1. initialize 握手 ──
            init_result, init_err = client.request("initialize", {
                "protocolVersion": 1,
                "clientInfo": {"name": "grape-agent", "version": "0.1.0"},
                "clientCapabilities": {},
            }, timeout=100)
            if init_err:
                error_events.append(f"initialize failed: {init_err}")
                yield ("done", _done_result(-1, "", session_id, error_events, ""))
                return

            log.info(f"{label}hermes ACP initialized")

            # ── 2. session/new 或 session/resume ──
            yield ("step_start", "Step 1 started (session: ACP)")
            cwd_arg = cwd if cwd else "."
            if session_id:
                from src.runtime.context_compaction import check_state_db_size
                _skip_resume = check_state_db_size(iso_home, threshold_mb=50.0)
                if _skip_resume:
                    log.warning(f"{label}hermes ACP state.db too large, skipping resume to avoid context overflow")
                    session_id = ""
                    result, err = client.request("session/new", {
                        "cwd": cwd_arg, "mcpServers": [],
                    }, timeout=100)
                else:
                    result, err = client.request("session/resume", {
                        "cwd": cwd_arg, "sessionId": session_id, "mcpServers": [],
                    }, timeout=100)
                if err:
                    log.warning(f"{label}hermes ACP resume {session_id[:12]}... failed: {err}, creating new")
                    result, err = client.request("session/new", {
                        "cwd": cwd_arg, "mcpServers": [],
                    }, timeout=100)
                    if err:
                        error_events.append(f"session/new failed: {err}")
                        yield ("done", _done_result(-1, "", session_id, error_events, ""))
                        return
                    new_session_id = _extract_session_id(result) or ""
                else:
                    new_session_id = _extract_session_id(result) or session_id
            else:
                result, err = client.request("session/new", {
                    "cwd": cwd_arg, "mcpServers": [],
                }, timeout=100)
                if err:
                    error_events.append(f"session/new failed: {err}")
                    yield ("done", _done_result(-1, "", session_id, error_events, ""))
                    return
                new_session_id = _extract_session_id(result) or ""
            log.info(f"{label}hermes ACP session: {new_session_id[:12]}...")

            # 清理残留的 _pending entry
            with client._lock:
                for _pid in list(client._pending.keys()):
                    client._pending.pop(_pid, None)

            # ── 3. session/prompt（fire-and-forget）──
            with client._lock:
                _prompt_id = client._next_id
                client._next_id += 1
            _prompt_evt = threading.Event()
            with client._lock:
                client._pending[_prompt_id] = _prompt_evt

            def _send_prompt(_pid=_prompt_id, _sp=prompt):
                msg = {"jsonrpc": "2.0", "id": _pid, "method": "session/prompt",
                       "params": {"sessionId": new_session_id,
                                  "prompt": [{"type": "text", "text": _sp}]}}
                client._prompt_req_id = _pid
                try:
                    client.write_line((json.dumps(msg) + "\n").encode("utf-8"))
                except Exception as e:
                    error_events.append(f"send prompt: {e}")
                    prompt_done.set()

            prompt_thread = threading.Thread(target=_send_prompt, daemon=True)
            prompt_thread.start()

            # ── 4. 内循环：从队列消费事件 + 等 prompt 完成 ──
            _last_activity = time.time()
            _global_start = time.time()
            _SILENT_TIMEOUT = 300   # 非 text 事件（tool/reasoning）后 5 分钟无事件 → 超时终止
            _TEXT_SILENT_TIMEOUT = 60  # text 输出后静默 60s → 先做一次「完成证据」检查
            _TEXT_INCOMPLETE_TIMEOUT = _text_incomplete_timeout_for()  # 无证据时最多再等到这里 → 按疑似提前结束收尾
            _GLOBAL_TIMEOUT = _global_timeout_for(max_exec_seconds)
            _last_event_type = ""   # 记录最后一次事件类型
            _rec_check_n = -1       # 已完成收尾记录检查的 collected_text 长度（避免静默期每 50ms 重复解析）
            _rec_ok = False         # 是否已检测到收尾记录（actions + conclusion）
            _silent_wait_logged = False

            while True:
                with queue_lock:
                    if event_queue:
                        events = event_queue[:]
                        event_queue.clear()
                    else:
                        events = []

                if events:
                    _last_activity = time.time()
                    for et, data in events:
                        _last_event_type = et
                        yield (et, data)

                if prompt_done.is_set():
                    break

                # hermes 进程退出但没发 PromptResponse → 兼容性兜底
                if reader_done.is_set() and not prompt_done.is_set():
                    if collected_text:
                        log.info(f"{label}hermes ACP reader EOF with {len(collected_text)} text chunks, assuming done")
                        prompt_result["stopReason"] = "end_turn"
                        break
                    error_events.append("hermes process exited before prompt completed")
                    break

                # text 后静默：不再直接判完成——静默只决定「还要不要等」，
                #   「算不算完成」必须由证据决定（见 _has_completion_record / PromptResponse）。
                #   ① 累计输出里已有收尾记录（actions + conclusion）→ 判完成（response 丢失）；
                #   ② 无证据 → 继续等到 _TEXT_INCOMPLETE_TIMEOUT（默认 300s，与 tool 静默对齐），
                #      到点按「疑似提前结束」收尾（exit 0 + 标记，不冒充干净完成）。
                #   非 text 事件（tool/reasoning）后的静默维持原语义：超时终止（exit -1）。
                elapsed_silent = time.time() - _last_activity
                if _last_event_type == "text" and elapsed_silent > _TEXT_SILENT_TIMEOUT:
                    if len(collected_text) != _rec_check_n:
                        _rec_check_n = len(collected_text)
                        _rec_ok = _rec_ok or _has_completion_record(collected_text)
                    if _rec_ok:
                        log.info(f"{label}hermes ACP silent {elapsed_silent:.0f}s after text, "
                                 f"completion record detected, assuming done")
                        prompt_result["stopReason"] = "end_turn"
                        break
                    if elapsed_silent > _TEXT_INCOMPLETE_TIMEOUT:
                        log.warning(f"{label}hermes ACP silent {elapsed_silent:.0f}s after text without "
                                    f"completion record (actions/conclusion), premature end "
                                    f"(no forced kill; task will report incomplete)")
                        prompt_result["stopReason"] = "premature_end"
                        break
                    if not _silent_wait_logged:
                        _silent_wait_logged = True
                        log.info(f"{label}hermes ACP silent {elapsed_silent:.0f}s after text, "
                                 f"no completion record yet, waiting up to "
                                 f"{_TEXT_INCOMPLETE_TIMEOUT:.0f}s (model may be generating a large tool call)")
                elif elapsed_silent > _SILENT_TIMEOUT:
                    log.warning(f"{label}hermes ACP silent {elapsed_silent:.0f}s, timeout")
                    prompt_result["stopReason"] = "timeout"
                    break

                # 全局超时（#282：对齐外层 RUNTIME_TASK_TIMEOUT，默认 3600s）
                if time.time() - _global_start > _GLOBAL_TIMEOUT:
                    _gt_src = os.environ.get("RUNTIME_TASK_TIMEOUT")
                    _gt_src_txt = f"env RUNTIME_TASK_TIMEOUT={_gt_src}" if _gt_src else "default 3600s"
                    log.warning(f"{label}hermes ACP global timeout ({_GLOBAL_TIMEOUT:.0f}s, {_gt_src_txt}) exceeded")
                    prompt_result["stopReason"] = "timeout"
                    break

                time.sleep(0.05)

            # ── 5. 收尾 ──
            _flush_text()
            _flush_thinking()

            stop_reason = prompt_result.get("stopReason", "")
            log.info(f"{label}hermes ACP done: stopReason={stop_reason}")

            yield ("step_finish", f"Step 1 done (stopReason={stop_reason})")

            # 消费残留事件
            with queue_lock:
                if event_queue:
                    for et, data in event_queue[:]:
                        yield (et, data)
                    event_queue.clear()

            # 关闭 stdin 让 hermes 退出
            try:
                proc.stdin.close()
            except Exception:
                pass

            # 等待进程退出（grace 5s）
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                _terminate_proc(proc, f"{label}hermes ACP")

            reader_thread.join(timeout=3)

            # 消费残留事件
            with queue_lock:
                if event_queue:
                    for et, data in event_queue[:]:
                        yield (et, data)
                    event_queue.clear()

            stderr_output = ""
            try:
                stderr_output = "".join(_stderr_chunks)[:500]
            except Exception:
                pass

            # exit_code 判定
            if stop_reason == "end_turn" and not error_events:
                exit_code = 0
            elif stop_reason == "premature_end":
                # 无收尾记录即静默收尾：按「疑似提前结束」放行（exit 0 + 标记）。
                # 不判 -1：避免把「模型长停顿被截断」当成引擎故障触发整任务重跑
                #（重跑需幂等约束，见 docs/weekly-07/hermes-acp-premature-endturn-fix-design.md）。
                error_events.append(
                    f"{PREMATURE_END_MARKER} 模型输出后静默超过文本静默上限仍无收尾记录"
                    "（actions/conclusion），疑似提前结束；工作可能未做完")
                exit_code = 0
            elif stop_reason == "timeout":
                exit_code = -1
            elif stop_reason == "error":
                exit_code = -1
            elif error_events:
                exit_code = proc.returncode if proc.returncode is not None else -1
            else:
                exit_code = 0

            full_text = "\n".join(collected_text)
            # JSON 鲁棒拼接：修复 flush 边界 \n 导致的字段名断裂、缺闭合 } 等
            if _detect_structured_output(full_text):
                _fixed = _robust_join_json(collected_text)
                if _fixed and _detect_structured_output(_fixed):
                    full_text = _fixed
            elif _detect_findings_block(full_text):
                _fixed = _robust_join_findings(collected_text)
                if _fixed and _detect_findings_block(_fixed):
                    full_text = _fixed
            # 疑似提前结束：把标记落到 result_text（events.result 只取前 200 字符，
            # 平台侧可见性补强见 P1；此处保证工单/任务详情能看到「没走到收尾」）。
            if stop_reason == "premature_end":
                full_text = (full_text.rstrip() +
                             f"\n\n{PREMATURE_END_MARKER} 引擎在模型输出后静默 "
                             f"{int(_TEXT_INCOMPLETE_TIMEOUT)}s 未检测到收尾记录"
                             "（actions/conclusion），按疑似提前结束收尾；"
                             "该任务的工作可能未完成，续做需带幂等约束（不要重做已完成部分）。")

            yield ("done", {
                "exit_code": exit_code,
                "text": full_text,
                "oc_session_id": new_session_id,
                "errors": error_events,
                "stderr": stderr_output[:500],
                "backend": "hermes",
            })

        except Exception as e:
            log.error(f"{label}hermes ACP exception: {e}", exc_info=True)
            error_events.append(str(e))
            yield ("done", {
                "exit_code": -1, "text": "\n".join(collected_text),
                "oc_session_id": new_session_id,
                "errors": error_events, "stderr": "",
            })
        finally:
            # Unified cleanup via TaskResourceStack (replaces scattered _untrack_proc + _cleanup_overlay)
            try:
                proc.stdin.close()
            except Exception:
                pass
            if proc.poll() is None:
                _terminate_proc(proc, f"{label}hermes ACP")
            # 不再 dispose overlay 目录：iso_home 现在是稳定路径（基于 cwd hash），
            # state.db 需要持久化供下次 session/resume 使用（chat-ctx-fix §方向一）。
            # 只杀进程，不删目录。
            _res.dispose(kill_procs=False, keep_overlays=True)


# ══════════════════════════════════════════════════════════════
# ACP JSON-RPC 客户端（学习 multica hermesClient）
# ══════════════════════════════════════════════════════════════

class _ACPSimpleClient:
    """极简 ACP JSON-RPC 客户端：request/response + notification 处理。"""

    def __init__(self, stdin, stdout, label="", on_message=None, on_prompt_done=None, on_prompt_error=None):
        self.stdin = stdin
        self.stdout = stdout
        self.label = label
        self.on_message = on_message or (lambda t, c: None)
        self.on_prompt_done = on_prompt_done or (lambda r: None)
        self.on_prompt_error = on_prompt_error or (lambda e: None)
        # 注意：不做 self._on_prompt_done = self.on_prompt_done 别名快照——
        # 本文件的调用方（HermesACPBackend.stream_execute）是构造后用
        # `client.on_prompt_done = ...` 赋值的，快照会永久停留在构造时的 no-op，
        # 导致 ACP PromptResponse / session/update 的 stopReason 完全被丢弃
        #（生产长期只能靠 60s text 静默兜底收尾；hwcloud_acp.py 已先行修掉同一 bug）。
        # 调用点直接读公开属性。

        self._write_lock = threading.Lock()
        self._next_id = 1
        self._pending: dict[int, threading.Event] = {}
        self._pending_results: dict[int, any] = {}
        self._pending_errors: dict[int, str] = {}
        self._prompt_req_id: int = 0  # session/prompt 的 req_id（只有它才触发 prompt_done）
        self._lock = threading.Lock()

    def write_line(self, data: bytes):
        with self._write_lock:
            self.stdin.write(data)
            self.stdin.flush()

    def request(self, method: str, params: dict, timeout: float = 60) -> tuple[dict | None, str | None]:
        """发送 JSON-RPC 请求，等待响应。返回 (result, error)。"""
        with self._lock:
            req_id = self._next_id
            self._next_id += 1
            evt = threading.Event()
            self._pending[req_id] = evt

        msg = {"jsonrpc": "2.0", "id": req_id, "method": method, "params": params}
        data = (json.dumps(msg) + "\n").encode("utf-8")
        try:
            self.write_line(data)
        except Exception as e:
            with self._lock:
                del self._pending[req_id]
            return None, f"write {method}: {e}"

        if evt.wait(timeout=timeout):
            with self._lock:
                result = self._pending_results.pop(req_id, None)
                error = self._pending_errors.pop(req_id, None)
                self._pending.pop(req_id, None)
            return result, error
        else:
            with self._lock:
                self._pending.pop(req_id, None)
            return None, f"{method} timeout after {timeout}s"

    def handle_line(self, line: str):
        """处理从 hermes stdout 收到的一行 JSON-RPC 消息。"""
        try:
            raw = json.loads(line)
        except json.JSONDecodeError:
            return

        # 1. Response（有 id，有 result/error）→ 匹配 pending request
        if "id" in raw and ("result" in raw or "error" in raw):
            self._handle_response(raw)
            return

        # 2. Agent → client request（有 id，有 method）→ 回复
        if "id" in raw and "method" in raw:
            self._handle_agent_request(raw)
            return

        # 3. Notification（无 id，有 method）→ session/update 等
        if "method" in raw and "id" not in raw:
            self._handle_notification(raw)
            return

    def _handle_response(self, raw: dict):
        req_id = raw["id"]
        with self._lock:
            evt = self._pending.get(req_id)

        if evt is None:
            # 未匹配的 response，记录但不处理
            return

        is_prompt_response = (req_id == self._prompt_req_id)

        if "error" in raw:
            err = raw["error"]
            err_msg = err.get("message", str(err))
            if err.get("data"):
                err_msg += f" (data={err['data']})"
            with self._lock:
                self._pending_errors[req_id] = err_msg
            # 只有 session/prompt 的 error 才触发 prompt_done
            if is_prompt_response:
                self.on_prompt_done({"stopReason": "error", "result": raw})
        else:
            result = raw.get("result")
            with self._lock:
                self._pending_results[req_id] = result
            # 只有 session/prompt 的 response 才触发 prompt_done
            if is_prompt_response:
                if isinstance(result, dict):
                    stop_reason = result.get("stopReason", "")
                    if stop_reason:
                        self.on_prompt_done({"stopReason": stop_reason, "result": result})
                    else:
                        log.info(f"{self.label}hermes ACP prompt response without stopReason, treating as done (id={req_id})")
                        self.on_prompt_done({"stopReason": "end_turn", "result": result})
                else:
                    self.on_prompt_done({"stopReason": "end_turn", "result": result or {}})

        evt.set()

    def _handle_agent_request(self, raw: dict):
        """处理 agent → client 的 JSON-RPC 请求（如 session/request_permission）。"""
        method = raw.get("method", "")
        req_id = raw["id"]

        if method == "session/request_permission":
            # 自动批准 allow_once（headless 模式，学习 multica selectACPPermissionOption）
            params = raw.get("params", {})
            options = params.get("options", [])
            option_id = ""
            for opt in options:
                kind = opt.get("kind", "")
                if kind == "allow_once":
                    option_id = opt.get("optionId", "")
                    break
                if kind == "allow_always" and opt.get("optionId") in ("allow_session", "approve_for_session"):
                    option_id = opt.get("optionId", "")
                    break
            if not option_id:
                # fallback: 第一个 allow 类
                for opt in options:
                    if opt.get("kind", "") in ("allow_once", "allow_always"):
                        option_id = opt.get("optionId", "")
                        break

            resp = {
                "jsonrpc": "2.0", "id": req_id,
                "result": {"outcome": {"outcome": "selected", "optionId": option_id}},
            }
        else:
            resp = {
                "jsonrpc": "2.0", "id": req_id,
                "error": {"code": -32601, "message": f"method not found: {method}"},
            }

        data = (json.dumps(resp) + "\n").encode("utf-8")
        try:
            self.write_line(data)
        except Exception as e:
            log.warning(f"{self.label}hermes ACP write response error: {e}")

    def _handle_notification(self, raw: dict):
        """处理 session/update 通知（agent 消息、工具调用等）。"""
        method = raw.get("method", "")
        if method not in ("session/update", "session/notification"):
            return

        params = raw.get("params", {})
        update = params.get("update", params)
        if not isinstance(update, dict):
            return

        # 提取 update type
        update_type = update.get("sessionUpdate", update.get("type", ""))
        update_type_norm = _normalize_update_type(update_type)
        if not update_type_norm:
            # 尝试 externally-tagged object
            if len(update) == 1:
                key = list(update.keys())[0]
                update_type_norm = _normalize_update_type(key)
                update = update[key] if isinstance(update.get(key), dict) else update
            if not update_type_norm:
                return

        if update_type_norm == "agent_message_chunk":
            text = update.get("content", {}).get("text", "")
            if text:
                self.on_message("text", text)
        elif update_type_norm == "agent_thought_chunk":
            text = update.get("content", {}).get("text", "")
            if text:
                self.on_message("thinking", text)
        elif update_type_norm == "tool_call":
            tool_call_id = update.get("toolCallId", "")
            name = update.get("name", "")
            title = update.get("title", "")
            raw_input = update.get("rawInput") or update.get("input") or update.get("parameters") or {}
            tool_name = _tool_name_from_title(title) or name
            if tool_name:
                self.on_message("tool_use", {"tool": tool_name, "input": raw_input})
        elif update_type_norm == "tool_call_update":
            status = update.get("status", "")
            title = update.get("title", update.get("name", ""))
            tool_name = _tool_name_from_title(title) or title
            if status in ("completed", "failed"):
                self.on_message("tool_finish", f"Tool {tool_name} {status}")
                # 提取 output
                output = update.get("rawOutput") or update.get("output")
                if output:
                    output_text = output if isinstance(output, str) else json.dumps(output, ensure_ascii=False)[:500]
                    self.on_message("tool_result", output_text)
        elif update_type_norm == "turn_end":
            # 提取 stopReason
            stop_reason = update.get("stopReason", "")
            self.on_prompt_done({"stopReason": stop_reason, "result": update})


# ══════════════════════════════════════════════════════════════
# 辅助函数
# ══════════════════════════════════════════════════════════════

_UPDATE_TYPE_MAP = {
    "agentmessagechunk": "agent_message_chunk",
    "agentthoughtchunk": "agent_thought_chunk",
    "toolcall": "tool_call",
    "toolcallupdate": "tool_call_update",
    "usageupdate": "usage_update",
    "turnend": "turn_end",
    "endturn": "turn_end",
}

def _normalize_update_type(t: str) -> str:
    key = t.lower().replace("_", "").replace("-", "")
    return _UPDATE_TYPE_MAP.get(key, "")

def _tool_name_from_title(title: str) -> str:
    if not title:
        return ""
    # hermes tool title 格式: "terminal" / "write_file" / "read_file" 等
    # 或 "💻 $ command" / "✅ Tool N" 等
    title = title.strip()
    if title.startswith("💻") or title.startswith("$ "):
        return "terminal"
    if title.startswith("✅"):
        return ""
    # 直接就是 tool name
    if title.replace("_", "").replace("-", "").isalnum():
        return title
    return title

def _extract_session_id(result) -> str:
    if not result or not isinstance(result, dict):
        return ""
    # 顶层 sessionId
    sid = result.get("sessionId") or result.get("session_id")
    if sid:
        return sid
    # hermes ACP 返回在 _meta.hermes.sessionProvenance.acpSessionId
    meta = result.get("_meta", {})
    if isinstance(meta, dict):
        hp = meta.get("hermes", {}).get("sessionProvenance", {})
        if isinstance(hp, dict):
            sid = hp.get("acpSessionId") or hp.get("currentHermesSessionId")
            if sid:
                return sid
    return ""


def _has_completion_record(chunks: list) -> bool:
    """累计 text 输出里是否已出现 agent 的收尾记录（actions + conclusion）。

    仅用于 text 静默后的「到底干完没干完」判定：
      · 能解析出 actions + conclusion 的对象才算完成证据（与 Grape agent 收尾契约一致）；
      · 不做子串匹配——模型在正文里引用 JSON 字段示例时会误判（_detect_structured_output 的短板）；
      · chunk 边界可能把 JSON 拆坏（字段名中间插 \\n / 键名尾部插 \\n），
        先按 _robust_join_json 修一遍再解析，解析后按键名 strip 后比对；
      · 只看末尾窗口（收尾记录总在最后），避免长文本全文扫描。
    """
    if not chunks:
        return False
    window = chunks[-40:]
    for candidate in ("\n".join(window), _robust_join_json(window)):
        if not candidate:
            continue
        m = re.search(r'\{\s*"actions', candidate)
        if not m:
            continue
        try:
            # strict=False：允许字符串里夹带 flush 边界引入的字面换行
            # （正常输出解析不用宽松模式，这里只服务"有没有收尾记录"的启发判定）
            obj, _end = json.JSONDecoder(strict=False).raw_decode(candidate[m.start():])
        except Exception:
            continue
        if isinstance(obj, dict):
            keys = {str(k).strip(): v for k, v in obj.items()}
            if keys.get("actions") and keys.get("conclusion"):
                return True
    return False


def _detect_structured_output(text: str) -> bool:
    """检测 agent 累积输出中是否包含结构化 JSON 记录块。

    Grape agent 任务完成时输出含 actions + conclusion + issue_summary
    字段的 JSON 块。有则任务真正完成；无则 LLM 提前 end_turn，需续步。

    ACP chunk 可能将 JSON 字段名拆分到不同 chunk（如 '"actions' + '": [...]'),
    flush 后 \n join 会在字段名中间插入换行，导致精确字符串匹配失败。
    因此先去除所有空白字符再做匹配，避免 chunk 边界干扰。
    """
    if not text:
        return False
    # 去除所有空白字符（空格、换行、制表符）后匹配
    compact = re.sub(r'\s+', '', text)
    return '"actions"' in compact and '"conclusion"' in compact and '"issue_summary"' in compact


def _detect_findings_block(text: str) -> bool:
    """检测 agent 输出中是否包含 autopilot findings JSON 块。

    与 _detect_structured_output 同理：去空白后按字段特征匹配，
    不依赖 HTML 注释标记完整性（<!-- --> 可能被 chunk 拆断）。
    """
    if not text:
        return False
    compact = re.sub(r'\s+', '', text)
    return '"summary"' in compact and '"findings"' in compact


# ── JSON 鲁棒拼接辅助函数（源头预防 flush 边界损坏）──

_KNOWN_JSON_FIELDS = ("actions", "conclusion", "artifacts", "next_step", "issue_summary")

_FINDINGS_JSON_FIELDS = ("summary", "findings", "id", "title", "severity",
                         "category", "description", "suggested_assignee", "suggested_skill")


def _robust_join_json(chunks: list) -> str:
    """拼接 collected_text，修复 flush 边界导致的 JSON 损坏。

    只对 JSON 块部分操作，不影响非 JSON 文本。
    """
    full = "\n".join(chunks)
    # 定位 JSON 块起点：找 {"actions" 模式
    m = re.search(r'\{\s*"actions"', full)
    if not m:
        return full
    start = m.start()
    json_part = full[start:]
    # 去掉尾部非 JSON 标记
    for marker in ('[PREMATURE_END_TURN]', '\n---', '\n\n['):
        idx = json_part.rfind(marker)
        if idx > 0:
            json_part = json_part[:idx]
    # 修复 1: 断裂字段名（\n 在字段名中间）
    for field in _KNOWN_JSON_FIELDS:
        for i in range(1, len(field)):
            broken = field[:i] + '\n' + field[i:]
            if broken in json_part:
                json_part = json_part.replace(broken, field)
    # 修复 2: 字符串外 \n 去除，字符串内裸 \n 转义
    json_part = _fix_json_newlines(json_part)
    # 修复 3: 补闭合 } 如果缺失
    json_part = _ensure_closed(json_part)
    return full[:start] + json_part


def _robust_join_findings(chunks: list) -> str:
    """拼接 collected_text，修复 autopilot findings JSON 的 flush 边界损坏。

    与 _robust_join_json 逻辑相同，适配 findings 格式：
    - 起点匹配 {"summary"（而非 {"actions"）
    - 字段名列表使用 _FINDINGS_JSON_FIELDS
    - 尾部标记清理包含 --> 和 --\n>（注释标记可能被 chunk 拆断）
    """
    full = "\n".join(chunks)
    m = re.search(r'\{\s*"summary"', full)
    if not m:
        return full
    start = m.start()
    json_part = full[start:]
    # 去掉尾部非 JSON 标记：HTML 注释结束符（可能被 chunk 拆断）+ 其他杂标记
    for marker in ('-->', '--\n>', '\n---', '\n\n['):
        idx = json_part.rfind(marker)
        if idx > 0:
            json_part = json_part[:idx]
    # 修复 1: 断裂字段名（\n 在字段名中间）
    for field in _FINDINGS_JSON_FIELDS:
        for i in range(1, len(field)):
            broken = field[:i] + '\n' + field[i:]
            if broken in json_part:
                json_part = json_part.replace(broken, field)
    # 修复 2: 字符串外 \n 去除，字符串内裸 \n 转义
    json_part = _fix_json_newlines(json_part)
    # 修复 3: 补闭合 } 如果缺失
    json_part = _ensure_closed(json_part)
    return full[:start] + json_part


def _fix_json_newlines(json_str: str) -> str:
    """去掉 JSON 中非字符串值内的换行符，字符串内的裸 \\n 转为 \\\\n。"""
    result = []
    in_string = False
    escape = False
    for ch in json_str:
        if escape:
            result.append(ch)
            escape = False
            continue
        if ch == '\\':
            result.append(ch)
            escape = True
            continue
        if ch == '"':
            in_string = not in_string
            result.append(ch)
            continue
        if ch == '\n':
            if in_string:
                result.append('\\n')
            # 字符串外: 跳过
            continue
        result.append(ch)
    return "".join(result)


def _ensure_closed(json_str: str) -> str:
    """如果 JSON 缺少闭合 }，按括号深度补上（忽略字符串内的 {{ }}）。"""
    s = json_str.rstrip()
    if s.endswith('}'):
        return s
    depth = 0
    in_string = False
    escape = False
    for ch in s:
        if escape:
            escape = False
            continue
        if ch == '\\':
            escape = True
            continue
        if ch == '"':
            in_string = not in_string
            continue
        if in_string:
            continue
        if ch == '{':
            depth += 1
        elif ch == '}':
            depth -= 1
    if depth > 0:
        s += '}' * depth
    return s


def _done_result(exit_code, text, session_id, errors, stderr) -> dict:
    return {
        "exit_code": exit_code, "text": text,
        "oc_session_id": session_id, "errors": errors, "stderr": stderr,
    }

def _build_overlay_home(iso_home: str, src_home: str):
    """构建 per-task HERMES_HOME overlay（学习 multica mirrorSharedHermesHome）。

    - symlink 镜像共享 home 的所有内容（skills/plugins/auth 等）
    - 覆写 config.yaml（tool_progress=new）
    - 覆写 .env（pin HERMES_HOME + 保留凭据）
    - state.db 保持 task-local（不 symlink，每个 task 独立）
    """
    try:
        # 镜像共享 home 的条目
        if os.path.isdir(src_home):
            for entry in os.listdir(src_home):
                dst = os.path.join(iso_home, entry)
                # 跳过覆写的条目
                if entry in ("state.db", "state.db-wal", "state.db-shm",
                             "config.yaml", ".env", "active_profile", "profiles"):
                    continue
                if os.path.exists(dst):
                    continue
                src = os.path.join(src_home, entry)
                try:
                    os.symlink(src, dst)
                except Exception:
                    pass

        # 覆写 config.yaml：从源复制 + 改 tool_progress=new
        src_cfg = os.path.join(src_home, "config.yaml")
        dst_cfg = os.path.join(iso_home, "config.yaml")
        if os.path.isfile(src_cfg):
            try:
                import re
                with open(src_cfg, "r", encoding="utf-8") as f:
                    cfg_text = f.read()
                cfg_text = re.sub(r'(tool_progress:\s*)\w+', r'\g<1>new', cfg_text)
                with open(dst_cfg, "w", encoding="utf-8") as f:
                    f.write(cfg_text)
            except Exception:
                shutil.copy2(src_cfg, dst_cfg)

        # 覆写 .env：保留源凭据 + pin HERMES_HOME
        src_env = os.path.join(src_home, ".env")
        dst_envf = os.path.join(iso_home, ".env")
        env_body = ""
        if os.path.isfile(src_env):
            try:
                with open(src_env, "r", encoding="utf-8") as f:
                    for line in f:
                        stripped = line.strip()
                        if stripped.startswith("HERMES_HOME=") or stripped.startswith("export HERMES_HOME="):
                            continue  # 删掉源 HERMES_HOME 行
                        env_body += line
            except Exception:
                pass
        # pin HERMES_HOME 到 overlay（单引号防变量展开）
        if env_body and not env_body.endswith("\n"):
            env_body += "\n"
        env_body += f"HERMES_HOME='{iso_home}'\n"
        try:
            with open(dst_envf, "w", encoding="utf-8") as f:
                f.write(env_body)
            os.chmod(dst_envf, 0o600)
        except Exception:
            pass

    except Exception as e:
        log.warning(f"hermes ACP overlay build error: {e}")

def _cleanup_overlay(iso_home: str):
    """清理 per-task HERMES_HOME（保留 state.db 供可能的续接，只删 symlink）。"""
    try:
        shutil.rmtree(iso_home, ignore_errors=True)
    except Exception:
        pass
