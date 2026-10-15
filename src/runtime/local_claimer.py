"""本机 direct 引擎一等认领器（Local Claimer）。

设计文档：docs/weekly-07/local-claimer-first-class-broadcast-design.md

plugin 广播模式下，本机 direct 引擎（credential_id=0）此前不是认领者——
依赖方案B/方案A的"入队即 self-run"兜底，绕过 pool_engine_ids 白名单，
造成 2026-10-14 pvs-zombies-demo2 issue #4 全链路跑错引擎事故。

本模块让本机引擎成为与远端 worker 完全同级的认领者：
- 直接调用 service.claim_by_tag(tag, runtime_id)——tag 匹配、scope 隔离、
  引擎满槽、pool_engine_ids 白名单、健康分拦截、会话引擎守卫，全部走
  同一条检查链，不引入任何新认领规则；
- 3s 轮询 + jitter，与 worker 的 claim 节奏一致；
- 认领后复用 direct 执行通道（start_direct_agent + _execute_agent），
  evt 从 events 表按 event_seq 忠实重建（payload/type/actor 落库齐全）。

双主安全：claim_by_tag 的 UPDATE ... WHERE status='queued' 原子性保证
不双跑（谁先 UPDATE 成功谁执行，另一方 rowcount=0 自然放弃）。

开关：GRAPE_LOCAL_CLAIMER（默认开启；关闭时本机引擎不参与广播认领）。
"""
import asyncio
import json
import os
import random

from src.config import log

CLAIMER_ENABLED = os.environ.get("GRAPE_LOCAL_CLAIMER", "1").strip().lower() in ("1", "true", "yes", "on")
CLAIM_INTERVAL = 3.0  # 与 worker 的 RUNTIME_POLL_INTERVAL 默认值一致


def _local_direct_engine_ids() -> list[int]:
    """本机 direct 引擎发现：credential_id=0（本机直连）且 scope='global' 且在线。

    与方案B的引擎发现同源（吸收自 _pool_only_local_direct 的查询）。
    unhealthy 的引擎照常返回——健康分拦截在 claim_by_tag 内完成
    （与远端 worker 同约束，低分引擎 claim 返回 None 不抢任务）。
    """
    try:
        from src.session_manager import db, db_lock
        with db_lock:
            c = db.cursor()
            rows = c.execute(
                "SELECT id FROM agent_runtimes "
                "WHERE status='online' AND credential_id=0 AND scope='global' "
                "ORDER BY CASE name WHEN 'opencode' THEN 0 WHEN 'codearts' THEN 1 ELSE 2 END, id"
            ).fetchall()
        return [int(r[0]) for r in rows]
    except Exception as e:
        log.warning(f"local_claimer: engine discovery failed: {e}")
        return []


def _engine_tags(runtime_id: int) -> list[str]:
    """引擎的认领标签（engine_capabilities，通常为 ['all']）。"""
    try:
        from src.session_manager import db, db_lock
        with db_lock:
            c = db.cursor()
            rows = c.execute(
                "SELECT tag FROM engine_capabilities WHERE runtime_id=? AND enabled=1 "
                "AND (tag!='' OR tag='all')", (runtime_id,)).fetchall()
        tags = [r[0] for r in rows if r[0]]
        # 'all' 已覆盖全部 pool_tag；普通引擎只带精确 tag
        return tags if tags else ["all"]
    except Exception as e:
        log.warning(f"local_claimer: tags query failed for {runtime_id}: {e}")
        return []


def _engine_busy(runtime_id: int) -> bool:
    """引擎满载检查（busy_slots >= max_concurrent → 跳过，满载不抢）。"""
    try:
        from src.session_manager import db, db_lock
        with db_lock:
            c = db.cursor()
            c.execute("SELECT busy_slots, max_concurrent FROM agent_runtimes WHERE id=?", (runtime_id,))
            r = c.fetchone()
        if r and r[1] and (r[0] or 0) >= (r[1] or 0):
            return True
        return False
    except Exception:
        return True  # 查询失败保守视为忙（不抢）


def _rebuild_evt_from_task(task: dict) -> dict:
    """按任务行 + events 表重建 _execute_agent 所需的 evt。

    enqueue 时的原始 evt 含完整 webhook payload；任务行只存摘要字段，
    但 events 表按 seq 落库了 payload/type/actor/source——足以让
    build_devops_agent_prompt 忠实重建（与 enqueue 时同一数据源）。
    autopilot 等不写 events 表的任务（event_seq 查不到）→ 返回最小 evt，
    _execute_agent_inner 会按 task 行内已有 prompt 走（writeback 只填空）。
    """
    evt: dict = {
        "repo_key": task.get("repo_key") or "",
        "issue_id": task.get("issue_id") or "",
        "type": task.get("event_type") or "",
        "account": task.get("account") or "",
        "repository": (task.get("repo_key") or "").split(":", 1)[-1],
    }
    seq = task.get("event_seq") or 0
    if not seq:
        return evt
    try:
        from src.session_manager import db, db_lock
        with db_lock:
            c = db.cursor()
            c.execute(
                "SELECT event_type, source, actor, payload, title FROM events WHERE seq=?",
                (seq,))
            r = c.fetchone()
        if r:
            evt["type"] = r[0] or evt.get("type", "")
            evt["source"] = r[1] or ""
            evt["actor"] = r[2] or ""
            try:
                evt["payload"] = json.loads(r[3]) if r[3] else {}
            except Exception:
                evt["payload"] = {}
            if r[1]:
                evt["repository"] = (task.get("repo_key") or "").split(":", 1)[-1]
            if r[4]:
                evt["title"] = r[4]
    except Exception as e:
        log.warning(f"local_claimer: evt rebuild failed for seq={seq}: {e}")
    return evt


async def _execute_claimed_locally(task: dict, rid: int):
    """认领后的本机执行：复用方案B已验证的 direct 执行通道。

    task 是已被 claim_by_tag 更新为 running + runtime_id=rid 的现有任务行
    （不另起新行）；状态流转与 worker 路径一致 queued→running→completed。
    """
    from src.api.event_pipeline import _execute_agent, start_direct_agent
    from src.runtime.engine_select import engine_name
    from src.runtime.instances import runtime_instance_name

    evt = _rebuild_evt_from_task(task)
    backend = engine_name(rid) or runtime_instance_name(rid) or ""
    seq = task.get("event_seq") or 0
    tid = task.get("id") or ""
    log.info(f"local_claimer: executing task={tid} seq={seq} agent={task.get('agent_type')} "
             f"engine={rid}({runtime_instance_name(rid)})")
    # claim_by_tag 是同步 DB 操作（已在线程里跑过）；执行走 _DIRECT_POOL 线程池
    await asyncio.to_thread(
        start_direct_agent, _execute_agent,
        (task.get("repo_key") or "", seq, task.get("agent_type") or "", backend,
         task.get("skill") or "", evt, rid, tid))


async def local_claimer_loop():
    """本机 direct 引擎认领主循环——语义与 worker 的 claim 循环对齐。"""
    log.info("Local claimer loop started (3s interval) — local direct engines now "
             "first-class broadcast claimers (see local-claimer-first-class-broadcast-design.md)")
    while True:
        try:
            from src.runtime.service import claim_by_tag
            for rid in _local_direct_engine_ids():
                if _engine_busy(rid):
                    continue
                claimed = None
                for tag in _engine_tags(rid):
                    # 与远端 worker 完全相同的认领入口（含白名单/健康分/满载/守卫全套检查）
                    task = await asyncio.to_thread(claim_by_tag, tag, rid)
                    if task:
                        claimed = task
                        break
                if claimed:
                    await _execute_claimed_locally(claimed, rid)
                    break  # 本轮已领到任务，下轮再继续（与 worker 单任务循环节奏一致）
        except asyncio.CancelledError:
            log.info("Local claimer loop cancelled")
            raise
        except Exception as e:
            log.warning(f"local_claimer loop error: {e}")
        await asyncio.sleep(CLAIM_INTERVAL + random.uniform(0, 1))


def start_local_claimer():
    """幂等启动（双 lifespan 调用安全）。返回 task 或 None。"""
    if not CLAIMER_ENABLED:
        log.info("Local claimer disabled (GRAPE_LOCAL_CLAIMER=0) — local direct engines "
                 "do NOT participate in broadcast claiming")
        return None
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        log.warning("local_claimer: no running event loop, not started")
        return None
    # 幂等：已存在同协程任务则不重复启动
    for t in asyncio.all_tasks(loop):
        coro = t.get_coro()
        if coro is not None and getattr(coro, "__qualname__", "") == local_claimer_loop.__qualname__ and not t.done():
            return t
    task = loop.create_task(local_claimer_loop(), name="local-claimer")
    return task
