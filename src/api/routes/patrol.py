"""A2A 链路巡检器只读 API（Phase 1）。

docs/weekly-09/a2a-chain-patrol-design.md §四：GET /api/patrol/findings——
只读清单（unowned 置顶），status 变更（dismissed）为最小人工收敛入口；
POST /api/patrol/scan——手动触发一轮检测（R12/D22，实现后审计补口）。
"""
from __future__ import annotations

import asyncio

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

from src.api.deps import require_no_session_token
from src.api.patrol import PatrolScanInProgress, run_patrol_once
from src.session_manager import db, db_lock

router = APIRouter(prefix="/api/patrol", tags=["patrol"])


@router.get("/findings", operation_id="grape_t_patrol_findings_list",
            summary="Patrol findings list",
            description="Read-only anomaly list from the chain patrol scanner (broken handoff chains, zombie tasks, stalled issues). Unowned findings are pinned to the top.")
def list_findings(status: str = Query("", description="filter by status (open/claiming/compensated/dismissed/false_positive/permanent_failure/needs_human)"),
                  kind: str = Query("", description="filter by kind (broken_chain/zombie_running_signal/...)"),
                  limit: int = Query(100, ge=1, le=500),
                  user: dict = Depends(require_no_session_token)):
    """巡检异常清单（只读 + dismissed 人工收敛；unowned 置顶）。

    Phase 2 自动补偿默认开（meta patrol:compensate_enabled 可紧急关），
    本端点为清单核对唯一入口（S1/验收）。

    归属过滤（S38/R14，全链路审计补口）：admin 全量可见（unowned 置顶，
    管理页口径）；非 admin 仅见 owner=自己 的行——与 dismiss 的 S30 归属
    校验对称（写口子锁了归属，读口子同口径锁），unowned（owner=0）高危项
    仅 admin 可见。读口子的可见面收窄不改变告警路由（R8 归属字段仍在行内）。
    """
    _norm = (lambda v: v if isinstance(v, int) else 0)
    if not isinstance(limit, int) or limit < 1:
        limit = 100
    uid = int(user.get("id") or 0)
    where, params = [], []
    if status:
        where.append("status=?")
        params.append(status)
    if kind:
        where.append("kind=?")
        params.append(kind)
    is_admin = False
    with db_lock:
        try:
            c = db.cursor()
            c.execute("SELECT id FROM admin_accounts WHERE id=?", (uid,))
            is_admin = bool(c.fetchone())
        except Exception:
            is_admin = False    # fail-safe：台账查不了宁可少看（owner 过滤兜底）
    if not is_admin:
        where.append("owner=?")
        params.append(uid)
    sql = ("SELECT id, kind, task_id, event_seq, repo_key, issue_id, owner, detail, "
           "suggestion, status, action_meta, created_at, updated_at FROM grape_t_patrol_findings")
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY (owner=0) DESC, created_at DESC, id DESC LIMIT ?"  # created_at 倒序稳定（id 是 UUID，按 id 排序=随机序）
    params.append(limit)
    with db_lock:
        c = db.cursor()
        try:
            c.execute(sql, tuple(params))
            rows = c.fetchall()
        except Exception as e:
            raise HTTPException(status_code=500, detail={"code": 500, "message": f"读取巡检清单失败: {e}"})
    return {
        "status": "ok",
        "total": len(rows),
        "findings": [
            {"id": r[0], "kind": r[1], "task_id": r[2] or "", "event_seq": r[3] or 0,
             "repo_key": r[4] or "", "issue_id": r[5] or "", "owner": _norm(r[6]),
             "detail": r[7] or "", "suggestion": r[8] or "", "status": r[9] or "open",
             "action_meta": r[10] or "", "created_at": r[11] or "", "updated_at": r[12] or ""}
            for r in rows
        ],
    }


class DismissBody(BaseModel):
    reason: str = ""


@router.post("/findings/{finding_id}/dismiss", operation_id="grape_t_patrol_findings_dismiss",
             summary="Dismiss a patrol finding",
             description="Mark a finding as dismissed (false alarm acknowledged by the repo owner). Ownership enforced: only the finding owner or an admin can dismiss (unowned findings are admin-only). Read-only scan keeps re-detecting, but the unique constraint keeps the dismissed row stable.")
def dismiss_finding(finding_id: str, body: DismissBody = None, user: dict = Depends(require_no_session_token)):
    """人工核对的误报收敛入口（Phase 1 验收：误报=0 硬线的数据支撑）。

    评审十六轮修正②/S30 归属校验：owner 落库的归属人维度必须在收敛入口生效——
    归属人本人（owner==user.id）或 admin 可 dismiss；unowned（owner=0，"无人知晓"
    高危项）仅 admin 可收敛，任意登录用户不能消音他人告警。
    """
    body = body or DismissBody()
    from src.api.patrol import _now_iso
    uid = int(user.get("id") or 0)
    with db_lock:
        c = db.cursor()
        try:
            c.execute("SELECT owner FROM grape_t_patrol_findings WHERE id=?", (finding_id,))
            row = c.fetchone()
            if not row:
                raise HTTPException(status_code=404, detail={"code": 404,
                                                             "message": "finding 不存在"})
            owner = row[0]
            owner = owner if isinstance(owner, int) else int(owner or 0)
            if owner != uid or owner == 0:
                # 非归属人 / unowned：admin 兜底（admin_accounts 台账核对）
                c.execute("SELECT id FROM admin_accounts WHERE id=?", (uid,))
                if not c.fetchone():
                    raise HTTPException(status_code=403, detail={"code": 403,
                                                                 "message": "仅 finding 归属人或管理员可收敛"})
            c.execute(
                "UPDATE grape_t_patrol_findings SET status='dismissed', action_meta=?, updated_at=? "
                "WHERE id=? AND status='open'",
                (body.reason or "dismissed by user", _now_iso(), finding_id))
            db.commit()
            if c.rowcount != 1:
                raise HTTPException(status_code=404, detail={"code": 404,
                                                             "message": "finding 已非 open 状态"})
        except HTTPException:
            raise
        except Exception as e:
            raise HTTPException(status_code=500, detail={"code": 500, "message": f"更新失败: {e}"})
    return {"status": "ok", "id": finding_id, "finding_status": "dismissed"}


@router.post("/scan", operation_id="patrol_scan_run",
             summary="Trigger one patrol scan round manually",
             description="Manually run one detection round (scan-only: detection + findings insert + terminal-findings retention cleanup; NO compensation and NO leader claim — compensation stays on the automatic leader-gated loop). The synchronous scan runs off the event loop via asyncio.to_thread. Single-flight: a scan already in progress returns 409 (the automatic leader-gated loop is the symmetric gate for automatic rounds).")
async def scan_run(user: dict = Depends(require_no_session_token)):
    """手动触发一轮巡检（R12/D22：运营介入面——不等 5 分钟自动周期）。

    刻意只做检测面：不补偿、不抢主——补偿仍走选主门控的自动循环（D3/D20），
    手动轮与自动轮在 findings 唯一约束上幂等汇合（同发现不重插）。
    扫描为持 db_lock 的同步函数，经 asyncio.to_thread 卸载出事件循环（D23）。
    single-flight 闸（S39/R15）：自动轮有选主总闸，手动轮此前无对称闸——
    N 个并发请求=N 个并发全表扫描互踩 db_lock；进程内在跑标志挡第二发，
    异常路径 finally 释放（闸不死锁）。
    """
    global _scan_in_flight
    if _scan_in_flight:
        raise HTTPException(status_code=409, detail={"code": 409,
                                                     "message": "巡检轮已在执行中，请稍后重试"})
    _scan_in_flight = True
    try:
        stats = await asyncio.to_thread(run_patrol_once, "")
    except Exception as e:
        # 全入口闸（S41/R16）：自动轮正在扫描（路由层快检未命中、函数级闸命中）
        if isinstance(e, PatrolScanInProgress):
            raise HTTPException(status_code=409, detail={"code": 409,
                                                         "message": "巡检轮已在执行中，请稍后重试"})
        raise HTTPException(status_code=500, detail={"code": 500,
                                                     "message": f"巡检轮执行失败: {e}"})
    finally:
        _scan_in_flight = False
    return {"status": "ok", "scan": stats}


_scan_in_flight = False
