"""RuntimeAgent 边端入口（python -m runtime.agent，仅外连）。

见 agent-runtime-design.md §7.4 / tech-impl.md §1.4 / skill-delivery-remote.md §3.5。
流程：discover_runtimes → register → recover_orphans → 循环 claim（带 jitter）→ 执行 → 回报。
"""
import sys

# 版本守卫：代码使用 Python 3.10+ 联合类型语法（X | None），<3.10 会在 import 期崩溃。
# 放最顶部（任何 import 之前），给清晰报错而非 traceback。
if sys.version_info < (3, 10):
    print(f"RuntimeAgent requires Python >= 3.10 (got {sys.version.split()[0]}). "
          f"请用 python3.12 重建 venv: python3.12 -m venv .venv && .venv/bin/pip install -r requirements-worker.txt",
          file=sys.stderr)
    sys.exit(1)

import asyncio, json, os, random

from src.config import log


def _env_float(name, default):
    try:
        return float(os.environ.get(name, default))
    except Exception:
        return float(default)


def _env_int(name, default):
    try:
        return int(os.environ.get(name, default))
    except Exception:
        return int(default)


def _discover_local_runtimes() -> list[dict]:
    """本机 CLI 列表（复用 discover_runtimes，补齐 version/healthy）。"""
    from src.runtime.registry import discover_runtimes
    out = []
    for r in discover_runtimes():
        version = ""
        healthy = False
        cli = r.get("cli_path") or ""
        if cli:
            try:
                import subprocess
                res = subprocess.run([cli, "--version"], capture_output=True, text=True, timeout=5)
                healthy = res.returncode == 0
                if healthy:
                    version = (res.stdout or "").strip().split("\n")[0][:60]
            except Exception:
                pass
        out.append({"name": r["name"], "version": version, "healthy": healthy, "cli_path": cli, "installed": r.get("installed", False)})
    return out


def _remap_cwd(path: str) -> str:
    """将服务端下发的 cwd 路径映射到本地用户 home 目录。

    服务端以 /home/grape 为基准生成 workspaces 路径，但各接入机器的用户不同
    （lvjuntao、zhangsan 等）。通过 RUNTIME_CWD_PREFIX_MAP 环境变量配置映射：
      RUNTIME_CWD_PREFIX_MAP=/home/grape=/home/lvjuntao
    支持多组映射，用逗号分隔：
      RUNTIME_CWD_PREFIX_MAP=/home/grape=/home/lvjuntao,/opt/data=/data
    未配置时默认将 /home/grape 映射到当前用户的 home 目录。
    """
    if not path:
        return path
    import os as _os
    mapping = _os.environ.get("RUNTIME_CWD_PREFIX_MAP", "")
    if not mapping:
        # 默认映射：/home/grape → 当前用户 home
        default_src = "/home/grape"
        local_home = _os.path.expanduser("~")
        if path.startswith(default_src) and local_home != default_src:
            return local_home + path[len(default_src):]
        return path
    for pair in mapping.split(","):
        pair = pair.strip()
        if "=" not in pair:
            continue
        src, dst = pair.split("=", 1)
        src, dst = src.strip(), dst.strip()
        if src and path.startswith(src):
            return dst + path[len(src):]
    return path


def _kill_stale_opencode(task: dict):
    """超时后杀掉对应 opencode 子进程。按 cwd 匹配，避免误杀其他任务。"""
    import subprocess, signal
    cwd = _remap_cwd(task.get("cwd") or os.getcwd())
    try:
        out = subprocess.run(
            ["pgrep", "-u", os.environ.get("USER", os.getlogin() if hasattr(os, "getlogin") else ""), "-f", "opencode run"],
            capture_output=True, text=True, timeout=5
        )
        for pid_str in out.stdout.strip().split():
            pid = int(pid_str)
            try:
                proc_cwd = os.readlink(f"/proc/{pid}/cwd")
                if proc_cwd.startswith(cwd):
                    pgid = os.getpgid(pid)
                    os.killpg(pgid, signal.SIGTERM)
                    log.info(f"killed opencode pid={pid} pgid={pgid} cwd={proc_cwd}")
            except Exception as e:
                log.debug(f"skip pid={pid}: {e}")
    except Exception as e:
        log.warning(f"_kill_stale_opencode failed: {e}")


async def _execute_task(client, task, engine_name: str = ""):
    """执行单个任务：落地技能包 → 占位符替换 → stream_runtime → 回报。

    engine_name 为该任务所属引擎实例名（如 opencode-worker-123249），用于日志区分。
    """
    import os as _os
    tid = task.get("id", "")
    cwd = _remap_cwd(task.get("cwd") or _os.getcwd())
    _engine_tag = f"|engine:{engine_name}" if engine_name else ""
    try:
        _os.makedirs(cwd, exist_ok=True)
    except Exception:
        pass
    env = {}
    try:
        env = json.loads(task.get("env_extra") or "{}")
    except Exception:
        pass
    skill_dir = _os.path.join(cwd, ".skills")
    prompt = (task.get("prompt") or "").replace("{{SKILL_DIR}}", skill_dir) \
                                       .replace("{{SERVER_URL}}", task.get("server_url") or "")
    try:
        skill_files = json.loads(task.get("skill_files") or "{}")
        for _sn, files in skill_files.items():
            for rel, content in (files or {}).items():
                dest = _os.path.join(skill_dir, _sn, rel)
                _os.makedirs(_os.path.dirname(dest), exist_ok=True)
                with open(dest, "w", encoding="utf-8") as f:
                    f.write(content)
        if skill_files:
            env["DEVOPS_SKILLS_DIR"] = skill_dir
    except Exception as e:
        log.warning(f"skill materialize failed: {e}")
    # project-info 落地 + git worktree（coding/qa/sre，X 工作模型，见 multi-issue-isolation-design.md §3.4.1）
    proj_dir = _os.path.join(cwd, ".project-info")
    try:
        proj_files = json.loads(task.get("project_info_files") or "{}")
        for rel, content in (proj_files or {}).items():
            if rel == ".metadata.json":
                continue
            dest = _os.path.join(proj_dir, rel)
            _os.makedirs(_os.path.dirname(dest), exist_ok=True)
            with open(dest, "w", encoding="utf-8") as f:
                f.write(content)
        prompt = prompt.replace("{{PROJECT_INFO_DIR}}", proj_dir)
    except Exception as e:
        log.warning(f"project-info materialize failed: {e}")
    # 共享知识落地（event-conversation-design.md §3.1.1；随任务交付远端 worker）
    know_dir = _os.path.join(cwd, ".knowledge")
    try:
        know_files = json.loads(task.get("knowledge_files") or "{}")
        for rel, content in (know_files or {}).items():
            dest = _os.path.join(know_dir, rel)
            _os.makedirs(_os.path.dirname(dest), exist_ok=True)
            with open(dest, "w", encoding="utf-8") as f:
                f.write(content)
        prompt = prompt.replace("{{KNOWLEDGE_DIR}}", know_dir)
    except Exception as e:
        log.warning(f"knowledge materialize failed: {e}")
    try:
        from src.runtime import repogit
        _num = repogit.issue_num_from(task.get("issue_id") or "")
        if repogit.is_git_agent(task.get("agent_type") or "") and _num and task.get("issue_session_id"):
            _wt = repogit.ensure_issue_worktree(task.get("repo_key") or "", task.get("agent_type") or "",
                                                _num, token=task.get("repo_gitcode_token") or "")
            if _wt:
                env["GIT_ASKPASS"] = _os.path.join(_wt, ".git-askpass.sh")
    except Exception as e:
        log.warning(f"git worktree ensure failed: {e}")

    await client.task_start(tid)
    done = None
    batch = []
    try:
        from src.runtime.executor import stream_runtime
        _agent_label = f"[Agent:{task.get('agent_type') or ''}|repo:{task.get('repo_key') or ''}{_engine_tag}]"
        # 逐步日志前缀：seq + 非空的 issue/agent/repo/engine，便于在 journalctl 按事件/issue/仓库定位
        _esq_parts = [f"seq={task.get('event_seq') or '?'}"]
        _issue_num = (task.get("issue_id") or "").strip().lstrip("#")
        if _issue_num and _issue_num != "0":
            _esq_parts.append(f"issue:#{_issue_num}")
        _agent_type = task.get("agent_type") or ""
        if _agent_type:
            _esq_parts.append(f"agent:{_agent_type}")
        _repo_key = task.get("repo_key") or ""
        if _repo_key:
            _esq_parts.append(f"repo:{_repo_key}")
        if engine_name:
            _esq_parts.append(f"engine:{engine_name}")
        _esq = "|".join(_esq_parts)
        for evt_type, data in stream_runtime(
            prompt, cwd, session_id=task.get("runtime_session_id") or "",
            event_type=task.get("event_type") or "",
            backend_name="opencode",
            account=task.get("account") or "", user_id=task.get("user_id") or 0,
            env_extra=env, bot_token=task.get("repo_gitcode_token") or "",
            agent_label=_agent_label,
        ):
            if evt_type == "done":
                done = data
                continue
            # 逐步日志（本地模式同款，供 worker 侧 journalctl 观察执行过程）；前缀含 seq 便于定位
            if evt_type == "step_start":
                log.info(f"  [{_esq}] ▶ {data}")
            elif evt_type == "text":
                log.info(f"  [{_esq}] 💬 {str(data)[:120]}")
            elif evt_type == "reasoning":
                log.info(f"  [{_esq}] 🤔 {str(data)[:120]}")
            elif evt_type == "tool_use":
                log.info(f"  [{_esq}] 🔧 {str(data)[:150]}")
            elif evt_type == "step_finish":
                log.info(f"  [{_esq}] ✅ {data}")
            batch.append({"type": evt_type, "data": data if not isinstance(data, str) else data})
            if len(batch) >= 50:
                await client.report_messages(tid, batch)
                batch = []
            # 每次迭代让出事件循环，避免同步生成器迭代阻塞 claim/heartbeat
            # （见 worker-event-loop-block-rootcause.md 方案 1）
            await asyncio.sleep(0)
        if batch:
            await client.report_messages(tid, batch)
        if done:
            await client.complete(tid, done.get("text", ""), done.get("exit_code", 0),
                                  session_id=done.get("oc_session_id", ""), work_dir=cwd)
        else:
            await client.fail(tid, "no done event")
    except Exception as e:
        log.warning(f"task {tid} failed: {e}")
        await client.fail(tid, str(e))


async def _reregister(client, installed, device_name) -> list[int] | None:
    """重新注册获取新 runtime id。先重置连接池清除 CLOSE-WAIT 死连接，再注册。"""
    await client.reset_session()
    log.warning("re-registering with server to refresh runtime ids...")
    try:
        ids = await client.register(installed, device_name=device_name)
        if ids:
            for rid in ids:
                await client.recover_orphans(rid)
            log.info(f"re-registered successfully, new runtime ids: {ids}")
            return ids
        log.warning("re-register returned empty ids")
    except Exception as e:
        log.warning(f"re-register failed: {e}")
    return None


async def _run_loop(client, runtime_ids, poll_interval, heartbeat_interval, max_concurrent,
                    engine_by_rid: dict | None = None, installed=None, device_name="",
                    _test_execute=None, _test_active=None):
    """主循环：claim → 执行 → 回报，含心跳 + 自动重连自修复。

    自修复机制（三层防护，见 worker 自修复重连设计）：
    1. 心跳失败计数：连续 N 次 heartbeat 失败 → 重注册
    2. 定期重注册：每 RE_REGISTER_INTERVAL 秒主动刷新 runtime id，
       防止服务端重启后 id 静默失效（服务端对旧 id 仍返回 200 ok）
    3. 网络中断恢复：连接错误计入心跳失败，触发重注册
    """
    active_sessions = _test_active if _test_active is not None else set()  # D9 串行槽：同 session 已有活跃任务，跳过（服务端 claim 已排除，防御双保险）
    active_tasks = set()      # task_id 去重（防止同 task 被重复 claim 并发执行）
    last_heartbeat = 0.0
    heartbeat_fail_streak = 0
    HEARTBEAT_FAIL_THRESHOLD = 3
    RE_REGISTER_INTERVAL = float(os.environ.get("RUNTIME_REREGISTER_INTERVAL", "300"))
    last_reregister = asyncio.get_running_loop().time()
    engine_by_rid = engine_by_rid or {}

    while True:
        # ---- 定期重注册（防 id 静默失效）----
        now = asyncio.get_running_loop().time()
        if now - last_reregister >= RE_REGISTER_INTERVAL:
            new_ids = await _reregister(client, installed, device_name)
            if new_ids:
                runtime_ids = new_ids
                heartbeat_fail_streak = 0
            last_reregister = asyncio.get_running_loop().time()

        for rid in runtime_ids:
            try:
                task = await client.claim(rid)
            except Exception as e:
                log.warning(f"claim rid={rid} failed: {e}")
                heartbeat_fail_streak += 1
                continue
            if not task:
                continue
            tid = task.get("id", "")
            # 并发槽 key 与服务端 claim 串行粒度一致（multi-issue-isolation-design.md §3.3）：
            # 有关联 Issue → 按 issue_session_id（跨 Issue 并行）；无（#0/旧任务）→ 回退 agent_session_id。
            # 修复：此前一律用 agent_session_id，导致不同 Issue 的任务在客户端被误判冲突拒收
            #       （服务端已放行并标记 dispatched，客户端却 skip → 任务悬空卡死，见 seq#622）。
            sid = task.get("issue_session_id") or task.get("agent_session_id") or ""
            if sid in active_sessions:
                # D9 串行槽：同 session 已有活跃任务，跳过（服务端 claim 已排除，防御双保险）。
                # 回滚任务为 queued，避免 dispatched 悬空（skip 后无人执行也不重新排队）。
                log.warning(f"claim conflict: session {sid} busy, requeue task {tid}")
                try:
                    await client.requeue(tid)
                except Exception as _e:
                    log.warning(f"requeue task {tid} failed: {_e}")
                continue
            if tid in active_tasks:
                log.debug(f"task {tid} already active, skip")
                continue
            if len(active_sessions) >= max_concurrent:
                continue
            active_sessions.add(sid)
            active_tasks.add(tid)

            async def _wrap(t=task, _tid=tid, s=sid, e=engine_by_rid.get(rid, "")):
                task_timeout = _env_float("RUNTIME_TASK_TIMEOUT", 600)
                try:
                    if _test_execute is not None:
                        await _test_execute(client, t, e)
                    else:
                        await asyncio.wait_for(_execute_task(client, t, e), timeout=task_timeout)
                except asyncio.TimeoutError:
                    log.warning(f"task {_tid} timed out after {task_timeout}s, killing child processes and failing")
                    # 杀掉超时任务的 opencode 子进程（按 cwd 匹配，避免误杀其他任务）
                    _kill_stale_opencode(t)
                    try:
                        await client.fail(_tid, f"task timed out after {task_timeout}s")
                    except Exception:
                        pass
                finally:
                    active_sessions.discard(s)
                    active_tasks.discard(_tid)

            asyncio.create_task(_wrap())

        # ---- 心跳 + 失败检测 ----
        now = asyncio.get_running_loop().time()
        if now - last_heartbeat >= heartbeat_interval:
            ok = await client.heartbeat(runtime_ids)
            if ok:
                heartbeat_fail_streak = 0
                log.debug(f"heartbeat ok, ids={runtime_ids}")
            else:
                heartbeat_fail_streak += 1
                log.warning(f"heartbeat failed (streak={heartbeat_fail_streak}/{HEARTBEAT_FAIL_THRESHOLD}), resetting session")
                await client.reset_session()
            last_heartbeat = now

            if heartbeat_fail_streak >= HEARTBEAT_FAIL_THRESHOLD:
                log.warning(f"heartbeat failed {heartbeat_fail_streak} times in a row, triggering re-register")
                new_ids = await _reregister(client, installed, device_name)
                if new_ids:
                    runtime_ids = new_ids
                    heartbeat_fail_streak = 0
                    last_reregister = asyncio.get_running_loop().time()
                else:
                    backoff = min(30 * (2 ** (heartbeat_fail_streak - HEARTBEAT_FAIL_THRESHOLD)), 300)
                    log.warning(f"re-register failed, backing off {backoff:.0f}s")
                    await asyncio.sleep(backoff)

        await asyncio.sleep(poll_interval + random.uniform(0, poll_interval * 0.3))


def _read_local_worker_version() -> str:
    """读取本机 Worker 安装包版本（/etc/grape/worker.version，安装脚本写入）。"""
    try:
        with open("/etc/grape/worker.version", "r", encoding="utf-8") as f:
            return f.read().strip()
    except Exception:
        return ""


def main():
    token = os.environ.get("RUNTIME_TOKEN", "")
    server = os.environ.get("RUNTIME_SERVER_URL", "http://localhost:3000")
    poll = _env_float("RUNTIME_POLL_INTERVAL", 3)
    heartbeat = _env_float("RUNTIME_HEARTBEAT_INTERVAL", 15)
    max_concurrent = _env_int("RUNTIME_MAX_CONCURRENT_TASKS", 20)

    if not token:
        print("RUNTIME_TOKEN not set", file=sys.stderr)
        sys.exit(1)

    from src.runtime.client import RuntimeClient

    async def _amain():
        client = RuntimeClient(server, token)

        # 版本自检（worker-server-delivery-design.md §2.4）：服务端包版本不一致时提示升级
        try:
            latest = await client.latest_version()
            local = _read_local_worker_version()
            if latest and local and latest != local:
                log.warning(f"Worker 包版本不一致: 本地={local} 服务端={latest}，"
                            f"请重新执行一键安装命令升级: curl -sSL {server}/api/runtime/install | bash -s -- --token <token>")
        except Exception as e:
            log.warning(f"Worker 版本自检失败: {e}")

        runtimes = _discover_local_runtimes()
        installed = [r for r in runtimes if r.get("installed")]
        if not installed:
            log.warning("No runtimes installed locally, registering empty")
        device = os.environ.get("RUNTIME_DEVICE_NAME", os.uname().nodename if hasattr(os, "uname") else "host")
        ids = await client.register(installed, device_name=device)
        if not ids:
            log.error("Register failed (check RUNTIME_TOKEN / server URL)")
            return
        # rid → 引擎实例名（`{type}-{device}`，register 返回顺序与 installed 一致）
        from src.runtime.instances import instance_name
        engine_by_rid = {rid: instance_name(r.get("name", ""), device, 0)
                         for rid, r in zip(ids, installed)}
        log.info(f"RuntimeAgent registered: {ids} -> {server}, engines={list(engine_by_rid.values())}, "
                 f"poll={poll}s heartbeat={heartbeat}s max_concurrent={max_concurrent}")
        for rid in ids:
            await client.recover_orphans(rid)
        try:
            await _run_loop(client, ids, poll, heartbeat, max_concurrent, engine_by_rid,
                            installed=installed, device_name=device)
        finally:
            await client.close()

    asyncio.run(_amain())


if __name__ == "__main__":
    main()
