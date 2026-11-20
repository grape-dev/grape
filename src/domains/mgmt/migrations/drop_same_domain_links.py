"""删除 oauth_provider == repo_provider 的渠道↔仓库关系行（迁移 202611170033）。

同域不是授权：渠道自己就是平台，运行时 hooks._targets 天然补上自己。这些行是上一版
"只认配置"留下的冗余数据（configs 里也配过同名对）。

表 grape_t_channel_repo_link 归 mgmt，所以这条迁移连同 SQL 一起住在本域 migrations 下；
注册仍由 api 层的引导器做（register_migration），本模块只导出内容。
"""
from __future__ import annotations


def drop_same_domain_links() -> None:
    """删除同域配对关系行（幂等：无行时 DELETE 影响 0 行）。"""
    from src.kernel.config import log
    from src.kernel.session_manager import db, db_lock

    with db_lock:
        c = db.cursor()
        c.execute("SELECT COUNT(*) FROM grape_t_channel_repo_link"
                  " WHERE oauth_provider = repo_provider")
        n = (c.fetchone() or [0])[0]
        if n:
            c.execute("DELETE FROM grape_t_channel_repo_link WHERE oauth_provider = repo_provider")
        db.commit()
    log.info(f"202611170033 清理同域配对关系行 {n} 条（同域由运行时隐式提供）")


MIGRATION = {
    "version": "202611170033",
    "name": "drop-same-domain-channel-links",
    "kind": "backfill",   # 纯数据清理，无 DDL：不受 min_code_version 门槛约束
    "fn": drop_same_domain_links,
    "domains": ("mgmt",),
}
