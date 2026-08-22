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
import time
import threading
import subprocess
import shutil
import signal
from typing import Generator
from src.config import log


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
        # 每个 task 独立的 HERMES_HOME，state.db 按 conversation 隔离
        import tempfile
        iso_home = tempfile.mkdtemp(prefix="hermes-acp-")
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

        proc = subprocess.Popen(
            [agent_cmd, "acp"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            cwd=cwd, env=proc_env, bufsize=0, **_popen_kwargs
        )

        # 注册到全局进程表（供 shutdown 时终止）—— 延迟导入避免循环引用
        try:
            from src.agent.executor import _track_proc, _untrack_proc, _terminate_proc
        except ImportError:
            _track_proc = _untrack_proc = lambda p: None
            _terminate_proc = lambda p, l: None

        collected_text = []
        new_session_id = session_id
        error_events = []

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
        import fcntl as _fcntl
        _fl = _fcntl.fcntl(_fd, _fcntl.F_GETFL)
        _fcntl.fcntl(_fd, _fcntl.F_SETFL, _fl | os.O_NONBLOCK)

        _read_buf = b""

        def _reader():
            nonlocal _read_buf
            try:
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

        try:
            # ── 1. initialize 握手 ──
            init_result, init_err = client.request("initialize", {
                "protocolVersion": 1,
                "clientInfo": {"name": "grape-agent", "version": "0.1.0"},
                "clientCapabilities": {},
            }, timeout=30)
            if init_err:
                error_events.append(f"initialize failed: {init_err}")
                yield ("done", _done_result(-1, "", session_id, error_events, ""))
                _untrack_proc(proc)
                _cleanup_overlay(iso_home)
                return

            log.info(f"{label}hermes ACP initialized")

            # ── 2. session/new 或 session/resume ──
            yield ("step_start", "Step 1 started (session: ACP)")
            cwd_arg = cwd if cwd else "."

            if session_id:
                # 尝试 resume
                result, err = client.request("session/resume", {
                    "cwd": cwd_arg,
                    "sessionId": session_id,
                    "mcpServers": [],
                }, timeout=30)
                if err:
                    log.warning(f"{label}hermes ACP resume {session_id[:12]}... failed: {err}, creating new")
                    result, err = client.request("session/new", {
                        "cwd": cwd_arg,
                        "mcpServers": [],
                    }, timeout=30)
                    if err:
                        error_events.append(f"session/new failed: {err}")
                        yield ("done", _done_result(-1, "", session_id, error_events, ""))
                        _untrack_proc(proc)
                        _cleanup_overlay(iso_home)
                        return
                    new_session_id = _extract_session_id(result) or ""
                else:
                    new_session_id = _extract_session_id(result) or session_id
            else:
                result, err = client.request("session/new", {
                    "cwd": cwd_arg,
                    "mcpServers": [],
                }, timeout=30)
                if err:
                    error_events.append(f"session/new failed: {err}")
                    yield ("done", _done_result(-1, "", session_id, error_events, ""))
                    _untrack_proc(proc)
                    _cleanup_overlay(iso_home)
                    return
                new_session_id = _extract_session_id(result) or ""

            log.info(f"{label}hermes ACP session: {new_session_id[:12]}...")

            # ── 3. session/prompt（fire-and-forget：不等 response，等 notification）──
            # hermes ACP 的 prompt 响应通过 JSON-RPC response 返回（带 stopReason），
            # 也可能通过 session/update notification 的 turn_end 返回。
            # 发完 prompt 后主循环等 prompt_done event。
            with client._lock:
                _prompt_id = client._next_id
                client._next_id += 1
            # 注册 pending 以便 _handle_response 能匹配
            _prompt_evt = threading.Event()
            with client._lock:
                client._pending[_prompt_id] = _prompt_evt

            def _send_prompt():
                msg = {"jsonrpc": "2.0", "id": _prompt_id, "method": "session/prompt",
                       "params": {"sessionId": new_session_id,
                                  "prompt": [{"type": "text", "text": prompt}]}}
                try:
                    client.write_line((json.dumps(msg) + "\n").encode("utf-8"))
                except Exception as e:
                    error_events.append(f"send prompt: {e}")
                    prompt_done.set()

            prompt_thread = threading.Thread(target=_send_prompt, daemon=True)
            prompt_thread.start()

            # ── 4. 主循环：从队列消费事件 + 等 prompt 完成 ──
            _last_activity = time.time()
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
                        yield (et, data)

                if prompt_done.is_set():
                    break

                if reader_done.is_set() and not prompt_done.is_set():
                    # hermes 进程退出但 prompt 未完成
                    if collected_text:
                        log.info(f"{label}hermes ACP reader EOF with {len(collected_text)} text chunks, assuming done")
                        prompt_result["stopReason"] = "end_turn"
                        break
                    error_events.append("hermes process exited before prompt completed")
                    break

                elapsed_silent = time.time() - _last_activity
                if elapsed_silent > max_exec_seconds:
                    if proc.poll() is None and elapsed_silent < max_exec_seconds * 2:
                        # 进程还活着，可能是长命令/长思考
                        _last_activity = time.time()
                        time.sleep(0.1)
                        continue
                    # 超时或进程已退出：如果有文本输出，认为 prompt 已完成
                    if collected_text:
                        log.info(f"{label}hermes ACP silent {elapsed_silent:.0f}s with {len(collected_text)} text chunks, assuming done")
                        prompt_result["stopReason"] = "end_turn"
                        break
                    log.warning(f"{label}hermes ACP no activity for {max_exec_seconds}s, terminating")
                    break

                # 兜底：600s 无新消息 + 已有文本输出 → prompt 已完成但 response 丢失
                # （原 30s 太激进，terraform init/apply 等长命令执行期间无 stdout，30s idle 会误杀）
                if elapsed_silent > 600 and collected_text:
                    log.info(f"{label}hermes ACP idle 600s with {len(collected_text)} text chunks, assuming prompt done (response may be lost)")
                    prompt_result["stopReason"] = "end_turn"
                    break

                time.sleep(0.05)

            # ── 5. 收尾 ──
            # flush 残留的文本缓冲
            _flush_text()
            _flush_thinking()

            stop_reason = prompt_result.get("stopReason", "")
            log.info(f"{label}hermes ACP prompt done: stopReason={stop_reason}")

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
                stderr_output = proc.stderr.read().decode("utf-8", errors="replace")[:500]
            except Exception:
                pass

            exit_code = proc.returncode if proc.returncode is not None else -1
            full_text = "\n".join(collected_text)

            yield ("step_finish", f"Step 1 done (stopReason={stop_reason})")
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
            _untrack_proc(proc)
            try:
                proc.stdin.close()
            except Exception:
                pass
            if proc.poll() is None:
                _terminate_proc(proc, f"{label}hermes ACP")
            _cleanup_overlay(iso_home)


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
        self._on_prompt_done = self.on_prompt_done
        self._on_prompt_error = self.on_prompt_error

        self._write_lock = threading.Lock()
        self._next_id = 1
        self._pending: dict[int, threading.Event] = {}
        self._pending_results: dict[int, any] = {}
        self._pending_errors: dict[int, str] = {}
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
            return

        if "error" in raw:
            err = raw["error"]
            err_msg = err.get("message", str(err))
            if err.get("data"):
                err_msg += f" (data={err['data']})"
            with self._lock:
                self._pending_errors[req_id] = err_msg
        else:
            result = raw.get("result")
            # 如果是 session/prompt 响应，提取 stopReason 并触发回调
            if isinstance(result, dict):
                stop_reason = result.get("stopReason", "")
                if stop_reason:
                    # prompt 完成，触发回调
                    self._on_prompt_done({"stopReason": stop_reason, "result": result})
            with self._lock:
                self._pending_results[req_id] = result

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
            self._on_prompt_done({"stopReason": stop_reason, "result": update})


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
                with open(src_cfg, "r") as f:
                    cfg_text = f.read()
                cfg_text = re.sub(r'(tool_progress:\s*)\w+', r'\g<1>new', cfg_text)
                with open(dst_cfg, "w") as f:
                    f.write(cfg_text)
            except Exception:
                shutil.copy2(src_cfg, dst_cfg)

        # 覆写 .env：保留源凭据 + pin HERMES_HOME
        src_env = os.path.join(src_home, ".env")
        dst_envf = os.path.join(iso_home, ".env")
        env_body = ""
        if os.path.isfile(src_env):
            try:
                with open(src_env, "r") as f:
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
            with open(dst_envf, "w") as f:
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
