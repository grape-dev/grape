"""沙箱 DB schema 管理。

独立模块——bootstrap.py 调用 ensure_sandbox_tables() 作为第一步，
但建表逻辑不放在 bootstrap 里，避免职责混淆。

幂等：CREATE TABLE IF NOT EXISTS + INSERT OR IGNORE。
区分两类失败：
- TransientError: DB 连接断开、超时（可重试）
- SchemaError: 表结构冲突、权限不足（不可重试，fail-fast）
"""
from __future__ import annotations

from src.kernel.config import log


class SchemaError(Exception):
    """schema 冲突——不可重试，应 fail-fast。"""


class TransientError(Exception):
    """DB 临时不可用——可重试。"""


def ensure_sandbox_tables() -> None:
    """确保沙箱相关 4 张表存在 + seed 官方域名。

    幂等：重复调用不报错。
    schema 冲突时 raise SchemaError（不可重试）。
    DB 不可用时 raise TransientError（可重试）。
    调用方（bootstrap.py）决定重试/降级/fail-fast。
    """
    from src.kernel.session_manager import db, db_lock

    _DDL = [
        """CREATE TABLE IF NOT EXISTS grape_t_engine_sandbox_paths (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    backend_name  TEXT NOT NULL,
    path          TEXT NOT NULL,
    access        TEXT NOT NULL DEFAULT 'ro',
    created_at    TEXT DEFAULT (datetime('now')),
    updated_at    TEXT DEFAULT (datetime('now')),
    UNIQUE(backend_name, path))""",
        """CREATE TABLE IF NOT EXISTS grape_t_engine_sandbox_config (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    runtime_id    INTEGER NOT NULL,
    engine_name   TEXT NOT NULL,
    sandbox_mode  TEXT NOT NULL DEFAULT 'required',
    degrade_policy TEXT DEFAULT 'compatible',
    created_at    TEXT DEFAULT (datetime('now')),
    updated_at    TEXT DEFAULT (datetime('now')),
    UNIQUE(runtime_id, engine_name))""",
        """CREATE TABLE IF NOT EXISTS grape_t_engine_sandbox_whitelist (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    backend_name  TEXT NOT NULL,
    domain        TEXT NOT NULL,
    method        TEXT NOT NULL CHECK(method IN ('GET','POST','PUT','DELETE','PATCH')),
    path_prefix   TEXT NOT NULL DEFAULT '*',
    scheme        TEXT NOT NULL DEFAULT 'https' CHECK(scheme IN ('http','https')),
    port          INTEGER NOT NULL DEFAULT 443 CHECK(port BETWEEN 1 AND 65535),
    created_at    TEXT DEFAULT (datetime('now')),
    updated_at    TEXT DEFAULT (datetime('now')),
    UNIQUE(backend_name, domain, method, scheme, port))""",
        """CREATE TABLE IF NOT EXISTS grape_t_task_proxy_tokens (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id      TEXT NOT NULL,
    token_hash   TEXT NOT NULL,
    backend_name TEXT NOT NULL,
    created_at   TEXT DEFAULT (datetime('now')),
    expires_at   TEXT,
    UNIQUE(task_id, backend_name))""",
        # ===== DevBox 云沙箱集成：统一云沙箱表 + LLM 凭据表 =====
        """CREATE TABLE IF NOT EXISTS grape_t_cloud_sandboxes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    issue_id VARCHAR(255) NOT NULL,
    repo_key VARCHAR(255) NOT NULL,
    sandbox_id VARCHAR(255) NOT NULL,
    provider VARCHAR(64) NOT NULL,
    provider_id INTEGER DEFAULT 0,
    state VARCHAR(32) DEFAULT 'creating',
    sandbox_url VARCHAR(512) DEFAULT '',
    created_at TEXT,
    last_refresh_at TEXT,
    agent_installed INTEGER DEFAULT 0,
    kill_attempts INTEGER DEFAULT 0,
    task_id VARCHAR(255) DEFAULT '',
    stage VARCHAR(64) DEFAULT '',
    expire_at VARCHAR(32) DEFAULT '',
    attempt INTEGER DEFAULT 0,
    UNIQUE(sandbox_id))""",
        """CREATE TABLE IF NOT EXISTS grape_t_user_llm_credentials (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
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
    UNIQUE(user_id, engine_type))""",
        """CREATE TABLE IF NOT EXISTS grape_t_sandbox_templates (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
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
    updated_by VARCHAR(64) DEFAULT '')""",
    ]

    try:
        with db_lock:
            for ddl in _DDL:
                db.execute(ddl)
            db.commit()
    except Exception as e:
        err_msg = str(e).lower()
        # schema 冲突（权限不足、表结构不对）
        if any(k in err_msg for k in ("permission", "denied", "syntax", "constraint", "already exists")):
            raise SchemaError(f"schema conflict: {e}") from e
        # DB 连接问题（超时、连接断开）
        raise TransientError(f"DB transient error: {e}") from e

    # seed 官方域名（幂等——INSERT OR IGNORE + 表空检查）
    try:
        count = db.execute("SELECT COUNT(*) FROM grape_t_engine_sandbox_whitelist").fetchone()[0]
        if count == 0:
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
            ]
            with db_lock:
                for domain, method, path_prefix, scheme, port in _OFFICIAL_DOMAINS:
                    db.execute(
                        "INSERT OR IGNORE INTO grape_t_engine_sandbox_whitelist "
                        "(backend_name, domain, method, path_prefix, scheme, port) "
                        "VALUES (?, ?, ?, ?, ?, ?)",
                        ("*", domain, method, path_prefix, scheme, port))
                db.commit()
            log.info(f"Seeded grape_t_engine_sandbox_whitelist: {len(_OFFICIAL_DOMAINS)} official domains")
    except (SchemaError, TransientError):
        raise
    except Exception as e:
        raise TransientError(f"seed failed: {e}") from e

