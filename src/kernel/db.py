"""内核 DB schema 管理层——三域分 Schema / SQLite 表前缀（Issue #699 Phase 2）。

设计依据：docs/weekly-09/grape-unified-implementation-design.md §4（分 Schema 设计）。

职责：
1. 表注册表 TABLE_REGISTRY：全部业务表 → 目标 schema（grape_mgmt / grape_runtime / grape_insight）。
2. 表名映射：table()/t_mgmt()/t_runtime()/t_insight()——SQLite 用前缀（mgmt_/rt_/ins_），
   MySQL 用 schema 限定名（grape_mgmt.xxx）。业务代码经此统一封装，对分 Schema 无感。
3. 运行时 SQL 改写 qualify_sql()：把 SQL 中表名语法位置（FROM/JOIN/INTO/UPDATE/TABLE/TO 后）
   的注册表表名自动映射为前缀/schema 限定名；跳过单引号字符串字面量，避免数据值误改写。
4. 灰度开关 schema_mode_enabled()：GRAPE_SCHEMA_MODE=1 时启用改写（SQLite 前缀 / MySQL 限定），
   默认关闭——存量库与全量测试零回归。t_*() 映射遵循开关：模式关闭返回裸表名，
   模式开启返回前缀/限定名（保证 codemod 通道 B 产物在两种模式下均可用）。
5. 迁移辅助：ensure_schemas()（MySQL CREATE SCHEMA IF NOT EXISTS）、
   migrate_sqlite_prefixes()（ALTER TABLE RENAME，保留数据）、migrate_mysql_schemas()（RENAME TABLE）。
6. SQLite 连接代理 SchemaSQLiteDB：execute 入口统一过 qualify_sql（唯一写路径挂接点）。

团队规则（写 SQL 前必读，mysql_compat 头注释）：字符串字面量内禁止反斜杠；本模块不改写
字面量内容，与该规则正交。
"""
from __future__ import annotations

import re

# ===== schema 常量 =====
SCHEMA_MGMT = "grape_mgmt"
SCHEMA_RUNTIME = "grape_runtime"
SCHEMA_INSIGHT = "grape_insight"

SCHEMAS = (SCHEMA_MGMT, SCHEMA_RUNTIME, SCHEMA_INSIGHT)

# SQLite 无 schema：用表前缀模拟（design §4.5）
SQLITE_PREFIX = {
    SCHEMA_MGMT: "mgmt_",
    SCHEMA_RUNTIME: "rt_",
    SCHEMA_INSIGHT: "ins_",
}

# ===== 表注册表（design §4.1~4.3，v2.2）=====
# v2.2 变更（docs/weekly-09/grape-unified-implementation-design.md v2.2）：
#   chat_conversations / chat_messages / chat_agent_session 从 grape_mgmt 迁至
#   grape_runtime（Runtime 写、Mgmt 经 RuntimeService 读）。mgmt 核心 48→45 张；
#   runtime 22→25 张。跨域方向：Runtime 写，Mgmt 通过 RuntimeService.get_chat_*() 读。
_MGMT_TABLES = (
    # 4.1 核心 1-45（v2.2 剔除 chat 三表）
    "grape_accounts", "external_identities", "verification_codes", "signup_whitelist",
    "login_whitelist", "login_failures", "exchange_codes", "binding_tickets",
    "audit_logs", "admin_accounts", "api_tokens", "session_api_tokens",
    "organization", "org_member", "org_invitation", "managed_repos", "user_repos",
    "runtime_credentials", "devops_agent_session", "devops_pipeline_scene", "scene_market",
    "scene_category", "scene_likes", "scene_skill_ref", "scene_engine_detach_log", "skill",
    "skill_category", "skill_likes", "featured_templates", "vod_conversations", "community_posts",
    "community_comments", "community_post_likes", "user_feedback", "uploaded_files",
    "knowledge_write_queue", "feishu_onboard_task", "global_im_bot_config",
    "grape_im_instance", "bot_seen_messages", "repo_im_group_binding", "autopilot_tasks",
    "autopilot_executions", "autopilot_subscriptions", "oauth_states",
    # 4.1 附加/兼容表
    "user_credentials", "outbound_content_ledger",
    "grape_t_credential_reveal_log", "grape_t_schema_migrations", "unified_user_mapping",
)
_RUNTIME_TABLES = (
    # 4.2 1-25（devops_agent_session runtime 视图复用 mgmt 物理表，不重复注册；
    # v2.2：chat 三表归属运行域——Runtime 写、Mgmt 经 RuntimeService 读）
    "events", "event_skipped", "event_active_guard", "event_thread",
    "event_action_filter", "event_route_config", "agent_tasks", "task_messages", "issue",
    "issue_state", "issue_agent_session", "pipeline_instances", "agent_runtimes",
    "engine_capabilities", "agent_type_runtime_pool", "agent_shared_knowledge",
    "scene_knowledge_ref", "grape_t_engine_lifecycle_audit", "agent_type", "backend_agents",
    "distributed_locks", "meta",
    "chat_conversations", "chat_messages", "chat_agent_session",
    "grape_t_skill_version", "grape_t_skill_file", "grape_t_skill_comment",
    # 存量（db_setup DDL）
    "agent_type_config",
)
_INSIGHT_TABLES = (
    # 4.3 1-11
    "obs_log", "obs_metric_rollup", "obs_runtime_samples", "obs_slo", "obs_slo_snapshots",
    "obs_alert_rules", "obs_alert_events", "obs_llm_usage", "obs_llm_usage_archive",
    "api_latency_log", "runtime_health_scores",
)

TABLE_REGISTRY: dict[str, str] = {}
for _t in _MGMT_TABLES:
    TABLE_REGISTRY[_t] = SCHEMA_MGMT
for _t in _RUNTIME_TABLES:
    TABLE_REGISTRY[_t] = SCHEMA_RUNTIME
for _t in _INSIGHT_TABLES:
    TABLE_REGISTRY[_t] = SCHEMA_INSIGHT

# 重复归属校验（模块导入即断言，防注册表笔误）
assert len(TABLE_REGISTRY) == len(_MGMT_TABLES) + len(_RUNTIME_TABLES) + len(_INSIGHT_TABLES)

_MIGRATION_LEDGER = "grape_t_schema_migrations"


def get_db_backend() -> str:
    """当前数据库后端（sqlite/mysql）。复用 mysql_compat 的开关优先级，惰性 import 防 R5 环。"""
    try:
        from src.kernel.mysql_compat import get_db_backend as _g
        return _g()
    except Exception:
        return "sqlite"


def schema_mode_enabled() -> bool:
    """分 Schema 灰度开关：GRAPE_SCHEMA_MODE=1/true/on 开启运行时改写，默认关闭。"""
    import os
    return os.environ.get("GRAPE_SCHEMA_MODE", "").strip().lower() in ("1", "true", "on", "yes")


def table(schema: str, table_name: str) -> str:
    """返回带 schema 前缀/限定的完整表名（design §4.5）。

    遵循 schema_mode_enabled()：模式关闭时返回裸表名（存量库零回归），
    模式开启时返回前缀/限定名。这样 codemod 通道 B 产物（FROM {t_runtime(...)})
    在未开 schema mode 的库上也能正确引用裸表名，灰度降级语义不断裂。
    迁移函数用 _qualified_table() 强制取目标名（不受开关影响）。
    """
    if not schema_mode_enabled():
        return table_name
    return _qualified_table(schema, table_name)


def _qualified_table(schema: str, table_name: str) -> str:
    """强制返回 schema 前缀/限定名（不受 schema_mode 开关影响）。

    供迁移函数（migrate_sqlite_prefixes / migrate_mysql_schemas）使用：
    迁移本身就是启用 schema mode 的过程，需始终取目标前缀名。
    """
    if schema in SQLITE_PREFIX:
        if get_db_backend() == "mysql":
            return f"{schema}.{table_name}"
        return f"{SQLITE_PREFIX[schema]}{table_name}"
    return table_name


def t_mgmt(table_name: str) -> str:
    return table(SCHEMA_MGMT, table_name)


def t_runtime(table_name: str) -> str:
    return table(SCHEMA_RUNTIME, table_name)


def t_insight(table_name: str) -> str:
    return table(SCHEMA_INSIGHT, table_name)


# ===== 运行时 SQL 改写 =====

# 单引号字符串字面量分段（SQLite '' 转义视作字面量内容）
_LITERAL_RE = re.compile(r"'(?:[^']|'')*'")

# 表名语法位置：FROM/JOIN/INTO/UPDATE/TABLE/TO 之后（可带 IF NOT EXISTS）。
# 仅在这些关键字后命中注册表才改写——列名/变量名（如与表同名的 agent_type 列）不改写。
_SQL_KEYWORDS = r"FROM|JOIN|INTO|UPDATE|TABLE|TO"
_TABLE_POS_RE = re.compile(
    r"(?i)\b(" + _SQL_KEYWORDS + r")(\s+)(IF\s+NOT\s+EXISTS\s+)?([`\"\[]?)(\w+)([`\"\]]?)"
)

# PRAGMA table_info(<table>)：SQLite 物理表名发现（须改写为物理前缀名）
_PRAGMA_TABLE_RE = re.compile(r"(?i)^(PRAGMA\s+table_info\s*\(\s*)(\w+)(\s*\)\s*)$")

# SELECT name FROM sqlite_master：返回逻辑表名（剥前缀），保证业务侧
# `if "xxx" in _tables` 这类物理名成员判断在分 Schema 模式下语义不变。
# 注意：必须用 LIKE 'prefix%' 锚定前缀，不能用 REPLACE 子串替换——
# 否则表名内含 rt_/ins_/mgmt_ 子串（如 obs_alert_rules 中的 rt_）会被误剥。
_SQLITE_MASTER_NAMES_RE = re.compile(
    r"(?i)^(\s*SELECT\s+)name(\s+FROM\s+sqlite_master\b)(.*)$"
)
_SQLITE_MASTER_STRIP = (
    "CASE"
    " WHEN name LIKE 'mgmt_%' THEN substr(name, 6)"
    " WHEN name LIKE 'rt_%' THEN substr(name, 4)"
    " WHEN name LIKE 'ins_%' THEN substr(name, 5)"
    " ELSE name END AS name"
)


def _qualify_segment(seg: str) -> str:
    """对不含字符串字面量的 SQL 片段做表名改写。"""
    def _sub(m: "re.Match[str]") -> str:
        kw, ws, ine, pre, name, post = m.groups()
        target = TABLE_REGISTRY.get(name)
        if not target:
            return m.group(0)
        mapped = table(target, name)
        return f"{kw}{ws}{ine or ''}{pre}{mapped}{post}"

    if get_db_backend() == "sqlite":
        # PRAGMA table_info(逻辑名) → 物理前缀名
        pm = _PRAGMA_TABLE_RE.match(seg.strip())
        if pm and pm.group(2) in TABLE_REGISTRY:
            return _PRAGMA_TABLE_RE.sub(
                lambda mm: mm.group(1) + table(TABLE_REGISTRY[mm.group(2)], mm.group(2)) + mm.group(3),
                seg.strip(),
            )
        # sqlite_master 表名清单 → 逻辑名（剥前缀）
        sm = _SQLITE_MASTER_NAMES_RE.match(seg.strip())
        if sm:
            return _SQLITE_MASTER_NAMES_RE.sub(
                lambda mm: mm.group(1) + _SQLITE_MASTER_STRIP + mm.group(2) + mm.group(3),
                seg.strip(),
            )
    return _TABLE_POS_RE.sub(_sub, seg)


def qualify_sql(sql: str) -> str:
    """把 SQL 中表名语法位置的注册表表名映射为前缀/schema 限定名。

    - 未启用 schema mode（GRAPE_SCHEMA_MODE）→ 原样返回（灰度安全）。
    - 跳过单引号字符串字面量内容（数据值如 'events' 不受影响）。
    - 未注册表名（sqlite_master、历史表等）不改写。
    """
    if not sql or not schema_mode_enabled():
        return sql
    out = []
    pos = 0
    for m in _LITERAL_RE.finditer(sql):
        out.append(_qualify_segment(sql[pos:m.start()]))
        out.append(m.group(0))
        pos = m.end()
    out.append(_qualify_segment(sql[pos:]))
    return "".join(out)


# ===== schema 迁移辅助 =====

def ensure_schemas(conn) -> None:
    """MySQL：CREATE SCHEMA IF NOT EXISTS ×3；SQLite：无操作（前缀模拟，无需建 schema）。"""
    if get_db_backend() != "mysql":
        return
    for schema in SCHEMAS:
        conn.execute(f"CREATE SCHEMA IF NOT EXISTS {schema}")
    conn.commit()


def _ledger_rows(conn, backend: str) -> set:
    """已迁移表集合（读 grape_t_schema_migrations；表不存在返回空集）。"""
    ledger = _qualified_table(SCHEMA_MGMT, _MIGRATION_LEDGER) if backend != "identity" else _MIGRATION_LEDGER
    try:
        rows = conn.execute(f"SELECT table_name FROM {ledger}").fetchall()
        return {r[0] for r in rows}
    except Exception:
        return set()


def migrate_sqlite_prefixes(conn, dry_run: bool = False) -> list:
    """SQLite：把注册表内裸表名 RENAME 为前缀表名（保留数据，幂等）。

    返回 [(old, new)] 迁移清单（dry_run=True 只列不执行）。执行后写迁移台账。
    """
    existing = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
    done = _ledger_rows(conn, "sqlite")
    plan = []
    for name, schema in TABLE_REGISTRY.items():
        new = _qualified_table(schema, name)
        if name in existing and new not in existing and new != name and name not in done:
            plan.append((name, new))
    if dry_run:
        return plan
    ledger = _qualified_table(SCHEMA_MGMT, _MIGRATION_LEDGER)
    for old, new in plan:
        conn.execute(f"ALTER TABLE {old} RENAME TO {new}")
    conn.execute(
        f"CREATE TABLE IF NOT EXISTS {ledger} ("
        f"table_name TEXT PRIMARY KEY, target_schema TEXT, migrated_at TEXT "
        f"DEFAULT (datetime('now')))"
    )
    for old, new in plan:
        conn.execute(
            f"INSERT OR REPLACE INTO {ledger} (table_name, target_schema) VALUES (?, ?)",
            (old, TABLE_REGISTRY[old]),
        )
    conn.commit()
    return plan


def migrate_mysql_schemas(conn, dry_run: bool = False) -> list:
    """MySQL：CREATE SCHEMA IF NOT EXISTS ×3 + RENAME TABLE 迁移（幂等）。

    conn 需连到默认库（可跨 schema RENAME 的会话）。返回 [(old, schema)] 清单。
    """
    ensure_schemas(conn)
    existing = set()
    for schema in SCHEMAS:
        for r in conn.execute(
            f"SELECT TABLE_NAME FROM information_schema.tables WHERE TABLE_SCHEMA='{schema}'"
        ).fetchall():
            existing.add(r[0])
    # 当前默认库中的裸表
    try:
        current = {r[0] for r in conn.execute("SHOW TABLES").fetchall()}
    except Exception:
        current = set()
    plan = []
    for name, schema in TABLE_REGISTRY.items():
        if name in current and name not in existing:
            plan.append((name, schema))
    if dry_run:
        return plan
    ledger = _qualified_table(SCHEMA_MGMT, _MIGRATION_LEDGER)
    conn.execute(
        f"CREATE TABLE IF NOT EXISTS {ledger} ("
        f"table_name VARCHAR(190) PRIMARY KEY, target_schema VARCHAR(64), "
        f"migrated_at DATETIME DEFAULT CURRENT_TIMESTAMP)"
    )
    for name, schema in plan:
        conn.execute(f"RENAME TABLE {name} TO {schema}.{name}")
        conn.execute(f"REPLACE INTO {ledger} (table_name, target_schema) VALUES (%s, %s)",
            (name, schema),
        )
    conn.commit()
    return plan


# ===== SQLite 连接代理（唯一写路径挂接点）=====

class SchemaSQLiteCursor:
    """sqlite3.Cursor 代理：execute/executemany 入口过 qualify_sql，其余透传。"""

    def __init__(self, cur):
        object.__setattr__(self, "_cur", cur)

    def execute(self, sql, params=()):
        return SchemaSQLiteCursor(self.__dict__["_cur"].execute(
            qualify_sql(sql), params))

    def executemany(self, sql, seq_of_params):
        return SchemaSQLiteCursor(self.__dict__["_cur"].executemany(
            qualify_sql(sql), seq_of_params))

    def __getattr__(self, name):
        return getattr(self.__dict__["_cur"], name)

    def __setattr__(self, name, value):
        setattr(self.__dict__["_cur"], name, value)

    def __iter__(self):
        return iter(self.__dict__["_cur"])


class SchemaSQLiteDB:
    """sqlite3.Connection 代理：execute 入口统一过 qualify_sql。

    仅在 GRAPE_SCHEMA_MODE=1 时由 session_manager 包装使用；PRAGMA 等透传原样执行
    （qualify_sql 对非注册表语句零改动）。
    """

    def __init__(self, conn):
        object.__setattr__(self, "_conn", conn)

    def cursor(self, *args, **kwargs):
        return SchemaSQLiteCursor(self.__dict__["_conn"].cursor(*args, **kwargs))

    def execute(self, sql, params=()):
        return SchemaSQLiteCursor(self.__dict__["_conn"].execute(
            qualify_sql(sql), params))

    def executemany(self, sql, seq_of_params):
        return SchemaSQLiteCursor(self.__dict__["_conn"].executemany(
            qualify_sql(sql), seq_of_params))

    def executescript(self, sql):
        # executescript 无参数化、按语句拆分执行；逐句改写后执行
        # （sqlite3 原生 executescript 会隐式 commit，这里先 commit 再逐句 execute 保持等价语义）
        conn = self.__dict__["_conn"]
        conn.commit()
        cur = None
        for stmt in _split_script(sql):
            cur = conn.execute(qualify_sql(stmt))
        return cur

    def commit(self):
        self.__dict__["_conn"].commit()

    def rollback(self):
        self.__dict__["_conn"].rollback()

    def close(self):
        self.__dict__["_conn"].close()

    def __getattr__(self, name):
        return getattr(self.__dict__["_conn"], name)

    def __setattr__(self, name, value):
        setattr(self.__dict__["_conn"], name, value)


def _split_script(sql: str) -> list:
    """按分号拆 SQL 脚本为完整语句（保守实现：跳过字符串字面量）。"""
    stmts, buf, in_lit = [], [], False
    i = 0
    while i < len(sql):
        ch = sql[i]
        if ch == "'":
            in_lit = not in_lit
            buf.append(ch)
        elif ch == ";" and not in_lit:
            stmts.append("".join(buf) + ";")
            buf = []
        else:
            buf.append(ch)
        i += 1
    if "".join(buf).strip():
        stmts.append("".join(buf))
    return stmts
