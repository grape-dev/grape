"""数据库初始化：新增 events、agent_sessions、配置表 + JSON 种子数据导入"""
import json, os, threading
from src.session_manager import db, db_lock
from src.config import log

# ====== 新表 DDL ======

_EVENTS_TABLE_DDL = """
CREATE TABLE IF NOT EXISTS events (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    repo_key TEXT NOT NULL,
    event_type TEXT NOT NULL,
    source TEXT DEFAULT 'gitcode',
    issue_id TEXT DEFAULT '',
    title TEXT DEFAULT '',
    summary TEXT DEFAULT '',
    actor TEXT DEFAULT '',
    payload TEXT DEFAULT '{}',
    agent_type TEXT DEFAULT '',
    backend TEXT DEFAULT '',
    skill TEXT DEFAULT '',
    status TEXT DEFAULT 'pending',
    exit_code INTEGER DEFAULT NULL,
    duration REAL DEFAULT NULL,
    agent_logs TEXT DEFAULT '[]',
    created_at TEXT DEFAULT (datetime('now')),
    started_at TEXT DEFAULT '',
    completed_at TEXT DEFAULT ''
)
"""

_AGENT_SESSIONS_TABLE_DDL = """
CREATE TABLE IF NOT EXISTS devops_agent_session (
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

_AGENT_TYPE_DDL = """
CREATE TABLE IF NOT EXISTS agent_type (
    agent_type TEXT PRIMARY KEY,
    display_name TEXT NOT NULL,
    description TEXT DEFAULT '',
    default_backend TEXT DEFAULT 'opencode',
    icon TEXT DEFAULT '',
    order_num INTEGER DEFAULT 0,
    subscribe_events TEXT DEFAULT '[]',
    created_at TEXT DEFAULT (datetime('now'))
)
"""

_SKILL_DDL = """
CREATE TABLE IF NOT EXISTS skill (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    skill_name TEXT NOT NULL,
    display_name TEXT DEFAULT '',
    description TEXT DEFAULT '',
    agent_type TEXT DEFAULT '',
    skill_path TEXT DEFAULT '',
    scope TEXT DEFAULT 'official',
    owner_id TEXT DEFAULT '',
    is_active INTEGER DEFAULT 1,
    created_at TEXT DEFAULT (datetime('now')),
    updated_at TEXT DEFAULT (datetime('now')),
    UNIQUE (skill_name, scope, owner_id)
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

_IM_BOT_CONFIG_DDL = """
CREATE TABLE IF NOT EXISTS im_bot_config (
    bot_app_id TEXT PRIMARY KEY,
    bot_app_secret TEXT NOT NULL,
    bot_type TEXT NOT NULL,
    created_at TEXT DEFAULT (datetime('now'))
)
"""

_VOD_CONVERSATIONS_DDL = """
CREATE TABLE IF NOT EXISTS vod_conversations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    agent_session_id TEXT NOT NULL,
    feishu_chat_id TEXT NOT NULL,
    feishu_chat_name TEXT DEFAULT '',
    oc_session_id TEXT DEFAULT '',
    status TEXT DEFAULT 'active',
    created_at TEXT DEFAULT (datetime('now')),
    last_active_at TEXT DEFAULT (datetime('now')),
    UNIQUE(agent_session_id, feishu_chat_id)
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
    ("devops_agent_session", _AGENT_SESSIONS_TABLE_DDL),
    ("event_route_config", _EVENT_ROUTE_CONFIG_DDL),
    ("agent_type", _AGENT_TYPE_DDL),
    ("skill", _SKILL_DDL),
    ("agent_registry", _AGENT_REGISTRY_DDL),
    ("im_bot_config", _IM_BOT_CONFIG_DDL),
    ("vod_conversations", _VOD_CONVERSATIONS_DDL),
]

_CONFIG_DIR = os.path.join(os.path.dirname(__file__), "..", "..", "configs")

# (seed_key, json_filename, actual_table_name)
_SEED_FILES = [
    ("agent_type", "agent-description.json", "agent_type"),
    ("skill_official", "skill-description.json", "skill"),
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
        "CREATE INDEX IF NOT EXISTS idx_agent_sessions_repo ON devops_agent_session(repo_key)",
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
        if seed_key in seeded_keys and seed_key != "agent_type":
            log.info(f"Seed key '{seed_key}' already run, skip")
            continue

        filepath = os.path.join(_CONFIG_DIR, json_file)
        if not os.path.exists(filepath):
            log.warning(f"Seed file not found: {filepath}")
            continue

        with db_lock:
            c = db.cursor()
            if seed_key == "skill_official":
                # 官方技能：scope='official' 行
                c.execute("SELECT COUNT(*) FROM skill WHERE scope='official'")
            else:
                c.execute(f"SELECT COUNT(*) FROM {db_table}")
            count = c.fetchone()[0]
            if count > 0 and db_table != "agent_type":
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

        elif table == "agent_type":
            # agent-description.json: {"agent_type": {"name": "...", ...}}
            for agent_type, info in data.items():
                display_name = info.get("name", agent_type)
                description = info.get("description", "")
                default_backend = info.get("backend", "opencode")
                icon = info.get("icon", "")
                order_num = info.get("order_num", 0)
                subscribe_events = json.dumps(info.get("subscribe_events", []), ensure_ascii=False)
                c.execute(
                    "INSERT OR REPLACE INTO agent_type (agent_type, display_name, description, default_backend, icon, order_num, subscribe_events) VALUES (?,?,?,?,?,?,?)",
                    (agent_type, display_name, description, default_backend, icon, order_num, subscribe_events)
                )

        elif table == "skill_official":
            # skill-description.json: {"skill_name": {"name": "...", "agent_type": "...", "path": "...", ...}}
            for skill_name, info in data.items():
                display_name = info.get("name", skill_name)
                description = info.get("description", "")
                agent_type = info.get("agent_type", "")
                skill_path = info.get("path", skill_name)
                c.execute(
                    "INSERT OR REPLACE INTO skill (skill_name, display_name, description, agent_type, skill_path, scope, owner_id) VALUES (?,?,?,?,?,'official','')",
                    (skill_name, display_name, description, agent_type, skill_path)
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
        "ALTER TABLE devops_agent_session ADD COLUMN skill TEXT DEFAULT ''",
        "ALTER TABLE chat_conversations ADD COLUMN session_id TEXT DEFAULT ''",
        "ALTER TABLE chat_config ADD COLUMN skill TEXT DEFAULT ''",
        "ALTER TABLE chat_conversations ADD COLUMN user_gitcode_token TEXT DEFAULT ''",
        "ALTER TABLE devops_agent_session ADD COLUMN repo_gitcode_token TEXT DEFAULT ''",
        "ALTER TABLE agent_type_config ADD COLUMN skill_path TEXT DEFAULT ''",
        "ALTER TABLE feishu_users ADD COLUMN user_id INTEGER DEFAULT 0",
        "ALTER TABLE agent_type_config ADD COLUMN subscribe_events TEXT DEFAULT '[]'",
        "ALTER TABLE events ADD COLUMN actor TEXT DEFAULT ''",
        "ALTER TABLE events ADD COLUMN summary TEXT DEFAULT ''",
        "ALTER TABLE events ADD COLUMN backend TEXT DEFAULT ''",
        "ALTER TABLE events ADD COLUMN skill TEXT DEFAULT ''",
        "ALTER TABLE events ADD COLUMN exit_code INTEGER DEFAULT NULL",
        "ALTER TABLE events ADD COLUMN duration REAL DEFAULT NULL",
        "ALTER TABLE events ADD COLUMN agent_logs TEXT DEFAULT '[]'",
        "ALTER TABLE events ADD COLUMN started_at TEXT DEFAULT ''",
        "ALTER TABLE events ADD COLUMN completed_at TEXT DEFAULT ''",
        "ALTER TABLE devops_agent_session ADD COLUMN im_provider TEXT DEFAULT ''",
        "ALTER TABLE devops_agent_session ADD COLUMN im_app_id TEXT DEFAULT ''",
        "ALTER TABLE devops_agent_session ADD COLUMN im_app_secret TEXT DEFAULT ''",
        "ALTER TABLE devops_agent_session ADD COLUMN vod_bot_app_id TEXT DEFAULT ''",
    ]
    for sql in migrations:
        try:
            with db_lock:
                db.execute(sql)
                db.commit()
        except Exception:
            pass  # column already exists
    # 迁移 im 凭证到 im_bot_config 统一表
    try:
        with db_lock:
            c = db.cursor()
            # Chat Agent 凭证
            c.execute("INSERT OR IGNORE INTO im_bot_config (bot_app_id, bot_app_secret, bot_type) SELECT feishu_app_id, feishu_app_secret, 'chat_bot' FROM chat_config WHERE feishu_app_id!='' AND feishu_app_secret!='' AND feishu_app_secret!='********'")
            db.commit()
            log.info(f"Migrated IM configs to im_bot_config")
    except Exception as e:
        log.info(f"Migrate im_bot_config skipped: {e}")
    # 清理旧表（如果存在）
    try:
        with db_lock:
            db.execute("DROP TABLE IF EXISTS vod_bot_config")
            db.commit()
    except Exception:
        pass
    # 迁移 feishu_bindings 的 account='default' → Chat Bot app_id（清理 default 兜底账户）
    try:
        with db_lock:
            c = db.cursor()
            c.execute("SELECT COUNT(*) FROM feishu_bindings WHERE account='default' OR account IS NULL OR account=''")
            cnt = c.fetchone()[0]
            if cnt > 0:
                c.execute("SELECT bot_app_id FROM im_bot_config WHERE bot_type='chat_bot' LIMIT 1")
                row = c.fetchone()
                if row and row[0]:
                    c.execute("UPDATE feishu_bindings SET account=? WHERE account='default' OR account IS NULL OR account=''", (row[0],))
                    db.commit()
                    log.info(f"Migrated {cnt} feishu_bindings account='default' -> {row[0]}")
    except Exception as e:
        log.warning(f"Migrate feishu_bindings account failed: {e}")
    # 迁移 default 账户：OAuth 配置 → meta 表，bot_usernames → hwc-dev-bot 账户，删除 default
    try:
        with db_lock:
            c = db.cursor()
            c.execute("SELECT * FROM user_accounts WHERE username='default'")
            row = c.fetchone()
            if row:
                cols = [d[0] for d in c.description]
                acc = dict(zip(cols, row))
                migrated = False
                # 1. OAuth 配置 → meta 表
                oauth_keys = [
                    ("gitcode_oauth_client_id", "oauth_gitcode_client_id"),
                    ("gitcode_oauth_client_secret", "oauth_gitcode_client_secret"),
                    ("gitcode_oauth_redirect_uri", "oauth_gitcode_redirect_uri"),
                ]
                for src_key, meta_key in oauth_keys:
                    val = acc.get(src_key, "")
                    if val:
                        c.execute("SELECT COUNT(*) FROM meta WHERE key=?", (meta_key,))
                        if c.fetchone()[0] == 0:
                            c.execute("INSERT OR REPLACE INTO meta (key, value) VALUES (?,?)", (meta_key, val))
                            migrated = True
                # 2. bot_usernames → 迁移到 hwc-dev-bot 自身账户（若有该账户）
                bu = acc.get("bot_usernames", "") or ""
                try:
                    bu_list = json.loads(bu) if bu else []
                except (json.JSONDecodeError, TypeError):
                    bu_list = [s.strip() for s in bu.split(",") if s.strip()] if bu else []
                if bu_list:
                    c.execute("SELECT COUNT(*) FROM user_accounts WHERE username='hwc-dev-bot'")
                    if c.fetchone()[0] > 0:
                        for bot_name in bu_list:
                            c.execute("UPDATE user_accounts SET bot_usernames=? WHERE username=?", (json.dumps([bot_name]), bot_name))
                        migrated = True
                # 3. 删除 default 账户
                c.execute("DELETE FROM user_accounts WHERE username='default'")
                db.commit()
                if migrated:
                    log.info("Migrated default account config to meta/user_accounts and deleted default")
                else:
                    log.info("Deleted default account (no config to migrate)")
    except Exception as e:
        log.warning(f"Migrate/delete default account failed: {e}")
    # 迁移旧表 agent_sessions → devops_agent_session（幂等）
    try:
        with db_lock:
            c = db.cursor()
            _tables = [r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()]
            if "agent_sessions" in _tables and "devops_agent_session" not in _tables:
                c.execute("ALTER TABLE agent_sessions RENAME TO devops_agent_session")
                db.commit()
                log.info("Migrated table agent_sessions -> devops_agent_session")
            elif "agent_sessions" in _tables and "devops_agent_session" in _tables:
                # 新表已被创建但旧表仍存在（历史库），迁移数据后删除旧表
                c.execute("INSERT OR IGNORE INTO devops_agent_session (id, repo_key, agent_type, backend, skill, oc_session_id, repo_gitcode_token, status, created_at, last_active_at, im_provider, im_app_id, im_app_secret, vod_bot_app_id) SELECT id, repo_key, agent_type, backend, skill, oc_session_id, repo_gitcode_token, status, created_at, last_active_at, im_provider, im_app_id, im_app_secret, vod_bot_app_id FROM agent_sessions")
                c.execute("DROP TABLE agent_sessions")
                db.commit()
                log.info("Migrated agent_sessions data into devops_agent_session and dropped agent_sessions")
    except Exception as e:
        log.warning(f"Migrate agent_sessions table failed: {e}")
    # 迁移 agent_type_config → agent_type + skill（旧架构拆分）
    try:
        with db_lock:
            c = db.cursor()
            _tables = [r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()]
            if "agent_type_config" in _tables:
                # 1. Agent 类型（order_num>0）→ agent_type 表
                c.execute("SELECT agent_type, display_name, description, default_backend, icon, order_num, subscribe_events FROM agent_type_config WHERE order_num>0")
                for r in c.fetchall():
                    c.execute(
                        "INSERT OR IGNORE INTO agent_type (agent_type, display_name, description, default_backend, icon, order_num, subscribe_events) VALUES (?,?,?,?,?,?,?)",
                        (r[0], r[1], r[2] or "", r[3] or "opencode", r[4] or "", r[5] or 0, r[6] or '[]')
                    )
                # 2. 官方技能（parent_agent_type!=''）→ skill 表（scope=official）
                c.execute("SELECT agent_type, display_name, description, parent_agent_type, skill_path FROM agent_type_config WHERE parent_agent_type!=''")
                for r in c.fetchall():
                    c.execute(
                        "INSERT OR IGNORE INTO skill (skill_name, display_name, description, agent_type, skill_path, scope, owner_id) VALUES (?,?,?,?,?,'official','')",
                        (r[0], r[1], r[2] or "", r[3] or "", r[4] or r[0])
                    )
                db.commit()
                log.info("Migrated agent_type_config -> agent_type + skill")
    except Exception as e:
        log.warning(f"Migrate agent_type_config failed: {e}")
    # 迁移 user_skills → skill 表（scope=user）
    try:
        with db_lock:
            c = db.cursor()
            _tables = [r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()]
            if "user_skills" in _tables:
                c.execute("SELECT user_id, skill_name, agent_type, description, parent_agent_type FROM user_skills")
                for r in c.fetchall():
                    c.execute(
                        "INSERT OR IGNORE INTO skill (skill_name, display_name, description, agent_type, skill_path, scope, owner_id) VALUES (?,?,?,?,?,'user',?)",
                        (r[1], r[1], r[3] or "", r[2] or "", r[1], str(r[0]))
                    )
                db.commit()
                log.info("Migrated user_skills -> skill (scope=user)")
    except Exception as e:
        log.warning(f"Migrate user_skills failed: {e}")
    # 删除旧表（agent_type_config / user_skills / repo_skill_mapping）
    try:
        with db_lock:
            c = db.cursor()
            for tbl in ["agent_type_config", "user_skills", "repo_skill_mapping"]:
                c.execute(f"DROP TABLE IF EXISTS {tbl}")
            db.commit()
            log.info("Dropped legacy tables: agent_type_config, user_skills, repo_skill_mapping")
    except Exception as e:
        log.warning(f"Drop legacy skill tables failed: {e}")
