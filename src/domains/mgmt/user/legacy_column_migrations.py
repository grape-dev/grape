"""user/db.py init_db 迁移段收敛重构（一次性迁移模块化，2026-11-07）。

背景：init_db 从 810 行到 1040 行共 230 行，混杂 5 类内联迁移
（幂等 ALTER 列、user_accounts 死表收敛、user_id 重映射、孤儿凭据清理、
废弃字段搬迁）——每加一个迁移就在巨函数里追加一段 try/except，历史上已经
长到无法审阅。N1 迁移（credentials_backfill_n1）已示范正确形态：独立模块 +
init_db 一行调用 + 下线=删文件。

本模块收敛原则（与 credentials_backfill_n1 同款生命周期）：
- 每类迁移一个私有函数（单一职责，可独立注释/测试）
- run() 作为唯一入口，init_db 一行调用
- 幂等：每个函数重跑零写入（ALTER 失败=列已存在=pass；UPDATE/DELETE 空命中=no-op）
- 迁移代码的天然归宿是用完即删：哪些可下线、判据是什么，见各函数 docstring

下线判据：
- _add_legacy_columns：所有 ALTER 的列在 _DDL 常量中齐备（新库直接带全）后，
  存量部署渗透完成即可删（观察生产日志无 "added" 输出即零命中）
- _remap_orphan_user_ids / _cleanup_orphan_credentials：D32 迁移遗留兜底，
  生产重映射/清理计数连续为零即可删
- _migrate_user_accounts_deprecated_fields / _drop_user_accounts_deprecated_columns：
  user_accounts 写入冻结收尾（死表收敛），列全部 DROP 完成后整个 user_accounts
  域随表 DROP 一并消失
"""
import json
import os

from src.kernel.session_manager import db
from src.kernel.config import log

# user_accounts 幂等补列族（历史遗留：列族分散在 init_db 各处，此处集中声明）
_USER_ACCOUNTS_LEGACY_COLUMNS = (
    ("nickname", "TEXT DEFAULT ''"),
    ("last_active_at", "TEXT DEFAULT NULL"),
    ("github_id", "TEXT DEFAULT ''"),
    ("github_api_token", "TEXT DEFAULT ''"),
    ("hw_iam_domain", "TEXT DEFAULT ''"),
    ("hw_iam_user", "TEXT DEFAULT ''"),
    ("hw_iam_password", "TEXT DEFAULT ''"),
    ("hw_iam_account_type", "TEXT DEFAULT ''"),
)

_MANAGED_REPOS_LEGACY_COLUMNS = (
    ("bot_github_token", "TEXT DEFAULT ''"),
    ("gitcode_webhook_token", "TEXT DEFAULT ''"),
    ("is_admin", "INTEGER DEFAULT 0"),
    ("bot_gitcode_token", "TEXT DEFAULT ''"),
    ("bot_username", "TEXT DEFAULT ''"),
    ("host_user_id", "INTEGER DEFAULT 0"),          # Issue #372: 用户 Token 降级支持
    ("webhook_secret", "TEXT DEFAULT ''"),
    ("bot_token_status", "TEXT DEFAULT ''"),        # R16.5.1: 过期标记
    ("host_vcs_username", "TEXT DEFAULT ''"),       # R16.5.5: 路径 B 自循环检测
)


def _add_column_if_missing(table: str, col: str, ddl: str) -> None:
    """幂等补列原语（SQLite；ALTER 失败=列已存在=pass）。"""
    try:
        db.execute(f"ALTER TABLE {table} ADD COLUMN {col} {ddl}")
    except Exception:
        pass


def add_legacy_columns() -> None:
    """存量库幂等补列（user_accounts / managed_repos / login_whitelist 列族）。

    新库由 CREATE TABLE DDL 直接带全，此处零命中；存量库补齐后同样零命中。
    下线判据：生产无存量库依赖本路径后整段删除。
    """
    for col, ddl in _USER_ACCOUNTS_LEGACY_COLUMNS:
        _add_column_if_missing("user_accounts", col, ddl)
    for col, ddl in _MANAGED_REPOS_LEGACY_COLUMNS:
        _add_column_if_missing("managed_repos", col, ddl)
    # login_whitelist.github_username（幂等，与上面同族）
    _add_column_if_missing("login_whitelist", "github_username", "TEXT DEFAULT ''")


def remap_orphan_user_ids() -> None:
    """D32 遗留兜底：user_credentials.user_id 旧表 id → grape_accounts 新 id 重映射。

    2026-10-17 修复（Issue #357）：重映射/清理曾锚定旧表 user_accounts——新体系 id
    不在旧表 id 区间被当"悬空行"每次启动批量删除，用户设置页 AK/SK 反复丢失。
    现全部改锚 grape_accounts；users 中间表已不存在，重映射 UPDATE 自然 no-op
    （保留幂等兼容）。下线判据：生产重映射计数连续为零。
    """
    try:
        db.execute("""
            UPDATE user_credentials
            SET user_id = (SELECT ga.id FROM grape_accounts ga, user_accounts ua
                            WHERE ga.username = ua.username AND ua.id = user_credentials.user_id)
            WHERE EXISTS (SELECT 1 FROM user_accounts ua WHERE ua.id = user_credentials.user_id)
              AND NOT EXISTS (SELECT 1 FROM grape_accounts ga WHERE ga.id = user_credentials.user_id)
              AND EXISTS (SELECT 1 FROM grape_accounts ga, user_accounts ua
                          WHERE ga.username = ua.username AND ua.id = user_credentials.user_id)
        """)
        db.commit()
    except Exception:
        pass


def cleanup_orphan_credentials() -> None:
    """D32 遗留兜底：清理 user_id 不在 grape_accounts 的孤儿凭据行。

    P2-A (Issue #357)：MySQL 分批（LIMIT 100 防长事务/deadlock），SQLite 单发。
    下线判据：生产清理计数连续为零（孤儿行只会来自已下线的旧写入路径）。
    """
    try:
        from src.kernel.mysql_compat import get_db_backend
        is_mysql = get_db_backend() == "mysql"
        while True:
            if is_mysql:
                # MySQL: DELETE 不支持子查询 LIMIT，用派生表包装
                db.execute("""
                    DELETE FROM user_credentials WHERE user_id IN (
                        SELECT user_id FROM (
                            SELECT uc.user_id FROM user_credentials uc
                            LEFT JOIN grape_accounts ga ON uc.user_id = ga.id
                            WHERE ga.id IS NULL
                            LIMIT 100
                        ) AS __to_delete
                    )
                """)
            else:
                db.execute("""
                    DELETE FROM user_credentials WHERE user_id NOT IN (SELECT id FROM grape_accounts)
                """)
            db.commit()
            if not is_mysql:
                break
            remaining = db.execute("""
                SELECT COUNT(*) FROM user_credentials uc
                LEFT JOIN grape_accounts ga ON uc.user_id = ga.id
                WHERE ga.id IS NULL
            """).fetchone()[0]
            if remaining == 0:
                break
        # managed_repos 旧 user_id 修复（通过旧 users 表映射；users 已不存在=no-op）
        try:
            db.execute("""
                UPDATE managed_repos SET user_id = (
                    SELECT ua.id FROM user_accounts ua, users u WHERE ua.username = u.username AND u.id = managed_repos.user_id
                ) WHERE EXISTS (SELECT 1 FROM users u WHERE u.id = managed_repos.user_id)
            """)
        except Exception:
            pass
        db.commit()
    except Exception:
        pass


def migrate_user_accounts_deprecated_fields() -> None:
    """user_accounts 死表收敛（1/2）：废弃字段搬迁到正确位置（DROP COLUMN 之前）。

    1. webhook_url → meta 表（全局配置，取首个非空值）
    2. OAuth 配置 → meta 表（default 账户清理时已完成，此处兜底）
    3. bot_usernames → managed_repos.bot_username（Issue #345 按前缀分流）
    下线判据：user_accounts 列全 DROP（见 drop_user_accounts_deprecated_columns）
    且死表 DROP 后，本函数与上一步一并消失。
    """
    try:
        # meta 基表在运行域（runtime）；延迟导入：本模块在 init_db 早期被加载，
        # 模块级 import 运行域 public 会把启动顺序绑死；导入失败由下面既有的
        # except 兜成「迁移零命中」，与原「表不存在即跳过」行为一致。
        from src.domains.runtime import public as _rt  # noqa: PLC0415
        c = db.cursor()
        ua_cols = [r[1] for r in c.execute("PRAGMA table_info(user_accounts)").fetchall()]
        if "webhook_url" in ua_cols:
            c.execute("SELECT webhook_url FROM user_accounts WHERE webhook_url IS NOT NULL AND webhook_url!='' LIMIT 1")
            row = c.fetchone()
            if row and row[0]:
                # meta 基表在运行域：幂等「不存在才写」原语自带单库回落（单库=进程主库，
                # 三库=运行域自有 DSN），计数+写入在 owner 侧一次完成 —— 原「先 COUNT 再
                # INSERT OR REPLACE」的两次跨域往返收成一条语句，也不再依赖 SQLite 专有
                # 的元组式 INSERT OR REPLACE。
                _rt.set_meta_if_absent("webhook_url", row[0])
        if "gitcode_oauth_client_id" in ua_cols:
            c.execute("SELECT gitcode_oauth_client_id, gitcode_oauth_client_secret, gitcode_oauth_redirect_uri FROM user_accounts WHERE gitcode_oauth_client_id IS NOT NULL AND gitcode_oauth_client_id!='' LIMIT 1")
            row = c.fetchone()
            if row:
                for meta_key, val in zip(("oauth_gitcode_client_id", "oauth_gitcode_client_secret", "oauth_gitcode_redirect_uri"), row):
                    if val:
                        _rt.set_meta_if_absent(meta_key, val)
        if "bot_usernames" in ua_cols:
            c.execute("SELECT username, bot_usernames FROM user_accounts WHERE bot_usernames IS NOT NULL AND bot_usernames!=''")
            for uname, raw in c.fetchall():
                try:
                    names = json.loads(raw)
                    if not isinstance(names, list):
                        names = []
                except (json.JSONDecodeError, TypeError):
                    names = [s.strip() for s in raw.split(",") if s.strip()]
                if not names:
                    continue
                bot_name = names[0]
                # Issue #345: github: 仓不填 GitCode bot name（user_accounts.bot_usernames 是 GitCode 的 bot 列表）
                c.execute(
                    "UPDATE managed_repos SET bot_username=? "
                    "WHERE full_name=? AND (bot_username='' OR bot_username IS NULL) "
                    "AND repo_key NOT LIKE 'github:%'",
                    (bot_name, uname))
                c.execute(
                    "UPDATE managed_repos SET bot_username=? "
                    "WHERE (repo_owner=? OR full_name=?) AND (bot_username='' OR bot_username IS NULL) "
                    "AND repo_key NOT LIKE 'github:%'",
                    (bot_name, uname, uname))
            # 兜底：有 bot_gitcode_token 但无 bot_username 时设置默认值
            c.execute("UPDATE managed_repos SET bot_username=COALESCE(NULLIF(bot_username,''), 'devops-bot') WHERE bot_username='' OR bot_username IS NULL")
        db.commit()
    except Exception as e:
        log.warning(f"Migrate user_accounts deprecated fields failed: {e}")


def drop_user_accounts_deprecated_columns() -> None:
    """user_accounts 死表收敛（2/2）：移除废弃列（须在所有读取迁移之后执行）。

    下线判据：与 migrate_user_accounts_deprecated_fields 一并消失（列全 DROP
    后本函数零命中）。
    """
    try:
        c = db.cursor()
        ua_cols = [r[1] for r in c.execute("PRAGMA table_info(user_accounts)").fetchall()]
        for col in ["gitcode_webhook_token", "gitcode_oauth_client_id", "gitcode_oauth_client_secret",
                    "gitcode_oauth_redirect_uri", "webhook_url", "bot_usernames"]:
            if col in ua_cols:
                try:
                    db.execute(f"ALTER TABLE user_accounts DROP COLUMN {col}")
                except Exception:
                    pass
        db.commit()
        log.info("Cleaned up user_accounts deprecated columns (webhook_token/oauth/webhook_url/bot_usernames)")
    except Exception as e:
        log.warning(f"Cleanup user_accounts columns failed: {e}")


def run() -> None:
    """迁移入口（init_db 在 DDL 执行后、_migrate_repo_key_tables 前调用一次）。

    执行顺序敏感：字段搬迁（migrate_user_accounts_deprecated_fields）必须先于
    列删除（drop_user_accounts_deprecated_columns）。
    """
    add_legacy_columns()
    remap_orphan_user_ids()
    cleanup_orphan_credentials()
    migrate_user_accounts_deprecated_fields()
    drop_user_accounts_deprecated_columns()
