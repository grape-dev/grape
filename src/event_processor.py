"""事件处理器：归一化、验证、路由、执行"""
import json, os, threading, concurrent.futures, re
from datetime import datetime
from src.config import log, GITCODE_WEBHOOK_TOKEN, BOT_USERNAMES, WORKSPACE_DIR, SKILLS_DIR, get_domain_skill
from src.session_manager import db_lock, get_or_create_session, update_session
from src.prompt_builder import build_prompt, extract_event_number
from src.agent_executor import stream_opencode

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
def normalize_event(raw, event_header):
    if isinstance(raw, bytes):
        hex_preview = raw[:200].hex()
        log.info(f"DEBUG normalize: first bytes hex={hex_preview}")
        text = raw.decode("utf-8", errors="replace")
        if "\ufffd" in text:
            log.warning("UTF-8 decode had replacement chars, trying GBK")
            try:
                text = raw.decode("gbk")
            except:
                text = raw.decode("utf-8", errors="replace")
        payload = json.loads(text)
    elif isinstance(raw, str):
        payload = json.loads(raw)
    else:
        payload = raw
    event_type_map = {
        "push": "push", "issues": "issue_opened", "issue": "issue_opened",
        "Issue Hook": "issue_opened", "Note Hook": "issue_comment",
        "Merge Request Hook": "pull_request_opened", "pull_request": "pull_request_opened",
        "tag_push": "tag_created", "release": "release_created", "pipeline": "build_start"
    }
    evt_type = event_type_map.get(event_header, event_header)
    repo = ""
    if "project" in payload:
        p = payload["project"]
        if isinstance(p, dict):
            repo = p.get("path_with_namespace", p.get("full_name", p.get("name", "")))
    if not repo and "repository" in payload:
        r = payload["repository"]
        if isinstance(r, dict):
            repo = r.get("full_name", r.get("name", ""))
        elif isinstance(r, str):
            repo = r
    actor = payload.get("user_name", "")
    if not actor:
        sender = payload.get("sender", {})
        if isinstance(sender, dict):
            actor = sender.get("username", sender.get("login", ""))
    if not actor:
        issue = payload.get("issue", {})
        if isinstance(issue, dict):
            author = issue.get("author", {})
            if isinstance(author, dict):
                actor = author.get("username", "")
    if not actor:
        user = payload.get("user", {})
        if isinstance(user, dict):
            actor = user.get("username", user.get("login", ""))
    branch = ""
    ref = payload.get("ref", "")
    if isinstance(ref, str) and ref.startswith("refs/heads/"):
        branch = ref[11:]
    return {"source": "gitcode", "type": evt_type, "repository": repo, "branch": branch, "actor": actor, "payload": payload}


# ====== 事件处理入口 ======
def process_event(raw_body, headers):
    try:
        event_header = headers.get("X-GitCode-Event", "unknown")
        evt = normalize_event(raw_body, event_header)
        repo_key = f"{evt['source']}:{evt['repository']}"
        log.info(f"═══ Event: {evt['type']} on {evt['repository']} by {evt['actor']} ═══")

        # 前置过滤
        if not evt['repository'] or evt['type'] == 'unknown':
            log.info(f"Skipped malformed event: type={evt['type']}, repo={evt['repository']}")
            return

        # 自循环跳过
        user = evt['payload'].get('user', {})
        username = user.get('username', '') if isinstance(user, dict) else ''
        if username in BOT_USERNAMES:
            log.info(f"Skip: self-triggered event from {username}")
            return

        lock = _acquire_repo_lock(repo_key)
        lock.acquire(timeout=180)
        try:
            _process_event_inner(evt, repo_key)
        finally:
            lock.release()

    except Exception as e:
        import traceback
        log.error(f"Error processing event: {e}")
        traceback.print_exc()


# ====== 事件处理主体 ======
def _process_event_inner(evt, repo_key):
    try:
        ws = os.path.join(WORKSPACE_DIR, evt['repository'])
        os.makedirs(ws, exist_ok=True)

        sid, history, oc_session_id, is_new = get_or_create_session(repo_key, evt['source'], evt['repository'])
        log.info(f"Session: {sid} (new={is_new}, history={len(history)} events, oc_session={oc_session_id[:30] if oc_session_id else 'none'})")

        skills = [get_domain_skill(evt['type'])]
        log.info(f"Skills: {skills}")

        prompt = build_prompt(evt, history, oc_session_id, skills)
        log.info(f"Prompt built ({len(prompt)} chars)")

        log.info(f"═══ Spawning opencode with --format json ═══")
        event_count = 0
        final_text = ""
        new_oc_session_id = oc_session_id

        for event_type, data in stream_opencode(prompt, ws, oc_session_id, evt['type']):
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
            "actor": evt["actor"],
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
