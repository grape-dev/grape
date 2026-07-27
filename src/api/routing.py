"""事件路由 — 从 DB 读取路由规则，三级路由策略"""
from src.session_manager import db, db_lock
from src.config import log


def get_domain_skill(event_type: str, repo_key: str = "",
                     user_id: int = 0) -> str:
    """三级路由：repo_skill_mapping(user) → event_route_config(repo) → event_route_config(global)"""
    # 第一级：用户自定义映射（repo_skill_mapping）
    if user_id:
        try:
            with db_lock:
                c = db.cursor()
                c.execute(
                    "SELECT skill_name FROM repo_skill_mapping WHERE user_id=? AND repo_key=? AND event_type=? AND is_active=1 LIMIT 1",
                    (user_id, repo_key, event_type)
                )
                row = c.fetchone()
                if row:
                    log.info(f"Route: user_mapping -> {row[0]} (user={user_id}, repo={repo_key}, type={event_type})")
                    return row[0]
        except Exception:
            pass

    # 第二级：仓库覆盖配置（event_route_config.repo_key = repo_key）
    if repo_key:
        try:
            with db_lock:
                c = db.cursor()
                c.execute(
                    "SELECT skill FROM event_route_config WHERE repo_key=? AND event_type=? ORDER BY priority DESC LIMIT 1",
                    (repo_key, event_type)
                )
                row = c.fetchone()
                if row:
                    log.info(f"Route: repo_override -> {row[0]} (repo={repo_key}, type={event_type})")
                    return row[0]
        except Exception:
            pass

    # 第三级：全局路由（event_route_config.repo_key = ''）
    try:
        with db_lock:
            c = db.cursor()
            c.execute(
                "SELECT skill FROM event_route_config WHERE repo_key='' AND event_type=? ORDER BY priority DESC LIMIT 1",
                (event_type,)
            )
            row = c.fetchone()
            if row:
                log.info(f"Route: global -> {row[0]} (type={event_type})")
                return row[0]
    except Exception:
        pass

    log.info(f"Route: no match for type={event_type}, repo={repo_key}")
    return ""


def get_agent_skill_map() -> dict[str, str]:
    """获取 {event_type: skill} 全局映射，用于前端展示"""
    result = {}
    try:
        with db_lock:
            c = db.cursor()
            c.execute("SELECT event_type, skill FROM event_route_config WHERE repo_key='' ORDER BY event_type")
            for r in c.fetchall():
                result[r[0]] = r[1]
    except Exception:
        pass
    return result
