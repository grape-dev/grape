"""机器健康度评估模块（Issue #134 / #133 Spec）。

基于任务中心 DB 的历史执行记录，按滑动窗口（近 7 天）计算 per-machine：
- 总任务数、失败数、失败率
- 连续失败次数（consecutive_failures）
- 健康分（0-100，越高越健康）

健康分 = round(100 * (1 - failure_rate)) - consecutive_failure_penalty
  - failure_rate = failed / total（total=0 时返回满分 100）
  - consecutive_failure_penalty = min(consecutive_failures * 5, 20)
  - 最终 clamp 到 [0, 100]

设计决策：
- DB 持久化（runtime_health_scores 表）+ 内存缓存（60s TTL）
- 派发链路作为增强层调用 get_health_scores / filter_healthy / best_fallback
- 不改变广播协议/任务分发语义/A2A 事件流转
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
    """
    window = _window_start()
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


def _compute_and_persist(runtime_id: int) -> dict:
    """计算健康分并持久化到 DB + 更新内存缓存。返回健康度数据 dict。"""
    stats = _fetch_task_stats(runtime_id)
    score = _compute_health_score(
        stats["total"], stats["failed"], stats["consecutive_failures"])
    data = {
        "runtime_id": runtime_id,
        "health_score": score,
        "total_tasks": stats["total"],
        "failed_tasks": stats["failed"],
        "completed_tasks": stats["completed"],
        "consecutive_failures": stats["consecutive_failures"],
        "failure_rate": round(stats["failed"] / stats["total"], 4) if stats["total"] > 0 else 0.0,
        "last_status": stats["last_status"],
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
    """获取单个引擎的健康度数据（带内存缓存）。

    force_refresh=True 时跳过缓存重新计算。
    返回 dict：{runtime_id, health_score, total_tasks, failed_tasks,
               completed_tasks, consecutive_failures, failure_rate,
               last_status, window_days, updated_at}
    """
    if not runtime_id:
        return _empty_score(0)
    # 检查缓存
    if not force_refresh:
        with _cache_lock:
            cached = _cache.get(runtime_id)
            if cached and cached["expires"] > time.time():
                return cached["data"]
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
        "window_days": _WINDOW_DAYS,
        "updated_at": _utcnow().isoformat(),
    }


def record_task_completion(runtime_id: int, status: str):
    """任务终态后调用，清除该引擎缓存（下次查询时重新计算）。

    不改变广播/分发语义——仅在任务终态后异步刷新缓存。
    """
    if runtime_id:
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
