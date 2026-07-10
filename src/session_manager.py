"""Session 管理：DB 初始化、Session CRUD、飞书绑定"""
import sqlite3, json, threading, os, time
from datetime import datetime
from src.config import log

FEISHU_BINDINGS_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS feishu_bindings (
    chat_id TEXT PRIMARY KEY,
    repo_key TEXT NOT NULL,
    source TEXT DEFAULT 'gitcode',
    bind_by TEXT DEFAULT '',
    created_at TEXT DEFAULT (datetime('now'))
)
"""

_db_path = os.path.join(os.path.dirname(__file__), "..", "data/sessions.db")
db_lock = threading.Lock()

db = sqlite3.connect(_db_path, check_same_thread=False)
db.execute("""
    CREATE TABLE IF NOT EXISTS sessions (
        id TEXT PRIMARY KEY,
        repo_key TEXT UNIQUE,
        source TEXT, repository TEXT, status TEXT,
        event_history TEXT DEFAULT '[]',
        oc_session_id TEXT DEFAULT '',
        created_at TEXT, last_active_at TEXT
    )
""")
db.execute(FEISHU_BINDINGS_TABLE_SQL)
db.commit()


def get_or_create_session(repo_key, source, repository):
    with db_lock:
        c = db.cursor()
        c.execute("SELECT id, event_history, oc_session_id, status FROM sessions WHERE repo_key=?", (repo_key,))
        row = c.fetchone()
        if row:
            sid, hist_json, oc_sid, status = row
            history = json.loads(hist_json) if hist_json else []
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
