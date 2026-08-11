"""Session 管理：DB 初始化、Session CRUD、飞书绑定、Feishu 用户映射"""
import sqlite3, json, threading, os, time, shutil
from datetime import datetime
from src.config import log

REPO_IM_GROUP_BINDING_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS repo_im_group_binding (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    im_provider TEXT DEFAULT 'feishu',
    im_chat_id TEXT NOT NULL,
    im_channel_name TEXT DEFAULT '',
    repo_key TEXT NOT NULL,
    bind_by TEXT DEFAULT '',
    im_app_id TEXT DEFAULT '',
    source TEXT DEFAULT '',
    auto_impl_enabled INTEGER DEFAULT 0,
    notify_enabled INTEGER DEFAULT 0,
    created_at TEXT DEFAULT (datetime('now')),
    UNIQUE (im_provider, im_chat_id, im_app_id)
)
"""

UNIFIED_USER_MAPPING_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS unified_user_mapping (
    user_id INTEGER PRIMARY KEY,
    gitcode_user TEXT DEFAULT '',
    gitcode_nickname TEXT DEFAULT '',
    feishu_open_id TEXT DEFAULT '',
    feishu_name TEXT DEFAULT '',
    github_login TEXT DEFAULT '',
    dingtalk_user_id TEXT DEFAULT '',
    updated_at TEXT DEFAULT (datetime('now'))
)
"""

_db_path = os.environ.get("DEVOPS_DB_PATH", os.path.join(os.path.dirname(__file__), "..", "data/sessions.db"))
db_lock = threading.RLock()


def _backup_db(path: str, tag: str = "") -> str:
    """迁移前备份 DB（仅在有实际数据且首次时），返回备份路径。失败不阻断（仅告警）。"""
    try:
        if not os.path.exists(path):
            return ""
        backup_dir = os.path.join(os.path.dirname(path), "backups")
        os.makedirs(backup_dir, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        name = f"{os.path.basename(path)}{('_' + tag) if tag else ''}.{stamp}.bak"
        dest = os.path.join(backup_dir, name)
        if not os.path.exists(dest):
            shutil.copy2(path, dest)
            log.info(f"DB backed up: {path} -> {dest}")
        return dest
    except Exception as e:
        log.warning(f"DB backup failed: {e}")
        return ""


db = sqlite3.connect(_db_path, check_same_thread=False)
# WAL + busy_timeout：跨进程写安全（双进程 3000/8080 共享 DB，见 concurrency-design.md §2.1）
try:
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("PRAGMA busy_timeout=5000")
    db.execute("PRAGMA synchronous=NORMAL")
    db.execute("PRAGMA foreign_keys=ON")
except Exception as e:
    log.warning(f"SQLite pragma setup failed: {e}")
# 迁移旧表 sessions → chat_agent_session（幂等，须先于 CREATE 执行）
try:
    _old_tables = [r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()]
    if "sessions" in _old_tables and "chat_agent_session" not in _old_tables:
        db.execute("ALTER TABLE sessions RENAME TO chat_agent_session")
        db.commit()
        log.info("Migrated table sessions -> chat_agent_session")
    elif "sessions" in _old_tables and "chat_agent_session" in _old_tables:
        # 新表已被创建但旧表仍存在（历史库），迁移数据后删除旧表
        db.execute("INSERT OR IGNORE INTO chat_agent_session (id, repo_key, source, repository, status, event_history, oc_session_id, created_at, last_active_at) SELECT id, repo_key, source, repository, status, event_history, oc_session_id, created_at, last_active_at FROM sessions")
        db.execute("DROP TABLE sessions")
        db.commit()
        log.info("Migrated sessions data into chat_agent_session and dropped sessions")
except Exception as e:
    log.warning(f"Migrate sessions table failed: {e}")
# 迁移旧表 feishu_bindings → repo_im_group_binding
try:
    _tables = [r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()]
    if "feishu_bindings" in _tables and "repo_im_group_binding" not in _tables:
        db.execute("ALTER TABLE feishu_bindings RENAME TO repo_im_group_binding")
        db.commit()
        log.info("Migrated table feishu_bindings -> repo_im_group_binding")
except Exception as e:
    log.warning(f"Migrate feishu_bindings table failed: {e}")
# 迁移旧表 feishu_users → unified_user_mapping（旧表废弃，直接删除）
try:
    _tables = [r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()]
    if "feishu_users" in _tables:
        db.execute("DROP TABLE feishu_users")
        db.commit()
        log.info("Dropped legacy table feishu_users")
except Exception as e:
    log.warning(f"Drop feishu_users table failed: {e}")
db.execute("""
    CREATE TABLE IF NOT EXISTS chat_agent_session (
        id TEXT PRIMARY KEY,
        user_id INTEGER DEFAULT 0,
        repo_key TEXT DEFAULT '',
        source TEXT, repository TEXT, status TEXT,
        runtime TEXT DEFAULT 'opencode',
        skill TEXT DEFAULT '',
        im_provider TEXT DEFAULT '',
        im_app_id TEXT DEFAULT '',
        im_app_secret TEXT DEFAULT '',
        event_history TEXT DEFAULT '[]',
        created_at TEXT, last_active_at TEXT
    )
""")
# 兼容列迁移（幂等，M0 术语重构：runtime_* 为新列名；老库升级时补齐，新库 DDL 已含）
try:
    _ccols = [r[1] for r in db.execute("PRAGMA table_info(chat_agent_session)").fetchall()]
    if "runtime_session_id" not in _ccols:
        db.execute("ALTER TABLE chat_agent_session ADD COLUMN runtime_session_id TEXT DEFAULT ''")
except Exception:
    pass
try:
    _ccols = [r[1] for r in db.execute("PRAGMA table_info(chat_agent_session)").fetchall()]
    if "runtime_id" not in _ccols:
        db.execute("ALTER TABLE chat_agent_session ADD COLUMN runtime_id INTEGER DEFAULT 0")
except Exception:
    pass
try:
    db.execute("ALTER TABLE chat_agent_session ADD COLUMN skill_id INTEGER DEFAULT 0")
except Exception:
    pass
db.execute(REPO_IM_GROUP_BINDING_TABLE_SQL)
# 彻底重建 unified_user_mapping：若表结构不匹配新 DDL（缺 gitcode_nickname/github_login/dingtalk_user_id）
# 则 DROP 后重建。当前表为空（历史遗留），直接 DROP 无数据丢失。
try:
    _um_cols = [r[1] for r in db.execute("PRAGMA table_info(unified_user_mapping)").fetchall()]
    if _um_cols and not {"gitcode_nickname", "github_login", "dingtalk_user_id"}.issubset(set(_um_cols)):
        db.execute("DROP TABLE unified_user_mapping")
        db.commit()
        log.info("Rebuilt unified_user_mapping (structure mismatch -> dropped & recreated)")
except Exception as e:
    log.warning(f"Rebuild unified_user_mapping check failed: {e}")
db.execute(UNIFIED_USER_MAPPING_TABLE_SQL)
# 兼容旧列迁移（幂等）
try:
    db.execute("ALTER TABLE repo_im_group_binding ADD COLUMN auto_impl_enabled INTEGER DEFAULT 0")
except:
    pass
try:
    db.execute("ALTER TABLE repo_im_group_binding ADD COLUMN notify_enabled INTEGER DEFAULT 0")
except:
    pass
try:
    db.execute("ALTER TABLE repo_im_group_binding ADD COLUMN im_app_id TEXT DEFAULT ''")
except:
    pass
try:
    db.execute("ALTER TABLE repo_im_group_binding ADD COLUMN im_provider TEXT DEFAULT 'feishu'")
except:
    pass
try:
    db.execute("ALTER TABLE repo_im_group_binding ADD COLUMN im_channel_name TEXT DEFAULT ''")
except:
    pass
try:
    db.execute("ALTER TABLE repo_im_group_binding ADD COLUMN source TEXT DEFAULT ''")
except:
    pass
try:
    _ccols = [r[1] for r in db.execute("PRAGMA table_info(chat_agent_session)").fetchall()]
    if "backend" not in _ccols and "runtime" not in _ccols:
        db.execute("ALTER TABLE chat_agent_session ADD COLUMN runtime TEXT DEFAULT 'opencode'")
except:
    pass
try:
    db.execute("ALTER TABLE chat_agent_session ADD COLUMN skill TEXT DEFAULT ''")
except:
    pass
try:
    db.execute("ALTER TABLE chat_agent_session ADD COLUMN im_provider TEXT DEFAULT ''")
except:
    pass
try:
    db.execute("ALTER TABLE chat_agent_session ADD COLUMN im_app_id TEXT DEFAULT ''")
except:
    pass
try:
    db.execute("ALTER TABLE chat_agent_session ADD COLUMN im_app_secret TEXT DEFAULT ''")
except:
    pass
try:
    db.execute("ALTER TABLE chat_agent_session ADD COLUMN user_id INTEGER DEFAULT 0")
except:
    pass
try:
    db.execute("ALTER TABLE chat_agent_session ADD COLUMN runtime_session_id TEXT DEFAULT ''")
except:
    pass
db.commit()

MAX_HISTORY = 100


def get_or_create_session(repo_key, source, repository, backend="opencode"):
    """获取或创建 repo 级 Chat 会话（chat_agent_session，飞书 Chat 用）"""
    from src.repo_key import to_repo_key
    repo_key = to_repo_key(repo_key)
    with db_lock:
        c = db.cursor()
        c.execute("SELECT id, event_history, runtime_session_id, status FROM chat_agent_session WHERE repo_key=?", (repo_key,))
        row = c.fetchone()
        if row:
            sid, hist_json, bsid, status = row
            history = json.loads(hist_json) if hist_json else []
            if status == "failed":
                c.execute("UPDATE chat_agent_session SET status=? WHERE id=?", ("idle", sid))
                db.commit()
            return sid, history, bsid or "", "", False
        sid = f"sess-{repo_key}-{int(time.time()*1000000)}"
        now = datetime.now().isoformat()
        c.execute(
            "INSERT INTO chat_agent_session (id, repo_key, source, repository, status, event_history, created_at, last_active_at) VALUES (?,?,?,?,?,?,?,?)",
            (sid, repo_key, source, repository, "idle", "[]", now, now)
        )
        db.commit()
        return sid, [], "", "", True


def update_session(sid, history, status, oc_session_id=None, backend="opencode"):
    """更新 Chat 会话（runtime_session_id 存当前 backend 会话）"""
    with db_lock:
        c = db.cursor()
        if len(history) > MAX_HISTORY:
            history = history[-MAX_HISTORY:]
        if oc_session_id:
            c.execute(
                "UPDATE chat_agent_session SET event_history=?, status=?, runtime_session_id=?, last_active_at=? WHERE id=?",
                (json.dumps(history), status, oc_session_id, datetime.now().isoformat(), sid)
            )
        else:
            c.execute(
                "UPDATE chat_agent_session SET event_history=?, status=?, last_active_at=? WHERE id=?",
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
    account_dir = account or "default"
    hist_dir = os.path.join(WORKSPACE_DIR, "history", account_dir, repo_name)
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
    account_dir = account or "default"
    hist_dir = os.path.join(WORKSPACE_DIR, "history", account_dir, repo_name)
    files = []
    try:
        if os.path.isdir(hist_dir):
            for f in sorted(os.listdir(hist_dir), reverse=True):
                if f.endswith(".json"):
                    files.append(os.path.join(hist_dir, f))
    except:
        pass
    return files or []


def get_feishu_open_id(gitcode_user):
    """获取 GitCode 用户名对应的飞书 open_id（按 gitcode_user 全局关联）"""
    with db_lock:
        c = db.cursor()
        c.execute("SELECT feishu_open_id FROM unified_user_mapping WHERE gitcode_user=?", (gitcode_user,))
        row = c.fetchone()
        return row[0] if row else None


def set_feishu_open_id(repo_key, gitcode_user, open_id, feishu_name="", gitcode_nickname=""):
    """写入 GitCode → Feishu 用户映射（user_id = user_accounts.id，按 gitcode_user 关联）。

    同步刷新 gitcode_nickname / feishu_open_id / feishu_name。
    gitcode_nickname 优先取传入值，否则从 user_accounts.nickname 获取。
    user_id 始终为真实 user_accounts.id：若该 gitcode_user 未在 user_accounts 中，
    则尝试按已有记录更新；两者都不可得时跳过（不写入负数占位）。
    """
    from src.user import db as user_db
    user_id = 0
    try:
        acc = user_db.get_user_account(gitcode_user)
        if acc:
            user_id = acc.get("id", 0)
            if not gitcode_nickname:
                gitcode_nickname = acc.get("nickname", "") or ""
    except Exception:
        pass

    with db_lock:
        c = db.cursor()
        if user_id:
            # 有真实 user_id：直接以 user_id 为主键写入/覆盖
            c.execute(
                "INSERT OR REPLACE INTO unified_user_mapping (user_id, gitcode_user, gitcode_nickname, feishu_open_id, feishu_name, updated_at) VALUES (?,?,?,?,?,?)",
                (user_id, gitcode_user, gitcode_nickname, open_id, feishu_name, datetime.now().isoformat())
            )
        else:
            # 无 user_id：尝试按 gitcode_user 更新已有记录（保留其 user_id）
            row = c.execute("SELECT user_id FROM unified_user_mapping WHERE gitcode_user=?", (gitcode_user,)).fetchone()
            if row:
                c.execute(
                    "UPDATE unified_user_mapping SET gitcode_nickname=?, feishu_open_id=?, feishu_name=?, updated_at=? WHERE user_id=?",
                    (gitcode_nickname, open_id, feishu_name, datetime.now().isoformat(), row[0])
                )
            else:
                # 无 user_id 也无已有记录：跳过（user_id 必须为真实 user_accounts.id）
                log.info(f"set_feishu_open_id: skip mapping for {gitcode_user} (no user_accounts record)")
                return False
        db.commit()
        return True


def get_feishu_mapping(repo_key):
    """获取所有已知映射 {gitcode_user: {open_id, name, gitcode_nickname}}"""
    with db_lock:
        c = db.cursor()
        c.execute("SELECT gitcode_user, feishu_open_id, feishu_name, gitcode_nickname FROM unified_user_mapping WHERE gitcode_user!=''")
        return {row[0]: {"open_id": row[1], "name": row[2], "gitcode_nickname": row[3] or ""} for row in c.fetchall()}


def get_feishu_group_name_map(repo_key):
    """获取飞书群成员 {name: open_id}（从 unified_user_mapping 聚合得到）"""
    with db_lock:
        c = db.cursor()
        c.execute("SELECT feishu_name, feishu_open_id FROM unified_user_mapping WHERE feishu_name!=''")
        return {row[0]: row[1] for row in c.fetchall()}


def get_auto_impl_enabled(repo_key):
    """查询仓库是否开启了自动实现"""
    from src.repo_key import to_repo_key
    repo_key = to_repo_key(repo_key)
    with db_lock:
        c = db.cursor()
        c.execute("SELECT auto_impl_enabled FROM repo_im_group_binding WHERE repo_key=?", (repo_key,))
        row = c.fetchone()
        return bool(row[0]) if row else False


def set_auto_impl_enabled(repo_key, enabled):
    """设置仓库的自动实现开关"""
    from src.repo_key import to_repo_key
    repo_key = to_repo_key(repo_key)
    with db_lock:
        c = db.cursor()
        c.execute("UPDATE repo_im_group_binding SET auto_impl_enabled=? WHERE repo_key=?", (int(enabled), repo_key))
        db.commit()


def get_notify_enabled(repo_key):
    """查询仓库是否开启了飞书通知"""
    from src.repo_key import to_repo_key
    repo_key = to_repo_key(repo_key)
    with db_lock:
        c = db.cursor()
        c.execute("SELECT notify_enabled FROM repo_im_group_binding WHERE repo_key=?", (repo_key,))
        row = c.fetchone()
        # default to 0 (False) if column is NULL
        return bool(row[0]) if row and row[0] is not None else False


def set_notify_enabled(repo_key, enabled):
    """设置仓库的飞书通知开关"""
    from src.repo_key import to_repo_key
    repo_key = to_repo_key(repo_key)
    with db_lock:
        c = db.cursor()
        c.execute("UPDATE repo_im_group_binding SET notify_enabled=? WHERE repo_key=?", (int(enabled), repo_key))
        db.commit()


# ====== chat_conversations CRUD ======

def create_conversation(user_id: int, repo_key: str = "", im_channel_id: str = "", title: str = "",
                        user_gitcode_token: str = "", agent_session_id: str = "") -> str:
    from src.repo_key import to_repo_key
    repo_key = to_repo_key(repo_key)
    import uuid
    cid = str(uuid.uuid4())
    with db_lock:
        c = db.cursor()
        c.execute("INSERT INTO chat_conversations (id, user_id, agent_session_id, runtime_session_id, repo_key, im_channel_id, title, user_gitcode_token) VALUES (?,?,?,?,?,?,?,?)",
                  (cid, user_id, agent_session_id, "", repo_key, im_channel_id, title, user_gitcode_token))
        db.commit()
    return cid


def get_user_conversations(user_id: int, repo_key: str = "", limit: int = 20) -> list[dict]:
    from src.repo_key import to_repo_key, from_repo_key
    if repo_key:
        repo_key = to_repo_key(repo_key)
    with db_lock:
        c = db.cursor()
        if repo_key:
            c.execute("SELECT id, user_id, agent_session_id, runtime_session_id, user_gitcode_token, repo_key, im_channel_id, title, status, created_at, updated_at FROM chat_conversations WHERE user_id=? AND repo_key=? ORDER BY updated_at DESC LIMIT ?", (user_id, repo_key, limit))
        else:
            c.execute("SELECT id, user_id, agent_session_id, runtime_session_id, user_gitcode_token, repo_key, im_channel_id, title, status, created_at, updated_at FROM chat_conversations WHERE user_id=? ORDER BY updated_at DESC LIMIT ?", (user_id, limit))
        return [{"id": r[0], "user_id": r[1], "agent_session_id": r[2] or "", "runtime_session_id": r[3] or "", "user_gitcode_token": r[4] or "", "repo_key": from_repo_key(r[5]), "im_channel_id": r[6], "title": r[7], "status": r[8], "created_at": r[9], "updated_at": r[10]} for r in c.fetchall()]


def get_conversation(conv_id: str) -> dict | None:
    from src.repo_key import from_repo_key
    with db_lock:
        c = db.cursor()
        c.execute("SELECT id, user_id, agent_session_id, runtime_session_id, user_gitcode_token, repo_key, im_channel_id, title, status, created_at, updated_at FROM chat_conversations WHERE id=?", (conv_id,))
        row = c.fetchone()
        if not row:
            return None
        return {"id": row[0], "user_id": row[1], "agent_session_id": row[2] or "", "runtime_session_id": row[3] or "", "user_gitcode_token": row[4] or "", "repo_key": from_repo_key(row[5]), "im_channel_id": row[6],
                "title": row[7], "status": row[8], "created_at": row[9], "updated_at": row[10]}


def get_conversation_by_im(im_channel_id: str, user_id: int = 0) -> dict | None:
    """按 IM 渠道 + 用户查询会话（IM chat 场景用，幂等复用）"""
    from src.repo_key import from_repo_key
    with db_lock:
        c = db.cursor()
        if user_id > 0:
            c.execute("SELECT id, user_id, agent_session_id, runtime_session_id, user_gitcode_token, repo_key, im_channel_id, title, status, created_at, updated_at FROM chat_conversations WHERE im_channel_id=? AND user_id=? ORDER BY updated_at DESC LIMIT 1", (im_channel_id, user_id))
        else:
            c.execute("SELECT id, user_id, agent_session_id, runtime_session_id, user_gitcode_token, repo_key, im_channel_id, title, status, created_at, updated_at FROM chat_conversations WHERE im_channel_id=? ORDER BY updated_at DESC LIMIT 1", (im_channel_id,))
        row = c.fetchone()
        if not row:
            return None
        return {"id": row[0], "user_id": row[1], "agent_session_id": row[2] or "", "runtime_session_id": row[3] or "", "user_gitcode_token": row[4] or "", "repo_key": from_repo_key(row[5]), "im_channel_id": row[6],
                "title": row[7], "status": row[8], "created_at": row[9], "updated_at": row[10]}


def update_conversation(conv_id: str, **kwargs):
    allowed = {"title", "status", "runtime_session_id", "agent_session_id", "user_gitcode_token"}
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


# ====== chat_config CRUD（合并进 chat_agent_session）======

def get_chat_config(user_id: int) -> dict:
    """获取用户全局 Chat 配置（repo_key='' 表示全局配置行）"""
    with db_lock:
        c = db.cursor()
        c.execute("SELECT runtime_id, skill, skill_id, im_app_id, im_app_secret, im_provider FROM chat_agent_session WHERE user_id=? AND (repo_key='' OR repo_key IS NULL) LIMIT 1", (user_id,))
        row = c.fetchone()
        if not row:
            return {"backend": "opencode", "skill": "", "skill_id": 0, "im_app_id": "", "im_app_secret": "", "im_provider": "", "updated_at": ""}
        backend_name = "opencode"
        if row[0]:
            c.execute("SELECT name FROM agent_runtimes WHERE id=?", (row[0],))
            brow = c.fetchone()
            if brow and brow[0]:
                from src.user.db import is_known_engine_name
                _n = (brow[0] or "").strip().lower()
                if is_known_engine_name(_n):
                    backend_name = brow[0]
                else:
                    # 幽灵/未知引擎（如历史遗留 'default'）：回退 opencode，避免 chat 路由到不存在的引擎
                    log.warning(f"get_chat_config: runtime_id={row[0]} 指向未知引擎 '{brow[0]}'，回退 opencode")
        return {"backend": backend_name, "skill": row[1] or "", "skill_id": row[2] or 0,
                "im_app_id": row[3] or "", "im_app_secret": row[4] or "", "im_provider": row[5] or "", "updated_at": ""}


def set_chat_config(user_id: int, **kwargs):
    """写入用户全局 Chat 配置（合并进 chat_agent_session，repo_key=''）"""
    allowed = {"backend", "skill", "skill_id", "im_app_id", "im_app_secret", "im_provider"}
    data = get_chat_config(user_id)
    for k, v in kwargs.items():
        if k in allowed and v is not None:
            data[k] = v
    # backend → runtime_id：优先按唯一引擎名（engine name）解析；Chat 仅个人引擎（见 runtime-admin-two-view-design §5.1）
    runtime_id = 0
    if data.get("backend"):
        from src.user.db import ensure_default_backend, get_runtime_id_by_name
        from src.runtime.instances import resolve_instance_id, scope_of_runtime
        _rid = resolve_instance_id(data["backend"])
        if _rid is not None and scope_of_runtime(_rid) == "user":
            runtime_id = _rid
        else:
            runtime_id = get_runtime_id_by_name(data["backend"]) or ensure_default_backend(data["backend"]) or 0
    # skill name → skill_id（若未显式传 skill_id）
    if data.get("skill") and not data.get("skill_id"):
        from src.user.db import resolve_skill_id
        data["skill_id"] = resolve_skill_id(data["skill"])
    sid = f"sess-cfg-{user_id}"
    with db_lock:
        c = db.cursor()
        c.execute(
            "INSERT OR REPLACE INTO chat_agent_session (id, user_id, repo_key, runtime_id, skill, skill_id, im_app_id, im_app_secret, im_provider, created_at, last_active_at) VALUES (?,?,?,?,?,?,?,?,?,datetime('now'),datetime('now'))",
            (sid, user_id, "", runtime_id, data["skill"], data.get("skill_id", 0), data["im_app_id"], data["im_app_secret"], data["im_provider"])
        )
        db.commit()
