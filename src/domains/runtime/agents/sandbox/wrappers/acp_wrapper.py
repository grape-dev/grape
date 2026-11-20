#!/usr/bin/env python3
"""ACP Wrapper — 在 CubeSandbox 沙箱内桥接 hermes acp。

用 subprocess.Popen + OS 管道启动 hermes acp，完成完整 ACP 握手
（initialize → session/new → session/prompt），把结果以 NDJSON 事件流
输出到 stdout。PTY 只负责启动本脚本，ACP 协议在本脚本内部的管道上完成。

对外接口：
  输入：环境变量 GRAPE_TASK_PROMPT（任务 prompt）
        环境变量 GRAPE_TASK_CWD（工作目录，可选）
        环境变量 GRAPE_TASK_TIMEOUT（超时秒数，可选，默认 600）
        其他环境变量（HERMES_HOME, LLM_API_KEY 等）由调用方传入
  输出（stdout）：NDJSON 事件流，每行一个 JSON 对象
  输出（stderr）：调试日志
  退出码：0=成功，1=ACP 协议错误，2=hermes 未找到，3=超时

零第三方依赖，只用 Python 标准库。
"""
import json
import os
import select
import signal
import subprocess
import sys
import threading
import time

# ── NDJSON 输出辅助 ──

def emit(event: dict):
    """输出一个 NDJSON 事件到 stdout，立即 flush。"""
    line = json.dumps(event, ensure_ascii=False)
    sys.stdout.write(line + "\n")
    sys.stdout.flush()

def emit_text(content: str):
    if content:
        emit({"type": "text", "content": content})

def emit_reasoning(content: str):
    if content:
        emit({"type": "reasoning", "content": content})

def emit_tool_use(tool: str, input_data):
    emit({"type": "tool_use", "tool": tool, "input": input_data or {}})

def emit_tool_finish(content: str):
    if content:
        emit({"type": "tool_finish", "content": content})

def emit_tool_result(content: str):
    if content:
        emit({"type": "tool_result", "content": content})

def emit_done(exit_code: int, stop_reason: str = "", session_id: str = "", errors: list | None = None):
    emit({"type": "done", "exit_code": exit_code, "stop_reason": stop_reason,
          "session_id": session_id, "errors": errors or []})

def log(msg: str):
    """调试日志到 stderr。"""
    sys.stderr.write(f"[acp_wrapper] {msg}\n")
    sys.stderr.flush()


# ── ACP JSON-RPC 消息解析 ──

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
    title = title.strip()
    if title.startswith("\U0001f4bb") or title.startswith("$ "):
        return "terminal"
    if title.startswith("\u2705"):
        return ""
    if title.replace("_", "").replace("-", "").isalnum():
        return title
    return title

def _extract_session_id(result) -> str:
    if not result or not isinstance(result, dict):
        return ""
    sid = result.get("sessionId") or result.get("session_id")
    if sid:
        return sid
    meta = result.get("_meta", {})
    if isinstance(meta, dict):
        hp = meta.get("hermes", {}).get("sessionProvenance", {})
        if isinstance(hp, dict):
            sid = hp.get("acpSessionId") or hp.get("currentHermesSessionId")
            if sid:
                return sid
    return ""


# ── ACP Wrapper 主体 ──

class ACPWrapper:
    """极简 ACP 客户端：用 OS 管道跟 hermes acp 通信。"""

    def __init__(self, prompt: str, cwd: str, timeout: int):
        self.prompt = prompt
        self.cwd = cwd
        self.timeout = timeout
        self.proc = None
        self.session_id = ""
        self._next_id = 1
        self._lock = threading.Lock()
        self._pending: dict = {}  # id → Event
        self._results: dict = {}  # id → result
        self._errors: dict = {}   # id → error str
        self._prompt_req_id = 0
        self._prompt_done = threading.Event()
        self._prompt_result = {}
        self._prompt_errors = []
        self._reader_done = threading.Event()
        self._text_buf = []
        self._think_buf = []
        self._BUF_MAX = 200

    def _next(self):
        with self._lock:
            rid = self._next_id
            self._next_id += 1
            return rid

    def _send(self, msg: dict):
        assert self.proc is not None and self.proc.stdin is not None
        data = (json.dumps(msg) + "\n").encode("utf-8")
        self.proc.stdin.write(data)
        self.proc.stdin.flush()

    def _request(self, method: str, params: dict, wait_timeout: float = 60) -> tuple:
        """发送请求，等待响应。返回 (result, error)。"""
        rid = self._next()
        evt = threading.Event()
        with self._lock:
            self._pending[rid] = evt
        msg = {"jsonrpc": "2.0", "id": rid, "method": method, "params": params}
        try:
            self._send(msg)
        except Exception as e:
            with self._lock:
                self._pending.pop(rid, None)
            return None, f"send {method}: {e}"

        if evt.wait(timeout=wait_timeout):
            with self._lock:
                result = self._results.pop(rid, None)
                error = self._errors.pop(rid, None)
                self._pending.pop(rid, None)
            return result, error
        else:
            with self._lock:
                self._pending.pop(rid, None)
            return None, f"{method} timeout after {wait_timeout}s"

    def _handle_line(self, line: str):
        """处理从 hermes stdout 收到的一行 JSON-RPC 消息。"""
        try:
            raw = json.loads(line)
        except json.JSONDecodeError:
            return

        # 1. Response（有 id，有 result/error）
        if "id" in raw and ("result" in raw or "error" in raw):
            self._handle_response(raw)
            return

        # 2. Agent → client request（有 id，有 method）
        if "id" in raw and "method" in raw:
            self._handle_agent_request(raw)
            return

        # 3. Notification（无 id，有 method）
        if "method" in raw and "id" not in raw:
            self._handle_notification(raw)
            return

    def _handle_response(self, raw: dict):
        rid = raw["id"]
        with self._lock:
            evt = self._pending.get(rid)

        if evt is None:
            return

        is_prompt_response = (rid == self._prompt_req_id)

        if "error" in raw:
            err = raw["error"]
            err_msg = err.get("message", str(err))
            if err.get("data"):
                err_msg += f" (data={err['data']})"
            with self._lock:
                self._errors[rid] = err_msg
            if is_prompt_response:
                self._prompt_result = {"stopReason": "error"}
                self._prompt_errors.append(err_msg)
                self._prompt_done.set()
        else:
            result = raw.get("result")
            with self._lock:
                self._results[rid] = result
            if is_prompt_response:
                if isinstance(result, dict):
                    stop_reason = result.get("stopReason", "end_turn")
                    self._prompt_result = {"stopReason": stop_reason}
                else:
                    self._prompt_result = {"stopReason": "end_turn"}
                self._prompt_done.set()

        evt.set()

    def _handle_agent_request(self, raw: dict):
        """处理 agent → client 的 JSON-RPC 请求（如 session/request_permission）。"""
        method = raw.get("method", "")
        rid = raw["id"]

        if method == "session/request_permission":
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
                for opt in options:
                    if opt.get("kind", "") in ("allow_once", "allow_always"):
                        option_id = opt.get("optionId", "")
                        break
            resp = {"jsonrpc": "2.0", "id": rid,
                    "result": {"outcome": {"outcome": "selected", "optionId": option_id}}}
        else:
            resp = {"jsonrpc": "2.0", "id": rid,
                    "error": {"code": -32601, "message": f"method not found: {method}"}}

        try:
            self._send(resp)
        except Exception as e:
            log(f"write response error: {e}")

    def _handle_notification(self, raw: dict):
        """处理 session/update 通知。"""
        method = raw.get("method", "")
        if method not in ("session/update", "session/notification"):
            return

        params = raw.get("params", {})
        update = params.get("update", params)
        if not isinstance(update, dict):
            return

        update_type = update.get("sessionUpdate", update.get("type", ""))
        update_type_norm = _normalize_update_type(update_type)
        if not update_type_norm:
            if len(update) == 1:
                key = list(update.keys())[0]
                update_type_norm = _normalize_update_type(key)
                update = update[key] if isinstance(update.get(key), dict) else update
            if not update_type_norm:
                return

        if update_type_norm == "agent_message_chunk":
            text = update.get("content", {}).get("text", "")
            if text:
                self._text_buf.append(text)
                if "\n" in text or sum(len(s) for s in self._text_buf) >= self._BUF_MAX:
                    self._flush_text()
        elif update_type_norm == "agent_thought_chunk":
            text = update.get("content", {}).get("text", "")
            if text:
                self._think_buf.append(text)
                if "\n" in text or sum(len(s) for s in self._think_buf) >= self._BUF_MAX:
                    self._flush_thinking()
        elif update_type_norm == "tool_call":
            self._flush_text()
            self._flush_thinking()
            tool_call_id = update.get("toolCallId", "")
            name = update.get("name", "")
            title = update.get("title", "")
            raw_input = update.get("rawInput") or update.get("input") or update.get("parameters") or {}
            tool_name = _tool_name_from_title(title) or name
            if tool_name:
                emit_tool_use(tool_name, raw_input)
        elif update_type_norm == "tool_call_update":
            status = update.get("status", "")
            title = update.get("title", update.get("name", ""))
            tool_name = _tool_name_from_title(title) or title
            if status in ("completed", "failed"):
                emit_tool_finish(f"Tool {tool_name} {status}")
                output = update.get("rawOutput") or update.get("output")
                if output:
                    output_text = output if isinstance(output, str) else json.dumps(output, ensure_ascii=False)[:500]
                    emit_tool_result(output_text)
        elif update_type_norm == "turn_end":
            stop_reason = update.get("stopReason", "")
            self._prompt_result = {"stopReason": stop_reason}
            self._prompt_done.set()

    def _flush_text(self):
        if self._text_buf:
            line = "".join(self._text_buf)
            self._text_buf.clear()
            if line.strip():
                emit_text(line)

    def _flush_thinking(self):
        if self._think_buf:
            line = "".join(self._think_buf)
            self._think_buf.clear()
            if line.strip():
                emit_reasoning(line)

    def _reader(self):
        """非阻塞读 hermes stdout，逐行解析 JSON-RPC。"""
        assert self.proc is not None and self.proc.stdout is not None
        fd = self.proc.stdout.fileno()
        import fcntl
        fl = fcntl.fcntl(fd, fcntl.F_GETFL)
        fcntl.fcntl(fd, fcntl.F_SETFL, fl | os.O_NONBLOCK)

        read_buf = b""
        try:
            while True:
                readable, _, _ = select.select([fd], [], [], 0.5)
                if not readable:
                    if self.proc.poll() is not None:
                        break
                    continue
                try:
                    chunk = self.proc.stdout.read(4096)
                except (BlockingIOError, OSError):
                    continue
                if not chunk:
                    break
                read_buf += chunk
                while b"\n" in read_buf:
                    line, read_buf = read_buf.split(b"\n", 1)
                    line_str = line.decode("utf-8", errors="replace").strip()
                    if not line_str:
                        continue
                    try:
                        self._handle_line(line_str)
                    except Exception as e:
                        log(f"handle_line error: {e}")
        except Exception as e:
            log(f"reader exception: {e}")
        finally:
            self._reader_done.set()
            log("reader EOF")

    def _stderr_reader(self):
        """排空 stderr 管道，防止管道满导致死锁。"""
        assert self.proc is not None and self.proc.stderr is not None
        try:
            while True:
                line = self.proc.stderr.readline()
                if not line:
                    break
                # stderr 日志直接转发到 wrapper 的 stderr（供调试）
                sys.stderr.write(line.decode("utf-8", errors="replace"))
                sys.stderr.flush()
        except Exception:
            pass

    def run(self) -> int:
        """主流程：启动 hermes acp → 握手 → 发 prompt → 等完成 → 收尾。"""
        # 1. 找到 hermes
        import shutil
        hermes_cmd = shutil.which("hermes") or shutil.which("hermes.cmd")
        if not hermes_cmd:
            log("hermes not found in PATH")
            emit_done(2, errors=["hermes not found"])
            return 2

        # 2. 启动 hermes acp
        self.proc = subprocess.Popen(
            [hermes_cmd, "acp"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=self.cwd if self.cwd and os.path.isdir(self.cwd) else None,
            bufsize=0,
            start_new_session=True,
        )

        reader_thread = threading.Thread(target=self._reader, daemon=True)
        reader_thread.start()
        stderr_thread = threading.Thread(target=self._stderr_reader, daemon=True)
        stderr_thread.start()

        try:
            # 3. initialize 握手
            init_result, init_err = self._request("initialize", {
                "protocolVersion": 1,
                "clientInfo": {"name": "grape-agent", "version": "0.1.0"},
                "clientCapabilities": {},
            }, wait_timeout=100)
            if init_err:
                log(f"initialize failed: {init_err}")
                emit_done(1, errors=[f"initialize failed: {init_err}"])
                return 1
            log("initialized")

            # 4. session/new
            cwd_arg = self.cwd if self.cwd else "."
            result, err = self._request("session/new", {
                "cwd": cwd_arg,
                "mcpServers": [],
            }, wait_timeout=100)
            if err:
                log(f"session/new failed: {err}")
                emit_done(1, errors=[f"session/new failed: {err}"])
                return 1
            self.session_id = _extract_session_id(result) or ""
            log(f"session: {self.session_id[:12]}...")

            # 清理残留 pending
            with self._lock:
                for pid in list(self._pending.keys()):
                    self._pending.pop(pid, None)

            # 5. session/prompt
            prompt_id = self._next()
            self._prompt_req_id = prompt_id
            with self._lock:
                self._pending[prompt_id] = threading.Event()

            prompt_msg = {
                "jsonrpc": "2.0", "id": prompt_id,
                "method": "session/prompt",
                "params": {
                    "sessionId": self.session_id,
                    "prompt": [{"type": "text", "text": self.prompt}],
                },
            }
            try:
                self._send(prompt_msg)
            except Exception as e:
                log(f"send prompt failed: {e}")
                emit_done(1, errors=[f"send prompt: {e}"])
                return 1

            # 6. 等待 prompt 完成（带超时）
            deadline = time.time() + self.timeout
            while not self._prompt_done.is_set():
                if time.time() > deadline:
                    log(f"timeout after {self.timeout}s")
                    self._flush_text()
                    self._flush_thinking()
                    emit_done(3, stop_reason="timeout", session_id=self.session_id,
                              errors=[f"timeout after {self.timeout}s"])
                    return 3
                # hermes 进程退出但没发 PromptResponse
                if self._reader_done.is_set() and not self._prompt_done.is_set():
                    self._flush_text()
                    self._flush_thinking()
                    log("reader EOF before prompt done, assuming done")
                    emit_done(0, stop_reason="end_turn", session_id=self.session_id)
                    return 0
                time.sleep(0.05)

            # 7. 收尾
            self._flush_text()
            self._flush_thinking()

            stop_reason = self._prompt_result.get("stopReason", "end_turn")
            log(f"done: stopReason={stop_reason}")

            # 关闭 stdin 让 hermes 退出
            try:
                self.proc.stdin.close()
            except Exception:
                pass

            # 等待进程退出（grace 5s）
            try:
                self.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self._kill_proc()

            reader_thread.join(timeout=3)

            exit_code = 0 if stop_reason in ("end_turn", "end") and not self._prompt_errors else 1
            emit_done(exit_code, stop_reason=stop_reason, session_id=self.session_id,
                      errors=self._prompt_errors)
            return exit_code

        except Exception as e:
            log(f"exception: {e}")
            import traceback
            traceback.print_exc(file=sys.stderr)
            self._flush_text()
            self._flush_thinking()
            emit_done(1, errors=[str(e)])
            return 1
        finally:
            self._kill_proc()

    def _kill_proc(self):
        """kill hermes 进程。"""
        if self.proc and self.proc.poll() is None:
            try:
                os.killpg(os.getpgid(self.proc.pid), signal.SIGKILL)
            except Exception:
                try:
                    self.proc.kill()
                except Exception:
                    pass


def main():
    prompt = os.environ.get("GRAPE_TASK_PROMPT", "")
    if not prompt:
        emit_done(1, errors=["GRAPE_TASK_PROMPT not set"])
        sys.exit(1)

    cwd = os.environ.get("GRAPE_TASK_CWD", "/home/user/repo")
    timeout = int(os.environ.get("GRAPE_TASK_TIMEOUT", "600"))

    wrapper = ACPWrapper(prompt=prompt, cwd=cwd, timeout=timeout)
    exit_code = wrapper.run()
    sys.exit(exit_code)


if __name__ == "__main__":
    main()
