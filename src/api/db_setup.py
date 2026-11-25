"""数据库初始化:新增 events,agent_sessions,配置表 + JSON 种子数据导入"""
import hashlib
import inspect
import json, os, threading
import time as _time_mod
from contextlib import nullcontext
from dataclasses import dataclass
from typing import Callable, List, Optional

from src.domains.insight.schema_ddl import (
    _MERGE_GATE_LOG_DDL,
    _REPO_CREDENTIALS_DDL,
    _API_LATENCY_LOG_DDL,
    _OBS_LLM_USAGE_DDL,
    _OBS_LLM_USAGE_ARCHIVE_DDL,
    _OBS_ALERT_DDL,
    _OBS_ALERT_EVENTS_DDL,
    _OBS_RUNTIME_SAMPLES_DDL,
    _OBS_METRIC_ROLLUP_DDL,
    _OBS_SLO_DDL,
    _OBS_SLO_SNAPSHOTS_DDL,
    _OBS_LOG_DDL,
    _OBS_WEBHOOK_REJECTS_DDL,
)
from src.domains.mgmt.schema_ddl import (
    _EVENT_ROUTE_CONFIG_DDL,
    _PATROL_FINDINGS_DDL,
    _SCHEMA_MIGRATIONS_DDL,
    _FEISHU_ONBOARD_TASK_DDL,
    _PIPELINE_INSTANCES_DDL,
    _AGENT_SHARED_KNOWLEDGE_DDL,
    _SCENE_KNOWLEDGE_REF_DDL,
    _SCENE_KNOWLEDGE_REF_MYSQL_DDL,
    _EVENT_ACTION_FILTER_DDL,
    _PIPELINE_SCENE_DDL,
    _FEATURED_TEMPLATES_DDL,
    _LOGIN_WHITELIST_DDL,
    _SKILL_CATEGORY_DDL,
    _CATEGORY_DICT_DDL,
    _SCENE_MARKET_DDL,
    _SCENE_LIKES_DDL,
    _SCENE_SKILL_REF_DDL,
    _SKILL_LIKES_DDL,
    _UPLOADED_FILES_DDL,
    _AUTOPILOT_TASKS_DDL,
    _AUTOPILOT_SUBSCRIPTIONS_DDL,
    _ADMIN_ACCOUNTS_TABLE_DDL,
    _ORGANIZATION_DDL,
    _ORG_MEMBER_DDL,
    _ORG_INVITATION_DDL,
    _GRAPE_ACCOUNTS_DDL,
    _EXTERNAL_IDENTITIES_DDL,
    _VERIFICATION_CODES_DDL,
    _AUDIT_LOGS_DDL,
    _SIGNUP_WHITELIST_DDL,
    _LOGIN_FAILURES_DDL,
    _CREDENTIAL_REVEAL_LOG_DDL,
    _EXCHANGE_CODES_DDL,
    _BINDING_TICKETS_DDL,
    _COMMUNITY_DDL,
    _COMMUNITY_COMMENTS_DDL,
    _COMMUNITY_LIKES_DDL,
    CHANNEL_TABLE_DDL,
    _ORG_DISPATCH_DDL,
    _ORG_DISPATCH_TARGET_DDL,
    _ORG_DISPATCH_AGGREGATE_DDL,
    _ORG_CONTEXT_SNAPSHOT_DDL,
    _ORG_CONTEXT_DDL,
)
from src.domains.runtime.schema_ddl import (
    _EVENTS_TABLE_DDL,
    _EVENT_SKIPPED_DDL,
    _AGENT_SESSIONS_TABLE_DDL,
    _AGENT_TYPE_DDL,
    _SKILL_DDL,
    _SKILL_VERSION_DDL,
    _SKILL_FILE_DDL,
    _SKILL_COMMENT_DDL,
    _META_TABLE_DDL,
    _AGENT_RUNTIMES_DDL,
    _CHAT_CONVERSATIONS_DDL,
    _CHAT_MESSAGES_DDL,
    _GLOBAL_IM_BOT_CONFIG_DDL,
    _IM_INSTANCE_DDL,
    # Issue #3799：渠道健康状态表（runtime 域，ret=-14 暂停可观测）
    _CHANNEL_HEALTH_DDL,
    _VOD_CONVERSATIONS_DDL,
    _ISSUE_DDL,
    _AGENT_TASKS_DDL,
    _TASK_MESSAGES_DDL,
    _AGENT_TYPE_RUNTIME_POOL_DDL,
    _RUNTIME_CREDENTIALS_DDL,
    _ISSUE_STATE_DDL,
    _ISSUE_AGENT_SESSION_DDL,
    _EVENT_THREAD_DDL,
    _ENGINE_CAPABILITIES_DDL,
    _SCENE_CATEGORY_DDL,
    _SCENE_ENGINE_DETACH_LOG_DDL,
    _ENGINE_LIFECYCLE_AUDIT_DDL,
    _DISTRIBUTED_LOCKS_DDL,
    _BRANCH_GUARD_STATE_DDL,
    _AUTOPILOT_EXECUTIONS_DDL,
    _AUTOPILOT_PAYLOAD_GUARD_DDL,
    _CUL_DDL,
    _SC_DDL,
    _TAKEOVER_SESSIONS_DDL,
    _WEBHOOK_INBOX_DDL,
)

from src.domains.mgmt import migrations_ddl as mgmt_mig
from src.domains.runtime import migrations_ddl as runtime_mig
from src.domains.insight import migrations_ddl as insight_mig
from src.kernel.session_manager import db, db_lock
from src.kernel.config import log
from src.kernel import ddl_safety as _ddl_safety

# ====== 新表 DDL ======

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

# A2A 链路巡检器（docs/weekly-09/a2a-chain-patrol-design.md §三）：只增新表，不改既有表。
# UNIQUE(kind, task_id, event_seq) + INSERT OR IGNORE = 集群双主同轮同发现静默去重（D3 兜底层）。
# Issue #752: 合入流程冲突标记扫描门禁日志（docs/issue-752/plan.md §3）。
# 只增新表不改既有表；主键 UUID（新表硬性要求）；每次门禁决策一行
# （phase: pre_merge/post_merge；decision: blocked/warned/alarmed/passed/error）。
# ===== Issue #536: 迁移版本表（迁移只跑一遍的地基）=====
# 设计：docs/weekly-09/migration-versioning-and-ddl-safety-design.md §D1
# - version 主键（'202611010001' 日期+序号）；kind: safe/backfill/cleanup/legacy-stamp；
# - checksum = fn 源码 sha256 前 16 位，已盖章项漂移只告警不阻断（fail-safe）；
# - 全新库与在线库统一经 _create_tables 建出，保证两条路径 schema 一致。
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
# ⚠️ 时间口径警告（2026-10-18 加固）：这两个 DEFAULT 在 MySQL 上经 mysql_compat 翻译后等价于
# 服务器本地时间（CST=UTC+8），而读侧（bot_lease._heartbeat_window / list_active_instances）
# 一律按 **UTC** 比较 last_heartbeat。因此**写入方必须显式传 last_heartbeat（UTC 值）**，
# 不要依赖默认值——历史上就是 MySQL 分支用 CAST(NOW() AS CHAR) 写 CST，导致幽灵实例占满一致性
# 哈希环、bot 永不连接（issue #365 / commit f7f9951005）。默认值保留仅为兼容旧插入路径。
# ===== Issue 任务卡片表（原 tasks，Phase B1 改名；结构不变，后续 B2 收编 issue_state 字段）=====
# ===== new-version2 M1:agent_tasks 任务队列(确定路由 + 事件档案快照)=====
# 见 agent-runtime-event-full-design.md §4.3 / concurrency-design.md / skill-delivery-remote.md
# ===== 运行日志:task_messages 结构化消息表(实时日志功能,对齐 multica TaskMessage 模型)=====
# ===== agent-cluster-design.md §4.1:Agent 类型 -> 引擎池绑定(1..N)=====
# ===== new-version2 M3:runtime_credentials 独立凭据(rta_ token,见 agent-runtime-event-full-design.md §4.1)=====
# ===== new-version2 VOD:Issue 生命周期状态机(权威阶段,见 vod-skill-upgrade-plan.md §3.0.2)=====
# ===== Issue #455: pipeline_instances 表（0→1 安装/清理路径被引用但此前未建表）=====
# 清理逻辑（onboarding/repo_cleanup）按 repo_key 直删该表；新实例缺表会报 "no such table"。
# ===== new-version2 Issue 级执行会话(多 Issue 并行,见 multi-issue-isolation-design.md §3.3)=====
# 配置层(devops_agent_session,repo+agent)与执行层(issue_agent_session,repo+agent+issue)分离.
# new-version2 Event 对话(event-conversation-design.md):以事件为锚点,与处理该事件的 agent 对话
# 共享知识索引:正文=工作区文件(project-info/knowledge.md,issues/{n}/knowledge.md,
# {agent}/issues/{n}/knowledge.md),本表仅存索引/状态/审计(见 event-conversation-design.md §3.1.1)
# ===== scene_knowledge_ref：知识-场景-Agent 绑定关系表（Issue #67） =====
# MySQL 原生 DDL（不走翻译层，生产环境用）
# ===== engine tagging (event-dispatch-redesign-v2.md 2.2) =====
# ===== Issue #49：Webhook 事件 action 过滤规则表（Q3 已确认可配置）=====
# run_pipeline L1 准入过滤：{event_type, action} 命中规则决定是否放行/副作用短路/过滤。
# - allow=1 且 require_changes 为空 → 放行进入阶段路由
# - allow=1 且 require_changes 非空 → 需 payload.changes 命中这些键才放行（如 edit 需 description 变更）
# - allow=0 → 过滤（写 event_skipped）
# - allow=2（副作用-only）→ 执行 handle_side_effect 后直接短路，不进入 agent（如 close）
# - actor: user / bot / any —— bot 操作（self-comment）在前置已处理，此处主要针对 user 操作细分
# ===== Scene Center: 场景配置表（替代 configs/pipelines.json 运行时读取）=====
# 场景只与仓库（repo_key）关联，无 scope/user_id 来源区分（issue-39 方案 B）
# 官方场景不存于此表，由 scene_market(source='official') 承载（scene_sync 同步）
# ===== 优选项目模板（GitCode/GitHub 云原生热门项目）=====
# github_username 唯一约束不在 DDL 内联定义——SQLite 内联 UNIQUE 不支持
# WHERE 条件（需要允许空串 '' 共存）。改为运行时部分索引
# （_ensure_case_insensitive_indexes + Issue #332 首跑索引）。
# MySQL 侧在 _ensure_case_insensitive_indexes 中用生成列 LOWER(NULLIF(...)) + 唯一索引。
#
# 2026-11-12 行式化（weekly-11）：新增 provider/username 两列——新行=一条
# (provider, username) 白名单条目（provider=OAuth 渠道 key），老列式列
# （gitcode/github/gitee_username）原样保留参与读路径兜底（双读兼容：
# 新行优先→老列兜底，逐人判定）。新行唯一性=部分表达式索引，见
# _fn_login_whitelist_row_columns 注册式迁移。

# ===== Issue-11: DevOps 市场（场景市场 + 技能市场）======
# 技术方案：docs/new-version3/issue-11-scene-skill-market-tech-design.md 第二章

# 引擎绑定真实性同步：场景引擎解绑审计表（引擎离线超 8h 系统自动移除场景绑定的留痕）
# Issue #2133：引擎生命周期审计（spawn_failed 告警/降级、恢复加回、任务绑定对账留痕）
# Issue #993：分支守卫的期望远端 sha 状态（与 distributed_locks 同源 DB，跨 worker 可见）。
# ===== Issue #150: Autopilot 定时自动化任务（§5.3.1 + §5.3.2 + §5.3.3）=====
# Issue #1082/#2423（#2423 移植）：统一凭据库 —— 全系统唯一持明文处（真本单存）。
# ref 稳定（同 plugin+name+owner 重置密钥时保持不变，句柄引用不失效）；
# 其余各处（overlay JSON / 配置视图 / 审计日志）只允许出现引用句柄 vault://cred/<ref>。
_PLUGIN_CREDENTIAL_VAULT_DDL = """
CREATE TABLE IF NOT EXISTS grape_t_plugin_credential_vault (
    id         TEXT PRIMARY KEY,
    ref        TEXT NOT NULL UNIQUE,         -- 引用编号（句柄 = vault://cred/<ref>）
    plugin_id  TEXT NOT NULL DEFAULT '',
    name       TEXT NOT NULL,                -- 凭据名（密钥命名约定，如 HWCLOUD_AK）
    secret     TEXT NOT NULL DEFAULT '',     -- 明文（唯一持明文列）
    owner_id   TEXT DEFAULT '',              -- 归属（scope:scope_key，审计溯源）
    created_at TEXT DEFAULT (datetime('now')),
    UNIQUE (plugin_id, name, owner_id)
)
"""

# ===== 桶B B1（plugin 体系，桶B 移植）：七张插件表 DDL（自 weekly 提取）=====

_PLUGIN_REGISTRY_DDL = """
CREATE TABLE IF NOT EXISTS grape_t_plugin_registry (
    id          TEXT PRIMARY KEY,
    name        TEXT NOT NULL,  -- 插件唯一名（小写字母/数字/连字符）
    version     TEXT DEFAULT '1.0.0',
    display_name TEXT DEFAULT '',
    description TEXT DEFAULT '',
    author      TEXT DEFAULT '',
    credentials_json TEXT DEFAULT '[]',   -- 凭据声明 [{key,label,required}]（plugin.json credentials）
    bundled_skills_json TEXT DEFAULT '[]', -- 捆绑技能声明 [{skill_name,display_name,description,agent_type}]（plugin.json bundled_skills，#1089 挂点⑤）
    status      TEXT DEFAULT 'installed',
    source      TEXT DEFAULT 'market',
    created_at  TEXT DEFAULT (datetime('now')),
    updated_at  TEXT DEFAULT (datetime('now')),
    UNIQUE (name)
)
"""

_PLUGIN_SKILL_DDL = """
CREATE TABLE IF NOT EXISTS grape_t_plugin_skill (
    plugin_id  TEXT NOT NULL,
    skill_name TEXT NOT NULL,
    created_by_plugin INTEGER DEFAULT 0,
    created_at TEXT DEFAULT (datetime('now')),
    PRIMARY KEY (plugin_id, skill_name)
)
"""

_PLUGIN_TABLE_DDL = """
CREATE TABLE IF NOT EXISTS grape_t_plugin (
    id            TEXT PRIMARY KEY,
    name          TEXT NOT NULL,
    version       TEXT DEFAULT '1.0.0',
    display_name  TEXT DEFAULT '',
    description   TEXT DEFAULT '',
    manifest      MEDIUMTEXT DEFAULT '{}',      -- manifest.json 全文 JSON（详情展示/审计）
    manifest_hash VARCHAR(64) DEFAULT '',       -- manifest sha256（变更检测）
    category_id   TEXT DEFAULT '',
    author        VARCHAR(64) DEFAULT '',
    like_count    INTEGER DEFAULT 0,
    object_key    VARCHAR(512) DEFAULT '',      -- 插件包对象键（OBS/local）
    content_hash  VARCHAR(64) DEFAULT '',       -- 插件包 zip sha256
    namespace_owner VARCHAR(64) DEFAULT '',     -- @owner 命名空间归属账号（B2 #1085；@owner 名=命名空间主，存量行迁移回填=现存发布者，新上传裸名=空开放轨）
    source_repo   VARCHAR(191) DEFAULT '',      -- Issue #1798：来源仓库声明（owner/repo，来源一致性门控回写）
    source_ref    VARCHAR(40) DEFAULT '',       -- Issue #1798：来源 HEAD commit sha（40 位）
    source_content_hash VARCHAR(64) DEFAULT '', -- Issue #1798：来源 canonical 树指纹（版本回溯）
    scope         VARCHAR(32) DEFAULT 'user',   -- official/user/repo 三态可见性
    owner_id      TEXT DEFAULT '',              -- user_id（user scope）/ repo_key（repo scope）
    created_by    VARCHAR(64) DEFAULT '',
    status        VARCHAR(16) DEFAULT 'active', -- active/superseded/revoked（#1799 软删+版本生效三态；superseded=版本族历史，revoked=软删台账）
    certification VARCHAR(16) DEFAULT 'none',   -- 认证位：none|official（B2 #1085，D5 双轨）
    org_id        INTEGER DEFAULT 0,            -- 组织隔离维度（#2423 M2b 插件格子配套/#2544 exec 按 org 选格）
    certified_by  VARCHAR(64) DEFAULT '',       -- 认证操作人（管理端）
    certified_at  TEXT DEFAULT '',              -- 最近标记时间
    revoked_at    TEXT DEFAULT '',              -- 最近撤销时间
    created_at    TEXT DEFAULT (datetime('now')),
    updated_at    TEXT DEFAULT (datetime('now')),
    UNIQUE (name, version, scope, owner_id)
)
"""

_PLUGIN_LIKES_DDL = """
CREATE TABLE IF NOT EXISTS grape_t_plugin_likes (
    id          TEXT PRIMARY KEY,
    user_id     TEXT NOT NULL,
    plugin_id   TEXT NOT NULL,
    created_at  TEXT DEFAULT (datetime('now')),
    UNIQUE (user_id, plugin_id)
)
"""

_PLUGIN_CATEGORY_DDL = """
CREATE TABLE IF NOT EXISTS grape_t_plugin_category (
    id          TEXT PRIMARY KEY,
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

_PLUGIN_CERTIFICATION_AUDIT_DDL = """
CREATE TABLE IF NOT EXISTS grape_t_plugin_certification_audit (
    id          TEXT PRIMARY KEY,
    plugin_id   TEXT NOT NULL,
    action      VARCHAR(16) DEFAULT '',       -- certify | revoke
    operator    VARCHAR(64) DEFAULT '',       -- 管理端操作人
    detail      TEXT DEFAULT '',
    created_at  TEXT DEFAULT (datetime('now'))
)
"""

_PLUGIN_CONFIG_OVERLAYS_DDL = """
CREATE TABLE IF NOT EXISTS grape_t_plugin_config_overlays (
    id          TEXT PRIMARY KEY,
    scope       TEXT NOT NULL,               -- org/personal/repo/node/task
    scope_key   TEXT NOT NULL,               -- 层内主体：org id / user id / repo key / node id / task id
    plugin_id   TEXT NOT NULL,               -- 插件标识
    config_json TEXT NOT NULL DEFAULT '{}',  -- 该层差异覆盖（仅写差异项）
    updated_by  TEXT DEFAULT '',
    -- 注意：datetime('now') 经 mysql_compat 翻译后为数据库服务器本地时间
    --（见本文件时间基准告警），跨时区比较类读取需统一时区约定
    updated_at  TEXT DEFAULT (datetime('now')),
    UNIQUE (scope, scope_key, plugin_id)
)
"""


_PLUGIN_CALL_LOGS_DDL = """
CREATE TABLE IF NOT EXISTS grape_t_plugin_call_logs (
    id          TEXT PRIMARY KEY,
    dedup_key   TEXT NOT NULL UNIQUE,
    ts          TEXT NOT NULL,
    plugin      TEXT DEFAULT '',
    command     TEXT DEFAULT '',
    adapter     TEXT DEFAULT '',
    duration_ms INTEGER DEFAULT 0,
    exit_code   INTEGER DEFAULT 0,
    outcome     TEXT NOT NULL DEFAULT 'success',
    runtime_id  INTEGER DEFAULT 0,
    task_id     TEXT DEFAULT '',
    repo_key    TEXT DEFAULT '',
    error_json  TEXT DEFAULT '',
    created_at  TEXT DEFAULT (datetime('now'))
)
"""

# Issue #2423 M2b（#2423 移植）：OV 凭据格子（user_id×org）+ 吊销台账——
# 明文唯一落点为 grape_t_plugin_credential_vault；格子表只存 vault_ref 句柄。
# DDL 单一事实源在 domains/mgmt/auth/credential_cell.py（Standards #4：双份定义易漂移）。
from src.domains.mgmt.auth.credential_cell import (
    OV_CREDENTIAL_CELLS_DDL as _OV_CREDENTIAL_CELLS_DDL,
    OV_CREDENTIAL_REVOCATIONS_DDL as _OV_CREDENTIAL_REVOCATIONS_DDL,
)

_NEW_TABLES = [
    ("grape_t_plugin_credential_vault", _PLUGIN_CREDENTIAL_VAULT_DDL),
    ("grape_t_plugin_registry", _PLUGIN_REGISTRY_DDL),
    ("grape_t_plugin_skill", _PLUGIN_SKILL_DDL),
    ("plugin", _PLUGIN_TABLE_DDL),
    ("grape_t_plugin_call_logs", _PLUGIN_CALL_LOGS_DDL),
    ("grape_t_plugin_likes", _PLUGIN_LIKES_DDL),
    ("grape_t_plugin_category", _PLUGIN_CATEGORY_DDL),
    ("grape_t_plugin_certification_audit", _PLUGIN_CERTIFICATION_AUDIT_DDL),
    ("grape_t_plugin_config_overlays", _PLUGIN_CONFIG_OVERLAYS_DDL),
    ("grape_t_ov_credential_cells", _OV_CREDENTIAL_CELLS_DDL),
    ("grape_t_ov_credential_cell_revocations", _OV_CREDENTIAL_REVOCATIONS_DDL),
    ("admin_accounts", _ADMIN_ACCOUNTS_TABLE_DDL),
    ("meta", _META_TABLE_DDL),
    ("grape_t_patrol_findings", _PATROL_FINDINGS_DDL),
    ("grape_t_merge_gate_log", _MERGE_GATE_LOG_DDL),
    ("grape_t_schema_migrations", _SCHEMA_MIGRATIONS_DDL),
    ("agent_runtimes", _AGENT_RUNTIMES_DDL),
    ("agent_tasks", _AGENT_TASKS_DDL),
    ("task_messages", _TASK_MESSAGES_DDL),
    ("agent_type_runtime_pool", _AGENT_TYPE_RUNTIME_POOL_DDL),
    ("runtime_credentials", _RUNTIME_CREDENTIALS_DDL),
    # issue_state: 保留创建（session_store.py 兜底读取 + 测试引用），
    # C5 DROP 条件性跳过（生产环境确认无孤儿后才删，测试环境保留）
    ("issue_state", _ISSUE_STATE_DDL),
    # Issue #455: pipeline_instances —— 清理逻辑引用但不建表会导致新实例 "no such table"
    ("pipeline_instances", _PIPELINE_INSTANCES_DDL),
    ("issue_agent_session", _ISSUE_AGENT_SESSION_DDL),
    ("events", _EVENTS_TABLE_DDL),
    ("grape_t_webhook_inbox", _WEBHOOK_INBOX_DDL),
    ("event_skipped", _EVENT_SKIPPED_DDL),
    ("engine_capabilities", _ENGINE_CAPABILITIES_DDL),
    ("event_action_filter", _EVENT_ACTION_FILTER_DDL),
    ("chat_conversations", _CHAT_CONVERSATIONS_DDL),
    ("chat_messages", _CHAT_MESSAGES_DDL),
    ("devops_agent_session", _AGENT_SESSIONS_TABLE_DDL),
    ("event_route_config", _EVENT_ROUTE_CONFIG_DDL),
    ("agent_type", _AGENT_TYPE_DDL),
    ("skill", _SKILL_DDL),
    ("grape_t_skill_version", _SKILL_VERSION_DDL),
    ("grape_t_skill_file", _SKILL_FILE_DDL),
    ("grape_t_skill_comment", _SKILL_COMMENT_DDL),
    ("grape_im_instance", _IM_INSTANCE_DDL),
    # Issue #3799：渠道健康状态（ret=-14 风控暂停）——runtime 域状态表，
    # 管理台/告警与渠道写侧共用同一真源（进程重启后仍可见）。
    ("grape_t_channel_health", _CHANNEL_HEALTH_DDL),
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
    ("grape_t_category_dict", _CATEGORY_DICT_DDL),
    ("scene_market", _SCENE_MARKET_DDL),
    ("scene_likes", _SCENE_LIKES_DDL),
    ("scene_skill_ref", _SCENE_SKILL_REF_DDL),
    # 引擎绑定真实性同步：场景引擎解绑审计表
    ("scene_engine_detach_log", _SCENE_ENGINE_DETACH_LOG_DDL),
    # Issue #2133：引擎生命周期审计（spawn_failed 告警/降级 + 恢复加回 + 任务对账）
    ("grape_t_engine_lifecycle_audit", _ENGINE_LIFECYCLE_AUDIT_DDL),
    ("skill_likes", _SKILL_LIKES_DDL),
    ("skill_category", _SKILL_CATEGORY_DDL),
    ("uploaded_files", _UPLOADED_FILES_DDL),
    ("distributed_locks", _DISTRIBUTED_LOCKS_DDL),
    ("api_latency_log", _API_LATENCY_LOG_DDL),
    # ===== Issue-45: API Token 认证表 =====
    ("session_api_tokens", """
CREATE TABLE IF NOT EXISTS session_api_tokens (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    -- 唯一键列用定长类型：MySQL 的 TEXT 不能直接建 UNIQUE（BLOB/TEXT column used
    -- in key specification without a key length），会话键是 task_id/conv_id，远短于 191。
    session_key VARCHAR(191) NOT NULL,
    repo_key TEXT NOT NULL DEFAULT '',
    agent_type TEXT NOT NULL DEFAULT '',
    user_id TEXT NOT NULL DEFAULT 0,
    token_sha256 TEXT NOT NULL,
    expires_at INTEGER NOT NULL DEFAULT 0,
    -- worker=任务执行期会话凭据 / takeover=接管凭据（202611160001 引入；同步进基表
    -- DDL，避免全新库/测试库只靠迁移补列，缺列即 1054 → 会话凭据全 401）
    kind VARCHAR(16) NOT NULL DEFAULT 'worker'
)"""),
    # ===== 事件活跃守卫（duplicate-issue-opened-event-fix-design.md 方案 A/C）=====
    # 同一 (repo, issue, agent) 同一时刻只允许一条活跃事件；跨进程原子（PK 唯一约束，
    # SQLite INSERT OR IGNORE / MySQL 由 mysql_compat 转 INSERT IGNORE）。终态释放或 TTL 兜底清理。
    ("event_active_guard", """
CREATE TABLE IF NOT EXISTS event_active_guard (
    repo_key    TEXT NOT NULL,
    issue_id    TEXT NOT NULL,
    agent_type  TEXT NOT NULL,
    seq         INTEGER NOT NULL DEFAULT 0,
    created_at  TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (repo_key, issue_id, agent_type)
)"""),
    ("api_tokens", """
CREATE TABLE IF NOT EXISTS api_tokens (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id TEXT NOT NULL,
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
    user_id         TEXT      NOT NULL DEFAULT 0,
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
    ("autopilot_payload_guard", _AUTOPILOT_PAYLOAD_GUARD_DDL),
    ("autopilot_subscriptions", _AUTOPILOT_SUBSCRIPTIONS_DDL),
]

# ===== 渠道族（归属 mgmt；DDL 由 scripts/migrations/001 归位到 owner 域）=====
# 只在 owner 进程建表（与 db_setup 里 ensure_channel_schema 的域闸门同源），
# 避免三库模式下每个进程各建一份同名基表。
_NEW_TABLES.extend(CHANNEL_TABLE_DDL)

# ===== Organization（组织）特性 =====
_NEW_TABLES.extend([
    ("organization", _ORGANIZATION_DDL),
    ("org_member", _ORG_MEMBER_DDL),
    ("org_invitation", _ORG_INVITATION_DDL),
])

# ===== 账号体系新表 =====
# Issue #474（方案A）：凭据明文按需取回日志——审计+高频取回告警计数用（不存明文值）
# P0-3: OAuth 回调成功后的一次性交换码——替代 URL 直接传递 access_token
_NEW_TABLES.extend([
    ("grape_accounts", _GRAPE_ACCOUNTS_DDL),
    ("external_identities", _EXTERNAL_IDENTITIES_DDL),
    ("verification_codes", _VERIFICATION_CODES_DDL),
    ("audit_logs", _AUDIT_LOGS_DDL),
    ("signup_whitelist", _SIGNUP_WHITELIST_DDL),
    ("login_failures", _LOGIN_FAILURES_DDL),
    ("grape_t_credential_reveal_log", _CREDENTIAL_REVEAL_LOG_DDL),
    ("exchange_codes", _EXCHANGE_CODES_DDL),
    ("binding_tickets", _BINDING_TICKETS_DDL),
])

# grape_accounts 的「活跃 email 唯一」索引由迁移模块
# `mgmt/migrations/active_email_uniqueness.py` 统一实现（新装/升级共用；Issue #3469 O）。

# ===== Community tables (grape-website community page) =====
_NEW_TABLES.extend([
    ("community_posts", _COMMUNITY_DDL),
    ("community_comments", _COMMUNITY_COMMENTS_DDL),
    ("community_post_likes", _COMMUNITY_LIKES_DDL),
])

# ===== 可观测性:LLM token 用量台账 =====
# 设计文档:docs/weekly-07/observability-llm-usage-design.md
# 关键约束:绝不复用 agent_tasks.usage_json —— 该列是"会话续接指针"(session_id/work_dir/
# engine),被 routes/events.py、im/handler_async.py 等消费,归 complete_task 独占。
# 台账年度归档（审计决定 2026-10-17）：超过保留期的用量行搬到本表，台账只留近一年。
# 结构镜像 obs_llm_usage，但 **dedup_key 不唯一**（归档是"搬走"，不承担幂等职责）
# 且不带原 id（避免与台账自增 id 撞车；追溯靠 task_id/dedup_key/created_at）。
_NEW_TABLES.extend([
    ("obs_llm_usage", _OBS_LLM_USAGE_DDL),
    ("obs_llm_usage_archive", _OBS_LLM_USAGE_ARCHIVE_DDL),
])


# ===== 可观测性告警中心（文档 §9.1）=====
# 规则全局共享（平台配置），事件按维度（dim_key）记录；用户面只看 level='user' 且
# 落在自己可见仓库范围内的事件 —— 平台级规则/事件不进用户面（§5.3 三层边界）。
_NEW_TABLES.extend([
    ("obs_alert_rules", _OBS_ALERT_DDL),
    ("obs_alert_events", _OBS_ALERT_EVENTS_DDL),
])

# ===== 设备资源采样（资源曲线用；runtime_credentials 只有当前值，没有历史）=====
# ===== 业务指标分钟/小时聚合（P1-2，§6.2）=====
# 写入方：main.py 的 rollup 循环（60s，追赶式补齐）；读取方：总览趋势 / 指标序列 API / 后续 SLO。
# 维度：repo:<repo_key> 与 global（global 只在 L2 暴露）。设备资源仍走 obs_runtime_samples。
_NEW_TABLES.extend([
    ("obs_runtime_samples", _OBS_RUNTIME_SAMPLES_DDL),
    ("obs_metric_rollup", _OBS_METRIC_ROLLUP_DDL),
])

# ===== SLO / 错误预算（P2-1，§6.7）=====
# 配置表：三类 SLI（任务成功率 / 排队时延 / 引擎可用率）；目标值由管理台统一设定（L2 写、L1 读）。
# 快照表：按小时落一行（唯一 (slo_id, bucket_start)），供燃尽曲线与历史回溯。
_NEW_TABLES.extend([
    ("obs_slo", _OBS_SLO_DDL),
    ("obs_slo_snapshots", _OBS_SLO_SNAPSHOTS_DDL),
])

# ===== 可观测性日志（热数据落库，§6.3）=====
# 只落 WARNING/ERROR（INFO 走文件）；14 天留存。dedup_key 防 worker 批量重试重复落行
# （设计 DDL 无该列，本仓实现多加一列并记入文档：批量上报天然会重试）。
_NEW_TABLES.extend([
    ("obs_log", _OBS_LOG_DDL),
])

# ===== webhook 验签拒绝事实（Issue #3750）=====
# 403 token mismatch 的每次拒绝落一行，供「同仓库 mismatch 速率」告警与
# (ip, token_fp) 反查残留旧 secret；只存 sha256 短指纹，不存明文/前缀。
_NEW_TABLES.extend([
    ("obs_webhook_rejects", _OBS_WEBHOOK_REJECTS_DDL),
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
    """为核心表幂等添加 org_id 列（''=全局/未归属哨兵，D105 第三批 org 域 TEXT 化）。

    哨兵由 0 改为 ''：MySQL TEXT/VARCHAR 列与数值 0 比较会把非数字串强转 0
    （'abc'=0 为 TRUE），未归属行会被误匹配；空串在两种后端语义一致。

    ⚠️ 本函数属**冻结老轨**（`_run_all_migrations()`，存量库不执行），只补**缺失**
    列，不转换存量类型：老库里的 INT org_id 由注册式迁移
    `d105-org-domain-text-normalize`（version=_ORG_TEXT_MIGRATION_VERSION）
    负责 MODIFY 成字符串族，并在转换后自查列类型（QA M2：检查必须挂在会真正
    在存量库执行的注册式迁移上，不能挂这里）。
    """
    try:
        with db_lock:
            c = db.cursor()
            for table in _ORG_ID_TABLES:
                _ensure_column(c, table, "org_id", "TEXT DEFAULT ''")
            db.commit()
            log.info("org_id columns ensured on all core tables")
    except Exception as e:
        log.warning(f"org_id column migration failed: {e}")


_CONFIG_DIR = os.path.join(os.path.dirname(__file__), "..", "..", "configs")

# (seed_key, json_filename, actual_table_name)
_SEED_FILES = [
    ("agent_type", "agent-description.json", "agent_type"),
]


def _create_tables():
    """幂等创建新表"""
    for name, ddl in _NEW_TABLES:
        if _owned_skip(name, "create table"):
            continue
        try:
            with db_lock:
                db.execute(ddl)
                db.commit()
            log.info(f"Table '{name}' ready")
        except Exception as e:
            log.warning(f"Create table '{name}' failed: {e}")

    # IM 渠道 4 表（Issue #742 §5.1）：DDL 与引擎启动迁移共用 scripts/migrations/001_channel_tables，
    # 在此统一建/补（幂等），保证 MySQL 后端下即使 IM 引擎模式为 off 也表示层完整（P1 回填/对账依赖）。
    # Issue #2261：通知 outbox 新增列（004_notify_outbox）也挂在这里——四进程都会跑 db_setup，
    # 而 run_startup_migration 只在 A 进程 start_im_engine(mode=on) 时跑；挂在引擎启动上会让
    # mode=off / A 未起时旧库缺列，B/C 进程入队直接异常（通知静默丢失）。
    # 2026-11-14 修复：两 ensure 各自独立 try——此前同一 try 里 ensure_channel_schema 先炸
    # （MariaDB 1064：函数索引语法 (COALESCE(...)) 不支持），ensure_notify_outbox_schema
    # 连带没跑，outbox 列一直缺失 → OutboxConsumer 每秒 1054（4p E2E 实证）。
    # 建表/补列互不阻断：任一失败只丢自己的防御，不再级联。
    try:
        from src.domains.runtime.im.engine_wiring import ensure_channel_schema, ensure_notify_outbox_schema
        from src.kernel.table_ownership import db_name_domain
        _dom = db_name_domain()
        # 渠道族归属 mgmt：只有 owner 进程（及单库/无法判定域）建表与补列。
        # 三库模式下 runtime/insight 跳过，避免每个进程各建一份同名基表（旧行为已造成重复基表）。
        with db_lock:
            if _dom in (None, "mgmt"):
                ensure_channel_schema(db)
                ensure_notify_outbox_schema(db)
            db.commit()
        if _dom in (None, "mgmt"):
            log.info("IM channel tables ready "
                     "(grape_t_channel_installation / session_binding / inbound_dedup / sender_identity)")
        else:
            log.info(f"IM channel tables owned by mgmt, skipped in domain={_dom}")
    except Exception as e:
        log.warning(f"IM channel tables setup failed: {e}")
    try:
        from src.domains.runtime.im.engine_wiring import ensure_notify_outbox_schema
        with db_lock:
            ensure_notify_outbox_schema(db)
            db.commit()
        log.info("IM notify outbox columns ready (grape_t_channel_outbound_card)")
    except Exception as e:
        log.warning(f"IM notify outbox setup failed: {e}")

    # grape_accounts email 唯一索引（D12: 统一用 NULL + 普通唯一索引，SQLite 和 MySQL 统一语法）
    # 幂等补列：verification_codes.ip（老库可能缺此列）
    try:
        with db_lock:
            c = db.cursor()
            _ensure_column(c, "verification_codes", "ip", "VARCHAR(64) DEFAULT ''")
            db.commit()
    except Exception as e:
        log.warning(f"Ensure verification_codes.ip column failed: {e}")

    # D11: 幂等补列 grape_accounts.last_login_at + email_verified
    try:
        with db_lock:
            c = db.cursor()
            _ensure_column(c, "plugin", "org_id", "INTEGER DEFAULT 0")  # #2423 M2b/#2544：组织隔离维度
            _ensure_column(c, "grape_accounts", "last_login_at", "TEXT DEFAULT NULL")
            _ensure_column(c, "grape_accounts", "email_verified", "INTEGER DEFAULT 0")
            _ensure_column(c, "grape_accounts", "email_source", "VARCHAR(16) DEFAULT NULL")
            _ensure_column(c, "grape_accounts", "email_is_placeholder", "INTEGER DEFAULT 0")
            _ensure_column(c, "grape_accounts", "created_by_oauth", "INTEGER DEFAULT 0")
            db.commit()
    except Exception as e:
        log.warning(f"Ensure grape_accounts columns failed: {e}")

    # D22: 幂等补列 external_identities.status + unbound_at（软删除/恢复绑定）
    # Issue #344: 补 token_expires_at 列（#343 新增字段，早期建表可能缺失）
    try:
        with db_lock:
            c = db.cursor()
            _ensure_column(c, "external_identities", "status", "INTEGER DEFAULT 1")
            _ensure_column(c, "external_identities", "unbound_at", "TEXT DEFAULT NULL")
            _ensure_column(c, "external_identities", "token_expires_at", "TEXT DEFAULT ''")
            db.commit()
    except Exception as e:
        log.warning(f"Ensure external_identities columns failed: {e}")

    # 修复 email 列 Default：MySQL 可能把 VARCHAR 默认设为空字符串而非 NULL，
    # 导致绕过代码直接 INSERT 时多个空串触发唯一冲突。
    # 幂等 ALTER MODIFY 把 Default 改回 NULL（MySQL 专用，SQLite 不支持 MODIFY）。
    # Issue #536：MODIFY 属破坏性 DDL，统一走 _destructive_ddl 收口。
    try:
        from src.kernel.mysql_compat import get_db_backend
        if get_db_backend() == "mysql":
            with db_lock:
                c = db.cursor()
                _ok = _destructive_ddl(
                    c, mgmt_mig.ALTER_GRAPE_ACCOUNTS,
                    kind_hint="cleanup")
                db.commit()
                if _ok:
                    log.info("grape_accounts.email Default corrected to NULL")
    except Exception as e:
        log.warning(f"Fix email Default failed (may be SQLite, expected): {e}")

    _index_sqls = [
        # verification_codes 索引
        "CREATE INDEX IF NOT EXISTS idx_vc_email_scene "
        "ON verification_codes(email, scene, used, expires_at)",
        "CREATE INDEX IF NOT EXISTS idx_vc_ip_time "
        "ON verification_codes(ip, created_at)",
        # login_failures 索引
        "CREATE INDEX IF NOT EXISTS idx_lf_identifier_time "
        "ON login_failures(identifier, created_at)",
        "CREATE INDEX IF NOT EXISTS idx_lf_ip_time "
        "ON login_failures(ip, created_at)",

        # grape_t_credential_reveal_log 索引：高频取回按 (用户,时间) 计数
        "CREATE INDEX IF NOT EXISTS idx_crl_user_time "
        "ON grape_t_credential_reveal_log(user_id, created_at)",
        # audit_logs 索引（seq UNIQUE 约束——集群并发安全，DDL 中已有 UNIQUE，此索引兼容已有表）
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_audit_seq "
        "ON audit_logs(seq)",
        # grape_accounts 的 email 唯一索引不在这里建（见循环后的
        # `_ensure_active_email_index`）：Issue #3469 O 起它必须是部分索引，
        # 而 MySQL 分支要走生成列等价实现，两栈共用一份建索引/复查代码。
        # obs_llm_usage 查询索引（任务/时间/模型/仓库四个下钻维度）
        "CREATE INDEX IF NOT EXISTS idx_obs_llm_task ON obs_llm_usage(task_id)",
        "CREATE INDEX IF NOT EXISTS idx_obs_llm_time ON obs_llm_usage(created_at)",
        "CREATE INDEX IF NOT EXISTS idx_obs_llm_model ON obs_llm_usage(model_key, created_at)",
        "CREATE INDEX IF NOT EXISTS idx_obs_llm_repo ON obs_llm_usage(repo_key, created_at)",
        "CREATE INDEX IF NOT EXISTS idx_obs_llm_run ON obs_llm_usage(run_kind, run_id)",
        "CREATE INDEX IF NOT EXISTS idx_obs_llm_arc_time ON obs_llm_usage_archive(created_at)",
        "CREATE INDEX IF NOT EXISTS idx_obs_llm_arc_task ON obs_llm_usage_archive(task_id)",
        # obs_alert_events：开放事件按 (规则,维度) 查找 + 用户面按仓库/时间列表
        "CREATE INDEX IF NOT EXISTS idx_obs_alert_open ON obs_alert_events(rule_id, dim_key, status)",
        "CREATE INDEX IF NOT EXISTS idx_obs_alert_repo ON obs_alert_events(repo_key, last_seen_at)",
        "CREATE INDEX IF NOT EXISTS idx_obs_alert_status ON obs_alert_events(status, last_seen_at)",
        # obs_runtime_samples：资源曲线按 (引擎, 时间) 取；SLO 可用率按时间扫 online
        "CREATE INDEX IF NOT EXISTS idx_obs_sample_rt ON obs_runtime_samples(runtime_id, sampled_at)",
        "CREATE INDEX IF NOT EXISTS idx_obs_sample_time ON obs_runtime_samples(sampled_at)",
        # Issue #3580：webhook 收件箱消费查询（status + 到期时间 + 租约）走索引，避免 1s 轮询全表扫
        "CREATE INDEX IF NOT EXISTS idx_webhook_inbox_claim "
        "ON grape_t_webhook_inbox(status, next_attempt_at, lease_expires_at)",
        # obs_slo / obs_slo_snapshots：燃尽曲线按 (SLO, 桶) 取
        "CREATE INDEX IF NOT EXISTS idx_obs_slo_enabled ON obs_slo(enabled, scope_type)",
        "CREATE INDEX IF NOT EXISTS idx_obs_slo_snap ON obs_slo_snapshots(slo_id, bucket_start)",
        # obs_metric_rollup：序列查询按 (指标, 分辨率, 桶) + 用户面按仓库过滤
        "CREATE INDEX IF NOT EXISTS idx_obs_rollup_metric ON obs_metric_rollup(metric, resolution, bucket_start)",
        "CREATE INDEX IF NOT EXISTS idx_obs_rollup_repo ON obs_metric_rollup(repo_key, bucket_start)",
        # obs_log：检索维度（时间/级别/来源/任务/仓库）
        "CREATE INDEX IF NOT EXISTS idx_obs_log_ts ON obs_log(ts)",
        "CREATE INDEX IF NOT EXISTS idx_obs_log_level_ts ON obs_log(level, ts)",
        "CREATE INDEX IF NOT EXISTS idx_obs_log_task ON obs_log(task_id)",
        "CREATE INDEX IF NOT EXISTS idx_obs_log_repo_ts ON obs_log(repo_key, ts)",
    ]

    for idx_sql in _index_sqls:
        try:
            with db_lock:
                db.execute(idx_sql)
                db.commit()
        except Exception as e:
            log.warning(f"Create index failed: {e}")

    # Issue #3469 O：grape_accounts 的 email 唯一索引（新装库口径）。
    # 不能用 `CREATE UNIQUE INDEX IF NOT EXISTS ... WHERE`——mysql_compat 会把 WHERE
    # 丢掉建成**全列**唯一，正是本工单要修掉的旧形态。两栈共用一份实现，保证
    # 「新装库」与「升级库（迁移 202611190101）」终态一致。
    # 失败必须炸开（唯一约束缺失 = 活跃用户可重复邮箱，不能静默）；调用方
    # `_run_all_migrations` 有兜底 try/except，不会把启动挂死。
    # 域归属守卫：三库模式下 grape_accounts 只在 mgmt 是基表，别家域跳过（同建表口径）。
    if not _owned_skip("grape_accounts", "ensure active email index"):
        try:
            with db_lock:
                c = db.cursor()
                _stats = ensure_active_email_index(c)
                db.commit()
            log.info(f"grape_accounts uq_active_email ready: {_stats}")
        except Exception as e:
            log.error(f"grape_accounts uq_active_email 建立/复查失败（新装库口径）: {e}")
            raise


def _mysql_backend() -> bool:
    """是否 MySQL 后端（ALGORITHM / SET SESSION 仅 MySQL 下发；SQLite 路径不变）。"""
    try:
        from src.kernel.mysql_compat import get_db_backend
        return get_db_backend() == "mysql"
    except Exception:
        return False


# Issue #536: _ensure_column 两级降级都失败时的人工处理清单（启动结束打 ERROR 汇总）
_DDL_MANUAL_REVIEW: List[str] = []

# Issue #536 QA R3：legacy-stamp 首跑中被「min_code_version 门槛拦截 / MDL 超时」
# 而未执行的 DDL 清单。非空时执行器**不打 legacy-stamp 帽**（版本表保持为空），
# 保证被拦截的破坏性 DDL 在升级后下次启动重跑补执行，而非永久跳过。
_LEGACY_BLOCKED_DDL: List[str] = []


# DDL 安全带的纯函数/异常已下沉 kernel（PR #2009 I1）：db_setup 保留同名别名，
# 既有调用点、测试与「执行器捕获本模块异常类」的约定都不变。
_strip_null_default = _ddl_safety.strip_null_default
_strip_text_default = _ddl_safety.strip_text_default
_NonOwnerObject = _ddl_safety.NonOwnerObject


def _base_table_present(c, tbl: str) -> bool:
    """当前库里 tbl 是否为**基表**（MySQL 只认 BASE TABLE；SQLite 只认 table）。

    2026-11-20（审计 F2）：`_fn_rename_org_tables_and_uuid_pk` 对六张 org 表无条件跑
    `UuidPkMigration(...).run()`。三库模式下 runtime/insight 库里既没有这些表、看到的还是
    跨库只读视图 → 1146/1347 → 执行器按「异常=不盖章」跳过 → 每轮启动重试、该域台账永不收敛。
    迁移只应处理「本域库里的基表」。探针自身异常按存在处理（fail-safe：宁可让 DDL 自己报错，
    也不要把迁移静默跳过，否则又会变成「跳过却盖章」）。
    """
    try:
        if _mysql_backend():
            return bool(c.execute(
                "SELECT COUNT(*) FROM information_schema.tables "
                "WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME=%s AND TABLE_TYPE='BASE TABLE'",
                (tbl,)).fetchone()[0])
        return bool(c.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (tbl,)).fetchone())
    except Exception:  # noqa: BLE001
        return True


def _is_base_table(c, tbl: str) -> bool:
    """当前库中 tbl 是否为基表（MySQL 查 information_schema；SQLite 恒 True）。

    库按域拆分后，非 owner 角色看到的是跨库只读视图：视图不能 ALTER，且其列是
    **建视图时**的快照。此时补列必须由 owner 库完成并重建视图，不能在本库盲试
    ALTER（MySQL 报 1347 "'xxx' is not BASE TABLE"）。
    """
    if not _mysql_backend():
        return True
    try:
        row = c.execute(
            "SELECT table_type FROM information_schema.tables "
            "WHERE table_schema=DATABASE() AND table_name=?",
            (tbl,),
        ).fetchone()
        if not row:
            return True   # 查不到（mock/临时表）→ 按基表处理，保持既有行为
        return str(row[0]).upper() == "BASE TABLE"
    except Exception:
        return True   # 查询失败按基表处理，保持既有行为


def _owned_skip(table: str, what: str = "") -> bool:
    """表归属守卫：别家域的表不建/不改（防止拆库后四进程各建一份全量表）。

    见 src/kernel/table_ownership.py；`GRAPE_DDL_OWNER_GUARD=0` 可关闭。
    守卫自身异常一律放行（不能因守卫故障阻断启动）。
    """
    try:
        from src.kernel import table_ownership as _own
        owner = _own.foreign_owner(table)
        if owner:
            log.info(f"[DDL_SKIP] {what} `{table}`: owner={owner} current={_own.current_domain()}")
            return True
    except Exception as exc:  # noqa: BLE001
        log.warning(f"[DDL_SKIP] ownership guard error on `{table}`: {exc}")
    return False


def _migration_conn(domain: str, default=None):
    """迁移专用：返回**拥有域**的连接；三库模式不可用时回落进程库。

    背景：迁移注册表由 mgmt 进程统一跑，但 `_LEGACY_DOMAIN_LABELS` 只做了标注，
    执行器还没按域分派连接。只碰 runtime/insight 表的迁移在 mgmt 连接上必然
    1146 → 失败不盖章 → 每次启动重试。这里由迁移自己取 owner 连接。

    配套：用 `_ensure_column(..., force=True)` 绕过「别家域」DDL 守卫
    （守卫看的是**进程**归属，我们这里已经明确在 owner 库上操作）。
    """
    try:
        from src.kernel import db_registry
        # 只有在「该域确实有独立 DSN」时才切连接：单库/测试环境各域共用进程库，
        # 切过去反而脱离调用方（含单测）绑定的连接。
        if db_registry.lib_enabled() and db_registry.uses_own_dsn(domain) \
                and db_registry.current_domain() != domain:
            conn, _qualifier = db_registry.connection_for(domain)
            if conn is not None:
                return conn
    except Exception as e:  # noqa: BLE001
        log.warning(f"_migration_conn({domain}) 不可用，回落进程库: {e}")
    # default 让调用方把**函数内**导入的 db 传进来（单测会 monkeypatch
    # session_manager.db，模块级全局引用是旧对象，直接 return db 会绕过补丁）
    return default if default is not None else db


def _index_table(ddl: str) -> str:
    """从 ``CREATE [UNIQUE] INDEX ... ON <table>(...)`` 取表名。"""
    try:
        low = ddl.lower()
        i = low.find(" on ")
        if i < 0:
            return ""
        name = ddl[i + 4:].strip().split("(")[0].strip().strip('`"')
        return name.split()[0] if name else ""
    except Exception:  # noqa: BLE001
        return ""


def _ensure_column(c, tbl: str, col: str, ddl: str, allow_copy: bool = False,
                   strict: bool = False, force: bool = False,
                   raise_on_guard: bool = False):
    """Idempotent column add: PRAGMA check if column missing then ALTER.

    实现已下沉 `src.kernel.ddl_safety.ensure_column`（PR #2009 I1：域模块不再
    domains→api 反向依赖）；这里保留兼容包装，注入本模块的域守卫、人工处理清单、
    MDL 等待异常，既有调用点与测试（monkeypatch `_owned_skip`）语义不变。

    Column name wrapped in backticks for MySQL compatibility (e.g. `binary` is reserved).

    参数语义：
    - allow_copy=True（小表迁移专用）：INSTANT/INPLACE 都不支持时允许默认算法 COPY；
    - strict=True（注册式迁移专用）：所有降级分支失败时 raise，fn 抛错=不盖章；
    - raise_on_guard=True：域归属守卫跳过时 raise（默认保持历史静默 False）。
    """
    return _ddl_safety.ensure_column(
        c, tbl, col, ddl,
        allow_copy=allow_copy, strict=strict, force=force,
        raise_on_guard=raise_on_guard,
        guard=_owned_skip,
        is_base_table=_is_base_table,
        mysql_backend=_mysql_backend,
        manual_review=_DDL_MANUAL_REVIEW,
        blocked_ddl=_LEGACY_BLOCKED_DDL,
        non_owner_exc=_NonOwnerObject,
        mdl_exc=_MdlWaitTimeout,
        is_mdl_timeout_fn=_is_mdl_timeout,
    )


def _rename_column_if_exists(c, tbl: str, old: str, new: str):
    """幂等改名:旧列存在才处理(SQLite 3.25+).
    - 若 new 不存在:RENAME COLUMN(保数据)
    - 若 new 已存在(如 session_manager 模块级 ALTER 预建的兼容列)且 old 仍有数据:
      把 old 数据并入 new,再 DROP old,避免双列并存丢数据

    Issue #536：RENAME/DROP 属破坏性 DDL，统一经 _destructive_ddl() 收口
    （cleanup 上下文 + min_code_version 门槛），不再裸发。
    """
    cols = [r[1] for r in c.execute(f"PRAGMA table_info({tbl})").fetchall()]
    if old not in cols:
        return
    if new not in cols:
        _destructive_ddl(c, f"ALTER TABLE {tbl} RENAME COLUMN {old} TO {new}",
                         kind_hint="cleanup")
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
    _destructive_ddl(c, f"ALTER TABLE {tbl} DROP COLUMN {old}", kind_hint="cleanup")


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
        existing = c.execute(runtime_mig.SELECT_AGENT_RUNTIMES,
                             (name.lower() if name else name, scope or "global", owner or "")).fetchone()
        if existing:
            # 已存在(如 ensure 预置空行):补全缺失字段,保留其 id
            c.execute(runtime_mig.UPDATE_AGENT_RUNTIMES,
                      (display or name, 1 if installed else 0, cli or "", ver or "", 1 if healthy else 0, pip or "", updated or "", existing[0]))
        else:
            c.execute(runtime_mig.INSERT_IGNORE_AGENT_RUNTIMES,
                      (rid, name, display or name, 1 if installed else 0, cli or "", ver or "", 1 if healthy else 0, pip or "", scope or "global", owner or "", updated or ""))


def _create_indexes():
    """创建必要索引"""
    indexes = [
        "CREATE INDEX IF NOT EXISTS idx_events_repo_key ON events(repo_key)",
        "CREATE INDEX IF NOT EXISTS idx_events_issue_id ON events(issue_id)",
        "CREATE INDEX IF NOT EXISTS idx_events_status ON events(status)",
        # P2-B (Issue #357): Composite index for OR-pair queries (repo_key=? AND issue_id=?)
        # Eliminates full table scan in _batch_fetch_agent_statuses / _batch_fetch_pr_issue_keys
        "CREATE INDEX IF NOT EXISTS idx_events_repo_issue ON events(repo_key, issue_id)",
        "CREATE INDEX IF NOT EXISTS idx_agent_sessions_repo ON devops_agent_session(repo_key)",
        "CREATE INDEX IF NOT EXISTS idx_event_route_config_repo ON event_route_config(repo_key)",
        "CREATE INDEX IF NOT EXISTS idx_bot_seen_messages_app_id ON bot_seen_messages(app_id)",
        "CREATE INDEX IF NOT EXISTS idx_bot_seen_messages_seen_at ON bot_seen_messages(seen_at)",
        "CREATE INDEX IF NOT EXISTS idx_tasks_user_status ON tasks(user_id, status)",
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_tasks_repo_issue ON issue(repo_key, issue_number)",
        "CREATE INDEX IF NOT EXISTS idx_agent_tasks_status ON agent_tasks(status)",
        "CREATE INDEX IF NOT EXISTS idx_agent_tasks_runtime ON agent_tasks(runtime_id)",
        "CREATE INDEX IF NOT EXISTS idx_agent_tasks_runtime_status ON agent_tasks(runtime_id, status)",
        "CREATE INDEX IF NOT EXISTS idx_agent_tasks_session ON agent_tasks(agent_session_id)",
        "CREATE INDEX IF NOT EXISTS idx_agent_tasks_event ON agent_tasks(event_seq)",
        "CREATE INDEX IF NOT EXISTS idx_agent_tasks_issue_session ON agent_tasks(issue_session_id)",
# Issue #2090: 解绑/派发拦截按 repo_key + status 查询
        "CREATE INDEX IF NOT EXISTS idx_agent_tasks_repo_status ON agent_tasks(repo_key, status)",
        # ===== Issue #646: uid 稳定键索引（UNIQUE，NULL 多行合法——孤儿/历史行 uid=NULL 不冲突） =====
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_agent_runtimes_engine_uid ON agent_runtimes(engine_uid)",
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_runtime_credentials_credential_uid ON runtime_credentials(credential_uid)",
        "CREATE INDEX IF NOT EXISTS idx_agent_tasks_engine_uid ON agent_tasks(engine_uid)",
        "CREATE INDEX IF NOT EXISTS idx_agent_type_runtime_pool_engine_uid ON agent_type_runtime_pool(engine_uid)",
        "CREATE INDEX IF NOT EXISTS idx_engine_capabilities_engine_uid ON engine_capabilities(engine_uid)",
        "CREATE INDEX IF NOT EXISTS idx_task_messages_engine_uid ON task_messages(engine_uid)",
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
        # Issue #3750: webhook 验签拒绝按**时间窗口**计数（告警求值 + 14 天留存清理都是
        # `WHERE created_at >= ?` / `< ?` 且不带 repo_key 等值，故 created_at 必须在前；
        # repo_key 作覆盖列供 GROUP BY / 反查，放第二列不参与范围裁剪。反序
        # (repo_key, created_at) 对这种查询用不上索引，只能全表扫）
        "CREATE INDEX IF NOT EXISTS idx_obs_webhook_rejects_time_repo "
        "ON obs_webhook_rejects(created_at, repo_key)",
        # ===== Issue #103: 知识写入队列索引 =====
        "CREATE INDEX IF NOT EXISTS idx_kwq_status_retry ON knowledge_write_queue(queue_status, next_retry_at)",
        "CREATE INDEX IF NOT EXISTS idx_kwq_dim_created ON knowledge_write_queue(repo_key, scope, agent_type, issue_num, created_at)",
        "CREATE INDEX IF NOT EXISTS idx_kwq_source_seq ON knowledge_write_queue(repo_key, source_seq)",
        # ===== Issue #150: Autopilot 索引 =====
        "CREATE INDEX IF NOT EXISTS idx_autopilot_tasks_enabled ON autopilot_tasks(enabled)",
        "CREATE INDEX IF NOT EXISTS idx_autopilot_tasks_repo ON autopilot_tasks(repo_key)",
        "CREATE INDEX IF NOT EXISTS idx_autopilot_exec_task ON autopilot_executions(task_id)",
        "CREATE INDEX IF NOT EXISTS idx_autopilot_exec_status ON autopilot_executions(status)",
        # FR-16.1/FR-20.1: chat_conversations 部分唯一索引——issue 绑定的任务会话防双主并发双行
        # FR-20.1: issue_number > 0 替代 != ''（SQLite 类型亲和性使 0 != '' 为 true，误拦 general 会话）
        # FR-20.15: 补 conv_type 列，限定 conv_type='task' 只约束任务会话
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_chat_conv_repo_issue "
        "ON chat_conversations (user_id, repo_key, issue_number, conv_type) "
        "WHERE issue_number > 0 AND repo_key != '' AND conv_type = 'task'",
        # sweeper 的 chat 目录 TTL 清理每轮都跑 SELECT id FROM chat_conversations WHERE status='active'，
        # 原先无索引=全表扫（见 docs/sweeper-lock-decoupling.md「后续可做」）。
        # 这里只写 SQLite 通用语法：MySQL 的 TEXT 列建索引必须带前缀长度，由
        # mysql_compat 在 CREATE INDEX 特判里自动补 (191)，调用方不要再写 (32)（会被剥掉，
        # 且绕过兼容层直连 MySQL 会报 1170）。
        "CREATE INDEX IF NOT EXISTS idx_chat_conv_status ON chat_conversations(status)",
    ]
    # autopilot_tasks.created_by 迁移
    # 2026-11-14 E2E 实测修正：新基线 user_accounts.id 已 UUID 化（36 字符），
    # created_by INTEGER 在 MariaDB 上写入 UUID 必炸 "Data truncated"
    # （4p E2E：POST /api/autopilot/tasks 500，操作者 UUID id 落库失败）。
    # DDL 同步改 TEXT；存量列走 MODIFY 收敛（幂等：已是 TEXT 时 MODIFY 无损重跑）。
    try:
        with db_lock:
            c = db.cursor()
            _ensure_column(c, "autopilot_tasks", "created_by", "TEXT DEFAULT ''")
            from src.kernel.session_manager import _is_mysql_backend
            if _is_mysql_backend():
                _destructive_ddl(c, "ALTER TABLE autopilot_tasks MODIFY COLUMN created_by TEXT", kind_hint="cleanup")
            db.commit()
    except Exception as e:
        log.error(f"autopilot_tasks created_by migration failed: {e}")
    # ===== issue 表 (repo_key, issue_number) 去重（保证下方唯一索引可建，2026-10-06）=====
    # 背景：ensure_issue_task 此前仅复用 active/processing 卡，awaiting_input/completed/cancelled 时
    # 后续事件会新建一行 → 同一平台 issue 多卡（需求中心并排出现多张卡）。此处兜底删除多余行，
    # 保留执行中最有意义的一张（processing > awaiting_input/active > pending_review > completed > cancelled，
    # 同级取 updated_at 最新、id 最小），随后创建唯一索引从根上防重。
    try:
        with db_lock:
            c = db.cursor()
            _dup_groups = c.execute(
                runtime_mig.SELECT_ISSUE).fetchall()
            _del = 0
            for _rk, _num in _dup_groups:
                _rows = c.execute(
                    runtime_mig.SELECT_ISSUE__CREATE_INDEXES,
                    (_rk, _num)).fetchall()
                if len(_rows) < 2:
                    continue
                def _pri(_st):
                    return {"processing": 5, "awaiting_input": 4, "active": 4, "pending_review": 3,
                            "completed": 2, "cancelled": 1}.get(_st or "", 0)
                _keep = max(_rows, key=lambda r: (_pri(r[1]), r[2] or "", r[0]))
                for _r in _rows:
                    if _r[0] != _keep[0]:
                        c.execute(runtime_mig.DELETE_ISSUE, (_r[0],))
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
                    # F9/1064 修复（双栈）：裸 DROP INDEX 仅 SQLite 合法，MySQL 需 ON 子句
                    from src.kernel.session_manager import _is_mysql_backend as _imb
                    if _imb():
                        c.execute("DROP INDEX idx_tasks_repo_issue ON issue")
                    else:
                        c.execute("DROP INDEX idx_tasks_repo_issue")
                    db.commit()
                    log.info("Dropped non-unique idx_tasks_repo_issue for unique re-create")
                    break
    except Exception as e:
        log.warning(f"ensure unique idx_tasks_repo_issue prep failed: {e}")

    for idx in indexes:
        _idx_tbl = _index_table(idx)
        if _idx_tbl and _owned_skip(_idx_tbl, "create index"):
            continue
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
                # F9 修复（双栈）：旧写法裸 CREATE 无前缀在 MySQL 1170 失败 →
                # DROP 成功 CREATE 失败，索引被删不重建。改为 IF NOT EXISTS 形态
                # （MySQL 走兼容层特判自动加 TEXT 前缀；SQLite 原生合法）。
                # DROP 的 ON 子句 MySQL 必须 / SQLite 不允许，按后端分支。
                from src.kernel.session_manager import _is_mysql_backend as _imb
                if _imb():
                    c.execute("DROP INDEX idx_tasks_repo_issue ON issue")
                else:
                    c.execute("DROP INDEX idx_tasks_repo_issue")
                c.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_tasks_repo_issue ON issue(repo_key, issue_number)")
                db.commit()
                log.info("Upgraded idx_tasks_repo_issue to UNIQUE")
    except Exception as e:
        log.warning(f"Upgrade idx_tasks_repo_issue to UNIQUE failed: {e}")


def _seed_from_json():
    """首次运行时从 JSON 种子文件导入数据"""
    with db_lock:
        c = db.cursor()
        c.execute(runtime_mig.SELECT_META)
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
            c.execute(runtime_mig.UPSERT_META__SEED_FROM_JSON,
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
                    mgmt_mig.INSERT_IGNORE_EVENT_ACTION_FILTER,
                    (evt, act, allow, actor, req_changes, block_changes, side_effect))
            except Exception as e:
                log.warning(f"event_action_filter seed failed ({evt}:{act}): {e}")
        db.commit()


# ── Issue #2091（上游 #10）：存量库补种 agent_type ─────────────────────────────
def _fn_seed_agent_types_from_config() -> None:
    """从 configs/agent-description.json 补种 agent_type（只补缺失行，不覆盖用户改过的行）。

    种子写入点在老轨 `_run_all_migrations()` 里，存量库（已盖 legacy-stamp）启动时
    **完全不执行**该函数 → 容器化部署（镜像原先还缺 configs/）时 agent_type 永远为空，
    `GET /api/agents/types` 返回空数组、Agent 详情 404「Agent 类型未配置」。

    幂等且不覆盖：逐行 `INSERT OR IGNORE`——已有 agent_type 行（用户可能改过
    display_name/icon）不动，只补缺失的。文件缺失只告警。
    """
    filepath = os.path.join(_CONFIG_DIR, "agent-description.json")
    if not os.path.exists(filepath):
        log.warning(f"seed-agent-types: {filepath} not found, skip "
                    f"(container image needs .dockerignore to whitelist configs seeds)")
        return
    try:
        with open(filepath, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception as e:
        log.warning(f"seed-agent-types: failed to read {filepath}: {e}")
        return
    if not isinstance(data, dict) or not data:
        log.warning(f"seed-agent-types: {filepath} empty or not a dict, skip")
        return

    inserted = 0
    # agent_type 归 runtime 域：三库模式下 mgmt 连接里没有该表（1146），须在 owner 连接上补种
    conn = _migration_conn("runtime")
    with (db_lock if conn is db else nullcontext()):
        c = conn.cursor()
        for agent_type, info in data.items():
            info = info or {}
            c.execute(
                runtime_mig.INSERT_IGNORE_AGENT_TYPE,
                (
                    agent_type,
                    info.get("name", agent_type),
                    info.get("description", ""),
                    info.get("icon", ""),
                    info.get("order_num", 0),
                    json.dumps(info.get("subscribe_events", []), ensure_ascii=False),
                    1 if info.get("enable", True) else 0,
                ),
            )
            inserted += c.rowcount if c.rowcount and c.rowcount > 0 else 0
        conn.commit()
        # Spec 要求：补种成功后同步 meta.seeded_keys（与 _seed_from_json 的老轨记账一致），
        # 供后续诊断/去重对账；已存在该 key 时不改写（首轨种子已记）。
        if inserted > 0:
            c.execute(runtime_mig.SELECT_META__FN_SEED_AGENT_TYPES_FROM_CON)
            row = c.fetchone()
            seeded = set(row[0].split(",")) if row and row[0] else set()
            if "agent_type" not in seeded:
                seeded.add("agent_type")
                c.execute(
                    runtime_mig.UPSERT_META__FN_SEED_AGENT_TYPES_FROM_CON,
                    (",".join(sorted(seeded)),),
                )
                conn.commit()
    log.info(f"seed-agent-types: upserted {inserted} agent type(s) from agent-description.json")


# ── Issue #2095（上游 #14）：存量库补种 event_action_filter 默认规则 ──────────────
def _fn_seed_event_action_filter_once() -> None:
    """存量库补种默认事件动作过滤规则；已有数据（含用户自定义）则跳过。

    老轨 `_run_all_migrations()` 在存量库（已盖 legacy-stamp）完全不执行，
    而 `_seed_event_action_filter()` 的调用点就在那里 → 升级后默认规则永久缺失，
    webhook 事件的放行/过滤/副作用行为与全新安装不一致且不报错（与 #2091 同源）。

    幂等：表里已有任何行就直接返回，不重灌（避免与用户自定义规则叠加）；
    真正写入由 `_seed_event_action_filter()` 的 INSERT OR IGNORE 完成。
    """
    with db_lock:
        c = db.cursor()
        try:
            c.execute(mgmt_mig.SELECT_EVENT_ACTION_FILTER__FN_SEED_EVENT_ACTION_FILTER)
            existing = c.fetchone()[0]
        except Exception as e:
            log.warning(f"seed-event-action-filter: count query failed, skip: {e}")
            return
    if existing > 0:
        log.info(f"seed-event-action-filter: table already has {existing} row(s), skip")
        return
    _seed_event_action_filter()
    with db_lock:
        c = db.cursor()
        c.execute(mgmt_mig.SELECT_EVENT_ACTION_FILTER)
        after = c.fetchone()[0]
    log.info(f"seed-event-action-filter: seeded {after} default rule(s)")


def _insert_seed_data(table: str, data: dict):
    """将 JSON 数据插入对应表"""
    with db_lock:
        c = db.cursor()
        if table == "event_route_config":
            # event-routing.json: {"routing": {...}, "repo_routing": {...}}
            for event_type, skill in data.get("routing", {}).items():
                c.execute(
                    mgmt_mig.INSERT_IGNORE_EVENT_ROUTE_CONFIG,
                    ("", event_type, skill)
                )
            for repo_key, routes in data.get("repo_routing", {}).items():
                for event_type, skill in routes.items():
                    c.execute(
                        mgmt_mig.INSERT_IGNORE_EVENT_ROUTE_CONFIG__INSERT_SEED_DATA,
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
                    runtime_mig.UPSERT_AGENT_TYPE,
                    (agent_type, display_name, description, icon, order_num, subscribe_events, enable)
                )

        elif table == "agent_registry":
            # agent-routing.json 已废弃(agent_registry 表移除),backend 统一用 backend_agents
            pass

        db.commit()


def _migrate_whitelist_from_file():
    """首次启动：如果 login_whitelist 表为空但 login-whitelist.json 存在，导入文件中的用户名（一次性）。"""
    try:
        from src.kernel.config import LOGIN_WHITELIST_PATH
        if not os.path.exists(LOGIN_WHITELIST_PATH):
            return
        with db_lock:
            c = db.cursor()
            count = c.execute(mgmt_mig.SELECT_LOGIN_WHITELIST).fetchone()[0]
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
                    # 2026-11-12 行式化：文件导入写新模式行（provider='gitcode'——
                    # login-whitelist.json 是 gitcode 时代产物）。未盖章老库兜底
                    # 路径上列可能尚未由注册式迁移补齐，INSERT 失败静默跳过
                    # （登录侧文件降级读仍覆盖，见 config.is_user_allowed）。
                    c.execute(mgmt_mig.INSERT_IGNORE_LOGIN_WHITELIST,
                              ("gitcode", username, "from file"))
                    imported += c.rowcount
                except Exception:
                    pass
            db.commit()
        if imported:
            log.info(f"Whitelist migrated from file: {imported} users")
    except Exception as e:
        log.warning(f"Whitelist file migration skipped: {e}")


def _fn_login_whitelist_row_columns() -> None:
    """2026-11-12 行式化（weekly-11）：login_whitelist 补 provider/username 两列
    + 新行大小写不敏感唯一索引（注册式迁移，存量库启动自动补列；幂等）。

    - 老列式列（gitcode/github/gitee_username）不动：读路径兜底（双读兼容）
    - 新行唯一性：SQLite 部分表达式索引（provider, LOWER(username)），
      MySQL 生成列 username_lower + UNIQUE(provider, username_lower)
      （复用 _ensure_case_insensitive_indexes 的生成列模式，不依赖 8.0.13 函数索引）
    - fn 失败要抛（strict=True）：列没建成不盖章，下次启动重试
    - 域拆分库（arch-weekly-11 四进程）：login_whitelist 归属 mgmt 基表，
      runtime/insight 侧是跨库视图，ALTER 必然 1346/1347（视图侧不能 ALTER）。
      视图场景直接跳过——与 add-agent-tasks-trace-context 迁移同款守卫。
    """
    with db_lock:
        c = db.cursor()
        if _mysql_backend():
            _row = c.execute(
                "SELECT TABLE_TYPE FROM information_schema.tables "
                "WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME='login_whitelist'").fetchone()
            if _row and str(_row[0]).upper() == "VIEW":
                log.info("login_whitelist 在本库为跨库视图，补列归属 owner 基表，跳过 ALTER")
                return
        _ensure_column(c, "login_whitelist", "provider", "VARCHAR(32) DEFAULT NULL", strict=True)
        _ensure_column(c, "login_whitelist", "username", "VARCHAR(128) DEFAULT ''", strict=True)
        if _mysql_backend():
            c.execute(
                "SELECT COUNT(*) FROM information_schema.columns "
                "WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME='login_whitelist' "
                "AND COLUMN_NAME='username_lower'")
            if c.fetchone()[0] == 0:
                c.execute(
                    mgmt_mig.ALTER_LOGIN_WHITELIST)
            c.execute(
                "SELECT COUNT(*) FROM information_schema.statistics "
                "WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME='login_whitelist' "
                "AND INDEX_NAME='uq_login_whitelist_row'")
            if c.fetchone()[0] == 0:
                # 老行 provider=NULL：MySQL 唯一索引 NULL 不参与约束，存量行零影响
                c.execute(
                    mgmt_mig.ALTER_LOGIN_WHITELIST__FN_LOGIN_WHITELIST_ROW_COLUM)
        else:
            # SQLite：部分表达式索引——仅约束新模式行（provider 非空且 username 非空串）
            c.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS uq_login_whitelist_row "
                "ON login_whitelist(provider, LOWER(username)) "
                "WHERE provider IS NOT NULL AND username != ''")
        db.commit()


def _init_scene_category_data():
    """初始化 scene_category 表的 6 条分类数据（幂等）。

    技术方案 §2.1：通用/移动开发/基础设施/技能开发/AI应用/内容创作
    """
    try:
        from src.domains.mgmt.user.db import get_meta_value, set_meta_value
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
                    runtime_mig.INSERT_IGNORE_SCENE_CATEGORY,
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
        from src.domains.mgmt.user.db import get_meta_value, set_meta_value
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
                    mgmt_mig.INSERT_IGNORE_SKILL_CATEGORY,
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
        # skill 归运行域、skill_category 归 mgmt：跨域子查询拆成「mgmt 取 slug→id 映射 → 本域更新」
        from src.domains.mgmt import public as _mgmt_pub_cat  # noqa: PLC0415
        from src.domains.runtime import public as _rt_pub_cat  # noqa: PLC0415

        updated = _rt_pub_cat.backfill_skill_category_id(_mgmt_pub_cat.skill_category_map())
        if updated:
            log.info(f"Backfilled skill category_id: {updated} rows")
    except Exception as e:
        log.warning(f"Backfill skill category_id failed: {e}")


def _init_scene_market_data():
    """初始化 scene_market 表的 8 条场景数据（幂等）。

    技术方案 §3.2：从现有 devops_pipeline_scene official 场景复制 nodes_json + 补充展示字段。
    """
    try:
        from src.domains.mgmt.user.db import get_meta_value, set_meta_value
        if get_meta_value("scene_market_seeded") == "1":
            return
        with db_lock:
            c = db.cursor()
            # 检查已有数据
            count = c.execute(mgmt_mig.SELECT_SCENE_MARKET__INIT_SCENE_MARKET_DATA_2).fetchone()[0]
            if count > 0:
                set_meta_value("scene_market_seeded", "1")
                return
            # 从 scene_market(source='official') 复制 nodes_json（官方场景模板已种入 market；旧流程兼容 devops_pipeline_scene official）
            scene_map = {}
            try:
                c.execute(mgmt_mig.SELECT_SCENE_MARKET__INIT_SCENE_MARKET_DATA)
                for r in c.fetchall():
                    scene_map[r[0]] = r
            except Exception:
                pass
            if not scene_map:
                try:
                    c.execute(mgmt_mig.SELECT_DEVOPS_PIPELINE_SCENE__INIT_SCENE_MARKET_DATA)
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
                    mgmt_mig.INSERT_IGNORE_SCENE_MARKET,
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
        from src.domains.mgmt.user.db import get_meta_value, set_meta_value
        with db_lock:
            c = db.cursor()
            # 回填：已有记录中 skill_id=0 的，从 skill 表查 id 补上
            c.execute(mgmt_mig.SELECT_SCENE_SKILL_REF)
            stale_rows = c.fetchall()
            for ref_id, skill_name in stale_rows:
                c.execute(
                    runtime_mig.SELECT_SKILL,
                    (skill_name,),
                )
                sk_row = c.fetchone()
                if sk_row:
                    c.execute(
                        mgmt_mig.UPDATE_SCENE_SKILL_REF,
                        (sk_row[0], ref_id),
                    )
            if stale_rows:
                db.commit()
                log.info(f"Backfilled {len(stale_rows)} scene_skill_ref.skill_id")

            # 首次初始化：未 seeded 时写入全部关联
            if get_meta_value("scene_skill_ref_seeded") == "1":
                return
            c.execute(mgmt_mig.SELECT_SCENE_MARKET)
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
                        runtime_mig.SELECT_SKILL__INIT_SCENE_SKILL_REF,
                        (skill_name,),
                    )
                    sk_row = c.fetchone()
                    if sk_row:
                        skill_id = sk_row[0]
                    c.execute(
                        mgmt_mig.INSERT_IGNORE_SCENE_SKILL_REF,
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
        from src.domains.mgmt.user.db import get_meta_value, set_meta_value
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
                    runtime_mig.UPDATE_SKILL,
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
    from src.domains.mgmt.user.db import get_meta_value, set_meta_value
    # 三库拓扑：admin_accounts 归 mgmt。runtime/insight 进程里这张表即便存在也是单库时代的
    # 空壳副本，种子会写进别人的库（2026-11-15 在 grape_stage_runtime 里就长出了 admin 行），
    # 随后被清理时又像是"丢数据"。非 owner 进程直接跳过。
    from src.kernel import table_ownership as _table_own
    if _table_own.should_skip("admin_accounts"):
        log.info("[seed] admin_accounts owner=mgmt，非本域进程跳过种子")
        return
    _default_pwd = os.environ.get("GRAPE_ADMIN_PASSWORD") or "admin123"
    try:
        with db_lock:
            c = db.cursor()
            c.execute(mgmt_mig.SELECT_ADMIN_ACCOUNTS)
            if c.fetchone()[0] == 0:
                _pw_hash = hashlib.sha256(_default_pwd.encode()).hexdigest()
                c.execute(mgmt_mig.INSERT_ADMIN_ACCOUNTS,
                          ("admin", _pw_hash))
                db.commit()
                _src = "GRAPE_ADMIN_PASSWORD env" if os.environ.get("GRAPE_ADMIN_PASSWORD") else "default admin123"
                log.info(f"Seeded default admin account: admin (password from {_src})")
                # Issue #452: 默认口令安全告警——使用默认密码时打 WARNING 提示改密
                if _default_pwd == "admin123":
                    log.warning(
                        "⚠️  SECURITY: Admin account using default password 'admin123'. "
                        "Set GRAPE_ADMIN_PASSWORD env var or change password after first login. "
                        "(meta admin_pwd_initial=1, frontend will prompt change on next login)"
                    )
        set_meta_value("admin_initialized", "1")
        if not get_meta_value("admin_pwd_initial"):
            set_meta_value("admin_pwd_initial", "1" if _default_pwd == "admin123" else "0")
    except Exception as e:
        log.warning(f"Seed admin account failed: {e}")


# ===== 迁移锁（2026-10-18 修复 + 采纳 MR#516 可取之处）=====
# GET_LOCK 是 MySQL **连接级**锁：获取与释放必须在同一条物理连接上。
# 旧路径借的是连接池的连接：SELECT 类语句执行后自动 commit 把连接还池
# （src/kernel/mysql_compat.py 的 _raw_cursor 借还逻辑），持锁连接留在池里 Sleep，
# 之后 RELEASE_LOCK 落在重借的另一条连接上（返回 0，静默失败）→ 锁泄漏：
# 下次启动（哪怕另一台主）在 GET_LOCK 上等满 timeout，实测 174/79 每次重启
# 都卡、需人工 KILL 解围。修复：锁全程绑一条**池外专用连接**
# （MySQLCompatDB.dedicated_connection），finally 先显式 RELEASE 再 close 兜底
# ——连接断开即由 MySQL 释放其上所有命名锁，进程崩溃也不泄漏。
_MIGRATE_LOCK_SLICE = 10                                      # 单次 GET_LOCK 分片（秒）：分片才能中途告警
_MIGRATE_LOCK_TIMEOUT_ENV = "GRAPE_MIGRATE_LOCK_TIMEOUT"      # 等锁总上限（秒，默认 60）
_MIGRATE_LOCK_WAIT_WARN_ENV = "GRAPE_MIGRATE_LOCK_WAIT_WARN"  # 等锁告警阈值（秒，默认 min(30, 上限/2)）


def _migrate_lock_timeout() -> float:
    """等锁总上限（秒），env 可配，默认 60（旧行为 300s 太长：被占时白卡 5 分钟）。"""
    try:
        v = float(os.environ.get(_MIGRATE_LOCK_TIMEOUT_ENV, "60") or 60)
        return v if v > 0 else 60.0
    except (TypeError, ValueError):
        return 60.0


def _migrate_lock_warn_threshold(total: float) -> float:
    """等锁中途告警阈值（秒）：env 可配，默认 min(30, max(5, 上限/2))。"""
    try:
        v = float(os.environ.get(_MIGRATE_LOCK_WAIT_WARN_ENV, "") or 0)
    except (TypeError, ValueError):
        v = 0.0
    if v <= 0:
        v = min(30.0, max(5.0, total / 2.0))
    return v


def _query_lock_holder_info(dconn, lock_name: str) -> str:
    """持锁连接的 processlist 信息（performance_schema 可用时；不可用返回空串）。

    ⚠️ 只取 LOCK_STATUS='GRANTED'：等锁方（PENDING 行）也在 metadata_locks 里，
    不过滤会把"正在等锁的人"误报成"持锁者"（2026-10-18 采纳 MR#516 的做法）。
    """
    try:
        cur = dconn.cursor()
        cur.execute(
            "SELECT t.PROCESSLIST_ID, t.PROCESSLIST_HOST, t.PROCESSLIST_USER, "
            "t.PROCESSLIST_DB, t.PROCESSLIST_TIME, t.PROCESSLIST_STATE "
            "FROM performance_schema.metadata_locks ml "
            "JOIN performance_schema.threads t ON ml.OWNER_THREAD_ID = t.THREAD_ID "
            "WHERE ml.OBJECT_TYPE = 'USER LEVEL LOCK' AND ml.OBJECT_NAME = %s "
            "AND ml.LOCK_STATUS = 'GRANTED'",
            (lock_name,),
        )
        rows = cur.fetchall() or []
        return "; ".join(
            "processlist_id=%s host=%s user=%s db=%s time=%ss state=%s"
            % (r[0], r[1], r[2], r[3], r[4], r[5]) for r in rows
        )
    except Exception:
        return ""


def _acquire_migrate_lock(dconn, name: str):
    """在专用连接上获取迁移锁：分片等待 + 中途阈值告警（采纳 MR#516）。

    返回 (acquired, waited)。总等待达上限仍未获锁 → (False, waited)，由调用方
    打 ERROR 后**继续迁移**（迁移幂等），并把持锁者信息留在日志里便于人工解围。
    """
    total = _migrate_lock_timeout()
    threshold = _migrate_lock_warn_threshold(total)
    waited = 0.0
    next_warn = threshold
    while waited < total:
        slice_s = min(_MIGRATE_LOCK_SLICE, total - waited)
        cur = dconn.cursor()
        cur.execute("SELECT GET_LOCK(%s, %s)", (name, int(slice_s)))
        row = cur.fetchone()
        acquired = bool(row and row[0] == 1)
        waited += slice_s
        if acquired:
            if waited > _MIGRATE_LOCK_SLICE:      # 有一次以上等待 → 记下等了多久
                log.info(f"migrate lock acquired: {name}（等待 {waited:.0f}s 后拿到）")
            else:
                log.info(f"migrate lock acquired: {name}（专用连接，finally 显式释放）")
            return True, waited
        if waited >= next_warn and waited < total:
            holder = _query_lock_holder_info(dconn, name)
            extra = f" holder: {holder}" if holder else " (performance_schema 不可用，无持锁者信息)"
            log.warning(f"migrate lock '{name}' 等待中 {waited:.0f}s（阈值 {threshold:.0f}s）{extra}")
            next_warn = waited + threshold
    return False, waited


# ===== Issue #536: 迁移版本化执行器 + DDL 安全带 =====
# 设计：docs/weekly-09/migration-versioning-and-ddl-safety-design.md §D1–D4

# 迁移 kind 语义：
#   safe         加列/加表/加索引——启动路径直接执行；
#   backfill     数据回填——fn 内部分批（LIMIT 1000 循环 + 每批独立事务），
#                单项失败跳过不阻断启动，幂等保证下次续跑；
#   cleanup      DROP/RENAME/MODIFY 等破坏性 DDL——受 meta 表 min_code_version
#                门槛（自身版本 ≥ 该值才放行，否则跳过不盖章下版本再试）；
#   legacy-stamp 存量 133 处历史迁移一次性盖帽，仅初始化时生成，无 fn。
MIGRATION_KINDS = ("safe", "backfill", "cleanup", "legacy-stamp")

_LEGACY_STAMP_VERSION = "202610300001"   # 存量 _run_all_migrations 的盖帽版本
_LEGACY_STAMP_NAME = "legacy-run-all-migrations"


@dataclass
class Migration:
    """一条注册进版本表执行器的迁移。"""
    version: str                       # '202611010001'（日期+序号，升序执行）
    name: str
    kind: str                          # MIGRATION_KINDS 之一
    fn: Optional[Callable[[], None]] = None   # legacy-stamp 无 fn
    # 这条迁移该由哪个域库执行（三库模式：各域进程只跑自己域的条目）。
    # 默认 mgmt = 历史行为；只碰 runtime 表的迁移必须标 ("runtime",)，否则
    # mgmt 进程会「加列被归属守卫跳过、预检又判未完成」永远不盖章。
    domains: tuple = ("mgmt",)
    # 执行后置校验（#3648）：fn 正常返回后、盖章前调用；不达标抛错 = 不盖章。
    # 同号不同名（跨分支 renumber 撞号）时也用它判定目标是否真的到位：
    # 校验通过 → 视为已执行；校验失败 → 重跑幂等 fn 再校验（禁止静默跳过）。
    # 类型放宽为 Callable：实际校验函数可带 conn=None 关键字（供测试注入）、
    # 可返回清单（如 verify_skill_classification_columns 返回列名列表），
    # Callable[[], None] 与实现不符（QA：类型声明失真）。
    post_check: Optional[Callable] = None

    def __post_init__(self):
        if self.kind not in MIGRATION_KINDS:
            raise ValueError(f"unknown migration kind: {self.kind}")
        if self.kind != "legacy-stamp" and not callable(self.fn):
            raise ValueError(f"migration {self.version} kind={self.kind} 缺 fn")


_MIGRATIONS: List[Migration] = []



# 🧊 老轨 _run_all_migrations() 的冻结快照（调用名清单）。
# 存量库不会跑老轨，任何新增补列/补表一律 register_migration（见该函数 docstring）。
# tests/api/test_migration_track_guard.py 用 AST 比对：本常量与函数体里的实际调用
# 集合必须一致 —— 有人往老轨塞新迁移，测试立刻红；确实要改动老轨（极少见）时，
# 先评估是否该改成注册式迁移，只有确认必须留老轨才同步改这里，并在 PR 里说明原因。
LEGACY_TRACK_FROZEN = (
    "_ensure_case_insensitive_indexes",
    # 67ed59fb4（arch-weekly-11 platformization）加进老轨但没同步本常量 → 守卫长期红。
    # 只做账面对齐：该调用本来就在执行，语义零变化。
    "_ensure_platform_storage_columns",
    "_migrate_a2a_idempotency_key",
    "_migrate_allow_signup_default",
    "_migrate_event_status_pending",
    "_migrate_issue_created_by",
    "_migrate_knowledge_dedup_and_ref",
    "_migrate_tables",
    "_migrate_whitelist_from_file",
)


# ===== D105 存量库类型归一（自 arch 线移植，2026-11-12）=====
# 口径：**DDL 是唯一权威**（本文件 _ACCOUNT_FK_USER_ID_COLS 对应列在 CREATE TABLE 里
# 已声明 TEXT/VARCHAR(36)），下面的归一函数只服务于两类场景：
#   1. 存量库（旧 DDL 声明 INTEGER / TEXT 混型）需要一次性重建或放宽；
#   2. 契约用例（tests/kernel/test_*_text_migration.py）直接调用函数验证行为。
# 因此这些函数**不注册成启动迁移**：自动注册会在每次启动对线上 MySQL 逐列 ALTER，
# 风险高于收益（线上三库已按 DDL 规整）。需要手工修复存量库时显式调用即可。

_ACCOUNT_FK_USER_ID_COLS = (
    ("chat_agent_session", "user_id"),
    ("user_credentials", "user_id"),
    ("managed_repos", "host_user_id"),
    ("user_repos", "user_id"),
    ("agent_tasks", "user_id"),
    ("chat_conversations", "user_id"),
    ("feishu_onboard_task", "user_id"),
    ("scene_market", "source_user_id"),
    ("scene_likes", "user_id"),
    ("skill_likes", "user_id"),
    ("session_api_tokens", "user_id"),
    ("api_tokens", "user_id"),
    ("user_feedback", "user_id"),
    ("autopilot_subscriptions", "user_id"),
    ("org_member", "user_id"),
    ("org_invitation", "invitee_user_id"),
    ("audit_logs", "user_id"),
    # 接管会话归属人（Issue: 接管 500 真因 —— 账号 id 已 uuid，INT 列 int() 崩）
    ("takeover_sessions", "user_id"),
    ("exchange_codes", "user_id"),
    ("binding_tickets", "initiating_user_id"),
    ("binding_tickets", "original_user_id"),
    ("community_posts", "user_id"),
    ("community_comments", "user_id"),
    ("community_post_likes", "user_id"),
    ("obs_llm_usage", "user_id"),
    ("obs_llm_usage_archive", "user_id"),
    ("grape_t_credential_reveal_log", "user_id"),
    ("grape_t_credential_usage_log", "user_id"),
    ("grape_t_credential_usage_log", "initiating_actor_id"),
    ("external_identities", "grape_user_id"),
    ("issue", "user_id"),
    ("issue", "scene_user_id"),
    # 202611091100 补录：D105 漏列（owner 为归属人 user_id、release_approved_by 为审批人
    # user_id——同为账号域 uuid 引用，INTEGER 亲和/INT 列对 uuid 同样混型不命中/写崩）
    ("grape_t_patrol_findings", "owner"),
    ("issue", "release_approved_by"),
    # 注：unified_user_mapping.dingtalk_user_id 由 202611091200 单独放宽（守卫测试
    # 锁定本清单 33 对；该列走 _widen_mysql_issue_state_scene_user_id 顺带处理）。
)



_ORG_DOMAIN_TEXT_COLS = (
    ("organization", "owner_id"),
    ("org_member", "org_id"),
    ("org_member", "invited_by"),
    ("org_invitation", "org_id"),
    ("org_invitation", "inviter_id"),
    *[(t, "org_id") for t in _ORG_ID_TABLES],
    # 组织下派/上下文表（本轮新增）：存量库旧 DDL 为 INTEGER org_id，随批 TEXT 化
    ("grape_t_org_dispatch", "org_id"),
    ("grape_t_org_context", "org_id"),
)



def _rebuild_table_text_columns(c, table, cols, text_cols) -> None:
    """任意表整表重建：把 text_cols 中各列亲和 INTEGER/NUMERIC → TEXT，数据保值拷贝。

    DDL 用 sqlite_master 原文改造（仅把目标列的 INTEGER 改 TEXT）——
    不能从 PRAGMA table_info 反推重建：表达式默认值（如 DEFAULT (datetime('now'))）在
    PRAGMA 里丢失括号，重建即语法错误（实锤踩过）。cols 为该表 PRAGMA table_info 全量行。
    """
    import re
    old = f"{table}__text_rebuild_old"
    c.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (table,))
    row = c.fetchone()
    ddl = (row[0] or "") if row else ""
    new_create = ddl
    for col in text_cols:
        # 列名可能带反引号/双引号/方括号（存量库建表风格不一，实锤：线上 issue 表 `parent_issue_number` INTEGER）
        _qmarks = chr(96) + chr(34) + chr(92) + "["
        _pat = "([" + _qmarks + "]?" + col + "[" + _qmarks + "]?\\s+)INTEGER\\b"
        new_create, _n = re.subn(_pat, lambda m: m.group(1) + "TEXT", new_create, count=1)
        if _n != 1:
            raise ValueError(f"{table}: DDL 中未找到 {col} INTEGER 列声明，重建中止")
    _destructive_ddl(c, f"DROP TABLE IF EXISTS {old}", kind_hint="cleanup")
    _destructive_ddl(c, f"ALTER TABLE {table} RENAME TO {old}", kind_hint="cleanup")
    c.execute(new_create)
    _names = ", ".join(r[1] for r in cols)
    _sel = ", ".join(f"CAST({col} AS TEXT)" if col in text_cols else col for col in (r[1] for r in cols))
    c.execute(f"INSERT INTO {table} ({_names}) SELECT {_sel} FROM {old}")
    # 重建该表的显式索引（sqlite_autoindex 之外；含 idx_tasks_repo_issue UNIQUE 等）。
    # 注意：RENAME 会把索引 SQL 里的表名改写为旧表名（带引号），重建前需替换回新表名
    _idx = c.execute("SELECT name, sql FROM sqlite_master "
                     f"WHERE type='index' AND tbl_name=? AND sql IS NOT NULL", (old,)).fetchall()
    _destructive_ddl(c, f"DROP TABLE {old}", kind_hint="cleanup")
    for _name, _sql in _idx:
        _sql = _sql.replace(f'"{old}"', f'"{table}"').replace(f"'{old}'", f"'{table}'")
        try:
            c.execute(_sql)
        except Exception:
            try:
                c.execute(f'DROP INDEX IF EXISTS "{_name}"')
                c.execute(_sql)
            except Exception:
                pass


def _normalize_account_fk_user_id_text(conn) -> int:
    """账号域 user_id 族外键列存量混型归一为 VARCHAR(36) 存储（见 _ACCOUNT_FK_USER_ID_COLS）。

    每列独立处理，幂等：亲和已 TEXT 且无 typeof != 'text' 行时零改动。
    亲和非 TEXT（存量库旧 DDL 声明 INTEGER）时整表重建改亲和——纯 CAST AS TEXT
    会被 INTEGER 亲和在落盘时翻回 integer（同 issue_number 迁移实锤口径）。
    表/列不存在（_ensure_column 兜底前、可选表未建）逐项跳过。
    MySQL 分支：INT→VARCHAR(36) 放宽（与 D105 0002 的 uid_widen 同口径，仅限本清单列）。
    """
    from src.kernel.mysql_compat import get_db_backend
    if get_db_backend() != "sqlite":
        return _widen_mysql_account_fk_user_id_cols()
    c = conn.cursor()
    changed = 0
    rebuilt = 0
    for table, col in _ACCOUNT_FK_USER_ID_COLS:
        _cols = c.execute(f"PRAGMA table_info({table})").fetchall()
        if not _cols:
            continue  # 表不存在（未建/已 DROP），跳过
        _decl = next((r[2] or "" for r in _cols if r[1] == col), None)
        if _decl is None:
            continue  # 列不存在（_ensure_column 兜底前），跳过
        _decl_u = _decl.strip().upper()
        if _decl_u == "TEXT" or any(_t in _decl_u for _t in ("CHAR", "CLOB")):
            # 已是字符串族亲和（TEXT / VARCHAR(36) 等，如 agent_tasks.user_id）——
            # 无需整表重建：_rebuild 的 DDL 改写只认 INTEGER 声明，遇 VARCHAR 列
            # 会 raise「DDL 中未找到 INTEGER 列声明」中止整条迁移（启动 warning
            # 且永不盖章）。直接 typeof 混型归一后跳过续跑。
            changed += c.execute(
                f"UPDATE {table} SET {col} = CAST({col} AS TEXT) "
                f"WHERE typeof({col}) != 'text'").rowcount
            continue
        _rebuild_table_text_columns(c, table, _cols, (col,))
        rebuilt += 1
        continue
    if rebuilt:
        return -1  # 重建路径（行数级改写不计，幂等由亲和判定保证）
    return changed or 0


def _widen_mysql_account_fk_user_id_cols() -> int:
    """MySQL 侧：清单内仍为 INT 族的列 ALTER MODIFY VARCHAR(36)（保值；NOT NULL 保留）。
    202611091200：VARCHAR(36) 列也纳入（VARCHAR(191)）——credential_usage_log.user_id 等
    新表 DDL 声明 VARCHAR(36)，MySQL 上非 VARCHAR（门禁 user_id 族不变式红）。
    幂等：已 varchar 跳过。与 D105 0002 的 information_schema 扫描同口径，但只限本清单
    （0002 按列名全库扫描覆盖不到 invitee/initiating/original_user_id）。"""
    from src.kernel.session_manager import db
    c = db.cursor()
    targets = []
    for table, col in _ACCOUNT_FK_USER_ID_COLS:
        c.execute(
            "SELECT IS_NULLABLE, DATA_TYPE FROM information_schema.columns "
            "WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME=%s AND COLUMN_NAME=%s "
            "AND (DATA_TYPE IN ('int','bigint','smallint','tinyint','mediumint') "
            "     OR DATA_TYPE = 'text')",
            (table, col))
        row = c.fetchone()
        if row:
            targets.append((table, col, row[0], row[1]))
    for table, col, nullable, dtype in targets:
        # INT→VARCHAR(36)（uuid 保值）；TEXT→VARCHAR(191)（平台 id/长引用留裕量）
        length = "191" if dtype == "text" else "36"
        suffix = " NOT NULL" if nullable == "NO" else ""
        c.execute(f"ALTER TABLE {table} MODIFY {col} VARCHAR({length}){suffix}")
    if targets:
        db.commit()
        log.info(f"account fk user_id widen: {len(targets)} col(s) -> VARCHAR (MySQL)")
    return len(targets)


def _rebuild_organization_text_pk(c, cols) -> None:
    """organization 整表重建：id INTEGER PRIMARY KEY AUTOINCREMENT → TEXT PRIMARY KEY，
    owner_id 同批 TEXT 化，存量值 CAST 保值拷贝（手法对齐 UuidPkMigration._auto_sqlite_plan，
    但作用于任意 conn——迁移函数收 conn 参数，不能依赖 session_manager 全局连接）。"""
    import re
    row = c.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='organization'"
    ).fetchone()
    ddl = (row[0] or "") if row else ""
    new_create, _n = re.subn(r"(\bid\s+)INTEGER PRIMARY KEY AUTOINCREMENT",
                             r"\1TEXT PRIMARY KEY", ddl, count=1)
    if _n != 1:
        raise ValueError("organization: DDL 无 INTEGER PRIMARY KEY AUTOINCREMENT，重建中止")
    text_cols = ("id", "owner_id")
    for col in text_cols:
        _qmarks = chr(96) + chr(34) + chr(92) + "["
        _pat = "([" + _qmarks + "]?" + col + "[" + _qmarks + "]?\\s+)INTEGER\\b"
        new_create, _n = re.subn(_pat, lambda m: m.group(1) + "TEXT", new_create, count=1)
    old = "organization__text_rebuild_old"
    c.execute("DROP TABLE IF EXISTS " + old)
    c.execute(mgmt_mig.ALTER_ORGANIZATION + old)
    c.execute(new_create)
    _names = ", ".join(r[1] for r in cols)
    _sel = ", ".join(f"CAST({col} AS TEXT)" if col in text_cols else col
                     for col in (r[1] for r in cols))
    c.execute(mgmt_mig.organization_insert_from(old, _names, _sel))
    _idx = c.execute("SELECT name, sql FROM sqlite_master "
                     "WHERE type='index' AND tbl_name=? AND sql IS NOT NULL", (old,)).fetchall()
    _destructive_ddl(c, f"DROP TABLE {old}", kind_hint="cleanup")
    for _name, _sql in _idx:
        _sql = _sql.replace(f'"{old}"', '"organization"').replace(f"'{old}'", "'organization'")
        try:
            c.execute(_sql)
        except Exception:
            try:
                c.execute(f'DROP INDEX IF EXISTS "{_name}"')
                c.execute(_sql)
            except Exception as e:
                log.warning(f"org text migration: index rebuild failed on organization.{_name}: {e}")


# MySQL 侧 org 域列的目标类型（D105 第三批 MySQL 分支）。
# 核心表 org_id 用 TEXT，与全新库 `ADD COLUMN org_id TEXT` 一致；
# organization.id 是主键、org_member 上还挂着 UNIQUE(org_id,user_id)，
# MySQL 不允许 TEXT 直接进索引/主键（1170），故这几个定长列用 VARCHAR(36)：
# org 域 id 在 D105 后是 uuid7 字符串，36 位够用且保值（存量 1 → '1'）。
_ORG_MYSQL_TEXT_COLS = (
    # (table, column, target_type)
    ("organization", "id", "VARCHAR(36)"),
    ("organization", "owner_id", "VARCHAR(36)"),
    ("org_member", "org_id", "VARCHAR(36)"),
    ("org_member", "invited_by", "VARCHAR(36)"),
    ("org_invitation", "org_id", "VARCHAR(36)"),
    ("org_invitation", "inviter_id", "VARCHAR(36)"),
) + tuple((_t, "org_id", "TEXT") for _t in _ORG_ID_TABLES)

# 版本号单点定义：登记处与日志/自检引用同一常量，避免两处字面量漂移（QA I3）。
_ORG_TEXT_MIGRATION_VERSION = "202611190100"


def _widen_mysql_org_ids(conn=None) -> int:
    """MySQL 侧 org 域列字符串化 + 哨兵 0 → ''（D105 第三批，升级库专用）。

    为什么需要本函数：`_migrate_add_org_id_columns()` 只在列**缺失**时
    `ADD COLUMN org_id TEXT`，对存量库已存在的 INT 列既不报错也不转换；
    而 SQLite 的整表重建分支（`_normalize_org_domain_text`）用的是 PRAGMA /
    sqlite_master，在 MySQL 上是 1064。结果：老 schema + 新代码的 MySQL 库，
    任何一次新建 runtime 凭据都因「'' 写 int 列」报
    `(1366, "Incorrect integer value: '' for column 'org_id'")`。

    口径与 DDL 一致（`_ORGANIZATION_DDL` / `_ORG_MEMBER_DDL` 等）：org 域 id 为
    字符串、'' 表示未归属/全局。存量整型值保值字符串化（1 → '1'，不重编号），
    因此不会与既有 org_id 行错位。本函数幂等：已是字符串族的列零改动。
    返回：实际 ALTER 的列数。

    注意：`MODIFY COLUMN` 在 MySQL 上是 COPY 算法，会锁写整表——迁移在启动时
    执行一次，大表（events / agent_tasks）请安排维护窗口或低峰期首启。
    """
    from src.kernel.session_manager import db as _pdb
    conn = conn if conn is not None else _pdb
    c = conn.cursor()
    altered = 0
    rewritten = 0
    view_tables = []
    for table, col, target in _ORG_MYSQL_TEXT_COLS:
        # 本库不是该表 owner 时，拿到的是**跨域可更新视图**（三库拓扑）：ALTER 必
        # 1346/1347，UPDATE 会穿透改写别域基表（架构守护 A11/B-1）。视图列是建
        # 视图时的快照，转换只能由 owner 域进程做——本域跳过，owner 域跑同一条
        # 登记迁移并盖章；视图可见性由 scripts/sync_domain_views.py 重建保证。
        c.execute("SELECT TABLE_TYPE FROM information_schema.TABLES "
                  "WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME=%s", (table,))
        trow = c.fetchone()
        if not trow:
            continue                      # 表不在本库（三库分域的另一半），跳过
        if str(trow[0]).upper() != "BASE TABLE":
            view_tables.append(table)
            continue
        c.execute(
            "SELECT IS_NULLABLE, DATA_TYPE, COLUMN_DEFAULT FROM information_schema.columns "
            "WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME=%s AND COLUMN_NAME=%s",
            (table, col))
        row = c.fetchone()
        if not row:
            continue                      # 表/列不在本库（三库分域的另一半），跳过
        nullable, dtype, default = row[0], (row[1] or "").lower(), row[2]
        if dtype not in ("char", "varchar", "tinytext", "text", "mediumtext", "longtext"):
            suffix = " NOT NULL" if nullable == "NO" else ""
            # 存量列的数值默认值（invited_by DEFAULT 0 一类）跟着列一起保留，
            # 否则未显式赋值的 INSERT 会从 0 变成 NULL，哨兵口径不一致。
            # TEXT/BLOB 在 MySQL 上只接受**表达式**默认值（DEFAULT ('')），
            # 字面量 DEFAULT '0' 会报 1101，所以只对 VARCHAR 目标保留默认值。
            if default not in (None, "") and target.lower().startswith("varchar"):
                suffix += f" DEFAULT '{default}'"
            # 危险 DDL 唯一入口（Issue #536 D4-2）：蓝绿共存期落后实例跳过，
            # 且跳过必须**不盖章**（raise），否则列永远停在 INT（QA M3）。
            if not _destructive_ddl(c, f"ALTER TABLE {table} MODIFY {col} {target}{suffix}"):
                raise RuntimeError(
                    f"org text normalize: ALTER {table}.{col} 被 cleanup 门槛拦下"
                    "（本实例版本落后于 meta.min_code_version）→ 本次不盖章，下版本重试")
            # _destructive_ddl 对非 MDL 异常只记 WARNING 仍返回 True（fail-open），
            # 所以「转换成功」必须自己复检列型，否则日志与计数会把失败读成成功（QA I1）。
            c.execute(
                "SELECT DATA_TYPE FROM information_schema.columns "
                "WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME=%s AND COLUMN_NAME=%s",
                (table, col))
            got = str((c.fetchone() or [""])[0] or "").lower()
            if got not in ("char", "varchar", "tinytext", "text", "mediumtext", "longtext"):
                log.error(f"org text normalize (MySQL): {table}.{col} ALTER 后仍为 {got or '?'}")
                raise RuntimeError(
                    f"org text normalize: {table}.{col} MODIFY 未生效（仍为 {got or '?'}）"
                    "→ 本次不盖章，下次启动重试")
            altered += 1
            log.info(f"org text normalize (MySQL): {table}.{col} {dtype} -> {target}{suffix}")
        # 哨兵统一：0 / '0' / NULL → ''（与 SQLite 分支 #③ 同口径）。
        # 先 LIMIT 1 探一下再写：已字符串化的库不让每次启动都全表 UPDATE 一遍
        # （events / agent_tasks 是大表，QA I3）。
        c.execute(f"SELECT 1 FROM {table} WHERE {col}='0' OR {col} IS NULL LIMIT 1")
        if c.fetchone():
            c.execute(f"UPDATE {table} SET {col}='' WHERE {col}='0' OR {col} IS NULL")
            rewritten += getattr(c, "rowcount", 0) or 0
    if view_tables:
        log.info("org text normalize (MySQL): 跳过 %d 张跨域视图 %s（非本域 owner，"
                 "由 owner 域进程转换，视图侧需 sync_domain_views 重建）",
                 len(set(view_tables)), sorted(set(view_tables)))
    if altered:
        log.info(f"org text normalize (MySQL): {altered} column(s) widened, "
                 f"{rewritten} sentinel row(s) rewritten")
    return altered


def _org_columns_not_text(conn=None) -> list:
    """当前库里仍是数值型的 org 域列清单（迁移后校验用，不做 DDL）。

    用途：注册式迁移 `d105-org-domain-text-normalize`（QA 命名）转换完成后自查——
    `_destructive_ddl()` 对非 MDL 异常**抛出**（fail-closed，迁移不盖章），
    「已达目标态」的重复/缺失错误码（1050/1051/1054/1060/1061/1091/1146）才放行；
    即便如此，「ALTER 是否真生效」仍应落到列类型实查。

    口径对称（QA I2）：两种后端都按 `_ORG_MYSQL_TEXT_COLS` 的同一份清单检查，
    organization.id 在 SQLite 分支同样纳入。
    """
    from src.kernel.mysql_compat import get_db_backend
    from src.kernel.session_manager import db as _pdb
    conn = conn if conn is not None else _pdb
    c = conn.cursor()
    bad = []
    if get_db_backend() == "mysql":
        for table, col, _target in _ORG_MYSQL_TEXT_COLS:
            # 只统计基表：非 owner 域库里这张表是跨域视图，列型是建视图时的快照，
            # 拿它判「转换未生效」会让该域永不盖章（在别域已转换的情况下反复 ERROR）。
            c.execute(
                "SELECT c.DATA_TYPE FROM information_schema.columns c "
                "JOIN information_schema.tables t "
                "  ON t.TABLE_SCHEMA=c.TABLE_SCHEMA AND t.TABLE_NAME=c.TABLE_NAME "
                "WHERE c.TABLE_SCHEMA=DATABASE() AND c.TABLE_NAME=%s "
                "  AND c.COLUMN_NAME=%s AND t.TABLE_TYPE='BASE TABLE'",
                (table, col))
            row = c.fetchone()
            if row and (row[0] or "").lower() not in (
                    "char", "varchar", "tinytext", "text", "mediumtext", "longtext"):
                bad.append((table, col, row[0]))
        return bad
    for table, col, _target in _ORG_MYSQL_TEXT_COLS:
        cols = c.execute(f"PRAGMA table_info({table})").fetchall()
        decl = next((r[2] or "" for r in cols if r[1] == col), None)
        if decl is not None and decl.strip().upper() not in ("TEXT",) \
                and "CHAR" not in decl.upper() and "CLOB" not in decl.upper():
            bad.append((table, col, decl))
    return bad


def _normalize_org_domain_text(conn) -> int:
    """org 域 id TEXT 归一：organization.id 主键 TEXT 化 + owner_id/
    org_id/inviter_id/invited_by/核心表 org_id 列 TEXT 亲和 + 哨兵 0 → ''。

    SQLite 分支整表重建（PRAGMA / sqlite_master）；MySQL 分支走
    `_widen_mysql_org_ids()`（information_schema + 列宽调整）。
    早期实现只有 SQLite 分支且在 MySQL 上直接 1064——升级库的 org_id 因此
    永远是 INT，新代码写 '' 哨兵时 1366（凭据生成失败）。

    幂等：id 已 TEXT、各列亲和已 TEXT 且无混型行、哨兵已 '' 时零改动。
    亲和非 TEXT（存量库旧 DDL）时整表重建——纯 CAST 会被 INTEGER 亲和翻回
    integer（202611091000 同口径实锤）。表/列不存在逐项跳过。
    返回：重建路径 -1；否则改写行数（MySQL 返回 ALTER 列数）。"""
    from src.kernel.mysql_compat import get_db_backend
    if get_db_backend() != "sqlite":
        return _widen_mysql_org_ids(conn)
    c = conn.cursor()
    changed = 0
    rebuilt = False

    # ① organization.id 主键（TEXT PK 重建，owner_id 随批）
    _cols = _ddl_safety.table_info_rows(c, "organization")
    if _cols:
        _decl = next((r[2] or "" for r in _cols if r[1] == "id"), None)
        if _decl is not None and _decl.strip().upper() != "TEXT":
            _rebuild_organization_text_pk(c, _cols)
            rebuilt = True

    # ② 引用列 TEXT 亲和归一（保值）
    for table, col in _ORG_DOMAIN_TEXT_COLS:
        _cols = _ddl_safety.table_info_rows(c, table)
        if not _cols:
            continue
        _decl = next((r[2] or "" for r in _cols if r[1] == col), None)
        if _decl is None:
            continue
        if _decl.strip().upper() != "TEXT":
            _rebuild_table_text_columns(c, table, _cols, (col,))
            rebuilt = True
            continue
        changed += c.execute(
            f"UPDATE {table} SET {col} = CAST({col} AS TEXT) "
            f"WHERE typeof({col}) != 'text'").rowcount

    # ③ 哨兵改写：0 → ''（''=全局/未归属；对齐 owner 列 ''=unowned 口径）
    for table, col in _ORG_SENTINEL_COLS:
        _cols = _ddl_safety.table_info_rows(c, table)
        if not _cols or not any(r[1] == col for r in _cols):
            continue
        changed += c.execute(
            f"UPDATE {table} SET {col}='' WHERE {col}='0' OR {col}=0").rowcount

    if rebuilt:
        return -1
    return changed or 0


def _mysql_view_base_table(c, table: str):
    """若 table 在 MySQL 里是视图，返回其基表 (schema, table)；否则 None。

    cross-schema DEFINER 视图上直接 ALTER 会报 1347（对视图名改列不允许），
    必须先落到真正的基表；DEFINITION 里解析 `from schema.table` 即可，
    解析不出时保守返回 None（调用方退化到原路径，不阻断启动）。
    """
    import re as _re_vbt
    try:
        c.execute(
            "SELECT TABLE_TYPE FROM information_schema.TABLES "
            "WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME=%s", (table,))
        row = c.fetchone()
        if not row or str(row[0] or "").upper() != "VIEW":
            return None
        c.execute(
            "SELECT VIEW_DEFINITION FROM information_schema.VIEWS "
            "WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME=%s", (table,))
        vdef = (c.fetchone() or [""])[0] or ""
        m = _re_vbt.search(r"\bfrom\s+`?(\w+)`?\.`?(\w+)`?", vdef, _re_vbt.I)
        if m:
            return (m.group(1), m.group(2))
        m = _re_vbt.search(r"\bfrom\s+`?(\w+)`?", vdef, _re_vbt.I)
        if m:
            return (None, m.group(1))
    except Exception:
        return None
    return None


def _rebuild_issue_ident_columns_integer(c, cols, ident_cols) -> None:
    """issue 表整表重建：把 ident_cols 各列亲和 TEXT → INTEGER（基线 #995 修复）。

    修复 202611090004 把 issue_number/parent_issue_number 归一为 TEXT 亲和后，
    数字 ident 读侧变 '30'（str）压掉基线 #995 的 int 契约。INTEGER 亲和下：
    - 数字值（int 或 '30'）落盘即 int，读侧恒 int（基线行为恢复）；
    - 非数字 ident（gitee IK*）按 SQLite 动态类型原样 text 存储，D105 域不受影响；
    - str 参数等值比较（WHERE issue_number=?）经 INTEGER 亲和无损转换仍命中。
    拷贝不 CAST：text '30' 写入 INTEGER 亲和列由亲和自动转 int。
    """
    import re
    table = "issue"
    old = f"{table}__ident_int_rebuild_old"
    c.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (table,))
    row = c.fetchone()
    ddl = (row[0] or "") if row else ""
    if not ddl:
        return
    new_create = ddl
    for col in ident_cols:
        _qmarks = chr(96) + chr(34) + chr(92) + "["
        _pat = "([" + _qmarks + "]?" + col + "[" + _qmarks + "]?\\s+)TEXT\\b"
        new_create, _n = re.subn(_pat, lambda m: m.group(1) + "INTEGER", new_create, count=1)
        if _n != 1:
            raise ValueError(f"{table}: DDL 中未找到 {col} TEXT 列声明，重建中止")
    _destructive_ddl(c, f"DROP TABLE IF EXISTS {old}", kind_hint="cleanup")
    _destructive_ddl(c, f"ALTER TABLE {table} RENAME TO {old}", kind_hint="cleanup")
    c.execute(new_create)
    _names = ", ".join(r[1] for r in cols)
    c.execute(f"INSERT INTO {table} ({_names}) SELECT {_names} FROM {old}")
    _idx = c.execute("SELECT name, sql FROM sqlite_master "
                     f"WHERE type='index' AND tbl_name=? AND sql IS NOT NULL", (old,)).fetchall()
    _destructive_ddl(c, f"DROP TABLE {old}", kind_hint="cleanup")
    for _name, _sql in _idx:
        _sql = _sql.replace(f'"{old}"', f'"{table}"').replace(f"'{old}'", f"'{table}'")
        try:
            c.execute(_sql)
        except Exception:
            try:
                c.execute(f'DROP INDEX IF EXISTS "{_name}"')
                c.execute(_sql)
            except Exception:
                pass


def _widen_mysql_issue_ident_cols() -> int:
    """MySQL 侧：issue.issue_number / parent_issue_number 存量库旧 DDL 为 INT 族时
    ALTER MODIFY VARCHAR(191) 放宽（202611091000 account-fk widen 同口径）。

    严格模式下 INT 列写 gitee 等字母 ident 必炸；VARCHAR 列恒 str 存储，
    天然无 SQLite typeof 混型病，无需逐行 UPDATE。选 VARCHAR(191) 而非 TEXT：
    两列均在索引内（idx_tasks_repo_issue / uq_issue_release_bind），TEXT 无前缀
    长度不能直接做索引键，MODIFY 会报 1170。幂等：已非 INT 族（text/varchar）
    跳过——新 DDL 库零改动。表不存在（首跑建表前）跳过。

    B2：`issue` 为视图（跨 schema DEFINER 视图）时解析基表再 ALTER——直接
    ALTER 视图名报 1347，迁移失败不盖章，INT 强转 gitee ident 落 0。当前连接
    无基表 ALTER 权限时异常上抛由迁移框架记录（有权限账号执行一次后幂等跳过）。"""
    from src.kernel.session_manager import db
    c = db.cursor()
    c.execute(
        "SELECT COUNT(*) FROM information_schema.tables "
        "WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME='issue'")
    if not c.fetchone()[0]:
        return 0  # MySQL path: issue 表未建（legacy 全量首跑会按新 DDL 整表建齐）
    c.execute("SELECT DATABASE()")
    _cur_db = (c.fetchone() or [""])[0]
    _base = _mysql_view_base_table(c, "issue")
    if _base:
        _bs, _bt = _base
        _sch = _bs or _cur_db
        _qual = f"`{_bs}`.`{_bt}`" if _bs else f"`{_bt}`"
        log.warning(f"issue ident widen: 'issue' is a VIEW -> base table {_qual} "
                    f"(cross-schema DEFINER view; ALTER on view name errors 1347)")
    else:
        _sch = _cur_db
        _bt = "issue"
        _qual = "`issue`"
    targets = []
    for col in _TEXT_IDENT_COLS:
        c.execute(
            "SELECT IS_NULLABLE FROM information_schema.columns "
            "WHERE TABLE_SCHEMA=%s AND TABLE_NAME=%s AND COLUMN_NAME=%s "
            "AND DATA_TYPE IN ('int','bigint','smallint','tinyint','mediumint')",
            (_sch, _bt, col))
        row = c.fetchone()
        if row:
            targets.append((col, row[0]))
    for col, nullable in targets:
        suffix = " NOT NULL" if nullable == "NO" else ""
        c.execute(f"ALTER TABLE {_qual} MODIFY {col} VARCHAR(191){suffix}")
    if targets:
        db.commit()
        log.info(f"issue ident widen: {len(targets)} col(s) -> VARCHAR(191) (MySQL)")
    return len(targets)


def _normalize_issue_number_text(conn) -> int:
    """issue-ident：issue.issue_number / issue.parent_issue_number 存量混型归一为 TEXT 存储。

    gitee 等平台 issue_number 为纯字母 ident，与数字字符串混存同一列后
    typeof 混型（int/text 混存）——SQL 等值比较跨类型永不命中，str 参数
    查不到数字存储行。归一后全列恒 text：str 参数直接命中，int 参数经
    TEXT 亲和自动转换后命中。

    parent_issue_number 同根因：子任务父引用可写 IK* 字符串（issues_center
    恒 str 写入），SQLite INTEGER 亲和列侥幸能存，MySQL 侧 INT 列严格模式
    必炸，一并纳入 TEXT 归一。

    关键口径：若列亲和非 TEXT（存量库旧 DDL 声明 INTEGER），纯
    CAST AS TEXT 会被 INTEGER 亲和在落盘时翻回 integer——必须整表重建
    把列亲和改为 TEXT 才能恒 text。新 DDL（202611090004 起）已声明 TEXT，
    只需 UPDATE 兜底 typeof 混型。幂等：亲和已 TEXT 且无混型行时零改动。
    MySQL 侧本函数不执行（入口提前分派 _widen_mysql_issue_ident_cols）。
    """
    from src.kernel.mysql_compat import get_db_backend
    if get_db_backend() != "sqlite":
        # MySQL path: 分派提到最前——先于任何 PRAGMA/typeof SQLite-only SQL
        return _widen_mysql_issue_ident_cols()
    c = conn.cursor()
    _cols = c.execute("PRAGMA table_info(issue)").fetchall()
    if not _cols:
        return 0  # 无 issue 表（首跑 _NEW_TABLES 前调用等），不处理
    changed = 0
    rebuilt = False
    for _col in _TEXT_IDENT_COLS:
        _decl = next((r[2] or "" for r in _cols if r[1] == _col), None)
        if _decl is None:
            continue  # 列不存在（_ensure_column 兜底前），不处理
        if _decl.strip().upper() in ("INTEGER", "INT", "BIGINT", "SMALLINT",
                                     "MEDIUMINT", "TINYINT"):
            # 基线 #995 口径：INTEGER 亲和——数字 ident 落盘即 int、读侧恒 int；
            # 非数字 ident（gitee IK*）动态类型原样 text，D105 域不受影响；
            # str 参数等值比较经 INTEGER 亲和无损转换仍命中数字行，无需 typeof 归一。
            continue
        # 亲和被 202611090004 归一为 TEXT（存量库）→ 整表重建回 INTEGER。
        _rebuild_issue_ident_columns_integer(c, _cols, (_col,))
        rebuilt = True
    if rebuilt:
        return -1  # 重建路径（行数级改写不计，幂等由亲和判定保证）
    return changed or 0


def _normalize_whitelist_null_literals(c):
    """login_whitelist 存量字面 'NULL' 占位清洗（D105d 59 RDS 演练发现）。

    存量库 github_username/gitcode_username 有历史导入留下的字面 'NULL' 字符串，
    大小写不敏感唯一索引的生成列把它当真实用户名 → 多行 'null' 撞 1062，
    CREATE UNIQUE INDEX 被 except 吞成 warning，唯一索引静默缺失。
    统一清成空串（空串在 SQLite 部分索引 WHERE != '' 与 MySQL NULLIF 两条路
    都不参与唯一约束）。UPDATE 基列会触发 STORED 生成列重算，旧装已建的
    脏生成列同样被治愈。幂等，可重复执行。
    """
    for col in ("github_username", "gitcode_username"):
        try:
            c.execute(mgmt_mig.login_whitelist_clear_null_literal(col))
        except Exception as e:
            log.warning(f"_normalize_whitelist_null_literals({col}) failed: {e}")


_ORG_SENTINEL_COLS = _ORG_DOMAIN_TEXT_COLS

_TEXT_IDENT_COLS = ("issue_number", "parent_issue_number")

# ── 存量迁移的域归属补正（review M1）─────────────────────────────────────────
# 早期迁移一律登记成默认的 ("mgmt",)，但其中不少只碰 runtime/insight 域的表。分库
# 模式下 mgmt 进程执行它们时 DDL 被归属守卫跳过、预检却判「已落库」→ 盖章假绿，
# 真正的列/索引从未真正核验过。这里按**实测拓扑**（information_schema：该表只在这
# 一个域库里存在）把归属补正，执行交给属主域进程。
# 只补「单库独存」的表；两库同时存在同名表的迁移（branch_guard_state /
# takeover_sessions / grape_t_system_credentials / grape_t_credential_usage_log /
# grape_t_repo_credentials）暂不动 —— 那是历史拆库留下的双份副本，需要先决定
# 唯一权威副本再改归属，不属于本 PR 的处理范围（已在 PR 评论里列为待决策项）。
_LEGACY_DOMAIN_LABELS = {
    # runtime_credentials / agent_runtimes / agent_tasks / issue / events /
    # chat_conversations / task_messages / autopilot_executions（均只在 runtime 库）
    "202611020003": ("runtime",),
    "202611040002": ("runtime",),
    "202611040003": ("runtime",),
    "202611060004": ("runtime",),
    "202611070001": ("runtime",),
    "202611080004": ("runtime",),
    "202611182015": ("runtime",),
    "202611100001": ("runtime",),
    "202611110004": ("runtime",),
    "202611110006": ("runtime",),
    "202611130001": ("runtime",),
    "202611140003": ("runtime",),
    "202611150007": ("runtime",),
    "202611150008": ("runtime",),
    "202611160007": ("runtime",),
    "202611160020": ("runtime",),
    "202611220001": ("runtime",),
    "202611170043": ("runtime",),   # #2925 issue.title_fetched_at（issue 表在 runtime 库）
    "202611170045": ("runtime",),   # runtime_credentials.cred_type/platform
    # obs_llm_usage（只在 insight 库）
    "202611030003": ("insight",),
}

def register_migration(version: str, name: str, kind: str,
                       fn: Optional[Callable[[], None]] = None,
                       domains: Optional[tuple] = None,
                       post_check: Optional[Callable] = None) -> Migration:
    """登记一条迁移（新增迁移一律走这里 + kind 规则）。

    ⚠️ 新增补列/补表/回填必须走这里，不要写进老轨 _run_all_migrations()
    ────────────────────────────────────────────────────────────────
    存量库的 grape_t_schema_migrations 早已被 legacy-stamp 盖帽
    （202610300001），启动时执行器只跑本函数登记的增量迁移 —— 写进老轨的
    ALTER TABLE 永远不会执行，运行代码查该列立刻 Unknown column(1054)，
    异常被各处 except 吞掉后接口静默返回空数组（2026-11-06 Issue #995
    的 issue.vcs_exists 就是这么挂的：升级后四台 794 次 1054，
    /api/issues/mine 从有数据变成 0 条）。

    提交前自查五条（tests/api/test_migration_track_guard.py 会卡前 1/2/3/4 条）：
    1. version 未被占用：库里已盖章的号 + 源码里其它 register_migration 的号都不能重
       （同号会被判「已执行」永久跳过；历史事故 202611050001 / 202611060001）；
    2. fn 失败要抛出，不要 try/except 吞掉 —— 执行器约定「抛错=不盖章，下次启动重试」，
       吞了就会「列没建成、版本号却盖死」；
    3. 加 TEXT 列且带非 NULL DEFAULT 时，_ensure_column 要传 allow_copy=True
       （TEXT 带 DEFAULT 走不了 INSTANT/INPLACE，不传会被 DDL 安全带挡下）；
    4. fn 必须幂等（IF NOT EXISTS / _ensure_column），存量库会重跑；
    5. renumber 前必须人工核对目标环境 grape_t_schema_migrations 现值
       （读写版本台账表的 version/name 两列）——测试连不了真实库，
       跨分支撞号只能靠这一步发现；执行器对「同号不同名」会报 ERROR 但不阻断启动。
    6. 关键迁移（列存在性 / 收敛结果可判定）应传 post_check（Issue #3648）：
       fn → post_check 通过才盖章；同号不同名时 post_check 失败会重跑幂等 fn 兜底。
    """
    # 未显式声明域时，套用存量补正表（见 _LEGACY_DOMAIN_LABELS 注释）
    _doms = tuple(domains) if domains else _LEGACY_DOMAIN_LABELS.get(version, ("mgmt",))
    mig = Migration(version=version, name=name, kind=kind, fn=fn, domains=_doms,
                    post_check=post_check)
    # 撞号守卫（对齐 weekly-10 #1412）：runner 只按 version 判「已执行」不比对 name，
    # 源内同号后登记者将被永久跳过（跨线教训 202611040001/002 与 weekly-10 撞号，
    # 8082 实锤 #1011 两条被顶替跳过 → autopilot 500）。
    if any(m.version == version for m in _MIGRATIONS):
        _prev = next(m.name for m in _MIGRATIONS if m.version == version)
        log.error(
            f"迁移版本号撞号: {version} 已被 {_prev} 占用，本迁移 {name} 将被版本表"
            f"判为「已执行」而永久跳过。改用未占用号（日期+序号顺延，先对"
            f" weekly-10 注册表核号）后再提交。")
    _MIGRATIONS.append(mig)
    _MIGRATIONS.sort(key=lambda m: m.version)
    return mig


def _fn_uuid_pk_grape_accounts_and_user_id_cols():
    """D105 第二批：grape_accounts.id → VARCHAR(36) PK + 29 张引用表 user_id 族列
    MySQL 侧 INTEGER→VARCHAR(36) 放宽（已拍板口径：主键 UUID、存量值字符串化
    保留不重编号；引用列 VARCHAR(36) 与 grape_t_system_credentials 对齐）。
    自省模式接入：不声明 DDL/拷贝 SQL——sqlite_master 原文改造 + PRAGMA 列清单
    自动生成（UuidPkMigration._auto_sqlite_plan），约束/默认值/UNIQUE 零丢失。
    第三批 organization 只需同样两行。"""
    from src.api.uuid_pk_migration import UuidPkMigration
    UuidPkMigration(
        pk_table="grape_accounts",
        uid_widen_cols=("user_id", "grape_user_id", "owner_user_id",
                        "source_user_id", "host_user_id",
                        # 202611091100 兜底：patrol_findings.owner / issue.release_approved_by
                        # 全库按列名扫描仅各自命中一张表（owner 无同名歧义列），一并 VARCHAR(36)
                        "owner", "release_approved_by"),
    ).run()

register_migration(
    # G4 撞号根治：原号 202611090002 与基线 repair-autopilot-exec-agent-task-id
    # 同号异名（补盖已由 202611110003 承担）。改号到 202611120003。幂等保障：
    # UuidPkMigration 三态自检，MySQL 侧逐列 information_schema 探测后才
    # MODIFY，旧号已盖章的库上重跑零写入。
    version="202611120003",
    name="uuid-pk-grape-accounts-and-user-id-cols",
    kind="safe",
    fn=_fn_uuid_pk_grape_accounts_and_user_id_cols,
)

def _fn_create_grape_t_patrol_findings() -> None:
    """部署验证实测发现的缺口修复（2026-11-02）：存量库（迁移台账已盖章）
    上 grape_t_patrol_findings 永不建表——_NEW_TABLES 只服务 legacy 全量首跑路径，
    已盖章库启动只跑 _MIGRATIONS。注册 safe 迁移让存量库启动自动补建
    （CREATE TABLE IF NOT EXISTS 幂等）。"""
    with db_lock:
        db.execute(_PATROL_FINDINGS_DDL)
        db.commit()


def _ensure_platform_storage_columns():
    """D44/S64：按仓库平台卡幂等加 managed_repos 存储列（行式白名单先例同款）。

    F4/D106 终态修订（2026-11-11）：卡 token_storage_field/webhook_token_field
    显式空串 = 无物理列（gitee/gitlab 终态，凭据只落 grape_t_repo_credentials 单表），
    本函数跳过；仅键缺席（派生列名，gitcode/github 存量语义）才自动补列。
    identifier 白名单校验；列已存在跳过；无卡不加。

    （squash 合并一度丢失，2026-11-13 按 b439764fd 原形恢复——新增仓库平台
    "加卡即接入"的动态列供给链依赖此函数：config-only 平台声明存储列后，
    盖章库/新库启动时列自动补齐，无需手工 DDL。）
    """
    try:
        import re as _re
        from src.domains.runtime.vcs.platform_card import list_vcs_platforms, get_platform_card
        with db_lock:
            c = db.cursor()
            cols = {r[1] for r in c.execute("PRAGMA table_info(managed_repos)").fetchall()}
            for provider in (list_vcs_platforms() or []):
                card = get_platform_card(provider)
                if card is None:
                    continue
                for col in (card.token_storage_field, card.webhook_token_field):
                    if not col or col in cols:
                        continue
                    if not _re.match(r'^[a-z_]+$', col):
                        log.warning(f"Invalid managed_repos storage column '{col}' for {provider}, skipping")
                        continue
                    try:
                        c.execute(mgmt_mig.managed_repos_add_column(col))
                        cols.add(col)
                        log.info(f"Added managed_repos storage column: {col} (provider={provider})")
                    except Exception as e:
                        log.warning(f"Failed to add storage column {col} (provider={provider}): {e}")
            db.commit()
    except Exception as e:
        log.warning(f"_ensure_platform_storage_columns failed: {e}")


def _fn_github_bot_username_to_card() -> None:
    """D15/S23（2026-11-12 B 层收敛）：存量 global_bot_github_username 迁入
    github 仓库平台卡 bot_username 字段（写 meta vcs_providers_config）。

    幂等：卡 bot_username 已非空跳过；meta 无值跳过。meta 键保留兜底不删
    （get_global_github_bot_username 读链=卡优先→meta 兜底）。
    """
    import json as _json
    from src.kernel.session_manager import db as _db, db_lock as _dl
    from src.domains.mgmt.user.db import get_meta_value, set_meta_value
    _old = (get_meta_value("global_bot_github_username") or "").strip()
    if not _old:
        return
    with _dl:
        c = _db.cursor()
        row = c.execute(runtime_mig.SELECT_META__FN_GITHUB_BOT_USERNAME_TO_CA).fetchone()
        cards = _json.loads(row[0]) if row and row[0] else {}
        gh = dict(cards.get("github") or {})
        if str(gh.get("bot_username") or "").strip():
            return  # 卡已有值（管理员显式配置）——不覆盖
        gh["bot_username"] = _old
        cards["github"] = gh
        c.execute(runtime_mig.UPSERT_META,
                  (_json.dumps(cards, ensure_ascii=False),))
        _db.commit()
    try:
        from src.domains.runtime.vcs.platform_card import invalidate_platform_card_cache
        invalidate_platform_card_cache()
    except Exception:
        pass
    from src.kernel.config import log
    log.info(f"migrate global_bot_github_username -> github card bot_username: {_old}")


register_migration(
    # 撞号改号（2026-11-13 同步 arch-weekly-11 基线）：原号 202611150002 与基线
    # seed-event-action-filter-defaults 同号异名——基线已盖章库按号跳过，本链 fn
    # 静默不执行。改号到 202611150006（> 基线 max 202611150002）。幂等保障：
    # 卡 bot_username 已非空跳过、meta 无值跳过，旧号已盖章的库上重跑零写入。
    version="202611150006",
    name="github-bot-username-to-card",
    kind="safe",
    fn=_fn_github_bot_username_to_card,
)

register_migration(
    # 撞号改号（2026-11-13 同步 arch-weekly-11 基线）：原号 202611150001 与基线
    # seed-agent-types-from-config 同号异名。改号到 202611150005。幂等保障：
    # _ensure_column + information_schema/IF NOT EXISTS 三态自检，旧号已盖章
    # 库上重跑零写入（与 G4 改号同款）。
    version="202611150005",
    name="login-whitelist-row-columns",
    kind="safe",
    fn=_fn_login_whitelist_row_columns,
)


def _fn_ensure_im_channel_tables() -> None:
    """存量库补建 IM 渠道表族（2026-11-18）。

    渠道 8 表原先只挂在老轨 `_create_tables()`（`_NEW_TABLES`）里，而存量库
    （已盖 legacy-stamp 202610300001）启动**不跑老轨** → 表永不创建：
    页面保存渠道时 `sync_installation` 撞 `no such table` 被吞成 warning
    （接口仍返回成功、AI 徽标一直「未连接」），安装读取/通知扇出全部静默失效。
    DDL 与引擎启动迁移共用同一份 001/004，幂等（IF NOT EXISTS + 列判存在），
    存量库重跑零写入。
    """
    from src.kernel.session_manager import db as _c_db, db_lock as _c_lock
    from src.domains.runtime.im.engine_wiring import (ensure_channel_schema,
                                                      ensure_notify_outbox_schema)
    with _c_lock:
        ensure_channel_schema(_c_db)
        ensure_notify_outbox_schema(_c_db)
        _c_db.commit()


register_migration(
    # 2026-11-19 合并避让：目标库 202611182001 已由本仓 create-org-dispatch-tables
    # 盖章（同号不同名），本迁移沿用原号会被判「已执行」而永久跳过、IM 渠道表不会建。
    version="202611184000",
    name="ensure-im-channel-tables",
    kind="safe",
    fn=_fn_ensure_im_channel_tables,
)


def _fn_repo_platforms_create_repo_options() -> None:
    """grape_t_repo_platforms 补 create_repo_options 列（2026-11-20）。

    上游把该能力位同时写进了 `configs/platforms/*.json` 与
    `src/domains/runtime/vcs/platform_card.py`，唯独持久化模型/存量库没有这列。
    后果链：`importer._apply_fields` 把配置键当成「未知配置键」→ 整条平台配置被拒
    （启动日志 `platform-object import failed: platform:gitcode: 未知配置键
    ['create_repo_options']（整条拒绝）`）→ 平台行停在 `source='orphaned'`、
    `enabled=0` → `is_platform_visible()` 为假 → **用户面仓库列表整页为空**，
    现象像「托管仓库丢了」，实际数据一行没少。

    模型列已补（models_platform_object.RepoPlatformModel），存量库靠本迁移补列；
    列判存在 + 幂等，重跑零写入。
    """
    from src.kernel.session_manager import db as _c_db, db_lock as _c_lock
    with _c_lock:
        _ensure_column(_c_db.cursor(), "grape_t_repo_platforms",
                       "create_repo_options", "INTEGER DEFAULT 0")
        _c_db.commit()


register_migration(
    version="202611202001",
    name="repo-platforms-create-repo-options",
    kind="safe",
    fn=_fn_repo_platforms_create_repo_options,
)


def _fn_create_repo_credentials_table() -> None:
    """S80/D55 建 rc 单表；D99（2026-11-08）语义重设计——目标态一步建对。

    Issue #2273：该迁移在本仓一线历史（c9aabca63，版本 202611120005）已存在，
    合并 47bee3126 整体替换 db_setup.py 时被丢弃，而写路径（add_managed_repo）
    与用例（_seed_repo_bot_tokens）仍在——冷启动库缺表即 OperationalError
    （实测 69 errors）。本线未含 202611120005 盖章记录，且按迁移版本号守卫
    （R3：新号必须 > 基线 max 202611140004）改号 202611150003 注册；
    一线已按 202611120005 盖章的环境因 CREATE TABLE IF NOT EXISTS 幂等，
    重跑零写入。feat/issue-2273（1ef1c04b3）同函数体，编号口径见其提交说明。

    - id VARCHAR(256) PRIMARY KEY：UUID 主键（查询/引用以 id 为准，首写生成；
      存量行由 backfill_repo_credentials_uuid 渐进回填）
    - cred_type 归一值域 {'bot','webhook'}（平台语义由 repo_key 前缀承载）
    - UNIQUE (repo_key, cred_type)：一仓库一类一条
    """
    with db_lock:
        db.execute(_REPO_CREDENTIALS_DDL)
        db.commit()


register_migration(
    version="202611150003",
    name="create-repo-credentials-table",
    kind="safe",
    fn=_fn_create_repo_credentials_table,
)


def backfill_gitee_gitlab_token_columns_to_rc() -> int:
    """F4/D106（2026-11-11）存量列回填迁移：managed_repos 的 gitee/gitlab
    四列 → grape_t_repo_credentials 行（幂等：只填空，已有 rc 行不覆盖）。

    设计要点：
    - 值归一：列值可能为 N3 密文或历史明文——_decrypt（明文直通）→ _encrypt
      统一回填为密文形态（与写路径同构，读侧零特判）。
    - 列存在性守卫：冷启动库（F4 后）无这四列，逐列探测，无列跳过零写入。
    - 物理列**不 DROP**：作为历史回落面保留至渐进收敛完成（写路径轮换置空）。
    - 退出 checklist（legacy 列回落分支退役顺序，见 token_resolver RepoBotToken）：
      ①本回填（存量值进 rc）→ ②写路径置空（每次轮换自动清一行）→
      ③确认日志零 legacy column fallback 命中后关闭回落分支 → ④删回落代码并
      另立迁移评估列删除。本迁移只负责 ①。

    幂等性：INSERT OR IGNORE（UNIQUE(repo_key, cred_type) 冲突即忽略），
    重复执行零写入。返回回填行数。

    （squash 合并一度丢失，2026-11-13 按 b439764fd 原形恢复并按
    migration-parity-assessment.md 阶段二口径改号 202611150004
    （原号 202611110004 落历史区间）；原函数体内 202611120005 撞号
    由 202611150003 建表迁移承担。）
    """
    from src.kernel.id_gen import uuid7_str
    _GL_COLS = ("bot_gitee_token", "gitee_webhook_token",
                "bot_gitlab_token", "gitlab_webhook_token")
    n = 0
    per_col: dict = {}
    try:
        from src.domains.mgmt.user.db import _encrypt_repo_secret, _decrypt_repo_secret
    except Exception as e:
        log.warning(f"F4 backfill: crypto import failed, skip: {e}")
        return 0
    with db_lock:
        c = db.cursor()
        try:
            cols = {r[1] for r in c.execute("PRAGMA table_info(managed_repos)").fetchall()}
        except Exception as e:
            log.warning(f"F4 backfill: managed_repos column probe failed, skip: {e}")
            return 0
        for col in _GL_COLS:
            if col not in cols:
                per_col[col] = 0  # 冷启动库无此列（F4 后新库）
                continue
            _ct = "bot" if col.startswith("bot_") else "webhook"
            rows = c.execute(mgmt_mig.managed_repos_select_column(col)).fetchall()
            k = 0
            for rk, val in rows:
                try:
                    _norm = _encrypt_repo_secret(_decrypt_repo_secret(val or ""))
                except Exception as _de:
                    # 单行解密失败不阻断整档：保留列值原样回填（读侧明文兼容可读）
                    log.warning(f"F4 backfill: decrypt failed for {rk}.{col}, raw copy: {_de}")
                    _norm = val or ""
                cur = c.execute(
                    insight_mig.INSERT_IGNORE_GRAPE_T_REPO_CREDENTIALS,
                    (uuid7_str(), rk, _ct, _norm))
                k += cur.rowcount
            per_col[col] = k
            n += k
        db.commit()
    if n:
        log.info(f"F4 backfill gitee/gitlab token columns -> grape_t_repo_credentials: "
                 f"{per_col} (rows filled={n})")
    return n


def _fn_backfill_gitee_gitlab_token_columns() -> None:
    """迁移壳：gitee/gitlab 存量列回填 rc（F4 第一批，safe）。"""
    backfill_gitee_gitlab_token_columns_to_rc()


register_migration(
    version="202611150004",
    name="backfill-gitee-gitlab-token-cols-to-rc",
    kind="safe",
    fn=_fn_backfill_gitee_gitlab_token_columns,
)

register_migration(
    version="202611020001",
    name="create-patrol-findings-table",
    kind="safe",
    fn=_fn_create_grape_t_patrol_findings,
)


def _fn_task_liveness_index_and_revived_at() -> None:
    """Issue #1071：迟到结果复活所需的结构（存量库幂等补齐）。

    - agent_tasks.revived_at：复活只允许发生一次，靠此列落痕（NULL = 未复活）。
    不加 task_messages(task_id, created_at) 复合索引：实测该表已有
    idx_task_messages_task(task_id)（db_setup:1986，存量库均在），而活性查询是
    「按 task_id 过滤 + ORDER BY id DESC LIMIT 20」，单列索引即命中；task_messages 是
    追加最频繁的表，再挂一个索引只有写入放大、没有读收益。
    走 register_migration（legacy 清单已冻结，不再往 _NEW_TABLES/legacy 里加）。
    """
    with db_lock:
        c = db.cursor()
        try:
            _ensure_column(c, "agent_tasks", "revived_at", "TEXT DEFAULT NULL")
            db.commit()
        except Exception as e:
            log.warning(f"agent_tasks.revived_at migration failed: {e}")


register_migration(
    version="202611080004",
    name="task-liveness-revived-at",
    kind="safe",
    fn=_fn_task_liveness_index_and_revived_at,
)


def _fn_create_grape_t_merge_gate_log() -> None:
    """Issue #752：存量库补建合入门禁日志表（CREATE TABLE IF NOT EXISTS 幂等，
    与 patrol-findings 同路径：_NEW_TABLES 只服务 legacy 全量首跑，已盖章库走本迁移）。"""
    with db_lock:
        db.execute(_MERGE_GATE_LOG_DDL)
        db.commit()


register_migration(
    # 20261105 修复：远端曾误改为 0001（与 issue658_role_tags 撞号），且生产版本表
    # 已按 0002 落库——回到 0002 对齐生产；issue865 挪至 0004 避让。
    # 2026-11-10：目标库 202611030002 已被 issue865_agent_runtimes_model 占用（同号不同名，
    # 本次迁移被判已执行永久跳过）→ renumber 到 202611100002。该迁移只做
    # CREATE TABLE IF NOT EXISTS，重跑幂等无损。
    version="202611100002",
    name="create-merge-gate-log-table",
    kind="safe",
    fn=_fn_create_grape_t_merge_gate_log,
)


def _fn_add_agent_tasks_trace_context() -> None:
    """MR-2（weekly-10）：存量库补 agent_tasks.trace_context（幂等补列）。

    _migrate_tables 里的同名 _ensure_column 只在 legacy 全量迁移路径执行，已盖章的
    存量库不会再跑那一段，缺列时代码侧 _TASK_COLUMNS_LIST 含该列会让
    _create_direct_task 的严格 INSERT 直接抛错、任务行永不落库。故补一条独立迁移。
    """
    with db_lock:
        c = db.cursor()
        # 非 owner 库（mgmt/insight）的 agent_tasks 是跨库可更新视图：ALTER 必然 1346/1347，
        # 列只能加在 owner 基表上（grape_runtime_db），可见性由 scripts/sync_domain_views.py
        # 重建视图保证。视图场景直接跳过，避免每次启动刷 ERROR。
        _row = c.execute(
            "SELECT TABLE_TYPE FROM information_schema.tables "
            "WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME='agent_tasks'").fetchone()
        if _row and str(_row[0]).upper() == "VIEW":
            log.info("agent_tasks 在本库为跨库视图，trace_context 归属 owner 基表，跳过 ALTER")
            return
        _ensure_column(c, "agent_tasks", "trace_context", "TEXT",
                       allow_copy=True, strict=True)
        db.commit()


register_migration(
    version="202611100001",
    name="add-agent-tasks-trace-context",
    kind="safe",
    fn=_fn_add_agent_tasks_trace_context,
)


def _fn_create_branch_guard_state() -> None:
    """Issue #993：存量库补建分支守卫状态表（CREATE TABLE IF NOT EXISTS 幂等）。

    与 distributed_locks 同源 DB，跨 worker 共享 expected_remote_sha；早期把它落在
    本机 workspace 文件，跨 worker 调度时漂移检测会 fail-open。
    """
    with db_lock:
        db.execute(_BRANCH_GUARD_STATE_DDL)
        db.commit()


# 撞号修复（2026-11-11）：202611060001 在历史库里已被 backfill_events_terminal_completed_at
# 盖章，导致本迁移长期被判「已执行」而跳过 → branch_guard_state 表在 dev/生产都不存在
# （启动日志 ERROR「迁移版本号撞号」即此）。改用未被占用的新版本号，并把 pending 列一起补齐，
# 使迁移自包含、不依赖 202611060002 的执行顺序。
def _fn_create_branch_guard_state_v2() -> None:
    _fn_create_branch_guard_state()
    _fn_add_branch_guard_pending_columns()


register_migration(
    version="202611110001",
    name="create-branch-guard-state-table",
    kind="safe",
    fn=_fn_create_branch_guard_state_v2,
)


def _fn_add_branch_guard_pending_columns() -> None:
    """Issue #993 复审：pre-push 钩子候选 sha 字段（存量库幂等补列）。

    钩子退出后才真正传输，无法在钩子内确认 push 成功；用 pending_expected_sha +
    pending_prev_sha 记录候选与推送前远端，下一次守卫按远端实际值收敛，避免 push
    失败后误报 remote_drift。
    """
    with db_lock:
        c = db.cursor()
        _ensure_column(c, "branch_guard_state", "pending_expected_sha",
                       "VARCHAR(64) NOT NULL DEFAULT ''")
        _ensure_column(c, "branch_guard_state", "pending_prev_sha",
                       "VARCHAR(64) NOT NULL DEFAULT ''")
        db.commit()


register_migration(
    version="202611060002",
    name="add-branch-guard-pending-columns",
    kind="safe",
    fn=_fn_add_branch_guard_pending_columns,
)


def _fn_add_ap_exec_agent_task_id() -> None:
    """Issue #1546：autopilot_executions 直接持有 agent_tasks.id（存量库幂等补列 + 索引）。

    反查任务不再依赖 `event_seq`（= exec_id）与 events.seq 的跨序列撞号；索引
    支撑按 agent_task_id 直连查询。列/索引已存在时正常返回并盖章，失败抛出由
    迁移执行器跳过不盖章（幂等重试）。
    """
    with db_lock:
        c = db.cursor()
        _ensure_column(c, "autopilot_executions", "agent_task_id", "TEXT DEFAULT NULL")
        db.execute(
            "CREATE INDEX IF NOT EXISTS idx_ap_exec_agent_task "
            "ON autopilot_executions(agent_task_id)")
        db.commit()


register_migration(
    # 撞号避让（2026-11-09 变基）：weekly-10 已用 202611060003 给
    # backfill_events_terminal_completed_at，同号后登记项会被判「已执行」永久跳过。
    # 2026-11-19 合并上游后再次避让：目标库 202611090001 已由
    # session_api_tokens_agent_identity 盖章（同号不同名），本迁移沿用本仓既有号
    # 202611182015（该号在库内盖章名即本迁移），否则会被永久跳过。
    version="202611182015",
    name="add-autopilot-exec-agent-task-id",
    kind="safe",
    fn=_fn_add_ap_exec_agent_task_id,
)


def _fn_create_credential_usage_log_table() -> None:
    """D90 消费端批步 4（P-5 硬前置）：grape_t_credential_usage_log 审计表。

    表名按主控新表 grape_t_xxx 约定定名（规则 2.0 A6，同 003 渠道表口径）——
    本分支新增表，建表即用规范名，无存量改名场景。
    credential_id 软引用无外键级联（凭据删除保留审计历史）；credential_kind
    预留（P-2c 上线零迁移）；executor/initiator 分列。ORM/Core 面：
    src/persistence/credential_usage_log.py（铁律 9 高频审计写入走 Core insert）。
    """
    with db_lock:
        db.execute(_CUL_DDL)
        # N4：存量旧形态表幂等补列（repo_key）——ALTER + 吞 duplicate column
        try:
            db.execute(runtime_mig.ALTER_GRAPE_T_CREDENTIAL_USAGE_LOG)
        except Exception:
            pass  # duplicate column = 已补
        # N4-p1：审计查询索引（list_by_credential 高频路径；全量记录下防全表扫）
        db.execute("CREATE INDEX IF NOT EXISTS idx_cul_credential_id "
                   "ON grape_t_credential_usage_log (credential_id)")
        db.execute("CREATE INDEX IF NOT EXISTS idx_cul_repo_key "
                   "ON grape_t_credential_usage_log (repo_key)")
        db.commit()


def _fn_repair_agent_tasks_trace_context() -> None:
    """补 202611120004 的失效效果：该迁移在同号事故后已盖章，但 MySQL 上
    `TEXT DEFAULT ''` 既不能 INSTANT 也不能 INPLACE,LOCK=NONE，_ensure_column
    只记人工清单便返回 → 三库都出现「已盖章、列不存在」（event_pipeline 已在写
    trace_context）。本条用无 DEFAULT 的 TEXT + allow_copy 幂等重试。
    """
    with db_lock:
        c = db.cursor()
        _ensure_column(c, "agent_tasks", "trace_context", "TEXT",
                       allow_copy=True, strict=True)
        db.commit()


def _fn_create_system_credentials_table() -> None:
    """D100 批 1（2026-11-08）：grape_t_system_credentials 系统/组织级凭据表。

    方案稿 docs/weekly-09/d100-credential-type-schema-design.md §1.3：
    - id UUID 36 应用层生成（表设计铁律 1）
    - scope_type 值域 {'system'}（org 预留不实现，放开走独立评审）；
      scope_id system 固定 ''——MySQL UNIQUE 对 NULL 视为互不相等，
      用 '' 保证 UNIQUE(scope_type, scope_id, cred_type) 真正生效
    - created_at/updated_at NOT NULL 无 DEFAULT：时间由门面应用层强制
      赋值（铁律 2），DEFAULT '' 会让漏传静默成空串（时间审计失真）
    - value 存 credential_crypto 加密后密文（加密在门面写入口完成，
      本表零明文）——与 rc/N3 通道加密同模式
    - 零数据动作：A1-A4 存量凭据暂缓收编（用户裁定），落库后为空表
    双方言：DDL 在 SQLite/MySQL 均原生合法（无 ON CONFLICT、无 DB 侧
    时间函数），fn 无需后端分支。
    """
    with db_lock:
        db.execute(_SC_DDL)
        # cred_type 单查索引：管理面"按类型列出"+ 批 2 迁移探针均按
        # cred_type 单查，UNIQUE 三列组合索引覆盖不了该路径
        db.execute("CREATE INDEX IF NOT EXISTS idx_sc_cred_type "
                   "ON grape_t_system_credentials (cred_type)")
        db.commit()

def _fn_issue_state_scene_user_id_text_normalize() -> None:
    """202611091200（G3 门禁影子演练实锤）：issue_state.scene_user_id 归一。

    DDL 为 INTEGER DEFAULT 0（0=非用户级哨兵）。用户级场景创建者是账号域 uuid
    字符串——MySQL INT 列严格模式直接写崩；门禁 user_id 族不变式同样红。
    MySQL：INT/TEXT → VARCHAR(36) NOT NULL DEFAULT ''，并把存量哨兵 0 改写 ''；
    读侧统一 `or 0`（''/0 均 falsy），哨兵语义保真。
    同迁移顺带放宽 unified_user_mapping.dingtalk_user_id（VARCHAR(36)→VARCHAR(191)，
    门禁 user_id 族不变式红；不进 _ACCOUNT_FK_USER_ID_COLS——守卫测试锁定 33 对）。
    SQLite：INTEGER 亲和天然容混型、0 哨兵 falsy——幂等零改动（只占盖章位）。
    """
    from src.kernel.mysql_compat import get_db_backend
    if get_db_backend() != "mysql":
        return
    from src.kernel.session_manager import db
    c = db.cursor()
    c.execute(
        "SELECT DATA_TYPE FROM information_schema.columns "
        "WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME='issue_state' "
        "AND COLUMN_NAME='scene_user_id'")
    row = c.fetchone()
    if row and row[0] not in ("varchar", "char"):
        c.execute(runtime_mig.ALTER_ISSUE_STATE)
    if row:
        # 哨兵改写（幂等）：'0' 字符串 truthy，会破坏读侧 if not scene_uid 兜底
        c.execute(runtime_mig.UPDATE_ISSUE_STATE)
    c.execute(
        "SELECT DATA_TYPE, IS_NULLABLE FROM information_schema.columns "
        "WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME='unified_user_mapping' "
        "AND COLUMN_NAME='dingtalk_user_id' AND DATA_TYPE='text'")
    row = c.fetchone()
    if row:
        suffix = " NOT NULL" if row[1] == "NO" else ""
        c.execute(mgmt_mig.unified_user_mapping_widen_dingtalk(suffix))
    db.commit()

register_migration(
    version="202611140001",
    name="create-credential-usage-log-table",
    kind="safe",
    fn=_fn_create_credential_usage_log_table,
)

register_migration(
    version="202611140002",
    name="create-system-credentials-table",
    kind="safe",
    fn=_fn_create_system_credentials_table,
)

register_migration(
    version="202611140003",
    name="repair-agent-tasks-trace-context",
    kind="safe",
    fn=_fn_repair_agent_tasks_trace_context,
)

register_migration(
    version="202611140004",
    name="issue-state-scene-user-id-text-normalize",
    kind="safe",
    fn=_fn_issue_state_scene_user_id_text_normalize,
)


def _fn_repair_ap_exec_agent_task_id() -> None:
    """Issue #1546 修复（2026-11-09）：原迁移在 MySQL 上可能「已盖章但列未创建」。

    _ensure_column 两级降级失败时不抛异常、只记人工清单，迁移仍会被盖章 → 该列再也
    不会补。本迁移幂等重查一次：列存在则只补索引（无副作用），缺失则按修正后的
    剥离 DEFAULT NULL 路径补列；即使仍失败，下次启动也会重试（未被提前盖章掩盖）。
    """
    with db_lock:
        c = db.cursor()
        _ensure_column(c, "autopilot_executions", "agent_task_id", "TEXT DEFAULT NULL")
        db.execute(
            "CREATE INDEX IF NOT EXISTS idx_ap_exec_agent_task "
            "ON autopilot_executions(agent_task_id)")
        db.commit()


register_migration(
    version="202611130001",
    name="repair-autopilot-exec-agent-task-id",
    kind="safe",
    fn=_fn_repair_ap_exec_agent_task_id,
)


def _fn_create_autopilot_executions_table() -> None:
    """Issue #3763：存量库补建 runtime 域表 autopilot_executions。

    `autopilot_executions` 原先只挂在老轨 `_NEW_TABLES`（legacy 全量首跑清单）里，
    已盖 legacy-stamp 的存量库启动只跑 `_MIGRATIONS`，所以该表永远不会补建：
    worker 建执行记录时 `(1146) Table ... doesn't exist`，被 `_execute_task` 外层
    except 吞成一条无上下文 ERROR，autopilot 静默失效（生产 33 条/时）。

    归属 runtime（configs/table_ownership.json），故只登记 runtime 域并在 owner
    连接上执行；CREATE IF NOT EXISTS + 幂等补列/索引，存量库重跑零写入。
    """
    if _owned_skip("autopilot_executions", "create table"):
        return
    conn = _migration_conn("runtime")
    with (db_lock if conn is db else nullcontext()):
        c = conn.cursor()
        if not _table_present(c, "autopilot_executions"):
            c.execute(_AUTOPILOT_EXECUTIONS_DDL)
        for _col, _ddl in (
            ("agent_type", "TEXT DEFAULT ''"),
            ("findings_json", "MEDIUMTEXT DEFAULT ''"),
            ("agent_task_id", "TEXT DEFAULT NULL"),
            ("params_json", "MEDIUMTEXT DEFAULT ''"),
            ("notified", "INTEGER NOT NULL DEFAULT 0"),
        ):
            _ensure_column(c, "autopilot_executions", _col, _ddl, strict=True)
        for _idx in (
            "CREATE INDEX IF NOT EXISTS idx_autopilot_exec_task "
            "ON autopilot_executions(task_id)",
            "CREATE INDEX IF NOT EXISTS idx_autopilot_exec_status "
            "ON autopilot_executions(status)",
            "CREATE INDEX IF NOT EXISTS idx_ap_exec_agent_task "
            "ON autopilot_executions(agent_task_id)",
        ):
            c.execute(_idx)
        conn.commit()


def _postcheck_autopilot_executions_table() -> None:
    """Issue #3763 复审加固：autopilot_executions 真在 runtime owner 库才盖章。

    执行器对「fn 正常返回」即盖章。但本迁移在非 runtime owner 进程会被
    `_owned_skip` 直接 return（或三库 DSN 缺失时 `_migration_conn` 回落进程库），
    两种情况下表都没建，台账却被写入 → runtime 进程因「已盖章」不再执行，
    表永久缺失。此处断言基表存在，缺表 raise 不盖章，交 owner 进程下次启动补齐。
    """
    conn = _migration_conn("runtime")
    with (db_lock if conn is db else nullcontext()):
        c = conn.cursor()
        if not _base_table_present(c, "autopilot_executions"):
            raise RuntimeError(
                "202611240010 post_check 未收敛：runtime owner 库缺 autopilot_executions "
                "基表（_owned_skip 跳过 / 回落进程库未命中），不盖章，待 owner 进程重试")


register_migration(
    # R3 换号：基线已发布最大号为 202611230012（tests/arch/test_migration_version_integrity.py
    # 强制新增迁移必须大于该值），早前顺延到的 202611230006 落在历史区间内会被门禁拦下；
    # 202611240001..009 均已被占用（含基线侧 #3349 的 202611240003 与 autopilot-task-dispatch-state
    # 的 202611240007），顺延到未占用的 202611240010（名字/语义不变，仍只登记 runtime 域）。
    version="202611240010",
    name="create-autopilot-executions-table",
    kind="safe",
    domains=("runtime",),
    fn=_fn_create_autopilot_executions_table,
    post_check=_postcheck_autopilot_executions_table,
)


def _fn_rebuild_grape_t_patrol_findings_unique() -> None:
    """部署验证实测发现（2026-11-02）：issue 级 finding（stalled_issue）task_id 恒为空串，
    原三列唯一键 UNIQUE(kind, task_id, event_seq) 使全部 issue 级发现共享同一键——
    本机实测 14 条 stalled 只落 1 条。issue_id 进唯一键；旧形态表重建（保数据）。"""
    with db_lock:
        c = db.cursor()
        # 评审十轮修正②：sqlite_master 判断用字面量名形态——兼容层
        # _SQLITE_MASTER_SQL_RE 只认 name='字面量'（参数化形态穿透不翻译，
        # MySQL 上每次启动报错、迁移永不盖章）。字面量形态被翻译为
        # information_schema 查询，双方言一致。
        c.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='grape_t_patrol_findings'")
        row = c.fetchone()
        create_sql = (row[0] or "") if row else ""
        if "grape_t_patrol_findings" not in create_sql:
            return          # 表不存在：fresh 库走新 DDL
        if "UNIQUE(kind,task_id,issue_id,event_seq)" in create_sql.replace(" ", ""):
            return          # 已是新形态
        db.execute(mgmt_mig.ALTER_GRAPE_T_PATROL_FINDINGS)
        db.execute(_PATROL_FINDINGS_DDL)
        db.execute(mgmt_mig.INSERT_GRAPE_T_PATROL_FINDINGS)
        db.execute("DROP TABLE grape_t_patrol_findings_old")
        db.commit()


register_migration(
    version="202611020002",
    name="rebuild-patrol-findings-unique-with-issue-id",
    kind="safe",
    fn=_fn_rebuild_grape_t_patrol_findings_unique,
)


def _fn_add_agent_runtimes_role_tags() -> None:
    """Issue #658 补注册（2026-11-03）：role_tags DDL 被写进已冻结的 legacy
    _migrate_tables 清单而未走新轨——legacy-stamp 盖帽后该 ALTER 永不执行。
    生产库已人工补列（本迁移在其上幂等空转、仅补盖章）；新装库建表定义
    天然含列，同样空转。幂等：information_schema 查列，缺才 ALTER。"""
    is_mysql = False
    try:
        from src.kernel.session_manager import _is_mysql_backend
        is_mysql = _is_mysql_backend()
    except Exception:
        pass
    with db_lock:
        c = db.cursor()
        if is_mysql:
            c.execute(
                "SELECT COUNT(*) FROM information_schema.columns "
                "WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME='agent_runtimes'"
                " AND COLUMN_NAME='role_tags'")
            if c.fetchone()[0]:
                return
            c.execute(runtime_mig.ALTER_AGENT_RUNTIMES)
            db.commit()
        else:
            c.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='agent_runtimes'")
            row = c.fetchone()
            if not row or "role_tags" in (row[0] or ""):
                return
            db.execute(runtime_mig.ALTER_AGENT_RUNTIMES__FN_ADD_AGENT_RUNTIMES_ROLE_T)
            db.commit()


register_migration(
    # 2026-11-13 回滚一次性误改号：账本实测 202611030001 = issue658_role_tags、
    # 202611100002 = create-merge-gate-log，并不存在"同号异名"；改号到 202611160001
    # 反而与已盖章的 takeover-sessions-v1 相撞，且违反"已发布版本不得改名改号"。
    # 恢复原号；fn 幂等（先查列存在才 ALTER），各环境重放无损。
    version="202611030001",
    name="issue658_agent_runtimes_role_tags",
    kind="safe",
    fn=_fn_add_agent_runtimes_role_tags,
)


def _fn_add_agent_runtimes_model() -> None:
    """Issue #865 补注册：agent_runtimes 增 model_name/model_source/model_reported_at
    三列（配置模型展示）。与 #658 role_tags 同模式：legacy 盖帽库不自动执行时
    附手工 ALTER（见 _migrate_tables 冻结清单）；幂等：查列缺才 ALTER。"""
    is_mysql = False
    try:
        from src.kernel.session_manager import _is_mysql_backend
        is_mysql = _is_mysql_backend()
    except Exception:
        pass
    with db_lock:
        c = db.cursor()
        if is_mysql:
            # Issue #1412 QA Required：逐列 _ensure_column 幂等补列。原实现在 have<3
            # 分支内无条件执行第三个 ALTER，当 model_reported_at 已存在而
            # model_name/model_source 缺一个（have==2）时每次启动都抛
            # Duplicate column name (1060)，迁移永不盖章、三列永远补不齐。
            # _ensure_column 内部 PRAGMA table_info 由 mysql_compat 转
            # information_schema，按列判断存在性；strict=True 保证补列失败
            # 不盖章（下次启动重试）。
            for col in ("model_name", "model_source", "model_reported_at"):
                _ensure_column(c, "agent_runtimes", col, "TEXT DEFAULT ''",
                               allow_copy=True, strict=True)
            db.commit()
        else:
            c.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='agent_runtimes'")
            row = c.fetchone()
            ddl = (row[0] or "") if row else ""
            if not row:
                return
            for col in ("model_name", "model_source", "model_reported_at"):
                if col not in ddl:
                    # DDL 归运行域（动态列名走 owner 域函数返回，不在本文件拼表名）
                    db.execute(runtime_mig.agent_runtimes_add_column(col))
            db.commit()


register_migration(
    # 20261105 修复：原 0002 与 merge-gate-log 撞号（merge-gate-log 已按 0002 落库），
    # 挪至 0004；后 0004 又被其它分支落库的 issue-646-mysql-fks 占用（Issue #1412），
    # 再 renumber 至全新日期段 202611070001（name 不变，兼容已按旧号盖章的环境）。
    # 列添加幂等（已存在则跳过），生产/新库均安全。
    version="202611070001",
    name="issue865_agent_runtimes_model",
    kind="safe",
    fn=_fn_add_agent_runtimes_model,
)


def _fn_add_obs_llm_usage_engine_index() -> None:
    """性能补丁（2026-11-03）：obs_llm_usage 原先没有 engine_id 索引，
    引擎页的"最近使用模型"聚合（Issue #865 `_recent_models_by_runtime`）
    每页触发一次全表扫描（EXPLAIN: type=ALL + Using temporary）——千万行量级
    下单页 0.3~0.6s，且全程持 db_lock，会串行堵住其它 DB 请求。
    补 (engine_id, model_key, model_raw, created_at) 索引后按引擎收窄 + 覆盖分组，
    实测（sqlite，100 万行）38ms → 1.5ms。幂等：索引已存在即跳过。"""
    is_mysql = False
    try:
        from src.kernel.session_manager import _is_mysql_backend
        is_mysql = _is_mysql_backend()
    except Exception:
        pass
    _idx = "idx_obs_llm_engine_model"
    with db_lock:
        c = db.cursor()
        if is_mysql:
            # MySQL 下 model_key/model_raw/created_at 是 TEXT，必须带前缀长度
            c.execute(
                "SELECT COUNT(*) FROM information_schema.statistics "
                "WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME='obs_llm_usage' "
                "AND INDEX_NAME=%s", (_idx,))
            if c.fetchone()[0]:
                return
            c.execute(
                f"CREATE INDEX {_idx} ON obs_llm_usage "
                "(engine_id, model_key(64), model_raw(64), created_at(32))")
            db.commit()
        else:
            c.execute("SELECT name FROM sqlite_master WHERE type='index' AND name=?", (_idx,))
            if c.fetchone():
                return
            c.execute(
                f"CREATE INDEX {_idx} ON obs_llm_usage "
                "(engine_id, model_key, model_raw, created_at)")
            db.commit()


register_migration(
    version="202611030003",
    name="perf_obs_llm_usage_engine_index",
    kind="safe",
    fn=_fn_add_obs_llm_usage_engine_index,
)


def _backfill_scene_rows_skill_id(rows: list, update_sql: str) -> int:
    """Issue #979 backfill 公共循环（devops_pipeline_scene / scene_market 共用）。

    rows 元素为 (row_id, repo_key, uid, nodes_json)：对仅含 skill_name 的节点按
    现有规则（user→repo→official，resolve_skill_id）解析一次并写回 skill_id
    （update_sql 形如 "UPDATE <table> SET nodes_json=? WHERE id=?"）。
    幂等：节点已有 skill_id 跳过；解析不到保持原样（派发时按名字降级 + warning）。
    返回发生写回的行数。
    """
    from src.domains.mgmt.user.db import resolve_skill_id
    updated = 0
    for row_id, repo_key, uid, nodes_json in rows:
        try:
            nodes = json.loads(nodes_json or "[]")
        except Exception as e:
            log.warning(f"issue979 backfill: bad nodes_json (row={row_id}): {e}")
            continue
        if not isinstance(nodes, list) or not nodes:
            continue
        changed = False
        for n in nodes:
            if not isinstance(n, dict):
                continue
            _name = n.get("skill") or ""
            if not _name or n.get("skill_id"):
                continue
            sid = resolve_skill_id(_name, repo_key=repo_key or "", user_id=uid or 0)
            if sid:
                n["skill_id"] = int(sid)
                changed = True
        if changed:
            try:
                with db_lock:
                    db.cursor().execute(update_sql,
                                        (json.dumps(nodes, ensure_ascii=False), row_id))
                    db.commit()
                updated += 1
            except Exception as e:
                log.warning(f"issue979 backfill: write back failed (row={row_id}): {e}")
    return updated


def _fn_scene_nodes_skill_id_backfill() -> None:
    """Issue #979：存量场景节点写回 skill_id（backfill，幂等续跑）。

    三档（official/repo/user）同名技能并存时，节点仅存 skill_name 无法锁定
    档位。本迁移对仅含 skill_name 的节点按现有规则（user→repo→official，
    resolve_skill_id）解析一次并写回 skill_id：
    - devops_pipeline_scene：user 上下文取 created_by（gitcode 用户名 →
      unified_user_mapping.user_id），repo 上下文取行内 repo_key；
    - scene_market：user 上下文取 source_user_id，repo 上下文取行内 repo_key
      （scope='repo' 行；此前空 repo_key 解析会漏 repo 档）。
    解析不到（官方内置包形态等）保持原样，派发时按名字降级 + warning。
    仅写回 skill_id，不删 skill_name（显示与降级兜底）。"""
    with db_lock:
        c = db.cursor()
        c.execute(
            mgmt_mig.SELECT_DEVOPS_PIPELINE_SCENE)
        scene_rows = c.fetchall() or []
        try:
            c.execute(mgmt_mig.SELECT_SCENE_MARKET__FN_SCENE_NODES_SKILL_ID_BACK)
            market_rows = c.fetchall() or []
        except Exception as e:
            log.warning(f"issue979 backfill: scene_market read failed: {e}")
            market_rows = []
    # devops_pipeline_scene：created_by（gitcode 用户名）→ user_id
    normalized = []
    for row_id, repo_key, nodes_json, created_by in scene_rows:
        uid = 0
        if created_by:
            try:
                with db_lock:
                    r2 = db.cursor().execute(
                        mgmt_mig.SELECT_UNIFIED_USER_MAPPING,
                        (created_by,)).fetchone()
                if r2:
                    uid = int(r2[0] or 0)
            except Exception as e:
                log.warning(f"issue979 backfill: created_by lookup failed "
                            f"(row={row_id}, user={created_by}): {e}")
        normalized.append((row_id, repo_key or "", uid, nodes_json))
    _backfill_scene_rows_skill_id(
        normalized, mgmt_mig.UPDATE_DEVOPS_PIPELINE_SCENE)
    # scene_market：source_user_id 即 user 上下文，repo_key 行内（repo 档）
    _backfill_scene_rows_skill_id(
        [(row_id, repo_key or "", int(source_user_id or 0), nodes_json)
         for row_id, repo_key, source_user_id, nodes_json in market_rows],
        mgmt_mig.UPDATE_SCENE_MARKET)


register_migration(
    version="202611040001",
    name="issue979_scene_nodes_skill_id_backfill",
    kind="backfill",
    fn=_fn_scene_nodes_skill_id_backfill,
)


def _fn_add_issue_repo_created_index() -> None:
    """性能补丁（2026-11-04 压测）：需求中心服务端分页（Issue #926）按
    `WHERE repo_key=? ... ORDER BY created_at DESC LIMIT/OFFSET` 取数，issue 表只有
    (repo_key, issue_number) 唯一索引 → 每页 COUNT(*) 全扫 + ORDER BY 建临时 B-TREE
    （200k 行实测首页 78ms / 深页 350ms），且全程持 db_lock 串行阻塞其它查询。
    补 (repo_key, created_at) 后 COUNT 与 time 模式排序可走索引；default 模式
    的第一排序键是状态优先级 CASE，无法完全免排序，深页由路由侧偏移上限兜底。
    幂等：索引已存在即跳过。"""
    is_mysql = False
    try:
        from src.kernel.session_manager import _is_mysql_backend
        is_mysql = _is_mysql_backend()
    except Exception:
        pass
    _idx = "idx_issue_repo_created"
    with db_lock:
        c = db.cursor()
        if is_mysql:
            # MySQL 下 repo_key/created_at 是 TEXT，必须带前缀长度
            c.execute(
                "SELECT COUNT(*) FROM information_schema.statistics "
                "WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME='issue' "
                "AND INDEX_NAME=%s", (_idx,))
            if c.fetchone()[0]:
                return
            c.execute(
                f"CREATE INDEX {_idx} ON issue (repo_key(64), created_at(32))")
            db.commit()
        else:
            c.execute("SELECT name FROM sqlite_master WHERE type='index' AND name=?", (_idx,))
            if c.fetchone():
                return
            c.execute(f"CREATE INDEX {_idx} ON issue (repo_key, created_at)")
            db.commit()


register_migration(
    version="202611040002",
    name="perf_issue_repo_created_index",
    kind="safe",
    fn=_fn_add_issue_repo_created_index,
)


def _fn_add_issue_status_priority_index() -> None:
    """性能补丁（2026-11-04 压测，方案 B）：需求中心默认排序是
    `CASE 状态优先级, created_at DESC`（路由 _status_order_sql，单一事实源）。
    普通列索引对"算出来的第一排序键"无效 → 20 万行 offset 19950 实测 168ms，
    且全程持 db_lock。改为表达式索引：sqlite 支持 `CREATE INDEX ... ON issue
    (repo_key, <CASE 表达式>, created_at DESC)`，EXPLAIN 变为
    `SEARCH ... USING INDEX`，同数据同深页 168ms → 1.0ms（首页 0.1ms）。
    表达式的 CASE 文本必须与 _status_order_sql() 完全一致（同一函数懒导入生成，
    避免两处手写漂移）；MySQL 侧函数索引需 >= 8.0.13，失败只告警跳过（不阻断启动，
    深页由路由偏移护栏兜底）。幂等：索引已存在即跳过。"""
    is_mysql = False
    try:
        from src.kernel.session_manager import _is_mysql_backend
        is_mysql = _is_mysql_backend()
    except Exception:
        pass
    from src.domains.runtime.routes.issues import _status_order_sql  # 懒导入：避免模块级环形依赖
    _expr = _status_order_sql()
    _idx = "idx_issue_repo_statuspri_time"
    with db_lock:
        c = db.cursor()
        if is_mysql:
            try:
                c.execute(
                    "SELECT COUNT(*) FROM information_schema.statistics "
                    "WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME='issue' "
                    "AND INDEX_NAME=%s", (_idx,))
                if c.fetchone()[0]:
                    return
                c.execute(
                    f"CREATE INDEX {_idx} ON issue (repo_key(64), ({_expr}), created_at(32))")
                db.commit()
            except Exception as e:
                log.warning(
                    f"{_idx} 建索引失败（MySQL 函数索引需 >= 8.0.13，跳过；"
                    f"默认排序深页由偏移护栏兜底）: {e}")
            return
        c.execute("SELECT name FROM sqlite_master WHERE type='index' AND name=?", (_idx,))
        if c.fetchone():
            return
        c.execute(f"CREATE INDEX {_idx} ON issue (repo_key, {_expr}, created_at DESC)")
        db.commit()


register_migration(
    version="202611040003",
    name="perf_issue_status_priority_index",
    kind="safe",
    fn=_fn_add_issue_status_priority_index,
)


def _fn_backfill_events_terminal_completed_at(conn=None) -> None:
    """Issue #1105：存量回填——终态（completed/failed/cancelled）且 completed_at
    为空的 events 行，取 created_at 回填（created_at 为空则兜底当前时间），
    恢复「终态 ⇔ completed_at 非空」不变量。

    幂等：只更新 completed_at 为空的行，重跑无副作用。
    分批回填（每批 500 条，按 seq 升序推进），避免大表单条 UPDATE 长事务/锁。
    conn 可选：测试注入独立连接；缺省用全局 db（受 db_lock 保护）。
    """
    _TERMINALS = ("completed", "failed", "cancelled")
    _BATCH = 500
    _own = conn is None
    if _own:
        conn = db
    try:
        from src.kernel.session_manager import _is_mysql_backend
    except Exception:
        _is_mysql_backend = lambda: False  # noqa: E731
    _mysql = _is_mysql_backend()
    _ph = "%s" if _mysql else "?"
    _now_fn = "NOW()" if _mysql else "datetime('now')"
    with db_lock:
        c = conn.cursor()
        _placeholders = ",".join(_ph for _ in _TERMINALS)
        while True:
            try:
                c.execute(runtime_mig.events_pending_terminal_select(_placeholders, _BATCH),
                          _TERMINALS)
                rows = c.fetchall()
                if not rows:
                    break
                seqs = [r[0] for r in rows]
                _in = ",".join(_ph for _ in seqs)
                c.execute(
                    runtime_mig.events_backfill_completed_at(_in, _placeholders, _now_fn),
                    (*seqs, *_TERMINALS))
                conn.commit()
                if len(seqs) < _BATCH:
                    break
            except Exception as e:
                log.warning(f"backfill_events_terminal_completed_at failed: {e}")
                break


register_migration(
    # 撞号修复（2026-11-06 发版实测）：本迁移原用 202611060001，与
    # create-branch-guard-state-table 同号 —— 执行器按 version 判定，后登记的
    # 那条会被判「已执行」而永久跳过（生产库当时是拿包内改号 202611060003 绕过的，
    # 分支上必须同步成同一个号，否则下次从分支打包又会撞）。
    version="202611060003",
    name="backfill_events_terminal_completed_at",
    kind="safe",
    fn=_fn_backfill_events_terminal_completed_at,
)


def _fn_backfill_events_terminal_completed_at_1497(conn=None) -> None:
    """Issue #1497：存量回填（第二轮）——终态（completed/failed/cancelled/timeout）
    且 completed_at 为空的 events 行补写 completed_at。

    口径：completed_at = COALESCE(已有非空值, updated_at, created_at, 当前时间)。
    events 表当前无 updated_at 列（列存在时优先取，语义为「最后更新时间」兜底口径），
    缺列时按 created_at 回填——与 #1105 第一轮迁移口径一致并记录于此。
    幂等：只更新 completed_at 为空的行，重跑无副作用；分批（每批 500 条，按 seq 升序）。
    conn 可选：测试注入独立连接；缺省用全局 db（受 db_lock 保护）。
    """
    _TERMINALS = ("completed", "failed", "cancelled", "timeout")
    _BATCH = 500
    _own = conn is None
    if _own:
        conn = db
    try:
        from src.kernel.session_manager import _is_mysql_backend
    except Exception:
        _is_mysql_backend = lambda: False  # noqa: E731
    _mysql = _is_mysql_backend()
    _ph = "%s" if _mysql else "?"
    _now_fn = "NOW()" if _mysql else "datetime('now')"
    with db_lock:
        c = conn.cursor()
        # updated_at 列存在性探测（events 老库无该列）
        _has_updated_at = False
        try:
            _cols = [r[1] for r in c.execute("PRAGMA table_info(events)").fetchall()]
            _has_updated_at = "updated_at" in _cols
        except Exception:
            _has_updated_at = False
        _val_expr = (
            "COALESCE(NULLIF(updated_at,''), NULLIF(created_at,''), " + _now_fn + ")"
            if _has_updated_at else
            "COALESCE(NULLIF(created_at,''), " + _now_fn + ")")
        while True:
            try:
                c.execute(
                    runtime_mig.events_pending_terminal_select(
                        ",".join(_ph for _ in _TERMINALS), _BATCH), _TERMINALS)
                rows = c.fetchall()
                if not rows:
                    break
                seqs = [r[0] for r in rows]
                _in = ",".join(_ph for _ in seqs)
                c.execute(
                    runtime_mig.events_backfill_completed_at_valued(
                        _in, ",".join(_ph for _ in _TERMINALS), _val_expr),
                    (*seqs, *_TERMINALS))
                conn.commit()
                if len(seqs) < _BATCH:
                    break
            except Exception as e:
                log.warning(f"backfill_events_terminal_completed_at_1497 failed: {e}")
                break


register_migration(
    version="202611080001",
    name="backfill_events_terminal_completed_at_1497",
    kind="safe",
    fn=_fn_backfill_events_terminal_completed_at_1497,
)


def _fn_scene_pin_columns() -> None:
    """Issue #1735：devops_pipeline_scene 补置顶列（存量库幂等补列）。

    is_pinned / pinned_at 供项目场景列表「置顶」（仓库共享、pinned_at 倒序）。
    异常必须抛出（不吞），保证执行器不盖章、下次启动重试。
    """
    with db_lock:
        c = db.cursor()
        _ensure_column(c, "devops_pipeline_scene", "is_pinned", "INTEGER NOT NULL DEFAULT 0", strict=True)
        _ensure_column(c, "devops_pipeline_scene", "pinned_at", "VARCHAR(32) DEFAULT NULL", strict=True)
        db.commit()


register_migration(
    # 2026-11-10：202611100001 已被 add-agent-tasks-trace-context 占用（rebase 到
    # weekly-mys 后同号不同名，本迁移会被判「已执行」永久跳过）→ 顺延到
    # 202611100003（202611100002 已被 create-merge-gate-log-table 占用）。
    # 迁移只做 _ensure_column 幂等补列，重跑无损。
    version="202611100003",
    name="scene-pin-columns",
    kind="safe",
    fn=_fn_scene_pin_columns,
)


# ===== 接管（takeover）v1 结构 =====
# 设计说明：接口约定见仓库内 takeover 文档。
# - takeover_sessions：接管会话（审计 + 幂等 + 状态机）
# - session_api_tokens.kind：worker | takeover（区分两类会话凭据）
# - task_messages.actor：agent | <username>（progress 复用同一份任务日志）

def _fn_takeover_client_session_v1() -> None:
    """接管会话记录"本地那个 dsh session 的 id"（面板可跳回；多端时知道是哪台机器）。"""
    with db_lock:
        c = db.cursor()
        _ensure_column(c, "takeover_sessions", "client_session", "VARCHAR(128) NOT NULL DEFAULT ''")
        db.commit()


register_migration(
    version="202611170002",
    name="takeover-client-session-v1",
    kind="safe",
    fn=_fn_takeover_client_session_v1,
)


def _fn_takeover_finish_request_v1() -> None:
    """接管会话记录"agent 提交完成申请"：时间戳 + 文件清单（finish 时直接用）。"""
    with db_lock:
        c = db.cursor()
        _ensure_column(c, "takeover_sessions", "finish_requested_at", "INTEGER NOT NULL DEFAULT 0")
        _ensure_column(c, "takeover_sessions", "finish_files", "TEXT NOT NULL DEFAULT ''")
        db.commit()


def _fn_takeover_result_json_v1() -> None:
    """接管会话的结构化结论（与 worker self-complete 同构的 5 字段）。

    result_json = {"conclusion","actions","artifacts","next_step","issue_summary"}
    finish 时既写这本列（供面板/GET 回读），也传给 update_event_complete 的
    structured_output → 事件详情页「Agent 处理记录与答复结论」结构化渲染。
    """
    with db_lock:
        c = db.cursor()
        _ensure_column(c, "takeover_sessions", "result_json", "TEXT NOT NULL DEFAULT ''")
        db.commit()


def _fn_takeover_host_os_v1() -> None:
    """接管会话记录宿主 OS / 架构 / shell（跨平台适配：服务端按它渲染脚本与 prompt）。"""
    from src.kernel.session_manager import db
    c = db.cursor()
    for col, typ in (("host_os", "TEXT DEFAULT ''"), ("host_arch", "TEXT DEFAULT ''"),
                     ("host_shell", "TEXT DEFAULT ''")):
        try:
            c.execute(runtime_mig.takeover_sessions_add_column(col, typ))
        except Exception:
            pass
    try:
        db.commit()
    except Exception:
        pass


register_migration(
    version="202611170005",
    name="takeover-host-os-v1",
    kind="safe",
    fn=_fn_takeover_host_os_v1,
)


def _fn_takeover_sessions_user_id_uuid_v1() -> None:
    """接管会话 user_id 放宽为 VARCHAR(36)：D105 后账号 id 是 uuid 字符串。

    存量库该列是 INTEGER —— `int(user["id"])` 直接 ValueError（接管接口 500 的真因），
    MySQL 侧即便吞掉也只会把 uuid 截成 0 → 会话归属校验永远不相等。
    两条约束：
    - MODIFY 属破坏性 DDL（Issue #536 D4-2），必须走 `_destructive_ddl` 的
      min_code_version 门槛；被拦下时不盖章（raise），下次启动再试；
    - `takeover_sessions` 归 runtime 域：三库拓扑下 mgmt/insight 进程库里可能有同名
      历史表，真正被接管接口读写的是 runtime 域库 —— 有自有 DSN 时再补一次。
    """
    from src.kernel import db_registry
    from src.kernel.mysql_compat import get_db_backend

    if get_db_backend() == "sqlite":
        return  # sqlite 动态类型接受文本；新库 DDL 已是 VARCHAR(36)

    _ddl = ("ALTER TABLE takeover_sessions MODIFY COLUMN user_id "
            "VARCHAR(36) NOT NULL DEFAULT '0'")

    def _widen(cur) -> bool:
        """True=该库无需再改/已改完；False=被 destructive 门槛拦下。"""
        cur.execute(
            "SELECT DATA_TYPE FROM information_schema.columns "
            "WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME='takeover_sessions' "
            "AND COLUMN_NAME='user_id'")
        row = cur.fetchone()
        dtype = str((row[0] if row else "") or "").lower()
        if dtype not in ("int", "bigint", "smallint", "tinyint", "mediumint"):
            return True
        return bool(_destructive_ddl(cur, _ddl, kind_hint="cleanup"))

    blocked = False
    with db_lock:
        c = db.cursor()
        try:
            if not _widen(c):
                blocked = True
        finally:
            db.commit()

    if db_registry.uses_own_dsn("runtime") and db_registry.current_domain() != "runtime":
        try:
            with db_registry.transaction("runtime") as rc:
                if not _widen(rc):
                    blocked = True
        except Exception as e:  # noqa: BLE001 - 连接/DDL 失败都不盖章，下次重试
            log.warning(f"takeover_sessions.user_id widen (runtime domain) failed: {e}")
            blocked = True

    if blocked:
        raise RuntimeError(
            "takeover_sessions.user_id 放宽被 min_code_version 门槛拦截或 runtime 域执行失败"
            "（本次不盖章，下次启动重试）")
    log.info("takeover_sessions.user_id widened to VARCHAR(36) (uuid) where applicable")


register_migration(
    version="202611200001",
    name="takeover-sessions-user-id-uuid-v1",
    kind="cleanup",  # 含 MODIFY COLUMN：破坏性 DDL，走 min_code_version 门槛
    fn=_fn_takeover_sessions_user_id_uuid_v1,
)


register_migration(
    version="202611170004",
    name="takeover-result-json-v1",
    kind="safe",
    fn=_fn_takeover_result_json_v1,
)


register_migration(
    version="202611170003",
    name="takeover-finish-request-v1",
    kind="safe",
    fn=_fn_takeover_finish_request_v1,
)


def _fn_takeover_schema_v1() -> None:
    """接管功能的一次性结构迁移（幂等；SQLite/MySQL 双后端）。

    active_key 承担"一条任务只能有一个进行中接管"：active 时写 task_id、结束置 NULL。
    用 NULL 唯一索引而不是 `WHERE status='active'` 部分索引 —— MySQL 不支持部分索引，
    而两种后端都允许多个 NULL。
    """
    with db_lock:
        c = db.cursor()
        c.execute(_TAKEOVER_SESSIONS_DDL)
        # strict=True：补列失败必须 raise（迁移不盖章、下次启动重试）。否则版本号被
        # 盖死而列永不创建，线上表现为「查询该列 1054 → 鉴权静默 401」
        #（2026-11-13 事故：session_api_tokens.kind 缺失导致所有 tk_ 会话凭据 401）。
        _ensure_column(c, "session_api_tokens", "kind", "VARCHAR(16) NOT NULL DEFAULT 'worker'", strict=True)
        _ensure_column(c, "task_messages", "actor", "VARCHAR(128) NOT NULL DEFAULT 'agent'", strict=True)
        _ensure_column(c, "takeover_sessions", "token_issued_at", "INTEGER NOT NULL DEFAULT 0", strict=True)
        _ensure_column(c, "takeover_sessions", "handoff_seq", "INTEGER NOT NULL DEFAULT 0", strict=True)
        _ensure_column(c, "takeover_sessions", "next_agent", "VARCHAR(32) NOT NULL DEFAULT ''", strict=True)
        db.commit()

        # 索引：MySQL 不支持 CREATE INDEX IF NOT EXISTS → 逐个 try，重复即忽略。
        # session_key 是 TEXT，MySQL 索引必须给前缀长度（否则 1170）。
        _sat_key_col = "session_key(64)" if _mysql_backend() else "session_key"
        for ddl in (
            f"CREATE INDEX IF NOT EXISTS ix_sat_kind_key ON session_api_tokens(kind, {_sat_key_col})",
            "CREATE UNIQUE INDEX IF NOT EXISTS ux_takeover_active ON takeover_sessions(active_key)",
            "CREATE INDEX IF NOT EXISTS ix_takeover_task ON takeover_sessions(task_id)",
            "CREATE INDEX IF NOT EXISTS ix_takeover_status ON takeover_sessions(status, updated_at)",
        ):
            try:
                c.execute(ddl)
            except Exception as e:
                msg = str(e).lower()
                if "already exists" not in msg and "duplicate" not in msg:
                    log.warning(f"takeover schema index skipped: {ddl} ({e})")
        db.commit()


register_migration(
    version="202611170001",
    name="takeover-sessions-v1",
    kind="safe",
    fn=_fn_takeover_schema_v1,
)

def _fn_repair_session_token_kind_v1() -> None:
    """补 `session_api_tokens.kind`（owner=mgmt 域库）——2026-11-13 事故修复。

    事故链：`_fn_takeover_schema_v1` 的 `_ensure_column` 当时不 strict，在非 owner
    库（该表是跨库视图）ALTER 报 1347 后只记 MANUAL REVIEW 就正常返回，迁移照样
    盖章 → 列永不创建；而 `verify_session_token()` 的 SQL 带
    `COALESCE(kind,'worker')`，MySQL 直接 1054 → 异常被吞返回 None → 所有 tk_ 会话
    凭据 401 → Agent 任务「前置检查失败」，流水线自动停摆且退出码 0（无告警）。

    只补列（幂等）。视图侧仍需重建（owner=mgmt → runtime/insight）：
      `python3 scripts/sync_domain_views.py --owner grape_mgmt_db \\
         --target grape_runtime_db,grape_insight_db --tables session_api_tokens`
    """
    with db_lock:
        c = db.cursor()
        _ensure_column(c, "session_api_tokens", "kind",
                       "VARCHAR(16) NOT NULL DEFAULT 'worker'", strict=True)
        db.commit()
        # session_key 是 TEXT → MySQL 索引用前缀长度（否则 1170）
        _key_col = "session_key(64)" if _mysql_backend() else "session_key"
        try:
            c.execute(f"CREATE INDEX IF NOT EXISTS ix_sat_kind_key "
                      f"ON session_api_tokens(kind, {_key_col})")
        except Exception as e:
            _msg = str(e).lower()
            if "already exists" not in _msg and "duplicate" not in _msg:
                log.warning(f"repair ix_sat_kind_key skipped: {e}")
        db.commit()


register_migration(
    version="202611160006",
    name="repair-session-token-kind-v1",
    kind="safe",
    fn=_fn_repair_session_token_kind_v1,
)


def _fn_repair_task_messages_actor_v1() -> None:
    """补 `task_messages.actor`（owner=runtime 域库）——同批次静默漏列修复。

    `_fn_takeover_schema_v1` 在 mgmt/insight（该表为视图）同样 ALTER 失败后照常
    盖章，导致非 owner 侧读 `actor` 会 1054。视图侧重建：
      owner=runtime → targets mgmt,insight
    """
    with db_lock:
        c = db.cursor()
        _ensure_column(c, "task_messages", "actor",
                       "VARCHAR(128) NOT NULL DEFAULT 'agent'", strict=True)
        db.commit()


register_migration(
    version="202611160007",
    name="repair-task-messages-actor-v1",
    kind="safe",
    fn=_fn_repair_task_messages_actor_v1,
)


def _fn_checksum(fn: Callable[[], None]) -> str:
    """fn 指纹 sha256 前 16 位（漂移检测用；取不到返回空串不阻断）。

    优先 inspect.getsource（源码级）；闭包/exec 定义取不到源码时回退
    dis 反汇编指纹（注释级差异不可见，但语义变更可见——漂移检测降级可用）。
    """
    try:
        src = inspect.getsource(fn)
        return hashlib.sha256(src.encode("utf-8")).hexdigest()[:16]
    except Exception:
        pass
    try:
        import dis
        bytecode = dis.Bytecode(fn).dis()
        consts = repr(fn.__code__.co_consts)
        return hashlib.sha256((bytecode + consts).encode("utf-8")).hexdigest()[:16]
    except Exception:
        return ""


# PR #2009 I1：MDL 异常与判定下沉 kernel，db_setup 只保留同名别名；
# 域迁移（kernel.ensure_column 默认实现）与本模块抛/捕获的是同一组类。
_MdlWaitTimeout = _ddl_safety.MdlWaitTimeout
_is_mdl_timeout = _ddl_safety.is_mdl_timeout


def _query_mdl_holders(err: Exception) -> str:
    """MDL 超时时附 processlist 摘要（复用 _query_lock_holder_info 思路，
    从 db 连接查当前等待 metadata lock 的会话及其阻塞源；失败返回空串）。"""
    try:
        with db_lock:
            c = db.cursor()
            c.execute(
                "SELECT id, user, host, db, time, state "
                "FROM information_schema.processlist "
                "WHERE state LIKE '%metadata lock%' ORDER BY time DESC LIMIT 8")
            rows = c.fetchall() or []
        if not rows:
            return ""
        return "; ".join(
            "processlist_id=%s user=%s host=%s db=%s time=%ss state=%s" % r
            for r in rows)
    except Exception:
        return ""


def _get_meta_value_conn(conn, key: str) -> str:
    """按给定连接读 meta（执行器自用，避免与全局 db 连接耦合）。"""
    try:
        c = conn.cursor()
        c.execute(runtime_mig.SELECT_META__GET_META_VALUE_CONN, (key,))
        row = c.fetchone()
        return row[0] if row else ""
    except Exception:
        return ""


def _set_meta_value_conn(conn, key: str, value: str) -> None:
    """按给定连接写 meta。"""
    try:
        c = conn.cursor()
        c.execute(runtime_mig.UPSERT_META__SET_META_VALUE_CONN, (key, value))
        conn.commit()
    except Exception as e:
        log.warning(f"_set_meta_value_conn failed: {key}: {e}")


def get_min_code_version() -> str:
    """meta 表 min_code_version（空串=未设置，cleanup 不受门槛约束）。"""
    try:
        from src.domains.mgmt.user.db import get_meta_value
        return get_meta_value("min_code_version") or ""
    except Exception:
        return ""


def set_min_code_version(value: str) -> None:
    """部署脚本在全部集群升级完成后写入（与 DB 状态同源，多节点读到同一值）。"""
    try:
        from src.domains.mgmt.user.db import set_meta_value
        set_meta_value("min_code_version", value)
    except Exception as e:
        log.warning(f"set_min_code_version failed: {e}")


def current_code_version() -> str:
    """主控自身代码版本（src/api/code_version.py；门槛比较用）。"""
    try:
        from src.api.code_version import current_code_version as _ccv
        return _ccv()
    except Exception:
        return ""


def _version_ge(self_ver: str, min_ver: str) -> bool:
    """版本串比较：带数值点分前缀的形态（日期式 2026.12.01 / git 式
    g-2026.10.30-abc123）逐段数值比较；无数值前缀形态（纯哈希/指纹）退化
    为字符串相等判定——保守处理（QA R2：自身版本与 min_code_version 必须
    同形可比，否则 cleanup 永久跳过）。"""
    if not min_ver:
        return True
    if not self_ver:
        return False
    try:
        a = [int(p) for p in self_ver.replace("-", ".").split(".") if p.isdigit()]
        b = [int(p) for p in min_ver.replace("-", ".").split(".") if p.isdigit()]
        if a and b:
            return a >= b
    except Exception:
        pass
    return self_ver == min_ver


def _cleanup_gate_passed() -> bool:
    """cleanup 门槛：meta 未设 min_code_version 或自身版本 ≥ 该值才放行。"""
    min_ver = get_min_code_version()
    if not min_ver:
        return True
    self_ver = current_code_version()
    ok = _version_ge(self_ver, min_ver)
    if not ok:
        log.info(f"cleanup gate: 自身版本 {self_ver} < meta.min_code_version {min_ver}"
                 f"（本实例为落后者，跳过 cleanup，下版本再试）")
    return ok


def _destructive_ddl(c, ddl: str, *, kind_hint: str = "cleanup",
                      version: str = "") -> bool:
    """危险 DDL（DROP/RENAME/MODIFY COLUMN）唯一入口（Issue #536 D4-2）。

    - kind_hint 必须是 cleanup（调用点即 cleanup 语义；防误用）；
    - MySQL 侧过 _cleanup_gate_passed()（meta min_code_version）才执行；
      未过 → log.info 跳过并记入 _LEGACY_BLOCKED_DDL（返回 False，不抛错；
      该迁移不盖章，下版本再试——QA R3：legacy-stamp 同样遵守「跳过不盖章」）；
    - SQLite 侧（单实例、无蓝绿共存问题）直接执行。

    返回值：True = 已执行（或无需执行）；False = 被门槛拦截跳过。调用点应自查
    返回值：跳过时不打成功日志、不向调用链传播「已迁移」结论；注册表迁移 fn
    中被跳过时应 raise 跳过盖章（执行器以「fn 正常返回 = 盖章」为约定）。
    """
    if kind_hint != "cleanup":
        raise ValueError(f"_destructive_ddl 仅接受 cleanup 语义，got kind_hint={kind_hint}")
    if _mysql_backend() and not _cleanup_gate_passed():
        log.info(f"_destructive_ddl 跳过（min_code_version 门槛未过）: {ddl}")
        _LEGACY_BLOCKED_DDL.append(ddl)
        return False
    try:
        c.execute(ddl)
    except Exception as e:
        if _is_mdl_timeout(e):
            _LEGACY_BLOCKED_DDL.append(f"{ddl}（MDL 超时）")
            raise _MdlWaitTimeout(str(e))
        # 已经处于目标状态 = 成功（重复删列/建列、列不存在等）：这类异常不阻断启动。
        _code = getattr(e, "args", (None,))[0] if getattr(e, "args", None) else None
        if isinstance(_code, int) and _code in (1050, 1051, 1054, 1060, 1061, 1091, 1146):
            log.debug(f"_destructive_ddl already in target state: {ddl}: {e}")
            return True
        # 其它失败必须抛出：调用链约定「fn 正常返回 = 盖章」，吞掉异常会让
        # 审计写成 after 且下次启动不再重试（2026-11-22 评审④：假绿）。
        log.warning(f"_destructive_ddl failed, not stamped: {ddl}: {e}")
        raise
    return True


# ===== Issue #3469 O：uq_active_email 收敛为「仅活跃状态唯一」=====
# 实现（SQLite 部分索引 / MySQL STORED 生成列 / 复查 / ''→NULL 归一）在
# `src/domains/mgmt/migrations/active_email_uniqueness.py`（迁移对象）：
# 巨型文件只留注册接线，方言与 DDL 集中在迁移模块里可单测、可复查。
from src.domains.mgmt.migrations.active_email_uniqueness import (  # noqa: E402
    ActiveEmailUniquenessMigration,
    ensure_active_email_index,
    migrate_active_email_uniqueness,
)

# ===== Issue #3469 I 复审：code_hash 装不下 "{kid}:{digest}" =====
# 实现（MySQL MODIFY 加宽 + 复查；SQLite 无长度语义）在
# `src/domains/mgmt/migrations/verification_hash_width.py`。
from src.domains.mgmt.migrations.verification_hash_width import (  # noqa: E402
    VerificationHashWidthMigration,
    migrate_verification_hash_width,
)

register_migration(
    version="202611190102",
    name=VerificationHashWidthMigration.NAME,
    kind="safe",
    fn=migrate_verification_hash_width,
    domains=("mgmt",),
)


register_migration(
    # 版本号 = 源码当前最大号 202611190100（_ORG_TEXT_MIGRATION_VERSION）+ 1。
    # 必须写字面量：tests/api/test_migration_track_guard.py 用 ast 静态解析注册链。
    version="202611190101",
    name=ActiveEmailUniquenessMigration.NAME,
    # kind=backfill：本迁移会 UPDATE 存量行（email='' → NULL），不是纯 DDL。
    kind="backfill",
    fn=migrate_active_email_uniqueness,
    domains=("mgmt",),
)


def _backfill_in_batches(c, sql_template: str, params: tuple = (),
                         batch_size: int = 1000, max_batches: int = 0) -> int:
    """backfill 迁移的标准分批执行器（Issue #536 kind=backfill 契约）。

    规则（设计文档 D2）：
    - 每批 LIMIT {batch_size}（默认 1000）循环；
    - **每批独立事务**（批内 commit，长事务会持 MDL/行锁阻塞在线业务）；
    - 单批失败返回已完成的批数（不抛错）——调用方（迁移 fn）据此决定是否
      抛错跳过盖章；幂等 WHERE 条件保证下次续跑。

    sql_template 必须自带幂等筛选与排序（如 WHERE x IS NULL ORDER BY id
    LIMIT ?），本函数只负责分批循环 + 独立事务 + 失败隔离。
    max_batches=0 表示不设上限（跑满为止）。
    """
    done = 0
    batches = 0
    while True:
        batches += 1
        if max_batches and batches > max_batches:
            break
        try:
            with db_lock:
                affected = c.execute(sql_template, (*params, batch_size))
                n = getattr(affected, "rowcount", 0) or 0
                db.commit()
            if not n:
                break
            done += n
            if n < batch_size:
                break
        except Exception as e:
            log.warning(f"_backfill_in_batches 第 {batches} 批失败（已完成 {done} 行），"
                        f"跳过剩余批次（下次续跑）: {e}")
            break
    return done


def _set_migration_session_lock_timeout(dconn, seconds: int = 10) -> None:
    """在给定连接/会话上 SET SESSION lock_wait_timeout（仅 MySQL）。

    DDL 拿 MDL 超时（默认无限等待）→ 报错由执行器捕获跳过该条迁移，
    不无限等待、不影响业务会话（超时杀的是迁移语句本身）。

    QA R1：锁超时必须下发在**迁移 DDL 所在会话**，而非 GET_LOCK 专用连接
    （DDL 不经专用连接执行）。生产路径由 _ensure_migration_session_lock_timeout()
    对全局 db（迁移 DDL 会话）下发；本函数保留通用 setter（含专用连接场景）。
    """
    try:
        cur = dconn.cursor()
        cur.execute(f"SET SESSION lock_wait_timeout = {int(seconds)}")
    except Exception as e:
        log.warning(f"SET SESSION lock_wait_timeout failed: {e}")


def _ensure_migration_session_lock_timeout(seconds: int = 10) -> None:
    """在迁移 DDL 所在会话（全局 db）上 SET SESSION lock_wait_timeout（仅 MySQL）。

    迁移 fn 内部经 db.cursor()/db.execute() 执行 DDL（MySQLCompatDB 连接池）；
    GET_LOCK 专用连接是**不同会话**，对其 SET SESSION 对 DDL 无效（QA R1）。
    执行器在每条 pending 迁移前重新下发：池连接可能在 fn 内 commit 后还池并
    重新借出，逐条重发保证 DDL 语句所在会话始终带锁超时。
    """
    if not _mysql_backend():
        return
    try:
        _set_migration_session_lock_timeout(db, seconds)
    except Exception as e:
        log.warning(f"SET SESSION lock_wait_timeout on migration session failed: {e}")


_MIGRATION_DOMAINS = ("mgmt", "runtime", "insight")


def _process_role() -> str:
    """进程角色（server / event / worker / insight）。"""
    for key in ("GRAPE_PROCESS_ROLE", "GRAPE_SERVICE_NAME"):
        val = (os.environ.get(key) or "").strip().lower()
        if val:
            return val
    return ""


def current_migration_domain() -> str:
    """本进程负责的迁移域（只在三库分库模式下返回非空）。

    三级判定（2026-11-21 补齐后两级）：

      ① `GRAPE_MIGRATION_OWNER` 显式声明 → 直接用（部署脚本的权威来源）；
      ② 缺省/非法 → 由本进程库名推断（`<db_runtime>` → runtime）；
      ③ 仍判不出 → 由进程角色映射（event → runtime / insight → insight / server → mgmt）。

    **为什么必须补 ② ③**：owner 为空时本函数返回空串 = 不筛域，mgmt 进程会以
    「全量迁移」形态把别域 DDL 建进自己的库，而 event/insight 的域迁移入口会直接
    return（本域库永远拿不到该有的列和台账表）。实测后果：迁移 202611200006 把
    `agent_runtimes.desired_plugins` 建进了 `<db_mgmt>`（该表 0 行），而
    `<db_runtime>` 有 19 行却缺这一列 → 引擎注册连续 500 unknown column。
    部署脚本漏一个 env 就能造成这种「盖章在错库、列没建」的假绿，必须由代码兜住。

    - 单库部署（`GRAPE_DOMAIN_LIB` 非 1）仍返回空串 = 跑全量，历史行为不变。
    - 返回空串的语义是「不筛」，调用方据此决定是否按 `Migration.domains` 过滤。
    """
    if (os.environ.get("GRAPE_DOMAIN_LIB") or "").strip() not in ("1", "true", "yes"):
        return ""
    dom = (os.environ.get("GRAPE_MIGRATION_OWNER") or "").strip().lower()
    if dom in _MIGRATION_DOMAINS:
        return dom
    # ② 库名推断（DEVOPS_DB_NAME 优先，其次角色名）
    try:
        from src.kernel import db_registry as _dbreg

        inferred = (_dbreg.current_domain() or "").strip().lower()
        if inferred in _MIGRATION_DOMAINS:
            return inferred
    except Exception as e:  # noqa: BLE001 - 推断失败继续走角色映射
        log.debug(f"current_migration_domain: db_registry 推断失败: {e}")
    # ③ 角色映射
    role = _process_role()
    if role in ("server", "mgmt", "main"):
        return "mgmt"
    if role == "event":
        return "runtime"
    if role == "insight":
        return "insight"
    return ""


def filter_migration_registry(registry: Optional[List[Migration]]) -> List["Migration"]:
    """按当前进程的迁移域过滤登记表；单库/无 owner 时原样返回。"""
    owner = current_migration_domain()
    if not owner:
        return list(registry or [])
    return [m for m in (registry or []) if _migration_domain_allows(m, owner)]


def _migration_domain_allows(m: "Migration", owner: str) -> bool:
    """owner 非空时：只允许 owner 域登记过的迁移（缺省登记域=mgmt）。"""
    if not owner:
        return True
    return owner in (m.domains or ("mgmt",))


def _run_migrations_with_conn(conn, registry: Optional[List[Migration]] = None,
                              mdl_holders=None, allow_legacy: bool = True) -> None:
    """执行器核心（可注入连接，供测试与生产共用同一语义）。

    流程：读版本表 → legacy-stamp 判定（表空则先全量 _run_all_migrations 并盖帽）
    → 只执行未盖章项 → 逐条盖章（duration_ms 实测 / checksum=fn 源码指纹）；
    已盖章 checksum 漂移只告警不阻断（fail-safe）。

    约定（与设计文档 D1/D2 对齐）：
    - fn 正常返回 = 盖章；fn 抛错 = 不盖章（下次幂等重试）；
    - backfill/cleanup 失败跳过不阻断后续迁移（可用性优先）；
    - legacy-stamp 无 fn，盖章即收敛。
    """
    if registry is None:
        registry = _MIGRATIONS
    # 三库模式：域外登记条目在这里统一剔除。否则被 `_owned_skip` 跳过 DDL 的迁移
    # 仍会因「预检看到外来表=无事可做」而正常返回 → 在本域台账盖章，形成
    # 「盖章表有记录、列没建」的假绿（review F1/F4）。
    _filtered = filter_migration_registry(registry)
    if len(_filtered) != len(registry):
        log.info(f"域迁移过滤（owner={current_migration_domain()}）："
                 f"{len(registry)} → {len(_filtered)} 条（域外条目由对应域进程负责）")
    registry = _filtered
    if mdl_holders is None:
        mdl_holders = _query_mdl_holders

    def _applied_versions() -> dict:
        try:
            c = conn.cursor()
            c.execute(mgmt_mig.SELECT_GRAPE_T_SCHEMA_MIGRATIONS)
            return {r[0]: ((r[1] or ""), (r[2] or "")) for r in c.fetchall()}
        except Exception as _read_err:
            # Issue #1412 QA Optional：区分「读失败」与「版本表为空」，避免线上
            # 只能看到 legacy 全量重放却无从定位异常。全新库尚无该表时 SELECT
            # 也会失败，属预期，故仅 warning 不做 fail-closed。
            log.warning(f"读取 grape_t_schema_migrations 失败，按空表处理: {_read_err}")
            return {}

    applied = _applied_versions()

    # ── legacy-stamp：版本表无任何盖章行 → 存量库首跑 ──
    # allow_legacy=False（三库模式的 non-mgmt 域进程）：域库台账天然是"空"的，
    # 绝不能再跑老轨全量迁移（那是 mgmt 单库语义，会把别域 DDL 打在错库上）。
    if not applied and not allow_legacy:
        log.info(f"域库台账为空：只跑本域登记迁移（{len(registry)} 条），不跑 legacy 全量")
    if not applied and allow_legacy:
        _LEGACY_BLOCKED_DDL.clear()   # 每次首跑前重置，防跨启动残留
        log.info("grape_t_schema_migrations 为空：按 legacy-stamp 首跑全量迁移"
                 "（行为与历史版本完全一致），完成后一次性盖帽收敛")
        try:
            _run_all_migrations()
        except _MdlWaitTimeout as _legacy_mdl:
            # 全量迁移中 MDL 超时未被子函数吞掉（理论上被子 try/except 消化，
            # 防御兜底）：不打帽，下轮启动重试
            log.error(f"legacy 全量迁移 MDL 超时（{_legacy_mdl}），legacy-stamp 不打帽")
        if _LEGACY_BLOCKED_DDL:
            # QA R3：被门槛/MDL 拦截的破坏性 DDL 未执行——不打 legacy-stamp 帽
            #（版本表保持为空）→ 升级完成后下次启动重跑全量补执行；
            # 与设计文档「未过→跳过不盖章（下版本再试）」一致。
            log.error(
                f"legacy-stamp 不打帽：{len(_LEGACY_BLOCKED_DDL)} 条 DDL 被拦截"
                f"（min_code_version 门槛未过 / MDL 超时），升级后下次启动自动重试。"
                f"清单: {_LEGACY_BLOCKED_DDL}")
            return
        try:
            c = conn.cursor()
            c.execute(
                mgmt_mig.UPSERT_GRAPE_T_SCHEMA_MIGRATIONS,
                (_LEGACY_STAMP_VERSION, _LEGACY_STAMP_NAME, "legacy-stamp",
                 _now_iso(), 0, ""))
            conn.commit()
            log.info(f"legacy-stamp 盖帽完成: version={_LEGACY_STAMP_VERSION}"
                     f"（此后启动只跑增量迁移）")
        except Exception as e:
            log.warning(f"legacy-stamp 盖章失败（下次启动重跑全量，幂等兜底）: {e}")
        # 盖帽后重读（上面全量迁移可能已含注册表内的早期迁移语义，直接进入增量判定）
        applied = _applied_versions()

    # 同号不同名守卫（Issue #1412 / #3648）：撞号 ≠ 普通 checksum 漂移。
    # 存量库可能已被其它分支的同号迁移盖章（name 不同），此时 pending 判定按
    # version 会把它永久判「已执行」，本分支的迁移 fn 永不执行。处理策略：
    #   · 迁移带 post_check → 先用后置校验判目标是否真的到位；
    #       - 已到位：视为已执行（只留痕，不重复 ALTER）；
    #       - 未到位：加入 reconcile 队列，重跑幂等 fn + 再校验，通过后才改盖新名。
    #   · 无 post_check → 保留 fail-safe 跳过，但必须 ERROR 报出（禁止静默）。
    # 本 Issue 的两条撞号迁移已 renumber 到未占用号（见 202611210001/2/3），
    # 上面的 reconcile 是给历史库 / 后续跨分支撞号兜底。
    _reconcile: List[Migration] = []
    for m in registry:
        if m.version not in applied:
            continue
        recorded_name = applied[m.version][0]
        if not (recorded_name and recorded_name != m.name):
            continue
        if getattr(m, "post_check", None) is None:
            log.error(
                f"迁移版本号撞号（同号不同名，非普通 checksum 漂移）: version={m.version} "
                f"库内盖章名={recorded_name} 本次注册名={m.name}。该号在目标库已被其它分支的"
                f"迁移占用，本次迁移会被判「已执行」而永久跳过、其 DDL 不会执行。"
                f"修复指引：把本次迁移 renumber 到未占用号（日期+序号顺延），并先核对目标库 "
                f"版本台账表 version/name 两列的现值。")
            continue
        try:
            m.post_check()
        except _NonOwnerObject as e:
            log.error(
                f"迁移撞号后置校验无法在本域执行: version={m.version} {m.name}: {e}；"
                f"不重跑（由 owner 域进程收敛）")
        except Exception as e:
            log.error(
                f"迁移版本号撞号且目标未到位（同号不同名）: version={m.version} "
                f"库内盖章名={recorded_name} 本次注册名={m.name}，后置校验失败: {e}。"
                f"将重跑幂等迁移并再次校验后再盖章（禁止静默跳过）。")
            _reconcile.append(m)
        else:
            log.warning(
                f"迁移版本号撞号（同号不同名）但目标已到位: version={m.version} "
                f"库内盖章名={recorded_name} 本次注册名={m.name}；按已执行处理，不重复执行。")

    # checksum 漂移告警（fail-safe：不阻断不重跑；先于 pending 判定，0 pending 时也要查）
    for m in registry:
        if m.version in applied and m.fn is not None:
            recorded = applied[m.version][1]
            current = _fn_checksum(m.fn)
            if recorded and current and recorded != current:
                log.warning(
                    f"grape_t_schema_migrations checksum 漂移（只告警不阻断）: {m.version} {m.name}"
                    f" recorded={recorded} current={current}")

    pending = [m for m in registry if m.version not in applied]
    if _reconcile:
        _seen = {m.version for m in pending}
        pending.extend(m for m in _reconcile if m.version not in _seen)
        pending.sort(key=lambda m: m.version)
        log.error(f"grape_t_schema_migrations: {len(_reconcile)} 条撞号迁移目标未到位，"
                  f"纳入本次重跑（{', '.join(m.version + ' ' + m.name for m in _reconcile)}）")
    if not pending:
        log.info("grape_t_schema_migrations: 0 pending（全部已盖章，启动迁移段收敛）")
        return
    log.info(f"grape_t_schema_migrations: {len(pending)} pending / {len(registry)} registered")

    for mig in pending:
        started = _time_mod.monotonic()
        try:
            if mig.fn is not None:
                # QA R1：每条迁移前在 DDL 所在会话下发锁超时（commit 后池连接
                # 可能更换，逐条重发保证生效）
                _ensure_migration_session_lock_timeout()
                mig.fn()
        except _MdlWaitTimeout as e:
            holders = mdl_holders(e)
            log.error(
                f"迁移 {mig.version} {mig.name} MDL 等待超时，跳过不盖章（下次续跑）: {e}"
                + (f"；MDL 持有者摘要: {holders}" if holders else ""))
            continue
        except _NonOwnerObject as e:
            # 非 owner 域库（该表在本库是视图）——不能 ALTER，也不能盖章，
            # 等 owner 库进程补列 + 重建视图后自然收敛。
            log.info(f"迁移 {mig.version} {mig.name} 跳过不盖章（非 owner 视图，等 owner 库收敛）: {e}")
            continue
        except Exception as e:
            # backfill/cleanup 失败跳过不阻断启动（可用性优先，幂等下次续跑）
            log.warning(f"迁移 {mig.version} {mig.name}（{mig.kind}）失败，跳过不盖章: {e}")
            continue
        # 后置校验（#3648）：执行成功 ≠ 目标到位。校验失败绝不盖章，下次启动重试。
        if getattr(mig, "post_check", None) is not None:
            try:
                mig.post_check()
            except _NonOwnerObject as e:
                log.info(f"迁移 {mig.version} {mig.name} 后置校验非 owner 视图，跳过不盖章: {e}")
                continue
            except Exception as e:
                log.error(
                    f"迁移 {mig.version} {mig.name} 后置校验失败，不盖章（目标未到位，下次启动重试）: {e}")
                continue
        duration_ms = int((_time_mod.monotonic() - started) * 1000)
        try:
            c = conn.cursor()
            c.execute(
                mgmt_mig.UPSERT_GRAPE_T_SCHEMA_MIGRATIONS__RUN_MIGRATIONS_WITH_CONN,
                (mig.version, mig.name, mig.kind, _now_iso(), duration_ms,
                 _fn_checksum(mig.fn) if mig.fn else ""))
            conn.commit()
            log.info(f"迁移盖章: {mig.version} {mig.name}（{mig.kind}，{duration_ms}ms）")
        except Exception as e:
            log.warning(f"迁移 {mig.version} 盖章失败（fn 已执行，幂等兜底）: {e}")

    # DDL 人工清单汇总（_ensure_column 两级降级均失败的项目）
    if _DDL_MANUAL_REVIEW:
        for entry in _DDL_MANUAL_REVIEW:
            log.error(f"[Issue #536 DDL 人工处理清单] {entry}")


def verify_migration_targets(conn=None, domain: Optional[str] = None,
                             raise_on_fail: bool = False) -> list:
    """启动期后置校验（Issue #3648）：台账之外再核对一次关键目标是否到位。

    检查项（域内目标表必须存在——QA：整表缺失 = 迁移目标没建出来，
    不能用 _table_present 包裹后静默 log.info 通过，必须判为失败）：
    - mgmt（单库/不筛域）：分类字典表必须存在，且一级活跃必须恰好 KEEP_L1_IDS；
    - mgmt（单库/不筛域）：autopilot_tasks 状态三列的 COLUMN_DEFAULT 必须是空串
      （`autopilot_dispatch_state.state_column_defaults_converged`）。加这一项的
      原因：202611240011 的判据在 PR #2157 前几版里把 NULL 与 '' 折成同一态，
      迁移空转却盖章（假绿），那些「已盖章」的库启动时不会再跑 fn——只有把同一
      后置校验挂到台账之外的核对点，旧判据盖章的库才会在启动日志暴露漂移；
      SQLite 侧列定义本身带 DEFAULT ''，该函数直接通过（N/A）。
    - runtime（单库/不筛域）：skill 表必须存在，且必须含 cat_l1_id / cat_l2_id。

    实现口径：检查项各委托属主域迁移模块的后置校验函数（表缺失/
    集合不对/缺列均以 RuntimeError 报出，这里转成失败清单），api 层不再
    复制域内 SQL / id 字面量（tests/arch P0-2 棘轮 + QA C-1 单一真源）。
    表整张缺失 = 迁移目标没建出来，同样判失败（QA：不允许 _table_present
    包裹后静默 log.info 通过）。

    返回失败清单；`raise_on_fail=True` 时抛 RuntimeError。默认只 ERROR 留痕，
    避免把「已盖章但目标缺失」的历史库直接变成启动崩溃（迁移本身已在盖章前
    post_check 拦截，不会产生新的假绿）。
    """
    conn = db if conn is None else conn
    dom = (domain if domain is not None else current_migration_domain()) or ""
    failures: list = []
    try:
        cur = conn.cursor()
    except Exception as e:  # noqa: BLE001 - 连接不可用不阻断启动
        failures.append(f"后置校验无法取得游标: {e}")
        cur = None
    if cur is not None:
        if dom in ("", "mgmt"):
            try:
                from src.domains.mgmt.migrations.category_collapse_dict import (
                    verify_category_collapse,
                )
                verify_category_collapse(conn=conn)
            except Exception as e:  # noqa: BLE001
                failures.append(f"mgmt 分类字典后置校验失败: {e}")
            try:
                from src.domains.mgmt.migrations.org_repo_create_options import (
                    verify_org_repo_create_columns,
                )
                verify_org_repo_create_columns(conn=conn)
            except Exception as e:  # noqa: BLE001
                failures.append(f"mgmt organization 仓库创建选项列后置校验失败: {e}")
            try:
                # PR #2157 复审 Required：台账之外再核一次状态三列默认值。旧判据
                # （把 COLUMN_DEFAULT NULL 当已收敛）盖章的库不会重跑 202611240011，
                # 只有这里能让「已盖章但目标未到位」在启动日志暴露。
                from src.domains.mgmt.migrations.autopilot_dispatch_state import (
                    state_column_defaults_converged,
                )
                state_column_defaults_converged(conn=conn)
            except Exception as e:  # noqa: BLE001
                failures.append(f"mgmt autopilot 状态列默认值后置校验失败: {e}")
        if dom in ("", "runtime"):
            try:
                from src.domains.runtime.migrations.skill_detail_tables import (
                    verify_skill_classification_columns,
                )
                verify_skill_classification_columns(conn=conn)
            except Exception as e:  # noqa: BLE001
                failures.append(f"runtime skill 列后置校验失败: {e}")
    if failures:
        msg = "迁移目标后置校验失败（台账与目标不一致，需按域重跑）: " + "; ".join(failures)
        log.error(msg)
        if raise_on_fail:
            raise RuntimeError(msg)
    else:
        log.info("迁移目标后置校验通过（分类一级集合 / skill 分类列 / organization 仓库列"
                 " / autopilot 状态列默认值）")
    return failures


def _now_iso() -> str:
    return _time_mod.strftime("%Y-%m-%d %H:%M:%S", _time_mod.localtime())


def run_pending_migrations() -> None:
    """init_database() 的迁移入口（替代直接调 _run_all_migrations）。

    与 init_database 的锁/连接协作：版本表读写走全局 db 连接（与 GET_LOCK
    无关，不要求同连接）。MySQL 分支在迁移前对**迁移 DDL 所在会话（全局 db）**
    下发 SET SESSION lock_wait_timeout=10（QA R1：专用连接上的 SET 对 DDL 无效）；
    执行器内每条 pending 迁移前还会再下发一次。
    """
    # Issue #3527：不筛域全量迁移的真正入口在这里（server_common
    # → init_database → run_pending_migrations），owner 为空时必须在此告警，
    # 而不是只挂在 run_domain_migrations() 顶部（该入口 owner 为空直接 return）。
    _warn_missing_migration_owner_if_multi_db("pending")
    _ensure_migration_session_lock_timeout()
    try:
        _run_migrations_with_conn(db)
    except Exception as e:
        # 执行器自身异常（版本表读不了等）→ 退回全量迁移（与历史行为一致的兜底）
        log.warning(f"run_pending_migrations 执行器异常，退回全量 _run_all_migrations: {e}")
        _run_all_migrations()
    # Issue #3648：迁移段收口后再核对关键目标（列在 / 仅 3 个活跃一级）。
    # 迁移自身已 post_check 不盖章；这里保证「已盖章历史库」也能在启动日志暴露漂移。
    verify_migration_targets()
    # Issue #1011 In Scope 0：已盖章存量库迁移报告补写（幂等，失败不阻断启动）
    try:
        _backfill_missing_migration_reports()
    except Exception as e:
        log.warning(f"Issue #1011 迁移报告补写异常（不阻断启动）: {e}")


def _ensure_migrations_ledger(conn) -> None:
    """在**本域库**建版本台账（每域一份，不是跨域副本）。"""
    c = conn.cursor()
    c.execute(_SCHEMA_MIGRATIONS_DDL)
    conn.commit()


def _warn_missing_migration_owner_if_multi_db(call_site: str = "pending") -> None:
    """多库 DSN 已配但 GRAPE_MIGRATION_OWNER 为空 → ERROR 级告警（含修复指引）。

    Issue #3527：告警必须挂在真正跑迁移的入口，且按 call site 区分文案——
    - "pending"：init_database() → run_pending_migrations() 的不筛域形态，
      执行器按全量登记表跑迁移，非属主进程会把别域的表建进本域库
      （沙箱同名空表遮蔽即此形态）；
    - "domain"：run_domain_migrations()（GRAPE_DOMAIN_LIB=1）的域迁移入口，
      owner 为空时该入口直接 return，本进程未执行任何域迁移。
    worker 与 event 共用 runtime 库、按设计不跑迁移，owner 为空属正常，不告警。
    """
    try:
        from src.kernel import db_registry as _dbreg
        if not _dbreg.lib_enabled():
            return
    except Exception as _e:  # noqa: BLE001 - 判定失败不阻断启动
        log.warning(f"迁移属主告警判定跳过（db_registry 不可用）: {_e}")
        return
    if (os.environ.get("GRAPE_MIGRATION_OWNER") or "").strip():
        return
    # 代码已能自行推断域（库名 → 角色）时，这不是错误，只是一条建议：
    # 否则「env 漏配但实际筛域正确」的进程会一直刷 ERROR，把真问题淹掉
    # （2026-11-21 实测：推断已把 mgmt 筛成 122→82 条，告警仍在报「不筛域跑全量」）。
    _resolved = current_migration_domain()
    if _resolved:
        log.info(f"[migration-guard] GRAPE_MIGRATION_OWNER 未显式设置，已按本进程推断域="
                 f"{_resolved}（库名/角色）；建议在启动脚本显式导出 —— "
                 f"mgmt=mgmt / event=runtime / insight=insight / worker 留空")
        return
    _roles = {
        (os.environ.get("GRAPE_PROCESS_ROLE") or "").strip().lower(),
        (os.environ.get("GRAPE_SERVICE_NAME") or "").strip().lower(),
    }
    if "worker" in _roles:
        return
    if call_site == "domain":
        log.error(
            "[migration-guard] 多库 DSN（GRAPE_DSN_MGMT/RUNTIME/INSIGHT）已配置，"
            "但 GRAPE_MIGRATION_OWNER 为空：本进程未执行任何域迁移（域迁移入口直接跳过），"
            "登记的增量迁移将停留在未盖章状态。"
            "修复：启动脚本按角色导出 GRAPE_MIGRATION_OWNER —— mgmt=mgmt / event=runtime / "
            "insight=insight / worker 留空；参考 scripts/grape-systemd-run.sh 与 deploy/deploy3db.sh。"
        )
        return
    log.error(
        "[migration-guard] 多库 DSN（GRAPE_DSN_MGMT/RUNTIME/INSIGHT）已配置，"
        "但 GRAPE_MIGRATION_OWNER 为空：本进程将以「不筛域」形态执行全量迁移，"
        "可能把别域的表建进本域库（Issue #3527 空表遮蔽的成因）。"
        "修复：启动脚本按角色导出 GRAPE_MIGRATION_OWNER —— mgmt=mgmt / event=runtime / "
        "insight=insight / worker 留空；参考 scripts/grape-systemd-run.sh 与 deploy/deploy3db.sh。"
    )


def run_domain_migrations() -> None:
    """三库模式：本域进程只跑本域登记的迁移（不跑 legacy 全量）。

    由 scripts/grape-systemd-run.sh 按角色导出 GRAPE_MIGRATION_OWNER 决定谁来跑：
    runtime 表只由 event 进程跑（worker 与 event 共库，避免并发重复 DDL），
    insight 由 insight 进程跑；mgmt 仍走 init_database() 的常规路径。
    """
    # Issue #3527：此处 owner 为空 → 直接 return，本进程未执行
    # 任何域迁移，文案与 run_pending_migrations 的「不筛域跑全量」区分。
    _warn_missing_migration_owner_if_multi_db("domain")
    # worker 与 event 共用 runtime 库，按设计不跑迁移（避免并发重复 DDL）。
    # 这一跳必须在推断之前：库名推断会把 worker 也判成 runtime。
    if _process_role() == "worker":
        log.info("run_domain_migrations: 进程角色=worker，与 event 共库，跳过域迁移")
        return
    domain = current_migration_domain()
    if domain not in ("runtime", "insight"):
        return  # mgmt：init_database() 已经跑
    registry = [m for m in _MIGRATIONS if domain in (m.domains or ())]
    if not registry:
        log.info(f"{domain} 域无登记的迁移，跳过")
        return
    try:
        _ensure_migrations_ledger(db)
    except Exception as e:  # noqa: BLE001
        log.warning(f"{domain} 域台账建立失败（不阻断启动）: {e}")
        return
    def _apply() -> None:
        _ensure_migration_session_lock_timeout()
        _run_migrations_with_conn(db, registry=registry, allow_legacy=False)
        # Issue #3648：域库同样在启动期核对关键目标（只检查本域对象）。
        verify_migration_targets(domain=domain)
        log.info(f"{domain} 域迁移完成（{len(registry)} 条登记）")

    try:
        # 与 init_database() 走同一把 MySQL GET_LOCK（review M3）：
        # 同域多实例（systemd Restart=always 重叠、两台机滚动升级）不能在同一个库上并发 DDL。
        _run_with_migrate_lock(_apply)
    except Exception as e:  # noqa: BLE001
        log.error(f"{domain} 域迁移执行器异常（不阻塞启动，下次重试）: {e}")


def _run_all_migrations():
    """init_database 的迁移段（顺序与历史一致；MySQL/SQLite 共用，避免两处清单漂移）。

    🧊 老轨已冻结（FROZEN LEGACY TRACK）——只跑两种场景：
    ① 全新库（grape_t_schema_migrations 为空）首跑全量；
    ② run_pending_migrations 执行器自身异常时的兜底重放。
    存量库（已盖 legacy-stamp 202610300001）启动时**完全不执行本函数**。

    ⛔ 所以：任何新增的补列 / 补表 / 回填 / 补索引，一律写成
       register_migration(version, name, kind, fn) 注册式迁移，
       **不要加到本函数里**（加了不报错、也不执行，直到线上查询报 1054 才暴露）。
    事故样本：Issue #995 issue.vcs_exists（2026-11-06 升级后列表静默变空）、
             Issue #785 grape_t_patrol_findings（表永远不建）。

    🧪 守卫：tests/api/test_migration_track_guard.py 用 AST 比对本函数里的
       迁移调用清单与 LEGACY_TRACK_FROZEN —— 少一条（删了要同步改常量）或多一条
       （有人又往老轨塞东西）都会让测试变红。要往这里加东西，正确做法是
       register_migration，不是改这个常量。
    """
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
    _ensure_platform_storage_columns()
    _ensure_case_insensitive_indexes()   # Issue #348: 大小写不敏感唯一约束（dev 侧补充，生产未合）
    _migrate_allow_signup_default()      # allow_signup 默认值迁移（dev 侧补充，生产未合）
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
    # ===== Event 状态语义迁移（seq#2310：入队 pending，认领才 running）=====（lyz 补）
    try:
        _migrate_event_status_pending()
    except Exception as _e:
        log.warning(f"Event status-pending migration failed: {_e}")
    # ===== Chat 配置重复行清理（解绑失效根因）=====（lyz 补）
    try:
        _cleanup_dup_chat_config_rows()
    except Exception as _e:
        log.warning(f"Chat config dedup migration failed: {_e}")
    # ===== Issue #544 L2：A2A 幂等键列 + 唯一索引（幂等）=====
    try:
        _migrate_a2a_idempotency_key()
    except Exception as _e:
        log.warning(f"A2A idempotency key migration failed: {_e}")
    # ===== Issue #805: issue.created_by 列兜底（SQLite 新库建表已含，存量库/MySQL 幂等补列）=====
    try:
        _migrate_issue_created_by()
    except Exception as _e:
        log.warning(f"Issue #805 issue created_by migration failed: {_e}")


def _migrate_issue_vcs_exists():
    """Issue #995：issue 表幂等补 VCS 存在性列（存量回填由 issue_vcs_index 异步执行）。

    - SQLite 新库：建表 DDL 已含该列，_ensure_column 自动跳过；
    - 存量 SQLite / MySQL：_ensure_column 走 ALTER TABLE ADD COLUMN（幂等可重跑）；
    - vcs_exists 默认 1（未知/存在），vcs_checked_at 空表示尚未校验。

    失败必须抛出（不吞）——迁移执行器约定「fn 抛错=不盖章，下次启动幂等重试」，
    吞掉异常会导致列没建成却把版本号盖死，存量库永远等不到补列。

    两道保险（QA 审查 M1：_ensure_column 的人工清单分支默认只 log 不抛）：
    ① 两个补列都传 strict=True，降级算法全失败时直接 raise；
    ② 补完再显式回验列存在，缺一个就 raise —— 印章只在 schema 真落地后才写。
    """
    from src.kernel.session_manager import db as _db, db_lock
    wanted = {"vcs_exists", "vcs_checked_at"}
    # issue 归 runtime 域：三库模式下必须在该域连接上补列（mgmt 连接里没有 issue 表）
    conn = _migration_conn("runtime", default=_db)
    with (db_lock if conn is _db else nullcontext()):
        c = conn.cursor()
        _ensure_column(c, "issue", "vcs_exists", "INTEGER NOT NULL DEFAULT 1",
                       strict=True, force=True)
        # TEXT 列带 DEFAULT 无法 INSTANT/INPLACE，须 allow_copy 兜底（issue 表行数级别可接受）
        _ensure_column(c, "issue", "vcs_checked_at", "TEXT DEFAULT NULL",
                       allow_copy=True, strict=True, force=True)
        conn.commit()
        cols = {r[1] for r in c.execute("PRAGMA table_info(issue)").fetchall()}
        missing = wanted - cols
        if missing:
            raise RuntimeError(
                f"issue.vcs_exists migration finished but columns still missing: {sorted(missing)}")


# 注册式迁移：老轨（_run_all_migrations）在版本表盖帽后即冻结，补列写在那里永远不会执行。
# 本迁移与 _fn_create_grape_t_patrol_findings 同因——存量库靠新轨增量补列。
# 已知时序（QA 审查 I3）：若某次启动同时满足「版本表为空」+「破坏性 DDL 被
# min_code_version 门槛或 MDL 超时拦截」，执行器会先 return 不盖帽、本轮不跑 pending，
# 补列推迟到下一次启动（幂等，不丢）。真正的新库建表已含两列，不受影响。
register_migration(
    "202611060004", "issue995_issue_vcs_exists_columns", "safe",
    _migrate_issue_vcs_exists)


def _fn_normalize_legacy_health_column(conn=None) -> int:
    """Issue #1496：遗留 health 列按 status+last_seen_at 口径幂等回填。

    背景：现 DDL _AGENT_RUNTIMES_DDL 只有 healthy INTEGER，没有 health 列；
    库中出现的 health 是历史版本遗留列（读路径已不消费，展示侧统一走
    derive_runtime_health(status, last_seen_at) 派生）。旧写入方单独写
    health 不同步 status → 「health='online' AND status<>'online'」自相矛盾行
    （存量 4 条 id 23259–23262），测试直读库即红、列表/派发口径分叉（#758 表现）。

    裁定（Spec 拍板）：status 为唯一真源；health 仅允许作为派生展示值。
    本迁移【只规范化不删列】（存在无法立即下线的外部写入方），登记待删除；
    回填值必须由 status+last_seen_at 派生——绝不信旧 health 值，防止真失联
    引擎被旧 health='online' 反向"复活"。

    幂等：值级幂等（health 已等于派生目标值的行不再 UPDATE），重复执行零变更；
    无遗留列时 no-op 且不引入新列。
    conn 可选：测试注入独立连接；缺省用全局 db（受 db_lock 保护）。
    返回本次实际改写的行数（幂等断言用）。
    """
    _own = conn is None
    if _own:
        conn = db
    try:
        from src.kernel.session_manager import _is_mysql_backend
        is_mysql = _is_mysql_backend()
    except Exception:
        is_mysql = False
    from src.domains.runtime.engine.health import derive_runtime_health
    import time as _time

    def _has_col() -> bool:
        if is_mysql:
            c.execute(
                "SELECT COUNT(*) FROM information_schema.columns "
                "WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME='agent_runtimes' "
                "AND COLUMN_NAME='health'")
            return bool(c.fetchone()[0])
        # SQLite：PRAGMA table_info（mysql_compat Cursor 对 PRAGMA 特判翻译，
        # 但本函数测试注入的是裸 sqlite3 连接，直接原生查询双方言通用）
        try:
            return any(r[1] == "health" for r in c.execute(
                "PRAGMA table_info(agent_runtimes)").fetchall())
        except Exception:
            return False

    changed = 0
    with db_lock:
        c = conn.cursor()
        if not _has_col():
            return 0
        _ph = "%s" if is_mysql else "?"
        c.execute(runtime_mig.AGENT_RUNTIMES_HEALTH_ROWS)
        rows = c.fetchall()
        now_ts = _time.time()
        from src.domains.runtime.engine.health import derive_local_runtime_health
        for rid, status, last_seen, health, cred, installed, healthy in rows:
            # Issue #1496 返工：本机直连（cred0）按 #592 口径派生（installed+healthy），
            # 不能用远端心跳口径——本机 last_seen 10min 才刷新，套 100s 新鲜度会把健康
            # 的本机引擎回填成 recently_lost（制造新的矛盾行）。
            if not (cred or 0):
                target = derive_local_runtime_health(
                    bool(installed if installed is not None else 1), bool(healthy))
            else:
                target = derive_runtime_health(status or "offline", last_seen, now_ts)
            if (health or "") == target:
                continue  # 值级幂等：已等不再 UPDATE
            c.execute(runtime_mig.agent_runtimes_set_health(_ph), (target, rid, health))
            changed += c.rowcount if c.rowcount and c.rowcount > 0 else 0
        conn.commit()
    if changed:
        log.info(f"Issue #1496: 遗留 health 列回填 {changed} 行（按 status+last_seen 派生口径）")
    return changed


register_migration(
    # 版本号取当前日期段顺延（全库 register_migration 现存最大 202611060004；202611080001 已被 #1497 占用 → 顺延 202611080002；
    # 避开 #1412 撞号教训的 202611030004/20261105 0001/20261106 0001 已占号段）。
    # 列不删、只按派生口径规范化 + 登记待删除（外部写入方下线后再清理）。
    "202611080002", "issue1496_normalize_legacy_health_column", "safe",
    _fn_normalize_legacy_health_column)


def _fn_normalize_task_messages_created_at_default(conn=None) -> int:
    """Issue #1495（B 项）：MySQL 侧 task_messages.created_at 默认值统一为 ISO+00:00（UTC）。

    病根：MySQL 默认 `cast(now() as char)` 是**本地墙钟、空格分隔、无时区**的形态；
    parse_dt 的 mysql 分支虽按 CST 正确解析这种值，但只要有写入方按 UTC 空格格式
    （SQLite 口径）落库，就会产生 8h 偏差 → 活跃任务被判不活跃（实测复现）。

    处理：
    - SQLite：默认 `datetime('now')` 本身是 UTC，parse_dt 的 sqlite 分支按 UTC 解析，
      无歧义 → **no-op**（SQLite 不支持 ALTER COLUMN 改默认值，新库沿用原 DDL）。
    - MySQL：ALTER COLUMN ... SET DEFAULT 为元数据变更（8.0 起 INSTANT），
      值形如 `2026-11-08T12:34:09.824507+00:00`，与写入侧 isoformat() 一致。

    幂等：默认值已含 '+00:00' 直接跳过；重复执行零变更。
    注意：表达式刻意不用 DATE_FORMAT('%%Y-...')——mysql_compat 参数化会把 `%` 转义成
    `%%`，默认值退化为常量串（实测）；改用 CONCAT/REPLACE 拼装，表达式中无 `%`。
    """
    try:
        from src.kernel.session_manager import _is_mysql_backend
        if not _is_mysql_backend():
            return 0
    except Exception:
        return 0
    _own = conn is None
    if _own:
        conn = db
    try:
        with db_lock:
            c = conn.cursor()
            c.execute(
                "SELECT COLUMN_DEFAULT FROM information_schema.columns "
                "WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME='task_messages' "
                "AND COLUMN_NAME='created_at'")
            row = c.fetchone()
            if not row:
                return 0            # 表不存在（初始化顺序）→ 跳过
            _cur_default = str(row[0] or "")
            if "+00:00" in _cur_default:
                return 0            # 已是 ISO+00:00，幂等
            c.execute(
                runtime_mig.ALTER_TASK_MESSAGES)
            conn.commit()
            log.info("Issue #1495: task_messages.created_at 默认值改为 ISO+00:00（UTC）")
            return 1
    except Exception as e:
        log.warning(f"Issue #1495 default migration skipped: {e}")
        try:
            conn.rollback()
        except Exception:
            pass
        return 0


register_migration(
    "202611080003", "issue1495_task_messages_created_at_default", "safe",
    _fn_normalize_task_messages_created_at_default)


def _migrate_issue_created_by():
    """Issue #805：issue 表幂等补 `created_by TEXT DEFAULT ''` 列（创建人一等数据化）。

    - SQLite 新库：建表 DDL 已含该列，_ensure_column 自动跳过；
    - 存量 SQLite / MySQL：_ensure_column 走 ALTER TABLE ADD COLUMN（MySQL 经 mysql_compat 翻译）。
    """
    from src.kernel.session_manager import db, db_lock
    try:
        with db_lock:
            c = db.cursor()
            _ensure_column(c, "issue", "created_by", "TEXT DEFAULT ''")
            db.commit()
    except Exception as e:
        log.warning(f"issue created_by column migration failed: {e}")


def _migrate_a2a_idempotency_key():
    """Issue #544 L2：events 表幂等键迁移（幂等可重跑）。

    - 加列 `idempotency_key TEXT DEFAULT NULL`（空 = NULL，SQLite/MySQL 下 NULL 均可重复，
      不影响无键的存量/增量事件）；
    - 唯一索引 `(repo_key, idempotency_key)`：同仓同键只允许一条事件，
      服务端据此把重复 A2A 请求去重为"返回已有事件"。
    """
    from src.kernel.session_manager import db, db_lock
    is_mysql = False
    try:
        from src.kernel.session_manager import _is_mysql_backend
        is_mysql = _is_mysql_backend()
    except Exception:
        pass
    with db_lock:
        c = db.cursor()
        _ensure_column(c, "events", "idempotency_key", "TEXT DEFAULT NULL")
        db.commit()
        if is_mysql:
            # MySQL 无 CREATE INDEX IF NOT EXISTS：先查 information_schema
            try:
                c.execute(
                    "SELECT COUNT(*) FROM information_schema.statistics"
                    " WHERE table_schema = DATABASE()"
                    " AND table_name = 'events' AND index_name = 'idx_events_idem'")
                if (c.fetchone()[0] or 0) == 0:
                    c.execute("CREATE UNIQUE INDEX idx_events_idem ON events(repo_key, idempotency_key)")
                    db.commit()
            except Exception as e:
                log.warning(f"events idempotency index (mysql) failed: {e}")
        else:
            try:
                c.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_events_idem"
                          " ON events(repo_key, idempotency_key)")
                db.commit()
            except Exception as e:
                log.warning(f"events idempotency index (sqlite) failed: {e}")


def _migrate_release_subissue_unique_index():
    """Issue #627 方案 C 审查修复（R1）：发布子单唯一性 DB 兜底（部分唯一索引）。

    前置显式查询防重复存在并发窗口（双提交可同时通过前置查询各建一张子单），
    DB 层唯一约束兜底：同 (repo_key, parent_issue_number, release_candidate_sha,
    release_target_env) 只允许一行发布子单；普通 issue（release_target_env 空）不参与。

    - SQLite：部分唯一索引 WHERE release_target_env != ''；
    - MySQL：生成列 NULLIF(release_target_env, '')（空串转 NULL 不参与唯一）+ 唯一索引。
    建索引幂等（IF NOT EXISTS / information_schema 检查）；存量脏数据冲突时记
    WARNING 不阻塞启动（前置查询仍兜一层）。
    """
    from src.kernel.session_manager import db, db_lock
    is_mysql = False
    try:
        from src.kernel.session_manager import _is_mysql_backend
        is_mysql = _is_mysql_backend()
    except Exception:
        pass
    with db_lock:
        c = db.cursor()
        # B5（本地集成修复）：release_* 字段此前只在 legacy 全量首跑路径补列，已盖章库
        # 从未建过；且带 DEFAULT 的 TEXT 列 MySQL 无法 INSTANT/INPLACE，安全策略会挡下
        # 导致列永远建不出来 → 此处统一补全部 7 列并允许默认算法（issue 表百级行，COPY 安全）。
        for _col, _ddl in (
            ("release_candidate_sha", "TEXT DEFAULT ''"),
            ("release_target_env", "TEXT DEFAULT ''"),
            ("release_evidence_ref", "TEXT DEFAULT ''"),
            ("release_approved_by", "VARCHAR(36) DEFAULT ''"),
            ("release_approved_at", "TEXT DEFAULT ''"),
            ("release_result", "TEXT DEFAULT ''"),
            ("release_failure_reason", "TEXT DEFAULT ''"),
        ):
            _ensure_column(c, "issue", _col, _ddl, allow_copy=True)
        db.commit()
        if is_mysql:
            try:
                # 生成列：release_bind_env = NULLIF(release_target_env, '') STORED
                # （空串转 NULL 不参与唯一约束）。ALTER 幂等：先查列存在。
                c.execute(
                    "SELECT COUNT(*) FROM information_schema.columns "
                    "WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME='issue' "
                    "AND COLUMN_NAME='release_bind_env'")
                if (c.fetchone()[0] or 0) == 0:
                    c.execute(
                        runtime_mig.ALTER_ISSUE)
                    db.commit()
                # B4（PR #847 审查）：索引必须用 CREATE UNIQUE INDEX IF NOT EXISTS
                # 走 mysql_compat Cursor.execute 特判——对 TEXT 列自动加 (191) 前缀
                # （repo_key/release_candidate_sha 均为 TEXT，无前缀长度 MySQL 报
                # error 1170），并内置 information_schema 存在性检查。
                # 此前自拼存在性检查 + 无 IF NOT EXISTS 的裸 CREATE 绕过特判 →
                # 直发 MySQL → 1170 被 except 吞掉 → DB 层唯一性兜底在 MySQL 后端
                # 完全缺失（与 idx_tasks_repo_issue 同路径才是对的）。
                c.execute(
                    "CREATE UNIQUE INDEX IF NOT EXISTS uq_issue_release_bind ON issue"
                    "(repo_key, parent_issue_number, release_candidate_sha, release_bind_env)")
                db.commit()
            except Exception as e:
                log.warning(f"release subissue unique index (mysql) failed: {e}")
        else:
            try:
                c.execute(
                    "CREATE UNIQUE INDEX IF NOT EXISTS uq_release_subissue ON issue"
                    "(repo_key, parent_issue_number, release_candidate_sha, release_target_env) "
                    "WHERE release_target_env != ''")
                db.commit()
            except Exception as e:
                log.warning(f"release subissue unique index (sqlite) failed: {e}")


def _mig_issue627_drop_legacy_production_release():
    """Issue #627 方案 C 审查修复（Optional-4）：DROP 旧库遗留旁路表（代码已无引用）。"""
    from src.kernel.session_manager import db, db_lock
    with db_lock:
        db.execute("DROP TABLE IF EXISTS production_release")
        db.commit()
    log.info("Dropped legacy production_release table (Issue #627 Plan C)")


register_migration(
    # 版本号冲突修复（本地集成）：202611020001/2 已被 patrol-findings 迁移占用并盖章，
    # 复用同号会导致本迁移永远被判"已执行"而跳过（#627 发布子单列/唯一索引在 MySQL 上缺失）。
    "202611020003", "issue627_release_subissue_unique_index", "safe",
    _migrate_release_subissue_unique_index)
register_migration(
    "202611020004", "issue627_drop_legacy_production_release", "cleanup",
    _mig_issue627_drop_legacy_production_release)


def _fn_add_agent_runtimes_probe_spawn_fail_count() -> None:
    """Issue #1982 复审：agent_runtimes 补 probe_spawn_fail_count（存量库幂等补列）。

    spawn_failed（EMFILE/ENOMEM/EAGAIN 资源类 spawn 失败）不计入 probe_fail_count
    （偶发资源紧张不摘除引擎），但必须有独立计数兜底：持续 spawn_failed 连续
    达到阈值后由心跳降级 healthy=0，避免引擎永远 healthy 持续接单。计数列
    与 probe_fail_count 同模式（心跳按 last_probe 幂等去重，ok 清零）。
    """
    with db_lock:
        c = db.cursor()
        _ensure_column(c, "agent_runtimes", "probe_spawn_fail_count", "INT DEFAULT 0")
        db.commit()


register_migration(
    # 迁移号合规（tests/arch/test_migration_version_integrity）：基线快照
    # max_version=202611140004（migration_versions_baseline.json），新迁移号
    # 必须大于 max_version 且不得落在已发布 20261112xxxx 区间。原号
    # 202611120001 与基线迁移撞号、顺延号 0005/0006 低于 max_version；
    # 202611150001/2 被 #2091/#2095 抢注，0003-0006 又被基线 67ed59fb4
    # 卡驱动凭据合入（repo-credentials 表族）占用，最终改号 202611150007/8。
    version="202611150008",
    name="add-agent-runtimes-probe-spawn-fail-count",
    kind="safe",
    fn=_fn_add_agent_runtimes_probe_spawn_fail_count,
)


def _fn_issue2133_engine_recovery_schema() -> None:
    """Issue #2133：引擎恢复加回闭环所需结构（存量库幂等补齐）。

    - agent_runtimes 补 spawn_failed 时间窗追踪列：spawn_fail_since（连续
      spawn_failed 首见时刻）、spawn_fail_warned_at（强告警已发）、
      spawn_fail_degraded_at（超第二阈值标记 degraded，UI 高亮，不自动摘除）；
    - agent_runtimes 补 probe_ok_streak（连续探活 ok 次数，恢复判定用）；
    - 新建 grape_t_engine_lifecycle_audit（告警/降级/自动加回/一键加回/任务对账审计）。
    """
    with db_lock:
        c = db.cursor()
        _ensure_column(c, "agent_runtimes", "spawn_fail_since", "TEXT DEFAULT ''")
        _ensure_column(c, "agent_runtimes", "spawn_fail_warned_at", "TEXT DEFAULT ''")
        _ensure_column(c, "agent_runtimes", "spawn_fail_degraded_at", "TEXT DEFAULT ''")
        _ensure_column(c, "agent_runtimes", "probe_ok_streak", "INT DEFAULT 0")
        db.execute(_ENGINE_LIFECYCLE_AUDIT_DDL)
        db.commit()


register_migration(
    version="202611150007",
    name="issue2133-engine-recovery-schema",
    kind="safe",
    fn=_fn_issue2133_engine_recovery_schema,
)


def _mig_issue3349_issue_repo_title_index() -> None:
    """Issue #3349：建单查重改查平台库（`issue` 台账）→ 补 `(repo_key, title)` 索引。

    查重 SQL 先按 `repo_key + title` 精确命中（索引定位，成本与仓内 issue 数无关）；
    未命中再退回同仓 `TRIM(title)` 比对（兼容历史行标题两端带空白）。缺索引时每次
    建单都要扫该仓库全部台账行，仓内 issue 越多越慢，正是本单要消除的线性成本。
    幂等：索引已存在即跳过；失败只告警，不阻断启动（查重退回仓库级扫描仍可用）。
    """
    is_mysql = False
    try:
        from src.kernel.session_manager import _is_mysql_backend
        is_mysql = _is_mysql_backend()
    except Exception:
        pass
    _idx = "idx_issue_repo_title"
    with db_lock:
        c = db.cursor()
        try:
            if is_mysql:
                # MySQL 下 repo_key/title 是 TEXT，必须带前缀长度
                c.execute(
                    "SELECT COUNT(*) FROM information_schema.statistics "
                    "WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME='issue' "
                    "AND INDEX_NAME=%s", (_idx,))
                if c.fetchone()[0]:
                    return
                c.execute(f"CREATE INDEX {_idx} ON issue (repo_key(64), title(191))")
            else:
                c.execute("SELECT name FROM sqlite_master WHERE type='index' AND name=?", (_idx,))
                if c.fetchone():
                    return
                c.execute(f"CREATE INDEX {_idx} ON issue (repo_key, title)")
            db.commit()
            log.info(f"Issue #3349: created index {_idx} on issue")
        except Exception as e:
            log.warning(f"Issue #3349 issue(repo_key,title) index failed (dedupe degrades to repo scan): {e}")


register_migration(
    "202611240003", "issue3349_issue_repo_title_index", "safe",  # Issue #3349: renumbered from 202611190001 (below baseline max 202611240002)
    _mig_issue3349_issue_repo_title_index,
    # `issue` 台账归 runtime 域（configs/table_ownership.json），必须登记到 owner 域执行：
    # 按默认 mgmt 域跑时连接里没有 issue 表，CREATE INDEX 抛错被兜底吞掉，迁移照样盖章，
    # 结果「已执行」但索引不存在（建单查重继续退化成整仓扫描）。
    domains=("runtime",))


def _ensure_case_insensitive_indexes():
    """Issue #348: 大小写不敏感唯一约束（DB 层纵深防御）。

    SQLite：表达式索引 CREATE UNIQUE INDEX ... ON table(LOWER(col)) WHERE ...
    MySQL：生成列 + 唯一索引。空串用 NULLIF 转为 NULL 避免多条空串冲突。

    所有创建操作幂等（IF NOT EXISTS / 检查已存在），失败时记 WARNING 不崩溃。
    """
    from src.kernel.session_manager import db, db_lock
    is_mysql = False
    try:
        from src.kernel.session_manager import _is_mysql_backend
        is_mysql = _is_mysql_backend()
    except Exception:
        pass

    if not is_mysql:
        # SQLite: 表达式索引直接支持
        _sqlite_indexes = [
            "CREATE UNIQUE INDEX IF NOT EXISTS uq_grape_accounts_username_lower ON grape_accounts(LOWER(username))",
            "CREATE UNIQUE INDEX IF NOT EXISTS uq_admin_accounts_username_lower ON admin_accounts(LOWER(username))",
            "CREATE UNIQUE INDEX IF NOT EXISTS uq_login_whitelist_gitcode_lower ON login_whitelist(LOWER(gitcode_username)) WHERE gitcode_username IS NOT NULL",
            "CREATE UNIQUE INDEX IF NOT EXISTS uq_login_whitelist_github_lower ON login_whitelist(LOWER(github_username)) WHERE github_username != ''",
        ]
        for sql in _sqlite_indexes:
            try:
                with db_lock:
                    db.execute(sql)
                    db.commit()
            except Exception as e:
                log.warning(f"_ensure_case_insensitive_indexes (SQLite): {e}")
        return

    # MySQL: 生成列 + 唯一索引
    # NULLIF(col, '') 把空串转为 NULL, NULL 不参与唯一约束 → 多条空串不冲突
    # gitcode_username DEFAULT NULL → LOWER(NULL) = NULL → 天然不冲突
    _mysql_targets = [
        # (table, column, gen_col_name, gen_expr)
        ("grape_accounts", "username", "username_lower", "LOWER(username)"),
        ("admin_accounts", "username", "username_lower", "LOWER(username)"),
        ("login_whitelist", "gitcode_username", "gitcode_username_lower", "LOWER(gitcode_username)"),
        ("login_whitelist", "github_username", "github_username_lower", "LOWER(NULLIF(github_username, ''))"),
    ]
    for table, col, gen_col, gen_expr in _mysql_targets:
        idx_name = f"uq_{table}_{gen_col}"
        try:
            with db_lock:
                c = db.cursor()
                # 检查生成列是否已存在
                c.execute(
                    "SELECT COUNT(*) FROM information_schema.columns "
                    "WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME=%s AND COLUMN_NAME=%s",
                    (table, gen_col),
                )
                if c.fetchone()[0] == 0:
                    c.execute(
                        f"ALTER TABLE {table} ADD COLUMN {gen_col} VARCHAR(128) AS ({gen_expr}) STORED"
                    )
                    db.commit()
                    log.info(f"Added generated column {table}.{gen_col}")
                # 检查索引是否已存在
                c.execute(
                    "SELECT COUNT(*) FROM information_schema.statistics "
                    "WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME=%s AND INDEX_NAME=%s",
                    (table, idx_name),
                )
                if c.fetchone()[0] == 0:
                    c.execute(f"CREATE UNIQUE INDEX {idx_name} ON {table}({gen_col})")
                    db.commit()
                    log.info(f"Created case-insensitive index {idx_name}")
        except Exception as e:
            try:
                db.rollback()
            except Exception:
                pass
            log.warning(f"_ensure_case_insensitive_indexes: {table}.{col} failed: {e}")


def _migrate_allow_signup_default():
    """向前兼容迁移：升级部署时保持原有注册行为。

    基线 signup_policy 空白名单 → return True（允许注册）。
    v4 改为 return _is_allow_signup()，未配置 allow_signup meta 时返回 False。

    如果 allow_signup meta 未设置且 grape_accounts 表已有数据（升级部署），
    自动设置 allow_signup='true' 保持原有行为。新部署（grape_accounts 为空）
    不设置，保持默认关闭。

    注意：不能用 admin_accounts 判断——_seed_admin_accounts 先于此函数执行，
    新部署也会插入 admin 账号导致 admin_count>0 误判为升级。
    """
    try:
        from src.domains.mgmt.user.db import get_meta_value, set_meta_value
        # get_meta_value 在 key 不存在时返回 ""（非 None），用 truthy 检查
        existing = get_meta_value("allow_signup")
        if existing:
            return  # 已设置，不覆盖
        with db_lock:
            c = db.cursor()
            c.execute(mgmt_mig.SELECT_GRAPE_ACCOUNTS)
            user_count = c.fetchone()[0]
        if user_count > 0:
            set_meta_value("allow_signup", "1")
            log.info("_migrate_allow_signup_default: upgrade detected (grape_accounts not empty), set allow_signup=1 for backward compat")
    except Exception as e:
        log.warning(f"_migrate_allow_signup_default failed: {e}")


def _run_with_migrate_lock(runner) -> None:
    """在迁移锁保护下执行 runner（同库多实例/重启重叠互斥）。

    MySQL：GET_LOCK 绑池外专用连接（取锁/放锁必须同一物理连接），finally 先显式
    RELEASE 再 close 兜底；SQLite：单实例，直接跑。
    `init_database()` 与 `run_domain_migrations()` 共用本函数
    （review M3：域迁移执行器原先绕过了这条互斥，滚动升级/重启重叠时同域并发 DDL）。
    """
    from src.kernel.mysql_compat import get_db_backend
    if get_db_backend() != "mysql":
        runner()
        return

    try:
        from src.kernel.mysql_compat import mysql_config
        _lock_name = "grape_migrate_" + (mysql_config().get("database") or "default")
    except Exception:
        _lock_name = "grape_migrate"

    _factory = getattr(db, "dedicated_connection", None)
    if _factory is None:
        log.error("migrate lock UNAVAILABLE (db 后端为 mysql 但无 dedicated_connection 支持)；"
                  "以无锁方式继续迁移（迁移幂等，但同库多实例可能并发迁移，请关注）")
        with db_lock:
            runner()
        return
    try:
        _dconn = _factory()
    except Exception as _lock_err:
        log.error(f"migrate lock UNAVAILABLE (dedicated conn failed: {_lock_err})；"
                  f"以无锁方式继续迁移（迁移幂等，但同库多实例可能并发迁移，请关注）")
        with db_lock:
            runner()
        return

    try:
        _acquired = False
        try:
            _acquired, _waited = _acquire_migrate_lock(_dconn, _lock_name)
        except Exception as _ae:
            log.error(f"migrate lock acquire failed: {_ae}；以无锁方式继续迁移（迁移幂等）")
        if not _acquired:
            _holder = _query_lock_holder_info(_dconn, _lock_name)
            log.error(f"migrate lock NOT acquired: {_lock_name}，继续迁移（迁移幂等）"
                      + (f"；holder: {_holder}" if _holder else "（无持锁者信息）"))
        _set_migration_session_lock_timeout(_dconn, 10)
        try:
            with db_lock:
                runner()
        finally:
            if _acquired:
                try:
                    _cur = _dconn.cursor()
                    _cur.execute("SELECT RELEASE_LOCK(%s)", (_lock_name,))
                    _r = _cur.fetchone()
                    log.info(f"migrate lock released: {_lock_name}"
                             f"（RELEASE_LOCK={_r[0] if _r else None}）")
                except Exception as _re:
                    log.warning(f"migrate lock release failed（连接 close 会兜底释放）: {_re}")
    finally:
        _dconn.close()


def init_database():
    """初始化所有新表(幂等),启动时调用。

    `GRAPE_SKIP_DB_INIT=1` 时整体跳过：**执行侧**（runtime agent）只通过 HTTP 与主控交互，
    不需要建表/迁移；而它在异构库（如 MariaDB）上跑这套 MySQL 8 DDL 会连续 1064，
    既污染日志又把启动拖到几分钟，导致心跳迟迟不发、凭据被判失联。
    建表迁移只应由主控进程（mgmt/event/worker/insight）执行。
    """
    if (os.environ.get("GRAPE_SKIP_DB_INIT") or "").strip().lower() in ("1", "true", "yes", "on"):
        log.info("GRAPE_SKIP_DB_INIT=1：跳过 schema 初始化/迁移（执行侧 agent 不需要建表）")
        return

    # kernel 端口绑定（装配层职责）：kernel 只依赖 src.kernel.ports，实现由本层注入。
    # 放在所有建表/迁移之前——kernel 的迁移前逻辑（如备份保留策略）也可能用到端口。
    # 幂等：多个 server 进程、重复启动路径都安全。
    try:
        from src.api import contracts_wiring
        contracts_wiring.bind_all(replace=True)
    except Exception as _cw:
        log.warning("kernel 端口绑定失败（kernel 侧 port 调用将 fail-fast）: %s", _cw)
    # 迁移前 DB 自动备份保留策略兜底（2026-10 磁盘告警：data/backups 曾堆到 63G 打满根盘）
    # 每次启动清理过量 pre-m0 备份，保留 DB_BACKUP_KEEP(默认5) 份
    try:
        from src.kernel.session_manager import prune_db_backups, _db_path
        prune_db_backups(_db_path)
    except Exception as _pb:
        log.warning(f"DB backup prune skipped: {_pb}")

    _run_with_migrate_lock(run_pending_migrations)
    # S70（实弹发现）：config 派生存储列（bot_<p>_token 等）必须每次启动幂等补齐——
    # 盖章库的迁移段一次性跳过，而平台卡可任意时刻注册（新平台加卡后注册 → 列不创建
    # → add_managed_repo 静默 False → webhook_secret 不落库 → webhook 验签必 404）。
    # 幂等 DDL（列已存在跳过）与盖章迁移语义解耦，每次启动执行。
    # （D44/S64 函数体在合并 squash 时一度丢失，2026-11-13 按 b439764fd 原形恢复。）
    _ensure_platform_storage_columns()
    # Issue #3897：启动期 schema 自检——跨域身份关系（external_identities 基表/视图）
    # 缺失时立刻可诊断（生产曾出现「代码已上线、runtime 库视图未建」→ 每次身份解析 1146）。
    # 失败不阻断启动（自检只负责可见性，修复交给迁移/部署脚本）。
    _check_identity_relations()


#: 启动自检覆盖的跨域身份关系（owner=mgmt；非 owner 库形态为跨域视图）。
_IDENTITY_RELATIONS = ("external_identities", "grape_accounts")
_IDENTITY_RELATION_REMEDIATION = (
    "python3 scripts/domain_view_freshness.py --repair "
    "（owner=mgmt 补建跨域视图；存量 mgmt 库缺基表时由迁移 202611230013 补建）")
#: identity 读写的 owner 域（`grape_account_db` / `identity_lifecycle` 固定 `uow("mgmt")`）。
_IDENTITY_DOMAIN = "mgmt"
_IDENTITY_DOMAIN_REMEDIATION = (
    "worker/event 进程需可见 GRAPE_DSN_MGMT（三库拓扑下不共享该 DSN 时请用 "
    "GRAPE_3DB_ENV drop-in 统一下发）；缺 DSN 时 mgmt 域不可用，身份解析会降级为 []"
    "（等同「无身份」，用户绑定/PAT 回填全部失效）")


def _check_identity_domain() -> str:
    """启动期自检：identity owner 域（mgmt）是否可路由。

    Issue #3897 QA R1：identity 读写已固定 `uow("mgmt")`，三库拓扑下
    `route_of("mgmt")` 在缺 `GRAPE_DSN_MGMT` 时抛 `DomainUnavailable`，被降级成
    返回 `[]` —— 表现为「用户突然没有身份」，且只在业务日志里零星可见。
    该检查把部署前置（DSN 可见性）变成启动那一刻的一条 ERROR（含 remediation），
    不阻断启动：返回路由结果（"local"/"domain"），不可用时返回空串。
    """
    try:
        from src.kernel.db_registry import route_of
        return route_of(_IDENTITY_DOMAIN)
    except Exception as _e:
        log.error(
            "schema self-check: identity domain '%s' unavailable (%s): starts degraded, "
            "identity lookups return []; remediation: %s",
            _IDENTITY_DOMAIN, _e, _IDENTITY_DOMAIN_REMEDIATION)
        return ""


def _check_identity_relations(relation_kind=None) -> list[str]:
    """启动期自检：当前库是否具备身份域关系（基表或跨域视图）。

    Issue #3897：`external_identities` 在生产 `grape_runtime` 缺失（1146），
    属于「代码已上线、跨域视图未补建」的 schema 漂移。迁移只会补 owner 基表，
    非 owner 库的视图由部署脚本 `scripts/domain_view_freshness.py --repair` 建
    （补建清单在 `scripts/view_create_missing_allowlist.txt`）。
    本自检把这种漂移在**启动那一刻**变成一条可 grep 的 ERROR（含库名与修复命令），
    不再等业务请求逐条刷 WARNING。

    返回缺失关系名列表（便于测试/运维断言）；探测异常只 WARN，绝不抛。
    """
    from src.domains.runtime.schema_probe import relation_kind as _default_probe

    probe = relation_kind or _default_probe
    missing: list[str] = []
    # Issue #3897 QA R1：先确认 owner 域可路由（缺 DSN → ERROR + remediation），
    # 再探测关系是否存在；两者都不阻断启动。
    _domain_route = _check_identity_domain()
    try:
        from src.kernel.db_registry import current_domain, database_of
        _db_label = database_of(current_domain() or "") or os.environ.get("DEVOPS_DB_NAME", "")
    except Exception:
        _db_label = ""
    try:
        with db_lock:
            c = db.cursor()
            for _table in _IDENTITY_RELATIONS:
                if probe(c, _table) is None:
                    missing.append(_table)
    except Exception as _e:
        log.warning(f"schema self-check (identity relations) skipped: {_e}")
        return []
    if missing:
        log.error(
            "schema self-check: identity relation(s) missing in db=%s (domain=%s route=%s): %s "
            "(身份解析将 1146/降级); remediation: %s",
            _db_label or "current", _IDENTITY_DOMAIN, _domain_route or "unavailable",
            ",".join(missing), _IDENTITY_RELATION_REMEDIATION)
    else:
        log.info("schema self-check: identity relations present in db=%s (domain=%s route=%s) (%s)",
                 _db_label or "current", _IDENTITY_DOMAIN, _domain_route or "unavailable",
                 ",".join(_IDENTITY_RELATIONS))
    return missing


def _migrate_event_status_pending():
    """事件状态语义迁移（docs/weekly-07/seq-2310-health-gate-stuck-design.md §6.5.4）。

    存量 events：status='running' 但关联任务仍 queued / 无任务 → 置 pending，
    与「入队 pending、认领才 running」新语义对齐。幂等（每次启动可重跑无副作用）。
    """
    try:
        from src.kernel.mysql_compat import get_db_backend
        with db_lock:
            c = db.cursor()
            # ① running 且关联任务 queued → pending
            c.execute(
                runtime_mig.UPDATE_EVENTS)
            _n1 = c.rowcount
            # ② running 且 started_at 空且无任何关联任务 → pending（疑似入队后从未被认领/创建任务失败）
            c.execute(
                runtime_mig.UPDATE_EVENTS__MIGRATE_EVENT_STATUS_PENDING)
            _n2 = c.rowcount
            db.commit()
        if _n1 or _n2:
            log.info(f"Event status-pending migration: {_n1} running+queued -> pending, "
                     f"{_n2} running+untasked -> pending")
    except Exception as e:
        log.warning(f"Event status-pending migration failed: {e}")


def _cleanup_dup_chat_config_rows():
    """幂等迁移：清理 chat_agent_session 中同一 user 的重复 repo_key='' 配置行。

    背景（chat-config-duplicate-row-unbind-fix-design.md）：账号迁移残留第二条 repo_key='' 行，
    get_chat_config 无序 LIMIT 1 命中空行 → 解绑/配置读写落错行、真绑定行永不清。
    只保留每 user 的 id=MIN(id) 一行（通常 sess-cfg-{user_id}）。
    """
    try:
        with db_lock:
            c = db.cursor()
            c.execute(
                runtime_mig.DELETE_CHAT_AGENT_SESSION)
            _n = c.rowcount
            db.commit()
        if _n:
            log.info(f"Chat config dedup migration: removed {_n} duplicate repo_key='' row(s)")
    except Exception as e:
        log.warning(f"Cleanup dup chat config rows failed: {e}")


def _migrate_knowledge_dedup_and_ref():
    """知识库 source 回填 + name/description 回填 + 重复记录去重 + scene_knowledge_ref 数据迁移 + MySQL 原生 DDL。

    每次启动执行（幂等）。issue95 修复部分有独立幂等标记。
    步骤顺序：source 回填 → name/description 回填 → 去重 → ref 迁移 → 孤儿 ref 清理。
    """
    from src.kernel.mysql_compat import get_db_backend
    _is_mysql = get_db_backend() == "mysql"

    # Issue #95：检查 #95 修复是否已执行过
    _issue95_done = False
    with db_lock:
        try:
            row = db.cursor().execute(
                runtime_mig.SELECT_META__MIGRATE_KNOWLEDGE_DEDUP_AND
            ).fetchone()
            _issue95_done = bool(row and row[0] == '1')
        except Exception:
            pass

    with db_lock:
        c = db.cursor()

        # 1. source 字段回填（幂等——只更新 source 为空的记录）
        c.execute(
            mgmt_mig.UPDATE_AGENT_SHARED_KNOWLEDGE)
        c.execute(
            mgmt_mig.UPDATE_AGENT_SHARED_KNOWLEDGE__MIGRATE_KNOWLEDGE_DEDUP_AND)
        c.execute(
            mgmt_mig.UPDATE_AGENT_SHARED_KNOWLEDGE__MIGRATE_KNOWLEDGE_DEDUP_AND_2)

        # 1.5 回填空 name + 统一 description（所有 scope，必须在去重之前）
        # 老代码部分 INSERT 路径没有 name 字段或自动生成逻辑不存在 → DEFAULT ''。
        # baseline 去重按 (repo_key, name) GROUP BY，name 为空时同 repo 的 3 条
        # 会被合并为 1 条丢失数据。description 硬编码统一值无法区分。
        # 用 derive_knowledge_name 统一推导，所有 scope 共用同一逻辑。
        from src.api.knowledge import derive_knowledge_name, derive_knowledge_description
        from src.kernel.repo_key import (BASELINE_NAME_MAP, BASELINE_DESC_MAP,
                                  BASELINE_NAME_FALLBACK, BASELINE_DESC_FALLBACK)
        _UNIFIED_DESC = "codegraph 自动生成的项目基线知识"  # 老的统一值，勿改
        _rows = c.execute(
            mgmt_mig.SELECT_AGENT_SHARED_KNOWLEDGE,
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
                mgmt_mig.UPDATE_AGENT_SHARED_KNOWLEDGE__MIGRATE_KNOWLEDGE_DEDUP_AND_3,
                _updates)
            log.info(f"[issue67] backfilled name/description for {len(_updates)} rows")

        # 2. 重复记录去重——每类 scope 只保留最新一条
        #    baseline：按 (repo_key, scope, name) 去重
        #    其他 scope：按 (repo_key, scope, agent_type, issue_num, file_path) 去重
        #    用派生表子查询包装避免 DELETE 自引用（MySQL 限制），SQLite/MySQL 通用
        # baseline 去重
        c.execute(
            mgmt_mig.DELETE_SCENE_KNOWLEDGE_REF)
        c.execute(
            mgmt_mig.DELETE_AGENT_SHARED_KNOWLEDGE)
        # 其他 scope 去重
        c.execute(
            mgmt_mig.DELETE_SCENE_KNOWLEDGE_REF__MIGRATE_KNOWLEDGE_DEDUP_AND)
        c.execute(
            mgmt_mig.DELETE_AGENT_SHARED_KNOWLEDGE__MIGRATE_KNOWLEDGE_DEDUP_AND)

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
            mgmt_mig.INSERT_IGNORE_SCENE_KNOWLEDGE_REF)
        # project → 指定 Agent 关联（issue_num=0）
        c.execute(
            mgmt_mig.INSERT_IGNORE_SCENE_KNOWLEDGE_REF__MIGRATE_KNOWLEDGE_DEDUP_AND)
        # issue → 全局关联（issue_num=实际值）
        c.execute(
            mgmt_mig.INSERT_IGNORE_SCENE_KNOWLEDGE_REF__MIGRATE_KNOWLEDGE_DEDUP_AND_2)
        # issue_agent → 指定 Agent + Issue 关联
        c.execute(
            mgmt_mig.INSERT_IGNORE_SCENE_KNOWLEDGE_REF__MIGRATE_KNOWLEDGE_DEDUP_AND_3)
        # member（agent_type 非空）→ 指定 Agent 关联（issue_num=0）
        # Issue #95：member 自动关联的两段 INSERT 已删除（违反"member 不自动关联"设计，见 git 历史）

        # Issue #95：清理 migration 自动创建的错误 member ref（幂等）
        # 只删 created_by='system' 的——用户手动创建的（created_by=username）不删
        # 加幂等标记：首次执行后设 meta 标记，后续跳过
        if not _issue95_done:
            try:
                c.execute(
                    mgmt_mig.DELETE_SCENE_KNOWLEDGE_REF__MIGRATE_KNOWLEDGE_DEDUP_AND_2)
            except Exception:
                pass

            # Issue #95：修复 rejected→accepted 老数据（幂等）
            # 老代码 UPDATE 不改 status，导致先 rejected 再 accepted 的记录仍为 rejected
            # 有文件内容（obs_key 非空 + content_hash 非空）说明 accepted 写过文件，应为 accepted
            try:
                c.execute(
                    mgmt_mig.UPDATE_AGENT_SHARED_KNOWLEDGE__MIGRATE_KNOWLEDGE_DEDUP_AND_4)
            except Exception:
                pass

            # Issue #95：标记已完成
            try:
                c.execute(
                    runtime_mig.UPSERT_META__MIGRATE_KNOWLEDGE_DEDUP_AND)
            except Exception:
                pass

        # 4. content_hash 回填——OBS 模式下 obs_key 非空但 hash 为空的老数据
        try:
            from src.domains.runtime.storage import get_storage
            _storage = get_storage()
            if _storage is not None:
                import hashlib as _hashlib
                _prefix = os.environ.get("OBS_PREFIX", "") or ""
                _empty_hash_rows = c.execute(
                    mgmt_mig.SELECT_AGENT_SHARED_KNOWLEDGE__MIGRATE_KNOWLEDGE_DEDUP_AND
                ).fetchall()
                for _kid, _obs_key in _empty_hash_rows:
                    for _try_key in (_obs_key, f"{_prefix}/{_obs_key}"):
                        _data = _storage.read_bytes(_try_key)
                        if _data:
                            _h = _hashlib.sha256(_data).hexdigest()
                            c.execute(mgmt_mig.UPDATE_AGENT_SHARED_KNOWLEDGE__MIGRATE_KNOWLEDGE_DEDUP_AND_5, (_h, _kid))
                            break
        except Exception as _e:
            log.warning(f"[migrate] content_hash backfill failed: {_e}")

        # 5. 清理孤儿 scene_knowledge_ref（knowledge_id 指向已删除的知识记录）
        # 去重删除 knowledge 记录时已先删 ref，但历史迁移轮次可能残留
        # 先 EXISTS 快速检查，有孤儿才执行 DELETE
        try:
            _has_orphan = c.execute(
                mgmt_mig.SELECT_SCENE_KNOWLEDGE_REF
            ).fetchone()[0]
            if _has_orphan:
                c.execute(
                    mgmt_mig.DELETE_SCENE_KNOWLEDGE_REF__MIGRATE_KNOWLEDGE_DEDUP_AND_3)
                log.info("[issue67] cleaned orphan scene_knowledge_ref")
        except Exception as _e:
            log.warning(f"[issue67] orphan ref cleanup failed: {_e}")

        db.commit()

    # 4. MySQL 原生 DDL——scene_knowledge_ref 表用原生 MySQL DDL 重建（如果翻译层产出不够严谨）
    if _is_mysql:
        try:
            from src.kernel.mysql_compat import mysql_config
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
    from src.kernel.session_manager import db, db_lock
    from src.domains.mgmt.user.db import get_meta_value, set_meta_value
    from src.kernel.repo_key import parse_repo_key, knowledge_obs_prefix
    from src.kernel.lock import acquire_lock, release_lock

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
            c.execute(mgmt_mig.UPDATE_AGENT_SHARED_KNOWLEDGE__MIGRATE_KNOWLEDGE)
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
        from src.domains.runtime.storage import get_storage
        _storage = get_storage()
        with db_lock:
            rows = db.cursor().execute(
                mgmt_mig.SELECT_AGENT_SHARED_KNOWLEDGE__MIGRATE_KNOWLEDGE
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
                    mgmt_mig.UPDATE_AGENT_SHARED_KNOWLEDGE__MIGRATE_KNOWLEDGE_2,
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
                mgmt_mig.SELECT_AGENT_SHARED_KNOWLEDGE__MIGRATE_KNOWLEDGE_2
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
                    mgmt_mig.UPDATE_AGENT_SHARED_KNOWLEDGE__MIGRATE_KNOWLEDGE_3,
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
        c.execute(runtime_mig.DELETE_META)
        db.commit()
        log.info("Cleaned stale meta key: ws_feishu_app_id")
    except Exception as e:
        log.warning(f"Cleanup stale meta ws_feishu_app_id failed: {e}")


def _cleanup_old_latency_logs():
    """清理 7 天前的 API 慢请求日志"""
    try:
        from src.kernel.mysql_compat import get_db_backend
        is_mysql = get_db_backend() == "mysql"
        time_expr = "NOW() - INTERVAL 7 DAY" if is_mysql else "datetime('now', '-7 days')"
        with db_lock:
            c = db.cursor()
            # 观测表归 insight：清理语句由该域迁移模块给出（时间表达式按方言传入）
            from src.domains.insight import migrations_ddl as insight_mig  # noqa: PLC0415
            c.execute(insight_mig.api_latency_prune(time_expr))
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
        from src.domains.runtime.engine.service import get_execution_mode, EXEC_MODE_REMOTE
        if get_execution_mode() == EXEC_MODE_REMOTE:
            return
    except Exception:
        pass
    try:
        with db_lock:
            c = db.cursor()
            rows = c.execute(runtime_mig.SELECT_CHAT_AGENT_SESSION__CLEANUP_CHAT_SHARED_WORKER_R).fetchall()
            changed = 0
            for cid, _rid, name in rows:
                local = c.execute(
                    runtime_mig.SELECT_AGENT_RUNTIMES__CLEANUP_CHAT_SHARED_WORKER_R,
                    ((name or "").lower(),)).fetchone()
                new_rid = local[0] if local else 0
                c.execute(runtime_mig.UPDATE_CHAT_AGENT_SESSION, (new_rid, cid))
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
            done = c.execute(runtime_mig.SELECT_META__ALIGN_AGENT_ENABLE).fetchone()[0]
            if done:
                return
            # 先确保 enable 列存在(新库可能无)
            _ensure_column(c, "agent_type", "enable", "INTEGER DEFAULT 1")
            _ensure_column(c, "devops_agent_session", "enable", "INTEGER DEFAULT 1")
            c.execute(
                runtime_mig.UPDATE_DEVOPS_AGENT_SESSION
            )
            c.execute(runtime_mig.UPSERT_META__ALIGN_AGENT_ENABLE)
            db.commit()
            log.info(runtime_mig.SQL_AGENT_TYPE)
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
    c.execute(runtime_mig.chat_agent_session_new_insert_select(cols))
    c.execute(runtime_mig.DROP_CHAT_AGENT_SESSION)
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
    c.execute(runtime_mig.DELETE_AGENT_RUNTIMES)
    cols = [r[1] for r in c.execute("PRAGMA table_info(agent_runtimes)").fetchall()]
    c.execute(_AGENT_RUNTIMES_DDL.replace("CREATE TABLE IF NOT EXISTS agent_runtimes", "CREATE TABLE agent_runtimes_new"))
    # 只复制新旧表共有的列（旧表可能有 busy_slots/max_concurrent/tags 等已废弃列）
    new_cols = [r[1] for r in c.execute("PRAGMA table_info(agent_runtimes_new)").fetchall()]
    common_cols = [col for col in cols if col in new_cols]
    c.execute(runtime_mig.agent_runtimes_new_insert_select(common_cols))
    c.execute(runtime_mig.DROP_AGENT_RUNTIMES)
    c.execute("ALTER TABLE agent_runtimes_new RENAME TO agent_runtimes")
    return True


def _alter_skill_files_if_needed() -> str:
    """agent_tasks.skill_files 仅非 MEDIUMTEXT 时返回 ALTER(避免每次启动 DDL 触发元数据锁).

    MySQL 元数据锁(metadata lock)在并发/残留 Sleep 连接下会等待--
    无条件每次 MODIFY 是启动变慢根因(2026-08-17 修复).
    SQLite 下无长度概念,直接返回空串(无害).
    """
    try:
        from src.kernel.session_manager import db
        rows = db.execute(
            "SELECT DATA_TYPE FROM information_schema.COLUMNS "
            "WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME='agent_tasks' "
            "AND COLUMN_NAME='skill_files'"
        ).fetchall()
        if rows and rows[0][0] == "mediumtext":
            return ""
        return runtime_mig.ALTER_AGENT_TASKS
    except Exception:
        return runtime_mig.ALTER_AGENT_TASKS__ALTER_SKILL_FILES_IF_NEEDED


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
                _issue_cnt = c.execute(runtime_mig.SELECT_ISSUE__TASKS_TO_ISSUE_RENAME_PRE_CR).fetchone()[0]
                _tasks_cnt = c.execute("SELECT COUNT(*) FROM tasks").fetchone()[0]
                if _issue_cnt == 0 and _tasks_cnt > 0:
                    c.execute(runtime_mig.DROP_ISSUE)
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


def _migrate_global_bot_token_to_db():
    """Issue #345: 全局 bot token 从文件迁移到 DB meta 表。

    集群安全：用 src/kernel/lock.py distributed_locks 表保护，N 个实例同时启动不冲突。
    set_meta_value 本身是 INSERT OR REPLACE（UPSERT 语义），多实例并发写同一值结果一致。
    幂等：DB 已有值则跳过。
    """
    from src.kernel.lock import acquire_lock, release_lock
    if not acquire_lock("migrate_global_bot_token", ttl=60):
        return  # 其他实例正在迁移，跳过
    try:
        from src.domains.mgmt.user.db import get_meta_value, set_meta_value
        # 用独立标记 key 判断是否已迁移，不用 token 值的 truthiness
        # （admin 可能把 token 清空，空值不应触发重新迁移覆盖）
        if get_meta_value("global_bot_token_migrated") == "1":
            return  # 已迁移
        import json, os
        _gbt_path = os.path.join(_CONFIG_DIR, "global-bot-token.json")
        if not os.path.exists(_gbt_path):
            return
        with open(_gbt_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        # Issue #345: 逐字段迁移——DB 已有值则跳过该字段（不覆盖 admin 已配置的值）
        # 跳过明显的占位符值（含省略号/过短）
        def _is_valid_token(val):
            """最小格式校验：非空、不含省略号、长度 >= 20。

            20 is an empirical threshold — production GitCode PATs are ~25 chars.
            Placeholder values in example configs (e.g. "ghp_zG...uUef") are shorter
            or contain ellipsis. This only affects migration (file → DB first boot);
            tokens rejected here fall back to file read until admin configures via API.
            """
            if not val or not isinstance(val, str):
                return False
            if "..." in val:
                return False
            return len(val) >= 20
        def _migrate_field(meta_key, file_key, validator=None):
            """DB 无值 + 文件有值 → 迁移；DB 有值 → 跳过。"""
            db_val = get_meta_value(meta_key)
            if db_val:
                return  # DB 已有值，不覆盖
            file_val = data.get(file_key)
            if not file_val:
                return
            if validator and not validator(file_val):
                log.warning(f"Issue #345: skip migrating {file_key} (looks like placeholder: len={len(str(file_val))})")
                return
            if isinstance(file_val, list):
                set_meta_value(meta_key, json.dumps(file_val))
            else:
                set_meta_value(meta_key, str(file_val))
        _migrate_field("global_bot_gitcode_token", "gitcode_api_token", _is_valid_token)
        _migrate_field("global_bot_github_token", "github_bot_token", _is_valid_token)
        _migrate_field("global_bot_github_username", "github_bot_username")
        _migrate_field("global_bot_usernames", "bot_usernames")
        _migrate_field("webhook_url", "webhook_url")
        # webhook token 文件里字段名可能是 gitcode_webhook_token 或 webhook_secret
        _migrate_field("gitcode_webhook_token", "gitcode_webhook_token")
        if not get_meta_value("gitcode_webhook_token"):
            _migrate_field("gitcode_webhook_token", "webhook_secret")
        # 标记迁移完成（独立标记 key，不依赖 token 值的 truthiness）
        set_meta_value("global_bot_token_migrated", "1")
        log.info("Issue #345: Global bot token migrated from file to DB meta")
        # R21.2.9: 旧 global_bot_token_status key 拆分为 per-provider key
        _old_status = get_meta_value("global_bot_token_status") or ""
        if _old_status:
            set_meta_value("global_gitcode_bot_token_status", _old_status)
            set_meta_value("global_github_bot_token_status", _old_status)
            set_meta_value("global_bot_token_status", "")  # 清除旧 key
            log.info(f"R21.2.9: migrated global_bot_token_status='{_old_status}' to per-provider keys")
    finally:
        release_lock("migrate_global_bot_token")


def _migrate_tables():
    """已有数据库的 schema 迁移"""
    # ===== new-version2 数据安全:迁移前自动备份(仅在有改动性迁移时触发一次)=====
    try:
        from src.kernel.session_manager import _backup_db, _db_path
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
                    c.execute(runtime_mig.INSERT_IGNORE_META)
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
        *runtime_mig.ALTERS_SESSION_EVENT_COLUMNS,  # 表：devops_agent_session, chat_agent_session, chat_conversations, events, agent_type, issue_state, skill,
        # skill_zip 已移除(本次需求新增,未上线,无包袱直砍--见 obs-skill-storage-design.md §5.7 v3 决策)
        # MySQL 下 skill_files 升级(SQLite 无长度限制,MODIFY 语句报错被忽略无害)
        # 2026-08-17 修复:DEFAULT '{}' 在 MySQL 上触发 metadata lock 卡启动--改为无默认;
        # 且 ALTER 仅在列非 MEDIUMTEXT 时执行(避免每次启动 DDL 触发元数据锁--启动变慢根因)
        _alter_skill_files_if_needed(),
        *runtime_mig.ALTERS_ENGINE_CREDENTIAL_COLUMNS,  # 表：runtime_credentials, agent_runtimes,
        *mgmt_mig.ALTERS_USER_AND_KNOWLEDGE_COLUMNS,  # 表：agent_shared_knowledge,
        *runtime_mig.ALTERS_SKILL_ENGINE_COLUMNS,  # 表：agent_tasks, agent_runtimes, runtime_health_scores, skill,
        *mgmt_mig.ALTERS_SCENE_COLUMNS,  # 表：devops_pipeline_scene, scene_skill_ref, scene_market,
        *runtime_mig.ALTERS_SCENE_CATEGORY_EVENT_SKIPPED,  # 表：scene_category, event_skipped,
        *mgmt_mig.ALTERS_USER_FEEDBACK_COLUMNS,  # 表：user_feedback,
    ]
    # 回填已有分类数据的 slug（幂等，仅 slug 为空时更新）
    _category_slugs = [
        ("general", "通用"), ("mobile", "移动开发"), ("cloud-native", "云原生"),
        ("iac", "IaC"), ("skill-dev", "技能开发"), ("ai", "AI应用"), ("content", "内容创作"),
    ]
    for slug, name in _category_slugs:
        try:
            with db_lock:
                db.execute(runtime_mig.UPDATE_SCENE_CATEGORY, (slug, name))
                db.commit()
        except Exception:
            pass
    for sql in migrations:
        try:
            with db_lock:
                if "DROP COLUMN" in sql or "RENAME COLUMN" in sql or "MODIFY COLUMN" in sql:
                    # Issue #536：破坏性 DDL 走唯一入口（cleanup 语义 + min_code_version 门槛）
                    _destructive_ddl(db.cursor(), sql, kind_hint="cleanup")
                else:
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
                    c.execute(runtime_mig.UPSERT_CHAT_AGENT_SESSION,
                              (sid, r[0], "", r[1] or "opencode", r[2] or ""))
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
            from src.domains.mgmt.user.db import ensure_default_backend, get_backend_id_by_name
            # 预置 opencode 默认 backend
            default_id = ensure_default_backend("opencode") or 0
            # devops_agent_session
            cols = [r[1] for r in c.execute("PRAGMA table_info(devops_agent_session)").fetchall()]
            if "backend" in cols and "backend_id" in cols:
                c.execute(runtime_mig.SELECT_DEVOPS_AGENT_SESSION)
                for r in c.fetchall():
                    bid = get_backend_id_by_name(r[0]) or ensure_default_backend(r[0]) or default_id
                    c.execute(runtime_mig.UPDATE_DEVOPS_AGENT_SESSION__MIGRATE_TABLES_3, (bid, r[0]))
                c.execute(runtime_mig.UPDATE_DEVOPS_AGENT_SESSION__MIGRATE_TABLES, (default_id,))
            # agent_type
            acols = [r[1] for r in c.execute("PRAGMA table_info(agent_type)").fetchall()]
            if "default_backend" in acols and "default_backend_id" in acols:
                c.execute(runtime_mig.SELECT_AGENT_TYPE)
                for r in c.fetchall():
                    bid = get_backend_id_by_name(r[0]) or ensure_default_backend(r[0]) or default_id
                    c.execute(runtime_mig.UPDATE_AGENT_TYPE__MIGRATE_TABLES, (bid, r[0]))
                c.execute(runtime_mig.UPDATE_AGENT_TYPE, (default_id,))
            # chat_agent_session
            ccols = [r[1] for r in c.execute("PRAGMA table_info(chat_agent_session)").fetchall()]
            if "backend" in ccols and "backend_id" in ccols:
                c.execute(runtime_mig.SELECT_CHAT_AGENT_SESSION)
                for r in c.fetchall():
                    bid = get_backend_id_by_name(r[0]) or ensure_default_backend(r[0]) or default_id
                    c.execute(runtime_mig.UPDATE_CHAT_AGENT_SESSION__MIGRATE_TABLES_2, (bid, r[0]))
                c.execute(runtime_mig.UPDATE_CHAT_AGENT_SESSION__MIGRATE_TABLES, (default_id,))
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
                    c.execute(runtime_mig.UPDATE_REPO_IM_GROUP_BINDING)
                    _ok = _destructive_ddl(c, runtime_mig.ALTER_REPO_IM_GROUP_BINDING,
                                           kind_hint="cleanup")
                    db.commit()
                    if _ok:
                        log.info("Migrated repo_im_group_binding: account -> im_app_id (dropped account)")
                # chat_id -> im_chat_id
                bcols2 = [r[1] for r in c.execute("PRAGMA table_info(repo_im_group_binding)").fetchall()]
                if "chat_id" in bcols2 and "im_chat_id" not in bcols2:
                    _ok = _destructive_ddl(c, runtime_mig.ALTER_REPO_IM_GROUP_BINDING__MIGRATE_TABLES,
                                           kind_hint="cleanup")
                    db.commit()
                    if _ok:
                        log.info("Migrated repo_im_group_binding: chat_id -> im_chat_id")
    except Exception as e:
        log.warning(f"Migrate repo_im_group_binding columns failed: {e}")
    # 清理历史 bot_username 占位值 + 纠正跨平台回填错误
    # Issue #345: 按 repo_key 前缀分流——github: 取 github_bot_username，其余取 GitCode bot name
    try:
        from src.domains.mgmt.user.db import get_global_bot_usernames, get_global_github_bot_username
        _us = get_global_bot_usernames()
        _gitcode_bot_name = _us[0] if _us else ""
        _github_bot_name = get_global_github_bot_username()
        with db_lock:
            c = db.cursor()
            # GitCode 仓库：bot_username 为空或占位值 → 填 GitCode bot name
            if _gitcode_bot_name:
                c.execute(
                    mgmt_mig.UPDATE_MANAGED_REPOS,
                    (_gitcode_bot_name,))
                _gc_rows = c.rowcount
            else:
                _gc_rows = 0
            # GitHub 仓库：bot_username 为空或占位值 → 填 GitHub bot username
            if _github_bot_name:
                c.execute(
                    mgmt_mig.UPDATE_MANAGED_REPOS__MIGRATE_TABLES,
                    (_github_bot_name,))
                _gh_rows = c.rowcount
            else:
                _gh_rows = 0
            # 纠正历史跨平台回填错误：GitHub 仓被填成 GitCode bot name → 修正为 GitHub bot username
            if _github_bot_name and _gitcode_bot_name and _github_bot_name != _gitcode_bot_name:
                c.execute(
                    mgmt_mig.UPDATE_MANAGED_REPOS__MIGRATE_TABLES_2,
                    (_github_bot_name, _gitcode_bot_name))
                _fix_rows = c.rowcount
            else:
                _fix_rows = 0
            db.commit()
            log.info(f"Migrated bot_username: gitcode={_gc_rows}, github={_gh_rows}, cross-platform-fix={_fix_rows}")
    except Exception as e:
        log.warning(f"Migrate bot_username placeholder failed: {e}")
    # Issue #345: 全局 bot token 从文件迁移到 DB meta 表（幂等，集群安全）
    try:
        _migrate_global_bot_token_to_db()
    except Exception as e:
        log.warning(f"Issue #345: migrate global bot token to DB failed: {e}")
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
                c.execute(runtime_mig.INSERT_IGNORE_DEVOPS_AGENT_SESSION)
                c.execute("DROP TABLE agent_sessions")
                db.commit()
                log.info(runtime_mig.SQL_DEVOPS_AGENT_SESSION)
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
                        runtime_mig.INSERT_IGNORE_AGENT_TYPE__MIGRATE_TABLES,
                        (r[0], r[1], r[2] or "", r[3] or "opencode", r[4] or "", r[5] or 0, r[6] or '[]')
                    )
                # 2. 官方技能(parent_agent_type!='')-> skill 表(scope=official)
                c.execute("SELECT agent_type, display_name, description, parent_agent_type, skill_path FROM agent_type_config WHERE parent_agent_type!=''")
                for r in c.fetchall():
                    c.execute(
                        runtime_mig.INSERT_IGNORE_SKILL,
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
                        runtime_mig.INSERT_IGNORE_SKILL__MIGRATE_TABLES,
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
    # 迁移 devops_agent_session.oc_session_id -> backend_session_id
    try:
        with db_lock:
            c = db.cursor()
            cols = [r[1] for r in c.execute("PRAGMA table_info(devops_agent_session)").fetchall()]
            if "oc_session_id" in cols and "backend_session_id" in cols:
                c.execute(runtime_mig.UPDATE_DEVOPS_AGENT_SESSION__MIGRATE_TABLES_2)
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
                _ok = _destructive_ddl(c, runtime_mig.ALTER_VOD_CONVERSATIONS,
                                       kind_hint="cleanup")
                _skipped = [] if _ok else ["feishu_chat_id->im_chat_id"]
            else:
                _ok, _skipped = True, []
            if "feishu_chat_name" in cols:
                _ok2 = _destructive_ddl(c, runtime_mig.ALTER_VOD_CONVERSATIONS__MIGRATE_TABLES,
                                        kind_hint="cleanup")
                _ok = _ok and _ok2
                if not _ok2:
                    _skipped.append("feishu_chat_name->im_channel_name")
            if "oc_session_id" in cols:
                _ok3 = _destructive_ddl(c, runtime_mig.ALTER_VOD_CONVERSATIONS__MIGRATE_TABLES_2,
                                        kind_hint="cleanup")
                _ok = _ok and _ok3
                if not _ok3:
                    _skipped.append("oc_session_id->backend_session_id")
            cols = [r[1] for r in c.execute("PRAGMA table_info(vod_conversations)").fetchall()]
            if "im_provider" not in cols:
                c.execute(runtime_mig.ALTER_VOD_CONVERSATIONS__MIGRATE_TABLES_3)
            db.commit()
            if _ok:
                log.info("Migrated vod_conversations fields to im_* / backend_session_id")
            else:
                log.warning(f"vod_conversations 部分迁移被 min_code_version 门槛拦截"
                            f"（升级后自动重试）: {_skipped}")
    except Exception as e:
        log.warning(f"Migrate vod_conversations fields failed: {e}")
    # 迁移 chat_conversations.session_id -> backend_session_id
    try:
        with db_lock:
            c = db.cursor()
            cols = [r[1] for r in c.execute("PRAGMA table_info(chat_conversations)").fetchall()]
            if "session_id" in cols and "backend_session_id" in cols:
                c.execute(runtime_mig.UPDATE_CHAT_CONVERSATIONS)
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
                c.execute(runtime_mig.SELECT_SKILL__MIGRATE_TABLES)
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
    # ===== managed_repos 部署URL列（weekly-01 合并） =====
    try:
        with db_lock:
            c = db.cursor()
            _ensure_column(c, "managed_repos", "deploy_url", "VARCHAR(512) DEFAULT ''")
            # 平台凭据列（多平台解耦后冻结为只读列：写路径已收拢到
            # grape_t_repo_credentials，但读取/回填路径仍按列名探测，冷启动库缺列会
            # OperationalError）。逐列幂等补齐。
            # F4/D106 终态（2026-11-11，S128 冷启动断言锁）：gitee/gitlab 四列不再
            # 补建——读链（token_resolver RepoBot）对卡列空平台直接短路不查列，
            # 写路径只落 rc 单表；存量库旧列由 backfill 迁入后写路径轮换置空。
            for _cred_col in ("bot_gitcode_token", "gitcode_webhook_token",
                              "bot_github_token", "github_webhook_token"):
                _ensure_column(c, "managed_repos", _cred_col, "TEXT DEFAULT ''")
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
            c.execute(mgmt_mig.UPDATE_DEVOPS_PIPELINE_SCENE__MIGRATE_TABLES)
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
    # ===== Issue #461: 场景级访问地址列（项目场景详情页地球仪图标登记；与 managed_repos.deploy_url 需求中心级互不影响）=====
    try:
        with db_lock:
            c = db.cursor()
            _ensure_column(c, "devops_pipeline_scene", "deploy_url", "VARCHAR(512) DEFAULT ''")
            db.commit()
            log.info("Added devops_pipeline_scene.deploy_url column")
    except Exception as e:
        log.warning(f"Add devops_pipeline_scene.deploy_url column failed: {e}")
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
    # ===== events 表补 failed_node 列（Issue #2432：失败终态落库失败节点名）=====
    try:
        with db_lock:
            c = db.cursor()
            _ensure_column(c, "events", "failed_node", "TEXT DEFAULT ''")
            db.commit()
            log.info("Added events.failed_node column")
    except Exception as e:
        log.warning(f"Add events.failed_node column failed: {e}")
    # ===== Issue #1938：events 表补级联收敛补偿归因列 =====
    # 补偿结论结构化落库（cascade_reason / report_missing / triggered_by + 原始任务终态 +
    # 最后已知事件状态），文案由字段渲染，展示层据此标注/过滤「系统补偿，原始回报丢失」。
    # 列类型用 VARCHAR + INTEGER（不用 TEXT）：MySQL 的 TEXT 列不支持 DEFAULT ''，
    # _ensure_column 的 INSTANT/INPLACE 降级路径会因此建不出列。
    try:
        with db_lock:
            c = db.cursor()
            _ensure_column(c, "events", "cascade_reason", "VARCHAR(64) DEFAULT ''")
            _ensure_column(c, "events", "report_missing", "INTEGER DEFAULT 0")
            _ensure_column(c, "events", "triggered_by", "VARCHAR(64) DEFAULT ''")
            _ensure_column(c, "events", "cascade_task_status", "VARCHAR(32) DEFAULT ''")
            _ensure_column(c, "events", "cascade_prev_event_status", "VARCHAR(32) DEFAULT ''")
            db.commit()
            log.info("Added events cascade compensation columns (Issue #1938)")
    except Exception as e:
        log.warning(f"Add events cascade compensation columns failed: {e}")
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
            # Issue #537：pending_self_report 中间态的超时基准列（老库幂等补列）
            _ensure_column(c, "agent_tasks", "pending_since", "TEXT DEFAULT NULL")
            # §6.4 追踪：trace_id（任务级轨迹）+ pipeline_trace_id（同一 issue 流程共享）
            _ensure_column(c, "agent_tasks", "trace_id", "TEXT DEFAULT ''")
            _ensure_column(c, "agent_tasks", "pipeline_trace_id", "TEXT DEFAULT ''")
            # 架构守护 BLOCKED-2（2026-11-09）：MR-2 跨进程 trace 串联依赖该列。
            # 之前只在 dev 库手工加列，新库/未 ALTER 的存量库会被 _avail 列过滤静默剔除，
            # 导致 worker 取不到 traceparent、execute/finish 各自成为新根 trace。
            _ensure_column(c, "agent_tasks", "trace_context", "TEXT DEFAULT ''")
            # 告警策略层（P1-1）：规则侧节流/聚合/时段/升级 + 事件侧通知状态
            # P2-1 SLO：采样表补"在线"列（老库迁移；历史行只能为 0 = 数据不足）
            _ensure_column(c, "obs_runtime_samples", "online", "INTEGER NOT NULL DEFAULT 0")
            _ensure_column(c, "obs_alert_rules", "repeat_sec", "INTEGER NOT NULL DEFAULT 0")
            _ensure_column(c, "obs_alert_rules", "aggregate_mode", "TEXT NOT NULL DEFAULT 'none'")
            _ensure_column(c, "obs_alert_rules", "aggregate_window_sec", "INTEGER NOT NULL DEFAULT 0")
            _ensure_column(c, "obs_alert_rules", "notify_window_json", "TEXT NOT NULL DEFAULT ''")
            _ensure_column(c, "obs_alert_rules", "escalate_json", "TEXT NOT NULL DEFAULT ''")
            _ensure_column(c, "obs_alert_events", "notify_count", "INTEGER NOT NULL DEFAULT 0")
            _ensure_column(c, "obs_alert_events", "last_notified_at", "TEXT DEFAULT ''")
            _ensure_column(c, "obs_alert_events", "escalated_at", "TEXT DEFAULT ''")
            _ensure_column(c, "obs_alert_events", "notify_deferred", "INTEGER NOT NULL DEFAULT 0")
            # P1-3：数据缺失/突变 来源标记 + 规则类型
            _ensure_column(c, "obs_alert_rules", "kind", "TEXT NOT NULL DEFAULT 'metric'")
            _ensure_column(c, "obs_alert_events", "source", "TEXT NOT NULL DEFAULT 'monitor'")
            # 用量台账按"运行主体"分类型（2026-10-17 决定）：worker 任务 run_kind='task'
            # （task_id 有值、run_id 同值）；主控机内直接跑的引擎（chat/devops/im/wiki）
            # 没有 agent_tasks 行 → task_id 空串、主体放 run_id。两表都补（归档表要能接住）。
            for _t in ("obs_llm_usage", "obs_llm_usage_archive"):
                _ensure_column(c, _t, "run_id", "TEXT DEFAULT ''")
                _ensure_column(c, _t, "run_kind", "TEXT DEFAULT 'task'")
            # 索引必须建在**加列之后**：初始化里那批 CREATE INDEX 跑在列迁移之前，
            # 老库上会因为列还不存在而失败（实测：新列有了、idx_obs_llm_run 没建成）。
            try:
                c.execute("CREATE INDEX IF NOT EXISTS idx_obs_llm_run ON obs_llm_usage(run_kind, run_id)")
            except Exception as _ie:
                log.warning(f"db_setup: create idx_obs_llm_run failed: {_ie}")
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
                # ⚠️ Issue #3773 的 4 个 acp_state_db_* / acp_resume_skipped_total 列**不在本段**：
                # 本段属冻结老轨（存量库已盖 legacy-stamp，启动不执行），新增列写在这里
                # 存量库永远没有该列 → 心跳 UPDATE 报 1054 Unknown column。这 4 列改走
                # 注册式迁移 202611240006（domains/runtime/migrations/acp_state_db_metrics_columns.py）。
                # 凭据名全局唯一:先清重(同名多行仅保留最小 id),再建唯一索引
                c.execute(runtime_mig.DELETE_RUNTIME_CREDENTIALS)
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
    # ===== multi-task mode: issue_state 已停用（C5），仅补 issue/chat_messages 列 =====
    try:
        with db_lock:
            c = db.cursor()
            _ensure_column(c, "issue", "parent_issue_number", "INTEGER DEFAULT 0")
            _ensure_column(c, "issue", "is_subtask_mode", "INTEGER DEFAULT 0")
            _ensure_column(c, "chat_messages", "task_id", "TEXT")
            _ensure_column(c, "chat_messages", "generate_status", "INTEGER DEFAULT NULL")
            _ensure_column(c, "chat_messages", "deleted", "INTEGER DEFAULT 0")
            db.commit()
        log.info("Added parent_issue_number/is_subtask_mode to issue + task_id/generate_status/deleted to chat_messages")
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
            _ensure_column(c, "issue", "scene_user_id", "VARCHAR(36) DEFAULT ''")
            _ensure_column(c, "issue", "allow_scene_override", "INTEGER DEFAULT 0")
            # 批 C：A2A 触发 + 子任务归属 + 更新时间
            _ensure_column(c, "issue", "a2a_seq", "INTEGER")
            _ensure_column(c, "issue", "parent_issue_id", "TEXT DEFAULT ''")
            _ensure_column(c, "issue", "updated_at", "TEXT DEFAULT ''")
            # Issue #329: cross-pipeline handoff carried context
            _ensure_column(c, "issue", "carried_context", "TEXT DEFAULT ''")
            # Issue #627 方案 C：发布子单（复用现有子任务机制）——绑定与结果字段落在子单 issue 行
            # 自身（替代 PR #799 旁路 production_release 表；零平行表、零状态机改动）。
            # release_result 三分支：deployed_success / deploy_failed / not_executed（空=未完成）。
            _ensure_column(c, "issue", "release_candidate_sha", "TEXT DEFAULT ''")
            _ensure_column(c, "issue", "release_target_env", "TEXT DEFAULT ''")
            _ensure_column(c, "issue", "release_evidence_ref", "TEXT DEFAULT ''")
            _ensure_column(c, "issue", "release_approved_by", "VARCHAR(36) DEFAULT ''")
            _ensure_column(c, "issue", "release_approved_at", "TEXT DEFAULT ''")
            _ensure_column(c, "issue", "release_result", "TEXT DEFAULT ''")
            _ensure_column(c, "issue", "release_failure_reason", "TEXT DEFAULT ''")
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
                    runtime_mig.UPDATE_ISSUE)
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
            # local-claimer 设计：pool_engine_ids 来源标记（scene=用户显式绑定 / auto=自动选择；
            # 白名单全灭时降级放行的判别依据，见 local-claimer-first-class-broadcast-design.md §3.3）
            _ensure_column(c, "agent_tasks", "pool_engine_source", "TEXT DEFAULT ''")
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
            from src.kernel.mysql_compat import get_db_backend
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
                            runtime_mig.SELECT_ISSUE_STATE
                        ).fetchone()[0]
                        _mismatch = c.execute(
                            runtime_mig.SELECT_ISSUE_STATE__MIGRATE_TABLES
                        ).fetchone()[0]
                        if _orphan == 0 and _mismatch == 0:
                            c.execute(runtime_mig.DROP_ISSUE_STATE)
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
    from src.kernel.mysql_compat import get_db_backend
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
                    c.execute(mgmt_mig.scene_market_drop_index(old_idx))
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
                        mgmt_mig.ALTER_SCENE_MARKET__REBUILD_SCENE_MARKET_UNIQUE
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
            c.execute(mgmt_mig.ALTER_SCENE_MARKET)
            c.execute(_SCENE_MARKET_DDL)
            # 复制数据（official 场景补 scope='official', repo_key=***）
            c.execute(
                mgmt_mig.INSERT_SCENE_MARKET
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
            mgmt_mig.SELECT_DEVOPS_PIPELINE_SCENE__MIGRATE_SCENE_TABLE_TO_REPO).fetchall()
        # user scope → repo_key 反查（user_repos）
        repo_of_user = {}
        try:
            for ur in c.execute(mgmt_mig.SELECT_USER_REPOS).fetchall():
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
        c.execute(mgmt_mig.DROP_DEVOPS_PIPELINE_SCENE)
        c.execute("ALTER TABLE devops_pipeline_scene_new RENAME TO devops_pipeline_scene")
        db.commit()
        log.info(
            f"Scene table migrated to repo model: repo_in={_moved_repo} user_in={_moved_user} dropped={_dropped}")


def _seed_official_scene_market_from_pipelines():
    """官方场景增量导入 scene_market（委托 mgmt 域内实现，避免跨域 SQL）。"""
    try:
        from src.domains.mgmt import public as _mgmt_public

        _pp = os.path.join(_CONFIG_DIR, "pipelines.json")
        n = _mgmt_public.seed_official_scene_market(_pp)
        if n:
            log.info(f"Seeded official scene market: {n}")
    except Exception as e:
        log.warning(f"Init scene market data failed: {e}")


def _backfill_scene_display_names():
    """场景表 display_name 回填：按 scene_id 从 scene_market(source='official') 匹配展示名（幂等）。"""
    try:
        with db_lock:
            c = db.cursor()
            # 确保列存在（老库补充）
            _cols = {r[1] for r in c.execute("PRAGMA table_info(devops_pipeline_scene)").fetchall()}
            if _cols and "display_name" not in _cols:
                c.execute(mgmt_mig.ALTER_DEVOPS_PIPELINE_SCENE)
                db.commit()
                log.info("Added display_name column to devops_pipeline_scene")
            # market 映射 scene_id(name) -> display_name
            _map = {}
            try:
                for r in c.execute(
                        mgmt_mig.SELECT_SCENE_MARKET__BACKFILL_SCENE_DISPLAY_NAMES):
                    _map[r[0]] = r[1]
            except Exception as _e:
                log.warning(f"_backfill_scene_display_names: read market failed: {_e}")
            # 回填：display_name 为空的行按 scene_id 匹配 market 的 name（官方场景标识符）
            _upd = 0
            for r in c.execute(
                    mgmt_mig.SELECT_DEVOPS_PIPELINE_SCENE__BACKFILL_SCENE_DISPLAY_NAMES):
                _row_id, _sid, _rname, _dn = r
                if _dn:
                    continue
                # scene_id 即官方场景标识符（如 default/iac），market.display_name 为其展示名
                _target = _map.get(_sid)
                if _target and _target != _sid:
                    c.execute(mgmt_mig.UPDATE_DEVOPS_PIPELINE_SCENE__BACKFILL_SCENE_DISPLAY_NAMES, (_target, _row_id))
                    _upd += 1
            db.commit()
            if _upd:
                log.info(f"Backfilled {_upd} scene display_name from the scene market rows")
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
            # Feishu subscription notification idempotency guard
            _ensure_column(c, "autopilot_executions", "notified", "INTEGER NOT NULL DEFAULT 0")
            db.commit()
            log.info("Autopilot v3.0 schema migration: agent_type/instruction/skill_name/findings_json/custom_fields/work_branch/notified ensured")
    except Exception as e:
        log.warning(f"Autopilot v3.0 schema migration failed: {e}")

    # ===== Organization 特性迁移 =====
    _migrate_add_org_id_columns()
    # Issue #343: 删除 _migrate_create_personal_orgs / _migrate_data_to_personal_orgs 调用
    # 这两个函数读 user_accounts 旧表用旧 ID 建组织，每次启动重复建，导致个人组织碎片化
    # （每用户 2~4 个个人组织）+ owner 挂错人（旧 ID 撞新用户）
    # 运行时 _ensure_personal_org（grape_auth.py）已在用户登录时按新 ID 正确建组织，不需要启动脚本重复建
    # _migrate_create_personal_orgs()  — removed
    # _migrate_data_to_personal_orgs()  — removed

    # Issue #343 P1-3: token_version+1 集成到 init_database，不依赖人工 SQL
    # 一次性强制旧 JWT 失效（旧 JWT sub=旧id 撞新表用户导致错位写入）。
    # 用 meta 表标记确保只执行一次（幂等）。
    try:
        with db_lock:
            c = db.cursor()
            c.execute(runtime_mig.SELECT_META__BACKFILL_SCENE_DISPLAY_NAMES)
            if not c.fetchone():
                c.execute(mgmt_mig.UPDATE_GRAPE_ACCOUNTS)
                c.execute(runtime_mig.UPSERT_META__BACKFILL_SCENE_DISPLAY_NAMES)
                db.commit()
                log.info("Issue #343: token_version +1 applied (old JWTs invalidated)")
    except Exception as e:
        log.warning(f"Issue #343 token_version invalidation failed: {e}")

    # Issue #344: clear cached plaintext OAuth tokens from the conversation and task tables
    # the agent now reads the token from the identity table at runtime (with pre-refresh)
    # meta-guard ensures one-time execution (aligned with #343 pattern)
    try:
        with db_lock:
            c = db.cursor()
            c.execute(runtime_mig.SELECT_META__BACKFILL_SCENE_DISPLAY_NAMES_2)
            if not c.fetchone():
                c.execute(runtime_mig.UPDATE_CHAT_CONVERSATIONS__BACKFILL_SCENE_DISPLAY_NAMES)
                c.execute(runtime_mig.UPDATE_AGENT_TASKS)
                c.execute(runtime_mig.UPSERT_META__BACKFILL_SCENE_DISPLAY_NAMES_2)
                db.commit()
                log.info(runtime_mig.SQL_CHAT_CONVERSATIONS)
    except Exception as e:
        log.warning(f"Issue #344 token cleanup failed: {e}")


# ── Issue #2091（上游 #10）：存量库补种 agent_type（登记在文件末尾，确保 register_migration 已定义）
register_migration(
    version="202611150001",
    name="seed-agent-types-from-config",
    kind="safe",
    fn=_fn_seed_agent_types_from_config,
)

# ── Issue #2095（上游 #14）：存量库补种 event_action_filter 默认规则
register_migration(
    version="202611150002",
    name="seed-event-action-filter-defaults",
    kind="safe",
    fn=_fn_seed_event_action_filter_once,
)


# ── A2（2026-11-13）：凭据类型归位——provider 存纯名，类型进 kind 列
def _fn_user_credentials_kind_model() -> None:
    """user_credentials 加 kind 列 + 存量 `<p>_pat/_oauth` 复合键回填归位行。

    幂等三态：列已存在 → _ensure_column no-op；归位行已存在 → backfill 跳过；
    旧复合键行保留（双写期，老实例按 `<p>_pat` 仍可读），收口迁移再删。
    """
    with db_lock:
        c = db.cursor()
        _ensure_column(c, "user_credentials", "kind", "TEXT NOT NULL DEFAULT ''",
                       allow_copy=True, strict=True)  # TEXT + 非 NULL DEFAULT：MySQL 需 COPY
        # QA N3（2026-11-13）：注册式迁移必须 strict=True——静默失败也会盖章，
        # 下次启动不再重试，线上表现为查询该列 1054（与既有 10 处 strict 用法一致）。
        db.commit()
    from src.domains.mgmt.user.db import backfill_credential_kind
    backfill_credential_kind()


register_migration(
    version="202611170006",
    name="user-credentials-kind-model",
    kind="safe",
    fn=_fn_user_credentials_kind_model,
)


def _table_present(c, tbl: str) -> bool:
    """表或视图是否存在于当前库（双后端）。"""
    if _mysql_backend():
        row = c.execute("SELECT 1 FROM information_schema.tables "
                        "WHERE table_schema=DATABASE() AND table_name=?", (tbl,)).fetchone()
    else:
        row = c.execute("SELECT 1 FROM sqlite_master WHERE name=? "
                        "AND type IN ('table','view')", (tbl,)).fetchone()
    return bool(row)


def _uc_unique_key_columns(c) -> set:
    """user_credentials 当前唯一键的列集合（SQLite: PRAGMA / MySQL: SHOW INDEX）。"""
    if _mysql_backend():
        try:
            rows = c.execute(mgmt_mig.SQL_USER_CREDENTIALS__UC_UNIQUE_KEY_COLUMNS).fetchall()
        except Exception:
            return set()
        by_key: dict = {}
        for r in rows:
            # MySQL 列序：Table, Non_unique, Key_name, Seq_in_index, Column_name, ...
            if str(r[1]) in ("0", "0.0"):
                by_key.setdefault(r[2], set()).add(str(r[4]))
        for cols in by_key.values():
            if {"user_id", "provider"} <= cols:
                return cols
        return set()
    for row in c.execute("PRAGMA index_list('user_credentials')").fetchall():
        name, unique = row[1], row[2]
        if not unique:
            continue
        info = c.execute(f"PRAGMA index_info('{name}')").fetchall()
        cols = {str(r[2]) for r in info}
        if {"user_id", "provider"} <= cols:
            return cols
    return set()


# ⚠️ 列清单在这里与 src/domains/mgmt/user/db.py 的 _CREDENTIALS_DDL **各持一份**
# （QA I1 已记录）：给 user_credentials 增删列必须两处同步，否则重建会静默丢列。
_UC_REBUILD_DDL = """CREATE TABLE user_credentials__uqkind (
    id VARCHAR(191) NOT NULL DEFAULT '',
    user_id VARCHAR(191) NOT NULL,
    provider VARCHAR(191) NOT NULL,
    kind VARCHAR(191) NOT NULL DEFAULT '',
    config_json MEDIUMTEXT NOT NULL,
    created_at VARCHAR(191) DEFAULT '',
    updated_at VARCHAR(191) DEFAULT '',
    PRIMARY KEY (user_id, provider, kind)
)"""

_UC_COPY_SQL = (mgmt_mig.INSERT_USER_CREDENTIALS)


_UC_TMP_TABLE = "user_credentials__uqkind"
_UC_BAK_TABLE = "user_credentials__old_backup"


def _uc_table_rows(c, table: str) -> int:
    """表存在则返回行数，不存在返回 -1（用于区分「表不存在」与「表为空」）。"""
    if not _table_present(c, table):
        return -1
    return int(c.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])


def _uc_plain_config(value) -> str:
    """config_json 的可比形态：密文先解密再比。

    AES-GCM 每次加密用随机 nonce，**同一明文两次加密得到的密文不同** —— 直接比
    密文会把「启动期加密回填」（对本表 UPDATE 重加密）误判成窗口丢写。明文/解密
    失败按原值返回（decrypt 本身兼容明文）。
    """
    raw = "" if value is None else str(value)
    try:
        from src.domains.mgmt.auth.credential_crypto import decrypt as _dec
        return _dec(raw)
    except Exception:
        return raw


def _uc_has_column(c, table: str, column: str) -> bool:
    """表是否含某列（MySQL: information_schema / SQLite: PRAGMA）。"""
    try:
        if _mysql_backend():
            row = c.execute(
                "SELECT 1 FROM information_schema.columns WHERE table_schema=DATABASE() "
                "AND table_name=? AND column_name=?", (table, column)).fetchone()
            return bool(row)
        return any(str(r[1]) == column
                   for r in c.execute(f"PRAGMA table_info('{table}')").fetchall())
    except Exception:
        return False


def _uc_rows_by_key(c, table: str) -> dict:
    """按 (user_id, provider, kind) 取 (解密后 config_json, updated_at)；表不存在返回空 dict。

    config_json 必须先解密再比：AES-GCM 每次加密用随机 nonce，同一明文两次加密的密文
    不同 —— 直接比密文会把「启动期加密回填」误判成丢写。
    没有 updated_at 列的历史/中转形态按空串处理（键级判定退化为「缺键才算丢」）。
    """
    if not _table_present(c, table):
        return {}
    ts_col = "COALESCE(updated_at,'')" if _uc_has_column(c, table, "updated_at") else "''"
    # ⚠️ MySQL 后端返回的是 MySQLCompatCursor，不可直接迭代（SQLite 可以）→ 必须 fetchall
    rows = c.execute(
        f"SELECT user_id, provider, COALESCE(kind,''), config_json, {ts_col} "
        f"FROM {table}").fetchall()
    return {(str(r[0]), str(r[1]), str(r[2])): (_uc_plain_config(r[3]), str(r[4]))
            for r in rows}


def _uc_lost_keys(bak: dict, live: dict) -> list:
    """真丢写的键（评审 M-1：不能只比内容集合差，否则「存活表被更新覆盖」会误报）。

    判定：
    - 存活表**缺键** ⇒ 真丢（该行只存在于备份表）；
    - 键在、但备份行的 updated_at **更新** ⇒ 真丢（存活表拿的是旧值）；
    - 键在、存活行更新/相同 ⇒ 不丢（多实例刷新把旧值覆盖成新值，数据没丢）。
    updated_at 为空串时按最小时间处理（空 < 非空）。
    """
    lost = []
    for key, (_cfg_b, ts_b) in bak.items():
        hit = live.get(key)
        if hit is None:
            lost.append(key)
            continue
        if ts_b and ts_b > hit[1]:
            lost.append(key)
    return lost


def _uc_backfill_kind_rows() -> None:
    """扩键后补一次 kind 归位（幂等）。

    必须挂在**所有** return 之前：`(gitee,'pat')` 这类归位行只由 006 的回填产出，
    而 006 在所有已升级库上早已盖章，本调用是「扩键之后再归位」的唯一入口。
    早退路径漏调（交换成功但进程在回填前退出、或键已扩却未盖章）会让 A2 收口
    删掉旧键行时 PAT 丢失。
    """
    from src.domains.mgmt.user.db import backfill_credential_kind
    backfill_credential_kind()


def _uc_cleanup_gate_strict() -> None:
    """重建表之前，要求运维显式声明「已无旧实例在写」。

    为什么不能只用 `_destructive_ddl` 的全局门槛：那个门槛服务全仓历史迁移，口径是
    「meta.min_code_version 未设置就放行」。而本迁移把整张表换掉，滚动窗口里旧实例
    仍在用 `ON CONFLICT(user_id, provider)` 写凭据 —— 交换后冲突目标失配，异常被
    `set_credential` 的 `except Exception` 吞成 `return False`，凭据静默不落库且没有
    失败信号。门槛比的是「本实例版本 vs meta」，识别不到「另一侧还有旧代码实例在跑」，
    所以这一层只能由部署前提承担：meta.min_code_version 必须先写上（停机/停写窗口，
    值 ≤ 本次部署版本），未设置即拒绝。

    拒绝 = raise → 执行器不盖章、下次启动重试，不会静默跳过。
    SQLite 是单进程单库、没有跨实例滚动窗口，直接放行；单机/测试可用
    GRAPE_ALLOW_UNGUARDED_CLEANUP=1 显式跳过（MySQL 门禁沙盒库即用此口）。
    """
    if not _mysql_backend():
        return
    if os.environ.get("GRAPE_ALLOW_UNGUARDED_CLEANUP") == "1":
        return
    min_ver = get_min_code_version()
    self_ver = current_code_version()
    if not min_ver:
        raise RuntimeError(
            "202611170007 拒绝重建 user_credentials：meta.min_code_version 未设置。"
            "本迁移会在滚动窗口内把整张表换掉，旧实例写凭据会静默失败。"
            "请确认已停写/全集群已升级，再执行 "
            "`python3 scripts/set_min_code_version.py <目标版本>`；"
            "单机或测试环境可设 GRAPE_ALLOW_UNGUARDED_CLEANUP=1。")
    if not _version_ge(self_ver, min_ver):
        _hint = ""
        if self_ver.startswith("fp-"):
            _hint = ("；当前是无 .git 部署（fp- 内容指纹），与日期式 min_code_version 不可比。"
                     "请用 GRAPE_DEPLOY_VERSION 注入可比版本（镜像 build 时 ENV），"
                     "或把 min_code_version 写成目标代码 current_code_version() 的 fp- 原值")
        raise RuntimeError(
            f"202611170007 拒绝重建 user_credentials：本实例版本 {self_ver!r} < "
            f"meta.min_code_version {min_ver!r}（落后节点不执行破坏性 DDL）{_hint}")


def _fn_user_credentials_unique_kind() -> None:
    """主键扩为 (user_id, provider, kind)（A2b 2026-11-13）。

    为什么必须：OAuth 刷新是现役写路径（refresh_oauth_token → set_credential(kind="oauth")），
    旧唯一键 (user_id, provider) 会把同平台 PAT 的归位行 upsert 成覆盖。滚动期靠双写
    `<p>_pat` 旧键行兜住，A2 收口删旧键行后 PAT 直接丢失（DB 副本实弹复现）。

    执行安全（C1/C2 + C-1/C-2/M-1/M-3 回修）：
    - 不做「异常回滚」假设：MySQL 的 DROP/ALTER 隐式提交。顺序固定为
      「搬数 → 行数守恒 → 原子交换 → 交换后复核 → 删备份」。
    - **每个 return 都是盖章路口**：早退分支先做残留验收 —— 存活表缺失而备份/中转表
      仍有数据 → raise（半交换态不能被当成新装库，否则空表被造出来、数据滞留备份表、
      迁移永不重试）。
    - DROP/RENAME 全部走 `_destructive_ddl(kind_hint="cleanup")`，被门槛拦截即 raise。
    - 重建前再过一层 `_uc_cleanup_gate_strict()`：meta.min_code_version 未设置时
      MySQL 侧拒绝（门槛拦不住「另一侧还在跑的旧实例」，只能靠部署前提）。
    - 回填 `_uc_backfill_kind_rows()` 挂在所有 return 之前（M-1）。
    - 先 `_is_base_table` 判形态：非 owner 域库看到的是跨库视图，不扩键、不盖章；
      该库靠 owner 库迁移后重建视图获得新列（本库每次启动都会重试并跳过，属设计）。
    """
    with db_lock:
        c = db.cursor()
        # ── 早退 A：存活表不存在 —— 新装库，或半交换态（C-1）───────────────
        if not _table_present(c, "user_credentials"):
            _leftover = {}
            for _t in (_UC_TMP_TABLE, _UC_BAK_TABLE):
                _n = _uc_table_rows(c, _t)
                if _n > 0:
                    _leftover[_t] = _n
            if _leftover:
                # 人工介入态：执行器只会降级成 warning，这里显式打 error 供巡检（评审 M-4）
                log.error(f"202611170007 半交换残留，需人工恢复: {_leftover}")
                raise RuntimeError(
                    f"202611170007 半交换残留：user_credentials 不存在，但 {_leftover} "
                    f"仍有数据（{_UC_BAK_TABLE}=旧表副本 / {_UC_TMP_TABLE}=新表副本）。"
                    "需人工核对后再恢复；不盖章、下次启动重试")
            for _t in (_UC_TMP_TABLE, _UC_BAK_TABLE):
                if _table_present(c, _t):
                    _destructive_ddl(c, f"DROP TABLE IF EXISTS {_t}", kind_hint="cleanup")
                    # _destructive_ddl 对执行失败只 warning 返回 True → 复查（评审 I-1）
                    if _table_present(c, _t):
                        log.warning(f"202611170007: 空残留表 {_t} 未清理（下次启动重试）")
            # 不调回填：表不存在=新装库，没有历史行可归位；此时 backfill 会
            # SELECT 一张不存在的表并 raise，把「表缺失 + 无残留」的库卡成
            # 每次启动失败（评审 I-1）。
            return
        if not _is_base_table(c, "user_credentials"):
            raise _NonOwnerObject(
                "user_credentials: 当前库中是视图（非 owner 域库），扩主键必须由 owner "
                "库执行；本库永不盖章，列形态靠 owner 库迁移后重建视图保证")
        cols = _uc_unique_key_columns(c)
        if "kind" in cols:
            # ── 早退 B：键已扩 —— 迁移已完成，或只差清理/回填 ─────────────────
            _live = _uc_table_rows(c, "user_credentials")
            _bak = _uc_table_rows(c, _UC_BAK_TABLE)
            _tmp = _uc_table_rows(c, _UC_TMP_TABLE)
            if _tmp > 0:
                raise RuntimeError(
                    f"202611170007 中断残留：{_UC_TMP_TABLE} 尚有 {_tmp} 行而存活表已扩键，"
                    "两次搬迁结果冲突，需人工核对（不盖章）")
            # 按键级判定（M-1）：缺键才是真丢；键在但内容不同要看 updated_at 谁更新，
            # 否则「存活表已被刷新覆盖」会被误判成丢写、迁移永久停滞。
            _lost = _uc_lost_keys(_uc_rows_by_key(c, _UC_BAK_TABLE),
                                  _uc_rows_by_key(c, "user_credentials"))
            if _lost:
                log.error(f"202611170007 备份表有存活表没有的行，需人工回收: {len(_lost)} 行")
                raise RuntimeError(
                    f"202611170007 备份表存在存活表没有的行：live={_live} backup={_bak}，"
                    f"丢失 {len(_lost)} 行（如 {sorted(_lost)[:3]}）。"
                    "交换窗口内可能有写入只落旧表，备份表保留待人工回收（不盖章）")
            for _t in (_UC_TMP_TABLE, _UC_BAK_TABLE):
                if _table_present(c, _t):
                    try:
                        if _destructive_ddl(c, f"DROP TABLE {_t}", kind_hint="cleanup"):
                            log.info(f"202611170007: 清理残留表 {_t}")
                    except Exception as _e:
                        log.warning(f"202611170007: 残留表 {_t} 未清理（数据安全）: {_e}")
            _uc_backfill_kind_rows()
            return
        # ── 重建路径 ────────────────────────────────────────────────────────
        _uc_cleanup_gate_strict()
        # 1) 上次中断留下的中转表：有数据即冲突（不能盲删），空表才清
        _tmp_rows = _uc_table_rows(c, _UC_TMP_TABLE)
        if _tmp_rows > 0:
            # 存活表仍是旧键：中转表是上次中断留下的纯派生数据（本次会重新搬数），
            # 丢弃重建是幂等的；硬 raise 只留给「存活表缺失」与「存活表已扩键」两个
            # 真危险态 —— 否则一次无害中断就变成必须人工 DROP 救火。
            log.warning(f"202611170007: 丢弃上次中断留下的中转表 {_UC_TMP_TABLE}"
                        f"（{_tmp_rows} 行，派生数据，将重新搬数）")
        if _table_present(c, _UC_TMP_TABLE):
            if not _destructive_ddl(c, f"DROP TABLE {_UC_TMP_TABLE}", kind_hint="cleanup"):
                raise RuntimeError("202611170007 被 cleanup 门槛拦截（不盖章，下版本重试）")
        # 2) 建新表 + 搬数 + 行数守恒（此时尚未动旧表）
        c.execute(_UC_REBUILD_DDL)
        c.execute(_UC_COPY_SQL)
        _n_old = _uc_table_rows(c, "user_credentials")
        _n_new = _uc_table_rows(c, _UC_TMP_TABLE)
        if _n_old != _n_new:
            raise RuntimeError(
                f"202611170007 搬数不守恒 old={_n_old} new={_n_new}（不盖章，下次重试）")
        # 3) 原子交换：MySQL 单条 RENAME TABLE 两表互换；SQLite 两步 RENAME 无事务
        #    保护，每步后复查（C-1：不能信 _destructive_ddl 的返回值）
        if _mysql_backend():
            if not _destructive_ddl(
                    c, mgmt_mig.SQL_USER_CREDENTIALS, kind_hint="cleanup"):
                raise RuntimeError("202611170007 被 cleanup 门槛拦截（不盖章，下版本重试）")
        else:
            for _src, _dst in (("user_credentials", _UC_BAK_TABLE),
                               (_UC_TMP_TABLE, "user_credentials")):
                if not _destructive_ddl(c, f"ALTER TABLE {_src} RENAME TO {_dst}",
                                        kind_hint="cleanup"):
                    raise RuntimeError("202611170007 被 cleanup 门槛拦截（不盖章）")
                if _table_present(c, _src) or not _table_present(c, _dst):
                    log.error(f"202611170007 交换未生效，半交换态: {_src} → {_dst}")
                    raise RuntimeError(
                        f"202611170007 交换未完成：{_src} → {_dst} 未生效（半交换态，"
                        "下次启动按残留验收处理，不盖章）")
        db.commit()
        # 交换后结构复核：不信任 _destructive_ddl 的返回值 —— 它对非 MDL 失败
        # 一律 log.warning 后返回 True（被门槛拦是 False，执行失败也是 True），
        # 所以「没抛错」不等于「交换生效」。主键不含 kind 即 raise，不盖章。
        _post_cols = _uc_unique_key_columns(c)
        if "kind" not in _post_cols:
            raise RuntimeError(
                f"202611170007 交换后主键仍不含 kind（唯一键列={sorted(_post_cols) or '无'}）："
                "RENAME 未生效，需人工核对（不盖章、下次启动重试）")
        log.info("migration 202611170007: user_credentials 主键扩为 "
                 "(user_id, provider, kind)")
        # 4) 交换后复核（M-3）：备份表里不得有存活表没有的行（内容级），否则窗口内
        #    有写入只落在旧表 —— 保留备份供人工回收，不删、不盖章
        _bak_rows = _uc_table_rows(c, _UC_BAK_TABLE)
        # 按键级复核（M-3 + M-1）：缺键=真丢；键在但备份的 updated_at 更新=真丢；
        # 键在且存活行更新=多实例刷新覆盖，不算丢（否则会误报并永久停滞）。
        _lost_after = _uc_lost_keys(_uc_rows_by_key(c, _UC_BAK_TABLE),
                                    _uc_rows_by_key(c, "user_credentials"))
        if _lost_after:
            log.error(f"202611170007 交换窗口丢写 {len(_lost_after)} 行，备份表已保留")
            raise RuntimeError(
                f"202611170007 交换窗口内有写入落在旧表：备份 {_bak_rows} 行 / 搬数 "
                f"{_n_old} 行，丢失 {len(_lost_after)} 行"
                f"（如 {sorted(_lost_after)[:3]}）。"
                f"备份表 {_UC_BAK_TABLE} 已保留待人工回收（不盖章）")
        # 5) 结论一致才删备份（失败只告警：数据仍在备份表里，可人工回收）
        try:
            if not _destructive_ddl(c, f"DROP TABLE {_UC_BAK_TABLE}", kind_hint="cleanup"):
                raise RuntimeError("cleanup 门槛拦截")
            db.commit()
        except Exception as _e:
            log.warning(f"202611170007: 备份表 {_UC_BAK_TABLE} 未删除"
                        f"（{_n_old} 行，数据安全，可手工清理）: {_e}")
    # 6) 扩键后补一次归位（M-1）：006 已盖章的库不会再跑 006，这一步是唯一入口
    _uc_backfill_kind_rows()


register_migration(
    version="202611170007",
    name="user-credentials-unique-kind",
    kind="cleanup",  # 重建表（DROP/RENAME）= 破坏性 DDL 语义，受 min_code_version 门槛
    fn=_fn_user_credentials_unique_kind,
)


# ── 扩键之后的 kind 归位补跑（202611170007 的配套，M-1）───────────────────
def _fn_user_credentials_kind_backfill() -> None:
    """扩键后再跑一次 kind 归位（幂等）。

    为什么单独注册而不是只挂在 007 末尾：007 的版本号在「已用旧版 007 跑过」的库上
    已经盖章，改 007 的函数体只会触发 checksum 漂移告警（不重跑）。凡是「键已扩、
    归位行仍缺」的库都要靠这条新号补一次；006/007 的归位本身幂等，重复执行零写入。

    视图守卫与 007 同口径：非 owner 域库里 user_credentials 是跨库视图，回填 INSERT
    在此必然失败 → 每次启动重试且永不盖章（纯日志噪声）。所以先判形态，视图库抛
    `_NonOwnerObject` 让执行器跳过（不盖章），回归位由「本库永不执行」这条口径驱动。
    """
    with db_lock:
        c = db.cursor()
        _present = _table_present(c, "user_credentials")
        _is_base = _is_base_table(c, "user_credentials") if _present else True
        _cols = _uc_unique_key_columns(c) if (_present and _is_base) else set()
    if not _present:
        return                      # 新装库：建表路径已含三列主键，无需回填
    if not _is_base:
        raise _NonOwnerObject(
            "user_credentials: 当前库中是视图（非 owner 域库），回填归位必须由 owner 库执行")
    if "kind" not in _cols:
        # 依赖隔离（评审 M-1）：007 未完成时（门槛拒跑 / 半交换残留 / 中断残留），
        # 唯一键仍是两列 —— 此时补 (纯名, kind) 归位行必撞旧 UNIQUE(user_id, provider)，
        # 变成每次启动一条 IntegrityError。跳过不盖章，等 007 扩键后的下次启动再来。
        raise RuntimeError(
            "202611170008 跳过：user_credentials 唯一键仍不含 kind（007 未完成）。"
            "补归位行会撞旧两列 UNIQUE(user_id, provider)，不执行、不盖章，"
            "等 007 扩键后的下次启动重试")
    _uc_backfill_kind_rows()


# ── sweeper 的 chat 会话状态索引（2026-11-15）─────────────────────────────
def _fn_chat_conversations_status_index() -> None:
    """chat_conversations(status) 索引：sweeper 每轮都跑 `WHERE status='active'`，原先全表扫。

    跨域守卫：chat_conversations 归 runtime，只有 owner 库能建索引。其它域的库里它是
    视图或不存在 → 抛 _NonOwnerObject，执行器跳过且不盖章（下次启动在该域仍跳过，
    真正的建索引发生在 runtime 的 worker 进程）。
    MySQL 的 TEXT 列做键必须带前缀长度：`status` 在建表 DDL 里是 TEXT，且没有任何迁移
    把它改成 VARCHAR，所以 MySQL 分支显式写 `status(191)`（与 cleanup.py 的运行期写法一致）。
    **不能指望 mysql_compat 的自动补前缀**——那段特判只匹配 `CREATE INDEX IF NOT EXISTS`
    一种形式，裸 CREATE INDEX 在会话游标与域连接游标上都原样透传，MySQL 会报 1170。
    也不能只写 (32)：191 是 utf8mb4 前缀索引的安全上限口径。
    """
    # chat_conversations 归 runtime：三库模式下在 owner 连接上建索引，否则恒判「不在本库」
    conn = _migration_conn("runtime")
    with (db_lock if conn is db else nullcontext()):
        c = conn.cursor()
        present = _table_present(c, "chat_conversations")
        is_base = _is_base_table(c, "chat_conversations") if present else True
    if not present:
        raise _NonOwnerObject(
            "chat_conversations 不在本库（非 owner 域），状态索引由 runtime 库建")
    if not is_base:
        raise _NonOwnerObject(
            "chat_conversations 在本库是视图（非 owner 域），状态索引由 runtime 库建")
    _c2 = conn.cursor()
    if _mysql_backend():
        # MySQL 不支持 CREATE INDEX IF NOT EXISTS（1064），重复索引只能按 1061 吞；
        # 同时 TEXT 列必须带前缀长度（否则 1170），自动补前缀的兼容层特判只认 IF NOT EXISTS 形式
        try:
            _c2.execute("CREATE INDEX idx_chat_conv_status ON chat_conversations(status(191))")
        except Exception as e:  # noqa: BLE001
            if "1061" not in str(e) and "already exists" not in str(e).lower():
                raise
    else:
        _c2.execute("CREATE INDEX IF NOT EXISTS idx_chat_conv_status ON chat_conversations(status)")
    conn.commit()


register_migration(
    version="202611220001",
    name="chat-conversations-status-index",
    kind="safe",
    fn=_fn_chat_conversations_status_index,
)

register_migration(
    version="202611170008",
    name="user-credentials-kind-backfill-after-widen",
    kind="backfill",  # 纯回填、无 DDL：不受 min_code_version 门槛约束
    fn=_fn_user_credentials_kind_backfill,
)


def _fn_add_events_failed_node() -> None:
    """Issue #2432: backfill events.failed_node on existing databases.

    The old-track `_migrate_tables` branch is frozen once a database carries
    the legacy stamp, so the column has to be registered as its own idempotent
    migration; without it a stamped database never gets the column and the
    failure notice loses its node name.
    """
    try:
        with db_lock:
            c = db.cursor()
            _ensure_column(c, "events", "failed_node", "TEXT DEFAULT ''")
            db.commit()
            log.info("Added events.failed_node column (Issue #2432)")
    except Exception as e:
        log.warning(f"Add events.failed_node column failed (Issue #2432): {e}")


register_migration(
    version="202611160020",
    name="issue2432-events-failed-node-column",
    kind="safe",
    fn=_fn_add_events_failed_node,
)


# ===== D109 平台对象模型六表（设计分片 d109 §4；DDL 单一来源 src/api/platform_object_ddl.py）=====

def _fn_create_platform_object_tables() -> None:
    """建六表 + 部分唯一索引的方言等价 + grape_t_repo_credentials 补 created_at / derived_from。

    幂等：IF NOT EXISTS + PRAGMA/信息模式判列；抛错不吞（执行器约定：抛错=不盖章，下次启动重试）。

    方言：**逐条 cursor.execute** —— `executescript` 是 sqlite3 专有 API，不经过
    `mysql_compat` 的翻译层，MySQL 后端下会 1064；项目既有建表迁移一律逐条 execute
    （db_setup.py:4788/5021）。唯一真正不兼容的是 bot 默认位的**部分唯一索引**
    （SQLite 的 `WHERE is_default = 1`）：MySQL 用 STORED 生成列 + 复合唯一键等价
    （is_default=0 行生成列为 NULL，MySQL 唯一键不约束 NULL）。
    """
    from src.api.platform_object_ddl import iter_statements
    from src.kernel.config import log
    from src.kernel.mysql_compat import get_db_backend
    from src.kernel.session_manager import db, db_lock

    backend = get_db_backend()
    with db_lock:
        c = db.cursor()
        for stmt in iter_statements():
            # 表归属守卫：平台对象四表归 mgmt 域，非 owner 进程不建（否则三库形态下
            # 每个进程各建一份同名基表——设计明令禁止；单库/无法判定域时守卫放行）。
            _tbl = ""
            _low = stmt.lower()
            _i = _low.find("create table")
            if _i >= 0:
                _rest = stmt[_i + len("create table"):].strip()
                if _rest.lower().startswith("if not exists"):
                    _rest = _rest[len("if not exists"):].strip()
                _tbl = _rest.split("(")[0].split()[0].replace(chr(34), "").replace(chr(39), "").replace("`", "") if _rest else ""
            if _tbl and _owned_skip(_tbl, "D109 create table"):
                continue
            c.execute(stmt)
        # D110：bot 账号目录已退役（bot 归平台行），部分唯一索引/生成列的方言适配一并删除

        # grape_t_repo_credentials 归 insight 域：只有 owner 进程给该表补列
        if not _owned_skip("grape_t_repo_credentials", "ensure column"):
            cols = {r[1] for r in c.execute("PRAGMA table_info(grape_t_repo_credentials)").fetchall()}
            if cols:  # 表存在才补列（rc 由 202611150003 建）
                # MySQL 的 TEXT 不能带 DEFAULT，补列用 VARCHAR；SQLite 保持 TEXT DEFAULT ''
                decl_text = "VARCHAR(32) NOT NULL DEFAULT ''" if backend == "mysql" else "TEXT NOT NULL DEFAULT ''"
                for col, decl in (("created_at", decl_text), ("derived_from", decl_text)):
                    if col not in cols:
                        c.execute(f"ALTER TABLE grape_t_repo_credentials ADD COLUMN {col} {decl}")
        db.commit()


register_migration(
    version="202611170030",
    name="create-platform-object-tables",
    kind="safe",
    fn=_fn_create_platform_object_tables,
)

# ===== 凭据能力表收口（2026-11-14：删除 configs/repo-credentials.json）=====

def _fn_merge_credential_caps_into_platforms() -> None:
    """凭据能力表并入平台表：补列 → 回填存活字段 → 删表。

    背景：`configs/repo-credentials.json` 已删除（drivers / oauth_requires_scope /
    supports_bot_token / supports_webhook_secret 不再有语义）。只剩 pat_create_url /
    token_storage_field / webhook_token_field / bot_username / owner_fallback 仍被运行时读取，
    它们本来就在 `configs/platforms/<p>.json` 里声明 → 直接落到平台表同名列。

    幂等：逐列 PRAGMA / information_schema 判存在；表已删则什么都不做。
    破坏性 DDL（DROP TABLE）走 `_destructive_ddl` 唯一入口，受 min_code_version 门槛。
    """
    from src.kernel.config import log
    from src.kernel.mysql_compat import get_db_backend
    from src.kernel.session_manager import db, db_lock

    backend = get_db_backend()
    text_decl = "VARCHAR(64) NOT NULL DEFAULT ''" if backend == "mysql" else "TEXT NOT NULL DEFAULT ''"
    cols = {"pat_create_url": text_decl, "token_storage_field": text_decl,
            "webhook_token_field": text_decl, "bot_username": text_decl,
            "owner_fallback": "INTEGER NOT NULL DEFAULT 0"}
    with db_lock:
        c = db.cursor()
        if backend == "mysql":
            have = {r[0] for r in c.execute(
                "SELECT column_name FROM information_schema.columns"
                " WHERE table_schema = DATABASE() AND table_name = 'grape_t_repo_platforms'"
            ).fetchall()}
            exists = c.execute(
                "SELECT COUNT(*) FROM information_schema.tables"
                " WHERE table_schema = DATABASE() AND table_name = 'grape_t_repo_credential_caps'"
            ).fetchone()
            caps_exists = bool(exists and exists[0])
        else:
            have = {r[1] for r in c.execute("PRAGMA table_info(grape_t_repo_platforms)").fetchall()}
            caps_exists = bool(c.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='grape_t_repo_credential_caps'"
            ).fetchone())
        for col, decl in cols.items():
            if col not in have:
                c.execute(f"ALTER TABLE grape_t_repo_platforms ADD COLUMN {col} {decl}")
        if caps_exists:
            rows = c.execute(
                "SELECT provider, pat_create_url, token_storage_field, webhook_token_field,"
                " bot_username, owner_fallback FROM grape_t_repo_credential_caps"
            ).fetchall()
            for provider, pat, tok, wh, bot, owner in rows:
                c.execute(
                    "UPDATE grape_t_repo_platforms SET pat_create_url = ?, token_storage_field = ?,"
                    " webhook_token_field = ?, bot_username = ?, owner_fallback = ? WHERE provider = ?",
                    (pat or "", tok or "", wh or "", bot or "", int(owner or 0), provider),
                )
            if not _destructive_ddl(c, "DROP TABLE IF EXISTS grape_t_repo_credential_caps",
                                    version="202611170031"):
                db.commit()
                raise RuntimeError("202611170031 删凭据能力表被 min_code_version 门槛拦截，下次启动重试")
            log.info("202611170031 凭据能力字段已并入 grape_t_repo_platforms，并删除 grape_t_repo_credential_caps")
        db.commit()
        # 复检（2026-11-14 实锤：本迁移在 MySQL 上盖了章，但 5 个列一个都没建、caps 表也还在
        # ——线上 ORM 因此每次读平台表都抛 Unknown column 走文件兜底）。DDL 的"执行成功"不等于
        # "库真的变了"，所以这里按最终状态判定，不满足就不盖章、下次启动重试。
        _missing, _caps_still = [], False
        if backend == "mysql":
            _have = {r[0] for r in c.execute(
                "SELECT column_name FROM information_schema.columns"
                " WHERE table_schema = DATABASE() AND table_name = 'grape_t_repo_platforms'"
            ).fetchall()}
            _missing = [col for col in cols if col not in _have]
            _caps_still = bool(c.execute(
                "SELECT COUNT(*) FROM information_schema.tables"
                " WHERE table_schema = DATABASE() AND table_name = 'grape_t_repo_credential_caps'"
            ).fetchone()[0])
        else:
            _have = {r[1] for r in c.execute("PRAGMA table_info(grape_t_repo_platforms)").fetchall()}
            _missing = [col for col in cols if col not in _have]
            _caps_still = bool(c.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='grape_t_repo_credential_caps'"
            ).fetchone())
        if _missing or _caps_still:
            raise RuntimeError(
                f"202611170031 复检未通过（缺列={_missing}，caps 表仍在={_caps_still}）——"
                f"DDL 未真正生效，拒绝盖章，下次启动重试")


register_migration(
    version="202611170031",
    name="merge-credential-caps-into-platforms",
    kind="cleanup",  # 含 DROP TABLE，走 cleanup 语义（min_code_version 门槛）
    fn=_fn_merge_credential_caps_into_platforms,
)


# ===== 配对即授权收口（2026-11-14）：删 grape_t_channel_repo_link.relation =====

def _fn_drop_channel_link_relation_column() -> None:
    """relation 列不再参与任何判定（配对即授权），随本批删除。

    幂等：SQLite PRAGMA / MySQL information_schema 判列；删列属危险 DDL，
    走 `_destructive_ddl` 唯一入口（cleanup 语义 + min_code_version 门槛）。
    """
    from src.kernel.mysql_compat import get_db_backend
    from src.kernel.session_manager import db, db_lock

    backend = get_db_backend()
    with db_lock:
        c = db.cursor()
        if backend == "mysql":
            cols = {r[0] for r in c.execute(
                "SELECT column_name FROM information_schema.columns"
                " WHERE table_schema = DATABASE() AND table_name = 'grape_t_channel_repo_link'"
            ).fetchall()}
        else:
            cols = {r[1] for r in c.execute("PRAGMA table_info(grape_t_channel_repo_link)").fetchall()}
        if "relation" not in cols:
            db.commit()
            return
        if not _destructive_ddl(c, "ALTER TABLE grape_t_channel_repo_link DROP COLUMN relation",
                                version="202611170032"):
            db.commit()
            raise RuntimeError("202611170032 删 relation 列被 min_code_version 门槛拦截，下次启动重试")
        db.commit()
        # 复检：`_destructive_ddl` 对非 MDL 异常只告警不抛（历史行为），不复检就会把
        # "实际没删"盖成"已删"（2026-11-14 实锤：MySQL 上盖章了但列仍在）。拒绝盖章。
        if backend == "mysql":
            cols_after = {r[0] for r in c.execute(
                "SELECT column_name FROM information_schema.columns"
                " WHERE table_schema = DATABASE() AND table_name = 'grape_t_channel_repo_link'"
            ).fetchall()}
        else:
            cols_after = {r[1] for r in c.execute("PRAGMA table_info(grape_t_channel_repo_link)").fetchall()}
        if "relation" in cols_after:
            raise RuntimeError("202611170032 relation 列仍在（DROP 未实际生效，拒绝盖章）")
        log.info("202611170032 已删除 grape_t_channel_repo_link.relation（配对即授权，档位退役）")


register_migration(
    version="202611170032",
    name="drop-channel-link-relation-column",
    kind="cleanup",  # 含 DROP COLUMN，走 cleanup 语义（min_code_version 门槛）
    fn=_fn_drop_channel_link_relation_column,
)


# ===== 同域配对收口（2026-11-14 用户裁定「同域配对是错误配置」）=====

# 迁移内容（含 SQL）住在 owner 域：src/domains/mgmt/migrations/drop_same_domain_links.py
# 引导器只负责注册，不再持有本域表的 SQL。
from src.domains.mgmt.migrations.drop_same_domain_links import MIGRATION as _M_DROP_SAME_DOMAIN

register_migration(**_M_DROP_SAME_DOMAIN)



# ===== D110：bot 归属收敛到仓库平台行（一平台一个 bot；bot_accounts 退役）=====

_BOT_PLATFORM_COLUMNS = (
    # (列名, SQLite 声明, MySQL 声明) —— MySQL 的 TEXT 不能带字面量 DEFAULT，补列用 VARCHAR
    ("bot_token_enc", "TEXT NOT NULL DEFAULT ''", "VARCHAR(2048) NOT NULL DEFAULT ''"),
    ("bot_token_status", "TEXT NOT NULL DEFAULT 'unknown'", "VARCHAR(16) NOT NULL DEFAULT 'unknown'"),
    ("bot_checked_at", "TEXT NOT NULL DEFAULT ''", "VARCHAR(32) NOT NULL DEFAULT ''"),
)


def _fn_add_platform_bot_columns() -> None:
    """grape_t_repo_platforms 补 3 列（幂等；表不存在则跳过，建表迁移先跑）。

    D110 决策：bot 的家 = 平台行（一平台一个 bot），仓库级覆盖仍在 rc('bot')。
    """
    from src.kernel.mysql_compat import get_db_backend
    from src.kernel.session_manager import db, db_lock

    backend = get_db_backend()
    with db_lock:
        c = db.cursor()
        cols = {row[1] for row in c.execute("PRAGMA table_info(grape_t_repo_platforms)").fetchall()}
        if not cols:
            log.info("202611170034 grape_t_repo_platforms 不存在，跳过补列")
            return
        added = []
        for name, sqlite_decl, mysql_decl in _BOT_PLATFORM_COLUMNS:
            if name in cols:
                continue
            c.execute(f"ALTER TABLE grape_t_repo_platforms ADD COLUMN {name} "
                      f"{mysql_decl if backend == 'mysql' else sqlite_decl}")
            added.append(name)
        db.commit()
    log.info(f"202611170034 平台 bot 列已就绪（本轮新增 {added or '无'}）")


register_migration(
    version="202611170034",
    name="add-platform-bot-columns",
    kind="safe",
    fn=_fn_add_platform_bot_columns,
)


def _fn_strip_org_analysis_reserved_fields() -> None:
    """清掉组织分析自动化里的系统保留 custom_fields（幂等）。

    历史 ensure_analysis_task 会写入 GRAPE_MANAGED_BY / GRAPE_ORG_ID /
    GRAPE_ANALYSIS_MODE。GRAPE_* 是保留环境变量：前端自定义配置页标红、保存被拒，
    执行侧也拒绝注入（日志 `custom env ... rejected`）。现在 org_id/mode 由任务
    指令和本次运行参数提供，受管身份走 skill_name；这些字段不再生成，存量行在此
    一次性删除。
    """
    _names = {"GRAPE_MANAGED_BY", "GRAPE_ORG_ID", "GRAPE_ANALYSIS_MODE"}
    from src.kernel.mysql_compat import get_db_backend

    with db_lock:
        c = db.cursor()
        if get_db_backend() == "mysql":
            _tbl = c.execute(
                "SELECT table_name FROM information_schema.tables "
                "WHERE table_schema=DATABASE() AND table_name='autopilot_tasks'"
            ).fetchone()
        else:
            _tbl = c.execute(
                "SELECT name FROM sqlite_master WHERE type='table' "
                "AND name='autopilot_tasks'"
            ).fetchone()
        if not _tbl:
            log.info("202611201700 autopilot_tasks 不存在，跳过")
            return
        rows = c.execute(
            "SELECT id, custom_fields FROM autopilot_tasks "
            "WHERE custom_fields LIKE '%GRAPE%'"
        ).fetchall()
        changed = 0
        for _task_id, _raw in rows:
            try:
                fields = json.loads(_raw or "[]")
            except Exception:
                continue
            if not isinstance(fields, list):
                continue
            cleaned = [
                f for f in fields
                if not (isinstance(f, dict)
                        and str(f.get("name") or "").strip() in _names)
            ]
            if len(cleaned) == len(fields):
                continue
            c.execute(
                "UPDATE autopilot_tasks SET custom_fields=? WHERE id=?",
                (json.dumps(cleaned, ensure_ascii=False) if cleaned else "", _task_id),
            )
            changed += 1
        db.commit()
    if changed:
        log.info(f"202611201700 已清理 {changed} 个自动化任务的保留 custom_fields")


register_migration(
    version="202611201700",
    name="strip-org-analysis-reserved-custom-fields",
    kind="safe",
    domains=("mgmt",),
    fn=_fn_strip_org_analysis_reserved_fields,
)


# 已盖章迁移的源码（含 docstring）是 checksum 冻结面：改动会让每次启动报"漂移"。
# 因此下面的原 docstring 保持盖章时的原文；与事实不符之处在此更正：
#   - meta 的 global_bot_*_token 是**历史明文**，本迁移会把它原样搬进 bot_token_enc
#     （线上实锤：gitcode 24 字符 / github ghp_ 40 字符落成了明文），
#     由 202611170039 统一就地加密；bot_accounts.token_enc 才是密文、原样搬。
def _fn_seed_platform_bot() -> None:
    """把 meta 的全局 bot（含历史别名）与 bot_accounts 的默认行搬进平台行（幂等）。

    读序口径（D110 §4）：平台行 → meta → 文件。迁移只补空位，不覆盖任何既有值；
    bot_accounts 的 token_enc 与平台行同一加密方案，密文直接搬（不落明文）。
    """
    from src.kernel.mysql_compat import get_db_backend
    from src.kernel.session_manager import db, db_lock

    _alias = {"gitcode": ("gitcode_api_token", "global_bot_gitcode_token"),
              "github": ("github_bot_token", "global_bot_github_token")}
    with db_lock:
        c = db.cursor()
        rows = c.execute("SELECT provider, bot_username, bot_token_enc "
                         "FROM grape_t_repo_platforms").fetchall()
        if not rows:
            log.info("202611170035 平台表为空，跳过 bot 播种")
            return
        has_bot_table = False
        try:
            if get_db_backend() == "mysql":
                has_bot_table = bool(c.execute(
                    "SELECT table_name FROM information_schema.tables"
                    " WHERE table_schema = DATABASE() AND table_name = 'grape_t_bot_accounts'"
                ).fetchone())
            else:
                has_bot_table = bool(c.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name='grape_t_bot_accounts'"
                ).fetchone())
        except Exception as _e:
            log.warning(f"202611170035 探测 bot_accounts 失败（按不存在处理）: {_e}")
        filled = []
        for provider, username, token_enc in rows:
            _new_username, _new_token = username or "", token_enc or ""
            if not _new_token:
                meta_keys = [f"global_bot_{provider}_token", *_alias.get(provider, ())]
                for key in meta_keys:
                    row = c.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
                    if row and row[0]:
                        _new_token = row[0]
                        break
            if not _new_token and has_bot_table:
                row = c.execute(
                    "SELECT token_enc FROM grape_t_bot_accounts "
                    "WHERE provider=? AND is_default=1 AND token_enc!='' LIMIT 1", (provider,)
                ).fetchone()
                if row and row[0]:
                    _new_token = row[0]
            if not _new_username:
                for key in (f"global_bot_{provider}_username",):
                    row = c.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
                    if row and row[0]:
                        _new_username = row[0]
                        break
            if not _new_username and has_bot_table:
                row = c.execute(
                    "SELECT username FROM grape_t_bot_accounts "
                    "WHERE provider=? ORDER BY is_default DESC, created_at ASC LIMIT 1", (provider,)
                ).fetchone()
                if row and row[0]:
                    _new_username = row[0]
            if (_new_username, _new_token) != (username or "", token_enc or ""):
                c.execute("UPDATE grape_t_repo_platforms SET bot_username=?, bot_token_enc=?, "
                          "updated_at=datetime('now') WHERE provider=?",
                          (_new_username, _new_token, provider))
                filled.append(provider)
        # 迁移标记：兼容期 meta 仍作第 3 级来源，标记只用于"文件 fallback 是否还在服役"
        c.execute("INSERT OR REPLACE INTO meta (key, value) VALUES ('bot_platform_migrated', '1')")
        db.commit()
    log.info(f"202611170035 平台 bot 播种完成（本轮写入 {filled or '无'}）")


register_migration(
    version="202611170035",
    name="seed-platform-bot-from-meta",
    kind="backfill",
    fn=_fn_seed_platform_bot,
)


def _fn_widen_repo_credential_derived_from() -> None:
    """MySQL：grape_t_repo_credentials.derived_from VARCHAR(32) → VARCHAR(36)。

    该列存 bot 账号 id，uuid7_str() 是 36 字符；32 宽在 strict 模式下写入即 1406。
    SQLite 侧是 TEXT，无需处理。
    """
    from src.kernel.mysql_compat import get_db_backend
    from src.kernel.session_manager import db, db_lock

    if get_db_backend() != "mysql":
        return
    with db_lock:
        c = db.cursor()
        row = c.execute(
            "SELECT CHARACTER_MAXIMUM_LENGTH FROM information_schema.columns"
            " WHERE table_schema = DATABASE() AND table_name = 'grape_t_repo_credentials'"
            " AND column_name = 'derived_from'"
        ).fetchone()
        if not row:
            return
        width = int(row[0] or 0)
        if width >= 36:
            return
        _destructive_ddl(c, "ALTER TABLE grape_t_repo_credentials MODIFY COLUMN derived_from "
                  "VARCHAR(36) NOT NULL DEFAULT '')", kind_hint="cleanup")
        db.commit()
    log.info(f"202611170036 derived_from 列宽 {width} → 36")


register_migration(
    version="202611170036",
    name="widen-repo-credential-derived-from",
    kind="backfill",
    fn=_fn_widen_repo_credential_derived_from,
)


# ===== D110 续：平台 bot 的"初始化种子"从配置文件读（任意平台，非仅 gitcode/github）=====


# 注意：函数体内 docstring 提到的 platform-bots.example.json 是盖章时的文件名，
# 现已改回 configs/global-bot-token.example.json（docstring 属 checksum 冻结面，不改）。
# 迁移内容住 owner 域：src/domains/mgmt/migrations/seed_platform_bot_from_file.py
from src.domains.mgmt.migrations.seed_platform_bot_from_file import MIGRATION as _M_SEED_BOT

register_migration(**_M_SEED_BOT)


# ===== D110 收尾：删除已无读者的 bot 账号目录表（先导出，导出失败拒绝 DROP）=====

def _fn_drop_bot_accounts_table() -> None:
    """删除 `grape_t_bot_accounts`（D110：一平台一个 bot，目录表零读取者）。

    可恢复性优先：先把整表导出成 `data/bot_accounts-backup-<ts>.json`，
    写盘失败就**不执行 DROP**（宁可留着死表，也不丢数据）。表不存在时幂等跳过。
    """
    import json as _json
    import os as _os
    import time as _time

    from src.kernel.mysql_compat import get_db_backend
    from src.kernel.session_manager import db, db_lock

    table = "grape_t_bot_accounts"
    data_dir = _os.path.abspath(_os.path.join(_os.path.dirname(__file__), "..", "..", "data"))

    with db_lock:
        c = db.cursor()
        try:
            c.execute(f"SELECT * FROM {table}")
            rows = c.fetchall()
            cols = [d[0] for d in (c.description or [])]
        except Exception:
            log.info(f"202611170038 {table} 不存在，跳过")
            return

        payload = {
            "table": table,
            "exported_at": _time.strftime("%Y-%m-%d %H:%M:%S"),
            "backend": get_db_backend(),
            "columns": cols,
            "rows": [dict(zip(cols, r)) for r in rows],
        }
        _os.makedirs(data_dir, exist_ok=True)
        path = _os.path.join(data_dir, f"bot_accounts-backup-{_time.strftime('%Y%m%d-%H%M%S')}.json")
        try:
            with open(path, "w", encoding="utf-8") as f:
                _json.dump(payload, f, ensure_ascii=False, indent=2)
        except Exception as e:
            log.error(f"202611170038 导出失败，拒绝 DROP {table}: {e}")
            return

        _destructive_ddl(c, f"DROP TABLE {table}", kind_hint="cleanup")
        log.info(f"202611170038 已导出 {len(rows)} 行到 {path} 并删除 {table}")


register_migration(
    version="202611170038",
    name="drop-bot-accounts-table",
    kind="cleanup",  # 含 DROP TABLE，走 cleanup 语义（min_code_version 门槛）
    fn=_fn_drop_bot_accounts_table,
)


# ===== D110 修正：平台行的 bot token 必须是密文（014 从 meta 搬来的是历史明文）=====

# 迁移内容住 owner 域：src/domains/mgmt/migrations/encrypt_platform_bot_tokens.py
from src.domains.mgmt.migrations.encrypt_platform_bot_tokens import MIGRATION as _M_ENCRYPT_BOT

register_migration(**_M_ENCRYPT_BOT)


# ===== 甲方案：可见性真源收敛到平台行 enabled，退役部署级隐藏名单 =====

def _fn_retire_vcs_platforms_hidden() -> None:
    """把 `meta.vcs_platforms_hidden` 名单搬成平台行 `enabled=0`，然后删除该 meta 键。

    背景（2026-11-15 用户拍板方案甲）：名单在 meta 里没有管理面入口、且与行级 enabled 语义重叠；
    统一到 enabled 后，用户面可见性只剩一个开关（有 UI、有导入保护）。
    幂等：名单缺失/为空 → 只删键；平台行不存在的 provider 跳过（纯配置平台没有行可写）。
    """
    import json as _json

    from src.kernel.session_manager import db, db_lock

    with db_lock:
        c = db.cursor()
        try:
            row = c.execute("SELECT value FROM meta WHERE key='vcs_platforms_hidden'").fetchone()
        except Exception:
            log.info("202611170040 meta 表不可用，跳过")
            return
        raw = (row[0] if row else "") or ""
        providers: list[str] = []
        if raw:
            try:
                data = _json.loads(raw)
                if isinstance(data, list):
                    providers = [str(x).strip() for x in data if str(x).strip()]
            except Exception:
                log.warning(f"202611170040 vcs_platforms_hidden 解析失败，按空名单处理: {raw[:80]}")
        moved = []
        for prov in providers:
            try:
                res = c.execute("UPDATE grape_t_repo_platforms SET enabled=0, "
                                "updated_at=datetime('now') WHERE provider=?", (prov,))
                if getattr(res, "rowcount", 0):
                    moved.append(prov)
            except Exception as e:
                log.warning(f"202611170040 写平台行失败 provider={prov}: {e}")
        try:
            c.execute("DELETE FROM meta WHERE key='vcs_platforms_hidden'")
        except Exception as e:
            log.warning(f"202611170040 删除 meta 键失败: {e}")
        db.commit()
    # 初始化默认：DEFAULT_HIDDEN_PLATFORMS 里的平台在**新建/既有未动过**的行上一律给
    # enabled=0（用户裁定：这种"部署默认不展示"的规则写在初始化代码里，不进平台卡——
    # 卡片只描述能力，可见性归 DB 行）。这里对既有行也补一次默认，运维之后可在管理面打开。
    from src.kernel.persistence.repositories_platform_object import DEFAULT_HIDDEN_PLATFORMS

    defaulted = []
    with db_lock:
        c = db.cursor()
        for prov in sorted(DEFAULT_HIDDEN_PLATFORMS):
            try:
                res = c.execute("UPDATE grape_t_repo_platforms SET enabled=0, "
                                "updated_at=datetime('now') WHERE provider=? AND enabled=1", (prov,))
                if getattr(res, "rowcount", 0):
                    defaulted.append(prov)
            except Exception as e:
                log.warning(f"202611170040 应用默认不展示失败 provider={prov}: {e}")
        db.commit()
    log.info(f"202611170040 隐藏名单已退役（写入 enabled=0 的平台：{moved or '无'}；"
             f"初始化默认不展示：{defaulted or '无（已为默认值）'}）")


# ===== D109 自愈迁移：盖章了但表没落地（2026-11-16 生产快照实锤）=====
#
# 现场：存量库首跑走 legacy-stamp 全量轨，202611170030 记账 2ms 却四表皆无（后续
# backfill 迁移失败），台账与 DDL 不一致；PAT 保存钩子写关系表报 1146/「no such table」，
# 表现为「配了 PAT 但平台关系不出现」。本迁移幂等重放同一份 DDL 并校验四表存在，
# 让任何「台账说谎」的库在下次启动自动收敛（不改基线执行器语义）。
def _fn_ensure_platform_object_tables() -> None:
    """确保 D109 四表存在（幂等；台账已盖章但表缺失时自愈）。"""
    _fn_create_platform_object_tables()


register_migration(
    version="202611170041",
    name="ensure-platform-object-tables",
    kind="safe",
    fn=_fn_ensure_platform_object_tables,
)


def _fn_audit_logs_operator_id_widen() -> None:
    """audit_logs.operator_id 列型归一（uuid-pk 线漏改；2026-11-16 8070 E2E 实锤）。

    现象：scene_pin / issue_admin_confirmed 等「用户操作」审计全断——
    log_audit 传账号域 uuid 字符串进 INTEGER 列，MySQL STRICT 报
    1265 Data truncated for column 'operator_id'，被 log_audit 的 except
    吞成 WARNING，audit_logs 一行不留（整条审计静默丢失）。

    202611120003（uuid-pk-and-user-id-cols）的 widen 清单只覆盖 user_id 族列
    （audit_logs.user_id 已 TEXT），operator_id 漏在清单外。

    MySQL：INTEGER → VARCHAR(36) NOT NULL DEFAULT '0'（对齐 user_id 列型；
    存量 int 值 0 语义保真，TEXT 列混存无碍）。SQLite：INTEGER 亲和天然容混型，
    幂等零改动（只占盖章位）——同 202611140004 issue_state.scene_user_id 手法。

    G-01 预防动作：末尾 information_schema 断言列存在且已非 int，不存在 raise
    不盖章。
    """
    from src.kernel.mysql_compat import get_db_backend
    if get_db_backend() != "mysql":
        return
    from src.kernel.session_manager import db
    c = db.cursor()
    c.execute(
        "SELECT DATA_TYPE FROM information_schema.columns "
        "WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME='audit_logs' "
        "AND COLUMN_NAME='operator_id'")
    row = c.fetchone()
    if row and row[0] in ("int", "bigint", "smallint", "tinyint", "mediumint"):
        c.execute(
            "ALTER TABLE audit_logs MODIFY operator_id "
            "VARCHAR(36) NOT NULL DEFAULT '0'")
        db.commit()
        log.info("[migration 202611170042] audit_logs.operator_id "
                 "INTEGER -> VARCHAR(36) widened")
    # 幂等自检（G-01/G-05 口径）：列必须已是非整型，否则不盖章
    c.execute(
        "SELECT DATA_TYPE FROM information_schema.columns "
        "WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME='audit_logs' "
        "AND COLUMN_NAME='operator_id'")
    row = c.fetchone()
    if not row:
        raise RuntimeError(
            "202611170042: audit_logs.operator_id missing after widen — "
            "refuse to stamp")
    if row[0] in ("int", "bigint", "smallint", "tinyint", "mediumint"):
        raise RuntimeError(
            "202611170042: audit_logs.operator_id still integer after widen — "
            "refuse to stamp")


def _fn_runtime_cred_type_platform(conn=None):
    """[202611170045 safe] runtime_credentials 补 cred_type / platform 列。

    沙箱移植起，凭据生成会写这两列，但 schema_ddl 与迁移都没补：
    旧库一生成凭据就 (1054, "Unknown column 'cred_type' in 'field list'")。
    SQLite 走 ADD COLUMN；MySQL 走 _ensure_column 的 INSTANT/INPLACE 在线路径，停机≈0。

    异常协议（执行器契约）：fn 正常返回=盖章，所以这里 **不吞异常**、并传 strict=True —
    MDL 超时 / 非 owner 视图 / 连 COPY 都失败时一律抛出，版本不盖章、下次启动重试。
    吞掉异常会把版本钉死，列永不补齐，与本迁移要修的事故同型。
    _ensure_column 无返回值（None），补列与否靠执行前后 PRAGMA 列集差判定。
    表归属 runtime：在 mgmt 进程里 _owned_skip 会跳过（由 runtime 进程补），跳过不算失败。
    """
    conn = conn if conn is not None else db
    with (db_lock if conn is db else nullcontext()):
        c = conn.cursor()
        # 非 owner 域（三库模式下 runtime_credentials 在 mgmt 库不存在/是视图）：
        # 抛 _NonOwnerObject 让执行器「跳过且不盖章」——否则 fn 正常返回就会在
        # 非 owner 库盖死版本号，而列并没有建成（与 2026-11-06 issue.vcs_exists 事故同型）。
        _present = _table_present(c, "runtime_credentials")
        if not _present or not _is_base_table(c, "runtime_credentials"):
            raise _NonOwnerObject(
                "runtime_credentials 不在本库（非 owner 域），cred_type/platform 由 runtime 库补")
        _want = ("cred_type", "platform")
        before = {r[1] for r in c.execute("PRAGMA table_info(runtime_credentials)").fetchall()}
        for col, ddl in (("cred_type", "VARCHAR(32) NOT NULL DEFAULT 'worker'"),
                         ("platform", "VARCHAR(32) NOT NULL DEFAULT ''")):
            _ensure_column(c, "runtime_credentials", col, ddl, strict=True)
        after = {r[1] for r in c.execute("PRAGMA table_info(runtime_credentials)").fetchall()}
        conn.commit()
        added = [k for k in _want if k not in before and k in after]
        if added:
            log.info("[migration 202611170045] runtime_credentials 补列: "
                     + ", ".join(added))


register_migration(
    version="202611170042",
    name="audit-logs-operator-id-text",
    kind="safe",
    fn=_fn_audit_logs_operator_id_widen,
)

register_migration(
    version="202611170045",
    name="runtime-credentials-cred-type-platform",
    kind="safe",
    fn=_fn_runtime_cred_type_platform,
)

register_migration(
    version="202611170040",
    name="retire-vcs-platforms-hidden",
    kind="backfill",
    fn=_fn_retire_vcs_platforms_hidden,
)
# ===== Issue #2423 M1-b（#2423 移植）：env_extra 存量明文加密 backfill =====
# （arch11 号系顺延——原 202611180001 与 runtime 域 cloud_sandboxes 迁移撞号，backfill 让位改 202611220003）
def _fn_migrate_env_extra_encryption_2423() -> None:
    """代理到 engine/service 的 backfill（迁移系统注册面在 db_setup，实现在域层）。"""
    from src.domains.runtime.engine.service import _fn_migrate_env_extra_encryption
    _fn_migrate_env_extra_encryption()


register_migration(
    version="202611220003",
    name="issue2423-env-extra-encrypt-backfill",
    kind="backfill",
    fn=_fn_migrate_env_extra_encryption_2423,
)


# ===== Issue #2423 M2b（#2423 移植）：格子/台账存量库 safe 迁移注册 =====
def _fn_create_ov_credential_cells_2423() -> None:
    """存量库（迁移台账已盖章）启动补建 grape_t_ov_credential_cells / 台账 / vault 表（CREATE 幂等）。"""
    from src.domains.mgmt.auth.credential_cell import (
        OV_CREDENTIAL_CELLS_DDL, OV_CREDENTIAL_REVOCATIONS_DDL)
    from src.kernel.session_manager import db, db_lock
    with db_lock:
        db.execute(OV_CREDENTIAL_CELLS_DDL)
        db.execute(OV_CREDENTIAL_REVOCATIONS_DDL)
        db.execute(_PLUGIN_CREDENTIAL_VAULT_DDL)
        db.commit()


register_migration(
    version="202611220004",
    name="issue2423-create-ov-credential-cells",
    kind="safe",
    fn=_fn_create_ov_credential_cells_2423,
)


# ===== 桶B B1（plugin 体系）：七张插件表存量库 safe 迁移 =====
def _fn_create_plugin_tables_b1() -> None:
    """存量库启动补建七张插件表（CREATE 幂等；桶B B1 移植）。"""
    from src.kernel.session_manager import db, db_lock
    with db_lock:
        for ddl in (_PLUGIN_TABLE_DDL, _PLUGIN_REGISTRY_DDL, _PLUGIN_SKILL_DDL,
                    _PLUGIN_LIKES_DDL, _PLUGIN_CATEGORY_DDL,
                    _PLUGIN_CERTIFICATION_AUDIT_DDL, _PLUGIN_CONFIG_OVERLAYS_DDL):
            db.execute(ddl)
        db.commit()


# ===== 插件市场分类种子（#1084 B1 移植）：独立 safe 迁移（202611180003 在存量库已盖章）=====
_PLUGIN_DEFAULT_CATEGORIES = [
    ("开发工具", "dev-tools", "🛠️", 1),
    ("部署运维", "deploy-ops", "🚀", 2),
    ("测试验证", "testing", "🧪", 3),
    ("数据处理", "data-processing", "📊", 4),
    ("协作沟通", "collaboration", "💬", 5),
    ("其他", "other", "📌", 99),
]


# ── 2026-11-22 撞号换号（部署环境 8 条 ERROR：同号不同名，被台账永久判已执行）──
# 旧号 → 新号（日期段顺延到 20261122，全仓/台账此前均未占用）：
#   202611170009 chat-conversations-status-index        → 202611220001
#   202611200002 plugin-create-seven-tables-b1          → 202611220002（并折入分类种子）
#   202611200003 issue2423-env-extra-encrypt-backfill    → 202611220003
#   202611200004 issue2423-create-ov-credential-cells    → 202611220004
#   202611210004 session-token-key-column-width          → 202611220005
# 换号后必须在目标库先核对 grape_t_schema_migrations 无这些新号（见
# docs/weekly-11/migration-renumber-20261122.md 的只读核对 SQL）。


def _fn_plugin_category_seed_b1() -> None:
    """插件分类种子（唯一键 + INSERT OR IGNORE，天然幂等可重复执行）。"""
    from src.kernel.session_manager import db, db_lock
    with db_lock:
        c = db.cursor()
        for name, slug, icon, order_num in _PLUGIN_DEFAULT_CATEGORIES:
            c.execute(
                "INSERT OR IGNORE INTO grape_t_plugin_category (name, slug, icon, order_num, is_active) "
                "VALUES (?,?,?,?,1)",
                (name, slug, icon, order_num),
            )
        db.commit()


def _fn_plugin_tables_and_seed_b1() -> None:
    """建 7 张插件表，并补一次分类种子（幂等 INSERT OR IGNORE）。

    为什么合成一条：target 上 202611200005（种子）可能早于建表执行并已盖章，
    建表迁移重排号后若只建表，种子数据仍缺；把种子折进来可在同一次执行里补齐。
    """
    _fn_create_plugin_tables_b1()
    # 种子失败必须抛出：迁移框架只在 fn 正常返回时盖章；吞掉异常会留下
    # 「表建好了、分类没写」且不再重试的半状态（2026-11-22 评审 A2）。
    _fn_plugin_category_seed_b1()


register_migration(
    version="202611220002",
    name="plugin-create-seven-tables-b1",
    kind="safe",
    fn=_fn_plugin_tables_and_seed_b1,
)


register_migration(
    version="202611200005",
    name="plugin-market-category-seed-b1",
    kind="safe",
    fn=_fn_plugin_category_seed_b1,
)


# ===== Issue #1089 挂点④（arch 移植）：agent_runtimes.desired_plugins 列 =====
def _fn_agent_runtimes_desired_plugins_1089() -> None:
    """控制台远程安装的期望插件列（#1089 挂点④，register 响应合并下发）。"""
    from src.kernel.session_manager import db, db_lock
    with db_lock:
        try:
            cols = {r[1] for r in db.execute("PRAGMA table_info(agent_runtimes)").fetchall()}
        except Exception:
            cols = set()
        if "desired_plugins" not in cols:
            db.execute("ALTER TABLE agent_runtimes ADD COLUMN desired_plugins TEXT DEFAULT '[]'")
        db.commit()

# ==============================================================
# ===== Issue #646: engine_uid / credential_uid 稳定键迁移 ============
# 设计依据 Spec v3（Issue #646 评论）：UUIDv5 自然键派生（幂等防漂移）/
# UUIDv4 凭据键（名可变）/ 五阶段在线迁移中的前三阶段（加列→回填→FK）。
# 幂等：全部经 grape_t_schema_migrations 版本表盖章，可重复执行不报错。
# =====================================================================

_ISSUE_646_UID_COLUMNS = (
    # (table, column, ddl)
    ("agent_runtimes", "engine_uid", "VARCHAR(36) DEFAULT NULL"),
    ("agent_runtimes", "credential_uid", "VARCHAR(36) DEFAULT NULL"),
    ("runtime_credentials", "credential_uid", "VARCHAR(36) DEFAULT NULL"),
    ("agent_type_runtime_pool", "engine_uid", "VARCHAR(36) DEFAULT NULL"),
    ("engine_capabilities", "engine_uid", "VARCHAR(36) DEFAULT NULL"),
    ("agent_tasks", "engine_uid", "VARCHAR(36) DEFAULT NULL"),
    ("agent_tasks", "engine_name_snapshot", "TEXT DEFAULT ''"),
    ("agent_tasks", "pool_engine_uids", "TEXT DEFAULT ''"),
    ("task_messages", "engine_uid", "VARCHAR(36) DEFAULT NULL"),
)

_ISSUE_646_UID_UNIQUE_INDEXES = (
    "CREATE UNIQUE INDEX idx_agent_runtimes_engine_uid ON agent_runtimes(engine_uid)",
    "CREATE UNIQUE INDEX idx_runtime_credentials_credential_uid ON runtime_credentials(credential_uid)",
)

_ISSUE_646_UID_PLAIN_INDEXES = (
    "CREATE INDEX idx_agent_tasks_engine_uid ON agent_tasks(engine_uid)",
    "CREATE INDEX idx_agent_type_runtime_pool_engine_uid ON agent_type_runtime_pool(engine_uid)",
    "CREATE INDEX idx_engine_capabilities_engine_uid ON engine_capabilities(engine_uid)",
    "CREATE INDEX idx_task_messages_engine_uid ON task_messages(engine_uid)",
)

# 回填分批大小（大表游标推进，分钟级后台执行；标准环境行数少、秒级完成）
_ISSUE_646_BACKFILL_BATCH = 1000

# 迁移报告（孤儿引用清单）：进程内留档 + log.warning 逐条输出，供升级后人工核对。
_ISSUE_646_UID_MIGRATION_REPORT: dict = {}


def get_issue_646_uid_migration_report() -> dict:
    """Issue #646 回填迁移报告（孤儿引用清单等；标准环境孤儿数为 0）。"""
    return dict(_ISSUE_646_UID_MIGRATION_REPORT)


def _issue_646_create_index(c, ddl: str) -> None:
    """幂等建索引：SQLite 加 IF NOT EXISTS；MySQL 重复索引(1061)吞掉。"""
    if _mysql_backend():
        try:
            c.execute(ddl)
        except Exception as e:
            if "1061" not in str(e) and "already exists" not in str(e).lower():
                raise
    else:
        import re as _re
        sqlite_ddl = _re.sub(r"^CREATE(\s+UNIQUE)?\s+INDEX\s+",
                             lambda m: f"{m.group(0)}IF NOT EXISTS ", ddl, count=1)
        c.execute(sqlite_ddl)


def _issue_646_column_ddl(ddl: str) -> str:
    """QA 复审 R1：TEXT 列加列 DDL 的跨后端安全形态。

    MySQL 8.0 不允许 BLOB/TEXT 列默认字面量（CREATE TABLE 路径有 mysql_compat
    翻译层兜底，ALTER ADD COLUMN 路径不经翻译层直接下发会 1064/1845 失败），
    MySQL 侧一律去默认值（TEXT NULL）；行级代码全部显式写入这两列，不依赖
    DB 默认值。SQLite 保持 DEFAULT ''。
    """
    head = ddl.strip().split()[0].upper() if ddl.strip() else ""
    if _mysql_backend() and head in ("TEXT", "MEDIUMTEXT", "LONGTEXT"):
        return f"{head} NULL"
    return ddl


def _foreign_owner_will_run(table: str) -> bool:
    """域外表的落库核验能否交给属主域进程（review M2）。

    只有本进程**确实**注入了迁移域（三库模式 GRAPE_DOMAIN_LIB=1 + owner）时才返回
    True，并打 WARNING 明示「本进程不核验」；单库部署里 `current_domain()` 会按角色
    回落（src/kernel/table_ownership.py 自述「单库部署下会误判成非 owner」），
    此时归属只是配置推断、不能替代落库实测 —— 返回 False，让预检按「未建成」上报，
    保持改造前「不盖章 + 可见 ERROR」的行为。
    """
    try:
        from src.kernel import table_ownership as _own
        owner = _own.foreign_owner(table)
        if not owner:
            return False
        dom = current_migration_domain()
        if not dom:
            log.warning(f"[PRECHECK] `{table}` 归 {owner} 域，但本进程未注入迁移域"
                        f"（单库/角色回落）：归属不能替代实测，按未建成处理")
            return False
        log.warning(f"[PRECHECK_SKIP] `{table}` 归 {owner} 域，由该域进程执行并核验；"
                    f"本进程（{dom}）不核验")
        return True
    except Exception as exc:  # noqa: BLE001
        log.warning(f"[PRECHECK] ownership probe failed on `{table}`: {exc}")
        return False


def _issue_646_columns_missing(c) -> list:
    """QA 复审 R1：加列迁移落库核验——返回仍未建成的 (table, column) 清单。

    三库模式：别的域拥有的表（agent_runtimes 等在 runtime 库）本进程根本不建——
    `_ensure_column` 已被归属守卫跳过。此时预检交给 `_foreign_owner_will_run()` 判定：
    只有确认本进程已注入迁移域（三库模式）才跳过核验并明示 WARNING；单库/角色回落
    形态下归属只是推断，仍按「未建成」上报，不盖章、留可见 ERROR（review M2）。
    """
    missing = []
    for tbl, col, _ddl in _ISSUE_646_UID_COLUMNS:
        if _foreign_owner_will_run(tbl):
            continue
        cols = [r[1] for r in c.execute(f"PRAGMA table_info({tbl})").fetchall()]
        if col not in cols:
            missing.append(f"{tbl}.{col}")
    return missing


def _migration_646_add_uid_columns(conn=None) -> None:
    """[202611080011 safe] 阶段1 加列：uid 列（可空，NULL 多行合法）+ 索引。

    SQLite ADD COLUMN 不重建表；MySQL 8.0 走 _ensure_column 的 INSTANT/INPLACE
    在线路径；停机 ≈ 0。全新库 DDL 已含列，本迁移对旧库补列（幂等）。

    QA 复审 R1（MySQL 阻塞缺陷）：
    - agent_tasks.engine_name_snapshot / pool_engine_uids 两列 DDL 按后端分支：
      MySQL 侧 TEXT NULL（无默认值，ALTER 路径 TEXT DEFAULT '' 不合法），
      SQLite 侧保持 TEXT DEFAULT ''；
    - 加列未建成 → 抛错不盖章（对齐 _LEGACY_BLOCKED_DDL 语义：DDL 被拦截
      不打帽，下次启动幂等重试），避免「202611080011 已盖章 + 回填 1054
      每次启动重跑再失败」的永久中间态。
    """
    conn = conn if conn is not None else db
    with (db_lock if conn is db else nullcontext()):
        c = conn.cursor()
        for tbl, col, ddl in _ISSUE_646_UID_COLUMNS:
            _ensure_column(c, tbl, col, _issue_646_column_ddl(ddl))
        for ddl in _ISSUE_646_UID_UNIQUE_INDEXES + _ISSUE_646_UID_PLAIN_INDEXES:
            try:
                _issue_646_create_index(c, ddl)
            except Exception as e:
                log.warning(f"Issue #646 索引创建失败（幂等重试）: {ddl} -> {e}")
        missing = _issue_646_columns_missing(c)
        if missing:
            log.error(f"Issue #646 加列未完成（不盖章，下次启动重试）: {', '.join(missing)}")
            raise RuntimeError(
                f"issue-646-add-uid-columns 未完成（列未建成不盖章，下次启动重试）: "
                f"{', '.join(missing)}")
        conn.commit()


def _migration_646_backfill_uids(conn=None) -> dict:
    """[202611080005 backfill] 阶段2 回填：uid 生成与存量引用切换（分批/幂等/断点续跑）。

    - runtime_credentials.credential_uid ← UUIDv4（一次性，终身不变）
    - agent_runtimes.engine_uid ← UUIDv5(credential_uid|name|scope|owner_id|host_name)
      自然键重复行（历史去重遗留）：仅 id 最小行得 uid，其余置 NULL 并入报告（改绑/清理由
      save_agent_runtimes 去重路径处理，不静默丢弃）
    - agent_type_runtime_pool / engine_capabilities / agent_tasks.engine_uid ←
      按 runtime_id 映射；孤儿引用置 NULL 并入报告
    - agent_tasks.engine_name_snapshot ← 引擎展示名快照；pool_engine_ids(JSON) →
      pool_engine_uids(JSON) 镜像（孤儿项置 null）
    - task_messages.engine_uid ← 经 agent_tasks.engine_uid 快照带入（分批）
    - 大表（agent_tasks/task_messages）按 id 游标分批（每批 1000），失败下次续跑。
    """
    from src.kernel.engine_uid import derive_engine_uid, new_credential_uid
    conn = conn if conn is not None else db
    # 三库模式：这些表归 runtime 域，本进程（mgmt）库里没有它们。DDL 已被归属守卫
    # 跳过，回填同样由 runtime 域进程负责——这里不抛错、直接收敛，避免「不盖章」。
    if _owned_skip("runtime_credentials", "backfill"):
        return {}
    report = {"duplicated_engine_rows": [], "orphan_pool_rows": [], "orphan_capability_rows": [],
              "orphan_task_runtime": [], "orphan_task_pool_ids": [], "orphan_task_messages": 0,
              "credentials_backfilled": 0, "engines_backfilled": 0, "tasks_backfilled": 0,
              "task_messages_backfilled": 0}

    def _warn(msg):
        log.warning(f"[Issue #646 迁移报告] {msg}")

    c = conn.cursor()
    # ── 1. runtime_credentials.credential_uid（UUIDv4，行数百级） ──
    rows = c.execute("SELECT id FROM runtime_credentials WHERE credential_uid IS NULL OR credential_uid=''").fetchall()
    for (cid,) in rows:
        c.execute("UPDATE runtime_credentials SET credential_uid=? WHERE id=?", (new_credential_uid(), cid))
        report["credentials_backfilled"] += 1
    cred_uid_by_id = {r[0]: (r[1] or "") for r in
                      c.execute("SELECT id, credential_uid FROM runtime_credentials").fetchall()}

    # ── 2. agent_runtimes.engine_uid（UUIDv5 自然键派生；含已回填行跳过） ──
    engine_rows = c.execute(
        "SELECT id, credential_id, name, scope, owner_id, host_name, engine_uid FROM agent_runtimes "
        "ORDER BY id").fetchall()
    uid_by_id: dict = {}
    natural_key_seen: dict = {}
    for rid, cred_id, name, scope, owner_id, host_name, existing_uid in engine_rows:
        if existing_uid:
            # 已回填（幂等重跑）：保持既有 uid 不变（防漂移），只补映射
            uid_by_id[rid] = existing_uid
            natural_key_seen.setdefault(
                (cred_uid_by_id.get(cred_id, "") if cred_id else "",
                 (name or "").strip().lower(), (scope or "").strip().lower(),
                 (owner_id or "").strip().lower(), (host_name or "").strip().lower()), rid)
            continue
        cred_uid = cred_uid_by_id.get(cred_id, "") if cred_id else ""
        key = (cred_uid, (name or "").strip().lower(), (scope or "").strip().lower(),
               (owner_id or "").strip().lower(), (host_name or "").strip().lower())
        if key in natural_key_seen:
            # 自然键重复（历史去重遗留）：id 最小行保留 uid，其余置 NULL 入报告
            c.execute("UPDATE agent_runtimes SET engine_uid=NULL WHERE id=?", (rid,))
            report["duplicated_engine_rows"].append(
                {"id": rid, "kept_id": natural_key_seen[key], "name": name})
            _warn(f"agent_runtimes 自然键重复行 id={rid}（保留 id={natural_key_seen[key]}）: {name}")
            continue
        uid = derive_engine_uid(cred_uid, name or "", scope or "", owner_id or "", host_name or "")
        c.execute("UPDATE agent_runtimes SET engine_uid=? WHERE id=?", (uid, rid))
        natural_key_seen[key] = rid
        uid_by_id[rid] = uid
        report["engines_backfilled"] += 1

    # QA 复审 R3：快照名与新写路径 engine_ref_snapshot 统一 display_name 优先
    name_by_id = {r[0]: (r[1] or r[2] or "") for r in
                  c.execute("SELECT id, COALESCE(display_name, name), name FROM agent_runtimes").fetchall()}

    # ── 3. agent_type_runtime_pool / engine_capabilities（行数百级） ──
    for tbl, orphan_key in (("agent_type_runtime_pool", "orphan_pool_rows"),
                            ("engine_capabilities", "orphan_capability_rows")):
        prow = c.execute(f"SELECT id, runtime_id FROM {tbl} WHERE engine_uid IS NULL OR engine_uid=''").fetchall()
        for pid, rid in prow:
            uid = uid_by_id.get(rid)
            if uid:
                c.execute(f"UPDATE {tbl} SET engine_uid=? WHERE id=?", (uid, pid))
            else:
                c.execute(f"UPDATE {tbl} SET engine_uid=NULL WHERE id=?", (pid,))
                report[orphan_key].append({"id": pid, "runtime_id": rid})
                _warn(f"{tbl} 孤儿引用 id={pid} runtime_id={rid}（置 NULL，请改绑/清理）")

    # ── 4. agent_tasks（分批：id 游标推进；id 为 TEXT 主键，字符串游标） ──
    import json as _json
    _cursor = ""
    while True:
        batch = c.execute(
            "SELECT id, runtime_id, pool_engine_ids FROM agent_tasks "
            "WHERE (engine_uid IS NULL OR engine_uid='') AND id > ? "
            "ORDER BY id LIMIT ?",
            (_cursor, _ISSUE_646_BACKFILL_BATCH)).fetchall()
        if not batch:
            break
        for tid, rid, pool_json in batch:
            uid = uid_by_id.get(rid)
            snapshot = name_by_id.get(rid, "")
            if not uid:
                report["orphan_task_runtime"].append({"task_id": tid, "runtime_id": rid})
                _warn(f"agent_tasks 孤儿 runtime 引用 task={tid} runtime_id={rid}")
            pool_uid_json = ""
            if pool_json:
                try:
                    ids = _json.loads(pool_json)
                    int_ids = [i for i in ids if isinstance(i, int)]
                    uid_list = [uid_by_id.get(i) for i in int_ids]
                    orphan_ids = [i for i, u in zip(int_ids, uid_list) if u is None]
                    if orphan_ids:
                        report["orphan_task_pool_ids"].append(
                            {"task_id": tid, "orphan_engine_ids": orphan_ids})
                        _warn(f"agent_tasks.pool_engine_ids 含孤儿 id: task={tid} "
                              f"orphan_ids={orphan_ids}（镜像置 null，不静默丢弃）")
                    pool_uid_json = _json.dumps([u for u in uid_list if u])
                except Exception:
                    pool_uid_json = ""
            c.execute(
                "UPDATE agent_tasks SET engine_uid=?, engine_name_snapshot=?, pool_engine_uids=? WHERE id=?",
                (uid, snapshot, pool_uid_json, tid))
            report["tasks_backfilled"] += 1
        _cursor = batch[-1][0]
        if len(batch) < _ISSUE_646_BACKFILL_BATCH:
            break

    # ── 5. task_messages（分批：LEFT JOIN agent_tasks 逐批带入快照，避免全量加载；id 自增整型游标） ──
    _cursor = 0
    while True:
        batch = c.execute(
            "SELECT tm.id, tm.task_id, at.engine_uid FROM task_messages tm "
            "LEFT JOIN agent_tasks at ON tm.task_id = at.id "
            "WHERE (tm.engine_uid IS NULL OR tm.engine_uid='') AND tm.id > ? "
            "ORDER BY tm.id LIMIT ?",
            (_cursor, _ISSUE_646_BACKFILL_BATCH)).fetchall()
        if not batch:
            break
        for mid, tid, uid in batch:
            if uid:
                c.execute("UPDATE task_messages SET engine_uid=? WHERE id=?", (uid, mid))
                report["task_messages_backfilled"] += 1
            else:
                report["orphan_task_messages"] += 1
        _cursor = batch[-1][0]
        if len(batch) < _ISSUE_646_BACKFILL_BATCH:
            break

    conn.commit()
    # Issue #868 E2E 复核问题2（机制对齐）：#646 迁移报告同样落盘 JSON，可事后复核
    report["report_file"] = _write_uid_migration_report_file(conn, "646", report)
    _ISSUE_646_UID_MIGRATION_REPORT.clear()
    _ISSUE_646_UID_MIGRATION_REPORT.update(report)
    orphans = (len(report["duplicated_engine_rows"]) + len(report["orphan_pool_rows"])
               + len(report["orphan_capability_rows"]) + len(report["orphan_task_runtime"])
               + len(report["orphan_task_pool_ids"]) + report["orphan_task_messages"])
    log.info(f"Issue #646 uid 回填完成：credential={report['credentials_backfilled']} "
             f"engine={report['engines_backfilled']} task={report['tasks_backfilled']} "
             f"message={report['task_messages_backfilled']} 孤儿引用={orphans}（标准环境为 0）")
    return report


# Issue #646 复审裁定：三条 FK 一律 ON DELETE RESTRICT（删除走显式清理链 + 对账报告，
# 见 user/db._rebind_runtime_references 及其公开入口），RESTRICT 作兜底防漏——
# 漏清引用时 DELETE 报错可见，不静默级联蒸发。先 DROP 同名约束再 ADD，
# 兼容早期 CASCADE 形态与重复执行（1826/121 duplicate 幂等跳过）。
_ISSUE_646_FK_DROPS = (
    "ALTER TABLE agent_type_runtime_pool DROP FOREIGN KEY fk_pool_engine_uid",
    "ALTER TABLE engine_capabilities DROP FOREIGN KEY fk_cap_engine_uid",
    "ALTER TABLE agent_runtimes DROP FOREIGN KEY fk_runtime_credential_uid",
)
_ISSUE_646_FK_DDLS = (
    "ALTER TABLE agent_type_runtime_pool ADD CONSTRAINT fk_pool_engine_uid "
    "FOREIGN KEY (engine_uid) REFERENCES agent_runtimes(engine_uid) ON DELETE RESTRICT",
    "ALTER TABLE engine_capabilities ADD CONSTRAINT fk_cap_engine_uid "
    "FOREIGN KEY (engine_uid) REFERENCES agent_runtimes(engine_uid) ON DELETE RESTRICT",
    "ALTER TABLE agent_runtimes ADD CONSTRAINT fk_runtime_credential_uid "
    "FOREIGN KEY (credential_uid) REFERENCES runtime_credentials(credential_uid) ON DELETE RESTRICT",
)


def _migration_646_mysql_fks(conn=None) -> None:
    """
    [202611080006 safe] 阶段3 MySQL 真实 FK（SQLite 跳过，悬挂防护由迁移回填 + 显式清理链保证）。

    Issue #646 复审裁定（CASCADE→RESTRICT）：静默级联蒸发比可见悬挂更危险——
    - agent_type_runtime_pool.engine_uid → agent_runtimes.engine_uid ON DELETE RESTRICT
    - engine_capabilities.engine_uid → agent_runtimes.engine_uid ON DELETE RESTRICT
    - agent_runtimes.credential_uid → runtime_credentials(credential_uid) ON DELETE RESTRICT
    删除引擎/凭据一律走显式清理链（user/db._rebind_runtime_references 及公开入口
    rebind_runtime_references / explicit_cleanup_runtime_references：先清池/能力/会话/
    绑定/场景引用并出对账报告，再删行），RESTRICT 作兜底防漏（漏删时报错可见，
    不静默蒸发）。幂等：先 DROP 同名约束（兼容早期 CASCADE 形态/重复执行）再 ADD；
    孤儿行 uid=NULL 不受 FK 约束（预期）。
    """
    if _owned_skip("agent_runtimes", "fk"):
        return  # 三库模式：FK 属于 runtime 域，由 runtime 域进程建立

    if not _mysql_backend():
        log.info("Issue #646: SQLite 后端跳过 MySQL FK（回填+显式清理链已保证无悬挂）")
        return
    conn = conn if conn is not None else db
    c = conn.cursor()
    for drop in _ISSUE_646_FK_DROPS:
        try:
            c.execute(drop)
        except Exception:
            pass  # 约束不存在（首次执行）→ 直接 ADD
    for ddl in _ISSUE_646_FK_DDLS:
        try:
            c.execute(ddl)
        except Exception as e:
            s = str(e).lower()
            if "1826" in s or "121" in s or "already exists" in s or "duplicate" in s:
                continue
            # 引擎行含 NULL uid 的孤儿不影响建 FK；真正的数据冲突会在此暴露
            log.warning(f"Issue #646 FK 建立失败（幂等重试）: {ddl} -> {e}")
    conn.commit()


register_migration(
    "202611080011", "issue-646-add-uid-columns", "safe",
    lambda: _migration_646_add_uid_columns(),
    domains=("runtime",))
register_migration(
    "202611080012", "issue-646-backfill-uids", "backfill",
    lambda: _migration_646_backfill_uids(),
    domains=("runtime",))
# 2026-11-18 三库核对：grape_t_cloud_sandboxes 归 runtime，但历史上前置在 mgmt 库建过
# （mgmt 那份 0 行、runtime 反而缺）。迁移内容在 owner 域，只为把表补到正确库。
from src.domains.runtime.migrations.ensure_cloud_sandboxes_table import MIGRATION as _M_CLOUD_SB
register_migration(**_M_CLOUD_SB)
# 2026-11-18 Issue #113/seq 9946：agent_runtimes 缺 runtime_type / runtime_info 两列，
# claim 查询 1054 → 引擎不 spawn。迁移只在 runtime 域执行（列在该域的表上）。
from src.domains.runtime.migrations.ensure_runtime_type_info_columns import MIGRATION as _M_RT_COLS
register_migration(**_M_RT_COLS)
register_migration(
    "202611080013", "issue-646-mysql-fks", "safe",
    lambda: _migration_646_mysql_fks(),
    domains=("runtime",))


# =====================================================================
# ===== Issue #868 (T10): 会话表 engine_uid 双读迁移 ===================
# 设计依据 Issue #868 Spec（#646 四阶段 uid 改造的会话表落地）：
# devops_agent_session / chat_agent_session 增加 engine_uid（VARCHAR(36)，
# 语义复用 #646：UUIDv5 自然键派生列，uid 优先、runtime_id 兜底双读）。
# 版本号注意：arch 线已用 202611030001~4（#658 role_tags / #646 三条），
# 本 Issue 取 202611030005/6 全局唯一号段，防两线漂移（重编号经验见 1bc349dfa）。
# 幂等：经 grape_t_schema_migrations 盖章，可重复执行不报错。
# =====================================================================

_ISSUE_868_SESSION_UID_COLUMNS = (
    # (table, column, ddl)
    ("devops_agent_session", "engine_uid", "VARCHAR(36) DEFAULT NULL"),
    ("chat_agent_session", "engine_uid", "VARCHAR(36) DEFAULT NULL"),
)

_ISSUE_868_SESSION_UID_INDEXES = (
    "CREATE INDEX idx_devops_agent_session_engine_uid ON devops_agent_session(engine_uid)",
    "CREATE INDEX idx_chat_agent_session_engine_uid ON chat_agent_session(engine_uid)",
)

# 迁移报告（会话孤儿引用清单）：进程内留档 + log.warning 逐条输出，供升级后人工核对。
_ISSUE_868_SESSION_UID_MIGRATION_REPORT: dict = {}


def _uid_migration_report_path(conn, issue_tag: str) -> str:
    """迁移报告落盘路径：与 SQLite 库文件同目录 <db>.issue-<tag>-migration-report.json。

    E2E 复核问题2：孤儿对账报告原先仅 log.warning + 进程内 dict，日志滚动即丢失。
    路径解析（#646 / #868 迁移报告共用，机制对齐）：
    - SQLite 文件库 → 库文件旁 `<db文件名>.issue-<tag>-migration-report.json`；
    - 模块级默认连接（含 MySQL 后端，PRAGMA 不可用）→ session_manager._db_path
      所在目录下 `issue-<tag>-migration-report.json`；
    - 内存库 / 外部注入的临时连接（测试）→ 返回空串跳过落盘（不写仓库目录）。
    """
    db_file = ""
    try:
        main = [r for r in conn.execute("PRAGMA database_list").fetchall() if r[1] == "main"]
        db_file = (main[0][2] or "") if main else ""
    except Exception:
        db_file = ""
    if db_file and ":memory:" not in db_file:
        return f"{db_file}.issue-{issue_tag}-migration-report.json"
    if conn is db:
        try:
            from src.kernel.session_manager import _db_path
            return os.path.join(os.path.dirname(os.path.abspath(_db_path)),
                                f"issue-{issue_tag}-migration-report.json")
        except Exception:
            return ""
    return ""


def _write_uid_migration_report_file(conn, issue_tag: str, report: dict) -> str:
    """迁移报告落盘（原子写：tmp + os.replace），失败仅告警不阻断迁移（幂等可重跑）。"""
    path = _uid_migration_report_path(conn, issue_tag)
    if not path:
        return ""
    try:
        payload = {"generated_at": _now_iso(), "issue": issue_tag, "report": report}
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
        log.info(f"[Issue #{issue_tag}] 迁移报告已落盘: {path}")
        return path
    except Exception as e:
        log.warning(f"[Issue #{issue_tag}] 迁移报告落盘失败（不阻断迁移）: {e}")
        return ""


def get_issue_868_session_uid_migration_report() -> dict:
    """Issue #868 会话表 uid 回填迁移报告（孤儿引用清单；标准环境孤儿数为 0）。"""
    return dict(_ISSUE_868_SESSION_UID_MIGRATION_REPORT)


def _issue_868_session_columns_missing(c) -> list:
    """加列迁移落库核验——返回仍未建成的 (table, column) 清单（模式同 #646 R1）。

    外来表同 #646：归属守卫已跳过，缺列由属主域进程负责，不计入缺失。
    """
    missing = []
    for tbl, col, _ddl in _ISSUE_868_SESSION_UID_COLUMNS:
        if _foreign_owner_will_run(tbl):
            continue
        try:
            cols = [r[1] for r in c.execute(f"PRAGMA table_info({tbl})").fetchall()]
        except Exception:
            cols = []
        if col not in cols:
            missing.append(f"{tbl}.{col}")
    return missing


def _migration_868_add_session_uid_columns(conn=None) -> None:
    """[202611030005 safe] Issue #868 T10 阶段1 加列：两张会话表 engine_uid + 索引。

    SQLite ADD COLUMN 不重建表；MySQL 8.0 走 _ensure_column 的 INSTANT/INPLACE
    在线路径，停机 ≈ 0。全新库 DDL 已含列（db_setup / session_manager 兼容列），
    本迁移对旧库补列（幂等）。列未建成 → 抛错不盖章（对齐 #646 R1 语义，
    避免「已盖章 + 回填每次重跑失败」的永久中间态）。
    """
    conn = conn if conn is not None else db
    with (db_lock if conn is db else nullcontext()):
        c = conn.cursor()
        for tbl, col, ddl in _ISSUE_868_SESSION_UID_COLUMNS:
            _ensure_column(c, tbl, col, _issue_646_column_ddl(ddl))
        for ddl in _ISSUE_868_SESSION_UID_INDEXES:
            try:
                _issue_646_create_index(c, ddl)
            except Exception as e:
                log.warning(f"Issue #868 索引创建失败（幂等重试）: {ddl} -> {e}")
        missing = _issue_868_session_columns_missing(c)
        if missing:
            log.error(f"Issue #868 加列未完成（不盖章，下次启动重试）: {', '.join(missing)}")
            raise RuntimeError(
                f"issue-868-session-uid-columns 未完成（列未建成不盖章，下次启动重试）: "
                f"{', '.join(missing)}")
        conn.commit()


def _migration_868_backfill_session_uids(conn=None) -> dict:
    """[202611030006 backfill] Issue #868 T10 阶段2 回填：会话 uid + 对账报告。

    - devops_agent_session / chat_agent_session.engine_uid ← 按 runtime_id 映射
      agent_runtimes.engine_uid（已回填行跳过，幂等不漂移）；
    - 孤儿/未绑定 runtime_id（引擎行不存在或 runtime_id=0）→ uid 置 NULL 并入
      报告（不静默丢弃），供对账/改绑；
    - 会话表行数量级小（千级），整表遍历即可，无需分批。
    """
    conn = conn if conn is not None else db
    # 三库模式：会话表归 runtime 域，回填由 runtime 域进程做（见 #646 同款说明）。
    if _owned_skip("devops_agent_session", "backfill"):
        return {}
    report = {"devops_sessions_backfilled": 0, "chat_sessions_backfilled": 0,
              "orphan_devops_sessions": [], "orphan_chat_sessions": [],
              "update_unmatched_sessions": []}

    def _warn(msg):
        log.warning(f"[Issue #868 迁移报告] {msg}")

    c = conn.cursor()
    # QA Required：005 加列失败不盖章时，006 若在同轮吞错照常返回会被盖章，
    # 此后 005 补跑成功也不再回填（uid 永久 NULL、孤儿报告失真）。
    # 故 006 开头预检两表 engine_uid 列（复用 005 落库核验）：列缺失即 raise
    # 不盖章，下次启动重试，保证「回填可重跑」验收。
    missing = _issue_868_session_columns_missing(c)
    if missing:
        log.error(f"Issue #868 回填预检失败（会话表缺列，不盖章，下次启动重试）: {', '.join(missing)}")
        raise RuntimeError(
            f"issue-868-backfill-session-uids 预检未通过（列未建成不盖章，下次启动重试）: "
            f"{', '.join(missing)}")
    try:
        uid_by_id = {r[0]: (r[1] or "") for r in
                     c.execute("SELECT id, engine_uid FROM agent_runtimes").fetchall()}
    except Exception as e:
        # agent_runtimes 不可读（含 engine_uid 列缺失）同样不盖章——回填数据源缺失
        # 时盖章会造成永久 NULL 中间态，与 005 语义对齐（下次启动重试）。
        log.error(f"Issue #868 回填预检失败（agent_runtimes 不可读，不盖章，下次启动重试）: {e}")
        raise RuntimeError(f"issue-868-backfill-session-uids 预检未通过（agent_runtimes 不可读）: {e}") from e
    for tbl, ok_key, orphan_key in (
            ("devops_agent_session", "devops_sessions_backfilled", "orphan_devops_sessions"),
            ("chat_agent_session", "chat_sessions_backfilled", "orphan_chat_sessions")):
        try:
            rows = c.execute(
                f"SELECT id, runtime_id FROM {tbl} WHERE (engine_uid IS NULL OR engine_uid='')").fetchall()
        except Exception as e:
            log.warning(f"Issue #868 回填跳过 {tbl}: {e}")
            continue
        for sid, rid in rows:
            uid = uid_by_id.get(rid) if rid else None
            if uid:
                # E2E 复核问题1：UPDATE 不校验 rowcount 会在未命中时虚报 backfilled
                # （如 SQLite TEXT 主键 NULL 行 WHERE id=NULL 不命中）——rowcount==1
                # 才计数，未命中入异常报告（不静默丢弃，报告计数与落库效果一致）。
                cur = c.execute(f"UPDATE {tbl} SET engine_uid=? WHERE id=?", (uid, sid))
                if getattr(cur, "rowcount", 0) == 1:
                    report[ok_key] += 1
                else:
                    report["update_unmatched_sessions"].append(
                        {"table": tbl, "session_id": sid, "runtime_id": rid, "uid": uid})
                    _warn(f"{tbl} 回填 UPDATE 未命中（rowcount!=1）session={sid} "
                          f"runtime_id={rid}（不计数 backfilled，入异常报告）")
            else:
                cur = c.execute(f"UPDATE {tbl} SET engine_uid=NULL WHERE id=?", (sid,))
                if getattr(cur, "rowcount", 0) != 1:
                    report["update_unmatched_sessions"].append(
                        {"table": tbl, "session_id": sid, "runtime_id": rid, "uid": None})
                    _warn(f"{tbl} 孤儿置 NULL UPDATE 未命中（rowcount!=1）session={sid} "
                          f"runtime_id={rid}（入异常报告）")
                    continue
                report[orphan_key].append({"session_id": sid, "runtime_id": rid})
                _warn(f"{tbl} 孤儿/未绑定 runtime 引用 session={sid} runtime_id={rid}（uid 置 NULL，请改绑/清理）")
    conn.commit()
    # E2E 复核问题2：孤儿/异常对账报告落盘 JSON（与 #646 迁移报告机制对齐），可事后复核
    report["report_file"] = _write_uid_migration_report_file(conn, "868", report)
    _ISSUE_868_SESSION_UID_MIGRATION_REPORT.clear()
    _ISSUE_868_SESSION_UID_MIGRATION_REPORT.update(report)
    orphans = len(report["orphan_devops_sessions"]) + len(report["orphan_chat_sessions"])
    log.info(f"Issue #868 会话 uid 回填完成：devops={report['devops_sessions_backfilled']} "
             f"chat={report['chat_sessions_backfilled']} 孤儿引用={orphans} "
             f"未命中UPDATE={len(report['update_unmatched_sessions'])}（标准环境为 0）")
    return report


register_migration(
    "202611030005", "issue-868-session-uid-columns", "safe",
    lambda: _migration_868_add_session_uid_columns(),
    domains=("runtime",))
register_migration(
    "202611030006", "issue-868-backfill-session-uids", "backfill",
    lambda: _migration_868_backfill_session_uids(),
    domains=("runtime",))


# =====================================================================
# ===== Issue #1011: 派生源修正收口 + claim-by-uid v2 前置迁移 ========
# 设计依据 Issue #1011 正文 In Scope 0/2：
# - 202611080007（safe）：agent_runtimes 加云引擎身份锚快照列（注册路径持久化，
#   使派生源可离线重算）；autopilot_tasks 加姊妹字段 engine_runtime_uids。
# - 202611080008（backfill）：存量 engine_uid 按当前公式（身份不变量）全量重派生，
#   uid 变化行经 rebase_engine_uid_references 映射式改绑（旧行 uid 从行内读取），
#   对账报告落盘（tag=1011）。普通引擎重算=原值（幂等零变更）。
# 幂等：经 grape_t_schema_migrations 盖章，可重复执行不报错。
# =====================================================================

_ISSUE_1011_ANCHOR_COLUMNS = (
    ("agent_runtimes", "engine_type", "TEXT DEFAULT ''"),
    ("agent_runtimes", "provider_id", "TEXT DEFAULT ''"),
    ("agent_runtimes", "template_id", "TEXT DEFAULT ''"),
    ("agent_runtimes", "provider_uid", "VARCHAR(36) DEFAULT ''"),
    ("agent_runtimes", "template_uid", "VARCHAR(36) DEFAULT ''"),
)


def _migration_1011_add_engine_anchor_columns(conn=None) -> None:
    """[202611080007 safe] Issue #1011：锚快照列 + autopilot 姊妹字段。

    - agent_runtimes 五列锚快照（engine_type/provider_id/template_id/
      provider_uid/template_uid）——注册路径（runtime.py register）持久化
      i5 透传锚，使派生源修正后存量行可离线重算（第 002 号迁移消费）。
    姊妹字段 autopilot_tasks.engine_runtime_uids 归 mgmt 域，已拆到 202611170046
    （本迁移只标 runtime；在本迁移里建 mgmt 表列会被归属守卫跳过，导致该列永不落地）。
    """
    conn = conn if conn is not None else db
    c = conn.cursor()
    for tbl, col, ddl in _ISSUE_1011_ANCHOR_COLUMNS:
        _ensure_column(c, tbl, col, ddl)
    conn.commit()


def _migration_1011_backfill_rederive_engine_uids(conn=None) -> dict:
    """[202611080008 backfill] Issue #1011：存量 engine_uid 重派生 + 引用改绑 + 报告。

    - 按当前 derive_engine_uid 公式对 agent_runtimes 全量重算：
      uid 锚成对在场 → uid 公式；id 锚在场 → id 公式；无锚 → legacy 公式
      （输入串与 #646 回填一致 → 重算=原值，普通引擎零变更）。
    - uid 变化行：rebase_engine_uid_references（映射式改绑：池/能力/任务/消息
      uid 引用轨道整体跟随，旧行 uid 从行内读取，不按 runtime_id 重派 JOIN）。
    - 对账报告落盘（tag=1011，幂等：文件每次执行覆盖重写为最新对账结果）。
    """
    from src.kernel.engine_uid import derive_engine_uid
    from src.domains.mgmt.user.db import rebase_engine_uid_references
    # agent_runtimes / runtime_credentials 归 runtime 域：三库模式下预检与改绑都必须
    # 在该域连接上做（mgmt 连接里没有这两张表 → 预检恒判「锚列未建成」→ 永不盖章）
    conn = conn if conn is not None else _migration_conn("runtime")
    report = {"engines_total": 0, "engines_rederived": 0, "engines_unchanged": 0,
              "rebased": [], "rebase_reports": []}

    c = conn.cursor()
    # 预检：锚快照列已建成（001 不盖章时 002 不盖章，下次启动重试）
    # 三库模式：agent_runtimes 归 runtime 域，本进程（mgmt）看不到这些列，
    # 由 runtime 域进程在建列后自己跑回填；此处不预检、不阻断。
    if _foreign_owner_will_run("agent_runtimes"):
        return report
    cols = [r[1] for r in c.execute("PRAGMA table_info(agent_runtimes)").fetchall()]
    missing = [col for (_t, col, _d) in _ISSUE_1011_ANCHOR_COLUMNS if col not in cols]
    if missing:
        raise RuntimeError(
            f"issue-1011-backfill-rederive-engine-uids 预检未通过（锚快照列未建成"
            f"不盖章，下次启动重试）: {', '.join(missing)}")
    rows = c.execute(
        "SELECT id, name, scope, owner_id, host_name, engine_type, provider_id, "
        "template_id, provider_uid, template_uid, engine_uid, credential_uid, credential_id "
        "FROM agent_runtimes ORDER BY id").fetchall()
    cred_uid_by_id = {}
    try:
        cred_uid_by_id = {r[0]: (r[1] or "") for r in
                          c.execute("SELECT id, credential_uid FROM runtime_credentials").fetchall()}
    except Exception as _e:
        log.warning(f"Issue #1011 重派生：runtime_credentials.credential_uid 读取失败"
                    f"（credential_id>0 行将按空 cred_uid 派生）: {_e}")
    for (rid, name, scope, owner_id, host_name, engine_type, provider_id,
         template_id, provider_uid, template_uid, old_uid, cred_uid, cred_id) in rows:
        report["engines_total"] += 1
        _cred_uid = (cred_uid or "") or cred_uid_by_id.get(cred_id or 0, "")
        new_uid = derive_engine_uid(
            _cred_uid, name or "", scope or "", owner_id or "", host_name or "",
            engine_type=engine_type or "", provider_id=provider_id or "",
            template_id=template_id or "", provider_uid=provider_uid or "",
            template_uid=template_uid or "")
        if old_uid and new_uid == old_uid:
            report["engines_unchanged"] += 1
            continue
        # 新派生值与旧行 uid 不一致 → 映射式改绑（含 agent_runtimes 行本身）
        report["engines_rederived"] += 1
        report["rebased"].append({"runtime_id": rid, "old_engine_uid": old_uid or "",
                                  "new_engine_uid": new_uid})
        if old_uid:
            _rb = rebase_engine_uid_references(c, old_uid, new_uid)
            report["rebase_reports"].append({"runtime_id": rid, "report": _rb})
        else:
            c.execute("UPDATE agent_runtimes SET engine_uid=? WHERE id=?", (new_uid, rid))
    conn.commit()
    report["report_file"] = _write_uid_migration_report_file(conn, "1011", report)
    log.info(f"Issue #1011 engine_uid 重派生完成：total={report['engines_total']} "
             f"rederived={report['engines_rederived']} unchanged={report['engines_unchanged']}"
             f"（普通引擎重算=原值零变更；云引擎按身份不变量重派 + 引用改绑）")
    return report


def _backfill_missing_migration_reports(conn=None) -> dict:
    """Issue #1011 In Scope 0：已盖章存量库迁移报告补写（幂等）。

    PR #895 检查报告 198303559 遗留项：646/868/1011 迁移报告只在迁移真正执行时
    落盘——首轮部署在旧代码上已盖章的存量库，升级后不再跑迁移 → 永远不生成
    落盘报告。本函数在启动迁移段末尾补写：对应迁移版本已盖章且报告文件不存在
    → 补写一次空统计报告（注明「迁移已于先前版本执行」）；文件已存在不重写。
    """
    conn = conn if conn is not None else db
    out = {}
    tags = {
        "646": ("202611080011", "202611080012"),
        "868": ("202611030005", "202611030006"),
        "1011": ("202611080007", "202611080008"),
    }
    try:
        c = conn.cursor()
        applied = {r[0] for r in
                   c.execute("SELECT version FROM grape_t_schema_migrations").fetchall()}
    except Exception as e:
        log.warning(f"Issue #1011 报告补写：版本表读取失败，跳过: {e}")
        return out
    for tag, versions in tags.items():
        try:
            if not any(v in applied for v in versions):
                continue
            path = _uid_migration_report_path(conn, tag)
            if not path or os.path.exists(path):
                continue
            payload = {"generated_at": _now_iso(), "issue": tag,
                       "note": "迁移已于先前版本执行（本报告为升级后补写，空统计，"
                               "仅供落盘复核；如需真实对账请人工触发迁移重放）",
                       "report": {"backfilled_on_startup": True}}
            tmp = path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False, indent=2)
            os.replace(tmp, path)
            out[tag] = path
            log.info(f"[Issue #1011] 已盖章库迁移报告补写: {path}")
        except Exception as e:
            log.warning(f"[Issue #1011] 报告补写失败（tag={tag}，不阻断启动）: {e}")
    return out


register_migration(
    "202611080014", "issue-1011-engine-anchor-columns", "safe",
    lambda: _migration_1011_add_engine_anchor_columns(),
    domains=("runtime",))


# ======================================================================
# 缺列补齐（2026-11-17）：线上库由旧快照 provision，比代码少几列，导致每轮扫描
# 1054 报错（sweeper 每 30s / autopilot 每 60s 各一条，功能静默失效）：
#   - agent_tasks.pool_selector（Issue #1208 selector 入队落列，runtime 域）
#   - autopilot_tasks.engine_runtime_ids / custom_fields / work_branch（mgmt 域）
# 走登记式迁移而不是手工 ALTER：本域进程各自补本域列（Route A 归属）。
# ======================================================================

def _migration_202611170047_agent_tasks_pool_selector() -> None:
    """agent_tasks.pool_selector：selector 模式入队落列；缺列时 sweeper 全段失效。"""
    with db_lock:
        c = db.cursor()
        _ensure_column(c, "agent_tasks", "pool_selector", "TEXT DEFAULT ''")
        db.commit()


def _migration_202611170044_autopilot_task_columns() -> None:
    """autopilot_tasks 的三列（调度器 enabled-full / 触发器读取全列）。"""
    with db_lock:
        c = db.cursor()
        _ensure_column(c, "autopilot_tasks", "engine_runtime_ids", "TEXT DEFAULT ''")
        _ensure_column(c, "autopilot_tasks", "custom_fields", "TEXT DEFAULT ''")
        _ensure_column(c, "autopilot_tasks", "work_branch", "TEXT DEFAULT ''")
        db.commit()

register_migration(
    "202611080015", "issue-1011-backfill-rederive-engine-uids", "backfill",
    lambda: _migration_1011_backfill_rederive_engine_uids(),
    domains=("runtime",))

register_migration(
    "202611170047", "agent-tasks-pool-selector-definition", "safe",
    lambda: _migration_202611170047_agent_tasks_pool_selector(), domains=("runtime",))

def _migration_202611170046_autopilot_engine_runtime_uids() -> None:
    """autopilot_tasks.engine_runtime_uids（Issue #1011 In Scope 2 姊妹字段）。

    原挂在与 agent_runtimes 同一条 runtime 域迁移（202611080014）里，跨域混表：
    分库后 runtime 进程建不了 mgmt 库的表、mgmt 进程又被域过滤掉，列永远不会落地。
    """
    with db_lock:
        c = db.cursor()
        _ensure_column(c, "autopilot_tasks", "engine_runtime_uids", "TEXT NOT NULL DEFAULT ''")
        db.commit()

register_migration(
    "202611170044", "autopilot-task-columns-definition", "safe",
    lambda: _migration_202611170044_autopilot_task_columns(), domains=("mgmt",))

register_migration(
    "202611170046", "autopilot-task-engine-runtime-uids", "safe",
    lambda: _migration_202611170046_autopilot_engine_runtime_uids(), domains=("mgmt",))


# ======================================================================
# ===== Issue #3733：autopilot_tasks「不可派发」状态三列 + 存量一次性自检 =====
# ======================================================================
def _migration_202611240007_autopilot_dispatch_state() -> None:
    """注册式入口：autopilot_tasks 状态三列 + 存量空指令行一次性幂等标记。

    Issue #3733：拦截语义保留，但「拦截」不能再只靠每 tick 一条日志表达——
    `autopilot_tasks` 此前没有任何「不可派发原因/健康状态」列，运营侧只能翻 Loki。

    实现不在本文件（本模块是启动期装配，9000+ 行只留接线）：见
    `src/domains/mgmt/migrations/autopilot_dispatch_state.py`。放本域的理由：
    autopilot_tasks 归 mgmt（configs/table_ownership.json），补列必须由 owner 域执行；
    含表名的 UPDATE 若留在 api 层会被 tests/arch/test_foreign_table_refs 判成
    新增跨域引用（autopilot_tasks←api）。
    """
    from src.domains.mgmt.migrations.autopilot_dispatch_state import run

    run()


# 版本号避让：202611240004 已被 #3846 沙箱表迁移占用，本迁移顺延取
# 202611240007（register_migration 撞号守卫：同号会被判「已执行」永久跳过）。
register_migration(
    version="202611240007",
    name="autopilot-task-dispatch-state",
    kind="safe",
    domains=("mgmt",),   # autopilot_tasks 归 mgmt 域（configs/table_ownership.json）
    fn=_migration_202611240007_autopilot_dispatch_state,
)


def _migration_202611240011_autopilot_state_column_defaults() -> None:
    """存量库修列：三列 TEXT NOT NULL 无默认 → VARCHAR(255) NOT NULL DEFAULT ''。

    2026-11-25 部署验证实测：`TEXT NOT NULL DEFAULT ''` 在 MySQL 8.0 落地为
    `text NOT NULL DEFAULT NULL`，建单 INSERT 不给值即 1364，POST /api/autopilot/tasks
    直接 500。列已存在时 `_ensure_column` 不改类型/默认，需独立迁移补一刀（幂等 MODIFY）。

    kind=cleanup：MODIFY COLUMN 属危险 DDL，只走 `_destructive_ddl` 唯一入口（过
    min_code_version 门槛 + MDL 归一）；被门槛拦截时 fn raise → 不盖章，下版本重试。
    """
    from src.domains.mgmt.migrations.autopilot_dispatch_state import ensure_state_column_defaults

    ensure_state_column_defaults()


def _postcheck_202611240011_autopilot_state_column_defaults() -> None:
    """盖章前校验三列默认值确已收敛为空串（Issue #3648 第 6 条约定）。"""
    from src.domains.mgmt.migrations.autopilot_dispatch_state import (
        state_column_defaults_converged,
    )

    state_column_defaults_converged()


register_migration(
    version="202611240011",
    name="autopilot-state-column-defaults",
    kind="cleanup",
    domains=("mgmt",),
    fn=_migration_202611240011_autopilot_state_column_defaults,
    post_check=_postcheck_202611240011_autopilot_state_column_defaults,
)


# Issue #3759：managed_repos 补仓库级 VCS 存在性熔断台账列（孤儿仓库卡 4xx 熔断）。
# 实现住 mgmt 域（managed_repos 归 mgmt），本处只做注册接线。
from src.domains.mgmt.migrations.repo_vcs_health_columns import (  # noqa: E402
    MIGRATION as _M_REPO_VCS_HEALTH,
)

register_migration(**_M_REPO_VCS_HEALTH)


# ======================================================================
# ===== 沙箱控制面 schema（feat/devbox-sandbox-integration7 移植，2026-11-10）=====
# ===== i7 侧原注册于 202611040005-009（毒号段），此处以新号 202611100001 重注册，
# ===== DDL 与列集取自 i7 现值（IF NOT EXISTS/_ensure_column 幂等）。
# =============================================================================

_SANDBOX_CONTROL_TABLES: list[tuple[str, str]] = [
    # Issue #3527（2026-11-20）：grape_t_cloud_sandboxes 已从本组摘除。
    # 该表归 runtime（configs/table_ownership.json: tables.grape_t_cloud_sandboxes=runtime），
    # runtime 侧已有双实现：202611240004 ensure_cloud_sandboxes_table()（带所有权守卫；
    # 原号 202611180001 撞号 renumber，Issue #3846）
    # 与 src/domains/runtime/agents/sandbox/schema.py::ensure_sandbox_tables()。
    # 保留在这里会让 mgmt 进程额外建出第三份同名空表，把沙箱列表读成 0 行。
    # 本组剩余表全部归 mgmt；摘表不影响 202611110007 的 _ensure_column 列补齐段。
    ("grape_t_sandbox_templates", """
CREATE TABLE IF NOT EXISTS grape_t_sandbox_templates (
    id TEXT PRIMARY KEY,
    provider_id TEXT NOT NULL DEFAULT '',
    name VARCHAR(255) NOT NULL,
    template_id VARCHAR(255) NOT NULL,
    alias VARCHAR(255) DEFAULT '',
    description VARCHAR(512) DEFAULT '',
    engines VARCHAR(1024) DEFAULT '[]',
    preinstalled VARCHAR(2048) DEFAULT '{}',
    disk_size_mb INTEGER DEFAULT 0,
    enabled INTEGER DEFAULT 1,
    last_check_at VARCHAR(64) DEFAULT '',
    last_check_status VARCHAR(32) DEFAULT '',
    template_uid VARCHAR(36) DEFAULT NULL,
    last_check_error VARCHAR(512) DEFAULT '',
    created_at VARCHAR(64) NOT NULL,
    updated_at VARCHAR(64) NOT NULL,
    created_by VARCHAR(64) DEFAULT '',
    updated_by VARCHAR(64) DEFAULT ''
)"""),
    ("grape_t_sandbox_provider_config", """
CREATE TABLE IF NOT EXISTS grape_t_sandbox_provider_config (
    id TEXT PRIMARY KEY,
    platform VARCHAR(64) NOT NULL DEFAULT '',
    endpoint VARCHAR(512) NOT NULL DEFAULT '',
    api_key_enc TEXT,
    extra_config TEXT,
    enabled INTEGER DEFAULT 1,
    max_sandboxes INTEGER DEFAULT 10,
    runtime_credential_id INTEGER NOT NULL,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    updated_by VARCHAR(64) DEFAULT '',
    provider_uid VARCHAR(36) DEFAULT NULL,
    UNIQUE(provider_uid)
)"""),
    ("grape_t_engine_template_binding", """
CREATE TABLE IF NOT EXISTS grape_t_engine_template_binding (
    id TEXT PRIMARY KEY,
    provider_id TEXT NOT NULL,
    engine_type VARCHAR(64) NOT NULL,
    template_id TEXT NOT NULL,
    -- [沙箱迁移 i7 _ensure_column 散段] 探测上报用状态/版本列（provider-report 写入）
    status VARCHAR(32) DEFAULT '',
    engine_version VARCHAR(64) DEFAULT '',
    created_at VARCHAR(64) NOT NULL,
    updated_at VARCHAR(64) NOT NULL,
    -- [多模板共用引擎 spec B1 / 2026-11-04 裁定] 三元组唯一：同 Provider 下不同模板
    -- 可绑定相同引擎（UNIQUE 于 provider_id+engine_type+template_id 三列）；旧版
    -- 二列窄唯一约束曾把该裁定顶掉（上报 handler 2026-11-04 已改三元组 upsert，
    -- 建表 DDL 漏对齐——E2E 实证多模板同引擎上报 500 Duplicate entry '1-hermes'）。
    -- 存量窄约束库由迁移 202611160001 收敛。
    UNIQUE(provider_id, engine_type, template_id),
    FOREIGN KEY (provider_id) REFERENCES grape_t_sandbox_provider_config(id) ON DELETE CASCADE,
    FOREIGN KEY (template_id) REFERENCES grape_t_sandbox_templates(id) ON DELETE CASCADE
)"""),
    ("grape_t_engine_sandbox_toggle", """
CREATE TABLE IF NOT EXISTS grape_t_engine_sandbox_toggle (
    id TEXT PRIMARY KEY,
    engine_type VARCHAR(64) NOT NULL UNIQUE,
    sandbox_on INTEGER NOT NULL DEFAULT 1,
    created_at TEXT,
    updated_at TEXT
)"""),
    ("grape_t_user_llm_credentials", """
CREATE TABLE IF NOT EXISTS grape_t_user_llm_credentials (
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
    UNIQUE(user_id, engine_type)
)"""),
    ("grape_t_repo_llm_credentials", """
CREATE TABLE IF NOT EXISTS grape_t_repo_llm_credentials (
    id TEXT PRIMARY KEY,
    repo_key VARCHAR(512) NOT NULL,
    engine_type VARCHAR(64) NOT NULL DEFAULT 'common',
    provider VARCHAR(64) NOT NULL DEFAULT '',
    api_key_enc TEXT,
    base_url VARCHAR(512) DEFAULT '',
    model VARCHAR(128) DEFAULT '',
    enabled INTEGER DEFAULT 1,
    updated_by VARCHAR(64) DEFAULT '',
    updated_at TEXT
)"""),
    ("grape_t_egress_admin_policy", """
CREATE TABLE IF NOT EXISTS grape_t_egress_admin_policy (
    id TEXT PRIMARY KEY,
    skill_name    VARCHAR(191) NOT NULL,
    skill_scope   VARCHAR(64) NOT NULL DEFAULT 'official',
    policy        VARCHAR(32) NOT NULL,
    approved_by   VARCHAR(128),
    approved_at   TEXT,
    reason        TEXT,
    created_at    TEXT DEFAULT (datetime('now')),
    updated_at    TEXT DEFAULT (datetime('now')),
    UNIQUE(skill_name, skill_scope)
)"""),
    # 2026-11-18：原 DDL 带 FOREIGN KEY (runtime_id) REFERENCES agent_runtimes(id)。
    # 三库拆分后 agent_runtimes 归 runtime，mgmt 库里没有它 → MySQL 报
    # (1824, "Failed to open the referenced table 'agent_runtimes'")，整条迁移
    # 202611110007 失败且从不盖章，本组的 5 张表（含本表）因此一张都没建出来。
    # 跨库外键在三库拓扑下本来就不该存在（连通性由应用层保证），故移除。
    ("grape_t_engine_sandbox_config", """
CREATE TABLE IF NOT EXISTS grape_t_engine_sandbox_config (
    id TEXT PRIMARY KEY,
    runtime_id    INTEGER NOT NULL,
    engine_name   TEXT NOT NULL,
    sandbox_mode  TEXT NOT NULL DEFAULT 'required',
    degrade_policy TEXT DEFAULT 'compatible',
    created_at    TEXT DEFAULT (datetime('now')),
    updated_at    TEXT DEFAULT (datetime('now')),
    UNIQUE(runtime_id, engine_name)
)"""),
    ("grape_t_engine_sandbox_paths", """
CREATE TABLE IF NOT EXISTS grape_t_engine_sandbox_paths (
    id TEXT PRIMARY KEY,
    backend_name  TEXT NOT NULL,
    path          TEXT NOT NULL,
    access        TEXT NOT NULL DEFAULT 'ro',
    created_at    TEXT DEFAULT (datetime('now')),
    updated_at    TEXT DEFAULT (datetime('now')),
    UNIQUE(backend_name, path)
)"""),
    ("grape_t_engine_sandbox_whitelist", """
CREATE TABLE IF NOT EXISTS grape_t_engine_sandbox_whitelist (
    id TEXT PRIMARY KEY,
    backend_name  TEXT NOT NULL,
    domain        TEXT NOT NULL,
    method        TEXT NOT NULL CHECK(method IN ('GET','POST','PUT','DELETE','PATCH')),
    path_prefix   TEXT NOT NULL DEFAULT '*',
    scheme        TEXT NOT NULL DEFAULT 'https' CHECK(scheme IN ('http','https')),
    port          INTEGER NOT NULL DEFAULT 443 CHECK(port BETWEEN 1 AND 65535),
    created_at    TEXT DEFAULT (datetime('now')),
    updated_at    TEXT DEFAULT (datetime('now')),
    UNIQUE(backend_name, domain, method, scheme, port)
)"""),
    ("grape_t_task_proxy_tokens", """
CREATE TABLE IF NOT EXISTS grape_t_task_proxy_tokens (
    id TEXT PRIMARY KEY,
    task_id      TEXT NOT NULL,
    token_hash   TEXT NOT NULL,
    backend_name TEXT NOT NULL,
    created_at   TEXT DEFAULT (datetime('now')),
    expires_at   TEXT,
    UNIQUE(task_id, backend_name)
)"""),
    ("grape_t_egress_admin_exception", """
CREATE TABLE IF NOT EXISTS grape_t_egress_admin_exception (
    id TEXT PRIMARY KEY,
    host          VARCHAR(255) NOT NULL,
    host_type     VARCHAR(16) NOT NULL,
    ports         VARCHAR(256) NOT NULL,
    methods       VARCHAR(256) NOT NULL DEFAULT '["GET"]',
    scope         VARCHAR(16) NOT NULL,
    scope_target  VARCHAR(191) NOT NULL DEFAULT '',
    skill_digest  VARCHAR(128) NOT NULL DEFAULT '',
    ttl           INTEGER NOT NULL DEFAULT 0,
    approved_by   VARCHAR(256) NOT NULL,
    reason        TEXT,
    created_at    TEXT DEFAULT (datetime('now')),
    expires_at    TEXT,
    is_active     INTEGER NOT NULL DEFAULT 1,
    UNIQUE(host, host_type, ports, scope, scope_target)
)"""),
]


def _migration_sandbox_control_plane_schema() -> None:
    """沙箱控制面表/列（i7 移植）。全部 IF NOT EXISTS/_ensure_column 幂等。

    Issue #3527：本组表全部归 mgmt；函数体逐表过归属守卫，使「已配 DSN 但未注入
    迁移属主（不筛域）」形态下 runtime/insight 进程也不会越权建出 mgmt 同名空表。
    grape_t_cloud_sandboxes 已摘出本组，由 runtime 域 202611240004 建表（原号
    202611180001 撞号 renumber，Issue #3846）。
    """
    with db_lock:
        c = db.cursor()
        for _name, ddl in _SANDBOX_CONTROL_TABLES:
            if _owned_skip(_name, "create sandbox control table"):
                continue
            c.execute(ddl)
        # [沙箱迁移] 沙箱凭据形态列：cred_type 区分普通 worker('worker')/沙箱 worker('sandbox')
        _ensure_column(c, "runtime_credentials", "cred_type", "TEXT DEFAULT 'worker'")
        # [沙箱迁移] 沙箱凭据平台列（cubesandbox/devbox...）
        _ensure_column(c, "runtime_credentials", "platform", "TEXT DEFAULT ''")
        # [沙箱迁移] 引擎注册改造：runtime_type(local/sandbox)+runtime_info(JSON 模板/平台信息)
        # 列定义与 src/domains/runtime/schema_ddl.py::_AGENT_RUNTIMES_DDL 及
        # 迁移 202611180002 保持一致（QA M-2：同列三处定义漂移会让新库/存量库行为分叉）。
        _ensure_column(c, "agent_runtimes", "runtime_type", "VARCHAR(32) NOT NULL DEFAULT 'local'")
        _ensure_column(c, "agent_runtimes", "runtime_info", "TEXT", allow_copy=True)
        # [沙箱迁移 i7 _seed_sandbox_whitelist] Issue #156: 官方域名白名单种子（幂等）
        _OFFICIAL_DOMAINS = [
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
            ("pypi.org", "GET", "*", "https", 443),
            ("pypi.org", "POST", "*", "https", 443),
            ("files.pythonhosted.org", "GET", "*", "https", 443),
            ("files.pythonhosted.org", "POST", "*", "https", 443),
            ("github.com", "GET", "*", "https", 443),
            ("github.com", "POST", "*", "https", 443),
        ]
        # 白名单种子表归 mgmt：非属主域跳过，否则守卫已跳过建表、这里 INSERT 会 1146。
        if not _owned_skip("grape_t_engine_sandbox_whitelist", "seed sandbox whitelist"):
            for domain, method, path_prefix, scheme, port in _OFFICIAL_DOMAINS:
                try:
                    from src.kernel.sandbox_uid import new_row_uid as _nru
                    c.execute(
                        "INSERT OR IGNORE INTO grape_t_engine_sandbox_whitelist "
                        "(id, backend_name, domain, method, path_prefix, scheme, port) VALUES (?, ?, ?, ?, ?, ?, ?)",
                        (_nru(), "*", domain, method, path_prefix, scheme, port))
                except Exception:
                    pass
        db.commit()


# [rebase 撞号 2026-11-12] 原 202611100003 与基线 scene-pin-columns（weekly-11 新批次
# 迁移时已顺延到 003）撞号——同号迁移被判「已执行」永久跳过。顺延到 202611110007
# （004-006 已被 #1208 selector 占用，007 空闲）。迁移为幂等 DDL，重跑无损。
# [Issue #3527] 本迁移只剩 mgmt 归属表（grape_t_cloud_sandboxes 已摘出并由 runtime
# 域的 202611240004 负责——原号 202611180001 撞号 renumber，Issue #3846），显式声明
# domains 避免缺省域语义随摘表漂移；函数体内的
# runtime 列补齐段（runtime_credentials / agent_runtimes）由 _ensure_column 守卫跳过，
# 真正落地在 runtime 域 202611170045 / 202611180002。
register_migration(
    "202611110007", "issue-sandbox-control-plane-schema", "safe",
    lambda: _migration_sandbox_control_plane_schema(),
    domains=("mgmt",))
# =====================================================================
# ===== Issue #1208: selector 标签化调度迁移 ===========================
# 版本号：202611110004/5/6。[撞号教训 2026-11-11] 原选 202611110001-003「全新日期
# 号段两线天然区分」——与 arch-weekly-11 新批次 #993 create-branch-guard-state-table
# 的 202611110001 同日撞号（先登记者赢、后者永久跳过，实测 #1208 迁移会被静默丢弃）。
# renumber 至 004-006（保持 safe→backfill 依赖顺序），规则同 #646 重编先例。
# - 202611110004 safe：agent_tasks.pool_selector（TEXT NULL，MySQL 侧经
#   _issue_646_column_ddl 去 DEFAULT——#627 MySQL TEXT DEFAULT 教训）。
#   与 pool_engine_ids（id 池）/ pool_engine_uids（uid 池）三列语义互斥，
#   同任务只走一种模式（互斥由入队分叉判据保证，列级不强约束）。
# - 202611110005 backfill：T11 存量场景节点 uid 升格（见下方函数 docstring）。
# 幂等：经 grape_t_schema_migrations 盖章 + 函数内幂等，可重复执行。
# =====================================================================

def _migration_1208_add_pool_selector(conn=None) -> None:
    """[202611110004 safe] Issue #1208：agent_tasks.pool_selector 幂等加列。

    selector 任务落行 {"tags":[...]}；pool_engine_ids/pool_engine_uids 置空。
    未建表（异常库形态）降级跳过不阻断启动（列未建成时任务侧 pool_selector
    写入按 _table_columns 兼容过滤，README 口径同 #646 未迁移旧库）。
    """
    conn = conn if conn is not None else db
    with (db_lock if conn is db else nullcontext()):
        c = conn.cursor()
        try:
            cols = [r[1] for r in c.execute("PRAGMA table_info(agent_tasks)").fetchall()]
        except Exception:
            cols = []
        if not cols:
            if conn is db:
                log.warning("Issue #1208 迁移：agent_tasks 表不存在，跳过加列（不盖章重试语义交由执行器）")
            return
        if "pool_selector" not in cols:
            _ensure_column(c, "agent_tasks", "pool_selector",
                           _issue_646_column_ddl("TEXT"))
        conn.commit()


register_migration(
    "202611110004", "issue-1208-add-pool-selector", "safe",
    lambda: _migration_1208_add_pool_selector())


def _migration_1208_ensure_backup_table(c) -> str:
    """[Issue #1208 v2 整改] T11 升格前备份表（幂等建表）。

    存量 scene 行 nodes_json 原文在改写前整行备份，供升格误伤时人工回滚比对：
    devops_pipeline_scene_uid_backup_1208(scene_row_id PK, scene_id, nodes_json, backed_up_at)。
    """
    c.execute(
        "CREATE TABLE IF NOT EXISTS devops_pipeline_scene_uid_backup_1208 ("
        "scene_row_id INTEGER PRIMARY KEY, scene_id TEXT NOT NULL, "
        "nodes_json TEXT NOT NULL, backed_up_at TEXT NOT NULL)")
    return "devops_pipeline_scene_uid_backup_1208"


def _migration_1208_backfill_scene_uids(conn=None) -> dict:
    """[202611110005 backfill] Issue #1208 T11：存量场景节点 engine_runtime_ids 升格 uid。

    §2.7 行为表（幂等/分批/断点续跑——单事务逐行 UPDATE，天然可重跑）：
    - ids 有效：JOIN agent_runtimes 查 engine_uid → 写 uids + 清空 ids；
    - id 悬空（引擎行不存在 / engine_uid NULL）：跳过不补 + 迁移报告列清单（不猜不静默）；
    - 无绑定 / selector 节点：不动（不自动生成 selector——明确不做 §2.8/D5）。
    边界：只做 uid 升格，不自动翻译成 selector；非活跃场景行不扫。
    报告落盘 tag=1208（幂等：每次执行覆盖为最新对账结果）。

    v2 审查整改（2026-11）：
    - 升格保留 engine_runtime_ids（双写并存，读侧 uids 优先回落 ids 已等价；
      真正清退并入 #646 退役批次统一处理，避免混跑期回滚无路）；
    - 迁移改写前把待改 scene 行 nodes_json 原文备份到
      devops_pipeline_scene_uid_backup_1208（幂等：INSERT OR REPLACE 同行覆盖），
      报告记录 backup_rows 实际备份行数。
    """
    conn = conn if conn is not None else db
    report = {"scenes_total": 0, "scenes_scanned": 0, "upgraded_nodes": 0,
              "upgraded_scenes": [], "dangling": [], "skipped_selector": 0,
              "backup_rows": 0, "ids_retained": True}
    backup_table = ""
    with (db_lock if conn is db else nullcontext()):
        c = conn.cursor()
        try:
            rows = c.execute(
                "SELECT id, scene_id, nodes_json FROM devops_pipeline_scene "
                "WHERE is_active=1 ORDER BY id").fetchall()
        except Exception as e:
            log.warning(f"[Issue #1208] T11：devops_pipeline_scene 不可读，跳过回填: {e}")
            if conn is db:
                conn.commit()
            return report
        try:
            backup_table = _migration_1208_ensure_backup_table(c)
        except Exception as e:
            # 备份表建不成 → 中止改写（宁可升格不执行，不可无备份裸改）
            log.warning(f"[Issue #1208] T11：备份表创建失败，中止升格（不裸改）: {e}")
            if conn is db:
                conn.commit()
            return report
        for (row_id, scene_id, nodes_raw) in rows:
            report["scenes_total"] += 1
            if not nodes_raw:
                continue
            try:
                nodes = json.loads(nodes_raw)
            except Exception:
                continue
            if not isinstance(nodes, list):
                continue
            changed = False
            for node in nodes:
                if not isinstance(node, dict):
                    continue
                eids = node.get("engine_runtime_ids") or []
                uids = node.get("engine_runtime_uids") or []
                if (node.get("engine_selector") or {}).get("tags"):
                    report["skipped_selector"] += 1
                    continue
                if uids or not eids:
                    continue
                report["scenes_scanned"] += 1
                resolved = []
                dangling = False
                for eid in eids:
                    try:
                        _r = c.execute(
                            "SELECT engine_uid FROM agent_runtimes WHERE id=?", (int(eid),)).fetchone()
                    except (ValueError, TypeError):
                        _r = None
                    except Exception as e:
                        log.warning(f"[Issue #1208] T11：agent_runtimes 读取失败（按悬空跳过）: {e}")
                        _r = None
                    _uid = (_r[0] or "").strip().lower() if _r else ""
                    if not _uid:
                        dangling = True
                        report["dangling"].append({
                            "scene_id": scene_id, "node": node.get("tag") or node.get("id") or "",
                            "runtime_id": eid,
                            "reason": "engine_row_missing" if _r is None else "engine_uid_null"})
                    else:
                        resolved.append(_uid)
                if dangling:
                    continue  # 悬空跳过不补（保留原 ids，报告列清单）
                node["engine_runtime_uids"] = resolved
                # v2 整改：升格保留 engine_runtime_ids 不清空（真正清退并入 #646 退役批次）
                changed = True
                report["upgraded_nodes"] += 1
            if changed:
                # 改写前原文备份（同行幂等覆盖，可重跑）
                try:
                    c.execute(
                        f"INSERT OR REPLACE INTO {backup_table} "
                        "(scene_row_id, scene_id, nodes_json, backed_up_at) VALUES (?,?,?,?)",
                        (row_id, scene_id, nodes_raw,
                         _time_mod.strftime("%Y-%m-%dT%H:%M:%SZ", _time_mod.gmtime())))
                    report["backup_rows"] += 1
                except Exception as e:
                    log.warning(f"[Issue #1208] T11：scene 行备份失败（跳过该行不改写）: {e}")
                    continue
                c.execute("UPDATE devops_pipeline_scene SET nodes_json=? WHERE id=?",
                          (json.dumps(nodes, ensure_ascii=False), row_id))
                report["upgraded_scenes"].append(scene_id)
        conn.commit()
    report["report_file"] = _write_uid_migration_report_file(conn, "1208", report)
    log.info(f"[Issue #1208] T11 存量 uid 升格完成：scenes={report['scenes_total']} "
             f"upgraded_nodes={report['upgraded_nodes']} dangling={len(report['dangling'])} "
             f"selector_skipped={report['skipped_selector']} backup_rows={report['backup_rows']} "
             f"ids_retained=True")
    return report


register_migration(
    "202611110005", "issue-1208-backfill-scene-uids", "backfill",
    lambda: _migration_1208_backfill_scene_uids())


def _migration_1208_backfill_derived_agent_tags(conn=None) -> dict:
    """[202611110006 backfill] Issue #1208 v2 整改：旧/离线引擎派生标签批量补齐。

    注册/心跳双链只覆盖「升级后仍有心跳」的引擎；升级前已离线/被删环境的存量
    agent_runtimes 行永远等不到心跳，selector 圈池对它们恒为零命中（配置死）。
    本迁移 JOIN runtime_credentials 一次性补齐全量引擎的 engine:/scope:/scene:
    派生行（含离线引擎），调 sync_derived_agent_tags——其内置写放大防护
    （派生集与现有冒号行一致 + 快照已同步 → 跳过写），幂等可重跑。
    未迁移旧库（缺列/缺表）降级跳过不阻断启动。
    """
    conn = conn if conn is not None else db
    report = {"engines_total": 0, "synced": 0, "skipped_unchanged": 0, "errors": 0}
    with (db_lock if conn is db else nullcontext()):
        c = conn.cursor()
        try:
            rows = c.execute(
                "SELECT r.id, r.name, r.scene_tags, r.engine_uid, r.runtime_info, "
                "COALESCE(rc.scope,'global') "
                "FROM agent_runtimes r LEFT JOIN runtime_credentials rc ON rc.id=r.credential_id "
                "ORDER BY r.id").fetchall()
        except Exception as e:
            log.warning(f"[Issue #1208] agent_tags backfill：agent_runtimes 不可读，跳过: {e}")
            if conn is db:
                conn.commit()
            return report
        from src.domains.runtime.engine.agent_tags import sync_derived_agent_tags, derive_agent_tags
        for (rid, name, scene_tags, engine_uid, rt_info, cred_scope) in rows:
            report["engines_total"] += 1
            try:
                _et_m = ""
                try:
                    import json as _jm
                    _et_m = (_jm.loads(rt_info or "{}").get("engine_type") or "")
                except Exception:
                    _et_m = ""
                # [云引擎标签修复] engine: 按 engine_type 派生（云四段名不入标签）
                derived = derive_agent_tags(name, cred_scope, scene_tags, engine_type=_et_m)
                existing = [r[0] for r in c.execute(
                    "SELECT tag FROM engine_capabilities WHERE runtime_id=? AND enabled=1 "
                    "AND tag LIKE '%:%'", (rid,)).fetchall()]
                if sorted(existing) == sorted(derived):
                    report["skipped_unchanged"] += 1
                    continue
                sync_derived_agent_tags(c, rid, name, cred_scope, scene_tags, engine_type=_et_m,
                                        engine_uid=engine_uid or "")
                report["synced"] += 1
            except Exception as e:
                report["errors"] += 1
                log.warning(f"[Issue #1208] agent_tags backfill runtime {rid} failed: {e}")
        conn.commit()
    log.info(f"[Issue #1208] 旧/离线引擎派生标签补齐完成：engines={report['engines_total']} "
             f"synced={report['synced']} unchanged={report['skipped_unchanged']} "
             f"errors={report['errors']}")
    return report


register_migration(
    "202611110006", "issue-1208-backfill-derived-agent-tags", "backfill",
    lambda: _migration_1208_backfill_derived_agent_tags())

register_migration(
    "202611182014", "engine-template-binding-widen-3tuple", "safe",
    lambda: _migration_engine_template_binding_widen() or None)


def _migration_engine_template_binding_widen(conn=None) -> dict:
    """[202611160001 safe] grape_t_engine_template_binding 唯一约束三元组化（spec B1 兑现）。

    多模板共用引擎 spec（docs/sandbox/platform/multi-template-per-engine-spec.md B1）
    2026-11-04 已裁定 UNIQUE(provider_id, engine_type) → (provider_id, engine_type,
    template_id)，provider-report handler 同日改三元组 upsert——但建表 DDL 漏改，
    旧库/新库带窄约束时「同 Provider 不同模板上报相同引擎」必 500
    （Duplicate entry '<pid>-<engine>'，E2E 实证：tpl-124351 与 tpl-9f0b 同报 hermes）。

    幂等收敛：检测 information_schema 发现窄 UNIQUE(provider_id, engine_type)
    → DROP INDEX → ADD UNIQUE 三元组；已是三元组/无该表（未迁移旧库降级）→ 直接跳过。
    SQLite 无该表时不动作（SQLite 端建表 DDL 已是三元组，新库天然正确）。
    """
    report = {"action": "skipped", "detail": ""}
    conn = conn if conn is not None else db
    if conn is None:
        report["detail"] = "db not initialized — skip (fresh/test env)"
        return report
    with (db_lock if conn is db else nullcontext()):
        c = conn.cursor()
        try:
            rows = c.execute(
                "SELECT index_name FROM information_schema.statistics "
                "WHERE table_schema=DATABASE() AND table_name='grape_t_engine_template_binding' "
                "GROUP BY index_name HAVING COUNT(*)=2 "
                "AND SUM(column_name IN ('provider_id','engine_type'))=2 "
                "AND SUM(column_name='template_id')=0"
            ).fetchall()
        except Exception:
            # SQLite / 未建表 → 无窄索引可收（新库 DDL 已三元组）
            report["detail"] = "no information_schema (sqlite/fresh) — DDL already 3-tuple"
            return report
        if not rows:
            report["detail"] = "no narrow UNIQUE(provider_id, engine_type) found — already 3-tuple"
            return report
        _idx = rows[0][0]
        try:
            c.execute(f"ALTER TABLE grape_t_engine_template_binding DROP INDEX `{_idx}`")
            c.execute("ALTER TABLE grape_t_engine_template_binding ADD UNIQUE KEY "
                      "`uq_provider_engine_template` (provider_id, engine_type, template_id)")
            conn.commit()
            report.update(action="widened", detail=f"dropped {_idx}, added uq_provider_engine_template")
        except Exception as e:
            conn.rollback()
            raise RuntimeError(f"grape_t_engine_template_binding widen failed: {e}") from e
    log.info(f"[migration 202611160001] grape_t_engine_template_binding widen: {report['action']} ({report['detail']})")
    return report



# ===== Issue #2761：场景/技能统一分类字典 + 标签存储 =====
#
# 分类字典（category_dict）是场景/技能一二级分类的唯一真源；旧 scene_category /
# skill_category 与 scene_market.category_id / skill.category_id 保留不动（零破坏）。
# 标签统一 JSON [{name, source}]，旧逗号串在本迁移里就地回填为 manual 标签。

def _fn_category_dict_and_tags() -> None:
    """Issue #2761 M1：统一分类字典表 + 场景分类引用列 + 标签 JSON 化。

    - 建 grape_t_category_dict 并落 10 个一级域及二级种子；
    - scene_market 补 cat_l1_id / cat_l2_id（mgmt 域；skill 的三列同号需求
      归 runtime 域，见 202611210001）；
    - scene_market.tags 旧逗号串就地回填为 [{name, source:"manual"}]；
    - 幂等：IF NOT EXISTS / _ensure_column / INSERT OR IGNORE / 已规范化行跳过。
    """
    with db_lock:
        c = db.cursor()
        if not _owned_skip("grape_t_category_dict", "create"):
            c.execute(_CATEGORY_DICT_DDL)
            from src.domains.mgmt.user.db import seed_category_dict
            seed_category_dict()
        # skill 归 runtime 域（configs/table_ownership.json），本迁移只跑 mgmt 域：
        # 在这里补 skill 列会被归属守卫跳过、却因本迁移盖章而永久假绿 → 参考
        # 202611210001（runtime 域，Issue #3648 renumber）单独补。scene_market 归 mgmt，就地补。
        for _tbl, _col, _ddl in (
            ("scene_market", "cat_l1_id", "INTEGER DEFAULT 0"),
            ("scene_market", "cat_l2_id", "INTEGER DEFAULT 0"),
        ):
            # strict=True：本进程不是 _tbl 属主时抛错而不是静默跳过（2026-11-18 事故：
            # 静默跳过仍盖章 → 列永不创建 → /api/runtime/scene-tags 500 Unknown column）。
            _ensure_column(c, _tbl, _col, _ddl, strict=True)
        db.commit()
        if not _owned_skip("scene_market", "backfill tags"):
            # 回填失败**不吞**：与补列同理，失败即抛错让迁移不盖章、下次重试。
            # 该回填幂等（只处理仍含旧逗号串的行），重跑代价低；吞掉会留下「列在、数据没收敛」
            # 且永不重试，与 G-07 事故同态（架构复审：回填归属 mgmt 段、失败必须可见）。
            from src.domains.mgmt.user.db import backfill_scene_market_legacy_tags
            migrated = backfill_scene_market_legacy_tags()
            if migrated:
                log.info(f"202611171846 scene_market.tags 旧逗号串回填 {migrated} 行")


register_migration(
    version="202611171846",
    name="issue2761-category-dict-and-tags",
    kind="safe",
    # 显式标域：本迁移只碰 mgmt 属主的 grape_t_category_dict / scene_market；runtime 的
    # skill 三列由 202611210001（runtime 域）负责。不标会被默认成 mgmt。
    domains=("mgmt",),
    fn=_fn_category_dict_and_tags,
)


def _fn_skill_classification_columns() -> None:
    """Issue #2761：skill 补 cat_l1_id / cat_l2_id / tags（runtime 域）。

    skill 表在 runtime 库（configs/table_ownership.json: tables.skill=runtime），而
    202611171846 登记在 mgmt 域：mgmt 进程补这三列会被归属守卫跳过，迁移照样盖章，
    runtime 进程又因域过滤不跑它 —— 结果是列永远不建，skill_sync 每轮 1054
    （实测每次启动 146 条 ERROR）。本迁移把这三列放回属主域，strict 语义：任何一列
    建不出来就抛错不盖章，下次启动重试。
    """
    with db_lock:
        c = db.cursor()
        for _col, _ddl in (("cat_l1_id", "INTEGER DEFAULT 0"),
                           ("cat_l2_id", "INTEGER DEFAULT 0"),
                           ("tags", "TEXT DEFAULT ''")):
            _ensure_column(c, "skill", _col, _ddl, strict=True)
        db.commit()


def _postcheck_skill_classification_columns() -> list:
    """后置校验（Issue #3648）：委托 runtime 属主域模块，避免双份列清单漂移。"""
    from src.domains.runtime.migrations.skill_detail_tables import (
        verify_skill_classification_columns,
    )
    return verify_skill_classification_columns()


# [撞号修复 2026-11-18] 原用 202611180001，与同批 ensure-cloud-sandboxes-in-runtime 撞号：
# 同一 version 的登记行名已被 issue2761-skill-classification-columns 占用，
# ensure-cloud-sandboxes-in-runtime 因此被判「已执行」而从未真跑。按「已发布保持原号、
# 只改未发布的」规则，本迁移顺延到 003（002 被 ensure-runtime-type-info-columns 占用）。
#
# [Issue #3648 再 renumber] dev 三库的 202611180003 已被旧构建的
# skill-intro-override-schema 盖章（同号不同名），本迁移被永久判已执行、skill 缺列。
# 顺延到未占用号 202611210001，并加 post_check：列在才算执行成功（执行→校验→盖章）。
register_migration(
    version="202611210001",
    name="issue2761-skill-classification-columns",
    kind="safe",
    fn=_fn_skill_classification_columns,
    domains=("runtime",),
    post_check=_postcheck_skill_classification_columns,
)



# ===== Issue #2761 复审（架构 A6/A12）：表名前缀 + 显式 id 迁迁移 =====
# 原表 `category_dict` 无前缀、主键自增；改为 `grape_t_category_dict` + 显式 id
# （一级 = 1000+order，二级 = 一级 id*100+order）。已跑过 202611171846 前身的库
# 需要把存量行与 scene_market/skill 的 cat_l1_id / cat_l2_id 一起重映射。
def _fn_category_dict_prefix_and_explicit_ids() -> None:
    """把旧 category_dict 迁到 grape_t_category_dict 并重算显式 id（幂等）。"""
    with db_lock:
        c = db.cursor()
        legacy_rows: list = []
        try:
            legacy_rows = c.execute(
                "SELECT id, parent_id, level, name, slug, icon, order_num, applies_to, is_active "
                "FROM category_dict").fetchall()
        except Exception:
            legacy_rows = []
        if not _owned_skip("grape_t_category_dict", "create"):
            c.execute(_CATEGORY_DICT_DDL)
        from src.domains.mgmt.user.db import (
            category_dict_seed_id, seed_category_dict)
        if not _owned_skip("grape_t_category_dict", "seed"):
            seed_category_dict()
        db.commit()
        if not legacy_rows:
            return

        # 旧 id → 新 id：官方行按 slug 命中种子；其余（管理台手工增补）按 900000+ 顺序兜底。
        from src.domains.mgmt.user.db import _CATEGORY_DICT_SEED as _seed
        _new_by_slug = {}
        for idx, row in enumerate(_seed):
            _new_by_slug[row[3]] = category_dict_seed_id(_seed, idx)
        old_to_new: dict[int, int] = {}
        extra = 900000
        for r in legacy_rows:
            old_id, slug = int(r[0] or 0), str(r[4] or "")
            new_id = _new_by_slug.get(slug)
            if new_id:
                old_to_new[old_id] = new_id
                continue
            extra += 1
            old_to_new[old_id] = extra
            try:
                c.execute(
                    "INSERT OR IGNORE INTO grape_t_category_dict "
                    "(id, parent_id, level, name, slug, icon, order_num, applies_to, is_active) "
                    "VALUES (?,?,?,?,?,?,?,?,?)",
                    (extra, int(old_to_new.get(int(r[1] or 0), 0)), int(r[2] or 1), r[3], slug,
                     r[5] or "", int(r[6] or 0), r[7] or "both", int(r[8] or 1)))
            except Exception:
                c.execute(
                    "INSERT IGNORE INTO grape_t_category_dict "
                    "(id, parent_id, level, name, slug, icon, order_num, applies_to, is_active) "
                    "VALUES (?,?,?,?,?,?,?,?,?)",
                    (extra, int(old_to_new.get(int(r[1] or 0), 0)), int(r[2] or 1), r[3], slug,
                     r[5] or "", int(r[6] or 0), r[7] or "both", int(r[8] or 1)))
        db.commit()

        # 引用重映射：scene_market / skill 的 cat_l1_id、cat_l2_id。
        for old_id, new_id in old_to_new.items():
            if old_id == new_id:
                continue
            for tbl in ("scene_market", "skill"):
                for col in ("cat_l1_id", "cat_l2_id"):
                    try:
                        c.execute(f"UPDATE {tbl} SET {col}=? WHERE {col}=?", (new_id, old_id))
                    except Exception as e:  # noqa: BLE001 - 表/列可能不存在（旧库）
                        log.warning(f"202611171845 remap {tbl}.{col} 跳过: {e}")
        db.commit()
        try:
            c.execute("DROP TABLE IF EXISTS category_dict")
            db.commit()
            log.info(f"202611171845 旧 category_dict 已迁移并删除，重映射 {len(old_to_new)} 个 id")
        except Exception as e:  # noqa: BLE001
            log.warning(f"202611171845 删除旧 category_dict 失败: {e}")


register_migration(
    version="202611171845",
    name="issue2761-category-dict-prefix-and-explicit-ids",
    kind="safe",
    fn=_fn_category_dict_prefix_and_explicit_ids,
)


def _fn_add_issue_title_fetched_at() -> None:
    """Issue #2925：issue 表补 `title_fetched_at`（pipeline 标题兜底「已尝试」标记）。

    背景：`GET /api/pipeline` 对占位标题逐条串行外呼 VCS，且回写条件只看
    `title='' OR title IS NULL`，标题本身是事件类型名时写回后仍判占位 → 永不收敛。
    新列记录「尝试过的标题回填时间」（epoch 秒字符串），形成负缓存，使同仓后续
    请求不再重复外呼；取到真实标题时同时写回 title。

    幂等：_ensure_column 先探测列是否存在。strict=True 让补列失败抛出、不盖章，
    避免「版本盖死、列永不创建」的假绿。
    """
    with db_lock:
        c = db.cursor()
        if _owned_skip("issue", "ensure column title_fetched_at"):
            return
        _ensure_column(c, "issue", "title_fetched_at", "VARCHAR(64) DEFAULT ''",
                       strict=True)
        db.commit()


register_migration(
    # 与基线 arch-weekly-11 的 takeover-sessions-user-id-uuid-v1 撞号（两侧并行线都取了
    # 202611200001）：已发布的那条（takeover）保号，本条未发布 → 顺延到未占用号。
    version="202611200006",
    name="issue1089-agent-runtimes-desired-plugins",
    kind="safe",
    domains=("runtime",),   # agent_runtimes 属 runtime 域：三库模式下必须由 runtime 进程执行
    fn=_fn_agent_runtimes_desired_plugins_1089,
)


register_migration(
    version="202611170043",
    name="issue2925-issue-title-fetched-at",
    kind="safe",
    fn=_fn_add_issue_title_fetched_at,
)


def _fn_issue3055_ident_schema(conn=None):
    """[Issue #3055 B 项] issue ident 列宽度 + `(repo_key, issue_number)` 键空间收敛。

    实现不在本文件（9000+ 行启动模块只留接线）：见
    `src/domains/runtime/migrations/issue_ident_columns.py`
    `IssueIdentColumnsMigration`。口径 = 设计文档
    `docs/weekly-11/issue-3055-issue-ident-hardening.md` §2.3 / §3 / §4-B：

    * MySQL：`issue.issue_number` / `issue.parent_issue_number` /
      `chat_conversations.issue_number` MODIFY VARCHAR(191)（INT 列在非严格
      sql_mode 下把 ident 静默截断成 0）；
    * SQLite：**不改列类型**——INTEGER 亲和是有意契约（#995/#2072 数字编号读侧
      恒 int），非数字 ident 由动态类型原样存 text，本无截断；
    * 存量 `issue.issue_number=0` → `pr:<主键 id>` 后再建唯一索引（同仓多行 0 会
      撞 1062，历史 `log.warning` 吞异常 → 唯一索引静默缺失）；
    * `chat_conversations.issue_number=0` 是通用会话合法哨兵，不动。

    跨域守卫：两张表归 runtime，非 owner 域（不在本库/是视图）由实现侧抛
    `_NonOwnerObject`，执行器跳过且不盖章。失败一律 raise（不盖章、下次启动重试）。
    """
    from src.domains.runtime.migrations.issue_ident_columns import (
        IssueIdentColumnsMigration,
    )

    # issue / chat_conversations 归 runtime：三库模式下必须在该域连接上改列与建唯一索引
    return IssueIdentColumnsMigration(conn=conn or _migration_conn("runtime")).run()


# ===== Issue #3054：身份墓碑独立表（metadata_json → 唯一表 + 索引）=====
_EXTERNAL_IDENTITY_TOMBSTONES_DDL = """
CREATE TABLE IF NOT EXISTS grape_t_external_identity_tombstones (
  id VARCHAR(36) PRIMARY KEY,
  provider VARCHAR(64) NOT NULL,
  external_id VARCHAR(191) NOT NULL,
  grape_user_id VARCHAR(36) NOT NULL DEFAULT '',
  unbound_at VARCHAR(32) NOT NULL DEFAULT '',
  reason VARCHAR(64) NOT NULL DEFAULT '',
  UNIQUE (provider, external_id)
)
"""


def _fn_issue3054_tombstone_table(conn=None):
    """建墓碑表并从 external_identities.metadata 回填（幂等、owner 自判）。

    背景（Issue #3054 QA）：墓碑原先寄居 metadata JSON —— 不可索引（每次未命中
    身份的登录要扫该 provider 全量行并逐行解析）、无唯一约束（同一 external_id
    可能同时存在于两个用户台账，遍历命中第一条就可能登进错账号）、只保留 10 条
    （第 11 次换绑后最早那个账号回登会新建第二个 Grape 号）。

    本迁移只建表 + 回填；写路径由 identity_lifecycle.TombstoneStore 双写，
    metadata 保留最近 N 条作降级读（过渡期，不一次性清空）。
    """
    if _owned_skip("grape_t_external_identity_tombstones", "create table"):
        raise _NonOwnerObject("grape_t_external_identity_tombstones 不在本库，由 owner 域执行")

    from src.kernel.session_manager import db as _db
    c = conn.cursor() if conn is not None else _db.cursor()
    c.execute(_EXTERNAL_IDENTITY_TOMBSTONES_DDL)
    c.execute("CREATE UNIQUE INDEX IF NOT EXISTS uk_ext_tomb_provider_ext "
              "ON grape_t_external_identity_tombstones (provider, external_id)")
    if conn is not None:
        conn.commit()

    # 回填：只处理 metadata 里带 tombstones 的行；解析失败跳过（不阻塞启动）
    import json as _json
    import time as _time

    from src.kernel.id_gen import uuid7_str as _uid

    now = _time.strftime("%Y-%m-%d %H:%M:%S")
    c.execute("SELECT grape_user_id, provider, COALESCE(metadata,'') FROM external_identities "
              "WHERE metadata LIKE '%tombstones%'")
    rows = c.fetchall() or []
    filled = 0
    for grape_user_id, provider, meta in rows:
        try:
            data = _json.loads(meta or "{}")
        except Exception:
            continue
        items = data.get("tombstones") if isinstance(data, dict) else None
        if not isinstance(items, list):
            continue
        for t in items:
            if not isinstance(t, dict):
                continue
            ext = str(t.get("external_id") or "").strip()
            if not ext:
                continue
            c.execute("SELECT 1 FROM grape_t_external_identity_tombstones "
                      "WHERE provider=? AND external_id=?", (provider, ext))
            if c.fetchone():
                continue
            c.execute(
                "INSERT INTO grape_t_external_identity_tombstones "
                "(id, provider, external_id, grape_user_id, unbound_at, reason) "
                "VALUES (?,?,?,?,?,?)",
                (_uid(), provider, ext, str(grape_user_id or ""),
                 str(t.get("unbound_at") or now), str(t.get("reason") or "legacy_backfill")))
            filled += 1
    if conn is not None:
        conn.commit()
    if filled:
        log.info(f"[Issue #3054] 墓碑回填 {filled} 条（metadata → grape_t_external_identity_tombstones）")
    return {"filled": filled}


register_migration(
    version="202611172100",
    name="issue3054-external-identity-tombstones",
    kind="safe",
    fn=_fn_issue3054_tombstone_table,
)

register_migration(
    # 版本号 = 合并基线（d2c635e97）现有最大号 202611171846 之后。必须写字面量：撞号守卫
    # （tests/kernel/test_migration_no_baseline_version_collision.py）与
    # tests/api/test_migration_track_guard.py 用 ast.literal_eval 静态解析注册链。
    version="202611172020",
    name="issue3055-issue-ident-schema",
    # kind=backfill：本迁移会 UPDATE 存量 issue 行（0 号行 → pr_<id>），不是纯 DDL。
    # 标 safe 会让「迁移动过数据」在版本表里不可见（Issue #3054 QA）。
    kind="backfill",
    # 基线 arch-weekly-11 起「每域只跑自己域的迁移」+ 跨域迁移必须显式标域：
    # issue / chat_conversations 归 runtime 库，不标会被判 mgmt（默认）→ runtime
    # 进程永远不执行 → 列宽不收敛、ident 仍被 INT 截断。
    domains=("runtime",),
    fn=_fn_issue3055_ident_schema,
)

# ===================== 组织（org）特性迁移：下派 / 上下文 / 知识库 =====================
# 说明：这些表与列属本仓组织特性（Issue #2112/#2150/#2251/#2090/#2118），
# 上游 arch-weekly-11 无同名迁移，故统一登记为注册式迁移（存量库启动补齐）；
# 版本号统一重排到 202611183000+ 独立段：上游 arch-weekly-11 已占用
# 202611170042~202611170047、202611171845/1846/2020/2100 与 202611182000+，
# 原号段会撞号被版本表判「已执行」而永久跳过；迁移函数全部幂等，改号后存量库重跑安全。

def _migrate_add_org_provision_columns():
    """幂等补充组织 provisioning 与组织仓库关联字段。"""
    with db_lock:
        c = db.cursor()
        for col, ddl in (
            ("max_repos", "INTEGER DEFAULT 20"),
            ("org_repo_key", "TEXT DEFAULT ''"),
            ("org_scene_id", "TEXT DEFAULT ''"),
            ("provision_status", "TEXT DEFAULT 'none'"),
            ("provision_error", "TEXT DEFAULT ''"),
            ("provisioned_at", "TEXT DEFAULT ''"),
            ("provision_attempt", "TEXT DEFAULT ''"),
        ):
            _ensure_column(c, "organization", col, ddl)
        _ensure_column(c, "managed_repos", "is_org_repo", "INTEGER DEFAULT 0")
        _ensure_column(c, "managed_repos", "linked_by", "TEXT DEFAULT ''")
        _ensure_column(c, "managed_repos", "linked_at", "TEXT DEFAULT ''")
        db.commit()
        log.info("organization provisioning columns ensured")


register_migration(
    version="202611183000",
    name="add-org-provision-columns",
    kind="safe",
    fn=_migrate_add_org_provision_columns,
)


def _migrate_org_dispatch_tables() -> None:
    """建 grape_t_org_dispatch / grape_t_org_dispatch_target 及索引（幂等）。"""
    if _owned_skip("grape_t_org_dispatch", "create table"):
        return
    with db_lock:
        c = db.cursor()
        c.execute(_ORG_DISPATCH_DDL)
        c.execute(_ORG_DISPATCH_TARGET_DDL)
        c.execute(_ORG_DISPATCH_AGGREGATE_DDL)
        c.execute(
            "CREATE INDEX IF NOT EXISTS idx_org_dispatch_org "
            "ON grape_t_org_dispatch(org_id, created_at)"
        )
        c.execute(
            "CREATE INDEX IF NOT EXISTS idx_grape_t_org_dispatch_target_dispatch "
            "ON grape_t_org_dispatch_target(dispatch_id, status)"
        )
        c.execute(
            "CREATE INDEX IF NOT EXISTS idx_grape_t_org_dispatch_aggregate_dispatch "
            "ON grape_t_org_dispatch_aggregate(dispatch_id, terminal_status)"
        )
        # 批次级幂等键唯一索引：NULL 不参与唯一约束（SQLite/MySQL 一致），
        # 存量 NULL 行互不冲突，新建批次各有唯一 key → 跨进程并发只有一条落库。
        c.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS uq_org_dispatch_idem "
            "ON grape_t_org_dispatch(idempotency_key) WHERE idempotency_key IS NOT NULL"
        )
        db.commit()


register_migration(
    version="202611183001",
    name="create-org-dispatch-tables",
    kind="safe",
    fn=_migrate_org_dispatch_tables,
)


def _fn_add_org_dispatch_idempotency_key() -> None:
    """Issue #2112 审查 R1：存量库补 grape_t_org_dispatch.idempotency_key + 唯一索引。

    已被建表迁移盖章的库不再跑该迁移，缺列会让 create_dispatch 的
    INSERT 报 1054 且并发幂等失效，故单独补一条（幂等：_ensure_column / IF NOT EXISTS）。
    """
    if _owned_skip("grape_t_org_dispatch", "ensure idempotency index"):
        return
    with db_lock:
        c = db.cursor()
        _ensure_column(c, "grape_t_org_dispatch", "idempotency_key", "TEXT DEFAULT NULL", strict=True)
        c.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS uq_org_dispatch_idem "
            "ON grape_t_org_dispatch(idempotency_key) WHERE idempotency_key IS NOT NULL"
        )
        db.commit()


register_migration(
    version="202611183002",
    name="add-org-dispatch-idempotency-key",
    kind="safe",
    fn=_fn_add_org_dispatch_idempotency_key,
)


def _fn_add_org_dispatch_done_when_and_aggregate() -> None:
    """Issue #2251：target 补 done_when + 建聚合触发幂等台账（存量库幂等）。

    - 存量 target 回填 ``issue_done``（与列默认值一致，行为不突变）；
    - ``grape_t_org_dispatch_aggregate`` 以 ``UNIQUE(dispatch_id, terminal_status)`` 承载
      「聚合只触发一次」的幂等键。
    """
    if _owned_skip("grape_t_org_dispatch_target", "ensure done_when / aggregate"):
        return
    with db_lock:
        c = db.cursor()
        _ensure_column(c, "grape_t_org_dispatch_target", "done_when",
                       "TEXT NOT NULL DEFAULT 'issue_done'", strict=True)
        c.execute(_ORG_DISPATCH_AGGREGATE_DDL)
        c.execute(
            "CREATE INDEX IF NOT EXISTS idx_grape_t_org_dispatch_aggregate_dispatch "
            "ON grape_t_org_dispatch_aggregate(dispatch_id, terminal_status)"
        )
        db.commit()


register_migration(
    version="202611183003",
    name="add-org-dispatch-done-when-and-aggregate",
    kind="safe",
    fn=_fn_add_org_dispatch_done_when_and_aggregate,
)


def _fn_migrate_org_scene_type() -> None:
    """Issue #2118 复审：存量组织场景补写 scene_type='org'（幂等）。

    create_org_scene 在本修复前只落 org_id/advanced_options，不写 scene_type；
    已完成 provisioning 的组织不会再次走 create_org_scene，存量库里的
    custom-org-* 场景因此仍是默认 standard，组织入口校验与 org-only 模板
    解析全部失效。以 organization.org_scene_id 为权威来源，并用 provisioning
    固定前缀兜底，只升级仍为 standard 的行，不覆盖其它自定义场景。
    """
    if _owned_skip("devops_pipeline_scene", "migrate org scene type") or \
            _owned_skip("organization", "read org scene ids"):
        return
    with db_lock:
        c = db.cursor()
        _ensure_column(c, "devops_pipeline_scene", "scene_type",
                       "TEXT DEFAULT 'standard'", allow_copy=True, strict=True)
        db.commit()
    from src.domains.mgmt import public as _mgmt_public

    _mgmt_public.migrate_org_scene_type()


register_migration(
    version="202611183004",
    name="migrate-existing-org-scene-type",
    kind="safe",
    fn=_fn_migrate_org_scene_type,
)


# ===== Issue #2150：组织上下文 + 组织知识库迁移（存量库幂等补建）=====
def _fn_create_org_context_table() -> None:
    """建 org_context 表 + 查询索引（CREATE IF NOT EXISTS 幂等）。"""
    if _owned_skip("grape_t_org_context", "create table"):
        return
    with db_lock:
        c = db.cursor()
        c.execute(_ORG_CONTEXT_DDL)
        c.execute(
            "CREATE INDEX IF NOT EXISTS idx_org_context_org "
            "ON grape_t_org_context(org_id, kind, lifecycle_status)"
        )
        db.commit()


register_migration(
    version="202611183005",
    name="create-org-context-table",
    kind="safe",
    fn=_fn_create_org_context_table,
)


def _fn_add_org_knowledge_columns() -> None:
    """Issue #2150：agent_shared_knowledge 扩展组织知识生命周期/归属列（幂等补列）。

    不改既有 status(accepted/rejected) 与 archived 语义——知识看板/注入逻辑仍读旧列，
    新增 lifecycle_status 等独立列，避免既有链路回归。strict=True：失败抛出，由执行器
    跳过不盖章、下次重试（防「列没建成却盖死版本号」事故）。
    """
    with db_lock:
        c = db.cursor()
        # TEXT NOT NULL DEFAULT 列在 MySQL 上 INSTANT/INPLACE 均不支持，必须
        # allow_copy=True（项目规约 register_migration 自查第 3 条）。不加则 strict
        # 抛错 → 迁移永不盖章，org knowledge 查询 1054。
        _ensure_column(c, "agent_shared_knowledge", "lifecycle_status",
                       "TEXT NOT NULL DEFAULT 'draft'", allow_copy=True, strict=True)
        _ensure_column(c, "agent_shared_knowledge", "org_id",
                       "TEXT NOT NULL DEFAULT ''", allow_copy=True, strict=True)
        _ensure_column(c, "agent_shared_knowledge", "kind",
                       "TEXT NOT NULL DEFAULT ''", allow_copy=True, strict=True)
        _ensure_column(c, "agent_shared_knowledge", "tags",
                       "TEXT NOT NULL DEFAULT ''", allow_copy=True, strict=True)
        _ensure_column(c, "agent_shared_knowledge", "applies_to",
                       "TEXT NOT NULL DEFAULT ''", allow_copy=True, strict=True)
        _ensure_column(c, "agent_shared_knowledge", "valid_until",
                       "TEXT NOT NULL DEFAULT ''", allow_copy=True, strict=True)
        _ensure_column(c, "agent_shared_knowledge", "review_interval_days",
                       "INTEGER NOT NULL DEFAULT 0", strict=True)
        _ensure_column(c, "agent_shared_knowledge", "version",
                       "INTEGER NOT NULL DEFAULT 1", strict=True)
        _ensure_column(c, "agent_shared_knowledge", "last_reviewed_at",
                       "TEXT NOT NULL DEFAULT ''", allow_copy=True, strict=True)
        _ensure_column(c, "agent_shared_knowledge", "owner_id",
                       "TEXT NOT NULL DEFAULT ''", allow_copy=True, strict=True)
        c.execute(
            "CREATE INDEX IF NOT EXISTS idx_ask_org_lifecycle "
            "ON agent_shared_knowledge(org_id, kind, lifecycle_status)"
        )
        db.commit()


register_migration(
    version="202611183006",
    name="add-org-knowledge-columns",
    kind="safe",
    fn=_fn_add_org_knowledge_columns,
)


# ===== D105 第四批：组织域用户引用列 TEXT 化（Issue #2150/#2269）=====
# grape_t_org_context.owner_id/created_by/updated_by、grape_t_org_dispatch.created_by、
# agent_shared_knowledge.owner_id、managed_repos.linked_by 均存 user id。D105 后
# 用户主键为 uuid7 字符串，INTEGER 亲和会混型不命中（SQLite）/严格模式写崩 1366
# （MySQL）。DDL 已声明 TEXT，这里只归一存量库。
_ORG_USER_TEXT_COLS = (
    ("grape_t_org_dispatch", "created_by"),
    ("grape_t_org_context", "owner_id"),
    ("grape_t_org_context", "created_by"),
    ("grape_t_org_context", "updated_by"),
    ("agent_shared_knowledge", "owner_id"),
    ("managed_repos", "linked_by"),
)


def _normalize_org_user_text_sqlite(conn) -> int:
    """SQLite 侧：把上述列亲和 INTEGER → TEXT，存量值 CAST 保值，哨兵 0 → ''。

    幂等：全部列已 TEXT 且无混型行/哨兵时返回 0；发生整表重建返回 -1。
    表/列不存在逐项跳过（新库由 DDL 直接建 TEXT）。
    """
    c = conn.cursor()
    changed = 0
    rebuilt = False
    by_table: dict[str, list[str]] = {}
    for table, col in _ORG_USER_TEXT_COLS:
        by_table.setdefault(table, []).append(col)
    for table, cols in by_table.items():
        tcols = c.execute(f"PRAGMA table_info({table})").fetchall()
        if not tcols:
            continue
        decls = {r[1]: (r[2] or "").strip().upper() for r in tcols}
        pending = [col for col in cols
                   if col in decls and decls[col] != "TEXT"]
        if pending:
            _rebuild_table_text_columns(c, table, tcols, tuple(pending))
            rebuilt = True
            tcols = c.execute(f"PRAGMA table_info({table})").fetchall()
        present = {r[1] for r in tcols}
        for col in cols:
            if col not in present:
                continue
            changed += c.execute(
                f"UPDATE {table} SET {col}=CAST({col} AS TEXT) "
                f"WHERE typeof({col})!='text'").rowcount
            changed += c.execute(
                f"UPDATE {table} SET {col}='' WHERE {col}='0'").rowcount
    return -1 if rebuilt else changed


def _widen_mysql_org_user_text() -> int:
    """MySQL 侧：INT → VARCHAR(191) 放宽（保留非空约束，哨兵由应用层 '' 兜底）。"""
    from src.kernel.session_manager import db as _db, db_lock as _db_lock

    nullable = {("managed_repos", "linked_by")}
    changed = 0
    with _db_lock:
        c = _db.cursor()
        for table, col in _ORG_USER_TEXT_COLS:
            row = c.execute(
                "SELECT DATA_TYPE FROM information_schema.columns "
                "WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME=%s AND COLUMN_NAME=%s",
                (table, col),
            ).fetchone()
            if not row:
                continue
            if str(row[0] or "").lower() in ("varchar", "char", "text", "mediumtext", "longtext"):
                continue
            spec = "VARCHAR(191) DEFAULT ''" if (table, col) in nullable \
                else "VARCHAR(191) NOT NULL DEFAULT ''"
            c.execute(f"ALTER TABLE {table} MODIFY {col} {spec}")
            changed += 1
        if changed:
            _db.commit()
    return changed


def _fn_normalize_org_user_text() -> None:
    from src.kernel.session_manager import (
        _is_mysql_backend,
        db as _db,
        db_lock as _db_lock,
    )

    if any(_owned_skip(t, "normalize org user text") for t, _ in _ORG_USER_TEXT_COLS):
        return
    if _is_mysql_backend():
        _widen_mysql_org_user_text()
        return
    with _db_lock:
        _normalize_org_user_text_sqlite(_db)
        _db.commit()


register_migration(
    version="202611183008",
    name="normalize-org-user-text",
    kind="safe",
    fn=_fn_normalize_org_user_text,
)


# ===== 组织上下文快照表（2026-11-16）=====
def _fn_create_org_context_snapshot() -> None:
    if _owned_skip("grape_t_org_context_snapshot", "create table"):
        return
    with db_lock:
        c = db.cursor()
        c.execute(_ORG_CONTEXT_SNAPSHOT_DDL)
        c.execute(
            "CREATE INDEX IF NOT EXISTS idx_org_context_snapshot_org_dispatch "
            "ON grape_t_org_context_snapshot(org_id, dispatch_id)"
        )
        c.execute(
            "CREATE INDEX IF NOT EXISTS idx_org_context_snapshot_demand "
            "ON grape_t_org_context_snapshot(org_id, demand_id)"
        )
        db.commit()


register_migration(
    version="202611183009",
    name="create-org-context-snapshot",
    kind="safe",
    fn=_fn_create_org_context_snapshot,
)


# ===== Issue #2150/org pipeline：issue 记录组织上下文快照引用 =====
def _fn_add_issue_org_snapshot_column() -> None:
    # 2026-11-20（审计 F3）：本迁移 ALTER 的是 `issue`，其属主域是 runtime。
    # 旧写法「注册域缺省 mgmt + raise_on_guard 未开」在三库模式下形成假绿：
    #   mgmt 进程 _owned_skip(issue) 命中 → _ensure_column 静默 return False → fn 正常返回 → 盖章；
    #   runtime 进程又因缺省域=mgmt 被 filter_migration_registry 剔除，压根不跑。
    # 两侧都不建列，而 engine/service.py:1087 SELECT 该列 → 1054。
    # 修法：注册域收敛到属主域 runtime；并开 raise_on_guard，万一仍在非属主进程执行则显式失败不盖章。
    with db_lock:
        c = db.cursor()
        _ensure_column(c, "issue", "org_context_snapshot_id",
                       "TEXT NOT NULL DEFAULT ''", strict=True, raise_on_guard=True)
        db.commit()


register_migration(
    version="202611183010",
    name="add-issue-org-context-snapshot",
    kind="safe",
    domains=("runtime",),
    fn=_fn_add_issue_org_snapshot_column,
)


# ===== 组织仓库创建选项：平台 / 所属空间 / 仓库名 / 路径 / 可见性 =====
def _fn_add_org_repo_create_options() -> None:
    with db_lock:
        c = db.cursor()
        for col, ddl in (
            ("org_repo_provider", "TEXT DEFAULT ''"),
            ("org_repo_owner", "TEXT DEFAULT ''"),
            ("org_repo_name", "TEXT DEFAULT ''"),
            ("org_repo_path", "TEXT DEFAULT ''"),
            ("org_repo_private", "INTEGER DEFAULT 1"),
        ):
            _ensure_column(c, "organization", col, ddl)
        db.commit()


register_migration(
    version="202611183011",
    name="add-org-repo-create-options",
    kind="safe",
    fn=_fn_add_org_repo_create_options,
)


# ===== 组织仓库创建选项：假盖章修复（2026-11-23）=====
# 202611183011 在部分库已盖章但 organization 仍缺 5 列（实测 POST /api/orgs 1054）。
# 老迁移的 strict/raise_on_guard 补不上——版本号已判「已执行」，改它只改 checksum。
# 用新号幂等重跑：strict + raise_on_guard + post_check，列不全不盖章，下次启动重试。
# SQL 住属主域 migrations 下（organization owner=mgmt）。
from src.domains.mgmt.migrations.org_repo_create_options import (
    ensure_org_repo_create_columns as _ensure_org_repo_create_columns,
    verify_org_repo_create_columns as _verify_org_repo_create_columns,
)

register_migration(
    version="202611230012",
    name="repair-org-repo-create-options",
    kind="safe",
    domains=("mgmt",),
    fn=_ensure_org_repo_create_columns,
    post_check=_verify_org_repo_create_columns,
)

# ===== 组织仓库地图（MD-first） =====
def _fn_create_org_repo_map_table() -> None:
    if _owned_skip("grape_t_org_repo_map", "create table"):
        return
    from src.domains.mgmt.org_repo_map import ensure_table

    ensure_table()


register_migration(
    version="202611183012",
    name="create-org-repo-map-table",
    kind="safe",
    fn=_fn_create_org_repo_map_table,
)


def _fn_widen_org_issue_ident() -> None:
    """D105：组织下派/快照的 issue 编号列改 TEXT，支持 Gitee IK* 等非数字 ident。

    SQLite 动态类型无需变更；MySQL 严格模式必须显式 MODIFY，否则写字符串直接 1366。
    """
    from src.kernel.session_manager import _is_mysql_backend

    if not _is_mysql_backend():
        return
    if _owned_skip("grape_t_org_dispatch", "widen org issue ident"):
        return
    with db_lock:
        c = db.cursor()
        # Issue #536：MODIFY COLUMN 属破坏性 DDL，统一经 _destructive_ddl 收口；
        # cleanup 门槛未过（蓝绿共存期落后实例）→ 不盖章，下版本重试。
        _blocked = False
        if not _destructive_ddl(
                c, "ALTER TABLE grape_t_org_dispatch MODIFY COLUMN org_issue_number "
                   "VARCHAR(191) NOT NULL DEFAULT ''", kind_hint="cleanup"):
            _blocked = True
        if not _destructive_ddl(
                c, "ALTER TABLE grape_t_org_dispatch_target MODIFY COLUMN child_issue_number "
                   "VARCHAR(191) DEFAULT ''", kind_hint="cleanup"):
            _blocked = True
        if not _destructive_ddl(
                c, "ALTER TABLE grape_t_org_context_snapshot MODIFY COLUMN demand_id "
                   "VARCHAR(191) NOT NULL DEFAULT ''", kind_hint="cleanup"):
            _blocked = True
        db.commit()
        if _blocked:
            raise RuntimeError(
                "widen-org-issue-ident: MODIFY 被 cleanup 门槛拦下"
                "（本实例版本落后于 meta.min_code_version）→ 本次不盖章，下版本重试")


register_migration(
    version="202611183013",
    name="widen-org-issue-ident",
    kind="safe",
    fn=_fn_widen_org_issue_ident,
)


# ===== 2026-11-18 补正：category_dict 列被域归属守卫静默跳过（线上 1054） =====
# 事故链：202611171846 在「进程域判定不出/非属主」时，_ensure_column 静默 return False，
# fn 正常返回 → 迁移盖章 → scene_market / skill 的 cat_l1_id 永不创建也不重试 →
# /api/runtime/scene-tags 500 `Unknown column 'cat_l1_id'`（引擎详情「场景标签」空白）
# + skill_sync 每轮 ERROR。修法两条：
#   ① _ensure_column(strict=True) 跳过即抛错（执行器不盖章，下次在属主域重试）；
#   ② 按属主域各补一条幂等补正迁移，把已盖章库里缺失的列补上（存量库不会重跑老号）。
_CATEGORY_COLUMN_PLANS = (
    # (属主域, 表, ((列, DDL), ...), 需重建视图的消费域)
    # 只留 mgmt：skill 的三列归 runtime 域，已由 202611210001 补（Issue #3648 renumber），
    # 在这里重复补只会与它抢写同一批列（架构复审 F3 / 基线 085f736a5）。
    ("mgmt", "scene_market",
     (("cat_l1_id", "INTEGER DEFAULT 0"), ("cat_l2_id", "INTEGER DEFAULT 0")),
     ("insight", "runtime")),
)

# skill 的消费域视图：列由 202611210001 补，但 MariaDB 视图列清单在 CREATE 时固化，
# 补列不会让既有视图自愈 —— 读 mgmt/insight 上的 skill 视图仍 1054。本迁移只重建视图。
_SKILL_VIEW_OWNER = "runtime"
_SKILL_VIEW_TABLE = "skill"
_SKILL_VIEW_TARGETS = ("mgmt", "insight")


def _fn_category_columns_repair(owner_domain: str) -> None:
    """按属主域补齐 category_dict 相关列 + 刷新消费域视图（幂等，可重跑）。"""
    plan = next((p for p in _CATEGORY_COLUMN_PLANS if p[0] == owner_domain), None)
    if plan is None:
        raise RuntimeError(f"未知属主域: {owner_domain}")
    _dom, table, cols, targets = plan
    with db_lock:
        c = db.cursor()
        for col, ddl in cols:
            # raise_on_guard=True：本进程不是属主时抛错（不盖章、下次在属主域重试）
            _ensure_column(c, table, col, ddl, strict=True, raise_on_guard=True)
        db.commit()
    log.info(f"category_columns_repair({owner_domain}.{table}) 完成（列已就绪；消费域视图由部署步骤重建）")


def _fn_category_columns_repair_mgmt() -> None:
    _fn_category_columns_repair("mgmt")


register_migration(
    # 补正 202611171846 在存量库漏掉的列（该号已盖章，不会再跑）。号段取 20261118-1200，
    # 提交前已核对：源码与 grape_aw11_* 三库 grape_t_schema_migrations 均未占用。
    version="202611181200",
    name="issue2761-category-columns-repair-mgmt",
    kind="safe",
    domains=("mgmt",),
    fn=_fn_category_columns_repair_mgmt,
)


# ===== Issue #3168 技能详情页 / #3202 分类收敛：迁移内容归 owner 域 =====
# skill / grape_t_skill_version / grape_t_skill_file / grape_t_skill_comment 归 runtime（skill_category 归 mgmt），
# grape_t_category_dict / scene_market 归 mgmt——SQL 一律住在 owner 域的
# migrations 模块（跨域守卫 P0-2：api 层直写他域表 = 新增跨域引用），
# 这里只做注册引导（模式同 ensure_runtime_type_info_columns / drop_same_domain_links）。
# 版本号沿革（rebase 改号记录见各模块 docstring）：1301/1302 曾用
# 202611180001/002（基线占用）→ 202611181201/1202（基线守卫钉空）→ 202611181301/1302；
# 1303 原用 202611180003（基线 issue2761-skill-classification-columns 占用，该条已按
# Issue #3648 改号到 202611210001）。
# 分类收敛按属主拆两条：202611210002（mgmt：字典停用 + scene_market 重挂）、
# 202611210003（runtime：skill 重挂 + 老分类废止）——QA A11：混在一条无 domains=
# 声明的迁移里，三库拓扑下 runtime 库的收敛会被静默跳过。两条原用
# 202611180004/202611181304，因 dev 库已被旧名盖章而按 #3648 改号，并各带 post_check。
from src.domains.runtime.migrations.skill_detail_tables import (
    MIGRATION_SCHEMA as _M_SKILL_DETAIL_SCHEMA,
    MIGRATION_BACKFILL as _M_SKILL_DETAIL_BACKFILL,
    MIGRATION_INTRO_OVERRIDE as _M_SKILL_INTRO_OVERRIDE,
)
register_migration(**_M_SKILL_DETAIL_SCHEMA)
register_migration(**_M_SKILL_DETAIL_BACKFILL)

from src.domains.mgmt.migrations.category_collapse_dict import MIGRATION as _M_CAT_COLLAPSE_MGMT
register_migration(**_M_CAT_COLLAPSE_MGMT)

from src.domains.runtime.migrations.skill_category_collapse import MIGRATION as _M_CAT_COLLAPSE_RT
register_migration(**_M_CAT_COLLAPSE_RT)

register_migration(**_M_SKILL_INTRO_OVERRIDE)

# ===== 2026-11-19 补正：D105 第三批 org 域 TEXT 化从未在 MySQL 存量库执行 =====
# 事故链：`_normalize_org_domain_text()` 只实现了 SQLite 分支（PRAGMA / sqlite_master
# 整表重建），MySQL 上直接 1064；而 `_migrate_add_org_id_columns()` 只在列缺失时
# `ADD COLUMN org_id TEXT`，对已存在的 INT 列既不报错也不转换。于是「老 schema +
# 新代码」的 MySQL 库上，`org_id` 永远是 INT，而新代码把未归属/个人组织写成 ''
# 哨兵 —— 新建 runtime 凭据必报 (1366, "Incorrect integer value: '' for column
# 'org_id'")，前端「AI Devspace 云开发环境」卡片点创建即失败。
# 修法：补 MySQL 分支（_widen_mysql_org_ids）+ 注册式迁移补齐存量库。
# 号段核对（提交前）：源码 register_migration 与四套目标库
# grape_t_schema_migrations 均未占用 202611190100。
def _fn_normalize_org_domain_text() -> None:
    """D105 第三批 org 域字符串化（存量补齐；幂等，按域进程各跑一次）。

    只处理**本域库里存在**的表：三库拓扑下 mgmt 进程改 organization/org_member 等，
    runtime 进程改 runtime_credentials/agent_tasks 等，缺表按项跳过。

    转换后自查列类型：`_destructive_ddl()` 对非 MDL 异常抛出（不盖章），
    只有「已达目标态」错误码放行；但放行不等于生效，仍以列类型实查为准（QA M3/I2）。发现仍有数值列 → ERROR +
    raise，执行器据此不盖章、下次启动重试——本函数属注册式迁移，存量库每次启动
    都会跑到（QA M2）。
    """
    with db_lock:
        n = _normalize_org_domain_text(db)
        db.commit()
    bad = _org_columns_not_text(db)
    if bad:
        log.error(f"org domain text normalize 后仍有数值列（'' 哨兵会报 1366）: {bad}")
        raise RuntimeError(
            f"org domain text normalize 未完成：{len(bad)} 列仍非字符串族 {bad}；"
            "本次不盖章，下次启动重试（大表 MODIFY 是 COPY 锁写，需安排低峰/维护窗口）")
    log.info(f"org domain text normalize 完成（本域改动: {n}）")


def _fn_normalize_org_domain_text_sqlite() -> None:
    """D105 第三批：存量 SQLite 库 org 域 id TEXT 化（幂等）。

    背景：organization.id 的 TEXT 主键 DDL 只在全新库生效。存量库建表时仍是
    ``INTEGER PRIMARY KEY AUTOINCREMENT``，而 create_org 已改为写入 uuid7 字符串
    （TEXT 主键）——旧库创建组织会直接 ``datatype mismatch``，组织 provisioning
    全链路不可用。MySQL 环境的 org 域 TEXT 化已由历史迁移盖章，
    这里只处理 SQLite 旧 DDL（PRAGMA 重建，见 _normalize_org_domain_text）。

    为什么单独保留：迁移 202611183007 已在各环境盖章，删号会让它永久跳过
    （迁移台账按版本号判「已执行」）。后续的跨域 TEXT 化收敛到
    202611190100（d105-org-domain-text-normalize），两者并存不重复执行：
    本迁移在 MySQL 上直接返回，收敛版在 SQLite 上同样幂等。
    """
    from src.kernel.session_manager import _is_mysql_backend, db as _db, db_lock as _db_lock
    if _is_mysql_backend():
        # MySQL：历史迁移已把 org 域改为 VARCHAR/TEXT，无需重复处理。
        return
    with _db_lock:
        _normalize_org_domain_text(_db)
        _db.commit()


register_migration(
    version="202611183007",
    name="normalize-org-domain-text",
    kind="safe",
    fn=_fn_normalize_org_domain_text_sqlite,
)


register_migration(
    version=_ORG_TEXT_MIGRATION_VERSION,
    name="d105-org-domain-text-normalize",
    kind="safe",
    domains=("mgmt", "runtime"),
    fn=_fn_normalize_org_domain_text,
)


# ===== 2026-11-19 补正：org 新表前缀 + UUID 主键（A6/A12 复审整改）=====
# 本 PR 新增的 6 张组织表最初按 org_* 命名、主键为 INTEGER AUTOINCREMENT。
# 规则 A6 要求 grape 主控系统新增表统一 grape_t_xxx；A12 要求新增对象表唯一键
# 不得自增（用 32 位 uuid）。新库由上面的建表迁移直接落正确形态；存量库
# （202611183001~202611183013 已盖章）走本迁移：先按旧名→新名改名，再逐表把
# 主键重建为 TEXT（UuidPkMigration 自省，存量整型 id CAST 字符串化保值），
# 最后把引用列 dispatch_id / issue.org_context_snapshot_id 放宽为字符串。
# 幂等：旧表不存在或新表已改名时跳过；已是 TEXT 主键时 UuidPkMigration 直接返回。
_ORG_TABLE_RENAMES = (
    ("org_dispatch", "grape_t_org_dispatch"),
    ("org_dispatch_target", "grape_t_org_dispatch_target"),
    ("org_dispatch_aggregate", "grape_t_org_dispatch_aggregate"),
    ("org_context_snapshot", "grape_t_org_context_snapshot"),
    ("org_context", "grape_t_org_context"),
    ("org_repo_map", "grape_t_org_repo_map"),
)


def _fn_rename_org_tables_and_uuid_pk() -> None:
    from src.kernel.session_manager import _is_mysql_backend

    mysql = _is_mysql_backend()
    with db_lock:
        c = db.cursor()
        for old, new in _ORG_TABLE_RENAMES:
            if mysql:
                old_ok = c.execute(
                    "SELECT COUNT(*) FROM information_schema.tables "
                    "WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME=%s", (old,)
                ).fetchone()[0]
                new_ok = c.execute(
                    "SELECT COUNT(*) FROM information_schema.tables "
                    "WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME=%s", (new,)
                ).fetchone()[0]
            else:
                old_ok = bool(c.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                    (old,)).fetchone())
                new_ok = bool(c.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                    (new,)).fetchone())
            if old_ok and not new_ok:
                c.execute(f"ALTER TABLE {old} RENAME TO {new}")
                log.info(f"org table rename: {old} -> {new}")
        db.commit()

    # 主键整型→TEXT（SQLite 重建 / MySQL MODIFY；已是目标形态则零写入）
    # 2026-11-20（审计 F2）：只处理本域库里真实存在的表。三库模式下 runtime/insight 库
    # 没有这些 mgmt 表，旧写法会 1146 → 不盖章 → 每轮重试永不收敛。
    from src.api.uuid_pk_migration import UuidPkMigration

    for _old, new in _ORG_TABLE_RENAMES:
        with db_lock:
            _present = _base_table_present(db.cursor(), new)
        if not _present:
            continue
        UuidPkMigration(pk_table=new).run()

    if mysql:
        with db_lock:
            c = db.cursor()
            # Issue #536：MODIFY COLUMN 属破坏性 DDL，统一经 _destructive_ddl 收口。
            # 缺表/已是字符串族由 _destructive_ddl 内部吞异常继续（幂等可重试）；
            # cleanup 门槛未过 → 不盖章，下版本重试。
            _blocked = False
            for tbl in ("grape_t_org_dispatch_target",
                        "grape_t_org_dispatch_aggregate",
                        "grape_t_org_context_snapshot"):
                if not _destructive_ddl(
                        c, f"ALTER TABLE {tbl} MODIFY COLUMN dispatch_id VARCHAR(36) NOT NULL",
                        kind_hint="cleanup"):
                    _blocked = True
            if not _destructive_ddl(
                    c, "ALTER TABLE issue MODIFY COLUMN org_context_snapshot_id "
                       "VARCHAR(36) NOT NULL DEFAULT ''", kind_hint="cleanup"):
                _blocked = True
            db.commit()
            if _blocked:
                raise RuntimeError(
                    "rename-org-tables-and-uuid-pk: MODIFY 被 cleanup 门槛拦下"
                    "（本实例版本落后于 meta.min_code_version）→ 本次不盖章，下版本重试")
    else:
        with db_lock:
            c = db.cursor()
            info = {r[1]: (r[2] or "").lower()
                    for r in c.execute("PRAGMA table_info(issue)").fetchall()}
            if info.get("org_context_snapshot_id") not in (None, "text"):
                _rebuild_table_text_columns(
                    c, "issue", c.execute("PRAGMA table_info(issue)").fetchall(),
                    ("org_context_snapshot_id",))
                c.execute(runtime_mig.UPDATE_ISSUE_ORG_CONTEXT_SNAPSHOT_ID_SENTINEL)
                db.commit()
                log.info("org uuid-pk: issue.org_context_snapshot_id -> TEXT (SQLite)")


def _fn_add_autopilot_exec_params() -> None:
    """Autopilot 执行记录补 params_json（组织 AI 分析单次运行参数）。"""
    try:
        with db_lock:
            c = db.cursor()
            _ensure_column(c, "autopilot_executions", "params_json", "MEDIUMTEXT DEFAULT ''")
            db.commit()
    except Exception as exc:  # 缺表/别域库：不阻断（runtime 进程会在本域库补齐）
        log.warning(f"autopilot_executions.params_json ensure skipped: {exc}")


def _fn_seed_org_repo_analyzer_skill() -> None:
    """注册组织 AI 分析技能（通过 runtime public，避免 api 直写 skill 表）。"""
    try:
        from src.domains.runtime import public as _rt_public

        _rt_public.seed_official_skill_if_missing(
            "org-repo-analyzer",
            "组织仓库 AI 分析",
            "组织关联仓库的 AI 能力分析（只读，输出结构化摘要并回传平台）",
            "qa",
            "qa/org-repo-analyzer",
        )
    except Exception as exc:  # 分库/缺表不阻断
        log.warning(f"seed org-repo-analyzer skill skipped: {exc}")


def _fn_create_org_repo_ai_table() -> None:
    """组织仓库 AI 分析台账表（与确定性地图分表）。"""
    from src.domains.mgmt.schema_ddl import _ORG_REPO_AI_DDL

    with db_lock:
        c = db.cursor()
        c.execute(_ORG_REPO_AI_DDL)
        db.commit()


def _fn_personal_orgs_from_accounts(conn=None):
    from src.domains.mgmt.migrations.personal_org_backfill import (
        migrate_personal_orgs_from_accounts as _impl,
    )

    return _impl(conn)


def _fn_runtime_org_backfill(conn=None):
    from src.domains.runtime.migrations.org_id_backfill import (
        migrate_runtime_org_ids as _impl,
    )

    return _impl(conn)


def _fn_add_platform_ca_bundle() -> None:
    """Issue #3580：grape_t_repo_platforms 增 ca_bundle（实例 TLS CA，PEM 绝对路径）。

    部署补强（2026-11-20，arch-weekly-11-yaw）：原版本号 202611200001 已被
    takeover-sessions-user-id-uuid-v1（PR #1869，11:08 落库）先行盖帽——三库台账
    同号不同名 → 本迁移永久判「已执行」，列永不落地；mgmt public 的
    repo_platform_cards 查询带 ca_bundle 直接 1054，平台卡整体回落文件（DB 覆盖/
    enabled 开关全失效）。renumber 到空号 202611200002，并加 strict + 落地回验：
    印章只在列真正存在后写。
    """
    from src.kernel.session_manager import db as _db, db_lock as _lock

    with _lock:
        c = _db.cursor()
        _ensure_column(c, "grape_t_repo_platforms", "ca_bundle",
                       "TEXT NOT NULL DEFAULT ''", allow_copy=True,
                       strict=True, force=True)
        _db.commit()
        cols = {r[1] for r in c.execute("PRAGMA table_info(grape_t_repo_platforms)").fetchall()}
        if "ca_bundle" not in cols:
            raise RuntimeError("grape_t_repo_platforms.ca_bundle missing after migration")


register_migration(
    version="202611200002",
    name="add-platform-ca-bundle",
    kind="safe",
    domains=("mgmt",),
    fn=_fn_add_platform_ca_bundle,
)


def _fn_create_webhook_inbox_table() -> None:
    """Issue #3580/P3 部署缺口：grape_t_webhook_inbox 只登记在冻结老轨 _NEW_TABLES。

    存量库（已盖 legacy-stamp）启动完全不跑 _create_tables() → 表永不创建 →
    Event 进程 enqueue_webhook 每次 1054/1146 → 通用 webhook 全量 500（事件进不来）。
    与 patrol-findings、merge-gate-log 同因，改走 register_migration；表归 runtime 域
    （入队/消费同在 Event 进程），三库模式取 owner 连接，单库回落进程库。
    """
    from src.kernel.session_manager import db as _db, db_lock as _lock

    conn = _migration_conn("runtime", default=_db)
    with (_lock if conn is _db else nullcontext()):
        c = conn.cursor()
        if not _table_present(c, "grape_t_webhook_inbox"):
            c.execute(_WEBHOOK_INBOX_DDL)
        conn.commit()
        if not _table_present(c, "grape_t_webhook_inbox"):
            raise RuntimeError("grape_t_webhook_inbox missing after migration")


register_migration(
    version="202611200003",
    name="create-webhook-inbox-table",
    kind="safe",
    domains=("runtime",),
    fn=_fn_create_webhook_inbox_table,
)


def _fn_webhook_inbox_last_error_nullable() -> None:
    """Issue #3580 复审：把 last_error 收敛成可空列（MySQL 独有 1364 的存量库修法）。

    建表 DDL 原为 `last_error TEXT NOT NULL DEFAULT ''`：MySQL 不接受 TEXT 字面默认值，
    兼容层剥掉默认值后列变成 NOT NULL 无默认，严格模式里任何不写该列的 INSERT 直接
    1364（实测：webhook 入队每次 500；SQLite 全套测试测不出来）。新装库由修好的 DDL
    直接建成可空 MEDIUMTEXT，本迁移负责把**已盖 202611200003 章的存量库**收敛过来。

    SQLite 侧的字面默认值合法、无需改动（早退即视为已满足）；非 owner 库与跨库视图
    跳过（视图列是 owner 建视图时的快照，不能在本库 ALTER）。
    """
    from src.kernel.session_manager import db as _db, db_lock as _lock

    conn = _migration_conn("runtime", default=_db)
    with (_lock if conn is _db else nullcontext()):
        c = conn.cursor()
        if not _table_present(c, "grape_t_webhook_inbox"):
            return                      # 本库没有该表（非 owner 域）→ 无需收敛
        if not _is_base_table(c, "grape_t_webhook_inbox"):
            return                      # 跨库可更新视图：列快照由 owner 域重建
        if not _mysql_backend():
            return                      # SQLite 的字面默认值合法
        sql_nullable = (
            "SELECT IS_NULLABLE FROM information_schema.columns "
            "WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME='grape_t_webhook_inbox' "
            "AND COLUMN_NAME='last_error'")
        row = c.execute(sql_nullable).fetchone()
        if not row:
            raise RuntimeError("grape_t_webhook_inbox.last_error missing")
        if str(row[0]).upper() == "YES":
            return                      # 已收敛（幂等：二次启动零写入）
        if not _destructive_ddl(
                c, "ALTER TABLE grape_t_webhook_inbox MODIFY last_error MEDIUMTEXT NULL",
                kind_hint="cleanup"):
            raise RuntimeError(
                "webhook inbox last_error 放宽被 cleanup 门槛拦下"
                "（本实例版本落后于 meta.min_code_version）→ 本次不盖章，下版本重试")
        row = c.execute(sql_nullable).fetchone()   # _destructive_ddl 失败只告警，必须复验
        if str((row or [""])[0]).upper() != "YES":
            raise RuntimeError(
                "webhook inbox last_error MODIFY 未生效（仍非空）→ 本次不盖章，下版本重试")
        conn.commit()
        log.info("webhook inbox last_error: TEXT NOT NULL -> MEDIUMTEXT NULL")


register_migration(
    version="202611200004",
    name="webhook-inbox-last-error-nullable",
    kind="safe",
    domains=("runtime",),
    fn=_fn_webhook_inbox_last_error_nullable,
)

register_migration(
    version="202611191120",
    name="create-org-repo-ai-table",
    kind="safe",
    domains=("mgmt",),
    fn=_fn_create_org_repo_ai_table,
)


register_migration(
    version="202611191110",
    name="seed-org-repo-analyzer-skill",
    kind="safe",
    domains=("runtime",),
    fn=_fn_seed_org_repo_analyzer_skill,
)


register_migration(
    version="202611191100",
    name="add-autopilot-execution-params",
    kind="safe",
    domains=("runtime",),
    fn=_fn_add_autopilot_exec_params,
)


register_migration(
    # 2026-11-20（审计 F2）：本迁移**确实跨两域** —— 六张 org 表
    # （grape_t_org_dispatch / _target / _aggregate / grape_t_org_context /
    #   _context_snapshot / _org_repo_map）owner=mgmt，而它后段还要放宽
    # issue.org_context_snapshot_id（issue owner=runtime）。所以不能像审计建议那样
    # 简单收敛成 ("mgmt",)：那会让 runtime 库里的 issue 列永远不被放宽。
    # 真问题是 fn 里对「本域库不存在的表」也硬跑 UuidPkMigration → runtime/insight 每轮 1146、
    # 不盖章、永不收敛。修法：每一跳先探表是否存在，缺表即跳过（见 fn 内 _table_present）。
    version="202611191000",
    name="rename-org-tables-and-uuid-pk",
    kind="safe",
    domains=("mgmt", "runtime"),
    fn=_fn_rename_org_tables_and_uuid_pk,
)


def _audit_ops_event(event_type: str, detail: dict) -> None:
    """运维类 DDL 的审计留痕（QA C3，2026-11-21）。

    为什么需要：`GRAPE_TABLE_COPY_CLEANUP=1` 会在启动期 DROP 副本表，列宽 MODIFY
    也是破坏性 DDL —— 只有一条可能被轮转掉的 warning，事后无法回答「哪次启动、
    哪个进程、动了哪张表、当时多少行」。审计写 mgmt 审计链（哈希链），detail 只记
    结构信息（表名/域/行数/库/角色/迁移号），不含任何数据内容。

    审计失败不阻断 DDL：审计面 vs 可用性是两个口径，这里选择可用性优先并留 warning。
    """
    try:
        from src.domains.mgmt.public import log_audit
        log_audit(event_type, detail=dict(detail or {}))
    except Exception as e:  # noqa: BLE001
        log.warning(f"[audit] {event_type} 写入失败（不影响 DDL）: {e}")


def _fn_session_token_unique_session_key() -> None:
    """session_api_tokens 列宽对齐（幂等）；**不再**去重、**不再**建唯一索引。

    2026-11-21 我曾在此处把「同 key 可多行」收敛成单行（去重 + 建 uniq_session_key），
    用来支撑当时的无条件 upsert。那是倒果为因，已撤销：

      · 原设计允许多枚并存 —— 建表只有 id 主键；索引 ix_sat_kind_key(kind, session_key)
        是非唯一的；session_token_set_expiry / shrink_active / delete_by_session 全是按 key
        批量操作；读侧只有 by_sha，没有「按 key 取一枚」。
      · chat 的 conv 级 key 正依赖这一点（同一会话多条消息各持一枚）。
      · 现在 session_token_insert / rotate 是「裸 INSERT 优先 → 撞唯一键才降级 UPDATE」，
        无唯一索引的库恢复多行，有索引的库兼容单行。

    本迁移只保留 MySQL 侧 TEXT → VARCHAR(191) 的列宽对齐：部署侧**可能**已手工建了
    uniq_session_key（实测 <db_mgmt> / <db_alt> 有），而 MySQL 不允许在
    TEXT 上建唯一索引 —— 列宽不改会与该索引形态冲突。

    已经跑过本迁移并且据此建过 uniq_session_key 的部署，保留现状即可（代码把它当「可能
    存在的物理约束」兼容，不依赖它）。要还原成原设计形态，需人工确认后执行：
        ALTER TABLE session_api_tokens DROP INDEX uniq_session_key
    """
    from src.kernel.session_manager import _is_mysql_backend

    _ddl = "ALTER TABLE session_api_tokens MODIFY session_key VARCHAR(191) NOT NULL"
    _detail = {
        "migration": "202611220005",
        "ddl": _ddl,
        "db": os.environ.get("DEVOPS_DB_NAME", ""),
        "role": os.environ.get("GRAPE_PROCESS_ROLE", ""),
    }
    with db_lock:
        c = db.cursor()
        _ddl_ran = False
        if _is_mysql_backend():
            # QA C3（2026-11-21）：MODIFY COLUMN 属破坏性 DDL，必须走 _destructive_ddl
            # （min_code_version 门槛 + MDL 处理）；门槛未过时跳过不盖章，下次启动重试。
            # 审计前后各一条（detail 只记结构信息）。
            _audit_ops_event("session_token_key_widen", {**_detail, "phase": "before"})
            if not _destructive_ddl(c, _ddl, kind_hint="cleanup", version="202611220005"):
                db.commit()
                raise RuntimeError(
                    "cleanup gate 未过（本实例版本低于 meta.min_code_version），"
                    "session_key 列宽未对齐，跳过不盖章")
            _ddl_ran = True
        db.commit()
    if _ddl_ran:
        _audit_ops_event("session_token_key_widen", {**_detail, "phase": "after"})
    log.info("session_api_tokens: session_key 列宽对齐 VARCHAR(191)（不建唯一索引、不去重）")



register_migration(
    version="202611220006",
    name="personal-orgs-from-accounts",
    kind="safe",
    domains=("mgmt",),
    fn=_fn_personal_orgs_from_accounts,
)


register_migration(
    version="202611220007",
    name="runtime-org-id-backfill",
    kind="safe",
    domains=("runtime",),
    fn=_fn_runtime_org_backfill,
)


def _patrol_scan_indexes_post_check() -> None:
    """Issue #3813 post_check：runtime 两条巡检索引在位（缺=不盖章）。"""
    from src.domains.runtime.migrations.patrol_scan_indexes import (
        verify_patrol_scan_indexes as _impl,
    )
    _impl()


def _patrol_findings_indexes_post_check() -> None:
    """Issue #3813 post_check：mgmt 两条 findings 索引在位（缺=不盖章）。"""
    from src.domains.mgmt.migrations.patrol_findings_indexes import (
        verify_patrol_findings_indexes as _impl,
    )
    _impl()


def _fn_patrol_scan_indexes() -> None:
    """Issue #3813：巡检候选扫描复合索引（events/agent_tasks，runtime 域）。

    列序依据与 EXPLAIN 基线见
    src/domains/runtime/migrations/patrol_scan_indexes.py 模块 docstring。
    """
    from src.domains.runtime.migrations.patrol_scan_indexes import (
        ensure_patrol_scan_indexes as _impl,
    )
    _impl()


def _fn_patrol_findings_indexes() -> None:
    """Issue #3813：findings known-set / TTL 清理索引（grape_t_patrol_findings，mgmt 域）。"""
    from src.domains.mgmt.migrations.patrol_findings_indexes import (
        ensure_patrol_findings_indexes as _impl,
    )
    _impl()


register_migration(
    # Issue #3813 S52：巡检候选扫描的 events/agent_tasks 复合索引。列序依据
    # （实测 EXPLAIN：等值列在前、范围列居中、排序/键集游标列在尾）标注在
    # 迁移模块 docstring；post_check 保证「索引没建成但盖章」不可发生。
    version="202611231001",
    name="patrol-scan-composite-indexes",
    kind="safe",
    domains=("runtime",),
    fn=_fn_patrol_scan_indexes,
    post_check=_patrol_scan_indexes_post_check,
)


register_migration(
    # Issue #3813 S52：findings known-set covering 索引 + TTL 清理定位索引。
    version="202611231002",
    name="patrol-findings-scan-indexes",
    kind="safe",
    domains=("mgmt",),
    fn=_fn_patrol_findings_indexes,
    post_check=_patrol_findings_indexes_post_check,
)


register_migration(
    version="202611220005",
    name="session-token-key-column-width",
    kind="cleanup",  # 含 MODIFY COLUMN：破坏性 DDL，走 min_code_version 门槛
    domains=("mgmt",),
    fn=_fn_session_token_unique_session_key,
)


def _fn_table_ownership_copy_audit() -> None:
    """审计本库里「归别域、却是 BASE TABLE」的表副本（2026-11-21）。

    背景：契约层单点（跨域读只走 owner 域 public），但 DDL 层带历史副本 ——
    非 owner 库里的同名 BASE TABLE 查起来成功返回 0 行、不报错，会被当成
    「没配置/数据丢了」（本仓已误判两次）。这里把副本变成可观测。

    默认**只告警不删**：
      · 非 0 行副本可能是该部署的真源（实测 227：rc 基表在 runtime，清单却声明
        owner=insight），自动删会直接丢数据；
      · 0 行副本虽然安全，清理仍是运维动作，不该在启动里静默发生。
    需要清理时显式开 `GRAPE_TABLE_COPY_CLEANUP=1`，且只删 0 行的那部分。
    """
    from src.kernel import table_ownership as _own

    try:
        row = db.execute("SELECT DATABASE()").fetchone()
        dbname = str((row[0] if row else "") or "")
    except Exception as e:  # noqa: BLE001 - 审计失败不阻断启动（本迁移不盖章也无害）
        log.debug(f"[TABLE_OWNERSHIP_AUDIT] skip: {e}")
        return
    dom = _own._domain_from(dbname)
    if not dom:
        # 判不出域 = 单库部署 / 库名不含域标识 → 不做跨域副本审计（debug 说明，不让空结果冒充干净）
        log.debug(f"[TABLE_OWNERSHIP_AUDIT] skip {dbname or '?'}: 判不出域，不做跨域副本审计")
        return
    try:
        # strict=True：information_schema 不可用 → 抛错而不是返回空表。
        # 空结果只代表「没有任何别域副本」，不能代表「没审计」。
        items = _own.audit_foreign_copies(db, dbname, strict=True)
    except Exception as e:  # noqa: BLE001
        log.warning(f"[TABLE_OWNERSHIP_AUDIT] 无法审计 {dbname}（不等于干净）: {e}")
        raise  # 不盖章，下次启动重试
    if not items:
        # 与上面的 skip 区分：这里是**审过了且干净**，不是没审
        log.info(f"[TABLE_OWNERSHIP_AUDIT] {dbname} (owner={dom}): clean")
        return
    empty = [i for i in items if i["kind"] == "empty_copy"]
    diverge = [i for i in items if i["kind"] == "divergent_copy"]
    unknown = [i for i in items if i["kind"] == "unknown_rows"]
    log.warning(
        f"[TABLE_OWNERSHIP_AUDIT] {dbname}: 别域副本 {len(items)} 张 "
        f"(空 {len(empty)} / 非空 {len(diverge)} / 行数未知 {len(unknown)})；"
        f"裸 SQL 会读到空表或旧数据且不报错，断言数据面请走 owner 域 public 原语")
    for i in diverge:
        log.warning(
            f"[TABLE_OWNERSHIP_AUDIT] `{i['table']}` owner={i['owner']} 本库 {i['rows']} 行 "
            f"→ 非空副本，可能该部署真源在此，需人工裁决后再迁，禁止自动删")
    for i in unknown:
        # 没数过 ≠ 空：不得进入 empty 清理队列（QA B2）
        log.warning(
            f"[TABLE_OWNERSHIP_AUDIT] `{i['table']}` owner={i['owner']} 行数未知 "
            f"→ 不做任何自动处理（先人工确认）")
    if os.environ.get("GRAPE_TABLE_COPY_CLEANUP", "").strip().lower() in ("1", "true", "on"):
        # QA C3：清理前先过破坏性 DDL 门槛一次。门槛未过 → raise，让本迁移不盖章、
        # 下次启动重试（不能在 try/except 里吞掉，否则会带着「已清理」的假象盖章）。
        if not _cleanup_gate_passed():
            raise RuntimeError(
                "cleanup gate 未过（本实例版本低于 meta.min_code_version），"
                "本次不清理副本，跳过不盖章")
        _failed: list[str] = []
        for i in empty:
            _detail = {
                "migration": "202611210005",
                "table": i["table"],
                "owner": i["owner"],
                "rows": i["rows"],
                "db": dbname,
                "role": os.environ.get("GRAPE_PROCESS_ROLE", ""),
            }
            try:
                # QA C3：DROP 走 _destructive_ddl（门槛 + MDL），前后各留一条审计；
                # 门槛拦截时 drop_empty_copy 抛错 → 本迁移不盖章、下次启动重试。
                _audit_ops_event("table_copy_cleanup", {**_detail, "phase": "before"})
                _own.drop_empty_copy(
                    db, i["table"],
                    execute_ddl=lambda ddl: _destructive_ddl(db, ddl, kind_hint="cleanup"))
                _audit_ops_event("table_copy_cleanup", {**_detail, "phase": "after"})
                log.warning(f"[TABLE_COPY_CLEANUP] dropped empty copy `{i['table']}` (owner={i['owner']})")
            except Exception as e:  # noqa: BLE001
                _failed.append(f"{i['table']}: {e}")
                log.warning(f"[TABLE_COPY_CLEANUP] skip `{i['table']}`: {e}")
        if _failed:
            # 有副本没删掉却照常返回 = 带着「已清理」的假象盖章（QA 第 6 轮
            # Informational 5）。这里显式失败 → 不盖章，下次启动重试。
            raise RuntimeError("table copy cleanup incomplete: " + "; ".join(_failed))


register_migration(
    version="202611210005",
    name="table-ownership-copy-audit",
    kind="cleanup",  # 显式开启时含 DROP TABLE：破坏性 DDL，走 min_code_version 门槛
    domains=("mgmt", "runtime", "insight"),
    fn=_fn_table_ownership_copy_audit,
)


# ===== 技能分类切换（统一字典 l1/l2 收口）——2026-11-22 三问题修复 =====
# 背景：全量技能 cat_l1_id=0（挂靠从未落库）+ 老 skill_category 与新字典双轨并存。
# 迁移 SQL 住属主域 migrations 下（跨域守卫 P0-2，QA A8/A11）：
#   mgmt 侧 202611230001：字典补种「grape平台应用」(id 100109) + 老分类停用重申；
#   runtime 侧 202611230002：74 个官方技能按 skill_name 挂 cat_l1_id/cat_l2_id
#   （含组织仓库 AI 分析→测试与质量，2026-11-22 用户拍板）+ 老 category_id 清零。
# 两条均为 kind=backfill + post_check（收敛可判定才盖章）；runtime 条探测前置
# （官方技能空表时不做任何写、不盖章，等首轮 skill sync 后收敛）。

from src.domains.mgmt.migrations.skill_category_cutover_dict import (
    MIGRATION as _M_SKILL_CUTOVER_DICT,
)
register_migration(**_M_SKILL_CUTOVER_DICT)

from src.domains.runtime.migrations.skill_category_cutover import (
    MIGRATION as _M_SKILL_CUTOVER_MAP,
)
register_migration(**_M_SKILL_CUTOVER_MAP)


# ===== Autopilot 环境性失败退避/熔断（Issue #3800）=====
# 材质回写与列名同源于属主域（跨域守卫 P0-2），这里只做登记：
#   mgmt 侧 202611230003：autopilot_tasks 增 5 列（连续失败/错误码/离线起算/退避截止/熔断位）；
#   runtime 侧 202611230004：autopilot_executions 增 error_code（失败分类落库）。
# 两条均 kind=safe + post_check（列存在性可判定才盖章）。version/name/kind 用字面量
# 登记（不用 `**_MIGRATION`），否则 tests/api/test_migration_track_guard.py 的
# 版本号唯一性与格式守卫解析不出该条，会漏看。

from src.domains.mgmt.migrations.autopilot_failure_state import (
    add_failure_state_columns as _fn_autopilot_failure_state_columns,
    verify_failure_state_columns as _post_autopilot_failure_state_columns,
)
register_migration(
    version="202611230003",
    name="autopilot-task-failure-state-columns",
    kind="safe",
    fn=_fn_autopilot_failure_state_columns,
    domains=("mgmt",),
    post_check=_post_autopilot_failure_state_columns,
)

from src.domains.runtime.migrations.autopilot_execution_error_code import (
    add_error_code_column as _fn_autopilot_exec_error_code,
    verify_error_code_column as _post_autopilot_exec_error_code,
)
register_migration(
    version="202611230004",
    name="autopilot-execution-error-code",
    kind="safe",
    fn=_fn_autopilot_exec_error_code,
    domains=("runtime",),
    post_check=_post_autopilot_exec_error_code,
)


# ===== 技能分类字典补种 content-generation（Issue #3798）=====
# 背景：官方技能同步按 sub_scene 解析 skill.category_id，映射源是 mgmt 域
# skill_category（is_active=1）；15 条种子里没有 content-generation，内容生成类
# 技能每轮判未命中、category_id 恒 0（原实现还逐技能刷一条 WARNING）。
# SQL 住属主域 migrations 下（跨域守卫 P0-2），这里只做注册引导。号段核对（提交前）：
# 源码 register_migration 与各目标库 grape_t_schema_migrations 均未占用 202611240012
# （当前最大 202611240011）。字面量登记 version/name/kind，供
# tests/api/test_migration_track_guard.py 解析。

from src.domains.mgmt.migrations.skill_category_content_generation_seed import (
    seed_content_generation_category as _fn_seed_content_generation_category,
    verify_content_generation_category as _post_content_generation_category,
)
register_migration(
    version="202611240012",
    name="skill-category-seed-content-generation",
    kind="backfill",
    fn=_fn_seed_content_generation_category,
    domains=("mgmt",),
    post_check=_post_content_generation_category,
)


# ===== 存量 custom_fields 保留前缀清理（Issue #4066）=====
# 写侧此前不校验 autopilot custom_fields，非法/保留条目长期留库、每次执行刷
# `custom env ... rejected` WARNING。SQL 住属主域 migrations 下（跨域守卫 P0-2）：
#   mgmt 侧 202611240001：清理 autopilot_tasks.custom_fields 与
#   devops_pipeline_scene.advanced_options.custom_fields 的非法条目（幂等 + post_check）。
# 字面量登记 version/name/kind，供 tests/api/test_migration_track_guard.py 解析。

from src.domains.mgmt.migrations.autopilot_custom_fields_cleanup import (
    cleanup_invalid_custom_fields as _fn_autopilot_cf_cleanup,
    verify_custom_fields_clean as _post_autopilot_cf_cleanup,
)
register_migration(
    version="202611240001",
    name="autopilot-custom-fields-cleanup",
    kind="cleanup",
    fn=_fn_autopilot_cf_cleanup,
    domains=("mgmt",),
    post_check=_post_autopilot_cf_cleanup,
)


# ===== Issue #3789：场景节点 skill_id 存量收敛（repo→official 优先）=====
# 只读/回写 devops_pipeline_scene + scene_market（mgmt 域），技能档位查询走
# mgmt.public（skill 在 runtime 域，跨域守卫 P0-2）。幂等；post_check 判定
# 「repo 级场景节点不得引用 user 档技能」收敛后才盖章。
# 版本号取 202611240002（原 202611230003 与 Issue #3800 的 autopilot 失败状态列
# 迁移撞号，合入基线时改号；迁移函数本身幂等，改号后重跑无副作用）。

from src.domains.mgmt.migrations.scene_nodes_skill_id_repo_scope import (
    MIGRATION as _M_SCENE_NODES_SKILL_ID_REPO_SCOPE,
)
register_migration(**_M_SCENE_NODES_SKILL_ID_REPO_SCOPE)
# ===== external_identities 关系补齐（Issue #3897 B 组）=====
from src.domains.mgmt.migrations.external_identities_relation import (
    ensure_identity_relation as _fn_external_identities_relation,
    verify_identity_relation as _post_external_identities_relation,
)
register_migration(
    version="202611230013",
    name="external-identities-relation",
    kind="safe",
    fn=_fn_external_identities_relation,
    domains=("mgmt",),
    post_check=_post_external_identities_relation,
)


# ===== 部署验证缺口修复：新增列曾经只落在冻结老轨（Issue #3731 / #3773）=====
# 背景：`convert_fail_count`（#3731 的转换连败计数）曾登记在
# `mgmt/migrations_ddl.py::ALTERS_USER_FEEDBACK_COLUMNS`；`acp_state_db_*` 4 列
# （#3773 的 ACP state.db 指标）曾登记在 `runtime/migrations_ddl.py::ALTERS_ENGINE_CREDENTIAL_COLUMNS`
# + `_migrate_tables()` 的 `_ensure_column` 段。这两个消费点都在**冻结老轨**：
# 存量库（已盖 legacy-stamp 202610300001）启动时整段不执行 → 列永远不建 →
# 读/写侧 1054 Unknown column（转换读侧首轮即崩；worker 心跳 UPDATE 全断）。
# 修法：两批列都改成注册式迁移（kind=safe + post_check，列存在性可判定才盖章）。
# version/name/kind 用字面量登记（不用 `**_MIGRATION`），否则
# tests/api/test_migration_track_guard.py 的版本号唯一性/格式守卫解析不出该条。

from src.domains.mgmt.migrations.feedback_convert_fail_count import (
    add_convert_fail_count_column as _fn_feedback_convert_fail_count,
    verify_convert_fail_count_column as _post_feedback_convert_fail_count,
)
register_migration(
    version="202611240005",
    name="issue3731-user-feedback-convert-fail-count",
    kind="safe",
    fn=_fn_feedback_convert_fail_count,
    domains=("mgmt",),
    post_check=_post_feedback_convert_fail_count,
)

from src.domains.runtime.migrations.acp_state_db_metrics_columns import (
    add_acp_state_db_metrics_columns as _fn_acp_state_db_metrics_columns,
    verify_acp_state_db_metrics_columns as _post_acp_state_db_metrics_columns,
)
register_migration(
    version="202611240006",
    name="issue3773-acp-state-db-metrics-columns",
    kind="safe",
    fn=_fn_acp_state_db_metrics_columns,
    domains=("runtime",),
    post_check=_post_acp_state_db_metrics_columns,
)

# ===== Issue #3877：events 级联补偿五列（存量 runtime 库仍缺列，1054 持续）=====
# 背景：`cascade_reason` / `report_missing` / `triggered_by` / `cascade_task_status` /
# `cascade_prev_event_status` 此前只登记在
#   · `runtime/schema_ddl.py::_EVENTS_TABLE_DDL`（只服务新装库的 CREATE TABLE）；
#   · `runtime/migrations_ddl.py::ALTERS_SESSION_EVENT_COLUMNS`（只被
#     `_migrate_tables()` 消费，而它属**已冻结的 legacy 轨道**）。
# 已盖 legacy-stamp（202610300001）的存量库启动时老轨整段不执行 → 五列永远不存在 →
# `_cascade_event_terminal()` 的终态收敛 UPDATE 每条都 1054，事件终态不收敛、
# event_active_guard 只能等 TTL（Issue #3877 生产证据：72 小时 54 次）。
# 修法同 #3731/#3773：改成注册式迁移（kind=safe + post_check，五列在才盖章）。
# version/name/kind 用字面量登记（不用 `**_MIGRATION`），否则
# tests/api/test_migration_track_guard.py 的版本号唯一性/格式守卫解析不出该条。
# 202611250001 由本条独占（PR #2157 门禁复审 Critical：同号另有 repo-vcs-health
# / events-payload-mediumtext / payload-guard 三条，已分别 renumber 到
# 202611250005 / 202611250003 / 202611250004）。
from src.domains.runtime.migrations.events_cascade_columns import (
    add_events_cascade_columns as _fn_events_cascade_columns,
    verify_events_cascade_columns as _post_events_cascade_columns,
)
register_migration(
    version="202611250001",
    name="events-cascade-compensation-columns",
    kind="safe",
    fn=_fn_events_cascade_columns,
    domains=("runtime",),
    post_check=_post_events_cascade_columns,
)

# ============================================================================
# Issue #4339：events.payload TEXT(65,535) → MEDIUMTEXT(16,777,215)
#
# 建表 DDL 已改（src/domains/runtime/schema_ddl.py::_EVENTS_TABLE_DDL），存量的
# MySQL 库靠本条收敛：类型探测短路（已 mediumtext 零 DDL）+ _destructive_ddl
# 收口（min_code_version 门槛）+ ALTER 后复检 DATA_TYPE（未生效就不盖章）。
# 迁移 fn/常量/方言分支都在 runtime 域模块（events 归 runtime）。
#
# 版本号 202611250003（PR #2157 门禁复审 Critical）：本条与
# repo-vcs-health-breaker-columns / events-cascade-compensation-columns /
# create-autopilot-payload-guard-table 四条曾共用 202611250001——版本台账以
# version 为键（INSERT OR REPLACE），同号后登记者会被判「已执行」永久跳过。
# 现号段：001=events-cascade-compensation-columns（保留原号）、
# 002=卡片表 payload mediumtext、003=本条、004=payload-guard 建表、
# 005=repo-vcs-health-breaker-columns。
# ============================================================================
from src.domains.runtime.migrations.events_payload_mediumtext import (  # noqa: E402
    migrate_events_payload_mediumtext as _fn_events_payload_mediumtext,
    verify_events_payload_mediumtext as _post_events_payload_mediumtext,
)
register_migration(
    version="202611250003",
    name="issue4339-events-payload-mediumtext",
    kind="safe",
    fn=_fn_events_payload_mediumtext,
    domains=("runtime",),
    post_check=_post_events_payload_mediumtext,
)

# ============================================================================
# Issue #4339 同批审计：grape_t_channel_outbound_card.payload 同为 TEXT
#
# IM 外发卡片体（markdown 卡片）超 64KB 同样会被 MySQL 静默截断，而该表是外发重试
# 的唯一事实源 → 截断后重试发出的是坏卡片。形态与上一条完全一致，表归 mgmt 域。
# ============================================================================
from src.domains.mgmt.migrations.channel_outbound_card_payload_mediumtext import (  # noqa: E402
    migrate_channel_outbound_card_payload_mediumtext as _fn_card_payload_mediumtext,
    verify_channel_outbound_card_payload_mediumtext as _post_card_payload_mediumtext,
)
register_migration(
    version="202611250002",
    name="issue4339-channel-outbound-card-payload-mediumtext",
    kind="safe",
    fn=_fn_card_payload_mediumtext,
    domains=("mgmt",),
    post_check=_post_card_payload_mediumtext,
)

# ============================================================================
# Issue #3740：bot 凭据回填（managed_repos 历史列 → grape_t_repo_credentials）
#
# 根因背景：rc（cred_type='bot'）在生产的 gitcode/github 侧**从未被回填**——此前
# 只注册了 gitee/gitlab 四列的回填迁移（202611150004），而 gitcode/github 的
# bot_*_token 列没有回填入口，于是 RepoBotToken 的 rc 查询恒 miss、100% 走 legacy
# 列回落（8,872 条 WARNING/h）。
#
# 执行权责：生产执行由 sre 环节承接（脚本 + 盖章 + 验证），本模块提供三个幂等原语，
# 不注册自动迁移——回填写生产数据属高风险动作（写错可致全站 token 解析失败），
# 必须走「先 dry-run → 备份 → apply → verify → stamp」的受控流程。
# ============================================================================

#: 回填盖章版本号（未在源码注册 register_migration：由脚本显式盖章，幂等；
#: 与 `grape_t_schema_migrations` 中现有号无冲突，取当日最大号 + 1）
REPO_BOT_BACKFILL_VERSION = "202611220001"
REPO_BOT_BACKFILL_NAME = "backfill-repo-bot-token-columns-to-rc"

#: bot 凭据列（cred_type='bot'）：四个平台一一对应
_REPO_BOT_TOKEN_COLS = ("bot_gitcode_token", "bot_github_token",
                        "bot_gitee_token", "bot_gitlab_token")

#: 全部平台凭据列（bot + webhook，供 `backfill_repo_credentials_from_columns` 用）
_REPO_CRED_COLS = ("bot_gitcode_token", "gitcode_webhook_token",
                   "bot_github_token", "github_webhook_token",
                   "bot_gitee_token", "gitee_webhook_token",
                   "bot_gitlab_token", "gitlab_webhook_token")


def _managed_repos_columns_probe(c) -> "tuple[set[str], str]":
    """managed_repos 列集合探测（sqlite PRAGMA / MySQL information_schema 双方言）。

    返回 ``(columns, error)``：``error == ""`` 表示探测成功（``columns`` 可为空，
    即库确实没有该表/列）；``error != ""`` 表示探测本身失败（驱动/权限/方言异常）。
    二者必须区分——探列失败若被当成「无列」，回填/验证会静默零写入却报
    ``coverage == 1.0``（Issue #2432 同族坑 + #3740 复审 Required）。
    """
    try:
        from src.kernel.session_manager import _is_mysql_backend

        is_mysql = bool(_is_mysql_backend())
    except Exception:  # noqa: BLE001 - 探不到后端就按 sqlite 口径试 PRAGMA
        is_mysql = False
    if is_mysql:
        try:
            rows = c.execute(
                "SELECT COLUMN_NAME FROM information_schema.columns "
                "WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME='managed_repos'").fetchall()
            return {r[0] for r in rows}, ""
        except Exception as e:  # noqa: BLE001 - 探列失败显式上报，由调用方拒绝验证/盖章
            msg = f"managed_repos column probe failed: {e}"
            log.warning(msg)
            return set(), msg
    try:
        return {r[1] for r in c.execute("PRAGMA table_info(managed_repos)").fetchall()}, ""
    except Exception as e:  # noqa: BLE001
        msg = f"managed_repos column probe failed: {e}"
        log.warning(msg)
        return set(), msg


def _managed_repos_columns(c) -> "set[str]":
    """managed_repos 列集合探测（仅返回列名，探列失败按「无列」处理）。

    供既有只读调用方使用；需要区分「探列失败 / 真的无列」的验证与盖章路径
    必须改用 ``_managed_repos_columns_probe``（Issue #3740 复审 Required）。
    """
    cols, _error = _managed_repos_columns_probe(c)
    return cols


def backfill_repo_credentials_from_columns() -> int:
    """managed_repos 平台凭据列 → grape_t_repo_credentials 行（幂等：只填空）。

    D99 归一映射：``bot_*_token`` → ``cred_type='bot'``、``*_webhook_token`` →
    ``'webhook'``；行 id = UUID 主键首写。值按**原样拷贝**（列值与 rc 值同属 N3
    通道加密形态，读侧解密口径一致；历史明文由读侧「明文兼容」覆盖）。

    幂等：``INSERT OR IGNORE``（UNIQUE(repo_key, cred_type) 冲突即忽略），重复执行
    零写入。列存在性守卫（双方言探测）：冷启动库无 gitee/gitlab 列时跳过零写入。

    （Issue #3740：本函数在历史上的 squash 合并中丢失，而 `tests/mgmt/
    test_s80_repo_credentials.py`、`test_s82_dualwrite_removal.py`、
    `test_uc41_rc_schema.py` 一直 import 它——缺失即三处 ERROR。按 b439764fd
    原形恢复，并把列探测改为双方言，避免 MySQL 拓扑静默零写入。）
    """
    from src.kernel.id_gen import uuid7_str

    n = 0
    per_col: dict = {}
    with db_lock:
        c = db.cursor()
        cols = _managed_repos_columns(c)
        if not cols:
            return 0
        for col in _REPO_CRED_COLS:
            if col not in cols:
                per_col[col] = 0      # 冷启动库无此列（F4 后新库只留 gitcode/github）
                continue
            cred_type = "bot" if col.startswith("bot_") else "webhook"
            rows = c.execute(mgmt_mig.managed_repos_select_column(col)).fetchall()
            k = 0
            for repo_key, val in rows:
                cur = c.execute(
                    insight_mig.INSERT_IGNORE_GRAPE_T_REPO_CREDENTIALS,
                    (uuid7_str(), repo_key, cred_type, val or ""))
                k += int(getattr(cur, "rowcount", 0) or 0)
            per_col[col] = k
            n += k
        db.commit()
    if n:
        log.info(f"grape_t_repo_credentials backfill of repo-registry columns: "
                 f"{per_col} (rows filled={n})")
    return n


def _rc_bot_repo_keys() -> "set[str]":
    """rc 中已有非空 bot 行的仓库集合。

    走 insight 域公开读写口（凭据表归 insight），不在 api 层裸读外域表——跨域读
    清单门禁（scripts/p4/scan_cross_domain_reads.py）按裸 SQL 记账，本函数因此
    无新增读点；三库拓扑下由 insight public 的任意拓扑游标决定落哪台库。
    """
    try:
        from src.domains.insight import public as _insight_public

        rows = _insight_public.list_repo_credential_rows()
    except Exception as e:  # noqa: BLE001 - 读失败按「无 rc 行」处理（回填只填空，最坏多插幂等行）
        log.warning(f"rc bot rows read failed: {e}")
        return set()
    return {r[1] for r in rows if len(r) > 4 and r[2] == "bot" and (r[3] or "")}


def backfill_repo_bot_token_columns_to_rc(dry_run: bool = False) -> dict:
    """bot 列专用回填：managed_repos 的 ``bot_*_token`` 四列 → rc ``cred_type='bot'``。

    与 gitee/gitlab 回填同款语义，但覆盖**全部平台**的 bot 列（gitcode/github 正是
    本次生产 100% legacy 回落的来源），并补上：

    - 值归一：列值可能为 N3 密文或历史明文 → ``_decrypt``（明文直通）→ ``_encrypt``
      统一回填为密文（与写路径同构，读侧零特判）；单行解密失败 raw-copy 不阻断整档。
    - 只填空：``INSERT OR IGNORE``，已有 rc 行（可能已轮换）绝不被列值覆盖。
    - dry-run：默认只算计划（``filled`` 恒 0），供 sre 先看影响面再 apply。

    返回 ``{dry_run, scanned, would_fill, filled, per_column, missing, probe_ok, error}``；
    ``missing`` = 持有 legacy bot 值但没有 rc bot 行的仓库（回填后应为空）；
    ``probe_ok=False`` = managed_repos 探列失败（``error`` 带原因），此档不可当作
    「无缺口」消费——脚本必须失败退出（Issue #3740 复审 Required）。
    """
    from src.kernel.id_gen import uuid7_str

    report: dict = {
        "dry_run": bool(dry_run),
        "scanned": 0,          # legacy 非空 (repo, col) 条目数
        "would_fill": 0,       # 计划写入的 rc 行数（按仓库去重）
        "filled": 0,           # 实际写入行数（dry-run 恒 0）
        "per_column": {},
        "missing": [],
        "probe_ok": True,
        "error": "",
    }
    try:
        from src.domains.mgmt.user.db import _decrypt_repo_secret, _encrypt_repo_secret
    except Exception as e:  # noqa: BLE001 - 加解密不可用则不做归一，宁可不动生产数据
        log.warning(f"bot backfill: crypto import failed, skip: {e}")
        return report

    missing: "list[str]" = []
    seen_missing: set = set()
    rc_bot = _rc_bot_repo_keys()
    with db_lock:
        c = db.cursor()
        cols, probe_error = _managed_repos_columns_probe(c)
        if probe_error:
            report["probe_ok"] = False
            report["error"] = probe_error
            return report
        if not cols:
            return report
        for col in _REPO_BOT_TOKEN_COLS:
            filled_col = 0
            report["per_column"][col] = 0
            if col not in cols:
                continue      # 冷启动库无该平台的物理列（F4 终态）
            for repo_key, val in c.execute(mgmt_mig.managed_repos_select_column(col)).fetchall():
                report["scanned"] += 1
                if repo_key in rc_bot:
                    continue  # 已有 rc bot 行 = 权威，回填不覆盖
                first_seen = repo_key not in seen_missing
                if first_seen:
                    seen_missing.add(repo_key)
                    missing.append(repo_key)
                    # (repo_key, 'bot') 一行唯一：计划行数 = 缺口仓库数
                    report["would_fill"] += 1
                if dry_run or not first_seen:
                    # dry-run 只算计划；同仓库第二个 bot 列无新行可写
                    continue
                try:
                    _norm = _encrypt_repo_secret(_decrypt_repo_secret(val or ""))
                except Exception as _de:  # noqa: BLE001
                    # 单行解密失败不阻断整档：保留列值原样回填（读侧明文兼容可读）
                    log.warning(f"bot backfill: decrypt failed for {repo_key}.{col}, "
                                f"raw copy: {_de}")
                    _norm = val or ""
                cur = c.execute(insight_mig.INSERT_IGNORE_GRAPE_T_REPO_CREDENTIALS,
                                (uuid7_str(), repo_key, "bot", _norm))
                wrote = int(getattr(cur, "rowcount", 0) or 0)
                filled_col += wrote
                report["filled"] += wrote
            report["per_column"][col] = filled_col
        if not dry_run:
            db.commit()
        else:
            db.rollback()     # 只读扫描：不留下任何隐性写
    report["missing"] = missing
    if report["filled"]:
        log.info(f"bot credential backfill -> grape_t_repo_credentials: "
                 f"{report['per_column']} (rows filled={report['filled']})")
    return report


def verify_repo_bot_credential_backfill() -> dict:
    """回填验证报告：legacy bot 列与 rc bot 行的覆盖差（回落分支下线前置）。

    返回 ``{legacy_repos, rc_bot_rows, missing, coverage, per_column, probe_ok, error}``：
    - ``legacy_repos``：持有非空 legacy bot 列的仓库数（回填源规模）
    - ``rc_bot_rows``：rc 中非空 bot 行数（回填目标现状）
    - ``missing``：持有 legacy 值但 rc 无 bot 行的仓库（回填缺口）
    - ``coverage``：``1 - len(missing)/legacy_repos``（无 legacy 值时记 1.0）
    - ``probe_ok``：``False`` = managed_repos 探列失败，``coverage`` 强制 0.0——
      **探列失败绝不能伪装成「无缺口、覆盖 1.0」**（Issue #3740 复审 Required）。

    验收口径：回填后 ``missing`` 为空、``coverage == 1.0``；生产下线判据再叠加
    ``grape_repo_bot_legacy_fallback_total`` 窗口增量 == 0 且
    ``repo_bot_hit_legacy_column`` 审计零命中（日志已限频，不再作为判据）。
    """
    report: dict = {"legacy_repos": 0, "rc_bot_rows": 0, "missing": [],
                    "coverage": 1.0, "per_column": {}, "probe_ok": True, "error": ""}
    rc_bot = _rc_bot_repo_keys()
    with db_lock:
        c = db.cursor()
        cols, probe_error = _managed_repos_columns_probe(c)
        if probe_error:
            report["probe_ok"] = False
            report["error"] = probe_error
            report["coverage"] = 0.0
            return report
        if not cols:
            return report
        report["rc_bot_rows"] = len(rc_bot)
        legacy: set = set()
        for col in _REPO_BOT_TOKEN_COLS:
            if col not in cols:
                report["per_column"][col] = 0
                continue
            keys = {r[0] for r in c.execute(mgmt_mig.managed_repos_select_column(col)).fetchall()}
            report["per_column"][col] = len(keys)
            legacy |= keys
        report["legacy_repos"] = len(legacy)
        report["missing"] = sorted(legacy - rc_bot)
    if report["legacy_repos"]:
        report["coverage"] = round(
            1.0 - len(report["missing"]) / float(report["legacy_repos"]), 4)
    return report


def stamp_repo_bot_credential_backfill() -> dict:
    """回填盖章：把 ``REPO_BOT_BACKFILL_VERSION`` 写进 ``grape_t_schema_migrations``。

    回落分支下线 checklist ①「回填迁移全量盖章」的凭证——没有台账行，后续没人能
    证明「存量值确实进过 rc」，删分支就是拿全站 token 解析冒险。幂等：已存在同号
    台账行时不重写（``already=True``），避免覆盖首次执行时间。
    """
    stamped_at = _now_iso()
    with db_lock:
        c = db.cursor()
        existing = c.execute(
            mgmt_mig.SELECT_GRAPE_T_SCHEMA_MIGRATIONS + " WHERE version=?",
            (REPO_BOT_BACKFILL_VERSION,)).fetchone()
        already = existing is not None
        if not already:
            c.execute(
                mgmt_mig.UPSERT_GRAPE_T_SCHEMA_MIGRATIONS,
                (REPO_BOT_BACKFILL_VERSION, REPO_BOT_BACKFILL_NAME, "backfill",
                 stamped_at, 0, _fn_checksum(backfill_repo_bot_token_columns_to_rc)))
        db.commit()
    return {"version": REPO_BOT_BACKFILL_VERSION, "name": REPO_BOT_BACKFILL_NAME,
            "stamped_at": stamped_at, "already": already}

# ===== 引擎后端能力探针 + runtime 熔断（Issue #3928）=====
# agent_runtimes 增 6 列：backend_status/backend_reason/backend_checked_at +
# circuit_open_until/circuit_fail_count/circuit_half_open。新装库由
# runtime schema_ddl 直接带列，本迁移只服务存量库；列在位才盖章。
# 版本号避让：基线上 202611240001（#4066）、202611240002（#3789）、
# 202611240010（#3763 autopilot_executions 补建）、202611240004（#3846 沙箱表）、
# 202611240005（#3731 convert_fail_count）与 202611240006（#3773 ACP 指标列）
# 已占用，本迁移顺延取 202611240008（register_migration 撞号守卫：同号会被判
# 「已执行」永久跳过）。

from src.domains.runtime.migrations.backend_health_columns import (
    add_backend_health_columns as _fn_backend_health_columns,
    verify_backend_health_columns as _post_backend_health_columns,
)
register_migration(
    version="202611240008",
    name="backend-capability-circuit-breaker-columns",
    kind="safe",
    fn=_fn_backend_health_columns,
    domains=("runtime",),
    post_check=_post_backend_health_columns,
)

# ===== Issue #3794: agent_tasks(event_seq, status) 组合索引 =====
# QA #1981 C1：该索引原先写进老轨 `_create_indexes()`，而老轨只在「版本表为空首跑」或
# 「执行器异常兜底」时执行；存量库已盖 legacy-stamp 202610300001，启动时完全不跑，
# 所以 1205 根因之一（NOT EXISTS 复核逐行扫 agent_tasks 热表）在生产实际未消除；
# 三库模式下 runtime 域进程只跑登记迁移（allow_legacy=False），更是永远到不了那段。
# 改注册式迁移后，存量库下次启动即补齐并盖章（幂等：IF NOT EXISTS）。
# 版本号 202611240009：已发布最大号 202611230012，往后 202611240001（#4066）、
# 202611240002（#3789）、202611240003（#3763）、202611240004（#3846 沙箱表）、
# 202611240005（#3731 convert_fail_count）、202611240006（#3773 ACP 指标列）、
# 202611240007（#3733 autopilot-task-dispatch-state）、202611240008（#3928
# backend-capability-circuit-breaker-columns）在基线上均已占用，故本迁移取下一个
# 空闲号 202611240009（同号会被判「已执行」而永久跳过，见
# tests/arch/test_migration_version_integrity.py R1 唯一性守卫）。
def _index_present(c, table: str, index_name: str) -> bool:
    """索引是否已存在（MySQL 查 information_schema，SQLite 查 PRAGMA）。"""
    try:
        if _mysql_backend():
            row = c.execute(
                "SELECT 1 FROM information_schema.statistics "
                "WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME=? AND INDEX_NAME=? LIMIT 1",
                (table, index_name)).fetchone()
            return bool(row)
        return any(r[1] == index_name
                   for r in c.execute(f"PRAGMA index_list('{table}')").fetchall())
    except Exception as e:  # noqa: BLE001 - 判不出存在性按「不存在」处理，DROP 仍会被幂等兜住
        log.debug(f"_index_present({table}.{index_name}) 探测失败: {e}")
        return False


def _fn_add_agent_tasks_event_seq_status_index() -> None:
    """Issue #3794：补 (event_seq, status) 组合索引，并回收左前缀冗余索引。

    SQLite 走 `CREATE INDEX IF NOT EXISTS`；MySQL 不支持该语法（1064），且 `status`
    为 TEXT、建索引必须带前缀长度（否则 1170），故 MySQL 显式写 `status(191)`。
    索引建不出来必须 raise（不盖章，下次启动重试）——吞掉异常会「版本号盖死、
    热表仍逐行扫」，正是本次要修的 1205 根因。
    """
    from src.kernel.session_manager import db as _db, db_lock
    # agent_tasks 属 runtime 域：三库模式下必须在该域连接上执行（mgmt 连接里没有该表）。
    conn = _migration_conn("runtime", default=_db)
    with (db_lock if conn is _db else nullcontext()):
        c = conn.cursor()
        if _mysql_backend():
            # 三库拓扑下迁移走 db_registry._CompatCursor，mysql_compat 里「自动补 (191)
            # 前缀」的特判只挂在它自己的 Cursor 上，这里不生效，必须显式写前缀。
            # 重复索引按 1061 吞（并发/他进程已建，等价幂等）；其余异常上抛不盖章。
            if not _index_present(c, "agent_tasks", "idx_agent_tasks_event_seq_status"):
                try:
                    c.execute(
                        "CREATE INDEX idx_agent_tasks_event_seq_status "
                        "ON agent_tasks(event_seq, status(191))")
                except Exception as e:  # noqa: BLE001
                    if "1061" in str(e) or "already exists" in str(e).lower():
                        log.info("idx_agent_tasks_event_seq_status 已存在（1061），视为建成")
                    else:
                        conn.rollback()
                        raise
        else:
            try:
                c.execute(
                    "CREATE INDEX IF NOT EXISTS idx_agent_tasks_event_seq_status "
                    "ON agent_tasks(event_seq, status)")
            except Exception:
                conn.rollback()
                raise
        # 左前缀冗余索引回收（QA #1981 M3）：组合索引已覆盖 event_seq 单列查询，
        # 保留旧索引只会让热表每次状态回写多维护一棵树。不走 _destructive_ddl 的
        # min_code_version 门槛：旧版本代码只按 event_seq 查询，新组合索引是它的超集，
        # 删除不改任何查询语义，不存在蓝绿共存风险。
        if _index_present(c, "agent_tasks", "idx_agent_tasks_event"):
            try:
                if _mysql_backend():
                    # MySQL 的 DROP INDEX 必须带 ON 子句，SQLite 不允许带。
                    c.execute("DROP INDEX idx_agent_tasks_event ON agent_tasks")
                else:
                    c.execute("DROP INDEX idx_agent_tasks_event")
                log.info("dropped redundant idx_agent_tasks_event（组合索引已覆盖左前缀）")
            except Exception as e:  # noqa: BLE001 - 冗余索引删不掉不该阻断索引补齐
                log.warning(f"drop redundant idx_agent_tasks_event failed: {e}")
        conn.commit()


register_migration(
    version="202611240009",
    name="add-agent-tasks-event-seq-status-index",
    kind="safe",
    domains=("runtime",),
    fn=_fn_add_agent_tasks_event_seq_status_index,
)

def _fn_create_autopilot_payload_guard_table() -> None:
    """Issue #3818：存量库补建 runtime 域表 autopilot_payload_guard。

    autopilot 定时任务反复投递同一份凭据外传载荷（1h 66 次、88+ 轮），调度侧无
    dead-letter / 熔断。台账表归 runtime，由 owner 连接建；CREATE IF NOT EXISTS 幂等，
    存量库重跑零写入（调度器首次使用还有一层惰性建表兜底）。
    """
    if _owned_skip("autopilot_payload_guard", "create table"):
        return
    conn = _migration_conn("runtime")
    with (db_lock if conn is db else nullcontext()):
        c = conn.cursor()
        if not _table_present(c, "autopilot_payload_guard"):
            c.execute(_AUTOPILOT_PAYLOAD_GUARD_DDL)
        conn.commit()


def _postcheck_create_autopilot_payload_guard_table() -> None:
    """post_check：台账表在位才盖章（Issue #3818 / PR #2157 复审 Critical）。

    建表 fn 是「表在则零写」，但执行器以「fn 正常返回 = 盖章」为约定：没有后置
    校验时，守卫跳过 / 撞号顶替的库只会留一行 ERROR，版本号照盖、下次不再重试。
    本迁移曾是 202611250001 的四条同号迁移里**唯一没有 post_check** 的一条，
    不收敛也只能靠日志发现，故补上：表缺失即 raise，不盖章、下次启动重试。
    """
    if _owned_skip("autopilot_payload_guard", "post_check"):
        return
    conn = _migration_conn("runtime")
    cursor = conn.cursor()
    # 不显式关游标（与建表 fn / verify_repo_vcs_health_columns 同口径）：池化连接上
    # close 失败会掩盖校验结论，而 arch 静默 except 棘轮也不允许为此新增吞异常。
    if not _table_present(cursor, "autopilot_payload_guard"):
        raise RuntimeError(
            "autopilot_payload_guard 缺失（建表迁移未生效，不盖章，下次启动重试）")


register_migration(
    # 202611250004（PR #2157 门禁复审 Critical）：原与本条同批三条迁移共用
    # 202611250001，被台账按 version 判「已执行」永久跳过；重新编号后首跑必执行，
    # 且补了 post_check，不收敛会显式不盖章。
    version="202611250004",
    name="create-autopilot-payload-guard-table",
    kind="safe",
    domains=("runtime",),
    fn=_fn_create_autopilot_payload_guard_table,
    post_check=_postcheck_create_autopilot_payload_guard_table,
)

