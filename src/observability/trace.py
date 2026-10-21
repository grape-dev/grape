"""可观测性 · 追踪（Traces）——以任务为骨架重建时间线（设计 §6.4）。

不引入 OTel：用已有的 `agent_tasks` / `task_messages` / `events` / `obs_log` / `obs_llm_usage` 拼出
一条任务的完整轨迹，并把同一 issue 的多轮任务（vod → issue → coding → qa）串成流水线视图。

**两个 id 的定义（都确定性生成，不依赖写入方配合）**：
- `trace_id = tr_<sha1(task_id)[:16]>`  —— 任务级轨迹 id，稳定可复算
- `pipeline_trace_id = tr_<sha1(repo_key|issue_id)[:16]>` —— 同一 issue 的开发流程共享；
  没有 issue 的任务退回用自身 trace_id

为什么用确定性 id + 惰性回填，而不是改三处 INSERT：`agent_tasks` 的写入点在
event_pipeline(×2)/runtime.service 三处，改动面大且容易漏；确定性 id 让**历史任务立刻有轨迹**
（首次查询时批量回填），新任务也自动成立。若将来要给 worker/日志透传 trace_id，
直接用这两个函数算即可（同一 input 恒等）。
"""

from __future__ import annotations

import hashlib
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from src.config import log

STEP_LIMIT = 200            # 单任务时间线最多返回的步骤数
LOG_LIMIT = 50              # 时间线内联命中的日志条数
LOG_WINDOW_SEC = 30         # "与日志互跳"的默认窗口（±30s）
_TASK_RE = None


def _sha12(s: str) -> str:
    return hashlib.sha1(s.encode("utf-8", "ignore")).hexdigest()[:16]


def task_trace_id(task_id: str) -> str:
    """任务级 trace id（确定性）。"""
    tid = str(task_id or "").strip()
    return ("tr_" + _sha12(tid)) if tid else ""


def pipeline_trace_id(repo_key: str, issue_id: str, task_id: str = "") -> str:
    """流水线级 trace id：同一 (仓库, issue) 共享；没有 issue 时退回任务级 id。"""
    rk = str(repo_key or "").strip()
    iss = str(issue_id or "").strip()
    if rk and iss:
        return "tr_" + _sha12(f"{rk}|{iss}")
    return task_trace_id(task_id)


def _dt(raw: Any) -> Optional[datetime]:
    from src.api.time_utils import normalize_event_time
    s = normalize_event_time(raw)
    if not s or not isinstance(s, str):
        return None
    try:
        d = datetime.fromisoformat(s)
    except (ValueError, TypeError):
        return None
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def _iso(d) -> str:
    return d.isoformat() if isinstance(d, datetime) else ""


# ---------------------------------------------------------------- 回填

def stamp_missing(limit: int = 5000) -> int:
    """给还没 trace 的任务补上两个 id（幂等，可反复调用）。返回补齐行数。"""
    from src.session_manager import db, db_lock
    done = 0
    try:
        with db_lock:
            c = db.cursor()
            c.execute("SELECT id, repo_key, issue_id FROM agent_tasks"
                      " WHERE trace_id IS NULL OR trace_id='' LIMIT ?", (int(limit),))
            rows = c.fetchall()
            for tid, rk, iss in rows:
                try:
                    c.execute("UPDATE agent_tasks SET trace_id=?, pipeline_trace_id=? WHERE id=?",
                              (task_trace_id(tid), pipeline_trace_id(rk or "", iss or "", tid), tid))
                    done += 1
                except Exception:
                    pass
            if done:
                db.commit()
    except Exception as e:
        log.warning(f"obs trace stamp_missing failed: {e}")
        return 0
    return done


# ---------------------------------------------------------------- 时间线

def _steps(task_id: str) -> List[Dict[str, Any]]:
    from src.session_manager import db, db_lock
    out: List[Dict[str, Any]] = []
    try:
        with db_lock:
            c = db.cursor()
            c.execute("SELECT seq, type, tool, content, input_json, output, created_at"
                      " FROM task_messages WHERE task_id=? ORDER BY seq LIMIT ?",
                      (task_id, STEP_LIMIT))
            for seq, typ, tool, content, input_json, output, created_at in c.fetchall():
                out.append({
                    "seq": int(seq or 0), "type": typ or "", "tool": tool or "",
                    "ts": created_at or "",
                    "content": (content or "")[:400],
                    "input": (input_json or "")[:400],
                    "output": (output or "")[:400],
                })
    except Exception as e:
        log.warning(f"obs trace steps failed task={task_id}: {e}")
    return out


def _tokens_by_task(task_ids: List[str]) -> Dict[str, Dict[str, int]]:
    from src.session_manager import db, db_lock
    out: Dict[str, Dict[str, int]] = {}
    if not task_ids:
        return out
    ph = ",".join("?" * len(task_ids))
    try:
        with db_lock:
            c = db.cursor()
            c.execute(
                "SELECT task_id, COALESCE(SUM(input_tokens),0), COALESCE(SUM(output_tokens),0),"
                " COALESCE(SUM(cache_read_tokens),0), COALESCE(SUM(cache_write_tokens),0),"
                " COALESCE(SUM(reasoning_tokens),0), COUNT(*) FROM obs_llm_usage"
                f" WHERE task_id IN ({ph}) GROUP BY task_id", tuple(task_ids))
            for tid, i, o, cr, cw, rz, n in c.fetchall():
                out[str(tid)] = {"input": int(i or 0), "output": int(o or 0),
                                 "cache_read": int(cr or 0), "cache_write": int(cw or 0),
                                 "reasoning": int(rz or 0), "calls": int(n or 0),
                                 "total": int(i or 0) + int(o or 0) + int(cr or 0)
                                          + int(cw or 0) + int(rz or 0)}
    except Exception as e:
        log.warning(f"obs trace tokens failed: {e}")
    return out


def _engine_label(runtime_id: int) -> str:
    """runtime_id → `引擎@设备`（给时间线标注在哪台机器的哪个引擎上跑）。"""
    if not runtime_id:
        return ""
    from src.session_manager import db, db_lock
    try:
        with db_lock:
            c = db.cursor()
            c.execute("SELECT COALESCE(NULLIF(r.name,''), NULLIF(r.provider,''), ''),"
                      " COALESCE(rc.device_name, rc.name, '')"
                      " FROM agent_runtimes r LEFT JOIN runtime_credentials rc"
                      " ON rc.id = r.credential_id WHERE r.id=?", (int(runtime_id),))
            row = c.fetchone()
            if not row:
                return f"#{runtime_id}"
            eng = row[0] or ""
            dev = row[1] or ""
            return (f"{eng}@{dev}" if (eng and dev) else (eng or dev or f"#{runtime_id}"))
    except Exception:
        return f"#{runtime_id}"


def _task_item(row: Dict[str, Any], tokens: Dict[str, Dict[str, int]],
               logs: List[Dict[str, Any]], with_steps: bool) -> Dict[str, Any]:
    enq = _dt(row.get("created_at"))
    disp = _dt(row.get("started_at"))
    term = _dt(row.get("completed_at"))
    status = (row.get("status") or "").lower()
    dur = round((term - disp).total_seconds(), 1) if (term and disp) else None
    wait = round((disp - enq).total_seconds(), 1) if (disp and enq) else None
    failure = ""
    if status in ("failed", "cancelled"):
        raw = (row.get("errors") or row.get("stderr") or row.get("result_text") or "")
        failure = str(raw).strip().replace("\n", " ")[:300]
    item = {
        "id": row.get("id"), "agent_type": row.get("agent_type") or "",
        "status": row.get("status") or "", "issue_id": row.get("issue_id") or "",
        "repo_key": row.get("repo_key") or "", "runtime_id": row.get("runtime_id") or 0,
        "engine": _engine_label(int(row.get("runtime_id") or 0)),
        "enqueue_at": _iso(enq), "dispatch_at": _iso(disp), "terminal_at": _iso(term),
        "queue_wait_s": wait, "duration_s": dur,
        "event_seq": row.get("event_seq") or 0,
        "tokens": tokens.get(str(row.get("id")), {}),
        "failure_reason": failure,
        "log_hits": [l for l in logs if str(l.get("task_id")) == str(row.get("id"))][:10],
    }
    if with_steps:
        item["steps"] = _steps(str(row.get("id")))
    return item


def resolve_tasks(trace: str) -> List[Dict[str, Any]]:
    """按 trace 值找出任务：支持任务级 id、流水线级 id，也支持直接传任务号（t_xxx）。

    传任务号时返回**该任务的整条流水线**（同 issue 的其他任务一起），前端从任务列表点进来即可看全流程。
    """
    from src.session_manager import db, db_lock
    t = str(trace or "").strip()
    if not t:
        return []
    rows: List[Dict[str, Any]] = []
    try:
        with db_lock:
            c = db.cursor()
            if t.startswith("t_"):
                # 先按任务号定位，再用它的 pipeline_trace_id 取同流程全部任务
                c.execute("SELECT trace_id, pipeline_trace_id FROM agent_tasks WHERE id=?", (t,))
                got = c.fetchone()
                if not got:
                    return []
                keys = [t, got[0] or "", got[1] or ""]
                keys = [k for k in keys if k]
                ph = ",".join("?" * len(keys))
                c.execute(
                    "SELECT id, agent_type, status, issue_id, repo_key, runtime_id, event_seq,"
                    " created_at, started_at, completed_at, errors, stderr, result_text,"
                    " trace_id, pipeline_trace_id FROM agent_tasks"
                    f" WHERE trace_id IN ({ph}) OR pipeline_trace_id IN ({ph}) OR id=?"
                    " ORDER BY created_at", tuple(keys + keys + [t]))
            else:
                c.execute(
                    "SELECT id, agent_type, status, issue_id, repo_key, runtime_id, event_seq,"
                    " created_at, started_at, completed_at, errors, stderr, result_text,"
                    " trace_id, pipeline_trace_id FROM agent_tasks"
                    " WHERE trace_id=? OR pipeline_trace_id=? ORDER BY created_at",
                    (t, t))
            cols = [d[0] for d in c.description]
            rows = [dict(zip(cols, r)) for r in c.fetchall()]
    except Exception as e:
        log.warning(f"obs trace resolve failed trace={t}: {e}")
    return rows


def timeline(user: Dict[str, Any], trace: str, with_steps: bool = True) -> Dict[str, Any]:
    """时间线聚合。**按可见范围过滤**：调用者看不到的任务一律不出现；全不可见 → 返回空。

    不做 404：trace id 是哈希值，猜不到别人仓库的 trace；返回空与"不存在"等价，
    路由层对空结果给 404。
    """
    from src.observability.overview import visible_repo_keys
    from src.session_manager import db, db_lock

    stamp_missing()                                     # 保证历史任务也有 id
    rows = resolve_tasks(trace)
    visible = set(visible_repo_keys(user))
    rows = [r for r in rows if (r.get("repo_key") or "") in visible]
    if not rows:
        return {"trace": trace, "kind": "unknown", "tasks": [], "events": [],
                "logs": [], "log_window": {}}

    task_ids = [str(r.get("id")) for r in rows]
    tokens = _tokens_by_task(task_ids)
    ph = ",".join("?" * len(task_ids))

    # 内联日志命中：按任务 + ±30s 窗口
    logs: List[Dict[str, Any]] = []
    try:
        with db_lock:
            c = db.cursor()
            c.execute("SELECT ts, level, source, task_id, repo_key, msg FROM obs_log"
                      f" WHERE task_id IN ({ph}) ORDER BY ts DESC LIMIT ?",
                      tuple(task_ids + [LOG_LIMIT]))
            cols = [d[0] for d in c.description]
            logs = [dict(zip(cols, r)) for r in c.fetchall()]
    except Exception as e:
        log.warning(f"obs trace logs failed: {e}")

    # 事件（同一 issue 的流水线上下文）
    events: List[Dict[str, Any]] = []
    seqs = [int(r.get("event_seq") or 0) for r in rows if r.get("event_seq")]
    if seqs:
        ph2 = ",".join("?" * len(seqs))
        try:
            with db_lock:
                c = db.cursor()
                c.execute("SELECT seq, event_type, actor, issue_id, title, status, created_at"
                          f" FROM events WHERE seq IN ({ph2}) ORDER BY seq", tuple(seqs))
                cols = [d[0] for d in c.description]
                events = [dict(zip(cols, r)) for r in c.fetchall()]
        except Exception as e:
            log.warning(f"obs trace events failed: {e}")

    first, last = rows[0], rows[-1]
    pt = first.get("pipeline_trace_id") or ""
    kind = "task" if (rows[0].get("trace_id") == trace and len(rows) <= 1) else "pipeline"
    return {
        "trace": trace,
        "kind": kind,
        "pipeline_trace_id": pt,
        "repo_key": first.get("repo_key") or "",
        "issue_id": first.get("issue_id") or "",
        "tasks": [_task_item(r, tokens, logs, with_steps) for r in rows],
        "events": events,
        "logs": logs,
        "log_window_sec": LOG_WINDOW_SEC,
        "counts": {"tasks": len(rows), "failed": sum(1 for r in rows
                                                     if (r.get("status") or "").lower() == "failed")},
    }


def pipeline_by_issue(user: Dict[str, Any], repo: str, issue_id: str) -> Dict[str, Any]:
    """按 (仓库, issue) 直接看整条流水线（前端从任务列表点进"这条 issue 的流程"）。"""
    from src.repo_key import to_repo_key
    rk = to_repo_key(repo) if repo else ""
    return timeline(user, pipeline_trace_id(rk, issue_id))