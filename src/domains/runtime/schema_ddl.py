"""DDL：runtime 域基表建表语句（由 db_setup 拆分而来，归属本域）。

搬迁原因见 scripts/split_schema_ddl.py：DDL 属表的归属域（方案 D9），
放在 owner 域模块里才能让「跨域引用」统计反映真实依赖。
行为不变：db_setup 仍按原顺序执行这些语句。
"""

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
    idempotency_key TEXT DEFAULT NULL,
    created_at TEXT DEFAULT (datetime('now')),
    started_at TEXT DEFAULT '',
    completed_at TEXT DEFAULT '',
    failed_node TEXT DEFAULT ''
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
    created_at TEXT DEFAULT (datetime('now')),
    last_active_at TEXT DEFAULT (datetime('now')),
    UNIQUE(repo_key, agent_type)
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
    cat_l1_id INTEGER DEFAULT 0,
    cat_l2_id INTEGER DEFAULT 0,
    tags TEXT DEFAULT '',
    summary TEXT DEFAULT '',
    download_count INTEGER DEFAULT 0,
    created_at TEXT DEFAULT (datetime('now')),
    updated_at TEXT DEFAULT (datetime('now')),
    UNIQUE (skill_name, scope, owner_id)
)
"""

# 技能版本历史（Issue #3168 D6）：每次上传覆盖写一行，版本号强制递增。
_SKILL_VERSION_DDL = """
CREATE TABLE IF NOT EXISTS grape_t_skill_version (
    id TEXT PRIMARY KEY,
    skill_id INTEGER NOT NULL,
    version VARCHAR(64) NOT NULL DEFAULT '',
    change_note TEXT DEFAULT '',
    uploaded_at TEXT DEFAULT (datetime('now')),
    uploaded_by VARCHAR(64) DEFAULT '',
    UNIQUE (skill_id, version)
)
"""

# 技能包文件清单（Issue #3168 D9）：上传时解析 zip 落库，供资源 Tab 文件树。
_SKILL_FILE_DDL = """
CREATE TABLE IF NOT EXISTS grape_t_skill_file (
    id TEXT PRIMARY KEY,
    skill_id INTEGER NOT NULL,
    path VARCHAR(512) NOT NULL DEFAULT '',
    size INTEGER DEFAULT 0,
    UNIQUE (skill_id, path)
)
"""

# 技能用户评论（Issue #3168 D11）：概览 Tab 底部评论区，纯文本。
_SKILL_COMMENT_DDL = """
CREATE TABLE IF NOT EXISTS grape_t_skill_comment (
    id TEXT PRIMARY KEY,
    skill_id INTEGER NOT NULL,
    user_id TEXT NOT NULL DEFAULT '',
    user_name VARCHAR(128) DEFAULT '',
    content TEXT NOT NULL DEFAULT '',
    created_at TEXT DEFAULT (datetime('now'))
)
"""

_META_TABLE_DDL = """
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
)
"""

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
    role_tags TEXT DEFAULT '',           -- Issue #658：引擎角色标签（JSON 数组，如 ["coding","issue"]；空=按引擎类型默认映射兜底）
    engine_uid VARCHAR(36) DEFAULT NULL,        -- Issue #646: 稳定业务键（UUIDv5 自然键派生，UNIQUE 索引兜底），自增 id 保留兼容
    credential_uid VARCHAR(36) DEFAULT NULL,    -- Issue #646: 引用 runtime_credentials.credential_uid（自增 credential_id 保留兼容）
    model_name TEXT DEFAULT '',          -- Issue #865：配置模型（worker 注册上报的本机配置模型名）
    model_source TEXT DEFAULT '',        -- Issue #865：配置模型来源（worker_report=worker 上报 / local_config=服务端本机解析）
    model_reported_at TEXT DEFAULT '',   -- Issue #865：配置模型上报时间（UTC ISO）
    -- 2026-11-18：7794d05ee 的沙箱/引擎改造在 7 处代码里引用了这两列，却没加进表里，
    -- 导致 claim 查询 1054（Unknown column）→ 引擎进程从未 spawn → 任务挂到超时判 failed。
    -- 老库由迁移 202611180002 补列；这里让新库建表即带。
    runtime_type VARCHAR(32) NOT NULL DEFAULT 'local',  -- 引擎类型 local/sandbox（与既有 engine_type 并存）
    runtime_info TEXT,                                  -- 云沙箱路由 JSON（template_id 等）；本机引擎为空 → bwrap
    updated_at TEXT DEFAULT (datetime('now')),
    UNIQUE (credential_id, name, scope, owner_id, host_name)
)
"""

_CHAT_CONVERSATIONS_DDL = """
CREATE TABLE IF NOT EXISTS chat_conversations (
    id TEXT PRIMARY KEY,
    agent_session_id TEXT DEFAULT '',
    user_id TEXT NOT NULL,
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
    generate_status INTEGER DEFAULT NULL,
    deleted INTEGER DEFAULT 0
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

_IM_INSTANCE_DDL = """
CREATE TABLE IF NOT EXISTS grape_im_instance (
    instance_id      VARCHAR(64) PRIMARY KEY,
    hostname         VARCHAR(64) NOT NULL,
    ip               VARCHAR(64) DEFAULT '',
    joined_at        DATETIME DEFAULT (datetime('now')),
    last_heartbeat   DATETIME DEFAULT (datetime('now')),
    im_enabled       INTEGER DEFAULT 1
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

# Issue #3055 B 项口径（勿改本声明）：SQLite 侧 issue_number 的 INTEGER 亲和是**有意
# 契约**（#995/#2072：数字编号落盘即 int、读侧恒 int；字符串 ident 由 SQLite 动态类型
# 原样存 text，不存在截断）。MySQL 侧翻译层会把它变成 INT 并在非严格 sql_mode 下静默
# 截断 ident → 由注册迁移 202611172020（db_setup._fn_issue3055_ident_schema）
# 在启动时 MODIFY 成 VARCHAR(191)，因此这里保持 INTEGER 声明不动。
_ISSUE_DDL = """
CREATE TABLE IF NOT EXISTS issue (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id          TEXT NOT NULL,
    assignee         TEXT NOT NULL,
    repo_key         TEXT NOT NULL,
    issue_number     INTEGER NOT NULL,
    title            TEXT DEFAULT '',
    task_description TEXT DEFAULT '',
    conversation_id  TEXT DEFAULT '',
    status           TEXT DEFAULT 'active',
    completed_reason TEXT DEFAULT '',
    created_by       TEXT DEFAULT '',
    vcs_exists       INTEGER NOT NULL DEFAULT 1,
    vcs_checked_at   TEXT DEFAULT NULL,
    org_context_snapshot_id TEXT NOT NULL DEFAULT '',  -- 组织快照 uuid（32 hex）
    created_at       TEXT DEFAULT (datetime('now')),
    completed_at     TEXT DEFAULT NULL
)
"""

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
    user_id           TEXT DEFAULT 0,
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
    pool_engine_uids   TEXT DEFAULT '',           -- Issue #646: pool_engine_ids 的 uid 语义镜像（JSON 数组，双读期双写）
    pool_selector      TEXT,                      -- Issue #1208: selector 标签池（JSON {"tags":[...]}，与两池列三列语义互斥；TEXT NULL 无 DEFAULT，#627 MySQL 教训）
    engine_uid         VARCHAR(36) DEFAULT NULL,  -- Issue #646: runtime_id 的 uid 引用（孤儿/历史置 NULL）
    engine_name_snapshot TEXT DEFAULT '',         -- Issue #646: 引擎展示名快照（引擎删除后历史可读）
    pool_engine_source TEXT DEFAULT '',           -- pool_engine_ids 来源：scene=用户场景绑定 / auto=自动选择 / directed=engine_config.runtime_id 定向锁定（降级判别，local-claimer 设计 + Issue #2791）
    status            TEXT DEFAULT 'queued', -- queued/dispatched/running/pending_self_report/completed/failed/timeout_failed/cancelled（Issue #537 新增 pending_self_report 中间态 + timeout_failed 终态）
    exit_code         INTEGER DEFAULT NULL,
    result_text       MEDIUMTEXT DEFAULT '',
    errors            MEDIUMTEXT DEFAULT '',
    stderr            MEDIUMTEXT DEFAULT '',
    usage_json        MEDIUMTEXT DEFAULT '{}',
    started_at        TEXT DEFAULT NULL,
    completed_at      TEXT DEFAULT NULL,
    pending_since     TEXT DEFAULT NULL,     -- 进入 pending_self_report 的时刻（Issue #537：主控停机等 worker 自报的超时基准）
    created_at        TEXT DEFAULT (datetime('now'))
)
"""

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
    -- agent|user：谁产生这条消息（202611160001 引入，同步进基表 DDL）
    actor      VARCHAR(128) NOT NULL DEFAULT 'agent',
    UNIQUE(task_id, seq)
)
"""

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
    credential_uid VARCHAR(36) DEFAULT NULL,-- Issue #646 凭据稳定键(UUIDv4,一次性终身不变;与迁移 202611080011 _ensure_column 同形,新库免迁移即带)
    cred_type    VARCHAR(32) NOT NULL DEFAULT 'worker', -- worker(普通设备)/sandbox(沙箱 worker)
    platform     VARCHAR(32) NOT NULL DEFAULT '',        -- cred_type=sandbox 时的沙箱平台(cubesandbox/devbox)
    status       TEXT DEFAULT 'active',     -- active / revoked
    last_seen_at TEXT DEFAULT NULL,
    last_ip      TEXT DEFAULT '',           -- 最近注册/心跳来源 IP(展示用,不入唯一键)
    created_at   TEXT DEFAULT (datetime('now')),
    UNIQUE (token_hash)
)
"""

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
    scene_user_id VARCHAR(36) DEFAULT '',  -- 用户级场景的创建者 ID（''=非用户级；D105 UUID 字符串）
    allow_scene_override INTEGER DEFAULT 0, -- 允许场景覆盖（B2 收编字段，issue_state 兼容）
    updated_at   TEXT DEFAULT (datetime('now')),
    created_at   TEXT DEFAULT (datetime('now')),
    UNIQUE (repo_key, issue_id)
)
"""

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

_ENGINE_LIFECYCLE_AUDIT_DDL = """
CREATE TABLE IF NOT EXISTS grape_t_engine_lifecycle_audit (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    runtime_id  INTEGER NOT NULL,              -- agent_runtimes.id
    runtime_name TEXT DEFAULT '',              -- 引擎名快照
    kind        TEXT NOT NULL,                 -- spawn_fail_alarm / spawn_fail_degraded / recovered / manual_readd
    scene_row_id INTEGER DEFAULT 0,            -- devops_pipeline_scene.id（加回相关）
    node_tag    TEXT DEFAULT '',               -- 场景节点标签
    detail      TEXT DEFAULT '{}',             -- JSON：计数/时长/对账任务快照
    created_at  TEXT DEFAULT (datetime('now'))
)
"""

_DISTRIBUTED_LOCKS_DDL = """
CREATE TABLE IF NOT EXISTS distributed_locks (
    lock_key    VARCHAR(128) NOT NULL,          -- 锁唯一 key（如 'upload_cleanup'）
    holder      VARCHAR(128) NOT NULL,          -- 锁持有者标识（hostname-随机hex）
    locked_at   BIGINT       NOT NULL DEFAULT 0, -- 获取锁时间戳（秒）
    expires_at  BIGINT       NOT NULL DEFAULT 0, -- 锁过期时间戳（秒），过期可被抢占
    update_at   BIGINT       NOT NULL DEFAULT 0, -- 最后更新时间戳（排查用）
    created_at  BIGINT       NOT NULL DEFAULT 0, -- 锁记录首次创建时间戳（排查用）
    PRIMARY KEY (lock_key)
)
"""

_BRANCH_GUARD_STATE_DDL = """
CREATE TABLE IF NOT EXISTS branch_guard_state (
    repo_key            VARCHAR(255) NOT NULL DEFAULT '',
    head_branch         VARCHAR(255) NOT NULL DEFAULT '',
    expected_remote_sha VARCHAR(64)  NOT NULL DEFAULT '',
    pr_number           INTEGER      NOT NULL DEFAULT 0,
    base_branch         VARCHAR(255) NOT NULL DEFAULT '',
    updated_at          TEXT         NOT NULL DEFAULT '',
    pending_expected_sha VARCHAR(64) NOT NULL DEFAULT '',
    pending_prev_sha    VARCHAR(64)  NOT NULL DEFAULT '',
    PRIMARY KEY (repo_key, head_branch)
)
"""

_AUTOPILOT_EXECUTIONS_DDL = """
CREATE TABLE IF NOT EXISTS autopilot_executions (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id         INTEGER NOT NULL,                    -- 外键 -> autopilot_tasks.id
    status          TEXT NOT NULL DEFAULT 'pending',     -- pending/running/success/failed/retrying
    retry_count     INTEGER NOT NULL DEFAULT 0,          -- 已重试次数
    result          MEDIUMTEXT DEFAULT '',               -- 执行结果摘要
    params_json     MEDIUMTEXT DEFAULT '',               -- 本次手动触发的运行参数（JSON）
    error           MEDIUMTEXT DEFAULT '',               -- 错误信息
    a2a_seq         INTEGER DEFAULT NULL,                -- 关联 agent_tasks.event_seq（=exec_id）
    started_at      TEXT DEFAULT '',                     -- 开始时间
    completed_at    TEXT DEFAULT '',                     -- 完成时间
    created_at      TEXT DEFAULT (datetime('now')),
    -- v3.0 新增字段
    agent_type      TEXT DEFAULT '',                     -- 执行快照
    findings_json   MEDIUMTEXT DEFAULT '',               -- findings list JSON
    -- Issue #1546：执行记录直接持有的 agent_tasks.id（非 events.seq），反查唯一权威来源
    agent_task_id   TEXT DEFAULT NULL,                   -- agent_tasks.id（字符串，空=历史行）
    -- Feishu subscription notification: terminal-state notify idempotency guard
    notified        INTEGER NOT NULL DEFAULT 0           -- 0=pending 1=notified (prevents double-send from two terminal hooks)
)
"""

_CUL_DDL = """
CREATE TABLE IF NOT EXISTS grape_t_credential_usage_log (
    id TEXT PRIMARY KEY,
    credential_id TEXT NOT NULL,
    credential_kind TEXT NOT NULL DEFAULT 'repo_credentials',
    user_id VARCHAR(36) NOT NULL,
    repo_key TEXT DEFAULT '',
    initiating_actor_id VARCHAR(36) DEFAULT '',
    executor_context TEXT DEFAULT '',
    operation TEXT DEFAULT '',
    created_at TEXT DEFAULT ''
)
"""

_SC_DDL = """
CREATE TABLE IF NOT EXISTS grape_t_system_credentials (
    id VARCHAR(36) NOT NULL PRIMARY KEY,
    scope_type VARCHAR(16) NOT NULL,
    scope_id VARCHAR(64) NOT NULL,
    cred_type VARCHAR(64) NOT NULL,
    value TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE (scope_type, scope_id, cred_type)
)
"""

_TAKEOVER_SESSIONS_DDL = """
CREATE TABLE IF NOT EXISTS takeover_sessions (
    id                VARCHAR(64) PRIMARY KEY,
    active_key        VARCHAR(64) DEFAULT NULL,
    task_id           VARCHAR(64) NOT NULL,
    event_seq         INTEGER NOT NULL DEFAULT 0,
    repo_key          VARCHAR(255) NOT NULL DEFAULT '',
    issue_id          VARCHAR(32) NOT NULL DEFAULT '',
    agent_type        VARCHAR(32) NOT NULL DEFAULT '',
    -- D105 口径：账号 id 是 uuid 字符串（grape_accounts.id VARCHAR(36)），
    -- INTEGER 会在这里 int() 崩（ValueError），统一 VARCHAR(36)。
    user_id           VARCHAR(36) NOT NULL DEFAULT '0',
    username          VARCHAR(128) NOT NULL DEFAULT '',
    runtime_id        INTEGER NOT NULL DEFAULT 0,
    platform          VARCHAR(32) NOT NULL DEFAULT '',
    host              VARCHAR(128) NOT NULL DEFAULT '',
    client_version    VARCHAR(64) NOT NULL DEFAULT '',
    status            VARCHAR(16) NOT NULL DEFAULT 'active',
    token_sha256      VARCHAR(64) NOT NULL DEFAULT '',
    token_expires_at  INTEGER NOT NULL DEFAULT 0,
    token_issued_at   INTEGER NOT NULL DEFAULT 0,
    handoff_seq       INTEGER NOT NULL DEFAULT 0,
    next_agent        VARCHAR(32) NOT NULL DEFAULT '',
    bundle_json       MEDIUMTEXT,
    warnings_json     TEXT,
    summary           MEDIUMTEXT,
    evidence          MEDIUMTEXT,
    created_at        TEXT DEFAULT (datetime('now')),
    updated_at        TEXT DEFAULT (datetime('now')),
    finished_at       TEXT DEFAULT ''
)
"""

# Issue #3580/P3：webhook 事件持久收件箱（入队与消费同在 Event 进程）。
# 幂等键 = (repo_key, delivery_id)；delivery_id 取平台投递 ID 头，缺失则用 body sha256。
# claim_owner + lease_expires_at 提供租约（进程崩溃后过期可被重放消费）。
# Issue #3580 复审：last_error 曾写成 `TEXT NOT NULL DEFAULT ''` —— MySQL 下 TEXT 列
# 带不了字面默认值（兼容层会剥掉），严格模式里任何不写该列的 INSERT 直接 1364。
# 改可空 MEDIUMTEXT，入队显式写 ''；存量库由迁移 202611200004 收敛。
_WEBHOOK_INBOX_DDL = """
CREATE TABLE IF NOT EXISTS grape_t_webhook_inbox (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    repo_key VARCHAR(255) NOT NULL,
    provider VARCHAR(64) NOT NULL DEFAULT '',
    delivery_id VARCHAR(128) NOT NULL,
    event_header VARCHAR(128) NOT NULL DEFAULT '',
    payload MEDIUMTEXT NOT NULL,
    headers TEXT NOT NULL DEFAULT '{}',
    status VARCHAR(16) NOT NULL DEFAULT 'pending',
    attempts INTEGER NOT NULL DEFAULT 0,
    max_attempts INTEGER NOT NULL DEFAULT 5,
    claim_owner VARCHAR(128) NOT NULL DEFAULT '',
    claimed_at VARCHAR(32) NOT NULL DEFAULT '',
    lease_expires_at VARCHAR(32) NOT NULL DEFAULT '',
    next_attempt_at VARCHAR(32) NOT NULL DEFAULT '',
    last_error MEDIUMTEXT DEFAULT NULL,
    created_at TEXT DEFAULT (datetime('now')),
    updated_at TEXT DEFAULT (datetime('now')),
    finished_at VARCHAR(32) NOT NULL DEFAULT '',
    UNIQUE(repo_key, delivery_id)
)
"""

# Issue #3580 复审 A7：收件箱是纯追加的高写入表，dead/长期 pending 若只删不归档，
# 故障排查就失去原始投递；若只留不删，单表无界增长。归档表与主表同构、无唯一键
# （同一 delivery 允许在主/归档各留一份），由 purge 每小时搬一次。
_WEBHOOK_INBOX_ARCHIVE_DDL = """
CREATE TABLE IF NOT EXISTS grape_t_webhook_inbox_archive (
    repo_key VARCHAR(255) NOT NULL,
    provider VARCHAR(64) NOT NULL DEFAULT '',
    delivery_id VARCHAR(128) NOT NULL,
    event_header VARCHAR(128) NOT NULL DEFAULT '',
    payload MEDIUMTEXT NOT NULL,
    headers TEXT NOT NULL DEFAULT '{}',
    status VARCHAR(16) NOT NULL DEFAULT 'pending',
    attempts INTEGER NOT NULL DEFAULT 0,
    max_attempts INTEGER NOT NULL DEFAULT 5,
    claim_owner VARCHAR(128) NOT NULL DEFAULT '',
    claimed_at VARCHAR(32) NOT NULL DEFAULT '',
    lease_expires_at VARCHAR(32) NOT NULL DEFAULT '',
    next_attempt_at VARCHAR(32) NOT NULL DEFAULT '',
    last_error MEDIUMTEXT DEFAULT NULL,
    created_at TEXT DEFAULT (datetime('now')),
    updated_at TEXT DEFAULT (datetime('now')),
    finished_at VARCHAR(32) NOT NULL DEFAULT '',
    archived_at TEXT DEFAULT (datetime('now')),
    UNIQUE(repo_key, delivery_id, archived_at)
)
"""

# A7 残留：清扫/排障都按 status、时间过滤，补索引（主表与归档表各自）
# MySQL 不支持 `CREATE INDEX IF NOT EXISTS`（MariaDB 才支持），所以索引不做成裸 DDL，
# 由 ensure 侧先查 information_schema 再建（见 webhook_inbox._ensure_index）。
_WEBHOOK_INBOX_INDEXES = (
    ("grape_t_webhook_inbox", "idx_webhook_inbox_status_created", "(status, created_at)"),
    ("grape_t_webhook_inbox_archive", "idx_webhook_inbox_archived", "(archived_at)"),
)
