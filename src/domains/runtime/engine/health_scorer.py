"""机器健康度评估模块（Issue #134 / #133 / #135 Spec）。

基于任务中心 DB 的历史执行记录，按滑动窗口（近 7 天）计算 per-machine：
- 总任务数、失败数、失败率
- 连续失败次数（consecutive_failures）
- 健康分（0-100，越高越健康）

健康分 = round(100 * (1 - failure_rate * 0.5)) - consecutive_failure_penalty
  - failure_rate = failed / total（total=0 且心跳正常时返回满分 100）
    （失败率权重 0.5：给引擎试错空间）
  - consecutive_failure_penalty = consecutive_failures * 15（每次扣 15，不设上限，
    小于 0 时整体 clamp 为 0；连挂 3 次即到 30 分临界、5 次归 0）
  - 最终 clamp 到 [0, 100]

Issue #135 心跳因素（优先于失败率计算）：
  - 引擎最近无心跳（agent_runtimes.last_seen_at 超时或为 NULL）→ 健康分 = 0
  - 心跳正常 → 按原失败率公式计算
  - 心跳阈值 _HEARTBEAT_STALE_SECONDS（默认 300s，env HEALTH_HEARTBEAT_STALE_SECONDS 可调）

设计决策：
- DB 持久化（runtime_health_scores 表）+ 内存缓存（60s TTL）
- 派发链路作为增强层调用 get_health_scores / filter_healthy / best_fallback
- 不改变广播协议/任务分发语义/A2A 事件流转

及时性保障（Issue #85）：
- 即时登记：任务终态时 record_task_completion 立刻重新计算并写入 DB（不等定时刷新）
- 定时刷新：后台线程每 5 分钟对所有引擎 force_refresh，覆盖离线引擎无任务事件触发的场景
- 双重机制确保失联引擎的健康分在 5 分钟内从 100 降至 0
"""
import json
import os
import threading
import time
from datetime import datetime, timezone, timedelta
from decimal import Decimal

from src.kernel.config import log
from src.kernel.lock_instrument import lock_owner

# ===== 配置（可环境变量调优）=====

_WINDOW_DAYS = int(os.environ.get("HEALTH_WINDOW_DAYS", "7"))
_CACHE_TTL = int(os.environ.get("HEALTH_CACHE_TTL", "60"))
# 健康分硬阈值：低于此值的机器在派发候选筛选阶段被过滤
_HEALTH_THRESHOLD = int(os.environ.get("HEALTH_SCORE_THRESHOLD", "30"))
# 连续失败上限：超过此值直接视为不健康（无需等失败率累积）
_MAX_CONSECUTIVE_FAILURES = int(os.environ.get("HEALTH_MAX_CONSECUTIVE_FAILURES", "5"))
# 心跳超时阈值（秒）：last_seen_at 超过此值或为 NULL → 引擎失联，健康分强制 0。
# 与 engine_select._RUNTIME_STALE_SECONDS / health.py 5min 判定保持一致。
_HEARTBEAT_STALE_SECONDS = int(os.environ.get("HEALTH_HEARTBEAT_STALE_SECONDS", "100"))
# Issue #3969：显式「连续 ≥N 个心跳间隔才判离线」口径。worker 心跳默认 15s（env
# RUNTIME_HEARTBEAT_INTERVAL）；单次 502 会被客户端退避重试覆盖，不应判离线，只有
# 连续错过 N 个间隔才算失联。实际阈值 = max(静态阈值, interval × N)，默认
# max(100, 15×3)=100 保持现网行为不变。
_HEARTBEAT_INTERVAL_SECONDS = float(os.environ.get("RUNTIME_HEARTBEAT_INTERVAL", "15"))
_HEARTBEAT_MISSES_THRESHOLD = int(os.environ.get("HEALTH_HEARTBEAT_MISSES_THRESHOLD", "3"))


def _heartbeat_stale_threshold() -> int:
    """心跳失联阈值（秒）：`max(HEALTH_HEARTBEAT_STALE_SECONDS, interval × misses)`。

    每次调用读取 env，便于测试与按部署调参；默认 100s 与 engine_select / health.py
    的 `RUNTIME_STALE_SECONDS=100` 对齐。
    """
    try:
        stale = int(os.environ.get("HEALTH_HEARTBEAT_STALE_SECONDS", str(_HEARTBEAT_STALE_SECONDS)))
    except (TypeError, ValueError):
        stale = _HEARTBEAT_STALE_SECONDS
    try:
        interval = float(os.environ.get("RUNTIME_HEARTBEAT_INTERVAL", str(_HEARTBEAT_INTERVAL_SECONDS)))
    except (TypeError, ValueError):
        interval = _HEARTBEAT_INTERVAL_SECONDS
    try:
        misses = int(os.environ.get("HEALTH_HEARTBEAT_MISSES_THRESHOLD", str(_HEARTBEAT_MISSES_THRESHOLD)))
    except (TypeError, ValueError):
        misses = _HEARTBEAT_MISSES_THRESHOLD
    return int(max(stale, interval * max(1, misses)))
# 小样本保护阈值（issue #439）：窗口内任务总数 < N 时豁免连败惩罚、失败率按
# failed/N 稀释——低样本引擎单次失败不再打入拒派区（旧逻辑 total=1/failed=1
# → 0 分 → claim_by_tag 拒派死锁）。
_MIN_SAMPLES = int(os.environ.get("HEALTH_MIN_SAMPLES", "5"))
# 失败衰减窗口（小时，Issue #498）：窗口内无新失败时 consecutive_failures 清零、
# 失败不再计入健康分有效口径（failed_effective）——探活/心跳正常 + 无新失败 →
# 异常标记自动消除、健康分回升（自愈）。默认 72h，env HEALTH_FAILURE_DECAY_HOURS 可调。
_FAILURE_DECAY_HOURS = float(os.environ.get("HEALTH_FAILURE_DECAY_HOURS", "72"))

# ===== 内存缓存 =====

_cache: dict = {}  # {runtime_id: {"score": int, "data": dict, "expires": float}}
_cache_lock = threading.Lock()


def _db():
    """本表域（agent_runtimes / agent_tasks 归 runtime）：分库模式走 runtime owner 连接。"""
    from src.kernel.owner_db import owner_db
    return owner_db("runtime")


def _score_db():
    """健康分表 runtime_health_scores 归 insight 域。"""
    from src.kernel.owner_db import owner_db
    return owner_db("insight")


def _lock():
    from src.kernel.owner_db import owner_lock
    return owner_lock("runtime")


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _window_start() -> str:
    """滑动窗口起始时间（ISO 格式，近 N 天）。"""
    return (_utcnow() - timedelta(days=_WINDOW_DAYS)).isoformat()


def _decay_start() -> str:
    """Issue #498: 失败衰减窗口起点（T 格式纯 UTC 字面量，近 N 小时）。

    与 agent_tasks.completed_at（TEXT，Python isoformat 写入的 T 格式存量）
    同格式字符串比较，复用 _to_sql_utc 的口径（见其注释中的两次实测踩坑）。
    """
    return _to_sql_utc((_utcnow() - timedelta(hours=_FAILURE_DECAY_HOURS)).isoformat())


def _effective_window_start(runtime_id: int) -> str:
    """Issue-P2: 实际统计窗口起点 = max(健康分重置时间戳, 7天前)。

    管理员通过 health-reset 接口写入 agent_runtimes.health_reset_at 赦免点：
    - 重置时间戳晚于 7 天前 → 从重置点开始统计（重置前的 failed 被赦免）
    - 重置时间戳早于 7 天前（重置已滚出窗口）→ 回到普通 7 天窗口，赦免自动失效
    - 从未重置 → 普通 7 天窗口
    交集语义：两个区间取更晚的起点，无需任何清理逻辑。
    """
    window = _window_start()
    reset_at = ""
    try:
        with _lock():
            c = _db().cursor()
            c.execute("SELECT health_reset_at FROM agent_runtimes WHERE id=?",
                      (runtime_id,))
            row = c.fetchone()
            reset_at = (row[0] or "") if row else ""
    except Exception as e:
        log.warning(f"health_scorer: fetch health_reset_at failed for runtime={runtime_id}: {e}")
        return window
    if not reset_at:
        return window
    # 与 7 天窗口起点按「时间」比较（不再按字符串比较：精度/格式差异会误判，
    # 例如 '...:42+00:00' vs '...:42.916425+00:00' 的字典序会把同秒判反）
    try:
        ts = datetime.fromisoformat(reset_at)
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        win_dt = datetime.fromisoformat(window)
        if win_dt.tzinfo is None:
            win_dt = win_dt.replace(tzinfo=timezone.utc)
        return ts.isoformat() if ts > win_dt else window
    except Exception:
        return window


_SQL_DT_FMT = "%Y-%m-%dT%H:%M:%S"


def _to_sql_utc(ts) -> str:
    """把时间戳规范成与 agent_tasks 时间列存量一致的 T 分隔字面量（UTC、秒精度，
    'YYYY-MM-DDTHH:MM:SS'）。

    格式演进（两次实测踩坑，Issue #428）：
    1. 最初：直接传带时区偏移的 ISO 串（...T07:49:24+00:00）。MySQL 把该串按
       「会话时区」解释，实测被抬高 8 小时（CAST('2026-10-21T07:49:24+00:00' AS
       DATETIME) = 2026-10-21 15:49:25）→ 窗口起点落到未来，重置后 8 小时内的
       新失败全部落在窗口之外 → 赦免期退化成"免疫期"。2026-10-21 修复引入秒级
       纯 UTC 规范，方向正确。
    2. 但当时输出成了空格分隔（'YYYY-MM-DD HH:MM:SS'）。agent_tasks.created_at /
       completed_at 是 **TEXT 列**，由 Python `datetime.now(timezone.utc).isoformat()`
       写入，存量全是 T 分隔 ISO（'2026-10-22T05:52:42.090948+00:00'）。SQL
       `created_at >= ?` 是逐字节字符串比较：日期前 10 位相同时第 11 位
       T(0x54) > 空格(0x20) → **重置点同日的旧失败被误判入窗，赦免失效**
       （MySQL 实测 '2026-10-22T05:52:42' >= '2026-10-22 06:21:32' 为真）。
       首次重置生效（当时无同日旧失败）、二次失效的现象即由此而来。
    3. 本修复（Issue #428）：输出改为 T 分隔纯 UTC 'YYYY-MM-DDTHH:MM:SS'——
       与存量数据同格式，字符串比较/排序两侧自然对齐；无时区偏移后缀，不会再被
       MySQL 按会话时区抬高。治本方案（时间列迁移 DATETIME）另行评估。

    性能：只在每次统计前转换一次（两次查询共用一个常量参数），
    **不在 SQL 里对列做任何函数运算** → idx_tasks_runtime_status(runtime_id,status)
    照旧可用；快路径是纯字符串切片，无 datetime 解析开销。

    精度语义：窗口取秒精度；与重置点同一秒内产生的失败（存量带微秒/偏移后缀，
    前缀相同串更长 → 字典序更大）按"发生在重置之后"计入（保守方向——宁可计
    失败，也不制造免疫期）。
    """
    if isinstance(ts, datetime):
        dt = ts
    else:
        s = (ts or "").strip()
        if not s:
            return ""
        body = s
        if body.endswith("Z"):
            body = body[:-1]
        elif body.endswith(("+00:00", "-00:00")):
            body = body[:-6]
        # 快路径：'YYYY-MM-DDTHH:MM:SS' 或带小数秒（偏移已剥掉）→ 直接切片（保留 T）
        if len(body) >= 19 and body[10] == "T":
            tail = body[19:]
            if tail == "" or tail.startswith("."):
                return body[:19]
        dt = datetime.fromisoformat(s)
    if dt.tzinfo is not None:
        dt = dt.astimezone(timezone.utc)
    return dt.strftime(_SQL_DT_FMT)


# ===== 核心计算 =====

def _consecutive_failure_penalty(consecutive_failures: "int | Decimal | None") -> int:
    """连续失败惩罚（2026-10-23 试错友好口径）：每次 -15，不设上限（由整体 clamp 到 0 兜底）。

    配合失败率权重 0.5（基础分最高 75）：连挂 3 次即到 30 分临界，
    连挂 5 次归 0；失败率越低的引擎基础分越高，能扛更多次连续失败。

    入口归一化（2026-10-23 Decimal 崩溃修复）：MySQL 后端 SUM()/COUNT(*) 返回
    decimal.Decimal，先 int(or 0) 归一，None 语义保留为 0。
    """
    n = int(consecutive_failures or 0)
    if n <= 0:
        return 0
    return n * 15


def _compute_health_score(total: "int | Decimal | None", failed: "int | Decimal | None",
                          consecutive_failures: "int | Decimal | None") -> int:
    """根据任务统计计算健康分（0-100）。

    - total=0 → 100（无历史数据，默认健康）
    - failure_rate = failed / total
    - 健康分 = round(100 * (1 - failure_rate * 0.5)) - consecutive_failures * 15
      （失败率权重 0.5：给引擎试错空间——挂掉一半任务仍有 50 基础分；
       连续失败惩罚每次 -15、不设上限：连挂 3 次即到 30 分临界、
       5 次归 0；总分小于 0 时 clamp 为 0）
    - clamp [0, 100]

    issue #439 小样本保护：0 < total < _MIN_SAMPLES 时——
    - 失败率稀释为 failed / _MIN_SAMPLES 再按 0.5 权重计分
      （单次失败 total=1 → 90 分，不再直接打入低分区）
    - 连败惩罚豁免（样本不足时连败不构成统计显著性）
    样本数达到 N 后恢复原公式（原行为不回归）。

    入口归一化（2026-10-23 Decimal 崩溃修复）：本函数被多处调用（健康分重置、
    管理台展示等），调用方可能直接传 MySQL SUM()/COUNT() 返回的 Decimal 或
    None——本函数自身容错（而非只靠 _fetch_task_stats 一处归一化）才是结构性
    修法。归一化放在 total<=0 提前返回之后，提前返回分支自己兜 None。
    """
    if int(total or 0) <= 0:
        return 100
    total = int(total or 0)
    failed = int(failed or 0)
    consecutive_failures = int(consecutive_failures or 0)
    if total < _MIN_SAMPLES:
        failure_rate = failed / _MIN_SAMPLES
        penalty = 0
    else:
        failure_rate = failed / total if total > 0 else 0.0
        penalty = _consecutive_failure_penalty(consecutive_failures)
    base_score = round(100 * (1 - failure_rate * 0.5))
    score = base_score - penalty
    return max(0, min(100, score))


def _snapshot_read_lock():
    """快照读的锁选择：MySQL 每线程连接可无锁读；SQLite 单连接仍需串行。

    Issue #3927：refactor 后聚合在锁外，快照 SELECT 在 MySQL 亦可脱离进程级锁，
    从而把 refresh 的单引擎临界区压到健康分写回的一次短 UPDATE。
    """
    try:
        from src.kernel.session_manager import _is_mysql_backend
        if _is_mysql_backend():
            from contextlib import nullcontext
            return nullcontext()
    except Exception:  # noqa: BLE001 - 探测失败按 SQLite 保守处理
        pass
    return _lock()


def _snapshot_task_rows(runtime_id: int, window: str) -> tuple[list, str]:
    """Issue #3927：短锁快照读——只取行，不做聚合。

    只发两条 SELECT 并 fetchall（取窗口内终态行 + 最近一条状态），聚合计算
    在 `_aggregate_task_rows` 里锁外完成，避免 COUNT/SUM 大扫描 + 逐行判断占用
    进程级 `_lock()`。MySQL 下快照读整体不持进程锁。异常时返回空快照。
    """
    rows: list = []
    last_status = ""
    try:
        with lock_owner(f"health:stats:{runtime_id}"):
            with _snapshot_read_lock():
                c = _db().cursor()
                c.execute(
                    "SELECT status, completed_at FROM agent_tasks "
                    "WHERE runtime_id=? AND created_at >= ? "
                    "AND status IN ('completed','failed','cancelled') "
                    "ORDER BY completed_at DESC",
                    (runtime_id, window))
                rows = list(c.fetchall() or [])
                c.execute(
                    "SELECT status FROM agent_tasks "
                    "WHERE runtime_id=? ORDER BY completed_at DESC LIMIT 1",
                    (runtime_id,))
                last = c.fetchone()
                if last:
                    last_status = last[0] or ""
    except Exception as e:
        log.warning(f"health_scorer: snapshot_task_rows failed for runtime={runtime_id}: {e}")
    return rows, last_status


def _aggregate_task_rows(rows: list, decay: str, last_status: str = "") -> dict:
    """Issue #3927：锁外聚合（纯函数，便于单测/无锁调用）。

    口径与原 SQL 聚合一致：
    - total/failed/completed 来自窗口内终态行；
    - failed_effective 按 completed_at ≥ 衰减窗口起点（None 不计）；
    - consecutive_failures 从最近一条终态倒序，遇到非 failed 即停；最近失败
      已滚出衰减窗口 → 清零（Issue #498 自愈）。
    """
    total = len(rows)
    failed = 0
    completed = 0
    failed_effective = 0
    consec = 0
    last_failure_at = ""
    for st, comp_at in rows:
        if st == "failed":
            failed += 1
            if comp_at and comp_at >= decay:
                failed_effective += 1
        elif st == "completed":
            completed += 1
    # rows 已按 completed_at DESC 排序：连续失败统计到第一个非失败为止
    # （与原 SQL `ORDER BY completed_at DESC LIMIT 50` 口径一致，最多看 50 条）
    for idx, (st, comp_at) in enumerate(rows):
        if idx >= 50:
            break
        if st == "failed":
            if not last_failure_at and comp_at:
                last_failure_at = comp_at
            consec += 1
        else:
            break
    if last_failure_at:
        from src.domains.runtime.engine.health import parse_dt
        ts = parse_dt(last_failure_at)
        if ts is not None and (time.time() - ts) > _FAILURE_DECAY_HOURS * 3600:
            consec = 0
    return {
        "total": total,
        "failed": failed,
        "completed": completed,
        "failed_effective": failed_effective,
        "consecutive_failures": consec,
        "last_failure_at": last_failure_at,
        "last_status": last_status or "",
    }


def _fetch_task_stats(runtime_id: int) -> dict:
    """从 agent_tasks 表查询近 N 天的任务执行统计。

    返回 {total, failed, completed, consecutive_failures, last_status}
    Issue-P2: 窗口起点走 _effective_window_start（含健康分重置赦免，交集语义）。
    2026-10-21 修复：窗口起点先经 _to_sql_utc 规范成纯 UTC 字面量再参与比较
    （带时区偏移的 ISO 串会被 MySQL 按会话时区误解释、抬高 8 小时，见 _to_sql_utc 注释）。
    2026-10-22 修复（Issue #428）：窗口字面量为 T 分隔 'YYYY-MM-DDTHH:MM:SS'，
    与 agent_tasks 时间列（TEXT，Python isoformat 写入的 T 格式 ISO 存量）同格式——
    `created_at >= ?` 字符串比较与 `ORDER BY completed_at DESC` 排序两侧口径一致
    （空格格式会在同日第 11 位 T(0x54) vs 空格(0x20) 上误判，赦免失效）。

    Issue #3927：改为「短锁快照读 + 锁外聚合」，单引擎临界区只覆盖快照 SELECT，
    聚合（COUNT/SUM 等价计算）移出进程级全局锁，避免与 complete 终态写互堵。

    Issue #498 失败衰减（自愈）：
    - failed_effective：completed_at ≥ 衰减窗口起点的失败数（老失败不计入健康分）
    - last_failure_at：最近一次失败时间（排障透出）
    - 最近失败已滚出衰减窗口 → consecutive_failures 清零（无新失败不长期判异常）
    """
    window = _to_sql_utc(_effective_window_start(runtime_id))
    decay = _decay_start()
    rows, last_status = _snapshot_task_rows(runtime_id, window)
    return _aggregate_task_rows(rows, decay, last_status=last_status)


def _fetch_heartbeat(runtime_id: int) -> bool:
    """查询 agent_runtimes 表的心跳状态（Issue #135）。

    本机直连引擎（credential_id=0）与远端 worker（credential_id>0）判定口径不同：
    - 本机 direct 引擎：其 last_seen_at 只由 local probe 每 600s 刷新一次，若按心跳超时
      阈值（_HEARTBEAT_STALE_SECONDS，默认 100s）判定会长期误判失联 → 健康分强制 0 →
      claim_by_tag 永不认领本机引擎（seq#2310 队列停摆根因）。改为按 status=online +
      probe 结果判定：online 即视为活跃（probe 线程每 10min 真实探测 --version 并刷新 healthy）。
    - 远端 worker：维持原语义——last_seen_at 为 NULL / 超阈值 → 失联。

    返回 True 表示心跳正常，False 表示失联。
    """
    from src.domains.runtime.engine.health import parse_dt
    try:
        with _lock():
            c = _db().cursor()
            c.execute(
                "SELECT credential_id, status, last_seen_at FROM agent_runtimes WHERE id=?",
                (runtime_id,))
            row = c.fetchone()
        if not row:
            return False  # 引擎不存在 → 失联
        credential_id = row[0] or 0
        status = (row[1] or "") if row[1] is not None else ""
        last_seen_at = row[2]
        if credential_id == 0:
            # 本机直连引擎：last_seen_at 写节奏慢（local probe 600s），不按心跳超时判失联。
            # 只要 status=online 即视为活跃（online 状态由 local probe 维护，probe 失败会转 offline）。
            return status == "online"
        if not last_seen_at:
            return False  # NULL / 空 → 从未上线 → 失联
        ts = parse_dt(last_seen_at)
        if ts is None:
            return False  # 解析失败 → 保守判为失联
        return (time.time() - ts) <= _heartbeat_stale_threshold()
    except Exception as e:
        log.warning(f"health_scorer: fetch_heartbeat failed for runtime={runtime_id}: {e}")
        return False


def _derive_health_flag(data: dict) -> dict:
    """Issue #498: 统一「健康异常」判定口径（后端单一事实源，前端/接口透出复用）。

    判定顺序（与 is_healthy 拒派口径一致）：
    1. 心跳失联（heartbeat_alive=False）→ heartbeat_lost
    2. 连续失败超限且非小样本（total ≥ _MIN_SAMPLES 时 cf ≥ _MAX_CONSECUTIVE_FAILURES）
       → consecutive_failures（小样本豁免：单次偶发失败不判异常，#439/#498）
    3. 健康分 < _HEALTH_THRESHOLD → low_score
    4. 否则健康（abnormal=False, reason=''）
    """
    if data.get("heartbeat_alive") is False:
        return {"health_abnormal": True, "health_reason": "heartbeat_lost"}
    total = data.get("total_tasks")
    cf = int(data.get("consecutive_failures") or 0)
    if (total is None or int(total) >= _MIN_SAMPLES) and cf >= _MAX_CONSECUTIVE_FAILURES:
        return {"health_abnormal": True, "health_reason": "consecutive_failures"}
    if int(data.get("health_score") or 0) < _HEALTH_THRESHOLD:
        return {"health_abnormal": True, "health_reason": "low_score"}
    return {"health_abnormal": False, "health_reason": ""}


def _local_probe_streak(runtime_id: int) -> int:
    """该引擎连续探测成功次数（agent_runtimes.probe_ok_streak）。

    用作「引擎本身可用」的独立证据：CLI 能起、版本答得出。连败清零点由它触发，
    避免「判不健康 → 拒领 → 永远拿不到任务 → 永远无法成功清零」的死结。
    """
    try:
        from src.kernel.session_manager import db, db_lock
        with db_lock:
            _row = db.cursor().execute(
                "SELECT COALESCE(probe_ok_streak,0) FROM agent_runtimes WHERE id=?",
                (runtime_id,)).fetchone()
        return int(_row[0] or 0) if _row else 0
    except Exception as _e:
        log.debug(f"health: read probe_ok_streak failed for {runtime_id}: {_e}")
        return 0


def _session_fields(runtime_id: int) -> dict:
    """Issue #4346：session 层指标（失败计数/熔断态/provider 校验）透出。

    独立于既有任务层 `ENGINE_OFFLINE` 计分：session 熔断只影响调度候选与观测，
    不改写 `health_score` / `is_healthy` 语义。异常 → 空 dict（fail-open）。
    """
    try:
        from src.domains.runtime.engine import session_guard
        return session_guard.session_state(runtime_id)
    except Exception as e:  # noqa: BLE001 - 观测字段缺失不得阻断健康分主链路
        log.debug(f"health_scorer: session state unavailable for runtime={runtime_id}: {e}")
        return {}


def _compute_and_persist(runtime_id: int) -> dict:
    """计算健康分并持久化到 DB + 更新内存缓存。返回健康度数据 dict。

    Issue #135: 心跳判定优先于失败率计算。
    - 引擎失联（last_seen_at 超时 / NULL / 不存在）→ score=0
    - 心跳正常 → 原失败率公式（total=0 → 100）
    total/failed 仍保留便于排障。

    Issue #498 自愈：健康分按 failed_effective（衰减窗口内的失败数）计算——
    探活/心跳正常 + 衰减窗口内无新失败 → 分数回升至满分口径、连败清零；
    failed_tasks 保留 7 天窗口信息口径便于排障对比。
    """
    stats = _fetch_task_stats(runtime_id)
    # 自锁解除：consecutive_failures 是任务层面的连败，惩罚 + 拒领组合起来会形成死结 ——
    # 引擎因数次失败被判 unhealthy → claim 拒绝它 → 它永远拿不到任务 → 永远无法"成功一次"
    # 来清零连败。本机探测（probe）成功是引擎可用性的独立强证据：CLI 起得来、版本答得出。
    # 因此 probe_ok_streak >= 2 时把任务连败按 0 计（原始值仍保留在返回字段里可观测）。
    try:
        _probe_streak = _local_probe_streak(runtime_id)
        if _probe_streak >= 1 and int(stats.get("consecutive_failures") or 0) > 0:
            _orig = int(stats.get("consecutive_failures") or 0)
            stats["consecutive_failures"] = 0
            stats["consecutive_failures_raw"] = _orig
            log.info(f"health: runtime={runtime_id} probe_ok_streak={_probe_streak} "
                     f"→ task-level consecutive_failures {_orig} treated as 0 (claim deadlock breaker)")
    except Exception as _pe:
        log.debug(f"health: probe streak override skipped for {runtime_id}: {_pe}")
    heartbeat_alive = _fetch_heartbeat(runtime_id)
    # 旧 mock/旧档无 failed_effective 字段 → 回退 failed（兼容）
    failed_effective = stats.get("failed_effective")
    if failed_effective is None:
        failed_effective = stats["failed"]
    if heartbeat_alive:
        score = _compute_health_score(
            stats["total"], failed_effective, stats["consecutive_failures"])
    else:
        score = 0  # 失联强制 0，优先于失败率
    data = {
        "runtime_id": runtime_id,
        "health_score": score,
        "total_tasks": stats["total"],
        "failed_tasks": stats["failed"],
        "failed_tasks_effective": int(failed_effective),
        "completed_tasks": stats["completed"],
        "consecutive_failures": stats["consecutive_failures"],
        "failure_rate": round(stats["failed"] / stats["total"], 4) if stats["total"] > 0 else 0.0,
        "last_status": stats["last_status"],
        "last_failure_at": stats.get("last_failure_at", ""),
        "heartbeat_alive": heartbeat_alive,
        "window_days": _WINDOW_DAYS,
        "failure_decay_hours": _FAILURE_DECAY_HOURS,
        "updated_at": _utcnow().isoformat(),
    }
    data.update(_derive_health_flag(data))
    data.update(_session_fields(runtime_id))
    # 内存缓存
    with _cache_lock:
        _cache[runtime_id] = {
            "score": score,
            "data": data,
            "expires": time.time() + _CACHE_TTL,
        }
    # DB 持久化（upsert）
    _persist_score(data)
    return data


def _persist_score(data: dict):
    """将健康分写入 runtime_health_scores 表（upsert）。

    Issue #498 新列：failed_tasks_effective / last_failure_at
    （db_setup 幂等迁移补列；列缺失时降级为旧列集写入，不抛错）。
    """
    try:
        with lock_owner(f"health:persist:{data['runtime_id']}"):
            with _lock():
                c = _db().cursor()
                try:
                    c.execute(
                        "INSERT OR REPLACE INTO runtime_health_scores "
                        "(runtime_id, health_score, total_tasks, failed_tasks, completed_tasks, "
                        "consecutive_failures, failure_rate, last_status, window_days, updated_at, "
                        "failed_tasks_effective, last_failure_at) "
                        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                        (data["runtime_id"], data["health_score"], data["total_tasks"],
                         data["failed_tasks"], data["completed_tasks"],
                         data["consecutive_failures"], data["failure_rate"],
                         data["last_status"], data["window_days"], data["updated_at"],
                         data.get("failed_tasks_effective", 0),
                         data.get("last_failure_at", "")))
                except Exception:
                    # 旧库未迁移（新列缺失）→ 降级旧列集
                    c.execute(
                        "INSERT OR REPLACE INTO runtime_health_scores "
                        "(runtime_id, health_score, total_tasks, failed_tasks, completed_tasks, "
                        "consecutive_failures, failure_rate, last_status, window_days, updated_at) "
                        "VALUES (?,?,?,?,?,?,?,?,?,?)",
                        (data["runtime_id"], data["health_score"], data["total_tasks"],
                         data["failed_tasks"], data["completed_tasks"],
                         data["consecutive_failures"], data["failure_rate"],
                         data["last_status"], data["window_days"], data["updated_at"]))
                _db().commit()
    except Exception as e:
        log.warning(f"health_scorer: persist_score failed for runtime={data['runtime_id']}: {e}")


# ===== 公共接口 =====

def get_health_score(runtime_id: int, force_refresh: bool = False,
                     allow_compute: bool = True) -> dict | None:
    """获取单个引擎的健康度数据。

    Issue-P2: 读路径改为读 runtime_health_scores 表（表即缓存）——
    双主天然一致，写路径（任务终态触发 / 5min 定时扫描）负责现算落表。
    force_refresh=True 仍强制现算（管理端详情查询用）。
    表中无记录（全新引擎 / 服务刚迁移）→ 现算一条落表。
    allow_compute=False：只读查询路径（如 GET engine-status）——无档时返回 None，
    不触发 _compute_and_persist 的现算落表副作用（Issue #3508 复审）。
    返回 dict：{runtime_id, health_score, total_tasks, failed_tasks,
               completed_tasks, consecutive_failures, failure_rate,
               last_status, heartbeat_alive, window_days, updated_at}

    Issue #135: health_score=0 当且仅当引擎失联（heartbeat_alive=False）。
    """
    if not runtime_id:
        return _empty_score(0)
    if not force_refresh:
        try:
            with _lock():
                c = _db().cursor()
                c.execute(
                    "SELECT health_score, total_tasks, failed_tasks, completed_tasks, "
                    "consecutive_failures, failure_rate, last_status, window_days, updated_at, "
                    "failed_tasks_effective, last_failure_at "
                    "FROM runtime_health_scores WHERE runtime_id=?",
                    (runtime_id,))
                row = c.fetchone()
            if row:
                data = {
                    "runtime_id": runtime_id,
                    "health_score": row[0],
                    "total_tasks": row[1],
                    "failed_tasks": row[2],
                    "completed_tasks": row[3],
                    "consecutive_failures": row[4],
                    "failure_rate": row[5],
                    "last_status": row[6] or "",
                    "heartbeat_alive": None,   # 读档不含实时心跳；失联判定由定时扫描落表
                    "window_days": row[7],
                    "updated_at": row[8] or "",
                    "failed_tasks_effective": int(row[9] or 0),
                    "last_failure_at": row[10] or "",
                }
                data.update(_derive_health_flag(data))
                data.update(_session_fields(runtime_id))
                return data
        except Exception as e:
            log.warning(f"health_scorer: read_score failed for runtime={runtime_id}: {e}")
        # 无记录/旧档列缺失 → 落到现算路径
    if not allow_compute:
        # Issue #3508 复审：GET 查询路径禁止现算落表（避免健康查询写入脏行）
        return None
    return _compute_and_persist(runtime_id)


def get_health_scores(runtime_ids: list[int], force_refresh: bool = False) -> dict[int, dict]:
    """批量获取多个引擎的健康度。返回 {runtime_id: health_data}。"""
    result = {}
    for rid in runtime_ids:
        if rid:
            result[rid] = get_health_score(rid, force_refresh=force_refresh)
    return result


def is_healthy(runtime_id: int, threshold: int | None = None,
               force_refresh: bool = False) -> bool:
    """判断单个引擎是否健康（健康分 >= threshold）。

    threshold 默认 _HEALTH_THRESHOLD（可环境变量调优）。
    连续失败超过 _MAX_CONSECUTIVE_FAILURES 也视为不健康。

    issue #439 小样本保护：窗口任务总数 < _MIN_SAMPLES 时连败拒派豁免
    （单次/少量失败不把低样本引擎打入拒派区，避免 claim_by_tag 拒派死锁）；
    total_tasks 未知（旧档/兼容 mock）时维持原判定。
    """
    if not runtime_id:
        return True  # runtime_id=0（广播/未绑定）不参与健康过滤
    _thr = threshold if threshold is not None else _HEALTH_THRESHOLD
    data = get_health_score(runtime_id, force_refresh=force_refresh)
    _total = data.get("total_tasks")
    if _total is None or _total >= _MIN_SAMPLES:
        if data["consecutive_failures"] >= _MAX_CONSECUTIVE_FAILURES:
            return False
    return data["health_score"] >= _thr


def filter_healthy(runtime_ids: list[int], threshold: int | None = None,
                   force_refresh: bool = False) -> list[int]:
    """从候选引擎列表中过滤出健康的引擎。

    全部不健康时返回空列表（调用方应调 best_fallback 降级兜底）。
    """
    if not runtime_ids:
        return list(runtime_ids)
    healthy = [rid for rid in runtime_ids if is_healthy(rid, threshold, force_refresh)]
    return healthy


def best_fallback(runtime_ids: list[int], force_refresh: bool = False) -> int | None:
    """全部不健康时的降级兜底：返回健康分最高的引擎。

    无候选返回 None。
    """
    if not runtime_ids:
        return None
    scores = get_health_scores(runtime_ids, force_refresh=force_refresh)
    if not scores:
        return None
    best_rid = max(scores.keys(), key=lambda rid: scores[rid]["health_score"])
    return best_rid


def filter_or_fallback(runtime_ids: list[int], threshold: int | None = None,
                       force_refresh: bool = False) -> list[int]:
    """派发链路增强层入口：先过滤健康的，全部不健康时降级取最优。

    始终返回非空列表（只要输入非空），保证派发不会因健康度过滤而无机器可用。
    """
    if not runtime_ids:
        return list(runtime_ids)
    healthy = filter_healthy(runtime_ids, threshold, force_refresh)
    if healthy:
        return healthy
    # 全部不健康 → 降级兜底取健康分最高的
    best_rid = best_fallback(runtime_ids, force_refresh=force_refresh)
    if best_rid is not None:
        log.info(f"health_scorer: all candidates unhealthy, fallback to best={best_rid}")
        return [best_rid]
    return list(runtime_ids)  # 兜底失败时返回原列表（不阻塞派发）


def invalidate_cache(runtime_id: int = 0):
    """清除缓存（任务终态后可调，强制下次重新计算）。"""
    with _cache_lock:
        if runtime_id:
            _cache.pop(runtime_id, None)
        else:
            _cache.clear()


def reset_health(runtime_id: int) -> dict | None:
    """重置单个引擎的健康分（Issue #394）。

    管理员/仓库成员/个人设备 owner 通过 health-reset 管理接口调用：
    写入 agent_runtimes.health_reset_at = 当前 UTC 时间作为赦免点，
    随后清缓存并强制重算——统计窗口从重置点重新开始（见 _effective_window_start），
    此前误降级/历史故障导致的低分被赦免，引擎可恢复参与调度。

    返回重置后的健康度数据 dict；runtime_id 非法（0）时返回 None，不做写入。
    """
    if not runtime_id:
        return None
    reset_at = _utcnow().isoformat()
    try:
        with _lock():
            c = _db().cursor()
            c.execute(
                "UPDATE agent_runtimes SET health_reset_at=? WHERE id=?",
                (reset_at, runtime_id))
            _db().commit()
    except Exception as e:
        log.warning(f"health_scorer: reset_health failed for runtime={runtime_id}: {e}")
        return None
    invalidate_cache(runtime_id)
    log.info(f"health_scorer: health reset for runtime={runtime_id} reset_at={reset_at}")
    data = get_health_score(runtime_id, force_refresh=True)
    if data:
        data["health_reset_at"] = reset_at
    return data


def _empty_score(runtime_id: int) -> dict:
    """空健康度数据（runtime_id=0 或无效时）。"""
    data = {
        "runtime_id": runtime_id,
        "health_score": 100,
        "total_tasks": 0,
        "failed_tasks": 0,
        "failed_tasks_effective": 0,
        "completed_tasks": 0,
        "consecutive_failures": 0,
        "failure_rate": 0.0,
        "last_status": "",
        "last_failure_at": "",
        "heartbeat_alive": True,  # runtime_id=0（广播）不参与心跳判定
        "window_days": _WINDOW_DAYS,
        "failure_decay_hours": _FAILURE_DECAY_HOURS,
        "updated_at": _utcnow().isoformat(),
    }
    data.update(_derive_health_flag(data))
    data.update(_session_fields(runtime_id))
    return data


def record_task_completion(runtime_id: int, status: str):
    """任务终态后调用，即时计算并持久化健康度到 DB + 刷新缓存。

    Issue #85: 改为即时模式——任务完成时立刻重新计算健康分并写入
    runtime_health_scores 表，增强健康度分析的及时性。
    不改变广播/分发语义——仅在任务终态后异步刷新。

    Issue #4346（复审 R1）：session 熔断/半开的收口**不在这里**。它必须覆盖全部终态
    （取消 / exit!=0 业务失败 / 超时判死），而本函数只拿得到「是 completed 就复位」这
    一半语义；收口点已统一到 `service.update_task_status` 终态事务内的
    `session_guard.note_claim_outcome(c, runtime_id, status)`（方案 §4 的
    note_claim_outcome 落点）。这里只做健康分重算，避免两处口径再分叉。
    ``status`` 保留形参仅供调用方兼容（健康分只看终态事实，不再据此改 session 状态）。
    """
    if runtime_id:
        try:
            _compute_and_persist(runtime_id)
        except Exception as e:
            log.warning(f"health_scorer: record_task_completion failed for runtime={runtime_id}: {e}")
            invalidate_cache(runtime_id)


# ===== 健康度汇总（供 API 查询 / 分析脚本）=====

def get_all_health_scores(force_refresh: bool = False) -> list[dict]:
    """获取所有已注册引擎的健康度（供 API 查询 / 前端展示）。

    从 agent_runtimes 表读所有引擎 id，逐个计算健康分。
    """
    try:
        with _lock():
            c = _db().cursor()
            c.execute("SELECT id FROM agent_runtimes ORDER BY id")
            rids = [r[0] for r in c.fetchall()]
    except Exception as e:
        log.warning(f"health_scorer: get_all_health_scores failed: {e}")
        return []
    return [get_health_score(rid, force_refresh=force_refresh) for rid in rids]


# ===== Issue #85: 定时刷新调度器 =====

_REFRESH_INTERVAL = int(os.environ.get("HEALTH_REFRESH_INTERVAL", "60"))  # 默认 1 分钟
# 周期从 300s 收到 60s：心跳/失联是秒级事实，5 分钟的刷新间隔会让「心跳已恢复」
# 与「分数还是 0」并存好几分钟（用户侧看到的就是「接入引擎健康分全是 0」）。
# 7~20 个引擎的量级下每分钟一轮的查询成本可以忽略。
_refresh_thread: threading.Thread | None = None
_refresh_stop = threading.Event()


def _refresh_all_loop():
    """后台循环：定期对所有引擎强制刷新健康分并持久化到 DB。

    覆盖场景：
    - 引擎离线后无任务事件 → record_task_completion 不会被触发，需定时刷新
    - 引擎心跳超时 → _fetch_heartbeat 判定失联，score 降为 0
    - 服务重启后缓存清空 → 首次查询走 DB 旧值，定时刷新纠正
    """
    log.info(f"health_scorer: refresh loop started (interval={_REFRESH_INTERVAL}s)")
    while not _refresh_stop.is_set():
        if _refresh_stop.wait(_REFRESH_INTERVAL):
            break  # stop 信号
        try:
            with lock_owner("health:refresh:ids"):
                with _lock():
                    c = _db().cursor()
                    c.execute("SELECT id FROM agent_runtimes ORDER BY id")
                    rids = [r[0] for r in c.fetchall()]
            stale = 0
            for rid in rids:
                try:
                    with _cache_lock:
                        old = _cache.get(rid, {}).get("data", {}).get("health_score")
                    data = _compute_and_persist(rid)
                    if old is not None and data["health_score"] != old:
                        log.info(f"health_scorer: refresh rid={rid} score {old} -> {data['health_score']}")
                    if data["health_score"] == 0 and data["heartbeat_alive"] is False:
                        stale += 1
                except Exception as e:
                    log.warning(f"health_scorer: refresh failed for rid={rid}: {e}")
            if stale:
                log.info(f"health_scorer: refresh loop done, {stale}/{len(rids)} engine(s) offline")
        except Exception as e:
            log.warning(f"health_scorer: refresh loop error: {e}")
    log.info("health_scorer: refresh loop stopped")


def start_health_refresh_scheduler():
    """启动后台定时刷新线程（幂等，重复调用不创建多个线程）。"""
    global _refresh_thread
    if _refresh_thread and _refresh_thread.is_alive():
        return
    _refresh_stop.clear()
    _refresh_thread = threading.Thread(target=_refresh_all_loop, daemon=True)
    _refresh_thread.start()


def stop_health_refresh_scheduler():
    """停止定时刷新线程（服务关闭时调用）。"""
    _refresh_stop.set()
    if _refresh_thread:
        _refresh_thread.join(timeout=5)
