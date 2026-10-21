"""机器健康度评估模块（Issue #134 / #133 / #135 Spec）。

基于任务中心 DB 的历史执行记录，按滑动窗口（近 7 天）计算 per-machine：
- 总任务数、失败数、失败率
- 连续失败次数（consecutive_failures）
- 健康分（0-100，越高越健康）

健康分 = round(100 * (1 - failure_rate)) - consecutive_failure_penalty
  - failure_rate = failed / total（total=0 且心跳正常时返回满分 100）
  - consecutive_failure_penalty = min(consecutive_failures * 5, 20)
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

from src.config import log

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

# ===== 内存缓存 =====

_cache: dict = {}  # {runtime_id: {"score": int, "data": dict, "expires": float}}
_cache_lock = threading.Lock()


def _db():
    from src.session_manager import db
    return db


def _lock():
    from src.session_manager import db_lock
    return db_lock


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _window_start() -> str:
    """滑动窗口起始时间（ISO 格式，近 N 天）。"""
    return (_utcnow() - timedelta(days=_WINDOW_DAYS)).isoformat()


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


_SQL_DT_FMT = "%Y-%m-%d %H:%M:%S"


def _to_sql_utc(ts) -> str:
    """把时间戳规范成 MySQL DATETIME 字面量（UTC、秒精度，'YYYY-MM-DD HH:MM:SS'）。

    为什么必须规范（2026-10-21 dev 实测）：agent_tasks.created_at 由 UTC_TIMESTAMP()
    写入（纯 UTC datetime），而赦免窗口起点曾是带时区偏移的 ISO 串（...T07:49:24+00:00）。
    MySQL 把该 ISO 串按「会话时区」解释，实测被抬高 8 小时
    （CAST('2026-10-21T07:49:24.519516+00:00' AS DATETIME) = 2026-10-21 15:49:25）→
    窗口起点落到未来，重置后 8 小时内的新失败全部落在窗口之外 → 健康分不降
    （赦免期退化成"免疫期"）。7 天窗口写法同一隐患，只是被量级掩盖。

    性能：只在每次统计前转换一次（两次查询共用一个常量参数），
    **不在 SQL 里对列做任何函数运算** → idx_tasks_runtime_status(runtime_id,status)
    照旧可用；快路径是纯字符串切片，无 datetime 解析开销。

    精度语义：agent_tasks.created_at 是秒精度，故窗口也取秒精度；与重置点同一秒内
    产生的失败按"发生在重置之后"计入（保守方向——宁可计失败，也不制造免疫期）。
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
        # 快路径：'YYYY-MM-DDTHH:MM:SS' 或带小数秒（偏移已剥掉）→ 直接切片
        if len(body) >= 19 and body[10] == "T":
            tail = body[19:]
            if tail == "" or tail.startswith("."):
                return body[:10] + " " + body[11:19]
        dt = datetime.fromisoformat(s)
    if dt.tzinfo is not None:
        dt = dt.astimezone(timezone.utc)
    return dt.strftime(_SQL_DT_FMT)


# ===== 核心计算 =====

def _compute_health_score(total: int, failed: int, consecutive_failures: int) -> int:
    """根据任务统计计算健康分（0-100）。

    - total=0 → 100（无历史数据，默认健康）
    - failure_rate = failed / total
    - 健康分 = round(100 * (1 - failure_rate)) - min(consecutive_failures * 5, 20)
    - clamp [0, 100]
    """
    if total <= 0:
        return 100
    failure_rate = failed / total if total > 0 else 0.0
    base_score = round(100 * (1 - failure_rate))
    penalty = min(consecutive_failures * 5, 20)
    score = base_score - penalty
    return max(0, min(100, score))


def _fetch_task_stats(runtime_id: int) -> dict:
    """从 agent_tasks 表查询近 N 天的任务执行统计。

    返回 {total, failed, completed, consecutive_failures, last_status}
    Issue-P2: 窗口起点走 _effective_window_start（含健康分重置赦免，交集语义）。
    2026-10-21 修复：窗口起点先经 _to_sql_utc 规范成纯 UTC datetime 字面量再参与比较
    （带时区偏移的 ISO 串会被 MySQL 按会话时区误解释、抬高 8 小时，见 _to_sql_utc 注释）。
    """
    window = _to_sql_utc(_effective_window_start(runtime_id))
    result = {"total": 0, "failed": 0, "completed": 0,
              "consecutive_failures": 0, "last_status": ""}
    try:
        with _lock():
            c = _db().cursor()
            # 近 N 天总任务数 + 失败数 + 完成数
            c.execute(
                "SELECT COUNT(*), "
                "SUM(CASE WHEN status='failed' THEN 1 ELSE 0 END), "
                "SUM(CASE WHEN status='completed' THEN 1 ELSE 0 END) "
                "FROM agent_tasks "
                "WHERE runtime_id=? AND created_at >= ? "
                "AND status IN ('completed','failed','cancelled')",
                (runtime_id, window))
            row = c.fetchone()
            if row:
                result["total"] = row[0] or 0
                result["failed"] = row[1] or 0
                result["completed"] = row[2] or 0

            # 连续失败次数：从最近一条终态任务倒序遍历，统计连续 failed
            c.execute(
                "SELECT status FROM agent_tasks "
                "WHERE runtime_id=? AND created_at >= ? "
                "AND status IN ('completed','failed','cancelled') "
                "ORDER BY completed_at DESC LIMIT 50",
                (runtime_id, window))
            consec = 0
            for (st,) in c.fetchall():
                if st == "failed":
                    consec += 1
                else:
                    break  # 遇到非失败即停止
            result["consecutive_failures"] = consec

            # 最近一条任务状态
            c.execute(
                "SELECT status FROM agent_tasks "
                "WHERE runtime_id=? ORDER BY completed_at DESC LIMIT 1",
                (runtime_id,))
            last = c.fetchone()
            if last:
                result["last_status"] = last[0] or ""
    except Exception as e:
        log.warning(f"health_scorer: fetch_task_stats failed for runtime={runtime_id}: {e}")
    return result


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
    from src.runtime.health import parse_dt
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
        return (time.time() - ts) <= _HEARTBEAT_STALE_SECONDS
    except Exception as e:
        log.warning(f"health_scorer: fetch_heartbeat failed for runtime={runtime_id}: {e}")
        return False


def _compute_and_persist(runtime_id: int) -> dict:
    """计算健康分并持久化到 DB + 更新内存缓存。返回健康度数据 dict。

    Issue #135: 心跳判定优先于失败率计算。
    - 引擎失联（last_seen_at 超时 / NULL / 不存在）→ score=0
    - 心跳正常 → 原失败率公式（total=0 → 100）
    total/failed 仍保留便于排障。
    """
    stats = _fetch_task_stats(runtime_id)
    heartbeat_alive = _fetch_heartbeat(runtime_id)
    if heartbeat_alive:
        score = _compute_health_score(
            stats["total"], stats["failed"], stats["consecutive_failures"])
    else:
        score = 0  # 失联强制 0，优先于失败率
    data = {
        "runtime_id": runtime_id,
        "health_score": score,
        "total_tasks": stats["total"],
        "failed_tasks": stats["failed"],
        "completed_tasks": stats["completed"],
        "consecutive_failures": stats["consecutive_failures"],
        "failure_rate": round(stats["failed"] / stats["total"], 4) if stats["total"] > 0 else 0.0,
        "last_status": stats["last_status"],
        "heartbeat_alive": heartbeat_alive,
        "window_days": _WINDOW_DAYS,
        "updated_at": _utcnow().isoformat(),
    }
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
    """将健康分写入 runtime_health_scores 表（upsert）。"""
    try:
        with _lock():
            c = _db().cursor()
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

def get_health_score(runtime_id: int, force_refresh: bool = False) -> dict:
    """获取单个引擎的健康度数据。

    Issue-P2: 读路径改为读 runtime_health_scores 表（表即缓存）——
    双主天然一致，写路径（任务终态触发 / 5min 定时扫描）负责现算落表。
    force_refresh=True 仍强制现算（管理端详情查询用）。
    表中无记录（全新引擎 / 服务刚迁移）→ 现算一条落表。
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
                    "consecutive_failures, failure_rate, last_status, window_days, updated_at "
                    "FROM runtime_health_scores WHERE runtime_id=?",
                    (runtime_id,))
                row = c.fetchone()
            if row:
                return {
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
                }
        except Exception as e:
            log.warning(f"health_scorer: read_score failed for runtime={runtime_id}: {e}")
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
    """
    if not runtime_id:
        return True  # runtime_id=0（广播/未绑定）不参与健康过滤
    _thr = threshold if threshold is not None else _HEALTH_THRESHOLD
    data = get_health_score(runtime_id, force_refresh=force_refresh)
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
    return {
        "runtime_id": runtime_id,
        "health_score": 100,
        "total_tasks": 0,
        "failed_tasks": 0,
        "completed_tasks": 0,
        "consecutive_failures": 0,
        "failure_rate": 0.0,
        "last_status": "",
        "heartbeat_alive": True,  # runtime_id=0（广播）不参与心跳判定
        "window_days": _WINDOW_DAYS,
        "updated_at": _utcnow().isoformat(),
    }


def record_task_completion(runtime_id: int, status: str):
    """任务终态后调用，即时计算并持久化健康度到 DB + 刷新缓存。

    Issue #85: 改为即时模式——任务完成时立刻重新计算健康分并写入
    runtime_health_scores 表，增强健康度分析的及时性。
    不改变广播/分发语义——仅在任务终态后异步刷新。
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

_REFRESH_INTERVAL = int(os.environ.get("HEALTH_REFRESH_INTERVAL", "300"))  # 默认 5 分钟
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
            with _lock():
                c = _db().cursor()
                c.execute("SELECT id FROM agent_runtimes ORDER BY id")
                rids = [r[0] for r in c.fetchall()]
            stale = 0
            for rid in rids:
                try:
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
