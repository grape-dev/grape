"""事件路由 — 单级路由：从 agent_type.subscribe_events 查找匹配的 Agent"""
import json
from src.session_manager import db, db_lock
from src.config import log


def get_domain_agent(event_type: str) -> str:
    """单级路由：从 agent_type.subscribe_events 查找匹配的 Agent"""
    with db_lock:
        c = db.cursor()
        c.execute(
            "SELECT agent_type FROM agent_type WHERE order_num>0 AND subscribe_events LIKE ?",
            (f'%"{event_type}"%',)
        )
        row = c.fetchone()
        if row:
            log.info(f"Route: {event_type} -> {row[0]}")
            return row[0]
    log.info(f"Route: no match for type={event_type}")
    return ""


def get_agent_skill_map() -> dict[str, str]:
    """获取 {event_type: agent_type} 映射，用于前端展示"""
    result = {}
    try:
        with db_lock:
            c = db.cursor()
            c.execute("SELECT agent_type, subscribe_events FROM agent_type WHERE order_num>0")
            for r in c.fetchall():
                agent_type = r[0]
                try:
                    events = json.loads(r[1])
                    if isinstance(events, list):
                        for evt in events:
                            result[evt] = agent_type
                except (json.JSONDecodeError, TypeError):
                    pass
    except Exception:
        pass
    return result
