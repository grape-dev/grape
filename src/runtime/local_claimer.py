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
# 本机直连兜底延迟（local-claimer-backstop-design）：任务入队后先给远端 worker 一个
# 专属认领窗口（worker 3s 轮询 + 0~0.9s jitter，15s ≈ 4-5 轮机会），窗口内本机不抢；
# 超时后本机作为兜底认领。例外：任务白名单内的远端引擎已全部物理不可达（离线/心跳
# 过期/无 tag/scope 不符）时跳过等待立即兜底（空等无意义）。
# GRAPE_LOCAL_CLAIMER_DELAY=0 可关闭延迟（恢复立即抢的旧行为）。
try:
    CLAIM_BACKSTOP_DELAY = float(os.environ.get("GRAPE_LOCAL_CLAIMER_DELAY", "15"))
except ValueError:
    CLAIM_BACKSTOP_DELAY = 15.0


def _claimer_enabled() -> bool:
    """循环内实时读取开关（QA 审查建议：支持运行态热切换，无需 reload/restart）。"""
    return os.environ.get("GRAPE_LOCAL_CLAIMER", "1").strip().lower() in ("1", "true", "yes", "on")


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


def _task_age_seconds(task: dict) -> float:
    """任务已排队时长（秒）。created_at 为 UTC ISO 格式；解析失败返回大值（视为已超龄，不阻塞兜底）。"""
    created = (task.get("created_at") or "").strip()
    if not created:
        return CLAIM_BACKSTOP_DELAY + 1.0
    try:
        from src.runtime.health import parse_dt
        ts = parse_dt(created)
        if ts is not None:
            import time as _time
            return max(0.0, _time.time() - ts)
    except Exception:
        pass
    return CLAIM_BACKSTOP_DELAY + 1.0


def _remote_claimants_reachable(task: dict) -> bool:
    """该任务还有没有物理可达的远端认领方。

    - pool_engine_ids 非空 → 检查名单内（含本机直连之外的）引擎是否有任一可达
      （复用 claim_by_tag 同款尺子 _check_pool_engines_reachable：在线+心跳新鲜+
      healthy+probe 正常+有 tag 能力+scope 匹配）。全不可达 → False（本机可跳过
      兜底延迟立即认领——空等无意义）。
    - pool_engine_ids 为空（全广播）→ 查全站是否有任一在线的远端 worker 引擎
      （credential_id>0 且心跳新鲜）。有 → 本机继续等待让远端先领；无 → False。
    查询异常保守返回 True（继续等待，宁可慢不可抢跑）。
    """
    import json as _json
    from src.session_manager import db, db_lock
    _tag = (task.get("pool_tag") or task.get("agent_type") or "").strip()
    _repo = task.get("repo_key") or ""
    _uid = task.get("user_id") or 0
    _pe = (task.get("pool_engine_ids") or "").strip()
    try:
        with db_lock:
            c = db.cursor()
            if _pe:
                try:
                    allowed = _json.loads(_pe)
                except Exception:
                    return True
                # 名单里只剩本机直连引擎 → 远端已无候选
                remote_ids = [e for e in allowed if e and int(e) not in _local_direct_engine_ids()]
                if not remote_ids:
                    return False
                from src.runtime.service import _check_pool_engines_reachable
                # 只需远端中任一可达；本机自身可达与否不影响"是否等待"的判断
                return bool(_check_pool_engines_reachable(remote_ids, _tag, _repo, task_user_id=int(_uid)))
            # 全广播：全站有没有在线远端 worker（心跳新鲜）
            c.execute(
                "SELECT COUNT(*) FROM agent_runtimes ar "
                "LEFT JOIN runtime_credentials rc ON rc.id=ar.credential_id "
                "WHERE ar.credential_id>0 AND ar.status='online' AND ar.healthy=1 "
                "AND ar.last_seen_at IS NOT NULL AND ar.last_seen_at!=''")
            n = c.fetchone()
            if n and (n[0] or 0) > 0:
                # 有在线远端，但心跳新鲜度逐行判断（SQL 表达复杂，逐行复用 parse_dt）
                c.execute(
                    "SELECT ar.id, ar.last_seen_at FROM agent_runtimes ar "
                    "LEFT JOIN runtime_credentials rc ON rc.id=ar.credential_id "
                    "WHERE ar.credential_id>0 AND ar.status='online' AND ar.healthy=1 "
                    "AND ar.last_seen_at IS NOT NULL AND ar.last_seen_at!='' LIMIT 20")
                import time as _time
                from src.runtime.health import parse_dt
                from src.runtime.service import _RUNTIME_STALE_SECONDS
                for r in c.fetchall():
                    ts = parse_dt(r[1] or "")
                    if ts is None or _time.time() - ts <= _RUNTIME_STALE_SECONDS:
                        return True
                return False
            return False
    except Exception as e:
        log.warning(f"local_claimer: remote reachability check failed: {e}")
        return True


def _backstop_should_yield(task: dict) -> bool:
    """兜底守卫判定：本机这次认领是否应当让位给远端 worker。

    返回 True = 让位（回滚任务为 queued，本轮不执行）。条件：
    1. 延迟开关未关（CLAIM_BACKSTOP_DELAY > 0）；
    2. 任务年龄仍在窗口期内；
    3. 仍有物理可达的远端认领方（全广播看全站远端，白名单模式看名单内远端）。
    例外放行：远端引擎已全部判死/无权限 → 不空等，立即由本机兜底执行。
    """
    if CLAIM_BACKSTOP_DELAY <= 0:
        return False
    if _task_age_seconds(task) >= CLAIM_BACKSTOP_DELAY:
        return False
    return _remote_claimants_reachable(task)


def _engine_busy(runtime_id: int) -> bool:
    """引擎满载检查（真实 running 数 >= max_concurrent → 跳过，满载不抢）。

    方案 D（local-engine-busy-slots-zombie-fix-design.md §十）：以 agent_tasks 真实
    running/dispatched 数（covering 索引 idx_agent_tasks_runtime_status 支撑）为唯一事实源，
    不再读 busy_slots 列——busy_slots 为展示快照，残留僵尸值不阻塞认领。
    """
    try:
        from src.session_manager import db, db_lock
        with db_lock:
            c = db.cursor()
            c.execute(
                "SELECT COUNT(*) FROM agent_tasks "
                "WHERE runtime_id=? AND status IN ('running','dispatched')", (runtime_id,))
            r = c.fetchone()
            c.execute("SELECT max_concurrent FROM agent_runtimes WHERE id=?", (runtime_id,))
            m = c.fetchone()
        if m and m[0]:
            return (r[0] or 0) >= (m[0] or 0)
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
    _disabled_logged = False
    while True:
        # 热切换：循环内实时读取 GRAPE_LOCAL_CLAIMER（关闭即时停抢，重新开启即时恢复）
        if not _claimer_enabled():
            if not _disabled_logged:
                log.info("Local claimer disabled at runtime (GRAPE_LOCAL_CLAIMER=0) — "
                         "local direct engines stop claiming until re-enabled")
                _disabled_logged = True
            await asyncio.sleep(CLAIM_INTERVAL)
            continue
        _disabled_logged = False
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
                    # 兜底守卫（local-claimer-backstop-design）：窗口期内且仍有可达的
                    # 远端认领方 → 放弃本次认领，让远端 worker 先拿。
                    # 放弃方式：把任务状态回滚 queued（claim 已把它置为 running 并占用本机
                    # runtime_id），恢复为无主池任务，等下一轮再评估。
                    if _backstop_should_yield(claimed):
                        try:
                            from src.session_manager import db, db_lock
                            _yid = claimed.get("id") or ""
                            _yseq = claimed.get("event_seq") or 0
                            with db_lock:
                                c = db.cursor()
                                # 回滚任务为无主池任务（claim 已置 running + 占用本机 runtime_id）
                                c.execute(
                                    "UPDATE agent_tasks SET status='queued', runtime_id=0, started_at=NULL "
                                    "WHERE id=? AND status='running'", (_yid,))
                                # 还原 claim 的副作用：事件回 pending、runtime 清空（留给真正认领方回写）
                                if _yseq:
                                    c.execute(
                                        "UPDATE events SET status='pending', runtime='' "
                                        "WHERE seq=? AND status='running'", (_yseq,))
                                # busy_slots 是展示快照，方案 D 有 recalc/兜底，无需精确还原
                                db.commit()
                            log.info(f"local_claimer: backstop yielded task={_yid} "
                                     f"(age={_task_age_seconds(claimed):.0f}s < {CLAIM_BACKSTOP_DELAY:.0f}s, "
                                     f"remote claimant reachable)")
                        except Exception as _ye:
                            log.warning(f"local_claimer: backstop yield failed: {_ye}")
                        continue
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
