"""Issue 取消核心逻辑（Issue #2112 抽取）。

原逻辑内联在 ``src/domains/runtime/routes/issues.py::cancel_issue``；下派取消需要复用
同一语义，故抽为可复用函数：终止运行中 Agent 任务 + 停止流水线 + Issue 落 cancelled。
路由与组织下派都调用本函数，禁止保留第二份实现。
"""
from __future__ import annotations

from fastapi import HTTPException

from src.kernel.config import log


def cancel_issue_core(issue_id: int, *, user_id: int | None = None) -> dict:
    """取消 Issue 并终止其运行中任务。

    - ``user_id`` 非空：沿用用户面归属校验（``user_id`` 本人或系统 0 号卡）；
    - ``user_id=None``：调用方已完成权限校验（如组织 owner/admin 取消下派），跳过归属校验。

    返回值语义与原路由一致：``{"ok", "issue_id", "status", "agents_cancelled"}``，
    已取消时返回 ``{"ok": True, "already_cancelled": True}``。
    """
    from src.kernel.owner_db import db_pair

    db, db_lock = db_pair("runtime")

    with db_lock:
        c = db.cursor()
        _sql = "SELECT status, repo_key, issue_number FROM issue WHERE id=?"
        _params: list = [issue_id]
        if user_id is not None:
            _sql += " AND (user_id=? OR user_id=0)"
            _params.append(user_id)
        c.execute(_sql, tuple(_params))
        row = c.fetchone()
        if not row:
            raise HTTPException(status_code=404, detail={"code": 404, "message": "需求不存在"})
        status, repo_key, issue_number = row[0], row[1], row[2]
        issue_key = f"#{issue_number}" if issue_number else ""
        # Issue #1741：终止 agent 与多行收口复用 issue 路由/中心的共享实现，
        # 避免取消端点、评论取消、组织下派取消三处各写一份。
        from src.domains.runtime.routes.issues import _cancel_issue_agents
        from src.api.issues_center import cancel_issue_rows

        if status == "cancelled":
            # Issue #1741：主卡已是 cancelled 时仍对同一需求的历史多行做幂等收口，
            # 防止残留 awaiting_input 行继续出现在待澄清列表；并同步终止其残留 agent_tasks。
            if repo_key and issue_number:
                _cancel_issue_agents(c, repo_key, issue_key)
                cancel_issue_rows(c, repo_key, issue_number)
                db.commit()
            return {"ok": True, "already_cancelled": True}
        if status == "completed":
            raise HTTPException(status_code=400, detail={"code": 400, "message": "已完成需求不可取消"})
        cancelled_agents = _cancel_issue_agents(c, repo_key, issue_key) if issue_key else 0
        # Issue #1741：同一 repo_key+issue_number 的历史多行全量幂等取消，
        # 避免只改单行时同需求其它行仍停留在待澄清列表；已完成/已取消行保持终态不受影响。
        if repo_key and issue_number:
            cancel_issue_rows(c, repo_key, issue_number)
        else:
            c.execute(
                "UPDATE issue SET status='cancelled', completed_reason='user_cancelled', "
                "completed_at=datetime('now') WHERE id=?",
                (issue_id,),
            )
        db.commit()
    log.info(f"Issue #{issue_id} cancelled (agents={cancelled_agents}, via_dispatch={user_id is None})")
    return {"ok": True, "issue_id": issue_id, "status": "cancelled",
            "agents_cancelled": cancelled_agents}