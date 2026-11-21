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
- user_accounts 收敛（字段搬迁 + 废弃列 DROP）：已下线删除（2026-11-22）。
  死表且废弃列零残留，原实现还存在"用进程主库 db 跑 DDL + 吞异常后打印成功"
  的缺陷；既然迁移恒零命中，修复没有意义，直接删掉
"""
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
    """幂等补列原语（SQLite；ALTER 失败=列已存在=pass）。

    MySQL 下 ALTER 需要 metadata lock：远端实例上只要有一条长事务压着同表，这里就会在
    服务端无限期等待（实测 2026-11-15：insight 启动卡在这一行，进程连监听端口都没开）。
    会话级 `lock_wait_timeout` 压到 5s，拿不到锁就当作「列已存在」跳过，下次启动重试。
    """
    try:
        from src.kernel.session_manager import _is_mysql_backend as _is_mysql
        if _is_mysql():
            try:
                db.execute("SET SESSION lock_wait_timeout=5")
            except Exception:
                pass
        db.execute(f"ALTER TABLE {table} ADD COLUMN {col} {ddl}")
    except Exception as _e:
        # 1205 = lock wait timeout：同表后续列也一定被挡，交给上层 break
        if "1205" in str(_e):
            raise


def add_legacy_columns() -> None:
    """存量库幂等补列（user_accounts / managed_repos / login_whitelist 列族）。

    新库由 CREATE TABLE DDL 直接带全，此处零命中；存量库补齐后同样零命中。

    三库拓扑：这三张都归 mgmt，非 owner 进程（runtime/insight）跑这些 ALTER 既没意义，
    又会在远端库上等 metadata lock 卡死启动；按归属直接跳过。
    下线判据：生产无存量库依赖本路径后整段删除。
    """
    from src.kernel import table_ownership as _own_tbl

    plans = (
        ("user_accounts", _USER_ACCOUNTS_LEGACY_COLUMNS),
        ("managed_repos", _MANAGED_REPOS_LEGACY_COLUMNS),
        ("login_whitelist", (("github_username", "TEXT DEFAULT ''"),)),   # 与上面同族
    )
    for table, cols in plans:
        if _own_tbl.should_skip(table):
            log.info(f"[DDL_SKIP] `{table}` 补列: owner={_own_tbl.owner_of(table)}，非本域进程跳过")
            continue
        for col, ddl in cols:
            try:
                _add_column_if_missing(table, col, ddl)
            except Exception:
                log.warning(f"{table} 补列跳过（等 metadata lock 超时），下次启动重试")
                break


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


def run() -> None:
    """迁移入口（init_db 在 DDL 执行后、_migrate_repo_key_tables 前调用一次）。

    user_accounts 收敛（字段搬迁 + 废弃列 DROP）已整体下线：该域是死表，
    废弃列在存量部署里全部消失（真库 29 列中零残留），两段迁移恒零命中。
    """
    add_legacy_columns()
    remap_orphan_user_ids()
    cleanup_orphan_credentials()
