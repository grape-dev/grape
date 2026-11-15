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

取数位置：events 归 runtime 域（`docs/weekly-11/table-owner.tsv`），本模块**不留 SQL**
——统一走 runtime public 的任意拓扑原语（自有 DSN → db_registry；单库/测试拓扑 →
进程主库）。原实现是「lib 有 DSN 时用具名查询、否则在本进程主库直发 4 条 events SQL」，
三库拓扑下兜底那 4 条必 1146 并被吞成「全 0」，页面反而显示"没有无头失败"。
"""
from __future__ import annotations

from typing import Any, Dict

from src.kernel.config import log

#: 存量 legacy 标记前缀（backfill 脚本写入；口径只认这个前缀）
LEGACY_PREFIX = "[legacy]"

#: 取数失败时的空结果（字段与成功路径同构，前端不必分支）
_EMPTY = {
    "total_failed": 0, "empty_result": 0, "legacy": 0, "with_diag": 0,
    "empty_ratio": 0.0, "legacy_ratio": 0.0, "diagnosed_ratio": 0.0,
    "samples": [],
}


def failed_result_gap(user: Dict[str, Any], limit: int = 20) -> Dict[str, Any]:
    """跨仓 failed 空 result 占比（调用者可见范围）。

    limit: 最近空 result 样本条数（含真空 + legacy，按 seq 倒序）。
    """
    from src.domains.insight.observability.overview import visible_repo_keys
    from src.domains.runtime.public import (
        failed_event_gap_counts_any,
        failed_event_gap_samples_any,
    )

    repos = visible_repo_keys(user)
    if not repos:
        return dict(_EMPTY)

    try:
        counts = failed_event_gap_counts_any(repos, LEGACY_PREFIX)
        total_failed = int(counts.get("total_failed") or 0)
        # 真空：result 空（不含 legacy 前缀，legacy 有内容但无诊断意义）
        empty_result = int(counts.get("empty_result") or 0)
        # legacy：result 以 [legacy] 开头（存量标记）
        legacy = int(counts.get("legacy") or 0)
        with_diag = max(0, total_failed - empty_result - legacy)

        _rows_fg = failed_event_gap_samples_any(repos, LEGACY_PREFIX, int(limit))
        samples = [
            {
                "seq": r[0], "repo_key": r[1], "event_type": r[2] or "",
                "agent_type": r[3] or "", "issue_id": r[4] or "",
                "result": (r[5] or "")[:200],
                "created_at": r[6] or "",
                "legacy": bool(r[5] and str(r[5]).startswith(LEGACY_PREFIX)),
            }
            for r in _rows_fg
        ]
    except Exception as e:
        log.warning(f"failed_result_gap query failed: {e}")
        return dict(_EMPTY)

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