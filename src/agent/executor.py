"""Agent 执行器：流式子进程调度 (NDJSON)，不绑定具体后端。"""
import os, subprocess, json, shutil, time, signal
from typing import Generator, AsyncGenerator
from src.config import log
from .base import AgentBackend
from .opencode import OpencodeBackend
from .codearts import CodeArtsBackend
from .hermes import HermesBackend


_BACKENDS = {
    "opencode": OpencodeBackend(),
    "codearts": CodeArtsBackend(),
    "hermes": HermesBackend(),
}

# 运行中的 agent 子进程注册表：供服务停机时统一终止，避免 uvicorn 优雅停机
# 等待 executor（opencode 子进程）超时被 systemd SIGKILL（grape-main timeout 重启根因，2026-08-18）。
import threading as _threading
_active_procs: set[subprocess.Popen] = set()
_active_procs_lock = _threading.Lock()


def _track_proc(proc: subprocess.Popen):
    with _active_procs_lock:
        _active_procs.add(proc)


def _untrack_proc(proc: subprocess.Popen):
    with _active_procs_lock:
        _active_procs.discard(proc)


def terminate_active_agents(grace_seconds: int = 5):
    """服务停机时终止所有运行中的 agent 子进程（opencode 等）。

    子进程被杀 → stdout 管道 EOF → executor 线程的 reader/主循环 break → 线程结束，
    uvicorn `shutdown_default_executor()` 不再等待 → 服务快速退出，避免 stop-sigterm 超时。
    """
    with _active_procs_lock:
        procs = list(_active_procs)
        _active_procs.clear()
    for proc in procs:
        try:
            _terminate_proc(proc, "shutdown")
        except Exception:
            pass
    if procs:
        log.info(f"terminate_active_agents: terminated {len(procs)} running agent subprocess(es)")



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


def _stream_hermes_acp(prompt, cwd, session_id, event_type, backend,
                       account, user_id, env_extra, bot_token, user_token,
                       agent_name, agent_label, max_exec_seconds) -> Generator:
    """hermes ACP 模式入口：构建 env → 调 hermes_acp.HermesACPBackend.stream_execute。

    与 stream_agent 的 opencode/codearts 路径并行，不走 build_args/parse_output。
    """
    from src.agent.hermes_acp import HermesACPBackend

    acp = HermesACPBackend()
    env = acp.get_env(account=account, user_id=user_id)
    env["DEVOPS_EVENT_TYPE"] = event_type

    # GitCode token 互斥注入（与 stream_agent 一致）：
    #   chat → 只 USER_GITCODE_TOKEN；事件任务 → 只 BOT_GITCODE_TOKEN（不同时注入）
    if event_type == "chat":
        if not user_token and user_id > 0:
            try:
                from src.user.db import get_user_gitcode_token
                user_token = get_user_gitcode_token(user_id) or ""
            except Exception as e:
                log.warning(f"Fallback resolve user gitcode token failed: {e}")
                user_token = ""
        if user_token:
            env["USER_GITCODE_TOKEN"] = user_token
        else:
            if bot_token:
                log.error(f"{label}chat task lacks user token but bot_token provided: "
                          f"will NOT impersonate user with bot token; GitCode API 401 for user write ops")
            else:
                log.warning(f"{label}NO token available for chat agent, GitCode API will 401")
    elif bot_token:
        env["BOT_GITCODE_TOKEN"] = bot_token
        log.info(f"{label}devops event task: using bot token (len={len(bot_token)})")
    if agent_name:
        env["AGENT_NAME"] = agent_name
    if env_extra:
        env.update(env_extra)

    # 技能路径占位符替换（direct 模式：按 skills/<agent_type>/<skill>/ 结构解析）
    try:
        from src.config import replace_skill_dir_placeholders
        prompt = replace_skill_dir_placeholders(prompt)
    except Exception:
        pass

    # 注入用户凭据
    if user_id > 0:
        try:
            from src.user.db import get_credentials
            for c in get_credentials(user_id):
                provider = c.get("provider", "")
                cfg = c.get("config", {})
                if provider == "huawei_cloud":
                    if cfg.get("ak"): env["HUAWEICLOUD_SDK_AK"] = str(cfg["ak"])
                    if cfg.get("sk"): env["HUAWEICLOUD_SDK_SK"] = str(cfg["sk"])
                elif provider == "codearts":
                    if cfg.get("ak"): env["CODEARTS_CLI_AK"] = str(cfg["ak"])
                    if cfg.get("sk"): env["CODEARTS_CLI_SK"] = str(cfg["sk"])
                else:
                    for k, v in cfg.items():
                        env[k.upper()] = str(v)
        except Exception as e:
            log.warning(f"Failed to inject user credentials: {e}")

    # 脱敏 token 列表
    _tokens = [t for t in (bot_token, user_token) if t]

    def _redact_text(text):
        if not text or not _tokens:
            return text
        s = str(text)
        for t in _tokens:
            if t:
                s = s.replace(t, "[REDACTED]")
        return s

    label = f"{agent_label} " if agent_label else ""

    # 会话失效重试（与 stream_agent 一致）
    for evt_type, data in acp.stream_execute(
        prompt, cwd, session_id=session_id, env=env,
        max_exec_seconds=max_exec_seconds, agent_label=label,
    ):
        if evt_type == "text":
            yield ("text", _redact_text(data))
        elif evt_type == "tool_use":
            if isinstance(data, dict):
                data["input"] = _redact_text(json.dumps(data.get("input", {}), ensure_ascii=False, default=str))
            yield ("tool_use", data)
        elif evt_type == "tool_result":
            yield ("tool_result", _redact_text(data))
        elif evt_type == "done":
            if isinstance(data, dict):
                data["text"] = _redact_text(data.get("text", ""))
                data["stderr"] = _redact_text(data.get("stderr", ""))
            yield ("done", data)
        else:
            yield (evt_type, _redact_text(data) if isinstance(data, str) else data)


def stream_agent(prompt: str, cwd: str, session_id: str = "",
                 event_type: str = "", backend_name: str = "",
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

    **hermes ACP 模式**：hermes 后端走 ACP JSON-RPC 协议（hermes_acp.py），
    不走旧的 build_args/parse_output CLI 文本解析。ACP 模式下 hermes 内部
    线程模型更简单，不触发 CLI 模式的 futex 死锁。
    """
    label = f"{agent_label} " if agent_label else ""
    backend = get_backend(backend_name)

    # ── hermes ACP 专用路径 ──
    if backend_name == "hermes":
        yield from _stream_hermes_acp(
            prompt, cwd, session_id, event_type, backend,
            account, user_id, env_extra, bot_token, user_token,
            agent_name, agent_label, max_exec_seconds,
        )
        return

    env = backend.get_env(account=account, user_id=user_id)
    env["DEVOPS_EVENT_TYPE"] = event_type
    # GitCode token 互斥注入（§十 写操作隔离）：
    #   chat（用户身份）→ 只注入 USER_GITCODE_TOKEN；事件任务（仓库身份）→ 只注入 BOT_GITCODE_TOKEN。
    #   BOT_GITCODE_TOKEN 与 USER_GITCODE_TOKEN 不同时注入，避免用户对话上下文暴露仓库 bot token。
    #   判定严格以 event_type 为准（不依赖 user_token 兜底判断，防调用方误传导致误判 chat）。
    if event_type == "chat":
        # chat：仅用户 token（DB 兜底；若仍无则视为配置异常，不以 bot token 冒充用户身份）
        if not user_token and user_id > 0:
            try:
                from src.user.db import get_user_gitcode_token
                user_token = get_user_gitcode_token(user_id) or ""
            except Exception as e:
                log.warning(f"Fallback resolve user gitcode token failed: {e}")
                user_token = ""
        if user_token:
            env["USER_GITCODE_TOKEN"] = user_token
        else:
            if bot_token:
                log.error(f"{label}chat task lacks user token but bot_token provided: "
                          f"will NOT impersonate user with bot token; GitCode API 401 for user write ops")
            else:
                log.warning(f"{label}NO token available for chat agent, GitCode API will 401")
    elif bot_token:
        # devops 事件任务：只用仓库 bot token（prompt 只认 BOT_GITCODE_TOKEN），不注入 USER_GITCODE_TOKEN
        env["BOT_GITCODE_TOKEN"] = bot_token
        log.info(f"{label}devops event task: user token not applicable, using bot token (len={len(bot_token)})")
    # Agent 显示名（skill 中 [$AGENT_NAME] 使用，如 "Issue Agent" / "QA Agent"）
    if agent_name:
        env["AGENT_NAME"] = agent_name
    if env_extra:
        env.update(env_extra)

    # 技能路径占位符：worker 侧已在物化时把 {{SKILL_DIR}} 替换为随任务下发的目录；
    # direct（服务端本地）执行在此按 skills/<agent_type>/<skill>/ 结构解析，使 read tool 能找到技能文件。
    try:
        from src.config import replace_skill_dir_placeholders
        prompt = replace_skill_dir_placeholders(prompt)
    except Exception:
        pass

    # 注入用户凭据
    if user_id > 0:
        try:
            from src.user.db import get_credentials
            for c in get_credentials(user_id):
                provider = c.get("provider", "")
                cfg = c.get("config", {})
                if provider == "huawei_cloud":
                    if cfg.get("ak"): env["HUAWEICLOUD_SDK_AK"] = str(cfg["ak"])
                    if cfg.get("sk"): env["HUAWEICLOUD_SDK_SK"] = str(cfg["sk"])
                elif provider == "codearts":
                    if cfg.get("ak"): env["CODEARTS_CLI_AK"] = str(cfg["ak"])
                    if cfg.get("sk"): env["CODEARTS_CLI_SK"] = str(cfg["sk"])
                else:
                    for k, v in cfg.items():
                        env[k.upper()] = str(v)
        except Exception as e:
            log.warning(f"Failed to inject user credentials: {e}")

    # 需要脱敏的 token 列表（用于日志/事件输出）
    _tokens = [t for t in (bot_token, user_token) if t]

    # codearts 方案E2: serve 启动移到 _run_once 内部（每次执行启动自己的 serve，避免单例并发串号）
    # pre_exec/post_exec 由 _run_once 内部调用，serve 信息用局部变量

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
        # codearts 方案E2: 每次执行启动独立的 codearts serve
        # serve 信息存在局部变量 _serve_port/_serve_proc 中，不写到单例 backend 上
        # build_args 从 backend._serve_port 读取（同一 _run_once 内设置，串行无并发问题）
        _serve_port = 0
        _serve_proc = None
        if hasattr(backend, "pre_exec") and callable(backend.pre_exec):
            try:
                serve_info = backend.pre_exec(cwd=cwd, env=dict(env))
                _serve_port = serve_info.get("port", 0)
                _serve_proc = serve_info.get("proc")
                _serve_env = serve_info.get("env", env)
                # 临时存 port 供 build_args 读取（同一线程内，_run_once 串行执行无并发）
                backend._serve_port = _serve_port
            except Exception as _pe_err:
                # serve 启动失败（codearts 二进制缺失/配置错误/30s 超时）：
                # 不能让异常传播到 _stream_runtime_async 线程（会导致 done=None →
                # 上层误报"task timed out after 1800s"，掩盖真实根因）。
                # yield 明确的 done event，把错误信息传回。
                log.error(f"{label}{backend.name} pre_exec failed: {_pe_err}")
                yield ("done", {
                    "exit_code": -1, "text": "", "oc_session_id": use_session_id,
                    "errors": [f"{backend.name} engine startup failed: {_pe_err}"],
                    "stderr": str(_pe_err)[:500],
                })
                return
        else:
            _serve_env = env

        args = backend.build_args(prompt, use_session_id)
        log.info(f"{label}{backend.name} args: {' '.join(args)[:2000]}...")
        log.info(f"{label}{backend.name} cwd: {cwd}")

        stdin_pipe = subprocess.PIPE if backend.prompt_via_stdin else subprocess.DEVNULL
        _popen_kwargs = {}
        if hasattr(os, "getpgid"):  # POSIX：独立进程组，便于整组优雅终止
            _popen_kwargs["start_new_session"] = True

        # hermes 引擎：每个子进程隔离 HERMES_HOME → 独立 state.db，避免并发争锁
        # （多 hermes chat -q 子进程共享 ~/.hermes/state.db 时 SQLite 写锁互斥阻塞）
        # 必须把 config.yaml / .env 复制过去，否则 hermes 在空目录里找不到 provider/key
        _proc_env = _serve_env  # codearts 方案E2: 用 pre_exec 返回的 env（含 CODEARTS_SERVER_PASSWORD）
        _iso_home = ""
        if backend.name == "hermes":
            import tempfile, shutil as _sh
            _iso_home = tempfile.mkdtemp(prefix="hermes-iso-")
            # 从默认 HERMES_HOME（~/.hermes 或 env HERMES_HOME）复制配置文件
            _src_home = os.path.expanduser(os.environ.get("HERMES_HOME", "~/.hermes"))
            for _cfg in ("config.yaml", ".env", "SOUL.md"):
                _src = os.path.join(_src_home, _cfg)
                if os.path.isfile(_src):
                    try:
                        _sh.copy2(_src, os.path.join(_iso_home, _cfg))
                    except Exception:
                        pass
            # 覆写 config.yaml 中的 tool_progress 为 new（减少 state.db 写入量，
            # 避免 hermes 内部多线程争锁）。env HERMES_TOOL_PROGRESS_MODE 已 deprecated，
            # config.yaml 优先级更高，必须改文件才生效。
            _cfg_path = os.path.join(_iso_home, "config.yaml")
            if os.path.isfile(_cfg_path):
                try:
                    with open(_cfg_path, "r") as _f:
                        _cfg_text = _f.read()
                    import re as _re
                    _cfg_text = _re.sub(
                        r'(tool_progress:\s*)\w+',
                        r'\g<1>new',
                        _cfg_text)
                    with open(_cfg_path, "w") as _f:
                        _f.write(_cfg_text)
                except Exception:
                    pass
            # 注入 _grape_memory_db.pth：让 hermes state.db 使用 :memory:（纯内存）。
            # sitecustomize.py 方式不稳定（import hermes_state 在 Python 启动初期可能失败）。
            # .pth 文件由 site 模块在 sys.path 初始化后处理，hermes_state 此时已可 import。
            _inject_dir = os.path.join(_iso_home, "py_inject")
            os.makedirs(_inject_dir, exist_ok=True)
            with open(os.path.join(_inject_dir, "_grape_memory_db.pth"), "w") as _f:
                _f.write("import hermes_state, pathlib; hermes_state.DEFAULT_DB_PATH = pathlib.Path(':memory:')\n")
            _proc_env = dict(env)
            _proc_env["HERMES_HOME"] = _iso_home
            _old_pp = _proc_env.get("PYTHONPATH", "")
            _proc_env["PYTHONPATH"] = _inject_dir + (":" + _old_pp if _old_pp else "")
            log.info(f"{label}hermes HERMES_HOME isolated: {_iso_home} (state.db=:memory: via .pth)")

        # 组件2：文件系统隔离（GRAPE_AGENT_SANDBOX 开 + bwrap 可用时套白名单锁）
        _cmd = [agent_cmd] + args
        try:
            from src.agent.sandbox import build_bwrap_cmd
            _wrap = build_bwrap_cmd(cwd, backend_name=backend.name)
            if _wrap:
                _cmd = _wrap + _cmd
                log.info(f"{label}{backend.name} sandboxed via bwrap (cwd={cwd})")
        except Exception as _bw_e:
            log.warning(f"{label}{backend.name} bwrap wrap failed, running unsandboxed: {_bw_e}")

        # 纯管道启动（与 main 一致；script 包装为临时对照，已按分析移除）
        proc = subprocess.Popen(
            _cmd,
            stdin=stdin_pipe, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            cwd=cwd, env=_proc_env, bufsize=0, **_popen_kwargs
        )
        _track_proc(proc)
        if backend.prompt_via_stdin:
            proc.stdin.write(prompt.encode("utf-8"))
            proc.stdin.close()

        collected_text = []
        new_session_id = use_session_id
        error_events = []
        step_count = 0
        _last_output = time.time()
        _reader_total = 0
        # 进程存活但无输出的累计延长上限（防长命令/长思考无上限；超硬上限仍强杀，见 #501 修复）
        # 默认 = max_exec_seconds（首个静默窗口 + 1 次续期 ≈ 20min），可用 AGENT_SILENT_LIMIT 调整；
        # 兼容旧变量 AGENT_SILENT_EXTEND_CAP。
        _silent_extend_total = 0.0
        _silent_extend_cap = float(os.environ.get("AGENT_SILENT_LIMIT",
                                                   os.environ.get("AGENT_SILENT_EXTEND_CAP", str(max_exec_seconds))))

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
                        _tok = parsed.get("tokens", "")
                        yield ("step_finish", f"Step {step_count} done" + (f", tokens={_tok}" if _tok else ""))
                    elif et == "error":
                        error_events.append(_redact(parsed["message"], _tokens))
                        yield ("error", _redact(parsed["message"], _tokens))
                    elif et in ("tool_finish", "tool_result", "api_call", "api_response", "session"):
                        # Hermes verbose 模式新事件类型：直接转发 text 字段
                        _txt = parsed.get("text", "")
                        if _txt:
                            yield (et, _redact(_txt, _tokens))
                        # session 事件提取 sessionID
                        if et == "session" and parsed.get("sessionID"):
                            new_session_id = parsed["sessionID"]
                    else:
                        yield ("other", et)
            elif _reader_done:
                log.info(f"{label}{backend.name} loop break on reader EOF: raw={_raw_count} parsed={_parsed_count} total_lines={_reader_total}")
                break
            else:
                elapsed_silent = time.time() - _last_output
                if elapsed_silent > max_exec_seconds:
                    # 若 opencode 进程仍存活（长 bash 命令/长思考中，如 pip install），
                    # 不立即判超时——重置静默计时继续等待，避免长命令被误杀（见 #501 修复）。
                    # 但累计延长受硬上限约束（AGENT_SILENT_EXTEND_CAP，默认 3×max_exec_seconds），
                    # 防止进程活着的死循环永久挂起。
                    if proc.poll() is None and _silent_extend_total < _silent_extend_cap:
                        _silent_extend_total += elapsed_silent
                        log.info(f"{label}{backend.name} silent {elapsed_silent:.0f}s but process alive, "
                                 f"extending window (long cmd/reasoning, total_extend={_silent_extend_total:.0f}s)")
                        _last_output = time.time()
                        time.sleep(0.1)
                        continue
                    log.warning(f"{label}{backend.name} no output for {max_exec_seconds}s"
                                f"(extend_total={_silent_extend_total:.0f}s), terminating")
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
                    _untrack_proc(proc)
                    # codearts 方案E2: 超时也要杀 serve
                    if _serve_proc and _serve_proc.poll() is None:
                        try:
                            os.killpg(os.getpgid(_serve_proc.pid), signal.SIGTERM)
                            _serve_proc.wait(timeout=3)
                        except Exception:
                            try: os.killpg(os.getpgid(_serve_proc.pid), signal.SIGKILL)
                            except Exception: _serve_proc.kill()
                    return
                time.sleep(0.1)

        try:
            proc.wait(timeout=120)
        except subprocess.TimeoutExpired:
            log.warning(f"{label}{backend.name} did not exit after stdout EOF, terminating")
            _terminate_proc(proc, f"{label}{backend.name}")
            _reader_thread.join(timeout=5)
            _untrack_proc(proc)
            # codearts 方案E2: 超时也要杀 serve
            if _serve_proc and _serve_proc.poll() is None:
                try:
                    os.killpg(os.getpgid(_serve_proc.pid), signal.SIGTERM)
                    _serve_proc.wait(timeout=3)
                except Exception:
                    try: os.killpg(os.getpgid(_serve_proc.pid), signal.SIGKILL)
                    except Exception: _serve_proc.kill()
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
        _untrack_proc(proc)

        # codearts 方案E2: 杀掉 per-task codearts serve 子进程，释放内存
        if _serve_proc and _serve_proc.poll() is None:
            try:
                _pgid = os.getpgid(_serve_proc.pid)
                os.killpg(_pgid, signal.SIGTERM)
                _serve_proc.wait(timeout=5)
            except Exception:
                try:
                    _pgid = os.getpgid(_serve_proc.pid)
                    os.killpg(_pgid, signal.SIGKILL)
                except Exception:
                    _serve_proc.kill()
            log.info(f"{label}codearts serve (pid={_serve_proc.pid}) killed, RSS released")

        # 清理 hermes 隔离的 HERMES_HOME 临时目录（state.db 等不再需要）
        if backend.name == "hermes" and _iso_home:
            try:
                import shutil as _shutil
                _shutil.rmtree(_iso_home, ignore_errors=True)
            except Exception:
                pass

        yield ("done", {
            "exit_code": proc.returncode, "text": _redact(full_text, _tokens),
            "oc_session_id": new_session_id,
            "errors": error_events, "stderr": _redact(stderr_output, _tokens),
            "backend": backend.name, "reader_total": _reader_total, "parsed_count": _parsed_count,
        })

    # 执行一次；若因会话失效失败，降级重跑一次（不带 session）。
    # 判定覆盖两种情况：
    #  1. stderr 显式含 "session not found"（旧版 opencode 行为）；
    #  2. 带 session 启动但进程"无任何输出即失败"（exit!=0 且 text/stderr 均为空）——
    #     新版 opencode（≥1.17）对失效 session 静默 EOF exit=1，不再打印 Session not found。
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

    _stderr = (done.get("stderr") or "").lower()
    _errors = " ".join(done.get("errors") or []) or ""
    # 新版 opencode 对失效 session 静默失败：exit!=0 且无 text/stderr，
    # 但 stdout 读到过 ≥1 个被解析的事件后即断流（比对 raw=0 parsed=1 的日志形态）。
    # 用 parsed_count 区分：真实错误（裸行非 NDJSON）parsed_count=0 → 不重跑。
    _stale_fail = (
        done.get("exit_code", 0) != 0
        and not (done.get("text") or "").strip()
        and not (done.get("stderr") or "").strip()
        and backend.name == "opencode"
        and (done.get("parsed_count") or 0) >= 1
    )
    session_missing = (
        session_id
        and done.get("exit_code", 0) != 0
        and ("session not found" in _stderr or "session not found" in _errors.lower() or _stale_fail)
    )
    if session_missing:
        log.warning(f"{label}{backend.name} session {session_id[:20]}... invalid/not-found (exit={done.get('exit_code')} empty-out), retrying without session")
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
