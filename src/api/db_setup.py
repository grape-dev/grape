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
    entity_type TEXT DEFAULT '',
    created_at TEXT DEFAULT (datetime('now')),
    started_at TEXT DEFAULT '',
    completed_at TEXT DEFAULT ''
)
"""

_EVENT_SKIPPED_DDL = """
CREATE TABLE IF NOT EXISTS event_skipped (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    repo_key TEXT DEFAULT '',
    issue_id TEXT DEFAULT '',
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
    sub_scene TEXT DEFAULT '',
    category_id INTEGER DEFAULT 0,
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
    detached_from_scenes_at TEXT DEFAULT '',  -- 引擎离线超阈值被系统从场景绑定移除的时间（空=未处理，引擎绑定真实性同步方案）
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

# ===== Issue 任务卡片表（原 tasks，Phase B1 改名；结构不变，后续 B2 收编 issue_state 字段）=====
_ISSUE_DDL = """
CREATE TABLE IF NOT EXISTS issue (
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
    created_by       TEXT DEFAULT '',
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
    user_gitcode_token TEXT DEFAULT '',           -- chat 专用：用户级 GitCode token（互斥注入，不放 repo_gitcode_token）
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
    allow_scene_override INTEGER DEFAULT 0, -- 允许场景覆盖（B2 收编字段，issue_state 兼容）
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
    runtime_session_id TEXT DEFAULT '',          -- 引擎 --session 续接指针(Issue 级,opencode/hermes)
    session_engine     TEXT DEFAULT '',          -- 指针产出的引擎名(opencode/hermes/codearts/codex),跨引擎守卫用
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
    scope       TEXT NOT NULL,              -- baseline / project / issue / issue_agent / member
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
    updated_at   DATETIME DEFAULT CURRENT_TIMESTAMP,  -- 最近内容更新时间（OBS 同步审计）
    source       TEXT NOT NULL DEFAULT '',   -- 知识来源：system / agent / manual
    archived     INTEGER NOT NULL DEFAULT 0,  -- 0正常 1归档（归档后不注入）
    description  TEXT DEFAULT '',             -- 知识描述：system/agent 自动生成，manual 用户填写
    name         VARCHAR(256) DEFAULT ''      -- 知识名称：system/agent 自动生成，manual 用户必填
)
"""

# ===== scene_knowledge_ref：知识-场景-Agent 绑定关系表（Issue #67） =====
_SCENE_KNOWLEDGE_REF_DDL = """
CREATE TABLE IF NOT EXISTS scene_knowledge_ref (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    knowledge_id  INTEGER NOT NULL,                        -- agent_shared_knowledge.id
    scene_id      TEXT NOT NULL DEFAULT '',                 -- 场景UUID，空字符串=全部场景，禁止NULL
    agent_type    TEXT NOT NULL DEFAULT '',                 -- Agent类型，空字符串=全部Agent，禁止NULL
    issue_num     INTEGER NOT NULL DEFAULT 0,               -- Issue编号，0=全局（不限Issue），>0=只匹配该Issue
    enabled       INTEGER NOT NULL DEFAULT 1,               -- 1启用 0禁用
    created_by    TEXT NOT NULL DEFAULT '',                 -- 创建人
    created_at    TEXT NOT NULL DEFAULT (datetime('now')),  -- 创建时间
    updated_at    TEXT NOT NULL DEFAULT (datetime('now')),  -- 更新时间
    updated_by    TEXT NOT NULL DEFAULT '',                 -- 最后修改人
    UNIQUE (knowledge_id, scene_id, agent_type, issue_num)
)
"""

# MySQL 原生 DDL（不走翻译层，生产环境用）
_SCENE_KNOWLEDGE_REF_MYSQL_DDL = """
CREATE TABLE IF NOT EXISTS scene_knowledge_ref (
    id            BIGINT UNSIGNED AUTO_INCREMENT PRIMARY KEY COMMENT '主键ID',
    knowledge_id  BIGINT UNSIGNED NOT NULL COMMENT 'agent_shared_knowledge主键',
    scene_id      VARCHAR(64) NOT NULL DEFAULT '' COMMENT '场景UUID，空字符串代表全部场景，禁止NULL',
    agent_type    VARCHAR(64) NOT NULL DEFAULT '' COMMENT 'Agent类型，空字符串代表全部Agent，禁止NULL',
    issue_num     INT NOT NULL DEFAULT 0 COMMENT 'Issue编号，0=全局不限Issue，>0=只匹配该Issue',
    enabled       TINYINT NOT NULL DEFAULT 1 COMMENT '1启用 0禁用',
    created_by    VARCHAR(64) NOT NULL DEFAULT '' COMMENT '创建人',
    created_at    DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间',
    updated_at    DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP COMMENT '更新时间，自动刷新',
    updated_by    VARCHAR(64) NOT NULL DEFAULT '' COMMENT '最后修改人',
    UNIQUE KEY uk_knowledge_scene_agent_issue (knowledge_id, scene_id, agent_type, issue_num),
    KEY idx_query (enabled, scene_id, agent_type, issue_num),
    CONSTRAINT fk_scene_knowledge_ref_knowledge_id
        FOREIGN KEY (knowledge_id) REFERENCES agent_shared_knowledge(id)
        ON DELETE CASCADE
        ON UPDATE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='知识库-场景-Agent绑定关系'
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
    scene_type  TEXT DEFAULT 'standard',  -- standard | pr_subscription
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
    gitcode_username VARCHAR(64) DEFAULT NULL,
    remark VARCHAR(128) DEFAULT '',
    created_by VARCHAR(64) DEFAULT '',
    created_at TEXT DEFAULT (datetime('now')),
    github_username TEXT DEFAULT '',
    UNIQUE (gitcode_username)
)
"""

_API_LATENCY_LOG_DDL = """
CREATE TABLE IF NOT EXISTS api_latency_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    request_id VARCHAR(32) DEFAULT '',
    method VARCHAR(8) DEFAULT '',
    path VARCHAR(256) DEFAULT '',
    status INTEGER DEFAULT 0,
    duration_ms INTEGER DEFAULT 0,
    repo_key VARCHAR(128) DEFAULT '',
    user_id VARCHAR(32) DEFAULT '',
    source VARCHAR(16) DEFAULT 'backend',
    created_at TEXT DEFAULT (datetime('now'))
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

_SKILL_CATEGORY_DDL = """
CREATE TABLE IF NOT EXISTS skill_category (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT NOT NULL,
    slug        VARCHAR(64) NOT NULL,
    icon        TEXT DEFAULT '',
    order_num   INTEGER DEFAULT 0,
    is_active   INTEGER DEFAULT 1,
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
    preset_custom_fields TEXT DEFAULT '[]',  -- 场景预置自定义参数声明（scene.json custom_fields 数组，Issue #246）
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

# 引擎绑定真实性同步：场景引擎解绑审计表（引擎离线超 8h 系统自动移除场景绑定的留痕）
_SCENE_ENGINE_DETACH_LOG_DDL = """
CREATE TABLE IF NOT EXISTS scene_engine_detach_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    scene_row_id INTEGER NOT NULL,        -- devops_pipeline_scene.id
    repo_key    TEXT DEFAULT '',
    scene_id    TEXT DEFAULT '',          -- 场景 scene_id（非 market id）
    node_tag    TEXT DEFAULT '',          -- 被移除绑定的节点标签（coding/qa/sre…）
    runtime_id  INTEGER NOT NULL,         -- 被移除的引擎 id
    runtime_name TEXT DEFAULT '',         -- 引擎名（快照，防 runtime 行后续变更）
    detached_at TEXT DEFAULT (datetime('now')),
    reason      TEXT DEFAULT '',          -- 触发原因（offline_timeout 等）
    affected_autopilot_tasks TEXT DEFAULT '[]',  -- 仍绑定该引擎的巡检任务 [{id,name}]（两表协同提示用）
    rebound_at  TEXT DEFAULT ''           -- 用户重绑时间（非空=不再提示）
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

# ===== Issue #150: Autopilot 定时自动化任务（§5.3.1 + §5.3.2 + §5.3.3）=====
_AUTOPILOT_TASKS_DDL = """
CREATE TABLE IF NOT EXISTS autopilot_tasks (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    name            TEXT NOT NULL,                       -- 任务名称
    repo_key        TEXT NOT NULL DEFAULT '',            -- 仓库标识（gitcode:owner/repo）
    agent_type      TEXT NOT NULL DEFAULT 'issue',       -- 执行哪个agent（issue/qa/sre）
    instruction     TEXT NOT NULL DEFAULT '',            -- 巡检指令（注入prompt）
    skill_name      TEXT NOT NULL DEFAULT '',            -- 可选skill（空=不用skill）
    cron            TEXT NOT NULL DEFAULT '*/30 * * * *', -- cron 表达式（5 段制）
    max_retry       INTEGER NOT NULL DEFAULT 3,          -- 最大重试次数
    enabled         INTEGER NOT NULL DEFAULT 1,          -- 1=启用 0=禁用
    last_run_at     TEXT DEFAULT '',                     -- 最近触发时间
    next_run_at     TEXT DEFAULT '',                     -- 下次预计触发时间
    created_at      TEXT DEFAULT (datetime('now')),
    updated_at      TEXT DEFAULT (datetime('now')),
    created_by      INTEGER DEFAULT 0                    -- 创建者 user_id
)
"""

_AUTOPILOT_EXECUTIONS_DDL = """
CREATE TABLE IF NOT EXISTS autopilot_executions (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id         INTEGER NOT NULL,                    -- 外键 -> autopilot_tasks.id
    status          TEXT NOT NULL DEFAULT 'pending',     -- pending/running/success/failed/retrying
    retry_count     INTEGER NOT NULL DEFAULT 0,          -- 已重试次数
    result          MEDIUMTEXT DEFAULT '',               -- 执行结果摘要
    error           MEDIUMTEXT DEFAULT '',               -- 错误信息
    a2a_seq         INTEGER DEFAULT NULL,                -- 关联 agent_tasks.event_seq（=exec_id）
    started_at      TEXT DEFAULT '',                     -- 开始时间
    completed_at    TEXT DEFAULT '',                     -- 完成时间
    created_at      TEXT DEFAULT (datetime('now')),
    -- v3.0 新增字段
    agent_type      TEXT DEFAULT '',                     -- 执行快照
    findings_json   MEDIUMTEXT DEFAULT ''                -- 问题列表JSON
)
"""

_ADMIN_ACCOUNTS_TABLE_DDL = """
CREATE TABLE IF NOT EXISTS admin_accounts (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    username      TEXT UNIQUE NOT NULL,
    password_hash TEXT NOT NULL DEFAULT '',
    is_super      INTEGER NOT NULL DEFAULT 1,
    token_version INTEGER NOT NULL DEFAULT 0,
    created_at    TEXT DEFAULT (datetime('now')),
    last_login_at TEXT DEFAULT NULL
)
"""

_NEW_TABLES = [
    ("admin_accounts", _ADMIN_ACCOUNTS_TABLE_DDL),
    ("meta", _META_TABLE_DDL),
    ("agent_runtimes", _AGENT_RUNTIMES_DDL),
    ("agent_tasks", _AGENT_TASKS_DDL),
    ("task_messages", _TASK_MESSAGES_DDL),
    ("agent_type_runtime_pool", _AGENT_TYPE_RUNTIME_POOL_DDL),
    ("runtime_credentials", _RUNTIME_CREDENTIALS_DDL),
    # issue_state: 保留创建（session_store.py 兜底读取 + 测试引用），
    # C5 DROP 条件性跳过（生产环境确认无孤儿后才删，测试环境保留）
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
    ("issue", _ISSUE_DDL),
    ("event_thread", _EVENT_THREAD_DDL),
    ("agent_shared_knowledge", _AGENT_SHARED_KNOWLEDGE_DDL),
    ("scene_knowledge_ref", _SCENE_KNOWLEDGE_REF_DDL),
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
    # 引擎绑定真实性同步：场景引擎解绑审计表
    ("scene_engine_detach_log", _SCENE_ENGINE_DETACH_LOG_DDL),
    ("skill_likes", _SKILL_LIKES_DDL),
    ("skill_category", _SKILL_CATEGORY_DDL),
    ("uploaded_files", _UPLOADED_FILES_DDL),
    ("distributed_locks", _DISTRIBUTED_LOCKS_DDL),
    ("api_latency_log", _API_LATENCY_LOG_DDL),
    # ===== Issue-45: API Token 认证表 =====
    ("session_api_tokens", """
CREATE TABLE IF NOT EXISTS session_api_tokens (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_key TEXT NOT NULL,
    repo_key TEXT NOT NULL DEFAULT '',
    agent_type TEXT NOT NULL DEFAULT '',
    user_id INTEGER NOT NULL DEFAULT 0,
    token_sha256 TEXT NOT NULL,
    expires_at INTEGER NOT NULL DEFAULT 0
)"""),
    ("api_tokens", """
CREATE TABLE IF NOT EXISTS api_tokens (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    token_sha256 TEXT NOT NULL,
    name TEXT NOT NULL DEFAULT '',
    scopes TEXT NOT NULL DEFAULT '["public"]',
    revoked INTEGER NOT NULL DEFAULT 0,
    created_at TEXT DEFAULT (datetime('now')),
    last_used_at TEXT DEFAULT ''
)"""),
    # ===== legacy: backend_agents（_migrate_tables rebuild 依赖其存在）=====
    ("backend_agents", _BACKEND_AGENTS_DDL),
    # ===== Issue #103: 知识写入队列 =====
    ("knowledge_write_queue", """
CREATE TABLE IF NOT EXISTS knowledge_write_queue (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    repo_key      TEXT NOT NULL,
    scope         TEXT NOT NULL,
    agent_type    TEXT NOT NULL DEFAULT '',
    issue_num     INTEGER DEFAULT 0,
    content       TEXT NOT NULL,
    status        TEXT DEFAULT 'accepted',
    source_seq    INTEGER DEFAULT 0,
    created_by    TEXT DEFAULT '',
    source        TEXT DEFAULT '',
    description   TEXT DEFAULT '',
    name          TEXT DEFAULT '',
    queue_status  TEXT NOT NULL DEFAULT 'pending',
    retry_count   INTEGER NOT NULL DEFAULT 0,
    last_error    TEXT NOT NULL DEFAULT '',
    next_retry_at TEXT NOT NULL DEFAULT (datetime('now')),
    created_at    TEXT DEFAULT (datetime('now')),
    updated_at      TEXT DEFAULT (datetime('now'))
)"""""),    # ===== Issue #134: 机器健康度持久化表 =====
    ("runtime_health_scores", """
CREATE TABLE IF NOT EXISTS runtime_health_scores (
    runtime_id          INTEGER PRIMARY KEY,          -- agent_runtimes.id
    health_score        INTEGER NOT NULL DEFAULT 100,  -- 0-100, 越高越健康
    total_tasks         INTEGER NOT NULL DEFAULT 0,    -- 近 N 天总任务数
    failed_tasks        INTEGER NOT NULL DEFAULT 0,    -- 近 N 天失败数
    completed_tasks     INTEGER NOT NULL DEFAULT 0,    -- 近 N 天完成数
    consecutive_failures INTEGER NOT NULL DEFAULT 0,   -- 连续失败次数
    failure_rate        REAL NOT NULL DEFAULT 0.0,     -- 失败率 (0-1)
    last_status         TEXT DEFAULT '',               -- 最近一条任务状态
    window_days         INTEGER NOT NULL DEFAULT 7,    -- 滑动窗口天数
    updated_at          TEXT DEFAULT (datetime('now'))
)"""),
    # ===== Issue #174: 用户问题反馈表（grape_db）=====
    ("user_feedback", """
CREATE TABLE IF NOT EXISTS user_feedback (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id         INTEGER      NOT NULL DEFAULT 0,
    username        TEXT         NOT NULL DEFAULT '',
    category        TEXT         NOT NULL DEFAULT 'bug',
    title           TEXT         NOT NULL,
    description     TEXT         NOT NULL,
    page_url        TEXT         DEFAULT '',
    repo_key        TEXT         DEFAULT '',
    user_agent      TEXT         DEFAULT '',
    attachment_url  TEXT         DEFAULT '',
    attachment_name TEXT         DEFAULT '',
    status          TEXT         NOT NULL DEFAULT 'open',
    priority        INTEGER      NOT NULL DEFAULT 0,
    handler_name    TEXT         DEFAULT '',
    handler_remark  TEXT         DEFAULT '',
    handled_at      TEXT         DEFAULT NULL,
    is_deleted      INTEGER      NOT NULL DEFAULT 0,
    created_at      TEXT         DEFAULT (datetime('now')),
    updated_at      TEXT         DEFAULT (datetime('now'))
)"""),
    # ===== Issue #150: Autopilot 定时自动化任务表 =====
    ("autopilot_tasks", _AUTOPILOT_TASKS_DDL),
    ("autopilot_executions", _AUTOPILOT_EXECUTIONS_DDL),
]

# ===== Organization（组织）特性 =====
_ORGANIZATION_DDL = """
CREATE TABLE IF NOT EXISTS organization (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    name            VARCHAR(128) NOT NULL,
    slug            VARCHAR(64) NOT NULL UNIQUE,
    description     TEXT DEFAULT '',
    avatar_url      VARCHAR(512) DEFAULT '',
    owner_id        INTEGER NOT NULL,
    is_personal     INTEGER DEFAULT 0,
    settings        TEXT DEFAULT '{}',
    max_members     INTEGER DEFAULT 50,
    max_workers     INTEGER DEFAULT 10,
    created_at      TEXT DEFAULT (datetime('now')),
    updated_at      TEXT DEFAULT (datetime('now'))
)"""

_ORG_MEMBER_DDL = """
CREATE TABLE IF NOT EXISTS org_member (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    org_id          INTEGER NOT NULL,
    user_id         INTEGER NOT NULL,
    role            TEXT NOT NULL DEFAULT 'member',
    invited_by      INTEGER DEFAULT 0,
    joined_at       TEXT DEFAULT (datetime('now')),
    UNIQUE (org_id, user_id)
)"""

_ORG_INVITATION_DDL = """
CREATE TABLE IF NOT EXISTS org_invitation (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    org_id          INTEGER NOT NULL,
    inviter_id      INTEGER NOT NULL,
    invitee_email   TEXT DEFAULT '',
    invitee_user_id INTEGER DEFAULT 0,
    role            TEXT NOT NULL DEFAULT 'member',
    status          TEXT DEFAULT 'pending',
    expires_at      TEXT,
    created_at      TEXT DEFAULT (datetime('now')),
    updated_at      TEXT DEFAULT (datetime('now'))
)"""

_NEW_TABLES.extend([
    ("organization", _ORGANIZATION_DDL),
    ("org_member", _ORG_MEMBER_DDL),
    ("org_invitation", _ORG_INVITATION_DDL),
])

# 需要 org_id 列的核心表
_ORG_ID_TABLES = [
    "managed_repos",
    "runtime_credentials",
    "agent_runtimes",
    "skill",
    "devops_pipeline_scene",
    "scene_market",
    "agent_shared_knowledge",
    "events",
    "agent_tasks",
    "devops_agent_session",
    "chat_conversations",
    "issue",
    "session_api_tokens",
]


def _migrate_add_org_id_columns():
    """为核心表幂等添加 org_id 列（0=全局/未归属，兼容现有数据）。"""
    try:
        with db_lock:
            c = db.cursor()
            for table in _ORG_ID_TABLES:
                _ensure_column(c, table, "org_id", "INTEGER DEFAULT 0")
            db.commit()
            log.info("org_id columns ensured on all core tables")
    except Exception as e:
        log.warning(f"org_id column migration failed: {e}")


def _migrate_create_personal_orgs():
    """为每个现有用户创建个人组织（幂等）。"""
    try:
        with db_lock:
            c = db.cursor()
            users = c.execute("SELECT id, username FROM user_accounts").fetchall()
            for uid, username in users:
                existing = c.execute(
                    "SELECT id FROM organization WHERE owner_id=? AND is_personal=1", (uid,)
                ).fetchone()
                if existing:
                    continue
                slug = f"personal-{uid}"
                c.execute(
                    "INSERT INTO organization (name, slug, owner_id, is_personal) VALUES (?, ?, ?, 1)",
                    ("默认工作空间", slug, uid)
                )
                org_id = c.lastrowid
                c.execute(
                    "INSERT OR IGNORE INTO org_member (org_id, user_id, role) VALUES (?, ?, 'owner')",
                    (org_id, uid)
                )
            db.commit()
            log.info("Personal organizations ensured for all users")
    except Exception as e:
        log.warning(f"Personal org creation migration failed: {e}")


def _migrate_data_to_personal_orgs():
    """将现有数据迁入对应用户的个人组织（幂等，只处理 org_id=0 的行）。"""
    try:
        with db_lock:
            c = db.cursor()

            # 1. managed_repos：按 user_repos.is_admin 找管理员迁入
            repos = c.execute(
                "SELECT repo_key FROM managed_repos WHERE org_id=0 OR org_id IS NULL"
            ).fetchall()
            for (repo_key,) in repos:
                admin = c.execute(
                    "SELECT user_id FROM user_repos WHERE repo_key=? AND is_admin=1 LIMIT 1",
                    (repo_key,)
                ).fetchone()
                if admin:
                    uid = admin[0]
                    org = c.execute(
                        "SELECT id FROM organization WHERE owner_id=? AND is_personal=1", (uid,)
                    ).fetchone()
                    if org:
                        c.execute("UPDATE managed_repos SET org_id=? WHERE repo_key=?", (org[0], repo_key))

            # 2. runtime_credentials：scope=user 的按 owner_name 迁入
            creds = c.execute(
                "SELECT id, owner_name, scope FROM runtime_credentials WHERE org_id=0 OR org_id IS NULL"
            ).fetchall()
            for cid, owner_name, scope in creds:
                if not owner_name or scope == "global":
                    continue
                user = c.execute(
                    "SELECT id FROM user_accounts WHERE username=?", (owner_name,)
                ).fetchone()
                if user:
                    uid = user[0]
                    org = c.execute(
                        "SELECT id FROM organization WHERE owner_id=? AND is_personal=1", (uid,)
                    ).fetchone()
                    if org:
                        c.execute("UPDATE runtime_credentials SET org_id=? WHERE id=?", (org[0], cid))
                        c.execute("UPDATE agent_runtimes SET org_id=? WHERE credential_id=?", (org[0], cid))

            # 3. skill：scope=user 的按 owner_id 迁入
            skills = c.execute(
                "SELECT id, owner_id, scope FROM skill WHERE scope='user' AND (org_id=0 OR org_id IS NULL)"
            ).fetchall()
            for sid, owner_id, _ in skills:
                try:
                    uid = int(owner_id)
                except (ValueError, TypeError):
                    continue
                org = c.execute(
                    "SELECT id FROM organization WHERE owner_id=? AND is_personal=1", (uid,)
                ).fetchone()
                if org:
                    c.execute("UPDATE skill SET org_id=? WHERE id=?", (org[0], sid))

            # 4. skill：scope=repo 的跟随 repo_key 迁移
            repo_skills = c.execute(
                "SELECT id, owner_id FROM skill WHERE scope='repo' AND (org_id=0 OR org_id IS NULL)"
            ).fetchall()
            for sid, owner_id in repo_skills:
                repo_org = c.execute(
                    "SELECT org_id FROM managed_repos WHERE repo_key=?", (owner_id,)
                ).fetchone()
                if repo_org and repo_org[0]:
                    c.execute("UPDATE skill SET org_id=? WHERE id=?", (repo_org[0], sid))

            # 5. 跟随 repo_key 的表：devops_pipeline_scene / agent_shared_knowledge /
            #    events / agent_tasks / devops_agent_session / issue
            for table in ["devops_pipeline_scene", "agent_shared_knowledge", "events",
                           "agent_tasks", "devops_agent_session", "issue"]:
                try:
                    c.execute(f"""
                        UPDATE {table} SET org_id = (
                            SELECT m.org_id FROM managed_repos m
                            WHERE m.repo_key = {table}.repo_key AND m.org_id > 0
                        )
                        WHERE (org_id = 0 OR org_id IS NULL)
                          AND repo_key IN (SELECT repo_key FROM managed_repos WHERE org_id > 0)
                    """)
                except Exception as _e:
                    log.warning(f"org_id backfill for {table} failed: {_e}")

            db.commit()
            log.info("Data migrated to personal organizations")
    except Exception as e:
        log.warning(f"Data migration to personal orgs failed: {e}")


_CONFIG_DIR = os.path.join(os.path.dirname(__file__), "..", "..", "configs")

# (seed_key, json_filename, actual_table_name)
_SEED_FILES = [
    ("agent_type", "agent-description.json", "agent_type"),
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
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_tasks_repo_issue ON issue(repo_key, issue_number)",
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
        "CREATE INDEX IF NOT EXISTS idx_skill_category_id ON skill(category_id)",
        "CREATE INDEX IF NOT EXISTS idx_market_scene_id ON devops_pipeline_scene(market_scene_id)",
        # ===== API 慢请求监控索引 =====
        "CREATE INDEX IF NOT EXISTS idx_latency_log_created ON api_latency_log(created_at)",
        "CREATE INDEX IF NOT EXISTS idx_latency_log_path ON api_latency_log(path)",
        "CREATE INDEX IF NOT EXISTS idx_latency_log_duration ON api_latency_log(duration_ms)",
        "CREATE INDEX IF NOT EXISTS idx_latency_log_source ON api_latency_log(source)",
        # ===== Issue #103: 知识写入队列索引 =====
        "CREATE INDEX IF NOT EXISTS idx_kwq_status_retry ON knowledge_write_queue(queue_status, next_retry_at)",
        "CREATE INDEX IF NOT EXISTS idx_kwq_dim_created ON knowledge_write_queue(repo_key, scope, agent_type, issue_num, created_at)",
        "CREATE INDEX IF NOT EXISTS idx_kwq_source_seq ON knowledge_write_queue(repo_key, source_seq)",
        # ===== Issue #150: Autopilot 索引 =====
        "CREATE INDEX IF NOT EXISTS idx_autopilot_tasks_enabled ON autopilot_tasks(enabled)",
        "CREATE INDEX IF NOT EXISTS idx_autopilot_tasks_repo ON autopilot_tasks(repo_key)",
        "CREATE INDEX IF NOT EXISTS idx_autopilot_exec_task ON autopilot_executions(task_id)",
        "CREATE INDEX IF NOT EXISTS idx_autopilot_exec_status ON autopilot_executions(status)",
    ]
    # autopilot_tasks.created_by 迁移
    try:
        with db_lock:
            c = db.cursor()
            _ensure_column(c, "autopilot_tasks", "created_by", "INTEGER DEFAULT 0")
            db.commit()
    except Exception as e:
        log.warning(f"autopilot_tasks created_by migration failed: {e}")
    # ===== issue 表 (repo_key, issue_number) 去重（保证下方唯一索引可建，2026-10-06）=====
    # 背景：ensure_issue_task 此前仅复用 active/processing 卡，awaiting_input/completed/cancelled 时
    # 后续事件会新建一行 → 同一平台 issue 多卡（需求中心并排出现多张卡）。此处兜底删除多余行，
    # 保留执行中最有意义的一张（processing > awaiting_input/active > pending_review > completed > cancelled，
    # 同级取 updated_at 最新、id 最小），随后创建唯一索引从根上防重。
    try:
        with db_lock:
            c = db.cursor()
            _dup_groups = c.execute(
                "SELECT repo_key, issue_number FROM issue WHERE issue_number>0 "
                "GROUP BY repo_key, issue_number HAVING COUNT(*)>1").fetchall()
            _del = 0
            for _rk, _num in _dup_groups:
                _rows = c.execute(
                    "SELECT id, status, updated_at FROM issue WHERE repo_key=? AND issue_number=? ORDER BY id",
                    (_rk, _num)).fetchall()
                if len(_rows) < 2:
                    continue
                def _pri(_st):
                    return {"processing": 5, "awaiting_input": 4, "active": 4, "pending_review": 3,
                            "completed": 2, "cancelled": 1}.get(_st or "", 0)
                _keep = max(_rows, key=lambda r: (_pri(r[1]), r[2] or "", r[0]))
                for _r in _rows:
                    if _r[0] != _keep[0]:
                        c.execute("DELETE FROM issue WHERE id=?", (_r[0],))
                        _del += 1
            if _del:
                db.commit()
                log.info(f"issue dedupe: removed {_del} duplicate row(s) before unique index")
    except Exception as e:
        log.warning(f"issue dedupe cleanup failed: {e}")

    # 确保 idx_tasks_repo_issue 为 UNIQUE（SQLite/MySQL 双栈）：历史可能存在同名**普通**索引，
    # `CREATE UNIQUE INDEX IF NOT EXISTS` 遇同名不会升级 → 先 DROP 非唯一同名索引，由下方循环重建为唯一。
    try:
        with db_lock:
            c = db.cursor()
            for _r in c.execute("PRAGMA index_list('issue')").fetchall():
                if _r[1] == "idx_tasks_repo_issue" and not _r[2]:
                    c.execute("DROP INDEX idx_tasks_repo_issue")
                    db.commit()
                    log.info("Dropped non-unique idx_tasks_repo_issue for unique re-create")
                    break
    except Exception as e:
        log.warning(f"ensure unique idx_tasks_repo_issue prep failed: {e}")

    for idx in indexes:
        try:
            with db_lock:
                db.execute(idx)
                db.commit()
        except Exception as e:
            log.warning(f"Create index failed: {e}")

    # idx_tasks_repo_issue 升级为唯一索引（防止 issue 表重复行）
    # 已有同名普通索引时，先 DROP 再 CREATE UNIQUE
    try:
        with db_lock:
            c = db.cursor()
            _need_upgrade = False
            try:
                c.execute("SELECT NON_UNIQUE FROM information_schema.statistics "
                          "WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME='issue' AND INDEX_NAME='idx_tasks_repo_issue' LIMIT 1")
                row = c.fetchone()
                _need_upgrade = bool(row and row[0] == 1)
            except Exception:
                pass  # SQLite 无 information_schema，跳过升级检查
            if _need_upgrade:
                c.execute("DROP INDEX idx_tasks_repo_issue ON issue")
                c.execute("CREATE UNIQUE INDEX idx_tasks_repo_issue ON issue (repo_key, issue_number)")
                db.commit()
                log.info("Upgraded idx_tasks_repo_issue to UNIQUE")
    except Exception as e:
        log.warning(f"Upgrade idx_tasks_repo_issue to UNIQUE failed: {e}")


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
            c.execute(f"SELECT COUNT(*) FROM {db_table}")
            count = c.fetchone()[0]
            if count > 0 and db_table != "agent_type":
                # 表已有数据，跳过 seed
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
        # pull_request_updated（Issue #49 补充：normalize 细分后，update/审查类事件走这里；
        # 无新 commit 的 update（审查/reopen/元数据）由 event_rule 的 no_commit 门控过滤）
        ("pull_request_updated", "open",   1, "any", "", "", ""),
        ("pull_request_updated", "update", 1, "user", "", "", ""),
        ("pull_request_updated", "close",  0, "any", "", "", ""),
        ("pull_request_updated", "reopen", 0, "any", "", "", ""),
        # pull_request_review / pull_request_closed（multi-vcs 细分后独立事件，默认不触发执行 agent）
        ("pull_request_review", "review",  0, "any", "", "", ""),
        ("pull_request_review", "approve", 0, "any", "", "", ""),
        ("pull_request_closed", "close",   0, "any", "", "", ""),
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
            ("云原生", "cloud-native", "☁", 3),
            ("IaC", "iac", "🏗️", 4),
            ("技能开发", "skill-dev", "🔧", 5),
            ("AI应用", "ai", "🤖", 6),
            ("内容创作", "content", "📝", 7),
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


def _init_skill_category_data():
    """初始化 skill_category 表的 15 条分类数据（幂等）。

    Issue #179: 技能分类表独立化，对齐 scene_category 模式。
    Issue #222: 新增 code-hosting/browser-verify/meeting/other 4 个分类。
    """
    try:
        from src.user.db import get_meta_value, set_meta_value
        if get_meta_value("skill_category_seeded_v2") == "1":
            return
        categories = [
            ("AI辅助编码与评审", "ai-coding-review", "🤖", 1),
            ("应用发布", "app-release", "📦", 2),
            ("自动化测试", "auto-testing", "🧪", 3),
            ("持续集成CI", "ci", "🔧", 4),
            ("代码质量与安全", "code-quality", "🛡️", 5),
            ("容器与集群部署", "container-deploy", "☸️", 6),
            ("反馈闭环", "feedback-loop", "🔄", 7),
            ("IM", "im", "💬", 8),
            ("日志与诊断", "log-diagnostics", "🔍", 9),
            ("可观测性", "observability", "📊", 10),
            ("环境与脚手架", "scaffolding", "🏗️", 11),
            ("代码托管", "code-hosting", "📁", 12),
            ("浏览器验证", "browser-verify", "🌐", 13),
            ("会议", "meeting", "📅", 14),
            ("其他", "other", "📌", 15),
        ]
        with db_lock:
            c = db.cursor()
            for name, slug, icon, order_num in categories:
                c.execute(
                    "INSERT OR IGNORE INTO skill_category (name, slug, icon, order_num, is_active) VALUES (?,?,?,?,1)",
                    (name, slug, icon, order_num),
                )
            db.commit()
        set_meta_value("skill_category_seeded_v2", "1")
        log.info("Seeded skill_category with 15 categories")
    except Exception as e:
        log.warning(f"Init skill_category data failed: {e}")


def _backfill_skill_category_id():
    """一次性回填：sub_scene → category_id（幂等）。

    Issue #179: 已有 official 技能的 sub_scene 有值，通过 slug 关联 skill_category 获取 category_id。
    user/repo 技能 sub_scene 为空，category_id 保持 0。
    """
    try:
        with db_lock:
            c = db.cursor()
            c.execute(
                "UPDATE skill SET category_id = "
                "(SELECT sc.id FROM skill_category sc WHERE sc.slug = skill.sub_scene) "
                "WHERE sub_scene != '' AND category_id = 0"
            )
            updated = c.rowcount
            db.commit()
        if updated:
            log.info(f"Backfilled skill category_id: {updated} rows")
    except Exception as e:
        log.warning(f"Backfill skill category_id failed: {e}")


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
                ("iac", "IaC开发", 4, 1, "tf-infra", "IaC 基础设施即代码开发场景，使用 Terraform 管理云资源", "iac"),
                ("skill-dev", "华为云Skill开发", 5, 1, "hello-skill", "华为云 Skill 开发场景，创建和发布华为云技能", "skill-dev"),
                ("cloud-native", "云原生开发", 3, 0, "micro-svc", "云原生开发场景，适合微服务和容器化项目", "cloud-native"),
                ("agent-dev", "Agent开发", 6, 0, "ai-interview-swarm", "Agent 开发场景，适合构建 AI Agent 项目", "agent-dev"),
                ("api-skill-dev", "通用Skill开发", 5, 0, "hello-skill", "通用 Skill 开发场景，创建和发布通用技能", "api-skill-dev"),
                ("blog-publish", "博客发布", 7, 0, "", "博客系统开发与发布场景", "blog-publish"),
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


def _seed_admin_accounts():
    """首次部署种子 admin 账号（admin/admin123）+ meta 标记。

    - 仅当 admin_accounts 为空时插入默认账号 admin；密码优先级：
      环境变量 GRAPE_ADMIN_PASSWORD > 默认 'admin123'
    - meta 标记：
      admin_initialized  = 1（账号已种子，防重复）
      admin_pwd_initial  = 1（仍为默认密码，前端引导改密；改密后置 0）
    - admin 账号与 user_accounts 完全独立，不影响用户登录
    """
    import hashlib
    from src.user.db import get_meta_value, set_meta_value
    _default_pwd = os.environ.get("GRAPE_ADMIN_PASSWORD") or "admin123"
    try:
        with db_lock:
            c = db.cursor()
            c.execute("SELECT COUNT(*) FROM admin_accounts")
            if c.fetchone()[0] == 0:
                _pw_hash = hashlib.sha256(_default_pwd.encode()).hexdigest()
                c.execute("INSERT INTO admin_accounts (username, password_hash, is_super, token_version) VALUES (?,?,1,0)",
                          ("admin", _pw_hash))
                db.commit()
                _src = "GRAPE_ADMIN_PASSWORD env" if os.environ.get("GRAPE_ADMIN_PASSWORD") else "default admin123"
                log.info(f"Seeded default admin account: admin (password from {_src})")
        set_meta_value("admin_initialized", "1")
        if not get_meta_value("admin_pwd_initial"):
            set_meta_value("admin_pwd_initial", "1" if _default_pwd == "admin123" else "0")
    except Exception as e:
        log.warning(f"Seed admin account failed: {e}")


def init_database():
    """初始化所有新表(幂等),启动时调用"""
    # 迁移前 DB 自动备份保留策略兜底（2026-10 磁盘告警：data/backups 曾堆到 63G 打满根盘）
    # 每次启动清理过量 pre-m0 备份，保留 DB_BACKUP_KEEP(默认5) 份
    try:
        from src.session_manager import prune_db_backups, _db_path
        prune_db_backups(_db_path)
    except Exception as _pb:
        log.warning(f"DB backup prune skipped: {_pb}")

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
                _tasks_to_issue_rename_pre_create()
                _create_tables()
                _seed_admin_accounts()
                _migrate_tables()
                _create_indexes()
                _seed_from_json()
                _seed_event_action_filter()
                _align_agent_enable()
                _cleanup_stale_meta()
                _cleanup_chat_shared_worker_runtime()
                _migrate_whitelist_from_file()
                _cleanup_old_latency_logs()
                # ===== Issue-11: DevOps 市场数据初始化 =====
                _init_scene_category_data()
                _init_scene_market_data()
                _init_scene_skill_ref()
                _init_skill_featured()
                # ===== Issue #179: skill 分类表初始化 + category_id 回填 =====
                _init_skill_category_data()
                _backfill_skill_category_id()
                # ===== Issue-39: 场景 display_name 回填（market 数据就绪后再回填）=====
                try:
                    _backfill_scene_display_names()
                except Exception as _e:
                    log.warning(f"Scene display_name backfill failed: {_e}")
                # ===== 知识库去重 + ref 迁移（每次启动执行，幂等）=====
                try:
                    _migrate_knowledge_dedup_and_ref()
                except Exception as _e:
                    log.warning(f"Issue #67 knowledge migration failed: {_e}")
            finally:
                c.execute("SELECT RELEASE_LOCK('grape_migrate')")
    else:
        # SQLite——单实例不需要锁
        _tasks_to_issue_rename_pre_create()
        _create_tables()
        _seed_admin_accounts()
        _migrate_tables()
        _create_indexes()
        _seed_from_json()
        _seed_event_action_filter()
        _align_agent_enable()
        _cleanup_stale_meta()
        _cleanup_chat_shared_worker_runtime()
        _migrate_whitelist_from_file()
        _cleanup_old_latency_logs()
        # ===== Issue-11: DevOps 市场数据初始化 =====
        _init_scene_category_data()
        _init_scene_market_data()
        _init_scene_skill_ref()
        _init_skill_featured()
        # ===== Issue #179: skill 分类表初始化 + category_id 回填 =====
        _init_skill_category_data()
        _backfill_skill_category_id()
        # ===== Issue-39: 场景 display_name 回填（market 数据就绪后再回填）=====
        try:
            _backfill_scene_display_names()
        except Exception as _e:
            log.warning(f"Scene display_name backfill failed: {_e}")
        # ===== Issue #67: 知识库优化迁移 =====
        try:
            _migrate_knowledge_dedup_and_ref()
        except Exception as _e:
            log.warning(f"Issue #67 knowledge migration failed: {_e}")
    # MySQL with session_manager

def _migrate_knowledge_dedup_and_ref():
    """知识库 source 回填 + name/description 回填 + 重复记录去重 + scene_knowledge_ref 数据迁移 + MySQL 原生 DDL。

    每次启动执行（幂等）。issue95 修复部分有独立幂等标记。
    步骤顺序：source 回填 → name/description 回填 → 去重 → ref 迁移 → 孤儿 ref 清理。
    """
    from src.mysql_compat import get_db_backend
    _is_mysql = get_db_backend() == "mysql"

    # Issue #95：检查 #95 修复是否已执行过
    _issue95_done = False
    with db_lock:
        try:
            row = db.cursor().execute(
                "SELECT value FROM meta WHERE key='issue95_migrate_done'"
            ).fetchone()
            _issue95_done = bool(row and row[0] == '1')
        except Exception:
            pass

    with db_lock:
        c = db.cursor()

        # 1. source 字段回填（幂等——只更新 source 为空的记录）
        c.execute(
            "UPDATE agent_shared_knowledge SET source = 'system' "
            "WHERE scope = 'baseline' AND (source IS NULL OR source = '')")
        c.execute(
            "UPDATE agent_shared_knowledge SET source = 'agent' "
            "WHERE scope IN ('project', 'issue', 'issue_agent') AND (source IS NULL OR source = '')")
        c.execute(
            "UPDATE agent_shared_knowledge SET source = 'manual' "
            "WHERE scope = 'member' AND (source IS NULL OR source = '')")

        # 1.5 回填空 name + 统一 description（所有 scope，必须在去重之前）
        # 老代码部分 INSERT 路径没有 name 字段或自动生成逻辑不存在 → DEFAULT ''。
        # baseline 去重按 (repo_key, name) GROUP BY，name 为空时同 repo 的 3 条
        # 会被合并为 1 条丢失数据。description 硬编码统一值无法区分。
        # 用 derive_knowledge_name 统一推导，所有 scope 共用同一逻辑。
        from src.api.knowledge import derive_knowledge_name, derive_knowledge_description
        from src.repo_key import (BASELINE_NAME_MAP, BASELINE_DESC_MAP,
                                  BASELINE_NAME_FALLBACK, BASELINE_DESC_FALLBACK)
        _UNIFIED_DESC = "codegraph 自动生成的项目基线知识"  # 老的统一值，勿改
        _rows = c.execute(
            "SELECT id, scope, source, agent_type, issue_num, name, file_path, obs_key, description "
            "FROM agent_shared_knowledge "
            "WHERE name IS NULL OR name = '' OR name = ? "
            "OR description = ? OR description IS NULL OR description = ''",
            (BASELINE_NAME_FALLBACK, _UNIFIED_DESC)
        ).fetchall()
        _updates = []
        for _rid, _scope, _source, _agent_type, _issue_num, _cur_name, _fp, _obs, _cur_desc in _rows:
            # source="manual" 是用户上传的：
            # - name 非空时不覆盖（用户填的），name 为空时用 file_path 推导回填
            # - description 永远不碰（用户行为，即使为空也不回填）
            if _source == "manual":
                if not _cur_name:
                    _new_name = derive_knowledge_name(_scope, _agent_type or "", _issue_num or 0, _fp or _obs or "")
                    _updates.append((_new_name, _cur_desc or "", _rid))
                continue
            _new_name = derive_knowledge_name(_scope, _agent_type or "", _issue_num or 0, _fp or _obs or "")
            _needs_name = (_cur_name != _new_name)
            _new_desc = derive_knowledge_description(_scope, _agent_type or "", _issue_num or 0, _fp or _obs or "")
            _needs_desc = (not _cur_desc or _cur_desc == _UNIFIED_DESC) and bool(_new_desc)
            _final_desc = _new_desc if _needs_desc else (_cur_desc or "")
            if _needs_name or _needs_desc:
                _updates.append((_new_name, _final_desc, _rid))
        if _updates:
            c.executemany(
                "UPDATE agent_shared_knowledge SET name=?, description=? WHERE id=?",
                _updates)
            log.info(f"[issue67] backfilled name/description for {len(_updates)} rows")

        # 2. 重复记录去重——每类 scope 只保留最新一条
        #    baseline：按 (repo_key, scope, name) 去重
        #    其他 scope：按 (repo_key, scope, agent_type, issue_num, file_path) 去重
        #    用派生表子查询包装避免 DELETE 自引用（MySQL 限制），SQLite/MySQL 通用
        # baseline 去重
        c.execute(
            "DELETE FROM scene_knowledge_ref WHERE knowledge_id IN ("
            "SELECT id FROM agent_shared_knowledge WHERE scope='baseline' "
            "AND id NOT IN (SELECT keep.keep_id FROM ("
            "SELECT MAX(id) AS keep_id FROM agent_shared_knowledge "
            "WHERE scope='baseline' GROUP BY repo_key, scope, name) AS keep))")
        c.execute(
            "DELETE FROM agent_shared_knowledge WHERE scope='baseline' "
            "AND id NOT IN (SELECT keep.keep_id FROM ("
            "SELECT MAX(id) AS keep_id FROM agent_shared_knowledge "
            "WHERE scope='baseline' GROUP BY repo_key, scope, name) AS keep)")
        # 其他 scope 去重
        c.execute(
            "DELETE FROM scene_knowledge_ref WHERE knowledge_id IN ("
            "SELECT id FROM agent_shared_knowledge WHERE scope != 'baseline' "
            "AND id NOT IN (SELECT keep.keep_id FROM ("
            "SELECT MAX(id) AS keep_id FROM agent_shared_knowledge "
            "WHERE scope != 'baseline' GROUP BY repo_key, scope, agent_type, issue_num, file_path) AS keep))")
        c.execute(
            "DELETE FROM agent_shared_knowledge WHERE scope != 'baseline' "
            "AND id NOT IN (SELECT keep.keep_id FROM ("
            "SELECT MAX(id) AS keep_id FROM agent_shared_knowledge "
            "WHERE scope != 'baseline' GROUP BY repo_key, scope, agent_type, issue_num, file_path) AS keep)")

        # 3. scene_knowledge_ref 加 issue_num 列（幂等）
        _ensure_column(c, "scene_knowledge_ref", "issue_num", "INTEGER NOT NULL DEFAULT 0")

        # 3.0 scene_market 加 preset_custom_fields 列（Issue #246：场景预置自定义参数声明）
        _ensure_column(c, "scene_market", "preset_custom_fields", "TEXT DEFAULT '[]'")

        # 3.0b agent_runtimes 加 detached_from_scenes_at 列（引擎绑定真实性同步：离线超阈值解绑标记）
        _ensure_column(c, "agent_runtimes", "detached_from_scenes_at", "TEXT DEFAULT ''")

        # 3.1 MySQL: 修复 UNIQUE 索引——老索引缺 issue_num，删旧建新
        if _is_mysql:
            try:
                c.execute("DROP INDEX knowledge_id ON scene_knowledge_ref")
            except Exception:
                pass  # 索引不存在则跳过
            try:
                c.execute(
                    "CREATE UNIQUE INDEX uk_kid_scene_agent_issue "
                    "ON scene_knowledge_ref (knowledge_id, scene_id, agent_type, issue_num)")
            except Exception:
                pass  # 已存在则跳过

        # 4. scene_knowledge_ref 数据迁移——为现有知识生成关联记录
        # baseline → 全局关联（issue_num=0）
        c.execute(
            "INSERT OR IGNORE INTO scene_knowledge_ref (knowledge_id, scene_id, agent_type, issue_num, enabled, created_by) "
            "SELECT id, '', '', 0, 1, 'system' FROM agent_shared_knowledge "
            "WHERE scope = 'baseline' AND source = 'system'")
        # project → 指定 Agent 关联（issue_num=0）
        c.execute(
            "INSERT OR IGNORE INTO scene_knowledge_ref (knowledge_id, scene_id, agent_type, issue_num, enabled, created_by) "
            "SELECT id, '', agent_type, 0, 1, 'system' FROM agent_shared_knowledge "
            "WHERE scope = 'project' AND source = 'agent'")
        # issue → 全局关联（issue_num=实际值）
        c.execute(
            "INSERT OR IGNORE INTO scene_knowledge_ref (knowledge_id, scene_id, agent_type, issue_num, enabled, created_by) "
            "SELECT id, '', '', issue_num, 1, 'system' FROM agent_shared_knowledge "
            "WHERE scope = 'issue' AND source = 'agent'")
        # issue_agent → 指定 Agent + Issue 关联
        c.execute(
            "INSERT OR IGNORE INTO scene_knowledge_ref (knowledge_id, scene_id, agent_type, issue_num, enabled, created_by) "
            "SELECT id, '', agent_type, issue_num, 1, 'system' FROM agent_shared_knowledge "
            "WHERE scope = 'issue_agent' AND source = 'agent' AND agent_type != ''")
        # member（agent_type 非空）→ 指定 Agent 关联（issue_num=0）
        # Issue #95：删除——member 不自动关联，用户手动绑定才注入
        # c.execute(
        #     "INSERT OR IGNORE INTO scene_knowledge_ref (knowledge_id, scene_id, agent_type, issue_num, enabled, created_by) "
        #     "SELECT id, '', agent_type, 0, 1, 'system' FROM agent_shared_knowledge "
        #     "WHERE scope = 'member' AND source = 'manual' AND agent_type != ''")
        # member（agent_type 空）→ 全局关联（issue_num=0）
        # Issue #95：删除——违反"member 不自动关联"设计
        # c.execute(
        #     "INSERT OR IGNORE INTO scene_knowledge_ref (knowledge_id, scene_id, agent_type, issue_num, enabled, created_by) "
        #     "SELECT id, '', '', 0, 1, 'system' FROM agent_shared_knowledge "
        #     "WHERE scope = 'member' AND source = 'manual' AND (agent_type = '' OR agent_type IS NULL)")

        # Issue #95：清理 migration 自动创建的错误 member ref（幂等）
        # 只删 created_by='system' 的——用户手动创建的（created_by=username）不删
        # 加幂等标记：首次执行后设 meta 标记，后续跳过
        if not _issue95_done:
            try:
                c.execute(
                    "DELETE FROM scene_knowledge_ref WHERE knowledge_id IN "
                    "(SELECT id FROM agent_shared_knowledge WHERE scope = 'member') "
                    "AND created_by = 'system'")
            except Exception:
                pass

            # Issue #95：修复 rejected→accepted 老数据（幂等）
            # 老代码 UPDATE 不改 status，导致先 rejected 再 accepted 的记录仍为 rejected
            # 有文件内容（obs_key 非空 + content_hash 非空）说明 accepted 写过文件，应为 accepted
            try:
                c.execute(
                    "UPDATE agent_shared_knowledge SET status='accepted' "
                    "WHERE status='rejected' AND obs_key != '' AND content_hash != '' "
                    "AND obs_key IS NOT NULL AND content_hash IS NOT NULL")
            except Exception:
                pass

            # Issue #95：标记已完成
            try:
                c.execute(
                    "INSERT OR REPLACE INTO meta (key, value) VALUES ('issue95_migrate_done', '1')")
            except Exception:
                pass

        # 4. content_hash 回填——OBS 模式下 obs_key 非空但 hash 为空的老数据
        try:
            from src.storage import get_storage
            _storage = get_storage()
            if _storage is not None:
                import hashlib as _hashlib
                _prefix = os.environ.get("OBS_PREFIX", "") or ""
                _empty_hash_rows = c.execute(
                    "SELECT id, obs_key FROM agent_shared_knowledge "
                    "WHERE obs_key != '' AND (content_hash = '' OR content_hash IS NULL)"
                ).fetchall()
                for _kid, _obs_key in _empty_hash_rows:
                    for _try_key in (_obs_key, f"{_prefix}/{_obs_key}"):
                        _data = _storage.read_bytes(_try_key)
                        if _data:
                            _h = _hashlib.sha256(_data).hexdigest()
                            c.execute("UPDATE agent_shared_knowledge SET content_hash=? WHERE id=?", (_h, _kid))
                            break
        except Exception as _e:
            log.warning(f"[migrate] content_hash backfill failed: {_e}")

        # 5. 清理孤儿 scene_knowledge_ref（knowledge_id 指向已删除的知识记录）
        # 去重删除 knowledge 记录时已先删 ref，但历史迁移轮次可能残留
        # 先 EXISTS 快速检查，有孤儿才执行 DELETE
        try:
            _has_orphan = c.execute(
                "SELECT EXISTS(SELECT 1 FROM scene_knowledge_ref "
                "WHERE knowledge_id NOT IN (SELECT id FROM agent_shared_knowledge))"
            ).fetchone()[0]
            if _has_orphan:
                c.execute(
                    "DELETE FROM scene_knowledge_ref "
                    "WHERE knowledge_id NOT IN (SELECT id FROM agent_shared_knowledge)")
                log.info("[issue67] cleaned orphan scene_knowledge_ref")
        except Exception as _e:
            log.warning(f"[issue67] orphan ref cleanup failed: {_e}")

        db.commit()

    # 4. MySQL 原生 DDL——scene_knowledge_ref 表用原生 MySQL DDL 重建（如果翻译层产出不够严谨）
    if _is_mysql:
        try:
            from src.mysql_compat import mysql_config
            import pymysql
            cfg = mysql_config()
            conn = pymysql.connect(
                host=cfg.get("host", ""), port=cfg.get("port", 3306),
                user=cfg.get("user", ""), password=cfg.get("password", ""),
                database=cfg.get("database", ""), charset="utf8mb4", autocommit=True)
            try:
                with conn.cursor() as cur:
                    # 检查表是否有 updated_by 列（翻译层产出的可能没有）
                    cur.execute(
                        "SELECT COUNT(*) FROM information_schema.COLUMNS "
                        "WHERE TABLE_SCHEMA = %s AND TABLE_NAME = 'scene_knowledge_ref' "
                        "AND COLUMN_NAME = 'updated_by'", (cfg.get("database", ""),))
                    has_updated_by = cur.fetchone()[0]
                    if not has_updated_by:
                        # 翻译层产出不完整——用原生 DDL 重建
                        cur.execute("DROP TABLE IF EXISTS scene_knowledge_ref")
                        cur.execute(_SCENE_KNOWLEDGE_REF_MYSQL_DDL)
                        log.info("[Issue #67] scene_knowledge_ref recreated with native MySQL DDL")
            finally:
                conn.close()
        except Exception as e:
            log.warning(f"[Issue #67] MySQL native DDL failed: {e}")

    log.info("[Issue #67] knowledge migration done")


def _migrate_knowledge():
    """Issue #190：baseline 去重 + obs_key 归一化 + file_path 归一化。

    幂等：通过 meta 表 knowledge_migrate_done 标记控制，已执行则跳过。
    异步执行：由 main.py lifespan() 内 daemon thread 调用，不阻塞启动。
    分布式锁：acquire_lock 防多实例并发执行迁移。
    最终一致性：迁移期间旧格式数据仍可用，迁移完成后切换到新格式。
    """
    from src.session_manager import db, db_lock
    from src.user.db import get_meta_value, set_meta_value
    from src.repo_key import parse_repo_key, knowledge_obs_prefix
    from src.lock import acquire_lock, release_lock

    # 幂等检查
    if get_meta_value("knowledge_migrate_done") == "1":
        log.info("[knowledge_migrate] migration already done, skip")
        return

    # 分布式锁：防多实例同时迁移
    _lock_key = "knowledge_migrate"
    if not acquire_lock(_lock_key, ttl=600):
        log.info("[knowledge_migrate] migration lock contention, another instance is running, skip")
        return

    log.info("[knowledge_migrate] migration started")

    _restore_ok = False       # 恢复归档记录
    _obs_key_ok = False        # 归一化 obs_key
    _file_path_ok = False      # 归一化 file_path

    # ===== 恢复归档记录（archived=1 → archived=0）=====
    # 归档逻辑已移除，现有归档记录恢复为活跃，防止成为孤儿数据
    try:
        with db_lock:
            c = db.cursor()
            c.execute("UPDATE agent_shared_knowledge SET archived=0 WHERE archived=1")
            db.commit()
        _restore_ok = True
        log.info("[knowledge_migrate] restore archived records done")
    except Exception as e:
        log.warning(f"[knowledge_migrate] restore archived failed: {e}")

    # NOTE: baseline name/description 回填已移至 _migrate_knowledge_dedup_and_ref()
    # step 1.5（每次启动执行，在去重之前）。此处不再重复——_migrate_knowledge
    # 受 knowledge_migrate_done 标记控制，首次执行后跳过，无法保证回填在
    # 去重之前完成。
    # NOTE: baseline 去重也已移至 _migrate_knowledge_dedup_and_ref() step 2
    # （每次启动执行）。此处不再重复。

    # ===== 归一化 obs_key（新格式含 provider 前缀）=====
    try:
        from src.storage import get_storage
        _storage = get_storage()
        with db_lock:
            rows = db.cursor().execute(
                "SELECT id, repo_key, scope, agent_type, issue_num, obs_key, file_path "
                "FROM agent_shared_knowledge WHERE obs_key != '' AND obs_key IS NOT NULL"
            ).fetchall()
        updates = []  # [(new_obs_key, id), ...]
        for rid, repo_key, scope, agent_type, issue_num, obs_key, file_path in rows:
            provider, owner, repo = parse_repo_key(repo_key)
            new_prefix = knowledge_obs_prefix(repo_key)
            new_obs_key = obs_key
            if obs_key.startswith(new_prefix):
                continue  # 已是新格式
            elif scope == "member":
                # member：提取 member/ 或 agent/ 后的相对路径
                for marker in ("member/", "agent/"):
                    idx = obs_key.find(marker)
                    if idx >= 0:
                        _rel = obs_key[idx:]
                        break
                else:
                    _rel = os.path.basename(obs_key)
                new_obs_key = f"{new_prefix}/{_rel}"
            elif obs_key.startswith("project-info/"):
                # OLD_A 格式：完全重写
                _fname = os.path.basename(obs_key)
                if scope == "baseline":
                    new_obs_key = f"{new_prefix}/project-info/{_fname}"
                else:
                    try:
                        from src.api.knowledge import knowledge_storage_key
                        new_obs_key = knowledge_storage_key(repo_key, scope, agent_type or "", issue_num or 0)
                    except Exception:
                        continue
            elif obs_key.startswith(f"knowledge/{owner}/{repo}"):
                # OLD_B 格式：在 knowledge/ 后插入 provider/
                new_obs_key = f"knowledge/{provider}/{obs_key[len('knowledge/'):]}"
            else:
                continue  # 未知格式，跳过

            if new_obs_key == obs_key:
                continue

            # OBS 对象迁移：旧 key 读取内容 → 写入新 key → 删旧 key
            # 读不到内容时跳过（不更新 DB、不删旧对象），下次启动重试
            if _storage:
                try:
                    _data = _storage.read_bytes(obs_key)
                    if not _data:
                        log.warning(f"[knowledge_migrate] OBS read empty for id={rid} old_key={obs_key}, skip")
                        continue
                    _storage.save_bytes(new_obs_key, _data)
                except Exception as e:
                    log.warning(f"[knowledge_migrate] OBS migrate failed id={rid}: {e}")
                    continue
            updates.append((new_obs_key, rid, obs_key))  # 记录旧 key 供清理

        # 批量 UPDATE，一次 commit
        if updates:
            with db_lock:
                c = db.cursor()
                c.executemany(
                    "UPDATE agent_shared_knowledge SET obs_key=? WHERE id=?",
                    [(u[0], u[1]) for u in updates])
                db.commit()
            # DB 更新成功后删除旧 OBS 对象（去重防跨平台同名仓库共享同一旧 key）
            if _storage:
                _deleted_keys = set()
                for _, _, _old_key in updates:
                    if _old_key in _deleted_keys:
                        continue
                    _deleted_keys.add(_old_key)
                    try:
                        _storage.delete_object(_old_key)
                    except Exception as e:
                        log.warning(f"[knowledge_migrate] OBS old key delete failed {_old_key}: {e}")
        _obs_key_ok = True
        log.info(f"[knowledge_migrate] obs_key normalization done ({len(updates)} rows)")
    except Exception as e:
        log.warning(f"[knowledge_migrate] obs_key normalization failed: {e}")

    # ===== 归一化 file_path（新格式含 provider 前缀）=====
    try:
        with db_lock:
            rows = db.cursor().execute(
                "SELECT id, repo_key, file_path FROM agent_shared_knowledge "
                "WHERE file_path != '' AND file_path IS NOT NULL"
            ).fetchall()
        updates = []  # [(new_file_path, id), ...]
        for rid, repo_key, file_path in rows:
            provider, owner, repo = parse_repo_key(repo_key)
            full_prefix = f"{provider}/{owner}/{repo}/"
            partial_prefix = f"{owner}/{repo}/"
            if file_path.startswith(full_prefix):
                continue  # 已是 NEW 格式
            elif file_path.startswith(partial_prefix):
                new_fp = f"{provider}/{file_path}"  # 只补 provider
            elif file_path.startswith("project-info/"):
                new_fp = f"{full_prefix}{file_path}"  # 补完整前缀
            else:
                continue  # 未知格式，跳过
            updates.append((new_fp, rid))

        if updates:
            with db_lock:
                c = db.cursor()
                c.executemany(
                    "UPDATE agent_shared_knowledge SET file_path=? WHERE id=?",
                    updates)
                db.commit()
        _file_path_ok = True
        log.info(f"[knowledge_migrate] file_path normalization done ({len(updates)} rows)")
    except Exception as e:
        log.warning(f"[knowledge_migrate] file_path normalization failed: {e}")

    # ===== 标记完成 + 释放锁（finally 保证锁一定释放）=====
    try:
        if _restore_ok and _obs_key_ok and _file_path_ok:
            set_meta_value("knowledge_migrate_done", "1")
            log.info("[knowledge_migrate] migration done")
        else:
            log.warning("[knowledge_migrate] migration incomplete, will retry on next startup")
    finally:
        release_lock(_lock_key)


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


def _cleanup_old_latency_logs():
    """清理 7 天前的 API 慢请求日志"""
    try:
        with db_lock:
            c = db.cursor()
            c.execute("DELETE FROM api_latency_log WHERE created_at < datetime('now', '-7 days')")
            deleted = c.rowcount
            db.commit()
        if deleted:
            log.info(f"Cleaned {deleted} old latency logs")
    except Exception as e:
        log.warning(f"Latency log cleanup failed: {e}")


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


def _tasks_to_issue_rename_pre_create():
    """Phase B1：`tasks` 表改名为 `issue`（结构不变，数据零迁移）。

    必须在 `_create_tables()` **之前**执行——否则 `CREATE TABLE IF NOT EXISTS issue` 会先建出空
    `issue` 表，导致 `_migrate_tables` 里"issue 已存在则跳过 rename"，数据滞留旧 `tasks` 表。
    幂等：无 `tasks` 表或已有 `issue` 表（非空）则跳过。
    """
    try:
        with db_lock:
            c = db.cursor()
            _tables = [r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()]
            if "tasks" not in _tables:
                return
            if "issue" in _tables:
                _issue_cnt = c.execute("SELECT COUNT(*) FROM issue").fetchone()[0]
                _tasks_cnt = c.execute("SELECT COUNT(*) FROM tasks").fetchone()[0]
                if _issue_cnt == 0 and _tasks_cnt > 0:
                    c.execute("DROP TABLE issue")
                    c.execute("ALTER TABLE tasks RENAME TO issue")
                    db.commit()
                    log.info(f"Phase B1: tasks -> issue (dropped empty issue, renamed), {_tasks_cnt} rows")
                else:
                    log.info(f"Phase B1: issue already exists ({_issue_cnt} rows), rename skipped")
                return
            c.execute("ALTER TABLE tasks RENAME TO issue")
            db.commit()
            log.info("Phase B1: tasks -> issue (structure unchanged)")
    except Exception as e:
        log.warning(f"Phase B1 tasks->issue pre-create rename failed: {e}")


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
    # ===== Phase B1: tasks 表改名为 issue（结构不变，后续 B2 收编 issue_state 字段）=====
    # 幂等：已存在 issue 表则跳过（新建库直接建 issue；老库 tasks→issue）
    try:
        with db_lock:
            c = db.cursor()
            _tables = [r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()]
            if "issue" not in _tables and "tasks" in _tables:
                c.execute("ALTER TABLE tasks RENAME TO issue")
                db.commit()
                log.info("Migrated table tasks -> issue (Phase B1, 结构不变)")
            elif "issue" in _tables:
                log.info("issue table already exists (Phase B1 rename skipped)")
    except Exception as e:
        log.warning(f"tasks->issue rename failed: {e}")
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
        "ALTER TABLE events ADD COLUMN scene_id TEXT DEFAULT ''",
        "ALTER TABLE events ADD COLUMN entity_type TEXT DEFAULT ''",
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
        # ===== Issue #180: 带宽占用率指标 =====
        "ALTER TABLE runtime_credentials ADD COLUMN bandwidth_percent DOUBLE DEFAULT 0",
        "ALTER TABLE runtime_credentials ADD COLUMN bandwidth_mbps DOUBLE DEFAULT 0",
        "ALTER TABLE agent_runtimes ADD COLUMN repo_key VARCHAR(512) DEFAULT NULL",
        # JWT 撤销——token_version 字段（改密码/logout 时 +1）
        "ALTER TABLE user_accounts ADD COLUMN token_version INTEGER DEFAULT 0",
        # GitHub OAuth 集成——user_accounts 新增 github 字段
        "ALTER TABLE user_accounts ADD COLUMN github_id TEXT DEFAULT ''",
        "ALTER TABLE user_accounts ADD COLUMN github_api_token TEXT DEFAULT ''",
        "ALTER TABLE user_accounts ADD COLUMN github_login TEXT DEFAULT ''",
        # 飞书 Bot Lease——集群部署保证同一 bot 同一时刻只有一个实例连 WebSocket
        "ALTER TABLE global_im_bot_config ADD COLUMN bot_name VARCHAR(128) DEFAULT ''",
        "ALTER TABLE global_im_bot_config ADD COLUMN lease_token VARCHAR(128) DEFAULT NULL",
        "ALTER TABLE global_im_bot_config ADD COLUMN lease_expires_at DATETIME DEFAULT NULL",
        "ALTER TABLE global_im_bot_config ADD COLUMN lease_owner VARCHAR(128) DEFAULT NULL",
        "ALTER TABLE global_im_bot_config ADD COLUMN lease_acquired_at DATETIME DEFAULT NULL",
        # SQLite 不支持 ALTER ADD COLUMN non-constant default（CURRENT_TIMESTAMP 会静默失败）
        "ALTER TABLE global_im_bot_config ADD COLUMN updated_at TEXT DEFAULT ''",
        # ===== knowledge-obs-design：agent_shared_knowledge OBS 对象存储字段 =====
        "ALTER TABLE agent_shared_knowledge ADD COLUMN obs_key VARCHAR(512) DEFAULT ''",
        "ALTER TABLE agent_shared_knowledge ADD COLUMN content_hash VARCHAR(64) DEFAULT ''",
        "ALTER TABLE agent_shared_knowledge ADD COLUMN updated_at TEXT DEFAULT ''",
        # ===== Issue #67：知识来源 + 归档标记 =====
        "ALTER TABLE agent_shared_knowledge ADD COLUMN source TEXT NOT NULL DEFAULT ''",
        "ALTER TABLE agent_shared_knowledge ADD COLUMN archived INTEGER NOT NULL DEFAULT 0",
        "ALTER TABLE agent_shared_knowledge ADD COLUMN description TEXT DEFAULT ''",
        "ALTER TABLE agent_shared_knowledge ADD COLUMN name VARCHAR(256) DEFAULT ''",
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
        # ===== probe 连续失败计数：连续2次fail才降级healthy，避免偶发超时误判 =====
        "ALTER TABLE agent_runtimes ADD COLUMN probe_fail_count INT DEFAULT 0",
        # ===== 场景标签：引擎绑定的场景列表，约束可选角色标签 =====
        "ALTER TABLE agent_runtimes ADD COLUMN scene_tags TEXT DEFAULT '[\"default\"]'",
        # ===== 引擎别名：用户自定义别名，不受心跳覆盖 =====
        "ALTER TABLE agent_runtimes ADD COLUMN alias TEXT DEFAULT ''",
        # ===== 标签用户标记：用户手动设过标签后置1，心跳注册不再自动补 'all' =====
        "ALTER TABLE agent_runtimes ADD COLUMN tags_user_set INTEGER DEFAULT 0",
        # ===== Issue-P2: 健康分重置时间戳（管理员赦免点，计算窗口起点=max(此值, 7天前)）=====
        "ALTER TABLE agent_runtimes ADD COLUMN health_reset_at VARCHAR(64) DEFAULT ''",
        # ===== Issue-11: skill 表加 is_featured + like_count =====
        "ALTER TABLE skill ADD COLUMN is_featured INTEGER DEFAULT 0",
        "ALTER TABLE skill ADD COLUMN like_count INTEGER DEFAULT 0",
        "ALTER TABLE skill ADD COLUMN sub_scene TEXT DEFAULT ''",
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
        # ===== Issue-89: event_skipped 加 issue_id 列（skipped 事件审计需关联 Issue） =====
        "ALTER TABLE event_skipped ADD COLUMN issue_id TEXT DEFAULT ''",
        # ===== Issue #177: user_feedback 软删除标记 =====
        "ALTER TABLE user_feedback ADD COLUMN is_deleted INTEGER NOT NULL DEFAULT 0",
    ]
    # 回填已有分类数据的 slug（幂等，仅 slug 为空时更新）
    _category_slugs = [
        ("general", "通用"), ("mobile", "移动开发"), ("cloud-native", "云原生"),
        ("iac", "IaC"), ("skill-dev", "技能开发"), ("ai", "AI应用"), ("content", "内容创作"),
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
    # skill agent_type 同步由 worker 的 _sync_skills_to_db 变更检测覆盖，init_database 不做
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
            ("github_oauth_client_id", "oauth_github_client_id"),
            ("github_oauth_client_secret", "oauth_github_client_secret"),
            ("github_oauth_redirect_uri", "oauth_github_redirect_uri"),
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
    # ===== session 引擎守卫:issue_agent_session 加 session_engine 列(老库升级) =====
    # 指针(runtime_session_id)记引擎名,消费端校验"指针引擎==本次引擎"不一致则弃用(开新会话),
    # 防止引擎池混跑时 hermes 拿到 opencode 的会话号静默挂死(跨引擎续接无防护的历史遗留)
    try:
        with db_lock:
            c = db.cursor()
            _ensure_column(c, "issue_agent_session", "session_engine", "TEXT DEFAULT ''")
            db.commit()
        log.info("Added issue_agent_session.session_engine column")
    except Exception as e:
        log.warning(f"Add issue_agent_session.session_engine column failed: {e}")
    # ===== user_accounts 发布仓库列(老库升级;CREATE TABLE 只影响新库,存量库须幂等补列)=====
    try:
        with db_lock:
            c = db.cursor()
            _ensure_column(c, "user_accounts", "gitcode_publish_repo", "TEXT DEFAULT ''")
            _ensure_column(c, "user_accounts", "github_publish_repo", "TEXT DEFAULT ''")
            _ensure_column(c, "user_accounts", "github_login", "TEXT DEFAULT ''")
            db.commit()
        log.info("Added gitcode_publish_repo / github_publish_repo / github_login columns to user_accounts")
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
    # ===== Issue #179: skill 表加 category_id 列（老库升级） =====
    try:
        with db_lock:
            c = db.cursor()
            _ensure_column(c, "skill", "category_id", "INTEGER DEFAULT 0")
            db.commit()
            log.info("Added skill.category_id column")
    except Exception as e:
        log.warning(f"Add skill.category_id column failed: {e}")
    # ===== devops_pipeline_scene 高级选项列（场景级工作分支 + 自定义 key:value）=====
    try:
        with db_lock:
            c = db.cursor()
            _ensure_column(c, "devops_pipeline_scene", "advanced_options", "TEXT DEFAULT '{}'")
            _ensure_column(c, "devops_pipeline_scene", "created_by", "TEXT DEFAULT ''")
            _ensure_column(c, "devops_pipeline_scene", "icon", "TEXT DEFAULT ''")
            _ensure_column(c, "devops_pipeline_scene", "scene_type", "TEXT DEFAULT 'standard'")
            db.commit()
            log.info("Added devops_pipeline_scene.advanced_options + created_by + icon + scene_type columns")
    except Exception as e:
        log.warning(f"Add devops_pipeline_scene.advanced_options/created_by/scene_type column failed: {e}")
    # ===== PR 订阅附加能力：场景级开关（替代独立 scene_type='pr_subscription'）=====
    try:
        with db_lock:
            c = db.cursor()
            _ensure_column(c, "devops_pipeline_scene", "pr_subscription_enabled", "INTEGER NOT NULL DEFAULT 0")
            db.commit()
            # 数据迁移：已有 pr_subscription 场景 → standard + pr_subscription_enabled=1
            c.execute("UPDATE devops_pipeline_scene SET pr_subscription_enabled=1, scene_type='standard' WHERE scene_type='pr_subscription'")
            _migrated = c.rowcount
            db.commit()
            if _migrated:
                log.info(f"Migrated {_migrated} pr_subscription scenes → standard + pr_subscription_enabled=1")
    except Exception as e:
        log.warning(f"Add devops_pipeline_scene.pr_subscription_enabled column failed: {e}")
    # ===== Issue #242: devops_pipeline_scene 关联仓库列（跨仓库协同开发）=====
    try:
        with db_lock:
            c = db.cursor()
            _ensure_column(c, "devops_pipeline_scene", "linked_repos", "TEXT DEFAULT '[]'")
            db.commit()
            log.info("Added devops_pipeline_scene.linked_repos column")
    except Exception as e:
        log.warning(f"Add devops_pipeline_scene.linked_repos column failed: {e}")
    # ===== events 表补 scene_id 列（PR 订阅场景持久化）=====
    try:
        with db_lock:
            c = db.cursor()
            _ensure_column(c, "events", "scene_id", "TEXT DEFAULT ''")
            db.commit()
            log.info("Added events.scene_id column")
    except Exception as e:
        log.warning(f"Add events.scene_id column failed: {e}")
    # ===== events 表补 structured_output 列（weekly-05：结构化产出落库替代 event.json seg.output）=====
    try:
        with db_lock:
            c = db.cursor()
            _ensure_column(c, "events", "structured_output", "MEDIUMTEXT DEFAULT NULL")
            db.commit()
            log.info("Added events.structured_output column")
    except Exception as e:
        log.warning(f"Add events.structured_output column failed: {e}")
    # ===== agent-cluster-design.md §4.1/§4.2:引擎池数据面(池表 + 槽位列 + 任务池标记)=====
    # 老库升级:agent_runtimes 补 busy_slots/max_concurrent;agent_tasks 补 attempts/pool_agent_type/affinity_runtime_id
    try:
        with db_lock:
            c = db.cursor()
            _ensure_column(c, "agent_runtimes", "busy_slots", "INTEGER NOT NULL DEFAULT 0")
            _ensure_column(c, "agent_runtimes", "max_concurrent", "INTEGER NOT NULL DEFAULT 20")
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
                _ensure_column(c, "runtime_credentials", "spec", "TEXT DEFAULT ''")
                _ensure_column(c, "runtime_credentials", "tmp_percent", "REAL DEFAULT 0")
                _ensure_column(c, "runtime_credentials", "bandwidth_percent", "REAL DEFAULT 0")
                _ensure_column(c, "runtime_credentials", "bandwidth_mbps", "REAL DEFAULT 0")
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
            _ensure_column(c, "event_action_filter", "vcs_source", "TEXT DEFAULT ''")
            db.commit()
        log.info("Added event_action_filter.block_changes + vcs_source columns")
    except Exception as e:
        log.warning(f"Add event_action_filter columns failed: {e}")
    # ===== Phase 2: user_accounts.last_scene (event-dispatch-redesign-v2.md 3.1) =====
    try:
        with db_lock:
            c = db.cursor()
            _ensure_column(c, "user_accounts", "last_scene", "TEXT DEFAULT 'default'")
            db.commit()
        log.info("Added last_scene column to user_accounts")
    except Exception as e:
        log.warning(f"Add last_scene column failed: {e}")
    # ===== multi-task mode: issue_state 已停用（C5），仅补 issue/chat_messages 列 =====
    try:
        with db_lock:
            c = db.cursor()
            _ensure_column(c, "issue", "parent_issue_number", "INTEGER DEFAULT 0")
            _ensure_column(c, "issue", "is_subtask_mode", "INTEGER DEFAULT 0")
            _ensure_column(c, "chat_messages", "task_id", "TEXT")
            _ensure_column(c, "chat_messages", "generate_status", "INTEGER DEFAULT NULL")
            db.commit()
        log.info("Added parent_issue_number/is_subtask_mode to issue + task_id/generate_status to chat_messages")
    except Exception as e:
        log.warning(f"Add multi-task columns failed: {e}")
    # ===== Phase B2: issue 收编 issue_state 字段（新增列 + 存量数据回填）=====
    # 目标：issue 单表承载 stage/status 全量，为 C 阶段弃用 issue_state 做准备。
    # 幂等：列已存在则跳过；C5 后 issue_state 已 DROP，回填自动跳过。
    try:
        with db_lock:
            c = db.cursor()
            # 批 A：stage + owner_agent（默认 vod）
            _ensure_column(c, "issue", "stage", "TEXT DEFAULT 'vod'")
            _ensure_column(c, "issue", "owner_agent", "TEXT DEFAULT 'vod'")
            # 批 B：场景相关
            _ensure_column(c, "issue", "scene_id", "TEXT DEFAULT ''")
            _ensure_column(c, "issue", "scene_user_id", "INTEGER DEFAULT 0")
            _ensure_column(c, "issue", "allow_scene_override", "INTEGER DEFAULT 0")
            # 批 C：A2A 触发 + 子任务归属 + 更新时间
            _ensure_column(c, "issue", "a2a_seq", "INTEGER")
            _ensure_column(c, "issue", "parent_issue_id", "TEXT DEFAULT ''")
            _ensure_column(c, "issue", "updated_at", "TEXT DEFAULT ''")
            db.commit()
    except Exception as e:
        log.warning(f"Phase B2 issue 收编字段补列失败: {e}")
    # 存量回填：从 issue_state 并入 issue（按 issue_id == "#{issue_number}"；C5 DROP 后跳过）
    try:
        with db_lock:
            c = db.cursor()
            _has_issue_state = bool(c.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='issue_state'").fetchone())
        if _has_issue_state:
            with db_lock:
                c = db.cursor()
                c.execute(
                    "UPDATE issue SET "
                    "stage = COALESCE((SELECT s.stage FROM issue_state s "
                    "                    WHERE s.repo_key=issue.repo_key "
                    "                      AND s.issue_id='#'||issue.issue_number), 'vod'), "
                    "owner_agent = COALESCE((SELECT s.owner_agent FROM issue_state s "
                "                    WHERE s.repo_key=issue.repo_key "
                "                      AND s.issue_id='#'||issue.issue_number), 'vod'), "
                "scene_id = COALESCE((SELECT s.scene_id FROM issue_state s "
                "                    WHERE s.repo_key=issue.repo_key "
                "                      AND s.issue_id='#'||issue.issue_number), ''), "
                "scene_user_id = COALESCE((SELECT s.scene_user_id FROM issue_state s "
                "                    WHERE s.repo_key=issue.repo_key "
                "                      AND s.issue_id='#'||issue.issue_number), 0), "
                "allow_scene_override = COALESCE((SELECT s.allow_scene_override FROM issue_state s "
                "                    WHERE s.repo_key=issue.repo_key "
                "                      AND s.issue_id='#'||issue.issue_number), 0), "
                "a2a_seq = COALESCE((SELECT s.a2a_seq FROM issue_state s "
                "                    WHERE s.repo_key=issue.repo_key "
                "                      AND s.issue_id='#'||issue.issue_number), a2a_seq), "
                "parent_issue_id = COALESCE((SELECT s.parent_issue_id FROM issue_state s "
                "                    WHERE s.repo_key=issue.repo_key "
                "                      AND s.issue_id='#'||issue.issue_number), ''), "
                "is_subtask_mode = COALESCE((SELECT s.is_subtask_mode FROM issue_state s "
                "                    WHERE s.repo_key=issue.repo_key "
                "                      AND s.issue_id='#'||issue.issue_number), is_subtask_mode), "
                "updated_at = COALESCE((SELECT s.updated_at FROM issue_state s "
                "                    WHERE s.repo_key=issue.repo_key "
                "                      AND s.issue_id='#'||issue.issue_number), '') "
                "WHERE EXISTS (SELECT 1 FROM issue_state s "
                "              WHERE s.repo_key=issue.repo_key AND s.issue_id='#'||issue.issue_number)")
            _backfilled = c.rowcount
            db.commit()
        else:
            _backfilled = 0
        log.info(f"Phase B2: issue 收编 issue_state 字段完成（补列 + 回填 {_backfilled} 行）")
    except Exception as e:
        log.warning(f"Phase B2 issue 收编 issue_state 字段失败: {e}")
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
    # ===== Scene Center: agent_tasks.pool_engine_ids + 官方场景种子导入 =====
    try:
        with db_lock:
            c = db.cursor()
            _ensure_column(c, "agent_tasks", "pool_engine_ids", "TEXT DEFAULT ''")
            _ensure_column(c, "agent_tasks", "user_gitcode_token", "TEXT DEFAULT ''")
            db.commit()
        log.info("Added pool_engine_ids column to agent_tasks")
    except Exception as e:
        log.warning(f"Add pool_engine_ids column failed: {e}")
    # ===== 双重标记完成（issue-219-task-complete-double-signal-design.md）：stream_ended / self_reported =====
    try:
        with db_lock:
            c = db.cursor()
            _ensure_column(c, "agent_tasks", "stream_ended", "INTEGER DEFAULT 0")
            _ensure_column(c, "agent_tasks", "self_reported", "INTEGER DEFAULT 0")
            db.commit()
        log.info("Added stream_ended/self_reported columns to agent_tasks")
    except Exception as e:
        log.warning(f"Add stream_ended/self_reported columns failed: {e}")
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
    # ===== Issue-76: tasks.created_by（记录任务来源 Issue 的创建者 GitCode 用户名，用于「我创建的任务」过滤）=====
    try:
        with db_lock:
            c = db.cursor()
            _ensure_column(c, "issue", "created_by", "TEXT DEFAULT ''")
            db.commit()
        log.info("Added created_by column to issue")
    except Exception as e:
        log.warning(f"Add tasks.created_by column failed: {e}")
    # ===== Scene scope downgrade 已在 _migrate_scene_table_to_repo_model 中处理（Issue-39 方案 B）=====
    # ===== Phase C5: DROP issue_state（字段已收编进 issue 单表，见 issues-api-execution-plan.md）=====
    # 安全条件：不存在「issue_state 有、issue 无」的孤儿；且两边 stage 全部一致。否则跳过并告警。
    # 当前仅 SQLite 自动执行（生产为 SQLite）；MySQL 手动执行（CAST 语法差异，避免迁移期报错）。
    # 跳过条件：测试环境（pytest）保留 issue_state（session_store 兜底 + 测试引用）。
    try:
        import sys as _sys
        _is_test = any("pytest" in _mod for _mod in (_sys.modules.keys() if _sys.modules else []))
        if _is_test:
            log.info("Phase C5: skip drop issue_state in test mode")
        else:
            from src.mysql_compat import get_db_backend
            if get_db_backend() == "mysql":
                log.info("Phase C5: MySQL 请手动执行 DROP issue_state（数据核对后）")
            else:
                with db_lock:
                    c = db.cursor()
                    _tables = [r[0] for r in c.execute(
                        "SELECT name FROM sqlite_master WHERE type='table' AND name='issue_state'").fetchall()]
                    if not _tables:
                        _is_dropped = True
                    else:
                        _orphan = c.execute(
                            "SELECT COUNT(*) FROM issue_state s WHERE NOT EXISTS ("
                            "SELECT 1 FROM issue i WHERE i.repo_key=s.repo_key "
                            "AND i.issue_number=CAST(REPLACE(s.issue_id,'#','') AS INTEGER))"
                        ).fetchone()[0]
                        _mismatch = c.execute(
                            "SELECT COUNT(*) FROM issue_state s JOIN issue i "
                            "ON i.repo_key=s.repo_key AND i.issue_number=CAST(REPLACE(s.issue_id,'#','') AS INTEGER) "
                            "WHERE COALESCE(i.stage,'')<>COALESCE(s.stage,'')"
                        ).fetchone()[0]
                        if _orphan == 0 and _mismatch == 0:
                            c.execute("DROP TABLE issue_state")
                            db.commit()
                            _is_dropped = True
                        else:
                            _is_dropped = False
                            log.warning(f"Phase C5: skip drop issue_state (orphan={_orphan}, mismatch={_mismatch})")
                if _is_dropped:
                    log.info("Phase C5: issue_state dropped（字段已收编进 issue 单表）")
            if _is_dropped:
                log.info("Phase C5: issue_state dropped（字段已收编进 issue 单表）")
    except Exception as e:
        log.warning(f"Phase C5 DROP issue_state failed: {e}")


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



    # ===== autopilot v3.0: 表结构重构 =====
    # 旧字段 scene_id/description/agent_chain/issue_number/agent_chain_step 已移除
    # 新字段 agent_type/instruction/skill_name/findings_json
    # 迁移策略: 清空两张表，新结构从头开始（_ensure_column 只加不删，旧字段物理保留但不再使用）
    try:
        with db_lock:
            c = db.cursor()
            # autopilot_tasks 新字段
            _ensure_column(c, "autopilot_tasks", "agent_type", "TEXT NOT NULL DEFAULT 'issue'")
            _ensure_column(c, "autopilot_tasks", "instruction", "TEXT NOT NULL DEFAULT ''")
            _ensure_column(c, "autopilot_tasks", "skill_name", "TEXT NOT NULL DEFAULT ''")
            _ensure_column(c, "autopilot_tasks", "engine_runtime_ids", "TEXT NOT NULL DEFAULT ''")
            _ensure_column(c, "autopilot_tasks", "custom_fields", "TEXT NOT NULL DEFAULT ''")
            _ensure_column(c, "autopilot_tasks", "work_branch", "TEXT NOT NULL DEFAULT ''")
            # autopilot_executions 新字段
            _ensure_column(c, "autopilot_executions", "agent_type", "TEXT DEFAULT ''")
            _ensure_column(c, "autopilot_executions", "findings_json", "MEDIUMTEXT DEFAULT ''")
            db.commit()
            log.info("Autopilot v3.0 schema migration: agent_type/instruction/skill_name/findings_json/custom_fields/work_branch ensured")
    except Exception as e:
        log.warning(f"Autopilot v3.0 schema migration failed: {e}")

    # ===== Organization 特性迁移 =====
    _migrate_add_org_id_columns()
    _migrate_create_personal_orgs()
    _migrate_data_to_personal_orgs()
