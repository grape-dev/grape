"""Webhook 事件 action 过滤规则（Issue #49）：可配置的 L1 准入判定。

规则存 DB 表 `event_action_filter`（db_setup.py `_EVENT_ACTION_FILTER_DDL`），
run_pipeline L1 命中时调用本模块判定事件是否有效/副作用短路/过滤。

参见 issue-49-webhook-action-filter-fix-design.md §四/§五（L1 层）。
"""
import json
import time as _time

from src.config import log

# 规则缓存（进程内 TTL，避免每次事件都查库）
_RULES = None
_RULES_TS = 0.0
_RULES_TTL = 30.0  # 30s 内复用缓存，热生效延迟 ≤30s


def _load_from_db():
    """从 event_action_filter 表加载启用规则为 {event_type: {action: rule}}。"""
    from src.session_manager import db, db_lock
    rules = {}
    try:
        with db_lock:
            c = db.cursor()
            c.execute(
                "SELECT event_type, action, allow, actor, require_changes, handle_side_effect "
                "FROM event_action_filter WHERE enabled=1 ORDER BY priority, id")
            for r in c.fetchall():
                evt, act, allow, actor, req, se = r[0], r[1], r[2], r[3] or "any", r[4] or "", r[5] or ""
                _req_list = []
                if req:
                    try:
                        _req_list = json.loads(req)
                    except Exception:
                        _req_list = [req]
                rules.setdefault(evt, {})[act] = {
                    "allow": allow,
                    "actor": actor,
                    "require_changes": _req_list,
                    "handle_side_effect": se,
                }
    except Exception as e:
        log.warning(f"event_action_filter load failed: {e}")
    return rules


def load_event_action_rule(force=False) -> dict:
    """返回 {event_type: {action: rule}}，带 TTL 缓存。"""
    global _RULES, _RULES_TS
    now = _time.time()
    if force or _RULES is None or (now - _RULES_TS) > _RULES_TTL:
        _RULES = _load_from_db()
        _RULES_TS = now
    return _RULES or {}


def _get_actor(payload: dict) -> str:
    """从 payload 判断操作者类型：user / bot / unknown。"""
    from src.user.db import get_all_bot_usernames
    # 兼容 vet 归一化：顶层 user、user_name、sender
    actor_name = ""
    for key in ("user_name", "actor"):
        if payload.get(key):
            actor_name = str(payload[key])
            break
    if not actor_name:
        u = payload.get("user") or {}
        if isinstance(u, dict):
            actor_name = str(u.get("username") or u.get("login") or "")
    if not actor_name:
        s = payload.get("sender") or {}
        if isinstance(s, dict):
            actor_name = str(s.get("username") or s.get("login") or "")
    if not actor_name:
        return "unknown"
    bots = set(get_all_bot_usernames())
    return "bot" if actor_name in bots else "user"


def evaluate_event_action(event_type: str, payload: dict) -> dict:
    """L1 准入判定：返回 {decision, reason, actor_type, side_effect}。

    decision:
      - "allow"      → 放行进入阶段路由
      - "skip"       → 过滤（写 event_skipped）
      - "side_effect"→ 执行副作用后短路（不进入 agent）
      - "no_rule"    → 无匹配规则，按放行处理（保守：不因缺规则误杀）
    """
    rules = load_event_action_rule()
    evt_rules = rules.get(event_type) or {}
    action_obj = (payload.get("object_attributes") or {}) if isinstance(payload, dict) else {}
    action = str(action_obj.get("action") or "").lower() if isinstance(action_obj, dict) else ""
    if not action:
        # 无 action（如 issue_comment/note_hook_pr 无 object_attributes.action）→ 规则缺失时放行
        return {"decision": "no_rule", "reason": "no_action", "actor_type": _get_actor(payload), "side_effect": ""}

    rule = evt_rules.get(action)
    if rule is None:
        return {"decision": "no_rule", "reason": f"no_rule:{action}",
                "actor_type": _get_actor(payload), "side_effect": ""}

    # actor 细分：规则限定 user/bot 时校验
    actor_type = _get_actor(payload)
    if rule["actor"] and rule["actor"] != "any" and actor_type != rule["actor"]:
        return {"decision": "skip", "reason": f"actor_mismatch:{actor_type}!={rule['actor']}",
                "actor_type": actor_type, "side_effect": rule["handle_side_effect"]}

    # require_changes：edit 等需 changes 命中指定键
    if rule["require_changes"]:
        changes = payload.get("changes") or {}
        if not isinstance(changes, dict) or not all(k in changes for k in rule["require_changes"]):
            return {"decision": "skip", "reason": f"changes_not_met:{action}",
                    "actor_type": actor_type, "side_effect": rule["handle_side_effect"]}

    if rule["allow"] == 2:
        return {"decision": "side_effect", "reason": f"side_effect:{action}",
                "actor_type": actor_type, "side_effect": rule["handle_side_effect"]}
    if rule["allow"] == 1:
        return {"decision": "allow", "reason": f"allow:{action}",
                "actor_type": actor_type, "side_effect": rule["handle_side_effect"]}
    return {"decision": "skip", "reason": f"action_disallowed:{action}",
            "actor_type": actor_type, "side_effect": rule["handle_side_effect"]}