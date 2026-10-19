"""Agent 执行器：流式子进程调度 (NDJSON)，不绑定具体后端。"""
import os, subprocess, json, shutil, time, signal
from typing import Generator, AsyncGenerator
from src.config import log
from .base import AgentBackend
from .opencode import OpencodeBackend
from .codearts import CodeArtsBackend
from .hermes import HermesBackend
from .codex import CodexBackend
from .hwcloud import HwcloudBackend


_BACKENDS = {
    "opencode": OpencodeBackend(),
    "codearts": CodeArtsBackend(),
    "hermes": HermesBackend(),
    "codex": CodexBackend(),
    "hwcloud": HwcloudBackend(),
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

    同时将 DB 中所有 running/dispatched 状态的 agent_tasks 和对应 events 标记为 failed，
    防止服务重启后留下悬空任务（status=running 但实际进程已终止）。

    graceful-shutdown-requeue-design.md Step 1：
    - 失败原因细化并**同步写进 events.result**（否则事件详情页「失败原因」会兜底显示
      summary，如 "A2A: → coding" 无意义，见 #2337）；
    - 对被 force-fail 的本机任务**统一释放 busy 槽**（release_busy_slot），杜绝每次重启漏槽；
    - REMOTE 任务（worker 承载）继续跳过。
    """
    _SHUTDOWN_EVENT_REASON = "任务被系统重启/停机中断，可能未完成，请重派"
    _SHUTDOWN_TASK_REASON = ("任务被系统重启/停机中断（server shutdown/restart）；"
                             "可能未完成或有部分产出，请人工确认后重派（redispatch）")
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
    # 标记 DB 中 running/dispatched 任务（防止重启后悬空）。
    # Issue #404 自杀修复（2026-10-15）：REMOTE 任务（worker 承载引擎，credential_id>0）的
    # 进程活在远端 worker 上，与主控进程生死无关——主控停机不等于任务死了。停机时把它们
    # 标 failed 会在部署类任务停主控时自杀（agent 通过 SSH systemctl stop grape-main →
    # 本函数把任务标 failed → worker 侧进程仍在跑但上报被幂等保护拒绝 → 任务永败）。
    # REMOTE 任务改为跳过：worker 继续执行并正常上报（complete_task 不受影响）；
    # 悬空兜底 = worker 侧 recover-orphans（worker 重连时 force_fail）+ scheduler
    # zombie recovery（引擎离线的 running 任务同步 failed，见 scheduler_loop）。
    # 只有本机 DIRECT 任务（runtime_id=0 或本机引擎 credential_id=0）的子进程确实被
    # 上面的 SIGTERM 杀掉了，照旧标 failed。
    try:
        from src.session_manager import db, db_lock
        from src.api.event_store import update_event_complete
        import time as _time
        _now = _time.strftime("%Y-%m-%d %H:%M:%S", _time.gmtime())
        with db_lock:
            c = db.cursor()
            # 查询所有 running/dispatched 任务 + 引擎归属（credential_id>0 = REMOTE worker 承载）
            c.execute(
                "SELECT t.id, t.event_seq, t.runtime_id, "
                "COALESCE(ar.credential_id, 0) "
                "FROM agent_tasks t "
                "LEFT JOIN agent_runtimes ar ON ar.id = t.runtime_id "
                "WHERE t.status IN ('running','dispatched')"
            )
            stale_tasks = c.fetchall()
            _remote_skipped = 0
            for task_id, event_seq, runtime_id, cred_id in stale_tasks:
                # REMOTE 任务：进程在 worker 上活着，跳过（worker 会继续跑完并上报；
                # 兜底 = worker recover-orphans + scheduler zombie recovery 引擎离线同步）
                if runtime_id and cred_id and cred_id > 0:
                    _remote_skipped += 1
                    log.info(f"terminate_active_agents: skip REMOTE task {task_id} "
                             f"(runtime={runtime_id}, worker-owned, will self-report)")
                    continue
                try:
                    c.execute(
                        "UPDATE agent_tasks SET status='failed', exit_code=-1, "
                        "completed_at=?, stream_ended=1, self_reported=1, errors=? WHERE id=?",
                        (_now, _SHUTDOWN_TASK_REASON, task_id)
                    )
                    if event_seq:
                        # Issue #369：完整回写事件诊断字段（result/exit_code/completed_at），
                        # 对齐 event_pipeline._fail_event 语义；终态保护天然幂等（双主安全）。
                        # rebase 融合注：reason 用 _SHUTDOWN_EVENT_REASON（上游 #2337 文案；
                        # 原 cherry-pick 版引用过未定义的 _reason，此处修正）。
                        update_event_complete(event_seq, "failed", exit_code=-1,
                                              completed_at=_now,
                                              result=_SHUTDOWN_EVENT_REASON[:200])
                    # Step 1：统一释放 busy 槽（不漏槽，防止重启累积卡死队列）
                    if runtime_id:
                        try:
                            from src.runtime.service import release_busy_slot
                            release_busy_slot(runtime_id, cursor=c)
                        except Exception as _rb:
                            log.warning(f"terminate_active_agents: release_busy_slot failed {runtime_id}: {_rb}")
                    log.info(f"terminate_active_agents: marked stale DIRECT task {task_id} (seq={event_seq}) as failed")
                except Exception as e:
                    log.warning(f"terminate_active_agents: failed to mark task {task_id} as failed: {e}")
            db.commit()
            if _remote_skipped:
                log.info(f"terminate_active_agents: skipped {_remote_skipped} REMOTE task(s) (worker-owned, left running)")
        if stale_tasks:
            log.info(f"terminate_active_agents: marked {len(stale_tasks)} stale task(s) as failed")
    except Exception as e:
        log.warning(f"terminate_active_agents: stale task cleanup failed: {e}")



def register_backend(name: str, backend: AgentBackend):
    """注册第三方 Agent 后端（插件接口，~20 行代码即可接入新引擎）"""
    _BACKENDS[name] = backend
    log.info(f"Backend registered: {name} -> {backend.__class__.__name__}")


def get_backend(name: str) -> AgentBackend:
    return _BACKENDS.get(name, _BACKENDS["opencode"])


def discover_backends() -> list[dict]:
    """动态发现本地已安装的 Agent 引擎（跨平台）。

    Unix: shutil.which() 查 PATH。
    Windows: shutil.which() 查 PATH（含 PATHEXT）+ os_adapter 额外查找常见安装目录。
    """
    from src.runtime.os_adapter import find_cli
    known = {
        "opencode": {"cli": "opencode", "pip": "opencode"},
        "codearts": {"cli": "codearts", "pip": "codearts-cli"},
        "hermes": {"cli": "hermes", "pip": "hermes"},
        "codex": {"cli": "codex", "pip": "@openai/codex"},
        "hwcloud": {"cli": "hwcloud", "pip": ""},
        "cursor": {"cli": "cursor", "pip": "cursor"},
    }
    results = []
    for name, info in known.items():
        cli_path = find_cli(info["cli"])
        results.append({
            "name": name,
            "installed": bool(cli_path),
            "cli_path": cli_path,
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


# ── 引擎异常退出遗言采集与自愈（issue #317）──
# 已观测 seq 10353/10392：引擎 exit 1、stderr 空、result 截断，事后零线索无法定位。
_LOG_TAIL_MAX_LINES = 50
_LOG_TAIL_MAX_BYTES = 4096
_OOM_KEYWORDS = ("out of memory", "oom-killer", "oom_reaper", "killed process")


def _push_log_tail(tail: list, line: str):
    """维护引擎 stdout 遗言尾部缓冲：末 50 行 / 累计 4KB 截断。"""
    if not line:
        return
    tail.append(line)
    while tail and (len(tail) > _LOG_TAIL_MAX_LINES
                    or sum(len(_l) for _l in tail) > _LOG_TAIL_MAX_BYTES):
        tail.pop(0)


def _oom_kill_evidence() -> bool:
    """启发式检查系统日志中是否有内核 OOM killer 记录（仅 POSIX；5s 超时，异常静默）。"""
    if not hasattr(os, "getpgid"):
        return False
    # ① dmesg / journalctl（容器内可能无权限，逐项降级）
    for cmd in (["dmesg"], ["journalctl", "-k", "--no-pager", "-n", "200"]):
        try:
            _out = subprocess.run(cmd, capture_output=True, timeout=5).stdout
            _txt = _out.decode("utf-8", "replace").lower()
            if any(k in _txt for k in _OOM_KEYWORDS):
                return True
        except Exception:
            continue
    # ② 内核/系统日志文件兜底（读尾部 4KB）
    for _path in ("/var/log/syslog", "/var/log/messages", "/var/log/kern.log"):
        try:
            with open(_path, "rb") as _f:
                _f.seek(0, 2)
                _size = _f.tell()
                _f.seek(max(0, _size - _LOG_TAIL_MAX_BYTES))
                _txt = _f.read().decode("utf-8", "replace").lower()
            if any(k in _txt for k in _OOM_KEYWORDS):
                return True
        except Exception:
            continue
    return False


def _stream_hermes_acp(prompt, cwd, session_id, event_type, backend,
                       account, user_id, env_extra, bot_token, user_token,
                       agent_name, agent_label, max_exec_seconds,
                       repo_key: str = "") -> Generator:
    """hermes ACP 模式入口：构建 env → 调 hermes_acp.HermesACPBackend.stream_execute。

    与 stream_agent 的 opencode/codearts 路径并行，不走 build_args/parse_output。
    """
    from src.agent.hermes_acp import HermesACPBackend

    label = f"{agent_label} " if agent_label else ""

    acp = HermesACPBackend()
    env = acp.get_env(account=account, user_id=user_id)
    env["DEVOPS_EVENT_TYPE"] = event_type

    # P2：平台 VCS token（BOT_*/USER_*）不再注入 agent env，收口到 grape CLI + GRAPE_SESSION_TOKEN。
    # _assemble_env 仅注入机制元数据（VCS_API_BASE/VCS_NAME）与用户云凭据（openai 等）。
    _assemble_env(env, kind=("chat" if event_type == "chat" else "devops"),
                  label=label, bot_token=bot_token, user_token=user_token,
                  user_id=user_id, repo_key=repo_key)
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


def _stream_hwcloud_acp(prompt, cwd, session_id, event_type, backend,
                        account, user_id, env_extra, bot_token, user_token,
                        agent_name, agent_label, max_exec_seconds,
                        repo_key: str = "") -> Generator:
    """hwcloud ACP 模式入口：构建 env → 调 hwcloud_acp.HwcloudACPBackend.stream_execute。

    与 _stream_hermes_acp 并行（docs/hwcloud-runtime-acp-design.md）：
    - provider 凭据经 env 注入（HWCLOUD_PROVIDER_*，hwcloud_acp 写入 per-task
      settings.json）；来源：env_extra（用户云凭据 provider=opengw 按 user_id 注入）
    - session_id 一期不使用（resume 不校验存在性，见设计实测行为 §5）
    """
    from src.agent.hwcloud_acp import HwcloudACPBackend

    label = f"{agent_label} " if agent_label else ""

    acp = HwcloudACPBackend()
    env = acp.get_env(account=account, user_id=user_id)
    env["DEVOPS_EVENT_TYPE"] = event_type

    _assemble_env(env, kind=("chat" if event_type == "chat" else "devops"),
                  label=label, bot_token=bot_token, user_token=user_token,
                  user_id=user_id, repo_key=repo_key)
    if agent_name:
        env["AGENT_NAME"] = agent_name
    if env_extra:
        env.update(env_extra)

    # 技能路径占位符替换（direct 模式）
    try:
        from src.config import replace_skill_dir_placeholders
        prompt = replace_skill_dir_placeholders(prompt)
    except Exception:
        pass

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


def _assemble_env(env: dict, *, kind: str, label: str = "",
                  bot_token: str = "", user_token: str = "", user_id: int = 0,
                  repo_key: str = "", username: str = "") -> None:
    """组装 agent 执行环境（P2：平台 VCS token 不再注入 agent env）。

    - **BOT_GITCODE_TOKEN / BOT_GITHUB_TOKEN / USER_GITCODE_TOKEN / USER_GITHUB_TOKEN / BOT_TOKEN
      一律不再注入**（vcs-env-token-boundary-audit-design.md P2）：平台长期凭据只在服务端
      进程内流转，agent 侧统一经 `grape git/git-repo/issues`（携带 GRAPE_SESSION_TOKEN=tk_）转发。
    - 机制元数据（VCS_API_BASE / VCS_NAME）仍注入，供技能判断平台。
    - 用户云凭据（非 VCS 平台 token：openai/custom 等）仍按 user_id 注入——这些是
      chat/任务需要的模型凭据，与 VCS 平台 token 无关。
    - 参数 bot_token/user_token 仅用于日志脱敏（_tokens 列表），不再写入 env。
    """
    # 平台 VCS token 不再注入（P2）。bot_token/user_token 保留仅作日志脱敏用途，
    # 若确需在此记录是否持有，仅 print 长度摘要即可，值绝不进 env。
    # Issue #344: 改用带预刷新的 token 获取函数，避免拿到过期 token。
    if kind == "chat":
        if not user_token and user_id > 0:
            try:
                from src.api.deps import get_user_token_with_refresh
                from src.auth.grape_account_db import get_grape_account_by_id
                _acc = get_grape_account_by_id(user_id)
                if _acc:
                    user_token = get_user_token_with_refresh(_acc["username"], user_id=user_id) or ""
            except Exception as e:
                log.warning(f"Fallback resolve user gitcode token failed: {e}")
                user_token = ""
        if user_token:
            log.info(f"{label}chat task: user token present (len={len(user_token)}), NOT injecting into env (P2)")
        else:
            log.warning(f"{label}NO user platform token available for chat; VCS ops go through grape CLI + session token")
    elif bot_token:
        log.info(f"{label}devops event task: bot token present (len={len(bot_token)}), NOT injecting into env (P2)")
        # GitHub bot token 不再注入（agent 侧一律 grape CLI 转发）

    # VCS API base URL + 名称注入（skill 中 $VCS_API_BASE 引用，适配多 VCS provider）
    if repo_key:
        try:
            from src.prompt_builder import _vcs_api_base, _vcs_name
            env["VCS_API_BASE"] = _vcs_api_base(repo_key)
            env["VCS_NAME"] = _vcs_name(repo_key)
        except Exception as e:
            log.warning(f"Failed to inject VCS_API_BASE: {e}")

    # 用户云凭据注入（devops 的 user_id=仓库归属用户，见 event_pipeline _issue_scene_uid）
    # #219：huawei_cloud / codearts 的 AK-SK 不再经 env 注入（按需获取或自定义场景变量），跳过。
    if user_id > 0:
        try:
            from src.user.db import get_credentials
            for c in get_credentials(user_id):
                provider = c.get("provider", "")
                cfg = c.get("config", {})
                if provider in ("huawei_cloud", "codearts"):
                    continue
                for k, v in cfg.items():
                    k_upper = k.upper()
                    # 防御：任何以 *_TOKEN / *_KEY / *_SECRET / *_PASSWORD 结尾的云凭据
                    # 仍属于敏感凭据，但它们是模型云凭据（openai/api_key 等），
                    # chat 任务确实需要——与 VCS 平台 token 边界不同，按 P2 范围保留。
                    env[k_upper] = str(v)
                # provider=openai: api_key → OPENAI_API_KEY（codex 读此变量）
                if provider == "openai" and cfg.get("api_key"):
                    env["OPENAI_API_KEY"] = str(cfg["api_key"])
        except Exception as e:
            log.warning(f"Failed to inject user credentials: {e}")


def stream_chat_agent(prompt: str, cwd: str, session_id: str = "",
                      account: str = "", user_id: int = 0,
                      user_token: str = "",
                      env_extra: dict | None = None,
                      agent_name: str = "",
                      agent_label: str = "",
                      backend_name: str = "",
                      repo_key: str = "",
                      max_exec_seconds: int = 600) -> Generator:
    """chat 语义专用执行器：以用户身份运行（P2：不注入平台 token，VCS 走 grape CLI + 会话 token）。

    与 stream_devops_agent 隔离：本函数签名不含 bot_token，从调用方层面杜绝
    「以仓库 bot token 冒充用户身份」可能（§十 写操作隔离）。
    """
    yield from stream_agent(
        prompt, cwd, session_id=session_id, event_type="chat",
        backend_name=backend_name, account=account, user_id=user_id,
        env_extra=env_extra, bot_token="", user_token=user_token,
        agent_name=agent_name, agent_label=agent_label,
        repo_key=repo_key,
        max_exec_seconds=max_exec_seconds,
    )


def stream_devops_agent(prompt: str, cwd: str, session_id: str = "",
                        event_type: str = "",
                        account: str = "", repo_user_id: int = 0,
                        bot_token: str = "",
                        env_extra: dict | None = None,
                        agent_name: str = "",
                        agent_label: str = "",
                        backend_name: str = "",
                        repo_key: str = "",
                        max_exec_seconds: int = 600) -> Generator:
    """devops 事件语义专用执行器：以仓库身份运行（P2：不注入 VCS 平台 token）。

    repo_user_id 为仓库归属用户（仅用于注入云凭据），与 chat 用户 token 严格分离——
    事件任务无法以用户身份写 GitCode（§十 写操作隔离）。签名不含 user_token。
    """
    yield from stream_agent(
        prompt, cwd, session_id=session_id, event_type=event_type or "issue_opened",
        backend_name=backend_name, account=account, user_id=repo_user_id,
        env_extra=env_extra, bot_token=bot_token, user_token="",
        agent_name=agent_name, agent_label=agent_label,
        repo_key=repo_key,
        max_exec_seconds=max_exec_seconds,
    )


def stream_agent(prompt: str, cwd: str, session_id: str = "",
                 event_type: str = "", backend_name: str = "",
                 account: str = "", user_id: int = 0,
                 env_extra: dict | None = None,
                 bot_token: str = "", user_token: str = "",
                 agent_name: str = "",
                 agent_label: str = "",
                 repo_key: str = "",
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
            repo_key=repo_key,
        )
        return

    # ── hwcloud ACP 专用路径（docs/hwcloud-runtime-acp-design.md）──
    if backend_name == "hwcloud":
        yield from _stream_hwcloud_acp(
            prompt, cwd, session_id, event_type, backend,
            account, user_id, env_extra, bot_token, user_token,
            agent_name, agent_label, max_exec_seconds,
            repo_key=repo_key,
        )
        return

    env = backend.get_env(account=account, user_id=user_id)
    env["DEVOPS_EVENT_TYPE"] = event_type
    # P2：平台 VCS token（BOT_*/USER_*/BOT_TOKEN）不再注入 agent env——
    # VCS 操作统一走 grape CLI（携带 GRAPE_SESSION_TOKEN），平台长期凭据只在服务端进程内流转。
    _assemble_env(env, kind=("chat" if event_type == "chat" else "devops"),
                  label=label, bot_token=bot_token, user_token=user_token,
                  user_id=user_id, repo_key=repo_key)
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

    # 需要脱敏的 token 列表（用于日志/事件输出；_assemble_env 已把 token/凭据注入 env）
    _tokens = [t for t in (bot_token, user_token) if t]

    # codearts 方案E2: serve 启动移到 _run_once 内部（每次执行启动自己的 serve，避免单例并发串号）
    # pre_exec/post_exec 由 _run_once 内部调用，serve 信息用局部变量

    agent_cmd = shutil.which(backend.binary) or shutil.which(backend.binary + ".cmd")
    if not agent_cmd:
        from src.runtime.os_adapter import find_cli
        agent_cmd = find_cli(backend.binary)
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
            # 使用当前用户家目录下的缓存目录，避免 /tmp 多用户权限冲突（issue #238）
            _hermes_iso_root = os.path.join(os.path.expanduser("~"), ".cache", "hermes-iso")
            os.makedirs(_hermes_iso_root, exist_ok=True)
            _iso_home = tempfile.mkdtemp(prefix="hermes-iso-", dir=_hermes_iso_root)
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
            from src.runtime.os_adapter import path_separator
            _sep = path_separator()
            _proc_env["PYTHONPATH"] = _inject_dir + (_sep + _old_pp if _old_pp else "")
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
        # issue #317：引擎遗言尾部缓冲（exit!=0 且 stderr 空时补采进 errors）
        _log_tail = []
        step_count = 0
        _last_output = time.time()
        _reader_total = 0
        _text_count = 0      # 本次执行 text 事件数（假完成判定用，issue-P5）
        _tool_count = 0      # 本次执行 tool_use 事件数（假完成判定用，issue-P5）
        # 静默超时窗口：首个无输出窗口。与 hermes ACP _SILENT_TIMEOUT=300s 对齐，
        # 可用 AGENT_SILENT_WINDOW 环境变量覆盖（模型流挂死 5 分钟即感知，不再干等 20min）。
        _silent_window = float(os.environ.get("AGENT_SILENT_WINDOW", str(min(300, max_exec_seconds))))
        # 进程存活但无输出的累计延长上限（防长命令/长思考无上限；超硬上限仍强杀，见 #501 修复）
        # 可用 AGENT_SILENT_EXTEND_CAP 调整（旧变量 AGENT_SILENT_LIMIT 亦兼容，语义=续期额度上限）。
        _silent_extend_total = 0.0
        _silent_extend_cap = float(os.environ.get("AGENT_SILENT_EXTEND_CAP",
                                                   os.environ.get("AGENT_SILENT_LIMIT", str(max_exec_seconds))))
        # 超时总账（issue-P1）：单次执行最坏 ≈ _silent_window + _silent_extend_cap + grace 10s；
        # RUNTIME_TASK_TIMEOUT=3600s 需 ≥ 2×(单次最坏) 才能容纳一次无效完成自动重跑。
        # 300 + 600 + 10 = 910s → 2 次 ≈ 1820s < 3600s，安全。

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

        # stderr reader 线程：持续排空 stderr 管道，防止 Windows 管道缓冲区满导致死锁
        _stderr_chunks = []
        def _stderr_reader():
            try:
                for raw_line in iter(proc.stderr.readline, b""):
                    _stderr_chunks.append(raw_line.decode("utf-8", errors="replace"))
            except Exception:
                pass
        _stderr_thread = threading.Thread(target=_stderr_reader, daemon=True)
        _stderr_thread.start()

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
                    _push_log_tail(_log_tail, line)
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
                        _text_count += 1
                        yield ("text", _redact(parsed["text"], _tokens))
                    elif et == "reasoning":
                        yield ("reasoning", _redact(parsed["text"], _tokens))
                    elif et == "tool_use":
                        _tool_count += 1
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
                if elapsed_silent > _silent_window:
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
                    log.warning(f"{label}{backend.name} no output for {_silent_window:.0f}s"
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
                        "errors": error_events + ([f"no output for {_silent_window:.0f}s, killed"] if _grace_tail.strip() == "" else []),
                        "stderr": "",
                        "backend": backend.name, "parsed_count": _parsed_count,
                        "text_count": _text_count, "tool_count": _tool_count,
                    })
                    _untrack_proc(proc)
                    # codearts 方案E2: 超时也要杀 serve
                    if _serve_proc and _serve_proc.poll() is None:
                        try:
                            if hasattr(os, "getpgid"):
                                os.killpg(os.getpgid(_serve_proc.pid), signal.SIGTERM)
                            else:
                                _serve_proc.terminate()
                            _serve_proc.wait(timeout=3)
                        except Exception:
                            try:
                                if hasattr(os, "getpgid"):
                                    os.killpg(os.getpgid(_serve_proc.pid), signal.SIGKILL)
                                else:
                                    _serve_proc.kill()
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
            stderr_output = "".join(_stderr_chunks)[:500]
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

        # ── 异常退出遗言补采（issue #317）──
        # exit!=0 且 stderr 为空 = 引擎"无遗言"死亡：补采 stdout 尾部进 errors；
        # returncode==-9（外部 SIGKILL，非本 executor 超时杀——那条路径已自带 done）
        # 结合系统日志启发式判定 OOM。
        _exit_code = proc.returncode
        _final_errors = list(error_events)
        if _exit_code != 0:
            if _exit_code == -9:
                if _oom_kill_evidence():
                    _final_errors.append(
                        "suspected OOM (SIGKILL, kernel oom-killer evidence in system logs)")
                else:
                    _final_errors.append(
                        "killed by SIGKILL (no oom-killer evidence in system logs)")
            if not stderr_output.strip() and _log_tail:
                _final_errors.append(
                    "agent_logs tail (last 50 lines/4KB):\n"
                    + _redact("\n".join(_log_tail), _tokens))
                log.warning(f"{label}{backend.name} abnormal exit={_exit_code} with empty stderr, "
                            f"supplemented errors from stdout tail ({len(_log_tail)} lines)")

        yield ("done", {
            "exit_code": _exit_code, "text": _redact(full_text, _tokens),
            "oc_session_id": new_session_id,
            "errors": _final_errors, "stderr": _redact(stderr_output, _tokens),
            "backend": backend.name, "reader_total": _reader_total, "parsed_count": _parsed_count,
            "text_count": _text_count, "tool_count": _tool_count,
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

    # ── 引擎异常退出自愈（issue #317）：exit!=0 且 stderr 为空的"无遗言"死亡 ──
    # 触发条件收紧为"引擎产出过事件后死亡"（parsed_count>=1，即 result 截断形态，
    # 如 seq 10353/10392），避免对"启动即裸行报错"（parsed_count=0，如权限错误）盲目重跑。
    # 幂等性：重试仍在本事件执行内进行——pipeline 已通过 event_active_guard 原子抢占
    # (repo,issue,agent) 守卫并持有至终态，重试不产生重复事件/重复调度；
    # 且与上方 session_missing 重跑共享"每流最多一次重试"额度（session_missing=True
    # 时本分支不再触发），防止叠加双跑产生重复评论。
    _abrupt_exit = (
        done.get("exit_code", 0) != 0
        and not (done.get("stderr") or "").strip()
        and (done.get("parsed_count") or 0) >= 1
        and not session_missing
    )
    if _abrupt_exit:
        log.warning(f"{label}{backend.name} abrupt exit={done.get('exit_code')} with empty stderr "
                    f"(truncated output, parsed={done.get('parsed_count')}), "
                    f"retrying once without session")
        run_gen_ar = _run_once("")
        done_ar = None
        for evt_type, data in run_gen_ar:
            if evt_type == "done":
                done_ar = data
            else:
                yield (evt_type, data)
        if done_ar is not None:
            # 重试恢复成功时保留首跑的 OOM/SIGKILL 判定标记（事件可追溯，issue #317 验收项）
            _abrupt_notes = [e for e in (done.get("errors") or [])
                             if e.startswith(("suspected OOM", "killed by SIGKILL"))]
            done = done_ar
            if done.get("exit_code", 0) == 0 and _abrupt_notes:
                done.setdefault("errors", [])
                done["errors"] = list(done["errors"]) + [
                    f"recovered after auto-retry (first run: {n})" for n in _abrupt_notes]

    # ── 假完成识别（issue-P5：引擎 exit 0 但零产出）──
    # 场景：模型流挂死后引擎静默收场（codearts #9635）或空 end_turn（opencode #9662）。
    # 分级处理：
    #  a) 0 text + 0 tool → 零副作用 → 自动重跑一次（复用 session 失效重跑通道形状）；
    #  b) 0 text + tool>0 → 副作用状态未知（可能已发评论/PR/A2A），盲目重跑会重复执行
    #     → 不重跑，注入错误信息放行，由 event_pipeline 终态门槛（关卡B）裁为 failed。
    _invalid_done = (
        done.get("exit_code", 0) == 0
        and (done.get("text") or "").strip() == ""
        and (
            (done.get("text_count") or 0) == 0
            or (done.get("errors") or [])
        )
    )
    if _invalid_done and not session_missing:
        _tc = done.get("text_count") or 0
        _kc = done.get("tool_count") or 0
        if _tc == 0 and _kc == 0:
            log.warning(f"{label}{backend.name} invalid completion: exit=0 with 0 text/0 tool "
                        f"(model stream anomaly), retrying once without session")
            run_gen3 = _run_once("")
            done3 = None
            for evt_type, data in run_gen3:
                if evt_type == "done":
                    done3 = data
                else:
                    yield (evt_type, data)
            if done3 is not None:
                # 重跑仍无效完成 → 注入错误放行（关卡B 裁 failed）
                if (done3.get("exit_code", 0) == 0
                        and (done3.get("text") or "").strip() == ""
                        and (done3.get("text_count") or 0) == 0):
                    done3.setdefault("errors", []).append(
                        "invalid completion: engine exited 0 with empty output (model stream anomaly)")
                    log.warning(f"{label}{backend.name} retry still invalid completion, marking error")
                done = done3
        else:
            done.setdefault("errors", []).append(
                f"engine exited 0 after {_kc} tool calls with no conclusion "
                f"(suspected model stream anomaly, manual re-dispatch advised)")
            log.warning(f"{label}{backend.name} invalid completion: exit=0 with 0 text/{_kc} tools, "
                        f"no auto-retry (side effects unknown)")
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
