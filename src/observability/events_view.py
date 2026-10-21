"""统一事件视图（P1-4 / §6.9）—— 把多来源事件规范成同一组字段，供用户面与管理台消费。

设计要点（观测云"事件中心只聚合、不产生"的思路，但**不新建表**）：

| 来源 `source` | 物理表 | 说明 |
|---|---|---|
| `monitor` | `obs_alert_events` | 指标阈值触发（含 `kind='metric'`） |
| `gap` | `obs_alert_events` | 数据缺失（P1-3，`kind='gap'`） |
| `mutation` | `obs_alert_events` | 环比突变（P1-3） |
| `task` | `agent_tasks` | 卡住 / 失败任务（复用 `dashboard.get_ops_overview` 的口径） |
| `change` | `events` | 变更（PR 合并；P2-3 再扩展部署等） |
| `audit` | `audit_logs` | **仅 L2**（平台动作，不进用户面） |

统一字段：`source / kind / level / title / repo_key / issue_id / task_id / trace_id /
started_at / duration_sec / ongoing / state / value / threshold / next_step / key`

口径与取舍：
- **不重新实现可见性**：告警来源直接用 `alerts.list_events`（它已处理"可见仓库 ∪ 可见设备"）；
  任务来源复用 `dashboard.get_ops_overview` 的 `tasks`（同一套 repo/org 过滤）；变更来源按可见仓库过滤
- **首次发生与持续时长**：告警 `first_fired_at`（数据缺失事件里它就是"静默起点"），
  任务 `started_at`，变更 `started_at`；`duration_sec` = 结束时间 − 开始时间（未结束 → 用当前时间，`ongoing=true`）
- **`next_step`（处置建议）**：给前端做"下一步看哪里"的动作链接（时间线 / 日志 / 需求 / 设备），
  按 `kind` 映射；不写死在文案里，避免和后端文案耦合
- 变更/审计事件是**瞬时事件**（`duration_sec` 可能为 0）
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Set

from src.api.time_utils import normalize_event_time
from src.config import log

#: 用户面可用来源（`audit` 只在 L2）
#: `slo` = SLO 违约（P2-1）：仓库范围的对用户可见（level='user'），平台范围的是 level='platform'
#: → `alerts.list_events` 只出 user 级，故 L1 天然看不到平台 SLO 违约，无需额外过滤。
L1_SOURCES = ("monitor", "gap", "mutation", "slo", "task", "change")
L2_SOURCES = L1_SOURCES + ("audit",)

#: 处置建议：kind → 下一步动作（前端渲染成跳转）
_NEXT_STEP = {
    "runtime_silent": "device",
    "repo_silent": "timeline",
    "task_stuck": "timeline",
    "task_failed": "timeline",
    "task_failure_rate": "timeline",
    "task_success_rate_drop": "timeline",
    "task_queue_backlog": "logs",
    "slo_breached": "logs",
    "change": "issue",
}


#: 关键字检索上限（防畸形输入）。注意：SQL 里一律 `LIKE ?` + 参数绑定，**不把关键字拼进 SQL 文本**
#: —— 本轮踩过"SQL 文本含字面 `%` 又带参数"的地雷（pymysql 当格式符 → 异常被 try 吞掉 → 静默丢数据）。
Q_MAX = 64


def norm_q(q: Any) -> str:
    """规范关键字：去空白 + 截断。用户输入的 `%`/`_` 与日志检索（`logs.search`）一致，
    直接当通配符用（不做转义）——两端行为一致比"更严格"更重要。"""
    return str(q or "").strip()[:Q_MAX]


def _like(v: str) -> str:
    return f"%{v}%"


#: 事件状态全集（`state` 过滤允许值）。旧值 firing/resolved 保持兼容并带别名（见 `_STATE_ALIAS`）。
STATES = ("firing", "resolved", "stuck", "failed", "done", "cancelled", "completed")
#: 旧别名：前端历史上用 firing/resolved 同时指代任务侧的 stuck/failed
_STATE_ALIAS = {"firing": ("firing", "stuck"), "resolved": ("resolved", "failed")}
#: 点事件来源（`duration_sec` 恒 0，前端不显示时长）
_POINT_SOURCES = ("change", "audit")
#: 结果码（前端渲染"结果徽标"，与告警级别**分列**）
_OUTCOME = {"task_failed": "failed", "task_stuck": "stuck", "change": "merged"}
#: 处置动作的顺序补充（primary 来自各来源的 `_NEXT_STEP`，这里补"顺路能看什么"）
_NEXT_STEP_EXTRA = {"timeline": ("logs",), "logs": ("timeline",), "issue": ("timeline",),
                    "device": ("logs",), "": ()}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _dt(raw: Any) -> Optional[datetime]:
    s = normalize_event_time(raw)
    try:
        v = str(s or "").strip().replace("Z", "+00:00")
        if not v:
            return None
        d = datetime.fromisoformat(v)
        return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
    except Exception:
        return None


def _dur(started: Optional[datetime], ended: Optional[datetime], now: datetime) -> tuple:
    """返回 (duration_sec, ongoing)。"""
    if not started:
        return 0, False
    if ended:
        return max(0, int((ended - started).total_seconds())), False
    return max(0, int((now - started).total_seconds())), True


def _device_id(dim_key: Any) -> str:
    """从 `dim_key` 取设备 id（`runtime:<id>`）——缺口/设备类事件的设备维度。"""
    dk = str(dim_key or "")
    return dk.split(":", 1)[1] if dk.startswith("runtime:") else ""


def _state_ok(item: Dict[str, Any], state: str) -> bool:
    """`state` 过滤（含旧别名 firing→stuck、resolved→failed）。"""
    if not state:
        return True
    return str(item.get("state") or "") in _STATE_ALIAS.get(state, (state,))


def _finalize(item: Dict[str, Any]) -> Dict[str, Any]:
    """补齐 S1 契约字段（**旧字段一律保留**，前端可灰度）：

    - `ts_kind`：`interval`（有始有终）| `point`（瞬时；变更/审计）
    - `outcome`：结果码（failed/stuck/merged…），前端渲染"结果徽标"，与告警级别分列
    - `state_label_key`：后端给出 i18n 键 `observability.state.<x>`，前端只做翻译（不猜词表）
    - `ended_at`：结束时间（未结束/瞬时事件为 ""）
    - `target`：结构化 `{repo_key, issue_id, device_id}`，替代让前端自己拆字符串
    - `next_step`：处置动作**数组**（primary 在前），并按键上有无对应目标裁剪
      （无 task → 不给 timeline；无 device → 不给 device；无 issue → 不给 issue）
    """
    src = str(item.get("source") or "")
    st = str(item.get("state") or "")
    device_id = _device_id(item.get("dim_key"))
    end = ""
    if not item.get("ongoing") and src not in _POINT_SOURCES:
        end = str(item.get("last_seen_at") or "")
    primary = str(item.get("next_step") or "")
    steps: List[str] = []
    for _s in [primary] + list(_NEXT_STEP_EXTRA.get(primary, ())):
        if _s and _s not in steps:            # 保序去重；空串（审计动作）不进数组
            steps.append(_s)
    if not device_id:
        steps = [s for s in steps if s != "device"]
    if not item.get("task_id") and src != "task":
        steps = [s for s in steps if s != "timeline"]
    if not item.get("issue_id"):
        steps = [s for s in steps if s != "issue"]
    if not steps and src != "audit":
        # 兜底：告警/任务/变更至少给一个"看日志"入口（审计动作没有下钻目标，保持空）
        steps = ["logs"]
    item["ts_kind"] = "point" if src in _POINT_SOURCES else "interval"
    # 结果码只对任务/变更来源有意义（告警的结果是 state=firing/resolved，走 state_label_key）
    item["outcome"] = (_OUTCOME.get(str(item.get("kind") or ""), "")
                       if src in ("task", "change") else "")
    item["state_label_key"] = f"observability.state.{st}" if st in STATES else ""
    item["ended_at"] = end
    item["target"] = {"repo_key": item.get("repo_key") or "",
                      "issue_id": str(item.get("issue_id") or ""),
                      "device_id": device_id}
    item["next_step"] = steps
    return item


def parse_cursor(raw: Any) -> Optional[tuple]:
    """解析游标 `"<started_at>|<key>"`（`next_cursor` 的原样回传）。非法 → None（当首页）。"""
    s = str(raw or "").strip()
    if not s:
        return None
    ts, _, key = s.partition("|")
    return (ts.strip(), key.strip()) if ts.strip() else None


def _alert_events(user: Dict[str, Any], state: str, repo: str, limit: int,
                  global_scope: bool = False, q: str = "") -> List[Dict[str, Any]]:
    """告警类来源（monitor/gap/mutation/slo）。

    用户面：可见性由 `alerts.list_events` 负责（可见仓库 ∪ 可见设备）；
    管理台（`global_scope=True`）：走 `alerts.list_events_all`（全局，含平台级）。
    """
    from src.observability import alerts as al
    status = {"firing": "firing", "resolved": "resolved"}.get(state, "")
    try:
        rows = (al.list_events_all(status=status, limit=limit, repo=repo, q=q) if global_scope
                else al.list_events(user, status=status, limit=limit, repo=repo, q=q))
    except Exception as e:
        log.warning(f"obs events: alert source failed: {e}")
        return []
    now = _now()
    out: List[Dict[str, Any]] = []
    for r in rows:
        src = str(r.get("source") or "monitor")
        if src not in ("monitor", "gap", "mutation", "slo"):
            src = "monitor"
        started = _dt(r.get("first_fired_at"))
        ended = _dt(r.get("resolved_at")) if (r.get("status") == "resolved") else None
        dur, ongoing = _dur(started, ended, now)
        out.append({
            "key": f"alert:{r.get('id')}", "source": src, "kind": str(r.get("rule_name") or ""),
            "level": str(r.get("severity") or "warning"),
            # 可见层（user/platform）：与"级别 severity"是两回事（§9.3 已踩过的坑）
            "scope": str(r.get("level") or "user"),
            "repo_key": r.get("repo_key") or "", "dim_key": r.get("dim_key") or "",
            "issue_id": "", "task_id": str(r.get("link_task_id") or ""), "trace_id": "",
            "started_at": normalize_event_time(r.get("first_fired_at")) or "",
            "last_seen_at": normalize_event_time(r.get("last_seen_at")) or "",
            "duration_sec": dur, "ongoing": ongoing,
            "state": str(r.get("status") or ""),
            "value": r.get("value"), "threshold": r.get("threshold"),
            "metric": str(r.get("metric") or ""),
            "silenced": bool(r.get("silenced_until")),
            "escalated": bool((r.get("escalated_at") or "").strip()),
            "next_step": _NEXT_STEP.get(str(r.get("rule_name") or ""), "logs"),
        })
    return out


def _task_hay(item: Dict[str, Any]) -> str:
    """任务事件的检索串（含错误摘要）。"""
    parts = [str(item.get(k) or "") for k in
             ("kind", "repo_key", "issue_id", "task_id", "state", "detail", "title")]
    errs = item.get("errors")
    if isinstance(errs, list):
        parts.extend(str(e) for e in errs)
    return " ".join(parts).lower()


def _task_events(user: Dict[str, Any], state: str, repo: str, limit: int,
                 repo_keys: Optional[Set[str]] = None, global_scope: bool = False,
                 q: str = ""
                 ) -> List[Dict[str, Any]]:
    """任务类来源：卡住（stuck）+ 失败（failed）——复用 ops_overview 的口径（同一套仓库/组织过滤）。

    ⚠️ `ops_overview` 是**组织级**过滤（不是"我的可见仓库"）：所以用户面必须再按可见仓库收敛，
    否则会把同组织里别人仓库的任务事件带进用户面（这是实打实的越权面）。
    管理台全局视角（`global_scope=True`）不做这层收敛。
    """
    from src.api.routes.dashboard import get_ops_overview
    try:
        ops = get_ops_overview(repo=repo, user=user) or {}
    except Exception as e:
        log.warning(f"obs events: task source failed: {e}")
        return []
    tasks = (ops.get("tasks") or {})
    now = _now()
    out: List[Dict[str, Any]] = []
    if state in ("", "firing", "stuck"):
        for t in (tasks.get("stuck_tasks") or []):
            started = _dt(t.get("started_at"))
            dur, ongoing = _dur(started, None, now)
            out.append({
                "key": f"task:{t.get('id')}:stuck", "source": "task", "kind": "task_stuck",
                "level": "warning", "repo_key": t.get("repo_key") or "", "dim_key": "",
                "issue_id": str(t.get("issue_id") or ""), "task_id": str(t.get("id") or ""),
                "trace_id": "", "started_at": normalize_event_time(t.get("started_at")) or "",
                "last_seen_at": "", "duration_sec": dur, "ongoing": ongoing,
                "state": "stuck", "value": None, "threshold": None, "metric": "",
                "silenced": False, "escalated": False, "next_step": "timeline",
            })
    if state in ("", "resolved", "failed"):
        for t in (tasks.get("failed_tasks") or [])[:limit]:
            started = _dt(t.get("started_at"))
            ended = _dt(t.get("completed_at"))
            dur, ongoing = _dur(started, ended, now)
            errs = t.get("errors") or []
            out.append({
                "key": f"task:{t.get('id')}:failed", "source": "task", "kind": "task_failed",
                "level": "warning", "repo_key": t.get("repo_key") or "", "dim_key": "",
                "issue_id": str(t.get("issue_id") or ""), "task_id": str(t.get("id") or ""),
                "trace_id": "", "started_at": normalize_event_time(t.get("started_at")) or "",
                "last_seen_at": normalize_event_time(t.get("completed_at")) or "",
                "duration_sec": dur, "ongoing": ongoing, "state": "failed",
                "value": None, "threshold": None, "metric": "",
                "detail": (errs[0] if errs else (t.get("result_text") or ""))[:200],
                "exit_code": t.get("exit_code"),
                "silenced": False, "escalated": False, "next_step": "timeline",
            })
    # 关键字：任务来源没有独立 SQL（数据来自 ops_overview 的当前卡住/失败列表，本身已是 Top-N 视图），
    # 故在服务端这一层过滤；语义与其它来源一致 = "在该来源的全集内检索"。
    if q:
        ql = q.lower()
        out = [x for x in out if ql in _task_hay(x)]
    return out


def _visible_filter(items: List[Dict[str, Any]], repo_keys: Set[str]) -> List[Dict[str, Any]]:
    """按可见仓库收敛（任务来源用：ops_overview 只按组织过滤）。"""
    if not repo_keys:
        return []
    return [x for x in items if (x.get("repo_key") or "") in repo_keys]


def _change_events(user: Dict[str, Any], state: str, repo: str, limit: int,
                   repo_keys: Set[str], all_repos: bool = False, q: str = "") -> List[Dict[str, Any]]:
    """变更类来源：PR 合并（`events`，P2-3 会扩展到部署等）。"""
    from src.session_manager import db, db_lock
    if not all_repos and not repo_keys:
        return []
    sql = ("SELECT seq, repo_key, event_type, issue_id, title, actor, duration, started_at,"
           " completed_at, created_at, entity_type FROM events"
           " WHERE event_type='pull_request_merged'")
    params: List[Any] = []
    if not all_repos:
        ph = ",".join("?" * len(repo_keys))
        sql += f" AND repo_key IN ({ph})"
        params.extend(sorted(repo_keys))
    if q:
        sql += " AND (title LIKE ? OR actor LIKE ? OR issue_id LIKE ?)"
        params.extend([_like(q)] * 3)
    sql += " ORDER BY seq DESC LIMIT ?"
    params.append(int(limit))
    rows: List[tuple] = []
    try:
        with db_lock:
            c = db.cursor()
            c.execute(sql, tuple(params))
            rows = list(c.fetchall())
    except Exception as e:
        log.warning(f"obs events: change source failed: {e}")
        return []
    now = _now()
    out: List[Dict[str, Any]] = []
    for seq, repo_key, _etype, issue_id, title, actor, duration, started_at, completed_at, \
            created_at, _entity in rows:
        started = _dt(started_at) or _dt(created_at)
        ended = _dt(completed_at)
        dur, ongoing = _dur(started, ended, now)
        try:
            if not ended and duration:
                dur, ongoing = int(float(duration)), False
        except Exception:
            pass
        out.append({
            "key": f"change:{seq}", "source": "change", "kind": "change",
            "level": "info", "repo_key": repo_key or "", "dim_key": "",
            "issue_id": str(issue_id or ""), "task_id": "", "trace_id": "",
            "started_at": normalize_event_time(started_at or created_at) or "",
            "last_seen_at": normalize_event_time(completed_at) or "",
            "duration_sec": dur, "ongoing": ongoing, "state": "done",
            "title": title or "", "actor": actor or "", "value": None, "threshold": None,
            "metric": "", "silenced": False, "escalated": False, "next_step": "issue",
        })
    return out


def _audit_events(state: str, limit: int, q: str = "") -> List[Dict[str, Any]]:
    """审计类来源（**仅 L2**）：平台动作流水（登录/改配置等）。"""
    from src.session_manager import db, db_lock
    rows: List[tuple] = []
    try:
        with db_lock:
            c = db.cursor()
            _sql = ("SELECT id, event_type, user_id, operator_id, ip, created_at, detail"
                    " FROM audit_logs")
            _p: List[Any] = []
            if q:
                _sql += " WHERE (event_type LIKE ? OR detail LIKE ?)"
                _p.extend([_like(q)] * 2)
            _sql += " ORDER BY seq DESC LIMIT ?"
            _p.append(int(limit))
            c.execute(_sql, tuple(_p))
            rows = list(c.fetchall())
    except Exception as e:
        log.warning(f"obs events: audit source failed: {e}")
        return []
    out: List[Dict[str, Any]] = []
    for aid, etype, user_id, operator_id, ip, created_at, detail in rows:
        out.append({
            "key": f"audit:{aid}", "source": "audit", "kind": str(etype or ""),
            "level": "info", "repo_key": "", "dim_key": "",
            "issue_id": "", "task_id": "", "trace_id": "",
            "started_at": normalize_event_time(created_at) or "", "last_seen_at": "",
            "duration_sec": 0, "ongoing": False, "state": "done",
            "title": str(etype or ""), "actor": str(operator_id or user_id or ""),
            "ip": ip or "", "detail": str(detail or "")[:200],
            "value": None, "threshold": None, "metric": "",
            "silenced": False, "escalated": False, "next_step": "",
        })
    return out


def unified_events(user: Dict[str, Any], sources: Optional[List[str]] = None, state: str = "",
                   repo: str = "", limit: int = 100, allow_audit: bool = False,
                   global_scope: bool = False, q: Any = "", before: Any = "") -> Dict[str, Any]:
    """统一事件视图。

    `sources` 省略 = 全部可见来源；`state` 取 STATES 全集（空=全部；旧值 firing/resolved 兼容）；
    `repo` 传仓库短名时只出该仓库（用户面越权 → RepoNotVisible）；
    `allow_audit=True` 才可能返回 `audit` 来源（L2 专用）；
    `global_scope=True` = 管理台视角：**全局不过滤可见范围**、告警来源含平台级事件。
    """
    from src.observability.overview import visible_repo_keys
    allow = L2_SOURCES if allow_audit else L1_SOURCES
    want = [s for s in (sources or allow) if s in allow]
    if not want:
        want = list(allow)
    repo_keys: Set[str] = set()
    if not global_scope:
        try:
            repo_keys = set(visible_repo_keys(user))
        except Exception as e:
            log.warning(f"obs events: visible keys failed: {e}")
        if repo:
            from src.repo_key import to_repo_key
            want_keys = {to_repo_key(repo), to_repo_key(repo, "github")}
            hit = sorted(want_keys & repo_keys)
            if not hit:
                from src.observability.overview import RepoNotVisible
                raise RepoNotVisible(repo)
            repo_keys = set(hit)

    kw = norm_q(q)
    # 取数窗口（S1 游标分页的前提）：各来源按 3×limit 取，合并后才有"下一页"可翻；
    # 若某来源取满这个窗口，说明它可能还有更早的事件没进来 → `truncated_sources`。
    n_limit = max(1, min(int(limit or 100), 1000))
    fetch_n = min(1000, max(n_limit * 3, n_limit + 1))
    items: List[Dict[str, Any]] = []
    # 注意：告警类来源是**一组**（含 SLO 违约 P2-1）——这里漏一个会导致按该来源过滤时整类不取数
    if any(s in want for s in ("monitor", "gap", "mutation", "slo")):
        items.extend(_alert_events(user, state, repo, fetch_n, global_scope=global_scope, q=kw))
    if "task" in want:
        got = _task_events(user, state, repo, fetch_n, q=kw)
        if not global_scope:
            got = _visible_filter(got, repo_keys)
        items.extend(got)
    if "change" in want:
        items.extend(_change_events(user, state, repo, fetch_n, repo_keys,
                                    all_repos=global_scope, q=kw))
    if "audit" in want and allow_audit:
        items.extend(_audit_events(state, fetch_n, q=kw))

    # 按来源过滤（告警来源已按 source 细分）+ 状态过滤（S1：state 全集）
    items = [x for x in items if x["source"] in want and _state_ok(x, state)]
    # S1 契约字段（ts_kind/outcome/state_label_key/ended_at/target/next_step 数组）
    items = [_finalize(x) for x in items]
    # 游标分页（S1）：`before` = 上一页 `next_cursor` 原样回传 → 取 (started_at, key) 更小的一段。
    # 注意语义边界：各来源按各自 limit 取的是**最新一段**，因此翻页深度受这个窗口约束；
    # 哪个来源取满了会在 `truncated_sources` 里报出来（见 §四）。
    cur = parse_cursor(before)
    if cur:
        items = [x for x in items
                 if (str(x.get("started_at") or ""), str(x.get("key") or "")) < cur]
    window_counts: Dict[str, int] = {}
    for x in items:
        window_counts[x["source"]] = window_counts.get(x["source"], 0) + 1
    items.sort(key=lambda x: (x.get("started_at") or "", x.get("key") or ""), reverse=True)
    page = items[:n_limit]
    counts: Dict[str, int] = {}
    for x in page:                                  # `by_source` 只描述**本页**
        counts[x["source"]] = counts.get(x["source"], 0) + 1
    # 某来源取满取数窗口 → 可能还有更早的没取出（前端据此提示"已被截断"）
    truncated = sorted(s for s, n in window_counts.items() if n >= fetch_n)
    next_cursor = None
    if len(items) > n_limit and page:
        next_cursor = f"{page[-1].get('started_at') or ''}|{page[-1].get('key') or ''}"
    # 注意：`total` 是**本页条数**（各来源各取 limit 条后合并截断），不是数据库里的总数
    return {"events": page, "total": len(page), "next_cursor": next_cursor,
            "truncated_sources": truncated,
            "by_source": counts, "q": kw, "state": str(state or ""),
            "sources": list(want), "global": bool(allow_audit)}