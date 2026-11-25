"""DDL：mgmt 域基表建表语句（由 db_setup 拆分而来，归属本域）。

搬迁原因见 scripts/split_schema_ddl.py：DDL 属表的归属域（方案 D9），
放在 owner 域模块里才能让「跨域引用」统计反映真实依赖。
行为不变：db_setup 仍按原顺序执行这些语句。
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

_PATROL_FINDINGS_DDL = """
CREATE TABLE IF NOT EXISTS grape_t_patrol_findings (
    id            TEXT PRIMARY KEY,           -- uuid（硬性要求：新表主键一律 UUID，Python 侧生成，禁自增）
    kind          TEXT NOT NULL,            -- broken_chain | zombie_running_signal | zombie_running_window | zombie_queued_dead | zombie_queued_window | stalled_issue
    task_id       TEXT NOT NULL DEFAULT '',  -- agent_tasks.id（UUID 引用；合成键空串参与唯一约束）
    event_seq     INTEGER NOT NULL DEFAULT 0, -- 关联 events.seq（可空时用 0，参与唯一约束）
    repo_key      TEXT DEFAULT '',
    issue_id      TEXT DEFAULT '',
    owner         TEXT NOT NULL DEFAULT 0,  -- 归属人 user_id（D16：路径 B=host_user_id；0=unowned 置顶）
    detail        TEXT DEFAULT '',          -- 判定输入快照
    suggestion    TEXT DEFAULT '',          -- 建议处置（Phase 1 只写建议不执行）
    status        TEXT NOT NULL DEFAULT 'open',  -- open | claiming | compensated | dismissed | false_positive
    action_meta   TEXT DEFAULT '',
    created_at    TEXT DEFAULT '',
    updated_at    TEXT DEFAULT '',
    UNIQUE(kind, task_id, issue_id, event_seq)
    -- 部署验证修正（2026-11-02 实测）：issue 级 finding（stalled_issue）task_id 恒为空串，
    -- 原三列唯一键使全部 issue 级发现共享同一键（14 条只存 1 条）——issue_id 进键。
)
"""

_SCHEMA_MIGRATIONS_DDL = """
CREATE TABLE IF NOT EXISTS grape_t_schema_migrations (
    version     VARCHAR(32)  NOT NULL,
    name        VARCHAR(128) NOT NULL,
    kind        VARCHAR(16)  NOT NULL,
    applied_at  DATETIME     NOT NULL,
    duration_ms INT          DEFAULT 0,
    checksum    VARCHAR(64)  DEFAULT NULL,
    PRIMARY KEY (version)
)
"""

_FEISHU_ONBOARD_TASK_DDL = """
CREATE TABLE IF NOT EXISTS feishu_onboard_task (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id TEXT UNIQUE NOT NULL,
    bot_type TEXT NOT NULL,
    repo_key TEXT DEFAULT '',
    user_id TEXT NOT NULL,
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

_PIPELINE_INSTANCES_DDL = """
CREATE TABLE IF NOT EXISTS pipeline_instances (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    repo_key     TEXT NOT NULL,
    pipeline_id  TEXT DEFAULT '',
    instance_id  TEXT DEFAULT '',
    status       TEXT DEFAULT 'pending',
    created_at   TEXT DEFAULT (datetime('now')),
    updated_at   TEXT DEFAULT (datetime('now'))
)
"""

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
    scene_type  TEXT DEFAULT 'standard',  -- standard | pr_subscription | org
    is_pinned   INTEGER NOT NULL DEFAULT 0,
    pinned_at   VARCHAR(32) DEFAULT NULL,
    UNIQUE (scene_id, repo_key)
)
"""

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
    github_username TEXT DEFAULT '',
    provider VARCHAR(32) DEFAULT NULL,
    username VARCHAR(128) DEFAULT '',
    remark VARCHAR(128) DEFAULT '',
    created_by VARCHAR(64) DEFAULT '',
    created_at TEXT DEFAULT (datetime('now')),
    UNIQUE (gitcode_username)
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

# Issue #2761：场景/技能统一分类字典（一级域 + 二级子类）。
# 旧 scene_category / skill_category 保留不动，仅新字段 cat_l1_id / cat_l2_id 引用本表。
_CATEGORY_DICT_DDL = """
CREATE TABLE IF NOT EXISTS grape_t_category_dict (
    -- id 显式分配、非自增（A12）：一级 = 1000 + order_num，二级 = 一级 id * 100 + order_num。
    -- 稳定可预测，种子与同步两侧都按同一规则算，无需 lastrowid 回查。
    id          INTEGER PRIMARY KEY,
    parent_id   INTEGER NOT NULL DEFAULT 0,
    level       INTEGER NOT NULL DEFAULT 1,
    name        TEXT NOT NULL,
    slug        VARCHAR(64) NOT NULL,
    icon        TEXT DEFAULT '',
    order_num   INTEGER DEFAULT 0,
    applies_to  VARCHAR(16) NOT NULL DEFAULT 'both',
    is_active   INTEGER NOT NULL DEFAULT 1,
    created_at  TEXT DEFAULT (datetime('now')),
    updated_at  TEXT DEFAULT (datetime('now')),
    UNIQUE (parent_id, slug)
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
    cat_l1_id   INTEGER DEFAULT 0,     -- Issue #2761：统一分类字典一级域
    cat_l2_id   INTEGER DEFAULT 0,     -- Issue #2761：统一分类字典二级子类
    is_featured INTEGER DEFAULT 0,
    like_count  INTEGER DEFAULT 0,
    source      TEXT DEFAULT 'official',
    source_user_id TEXT DEFAULT 0,
    recommended_template VARCHAR(191) DEFAULT '',
    nodes_json  TEXT DEFAULT '[]',
    onboarding  TEXT DEFAULT 'repo-onboarding',
    tags        VARCHAR(256) DEFAULT '',  -- 标签，逗号分隔（如 build,deploy,ci）
    author      VARCHAR(64) DEFAULT '',
    content_hash VARCHAR(64) DEFAULT '',  -- 内容 hash（定时同步增量检测用）
    obs_icon_key VARCHAR(256) DEFAULT '',  -- OBS 桶 icon 对象 key（查询时动态生成签名 URL）
    preset_custom_fields TEXT DEFAULT '[]',  -- 场景预置自定义参数声明（scene.json custom_fields 数组，Issue #246）
    is_active   INTEGER DEFAULT 1,
    scope       VARCHAR(32) DEFAULT 'official',  -- 可见范围：official/repo/org
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
    user_id     TEXT NOT NULL,
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
    user_id     TEXT NOT NULL,
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
    -- Issue #3733：不可派发状态（''=正常；blocked_empty_instruction=已启用但指令为空）。
    -- 状态/原因/时间三列以库内为准（跨进程、跨重启权威）；原因直接进任务页展示。
    dispatch_state        TEXT NOT NULL DEFAULT '',
    dispatch_state_reason TEXT NOT NULL DEFAULT '',
    dispatch_state_at     TEXT NOT NULL DEFAULT '',
    created_at      TEXT DEFAULT (datetime('now')),
    updated_at      TEXT DEFAULT (datetime('now')),
    created_by      INTEGER DEFAULT 0,                   -- 创建者 user_id
    -- Issue #3800：环境性失败（绑定引擎离线/悬空）退避与熔断状态。
    -- 落库而非内存态：进程重启后退避仍生效；`circuit_paused` 与 `enabled` 分离，
    -- 保证「系统自动暂停」可自动解除，而不会抹掉用户主动停用的意图。
    consecutive_failures INTEGER NOT NULL DEFAULT 0,     -- 连续同因失败次数
    last_error_code  TEXT DEFAULT '',                    -- 最近一次失败错误码（同因判据）
    offline_since    TEXT DEFAULT '',                    -- 本次离线/悬空起始时间
    backoff_until    TEXT DEFAULT '',                    -- 退避截止时间（调度闸门）
    circuit_paused   INTEGER NOT NULL DEFAULT 0          -- 1=熔断自动暂停（引擎恢复后自愈）
)
"""

_AUTOPILOT_SUBSCRIPTIONS_DDL = """
CREATE TABLE IF NOT EXISTS autopilot_subscriptions (
    task_id     INTEGER NOT NULL,
    user_id     TEXT NOT NULL,
    created_at  TEXT DEFAULT (datetime('now')),
    PRIMARY KEY (task_id, user_id)
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

# 新增列口径：org_repo_* 与 domains/mgmt/migrations/org_repo_create_options.py 的
# ORG_REPO_COLUMN_DDL 对应。TEXT DEFAULT '' 在 MySQL 上会被静默丢掉默认值，存量库
# 补列用 TEXT（无 DEFAULT），读侧统一 `or ''`，两栈行为等价。
_ORGANIZATION_DDL = """
CREATE TABLE IF NOT EXISTS organization (
    id              TEXT,
    name            VARCHAR(128) NOT NULL,
    slug            VARCHAR(64) NOT NULL UNIQUE,
    description     TEXT DEFAULT '',
    avatar_url      VARCHAR(512) DEFAULT '',
    owner_id        TEXT NOT NULL,
    is_personal     INTEGER DEFAULT 0,
    settings        TEXT DEFAULT '{}',
    max_members     INTEGER DEFAULT 50,
    max_workers     INTEGER DEFAULT 10,
    max_repos       INTEGER DEFAULT 20,
    org_repo_key    TEXT DEFAULT '',
    org_repo_provider TEXT DEFAULT '',
    org_repo_owner  TEXT DEFAULT '',
    org_repo_name   TEXT DEFAULT '',
    org_repo_path   TEXT DEFAULT '',
    org_repo_private INTEGER DEFAULT 1,
    org_scene_id    TEXT DEFAULT '',
    provision_status TEXT DEFAULT 'none',
    provision_error TEXT DEFAULT '',
    provisioned_at  TEXT DEFAULT '',
    provision_attempt TEXT DEFAULT '',
    created_at      TEXT DEFAULT (datetime('now')),
    updated_at      TEXT DEFAULT (datetime('now'))
)"""

_ORG_MEMBER_DDL = """
CREATE TABLE IF NOT EXISTS org_member (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    org_id          TEXT NOT NULL,
    user_id         TEXT NOT NULL,
    role            TEXT NOT NULL DEFAULT 'member',
    invited_by      TEXT DEFAULT 0,
    joined_at       TEXT DEFAULT (datetime('now')),
    UNIQUE (org_id, user_id)
)"""

_ORG_INVITATION_DDL = """
CREATE TABLE IF NOT EXISTS org_invitation (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    org_id          TEXT NOT NULL,
    inviter_id      TEXT NOT NULL,
    invitee_email   TEXT DEFAULT '',
    invitee_user_id TEXT DEFAULT 0,
    role            TEXT NOT NULL DEFAULT 'member',
    status          TEXT DEFAULT 'pending',
    expires_at      TEXT,
    created_at      TEXT DEFAULT (datetime('now')),
    updated_at      TEXT DEFAULT (datetime('now'))
)"""

_GRAPE_ACCOUNTS_DDL = """
CREATE TABLE IF NOT EXISTS grape_accounts (
    id              TEXT PRIMARY KEY,      -- D105 第二批：UUIDv7（应用层生成）；存量整型值字符串化保留
    username        VARCHAR(64) NOT NULL UNIQUE,
    email           VARCHAR(255) DEFAULT NULL,
    email_source    VARCHAR(16) DEFAULT NULL,
    password_hash   TEXT NOT NULL DEFAULT '',
    nickname        VARCHAR(128) DEFAULT '',
    avatar_url      VARCHAR(512) DEFAULT '',
    status          INTEGER DEFAULT 1,
    token_version   INTEGER DEFAULT 0,
    email_verified  INTEGER DEFAULT 0,
    email_is_placeholder INTEGER DEFAULT 0,
    created_by_oauth    INTEGER DEFAULT 0,
    last_active_at  TEXT DEFAULT NULL,
    last_login_at   TEXT DEFAULT NULL,
    created_at      TEXT DEFAULT (datetime('now')),
    updated_at      TEXT DEFAULT (datetime('now'))
)
"""

_EXTERNAL_IDENTITIES_DDL = """
CREATE TABLE IF NOT EXISTS external_identities (
    id                TEXT,
    grape_user_id     TEXT NOT NULL,
    provider          VARCHAR(32) NOT NULL,
    external_id       VARCHAR(128) NOT NULL,
    external_login    VARCHAR(128) DEFAULT '',
    external_name     VARCHAR(128) DEFAULT '',
    external_avatar   VARCHAR(512) DEFAULT '',
    access_token      TEXT DEFAULT '',
    refresh_token     TEXT DEFAULT '',
    token_expires_at  TEXT DEFAULT '',
    metadata          TEXT DEFAULT '{}',
    status            INTEGER DEFAULT 1,
    unbound_at        TEXT DEFAULT NULL,
    created_at        TEXT DEFAULT (datetime('now')),
    updated_at        TEXT DEFAULT (datetime('now')),
    UNIQUE (provider, external_id),
    UNIQUE (grape_user_id, provider)
)
"""

_VERIFICATION_CODES_DDL = """
CREATE TABLE IF NOT EXISTS verification_codes (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    email        VARCHAR(255) NOT NULL,
    code_hash    VARCHAR(191) NOT NULL,   -- Issue #3469 I：存 "{kid}:{digest}"，64 装不下
    scene        VARCHAR(32) NOT NULL,
    ip           VARCHAR(64) DEFAULT '',
    attempts     INTEGER DEFAULT 0,
    used         INTEGER DEFAULT 0,
    expires_at   TEXT NOT NULL,
    created_at   TEXT DEFAULT (datetime('now'))
)
"""

_AUDIT_LOGS_DDL = """
CREATE TABLE IF NOT EXISTS audit_logs (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    seq          INTEGER NOT NULL UNIQUE,
    event_type   VARCHAR(64) NOT NULL,
    user_id      TEXT DEFAULT 0,
    operator_id  TEXT DEFAULT '0',
    ip           VARCHAR(64) DEFAULT '',
    detail       TEXT DEFAULT '{}',
    prev_hash    VARCHAR(64) DEFAULT '',
    curr_hash    VARCHAR(64) DEFAULT '',
    created_at   TEXT DEFAULT (datetime('now'))
)
"""

_SIGNUP_WHITELIST_DDL = """
CREATE TABLE IF NOT EXISTS signup_whitelist (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    type         VARCHAR(16) NOT NULL,
    value        VARCHAR(255) NOT NULL,
    remark       VARCHAR(128) DEFAULT '',
    created_by   VARCHAR(64) DEFAULT '',
    created_at   TEXT DEFAULT (datetime('now')),
    UNIQUE (type, value)
)
"""

_LOGIN_FAILURES_DDL = """
CREATE TABLE IF NOT EXISTS login_failures (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    identifier   VARCHAR(255) NOT NULL,
    ip           VARCHAR(64) DEFAULT '',
    created_at   TEXT DEFAULT (datetime('now'))
)
"""

_CREDENTIAL_REVEAL_LOG_DDL = """
CREATE TABLE IF NOT EXISTS grape_t_credential_reveal_log (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id      TEXT NOT NULL,
    provider     VARCHAR(32) NOT NULL,
    field        VARCHAR(16) NOT NULL,
    ip           VARCHAR(64) DEFAULT '',
    created_at   TEXT DEFAULT (datetime('now'))
)
"""

_EXCHANGE_CODES_DDL = """
CREATE TABLE IF NOT EXISTS exchange_codes (
    code         TEXT PRIMARY KEY,
    user_id      TEXT NOT NULL,
    username     VARCHAR(64) NOT NULL,
    provider     VARCHAR(32) NOT NULL,
    token_version INTEGER DEFAULT 0,
    is_new       INTEGER DEFAULT 0,
    created_at   REAL NOT NULL,
    expires      REAL NOT NULL
)
"""

_BINDING_TICKETS_DDL = """
CREATE TABLE IF NOT EXISTS binding_tickets (
    ticket           TEXT PRIMARY KEY,
    initiating_user_id  TEXT NOT NULL,
    provider         VARCHAR(32) NOT NULL,
    external_id      VARCHAR(128) NOT NULL,
    external_login   VARCHAR(128) DEFAULT '',
    external_name    VARCHAR(128) DEFAULT '',
    external_avatar  VARCHAR(512) DEFAULT '',
    original_user_id TEXT NOT NULL,
    created_at       REAL NOT NULL,
    expires          REAL NOT NULL
)
"""

_COMMUNITY_DDL = """
CREATE TABLE IF NOT EXISTS community_posts (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id         TEXT NOT NULL DEFAULT 0,
    username        TEXT NOT NULL DEFAULT '',
    title           TEXT NOT NULL,
    content         TEXT DEFAULT '',
    category        TEXT DEFAULT 'tech',
    views           INTEGER DEFAULT 0,
    likes           INTEGER DEFAULT 0,
    comments_count  INTEGER DEFAULT 0,
    is_deleted      INTEGER DEFAULT 0,
    created_at      TEXT DEFAULT (datetime('now'))
)"""

_COMMUNITY_COMMENTS_DDL = """
CREATE TABLE IF NOT EXISTS community_comments (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    post_id     INTEGER NOT NULL,
    user_id     TEXT NOT NULL DEFAULT 0,
    username    TEXT NOT NULL DEFAULT '',
    content     TEXT NOT NULL,
    is_deleted  INTEGER DEFAULT 0,
    created_at  TEXT DEFAULT (datetime('now'))
)"""

_COMMUNITY_LIKES_DDL = """
CREATE TABLE IF NOT EXISTS community_post_likes (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    post_id     INTEGER NOT NULL,
    user_id     TEXT NOT NULL,
    UNIQUE(post_id, user_id)
)"""


# ===== 渠道族（Issue #742 §5.1，2026-11-15 由 scripts/migrations/001 归位）=====
# 归属 mgmt：读写语句都在 mgmt public（runtime public 仅转发）。此前 DDL 只存在于
# scripts/migrations/001_channel_tables.py，每个进程启动都自己建一份 → 三库模式下
# mgmt/runtime/insight 各有一张同名基表。现在集中在本域，db_setup 只在 owner 进程建。
# 方言约定：SQLite 方言书写，MySQL 经 mysql_compat.translate_sql 翻译；
# 字符串内不要写 '--' 注释（executescript 按 ';' 切句会让方言翻译失效）。
_CHANNEL_INSTALLATION_DDL = """
CREATE TABLE IF NOT EXISTS grape_t_channel_installation (
    id                  TEXT PRIMARY KEY,
    channel_type        VARCHAR(32) NOT NULL CHECK (channel_type IN ('feishu','dingtalk','slack','wecom','telegram','wechat')),
    scope               VARCHAR(32) NOT NULL CHECK (scope IN ('user','repo')),
    owner_user_id       VARCHAR(191) NOT NULL DEFAULT '',
    repo_key            VARCHAR(191) NOT NULL DEFAULT '',
    config              TEXT NOT NULL DEFAULT '{}',
    config_fingerprint  VARCHAR(64) NOT NULL DEFAULT '',
    status              VARCHAR(32) NOT NULL DEFAULT 'active' CHECK (status IN ('active','revoked','disabled')),
    legacy_app_id       VARCHAR(191) NOT NULL DEFAULT '',
    created_at          TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at          TEXT NOT NULL DEFAULT (datetime('now'))
)
"""

_CHANNEL_SESSION_BINDING_DDL = """
CREATE TABLE IF NOT EXISTS grape_t_channel_chat_session_binding (
    id                 TEXT PRIMARY KEY,
    installation_id    TEXT NOT NULL,
    channel_type       VARCHAR(32) NOT NULL,
    channel_chat_id    TEXT NOT NULL,
    chat_type          TEXT NOT NULL DEFAULT 'p2p' CHECK (chat_type IN ('p2p','group')),
    scope              VARCHAR(32) NOT NULL DEFAULT 'user',
    repo_key           VARCHAR(191) NOT NULL DEFAULT '',
    agent_type         VARCHAR(32) NOT NULL DEFAULT 'chat',
    runtime_session_id TEXT NOT NULL DEFAULT '',
    config             TEXT NOT NULL DEFAULT '{}',
    last_active_at     TEXT,
    created_at         TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at         TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (installation_id, channel_chat_id)
)
"""

_CHANNEL_INBOUND_DEDUP_DDL = """
CREATE TABLE IF NOT EXISTS grape_t_channel_inbound_dedup (
    installation_id TEXT NOT NULL,
    message_id      TEXT NOT NULL,
    state           VARCHAR(32) NOT NULL DEFAULT 'claimed' CHECK (state IN ('claimed','marked')),
    claim_token     VARCHAR(64) NOT NULL DEFAULT '',
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (installation_id, message_id)
)
"""

_CHANNEL_SENDER_IDENTITY_DDL = """
CREATE TABLE IF NOT EXISTS grape_t_channel_sender_identity (
    installation_id         TEXT NOT NULL,
    platform_user_id        TEXT NOT NULL,
    platform_stable_user_id TEXT NOT NULL DEFAULT '',
    user_id                 TEXT NOT NULL,
    PRIMARY KEY (installation_id, platform_user_id)
)
"""

_CHANNEL_OUTBOUND_CARD_DDL = """
CREATE TABLE IF NOT EXISTS grape_t_channel_outbound_card (
    out_track_id     VARCHAR(191) PRIMARY KEY,
    installation_id  TEXT NOT NULL,
    channel_type     VARCHAR(32) NOT NULL DEFAULT 'dingtalk',
    channel_chat_id  TEXT NOT NULL DEFAULT '',
    chat_type        TEXT NOT NULL DEFAULT 'p2p',
    sender_staff_id  TEXT NOT NULL DEFAULT '',
    card_template_id VARCHAR(191) NOT NULL DEFAULT '',
    created_at       TEXT NOT NULL DEFAULT (datetime('now')),
    -- Issue #4339：卡片体随模板增长，TEXT(65,535) 不足以容纳超长 IM 卡片；
    -- 与 events.payload 同批升 MEDIUMTEXT（外发重试的事实源，截断不可恢复）。
    payload          MEDIUMTEXT NOT NULL DEFAULT '{}',
    status           VARCHAR(32) NOT NULL DEFAULT 'pending' CHECK (status IN ('pending','sending','sent','failed','expired')),
    attempt_count    INTEGER NOT NULL DEFAULT 0,
    next_retry_at    VARCHAR(32) NOT NULL DEFAULT '',
    expires_at       VARCHAR(32) NOT NULL DEFAULT '',
    last_error       TEXT NOT NULL DEFAULT '',
    updated_at       TEXT NOT NULL DEFAULT (datetime('now'))
)
"""

_CHANNEL_LEASE_DDL = """
CREATE TABLE IF NOT EXISTS grape_t_channel_lease (
    lease_key          TEXT PRIMARY KEY,
    channel_type       VARCHAR(32) NOT NULL DEFAULT '',
    installation_id    TEXT NOT NULL DEFAULT '',
    lease_token        VARCHAR(128) DEFAULT NULL,
    lease_owner        VARCHAR(128) DEFAULT NULL,
    lease_expires_at   VARCHAR(32) DEFAULT NULL,
    lease_acquired_at  VARCHAR(32) DEFAULT NULL,
    updated_at         TEXT NOT NULL DEFAULT (datetime('now'))
)
"""

_CHANNEL_INBOUND_MESSAGE_DDL = """
CREATE TABLE IF NOT EXISTS grape_t_channel_inbound_message (
    installation_id TEXT NOT NULL,
    message_id      TEXT NOT NULL,
    chat_id         TEXT NOT NULL DEFAULT '',
    sender_id       TEXT NOT NULL DEFAULT '',
    text            TEXT NOT NULL DEFAULT '',
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (installation_id, message_id)
)
"""

# 顺序即建表顺序（inbound_message 依赖 installation/dedup 的语义，仅逻辑上相关，无外键）
CHANNEL_TABLE_DDL = (
    ("grape_t_channel_installation", _CHANNEL_INSTALLATION_DDL),
    ("grape_t_channel_chat_session_binding", _CHANNEL_SESSION_BINDING_DDL),
    ("grape_t_channel_inbound_dedup", _CHANNEL_INBOUND_DEDUP_DDL),
    ("grape_t_channel_sender_identity", _CHANNEL_SENDER_IDENTITY_DDL),
    ("grape_t_channel_outbound_card", _CHANNEL_OUTBOUND_CARD_DDL),
    ("grape_t_channel_lease", _CHANNEL_LEASE_DDL),
    ("grape_t_channel_inbound_message", _CHANNEL_INBOUND_MESSAGE_DDL),
)


# ── 沙箱配置面 DDL（原在 runtime/agents/sandbox/schema.py，按 table_ownership 迁入）──────
# 这 6 张表归 mgmt（配置面：白名单/路径/开关/模板/凭据/代理 token），
# 但历史建表语句放在 runtime 的 ensure_sandbox_tables() 里，而该函数在
# main.py 与 runtime/agent.py 都会被调用 —— 三库拓扑下四个进程各自把表建进自己的库。
# 迁到 owner 域后，只有 mgmt 进程建表；runtime 侧改为按 should_skip 跳过。
SANDBOX_CONFIG_DDL: tuple[tuple[str, str], ...] = (
    ("grape_t_engine_sandbox_paths", """CREATE TABLE IF NOT EXISTS grape_t_engine_sandbox_paths (
    id TEXT PRIMARY KEY,
    backend_name  TEXT NOT NULL,
    path          TEXT NOT NULL,
    access        TEXT NOT NULL DEFAULT 'ro',
    created_at    TEXT DEFAULT (datetime('now')),
    updated_at    TEXT DEFAULT (datetime('now')),
    UNIQUE(backend_name, path))"""),
    ("grape_t_engine_sandbox_config", """CREATE TABLE IF NOT EXISTS grape_t_engine_sandbox_config (
    id TEXT PRIMARY KEY,
    runtime_id    INTEGER NOT NULL,
    engine_name   TEXT NOT NULL,
    sandbox_mode  TEXT NOT NULL DEFAULT 'required',
    degrade_policy TEXT DEFAULT 'compatible',
    created_at    TEXT DEFAULT (datetime('now')),
    updated_at    TEXT DEFAULT (datetime('now')),
    UNIQUE(runtime_id, engine_name))"""),
    ("grape_t_engine_sandbox_whitelist", """CREATE TABLE IF NOT EXISTS grape_t_engine_sandbox_whitelist (
    id TEXT PRIMARY KEY,
    backend_name  TEXT NOT NULL,
    domain        TEXT NOT NULL,
    method        TEXT NOT NULL CHECK(method IN ('GET','POST','PUT','DELETE','PATCH')),
    path_prefix   TEXT NOT NULL DEFAULT '*',
    scheme        TEXT NOT NULL DEFAULT 'https' CHECK(scheme IN ('http','https')),
    port          INTEGER NOT NULL DEFAULT 443 CHECK(port BETWEEN 1 AND 65535),
    created_at    TEXT DEFAULT (datetime('now')),
    updated_at    TEXT DEFAULT (datetime('now')),
    UNIQUE(backend_name, domain, method, scheme, port))"""),
    ("grape_t_task_proxy_tokens", """CREATE TABLE IF NOT EXISTS grape_t_task_proxy_tokens (
    id TEXT PRIMARY KEY,
    task_id      TEXT NOT NULL,
    token_hash   TEXT NOT NULL,
    backend_name TEXT NOT NULL,
    created_at   TEXT DEFAULT (datetime('now')),
    expires_at   TEXT,
    UNIQUE(task_id, backend_name))"""),
    ("grape_t_user_llm_credentials", """CREATE TABLE IF NOT EXISTS grape_t_user_llm_credentials (
    id TEXT PRIMARY KEY,
    user_id VARCHAR(36) NOT NULL,
    provider VARCHAR(64) NOT NULL,
    api_key_enc TEXT NOT NULL,
    base_url VARCHAR(512) DEFAULT '',
    model VARCHAR(128) DEFAULT '',
    enabled INTEGER DEFAULT 1,
    key_version INTEGER DEFAULT 1,
    engine_type VARCHAR(64) NOT NULL DEFAULT 'common',
    created_at TEXT DEFAULT (datetime('now')),
    updated_at TEXT DEFAULT (datetime('now')),
    UNIQUE(user_id, engine_type))"""),
    ("grape_t_sandbox_templates", """CREATE TABLE IF NOT EXISTS grape_t_sandbox_templates (
    id TEXT PRIMARY KEY,
    name VARCHAR(255) NOT NULL,
    provider VARCHAR(64) NOT NULL,
    template_id VARCHAR(255) NOT NULL,
    alias VARCHAR(255) DEFAULT '',
    description VARCHAR(512) DEFAULT '',
    engines VARCHAR(1024) DEFAULT '[]',
    preinstalled VARCHAR(2048) DEFAULT '{}',
    disk_size_mb INTEGER DEFAULT 0,
    enabled INTEGER DEFAULT 1,
    last_check_at VARCHAR(64) DEFAULT '',
    last_check_status VARCHAR(32) DEFAULT '',
    last_check_error VARCHAR(512) DEFAULT '',
    created_at VARCHAR(64) NOT NULL,
    updated_at VARCHAR(64) NOT NULL,
    created_by VARCHAR(64) DEFAULT '',
    updated_by VARCHAR(64) DEFAULT '')"""),
)


OFFICIAL_SANDBOX_DOMAINS: tuple[tuple[str, str, str, str, int], ...] = (
    ("opengw.clouddeveloper.club", "POST", "*", "https", 443),
    ("api.deepseek.com", "POST", "*", "https", 443),
    ("api.openai.com", "POST", "*", "https", 443),
    ("api.anthropic.com", "POST", "*", "https", 443),
    ("gitcode.com", "GET", "*", "https", 443),
    ("gitcode.com", "POST", "*", "https", 443),
    ("api.gitcode.com", "GET", "*", "https", 443),
    ("api.gitcode.com", "POST", "*", "https", 443),
    ("api.gitcode.com", "PUT", "*", "https", 443),
    ("api.gitcode.com", "DELETE", "*", "https", 443),
    ("api.gitcode.com", "PATCH", "*", "https", 443),
    ("objects.gitcode.com", "GET", "*", "https", 443),
    ("objects.gitcode.com", "PUT", "*", "https", 443),
    ("objects.gitcode.com", "POST", "*", "https", 443),
    ("obs.cn-north-4.myhuaweicloud.com", "GET", "*", "https", 443),
    ("obs.cn-north-4.myhuaweicloud.com", "PUT", "*", "https", 443),
    ("obs.cn-north-4.myhuaweicloud.com", "POST", "*", "https", 443),
    ("obs.cn-north-4.myhuaweicloud.com", "DELETE", "*", "https", 443),
    ("api.github.com", "GET", "*", "https", 443),
    ("api.github.com", "POST", "*", "https", 443),
    ("api.github.com", "PUT", "*", "https", 443),
    ("api.github.com", "PATCH", "*", "https", 443),
    ("api.github.com", "DELETE", "*", "https", 443),
)
# ===== Issue #2112：组织需求跨仓下派 =====
# 下派批次：一个组织需求 → N 个目标仓库子 Issue 的编排单元。
_ORG_DISPATCH_DDL = """
CREATE TABLE IF NOT EXISTS grape_t_org_dispatch (
    id                TEXT PRIMARY KEY,           -- uuid（32 hex，Python 侧生成，禁自增）
    org_id            TEXT NOT NULL DEFAULT '',
    org_issue_number  TEXT NOT NULL DEFAULT '',
    org_repo_key      TEXT NOT NULL DEFAULT '',
    scene_id          TEXT DEFAULT '',
    status            TEXT NOT NULL DEFAULT 'planning',
    plan_json         TEXT DEFAULT '{}',
    context_json      TEXT DEFAULT '{}',
    context_hash      TEXT DEFAULT '',
    idempotency_key   TEXT DEFAULT NULL,
    created_by        TEXT NOT NULL DEFAULT '',
    created_at        TEXT DEFAULT (datetime('now')),
    updated_at        TEXT DEFAULT (datetime('now'))
)
"""

# 下派目标：每个 (dispatch, repo) 唯一，承载子 Issue 与终态；UNIQUE 为幂等上界。
_ORG_DISPATCH_TARGET_DDL = """
CREATE TABLE IF NOT EXISTS grape_t_org_dispatch_target (
    id                TEXT PRIMARY KEY,           -- uuid（32 hex，Python 侧生成，禁自增）
    dispatch_id       TEXT NOT NULL,
    repo_key          TEXT NOT NULL,
    scene_id          TEXT DEFAULT '',
    child_issue_number TEXT DEFAULT '',
    child_task_id     INTEGER DEFAULT 0,
    status            TEXT NOT NULL DEFAULT 'pending',
    done_when         TEXT NOT NULL DEFAULT 'issue_done',  -- issue_done / pr_merged / task_terminal
    pr_number         INTEGER DEFAULT 0,
    error             TEXT DEFAULT '',
    started_at        TEXT DEFAULT NULL,
    finished_at       TEXT DEFAULT NULL,
    created_at        TEXT DEFAULT (datetime('now')),
    updated_at        TEXT DEFAULT (datetime('now')),
    UNIQUE (dispatch_id, repo_key)
)
"""

# Issue #2251：聚合触发幂等台账（dispatch_id + terminal_status 唯一）。
# sweeper 首次发现批次进入终态时 INSERT OR IGNORE 抢占，只有抢占成功者写收口评论
# 并持久化 aggregate_recheck 事件；重复 sweep / 多实例并发都只触发一次。
_ORG_DISPATCH_AGGREGATE_DDL = """
CREATE TABLE IF NOT EXISTS grape_t_org_dispatch_aggregate (
    id                TEXT PRIMARY KEY,           -- uuid（32 hex，Python 侧生成，禁自增）
    dispatch_id       TEXT NOT NULL,
    terminal_status   TEXT NOT NULL DEFAULT '',
    status            TEXT NOT NULL DEFAULT 'pending',  -- pending / finalized / triggered
    seq               INTEGER DEFAULT 0,                -- 聚合重排事件 seq
    comment_posted    INTEGER NOT NULL DEFAULT 0,
    issue_status      TEXT DEFAULT '',                  -- 本次收口后组织 Issue 状态
    error             TEXT DEFAULT '',
    created_at        TEXT DEFAULT (datetime('now')),
    updated_at        TEXT DEFAULT (datetime('now')),
    UNIQUE (dispatch_id, terminal_status)
)
"""

# ===== 组织上下文快照（下派 L3 快照引用）=====
# 正文落平台快照目录，DB 只存 manifest（路径/哈希/大小），不存正文。
_ORG_CONTEXT_SNAPSHOT_DDL = """
CREATE TABLE IF NOT EXISTS grape_t_org_context_snapshot (
    id            TEXT PRIMARY KEY,           -- uuid（32 hex，Python 侧生成，禁自增）
    org_id        TEXT NOT NULL DEFAULT '',
    demand_id     TEXT NOT NULL DEFAULT '',      -- 组织需求 Issue 编号
    dispatch_id   TEXT NOT NULL DEFAULT '',      -- 下派批次
    storage_path  TEXT NOT NULL DEFAULT '',        -- 相对 WORKSPACE_DIR 的快照目录
    files_json    TEXT NOT NULL DEFAULT '{}',      -- manifest：{rel: {size,hash,source,storage}}
    content_hash  TEXT NOT NULL DEFAULT '',
    total_bytes   INTEGER NOT NULL DEFAULT 0,
    created_at    TEXT DEFAULT (datetime('now'))
)
"""


# ===== Issue #2150：组织上下文（brief/policy/decision）存储 =====
# 每 org 每 kind 仅 1 条 active：由发布事务内行锁降级旧 active 实现（MySQL 无部分唯一索引）。
# 正文直接落 content 列（上下文片段，非知识库正文，后者走 file_path/obs_key 索引）。
_ORG_CONTEXT_DDL = """
CREATE TABLE IF NOT EXISTS grape_t_org_context (
    id               TEXT PRIMARY KEY,           -- uuid（32 hex，Python 侧生成，禁自增）
    org_id           TEXT NOT NULL DEFAULT '',
    kind             TEXT NOT NULL DEFAULT 'brief',       -- brief / policy / decision
    ctx_key          TEXT NOT NULL DEFAULT 'default',     -- 同 kind 内条目身份（版本序列）
    title            TEXT NOT NULL DEFAULT '',
    content          TEXT NOT NULL DEFAULT '',
    lifecycle_status TEXT NOT NULL DEFAULT 'draft',       -- draft / active / deprecated
    version          INTEGER NOT NULL DEFAULT 1,          -- 同 (org,kind,ctx_key) 递增
    owner_id         TEXT NOT NULL DEFAULT '',
    valid_until      TEXT NOT NULL DEFAULT '',
    created_by       TEXT NOT NULL DEFAULT '',
    updated_by       TEXT NOT NULL DEFAULT '',
    created_at       TEXT DEFAULT (datetime('now')),
    updated_at       TEXT DEFAULT (datetime('now'))
)
"""

# ===== 组织仓库地图（Issue #2112 组织能力路由）=====
_ORG_REPO_MAP_DDL = """
CREATE TABLE IF NOT EXISTS grape_t_org_repo_map (
    id             TEXT PRIMARY KEY,           -- uuid（32 hex，Python 侧生成，禁自增）
    org_id         TEXT NOT NULL DEFAULT '',
    repo_key       TEXT NOT NULL DEFAULT '',
    full_name      TEXT DEFAULT '',
    is_org_repo    INTEGER DEFAULT 0,
    status         TEXT DEFAULT 'none',
    purpose        TEXT DEFAULT '',
    domains_json   TEXT DEFAULT '[]',
    keywords_json  TEXT DEFAULT '[]',
    interfaces_json TEXT DEFAULT '[]',
    depends_on_json TEXT DEFAULT '[]',
    not_owns_json  TEXT DEFAULT '[]',
    confidence     REAL DEFAULT 0,
    source_commit  TEXT DEFAULT '',
    summary_md     TEXT DEFAULT '',
    error          TEXT DEFAULT '',
    analyzed_at    TEXT DEFAULT '',
    updated_at     TEXT DEFAULT (datetime('now'))
)
"""

# ===== 组织仓库 AI 分析运行/结果台账（与确定性地图分表，避免互相清空）=====
_ORG_REPO_AI_DDL = """
CREATE TABLE IF NOT EXISTS grape_t_org_repo_ai (
    org_id         TEXT NOT NULL DEFAULT '',
    repo_key       TEXT NOT NULL DEFAULT '',
    status         TEXT NOT NULL DEFAULT '',     -- queued / running / succeeded / failed / skipped
    result_json    TEXT DEFAULT '',              -- 模型回传的原始结构化结果
    error          TEXT DEFAULT '',
    engine_id      INTEGER DEFAULT 0,
    source_commit  TEXT DEFAULT '',
    result_hash    TEXT DEFAULT '',
    triggered_by   TEXT DEFAULT '',              -- link / schedule / manual
    analyzed_at    TEXT DEFAULT '',
    created_at     TEXT DEFAULT (datetime('now')),
    updated_at     TEXT DEFAULT (datetime('now')),
    PRIMARY KEY (org_id, repo_key)
)
"""
