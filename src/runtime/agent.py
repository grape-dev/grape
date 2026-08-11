"""RuntimeAgent 边端入口（python -m runtime.agent，仅外连）。

见 agent-runtime-design.md §7.4 / tech-impl.md §1.4 / skill-delivery-remote.md §3.5。
流程：discover_runtimes → register → recover_orphans → 循环 claim（带 jitter）→ 执行 → 回报。
"""
import asyncio, json, os, random, sys

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


async def _execute_task(client, task):
    """执行单个任务：落地技能包 → 占位符替换 → stream_runtime → 回报。"""
    import os as _os
    tid = task.get("id", "")
    cwd = task.get("cwd") or _os.getcwd()
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
        for evt_type, data in stream_runtime(
            prompt, cwd, session_id=task.get("runtime_session_id") or "",
            event_type=task.get("event_type") or "",
            backend_name="opencode",
            account=task.get("account") or "", user_id=task.get("user_id") or 0,
            env_extra=env, bot_token=task.get("repo_gitcode_token") or "",
        ):
            if evt_type == "done":
                done = data
                continue
            # 逐步日志（本地模式同款，供 worker 侧 journalctl 观察执行过程）
            if evt_type == "step_start":
                log.info(f"  ▶ {data}")
            elif evt_type == "text":
                log.info(f"  💬 {str(data)[:120]}")
            elif evt_type == "reasoning":
                log.info(f"  🤔 {str(data)[:120]}")
            elif evt_type == "tool_use":
                log.info(f"  🔧 {str(data)[:150]}")
            elif evt_type == "step_finish":
                log.info(f"  ✅ {data}")
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


async def _run_loop(client, runtime_ids, poll_interval, heartbeat_interval, max_concurrent):
    active_sessions = set()
    last_heartbeat = 0.0
    while True:
        for rid in runtime_ids:
            try:
                task = await client.claim(rid)
            except Exception as e:
                log.warning(f"claim rid={rid} failed: {e}")
                continue
            if not task:
                continue
            sid = task.get("agent_session_id") or ""
            if sid in active_sessions:
                # D9 串行槽：同 session 已有活跃任务，跳过（服务端 claim 已排除，防御双保险）
                log.warning(f"claim conflict: session {sid} busy, skip task {task.get('id')}")
                continue
            if len(active_sessions) >= max_concurrent:
                continue
            active_sessions.add(sid)

            async def _wrap(t=task, s=sid):
                try:
                    await _execute_task(client, t)
                finally:
                    active_sessions.discard(s)

            asyncio.create_task(_wrap())
        now = asyncio.get_running_loop().time()
        if now - last_heartbeat >= heartbeat_interval:
            await client.heartbeat(runtime_ids)
            last_heartbeat = now
        await asyncio.sleep(poll_interval + random.uniform(0, poll_interval * 0.3))


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
        runtimes = _discover_local_runtimes()
        installed = [r for r in runtimes if r.get("installed")]
        if not installed:
            log.warning("No runtimes installed locally, registering empty")
        device = os.environ.get("RUNTIME_DEVICE_NAME", os.uname().nodename if hasattr(os, "uname") else "host")
        ids = await client.register(installed, device_name=device)
        if not ids:
            log.error("Register failed (check RUNTIME_TOKEN / server URL)")
            return
        for rid in ids:
            await client.recover_orphans(rid)
        log.info(f"RuntimeAgent registered: {ids} -> {server}, poll={poll}s heartbeat={heartbeat}s max_concurrent={max_concurrent}")
        try:
            await _run_loop(client, ids, poll, heartbeat, max_concurrent)
        finally:
            await client.close()

    asyncio.run(_amain())


if __name__ == "__main__":
    main()
