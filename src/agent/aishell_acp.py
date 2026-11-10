"""AishellDsh ACP 后端 — 华为 AI Shell 本地发行版（dsh 框架），`aishell acp` ACP-over-stdio。

设计结论与阶段 0 实测证据：docs/todo/2026-11-05-aishell-dsh-runtime-acp-design.html
（本文件自包含关键差异，协议细节以实测为准，2026-11-06 aishell 0.0.2 真机验证）：

- 启动命令：`aishell acp`（非 hwcloud 的 `serve --acp`）
- 会话路由：session/new 返回 configOptions（id="model"），默认 provider=aishell/trial
  无凭据必报 MISSING_CREDENTIAL —— 提示词前须 set_config_option 切到设备已配的
  provider（cordis.patch.yml 用户自配，grape 不持 key/不透传，D3/D4）
- model option 的 value 不透明（JSON 串，如 '["<provider>","<model>"]'），必须从
  options 原样取，自拼报 -32602
- session/update 实测 5 类：agent_thought_chunk / agent_message_chunk /
  usage_update（{used,size} 上下文占用，非计费——一期忽略）/ tool_call /
  tool_call_update；tool_call_update 的输出在 content[] 内容块（与 hwcloud 的
  rawOutput 不同）
- 无 request_permission（免审直执行）；无 turn_end 通知，PromptResponse 收口
- 模型选择用 env AISHELL_DSH_MODEL 覆盖（原样 option value 字符串）；
  未设置时跳过 trial 默认值自动选第一个非 trial 路由，profile 已配好则不干预

凭据/复用边界（同 hwcloud_acp 模式）：客户端类复制自 hwcloud_acp._ACPSimpleClient
（仓库惯例：各引擎自持一份，适配各自协议差异），适配 aishell 的 content[] 输出块。
"""
from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import threading
import time
from typing import Generator

log = logging.getLogger(__name__)

# 复用 hwcloud_acp 的通用件（请求客户端 / 超时 / 辅助函数）
from src.agent.hwcloud_acp import (  # noqa: E402
    _ACPSimpleClient,
    _done_result,
    _extract_session_id,
    _global_timeout_for,
    _normalize_update_type,
    _tool_name_from_title,
)

# aishell 默认 trial 路由（无凭据必失败，见模块 docstring ①）
_TRIAL_MODEL_VALUE = '["aishell","trial"]'


class AishellDshACPBackend:
    """aishell-dsh ACP 后端 — 不走 build_args/parse_output，直接 stream_execute yield 事件。"""

    name = "aishell-dsh"
    binary = "aishell"
    prompt_via_stdin = False  # ACP 模式不用 stdin 传 prompt，用 JSON-RPC

    def build_args(self, prompt: str, session_id: str = "") -> list[str]:
        # ACP 模式不用 CLI 参数传 prompt；保留接口以兼容 AgentRuntime 契约
        # （chat 生产路径会取 backend.build_args 组命令，同 src/runtime/hwcloud.py 先例）。
        return ["acp"]

    def prepare_env(self, env: dict, cwd: str = "") -> "str | None":
        """任务级 env 合并之后的收尾钩子（dsh 框架专属 token 穿透，同 src/runtime/dsh.py）。

        aishell 与 dsh 同框架：其 bash 工具 spawn 时会 scrub 父进程 env 里所有
        匹配 /KEY|PASSWORD|SECRET|TOKEN/i 的变量名 → GRAPE_SESSION_TOKEN 被洗掉 →
        grape CLI 无法认证（2026-11-06 远端 Worker vod 场景实证）。

        解法：token 写 0600 临时文件，路径经 GRAPE_AUTH_FILE 传递（变量名不命中
        scrub）；grape CLI config 先读该文件再 fallback 环境变量。

        落盘位置优先 cwd（工位目录）：bwrap 沙箱把 /tmp 挂成私有 tmpfs
        （src/agent/sandbox/__init__.py --tmpfs /tmp），沙箱外写入 /tmp 的文件
        沙箱内不可见——coding/worktree 场景曾因此 GRAPE_AUTH_FILE 悬空
        （2026-11-06 event #6 coding 前置检查实证）。cwd 被沙箱 rw-bind 必可见。
        为防污染 git status，同步写入 .git/info/exclude（仓库级忽略，不动跟踪文件）。
        ⚠️ 必须在 env_extra 合并之后调用（token 是任务级的，get_env 阶段尚未进 env）。
        返回实际文件路径（executor 在任务结束后负责清理）。
        """
        sess_tok = env.get("GRAPE_SESSION_TOKEN", "")
        if not sess_tok:
            return None
        try:
            import secrets as _secrets
            import tempfile
            if cwd:
                # 工位目录可能由 acp 服务稍后创建（prepare_env 先行）——主动建，
                # 否则 isdir 为假会静默 fallback /tmp → bwrap 沙箱内不可见（实证）
                os.makedirs(cwd, exist_ok=True)
                auth_path = os.path.join(cwd, f".grape-sess-{_secrets.token_hex(4)}.tok")
                fd = os.open(auth_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                with os.fdopen(fd, "w") as _f:
                    _f.write(sess_tok)
                # 仓库级忽略（.git/info/exclude），不动工作区跟踪文件。
                # 用 git-path（兼容 linked worktree：返回 per-worktree 的 exclude 路径，
                # rev-parse --git-dir 会返回主 .git/worktrees/<wt>，主仓 info/exclude
                # 不作用于该 worktree 的 status——QA 架构守护 C-2 实证）；
                # 失败必须留痕（log.warning），静默 pass 会让「exclude 没写成功」
                # 在 token 残留泄漏链路上不可见。
                try:
                    import subprocess as _sp
                    _excl_path = _sp.run(["git", "-C", cwd, "rev-parse", "--git-path", "info/exclude"],
                                         capture_output=True, text=True, timeout=5).stdout.strip()
                    if _excl_path:
                        if not os.path.isabs(_excl_path):
                            _excl_path = os.path.join(cwd, _excl_path)
                        os.makedirs(os.path.dirname(_excl_path), exist_ok=True)
                        # 通配模式一行覆盖所有任务（旧实现写唯一文件名，每任务一行
                        # 长期累积——QA Optional 建议采纳）
                        _pat = ".grape-sess-*.tok"
                        _exists = False
                        if os.path.isfile(_excl_path):
                            with open(_excl_path, encoding="utf-8") as _ef:
                                _exists = _pat in _ef.read().splitlines()
                        if not _exists:
                            with open(_excl_path, "a", encoding="utf-8") as _ef:
                                _ef.write(f"\n{_pat}\n")
                    else:
                        log.warning("prepare_env: git-path info/exclude returned empty for cwd=%s "
                                    "(exclude not registered, token residue risk on crash)", cwd)
                except Exception as _exc:
                    log.warning("prepare_env: failed to register .git/info/exclude for cwd=%s: %s "
                                "(token residue risk on crash)", cwd, _exc)
            else:
                fd, auth_path = tempfile.mkstemp(prefix="grape-sess-", suffix=".tok")
                os.write(fd, sess_tok.encode("utf-8"))
                os.close(fd)
                os.chmod(auth_path, 0o600)
            env["GRAPE_AUTH_FILE"] = auth_path
            return auth_path
        except Exception:
            return None  # 写文件失败不阻断执行（fallback 到 env 变量）

    def get_env(self, account: str = "", user_id: int = 0) -> dict:
        """基础 env（D3/D4：模型凭据设备预配置，grape 不注 key/不透传）。"""
        env = os.environ.copy()
        try:
            from src.config import SKILLS_DIR
            env["DEVOPS_SKILLS_DIR"] = SKILLS_DIR
        except Exception:
            pass
        env["DEVOPS_EVENT_TYPE"] = ""
        env["NO_COLOR"] = "1"
        env["TERM"] = "dumb"
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
        """ACP 协议执行，yield (event_type, data) 事件（契约与 hwcloud_acp 一致）。

        事件：step_start / text / reasoning / tool_use / tool_finish /
        tool_result / step_finish / done{exit_code,text,oc_session_id,errors,stderr}
        一期每任务 session/new（resume 属二期，另立 issue）。
        """
        label = f"{agent_label} " if agent_label else ""
        # CLI 定位统一走 find_cli（PATH 优先 + ~/.local/bin 兜底）——pip --user
        # 安装的 aishell 在 systemd 服务的精简 PATH 里不可见，纯 which 会漏（远端 Worker E2E 实证）
        from src.runtime.os_adapter import find_cli as _find_cli

        agent_cmd = _find_cli(self.binary) or shutil.which(self.binary + ".cmd")
        if not agent_cmd:
            log.error(f"{label}aishell-dsh ACP CLI not found in PATH")
            # 失败信息指名引擎（反向冒烟断言：绝不静默回退到旧 hwcloud）
            yield ("done", {
                "exit_code": -1, "text": "", "oc_session_id": session_id,
                "errors": ["aishell-dsh engine not available: aishell CLI not found in PATH"],
                "stderr": "",
            })
            return

        proc_env = dict(env) if env else os.environ.copy()

        log.info(f"{label}aishell-dsh ACP starting: cwd={cwd}")

        _popen_kwargs = {}
        if hasattr(os, "getpgid"):
            _popen_kwargs["start_new_session"] = True

        proc = subprocess.Popen(
            [agent_cmd, "acp"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            cwd=cwd or ".", env=proc_env, bufsize=0, **_popen_kwargs,
        )
        assert proc.stdin is not None and proc.stdout is not None and proc.stderr is not None

        try:
            from src.agent.executor import _track_proc, _untrack_proc, _terminate_proc
        except ImportError:
            _track_proc = _untrack_proc = lambda p: None
            _terminate_proc = lambda p, l: None

        from src.runtime.resource_stack import TaskResourceStack
        _res = TaskResourceStack(task_id="", cwd=cwd)
        _res.track_proc(proc)

        collected_text = []
        new_session_id = session_id
        error_events = []

        client = _AishellACPClient(
            stdin=proc.stdin,
            stdout=proc.stdout,
            label=label,
        )

        event_queue: list[tuple[str, object]] = []
        queue_lock = threading.Lock()
        reader_done = threading.Event()
        prompt_done = threading.Event()
        prompt_result: dict = {}

        _text_buf = []
        _think_buf = []
        _BUF_MAX = 200

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

        def _enqueue(et, data):
            with queue_lock:
                event_queue.append((et, data))

        def _on_message(msg_type: str, content):
            if msg_type == "text":
                _text_buf.append(content)
                if "\n" in content or sum(len(s) for s in _text_buf) >= _BUF_MAX:
                    _flush_text()
            elif msg_type == "thinking":
                _think_buf.append(content)
                if "\n" in content or sum(len(s) for s in _think_buf) >= _BUF_MAX:
                    _flush_thinking()
            elif msg_type == "tool_use":
                _flush_text()
                _flush_thinking()
                _enqueue("tool_use", content)
            elif msg_type == "tool_finish":
                _flush_text()
                _flush_thinking()
                _enqueue("tool_finish", content)
            elif msg_type == "tool_result":
                _enqueue("tool_result", content)

        client.on_message = _on_message
        client.on_prompt_done = lambda result: (
            prompt_result.update(result),
            prompt_done.set(),
        )
        client.on_prompt_error = lambda err: (
            error_events.append(err),
            prompt_done.set(),
        )

        # reader 线程。POSIX：select 非阻塞方案（同 hwcloud_acp，readline 会阻塞
        # 半行数据——单线程轮询场景的问题）；**Windows：select 只支持 socket，对
        # 管道 fd 直接抛 WinError 10038「非套接字」（2026-11-07 真任务实证，reader
        # 一起跑即死、事件全丢）——改用专用阻塞 readline 线程（行及时投递，EOF
        # 返回 b""，agent.py 探活同方案已真机验证）。
        import sys as _sys
        _is_win = _sys.platform == "win32"
        import select as _select
        _fd = proc.stdout.fileno()
        if not _is_win:
            import fcntl as _fcntl
            _fl = _fcntl.fcntl(_fd, _fcntl.F_GETFL)
            _fcntl.fcntl(_fd, _fcntl.F_SETFL, _fl | os.O_NONBLOCK)

        _read_buf = b""

        def _feed(chunk: bytes):
            """切行并投递 client（select/read 与 readline 两条路共用）。"""
            nonlocal _read_buf
            _read_buf += chunk
            while b"\n" in _read_buf:
                line, _read_buf = _read_buf.split(b"\n", 1)
                line_str = line.decode("utf-8", errors="replace").strip()
                if not line_str:
                    continue
                try:
                    client.handle_line(line_str)
                except Exception as e:
                    log.warning(f"{label}aishell-dsh ACP handle_line error: {e}")

        def _reader():
            nonlocal _read_buf
            try:
                if _is_win:
                    while True:
                        chunk = proc.stdout.readline()  # 阻塞至一行/EOF，专线程无碍
                        if not chunk:
                            break
                        _feed(chunk)
                else:
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
                        _feed(chunk)
            except Exception as e:
                log.warning(f"{label}aishell-dsh ACP reader exception: {e}")
            finally:
                reader_done.set()
                log.info(f"{label}aishell-dsh ACP reader EOF")

        reader_thread = threading.Thread(target=_reader, daemon=True)
        reader_thread.start()
        _track_proc(proc)

        _stderr_chunks = []

        def _stderr_reader():
            try:
                while True:
                    chunk = proc.stderr.read(4096)
                    if not chunk:
                        break
                    _stderr_chunks.append(chunk.decode("utf-8", errors="replace"))
            except Exception:
                pass

        threading.Thread(target=_stderr_reader, daemon=True).start()

        try:
            # ── 1. initialize ──
            init_result, init_err = client.request("initialize", {
                "protocolVersion": 1,
                "clientInfo": {"name": "grape-agent", "version": "0.1.0"},
                "clientCapabilities": {
                    "fs": {"readTextFile": True, "writeTextFile": True},
                },
            }, timeout=100)
            if init_err:
                error_events.append(f"initialize failed: {init_err}")
                yield ("done", _done_result(-1, "", session_id, error_events, ""))
                return

            log.info(f"{label}aishell-dsh ACP initialized")

            # ── 2. session/new（一期每任务新会话）──
            yield ("step_start", "Step 1 started (session: ACP)")
            result, err = client.request("session/new", {
                "cwd": cwd if cwd else ".",
                "mcpServers": [],
            }, timeout=100)
            if err:
                error_events.append(f"session/new failed: {err}")
                yield ("done", _done_result(-1, "", session_id, error_events, ""))
                return
            new_session_id = _extract_session_id(result) or ""
            log.info(f"{label}aishell-dsh ACP session: {new_session_id[:24]}...")

            # ── 3. 模型路由（踩坑①/②：默认 trial 无凭据必失败；value 从 options
            #    原样取，自拼报 -32602）──
            model_value = (env or {}).get("AISHELL_DSH_MODEL") or _select_model_option(result)
            if model_value:
                set_res, set_err = client.request("session/set_config_option", {
                    "sessionId": new_session_id,
                    "configId": "model",
                    "value": model_value,
                }, timeout=30)
                if set_err:
                    error_events.append(f"set_config_option(model) failed: {set_err}")
                    yield ("done", _done_result(-1, "", session_id, error_events, ""))
                    return
                log.info(f"{label}aishell-dsh ACP model set: {model_value[:60]}")

            # ── 4. session/prompt（fire-and-forget，PromptResponse 收口）──
            with client._lock:
                _prompt_id = client._next_id
                client._next_id += 1
            with client._lock:
                client._pending[_prompt_id] = threading.Event()

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

            threading.Thread(target=_send_prompt, daemon=True).start()

            # ── 5. 内循环（分级超时同 hwcloud_acp）──
            _last_activity = time.time()
            _global_start = time.time()
            _silent_src = (env or {}).get("RUNTIME_TOOL_SILENT_TIMEOUT") or \
                os.environ.get("RUNTIME_TOOL_SILENT_TIMEOUT", "900")
            try:
                _SILENT_TIMEOUT = float(_silent_src)
            except Exception:
                _SILENT_TIMEOUT = 900.0
            _TEXT_SILENT_TIMEOUT = 60
            _GLOBAL_TIMEOUT = _global_timeout_for(max_exec_seconds, env)
            _last_event_type = ""
            _timeout_secs = None

            while True:
                with queue_lock:
                    events = event_queue[:]
                    event_queue.clear()
                if events:
                    _last_activity = time.time()
                    for et, data in events:
                        _last_event_type = et
                        yield (et, data)

                if prompt_done.is_set():
                    break

                if reader_done.is_set():
                    if collected_text:
                        log.info(f"{label}aishell-dsh ACP reader EOF with text, assuming done")
                        prompt_result["stopReason"] = "end_turn"
                        break
                    error_events.append("aishell-dsh process exited before prompt completed")
                    break

                elapsed_silent = time.time() - _last_activity
                if _last_event_type == "text" and elapsed_silent > _TEXT_SILENT_TIMEOUT:
                    log.info(f"{label}aishell-dsh ACP silent after text, assuming done")
                    prompt_result["stopReason"] = "end_turn"
                    break
                if elapsed_silent > _SILENT_TIMEOUT:
                    log.warning(f"{label}aishell-dsh ACP silent {elapsed_silent:.0f}s, timeout")
                    prompt_result["stopReason"] = "timeout"
                    _timeout_secs = _SILENT_TIMEOUT
                    break
                if time.time() - _global_start > _GLOBAL_TIMEOUT:
                    log.warning(f"{label}aishell-dsh ACP global timeout ({_GLOBAL_TIMEOUT:.0f}s)")
                    prompt_result["stopReason"] = "timeout"
                    _timeout_secs = _GLOBAL_TIMEOUT
                    break

                time.sleep(0.05)

            # ── 6. 收尾 ──
            _flush_text()
            _flush_thinking()

            stop_reason = prompt_result.get("stopReason", "")
            log.info(f"{label}aishell-dsh ACP done: stopReason={stop_reason}")

            if stop_reason == "timeout" and not error_events:
                from src.agent.failure_attribution import timeout_error_text
                error_events.append(timeout_error_text(
                    _timeout_secs if _timeout_secs is not None else _GLOBAL_TIMEOUT,
                    "aishell-dsh ACP stopReason=timeout"))
            if stop_reason == "error" and not error_events:
                _pr = prompt_result.get("result") or {}
                if isinstance(_pr, dict) and isinstance(_pr.get("error"), dict):
                    _em = _pr["error"].get("message", str(_pr["error"]))[:300]
                    error_events.append(f"prompt error: {_em}")

            yield ("step_finish", f"Step 1 done (stopReason={stop_reason})")

            with queue_lock:
                if event_queue:
                    for et, data in event_queue[:]:
                        yield (et, data)
                    event_queue.clear()

            try:
                proc.stdin.close()
            except Exception:
                pass
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                _terminate_proc(proc, f"{label}aishell-dsh ACP")

            reader_thread.join(timeout=3)
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

            if stop_reason == "end_turn" and not error_events:
                exit_code = 0
            elif stop_reason in ("timeout", "error"):
                exit_code = -1
            elif error_events:
                exit_code = proc.returncode if proc.returncode is not None else -1
            else:
                exit_code = 0

            yield ("done", {
                "exit_code": exit_code,
                "text": "\n".join(collected_text),
                "oc_session_id": new_session_id,
                "errors": error_events,
                "stderr": stderr_output[:500],
                "backend": "aishell-dsh",
            })

        except Exception as e:
            log.error(f"{label}aishell-dsh ACP exception: {e}", exc_info=True)
            error_events.append(str(e))
            yield ("done", {
                "exit_code": -1, "text": "\n".join(collected_text),
                "oc_session_id": new_session_id,
                "errors": error_events, "stderr": "",
            })
        finally:
            try:
                proc.stdin.close()
            except Exception:
                pass
            if proc.poll() is None:
                _terminate_proc(proc, f"{label}aishell-dsh ACP")
            # 会话文件不清理：~/.aishell/sessions 自持久（二期 token 解析数据源）
            _res.dispose(kill_procs=False)


def _select_model_option(session_new_result) -> str:
    """从 session/new 的 configOptions 选模型路由（返回原样 option value；"" = 不干预）。

    规则（阶段 0 实测）：profile 已配好默认路由（currentValue 非 trial）→ 不干预；
    默认 trial（无凭据必失败）→ 自动选第一个非 trial 路由。
    """
    try:
        opts = (session_new_result or {}).get("configOptions") or []
    except Exception:
        return ""
    for o in opts:
        if not isinstance(o, dict) or o.get("id") != "model":
            continue
        cur = o.get("currentValue", "")
        if cur and cur != _TRIAL_MODEL_VALUE:
            return ""  # 设备 profile 已配默认路由，不干预
        flat = []
        for g in (o.get("options") or []):
            if isinstance(g, dict):
                for item in (g.get("options") or []):
                    if isinstance(item, dict) and item.get("value"):
                        flat.append(item["value"])
        for v in flat:
            if v != _TRIAL_MODEL_VALUE:
                return v
        return ""
    return ""


class _AishellACPClient(_ACPSimpleClient):
    """_ACPSimpleClient 的 aishell 适配：tool_call_update 输出在 content[] 内容块
    （hwcloud 在 rawOutput/output）；usage_update 已在父类忽略清单。"""

    def _handle_notification(self, raw: dict):
        method = raw.get("method", "")
        if method not in ("session/update", "session/notification"):
            return
        if not isinstance(raw.get("params"), dict):
            return
        update = raw["params"].get("update", raw["params"])
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

        if update_type_norm == "usage_update":
            return  # {used,size} 上下文占用，非计费——一期忽略（二期消费）

        if update_type_norm == "agent_message_chunk":
            text = update.get("content", {}).get("text", "")
            if text:
                self.on_message("text", text)
        elif update_type_norm == "agent_thought_chunk":
            text = update.get("content", {}).get("text", "")
            if text:
                self.on_message("thinking", text)
        elif update_type_norm == "tool_call":
            title = update.get("title", "")
            name = update.get("name", "")
            raw_input = update.get("rawInput") or update.get("input") or {}
            tool_name = _tool_name_from_title(title) or name
            if tool_name:
                self.on_message("tool_use", {"tool": tool_name, "input": raw_input})
        elif update_type_norm == "tool_call_update":
            status = update.get("status", "")
            title = update.get("title", update.get("name", ""))
            tool_name = _tool_name_from_title(title) or title
            if status in ("completed", "failed"):
                self.on_message("tool_finish", f"Tool {tool_name} {status}")
                output_text = _extract_tool_output(update)
                if output_text:
                    self.on_message("tool_result", output_text)


def _extract_tool_output(update: dict) -> str:
    """tool_call_update 输出提取：aishell 形如 content:[{type:'content',
    content:{type:'text',text:...}}]；兼容 hwcloud 的 rawOutput/output 形态。"""
    output = update.get("rawOutput") or update.get("output")
    if output:
        return output if isinstance(output, str) else json.dumps(output, ensure_ascii=False)[:500]
    blocks = update.get("content")
    if isinstance(blocks, list):
        parts = []
        for b in blocks:
            if isinstance(b, dict):
                inner = b.get("content")
                if isinstance(inner, dict) and inner.get("type") == "text":
                    parts.append(inner.get("text", ""))
                elif isinstance(b, dict) and b.get("type") == "text":
                    parts.append(b.get("text", ""))
        if parts:
            return "\n".join(parts)[:500]
    return ""
