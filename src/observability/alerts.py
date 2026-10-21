"""可观测性 · 告警中心（规则求值 + 事件生命周期 + 用户面查询）。

设计：docs/weekly-07/observability-redesign-design.md §9（规则模型 / 求值引擎）
       §5.3（三层受众边界）—— **平台级规则与事件不进用户面**。

三条铁律：
1. **用户面只出 `level='user'` 的事件，且必须落在调用者可见仓库范围内**
   （平台级：DB 池 / DB 中止连接 / 全部 runtime 离线；HTTP 5xx 仅 L2 可见）。
2. **规则是全局配置，事件按维度记账**：`dim_key = repo:<repo_key>`；开放事件按
   `(rule_id, dim_key, status='firing')` 唯一，避免每轮求值刷出一堆重复行。
3. **`for_sec` 语义**：条件需**持续满足** for_sec 才触发（抑制抖动）；期间条件恢复则清空
   计时，已触发的事件转 resolved（只通知一次）。

通知：`notify_channels` 为空时**只记录不推送**（种子默认留空，避免误推打扰）；
非空时走既有 IM 通道（飞书）。
"""

from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

from src.config import log
from src.observability import alert_policy as _policy

# ===== 种子规则 =====
# 7 条与 deploy/alerts.yml 一一对应（seed_source 记原名）；后 3 条是 P1-3 新增的
# 数据缺失（kind='gap'）与突变检测（metric 以 _mutation 结尾）规则。
# level='user'   → 用户面可见（按仓库维度）
# level='platform' → 仅 L2/L3（DB/主机/全局引擎），用户面查询一律过滤掉
# kind='metric'（默认）→ 走指标阈值求值；kind='gap' → 走 gap.py 的数据缺失探测
SEED_RULES: List[Dict[str, Any]] = [
    {
        "name": "task_stuck", "kind": "metric", "level": "user", "metric": "task_stuck",
        "comparator": "gt", "threshold": 0, "window_sec": 300, "for_sec": 300,
        "severity": "warning", "seed_source": "GrapeTaskStuck",
        "description": "有任务运行超过 22 分钟（worker 默认单任务超时 1h，提前预警）",
    },
    {
        "name": "task_failure_rate", "kind": "metric", "level": "user", "metric": "task_failure_rate",
        "comparator": "gt", "threshold": 0.5, "window_sec": 300, "for_sec": 300,
        "severity": "warning", "seed_source": "GrapeTaskFailureRateHigh",
        "description": "近 5 分钟终态任务失败率超过 50%",
    },
    {
        "name": "task_queue_backlog", "kind": "metric", "level": "user", "metric": "task_queued",
        "comparator": "gt", "threshold": 10, "window_sec": 600, "for_sec": 600,
        "severity": "warning", "seed_source": "GrapeTaskQueueBacklog",
        "description": "排队任务超过 10 个且持续 10 分钟",
    },
    {
        "name": "all_runtimes_offline", "kind": "metric", "level": "platform",
        "metric": "runtimes_online",
        "comparator": "lte", "threshold": 0, "window_sec": 300, "for_sec": 600,
        "severity": "critical", "seed_source": "GrapeAllRuntimesOffline",
        "description": "全部引擎离线（平台级，L2 健康中心可见）",
    },
    {
        "name": "db_pool_usage", "kind": "metric", "level": "platform", "metric": "db_pool_usage",
        "comparator": "gt", "threshold": 0.8, "window_sec": 300, "for_sec": 300,
        "severity": "warning", "seed_source": "GrapeDBPoolUsageHigh",
        "description": "MySQL 连接使用率超过 80%（平台级）",
    },
    {
        "name": "db_aborted_connects", "kind": "metric", "level": "platform",
        "metric": "db_aborted_delta",
        "comparator": "gt", "threshold": 5, "window_sec": 600, "for_sec": 300,
        "severity": "warning", "seed_source": "GrapeDBAbortedConnects",
        "description": "MySQL 中止连接 10 分钟内新增超过 5（平台级）",
    },
    {
        "name": "http_error_rate_5xx", "kind": "metric", "level": "platform", "metric": "http_5xx_rate",
        "comparator": "gt", "threshold": 0.1, "window_sec": 300, "for_sec": 300,
        "severity": "warning", "seed_source": "GrapeHTTPErrorRateHigh",
        "description": "HTTP 5xx 占比超过 10%（平台级）",
    },
    # ----- P1-3：数据缺失（"该报数却没报"）-----
    {
        "name": "runtime_silent", "kind": "gap", "level": "user", "metric": "gap_runtime",
        "comparator": "gt", "threshold": 900, "window_sec": 0, "for_sec": 0,
        "severity": "warning", "seed_source": "GrapeGapRuntimeSilent",
        "description": "引擎/设备超过 15 分钟没有心跳上报（可能是 worker 掉线或网络被挡）；"
                       "从未上报过的凭证不算缺失，超过 7 天没上报视为已退役不再报",
    },
    {
        "name": "repo_silent", "kind": "gap", "level": "user", "metric": "gap_repo",
        "comparator": "gt", "threshold": 3600, "window_sec": 21600, "for_sec": 0,
        "severity": "warning", "seed_source": "GrapeGapRepoSilent", "enabled": 0,
        "description": "近 6 小时有任务活动的仓库，最近 1 小时没有任何任务活动"
                       "（默认关闭：低活跃仓库天然长期无任务，开启前先看该仓库历史频率）",
    },
    # ----- P1-3：突变检测（环比，统计法不上 ML）-----
    {
        "name": "task_success_rate_drop", "kind": "metric", "level": "user",
        "metric": "task_success_rate_mutation",
        "comparator": "lt", "threshold": -0.2, "window_sec": 1800, "for_sec": 0,
        "severity": "warning", "seed_source": "GrapeTaskSuccessRateDrop",
        "description": "任务成功率环比上一窗口下跌超过 20 个百分点"
                       "（两个 30 分钟窗口各需 ≥3 个终态任务，样本不足不出指标）",
    },
]

_COMPARATORS = {
    "gt": lambda v, t: v > t,
    "gte": lambda v, t: v >= t,
    "lt": lambda v, t: v < t,
    "lte": lambda v, t: v <= t,
}

# 过程态（不落库）：条件首次满足的时刻 / 平台指标的差分基线
_pending: Dict[str, float] = {}
_baseline: Dict[str, Any] = {}

EVENT_RETENTION_DAYS = 90   # obs_alert_events 留存（只清已恢复的，见 cleanup_events）

STUCK_MINUTES = 22          # 与 dashboard.ops_overview / grape_task_stuck 一致
FAILURE_WINDOW_SEC = 300
#: 突变检测（P1-3）：环比窗口长度（与 task_success_rate_drop 的 window_sec 一致）
MUTATION_WINDOW_SEC = 1800
#: 突变检测最小样本：两个窗口各自至少这么多终态任务才出指标（防小样本误报）
MUTATION_MIN_SAMPLES = 3


def _event_source(rule: Dict[str, Any]) -> str:
    """事件来源标记（§6.9 统一事件视图：monitor / gap / mutation）。"""
    if (rule.get("kind") or "metric") == "gap":
        return "gap"
    return "mutation" if str(rule.get("metric") or "").endswith("_mutation") else "monitor"


# ---------------------------------------------------------------- 工具

def _now() -> datetime:
    return datetime.now(timezone.utc)


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


def _iso(dt: datetime) -> str:
    return dt.isoformat()


# ---------------------------------------------------------------- 种子 / 规则

def seed_rules() -> int:
    """幂等写入种子规则（按 name 判重，已存在不动，避免覆盖用户改过的阈值）。"""
    from src.session_manager import db, db_lock
    n = 0
    with db_lock:
        c = db.cursor()
        for r in SEED_RULES:
            try:
                c.execute("SELECT id FROM obs_alert_rules WHERE name=?", (r["name"],))
                if c.fetchone():
                    continue
                c.execute(
                    "INSERT INTO obs_alert_rules (name, kind, level, metric, comparator, threshold,"
                    " window_sec, for_sec, severity, dim_filter, enabled, notify_channels,"
                    " description, seed_source) VALUES (?,?,?,?,?,?,?,?,?,'',?,'',?,?)",
                    (r["name"], r.get("kind") or "metric", r["level"], r["metric"],
                     r["comparator"], r["threshold"], r["window_sec"], r["for_sec"],
                     r["severity"], int(r.get("enabled", 1)), r["description"], r["seed_source"]))
                n += 1
            except Exception as e:
                log.warning(f"obs seed rule {r['name']} failed: {e}")
        db.commit()
    return n


def list_rules(level: str = "") -> List[Dict[str, Any]]:
    """规则列表。level='user' 过滤用户面可见规则；空=全部（平台面用）。"""
    from src.session_manager import db, db_lock
    out: List[Dict[str, Any]] = []
    sql = ("SELECT id, name, kind, level, metric, comparator, threshold, window_sec, for_sec,"
           " severity, enabled, notify_channels, description, seed_source,"
           " repeat_sec, aggregate_mode, aggregate_window_sec, notify_window_json, escalate_json"
           " FROM obs_alert_rules")
    params: tuple = ()
    if level:
        sql += " WHERE level=?"
        params = (level,)
    sql += " ORDER BY level DESC, id"
    try:
        with db_lock:
            c = db.cursor()
            c.execute(sql, params)
            cols = [d[0] for d in c.description]
            out = [dict(zip(cols, r)) for r in c.fetchall()]
    except Exception as e:
        log.warning(f"obs list_rules failed: {e}")
    return out


def set_rule_enabled(rule_id: int, enabled: bool) -> bool:
    from src.session_manager import db, db_lock
    try:
        with db_lock:
            c = db.cursor()
            c.execute("UPDATE obs_alert_rules SET enabled=?, updated_at=? WHERE id=?",
                      (1 if enabled else 0, _iso(_now()), int(rule_id)))
            db.commit()
        return True
    except Exception as e:
        log.warning(f"obs set_rule_enabled failed: {e}")
        return False


def set_rule_threshold(rule_id: int, threshold: float) -> bool:
    from src.session_manager import db, db_lock
    try:
        with db_lock:
            c = db.cursor()
            c.execute("UPDATE obs_alert_rules SET threshold=?, updated_at=? WHERE id=?",
                      (float(threshold), _iso(_now()), int(rule_id)))
            db.commit()
        return True
    except Exception as e:
        log.warning(f"obs set_rule_threshold failed: {e}")
        return False


def set_rule_notify(rule_id: int, channels: str) -> bool:
    from src.session_manager import db, db_lock
    try:
        with db_lock:
            c = db.cursor()
            c.execute("UPDATE obs_alert_rules SET notify_channels=?, updated_at=? WHERE id=?",
                      (channels or "", _iso(_now()), int(rule_id)))
            db.commit()
        return True
    except Exception as e:
        log.warning(f"obs set_rule_notify failed: {e}")
        return False


def get_rule(rule_id: int) -> Dict[str, Any]:
    """按 id 取单条规则（含策略字段）。找不到返回 {}。"""
    for r in list_rules():
        if int(r.get("id") or 0) == int(rule_id):
            return r
    return {}


def set_rule_policy(rule_id: int, payload: Dict[str, Any]) -> Dict[str, Any]:
    """写策略字段（P1-1 / D1，供管理台 L2 接口调用）。

    只允许策略相关字段：severity / repeat_sec / aggregate_mode / aggregate_window_sec /
    notify_window_json / escalate_json / notify_channels / enabled。
    返回 {"ok":bool, "errors":[...], "applied":{...}}；**校验不通过不写库**。
    """
    from src.observability.alert_policy import sanitize_policy
    from src.session_manager import db, db_lock
    rule = get_rule(rule_id)
    if not rule:
        return {"ok": False, "errors": ["rule not found"], "applied": {}}
    clean, errors = sanitize_policy(payload or {}, rule)
    if errors:
        return {"ok": False, "errors": errors, "applied": {}}
    if not clean:
        return {"ok": True, "errors": [], "applied": {}}
    sets = ", ".join(f"{k}=?" for k in clean)
    try:
        with db_lock:
            c = db.cursor()
            c.execute(f"UPDATE obs_alert_rules SET {sets}, updated_at=? WHERE id=?",
                      tuple(clean.values()) + (_iso(_now()), int(rule_id)))
            db.commit()
        log.info(f"obs alert policy updated: rule={rule_id} {clean}")
        return {"ok": True, "errors": [], "applied": clean}
    except Exception as e:
        log.warning(f"obs set_rule_policy failed: {e}")
        return {"ok": False, "errors": [str(e)], "applied": {}}


# ---------------------------------------------------------------- 指标计算

def _repo_metrics() -> Dict[str, Dict[str, float]]:
    """按仓库维度的指标：卡住数 / 失败率 / 排队数 / 成功率环比突变。

    **降载（2026-10-17 审计）**：原实现 `SELECT ... FROM agent_tasks WHERE status IN (...)`
    把**全表**（只有 6 个状态枚举做条件，索引形同虚设，EXPLAIN rows=全表/ filtered=100%）
    捞进 Python 每 30s 算一遍，并全程持全局 `db_lock` —— 实测把其它 DB 使用者的中位延迟
    从 1.1ms 抬到 9.6ms，且随任务量线性变差。现改为**3 条 GROUP BY 聚合查询**，
    只回每仓库一行；口径与原来逐行算完全一致（见下方注释与用例）。

    时间比较用**字符串比较**（`agent_tasks` 的时间列由本仓统一写 ISO(UTC)，字典序=时间序）：
    只有非空且形如 ISO 的值参与比较，语义与原来的 `datetime` 解析+窗口判断相同。

    **维度集合的语义变化（唯一一处，且对现有规则无影响）**：原来不管窗口，只要仓库有过
    这 6 种状态的任务就会出现该维度（即使是很久以前的、指标全 0 的仓库）；现在只出
    "近 1 小时有终态任务 或 当前有排队/在跑任务"的仓库。现有 10 条种子规则都是 `gt` 且阈值 ≥ 0
    （全 0 指标本来就不会触发），所以行为一致；**若将来加 `lt` 阈值或负阈值规则，
    需要把维度集合重新放宽**（否则"样本为空的仓库"不会参与求值）。
    """
    from src.session_manager import db, db_lock
    now = _now()
    stuck_bound = _iso(now - timedelta(minutes=STUCK_MINUTES))
    fail_cutoff = _iso(now - timedelta(seconds=FAILURE_WINDOW_SEC))
    mut0_cutoff = _iso(now - timedelta(seconds=MUTATION_WINDOW_SEC))
    mut1_cutoff = _iso(now - timedelta(seconds=2 * MUTATION_WINDOW_SEC))

    def _row(m: Dict[str, float]) -> Dict[str, float]:
        return m

    out: Dict[str, Dict[str, float]] = {}
    try:
        with db_lock:
            c = db.cursor()
            # ① 在跑/排队：卡住数（dispatched/running 且 started_at 早于阈值）+ 排队数
            c.execute(
                "SELECT repo_key,"
                " SUM(CASE WHEN status='queued' THEN 1 ELSE 0 END),"
                " SUM(CASE WHEN status IN ('dispatched','running')"
                "          AND started_at IS NOT NULL AND started_at != '' AND started_at < ?"
                "     THEN 1 ELSE 0 END)"
                " FROM agent_tasks WHERE repo_key IS NOT NULL AND repo_key != ''"
                "   AND (status='queued' OR status IN ('dispatched','running'))"
                " GROUP BY repo_key", (stuck_bound,))
            for repo_key, queued, stuck in c.fetchall():
                m = out.setdefault(repo_key, {"task_stuck": 0.0, "task_queued": 0.0,
                                              "_done": 0.0, "_bad": 0.0,
                                              "_mut0_tot": 0.0, "_mut0_bad": 0.0,
                                              "_mut1_tot": 0.0, "_mut1_bad": 0.0})
                _row(m)
                m["task_queued"] += float(queued or 0)
                m["task_stuck"] += float(stuck or 0)
            # ② 终态任务按仓库×状态计数，带**时间下界**（失败率窗口）——聚合在 SQL 侧做
            c.execute(
                "SELECT repo_key, status, COUNT(*) FROM agent_tasks"
                " WHERE repo_key IS NOT NULL AND repo_key != ''"
                "   AND status IN ('completed','failed','cancelled')"
                "   AND completed_at IS NOT NULL AND completed_at != '' AND completed_at >= ?"
                " GROUP BY repo_key, status", (fail_cutoff,))
            for repo_key, status, n in c.fetchall():
                m = out.setdefault(repo_key, {"task_stuck": 0.0, "task_queued": 0.0,
                                              "_done": 0.0, "_bad": 0.0,
                                              "_mut0_tot": 0.0, "_mut0_bad": 0.0,
                                              "_mut1_tot": 0.0, "_mut1_bad": 0.0})
                cnt = float(n or 0)
                m["_done"] += cnt
                if (status or "").lower() != "completed":
                    m["_bad"] += cnt
            # ③ 突变检测的两个相邻窗口（本窗口 / 上一窗口），同样 SQL 侧计数
            c.execute(
                "SELECT repo_key, status,"
                " SUM(CASE WHEN completed_at > ? THEN 1 ELSE 0 END),"
                " SUM(CASE WHEN completed_at > ? AND completed_at < ? THEN 1 ELSE 0 END)"
                " FROM agent_tasks WHERE repo_key IS NOT NULL AND repo_key != ''"
                "   AND status IN ('completed','failed','cancelled')"
                "   AND completed_at IS NOT NULL AND completed_at != '' AND completed_at >= ?"
                " GROUP BY repo_key, status", (mut0_cutoff, mut1_cutoff, mut0_cutoff, mut1_cutoff))
            for repo_key, status, n0, n1 in c.fetchall():
                m = out.setdefault(repo_key, {"task_stuck": 0.0, "task_queued": 0.0,
                                              "_done": 0.0, "_bad": 0.0,
                                              "_mut0_tot": 0.0, "_mut0_bad": 0.0,
                                              "_mut1_tot": 0.0, "_mut1_bad": 0.0})
                bad = (status or "").lower() != "completed"
                m["_mut0_tot"] += float(n0 or 0)
                m["_mut1_tot"] += float(n1 or 0)
                if bad:
                    m["_mut0_bad"] += float(n0 or 0)
                    m["_mut1_bad"] += float(n1 or 0)
    except Exception as e:
        log.warning(f"obs repo metrics failed: {e}")
    for m in out.values():
        tot = m["_done"]
        m["task_failure_rate"] = round(m["_bad"] / tot, 4) if tot else 0.0
        # 环比突变：本窗口成功率 − 上一窗口成功率（负值=变差）。样本不足则**不出指标**
        # （该维度本轮直接跳过，避免 1~2 个任务的窗口把噪声当突变）
        if m["_mut0_tot"] >= MUTATION_MIN_SAMPLES and m["_mut1_tot"] >= MUTATION_MIN_SAMPLES:
            r0 = 1.0 - m["_mut0_bad"] / m["_mut0_tot"]
            r1 = 1.0 - m["_mut1_bad"] / m["_mut1_tot"]
            m["task_success_rate_mutation"] = round(r0 - r1, 4)
        else:
            m.pop("task_success_rate_mutation", None)
    return out


def _pool_usage(threads_connected, max_connections) -> float:
    """连接使用率 = 当前连接数 / **配置上限**（`max_connections`）。

    ⚠ 不要用 `Max_used_connections` 当分母：那是**历史峰值**（高水位），不是上限。稳态服务器上
    当前连接数迟早逼近峰值 → 比值长期贴着 1 → **永久误报**。2026-10-17 实测踩到：
    `Threads_connected=74 / Max_used_connections=78 = 93.6%` 触发告警，而配置上限是 8000
    （真实占用 0.93%）；`deploy/alerts.yml` 里同一个口径错误是从 multica 带过来的。

    取不到上限（0/None）时返回 0.0 —— **未知就不告警**，不能用峰值兜底再造一个假警报。
    """
    try:
        tc = int(threads_connected or 0)
        mc = int(max_connections or 0)
    except (TypeError, ValueError):
        return 0.0
    if mc <= 0:
        return 0.0
    return round(tc / mc, 6)


def _platform_metrics() -> Dict[str, float]:
    """平台级指标（L2/L3 用）：引擎在线数、DB 连接池、DB 中止连接增量、HTTP 5xx 占比。"""
    vals: Dict[str, float] = {}
    now_ts = time.time()

    # 全部 runtime 在线数（health 判定与 dashboard 同源）
    try:
        from src.session_manager import db, db_lock
        from src.runtime.health import derive_runtime_health
        online = 0
        with db_lock:
            c = db.cursor()
            c.execute("SELECT status, last_seen_at FROM runtime_credentials WHERE status != 'revoked'")
            for status, last_seen in c.fetchall():
                try:
                    if derive_runtime_health(status or "offline", last_seen, now_ts) == "online":
                        online += 1
                except Exception:
                    pass
        vals["runtimes_online"] = float(online)
    except Exception as e:
        log.warning(f"obs platform runtimes_online failed: {e}")

    # MySQL 状态：连接使用率 + 中止连接增量
    try:
        from src.session_manager import _is_mysql_backend, db, db_lock
        if _is_mysql_backend():
            with db_lock:
                c = db.cursor()
                c.execute("SHOW STATUS WHERE Variable_name IN "
                          "('Threads_connected','Max_used_connections','Aborted_connects')")
                status = {r[0]: int(r[1]) for r in c.fetchall()}
                _max_conn = 0
                try:
                    c.execute("SHOW VARIABLES WHERE Variable_name='max_connections'")
                    _r = c.fetchone()
                    _max_conn = int(_r[1]) if _r and _r[1] is not None else 0
                except Exception:
                    _max_conn = 0
            tc = status.get("Threads_connected", 0)
            # 分母用**配置上限**（不是 Max_used_connections 历史峰值）
            vals["db_pool_usage"] = _pool_usage(tc, _max_conn)
            if _max_conn <= 0:
                log.warning("obs alert: 读不到 MySQL max_connections，db_pool_usage 记 0（不告警）")
            ab = status.get("Aborted_connects", 0)
            base = _baseline.get("aborted")
            if base and now_ts - base[0] >= 600:
                vals["db_aborted_delta"] = float(max(0, ab - base[1]))
                _baseline["aborted"] = (now_ts, ab)
            elif not base:
                _baseline["aborted"] = (now_ts, ab)
    except Exception as e:
        log.warning(f"obs platform db metrics failed: {e}")

    # HTTP 5xx 占比（进程内计数器差分；读数走 prometheus_client 私有结构，失败即跳过）
    try:
        from src.observability import HTTP_REQUESTS_TOTAL
        c5x = tot = 0
        for labels, counter in list(HTTP_REQUESTS_TOTAL._metrics.items()):
            st = str(labels[2]) if len(labels) > 2 else ""
            v = counter._value.get()
            tot += v
            if st.startswith("5"):
                c5x += v
        base = _baseline.get("http")
        if base and now_ts - base[0] >= 300:
            d_tot = tot - base[1]
            d_5x = c5x - base[2]
            vals["http_5xx_rate"] = round(d_5x / d_tot, 4) if d_tot > 0 else 0.0
            _baseline["http"] = (now_ts, tot, c5x)
        elif not base:
            _baseline["http"] = (now_ts, tot, c5x)
    except Exception as e:
        log.debug(f"obs platform http metrics skipped: {e}")

    return vals


# ---------------------------------------------------------------- 求值

def cleanup_events(days: int = EVENT_RETENTION_DAYS) -> int:
    """清理超过留存期的告警事件，返回删除行数。

    **只删 `status='resolved'`（已恢复）的事件**：仍在 `firing` 的事件即使很旧也必须保留——
    它是"这个维度当前还在告警"的唯一依据，删了会导致重复开一条新事件（还会重发通知）。
    时间比较用 ISO 字符串（`resolved_at` 由本模块统一写 ISO(UTC)，字典序=时间序）。
    """
    from src.session_manager import db, db_lock
    cutoff = _iso(_now() - timedelta(days=days))
    try:
        with db_lock:
            c = db.cursor()
            c.execute("DELETE FROM obs_alert_events WHERE status='resolved'"
                      " AND resolved_at IS NOT NULL AND resolved_at != '' AND resolved_at < ?",
                      (cutoff,))
            n = int(c.rowcount or 0)
            db.commit()
        if n:
            log.info(f"obs alert events cleanup: removed {n} resolved events older than {days}d")
        return n
    except Exception as e:
        log.warning(f"obs alert events cleanup failed: {e}")
        return 0


def _open_events_cache(rule_ids: Optional[List[int]] = None) -> Dict[Tuple[int, str], Dict[str, Any]]:
    """整轮一次性预取"开放（firing）事件" → {(rule_id, dim_key): ev}。

    降载（2026-10-17 审计）：原来每个 (规则, 维度) 都发一条查询（10 条规则 × 若干仓库 ≈ 39 条/轮）
    外加每条规则一次策略扫描，现在整轮一次查询 + 内存查表。数据口径不变（都取 status='firing'，
    同一 (规则,维度) 取 id 最大的一条，与原来 `ORDER BY id DESC LIMIT 1` 一致）。
    调用方在**同一轮内**改动了开放事件（新建/收口）时必须同步增删本缓存，否则策略扫描会看到过期状态。
    """
    from src.session_manager import db, db_lock
    out: Dict[Tuple[int, str], Dict[str, Any]] = {}
    try:
        with db_lock:
            c = db.cursor()
            sql = ("SELECT id, rule_id, dim_key, repo_key, first_fired_at, last_seen_at, value,"
                   " silenced_until, last_notified_at, escalated_at, notify_deferred, notify_count"
                   " FROM obs_alert_events WHERE status='firing'")
            params: List[Any] = []
            if rule_ids:
                ph = ",".join("?" * len(rule_ids))
                sql += f" AND rule_id IN ({ph})"
                params = [int(x) for x in rule_ids]
            sql += " ORDER BY id ASC"
            c.execute(sql, tuple(params))
            for row in c.fetchall():
                out[(int(row[1]), str(row[2]))] = {
                    "id": row[0], "dim_key": row[2], "repo_key": row[3], "first_fired_at": row[4],
                    "last_seen_at": row[5], "value": row[6], "silenced_until": row[7] or "",
                    "last_notified_at": row[8] or "", "escalated_at": row[9] or "",
                    "notify_deferred": int(row[10] or 0), "notify_count": int(row[11] or 0)}
    except Exception as e:
        log.warning(f"obs open events prefetch failed: {e}")
    return out


def _open_event(c, rule_id: int, dim_key: str,
                cache: Optional[Dict[Tuple[int, str], Dict[str, Any]]] = None) -> Optional[Dict[str, Any]]:
    if cache is not None:
        return cache.get((int(rule_id), str(dim_key)))
    c.execute("SELECT id, first_fired_at, last_seen_at, value, silenced_until,"
              " last_notified_at, escalated_at, notify_deferred, notify_count"
              " FROM obs_alert_events WHERE rule_id=? AND dim_key=? AND status='firing'"
              " ORDER BY id DESC LIMIT 1", (int(rule_id), dim_key))
    row = c.fetchone()
    if not row:
        return None
    return {"id": row[0], "first_fired_at": row[1], "last_seen_at": row[2],
            "value": row[3], "silenced_until": row[4] or "",
            "last_notified_at": row[5] or "", "escalated_at": row[6] or "",
            "notify_deferred": int(row[7] or 0), "notify_count": int(row[8] or 0)}


def _resolve_open(c, rule_id: int, dim_key: str, now: datetime,
                  cache: Optional[Dict[Tuple[int, str], Dict[str, Any]]] = None) -> bool:
    """把该 (规则, 维度) 的开放事件收口为 resolved。返回是否有事件被收口。

    指标求值与数据缺失探测共用（gap 探测在 gap.py 里调用）。传了 `cache`（整轮预取）时就地
    查表并把该条目**移除**，保证同一轮后续的策略扫描不会看到已收口的过期状态。
    """
    if cache is not None:
        ev = cache.pop((int(rule_id), str(dim_key)), None)
        row = (ev["id"],) if ev else None
    else:
        c.execute("SELECT id FROM obs_alert_events WHERE rule_id=? AND dim_key=? AND status='firing'"
                  " ORDER BY id DESC LIMIT 1", (int(rule_id), dim_key))
        row = c.fetchone()
    if not row:
        return False
    now_iso = _iso(now)
    c.execute("UPDATE obs_alert_events SET status='resolved', resolved_at=?, last_seen_at=?,"
              " updated_at=? WHERE id=?", (now_iso, now_iso, now_iso, int(row[0])))
    return True


def evaluate_rules() -> Dict[str, Any]:
    """求值一轮：返回 {fired, resolved, evaluated, skipped}。由进程内定时器每 30s 调用。"""
    from src.session_manager import db, db_lock
    now = _now()
    now_iso = _iso(now)
    fired = resolved = 0
    evaluated = skipped = 0
    new_events: List[Dict[str, Any]] = []
    _seen_ids: set = set()          # 本轮已进入通知候选的事件 id（防同一 tick 重复投递）

    try:
        rules = [r for r in list_rules() if int(r.get("enabled") or 0) == 1]
    except Exception as e:
        log.warning(f"obs evaluate: load rules failed: {e}")
        return {"fired": 0, "resolved": 0, "evaluated": 0, "skipped": 0}

    repo_m = _repo_metrics()
    plat_m = _platform_metrics()

    # 整轮预取"开放事件"：1 条查询替代原来的「每(规则,维度) 1 条 + 每规则 1 条策略扫描」
    # 全部启用规则（含 gap 规则）：gap 探测共用同一份缓存，两边都不再逐维度发查询
    _enabled_ids = [int(r["id"]) for r in rules]
    _open_cache = _open_events_cache(_enabled_ids)
    _open_by_rule: Dict[int, Dict[str, Dict[str, Any]]] = {}
    for (_rid, _dim), _ev in _open_cache.items():
        _open_by_rule.setdefault(_rid, {})[_dim] = _ev

    with db_lock:
        c = db.cursor()
        for r in rules:
            if (r.get("kind") or "metric") == "gap":
                continue                     # 数据缺失规则由 gap.probe_all() 探测
            name = r["name"]
            rule_id = int(r["id"])
            metric = r["metric"]
            thr = float(r["threshold"])
            cmp_fn = _COMPARATORS.get(r["comparator"] or "gt")
            if not cmp_fn:
                skipped += 1
                continue

            if r.get("level") == "platform":
                dims = [("global", "")]
                getter = lambda _rk, m=metric: plat_m.get(m)          # noqa: E731
            else:
                # 用户面规则：逐个有活动的仓库求值；无活动的仓库不产生事件
                dims = [(f"repo:{rk}", rk) for rk in repo_m.keys()]
                getter = lambda rk, m=metric: repo_m.get(rk, {}).get(m)  # noqa: E731

            for dim_key, repo_key in dims:
                val = getter(repo_key)
                evaluated += 1
                key = f"{name}|{dim_key}"

                if val is None:
                    skipped += 1
                    continue

                hit = cmp_fn(float(val), thr)
                if hit:
                    first = _pending.get(key)
                    if first is None:
                        _pending[key] = now.timestamp()
                        first = _pending[key]
                    open_ev = _open_event(c, rule_id, dim_key, _open_cache)
                    if open_ev is None:
                        if now.timestamp() - first >= float(r["for_sec"] or 0):
                            c.execute(
                                "INSERT INTO obs_alert_events (rule_id, rule_name, level, severity,"
                                " status, source, metric, value, threshold, dim_key, repo_key,"
                                " first_fired_at, last_seen_at) VALUES (?,?,?,?,'firing',?,?,?,?,?,?,?,?)",
                                (rule_id, name, r.get("level") or "user", r.get("severity") or "warning",
                                 _event_source(r), metric, float(val), thr, dim_key, repo_key,
                                 now_iso, now_iso))
                            fired += 1
                            ev_id = c.lastrowid
                            db.commit()
                            # 同步缓存：同一轮的策略扫描要能看到本轮新建的开放事件
                            if ev_id:
                                _open_cache[(rule_id, dim_key)] = {
                                    "id": ev_id, "dim_key": dim_key, "repo_key": repo_key,
                                    "first_fired_at": now_iso, "last_seen_at": now_iso,
                                    "value": float(val), "silenced_until": "",
                                    "last_notified_at": "", "escalated_at": "",
                                    "notify_deferred": 0, "notify_count": 0}
                                _open_by_rule.setdefault(rule_id, {})[dim_key] = _open_cache[(rule_id, dim_key)]
                            if ev_id:
                                _seen_ids.add(int(ev_id))
                            # 通知不在这里做（本函数跑在线程里）：把新事件交回 async 循环去推送
                            new_events.append({
                                "id": ev_id, "rule_name": name, "level": r.get("level") or "user",
                                "severity": r.get("severity") or "warning", "metric": metric,
                                "value": float(val), "threshold": thr, "dim_key": dim_key,
                                "repo_key": repo_key, "metric_name": metric,
                                "first_fired_at": now_iso, "silenced_until": "",
                            })
                    else:
                        c.execute("UPDATE obs_alert_events SET value=?, last_seen_at=?, updated_at=?"
                                  " WHERE id=?", (float(val), now_iso, now_iso, open_ev["id"]))
                else:
                    _pending.pop(key, None)
                    open_ev = _open_event(c, rule_id, dim_key, _open_cache)
                    if open_ev is not None:
                        _open_cache.pop((rule_id, dim_key), None)
                        _open_by_rule.get(rule_id, {}).pop(dim_key, None)
                        c.execute("UPDATE obs_alert_events SET status='resolved', resolved_at=?,"
                                  " last_seen_at=?, updated_at=? WHERE id=?",
                                  (now_iso, now_iso, now_iso, open_ev["id"]))
                        resolved += 1

            # ---- 策略层（P1-1）：开放事件的「升级」与「通知时段恢复后补推」----
            try:
                # 走整轮预取缓存（等价于原"逐规则查一次 status='firing'"，但不再发查询）：
                # 缓存里同时含本轮新建的开放事件（新建时已写入），已收口的条目已移除。
                for _cached in list(_open_by_rule.get(rule_id, {}).values()):
                    open_ev = dict(_cached)
                    open_ev.update({"rule_name": name, "level": r.get("level") or "user",
                                    "metric": metric, "threshold": thr,
                                    "severity": r.get("severity") or "warning"})
                    if int(open_ev["id"]) in _seen_ids:
                        continue
                    esc = _policy.escalation_due(r, open_ev, now)
                    if esc:
                        c.execute("UPDATE obs_alert_events SET escalated_at=?, updated_at=?"
                                  " WHERE id=?", (now_iso, now_iso, int(open_ev["id"])))
                        open_ev["escalated"] = True
                        open_ev["severity"] = esc["severity"]
                        open_ev["_escalate_channels"] = esc.get("channels") or ""
                        _seen_ids.add(int(open_ev["id"]))
                        new_events.append(open_ev)
                        log.info(f"obs alert escalated: rule={name} dim={open_ev['dim_key']} "
                                 f"after={esc['after_sec']}s → {esc['severity']}")
                        continue
                    # 之前因通知时段只记录（notify_deferred=1）→ 窗口内补推一次；
                    # 或 repeat_sec 到期 / 从未通知过 → 重新提名（"还在告警"）
                    renominate = False
                    if int(open_ev.get("notify_deferred") or 0) == 1:
                        renominate = (not open_ev.get("last_notified_at")) and \
                            _policy.in_notify_window(
                                _policy.parse_notify_window(r.get("notify_window_json")), now)
                    elif _policy.repeat_due(r, open_ev, now):
                        renominate = True
                    if renominate:
                        _seen_ids.add(int(open_ev["id"]))
                        new_events.append(open_ev)
            except Exception as e:
                log.warning(f"obs alert policy scan failed (rule={name}): {e}")
        db.commit()

    if fired or resolved:
        log.info(f"obs alerts: evaluated={evaluated} fired={fired} resolved={resolved}")

    # 数据缺失探测（P1-3）：独立于指标阈值，走 gap.py（'该报数却没报'）
    gap_stats = {"gap_fired": 0, "gap_resolved": 0, "gap_evaluated": 0}
    try:
        from src.observability import gap as _gap
        gap_stats = _gap.probe_all(_open_cache)
    except Exception as e:
        log.warning(f"obs gap probe failed: {e}")

    return {"fired": fired + gap_stats.get("gap_fired", 0),
            "resolved": resolved + gap_stats.get("gap_resolved", 0),
            "evaluated": evaluated + gap_stats.get("gap_evaluated", 0),
            "skipped": skipped, "new_events": new_events,
            "metric_fired": fired, "metric_resolved": resolved,
            "gap_fired": gap_stats.get("gap_fired", 0),
            "gap_resolved": gap_stats.get("gap_resolved", 0)}


# ---------------------------------------------------------------- 通知

def _rule_by_name(name: str) -> Dict[str, Any]:
    for r in list_rules():
        if r.get("name") == name:
            return r
    return {}


def _recipients_for_event(event: Dict[str, Any]) -> List[tuple]:
    """事件的飞书收件人 [(user_id, open_id)]。

    - 仓库维度事件 → 该仓库的成员（user_repos）；全局维度（平台级）→ 所有已绑定飞书身份的用户
    - **必须有本人 chat bot 才能发**（open_id 是 app 级，用别人的 app 发会静默失败），
      没有绑定 open_id 的直接跳过
    - 上限 20 人（避免一次刷屏）
    """
    from src.api.issues_center import _get_feishu_open_id_by_user_id
    from src.session_manager import db, db_lock
    uids: List[int] = []
    try:
        with db_lock:
            c = db.cursor()
            rk = event.get("repo_key") or ""
            if rk:
                c.execute("SELECT DISTINCT user_id FROM user_repos WHERE repo_key=?", (rk,))
            elif event.get("repo_keys"):
                # 聚合汇总：多个仓库的收件人取并集
                rks = [str(x) for x in (event.get("repo_keys") or []) if str(x).strip()]
                ph = ",".join("?" for _ in rks)
                c.execute(f"SELECT DISTINCT user_id FROM user_repos WHERE repo_key IN ({ph})",
                          tuple(rks))
            else:
                c.execute("SELECT DISTINCT user_id FROM unified_user_mapping"
                          " WHERE feishu_open_id!='' AND user_id>0")
            uids = [int(r[0]) for r in c.fetchall() if r and r[0]]
    except Exception as e:
        log.warning(f"obs alert recipients lookup failed: {e}")
        return []
    out: List[tuple] = []
    for uid in uids[:20]:
        try:
            oid = _get_feishu_open_id_by_user_id(uid)
        except Exception:
            oid = ""
        if oid:
            out.append((uid, oid))
    return out


def _mark_notified(event_ids: List[int], now_iso: str) -> None:
    """记录"已通知"：次数 +1、上次通知时间、清延后标记（策略层状态）。"""
    if not event_ids:
        return
    from src.session_manager import db, db_lock
    try:
        with db_lock:
            c = db.cursor()
            for eid in event_ids:
                c.execute("UPDATE obs_alert_events SET notify_count=notify_count+1,"
                          " last_notified_at=?, notify_deferred=0, updated_at=? WHERE id=?",
                          (now_iso, now_iso, int(eid)))
            db.commit()
    except Exception as e:
        log.warning(f"obs mark notified failed: {e}")


def _mark_deferred(event_id: int, now_iso: str) -> None:
    """通知时段外：只记录，等窗口内再补推一次。"""
    from src.session_manager import db, db_lock
    try:
        with db_lock:
            c = db.cursor()
            c.execute("UPDATE obs_alert_events SET notify_deferred=1, updated_at=? WHERE id=?",
                      (now_iso, int(event_id)))
            db.commit()
    except Exception as e:
        log.warning(f"obs mark deferred failed: {e}")


async def _send_event_text(rule: Dict[str, Any], ev: Dict[str, Any], text: str,
                           stats: Dict[str, int]) -> bool:
    """按渠道投递一条文本；返回是否至少成功一次（stats 原地累加）。"""
    chans = (rule.get("notify_channels") or "").strip()
    did_send = False
    if "feishu" in chans:
        recipients = _recipients_for_event(ev)
        if not recipients:
            stats["no_recipient"] += 1
            log.info(f"obs alert feishu: 无可用收件人（仓库 {ev.get('repo_key') or 'global'} "
                     f"无成员绑定飞书），仅记录: {text}")
        for uid, open_id in recipients:
            try:
                from src.api.issues_center import _resolve_user_chat_bot
                ctx = _resolve_user_chat_bot(bm, uid) if (bm := _bot_manager()) else None
                api = bm.api if bm else None
                if not ctx or not api:
                    stats["skipped"] += 1
                    log.info(f"obs alert feishu: user={uid} 未配置/未连接本人 chat bot，跳过")
                    continue
                mid = await api.send_text_to_user(ctx, open_id, text)
                if mid:
                    stats["sent"] += 1
                    did_send = True
                else:
                    stats["failed"] += 1
            except Exception as e:
                stats["failed"] += 1
                log.warning(f"obs alert feishu send to user={uid} failed: {e}")
    if "webhook" in chans:
        try:
            _notify_webhook(rule, ev, text=text)
            did_send = True
        except Exception as e:
            stats["failed"] += 1
            log.warning(f"obs alert webhook failed: {e}")
    return did_send


def _bot_manager():
    try:
        from src.api import main as api_main
        return api_main.bot_manager
    except Exception:
        return None


async def notify_fired_events(events: List[Dict[str, Any]]) -> Dict[str, int]:
    """投递通知候选（新触发 / 升级 / 时段恢复补推）——**策略层在此生效**。

    每条事件的处置由 `alert_policy.decide_notify` 决定：
    - `skip`（无渠道/静默/重复抑制期内）→ 只记录
    - `defer`（通知时段外）→ 事件标 `notify_deferred=1`，窗口内恢复后补推一次
    - `aggregate`（聚合规则）→ 进进程内聚合缓冲，窗口结束发一条汇总（`flush_aggregates`）
    - `send` → 走渠道投递，成功后写 `last_notified_at`/`notify_count`（重复抑制依据）

    升级事件（`escalated=True`）用升级配置里的渠道，标题带「升级」与升级后的级别。
    """
    stats = {"sent": 0, "failed": 0, "skipped": 0, "no_recipient": 0,
             "deferred": 0, "aggregated": 0}
    if not events:
        return stats
    now = _now()
    now_iso = _iso(now)

    for ev in events:
        rule = _rule_by_name(ev.get("rule_name") or "")
        if ev.get("_escalate_channels"):
            rule = {**rule, "notify_channels": ev["_escalate_channels"]}
        action, reason = _policy.decide_notify(rule, ev, now)
        if action == "skip":
            stats["skipped"] += 1
            if reason == "no_channel" and int(ev.get("id") or 0):
                # 没渠道就别挂着"延后"标记，否则窗口内会反复进候选
                _mark_notified([], now_iso)
                try:
                    from src.session_manager import db as _db, db_lock as _lock
                    with _lock:
                        _c = _db.cursor()
                        _c.execute("UPDATE obs_alert_events SET notify_deferred=0 WHERE id=?",
                                   (int(ev["id"]),))
                        _db.commit()
                except Exception:
                    pass
            continue
        if action == "defer":
            stats["deferred"] += 1
            if int(ev.get("id") or 0):
                _mark_deferred(int(ev["id"]), now_iso)
            log.info(f"obs alert deferred (通知时段外，仅记录): {ev.get('rule_name')} "
                     f"{ev.get('dim_key')}")
            continue
        if action == "aggregate":
            stats["aggregated"] += 1
            _policy.AGGREGATOR.add(rule, ev, now)
            continue

        text = _event_text(ev, rule)
        did_send = await _send_event_text(rule, ev, text, stats)
        if did_send:
            log.info(f"obs alert notified: {text}")
            if int(ev.get("id") or 0):
                _mark_notified([int(ev["id"])], now_iso)
    return stats


async def flush_aggregates() -> Dict[str, int]:
    """发出已到期的聚合汇总（窗口结束）。由 30s 循环每轮调用。"""
    stats = {"sent": 0, "failed": 0, "no_recipient": 0, "summaries": 0}
    try:
        due = _policy.AGGREGATOR.due(_now())
    except Exception as e:
        log.warning(f"obs aggregate flush failed to collect: {e}")
        return stats
    if not due:
        return stats
    now_iso = _iso(_now())
    for g in due:
        rule = _rule_by_name(g.get("rule_name") or "") or {}
        if not (rule.get("notify_channels") or "").strip():
            stats["failed"] += 0
            log.info(f"obs aggregate summary (无渠道，仅记录): {_aggregate_text(g)}")
            continue
        text = _aggregate_text(g)
        # 汇总涉及多个仓库 → 收件人取并集
        ev_like = {"repo_key": "", "repo_keys": g.get("repos") or [],
                   "rule_name": g.get("rule_name"), "level": g.get("level")}
        did_send = await _send_event_text(rule, ev_like, text, stats)
        stats["summaries"] += 1
        if did_send:
            log.info(f"obs aggregate notified: {text}")
        _mark_notified([int(x) for x in (g.get("event_ids") or [])], now_iso)
    return stats


def _aggregate_text(g: Dict[str, Any]) -> str:
    sev = _policy.normalize_severity(g.get("severity"))
    tag = "·升级" if g.get("escalated") else ""
    dims = g.get("dims") or []
    shown = "、".join(d.split(":", 1)[-1] for d in dims[:8])
    more = f" 等 {len(dims)} 处" if len(dims) > 8 else ""
    mode = g.get("mode")
    mode_cn = {"rule": "同规则窗口合并", "period": "周期汇总"}.get(str(mode), str(mode))
    maxv = g.get("max_value")
    return (f"[Grape 告警·汇总{tag}·{sev}] {g.get('rule_name')}（{mode_cn}）\n"
            f"受影响 {len(dims)} 个维度：{shown}{more}\n"
            f"最大当前值 {maxv}（阈值 {g.get('threshold')}）")


async def send_test_notification(user: Dict[str, Any]) -> Dict[str, Any]:
    """给调用者本人发一条测试告警（验证飞书通道是否真的通）。"""
    uid = int(user.get("id") or 0)
    from src.api.issues_center import _get_feishu_open_id_by_user_id, _resolve_user_chat_bot
    open_id = ""
    try:
        open_id = _get_feishu_open_id_by_user_id(uid) or ""
    except Exception:
        open_id = ""
    if not open_id:
        return {"ok": False, "reason": "no_open_id",
                "message": "本人未绑定飞书身份（unified_user_mapping.feishu_open_id 为空）——"
                           "请先在「Chat Agent」页扫码配置飞书"}
    try:
        from src.api import main as api_main
        bm = api_main.bot_manager
    except Exception:
        bm = None
    ctx = _resolve_user_chat_bot(bm, uid) if bm else None
    if not ctx:
        return {"ok": False, "reason": "no_bot",
                "message": "本人 chat bot 未连接（chat_agent_session.im_app_id 为空或 bot 不在内存）——"
                           "请先在「Chat Agent」页扫码配置飞书"}
    api = bm.api if bm else None
    if not api:
        return {"ok": False, "reason": "no_api", "message": "IM 客户端未初始化"}
    text = ("[Grape 告警·测试] 这是一条测试告警，用于验证飞书通知通道。"
            f"链路：{ctx.app_id[:20]} → 你的飞书私聊。看到即可忽略。")
    try:
        mid = await api.send_text_to_user(ctx, open_id, text)
    except Exception as e:
        return {"ok": False, "reason": "send_failed", "message": f"发送异常：{e}"}
    if not mid:
        return {"ok": False, "reason": "send_failed",
                "message": "飞书接口未返回 message_id（token 或权限问题），请查服务端日志"}
    return {"ok": True, "app_id": ctx.app_id[:20], "message_id": mid,
            "message": "测试告警已发出，请查看你的飞书私聊"}


def _event_text(ev: Dict[str, Any], rule: Dict[str, Any]) -> str:
    rk = ev.get("repo_key") or ""
    where = rk.split(":", 1)[1] if ":" in rk else (rk or "平台")
    desc = (rule or {}).get("description") or ""
    val = ev.get("value")
    m = str(ev.get("metric") or "")
    if val is not None and ("rate" in m or "usage" in m):
        try:
            val = f"{round(float(val) * 100, 1)}%"
        except Exception:
            pass
    sev = _policy.normalize_severity(ev.get("severity"))
    tag = "·升级" if ev.get("escalated") else ""
    return (f"[Grape 告警{tag}·{sev}] {ev.get('rule_name')} @ {where}\n"
            f"当前值 {val}（阈值 {ev.get('threshold')}）\n{desc}")


def _notify_webhook(rule: Dict[str, Any], event: Dict[str, Any], text: str = "") -> None:
    """投递到 `OBS_ALERT_WEBHOOK_URL`（IM 自定义机器人入站 webhook，飞书即为该形态）。"""
    import json as _json
    import os as _os
    import urllib.request as _urlreq
    hook = (_os.environ.get("OBS_ALERT_WEBHOOK_URL") or "").strip()
    text = text or _event_text(event, rule)
    if not hook:
        log.info(f"obs alert[webhook] 未配置 OBS_ALERT_WEBHOOK_URL，仅记录: {text}")
        return
    body = _json.dumps({"msg_type": "text", "content": {"text": text}}).encode("utf-8")
    req = _urlreq.Request(hook, data=body, headers={"Content-Type": "application/json"})
    _urlreq.urlopen(req, timeout=5).read()


# ---------------------------------------------------------------- 用户面查询

def firing_summary(user: Dict[str, Any]) -> Dict[str, Any]:
    """总览横幅用：可见范围内的 firing 数 + 最近一条。"""
    events = list_events(user, status="firing", limit=20)
    latest = events[0] if events else None
    critical = sum(1 for e in events
                   if _policy.severity_rank(e.get("severity")) <= _policy.SEVERITY_RANK["severe"])
    by_sev = {s: 0 for s in _policy.SEVERITIES}
    for e in events:
        by_sev[_policy.normalize_severity(e.get("severity"))] += 1
    esc = sum(1 for e in events if (e.get("escalated_at") or "").strip())
    deferred = sum(1 for e in events if int(e.get("notify_deferred") or 0) == 1)
    return {"firing": len(events), "critical": critical, "latest": latest,
            "escalated": esc, "deferred": deferred, "by_severity": by_sev}


def visible_runtime_dim_keys(user: Dict[str, Any], repo_keys: List[str]) -> List[str]:
    """用户可见设备对应的 `dim_key`（`runtime:<id>`）—— 设备维度事件（数据缺失）的可见性口径。

    与设备列表/资源曲线同一口径（`overview._cred_visibility`），避免"设备看得见、它的静默事件看不见"。
    """
    from src.observability.overview import _cred_visibility
    from src.session_manager import db, db_lock
    frag, params = _cred_visibility(user, list(repo_keys or []))
    ids: List[int] = []
    try:
        with db_lock:
            c = db.cursor()
            c.execute("SELECT id FROM runtime_credentials WHERE status != 'revoked'"
                      + frag, tuple(params))
            ids = [int(r[0]) for r in c.fetchall()]
    except Exception as e:
        log.warning(f"obs visible runtimes failed: {e}")
    return [f"runtime:{i}" for i in ids]


def list_events(user: Dict[str, Any], status: str = "", limit: int = 50,
                repo: str = "", q: str = "") -> List[Dict[str, Any]]:
    """用户面事件列表：**只出 level='user'** 且落在调用者可见范围内的事件。

    两类可见性（并集）：
    - 仓库维度事件：`repo_key` 在可见仓库内
    - 设备维度事件（数据缺失 `runtime:<id>`）：该设备在本人口径下可见
    """
    from src.observability.overview import RepoNotVisible, visible_repo_keys
    from src.session_manager import db, db_lock

    visible = set(visible_repo_keys(user))
    if repo:
        from src.repo_key import to_repo_key
        want = {to_repo_key(repo), to_repo_key(repo, "github")}
        hit = sorted(want & visible)
        if not hit:
            raise RepoNotVisible(repo)
        visible = set(hit)
    if not visible:
        return []

    dim_keys = [] if repo else visible_runtime_dim_keys(user, sorted(visible))
    ph = ",".join("?" * len(visible))
    where = f"level='user' AND (repo_key IN ({ph})"
    params: List[Any] = list(sorted(visible))
    if dim_keys:
        dph = ",".join("?" * len(dim_keys))
        # 注意：这里**不能写** `LIKE 'runtime:%'`——SQL 里出现字面 `%` 且同时带参数时，
        # pymysql 会把它当格式符（unsupported format character / not enough arguments），
        # 异常又被本函数的 try 吞掉 → 用户面事件列表会**静默丢掉全部告警类事件**（真机踩到）。
        # 用 substr 判定前缀，两个后端都支持，且没有 `%`。
        where += f" OR (dim_key IN ({dph}) AND substr(dim_key, 1, 8) = 'runtime:')"
        params.extend(dim_keys)
    where += ")"
    sql = ("SELECT id, rule_name, level, severity, status, source, metric, value, threshold,"
           " dim_key, repo_key, first_fired_at, last_seen_at, resolved_at, silenced_until,"
           " notify_count, last_notified_at, escalated_at, notify_deferred"
           " FROM obs_alert_events"
           f" WHERE {where}")
    if status:
        sql += " AND status=?"
        params.append(status)
    if q:
        # 关键字检索：`LIKE ?` + 参数绑定（SQL 文本里不出现字面 `%`，见上面 substr 那段注释）
        sql += " AND (rule_name LIKE ? OR dim_key LIKE ?)"
        params.extend([f"%{q}%"] * 2)
    sql += " ORDER BY last_seen_at DESC, id DESC LIMIT ?"
    params.append(int(limit))
    out: List[Dict[str, Any]] = []
    try:
        with db_lock:
            c = db.cursor()
            c.execute(sql, tuple(params))
            cols = [d[0] for d in c.description]
            out = [dict(zip(cols, r)) for r in c.fetchall()]
    except Exception as e:
        log.warning(f"obs list_events failed: {e}")
    return out


def list_events_all(status: str = "", limit: int = 100, repo: str = "",
                    q: str = "") -> List[Dict[str, Any]]:
    """**L2 专用**：全局事件列表（不过滤可见范围；默认含平台级事件）。

    与用户面 `list_events` 的字段投影保持一致（统一事件视图两端同口径）；`repo` 传短名时
    只出该仓库（**不做可见性校验**——调用方必须是管理台鉴权的接口）。
    """
    from src.session_manager import db, db_lock
    sql = ("SELECT id, rule_name, level, severity, status, source, metric, value, threshold,"
           " dim_key, repo_key, first_fired_at, last_seen_at, resolved_at, silenced_until,"
           " notify_count, last_notified_at, escalated_at, notify_deferred"
           " FROM obs_alert_events WHERE 1=1")
    params: List[Any] = []
    if repo:
        from src.repo_key import to_repo_key
        sql += " AND repo_key IN (?,?)"
        params.extend([to_repo_key(repo), to_repo_key(repo, "github")])
    if status:
        sql += " AND status=?"
        params.append(status)
    if q:
        sql += " AND (rule_name LIKE ? OR dim_key LIKE ?)"
        params.extend([f"%{q}%"] * 2)
    sql += " ORDER BY last_seen_at DESC, id DESC LIMIT ?"
    params.append(int(limit))
    out: List[Dict[str, Any]] = []
    try:
        with db_lock:
            c = db.cursor()
            c.execute(sql, tuple(params))
            cols = [d[0] for d in c.description]
            out = [dict(zip(cols, r)) for r in c.fetchall()]
    except Exception as e:
        log.warning(f"obs list_events_all failed: {e}")
    return out


def silence_event(event_id: int, minutes: int, user: Dict[str, Any]) -> bool:
    """静默一个事件：只影响通知，事件仍记录；越权（非可见仓库/设备）不动。"""
    from src.observability.overview import visible_repo_keys
    from src.session_manager import db, db_lock
    visible = set(visible_repo_keys(user))
    dim_keys = set(visible_runtime_dim_keys(user, sorted(visible)))
    try:
        until = _iso(_now() + timedelta(minutes=max(1, int(minutes))))
        with db_lock:
            c = db.cursor()
            c.execute("SELECT repo_key, level, dim_key FROM obs_alert_events WHERE id=?",
                      (int(event_id),))
            row = c.fetchone()
            if not row:
                return False
            repo_key, lvl, dim_key = row[0] or "", (row[1] or "user"), row[2] or ""
            if lvl != "user":
                return False        # 平台级：静默不了（等价 404）
            if repo_key not in visible and dim_key not in dim_keys:
                return False        # 他人仓库 / 他人设备
            c.execute("UPDATE obs_alert_events SET silenced_until=?, updated_at=? WHERE id=?",
                      (until, _iso(_now()), int(event_id)))
            db.commit()
        return True
    except Exception as e:
        log.warning(f"obs silence_event failed: {e}")
        return False