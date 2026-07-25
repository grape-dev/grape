"""事件处理器：归一化、验证、路由、执行"""
import json, os, threading, concurrent.futures, re, urllib.request
from datetime import datetime
from src.config import log, GITCODE_WEBHOOK_TOKEN, BOT_USERNAMES, WORKSPACE_DIR, SKILLS_DIR, get_domain_skill, get_agent_backend
from src.session_manager import db_lock, get_or_create_session, update_session, get_feishu_mapping, get_feishu_group_name_map
from src.prompt_builder import build_prompt, extract_event_number
from src.agent.executor import stream_agent

# ====== 事件队列 ======
_MAX_WORKERS = 10
_MAX_QUEUE = 20
_task_slot = threading.BoundedSemaphore(_MAX_QUEUE)
_event_executor = None


def _init_executor():
    global _event_executor
    if _event_executor is None:
        _event_executor = concurrent.futures.ThreadPoolExecutor(
            max_workers=_MAX_WORKERS, thread_name_prefix="evt"
        )


def _run_with_slot(fn, *args, **kwargs):
    try:
        fn(*args, **kwargs)
    finally:
        _task_slot.release()


def _submit_event(fn, *args, **kwargs):
    if not _task_slot.acquire(blocking=False):
        log.error(f"Task queue full (max={_MAX_QUEUE})! Dropping event")
        return
    _init_executor()
    _event_executor.submit(_run_with_slot, fn, *args, **kwargs)


# ====== Per-repo 锁 ======
_repo_locks = {}
_repo_locks_lock = threading.Lock()


def _acquire_repo_lock(repo_key: str) -> threading.Lock:
    with _repo_locks_lock:
        if repo_key not in _repo_locks:
            _repo_locks[repo_key] = threading.Lock()
        return _repo_locks[repo_key]


# ====== 事件归一化 ======
from src.vcs.adapter import normalize_event, get_event_header

# ====== 事件处理入口 ======
def process_event(raw_body, headers, account="default"):
    try:
        event_header, source = get_event_header(headers)
        evt = normalize_event(raw_body, event_header, source)
        repo_key = f"{evt['source']}:{evt['repository']}"
        evt["account"] = account
        log.info(f"═══ Event: {evt['type']} on {evt['repository']} by {evt['actor']} (account={account}) ═══")

        # 前置过滤
        if not evt['repository'] or evt['type'] == 'unknown':
            log.info(f"Skipped malformed event: type={evt['type']}, repo={evt['repository']}")
            return

        # 前置通知：/notify on 时才提交 im-notify 技能（不受防循环限制）
        if evt.get("type") not in ("im_notify",):
            from src.session_manager import get_notify_enabled
            if get_notify_enabled(repo_key):
                notify_evt = dict(evt)
                notify_evt["type"] = "im_notify"
                notify_evt["original_type"] = evt.get("type", "")
                log.info(f"Submit to im-notify: original={evt['type']}")
                _submit_event(_process_event_inner, notify_evt, repo_key, account)

        # 自循环跳过（仅评论会导致无限循环）
        user = evt['payload'].get('user', {})
        username = user.get('username', '') if isinstance(user, dict) else ''
        if username in BOT_USERNAMES:
            if evt['type'] in ('issue_comment', 'note_hook_pr'):
                log.info(f"Skip: self-comment from {username} ({evt['type']})")
                return
            log.info(f"Self-triggered event from {username}: {evt['type']}")

        lock = _acquire_repo_lock(repo_key)
        lock.acquire(timeout=180)
        try:
            _process_event_inner(evt, repo_key, evt.get("account", "default"))
        finally:
            lock.release()

    except Exception as e:
        import traceback
        log.error(f"Error processing event: {e}")
        traceback.print_exc()


# ====== 事件处理主体 ======
MAX_A2A_DEPTH = 5
_a2a_depth = {}  # repo_key -> depth counter


def process_a2a_event(body: dict):
    """处理 A2A API 请求，路由到指定 Agent"""
    import json, os
    agent_name = body.get("agent", "")
    agents_file = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "configs", "agent-description.json")
    try:
        with open(agents_file, encoding="utf-8") as f:
            registry = json.load(f)
    except Exception:
        registry = {}
    if agent_name not in registry:
        log.warning(f"A2A agent not found in registry: {agent_name}")
        log.warning(f"Available agents: {list(registry.keys())}")
        return
    payload = body.get("payload", {})
    repository = payload.get("repository", "")
    account = payload.get("account", "default")
    repo_key = f"gitcode:{repository}"
    # 深度检查
    current_depth = _a2a_depth.get(repo_key, 0)
    if current_depth >= MAX_A2A_DEPTH:
        log.warning(f"A2A max depth ({MAX_A2A_DEPTH}) reached for {repo_key}")
        return
    _a2a_depth[repo_key] = current_depth + 1
    a2a_evt = {
        "type": "a2a_event",
        "repository": repository,
        "source": "gitcode",
        "actor": "bot",
        "payload": payload,
        "_override_agent": agent_name,
        "_a2a_context": payload.get("context", ""),
        "account": account,
    }
    _process_event_inner(a2a_evt, repo_key, account)
    _a2a_depth[repo_key] = _a2a_depth.get(repo_key, 1) - 1


def _get_feishu_context(repo_key, repository):
    """Pre-fetch Feishu notification context for prompt injection.
    Returns dict with collaborators, group_members, mappings, token, chat_id."""
    ctx = {}
    try:
        from src.im.feishu import get_chat_id_for_repo, get_chat_members, get_tenant_token, ensure_configured
        if not ensure_configured():
            return ctx
        chat_id = get_chat_id_for_repo(repo_key)
        if not chat_id:
            return ctx
        ctx["chat_id"] = chat_id
        ctx["group_members"] = get_chat_members(chat_id)
        ctx["mappings"] = get_feishu_mapping(repo_key)
        ctx["token"] = get_tenant_token()
        from src.session_manager import get_auto_impl_enabled, get_notify_enabled
        ctx["auto_impl_enabled"] = get_auto_impl_enabled(repo_key)
        ctx["notify_enabled"] = get_notify_enabled(repo_key)
        # Fetch collaborators using VCS backend
        from src.vcs.gitcode import GitCodeBackend
        backend = GitCodeBackend()
        parts = repository.split("/", 1)
        if len(parts) == 2:
            url = backend.get_collaborators_url(parts[0], parts[1])
        else:
            url = f"https://api.gitcode.com/api/v5/repos/{repository}/collaborators"
        try:
            collaborators = []
            page = 1
            while True:
                page_url = f"{url}?per_page=100&page={page}"
                resp = urllib.request.urlopen(urllib.request.Request(page_url), timeout=10)
                data = json.loads(resp.read())
                if not data:
                    break
                collaborators.extend(data)
                page += 1
            ctx["collaborators"] = collaborators
            for m in collaborators:
                if m.get("role_name") == "Owner":
                    ctx["owner_login"] = m.get("username", "")
                    ctx["owner_nick"] = m.get("nick_name", "")
                    break
        except Exception as e:
            log.warning(f"Failed to fetch collaborators: {e}")
    except Exception as e:
        log.warning(f"Feishu context build error: {e}")
    return ctx


def _process_event_inner(evt, repo_key, account="default"):
    try:
        ws = os.path.join(WORKSPACE_DIR, evt['repository'])
        os.makedirs(ws, exist_ok=True)

        sid, history, oc_sid, cc_sid, is_new = get_or_create_session(repo_key, evt['source'], evt['repository'])
        log.info(f"Session: {sid} (new={is_new}, history={len(history)} events, oc_session={oc_sid[:30] if oc_sid else 'none'})")

        # A2A override: 使用指定的 agent 而非按事件类型路由
        override_agent = evt.pop("_override_agent", "")
        if override_agent:
            skills = [override_agent]
            log.info(f"A2A override: routing to agent={override_agent}")
        else:
            skills = [get_domain_skill(evt['type'], evt.get("repository", ""), 0)]
        log.info(f"Skills: {skills}")
        if not skills[0]:
            log.info(f"No skill for event type {evt['type']}, skipping")
            return
        backend = get_agent_backend(skills[0])
        log.info(f"Backend: {backend}")
        oc_session_id = {"opencode": oc_sid, "codearts": cc_sid}.get(backend, "")

        feishu_ctx = _get_feishu_context(repo_key, evt['repository'])
        prompt = build_prompt(evt, history, oc_session_id, skills, feishu_ctx)
        log.info(f"Prompt built ({len(prompt)} chars)")

        log.info(f"═══ Spawning agent (backend={backend}) with --format json ═══")

        account = evt.get("account", "default")

        event_count = 0
        final_text = ""
        new_oc_session_id = oc_sid

        env_extra = {
            "REPO_FULL_NAME": evt['repository'],
            "ACCOUNT": account,
            "CURRENT_EVENT_TYPE": evt['type'],
        }
        # 从事件 payload 提取 Issue/PR 编号
        from src.prompt_builder import extract_event_number
        evt_number = extract_event_number(evt.get("payload", {}))
        if evt_number is not None:
            env_extra["ISSUE_NUMBER"] = str(evt_number)
            env_extra["PR_NUMBER"] = str(evt_number)
        # 提取 PR head SHA
        payload = evt.get("payload", {})
        if isinstance(payload, dict):
            pr_data = payload.get("pull_request") or payload.get("merge_request") or {}
            if isinstance(pr_data, dict):
                head = pr_data.get("head", {})
                if isinstance(head, dict) and head.get("sha"):
                    env_extra["HEAD_SHA"] = head["sha"]
        if "a2a_event" in evt.get("type", "") or evt.get("_a2a_context"):
            env_extra["IS_A2A"] = "1"

        for event_type, data in stream_agent(prompt, ws, new_oc_session_id, evt['type'], backend_name=backend, account=account, env_extra=env_extra):
            event_count += 1
            if event_type == "step_start":
                log.info(f"  ▶ {data}")
            elif event_type == "text":
                log.info(f"  💬 {data[:120]}")
            elif event_type == "reasoning":
                log.info(f"  🤔 {data[:120]}")
            elif event_type == "tool_use":
                log.info(f"  🔧 {data[:150]}")
            elif event_type == "step_finish":
                log.info(f"  ✅ {data}")
            elif event_type == "error":
                log.info(f"  ❌ ERROR: {data}")
            elif event_type == "done":
                d = data
                final_text = d["text"]
                exit_code = d["exit_code"]
                if exit_code == 0 and d.get("oc_session_id"):
                    new_oc_session_id = d["oc_session_id"]
                log.info(f"═══ opencode done (exit={exit_code}, text_len={len(final_text)}, events={event_count}) ═══")

        record = {
            "type": evt["type"], "time": datetime.now().isoformat(),
            "success": exit_code == 0, "exit_code": exit_code,
            "actor": evt["actor"], "account": account,
            "backend": backend,
            "skill": skills[0],
            "stdout": final_text[:2000],
            "oc_session_id": new_oc_session_id
        }
        history.append(record)
        from src.session_manager import dump_history_to_file
        dump_history_to_file(f"{evt['source']}:{evt['repository']}", history, account)
        update_session(sid, history, "idle" if exit_code == 0 else "failed", new_oc_session_id, backend)

        if exit_code != 0:
            log.warning(f"Agent failed (exit={exit_code})")
            return

        log.info(f"Agent response ({len(final_text)} chars)")
        for line in final_text.strip().split("\n")[:15]:
            log.info(f"  | {line[:200]}")

    except Exception as e:
        import traceback
        log.error(f"Error processing event: {e}")
        traceback.print_exc()
