# -*- coding: utf-8 -*-
"""Issue #496：「PR merged → sre 收尾」链路场景引擎离线的派发级自动重试。

背景（日志证据，grape events detail seq 28353/14741/13993/13159）：
pull_request_merged 触发的 sre 事件在派发时命中 #259 方案A ——
`get_scene_pool_engine_ids` 返回 None（场景绑定引擎全部离线）→
`_fail_event` 立即终态 failed。PR merge webhook 是一次性投递，没有二次
触发机会：引擎离线窗口内发生的每一次 PR 合并，其 sre 收尾事件都会永久
飘红，形成系统性批量 failed（Issue #496 反馈 #31，seq 29249 等 7 例）。

修复思路（保留 #259「宁失败不跑飞」语义，不引入僵尸 running）：
1. 离线时仍即时 `_fail_event` 终态 failed（防 sweeper 误杀、防并发滑入）；
2. 同时为本单链路追加 threading.Timer 退避轮询：场景引擎池恢复后
   `reset_event_terminal` 复位事件 → 重新 pick_engine → 按原派发段语义
   入队（远程广播/定向）或直连执行；
3. 进程重启丢失 timer 时至多退化为旧行为（事件保持 failed，可人工重派），
   不会更差；重派前校验事件仍为 failed 且无活跃任务，防重复派发。

边界：不改动 #494（超时归因透出）与 #495（worker CLI 健康）已修复范围；
执行级瞬时异常（exit 0 空输出等）由 #403 transient_retry 覆盖，此处不重复。
"""
from __future__ import annotations

import os
import threading

from src.config import log

_DEFAULT_ATTEMPTS = 6    # 首次离线后最多自动重试次数
_DEFAULT_BASE_DELAY = 60  # 首次重试延迟（秒），指数退避
_MAX_DELAY = 300          # 单次延迟上限（秒），总窗口 ≈ 60+120+240+300*3 ≈ 22min

# 进程内调度去重（同 seq 只挂一条重试链）
_scheduled: set = set()
_sched_lock = threading.Lock()


def _cfg_attempts() -> int:
    try:
        return int(os.environ.get("GRAPE_PR_MERGED_SRE_RETRY_ATTEMPTS",
                                  str(_DEFAULT_ATTEMPTS)) or _DEFAULT_ATTEMPTS)
    except Exception:
        return _DEFAULT_ATTEMPTS


def _cfg_base_delay() -> int:
    try:
        return int(os.environ.get("GRAPE_PR_MERGED_SRE_RETRY_DELAY",
                                  str(_DEFAULT_BASE_DELAY)) or _DEFAULT_BASE_DELAY)
    except Exception:
        return _DEFAULT_BASE_DELAY


def _delay_for(n: int) -> int:
    return min(_cfg_base_delay() * (2 ** max(0, n - 1)), _MAX_DELAY)


def _reset_registry() -> None:
    """测试辅助：清空进程内调度去重表。"""
    with _sched_lock:
        _scheduled.clear()


def _release_seq(seq: int) -> None:
    with _sched_lock:
        _scheduled.discard(seq)


def _normalize_params(repo_key, seq, agent_type, backend, skill_name, skill_id,
                      skill_path, evt, issue_id="", runtime_id=0,
                      scene_id="", scene_uid=0) -> dict:
    return {
        "repo_key": repo_key or "", "seq": int(seq or 0),
        "agent_type": agent_type or "", "backend": backend or "",
        "skill_name": skill_name or "", "skill_id": int(skill_id or 0),
        "skill_path": skill_path or "", "evt": dict(evt or {}),
        "issue_id": issue_id or "", "runtime_id": int(runtime_id or 0),
        "scene_id": scene_id or "", "scene_uid": int(scene_uid or 0),
    }


def schedule_pr_merged_sre_retry(repo_key, seq, agent_type, backend, skill_name,
                                 skill_id, skill_path, evt, issue_id="",
                                 runtime_id=0, scene_id="", scene_uid=0,
                                 attempts=None, base_delay=None) -> bool:
    """离线 fail 后安排自动重试（仅 pull_request_merged → sre 收尾链路）。

    attempts / base_delay 仅供测试注入；生产取环境变量默认值。
    返回 True 表示已挂上重试链（调用方可在 fail 文案中追加「已安排自动重试」）。
    """
    if not seq or agent_type != "sre":
        return False
    if not scene_id or not skill_name:
        return False
    if (evt or {}).get("type") != "pull_request_merged":
        return False
    with _sched_lock:
        if seq in _scheduled:
            log.info(f"[496-retry] seq={seq} already scheduled, skip duplicate")
            return False
        _scheduled.add(seq)
    p = _normalize_params(repo_key, seq, agent_type, backend, skill_name, skill_id,
                          skill_path, evt, issue_id, runtime_id, scene_id, scene_uid)
    n_attempts = _cfg_attempts() if attempts is None else int(attempts)
    _schedule_attempt(1, p, n_attempts, base_delay)
    log.info(f"[496-retry] scheduled seq={seq} scene={scene_id} "
             f"attempts={n_attempts} base_delay={base_delay if base_delay is not None else _cfg_base_delay()}s")
    return True


def _schedule_attempt(n: int, p: dict, attempts: int, base_delay=None) -> None:
    delay = base_delay if base_delay is not None else _delay_for(n)
    t = threading.Timer(delay, _run_attempt, args=(n, p, attempts, base_delay))
    t.daemon = True
    t.start()


def _pick_ready_engine(p: dict):
    """引擎池就绪判定：返回 (engine_id, pool) 或 (None, None)（未就绪）。"""
    from src.runtime.engine_select import get_scene_pool_engine_ids, pick_engine
    pool = get_scene_pool_engine_ids(p["scene_id"], p["agent_type"],
                                     repo_key=p["repo_key"], user_id=p["scene_uid"])
    if pool is None:
        return None, None
    engine_id = pick_engine(p["repo_key"], p["agent_type"],
                            fallback_runtime_id=p["runtime_id"] or 0,
                            scene_id=p["scene_id"], user_id=p["scene_uid"])
    if engine_id is None:
        return None, None
    return engine_id, pool


def _run_attempt(n: int, p: dict, attempts: int, base_delay=None) -> None:
    """单次重试：就绪 → 重派；未就绪 → 退避续排 / 耗尽收口。"""
    seq = p["seq"]
    try:
        engine_id, pool = _pick_ready_engine(p)
        if engine_id is None:
            if n >= attempts:
                _mark_retry_exhausted(seq, attempts)
            else:
                _schedule_attempt(n + 1, p, attempts, base_delay)
            return
        _redispatch(p, engine_id, pool)
    except Exception as e:
        log.warning(f"[496-retry] attempt {n}/{attempts} seq={seq} error: {e}")
        if n >= attempts:
            _mark_retry_exhausted(seq, attempts, err=e)
        else:
            _schedule_attempt(n + 1, p, attempts, base_delay)


def _claim_for_redispatch(seq: int) -> bool:
    """原子 claim：仅当事件仍为 failed 且无活跃任务时，条件 UPDATE 复位为 running。

    check 与 reset 合并为同锁同事务的条件 UPDATE（WHERE status='failed'），
    消除 check-then-act 窗口内人工重派并发滑入导致的重复派发；
    复位语义与 event_store.reset_event_terminal 对齐（清 exit_code/structured_output，
    并重抢 event guard 防重跑期间并发事件滑入）。
    """
    from src.session_manager import db, db_lock
    try:
        with db_lock:
            c = db.cursor()
            c.execute("SELECT COUNT(*) FROM agent_tasks WHERE event_seq=? "
                      "AND status IN ('queued','running','dispatched')", (seq,))
            if (c.fetchone()[0] or 0) > 0:
                return False
            c.execute(
                "UPDATE events SET status='running', exit_code=NULL, completed_at='', "
                "structured_output=NULL, result='' WHERE seq=? AND status='failed'",
                (seq,))
            claimed = (c.rowcount or 0) > 0
            db.commit()
        if claimed:
            try:
                with db_lock:
                    row = db.cursor().execute(
                        "SELECT repo_key, issue_id, agent_type FROM events "
                        "WHERE seq=? LIMIT 1", (seq,)).fetchone()
                if row:
                    from src.api.event_store import (claim_event_guard,
                                                     update_event_guard_seq)
                    claim_event_guard(row[0], row[1] or "#0", row[2] or "")
                    update_event_guard_seq(row[0], row[1] or "#0", row[2] or "", seq)
            except Exception as ge:
                log.warning(f"[496-retry] guard re-claim seq={seq} failed: {ge}")
        return claimed
    except Exception as e:
        log.warning(f"[496-retry] claim check seq={seq} failed: {e}")
        return False


def _redispatch(p: dict, engine_id: int, pool: str) -> bool:
    """引擎恢复后的自动重派：复位事件 → 按原派发段语义重新入队/直连执行。"""
    seq = p["seq"]
    if not _claim_for_redispatch(seq):
        log.info(f"[496-retry] seq={seq} not in failed-idle state (taken over?), "
                 f"skip auto redispatch")
        _release_seq(seq)
        return True
    try:
        _dispatch_picked(p, engine_id, pool)
        _release_seq(seq)
        log.info(f"[496-retry] seq={seq} auto-redispatched engine={engine_id} pool={pool}")
        return True
    except Exception as e:
        log.warning(f"[496-retry] seq={seq} redispatch failed: {e}")
        # claim 已把事件复位 running，重派失败 → 收口回 failed
        # （不收口会成为无任务支撑的僵尸 running）
        try:
            from src.api.event_pipeline import _fail_event
            _fail_event(seq, f"[Issue #496] 引擎恢复后自动重派失败：{str(e)[:150]}")
        except Exception as _fe:
            log.warning(f"[496-retry] seq={seq} fail-close error: {_fe}")
        _release_seq(seq)
        return False


def _dispatch_picked(p: dict, engine_id: int, pool: str) -> None:
    """与 run_pipeline 派发段（引擎选中后的入队/直连分支）保持同一语义。"""
    from src.runtime.engine_select import engine_name
    from src.api.event_pipeline import is_plugin_mode, _enqueue_pipeline_task
    seq = p["seq"]
    _be = engine_name(engine_id) or p["backend"] or ""
    if not engine_id and is_plugin_mode():
        # 无主池广播（plugin 模式 engine_id=0）：pool 限定认领范围，来源=scene
        # backend 兜底先于参数校验（run_pipeline 广播分支为 backend or "opencode"，
        # 广播路径不因 backend 为空而失败）
        _be = _be or "opencode"
        if not p["skill_name"]:
            raise RuntimeError(f"missing dispatch params backend={_be!r} "
                               f"skill={p['skill_name']!r}")
        _enqueue_pipeline_task(p["repo_key"], seq, p["agent_type"], _be,
                               p["skill_name"], p["evt"], p["skill_id"],
                               p["skill_path"], 0, p["issue_id"],
                               pool_engine_ids=pool, user_id=p["scene_uid"],
                               pool_engine_source="scene")
        return
    if not _be or not p["skill_name"]:
        raise RuntimeError(f"missing dispatch params backend={_be!r} "
                           f"skill={p['skill_name']!r}")
    from src.runtime.service import runtime_execution_kind, EXEC_MODE_DIRECT
    if runtime_execution_kind(engine_id) != EXEC_MODE_DIRECT:
        # 远程定向：确定性路由到恢复的引擎
        _enqueue_pipeline_task(p["repo_key"], seq, p["agent_type"], _be,
                               p["skill_name"], p["evt"], p["skill_id"],
                               p["skill_path"], engine_id, p["issue_id"],
                               pool_engine_ids=pool, user_id=p["scene_uid"],
                               pool_engine_source="scene")
        return
    # 本机直连：建任务行（防 sweeper）+ 提交执行
    from src.api.event_pipeline import _create_direct_task, start_direct_agent, _execute_agent
    tid = _create_direct_task(p["repo_key"], seq, p["agent_type"], _be,
                              p["skill_name"], p["skill_path"], engine_id,
                              p["evt"], p["issue_id"], skill_id=p["skill_id"])
    if not tid:
        raise RuntimeError("create_direct_task returned empty task_id")
    start_direct_agent(_execute_agent,
                       (p["repo_key"], seq, p["agent_type"], _be, p["skill_name"],
                        p["evt"], engine_id, tid),
                       name=f"ag-{p['agent_type']}-{seq}")


def _mark_retry_exhausted(seq: int, attempts: int, err=None) -> None:
    """重试耗尽收口：failed 事件追加终止说明；running（复位后重派异常）收口回 failed。"""
    _release_seq(seq)
    note = (f"[Issue #496] 场景引擎离线自动重试 {attempts} 次后仍未恢复，自动重试终止，"
            f"请检查引擎在线状态后人工重派")
    if err:
        note += f"（最后一次异常：{str(err)[:120]}）"
    status = ""
    try:
        from src.session_manager import db, db_lock
        with db_lock:
            c = db.cursor()
            c.execute("SELECT status FROM events WHERE seq=?", (seq,))
            row = c.fetchone()
            if not row:
                return
            status = row[0]
            if status == "failed":
                c.execute("UPDATE events SET result=COALESCE(result,'') || ? WHERE seq=?",
                          ("\n" + note, seq))
                db.commit()
    except Exception as e:
        log.warning(f"[496-retry] exhausted mark seq={seq} failed: {e}")
        return
    if status == "running":
        # reset_event_terminal 后重派链路异常 → 收口回 failed，防僵尸 running
        try:
            from src.api.event_pipeline import _fail_event
            _fail_event(seq, note)
        except Exception as e:
            log.warning(f"[496-retry] exhausted fail-close seq={seq} error: {e}")
    log.info(f"[496-retry] seq={seq} auto retry exhausted after {attempts} attempts")
