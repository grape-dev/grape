"""Token 三级归因：引擎 / 任务 / 阶段事件（= issue × agent_type）。

需求（用户 2026-10-19）：① 每个引擎的 token ② 每个任务的 token ③ **每个任务(issue)每个阶段事件**的 token
（例：issue #1 走了 vod → issue 两个阶段 → 两个事件）。引擎内部 ACP 步骤那层暂不做。

数据源全是现成明细 `obs_llm_usage`（本域），任务行的 issue_id 由 runtime 侧 `agent_tasks`
经 `runtime_public.task_issue_id_rows()` 批量取回后在 Python 内连接补上（原为跨库 JOIN，
三库独立后该 JOIN 必 1146）——**不需要新增采集**；
`agent_type` 就是阶段（vod/issue/coding/qa/sre…），任务行上带 issue_id。

口径与坑：
- token 五项：input/output/cache_read/cache_write/reasoning；
  `total` = input+output+cache_read+cache_write（与 rollup 的 `token_total` 同口径，
  **不含 reasoning**——它通常是 output 的子集，加进去会重复计）。
- `obs_llm_usage.created_at` 走 DDL 默认值时是 MySQL 本地时间(CST)，历史行与 UTC 混写 →
  SQL 只用**宽松窗口**（±1 天）粗筛，真正的时间窗用 `normalize_event_time` 在 Python 侧精确判定
  （与 `metric_rollup` 同款处理，勿改成字符串比较）。
- 可见性：一律显式过滤，**空集合 → 空结果**（绝不降级成全量）。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from typing import Any, Callable, Dict, List, Optional, Tuple

from src.kernel.config import log

#: token 明细列（顺序即展示顺序）
TOKEN_COLS = ("input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens",
              "reasoning_tokens")

#: 支持的时间窗（小时）
RANGES = {"1h": 1, "6h": 6, "24h": 24, "7d": 168, "30d": 720}

DEFAULT_RANGE = "24h"

#: 明细列序（与 insight public `list_usage_detail_window` 的 SELECT 逐列一致；
#: 行→dict 映射留在本模块，owner 只返回元组）
_DETAIL_COLS: Tuple[str, ...] = ("task_id", "agent_type", "engine_id", "repo_key", "run_kind",
                                 "run_id", "created_at") + TOKEN_COLS + ("cost_usd",)

#: 每类返回的最大行数
MAX_ROWS = 200

def _now() -> datetime:
    return datetime.now(timezone.utc)

def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat()

def _dt(raw: Any) -> Optional[datetime]:
    from src.api.time_utils import normalize_event_time
    s = normalize_event_time(raw)
    if not s:
        return None
    try:
        d = datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)

def window_of(range_str: str, now: Optional[datetime] = None) -> Tuple[datetime, datetime]:
    """把 `1h/6h/24h/7d/30d`（或纯小时数）折算成 (start, end)。"""
    key = str(range_str or DEFAULT_RANGE).strip().lower()
    hours = RANGES.get(key)
    if hours is None:
        try:
            hours = max(1, int(float(key.rstrip("h")))) if key.endswith("h") else int(float(key))
        except (ValueError, TypeError):
            hours = RANGES[DEFAULT_RANGE]
    end = now or _now()
    return end - timedelta(hours=hours), end

def tokens_of(row: Dict[str, Any]) -> Dict[str, float]:
    out: Dict[str, float] = {}
    for col in TOKEN_COLS:
        out[col[:-7]] = float(row.get(col) or 0)
    out["total"] = sum(out[k] for k in ("input", "output", "cache_read", "cache_write"))
    return out

def _sum_rows(rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    agg: Dict[str, Any] = {c[:-7]: 0.0 for c in TOKEN_COLS}
    agg["total"] = 0.0
    cost = 0.0
    for r in rows:
        t = tokens_of(r)
        for k in list(agg):
            agg[k] += t[k]
        try:
            cost += float(r.get("cost_usd") or 0)
        except (TypeError, ValueError):
            pass
    out = {k: (round(v, 3) if isinstance(v, float) else v) for k, v in agg.items()}
    out["cost_usd"] = round(cost, 6)
    out["rows"] = len(rows)
    return out

def _by(rows: List[Dict[str, Any]], key: Callable[[Dict[str, Any]], Tuple],
        fields: Tuple[str, ...]) -> List[Dict[str, Any]]:
    """按 key 分组聚合（保持首次出现顺序）。`fields` 与 key 返回的元组一一对应。"""
    groups: Dict[Tuple, List[Dict[str, Any]]] = {}
    order: List[Tuple] = []
    for r in rows:
        k = key(r)
        if k not in groups:
            groups[k] = []
            order.append(k)
        groups[k].append(r)
    out: List[Dict[str, Any]] = []
    for k in order:
        item = dict(zip(fields, k))
        item.update(_sum_rows(groups[k]))
        out.append(item)
    return out

def _scope(user: Optional[Dict[str, Any]], repo: str = "", global_scope: bool = False,
           engine_ids: Optional[List[int]] = None
           ) -> Tuple[List[str], List[int], bool]:
    """可见性过滤：返回 (repo_key 候选集, engine_id 集合, 是否可用)。**空集合 → False**（不降级）。

    - `global_scope=True`（L2）→ 不过滤；
    - `repo` 给了 → 只该仓（L1 越权 → 空集合，由调用方转 404）；
    - 否则 → 调用者可见仓库 ∪ 可见引擎（`engine_ids` 由路由传入，已在别处校验）。

    只返回**结构化过滤值**、不再拼 SQL 片段：谓词由表归属域（insight public
    `list_usage_detail_window`）按列名白名单拼装，值一律 `?` 参数化。
    """
    if global_scope:
        return [], [], True
    if repo:
        from src.kernel.repo_key import to_repo_key
        from src.domains.insight.observability.overview import visible_repo_keys
        want = set(repo_key_candidates(to_repo_key(repo)))  # S94/D69：候选集统一（S25 helper）
        hit = sorted(want & set(visible_repo_keys(user or {})))
        if not hit:
            return [], [], False
        return hit, [], True
    keys: List[str] = []
    try:
        from src.domains.insight.observability.overview import visible_repo_keys
        keys = sorted(visible_repo_keys(user or {}))
    except Exception as e:
        log.warning(f"obs tokens scope repos failed: {e}")
    ids = [int(x) for x in (engine_ids or []) if x]
    if not keys and not ids:
        return [], [], False
    return keys, ids, True

def _fetch(start: datetime, end: datetime, repo_keys: Optional[List[str]] = None,
           engine_ids: Optional[List[int]] = None,
           task_id: str = "", issue_id: str = "", limit: int = 20000) -> List[Dict[str, Any]]:
    """取窗口内明细（SQL 宽松窗口 ±1 天粗筛 + Python 精确窗口，见模块注释）。

    跨库 JOIN 已拆成两步（表各归其域，本进程不写外域 SQL）：
    ① 明细窗口读 → 本域 `insight_public.list_usage_detail_window()`；
    ② `task_id → issue_id` → `runtime_public.task_issue_id_rows()` 批量取（分块 500），
       在 Python 内连接补 `issue_id`（任务行不存在 → None，即原 LEFT JOIN 的 NULL 侧）。
    任一环节失败 → 空窗口 + warning（与原「单条宽 SQL 失败即空」同口径）。
    """
    from src.domains.insight import public as insight_public
    from src.domains.runtime import public as runtime_public
    try:
        raw_rows = insight_public.list_usage_detail_window(
            _iso(start - timedelta(days=1)), _iso(end + timedelta(days=1)),
            repo_keys=repo_keys or (), engine_ids=engine_ids or (),
            task_id=str(task_id) if task_id else "", limit=limit)
    except Exception as e:
        log.warning(f"obs tokens fetch failed: {e}")
        return []
    raw = [dict(zip(_DETAIL_COLS, r)) for r in raw_rows]
    if not raw:
        return []
    ids: List[str] = []
    seen: set = set()
    for r in raw:
        t = str(r.get("task_id") or "")
        if t and t not in seen:
            seen.add(t)
            ids.append(t)
    try:
        pairs = runtime_public.task_issue_id_rows(ids)
    except Exception as e:
        log.warning(f"obs tokens issue map failed: {e}")
        return []
    issue_of = {str(rid): iid for rid, iid in pairs}   # 关联口径：id 与 task_id 字符串形式相等
    for r in raw:
        r["issue_id"] = issue_of.get(str(r.get("task_id") or ""))
    out: List[Dict[str, Any]] = []
    for r in raw:
        d = _dt(r.get("created_at"))
        if d is None or not (start <= d <= end):
            continue                                  # 精确窗口（不受 CST/方言影响）
        if issue_id and str(r.get("issue_id") or "") != str(issue_id):
            continue
        out.append(r)
    return out

def engine_totals(user=None, repo: str = "", range_str: str = DEFAULT_RANGE,
                  global_scope: bool = False, engine_ids: Optional[List[int]] = None,
                  now: Optional[datetime] = None) -> Dict[str, Any]:
    """① 每个引擎的 token 汇总（含它跑过的任务数）。"""
    start, end = window_of(range_str, now)
    scope_keys, scope_ids, ok = _scope(user, repo, global_scope, engine_ids)
    out: Dict[str, Any] = {"window": {"start": _iso(start), "end": _iso(end), "range": range_str}}
    if not ok:
        out.update({"engines": [], "total": _sum_rows([])})
        return out
    rows = _fetch(start, end, scope_keys, scope_ids)
    engines = _by(rows, lambda r: (int(r.get("engine_id") or 0),), ("engine_id",))
    for e in engines:
        e["tasks"] = len({r.get("task_id") for r in rows
                          if int(r.get("engine_id") or 0) == e["engine_id"]})
    names = _engine_names([e["engine_id"] for e in engines])
    for e in engines:
        e["name"] = names.get(e["engine_id"], "")
    engines.sort(key=lambda x: x["total"], reverse=True)
    out.update({"engines": engines[:MAX_ROWS], "total": _sum_rows(rows), "rows": len(rows)})
    return out

def task_totals(user=None, repo: str = "", range_str: str = DEFAULT_RANGE,
                global_scope: bool = False, run_kind: str = "",
                limit: int = MAX_ROWS, now: Optional[datetime] = None) -> Dict[str, Any]:
    """② 每个任务/运行的 token 汇总（含 issue/阶段/引擎）。"""
    start, end = window_of(range_str, now)
    scope_keys, scope_ids, ok = _scope(user, repo, global_scope)
    out: Dict[str, Any] = {"window": {"start": _iso(start), "end": _iso(end), "range": range_str}}
    if not ok:
        out.update({"tasks": [], "total": _sum_rows([])})
        return out
    rows = _fetch(start, end, scope_keys, scope_ids)
    if run_kind:
        rows = [r for r in rows if str(r.get("run_kind") or "task") == run_kind]
    tasks = _by(rows, lambda r: (str(r.get("task_id") or ""), str(r.get("run_kind") or "task"),
                                 str(r.get("run_id") or ""), str(r.get("issue_id") or ""),
                                 str(r.get("agent_type") or ""), int(r.get("engine_id") or 0)),
                ("task_id", "run_kind", "run_id", "issue_id", "stage", "engine_id"))
    tasks.sort(key=lambda x: x["total"], reverse=True)
    out.update({"tasks": tasks[:max(1, int(limit))], "total": _sum_rows(rows), "rows": len(rows)})
    return out

def stage_totals(user=None, repo: str = "", range_str: str = DEFAULT_RANGE,
                 global_scope: bool = False, issue_id: str = "",
                 now: Optional[datetime] = None) -> Dict[str, Any]:
    """③ 阶段事件级：按 (issue, 阶段) 聚合 —— 一个 issue 走 vod→issue 就是两行。"""
    start, end = window_of(range_str, now)
    scope_keys, scope_ids, ok = _scope(user, repo, global_scope)
    out: Dict[str, Any] = {"window": {"start": _iso(start), "end": _iso(end), "range": range_str}}
    if not ok:
        out.update({"stages": [], "total": _sum_rows([])})
        return out
    rows = _fetch(start, end, scope_keys, scope_ids, issue_id=issue_id)
    stages = _by(rows, lambda r: (str(r.get("issue_id") or ""), str(r.get("agent_type") or "")),
                 ("issue_id", "stage"))
    for s in stages:
        s["tasks"] = len({r.get("task_id") for r in rows
                          if str(r.get("issue_id") or "") == s["issue_id"]
                          and str(r.get("agent_type") or "") == s["stage"]})
    stages.sort(key=lambda x: (x["issue_id"], x["stage"]))
    out.update({"stages": stages, "total": _sum_rows(rows), "rows": len(rows)})
    return out

def issue_stages(issue_id: str, user=None, repo: str = "", range_str: str = "30d",
                 global_scope: bool = False, now: Optional[datetime] = None) -> Dict[str, Any]:
    """单个 issue 的阶段明细（任务视图里"这个 issue 每个阶段花了多少"）。"""
    data = stage_totals(user=user, repo=repo, range_str=range_str, global_scope=global_scope,
                        issue_id=issue_id, now=now)
    tasks = task_totals(user=user, repo=repo, range_str=range_str, global_scope=global_scope,
                        now=now).get("tasks", [])
    data["tasks"] = [t for t in tasks if str(t.get("issue_id") or "") == str(issue_id)]
    data["issue_id"] = str(issue_id)
    return data

def summary(user=None, repo: str = "", range_str: str = DEFAULT_RANGE,
            global_scope: bool = False, now: Optional[datetime] = None) -> Dict[str, Any]:
    """三层合计（用于验收硬断言：Σ引擎 = Σ任务 = Σ阶段）。"""
    e = engine_totals(user=user, repo=repo, range_str=range_str, global_scope=global_scope, now=now)
    t = task_totals(user=user, repo=repo, range_str=range_str, global_scope=global_scope, now=now)
    s = stage_totals(user=user, repo=repo, range_str=range_str, global_scope=global_scope, now=now)
    return {"engines": e["total"]["total"], "tasks": t["total"]["total"],
            "stages": s["total"]["total"], "rows": e.get("rows", 0),
            "equal": e["total"]["total"] == t["total"]["total"] == s["total"]["total"]}

def _engine_names(ids: List[int]) -> Dict[int, str]:
    """engine_id → 显示名（优先 device_name，退回 name）。

    表 `runtime_credentials` 归 runtime：三库独立后本进程库没有它（视图 0），
    原直连 SQL 必 1146 被吞成「无显示名」。改调 runtime public 批量读口
    （`COALESCE(NULLIF(device_name,''), name, '')` 表达式与列序原样保留）。
    """
    ids = sorted({int(i) for i in ids if i})
    if not ids:
        return {}
    from src.domains.runtime import public as runtime_public
    out: Dict[int, str] = {}
    try:
        for rid, nm in runtime_public.runtime_credential_display_names(ids):
            out[int(rid)] = str(nm or "")
    except Exception as e:
        log.warning(f"obs tokens engine names failed: {e}")
    return out

from src.kernel.repo_key import repo_key_candidates  # S94/D69：候选集统一
