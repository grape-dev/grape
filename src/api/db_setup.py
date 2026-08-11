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
    result TEXT DEFAULT '',
    created_at TEXT DEFAULT (datetime('now')),
    started_at TEXT DEFAULT '',
    completed_at TEXT DEFAULT ''
)
"""

_EVENT_SKIPPED_DDL = """
CREATE TABLE IF NOT EXISTS event_skipped (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    repo_key TEXT DEFAULT '',
    event_type TEXT DEFAULT '',
    reason TEXT DEFAULT '',
    detail TEXT DEFAULT '',
    created_at TEXT DEFAULT (datetime('now'))
)
"""

_AGENT_SESSIONS_TABLE_DDL = """
CREATE TABLE IF NOT EXISTS devops_agent_session (
    id TEXT PRIMARY KEY,
    repo_key TEXT NOT NULL,
    agent_type TEXT NOT NULL,
    backend_id INTEGER DEFAULT 0,
    skill TEXT DEFAULT '',
    backend_session_id TEXT DEFAULT '',
    repo_gitcode_token TEXT DEFAULT '',
    status TEXT DEFAULT 'idle',
    im_provider TEXT DEFAULT '',
    im_app_id TEXT DEFAULT '',
    im_app_secret TEXT DEFAULT '',
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
    default_backend_id INTEGER DEFAULT 0,
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

_META_TABLE_DDL = """
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
)
"""

_BACKEND_AGENTS_DDL = """
CREATE TABLE IF NOT EXISTS backend_agents (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    display_name TEXT DEFAULT '',
    installed INTEGER DEFAULT 0,
    cli_path TEXT DEFAULT '',
    version TEXT DEFAULT '',
    healthy INTEGER DEFAULT 0,
    pip_package TEXT DEFAULT '',
    scope TEXT DEFAULT 'global',
    owner_id TEXT DEFAULT '',
    updated_at TEXT DEFAULT (datetime('now')),
    UNIQUE (name, scope, owner_id)
)
"""

# new-version2：backend_agents → agent_runtimes（术语重构，见 agent-runtime-design.md §5.2）
# 多实例键（multi-runtime-instance-design §2.1）：注册键 = (credential_id, name, scope, owner_id)，
# 使每台 worker（凭据）对每个引擎独占一行、同时在线。老库由迁移重建（_rebuild_agent_runtimes_inst_key）。
_AGENT_RUNTIMES_DDL = """
CREATE TABLE IF NOT EXISTS agent_runtimes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    credential_id INTEGER DEFAULT 0,
    name TEXT NOT NULL,
    display_name TEXT DEFAULT '',
    provider TEXT DEFAULT '',
    binary TEXT DEFAULT '',
    installed INTEGER DEFAULT 0,
    cli_path TEXT DEFAULT '',
    version TEXT DEFAULT '',
    healthy INTEGER DEFAULT 0,
    pip_package TEXT DEFAULT '',
    status TEXT DEFAULT 'offline',
    last_seen_at TEXT DEFAULT NULL,
    scope TEXT DEFAULT 'global',
    owner_id TEXT DEFAULT '',
    updated_at TEXT DEFAULT (datetime('now')),
    UNIQUE (credential_id, name, scope, owner_id)
)
"""

_CHAT_CONVERSATIONS_DDL = """
CREATE TABLE IF NOT EXISTS chat_conversations (
    id TEXT PRIMARY KEY,
    agent_session_id TEXT DEFAULT '',
    user_id INTEGER NOT NULL,
    backend_session_id TEXT DEFAULT '',
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

_GLOBAL_IM_BOT_CONFIG_DDL = """
CREATE TABLE IF NOT EXISTS global_im_bot_config (
    bot_app_id TEXT PRIMARY KEY,
    bot_type TEXT NOT NULL,
    im_provider TEXT DEFAULT 'feishu',
    is_active INTEGER DEFAULT 1,
    created_at TEXT DEFAULT (datetime('now'))
)
"""

_VOD_CONVERSATIONS_DDL = """
CREATE TABLE IF NOT EXISTS vod_conversations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    agent_session_id TEXT NOT NULL,
    im_provider TEXT DEFAULT 'feishu',
    im_chat_id TEXT NOT NULL,
    im_channel_name TEXT DEFAULT '',
    backend_session_id TEXT DEFAULT '',
    status TEXT DEFAULT 'active',
    created_at TEXT DEFAULT (datetime('now')),
    last_active_at TEXT DEFAULT (datetime('now')),
    UNIQUE(agent_session_id, im_chat_id)
)
"""

_TASKS_DDL = """
CREATE TABLE IF NOT EXISTS tasks (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id          INTEGER NOT NULL,
    assignee         TEXT NOT NULL,
    repo_key         TEXT NOT NULL,
    issue_number     INTEGER NOT NULL,
    title            TEXT DEFAULT '',
    task_description TEXT DEFAULT '',
    conversation_id  TEXT DEFAULT '',
    status           TEXT DEFAULT 'active',
    completed_reason TEXT DEFAULT '',
    created_at       TEXT DEFAULT (datetime('now')),
    completed_at     TEXT DEFAULT NULL
)
"""

# ===== new-version2 M1：agent_tasks 任务队列（确定路由 + 事件档案快照）=====
# 见 agent-runtime-event-full-design.md §4.3 / concurrency-design.md / skill-delivery-remote.md
_AGENT_TASKS_DDL = """
CREATE TABLE IF NOT EXISTS agent_tasks (
    id                TEXT PRIMARY KEY,
    agent_session_id  TEXT NOT NULL,        -- 外键 → devops_agent_session.id
    runtime_id        INTEGER NOT NULL,     -- 确定路由 → agent_runtimes.id
    event_seq         INTEGER,              -- 关联 events 表 seq
    issue_id          TEXT DEFAULT '',      -- 关联 event.json pipeline 段
    repo_key          TEXT DEFAULT '',
    agent_type        TEXT DEFAULT '',
    skill             TEXT DEFAULT '',
    prompt            TEXT NOT NULL,
    cwd               TEXT DEFAULT '',
    runtime_session_id TEXT DEFAULT '',
    event_type        TEXT DEFAULT '',
    account           TEXT DEFAULT '',
    user_id           INTEGER DEFAULT 0,
    repo_gitcode_token TEXT DEFAULT '',
    env_extra         TEXT DEFAULT '{}',    -- JSON（含服务端已注入的账号/用户凭据，见 skill-delivery-remote.md）
    skill_files       TEXT DEFAULT '{}',    -- JSON: {skill_name: {rel_path: content}}，远端交付技能包
    server_url        TEXT DEFAULT '',      -- A2A 外呼地址，worker 替换 {{SERVER_URL}}
    issue_session_id  TEXT DEFAULT '',      -- 外键 → issue_agent_session.id（Issue 级执行会话，串行粒度，见 multi-issue-isolation-design.md §3.3）
    project_info_files TEXT DEFAULT '{}',   -- JSON: {rel_path: content}，repo 全局 project-info 随任务交付（§2.4）
    project_info_meta  TEXT DEFAULT '{}',   -- JSON: {last_commit_sha, branches, last_updated, degraded}
    status            TEXT DEFAULT 'queued', -- queued/dispatched/running/completed/failed/cancelled
    exit_code         INTEGER DEFAULT NULL,
    result_text       TEXT DEFAULT '',
    errors            TEXT DEFAULT '',
    stderr            TEXT DEFAULT '',
    usage_json        TEXT DEFAULT '{}',
    started_at        TEXT DEFAULT NULL,
    completed_at      TEXT DEFAULT NULL,
    created_at        TEXT DEFAULT (datetime('now'))
)
"""

# ===== new-version2 M3：runtime_credentials 独立凭据（rta_ token，见 agent-runtime-event-full-design.md §4.1）=====
_RUNTIME_CREDENTIALS_DDL = """
CREATE TABLE IF NOT EXISTS runtime_credentials (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    token_hash   TEXT NOT NULL,             -- sha256(rta_xxx)，不存明文
    name         TEXT NOT NULL UNIQUE,      -- 凭据名=worker 名，全局唯一（可改名）
    device_name  TEXT DEFAULT '',           -- 首次注册时回填（边端自报，辅助展示）
    scope        TEXT DEFAULT 'user',       -- user(个人)/repo(仓库)/global(全局)，默认个人
    owner_id     TEXT DEFAULT '',           -- 仓库 scope 时存 repo 全名（如 owner/repo）；个人/全局为 ''
    owner_name   TEXT DEFAULT '',           -- 归属用户 user_name（生成凭据的当前登录用户）
    status       TEXT DEFAULT 'active',     -- active / revoked
    last_seen_at TEXT DEFAULT NULL,
    last_ip      TEXT DEFAULT '',           -- 最近注册/心跳来源 IP（展示用，不入唯一键）
    created_at   TEXT DEFAULT (datetime('now')),
    UNIQUE (token_hash)
)
"""

# ===== new-version2 VOD：Issue 生命周期状态机（权威阶段，见 vod-skill-upgrade-plan.md §3.0.2）=====
_ISSUE_STATE_DDL = """
CREATE TABLE IF NOT EXISTS issue_state (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    repo_key     TEXT NOT NULL,
    issue_id     TEXT NOT NULL,             -- 如 "#5"
    stage        TEXT NOT NULL DEFAULT 'vod',   -- vod/issue/coding/qa/sre/done
    owner_agent  TEXT NOT NULL DEFAULT 'vod',   -- 当前归属 agent（= 最新被交办者）
    assignee     TEXT DEFAULT '',          -- Issue 责任人（GitCode assignee，可为 grape_bot 或用户）
    a2a_seq      INTEGER,                   -- 触发当前 stage 的 A2A 事件 seq
    updated_at   TEXT DEFAULT (datetime('now')),
    created_at   TEXT DEFAULT (datetime('now')),
    UNIQUE (repo_key, issue_id)
)
"""

# ===== new-version2 Issue 级执行会话（多 Issue 并行，见 multi-issue-isolation-design.md §3.3）=====
# 配置层（devops_agent_session，repo+agent）与执行层（issue_agent_session，repo+agent+issue）分离。
_ISSUE_AGENT_SESSION_DDL = """
CREATE TABLE IF NOT EXISTS issue_agent_session (
    id                TEXT PRIMARY KEY,          -- uuid，agent_tasks.issue_session_id 外键
    repo_key          TEXT NOT NULL,
    agent_type        TEXT NOT NULL,
    issue_id          TEXT NOT NULL,             -- 如 "#5"（对应 event.json 的 issue_num）
    runtime_session_id TEXT DEFAULT '',          -- opencode --session 续接指针（Issue 级）
    status            TEXT DEFAULT 'idle',       -- idle / running / failed
    last_active_at    TEXT DEFAULT (datetime('now')),
    created_at        TEXT DEFAULT (datetime('now')),
    UNIQUE (repo_key, agent_type, issue_id)
)
"""

_NEW_TABLES = [
    ("meta", _META_TABLE_DDL),
    ("agent_runtimes", _AGENT_RUNTIMES_DDL),
    ("agent_tasks", _AGENT_TASKS_DDL),
    ("runtime_credentials", _RUNTIME_CREDENTIALS_DDL),
    ("issue_state", _ISSUE_STATE_DDL),
    ("issue_agent_session", _ISSUE_AGENT_SESSION_DDL),
    ("events", _EVENTS_TABLE_DDL),
    ("event_skipped", _EVENT_SKIPPED_DDL),
    ("chat_conversations", _CHAT_CONVERSATIONS_DDL),
    ("chat_messages", _CHAT_MESSAGES_DDL),
    ("devops_agent_session", _AGENT_SESSIONS_TABLE_DDL),
    ("event_route_config", _EVENT_ROUTE_CONFIG_DDL),
    ("agent_type", _AGENT_TYPE_DDL),
    ("skill", _SKILL_DDL),
    ("global_im_bot_config", _GLOBAL_IM_BOT_CONFIG_DDL),
    ("vod_conversations", _VOD_CONVERSATIONS_DDL),
    ("tasks", _TASKS_DDL),
    ("bot_seen_messages", """
CREATE TABLE IF NOT EXISTS bot_seen_messages (
    app_id TEXT NOT NULL,
    message_id TEXT NOT NULL,
    seen_at REAL NOT NULL,
    PRIMARY KEY (app_id, message_id)
)"""),
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


def _ensure_column(c, tbl: str, col: str, ddl: str):
    """幂等补列：PRAGMA 检查列不存在才 ALTER"""
    cols = [r[1] for r in c.execute(f"PRAGMA table_info({tbl})").fetchall()]
    if col not in cols:
        c.execute(f"ALTER TABLE {tbl} ADD COLUMN {col} {ddl}")


def _rename_column_if_exists(c, tbl: str, old: str, new: str):
    """幂等改名：旧列存在才处理（SQLite 3.25+）。
    - 若 new 不存在：RENAME COLUMN（保数据）
    - 若 new 已存在（如 session_manager 模块级 ALTER 预建的兼容列）且 old 仍有数据：
      把 old 数据并入 new，再 DROP old，避免双列并存丢数据
    """
    cols = [r[1] for r in c.execute(f"PRAGMA table_info({tbl})").fetchall()]
    if old not in cols:
        return
    if new not in cols:
        c.execute(f"ALTER TABLE {tbl} RENAME COLUMN {old} TO {new}")
        return
    # 双列并存：按列类型归并 old → new（new 为默认值才填 old，避免覆盖新值）
    try:
        otype = [r[2] for r in c.execute(f"PRAGMA table_info({tbl})").fetchall() if r[1] == old][0].upper()
    except Exception:
        otype = "TEXT"
    if "INT" in otype:
        c.execute(f"UPDATE {tbl} SET {new}=CASE WHEN {new} IS NULL OR {new}=0 THEN {old} ELSE {new} END WHERE {old} IS NOT NULL AND {old}!=0")
    else:
        c.execute(f"UPDATE {tbl} SET {new}=CASE WHEN {new} IS NULL OR {new}='' THEN {old} ELSE {new} END WHERE {old} IS NOT NULL AND {old}!=''")
    try:
        c.execute(f"ALTER TABLE {tbl} DROP COLUMN {old}")
    except Exception:
        pass


def _copy_backend_to_runtime(c):
    """backend_agents → agent_runtimes 数据搬迁（保留 id；若 agent_runtimes 已有同 name 行——
    如历史迁移 ensure_default_runtime 预置的空行——则用老库数据补全缺失字段，老库为权威）。"""
    cols = [r[1] for r in c.execute("PRAGMA table_info(backend_agents)").fetchall()]
    if "name" not in cols:
        return
    rcols = [r[1] for r in c.execute("PRAGMA table_info(agent_runtimes)").fetchall()]
    rows = c.execute("SELECT id, name, display_name, installed, cli_path, version, healthy, pip_package, scope, owner_id, updated_at FROM backend_agents").fetchall()
    for r in rows:
        rid, name, display, installed, cli, ver, healthy, pip, scope, owner, updated = r
        existing = c.execute("SELECT id FROM agent_runtimes WHERE lower(name)=? AND scope=? AND owner_id=? LIMIT 1",
                             (name.lower() if name else name, scope or "global", owner or "")).fetchone()
        if existing:
            # 已存在（如 ensure 预置空行）：补全缺失字段，保留其 id
            c.execute("""UPDATE agent_runtimes SET display_name=?, installed=?, cli_path=?, version=?, healthy=?, pip_package=?, updated_at=?
                         WHERE id=? AND (display_name='' OR installed=0 OR cli_path='' OR version='' OR healthy=0 OR pip_package='')""",
                      (display or name, 1 if installed else 0, cli or "", ver or "", 1 if healthy else 0, pip or "", updated or "", existing[0]))
        else:
            c.execute("INSERT OR IGNORE INTO agent_runtimes (id, name, display_name, installed, cli_path, version, healthy, pip_package, scope, owner_id, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                      (rid, name, display or name, 1 if installed else 0, cli or "", ver or "", 1 if healthy else 0, pip or "", scope or "global", owner or "", updated or ""))


def _create_indexes():
    """创建必要索引"""
    indexes = [
        "CREATE INDEX IF NOT EXISTS idx_events_repo_key ON events(repo_key)",
        "CREATE INDEX IF NOT EXISTS idx_events_issue_id ON events(issue_id)",
        "CREATE INDEX IF NOT EXISTS idx_events_status ON events(status)",
        "CREATE INDEX IF NOT EXISTS idx_agent_sessions_repo ON devops_agent_session(repo_key)",
        "CREATE INDEX IF NOT EXISTS idx_event_route_config_repo ON event_route_config(repo_key)",
        "CREATE INDEX IF NOT EXISTS idx_bot_seen_messages_app_id ON bot_seen_messages(app_id)",
        "CREATE INDEX IF NOT EXISTS idx_bot_seen_messages_seen_at ON bot_seen_messages(seen_at)",
        "CREATE INDEX IF NOT EXISTS idx_tasks_user_status ON tasks(user_id, status)",
        "CREATE INDEX IF NOT EXISTS idx_tasks_repo_issue ON tasks(repo_key, issue_number)",
        "CREATE INDEX IF NOT EXISTS idx_agent_tasks_status ON agent_tasks(status)",
        "CREATE INDEX IF NOT EXISTS idx_agent_tasks_runtime ON agent_tasks(runtime_id)",
        "CREATE INDEX IF NOT EXISTS idx_agent_tasks_session ON agent_tasks(agent_session_id)",
        "CREATE INDEX IF NOT EXISTS idx_agent_tasks_event ON agent_tasks(event_seq)",
        "CREATE INDEX IF NOT EXISTS idx_agent_tasks_issue_session ON agent_tasks(issue_session_id)",
        "CREATE INDEX IF NOT EXISTS idx_issue_agent_session_repo ON issue_agent_session(repo_key, agent_type, issue_id)",
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
            # 已 seed 过：skill_official 仍需幂等补种缺失技能；其余跳过
            if seed_key != "skill_official":
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
                # 表已有数据：跳过全量 seed，但对 skill-description.json 中缺失的官方技能幂等补种
                if seed_key == "skill_official":
                    with open(filepath, "r", encoding="utf-8") as f:
                        data = json.load(f)
                    for skill_name, info in data.items():
                        existing = c.execute("SELECT COUNT(*) FROM skill WHERE skill_name=?", (skill_name,)).fetchone()
                        if existing and existing[0] == 0:
                            display_name = info.get("name", skill_name)
                            description = info.get("description", "")
                            agent_type = info.get("agent_type", "")
                            skill_path = info.get("path", skill_name)
                            c.execute(
                                "INSERT OR REPLACE INTO skill (skill_name, display_name, description, agent_type, skill_path, scope, owner_id) VALUES (?,?,?,?,?,'official','')",
                                (skill_name, display_name, description, agent_type, skill_path)
                            )
                            log.info(f"Seeded missing official skill: {skill_name}")
                    db.commit()
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
                default_runtime_name = info.get("backend", "opencode")
                # 预置 runtime（幂等），拿 id
                from src.user.db import ensure_default_runtime
                default_runtime_id = ensure_default_runtime(default_runtime_name) or 0
                icon = info.get("icon", "")
                order_num = info.get("order_num", 0)
                subscribe_events = json.dumps(info.get("subscribe_events", []), ensure_ascii=False)
                enable = 1 if info.get("enable", True) else 0
                c.execute(
                    "INSERT OR REPLACE INTO agent_type (agent_type, display_name, description, default_runtime_id, icon, order_num, subscribe_events, enable) VALUES (?,?,?,?,?,?,?,?)",
                    (agent_type, display_name, description, default_runtime_id, icon, order_num, subscribe_events, enable)
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
            # agent-routing.json 已废弃（agent_registry 表移除），backend 统一用 backend_agents
            pass

        db.commit()


def init_database():
    """初始化所有新表（幂等），启动时调用"""
    _create_tables()
    _migrate_tables()
    _create_indexes()
    _seed_from_json()
    _align_agent_enable()


def _align_agent_enable():
    """存量会话 enable 对齐 agent_type 默认值（仅执行一次，幂等）。

    依赖：须在 _seed_from_json 之后调用（agent_type.enable 已由 JSON seed 写入正确默认值，
    如 sre=0）。用 meta 标记 'enable_aligned' 保证只跑一次，避免覆盖用户后续在
    Dashboard 手动设置的仓库级 enable。
    """
    try:
        with db_lock:
            c = db.cursor()
            done = c.execute("SELECT COUNT(*) FROM meta WHERE key='enable_aligned'").fetchone()[0]
            if done:
                return
            # 先确保 enable 列存在（新库可能无）
            _ensure_column(c, "agent_type", "enable", "INTEGER DEFAULT 1")
            _ensure_column(c, "devops_agent_session", "enable", "INTEGER DEFAULT 1")
            c.execute(
                "UPDATE devops_agent_session SET enable = COALESCE((SELECT enable FROM agent_type WHERE agent_type = devops_agent_session.agent_type), 1)"
            )
            c.execute("INSERT OR REPLACE INTO meta (key, value) VALUES ('enable_aligned', '1')")
            db.commit()
            log.info("Aligned existing agent session enable from agent_type defaults")
    except Exception as e:
        log.warning(f"Align agent enable failed: {e}")


def _drop_chat_session_repo_unique(c):
    """幂等修复：重建 chat_agent_session，去掉陈旧的 `repo_key TEXT UNIQUE` 约束。

    背景：chat_agent_session 由旧 `sessions` 表 `ALTER TABLE ... RENAME` 而来，SQLite RENAME
    保留了旧表 `repo_key TEXT UNIQUE`；而代码 DDL（session_manager.py）意图为 per-user
    （repo_key TEXT DEFAULT ''，无 UNIQUE）。陈旧约束使全局 chat 配置（repo_key=''）只能有一行，
    多用户配置互相 INSERT OR REPLACE 顶掉，导致 chat bot secret 丢失、断连。

    检测 sqlite_master 中该表定义含 `repo_key TEXT UNIQUE` → 按当前 PRAGMA 列定义重建表
    （保留数据与全部列，仅去掉 UNIQUE）。返回是否执行了重建。
    """
    row = c.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='chat_agent_session'").fetchone()
    if not row or not row[0]:
        return False
    if "repo_key text unique" not in row[0].lower():
        return False
    info = c.execute("PRAGMA table_info(chat_agent_session)").fetchall()
    cols = [r[1] for r in info]
    col_defs = []
    for r in info:
        _cid, name, typ, notnull, dflt, pk = r
        d = f'"{name}" {typ or ""}'.rstrip()
        if notnull:
            d += " NOT NULL"
        if dflt is not None:
            d += " DEFAULT " + str(dflt)
        if pk:
            d += " PRIMARY KEY"
        col_defs.append(d)
    c.execute("CREATE TABLE chat_agent_session_new (" + ", ".join(col_defs) + ")")
    c.execute(f"INSERT INTO chat_agent_session_new ({','.join(cols)}) SELECT {','.join(cols)} FROM chat_agent_session")
    c.execute("DROP TABLE chat_agent_session")
    c.execute("ALTER TABLE chat_agent_session_new RENAME TO chat_agent_session")
    return True


def _rebuild_agent_runtimes_inst_key(c):
    """多实例键迁移（multi-runtime-instance-design §7）：把 agent_runtimes 的旧唯一约束
    `UNIQUE (name, scope, owner_id)` 升级为 `UNIQUE (credential_id, name, scope, owner_id)`。

    SQLite 无法直接改约束 → 检测到旧约束时重建表（用新 DDL，保留数据）。重建前清理
    `credential_id=0` 的同名重复行（历史迁移遗留，仅保留 id 最小的一行），避免新唯一键冲突。
    返回是否执行了重建（幂等：已是新键则跳过）。
    """
    row = c.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='agent_runtimes'").fetchone()
    if not row or not row[0]:
        return False
    sql = row[0].lower()
    if "unique" not in sql or "credential_id, name" in sql:
        return False
    c.execute("""
        DELETE FROM agent_runtimes
        WHERE credential_id=0 AND id NOT IN (
            SELECT MIN(id) FROM agent_runtimes WHERE credential_id=0 GROUP BY lower(name), scope, owner_id
        )
    """)
    cols = [r[1] for r in c.execute("PRAGMA table_info(agent_runtimes)").fetchall()]
    c.execute(_AGENT_RUNTIMES_DDL.replace("CREATE TABLE IF NOT EXISTS agent_runtimes", "CREATE TABLE agent_runtimes_new"))
    c.execute(f"INSERT INTO agent_runtimes_new ({','.join(cols)}) SELECT {','.join(cols)} FROM agent_runtimes")
    c.execute("DROP TABLE agent_runtimes")
    c.execute("ALTER TABLE agent_runtimes_new RENAME TO agent_runtimes")
    return True


def _migrate_tables():
    """已有数据库的 schema 迁移"""
    # ===== new-version2 数据安全：迁移前自动备份（仅在有改动性迁移时触发一次）=====
    try:
        from src.session_manager import _backup_db, _db_path
        # 仅当存在旧结构（backend_* 表/列）时备份，避免无谓备份
        with db_lock:
            c = db.cursor()
            _tables = [r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()]
            _need_backup = "backend_agents" in _tables
            if not _need_backup:
                for _t in ("devops_agent_session", "agent_type", "chat_agent_session", "chat_conversations", "vod_conversations", "events"):
                    if _t in _tables:
                        _cols = [r[1] for r in c.execute(f"PRAGMA table_info({_t})").fetchall()]
                        if any(col.startswith("backend") for col in _cols):
                            _need_backup = True
                            break
            if _need_backup:
                _backup_db(_db_path, tag="pre-m0")
                # 记录已备份，避免多进程重复备份
                try:
                    c.execute("INSERT OR IGNORE INTO meta (key, value) VALUES ('m0_backup_done', '1')")
                    db.commit()
                except Exception:
                    pass
    except Exception as e:
        log.warning(f"Pre-migration backup skipped: {e}")
    migrations = [
        "ALTER TABLE agent_type_config ADD COLUMN parent_agent_type TEXT DEFAULT ''",
        "ALTER TABLE user_skills ADD COLUMN agent_type TEXT DEFAULT ''",
        "ALTER TABLE devops_agent_session ADD COLUMN skill TEXT DEFAULT ''",
        "ALTER TABLE chat_agent_session ADD COLUMN skill TEXT DEFAULT ''",
        "ALTER TABLE devops_agent_session ADD COLUMN skill_id INTEGER DEFAULT 0",
        "ALTER TABLE chat_agent_session ADD COLUMN skill_id INTEGER DEFAULT 0",
        "ALTER TABLE chat_conversations ADD COLUMN session_id TEXT DEFAULT ''",
        "ALTER TABLE chat_conversations ADD COLUMN user_gitcode_token TEXT DEFAULT ''",
        "ALTER TABLE devops_agent_session ADD COLUMN repo_gitcode_token TEXT DEFAULT ''",
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
        "ALTER TABLE devops_agent_session ADD COLUMN backend_session_id TEXT DEFAULT ''",
        "ALTER TABLE chat_conversations ADD COLUMN agent_session_id TEXT DEFAULT ''",
        "ALTER TABLE chat_conversations ADD COLUMN backend_session_id TEXT DEFAULT ''",
        "ALTER TABLE devops_agent_session ADD COLUMN backend_id INTEGER DEFAULT 0",
        "ALTER TABLE agent_type ADD COLUMN default_backend_id INTEGER DEFAULT 0",
        "ALTER TABLE chat_agent_session ADD COLUMN backend_id INTEGER DEFAULT 0",
        "ALTER TABLE events ADD COLUMN result TEXT DEFAULT ''",
        "ALTER TABLE issue_state ADD COLUMN assignee TEXT DEFAULT ''",
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
            # 迁移旧表 im_bot_config → global_im_bot_config（保留 bot_type，secret 移入 devops_agent_session）
            _tables = [r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()]
            if "im_bot_config" in _tables and "global_im_bot_config" not in _tables:
                c.execute("ALTER TABLE im_bot_config RENAME TO global_im_bot_config")
                db.commit()
                log.info("Migrated table im_bot_config -> global_im_bot_config")
            elif "im_bot_config" in _tables and "global_im_bot_config" in _tables:
                c.execute("INSERT OR IGNORE INTO global_im_bot_config (bot_app_id, bot_type) SELECT bot_app_id, bot_type FROM im_bot_config")
                # 补齐 secret 到 devops_agent_session
                c.execute("UPDATE devops_agent_session SET im_app_secret=(SELECT bot_app_secret FROM im_bot_config WHERE bot_app_id=devops_agent_session.im_app_id) WHERE im_app_id!='' AND im_app_secret=''")
                c.execute("DROP TABLE im_bot_config")
                db.commit()
                log.info("Migrated im_bot_config -> global_im_bot_config (data moved)")
    except Exception as e:
        log.info(f"Migrate global_im_bot_config skipped: {e}")
    # 迁移旧表 chat_config → chat_agent_session（合并 user 配置）
    try:
        with db_lock:
            c = db.cursor()
            _tables = [r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()]
            if "chat_config" in _tables:
                c.execute("SELECT user_id, backend, skill, feishu_app_id, feishu_app_secret FROM chat_config")
                for r in c.fetchall():
                    if not r[0]:
                        continue
                    sid = f"sess-cfg-{r[0]}"
                    c.execute("INSERT OR REPLACE INTO chat_agent_session (id, user_id, repo_key, backend, skill, im_app_id, im_app_secret, im_provider, created_at, last_active_at) VALUES (?,?,?,?,?,?,?,'feishu',datetime('now'),datetime('now'))",
                              (sid, r[0], "", r[1] or "opencode", r[2] or "", r[3] or "", r[4] or ""))
                db.commit()
                log.info("Migrated chat_config -> chat_agent_session")
    except Exception as e:
        log.warning(f"Migrate chat_config failed: {e}")
    # 重建 backend_agents 表（旧结构 name 主键 → 新结构 id+scope+owner_id）
    try:
        with db_lock:
            c = db.cursor()
            ba_cols = [r[1] for r in c.execute("PRAGMA table_info(backend_agents)").fetchall()]
            if "id" not in ba_cols or "scope" not in ba_cols:
                c.execute("ALTER TABLE backend_agents RENAME TO backend_agents_old")
                c.execute("""
                    CREATE TABLE backend_agents (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        name TEXT NOT NULL,
                        display_name TEXT DEFAULT '',
                        installed INTEGER DEFAULT 0,
                        cli_path TEXT DEFAULT '',
                        version TEXT DEFAULT '',
                        healthy INTEGER DEFAULT 0,
                        pip_package TEXT DEFAULT '',
                        scope TEXT DEFAULT 'global',
                        owner_id TEXT DEFAULT '',
                        updated_at TEXT DEFAULT (datetime('now')),
                        UNIQUE (name, scope, owner_id)
                    )
                """)
                # 迁移旧数据
                try:
                    c.execute("SELECT name, display_name, installed, cli_path, version, healthy, pip_package, updated_at FROM backend_agents_old")
                    for r in c.fetchall():
                        c.execute("INSERT OR IGNORE INTO backend_agents (name, display_name, installed, cli_path, version, healthy, pip_package, scope, owner_id, updated_at) VALUES (?,?,?,?,?,?,?,'global','',?)",
                                  (r[0], r[1], r[2], r[3], r[4], r[5], r[6], r[7]))
                except Exception:
                    pass
                c.execute("DROP TABLE backend_agents_old")
                db.commit()
                log.info("Rebuilt backend_agents table (id+scope+owner_id)")
    except Exception as e:
        log.warning(f"Rebuild backend_agents failed: {e}")
    # 迁移 backend → backend_id（引用 backend_agents.id，预置默认 backend）
    try:
        with db_lock:
            c = db.cursor()
            from src.user.db import ensure_default_backend, get_backend_id_by_name
            # 预置 opencode 默认 backend
            default_id = ensure_default_backend("opencode") or 0
            # devops_agent_session
            cols = [r[1] for r in c.execute("PRAGMA table_info(devops_agent_session)").fetchall()]
            if "backend" in cols and "backend_id" in cols:
                c.execute("SELECT DISTINCT backend FROM devops_agent_session WHERE backend!='' AND backend IS NOT NULL")
                for r in c.fetchall():
                    bid = get_backend_id_by_name(r[0]) or ensure_default_backend(r[0]) or default_id
                    c.execute("UPDATE devops_agent_session SET backend_id=? WHERE backend=?", (bid, r[0]))
                c.execute("UPDATE devops_agent_session SET backend_id=? WHERE (backend='' OR backend IS NULL) AND backend_id=0", (default_id,))
            # agent_type
            acols = [r[1] for r in c.execute("PRAGMA table_info(agent_type)").fetchall()]
            if "default_backend" in acols and "default_backend_id" in acols:
                c.execute("SELECT DISTINCT default_backend FROM agent_type WHERE default_backend!='' AND default_backend IS NOT NULL")
                for r in c.fetchall():
                    bid = get_backend_id_by_name(r[0]) or ensure_default_backend(r[0]) or default_id
                    c.execute("UPDATE agent_type SET default_backend_id=? WHERE default_backend=?", (bid, r[0]))
                c.execute("UPDATE agent_type SET default_backend_id=? WHERE (default_backend='' OR default_backend IS NULL) AND default_backend_id=0", (default_id,))
            # chat_agent_session
            ccols = [r[1] for r in c.execute("PRAGMA table_info(chat_agent_session)").fetchall()]
            if "backend" in ccols and "backend_id" in ccols:
                c.execute("SELECT DISTINCT backend FROM chat_agent_session WHERE backend!='' AND backend IS NOT NULL")
                for r in c.fetchall():
                    bid = get_backend_id_by_name(r[0]) or ensure_default_backend(r[0]) or default_id
                    c.execute("UPDATE chat_agent_session SET backend_id=? WHERE backend=?", (bid, r[0]))
                c.execute("UPDATE chat_agent_session SET backend_id=? WHERE (backend='' OR backend IS NULL) AND backend_id=0", (default_id,))
            db.commit()
            log.info("Migrated backend -> backend_id across agent tables")
    except Exception as e:
        log.warning(f"Migrate backend->backend_id failed: {e}")
    # 同步 skill 归属：从 skill-description.json 更新 skill 表 agent_type（确保 pr→qa 等归属一致）
    try:
        _skill_desc_path = os.path.join(_CONFIG_DIR, "skill-description.json")
        if os.path.exists(_skill_desc_path):
            with open(_skill_desc_path, "r", encoding="utf-8") as _f:
                _skill_desc = json.load(_f)
            with db_lock:
                c = db.cursor()
                _updated = 0
                for _sn, _info in _skill_desc.items():
                    _at = (_info or {}).get("agent_type", "")
                    if _sn and _at:
                        _cur = c.execute("SELECT COUNT(*) FROM skill WHERE skill_name=?", (_sn,)).fetchone()
                        if _cur and _cur[0] > 0:
                            c.execute("UPDATE skill SET agent_type=? WHERE skill_name=?", (_at, _sn))
                            _updated += c.rowcount
                db.commit()
                if _updated:
                    log.info(f"Synced {_updated} skill agent_type mappings from skill-description.json")
    except Exception as e:
        log.warning(f"Sync skill agent_type failed: {e}")
    # backend_agents 大小写去重（合并为小写 name，保留最小 id，更新引用）
    try:
        with db_lock:
            c = db.cursor()
            # 找大小写重复的分组
            c.execute("SELECT lower(name) AS ln FROM backend_agents GROUP BY lower(name) HAVING COUNT(*)>1")
            dup_groups = [r[0] for r in c.fetchall()]
            for ln in dup_groups:
                c.execute("SELECT id FROM backend_agents WHERE lower(name)=? ORDER BY id ASC LIMIT 1", (ln,))
                keep_id = c.fetchone()[0]
                c.execute("SELECT id, name FROM backend_agents WHERE lower(name)=? AND id!=? ORDER BY id ASC", (ln, keep_id))
                dup_rows = c.fetchall()
                for did, dname in dup_rows:
                    # 更新各表引用
                    for tbl, col in [("devops_agent_session", "backend_id"), ("agent_type", "default_backend_id"), ("chat_agent_session", "backend_id")]:
                        try:
                            c.execute(f"UPDATE {tbl} SET {col}=? WHERE {col}=?", (keep_id, did))
                        except Exception:
                            pass
                    # 合并数据后删除重复行
                    c.execute("DELETE FROM backend_agents WHERE id=?", (did,))
                    log.info(f"backend_agents dedup: merged '{dname}' (id={did}) -> id={keep_id}")
            db.commit()
    except Exception as e:
        log.warning(f"backend_agents dedup failed: {e}")
    # 清理旧表（如果存在）
    try:
        with db_lock:
            c = db.cursor()
            for tbl in ["vod_bot_config", "chat_config", "im_channels", "user_im_bindings", "agent_registry"]:
                c.execute(f"DROP TABLE IF EXISTS {tbl}")
            db.commit()
    except Exception:
        pass
    # 迁移 repo_im_group_binding 列规范化：chat_id→im_chat_id，account→im_app_id（旧 feishu_bindings 遗留）
    try:
        with db_lock:
            c = db.cursor()
            _tables = [r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()]
            if "repo_im_group_binding" in _tables:
                bcols = [r[1] for r in c.execute("PRAGMA table_info(repo_im_group_binding)").fetchall()]
                # account → im_app_id（补空）
                if "account" in bcols and "im_app_id" in bcols:
                    c.execute("UPDATE repo_im_group_binding SET im_app_id=account WHERE (im_app_id IS NULL OR im_app_id='') AND account IS NOT NULL AND account!=''")
                    c.execute("ALTER TABLE repo_im_group_binding DROP COLUMN account")
                    db.commit()
                    log.info("Migrated repo_im_group_binding: account -> im_app_id (dropped account)")
                # chat_id → im_chat_id
                bcols2 = [r[1] for r in c.execute("PRAGMA table_info(repo_im_group_binding)").fetchall()]
                if "chat_id" in bcols2 and "im_chat_id" not in bcols2:
                    c.execute("ALTER TABLE repo_im_group_binding RENAME COLUMN chat_id TO im_chat_id")
                    db.commit()
                    log.info("Migrated repo_im_group_binding: chat_id -> im_chat_id")
    except Exception as e:
        log.warning(f"Migrate repo_im_group_binding columns failed: {e}")
    # 迁移 repo_im_group_binding 的 im_app_id='default' → Chat Bot app_id（清理 default 兜底账户）
    try:
        with db_lock:
            c = db.cursor()
            _tables = [r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()]
            if "repo_im_group_binding" in _tables:
                c.execute("SELECT COUNT(*) FROM repo_im_group_binding WHERE im_app_id='default' OR im_app_id IS NULL OR im_app_id=''")
                cnt = c.fetchone()[0]
                if cnt > 0:
                    c.execute("SELECT bot_app_id FROM global_im_bot_config WHERE bot_type='chat_bot' LIMIT 1")
                    row = c.fetchone()
                    if row and row[0]:
                        c.execute("UPDATE repo_im_group_binding SET im_app_id=? WHERE im_app_id='default' OR im_app_id IS NULL OR im_app_id=''", (row[0],))
                        db.commit()
                        log.info(f"Migrated {cnt} repo_im_group_binding im_app_id='default' -> {row[0]}")
    except Exception as e:
        log.warning(f"Migrate repo_im_group_binding im_app_id failed: {e}")
    # 清理历史 bot_username 占位值：'devops-bot' → 全局 bot 实际用户名（configs/global-bot-token.json）
    try:
        import json as _gbt_json, os as _gbt_os
        _gbt_path = _gbt_os.path.join(_CONFIG_DIR, "global-bot-token.json")
        _global_bot_username = ""
        if _gbt_os.path.exists(_gbt_path):
            with open(_gbt_path, "r", encoding="utf-8") as _f:
                _gbt_data = _gbt_json.load(_f)
            _us = _gbt_data.get("bot_usernames") or []
            _global_bot_username = _us[0] if _us else ""
        if _global_bot_username:
            with db_lock:
                c = db.cursor()
                # 历史占位值 'devops-bot'（非实际 GitCode 用户名）→ 替换为全局 bot 用户名
                c.execute("UPDATE managed_repos SET bot_username=? WHERE bot_username='devops-bot' OR bot_username='' OR bot_username IS NULL",
                          (_global_bot_username,))
                db.commit()
                log.info(f"Migrated managed_repos.bot_username legacy placeholder -> {_global_bot_username} ({c.rowcount} rows)")
    except Exception as e:
        log.warning(f"Migrate bot_username placeholder failed: {e}")
    # 首次运行加载配置：从 global-bot-token.json 将 OAuth 配置写入 meta 表（仅 meta 无该 key 时）
    # 兼容旧版：若存在 user_accounts default 账户，仍尝试从其中迁移 OAuth 配置到 meta 表
    try:
        import json as _cfg_json, os as _cfg_os
        _cfg_path = _cfg_os.path.join(_CONFIG_DIR, "global-bot-token.json")
        _cfg_data = {}
        if _cfg_os.path.exists(_cfg_path):
            with open(_cfg_path, "r", encoding="utf-8") as _f:
                _cfg_data = _cfg_json.load(_f) or {}
        _oauth_keys = [
            ("gitcode_oauth_client_id", "oauth_gitcode_client_id"),
            ("gitcode_oauth_client_secret", "oauth_gitcode_client_secret"),
            ("gitcode_oauth_redirect_uri", "oauth_gitcode_redirect_uri"),
        ]
        with db_lock:
            c = db.cursor()
            migrated = False
            # 1. 优先从 global-bot-token.json 首次加载
            for src_key, meta_key in _oauth_keys:
                val = _cfg_data.get(src_key, "") or ""
                if val:
                    c.execute("SELECT COUNT(*) FROM meta WHERE key=?", (meta_key,))
                    if c.fetchone()[0] == 0:
                        c.execute("INSERT OR REPLACE INTO meta (key, value) VALUES (?,?)", (meta_key, val))
                        migrated = True
            # 2. 兼容旧版 default 账户（OAuth 配置曾存于 user_accounts）
            c.execute("SELECT * FROM user_accounts WHERE username='default'")
            row = c.fetchone()
            if row:
                cols = [d[0] for d in c.description]
                acc = dict(zip(cols, row))
                for src_key, meta_key in _oauth_keys:
                    val = acc.get(src_key, "") or ""
                    if val:
                        c.execute("SELECT COUNT(*) FROM meta WHERE key=?", (meta_key,))
                        if c.fetchone()[0] == 0:
                            c.execute("INSERT OR REPLACE INTO meta (key, value) VALUES (?,?)", (meta_key, val))
                            migrated = True
                # 3. 删除 default 账户（OAuth 已收敛到 meta 表）
                c.execute("DELETE FROM user_accounts WHERE username='default'")
            db.commit()
            if migrated:
                log.info("Loaded OAuth config (gitcode client id/secret) from global-bot-token.json into meta table")
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
                c.execute("INSERT OR IGNORE INTO devops_agent_session (id, repo_key, agent_type, backend, skill, oc_session_id, repo_gitcode_token, status, created_at, last_active_at, im_provider, im_app_id, im_app_secret) SELECT id, repo_key, agent_type, backend, skill, oc_session_id, repo_gitcode_token, status, created_at, last_active_at, im_provider, im_app_id, im_app_secret FROM agent_sessions")
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
    # 迁移 devops_agent_session：vod_bot_app_id → im_app_id（通用化，保留 im_* 通用字段）
    try:
        with db_lock:
            c = db.cursor()
            cols = [r[1] for r in c.execute("PRAGMA table_info(devops_agent_session)").fetchall()]
            if "vod_bot_app_id" in cols:
                # 1. vod_bot_app_id → im_app_id + im_provider='feishu' + im_app_secret 补齐
                c.execute("UPDATE devops_agent_session SET im_app_id=vod_bot_app_id, im_provider='feishu' WHERE vod_bot_app_id!='' AND vod_bot_app_id IS NOT NULL")
                _tables2 = [r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()]
                if "im_bot_config" in _tables2:
                    c.execute("UPDATE devops_agent_session SET im_app_secret=(SELECT bot_app_secret FROM im_bot_config WHERE bot_app_id=devops_agent_session.im_app_id) WHERE im_app_id!='' AND im_app_secret=''")
                db.commit()
                # 2. 删除 vod_bot_app_id 列
                try:
                    c.execute("ALTER TABLE devops_agent_session DROP COLUMN vod_bot_app_id")
                    db.commit()
                    log.info("Migrated devops_agent_session vod_bot_app_id -> im_app_id, dropped vod_bot_app_id")
                except Exception:
                    log.warning("DROP COLUMN vod_bot_app_id failed (may need rebuild)")
    except Exception as e:
        log.warning(f"Migrate vod_bot_app_id -> im_app_id failed: {e}")
    # 迁移 devops_agent_session.oc_session_id → backend_session_id
    try:
        with db_lock:
            c = db.cursor()
            cols = [r[1] for r in c.execute("PRAGMA table_info(devops_agent_session)").fetchall()]
            if "oc_session_id" in cols and "backend_session_id" in cols:
                c.execute("UPDATE devops_agent_session SET backend_session_id=oc_session_id WHERE backend_session_id='' AND oc_session_id!=''")
                db.commit()
                log.info("Migrated devops_agent_session oc_session_id -> backend_session_id")
    except Exception as e:
        log.warning(f"Migrate devops_agent_session oc_session_id failed: {e}")
    # 迁移 vod_conversations 字段（feishu_chat_id→im_chat_id, oc_session_id→backend_session_id）
    try:
        with db_lock:
            c = db.cursor()
            cols = [r[1] for r in c.execute("PRAGMA table_info(vod_conversations)").fetchall()]
            if "feishu_chat_id" in cols:
                try:
                    c.execute("ALTER TABLE vod_conversations RENAME COLUMN feishu_chat_id TO im_chat_id")
                except Exception:
                    pass
            if "feishu_chat_name" in cols:
                try:
                    c.execute("ALTER TABLE vod_conversations RENAME COLUMN feishu_chat_name TO im_channel_name")
                except Exception:
                    pass
            if "oc_session_id" in cols:
                try:
                    c.execute("ALTER TABLE vod_conversations RENAME COLUMN oc_session_id TO backend_session_id")
                except Exception:
                    pass
            cols = [r[1] for r in c.execute("PRAGMA table_info(vod_conversations)").fetchall()]
            if "im_provider" not in cols:
                c.execute("ALTER TABLE vod_conversations ADD COLUMN im_provider TEXT DEFAULT 'feishu'")
            db.commit()
            log.info("Migrated vod_conversations fields to im_* / backend_session_id")
    except Exception as e:
        log.warning(f"Migrate vod_conversations fields failed: {e}")
    # 迁移 chat_conversations.session_id → backend_session_id
    try:
        with db_lock:
            c = db.cursor()
            cols = [r[1] for r in c.execute("PRAGMA table_info(chat_conversations)").fetchall()]
            if "session_id" in cols and "backend_session_id" in cols:
                c.execute("UPDATE chat_conversations SET backend_session_id=session_id WHERE backend_session_id='' AND session_id!=''")
                db.commit()
                log.info("Migrated chat_conversations session_id -> backend_session_id")
    except Exception as e:
        log.warning(f"Migrate chat_conversations session_id failed: {e}")
    # 迁移 devops_agent_session / chat_agent_session：skill_name(TEXT) → skill_id(INTEGER)
    # 按 skill_name 匹配 skill 表（official 优先）拿 id，匹配不到则保留 skill_name（兼容旧数据）
    try:
        with db_lock:
            c = db.cursor()
            for _tbl in ("devops_agent_session", "chat_agent_session"):
                _cols = [r[1] for r in c.execute(f"PRAGMA table_info({_tbl})").fetchall()]
                if "skill" not in _cols or "skill_id" not in _cols:
                    continue
                # 先构建 skill_name → id 映射（official 优先）
                c.execute("SELECT id, skill_name FROM skill WHERE scope='official'")
                _name2id = {r[1]: r[0] for r in c.fetchall()}
                c.execute(f"SELECT id, skill FROM {_tbl} WHERE skill IS NOT NULL AND skill!='' AND (skill_id IS NULL OR skill_id=0)")
                _rows = c.fetchall()
                _updated = 0
                for _sid, _sname in _rows:
                    _sname = (_sname or "").strip()
                    _id = _name2id.get(_sname)
                    if _id:
                        c.execute(f"UPDATE {_tbl} SET skill_id=? WHERE id=?", (_id, _sid))
                        _updated += 1
                db.commit()
                if _updated:
                    log.info(f"Migrated {_tbl}.skill_name -> skill_id ({_updated} rows)")
    except Exception as e:
        log.warning(f"Migrate skill_name -> skill_id failed: {e}")
    # ===== new-version2 M0：backend_agents → agent_runtimes（术语重构，须在所有 backend_agents 历史迁移之后）=====
    try:
        with db_lock:
            c = db.cursor()
            _tables = [r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()]
            if "backend_agents" in _tables and "agent_runtimes" in _tables:
                # 双表并存：老库升级时 _create_tables 已建空 agent_runtimes → 搬迁数据后删旧表
                _copy_backend_to_runtime(c)
                c.execute("DROP TABLE backend_agents")
                db.commit()
                log.info("Migrated backend_agents -> agent_runtimes (data copied, old dropped)")
            elif "backend_agents" in _tables and "agent_runtimes" not in _tables:
                # 仅有旧表：直接 rename 保数据（理论上不发生，_create_tables 总建新表；防御分支）
                c.execute("ALTER TABLE backend_agents RENAME TO agent_runtimes")
                _ensure_column(c, "agent_runtimes", "credential_id", "INTEGER DEFAULT 0")
                _ensure_column(c, "agent_runtimes", "provider", "TEXT DEFAULT ''")
                _ensure_column(c, "agent_runtimes", "binary", "TEXT DEFAULT ''")
                _ensure_column(c, "agent_runtimes", "status", "TEXT DEFAULT 'offline'")
                _ensure_column(c, "agent_runtimes", "last_seen_at", "TEXT DEFAULT NULL")
                db.commit()
                log.info("Migrated backend_agents -> agent_runtimes (rename)")
    except Exception as e:
        log.warning(f"Migrate backend_agents -> agent_runtimes failed: {e}")
    # ===== new-version2 M0：引用列改名（SQLite 3.25+ RENAME COLUMN，幂等）=====
    try:
        with db_lock:
            c = db.cursor()
            _tables = [r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()]
            _rename_map = {
                "devops_agent_session": [("backend_id", "runtime_id"), ("backend_session_id", "runtime_session_id")],
                "agent_type": [("default_backend_id", "default_runtime_id")],
                "chat_agent_session": [("backend_id", "runtime_id"), ("backend_session_id", "runtime_session_id"), ("backend", "runtime")],
                "chat_conversations": [("backend_session_id", "runtime_session_id")],
                "vod_conversations": [("backend_session_id", "runtime_session_id")],
                "events": [("backend", "runtime")],
            }
            for tbl, cols in _rename_map.items():
                if tbl not in _tables:
                    continue
                for old, new in cols:
                    _rename_column_if_exists(c, tbl, old, new)
            db.commit()
            log.info("Renamed backend* -> runtime* columns (M0)")
    except Exception as e:
        log.warning(f"Rename backend* -> runtime* columns failed: {e}")
    # ===== new-version2：Agent 启用开关 enable（见 agent-enable-toggle-design.md）=====
    # 仅加列；存量会话对齐在 seed 之后执行（_align_agent_enable），因为对齐依赖 agent_type.enable 已由 seed 写入
    try:
        with db_lock:
            c = db.cursor()
            _ensure_column(c, "agent_type", "enable", "INTEGER DEFAULT 1")
            _ensure_column(c, "devops_agent_session", "enable", "INTEGER DEFAULT 1")
            db.commit()
            log.info("Added enable column to agent_type / devops_agent_session")
    except Exception as e:
        log.warning(f"Add enable column failed: {e}")
    # ===== new-version2 多 Issue 隔离（见 multi-issue-isolation-design.md）：agent_tasks 新列 + issue_agent_session 表 =====
    # 表由 _create_tables 建；此处仅幂等补列（老库升级）
    try:
        with db_lock:
            c = db.cursor()
            _ensure_column(c, "agent_tasks", "issue_session_id", "TEXT DEFAULT ''")
            _ensure_column(c, "agent_tasks", "project_info_files", "TEXT DEFAULT '{}'")
            _ensure_column(c, "agent_tasks", "project_info_meta", "TEXT DEFAULT '{}'")
            db.commit()
            log.info("Added issue_session_id / project_info_* columns to agent_tasks")
    except Exception as e:
        log.warning(f"Add agent_tasks issue isolation columns failed: {e}")
    # ===== 修复 chat_agent_session 陈旧 UNIQUE(repo_key) 约束（多用户 chat 配置互斥顶掉，见 session_manager 注释）=====
    try:
        with db_lock:
            c = db.cursor()
            if _drop_chat_session_repo_unique(c):
                db.commit()
                log.info("Rebuilt chat_agent_session: removed stale UNIQUE(repo_key) constraint")
    except Exception as e:
        log.warning(f"Drop chat_agent_session UNIQUE(repo_key) failed: {e}")
    # ===== multi-runtime-instance-design §7：agent_runtimes 多实例唯一键 + runtime_credentials.last_ip =====
    # 注册键 (name, scope, owner_id) → (credential_id, name, scope, owner_id)，使多 worker 同名引擎并存。
    try:
        with db_lock:
            c = db.cursor()
            _tables = [r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()]
            if "runtime_credentials" in _tables:
                _ensure_column(c, "runtime_credentials", "last_ip", "TEXT DEFAULT ''")
                _ensure_column(c, "runtime_credentials", "owner_name", "TEXT DEFAULT ''")
                # 凭据名全局唯一：先清重（同名多行仅保留最小 id），再建唯一索引
                c.execute("""
                    DELETE FROM runtime_credentials WHERE id NOT IN (
                        SELECT MIN(id) FROM runtime_credentials GROUP BY name
                    )
                """)
                c.execute("CREATE UNIQUE INDEX IF NOT EXISTS uq_runtime_credentials_name ON runtime_credentials(name)")
                db.commit()
            if "agent_runtimes" in _tables:
                _rebuilt = _rebuild_agent_runtimes_inst_key(c)
                if _rebuilt:
                    log.info("Rebuilt agent_runtimes: unique key -> (credential_id, name, scope, owner_id)")
            db.commit()
    except Exception as e:
        log.warning(f"Migrate agent_runtimes instance unique key failed: {e}")
