"""能效看板聚合（Flow Efficiency Board）——纯聚合，无 HTTP 依赖。

Issue #1229 的升级版：从「按协作者列数字」升级为「看工作流入 / 积压 / 周期 / 瓶颈」。
本模块只依赖一个数据库 cursor，不 import FastAPI，便于：
1. 单测直接喂 SQLite 建的表；
2. 后续阶段（可观测性插件化）把这批函数注册成 resolver，被看板 / 导出 / 三方平台复用。

口径铁律（详见 docs/weekly-10/efficiency-board-redesign-2026-11-09.md）：
- 时间：`issue.created_at` / `completed_at` 是无时区本地时间（Asia/Shanghai）；
  所有"现在"类计算以服务端当前时间对齐 CST，不用浏览器时间。
- 计数：一律排除 `is_subtask_mode=1` 的子任务行（归属父需求，不重复计）。
- 完成：`status='completed'` 且有 `completed_at`（用于周期/吞吐）；
  `cancelled` 单独统计，不计入"完成"。
- 周：ISO 周，周一起算。
- 分位数：nearest-rank（与 src/observability/overview.py::_p95 同风格）。
"""
import re
from bisect import bisect_right
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Sequence

_CST = timezone(timedelta(hours=8))

# 未关闭状态集合：除 completed / cancelled 外都算在制工作项
_CLOSED_STATUSES = ("completed", "cancelled")

# 积压龄期分桶（小时上界，None = 无上界）。P1 只用它算 ⑤ 超龄计数，
# P2 的 ⑧ 积压龄期分布复用同一份边界，避免两处口径漂移。
AGING_BUCKETS = [
    ("<1小时", 1.0),
    ("1-4小时", 4.0),
    ("4-8小时", 8.0),
    ("8-24小时", 24.0),
    ("1-3天", 24.0 * 3),
    ("3-7天", 24.0 * 7),
    ("1-2周", 24.0 * 14),
    (">2周", None),
]

# 任务交付时长分桶（分钟上界，None = 无上界）——P3 的 ⑩ 用
DELIVERY_BUCKETS_MIN = [
    ("<1分钟", 1.0),
    ("1-5分钟", 5.0),
    ("5-15分钟", 15.0),
    ("15-60分钟", 60.0),
    ("1-3小时", 180.0),
    (">3小时", None),
]


# ---------------------------------------------------------------- 时间工具

def parse_local_ts(value: Any) -> Optional[datetime]:
    """解析库里存的时间文本为 naive 本地时间；无法解析返回 None。"""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.replace(tzinfo=None)
    s = str(value).strip()
    if not s:
        return None
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        d = datetime.fromisoformat(s)
    except ValueError:
        # 兼容 "2026-10-20 15:43:42" 与 "2026-10-20T15:43:42.123"
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d", "%Y/%m/%d %H:%M:%S"):
            try:
                return datetime.strptime(s[:19], fmt)
            except ValueError:
                continue
        return None
    if d.tzinfo is not None:
        # 带时区的一律换算到 CST 再去掉 tzinfo（库里 issue 时间即 CST 语义）
        d = d.astimezone(_CST).replace(tzinfo=None)
    return d



def week_start(d: datetime) -> datetime:
    """所在 ISO 周的周一 00:00。"""
    return (d - timedelta(days=d.weekday())).replace(
        hour=0, minute=0, second=0, microsecond=0)


def iso_week_label(d: datetime) -> str:
    iso = d.isocalendar()
    return "%d-W%02d" % (iso[0], iso[1])


def short_week_label(d: datetime) -> str:
    return "W%02d" % d.isocalendar()[1]


def _percentile(values: Sequence[float], q: float) -> Optional[float]:
    """nearest-rank 分位数（不做舍入）。

    必须返回原始值：调用方单位不同（分钟/小时/天），先按调用方单位舍入，
    否则分钟级样本会先被 round(x, 1) 小时抹成 0.0（实测踩过）。
    """
    if not values:
        return None
    vs = sorted(values)
    idx = max(0, min(len(vs) - 1, int(round(q * (len(vs) - 1)))))
    return float(vs[idx])


def _hours_to_days(hours: Optional[float]) -> Optional[float]:
    if hours is None:
        return None
    return round(float(hours) / 24.0, 1)


def _bucketize(values: Sequence[float], buckets) -> List[Dict[str, Any]]:
    """把数值按上界桶计数；落在 None 上界桶 = 其余全部。"""
    out = [{"label": label, "count": 0} for label, _ in buckets]
    for v in values:
        for i, (_, upper) in enumerate(buckets):
            if upper is None or v <= upper:
                out[i]["count"] += 1
                break
    return out


# ---------------------------------------------------------------- 数据读取

def _fetch_issue_rows(c, repo_keys: Sequence[str]):
    """一次取全量 issue 轻量列：分桶在 Python 侧完成。

    为什么不在 SQL 里分组：单测环境由 conftest 强制 SQLite，MySQL 的
    TIMESTAMPDIFF / DATE_FORMAT / JSON_EXTRACT 都不可用。仓库级 issue 行数
    在千级，Python 侧聚合成本可忽略，换来方言无关 + 口径唯一。
    """
    ph = ",".join("?" * len(repo_keys))
    c.execute(
        "SELECT status, created_at, completed_at, created_by, assignee, user_id"
        f" FROM issue WHERE repo_key IN ({ph}) AND is_subtask_mode=0",
        tuple(repo_keys))
    return c.fetchall()


# ---------------------------------------------------------------- 各卡片

def build_summary(rows, now: datetime) -> Dict[str, Any]:
    """②总工作量 / ③在制工作项 / 数据新鲜度。"""
    created_total = 0
    completed_total = 0
    open_total = 0
    unassigned = 0
    last_created = None
    for status, created_at, completed_at, created_by, assignee, _uid in rows:
        created_total += 1
        c_at = parse_local_ts(created_at)
        if c_at and (last_created is None or c_at > last_created):
            last_created = c_at
        if status == "completed" and parse_local_ts(completed_at):
            completed_total += 1
        if status not in _CLOSED_STATUSES:
            open_total += 1
            if not (created_by or "").strip() and not (assignee or "").strip():
                unassigned += 1
    stale_days = None
    if last_created:
        stale_days = round((now - last_created).total_seconds() / 86400.0, 1)
    return {
        "created_total": created_total,
        "completed_total": completed_total,
        "open_total": open_total,
        "unassigned": unassigned,
        "last_issue_at": last_created.strftime("%Y-%m-%d %H:%M:%S") if last_created else None,
        "stale_days": stale_days,
        "stale": bool(stale_days is not None and stale_days > 3),
    }


def build_throughput(rows, now: datetime, weeks: int = 12) -> Dict[str, Any]:
    """⑥周吞吐与累计积压：每周新建柱 + 每周关闭柱 + 周末未关闭折线。

    累计积压不落快照表，用 created/completed 回算：
    周末积压 = count(created_at < 周末) - count(completed_at < 周末)。
    """
    created_ts: List[datetime] = []
    completed_ts: List[datetime] = []
    for status, created_at, completed_at, *_rest in rows:
        c_at = parse_local_ts(created_at)
        if c_at:
            created_ts.append(c_at)
        if status == "completed":
            d_at = parse_local_ts(completed_at)
            if d_at:
                completed_ts.append(d_at)
    created_ts.sort()
    completed_ts.sort()

    cur_week = week_start(now)
    starts = [cur_week - timedelta(weeks=weeks - 1 - i) for i in range(weeks)]
    labels = [short_week_label(s) for s in starts]
    created_per = [0] * weeks
    completed_per = [0] * weeks
    backlog_per = [0] * weeks

    for i, s in enumerate(starts):
        e = s + timedelta(weeks=1)
        created_per[i] = bisect_right(created_ts, e - timedelta(microseconds=1)) - \
            bisect_right(created_ts, s - timedelta(microseconds=1))
        completed_per[i] = bisect_right(completed_ts, e - timedelta(microseconds=1)) - \
            bisect_right(completed_ts, s - timedelta(microseconds=1))
        backlog_per[i] = max(
            0,
            bisect_right(created_ts, e - timedelta(microseconds=1)) -
            bisect_right(completed_ts, e - timedelta(microseconds=1)),
        )

    return {
        "weeks": labels,
        "week_labels_full": [iso_week_label(s) for s in starts],
        "created": created_per,
        "completed": completed_per,
        "backlog": backlog_per,
    }


def build_net_flow(rows, now: datetime, throughput: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """①净流入：本周新建 − 本周关闭；附近 4 周净流入与积压趋势。"""
    tp = throughput or build_throughput(rows, now)
    created = tp["created"]
    completed = tp["completed"]
    backlog = tp["backlog"]
    week_created = created[-1] if created else 0
    week_completed = completed[-1] if completed else 0
    last4 = sum(created[-4:]) - sum(completed[-4:]) if created else 0
    trend = "flat"
    if len(backlog) >= 5:
        delta = backlog[-1] - backlog[-5]
        if delta > 0:
            trend = "up"
        elif delta < 0:
            trend = "down"
    return {
        "week": tp["week_labels_full"][-1] if tp.get("week_labels_full") else iso_week_label(now),
        "week_created": week_created,
        "week_completed": week_completed,
        "week_net": week_created - week_completed,
        "last4_weeks_net": last4,
        "backlog": backlog[-1] if backlog else 0,
        "backlog_trend": trend,
    }


def build_aging(rows, now: datetime, threshold_days: int = 7) -> Dict[str, Any]:
    """③ 在制工作项 + ⑤ 超龄未关 + ⑧ 积压龄期的公用计算。

    grape 没有 due date（payload 里 due_date 实测全为空且未落库），
    所以「超期」口径不存在，只能按龄期——卡片文案必须写明「按龄期，非期限」。
    """
    ages_hours: List[float] = []
    over_threshold = 0
    open_total = 0
    for status, created_at, _completed, *_rest in rows:
        if status in _CLOSED_STATUSES:
            continue
        c_at = parse_local_ts(created_at)
        if not c_at:
            continue
        open_total += 1
        age_h = (now - c_at).total_seconds() / 3600.0
        if age_h < 0:
            continue
        ages_hours.append(age_h)
        if age_h > threshold_days * 24:
            over_threshold += 1
    return {
        "open": open_total,
        "threshold_days": threshold_days,
        "over_threshold": over_threshold,
        "p50_days": _hours_to_days(_percentile(ages_hours, 0.50)),
        "p90_days": _hours_to_days(_percentile(ages_hours, 0.90)),
        "buckets": _bucketize(ages_hours, AGING_BUCKETS),
        "sample": len(ages_hours),
    }


def build_cycle_time(rows) -> Dict[str, Any]:
    """⑦ 周期时间（创建→关闭）。样本天然稀疏（只有 completed 且有 completed_at 的）。

    必须把 sample 一起返回：dev 库 307 个主任务里只有 32 个有 completed_at，
    只给分位数不给样本量就是假象。
    """
    values_days: List[float] = []
    dirty = 0
    for status, created_at, completed_at, *_rest in rows:
        if status != "completed":
            continue
        c_at = parse_local_ts(created_at)
        d_at = parse_local_ts(completed_at)
        if not c_at or not d_at:
            continue
        delta = (d_at - c_at).total_seconds() / 86400.0
        if delta < 0:
            dirty += 1
            continue
        values_days.append(delta)
    bucket_bounds = [
        ("<1天", 1.0), ("1-3天", 3.0), ("3-7天", 7.0),
        ("1-2周", 14.0), ("2-4周", 28.0), (">4周", None),
    ]
    values_hours = [v * 24.0 for v in values_days]
    p90_h = _percentile(values_hours, 0.90)
    # 自动化场景周期常是分钟级：按 P90 的量级选单位，避免 "P50 0.0 天" 这种废话。
    # minutes(<1h) → hours(<24h) → days
    if p90_h is not None and p90_h < 1.0:
        unit, scale = "minutes", 1.0 / 60.0
    elif p90_h is not None and p90_h < 24.0:
        unit, scale = "hours", 1.0
    else:
        unit, scale = "days", 24.0
    return {
        "unit": unit,
        "p50": round(_percentile(values_hours, 0.50) / scale, 1) if values_hours else None,
        "p75": round(_percentile(values_hours, 0.75) / scale, 1) if values_hours else None,
        "p90": round(p90_h / scale, 1) if p90_h is not None else None,
        "sample": len(values_days),
        "dirty_skipped": dirty,
        "buckets": _bucketize(values_days, bucket_bounds),
    }


# ---------------------------------------------------------------- ⑨ 在办分布

# bot 名形态（与 dashboard.py::_is_bot_name 同一规则，Issue #1337）：
# `bot` / `*_bot` / `*-bot`，大小写不敏感。裸 endswith('bot') 会误杀 abbot /
# robot / talbot 这类真人账号。名单命中（managed_repos.bot_username + 全局配置）
# 由调用方以 bot_names 传入，两条规则取并集。
_BOT_NAME_RE = re.compile(r"(?:^|[-_])bot$", re.IGNORECASE)

UNASSIGNED_LABEL = "未分配"


def is_bot_name(name: str, bot_names: Sequence[str] = ()) -> bool:
    n = (name or "").strip()
    if not n:
        return False
    if n in set(bot_names or ()):
        return True
    return bool(_BOT_NAME_RE.search(n))


def load_uid_names(c, uids) -> Dict[int, str]:
    """user_id → 平台用户名（gitcode_user 优先，github_login 兜底）。

    unified_user_mapping 在部分环境中可能不存在（老库/单测未建表），失败降级为空映射，
    不让归属兜底拖垮整块看板。
    """
    todo = sorted({int(u) for u in uids if u and int(u) > 0})
    out: Dict[int, str] = {}
    for i in range(0, len(todo), 500):
        part = todo[i:i + 500]
        ph = ",".join("?" * len(part))
        try:
            c.execute(
                "SELECT user_id, COALESCE(NULLIF(gitcode_user,''),"
                " NULLIF(github_login,'')) FROM unified_user_mapping"
                f" WHERE user_id IN ({ph}) AND user_id>0", tuple(part))
        except Exception:
            return out
        for uid, name in c.fetchall():
            if name:
                out[int(uid)] = name
    return out


def build_wip_distribution(rows, bot_names: Sequence[str] = (),
                           uid_names: Optional[Dict[int, str]] = None,
                           unassigned_label: str = UNASSIGNED_LABEL) -> Dict[str, Any]:
    """⑨ 在办分布：未关闭 issue 按处理人分组（找瓶颈，不是绩效）。

    归属：created_by 优先，空则 assignee，再空则 user_id 映射，全空落"未分配"。
    输出顺序：人类按数量降序 → bot 行（前端灰色，不参与人类排名）→ 未分配（垫底，
    它是数据质量提示不是一个人）。`total` 必须等于 ③ 在制工作项数（同源同口径）。
    每行同时给 `open`（未关闭）与 `completed`（已完成）——只有未关闭会被误读成
    "只提需求不干活"，两个数并排才能看出负载与产出。
    """
    uid_names = uid_names or {}
    human: Dict[str, int] = {}
    bots: Dict[str, int] = {}
    done_human: Dict[str, int] = {}
    done_bots: Dict[str, int] = {}
    unassigned = 0
    for status, _created, completed_at, created_by, assignee, uid in rows:
        name = (created_by or "").strip() or (assignee or "").strip()
        if not name and uid:
            name = (uid_names.get(int(uid)) or "").strip()
        if status in _CLOSED_STATUSES:
            # 已完成也按同一归属链计数：只有未关闭看不出这个人的产出，
            # 光看"新建"会被误读成"只提需求不干活"
            if status == "completed" and parse_local_ts(completed_at) and name:
                bucket_done = done_bots if is_bot_name(name, bot_names) else done_human
                bucket_done[name] = bucket_done.get(name, 0) + 1
            continue
        if not name:
            unassigned += 1
            continue
        bucket = bots if is_bot_name(name, bot_names) else human
        bucket[name] = bucket.get(name, 0) + 1
    total = sum(human.values()) + sum(bots.values()) + unassigned
    # 排序口径 = 该名字的总条数（未关闭 + 已完成），人与 bot 一起排——
    # 条形长度就是总条数，若把 bot 固定垫底，图上会出现"条更长却排在下面"的自相矛盾。
    # bot 仍用 kind=bot 标灰、不计入人类排名（top2_share 只算人类），未分配垫底。
    merged = []
    for name, cnt in human.items():
        merged.append((name, cnt, done_human.get(name, 0), "human"))
    for name, cnt in bots.items():
        merged.append((name, cnt, done_bots.get(name, 0), "bot"))
    merged.sort(key=lambda x: (-(x[1] + x[2]), x[0]))
    out = []
    for name, cnt, done, kind in merged:
        out.append({"name": name, "count": cnt, "open": cnt,
                    "completed": done, "total": cnt + done, "kind": kind,
                    "share": (cnt / total) if total else 0.0})
    if unassigned:
        out.append({"name": unassigned_label, "count": unassigned,
                    "open": unassigned, "completed": 0, "total": unassigned,
                    "kind": "unassigned",
                    "share": (unassigned / total) if total else 0.0})
    human_total = sum(human.values())
    return {
        "items": out,
        "total": total,
        "human_total": human_total,
        "completed_total": sum(done_human.values()) + sum(done_bots.values()),
        "bot_total": sum(bots.values()),
        "unassigned_total": unassigned,
        "human_share": (human_total / total) if total else 0.0,
        "top2_share": (sum(v for _, v in sorted(human.items(), key=lambda kv: -kv[1])[:2]) / total) if total else 0.0,
    }


# ---------------------------------------------------------------- ⑩ 任务交付时长

DELIVERY_GROUP_ALL = "all"


def _fetch_task_rows(c, repo_keys: Sequence[str]):
    """取任务轻量列；agent_tasks 缺失（老库/最小测试库）降级为空，不拖垮整板。"""
    ph = ",".join("?" * len(repo_keys))
    try:
        c.execute(
            "SELECT status, started_at, completed_at, agent_type"
            f" FROM agent_tasks WHERE repo_key IN ({ph})",
            tuple(repo_keys))
    except Exception:
        return []
    return c.fetchall()


def _task_durations_minutes(rows, statuses=None):
    """(status, minutes) 列表；只保留 started_at/completed_at 都有且不早于开始的行。

    agent_tasks 的时间是带时区 UTC ISO，issue.created_at 是无时区本地时间——
    跨表算差值会差 8 小时，所以本卡只在 agent_tasks 内部做减法（时区自动抵消）。
    """
    out = []
    dirty = 0
    for status, started_at, completed_at, agent_type in rows:
        if statuses is not None and status not in statuses:
            continue
        st = parse_local_ts(started_at)
        en = parse_local_ts(completed_at)
        if not st or not en:
            continue
        mins = (en - st).total_seconds() / 60.0
        if mins < 0:
            dirty += 1
            continue
        out.append((status, mins, agent_type or ""))
    return out, dirty


def _delivery_slice(pairs) -> Dict[str, Any]:
    """统一以「分钟」为原始单位返回（契约见设计文档 §4.1）。

    不在后端做单位自适应：这个平台的任务量级从 2 秒到 3 小时都有，
    任何固定单位都会在某些组里输出 "P50 0.0"。前端按值格式化（<1 分钟显示秒）。
    """
    values = [m for _, m, _t in pairs]
    return {
        "p50_min": round(_percentile(values, 0.50), 2) if values else None,
        "p90_min": round(_percentile(values, 0.90), 2) if values else None,
        "p99_min": round(_percentile(values, 0.99), 2) if values else None,
        "sample": len(values),
        "buckets": _bucketize(values, DELIVERY_BUCKETS_MIN),
    }


def build_task_delivery(rows, group_by: str = DELIVERY_GROUP_ALL) -> Dict[str, Any]:
    """⑩ 成效：任务交付——一个活交给 agent 后多久干完（P50/P90/P99 + 分桶）。

    为什么不用"首响"：grape 是自动处理场景，issue→任务派发实测中位 0.01 分钟
    （97.4% ≤ 1 分钟），"等人类响应"这一环节根本不存在；真人评论事件只覆盖 5 个
    issue。任务交付时长才是这个平台上有信息量的"响应/成效"指标。

    - 总览默认 group_by='all'；可按 completed / failed / cancelled 切单组。
    - by_status / by_agent_type 始终返回全量拆分：失败任务通常立即失败（dev 库
      failed 中位 0.0 分钟），混在一起会被短任务压平。
    - 时长含引擎排队与模型思考，不区分纯执行——要拆分需 OTel span（后续阶段）。
    """
    pairs, dirty = _task_durations_minutes(rows)
    by_status: Dict[str, Any] = {}
    statuses = sorted({p[0] for p in pairs})
    for st in statuses:
        sub = [p for p in pairs if p[0] == st]
        by_status[st] = _delivery_slice(sub)

    by_agent_type: Dict[str, Any] = {}
    for at in sorted({p[2] for p in pairs if p[2]}):
        sub = [p for p in pairs if p[2] == at]
        by_agent_type[at] = _delivery_slice(sub)

    if group_by and group_by != DELIVERY_GROUP_ALL:
        sel = [p for p in pairs if p[0] == group_by]
    else:
        sel = pairs
    result = _delivery_slice(sel)
    result.update({
        "group_by": group_by or DELIVERY_GROUP_ALL,
        "sample": len(sel),
        "total_tasks": len(rows),
        "dirty_skipped": dirty,
        "by_status": by_status,
        "by_agent_type": by_agent_type,
    })
    return result


# ---------------------------------------------------------------- 下钻：按日 / 按桶

def parse_week_label(label: str) -> Optional[datetime]:
    """"2026-W45" / "W45" → 该 ISO 周的周一 00:00（缺年份时取当前年）。"""
    import re as _re
    m = _re.match(r"^(?:(\d{4})-)?W(\d{1,2})$", (label or "").strip(), _re.I)
    if not m:
        return None
    year = int(m.group(1)) if m.group(1) else datetime.now().isocalendar()[0]
    week = int(m.group(2))
    if not 1 <= week <= 53:
        return None
    try:
        return datetime.fromisocalendar(year, week, 1)
    except ValueError:
        return None


def build_daily(rows, week_monday: datetime) -> Dict[str, Any]:
    """某周按天拆解：每日新建 / 每日关闭 / 当日累计积压。

    与 build_throughput 同源（同一批 issue 行、同样回算积压），只是桶从周换成日，
    保证"周值 = 该周 7 天之和"这一交叉校验成立。
    """
    created_ts: List[datetime] = []
    completed_ts: List[datetime] = []
    for status, created_at, completed_at, *_rest in rows:
        c_at = parse_local_ts(created_at)
        if c_at:
            created_ts.append(c_at)
        if status == "completed":
            d_at = parse_local_ts(completed_at)
            if d_at:
                completed_ts.append(d_at)
    created_ts.sort()
    completed_ts.sort()

    wd = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]
    days = []
    for i in range(7):
        start = week_monday + timedelta(days=i)
        end = start + timedelta(days=1) - timedelta(microseconds=1)
        lo = start - timedelta(microseconds=1)
        days.append({
            "date": start.strftime("%Y-%m-%d"),
            "label": start.strftime("%m-%d"),
            "weekday": wd[i],
            "created": bisect_right(created_ts, end) - bisect_right(created_ts, lo),
            "completed": bisect_right(completed_ts, end) - bisect_right(completed_ts, lo),
            "backlog": max(0, bisect_right(created_ts, end) - bisect_right(completed_ts, end)),
        })
    return {
        "week": iso_week_label(week_monday),
        "week_start": week_monday.strftime("%Y-%m-%d"),
        "days": days,
        "totals": {
            "created": sum(d["created"] for d in days),
            "completed": sum(d["completed"] for d in days),
        },
    }


# 周期时间分桶边界（与 build_cycle_time 保持一致，单位天）
CYCLE_BUCKETS = [
    ("<1天", 1.0), ("1-3天", 3.0), ("3-7天", 7.0),
    ("1-2周", 14.0), ("2-4周", 28.0), (">4周", None),
]


def _bucket_of(value: float, buckets) -> Optional[str]:
    for label, upper in buckets:
        if upper is None or value <= upper:
            return label
    return None


def _fetch_issue_detail_rows(c, repo_keys: Sequence[str]):
    ph = ",".join("?" * len(repo_keys))
    c.execute(
        "SELECT issue_number, title, status, COALESCE(stage,''), COALESCE(scene_id,''),"
        " created_at, completed_at, COALESCE(created_by,''), COALESCE(assignee,''),"
        " COALESCE(user_id,0)"
        f" FROM issue WHERE repo_key IN ({ph}) AND is_subtask_mode=0",
        tuple(repo_keys))
    return c.fetchall()


def load_scene_names(c, scene_ids) -> Dict[str, str]:
    """scene_id → 展示名（display_name 优先，空则 name）。表缺失/异常降级为空映射。"""
    todo = sorted({s for s in scene_ids if s})
    out: Dict[str, str] = {}
    for i in range(0, len(todo), 500):
        part = todo[i:i + 500]
        ph = ",".join("?" * len(part))
        try:
            c.execute(
                "SELECT scene_id, COALESCE(NULLIF(display_name,''), NULLIF(name,''))"
                f" FROM devops_pipeline_scene WHERE scene_id IN ({ph})", tuple(part))
        except Exception:
            return out
        for sid, name in c.fetchall():
            if name and sid not in out:
                out[sid] = name
    return out


def build_issue_list(c, repo_keys: Sequence[str], kind: str, bucket: str = "",
                     threshold_days: int = 7, now: Optional[datetime] = None,
                     limit: int = 20, offset: int = 0) -> Dict[str, Any]:
    """下钻列表：某卡片某一行对应哪些 issue。

    kind:
      - "aging"   未关闭 issue，按龄期桶（bucket 为桶标签，如 ">2周"）
      - "overdue" 未关闭且龄期 > threshold_days（⑤ 那张卡的点击）
      - "cycle"   已完成 issue，按创建→关闭耗时桶
    """
    now = now or datetime.now()
    if not repo_keys:
        return {"kind": kind, "bucket": bucket, "total": 0, "items": [], "empty": True}
    rows = _fetch_issue_detail_rows(c, repo_keys)
    scene_map = load_scene_names(c, (r[4] for r in rows))

    items = []
    for num, title, status, stage, scene_id, created_at, completed_at, creator, assignee, uid in rows:
        c_at = parse_local_ts(created_at)
        if not c_at:
            continue
        d_at = parse_local_ts(completed_at)
        rec = {
            "number": num,
            "title": title or "",
            "status": status,
            "stage": stage,
            "scene": scene_map.get(scene_id, scene_id or ""),
            "created_by": creator or assignee or (str(uid) if uid else ""),
            "created_at": c_at.strftime("%Y-%m-%d %H:%M"),
        }
        if kind in ("aging", "overdue"):
            if status in _CLOSED_STATUSES:
                continue
            age_h = (now - c_at).total_seconds() / 3600.0
            if age_h < 0:
                continue
            if kind == "overdue":
                if age_h <= threshold_days * 24:
                    continue
                rec["metric"] = round(age_h / 24.0, 1)
                rec["metric_unit"] = "days"
            else:
                if bucket and _bucket_of(age_h, AGING_BUCKETS) != bucket:
                    continue
                rec["metric"] = round(age_h / 24.0, 1)
                rec["metric_unit"] = "days"
        elif kind == "cycle":
            if status != "completed" or not d_at:
                continue
            cyc_days = (d_at - c_at).total_seconds() / 86400.0
            if cyc_days < 0:
                continue
            if bucket and _bucket_of(cyc_days, CYCLE_BUCKETS) != bucket:
                continue
            rec["metric"] = round(cyc_days * 24.0, 1)
            rec["metric_unit"] = "hours"
            rec["completed_at"] = d_at.strftime("%Y-%m-%d %H:%M")
        else:
            return {"kind": kind, "bucket": bucket, "error": "unknown_kind",
                    "items": [], "total": 0}
        items.append(rec)

    items.sort(key=lambda r: (-(r.get("metric") or 0), r.get("number") or 0))
    total = len(items)
    # 分页：调用方按 offset/limit 取，total 始终是全量条数（前端据此算页数）
    off = max(0, int(offset or 0))
    lim = max(1, int(limit or 20))
    return {"kind": kind, "bucket": bucket, "total": total,
            "offset": off, "limit": lim,
            "items": items[off:off + lim], "empty": total == 0}


# ---------------------------------------------------------------- 编排

def build_board(c, repo_keys: Sequence[str], repo_label: str = "",
                weeks: int = 12, age_threshold_days: int = 7,
                now: Optional[datetime] = None,
                bot_names: Sequence[str] = (),
                delivery_group: str = DELIVERY_GROUP_ALL) -> Dict[str, Any]:
    """把 P1+P2+P3 各段一次算完（②③①⑥⑤⑦⑧⑨⑩），返回看板骨架。

    bot_names 由路由层注入（managed_repos + 全局配置），模块本身不查用户库。
    delivery_group 控制 ⑩ 顶部展示的组（all / completed / failed / cancelled）；
    by_status / by_agent_type 始终给全量拆分。
    """
    if not repo_keys:
        return {
            "repo": repo_label,
            "empty": True,
            "message": "no_repo_selected",
            "summary": {},
            "net_flow": {},
            "throughput": {},
            "aging": {},
            "cycle_time": {},
            "wip_by_assignee": {"items": [], "total": 0},
            "task_delivery": {"sample": 0, "by_status": {}, "by_agent_type": {}},
        }
    now = now or datetime.now()
    rows = _fetch_issue_rows(c, repo_keys)
    summary = build_summary(rows, now)
    throughput = build_throughput(rows, now, weeks=weeks)
    uid_names = load_uid_names(c, (r[5] for r in rows if r[5]))
    wip = build_wip_distribution(rows, bot_names=bot_names, uid_names=uid_names)
    task_delivery = build_task_delivery(_fetch_task_rows(c, repo_keys),
                                       group_by=delivery_group)
    return {
        "repo": repo_label,
        "empty": len(rows) == 0,
        "range": {
            "week": iso_week_label(now),
            "weeks_back": weeks,
            "generated_at": now.strftime("%Y-%m-%d %H:%M:%S"),
            "timezone": "Asia/Shanghai",
        },
        "summary": summary,
        "net_flow": build_net_flow(rows, now, throughput),
        "throughput": throughput,
        "aging": build_aging(rows, now, threshold_days=age_threshold_days),
        "cycle_time": build_cycle_time(rows),
        "wip_by_assignee": wip,
        "task_delivery": task_delivery,
        "data_quality": {
            "issue_last_sync": summary.get("last_issue_at"),
            "stale": summary.get("stale"),
            "stale_days": summary.get("stale_days"),
        },
    }