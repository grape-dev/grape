"""服务端 RuntimeService：任务队列（确定路由 + 串行槽）与远端 worker 执行。

见 agent-runtime-event-tech-impl.md §1.3 / concurrency-design.md §2.2-2.4 / engine-selection-design.md v2。
执行路径：业务层 enqueue_task → agent_tasks(status=queued) → 远端 RuntimeAgent claim（原子抢单+D9）
→ stream_runtime → report/complete/fail。本机引擎走 direct（`_execute_agent`），不走队列。
"""
import os, json, re, threading, time, uuid
from datetime import datetime, timezone, timedelta
from src.session_manager import db, db_lock
from src.config import log
from src.api.time_utils import normalize_event_time
from src.runtime.health import apply_runtime_state  # Issue #1496 在线态统一写入口
from src.token_service import is_auth_401_error


# R11.4: _get_task_token 抽取到 src/user/db.py，统一调用
from src.user.db import get_task_token as _get_task_token

# R16.2.1: 统一 error_code → message 映射（task errors / dashboard 展示用）
_R16_ERROR_MESSAGES = {
    "TOKEN_EXPIRED": "user token expired or revoked",
    "GLOBAL_TOKEN_EXPIRED": "global bot token expired or revoked",
    "REPO_TOKEN_EXPIRED": "repo bot token expired or revoked",
    "SESSION_TOKEN_EXPIRED": "devops agent session token expired or revoked",
    "NO_TOKEN_AVAILABLE": "no token available for this repo",
}

# ===== 执行模式开关（engine-selection-design.md v2）=====
# auto（按绑定引擎宿主自动推导，见 execution-mode-auto-routing-design.md §3）
# / direct（本机就地 stream_agent）/ queue_remote（远端 RuntimeAgent）。queue_local 已废除。
EXEC_MODE_AUTO = "auto"
EXEC_MODE_DIRECT = "direct"
EXEC_MODE_REMOTE = "queue_remote"

_DEFAULT_MODE = EXEC_MODE_AUTO


def _check_pool_engines_reachable(engine_ids: list, tag: str, repo_key: str = "", task_user_id: int = 0) -> bool:
    """检查 pool_engine_ids 中的引擎是否有任何一个能真正认领该 tag 的任务。

    检查维度：在线 + healthy + probe_status + 心跳新鲜（Issue #259）+ 有 tag 能力 + scope 匹配。
    返回 True = 池里有物理可达的引擎，应保留绑定并排队等待容量。
    返回 False = 池里全不可达，由调用方诚实失败，不能放行池外引擎。
    并发容量由实际认领检查；满槽是暂时忙碌，不是不可达（Issue #306）。

    Issue #134 扩展：健康分维度不在此函数判定——健康分低但物理可达的引擎
    仍然算 reachable（不清空 pool），claim_by_tag 中的健康分检查会阻止它们认领。
    防饿死由 _check_stale_queued_tasks 超时降级处理。

    Lock contract（Issue #259 Optional）：调用方必须已持有 db_lock。
    本函数不再嵌套 with db_lock——生产 db_lock=RLock（可重入，嵌套无碍），
    但测试 monkeypatch 裸 Lock（非重入）时嵌套获取会自死锁。
    所有调用点（claim / claim_by_tag / _check_stale_queued_tasks）均在 with db_lock 块内调用。
    """
    from src.repo_key import from_repo_key
    _repo_norm = (from_repo_key(repo_key) or "").strip()
    for eid in engine_ids:
        try:
            c = db.cursor()
            # 健康判定统一走 src.runtime.health.engine_health_ok（Issue #1487：认领侧与派发侧
            # 必须同一把尺子，禁止各自复制判定——否则派发说健康、认领说失联，任务入队即判死）
            c.execute(
                "SELECT status, healthy, probe_status, last_seen_at, credential_id, installed "
                "FROM agent_runtimes WHERE id=?", (eid,))
            r = c.fetchone()
            if not r:
                continue
            _cred = r[4] or 0
            from src.runtime.health import engine_health_ok as _health_ok_fn
            if not _health_ok_fn(_cred, r[0], r[1], r[5], r[3],
                                 stale_seconds=_RUNTIME_STALE_SECONDS):
                continue
            if _cred:
                # 认领侧对远端的附加关（比派发侧更严，既有口径，Issue #1487 未改）：
                # probe 失败 = 引擎跑不了活，即使心跳新鲜也不认领
                _probe = r[2] or ""
                if _probe not in ("ok", "pending", ""):
                    continue
            # 有 tag 能力？
            c.execute(
                "SELECT 1 FROM engine_capabilities WHERE runtime_id=? AND enabled=1 "
                "AND (tag=? OR tag='all') LIMIT 1", (eid, tag))
            if not c.fetchone():
                continue
            # scope 匹配？
            c.execute(
                "SELECT rc.scope, rc.owner_id, rc.owner_name FROM agent_runtimes r "
                "LEFT JOIN runtime_credentials rc ON rc.id=r.credential_id WHERE r.id=?", (eid,))
            sr = c.fetchone()
            if sr:
                _scope = sr[0] or "global"
                _owner = sr[1] or ""
                _owner_name = sr[2] or ""
                if _scope == "repo" and _owner:
                    _owner_norm = (from_repo_key(_owner) or "").strip()
                    if _repo_norm and _owner_norm != _repo_norm:
                        continue  # repo scope 但仓库不匹配
                elif _scope == "user" and task_user_id > 0 and _owner_name:
                    # user scope：引擎 owner_name 对应的 user_id 必须匹配 task.user_id
                    try:
                        c.execute("SELECT id FROM grape_accounts WHERE username=? AND id>0 LIMIT 1", (_owner_name,))
                        _ur = c.fetchone()
                        _engine_uid = _ur[0] if _ur else 0
                        if _engine_uid != task_user_id:
                            continue  # user scope 但用户不匹配
                    except Exception:
                        pass
            # 全部通过：有能干的引擎（物理可达）
            return True
        except Exception:
            continue
    return False




def _sync_event_failed(seq, reason: str, exit_code: int = 1, completed_at: str = ""):
    """任务判死 → 回写关联事件完整终态（Issue #369）。

    对齐 event_pipeline._fail_event 语义：status=failed + result=reason + exit_code +
    completed_at，事件侧具备完整诊断信息（此前判死路径只调 update_event_status，
    事件 result/exit_code/completed_at 全空，排障零信息）。
    幂等：update_event_complete 自带终态保护（terminal→terminal 跳过），
    双主 scp 同步场景下两侧重复回写不冲突、不覆盖。
    """
    if not seq:
        return
    try:
        from src.api.event_store import update_event_complete
        update_event_complete(
            seq, "failed", exit_code=exit_code,
            completed_at=completed_at or datetime.now(timezone.utc).isoformat(),
            result=(reason or "")[:200])
    except Exception as _e:
        log.warning(f"_sync_event_failed failed for seq={seq}: {_e}")


def _classify_pool_unreachable_reasons(c, engine_ids: list, pool_tag: str = "", repo_key: str = "",
                                       task_user_id: int = 0) -> dict:
    """Issue #1487 改动D：逐引擎归类不可达原因，供失败文案分类展示。

    分类（互斥，取首个命中）：
    - offline：远端 status!=online / healthy!=1 / probe 异常；cred0 installed=0 或 healthy=0
    - stale_heartbeat：远端 last_seen_at 过期或为空（cred0 豁免心跳，不会落此桶）
    - no_tag：无 pool_tag 对应能力
    - scope_mismatch：repo/user scope 不匹配
    Lock contract：调用方已持有 db_lock。
    """
    from src.repo_key import from_repo_key as _frk
    _buckets = {"offline": [], "stale_heartbeat": [], "no_tag": [], "scope_mismatch": []}
    _repo_norm = (_frk(repo_key) or "").strip()
    for _eid in engine_ids:
        try:
            c.execute(
                "SELECT name, status, healthy, probe_status, last_seen_at, credential_id, installed "
                "FROM agent_runtimes WHERE id=?", (int(_eid),))
            _r = c.fetchone()
            if not _r:
                _buckets["offline"].append(str(_eid))
                continue
            _name = _r[0] or str(_eid)
            _cred = _r[5] or 0
            from src.runtime.health import engine_health_ok as _health_ok_fn
            if not _health_ok_fn(_cred, _r[1], _r[2], _r[6], _r[4],
                                 stale_seconds=_RUNTIME_STALE_SECONDS):
                # 归因（与 _check_pool_engines_reachable 同一把尺子，仅用于文案分类）：
                # 本机直连 / 远端不在线 / 远端 healthy=0 或 probe 异常 → 离线；
                # 远端在线且 healthy 正常，只是心跳过期/缺失 → 心跳过期
                if _cred == 0 or (_r[1] or "") not in ("online", "active"):
                    _buckets["offline"].append(_name)
                elif (_r[2] if _r[2] is not None else 0) != 1 \
                        or (_r[3] or "") not in ("ok", "pending", ""):
                    _buckets["offline"].append(_name)
                else:
                    _buckets["stale_heartbeat"].append(_name)
                continue
            # 有标签能力？
            c.execute(
                "SELECT 1 FROM engine_capabilities WHERE runtime_id=? AND enabled=1 "
                "AND (tag=? OR tag='all') LIMIT 1", (int(_eid), pool_tag))
            if not c.fetchone():
                _buckets["no_tag"].append(_name)
                continue
            # scope 匹配？
            c.execute(
                "SELECT rc.scope, rc.owner_id, rc.owner_name FROM agent_runtimes r "
                "LEFT JOIN runtime_credentials rc ON rc.id=r.credential_id WHERE r.id=?", (int(_eid),))
            _sr = c.fetchone()
            if _sr:
                _scope = _sr[0] or "global"
                _owner = _sr[1] or ""
                _owner_name = _sr[2] or ""
                if _scope == "repo" and _owner:
                    _owner_norm = (_frk(_owner) or "").strip()
                    if _repo_norm and _owner_norm != _repo_norm:
                        _buckets["scope_mismatch"].append(_name)
                        continue
                elif _scope == "user" and task_user_id > 0 and _owner_name:
                    try:
                        c.execute("SELECT id FROM grape_accounts WHERE username=? AND id>0 LIMIT 1", (_owner_name,))
                        _ur = c.fetchone()
                        _engine_uid = _ur[0] if _ur else 0
                        if _engine_uid != task_user_id:
                            _buckets["scope_mismatch"].append(_name)
                            continue
                    except Exception:
                        pass
            # 兜底：逐项未命中 → 归 offline（不应发生，防御分类漏网）
            _buckets["offline"].append(_name)
        except Exception:
            _buckets["offline"].append(str(_eid))
    return _buckets


def _fail_task_pool_unreachable(c, task_id: str, engine_ids: list, pool_tag: str = "",
                                repo_key: str = "", task_user_id: int = 0, note: str = ""):
    """Issue #253 改动2：绑定引擎全部物理不可达 → 任务诚实失败（宁失败，不跑飞）。

    claim / claim_by_tag 认领时发现 pool_engine_ids 内引擎全部不可达（离线/心跳过期/
    无 tag/scope 不符）时调用。满槽继续排队。替代原"清空 pool_engine_ids 放行全站"的降级。

    Issue #1487 改动D：文案按引擎归类四类原因（离线/心跳过期/无对应标签能力/scope 不匹配）。

    Lock contract：调用方必须已持有 db_lock（与 _check_pool_engines_reachable 相同）。
    c 为调用方传入的已持有锁事务的 cursor；本函数内部 commit 并同步事件终态。
    """
    from datetime import datetime, timezone
    _buckets = _classify_pool_unreachable_reasons(c, engine_ids, pool_tag, repo_key, task_user_id)
    _parts = []
    if _buckets["offline"]:
        _parts.append(f"离线（{', '.join(_buckets['offline'])}）")
    if _buckets["stale_heartbeat"]:
        _parts.append(f"心跳过期（{', '.join(_buckets['stale_heartbeat'])}）")
    if _buckets["no_tag"]:
        _parts.append(f"无对应标签能力（{', '.join(_buckets['no_tag'])}）")
    if _buckets["scope_mismatch"]:
        _parts.append(f"scope 不匹配（{', '.join(_buckets['scope_mismatch'])}）")
    _reason_detail = '；'.join(_parts) or '未知原因'
    _now = datetime.now(timezone.utc).isoformat()
    _reason = (f"绑定的 {len(engine_ids)} 台引擎全部不可达，任务停止派发"
               f"{'，标签=' + pool_tag if pool_tag else ''}：{_reason_detail}（Issue #253）")
    if note:
        _reason += f"；{note}"
    _fail_task_with_reason(c, task_id, _reason, _now,
                           log_msg=f"task {task_id} failed: pool engines {engine_ids} all unreachable (Issue #253)")


def _fail_task_with_reason(c, task_id: str, reason: str, now_iso: str = "", log_msg: str = ""):
    """把 queued 任务写成 failed + 同步事件终态（Issue #253/#1487 共用内核）。

    Lock contract：调用方已持有 db_lock。
    """
    from datetime import datetime, timezone
    _now = now_iso or datetime.now(timezone.utc).isoformat()
    try:
        c.execute(
            "UPDATE agent_tasks SET status='failed', result_text='', errors=?, "
            "completed_at=?, stream_ended=1, exit_code=COALESCE(exit_code, 1) "
            "WHERE id=? AND status='queued'",
            (reason, _now, task_id))
        if c.rowcount:
            db.commit()
            log.warning(log_msg or f"task {task_id} failed: {reason[:120]}")
            # 同步事件终态（模式同回收逻辑：防事件永远 running）；
            # Issue #369：走 _sync_event_failed 完整回写 result/exit_code/completed_at
            c.execute("SELECT event_seq FROM agent_tasks WHERE id=?", (task_id,))
            _es = c.fetchone()
            if _es and _es[0]:
                _sync_event_failed(_es[0], reason, completed_at=_now)
    except Exception as _e:
        log.warning(f"_fail_task_with_reason failed for task {task_id}: {_e}")
        try:
            db.rollback()
        except Exception:
            pass


def _fail_task_pool_unclaimed(c, task_id: str, engine_ids: list, pool_tag: str = "",
                             waited_seconds: int = 0, note: str = ""):
    """Issue #1487 改动C 闭环：池内引擎可达但超过硬窗口仍无人认领 → 诚实失败。

    与 _fail_task_pool_unreachable 的区别：**引擎是好的**，只是没有引擎来领
    （引擎进程没跑、被更高优先级任务占满、或本机 claimer 未覆盖该 tag）。
    因此不能复用「全部不可达 + 四分类」文案——那会把可达引擎误报成离线。
    """
    from datetime import datetime, timezone
    _names = []
    for _eid in engine_ids:
        try:
            c.execute("SELECT name FROM agent_runtimes WHERE id=?", (int(_eid),))
            _r = c.fetchone()
            if _r and _r[0]:
                _names.append(_r[0])
        except Exception:
            pass
    _label = ', '.join(_names) or ', '.join(str(x) for x in engine_ids)
    _reason = (f"绑定的 {len(engine_ids)} 台引擎（{_label}）在 {waited_seconds}s 内未被认领，"
               f"任务停止派发{'，标签=' + pool_tag if pool_tag else ''}（Issue #1487 排队超时）")
    if note:
        _reason += f"；{note}"
    _fail_task_with_reason(c, task_id, _reason, "",
                           log_msg=f"task {task_id} failed: bound engines {engine_ids} never claimed "
                                   f"within {waited_seconds}s (Issue #1487)")


def _fail_task_pool_unhealthy(c, task_id: str, engine_ids: list, waited_seconds: int):
    """Issue #253 改动3（方案 A）：绑定引擎物理可达但健康分不足 → 限时等待超时后诚实失败。

    _check_stale_queued_tasks 在 120s 窗口耗尽时调用。替代原"无限等待"死区
    （引擎活着但低分：健康分门禁拦它认领 + 可达即不撕证 = 永久 queued）。

    Lock contract：同 _fail_task_pool_unreachable，调用方已持有 db_lock。
    """
    from datetime import datetime, timezone
    _names = []
    try:
        for _eid in engine_ids:
            c.execute("SELECT name FROM agent_runtimes WHERE id=?", (int(_eid),))
            _r = c.fetchone()
            if _r and _r[0]:
                _names.append(_r[0])
    except Exception:
        pass
    _now = datetime.now(timezone.utc).isoformat()
    _name_str = ', '.join(_names) or ', '.join(str(x) for x in engine_ids)
    _reason = (f"绑定的 {len(engine_ids)} 台引擎（{_name_str}）在线但健康分不足，"
               f"等待 {waited_seconds}s 未恢复，任务停止派发（Issue #253）")
    try:
        c.execute(
            "UPDATE agent_tasks SET status='failed', result_text='', errors=?, "
            "completed_at=?, stream_ended=1, exit_code=COALESCE(exit_code, 1) "
            "WHERE id=? AND status='queued'",
            (_reason, _now, task_id))
        if c.rowcount:
            db.commit()
            log.warning(f"task {task_id} failed: pool engines {engine_ids} unhealthy "
                        f"after {waited_seconds}s wait (Issue #253)")
            c.execute("SELECT event_seq FROM agent_tasks WHERE id=?", (task_id,))
            _es = c.fetchone()
            if _es and _es[0]:
                # Issue #369：完整回写事件诊断字段（result/exit_code/completed_at）
                _sync_event_failed(_es[0], _reason, completed_at=_now)
    except Exception as _e:
        log.warning(f"_fail_task_pool_unhealthy failed for task {task_id}: {_e}")
        try:
            db.rollback()
        except Exception:
            pass


def _check_pool_has_healthy(engine_ids: list) -> bool:
    """检查 pool_engine_ids 中是否有健康分达标的引擎（Issue #134 扩展）。

    与 _check_pool_engines_reachable 的区别：
    - reachable 只看物理可达性（在线/能力/scope/槽位），不看健康分
    - has_healthy 看健康分，用于防饿死降级判定

    返回 True = 池中有健康引擎，低分引擎应被 claim_by_tag 挡住
    返回 False = 池中全不健康，应触发超时降级（_check_stale_queued_tasks）
    """
    try:
        from src.runtime.health_scorer import is_healthy as _is_engine_healthy
    except Exception:
        return True  # 模块异常时不影响现有逻辑
    for eid in engine_ids:
        try:
            if _is_engine_healthy(eid):
                return True
        except Exception:
            continue
    return False


def get_execution_mode() -> str:
    """返回当前执行模式：env 优先，其次 meta 表。"""
    _m = os.environ.get("DEVOPS_EXECUTION_MODE", "")
    if _m:
        return _m
    try:
        from src.user.db import get_meta_value
        _m = get_meta_value("execution_mode") or ""
    except Exception:
        pass
    return _m or _DEFAULT_MODE


def set_execution_mode(mode: str):
    """设置执行模式（落 meta 表，运维可改键值；读取实时生效，无需重启）。"""
    try:
        from src.user.db import set_meta_value
        set_meta_value("execution_mode", mode)
        log.info(f"execution_mode set to {mode}")
    except Exception as e:
        log.warning(f"set_execution_mode failed: {e}")


# ===== 按绑定引擎宿主自动推导执行通道（execution-mode-auto-routing-design.md §3.1/§3.6）=====

def runtime_credential_id(runtime_id: int):
    """按 runtime_id 反查引擎宿主 credential_id；查不到返回 None。

    credential_id=0 → 本机引擎（服务器装着该 CLI）；>0 → 远端 worker 承载的引擎。
    """
    if not runtime_id:
        return None
    try:
        with db_lock:
            c = db.cursor()
            c.execute("SELECT credential_id FROM agent_runtimes WHERE id=?", (runtime_id,))
            row = c.fetchone()
        return (row[0] or 0) if row else None
    except Exception:
        return None


_RUNTIME_STALE_SECONDS = int(os.environ.get("RUNTIME_STALE_SECONDS", "100"))

# Issue #259：running 任务回收阈值——对齐 worker 本地 RUNTIME_TASK_TIMEOUT（默认 3600s）。
# 假死 worker（断连但本地任务还在跑）的 running 任务须等本地超时自杀后才服务端重派，
# 双跑窗口=0。前提：该值 >= worker 端 RUNTIME_TASK_TIMEOUT（部署注意）。
# Issue #470 后续：回收阈值改为 get_task_recover_stale_seconds() 动态取值——
# env TASK_RECOVER_STALE_SECONDS 显式覆盖 > 跟随管理台 runtime_task_timeout 实时值。
# 启动期 env 覆盖仅用于耦合告警；回收循环（_reap_stale_loop）每次执行实时取值。
from src.config import get_task_recover_stale_seconds as _get_recover_stale_cfg
_TASK_RECOVER_STALE_SECONDS = _get_recover_stale_cfg()


def _recover_cutoff_for_agent(agent_type: str) -> str:
    """Issue #516 M1：running 任务回收阈值按 agent_type 超时对齐。

    worker 侧超时已配置化（coding 默认 7200s，见 src/runtime/task_timeout.py），
    服务端若仍用固定 3600s 阈值回收 running 任务，会在 worker 本地仍在执行时
    重派 → 双跑。此处与 worker 同源解析该 agent 的超时上限，再加固定余量
    （对齐旧部署约定 TASK_RECOVER_STALE_SECONDS >= worker 超时）。
    解析失败回退旧固定阈值（行为不回退）。
    """
    _grace = max(_TASK_RECOVER_STALE_SECONDS - _WORKER_TASK_TIMEOUT_DEFAULT, 0)
    try:
        from src.runtime.task_timeout import resolve_task_timeout
        _to, _src = resolve_task_timeout({"agent_type": (agent_type or "").strip().lower()})
        return (datetime.now(timezone.utc) - timedelta(seconds=_to + _grace)).isoformat()
    except Exception:
        return (datetime.now(timezone.utc) - timedelta(seconds=_TASK_RECOVER_STALE_SECONDS)).isoformat()

# Issue #259 Optional：启动期配置耦合告警——防双跑前提是 TASK_RECOVER_STALE_SECONDS >= worker RUNTIME_TASK_TIMEOUT。
# 二者默认均 3600s（安全），但部署侧单边调大 RUNTIME_TASK_TIMEOUT 而不调 TASK_RECOVER_STALE_SECONDS
# 会导致服务端在 worker 仍本地执行时重派 → 双跑。启动期告警一次，运行期不再重复。
# Issue #470：改读统一配置 getter（env > 管理台 meta > runtime-config.json > 默认 3600）。
from src.config import get_runtime_task_timeout as _get_runtime_task_timeout_cfg
_WORKER_TASK_TIMEOUT_DEFAULT = _get_runtime_task_timeout_cfg()
if _TASK_RECOVER_STALE_SECONDS < _WORKER_TASK_TIMEOUT_DEFAULT:
    log.warning(
        f"Config coupling risk: TASK_RECOVER_STALE_SECONDS={_TASK_RECOVER_STALE_SECONDS}s < "
        f"RUNTIME_TASK_TIMEOUT={_WORKER_TASK_TIMEOUT_DEFAULT}s — running tasks may be re-dispatched "
        f"before worker local timeout, causing double execution (Issue #259)"
    )


def _runtime_timeout_env_kv() -> dict:
    """Issue #470：统一超时配置下发给 worker 的 env 键值。

    服务端 enqueue_task 时以此 setdefault 进 env_extra（JSON 列），worker 端
    _execute_task env.update(env_extra) 后：
      - 看门狗 _watchdog_timeout_for(env) 任务级优先
      - hermes/hwcloud _global_timeout_for(max_exec, env) 任务级优先
    worker 本地 os.environ 显式设置仍最高优（优先级语义见 src/config.py）。
    """
    return {"RUNTIME_TASK_TIMEOUT": str(_get_runtime_task_timeout_cfg())}


def _remote_runtime_ready(runtime_id: int) -> bool:
    """远端引擎是否可用：引擎 status=online、last_seen 新鲜、凭据未吊销。

    心跳间隔 15s，失联 > _RUNTIME_STALE_SECONDS 视为不可用（防 auto 模式任务卡 queued）。
    """
    try:
        from src.runtime.health import parse_dt
        now = time.time()
        with db_lock:
            c = db.cursor()
            c.execute("SELECT status, last_seen_at, credential_id FROM agent_runtimes WHERE id=?", (runtime_id,))
            row = c.fetchone()
        if not row:
            return False
        engine_status, last_seen_at, cid = row
        if (engine_status or "") != "online":
            return False
        ts = parse_dt(last_seen_at)
        if ts is not None and now - ts > _RUNTIME_STALE_SECONDS:
            return False
        if cid:
            with db_lock:
                c = db.cursor()
                c.execute("SELECT status FROM runtime_credentials WHERE id=?", (cid,))
                cred = c.fetchone()
            if cred and cred[0] != "active":
                return False
        return True
    except Exception as e:
        log.warning(f"runtime_execution_kind: health check failed: {e}")
        return False


def runtime_execution_kind(runtime_id: int) -> str:
    """返回该任务的有效执行通道（auto 语义下的推导 / 强制模式直接生效）：

    - 全局显式 direct/queue_remote → 直接返回该强制值（向后兼容；历史遗留
      queue_local 已废除，归为 direct 本机执行）
    - auto 下按绑定引擎宿主推导：
      - credential_id == 0  → 本机引擎（服务器装着的 CLI）        → EXEC_MODE_DIRECT
      - credential_id >  0  → 远端 worker 承载的引擎             → EXEC_MODE_REMOTE
      - runtime_id=0 / 查不到（未绑定）→ EXEC_MODE_DIRECT（本机默认，见 §3.6）
    - auto 下远端引擎失联/凭据吊销 → 降级本机 direct 兜底（避免任务卡 queued）
    """
    mode = get_execution_mode()
    if mode != EXEC_MODE_AUTO:
        if mode == "queue_local":      # queue_local 已废除（无本地 worker），按本机 direct 处理
            return EXEC_MODE_DIRECT
        return mode
    cid = runtime_credential_id(runtime_id)
    if cid is None:
        return EXEC_MODE_DIRECT
    if cid > 0:
        if _remote_runtime_ready(runtime_id):
            return EXEC_MODE_REMOTE
        log.warning(f"runtime_execution_kind: runtime_id={runtime_id} 远端引擎不可用，降级本机 direct")
        return EXEC_MODE_DIRECT
    return EXEC_MODE_DIRECT

_TASK_COLUMNS = (
    "id, agent_session_id, runtime_id, event_seq, issue_id, repo_key, agent_type, skill, "
    "skill_id, "
    "prompt, cwd, runtime_session_id, event_type, account, user_id, repo_gitcode_token, "
    "user_gitcode_token, "
    "env_extra, skill_files, server_url, issue_session_id, project_info_files, project_info_meta, "
    "knowledge_files, project_info_file_urls, knowledge_file_urls, attempts, pool_agent_type, affinity_runtime_id, pool_tag, pool_engine_ids, "
    "pool_engine_source, "
    "status, stream_ended, self_reported, pending_since, "
    "exit_code, result_text, errors, stderr, "
    "usage_json, started_at, completed_at, created_at"
)

# Issue #537：任务状态机口径（集中定义，防字面量漂移）。
# - pending_self_report：主控停机/重启时 REMOTE（worker 承载）任务的中间态——
#   进程活在 worker 上，等待其自报终态（complete_task/fail_task 正常转换）。
# - timeout_failed：pending_self_report 超时（默认 30min）无自报的**带说明终态**，
#   区别于 plain failed（下游翻译时须附超时原因）。
TASK_TERMINAL_STATUSES = ("completed", "failed", "cancelled", "timeout_failed")
TASK_ACTIVE_STATUSES = ("queued", "dispatched", "running", "pending_self_report")
TASK_PENDING_SELF_REPORT = "pending_self_report"
TASK_TIMEOUT_FAILED = "timeout_failed"

_TASK_COLUMNS_LIST = [c.strip() for c in _TASK_COLUMNS.split(",")]

# 双标记列：INSERT 时让 DB 默认（0）落地，select 时按表实际列过滤（兼容未迁移库）
_TASK_SIGNAL_COLUMNS = ("stream_ended", "self_reported")

_TABLE_COLS_CACHE: dict = {}
_TABLE_COLS_CACHE_DB_ID = id(db)   # db 对象身份标记（测试 monkeypatch 换库 → 缓存自动失效）


# Issue #605：引擎不在 pool_engine_ids 名单 → 认领静默跳过，此前无任何日志，
# 导致 hermes 0 派发无法从服务端解释。补取证日志（60s 去重防 claim 轮询刷屏）。
_pool_skip_log_cache: dict = {}
_POOL_SKIP_LOG_INTERVAL = 60.0


def _log_pool_skip_once(task_id, runtime_id: int, allowed):
    """记录一次「引擎被 pool 名单挡在门外」的取证日志（每 task+engine 60s 至多一条）。"""
    key = (task_id, runtime_id)
    now = time.time()
    try:
        if _pool_skip_log_cache.get(key, 0.0) + _POOL_SKIP_LOG_INTERVAL > now:
            return
    except Exception:
        pass
    _pool_skip_log_cache[key] = now
    if len(_pool_skip_log_cache) > 1000:   # 防泄漏：粗剪枝
        _stale = [k for k, v in _pool_skip_log_cache.items() if v + _POOL_SKIP_LOG_INTERVAL < now]
        for k in _stale:
            _pool_skip_log_cache.pop(k, None)
    log.info(f"claim pool filter: engine {runtime_id} not in pool_engine_ids={allowed} "
             f"of task {task_id}, skip (Issue #605)")


def _table_columns(table: str) -> set:
    """表实际存在的列（PRAGMA，进程内缓存）。

    迁移动态列后代码兼容未迁移的旧库（如测试库）与已迁移库。

    Lock contract（Issue #259 Optional）：调用方必须已持有 db_lock。
    本函数不再嵌套 with db_lock——生产 db_lock=RLock（可重入，嵌套无碍），
    但测试 monkeypatch 裸 Lock（非重入）时嵌套获取会自死锁。
    PRAGMA 只读 + 进程内缓存，无锁竞争风险。

    2026-10-09 缓存失效修复：测试连跑时 monkeypatch 换 db（真实库 39 列 →
    内存库 38 列），列缓存仍命中旧库清单 → no such column（顺序依赖假红）。
    以 db 对象身份（id()）为缓存键，db 变更即整体失效。
    """
    global _TABLE_COLS_CACHE_DB_ID
    if id(db) != _TABLE_COLS_CACHE_DB_ID:
        _TABLE_COLS_CACHE.clear()
        _TABLE_COLS_CACHE_DB_ID = id(db)
    _cached = _TABLE_COLS_CACHE.get(table)
    if _cached is not None:
        return _cached
    try:
        _rows = db.cursor().execute(f"PRAGMA table_info({table})").fetchall()
        _col = {r[1] for r in _rows}
    except Exception:
        _col = set()
    _TABLE_COLS_CACHE[table] = _col
    return _col


def _row_to_dict(row):
    return dict(zip(_TASK_COLUMNS_LIST, row))


def _pack_skill_dir(skill_name, skill_path):
    """把技能目录（SKILL.md + references/**）打包为 {rel_path: content}（远端交付，见 skill-delivery-remote.md）"""
    import os as _os
    files = {}
    base = _os.path.dirname(skill_path) if skill_path else ""
    if not base or not _os.path.isdir(base):
        return {skill_name: {}}
    for root, _dirs, names in _os.walk(base):
        for n in names:
            full = _os.path.join(root, n)
            rel = _os.path.relpath(full, base).replace("\\", "/")
            try:
                with open(full, "r", encoding="utf-8") as f:
                    files[rel] = f.read()
            except Exception:
                pass
    return {skill_name: files}


def _skill_files_from_zip(zip_bytes: bytes, skill_name: str) -> dict:
    """OBS 模式双写（老 worker 过渡期兼容）：从技能 zip 提取可文本化文件，
    生成与 _pack_skill_dir 相同结构的 skill_files。二进制文件跳过（老 worker 能力边界）。

    （merge 修复：feat/obs-storage 合并 dev 时此函数丢失，补回——老 worker
    兼容通道；OBS 技能本地无目录，skill_files 须从 zip 提取）
    """
    import io as _io
    import zipfile as _zf
    files = {}
    try:
        with _zf.ZipFile(_io.BytesIO(zip_bytes)) as zf:
            sub = {}
            for info in zf.infolist():
                if info.is_dir():
                    continue
                parts = info.filename.split("/", 1)
                inner = parts[1] if len(parts) == 2 and parts[0] == skill_name else info.filename
                try:
                    sub[inner] = zf.read(info.filename).decode("utf-8")
                except Exception:
                    continue  # 二进制/非 UTF-8：老 worker 不支持，跳过
            if sub:
                files[skill_name] = sub
    except Exception as e:
        log.warning(f"_skill_files_from_zip failed: {e}")
    return files


def _collect_server_env(user_id, account):
    """服务端 DB 系凭据注入（user credentials / 账户 AK-SK），远端 worker 无本地 DB 也生效。

    huawei_cloud AK/SK 注入为 HUAWEICLOUD_SDK_AK / HUAWEICLOUD_SDK_SK 环境变量，
    供 SRE agent (cloud-deploy skill) 通过 hcloud CLI 认证使用。
    codearts AK/SK 注入为 CODEARTS_CLI_AK / CODEARTS_CLI_SK。
    """
    env = {}
    if user_id and user_id > 0:
        try:
            from src.user.db import get_credentials
            for c in get_credentials(user_id):
                provider = c.get("provider", "")
                cfg = c.get("config", {})
                if provider == "huawei_cloud":
                    # 注入为 SRE cloud-deploy skill 期望的环境变量名
                    if cfg.get("ak"):
                        env["HUAWEICLOUD_SDK_AK"] = str(cfg["ak"])
                    if cfg.get("sk"):
                        env["HUAWEICLOUD_SDK_SK"] = str(cfg["sk"])
                    continue
                if provider == "codearts":
                    if cfg.get("ak"):
                        env["CODEARTS_CLI_AK"] = str(cfg["ak"])
                    if cfg.get("sk"):
                        env["CODEARTS_CLI_SK"] = str(cfg["sk"])
                    continue
                for k, v in cfg.items():
                    env[k.upper()] = str(v)
        except Exception:
            pass
    return env


_PROJECT_INFO_FILES = ("project-graph.md", "project-capabilities.md", ".metadata.json")


def load_project_info(repo_key: str) -> tuple[dict, dict]:
    """读取服务端权威 project-info（见 multi-issue-isolation-design.md §2.4）。

    返回 (files, meta)：
    - files: {rel_path: content}（project-graph.md / project-capabilities.md / .metadata.json）
    - meta: {last_commit_sha, branches, last_updated, degraded}
    无基线 → (files={}, meta={})。
    """
    if not repo_key:
        return {}, {}
    try:
        from src.config import WORKSPACE_DIR
        from src.repo_key import split_owner_repo
        owner, rname = split_owner_repo(repo_key)

        # OBS 分支：查 DB scope='baseline' + OBS 读取（不依赖本地磁盘）
        try:
            from src.storage import get_storage
            _storage = get_storage()
        except Exception:
            _storage = None
        if _storage is not None:
            files = {}
            from src.session_manager import db, db_lock
            with db_lock:
                rows = db.cursor().execute(
                    "SELECT file_path, obs_key FROM agent_shared_knowledge "
                    "WHERE repo_key=? AND scope='baseline' AND obs_key!='' AND obs_key IS NOT NULL "
                    "AND archived=0 "
                    "ORDER BY id DESC",
                    (repo_key,)
                ).fetchall()
            for fp, ok in rows:
                if fp and ok:
                    fname = os.path.basename(fp)
                    if fname in _PROJECT_INFO_FILES or fname == ".metadata.json":
                        try:
                            data = _storage.read_bytes(ok)
                            if data:
                                files[fname] = data.decode("utf-8", errors="replace")
                        except Exception:
                            continue
            if files:
                # .metadata.json 特殊处理
                meta = {}
                if ".metadata.json" in files:
                    try:
                        meta = json.loads(files[".metadata.json"])
                    except Exception:
                        meta = {}
                files[".metadata.json"] = json.dumps(meta, ensure_ascii=False)
                return files, meta
            # DB 查不到 baseline → 回退本地读取（兜底）
            # 继续走下面的本地逻辑

        # 本地模式（和 dev 一致）
        from src.repo_key import workspace_repo_dir
        pi = os.path.join(workspace_repo_dir(repo_key), "project-info")
        if not os.path.isdir(pi):
            return {}, {}
        files = {}
        for rel in _PROJECT_INFO_FILES:
            p = os.path.join(pi, rel)
            if os.path.isfile(p):
                try:
                    with open(p, "r", encoding="utf-8") as f:
                        files[rel] = f.read()
                except Exception:
                    pass
        meta = {}
        mp = os.path.join(pi, ".metadata.json")
        if os.path.isfile(mp):
            try:
                with open(mp, "r", encoding="utf-8") as f:
                    meta = json.load(f)
            except Exception:
                meta = {}
        if files:
            files[".metadata.json"] = json.dumps(meta, ensure_ascii=False)
        return files, meta
    except Exception as e:
        log.warning(f"load_project_info failed for {repo_key}: {e}")
        return {}, {}


def load_project_info_with_obs(repo_key: str) -> tuple[dict, dict]:
    """Issue #95：和 load_project_info 同逻辑，但 files 值改为 {content, obs_key}。

    供 collect_knowledge_files → generate_file_urls 用，不暴露给前端。
    load_project_info 本身不变（repos.py / prompt_builder.py 不受影响）。
    """
    if not repo_key:
        return {}, {}
    try:
        from src.config import WORKSPACE_DIR
        from src.repo_key import split_owner_repo
        owner, rname = split_owner_repo(repo_key)

        try:
            from src.storage import get_storage
            _storage = get_storage()
        except Exception:
            _storage = None
        if _storage is not None:
            files = {}
            from src.session_manager import db, db_lock
            with db_lock:
                rows = db.cursor().execute(
                    "SELECT file_path, obs_key FROM agent_shared_knowledge "
                    "WHERE repo_key=? AND scope='baseline' AND obs_key!='' AND obs_key IS NOT NULL "
                    "AND archived=0 "
                    "ORDER BY id DESC",
                    (repo_key,)
                ).fetchall()
            for fp, ok in rows:
                if fp and ok:
                    fname = os.path.basename(fp)
                    if fname in _PROJECT_INFO_FILES or fname == ".metadata.json":
                        try:
                            data = _storage.read_bytes(ok)
                            if data:
                                files[fname] = {"content": data.decode("utf-8", errors="replace"), "obs_key": ok}
                        except Exception:
                            continue
            if files:
                meta = {}
                if ".metadata.json" in files:
                    try:
                        meta = json.loads(files[".metadata.json"]["content"])
                    except Exception:
                        meta = {}
                files[".metadata.json"] = {"content": json.dumps(meta, ensure_ascii=False), "obs_key": ""}
                return files, meta

        # 本地模式
        from src.repo_key import workspace_repo_dir
        pi = os.path.join(workspace_repo_dir(repo_key), "project-info")
        if not os.path.isdir(pi):
            return {}, {}
        files = {}
        for rel in _PROJECT_INFO_FILES:
            p = os.path.join(pi, rel)
            if os.path.isfile(p):
                try:
                    with open(p, "r", encoding="utf-8") as f:
                        files[rel] = {"content": f.read(), "obs_key": ""}
                except Exception:
                    pass
        meta = {}
        mp = os.path.join(pi, ".metadata.json")
        if os.path.isfile(mp):
            try:
                with open(mp, "r", encoding="utf-8") as f:
                    meta = json.load(f)
            except Exception:
                meta = {}
        if files:
            files[".metadata.json"] = {"content": json.dumps(meta, ensure_ascii=False), "obs_key": ""}
        return files, meta
    except Exception as e:
        log.warning(f"load_project_info_with_obs failed for {repo_key}: {e}")
        return {}, {}


def collect_knowledge_files(repo_key: str, agent_type: str = "", issue_id: str = "",
                            scene_id: str = "") -> dict:
    """Issue #95：统一过滤知识文件，返回三批 {rel: {content, obs_key}}。

    三源不交叉：
    - load_project_info_with_obs → baseline (source='system')
    - member_files_pi → member (source='manual')
    - agent_knowledge_files → agent (source='agent')

    enqueue_task 和 direct 物化都调此函数，过滤逻辑只写一遍。
    返回 {"project_info": {rel: {content, obs_key}}, "meta": dict, "knowledge": {rel: {content, obs_key}}}
    """
    from src.api.session_store import get_issue_scene
    from src.api.knowledge import member_files_pi, agent_knowledge_files

    _scene_id = scene_id
    if not _scene_id and issue_id:
        try:
            _scene_id, _ = get_issue_scene(repo_key, issue_id)
        except Exception:
            _scene_id = ""
    _inum = _issue_num(issue_id)

    # 1. baseline
    _pif, _pim = load_project_info_with_obs(repo_key)

    # 2. member（并入 baseline）
    try:
        for rel, meta in member_files_pi(repo_key, agent_type, _inum, _scene_id or "").items():
            _pif.setdefault(rel, meta)
    except Exception:
        pass

    # 3. agent 知识（独立）
    _know = {}
    try:
        _know = agent_knowledge_files(repo_key, agent_type=agent_type, issue_num=_inum, scene_id=_scene_id or "")
    except Exception:
        pass

    return {"project_info": _pif, "meta": _pim, "knowledge": _know}


def verify_skill_materializable(skill: str, skill_id: int = 0, repo_key: str = "",
                                user_id: str | int = "") -> tuple:
    """Issue #630：技能物化完整性校验（Pipeline enqueue 前门禁）。

    校验「配置了技能」时是否存在可完整物化的内容源（SKILL.md + references）：
      ① skill_id / 名称解析命中 skill 表（resolve_skill_id user→repo→official）；
      ② OBS 对象键（file_path）可读 → worker 端按 sid 整包拉取 zip 物化；
      ③ 本地技能目录完整（validate_skill_dir：SKILL.md + references）；
      ④ sid=0 时允许官方内置包（resolve_official_skill_path，部署随包分发）。
    返回 (ok, reason, sid)。

    空壳技能行（legacy create_skill/迁移注册、file_path 空 + 本地目录无 SKILL.md，
    如 sid=3276 vod-cli-crossrepo）→ (False, ...)，调用方显式阻断派发不静默降级。
    技能未配置（skill 为空）恒 (True, ...)，不误伤未配置场景（Spec 风险表）。
    """
    skill = (skill or "").strip()
    if not skill:
        return True, "skill not configured", 0
    from src.user.db import resolve_skill_id, get_skill_by_id, get_skill_path, get_skill_object_key
    sid = int(skill_id or 0)
    if not sid:
        try:
            sid = int(resolve_skill_id(skill, repo_key=repo_key, user_id=user_id) or 0)
        except Exception:
            sid = 0
    if sid:
        _sk = None
        try:
            _sk = get_skill_by_id(sid)
        except Exception:
            _sk = None
        if not _sk:
            return False, f"skill '{skill}' (sid={sid}) not found or inactive", sid
        _okey = (_sk.get("file_path") or "").strip() or get_skill_object_key(sid)
        # Issue #630 审查修复：storage 不可用（get_skill_storage 返回 None/异常）时
        # 不再 fail-open 放行——file_path 有值但对象不可读，worker 端整包拉取仍会
        # 静默失败。继续 fallthrough 本地目录校验链，阻断时 reason 标注 storage unavailable。
        _storage_unavailable = False
        if _okey:
            _st = None
            try:
                from src.storage import get_skill_storage
                _st = get_skill_storage()
            except Exception:
                _st = None
            if _st is None:
                _storage_unavailable = True
            elif _st.read_bytes(_okey) is not None:
                return True, f"obs object ok (key={_okey})", sid
        # 本地目录完整性（SKILL.md + references）
        try:
            _full = get_skill_path(sid)
        except Exception:
            _full = ""
        if _full:
            from src.runtime.skill_materialize import validate_skill_dir
            ok, missing, _extra, _vd = validate_skill_dir(os.path.dirname(_full))
            if ok:
                return True, "local skill dir ok", sid
            _src = (f"obs storage unavailable (key={_okey})" if _storage_unavailable else
                    (f"no readable obs object (key={_okey})" if _okey else "file_path empty"))
            return False, (f"skill '{skill}' (sid={sid}) materialization source missing: "
                           f"{_src}"
                           + f", local dir incomplete (missing: {', '.join(missing[:5])}); "
                           f"re-upload the skill package to fix"), sid
        _src = (f"obs storage unavailable (key={_okey})" if _storage_unavailable else
                ("no obs file_path and no local skill dir" if not _okey else
                 f"no readable obs object (key={_okey})"))
        return False, (f"skill '{skill}' (sid={sid}) materialization source missing: "
                       f"{_src}; re-upload the skill package to fix"), sid
    # sid=0：官方内置包兜底（worker 端 official fallback 可完整物化，非静默换技能）
    try:
        from src.config import resolve_official_skill_path
        _official = resolve_official_skill_path(skill)
        if _official and os.path.isfile(_official):
            return True, "official skill bundle ok", 0
    except Exception:
        pass
    return False, (f"skill '{skill}' unresolvable (no skill row, no official bundle); "
                   f"check scene skill binding or upload the skill first"), 0


def enqueue_task(*, agent_session_id, runtime_id, prompt, cwd="",
                 runtime_session_id="", event_type="", account="", user_id=0,
                 repo_gitcode_token="", user_gitcode_token="", env_extra=None,
                 event_seq=None, issue_id="", repo_key="", agent_type="",
                 skill="", skill_path="", skill_id=0, server_url="", issue_session_id="",
                 project_info_files=None, project_info_meta=None, knowledge_files=None,
                 project_info_file_urls=None, knowledge_file_urls=None,
                 pool_agent_type="", affinity_runtime_id=0, pool_tag="", pool_engine_ids="",
                 pool_engine_source=""):
    """业务层入队：确定路由（D3）+ 快照 + 技能打包 + 凭据注入。返回 task_id。

    引擎池（agent-cluster-design.md §4.1/§4.2）：runtime_id=0 + pool_agent_type 非空
    → 池调度任务，由池内任意空闲引擎 claim 时领取（claim 会把 runtime_id 定为实际引擎）。
    显式 runtime_id（>0）→ 确定路由，行为与现状一致。

    knowledge_files：共享知识快照（event-conversation-design.md §3.1.1），None 时自动打包；
    远端 worker 侧 materialize 到 cwd/.knowledge 并替换 {{KNOWLEDGE_DIR}}。

    skill_id：skill 表主键，优先于 skill_name 解析（避免 user/repo scope 的 owner_id 不匹配）。
    skill_id=0 时回退 resolve_skill_id 按名字解析（向后兼容）。

    pool_engine_source（local-claimer 设计）：pool_engine_ids 的来源标记——
    'scene' = 用户在场景里显式绑定（白名单全灭时保持 failed，Issue #253 硬约束）；
    'auto'  = 引擎自动选择/兜底产生的绑定（全灭时可降级放行全站，可审计）；
    ''      = 未标记（历史任务，按 scene 对待——保守不降级）。
    """
    if not agent_session_id:
        raise ValueError("agent_session_id is required")
    if not runtime_id and not pool_agent_type:
        raise ValueError("runtime_id or pool_agent_type is required (确定路由 D3)")
    # Issue #653：入队前置空候选校验——pool_engine_ids 为 JSON 空数组 "[]" 时：
    # '[]' 是 truthy 字符串但没有任何引擎能通过 claim 守卫，任务会卡死无人认领。
    # scene 来源（用户显式绑定，#253 硬约束）→ 拒绝入队（宁失败不跑飞）；
    # auto 来源（系统自动选择）→ 降级 "" 全量广播 + 告警（可审计）。
    if pool_engine_ids:
        try:
            _pei_653 = json.loads(pool_engine_ids)
        except Exception:
            _pei_653 = None  # 非 JSON 值保持原语义（历史调用方可能传其它格式）
        if isinstance(_pei_653, list) and not _pei_653:
            if (pool_engine_source or "") == "auto":
                log.warning(f"enqueue_task: empty pool_engine_ids (source=auto) degraded to "
                            f"broadcast for agent={agent_type} (Issue #653)")
                pool_engine_ids = ""
            else:
                raise ValueError(
                    "pool_engine_ids resolved to an empty engine list (source="
                    f"{pool_engine_source or 'scene'}, agent={agent_type}); refusing enqueue "
                    "to avoid silent global-pool fallback (Issue #653)")
    task_id = "t_" + uuid.uuid4().hex[:12]
    skill_files = {}
    # Issue #463 P2 扩展（2026-10-27）：skill_files URL/引用单写——技能在 OBS 有原始 zip
    # （file_path 非空，99% 的技能满足），DB 里再存一份完整内容拷贝属于双写：
    # 单任务约 600KB，单周 1.7GB，agent_tasks 表因此无上限增长（生产实测 4.76GB）。
    # 消费侧降级链早已有之：claim 现签 skill_obs_url（① worker 直连 OBS）→
    # POST /tasks/{id}/skill-zip 服务端代拉（② 现查 file_path 走 zip_cache，不读本列）→
    # skill_files 文本物化（③ 仅 ①② 都失败时）。因此 ①② 可达时本列只需存引用标记：
    # {"<skill_name>": {"__obs_key__": "<对象键>"}}（~50 字节）。
    # worker 侧（agent.py）识别 __obs_key__ 后按 ② 的路径拉 zip；只有无 _okey 的
    # 本地目录技能（约 1%）仍走原样全量内联兜底。
    _skill_obs_key = ""
    if skill:
        try:
            from src.user.db import resolve_skill_id, get_skill_path, get_skill_object_key, get_skill_by_id
            if skill_id:
                _sk = get_skill_by_id(skill_id)
                if _sk:
                    # ① skill_id 有效：直接按主键查，跳过 resolve_skill_id 的 owner_id 匹配
                    _sid = skill_id
                    _path = get_skill_path(skill_id)  # 绝对路径（official/user/repo 三 scope 统一）
                    _okey = _sk.get("file_path") or ""
                else:
                    # ② skill_id 失效（skill 被删除/停用）→ 回退按名字解析
                    _sid = resolve_skill_id(skill, repo_key=repo_key, user_id=user_id) or resolve_skill_id(skill)
                    _path = get_skill_path(_sid) if _sid else (skill_path or "")
                    _okey = get_skill_object_key(_sid) if _sid else ""
            else:
                # ③ 无 skill_id → 走老逻辑（向后兼容）
                _sid = resolve_skill_id(skill, repo_key=repo_key, user_id=user_id) or resolve_skill_id(skill)
                _path = get_skill_path(_sid) if _sid else (skill_path or "")
                _okey = get_skill_object_key(_sid) if _sid else ""
            # 本地目录存在 → 打包（仅无 OBS 键的兜底路径；有 _okey 时跳过本地打包——
            # OBS zip 是权威源且内容一致，本地再打一份就是双写）
            if _path and os.path.isdir(os.path.dirname(_path)) and not _okey:
                skill_files = _pack_skill_dir(skill, _path)
            # file_path 非空 → OBS zip 提取（user/repo 技能主要走此路径）
            if _okey and not skill_files:
                # Issue #463 P2 扩展：有 OBS 对象键 → 不再从 zip 提取内容内联（原
                # _skill_files_from_zip 双写路径），只存引用标记；worker 侧按
                # skill_obs_url / 服务端代拉获取原始 zip（内容等价且含二进制）。
                _skill_obs_key = _okey
                skill_files = {skill: {"__obs_key__": _okey}}
        except Exception as e:
            log.warning(f"enqueue: skill pack failed for {skill}: {e}")
    # Issue #95：统一调 collect_knowledge_files 过滤知识文件（三源不交叉，过滤只做一次）
    _collected = None
    if project_info_files is None or knowledge_files is None:
        try:
            _collected = collect_knowledge_files(repo_key, agent_type=agent_type, issue_id=issue_id)
            if project_info_files is None:
                project_info_files = {rel: v["content"] for rel, v in _collected["project_info"].items()}
                project_info_meta = _collected["meta"]
            if knowledge_files is None:
                knowledge_files = {rel: v["content"] for rel, v in _collected["knowledge"].items()}
        except Exception as e:
            log.warning(f"enqueue: collect_knowledge_files failed for {repo_key}: {e}")
            if project_info_files is None:
                project_info_files = {}
                project_info_meta = {}
            if knowledge_files is None:
                knowledge_files = {}
            _collected = {"project_info": {}, "knowledge": {}}
    # 生成 file_urls（基于已过滤结果，不查 DB；storage 为 None 时 file_urls 为空 dict，worker 用 content 兜底）
    try:
        from src.storage import get_storage
        _storage = get_storage()
    except Exception:
        _storage = None
    if _storage:
        # 复用 _collected（只在上面已调用过时复用；调用方传了内容时重新调一次取 obs_key）
        if _collected is None:
            try:
                _collected = collect_knowledge_files(repo_key, agent_type=agent_type, issue_id=issue_id)
            except Exception:
                _collected = {"project_info": {}, "knowledge": {}}
        _all_filtered = {}
        _all_filtered.update(_collected.get("project_info") or {})
        _all_filtered.update(_collected.get("knowledge") or {})
        _file_urls = _storage.generate_file_urls(_all_filtered) if _all_filtered else {}
    else:
        _file_urls = {}
    # 分离 URL：project_info_file_urls = baseline + member，knowledge_file_urls = agent
    project_info_file_urls = {k: v for k, v in _file_urls.items()
                              if k.startswith("member/") or k in _PROJECT_INFO_FILES}
    knowledge_file_urls = {k: v for k, v in _file_urls.items()
                           if not k.startswith("member/") and k not in _PROJECT_INFO_FILES}
    # Issue #463 P2：URL 优先单写——已生成 OBS URL 的文件不再内联 MEDIUMTEXT 内容（置空串）。
    # worker 端 _materialize_files 三级降级（URL → content → storage_key），置空后走
    # URL 下载 / storage_key 直连，无 URL 的键仍保留内联兜底，消除 DB 内联与 OBS 双写。
    if project_info_file_urls:
        project_info_files = {k: ("" if k in project_info_file_urls else v)
                              for k, v in (project_info_files or {}).items()}
    if knowledge_file_urls:
        knowledge_files = {k: ("" if k in knowledge_file_urls else v)
                           for k, v in (knowledge_files or {}).items()}
    env = dict(env_extra or {})
    for k, v in _collect_server_env(user_id, account).items():
        env.setdefault(k, v)
    # Issue #470：统一超时配置下发（setdefault——上层 env_extra 显式设置优先）。
    # worker 端看门狗 + ACP 全局超时均任务级 env 优先，管理台/配置文件改后对新任务生效。
    for k, v in _runtime_timeout_env_kv().items():
        env.setdefault(k, v)
    # Issue #887：任务 payload 携带按 agent 绑定（仓库级/场景级）解析的 skill——
    # 执行侧一致性校验与任务日志「技能来源」标注的绑定基准值（worker 端 agent.py
    # _skill_consistency_gate 与 env 双源互校消费；与任务行 skill 字段同源）。
    # QA 修复：直接赋值（非 setdefault）——env_extra 不得预置覆盖绑定基准，
    # 否则可观测标注失真且双源互校永远一致（假阴性）。
    env["GRAPE_BOUND_SKILL"] = (skill or "").strip()
    # Issue-45 M2：签发会话级临时 token 注入 GRAPE_SESSION_TOKEN（存在即有效，删除即失效）。
    # session_key=task_id（精确失效、不误伤并发同类会话）；chat 任务绑定 USER_ID 用户身份，
    # devops 事件任务无用户概念（user_id=0，仓库身份）。
    if repo_key and agent_type:
        try:
            from src.api.auth_token import issue_session_token
            _tk_uid = user_id or 0
            _usr = env.get("USER_ID") or ""
            if str(_usr).isdigit():
                _tk_uid = int(str(_usr))
            env["GRAPE_SESSION_TOKEN"] = issue_session_token(
                session_key=task_id, repo_key=repo_key,
                agent_type=agent_type, user_id=_tk_uid)
            # CLI 命令按 token 作用域过滤：本任务持 tk_ 会话 token → 只暴露 dual+session_only 命令
            # （见 grape-cli-mode-filter-design.md；chat 与 devops 任务均适用）
            env["GRAPE_CLI_MODE"] = "WORKER"
        except Exception as e:
            log.warning(f"enqueue: session token issue failed for {task_id}: {e}")
    _server_url = server_url or ""
    if not _server_url:
        try:
            # Issue #471 E1：任务下发的 server_url 是 worker 回连地址
            from src.user.db import get_worker_connect_url
            _server_url = get_worker_connect_url()
        except Exception:
            pass
    with db_lock:
        c = db.cursor()
        _avail = _table_columns("agent_tasks")
        _insert_cols = [x for x in _TASK_COLUMNS_LIST if x not in _TASK_SIGNAL_COLUMNS and x in _avail]
        _insert_vals = {
            "id": task_id,
            "agent_session_id": agent_session_id,
            "runtime_id": runtime_id,
            "event_seq": event_seq,
            "issue_id": issue_id or "", "repo_key": repo_key or "",
            "agent_type": agent_type or "", "skill": skill or "", "skill_id": skill_id or 0,
            "prompt": prompt, "cwd": cwd or "", "runtime_session_id": runtime_session_id or "",
            "event_type": event_type or "", "account": account or "", "user_id": user_id or 0,
            "repo_gitcode_token": repo_gitcode_token or "", "user_gitcode_token": user_gitcode_token or "",
            "env_extra": json.dumps(env), "skill_files": json.dumps(skill_files), "server_url": _server_url,
            "issue_session_id": issue_session_id or "",
            "project_info_files": json.dumps(project_info_files), "project_info_meta": json.dumps(project_info_meta),
            "knowledge_files": json.dumps(knowledge_files),
            "project_info_file_urls": json.dumps(project_info_file_urls), "knowledge_file_urls": json.dumps(knowledge_file_urls),
            "attempts": 0, "pool_agent_type": pool_agent_type or "", "affinity_runtime_id": affinity_runtime_id or 0,
            "pool_tag": pool_tag or "", "pool_engine_ids": pool_engine_ids or "",
            "pool_engine_source": pool_engine_source or "",
            "status": "queued", "exit_code": None, "result_text": "", "errors": "", "stderr": "",
            "usage_json": "{}", "started_at": None, "completed_at": None,
            "pending_since": None,
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
        c.execute(
            f"INSERT INTO agent_tasks ({', '.join(_insert_cols)}) VALUES ({','.join(['?'] * len(_insert_cols))})",
            [_insert_vals[x] for x in _insert_cols],
        )
        db.commit()
    from src.runtime.instances import runtime_instance_name
    _inst = runtime_instance_name(runtime_id)
    log.info(f"Task enqueued: {task_id} session={agent_session_id} runtime={runtime_id}({_inst}) skill={skill}")
    # 可观察性埋点：任务入队
    try:
        from src.observability import record_task_enqueued
        record_task_enqueued(task_id, event_type or agent_type or "other", runtime_id)
    except Exception:
        pass
    # 事件总线：任务创建
    try:
        from src.observability.events import bus, Event
        bus.publish(Event(type="task:enqueued", task_id=task_id,
                          payload={"agent_type": agent_type, "runtime_id": runtime_id,
                                    "repo_key": repo_key, "skill": skill}))
    except Exception:
        pass
    return task_id


# ===== 引擎池绑定（agent-cluster-design.md §4.1）=====
# 注：origin/main 曾声明弃用（engine-selection-design.md v2），但 dev 线
# 仍以引擎池为任务调度主路径（claim 池匹配 / pool_runtime_ids 在线过滤 /
# 事件 runtime 回填），保留本实现，engine_select 新模型可并行引入。

def set_agent_pool(agent_type: str, runtime_ids: list[int], weight: int = 1, priority: int = 5):
    """替换某 Agent 类型的引擎池绑定（先删后插，单事务）。runtime_ids 为空列表 → 清空池（回到兜底链）。"""
    if not agent_type:
        raise ValueError("agent_type is required")
    with db_lock:
        c = db.cursor()
        c.execute("DELETE FROM agent_type_runtime_pool WHERE agent_type=?", (agent_type,))
        for rid in (runtime_ids or []):
            c.execute(
                "INSERT OR IGNORE INTO agent_type_runtime_pool (agent_type, runtime_id, weight, priority) VALUES (?,?,?,?)",
                (agent_type, int(rid), int(weight), int(priority)))
        db.commit()
    log.info(f"Agent pool set: {agent_type} -> {runtime_ids or []} (weight={weight}, priority={priority})")


def get_agent_pool(agent_type: str) -> list[dict]:
    """返回某 Agent 类型的引擎池绑定（含引擎实时状态，供管理端/调度展示）。"""
    with db_lock:
        c = db.cursor()
        try:
            c.execute(
                "SELECT p.runtime_id, p.weight, p.priority, p.enabled, p.remark, "
                "       r.name, r.status, r.healthy, r.busy_slots, r.max_concurrent, r.version, "
                "       r.credential_id, COALESCE(rc.device_name, ''), COALESCE(rc.last_ip, '') "
                "FROM agent_type_runtime_pool p "
                "LEFT JOIN agent_runtimes r ON r.id = p.runtime_id "
                "LEFT JOIN runtime_credentials rc ON rc.id = r.credential_id "
                "WHERE p.agent_type=? ORDER BY p.priority ASC, p.id ASC",
                (agent_type,))
            rows = c.fetchall()
        except Exception as e:
            log.warning(f"get_agent_pool failed: {e}")
            return []
    return [{
        "runtime_id": r[0], "weight": r[1], "priority": r[2], "enabled": bool(r[3]),
        "remark": r[4] or "", "name": r[5] or "", "status": r[6] or "offline",
        "healthy": bool(r[7]), "busy_slots": r[8] or 0, "max_concurrent": r[9] or 0,
        "version": r[10] or "", "credential_id": r[11] or 0,
        "device_name": r[12] or "", "last_ip": r[13] or "",
    } for r in rows]


def pool_runtime_ids(agent_type: str) -> list[int]:
    """池内 enabled 且在线（心跳在线）的引擎 id 列表（claim 池调度用）。

    只返回在线引擎：池内引擎全离线/不存在时返回空 → 调用方（入队/对话）回退
    默认 runtime，避免任务下发到 offline worker 后无人领取而永久 queued
    （issue #41 卡住根因：池绑定 runtime 9/12/18 全部 offline，任务挂起）。
    """
    with db_lock:
        c = db.cursor()
        try:
            c.execute(
                "SELECT p.runtime_id FROM agent_type_runtime_pool p "
                "JOIN agent_runtimes r ON r.id = p.runtime_id "
                "WHERE p.agent_type=? AND p.enabled=1 AND r.status='online'",
                (agent_type,))
            return [r[0] for r in c.fetchall()]
        except Exception:
            return []


def _issue_num(issue_id) -> int:
    """'#5' / '5' / 5 → 5；无法解析返回 0。"""
    if not issue_id:
        return 0
    m = re.search(r"(\d+)", str(issue_id))
    return int(m.group(1)) if m else 0


def claim(runtime_id, expected_task_id=None, expected_event_seq=None) -> dict | None:
    """原子抢单 + 串行槽（见 multi-issue-isolation-design.md §3.3 / concurrency-design.md §2.3）。

    Issue #520 bound claim：expected_task_id + expected_event_seq 成对提供时，
    在同一 db_lock 临界区内对目标任务做原子身份校验（owner/runtime/池/状态/串行槽
    条件与 FIFO 完全一致 + expected 精确匹配），未命中返回 None——绝不回退 FIFO
    领取其他任务（一次性许可 fail-closed）。未提供 expected 时保持原 FIFO 路径零改动。

    串行粒度（M2）：任务带 issue_session_id → 同 Issue 严格串行（跨 Issue 并行）；
    issue_session_id 为空（旧任务/#0 事件）→ 回退按 agent_session_id 串行（现状语义）。

    引擎池（agent-cluster-design.md §4.2）：本引擎属于某 Agent 池时，除自己的队列外，
    还可领取该池的未定任务（runtime_id=0 + pool_agent_type 匹配，领取后 runtime_id 定为
    本引擎）。affinity_runtime_id 命中本引擎的任务优先（会话亲和，§4.5）。
    """
    with db_lock:
        c = db.cursor()
        try:
            # ---- 兜底回收（M1：dispatched 超时自动重排，防 worker 丢弃任务死循环）----
            # worker 端 max_concurrent 满时 claim 后可能丢弃任务（不 start 不释放），
            # 导致 dispatched 任务永久卡死、busy_slots 泄漏。这里在每次 claim 前
            # 将超时未 start 的任务回池重排；attempts>=3 直接置 failed。
            try:
                # 回收窗口 600s：SRE 执行 terraform apply 等长命令可达 900s，
                # 期间无事件产出→task_messages 无新增→活跃度检查失败→误回收重跑。
                # 300s 窗口会误杀执行中长任务；600s 窗口让长命令有足够时间完成。
                # worker 端 claim 后 task_start 置 running 可彻底规避；本窗口作为
                # 不依赖 worker 端的兜底（真死任务 10 分钟内恢复重排）。
                from src.runtime.task_activity import CLAIM_ACTIVITY_WINDOW_SECONDS
                _stale = (datetime.now(timezone.utc)
                          - timedelta(seconds=CLAIM_ACTIVITY_WINDOW_SECONDS)).isoformat()
                c.execute(
                    "SELECT id, runtime_id, event_seq, attempts FROM agent_tasks "
                    "WHERE status='dispatched' AND started_at < ? AND started_at IS NOT NULL",
                    (_stale,))
                _stale_rows = c.fetchall()
                _actually_recovered = []
                for _tid, _rid, _seq, _attempts in _stale_rows:
                    # 活跃度检查（2026-08-21 修复 / Issue #1495 改统一口径）：
                    # task_start 失败时 status 留在 dispatched 但 agent 实际在跑
                    # （有 task_messages 产出）。查最近 600s 是否有消息，有则跳过
                    # 回收——agent 活跃说明只是 task_start 回调丢了，不该重跑。
                    # 600s 窗口兼容长命令（terraform apply 可达 900s 无事件产出）。
                    # Issue #1495：判定改走 task_activity.has_recent_activity——
                    # 旧写法 `created_at > ?` 是 TEXT 逐字节比较，表默认值行
                    # （空格分隔，' '<'T'）与无微秒 ISO 行（同秒 '+'<'.'）会被
                    # 系统性误判不活跃；现按 task_id 索引取行后 Python 解析为
                    # epoch 比较（解析失败行不计活跃，保守宁可误回收）。
                    try:
                        from src.runtime.task_activity import has_recent_activity
                        if has_recent_activity(c, _tid, CLAIM_ACTIVITY_WINDOW_SECONDS):
                            continue  # 最近 600s 有消息，agent 在跑，跳过回收
                    except Exception:
                        pass  # 查询失败不阻塞回收（保守：宁可误回收也不留死任务）
                    _actually_recovered.append((_tid, _rid, _seq))
                    # 杀掉旧进程：recover 重新入队后会有新 worker claim 启动新进程，
                    # 旧进程不杀会继续执行（tf-infra7 出现过 2 个进程同时跑同一 task）
                    # 注：pytest 环境跳过真杀（测试库任务非真实引擎进程，pgrep 扫
                    # cwd 存量会误杀无关进程）；生产路径不受影响，仍然真杀。
                    try:
                        c.execute("SELECT cwd, runtime_id FROM agent_tasks WHERE id=?", (_tid,))
                        _old = c.fetchone()
                        if _old:
                            _backend = "opencode"
                            try:
                                c.execute("SELECT name FROM agent_runtimes WHERE id=?", (_old[1],))
                                _rn = c.fetchone()
                                if _rn and "hermes" in (_rn[0] or "").lower():
                                    _backend = "hermes"
                                elif _rn and "codearts" in (_rn[0] or "").lower():
                                    _backend = "codearts"
                            except Exception:
                                pass
                            import os as _os
                            if "PYTEST_CURRENT_TEST" not in _os.environ:
                                from src.runtime.agent import _kill_stale_engine
                                _kill_stale_engine({"cwd": _old[0], "id": _tid}, _backend)
                    except Exception as _ke:
                        log.warning(f"recover: kill stale engine failed for task {_tid}: {_ke}")
                    # 定向任务（pool_agent_type=''）回收归零 runtime_id 后变无主：
                    # claim 池匹配不命中、定向(runtime_id)也失配 → 永久死任务
                    # （实测 seq#264 issue#4 卡死 71 分钟）。归零时回填 agent_type
                    # 转为池任务，池内任意引擎可再领。
                    _pool_fill = ""
                    try:
                        c.execute("SELECT agent_type FROM agent_tasks WHERE id=?", (_tid,))
                        _row = c.fetchone()
                        if _row and _row[0]:
                            _pool_fill = _row[0]
                    except Exception:
                        pass
                    # 2026-10-20 MySQL attempts 语义修复（与 runtime.py recover-orphans
                    # 同款病根）：原写法 SET attempts=attempts+1 且 CASE 里 attempts+1
                    # —— MySQL 后赋值读新值（等效 +2），SQLite 读旧值，两库分叉导致
                    # 封顶轮数不一致。此处判死后有 SELECT status 复核（1058-1061 行）
                    # 所以事件不会悬空，但语义仍需统一。参数化传 _att 强制一致。
                    _att = (_attempts or 0) + 1
                    _STALE_RECOVER_MAX_ATTEMPTS = 50
                    c.execute(
                        "UPDATE agent_tasks SET attempts=?, "
                        "status=CASE WHEN ? >= ? THEN 'failed' ELSE 'queued' END, "
                        "runtime_id=CASE WHEN ? >= ? THEN runtime_id ELSE 0 END, "
                        "pool_tag=CASE WHEN ? < ? AND pool_tag='' "
                        "THEN ? ELSE pool_tag END, "
                        "pool_agent_type=CASE WHEN ? < ? AND pool_agent_type='' "
                        "THEN ? ELSE pool_agent_type END, "
                        "started_at=NULL, "
                        "result_text=CASE WHEN ? >= ? THEN '回收超限(dispatched 超时 50 次)' ELSE result_text END "
                        "WHERE id=? AND status='dispatched'",
                        (_att,
                         _att, _STALE_RECOVER_MAX_ATTEMPTS,
                         _att, _STALE_RECOVER_MAX_ATTEMPTS,
                         _att, _STALE_RECOVER_MAX_ATTEMPTS, _pool_fill,
                         _att, _STALE_RECOVER_MAX_ATTEMPTS, _pool_fill,
                         _att, _STALE_RECOVER_MAX_ATTEMPTS, _tid))
                    if c.rowcount and _rid:
                        release_busy_slot(_rid, cursor=c)
                    # 回收置 failed（attempts 超限）→ 同步事件终态，避免事件永远 running；
                    # Issue #369：完整回写事件诊断字段（result/exit_code/completed_at）
                    if c.rowcount and _seq:
                        c.execute("SELECT status FROM agent_tasks WHERE id=?", (_tid,))
                        _after = c.fetchone()
                        if _after and _after[0] == "failed":
                            _sync_event_failed(
                                _seq,
                                "回收超限：dispatched 超时回收 50 次仍无有效认领，任务判死")
                if _actually_recovered:
                    log.info(f"recover stale dispatched: {len(_actually_recovered)}/{len(_stale_rows)} tasks "
                             f"({len(_stale_rows) - len(_actually_recovered)} skipped: active messages)")
            except Exception as _e:
                log.warning(f"recover stale dispatched failed: {_e}")

            if expected_task_id is not None:
                # Issue #520 bound claim：expected 身份在 db_lock 临界区内原子校验。
                # 候选骨架与 FIFO 完全一致（定向 runtime_id / 共享池 / 串行槽 / queued）
                # + expected 精确匹配；未命中 → 无任务，绝不回退 FIFO 领其他任务。
                c.execute("""
                    SELECT id FROM agent_tasks
                    WHERE id=? AND event_seq=? AND status='queued'
                      AND (runtime_id=? OR (runtime_id=0 AND pool_agent_type != ''))
                      AND NOT EXISTS (
                        SELECT 1 FROM agent_tasks a2
                        WHERE a2.status IN ('dispatched','running')
                          AND (
                            (agent_tasks.issue_session_id != '' AND a2.issue_session_id = agent_tasks.issue_session_id)
                            OR
                            (agent_tasks.issue_session_id = '' AND a2.agent_session_id = agent_tasks.agent_session_id)
                          )
                      )
                    LIMIT 1
                """, (expected_task_id, expected_event_seq, runtime_id))
                row = c.fetchone()
                if not row:
                    db.commit()
                    return None
                # pool_engine_ids 广播范围守卫：目标任务不含本引擎 → fail-closed（不尝试其他任务）
                c.execute("SELECT pool_engine_ids FROM agent_tasks WHERE id=?", (row[0],))
                _pe_row = c.fetchone()
                if _pe_row and _pe_row[0]:
                    try:
                        _allowed = json.loads(_pe_row[0])
                        if _allowed and runtime_id not in _allowed:
                            db.commit()
                            return None
                    except Exception:
                        pass
                task_id = row[0]
            else:
                # 确定路由：① 定向任务（runtime_id=?）② 池任务（runtime_id=0 + pool_agent_type，
                # pool_engine_ids 为空或包含本引擎即可领；
                # 回收逻辑会把 dispatched 超时任务归零转池任务，claim 必须兼容，否则永久死任务）
                #
                # 插件模式：广播任务只能走 claim_by_tag（标签匹配），legacy claim 不认领广播任务，
                # 避免标签不匹配的机器通过兜底渠道抢到任务。
                c.execute("""
                    SELECT id FROM agent_tasks
                    WHERE runtime_id=? AND status='queued'
                      AND NOT EXISTS (
                        SELECT 1 FROM agent_tasks a2
                        WHERE a2.status IN ('dispatched','running')
                          AND (
                            (agent_tasks.issue_session_id != '' AND a2.issue_session_id = agent_tasks.issue_session_id)
                            OR
                            (agent_tasks.issue_session_id = '' AND a2.agent_session_id = agent_tasks.agent_session_id)
                          )
                      )
                    ORDER BY created_at ASC LIMIT 1
                """, (runtime_id,))
                row = c.fetchone()
                if not row:
                    _is_plugin = False
                    try:
                        from src.api.event_pipeline import is_plugin_mode
                        _is_plugin = is_plugin_mode()
                    except Exception:
                        pass
                    if not _is_plugin:
                        c.execute("""
                            SELECT id FROM agent_tasks
                            WHERE runtime_id=0 AND pool_agent_type != '' AND status='queued'
                              AND NOT EXISTS (
                                SELECT 1 FROM agent_tasks a2
                                WHERE a2.status IN ('dispatched','running')
                                  AND (
                                    (agent_tasks.issue_session_id != '' AND a2.issue_session_id = agent_tasks.issue_session_id)
                                    OR
                                    (agent_tasks.issue_session_id = '' AND a2.agent_session_id = agent_tasks.agent_session_id)
                                  )
                              )
                            ORDER BY created_at ASC LIMIT 1
                        """)
                        row = c.fetchone()
                if not row:
                    db.commit()
                    return None
                task_id = row[0]

            # pool_engine_ids 限定广播范围：非空时只有列表中的引擎能认领
            # 修复（2026-08-29）：原代码 return None 会阻塞队列——与 claim_by_tag 同一 bug。
            # 改为循环跳过不匹配的任务，继续尝试下一个。
            _tried_ids = [task_id]
            _max_tries = 20
            while True:
                if expected_task_id is not None:
                    break  # Issue #520 bound claim：目标已原子校验（含 pool_engine_ids 守卫），不进入 FIFO 跳过循环
                c.execute("SELECT pool_engine_ids FROM agent_tasks WHERE id=?", (task_id,))
                _pe_row = c.fetchone()
                _skip = False
                if _pe_row and _pe_row[0]:
                    import json as _json
                    try:
                        _allowed = _json.loads(_pe_row[0])
                        if _allowed and runtime_id not in _allowed:
                            # 兜底：检查 pool_engine_ids 中的引擎是否全部不可达
                            # 不可达 = 离线 / 无对应 tag 能力 / scope 不匹配
                            # 全部不可达时清空 pool_engine_ids，让 task 可被任意引擎认领
                            _task_tag = ""
                            _task_repo = ""
                            _task_uid = 0
                            try:
                                c.execute("SELECT pool_tag, repo_key, user_id FROM agent_tasks WHERE id=?", (task_id,))
                                _trow = c.fetchone()
                                if _trow:
                                    _task_tag = _trow[0] or ""
                                    _task_repo = _trow[1] or ""
                                    _task_uid = _trow[2] or 0
                            except Exception:
                                pass
                            _has_capable = _check_pool_engines_reachable(_allowed, _task_tag, _task_repo, task_user_id=_task_uid)
                            if not _has_capable:
                                # Issue #1487 改动C：池内全不可达只是单次瞬时快照的结论，
                                # 不在此处直接判死（避免非宿主主控在心跳非新鲜窗口先 tick
                                # 即把任务打死）——保持 queued，交 _check_stale_queued_tasks
                                # 的既有阈值窗口（120s）统一裁决。
                                log.info(f"claim: task {task_id} pool engines {_allowed} all "
                                         f"unreachable in this snapshot, keep queued for "
                                         f"_check_stale_queued_tasks (Issue #1487)")
                                _skip = True
                                # Issue #605：引擎不在广播名单被静默跳过 → 取证日志（60s 去重）
                                _log_pool_skip_once(task_id, runtime_id, _allowed)
                            else:
                                _skip = True
                                # Issue #605：引擎不在广播名单被静默跳过 → 取证日志（60s 去重）
                                _log_pool_skip_once(task_id, runtime_id, _allowed)
                    except Exception:
                        pass
                if not _skip:
                    break

                # 跳过当前，尝试下一个定向任务
                _tried_str = ','.join(["'"+x+"'" for x in _tried_ids])
                c.execute(
                    f"SELECT id FROM agent_tasks "
                    f"WHERE runtime_id=? AND status='queued' "
                    f"AND id NOT IN ({_tried_str}) "
                    f"AND NOT EXISTS ("
                    f"  SELECT 1 FROM agent_tasks a2 WHERE a2.status IN ('dispatched','running') AND ("
                    f"    (agent_tasks.issue_session_id != '' AND a2.issue_session_id = agent_tasks.issue_session_id)"
                    f"    OR (agent_tasks.issue_session_id = '' AND a2.agent_session_id = agent_tasks.agent_session_id)"
                    f"  )) ORDER BY created_at ASC LIMIT 1",
                    (runtime_id,)
                )
                row = c.fetchone()
                if not row:
                    # 定向任务都试过了，尝试池任务（非插件模式）
                    _is_plugin = False
                    try:
                        from src.api.event_pipeline import is_plugin_mode
                        _is_plugin = is_plugin_mode()
                    except Exception:
                        pass
                    if not _is_plugin:
                        c.execute(
                            f"SELECT id FROM agent_tasks "
                            f"WHERE runtime_id=0 AND pool_agent_type != '' AND status='queued' "
                            f"AND id NOT IN ({_tried_str}) "
                            f"AND NOT EXISTS ("
                            f"  SELECT 1 FROM agent_tasks a2 WHERE a2.status IN ('dispatched','running') AND ("
                            f"    (agent_tasks.issue_session_id != '' AND a2.issue_session_id = agent_tasks.issue_session_id)"
                            f"    OR (agent_tasks.issue_session_id = '' AND a2.agent_session_id = agent_tasks.agent_session_id)"
                            f"  )) ORDER BY created_at ASC LIMIT 1"
                        )
                        row = c.fetchone()
                    if not row:
                        db.commit()
                        return None
                task_id = row[0]
                _tried_ids.append(task_id)
                if len(_tried_ids) >= _max_tries:
                    db.commit()
                    return None
            c.execute(
                "UPDATE agent_tasks SET status='running', runtime_id=?, started_at=? WHERE id=? AND status='queued'",
                (runtime_id, datetime.now(timezone.utc).isoformat(), task_id))
            claimed = c.rowcount > 0
            if claimed:
                # 方案 D：展示快照重算（真实 active）；决策点已改读 _real_active_count，本段旧对账/递增不再承担正确性。
                refresh_busy_snapshot(runtime_id, cursor=c)
                # 方案 E：真正被引擎认领才把事件置 running（入队时是 pending）
                try:
                    _evt_seq = c.execute("SELECT event_seq FROM agent_tasks WHERE id=?", (task_id,)).fetchone()
                    if _evt_seq and _evt_seq[0]:
                        c.execute(
                            "UPDATE events SET status='running', completed_at='' "
                            "WHERE seq=? AND status IN ('pending','running')",
                            (_evt_seq[0],))
                except Exception as _ee:
                    log.warning(f"claim: sync event running failed for task {task_id}: {_ee}")
            db.commit()
        except Exception as e:
            log.warning(f"claim failed: {e}")
            db.rollback()
            return None
        if not claimed:
            return None
        task = get_task(task_id)
        if task:
            task["status"] = "running"
            # session 引擎守卫（worker 兜底层）：任务带的续接指针若由异构引擎产出（如
            # 入队时广播模式未知引擎，或存量脏指针），claim 已确定实际引擎 → 比对
            # issue_agent_session.session_engine，跨引擎则置空指针（引擎开新会话），
            # 防 hermes resume opencode 会话号静默挂死。引擎名未知 → 放行（fail-safe）。
            try:
                _isid = task.get("issue_session_id") or ""
                _sid = task.get("runtime_session_id") or ""
                if _isid and _sid:
                    _eng_name = ""
                    with db_lock:
                        c.execute("SELECT name FROM agent_runtimes WHERE id=?", (runtime_id,))
                        _er = c.fetchone()
                        _eng_name = (_er[0] or "") if _er else ""
                    with db_lock:
                        c.execute("SELECT session_engine FROM issue_agent_session WHERE id=?", (_isid,))
                        _sr = c.fetchone()
                    _ptr_engine = (_sr[0] or "") if _sr else ""
                    if _eng_name and _ptr_engine and _ptr_engine != _eng_name:
                        task["runtime_session_id"] = ""
                        log.info(f"claim: drop cross-engine session pointer "
                                 f"(ptr={_ptr_engine} engine={_eng_name} task={task_id})")
            except Exception as e:
                log.warning(f"claim session engine guard failed: {e}")
            # 回填事件 runtime 为真实引擎实例名（pool 模式入队显示 pool:<type>，
            # claim 后展示实际执行引擎，见 #44：vod 池 hermes 执行但事件显示 opencode）
            if task.get("event_seq"):
                try:
                    from src.runtime.instances import runtime_instance_name as _rin
                    with db_lock:
                        c = db.cursor()
                        c.execute("UPDATE events SET runtime=? WHERE seq=?",
                                  (_rin(runtime_id), task["event_seq"]))
                        db.commit()
                except Exception as e:
                    log.warning(f"claim backfill event runtime failed: {e}")
        from src.runtime.instances import runtime_instance_name
        log.info(f"claim: runtime_id={runtime_id}({runtime_instance_name(runtime_id)}) task={task_id}")
        # R21.12.1: 路径 A task 排队期间 token 过期窗口——claim 时检查过期标记
        # task 行 token 非空时检查 DB 过期标记（不调 VCS API，只查 DB）
        if task and (task.get("repo_gitcode_token") or ""):
            try:
                _rk = task.get("repo_key") or ""
                if _rk:
                    from src.token_resolver import TokenResolver, create_provider_from_repo_key
                    _provider = create_provider_from_repo_key(_rk)
                    _resolver = TokenResolver(_provider)
                    # R21.12.6: 只查 DB 过期标记，不调 VCS API
                    for _strategy in _resolver.strategies:
                        if _strategy._fetch(_rk) and _strategy._is_expired(_rk):
                            # R21.12.2: 已过期 → 重新调 get_token → 短路返回空则注入空串
                            _fresh_token = _resolver.get_token(_rk)
                            task["repo_gitcode_token"] = _fresh_token
                            if not _fresh_token:
                                log.warning(f"claim: task {task_id} token expired and short-circuited → empty token injected")
                            else:
                                log.info(f"claim: task {task_id} token expired, re-fetched from fallback strategy")
                            break
            except Exception as _te:
                log.warning(f"claim: token freshness check failed for task {task_id}: {_te}")
        # P0-1: 路径 D × 远程 worker token 注入
        # 路径 B/D 场景 task 行 repo_gitcode_token 刻意写空（防过期 token 落库）。
        # 本地引擎运行时 _get_task_token 会实时查主控 DB 取 token——但远程 worker
        # 用的是自己的本地 sessions.db（空库），查不到 token → git worktree 全链路断裂。
        # 修复：claim 响应中服务端现取 token 注入 task dict（不落库），worker 拿到的是
        # 新鲜 token，直接用 task["repo_gitcode_token"] 即可，无需查本地 DB。
        if task and not (task.get("repo_gitcode_token") or ""):
            try:
                task["repo_gitcode_token"] = _get_task_token(task)
            except Exception as _te:
                log.warning(f"claim: inject token failed for task {task_id}: {_te}")
        # 可观察性埋点：任务被 claim/dispatched
        try:
            from src.observability import record_task_dispatched
            if task:
                _queue_wait = -1
                if task.get("created_at"):
                    try:
                        _created = datetime.fromisoformat(task["created_at"])
                        _queue_wait = (datetime.now(timezone.utc) - _created).total_seconds()
                    except Exception:
                        pass
                record_task_dispatched(task_id, task.get("event_type") or task.get("agent_type") or "other",
                                       runtime_id, _queue_wait)
        except Exception:
            pass
        return task


def get_task(task_id) -> dict | None:
    with db_lock:
        c = db.cursor()
        _cols = [x for x in _TASK_COLUMNS_LIST if x in _table_columns("agent_tasks")]
        c.execute(f"SELECT {', '.join(_cols)} FROM agent_tasks WHERE id=?", (task_id,))
        row = c.fetchone()
    return dict(zip(_cols, row)) if row else None


class _TaskTokenLockSkipped(Exception):
    """已存在未结束事务，无法取得 SQLite 写锁：本轮同步 fail-closed（不触碰调用方事务）。"""


_SESSION_TOKEN_LOCK_TIMEOUT = 10  # 秒：同任务跨进程轮换锁等待上限

# Issue #990 复审：GET_LOCK 是连接级锁，连接一断就释放，因此这条专用连接可以**跨次复用**。
# 实测（MySQL 8.0.43 RDS）每次新建/关闭连接 + 加解一次锁 p50=8.4ms，复用后 p50=0.22ms；
# 每 50 次同步省 408ms（97%）。池外的语义不变：绝不用池借出的连接持锁。
# guard 保证「同进程内一次只有一个线程持锁」——同一连接上对同名 GET_LOCK 会重入计数，
# 若两个线程交错加解就会漏释放，其它进程被永久卡住。
_lock_conn_state: dict = {"conn": None, "guard": threading.Lock()}


def _locked_connection():
    """取复用的专用锁连接（失效则重建）。失败返回 None（调用方 fail-closed）。"""
    conn = _lock_conn_state.get("conn")
    if conn is not None:
        try:
            cur = conn.cursor()
            cur.execute("SELECT 1")
            cur.fetchone()
            return conn
        except Exception as e:
            log.warning(f"sync_task_session_token: lock conn unhealthy, reconnecting: {e}")
            try:
                conn.close()
            except Exception:
                pass
            _lock_conn_state["conn"] = None
    factory = getattr(db, "dedicated_connection", None)
    if factory is None:
        log.warning("sync_task_session_token: mysql backend without dedicated_connection; "
                    "cannot serialize cross-process token rotation")
        return None
    try:
        conn = factory()
    except Exception as e:
        log.warning(f"sync_task_session_token: dedicated lock conn failed: {e}")
        return None
    _lock_conn_state["conn"] = conn
    return conn


def _drop_locked_connection():
    """连接出错/断链时丢弃，下次重建（连接断开即释放其上的命名锁）。"""
    conn = _lock_conn_state.get("conn")
    _lock_conn_state["conn"] = None
    if conn is not None:
        try:
            conn.close()
        except Exception:
            pass


def _session_token_lock_name(task_id: str) -> str:
    """MySQL 命名锁名（≤64 字符）：按库名隔离，避免同一 MySQL 实例多环境互相排队。"""
    dbname = "default"
    try:
        from src.mysql_compat import mysql_config
        dbname = (mysql_config().get("database") or "default")
    except Exception:
        pass
    # MySQL 锁名长度上限 64；task_id 通常 < 32，库名截断兜底。
    return ("grape_sess_tk_%s_%s" % (task_id, dbname))[:64]


def _acquire_task_token_lock(task_id: str):
    """在复用的池外专用连接上获取 MySQL 服务器级命名锁（跨进程串行化）。

    为什么不用 ``SELECT ... FOR UPDATE``：``src/mysql_compat.py`` 对任何 SELECT
    执行后立即 ``commit()``，行锁随即释放，多 worker 仍可并发轮换互相作废旧 token。
    GET_LOCK 是连接级、服务器级互斥，且锁连接独立于业务连接，可稳定覆盖读+轮换+回写
    整个临界区（对应 QA 建议的「分布式锁」方案）。

    返回 (locked_conn, lock_name)；获取失败返回 (None, "")，调用方必须 fail-closed。
    进程内的 guard 先串行化（同一连接重入计数会漏释放），再用 GET_LOCK 跨进程互斥。
    """
    guard = _lock_conn_state["guard"]
    if not guard.acquire(timeout=_SESSION_TOKEN_LOCK_TIMEOUT):
        log.warning("sync_task_session_token: in-process token lock busy; fail closed without rotating")
        return None, ""
    try:
        dconn = _locked_connection()
        if dconn is None:
            guard.release()
            return None, ""
        lock_name = _session_token_lock_name(task_id)
        try:
            cur = dconn.cursor()
            cur.execute("SELECT GET_LOCK(%s, %s)", (lock_name, _SESSION_TOKEN_LOCK_TIMEOUT))
            row = cur.fetchone()
            if not row or row[0] != 1:
                log.warning(f"sync_task_session_token: mysql named lock not acquired ({lock_name}); "
                            "fail closed without rotating")
                guard.release()
                return None, ""
            return dconn, lock_name
        except Exception as e:
            log.warning(f"sync_task_session_token: mysql named lock error ({lock_name}): {e}")
            _drop_locked_connection()
            guard.release()
            return None, ""
    except Exception:
        guard.release()
        raise


def _release_task_token_lock(dconn, lock_name: str):
    """释放命名锁并归还进程内 guard（连接保留复用；断链时下次自动重建）。"""
    try:
        cur = dconn.cursor()
        cur.execute("SELECT RELEASE_LOCK(%s)", (lock_name,))
        cur.fetchone()
    except Exception as e:
        log.warning(f"sync_task_session_token: release lock {lock_name} failed (conn drop will release): {e}")
        _drop_locked_connection()
    finally:
        try:
            _lock_conn_state["guard"].release()
        except RuntimeError:
            # 未持锁（异常路径已释放）：无需处理
            pass


def sync_task_session_token(task_id: str) -> str:
    """同步任务的会话 token：保证 shell 环境快照与运行时注入值一致（Issue #990）。

    `agent_tasks.env_extra` 是任务创建时的 shell 环境快照；运行时进程执行前会经
    `GET /tasks/{id}/env` 刷新 env。若两端各自签发 token，快照会保留旧值 → Agent 用
    旧 token 调 grape 全量 401。

    规则：
    - 快照 token 有效（session_key / repo_key / agent_type 匹配且未过期）→ 原样返回，不轮换；
    - 失效或作用域不匹配 → 在同一事务内作废旧 token、签发新 token 并回写
      `agent_tasks.env_extra`，使快照与运行时注入值保持一致。

    跨进程原子性：SQLite 用 ``BEGIN IMMEDIATE`` 取库级写锁；MySQL 用池外专用连接上的
    ``GET_LOCK`` 命名锁串行化（服务端多 worker 运行，线程级 ``db_lock`` 不足以覆盖跨进程
    竞争；兼容层的 ``FOR UPDATE`` 会被 SELECT 后自动 commit 提前放锁，故不可用）。锁不可
    获取时 fail-closed 返回空串。过期、签发、快照回写共享同一事务；任一步失败即回滚并
    返回空串，绝不返回未落库的新 token。

    返回同步后的 token；任务不存在、无仓库/agent 上下文、取锁失败或持久化失败时返回空串。
    """
    if not task_id:
        return ""
    from src.api.auth_token import verify_session_token, rotate_session_token
    from src.session_manager import _is_mysql_backend
    is_mysql = _is_mysql_backend()
    lock_conn, lock_name = (None, "")
    if is_mysql:
        lock_conn, lock_name = _acquire_task_token_lock(task_id)
        if lock_conn is None:
            return ""
    try:
        with db_lock:
            c = db.cursor()
            try:
                if is_mysql:
                    # 已持有服务器级命名锁 → 临界区内普通 SELECT 即可（无需 FOR UPDATE）。
                    row = c.execute(
                        "SELECT repo_key, agent_type, user_id, env_extra FROM agent_tasks WHERE id=?",
                        (task_id,),
                    ).fetchone()
                else:
                    try:
                        c.execute("BEGIN IMMEDIATE")
                    except Exception as e:
                        if "within a transaction" in str(e).lower():
                            # 已有未结束事务：无法取得写锁，fail-closed；不回滚调用方事务。
                            raise _TaskTokenLockSkipped(str(e))
                        raise
                    row = c.execute(
                        "SELECT repo_key, agent_type, user_id, env_extra FROM agent_tasks WHERE id=?",
                        (task_id,),
                    ).fetchone()
                if not row:
                    db.commit()
                    return ""
                repo_key, agent_type = (row[0] or ""), (row[1] or "")
                try:
                    user_id = int(row[2] or 0)
                except (TypeError, ValueError):
                    user_id = 0
                try:
                    env = json.loads(row[3] or "{}")
                except Exception:
                    env = {}
                if not isinstance(env, dict):
                    env = {}
                if not repo_key or not agent_type:
                    db.commit()
                    return ""

                snapshot_token = str(env.get("GRAPE_SESSION_TOKEN") or "")
                info = verify_session_token(snapshot_token) if snapshot_token else None
                token_ok = bool(
                    info
                    and info.get("session_key") == task_id
                    and info.get("repo_key") == repo_key
                    and info.get("agent_type") == agent_type
                )
                if token_ok:
                    db.commit()
                    return snapshot_token

                new_token = rotate_session_token(
                    session_key=task_id, repo_key=repo_key,
                    agent_type=agent_type, user_id=user_id, commit=False)
                env["GRAPE_SESSION_TOKEN"] = new_token
                c.execute(
                    "UPDATE agent_tasks SET env_extra=? WHERE id=?",
                    (json.dumps(env, ensure_ascii=False), task_id),
                )
                db.commit()
                return new_token
            except _TaskTokenLockSkipped as e:
                log.warning(f"sync_task_session_token: sqlite write lock unavailable for {task_id}: {e}")
                return ""
            except Exception as e:
                try:
                    db.rollback()
                except Exception:
                    pass
                log.warning(f"sync_task_session_token: sync failed for {task_id}: {e}")
                return ""
    finally:
        if lock_conn is not None:
            _release_task_token_lock(lock_conn, lock_name)


def session_token_for_execution(task_id: str, *, session_key_fallback: str,
                                repo_key: str, agent_type: str,
                                user_id: int = 0) -> str:
    """direct 执行前的 token 解析：优先复用/同步任务快照，绝不制造未落库 token。

    - 有任务行：走 ``sync_task_session_token``，失败时返回空串（不另签未回写快照的 token，
      否则运行时注入值与 shell 环境快照再次漂移，正是 #990 的症状）；
    - 无任务行（如纯 direct 会话）：按 ``session_key_fallback`` 新签。
    """
    if task_id:
        token = sync_task_session_token(task_id)
        if token:
            return token
        if get_task(task_id) is not None:
            log.warning(f"session_token_for_execution: task {task_id} snapshot sync failed; "
                        "skip issuing an unpersisted token")
            return ""
        session_key = task_id
    else:
        session_key = session_key_fallback
    from src.api.auth_token import issue_session_token
    return issue_session_token(
        session_key=session_key, repo_key=repo_key,
        agent_type=agent_type, user_id=user_id)


def lookup_task_by_event_seq(runtime_id: int, event_seq: int) -> dict | None:
    """许可对号 lookup（Issue #520 Spec v2 §4，XOcto 补充设计）。

    纯验证性 SELECT，零副作用（不翻状态、不 UPDATE、不返回 payload/env/凭据）：
    按 expected_event_seq 精确匹配，候选 WHERE 骨架与 claim 定向分支完全一致
    （runtime_id 精确匹配 + 共享池 runtime_id=0 AND pool_agent_type != '' +
    串行槽 NOT EXISTS + status='queued'）。命中返回身份字段
    {task_id, issue_id, event_seq, agent_type, created_at}，未命中返回 None。
    仅支持 event_seq > 0 的新鲜任务（旧任务 event_seq 为空/0 不支持）。
    """
    if not event_seq or int(event_seq) <= 0:
        return None
    with db_lock:
        c = db.cursor()
        try:
            c.execute("""
                SELECT id, issue_id, event_seq, agent_type, created_at FROM agent_tasks
                WHERE event_seq=? AND status='queued'
                  AND (runtime_id=? OR (runtime_id=0 AND pool_agent_type != ''))
                  AND NOT EXISTS (
                    SELECT 1 FROM agent_tasks a2
                    WHERE a2.status IN ('dispatched','running')
                      AND (
                        (agent_tasks.issue_session_id != '' AND a2.issue_session_id = agent_tasks.issue_session_id)
                        OR
                        (agent_tasks.issue_session_id = '' AND a2.agent_session_id = agent_tasks.agent_session_id)
                      )
                  )
                LIMIT 1
            """, (int(event_seq), runtime_id))
            row = c.fetchone()
        except Exception as e:
            log.warning(f"lookup_task_by_event_seq failed: {e}")
            return None
    if not row:
        return None
    return {"task_id": row[0], "issue_id": row[1], "event_seq": row[2],
            "agent_type": row[3], "created_at": row[4]}


def claim_by_tag(tag: str, runtime_id: int,
                 expected_task_id=None, expected_event_seq=None) -> dict | None:
    """按标签认领任务 + scope 隔离（event-dispatch-redesign-v2.md §2.4）

    scope=global → 可认领所有仓库的 queued 任务
    scope=repo   → 只认领 owner_id 对应仓库的 queued 任务
    scope=user   → 只认领该用户创建的 queued 任务

    Issue #520 bound claim：expected_task_id + expected_event_seq 成对提供时，
    在同一 db_lock 临界区内对目标任务做原子身份校验（tag/scope/串行槽条件与
    FIFO 完全一致 + expected 精确匹配）；未命中返回 None，绝不回退 FIFO。
    """
    with db_lock:
        c = db.cursor()
        try:
            # 1. 校验标签能力 + 获取引擎 scope
            # 标签匹配：精确 tag OR "all"（all 可认领任意 pool_tag 任务）
            c.execute(
                "SELECT ec.tag, rc.scope, rc.owner_id, rc.owner_name "
                "FROM engine_capabilities ec "
                "JOIN agent_runtimes ar ON ar.id = ec.runtime_id "
                "LEFT JOIN runtime_credentials rc ON rc.id = ar.credential_id "
                "WHERE ec.runtime_id=? AND ec.enabled=1 AND (ec.tag=? OR ec.tag='all')",
                (runtime_id, tag)
            )
            row = c.fetchone()
            if not row:
                return None
            scope = row[1] or "global"
            owner_id = row[2] or ""
            owner_name = row[3] or ""

            # 2. 引擎级并发槽检查（方案一：标签只做路由，并发纯引擎级）
            # 废弃 engine_capabilities.max_concurrent，统一走 agent_runtimes.max_concurrent
            # 方案 D（2026-10-18）：满载判定以真实 running/dispatched COUNT 为事实源（covering 索引），
            # 不再读 busy_slots 列——busy_slots 仅为展示快照，僵尸残留不阻塞认领。
            c.execute(
                "SELECT max_concurrent FROM agent_runtimes WHERE id=?",
                (runtime_id,)
            )
            rt_row = c.fetchone()
            if rt_row and rt_row[0] and _real_active_count(c, runtime_id) >= rt_row[0]:
                return None  # 引擎已满载（真实 active >= max_concurrent）

            # 3. 构建任务查询（加 scope 过滤）
            # tag="all" → 匹配任意 pool_tag（广播）；其他 tag → 精确匹配 pool_tag
            # 只认领无主池任务（runtime_id=0），不抢已定向的任务
            _tag_match = "(t.pool_tag!='' AND t.runtime_id=0)" if tag == "all" else "(t.pool_tag=? AND t.runtime_id=0)"
            _tag_match_noprefix = "(pool_tag!='' AND runtime_id=0)" if tag == "all" else "(pool_tag=? AND runtime_id=0)"
            params = [] if tag == "all" else [tag]
            if scope == "repo":
                # repo_key 可能带平台前缀（gitcode:/github:）也可能不带，三种都要匹配
                # （跨平台候选与 runtime.py list_runtime_tasks 同款）
                params.append(owner_id)
                params.append(f"gitcode:{owner_id}")
                params.append(f"github:{owner_id}")
                sql = (
                    "SELECT t.id FROM agent_tasks t "
                    f"WHERE { _tag_match} AND t.status='queued' AND (t.repo_key=? OR t.repo_key=? OR t.repo_key=?) "
                    "AND NOT EXISTS ("
                    "  SELECT 1 FROM agent_tasks a2 WHERE a2.status IN ('dispatched','running') AND ("
                    "    (t.issue_session_id != '' AND a2.issue_session_id = t.issue_session_id)"
                    "    OR (t.issue_session_id = '' AND a2.agent_session_id = t.agent_session_id)"
                    "  )) ORDER BY t.created_at ASC LIMIT 1"
                )
            elif scope == "user":
                c.execute("SELECT id FROM grape_accounts WHERE LOWER(username)=LOWER(?)", (owner_name,))  # D32-补丁 #348: case-insensitive
                uid_row = c.fetchone()
                uid = uid_row[0] if uid_row else 0
                params.append(uid)
                sql = (
                    "SELECT t.id FROM agent_tasks t "
                    f"WHERE { _tag_match} AND t.status='queued' AND t.user_id=? "
                    "AND NOT EXISTS ("
                    "  SELECT 1 FROM agent_tasks a2 WHERE a2.status IN ('dispatched','running') AND ("
                    "    (t.issue_session_id != '' AND a2.issue_session_id = t.issue_session_id)"
                    "    OR (t.issue_session_id = '' AND a2.agent_session_id = t.agent_session_id)"
                    "  )) ORDER BY t.created_at ASC LIMIT 1"
                )
            else:
                sql = (
                    "SELECT id FROM agent_tasks "
                    f"WHERE {_tag_match_noprefix} AND status='queued' "
                    "AND NOT EXISTS ("
                    "  SELECT 1 FROM agent_tasks a2 WHERE a2.status IN ('dispatched','running') AND ("
                    "    (agent_tasks.issue_session_id != '' AND a2.issue_session_id = agent_tasks.issue_session_id)"
                    "    OR (agent_tasks.issue_session_id = '' AND a2.agent_session_id = agent_tasks.agent_session_id)"
                    "  )) ORDER BY created_at ASC LIMIT 1"
                )
            if expected_task_id is not None:
                # Issue #520 bound claim（tag 版）：expected 身份 + 原有 tag/scope/串行槽
                # 条件在同一 db_lock 临界区原子校验（scope SQL 与 FIFO 完全同源，仅在
                # WHERE 尾部追加 expected 精确匹配）；未命中 → 无任务，绝不回退 FIFO。
                c.execute(
                    sql.replace(" ORDER BY", " AND id=? AND event_seq=? ORDER BY"),
                    tuple(list(params) + [expected_task_id, expected_event_seq]))
                task_row = c.fetchone()
                if not task_row:
                    return None
                task_id = task_row[0]
                # pool_engine_ids 广播范围守卫：目标不含本引擎 → fail-closed（不尝试其他任务）
                c.execute("SELECT pool_engine_ids FROM agent_tasks WHERE id=?", (task_id,))
                _pe_row = c.fetchone()
                if _pe_row and _pe_row[0]:
                    try:
                        _allowed = json.loads(_pe_row[0])
                        if _allowed and runtime_id not in _allowed:
                            return None
                    except Exception:
                        pass
            else:
                c.execute(sql, tuple(params))
                task_row = c.fetchone()
                if not task_row:
                    return None
                task_id = task_row[0]

            # pool_engine_ids 限定广播范围：非空时只有列表中的引擎能认领
            # 修复（2026-08-29）：原代码 return None 会阻塞队列——队首任务如果不匹配，
            # 后面的任务永远不会被尝试。改为循环跳过不匹配的任务，继续尝试下一个。
            # pool_engine_ids 是 JSON 数组如 "[76]"，SQL 难以直接过滤，
            # 这里用 Python 循环 LIMIT N 逐个检查。
            # Issue #658：认领侧角色守卫——引擎有效角色集（显式 role_tags > 引擎类型默认映射）
            # 非空时，任务 pool_tag（已知节点角色）必须 ∈ 角色集才可认领（堵全量广播漏洞：
            # #90 vod 任务被 codearts 引擎按 tag='all' 认领 → rc=1）。角色集为 None
            # （未知类型且未显式声明）不限制；pool_tag 非已知角色（自定义路由 tag）不受约束。
            _engine_roles = None
            try:
                from src.runtime.engine_select import engine_role_tags as _ert, is_agent_role as _is_role
                _er_row = c.execute(
                    "SELECT name, COALESCE(role_tags,'') FROM agent_runtimes WHERE id=?",
                    (runtime_id,)).fetchone()
                if _er_row:
                    _engine_roles = _ert(_er_row[0] or "", _er_row[1] or "")
            except Exception as _ere:
                log.warning(f"claim_by_tag: role tags lookup failed for engine {runtime_id}: {_ere}")
                _engine_roles = None
            _tried_ids = [task_id]
            _max_tries = 20  # 防止全表扫描
            while True:
                if expected_task_id is not None:
                    # Issue #520 bound claim：目标已原子校验（含 pool_engine_ids 守卫），
                    # 此处补角色硬校验：不匹配 → fail-closed（不尝试其他任务）
                    if _engine_roles is not None:
                        _bt_row = c.execute("SELECT pool_tag FROM agent_tasks WHERE id=?", (task_id,)).fetchone()
                        _bt_tag = (_bt_row[0] or "").strip().lower() if _bt_row else ""
                        if _is_role(_bt_tag) and _bt_tag not in _engine_roles:
                            log.warning(f"claim_by_tag: engine {runtime_id} roles={_engine_roles} "
                                        f"mismatch task {task_id} pool_tag={_bt_tag} (Issue #658), reject bound claim")
                            return None
                    break
                c.execute("SELECT pool_engine_ids, pool_tag FROM agent_tasks WHERE id=?", (task_id,))
                _pe_row = c.fetchone()
                _task_pool_tag = ""
                if _pe_row:
                    _task_pool_tag = (_pe_row[1] or "").strip().lower()
                _skip = False
                if (_engine_roles is not None and _is_role(_task_pool_tag)
                        and _task_pool_tag not in _engine_roles):
                    # Issue #658：角色不匹配 → 跳过该任务继续尝试下一个（与 pool 名单
                    # 守卫同款跳过语义，不阻塞队列，也绝不认领不匹配角色任务）
                    log.info(f"claim_by_tag: engine {runtime_id} roles={_engine_roles} mismatch "
                             f"task {task_id} pool_tag={_task_pool_tag} (Issue #658), skip")
                    _skip = True
                if _pe_row and _pe_row[0]:
                    import json as _json
                    try:
                        _allowed = _json.loads(_pe_row[0])
                        if _allowed and runtime_id not in _allowed:
                            # 兜底：检查 pool_engine_ids 中的引擎是否全部不可达
                            # 不可达 = 离线 / 无对应 tag 能力 / scope 不匹配
                            # 全部不可达时清空 pool_engine_ids，让 task 可被任意引擎认领
                            _task_tag = ""
                            _task_repo = ""
                            _task_uid = 0
                            try:
                                c.execute("SELECT pool_tag, repo_key, user_id FROM agent_tasks WHERE id=?", (task_id,))
                                _trow = c.fetchone()
                                if _trow:
                                    _task_tag = _trow[0] or ""
                                    _task_repo = _trow[1] or ""
                                    _task_uid = _trow[2] or 0
                            except Exception:
                                pass
                            _has_capable = _check_pool_engines_reachable(_allowed, _task_tag, _task_repo, task_user_id=_task_uid)
                            if not _has_capable:
                                # Issue #1487 改动C：池内全不可达只是单次瞬时快照的结论，
                                # 不在此处直接判死——保持 queued，交 _check_stale_queued_tasks
                                # 的既有阈值窗口（120s）统一裁决（同 claim 定向分支）。
                                log.info(f"claim_by_tag: task {task_id} pool engines {_allowed} all "
                                         f"unreachable in this snapshot, keep queued for "
                                         f"_check_stale_queued_tasks (Issue #1487)")
                                _skip = True
                                # Issue #605：引擎不在广播名单被静默跳过 → 取证日志（60s 去重）
                                _log_pool_skip_once(task_id, runtime_id, _allowed)
                            else:
                                _skip = True
                                # Issue #605：引擎不在广播名单被静默跳过 → 取证日志（60s 去重）
                                _log_pool_skip_once(task_id, runtime_id, _allowed)
                    except Exception:
                        pass
                if not _skip:
                    # === 健康分检查（Plugin 模式增强层，Issue #134 扩展）===
                    # 引擎健康分过低或连续失败过多时拒绝认领，避免低质量引擎抢任务后失败。
                    # Issue #259/seq#2310：force_refresh=True 现算，避免读 5min 前持久化的 0 分
                    # 快照长期阻塞本机在线引擎；不健康 → 跳过当前任务尝试下一个（防队首阻塞）。
                    # 健康检查异常时保守跳过本轮认领，不放行低分引擎。
                    try:
                        from src.runtime.health_scorer import is_healthy as _is_engine_healthy
                        if not _is_engine_healthy(runtime_id, force_refresh=True):
                            from src.runtime.instances import runtime_instance_name as _rin2
                            log.info(f"claim_by_tag: engine {runtime_id}({_rin2(runtime_id)}) unhealthy (low score/consecutive failures), skip claiming task {task_id}")
                            _skip = True
                    except Exception as _he:
                        log.warning(f"claim_by_tag: health check failed for engine {runtime_id}: {_he}, reject this round")
                        _skip = True
                if not _skip:
                    break  # 当前任务可认领

                # 跳过当前任务，尝试下一个
                _tried_str = ','.join(["'"+x+"'" for x in _tried_ids])
                if scope == "repo":
                    # 2026-10-23 认领重试 SQL 修复：重试段是首轮查询的手工复制，
                    # 漏抄了第三种仓库写法（params 4 值 vs 2 坑 → MySQLdb
                    # ProgrammingError，claim_by_tag 整体异常退出）。必须与首轮
                    # （上方 repo_key=? x3）同口径：tag 1 坑 + 仓库 3 坑 = 4 值。
                    c.execute(
                        f"SELECT t.id FROM agent_tasks t "
                        f"WHERE {_tag_match} AND t.status='queued' AND (t.repo_key=? OR t.repo_key=? OR t.repo_key=?) "
                        f"AND t.id NOT IN ({_tried_str}) "
                        f"AND NOT EXISTS ("
                        f"  SELECT 1 FROM agent_tasks a2 WHERE a2.status IN ('dispatched','running') AND ("
                        f"    (t.issue_session_id != '' AND a2.issue_session_id = t.issue_session_id)"
                        f"    OR (t.issue_session_id = '' AND a2.agent_session_id = t.agent_session_id)"
                        f"  )) ORDER BY t.created_at ASC LIMIT 1",
                        (*params,)
                    )
                elif scope == "user":
                    c.execute(
                        f"SELECT t.id FROM agent_tasks t "
                        f"WHERE {_tag_match} AND t.status='queued' AND t.user_id=? "
                        f"AND t.id NOT IN ({_tried_str}) "
                        f"AND NOT EXISTS ("
                        f"  SELECT 1 FROM agent_tasks a2 WHERE a2.status IN ('dispatched','running') AND ("
                        f"    (t.issue_session_id != '' AND a2.issue_session_id = t.issue_session_id)"
                        f"    OR (t.issue_session_id = '' AND a2.agent_session_id = t.agent_session_id)"
                        f"  )) ORDER BY t.created_at ASC LIMIT 1",
                        (*params,)
                    )
                else:
                    c.execute(
                        f"SELECT id FROM agent_tasks "
                        f"WHERE {_tag_match_noprefix} AND status='queued' "
                        f"AND id NOT IN ({_tried_str}) "
                        f"AND NOT EXISTS ("
                        f"  SELECT 1 FROM agent_tasks a2 WHERE a2.status IN ('dispatched','running') AND ("
                        f"    (agent_tasks.issue_session_id != '' AND a2.issue_session_id = agent_tasks.issue_session_id)"
                        f"    OR (agent_tasks.issue_session_id = '' AND a2.agent_session_id = agent_tasks.agent_session_id)"
                        f"  )) ORDER BY created_at ASC LIMIT 1",
                        tuple(params)
                    )
                task_row = c.fetchone()
                if not task_row:
                    return None  # 所有任务都试过了，没有可认领的
                task_id = task_row[0]
                _tried_ids.append(task_id)
                if len(_tried_ids) >= _max_tries:
                    return None  # 防止无限循环

            # 标记 running（原 dispatched：worker 认领即开始执行，UI 长时间显示 dispatched
            # 是因为 worker 侧 task_start 回调丢失/批量消息延迟，靠 auto-fix 兜底才转 running。
            # 认领即 running 语义更准确，防 UI 卡 dispatched 窗口）
            c.execute(
                "UPDATE agent_tasks SET status='running', runtime_id=?, started_at=? WHERE id=? AND status='queued'",
                (runtime_id, datetime.now(timezone.utc).isoformat(), task_id)
            )
            claimed = c.rowcount > 0
            if claimed:
                # 方案 D：busy_slots 降级为展示快照，这里重算为真实 active 数（决策已用 _real_active_count）
                refresh_busy_snapshot(runtime_id, cursor=c)
                # 方案 E：真正被引擎认领才把事件置 running（入队时是 pending）
                try:
                    _evt_seq = c.execute("SELECT event_seq FROM agent_tasks WHERE id=?", (task_id,)).fetchone()
                    if _evt_seq and _evt_seq[0]:
                        c.execute(
                            "UPDATE events SET status='running', completed_at='' "
                            "WHERE seq=? AND status IN ('pending','running')",
                            (_evt_seq[0],))
                except Exception as _ee:
                    log.warning(f"claim_by_tag: sync event running failed for task {task_id}: {_ee}")
            db.commit()
        except Exception as e:
            log.warning(f"claim_by_tag failed: {e}")
            db.rollback()
            return None
    if not claimed:
        return None
    task = get_task(task_id)
    if task:
        task["status"] = "running"
        # session 引擎守卫（worker 兜底层，与 claim() 同款）：广播任务 claim 时才确定
        # 实际引擎 → 比对 issue_agent_session.session_engine，跨引擎置空指针（引擎开新
        # 会话），防 hermes resume opencode 会话号静默挂死。引擎未知 → 放行（fail-safe）。
        try:
            _isid = task.get("issue_session_id") or ""
            _sid = task.get("runtime_session_id") or ""
            if _isid and _sid:
                _eng_name = ""
                with db_lock:
                    c.execute("SELECT name FROM agent_runtimes WHERE id=?", (runtime_id,))
                    _er = c.fetchone()
                    _eng_name = (_er[0] or "") if _er else ""
                with db_lock:
                    c.execute("SELECT session_engine FROM issue_agent_session WHERE id=?", (_isid,))
                    _sr = c.fetchone()
                _ptr_engine = (_sr[0] or "") if _sr else ""
                if _eng_name and _ptr_engine and _ptr_engine != _eng_name:
                    task["runtime_session_id"] = ""
                    log.info(f"claim_by_tag: drop cross-engine session pointer "
                             f"(ptr={_ptr_engine} engine={_eng_name} task={task_id})")
        except Exception as e:
            log.warning(f"claim_by_tag session engine guard failed: {e}")
        # 回填事件 runtime
        if task.get("event_seq"):
            try:
                from src.runtime.instances import runtime_instance_name as _rin
                with db_lock:
                    c = db.cursor()
                    c.execute("UPDATE events SET runtime=? WHERE seq=?", (_rin(runtime_id), task["event_seq"]))
                    db.commit()
            except Exception:
                pass
    from src.runtime.instances import runtime_instance_name
    log.info(f"claim_by_tag: tag={tag} runtime={runtime_id}({runtime_instance_name(runtime_id)}) task={task_id}")
    # Issue #372 §29: 路径 D × 远程 worker token 注入（与 claim() L1284-1286 一致）
    # 路径 B/D 场景 task 行 repo_gitcode_token 刻意写空（防过期 token 落库）。
    # 远程 worker 用本地空库查不到 token → git clone/push 失败。
    # 修复：claim_by_tag 返回前服务端现取 token 注入 task dict（不落库）。
    if task and not (task.get("repo_gitcode_token") or ""):
        try:
            task["repo_gitcode_token"] = _get_task_token(task)
        except Exception as _te:
            log.warning(f"claim_by_tag: inject token failed for task {task_id}: {_te}")
    return task


_BUSY_SLOT_DECREMENT_SQL = (
    "UPDATE agent_runtimes SET busy_slots = "
    "CASE WHEN busy_slots > 0 THEN busy_slots - 1 ELSE 0 END WHERE id=?"
)


def _real_active_count(cursor, runtime_id: int) -> int:
    """该引擎当前真实 running/dispatched 任务数（方案 D 唯一事实源）。

    local-engine-busy-slots-zombie-fix-design.md §十：决策点一律以 agent_tasks 真实计数为准，
    依赖 covering 索引 idx_agent_tasks_runtime_status 走纯索引扫描（COVERING INDEX），
    成本与当前 running 数同量级，与历史行数解耦。
    """
    cursor.execute(
        "SELECT COUNT(*) FROM agent_tasks "
        "WHERE runtime_id=? AND status IN ('running','dispatched','pending_self_report')",
        (runtime_id,))
    r = cursor.fetchone()
    return r[0] if r else 0


def refresh_busy_snapshot(runtime_id: int, cursor=None):
    """把 busy_slots 列重算为真实 active 数（展示快照，不参与调度决策）。

    方案 D：busy_slots 降级为展示快照，由调度周期 / claim 路径调用本函数刷新；
    任一递增/递减路径漏调都不影响决策正确性（决策读 _real_active_count）。
    """
    if not runtime_id:
        return
    if cursor is not None:
        _n = _real_active_count(cursor, runtime_id)
        cursor.execute("UPDATE agent_runtimes SET busy_slots=? WHERE id=?", (_n, runtime_id))
        return
    from src.session_manager import db as _db, db_lock as _dl
    with _dl:
        _n = _real_active_count(_db.cursor(), runtime_id)
        _db.cursor().execute("UPDATE agent_runtimes SET busy_slots=? WHERE id=?", (_n, runtime_id))
        _db.commit()


def release_busy_slot(runtime_id, cursor=None):
    """统一释放调度槽（唯一递减入口，防越界 clamp >=0）。

    - cursor 为空：自行获取 db_lock + commit（独立事务）；
    - 传入 cursor：复用调用方事务（与任务状态更新同事务，避免嵌套锁）。
    所有「任务不再占用某引擎槽位」的路径都必须经此收敛
    （complete/fail/cancel/requeue/redispatch/孤儿回收），
    避免僵尸 busy_slots 让 local_claimer/claim_by_tag 误判满载（#256）。
    """
    if not runtime_id:
        return
    if cursor is not None:
        cursor.execute(_BUSY_SLOT_DECREMENT_SQL, (runtime_id,))
        return
    from src.session_manager import db as _db, db_lock as _dl
    with _dl:
        _db.cursor().execute(_BUSY_SLOT_DECREMENT_SQL, (runtime_id,))
        _db.commit()


# ===== Issue #1071：重启后「活着却被判死」——活性判定 · 回炉守卫 · 迟到复活 =====
#
# 三个根因（同一份故障链）：
#   1) 优雅停机把 REMOTE 任务置 pending_self_report，pending_since=停机时刻；判死扫描只看
#      pending_since，worker 仍在产出也 30 分钟后一刀切 → 活着的任务被杀。
#   2) 自动回炉（reap/rerequeue）不看活性，回炉认领会改写 started_at，而 started_at 就是
#      worker 的 run_token → 老 worker 的上报被 409 stale 永久拒收。
#   3) 判死走 update_task_status，对 timeout_failed 也 revoke 会话 token → 之后连重试都 401。
# 修复口径统一为「任务最近一次产出消息」：worker 还活着就必然在写 task_messages，
# 这比引擎心跳更贴近任务本身（心跳断 ≠ 任务死）。

# 兜底超时来源标记：sweep 写入 agent_tasks.errors 首元素，供迟到结果复活时精确识别
# （避免把真业务失败也复活，重新打开 2026-08-24 seq#2488「失败被成功覆盖」的洞）。
TIMEOUT_SOURCE_MARKER = "PENDING_SELF_REPORT_TIMEOUT"

# 自动回炉前的活性窗口：最近 N 秒内有产出消息 → 视为仍在跑，不自动回炉
TASK_ACTIVITY_GUARD_SECONDS = int(os.environ.get("TASK_ACTIVITY_GUARD_SECONDS", "600"))

# 迟到结果复活窗口：从判死时刻（completed_at）起 N 秒内允许复活一次
TIMEOUT_REVIVE_WINDOW_SECONDS = int(os.environ.get("TIMEOUT_REVIVE_WINDOW_SECONDS", "1800"))


def _epoch_of(value) -> float | None:
    """库里时间串 → epoch 秒；不可解析返回 None（复用 health.parse_dt 的双后端口径）。"""
    if not value:
        return None
    try:
        from src.runtime.health import parse_dt
        _v = parse_dt(value)
        return float(_v) if _v is not None else None
    except Exception:
        return None


def task_last_activity(task_id) -> float | None:
    """任务最近一次产出消息的 epoch 秒；无消息 → None。

    取最近 20 条在 Python 侧取数值最大，而不是 SQL MAX(created_at)：历史行是无时区
    '%Y-%m-%d %H:%M:%S'，新行是带时区 ISO-8601（Issue #588），字符串比较会把 ' '(0x20)
    < 'T'(0x54) 误判（与 scheduler 判死扫描同一个坑）。
    """
    try:
        with db_lock:
            c = db.cursor()
            rows = c.execute(
                "SELECT created_at FROM task_messages WHERE task_id=? ORDER BY id DESC LIMIT 20",
                (task_id,)
            ).fetchall()
        _best = None
        for (_ts,) in rows or []:
            _e = _epoch_of(_ts)
            if _e is not None and (_best is None or _e > _best):
                _best = _e
        return _best
    except Exception as e:
        log.warning(f"task_last_activity failed for task {task_id}: {e}")
        return None


def task_recently_active(task_id, window_seconds: int | None = None) -> bool:
    """最近 window_seconds 内是否有产出消息（默认 TASK_ACTIVITY_GUARD_SECONDS）。

    回炉守卫的统一口径。窗口化是有意的：守卫不能永久拦住回炉，否则又回到
    seq#622「任务悬空卡死」；超过窗口仍无产出，照旧按原逻辑回收。
    """
    _win = TASK_ACTIVITY_GUARD_SECONDS if window_seconds is None else int(window_seconds)
    _ts = task_last_activity(task_id)
    if _ts is None:
        return False
    return _ts >= datetime.now(timezone.utc).timestamp() - _win


def validate_run_token(task: dict | None, run_token: str = "") -> str:
    """校验补报实例（run_token = 认领时刻 started_at，Step 4）。

    返回值：
    - "ok"   ：可继续处理（任务处于 running/dispatched，且 started_at 匹配）
    - "noop" ：已终态（幂等，调用方照旧走终态保护即可）
    - "stale"：旧执行实例补报（started_at 不匹配，或任务已回炉/未处于活动态）
      → 调用方应拒绝，防止 recover_orphans 回炉重跑后旧实例误 complete 新执行。
    """
    if not task or not run_token:
        return "ok"
    _st = task.get("status")
    # 终态（含 timeout_failed）优先判定：无论 run_token 是否匹配都算 noop，
    # 迟到上报不会把超时/失败终态误判成 stale 冲突。
    if _st in TASK_TERMINAL_STATUSES:
        return "noop"
    if task.get("started_at") != run_token:
        return "stale"
    # Issue #537：pending_self_report（主控停机等 worker 自报）视同活动态，
    # worker 完成/失败自报不得被补报守卫当 stale 拒收（否则停双主后自报 409 → 永败）。
    if _st not in ("running", "dispatched", TASK_PENDING_SELF_REPORT):
        return "stale"  # queued（回炉未认领）/ 其它 → 旧实例
    return "ok"


_TERMINAL_STATUSES = ("completed", "failed", "cancelled")

# Issue #587: 任务终态 → 事件终态级联收敛的 result 文案
_EVENT_CASCADE_RESULTS = {
    "cancelled": "任务已取消：级联收敛（cancel 不作为业务通过，保留事件独立终态）",
    "failed": "任务失败：级联收敛（update_task_status 补偿，未收到详写终态回报）",
    "completed": "任务已完成：级联收敛（回报丢失补偿）",
}


def _cascade_event_terminal(cursor, event_seq: int, status: str, exit_code: int = None):
    """Issue #587: 任务终态级联事件收敛（事务内，幂等，终态保护）。

    仅当事件仍处于活跃态（pending/running/dispatched）时同步为任务终态；
    已终态事件不被另一终态覆盖（与 event_store.update_event_status 同语义）。
    返回 True 表示本次发生收敛。
    """
    try:
        row = cursor.execute("SELECT status FROM events WHERE seq=?", (event_seq,)).fetchone()
        if not row:
            return False
        if row[0] in _TERMINAL_STATUSES:
            return False
        _now = datetime.now(timezone.utc).isoformat()
        _result = _EVENT_CASCADE_RESULTS.get(status, f"级联收敛：任务{status}")
        if status == "cancelled":
            cursor.execute(
                "UPDATE events SET status='cancelled', exit_code=-1, completed_at=?, result=? WHERE seq=?",
                (_now, _result, event_seq))
        elif status == "failed":
            cursor.execute(
                "UPDATE events SET status='failed', exit_code=COALESCE(?, 1), completed_at=?, result=? WHERE seq=?",
                (exit_code, _now, _result, event_seq))
        else:
            cursor.execute(
                "UPDATE events SET status='completed', exit_code=0, completed_at=?, result=? WHERE seq=?",
                (_now, _result, event_seq))
        # Issue #587 审查修复：级联收敛须同步释放 event_active_guard（与
        # update_event_status 终态路径语义一致），否则守卫残留只能靠 TTL 30min 兜底。
        # 在同一事务/游标内完成，避免嵌套获取不可重入的 db_lock。
        try:
            _g = cursor.execute(
                "SELECT repo_key, issue_id, agent_type FROM events WHERE seq=?",
                (event_seq,)).fetchone()
            if _g:
                cursor.execute(
                    "DELETE FROM event_active_guard WHERE repo_key=? AND issue_id=? "
                    "AND agent_type=? AND seq=?",
                    (_g[0], _g[1] or "#0", _g[2] or "", event_seq))
        except Exception as _ge:
            log.warning(f"_cascade_event_terminal: guard release failed for seq={event_seq}: {_ge}")
        return True
    except Exception as _e:
        log.warning(f"_cascade_event_terminal: failed for event {event_seq} -> {status}: {_e}")
        return False


def converge_event_to_task(task_id: str) -> dict:
    """Issue #587: 补偿收敛入口——按任务当前终态强制收敛关联事件（幂等，可重复调用）。

    供管理端补偿重试端点（routes/runtime.py /tasks/{id}/converge-event）与
    sweeper 兜底使用：任务已终态但事件仍 running/pending（回报丢失）时收敛。
    """
    task = get_task(task_id)
    if not task:
        return {"status": "not_found", "task_id": task_id}
    _seq = task.get("event_seq") or 0
    _task_status = task.get("status") or ""
    if not _seq:
        return {"status": "no_event", "task_id": task_id, "task_status": _task_status}
    if _task_status not in _TERMINAL_STATUSES:
        return {"status": "task_not_terminal", "task_id": task_id,
                "task_status": _task_status, "event_seq": _seq}
    with db_lock:
        c = db.cursor()
        c.execute("SELECT status FROM events WHERE seq=?", (_seq,))
        row = c.fetchone()
        before = row[0] if row else "(missing)"
        converged = _cascade_event_terminal(c, _seq, _task_status,
                                            exit_code=task.get("exit_code"))
        db.commit()
    # Issue #1667：直接 SQL 收敛终态后补挂排队 drain（旁路兜底）
    if converged:
        try:
            from src.api.event_store import _drain_a2a_queue_after_terminal
            _drain_a2a_queue_after_terminal(_seq)
        except Exception as _qe:
            log.warning(f"converge_event_to_task: a2a queue drain failed: {_qe}")
    return {"status": "converged" if converged else "noop",
            "task_id": task_id, "task_status": _task_status, "event_seq": _seq,
            "event_status_before": before,
            "event_status_after": _task_status if converged else before}


def update_task_status(task_id, status, **kw):
    """终态幂等(重放安全): 已终态则不再更新(见 tech-impl 2.2)."""
    with db_lock:
        c = db.cursor()
        c.execute("SELECT status, runtime_id FROM agent_tasks WHERE id=?", (task_id,))
        row = c.fetchone()
        if not row:
            return
        if row[0] in TASK_TERMINAL_STATUSES:
            return
        sets = ["status=?"]
        params = [status]
        if status in TASK_TERMINAL_STATUSES:
            # 终态才写 completed_at；非终态（running 等）不动它，
            # 否则运行中任务带 completed_at 脏值（2026-08-19 修复：task_start 曾把
            # running 任务写成 completed_at=启动时刻，干扰终态判断/展示）。
            sets.append("completed_at=?")
            params.append(kw.get("completed_at") or datetime.now(timezone.utc).isoformat())
        for col in ("exit_code", "result_text", "errors", "stderr", "usage_json"):
            if col in kw:
                sets.append(f"{col}=?")
                params.append(kw[col])
        c.execute(f"UPDATE agent_tasks SET {', '.join(sets)} WHERE id=?", params + [task_id])
        # 方案 E：任务真正开始执行（running）→ 事件 pending→running（与 claim 认领语义一致）
        if status == "running":
            try:
                _evt_seq = c.execute("SELECT event_seq FROM agent_tasks WHERE id=?", (task_id,)).fetchone()
                if _evt_seq and _evt_seq[0]:
                    c.execute(
                        "UPDATE events SET status='running', completed_at='' "
                        "WHERE seq=? AND status IN ('pending','running')",
                        (_evt_seq[0],))
            except Exception as _e:
                log.warning(f"update_task_status: sync event running failed for task {task_id}: {_e}")
        # 槽位释放（agent-cluster-design.md §4.2.1）：非终态 → 终态（含 timeout_failed），且任务已绑定引擎 → busy_slots-1
        if status in TASK_TERMINAL_STATUSES and row[1] and row[1] > 0:
            release_busy_slot(row[1], cursor=c)
        # Issue #587：任务终态 → 事件级联收敛（事务内兜底）。
        # cancel_task / 直接判死路径（pool unreachable 等）此前只写 agent_tasks 不回写 events，
        # 事件长期 running → A2A 去重无限期阻断同角色新交接。complete_task/fail_task 的
        # 详写终态路径（update_event_complete 先行）在此为 no-op（事件已终态）。
        _cascaded_seq = 0
        if status in _TERMINAL_STATUSES:
            try:
                _evt_row = c.execute("SELECT event_seq FROM agent_tasks WHERE id=?",
                                     (task_id,)).fetchone()
                if _evt_row and _evt_row[0]:
                    if _cascade_event_terminal(c, _evt_row[0], status):
                        _cascaded_seq = _evt_row[0]
                        log.info(f"update_task_status: cascaded event {_evt_row[0]} -> "
                                 f"{status} for task {task_id} (Issue #587 convergence)")
            except Exception as _ce:
                log.warning(f"update_task_status: terminal cascade failed for task {task_id}: {_ce}")
        db.commit()
    # Issue #1667：任务级联写终态是「绕过 update_event_status/complete」的直接 SQL 旁路，
    # 排队 A2A 事件不会收到终态钩子 → 在此补挂 drain，避免 queued 永久滞留。
    if _cascaded_seq:
        try:
            from src.api.event_store import _drain_a2a_queue_after_terminal
            _drain_a2a_queue_after_terminal(_cascaded_seq)
        except Exception as _qe:
            log.warning(f"update_task_status: a2a queue drain after cascade failed: {_qe}")
    # Issue-45 M2：任务终态 → 会话级临时 token 即时失效（存在即有效，删除即失效）
    # Issue #1071（P4）：timeout_failed 是「疑似误判终态」，**不**收钥匙——
    #   判死是兜底猜测，不是任务真的结束；worker 之后 push/调平台接口全 401
    #   （生产 16:33–16:49 有 22 次 401）会让它连交付都做不到，活就白跑了。
    #   这类 token 靠 6 小时 TTL 自然过期，purge 会清；多活 6 小时只属于一个已判死的任务。
    if status in TASK_TERMINAL_STATUSES and status != TASK_TIMEOUT_FAILED:
        try:
            from src.api.auth_token import revoke_session_token
            revoke_session_token(task_id)
        except Exception as e:
            log.warning(f"update_task_status: revoke session token failed for {task_id}: {e}")
        # Issue #134：任务终态后刷新健康度缓存（增强层，不改派发/广播语义）
        try:
            from src.runtime.health_scorer import record_task_completion
            record_task_completion(row[1], status)
        except Exception:
            pass


def cancel_task(task_id):
    update_task_status(task_id, "cancelled")


def requeue_task(task_id):
    """将 dispatched/running 任务重置回 queued（客户端并发冲突跳过时回滚，避免任务悬空卡死）。

    见 seq#622 教训：客户端 _run_loop 因并发槽冲突 skip 任务后，服务端任务仍停留 dispatched，
    既不被执行也不重新排队 → 永久卡死。客户端冲突时应调本函数回滚。
    """
    with db_lock:
        c = db.cursor()
        # 释放槽位（根因2修复）：requeue 前先查 runtime_id，requeue 后 busy_slots-1
        c.execute("SELECT runtime_id, status FROM agent_tasks WHERE id=?", (task_id,))
        row = c.fetchone()
        # Issue #1071（P3）：活着的任务不回炉。回炉会改写 started_at，而 started_at 就是
        # worker 的 run_token → 老 worker 紧接着的 complete/fail 立刻被 409 stale 拒收。
        # 只拦 running（dispatched 从未开始执行，谈不上「活着」）；窗口化见
        # TASK_ACTIVITY_GUARD_SECONDS，超窗口仍无产出照旧回炉，不留悬空卡死。
        if row and (row[1] or "") == "running" and task_recently_active(task_id):
            log.info(f"requeue_task skipped: task {task_id} has recent task_messages activity "
                     f"(guard={TASK_ACTIVITY_GUARD_SECONDS}s)")
            return False
        c.execute("UPDATE agent_tasks SET status='queued', started_at=NULL WHERE id=? AND status IN ('dispatched','running')",
                  (task_id,))
        ret = c.rowcount > 0
        if ret and row and row[0]:
            release_busy_slot(row[0], cursor=c)
        # 方案 E：任务回落 queued → 事件同步回 pending（语义：尚未被认领）
        if ret:
            try:
                _evt_seq = c.execute("SELECT event_seq FROM agent_tasks WHERE id=?", (task_id,)).fetchone()
                if _evt_seq and _evt_seq[0]:
                    c.execute(
                        "UPDATE events SET status='pending' WHERE seq=? AND status IN ('running','pending')",
                        (_evt_seq[0],))
            except Exception as _e:
                log.warning(f"requeue_task: sync event pending failed for task {task_id}: {_e}")
        db.commit()
        return ret


def peek_next_seq(task_id) -> int:
    """预读 task 下一个可用 seq（MAX+1）。Issue #975：路由层预留下列 seq 范围作回执。"""
    with db_lock:
        c = db.cursor()
        c.execute("SELECT COALESCE(MAX(seq),0) FROM task_messages WHERE task_id=?", (task_id,))
        return (c.fetchone()[0] or 0) + 1


def report_messages(task_id, messages, seq_start: int | None = None) -> dict | None:
    """批量执行消息 → append_event_archive_log（M1 落档，脱敏）+ task_messages 结构化落库 + 实时 fanout

    seq_start（Issue #975）：路由层同步预定的批次 seq 起点（peek_next_seq 预留）。
    不传则沿用 MAX+1。批次回执（日志 + 返回值）带 seq 范围，供 worker/master 对账。

    返回（QA 复审修复，Spec #1）：**实际落库**的回执 dict
    {"count": 落库条数, "seq_start": 首条 seq, "seq_end": 末条 seq}；
    无可落库消息返回 None。落库异常时返回 {}（调用方不得当作成功范围）。
    非 dict 消息不计入 seq 消耗与回执（与 insert 跳过行为一致，杜绝假回执）。
    """
    task = get_task(task_id)
    if not task:
        return
    # 逐步日志前缀：task + 非空的 seq/issue/agent/repo，与 worker 侧（agent.py _esq）对齐便于定位
    _esq_parts = [f"seq={task.get('event_seq') or '?'}"]
    _issue_id = (task.get("issue_id") or "").strip().lstrip("#")
    if _issue_id and _issue_id != "0":
        _esq_parts.append(f"issue:#{_issue_id}")
    _agent_type = task.get("agent_type") or ""
    if _agent_type:
        _esq_parts.append(f"agent:{_agent_type}")
    _repo_key = task.get("repo_key") or ""
    if _repo_key:
        _esq_parts.append(f"repo:{_repo_key}")
    _esq = "|".join(_esq_parts)
    # 逐步日志（服务端观察：step_start/step_finish 等；远端执行时 step 详情在 worker 日志）
    for m in (messages or []):
        if not isinstance(m, dict):
            continue
        _t = m.get("type")
        if _t in ("step_start", "step_finish", "text", "reasoning", "tool_use", "error", "task:retry"):
            _d = m.get("data")
            log.info(f"[task {task_id}] [{_esq}] {_t}: {str(_d)[:120]}")
    if task.get("event_seq"):
        try:
            from src.api.event_archive import append_event_archive_log
            tokens = [_get_task_token(task)]
            hint = task.get("issue_id") or ""
            for m in messages or []:
                if isinstance(m, dict):
                    append_event_archive_log(task["repo_key"], task["event_seq"],
                                             m.get("type", "log"), m, tokens=tokens,
                                             issue_num_hint=hint)
        except Exception as e:
            log.warning(f"report_messages archive failed: {e}")
    # weekly-05：执行消息同时落 events.agent_logs（详情页「处理记录」，替代 event.json logs 依赖）
    if task.get("event_seq"):
        try:
            import json as _json
            _logs = [{"type": m.get("type", "log"),
                      "data": m.get("data"),
                      "ts": datetime.now(timezone.utc).isoformat()}
                     for m in (messages or []) if isinstance(m, dict)]
            if _logs:
                from src.session_manager import db as _edb, db_lock as _edbl
                with _edbl:
                    _c2 = _edb.cursor()
                    _c2.execute(
                        "SELECT agent_logs FROM events WHERE seq=?",
                        (task["event_seq"],))
                    _row = _c2.fetchone()
                    _prev = []
                    try:
                        _prev = _json.loads(_row[0]) if (_row and _row[0]) else []
                    except Exception:
                        _prev = []
                    _c2.execute(
                        "UPDATE events SET agent_logs=? WHERE seq=?",
                        (_json.dumps(_prev + _logs, ensure_ascii=False), task["event_seq"]))
                    _edb.commit()
        except Exception as e:
            log.warning(f"report_messages events.agent_logs failed: {e}")
    # 结构化落库（task_messages，双后端一致；seq 服务端生成 MAX+1，UNIQUE 防重放）
    # 收集落库后的 (原始消息, seq) 对，供 fanout 透传 seq（前端按 seq 去重，跨主 DB 尾随双通道不重复渲染）
    # QA 复审（Spec #1/#4）：只对 dict 消息消耗 seq；INSERT 命中 UNIQUE（预留 seq 与
    # 并发落库/重放撞号）时改用 MAX+1 重试一次——防 INSERT OR IGNORE 静默丢消息
    _persistable = [m for m in (messages or []) if isinstance(m, dict)]
    _seq_messages: list[dict] = []
    _receipt: dict | None = None
    if _persistable:
        try:
            with db_lock:
                c = db.cursor()
                if seq_start is not None:
                    seq = int(seq_start)
                else:
                    c.execute("SELECT COALESCE(MAX(seq),0) FROM task_messages WHERE task_id=?", (task_id,))
                    seq = (c.fetchone()[0] or 0) + 1
                _seq_first = None
                for m in _persistable:
                    _t = m.get("type", "log")
                    _d = m.get("data")
                    tool, content, input_json, output = _normalize_message(_t, _d)
                    # created_at 显式写 UTC ISO（时间本地化：MySQL 表默认 now() 为 CST 无时区，
                    # 双后端存储语义统一，读时 normalize_event_time 幂等）
                    _row = (task_id, seq, _t, tool, content, input_json, output,
                            datetime.now(timezone.utc).isoformat())
                    try:
                        c.execute(
                            "INSERT INTO task_messages (task_id, seq, type, tool, content, input_json, output, created_at) VALUES (?,?,?,?,?,?,?,?)",
                            _row)
                    except Exception as _ie:
                        # seq 撞号（预留范围与异步落库/补报重放并发）：改用 MAX+1 重试一次，
                        # 不允许 INSERT OR IGNORE 式静默丢消息（QA Spec #4 seq 重复风险）
                        c.execute("SELECT COALESCE(MAX(seq),0) FROM task_messages WHERE task_id=?", (task_id,))
                        seq = (c.fetchone()[0] or 0) + 1
                        c.execute(
                            "INSERT INTO task_messages (task_id, seq, type, tool, content, input_json, output, created_at) VALUES (?,?,?,?,?,?,?,?)",
                            (task_id, seq, _t, tool, content, input_json, output,
                             datetime.now(timezone.utc).isoformat()))
                        log.warning(f"report_messages: task={task_id} seq conflict, reallocated to {seq}")
                    if _seq_first is None:
                        _seq_first = seq
                    # 透传 seq 给 fanout（done 终态消息 _to_payload 返回 None 不会推送，但 seq 仍记录）
                    _seq_messages.append({**m, "seq": seq})
                    seq += 1
                db.commit()
                _receipt = {"count": len(_seq_messages), "seq_start": _seq_first,
                            "seq_end": _seq_messages[-1].get("seq")}
                # Issue #975：批次回执日志（seq 起止 + 条数）——master 侧对账凭证
                if _seq_messages:
                    log.info(f"report_messages: task={task_id} persisted count={len(_seq_messages)} "
                             f"seq_range={_seq_first}..{_seq_messages[-1].get('seq')}")
        except Exception as e:
            log.warning(f"report_messages task_messages insert failed: {e}")
            _receipt = {}
    # 实时 fanout（订阅表推送，队列满丢弃保活；SSE 重连靠 since 回源）
    # 传带 seq 的消息：前端按 seq 去重，queue 快路径与 SSE DB 尾随双通道不重复渲染
    try:
        from src.runtime.task_log_stream import fanout
        fanout(task_id, _seq_messages if _seq_messages else (messages or []))
    except Exception as e:
        log.warning(f"report_messages fanout failed: {e}")
    return _receipt


def _normalize_message(_t: str, _d) -> tuple[str, str, str, str]:
    """worker 消息 → (tool, content, input_json, output) 规范化。

    - tool_use: data 为 dict {tool, input}（input 可能已是 JSON 字符串或 dict）
    - text/reasoning/step_*: data 为 str 或 {text/content}
    - output 列保留给未来 tool_result（worker 端暂未上报 tool_result 结构化输出）
    """
    if isinstance(_d, dict):
        tool = str(_d.get("tool") or "")
        if _t == "tool_use":
            inp = _d.get("input") or {}
            if isinstance(inp, str):
                try:
                    json.loads(inp)
                    input_json = inp
                except Exception:
                    input_json = json.dumps({"raw": inp}, ensure_ascii=False)
            else:
                input_json = json.dumps(inp, ensure_ascii=False)
            return tool, "", input_json, ""
        content = str(_d.get("text") or _d.get("content") or "")
        return tool, content, "", ""
    return "", str(_d or ""), "", ""


def list_task_messages(task_id, since: int = 0, limit: int = 5000) -> list[dict]:
    """按 seq 增量拉取 task_messages（SSE 断线/新开页补数据用）。

    返回 [{seq, type, tool, content, input, output, created_at}]，input 为解析后的 dict。
    """
    import json as _json
    try:
        with db_lock:
            c = db.cursor()
            c.execute(
                "SELECT seq, type, tool, content, input_json, output, created_at FROM task_messages "
                "WHERE task_id=? AND seq>? ORDER BY seq LIMIT ?",
                (task_id, since, limit))
            rows = c.fetchall()
    except Exception as e:
        log.warning(f"list_task_messages failed: {e}")
        return []
    out = []
    for r in rows:
        inp = {}
        try:
            inp = _json.loads(r[4] or "{}")
        except Exception:
            pass
        out.append({"seq": r[0], "type": r[1], "tool": r[2] or "",
                    "content": r[3] or "", "input": inp, "output": r[5] or "",
                    "created_at": normalize_event_time(r[6] or "")})
    return out


def list_task_messages_recent(task_id, limit: int = 200) -> list[dict]:
    """拉取任务**最新**的 limit 条 task_messages，按时间正序返回。

    与 list_task_messages 的区别：后者从 seq=0 往后取，适合前端从头回放；
    本函数先按 seq DESC 取尾部再反转，用于「接管」这类只关心当前进展的场景 ——
    否则长任务的接管上下文会停在几小时前的任务开头。

    返回 [{seq, type, tool, content, input, output, created_at}]，input 为解析后的 dict。
    """
    import json as _json
    try:
        with db_lock:
            c = db.cursor()
            c.execute(
                "SELECT seq, type, tool, content, input_json, output, created_at FROM task_messages "
                "WHERE task_id=? ORDER BY seq DESC LIMIT ?",
                (task_id, limit))
            rows = list(reversed(c.fetchall()))
    except Exception as e:
        log.warning(f"list_task_messages_recent failed: {e}")
        return []
    out = []
    for r in rows:
        inp = {}
        try:
            inp = _json.loads(r[4] or "{}")
        except Exception:
            pass
        out.append({"seq": r[0], "type": r[1], "tool": r[2] or "",
                    "content": r[3] or "", "input": inp, "output": r[5] or "",
                    "created_at": normalize_event_time(r[6] or "")})
    return out


# wrote_files 派生查询认定的「写入类」工具名（含 hermes 等引擎的工具名变体，
# 兜底按 input 含 path + content/new_string 精筛，见 list_wrote_files）
_WRITE_TOOL_EXACT = frozenset({"write", "write_file", "patch"})

# 记忆化缓存（Issue #977 性能补丁）：key=task_id → (max_seq, wrote_files)。
# 只按「消息 seq 是否前进」判失效（新增即失效），不用纯 TTL，避免"配置/进度改了却显示旧值"。
# 上限 512 个任务，超出按插入序淘汰最旧（单进程内小字典，替换成本可忽略）。
_WROTE_FILES_CACHE: dict = {}
_WROTE_FILES_CACHE_MAX = 512


def _is_write_tool(tool: str) -> bool:
    t = (tool or "").strip().lower()
    return t in _WRITE_TOOL_EXACT or "write" in t or "patch" in t


def list_wrote_files(task_id) -> list[dict]:
    """派生查询：该任务写过的文件清单（Issue #977，/info 的 wrote_files 数据源）。

    - 数据源：task_messages 中 tool_use 消息的 input_json（write/patch 的 path +
      content/new_string 的 utf-8 字节数），不扫描工作区、不依赖 agent 自述。
    - 过滤：SQL 侧先按工具名收窄（write/patch 变体，与 _is_write_tool 同口径）
      + Python 侧精筛（input 含 path 与 content/new_string）。
    - 同 path 多次写入各记一条（保留全部 seq）；first_seen_at 取该条消息 created_at。
    - 性能（2026-11-04 压测）：原实现扫该任务全部 tool_use 行（10k 消息 36ms、
      大写入 221ms、40k 消息 146ms），且 /info 会被前端反复拉。改为
      「SQL 工具名预过滤」+「按 (task_id, max_seq) 记忆化」：任务消息无新增时命中缓存
      返回 ~µs；有新增消息（seq 前进）自动失效重算，不做纯 TTL 假缓存。
    """
    import json as _json
    try:
        with db_lock:
            c = db.cursor()
            c.execute("SELECT COALESCE(MAX(seq), 0) FROM task_messages WHERE task_id=?",
                      (task_id,))
            _max_seq = int(c.fetchone()[0] or 0)
            _hit = _WROTE_FILES_CACHE.get(str(task_id))
            if _hit and _hit[0] == _max_seq:
                return [dict(x) for x in _hit[1]]
            c.execute(
                "SELECT seq, tool, input_json, created_at FROM task_messages "
                "WHERE task_id=? AND type='tool_use' "
                "AND (LOWER(tool) LIKE '%write%' OR LOWER(tool) LIKE '%patch%') "
                "ORDER BY seq",
                (task_id,))
            rows = c.fetchall()
    except Exception as e:
        log.warning(f"list_wrote_files failed: {e}")
        return []
    out = []
    for seq, tool, input_json, created_at in rows:
        if not _is_write_tool(tool):
            continue
        try:
            inp = _json.loads(input_json or "{}")
        except Exception:
            continue
        if not isinstance(inp, dict):
            continue
        path = inp.get("path")
        if not isinstance(path, str) or not path:
            continue
        payload = inp.get("content")
        if not isinstance(payload, str):
            payload = inp.get("new_string")
        if not isinstance(payload, str):
            continue
        out.append({
            "path": path,
            "bytes": len(payload.encode("utf-8")),
            "tool": (tool or "").strip().lower(),
            "seq": seq,
            "first_seen_at": normalize_event_time(created_at or ""),
        })
    if len(_WROTE_FILES_CACHE) >= _WROTE_FILES_CACHE_MAX:
        _WROTE_FILES_CACHE.pop(next(iter(_WROTE_FILES_CACHE)), None)
    _WROTE_FILES_CACHE[str(task_id)] = (_max_seq, [dict(x) for x in out])
    return out


def report_progress(task_id, progress):
    # 阶段进度：写 result_text 前缀（保持轻量，完整进度落 agent_tasks.result_text）
    task = get_task(task_id)
    if not task:
        return
    p = progress or {}
    with db_lock:
        c = db.cursor()
        c.execute("UPDATE agent_tasks SET result_text=? WHERE id=?", (str(p.get("msg", "")), task_id))
        db.commit()


def _detect_business_failure(agent_type, output, result_text):
    """检测 agent 进程正常退出(exit_code=0)但业务实际失败的情况。

    只解析结构化 JSON 输出的 conclusion 字段做失败关键词匹配。
    返回 True 表示业务失败（应覆盖 exit_code 为非 0）。

    仅对 sre agent_type 生效（部署类任务），其他 agent 不受影响。

    作用域收紧（issue 内部误判案例教训）：
    - 旧逻辑拼接 conclusion+next_step+issue_summary 全文匹配，且无结构化输出时
      降级扫整个 result_text；长距正则 `部署.*失败` 会跨整句把否定语境
      （"部署验证通过：唯一失败用例为基线既有失败（非本 PR 引入）"）连起来，
      把成功的任务误标 failed。
    - 现只扫 conclusion 单字段：它是 agent 对任务结果的一句定性结论，
      表述短、不含过程叙述，长距正则的跨句连词风险被结构性消除；
      next_step/issue_summary/result_text 里的过程性描述不再参与判定。
    - 长距 `.*` 模式一并收紧为短窗口，防止 conclusion 内部仍出现跨从句连词。
    """
    if agent_type != "sre":
        return False
    if not output:
        # 无结构化输出 → 不做业务失败判定（宁缺勿滥：检测面收窄到 conclusion，
        # 降级扫 result_text 的路径删除——那是 内部误判案例的主通道）
        return False
    blob = output.get("conclusion", "") or ""
    if not blob:
        return False
    import re
    # 失败关键词：部署中止/失败、无法购买/创建、未认证、凭证不足、部署未成功等
    # （短窗口：部署后最多隔 2 个常见连接字，不再跨句/跨从句连词）
    failure_patterns = [
        r"部署[了中于]{0,2}失败", r"部署[了中于]{0,2}中止", r"部署[了中于]{0,2}未成功",
        r"部署[了中于]{0,2}无法",
        r"无法购买", r"无法创建", r"无法部署",
        r"未完成.{0,4}认证", r"未实名", r"实名认证",
        r"凭证.{0,4}(?:不足|缺失|无效)",
        r"Ecs\\.0707", r"AccessDenied", r"auth.{0,8}fail",
        r"操作失败", r"执行失败",
        r"无法继续", r"中止\b",
    ]
    for pat in failure_patterns:
        if re.search(pat, blob, re.I):
            return True
    return False


def premature_end_marker() -> str:
    """「疑似提前结束」标记（唯一来源：适配器 src/agent/hermes_acp.py）。

    懒加载转发，避免 runtime.service 与 agent 适配器在导入期互相牵扯；适配器
    不可用（未安装/裁剪）时回退到字面量，保证标记语义不变。
    """
    try:
        from src.agent.hermes_acp import PREMATURE_END_MARKER as _pm
        return _pm
    except Exception:
        return "[PREMATURE_END_TURN]"


# ===== Issue #627: SRE 生产发布任务终态回填 =====

def _backfill_release_subissue_on_complete(task: dict | None, exit_code: int,
                                            result_text: str) -> None:
    """按 task.issue_id 反查发布子单回填结果三分支（方案 C，无需 payload 锚点）。

    - 任务须为 sre 且关联发布子单（issue 行 release_target_env 非空）；
      普通 sre 任务直接返回，零影响。
    - exit_code == 0：有 deploy_url 证据 → deployed_success；否则 not_executed
      （阶段 completed 不视为发布成功）。
    - exit_code != 0：deploy_failed（failure_reason 取 result_text 摘要）。
    - 幂等：release_result 已回填不再覆盖（_backfill_release_subissue_result 内部保护）。
    """
    if not task:
        return
    if (task.get("agent_type") or "") != "sre":
        return
    _issue_id = task.get("issue_id") or ""
    _issue_num = str(_issue_id).lstrip("#")
    if not _issue_num.isdigit():
        return
    try:
        from src.api.issues_center import _backfill_release_subissue_result
        _backfill_release_subissue_result(
            repo_key=task.get("repo_key") or "",
            issue_number=int(_issue_num),
            exit_code=int(exit_code or 0),
            result_text=result_text or "")
    except Exception as _e:
        log.warning(f"_backfill_release_subissue_on_complete failed: {_e}")


def complete_task(task_id, result_text, exit_code=0, usage=None,
                  revive_from_timeout_failed: bool = False):
    """完成：解析结构化 output → 业务失败检测 → finalize_event_archive → update_event_complete → 终态。

    revive_from_timeout_failed（Issue #1071）：由 service.revive_timeout_task 传入，
    表示这是一次已校验过的「兜底超时判死后迟到结果」复活，允许把事件侧 failed 改判 completed。
    """
    task = get_task(task_id)
    # 可见性（P1）：引擎「疑似提前结束」（模型输出后静默、无收尾记录）时任务仍算
    # completed（exit 0），但把标记显式落到 agent_tasks.errors + 事件 result 前缀，
    # 让「跑绿了但可能没干完」在任务详情/事件列表里一眼可见。
    # 只做可见性：不降 failed、不触发重跑（重跑需幂等约束，另行评估）。
    _pm = premature_end_marker()
    _premature = bool(_pm) and _pm in (result_text or "")
    # 幂等保护：如果 task 已经是终态（failed/cancelled），不覆盖（防止 worker 终止后残留进程
    # 跑完调 complete 把 failed 状态覆盖成 completed，2026-08-24 seq#2488 实例）。
    # 例外：如果 result_text 包含完整结构化输出（actions+conclusion），说明任务实际已完成，
    # 即使 exit_code 非 0（如 hermes ACP 进程被 SIGTERM 清理后 proc.returncode=-15）也应覆盖为 completed。
    if task and task.get("status") in ("failed", "cancelled", TASK_TIMEOUT_FAILED):
        from src.api.event_store import parse_agent_output_json
        _output = parse_agent_output_json(result_text or "")
        if _output and _output.get("actions") and _output.get("conclusion"):
            log.info(f"Task {task_id} was {task.get('status')} but has structured output, overriding to completed")
        else:
            log.info(f"Task {task_id} already {task.get('status')}, skip complete_task (result={result_text[:60]})")
            return
    # Issue #403：瞬时上游抖动自动重试（判定层收口，worker 完成上报路径）。
    # 仅拦 exit_code!=0 的失败；可重试（瞬时分类 ∧ 无写操作/结构化产出证据 ∧
    # attempts 未耗尽）则退避 requeue 并跳过本函数全部终态写入。放在 event_seq
    # 分支之外：无关联事件的任务同样适用；「exit!=0 但结构化产出」由闸门放行
    # （不重试，走既有 override-to-completed 路径）。
    if exit_code != 0:
        try:
            from src.runtime.transient_retry import retry_transient_task
            if retry_transient_task(task_id, exit_code, "",
                                    result_text=result_text or ""):
                log.info(f"Task {task_id}: transient failure scheduled for retry, "
                         f"skip terminal write (exit={exit_code})")
                return
        except Exception as _e:
            log.warning(f"complete_task: transient retry check failed for {task_id}: {_e}")
    if task and task.get("event_seq"):
        try:
            from src.api.event_store import parse_agent_output_json, update_event_complete
            from src.api.event_archive import finalize_event_archive
            output = parse_agent_output_json(result_text or "")
            # 结构化产出修复：exit_code 非 0 但有完整结构化输出（actions+conclusion）
            # → hermes ACP 进程被 SIGTERM 清理后 proc.returncode=-15，任务实际已完成
            if exit_code != 0 and output and output.get("actions") and output.get("conclusion"):
                log.info(f"Task {task_id}: exit_code {exit_code} but has structured output, overriding to 0")
                exit_code = 0
            # 业务失败检测：进程退出码=0 但业务实际失败 → 覆盖 exit_code
            if exit_code == 0 and _detect_business_failure(
                    task.get("agent_type") or "", output, result_text):
                exit_code = 1
                log.info(f"Task {task_id}: business failure detected "
                         f"(agent={task.get('agent_type')}), overriding exit_code to 1")

            # exit_code=0 但无结构化产出 → 接受 hermes 的 end_turn 结果，直接 completed
            # （旧逻辑会 requeue 最多49次，导致重复评论污染；现改为不重试；
            #  瞬时抖动场景已由函数开头的 #403 判定层收口处理）

            status = "completed" if exit_code == 0 else "failed"
            duration = None
            try:
                s = datetime.fromisoformat(task["started_at"]) if task.get("started_at") else None
                if s:
                    duration = round((datetime.now(timezone.utc) - s).total_seconds(), 1)
            except Exception:
                pass
            finalize_event_archive(task["repo_key"], task["event_seq"], status,
                                   exit_code, duration, output,
                                   tokens=[_get_task_token(task)],
                                   issue_num_hint=task.get("issue_id") or "")
            _conclusion = ""
            if output and output.get("conclusion"):
                _conclusion = output["conclusion"]
            _result_show = _conclusion or (result_text or "")[:200]
            if not _result_show:
                # Issue #1503：conclusion 与 result_text 均空 → 兜底含状态/exit_code 默认摘要
                _result_show = f"{status} (exit_code={exit_code})"
            if _premature:
                # 事件列表/详情直接看到「没走到收尾」，不必翻完整 result_text
                _result_show = f"⚠️ {_pm} 疑似提前结束（未走到收尾）：{_result_show}"
            update_event_complete(task["event_seq"], status, exit_code=exit_code,
                                  duration=duration,
                                  completed_at=datetime.now(timezone.utc).isoformat(),
                                  result=_result_show,
                                  structured_output=output or None,
                                  revive_from_timeout_failed=(
                                      revive_from_timeout_failed and status == "completed"))
            # Issue #656: 末节点完成后 → issue 级自动结单（全部节点 completed + 无阻断
            # → stage=done / status=completed / completed_at 落库 + 联动关闭 VCS Issue；
            # failed 阻断回退 pending_review 保留人工 confirm done 路径）
            if status == "completed" and task.get("issue_id"):
                try:
                    from src.api.issues_center import auto_finalize_issue_on_last_node
                    _fin = auto_finalize_issue_on_last_node(
                        task.get("repo_key") or "", task.get("issue_id") or "",
                        task.get("agent_type") or "", source="complete_task")
                    if _fin not in ("noop:not_last_node", "noop:not_issue", "noop:terminal"):
                        log.info(f"complete_task: issue finalize result={_fin} "
                                 f"issue={task.get('issue_id')}")
                except Exception as _e:
                    log.warning(f"complete_task: issue auto-finalize failed: {_e}")
        except Exception as e:
            log.warning(f"complete_task finalize failed: {e}")
    # 疑似提前结束：标记落到 agent_tasks.errors（任务详情可见）；状态仍是 completed。
    # 非疑似场景不传 errors，避免覆盖 fail 路径写下的真实错误信息。
    _err_kw = ({"errors": json.dumps(
        [f"{_pm} 未检测到收尾记录（actions/conclusion），疑似提前结束；工作可能未完成"],
        ensure_ascii=False)} if _premature else {})
    update_task_status(task_id, "completed" if exit_code == 0 else "failed",
                       exit_code=exit_code, result_text=result_text or "",
                       usage_json=json.dumps(usage or {}), **_err_kw)
    # 新会话续接指针回写——仅写回 Issue 级执行会话
    # repo 级 devops_agent_session.runtime_session_id 已废弃（无消费方），不再回写。
    # 引擎守卫（B2）：usage.engine 为本次执行引擎（worker done 事件带上），回写时
    # 连同 session_engine 一起覆盖为本次引擎（最新产出者权威），消费端守卫比对用。
    # 陈旧异构指针不跳过而是直接覆盖——跳过会让坏指针永久滞留表里。
    _new_sid = (usage or {}).get("session_id") or ""
    _done_engine = (usage or {}).get("engine") or ""
    if _new_sid:
        try:
            from src.api.session_store import update_issue_session
            if task and task.get("issue_session_id"):
                update_issue_session(task["issue_session_id"], runtime_session_id=_new_sid,
                                     session_engine=_done_engine)
        except Exception as e:
            log.warning(f"complete_task update issue session failed: {e}")
    log.info(f"Task completed: {task_id} exit={exit_code}")
    # Issue #627: SRE 生产发布任务终态回填（成功/失败都要回填——deploy_failed 分支
    # 恰在 exit!=0；钩子自带 production_release 锚点判断，普通任务零影响、幂等）
    if task and (task.get("agent_type") or "") == "sre" and task.get("issue_id"):
        try:
            _backfill_release_subissue_on_complete(task, exit_code, result_text or "")
        except Exception as _se:
            log.warning(f"complete_task production_release backfill failed: {_se}")
    # remote worker 模式下 _execute_agent 不执行，系统回帖 + 会话同步在此补触发
    if exit_code == 0 and task and task.get("issue_id"):
        try:
            from src.api.event_pipeline import (
                _post_result_to_issue, _issue_num_from,
                _sync_issue_comments_to_conversation,
                _post_subtask_completion_to_parent, _post_all_subtasks_done,
            )
            from src.repo_key import from_repo_key
            _issue_id = task["issue_id"]
            _issue_num = _issue_num_from(_issue_id)
            _repo = from_repo_key(task["repo_key"]) or task.get("account") or ""
            _token = _get_task_token(task)
            _agent_type = task.get("agent_type") or ""
            _repo_key = task["repo_key"]
            _result_text = result_text or ""
            # 0. 多任务模式：父 Issue Agent 完成 → stage=done
            if _agent_type == "issue":
                try:
                    from src.api.session_store import is_subtask_mode_enabled, set_issue_stage
                    if is_subtask_mode_enabled(_repo_key, _issue_id):
                        set_issue_stage(_repo_key, _issue_id, "done", "issue")
                        log.info(f"Multi-task mode: parent issue {_issue_id} stage=done (subtasks will trigger independently)")
                except Exception as _se:
                    log.warning(f"complete_task multi-task stage=done failed: {_se}")
            # 1. 系统回帖（R18.6.10: 走 handler.post_issue_comment()）
            if _issue_num and _repo_key:
                try:
                    from src.token_resolver import TokenResolver, create_provider_from_repo_key, create_action_handler
                    _p = create_provider_from_repo_key(_repo_key)
                    # R25.3: 回帖链用 resolve_config_level（不走过期短路），保留 best-effort 行为
                    _r = TokenResolver(_p).resolve_config_level(_repo_key)
                    _r.issue_id = _issue_id
                    _h = create_action_handler(_r)
                    if _h:
                        # Issue #679：result_text 为空时无内容可发，跳过系统回帖（不发占位文案）
                        if _result_text:
                            _h.post_issue_comment(_repo_key, _issue_num, _result_text, _r, _agent_type)
                        else:
                            log.info(f"complete_task: skip system comment, empty result_text for {_repo_key}/issues/{_issue_num}")
                    elif _token:
                        # 无 handler（无 token 配置）→ fallback 直接调 _post_result_to_issue
                        from src.api.event_pipeline import _post_result_to_issue
                        _post_result_to_issue(_repo, _issue_num, _result_text, _token, _agent_type,
                                              agent_result=True, task_id=task_id,
                                              seq=task.get("event_seq"))
                except Exception as _pe:
                    log.warning(f"complete_task: handler.post_issue_comment failed: {_pe}")
                    # fallback 到直接调用
                    if _token:
                        from src.api.event_pipeline import _post_result_to_issue
                        _post_result_to_issue(_repo, _issue_num, _result_text, _token, _agent_type,
                                              agent_result=True, task_id=task_id,
                                              seq=task.get("event_seq"))
            # 2. 子任务 SRE 完成 → 登记到父 Issue
            if _agent_type == "sre" and _issue_num and _token:
                try:
                    from src.api.session_store import get_parent_issue, get_subtask_issues
                    _parent = get_parent_issue(_repo_key, _issue_id)
                    if _parent:
                        _parent_num = _issue_num_from(_parent)
                        _sub_num = _issue_num
                        if _parent_num and _sub_num and _repo:
                            _post_subtask_completion_to_parent(_repo, _sub_num, _parent_num, _result_text, _token)
                            _subs = get_subtask_issues(_repo_key, _parent)
                            _all_done = all(s.get("stage") == "done" or s.get("issue_id") == _issue_id for s in _subs)
                            if _all_done and len(_subs) > 0:
                                _post_all_subtasks_done(_repo, _parent_num, _subs, _token)
                except Exception as _se:
                    log.warning(f"complete_task subtask post failed: {_se}")
            # 3. 同步结果到任务会话
            if _issue_num and _result_text:
                try:
                    from src.session_manager import db as _db, db_lock as _dl, add_message as _am
                    with _dl:
                        _c = _db.cursor()
                        _c.execute("SELECT conversation_id FROM issue WHERE repo_key=? AND issue_number=? AND conversation_id!='' LIMIT 1",
                                   (_repo_key, _issue_num))
                        _row = _c.fetchone()
                    if _row and _row[0]:
                        _am(_row[0], "agent", f"[{_agent_type.upper()} Agent] {_result_text[:3000]}")
                except Exception as _se:
                    log.warning(f"complete_task sync to conversation failed: {_se}")
            # 4. 拉取 Issue 评论同步到会话
            if _issue_num and _repo and _token:
                try:
                    _sync_issue_comments_to_conversation(_repo_key, _issue_num, _token, _agent_type, _repo)
                except Exception as _se:
                    log.warning(f"complete_task sync issue comments failed: {_se}")
            # 5. VOD 智能推荐完成 → redispatch（remote worker 模式下 _execute_agent 不执行，此处补触发）
            if _agent_type == "vod" and _result_text and _issue_id:
                try:
                    _handle_vod_recommend_complete(_repo_key, _issue_id, _result_text, task_id, task, _token)
                except Exception as _se:
                    log.warning(f"complete_task vod recommend handler failed: {_se}")
            # 6. Issue #468: SRE 完成 → 从 result_text 提取部署 URL 登记到 managed_repos.deploy_url
            #    （remote worker 模式下 _execute_agent 不执行，此处与直连路径统一调用共享函数）
            if _agent_type == "sre" and _result_text:
                try:
                    from src.api.event_pipeline import register_sre_deploy_url
                    register_sre_deploy_url(_repo_key, _result_text)
                except Exception as _se:
                    log.warning(f"complete_task sre deploy_url register failed: {_se}")
                # Issue #482: 场景级登记——按 issue 绑定 scene_id 更新
                # devops_pipeline_scene.deploy_url（无绑定/失败不影响收尾主流程）
                try:
                    from src.api.event_pipeline import register_scene_deploy_url
                    register_scene_deploy_url(_repo_key, _issue_id, _result_text)
                except Exception as _se:
                    log.warning(f"complete_task scene deploy_url register failed: {_se}")
        except Exception as _pe:
            log.warning(f"complete_task post-completion hooks failed: {_pe}")
    # 可观察性埋点：任务到达终态
    try:
        from src.observability import record_task_terminal
        _terminal = "completed" if exit_code == 0 else "failed"
        _run_s = -1
        _total_s = -1
        if task:
            _now = datetime.now(timezone.utc)
            if task.get("started_at"):
                try:
                    _run_s = (_now - datetime.fromisoformat(task["started_at"])).total_seconds()
                except Exception:
                    pass
            if task.get("created_at"):
                try:
                    _total_s = (_now - datetime.fromisoformat(task["created_at"])).total_seconds()
                except Exception:
                    pass
            record_task_terminal(task_id, task.get("event_type") or task.get("agent_type") or "other",
                                 task.get("runtime_id", 0), _terminal, _run_s, _total_s)
    except Exception:
        pass
    # 事件总线：任务完成
    try:
        from src.observability.events import bus, Event
        bus.publish(Event(type="task:completed" if exit_code == 0 else "task:failed",
                          task_id=task_id, payload={"exit_code": exit_code}))
    except Exception:
        pass


# ===== 双重标记完成（issue-219-task-complete-double-signal-design.md）=====

def set_task_signal(task_id: str, signal: str, result_text: str = "") -> bool:
    """条件置双标记之一（stream_ended=worker 隐式 / self_reported=agent 显式）。

    - 仅当标记未置时置 1（重放安全）；置 `self_reported` 时同步写入 result_text（防值丢失）。
    - 返回 True = 本次调用是第一个落该标记的；调用方据此读取另一标记，决定是否 finalize。
    """
    if signal not in ("stream_ended", "self_reported"):
        return False
    try:
        with db_lock:
            c = db.cursor()
            if signal == "self_reported":
                c.execute("UPDATE agent_tasks SET self_reported=1, result_text=? "
                          "WHERE id=? AND self_reported=0", (result_text or "", task_id))
            else:
                c.execute("UPDATE agent_tasks SET stream_ended=1 WHERE id=? AND stream_ended=0", (task_id,))
            _first = c.rowcount > 0
            db.commit()
        return _first
    except Exception as e:
        log.warning(f"set_task_signal({signal}) failed for {task_id}: {e}")
        return False


def finalize_by_stream_end(task_id: str, fallback_text: str = "", exit_code: int = 0,
                           usage: dict | None = None) -> bool:
    """单信号收尾（stream-end-finalize-single-signal-design.md）：引擎流结束即任务终态。

    不再区分「自报/普通」技能、不再等双信号；结构化 self_report 只是产物而非闸门：
    - Agent 已显式 self_report 且存了结构化 JSON → 以其为权威结论；
    - 否则回落调用方传入的流/引擎文本。
    complete_task 负责全部落库副作用（事件、结果、回帖、stage、token 延迟失效）。
    已终态 → 幂等返回 False（补挂走 enrich_task_on_late_report）。
    """
    task = get_task(task_id)
    if not task:
        return False
    _st = task.get("status")
    if _st == TASK_TIMEOUT_FAILED:
        # Issue #1071（P2）：判死之后才回来的结果——走窄口子复活，其余终态照旧幂等返回。
        return revive_timeout_task(task_id, fallback_text or "", exit_code, usage=usage or {})
    if _st in TASK_TERMINAL_STATUSES:
        return False
    result_text = fallback_text or ""
    if task.get("self_reported") == 1 and task.get("result_text"):
        _stored = task.get("result_text") or ""
        try:
            import json as _j
            _d = _j.loads(_stored)
            if isinstance(_d, dict) and (_d.get("actions") or _d.get("conclusion")):
                result_text = _stored
        except Exception:
            pass
    complete_task(task_id, result_text, exit_code, usage=usage or {})
    return True


def _is_pending_self_report_timeout(task: dict) -> bool:
    """判死来源是否为「兜底超时」（sweep），而不是业务失败。

    Issue #1071：复活是窄口子，来源必须钉死——
    - 新行：sweep 写 errors 首元素 {"error_code": "PENDING_SELF_REPORT_TIMEOUT"}；
    - 存量行（改动前判死）：没有 error_code 标记，按 sweep 写死的判死文案兜底识别。
    业务失败（failed）永远走不到这里，2026-08-24 seq#2488 的「失败被成功覆盖」竞态不重开。
    """
    _errs = str(task.get("errors") or "")
    if not _errs:
        return False
    if TIMEOUT_SOURCE_MARKER in _errs:
        return True
    return ("内未自报完成/失败" in _errs) or ("pending_self_report 超时" in _errs)


def revive_timeout_task(task_id, result_text: str = "", exit_code: int = 0,
                        usage: dict | None = None) -> bool:
    """Issue #1071（P2）：迟到结果回收——判死后才回来的结构化结论不再永久丢弃。

    准入（全部满足才复活，任一不满足返回 False 并记原因）：
    1. 任务态 = timeout_failed（其它终态一律不动）；
    2. 从未复活过（revived_at 为空）——只复活一次，防反复重放刷状态；
    3. 判死时刻在 TIMEOUT_REVIVE_WINDOW_SECONDS 内（默认 30 分钟）；
    4. 判死来源是兜底超时（见 _is_pending_self_report_timeout），不是业务失败；
    5. 迟到结果含完整结构化产出（actions + conclusion），与 complete_task 既有判据一致。

    通过后：状态回 running（清 completed_at/exit_code，落 revived_at）→ 交给 complete_task
    走完整收尾（事件归档/事件改判/回帖/stage），事件侧 failed→completed 由
    update_event_complete(revive_from_timeout_failed=True) 这一个窄口子放行。
    """
    task = get_task(task_id)
    if not task or task.get("status") != TASK_TIMEOUT_FAILED:
        return False
    if task.get("revived_at"):
        log.info(f"revive skipped: task {task_id} already revived at {task['revived_at']}")
        return False
    from src.api.event_store import parse_agent_output_json
    _out = parse_agent_output_json(result_text or "")
    if not (_out and _out.get("actions") and _out.get("conclusion")):
        log.info(f"revive skipped: task {task_id} late result has no complete structured output")
        return False
    if not _is_pending_self_report_timeout(task):
        log.info(f"revive skipped: task {task_id} was not failed by the timeout sweep "
                 f"(business failure stays failed)")
        return False
    _dead_at = _epoch_of(task.get("completed_at"))
    if _dead_at is not None:
        _age = datetime.now(timezone.utc).timestamp() - _dead_at
        if _age > TIMEOUT_REVIVE_WINDOW_SECONDS:
            log.info(f"revive skipped: task {task_id} judged {_age:.0f}s ago "
                     f"(window={TIMEOUT_REVIVE_WINDOW_SECONDS}s)")
            return False
    with db_lock:
        c = db.cursor()
        c.execute(
            "UPDATE agent_tasks SET status='running', completed_at=NULL, exit_code=NULL, "
            "revived_at=? WHERE id=? AND status='timeout_failed' "
            "AND (revived_at IS NULL OR revived_at='')",
            (datetime.now(timezone.utc).isoformat(), task_id)
        )
        _ok = c.rowcount > 0
        db.commit()
    if not _ok:
        log.info(f"revive skipped: task {task_id} changed state concurrently")
        return False
    log.info(f"timeout revive: task {task_id} timeout_failed -> running "
             f"(late structured result, re-finalizing)")
    complete_task(task_id, result_text or "", 0 if exit_code is None else exit_code,
                  usage=usage or {}, revive_from_timeout_failed=True)
    return True


def enrich_task_on_late_report(task_id: str, result_text: str = "") -> str:
    """单信号下「流结束后才到达的显式 self_report」幂等补挂（宽限窗内，token 仍有效）。

    返回：attached（completed 补挂结构化）/ promoted（failed 占位提升 completed）/ already / noop。
    守卫：仅本任务、仅结构化结论、仅未落库的缺位；failed 提升仅限「no-output 占位失败」
    （exit_code in (0, NULL)），真实 crash（exit_code!=0）不提升。
    只改结果存储（task/event 的 structured_output/result），不重放任何副作用（评论/回帖）。
    """
    task = get_task(task_id)
    if not task:
        return "noop"
    status = task.get("status") or ""
    if status not in ("completed", "failed") or task.get("self_reported") != 1:
        return "noop"
    _raw = result_text or (task.get("result_text") or "")
    structured = None
    try:
        import json as _j
        _d = _j.loads(_raw)
        if isinstance(_d, dict) and (_d.get("actions") or _d.get("conclusion")):
            structured = _raw
    except Exception:
        structured = None
    if structured is None:
        return "noop"

    _event_seq = task.get("event_seq")
    if status == "completed":
        if _event_seq:
            # 已落 event 结构化 → 幂等 already
            try:
                with db_lock:
                    _r = db.cursor().execute(
                        "SELECT structured_output FROM events WHERE seq=?", (_event_seq,)).fetchone()
                if _r and (_r[0] or "").strip() not in ("", "null"):
                    return "already"
            except Exception:
                pass
            _set_task_late_result(task_id, task, structured, status="completed")
            return "attached"
        # 无关联 event：set_task_signal 已把结构化写入 agent_tasks.result_text，无需再附
        return "already"
    # failed：仅占位失败可提升
    if task.get("exit_code") not in (0, None):
        return "already"
    _set_task_late_result(task_id, task, structured, status="completed")
    return "promoted"


def _set_task_late_result(task_id: str, task: dict, result_text: str, status: str):
    """补挂/提升：更新 agent_tasks 与关联 event 的结果存储（无副作用重放）。"""
    _now_iso = datetime.now(timezone.utc).isoformat()
    with db_lock:
        c = db.cursor()
        c.execute(
            "UPDATE agent_tasks SET status=?, result_text=?, exit_code=CASE WHEN ?='completed' THEN 0 ELSE exit_code END, "
            "completed_at=? WHERE id=? AND status IN ('completed','failed')",
            (status, result_text, status, _now_iso, task_id))
        _event_seq = task.get("event_seq")
        if _event_seq:
            c.execute(
                "UPDATE events SET status=?, structured_output=?, result=?, completed_at=? WHERE seq=?",
                (status, result_text, result_text, _now_iso, _event_seq))
        db.commit()
    log.info(f"task {task_id} late self-report: {status} + structured attached (enrich)")


def _handle_vod_recommend_complete(repo_key: str, issue_id: str, result_text: str,
                                   task_id: str, task: dict, repo_gitcode_token: str = ""):
    """VOD 智能推荐完成处理：解析 scene_id → 持久化 → 重派 VOD。

    remote worker 模式下 _execute_agent 不执行，此函数在 complete_task 中补触发，
    与 _execute_agent_inner 中的逻辑等价（event_pipeline.py L2261-2310）。
    """
    import re as _re
    # 查 task prompt 判断是否为智能推荐任务
    _task_prompt = ""
    try:
        from src.session_manager import db as _rdb, db_lock as _rdb_lock
        with _rdb_lock:
            c = _rdb.cursor()
            c.execute("SELECT prompt FROM agent_tasks WHERE id=?", (task_id,))
            _trow = c.fetchone()
            _task_prompt = _trow[0] if _trow else ""
    except Exception:
        pass
    if not (_task_prompt and "智能推荐器" in _task_prompt and "{{SKILL_DIR}}" not in _task_prompt):
        return  # 不是智能推荐任务，跳过

    _rec_scene = ""
    try:
        from src.api.event_store import parse_agent_output_json
        _scene_out = parse_agent_output_json(result_text) or {}
        _rec_scene = _scene_out.get("scene_id", "") or ""
    except Exception:
        pass
    if not _rec_scene:
        _m_rec = _re.search(r'"scene_id"\s*:\s*"([^"]*)"', result_text)
        _rec_scene = _m_rec.group(1) if _m_rec else ""

    if not _rec_scene:
        log.info(f"VOD recommend: no scene_id for {issue_id}, pipeline terminated")
        return

    from src.api.session_store import set_issue_scene
    from src.runtime.engine_select import _repo_user_id
    _vod_issue_creator = ""
    try:
        from src.session_manager import db as _evdb, db_lock as _evdb_lock
        with _evdb_lock:
            _ec = _evdb.cursor()
            _ec.execute(
                "SELECT actor FROM events WHERE repo_key=? AND issue_id=? AND actor!='' "
                "ORDER BY seq ASC LIMIT 1",
                (repo_key, issue_id))
            _erow = _ec.fetchone()
            if _erow:
                _vod_issue_creator = _erow[0] or ""
    except Exception:
        pass
    _vod_uid = _repo_user_id(repo_key, issue_creator=_vod_issue_creator)
    set_issue_scene(repo_key, issue_id, _rec_scene, scene_user_id=_vod_uid)
    log.info(f"VOD recommend: scene_id={_rec_scene} set for {issue_id}")

    from src.api.pipeline_config import get_first_node, get_pipeline_config
    _vod_node = get_first_node(_rec_scene, repo_key=repo_key)
    if not _vod_node:
        _pcfg = get_pipeline_config(_rec_scene, repo_key=repo_key)
        _nodes = (_pcfg or {}).get("nodes", [])
        if _nodes:
            _vod_node = _nodes[0]
    if not _vod_node:
        log.warning(f"VOD recommend: no vod node for scene={_rec_scene}, skip redispatch")
        return

    _vod_skill = _vod_node.get("skill", "vod")
    log.info(f"VOD recommend: redispatch VOD with skill={_vod_skill} for {issue_id}")

    # 构建 evt 给 _redispatch_vod_after_recommend（prompt_builder 需要 payload 字段）
    _issue_num = str(issue_id).lstrip("#")
    _evt = {
        "repository": task.get("account") or "",
        "issue_id": issue_id,
        "payload": {"issue_number": _issue_num, "issue": {"number": int(_issue_num) if _issue_num.isdigit() else _issue_num}},
    }
    _runtime_id = task.get("runtime_id") or 0
    # Issue #565: bot 建单只做场景判定不进执行棒——worker 侧 evt 是重建的、无原始标记，
    # 按该 issue 首个事件 actor（建单人）是否 bot 还原 _bot_opened，
    # _redispatch_vod_after_recommend 内据此短路重派（查询失败降级为不标记，人建单行为不变）。
    try:
        from src.user.db import get_all_bot_usernames
        _evt["_bot_opened"] = _vod_issue_creator in (get_all_bot_usernames() or [])
    except Exception:
        pass
    from src.api.event_pipeline import _redispatch_vod_after_recommend
    _redispatch_vod_after_recommend(repo_key, issue_id, _vod_skill, _evt, _runtime_id)


def _fetch_issue_comments(repo_key: str, repo: str, issue_num: str, token: str) -> list:
    """查询 issue 已有评论列表（按 provider 分流）。

    Issue #372 R10 P2-1: fail_task 发"Token 已过期"回帖前去重用。
    GitHub: GET /repos/{owner}/{repo}/issues/{num}/comments
    GitCode: GET /api/v5/repos/{repo}/issues/{num}/comments
    返回评论 dict 列表（每项含 body 字段），失败返回空列表。
    """
    try:
        if repo_key.startswith("github:"):
            from src.repo_key import split_owner_repo
            from src.user.repo_manager import _github_req
            _o, _r = split_owner_repo(repo_key)
            _result = _github_req(
                "GET",
                f"https://api.github.com/repos/{_o}/{_r}/issues/{issue_num}/comments",
                token,
            )
            return _result if isinstance(_result, list) else []
        else:
            import urllib.request, json as _json
            _url = f"https://gitcode.com/api/v5/repos/{repo}/issues/{issue_num}/comments?per_page=100"
            _req = urllib.request.Request(_url, headers={"Authorization": f"Bearer {token}"})
            _resp = urllib.request.urlopen(_req, timeout=15)
            return _json.loads(_resp.read())
    except Exception:
        return []


def _task_created_key(value):
    """created_at 归一键（比较用），返回 (可解析标志, epoch 秒, 原串)。

    历史上 agent_tasks.created_at 有 ISO 带时区（`2026-10-19T09:42:07.12+00:00`）与
    本地 `YYYY-MM-DD HH:MM:SS` 两种写法，字符串直接比较会在混写时误判。
    统一成同构三元组：可解析的恒大于不可解析的，同组内按时间戳比较，避免混类型比较。
    """
    _v = (value or "").strip()
    if not _v:
        return (0, 0.0, "")
    try:
        _dt = datetime.fromisoformat(_v.replace(" ", "T"))
        if _dt.tzinfo is None:
            _dt = _dt.replace(tzinfo=timezone.utc)
        return (1, _dt.timestamp(), _v)
    except Exception:
        return (0, 0.0, _v)


def _task_errors_json(task_id):
    """读取 agent_tasks.errors 的 JSON 列表（异常回退空列表）。"""
    try:
        with db_lock:
            c = db.cursor()
            row = c.execute("SELECT errors FROM agent_tasks WHERE id=?", (task_id,)).fetchone()
        if row and row[0]:
            try:
                v = json.loads(row[0])
                return v if isinstance(v, list) else [str(v)]
            except Exception:
                return [str(row[0])]
    except Exception:
        pass
    return []


def friendly_failure_hint(task: dict | None) -> tuple[str, str]:
    """失败任务 → (发生了什么, 下一步行动) 的友好提示（chat-remote-failure-hint-design §2.2）。

    纯判定、无副作用、不写库；输入 `get_task` 的行 dict。
    输入为空 / 未命中任何分类 → 兜底「原始原因截断 + 参考任务详情/worker 日志」，
    严禁返回空文案（避免再次出现「查询失败: 」空尾）。
    """
    if not task:
        return "任务不存在或已被清理", "请重新发起对话重试"
    _errors = task.get("errors") or ""
    _result = task.get("result_text") or ""
    _stderr = task.get("stderr") or ""
    _exit = task.get("exit_code")
    # 统一检索串：errors 可能是 JSON 数组/dict/字符串，统一展平后拼接
    _haystack = " ".join(str(x) for x in [_errors, _result, _stderr])
    # token 类：先精确分 NO_TOKEN（仓库未配置凭据，比"授权失效"更贴切），再走通用映射
    # （复用既有映射；401 已由 fail_task 内 detect 处理，这里只做展示分类）
    if "NO_TOKEN_AVAILABLE" in _haystack or "no token available" in _haystack:
        return "仓库未配置可用凭据", "请检查仓库的机器人/token 配置后重试"
    for _code, _msg in _R16_ERROR_MESSAGES.items():
        if _code in _haystack:
            return "登录/授权状态失效（%s）" % _msg, "请重新授权后重试"
    # 引擎崩溃 / CLI 或数据版本不兼容（本次事故类：SQLiteError / replacement_seq 等）
    _eng_cli_markers = ("engine exit_code", "SQLiteError", "no such column", "databaseMigrations",
                        "localMigrations", "migration", "cli not found", "exit_code=")
    if any(_m in _haystack.lower() or _m in _haystack for _m in _eng_cli_markers):
        _detail = ""
        if _exit is not None:
            _detail = f"（exit_code={_exit}）"
        return (f"引擎执行异常{_detail}：本地引擎 CLI 可能损坏或与数据版本不兼容",
                "请检查所选 worker 上的引擎是否正常，或更换引擎后重试")
    # 超时
    if (_exit is not None and (str(_exit).startswith("-") or int(_exit or 0) < 0)) \
            or "timed out" in _haystack.lower() or "timeout" in _haystack.lower() \
            or "超时" in _haystack:
        return "任务执行超时", "请稍后重试，或简化问题后重发"
    # 兜底：原始原因截断（取 errors ≈ result ≈ stderr 首个非空）
    _raw = ""
    for _src in (_errors, _result, _stderr):
        if _src:
            _raw = str(_src)
            break
    _raw = " ".join(_raw.split())[:200]
    if not _raw:
        _raw = "引擎未报告具体错误信息"
    return f"执行失败：{_raw}", "请参考任务详情或 worker 日志排障"


def fail_task(task_id, error, exit_code=-1):
    task = get_task(task_id)
    # 幂等保护：如果 task 已经 completed，不调 fail（防止 complete→fail 竞态覆盖）
    if task and task.get("status") == "completed":
        log.info(f"Task {task_id} already completed, skip fail_task (error={error[:60]})")
        return
    # 幂等保护 2：已 failed 的 task 重复收到 fail（worker 补报重放 / 多进程重复上报）
    # 直接跳过——否则每次重放都会重跑 finalize_event_archive + update_event_complete，
    # 把人工 redispatch 已 reset 回 running 的事件再写回 failed + 旧失败原因
    # （2026-10-19 补报重放事故：重派后 180ms 被旧任务僵尸上报劫持）。
    if task and task.get("status") == "failed":
        log.info(f"Task {task_id} already failed, skip fail_task (error={str(error)[:60]})")
        return
    # Issue #537 幂等保护 3：已 timeout_failed（pending 超时兜底终态）的任务
    # 不得被迟到的 worker 失败上报覆写为 plain failed（超时说明会被洗掉）。
    if task and task.get("status") == TASK_TIMEOUT_FAILED:
        log.info(f"Task {task_id} already timeout_failed, skip fail_task (error={str(error)[:60]})")
        return
    # 过期上报丢弃：同一 event_seq 下已存在更晚创建的任务（redispatch / recover_orphans 重跑），
    # 说明本上报属于被取代的旧执行实例 → 不得回写任务终态，更不得回写事件终态。
    if task and task.get("event_seq"):
        try:
            with db_lock:
                c = db.cursor()
                c.execute("SELECT id, created_at FROM agent_tasks WHERE event_seq=?",
                          (task["event_seq"],))
                _sibs = c.fetchall() or []
            _mine = task.get("id") or task_id
            _mykey = _task_created_key(task.get("created_at"))
            _newer = ""
            for _sid, _sca in _sibs:
                if _sid == _mine:
                    continue
                if _task_created_key(_sca) > _mykey:
                    _newer = _sid
                    break
            if _newer:
                log.info(f"Stale fail report discarded: task {task_id} superseded by {_newer} "
                         f"(event_seq={task['event_seq']}); task/event state left intact")
                return
        except Exception as e:
            log.warning(f"fail_task stale-report check failed: {e}")
    # Issue #403：瞬时上游抖动自动重试（判定层收口，worker 失败上报路径）。
    # 终态幂等（completed/failed 跳过）与过期上报丢弃均已通过，此处任务仍
    # running；可重试（瞬时分类 ∧ 无写操作证据 ∧ attempts 未耗尽）则退避
    # requeue 并跳过本函数全部判死写入。
    try:
        from src.runtime.transient_retry import retry_transient_task
        if retry_transient_task(task_id, exit_code, str(error or "")):
            log.info(f"Task {task_id}: transient failure scheduled for retry, "
                     f"skip fail_task (exit={exit_code})")
            return
    except Exception as _e:
        log.warning(f"fail_task: transient retry check failed for {task_id}: {_e}")
    _result_show = None  # Issue #403：耗尽判死 → 分类失败摘要（无 event_seq 时保持 None）
    if task and task.get("event_seq"):
        try:
            from src.api.event_store import update_event_complete
            from src.api.event_archive import finalize_event_archive
            # issue #403：耗尽后判死 → 分类失败摘要写 result（区分上游抖动 vs 配置错误）
            try:
                from src.runtime.transient_retry import format_stream_failure_result
                _retries = [e for e in _task_errors_json(task_id)
                            if isinstance(e, dict) and e.get("error_code") in
                            ("TRANSIENT_RETRY_EXHAUSTED", "TRANSIENT_RETRY")]
                if _retries:
                    _ex = next((e for e in _retries
                                if e.get("error_code") == "TRANSIENT_RETRY_EXHAUSTED"), _retries[-1])
                    _result_show = format_stream_failure_result(
                        _ex.get("error_class") or "", _ex.get("attempts") or len(_retries),
                        _ex.get("max") or len(_retries), _ex.get("reason") or "",
                        exit_code=exit_code, errors=[str(error or "")])
            except Exception as _fe:
                log.warning(f"fail_task: failure summary build failed for {task_id}: {_fe}")
            finalize_event_archive(task["repo_key"], task["event_seq"], "failed",
                                   exit_code, None, None,
                                   tokens=[_get_task_token(task)],
                                   issue_num_hint=task.get("issue_id") or "")
            # Issue #1503：_result_show 与 error 均为空时兜底含状态/exit_code 的默认摘要，
            # 不依赖 store 层兜底（fail_task 第一优先来源仍为分类失败摘要/原始错误）。
            _default_fail_summary = f"failed (exit_code={exit_code})"
            update_event_complete(task["event_seq"], "failed", exit_code=exit_code,
                                  completed_at=datetime.now(timezone.utc).isoformat(),
                                  result=(_result_show or (error or "")
                                          or _default_fail_summary)[:200])
        except Exception as e:
            log.warning(f"fail_task finalize failed: {e}")
    # Issue #403：耗尽判死时 errors 合并而非覆盖——上方 retry 检查已把
    # TRANSIENT_RETRY_EXHAUSTED（分类/次数/摘要）写入 errors，直接覆盖会
    # 洗掉诊断记录（部署实测 V5 抓到）。追加侧语义见 transient_retry._append_task_error
    # （append 最新记录 + errs[-5:]，无头部保护需求——与判死合并的首条保护是不同场景）。
    _fail_errors = json.dumps([str(error)] if error else [])
    try:
        _prior = _task_errors_json(task_id)
        if _prior:
            # 保留既有结构化记录（dict 的 TRANSIENT_RETRY_* 诊断），原文去重合并
            _merged = [str(error)] + [e for e in _prior if e != str(error)][:4]
            _fail_errors = json.dumps(_merged[:5])
    except Exception:
        pass
    # 外审第 5 轮实测发现：update_task_status 写入后，下方 NO_TOKEN_AVAILABLE
    # 段的两段式追加 + [-5:] 截断会把首位的本次失败原因挤出去（5 条满载场景：
    # final = hist0-3 + NO_TOKEN，new failure 丢失）。已改为按信息价值截断
    # （首条永不挤掉），复现测试见 tests/test_transient_retry.py UC7-3。
    # 失败原因双写（chat-remote-failure-hint-design §2.1）：失败路径 result_text 此前恒空，
    # chat 远端轮询只读 result_text → 用户只见光秃秃「查询失败」。此处同步写入可读原因，
    # errors 仍保留原始排障详情（可能含结构化诊断 dict，如 TRANSIENT_RETRY_*）。
    _result_text_write = (_result_show if _result_show is not None else "") \
        or str(error or "")[:500] \
        or f"failed (exit_code={exit_code})"  # Issue #1503：空诊断兜底
    update_task_status(task_id, "failed", exit_code=exit_code,
                       result_text=_result_text_write,
                       errors=_fail_errors)
    # PR #799 R2：worker 失败上报终态路径同样回填发布任务结果三分支——
    # 否则 SRE 发布任务 failed 终态后 release 永远停留 dispatched。
    # 幂等由钩子内部 finished 终态保护承担；普通任务（无 production_release
    # payload 锚点）在钩子入口直接返回，零影响。
    _backfill_release_subissue_on_complete(task, exit_code, str(error or ""))
    # Issue #372 R10/R16.1: 统一 token 过期检测（fail_task 统一入口）
    # task failed 时检查 error 是否含 401/auth 错误
    # R16.1: 按 token 来源统一分流（用户 / 全局 / 仓库级 / session），不再只针对路径 B
    # R18: 委托 TokenResolver.detect_and_mark_expired 验证 + 标记 + 返回 error_code
    # P2-2: 加 exit_code < 0 前置条件 + 按词匹配（避免 issue 号 401、耗时 401s 误报）
    _is_401 = False  # R16.6: 初始化供后续无 token 检测使用
    try:
        _err_str = str(error or "")
        # R20.19.2: 空 token 检测在 401 关键词检测之前执行（短路优先）
        # R20.19.1: task 获取 token 为空时直接写 NO_TOKEN_AVAILABLE
        _task_token = _get_task_token(task) if task else ""
        if not _task_token and task:
            _rk = task.get("repo_key") or ""
            if _rk:
                try:
                    with db_lock:
                        c = db.cursor()
                        c.execute("SELECT errors FROM agent_tasks WHERE id=?", (task_id,))
                        _row = c.fetchone()
                        _existing_errors = []
                        if _row and _row[0]:
                            try:
                                _existing_errors = json.loads(_row[0])
                                if not isinstance(_existing_errors, list):
                                    _existing_errors = [str(_existing_errors)]
                            except (json.JSONDecodeError, TypeError):
                                _existing_errors = [str(_row[0])]
                        _already_has_code = any(
                            isinstance(e, dict) and e.get("error_code") == "NO_TOKEN_AVAILABLE"
                            for e in _existing_errors
                        )
                        if not _already_has_code:
                            _existing_errors.append({
                                "error_code": "NO_TOKEN_AVAILABLE",
                                "message": _R16_ERROR_MESSAGES["NO_TOKEN_AVAILABLE"],
                                "rebind_url": "/#settings",
                            })
                            # 按信息价值截断：首条（本次失败原因）永不挤掉
                            # （外审第 5 轮：旧 [-5:] 在满载时把新错误截没了）
                            _existing_errors = _existing_errors[:1] + _existing_errors[1:][-4:] if len(_existing_errors) > 5 else _existing_errors
                            c.execute("UPDATE agent_tasks SET errors=? WHERE id=?", (json.dumps(_existing_errors), task_id))
                            db.commit()
                except Exception:
                    pass
        # P2-2: 收紧 401 匹配——去掉单独 "unauthorized" 关键词（太宽泛，权限不足 403 也可能含此词）
        # R20.19.4: 扩展关键词列表——git clone/push 失败的 error 可能不含 "401 unauthorized"
        _is_401 = exit_code < 0 and is_auth_401_error(_err_str)
        if _is_401 and task:
            _rk = task.get("repo_key") or ""
            if _rk:
                # R18: 委托 TokenResolver 统一检测 + 标记
                from src.token_resolver import TokenResolver, create_provider_from_repo_key
                _provider = create_provider_from_repo_key(_rk)
                _resolver = TokenResolver(_provider)
                _error_code = _resolver.detect_and_mark_expired(_rk)

                if _error_code:
                    _expired_provider = _provider.provider_name  # "gitcode" / "github"
                    _issue_id = task.get("issue_id") or ""
                    _comment_token = ""

                    # R18.6.9: 评论 token 通过 handler.get_comment_token() 获取
                    # handler 内部按级别差异化选择评论 token（含 fallback）
                    try:
                        from src.token_resolver import create_action_handler
                        # R25.3: 回帖链用 resolve_config_level（不走过期短路），保留 best-effort 行为
                        _resolve_result = _resolver.resolve_config_level(_rk)
                        _resolve_result.issue_id = _issue_id
                        _handler = create_action_handler(_resolve_result)
                        if _handler:
                            _comment_token = _handler.get_comment_token(_resolve_result) or ""
                        else:
                            # 无 handler → fallback 到全局 bot token
                            if _rk.startswith("github:"):
                                from src.user.db import get_global_github_bot_token
                                _comment_token = get_global_github_bot_token() or ""
                            else:
                                from src.user.db import _get_global_bot_token
                                _comment_token = _get_global_bot_token() or ""
                    except Exception as _cte:
                        log.warning(f"fail_task: handler.get_comment_token failed: {_cte}")
                        # fallback 到原有逻辑
                        if _rk.startswith("github:"):
                            from src.user.db import get_global_github_bot_token
                            _comment_token = get_global_github_bot_token() or ""
                        else:
                            from src.user.db import _get_global_bot_token
                            _comment_token = _get_global_bot_token() or ""
                        if not _comment_token:
                            _comment_token = _get_task_token(task) or ""

                    # R14.18: task errors 改为 append 模式——读取已有 errors → append error_code → 写回
                    # 保留原始错误详情（如 401 响应体），dashboard 同时显示原始错误和过期提示
                    _token_expired_err = {
                        "error_code": _error_code,
                        "message": _R16_ERROR_MESSAGES.get(_error_code, "token expired or revoked"),
                        "provider": _expired_provider,
                        "rebind_url": "/#account",
                    }
                    try:
                        with db_lock:
                            c = db.cursor()
                            c.execute("SELECT errors FROM agent_tasks WHERE id=?", (task_id,))
                            _row = c.fetchone()
                            _existing_errors = []
                            if _row and _row[0]:
                                try:
                                    _existing_errors = json.loads(_row[0])
                                    if not isinstance(_existing_errors, list):
                                        _existing_errors = [str(_existing_errors)]
                                except (json.JSONDecodeError, TypeError):
                                    _existing_errors = [str(_row[0])]
                            # R16.1.6 幂等：已存在相同 error_code 则不重复 append
                            _already_has_code = any(
                                isinstance(e, dict) and e.get("error_code") == _error_code
                                for e in _existing_errors
                            )
                            if not _already_has_code:
                                _existing_errors.append(_token_expired_err)
                                # 限制 errors 列表长度（dashboard _errors[:3] 切片，保留最近 5 条）
                                # 首条保护式（与 NO_TOKEN 段同修，外审 P0）：本次失败原因永不挤掉，
                                # 截断只作用于历史部分
                                _existing_errors = _existing_errors[:1] + _existing_errors[1:][-4:] if len(_existing_errors) > 5 else _existing_errors
                                _err_obj = json.dumps(_existing_errors)
                                c.execute("UPDATE agent_tasks SET errors=? WHERE id=?", (_err_obj, task_id))
                                db.commit()
                    except Exception:
                        pass
                    # issue 回帖提示用户重新绑定
                    # P2-1 去重：同一 issue 多个 task 401 失败时，只发一条"Token 已过期"评论
                    if _issue_id and _comment_token:
                        from src.api.event_pipeline import _post_result_to_issue, _issue_num_from
                        from src.repo_key import from_repo_key
                        _issue_num = _issue_num_from(_issue_id)
                        _repo = from_repo_key(_rk) or task.get("account") or ""
                        if _issue_num and _repo:
                            _pName = "GitHub" if _expired_provider == "github" else "GitCode"
                            _msg = (
                                f"<!-- grape-auto-comment -->\n"
                                f"⚠️ {_pName} Token 已过期或被撤销，本次任务未能完成。"
                                f"请在设置页重新绑定 {_pName} Token 后重试。"
                            )
                            # 去重：查 issue 已有评论，若已含"Token 已过期"则跳过（避免重复刷屏）
                            _already_notified = False
                            try:
                                _comments = _fetch_issue_comments(
                                    _rk, _repo, _issue_num, _comment_token
                                )
                                _already_notified = any(
                                    "Token 已过期" in (_c.get("body") or "")
                                    for _c in (_comments or [])
                                    if isinstance(_c, dict)
                                )
                            except Exception:
                                pass  # 查询失败不阻断发帖（降级为原行为：可能重复发帖）
                            if not _already_notified:
                                try:
                                    # R18.6.10: 走 handler.post_issue_comment()
                                    from src.token_resolver import create_action_handler
                                    # R25.3: 回帖链用 resolve_config_level（不走过期短路），保留 best-effort 行为
                                    _resolve_r = _resolver.resolve_config_level(_rk)
                                    _resolve_r.issue_id = _issue_id
                                    _h = create_action_handler(_resolve_r)
                                    if _h:
                                        _ok = _h.post_issue_comment(_rk, _issue_num, _msg, _resolve_r, task.get("agent_type") or "agent")
                                        if not _ok:
                                            raise RuntimeError("handler.post_issue_comment returned False")
                                    else:
                                        _post_result_to_issue(
                                            _repo, _issue_num, _msg, _comment_token,
                                            task.get("agent_type") or "", _rk,
                                            agent_result=False, task_id=task_id,
                                            seq=task.get("event_seq"),
                                        )
                                except Exception as _pe:
                                    log.warning(f"fail_task: R10 issue comment post failed: {_pe}")
                                    # R20.2.1: 回帖失败不静默——append COMMENT_POST_FAILED
                                    try:
                                        with db_lock:
                                            c = db.cursor()
                                            c.execute("SELECT errors FROM agent_tasks WHERE id=?", (task_id,))
                                            _row2 = c.fetchone()
                                            _errs2 = []
                                            if _row2 and _row2[0]:
                                                try:
                                                    _errs2 = json.loads(_row2[0])
                                                    if not isinstance(_errs2, list):
                                                        _errs2 = [str(_errs2)]
                                                except (json.JSONDecodeError, TypeError):
                                                    _errs2 = [str(_row2[0])]
                                            _has_cpf = any(isinstance(e, dict) and e.get("error_code") == "COMMENT_POST_FAILED" for e in _errs2)
                                            if not _has_cpf:
                                                _errs2.append({
                                                    "error_code": "COMMENT_POST_FAILED",
                                                    "message": "failed to post comment to issue (token expired or no permission)",
                                                })
                                                # 首条保护式（同 UC7-3 家族）：头部诊断不被挤掉
                                                _errs2 = _errs2[:1] + _errs2[1:][-4:] if len(_errs2) > 5 else _errs2
                                                c.execute("UPDATE agent_tasks SET errors=? WHERE id=?", (json.dumps(_errs2), task_id))
                                                db.commit()
                                    except Exception:
                                        pass
    except Exception as _e:
        log.warning(f"fail_task: R10/R16 token expiry check failed: {_e}")
    # R16.6: 无 token 检测——task 失败且无 401 时，检查是否因无 token 导致
    # 所有策略返回空（token 被短路或未配置）→ 写 NO_TOKEN_AVAILABLE error_code
    if task and not _is_401:
        try:
            _rk = task.get("repo_key") or ""
            if _rk:
                from src.token_resolver import TokenResolver, create_provider_from_repo_key
                _provider = create_provider_from_repo_key(_rk)
                _resolver = TokenResolver(_provider)
                _has_token = _resolver.get_token(_rk)
                if not _has_token:
                    _no_token_err = {
                        "error_code": "NO_TOKEN_AVAILABLE",
                        "message": _R16_ERROR_MESSAGES["NO_TOKEN_AVAILABLE"],
                        "rebind_url": "/#settings",
                    }
                    try:
                        with db_lock:
                            c = db.cursor()
                            c.execute("SELECT errors FROM agent_tasks WHERE id=?", (task_id,))
                            _row = c.fetchone()
                            _existing_errors = []
                            if _row and _row[0]:
                                try:
                                    _existing_errors = json.loads(_row[0])
                                    if not isinstance(_existing_errors, list):
                                        _existing_errors = [str(_existing_errors)]
                                except (json.JSONDecodeError, TypeError):
                                    _existing_errors = [str(_row[0])]
                            _already_has_code = any(
                                isinstance(e, dict) and e.get("error_code") == "NO_TOKEN_AVAILABLE"
                                for e in _existing_errors
                            )
                            if not _already_has_code:
                                _existing_errors.append(_no_token_err)
                                # 首条保护式（同 UC7-3 家族）：头部诊断不被挤掉
                                _existing_errors = _existing_errors[:1] + _existing_errors[1:][-4:] if len(_existing_errors) > 5 else _existing_errors
                                _err_obj = json.dumps(_existing_errors)
                                c.execute("UPDATE agent_tasks SET errors=? WHERE id=?", (_err_obj, task_id))
                                db.commit()
                    except Exception:
                        pass
        except Exception as _e:
            log.warning(f"fail_task: R16.6 no-token check failed: {_e}")
    # 死 session 检测（smart-session-recovery-design.md §2.3）：
    # 同一 issue_agent_session 下连续 ≥3 次 exit_code < 0（-15/-1/timeout）→ 清空 runtime_session_id
    # exit_code 由 worker fail 上报透传（超时/-1、引擎崩溃码）；不传时默认 -1；
    # exit=1（快速失败如 key/config 问题）不计入 session 故障，避免误清
    _isid = (task or {}).get("issue_session_id") or ""
    if _isid and exit_code < 0:
        try:
            with db_lock:
                c = db.cursor()
                c.execute(
                    "SELECT exit_code FROM agent_tasks WHERE issue_session_id=? ORDER BY created_at DESC LIMIT 3",
                    (_isid,))
                rows = c.fetchall()
                if len(rows) >= 3 and all((r[0] or 0) < 0 for r in rows):
                    from src.api.session_store import update_issue_session
                    # 指针与引擎标记一起清，防残留 engine 标记让后续异构引擎回写被误拦
                    update_issue_session(_isid, runtime_session_id="", session_engine="")
                    log.info(f"fail_task: dead session detected (consecutive failures), cleared runtime_session_id for {_isid}")
        except Exception as e:
            log.warning(f"fail_task session check failed: {e}")
    log.warning(f"Task failed: {task_id} exit={exit_code} err={str(error)[:120]}")
    # 可观察性埋点：任务失败
    try:
        from src.observability import record_task_terminal, record_task_failed
        _run_s = -1
        _total_s = -1
        if task:
            _now = datetime.now(timezone.utc)
            if task.get("started_at"):
                try:
                    _run_s = (_now - datetime.fromisoformat(task["started_at"])).total_seconds()
                except Exception:
                    pass
            if task.get("created_at"):
                try:
                    _total_s = (_now - datetime.fromisoformat(task["created_at"])).total_seconds()
                except Exception:
                    pass
            record_task_terminal(task_id, task.get("event_type") or task.get("agent_type") or "other",
                                 task.get("runtime_id", 0), "failed", _run_s, _total_s)
            record_task_failed(task_id, task.get("event_type") or task.get("agent_type") or "other",
                               task.get("runtime_id", 0), str(error)[:200])
    except Exception:
        pass
    # 事件总线：任务失败
    try:
        from src.observability.events import bus, Event
        bus.publish(Event(type="task:failed", task_id=task_id,
                          payload={"exit_code": exit_code, "error": str(error)[:200]}))
    except Exception:
        pass


# ===== 回报异步化（M2，见 concurrency-design.md §2.4）=====
# 供 M3 /api/runtime/* 回报端点使用：入队即返回，后台攒批写库，避免阻塞 event loop。
# queue_local 本地 worker 仍走同步 report_*（在 worker 线程内，无 event loop 阻塞）。

def start_report_ingestion_loop(loop=None):
    """启动回报异步落库循环（FastAPI lifespan 调用，见 main.py）。"""
    try:
        import asyncio
        _loop = loop or asyncio.get_running_loop()
        _task = _loop.create_task(_ingestion_loop())
        return _task
    except Exception as e:
        log.warning(f"report ingestion loop not started (sync fallback): {e}")
        return None


# Issue #587：回报落库失败可观测记录（task 级 errors 结构化追加，含重试计数）
_REPORT_FAILURE_COUNTS: dict = {}


def _record_report_failure(task_id: str, kind: str, reason: str):
    """回报失败落可见记录（agent_tasks.errors，追加合并 [-5:] 截断，不覆盖既有诊断）。"""
    try:
        with db_lock:
            c = db.cursor()
            c.execute("SELECT errors FROM agent_tasks WHERE id=?", (task_id,))
            row = c.fetchone()
            if not row:
                return
            _existing = []
            if row[0]:
                try:
                    _existing = json.loads(row[0]) or []
                except Exception:
                    _existing = [row[0]]
            _n = _REPORT_FAILURE_COUNTS.get(f"{task_id}:{kind}", 0) + 1
            _REPORT_FAILURE_COUNTS[f"{task_id}:{kind}"] = _n
            _entry = {
                "error_code": "REPORT_INGESTION_FAILED",
                "kind": kind,
                "task_id": task_id,
                "reason": str(reason)[:300],
                "retries": _n,
                "ts": datetime.now(timezone.utc).isoformat(),
            }
            # 同类失败记录更新语义：移除旧条目后追加最新（计数/原因/时间刷新）
            _existing = [e for e in _existing
                         if not (isinstance(e, dict) and e.get("error_code") == "REPORT_INGESTION_FAILED"
                                 and e.get("kind") == kind)]
            _existing = _existing + [_entry]
            _existing = _existing[-5:]
            c.execute("UPDATE agent_tasks SET errors=? WHERE id=?",
                      (json.dumps(_existing, ensure_ascii=False), task_id))
            db.commit()
            log.warning(f"report ingestion failed (recorded): task={task_id} kind={kind} "
                        f"retries={_n} reason={reason}")
    except Exception as _e:
        log.warning(f"_record_report_failure failed for {task_id}: {_e}")


async def _ingestion_loop():
    """攒批落库：messages/progress/usage 入队，批量写。complete/fail 仍同步（强一致）。"""
    import asyncio
    _report_queue: asyncio.Queue = asyncio.Queue(maxsize=2000)
    # 模块级队列引用（供 async 端点 put）
    global _ASYNC_REPORT_QUEUE
    _ASYNC_REPORT_QUEUE = _report_queue
    while True:
        try:
            batch = [await _report_queue.get()]
            while len(batch) < 50:
                try:
                    batch.append(await asyncio.wait_for(_report_queue.get(), timeout=0.2))
                except asyncio.TimeoutError:
                    break
            with db_lock:
                for kind, tid, payload, seq_start in batch:
                    try:
                        if kind == "messages":
                            report_messages(tid, payload, seq_start=seq_start)
                        elif kind == "progress":
                            report_progress(tid, payload)
                    except Exception as e:
                        log.warning(f"ingestion apply failed: {e}")
                        # Issue #587：失败落 task 级可观测记录（含原因/重试次数），
                        # 补偿入口 /tasks/{id}/converge-event + worker 侧补报队列兜底
                        _record_report_failure(tid, kind, str(e))
                db.commit()
        except asyncio.CancelledError:
            break
        except Exception as e:
            log.warning(f"ingestion loop error: {e}")
            await asyncio.sleep(1)


_ASYNC_REPORT_QUEUE = None


def enqueue_async_report(kind: str, task_id: str, payload, seq_start: int | None = None) -> dict | None:
    """异步回报入队（队列满降级同步，不丢数据）。seq_start：Issue #975 批次 seq 预留起点。

    返回（QA 复审，Spec #1）：同步兜底路径返回 report_messages 的实际落库回执
    （dict）；走异步队列时返回 None（实际回执以服务端 report_messages 日志为准）。
    """
    q = _ASYNC_REPORT_QUEUE
    if q is None:
        # 队列未启动 → 同步兜底
        if kind == "messages":
            return report_messages(task_id, payload, seq_start=seq_start)
        elif kind == "progress":
            report_progress(task_id, payload)
            return None
        return None
    try:
        q.put_nowait((kind, task_id, payload, seq_start))
        return None
    except Exception:
        # 队列满：同步降级
        if kind == "messages":
            return report_messages(task_id, payload, seq_start=seq_start)
        elif kind == "progress":
            report_progress(task_id, payload)
        return None


async def wait_task_terminal(task_id: str, timeout: float = 1800.0, poll_interval: float = 1.0) -> dict:
    """异步轮询任务直到终态（completed/failed/cancelled）。

    供 IM / web 等"入队 + 等结果"路径使用（见 multi-runtime-instance-design 配套）。
    超时抛 TimeoutError。返回终态 task dict（含 result_text / exit_code / usage_json.session_id）。

    poll_interval 从 3s 降为 1s——远端任务完成后飞书/web 等待者感知延迟减少 2/3，
    且不增加 DB 压力（单条 SELECT 查询 <1ms）。
    """
    import asyncio, time as _time
    deadline = _time.monotonic() + timeout
    while True:
        t = get_task(task_id)
        if t and t.get("status") in TASK_TERMINAL_STATUSES:
            return t
        if _time.monotonic() > deadline:
            raise TimeoutError(f"task {task_id} not finished within {timeout}s")
        await asyncio.sleep(poll_interval)


# ===== Phase 5: Batch stale recovery + heartbeat consolidation =====

def batch_recover_stale_tasks(timeout_seconds: int = 300) -> dict:
    """Batch-recover stale dispatched tasks (Phase 5).

    Scans all dispatched tasks older than timeout_seconds that haven't started running.
    Requeues them back to queued status so other engines can claim.

    This replaces the per-claim stale check with a batch scan, reducing lock contention
    for 10万+ device scenarios.
    """
    from datetime import datetime, timezone, timedelta
    cutoff = (datetime.now(timezone.utc) - timedelta(seconds=timeout_seconds)).isoformat()
    recovered = []
    with db_lock:
        c = db.cursor()
        try:
            # Find stale dispatched tasks (dispatched but not running, older than cutoff)
            c.execute(
                "SELECT id, runtime_id, event_seq FROM agent_tasks "
                "WHERE status='dispatched' AND started_at IS NOT NULL AND started_at < ? "
                "AND NOT EXISTS ("
                "  SELECT 1 FROM task_messages WHERE task_messages.task_id = agent_tasks.id"
                ") ORDER BY started_at ASC LIMIT 100",
                (cutoff,)
            )
            stale = c.fetchall()
            for row in stale:
                task_id, rid, seq = row[0], row[1], row[2]
                # Requeue: set back to queued, clear runtime assignment
                c.execute(
                    "UPDATE agent_tasks SET status='queued', started_at=NULL WHERE id=? AND status='dispatched'",
                    (task_id,)
                )
                if c.rowcount > 0:
                    # Decrement busy_slots（统一收口 release_busy_slot）
                    if rid:
                        release_busy_slot(rid, cursor=c)
                    recovered.append({"task_id": task_id, "runtime_id": rid, "seq": seq})
            db.commit()
        except Exception as e:
            log.warning(f"batch_recover_stale_tasks failed: {e}")
            db.rollback()

    if recovered:
        log.info(f"Batch stale recovery: {len(recovered)} tasks requeued")
    return {"recovered": len(recovered), "tasks": recovered}


def heartbeat_consolidated(credential_id: int, runtime_ids: list[int], client_ip: str = "") -> dict:
    """Consolidated heartbeat (Phase 5): worker-level heartbeat instead of per-engine.

    One heartbeat call updates the credential + all its runtimes in a single transaction.
    Also triggers batch stale recovery opportunistically (every 10th heartbeat).
    """
    now = datetime.now(timezone.utc).isoformat()
    with db_lock:
        c = db.cursor()
        # Update credential
        c.execute(
            "UPDATE runtime_credentials SET last_seen_at=?, last_ip=? WHERE id=? AND status='active'",
            (now, client_ip, credential_id)
        )
        # Update all runtimes for this credential
        if runtime_ids:
            # Issue #1496：在线态统一写入口（status+last_seen_at 同事务，含遗留
            # health 列派生同步）；本机直连行（credential_id=0）不在此路径
            # （worker 心跳只上报自己凭据名下的 runtime，归属由下面 UPDATE 的
            # credential_id 条件兜底——先筛出归属行再逐个走统一入口）。
            ph = ",".join("?" for _ in runtime_ids)
            c.execute(
                f"SELECT id FROM agent_runtimes WHERE id IN ({ph}) AND credential_id=?",
                [*runtime_ids, credential_id])
            for (_rid,) in c.fetchall():
                apply_runtime_state(c, _rid, "online", now)
        db.commit()

    # Opportunistic stale recovery (every call, but batch is cheap)
    # In production, this would be rate-limited to every N seconds
    return {"status": "ok", "timestamp": now}


def reap_stale_runtimes() -> dict:
    """清理僵尸 runtime：把 last_seen_at 过期（> _RUNTIME_STALE_SECONDS）的远端引擎标 offline，
    并把绑定到这些 runtime 上的 dispatched 任务回收为 queued（防任务永久卡死）。

    场景：AI DevSpace 被删后 runtime 行残留 status=online 但 worker 不再心跳。
    由 main.py 每 60 秒调用一次（_reap_stale_loop）。
    """
    from src.runtime.health import parse_dt
    now = time.time()
    cutoff_iso = (datetime.now(timezone.utc) - timedelta(seconds=_RUNTIME_STALE_SECONDS)).isoformat()
    # Issue #259：回收分级——running 任务的对齐 worker 本地超时（Issue #516 起按 agent
    # 配置化对齐，见 _recover_cutoff_for_agent），防假死 worker（断连但任务还在本地跑）
    # 的服务端重派与本地执行重叠造成双跑。dispatched 任务从未开始执行，无双跑风险，
    # 跟心跳阈值走（100s）。
    # Issue #470 后续：回收阈值实时取值（env > meta runtime_task_timeout），
    # 管理台改 runtime_task_timeout 后回收循环下一轮（≤60s）即跟随，无需重启。
    _recover_cutoff_iso = (datetime.now(timezone.utc) - timedelta(seconds=_get_recover_stale_cfg())).isoformat()
    reaped = []
    with db_lock:
        c = db.cursor()
        try:
            # 查找远端引擎（credential_id>0）status=online 但心跳过期的行
            # Issue #259：去掉 last_seen_at IS NOT NULL——NULL（从未心跳）但 status=online 的
            # 幽灵行也算过期，一起标 offline（原查询会永久漏掉它们）
            c.execute(
                "SELECT id, name, credential_id, last_seen_at FROM agent_runtimes "
                "WHERE status='online' AND credential_id > 0 "
                "AND (last_seen_at IS NULL OR last_seen_at='' OR last_seen_at < ?)",
                (cutoff_iso,)
            )
            stale = c.fetchall()
            for row in stale:
                rid, rname, cid = row[0], row[1], row[2]
                # 标记 offline（Issue #1496：走统一写入口，status+last_seen_at 同事务，
                # 遗留 health 列同步为派生值——回收后的行不再残留 health='online'）
                apply_runtime_state(c, rid, "offline", "")
                # 回收该 runtime 上的 dispatched 任务 → queued（从未开始，零双跑风险，100s）
                # runtime_id 置 0（NOT NULL 列不能置 NULL；0 = 无主池任务，可被任意引擎 claim）
                c.execute(
                    "UPDATE agent_tasks SET status='queued', started_at=NULL, runtime_id=0, attempts=attempts+1 "
                    "WHERE runtime_id=? AND status='dispatched'",
                    (rid,)
                )
                recovered = c.rowcount
                # 回收 running 任务 → queued（可能还在假死 worker 本地执行，阈值对齐 worker
                # 本地超时——worker 本地到点自杀后才重派，双跑窗口=0）。
                # Issue #516：阈值按 agent_type 超时配置化对齐（coding 默认 7200s），
                # 防服务端在 worker 仍在本地执行长任务时重派双跑。宽口径预筛（PR #722
                # 审查修复：取各 agent 超时的**最小值**作为最宽回收窗口——原取最大值
                # 会把短超时 agent（如 vod 3600s）已到回收点、但未满最大阈值 7200s 的
                # running 任务永久漏掉）后逐任务按各自 agent 的 cutoff 精确判定。
                try:
                    from src.runtime.task_timeout import (AGENT_TIMEOUT_DEFAULTS,
                                                          DEFAULT_TASK_TIMEOUT, load_timeout_config)
                    _cfg_ad = load_timeout_config().get("agent_defaults") or {}
                    _min_to = min([DEFAULT_TASK_TIMEOUT,
                                   _WORKER_TASK_TIMEOUT_DEFAULT] +
                                  [float(v) for v in AGENT_TIMEOUT_DEFAULTS.values()] +
                                  [float(v) for v in _cfg_ad.values()
                                   if isinstance(v, (int, float)) and float(v) > 0])
                except Exception:
                    _min_to = float(_TASK_RECOVER_STALE_SECONDS)
                _wide_cutoff = (datetime.now(timezone.utc) - timedelta(
                    seconds=min(_min_to, _TASK_RECOVER_STALE_SECONDS))).isoformat()
                c.execute(
                    "SELECT id, agent_type, started_at FROM agent_tasks"
                    " WHERE runtime_id=? AND status='running'"
                    "   AND (started_at IS NULL OR started_at='' OR started_at < ?)",
                    (rid, _wide_cutoff)
                )
                for _task_id, _agent_t, _started_at in c.fetchall():
                    _cut = _recover_cutoff_for_agent(_agent_t or "")
                    if _started_at and _cut and _started_at >= _cut:
                        continue  # 该 agent 超时更宽（如 coding 7200s），未到其回收点
                    # Issue #1071（P3）：回炉前查活性。引擎心跳断 ≠ 任务死——worker 还在
                    # 写 task_messages 就说明它活着。不回炉的理由同上：回炉会把 started_at
                    # 改写成新的认领时刻，老 worker 的 run_token 立刻失效 → 409 stale 永败。
                    try:
                        if task_recently_active(_task_id):
                            log.info(f"reap_stale_runtimes: skip requeue task {_task_id} "
                                     f"(recent task_messages activity, runtime offline)")
                            continue
                    except Exception as _ae:
                        log.warning(f"reap_stale_runtimes: activity guard failed for "
                                    f"task {_task_id}: {_ae}")
                    c.execute(
                        "UPDATE agent_tasks SET status='queued', started_at=NULL, runtime_id=0,"
                        " attempts=attempts+1 WHERE id=? AND status='running'",
                        (_task_id,)
                    )
                    # PR #722 审查：rowcount 必须在每次 UPDATE 后立即累计（放循环外只会
                    # 计入最后一条，且循环零次时残留 SELECT 的 -1，导致 recovered 为负）
                    recovered += c.rowcount
                if recovered > 0:
                    c.execute(
                        "UPDATE agent_runtimes SET busy_slots=0 WHERE id=?",
                        (rid,)
                    )
                reaped.append({"runtime_id": rid, "name": rname, "recovered_tasks": recovered})
            if stale:
                db.commit()
        except Exception as e:
            log.warning(f"reap_stale_runtimes failed: {e}")
            try:
                db.rollback()
            except Exception:
                pass

    # Issue #134 扩展：queued 任务超时降级——防饿死机制
    # 当所有候选引擎健康分过低时，任务会一直 queued 没人认领。
    # 超过 _STALE_QUEUED_THRESHOLD 秒后清空 pool_engine_ids 放行，让任意在线引擎认领。
    try:
        _check_stale_queued_tasks()
    except Exception as _sqe:
        log.warning(f"reap_stale_runtimes: _check_stale_queued_tasks failed: {_sqe}")

    # 引擎绑定真实性同步：离线超阈值（默认 8h）的引擎，真实清理场景绑定 + 审计留痕。
    # 设计文档 docs/weekly-06/engine-binding-truth-sync-design.md §4.2。
    try:
        from src.runtime.engine_select import _detach_dead_engine_bindings
        _detach_dead_engine_bindings()
    except Exception as _de:
        log.warning(f"reap_stale_runtimes: _detach_dead_engine_bindings failed: {_de}")

    return {"reaped": reaped}


_STALE_QUEUED_THRESHOLD = int(os.environ.get("HEALTH_STALE_QUEUED_THRESHOLD", "120"))
# Issue #1487 改动C 闭环：queued 任务超过硬窗口仍无引擎认领 → 诚实失败，防「健康分健康但
# 物理不可达」的池把任务永久挂住（实测：claim 不再判死后该池永不裁决）。
_STALE_QUEUED_HARD_THRESHOLD = int(os.environ.get("HEALTH_STALE_QUEUED_HARD_THRESHOLD", "900"))


def _check_stale_queued_tasks():
    """Issue #253 改动3：queued 任务超时 → 限时等待收敛（宁失败，不跑飞）。

    当任务 queued 超过 _STALE_QUEUED_THRESHOLD（默认 120 秒）且 pool_engine_ids 非空时，
    按 issue #253 硬约束语义收敛（方案 A：低健康分限时等待，超时失败）：
    - 池中有健康引擎且物理可达 → 等待（引擎可能马上认领）；超过
      _STALE_QUEUED_HARD_THRESHOLD（默认 900 秒）仍未被认领 → 诚实失败（防永久挂单）
    - 池中健康分达标但物理不可达（无标签能力/scope 不符/心跳过期）→ 两把尺子矛盾，
      以物理可达性为准，立即按原因分类失败（Issue #1487 改动C 闭环）
    - 引擎不健康但物理可达 → 有界等待：超时后按来源分流（local-claimer 设计 §3.3）：
        * pool_engine_source='auto'（系统自动选择的绑定）→ 降级：清空白名单重新入队
          （放行全站认领，含本机 direct 引擎），置 source='degraded' 防反复，result 记录审计
        * 'scene'/''（用户显式绑定/历史未标记）→ 任务 failed（#253 硬约束，不跑飞）
    - 引擎既不健康又不可达 → 同上按来源分流（auto 降级 / scene 失败）

    空绑定任务不进本函数（查询条件 pool_engine_ids!=''），广播语义不受影响。
    """
    cutoff = (datetime.now(timezone.utc) - timedelta(seconds=_STALE_QUEUED_THRESHOLD)).isoformat()
    try:
        with db_lock:
            c = db.cursor()
            # 查询超时的 queued 任务（带 pool_engine_ids 限定）
            c.execute(
                "SELECT id, pool_engine_ids, pool_tag, repo_key, user_id, created_at "
                "FROM agent_tasks "
                "WHERE status='queued' AND pool_engine_ids!='' AND pool_engine_ids IS NOT NULL "
                "AND created_at <= ?",
                (cutoff,))
            stale_tasks = c.fetchall()
            if not stale_tasks:
                return
            import json as _json
            hard_cutoff = (datetime.now(timezone.utc)
                           - timedelta(seconds=_STALE_QUEUED_HARD_THRESHOLD)).isoformat()
            for tid, peids, task_tag, task_repo, task_uid, task_created in stale_tasks:
                try:
                    _engine_ids = _json.loads(peids) if peids else []
                except Exception:
                    _engine_ids = []
                if not _engine_ids:
                    continue
                # Issue #1487 改动C 闭环：claim 侧不再当场判死，本函数必须真能裁决。
                # 「健康分」与「物理可达性」是两把尺子，分别算，矛盾时以物理为准。
                _reachable = _check_pool_engines_reachable(
                    _engine_ids, task_tag or "", task_repo or "", task_user_id=task_uid or 0)
                _has_healthy = _check_pool_has_healthy(_engine_ids)
                _hard_expired = (task_created or "") <= hard_cutoff
                if _has_healthy:
                    if not _reachable:
                        log.warning(f"_check_stale_queued_tasks: engine(s) {_engine_ids} healthy by score "
                                    f"but physically unreachable -> fail task {tid} (Issue #1487)")
                        _fail_task_pool_unreachable(c, tid, _engine_ids, task_tag or "",
                                                    repo_key=task_repo or "",
                                                    task_user_id=task_uid or 0,
                                                    note="健康分达标但物理不可达")
                        continue
                    if not _hard_expired:
                        continue  # 正常等待：健康且可达，等引擎领取
                    log.warning(f"_check_stale_queued_tasks: task {tid} queued past hard window "
                                f"({_STALE_QUEUED_HARD_THRESHOLD}s) with reachable pool -> fail (Issue #1487)")
                    _fail_task_pool_unclaimed(c, tid, _engine_ids, task_tag or "",
                                              waited_seconds=_STALE_QUEUED_HARD_THRESHOLD)
                    continue
                # local-claimer 设计 §3.3：白名单全灭时按来源分流（显式绑定不降级，自动绑定放行）
                _source = ""
                try:
                    c.execute("SELECT pool_engine_source FROM agent_tasks WHERE id=?", (tid,))
                    _sr = c.fetchone()
                    _source = (_sr[0] or "") if _sr else ""
                except Exception:
                    _source = ""
                if _source == "auto":
                    # 系统自动选择的绑定全灭 → 降级放行全站（含本机 direct），只降一次
                    c.execute(
                        "UPDATE agent_tasks SET pool_engine_ids='', pool_engine_source='degraded', "
                        "result_text=COALESCE(NULLIF(result_text,''), ?) WHERE id=? AND status='queued'",
                        (f"[degraded] bound engines {_engine_ids} all unhealthy/unreachable at "
                         f"{datetime.now(timezone.utc).isoformat()}; released to global pool "
                         f"(source=auto, local-claimer design)", tid))
                    db.commit()
                    log.info(f"_check_stale_queued_tasks: engine(s) {_engine_ids} all down, "
                             f"source=auto -> degraded release task {tid} to global pool")
                    continue
                # 'scene' / '' / 'degraded'：用户显式绑定（或历史/已降级）→ 保持 #253 硬约束
                # 健康分不达标 → 再检查物理可达性（在线 + 有 tag 能力 + scope 匹配 + 有并发槽）
                # Issue #253 方案 A：物理可达但低分 → 限时等待已到点（120s），诚实失败；
                # 物理不可达 → 直接失败（不撕证放行全站）
                if _reachable:
                    log.info(f"_check_stale_queued_tasks: engine(s) {_engine_ids} unhealthy but reachable, "
                             f"wait window elapsed -> fail task {tid} (Issue #253)")
                    _fail_task_pool_unhealthy(c, tid, _engine_ids, _STALE_QUEUED_THRESHOLD)
                else:
                    # Issue #1487 改动D：带上 repo/user 上下文，文案分类更准确（scope 不匹配可归因）
                    _fail_task_pool_unreachable(c, tid, _engine_ids, task_tag or "",
                                                repo_key=task_repo or "", task_user_id=task_uid or 0)
    except Exception as e:
        log.warning(f"_check_stale_queued_tasks failed: {e}")
