"""Issue #1819: $ACCOUNT 个人账号形态校验与结构化告警公共实现。

集中维护账号形态契约（非空、不含 ``/`` / ``\\`` / 空白字符）与 ACCOUNT_* 告警
字段，供事件管道、任务 env 构建与 autopilot 调度复用，防止多处内联实现漂移。
"""
import json

from src.kernel.config import log


def normalize_account(value) -> str:
    """账号形态校验：合法个人账号为非空、不含 ``/`` / ``\\`` 及空白字符的字符串。"""
    acc = str(value or "").strip()
    if not acc:
        return ""
    if "/" in acc or "\\" in acc or any(ch.isspace() for ch in acc):
        return ""
    return acc


def task_creator_account(user_id) -> str:
    """Issue #1819：定时任务（autopilot）的任务创建者账号，作为其 $ACCOUNT 来源。

    autopilot 没有事件触发人，旧实现直接注入仓库全名。改为按 autopilot_tasks.created_by
    反查 user_accounts.username：查不到（created_by=0 的历史任务 / 用户已删）返回空串，
    由调用方留空并告警。
    语义边界：这是「任务创建者」，不是「本次触发人」，调用方须在 detail 里标明来源。
    """
    try:
        uid = int(user_id or 0)
    except (TypeError, ValueError):
        uid = 0
    if not uid:
        return ""
    try:
        from src.kernel.session_manager import db, db_lock
        with db_lock:
            c = db.cursor()
            from src.kernel.domain_read import mgmt_lib as _mg_lib_e2eb04_f
            _mg_lib_e2eb04 = _mg_lib_e2eb04_f(foreign_only=True)
            if _mg_lib_e2eb04 is not None:
                row = _mg_lib_e2eb04.read_one("user_accounts_e2eb04", (uid,))
            else:
                c.execute("SELECT username FROM user_accounts WHERE id=?", (uid,))
                row = c.fetchone()
        return normalize_account(row[0] if row else "")
    except Exception as e:
        log.warning(f"ACCOUNT_TASK_CREATOR_LOOKUP_FAILED user_id={uid}: {e}")
        return ""


def log_account_alert(kind: str, level: str = "warning", **fields) -> None:
    """输出 ACCOUNT_* 结构化告警（kind 如 ACCOUNT_INVALID / ACCOUNT_MISSING）。

    ``level`` 允许调用方按场景选择日志级别（如 autopilot 常态缺失降为 info），
    字段集合由调用方传入，统一 JSON 序列化口径。
    """
    getattr(log, level)(kind + " " + json.dumps(fields, ensure_ascii=False))
