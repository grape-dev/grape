"""Issue #4346：`session/new` 失败分类 + session 通道熔断 + 换引擎重试。

背景（实测 1 小时 7 条任务 failed，跨 3 仓 4 节点）：引擎 `session/new` 等满
100s 后任务直接 failed，无重试、无换引擎、无熔断，只能人工重派。本模块补四层
防护：

1. 失败可区分：`ENGINE_UNREACHABLE` / `SESSION_CREATE_BLOCKED` /
   `ENGINE_MISCONFIGURED`；
2. 观测载荷：`engine_id` / `queue_depth` / `disk_avail_bytes` 恒存在（取不到为
   ``None``）；
3. session 通道独立熔断：连续 N 次建会话超时 → 长熔断（默认 600s）并剔除调度
   候选；低于阈值只开短冷却（默认 60s），保证 requeue 后**不会回到同一引擎**
   （= 换引擎重试）；冷却结束半开，只放行 1 次探测；
4. 配置类 500（无 LLM provider）→ 标记引擎不可用并换引擎，不再反复打到坏引擎。

设计约束：
- session 熔断**不改写**既有 `ENGINE_OFFLINE` / backend 熔断语义（#3928 的
  `backend_status` / `circuit_*` 列由 `backend_health` 独占）；本模块只写
  `session_*` / `llm_provider_*` 列。
- 所有读路径 fail-open：存量库缺列 / 查询异常 → 不拦截（迁移幂等补列）。
- 模块 import 期只用 stdlib（worker 侧 ACP 文案格式化也会 import 本模块）。
"""
from __future__ import annotations

import os
import re
import shutil
from datetime import datetime, timedelta, timezone

from src.kernel.config import log

# ===== 错误码 =====
ENGINE_UNREACHABLE = "ENGINE_UNREACHABLE"          # 连接/握手期失败：写不进去、EOF、spawn 失败
SESSION_CREATE_BLOCKED = "SESSION_CREATE_BLOCKED"  # TCP 可达、请求已发出、100s 无响应
ENGINE_MISCONFIGURED = "ENGINE_MISCONFIGURED"      # 引擎返回 No LLM provider configured 等配置类错误

SESSION_STAGE = "session/new"

# ===== 默认阈值（调用期读 env，运行期可调）=====
_DEFAULT_STREAK = 3          # 连续 N 次超时 → 熔断
_DEFAULT_CIRCUIT_SECONDS = 600   # 熔断窗口（10min）
_DEFAULT_RETRY_COOLDOWN = 60     # 单次超时的短冷却（保证重试换引擎）
_DEFAULT_MAX_RETRY = 1           # 超时后换引擎重试次数
_DEFAULT_MISCONFIG_SECONDS = 3600  # 配置类错误的长熔断（等重新注册/修配置）


def _env_int(name: str, default: int, minimum: int = 1) -> int:
    try:
        return max(minimum, int(os.environ.get(name, str(default))))
    except (TypeError, ValueError):
        return default


def session_timeout_streak_threshold() -> int:
    """连续 N 次 `session/new` 超时即熔断（env RUNTIME_SESSION_TIMEOUT_STREAK）。"""
    return _env_int("RUNTIME_SESSION_TIMEOUT_STREAK", _DEFAULT_STREAK)


def session_circuit_seconds() -> int:
    """熔断窗口秒数（env RUNTIME_SESSION_CIRCUIT_SECONDS，默认 600）。"""
    return _env_int("RUNTIME_SESSION_CIRCUIT_SECONDS", _DEFAULT_CIRCUIT_SECONDS)


def session_retry_cooldown_seconds() -> int:
    """单次超时的短冷却（env RUNTIME_SESSION_RETRY_COOLDOWN，默认 60）。"""
    return _env_int("RUNTIME_SESSION_RETRY_COOLDOWN", _DEFAULT_RETRY_COOLDOWN)


def session_max_retry() -> int:
    """超时后换引擎重试上限（env RUNTIME_SESSION_MAX_RETRY，默认 1；0 表示不重试）。"""
    return _env_int("RUNTIME_SESSION_MAX_RETRY", _DEFAULT_MAX_RETRY, minimum=0)


def misconfig_circuit_seconds() -> int:
    """配置类错误熔断窗口（env RUNTIME_SESSION_MISCONFIG_SECONDS，默认 3600）。"""
    return _env_int("RUNTIME_SESSION_MISCONFIG_SECONDS", _DEFAULT_MISCONFIG_SECONDS)


# ===== 失败分类 =====

_MISCONFIG_RE = re.compile(r"no\s+llm\s+provider", re.I)
_SESSION_SIGNAL_RE = re.compile(
    r"(session/new|session/resume|session\s+failed|initialize\s+failed|"
    r"acp\s+initialize|acp\s+probe|llm\s+provider)",
    re.I)
# 握手期（initialize）失败：引擎根本没能建立 ACP 会话 → 不可达（#416/#1487 口径）。
_INIT_RE = re.compile(r"initialize", re.I)
_TIMEOUT_RE = re.compile(
    r"(session/new|session/resume)[\s\S]{0,40}timeout"
    r"|session\s+failed:[\s\S]{0,80}timeout"
    r"|timeout\s+after\s+\d+(?:\.\d+)?s", re.I)
_UNREACHABLE_RE = re.compile(
    r"(write\s+(?:session/new|initialize)"
    r"|connection\s+(?:refused|reset|closed|aborted)"
    r"|broken\s+pipe|no\s+such\s+file|not\s+found\s+in\s+path"
    r"|spawn\s+failed|engine\s+startup\s+failed|failed\s+to\s+start\s+(?:the\s+)?engine"
    r"|\bEOF\b)", re.I)


def classify_session_failure(text) -> dict | None:
    """把引擎失败文案归类为 session 通道错误码；无 session 信号返回 None。

    判定顺序（固定，便于测试与排障对齐）：
    1. `No LLM provider configured` → ``ENGINE_MISCONFIGURED``（配置类 500）；
    2. 无 session/ACP 信号 → ``None``（不干扰既有失败链路）；
    3. 握手期（initialize）失败 → ``ENGINE_UNREACHABLE``（会话根本没建立）；
    4. 命中 session/new 超时 → ``SESSION_CREATE_BLOCKED``（请求已发出、引擎可达但阻塞）；
    5. 命中 write/EOF/connection/spawn → ``ENGINE_UNREACHABLE``；
    6. 其余 session 期错误（如 -32603 internal error）→ ``SESSION_CREATE_BLOCKED``。
    """
    s = str(text or "").strip()
    if not s:
        return None
    if _MISCONFIG_RE.search(s):
        return {"error_code": ENGINE_MISCONFIGURED, "stage": SESSION_STAGE,
                "reason": s[:500]}
    if not _SESSION_SIGNAL_RE.search(s):
        return None
    if _INIT_RE.search(s) and not re.search(r"session/(?:new|resume)", s, re.I):
        return {"error_code": ENGINE_UNREACHABLE, "stage": "initialize",
                "reason": s[:500]}
    if _TIMEOUT_RE.search(s):
        return {"error_code": SESSION_CREATE_BLOCKED, "stage": SESSION_STAGE,
                "reason": s[:500]}
    if _UNREACHABLE_RE.search(s):
        return {"error_code": ENGINE_UNREACHABLE, "stage": SESSION_STAGE,
                "reason": s[:500]}
    return {"error_code": SESSION_CREATE_BLOCKED, "stage": SESSION_STAGE,
            "reason": s[:500]}


def format_session_error(method: str, err) -> str:
    """worker 侧统一失败文案：保留原文并追加稳定分类标记。

    服务端分类器与排障都靠该标记，禁止各 ACP 模块各自拼文案（文案漂移会让
    `SESSION_CREATE_BLOCKED` 静默退化成普通失败）。
    """
    cls = classify_session_failure(f"{method} {err}") or {}
    code = cls.get("error_code") or SESSION_CREATE_BLOCKED
    return f"{method} failed: {err} [{code}]"


# ===== 观测载荷 =====

def probe_disk_avail_bytes(path: str = "/") -> int | None:
    """宿主可用磁盘水位（字节）；探测失败返回 None（字段仍存在）。"""
    try:
        return int(shutil.disk_usage(path).free)
    except Exception:
        return None


def _db():
    from src.kernel.owner_db import owner_db
    return owner_db("runtime")


def _lock():
    """与 backend_health / engine_select 同锁口径（runtime 域连接配套锁）。"""
    from src.kernel import db_registry
    if db_registry.uses_own_dsn("runtime"):
        return db_registry.domain_lock("runtime")
    from src.kernel.session_manager import db_lock
    return db_lock


def probe_queue_depth(runtime_id: int) -> int | None:
    """引擎侧排队/在跑任务数（agent_tasks 同 runtime 的 queued/running）；失败 None。"""
    if not runtime_id:
        return None
    try:
        with _lock():
            row = _db().cursor().execute(
                "SELECT COUNT(*) FROM agent_tasks WHERE runtime_id=? "
                "AND status IN ('queued','running')", (int(runtime_id),)).fetchone()
        return int(row[0] or 0) if row else 0
    except Exception as e:
        log.debug(f"session_guard: queue depth probe failed (runtime={runtime_id}): {e}")
        return None


def failure_payload(runtime_id, engine_name: str = "", cls: dict | None = None) -> dict:
    """session 失败结构化载荷：`engine_id/queue_depth/disk_avail_bytes` 恒存在。

    取不到一律 ``None``（字段不得缺省——下游按字段存在性解析，缺字段会静默丢观测）。
    """
    cls = cls or {}
    rid = int(runtime_id or 0) or None
    return {
        "error_code": cls.get("error_code") or SESSION_CREATE_BLOCKED,
        "stage": cls.get("stage") or SESSION_STAGE,
        "engine_id": rid,
        "engine": (engine_name or "").strip(),
        "queue_depth": probe_queue_depth(rid) if rid else None,
        "disk_avail_bytes": probe_disk_avail_bytes(),
        "reason": str(cls.get("reason") or "")[:500],
        "at": datetime.now(timezone.utc).isoformat(),
    }


# ===== 熔断状态（agent_runtimes.session_* / llm_provider_*）=====

_SESSION_COLUMNS = (
    "session_fail_streak", "session_fail_total", "session_circuit_until",
    "session_circuit_half_open", "session_last_error_code",
    "session_last_failure_at", "session_last_success_at",
)


def _runtime_name(runtime_id: int) -> str:
    try:
        with _lock():
            row = _db().cursor().execute(
                "SELECT name FROM agent_runtimes WHERE id=?", (int(runtime_id),)).fetchone()
        return (row[0] or "") if row else ""
    except Exception:
        return ""


def _session_row(c, runtime_id: int):
    """读 session 状态行；缺列/异常返回 None（fail-open，旧库不阻断主链路）。"""
    try:
        return c.execute(
            "SELECT COALESCE(session_fail_streak,0), COALESCE(session_fail_total,0), "
            "COALESCE(session_circuit_until,''), COALESCE(session_circuit_half_open,0), "
            "COALESCE(session_last_error_code,''), COALESCE(session_last_failure_at,''), "
            "COALESCE(session_last_success_at,''), COALESCE(llm_provider_ok,1), "
            "COALESCE(llm_provider_reason,'') "
            "FROM agent_runtimes WHERE id=?", (int(runtime_id),)).fetchone()
    except Exception as e:
        log.debug(f"session_guard: session row read skipped (runtime={runtime_id}): {e}")
        return None


def _ts(value):
    from src.domains.runtime.engine.health import parse_dt
    return parse_dt(value)


def session_eligible(until: str, half, llm_ok, now: float | None = None) -> bool:
    """session 通道准入纯判定（认领/派发/池可达共用一把尺子）。

    - ``llm_provider_ok=0`` → 永不放行（配置类错误必须重新注册修配置）；
    - 熔断窗口内 → 拒绝；
    - 窗口结束且无在飞半开探测（``half=0``）→ 放行一次探测；有在飞探测 → 拒绝。
    """
    if llm_ok is not None and int(llm_ok or 0) == 0:
        return False
    if not until:
        return True
    now_ts = datetime.now(timezone.utc).timestamp() if now is None else float(now)
    until_ts = _ts(until)
    if until_ts is None:
        return True
    if until_ts > now_ts:
        return False
    return int(half or 0) == 0


def session_claim_allowed(runtime_id: int, now: float | None = None, cursor=None) -> bool:
    """认领/派发闸门：session 熔断或 provider 缺失 → False（读不到 → True）。"""
    rid = int(runtime_id or 0)
    if not rid:
        return True
    try:
        if cursor is not None:
            row = _session_row(cursor, rid)
        else:
            with _lock():
                row = _session_row(_db().cursor(), rid)
    except Exception:
        return True
    if not row:
        return True
    _streak, _total, until, half, _code, _fat, _sat, llm_ok, _lpreason = row
    return session_eligible(until, half, llm_ok, now)


def _circuit_open_now(until: str, streak, code: str = "", now: float | None = None) -> bool:
    """是否处于**真熔断**（区别于单次超时的短冷却）。

    短冷却与熔断共用 `session_circuit_until`（都是「认领闸门挡到某时刻」），但
    健康分/看板口径必须区分：只有 streak 达到阈值，或配置类错误（provider 缺失）
    才对外呈现 `session_circuit_open=True`。
    """
    if not until:
        return False
    ts = _ts(until)
    now_ts = datetime.now(timezone.utc).timestamp() if now is None else float(now)
    if ts is None or ts <= now_ts:
        return False
    if (code or "") == ENGINE_MISCONFIGURED:
        return True
    return int(streak or 0) >= session_timeout_streak_threshold()


def session_circuit_open(runtime_id: int, now: float | None = None) -> bool:
    """当前是否处于 session 熔断（健康分/看板透出用）。"""
    rid = int(runtime_id or 0)
    if not rid:
        return False
    try:
        with _lock():
            row = _session_row(_db().cursor(), rid)
    except Exception:
        return False
    if not row:
        return False
    streak, _total, until, _half, code = row[0], row[1], row[2], row[3], row[4]
    return _circuit_open_now(until, streak, code, now)


def session_state(runtime_id: int) -> dict:
    """健康分/接口透出的 session 层指标（缺列/异常 → 默认值，字段恒存在）。"""
    default = {
        "session_timeout_streak": 0,
        "session_failure_total": 0,
        "session_circuit_open": False,
        "session_circuit_until": "",
        "session_circuit_half_open": False,
        "session_last_error_code": "",
        "session_last_failure_at": "",
        "session_last_success_at": "",
        "llm_provider_ok": True,
        "llm_provider_reason": "",
    }
    rid = int(runtime_id or 0)
    if not rid:
        return default
    try:
        with _lock():
            row = _session_row(_db().cursor(), rid)
    except Exception:
        return default
    if not row:
        return default
    streak, total, until, half, code, fat, sat, llm_ok, lpreason = row
    return {
        "session_timeout_streak": int(streak or 0),
        "session_failure_total": int(total or 0),
        "session_circuit_open": _circuit_open_now(until, streak, code),
        "session_circuit_until": until or "",
        "session_circuit_half_open": bool(int(half or 0)),
        "session_last_error_code": code or "",
        "session_last_failure_at": fat or "",
        "session_last_success_at": sat or "",
        "llm_provider_ok": bool(int(llm_ok if llm_ok is not None else 1)),
        "llm_provider_reason": lpreason or "",
    }


def _audit(c, runtime_id: int, name: str, kind: str, detail: dict) -> None:
    """生命周期审计（与 backend_health 同表；失败不影响主链路）。"""
    try:
        import json
        c.execute(
            "INSERT INTO grape_t_engine_lifecycle_audit "
            "(runtime_id, runtime_name, kind, scene_row_id, node_tag, detail, created_at) "
            "VALUES (?,?,?,0,'',?,?)",
            (runtime_id, name or "", kind, json.dumps(detail, ensure_ascii=False),
             datetime.now(timezone.utc).isoformat()))
    except Exception as e:
        log.debug(f"session_guard: audit insert failed (runtime={runtime_id}, kind={kind}): {e}")


def _write_failure(c, runtime_id: int, error_code: str, payload: dict | None = None) -> dict:
    """计数 + 冷却/熔断写入（单事务内由调用方提交）。"""
    row = _session_row(c, runtime_id)
    if not row:
        return {}
    name = (payload or {}).get("engine") or ""
    streak = int(row[0] or 0) + 1
    total = int(row[1] or 0) + 1
    now = datetime.now(timezone.utc)
    now_iso = now.isoformat()
    threshold = session_timeout_streak_threshold()
    if error_code == ENGINE_MISCONFIGURED:
        cooldown = misconfig_circuit_seconds()
        circuit = True
    elif streak >= threshold:
        cooldown = session_circuit_seconds()
        circuit = True
    else:
        cooldown = session_retry_cooldown_seconds()
        circuit = False
    until = (now + timedelta(seconds=cooldown)).isoformat()
    c.execute(
        "UPDATE agent_runtimes SET session_fail_streak=?, session_fail_total=?, "
        "session_circuit_until=?, session_circuit_half_open=0, "
        "session_last_error_code=?, session_last_failure_at=? WHERE id=?",
        (streak, total, until, error_code, now_iso, int(runtime_id)))
    if error_code == ENGINE_MISCONFIGURED:
        # 配置类错误：拒绝健康态并摘除调度资格（存量行不再进调度池）
        c.execute(
            "UPDATE agent_runtimes SET healthy=0, llm_provider_ok=0, llm_provider_reason=? "
            "WHERE id=?", (str((payload or {}).get("reason") or "")[:255], int(runtime_id)))
        log.error(
            f"ENGINE_MISCONFIGURED: runtime_id={runtime_id} engine={name or '-'} "
            f"reason={str((payload or {}).get('reason') or '')[:240]} "
            f"(healthy=0, llm_provider_ok=0, excluded from scheduling)")
    if circuit:
        log.warning(
            f"ENGINE_SESSION_CIRCUIT_OPEN: runtime_id={runtime_id} engine={name or '-'} "
            f"code={error_code} streak={streak} threshold={threshold} "
            f"cooldown={cooldown}s circuit_open_until={until} (excluded from dispatch)")
    else:
        log.warning(
            f"SESSION_RETRY_COOLDOWN: runtime_id={runtime_id} engine={name or '-'} "
            f"code={error_code} streak={streak}/{threshold} cooldown={cooldown}s "
            f"(requeue must pick another engine)")
    _audit(c, runtime_id, name,
           "session_circuit_open" if circuit else "session_retry_cooldown",
           {"error_code": error_code, "streak": streak, "cooldown_seconds": cooldown,
            "session_circuit_until": until,
            "queue_depth": (payload or {}).get("queue_depth"),
            "disk_avail_bytes": (payload or {}).get("disk_avail_bytes")})
    return {"runtime_id": int(runtime_id), "error_code": error_code, "streak": streak,
            "fail_total": total, "circuit_open": circuit,
            "cooldown_seconds": cooldown, "session_circuit_until": until}


def note_session_failure(runtime_id: int, error_code: str = SESSION_CREATE_BLOCKED,
                         payload: dict | None = None) -> dict:
    """登记一次 session 通道失败（计数 + 短冷却/长熔断）。"""
    rid = int(runtime_id or 0)
    if not rid:
        return {}
    with _lock():
        d = _db()
        c = d.cursor()
        try:
            out = _write_failure(c, rid, error_code, payload)
            d.commit()
        except Exception as e:
            log.warning(f"session_guard: note_session_failure failed (runtime={rid}): {e}")
            try:
                d.rollback()
            except Exception as _rbe:
                log.debug(f"session_guard: rollback failed (runtime={rid}): {_rbe}")
            return {}
    # 引擎名缺失时补一条可读日志（payload 由调用方给）
    if out and not (payload or {}).get("engine"):
        out["engine"] = _runtime_name(rid)
    return out


def note_session_misconfigured(runtime_id: int, payload: dict | None = None) -> dict:
    """配置类 500（无 LLM provider）→ 长熔断 + 摘除健康态。"""
    return note_session_failure(runtime_id, ENGINE_MISCONFIGURED, payload)


def note_session_success_cursor(c, runtime_id: int) -> None:
    """已有游标/事务内复位 session 熔断（任务真实执行证据回调）。"""
    rid = int(runtime_id or 0)
    if not rid:
        return
    row = _session_row(c, rid)
    if not row:
        return
    streak, _total, until, half, _code, _fat, _sat, _llm, _lpreason = row
    if not int(streak or 0) and not until and not int(half or 0):
        return  # 已是干净状态：不产生无意义写入
    now_iso = datetime.now(timezone.utc).isoformat()
    c.execute(
        "UPDATE agent_runtimes SET session_fail_streak=0, session_circuit_until='', "
        "session_circuit_half_open=0, session_last_success_at=? WHERE id=?",
        (now_iso, rid))
    if until:
        log.info(f"ENGINE_SESSION_CIRCUIT_CLOSED: runtime_id={rid} engine="
                 f"{_runtime_name(rid) or '-'} (session succeeded, circuit cleared)")
        _audit(c, rid, "", "session_recovered",
               {"prev_session_circuit_until": until, "trigger": "session_success"})


def note_session_success(runtime_id: int) -> None:
    rid = int(runtime_id or 0)
    if not rid:
        return
    try:
        with _lock():
            d = _db()
            c = d.cursor()
            note_session_success_cursor(c, rid)
            d.commit()
    except Exception as e:
        log.debug(f"session_guard: note_session_success failed (runtime={rid}): {e}")


def note_session_probe_started(c, runtime_id: int) -> None:
    """冷却结束后的首次认领 = 半开探测占位（同一 runtime 只允许一个在飞探测）。"""
    rid = int(runtime_id or 0)
    if not rid:
        return
    row = _session_row(c, rid)
    if not row:
        return
    _streak, _total, until, half, code, _fat, _sat, llm_ok, _lp = row
    if not until or int(half or 0) != 0 or int(llm_ok if llm_ok is not None else 1) == 0:
        return
    ts = _ts(until)
    if ts is not None and ts > datetime.now(timezone.utc).timestamp():
        return  # 冷却窗口内，不会认领到（防御性）
    c.execute("UPDATE agent_runtimes SET session_circuit_half_open=1 WHERE id=?", (rid,))
    log.info(f"ENGINE_SESSION_HALF_OPEN: runtime_id={rid} engine={_runtime_name(rid) or '-'} "
             f"prev_code={code or '-'} (single probe allowed)")


def mark_provider_state(c, runtime_id: int, ok: bool, reason: str = "",
                        provider: str = "") -> None:
    """注册/启用期落 provider 校验结果（`ok=False` → 摘除健康态与调度资格）。"""
    rid = int(runtime_id or 0)
    if not rid:
        return
    try:
        c.execute(
            "UPDATE agent_runtimes SET llm_provider_ok=?, llm_provider_reason=? WHERE id=?",
            (1 if ok else 0, str(reason or "")[:255], rid))
        if not ok:
            c.execute("UPDATE agent_runtimes SET healthy=0 WHERE id=?", (rid,))
            log.error(f"ENGINE_MISCONFIGURED: runtime_id={rid} engine={_runtime_name(rid) or '-'} "
                      f"provider={provider or '-'} reason={str(reason or '')[:240]} "
                      f"(registration rejected, healthy=0, excluded from scheduling)")
    except Exception as e:
        log.debug(f"session_guard: mark_provider_state failed (runtime={rid}): {e}")


# ===== 换引擎重试（fail_task 收口）=====

def _append_error(task_id: str, entry: dict) -> None:
    try:
        from src.domains.runtime.engine.transient_retry import _append_task_error
        _append_task_error(task_id, entry)
    except Exception as e:
        log.debug(f"session_guard: append task error failed (task={task_id}): {e}")


def _merge_event_payload(event_seq, payload: dict) -> None:
    """事件 payload 合并（独立命名空间 `_session_fail`，不改既有字段语义）。"""
    try:
        seq = int(event_seq or 0)
        if not seq:
            return
        from src.domains.runtime.event_store import merge_event_payload
        merge_event_payload(seq, {"_session_fail": payload})
    except Exception as e:
        log.debug(f"session_guard: merge event payload failed (seq={event_seq}): {e}")


def has_other_session_engine(task: dict, exclude_runtime_id: int) -> bool:
    """池内是否还有可用的**其它**引擎（排除熔断/配置错误/离线实例）。

    查询失败 → True（fail-open：宁可 requeue 交给既有回收链，也不误判「无引擎」）。
    """
    pool_tag = (task.get("pool_tag") or task.get("agent_type") or "").strip()
    if not pool_tag:
        return True
    try:
        with _lock():
            rows = _db().cursor().execute(
                "SELECT ar.id, COALESCE(ar.healthy,0), COALESCE(ar.status,''), "
                "COALESCE(ar.credential_id,0), COALESCE(ar.session_circuit_until,''), "
                "COALESCE(ar.session_circuit_half_open,0), COALESCE(ar.llm_provider_ok,1) "
                "FROM agent_runtimes ar WHERE ar.id<>? AND EXISTS ("
                "SELECT 1 FROM engine_capabilities ec WHERE ec.runtime_id=ar.id "
                "AND ec.enabled=1 AND (ec.tag=? OR ec.tag='all'))",
                (int(exclude_runtime_id or 0), pool_tag)).fetchall()
    except Exception as e:
        log.debug(f"session_guard: other-engine probe failed (fail-open): {e}")
        return True
    now = datetime.now(timezone.utc).timestamp()
    for rid, healthy, status, cred_id, until, half, llm_ok in rows or []:
        if int(healthy or 0) != 1:
            continue
        if int(cred_id or 0) > 0 and (status or "") != "online":
            continue
        if not session_eligible(until or "", half, llm_ok, now):
            continue
        return True
    return False


def handle_task_failure(task_id: str, task: dict | None, error, exit_code=-1) -> str:
    """`fail_task` 收口：session 通道失败 → 留痕 + 换引擎重试 / 显式判死。

    返回：
    - ``not_session``：非 session 失败（调用方继续既有链路）；
    - ``observed``：已记录观测（不可重试的 `ENGINE_UNREACHABLE`，交既有后端/瞬时链路）；
    - ``requeued`` / ``already_requeued``：已换引擎重试（调用方跳过判死）；
    - ``exhausted``：重试耗尽或无其它可用引擎（调用方按 failed 判死并回落下一跳）。
    """
    if not task:
        return "not_session"
    cls = classify_session_failure(error)
    if not cls:
        return "not_session"
    rid = int(task.get("runtime_id") or 0)
    payload = failure_payload(rid, _runtime_name(rid) if rid else "", cls)
    code = payload["error_code"]

    if rid:
        if code == ENGINE_MISCONFIGURED:
            note_session_misconfigured(rid, payload)
        elif code == SESSION_CREATE_BLOCKED:
            note_session_failure(rid, code, payload)
    _append_error(task_id, {
        "error_code": code,
        "engine_id": payload["engine_id"],
        "queue_depth": payload["queue_depth"],
        "disk_avail_bytes": payload["disk_avail_bytes"],
        "engine": payload["engine"],
        "stage": payload["stage"],
        "reason": payload["reason"][:240],
        "note": "session channel failure classified" if code != SESSION_CREATE_BLOCKED
                else "session/new blocked (engine reachable, no response in budget)",
    })
    _merge_event_payload((task or {}).get("event_seq"), payload)

    if code == ENGINE_UNREACHABLE or not rid:
        return "observed"  # 连接期失败交既有后端不可用 / 瞬时重试链路

    try:
        with _lock():
            row = _db().cursor().execute(
                "SELECT status, attempts FROM agent_tasks WHERE id=?", (task_id,)).fetchone()
    except Exception as e:
        log.warning(f"session_guard: load task {task_id} failed: {e}")
        return "observed"
    if not row:
        return "observed"
    status, attempts = row[0], int(row[1] or 0)
    if status == "queued":
        return "already_requeued"
    if status != "running":
        return "observed"

    max_retry = session_max_retry()
    if attempts >= max_retry:
        _append_error(task_id, {
            "error_code": f"{code}_EXHAUSTED",
            "attempts": attempts, "max": max_retry,
            "engine_id": payload["engine_id"], "engine": payload["engine"],
            "queue_depth": payload["queue_depth"],
            "disk_avail_bytes": payload["disk_avail_bytes"],
            "reason": payload["reason"][:240],
            "note": "session retry attempts exhausted; task will be failed",
        })
        log.warning(f"SESSION_RETRY_EXHAUSTED: task_id={task_id} code={code} "
                    f"engine_id={rid} attempts={attempts}/{max_retry}")
        return "exhausted"

    if not has_other_session_engine(task, rid):
        _append_error(task_id, {
            "error_code": "NO_SESSION_ENGINE_AVAILABLE",
            "engine_id": payload["engine_id"], "engine": payload["engine"],
            "queue_depth": payload["queue_depth"],
            "disk_avail_bytes": payload["disk_avail_bytes"],
            "reason": payload["reason"][:240],
            "note": "no other healthy engine in pool; failing instead of hanging",
        })
        log.warning(f"SESSION_RETRY_NO_ENGINE: task_id={task_id} code={code} "
                    f"excluded_runtime={rid} pool_tag={task.get('pool_tag') or ''}")
        return "exhausted"

    from src.domains.runtime.engine.transient_retry import _do_requeue
    _do_requeue(task_id)
    try:
        with _lock():
            row = _db().cursor().execute(
                "SELECT status FROM agent_tasks WHERE id=?", (task_id,)).fetchone()
        if not row or row[0] != "queued":
            log.warning(f"session_guard: requeue task {task_id} did not reach queued "
                        f"(status={row[0] if row else 'vanished'})")
            return "observed"
    except Exception as _vq:
        log.debug(f"session_guard: requeue status verify failed (task={task_id}): {_vq}")
    _append_error(task_id, {
        "error_code": "SESSION_RETRY",
        "failed_error_code": code,
        "attempt": attempts + 1, "max": max_retry,
        "engine_id": payload["engine_id"], "engine": payload["engine"],
        "queue_depth": payload["queue_depth"],
        "disk_avail_bytes": payload["disk_avail_bytes"],
        "reason": payload["reason"][:240],
        "note": "requeued to another healthy engine (same engine session-cooled)",
    })
    log.info(f"SESSION_RETRY_REQUEUE: task_id={task_id} code={code} "
             f"excluded_runtime={rid} attempt={attempts + 1}/{max_retry}")
    return "requeued"