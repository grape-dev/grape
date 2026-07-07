"""
DevOps Bot v2 - NDJSON streaming agent executor
Architecture: Bot = shell, Agent = brain.
Bot listens events → builds prompt → spawns opencode with NDJSON streaming
Agent autonomously reads skills, calls GitCode API, takes actions
Bot monitors & logs the Agent's execution process
"""
import json, os, subprocess, sys, time, sqlite3, uuid, threading, logging, re, socket, concurrent.futures
from http.server import HTTPServer, BaseHTTPRequestHandler
from datetime import datetime
from urllib.request import Request, urlopen
from urllib.error import URLError

# .env 加载
_env_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
if os.path.exists(_env_path):
    with open(_env_path, encoding="utf-8") as _f:
        for _line in _f:
            _line = _line.strip()
            if not _line or _line.startswith("#") or "=" not in _line:
                continue
            _k, _v = _line.split("=", 1)
            _k, _v = _k.strip(), _v.strip().strip("\"'")
            if not os.environ.get(_k):
                os.environ[_k] = _v

from feishu_handler import (
    FEISHU_BINDINGS_TABLE_SQL, FEISHU_APP_ID, FEISHU_APP_SECRET,
    FEISHU_BOT_NAME, reply_text, resolve_repo, process_command,
    detect_bind_intent, start_ws, stop_ws
)

GITCODE_TOKEN = os.environ.get("GITCODE_WEBHOOK_TOKEN", "")
SKILLS_DIR = os.path.abspath("skills")
WORKSPACE_DIR = os.path.abspath("data/workspaces")
LOG_DIR = os.path.abspath("data/logs")
os.makedirs(WORKSPACE_DIR, exist_ok=True)
os.makedirs(LOG_DIR, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    handlers=[logging.handlers.RotatingFileHandler("data/devops-bot.log", maxBytes=10*1024*1024, backupCount=5, encoding="utf-8"), logging.StreamHandler()]
)
log = logging.getLogger("devops-bot")

db_lock = threading.Lock()

def init_db():
    conn = sqlite3.connect("data/sessions.db", check_same_thread=False)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS sessions (
            id TEXT PRIMARY KEY,
            repo_key TEXT UNIQUE,
            source TEXT, repository TEXT, status TEXT,
            event_history TEXT DEFAULT '[]',
            oc_session_id TEXT DEFAULT '',
            created_at TEXT, last_active_at TEXT
        )
    """)
    conn.execute(FEISHU_BINDINGS_TABLE_SQL)
    conn.commit()
    return conn

db = init_db()

def get_or_create_session(repo_key, source, repository):
    with db_lock:
        c = db.cursor()
        c.execute("SELECT id, event_history, oc_session_id, status FROM sessions WHERE repo_key=?", (repo_key,))
        row = c.fetchone()
        if row:
            sid, hist_json, oc_sid, status = row
            history = json.loads(hist_json) if hist_json else []
            # If session was failed, reset to idle
            if status == "failed":
                c.execute("UPDATE sessions SET status=? WHERE id=?", ("idle", sid))
                db.commit()
            return sid, history, oc_sid, False
        sid = f"sess-{repo_key}-{int(time.time()*1000000)}"
        now = datetime.now().isoformat()
        c.execute(
            "INSERT INTO sessions (id, repo_key, source, repository, status, event_history, created_at, last_active_at) VALUES (?,?,?,?,?,?,?,?)",
            (sid, repo_key, source, repository, "idle", "[]", now, now)
        )
        db.commit()
        return sid, [], "", True

def update_session(sid, history, status, oc_session_id=None):
    with db_lock:
        c = db.cursor()
        if oc_session_id:
            c.execute(
                "UPDATE sessions SET event_history=?, status=?, oc_session_id=?, last_active_at=? WHERE id=?",
                (json.dumps(history), status, oc_session_id, datetime.now().isoformat(), sid)
            )
        else:
            c.execute(
                "UPDATE sessions SET event_history=?, status=?, last_active_at=? WHERE id=?",
                (json.dumps(history), status, datetime.now().isoformat(), sid)
            )
        db.commit()

def extract_event_number(payload):
    for key in ["issue", "pull_request", "merge_request"]:
        obj = payload.get(key, {})
        if isinstance(obj, dict):
            if "number" in obj:
                return obj["number"]
            if "iid" in obj:
                return obj["iid"]
    obj_attrs = payload.get("object_attributes", {})
    log.info(f"DEBUG extract_number: keys={list(payload.keys())[:10]}")
    if isinstance(obj_attrs, dict):
        log.info(f"DEBUG extract_number: obj_attrs keys={list(obj_attrs.keys())[:10]}")
        if "noteable_iid" in obj_attrs:
            return obj_attrs["noteable_iid"]
        if "iid" in obj_attrs:
            return obj_attrs["iid"]
        if "id" in obj_attrs:
            log.info(f"DEBUG extract_number: obj_attrs.id={obj_attrs['id']} (noteable_id fallback)")
            return obj_attrs["id"]
    if "number" in payload:
        return payload["number"]
    log.warning(f"extract_event_number: could not find number in payload keys={list(payload.keys())}")
    return None

def build_role_definition() -> str:
    return (
        "You are a DevOps operations assistant. Your domain:\n"
        "- Code review, build diagnosis, deployment guard, incident triage\n"
        "- Issue/PR analysis, changelog generation, repository management\n"
        "- GitCode API operations\n"
        "\n"
        "You have no abilities outside DevOps (no weather, email, documents, "
        "PDF, Excel, writing, meetings, voice, etc.).\n"
        "Reply rules:\n"
        "1. User @mentioned you: always reply politely, state you only do DevOps\n"
        "2. User NOT @mentioned + DevOps topic: analyze and reply\n"
        "3. User NOT @mentioned + off-topic: output SKIP\n"
        "4. Never write code, modify files, or execute git commands"
    )


def get_skill_capabilities() -> str:
    """从 skills/ 目录动态读取所有技能，构建能力列表"""
    emoji_map = {
        "code-review": "📦 代码审查",
        "build-debug": "🔧 构建诊断",
        "deploy-guard": "🛡️ 部署守卫",
        "incident-triage": "🚨 事件分诊",
        "security-scan": "🔒 安全扫描",
        "changelog-gen": "📝 变更日志",
        "devops-event-ops": "🔄 事件编排",
        "comment": "💬 仓库评论回复",
        "chat": "🗨️ 飞书对话",
    }
    lines = ["我是 DevOps Bot，专注于以下领域：\n"]
    try:
        for entry in sorted(os.listdir(SKILLS_DIR)):
            skill_dir = os.path.join(SKILLS_DIR, entry)
            if not os.path.isdir(skill_dir) or entry.startswith("."):
                continue
            md_path = os.path.join(skill_dir, "SKILL.md")
            if not os.path.exists(md_path):
                continue
            display = emoji_map.get(entry, entry)
            # Read description from frontmatter
            desc = ""
            with open(md_path, encoding="utf-8") as f:
                content = f.read()
            if "description: |" in content:
                desc_block = content.split("description: |", 1)[1].split("---", 1)[0].strip()
                desc = desc_block.split("\n")[0].strip().rstrip(".。").strip('"').strip("'")
            lines.append(f"  {display}")
            if desc:
                lines[-1] += f" — {desc}"
        lines.append("\n我的能力范围仅限于 DevOps 相关领域。")
        return "\n".join(lines)
    except Exception as e:
        log.warning(f"Failed to read skills: {e}")
        return "我是 DevOps Bot，专注于 DevOps 相关领域。"


def build_prompt(evt, history, oc_session_id, skills):
    number = extract_event_number(evt['payload'])
    repo = evt['repository']
    payload = evt['payload']

    issue_data = {}
    if isinstance(payload.get("issue"), dict):
        issue_data = payload["issue"]
    elif isinstance(payload.get("object_attributes"), dict):
        issue_data = payload["object_attributes"]

    title = json.dumps(issue_data.get("title", ""), ensure_ascii=True)
    body = json.dumps(issue_data.get("body", issue_data.get("description", "")), ensure_ascii=True)[:500]

    parts = []
    evt_type = evt.get("type", "event")

    if evt_type == "issue_comment":
        # issue_comment 使用"用户评论"风格，非"事件"风格
        note = ""
        if isinstance(payload.get("object_attributes"), dict):
            note = payload["object_attributes"].get("note", "")
        prefix = ""
        if number is not None:
            prefix = f"User commented on issue #{number} of {repo}"
        else:
            prefix = f"User commented on {repo}"
        parts.append(prefix)
        if note:
            parts.append(f'Comment: {json.dumps(note, ensure_ascii=True)[:1000]}')
            if "@hwc-skills-bot" in note or "@skill-bot" in note:
                parts.append("(User @mentioned the bot)")
    else:
        if number is not None:
            parts.append(f"Event: {evt_type} #{number} on {repo}")
        else:
            parts.append(f"Event: {evt_type} on {repo}")
        if title != '""':
            parts.append(f"Title: {title}")
        if body != '""':
            parts.append(f"Body: {body}")

    if history:
        parts.append(f"Previous events in this session: {len(history)}")
        for h in history[-3:]:
            s = "ok" if h.get("success") else "fail"
            parts.append(f"  [{s}] {h.get('type','')}")

    if skills:
        parts.append(f"Skills: {', '.join(skills)}")
        parts.append(f"Respond based on the {skills[0]} skill.")

    parts.append(build_role_definition())
    return "\n".join(parts)

def stream_opencode(prompt, cwd, oc_session_id, event_type=""):
    """
    Spawn opencode with --format json, parse NDJSON stream in real-time.
    Yields (event_type, data) tuples for monitoring.
    Also captures opencode session ID from step_start for resume.
    """
    env = os.environ.copy()
    env["DEVOPS_SKILLS_DIR"] = SKILLS_DIR
    env["DEVOPS_EVENT_TYPE"] = event_type

    args = ["run", "--format", "json", "--dangerously-skip-permissions"]
    if oc_session_id:
        args.extend(["--session", oc_session_id])
    args.append(prompt)

    log.info(f"opencode args: {' '.join(args)[:200]}...")
    log.info(f"opencode cwd: {cwd}")

    proc = subprocess.Popen(
        ["C:\\Users\\janso\\AppData\\Roaming\\npm\\opencode.cmd"] + args,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        cwd=cwd,
        env=env,
        bufsize=0
    )

    collected_text = []
    new_oc_session_id = oc_session_id
    error_events = []
    step_count = 0

    for raw_line in iter(proc.stdout.readline, b""):
        line = raw_line.decode("utf-8", errors="replace").strip()
        if not line:
            continue
        try:
            event = json.loads(line)
            et = event.get("type", "unknown")

            if et == "step_start":
                step_count += 1
                sid = event.get("sessionID", "")
                if sid:
                    new_oc_session_id = sid
                yield ("step_start", f"Step {step_count} started (session: {new_oc_session_id})")

            elif et == "text":
                part = event.get("part", {})
                if isinstance(part, dict):
                    text = part.get("text", "")
                    ptype = part.get("type", "")
                    if text:
                        if ptype == "reasoning":
                            yield ("reasoning", text[:200])
                        elif ptype == "text":
                            collected_text.append(text)
                            # Print first 100 chars for monitoring
                            preview = text[:150].replace("\n", " ")
                            yield ("text", preview)

            elif et == "reasoning":
                part = event.get("part", {})
                if isinstance(part, dict):
                    text = part.get("text", "")
                    if text:
                        yield ("reasoning", text[:200])

            elif et == "tool_use":
                part = event.get("part", {})
                tool = ""
                if isinstance(part, dict):
                    tool = part.get("tool", "")
                    inp = part.get("state", {}).get("input", {})
                yield ("tool_use", f"Tool: {tool} {json.dumps(inp)[:200]}")

            elif et == "step_finish":
                tokens = event.get("part", {}).get("tokens", {})
                total = tokens.get("total", 0)
                yield ("step_finish", f"Step {step_count} done, tokens={total}")

            elif et == "error":
                err = event.get("error", {}).get("data", {}).get("message", str(event))
                error_events.append(err)
                yield ("error", err)

            else:
                yield ("other", et)

        except json.JSONDecodeError:
            yield ("raw", line[:200])

    proc.wait()
    stdout_remainder = ""
    try:
        stdout_remainder = proc.stdout.read().decode("utf-8", errors="replace")
    except:
        pass
    stderr_output = ""
    try:
        stderr_output = proc.stderr.read().decode("utf-8", errors="replace")[:500]
    except:
        pass

    # Collect non-empty NDJSON lines from remaining stdout
    if stdout_remainder:
        for line in stdout_remainder.strip().split("\n"):
            line = line.strip()
            if not line:
                continue
            try:
                event = json.loads(line)
                if event.get("type") == "text":
                    part = event.get("part", {})
                    if isinstance(part, dict):
                        text = part.get("text", "")
                        if text:
                            collected_text.append(text)
            except json.JSONDecodeError:
                pass

    full_text = "\n".join(collected_text)
    exit_code = proc.returncode

    yield ("done", {
        "exit_code": exit_code,
        "text": full_text,
        "oc_session_id": new_oc_session_id,
        "errors": error_events,
        "stderr": stderr_output[:500] if stderr_output else ""
    })

class Handler(BaseHTTPRequestHandler):
    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length) if length > 0 else b""
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"ok")
        _submit_event(process_event, body, self.headers)

    def do_GET(self):
        if self.path == "/health":
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            skills = [d for d in os.listdir(SKILLS_DIR) if os.path.isdir(os.path.join(SKILLS_DIR, d))]
            self.wfile.write(json.dumps({"status":"ok","agents":["opencode"],"skills":skills}).encode())
        elif self.path == "/api/sessions":
            with db_lock:
                c = db.cursor()
                c.execute("SELECT id, repo_key, source, repository, status, event_history, oc_session_id, created_at, last_active_at FROM sessions ORDER BY last_active_at DESC")
                rows = c.fetchall()
            sessions = []
            for r in rows:
                sessions.append({
                    "id":r[0],"repo_key":r[1],"source":r[2],"repository":r[3],"status":r[4],
                    "event_history":json.loads(r[5] or "[]"),"oc_session_id":r[6],
                    "created_at":r[7],"last_active_at":r[8]
                })
            self.send_response(200)
            self.send_header("Content-Type","application/json")
            self.end_headers()
            self.wfile.write(json.dumps(sessions, ensure_ascii=False).encode())
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, fmt, *args):
        pass

def normalize_event(raw, event_header):
    if isinstance(raw, bytes):
        # Debug: log hex of first few bytes to detect encoding
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
        "push":"push","issues":"issue_opened","issue":"issue_opened",
        "Issue Hook":"issue_opened","Note Hook":"issue_comment",
        "Merge Request Hook":"pull_request_opened","pull_request":"pull_request_opened",
        "tag_push":"tag_created","release":"release_created","pipeline":"build_start"
    }
    evt_type = event_type_map.get(event_header, event_header)
    repo = ""
    # Project first - has full path_with_namespace
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
    log.info(f"DEBUG normalize: repo={repo}, payload_project={list(payload.get('project',{}).keys())[:5] if isinstance(payload.get('project'),dict) else 'no project'}")
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
    return {"source":"gitcode","type":evt_type,"repository":repo,"branch":branch,"actor":actor,"payload":payload}

_recent_events = {}
_DEDUP_WINDOW = 120  # seconds
_repo_locks = {}
_repo_locks_lock = threading.Lock()

_MAX_WORKERS = 10
_MAX_QUEUE = 5
_event_executor = None
_task_slot = threading.BoundedSemaphore(_MAX_QUEUE)


def _run_with_slot(fn, *args, **kwargs):
    """执行任务，完成后释放槽位"""
    try:
        fn(*args, **kwargs)
    finally:
        _task_slot.release()


def _init_executor():
    """初始化全局线程池"""
    global _event_executor
    if _event_executor is None:
        _event_executor = concurrent.futures.ThreadPoolExecutor(
            max_workers=_MAX_WORKERS,
            thread_name_prefix="evt"
        )


def _submit_event(fn, *args, **kwargs):
    """提交事件，等待+运行最多 5 个，超出的丢弃"""
    if not _task_slot.acquire(blocking=False):
        log.error(f"Task queue full (max={_MAX_QUEUE})! Dropping event")
        return
    _init_executor()
    _event_executor.submit(_run_with_slot, fn, *args, **kwargs)


def _acquire_repo_lock(repo_key: str) -> threading.Lock:
    """获取 per-repo 处理锁，同 repo 的事件串行处理"""
    with _repo_locks_lock:
        if repo_key not in _repo_locks:
            _repo_locks[repo_key] = threading.Lock()
        return _repo_locks[repo_key]


def _is_duplicate_event(evt) -> bool:
    """检查短时间内的重复事件（Issue Hook + Note Hook 同时到达导致双评论）"""
    number = extract_event_number(evt['payload'])
    if not number:
        return False
    key = f"{evt['repository']}:{number}:{evt['type']}"
    now = time.time()
    if key in _recent_events:
        if now - _recent_events[key] < _DEDUP_WINDOW:
            log.info(f"Duplicate event skipped: {key}")
            return True
    # Also check if same issue was already processed under a different event type
    for other_key, ts in list(_recent_events.items()):
        if now - ts > _DEDUP_WINDOW:
            continue
        other_repo, other_num, _ = other_key.split(":", 2)
        if other_repo == evt['repository'] and other_num == str(number):
            log.info(f"Duplicate issue #{number} skipped (already processed as {other_key})")
            return True
    _recent_events[key] = now
    return False


def process_event(raw_body, headers):
    global evt
    try:
        event_header = headers.get("X-GitCode-Event", "unknown")
        evt = normalize_event(raw_body, event_header)
        repo_key = f"{evt['source']}:{evt['repository']}"
        log.info(f"═══ Event: {evt['type']} on {evt['repository']} by {evt['actor']} ═══")

        # 防自循环：Bot 自己的评论跳过
        if evt['type'] == 'issue_comment':
            note = evt['payload'].get('object_attributes', {}).get('note', '')
            user = evt['payload'].get('user', {})
            username = user.get('username', '') if isinstance(user, dict) else ''
            if note.startswith('[DevOps Bot]') and username in ('hwc-skills-bot', 'skill-bot'):
                log.info(f"Skip: self-comment from {username}")
                return

        # 重复事件去重（同一 Issue 短时间内只处理一次）
        if _is_duplicate_event(evt):
            return

        # per-repo 串行锁：同一仓库的事件排队处理
        lock = _acquire_repo_lock(repo_key)
        lock.acquire()
        try:
            _process_event_inner(evt, repo_key)
        finally:
            lock.release()

    except Exception as e:
        import traceback
        log.error(f"Error processing event: {e}")
        traceback.print_exc()


def _process_event_inner(evt, repo_key):
    """事件处理主体（受 per-repo 锁保护，同 repo 串行）"""
    try:
        ws = os.path.join(WORKSPACE_DIR, evt['repository'].replace("/", "\\"))
        os.makedirs(ws, exist_ok=True)

        sid, history, oc_session_id, is_new = get_or_create_session(repo_key, evt['source'], evt['repository'])
        log.info(f"Session: {sid} (new={is_new}, history={len(history)} events, oc_session={oc_session_id[:30] if oc_session_id else 'none'})")

        skills = ["devops-event-ops"]
        log.info(f"Skills: {skills}")

        prompt = build_prompt(evt, history, oc_session_id, skills)
        log.info(f"Prompt built ({len(prompt)} chars)")

        # === Stream opencode NDJSON ===
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

        # === Record result ===
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

        # Bot posts comment (skip if Agent said SKIP)
        output = final_text.strip()
        if output == "SKIP":
            log.info("Agent decided to SKIP (no response needed)")
        elif output:
            number = extract_event_number(evt['payload'])
            repo = evt['repository']
            if number and repo and 0 < number < 100000:
                # issue_comment 追加引用原文
                body = output[:1500]
                if evt['type'] == 'issue_comment':
                    note = evt['payload'].get('object_attributes', {}).get('note', '')
                    if note:
                        body = f'> {note[:200]}\n\n{body}'
                log.info(f"Posting comment to #{number} on {repo}...")
                try:
                    post_comment_to_gitcode(repo, number, body, evt['type'])
                    log.info(f"✅ Comment posted")
                except Exception as e:
                    log.error(f"Failed to post comment: {e}")
            else:
                log.info(f"No comment posted (number={number}, repo={repo})")

    except Exception as e:
        import traceback
        log.error(f"Error processing event: {e}")
        traceback.print_exc()

def post_comment_to_gitcode(repo, number, body, event_type):
    if not GITCODE_TOKEN:
        log.warning("No GITCODE_TOKEN, cannot post comment")
        return
    if "issue" in event_type.lower():
        url = f"https://api.gitcode.com/api/v5/repos/{repo}/issues/{number}/comments"
    else:
        url = f"https://api.gitcode.com/api/v5/repos/{repo}/pulls/{number}/comments"
    log.info(f"DEBUG post_comment: url={url}, event_type={event_type}, repo={repo}, number={number}, body_len={len(body)}")
    payload = json.dumps({"body": f"[DevOps Bot] {body}"}).encode()
    req = Request(url, data=payload, headers={
        "Authorization": f"Bearer {GITCODE_TOKEN}",
        "Content-Type": "application/json"
    })
    try:
        resp = urlopen(req, timeout=15)
        log.info(f"Comment posted: HTTP {resp.status}")
    except URLError as e:
        if hasattr(e, 'code') and e.code:
            err_body = e.read().decode()[:200] if hasattr(e, 'read') else ""
            raise Exception(f"API {e.code}: {err_body}")
        raise


# ========== Feishu / IM Chat Processing ==========

def build_chat_prompt(user_text: str, repo_key: str, history: list, skills: list = None) -> str:
    """Build prompt for IM chat (Feishu/Dingtalk/Slack) queries"""
    parts = []
    parts.append(f"User asks about {repo_key}: {user_text}")
    if history:
        parts.append(f"Session history ({len(history)} events):")
        for h in history[-8:]:
            status = "ok" if h.get("success") else "fail"
            t = h.get("type", "")
            parts.append(f"  [{status}] {t}")
    else:
        parts.append("No events yet for this repository.")
    if skills:
        parts.append(f"Skills: {', '.join(skills)}")
        parts.append(f"Respond based on the {skills[0]} skill.")
    parts.append(build_role_definition())
    return "\n".join(parts)


def process_chat(parsed: dict):
    """Handle Feishu chat message from WebSocket"""
    try:
        if parsed.get("type") != "message":
            return

        text = parsed["text"]
        chat_id = parsed["chat_id"]
        msg_id = parsed["message_id"]

        log.info(f"Feishu message: {text[:100]} (chat={chat_id})")

        # 能力询问检测：Bot 层直接回复，不送 Agent
        capability_patterns = [
            r'(?:你|您)(?:有|会|能|可以)?\s*什么\s*(?:能力|功能|本领|技能|用处|作用)',
            r'(?:你|您)(?:会|能|可以)\s*(?:做|干|处理|干嘛)\s*(?:什么|哪些)',
            r'what\s*(?:can|are)\s*(?:you|your)\s*(?:do|capabilities|skills|features)',
            r'(?:介绍|说说|告诉)(?:一下|下)?\s*(?:你|你自己)',
        ]
        is_capability_inquiry = any(re.search(p, text, re.IGNORECASE) for p in capability_patterns)
        if is_capability_inquiry:
            reply_text(msg_id, get_skill_capabilities())
            return

        # Handle commands
        if text.startswith("/"):
            result = process_command(text, chat_id, parsed.get("sender_id", ""), db)
            if result:
                reply_text(msg_id, result)
            return

        # Resolve repo
        repo_key = resolve_repo(text, chat_id, parsed.get("sender_id", ""), db)
        if not repo_key:
            # 查 sessions 表获取仓库列表
            reply_parts = ["当前未绑定任何仓库至本群。"]
            try:
                c = db.cursor()
                c.execute("SELECT DISTINCT repo_key, repository, source FROM sessions ORDER BY last_active_at DESC LIMIT 10")
                rows = c.fetchall()
                if rows:
                    reply_parts.append("已有处理的仓库：")
                    for r in rows:
                        reply_parts.append(f"  📦 {r[1]}")
                    reply_parts.append("")
                    reply_parts.append("绑定方式（任选一种）：")
                    reply_parts.append(f'  "绑定到 {rows[0][1]}"')
                    reply_parts.append(f'  "使用 {rows[0][1]}"')
                    reply_parts.append(f"  /bind {rows[0][1]}")
                else:
                    reply_parts.append("尚无任何仓库的 Session 记录。")
                    reply_parts.append("请先在 GitCode 上操作仓库触发事件，")
                    reply_parts.append('或发送：绑定到 owner/repo_name')
            except Exception as e:
                log.warning(f"Feishu: query sessions error: {e}")
                reply_parts.append("请发送：绑定到 owner/repo_name")
            reply_text(msg_id, "\n".join(reply_parts))
            return

        # 自然语言绑定：如果 detect_bind_intent 匹配了，执行绑定
        bind_repo = detect_bind_intent(text)
        if bind_repo:
            result = process_command(f"/bind {bind_repo}", chat_id, parsed.get("sender_id", ""), db)
            reply_text(msg_id, result)
            return

        source, repository = (repo_key.split(":", 1) + ["", ""])[:2]
        if not source or not repository:
            if "/" in repo_key:
                source = "gitcode"
                repository = repo_key
                repo_key = f"gitcode:{repo_key}"

        sess, history, oc_sid, is_new = get_or_create_session(repo_key, source, repository)

        # per-repo 串行：与 GitCode 事件共享队列
        lock = _acquire_repo_lock(repo_key)
        lock.acquire()
        try:
            _process_chat_inner(parsed, text, repo_key, repository, msg_id, sess, history, oc_sid, is_new)
        finally:
            lock.release()

    except Exception as e:
        import traceback
        log.error(f"Feishu process error: {e}")
        traceback.print_exc()
        try:
            local_vars = locals()
            if 'msg_id' in local_vars and local_vars['msg_id']:
                reply_text(local_vars['msg_id'], "❌ 处理消息时出错，请稍后重试")
        except:
            pass


def _process_chat_inner(parsed, text, repo_key, repository, msg_id, sess, history, oc_sid, is_new):
    """飞书消息处理主体（受 per-repo 锁保护）"""
    try:
        prompt = build_chat_prompt(text, repo_key, history, ["chat"])
        log.info(f"Chat prompt ({len(prompt)} chars): {prompt[:200]}")

        wsd = os.path.join(WORKSPACE_DIR, repository.replace("/", "\\"))
        os.makedirs(wsd, exist_ok=True)

        reply_text(msg_id, "🤔 正在查询，请稍候...")

        final_text = ""
        new_oc_session_id = oc_sid
        for event_type, data in stream_opencode(prompt, wsd, oc_sid, "chat"):
            if event_type == "done":
                d = data
                final_text = d["text"]
                new_oc_session_id = d["oc_session_id"] or new_oc_session_id

        record = {
            "type": "chat", "time": datetime.now().isoformat(),
            "success": True, "actor": parsed.get("sender_id", ""),
            "stdout": final_text[:2000],
            "oc_session_id": new_oc_session_id
        }
        history.append(record)
        update_session(sess, history, "idle", new_oc_session_id)

        output = final_text.strip()
        if output == "SKIP":
            log.info("Agent decided to SKIP (no reply needed)")
        elif output:
            reply_text(msg_id, output[:2000])

    except Exception as e:
        import traceback
        log.error(f"Feishu process error: {e}")
        traceback.print_exc()
        try:
            local_vars = locals()
            if 'msg_id' in local_vars and local_vars['msg_id']:
                reply_text(local_vars['msg_id'], "❌ 处理消息时出错，请稍后重试")
        except:
            pass


server = HTTPServer(("", 8080), Handler)
server.socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
log.info("═══ DevOps Bot v2 (NDJSON streaming) starting on :8080 ═══")
log.info(f"Skills dir: {SKILLS_DIR}")
log.info(f"Workspace dir: {WORKSPACE_DIR}")
log.info(f"GitCode token: {'YES' if GITCODE_TOKEN else 'NO'}")
log.info(f"Feishu bot: {'CONFIGURED' if FEISHU_APP_ID else 'NOT CONFIGURED (set FEISHU_APP_ID)'}")
log.info(f"Routes: /webhook/gitcode /health /api/sessions")

# Start Feishu WebSocket client (long-connection, no tunnel needed)
if FEISHU_APP_ID:
    threading.Thread(target=start_ws, args=(process_chat,), daemon=True, name="feishu-ws").start()

# Also run HTTP server for GitCode webhooks
try:
    server.serve_forever()
except KeyboardInterrupt:
    stop_ws()
    server.server_close()
