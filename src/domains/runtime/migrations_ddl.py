"""迁移 SQL：runtime 域归属表的 DDL/DML（由 db_setup 拆分而来）。

为什么在域模块里：`src/api/db_setup.py` 是 schema 引导器，却曾放着三域全部迁移 SQL；
静态扫描（scripts/foreign_table_refs.py）按文件路径判消费域，把这些 SQL 全算成
「api 消费外域表」。SQL 属表的归属域（方案 D9「每域自己的 schema」），放 owner 域
模块后扫描结果才反映真实依赖。

搬运工具：`scripts/split_migration_sql.py`（AST 定位字面量，原文搬迁）。
行为不变：`db_setup` 仍按原顺序执行同一批 SQL，只是字符串从本模块 import。
本模块只放**字符串常量**，不含执行逻辑——顺序由 db_setup 的编排保证。
"""

# ── 自 db_setup._migrate_data_to_personal_orgs() 下沉 ────────────────
SELECT_RUNTIME_CREDENTIALS = "SELECT id, owner_name, scope FROM runtime_credentials WHERE org_id='' OR org_id IS NULL"
UPDATE_RUNTIME_CREDENTIALS = "UPDATE runtime_credentials SET org_id=? WHERE id=?"
UPDATE_AGENT_RUNTIMES__MIGRATE_DATA_TO_PERSONAL_ORG = "UPDATE agent_runtimes SET org_id=? WHERE credential_id=?"
SELECT_SKILL__MIGRATE_DATA_TO_PERSONAL_ORG = "SELECT id, owner_id, scope FROM skill WHERE scope='user' AND (org_id='' OR org_id IS NULL)"
UPDATE_SKILL__MIGRATE_DATA_TO_PERSONAL_ORG = "UPDATE skill SET org_id=? WHERE id=?"
SELECT_SKILL__MIGRATE_DATA_TO_PERSONAL_ORG_2 = "SELECT id, owner_id FROM skill WHERE scope='repo' AND (org_id='' OR org_id IS NULL)"
UPDATE_SKILL__MIGRATE_DATA_TO_PERSONAL_ORG_2 = "UPDATE skill SET org_id=? WHERE id=?"

# ── 自 db_setup._copy_backend_to_runtime() 下沉 ──────────────────────
SELECT_AGENT_RUNTIMES = "SELECT id FROM agent_runtimes WHERE lower(name)=? AND scope=? AND owner_id=? LIMIT 1"
UPDATE_AGENT_RUNTIMES = """UPDATE agent_runtimes SET display_name=?, installed=?, cli_path=?, version=?, healthy=?, pip_package=?, updated_at=?
                         WHERE id=? AND (display_name='' OR installed=0 OR cli_path='' OR version='' OR healthy=0 OR pip_package='')"""
INSERT_IGNORE_AGENT_RUNTIMES = "INSERT OR IGNORE INTO agent_runtimes (id, name, display_name, installed, cli_path, version, healthy, pip_package, scope, owner_id, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)"

# ── 自 db_setup._create_indexes() 下沉 ───────────────────────────────
SELECT_ISSUE = (
    "SELECT repo_key, issue_number FROM issue WHERE issue_number>0 "
    "GROUP BY repo_key, issue_number HAVING COUNT(*)>1"
)
SELECT_ISSUE__CREATE_INDEXES = "SELECT id, status, updated_at FROM issue WHERE repo_key=? AND issue_number=? ORDER BY id"
DELETE_ISSUE = "DELETE FROM issue WHERE id=?"

# ── 自 db_setup._seed_from_json() 下沉 ───────────────────────────────
SELECT_META = "SELECT value FROM meta WHERE key='seeded_keys'"
UPSERT_META__SEED_FROM_JSON = "INSERT OR REPLACE INTO meta (key, value) VALUES ('seeded_keys', ?)"

# ── 自 db_setup._fn_seed_agent_types_from_config() 下沉 ──────────────
INSERT_IGNORE_AGENT_TYPE = (
    "INSERT OR IGNORE INTO agent_type "
    "(agent_type, display_name, description, default_runtime_id, icon, order_num, "
    " subscribe_events, enable) VALUES (?,?,?,0,?,?,?,?)"
)
SELECT_META__FN_SEED_AGENT_TYPES_FROM_CON = "SELECT value FROM meta WHERE key='seeded_keys'"
UPSERT_META__FN_SEED_AGENT_TYPES_FROM_CON = "INSERT OR REPLACE INTO meta (key, value) VALUES ('seeded_keys', ?)"

# ── 自 db_setup._insert_seed_data() 下沉 ─────────────────────────────
UPSERT_AGENT_TYPE = "INSERT OR REPLACE INTO agent_type (agent_type, display_name, description, default_runtime_id, icon, order_num, subscribe_events, enable) VALUES (?,?,?,0,?,?,?,?)"

# ── 自 db_setup._init_scene_category_data() 下沉 ─────────────────────
INSERT_IGNORE_SCENE_CATEGORY = "INSERT OR IGNORE INTO scene_category (name, slug, icon, order_num, is_active) VALUES (?,?,?,?,1)"

# ── 自 db_setup._init_scene_skill_ref() 下沉 ─────────────────────────
SELECT_SKILL = "SELECT id FROM skill WHERE skill_name=? AND is_active=1 LIMIT 1"
SELECT_SKILL__INIT_SCENE_SKILL_REF = "SELECT id FROM skill WHERE skill_name=? AND is_active=1 LIMIT 1"

# ── 自 db_setup._init_skill_featured() 下沉 ──────────────────────────
UPDATE_SKILL = "UPDATE skill SET is_featured=1 WHERE skill_name=? AND scope='official'"

# ── 自 db_setup._fn_github_bot_username_to_card() 下沉 ───────────────
SELECT_META__FN_GITHUB_BOT_USERNAME_TO_CA = "SELECT value FROM meta WHERE key='vcs_providers_config'"
UPSERT_META = "INSERT OR REPLACE INTO meta (key, value) VALUES ('vcs_providers_config', ?)"

# ── 自 db_setup._fn_create_credential_usage_log_table() 下沉 ─────────
ALTER_GRAPE_T_CREDENTIAL_USAGE_LOG = "ALTER TABLE grape_t_credential_usage_log ADD COLUMN repo_key TEXT DEFAULT ''"

# ── 自 db_setup._fn_issue_state_scene_user_id_text_normalize() 下沉 ──
ALTER_ISSUE_STATE = (
    "ALTER TABLE issue_state MODIFY scene_user_id "
    "VARCHAR(36) NOT NULL DEFAULT ''"
)
UPDATE_ISSUE_STATE = "UPDATE issue_state SET scene_user_id='' WHERE scene_user_id='0'"

# ── 自 db_setup._fn_add_agent_runtimes_role_tags() 下沉 ──────────────
ALTER_AGENT_RUNTIMES = "ALTER TABLE agent_runtimes ADD COLUMN role_tags TEXT DEFAULT ''"
ALTER_AGENT_RUNTIMES__FN_ADD_AGENT_RUNTIMES_ROLE_T = "ALTER TABLE agent_runtimes ADD COLUMN role_tags TEXT DEFAULT ''"

# ── 自 db_setup._get_meta_value_conn() 下沉 ──────────────────────────
SELECT_META__GET_META_VALUE_CONN = "SELECT value FROM meta WHERE key=?"

# ── 自 db_setup._set_meta_value_conn() 下沉 ──────────────────────────
UPSERT_META__SET_META_VALUE_CONN = "INSERT OR REPLACE INTO meta (key, value) VALUES (?,?)"

# ── 自 db_setup._fn_normalize_task_messages_created_at_default() 下沉 
ALTER_TASK_MESSAGES = (
    "ALTER TABLE task_messages ALTER COLUMN created_at SET DEFAULT "
    "(CONCAT(REPLACE(CAST(UTC_TIMESTAMP(6) AS CHAR), ' ', 'T'), '+00:00'))"
)

# ── 自 db_setup._migrate_release_subissue_unique_index() 下沉 ────────
ALTER_ISSUE = (
    "ALTER TABLE issue ADD COLUMN release_bind_env VARCHAR(64) "
    "AS (NULLIF(release_target_env, '')) STORED"
)

# ── 自 db_setup._migrate_event_status_pending() 下沉 ─────────────────
UPDATE_EVENTS = (
    "UPDATE events SET status='pending' "
    "WHERE status='running' AND EXISTS ("
    "  SELECT 1 FROM agent_tasks t WHERE t.event_seq = events.seq AND t.status='queued')"
)
UPDATE_EVENTS__MIGRATE_EVENT_STATUS_PENDING = (
    "UPDATE events SET status='pending' "
    "WHERE status='running' AND (started_at IS NULL OR started_at='') "
    "AND NOT EXISTS (SELECT 1 FROM agent_tasks t WHERE t.event_seq = events.seq)"
)

# ── 自 db_setup._cleanup_dup_chat_config_rows() 下沉 ─────────────────
DELETE_CHAT_AGENT_SESSION = (
    "DELETE FROM chat_agent_session "
    "WHERE repo_key='' AND id NOT IN ("
    "  SELECT MIN(id) FROM chat_agent_session WHERE repo_key='' GROUP BY user_id"
    ")"
)

# ── 自 db_setup._migrate_knowledge_dedup_and_ref() 下沉 ──────────────
SELECT_META__MIGRATE_KNOWLEDGE_DEDUP_AND = "SELECT value FROM meta WHERE key='issue95_migrate_done'"
UPSERT_META__MIGRATE_KNOWLEDGE_DEDUP_AND = "INSERT OR REPLACE INTO meta (key, value) VALUES ('issue95_migrate_done', '1')"

# ── 自 db_setup._cleanup_stale_meta() 下沉 ───────────────────────────
DELETE_META = "DELETE FROM meta WHERE key='ws_feishu_app_id'"

# ── 自 db_setup._cleanup_chat_shared_worker_runtime() 下沉 ───────────
SELECT_CHAT_AGENT_SESSION__CLEANUP_CHAT_SHARED_WORKER_R = """
                SELECT c.id, c.runtime_id, ar.name
                FROM chat_agent_session c JOIN agent_runtimes ar ON ar.id = c.runtime_id
                WHERE c.repo_key='' AND c.runtime_id>0 AND ar.scope='global' AND ar.credential_id>0
            """
SELECT_AGENT_RUNTIMES__CLEANUP_CHAT_SHARED_WORKER_R = "SELECT id FROM agent_runtimes WHERE lower(name)=? AND credential_id=0 AND installed=1 ORDER BY id LIMIT 1"
UPDATE_CHAT_AGENT_SESSION = "UPDATE chat_agent_session SET runtime_id=? WHERE id=?"

# ── 自 db_setup._align_agent_enable() 下沉 ───────────────────────────
SELECT_META__ALIGN_AGENT_ENABLE = "SELECT COUNT(*) FROM meta WHERE key='enable_aligned'"
UPDATE_DEVOPS_AGENT_SESSION = "UPDATE devops_agent_session SET enable = COALESCE((SELECT enable FROM agent_type WHERE agent_type = devops_agent_session.agent_type), 1)"
UPSERT_META__ALIGN_AGENT_ENABLE = "INSERT OR REPLACE INTO meta (key, value) VALUES ('enable_aligned', '1')"
SQL_AGENT_TYPE = "Aligned existing agent session enable from agent_type defaults"

# ── 自 db_setup._drop_chat_session_repo_unique() 下沉 ────────────────
DROP_CHAT_AGENT_SESSION = "DROP TABLE chat_agent_session"

# ── 自 db_setup._rebuild_agent_runtimes_inst_key() 下沉 ──────────────
DELETE_AGENT_RUNTIMES = """
        DELETE FROM agent_runtimes
        WHERE credential_id=0 AND id NOT IN (
            SELECT MIN(id) FROM agent_runtimes WHERE credential_id=0 GROUP BY lower(name), scope, owner_id
        )
    """
DROP_AGENT_RUNTIMES = "DROP TABLE agent_runtimes"

# ── 自 db_setup._alter_skill_files_if_needed() 下沉 ──────────────────
ALTER_AGENT_TASKS = "ALTER TABLE agent_tasks MODIFY skill_files MEDIUMTEXT"
ALTER_AGENT_TASKS__ALTER_SKILL_FILES_IF_NEEDED = "ALTER TABLE agent_tasks MODIFY skill_files MEDIUMTEXT"

# ── 自 db_setup._tasks_to_issue_rename_pre_create() 下沉 ─────────────
SELECT_ISSUE__TASKS_TO_ISSUE_RENAME_PRE_CR = "SELECT COUNT(*) FROM issue"
DROP_ISSUE = "DROP TABLE issue"

# ── 自 db_setup._migrate_tables() 下沉 ───────────────────────────────
INSERT_IGNORE_META = "INSERT OR IGNORE INTO meta (key, value) VALUES ('m0_backup_done', '1')"
ALTERS_SESSION_EVENT_COLUMNS = (
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
    "ALTER TABLE devops_agent_session ADD COLUMN backend_session_id TEXT DEFAULT ''",
    "ALTER TABLE chat_conversations ADD COLUMN agent_session_id TEXT DEFAULT ''",
    "ALTER TABLE chat_conversations ADD COLUMN backend_session_id TEXT DEFAULT ''",
    "ALTER TABLE devops_agent_session ADD COLUMN backend_id INTEGER DEFAULT 0",
    # R16.5.3: devops_agent_session 新增 token_status + repo_github_token 列
    "ALTER TABLE devops_agent_session ADD COLUMN token_status TEXT DEFAULT ''",
    "ALTER TABLE devops_agent_session ADD COLUMN repo_github_token TEXT DEFAULT ''",
    "ALTER TABLE agent_type ADD COLUMN default_backend_id INTEGER DEFAULT 0",
    "ALTER TABLE chat_agent_session ADD COLUMN backend_id INTEGER DEFAULT 0",
    "ALTER TABLE events ADD COLUMN result TEXT DEFAULT ''",
    "ALTER TABLE events ADD COLUMN scene_id TEXT DEFAULT ''",
    "ALTER TABLE events ADD COLUMN entity_type TEXT DEFAULT ''",
    # Issue #1938：级联收敛补偿归因列（cascade.py 读侧依赖；老库补列幂等）
    "ALTER TABLE events ADD COLUMN cascade_reason TEXT DEFAULT ''",
    "ALTER TABLE events ADD COLUMN report_missing INTEGER DEFAULT 0",
    "ALTER TABLE events ADD COLUMN triggered_by TEXT DEFAULT ''",
    "ALTER TABLE events ADD COLUMN cascade_task_status TEXT DEFAULT ''",
    "ALTER TABLE events ADD COLUMN cascade_prev_event_status TEXT DEFAULT ''",
    "ALTER TABLE issue_state ADD COLUMN assignee TEXT DEFAULT ''",
    # ===== 发布配置(skill-publish v1.1)=====
    # ===== OBS 技能库(obs-skill-storage-design.md §4)=====
    # v2.4 去版本化:DROP current_version(本期新增列,无历史依赖;列不存在时 SQLite 忽略报错)
    "ALTER TABLE skill DROP COLUMN current_version",
    # v2.5:file_path(真实存储路径 OBS 对象键,VARCHAR(512))+ created_by/updated_by(审计/删除权限,VARCHAR(64))
    "ALTER TABLE skill ADD COLUMN file_path VARCHAR(512) DEFAULT ''",
    "ALTER TABLE skill ADD COLUMN created_by VARCHAR(64) DEFAULT ''",
    "ALTER TABLE skill ADD COLUMN updated_by VARCHAR(64) DEFAULT ''",
)
ALTERS_ENGINE_CREDENTIAL_COLUMNS = (
    # Worker 三级分类——repo_key 字段（scope=user 时关联项目）
    "ALTER TABLE runtime_credentials ADD COLUMN repo_key VARCHAR(512) DEFAULT NULL",
    # ===== TMP (/tmp) 使用率指标（liufei 2026-08-28）=====
    "ALTER TABLE runtime_credentials ADD COLUMN tmp_percent DOUBLE DEFAULT 0",
    # ===== Issue #180: 带宽占用率指标 =====
    "ALTER TABLE runtime_credentials ADD COLUMN bandwidth_percent DOUBLE DEFAULT 0",
    "ALTER TABLE runtime_credentials ADD COLUMN bandwidth_mbps DOUBLE DEFAULT 0",
    # ⚠️ Issue #3773 的 4 个 `acp_state_db_*` / `acp_resume_skipped_total` 列**不在这里**：
    # 本常量只被**冻结老轨** `db_setup._migrate_tables()` 消费，存量库（已盖
    # legacy-stamp 202610300001）整段不执行 → 新增列写在这里线上表现为心跳 UPDATE
    # 「Unknown column 1054」，心跳与 ACP 指标全断（部署验证实测）。这 4 列改走注册式
    # 迁移 202611240006（src/domains/runtime/migrations/acp_state_db_metrics_columns.py）。
    # 新增列一律用 register_migration()，不要续写本常量。
    "ALTER TABLE agent_runtimes ADD COLUMN repo_key VARCHAR(512) DEFAULT NULL",
)
ALTERS_SKILL_ENGINE_COLUMNS = (
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
    # ===== Issue #658: 引擎角色标签（显式声明；空=按引擎类型默认角色映射兜底）=====
    "ALTER TABLE agent_runtimes ADD COLUMN role_tags TEXT DEFAULT ''",
    # ===== Issue #865: 配置模型（worker 注册上报；legacy 盖帽库需手工执行同款 ALTER）=====
    "ALTER TABLE agent_runtimes ADD COLUMN model_name TEXT DEFAULT ''",
    "ALTER TABLE agent_runtimes ADD COLUMN model_source TEXT DEFAULT ''",
    "ALTER TABLE agent_runtimes ADD COLUMN model_reported_at TEXT DEFAULT ''",
    # ===== Issue #498: 健康统计自愈留痕（失败衰减窗口有效失败数 + 最近失败时间）=====
    "ALTER TABLE runtime_health_scores ADD COLUMN failed_tasks_effective INTEGER DEFAULT 0",
    "ALTER TABLE runtime_health_scores ADD COLUMN last_failure_at TEXT DEFAULT ''",
    # ===== Issue-11: skill 表加 is_featured + like_count =====
    "ALTER TABLE skill ADD COLUMN is_featured INTEGER DEFAULT 0",
    "ALTER TABLE skill ADD COLUMN like_count INTEGER DEFAULT 0",
    "ALTER TABLE skill ADD COLUMN sub_scene TEXT DEFAULT ''",
)
ALTERS_SCENE_CATEGORY_EVENT_SKIPPED = (
    # ===== Issue-11: scene_category 加 slug（场景包 category 用 slug 引用） =====
    "ALTER TABLE scene_category ADD COLUMN slug VARCHAR(64) DEFAULT ''",
    # ===== Issue-89: event_skipped 加 issue_id 列（skipped 事件审计需关联 Issue） =====
    "ALTER TABLE event_skipped ADD COLUMN issue_id TEXT DEFAULT ''",
)
UPDATE_SCENE_CATEGORY = "UPDATE scene_category SET slug=? WHERE name=? AND (slug='' OR slug IS NULL)"
UPSERT_CHAT_AGENT_SESSION = "INSERT OR REPLACE INTO chat_agent_session (id, user_id, repo_key, backend, skill, created_at, last_active_at) VALUES (?,?,?,?,?,datetime('now'),datetime('now'))"
SELECT_DEVOPS_AGENT_SESSION = "SELECT DISTINCT backend FROM devops_agent_session WHERE backend!='' AND backend IS NOT NULL"
UPDATE_DEVOPS_AGENT_SESSION__MIGRATE_TABLES_3 = "UPDATE devops_agent_session SET backend_id=? WHERE backend=?"
UPDATE_DEVOPS_AGENT_SESSION__MIGRATE_TABLES = "UPDATE devops_agent_session SET backend_id=? WHERE (backend='' OR backend IS NULL) AND backend_id=0"
SELECT_AGENT_TYPE = "SELECT DISTINCT default_backend FROM agent_type WHERE default_backend!='' AND default_backend IS NOT NULL"
UPDATE_AGENT_TYPE__MIGRATE_TABLES = "UPDATE agent_type SET default_backend_id=? WHERE default_backend=?"
UPDATE_AGENT_TYPE = "UPDATE agent_type SET default_backend_id=? WHERE (default_backend='' OR default_backend IS NULL) AND default_backend_id=0"
SELECT_CHAT_AGENT_SESSION = "SELECT DISTINCT backend FROM chat_agent_session WHERE backend!='' AND backend IS NOT NULL"
UPDATE_CHAT_AGENT_SESSION__MIGRATE_TABLES_2 = "UPDATE chat_agent_session SET backend_id=? WHERE backend=?"
UPDATE_CHAT_AGENT_SESSION__MIGRATE_TABLES = "UPDATE chat_agent_session SET backend_id=? WHERE (backend='' OR backend IS NULL) AND backend_id=0"
UPDATE_REPO_IM_GROUP_BINDING = "UPDATE repo_im_group_binding SET im_app_id=account WHERE (im_app_id IS NULL OR im_app_id='') AND account IS NOT NULL AND account!=''"
ALTER_REPO_IM_GROUP_BINDING = "ALTER TABLE repo_im_group_binding DROP COLUMN account"
ALTER_REPO_IM_GROUP_BINDING__MIGRATE_TABLES = "ALTER TABLE repo_im_group_binding RENAME COLUMN chat_id TO im_chat_id"
SELECT_META__MIGRATE_TABLES = "SELECT COUNT(*) FROM meta WHERE key=?"
UPSERT_META__MIGRATE_TABLES = "INSERT OR REPLACE INTO meta (key, value) VALUES (?,?)"
INSERT_IGNORE_DEVOPS_AGENT_SESSION = "INSERT OR IGNORE INTO devops_agent_session (id, repo_key, agent_type, backend, skill, oc_session_id, repo_gitcode_token, status, created_at, last_active_at) SELECT id, repo_key, agent_type, backend, skill, oc_session_id, repo_gitcode_token, status, created_at, last_active_at FROM agent_sessions"
SQL_DEVOPS_AGENT_SESSION = "Migrated agent_sessions data into devops_agent_session and dropped agent_sessions"
INSERT_IGNORE_AGENT_TYPE__MIGRATE_TABLES = "INSERT OR IGNORE INTO agent_type (agent_type, display_name, description, default_backend, icon, order_num, subscribe_events) VALUES (?,?,?,?,?,?,?)"
INSERT_IGNORE_SKILL = "INSERT OR IGNORE INTO skill (skill_name, display_name, description, agent_type, skill_path, scope, owner_id) VALUES (?,?,?,?,?,'official','')"
INSERT_IGNORE_SKILL__MIGRATE_TABLES = "INSERT OR IGNORE INTO skill (skill_name, display_name, description, agent_type, skill_path, scope, owner_id) VALUES (?,?,?,?,?,'user',?)"
UPDATE_DEVOPS_AGENT_SESSION__MIGRATE_TABLES_2 = "UPDATE devops_agent_session SET backend_session_id=oc_session_id WHERE backend_session_id='' AND oc_session_id!=''"
ALTER_VOD_CONVERSATIONS = "ALTER TABLE vod_conversations RENAME COLUMN feishu_chat_id TO im_chat_id"
ALTER_VOD_CONVERSATIONS__MIGRATE_TABLES = "ALTER TABLE vod_conversations RENAME COLUMN feishu_chat_name TO im_channel_name"
ALTER_VOD_CONVERSATIONS__MIGRATE_TABLES_2 = "ALTER TABLE vod_conversations RENAME COLUMN oc_session_id TO backend_session_id"
ALTER_VOD_CONVERSATIONS__MIGRATE_TABLES_3 = "ALTER TABLE vod_conversations ADD COLUMN im_provider TEXT DEFAULT 'feishu'"
UPDATE_CHAT_CONVERSATIONS = "UPDATE chat_conversations SET backend_session_id=session_id WHERE backend_session_id='' AND session_id!=''"
SELECT_SKILL__MIGRATE_TABLES = "SELECT id, skill_name FROM skill WHERE scope='official'"
DELETE_RUNTIME_CREDENTIALS = """
                    DELETE FROM runtime_credentials WHERE id NOT IN (
                        SELECT MIN(id) FROM runtime_credentials GROUP BY name
                    )
                """
UPDATE_ISSUE = (
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
    "              WHERE s.repo_key=issue.repo_key AND s.issue_id='#'||issue.issue_number)"
)
SELECT_ISSUE_STATE = (
    "SELECT COUNT(*) FROM issue_state s WHERE NOT EXISTS ("
    "SELECT 1 FROM issue i WHERE i.repo_key=s.repo_key "
    "AND i.issue_number=CAST(REPLACE(s.issue_id,'#','') AS INTEGER))"
)
SELECT_ISSUE_STATE__MIGRATE_TABLES = (
    "SELECT COUNT(*) FROM issue_state s JOIN issue i "
    "ON i.repo_key=s.repo_key AND i.issue_number=CAST(REPLACE(s.issue_id,'#','') AS INTEGER) "
    "WHERE COALESCE(i.stage,'')<>COALESCE(s.stage,'')"
)
DROP_ISSUE_STATE = "DROP TABLE issue_state"

# ── 自 db_setup._backfill_scene_display_names() 下沉 ─────────────────
SELECT_META__BACKFILL_SCENE_DISPLAY_NAMES = "SELECT value FROM meta WHERE key='issue343_jwt_invalidated'"
UPSERT_META__BACKFILL_SCENE_DISPLAY_NAMES = "INSERT OR REPLACE INTO meta (key, value) VALUES ('issue343_jwt_invalidated', '1')"
SELECT_META__BACKFILL_SCENE_DISPLAY_NAMES_2 = "SELECT value FROM meta WHERE key='issue344_tokens_cleared'"
UPDATE_CHAT_CONVERSATIONS__BACKFILL_SCENE_DISPLAY_NAMES = "UPDATE chat_conversations SET user_gitcode_token = '' WHERE user_gitcode_token != ''"
UPDATE_AGENT_TASKS = "UPDATE agent_tasks SET user_gitcode_token = '' WHERE user_gitcode_token != ''"
UPSERT_META__BACKFILL_SCENE_DISPLAY_NAMES_2 = "INSERT OR REPLACE INTO meta (key, value) VALUES ('issue344_tokens_cleared', '1')"
SQL_CHAT_CONVERSATIONS = "Issue #344: cleared cached plaintext tokens from chat_conversations and agent_tasks"


# ── 自 db_setup 内联 DDL/DML 下沉（动态列名/表名一律「函数返回」，不 import 常量属性）──
#
# 这些语句原本内联在 `src/api/db_setup.py` 的迁移函数里，用 f-string 拼列名/占位符，
# 静态扫描按文件路径把它们算成 api 消费 runtime 表。搬到归属域后 SQL 与表同处一个模块，
# 执行顺序仍由 db_setup 的迁移编排保证（本模块不含执行逻辑）。

def agent_runtimes_add_column(col: str) -> str:
    """agent_runtimes 补列（Issue #865 模型列；幂等由调用方的列探测保证）。"""
    return f"ALTER TABLE agent_runtimes ADD COLUMN {col} TEXT DEFAULT ''"


def takeover_sessions_add_column(col: str, typ: str) -> str:
    """takeover_sessions 补列（列名/类型由调用方探测后传入）。"""
    return f"ALTER TABLE takeover_sessions ADD COLUMN {col} {typ}"


def events_pending_terminal_select(ph: str, batch: int) -> str:
    """待补 completed_at 的终态事件（分页拉 seq；ph 为方言占位符）。"""
    return (f"SELECT seq FROM events WHERE status IN ({ph}) "
            "AND (completed_at IS NULL OR completed_at='') "
            f"ORDER BY seq LIMIT {int(batch)}")


def events_backfill_completed_at(seq_ph: str, status_ph: str, now_fn: str) -> str:
    """按 created_at（缺失则当前时间）回填终态事件的 completed_at（Issue #1497）。"""
    return (f"UPDATE events SET completed_at=COALESCE(NULLIF(created_at,''), {now_fn}) "
            f"WHERE seq IN ({seq_ph}) AND status IN ({status_ph}) "
            "AND (completed_at IS NULL OR completed_at='')")


def events_backfill_completed_at_valued(seq_ph: str, status_ph: str, val_expr: str) -> str:
    """同上，但 completed_at 取调用方给定的值表达式（含 updated_at 回退链）。"""
    return (f"UPDATE events SET completed_at={val_expr} "
            f"WHERE seq IN ({seq_ph}) AND status IN ({status_ph}) "
            "AND (completed_at IS NULL OR completed_at='')")


AGENT_RUNTIMES_HEALTH_ROWS = (
    "SELECT id, status, last_seen_at, health, credential_id, installed, healthy "
    "FROM agent_runtimes WHERE health IS NOT NULL AND health != ''")


def agent_runtimes_set_health(ph: str) -> str:
    """遗留 health 列回填（值级幂等：健康值未变则 0 行）。"""
    return f"UPDATE agent_runtimes SET health={ph} WHERE id={ph} AND health={ph}"


def chat_agent_session_new_insert_select(cols: list[str]) -> str:
    """chat_agent_session 重建表的行搬迁（保留新旧表共有列）。"""
    _c = ",".join(cols)
    return f"INSERT INTO chat_agent_session_new ({_c}) SELECT {_c} FROM chat_agent_session"


def agent_runtimes_new_insert_select(cols: list[str]) -> str:
    """agent_runtimes 重建表的行搬迁（只带新旧表共有列）。"""
    _c = ",".join(cols)
    return f"INSERT INTO agent_runtimes_new ({_c}) SELECT {_c} FROM agent_runtimes"


# ── org uuid-pk（PR #1820 A12）：issue.org_context_snapshot_id 哨兵回填 ──────
# 该列属 runtime 域（configs/table_ownership.json: issue→runtime）；SQL 常量下沉到
# runtime 侧，避免 api 域迁移文件出现对 runtime 表的跨域数据引用（P0-2 棘轮）。
UPDATE_ISSUE_ORG_CONTEXT_SNAPSHOT_ID_SENTINEL = (
    "UPDATE issue SET org_context_snapshot_id='' "
    "WHERE org_context_snapshot_id IN (0, '0')")
