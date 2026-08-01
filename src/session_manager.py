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


def dump_history_to_file(repo_key: str, history: list, account: str = ""):
    """将 event_history 写入 workspace 目录（按 account 分目录，每文件最多 10 条）"""
    from src.config import WORKSPACE_DIR
    clean = []
    for r in history:
        entry = {k: v for k, v in r.items() if k != "oc_session_id"}
        clean.append(entry)
    repo_name = repo_key.replace("gitcode:", "").replace("/", "_")
    hist_dir = os.path.join(WORKSPACE_DIR, "history", account, repo_name)
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


def get_history_file_list(repo_key: str, account: str = "") -> list[str]:
    """返回可用的历史文件路径列表（最新在前）"""
    from src.config import WORKSPACE_DIR
    repo_name = repo_key.replace("gitcode:", "").replace("/", "_")
    hist_dir = os.path.join(WORKSPACE_DIR, "history", account, repo_name)
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
    """写入 GitCode → Feishu 用户映射（含 user_id）"""
    user_id = 0
    try:
        from src.user import db as user_db
        acc = user_db.get_user_account(gitcode_user)
        if acc:
            user_id = acc.get("id", 0)
    except Exception:
        pass
    with db_lock:
        c = db.cursor()
        c.execute(
            "INSERT OR REPLACE INTO feishu_users (repo_key, gitcode_user, feishu_open_id, feishu_name, user_id, updated_at) VALUES (?,?,?,?,?,?)",
            (repo_key, gitcode_user, open_id, feishu_name, user_id, datetime.now().isoformat())
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


# ====== im_channels CRUD ======

def create_im_channel(provider: str, channel_id: str, repo_key: str,
                      channel_name: str = "", bind_by: str = "",
                      account: str = "") -> str | None:
    import uuid
    cid = str(uuid.uuid4())
    try:
        with db_lock:
            c = db.cursor()
            c.execute(
                "INSERT OR IGNORE INTO im_channels (id, provider, channel_id, channel_name, repo_key, bind_by, account) VALUES (?,?,?,?,?,?,?)",
                (cid, provider, channel_id, channel_name, repo_key, bind_by, account)
            )
            db.commit()
            return cid
    except Exception as e:
        log.warning(f"Create im_channel failed: {e}")
        return None


def get_channel_by_repo(repo_key: str, provider: str = "feishu") -> dict | None:
    with db_lock:
        c = db.cursor()
        c.execute("SELECT id, provider, channel_id, channel_name, repo_key, auto_impl_enabled, notify_enabled, account, created_at FROM im_channels WHERE repo_key=? AND provider=?", (repo_key, provider))
        row = c.fetchone()
        if not row:
            c.execute("SELECT id, provider, channel_id, channel_name, repo_key, auto_impl_enabled, notify_enabled, account, created_at FROM im_channels WHERE repo_key=? AND provider=?", ("gitcode:" + repo_key, provider))
            row = c.fetchone()
        if not row:
            return None
        return {"id": row[0], "provider": row[1], "channel_id": row[2], "channel_name": row[3],
                "repo_key": row[4], "auto_impl_enabled": bool(row[5]), "notify_enabled": bool(row[6]),
                "account": row[7], "created_at": row[8]}


def get_channel_by_channel_id(provider: str, channel_id: str) -> dict | None:
    with db_lock:
        c = db.cursor()
        c.execute("SELECT id, provider, channel_id, channel_name, repo_key, auto_impl_enabled, notify_enabled, account, created_at FROM im_channels WHERE provider=? AND channel_id=?", (provider, channel_id))
        row = c.fetchone()
        if not row:
            return None
        return {"id": row[0], "provider": row[1], "channel_id": row[2], "channel_name": row[3],
                "repo_key": row[4], "auto_impl_enabled": bool(row[5]), "notify_enabled": bool(row[6]),
                "account": row[7], "created_at": row[8]}


def update_im_channel(repo_key: str, provider: str, **kwargs):
    allowed = {"auto_impl_enabled", "notify_enabled", "channel_name"}
    sets = []
    vals = []
    for k, v in kwargs.items():
        if k in allowed:
            sets.append(f"{k}=?")
            vals.append(int(v) if isinstance(v, bool) else v)
    if not sets:
        return
    vals += [repo_key, provider]
    with db_lock:
        c = db.cursor()
        c.execute(f"UPDATE im_channels SET {', '.join(sets)} WHERE repo_key=? AND provider=?", vals)
        if c.rowcount == 0:
            full_key = "gitcode:" + repo_key if not repo_key.startswith("gitcode:") else repo_key
            vals[-2] = full_key
            c.execute(f"UPDATE im_channels SET {', '.join(sets)} WHERE repo_key=? AND provider=?", vals)
        db.commit()


def delete_im_channel(provider: str, channel_id: str):
    with db_lock:
        c = db.cursor()
        c.execute("DELETE FROM im_channels WHERE provider=? AND channel_id=?", (provider, channel_id))
        db.commit()


# ====== user_im_bindings CRUD ======

def bind_user_im(user_id: int, provider: str, channel_id: str, channel_type: str = "direct_message") -> bool:
    try:
        with db_lock:
            c = db.cursor()
            c.execute("INSERT OR REPLACE INTO user_im_bindings (user_id, provider, channel_type, channel_id) VALUES (?,?,?,?)",
                      (user_id, provider, channel_type, channel_id))
            db.commit()
            return True
    except Exception as e:
        log.warning(f"Bind user IM failed: {e}")
        return False


def get_user_im_channel(user_id: int, provider: str = "feishu", channel_type: str = "direct_message") -> dict | None:
    with db_lock:
        c = db.cursor()
        c.execute("SELECT id, user_id, provider, channel_type, channel_id, enabled, created_at FROM user_im_bindings WHERE user_id=? AND provider=? AND channel_type=?", (user_id, provider, channel_type))
        row = c.fetchone()
        if not row:
            return None
        return {"id": row[0], "user_id": row[1], "provider": row[2], "channel_type": row[3],
                "channel_id": row[4], "enabled": bool(row[5]), "created_at": row[6]}


def unbind_user_im(user_id: int, provider: str = "feishu"):
    with db_lock:
        c = db.cursor()
        c.execute("DELETE FROM user_im_bindings WHERE user_id=? AND provider=?", (user_id, provider))
        db.commit()


# ====== chat_conversations CRUD ======

def create_conversation(user_id: int, repo_key: str = "", im_channel_id: str = "", title: str = "",
                        user_gitcode_token: str = "") -> str:
    import uuid
    cid = str(uuid.uuid4())
    session_id = ""
    with db_lock:
        c = db.cursor()
        c.execute("INSERT INTO chat_conversations (id, user_id, session_id, repo_key, im_channel_id, title, user_gitcode_token) VALUES (?,?,?,?,?,?,?)",
                  (cid, user_id, session_id, repo_key, im_channel_id, title, user_gitcode_token))
        db.commit()
    return cid


def get_user_conversations(user_id: int, repo_key: str = "", limit: int = 20) -> list[dict]:
    with db_lock:
        c = db.cursor()
        if repo_key:
            c.execute("SELECT id, user_id, session_id, user_gitcode_token, repo_key, im_channel_id, title, status, created_at, updated_at FROM chat_conversations WHERE user_id=? AND repo_key=? ORDER BY updated_at DESC LIMIT ?", (user_id, repo_key, limit))
        else:
            c.execute("SELECT id, user_id, session_id, user_gitcode_token, repo_key, im_channel_id, title, status, created_at, updated_at FROM chat_conversations WHERE user_id=? ORDER BY updated_at DESC LIMIT ?", (user_id, limit))
        return [{"id": r[0], "user_id": r[1], "session_id": r[2] or "", "user_gitcode_token": r[3] or "", "repo_key": r[4], "im_channel_id": r[5], "title": r[6], "status": r[7], "created_at": r[8], "updated_at": r[9]} for r in c.fetchall()]


def get_conversation(conv_id: str) -> dict | None:
    with db_lock:
        c = db.cursor()
        c.execute("SELECT id, user_id, session_id, user_gitcode_token, repo_key, im_channel_id, title, status, created_at, updated_at FROM chat_conversations WHERE id=?", (conv_id,))
        row = c.fetchone()
        if not row:
            return None
        return {"id": row[0], "user_id": row[1], "session_id": row[2] or "", "user_gitcode_token": row[3] or "", "repo_key": row[4], "im_channel_id": row[5],
                "title": row[6], "status": row[7], "created_at": row[8], "updated_at": row[9]}


def update_conversation(conv_id: str, **kwargs):
    allowed = {"title", "status", "session_id", "user_gitcode_token"}
    sets = []
    vals = []
    for k, v in kwargs.items():
        if k in allowed:
            sets.append(f"{k}=?")
            vals.append(v)
    if not sets:
        return
    vals.append(conv_id)
    with db_lock:
        c = db.cursor()
        c.execute(f"UPDATE chat_conversations SET {', '.join(sets)}, updated_at=datetime('now') WHERE id=?", vals)
        db.commit()


def touch_conversation(conv_id: str):
    with db_lock:
        c = db.cursor()
        c.execute("UPDATE chat_conversations SET updated_at=datetime('now') WHERE id=?", (conv_id,))
        db.commit()


# ====== chat_messages CRUD ======

def add_message(conversation_id: str, role: str, content: str) -> int:
    with db_lock:
        c = db.cursor()
        c.execute("INSERT INTO chat_messages (conversation_id, role, content) VALUES (?,?,?)",
                  (conversation_id, role, content))
        db.commit()
        return c.lastrowid


def get_messages(conversation_id: str, limit: int = 50, before_id: int = 0) -> list[dict]:
    with db_lock:
        c = db.cursor()
        if before_id > 0:
            c.execute("SELECT id, conversation_id, role, content, created_at FROM chat_messages WHERE conversation_id=? AND id<? ORDER BY id DESC LIMIT ?",
                      (conversation_id, before_id, limit))
        else:
            c.execute("SELECT id, conversation_id, role, content, created_at FROM chat_messages WHERE conversation_id=? ORDER BY id DESC LIMIT ?",
                      (conversation_id, limit))
        rows = c.fetchall()
        rows.reverse()
        return [{"id": r[0], "conversation_id": r[1], "role": r[2], "content": r[3], "created_at": r[4]} for r in rows]


def delete_messages_by_conv(conversation_id: str):
    with db_lock:
        c = db.cursor()
        c.execute("DELETE FROM chat_messages WHERE conversation_id=?", (conversation_id,))
        db.commit()


def delete_conversation(conv_id: str):
    with db_lock:
        c = db.cursor()
        c.execute("DELETE FROM chat_conversations WHERE id=?", (conv_id,))
        db.commit()


# ====== chat_config CRUD ======

def get_chat_config(user_id: int) -> dict:
    with db_lock:
        c = db.cursor()
        c.execute("SELECT backend, skill, feishu_app_id, feishu_app_secret, updated_at FROM chat_config WHERE user_id=?", (user_id,))
        row = c.fetchone()
        if not row:
            return {"backend": "opencode", "skill": "", "feishu_app_id": "", "feishu_app_secret": "", "updated_at": ""}
        return {"backend": row[0] or "opencode", "skill": row[1] or "", "feishu_app_id": row[2] or "", "feishu_app_secret": row[3] or "", "updated_at": row[4] or ""}


def set_chat_config(user_id: int, **kwargs):
    allowed = {"backend", "skill", "feishu_app_id", "feishu_app_secret"}
    data = get_chat_config(user_id)
    for k, v in kwargs.items():
        if k in allowed and v is not None:
            data[k] = v
    with db_lock:
        c = db.cursor()
        c.execute(
            "INSERT OR REPLACE INTO chat_config (user_id, backend, skill, feishu_app_id, feishu_app_secret, updated_at) VALUES (?,?,?,?,?,datetime('now'))",
            (user_id, data["backend"], data["skill"], data["feishu_app_id"], data["feishu_app_secret"])
        )
        db.commit()
