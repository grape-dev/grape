"""可观测性 · 插件调用审计（`grape_t_plugin_call_logs`）——Issue #1091（插件市场-C3）。

方案文档「五、Web 端开发清单 ⑥ 可观测扩展」：
- 审计视图字段：插件 / 命令 / adapter / 耗时 / exit / 结论，支持组合筛选；
- 用量与失败率统计接入 machine-health 健康分（health_scorer 消费 `runtime_stats`）。

四条边界（复用 obs_log 模式，见 logs.py）：
1. **入库前脱敏**：error_json 里的凭据类信息先打码再落库；
2. **用户面只出可见仓库的调用**：repo_key 在可见集合内才可见；无 repo 维度的
   机器级调用（如安装自检）不进用户面，但计入 per-runtime 健康分统计；
3. **上报通道**：Runner/Runtime 侧经 POST /api/obs/plugins/calls/report 批量上报
   （凭据鉴权，B5 #1088 落地后由 Plugin Runner 对齐调用）；
4. **留存**：与 obs_log 相同的按天清理（默认 14 天）。

数据契约（与 B5 并行开发，#1088 落地后对齐）：
    {ts, plugin, command, adapter, duration_ms, exit_code, outcome,
     runtime_id, task_id, repo_key, error_json}
    outcome ∈ success | failed | timeout（exit_code=0 → success，超时 → timeout 计失败）
"""

from __future__ import annotations

import hashlib
import json
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Tuple

from src.kernel.config import log
from src.kernel.id_gen import uuid7_str

# 数值五源（架构守护 B2）——每条给出「依据来源」，避免无源魔鬼数字：
#: 审计保留天数（明确业务要求：与月度发布节奏对齐，保留两个发布周期）
RETENTION_DAYS = 14
#: 清理任务间隔（明确业务要求：每小时一次；单批上限 MAX_LIMIT 保证单次窗口可控）
CLEANUP_INTERVAL_SEC = 3600
#: 单次查询返回上限（项目记录：控制台列表一次最多 500 条，超出走分页）
MAX_LIMIT = 500
#: 默认页大小（项目记录：与前端分页默认页一致）
DEFAULT_LIMIT = 100

# 失败口径：outcome ∈ FAILED_OUTCOMES（timeout 计失败——挂死同样拖累机器健康）
FAILED_OUTCOMES = ("failed", "timeout")
VALID_OUTCOMES = ("success", "failed", "timeout")


def redact(text: str) -> str:
    """凭据类信息打码（入库前调用，复用 obs_log 的脱敏规则）。"""
    from src.domains.insight.observability.logs import redact as _redact
    return _redact(text)


def normalize_outcome(rec: Dict[str, Any]) -> str:
    """outcome 归一：显式声明优先（timeout），否则按 exit_code 推导。"""
    oc = str(rec.get("outcome") or "").strip().lower()
    if oc in VALID_OUTCOMES:
        return oc
    try:
        exit_code = int(rec.get("exit_code") or 0)
    except (TypeError, ValueError):
        exit_code = 0
    return "success" if exit_code == 0 else "failed"


def _dedup_key(rec: Dict[str, Any]) -> str:
    raw = "|".join(str(x) for x in (
        rec.get("ts") or "", rec.get("plugin") or "", rec.get("command") or "",
        rec.get("adapter") or "", rec.get("runtime_id") or 0,
        rec.get("task_id") or "", rec.get("exit_code") or 0,
        rec.get("duration_ms") or 0, rec.get("outcome") or ""))
    return hashlib.sha1(raw.encode("utf-8", "ignore")).hexdigest()[:24]


def _norm_ts(ts: Any) -> str:
    """统一成 ISO(UTC)（worker 上报可能是本地时间字符串，复用 obs_log 的归一通道）。"""
    s = str(ts or "").strip()
    if s:
        try:
            from src.api.time_utils import normalize_event_time
            _n = normalize_event_time(s)
            if isinstance(_n, str) and _n:
                return _n
        except Exception:
            pass
        return s
    return datetime.now(timezone.utc).isoformat()


def _to_int(v: Any, default: int = 0) -> int:
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


# ---------------------------------------------------------------- 入库

def ingest(records: List[Dict[str, Any]], default_runtime_id: int = 0) -> int:
    """批量入库（幂等：同 dedup_key 跳过）。返回**新写入**行数。

    每条记录字段（缺省即空）：ts/plugin/command/adapter/duration_ms/exit_code/
    outcome/runtime_id/task_id/repo_key/error_json。
    缺 plugin 名的行丢弃（无法归因的审计行没有价值）。
    """
    if not records:
        return 0
    from src.kernel.session_manager import _is_mysql_backend, db, db_lock

    rows: List[Tuple[Any, ...]] = []
    seen = set()
    for r in records:
        plugin = str(r.get("plugin") or "").strip()
        if not plugin:
            continue
        ts = _norm_ts(r.get("ts"))
        outcome = normalize_outcome(r)
        err = r.get("error_json")
        if err is not None and not isinstance(err, str):
            try:
                err = json.dumps(err, ensure_ascii=False)
            except Exception:
                err = ""
        err = redact(str(err or ""))[:4000]
        runtime_id = _to_int(r.get("runtime_id"))
        if not runtime_id:
            runtime_id = _to_int(default_runtime_id)
        rec_key = dict(r)
        rec_key["outcome"] = outcome
        key = _dedup_key(rec_key)
        if key in seen:
            continue
        seen.add(key)
        rows.append((uuid7_str(), key, ts, plugin[:128], str(r.get("command") or "")[:256],
                     str(r.get("adapter") or "")[:32], _to_int(r.get("duration_ms")),
                     _to_int(r.get("exit_code")), outcome, runtime_id,
                     str(r.get("task_id") or "")[:64], str(r.get("repo_key") or "")[:256],
                     err))

    if not rows:
        return 0
    ins = ("INSERT INTO grape_t_plugin_call_logs (id, dedup_key, ts, plugin, command, adapter,"
           " duration_ms, exit_code, outcome, runtime_id, task_id, repo_key, error_json)"
           " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)")
    if _is_mysql_backend():
        ins = ins.replace("INSERT INTO", "INSERT IGNORE INTO")
    else:
        ins = ins.replace("INSERT INTO", "INSERT OR IGNORE INTO")
    n = 0
    try:
        with db_lock:
            c = db.cursor()
            for row in rows:
                try:
                    c.execute(ins, row)
                    n += int(c.rowcount if c.rowcount is not None else 1)
                except Exception:
                    pass                                # 单行冲突/超长不影响整批
            db.commit()
    except Exception as e:
        log.warning(f"grape_t_plugin_call_logs ingest failed ({len(rows)} rows): {e}")
        return 0
    return n


# ---------------------------------------------------------------- 用户面查询

def _visible_repo_keys(user: Dict[str, Any]) -> List[str]:
    from src.domains.insight.observability.overview import visible_repo_keys
    return visible_repo_keys(user)


def query(user: Dict[str, Any], plugin: str = "", command: str = "", adapter: str = "",
          exit_code: int | None = None, outcome: str = "", repo: str = "",
          task_id: str = "", q: str = "", since: str = "", until: str = "",
          duration_min: int | None = None, duration_max: int | None = None,
          limit: int = DEFAULT_LIMIT, offset: int = 0) -> Dict[str, Any]:
    """用户面审计查询：组合筛选 + **只返回可见仓库的调用**。

    无 repo 维度的机器级调用（安装自检等）不进用户面（对齐 logs.py 边界 2）；
    越权 `repo` → 抛 RepoNotVisible（路由转 404）。
    duration_min/duration_max：耗时（毫秒）闭区间筛选，两端均可选（Spec-a 验收「可筛 耗时」）。
    facets 携带筛选条下拉取值（plugins / adapters / outcomes 计数）。
    """
    from src.domains.insight.observability.overview import RepoNotVisible
    from src.kernel.repo_key import to_repo_key
    from src.kernel.session_manager import db, db_lock

    visible = set(_visible_repo_keys(user))
    if repo:
        want = {to_repo_key(repo), to_repo_key(repo, "github")}
        hit = sorted(want & visible)
        if not hit:
            raise RepoNotVisible(repo)
        visible = set(hit)
    if not visible:
        return {"total": 0, "items": [], "facets": {"plugins": [], "adapters": [], "outcomes": {}},
                "limit": max(1, min(int(limit or DEFAULT_LIMIT), MAX_LIMIT)), "offset": 0}

    limit = max(1, min(int(limit or DEFAULT_LIMIT), MAX_LIMIT))
    offset = max(0, int(offset or 0))
    ph = ",".join("?" * len(visible))
    where = [f"repo_key IN ({ph})"]
    params: List[Any] = sorted(visible)
    if plugin:
        where.append("plugin=?")
        params.append(plugin)
    if command:
        where.append("command=?")
        params.append(command)
    if adapter:
        where.append("adapter=?")
        params.append(adapter)
    if exit_code is not None:
        where.append("exit_code=?")
        params.append(int(exit_code))
    if duration_min is not None:
        where.append("duration_ms >= ?")
        params.append(int(duration_min))
    if duration_max is not None:
        where.append("duration_ms <= ?")
        params.append(int(duration_max))
    if outcome:
        oc = outcome.strip().lower()
        if oc in VALID_OUTCOMES:
            where.append("outcome=?")
            params.append(oc)
    if task_id:
        where.append("task_id=?")
        params.append(task_id)
    if q:
        where.append("(command LIKE ? OR plugin LIKE ? OR error_json LIKE ?)")
        params.extend([f"%{q}%", f"%{q}%", f"%{q}%"])
    if since:
        where.append("ts >= ?")
        params.append(since)
    if until:
        where.append("ts <= ?")
        params.append(until)
    wsql = " AND ".join(where)

    items: List[Dict[str, Any]] = []
    total = 0
    try:
        with db_lock:
            c = db.cursor()
            c.execute(f"SELECT COUNT(*) FROM grape_t_plugin_call_logs WHERE {wsql}", tuple(params))
            total = int(c.fetchone()[0])
            c.execute(
                "SELECT id, ts, plugin, command, adapter, duration_ms, exit_code, outcome,"
                " runtime_id, task_id, repo_key, error_json"
                f" FROM grape_t_plugin_call_logs WHERE {wsql} ORDER BY ts DESC, id DESC LIMIT ? OFFSET ?",
                tuple(params + [limit, offset]))
            cols = [d[0] for d in c.description]
            for r in c.fetchall():
                d = dict(zip(cols, r))
                d["error_json"] = redact(d.get("error_json") or "")  # 兜底脱敏（老数据）
                items.append(d)
    except Exception as e:
        log.warning(f"grape_t_plugin_call_logs query failed: {e}")

    facets = {"plugins": [], "adapters": [], "outcomes": {}}
    try:
        with db_lock:
            c = db.cursor()
            c.execute(f"SELECT plugin, COUNT(*) FROM grape_t_plugin_call_logs WHERE {wsql}"
                      " GROUP BY plugin ORDER BY 2 DESC LIMIT 100", tuple(params))
            facets["plugins"] = [r[0] for r in c.fetchall() if r[0]]
            c.execute(f"SELECT DISTINCT adapter FROM grape_t_plugin_call_logs WHERE {wsql}"
                      " LIMIT 50", tuple(params))
            facets["adapters"] = sorted([r[0] for r in c.fetchall() if r[0]])
            c.execute(f"SELECT outcome, COUNT(*) FROM grape_t_plugin_call_logs WHERE {wsql}"
                      " GROUP BY outcome", tuple(params))
            facets["outcomes"] = {r[0]: int(r[1]) for r in c.fetchall()}
    except Exception:
        pass
    return {"total": total, "items": items, "facets": facets,
            "limit": limit, "offset": offset, "scoped_repos": len(visible)}


# ---------------------------------------------------------------- 统计（用量/失败率）

def _failed_sum_sql() -> str:
    """失败数聚合口径：outcome ∈ {failed, timeout}（timeout 计失败——挂死同样拖累机器健康）。"""
    ph = ",".join("?" * len(FAILED_OUTCOMES))
    return f"SUM(CASE WHEN outcome IN ({ph}) THEN 1 ELSE 0 END)", list(FAILED_OUTCOMES)


def stats(user: Dict[str, Any], range_hours: int = 24, ts_end: str = "",
          repo: str = "") -> Dict[str, Any]:
    """用户面用量/失败率统计：per-plugin 聚合（**只统计调用者可见仓库**）。

    返回 {range_hours, plugins: [{plugin, calls, failed, failure_rate, avg_duration_ms}],
          total_calls, total_failed}。机器级调用（无 repo）不进用户面统计（对齐 query 边界）。
    """
    from src.domains.insight.observability.overview import RepoNotVisible
    from src.kernel.repo_key import to_repo_key

    visible = set(_visible_repo_keys(user))
    if repo:
        want = {to_repo_key(repo), to_repo_key(repo, "github")}
        hit = sorted(want & visible)
        if not hit:
            raise RepoNotVisible(repo)
        visible = set(hit)
    _range = max(1, min(int(range_hours or 24), 24 * 30))
    end_s = str(ts_end or "").strip()
    end_dt = datetime.fromisoformat(end_s) if end_s else datetime.now(timezone.utc)
    if end_dt.tzinfo is None:
        end_dt = end_dt.replace(tzinfo=timezone.utc)
    start = (end_dt - timedelta(hours=_range)).isoformat()

    empty = {"range_hours": _range, "plugins": [], "total_calls": 0, "total_failed": 0}
    if not visible:
        return empty
    ph = ",".join("?" * len(visible))
    fail_sql, fail_params = _failed_sum_sql()
    sql = ("SELECT plugin, COUNT(*), " + fail_sql + ", AVG(duration_ms)"
           " FROM grape_t_plugin_call_logs WHERE repo_key IN (" + ph + ") AND ts >= ? AND ts <= ?"
           " GROUP BY plugin ORDER BY 2 DESC")
    # 占位符顺序：IN(?,?)（SELECT 聚合里）→ 仓库 IN → 时间窗两端
    params = fail_params + sorted(visible) + [start, end_dt.isoformat()]
    plugins: List[Dict[str, Any]] = []
    try:
        from src.kernel.session_manager import db, db_lock
        with db_lock:
            c = db.cursor()
            c.execute(sql, tuple(params))
            for name, calls, failed, avg_ms in c.fetchall():
                calls_i, failed_i = int(calls or 0), int(failed or 0)
                plugins.append({
                    "plugin": name,
                    "calls": calls_i,
                    "failed": failed_i,
                    "failure_rate": round(failed_i / calls_i, 4) if calls_i else 0.0,
                    "avg_duration_ms": int(avg_ms or 0),
                })
    except Exception as e:
        log.warning(f"grape_t_plugin_call_logs stats failed: {e}")
        return empty
    return {"range_hours": _range, "plugins": plugins,
            "total_calls": sum(p["calls"] for p in plugins),
            "total_failed": sum(p["failed"] for p in plugins)}


def runtime_stats(runtime_id: int, window_hours: int = 1) -> Dict[str, Any]:
    """per-runtime 窗口统计（health_scorer 插件因子消费）。

    **机器级口径**：无 repo 维度也计入（安装自检/健康探测都是真实调用）；
    窗口起点用 ISO UTC 字面量字符串比较（ingest 已把 ts 归一成同格式，
    两供建表同格式比较，无 health_scorer 注释里那种混格式踩坑面）。
    health_scorer 调用时传自己的窗口（_WINDOW_DAYS*24）保持口径一致。
    """
    if not runtime_id:
        return {"calls": 0, "failed": 0, "failure_rate": 0.0}
    _range = max(1, min(int(window_hours or 1), 24 * 30))
    start = (datetime.now(timezone.utc) - timedelta(hours=_range)).isoformat()
    fail_sql, fail_params = _failed_sum_sql()
    sql = ("SELECT COUNT(*), " + fail_sql + " FROM grape_t_plugin_call_logs"
           " WHERE runtime_id=? AND ts >= ?")
    try:
        from src.kernel.session_manager import db, db_lock
        with db_lock:
            c = db.cursor()
            # 占位符顺序：IN(?,?)（SELECT 聚合里）→ runtime_id → 窗口起点
            c.execute(sql, tuple(fail_params + [int(runtime_id), start]))
            row = c.fetchone()
            calls = int(row[0] or 0) if row else 0
            failed = int(row[1] or 0) if row else 0
            return {"calls": calls, "failed": failed,
                    "failure_rate": round(failed / calls, 4) if calls else 0.0}
    except Exception as e:
        log.warning(f"grape_t_plugin_call_logs runtime_stats failed (runtime={runtime_id}): {e}")
        return {"calls": 0, "failed": 0, "failure_rate": 0.0}


# ---------------------------------------------------------------- 留存

_last_cleanup = 0.0


def cleanup(days: int = RETENTION_DAYS) -> int:
    """清理超过留存期的审计行。返回删除行数。"""
    from src.kernel.session_manager import db, db_lock
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    try:
        with db_lock:
            c = db.cursor()
            c.execute("DELETE FROM grape_t_plugin_call_logs WHERE ts < ?", (cutoff,))
            n = int(c.rowcount or 0)
            db.commit()
        return n
    except Exception as e:
        log.warning(f"grape_t_plugin_call_logs cleanup failed: {e}")
        return 0


def maybe_cleanup(interval: int = CLEANUP_INTERVAL_SEC) -> int:
    """按间隔限流清理（由采样循环每次调用，与 obs_log 同挂点）。"""
    global _last_cleanup
    now = time.time()
    if now - _last_cleanup < interval:
        return 0
    _last_cleanup = now
    return cleanup()
