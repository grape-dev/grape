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
    oc_session_id TEXT DEFAULT '',
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

_NEW_TABLES = [
    ("events", _EVENTS_TABLE_DDL),
    ("agent_sessions", _AGENT_SESSIONS_TABLE_DDL),
    ("event_route_config", _EVENT_ROUTE_CONFIG_DDL),
    ("agent_type_config", _AGENT_TYPE_CONFIG_DDL),
    ("agent_registry", _AGENT_REGISTRY_DDL),
]

_CONFIG_DIR = os.path.join(os.path.dirname(__file__), "..", "..", "configs")

_SEED_FILES = {
    "event_route_config": "event-routing.json",
    "agent_type_config": "agent-description.json",
    "agent_registry": "agent-routing.json",
}


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
    for table, json_file in _SEED_FILES.items():
        filepath = os.path.join(_CONFIG_DIR, json_file)
        if not os.path.exists(filepath):
            log.warning(f"Seed file not found: {filepath}")
            continue

        with db_lock:
            c = db.cursor()
            c.execute(f"SELECT COUNT(*) FROM {table}")
            count = c.fetchone()[0]
            if count > 0:
                log.info(f"Table '{table}' already has {count} rows, skip seed")
                continue

        with open(filepath, "r", encoding="utf-8") as f:
            data = json.load(f)

        _insert_seed_data(table, data)
        log.info(f"Seeded {table} from {json_file}")


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
                c.execute(
                    "INSERT OR IGNORE INTO agent_type_config (agent_type, display_name, description, default_backend, icon, order_num) VALUES (?,?,?,?,?,?)",
                    (agent_type, display_name, description, default_backend, icon, order_num)
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
    _create_indexes()
    _seed_from_json()
