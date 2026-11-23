"""Agent 执行器：流式子进程调度 (NDJSON)，不绑定具体后端。"""
from src.kernel.user_ids import has_user_id
import os, subprocess, json, time, signal, contextlib, shutil, threading
from typing import Generator, AsyncGenerator
from src.kernel.config import log
from .base import AgentBackend
from .opencode import OpencodeBackend
from .codearts import CodeArtsBackend
from .hermes import HermesBackend
from .codex import CodexBackend
from .dsh import DshBackend
from .atomcode import AtomCodeBackend
from .hwcloud import HwcloudBackend
from .aishell_acp import AishellDshACPBackend
from .mock_acp import MockACPBackend

_bwrap_fail_notified = False
_SESSION_AUTH_FILE_GLOB = ".grape-sess-*.tok"
# 正常任务最长也远低于该阈值；只有崩溃后长期残留的 token 才会被清扫，
# 避免同 cwd 并发任务仍在运行时被后续任务误删。
_SESSION_AUTH_FILE_STALE_SECONDS = 6 * 60 * 60


def _note_bwrap_fail_once(message: str) -> None:
    """issue #500：bwrap 包装失败告警只发一次（后续降 DEBUG），防逐命令刷屏。

    放在调用方模块（而非 src.domains.runtime.agents.sandbox）是因为本函数需覆盖 sandbox
    自身导入失败的场景，此时不能依赖 sandbox 内的助手。
    """
    global _bwrap_fail_notified
    if _bwrap_fail_notified:
        log.debug(message)
        return
    _bwrap_fail_notified = True
    log.warning(message)


def _cleanup_session_auth_file(cwd: str, auth_file: "str | None",
                               task_started_at: "float | None" = None) -> None:
    """清理本任务凭据文件，并按 mtime 清扫崩溃残留。

    只允许直接删除 prepare_env 明确返回并由 _auth_state 记录的本任务文件；cwd 下
    其他新文件可能属于重叠执行的另一任务，必须保留。SIGKILL 不会执行 finally，
    因此额外清扫超过陈旧阈值的同名前缀文件，作为后续任务的兜底。
    """
    if auth_file:
        try:
            os.unlink(auth_file)
        except OSError:
            pass
    if not cwd or not os.path.isdir(cwd):
        return
    import glob as _glob
    cutoff = (task_started_at if task_started_at is not None else time.time()) \
        - _SESSION_AUTH_FILE_STALE_SECONDS
    for _stale in _glob.glob(os.path.join(cwd, _SESSION_AUTH_FILE_GLOB)):
        try:
            if os.path.getmtime(_stale) < cutoff:
                os.unlink(_stale)
        except OSError:
            pass


def _call_prepare_env(prepare_env, env: dict, cwd: str):
    """调用 prepare_env；兼容尚未接收 cwd 的自定义 runtime 旧签名。"""
    import inspect
    try:
        params = inspect.signature(prepare_env).parameters.values()
    except (TypeError, ValueError):
        return prepare_env(env, cwd=cwd)
    accepts_cwd = any(
        param.name == "cwd" or param.kind == inspect.Parameter.VAR_KEYWORD
        for param in params
    )
    if accepts_cwd:
        return prepare_env(env, cwd=cwd)
    return prepare_env(env)


def _find_hermes_bin(venv_py: str = "", sandbox_mode: bool = False) -> str:
    """按优先级查找 hermes 可执行文件，返回绝对路径（最后回退为裸命令名）。

    查找顺序：
      1. 与 venv python 同目录（最可靠——venv 里 python 和 hermes 在同一个 bin/）
      2. 当前解释器同目录（覆盖未显式传 venv_py 的情况）
      3. sysconfig 的 scripts 目录
      4. PATH 中的 hermes
      5. 裸命令名 "hermes"（交给调用方 / shell 解析）

    sandbox_mode=True 时跳过 isfile/access 校验——venv_py 来自沙箱内探测，
    宿主机文件系统不存在该文件，但路径是可信的。
    """
    import sys, sysconfig
    exe_name = "hermes.exe" if os.name == "nt" else "hermes"
    candidates: list = []

    # 1) 显式传入的 venv python 同目录（仅当 venv_py 是绝对路径时）
    if venv_py and "/" in venv_py:
        candidates.append(
            os.path.join(os.path.dirname(os.path.abspath(venv_py)), exe_name)
        )

    # 2) 当前解释器同目录（非 sandbox_mode——宿主机路径）
    if not sandbox_mode and sys.executable:
        candidates.append(os.path.join(os.path.dirname(sys.executable), exe_name))

    # 3) sysconfig scripts 目录（非 sandbox_mode——宿主机路径）
    if not sandbox_mode:
        try:
            scripts_dir = sysconfig.get_path("scripts")
            if scripts_dir:
                candidates.append(os.path.join(scripts_dir, exe_name))
        except Exception:
            pass

    # 4) PATH（非 sandbox_mode——宿主机 PATH）
    if not sandbox_mode:
        which = shutil.which("hermes")
        if which:
            candidates.append(which)

    # 校验存在 + 可执行
    for c in candidates:
        if sandbox_mode:
            # 沙箱模式：venv_py 来自沙箱内探测，路径可信，不做宿主机 isfile 校验
            if c:
                return c
        else:
            if c and os.path.isfile(c) and os.access(c, os.X_OK):
                return c

    # 5) 最后回退
    return "hermes"


_BACKENDS = {
    "opencode": OpencodeBackend(),
    "codearts": CodeArtsBackend(),
    "hermes": HermesBackend(),
    "codex": CodexBackend(),
    "dsh": DshBackend(),
    "atomcode": AtomCodeBackend(),
    "hwcloud": HwcloudBackend(),
    "aishell-dsh": AishellDshACPBackend(),
    "mock-acp": MockACPBackend(),
}

# 运行中的 agent 子进程注册表：供服务停机时统一终止，避免 uvicorn 优雅停机
# 等待 executor（opencode 子进程）超时被 systemd SIGKILL（grape-main timeout 重启根因，2026-08-18）。
import threading as _threading
_active_procs: set[subprocess.Popen] = set()
_active_procs_lock = _threading.Lock()


# Issue #1982：任务执行作用域。stream_agent 每轮执行创建独立 scope，记录本轮
# spawn 的所有引擎子进程；正常路径由 _untrack_proc 逐个关闭管道，异常/提前中断
# （cancel/requeue/stream 提前 break/GeneratorExit）由 scope finally 兜底 terminate
# + 关闭管道，保证 fd 不随任务数累积。
_exec_scope = _threading.local()


def _close_proc_pipes(proc, label: str = "") -> None:
    """显式关闭子进程 stdin/stdout/stderr 管道（幂等，异常吞掉）。

    仅 terminate/kill 不关闭 PIPE 时，fd 要等 Popen 对象被 GC 才释放；读线程持有
    引用会让释放更晚，长跑 worker 因此累积匿名 pipe 直至 EMFILE（Issue #1982）。
    """
    if proc is None:
        return
    for _name in ("stdin", "stdout", "stderr"):
        _stream = getattr(proc, _name, None)
        if _stream is None:
            continue
        try:
            _stream.close()
        except Exception:
            pass


def _scope_procs():
    """当前 stream_agent 作用域的进程集合（不在作用域内返回 None）。"""
    return getattr(_exec_scope, "procs", None)


def _track_reader_threads(*threads: _threading.Thread):
    """把本轮 spawn 的 stdout/stderr reader 线程登记进当前作用域。

    Issue #1982 复审：异常/cancel/requeue 路径由 _close_exec_scope 兜底时，
    reader 线程此前从未被 join——terminate+close 管道产生 EOF 后线程本应退出，
    但守护线程不被等待就无法确认回收（线程对象与队列残留至调度周期结束）。
    登记后在 scope 收尾统一 join（带超时，不阻塞任务路径）。
    """
    threads_set = getattr(_exec_scope, "reader_threads", None)
    if threads_set is not None:
        threads_set.update(t for t in threads if t is not None)


def _track_proc(proc: subprocess.Popen, backend: str = "", cwd: str = ""):
    with _active_procs_lock:
        proc._grape_backend = backend
        proc._grape_cwd = os.path.normcase(os.path.realpath(cwd)) if cwd else ""
        _active_procs.add(proc)
    procs = _scope_procs()
    if procs is not None:
        procs.add(proc)


def _untrack_proc(proc: subprocess.Popen):
    with _active_procs_lock:
        _active_procs.discard(proc)
    procs = _scope_procs()
    if procs is not None:
        procs.discard(proc)
    _close_proc_pipes(proc)


def _close_exec_scope(prev_scope):
    """结束本轮执行作用域：terminate 尚未回收的进程、关闭全部管道并 join reader 线程。

    Issue #1982 复审：join 顺序必须是 terminate → close（管道关闭产生 EOF 唤醒
    readline）→ join；先 join 再 close 会因 reader 阻塞在 readline 上无法唤醒。
    join 带 1.5s 超时，线程异常存活时仅告警不阻塞任务路径。
    """
    procs = _scope_procs()
    reader_threads = getattr(_exec_scope, "reader_threads", None)
    _exec_scope.procs = prev_scope
    _exec_scope.reader_threads = None
    for proc in list(procs or ()):
        try:
            if proc.poll() is None:
                _terminate_proc(proc, "stream-scope", grace_seconds=3)
        except Exception:
            pass
        try:
            _close_proc_pipes(proc)
        except Exception:
            pass
        with _active_procs_lock:
            _active_procs.discard(proc)
    for t in list(reader_threads or ()):
        try:
            if t.is_alive():
                t.join(timeout=1.5)
            if t.is_alive():
                log.warning("stream-scope reader thread still alive after join(1.5s): "
                            f"{t.name}; daemon thread, left to finish on EOF")
        except Exception:
            pass


def active_task_processes(backend: str, cwd: str) -> list:
    """Select only live child processes owned by this executor and exact workdir."""
    key = os.path.normcase(os.path.realpath(cwd))
    with _active_procs_lock:
        return [proc for proc in _active_procs
                if getattr(proc, "_grape_backend", "") == backend
                and getattr(proc, "_grape_cwd", "") == key
                and proc.poll() is None]


def terminate_task_agents(backend: str, cwd: str) -> None:
    for proc in active_task_processes(backend, cwd):
        _terminate_proc(proc, f"{backend} task cancellation", grace_seconds=3)
        _untrack_proc(proc)


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
        _close_proc_pipes(proc)
    if procs:
        log.info(f"terminate_active_agents: terminated {len(procs)} running agent subprocess(es)")
    # 标记 DB 中 running/dispatched 任务（防止重启后悬空）。
    # Issue #404 自杀修复（2026-10-15）：REMOTE 任务（worker 承载引擎，credential_id>0）的
    # 进程活在远端 worker 上，与主控进程生死无关——主控停机不等于任务死了。停机时把它们
    # 标 failed 会在部署类任务停主控时自杀（agent 通过 SSH systemctl stop grape-main →
    # 本函数把任务标 failed → worker 侧进程仍在跑但上报被幂等保护拒绝 → 任务永败）。
    # Issue #537 升级（2026-10-30）：#404 的"跳过"留下 running/dispatched 悬空——下游
    # （zombie recovery / 心跳判死 / 活跃计数）无法区分"正常跑"与"等自报"，重启窗口内
    # worker 心跳断 → 引擎被标 offline → zombie recovery 误判 failed → 终态定格（生产
    # exec incident case 误报实例）。改为显式中间态 pending_self_report + pending_since 落时刻：
    #   - worker 继续执行并自报（complete_task/fail_task 从 pending 正常转换，终态幂等
    #     只拦真终态 completed/failed/cancelled/timeout_failed）；
    #   - 超时兜底 = scheduler 每轮扫描的 pending 超时回收（默认 30min → timeout_failed）；
    #   - 悬空兜底 = worker 侧 recover-orphans + scheduler zombie recovery（引擎离线
    #     持续 > 阈值才判死，见 scheduler_loop）。
    # 只有本机 DIRECT 任务（runtime_id=0 或本机引擎 credential_id=0）的子进程确实被
    # 上面的 SIGTERM 杀掉了，照旧标 failed。
    try:
        from src.kernel.session_manager import db, db_lock
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
            _remote_pending = 0
            # Issue #4340：真实失败数（DIRECT 分支 UPDATE 实际命中行数）。
            # 旧汇总行用 len(stale_tasks) 候选总数冒充失败数：REMOTE 任务走
            # pending_self_report 不计失败，REMOTE-only 窗口会虚报（生产 9/9 全 REMOTE、
            # 真实 failed=0），污染失败率与复盘结论。
            _failed_direct = 0
            for task_id, event_seq, runtime_id, cred_id in stale_tasks:
                # REMOTE 任务：进程在 worker 上活着 → 置 pending_self_report 等自报
                # （不释放 busy 槽——任务仍占 worker 引擎槽；事件保持 running 等自报收口）
                if runtime_id and cred_id and cred_id > 0:
                    try:
                        # pending_since 用带时区 ISO-8601 UTC（+00:00）；Issue #588/#9495 教训：
                        # 无偏移的 gmtime() 值在 MySQL 后端会被 parse_dt 按 CST(+08:00) 解释，
                        # 凭空多 8 小时 → 刚置 pending 就被判超时。
                        _now_iso = _time.strftime("%Y-%m-%dT%H:%M:%S+00:00", _time.gmtime())
                        c.execute(
                            "UPDATE agent_tasks SET status='pending_self_report', "
                            "pending_since=? WHERE id=? AND status IN ('running','dispatched')",
                            (_now_iso, task_id)
                        )
                        # 只在真正切到 pending_self_report 时计数（并发自报已终态 → 0 行不计，
                        # 与 failed_direct 同口径：汇总数字对应 DB 实况）
                        if int(c.rowcount or 0) > 0:
                            _remote_pending += 1
                            log.info(f"terminate_active_agents: REMOTE task {task_id} "
                                     f"(runtime={runtime_id}) -> pending_self_report "
                                     f"(worker-owned, awaiting self-report)")
                        else:
                            log.info(f"terminate_active_agents: REMOTE task {task_id} "
                                     f"(runtime={runtime_id}) no longer running/dispatched, skip")
                    except Exception as e:
                        log.warning(f"terminate_active_agents: mark pending_self_report "
                                    f"failed for task {task_id}: {e}")
                    continue
                try:
                    # Issue #4340：补 status 守卫，使 rowcount 反映真实判死条数——
                    # 并发自报把任务推进到终态时命中 0 行，不得计入 failed_direct，
                    # 也不得把已终态任务覆写成 failed。
                    c.execute(
                        "UPDATE agent_tasks SET status='failed', exit_code=-1, "
                        "completed_at=?, stream_ended=1, self_reported=1, errors=? "
                        "WHERE id=? AND status IN ('running','dispatched')",
                        (_now, _SHUTDOWN_TASK_REASON, task_id)
                    )
                    # MySQL 驱动 rowcount 可能为 None，按 0 计
                    if int(c.rowcount or 0) <= 0:
                        log.info(f"terminate_active_agents: stale DIRECT task {task_id} "
                                 f"(seq={event_seq}) no longer running/dispatched, skip force-fail")
                        continue
                    _failed_direct += 1
                    if event_seq:
                        # Issue #369：完整回写事件诊断字段（result/exit_code/completed_at），
                        # 对齐 event_pipeline._fail_event 语义；终态保护天然幂等（双主安全）。
                        # rebase 融合注：reason 用 _SHUTDOWN_EVENT_REASON（上游 #2337 文案；
                        # 原 cherry-pick 版引用过未定义的 _reason，此处修正）。
                        from src.domains.runtime.agents.failure_info import build_failure_info
                        update_event_complete(
                            event_seq, "failed", exit_code=-1,
                            completed_at=_now,
                            result=_SHUTDOWN_EVENT_REASON[:200],
                            failure_info=build_failure_info(
                                error_type="ServiceShutdown", phase="terminated",
                                message=_SHUTDOWN_EVENT_REASON, exit_code=-1))
                    # Step 1：统一释放 busy 槽（不漏槽，防止重启累积卡死队列）
                    if runtime_id:
                        try:
                            from src.domains.runtime.engine.service import release_busy_slot
                            release_busy_slot(runtime_id, cursor=c)
                        except Exception as _rb:
                            log.warning(f"terminate_active_agents: release_busy_slot failed {runtime_id}: {_rb}")
                    log.info(f"terminate_active_agents: marked stale DIRECT task {task_id} (seq={event_seq}) as failed")
                except Exception as e:
                    log.warning(f"terminate_active_agents: failed to mark task {task_id} as failed: {e}")
            db.commit()
            if _remote_pending:
                log.info(f"terminate_active_agents: {_remote_pending} REMOTE task(s) "
                         f"-> pending_self_report (worker-owned, awaiting self-report)")
        if stale_tasks:
            # Issue #4340：中性三字段汇总（候选数 / 真实失败数 / 等待自报数），
            # 仅凭本行即可读出实况，无需回查 agent_tasks；REMOTE-only 窗口不再出现 "as failed"。
            log.info(f"terminate_active_agents: shutdown sweep candidates={len(stale_tasks)} "
                     f"failed_direct={_failed_direct} pending_self_report={_remote_pending}")
            if _failed_direct:
                log.info(f"terminate_active_agents: marked {_failed_direct} stale task(s) as failed")
    except Exception as e:
        log.warning(f"terminate_active_agents: stale task cleanup failed: {e}")



def register_backend(name: str, backend: AgentBackend):
    """注册第三方 Agent 后端（插件接口，~20 行代码即可接入新引擎）"""
    _BACKENDS[name] = backend
    log.info(f"Backend registered: {name} -> {backend.__class__.__name__}")


def get_backend(name: str) -> AgentBackend:
    """按名取 backend，未注册显式抛错（P0-4 暗雷修复，2026-11-13 spec A4/A5）。

    历史行为是静默 fallback opencode——拼写错的引擎名会「跑错引擎」而非报错，
    descriptor 化迁移期尤其危险。显式失败语义在 get_backend_or_none 已局部存在，
    本函数扶正为主路径；消息带已注册清单便于排障。

    调用方对账（改前盘点）：裸调点仅 executor.py:918（暗雷入口，期望受益）；
    cloud_worker.py:789 已 try/except 包裹（抛错后走 fail log，语义正好成立）；
    vcs/adapter.py:49 是同名不同函数（显式 None 判定），不受影响。
    """
    if name not in _BACKENDS:
        raise KeyError(
            f"unknown agent backend: {name!r}; registered backends: "
            f"{sorted(_BACKENDS.keys())}"
        )
    return _BACKENDS[name]


def get_backend_or_none(name: str | None = "") -> AgentBackend | None:
    """[沙箱迁移] 按名取 backend，未注册返回 None（不 fallback opencode）。

    bwrap.py 的沙箱 setup 链按引擎名精确取 backend；未注册的引擎名
    返回 None 走未配置兜底。注：i7 侧 bwrap.py 一直引用本符号但从未定义
    （云沙箱路径不经过 bwrap 故未暴露），本次移植补齐定义。
    """
    if not name:
        return None
    return _BACKENDS.get(name)


def discover_backends() -> list[dict]:
    """动态发现本地已安装的 Agent 引擎（跨平台）。

    Unix: shutil.which() 查 PATH。
    Windows: shutil.which() 查 PATH（含 PATHEXT）+ os_adapter 额外查找常见安装目录。
    [P0b B3] 引擎清单单源=engine/descriptors/*.json（discover_map 派生），
    drift 守卫 scripts/engine_descriptor_drift.py 对账——新增引擎只落 descriptor。
    """
    from src.domains.runtime.engine.os_adapter import find_cli
    from src.domains.runtime.engine.descriptor import get_registry
    known = get_registry().discover_map()
    results = []
    for name, info in known.items():
        cli_path = find_cli(info["cli"])
        installed = bool(cli_path)
        # CLI 同名防误识别（2026-11-10 实证）：`aishell` 不是 dsh 发行版的独占名——
        # 其它 aishell 工具（如 DevSpace 镜像内同名 CLI）同样命中 find_cli。能力判别：
        # dsh 发行版独有 `acp` 子命令（`acp --help` 输出含 Agent Client Protocol），
        # 缺该能力的同名 CLI 不是 aishell-dsh 引擎，installed 判 False——避免假引擎行
        # 注册（服务端 register 会把上报的引擎行点亮 online，假行 = 派单黑洞）。
        if installed and info.get("capability") == "acp":
            from src.domains.runtime.agents.aishell_acp import aishell_dsh_capable
            installed = bool(cli_path) and aishell_dsh_capable(cli_path)
        results.append({
            "name": name,
            "installed": installed,
            "cli": info["cli"],
            "cli_path": cli_path if installed else "",
            "pip_package": info["pip"],
        })
    return results


def _redact(text, tokens):
    """将文本中的敏感 token 替换为 [REDACTED]（用于日志/事件脱敏）

    [沙箱迁移] P1-8: 内部委托 RedactEngine，支持精确值 + 正则模式两层令牌化。
    向后兼容：签名不变，tokens 参数转为 RedactEngine.secret_values。
    每次调用创建临时引擎——stream_agent 路径无法共享实例。
    """
    if not text:
        return text if text else ""
    from src.domains.runtime.agents.sandbox.redact import RedactEngine
    if not tokens:
        return RedactEngine().redact(str(text)).text
    engine = RedactEngine(secret_values=tokens)
    result = engine.redact(str(text)).text
    engine.clear()  # 确定性清除，不依赖 GC
    return result


def _terminate_proc(proc, label: str = "", grace_seconds: float = 10.0):
    """优雅终止 agent 进程树。

    流程：SIGTERM 整个进程组 → 等待 ≤ grace_seconds → 超时 SIGKILL 兜底 → proc.wait() 确认回收。
    agent 子进程由 start_new_session=True 放入独立进程组，killpg 可连带清理其 fork 出的
    node/git/shell 等孙进程，避免残留。Windows 使用 taskkill /T /F 终止整个进程树。
    """
    if proc.poll() is not None:
        return
    from src.domains.runtime.engine.os_adapter import is_windows, kill_process_tree
    if is_windows():
        # Killing only the npm/.cmd launcher can leave the native CLI and tools
        # running. Use the owned PID and wait for its process tree to be killed.
        if not kill_process_tree(proc.pid, force=True) and proc.poll() is None:
            raise RuntimeError(f"{label}: failed to terminate process tree pid={proc.pid}")
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            log.warning(f"{label} process tree pid={proc.pid} still alive 5s after taskkill")
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


def _safe_terminate(proc, label: str = "", grace_seconds: float = 10.0) -> bool:
    """容错版 _terminate_proc：终止失败只记日志，不向上抛。

    Windows 下 taskkill /T /F 失败会抛 RuntimeError（_terminate_proc 新语义）。
    杀不掉引擎不该让 worker 整体退出，也不该覆盖调用方正在处理的原始异常
    （如 finally 块里覆盖 try 内的真实错误）。返回是否成功终止。
    """
    try:
        _terminate_proc(proc, label, grace_seconds=grace_seconds)
        return True
    except Exception as _te:
        log.warning(f"{label} terminate failed: {_te}")
        return False


# ── 引擎异常退出遗言采集与自愈（issue #317）──
# 已观测 seq 10353/10392：引擎 exit 1、stderr 空、result 截断，事后零线索无法定位。
_LOG_TAIL_MAX_LINES = 50
_LOG_TAIL_MAX_BYTES = 4096
_OOM_KEYWORDS = ("out of memory", "oom-killer", "oom_reaper", "killed process")

# ── stderr 尾采环形缓冲（issue #416）──
# 旧逻辑只保留 stderr 前 200/500 字节，崩溃栈在末尾被截断丢光（E7：hermes
# 启动即死零诊断）。改为保留末尾 ≥2000 字节，可用 HERMES_STDERR_TAIL_BYTES 调整。
STDERR_TAIL_BYTES_DEFAULT = 2000


def _stderr_tail_bytes_for(env: dict | None = None) -> int:
    """stderr 尾采字节上限（HERMES_STDERR_TAIL_BYTES 可调，默认 2000，下限 200）。"""
    _src = (env or {}).get("HERMES_STDERR_TAIL_BYTES") or \
        os.environ.get("HERMES_STDERR_TAIL_BYTES", str(STDERR_TAIL_BYTES_DEFAULT))
    try:
        return max(int(float(_src)), 200)
    except Exception:
        return STDERR_TAIL_BYTES_DEFAULT


class _StderrTailBuffer:
    """stderr 尾采环形缓冲：仅保留末尾 max_bytes 字节（崩溃栈在末尾，issue #416）。

    内存有界：累计超过 2×max_bytes 时折叠为末尾 max_bytes；text() 恒返回末尾
    ≤ max_bytes 字节，头部日志（启动横幅等低价值信息）自然淘汰。
    """

    __slots__ = ("max_bytes", "_chunks", "_total")

    def __init__(self, max_bytes: int = STDERR_TAIL_BYTES_DEFAULT):
        self.max_bytes = max(int(max_bytes), 200)
        self._chunks: list[str] = []
        self._total = 0

    def append(self, s: str):
        if not s:
            return
        self._chunks.append(s)
        self._total += len(s)
        if self._total > self.max_bytes * 2:
            joined = "".join(self._chunks)
            keep = joined[-self.max_bytes:]
            self._chunks = [keep]
            self._total = len(keep)

    def text(self) -> str:
        joined = "".join(self._chunks)
        return joined[-self.max_bytes:] if len(joined) > self.max_bytes else joined


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


def _stream_acp(prompt, cwd, session_id, event_type, backend,
                account, user_id, env_extra, bot_token, user_token,
                agent_name, agent_label, max_exec_seconds,
                repo_key: str = "") -> Generator:
    """ACP 引擎统一入口（P0a A3，2026-11-13 spec：三份同构 _stream_*_acp 收敛）。

    分派判定 = backend 自报 acp_backend() 能力钩子（与引擎名解耦，duck-typed；
    不用 protocol 属性——云沙箱路径已用其判定 hermes 专用 ACP 云分支，本机
    分派不得连带改变云路径语义）。引擎 quirk 全部经钩子注入：
    - env 组装：acp.get_env（各引擎自带凭据/环境注入）；
    - prepare_env（可选，aishell 形态）：env_extra 合并后落 0600 token 文件，
      结束时清 cwd 下 .grape-sess-*.tok（含 kill 后残留的 glob 兜底）；
    - 事件流：acp.stream_execute（hermes/hwcloud/aishell 的差异已收编到
      各自客户端薄子类，见 acp_client.py）。
    脱敏/占位符/事件收口与原三函数逐行同构（等价性由 C02-C04 E2E 闸门守护）。
    """
    acp = backend.acp_backend()

    label = f"{agent_label} " if agent_label else ""

    # 基线清理 helper 的 cutoff 锚（只清本任务窗口内创建的 token 文件，不误删并发任务文件）
    task_started_at = time.time()

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
    _apply_runtime_env_contract(env, event_type=event_type, account=account,
                                repo_key=repo_key, label=label,
                                backend_name=(getattr(backend, "name", "") or ""))

    # 任务级 env 合并完成后的收尾钩子（aishell 形态；同原通用 CLI 路径语义）：
    # aishell（dsh 框架）会 scrub env 里 /TOKEN/i 变量，prepare_env 把
    # GRAPE_SESSION_TOKEN 落成 0600 临时文件 + GRAPE_AUTH_FILE 穿透。
    # 必须在 env_extra 之后：token 是任务级的，此刻才进 env。
    _auth_file = None
    _prepare_env = getattr(acp, "prepare_env", None)
    _has_prepare_env = callable(_prepare_env)
    if _has_prepare_env:
        try:
            # 传 cwd：bwrap 沙箱 /tmp 为私有 tmpfs，token 文件须落工位目录才沙箱内可见
            _auth_file = _prepare_env(env, cwd=cwd)
        except Exception as _pe:
            log.warning(f"{label}prepare_env failed: {_pe}")

    # 技能路径占位符替换（direct 模式：按 skills/<agent_type>/<skill>/ 的结构解析）
    try:
        from src.kernel.config import replace_skill_dir_placeholders
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

    try:
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
    finally:
        _cleanup_session_auth_file(cwd, _auth_file, task_started_at)


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
    # user_id 类型归一：chat 链路传 str（user["id"]），devops 链路传 int——
    # 在汇点统一转 int（str 不可比 int，未归一会 TypeError 中断整个任务）。
    # 归一失败静默为 0 会让 scope=user worker 认领不到（仓库规则：任务 user_id
    # 必须正确），故失败必须留痕（QA #1230 I3：静默降级不可接受）。
    try:
        user_id = int(user_id)
    except (TypeError, ValueError):
        log.warning(f"_assemble_env: user_id not int-coercible ({user_id!r}), "
                    f"degrading to 0 — user-scoped credential injection disabled")
        user_id = 0
    # grape CLI PATH 注入（Windows 部署缺口：install-worker.ps1 未像 install-worker.sh:615
    # 把 worker venv bin 注册进 PATH，agent shell 里 `grape` 找不到——2026-11-07 真任务
    # 实证 event 1）。目录存在才前缀注入；机器级 PATH 修复后此注入自然冗余无害。
    # agent 调用继续用裸 `grape`，无需绝对路径/prompt 模板改动。
    #
    # 为什么不用「Linux 同等方法」（把 venv bin 注册进用户/机器 PATH）——四个实测/结构性风险：
    #   1. 影子命令：worker venv Scripts 里有 python.exe/pip.exe 等，注册进用户 PATH 会
    #      shadow 全机 python/pip（真机 ls 实证 12 个 exe）；
    #   2. setx 1024 字符截断：用户 PATH 超长被静默截断，直接损坏；
    #   3. 非幂等：重复安装/升级重复追加，卸载通常不清理 → PATH 残骸累积；
    #   4. 作用域错位：Linux unit Environment 仅服务进程可见，Windows 用户 PATH 全局生效
    #      且已运行进程不重启不生效。
    # 正规部署修复应注册「只含 grape 启动器 shim 的专用目录」（不含 python/pip），见
    # plan 文档 §5 TODO；本注入与该修复可并存。
    try:
        from src.domains.runtime.engine.os_adapter import default_install_dir
        _gbin = os.path.join(default_install_dir(), ".venv",
                             "Scripts" if os.name == "nt" else "bin")
        _gexe = "grape.exe" if os.name == "nt" else "grape"
        if os.path.isfile(os.path.join(_gbin, _gexe)):
            env["PATH"] = _gbin + os.pathsep + env.get("PATH", "")
    except Exception:
        pass
    # 平台 VCS token 不再注入（P2）。bot_token/user_token 保留仅作日志脱敏用途，
    # 若确需在此记录是否持有，仅 print 长度摘要即可，值绝不进 env。
    # Issue #344: 改用带预刷新的 token 获取函数，避免拿到过期 token。
    if kind == "chat":
        if not user_token and has_user_id(user_id):
            try:
                # D37/S57：repo 上下文按 repo_key 前缀派生 provider；无 repo_key 时
                # 按用户已绑定身份的第一条（由数据决定，代码不写平台名）
                from src.api.deps import get_user_token_for, get_user_token_for_repo
                from src.domains.mgmt.public import get_grape_account_by_id
                _acc = get_grape_account_by_id(user_id)
                if _acc:
                    if repo_key:
                        user_token = get_user_token_for_repo(repo_key, _acc["username"],
                                                             user_id=user_id) or ""
                    else:
                        from src.domains.mgmt.public import list_platform_links
                        # A 批：兜底平台也要覆盖"只有 PAT、无 OAuth 身份行"的账号
                        _ids = list_platform_links(str(user_id)) or []
                        _prov = str((_ids[0] or {}).get("provider") or "") if _ids else ""
                        user_token = (get_user_token_for(_acc["username"], user_id=user_id,
                                                         provider=_prov) or "") if _prov else ""
            except Exception as e:
                log.warning(f"Fallback resolve user platform token failed: {e}")
                user_token = ""
        if user_token:
            log.info(f"{label}chat task: user token present (len={len(user_token)}), NOT injecting into env (P2)")
        else:
            log.warning(f"{label}NO user platform token available for chat; VCS ops go through grape CLI + session token")
        # chat env 兜底（Issue #1757）：仅补缺失键，env_extra 显式值优先，绝不覆盖。
        # Issue #1757 复审：仓库身份统一走 derive_repo_identity（与 chat_repo_env / 事件链同源），
        # 不再自己调 split_owner_repo，避免两份派生逻辑漂移。
        if repo_key and not repo_key.startswith("chat:") and "REPO_OWNER" not in env:
            try:
                from src.kernel.repo_key import derive_repo_identity
                full_name, owner, name = derive_repo_identity(repo_key, repo_key)
                if owner and name:
                    env.setdefault("REPO_FULL_NAME", full_name or repo_key)
                    env.setdefault("REPO_OWNER", owner)
                    env.setdefault("REPO_NAME", name)
            except Exception as e:
                log.warning(f"{label}chat env fallback repo split failed: {e}")
        if "GRAPE_SERVER_URL" not in env:
            try:
                from src.kernel.prompt_builder import _local_port
                env["GRAPE_SERVER_URL"] = f"http://127.0.0.1:{_local_port()}"
            except Exception as e:
                log.warning(f"{label}chat env fallback server url failed: {e}")
    elif bot_token:
        log.info(f"{label}devops event task: bot token present (len={len(bot_token)}), NOT injecting into env (P2)")
        # GitHub bot token 不再注入（agent 侧一律 grape CLI 转发）

    # Issue #1858：旧快照/旁路路径三面基址兜底（仅补缺失键，不覆盖显式值）。
    # A2A 回退 worker 回连基址（控制面语义），不与业务基址混用。
    if env.get("GRAPE_SERVER_URL"):
        env.setdefault("GRAPE_VCS_BASE_URL", env["GRAPE_SERVER_URL"])
        env.setdefault("GRAPE_ISSUE_BASE_URL", env["GRAPE_SERVER_URL"])
        env.setdefault("GRAPE_A2A_BASE_URL",
                       env.get("RUNTIME_SERVER_URL") or env["GRAPE_SERVER_URL"])

    # VCS API base URL + 名称注入（skill 中 $VCS_API_BASE 引用，适配多 VCS provider）
    if repo_key:
        try:
            from src.kernel.prompt_builder import _vcs_api_base, _vcs_name
            env["VCS_API_BASE"] = _vcs_api_base(repo_key)
            env["VCS_NAME"] = _vcs_name(repo_key)
        except Exception as e:
            log.warning(f"Failed to inject VCS_API_BASE: {e}")

    # 用户云凭据注入（devops 的 user_id=仓库归属用户，见 event_pipeline _issue_scene_uid）
    # #219：huawei_cloud / codearts 的 AK-SK 不再经 env 注入（按需获取或自定义场景变量），跳过。
    if has_user_id(user_id):
        try:
            from src.domains.mgmt.public import get_credentials
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
            from src.kernel.db_errors import missing_schema
            if missing_schema(e):
                # worker 本地库 user_credentials 缺 kind 列：任务照常跑（token 由
                # claim 现取注入），只记 debug，不再每个任务刷一条 WARNING。
                log.debug(f"user credential injection skipped (no local schema): {e}")
            else:
                log.warning(f"Failed to inject user credentials: {e}")

# ── 运行时环境变量注入契约（Issue #2395）──────────────────────────────────
# 四个变量在 VOD/Issue/Coding/QA 各引擎路径必须口径一致：
#   DEVOPS_EVENT_TYPE  真实事件类型（唯一口径 = executor.stream_agent 的 event_type 参数）
#   ACCOUNT            触发人账号（不是仓库名/仓库全名，历史 Issue #1746）
#   GRAPE_ISSUE_ID     平台内部需求卡片 id（issue 表主键，历史 Issue #667；≠ ISSUE_NUMBER）
#   GRAPE_WORK_BRANCH  场景开发基线分支（服务端场景/Issue 快照解析，历史 Issue #2322）
# 各引擎 get_env() 不再预置空串；任务级 env_extra 合并后由本函数统一补齐/覆盖，
# 缺失时输出可诊断告警（仅变量名与引擎标识，不打印值），并降级继续执行不阻断任务。
_RUNTIME_CONTRACT_ENV = (
    "DEVOPS_EVENT_TYPE",
    "ACCOUNT",
    "GRAPE_ISSUE_ID",
    "GRAPE_WORK_BRANCH",
)

# Issue #3905：issue 任务的三变量（DEVOPS_EVENT_TYPE 恒有值，不参与「全缺」判定）
_RUNTIME_CONTRACT_ISSUE_VARS = ("ACCOUNT", "GRAPE_ISSUE_ID", "GRAPE_WORK_BRANCH")

# 连续「三变量全缺」的告警阈值：单条任务缺变量只是 WARNING（24h 949 条淹没在噪声里），
# 连续 N 次全缺说明派发侧整条链路丢 env（正是 #3905 的生产形态），必须升级为 ERROR。
RUNTIME_CONTRACT_STREAK_ALERT = int(os.environ.get("GRAPE_ENV_CONTRACT_STREAK_ALERT", "10") or 10)

_contract_streak: dict = {}
_contract_streak_lock = threading.Lock()


def reset_runtime_env_contract_streak() -> None:
    """清空连续全缺计数（测试 seam / 进程内告警重置）。"""
    with _contract_streak_lock:
        _contract_streak.clear()


def _runtime_agent_type(label: str) -> str:
    """从执行标签解析 agent_type（低基数维度）。

    标签形如 ``"[Agent:coding|repo:o/r|engine:hermes-1] "`` / ``"[Autopilot:issue|repo:…]"``；
    解析失败归 ``unknown``，repo/issue 号不进指标标签（防高基数）。
    """
    try:
        s = (label or "").strip()
        if s.startswith("["):
            s = s[1:]
        head = s.split("|", 1)[0]
        if ":" in head:
            # 截断 40 与 insight.observability 的 backend/agent_type 标签同口径
            # （短枚举 + 前缀余量，见 record_runtime_env_contract_missing 注释）；
            # 这里只取 `[agent:xxx]` 标签里的 agent_type，超长值必然是拼接串。
            return (head.split(":", 1)[1] or "").strip()[:40] or "unknown"
    except Exception:
        pass
    return "unknown"


def _record_contract_missing(missing: list, backend_name: str, label: str,
                             issue_scoped: bool = True) -> None:
    """Issue #3905：契约缺失结构化计数 + 连续全缺升级告警（观测不得拖垮任务）。

    ``issue_scoped``（PR #2170 复审 Optional）：只有 Issue 域任务参与「连续全缺」计数。
    autopilot / chat 任务本就没有 ISSUE 变量，此前任一条非 issue 任务以
    ``full_missing=false`` 就把进程级单计数清零、且跨 backend/agent_type 混合累计，
    混合流量下「连续 N 次三变量全缺」可能永不触发。改为按 ``(backend, agent_type)``
    分桶、非 issue 域直接跳过。
    """
    agent_type = _runtime_agent_type(label)
    try:
        from src.domains.insight.observability import record_runtime_env_contract_missing
        for _name in missing:
            record_runtime_env_contract_missing(backend_name, agent_type, _name)
    except Exception as _obs_e:  # noqa: BLE001 - 指标失败只降级 debug
        log.debug(f"runtime env contract metric forward failed: {_obs_e}")
    if not issue_scoped:
        return
    full_missing = all(name in missing for name in _RUNTIME_CONTRACT_ISSUE_VARS)
    key = (backend_name or "", agent_type)
    with _contract_streak_lock:
        if full_missing:
            _contract_streak[key] = _contract_streak.get(key, 0) + 1
        else:
            _contract_streak[key] = 0
        streak = _contract_streak[key]
    limit = max(1, RUNTIME_CONTRACT_STREAK_ALERT)
    if full_missing and streak >= limit and streak % limit == 0:
        log.error(f"{label}runtime env contract missing on {streak} consecutive tasks: "
                  f"{','.join(missing)} (backend={backend_name or '-'}, agent={agent_type}); "
                  f"dispatch path is dropping env — check agent_tasks.env_extra snapshot "
                  f"(Issue #3905)")


def _runtime_contract_scope(env: dict, event_type: str) -> tuple:
    """Issue #3905：本次任务实际适用的契约变量集合。

    非 Issue 域任务（autopilot 定时任务 / chat）本就没有 Issue 编号：
    ``GRAPE_ISSUE_ID``（卡片 id）与 ``GRAPE_WORK_BRANCH``（场景基线）不适用，
    继续按「缺失」告警会让 Loki 的 24h 计数永远无法归零（每任务一条噪声）。
    判据 = 是否带 Issue 上下文（ISSUE_NUMBER / GRAPE_ISSUE_ID 任一非空）
    或事件类型不属于 autopilot/chat；其余情形只校验 DEVOPS_EVENT_TYPE + ACCOUNT。
    """
    _et = (event_type or "").strip().lower()
    _issue_scoped = bool(str(env.get("ISSUE_NUMBER") or "").strip()) or \
        bool(str(env.get("GRAPE_ISSUE_ID") or "").strip())
    if not _issue_scoped and (_et.startswith("autopilot") or _et == "chat"):
        return ("DEVOPS_EVENT_TYPE", "ACCOUNT")
    return _RUNTIME_CONTRACT_ENV


def _apply_runtime_env_contract(env: dict, *, event_type: str = "", account: str = "",
                                repo_key: str = "", label: str = "",
                                backend_name: str = "") -> dict:
    """任务级 env 合并后统一补齐运行时契约四变量（Issue #2395）。

    - DEVOPS_EVENT_TYPE 以本次真实 event_type 为权威值（覆盖 env_extra 中的空串/旧值）；
    - ACCOUNT 缺失时回退调用方 account，但拒绝把仓库标识（含 `/`/`:`）误当账号；
    - GRAPE_ISSUE_ID 缺失时按 ISSUE_NUMBER（仓库 Issue 编号）解析内部卡片 id，
      语义与 ISSUE_NUMBER 区分：前者是 issue 表主键，后者是仓库 Issue 编号；
    - GRAPE_WORK_BRANCH 缺失时回退场景配置（与 event_pipeline 同源）：回退查询按
      **仓库 Issue 编号**（ISSUE_NUMBER）索引 issue 表，不得用内部卡片 id（GRAPE_ISSUE_ID）——
      后者是 issue 表主键，命中恒错键会让场景回退恒空。
    任一必需变量仍缺失 → WARNING（只列变量名 + 引擎/agent 标签，不打印值），不阻断；
    Issue #3905：非 Issue 域任务（autopilot / chat）只校验 DEVOPS_EVENT_TYPE + ACCOUNT
    （见 ``_runtime_contract_scope``），缺失同时进结构化指标，连续三变量全缺升级 ERROR。
    """
    if event_type:
        env["DEVOPS_EVENT_TYPE"] = event_type
    if not (env.get("ACCOUNT") or "").strip():
        candidate = (account or "").strip()
        if candidate and "/" not in candidate and ":" not in candidate:
            env["ACCOUNT"] = candidate
    if not (env.get("GRAPE_ISSUE_ID") or "").strip():
        issue_number = str(env.get("ISSUE_NUMBER") or "").lstrip("#").strip()
        if issue_number and repo_key:
            try:
                from src.domains.runtime.pipeline.event_pipeline import resolve_issue_card_id
                card_id = resolve_issue_card_id(repo_key, issue_number)
                if card_id:
                    env["GRAPE_ISSUE_ID"] = card_id
            except Exception as exc:
                log.warning(f"{label}runtime env contract: GRAPE_ISSUE_ID resolve failed "
                            f"(backend={backend_name or '-'}): {exc}")
    if not (env.get("GRAPE_WORK_BRANCH") or "").strip() and repo_key:
        try:
            from src.domains.runtime.engine.repogit import _read_scene_work_branch
            # R1（PR #1460 审查）：_read_scene_work_branch 的 issue_id 形参按
            # issue 表 issue_number（仓库 Issue 编号）索引，与 direct_executor 同口径；
            # 传 GRAPE_ISSUE_ID（内部卡片 id）会命中恒错键，回退查询恒空。
            work_branch = _read_scene_work_branch(repo_key, env.get("ISSUE_NUMBER") or "")
            if work_branch:
                env["GRAPE_WORK_BRANCH"] = work_branch
        except Exception as exc:
            log.warning(f"{label}runtime env contract: GRAPE_WORK_BRANCH resolve failed "
                        f"(backend={backend_name or '-'}): {exc}")
    _scope = _runtime_contract_scope(env, event_type)
    missing = [name for name in _scope if not (env.get(name) or "").strip()]
    if missing:
        log.warning(f"{label}runtime env contract missing: {','.join(missing)} "
                    f"(backend={backend_name or '-'}); task continues with degraded env")
        # Issue #3905：单行 WARNING 无法聚合/告警 → 结构化计数（backend/agent_type/var）
        # + 连续三变量全缺升级 ERROR（派发侧整链丢 env 的确定性信号）。
        # 连续计数只统计 Issue 域任务（autopilot/chat 无 ISSUE 变量，不得稀释告警）。
        _record_contract_missing(missing, backend_name, label,
                                 issue_scoped=(_scope == _RUNTIME_CONTRACT_ENV))
    return env


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
    """流式执行 Agent（public wrapper）。

    dsh 在通用 CLI 分支创建的 token 文件必须在整个生成器生命周期结束后清理，
    包括正常完成、异常退出和调用方提前关闭生成器的路径。

    Issue #1982：为本轮所有引擎子进程建立 fd 收口作用域——正常结束由
    _untrack_proc 关闭管道；异常/cancel/requeue/消费方提前中断/GeneratorExit
    由 finally 兜底 terminate + 关闭 stdin/stdout/stderr，避免 pipe fd 泄漏。
    """
    _auth_state: dict = {"started_at": time.time()}
    prev_scope = _scope_procs()
    _exec_scope.procs = set()
    _exec_scope.reader_threads = set()
    try:
        yield from _stream_agent_impl(
            prompt, cwd, session_id=session_id, event_type=event_type,
            backend_name=backend_name, account=account, user_id=user_id,
            env_extra=env_extra, bot_token=bot_token, user_token=user_token,
            agent_name=agent_name, agent_label=agent_label,
            repo_key=repo_key, max_exec_seconds=max_exec_seconds,
            _auth_state=_auth_state,
        )
    finally:
        if _auth_state.get("prepare_env_attempted"):
            _cleanup_session_auth_file(
                cwd, _auth_state.get("auth_file"), _auth_state.get("started_at"))
        _close_exec_scope(prev_scope)


def _stream_agent_impl(prompt: str, cwd: str, session_id: str = "",
                       event_type: str = "", backend_name: str = "",
                       account: str = "", user_id: int = 0,
                       env_extra: dict | None = None,
                       bot_token: str = "", user_token: str = "",
                       agent_name: str = "",
                       agent_label: str = "",
                       repo_key: str = "",
                       max_exec_seconds: int = 600,
                       _auth_state: dict | None = None) -> Generator:
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
    # text_separator 是 AgentRuntime 基类的新增能力；自定义后端可能没有——缺失按换行拼接。
    _text_sep = getattr(backend, "text_separator", "\n")

    # ── ACP 引擎统一路径（P0a A3：按 backend 自报 acp_backend() 能力分派，不按引擎名）──
    # 新接入 ACP 引擎 = 薄 backend 子类（含 quirk）+ _BACKENDS 一行注册，零 executor 改动。
    _acp_factory = getattr(backend, "acp_backend", None)
    if callable(_acp_factory):
        yield from _stream_acp(
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
    _apply_runtime_env_contract(env, event_type=event_type, account=account,
                                repo_key=repo_key, label=label,
                                backend_name=(getattr(backend, "name", "") or ""))

    # 任务级 env 合并完成后的收尾钩子（可选，backend 自行实现）。
    # dsh 在这里把 GRAPE_SESSION_TOKEN 落成 0600 临时文件 + GRAPE_AUTH_FILE，
    # 绕过它 subprocess 服务的 /TOKEN/i scrub。必须放在 env_extra 之后：
    # token 是任务级的，get_env() 执行时它还不在 env 里（#948 曾写在 get_env 里，
    # 读到的永远是空串，临时文件从未生成）。
    _auth_file = None
    try:
        _prepare_env = getattr(backend, "prepare_env", None)
        if callable(_prepare_env):
            if _auth_state is not None:
                _auth_state["prepare_env_attempted"] = True
            _auth_file = _call_prepare_env(_prepare_env, env, cwd)
            if _auth_state is not None:
                _auth_state["auth_file"] = _auth_file
    except Exception as _pe:
        log.warning(f"{label}prepare_env failed: {_pe}")

    # 技能路径占位符：worker 侧已在物化时把 {{SKILL_DIR}} 替换为随任务下发的目录；
    # direct（服务端本地）执行在此按 skills/<agent_type>/<skill>/ 结构解析，使 read tool 能找到技能文件。
    try:
        from src.kernel.config import replace_skill_dir_placeholders
        prompt = replace_skill_dir_placeholders(prompt)
    except Exception:
        pass

    # 需要脱敏的 token 列表（用于日志/事件输出；_assemble_env 已把 token/凭据注入 env）
    _tokens = [t for t in (bot_token, user_token) if t]

    # codearts 方案E2: serve 启动移到 _run_once 内部（每次执行启动自己的 serve，避免单例并发串号）
    # pre_exec/post_exec 由 _run_once 内部调用，serve 信息用局部变量

    # issue #351: 统一走 os_adapter.find_cli（Windows exe 优先），
    # 移除 shutil.which(binary + ".cmd") 主动查找（cmd 壳会截断多行提示词）。
    from src.domains.runtime.engine.os_adapter import find_cli, is_shell_wrapper
    agent_cmd = find_cli(backend.binary)
    if not agent_cmd:
        log.error(f"{label}{backend.name} CLI not found in PATH")
        yield ("done", {
            "exit_code": -1, "text": "", "oc_session_id": session_id,
            "errors": [f"{backend.name} not found"], "stderr": ""
        })
        return
    # issue #351: cmd/bat 壳兜底降级为 WARN——Windows 上 .cmd/.bat 隐式经
    # cmd.exe 执行，argv 中的换行符被视为命令分隔符，多行提示词会被截断成第一行。
    if is_shell_wrapper(agent_cmd):
        log.warning(
            f"{label}{backend.name} CLI resolved to shell wrapper '{agent_cmd}' "
            f"(.cmd/.bat): multi-line prompts may be truncated on Windows "
            f"(issue #351); prompt_via_stdin={backend.prompt_via_stdin}; "
            f"prefer installing the engine's .exe"
        )

    # 单次进程执行：返回 (full_text, new_session_id, exit_code, stderr, error_events)
    def _run_once(use_session_id: str):
        # prepare_prompt 是 AgentRuntime 基类的新增能力；自定义后端（不继承基类）
        # 可能没有——缺失时退化为原行为，不让执行直接崩。
        _prepare = getattr(backend, "prepare_prompt", None)
        if _prepare is None:
            with contextlib.nullcontext(prompt) as invocation_prompt:
                yield from _run_once_impl(use_session_id, invocation_prompt)
        else:
            with _prepare(prompt, cwd) as invocation_prompt:
                yield from _run_once_impl(use_session_id, invocation_prompt)

    def _run_once_impl(use_session_id: str, invocation_prompt: str):
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

        args = backend.build_args(invocation_prompt, use_session_id)
        log.info(f"{label}{backend.name} args: {' '.join(args)[:2000]}...")
        log.info(f"{label}{backend.name} cwd: {cwd}")

        stdin_pipe = subprocess.PIPE if backend.prompt_via_stdin else subprocess.DEVNULL
        _popen_kwargs = {}
        if hasattr(os, "getpgid"):  # POSIX：独立进程组，便于整组优雅终止
            _popen_kwargs["start_new_session"] = True

        # hermes CLI iso-home 隔离块已删（P0b 死代码清理）：P0a A3 后 hermes
        # 本机执行恒走 ACP（acp_backend 能力钩子在 CLI 段前 return），本块不可达。
        _proc_env = _serve_env  # codearts 方案E2: 用 pre_exec 返回的 env（含 CODEARTS_SERVER_PASSWORD）

        # 组件2：文件系统隔离（issue #500：GRAPE_AGENT_SANDBOX 关闭时先短路，
        # 不进入 sandbox 导入/包装/告警路径，杜绝 wrap failed 刷屏噪音）
        _cmd = [agent_cmd] + args
        try:
            from src.kernel.config import GRAPE_AGENT_SANDBOX as _sbx_on
        except Exception:
            _sbx_on = False
        if _sbx_on:
            try:
                from src.domains.runtime.agents.sandbox import build_bwrap_cmd
                _wrap = build_bwrap_cmd(cwd, backend_name=backend.name)
                if _wrap:
                    if backend.name == "atomcode":
                        # bwrap hides host /tmp. Expose only this invocation's
                        # prompt, outside the checkout and read-only.
                        _wrap = _wrap[:-1] + ["--ro-bind", invocation_prompt, invocation_prompt, "--"]
                    _cmd = _wrap + _cmd
                    log.info(f"{label}{backend.name} sandboxed via bwrap (cwd={cwd})")
            except Exception as _bw_e:
                _note_bwrap_fail_once(
                    f"{label}{backend.name} bwrap wrap failed, running unsandboxed: {_bw_e}")

        # 纯管道启动（与 main 一致；script 包装为临时对照，已按分析移除）
        proc = subprocess.Popen(
            _cmd,
            stdin=stdin_pipe, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            cwd=cwd, env=_proc_env, bufsize=0, **_popen_kwargs
        )
        _track_proc(proc, backend=backend.name, cwd=cwd)
        # issue #351: prompt 经 stdin 传输（不进 argv，规避 cmd 壳多行截断）
        _stdin = getattr(proc, "stdin", None)
        if backend.prompt_via_stdin and _stdin is not None:
            _stdin.write(prompt.encode("utf-8"))
            _stdin.close()

        collected_text = []
        new_session_id = use_session_id
        error_events = []
        _terminal_seen = False
        # atomcode 用量兜底：正常路径每轮 usage 事件直报；若进程被提前终止导致
        # usage 事件缺失，退回 turn.completed 的整轮汇总（同一口径，不重复计入）。
        _usage_seen = False
        _usage_fallback = None
        # issue #317：引擎遗言尾部缓冲（exit!=0 且 stderr 空时补采进 errors）
        _log_tail = []
        step_count = 0
        _last_output = time.time()
        _reader_total = 0
        _text_count = 0      # 本次执行 text 事件数（假完成判定用，issue-P5）
        _tool_count = 0      # 本次执行 tool_use 事件数（假完成判定用，issue-P5）
        # 静默超时窗口：首个无输出窗口。与 hermes ACP _SILENT_TIMEOUT=600s 对齐，
        # 可用 AGENT_SILENT_WINDOW 环境变量覆盖（模型流挂死 10 分钟即感知，不再干等 20min）。
        # Issue #378: 优先读任务级 env dict（含 env_extra，由 _execute_task 按 agent_type 注入
        # coding/qa=1200s），os.environ 次之（手动调参/测试），默认 min(600, max_exec_seconds)。
        _silent_window = float(env.get("AGENT_SILENT_WINDOW",
            os.environ.get("AGENT_SILENT_WINDOW", str(min(600, max_exec_seconds)))))
        # 进程存活但无输出的累计延长上限（防长命令/长思考无上限；超硬上限仍强杀，见 #501 修复）
        # 可用 AGENT_SILENT_EXTEND_CAP 调整（旧变量 AGENT_SILENT_LIMIT 亦兼容，语义=续期额度上限）。
        _silent_extend_total = 0.0
        _silent_extend_cap = float(os.environ.get("AGENT_SILENT_EXTEND_CAP",
                                                   os.environ.get("AGENT_SILENT_LIMIT", str(max_exec_seconds))))
        # 超时总账（issue-P1）：单次执行最坏 ≈ _silent_window + _silent_extend_cap + grace 10s；
        # RUNTIME_TASK_TIMEOUT=3600s 需 ≥ 2×(单次最坏) 才能容纳一次无效完成自动重跑。
        # 600 + 600 + 10 = 1210s → 2 次 ≈ 2420s < 3600s，安全。

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

        # stderr reader 线程：持续排空 stderr 管道，防止 Windows 管道缓冲区满导致死锁；
        # 尾采环形缓冲（issue #416）：仅保留末尾 ≥2000 字节，崩溃栈在末尾不丢
        _stderr_tail = _StderrTailBuffer(_stderr_tail_bytes_for(env))
        def _stderr_reader():
            try:
                for raw_line in iter(proc.stderr.readline, b""):
                    _stderr_tail.append(raw_line.decode("utf-8", errors="replace"))
            except Exception:
                pass
        _stderr_thread = threading.Thread(target=_stderr_reader, daemon=True)
        _stderr_thread.start()
        # Issue #1982 复审：reader 线程登记进作用域，异常/cancel/requeue 兜底路径
        # （_close_exec_scope）在 terminate+close 产生 EOF 后统一 join 回收。
        _track_reader_threads(_reader_thread, _stderr_thread)

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
                    _terminal_seen = _terminal_seen or parsed.get("terminal", False)
                    if backend.name == "atomcode" and parsed.get("usage_total"):
                        _usage_fallback = parsed["usage_total"]
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
                        if backend.name == "atomcode" and parsed.get("usage"):
                            _usage_seen = True
                            yield ("usage", parsed["usage"])
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
                    # 终止失败不吞掉 done 事件：收尾窗口照常走，失败原因进 errors
                    if not _safe_terminate(proc, f"{label}{backend.name}"):
                        error_events.append(f"{backend.name} terminate failed")
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
                            if backend.name == "atomcode":
                                _terminal_seen = _terminal_seen or _parsed.get("terminal", False)
                                if _parsed.get("usage_total"):
                                    _usage_fallback = _parsed["usage_total"]
                                if _parsed["type"] == "error":
                                    error_events.append(_redact(_parsed["message"], _tokens))
                            if _parsed["type"] == "text":
                                collected_text.append(_parsed["text"])
                            elif _parsed["type"] == "tool_use":
                                pass
                    # 若收尾窗口内收到输出，视为 agent 已收尾（exit 以进程实际状态为准，不再强制 -9）
                    _killed = "-9" if _grace_tail.strip() == "" else "grace"
                    _grace_exit = -9 if not _grace_tail.strip() else (proc.returncode if proc.returncode is not None else -1)
                    if backend.name == "atomcode":
                        if not _terminal_seen:
                            error_events.append("AtomCode JSONL ended without a terminal event")
                        if error_events and _grace_exit == 0:
                            _grace_exit = 1
                    log.info(f"{label}{backend.name} hang fallback: tail_chars={len(_grace_tail)} outcome={_killed}")
                    # Issue #494：静默超时强杀必须带明确归因（不再只写英文 killed 短语）
                    from src.domains.runtime.agents.failure_attribution import timeout_error_text
                    if backend.name == "atomcode" and not _usage_seen and _usage_fallback:
                        yield ("usage", _usage_fallback)
                    yield ("done", {
                        "exit_code": _grace_exit,
                        "text": _redact(_text_sep.join(collected_text), _tokens),
                        "oc_session_id": new_session_id,
                        "errors": error_events + ([timeout_error_text(
                            _silent_window, f"{backend.name} 无输出静默超时被终止")] if _grace_tail.strip() == "" else []),
                        "stderr": _redact(_stderr_tail.text(), _tokens),
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
            if not _safe_terminate(proc, f"{label}{backend.name}"):
                error_events.append(f"{backend.name} terminate failed")
            _reader_thread.join(timeout=5)
            # Issue #1982 复审：terminate+close 已产生 EOF，stderr reader 同步 join 回收
            _stderr_thread.join(timeout=2)
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
        # Issue #1982 复审：正常结束路径 stdout 已 EOF，stderr reader 同步 join 回收
        _stderr_thread.join(timeout=2)

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
                if parsed and backend.name == "atomcode":
                    _terminal_seen = _terminal_seen or parsed.get("terminal", False)
                    if parsed.get("usage_total"):
                        _usage_fallback = parsed["usage_total"]
                    if parsed["type"] == "error":
                        error_events.append(_redact(parsed["message"], _tokens))
                    elif parsed["type"] == "step_start" and parsed.get("sessionID"):
                        new_session_id = parsed["sessionID"]
                    elif parsed.get("usage"):
                        yield ("usage", parsed["usage"])
                if parsed and parsed["type"] == "text":
                    collected_text.append(parsed["text"])

        full_text = _text_sep.join(collected_text)
        # issue #416：stderr 尾巴（末尾 ≥2000B，崩溃栈在末尾），不再保头 500B
        stderr_output = _stderr_tail.text()

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

        # hermes iso-home 临时目录清理已随隔离块删除（P0b 死代码清理，同上）。

        # ── 异常退出遗言补采（issue #317）──
        # exit!=0 且 stderr 为空 = 引擎"无遗言"死亡：补采 stdout 尾部进 errors；
        # returncode==-9（外部 SIGKILL，非本 executor 超时杀——那条路径已自带 done）
        # 结合系统日志启发式判定 OOM。
        _exit_code = proc.returncode
        _final_errors = list(error_events)
        if backend.name == "atomcode":
            if not _terminal_seen:
                _final_errors.append("AtomCode JSONL ended without a terminal event")
            if _final_errors and _exit_code == 0:
                _exit_code = 1
        # Issue #494：超时类死亡显式归因——returncode=124（timeout 命令）/ -15（SIGTERM）
        # 时 stderr 常为空，尾采拿不到原因，直接构造「执行超时」文案进 errors。
        from src.domains.runtime.agents.failure_attribution import process_exit_attribution
        _exit_attr = process_exit_attribution(_exit_code)
        if _exit_attr:
            _final_errors.append(_exit_attr)
        # issue #416（E7）：进程退出码非 0 且无文本 → errors 写明 `<引擎> exited rc=N`，
        # 不再让失败任务 errors 为空（旧形态：exit_code=-1、errors 空、stderr 截断）
        if _exit_code != 0 and not full_text.strip():
            _final_errors.append(f"{backend.name} exited rc={_exit_code}")
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

        if backend.name == "atomcode" and not _usage_seen and _usage_fallback:
            yield ("usage", _usage_fallback)
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
        backend.name != "atomcode"
        and session_id
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
        backend.name != "atomcode"
        and done.get("exit_code", 0) != 0
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
        if backend.name == "atomcode":
            # Preserve the session and fail explicitly; never silently start a
            # fresh conversation when the model returns an empty completion.
            done["exit_code"] = 1
            done.setdefault("errors", []).append("AtomCode completed without a text response; session preserved")
            yield ("done", done)
            return
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
                               agent_name: str = "", agent_label: str = "",
                               repo_key: str = "") -> AsyncGenerator:
    """async 桥接：在线程池中消费 stream_agent 生成器，逐事件桥接回 event loop。

    避免 agent 的同步生成器阻塞整个 event loop（否则 WS ping 超时掉线、其他消息/API 全卡住）。
    stream_agent 内部的"无输出 10 分钟优雅终止"保护在后台线程中照常生效。

    repo_key（R2，PR #1460 审查）：必须与 stream_agent 同口径透传。运行时四变量契约里
    GRAPE_ISSUE_ID / GRAPE_WORK_BRANCH 的补齐都依赖 repo_key；不透传则该解析整体跳过
    （VOD direct 路径会静默拿不到这两个变量）。
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
                repo_key=repo_key,
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
# _dispatch_execution — 统一执行分叉入口（P2-1 复活）
#
# cloud_worker 调 _dispatch_execution，传入自己创建的 sandbox + backend + prompt。
# 内部按 is_cloud_sandbox(sandbox) 判断：
#   - 云沙箱 → _run_cloud_sandbox（SDK 远程执行，yield 事件）
#   - bwrap/null → stream_agent（本地 Popen，yield 事件）
#
# stream_agent 的 is_cloud_sandbox 分支已删除（云沙箱不走 stream_agent），
# 所以 _dispatch_execution 当前只服务 cloud_worker。
# hermes_acp.py 仍保留自己的 is_cloud_sandbox 分支（独立 ACP 协议路径），暂不动。
# ============================================================================


def is_cloud_sandbox(sandbox) -> bool:
    """判断沙箱是否为云沙箱（CLOUD_SDK 模式）。

    统一判断入口，避免 stream_agent / hermes_acp 各写一份 hasattr 检查。
    """
    return getattr(sandbox, "execution_mode", None) == _ExecutionMode.CLOUD_SDK

from typing import cast as _cast, Iterator as _Iterator
from src.domains.runtime.agents.sandbox.types import (
    ExecutionMode as _ExecutionMode,
    CommandResult as _CommandResult,
    ExecHandle as _ExecHandleProtocol,
    UnsupportedCapability as _UnsupportedCapability,
)


class _PtyExecHandleAdapter:
    """适配 CubeSandbox PtyHandle 为 ExecHandle 协议。

    PtyHandle 迭代 yield bytes，没有 stream_stdout()/wait(timeout)。
    适配后让 _run_cloud_sandbox 后续代码统一处理。

    PTY 分支改用 wrapper 方案后，wrapper 输出 NDJSON 事件流。
    iter_events() 按行解析 NDJSON，yield 事件 dict。
    非 JSON 行（stty raw 前的 shell 提示符等噪声）丢弃 + debug 日志。
    """

    def __init__(self, pty_handle):
        self._pty = pty_handle
        self._collected = []  # 收集所有原始输出
        self._exit_code = None

    def stream_stdout(self):
        """迭代 PTY 输出，yield str（向后兼容，非 PTY-wrapper 路径用）。"""
        for chunk in self._pty:
            if isinstance(chunk, bytes):
                text = chunk.decode("utf-8", errors="replace")
            else:
                text = str(chunk)
            # PTY raw 模式可能有 \r\n，统一替换
            text = text.replace("\r\n", "\n").replace("\r", "\n")
            self._collected.append(text)
            yield text

    def iter_events(self):
        """迭代 PTY 输出，按行解析 NDJSON，yield 事件 dict。

        wrapper stdout 是 NDJSON（每行一个 JSON 事件）。
        stty raw -echo 之前的 shell 提示符等噪声行不是合法 JSON，丢弃。
        """
        import json as _json
        buf = ""
        for chunk in self._pty:
            if isinstance(chunk, bytes):
                text = chunk.decode("utf-8", errors="replace")
            else:
                text = str(chunk)
            # PTY raw 模式可能有 \r\n，统一替换
            text = text.replace("\r\n", "\n").replace("\r", "\n")
            self._collected.append(text)
            buf += text
            while "\n" in buf:
                line, buf = buf.split("\n", 1)
                line = line.strip()
                if not line:
                    continue
                try:
                    event = _json.loads(line)
                    yield event
                except _json.JSONDecodeError:
                    log.debug(f"non-json pty line: {line!r}")

    def wait(self, timeout=None):
        """阻塞直到 PTY 退出，返回类 CommandResult。

        SDK 的 PtyHandle.wait() 在 wrapper 退出码非 0 时会抛 CubeSandboxError。
        这里捕获异常，提取退出码，不抛——让上层根据 exit_code 判断成功/失败。
        """
        try:
            self._exit_code = self._pty.wait()
        except Exception as e:
            # SDK 在 wrapper exit_code != 0 时抛 "PTY exited with error: exit status N"
            # 提取退出码，不中断流程
            err_str = str(e)
            log.warning(f"PTY wait returned error: {err_str}")
            # 尝试从错误消息提取 exit code
            import re
            m = re.search(r'exit (?:status|code) (\d+)', err_str)
            self._exit_code = int(m.group(1)) if m else -1
        from src.domains.runtime.agents.sandbox.types import CommandResult
        return CommandResult(
            exit_code=self._exit_code,
            stdout="".join(self._collected),
            stderr="",
        )

    def kill(self):
        self._pty.kill()

    @property
    def pid(self):
        return self._pty.pid

    @property
    def exit_code(self):
        return self._exit_code


_ACP_CONTEXT_ENV_KEYS = (
    "GRAPE_SERVER_URL", "GRAPE_SESSION_TOKEN", "GRAPE_CLI_MODE",
    "REPO_FULL_NAME", "REPO_NAME", "ACCOUNT", "ISSUE_NUMBER",
    "PR_NUMBER", "DEVOPS_EVENT_TYPE", "AGENT_NAME", "GRAPE_SCENE_ID",
    "GIT_ASKPASS", "GRAPE_WORK_BRANCH",
    # Issue #3905：hermes ACP 子进程白名单此前独缺 GRAPE_ISSUE_ID——即使契约函数
    # 解析成功，backend=hermes 的 issue 任务在子进程里也读不到内部卡片 id
    # （pending-review / issues detail 依赖它）。与其余三变量同口径透传。
    "GRAPE_ISSUE_ID",
)

_ACP_PLAIN_ENV_KEYS = ("NO_COLOR", "TERM", "HERMES_YOLO_MODE", "PYTHONUNBUFFERED")


def _build_acp_envs(sandbox_envs: dict, proc_env: dict) -> dict:
    """hermes ACP 进程环境：沙箱基础 env + 上下文变量 + **任意平台** VCS 凭据。

    历史坑（2026-11-22）：这里曾把 VCS 凭据枚举成 BOT_GITCODE_TOKEN /
    BOT_GITHUB_TOKEN 两个键，后加的 gitee/gitlab 即便在 proc_env 里有值也会被裁掉，
    沙箱里 askpass 回声为空 → push 401。凭据键是平台知识，必须按谓词转发：
    任何 BOT_<PROVIDER>_TOKEN / USER_<PROVIDER>_TOKEN（含末级 BOT_TOKEN）都放行，
    新增平台不再改本文件。
    """
    from src.domains.runtime.vcs.clone_endpoint import is_vcs_token_env

    env = dict(sandbox_envs or {})
    for _k in _ACP_PLAIN_ENV_KEYS:
        if _k in proc_env:
            env[_k] = proc_env[_k]
    for _k in _ACP_CONTEXT_ENV_KEYS:
        if _k in proc_env:
            env[_k] = proc_env[_k]
    for _k, _v in (proc_env or {}).items():
        if is_vcs_token_env(_k):
            env[_k] = _v
    return env


def _run_cloud_sandbox(sandbox, backend, prompt, cwd, proc_env, session_id,
                       label, max_exec_seconds, use_session_id):
    """云沙箱执行路径——通过 SDK run_command 在远端沙箱内执行 agent。

    替代 bwrap 的 Popen 路径：
    - 构建 agent 启动命令
    - 调 sandbox.run_command() → ExecHandle
    - 后台线程定期 refresh 防超时
    - 流式读 stdout → yield 事件
    - wait() 拿 exit_code → yield done
    """
    # BUG-8 修复：云沙箱路径命令预检 + 提示注入检测（与 _run_acp_session 入口一致）
    try:
        from src.domains.runtime.agents.policy import policy_manager
        from src.domains.runtime.agents.policy.errors import CommandBlocked
        from src.domains.runtime.agents.policy.types import Decision, InputDecision
        _acp_cmd = [getattr(backend, "binary", "agent"), "acp"]
        _guard_result = policy_manager.check_command(_acp_cmd, task_id=session_id or "")
        if _guard_result.decision is Decision.DENY:
            raise CommandBlocked(f"dangerous command: {_guard_result.reason}")
        _input_result = policy_manager.check_input(prompt, task_id=session_id or "")
        if _input_result.decision is InputDecision.DENY:
            raise CommandBlocked(f"prompt injection detected: {_input_result.detail}")
    except CommandBlocked as _e:
        log.error(f"{label}cloud sandbox blocked: {_e}")
        yield ("done", {"exit_code": -1, "text": "", "oc_session_id": session_id,
                        "errors": [str(_e)], "stderr": ""})
        return
    except ImportError:
        pass  # policy 模块不可用时跳过（Null 模式）

    import time as _time
    import threading as _threading
    from src.domains.runtime.agents.backend_protocol import ProtocolAware, CloudBackendProvider, PTYAware, SessionStaleDetector

    # 后台 refresh 线程——防止 agent 长时间执行时沙箱超时（3600s）
    _refresh_stop = _threading.Event()

    def _refresh_loop():
        while not _refresh_stop.wait(180):  # 每 3 分钟 refresh 一次
            try:
                sandbox.refresh(duration=300)
                log.debug(f"{label}cloud sandbox refreshed")
            except Exception as e:
                log.warning(f"{label}cloud sandbox refresh failed: {e}")

    _refresh_thread = _threading.Thread(target=_refresh_loop, daemon=True)
    _refresh_thread.start()

    # 从 proc_env 补充仓库参数（manager.prepare 没传这些）
    # 平台必须来自带前缀的 REPO_KEY：REPO_FULL_NAME 是剥前缀裸名（runbook 用），
    # 单独用它会让 parse_repo_key 默认 provider=gitcode（gitee 仓被 clone 到 gitcode.com，
    # 令牌键也回落 BOT_GITCODE_TOKEN）。与 sandbox_context 同一判定顺序。
    repo_full_name = proc_env.get("REPO_KEY") or proc_env.get("REPO_FULL_NAME", "")
    # bot token 键按平台派生（过去写死 BOT_GITCODE_TOKEN：gitee 仓在沙箱里恒空 → 不 clone）
    try:
        from src.domains.runtime.vcs.clone_endpoint import bot_token_from_env as _bot_from_env

        bot_token = _bot_from_env(proc_env, repo_full_name)
    except Exception as _bt_err:
        log.warning(f"{label}bot token env derive failed: {_bt_err}")
        from src.domains.runtime.vcs.clone_endpoint import bot_token_from_env as _fallback_bot
        bot_token = _fallback_bot(proc_env, repo_full_name)
    work_branch = proc_env.get("GRAPE_WORK_BRANCH", "")

    # 通过通用接口配置仓库（不直接设私有属性，不 isinstance 分叉）
    if repo_full_name and bot_token:
        # clone 三要素（地址/鉴权形态/用户名）单点派生：这里过去写死
        # https://gitcode.com/{repo}.git，gitee/github/gitlab 仓会被当成 gitcode
        # 仓去 clone，失败与"仓库不存在"不可区分。
        try:
            from src.domains.runtime.vcs.clone_endpoint import clone_params

            _cp = clone_params(repo_full_name)
            repo_url = _cp["clone_url"]
            _clone_username = _cp["username"]
            # basic_password 平台（gitee）：用户名必须是令牌所属账号 login。
            # 有令牌就现场解析（带本地缓存），解析失败回落卡值——别再依赖人手填准。
            try:
                from src.domains.runtime.vcs.clone_identity import username_for_token as _user_for_token

                _clone_username, _cu_src = _user_for_token(_cp["provider"], bot_token, repo_full_name)
                log.info(f"{label}clone username source={_cu_src}")
            except Exception as _cu_err:  # noqa: BLE001
                log.warning(f"{label}clone username dynamic failed: {_cu_err}")
        except Exception as e:
            log.warning(f"{label}clone endpoint resolve failed for {repo_full_name}: {e}")
            repo_url, _clone_username = "", ""
        try:
            sandbox.configure_repo(
                repo_url=repo_url,
                bot_token=bot_token,
                work_branch=work_branch,
                username=_clone_username,
            )
        except Exception as e:
            log.warning(f"{label}cloud sandbox repo clone failed: {e}")

    # skill 下载已由 DefaultMaterializer → SkillMaterializer 在 cloud_worker 外层完成
    # {{SKILL_DIR}} 替换也已在 cloud_worker 外层完成

    # 修正 cwd 为沙箱内路径
    if sandbox.repo_dir:
        cwd = sandbox.repo_dir
    else:
        # git clone 失败 → repo_dir 为空 → cwd 是 task 下发的本机路径（不存在于沙箱）
        # fallback 到 /tmp 防止 PTY create 报 invalid_argument: cwd does not exist
        log.warning(f"{label}sandbox.repo_dir empty (git clone failed?), cwd fallback to /tmp")
        cwd = "/tmp"
    # {{SKILL_DIR}} 替换已由 SkillMaterializer 在 cloud_worker 外层完成

    # 根因修复：cwd 不存在时 EnvD 报 fork/exec /bin/bash: no such file or directory（误导性错误）
    # 确保 cwd 目录存在，不存在则 fallback 到 /tmp
    import shlex as _shlex
    if cwd:
        try:
            _cwd_check = sandbox.run_command(f"test -d {_shlex.quote(cwd)} && echo exists", timeout=5, background=False)
            if hasattr(_cwd_check, 'stdout') and 'exists' not in (_cwd_check.stdout or ''):
                log.warning(f"{label}cwd {cwd} not found, falling back to /tmp")
                cwd = "/tmp"
        except Exception:
            cwd = "/tmp"

    # 构建 agent 命令——职责下沉到 backend（build_remote_invocation）
    # opencode/codearts: 命令行传 prompt，无 stdin
    # hermes ACP: 命令行只传 hermes acp，stdin 传 JSON-RPC
    import shlex as _shlex
    # backend 替换——isinstance 检查能力，不按名字判断
    # HermesRuntime.cloud_backend() 返回 HermesACPBackend()（无状态，每次 new）
    # 其他引擎默认返回 self（AgentRuntime 基类）
    if isinstance(backend, CloudBackendProvider):
        backend = backend.cloud_backend()

    # ── 协议分叉——按 protocol 属性，不按引擎名 ──
    # ACP（hermes）：DevBox → DevBoxTransport，CubeSandbox → CubeSandboxTransport
    # NDJSON（opencode/codearts/codex）：走 PTY wrapper 或 run_command 路径
    if isinstance(backend, ProtocolAware) and backend.protocol == "acp":
        from src.domains.runtime.agents.hermes_acp import _run_acp_session
        from src.domains.runtime.agents.hermes_acp import _detect_structured_output, _robust_join_json, _detect_findings_block, _robust_join_findings
        # {{SKILL_DIR}} 替换已由 SkillMaterializer 在 cloud_worker 外层完成
        # hermes_bin 查找——多级回退，不靠字符串切割猜路径
        _sandbox_envs = getattr(sandbox, '_init_envs', None) or {}
        _venv_py = _sandbox_envs.get("_HERMES_VENV_PYTHON", "")
        # [沙箱迁移] _HERMES_BIN 是安装时算好的绝对路径（readlink -f which hermes）——
        # PTY 启动脚本非 login shell 不读 ~/.bashrc，裸命令名会 not found
        _hermes_bin = _sandbox_envs.get("_HERMES_BIN", "") or _find_hermes_bin(_venv_py, sandbox_mode=True)
        log.info(f"{label}hermes bin: venv_py={_venv_py!r} hermes_bin={_hermes_bin!r} _init_envs_keys={list(_sandbox_envs.keys())}")

        # 脱敏——和 PTY/run_command 路径一致
        from src.domains.runtime.vcs.clone_endpoint import vcs_token_values as _vcs_tokens
        _tokens = _vcs_tokens(proc_env)
        _redact_engine = None
        if _tokens:
            from src.domains.runtime.agents.sandbox.redact import RedactEngine
            _redact_engine = RedactEngine(secret_values=_tokens)
        def _redact_text(text):
            if not text or not _redact_engine:
                return text
            return _redact_engine.redact(str(text)).text

        # 构建 hermes acp 进程环境变量：沙箱基础 env + 上下文变量 + 任意平台 VCS 凭据
        _acp_envs = _build_acp_envs(_sandbox_envs, proc_env)

        # [USP] Transport 分派收编——adapter.build_transport 注册表分派，
        # supports_pty() if-else 分叉消失（平台自决 PTY vs 文件轮询）
        from src.domains.runtime.agents.sandbox.backends import get_adapter as _get_adapter
        _adapter = _get_adapter(getattr(sandbox, "platform", ""))
        if _adapter is None:
            raise RuntimeError(f"no platform adapter registered for {sandbox.platform!r}")
        _transport = _adapter.build_transport(sandbox, hermes_bin=_hermes_bin, envs=_acp_envs)
        log.info(f"{label}using {type(_transport).__name__} (via adapter.build_transport)")

        _transport.start()
        try:
            # P1-5 修复：DevBox 路径也传 on_message 回调，支持 chat 检测
            # 和 bwrap 路径（hermes_acp.py:746-756）逻辑一致
            _is_chat_task = proc_env.get("DEVOPS_EVENT_TYPE", "") == "chat_message"
            _chat_first_text_time = 0.0
            _chat_last_text_time = 0.0
            _CHAT_STABLE_SECONDS = 10.0
            _CHAT_IDLE_SECONDS = 3.0
            import threading as _chat_threading
            _chat_prompt_done = _chat_threading.Event()
            def _on_message(msg_type, content, prompt_done_event):
                nonlocal _chat_first_text_time, _chat_last_text_time
                if msg_type == "text" and _is_chat_task and not prompt_done_event.is_set():
                    if _chat_first_text_time == 0:
                        _chat_first_text_time = time.time()
                    _chat_last_text_time = time.time()
                    _stable = (time.time() - _chat_first_text_time) >= _CHAT_STABLE_SECONDS
                    _idle = (time.time() - _chat_last_text_time) >= _CHAT_IDLE_SECONDS
                    if _stable and _idle:
                        log.info(f"{label}hermes ACP chat task text stable for {_CHAT_IDLE_SECONDS}s, assuming done")
                        prompt_done_event.set()
            # session_mode：DevBox 路径暂不 resume（沙箱内 state.db 路径不确定）
            # [看门狗 2026-11-12] ACP 会话 tool 静默超时接通任务级 env——
            # agent.py #378 的 HERMES_SILENT_TIMEOUT 注入(coding/qa=1200)此前在这条
            # 调用路径断链(未传参, 恒为默认 300s). 仅放宽 tool_idle(判死);
            # text_idle(判完成/response lost)保持 300 不动, 不影响 chat 完成检测.
            from src.domains.runtime.agents.hermes_acp import _silent_timeout_for as _acp_silent_for
            _acp_tool_silent = _acp_silent_for(proc_env)
            for evt_type, evt_data in _run_acp_session(
                transport=_transport,
                prompt=prompt,
                cwd=cwd,
                session_id=use_session_id or session_id,
                label=label,
                max_exec_seconds=max_exec_seconds,
                session_mode="new",
                tool_idle_timeout=_acp_tool_silent,
                on_message=_on_message,
            ):
                if evt_type == "done":
                    # 文本后处理（JSON 鲁棒拼接）——和 bwrap 路径一致
                    full_text = evt_data.get("text", "")
                    if _detect_structured_output(full_text):
                        _fixed = _robust_join_json(full_text.split("\n"))
                        if _fixed and _detect_structured_output(_fixed):
                            full_text = _fixed
                    elif _detect_findings_block(full_text):
                        _fixed = _robust_join_findings(full_text.split("\n"))
                        if _fixed and _detect_findings_block(_fixed):
                            full_text = _fixed
                    evt_data["text"] = _redact_text(full_text)
                    # 补充 sandbox_id + backend（协议层不包含）
                    evt_data["sandbox_id"] = getattr(sandbox, "_sandbox_id", "")
                    evt_data["backend"] = "hermes"
                    if _redact_engine:
                        _redact_engine.clear()
                elif isinstance(evt_data, str):
                    evt_data = _redact_text(evt_data)
                yield (evt_type, evt_data)
        finally:
            _refresh_stop.set()  # BUG-4 修复：确保异常时也停止 refresh 线程
        return  # ACP 路径已完成，不走后续 CLI fallback

    if hasattr(backend, 'build_remote_invocation'):
        cmd_list, stdin_payload = backend.build_remote_invocation(prompt, session_id)
    else:
        cmd_list = backend.build_args(prompt, session_id)
        stdin_payload = None
        # [E-02 exit=127 修复 2026-11-13] build_args 契约=「不含 binary 的参数列表」
        # （本地链 executor:1116 是 _cmd = [find_cli(backend.binary)] + args——binary
        # 由调用方前置）。云沙箱链此前直接 join build_args → opencode 的首参
        # "run" 被当命令头 → /bin/bash: run: command not found exit=127（E3/E-02
        # 实证 t_4b1116ff9922 / t_e4b767a016aa）。hermes 不受影响（走
        # build_remote_invocation 返回完整 [binary, "acp"]）。
        if isinstance(cmd_list, list) and cmd_list and str(cmd_list[0]) not in (
                _binary := getattr(backend, 'binary', ''), getattr(backend, 'name', '')) \
                and _binary:
            cmd_list = [_binary] + cmd_list
        # [E-02 prompt 丢失修复 2026-11-13] prompt_via_stdin 引擎（opencode 等）
        # 的 prompt 不在 argv——本地链由 Popen stdin 喂；云沙箱链此前 stdin_payload
        # 恒 None → 沙箱跑 `opencode run` 无输入 → exit=1 "You must provide a
        # message or a command"（实证 t_8fad23936b99）。build_remote_invocation
        # 路径（hermes ACP）不受影响——其 stdin_payload 已含协议初始化帧。
        if stdin_payload is None and getattr(backend, 'prompt_via_stdin', False) and prompt:
            stdin_payload = prompt

    if isinstance(cmd_list, list):
        cmd_str = " ".join(_shlex.quote(str(arg)) for arg in cmd_list)
    else:
        cmd_str = str(cmd_list)

    # PATH 确保 /tmp/pylibs/bin 在路径中（grape wrapper 装在这里）
    # hermes 的 PATH 由 _post_install 写入 ~/.bashrc，login shell 自动获取
    cmd_str = f"export PATH=$PATH:/tmp/pylibs/bin && {cmd_str}"

    log.info(f"{label}cloud sandbox run_command: {cmd_str[:200]}")

    # prompt 已通过 build_remote_invocation 处理（命令行或 stdin）

    # 执行——C-2 修复：过滤 _LLM_ 前缀的 key，不注入沙箱环境变量（防止 API Key 泄漏）
    _safe_env = {k: v for k, v in proc_env.items() if not k.startswith("_LLM_")}
    # git credential helper 从 GIT_BOT_TOKEN env 读 token（不落盘 ~/.git-credentials）
    if bot_token:
        _safe_env["GIT_BOT_TOKEN"] = bot_token
    # 合并沙箱 bind_credential 绑定的环境变量（LLM API key 等）
    # _apply_engine_config 里 bind_credential 存到 _init_envs/_credentials，
    # 这些需要注入到执行环境里，否则引擎配置里的 ${ENV_VAR} 引用无法解析
    _sandbox_creds = getattr(sandbox, '_credentials', None) or {}
    _sandbox_envs = getattr(sandbox, '_init_envs', None) or {}
    _safe_env.update(_sandbox_creds)
    _safe_env.update(_sandbox_envs)

    # PTY 分派——isinstance 检查能力，零引擎名 if-else
    # PTYAware 引擎声明 wrapper 文件名 + 公共模块依赖 + stdin_payload 需求 + env 注入
    _use_pty = (
        sandbox.supports_pty()
        and isinstance(backend, PTYAware)
        and isinstance(cmd_list, list) and len(cmd_list) > 0
    )
    if _use_pty:
        _wrapper_name = backend.pty_wrapper_name()
        if _wrapper_name is None:
            _use_pty = False
        elif backend.pty_needs_stdin_payload():
            # ACP 协议需要 stdin_payload + cmd == [binary, "acp"]
            _use_pty = bool(stdin_payload) and cmd_list == [backend.binary, "acp"]

    if _use_pty:
        log.info(f"{label}using PTY with {_wrapper_name} (CubeSandbox)")
        try:
            # 1. 注入 wrapper 脚本到沙箱
            from pathlib import Path as _Path
            _wrapper_path = _Path(__file__).parent / "sandbox" / "wrappers" / _wrapper_name
            try:
                _wrapper_code = _wrapper_path.read_text()
            except Exception as _e:
                log.error(f"{label}failed to read {_wrapper_name}: {_e}")
                raise RuntimeError(f"{_wrapper_name} not found: {_e}")
            sandbox.write_file(f"/tmp/{_wrapper_name}", _wrapper_code)

            # NDJSON 类 wrapper 需要公共模块
            if backend.pty_use_ndjson_common():
                _common_path = _Path(__file__).parent / "sandbox" / "wrappers" / "_ndjson_common.py"
                try:
                    _common_code = _common_path.read_text()
                    sandbox.write_file("/tmp/_ndjson_common.py", _common_code)
                except Exception as _e:
                    log.error(f"{label}failed to read _ndjson_common.py: {_e}")
                    raise RuntimeError(f"_ndjson_common.py not found: {_e}")

            # 2. 传 prompt 和 cwd 给 wrapper（通过环境变量，避免 argv 长度限制 + shell 转义）
            _safe_env["GRAPE_TASK_PROMPT"] = prompt
            _safe_env["GRAPE_TASK_CWD"] = cwd or "/home/user/repo"
            _safe_env["GRAPE_TASK_TIMEOUT"] = str(max_exec_seconds)
            backend.pty_prepare_env(_safe_env, session_id)

            # 3. PTY 创建 → stty raw -echo → 启动 wrapper; exit
            #    ; exit 确保 wrapper 结束后 shell 退出 → PTY 关闭
            #    timeout=None：PTY 会话永不超时，完全依赖 wrapper 自己管命令超时
            # [USP] PTY 收编：SDK 私有对象穿透 → ExecSession 契约
            # （PTYSize/Sdk handle 封装进 backends/cubesandbox.pty_create）
            _pty_handle = sandbox.pty_create(
                rows=50, cols=200,
                cwd=cwd or None,
                envs=_safe_env,
                timeout=None,  # 会话永不超时，wrapper 内部管命令超时
            )
            # raw 模式——关回显 + 关 ONLCR
            _pty_handle.send_stdin("stty raw -echo\n")
            time.sleep(0.3)  # 等 stty 生效
            # 启动 wrapper（; exit 确保 shell 退出 → PTY 关闭）
            _pty_handle.send_stdin(f"python3 /tmp/{_wrapper_name}; exit\n")
            # 创建适配器——iter_events() 解析 NDJSON 事件流
            handle = _PtyExecHandleAdapter(_pty_handle)
        except Exception as e:
            log.error(f"{label}PTY wrapper setup failed: {e}")
            # BUG-B 修复：不调 sandbox.__exit__——让 cloud_worker finally 统一处理 kill + exit
            yield ("done", {
                "exit_code": -1, "text": "", "oc_session_id": session_id,
                "errors": [f"PTY wrapper setup failed: {e}"], "stderr": "",
                "sandbox_id": getattr(sandbox, "_sandbox_id", ""),
            })
            return
    else:
        try:
            handle = sandbox.run_command(
                cmd_str,
                envs=_safe_env,
                cwd=cwd,
                stdin=True,
                background=True,
                timeout=max_exec_seconds,
            )
            # 非 PTY 路径：如果有 stdin_payload 且 handle 支持 send_stdin
            if stdin_payload and hasattr(handle, 'send_stdin'):
                handle.send_stdin(stdin_payload)
                if hasattr(handle, 'close_stdin'):
                    try:
                        handle.close_stdin()
                    except Exception:
                        pass  # DevBox CloseStdin 可能返回 500（file already closed），不影响功能
        except Exception as e:
            log.error(f"{label}cloud sandbox run_command failed: {e}")
            # BUG-B 修复：不调 sandbox.__exit__——让 cloud_worker finally 统一处理 kill + exit
            yield ("done", {
                "exit_code": -1, "text": "", "oc_session_id": session_id,
                "errors": [f"cloud sandbox execution failed: {e}"], "stderr": "",
                "sandbox_id": getattr(sandbox, "_sandbox_id", ""),
            })
            return

    # 脱敏——和 bwrap 路径一致（RedactEngine）
    from src.domains.runtime.vcs.clone_endpoint import vcs_token_values as _vcs_tokens2
    _tokens = _vcs_tokens2(proc_env)
    _redact_engine = None
    if _tokens:
        from src.domains.runtime.agents.sandbox.redact import RedactEngine
        _redact_engine = RedactEngine(secret_values=_tokens)

    def _redact_text(text):
        if not text or not _redact_engine:
            return text
        return _redact_engine.redact(str(text)).text

    # 流式读 stdout（脱敏后 yield）+ BUG-4 修复：静默超时 300s
    # 用独立线程读 stream_stdout/iter_events，主线程轮询检查静默超时
    # 解决 agent 完全 hang 时 for 循环不 yield 导致超时检查不触发的问题
    import queue as _queue
    import threading as _threading

    collected_text = []
    _last_output = time.time()
    _SILENT_TIMEOUT = 300  # 5 分钟无输出 → kill
    _stream_q = _queue.Queue()
    _stream_done = _threading.Event()

    # PTY wrapper 路径用 iter_events（NDJSON 事件），非 PTY 路径用 stream_stdout（裸文本）
    _use_iter_events = hasattr(handle, 'iter_events')

    def _stream_worker():
        """后台线程：读输出，放入队列。PTY 路径放事件 dict，非 PTY 放文本 str。"""
        try:
            if _use_iter_events:
                for event in handle.iter_events():
                    _stream_q.put(event)
            else:
                for chunk in handle.stream_stdout():
                    _stream_q.put(chunk)
        except Exception as e:
            _stream_q.put(e)
        finally:
            _stream_done.set()

    _stream_t = _threading.Thread(target=_stream_worker, daemon=True)
    _stream_t.start()

    # PTY wrapper 路径：done 事件从 NDJSON 流中提取（不需要 wait()）
    _pty_done_extracted = False

    try:
        while not _stream_done.is_set():
            try:
                item = _stream_q.get(timeout=5)  # 5s 轮询间隔
            except _queue.Empty:
                # 没有输出——检查静默超时
                if time.time() - _last_output > _SILENT_TIMEOUT:
                    log.warning(f"{label}cloud sandbox silent timeout {_SILENT_TIMEOUT}s, killing")
                    try:
                        handle.kill()  # 先 kill 远端进程
                    except Exception:
                        pass
                    # BUG-B 修复：不调 sandbox.__exit__——让 cloud_worker finally 统一处理 kill + exit
                    yield ("done", {
                        "exit_code": -1, "text": "".join(collected_text),
                        "oc_session_id": session_id,
                        "errors": [f"silent timeout after {_SILENT_TIMEOUT}s"],
                        "stderr": "",
                        "sandbox_id": getattr(sandbox, "_sandbox_id", ""),
                    })
                    return
                continue

            if isinstance(item, Exception):
                log.warning(f"{label}cloud sandbox stream error: {item}")
                break

            # PTY wrapper 路径：item 是 NDJSON 事件 dict
            if _use_iter_events and isinstance(item, dict):
                evt_type = item.get("type", "")
                if not evt_type:
                    continue
                _last_output = time.time()

                if evt_type == "text":
                    content = item.get("content", "")
                    redacted = _redact_text(content)
                    collected_text.append(redacted)
                    yield ("text", redacted)
                elif evt_type == "reasoning":
                    yield ("reasoning", _redact_text(item.get("content", "")))
                elif evt_type == "tool_use":
                    yield ("tool_use", {"tool": item.get("tool", ""), "input": item.get("input", {})})
                elif evt_type == "tool_finish":
                    yield ("tool_finish", _redact_text(item.get("content", "")))
                elif evt_type == "tool_result":
                    yield ("tool_result", _redact_text(item.get("content", "")))
                elif evt_type == "error":
                    # 引擎报错——yield 给上层，不丢弃
                    _err_msg = _redact_text(item.get("content", ""))
                    log.warning(f"{label}engine error event: {_err_msg[:200]}")
                    yield ("text", f"[engine error] {_err_msg}")
                elif evt_type == "step_start":
                    yield ("step_start", item.get("sessionID", ""))
                elif evt_type == "step_finish":
                    yield ("step_finish", item.get("tokens", 0))
                elif evt_type == "done":
                    # wrapper 输出的最终结果——提取 exit_code/session_id
                    _pty_done_extracted = True
                    _done_exit = item.get("exit_code", -1)
                    _done_stop = item.get("stop_reason", "")
                    _done_sid = item.get("session_id", "")
                    _done_errors = item.get("errors", [])
                    # 如果有 collected_text，用 collected_text 作为 final_text
                    final_text = _redact_text("".join(collected_text))
                    _refresh_stop.set()
                    if _redact_engine:
                        _redact_engine.clear()
                    _sandbox_id = getattr(sandbox, "_sandbox_id", "")
                    log.info(f"{label}cloud sandbox done (wrapper): exit={_done_exit}, text_len={len(final_text)}")
                    yield ("done", {
                        "exit_code": _done_exit,
                        "text": final_text,
                        "oc_session_id": _done_sid or session_id,
                        "errors": _done_errors,
                        "stderr": "",
                        "sandbox_id": _sandbox_id,
                    })
                    return
                # 未知事件类型——记日志丢弃
                else:
                    # P2-15 修复：未知事件类型提升到 warning，便于发现协议变更
                    log.warning(f"{label}unknown ndjson event type: {evt_type}")
            else:
                # 非 PTY 路径：item 是裸文本 str
                if item:
                    _last_output = time.time()
                    redacted = _redact_text(item)
                    collected_text.append(redacted)
                    yield ("text", redacted)

        # 线程结束——消费剩余队列
        while not _stream_q.empty():
            item = _stream_q.get_nowait()
            if isinstance(item, Exception):
                break
            if _use_iter_events and isinstance(item, dict):
                evt_type = item.get("type", "")
                if evt_type == "text":
                    redacted = _redact_text(item.get("content", ""))
                    collected_text.append(redacted)
                    yield ("text", redacted)
                elif evt_type == "done":
                    _pty_done_extracted = True
            elif item:
                redacted = _redact_text(item)
                collected_text.append(redacted)
                yield ("text", redacted)
    except Exception as e:
        log.warning(f"{label}cloud sandbox stream outer error: {e}")

    # PTY wrapper 路径：done 事件已从 NDJSON 流提取，不需要 wait()
    if _pty_done_extracted:
        _refresh_stop.set()
        if _redact_engine:
            _redact_engine.clear()
        return

    # 非 PTY 路径：等待完成（超时 kill 远端进程）
    try:
        result = handle.wait(timeout=max(max_exec_seconds - 60, 60))
        exit_code = result.exit_code
        final_text = _redact_text(result.stdout or "".join(collected_text))
        stderr = _redact_text(result.stderr or "")
    except Exception as e:
        log.error(f"{label}cloud sandbox wait failed: {e}, killing remote process")
        try:
            handle.kill()  # 超时杀远端进程
        except Exception:
            pass
        exit_code = -1
        final_text = _redact_text("".join(collected_text))
        stderr = str(e)

    # 停止 refresh 线程
    _refresh_stop.set()

    # 撤销 proxy token——cloud_worker finally 已统一处理，此处不重复撤销

    # 清理脱敏引擎
    if _redact_engine:
        _redact_engine.clear()

    # 每环节新建——sandbox_id 放入 done event，kill 由 cloud_worker finally 统一处理
    _sandbox_id = getattr(sandbox, '_sandbox_id', '')

    log.info(f"{label}cloud sandbox done: exit={exit_code}, text_len={len(final_text)}")
    if exit_code != 0:
        log.warning(f"{label}cloud sandbox exit={exit_code} text={final_text[:300]!r} stderr={stderr[:300]!r}")
    yield ("done", {
        "exit_code": exit_code,
        "text": final_text,
        "oc_session_id": session_id,
        "errors": [] if exit_code == 0 else [stderr[:500]],
        "stderr": stderr,
        "sandbox_id": _sandbox_id,
    })

def dispatch_execution(sandbox, backend, prompt: str, cwd: str,
                        proc_env: dict, session_id: str = "",
                        label: str = "", max_exec_seconds: int = 600,
                        use_session_id: str = "",
                        ) -> Generator:
    """云沙箱执行分叉入口。sandbox 必须已 __enter__。

    只服务 cloud_worker 和 hermes_acp。bwrap 路径不走这里
    （stream_agent 自己 Popen，已删 cloud 分支）。
    非 cloud sandbox 传入时 raise（fail-fast）。

    Args:
        sandbox: 已 __enter__ 的 CloudSandbox 实例
        backend: AgentBackend 实例（hermes/opencode/codearts）
        prompt: agent prompt
        cwd: 沙箱内工作目录
        proc_env: 环境变量（含 _LLM_* / BOT_GITCODE_TOKEN 等）
        session_id: agent 会话 ID
        label: 日志前缀
        max_exec_seconds: 最大执行时间
        use_session_id: 覆盖 session_id

    Yields:
        tuple[str, dict]: ("text", chunk) / ("done", result_dict) / ("error", msg)
    """
    if not is_cloud_sandbox(sandbox):
        raise RuntimeError(f"dispatch_execution only for cloud sandbox, got {type(sandbox).__name__}")
    proc_env["GRAPE_SANDBOX_MODE"] = "cloud_sdk"
    log.info(f"{label}{backend.name} sandboxed via cloud SDK (mode=cloud_sdk)")
    yield from _run_cloud_sandbox(
        sandbox, backend, prompt, cwd, proc_env, session_id,
        label, max_exec_seconds, use_session_id or session_id,
    )


# deprecated alias——减少一次性破坏，新代码用 dispatch_execution
_dispatch_execution = dispatch_execution


async def dispatch_execution_async(sandbox, backend, prompt, cwd, proc_env,
                                   session_id="", label="", max_exec_seconds=600,
                                   use_session_id=""):
    """异步桥接：在线程池中消费 dispatch_execution 同步生成器，逐事件 yield。

    和 agent.py 的 _stream_runtime_async 同理——同步生成器的 readline() 阻塞
    移到后台线程，事件循环持续调度 claim/heartbeat。

    Args:
        sandbox: 已 __enter__ 的 CloudSandbox 实例
        backend: AgentBackend 实例
        prompt: agent prompt
        cwd: 沙箱内工作目录
        proc_env: 环境变量
        session_id: agent 会话 ID
        label: 日志前缀
        max_exec_seconds: 最大执行时间

    Yields:
        tuple[str, Any]: 事件类型 + 数据
    """
    import queue as _queue
    import threading as _threading
    import asyncio
    q: _queue.Queue = _queue.Queue(maxsize=256)
    sentinel = object()

    def _produce():
        try:
            for evt in dispatch_execution(
                sandbox, backend, prompt, cwd, proc_env,
                session_id=session_id, label=label,
                max_exec_seconds=max_exec_seconds,
                use_session_id=use_session_id,
            ):
                q.put(evt, timeout=30)  # 阻塞 put，30s 超时（不 busy-wait）
        except Exception as e:
            log.warning(f"dispatch_execution producer error: {e}")
            q.put(("error", str(e)))
        finally:
            q.put(sentinel)

    _t = _threading.Thread(target=_produce, daemon=True)
    _t.start()
    while True:
        try:
            evt = q.get_nowait()
        except _queue.Empty:
            await asyncio.sleep(0.05)  # 50ms 轮询
            continue
        if evt is sentinel:
            break
        yield evt
