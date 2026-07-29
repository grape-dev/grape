"""数据库初始化：新增 events、agent_sessions、配置表 + JSON 种子数据导入"""
import json, os, threading
from src.session_manager import db, db_lock
from src.config import log

# ====== 新表 DDL ======

_EVENTS_TABLE_DDL = """
CREATE TABLE IF NOT EXISTS events (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    repo_key TEXT NOT NULL,
    issue_id TEXT DEFAULT '',
    event_type TEXT NOT NULL,
    source TEXT DEFAULT 'gitcode',
    agent_type TEXT DEFAULT '',
    title TEXT DEFAULT '',
    body TEXT DEFAULT '',
    payload TEXT DEFAULT '{}',
    status TEXT DEFAULT 'pending',
    created_at TEXT DEFAULT (datetime('now'))
)
"""

_AGENT_SESSIONS_TABLE_DDL = """
CREATE TABLE IF NOT EXISTS agent_sessions (
    id TEXT PRIMARY KEY,
    repo_key TEXT NOT NULL,
    agent_type TEXT NOT NULL,
    backend TEXT DEFAULT 'opencode',
    skill TEXT DEFAULT '',
    oc_session_id TEXT DEFAULT '',
    repo_gitcode_token TEXT DEFAULT '',
    status TEXT DEFAULT 'idle',
    created_at TEXT DEFAULT (datetime('now')),
    last_active_at TEXT DEFAULT (datetime('now')),
    UNIQUE(repo_key, agent_type)
)
"""

_EVENT_ROUTE_CONFIG_DDL = """
CREATE TABLE IF NOT EXISTS event_route_config (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    repo_key TEXT DEFAULT '',
    event_type TEXT NOT NULL,
    skill TEXT NOT NULL,
    priority INTEGER DEFAULT 0,
    created_at TEXT DEFAULT (datetime('now'))
)
"""

_AGENT_TYPE_CONFIG_DDL = """
CREATE TABLE IF NOT EXISTS agent_type_config (
    agent_type TEXT PRIMARY KEY,
    display_name TEXT NOT NULL,
    description TEXT DEFAULT '',
    default_backend TEXT DEFAULT 'opencode',
    icon TEXT DEFAULT '',
    order_num INTEGER DEFAULT 0,
    parent_agent_type TEXT DEFAULT '',
    skill_path TEXT DEFAULT '',
    subscribe_events TEXT DEFAULT '[]',
    created_at TEXT DEFAULT (datetime('now'))
)
"""

_AGENT_REGISTRY_DDL = """
CREATE TABLE IF NOT EXISTS agent_registry (
    backend_name TEXT PRIMARY KEY,
    display_name TEXT NOT NULL,
    description TEXT DEFAULT '',
    cli_name TEXT DEFAULT '',
    is_active INTEGER DEFAULT 1,
    created_at TEXT DEFAULT (datetime('now'))
)
"""

_META_TABLE_DDL = """
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
)
"""

_BACKEND_AGENTS_DDL = """
CREATE TABLE IF NOT EXISTS backend_agents (
    name TEXT PRIMARY KEY,
    display_name TEXT DEFAULT '',
    installed INTEGER DEFAULT 0,
    cli_path TEXT DEFAULT '',
    version TEXT DEFAULT '',
    healthy INTEGER DEFAULT 0,
    pip_package TEXT DEFAULT '',
    updated_at TEXT DEFAULT (datetime('now'))
)
"""

_IM_CHANNELS_DDL = """
CREATE TABLE IF NOT EXISTS im_channels (
    id TEXT PRIMARY KEY,
    provider TEXT NOT NULL,
    channel_id TEXT NOT NULL,
    channel_name TEXT DEFAULT '',
    repo_key TEXT NOT NULL,
    source TEXT DEFAULT 'gitcode',
    bind_by TEXT DEFAULT '',
    auto_impl_enabled INTEGER DEFAULT 0,
    notify_enabled INTEGER DEFAULT 0,
    account TEXT DEFAULT 'default',
    created_at TEXT DEFAULT (datetime('now')),
    UNIQUE (provider, channel_id)
)
"""

_USER_IM_BINDINGS_DDL = """
CREATE TABLE IF NOT EXISTS user_im_bindings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    provider TEXT NOT NULL,
    channel_type TEXT NOT NULL DEFAULT 'direct_message',
    channel_id TEXT NOT NULL,
    enabled INTEGER DEFAULT 1,
    created_at TEXT DEFAULT (datetime('now')),
    UNIQUE (user_id, provider, channel_type)
)
"""

_CHAT_CONVERSATIONS_DDL = """
CREATE TABLE IF NOT EXISTS chat_conversations (
    id TEXT PRIMARY KEY,
    user_id INTEGER NOT NULL,
    session_id TEXT DEFAULT '',
    user_gitcode_token TEXT DEFAULT '',
    repo_key TEXT DEFAULT '',
    im_channel_id TEXT DEFAULT '',
    title TEXT DEFAULT '',
    status TEXT DEFAULT 'active',
    created_at TEXT DEFAULT (datetime('now')),
    updated_at TEXT DEFAULT (datetime('now'))
)
"""

_CHAT_MESSAGES_DDL = """
CREATE TABLE IF NOT EXISTS chat_messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    conversation_id TEXT NOT NULL,
    role TEXT NOT NULL,
    content TEXT NOT NULL,
    created_at TEXT DEFAULT (datetime('now'))
)
"""

_CHAT_CONFIG_DDL = """
CREATE TABLE IF NOT EXISTS chat_config (
    user_id INTEGER PRIMARY KEY,
    backend TEXT DEFAULT 'opencode',
    skill TEXT DEFAULT '',
    feishu_app_id TEXT DEFAULT '',
    feishu_app_secret TEXT DEFAULT '',
    updated_at TEXT DEFAULT (datetime('now'))
)
"""

_NEW_TABLES = [
    ("meta", _META_TABLE_DDL),
    ("backend_agents", _BACKEND_AGENTS_DDL),
    ("events", _EVENTS_TABLE_DDL),
    ("im_channels", _IM_CHANNELS_DDL),
    ("user_im_bindings", _USER_IM_BINDINGS_DDL),
    ("chat_conversations", _CHAT_CONVERSATIONS_DDL),
    ("chat_messages", _CHAT_MESSAGES_DDL),
    ("chat_config", _CHAT_CONFIG_DDL),
    ("agent_sessions", _AGENT_SESSIONS_TABLE_DDL),
    ("event_route_config", _EVENT_ROUTE_CONFIG_DDL),
    ("agent_type_config", _AGENT_TYPE_CONFIG_DDL),
    ("agent_registry", _AGENT_REGISTRY_DDL),
]

_CONFIG_DIR = os.path.join(os.path.dirname(__file__), "..", "..", "configs")

# (seed_key, json_filename, actual_table_name)
_SEED_FILES = [
    ("agent_type_config", "agent-description.json", "agent_type_config"),
    ("agent_type_config_skills", "skill-description.json", "agent_type_config"),
]


def _create_tables():
    """幂等创建新表"""
    for name, ddl in _NEW_TABLES:
        try:
            with db_lock:
                db.execute(ddl)
                db.commit()
            log.info(f"Table '{name}' ready")
        except Exception as e:
            log.warning(f"Create table '{name}' failed: {e}")


def _create_indexes():
    """创建必要索引"""
    indexes = [
        "CREATE INDEX IF NOT EXISTS idx_events_repo_key ON events(repo_key)",
        "CREATE INDEX IF NOT EXISTS idx_events_issue_id ON events(issue_id)",
        "CREATE INDEX IF NOT EXISTS idx_events_status ON events(status)",
        "CREATE INDEX IF NOT EXISTS idx_agent_sessions_repo ON agent_sessions(repo_key)",
        "CREATE INDEX IF NOT EXISTS idx_event_route_config_repo ON event_route_config(repo_key)",
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_chat_config_feishu_app_id ON chat_config(feishu_app_id) WHERE feishu_app_id != ''",
    ]
    for idx in indexes:
        try:
            with db_lock:
                db.execute(idx)
                db.commit()
        except Exception as e:
            log.warning(f"Create index failed: {e}")


def _seed_from_json():
    """首次运行时从 JSON 种子文件导入数据"""
    with db_lock:
        c = db.cursor()
        c.execute("SELECT value FROM meta WHERE key='seeded_keys'")
        row = c.fetchone()
        seeded_keys = set(row[0].split(",")) if row and row[0] else set()

    for seed_key, json_file, db_table in _SEED_FILES:
        if seed_key in seeded_keys:
            log.info(f"Seed key '{seed_key}' already run, skip")
            continue

        filepath = os.path.join(_CONFIG_DIR, json_file)
        if not os.path.exists(filepath):
            log.warning(f"Seed file not found: {filepath}")
            continue

        with db_lock:
            c = db.cursor()
            if seed_key == "agent_type_config_skills":
                # Skills share the same table as agent types, check for skill rows specifically
                c.execute("SELECT COUNT(*) FROM agent_type_config WHERE parent_agent_type!=''")
            else:
                c.execute(f"SELECT COUNT(*) FROM {db_table}")
            count = c.fetchone()[0]
            if count > 0:
                log.info(f"Table '{db_table}' already has {count} rows, skip seed from {json_file}")
                continue

        with open(filepath, "r", encoding="utf-8") as f:
            data = json.load(f)

        _insert_seed_data(seed_key, data)

        with db_lock:
            c = db.cursor()
            seeded_keys.add(seed_key)
            c.execute("INSERT OR REPLACE INTO meta (key, value) VALUES ('seeded_keys', ?)",
                      (",".join(sorted(seeded_keys)),))
            db.commit()
        log.info(f"Seeded {db_table} from {json_file}")


def _insert_seed_data(table: str, data: dict):
    """将 JSON 数据插入对应表"""
    with db_lock:
        c = db.cursor()
        if table == "event_route_config":
            # event-routing.json: {"routing": {...}, "repo_routing": {...}}
            for event_type, skill in data.get("routing", {}).items():
                c.execute(
                    "INSERT OR IGNORE INTO event_route_config (repo_key, event_type, skill) VALUES (?,?,?)",
                    ("", event_type, skill)
                )
            for repo_key, routes in data.get("repo_routing", {}).items():
                for event_type, skill in routes.items():
                    c.execute(
                        "INSERT OR IGNORE INTO event_route_config (repo_key, event_type, skill) VALUES (?,?,?)",
                        (repo_key, event_type, skill)
                    )

        elif table == "agent_type_config":
            # agent-description.json: {"agent_type": {"name": "...", ...}}
            for agent_type, info in data.items():
                display_name = info.get("name", agent_type)
                description = info.get("description", "")
                default_backend = info.get("backend", "opencode")
                icon = info.get("icon", "")
                order_num = info.get("order_num", 0)
                subscribe_events = json.dumps(info.get("subscribe_events", []), ensure_ascii=False)
                c.execute(
                    "INSERT OR IGNORE INTO agent_type_config (agent_type, display_name, description, default_backend, icon, order_num, subscribe_events) VALUES (?,?,?,?,?,?,?)",
                    (agent_type, display_name, description, default_backend, icon, order_num, subscribe_events)
                )

        elif table == "agent_type_config_skills":
            # skill-description.json: {"skill_name": {"name": "...", "agent_type": "...", "path": "...", ...}}
            for skill_name, info in data.items():
                display_name = info.get("name", skill_name)
                description = info.get("description", "")
                agent_type = info.get("agent_type", "")
                skill_path = info.get("path", skill_name)
                subscribe_events = json.dumps(info.get("subscribe_events", []), ensure_ascii=False)
                # Try UPDATE first (row may already exist from agent-description.json)
                c.execute(
                    "UPDATE agent_type_config SET parent_agent_type=?, description=?, skill_path=?, subscribe_events=? WHERE agent_type=? AND parent_agent_type=''",
                    (agent_type, description, skill_path, subscribe_events, skill_name)
                )
                if c.rowcount == 0:
                    # Skill-only key (not in agent-description.json), INSERT
                    c.execute(
                        "INSERT OR IGNORE INTO agent_type_config (agent_type, display_name, description, parent_agent_type, skill_path, subscribe_events) VALUES (?,?,?,?,?,?)",
                        (skill_name, display_name, description, agent_type, skill_path, subscribe_events)
                    )

        elif table == "agent_registry":
            # agent-routing.json: {"default_backend": "...", "skill_backends": {...}}
            default_backend = data.get("default_backend", "opencode")
            c.execute(
                "INSERT OR IGNORE INTO agent_registry (backend_name, display_name, description, cli_name) VALUES (?,?,?,?)",
                (default_backend, default_backend.capitalize(), f"Default backend: {default_backend}", default_backend)
            )
            for skill, backend in data.get("skill_backends", {}).items():
                c.execute(
                    "INSERT OR IGNORE INTO agent_registry (backend_name, display_name, description, cli_name) VALUES (?,?,?,?)",
                    (f"{skill}_{backend}", f"{skill} ({backend})", f"Skill '{skill}' uses {backend}", backend)
                )

        db.commit()


def init_database():
    """初始化所有新表（幂等），启动时调用"""
    _create_tables()
    _migrate_tables()
    _create_indexes()
    _seed_from_json()


def _migrate_tables():
    """已有数据库的 schema 迁移"""
    migrations = [
        "ALTER TABLE agent_type_config ADD COLUMN parent_agent_type TEXT DEFAULT ''",
        "ALTER TABLE user_skills ADD COLUMN agent_type TEXT DEFAULT ''",
        "ALTER TABLE user_skills ADD COLUMN description TEXT DEFAULT ''",
        "ALTER TABLE user_skills ADD COLUMN parent_agent_type TEXT DEFAULT ''",
        "ALTER TABLE agent_sessions ADD COLUMN skill TEXT DEFAULT ''",
        "ALTER TABLE chat_conversations ADD COLUMN session_id TEXT DEFAULT ''",
        "ALTER TABLE chat_config ADD COLUMN skill TEXT DEFAULT ''",
        "ALTER TABLE chat_conversations ADD COLUMN user_gitcode_token TEXT DEFAULT ''",
        "ALTER TABLE agent_sessions ADD COLUMN repo_gitcode_token TEXT DEFAULT ''",
        "ALTER TABLE agent_type_config ADD COLUMN skill_path TEXT DEFAULT ''",
        "ALTER TABLE feishu_users ADD COLUMN user_id INTEGER DEFAULT 0",
        "ALTER TABLE agent_type_config ADD COLUMN subscribe_events TEXT DEFAULT '[]'",
    ]
    for sql in migrations:
        try:
            with db_lock:
                db.execute(sql)
                db.commit()
        except Exception:
            pass  # column already exists
    # 补填已有技能的 skill_path（空值则用 agent_type 自身作为 path）
    try:
        with db_lock:
            db.execute("UPDATE agent_type_config SET skill_path=agent_type WHERE skill_path='' AND parent_agent_type!=''")
            db.commit()
    except Exception:
        pass
