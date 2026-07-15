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

        # 自循环跳过（仅评论会导致无限循环）
        user = evt['payload'].get('user', {})
        username = user.get('username', '') if isinstance(user, dict) else ''
        if username in BOT_USERNAMES:
            if evt['type'] == 'issue_comment':
                log.info(f"Skip: self-comment from {username}")
                return
            if evt['type'] == 'issue_opened':
                changes = evt['payload'].get('changes', {})
                if isinstance(changes, dict) and "labels" in changes:
                    evt['type'] = 'issue_labeled'
                    log.info(f"Bot label change, routing to implementation")
                else:
                    log.info(f"Self-triggered issue_opened from {username}")

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

        sid, history, oc_session_id, is_new = get_or_create_session(repo_key, evt['source'], evt['repository'])
        log.info(f"Session: {sid} (new={is_new}, history={len(history)} events, oc_session={oc_session_id[:30] if oc_session_id else 'none'})")

        skills = [get_domain_skill(evt['type'])]
        log.info(f"Skills: {skills}")
        if not skills[0]:
            log.info(f"No skill for event type {evt['type']}, skipping")
            return
        backend = get_agent_backend(skills[0])
        log.info(f"Backend: {backend}")

        feishu_ctx = _get_feishu_context(repo_key, evt['repository'])
        prompt = build_prompt(evt, history, oc_session_id, skills, feishu_ctx)
        log.info(f"Prompt built ({len(prompt)} chars)")

        log.info(f"═══ Spawning agent (backend={backend}) with --format json ═══")

        # 注入华为云凭据到环境变量
        from src.config import get_account
        acc = get_account(evt.get("account", "default"))
        if acc.get("huawei_cloud_ak"):
            os.environ["HUAWEICLOUD_SDK_AK"] = acc["huawei_cloud_ak"]
            os.environ["HUAWEICLOUD_SDK_SK"] = acc.get("huawei_cloud_sk", "")
            log.info("Injected Huawei Cloud AK/SK into env")

        event_count = 0
        final_text = ""
        new_oc_session_id = oc_session_id

        for event_type, data in stream_agent(prompt, ws, oc_session_id, evt['type'], backend_name=backend):
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
                new_oc_session_id = d["oc_session_id"] or new_oc_session_id
                log.info(f"═══ opencode done (exit={exit_code}, text_len={len(final_text)}, events={event_count}) ═══")

        record = {
            "type": evt["type"], "time": datetime.now().isoformat(),
            "success": exit_code == 0, "exit_code": exit_code,
            "actor": evt["actor"], "account": account,
            "stdout": final_text[:2000],
            "oc_session_id": new_oc_session_id
        }
        history.append(record)
        update_session(sid, history, "idle" if exit_code == 0 else "failed", new_oc_session_id)

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
