"""数据库初始化:新增 events,agent_sessions,配置表 + JSON 种子数据导入"""
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
    agent_logs MEDIUMTEXT DEFAULT '[]',
    result MEDIUMTEXT DEFAULT '',
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
    subscribe_events MEDIUMTEXT DEFAULT '[]',
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
    file_path VARCHAR(512) DEFAULT '',
    scope TEXT DEFAULT 'official',
    owner_id TEXT DEFAULT '',
    created_by VARCHAR(64) DEFAULT '',
    updated_by VARCHAR(64) DEFAULT '',
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
    subscribe_events MEDIUMTEXT DEFAULT '[]',
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

# new-version2:backend_agents -> agent_runtimes(术语重构,见 agent-runtime-design.md §5.2)
# 多实例键(multi-runtime-instance-design §2.1):注册键 = (credential_id, name, scope, owner_id),
# 使每台 worker(凭据)对每个引擎独占一行,同时在线.老库由迁移重建(_rebuild_agent_runtimes_inst_key).
_AGENT_RUNTIMES_DDL = """
CREATE TABLE IF NOT EXISTS agent_runtimes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    credential_id INTEGER DEFAULT 0,
    name TEXT NOT NULL,
    display_name TEXT DEFAULT '',
    provider TEXT DEFAULT '',
    `binary` TEXT DEFAULT '',
    installed INTEGER DEFAULT 0,
    cli_path TEXT DEFAULT '',
    version TEXT DEFAULT '',
    healthy INTEGER DEFAULT 0,
    pip_package TEXT DEFAULT '',
    status TEXT DEFAULT 'offline',
    last_seen_at TEXT DEFAULT NULL,
    scope TEXT DEFAULT 'global',
    owner_id TEXT DEFAULT '',
    local_skills TEXT DEFAULT '[]',
    pending_skill_scan INTEGER DEFAULT 0,
    skill_scan_result TEXT DEFAULT '',
    agent_tags TEXT DEFAULT '[]',
    repo_key TEXT DEFAULT NULL,          -- 关联项目 repo_key（注册时从凭据复制）
    worker_version TEXT DEFAULT '',      -- worker 自报版本号（心跳/兼容性检测，见 knowledge-obs-design）
    host_name VARCHAR(191) DEFAULT '',   -- 本机直连引擎归属主控机 hostname（credential_id=0 时区分多台主控）
    updated_at TEXT DEFAULT (datetime('now')),
    UNIQUE (credential_id, name, scope, owner_id, host_name)
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
    content MEDIUMTEXT NOT NULL,
    created_at TEXT DEFAULT (datetime('now')),
    task_id TEXT,
    generate_status INTEGER DEFAULT NULL
)
"""

_GLOBAL_IM_BOT_CONFIG_DDL = """
CREATE TABLE IF NOT EXISTS global_im_bot_config (
    bot_app_id        VARCHAR(191) PRIMARY KEY,
    bot_type          VARCHAR(32) NOT NULL,
    im_provider       VARCHAR(32) DEFAULT 'feishu',
    is_active         INTEGER DEFAULT 1,
    bot_name          VARCHAR(128) DEFAULT '',
    lease_token       VARCHAR(128) DEFAULT NULL,
    lease_expires_at  DATETIME DEFAULT NULL,
    lease_owner       VARCHAR(128) DEFAULT NULL,
    lease_acquired_at DATETIME DEFAULT NULL,
    created_at        DATETIME DEFAULT CURRENT_TIMESTAMP,
    updated_at        DATETIME DEFAULT CURRENT_TIMESTAMP
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

_FEISHU_ONBOARD_TASK_DDL = """
CREATE TABLE IF NOT EXISTS feishu_onboard_task (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id TEXT UNIQUE NOT NULL,
    bot_type TEXT NOT NULL,
    repo_key TEXT DEFAULT '',
    user_id INTEGER NOT NULL,
    status TEXT DEFAULT 'pending',
    app_id TEXT DEFAULT '',
    app_secret TEXT DEFAULT '',
    qr_url TEXT DEFAULT '',
    expire_in INTEGER DEFAULT 0,
    error TEXT DEFAULT '',
    created_at TEXT DEFAULT (datetime('now')),
    finished_at TEXT DEFAULT ''
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

# ===== new-version2 M1:agent_tasks 任务队列(确定路由 + 事件档案快照)=====
# 见 agent-runtime-event-full-design.md §4.3 / concurrency-design.md / skill-delivery-remote.md
_AGENT_TASKS_DDL = """
CREATE TABLE IF NOT EXISTS agent_tasks (
    id                TEXT PRIMARY KEY,
    agent_session_id  TEXT NOT NULL,        -- 外键 -> devops_agent_session.id
    runtime_id        INTEGER NOT NULL,     -- 确定路由 -> agent_runtimes.id
    event_seq         INTEGER,              -- 关联 events 表 seq
    issue_id          TEXT DEFAULT '',      -- 关联 event.json pipeline 段
    repo_key          TEXT DEFAULT '',
    agent_type        TEXT DEFAULT '',
    skill             TEXT DEFAULT '',
    prompt            MEDIUMTEXT NOT NULL,
    cwd               TEXT DEFAULT '',
    runtime_session_id TEXT DEFAULT '',
    event_type        TEXT DEFAULT '',
    account           TEXT DEFAULT '',
    user_id           INTEGER DEFAULT 0,
    repo_gitcode_token TEXT DEFAULT '',
    env_extra         MEDIUMTEXT DEFAULT '{}',    -- JSON(含服务端已注入的账号/用户凭据,见 skill-delivery-remote.md)
    skill_files       MEDIUMTEXT,    -- JSON: {skill_name: {rel_path: content}},远端交付技能包(MySQL 不允许 MEDIUMTEXT 默认值--无默认)
    server_url        TEXT DEFAULT '',      -- A2A 外呼地址,worker 替换 {{SERVER_URL}}
    issue_session_id  TEXT DEFAULT '',      -- 外键 -> issue_agent_session.id(Issue 级执行会话,串行粒度,见 multi-issue-isolation-design.md §3.3)
    project_info_files MEDIUMTEXT DEFAULT '{}',   -- JSON: {rel_path: content},repo 全局 project-info 随任务交付(§2.4)
    project_info_meta  MEDIUMTEXT DEFAULT '{}',   -- JSON: {last_commit_sha, branches, last_updated, degraded}
    knowledge_files    MEDIUMTEXT DEFAULT '{}',   -- JSON: {rel_path: content},共享知识快照交付(event-conversation-design.md §3.1.1)
    project_info_file_urls TEXT DEFAULT '',       -- project-info 文件的 OBS 临时 URL 列表（JSON 数组，见 knowledge-obs-design）
    knowledge_file_urls    TEXT DEFAULT '',       -- 知识文件的 OBS 临时 URL 列表（JSON 数组，见 knowledge-obs-design）
    pool_engine_ids    TEXT DEFAULT '',           -- 限定广播引擎列表（JSON 数组如[76,77]，空=全量广播）
    status            TEXT DEFAULT 'queued', -- queued/dispatched/running/completed/failed/cancelled
    exit_code         INTEGER DEFAULT NULL,
    result_text       MEDIUMTEXT DEFAULT '',
    errors            MEDIUMTEXT DEFAULT '',
    stderr            MEDIUMTEXT DEFAULT '',
    usage_json        MEDIUMTEXT DEFAULT '{}',
    started_at        TEXT DEFAULT NULL,
    completed_at      TEXT DEFAULT NULL,
    created_at        TEXT DEFAULT (datetime('now'))
)
"""

# ===== 运行日志:task_messages 结构化消息表(实时日志功能,对齐 multica TaskMessage 模型)=====
_TASK_MESSAGES_DDL = """
CREATE TABLE IF NOT EXISTS task_messages (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id    TEXT NOT NULL,           -- agent_tasks.id
    seq        INTEGER NOT NULL,        -- worker 侧单调序号(顺序/去重/增量拉取三合一)
    type       TEXT NOT NULL,           -- step_start|text|reasoning|tool_use|step_finish|error|done
    tool       TEXT DEFAULT '',         -- tool_use 工具名
    content    MEDIUMTEXT DEFAULT '',         -- text/reasoning/error 内容
    input_json MEDIUMTEXT DEFAULT '',         -- tool_use 入参 JSON(服务端已脱敏)
    output     TEXT DEFAULT '',         -- tool_result 输出(worker 截断 8192)
    created_at TEXT DEFAULT (datetime('now')),
    UNIQUE(task_id, seq)
)
"""

# ===== agent-cluster-design.md §4.1:Agent 类型 -> 引擎池绑定(1..N)=====
_AGENT_TYPE_RUNTIME_POOL_DDL = """
CREATE TABLE IF NOT EXISTS agent_type_runtime_pool (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    agent_type   TEXT NOT NULL,              -- vod / issue / coding / qa / sre
    runtime_id   INTEGER NOT NULL,           -- agent_runtimes.id(Worker 上的引擎实例)
    weight       INTEGER NOT NULL DEFAULT 1, -- 调度权重(≥1)
    priority     INTEGER NOT NULL DEFAULT 5, -- 池内优先级(1 高)
    enabled      INTEGER NOT NULL DEFAULT 1, -- 软摘除(保留绑定但不参与调度)
    remark       TEXT DEFAULT '',
    created_at   TEXT DEFAULT (datetime('now')),
    UNIQUE (agent_type, runtime_id)
)
"""

# ===== new-version2 M3:runtime_credentials 独立凭据(rta_ token,见 agent-runtime-event-full-design.md §4.1)=====
_RUNTIME_CREDENTIALS_DDL = """
CREATE TABLE IF NOT EXISTS runtime_credentials (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    token_hash   TEXT NOT NULL,             -- sha256(rta_xxx),不存明文
    name         TEXT NOT NULL UNIQUE,      -- 凭据名=worker 名,全局唯一(可改名)
    device_name  TEXT DEFAULT '',           -- 首次注册时回填(边端自报,辅助展示)
    scope        TEXT DEFAULT 'user',       -- user(个人)/repo(仓库)/global(全局),默认个人
    owner_id     TEXT DEFAULT '',           -- 仓库 scope 时存 repo 全名(如 owner/repo);个人/全局为 ''
    owner_name   TEXT DEFAULT '',           -- 归属用户 user_name(生成凭据的当前登录用户)
    repo_key     TEXT DEFAULT NULL,         -- 关联项目 repo_key（scope=user 时存）
    status       TEXT DEFAULT 'active',     -- active / revoked
    last_seen_at TEXT DEFAULT NULL,
    last_ip      TEXT DEFAULT '',           -- 最近注册/心跳来源 IP(展示用,不入唯一键)
    created_at   TEXT DEFAULT (datetime('now')),
    UNIQUE (token_hash)
)
"""

# ===== new-version2 VOD:Issue 生命周期状态机(权威阶段,见 vod-skill-upgrade-plan.md §3.0.2)=====
_ISSUE_STATE_DDL = """
CREATE TABLE IF NOT EXISTS issue_state (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    repo_key     TEXT NOT NULL,
    issue_id     TEXT NOT NULL,             -- 如 "#5"
    stage        TEXT NOT NULL DEFAULT 'vod',   -- vod/issue/coding/qa/sre/done
    owner_agent  TEXT NOT NULL DEFAULT 'vod',   -- 当前归属 agent(= 最新被交办者)
    assignee     TEXT DEFAULT '',          -- Issue 责任人(GitCode assignee,可为 grape_bot 或用户)
    a2a_seq      INTEGER,                   -- 触发当前 stage 的 A2A 事件 seq
    scene_id     TEXT DEFAULT '',          -- Issue 级开发场景（VOD 判定，空=回退仓库默认）
    parent_issue_id TEXT DEFAULT '',      -- 父任务 Issue ID（如 "#20"，空=普通任务）
    is_subtask_mode INTEGER DEFAULT 0,    -- 1=多任务模式（Issue Agent 应拆分子任务）
    scene_user_id INTEGER DEFAULT 0,       -- 用户级场景的创建者 ID（0=非用户级）
    updated_at   TEXT DEFAULT (datetime('now')),
    created_at   TEXT DEFAULT (datetime('now')),
    UNIQUE (repo_key, issue_id)
)
"""

# ===== new-version2 Issue 级执行会话(多 Issue 并行,见 multi-issue-isolation-design.md §3.3)=====
# 配置层(devops_agent_session,repo+agent)与执行层(issue_agent_session,repo+agent+issue)分离.
_ISSUE_AGENT_SESSION_DDL = """
CREATE TABLE IF NOT EXISTS issue_agent_session (
    id                TEXT PRIMARY KEY,          -- uuid,agent_tasks.issue_session_id 外键
    repo_key          TEXT NOT NULL,
    agent_type        TEXT NOT NULL,
    issue_id          TEXT NOT NULL,             -- 如 "#5"(对应 event.json 的 issue_num)
    runtime_session_id TEXT DEFAULT '',          -- opencode --session 续接指针(Issue 级)
    status            TEXT DEFAULT 'idle',       -- idle / running / failed
    last_active_at    TEXT DEFAULT (datetime('now')),
    created_at        TEXT DEFAULT (datetime('now')),
    UNIQUE (repo_key, agent_type, issue_id)
)
"""

# new-version2 Event 对话(event-conversation-design.md):以事件为锚点,与处理该事件的 agent 对话
_EVENT_THREAD_DDL = """
CREATE TABLE IF NOT EXISTS event_thread (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    anchor_seq  INTEGER NOT NULL,           -- 锚点事件 seq(对话对象 = 处理该事件的 agent)
    repo_key    TEXT NOT NULL,
    issue_id    TEXT DEFAULT '',            -- 关联 Issue(如 '#5';无关联事件为 '')
    agent_type  TEXT NOT NULL,              -- vod/issue/coding/qa/sre
    role        TEXT NOT NULL,              -- user / agent
    content     MEDIUMTEXT NOT NULL,
    created_by  TEXT DEFAULT '',
    created_at  TEXT DEFAULT (datetime('now')),
    task_id     TEXT DEFAULT '',            -- 对话任务 ID（持久化查询，对齐 Chat Agent）
    generate_status INTEGER DEFAULT NULL    -- 1=生成中 2=成功 3=失败 4=中断
)
"""

# 共享知识索引:正文=工作区文件(project-info/knowledge.md,issues/{n}/knowledge.md,
# {agent}/issues/{n}/knowledge.md),本表仅存索引/状态/审计(见 event-conversation-design.md §3.1.1)
_AGENT_SHARED_KNOWLEDGE_DDL = """
CREATE TABLE IF NOT EXISTS agent_shared_knowledge (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    scope       TEXT NOT NULL,              -- project / issue / issue_agent
    repo_key    TEXT NOT NULL,
    agent_type  TEXT DEFAULT '',            -- scope=issue_agent 时必填
    issue_num   INTEGER DEFAULT 0,          -- scope=issue / issue_agent 时必填
    file_path   TEXT NOT NULL,              -- 知识正文文件(相对 WORKSPACE_DIR)
    status      TEXT DEFAULT 'accepted',    -- accepted / rejected
    source_seq  INTEGER DEFAULT 0,
    created_by  TEXT DEFAULT '',
    created_at  TEXT DEFAULT (datetime('now')),
    obs_key      VARCHAR(512) DEFAULT '',    -- OBS 对象键（正文已搬入对象存储，见 knowledge-obs-design）
    content_hash VARCHAR(64) DEFAULT '',     -- 正文内容哈希（变更检测/增量同步）
    updated_at   DATETIME DEFAULT CURRENT_TIMESTAMP  -- 最近内容更新时间（OBS 同步审计）
)
"""

# ===== engine tagging (event-dispatch-redesign-v2.md 2.2) =====
_ENGINE_CAPABILITIES_DDL = """
CREATE TABLE IF NOT EXISTS engine_capabilities (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    runtime_id  INTEGER NOT NULL,
    tag         TEXT NOT NULL,
    max_concurrent INTEGER DEFAULT 1,
    priority    INTEGER DEFAULT 50,
    enabled     INTEGER DEFAULT 1,
    created_at  TEXT DEFAULT (datetime('now')),
    updated_at  TEXT DEFAULT (datetime('now')),
    UNIQUE(runtime_id, tag)
)
"""

# ===== Issue #49：Webhook 事件 action 过滤规则表（Q3 已确认可配置）=====
# run_pipeline L1 准入过滤：{event_type, action} 命中规则决定是否放行/副作用短路/过滤。
# - allow=1 且 require_changes 为空 → 放行进入阶段路由
# - allow=1 且 require_changes 非空 → 需 payload.changes 命中这些键才放行（如 edit 需 description 变更）
# - allow=0 → 过滤（写 event_skipped）
# - allow=2（副作用-only）→ 执行 handle_side_effect 后直接短路，不进入 agent（如 close）
# - actor: user / bot / any —— bot 操作（self-comment）在前置已处理，此处主要针对 user 操作细分
_EVENT_ACTION_FILTER_DDL = """
CREATE TABLE IF NOT EXISTS event_action_filter (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    event_type    TEXT NOT NULL,
    action        TEXT NOT NULL,
    allow         INTEGER NOT NULL DEFAULT 0,   -- 0=过滤 1=放行 2=副作用-only
    actor         TEXT DEFAULT 'any',           -- user / bot / any
    require_changes TEXT DEFAULT '',            -- JSON 数组，命中这些 changes 键才放行，如 ["description"]
    block_changes   TEXT DEFAULT '',            -- JSON 数组，命中这些 changes 键即过滤，如 ["assignees","labels"]
    handle_side_effect TEXT DEFAULT '',         -- 副作用标识（如 close_issue_tasks）；空=无副作用
    priority      INTEGER DEFAULT 0,
    enabled       INTEGER DEFAULT 1,
    created_at    TEXT DEFAULT (datetime('now')),
    updated_at    TEXT DEFAULT (datetime('now')),
    UNIQUE(event_type, action)
)
"""

# ===== Scene Center: 场景配置表（替代 configs/pipelines.json 运行时读取）=====
# 场景只与仓库（repo_key）关联，无 scope/user_id 来源区分（issue-39 方案 B）
# 官方场景不存于此表，由 scene_market(source='official') 承载（scene_sync 同步）
_PIPELINE_SCENE_DDL = """
CREATE TABLE IF NOT EXISTS devops_pipeline_scene (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    scene_id    TEXT NOT NULL,
    name        TEXT NOT NULL,
    description TEXT DEFAULT '',
    display_name TEXT DEFAULT '',      -- 场景展示名（如「通用开发」），空时回落 name
    repo_key    TEXT NOT NULL DEFAULT '',
    nodes_json  TEXT DEFAULT '[]',
    onboarding  TEXT DEFAULT 'repo-onboarding',
    is_active   INTEGER DEFAULT 1,
    created_at  TEXT DEFAULT (datetime('now')),
    updated_at  TEXT DEFAULT (datetime('now')),
    UNIQUE (scene_id, repo_key)
)
"""

# ===== 优选项目模板（GitCode/GitHub 云原生热门项目）=====
_FEATURED_TEMPLATES_DDL = """
CREATE TABLE IF NOT EXISTS featured_templates (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    platform    TEXT NOT NULL,
    repo_full_name TEXT NOT NULL,
    repo_name   TEXT NOT NULL,
    description TEXT DEFAULT '',
    language    TEXT DEFAULT '',
    stars       INTEGER DEFAULT 0,
    forks       INTEGER DEFAULT 0,
    topics      TEXT DEFAULT '[]',
    html_url    TEXT DEFAULT '',
    clone_url   TEXT DEFAULT '',
    category    TEXT DEFAULT 'cloud-native',
    recommend_reason TEXT DEFAULT '',
    fetched_at  TEXT DEFAULT '',
    UNIQUE(platform, repo_full_name)
)
"""

_LOGIN_WHITELIST_DDL = """
CREATE TABLE IF NOT EXISTS login_whitelist (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    gitcode_username VARCHAR(64) NOT NULL,
    remark VARCHAR(128) DEFAULT '',
    created_by VARCHAR(64) DEFAULT '',
    created_at TEXT DEFAULT (datetime('now')),
    UNIQUE (gitcode_username)
)
"""

# ===== Issue-11: DevOps 市场（场景市场 + 技能市场）======
# 技术方案：docs/new-version3/issue-11-scene-skill-market-tech-design.md 第二章

_SCENE_CATEGORY_DDL = """
CREATE TABLE IF NOT EXISTS scene_category (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT NOT NULL,
    slug        VARCHAR(64) DEFAULT '',
    icon        TEXT DEFAULT '',
    order_num   INTEGER DEFAULT 0,
    is_active   INTEGER DEFAULT 1,
    created_by  VARCHAR(64) DEFAULT '',
    updated_by  VARCHAR(64) DEFAULT '',
    created_at  TEXT DEFAULT (datetime('now')),
    updated_at  TEXT DEFAULT (datetime('now')),
    UNIQUE (name),
    UNIQUE (slug)
)
"""

_SCENE_MARKET_DDL = """
CREATE TABLE IF NOT EXISTS scene_market (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT NOT NULL,
    display_name TEXT DEFAULT '',
    version     TEXT DEFAULT '1.0.0',
    description TEXT DEFAULT '',
    market_description TEXT DEFAULT '',
    icon        TEXT DEFAULT '',
    category_id INTEGER DEFAULT 1,
    is_featured INTEGER DEFAULT 0,
    like_count  INTEGER DEFAULT 0,
    source      TEXT DEFAULT 'official',
    source_user_id INTEGER DEFAULT 0,
    recommended_template VARCHAR(191) DEFAULT '',
    nodes_json  TEXT DEFAULT '[]',
    onboarding  TEXT DEFAULT 'repo-onboarding',
    tags        VARCHAR(256) DEFAULT '',  -- 标签，逗号分隔（如 build,deploy,ci）
    author      VARCHAR(64) DEFAULT '',
    content_hash VARCHAR(64) DEFAULT '',  -- 内容 hash（定时同步增量检测用）
    obs_icon_key VARCHAR(256) DEFAULT '',  -- OBS 桶 icon 对象 key（查询时动态生成签名 URL）
    is_active   INTEGER DEFAULT 1,
    scope       VARCHAR(32) DEFAULT 'official',  -- 可见范围：official/repo
    repo_key    VARCHAR(191) DEFAULT '',  -- 仓库标识（official 为空，repo 为当前仓库）
    created_by  VARCHAR(64) DEFAULT '',
    updated_by  VARCHAR(64) DEFAULT '',
    created_at  TEXT DEFAULT (datetime('now')),
    updated_at  TEXT DEFAULT (datetime('now')),
    UNIQUE (name, scope, source_user_id, repo_key)
)
"""

_SCENE_LIKES_DDL = """
CREATE TABLE IF NOT EXISTS scene_likes (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id     INTEGER NOT NULL,
    scene_id    INTEGER NOT NULL,
    created_at  TEXT DEFAULT (datetime('now')),
    UNIQUE (user_id, scene_id)
)
"""

_SCENE_SKILL_REF_DDL = """
CREATE TABLE IF NOT EXISTS scene_skill_ref (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    scene_id    INTEGER NOT NULL,
    skill_id    INTEGER DEFAULT 0,
    skill_name  TEXT NOT NULL,
    created_at  TEXT DEFAULT (datetime('now')),
    UNIQUE (scene_id, skill_name)
)
"""

_SKILL_LIKES_DDL = """
CREATE TABLE IF NOT EXISTS skill_likes (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id     INTEGER NOT NULL,
    skill_id    INTEGER NOT NULL,
    created_at  TEXT DEFAULT (datetime('now')),
    UNIQUE (user_id, skill_id)
)
"""

_UPLOADED_FILES_DDL = """
CREATE TABLE IF NOT EXISTS uploaded_files (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    file_key        VARCHAR(1024) NOT NULL,      -- OBS 对象 key / 本地文件路径
    original_name   VARCHAR(512) DEFAULT '',     -- 用户上传时的原始文件名
    file_size       BIGINT DEFAULT 0,            -- 文件大小（字节）
    mime_type       VARCHAR(128) DEFAULT '',     -- MIME 类型（如 image/png）
    file_type       VARCHAR(32) DEFAULT '',      -- 文件用途类型（如 'scene_icon'，预留扩展）
    scope           VARCHAR(16) DEFAULT 'temp',  -- 文件状态：temp（临时）/ committed（已提交）
    ref_table       VARCHAR(64) DEFAULT '',      -- 关联业务表名（committed 时填，如 'scene_market'）
    ref_id          INTEGER DEFAULT 0,           -- 关联业务记录 ID（committed 时填）
    is_active       INTEGER DEFAULT 1,           -- 软删除标记（0=已删除）
    created_by      VARCHAR(64) DEFAULT '',      -- 上传用户
    created_at      TEXT DEFAULT (datetime('now')),
    updated_at      TEXT DEFAULT (datetime('now'))
)
"""

_DISTRIBUTED_LOCKS_DDL = """
CREATE TABLE IF NOT EXISTS distributed_locks (
    lock_key    VARCHAR(128) NOT NULL,          -- 锁唯一 key（如 'upload_cleanup'）
    holder      VARCHAR(128) NOT NULL,          -- 锁持有者标识（hostname-随机hex）
    locked_at   BIGINT       NOT NULL DEFAULT 0, -- 获取锁时间戳（秒）
    expires_at  BIGINT       NOT NULL DEFAULT 0, -- 锁过期时间戳（秒），过期可被抢占
    update_at   BIGINT       NOT NULL DEFAULT 0, -- 最后更新时间戳（排查用）
    created_at  BIGINT       NOT NULL DEFAULT 0, -- 锁记录首次创建时间（排查用）
    PRIMARY KEY (lock_key)
)
"""

_NEW_TABLES = [
    ("meta", _META_TABLE_DDL),
    ("agent_runtimes", _AGENT_RUNTIMES_DDL),
    ("agent_tasks", _AGENT_TASKS_DDL),
    ("task_messages", _TASK_MESSAGES_DDL),
    ("agent_type_runtime_pool", _AGENT_TYPE_RUNTIME_POOL_DDL),
    ("runtime_credentials", _RUNTIME_CREDENTIALS_DDL),
    ("issue_state", _ISSUE_STATE_DDL),
    ("issue_agent_session", _ISSUE_AGENT_SESSION_DDL),
    ("events", _EVENTS_TABLE_DDL),
    ("event_skipped", _EVENT_SKIPPED_DDL),
    ("engine_capabilities", _ENGINE_CAPABILITIES_DDL),
    ("event_action_filter", _EVENT_ACTION_FILTER_DDL),
    ("chat_conversations", _CHAT_CONVERSATIONS_DDL),
    ("chat_messages", _CHAT_MESSAGES_DDL),
    ("devops_agent_session", _AGENT_SESSIONS_TABLE_DDL),
    ("event_route_config", _EVENT_ROUTE_CONFIG_DDL),
    ("agent_type", _AGENT_TYPE_DDL),
    ("skill", _SKILL_DDL),
    ("global_im_bot_config", _GLOBAL_IM_BOT_CONFIG_DDL),
    ("feishu_onboard_task", _FEISHU_ONBOARD_TASK_DDL),
    ("vod_conversations", _VOD_CONVERSATIONS_DDL),
    ("tasks", _TASKS_DDL),
    ("event_thread", _EVENT_THREAD_DDL),
    ("agent_shared_knowledge", _AGENT_SHARED_KNOWLEDGE_DDL),
    ("bot_seen_messages", """
CREATE TABLE IF NOT EXISTS bot_seen_messages (
    app_id TEXT NOT NULL,
    message_id TEXT NOT NULL,
    seen_at REAL NOT NULL,
    PRIMARY KEY (app_id, message_id)
)"""),
    ("devops_pipeline_scene", _PIPELINE_SCENE_DDL),
    ("featured_templates", _FEATURED_TEMPLATES_DDL),
    ("login_whitelist", _LOGIN_WHITELIST_DDL),
    # ===== Issue-11: DevOps 市场表 =====
    ("scene_category", _SCENE_CATEGORY_DDL),
    ("scene_market", _SCENE_MARKET_DDL),
    ("scene_likes", _SCENE_LIKES_DDL),
    ("scene_skill_ref", _SCENE_SKILL_REF_DDL),
    ("skill_likes", _SKILL_LIKES_DDL),
    ("uploaded_files", _UPLOADED_FILES_DDL),
    ("distributed_locks", _DISTRIBUTED_LOCKS_DDL),
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
    """Idempotent column add: PRAGMA check if column missing then ALTER.

    Column name wrapped in backticks for MySQL compatibility (e.g. `binary` is reserved).
    """
    cols = [r[1] for r in c.execute(f"PRAGMA table_info({tbl})").fetchall()]
    if col not in cols:
        c.execute(f"ALTER TABLE {tbl} ADD COLUMN `{col}` {ddl}")


def _rename_column_if_exists(c, tbl: str, old: str, new: str):
    """幂等改名:旧列存在才处理(SQLite 3.25+).
    - 若 new 不存在:RENAME COLUMN(保数据)
    - 若 new 已存在(如 session_manager 模块级 ALTER 预建的兼容列)且 old 仍有数据:
      把 old 数据并入 new,再 DROP old,避免双列并存丢数据
    """
    cols = [r[1] for r in c.execute(f"PRAGMA table_info({tbl})").fetchall()]
    if old not in cols:
        return
    if new not in cols:
        c.execute(f"ALTER TABLE {tbl} RENAME COLUMN {old} TO {new}")
        return
    # 双列并存:按列类型归并 old -> new(new 为默认值才填 old,避免覆盖新值)
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
    """backend_agents -> agent_runtimes 数据搬迁(保留 id;若 agent_runtimes 已有同 name 行--
    如历史迁移 ensure_default_runtime 预置的空行--则用老库数据补全缺失字段,老库为权威)."""
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
            # 已存在(如 ensure 预置空行):补全缺失字段,保留其 id
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
        "CREATE INDEX IF NOT EXISTS idx_task_messages_task ON task_messages(task_id)",
        # ===== Issue-11: DevOps 市场索引 =====
        "CREATE INDEX IF NOT EXISTS idx_scene_market_category_id ON scene_market(category_id)",
        "CREATE INDEX IF NOT EXISTS idx_scene_market_is_featured ON scene_market(is_featured)",
        "CREATE INDEX IF NOT EXISTS idx_scene_market_source ON scene_market(source)",
        "CREATE INDEX IF NOT EXISTS idx_scene_market_scope ON scene_market(scope)",
        "CREATE INDEX IF NOT EXISTS idx_scene_market_repo_key ON scene_market(repo_key)",
        "CREATE INDEX IF NOT EXISTS idx_uploaded_files_scope ON uploaded_files(scope, is_active, created_at)",
        "CREATE INDEX IF NOT EXISTS idx_uploaded_files_ref ON uploaded_files(ref_table, ref_id, is_active)",
        "CREATE INDEX IF NOT EXISTS idx_scene_skill_ref_skill_name ON scene_skill_ref(skill_name)",
        "CREATE INDEX IF NOT EXISTS idx_skill_is_featured ON skill(is_featured)",
        "CREATE INDEX IF NOT EXISTS idx_market_scene_id ON devops_pipeline_scene(market_scene_id)",
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
            # 已 seed 过:skill_official 仍需幂等补种缺失技能;其余跳过
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
                # 官方技能:scope='official' 行
                c.execute("SELECT COUNT(*) FROM skill WHERE scope='official'")
            else:
                c.execute(f"SELECT COUNT(*) FROM {db_table}")
            count = c.fetchone()[0]
            if count > 0 and db_table != "agent_type":
                # 表已有数据:跳过全量 seed,但对 skill-description.json 中缺失的官方技能幂等补种
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


def _seed_event_action_filter():
    """Issue #49：Webhook 事件 action 过滤默认规则（按真实 payload 审计修正，2026-09-07）。

    allow: 0=过滤 1=放行 2=副作用-only（close）→ 不进入 agent 执行。
    审计结论：GitCode issue_updated.action 只出现 update/close/reopen；
    assign 变更 = update + changes.assignees；label 变更 = update + changes.labels；
    内容编辑 = update + changes.description/title。判定用 action + changes 组合。
    """
    default_rules = [
        # issue_updated —— 真实 action 只有 update/close/reopen
        # update：title/description 任一变更 → 内容编辑放行；纯 assignees/labels 变更 → 过滤
        ("issue_updated", "update", 1, "user", '["description","title"]', '["assignees","labels"]', ""),
        # close：副作用-only（close_issue_tasks），不进入 agent
        ("issue_updated", "close", 2, "any", "", "", "close_issue_tasks"),
        # reopen：无流水线意义
        ("issue_updated", "reopen", 0, "any", "", "", ""),
        # pull_request_opened
        ("pull_request_opened", "open",   1, "any", "", "", ""),
        # PR update 按操作者：user 有新 commit → 放行；bot 自操作过滤（self-comment 已前置拦截）
        ("pull_request_opened", "update", 1, "user", "", "", ""),
        ("pull_request_opened", "close",  0, "any", "", "", ""),
        ("pull_request_opened", "reopen", 0, "any", "", "", ""),
        ("pull_request_opened", "merge",  1, "any", "", "", ""),
        # pull_request_merged
        ("pull_request_merged", "merge",  1, "any", "", "", ""),
    ]
    with db_lock:
        c = db.cursor()
        for evt, act, allow, actor, req_changes, block_changes, side_effect in default_rules:
            try:
                c.execute(
                    "INSERT OR IGNORE INTO event_action_filter "
                    "(event_type, action, allow, actor, require_changes, block_changes, handle_side_effect, priority, enabled, created_at, updated_at) "
                    "VALUES (?,?,?,?,?,?,?,0,1,datetime('now'),datetime('now'))",
                    (evt, act, allow, actor, req_changes, block_changes, side_effect))
            except Exception as e:
                log.warning(f"event_action_filter seed failed ({evt}:{act}): {e}")
        db.commit()


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
            # default_runtime_id 不再写死 opencode(engine-selection-design.md v2 §8.2):
            # 绑定一律由 pick_engine 按 (仓库, agent) 引擎集合运行时解析,种子只留 0(未绑定).
            for agent_type, info in data.items():
                display_name = info.get("name", agent_type)
                description = info.get("description", "")
                icon = info.get("icon", "")
                order_num = info.get("order_num", 0)
                subscribe_events = json.dumps(info.get("subscribe_events", []), ensure_ascii=False)
                enable = 1 if info.get("enable", True) else 0
                c.execute(
                    "INSERT OR REPLACE INTO agent_type (agent_type, display_name, description, default_runtime_id, icon, order_num, subscribe_events, enable) VALUES (?,?,?,0,?,?,?,?)",
                    (agent_type, display_name, description, icon, order_num, subscribe_events, enable)
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
            # agent-routing.json 已废弃(agent_registry 表移除),backend 统一用 backend_agents
            pass

        db.commit()


def _migrate_whitelist_from_file():
    """首次启动：如果 login_whitelist 表为空但 login-whitelist.json 存在，导入文件中的用户名（一次性）。"""
    try:
        from src.config import LOGIN_WHITELIST_PATH
        if not os.path.exists(LOGIN_WHITELIST_PATH):
            return
        with db_lock:
            c = db.cursor()
            count = c.execute("SELECT COUNT(*) FROM login_whitelist").fetchone()[0]
            if count > 0:
                return  # 表已有数据，不重复导入
        with open(LOGIN_WHITELIST_PATH, encoding="utf-8") as f:
            data = json.load(f)
        wl = data.get("whitelist") if isinstance(data, dict) else None
        if not wl:
            return
        imported = 0
        with db_lock:
            for username in wl:
                try:
                    c.execute("INSERT OR IGNORE INTO login_whitelist (gitcode_username, remark) VALUES (?, ?)",
                              (username, "from file"))
                    imported += c.rowcount
                except Exception:
                    pass
            db.commit()
        if imported:
            log.info(f"Whitelist migrated from file: {imported} users")
    except Exception as e:
        log.warning(f"Whitelist file migration skipped: {e}")


def _init_scene_category_data():
    """初始化 scene_category 表的 6 条分类数据（幂等）。

    技术方案 §2.1：通用/移动开发/基础设施/技能开发/AI应用/内容创作
    """
    try:
        from src.user.db import get_meta_value, set_meta_value
        if get_meta_value("scene_category_seeded") == "1":
            return
        categories = [
            ("通用", "general", "⚙", 1),
            ("移动开发", "mobile", "🍃", 2),
            ("基础设施", "iac", "☁", 3),
            ("技能开发", "skill-dev", "🔧", 4),
            ("AI应用", "ai", "🤖", 5),
            ("内容创作", "content", "📝", 6),
        ]
        with db_lock:
            c = db.cursor()
            for name, slug, icon, order_num in categories:
                c.execute(
                    "INSERT OR IGNORE INTO scene_category (name, slug, icon, order_num, is_active) VALUES (?,?,?,?,1)",
                    (name, slug, icon, order_num),
                )
            db.commit()
        set_meta_value("scene_category_seeded", "1")
        log.info("Seeded scene_category with 6 categories")
    except Exception as e:
        log.warning(f"Init scene_category data failed: {e}")


def _init_scene_market_data():
    """初始化 scene_market 表的 8 条场景数据（幂等）。

    技术方案 §3.2：从现有 devops_pipeline_scene official 场景复制 nodes_json + 补充展示字段。
    """
    try:
        from src.user.db import get_meta_value, set_meta_value
        if get_meta_value("scene_market_seeded") == "1":
            return
        with db_lock:
            c = db.cursor()
            # 检查已有数据
            count = c.execute("SELECT COUNT(*) FROM scene_market WHERE source='official'").fetchone()[0]
            if count > 0:
                set_meta_value("scene_market_seeded", "1")
                return
            # 从 scene_market(source='official') 复制 nodes_json（官方场景模板已种入 market；旧流程兼容 devops_pipeline_scene official）
            scene_map = {}
            try:
                c.execute("SELECT DISTINCT name, display_name, description, nodes_json, onboarding FROM scene_market WHERE source='official' AND is_active=1")
                for r in c.fetchall():
                    scene_map[r[0]] = r
            except Exception:
                pass
            if not scene_map:
                try:
                    c.execute("SELECT scene_id, name, description, nodes_json, onboarding FROM devops_pipeline_scene WHERE scope='official'")
                    for r in c.fetchall():
                        scene_map[r[0]] = r
                except Exception:
                    pass
            # 8 条初始场景：name, display_name, category_id, is_featured, recommended_template, market_description, source_scene_id
            init_scenes = [
                ("harmony", "鸿蒙开发", 2, 0, "harmony-ecommerce", "鸿蒙应用开发场景，适合 HarmonyOS 应用开发", None),
                ("default", "通用开发", 1, 1, "hello-flask", "通用的全链路开发场景，从需求收集到部署运维，适合大多数项目", "default"),
                ("iac", "IaC开发", 3, 1, "tf-infra", "IaC 基础设施即代码开发场景，使用 Terraform 管理云资源", "iac"),
                ("skill-dev", "华为云Skill开发", 4, 1, "hello-skill", "华为云 Skill 开发场景，创建和发布华为云技能", "skill-dev"),
                ("cloud-native", "云原生开发", 3, 0, "micro-svc", "云原生开发场景，适合微服务和容器化项目", "cloud-native"),
                ("agent-dev", "Agent开发", 5, 0, "ai-interview-swarm", "Agent 开发场景，适合构建 AI Agent 项目", "agent-dev"),
                ("api-skill-dev", "通用Skill开发", 4, 0, "hello-skill", "通用 Skill 开发场景，创建和发布通用技能", "api-skill-dev"),
                ("blog-publish", "博客发布", 6, 0, "", "博客系统开发与发布场景", "blog-publish"),
            ]
            imported = 0
            for name, display_name, cat_id, featured, template, mkt_desc, src_scene_id in init_scenes:
                nodes_json = "[]"
                onboarding = "repo-onboarding"
                if src_scene_id and src_scene_id in scene_map:
                    row = scene_map[src_scene_id]
                    nodes_json = row[3] or "[]"
                    onboarding = row[4] or "repo-onboarding"
                c.execute(
                    "INSERT OR IGNORE INTO scene_market "
                    "(name, display_name, version, description, market_description, icon, category_id, "
                    "is_featured, like_count, source, source_user_id, recommended_template, nodes_json, onboarding, is_active) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,1)",
                    (name, display_name, "1.0.0", mkt_desc, mkt_desc, "", cat_id,
                     featured, 0, "official", 0, template, nodes_json, onboarding),
                )
                imported += 1
            db.commit()
        set_meta_value("scene_market_seeded", "1")
        log.info(f"Seeded scene_market with {imported} scenes")
    except Exception as e:
        log.warning(f"Init scene_market data failed: {e}")


def _init_scene_skill_ref():
    """初始化 scene_skill_ref 表（幂等）+ 回填 skill_id。

    技术方案 §3.3：遍历 scene_market 全部场景的 nodes_json，拆出每个 skill_name，
    查 skill 表拿 skill_id，一起写入 scene_skill_ref。
    幂等：已有记录补填 skill_id（旧数据可能 skill_id=0）。
    """
    try:
        from src.user.db import get_meta_value, set_meta_value
        with db_lock:
            c = db.cursor()
            # 回填：已有记录中 skill_id=0 的，从 skill 表查 id 补上
            c.execute("SELECT id, skill_name FROM scene_skill_ref WHERE skill_id=0")
            stale_rows = c.fetchall()
            for ref_id, skill_name in stale_rows:
                c.execute(
                    "SELECT id FROM skill WHERE skill_name=? AND is_active=1 LIMIT 1",
                    (skill_name,),
                )
                sk_row = c.fetchone()
                if sk_row:
                    c.execute(
                        "UPDATE scene_skill_ref SET skill_id=? WHERE id=?",
                        (sk_row[0], ref_id),
                    )
            if stale_rows:
                db.commit()
                log.info(f"Backfilled {len(stale_rows)} scene_skill_ref.skill_id")

            # 首次初始化：未 seeded 时写入全部关联
            if get_meta_value("scene_skill_ref_seeded") == "1":
                return
            c.execute("SELECT id, nodes_json FROM scene_market WHERE is_active=1")
            rows = c.fetchall()
            inserted = 0
            for scene_id, nodes_json in rows:
                if not nodes_json:
                    continue
                try:
                    nodes = json.loads(nodes_json)
                except Exception:
                    continue
                for node in (nodes or []):
                    skill_name = (node or {}).get("skill", "")
                    if not skill_name:
                        continue
                    # 查 skill 表拿 skill_id
                    skill_id = 0
                    c.execute(
                        "SELECT id FROM skill WHERE skill_name=? AND is_active=1 LIMIT 1",
                        (skill_name,),
                    )
                    sk_row = c.fetchone()
                    if sk_row:
                        skill_id = sk_row[0]
                    c.execute(
                        "INSERT OR IGNORE INTO scene_skill_ref (scene_id, skill_id, skill_name) VALUES (?,?,?)",
                        (scene_id, skill_id, skill_name),
                    )
                    inserted += c.rowcount
            db.commit()
        set_meta_value("scene_skill_ref_seeded", "1")
        log.info(f"Seeded scene_skill_ref with {inserted} skill references")
    except Exception as e:
        log.warning(f"Init scene_skill_ref failed: {e}")


def _init_skill_featured():
    """初始化 skill 表的 is_featured 精选标记（幂等）。

    4 个技能设 is_featured=1（vod/issue-advance/implementation/pr）。
    """
    try:
        from src.user.db import get_meta_value, set_meta_value
        if get_meta_value("skill_featured_seeded") == "1":
            return
        featured_skills = [
            "vod", "issue-advance", "implementation", "pr",
        ]
        with db_lock:
            c = db.cursor()
            updated = 0
            for skill_name in featured_skills:
                c.execute(
                    "UPDATE skill SET is_featured=1 WHERE skill_name=? AND scope='official'",
                    (skill_name,),
                )
                updated += c.rowcount
            db.commit()
        set_meta_value("skill_featured_seeded", "1")
        log.info(f"Set is_featured=1 for {updated} official skills")
    except Exception as e:
        log.warning(f"Init skill featured failed: {e}")


def _seed_featured_templates_from_json():
    """从 configs/project-templates.json 种入 featured_templates 表（幂等自愈）。

    Issue #56：优选项目 Tab 列表为空。根因：DB featured_templates 表为空（同步脚本
    依赖外部 GitCode/GitHub API + 有效 token，环境里常未跑通），而 /top1000 API 直查
    空表返回空列表 → 前端 Tab 无卡片。本函数用已随仓库发布的 JSON 兜底：仅当表为空时
    把 gitcode-featured/github-featured 两个分类反向映射为 DB 行写入，使 API 即时返回
    非空数据。同步脚本后续仍可 INSERT OR REPLACE 覆盖为最新真实数据。
    """
    try:
        config_path = os.path.join(_CONFIG_DIR, "project-templates.json")
        if not os.path.exists(config_path):
            log.info("project-templates.json not found, skip featured_templates seed")
            return
        with db_lock:
            c = db.cursor()
            # 仅表为空时种入——不挡同步脚本后续 INSERT OR REPLACE，DB 被清空后下次启动自愈
            cnt = c.execute("SELECT COUNT(*) FROM featured_templates").fetchone()[0]
            if cnt > 0:
                return
        with open(config_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        featured_cats = [cat for cat in (data.get("categories") or [])
                         if cat.get("id") in ("gitcode-featured", "github-featured")]
        if not featured_cats:
            log.info("No featured categories in project-templates.json, skip seed")
            return
        import datetime as _dt
        now = _dt.datetime.now().isoformat()
        inserted = 0
        with db_lock:
            c = db.cursor()
            for cat in featured_cats:
                platform = cat.get("platform") or ("gitcode" if "gitcode" in cat.get("id", "") else "github")
                for t in (cat.get("templates") or []):
                    stats = t.get("stats") or {}
                    repo_full_name = t.get("source_repo") or ""
                    if not repo_full_name:
                        continue
                    c.execute(
                        "INSERT OR REPLACE INTO featured_templates "
                        "(platform, repo_full_name, repo_name, description, language, "
                        "stars, forks, topics, html_url, clone_url, category, recommend_reason, fetched_at) "
                        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        (platform, repo_full_name, t.get("name") or t.get("display_name") or "",
                         (t.get("description") or "")[:200], stats.get("language") or "",
                         stats.get("stars") or 0, 0,
                         json.dumps(t.get("tags") or [], ensure_ascii=False),
                         t.get("html_url") or "", t.get("clone_url") or "",
                         "cloud-native", t.get("recommend_reason") or "", now)
                    )
                    inserted += 1
            db.commit()
        log.info(f"Seeded featured_templates with {inserted} items from project-templates.json")
    except Exception as e:
        log.warning(f"Seed featured_templates from json failed: {e}")


def init_database():
    """初始化所有新表(幂等),启动时调用"""
    # 迁移锁——MySQL 用 GET_LOCK 防多实例同时迁移
    # SQLite 不支持 GET_LOCK，跳过（单实例不需要）
    from src.mysql_compat import get_db_backend
    if get_db_backend() == "mysql":
        with db_lock:
            c = db.cursor()
            c.execute("SELECT GET_LOCK('grape_migrate', 300)")
            lock_acquired = c.fetchone()[0]
            if not lock_acquired:
                log.warning("Could not acquire migrate lock, waiting...")
            try:
                # 迁移逻辑在同一个 with db_lock 块内——同一个连接
                # db_lock 是 RLock（可重入）——_create_tables() 等内部 with db_lock 不会死锁
                _create_tables()
                _seed_featured_templates_from_json()
                _migrate_tables()
                _create_indexes()
                _seed_from_json()
                _seed_event_action_filter()
                _sync_official_skills()
                _align_agent_enable()
                _cleanup_stale_meta()
                _cleanup_chat_shared_worker_runtime()
                _migrate_whitelist_from_file()
                # ===== Issue-11: DevOps 市场数据初始化 =====
                _init_scene_category_data()
                _init_scene_market_data()
                _init_scene_skill_ref()
                _init_skill_featured()
                # ===== Issue-39: 场景 display_name 回填（market 数据就绪后再回填）=====
                try:
                    _backfill_scene_display_names()
                except Exception as _e:
                    log.warning(f"Scene display_name backfill failed: {_e}")
            finally:
                c.execute("SELECT RELEASE_LOCK('grape_migrate')")
    else:
        # SQLite——单实例不需要锁
        _create_tables()
        _seed_featured_templates_from_json()
        _migrate_tables()
        _create_indexes()
        _seed_from_json()
        _seed_event_action_filter()
        _sync_official_skills()
        _align_agent_enable()
        _cleanup_stale_meta()
        _cleanup_chat_shared_worker_runtime()
        _migrate_whitelist_from_file()
        # ===== Issue-11: DevOps 市场数据初始化 =====
        _init_scene_category_data()
        _init_scene_market_data()
        _init_scene_skill_ref()
        _init_skill_featured()
        # ===== Issue-39: 场景 display_name 回填（market 数据就绪后再回填）=====
        try:
            _backfill_scene_display_names()
        except Exception as _e:
            log.warning(f"Scene display_name backfill failed: {_e}")
    # MySQL with session_manager

def _sync_official_skills():
    """启动时从 configs/skill-description.json 同步官方技能到 skill 表(幂等).

    与 /api/skills/sync-official 逻辑一致:新增 INSERT + 已有 UPDATE 元信息
   (display_name/description/agent_type)._seed_from_json 只补种缺失技能,
    不更新已有技能的元信息--配置文件中改名/改描述后需要此步骤同步.
    """
    try:
        import json as _json
        config_dir = os.path.join(os.path.dirname(__file__), "..", "..", "configs")
        json_path = os.path.join(config_dir, "skill-description.json")
        if not os.path.exists(json_path):
            return
        with open(json_path, "r", encoding="utf-8") as f:
            data = _json.load(f)
        added = 0
        updated = 0
        with db_lock:
            c = db.cursor()
            for skill_name, info in data.items():
                display_name = info.get("name", skill_name)
                description = info.get("description", "")
                agent_type = info.get("agent_type", "")
                skill_path = info.get("path", skill_name)
                existed = c.execute(
                    "SELECT COUNT(*) FROM skill WHERE skill_name=? AND scope='official'",
                    (skill_name,)
                ).fetchone()[0]
                if existed:
                    # 更新元信息；本地（非 OBS 集群模式，file_path 为空）同时刷新 skill_path，
                    # 保证目录按 agent_type 分组后（skills/<agent_type>/<skill>/）DB 路径与磁盘一致。
                    _fp = c.execute(
                        "SELECT file_path FROM skill WHERE skill_name=? AND scope='official' AND file_path IS NOT NULL AND file_path!=''",
                        (skill_name,)
                    ).fetchone()
                    if _fp:
                        c.execute(
                            "UPDATE skill SET display_name=?, description=?, agent_type=? WHERE skill_name=? AND scope='official'",
                            (display_name, description, agent_type, skill_name)
                        )
                    else:
                        c.execute(
                            "UPDATE skill SET display_name=?, description=?, agent_type=?, skill_path=? WHERE skill_name=? AND scope='official'",
                            (display_name, description, agent_type, skill_path, skill_name)
                        )
                    updated += 1
                else:
                    c.execute(
                        "INSERT INTO skill (skill_name, display_name, description, agent_type, skill_path, scope, owner_id, content_hash) VALUES (?,?,?,?,?,'official','',?)",
                        (skill_name, display_name, description, agent_type, skill_path, "")
                    )
                    added += 1
            db.commit()
        if added or updated:
            log.info(f"Official skills synced: +{added} added, {updated} updated")
    except Exception as e:
        log.warning(f"Sync official skills failed: {e}")


def _cleanup_stale_meta():
    """删除单 bot 时代遗留的全局 meta.ws_feishu_app_id.

    该 key 由旧 WebSocket 启动流程写入,多 bot 化后已无写入方,无业务用途;曾被未绑定仓库
    兜底误用导致把 Jason7602 的仓库发给其它用户(2026-08-17,见 feishu-web-chat-unified-design.md §10).
    """
    try:
        c = db.cursor()
        c.execute("DELETE FROM meta WHERE key='ws_feishu_app_id'")
        db.commit()
        log.info("Cleaned stale meta key: ws_feishu_app_id")
    except Exception as e:
        log.warning(f"Cleanup stale meta ws_feishu_app_id failed: {e}")


def _cleanup_chat_shared_worker_runtime():
    """把 chat 配置中指向共享全局 worker(scope='global' 且 credential_id>0)的 runtime_id
    回落为本机直连(同名 credential_id=0),修复 backend 名解析被共享 worker 劫持
    (chat-agent-backend-resolution-fix.md 方案 C).幂等,启动时执行."""
    # queue_remote 下本机直连行(credential_id=0)无 claim 方 = 黑洞
    #(chat-task-blackhole-local-direct-2026-08.md):回落会把用户配置重置为死引擎,
    # 每次重启覆盖管理员/用户改绑(2026-08-18 实测 sess-cfg-* 全被重置为 1),跳过;
    # direct 模式保留原防劫持语义.
    try:
        from src.runtime.service import get_execution_mode, EXEC_MODE_REMOTE
        if get_execution_mode() == EXEC_MODE_REMOTE:
            return
    except Exception:
        pass
    try:
        with db_lock:
            c = db.cursor()
            rows = c.execute("""
                SELECT c.id, c.runtime_id, ar.name
                FROM chat_agent_session c JOIN agent_runtimes ar ON ar.id = c.runtime_id
                WHERE c.repo_key='' AND c.runtime_id>0 AND ar.scope='global' AND ar.credential_id>0
            """).fetchall()
            changed = 0
            for cid, _rid, name in rows:
                local = c.execute(
                    "SELECT id FROM agent_runtimes WHERE lower(name)=? AND credential_id=0 AND installed=1 ORDER BY id LIMIT 1",
                    ((name or "").lower(),)).fetchone()
                new_rid = local[0] if local else 0
                c.execute("UPDATE chat_agent_session SET runtime_id=? WHERE id=?", (new_rid, cid))
                changed += 1
            if changed:
                db.commit()
                log.info(f"chat config worker->local direct: {changed} rows")
    except Exception as e:
        log.warning(f"cleanup chat shared-worker runtime failed: {e}")


def _align_agent_enable():
    """存量会话 enable 对齐 agent_type 默认值(仅执行一次,幂等).

    依赖:须在 _seed_from_json 之后调用(agent_type.enable 已由 JSON seed 写入正确默认值,
    如 sre=0).用 meta 标记 'enable_aligned' 保证只跑一次,避免覆盖用户后续在
    Dashboard 手动设置的仓库级 enable.
    """
    try:
        with db_lock:
            c = db.cursor()
            done = c.execute("SELECT COUNT(*) FROM meta WHERE key='enable_aligned'").fetchone()[0]
            if done:
                return
            # 先确保 enable 列存在(新库可能无)
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
    """幂等修复:重建 chat_agent_session,去掉陈旧的 `repo_key TEXT UNIQUE` 约束.

    背景:chat_agent_session 由旧 `sessions` 表 `ALTER TABLE ... RENAME` 而来,SQLite RENAME
    保留了旧表 `repo_key TEXT UNIQUE`;而代码 DDL(session_manager.py)意图为 per-user
    (repo_key TEXT DEFAULT '',无 UNIQUE).陈旧约束使全局 chat 配置(repo_key='')只能有一行,
    多用户配置互相 INSERT OR REPLACE 顶掉,导致 chat bot secret 丢失,断连.

    检测 sqlite_master 中该表定义含 `repo_key TEXT UNIQUE` -> 按当前 PRAGMA 列定义重建表
    (保留数据与全部列,仅去掉 UNIQUE).返回是否执行了重建.
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
    """多实例键迁移(multi-runtime-instance-design §7):把 agent_runtimes 的旧唯一约束
    `UNIQUE (name, scope, owner_id)` 升级为 `UNIQUE (credential_id, name, scope, owner_id)`.

    SQLite 无法直接改约束 -> 检测到旧约束时重建表(用新 DDL,保留数据).重建前清理
    `credential_id=0` 的同名重复行(历史迁移遗留,仅保留 id 最小的一行),避免新唯一键冲突.
    返回是否执行了重建(幂等:已是新键则跳过).
    """
    row = c.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='agent_runtimes'").fetchone()
    if not row or not row[0]:
        return False
    sql = row[0].lower()
    if "unique" not in sql:
        return False
    # 新键应包含 host_name（5 列）；旧键仅 4 列 (credential_id, name, scope, owner_id)
    # 注意：host_name 作为列定义也出现在 SQL 里，必须只检查 UNIQUE 子句
    import re as _re
    uniq_match = _re.search(r'unique\s*\([^)]+\)', sql)
    if not uniq_match:
        return False
    uniq_clause = uniq_match.group(0)
    if "credential_id" in uniq_clause and "host_name" in uniq_clause:
        return False  # 已是最新 5 列唯一键
    c.execute("""
        DELETE FROM agent_runtimes
        WHERE credential_id=0 AND id NOT IN (
            SELECT MIN(id) FROM agent_runtimes WHERE credential_id=0 GROUP BY lower(name), scope, owner_id
        )
    """)
    cols = [r[1] for r in c.execute("PRAGMA table_info(agent_runtimes)").fetchall()]
    c.execute(_AGENT_RUNTIMES_DDL.replace("CREATE TABLE IF NOT EXISTS agent_runtimes", "CREATE TABLE agent_runtimes_new"))
    # 只复制新旧表共有的列（旧表可能有 busy_slots/max_concurrent/tags 等已废弃列）
    new_cols = [r[1] for r in c.execute("PRAGMA table_info(agent_runtimes_new)").fetchall()]
    common_cols = [col for col in cols if col in new_cols]
    c.execute(f"INSERT INTO agent_runtimes_new ({','.join(common_cols)}) SELECT {','.join(common_cols)} FROM agent_runtimes")
    c.execute("DROP TABLE agent_runtimes")
    c.execute("ALTER TABLE agent_runtimes_new RENAME TO agent_runtimes")
    return True


def _alter_skill_files_if_needed() -> str:
    """agent_tasks.skill_files 仅非 MEDIUMTEXT 时返回 ALTER(避免每次启动 DDL 触发元数据锁).

    MySQL 元数据锁(metadata lock)在并发/残留 Sleep 连接下会等待--
    无条件每次 MODIFY 是启动变慢根因(2026-08-17 修复).
    SQLite 下无长度概念,直接返回空串(无害).
    """
    try:
        from src.session_manager import db
        rows = db.execute(
            "SELECT DATA_TYPE FROM information_schema.COLUMNS "
            "WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME='agent_tasks' "
            "AND COLUMN_NAME='skill_files'"
        ).fetchall()
        if rows and rows[0][0] == "mediumtext":
            return ""
        return "ALTER TABLE agent_tasks MODIFY skill_files MEDIUMTEXT"
    except Exception:
        return "ALTER TABLE agent_tasks MODIFY skill_files MEDIUMTEXT"


def _migrate_tables():
    """已有数据库的 schema 迁移"""
    # ===== new-version2 数据安全:迁移前自动备份(仅在有改动性迁移时触发一次)=====
    try:
        from src.session_manager import _backup_db, _db_path
        # 仅当存在旧结构(backend_* 表/列)时备份,避免无谓备份
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
                # 记录已备份,避免多进程重复备份
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
        # ===== 发布配置(skill-publish v1.1)=====
        # ===== OBS 技能库(obs-skill-storage-design.md §4)=====
        # v2.4 去版本化:DROP current_version(本期新增列,无历史依赖;列不存在时 SQLite 忽略报错)
        "ALTER TABLE skill DROP COLUMN current_version",
        # v2.5:file_path(真实存储路径 OBS 对象键,VARCHAR(512))+ created_by/updated_by(审计/删除权限,VARCHAR(64))
        "ALTER TABLE skill ADD COLUMN file_path VARCHAR(512) DEFAULT ''",
        "ALTER TABLE skill ADD COLUMN created_by VARCHAR(64) DEFAULT ''",
        "ALTER TABLE skill ADD COLUMN updated_by VARCHAR(64) DEFAULT ''",
        # skill_zip 已移除(本次需求新增,未上线,无包袱直砍--见 obs-skill-storage-design.md §5.7 v3 决策)
        # MySQL 下 skill_files 升级(SQLite 无长度限制,MODIFY 语句报错被忽略无害)
        # 2026-08-17 修复:DEFAULT '{}' 在 MySQL 上触发 metadata lock 卡启动--改为无默认;
        # 且 ALTER 仅在列非 MEDIUMTEXT 时执行(避免每次启动 DDL 触发元数据锁--启动变慢根因)
        _alter_skill_files_if_needed(),
        # Worker 三级分类——repo_key 字段（scope=user 时关联项目）
        "ALTER TABLE runtime_credentials ADD COLUMN repo_key VARCHAR(512) DEFAULT NULL",
        # ===== TMP (/tmp) 使用率指标（liufei 2026-08-28）=====
        "ALTER TABLE runtime_credentials ADD COLUMN tmp_percent DOUBLE DEFAULT 0",
        "ALTER TABLE agent_runtimes ADD COLUMN repo_key VARCHAR(512) DEFAULT NULL",
        # JWT 撤销——token_version 字段（改密码/logout 时 +1）
        "ALTER TABLE user_accounts ADD COLUMN token_version INTEGER DEFAULT 0",
        # 飞书 Bot Lease——集群部署保证同一 bot 同一时刻只有一个实例连 WebSocket
        "ALTER TABLE global_im_bot_config ADD COLUMN bot_name VARCHAR(128) DEFAULT ''",
        "ALTER TABLE global_im_bot_config ADD COLUMN lease_token VARCHAR(128) DEFAULT NULL",
        "ALTER TABLE global_im_bot_config ADD COLUMN lease_expires_at DATETIME DEFAULT NULL",
        "ALTER TABLE global_im_bot_config ADD COLUMN lease_owner VARCHAR(128) DEFAULT NULL",
        "ALTER TABLE global_im_bot_config ADD COLUMN lease_acquired_at DATETIME DEFAULT NULL",
        "ALTER TABLE global_im_bot_config ADD COLUMN updated_at DATETIME DEFAULT CURRENT_TIMESTAMP",
        # ===== knowledge-obs-design：agent_shared_knowledge OBS 对象存储字段 =====
        "ALTER TABLE agent_shared_knowledge ADD COLUMN obs_key VARCHAR(512) DEFAULT ''",
        "ALTER TABLE agent_shared_knowledge ADD COLUMN content_hash VARCHAR(64) DEFAULT ''",
        "ALTER TABLE agent_shared_knowledge ADD COLUMN updated_at DATETIME DEFAULT CURRENT_TIMESTAMP",
        # ===== knowledge-obs-design：agent_tasks 知识/project-info OBS 临时 URL =====
        "ALTER TABLE agent_tasks ADD COLUMN project_info_file_urls TEXT DEFAULT ''",
        "ALTER TABLE agent_tasks ADD COLUMN knowledge_file_urls TEXT DEFAULT ''",
        # ===== knowledge-obs-design：agent_runtimes worker 版本自报 =====
        "ALTER TABLE agent_runtimes ADD COLUMN worker_version TEXT DEFAULT ''",
        # ===== 多主控机：本机直连引擎按 host_name 区分（credential_id=0 不再全局唯一）=====
        "ALTER TABLE agent_runtimes ADD COLUMN host_name VARCHAR(191) DEFAULT ''",
        # ===== engine probe 探测结果列（liufei 2026-08-28）=====
        "ALTER TABLE agent_runtimes ADD COLUMN probe_status VARCHAR(32) DEFAULT 'pending'",
        "ALTER TABLE agent_runtimes ADD COLUMN probe_latency INT DEFAULT 0",
        "ALTER TABLE agent_runtimes ADD COLUMN probe_last VARCHAR(64) DEFAULT ''",
        "ALTER TABLE agent_runtimes ADD COLUMN probe_error TEXT DEFAULT ''",
        # ===== Issue-11: skill 表加 is_featured + like_count =====
        "ALTER TABLE skill ADD COLUMN is_featured INTEGER DEFAULT 0",
        "ALTER TABLE skill ADD COLUMN like_count INTEGER DEFAULT 0",
        # ===== Issue-11: devops_pipeline_scene 加 market_scene_id（单向关联） =====
        "ALTER TABLE devops_pipeline_scene ADD COLUMN market_scene_id INTEGER DEFAULT NULL",
        # ===== Issue-11: scene_skill_ref 加 skill_id =====
        "ALTER TABLE scene_skill_ref ADD COLUMN skill_id INTEGER DEFAULT 0",
        # ===== Issue-11: scene_market 加 tags/author/content_hash（场景包格式 + 定时同步） =====
        "ALTER TABLE scene_market ADD COLUMN tags VARCHAR(256) DEFAULT ''",
        "ALTER TABLE scene_market ADD COLUMN author VARCHAR(64) DEFAULT ''",
        "ALTER TABLE scene_market ADD COLUMN content_hash VARCHAR(64) DEFAULT ''",
        "ALTER TABLE scene_market ADD COLUMN obs_icon_key VARCHAR(256) DEFAULT ''",
        # ===== Issue-11: scene_category 加 slug（场景包 category 用 slug 引用） =====
        "ALTER TABLE scene_category ADD COLUMN slug VARCHAR(64) DEFAULT ''",
        # ===== Issue-54: scene_market 加 scope + repo_key 列 =====
        "ALTER TABLE scene_market ADD COLUMN scope VARCHAR(32) DEFAULT 'official'",
        "ALTER TABLE scene_market ADD COLUMN repo_key VARCHAR(191) DEFAULT ''",
    ]
    # 回填已有分类数据的 slug（幂等，仅 slug 为空时更新）
    _category_slugs = [
        ("general", "通用"), ("mobile", "移动开发"), ("iac", "基础设施"),
        ("skill-dev", "技能开发"), ("ai", "AI应用"), ("content", "内容创作"),
    ]
    for slug, name in _category_slugs:
        try:
            with db_lock:
                db.execute("UPDATE scene_category SET slug=? WHERE name=? AND (slug='' OR slug IS NULL)", (slug, name))
                db.commit()
        except Exception:
            pass
    for sql in migrations:
        try:
            with db_lock:
                db.execute(sql)
                db.commit()
        except Exception:
            pass  # column already exists
    # ===== Issue-54: scene_market UNIQUE 约束重建（方案 A）=====
    # 旧约束 UNIQUE(name, source, source_user_id) → 新约束 UNIQUE(name, scope, source_user_id, repo_key)
    # SQLite：表重建；MySQL：DROP INDEX + ADD UNIQUE INDEX
    try:
        _rebuild_scene_market_unique()
    except Exception as e:
        log.warning(f"Scene market unique rebuild failed: {e}")
    # 迁移 im 凭证到 im_bot_config 统一表
    try:
        with db_lock:
            c = db.cursor()
            # 迁移旧表 im_bot_config -> global_im_bot_config(保留 bot_type,secret 移入 devops_agent_session)
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
    # 迁移旧表 chat_config -> chat_agent_session(合并 user 配置)
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
    # 重建 backend_agents 表(旧结构 name 主键 -> 新结构 id+scope+owner_id)
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
    # 迁移 backend -> backend_id(引用 backend_agents.id,预置默认 backend)
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
    # 同步 skill 归属:从 skill-description.json 更新 skill 表 agent_type(确保 pr->qa 等归属一致)
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
    # backend_agents 大小写去重(合并为小写 name,保留最小 id,更新引用)
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
    # 清理旧表(如果存在)
    try:
        with db_lock:
            c = db.cursor()
            for tbl in ["vod_bot_config", "chat_config", "im_channels", "user_im_bindings", "agent_registry"]:
                c.execute(f"DROP TABLE IF EXISTS {tbl}")
            db.commit()
    except Exception:
        pass
    # 迁移 repo_im_group_binding 列规范化:chat_id->im_chat_id,account->im_app_id(旧 feishu_bindings 遗留)
    try:
        with db_lock:
            c = db.cursor()
            _tables = [r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()]
            if "repo_im_group_binding" in _tables:
                bcols = [r[1] for r in c.execute("PRAGMA table_info(repo_im_group_binding)").fetchall()]
                # account -> im_app_id(补空)
                if "account" in bcols and "im_app_id" in bcols:
                    c.execute("UPDATE repo_im_group_binding SET im_app_id=account WHERE (im_app_id IS NULL OR im_app_id='') AND account IS NOT NULL AND account!=''")
                    c.execute("ALTER TABLE repo_im_group_binding DROP COLUMN account")
                    db.commit()
                    log.info("Migrated repo_im_group_binding: account -> im_app_id (dropped account)")
                # chat_id -> im_chat_id
                bcols2 = [r[1] for r in c.execute("PRAGMA table_info(repo_im_group_binding)").fetchall()]
                if "chat_id" in bcols2 and "im_chat_id" not in bcols2:
                    c.execute("ALTER TABLE repo_im_group_binding RENAME COLUMN chat_id TO im_chat_id")
                    db.commit()
                    log.info("Migrated repo_im_group_binding: chat_id -> im_chat_id")
    except Exception as e:
        log.warning(f"Migrate repo_im_group_binding columns failed: {e}")
    # 迁移 repo_im_group_binding 的 im_app_id='default' -> Chat Bot app_id(清理 default 兜底账户)
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
    # 清理历史 bot_username 占位值:'devops-bot' -> 全局 bot 实际用户名(configs/global-bot-token.json)
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
                # 历史占位值 'devops-bot'(非实际 GitCode 用户名)-> 替换为全局 bot 用户名
                c.execute("UPDATE managed_repos SET bot_username=? WHERE bot_username='devops-bot' OR bot_username='' OR bot_username IS NULL",
                          (_global_bot_username,))
                db.commit()
                log.info(f"Migrated managed_repos.bot_username legacy placeholder -> {_global_bot_username} ({c.rowcount} rows)")
    except Exception as e:
        log.warning(f"Migrate bot_username placeholder failed: {e}")
    # 首次运行加载配置:从 global-bot-token.json 将 OAuth 配置写入 meta 表(仅 meta 无该 key 时)
    # 兼容旧版:若存在 user_accounts default 账户,仍尝试从其中迁移 OAuth 配置到 meta 表
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
            # 2. 兼容旧版 default 账户(OAuth 配置曾存于 user_accounts)
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
                # 3. 删除 default 账户(OAuth 已收敛到 meta 表)
                c.execute("DELETE FROM user_accounts WHERE username='default'")
            db.commit()
            if migrated:
                log.info("Loaded OAuth config (gitcode client id/secret) from global-bot-token.json into meta table")
            else:
                log.info("Deleted default account (no config to migrate)")
    except Exception as e:
        log.warning(f"Migrate/delete default account failed: {e}")
    # 迁移旧表 agent_sessions -> devops_agent_session(幂等)
    try:
        with db_lock:
            c = db.cursor()
            _tables = [r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()]
            if "agent_sessions" in _tables and "devops_agent_session" not in _tables:
                c.execute("ALTER TABLE agent_sessions RENAME TO devops_agent_session")
                db.commit()
                log.info("Migrated table agent_sessions -> devops_agent_session")
            elif "agent_sessions" in _tables and "devops_agent_session" in _tables:
                # 新表已被创建但旧表仍存在(历史库),迁移数据后删除旧表
                c.execute("INSERT OR IGNORE INTO devops_agent_session (id, repo_key, agent_type, backend, skill, oc_session_id, repo_gitcode_token, status, created_at, last_active_at, im_provider, im_app_id, im_app_secret) SELECT id, repo_key, agent_type, backend, skill, oc_session_id, repo_gitcode_token, status, created_at, last_active_at, im_provider, im_app_id, im_app_secret FROM agent_sessions")
                c.execute("DROP TABLE agent_sessions")
                db.commit()
                log.info("Migrated agent_sessions data into devops_agent_session and dropped agent_sessions")
    except Exception as e:
        log.warning(f"Migrate agent_sessions table failed: {e}")
    # 迁移 agent_type_config -> agent_type + skill(旧架构拆分)
    try:
        with db_lock:
            c = db.cursor()
            _tables = [r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()]
            if "agent_type_config" in _tables:
                # 1. Agent 类型(order_num>0)-> agent_type 表
                c.execute("SELECT agent_type, display_name, description, default_backend, icon, order_num, subscribe_events FROM agent_type_config WHERE order_num>0")
                for r in c.fetchall():
                    c.execute(
                        "INSERT OR IGNORE INTO agent_type (agent_type, display_name, description, default_backend, icon, order_num, subscribe_events) VALUES (?,?,?,?,?,?,?)",
                        (r[0], r[1], r[2] or "", r[3] or "opencode", r[4] or "", r[5] or 0, r[6] or '[]')
                    )
                # 2. 官方技能(parent_agent_type!='')-> skill 表(scope=official)
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
    # 迁移 user_skills -> skill 表(scope=user)
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
    # 删除旧表(agent_type_config / user_skills / repo_skill_mapping)
    try:
        with db_lock:
            c = db.cursor()
            for tbl in ["agent_type_config", "user_skills", "repo_skill_mapping"]:
                c.execute(f"DROP TABLE IF EXISTS {tbl}")
            db.commit()
            log.info("Dropped legacy tables: agent_type_config, user_skills, repo_skill_mapping")
    except Exception as e:
        log.warning(f"Drop legacy skill tables failed: {e}")
    # 迁移 devops_agent_session:vod_bot_app_id -> im_app_id(通用化,保留 im_* 通用字段)
    try:
        with db_lock:
            c = db.cursor()
            cols = [r[1] for r in c.execute("PRAGMA table_info(devops_agent_session)").fetchall()]
            if "vod_bot_app_id" in cols:
                # 1. vod_bot_app_id -> im_app_id + im_provider='feishu' + im_app_secret 补齐
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
    # 迁移 devops_agent_session.oc_session_id -> backend_session_id
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
    # 迁移 vod_conversations 字段(feishu_chat_id->im_chat_id, oc_session_id->backend_session_id)
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
    # 迁移 chat_conversations.session_id -> backend_session_id
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
    # 迁移 devops_agent_session / chat_agent_session:skill_name(TEXT) -> skill_id(INTEGER)
    # 按 skill_name 匹配 skill 表(official 优先)拿 id,匹配不到则保留 skill_name(兼容旧数据)
    try:
        with db_lock:
            c = db.cursor()
            for _tbl in ("devops_agent_session", "chat_agent_session"):
                _cols = [r[1] for r in c.execute(f"PRAGMA table_info({_tbl})").fetchall()]
                if "skill" not in _cols or "skill_id" not in _cols:
                    continue
                # 先构建 skill_name -> id 映射(official 优先)
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
    # ===== new-version2 M0:backend_agents -> agent_runtimes(术语重构,须在所有 backend_agents 历史迁移之后)=====
    try:
        with db_lock:
            c = db.cursor()
            _tables = [r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()]
            if "backend_agents" in _tables and "agent_runtimes" in _tables:
                # 双表并存:老库升级时 _create_tables 已建空 agent_runtimes -> 搬迁数据后删旧表
                _copy_backend_to_runtime(c)
                c.execute("DROP TABLE backend_agents")
                db.commit()
                log.info("Migrated backend_agents -> agent_runtimes (data copied, old dropped)")
            elif "backend_agents" in _tables and "agent_runtimes" not in _tables:
                # 仅有旧表:直接 rename 保数据(理论上不发生,_create_tables 总建新表;防御分支)
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
    # ===== new-version2 M0:引用列改名(SQLite 3.25+ RENAME COLUMN,幂等)=====
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
    # ===== new-version2:Agent 启用开关 enable(见 agent-enable-toggle-design.md)=====
    # 仅加列;存量会话对齐在 seed 之后执行(_align_agent_enable),因为对齐依赖 agent_type.enable 已由 seed 写入
    try:
        with db_lock:
            c = db.cursor()
            _ensure_column(c, "agent_type", "enable", "INTEGER DEFAULT 1")
            _ensure_column(c, "devops_agent_session", "enable", "INTEGER DEFAULT 1")
            db.commit()
            log.info("Added enable column to agent_type / devops_agent_session")
    except Exception as e:
        log.warning(f"Add enable column failed: {e}")
    # ===== new-version2:Agent 事件订阅(仓库级,见 agent-event-subscription-design.md)=====
    try:
        with db_lock:
            c = db.cursor()
            _ensure_column(c, "devops_agent_session", "subscribe_events", "TEXT DEFAULT '[]'")
            db.commit()
            log.info("Added subscribe_events column to devops_agent_session")
    except Exception as e:
        log.warning(f"Add subscribe_events column failed: {e}")
    # ===== new-version2:Chat 任务对话场景(见 template-onboarding-task-full-chain-design.md §5.4)=====
    try:
        with db_lock:
            c = db.cursor()
            _ensure_column(c, "chat_conversations", "conv_type", "TEXT DEFAULT 'general'")
            _ensure_column(c, "chat_conversations", "issue_number", "INTEGER DEFAULT 0")
            db.commit()
            log.info("Added conv_type / issue_number to chat_conversations")
    except Exception as e:
        log.warning(f"Add chat conversation type failed: {e}")
    # ===== new-version2 多 Issue 隔离(见 multi-issue-isolation-design.md):agent_tasks 新列 + issue_agent_session 表 =====
    # 表由 _create_tables 建;此处仅幂等补列(老库升级)
    try:
        with db_lock:
            c = db.cursor()
            _ensure_column(c, "agent_tasks", "issue_session_id", "TEXT DEFAULT ''")
            _ensure_column(c, "agent_tasks", "project_info_files", "TEXT DEFAULT '{}'")
            _ensure_column(c, "agent_tasks", "project_info_meta", "TEXT DEFAULT '{}'")
            _ensure_column(c, "agent_tasks", "knowledge_files", "TEXT DEFAULT '{}'")
            _ensure_column(c, "agent_tasks", "skill_id", "INTEGER DEFAULT 0")
            db.commit()
            log.info("Added issue_session_id / project_info_* / knowledge_files / skill_id columns to agent_tasks")
    except Exception as e:
        log.warning(f"Add agent_tasks issue isolation columns failed: {e}")
    # ===== user_accounts 发布仓库列(老库升级;CREATE TABLE 只影响新库,存量库须幂等补列)=====
    try:
        with db_lock:
            c = db.cursor()
            _ensure_column(c, "user_accounts", "gitcode_publish_repo", "TEXT DEFAULT ''")
            _ensure_column(c, "user_accounts", "github_publish_repo", "TEXT DEFAULT ''")
            db.commit()
            log.info("Added gitcode_publish_repo / github_publish_repo columns to user_accounts")
    except Exception as e:
        log.warning(f"Add user_accounts publish repo columns failed: {e}")
    # ===== managed_repos 部署URL列（weekly-01 合并） =====
    try:
        with db_lock:
            c = db.cursor()
            _ensure_column(c, "managed_repos", "deploy_url", "VARCHAR(512) DEFAULT ''")
            db.commit()
            log.info("Added managed_repos.deploy_url column")
    except Exception as e:
        log.warning(f"Add managed_repos.deploy_url column failed: {e}")
    # ===== managed_repos 托管状态列（issue-host-repo-429 方案 F） =====
    try:
        with db_lock:
            c = db.cursor()
            _ensure_column(c, "managed_repos", "host_status", "TEXT DEFAULT ''")
            db.commit()
            log.info("Added managed_repos.host_status column")
    except Exception as e:
        log.warning(f"Add managed_repos.host_status column failed: {e}")
    # ===== devops_pipeline_scene 高级选项列（场景级工作分支 + 自定义 key:value）=====
    try:
        with db_lock:
            c = db.cursor()
            _ensure_column(c, "devops_pipeline_scene", "advanced_options", "TEXT DEFAULT '{}'")
            db.commit()
            log.info("Added devops_pipeline_scene.advanced_options column")
    except Exception as e:
        log.warning(f"Add devops_pipeline_scene.advanced_options column failed: {e}")
    # ===== agent-cluster-design.md §4.1/§4.2:引擎池数据面(池表 + 槽位列 + 任务池标记)=====
    # 老库升级:agent_runtimes 补 busy_slots/max_concurrent;agent_tasks 补 attempts/pool_agent_type/affinity_runtime_id
    try:
        with db_lock:
            c = db.cursor()
            _ensure_column(c, "agent_runtimes", "busy_slots", "INTEGER NOT NULL DEFAULT 0")
            _ensure_column(c, "agent_runtimes", "max_concurrent", "INTEGER NOT NULL DEFAULT 4")
            _ensure_column(c, "agent_tasks", "attempts", "INTEGER NOT NULL DEFAULT 0")
            _ensure_column(c, "agent_tasks", "pool_agent_type", "TEXT DEFAULT ''")
            _ensure_column(c, "agent_tasks", "affinity_runtime_id", "INTEGER DEFAULT 0")
            db.commit()
            log.info("Added cluster columns: agent_runtimes.busy_slots/max_concurrent, agent_tasks.attempts/pool_agent_type/affinity_runtime_id")
    except Exception as e:
        log.warning(f"Add cluster columns failed: {e}")
    # ===== worker engine local_skills 列（老库升级）=====
    try:
        with db_lock:
            c = db.cursor()
            _ensure_column(c, "agent_runtimes", "local_skills", "TEXT DEFAULT '[]'")
            _ensure_column(c, "agent_runtimes", "pending_skill_scan", "INTEGER DEFAULT 0")
            _ensure_column(c, "agent_runtimes", "skill_scan_result", "TEXT DEFAULT ''")
            _ensure_column(c, "agent_runtimes", "agent_tags", "TEXT DEFAULT '[]'")
            db.commit()
            log.info("Added local_skills / pending_skill_scan / skill_scan_result / agent_tags columns to agent_runtimes")
    except Exception as e:
        log.warning(f"Add local_skills column failed: {e}")
    # ===== 修复 chat_agent_session 陈旧 UNIQUE(repo_key) 约束(多用户 chat 配置互斥顶掉,见 session_manager 注释)=====
    try:
        with db_lock:
            c = db.cursor()
            if _drop_chat_session_repo_unique(c):
                db.commit()
                log.info("Rebuilt chat_agent_session: removed stale UNIQUE(repo_key) constraint")
    except Exception as e:
        log.warning(f"Drop chat_agent_session UNIQUE(repo_key) failed: {e}")
    # ===== multi-runtime-instance-design §7:agent_runtimes 多实例唯一键 + runtime_credentials.last_ip =====
    # 注册键 (name, scope, owner_id) -> (credential_id, name, scope, owner_id),使多 worker 同名引擎并存.
    try:
        with db_lock:
            c = db.cursor()
            _tables = [r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()]
            if "runtime_credentials" in _tables:
                _ensure_column(c, "runtime_credentials", "last_ip", "TEXT DEFAULT ''")
                _ensure_column(c, "runtime_credentials", "owner_name", "TEXT DEFAULT ''")
                _ensure_column(c, "runtime_credentials", "cpu_percent", "REAL DEFAULT 0")
                _ensure_column(c, "runtime_credentials", "memory_percent", "REAL DEFAULT 0")
                _ensure_column(c, "runtime_credentials", "disk_percent", "REAL DEFAULT 0")
                _ensure_column(c, "runtime_credentials", "load_avg", "TEXT DEFAULT ''")
                # 凭据名全局唯一:先清重(同名多行仅保留最小 id),再建唯一索引
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
    # ===== 引擎标签化:agent_runtimes.tags + agent_tasks.pool_tag(event-dispatch-redesign-v2.md §2.2/§2.5)=====
    try:
        with db_lock:
            c = db.cursor()
            _ensure_column(c, "agent_runtimes", "tags", "TEXT DEFAULT ''")
            _ensure_column(c, "agent_tasks", "pool_tag", "TEXT DEFAULT ''")
            db.commit()
        log.info("Added tags/pool_tag columns for engine tagging")
    except Exception as e:
        log.warning(f"Add engine tagging columns failed: {e}")
    # ===== Issue #49: event_action_filter.block_changes（按真实 payload 判定 assign/label 变更）=====
    try:
        with db_lock:
            c = db.cursor()
            _ensure_column(c, "event_action_filter", "block_changes", "TEXT DEFAULT ''")
            db.commit()
        log.info("Added event_action_filter.block_changes column")
    except Exception as e:
        log.warning(f"Add event_action_filter.block_changes column failed: {e}")
    # ===== Phase 2: user_accounts.last_scene (event-dispatch-redesign-v2.md 3.1) =====
    try:
        with db_lock:
            c = db.cursor()
            _ensure_column(c, "user_accounts", "last_scene", "TEXT DEFAULT 'default'")
            db.commit()
        log.info("Added last_scene column to user_accounts")
    except Exception as e:
        log.warning(f"Add last_scene column failed: {e}")
    # ===== VOD auto-scene: issue_state.scene_id (vod-auto-scene-detection-design.md §3.1) =====
    try:
        with db_lock:
            c = db.cursor()
            _ensure_column(c, "issue_state", "scene_id", "TEXT DEFAULT ''")
            db.commit()
        log.info("Added scene_id column to issue_state")
    except Exception as e:
        log.warning(f"Add issue_state.scene_id column failed: {e}")
    # ===== multi-task mode: issue_state.parent_issue_id + is_subtask_mode =====
    try:
        with db_lock:
            c = db.cursor()
            _ensure_column(c, "issue_state", "parent_issue_id", "TEXT DEFAULT ''")
            _ensure_column(c, "issue_state", "is_subtask_mode", "INTEGER DEFAULT 0")
            _ensure_column(c, "tasks", "parent_issue_number", "INTEGER DEFAULT 0")
            _ensure_column(c, "tasks", "is_subtask_mode", "INTEGER DEFAULT 0")
            _ensure_column(c, "chat_messages", "task_id", "TEXT")
            _ensure_column(c, "chat_messages", "generate_status", "INTEGER DEFAULT NULL")
            db.commit()
        log.info("Added parent_issue_id/is_subtask_mode columns to issue_state + tasks + task_id/generate_status to chat_messages")
    except Exception as e:
        log.warning(f"Add multi-task columns failed: {e}")
    # ===== event_thread: task_id + generate_status（对齐 Chat Agent 持久化查询）=====
    try:
        with db_lock:
            c = db.cursor()
            _ensure_column(c, "event_thread", "task_id", "TEXT DEFAULT ''")
            _ensure_column(c, "event_thread", "generate_status", "INTEGER DEFAULT NULL")
            db.commit()
        log.info("Added task_id/generate_status columns to event_thread")
    except Exception as e:
        log.warning(f"Add event_thread task_id/generate_status columns failed: {e}")
    # ===== Scene Center: issue_state.scene_user_id =====
    try:
        with db_lock:
            c = db.cursor()
            _ensure_column(c, "issue_state", "scene_user_id", "INTEGER DEFAULT 0")
            db.commit()
        log.info("Added scene_user_id column to issue_state")
    except Exception as e:
        log.warning(f"Add issue_state.scene_user_id column failed: {e}")
    # ===== VOD scene override switch: issue_state.allow_scene_override =====
    try:
        with db_lock:
            c = db.cursor()
            _ensure_column(c, "issue_state", "allow_scene_override", "INTEGER DEFAULT 0")
            db.commit()
        log.info("Added allow_scene_override column to issue_state")
    except Exception as e:
        log.warning(f"Add issue_state.allow_scene_override column failed: {e}")
    # ===== Scene Center: agent_tasks.pool_engine_ids + 官方场景种子导入 =====
    try:
        with db_lock:
            c = db.cursor()
            _ensure_column(c, "agent_tasks", "pool_engine_ids", "TEXT DEFAULT ''")
            db.commit()
        log.info("Added pool_engine_ids column to agent_tasks")
    except Exception as e:
        log.warning(f"Add pool_engine_ids column failed: {e}")
    # 官方场景种子导入（从 pipelines.json → DB，仅首次）
    try:
        _seed_official_scene_market_from_pipelines()
    except Exception as e:
        log.warning(f"Scene seed import failed: {e}")
    # ===== 官方技能 OBS 同步：skill 表新增 content_hash 字段（official-skill-obs-sync-design.md §4.4）=====
    try:
        with db_lock:
            c = db.cursor()
            _ensure_column(c, "skill", "content_hash", "VARCHAR(256) DEFAULT ''")
            db.commit()
        log.info("Added content_hash column to skill")
    except Exception as e:
        log.warning(f"Add skill.content_hash column failed: {e}")
    # ===== Scene scope downgrade 已在 _migrate_scene_table_to_repo_model 中处理（Issue-39 方案 B）=====


def _rebuild_scene_market_unique():
    """Issue-54: scene_market UNIQUE 约束重建（方案 A，幂等）。

    旧约束 UNIQUE(name, source, source_user_id) → 新约束 UNIQUE(name, scope, source_user_id, repo_key)

    SQLite：表重建（CREATE 新表 → 复制数据 → DROP 旧表 → RENAME）
    MySQL：DROP INDEX + ADD UNIQUE INDEX（在线 DDL）
    """
    from src.mysql_compat import get_db_backend
    is_mysql = get_db_backend() == "mysql"

    with db_lock:
        c = db.cursor()

        if is_mysql:
            # MySQL：检查旧索引是否存在，存在则 DROP + ADD
            # MySQL 的 UNIQUE 约束体现为索引，索引名取决于建表时的约束名
            # SQLite 的 UNIQUE(name, source, source_user_id) 在 MySQL 翻译后索引名为 sqlite_autoindex_scene_market_N
            # 需要查 information_schema 找到包含 (name, source, source_user_id) 的唯一索引
            try:
                c.execute(
                    "SELECT INDEX_NAME FROM information_schema.statistics "
                    "WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME='scene_market' "
                    "AND NON_UNIQUE=0 GROUP BY INDEX_NAME "
                    "HAVING GROUP_CONCAT(COLUMN_NAME ORDER BY SEQ_IN_INDEX) = 'name,source,source_user_id'"
                )
                row = c.fetchone()
                if row:
                    old_idx = row[0]
                    c.execute(f"ALTER TABLE scene_market DROP INDEX `{old_idx}`")
                    db.commit()
                    log.info(f"Scene market: dropped old unique index '{old_idx}'")
            except Exception as e:
                log.warning(f"Scene market: drop old unique index failed (may not exist): {e}")

            # 检查新索引是否已存在
            try:
                c.execute(
                    "SELECT COUNT(*) FROM information_schema.statistics "
                    "WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME='scene_market' "
                    "AND INDEX_NAME='uk_name_scope_user_repo' AND NON_UNIQUE=0"
                )
                if c.fetchone()[0] == 0:
                    c.execute(
                        "ALTER TABLE scene_market ADD UNIQUE INDEX uk_name_scope_user_repo "
                        "(name, scope, source_user_id, repo_key)"
                    )
                    db.commit()
                    log.info("Scene market: added new unique index uk_name_scope_user_repo (MySQL)")
            except Exception as e:
                log.warning(f"Scene market: add new unique index failed (may already exist): {e}")
        else:
            # SQLite：检查是否需要重建（新约束已存在则跳过）
            # 通过检查表是否有 scope 列 + 旧唯一约束是否还在
            cols = [r[1] for r in c.execute("PRAGMA table_info(scene_market)").fetchall()]
            if "scope" not in cols:
                # scope 列还没加（migrations 还没跑完），跳过
                return

            # 检查现有唯一索引：如果已有 (name, scope, source_user_id, repo_key) 就跳过
            indices = c.execute("PRAGMA index_list(scene_market)").fetchall()
            need_rebuild = True
            for idx_row in indices:
                idx_name = idx_row[1]
                is_unique = idx_row[2]
                if is_unique:
                    idx_cols = [r[2] for r in c.execute(f"PRAGMA index_info({idx_name})").fetchall()]
                    if set(idx_cols) == {"name", "scope", "source_user_id", "repo_key"}:
                        need_rebuild = False
                        break
            if not need_rebuild:
                return

            # 表重建：创建临时新表 → 复制数据 → 删旧表 → 重命名
            log.info("Scene market: rebuilding table for new UNIQUE constraint (SQLite)")
            c.execute("ALTER TABLE scene_market RENAME TO scene_market_old")
            c.execute(_SCENE_MARKET_DDL)
            # 复制数据（official 场景补 scope='official', repo_key=***）
            c.execute(
                "INSERT INTO scene_market SELECT "
                "id, name, display_name, version, description, market_description, "
                "icon, category_id, is_featured, like_count, source, source_user_id, "
                "recommended_template, nodes_json, onboarding, tags, author, "
                "content_hash, obs_icon_key, is_active, "
                "COALESCE(scope, 'official'), COALESCE(repo_key, ''), "
                "created_by, updated_by, created_at, updated_at "
                "FROM scene_market_old"
            )
            c.execute("DROP TABLE scene_market_old")
            db.commit()
            log.info("Scene market: table rebuilt with new UNIQUE constraint (SQLite)")


def _scene_table_columns(c) -> set[str]:
    try:
        return {r[1] for r in c.execute("PRAGMA table_info(devops_pipeline_scene)").fetchall()}
    except Exception:
        return set()


def _cols_old_scene() -> bool:
    """是否仍是旧结构（含 scope / user_id 列）→ 需要重建为 repo 模型。"""
    with db_lock:
        c = db.cursor()
        _cols = _scene_table_columns(c)
    return _cols and ("scope" in _cols or "user_id" in _cols)


def _strip_repo_prefix(scene_id: str) -> str:
    """去掉 scene_id 的 'repo:' 前缀。"""
    return scene_id[len("repo:"):] if scene_id.startswith("repo:") else scene_id


def _insert_repo_scene(c, scene_id: str, name: str, description: str, repo_key: str,
                       nodes_json: str, onboarding: str = "repo-onboarding"):
    try:
        c.execute(
            "INSERT OR IGNORE INTO devops_pipeline_scene_new (scene_id, name, description, repo_key, nodes_json, onboarding, is_active, created_at, updated_at) "
            "VALUES (?,?,?,?,?,?,1, datetime('now'), datetime('now'))",
            (scene_id, name, description, repo_key, nodes_json, onboarding))
    except Exception as _e:
        log.warning(f"Scene migrate: insert repo scene {scene_id}@{repo_key} failed: {_e}")


def _migrate_scene_table_to_repo_model():
    """Issue-39 方案 B：devops_pipeline_scene 重建为「场景只与仓库关联」模型（幂等）。

    - 旧表含 scope/user_id 列 → 重建为新表（无 scope/user_id，UNIQUE(scene_id, repo_key)）
    - 存量数据搬移：
      * scope='repo'：保留，scene_id 去 'repo:' 前缀，repo_key 保留
      * scope='user'：经 user_repos 反查所属仓库（repo_key）并入；孤儿（查不到仓库）丢弃
      * scope='official'：官方模板不保留在场景表（官方场景走 scene_market），跳过
    - 幂等：新表已重建（无 scope 列）则跳过
    """
    if not _cols_old_scene():
        return
    with db_lock:
        c = db.cursor()
        old_rows = c.execute(
            "SELECT scene_id, name, description, scope, repo_key, user_id, nodes_json, onboarding, is_active "
            "FROM devops_pipeline_scene").fetchall()
        # user scope → repo_key 反查（user_repos）
        repo_of_user = {}
        try:
            for ur in c.execute("SELECT user_id, repo_key FROM user_repos").fetchall():
                repo_of_user.setdefault(ur[0], set()).add(ur[1])
        except Exception:
            pass
        # 建新表（临时名 → 数据搬移 → 替换）
        c.execute("DROP TABLE IF EXISTS devops_pipeline_scene_new")
        c.execute("""
CREATE TABLE devops_pipeline_scene_new (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    scene_id    TEXT NOT NULL,
    name        TEXT NOT NULL,
    description TEXT DEFAULT '',
    display_name TEXT DEFAULT '',
    repo_key    TEXT NOT NULL DEFAULT '',
    nodes_json  TEXT DEFAULT '[]',
    onboarding  TEXT DEFAULT 'repo-onboarding',
    is_active   INTEGER DEFAULT 1,
    created_at  TEXT DEFAULT (datetime('now')),
    updated_at  TEXT DEFAULT (datetime('now')),
    UNIQUE (scene_id, repo_key)
)""")
        _moved_repo = 0
        _moved_user = 0
        _dropped = 0
        for sid, name, desc, scope, rk, uid, nodes, onboarding, active in old_rows:
            nodes = nodes or "[]"
            onboarding = onboarding or "repo-onboarding"
            if scope == "repo":
                _insert_repo_scene(c, _strip_repo_prefix(sid), name, desc, rk or "", nodes, onboarding)
                _moved_repo += 1
            elif scope == "user":
                _rk_set = repo_of_user.get(uid) or set()
                if _rk_set:
                    for _rk in _rk_set:
                        _insert_repo_scene(c, _strip_repo_prefix(sid), name, desc, _rk, nodes, onboarding)
                    _moved_user += 1
                else:
                    _dropped += 1  # 孤儿 user 场景（无归属仓库），丢弃
            else:  # official：官方模板不保留
                _dropped += 1
        # 替换旧表
        c.execute("DROP TABLE devops_pipeline_scene")
        c.execute("ALTER TABLE devops_pipeline_scene_new RENAME TO devops_pipeline_scene")
        db.commit()
        log.info(
            f"Scene table migrated to repo model: repo_in={_moved_repo} user_in={_moved_user} dropped={_dropped}")


def _seed_official_scene_market_from_pipelines():
    """官方场景种子导入 scene_market（从 configs/pipelines.json，仅首次）。

    官方场景不再写入 devops_pipeline_scene（场景只与仓库关联）。
    这里把官方场景作为模板种入 scene_market(source='official')，供场景市场展示与仓库应用复刻。
    """
    try:
        from src.user.db import get_meta_value, set_meta_value
        if get_meta_value("scene_market_seeded") == "1":
            return
        _pp = os.path.join(_CONFIG_DIR, "pipelines.json")
        if not os.path.exists(_pp):
            return
        import json as _json
        with open(_pp, "r", encoding="utf-8") as _f:
            _pdata = _json.load(_f)
        _imported = 0
        with db_lock:
            c = db.cursor()
            for _sid, _scene in (_pdata.get("scenes") or {}).items():
                _nodes = _json.dumps(_scene.get("nodes", []), ensure_ascii=False)
                try:
                    c.execute(
                        "INSERT OR IGNORE INTO scene_market "
                        "(name, display_name, version, description, market_description, category_id, is_featured, like_count, source, source_user_id, recommended_template, nodes_json, onboarding, is_active) "
                        "VALUES (?,?, '1.0.0', '', ?, 1, 0, 0, 'official', 0, '', ?, ?, 1)",
                        (_sid, _scene.get("name", _sid), _scene.get("name", _sid),
                         _nodes, _scene.get("onboarding", "repo-onboarding")))
                    _imported += 1
                except Exception:
                    pass
            db.commit()
        if _imported:
            set_meta_value("scene_market_seeded", "1")
        log.info(f"Seeded {_imported} official scenes from pipelines.json into scene_market")
    except Exception as e:
        log.warning(f"Scene market seed import failed: {e}")


def _backfill_scene_display_names():
    """场景表 display_name 回填：按 scene_id 从 scene_market(source='official') 匹配展示名（幂等）。"""
    try:
        with db_lock:
            c = db.cursor()
            # 确保列存在（老库补充）
            _cols = {r[1] for r in c.execute("PRAGMA table_info(devops_pipeline_scene)").fetchall()}
            if _cols and "display_name" not in _cols:
                c.execute("ALTER TABLE devops_pipeline_scene ADD COLUMN display_name TEXT DEFAULT ''")
                db.commit()
                log.info("Added display_name column to devops_pipeline_scene")
            # market 映射 scene_id(name) -> display_name
            _map = {}
            try:
                for r in c.execute(
                        "SELECT DISTINCT name, display_name FROM scene_market "
                        "WHERE source='official' AND is_active=1 AND display_name<>''"):
                    _map[r[0]] = r[1]
            except Exception as _e:
                log.warning(f"_backfill_scene_display_names: read market failed: {_e}")
            # 回填：display_name 为空的行按 scene_id 匹配 market 的 name（官方场景标识符）
            _upd = 0
            for r in c.execute(
                    "SELECT id, scene_id, name, display_name FROM devops_pipeline_scene"):
                _row_id, _sid, _rname, _dn = r
                if _dn:
                    continue
                # scene_id 即官方场景标识符（如 default/iac），market.display_name 为其展示名
                _target = _map.get(_sid)
                if _target and _target != _sid:
                    c.execute("UPDATE devops_pipeline_scene SET display_name=? WHERE id=?", (_target, _row_id))
                    _upd += 1
            db.commit()
            if _upd:
                log.info(f"Backfilled {_upd} scene display_name from scene_market")
    except Exception as e:
        log.warning(f"Scene display_name backfill failed: {e}")


