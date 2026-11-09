"""Dashboard 路由：仓库概览、指标、Agent 状态、运维可观察性"""
import re

from fastapi import APIRouter, Depends
from src.user import db as user_db
from src.api.deps import require_no_session_token, require_org_member
from src.api.time_utils import normalize_event_time
from src.observability.engine_probe import get_probe_results
from src.config import log

router = APIRouter(prefix="/api/dashboard", tags=["dashboard"])


def _stuck_minutes() -> int:
    """stuck 预警阈值（分钟，Issue #470：随统一超时配置派生，默认超时 3600s → 22min）。"""
    from src.config import get_stuck_threshold_minutes
    return get_stuck_threshold_minutes()


# ===== Issue #180: 本机带宽占用率差分采样缓存 =====
_BW_SNAPSHOT = {"ts": 0.0, "bytes": 0}


# R16.2.2: error_code → 本地化文案映射
# 从 i18n locales 文件加载，dashboard API 返回时映射到前端可读文案
_ERROR_CODE_I18N_CACHE: dict | None = None


def _load_error_code_i18n() -> dict:
    """加载 error_code 的 i18n 映射（zh/en 双语）。"""
    global _ERROR_CODE_I18N_CACHE
    if _ERROR_CODE_I18N_CACHE is not None:
        return _ERROR_CODE_I18N_CACHE
    import json as _json, os
    _result = {}
    _locales_dir = os.path.join(os.path.dirname(os.path.dirname(__file__)), "static", "locales")
    for _lang in ("zh", "en"):
        _path = os.path.join(_locales_dir, _lang, "app.json")
        try:
            with open(_path, "r", encoding="utf-8") as _f:
                _data = _json.loads(_f.read())
                _errors = _data.get("errors", {})
                if isinstance(_errors, dict):
                    _result[_lang] = _errors
        except Exception:
            _result[_lang] = {}
    _ERROR_CODE_I18N_CACHE = _result
    return _result


def _resolve_error_messages(errors: list, lang: str = "zh") -> list:
    """R16.2.2: 遍历 errors 数组，将 error_code 映射为本地化文案。

    每个 error 对象如果含 error_code 字段，附加 localized_message（按 lang 查 i18n 映射）。
    保留原始 message 字段（英文，供 console/debug 用）。
    """
    if not errors:
        return errors
    _i18n = _load_error_code_i18n()
    _lang_map = _i18n.get(lang, {})
    _result = []
    for _e in errors:
        if isinstance(_e, dict):
            _code = _e.get("error_code", "")
            if _code and _code in _lang_map:
                _e["localized_message"] = _lang_map[_code]
            _result.append(_e)
        else:
            _result.append(_e)
    return _result


def _local_bandwidth() -> tuple:
    """psutil.net_io_counters() 汇总非 loopback 网卡吞吐，两次调用差分算速率。
    返回 (percent, mbps)：percent 基于 RUNTIME_BANDWIDTH_THRESHOLD_MBPS（默认 10）。
    首次调用无基准返回 (0.0, 0.0)。"""
    global _BW_SNAPSHOT
    try:
        import os as _os
        import time as _t
        import psutil as _ps
        _per = _ps.net_io_counters(pernic=True)
        _cur_bytes = 0
        for _name, _cnt in _per.items():
            if _name == "lo":
                continue
            _cur_bytes += _cnt.bytes_recv + _cnt.bytes_sent
        _now = _t.time()
        _prev_ts = _BW_SNAPSHOT["ts"]
        _prev_bytes = _BW_SNAPSHOT["bytes"]
        _BW_SNAPSHOT["ts"] = _now
        _BW_SNAPSHOT["bytes"] = _cur_bytes
        if _prev_ts <= 0 or _prev_bytes <= 0:
            return 0.0, 0.0
        _dt = _now - _prev_ts
        _db = _cur_bytes - _prev_bytes
        if _dt <= 0 or _db < 0:
            return 0.0, 0.0
        _mbps = _db * 8 / _dt / 1e6
        _thr = float(_os.environ.get("RUNTIME_BANDWIDTH_THRESHOLD_MBPS", "10"))
        if _thr <= 0:
            return 0.0, round(_mbps, 1)
        _pct = round(min(_mbps / _thr * 100, 100.0), 1)
        return _pct, round(_mbps, 1)
    except Exception:
        return 0.0, 0.0


# ===== 运维可观察性端点（必须在 /{owner}/{repo} 之前注册，否则被通配路由拦截）=====
# ===== 能效看板（Issue #1229）：同属前缀路由，也必须在 /{owner}/{repo} 之前注册 =====


def _efficiency_bot_usernames() -> set:
    """bot 用户名集合（bot 是执行方不是需求方，不计入协作者）。失败按空集合降级。"""
    try:
        from src.user.db import get_all_bot_usernames
        return {u for u in (get_all_bot_usernames() or []) if u}
    except Exception as e:
        log.warning(f"Dashboard efficiency: bot usernames load failed: {e}")
        return set()


_UNASSIGNED_LABEL = "(未归属)"


# bot 名形态（Issue #1337）：必须带分隔符或整名就是 bot，避免把真人 login 误杀。
# 反例（实测）：abbot / robot / talbot 都以 "bot" 结尾但都是真人账号。
_BOT_NAME_RE = re.compile(r"(?:^|[-_])bot$", re.IGNORECASE)


def _is_bot_name(name: str) -> bool:
    """bot 规则识别（Issue #1337）：`bot` / `*_bot` / `*-bot`，大小写不敏感。

    不能用裸 `endswith('bot')`：abbot / robot / talbot 这类真人账号会被当成 bot
    从看板里静默丢掉（与 issue 抱怨的「数据不全」同一性质）。已知 bot 名单
    （managed_repos.bot_username / 全局 bot 配置）仍由调用方 `_bots` 兜底，
    svc-bot-account 这类非 `*bot` 形态的机器人靠名单命中。
    """
    n = (name or "").strip()
    return bool(n) and bool(_BOT_NAME_RE.search(n))


def _efficiency_uid_name_loader(c):
    """批量加载 user_id → 平台用户名映射（gitcode_user 优先，github_login 兜底）。

    分块查询规避 SQLite 变量上限；返回 {user_id: name}（仅含有名字的行）。
    """
    cache: dict = {}

    def _load(uids) -> dict:
        todo = sorted({u for u in uids if u and u not in cache})
        for i in range(0, len(todo), 500):
            part = todo[i:i + 500]
            _ph = ",".join("?" * len(part))
            c.execute(
                "SELECT user_id, COALESCE(NULLIF(gitcode_user,''),"
                " NULLIF(github_login,'')) FROM unified_user_mapping"
                f" WHERE user_id IN ({_ph}) AND user_id>0",
                tuple(part))
            for uid, name in c.fetchall():
                if name:
                    cache[uid] = name
        return cache

    return _load


@router.get("/efficiency", operation_id="dashboard_efficiency", summary="Efficiency board (deprecated)",
            deprecated=True,
            description="**Deprecated（2026-11-09）**：由 `GET /efficiency/board`（flow efficiency 10 卡）"
                        "取代，前端已切换；本端点保留 6 个月兼容期，仅返回旧的按协作者表。\n\n"
                        "Issue #1229: per-contributor aggregation for the current repo — "
            "issues opened / completed (issue table, creator with assignee fallback) "
            "plus LLM token usage attributed via task->issue creator chain "
            "(obs_llm_usage run_kind='task'; total = input+output+cache_read+cache_write). "
            "Bots and subtask rows are excluded; empty contributors = empty state.")
def get_efficiency_board(repo: str = "", limit: int = 50,
                         user: dict = Depends(require_org_member)):
    """能效看板聚合端点：按当前仓库、按协作者输出提需求/完成/token 统计。

    - 提需求/完成：`issue` 单表（Phase C3 权威，get_issue_stage 同源）；
      归属人 = COALESCE(NULLIF(created_by,''), assignee)；完成 = status='completed'；
      排除 bot 创建人与子任务行（is_subtask_mode=1，归属父需求）。
    - token：`obs_llm_usage`（run_kind='task'）按 task_id SUM（五项 token 与
      src/observability/tokens.py 同口径：total 不含 reasoning），经
      `agent_tasks.issue_id` 回链 issue 创建人；孤儿任务（无 issue 行/归属 bot）丢弃。
    - 仓库隔离：repo 支持 owner/repo 与 platform:owner/repo 两种入参，
      双平台前缀（gitcode:/github:）同查；空 repo 返回空态（仓库维度视图）。
    - 任何查询异常降级为该路径空结果（统计不拖垮页面），绝不向上抛。
    """
    from src.repo_key import to_repo_key
    from src.session_manager import db, db_lock

    # repo 归一化：入参可带或不带平台前缀；统一双前缀候选（与 get_dashboard 口径一致）
    repo_keys = []
    repo_key_out = ""
    if repo and repo.strip():
        _base = repo.split(":", 1)[1] if ":" in repo else repo
        repo_keys = [f"gitcode:{_base}", f"github:{_base}"]
        repo_key_out = to_repo_key(_base)

    out = {
        "repo": repo_key_out,
        "total_issues": 0,
        "completed_issues": 0,
        "total_tokens": 0,
        "contributors": [],
    }
    if not repo_keys:
        return out  # 未指定仓库：空态（不做全库聚合）

    try:
        from src.api.deps import check_repo_access
        check_repo_access(user, repo_key_out)
    except Exception as e:
        log.warning(f"Dashboard efficiency: repo access check failed: {e}")
        raise

    _rk_ph = ",".join("?" * len(repo_keys))
    _bots = _efficiency_bot_usernames()

    def _is_contributor(name: str) -> bool:
        return (bool(name) and name != "unknown" and not _is_bot_name(name)
                and name not in _bots)

    # --- ① 提需求/完成数（issue 单表） ---
    contrib: dict = {}   # user -> {issues, completed, input, output}
    load_uid_names = None
    try:
        with db_lock:
            c = db.cursor()
            load_uid_names = _efficiency_uid_name_loader(c)
            c.execute(
                "SELECT COALESCE(NULLIF(created_by,''), '') AS creator,"
                " COALESCE(assignee,'') AS assignee, COALESCE(user_id,0),"
                " COUNT(*), SUM(CASE WHEN status='completed' THEN 1 ELSE 0 END)"
                f" FROM issue WHERE repo_key IN ({_rk_ph}) AND is_subtask_mode=0"
                " GROUP BY creator, assignee, COALESCE(user_id,0)",
                tuple(repo_keys))
            _rows = c.fetchall()
            _uid_names = load_uid_names(r[2] for r in _rows)
            for creator, assignee, uid, cnt, done in _rows:
                name = creator.strip() or assignee.strip() or _uid_names.get(uid, "")
                if not name:
                    # 双空且 user_id 无映射 → 「未归属」档，计数可对账不丢数（Issue #1337 B）
                    name = _UNASSIGNED_LABEL
                elif not _is_contributor(name):
                    continue
                slot = contrib.setdefault(
                    name, {"issues": 0, "completed": 0, "input": 0, "output": 0, "cache": 0})
                slot["issues"] += int(cnt or 0)
                slot["completed"] += int(done or 0)
    except Exception as e:
        log.warning(f"Dashboard efficiency: issue aggregate failed: {e}")

    # --- ② token 归属链：obs_llm_usage → agent_tasks.issue_id → issue 创建人 ---
    try:
        with db_lock:
            c = db.cursor()
            if load_uid_names is None:
                load_uid_names = _efficiency_uid_name_loader(c)
            # 先按任务聚合（行数=任务数，可控），task 行上带仓库与 issue_id
            c.execute(
                "SELECT u.task_id, SUM(u.input_tokens), SUM(u.output_tokens),"
                " SUM(u.cache_read_tokens), SUM(u.cache_write_tokens)"
                " FROM obs_llm_usage u WHERE u.run_kind='task'"
                f" AND u.repo_key IN ({_rk_ph}) GROUP BY u.task_id",
                tuple(repo_keys))
            task_tokens = {
                r[0]: (int(r[1] or 0), int(r[2] or 0), int(r[3] or 0), int(r[4] or 0))
                for r in c.fetchall()
            }
            # 回链分块查询：SQLite 变量上限 999/32766 视版本，大仓库任务数可能超限
            _task_items = list(task_tokens.items())
            _chunk = 500
            _task_rows = []   # (task_id, created_by, assignee, user_id, is_subtask_mode, issue_id)
            for _i in range(0, len(_task_items), _chunk):
                _part = _task_items[_i:_i + _chunk]
                _t_ph = ",".join("?" * len(_part))
                c.execute(
                    "SELECT t.id, i.created_by, i.assignee,"
                    " COALESCE(i.user_id, 0), i.is_subtask_mode, i.id"
                    " FROM agent_tasks t LEFT JOIN issue i"
                    " ON i.repo_key = t.repo_key AND i.issue_number ="
                    " CAST(SUBSTR(t.issue_id, 2) AS INTEGER)"
                    f" WHERE t.id IN ({_t_ph})",
                    tuple(tid for tid, _ in _part))
                _task_rows.extend(c.fetchall())
            _uid_names = load_uid_names(r[3] for r in _task_rows)
            for task_id, created_by, assignee, uid, is_sub, issue_id in _task_rows:
                name = ((created_by or "").strip() or (assignee or "").strip()
                        or _uid_names.get(uid or 0, ""))
                if is_sub:
                    continue  # 子任务行归属父需求
                if not name:
                    # 孤儿任务（LEFT JOIN 未命中、无 issue 行）→ 用量丢弃；
                    # 命中行但归属全空（user_id NULL/0 无映射）→ 「未归属」档，
                    # 口径同 issue 链 COALESCE(user_id,0)（Issue #1337 B）。
                    if issue_id is None:
                        continue
                    name = _UNASSIGNED_LABEL
                elif not _is_contributor(name):
                    continue  # bot 的 issue：命中 issue 行但被 bot 规则排除 → 用量丢弃
                slot = contrib.setdefault(
                    name, {"issues": 0, "completed": 0, "input": 0, "output": 0, "cache": 0})
                it, ot, cr, cw = task_tokens.get(task_id, (0, 0, 0, 0))
                slot["input"] += it
                # cache 读写计入 total（同 tokens.py 口径），保留独立缓存桶：
                # 并入 output 桶会导致 output 列失真，故 total=input+output+cache。
                slot["output"] += ot
                slot["cache"] += cr + cw
    except Exception as e:
        log.warning(f"Dashboard efficiency: token aggregate failed: {e}")

    # --- ③ 汇总排序（issues DESC, tokens DESC, user ASC）+ 截断 ---
    rows = []
    for name, d in contrib.items():
        total = int(d["input"]) + int(d["output"]) + int(d.get("cache", 0))
        rows.append({
            "user": name,
            "issues": int(d["issues"]),
            "completed": int(d["completed"]),
            "input_tokens": int(d["input"]),
            "output_tokens": int(d["output"]),
            "total_tokens": total,
        })
    rows.sort(key=lambda r: (-r["issues"], -r["total_tokens"], r["user"]))
    try:
        _limit = max(1, min(int(limit or 50), 200))
    except (TypeError, ValueError):
        _limit = 50
    out["contributors"] = rows[:_limit]
    out["total_issues"] = sum(r["issues"] for r in rows)
    out["completed_issues"] = sum(r["completed"] for r in rows)
    out["total_tokens"] = sum(r["total_tokens"] for r in rows)
    return out


@router.get("/efficiency/board", operation_id="dashboard_efficiency_board",
            summary="Efficiency flow board",
            description="能效看板 2.0（flow efficiency）：净流入 / 总工作量 / 在制工作项 / "
                        "本周完成 / 超龄未关 / 周吞吐与累计积压 / 周期时间，单请求一次返回。"
                        "口径见 docs/weekly-10/efficiency-board-redesign-2026-11-09.md。")
def get_efficiency_flow_board(repo: str = "", weeks: int = 12,
                              age_threshold_days: int = 7,
                              group_by: str = "all",
                              user: dict = Depends(require_org_member)):
    """能效看板 2.0 聚合端点（P1 六段）。

    - 净流入 / 吞吐：按 ISO 周（周一起算，CST）分桶，累计积压用 created/completed 回算，
      不需要历史快照表。
    - 在制 / 超龄：未关闭 = status 不在 (completed, cancelled)；「超龄」按龄期阈值，
      **不是期限**——grape 没有 due date（payload 有字段但实测全空且未落库）。
    - 周期时间：仅 completed 且有 completed_at 的样本，响应用 sample 暴露样本量。
    - 任务交付：agent_tasks started_at→completed_at；group_by 切 all/completed/failed/
      cancelled，非法值回退 all；by_status / by_agent_type 始终给全量拆分。
    - 仓库隔离：repo 空 → 空态（不做全库聚合）；越权 → 沿用 check_repo_access 的 403/404。
    - 任何查询异常不外抛，按段降级为空结果（与 /efficiency 同策略），不拖垮页面。
    """
    from src.repo_key import to_repo_key
    from src.session_manager import db, db_lock
    from src.observability.efficiency import build_board

    repo_keys = []
    repo_key_out = ""
    if repo and repo.strip():
        _base = repo.split(":", 1)[1] if ":" in repo else repo
        repo_keys = [f"gitcode:{_base}", f"github:{_base}"]
        repo_key_out = to_repo_key(_base)

    if not repo_keys:
        return build_board(None, [], repo_label="", weeks=weeks,
                           age_threshold_days=age_threshold_days,
                           delivery_group="all")

    from src.api.deps import check_repo_access
    check_repo_access(user, repo_key_out)

    try:
        _weeks = max(4, min(int(weeks or 12), 26))
    except (TypeError, ValueError):
        _weeks = 12
    try:
        _threshold = max(1, min(int(age_threshold_days or 7), 90))
    except (TypeError, ValueError):
        _threshold = 7
    _group = (group_by or "all").strip().lower()
    if _group not in ("all", "completed", "failed", "cancelled"):
        _group = "all"

    try:
        with db_lock:
            return build_board(db.cursor(), repo_keys, repo_label=repo_key_out,
                               weeks=_weeks, age_threshold_days=_threshold,
                               bot_names=_efficiency_bot_usernames(),
                               delivery_group=_group)
    except Exception as e:
        log.warning(f"Dashboard efficiency board failed: {e}")
        return {
            "repo": repo_key_out, "empty": True, "error": "aggregation_failed",
            "summary": {}, "net_flow": {}, "throughput": {},
            "aging": {}, "cycle_time": {},
        }


def _eff_repo_keys(repo: str):
    """repo 入参归一化 → (repo_keys, repo_key_out)。空入参返回空（仓库维度视图）。"""
    from src.repo_key import to_repo_key
    if not (repo and repo.strip()):
        return [], ""
    base = repo.split(":", 1)[1] if ":" in repo else repo
    return [f"gitcode:{base}", f"github:{base}"], to_repo_key(base)


@router.get("/efficiency/daily", operation_id="dashboard_efficiency_daily",
            summary="Efficiency board daily drill-down",
            description="某周按天拆解（新建/关闭/当日累计积压）：④本周完成、⑥周吞吐的点击下钻。"
                        "week 为空取当前周；与看板同源回算，周值 = 该周 7 天之和。")
def get_efficiency_daily(repo: str = "", week: str = "",
                         user: dict = Depends(require_org_member)):
    from datetime import datetime as _dt
    from src.session_manager import db, db_lock
    from src.observability.efficiency import (build_daily, parse_week_label,
                                              _fetch_issue_rows, iso_week_label)

    repo_keys, repo_key_out = _eff_repo_keys(repo)
    if not repo_keys:
        return {"repo": "", "empty": True, "message": "no_repo_selected", "days": []}
    from src.api.deps import check_repo_access
    check_repo_access(user, repo_key_out)

    monday = parse_week_label(week) or parse_week_label(iso_week_label(_dt.now()))
    if monday is None:
        return {"repo": repo_key_out, "empty": True, "error": "invalid_week", "days": []}
    try:
        with db_lock:
            out = build_daily(_fetch_issue_rows(db.cursor(), repo_keys), monday)
        out["repo"] = repo_key_out
        out["empty"] = False
        return out
    except Exception as e:
        log.warning(f"Dashboard efficiency daily failed: {e}")
        return {"repo": repo_key_out, "empty": True, "error": "aggregation_failed", "days": []}


@router.get("/efficiency/issues", operation_id="dashboard_efficiency_issues",
            summary="Efficiency board issue drill-down",
            description="某卡片某一行对应的 issue 列表（含场景）：超龄未关/周期时间/积压龄期的点击下钻。"
                        "kind=aging(按龄期桶) | overdue(超阈值) | cycle(按周期桶)；"
                        "limit/offset 分页，total 为全量条数。")
def get_efficiency_issues(repo: str = "", kind: str = "aging", bucket: str = "",
                          threshold_days: int = 7, limit: int = 20, offset: int = 0,
                          user: dict = Depends(require_org_member)):
    from src.session_manager import db, db_lock
    from src.observability.efficiency import build_issue_list

    repo_keys, repo_key_out = _eff_repo_keys(repo)
    if not repo_keys:
        return {"repo": "", "empty": True, "message": "no_repo_selected", "items": []}
    from src.api.deps import check_repo_access
    check_repo_access(user, repo_key_out)

    try:
        _threshold = max(1, min(int(threshold_days or 7), 90))
    except (TypeError, ValueError):
        _threshold = 7
    try:
        _limit = max(1, min(int(limit or 20), 200))
    except (TypeError, ValueError):
        _limit = 20
    try:
        _offset = max(0, int(offset or 0))
    except (TypeError, ValueError):
        _offset = 0
    try:
        with db_lock:
            out = build_issue_list(db.cursor(), repo_keys, kind=(kind or "aging"),
                                   bucket=bucket or "", threshold_days=_threshold,
                                   limit=_limit, offset=_offset)
        out["repo"] = repo_key_out
        return out
    except Exception as e:
        log.warning(f"Dashboard efficiency issues failed: {e}")
        return {"repo": repo_key_out, "items": [], "total": 0, "empty": True,
                "error": "aggregation_failed"}


@router.get("/ops/overview", operation_id="dashboard_ops_overview", summary="Ops overview", description="Real-time ops overview: task queue depth, running count, stuck detection and runtime online status.")
def get_ops_overview(repo: str = "", user: dict = Depends(require_org_member)):
    """实时运维概览：任务队列深度、运行数、卡住检测、runtime 在线状态。

    参考 multica BusinessSampler（metrics/business_sampler.go）的实时采样设计，
    但通过 Dashboard API 暴露给前端看板，无需 Prometheus 基础设施。

    需登录：user scope worker 按当前用户过滤（与 Agent 运行时页面可见性一致）。
    数据全部来自 agent_tasks / agent_runtimes 表的实时 SQL 查询。
    repo 参数：仓库名（requiredsa/hello-flask），过滤仅该仓库的任务。
    """
    from src.session_manager import db, db_lock
    from src.runtime.health import derive_runtime_health
    from datetime import datetime, timezone, timedelta
    import time as _time

    now_ts = _time.time()
    now_iso = datetime.now(timezone.utc).isoformat()

    # repo 过滤条件
    _repo_filter = ""
    _repo_params = []
    repo_key = ""
    if repo:
        _base = repo.split(":", 1)[1] if ":" in repo else repo
        _repo_keys = [f"gitcode:{_base}", f"github:{_base}"]
        repo_key = _repo_keys[0]
        _repo_filter = f" AND repo_key IN ({','.join('?' * len(_repo_keys))})"
        _repo_params = _repo_keys
    # stuck 阈值：running 超过统一超时配置的派生预警窗口（默认超时 3600s → 22min，
    # 超时前预警；Issue #470 起随 RUNTIME_TASK_TIMEOUT 配置派生，不再写死 22min）
    stuck_cutoff = (datetime.now(timezone.utc) - timedelta(minutes=_stuck_minutes())).isoformat()

    # 组织隔离：非个人组织只显示该组织的引擎/凭据
    from src.user.org_db import is_org_personal
    _org_id = user.get("org_id", 0)
    _is_personal_org = is_org_personal(_org_id) if _org_id else True
    _filter_org_id = 0 if _is_personal_org else _org_id

    # 组织级任务过滤条件
    _task_org_filter = ""
    _task_org_params = []
    if _filter_org_id:
        _task_org_filter = " AND org_id=?"
        _task_org_params = [_filter_org_id]

    with db_lock:
        c = db.cursor()

        # --- 任务队列状态分布（task 级别）---
        task_status_counts = {"queued": 0, "dispatched": 0, "running": 0,
                              "completed": 0, "failed": 0, "cancelled": 0}
        try:
            c.execute(f"SELECT status, COUNT(*) FROM agent_tasks WHERE 1=1{_repo_filter}{_task_org_filter} GROUP BY status", tuple(_repo_params + _task_org_params))
            for status, count in c.fetchall():
                if status in task_status_counts:
                    task_status_counts[status] = count
        except Exception:
            pass

        # --- Issue 级别统计（一个 issue = 一个任务）---
        issue_stats = {"total": 0, "completed": 0, "in_progress": 0, "failed": 0}
        try:
            # 每个 issue 取最新 task 的状态作为该 issue 的状态
            c.execute(f"""
                SELECT t.status, COUNT(*) FROM (
                    SELECT issue_id, MAX(id) as max_id FROM agent_tasks
                    WHERE issue_id IS NOT NULL AND issue_id <> '' {(_repo_filter.replace('WHERE', 'AND') if _repo_filter else '')}{_task_org_filter}
                    GROUP BY issue_id
                ) latest JOIN agent_tasks t ON t.id = latest.max_id
                GROUP BY t.status
            """, tuple(_repo_params + _task_org_params))
            _issue_status_map = {}
            for status, count in c.fetchall():
                _issue_status_map[status] = count
            issue_stats["total"] = sum(_issue_status_map.values())
            issue_stats["completed"] = _issue_status_map.get("completed", 0)
            issue_stats["failed"] = _issue_status_map.get("failed", 0)
            issue_stats["in_progress"] = (_issue_status_map.get("running", 0) +
                                          _issue_status_map.get("dispatched", 0) +
                                          _issue_status_map.get("queued", 0))
        except Exception:
            pass

        # --- in_progress（dispatched + running）---
        in_progress = task_status_counts["dispatched"] + task_status_counts["running"]

        # --- stuck 检测：running 且 started_at < 30 分钟前 ---
        stuck_tasks = []
        try:
            c.execute(
                "SELECT id, agent_type, runtime_id, started_at, repo_key, issue_id, event_seq "
                "FROM agent_tasks WHERE status='running' AND started_at IS NOT NULL AND started_at < ?"
                + _repo_filter + _task_org_filter +
                " ORDER BY started_at ASC LIMIT 100",
                (stuck_cutoff,) + tuple(_repo_params) + tuple(_task_org_params),
            )
            for r in c.fetchall():
                stuck_tasks.append({
                    "id": r[0], "agent_type": r[1] or "", "runtime_id": r[2],
                    "started_at": normalize_event_time(r[3]), "repo_key": r[4] or "",
                    "issue_id": r[5] or "", "event_seq": r[6] or 0,
                })
        except Exception:
            pass

        # --- 最近 failed 任务（最近 20 条）---
        failed_tasks = []
        try:
            c.execute(
                "SELECT id, agent_type, runtime_id, started_at, completed_at, repo_key, issue_id, event_seq, result_text, exit_code, errors "
                "FROM agent_tasks WHERE status='failed'" + _repo_filter + _task_org_filter + " ORDER BY completed_at DESC",
                tuple(_repo_params) + tuple(_task_org_params),
            )
            for r in c.fetchall():
                # errors 字段是 JSON 数组字符串，解析后取前 3 条
                import json as _json
                _errors = []
                try:
                    _errors = _json.loads(r[10]) if r[10] else []
                except Exception:
                    _errors = [r[10]] if r[10] else []
                # R16.2.2: error_code 映射本地化文案
                _errors = _resolve_error_messages(_errors[:3])
                failed_tasks.append({
                    "id": r[0], "agent_type": r[1] or "", "runtime_id": r[2],
                    "started_at": normalize_event_time(r[3]), "completed_at": normalize_event_time(r[4]),
                    "repo_key": r[5] or "", "issue_id": r[6] or "", "event_seq": r[7] or 0,
                    "result_text": (r[8] or "")[:200],
                    "exit_code": r[9],
                    "errors": _errors,
                })
        except Exception:
            pass

        # --- 按来源（agent_type）分组排队/运行数 ---
        task_by_source = {}
        try:
            c.execute(
                "SELECT agent_type, status, COUNT(*) FROM agent_tasks "
                "WHERE status IN ('queued','dispatched','running')" + _repo_filter + _task_org_filter + " GROUP BY agent_type, status",
                tuple(_repo_params) + tuple(_task_org_params),
            )
            for atype, status, count in c.fetchall():
                key = atype or "unknown"
                if key not in task_by_source:
                    task_by_source[key] = {"queued": 0, "running": 0}
                if status == "queued":
                    task_by_source[key]["queued"] += count
                elif status in ("dispatched", "running"):
                    task_by_source[key]["running"] += count
        except Exception:
            pass

        # --- worker（凭据）列表 + 资源指标 ---
        # 本机直连引擎（credential_id=0）用 psutil 采集本机资源
        # interval=None 非阻塞：返回自上次调用以来的平均值（10s 刷新间隔内的 CPU 均值）
        # 首次调用返回 0.0（无基准），后续调用返回真实值
        _local_cpu = _local_mem = _local_disk = _local_tmp = 0.0
        _local_bw_pct = 0.0
        _local_bw_mbps = 0.0
        _local_load = ""
        try:
            import psutil as _ps
            _local_cpu = _ps.cpu_percent(interval=None)
            _local_bw_pct, _local_bw_mbps = _local_bandwidth()
            # MEM 用 /proc/meminfo 读取实际进程占用（total - free - buffers - cached，不含缓存）
            try:
                with open("/proc/meminfo") as _mf:
                    _mi = {}
                    for _line in _mf:
                        _p = _line.split(":")
                        if len(_p) == 2:
                            _mi[_p[0]] = int(_p[1].strip().split()[0]) * 1024
                    _total = _mi["MemTotal"]
                    _free = _mi.get("MemFree", 0)
                    _buffers = _mi.get("Buffers", 0)
                    _cached = _mi.get("Cached", 0)
                    _used = _total - _free - _buffers - _cached
                    if _total > 0:
                        _local_mem = round(_used / _total * 100, 1)
                    else:
                        _local_mem = _ps.virtual_memory().percent
            except Exception:
                _local_mem = _ps.virtual_memory().percent
            _local_disk = _ps.disk_usage("/").percent
            # TMP: /tmp 目录使用率（tmpfs 内存文件系统，独立于根分区）
            try:
                _local_tmp = _ps.disk_usage("/tmp").percent
            except Exception:
                _local_tmp = 0.0
            _local_load = ",".join(f"{x:.2f}" for x in _ps.getloadavg())
        except Exception:
            pass

        worker_list = []
        worker_online = 0
        worker_total = 0

        # 主控机指标由后台定时任务（_server_metrics_loop）每 15 秒写入 DB，
        # dashboard 不再采集/写入本机指标，改为纯读取。

        # 查 worker（凭据），排除已注销（revoked）
        # worker 可见性：非个人组织严格按 org_id 隔离；个人组织 global + user scope
        _uname = user.get("username") or ""
        try:
            _stale_cutoff = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
            if _filter_org_id:
                # 非个人组织：只查 org_id 匹配的凭据
                c.execute(
                    "SELECT id, name, device_name, status, last_seen_at, last_ip, "
                    "cpu_percent, memory_percent, disk_percent, tmp_percent, bandwidth_percent, bandwidth_mbps, load_avg, owner_name "
                    "FROM runtime_credentials WHERE status != 'revoked' "
                    "AND last_seen_at IS NOT NULL AND last_seen_at >= ? "
                    "AND org_id=? ORDER BY id",
                    (_stale_cutoff, _filter_org_id),
                )
            elif repo:
                # 仓库级看板：global + 自己的 user + 该 repo 的 worker
                c.execute(
                    "SELECT id, name, device_name, status, last_seen_at, last_ip, "
                    "cpu_percent, memory_percent, disk_percent, tmp_percent, bandwidth_percent, bandwidth_mbps, load_avg, owner_name "
                    "FROM runtime_credentials WHERE status != 'revoked' "
                    "AND last_seen_at IS NOT NULL AND last_seen_at >= ? "
                    "AND (scope='global' OR (scope='user' AND owner_name=?) OR repo_key=? OR repo_key=?) ORDER BY id",
                    (_stale_cutoff, _uname, repo_key, repo),
                )
            else:
                # 个人组织全局看板：global + 自己的 user scope worker
                c.execute(
                    "SELECT id, name, device_name, status, last_seen_at, last_ip, "
                    "cpu_percent, memory_percent, disk_percent, tmp_percent, bandwidth_percent, bandwidth_mbps, load_avg, owner_name "
                    "FROM runtime_credentials WHERE status != 'revoked' "
                    "AND last_seen_at IS NOT NULL AND last_seen_at >= ? "
                    "AND (scope='global' OR (scope='user' AND owner_name=?)) ORDER BY id",
                    (_stale_cutoff, _uname),
                )
            for w in c.fetchall():
                wid, wname, wdevice, wstatus, wlast_seen, wip, wcpu, wmem, wdisk, wtmp, wbw, wbm, wload, wowner = w
                worker_total += 1
                w_health = derive_runtime_health(wstatus or "offline", wlast_seen, now_ts)
                w_online = w_health == "online"
                if w_online:
                    worker_online += 1
                worker_list.append({
                    "id": wid, "name": wname or "", "device_name": wdevice or "",
                    "status": wstatus or "offline", "health": w_health,
                    "last_seen_at": normalize_event_time(wlast_seen), "last_ip": wip or "",
                    "cpu_percent": wcpu or 0, "memory_percent": wmem or 0,
                    "disk_percent": wdisk or 0, "tmp_percent": wtmp or 0,
                    "bandwidth_percent": wbw or 0,
                    "bandwidth_mbps": wbm or 0,
                    "load_avg": wload or "",
                    "owner_name": wowner or "",
                })
        except Exception:
            pass

        # 补充本机直连设备（credential_id=0 的引擎不在 runtime_credentials 表中）
        try:
            _local_hostname = ""
            try:
                import socket as _sock
                _local_hostname = _sock.gethostname()
            except Exception:
                pass
            c.execute(
                "SELECT ar.id, ar.name, ar.status, ar.last_seen_at, ar.host_name, "
                "ar.healthy, ar.busy_slots, ar.max_concurrent "
                "FROM agent_runtimes ar WHERE ar.credential_id = 0"
                + (" AND ar.org_id=?" if _filter_org_id else ""),
                ([_filter_org_id] if _filter_org_id else [])
            )
            _local_rts = c.fetchall()
            if _local_rts:
                _has_local_worker = any(w.get("device_name") == (_local_hostname or "本机直连") for w in worker_list)
                if not _has_local_worker:
                    worker_list.append({
                        "id": 0, "name": _local_hostname or "本机直连",
                        "device_name": _local_hostname or "本机直连",
                        "status": "online", "health": "online",
                        "last_seen_at": "", "last_ip": "127.0.0.1",
                        "cpu_percent": _local_cpu, "memory_percent": _local_mem,
                        "disk_percent": _local_disk, "tmp_percent": _local_tmp,
                        "bandwidth_percent": _local_bw_pct,
                        "bandwidth_mbps": _local_bw_mbps,
                        "load_avg": _local_load, "owner_name": "",
                    })
                    worker_total += 1
                    worker_online += 1
                else:
                    # 本机直连设备已在 worker_list 中（hostname 匹配），更新实时指标
                    for w in worker_list:
                        if w.get("device_name") == (_local_hostname or "本机直连"):
                            w["cpu_percent"] = _local_cpu
                            w["memory_percent"] = _local_mem
                            w["disk_percent"] = _local_disk
                            w["tmp_percent"] = _local_tmp
                            w["bandwidth_percent"] = _local_bw_pct
                            w["bandwidth_mbps"] = _local_bw_mbps
                            w["load_avg"] = _local_load
                            break
        except Exception:
            pass

        # 查所有 runtime（引擎），关联 worker 信息
        runtimes_online = 0
        runtimes_total = 0
        runtime_by_mode = {"local": 0, "remote": 0, "unknown": 0}
        runtime_list = []
        # 查每个 runtime 当前 running 任务数（实时负载）
        _running_counts = {}
        try:
            c.execute(
                "SELECT runtime_id, COUNT(*) FROM agent_tasks "
                "WHERE status='running'" + _task_org_filter + " GROUP BY runtime_id",
                tuple(_task_org_params),
            )
            for row in c.fetchall():
                _running_counts[row[0]] = row[1]
        except Exception:
            pass
        # 查每个引擎已完成任务的平均耗时（秒）
        _avg_durations = {}
        # 引擎探测结果（每 10 分钟 CLI 测试对话）
        _probe_results = get_probe_results()
        try:
            c.execute(
                "SELECT runtime_id, AVG(CASE "
                "WHEN completed_at IS NOT NULL AND started_at IS NOT NULL "
                "THEN (strftime('%s', completed_at) - strftime('%s', started_at)) "
                "ELSE NULL END) as avg_sec "
                "FROM agent_tasks WHERE status IN ('completed','failed') AND runtime_id > 0"
                + _task_org_filter + " GROUP BY runtime_id",
                tuple(_task_org_params),
            )
            for row in c.fetchall():
                _avg_durations[row[0]] = round(row[1] or 0, 1) if row[1] else 0
        except Exception:
            try:
                # MySQL 兼容（TIMESTAMPDIFF）
                c.execute(
                    "SELECT runtime_id, AVG(TIMESTAMPDIFF(SECOND, started_at, completed_at)) as avg_sec "
                    "FROM agent_tasks WHERE status IN ('completed','failed') AND runtime_id > 0 "
                    "AND started_at IS NOT NULL AND completed_at IS NOT NULL"
                    + _task_org_filter + " GROUP BY runtime_id",
                    tuple(_task_org_params),
                )
                for row in c.fetchall():
                    _avg_durations[row[0]] = round(row[1] or 0, 1) if row[1] else 0
            except Exception:
                pass
        try:
            c.execute(
                "SELECT ar.id, ar.name, ar.credential_id, ar.status, ar.last_seen_at, ar.provider, "
                "ar.healthy, ar.busy_slots, ar.max_concurrent, ar.version, "
                "rc.name AS rc_name, rc.device_name, rc.status AS cred_status, rc.id AS cred_pk, ar.host_name, "
                "ar.probe_status, ar.probe_latency, ar.probe_last, ar.probe_error, "
                "ar.org_id "
                "FROM agent_runtimes ar "
                "LEFT JOIN runtime_credentials rc ON ar.credential_id = rc.id "
                + ("WHERE ar.org_id=?" if _filter_org_id else "") +
                " ORDER BY ar.id",
                ([_filter_org_id] if _filter_org_id else [])
            )
            for r in c.fetchall():
                rid, rname, cid, rstatus, rlast_seen, rprovider, rhealthy, rbusy, rmax, rver, rc_name, rdev, rcred_status, cred_pk, rhost, rprobe_status, rprobe_latency, rprobe_last, rprobe_error, _rt_org_id = r
                # 跳过已注销（revoked）凭据绑定的远端引擎（与 Workers 视图一致）。
                # Issue #417：吊销=物理删除凭据行，LEFT JOIN 得 NULL（cred_pk 为 None）
                # 的孤儿 agent_runtimes 行同样跳过——否则已吊销设备在 runtime 表残留显示
                # （凭据行存在但 status 为 NULL 的历史合法行不误伤，与 instances.py 语义一致）。
                if (cid or 0) > 0 and (rcred_status == 'revoked' or cred_pk is None):
                    continue
                # 跳过从未连接或长期失联(>7天)的远端引擎（与 Workers 视图过滤一致）
                if (cid or 0) > 0:
                    if not rlast_seen:
                        continue
                    try:
                        _seen_ts = datetime.fromisoformat(rlast_seen.replace('Z', '+00:00')).timestamp() if rlast_seen else 0
                    except Exception:
                        _seen_ts = 0
                    if _seen_ts and (now_ts - _seen_ts) > 7 * 86400:
                        continue
                # 远端引擎跟随 Worker scope 过滤（与 Workers 视图一致）
                if (cid or 0) > 0 and repo:
                    # 仓库级看板：global + 自己的 user + 该 repo 的 worker
                    pass  # repo 级过滤由前端按需处理，后端不过滤
                elif (cid or 0) > 0:
                    # 全局看板：global scope worker 的引擎才展示给所有用户
                    # user scope worker 的引擎只展示给 owner
                    _cred_scope = ''
                    _cred_owner = ''
                    try:
                        c.execute("SELECT scope, owner_name FROM runtime_credentials WHERE id=?", (cid,))
                        _cr = c.fetchone()
                        if _cr:
                            _cred_scope, _cred_owner = _cr[0] or '', _cr[1] or ''
                    except Exception:
                        pass
                    if _cred_scope == 'user' and _cred_owner != _uname:
                        continue
                runtimes_total += 1
                base_health = derive_runtime_health(rstatus or "offline", rlast_seen, now_ts)
                mode = "remote" if (cid or 0) > 0 else "local"
                # 本机直连引擎始终算在线
                is_online = (cid or 0) == 0 or base_health == "online"
                if is_online:
                    runtimes_online += 1
                    runtime_by_mode[mode] = runtime_by_mode.get(mode, 0) + 1
                # running 任务数（实时负载）
                running = _running_counts.get(rid, 0)
                max_conc = rmax or 0
                # 本机直连引擎（credential_id=0）始终可用，不看心跳
                if (cid or 0) == 0:
                    if running == 0:
                        health = "idle"
                    elif running < max_conc:
                        health = "normal"
                    elif running < max_conc * 2:
                        health = "busy"
                    else:
                        health = "overload"
                # 远端引擎：先看心跳，online 时再看负载
                elif base_health == "online":
                    if running == 0:
                        health = "idle"
                    elif running < max_conc:
                        health = "normal"
                    elif running < max_conc * 2:
                        health = "busy"
                    else:
                        health = "overload"
                else:
                    health = base_health
                # 引擎探测结果：本机用内存（engine_probe），远端用 DB（心跳上报）
                if (cid or 0) == 0:
                    _probe = _probe_results.get(rname or "", {})
                else:
                    _probe = {
                        "status": rprobe_status or "pending",
                        "latency": rprobe_latency or 0,
                        "last_probe": rprobe_last or "",
                        "error": rprobe_error or "",
                    }
                # 本机直连引擎的 device_name 用 host_name（区分多台主控机）
                # 远端引擎 device_name = 凭据自定义名 rc.name 唯一来源（Issue #701 收口）：
                # 为空下发空值，前端统一占位「未命名设备」，不回退边端自报 device_name
                if (cid or 0) == 0:
                    _dev_name = rhost or "本机直连"
                else:
                    _dev_name = (rc_name or "").strip()
                from src.runtime.instances import to_display as _td
                runtime_list.append({
                    "id": rid, "name": _td(rname or ""), "status": rstatus or "offline",
                    "health": health, "mode": mode,
                    "provider": rprovider or "", "last_seen_at": normalize_event_time(rlast_seen),
                    "healthy": bool(rhealthy), "busy_slots": rbusy or 0, "max_concurrent": max_conc,
                    "running": running, "avg_duration": _avg_durations.get(rid, 0),
                    "version": rver or "", "device_name": _dev_name,
                    "probe_status": _probe.get("status", "pending"),
                    "probe_latency": _probe.get("latency", 0),
                    "probe_last": _probe.get("last_probe", ""),
                    "probe_error": _probe.get("error", ""),
                })
                # 引擎健康分（Issue #134 展示增强）
                try:
                    from src.runtime.health_scorer import get_health_score
                    _hd = get_health_score(rid)
                    runtime_list[-1]["health_score"] = _hd.get("health_score")
                    runtime_list[-1]["health_consecutive_failures"] = _hd.get("consecutive_failures", 0)
                    runtime_list[-1]["health_heartbeat_alive"] = _hd.get("heartbeat_alive")
                except Exception:
                    runtime_list[-1]["health_score"] = None
        except Exception:
            pass

        # --- 今日任务统计（UTC 日期前缀匹配 created_at ISO 时间戳）---
        today_prefix = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        today_stats = {"created": 0, "completed": 0, "failed": 0}
        try:
            c.execute(
                "SELECT status, COUNT(*) FROM agent_tasks "
                "WHERE created_at LIKE ?" + _repo_filter + _task_org_filter + " GROUP BY status",
                (today_prefix + "%",) + tuple(_repo_params) + tuple(_task_org_params),
            )
            for status, count in c.fetchall():
                if status in today_stats:
                    today_stats[status] = count
        except Exception:
            pass

    return {
        "timestamp": now_iso,
        "tasks": {
            "status_counts": task_status_counts,
            "issue_stats": issue_stats,
            "in_progress": in_progress,
            "stuck_count": len(stuck_tasks),
            "stuck_tasks": stuck_tasks,
            "failed_tasks": failed_tasks,
            "by_source": task_by_source,
            "today": today_stats,
        },
        "runtimes": {
            "total": runtimes_total,
            "online": runtimes_online,
            "offline": runtimes_total - runtimes_online,
            "by_mode": runtime_by_mode,
            "list": runtime_list,
        },
        "workers": {
            "total": worker_total,
            "online": worker_online,
            "offline": worker_total - worker_online,
            "list": worker_list,
        },
    }


@router.get("/ops/engine-select-explain", operation_id="dashboard_ops_engine_select_explain",
            summary="Engine selection explain", description="Issue #605: explain engine dispatch candidates "
            "for a (repo, agent_type, scene) — bound engines, per-candidate filter reasons, "
            "broadcast pool and legacy selection result. Read-only.")
def get_engine_select_explain(repo: str = "", agent_type: str = "vod", scene_id: str = "",
                              user: dict = Depends(require_org_member)):
    """主控侧取证接口（Issue #605）：解释引擎派发候选与过滤原因（只读）。

    - repo：仓库 repo_key（如 gitcode:owner/repo 或 owner/repo）
    - agent_type：agent 角色（vod/coding/qa/sre/issue）
    - scene_id：场景 ID（空则无场景绑定口径）
    返回 explain_engine_selection 结果：bound_engine_ids / candidates（含 reasons）/
    pool_engine_ids / selected。库内字段（healthy/online/last_seen）与 console 展示
    不一致的核对可由 candidates 字段直接完成。
    """
    from src.runtime.engine_select import explain_engine_selection
    try:
        return {"ok": True, **explain_engine_selection(repo, agent_type, scene_id=scene_id)}
    except Exception as e:
        log.warning(f"Dashboard: engine-select-explain failed: {e}")
        return {"ok": False, "error": str(e)[:200]}


@router.get("/ops/probe-now", operation_id="dashboard_ops_probe_now")
def probe_engines_now(user: dict = Depends(require_org_member)):
    """需登录：组织成员可见。手动触发引擎探测（60s 防抖：窗口内重复请求返回上次结果，不重新探测）。"""
    from src.observability.engine_probe import get_probe_results, _last_probe_time
    import time, threading
    now = time.time()
    last = _last_probe_time if _last_probe_time else 0
    if now - last < 300:
        # 防抖窗口内（5分钟），返回上次结果
        return {"status": "throttled", "wait": int(300 - (now - last)), "results": get_probe_results()}
    # 防抖窗口外：后台线程异步执行探测，立即返回（不阻塞当前请求）
    def _bg_probe():
        try:
            from src.observability.engine_probe import probe_all_now
            probe_all_now()
        except Exception as e:
            import logging
            logging.getLogger("devops-bot").warning(f"bg probe error: {e}")
    threading.Thread(target=_bg_probe, daemon=True).start()
    return {"status": "accepted", "results": get_probe_results()}


@router.get("/ops/error-logs/sources", operation_id="dashboard_ops_error_log_sources")
async def get_error_log_sources(user: dict = Depends(require_org_member)):
    """需登录：组织成员可见。返回可用的日志来源列表（主控 + 各 worker）。"""
    import os as _os, glob as _glob2
    from src.config import LOG_DIR
    from src.session_manager import db, db_lock

    sources = [{"source": "主控", "id": 0}]
    _worker_dir = _os.path.join(LOG_DIR, "workers")
    for _wf in sorted(_glob2.glob(_os.path.join(_worker_dir, "*.log"))):
        _cred_id = _os.path.splitext(_os.path.basename(_wf))[0]
        _wname = f"worker-{_cred_id}"
        try:
            with db_lock:
                c = db.cursor()
                c.execute("SELECT name, device_name FROM runtime_credentials WHERE id=?", (_cred_id,))
                row = c.fetchone()
                if row:
                    _wname = row[1] or row[0] or _wname
        except Exception:
            pass
        sources.append({"source": _wname, "id": int(_cred_id) if _cred_id.isdigit() else 0})
    return {"sources": sources}


@router.get("/ops/error-logs", operation_id="dashboard_ops_error_logs")
async def get_error_logs(limit: int = 100, level: str = "", repo: str = "", user_id: str = "", source: str = "", user: dict = Depends(require_org_member)):
    """最近错误/警告日志。

    同时读取新旧两个日志文件：
    - data/logs/devops-bot.log（新 JSON 结构化日志）
    - data/devops-bot.log（旧纯文本日志 + 轮转文件）

    参数：
    - limit: 最多返回条数（默认 100）
    - level: 过滤级别 — ""（ERROR+WARNING）/"ERROR"/"WARNING"
    - repo: 仓库名（requiredsa/hello-flask），过滤仅该仓库的日志
    - user_id: 用户 ID，过滤仅该用户的日志

    需登录：组织成员可见。运维诊断数据。
    """
    import os, json as _json, glob as _glob, re as _re
    from src.config import LOG_DIR

    allowed_levels = {"ERROR", "WARNING"}
    if level and level.upper() in allowed_levels:
        allowed_levels = {level.upper()}

    # repo/user_id 过滤条件
    _repo_filter = ""
    _repo_filter_list = []
    _base = ""
    if repo:
        _base = repo.split(":", 1)[1] if ":" in repo else repo
        _repo_filter_list = [f"gitcode:{_base}", f"github:{_base}"]
        _repo_filter = _repo_filter_list[0]

    entries = []
    files_read = []

    def _match_repo(entry):
        """检查日志条目是否匹配指定的 repo。
        - 无 repo 过滤条件：全部返回
        - 日志有 repo_key：必须精确匹配（支持跨平台前缀）
        - 日志无 repo_key 但 message 含 owner/repo 格式仓库名：
          匹配当前仓库则展示，含其他仓库则不展示
        - 日志不含任何仓库名：始终展示（系统级日志）"""
        if not _repo_filter_list:
            return True
        entry_repo = entry.get("repo_key", "")
        if entry_repo:
            return entry_repo in _repo_filter_list
        msg = entry.get("message", "")
        repo_short = _base
        if repo_short in msg:
            return True
        # 检查 message 中是否含其他仓库名（owner/repo 格式，排除路径段如 api/v5）
        import re as _re2
        _repo_pattern = _re2.compile(r'\b([a-zA-Z0-9_-]+)/([a-zA-Z0-9_.-]+)\b')
        for m in _repo_pattern.finditer(msg):
            candidate = f"{m.group(1)}/{m.group(2)}"
            if candidate != repo_short and not m.group(1).startswith("api"):
                return False  # 含其他仓库名，不展示
        # 不含任何仓库名，系统级日志，始终展示
        return True

    def _match_user(entry):
        """检查日志条目是否匹配指定的 user_id"""
        if not user_id:
            return True
        entry_uid = str(entry.get("user_id", ""))
        if entry_uid and entry_uid == str(user_id):
            return True
        return False

    def _parse_json_log(filepath):
        """解析 JSON 行格式日志"""
        nonlocal entries
        if not os.path.exists(filepath):
            return
        files_read.append(filepath)
        try:
            with open(filepath, "r", encoding="utf-8", errors="replace") as f:
                lines = f.readlines()
            for line in reversed(lines):
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = _json.loads(line)
                    if entry.get("level", "") in allowed_levels and _match_repo(entry) and _match_user(entry) and not _is_http_noise(entry):
                        entries.append(entry)
                        if len(entries) >= limit:
                            return
                except (_json.JSONDecodeError, ValueError):
                    continue
        except Exception:
            pass

    def _is_http_noise(entry):
        """过滤掉正常行为的 HTTP 请求日志（非真正错误）。
        保留 runtime/worker 凭据失败的 401（运维需感知 worker 掉线）。"""
        msg = entry.get("message", "")
        path = entry.get("path", "")
        status = entry.get("status")
        # 401：非 runtime 的是 token 过期刷新，过滤
        if status == 401 or "status=401" in msg:
            if "/api/runtime/" in path or "/api/runtime/" in msg:
                return False  # runtime 401 保留
            return True
        # 403/404：health 限制、favicon、SPA 路由等，过滤
        if status in (403, 404) or "status=403" in msg or "status=404" in msg:
            return True
        return False

    def _parse_text_log(filepath):
        """解析旧纯文本格式日志（正则提取 level + message）"""
        nonlocal entries
        if not os.path.exists(filepath):
            return
        files_read.append(filepath)
        # 旧格式: "2026-08-26T10:30:00.000Z - ERROR - devops-bot - message..."
        # 或: "ERROR:devops-bot:message"
        _re_json = _re.compile(r'^\s*\{.*\}\s*$')
        _re_text = _re.compile(
            r'(\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}[.\dZ]*)\s*[-_]\s*'
            r'(ERROR|WARNING|WARN)\s*[-_]\s*'
            r'(\S+)\s*[-_]\s*(.*)',
            _re.IGNORECASE
        )
        _re_simple = _re.compile(r'^(ERROR|WARNING|WARN)[:\s]+(.+)', _re.IGNORECASE)
        try:
            with open(filepath, "r", encoding="utf-8", errors="replace") as f:
                lines = f.readlines()
            for line in reversed(lines):
                line = line.strip()
                if not line:
                    continue
                # 跳过 JSON 行（已由 _parse_json_log 处理）
                if _re_json.match(line):
                    try:
                        entry = _json.loads(line)
                        if entry.get("level", "") in allowed_levels and _match_repo(entry) and _match_user(entry) and not _is_http_noise(entry):
                            entries.append(entry)
                            if len(entries) >= limit:
                                return
                    except Exception:
                        pass
                    continue
                # 纯文本格式
                m = _re_text.match(line)
                if m:
                    lvl = m.group(2).upper()
                    if lvl == "WARN":
                        lvl = "WARNING"
                    if lvl in allowed_levels:
                        _e = {"ts": m.group(1), "level": lvl,
                            "component": m.group(3), "request_id": "-",
                            "message": m.group(4)[:500]}
                        if _match_repo(_e) and _match_user(_e) and not _is_http_noise(_e):
                            entries.append(_e)
                            if len(entries) >= limit:
                                return
                    continue
                m2 = _re_simple.match(line)
                if m2:
                    lvl = m2.group(1).upper()
                    if lvl == "WARN":
                        lvl = "WARNING"
                    if lvl in allowed_levels:
                        _e = {"ts": "", "level": lvl, "component": "",
                            "request_id": "-", "message": m2.group(2)[:500]}
                        if _match_repo(_e) and _match_user(_e) and not _is_http_noise(_e):
                            entries.append(_e)
                        if len(entries) >= limit:
                            return
        except Exception:
            pass

    # 1. 新日志（JSON 格式）
    _parse_json_log(os.path.join(LOG_DIR, "devops-bot.log"))

    # 2. 旧日志（纯文本格式）+ 轮转文件
    old_log_dir = os.path.dirname(LOG_DIR)  # data/
    _parse_text_log(os.path.join(old_log_dir, "devops-bot.log"))
    for rotated in sorted(_glob.glob(os.path.join(old_log_dir, "devops-bot.log.*")), reverse=True):
        if len(entries) >= limit:
            break
        _parse_text_log(rotated)

    # 3. Worker 日志（data/logs/workers/*.log，JSON 格式，含 worker_id/worker_name）
    _worker_dir = os.path.join(LOG_DIR, "workers")
    for _wf in sorted(_glob.glob(os.path.join(_worker_dir, "*.log")), reverse=True):
        if len(entries) >= limit:
            break
        _parse_json_log(_wf)

    # 给主控日志加 source="主控"，worker 日志已有 worker_name
    for e in entries:
        if "source" not in e:
            e["source"] = e.get("worker_name") or "主控"

    # source 过滤
    if source:
        entries = [e for e in entries if e.get("source") == source or (source == "主控" and (e.get("source") == "主控"))]

    return {"entries": entries, "files": files_read, "count": len(entries)}


@router.get("/ops/timeline", operation_id="dashboard_ops_timeline")
async def get_ops_timeline(repo: str = "", user: dict = Depends(require_org_member)):
    """任务时序图数据：按 issue 分组的事件链（参考 admin RepoService.timeline）。

    需登录：组织成员可见。运维只读数据。
    返回 {repoKey, issues: [{issueId, title, eventCount, firstAt, lastAt, events: [...]}]}
    """
    from src.session_manager import db, db_lock
    import json as _json

    repo_key = ""
    _repo_keys = []
    if repo:
        _base = repo.split(":", 1)[1] if ":" in repo else repo
        _repo_keys = [f"gitcode:{_base}", f"github:{_base}"]
        repo_key = _repo_keys[0]

    with db_lock:
        c = db.cursor()
        if _repo_keys:
            c.execute(
                "SELECT seq, event_type, issue_id, title, actor, agent_type, skill, status, duration, created_at, payload, repo_key "
                "FROM events WHERE repo_key IN (" + ",".join("?" * len(_repo_keys)) + ") AND issue_id IS NOT NULL AND issue_id <> '' ORDER BY created_at",
                _repo_keys
            )
        else:
            c.execute(
                "SELECT seq, event_type, issue_id, title, actor, agent_type, skill, status, duration, created_at, payload, repo_key "
                "FROM events WHERE issue_id IS NOT NULL AND issue_id <> '' ORDER BY created_at"
            )
        rows = c.fetchall()

        # 查 agent_tasks 建立 (issue_id, event_seq) → (task_id, runtime_id) 映射
        c.execute(
            "SELECT issue_id, event_seq, id, runtime_id FROM agent_tasks "
            "WHERE issue_id IS NOT NULL AND issue_id <> ''"
        )
        task_map = {}
        runtime_map = {}
        for row in c.fetchall():
            task_map[(row[0], row[1])] = row[2]
            runtime_map[(row[0], row[1])] = row[3] or 0

        # 查 runtime_id → 引擎实例名映射（type-device_name 格式）
        c.execute(
            "SELECT ar.id, ar.name, ar.credential_id, rc.device_name, ar.host_name "
            "FROM agent_runtimes ar LEFT JOIN runtime_credentials rc ON ar.credential_id = rc.id"
        )
        from src.runtime.instances import instance_name as _inst_name
        import socket as _sock
        _local_hostname = ""
        try:
            _local_hostname = _sock.gethostname()
        except Exception:
            pass
        rt_name_map = {}
        for r in c.fetchall():
            _rid, _name, _cid, _dev, _host = r
            # 本机引擎（cid=0）：优先用 host_name，其次用本机 hostname
            if (_cid or 0) == 0:
                _dev = _host or _local_hostname or ""
            rt_name_map[_rid] = _inst_name(_name, _dev, _cid)

    # 按 issue_id 分组（同时记录每个 issue 的 repo_key）
    group = {}
    issue_repo = {}
    for row in rows:
        seq, event_type, issue_id, title, actor, agent_type, skill, status, duration, created_at, payload, ev_repo_key = row
        if issue_id not in group:
            group[issue_id] = []
            if ev_repo_key:
                issue_repo[issue_id] = ev_repo_key
        _rt_id = runtime_map.get((issue_id, seq), 0)
        group[issue_id].append({
            "seq": seq, "eventType": event_type, "agentType": agent_type or "",
            "skill": skill or "", "status": status or "",
            "duration": duration or 0, "createdAt": normalize_event_time(created_at),
            "actor": actor or "", "title": title or "",
            "taskId": task_map.get((issue_id, seq), ""),
            "engine": rt_name_map.get(_rt_id, "") if _rt_id else "",
            "_payload": payload or "",
        })

    issues = []
    for issue_id, evs in group.items():
        # 从 payload 提取 issue 标题
        issue_title = ""
        for ev in evs:
            if ev["_payload"]:
                try:
                    attrs = _json.loads(ev["_payload"])
                    oa = attrs.get("object_attributes", {})
                    issue_title = oa.get("title", "")
                    if issue_title:
                        break
                except Exception:
                    pass
        # 清理 _payload 字段
        for ev in evs:
            del ev["_payload"]
        issues.append({
            "issueId": issue_id, "title": issue_title,
            "repoKey": issue_repo.get(issue_id, repo_key),
            "eventCount": len(evs),
            "firstAt": evs[0]["createdAt"] if evs else "",
            "lastAt": evs[-1]["createdAt"] if evs else "",
            "events": evs,
        })

    return {"repoKey": repo_key, "issues": issues}


@router.get("/ops/a2a-health", operation_id="dashboard_ops_a2a_health")
async def get_a2a_health(user: dict = Depends(require_org_member)):
    """A2A 健康检查：server_base_url 可达性 + 最近 A2A 调用记录。

    需登录：组织成员可见。运维诊断数据。
    """
    import requests as _requests
    from src.user.db import get_server_base_url
    from src.session_manager import db, db_lock

    base_url = get_server_base_url()
    result = {
        "server_base_url": base_url,
        "reachable": False,
        "health_status": None,
        "latency_ms": None,
        "error": None,
        "recent_a2a": [],
    }

    # 检查可达性
    try:
        import time as _time
        t0 = _time.time()
        r = _requests.get(base_url.rstrip("/") + "/health", timeout=5)
        result["latency_ms"] = round((_time.time() - t0) * 1000)
        result["reachable"] = r.status_code == 200
        result["health_status"] = r.status_code
    except Exception as e:
        result["error"] = str(e)[:200]

    # 最近 A2A 调用记录（从 events 表查 agent_type=A2A 的最近记录）
    try:
        with db_lock:
            c = db.cursor()
            c.execute(
                "SELECT seq, event_type, repo_key, issue_id, status, created_at, payload "
                "FROM events WHERE event_type LIKE '%a2a%' OR event_type LIKE '%A2A%' "
                "ORDER BY created_at DESC LIMIT 10"
            )
            for row in c.fetchall():
                result["recent_a2a"].append({
                    "seq": row[0], "event_type": row[1] or "", "repo_key": row[2] or "",
                    "issue_id": row[3] or "", "status": row[4] or "",
                    "created_at": normalize_event_time(row[5]),
                })
    except Exception:
        pass

    return result


def _check_host_status(owner: str, repo: str, repo_key: str) -> str:
    """方案 X（F-2 落地）：纯本地判定托管状态，零 GitCode API 调用。

    - webhook 检测：读本地 managed_repos.webhook_id，>0 即视为存在（不远程比对 URL，杜绝 URL 漂移误报）。
    - bot 检测：读本地已落库的 host_status；仅当原值为 bot_missing/partial 且 bot 实际应存在时
      维持原值（不额外远程查权限，避免限流）。
    - 不调用任何远程 API，避免 429；真实"webhook 被手动删除"的漏报由 repair-host 手动确认兜底。
    """
    from src.config import log as _log
    try:
        from src.user.db import get_host_status, set_host_status, _get_managed_webhook_id
        webhook_id = _get_managed_webhook_id(repo_key)
        cur = get_host_status(repo_key)
        if not webhook_id:
            # webhook 从未建立 → webhook_missing
            if cur != "webhook_missing":
                set_host_status(repo_key, "webhook_missing")
            return "webhook_missing"
        # webhook 存在：
        if cur in ("bot_missing", "partial"):
            # bot 缺失/部分是托管时落库的明确标记，保留（不远程复查）
            return cur
        # 正常（含曾误标 webhook_missing → 现在 webhook_id>0 存在 → 清掉误报）
        if cur != "":
            set_host_status(repo_key, "")
        return ""
    except Exception as e:
        _log.warning(f"Dashboard: host status check failed: {e}")
        return ""


@router.get("/{owner}/{repo}", operation_id="dashboard_repo", summary="Repository dashboard", description="Repository-scoped dashboard panel data for the current user.")
def get_dashboard(owner: str, repo: str, user: dict = Depends(require_org_member)):
    from src.repo_key import to_repo_key
    repo_key = to_repo_key(f"{owner}/{repo}")

    # 权限校验：用户必须有权访问该仓库
    from src.api.deps import check_repo_access
    check_repo_access(user, repo_key)

    # 跨平台 repo_key 候选：DB 中可能是 gitcode: 或 github: 前缀
    _owner_repo = f"{owner}/{repo}"
    _repo_keys = [f"gitcode:{_owner_repo}", f"github:{_owner_repo}"]
    _rk_placeholders = ",".join("?" * len(_repo_keys))

    from src.session_manager import db, db_lock
    with db_lock:
        c = db.cursor()

        recent_events = []
        try:
            c.execute(
                "SELECT seq, event_type, agent_type, title, summary, status, created_at FROM events WHERE repo_key IN (?,?) ORDER BY seq DESC LIMIT 5",
                (_repo_keys)
            )
            recent_events = [
                {"seq": r[0], "event_type": r[1], "agent_type": r[2] or "", "title": r[3] or r[1],
                 "summary": r[4] or r[3] or r[1], "status": r[5], "created_at": normalize_event_time(r[6])}
                for r in c.fetchall()
            ]
        except Exception:
            pass

        agent_sessions = []
        try:
            c.execute(
                "SELECT s.agent_type, r.name, s.status, s.last_active_at, s.enable, COALESCE(rc.status,'') "
                "FROM devops_agent_session s LEFT JOIN agent_runtimes r ON s.runtime_id=r.id "
                "LEFT JOIN runtime_credentials rc ON rc.id=r.credential_id "
                "WHERE s.repo_key IN (?,?) ORDER BY s.agent_type",
                (_repo_keys)
            )
            agent_sessions = []
            for r in c.fetchall():
                _backend = r[1] or ""
                # 凭据已吊销的引擎视为不可用（revoke 后旧行保留但实际无法执行）：
                # 本机直连（rc.status=''，无凭据行）与 active 正常显示，revoked 置空走前端红字
                if _backend and r[5] not in ("", "active"):
                    _backend = ""
                agent_sessions.append({
                    "agent_type": r[0], "backend": _backend, "status": r[2], "last_active_at": normalize_event_time(r[3]),
                    "enable": bool(r[4]) if r[4] is not None else True})
        except Exception:
            pass

        # 每个 Agent 的最近 2 个事件（供 Dashboard Agent 卡片展示，替代假数据）
        agent_recent_events = {}
        try:
            for atype in ["vod", "issue", "coding", "qa", "sre"]:
                c.execute(
                    "SELECT seq, event_type, agent_type, title, summary, status, created_at FROM events WHERE repo_key IN (?,?) AND agent_type=? ORDER BY seq DESC LIMIT 2",
                    (_repo_keys + [atype])
                )
                agent_recent_events[atype] = [
                    {"seq": r[0], "event_type": r[1], "agent_type": r[2] or "", "title": r[3] or r[1],
                     "summary": r[4] or r[3] or r[1], "status": r[5], "created_at": normalize_event_time(r[6])}
                    for r in c.fetchall()
                ]
        except Exception:
            pass

    agent_types_present = {a["agent_type"] for a in agent_sessions}
    all_types = ["vod", "issue", "coding", "qa", "sre"]
    for atype in all_types:
        if atype not in agent_types_present:
            from src.api.session_store import get_agent_type_enable
            agent_sessions.append({
                "agent_type": atype,
                "backend": "",  # 无绑定/未建会话：不写硬编码默认（engine-selection-design.md v2）
                "status": "",
                "last_active_at": "",
                "enable": bool(get_agent_type_enable(atype)),
            })

    # 计算每个 Agent 的事件统计
    agent_event_counts = {}
    for ev in recent_events:
        at = ev["agent_type"]
        if at:
            agent_event_counts[at] = agent_event_counts.get(at, 0) + 1

    for atype in all_types:
        if atype not in agent_event_counts:
            agent_event_counts[atype] = 0

    # 获取用户角色
    role = "Member"
    try:
        repos = user_db.get_managed_repos(user["id"])
        for r in repos:
            if r["repo_owner"] == owner and r["repo_name"] == repo:
                role = "Maintainer" if r.get("is_admin") else "Member"
                break
    except Exception:
        pass

    event_counts = {"pending": 0, "running": 0, "completed": 0, "failed": 0}
    for ev in recent_events:
        s = ev["status"]
        if s in event_counts:
            event_counts[s] += 1

    # dev_scene 已退役，始终为空（前端自动选第一个场景展示）
    dev_scene = ""

    scenes = []
    scene_nodes = []
    # 查 DB 中实际使用的 repo_key（可能是 github: 前缀）
    _actual_repo_key = repo_key
    try:
        with db_lock:
            c.execute("SELECT repo_key FROM managed_repos WHERE repo_key IN (" + _rk_placeholders + ") LIMIT 1", _repo_keys)
            _row = c.fetchone()
            if _row:
                _actual_repo_key = _row[0]
    except Exception:
        pass
    try:
        from src.api.pipeline_config import get_scenes, get_pipeline_config
        scenes = get_scenes(repo_key=_actual_repo_key)
        # dev_scene 必须在可见场景列表中才加载，否则自动选中第一个标准场景
        # PR 订阅场景不作为 Dashboard 默认选中
        scene_ids = {s["id"] for s in scenes}
        if scenes:
            if not dev_scene or dev_scene not in scene_ids:
                dev_scene = scenes[0]["id"]
            cfg = get_pipeline_config(dev_scene, repo_key=_actual_repo_key)
            if cfg:
                scene_nodes = cfg.get("nodes", [])
        else:
            dev_scene = ""
    except Exception as e:
        log.warning(f"Dashboard: scene load failed: {e}")

    # 为每个 agent 附带场景信息（skill / enabled_in_scene / engine_runtime_ids）
    # engine_runtime_ids 只从场景 node 读取（为空 = 全广播/兜底）
    scene_by_agent = {n.get("tag") or n.get("id", ""): n for n in scene_nodes}
    # Issue #980：显示名解析统一走共用 skill_display 层（skill_id 优先 →
    # (scope, skill_name) → user/repo 上下文 → official 兜底），三档同名技能
    # 不再被 official 映射一律覆盖成官方名。
    from src.api.skill_display import build_skill_display_map, resolve_node_skill_display
    _uid_ctx = user.get("id", "")
    skill_display_map = build_skill_display_map(repo_key=repo_key, user_id=_uid_ctx)
    for ag in agent_sessions:
        at = ag.get("agent_type", "")
        node = scene_by_agent.get(at)
        if node:
            _sk = node.get("skill", "")
            ag["skill"] = _sk
            if _sk:
                _sid = node.get("skill_id") or 0
                ag["skill_display_name"] = resolve_node_skill_display(
                    _sk, _sid, skill_display_map, repo_key=repo_key, user_id=_uid_ctx)
            else:
                ag["skill_display_name"] = ""
            ag["enabled_in_scene"] = node.get("enabled", True)
            ag["engine_runtime_ids"] = node.get("engine_runtime_ids", []) or []
        else:
            ag["skill"] = ""
            ag["skill_display_name"] = ""
            ag["enabled_in_scene"] = False
            ag["engine_runtime_ids"] = []

    # 获取已注册引擎列表（供前端 engine 下拉）
    # 按 agent_type 标签过滤：只返回 tags 包含该 agent_type 或 "all" 的引擎
    # 返回全部引擎（含 offline/about_to_gc），前端通过状态徽章区分
    # 组织隔离：非个人组织只返回 org_id 匹配的引擎
    from src.user.org_db import is_org_personal
    _org_id = user.get("org_id", 0)
    _is_personal_org = is_org_personal(_org_id) if _org_id else True
    _filter_org_id = 0 if _is_personal_org else _org_id
    agent_engines = {}
    try:
        from src.runtime.engine_select import query_engines
        from src.runtime.instances import instance_name, to_display
        _uname = user.get("username") or ""

        # Issue #1310：凭据名缓存（runtime_credentials.name），供 agent_engines 下拉展示
        _cred_name_cache = {}
        def _cred_name(_cid):
            if not _cid:
                return ""
            if _cid in _cred_name_cache:
                return _cred_name_cache[_cid]
            _n = ""
            try:
                with db_lock:
                    c = db.cursor()
                    c.execute("SELECT name FROM runtime_credentials WHERE id=?", (_cid,))
                    _r = c.fetchone()
                    _n = (_r[0] or "") if _r else ""
            except Exception:
                _n = ""
            _cred_name_cache[_cid] = _n
            return _n
        for atype in all_types:
            # SRE 专属（sre-repo-worker-relax §1.3）：user + repo 两档
            # （global 池不放行：SRE 执行部署/运维命令，广播责任边界不清）
            if atype == "sre":
                engines = (query_engines(repo_key, "user", agent_type="sre", username=_uname)
                           + query_engines(repo_key, "repo", agent_type="sre"))
            else:
                engines = (query_engines(repo_key, "global", agent_type=atype)
                           + query_engines(repo_key, "user", agent_type=atype, username=_uname)
                           + query_engines(repo_key, "repo", agent_type=atype)
                           + query_engines(repo_key, "local", agent_type=atype, username=_uname))
            _scope_labels = {"global": "全局", "user": "个人", "repo": "仓库", "local": "本机"}
            from src.runtime.health import derive_runtime_health, derive_local_runtime_health
            import time as _time
            _now = _time.time()
            _seen_ids = set()
            _deduped = []
            for e in engines:
                _rid = e["runtime_id"]
                if _rid in _seen_ids:
                    continue
                _seen_ids.add(_rid)
                _deduped.append(e)
            # 组织隔离过滤
            if _filter_org_id:
                _deduped = [e for e in _deduped if (e.get("org_id") or 0) == _filter_org_id]
            agent_engines[atype] = [{"id": e["runtime_id"],
                                     "name": instance_name(e["name"], e.get("device_name", ""), e.get("credential_id", 0)),
                                     # Issue #1310：引擎显示名统一「引擎名（凭据名）」——
                                     # engine_name 为引擎类型名（name 仍是 instance_name 标识）；
                                     # worker_name 为凭据名（runtime_credentials.name），本机直连回退 host_name
                                     "engine_name": to_display(e.get("name", "")),
                                     "worker_name": ((e.get("host_name") or "") if (e.get("credential_id") or 0) == 0
                                                     else _cred_name(e.get("credential_id") or 0)),
                                     "device": e.get("device_name", ""), "cred_name": e.get("owner_name", ""),
                                     # Issue #592：本机直连（credential_id=0）不参与 worker 心跳，
                                     # last_seen_at 仅由 10min 周期重探刷新——统一走远端心跳口径
                                     # 会把探活间隔内的本机引擎误判 recently_lost（全显失联）。
                                     # 本机直连按探活结果派生（与引擎页 _engine_health 口径一致）。
                                     "status": (derive_local_runtime_health(bool(e.get("installed")), bool(e.get("healthy")))
                                                if (e.get("credential_id") or 0) == 0
                                                else derive_runtime_health(e["status"], e.get("last_seen_at"), _now)),
                                     "tags": e.get("tags", []),
                                     "scene_tags": e.get("scene_tags", ["default"]),
                                     "scope": e.get("scope", "global"),
                                     "scope_label": _scope_labels.get(e.get("scope", "global"), ""),
                                     "alias": e.get("alias", "")}
                                    for e in _deduped]
    except Exception as e:
        log.warning(f"dashboard agent_engines failed: {e}")

    # 真实指标统计（替代假数据）
    metrics = {"total_events": len(recent_events), "active_agents": 0,
               "pending_events": event_counts["pending"], "failed_events": event_counts["failed"],
               "auto_resolution": 0.0, "events_week": 0, "avg_response": 0}
    try:
        with db_lock:
            # total_issues: 统计不同 issue_id 的数量（去重）
            c.execute(
                "SELECT COUNT(DISTINCT issue_id) FROM events WHERE repo_key IN (?,?) AND issue_id!='' AND issue_id IS NOT NULL",
                (_repo_keys))
            metrics["total_issues"] = c.fetchone()[0] or 0
            # Auto-Resolution: completed / (completed + failed)
            c.execute(
                "SELECT COUNT(*) FROM events WHERE repo_key IN (?,?) AND status='completed'",
                (_repo_keys)
            )
            completed = c.fetchone()[0] or 0
            c.execute(
                "SELECT COUNT(*) FROM events WHERE repo_key IN (?,?) AND status='failed'",
                (_repo_keys)
            )
            failed = c.fetchone()[0] or 0
            total_done = completed + failed
            metrics["auto_resolution"] = round(completed / total_done * 100, 1) if total_done > 0 else 0.0

            # Events/Week: 最近 7 天事件数
            # N1c 修复：原 datetime('now', '-7 days') 是 SQLite 私有函数变体（MySQL
            # 1064），改应用层预计算 cutoff + 标准参数化 SQL，双方言一致。
            # Issue #1487 衍生修复：cutoff 必须走 db_now_str（存储层口径：MySQL=CST 墙钟 /
            # SQLite=UTC），原先用 gmtime 的 UTC 字面量在 MySQL 下会多算 8 小时窗口。
            from src.api.time_utils import db_now_str as _db_now_str
            _week_cutoff = _db_now_str(-7 * 86400)
            c.execute(
                "SELECT COUNT(*) FROM events WHERE repo_key IN (?,?) AND created_at >= ?",
                (_repo_keys[0], _repo_keys[1], _week_cutoff)
            )
            metrics["events_week"] = c.fetchone()[0] or 0

            # Avg Response: 已完成事件的平均 duration（秒）
            c.execute(
                "SELECT AVG(duration) FROM events WHERE repo_key IN (?,?) AND status='completed' AND duration IS NOT NULL AND duration > 0",
                (_repo_keys)
            )
            avg_dur = c.fetchone()[0]
            metrics["avg_response"] = round(avg_dur, 1) if avg_dur else 0
    except Exception:
        pass

    metrics["active_agents"] = sum(1 for a in agent_sessions if a["status"] == "running")

    # 方案 F-2：实时复查托管状态（bot 是否维护者 + webhook 是否存在），更新 host_status
    host_status = _check_host_status(owner, repo, _actual_repo_key)

    # 读取当前场景的高级选项（工作分支 + 自定义 key:value）
    advanced_options = {}
    try:
        if dev_scene:
            with db_lock:
                c.execute(
                    "SELECT advanced_options FROM devops_pipeline_scene "
                    "WHERE scene_id=? AND repo_key IN (?,?) AND is_active=1 LIMIT 1",
                    ([dev_scene] + _repo_keys))
                _adv_row = c.fetchone()
            if _adv_row and _adv_row[0]:
                import json as _json
                advanced_options = _json.loads(_adv_row[0])
    except Exception:
        pass

    return {
        "repo": _actual_repo_key,
        "role": role,
        "dev_scene": dev_scene,
        "advanced_options": advanced_options,
        "_scenes": scenes,
        "metrics": metrics,
        "agents": agent_sessions,
        "agent_engines": agent_engines,
        "scene_nodes": scene_nodes,
        "host_status": host_status,
    }
