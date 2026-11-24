"""ACP 客户端单源（P0-1/A1，2026-11-13 spec）：transport + pending + 生命周期。

根因：`_ACPSimpleClient` 此前在 hermes_acp.py:818 与 hwcloud_acp.py:694 各持一份
约 300 行的复制体（aishell_acp 已 import 复用 hwcloud 版），同一 bug 修复须改两处。
D-P0-2 拍板以 hermes 版为基准逐方法搬（它是最早实现且经 429 事故加固，hwcloud
版是它的退化副本——以 hwcloud_acp.py:562 / aishell_acp.py:628「并行」注释为证）。

结构（三分法，参照 OpenAgents acp-client.js）：
- PipeTransport：bwrap 路径 subprocess pipe 包装（select + 静默超时语义）
- _ACPSimpleClient：JSON-RPC 机制——request/response + pending 表 + reader 线程
  （transport 云沙箱模式 / stdin-stdout 本机 PTY 双模式）+ handle_line 分派

引擎 quirk 红线（spec §7 第 1 条：不许描述化吞并语义）——基类只收机制，差异走
显式 hook，各引擎在薄子类覆写：
- `_ignore_update(update_type_norm)`：噪音过滤（hwcloud 实测清单）
- `_process_tool_call(update)`：tool_call 入参解析（hermes issue #976 增强版）
- `_process_extra_update(update_type_norm, update)`：引擎私有 update 类型
  （hwcloud plan_update）
- `_fs_read_text_file/_fs_write_text_file(raw, req_id)`：Agent→Client RPC
  （hwcloud 实盘服务；基类按 hermes 现状答 -32601）

行为对齐声明：基类每个 hook 的默认实现 = hwcloud 现网行为（hwcloud 版是较瘦的
复制体，其差异面就是 hook 面）；hermes 的 #976 工具入参解析与占位事件在其
薄子类覆写，等价性由 tests/runtime/test_hermes_acp_tool_call_input.py 与
test_hwcloud_acp.py 双套既有单测守护（迁移不改断言）。
"""
from __future__ import annotations

import json
import select as _select_mod
import threading
import time

from src.kernel.config import log
from src.domains.runtime.agents.sandbox.backends.api import (
    TransportTimeout, TransportClosed, ACPTransport,
)


class PipeTransport:
    """bwrap 路径 transport——包装 subprocess.Popen 的 stdin/stdout pipe。

    现有 Popen 已设 bufsize=0，proc.stdout 是无缓冲 FileIO，
    select + os.read 可靠（不和 BufferedReader 冲突）。

    readline timeout 语义：连续静默 N 秒才超时，有数据就重置 deadline。
    长 prompt 生成几分钟不超时，只有完全静默才判死。
    """

    def __init__(self, stdin, stdout):
        self.stdin = stdin
        self.stdout = stdout
        self.buf = b""
        self._write_lock = threading.Lock()

    def write(self, data: bytes) -> None:
        with self._write_lock:
            self.stdin.write(data)
            self.stdin.flush()

    def readline(self, timeout: float = 30.0) -> str:
        import os as _os
        deadline = time.monotonic() + timeout
        while b"\n" not in self.buf:
            if len(self.buf) > 10 * 1024 * 1024:
                raise TransportClosed("line exceeds 10MB")
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TransportTimeout(f"readline timeout after {timeout}s silent")
            r, _, _ = _select_mod.select([self.stdout], [], [], remaining)
            if not r:
                # select 超时 = 静默超时，可恢复
                raise TransportTimeout(f"readline timeout after {timeout}s silent")
            chunk = _os.read(self.stdout.fileno(), 65536)
            if not chunk:
                # pipe 关了 = 不可恢复
                raise TransportClosed("stdout pipe closed")
            self.buf += chunk
            deadline = time.monotonic() + timeout
        line, self.buf = self.buf.split(b"\n", 1)
        return line.decode("utf-8") + "\n"

    def close(self) -> None:
        try:
            self.stdin.close()
        except Exception:
            pass
        try:
            self.stdout.close()
        except Exception:
            pass


# update 类型归一表：两版复制体并集（hwcloud 版含 planupdate；hermes 版无——
# 归一后 hermes 未实现的类型走 _process_extra_update no-op，行为等价于原「丢弃」）
_UPDATE_TYPE_MAP = {
    "agentmessagechunk": "agent_message_chunk",
    "agentthoughtchunk": "agent_thought_chunk",
    "toolcall": "tool_call",
    "toolcallupdate": "tool_call_update",
    "planupdate": "plan_update",
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
    # tool title 格式: "terminal" / "write_file" / "read_file" 等
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


class _ACPSimpleClient:
    """极简 ACP JSON-RPC 客户端：request/response + notification 处理（单源基类）。

    两种构造（兼容两类调用方）：
    - transport=ACPTransport —— 云沙箱路径（_run_acp_session 传 CubeSandboxTransport），
      用 transport.readline/write，start_reader() 自带 reader 线程
    - stdin/stdout —— 本机 PTY 路径（HermesACPBackend.stream_execute），
      外部 reader 线程调 handle_line

    引擎差异见模块 docstring 的 hook 清单——继承方只覆写 hook，机制不许复制。
    """

    def __init__(self, stdin=None, stdout=None, label="", on_message=None, on_prompt_done=None, on_prompt_error=None,
                 transport=None):
        self.transport = transport
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
        #（生产长期只能靠 60s text 静默兜底收尾；此 bug 已修，调用点直接读公开属性）。

        self._write_lock = threading.Lock()
        self._next_id = 1
        self._pending: dict[int, threading.Event] = {}
        self._pending_results: dict[int, any] = {}
        self._pending_errors: dict[int, str] = {}
        self._prompt_req_id: int = 0  # session/prompt 的 req_id（只有它才触发 prompt_done）
        self._lock = threading.Lock()
        # reader 线程生命周期管理（transport 模式用）
        self._reader_thread: threading.Thread | None = None
        self._running = False
        self._reader_done = threading.Event()

    # ── reader 线程（transport 模式）──────────────────────────────

    def start_reader(self, silent_timeout: float = 60.0,
                     global_deadline: float | None = None):
        """[云沙箱路径] transport 模式的 reader 线程。

        - TransportTimeout（静默超时）：检查 global_deadline，没超就 continue，超了就退出
        - TransportClosed（断开）：直接退出
        """
        if not self.transport:
            raise RuntimeError("start_reader requires transport mode")

        def _reader():
            try:
                while self._running:
                    if global_deadline and time.monotonic() > global_deadline:
                        break
                    try:
                        line = self.transport.readline(timeout=silent_timeout)
                    except TransportTimeout:
                        if global_deadline and time.monotonic() < global_deadline:
                            continue  # 静默超时但全局没超——继续等
                        break  # 全局超时
                    except TransportClosed:
                        break  # 不可恢复断开
                    line_str = line.strip()
                    if not line_str:
                        continue
                    try:
                        self.handle_line(line_str)
                    except Exception as e:
                        if self.label:
                            log.debug(f"[{self.label}] acp reader handle_line: {e}")
            finally:
                self._reader_done.set()

        self._running = True
        self._reader_thread = threading.Thread(target=_reader, daemon=True, name="acp-reader")
        self._reader_thread.start()

    def stop_reader(self):
        self._running = False
        self._reader_done.wait(timeout=5)

    def is_reader_done(self) -> bool:
        return self._reader_done.is_set()

    # ── JSON-RPC 机制：id 分配 / pending / 写出 / 请求─────────────

    def next_id(self) -> int:
        """分配并返回新的 JSON-RPC request id，内部管理 _next_id。"""
        with self._lock:
            req_id = self._next_id
            self._next_id += 1
        return req_id

    def register_pending(self, req_id: int) -> threading.Event:
        """注册一个 pending request，返回用于等待的 Event。"""
        evt = threading.Event()
        with self._lock:
            self._pending[req_id] = evt
        return evt

    def clear_pending(self):
        """清理所有残留的 pending entry。"""
        with self._lock:
            for pid in list(self._pending.keys()):
                self._pending.pop(pid, None)

    def set_prompt_req_id(self, req_id: int):
        """设置 session/prompt 的 req_id（只有它才触发 prompt_done）。"""
        self._prompt_req_id = req_id

    def register_prompt_pending(self, req_id: int, event: "threading.Event") -> None:
        """[F5] 注册 prompt 响应的 pending 等待（公开 API，支撑件勿触私有成员）。

        与「先 request() 后手工挂 pending」的既有调用方（hermes/hwcloud/mock）
        语义一致：把 req_id 映射到 Event，response 到达时 _handle_response
        触发 on_prompt_done 并 set 该 Event。
        """
        with self._lock:
            self._pending[req_id] = event

    def close(self):
        """transport 模式：关闭 transport（PTY/fifo 清理由 transport.close() 负责）。"""
        try:
            self.stop_reader()
        except Exception:
            pass
        if self.transport is not None:
            try:
                self.transport.close()
            except Exception:
                pass
        else:
            for f in (self.stdin, self.stdout):
                try:
                    if f:
                        f.close()
                except Exception:
                    pass

    def write_line(self, data: bytes):
        with self._write_lock:
            if self.transport is not None:
                self.transport.write(data)  # 云沙箱 transport 模式
            else:
                self.stdin.write(data)
                self.stdin.flush()

    def _write_response(self, resp: dict):
        """Agent→Client 请求的应答统一写出（NDJSON 一行）。"""
        data = (json.dumps(resp) + "\n").encode("utf-8")
        try:
            self.write_line(data)
        except Exception as e:
            log.warning(f"{self.label}ACP write response error: {e}")

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

    # ── 收线分派：response / agent-request / notification──────────

    def handle_line(self, line: str):
        """处理从引擎 stdout 收到的一行 JSON-RPC 消息。"""
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
                        log.info(f"{self.label}ACP prompt response without stopReason, treating as done (id={req_id})")
                        self.on_prompt_done({"stopReason": "end_turn", "result": result})
                else:
                    self.on_prompt_done({"stopReason": "end_turn", "result": result or {}})

        evt.set()

    def _handle_agent_request(self, raw: dict):
        """处理 agent → client 的 JSON-RPC 请求（fs RPC / session/request_permission）。"""
        method = raw.get("method", "")
        req_id = raw["id"]

        if method == "fs/read_text_file":
            self._fs_read_text_file(raw, req_id)
            return
        if method == "fs/write_text_file":
            self._fs_write_text_file(raw, req_id)
            return

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

        self._write_response(resp)

    # hook：Agent→Client fs RPC。基类按 hermes 现网行为答 -32601（hermes 引擎
    # 不发 fs RPC；hwcloud 实测会发，其薄子类覆写为实盘服务）。
    def _fs_read_text_file(self, raw: dict, req_id):
        self._write_response({"jsonrpc": "2.0", "id": req_id,
                              "error": {"code": -32601, "message": "method not found: fs/read_text_file"}})

    def _fs_write_text_file(self, raw: dict, req_id):
        self._write_response({"jsonrpc": "2.0", "id": req_id,
                              "error": {"code": -32601, "message": "method not found: fs/write_text_file"}})

    def _handle_notification(self, raw: dict):
        """处理 session/update 通知（agent 消息、工具调用等）。"""
        method = raw.get("method", "")
        if method not in ("session/update", "session/notification"):
            return

        params = raw.get("params", {})
        if not isinstance(params, dict):
            return
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

        # hook：引擎私有噪音过滤（hwcloud 实测清单在其子类）
        if self._ignore_update(update_type_norm):
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
            self._process_tool_call(update)
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
        else:
            # hook：引擎私有 update 类型（hwcloud plan_update 在其子类）
            self._process_extra_update(update_type_norm, update)

    # ── 引擎 quirk hook（默认实现 = hwcloud 现网行为）─────────────

    def _ignore_update(self, update_type_norm: str) -> bool:
        """噪音过滤 hook：返回 True 则丢弃该 update。默认不过滤。"""
        return False

    def _process_tool_call(self, update: dict):
        """tool_call 入参解析 hook（hwcloud 简单链语义；hermes 在其子类覆写为
        issue #976 增强版：rawInput → input → parameters → content 块 + 占位事件）。"""
        tool_call_id = update.get("toolCallId", "")
        name = update.get("name", "")
        title = update.get("title", "")
        raw_input = update.get("rawInput") or update.get("input") or update.get("parameters") or {}
        tool_name = _tool_name_from_title(title) or name
        if tool_name:
            self.on_message("tool_use", {"tool": tool_name, "input": raw_input})

    def _process_extra_update(self, update_type_norm: str, update: dict):
        """引擎私有 update 类型 hook。默认丢弃（各引擎现网对未知类型即丢弃）。"""
        return
