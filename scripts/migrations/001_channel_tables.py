"""迁移 001_channel（Issue #742 §5.1）—— 渠道 4 表 + 双轨安装回填（§5.3 第 1 步）。

双方言：SQLite（DEVOPS_DB_BACKEND=sqlite，默认）与 MySQL（RDS）均可用——
建表 SQL 写成 SQLite 方言，MySQL 侧经 src.mysql_compat.translate_sql 自动翻译
（? → %s、TEXT 主键 → VARCHAR(191)、datetime('now') → CAST(NOW() AS CHAR) 等）；
唯一的方言分叉是 uk_channel_installation_legacy 表达式索引，见
_ensure_legacy_unique_index。幂等：可重复执行。

表：
  grape_t_channel_installation            渠道安装（统一 chat_bot 用户轨 + vod_bot 仓库轨）
  grape_t_channel_chat_session_binding    会话绑定（合并 repo_im_group_binding + vod_conversations）
  grape_t_channel_inbound_dedup           两阶段去重（bot_seen_messages 双写期保留只读 7 天）
  grape_t_channel_sender_identity         身份绑定（external_identities 的 IM 视图）

回填语义 = load_eligible_bots（feishu_async.py L1520-1617）：
  chat_agent_session(repo_key='', user_id>0, im_app_id!='') → scope='user'（按 app_id 去重）
  devops_agent_session(im_app_id!='')                       → scope='repo'（app 可跨仓库共享 → 按 (channel, repo_key) 一行）
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid


def _config_fingerprint(config: dict) -> str:
    return hashlib.sha256(
        json.dumps(config, sort_keys=True, ensure_ascii=False).encode()
    ).hexdigest()[:16]


def ensure_channel_tables(conn) -> None:
    # executescript 走连接级接口：sqlite3.Connection 与 MySQLCompatDB 都有，
    # 而 cursor 级只有 sqlite3 有（MySQL 兼容层没实现 cursor.executescript）。
    #
    # A2 M3：grape_t_channel_lease 是安装级租约独立表（原租约寄生在 legacy 表 global_im_bot_config）。
    #   lease_key = "<channel_type>:<installation_id>"，行在首次抢锁时惰性创建；
    #   时间列存 Python UTC 字符串（bot_lease._now/_future），不要改成 NOW()：CST/UTC 差 8h
    #   会让锁出生即过期（#869 踩过）；用 VARCHAR 而非 TEXT DEFAULT 字面量，规避
    #   MySQL 8.0.13 前 TEXT 列不允许 DEFAULT 的限制。
    # 注意：**不要在 SQL 字符串里写 -- 注释**。MySQLCompatDB.executescript 按 ";" 切句后
    # 逐句翻译，行首注释会让 CREATE TABLE 不再是句首 → 列级方言翻译（TEXT 主键→VARCHAR(191)）
    # 失效 → MySQL 报 1170 "BLOB/TEXT column used in key specification"。
    conn.executescript(
        """
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
        );

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
        );

        CREATE TABLE IF NOT EXISTS grape_t_channel_inbound_dedup (
            installation_id TEXT NOT NULL,
            message_id      TEXT NOT NULL,
            state           VARCHAR(32) NOT NULL DEFAULT 'claimed' CHECK (state IN ('claimed','marked')),
            claim_token     VARCHAR(64) NOT NULL DEFAULT '',
            created_at      TEXT NOT NULL DEFAULT (datetime('now')),
            PRIMARY KEY (installation_id, message_id)
        );

        CREATE TABLE IF NOT EXISTS grape_t_channel_sender_identity (
            installation_id        TEXT NOT NULL,
            platform_user_id       TEXT NOT NULL,
            platform_stable_user_id TEXT NOT NULL DEFAULT '',
            user_id                TEXT NOT NULL,
            PRIMARY KEY (installation_id, platform_user_id)
        );

        CREATE TABLE IF NOT EXISTS grape_t_channel_outbound_card (
            out_track_id     VARCHAR(191) PRIMARY KEY,
            installation_id  TEXT NOT NULL,
            channel_type     VARCHAR(32) NOT NULL DEFAULT 'dingtalk',
            channel_chat_id  TEXT NOT NULL DEFAULT '',
            chat_type        TEXT NOT NULL DEFAULT 'p2p',
            sender_staff_id  TEXT NOT NULL DEFAULT '',
            card_template_id VARCHAR(191) NOT NULL DEFAULT '',
            created_at       TEXT NOT NULL DEFAULT (datetime('now'))
        );

        CREATE TABLE IF NOT EXISTS grape_t_channel_lease (
            lease_key          TEXT PRIMARY KEY,
            channel_type       VARCHAR(32) NOT NULL DEFAULT '',
            installation_id    TEXT NOT NULL DEFAULT '',
            lease_token        VARCHAR(128) DEFAULT NULL,
            lease_owner        VARCHAR(128) DEFAULT NULL,
            lease_expires_at   VARCHAR(32) DEFAULT NULL,
            lease_acquired_at  VARCHAR(32) DEFAULT NULL,
            updated_at         TEXT NOT NULL DEFAULT (datetime('now'))
        );
        """
    )
    _ensure_legacy_unique_index(conn)
    conn.commit()


def _ensure_legacy_unique_index(conn) -> None:
    """uk_channel_installation_legacy：legacy_app_id+scope+COALESCE(repo_key,owner) 唯一。

    双方言差异（MySQL 适配要点）：
      - SQLite：表达式索引，直接 CREATE UNIQUE INDEX IF NOT EXISTS。
      - MySQL 8.0.13+：函数索引要求键部件写成 (expr)；且不支持 IF NOT EXISTS，
        不能走 mysql_compat 的通用 CREATE INDEX 特判（那里按逗号切列并加反引号，
        会把 COALESCE(NULLIF(repo_key,''), owner_user_id) 拆坏），故显式判存在后直发。
    """
    idx = "uk_channel_installation_legacy"
    if isinstance(conn, sqlite3.Connection):
        conn.execute(
            f"CREATE UNIQUE INDEX IF NOT EXISTS {idx} ON grape_t_channel_installation"
            "(legacy_app_id, scope, COALESCE(NULLIF(repo_key,''), owner_user_id))"
        )
        return
    row = conn.execute(
        "SELECT COUNT(*) FROM information_schema.statistics "
        "WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME='grape_t_channel_installation' "
        f"AND INDEX_NAME='{idx}'"
    ).fetchone()
    if row and row[0]:
        return
    conn.execute(
        f"CREATE UNIQUE INDEX {idx} ON grape_t_channel_installation "
        "(legacy_app_id, scope, (COALESCE(NULLIF(repo_key,''), owner_user_id)))"
    )


# grape_t_channel_installation.channel_type 取值域（与建表 CHECK 一致）
_CHANNEL_TYPES = ("feishu", "dingtalk", "slack", "wecom", "telegram", "wechat")


def _channel_type_of(provider: object, default: str) -> str:
    """legacy 行的 im_provider → channel_type（Issue #1254）。

    回填若恒按 feishu 写，企微/钉钉配置在进程重启后会以 feishu 安装行出现，
    引擎就会用错适配器建连（企微 bot_id 被当飞书 app_id）。已知 provider 一律
    按 provider 落行，未知/空值回落 default（历史脏值语义不变）。
    """
    value = str(provider or "").strip().lower()
    return value if value in _CHANNEL_TYPES else default


def backfill_installations(conn: sqlite3.Connection, channel_type: str = "feishu") -> dict:
    """§5.3 第 1 步：load_eligible_bots 双轨语义 → grape_t_channel_installation 行。

    返回 {'user': n, 'repo': m, 'installations': [(id, app_id, scope, repo_key)...]}
    幂等：按 legacy_app_id+scope+repo 去重，已存在行更新 fingerprint。
    """
    ensure_channel_tables(conn)
    c = conn.cursor()
    results = {"user": 0, "repo": 0, "installations": []}

    # user 轨：chat_agent_session（repo_key='' 且 user_id>0），按 app_id 去重
    try:
        c.execute(
            "SELECT im_app_id, im_app_secret, user_id, COALESCE(im_provider,'') "
            "FROM chat_agent_session "
            "WHERE im_app_id!='' AND im_app_secret!='' AND user_id>0 "
            "  AND (repo_key='' OR repo_key IS NULL)"
        )
        user_rows = c.fetchall()
    except Exception:
        # 旧库可能没有 im_provider 列（ALTER 之前）→ 回落纯 feishu 语义
        c.execute(
            "SELECT im_app_id, im_app_secret, user_id FROM chat_agent_session "
            "WHERE im_app_id!='' AND im_app_secret!='' AND user_id>0 "
            "  AND (repo_key='' OR repo_key IS NULL)"
        )
        user_rows = [(r[0], r[1], r[2], "") for r in c.fetchall()]

    seen_user_apps: set = set()
    for app_id, secret, user_id, provider in user_rows:
        if app_id in seen_user_apps:
            continue  # app_id 去重语义（load_eligible_bots L1570 同款）
        seen_user_apps.add(app_id)
        _upsert_installation(conn, _channel_type_of(provider, channel_type),
                             "user", str(user_id), "",
                             {"app_id": app_id, "app_secret": secret}, app_id,
                             update_existing=False)
        results["user"] += 1
        results["installations"].append((app_id, app_id, "user", ""))

    # repo 轨：devops_agent_session（app 可共享 → 每仓库一行），同样按 im_provider 落 channel_type。
    # 不按 provider 会重演「企微安装重启后被当飞书建连」的错适配器故障。
    try:
        c.execute(
            "SELECT im_app_id, im_app_secret, repo_key, agent_type, COALESCE(im_provider,'') "
            "FROM devops_agent_session "
            "WHERE im_app_id!='' AND im_app_secret!=''"
        )
        repo_rows = c.fetchall()
    except Exception:
        # 旧库可能没有 im_provider 列（ALTER 之前）→ 回落到默认 channel_type 语义
        c.execute(
            "SELECT im_app_id, im_app_secret, repo_key, agent_type FROM devops_agent_session "
            "WHERE im_app_id!='' AND im_app_secret!=''"
        )
        repo_rows = [(r[0], r[1], r[2], r[3], "") for r in c.fetchall()]
    for app_id, secret, repo_key, agent_type, provider in repo_rows:
        if not repo_key:
            continue
        _upsert_installation(conn, _channel_type_of(provider, channel_type),
                             "repo", "", repo_key,
                             {"app_id": app_id, "app_secret": secret}, app_id,
                             update_existing=False)
        results["repo"] += 1
        results["installations"].append((f"{app_id}:{repo_key}", app_id, "repo", repo_key))
    conn.commit()
    return results


def _upsert_installation(conn, channel_type, scope, owner_user_id, repo_key,
                         config: dict, legacy_app_id: str, update_existing: bool = True) -> None:
    """幂等 upsert。

    update_existing=False（启动回填用）：只补建缺失的安装行，绝不覆盖既有行的 config ——
    legacy 列在 A2 收尾后不再随 UI 更新，用它覆盖会把新 secret 打回旧值。
    """
    c = conn.cursor()
    fp = _config_fingerprint(config)
    row = c.execute(
        "SELECT id FROM grape_t_channel_installation WHERE legacy_app_id=? AND scope=? "
        "AND COALESCE(NULLIF(repo_key,''), owner_user_id)=COALESCE(?,?)",
        (legacy_app_id, scope, repo_key or owner_user_id, repo_key or owner_user_id),
    ).fetchone()
    if row:
        inst_id = row[0]
        if not update_existing:
            return
        c.execute(
            "UPDATE grape_t_channel_installation SET config=?, config_fingerprint=?, updated_at=datetime('now') "
            "WHERE id=?",
            (json.dumps(config, ensure_ascii=False), fp, inst_id),
        )
        return
    c.execute(
        "INSERT INTO grape_t_channel_installation "
        "(id, channel_type, scope, owner_user_id, repo_key, config, config_fingerprint, legacy_app_id) "
        "VALUES (?,?,?,?,?,?,?,?)",
        (str(uuid.uuid4()), channel_type, scope, owner_user_id, repo_key,
         json.dumps(config, ensure_ascii=False), fp, legacy_app_id),
    )


def upsert_installation(conn, channel_type, scope, owner_user_id, repo_key,
                        config: dict, legacy_app_id: str) -> None:
    """公开入口（供写穿桥复用）：按 legacy_app_id+scope+COALESCE(repo_key,owner) 幂等 upsert。"""
    _upsert_installation(conn, channel_type, scope, owner_user_id, repo_key,
                         config, legacy_app_id)


def list_installations(conn: sqlite3.Connection) -> list:
    c = conn.cursor()
    c.execute(
        "SELECT id, channel_type, scope, owner_user_id, repo_key, config, "
        "config_fingerprint, status FROM grape_t_channel_installation WHERE status='active'"
    )
    out = []
    for row in c.fetchall():
        out.append({
            "installation_id": row[0], "channel_type": row[1], "scope": row[2],
            "owner_user_id": row[3], "repo_key": row[4],
            "config": json.loads(row[5] or "{}"),
            "config_fingerprint": row[6], "status": row[7],
        })
    return out


if __name__ == "__main__":  # 运维手动执行：python scripts/migrations/001_channel_tables.py
    from src.session_manager import db
    stats = backfill_installations(db)
    print(f"backfill done: user={stats['user']} repo={stats['repo']}")
