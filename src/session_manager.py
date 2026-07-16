"""Session 管理：DB 初始化、Session CRUD、飞书绑定、Feishu 用户映射"""
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

FEISHU_USERS_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS feishu_users (
    repo_key TEXT NOT NULL,
    gitcode_user TEXT NOT NULL,
    feishu_open_id TEXT NOT NULL,
    feishu_name TEXT DEFAULT '',
    updated_at TEXT DEFAULT (datetime('now')),
    PRIMARY KEY (repo_key, gitcode_user)
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
db.execute(FEISHU_USERS_TABLE_SQL)
try:
    db.execute("ALTER TABLE feishu_bindings ADD COLUMN auto_impl_enabled INTEGER DEFAULT 0")
except:
    pass
try:
    db.execute("ALTER TABLE feishu_bindings ADD COLUMN notify_enabled INTEGER DEFAULT 0")
except:
    pass
try:
    db.execute("ALTER TABLE sessions ADD COLUMN account TEXT DEFAULT 'default'")
except:
    pass
try:
    db.execute("ALTER TABLE feishu_bindings ADD COLUMN account TEXT DEFAULT 'default'")
except:
    pass
try:
    db.execute("ALTER TABLE sessions ADD COLUMN backend TEXT DEFAULT ''")
except:
    pass
try:
    db.execute("ALTER TABLE sessions ADD COLUMN opencode_session_id TEXT DEFAULT ''")
except:
    pass
try:
    db.execute("ALTER TABLE sessions ADD COLUMN codearts_session_id TEXT DEFAULT ''")
except:
    pass
db.execute("UPDATE feishu_bindings SET notify_enabled=0 WHERE notify_enabled IS NULL")
db.commit()

MAX_HISTORY = 100

_SESSION_COLS = {"opencode": "opencode_session_id", "codearts": "codearts_session_id"}


def _session_col(backend: str) -> str:
    return _SESSION_COLS.get(backend, "opencode_session_id")


def get_or_create_session(repo_key, source, repository, backend="opencode"):
    with db_lock:
        c = db.cursor()
        c.execute("SELECT id, event_history, opencode_session_id, codearts_session_id, status FROM sessions WHERE repo_key=?", (repo_key,))
        row = c.fetchone()
        if row:
            sid, hist_json, oc_sid, cc_sid, status = row
            history = json.loads(hist_json) if hist_json else []
            if status == "failed":
                c.execute("UPDATE sessions SET status=? WHERE id=?", ("idle", sid))
                db.commit()
            sid_val = {"opencode": oc_sid, "codearts": cc_sid}.get(backend, "")
            return sid, history, sid_val or "", "", False
        sid = f"sess-{repo_key}-{int(time.time()*1000000)}"
        now = datetime.now().isoformat()
        c.execute(
            "INSERT INTO sessions (id, repo_key, source, repository, status, event_history, created_at, last_active_at) VALUES (?,?,?,?,?,?,?,?)",
            (sid, repo_key, source, repository, "idle", "[]", now, now)
        )
        db.commit()
        return sid, [], "", "", True


def update_session(sid, history, status, oc_session_id=None, backend="opencode"):
    with db_lock:
        c = db.cursor()
        col = _session_col(backend)
        if len(history) > MAX_HISTORY:
            history = history[-MAX_HISTORY:]
        if oc_session_id:
            c.execute(
                f"UPDATE sessions SET event_history=?, status=?, {col}=?, last_active_at=? WHERE id=?",
                (json.dumps(history), status, oc_session_id, datetime.now().isoformat(), sid)
            )
        else:
            c.execute(
                "UPDATE sessions SET event_history=?, status=?, last_active_at=? WHERE id=?",
                (json.dumps(history), status, datetime.now().isoformat(), sid)
            )
        db.commit()


def dump_history_to_file(repo_key: str, history: list, account: str = "default"):
    """将 event_history 写入 workspace 目录（按 account 分目录，每文件最多 10 条）"""
    from src.config import WORKSPACE_DIR
    clean = []
    for r in history:
        entry = {k: v for k, v in r.items() if k != "oc_session_id"}
        clean.append(entry)
    repo_name = repo_key.replace("gitcode:", "").replace("/", "_")
    hist_dir = os.path.join(WORKSPACE_DIR, repo_name, "history", account)
    try:
        os.makedirs(hist_dir, exist_ok=True)
        # Write most recent 10 to base file, older chunks to numbered files
        remaining = clean[:]
        chunk_idx = 0
        while remaining:
            chunk = remaining[-10:]
            remaining = remaining[:-10]
            fname = "session_history.json" if chunk_idx == 0 else f"session_history_{chunk_idx}.json"
            with open(os.path.join(hist_dir, fname), "w", encoding="utf-8") as f:
                json.dump(chunk, f, indent=2, ensure_ascii=False)
            chunk_idx += 1
        # Clean up stale numbered files beyond what we just wrote
        for fname in os.listdir(hist_dir):
            if fname.startswith("session_history_") and fname.endswith(".json"):
                idx = fname.replace("session_history_", "").replace(".json", "")
                if idx.isdigit() and int(idx) >= chunk_idx:
                    os.remove(os.path.join(hist_dir, fname))
    except Exception as e:
        log.warning(f"Failed to dump history: {e}")


def get_history_file_list(repo_key: str, account: str = "default") -> list[str]:
    """返回可用的历史文件路径列表（最新在前）"""
    from src.config import WORKSPACE_DIR
    repo_name = repo_key.replace("gitcode:", "").replace("/", "_")
    hist_dir = os.path.join(WORKSPACE_DIR, repo_name, "history", account)
    files = []
    try:
        if os.path.isdir(hist_dir):
            for f in sorted(os.listdir(hist_dir), reverse=True):
                if f.endswith(".json"):
                    files.append(os.path.join(hist_dir, f))
    except:
        pass
    return files or []


def get_feishu_open_id(repo_key, gitcode_user):
    """获取 GitCode 用户名对应的飞书 open_id"""
    with db_lock:
        c = db.cursor()
        c.execute("SELECT feishu_open_id FROM feishu_users WHERE repo_key=? AND gitcode_user=?", (repo_key, gitcode_user))
        row = c.fetchone()
        return row[0] if row else None


def set_feishu_open_id(repo_key, gitcode_user, open_id, feishu_name=""):
    """写入 GitCode → Feishu 用户映射"""
    with db_lock:
        c = db.cursor()
        c.execute(
            "INSERT OR REPLACE INTO feishu_users (repo_key, gitcode_user, feishu_open_id, feishu_name, updated_at) VALUES (?,?,?,?,?)",
            (repo_key, gitcode_user, open_id, feishu_name, datetime.now().isoformat())
        )
        db.commit()


def get_feishu_mapping(repo_key):
    """获取某 repo 的所有已知映射 {gitcode_user: {open_id, name}}"""
    with db_lock:
        c = db.cursor()
        c.execute("SELECT gitcode_user, feishu_open_id, feishu_name FROM feishu_users WHERE repo_key=?", (repo_key,))
        return {row[0]: {"open_id": row[1], "name": row[2]} for row in c.fetchall()}


def get_feishu_group_name_map(repo_key):
    """获取 repo 的飞书群成员 {name: open_id}（从 feishu_users 聚合得到）"""
    with db_lock:
        c = db.cursor()
        c.execute("SELECT feishu_name, feishu_open_id FROM feishu_users WHERE repo_key=? AND feishu_name!=''", (repo_key,))
        return {row[0]: row[1] for row in c.fetchall()}


def get_auto_impl_enabled(repo_key):
    """查询仓库是否开启了自动实现"""
    with db_lock:
        c = db.cursor()
        c.execute("SELECT auto_impl_enabled FROM feishu_bindings WHERE repo_key=?", (repo_key,))
        row = c.fetchone()
        if not row:
            c.execute("SELECT auto_impl_enabled FROM feishu_bindings WHERE repo_key=?", ("gitcode:" + repo_key,))
            row = c.fetchone()
        return bool(row[0]) if row else False


def set_auto_impl_enabled(repo_key, enabled):
    """设置仓库的自动实现开关"""
    with db_lock:
        c = db.cursor()
        c.execute("UPDATE feishu_bindings SET auto_impl_enabled=? WHERE repo_key=?", (int(enabled), repo_key))
        if c.rowcount == 0:
            full_key = "gitcode:" + repo_key if not repo_key.startswith("gitcode:") else repo_key
            c.execute("UPDATE feishu_bindings SET auto_impl_enabled=? WHERE repo_key=?", (int(enabled), full_key))
        db.commit()


def get_notify_enabled(repo_key):
    """查询仓库是否开启了飞书通知"""
    with db_lock:
        c = db.cursor()
        c.execute("SELECT notify_enabled FROM feishu_bindings WHERE repo_key=?", (repo_key,))
        row = c.fetchone()
        if not row:
            c.execute("SELECT notify_enabled FROM feishu_bindings WHERE repo_key=?", ("gitcode:" + repo_key,))
            row = c.fetchone()
        # default to 0 (False) if column is NULL
        return bool(row[0]) if row and row[0] is not None else False


def set_notify_enabled(repo_key, enabled):
    """设置仓库的飞书通知开关"""
    with db_lock:
        c = db.cursor()
        c.execute("UPDATE feishu_bindings SET notify_enabled=? WHERE repo_key=?", (int(enabled), repo_key))
        if c.rowcount == 0:
            full_key = "gitcode:" + repo_key if not repo_key.startswith("gitcode:") else repo_key
            c.execute("UPDATE feishu_bindings SET notify_enabled=? WHERE repo_key=?", (int(enabled), full_key))
        db.commit()
