"""终态载体单的人工处置（Issue #2816）。

``CONFLICT_SCOPE=intersect``（载体被终态标记且与本单改动面相交）时，自动派发
已被 ``a2a_terminal_guard`` 短路；本模块提供三个可操作出口，替代 B1/B11-fallback
长期空转：

* ``new_carrier``：指定新载体 PR，恢复 processing/stage=coding 后人工派发；
* ``reassign``：改派到指定 agent（人工决定承接方）；
* ``close``：直接收敛关闭（复用 ``confirm_issue_done``）。

处置动作均落 meta 审计，清空终态短路缓存，避免旧信号在 TTL 内继续拦截。
"""
from __future__ import annotations

import json
import time

from src.api.pipeline_constants import DISPATCHABLE_AGENTS, RESUMABLE_ISSUE_STATUSES
from src.kernel.config import log

TERMINAL_ACTIONS = ("new_carrier", "reassign", "close")

# close 可收敛的 issue.status：常规运行态 + 「等用户」暂停态 + 待审核态。
# 与 resume 共用 pipeline_constants 单源，避免 awaiting_input 在此再次硬编码分叉。
_CLOSABLE_ISSUE_STATUSES = RESUMABLE_ISSUE_STATUSES

_CARRIER_META_PREFIX = "terminal-disposition:carrier:"
_OVERRIDE_META_PREFIX = "terminal-disposition:override:"
_LAST_META_PREFIX = "terminal-disposition:last:"
_DISPOSITION_INDEX = "terminal-disposition:index"


def get_carrier_override(repo_key: str, issue_number) -> int:
    """读取人工指定的新载体 PR 号（无则 0）。"""
    try:
        from src.domains.mgmt.user.db import get_meta_value
        raw = get_meta_value(f"{_CARRIER_META_PREFIX}{repo_key}:{issue_number}") or ""
        return int(raw) if str(raw).strip().isdigit() else 0
    except Exception:
        return 0


def set_carrier_override(repo_key: str, issue_number, carrier_pr: int) -> None:
    from src.domains.mgmt.user.db import set_meta_value
    set_meta_value(f"{_CARRIER_META_PREFIX}{repo_key}:{issue_number}", str(int(carrier_pr)))


def clear_carrier_override(repo_key: str, issue_number) -> None:
    """清除人工指定载体（close 后调用，避免重开时旧载体压过文本载体）。

    Issue #2816 复审：close 只清 manual override 时，``terminal-disposition:carrier:*``
    会残留成持久假阴性来源——单据重开后旧载体仍优先于文本载体判定。
    """
    try:
        from src.domains.mgmt.user.db import set_meta_value
        set_meta_value(f"{_CARRIER_META_PREFIX}{repo_key}:{issue_number}", "")
    except Exception as e:
        log.warning(f"terminal-disposition: clear carrier failed {repo_key}/#{issue_number}: {e}")


def set_manual_override(repo_key: str, issue_number, action: str,
                        carrier_pr: int = 0, by: str = "") -> None:
    """记录「人工越过」：处置后持久豁免终态自动短路（Issue #2816 复审）。

    仅覆盖 ``new_carrier`` / ``reassign``（close 本身即终态收敛）。记录载体维度，
    让 guard 在后续非 force 派发中忽略旧的 ``needs-human`` 标签，同时仍以新载体
    的标记行判定，避免处置一次后下一轮又被置回 awaiting_input。
    """
    try:
        from src.domains.mgmt.user.db import set_meta_value
        payload = {"action": action, "carrier_pr": int(carrier_pr or 0),
                   "by": by or "", "at": int(time.time())}
        set_meta_value(f"{_OVERRIDE_META_PREFIX}{repo_key}:{issue_number}",
                       json.dumps(payload, ensure_ascii=False))
    except Exception as e:
        log.warning(f"terminal-disposition: record override failed {repo_key}/#{issue_number}: {e}")


def get_manual_override(repo_key: str, issue_number) -> dict:
    """读取人工越过记录（无则返回空 dict）。坏数据按无记录处理（保守放行）。"""
    try:
        from src.domains.mgmt.user.db import get_meta_value
        raw = get_meta_value(f"{_OVERRIDE_META_PREFIX}{repo_key}:{issue_number}") or ""
        data = json.loads(raw) if raw else {}
        if isinstance(data, dict) and str(data.get("action") or "") in TERMINAL_ACTIONS:
            return data
    except Exception:
        pass
    return {}


def clear_manual_override(repo_key: str, issue_number) -> None:
    try:
        from src.domains.mgmt.user.db import set_meta_value
        set_meta_value(f"{_OVERRIDE_META_PREFIX}{repo_key}:{issue_number}", "")
    except Exception as e:
        log.warning(f"terminal-disposition: clear override failed {repo_key}/#{issue_number}: {e}")


def record_disposition(repo_key: str, issue_number, action: str,
                       detail: dict | None = None) -> None:
    """落处置审计（last 记录 + issue 索引），供排查与页面展示。"""
    try:
        from src.domains.mgmt.user.db import set_meta_value
        payload = dict(detail or {})
        payload.update({"action": action, "at": int(time.time())})
        set_meta_value(f"{_LAST_META_PREFIX}{repo_key}:{issue_number}",
                       json.dumps(payload, ensure_ascii=False))
        set_meta_value(f"{_DISPOSITION_INDEX}:{repo_key}:{issue_number}", action)
    except Exception as e:
        log.warning(f"terminal-disposition: record meta failed {repo_key}/#{issue_number}: {e}")


def validate_disposition(action: str, carrier_pr: int = 0, target_agent: str = "") -> str:
    """校验处置动作；返回空串=合法，否则返回错误文案。"""
    action = str(action or "").strip().lower()
    if action not in TERMINAL_ACTIONS:
        return f"action 必须是 {', '.join(TERMINAL_ACTIONS)}"
    if action == "new_carrier":
        try:
            carrier_pr = int(carrier_pr or 0)
        except (TypeError, ValueError):
            carrier_pr = 0
        if carrier_pr <= 0:
            return "new_carrier 需要 carrier_pr（新载体 PR 编号，正整数）"
    if action == "reassign":
        target_agent = str(target_agent or "").strip().lower()
        if target_agent not in DISPATCHABLE_AGENTS:
            return f"reassign 需要 target_agent（{'/'.join(DISPATCHABLE_AGENTS)}）"
    return ""


def resume_issue(repo_key: str, issue_number, stage: str = "coding",
                 owner_agent: str = "coding") -> bool:
    """把人工态/暂停态恢复为 processing（幂等），供处置后重新派发。

    状态迁移落在 runtime 域 helper（``issues_center.resume_issue_from_manual``），
    api 层不再直写 DB、也不再重复声明状态集合。
    """
    try:
        from src.domains.runtime.issues_center import resume_issue_from_manual
        return resume_issue_from_manual(repo_key, issue_number, stage, owner_agent)
    except Exception as e:
        log.warning(f"terminal-disposition: resume failed {repo_key}/#{issue_number}: {e}")
        return False


def close_issue(repo_key: str, issue_number, confirmed_by: str = "terminal_disposition") -> int:
    """收敛关闭（含 awaiting_input/pending_review），返回受影响行数。

    与用户主路径 ``confirm_issue_done`` 的差异：终态载体单在 guard 短路时已被置为
    ``awaiting_input``，用户主路径只覆盖 active/processing 会落成「blocked 但 stage=done」
    的不一致状态。此处复用同一域内收敛语义，补齐暂停态并仅在真实变更时推进 stage。
    """
    from src.domains.runtime.issues_center import confirm_issue_done
    return confirm_issue_done(
        repo_key, issue_number, confirmed_by=confirmed_by,
        extra_statuses=tuple(s for s in _CLOSABLE_ISSUE_STATUSES
                             if s not in ("active", "processing")),
        skip_stage_if_no_change=True)


def available_actions(state: dict | None = None) -> list[dict]:
    """人工处置入口的可选动作（供页面/CLI 展示）。"""
    state = state or {}
    intersect = bool(state.get("intersect"))
    return [
        {"action": "new_carrier", "label": "指定新载体 PR",
         "requires": "carrier_pr", "recommended": intersect},
        {"action": "reassign", "label": "改派到其它 Agent",
         "requires": "target_agent", "recommended": False},
        {"action": "close", "label": "关闭本单",
         "requires": "", "recommended": not intersect},
    ]