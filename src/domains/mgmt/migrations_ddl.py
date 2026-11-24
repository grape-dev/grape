"""迁移 SQL：mgmt 域归属表的 DDL/DML（由 db_setup 拆分而来）。

为什么在域模块里：`src/api/db_setup.py` 是 schema 引导器，却曾放着三域全部迁移 SQL；
静态扫描（scripts/foreign_table_refs.py）按文件路径判消费域，把这些 SQL 全算成
「api 消费外域表」。SQL 属表的归属域（方案 D9「每域自己的 schema」），放 owner 域
模块后扫描结果才反映真实依赖。

搬运工具：`scripts/split_migration_sql.py`（AST 定位字面量，原文搬迁）。
行为不变：`db_setup` 仍按原顺序执行同一批 SQL，只是字符串从本模块 import。
本模块只放**字符串常量**，不含执行逻辑——顺序由 db_setup 的编排保证。
"""

# ── 自 db_setup._migrate_create_personal_orgs() 下沉 ─────────────────
SELECT_ORGANIZATION = "SELECT id FROM organization WHERE owner_id=? AND is_personal=1"
INSERT_ORGANIZATION = "INSERT INTO organization (name, slug, owner_id, is_personal) VALUES (?, ?, ?, 1)"
INSERT_IGNORE_ORG_MEMBER = "INSERT OR IGNORE INTO org_member (org_id, user_id, role) VALUES (?, ?, 'owner')"

# ── 自 db_setup._migrate_data_to_personal_orgs() 下沉 ────────────────
SELECT_MANAGED_REPOS = "SELECT repo_key FROM managed_repos WHERE org_id='' OR org_id IS NULL"
SELECT_USER_REPOS__MIGRATE_DATA_TO_PERSONAL_ORG = "SELECT user_id FROM user_repos WHERE repo_key=? AND is_admin=1 LIMIT 1"
SELECT_ORGANIZATION__MIGRATE_DATA_TO_PERSONAL_ORG_2 = "SELECT id FROM organization WHERE owner_id=? AND is_personal=1"
UPDATE_MANAGED_REPOS__MIGRATE_DATA_TO_PERSONAL_ORG = "UPDATE managed_repos SET org_id=? WHERE repo_key=?"
SELECT_ORGANIZATION__MIGRATE_DATA_TO_PERSONAL_ORG_3 = "SELECT id FROM organization WHERE owner_id=? AND is_personal=1"
SELECT_ORGANIZATION__MIGRATE_DATA_TO_PERSONAL_ORG = "SELECT id FROM organization WHERE owner_id=? AND is_personal=1"
SELECT_MANAGED_REPOS__MIGRATE_DATA_TO_PERSONAL_ORG = "SELECT org_id FROM managed_repos WHERE repo_key=?"

# ── 自 db_setup._create_tables() 下沉 ────────────────────────────────
ALTER_GRAPE_ACCOUNTS = "ALTER TABLE grape_accounts MODIFY COLUMN email VARCHAR(255) DEFAULT NULL"

# ── 自 db_setup._seed_event_action_filter() 下沉 ─────────────────────
INSERT_IGNORE_EVENT_ACTION_FILTER = (
    "INSERT OR IGNORE INTO event_action_filter "
    "(event_type, action, allow, actor, require_changes, block_changes, handle_side_effect, priority, enabled, created_at, updated_at) "
    "VALUES (?,?,?,?,?,?,?,0,1,datetime('now'),datetime('now'))"
)

# ── 自 db_setup._fn_seed_event_action_filter_once() 下沉 ─────────────
SELECT_EVENT_ACTION_FILTER__FN_SEED_EVENT_ACTION_FILTER = "SELECT COUNT(*) FROM event_action_filter"
SELECT_EVENT_ACTION_FILTER = "SELECT COUNT(*) FROM event_action_filter"

# ── 自 db_setup._insert_seed_data() 下沉 ─────────────────────────────
INSERT_IGNORE_EVENT_ROUTE_CONFIG = "INSERT OR IGNORE INTO event_route_config (repo_key, event_type, skill) VALUES (?,?,?)"
INSERT_IGNORE_EVENT_ROUTE_CONFIG__INSERT_SEED_DATA = "INSERT OR IGNORE INTO event_route_config (repo_key, event_type, skill) VALUES (?,?,?)"

# ── 自 db_setup._migrate_whitelist_from_file() 下沉 ──────────────────
SELECT_LOGIN_WHITELIST = "SELECT COUNT(*) FROM login_whitelist"
INSERT_IGNORE_LOGIN_WHITELIST = "INSERT OR IGNORE INTO login_whitelist (provider, username, remark) VALUES (?, ?, ?)"

# ── 自 db_setup._fn_login_whitelist_row_columns() 下沉 ───────────────
ALTER_LOGIN_WHITELIST = (
    "ALTER TABLE login_whitelist ADD COLUMN username_lower VARCHAR(128) "
    "AS (LOWER(NULLIF(username, ''))) STORED"
)
ALTER_LOGIN_WHITELIST__FN_LOGIN_WHITELIST_ROW_COLUM = (
    "ALTER TABLE login_whitelist ADD UNIQUE INDEX uq_login_whitelist_row "
    "(provider, username_lower)"
)

# ── 自 db_setup._init_skill_category_data() 下沉 ─────────────────────
INSERT_IGNORE_SKILL_CATEGORY = "INSERT OR IGNORE INTO skill_category (name, slug, icon, order_num, is_active) VALUES (?,?,?,?,1)"


# ── 自 db_setup._init_scene_market_data() 下沉 ───────────────────────
SELECT_SCENE_MARKET__INIT_SCENE_MARKET_DATA_2 = "SELECT COUNT(*) FROM scene_market WHERE source='official'"
SELECT_SCENE_MARKET__INIT_SCENE_MARKET_DATA = "SELECT DISTINCT name, display_name, description, nodes_json, onboarding FROM scene_market WHERE source='official' AND is_active=1"
SELECT_DEVOPS_PIPELINE_SCENE__INIT_SCENE_MARKET_DATA = "SELECT scene_id, name, description, nodes_json, onboarding FROM devops_pipeline_scene WHERE scope='official'"
INSERT_IGNORE_SCENE_MARKET = (
    "INSERT OR IGNORE INTO scene_market "
    "(name, display_name, version, description, market_description, icon, category_id, "
    "is_featured, like_count, source, source_user_id, recommended_template, nodes_json, onboarding, is_active) "
    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,1)"
)

# ── 自 db_setup._init_scene_skill_ref() 下沉 ─────────────────────────
SELECT_SCENE_SKILL_REF = "SELECT id, skill_name FROM scene_skill_ref WHERE skill_id=0"
UPDATE_SCENE_SKILL_REF = "UPDATE scene_skill_ref SET skill_id=? WHERE id=?"
SELECT_SCENE_MARKET = "SELECT id, nodes_json FROM scene_market WHERE is_active=1"
INSERT_IGNORE_SCENE_SKILL_REF = "INSERT OR IGNORE INTO scene_skill_ref (scene_id, skill_id, skill_name) VALUES (?,?,?)"

# ── 自 db_setup._seed_admin_accounts() 下沉 ──────────────────────────
SELECT_ADMIN_ACCOUNTS = "SELECT COUNT(*) FROM admin_accounts"
INSERT_ADMIN_ACCOUNTS = "INSERT INTO admin_accounts (username, password_hash, is_super, token_version) VALUES (?,?,1,0)"

# ── 自 db_setup._rebuild_organization_text_pk() 下沉 ─────────────────
ALTER_ORGANIZATION = "ALTER TABLE organization RENAME TO "

# ── 自 db_setup._fn_rebuild_grape_t_patrol_findings_unique() 下沉 ────
ALTER_GRAPE_T_PATROL_FINDINGS = "ALTER TABLE grape_t_patrol_findings RENAME TO grape_t_patrol_findings_old"
INSERT_GRAPE_T_PATROL_FINDINGS = "INSERT INTO grape_t_patrol_findings SELECT * FROM grape_t_patrol_findings_old"

# ── 自 db_setup._fn_scene_nodes_skill_id_backfill() 下沉 ─────────────
SELECT_DEVOPS_PIPELINE_SCENE = (
    "SELECT id, repo_key, nodes_json, created_by "
    "FROM devops_pipeline_scene WHERE is_active=1"
)
SELECT_SCENE_MARKET__FN_SCENE_NODES_SKILL_ID_BACK = "SELECT id, repo_key, source_user_id, nodes_json FROM scene_market"
SELECT_UNIFIED_USER_MAPPING = (
    "SELECT user_id FROM unified_user_mapping "
    "WHERE gitcode_user=? AND user_id IS NOT NULL AND user_id NOT IN ('', '0') LIMIT 1"
)
UPDATE_DEVOPS_PIPELINE_SCENE = "UPDATE devops_pipeline_scene SET nodes_json=? WHERE id=?"
UPDATE_SCENE_MARKET = "UPDATE scene_market SET nodes_json=? WHERE id=?"

# ── 自 db_setup._run_migrations_with_conn() 下沉 ─────────────────────
SELECT_GRAPE_T_SCHEMA_MIGRATIONS = "SELECT version, name, checksum FROM grape_t_schema_migrations"
UPSERT_GRAPE_T_SCHEMA_MIGRATIONS = (
    "INSERT OR REPLACE INTO grape_t_schema_migrations "
    "(version, name, kind, applied_at, duration_ms, checksum) "
    "VALUES (?,?,?,?,?,?)"
)
UPSERT_GRAPE_T_SCHEMA_MIGRATIONS__RUN_MIGRATIONS_WITH_CONN = (
    "INSERT OR REPLACE INTO grape_t_schema_migrations "
    "(version, name, kind, applied_at, duration_ms, checksum) "
    "VALUES (?,?,?,?,?,?)"
)

# ── 自 db_setup._migrate_allow_signup_default() 下沉 ─────────────────
SELECT_GRAPE_ACCOUNTS = "SELECT COUNT(*) FROM grape_accounts"

# ── 自 db_setup._migrate_knowledge_dedup_and_ref() 下沉 ──────────────
UPDATE_AGENT_SHARED_KNOWLEDGE = (
    "UPDATE agent_shared_knowledge SET source = 'system' "
    "WHERE scope = 'baseline' AND (source IS NULL OR source = '')"
)
UPDATE_AGENT_SHARED_KNOWLEDGE__MIGRATE_KNOWLEDGE_DEDUP_AND = (
    "UPDATE agent_shared_knowledge SET source = 'agent' "
    "WHERE scope IN ('project', 'issue', 'issue_agent') AND (source IS NULL OR source = '')"
)
UPDATE_AGENT_SHARED_KNOWLEDGE__MIGRATE_KNOWLEDGE_DEDUP_AND_2 = (
    "UPDATE agent_shared_knowledge SET source = 'manual' "
    "WHERE scope = 'member' AND (source IS NULL OR source = '')"
)
SELECT_AGENT_SHARED_KNOWLEDGE = (
    "SELECT id, scope, source, agent_type, issue_num, name, file_path, obs_key, description "
    "FROM agent_shared_knowledge "
    "WHERE name IS NULL OR name = '' OR name = ? "
    "OR description = ? OR description IS NULL OR description = ''"
)
UPDATE_AGENT_SHARED_KNOWLEDGE__MIGRATE_KNOWLEDGE_DEDUP_AND_3 = "UPDATE agent_shared_knowledge SET name=?, description=? WHERE id=?"
DELETE_SCENE_KNOWLEDGE_REF = (
    "DELETE FROM scene_knowledge_ref WHERE knowledge_id IN ("
    "SELECT id FROM agent_shared_knowledge WHERE scope='baseline' "
    "AND id NOT IN (SELECT keep.keep_id FROM ("
    "SELECT MAX(id) AS keep_id FROM agent_shared_knowledge "
    "WHERE scope='baseline' GROUP BY repo_key, scope, name) AS keep))"
)
DELETE_AGENT_SHARED_KNOWLEDGE = (
    "DELETE FROM agent_shared_knowledge WHERE scope='baseline' "
    "AND id NOT IN (SELECT keep.keep_id FROM ("
    "SELECT MAX(id) AS keep_id FROM agent_shared_knowledge "
    "WHERE scope='baseline' GROUP BY repo_key, scope, name) AS keep)"
)
DELETE_SCENE_KNOWLEDGE_REF__MIGRATE_KNOWLEDGE_DEDUP_AND = (
    "DELETE FROM scene_knowledge_ref WHERE knowledge_id IN ("
    "SELECT id FROM agent_shared_knowledge WHERE scope != 'baseline' "
    "AND id NOT IN (SELECT keep.keep_id FROM ("
    "SELECT MAX(id) AS keep_id FROM agent_shared_knowledge "
    "WHERE scope != 'baseline' GROUP BY repo_key, scope, agent_type, issue_num, file_path) AS keep))"
)
DELETE_AGENT_SHARED_KNOWLEDGE__MIGRATE_KNOWLEDGE_DEDUP_AND = (
    "DELETE FROM agent_shared_knowledge WHERE scope != 'baseline' "
    "AND id NOT IN (SELECT keep.keep_id FROM ("
    "SELECT MAX(id) AS keep_id FROM agent_shared_knowledge "
    "WHERE scope != 'baseline' GROUP BY repo_key, scope, agent_type, issue_num, file_path) AS keep)"
)
INSERT_IGNORE_SCENE_KNOWLEDGE_REF = (
    "INSERT OR IGNORE INTO scene_knowledge_ref (knowledge_id, scene_id, agent_type, issue_num, enabled, created_by) "
    "SELECT id, '', '', 0, 1, 'system' FROM agent_shared_knowledge "
    "WHERE scope = 'baseline' AND source = 'system'"
)
INSERT_IGNORE_SCENE_KNOWLEDGE_REF__MIGRATE_KNOWLEDGE_DEDUP_AND = (
    "INSERT OR IGNORE INTO scene_knowledge_ref (knowledge_id, scene_id, agent_type, issue_num, enabled, created_by) "
    "SELECT id, '', agent_type, 0, 1, 'system' FROM agent_shared_knowledge "
    "WHERE scope = 'project' AND source = 'agent'"
)
INSERT_IGNORE_SCENE_KNOWLEDGE_REF__MIGRATE_KNOWLEDGE_DEDUP_AND_2 = (
    "INSERT OR IGNORE INTO scene_knowledge_ref (knowledge_id, scene_id, agent_type, issue_num, enabled, created_by) "
    "SELECT id, '', '', issue_num, 1, 'system' FROM agent_shared_knowledge "
    "WHERE scope = 'issue' AND source = 'agent'"
)
INSERT_IGNORE_SCENE_KNOWLEDGE_REF__MIGRATE_KNOWLEDGE_DEDUP_AND_3 = (
    "INSERT OR IGNORE INTO scene_knowledge_ref (knowledge_id, scene_id, agent_type, issue_num, enabled, created_by) "
    "SELECT id, '', agent_type, issue_num, 1, 'system' FROM agent_shared_knowledge "
    "WHERE scope = 'issue_agent' AND source = 'agent' AND agent_type != ''"
)
DELETE_SCENE_KNOWLEDGE_REF__MIGRATE_KNOWLEDGE_DEDUP_AND_2 = (
    "DELETE FROM scene_knowledge_ref WHERE knowledge_id IN "
    "(SELECT id FROM agent_shared_knowledge WHERE scope = 'member') "
    "AND created_by = 'system'"
)
UPDATE_AGENT_SHARED_KNOWLEDGE__MIGRATE_KNOWLEDGE_DEDUP_AND_4 = (
    "UPDATE agent_shared_knowledge SET status='accepted' "
    "WHERE status='rejected' AND obs_key != '' AND content_hash != '' "
    "AND obs_key IS NOT NULL AND content_hash IS NOT NULL"
)
SELECT_AGENT_SHARED_KNOWLEDGE__MIGRATE_KNOWLEDGE_DEDUP_AND = (
    "SELECT id, obs_key FROM agent_shared_knowledge "
    "WHERE obs_key != '' AND (content_hash = '' OR content_hash IS NULL)"
)
UPDATE_AGENT_SHARED_KNOWLEDGE__MIGRATE_KNOWLEDGE_DEDUP_AND_5 = "UPDATE agent_shared_knowledge SET content_hash=? WHERE id=?"
SELECT_SCENE_KNOWLEDGE_REF = (
    "SELECT EXISTS(SELECT 1 FROM scene_knowledge_ref "
    "WHERE knowledge_id NOT IN (SELECT id FROM agent_shared_knowledge))"
)
DELETE_SCENE_KNOWLEDGE_REF__MIGRATE_KNOWLEDGE_DEDUP_AND_3 = (
    "DELETE FROM scene_knowledge_ref "
    "WHERE knowledge_id NOT IN (SELECT id FROM agent_shared_knowledge)"
)

# ── 自 db_setup._migrate_knowledge() 下沉 ────────────────────────────
UPDATE_AGENT_SHARED_KNOWLEDGE__MIGRATE_KNOWLEDGE = "UPDATE agent_shared_knowledge SET archived=0 WHERE archived=1"
SELECT_AGENT_SHARED_KNOWLEDGE__MIGRATE_KNOWLEDGE = (
    "SELECT id, repo_key, scope, agent_type, issue_num, obs_key, file_path "
    "FROM agent_shared_knowledge WHERE obs_key != '' AND obs_key IS NOT NULL"
)
UPDATE_AGENT_SHARED_KNOWLEDGE__MIGRATE_KNOWLEDGE_2 = "UPDATE agent_shared_knowledge SET obs_key=? WHERE id=?"
SELECT_AGENT_SHARED_KNOWLEDGE__MIGRATE_KNOWLEDGE_2 = (
    "SELECT id, repo_key, file_path FROM agent_shared_knowledge "
    "WHERE file_path != '' AND file_path IS NOT NULL"
)
UPDATE_AGENT_SHARED_KNOWLEDGE__MIGRATE_KNOWLEDGE_3 = "UPDATE agent_shared_knowledge SET file_path=? WHERE id=?"

# ── 自 db_setup._migrate_tables() 下沉 ───────────────────────────────
ALTERS_USER_AND_KNOWLEDGE_COLUMNS = (
    # （user_accounts 列族 2026-11-22 随旧表清退删除）
    # ===== knowledge-obs-design：agent_shared_knowledge OBS 对象存储字段 =====
    "ALTER TABLE agent_shared_knowledge ADD COLUMN obs_key VARCHAR(512) DEFAULT ''",
    "ALTER TABLE agent_shared_knowledge ADD COLUMN content_hash VARCHAR(64) DEFAULT ''",
    "ALTER TABLE agent_shared_knowledge ADD COLUMN updated_at TEXT DEFAULT ''",
    # ===== Issue #67：知识来源 + 归档标记 =====
    "ALTER TABLE agent_shared_knowledge ADD COLUMN source TEXT NOT NULL DEFAULT ''",
    "ALTER TABLE agent_shared_knowledge ADD COLUMN archived INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE agent_shared_knowledge ADD COLUMN description TEXT DEFAULT ''",
    "ALTER TABLE agent_shared_knowledge ADD COLUMN name VARCHAR(256) DEFAULT ''",
)
ALTERS_SCENE_COLUMNS = (
    # ===== Issue-11: devops_pipeline_scene 加 market_scene_id（单向关联） =====
    "ALTER TABLE devops_pipeline_scene ADD COLUMN market_scene_id INTEGER DEFAULT NULL",
    # ===== Issue-11: scene_skill_ref 加 skill_id =====
    "ALTER TABLE scene_skill_ref ADD COLUMN skill_id INTEGER DEFAULT 0",
    # ===== Issue-11: scene_market 加 tags/author/content_hash（场景包格式 + 定时同步） =====
    "ALTER TABLE scene_market ADD COLUMN tags VARCHAR(256) DEFAULT ''",
    "ALTER TABLE scene_market ADD COLUMN author VARCHAR(64) DEFAULT ''",
    "ALTER TABLE scene_market ADD COLUMN content_hash VARCHAR(64) DEFAULT ''",
    "ALTER TABLE scene_market ADD COLUMN obs_icon_key VARCHAR(256) DEFAULT ''",
)
ALTERS_USER_FEEDBACK_COLUMNS = (
    # ===== Issue #177: user_feedback 软删除标记 =====
    "ALTER TABLE user_feedback ADD COLUMN is_deleted INTEGER NOT NULL DEFAULT 0",
    # ===== feedback-task-context: 反馈自动携带任务上下文（weekly-08） =====
    # event_seq/issue_id/agent_type 结构化列；issue_number 为 GitCode 展示号
    "ALTER TABLE user_feedback ADD COLUMN event_seq INTEGER DEFAULT NULL",
    "ALTER TABLE user_feedback ADD COLUMN task_id VARCHAR(191) DEFAULT ''",
    "ALTER TABLE user_feedback ADD COLUMN issue_id INTEGER DEFAULT NULL",
    "ALTER TABLE user_feedback ADD COLUMN issue_number VARCHAR(32) DEFAULT ''",
    "ALTER TABLE user_feedback ADD COLUMN agent_type VARCHAR(32) DEFAULT ''",
    "ALTER TABLE user_feedback ADD COLUMN engine_info TEXT DEFAULT ''",
    "ALTER TABLE user_feedback ADD COLUMN fail_summary TEXT DEFAULT ''",
    # ===== feedback-openapi（weekly-08）：Agent 自动反馈 + 反馈→Issue 闭环 =====
    "ALTER TABLE user_feedback ADD COLUMN source VARCHAR(16) DEFAULT 'user'",
    "ALTER TABLE user_feedback ADD COLUMN skill_id INTEGER DEFAULT NULL",
    "ALTER TABLE user_feedback ADD COLUMN skill_name VARCHAR(191) DEFAULT ''",
    "ALTER TABLE user_feedback ADD COLUMN skill_scope VARCHAR(16) DEFAULT ''",
    "ALTER TABLE user_feedback ADD COLUMN issue_url TEXT DEFAULT ''",
    "ALTER TABLE user_feedback ADD COLUMN issue_number VARCHAR(32) DEFAULT ''",
    "ALTER TABLE user_feedback ADD COLUMN converted_at VARCHAR(64) DEFAULT NULL",
    "ALTER TABLE user_feedback ADD COLUMN feedback_kind VARCHAR(32) DEFAULT ''",
    # ⚠️ Issue #3731 的 `convert_fail_count` **不在这里**：本常量只被**冻结老轨**
    # `db_setup._migrate_tables()` 消费，存量库（已盖 legacy-stamp 202610300001）整段
    # 不执行 → 新增列写在这里线上表现为「读侧 Unknown column 1054」（部署验证实测：
    # 转换读侧首轮即崩）。该列改走注册式迁移 202611240004
    # （src/domains/mgmt/migrations/feedback_convert_fail_count.py）。
    # 新增列一律用 register_migration()，不要续写本常量。
)
UPDATE_MANAGED_REPOS = (
    "UPDATE managed_repos SET bot_username=? "
    "WHERE (bot_username='devops-bot' OR bot_username='' OR bot_username IS NULL) "
    "AND repo_key NOT LIKE 'github:%'"
)
UPDATE_MANAGED_REPOS__MIGRATE_TABLES = (
    "UPDATE managed_repos SET bot_username=? "
    "WHERE (bot_username='devops-bot' OR bot_username='' OR bot_username IS NULL) "
    "AND repo_key LIKE 'github:%'"
)
UPDATE_MANAGED_REPOS__MIGRATE_TABLES_2 = (
    "UPDATE managed_repos SET bot_username=? "
    "WHERE bot_username=? AND repo_key LIKE 'github:%'"
)
UPDATE_DEVOPS_PIPELINE_SCENE__MIGRATE_TABLES = "UPDATE devops_pipeline_scene SET pr_subscription_enabled=1, scene_type='standard' WHERE scene_type='pr_subscription'"

# ── 自 db_setup._rebuild_scene_market_unique() 下沉 ──────────────────
ALTER_SCENE_MARKET__REBUILD_SCENE_MARKET_UNIQUE = (
    "ALTER TABLE scene_market ADD UNIQUE INDEX uk_name_scope_user_repo "
    "(name, scope, source_user_id, repo_key)"
)
ALTER_SCENE_MARKET = "ALTER TABLE scene_market RENAME TO scene_market_old"
INSERT_SCENE_MARKET = (
    "INSERT INTO scene_market SELECT "
    "id, name, display_name, version, description, market_description, "
    "icon, category_id, is_featured, like_count, source, source_user_id, "
    "recommended_template, nodes_json, onboarding, tags, author, "
    "content_hash, obs_icon_key, is_active, "
    "COALESCE(scope, 'official'), COALESCE(repo_key, ''), "
    "created_by, updated_by, created_at, updated_at "
    "FROM scene_market_old"
)

# ── 自 db_setup._migrate_scene_table_to_repo_model() 下沉 ────────────
SELECT_DEVOPS_PIPELINE_SCENE__MIGRATE_SCENE_TABLE_TO_REPO = (
    "SELECT scene_id, name, description, scope, repo_key, user_id, nodes_json, onboarding, is_active "
    "FROM devops_pipeline_scene"
)
SELECT_USER_REPOS = "SELECT user_id, repo_key FROM user_repos"
DROP_DEVOPS_PIPELINE_SCENE = "DROP TABLE devops_pipeline_scene"

# ── 自 db_setup._seed_official_scene_market_from_pipelines() 下沉 ────
INSERT_IGNORE_SCENE_MARKET__SEED_OFFICIAL_SCENE_MARKET_F = (
    "INSERT OR IGNORE INTO scene_market "
    "(name, display_name, version, description, market_description, category_id, is_featured, like_count, source, source_user_id, recommended_template, nodes_json, onboarding, is_active) "
    "VALUES (?,?, '1.0.0', '', ?, 1, 0, 0, 'official', 0, '', ?, ?, 1)"
)

# ── 自 db_setup._backfill_scene_display_names() 下沉 ─────────────────
ALTER_DEVOPS_PIPELINE_SCENE = "ALTER TABLE devops_pipeline_scene ADD COLUMN display_name TEXT DEFAULT ''"
SELECT_SCENE_MARKET__BACKFILL_SCENE_DISPLAY_NAMES = (
    "SELECT DISTINCT name, display_name FROM scene_market "
    "WHERE source='official' AND is_active=1 AND display_name<>''"
)
SELECT_DEVOPS_PIPELINE_SCENE__BACKFILL_SCENE_DISPLAY_NAMES = "SELECT id, scene_id, name, display_name FROM devops_pipeline_scene"
UPDATE_DEVOPS_PIPELINE_SCENE__BACKFILL_SCENE_DISPLAY_NAMES = "UPDATE devops_pipeline_scene SET display_name=? WHERE id=?"
UPDATE_GRAPE_ACCOUNTS = "UPDATE grape_accounts SET token_version = token_version + 1"

# ── 自 db_setup._uc_unique_key_columns() 下沉 ────────────────────────
SQL_USER_CREDENTIALS__UC_UNIQUE_KEY_COLUMNS = "SHOW INDEX FROM user_credentials"

# ── 自 db_setup.<module>() 下沉 ──────────────────────────────────────
INSERT_USER_CREDENTIALS = (
    "INSERT INTO user_credentials__uqkind "
    "(id, user_id, provider, kind, config_json, created_at, updated_at) "
    "SELECT COALESCE(id,''), user_id, provider, COALESCE(kind,''), "
    "config_json, COALESCE(created_at,''), COALESCE(updated_at,'') "
    "FROM user_credentials"
)

# ── 自 db_setup._fn_user_credentials_unique_kind() 下沉 ──────────────
SQL_USER_CREDENTIALS = (
    "RENAME TABLE user_credentials TO user_credentials__old_backup, "
    "user_credentials__uqkind TO user_credentials"
)


# ── 自 db_setup 内联 DDL/DML 下沉（动态列名/表名一律「函数返回」，不 import 常量属性）──
#
# 这些语句原本内联在 `src/api/db_setup.py` 的迁移函数里，用 f-string 拼列名/占位符，
# 静态扫描按路径把它们算成 api 消费 mgmt 表。搬到归属域后 SQL 与表同处一个模块；
# 执行顺序仍由 db_setup 的迁移编排保证（本模块不含执行逻辑）。

def organization_insert_from(old_table: str, names: str, sel: str) -> str:
    """organization 文本重建后从旧表搬数据（列名/选择表达式由调用方按列清单生成）。"""
    return f"INSERT INTO organization ({names}) SELECT {sel} FROM {old_table}"


def login_whitelist_clear_null_literal(col: str) -> str:
    """把历史导入留下的字面 'NULL' 字符串清成空串（避免大小写不敏感唯一索引撞 1062）。"""
    return (f"UPDATE login_whitelist SET {col}='' "
            f"WHERE {col} IS NOT NULL AND UPPER(TRIM({col}))='NULL'")


def managed_repos_add_column(col: str) -> str:
    """managed_repos 补平台存储列（列名来自平台卡，调用方已做合法性校验）。"""
    return f"ALTER TABLE managed_repos ADD COLUMN {col} TEXT DEFAULT ''"


def managed_repos_select_column(col: str) -> str:
    """读 managed_repos 某存储列的非空行（F4 凭据回填用）。"""
    return (f"SELECT repo_key, {col} FROM managed_repos "
            f"WHERE {col} IS NOT NULL AND {col} != ''")


def unified_user_mapping_widen_dingtalk(suffix: str) -> str:
    """unified_user_mapping.dingtalk_user_id TEXT → VARCHAR(191)（MySQL 侧定长化）。"""
    return f"ALTER TABLE unified_user_mapping MODIFY dingtalk_user_id VARCHAR(191){suffix}"


def scene_market_drop_index(index_name: str) -> str:
    """删除 scene_market 的旧唯一索引（索引名由 information_schema 探测得到）。"""
    return f"ALTER TABLE scene_market DROP INDEX `{index_name}`"
