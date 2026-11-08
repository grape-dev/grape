"""健康态派生（移植 multica derive-health，见 agent-runtime-design.md §7.5 / concurrency 无需）。

规则：status=online 且心跳新鲜(< STALE_SECONDS，默认 100s) → online；status=online 但心跳超时 → recently_lost（失联）；
无 last_seen → offline；距 GC（7d）<1d → about_to_gc；其余 offline。

修复记录：原实现 status='online' 直接返回 online，不校验 last_seen 新鲜度 ——
worker 宕机后引擎永久显示"在线"，界面/调度展示误导（2026-08-12 修复）。
Issue #259：新鲜度阈值与 engine_select._RUNTIME_STALE_SECONDS 对齐（env RUNTIME_STALE_SECONDS，默认 100s），
原 5min 写死导致展示层与调度层判定不一致。
"""
import os
from datetime import datetime, timedelta, timezone

_STALE_SECONDS = int(os.environ.get("RUNTIME_STALE_SECONDS", "100"))


def apply_runtime_state(c, runtime_id: int, status: str, last_seen_at: str) -> None:
    """Issue #1496：agent_runtimes 在线态统一写入口（status 唯一真源）。

    register/heartbeat/probe/本机直连刷新/僵尸回收等所有写 status 的路径一律走本函数：
    同一条 UPDATE 内写 status + last_seen_at（+遗留保留列 health 的派生同步值），
    消除「先写 A 后写 B」的二次写窗口期——库内任意时刻不再出现
    health='online' AND status<>'online' 的自相矛盾行。

    裁定：status 是唯一真源（online/idle/offline）；health 仅作派生展示值。
    派生口径按引擎类型分流（Issue #592 / #1496 返工）：
      · 远端（credential_id>0）→ derive_runtime_health(status, last_seen_at)
      · 本机直连（credential_id=0）→ derive_local_runtime_health(installed, healthy)
        （本机不参与 15s 心跳，只有 10min 探活，套心跳阈值会把健康引擎刷成 recently_lost）
    best-effort：未启用遗留列的库该列写入被 except 吞掉，不影响 status/last_seen_at 主语义。
    调用方必须在持有 db_lock 的事务内传入 cursor c，由调用方统一 commit。
    """
    _now = datetime.now(timezone.utc).timestamp()
    # Issue #1496 返工（2026-11-08）：本机直连（credential_id=0）的派生 health 必须走
    # Issue #592 口径 derive_local_runtime_health(installed, healthy)，不能用远端心跳
    # 口径 derive_runtime_health(status, last_seen)——本机引擎只由 10min 探活刷新
    # last_seen_at，套远端 100s 新鲜度会把健康的本机引擎刷成 recently_lost。
    _derived = None
    try:
        c.execute("SELECT credential_id, installed, healthy FROM agent_runtimes WHERE id=?",
                  (runtime_id,))
        _row = c.fetchone()
    except Exception:
        _row = None
    if _row and not (_row[0] or 0):
        _derived = derive_local_runtime_health(
            bool(_row[1] if _row[1] is not None else 1), bool(_row[2]))
    else:
        _derived = derive_runtime_health(status, last_seen_at, _now)
    try:
        c.execute(
            "UPDATE agent_runtimes SET status=?, last_seen_at=?, health=? WHERE id=?",
            (status, last_seen_at, _derived, runtime_id))
    except Exception:
        # 库无遗留 health 列（当前 DDL 口径）→ 只写 status+last_seen_at 主语义。
        # 注意：异常可能发生在列缺失之外的场景（表锁等），此时第二条 UPDATE 同样
        # 会失败并由调用方的事务回滚兜底，不会出现半写状态。
        c.execute(
            "UPDATE agent_runtimes SET status=?, last_seen_at=? WHERE id=?",
            (status, last_seen_at, runtime_id))


def parse_dt(s: str) -> float | None:
    if not s:
        return None
    try:
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            # 无时区值语义与存储层归一口径一致（Issue #512）：
            # mysql=naive 视为 CST(+08:00) 本地墙钟 → 转 UTC（防 MySQL 直连链路历史
            # CST 值按 UTC 解析被误判为"未来时间"）；sqlite=naive 视为 UTC（行为不变）
            from src.api.time_utils import _db_backend
            if _db_backend() == "mysql":
                dt = dt.replace(tzinfo=timezone(timedelta(hours=8), "CST"))
            else:
                dt = dt.replace(tzinfo=timezone.utc)
        return dt.timestamp()
    except Exception:
        return None


def derive_runtime_health(status: str, last_seen_at: str | None, now: float | None = None) -> str:
    """唯一派生入口（Issue #1496 裁定）：status 是 agent_runtimes 在线态的唯一真源，
    本函数是把 (status, last_seen_at) 派生为展示值的唯一口径 —— dashboard 列表、
    scene engine-status、settings 设备视图等读路径一律复用本函数，禁止直读库内
    遗留 health 列（该列不参与任何派发/展示判定，仅由 apply_runtime_state 同事务
    回写派生值、迁移按本口径回填，登记待删除）。"""
    _now = now if now is not None else datetime.now(timezone.utc).timestamp()
    ts = parse_dt(last_seen_at)
    # "active" 是 runtime_credentials 表的状态（等价于 online）
    if status in ("online", "active"):
        if ts is None:
            return "recently_lost"      # 标 online 但无心跳记录 → 视为失联
        if _now - ts < _STALE_SECONDS:
            return "online"             # 心跳新鲜 → 真在线
        return "recently_lost"          # 心跳超时 → 失联（原实现直接返回 online 的缺陷）
    if ts is None:
        return "offline"
    offline_for = _now - ts
    if offline_for < _STALE_SECONDS:
        return "recently_lost"
    if offline_for > 6 * 24 * 3600:
        return "about_to_gc"
    return "offline"


def derive_local_runtime_health(installed: bool, healthy: bool) -> str:
    """本机直连引擎（credential_id=0）健康态：按探活结果（installed/healthy）派生。

    本机直连引擎不参与远端 worker 心跳（15s），仅由主控机周期重探
    （main.py _local_probe_loop，默认 10min，local-direct-health-probe-design）刷新
    last_seen_at/healthy——若误用 derive_runtime_health 的心跳新鲜度阈值
    （_STALE_SECONDS=100s）判定，探活间隔内会被误判为 recently_lost（失联）。
    Issue #592：引擎选择弹窗全显失联的根因；与 settings._engine_health 口径统一。
    """
    return "online" if (installed and healthy) else "offline"


def engine_health_ok(credential_id, status, healthy, installed, last_seen_at,
                     stale_seconds: int | None = None) -> bool:
    """引擎是否「健康可达」的唯一判定真源（Issue #592 / #259 / #1487）。

    派发侧（engine_select._health_ok）与认领侧（service._check_pool_engines_reachable）
    必须调用本函数，禁止各自复制一份判定——#1487 的根因就是两侧各拿一把尺子：
    派发说健康、认领说失联，任务入队即被判死。

    - 本机直连（credential_id=0）：installed + healthy，**不判**心跳新鲜度，
      **也不判** status（status 在本机行上只是 probe 循环的副产物，真值已由
      installed/healthy 表达；#592 约定本机不参与远端心跳）。
    - 远端（credential_id>0）：status ∈ {online, active} + healthy==1 + last_seen_at 在
      stale_seconds（默认 RUNTIME_STALE_SECONDS=100s）内；无心跳记录视为失联。

    说明：probe_status 不在本函数口径内——认领侧对远端仍额外校验 probe
    （'ok'/'pending'/'' 之外判不可达），比派发侧更严，属既有差异（#1487 未改）。
    """
    _stale = stale_seconds if stale_seconds is not None else _STALE_SECONDS
    if not (credential_id or 0):
        _installed = installed if installed is not None else 1
        return derive_local_runtime_health(bool(_installed), bool(healthy)) == "online"
    if (status or "") not in ("online", "active"):
        return False
    if (healthy if healthy is not None else 0) != 1:
        return False          # 探活失败（healthy=0）的远端引擎不可派发/不可认领
    _now = datetime.now(timezone.utc).timestamp()
    _ts = parse_dt(last_seen_at or "")
    if _ts is None:
        return False          # 标 online 但无心跳记录 → 失联（与 derive_runtime_health 一致）
    return _now - _ts <= _stale


def check_skill_consistency(bound_skill: str, actual_skill: str,
                            declared: bool = False) -> dict:
    """Issue #887：skill 一致性校验项（健康检查口径，服务端/worker 共用，零依赖）。

    规则（对齐 Issue #887 Spec：fail-fast，仅绑定非空且不一致时拒绝）：
    - bound 为空 → ok（引擎本地缓存仅绑定为空时兜底，source=unbound）；
    - bound 非空且 actual == bound → ok；
    - bound 非空且 actual != bound：
      - declared=False（未声明的偏离，如引擎本地缓存决定执行技能）→ NOT ok（拒绝执行）；
      - declared=True（服务端明示的回退，如 #617 官方检查器 skill_fallback）→ ok，
        但 mismatch=True 显式标注（可观测，不静默）。

    返回 {"name": "skill_consistency", "ok": bool, "mismatch": bool, "bound": str,
          "actual": str, "detail": str}。
    """
    bound = (bound_skill or "").strip()
    actual = (actual_skill or "").strip()
    if not bound:
        return {"name": "skill_consistency", "ok": True, "mismatch": False,
                "bound": "", "actual": actual,
                "detail": "no bound skill; engine local cache may apply as fallback only"}
    if actual == bound:
        return {"name": "skill_consistency", "ok": True, "mismatch": False,
                "bound": bound, "actual": actual,
                "detail": "actual skill matches agent binding"}
    if declared:
        return {"name": "skill_consistency", "ok": True, "mismatch": True,
                "bound": bound, "actual": actual,
                "detail": (f"declared fallback: bound '{bound}' resolved to declared "
                           f"skill '{actual}' (server-sanctioned, Issue #617)")}
    return {"name": "skill_consistency", "ok": False, "mismatch": True,
            "bound": bound, "actual": actual,
            "detail": (f"skill mismatch: bound '{bound}' but about to execute "
                       f"'{actual}'; refusing execution (Issue #887)")}


def aggregate_skill_consistency(days: int = 7) -> dict:
    """Issue #887（QA AC4）：skill 一致性健康检查汇总项。

    统计近 N 天因 [skill_mismatch] 被拒绝执行的任务数（agent_tasks.result_text
    前缀），供 /readyz 与 /api/health 端点消费——校验逻辑本身已在执行链路
    fail-fast（verify_skill_execution_consistency），此处是聚合可观测口径：
    出现拒绝记录说明门禁真实拦截过绑定漂移（不静默），持续新增则提示环境
    存在残留缓存/绑定配置问题需排查。

    返回 {"name": "skill_consistency", "status": "ok"|"warn",
          "refused_recent": int, "window_days": int, "detail": str}。
    查询失败降级 status=unknown，绝不阻断健康端点。
    """
    out = {"name": "skill_consistency", "status": "unknown", "refused_recent": 0,
           "window_days": int(days), "detail": ""}
    try:
        from datetime import timedelta
        from src.session_manager import db, db_lock
        _since = (datetime.now(timezone.utc) - timedelta(days=int(days))).isoformat()
        with db_lock:
            c = db.cursor()
            c.execute(
                "SELECT COUNT(*) FROM agent_tasks "
                "WHERE status='failed' AND result_text LIKE '[skill_mismatch]%' "
                "AND completed_at >= ?", (_since,))
            n = int(c.fetchone()[0] or 0)
        out["refused_recent"] = n
        if n:
            out["status"] = "warn"
            out["detail"] = (f"{n} task(s) refused by skill consistency gate in last "
                             f"{int(days)}d (bound-skill drift intercepted; check "
                             f"workspace/engine skill cache residue)")
        else:
            out["status"] = "ok"
            out["detail"] = (f"no skill-mismatch refusals in last {int(days)}d "
                             f"(Issue #887 gate active)")
        return out
    except Exception as e:
        out["detail"] = f"skill consistency aggregate failed: {e}"
        return out
