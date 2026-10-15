"""Hwcloud ACP 后端：通过 JSON-RPC over stdin/stdout 与 hwcloud CLI 通信。

hwcloud CLI = openagent-go（https://github.com/yusheng-g/openagent-go）构建产物
（OPENAGENT_BINARY_NAME=hwcloud ./build.sh）。接入设计见
docs/hwcloud-runtime-acp-design.md（阶段 0 实测行为已回填）。

复刻 hermes_acp.py 的 ACP JSON-RPC 模式（结构化通信、事件队列、分级超时），
差异点均来自阶段 0 实测（docs/hwcloud-runtime-acp-design.md「实测行为」小节）：

1. 启动命令：`hwcloud serve --acp`（hermes 是 `hwcloud acp` 子命令）。
2. mcpServers 必须传 `[]`（空数组）；传 `{}` 报 -32602 unmarshal 错误。
3. session/resume 对不存在的 sessionId 不报错、静默重建空会话——不能靠 resume
   报错触发降级。一期策略：**每次 task 直接 session/new**，多轮上下文靠
   prompt_builder 注入前序结论（对齐 codearts 现状）；resume 能力保留给
   chat 场景二期启用。
4. 噪音事件忽略：available_commands_update / available_skills_update /
   mcp_servers_update / session_info_update / usage_update。
5. 免审：per-task settings.json 的 default_mode:"auto"（等效 hermes 的
   HERMES_YOLO_MODE=1）；request_permission 自动应答保留兜底。
6. 配置隔离：OPENAGENT_CLI_CONFIG 指向 per-task settings.json，
   整个工作集（profile/plugins/会话）随之隔离（对齐 codearts 方案 G 思路）。
   会话目录不纳入 6h 清理（保留 sqlite 供 resume 长期续接，已确认决策）。

ACP 协议流程（与 hermes 同构）：
  1. 启动 `hwcloud serve --acp` 子进程
  2. 发 initialize → 握手
  3. 发 session/new → 获取 sessionId
  4. 发 session/prompt → 等 PromptResponse（stopReason）
  5. 期间收 session/update 通知（agent_message_chunk / tool_call / tool_call_update /
     agent_thought_chunk）
  6. 处理 session/request_permission（自动批准 allow_once）
  7. stdin EOF → hwcloud 退出

通信格式：每行一个 JSON-RPC 消息（NDJSON over stdin/stdout）
"""
import os
import json
import hashlib
import time
import threading
import subprocess
import shutil
from typing import Generator
from src.config import log


def _global_timeout_for(max_exec_seconds: float) -> float:
    """hwcloud ACP 内循环全局墙钟上限（#282 对齐，与 hermes_acp 同一配置源）。

    取 RUNTIME_TASK_TIMEOUT 环境变量（与 src/runtime/agent.py 任务级看门狗
    `_env_float("RUNTIME_TASK_TIMEOUT", 3600)` 同源、同默认值），下限保护
    max_exec_seconds。
    """
    try:
        _v = float(os.environ.get("RUNTIME_TASK_TIMEOUT", "3600"))
    except Exception:
        _v = 3600.0
    return max(_v, float(max_exec_seconds))


# ── per-task settings.json 模板 ──
# provider 段由 get_env 调用方（executor _stream_hwcloud_acp）通过 env 注入
# HWCLOUD_PROVIDER_BASE_URL / HWCLOUD_PROVIDER_API_KEY / HWCLOUD_PROVIDER_MODELS
# （来源：用户云凭据 provider=opengw）。目录权限 0700、文件 0600（含明文 api_key）。

_PROVIDER_NAME = "opengw"


def _build_task_settings(iso_home: str) -> str:
    """在 per-task 目录写 settings.json（opengw provider + default_mode=auto）。

    模型配置优先读环境变量（env_extra 可按 task 覆盖）；缺省回退全局
    ~/.hwcloud/settings.json 已有内容（若存在则整文件复制，仅覆写 default_mode）。

    返回 ""（空串）表示两层配置都无 provider（env 未注入且全局 settings.json
    不存在/无 provider 段）——此时调用方不应设置 OPENAGENT_CLI_CONFIG，让
    hwcloud 用自己的默认配置链（~/.hwcloud 或 devspace 预置，如华为云 MaaS）。
    seq#561 教训：强制重定位到空配置会报 "no model configured"，把设备本地
    能用的配置（直接对话正常、探活正常——探活本就不设 OPENAGENT_CLI_CONFIG）
    屏蔽掉。codearts/hermes/opencode 均为"设备本地自给配置"模式，hwcloud
    对齐此语义。
    """
    settings_path = os.path.join(iso_home, "settings.json")

    base_url = os.environ.get("HWCLOUD_PROVIDER_BASE_URL", "")
    api_key = os.environ.get("HWCLOUD_PROVIDER_API_KEY", "")
    models_raw = os.environ.get("HWCLOUD_PROVIDER_MODELS", "")
    models = [m.strip() for m in models_raw.split(",") if m.strip()] if models_raw else []

    cfg: dict = {}
    # 回退：复制全局 settings.json 的 provider 段（worker 机器预配置）
    global_cfg_path = os.path.join(os.path.expanduser("~"), ".hwcloud", "settings.json")
    if os.path.isfile(global_cfg_path):
        try:
            with open(global_cfg_path, "r", encoding="utf-8") as f:
                cfg = json.load(f)
        except Exception:
            cfg = {}

    provider = dict(cfg.get("provider", {}))
    if base_url or api_key or models:
        p = dict(provider.get(_PROVIDER_NAME, {}))
        if base_url:
            p["base_url"] = base_url
        if api_key:
            p["api_key"] = api_key
        if models:
            p["models"] = models
        provider[_PROVIDER_NAME] = p
    # 无任何 provider 配置 → 返回空串，调用方不重定位配置（见 docstring）
    if not provider:
        return ""

    cfg["provider"] = provider
    # 免审（阶段 0 实测确认：default_mode=auto 全程无 request_permission）
    cfg["default_mode"] = "auto"

    os.makedirs(iso_home, exist_ok=True)
    with open(settings_path, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)
    os.chmod(settings_path, 0o600)
    try:
        os.chmod(iso_home, 0o700)
    except Exception:
        pass
    return settings_path


def _task_home_root() -> str:
    """per-task hwcloud 数据目录根（~/.cache/hwcloud-acp-homes/，按 cwd-hash 分片）。

    cwd-hash 稳定路径（非随机临时目录）：会话 sqlite 持久化，同仓库下次执行
    session/resume 可续接（对齐 hermes_acp HERMES_HOME overlay 同一决策）。
    不纳入 6h 清理（已确认决策 2026-10-12）。
    """
    root = os.path.join(os.path.expanduser("~"), ".cache", "hwcloud-acp-homes")
    os.makedirs(root, exist_ok=True)
    return root


class HwcloudACPBackend:
    """hwcloud ACP 后端 — 不走 build_args/parse_output，直接 stream_execute yield 事件。"""
    name = "hwcloud"
    binary = "hwcloud"
    prompt_via_stdin = False  # ACP 模式不用 stdin 传 prompt，用 JSON-RPC

    def get_env(self, account: str = "", user_id: int = 0) -> dict:
        env = os.environ.copy()
        from src.config import SKILLS_DIR
        env["DEVOPS_SKILLS_DIR"] = SKILLS_DIR
        env["DEVOPS_EVENT_TYPE"] = ""
        env["NO_COLOR"] = "1"
        env["TERM"] = "dumb"
        # provider 凭据经 env_extra 按 task 注入（_build_task_settings 读取），
        # get_env 不预置——避免多任务共享进程级 env 串扰。
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

        事件类型与 stream_agent / hermes_acp 兼容：
        - ("step_start", str)
        - ("text", str)
        - ("reasoning", str)
        - ("tool_use", {"tool": str, "input": dict})
        - ("tool_finish", str)
        - ("tool_result", str)
        - ("step_finish", str)
        - ("done", {"exit_code": int, "text": str, "oc_session_id": str, "errors": list, "stderr": str})

        session_id 一期不使用（resume 不校验存在性，静默空会话有丢上下文风险，
        见设计文档实测行为 §5）：每次 task 直接 session/new。
        """
        label = f"{agent_label} " if agent_label else ""
        agent_cmd = shutil.which(self.binary) or shutil.which(self.binary + ".cmd")
        if not agent_cmd:
            log.error(f"{label}hwcloud ACP CLI not found in PATH")
            yield ("done", {
                "exit_code": -1, "text": "", "oc_session_id": session_id,
                "errors": ["hwcloud not found"], "stderr": ""
            })
            return

        # ── per-task 数据目录（OPENAGENT_CLI_CONFIG 重定位整个工作集）──
        _cwd_hash = hashlib.md5((cwd or ".").encode()).hexdigest()[:12]
        iso_home = os.path.join(_task_home_root(), _cwd_hash)
        # 合并调用方注入的 provider env（_build_task_settings 读的是 os.environ，
        # 这里把 env(dict) 中的值临时注入——避免永久污染 worker 进程环境）
        _provider_env_keys = ("HWCLOUD_PROVIDER_BASE_URL", "HWCLOUD_PROVIDER_API_KEY",
                              "HWCLOUD_PROVIDER_MODELS")
        _saved_env = {}
        try:
            for k in _provider_env_keys:
                if env and env.get(k):
                    _saved_env[k] = os.environ.get(k)
                    os.environ[k] = env[k]
            settings_path = _build_task_settings(iso_home)
        finally:
            for k, v in _saved_env.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v

        proc_env = dict(env) if env else os.environ.copy()
        if settings_path:
            # 有 grape 侧配置（env 注入或全局回退）→ per-task 重定位整个工作集
            proc_env["OPENAGENT_CLI_CONFIG"] = settings_path
        # else: 无任何 provider 配置 → 不重定位，hwcloud 用自己的默认配置链
        # （devspace 等设备自带配置场景；会话落设备默认位置，放弃 per-task
        # 会话隔离——功能优先于隔离，见 _build_task_settings docstring）

        log.info(f"{label}hwcloud ACP starting: settings={settings_path or '(device default)'} cwd={cwd}")

        # ── 启动 hwcloud serve --acp 子进程 ──
        _popen_kwargs = {}
        if hasattr(os, "getpgid"):
            _popen_kwargs["start_new_session"] = True

        # 文件系统隔离（GRAPE_AGENT_SANDBOX 开 + bwrap 可用时套白名单锁）
        _acp_cmd = [agent_cmd, "serve", "--acp"]
        try:
            from src.agent.sandbox import build_bwrap_cmd
            _wrap = build_bwrap_cmd(cwd, backend_name="hwcloud")
            if _wrap:
                _acp_cmd = _wrap + _acp_cmd
                log.info(f"{label}hwcloud ACP sandboxed via bwrap (cwd={cwd})")
        except Exception as _bw_e:
            log.warning(f"{label}hwcloud ACP bwrap wrap failed, running unsandboxed: {_bw_e}")

        proc = subprocess.Popen(
            _acp_cmd,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            cwd=cwd, env=proc_env, bufsize=0, **_popen_kwargs
        )
        assert proc.stdin is not None and proc.stdout is not None and proc.stderr is not None

        # 注册到全局进程表（供 shutdown 时终止）—— 延迟导入避免循环引用
        try:
            from src.agent.executor import _track_proc, _untrack_proc, _terminate_proc
        except ImportError:
            _track_proc = _untrack_proc = lambda p: None
            _terminate_proc = lambda p, l: None

        # TaskResourceStack: unified resource lifecycle (effect/dispose pattern)
        from src.runtime.resource_stack import TaskResourceStack
        _res = TaskResourceStack(task_id="", cwd=cwd)
        _res.track_proc(proc)

        collected_text = []
        new_session_id = session_id
        error_events = []

        # chat 任务完成检测参数（与 hermes_acp 同参）：text 稳定 3s + 空闲 5s
        _CHAT_STABLE_SECONDS = 3
        _CHAT_IDLE_SECONDS = 5
        _chat_first_text_time = 0.0
        _chat_last_text_time = 0.0
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

        # 文本缓冲区：ACP 每次只发几个 token 的 chunk，缓冲到换行或一定长度再 flush
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
                    if _is_chat_task and collected_text and not prompt_done.is_set():
                        nonlocal _chat_first_text_time, _chat_last_text_time
                        if _chat_first_text_time == 0:
                            _chat_first_text_time = time.time()
                        _chat_last_text_time = time.time()
                        _chat_stable = (time.time() - _chat_first_text_time) >= _CHAT_STABLE_SECONDS
                        _chat_idle = (time.time() - _chat_last_text_time) >= _CHAT_IDLE_SECONDS
                        if _chat_stable and _chat_idle:
                            log.info(f"{label}hwcloud ACP chat task text stable for {_CHAT_IDLE_SECONDS}s, assuming done")
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

        # ── reader 线程：非阻塞读 stdout（hermes acp 实测一次性 flush 多行，
        #    readline 只读第一行就阻塞；hwcloud 同样按行 NDJSON，沿用 select 方案）──
        import select as _select
        _fd = proc.stdout.fileno()
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
                            log.warning(f"{label}hwcloud ACP handle_line error: {e}")
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
                        _read_buf += chunk
                        while b"\n" in _read_buf:
                            line, _read_buf = _read_buf.split(b"\n", 1)
                            line_str = line.decode("utf-8", errors="replace").strip()
                            if not line_str:
                                continue
                            try:
                                client.handle_line(line_str)
                            except Exception as e:
                                log.warning(f"{label}hwcloud ACP handle_line error: {e}")
            except Exception as e:
                log.warning(f"{label}hwcloud ACP reader exception: {e}")
            finally:
                reader_done.set()
                log.info(f"{label}hwcloud ACP reader EOF")

        reader_thread = threading.Thread(target=_reader, daemon=True)
        reader_thread.start()
        _track_proc(proc)

        # stderr 收集线程（截断保留 500 字符）
        _stderr_chunks = []

        def _stderr_reader():
            try:
                _stderr = proc.stderr
                while True:
                    chunk = _stderr.read(4096)
                    if not chunk:
                        break
                    _stderr_chunks.append(chunk.decode("utf-8", errors="replace"))
            except Exception:
                pass

        threading.Thread(target=_stderr_reader, daemon=True).start()

        try:
            # ── 1. initialize 握手 ──
            init_result, init_err = client.request("initialize", {
                "protocolVersion": 1,
                "clientInfo": {"name": "grape-agent", "version": "0.1.0"},
                # 声明 fs 能力：openagent OnInitialize 按 clientCapabilities gate
                # Agent→Client RPC 工具注册（fs/read_text_file 等，server.go:1274——
                # 不声明则这些工具被裁掉，LLM 拿不到；terminal 不声明，grape 无法
                # 承载交互终端，agent 用自带 shell 工具）。
                # _handle_agent_request 已实现这两个方法的真实读写。
                "clientCapabilities": {
                    "fs": {"readTextFile": True, "writeTextFile": True},
                },
            }, timeout=100)
            if init_err:
                error_events.append(f"initialize failed: {init_err}")
                yield ("done", _done_result(-1, "", session_id, error_events, ""))
                return

            log.info(f"{label}hwcloud ACP initialized")

            # ── 2. session/new（一期：不 resume——resume 不校验存在性，
            #    静默空会话有丢上下文风险，见设计文档实测行为 §5）──
            yield ("step_start", "Step 1 started (session: ACP)")
            cwd_arg = cwd if cwd else "."
            result, err = client.request("session/new", {
                "cwd": cwd_arg,
                # 阶段 0 实测：mcpServers 必须空数组 []，传 {} 报 -32602
                "mcpServers": [],
            }, timeout=100)
            if err:
                error_events.append(f"session/new failed: {err}")
                yield ("done", _done_result(-1, "", session_id, error_events, ""))
                return
            new_session_id = _extract_session_id(result) or ""
            log.info(f"{label}hwcloud ACP session: {new_session_id[:24]}...")
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
            # 工具静默超时（RUNTIME_TOOL_SILENT_TIMEOUT env 可调，默认 900s）：
            # hwcloud 的 shell 工具是长阻塞单工具（apt/pip 装 playwright+chromium
            # 数百 MB 全程无 ACP 事件），300s 会在正常安装中途硬杀（seq#554 实测：
            # 12:00:33 发起安装 → 12:05:33 恰 300s 判 timeout，任务 failed）。
            # 静默超时只防 agent 挂死——全局上限已由 _GLOBAL_TIMEOUT
            # （RUNTIME_TASK_TIMEOUT，默认 3600s）兜底，单工具静默放宽不放大总时长。
            _silent_src = (env or {}).get("RUNTIME_TOOL_SILENT_TIMEOUT") or \
                os.environ.get("RUNTIME_TOOL_SILENT_TIMEOUT", "900")
            try:
                _SILENT_TIMEOUT = float(_silent_src)
            except Exception:
                _SILENT_TIMEOUT = 900.0
            _TEXT_SILENT_TIMEOUT = 60  # text 输出后 60s 无后续 → 视为完成（响应丢失兜底）
            _GLOBAL_TIMEOUT = _global_timeout_for(max_exec_seconds)
            _last_event_type = ""   # 记录最后一次事件类型

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

                # hwcloud 进程退出但没发 PromptResponse → 兼容性兜底
                if reader_done.is_set() and not prompt_done.is_set():
                    if collected_text:
                        log.info(f"{label}hwcloud ACP reader EOF with {len(collected_text)} text chunks, assuming done")
                        prompt_result["stopReason"] = "end_turn"
                        break
                    error_events.append("hwcloud process exited before prompt completed")
                    break

                # 分级超时：text 后 60s → 视为完成（PromptResponse 丢失）；
                #   tool 后 300s → 超时终止
                elapsed_silent = time.time() - _last_activity
                if _last_event_type == "text" and elapsed_silent > _TEXT_SILENT_TIMEOUT:
                    log.info(f"{label}hwcloud ACP silent {elapsed_silent:.0f}s after text, assuming done (response lost)")
                    prompt_result["stopReason"] = "end_turn"
                    break
                if elapsed_silent > _SILENT_TIMEOUT:
                    log.warning(f"{label}hwcloud ACP silent {elapsed_silent:.0f}s, timeout")
                    prompt_result["stopReason"] = "timeout"
                    break

                # 全局超时（#282：对齐外层 RUNTIME_TASK_TIMEOUT，默认 3600s）
                if time.time() - _global_start > _GLOBAL_TIMEOUT:
                    _gt_src = os.environ.get("RUNTIME_TASK_TIMEOUT")
                    _gt_src_txt = f"env RUNTIME_TASK_TIMEOUT={_gt_src}" if _gt_src else "default 3600s"
                    log.warning(f"{label}hwcloud ACP global timeout ({_GLOBAL_TIMEOUT:.0f}s, {_gt_src_txt}) exceeded")
                    prompt_result["stopReason"] = "timeout"
                    break

                time.sleep(0.05)

            # ── 5. 收尾 ──
            _flush_text()
            _flush_thinking()

            stop_reason = prompt_result.get("stopReason", "")
            log.info(f"{label}hwcloud ACP done: stopReason={stop_reason}")

            # 信息透传修复（seq#559 排障盲点）：stopReason=error 时把 PromptResponse
            # 里的 error 详情补进 error_events——旧实现只置 stopReason 不带消息，
            # error_events 为空 → done.errors 空 → worker fail 上报只剩
            # "engine exit_code=-1, errors: "（空壳），真实原因（如旧版二进制
            # 协议不兼容的 JSON-RPC error message）无法到达服务端，排障只能登机器。
            if stop_reason == "error" and not error_events:
                _pr = prompt_result.get("result") or {}
                if isinstance(_pr, dict) and isinstance(_pr.get("error"), dict):
                    _em = _pr["error"].get("message", str(_pr["error"]))[:300]
                    error_events.append(f"prompt error: {_em}")

            yield ("step_finish", f"Step 1 done (stopReason={stop_reason})")

            # 消费残留事件
            with queue_lock:
                if event_queue:
                    for et, data in event_queue[:]:
                        yield (et, data)
                    event_queue.clear()

            # 关闭 stdin 让 hwcloud 退出
            try:
                proc.stdin.close()
            except Exception:
                pass

            # 等待进程退出（grace 5s）
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                _terminate_proc(proc, f"{label}hwcloud ACP")

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
            elif stop_reason == "timeout":
                exit_code = -1
            elif stop_reason == "error":
                exit_code = -1
            elif error_events:
                exit_code = proc.returncode if proc.returncode is not None else -1
            else:
                exit_code = 0

            full_text = "\n".join(collected_text)

            yield ("done", {
                "exit_code": exit_code,
                "text": full_text,
                "oc_session_id": new_session_id,
                "errors": error_events,
                "stderr": stderr_output[:500],
                "backend": "hwcloud",
            })

        except Exception as e:
            log.error(f"{label}hwcloud ACP exception: {e}", exc_info=True)
            error_events.append(str(e))
            yield ("done", {
                "exit_code": -1, "text": "\n".join(collected_text),
                "oc_session_id": new_session_id,
                "errors": error_events, "stderr": "",
            })
        finally:
            # Unified cleanup via TaskResourceStack
            try:
                proc.stdin.close()
            except Exception:
                pass
            if proc.poll() is None:
                _terminate_proc(proc, f"{label}hwcloud ACP")
            # iso_home 不清理：会话 sqlite 供 session/resume 长期续接（已确认决策），
            # settings.json 含明文 api_key 但目录已 0700。
            _res.dispose(kill_procs=False)


# ══════════════════════════════════════════════════════════════
# ACP JSON-RPC 客户端（复制自 hermes_acp._ACPSimpleClient，适配 hwcloud 差异）
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
        # 构造后再赋 client.on_prompt_done = ... 时快照仍是 no-op（hermes_acp 的
        # 潜伏 bug，其生产靠 60s text 静默超时兜底硬撑）。调用点直接读公开属性。

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
        """处理从 hwcloud stdout 收到的一行 JSON-RPC 消息。"""
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
                        log.info(f"{self.label}hwcloud ACP prompt response without stopReason, treating as done (id={req_id})")
                        self.on_prompt_done({"stopReason": "end_turn", "result": result})
                else:
                    self.on_prompt_done({"stopReason": "end_turn", "result": result or {}})

        evt.set()

    def _write_response(self, resp: dict):
        """Agent→Client 请求的应答统一写出（NDJSON 一行）。"""
        data = (json.dumps(resp) + "\n").encode("utf-8")
        try:
            self.write_line(data)
        except Exception as e:
            log.warning(f"{self.label}hwcloud ACP write response error: {e}")

    def _handle_agent_request(self, raw: dict):
        """处理 agent → client 的 JSON-RPC 请求（如 session/request_permission）。"""
        method = raw.get("method", "")
        req_id = raw["id"]

        if method == "fs/read_text_file":
            # Agent→Client RPC：agent 读客户端侧文件（Agent→Client tools 的底层调用，
            # initialize 已声明 fs.readTextFile 能力）。grape 环境本就有全盘文件访问。
            params = raw.get("params") or {}
            path = params.get("path") or ""
            try:
                with open(path, "r", encoding="utf-8", errors="replace") as f:
                    content = f.read()
                resp = {"jsonrpc": "2.0", "id": req_id, "result": {"content": content}}
            except Exception as e:
                resp = {"jsonrpc": "2.0", "id": req_id,
                        "error": {"code": -32000, "message": f"read {path}: {e}"}}
            self._write_response(resp)
            return

        if method == "fs/write_text_file":
            # Agent→Client RPC：agent 写客户端侧文件（initialize 已声明 fs.writeTextFile）。
            params = raw.get("params") or {}
            path = params.get("path") or ""
            content = params.get("content") or ""
            try:
                _pd = os.path.dirname(path)
                if _pd:
                    os.makedirs(_pd, exist_ok=True)
                with open(path, "w", encoding="utf-8") as f:
                    f.write(content)
                resp = {"jsonrpc": "2.0", "id": req_id, "result": {}}
            except Exception as e:
                resp = {"jsonrpc": "2.0", "id": req_id,
                        "error": {"code": -32000, "message": f"write {path}: {e}"}}
            self._write_response(resp)
            return

        if method == "session/request_permission":
            # 自动批准 allow_once（兜底：default_mode=auto 下通常不触发，
            # 会话级 mode 覆盖或策略降级时触发）
            params = raw.get("params")
            if not isinstance(params, dict):
                params = {}
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

    def _handle_notification(self, raw: dict):
        """处理 session/update 通知（agent 消息、工具调用等）。"""
        method = raw.get("method", "")
        if method not in ("session/update", "session/notification"):
            return
        if not isinstance(raw.get("params"), dict):
            return
        update = raw["params"].get("update", raw["params"])
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

        # hwcloud 噪音事件忽略（阶段 0 实测：新会话首轮必发，
        # available_*/mcp_servers/session_info/usage_update 不进 grape 事件流）
        if update_type_norm in _IGNORED_UPDATE_TYPES:
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
        elif update_type_norm == "plan_update":
            # hwcloud 新增：计划条目更新，映射 text 透传（一期不单独建事件类型）
            entries = update.get("entries", [])
            if isinstance(entries, list):
                titles = []
                for e in entries:
                    if isinstance(e, dict):
                        titles.append(e.get("title", e.get("content", "")) or "")
                line = "[plan] " + " | ".join(t for t in titles if t)
                if line.strip() != "[plan]":
                    self.on_message("text", line)
        elif update_type_norm == "turn_end":
            # 提取 stopReason
            stop_reason = update.get("stopReason", "")
            self.on_prompt_done({"stopReason": stop_reason, "result": update})


# ══════════════════════════════════════════════════════════════
# 辅助函数
# ══════════════════════════════════════════════════════════════

# hwcloud 噪音事件（阶段 0 实测）：available_*/mcp_servers/session_info/usage
_IGNORED_UPDATE_TYPES = {
    "available_commands_update",
    "available_skills_update",
    "mcp_servers_update",
    "session_info_update",
    "usage_update",
}

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
    # hwcloud session/new 响应：顶层 sessionId（实测 acp_<ns>_N）
    sid = result.get("sessionId") or result.get("session_id")
    if sid:
        return sid
    return ""


def _done_result(exit_code, text, session_id, errors, stderr) -> dict:
    return {
        "exit_code": exit_code, "text": text,
        "oc_session_id": session_id, "errors": errors, "stderr": stderr,
    }
