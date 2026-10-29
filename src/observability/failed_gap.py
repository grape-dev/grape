"""跨仓 "failed 且 result 为空" 占比口径（Issue #476）。

背景：事件列表出现"无法解释的红"——failed 事件 result 为空（无头失败）。
#418/#368/#369 已收口绝大多数失败路径到 _fail_event；webhook agent/handoff
listener 漏改路径由 #476 收口；update_event_status 增加终态 failed 防御回填。
本模块给出**统计口径**，纳入可观测性页面（/api/obs/events/failed-result-gap）：
- 只统计 events 表 status='failed' 的事件
- 空 result：result IS NULL 或 trim 后为空（真空，即"无头失败"）
- legacy：result 以 `[legacy]` 开头（存量一次性标记脚本写入，见
  scripts/backfill_legacy_failed_events.py；口径与增量区分）
- with_diag：其余 failed 事件（有 result，含 `[code]` 前缀或任意可读原因）
- 占比 = empty_result / total_failed（legacy 单列，不混入真空）

边界：与可观测性其余接口一致，只统计调用者可见仓库（visible_repo_keys）。
"""
from __future__ import annotations

from typing import Any, Dict, List

from src.config import log

#: 存量 legacy 标记前缀（backfill 脚本写入；口径只认这个前缀）
LEGACY_PREFIX = "[legacy]"


def failed_result_gap(user: Dict[str, Any], limit: int = 20) -> Dict[str, Any]:
    """跨仓 failed 空 result 占比（调用者可见范围）。

    limit: 最近空 result 样本条数（含真空 + legacy，按 seq 倒序）。
    """
    from src.session_manager import db, db_lock
    from src.observability.overview import visible_repo_keys

    repos = visible_repo_keys(user)
    if not repos:
        return {
            "total_failed": 0, "empty_result": 0, "legacy": 0, "with_diag": 0,
            "empty_ratio": 0.0, "legacy_ratio": 0.0, "diagnosed_ratio": 0.0,
            "samples": [],
        }

    placeholders = ",".join("?" * len(repos))
    with db_lock:
        c = db.cursor()
        try:
            c.execute(
                f"SELECT COUNT(*) FROM events WHERE status='failed' AND repo_key IN ({placeholders})",
                repos)
            total_failed = c.fetchone()[0] or 0

            # 真空：result 空（不含 legacy 前缀，legacy 有内容但无诊断意义）
            c.execute(
                f"SELECT COUNT(*) FROM events WHERE status='failed' AND repo_key IN ({placeholders}) "
                f"AND (result IS NULL OR TRIM(result)='')",
                repos)
            empty_result = c.fetchone()[0] or 0

            # legacy：result 以 [legacy] 开头（存量标记）
            c.execute(
                f"SELECT COUNT(*) FROM events WHERE status='failed' AND repo_key IN ({placeholders}) "
                f"AND result LIKE ?",
                repos + [LEGACY_PREFIX + "%"])
            legacy = c.fetchone()[0] or 0

            with_diag = max(0, total_failed - empty_result - legacy)

            c.execute(
                f"SELECT seq, repo_key, event_type, agent_type, issue_id, result, created_at "
                f"FROM events WHERE status='failed' AND repo_key IN ({placeholders}) "
                f"AND (result IS NULL OR TRIM(result)='' OR result LIKE ?) "
                f"ORDER BY seq DESC LIMIT ?",
                repos + [LEGACY_PREFIX + "%", int(limit)])
            samples = [
                {
                    "seq": r[0], "repo_key": r[1], "event_type": r[2] or "",
                    "agent_type": r[3] or "", "issue_id": r[4] or "",
                    "result": (r[5] or "")[:200],
                    "created_at": r[6] or "",
                    "legacy": bool(r[5] and str(r[5]).startswith(LEGACY_PREFIX)),
                }
                for r in c.fetchall()
            ]
        except Exception as e:
            log.warning(f"failed_result_gap query failed: {e}")
            return {
                "total_failed": 0, "empty_result": 0, "legacy": 0, "with_diag": 0,
                "empty_ratio": 0.0, "legacy_ratio": 0.0, "diagnosed_ratio": 0.0,
                "samples": [],
            }

    def _ratio(n: int) -> float:
        return round(n / total_failed, 4) if total_failed else 0.0

    return {
        "total_failed": total_failed,
        "empty_result": empty_result,
        "legacy": legacy,
        "with_diag": with_diag,
        "empty_ratio": _ratio(empty_result),
        "legacy_ratio": _ratio(legacy),
        "diagnosed_ratio": _ratio(with_diag),
        "samples": samples,
    }