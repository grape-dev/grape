"""仓库级 VCS 永久失败熔断（Issue #3759）。

现象：`huaweicloud-mate/repository-requests` 在上游 GitCode 已被删除（`Project
not found`），但平台台账里仍留有仓库卡与 Issue 62/63。巡检每轮对这两条 issue
各打一次上游 GET，404 被当作可重试错误：一小时 76 条 WARNING，永不收敛，
既烧上游配额又淹没同仓库真实告警。

本模块给出仓库级（不是 issue 级）熔断：

1. **永久失败判定**：4xx 除 429 视为不可自愈（401/403 等同理，覆盖权限/归属
   错配；429 是限流，属可重试）；
2. **仓库级存在性探测**：首次永久失败时探测一次 `/repos/{owner}/{repo}`，
   得出 missing/exists/unknown 并落仓库卡台账——这样「仓库不存在」这一事实
   只需一次上游请求，不随 issue 数线性增长；
   ⚠️ **只有 404/410 + 平台仓储凭据**才能构成仓库级事实（`CREDENTIAL_REPO`）。
   400/401/403/422 与任何来自调用方凭据的失败只走 `note_caller_error`（零写库）：
   私有仓/机器人失可见性时上游 `/repos` 同样返 404，用调用方凭据落台账会把健康
   仓库判死、并让用户态 GET 吃到合成 404；
3. **退避放行**：探测结论 missing → `vcs_missing`，熔断期间该仓库的逐 issue 读
   直接短路（0 上游请求），只在 `vcs_next_probe_at` 到点后由 `allow_read` **原子
   占位**放行一次（8 并发批量复核不会整批打上游）；退避档位 5min → 15min → 60min
   封顶（上限依据验收项「≤1 次/小时」）；探测结论 unknown **不解除**已确认的熔断；
4. **自动恢复**：探测结论 exists、或熔断外的正常读成功 → 清除 `vcs_missing`，
   订阅读路径自动恢复；
5. **运维入口**：`reset()` 一键清除（重绑 provider / 人工处置后调用）。

事实源 = 仓库卡（`managed_repos` 归 mgmt 域，经 `mgmt.public` 读写，跨域写走
`cross_write` 幂等意图）；进程内只保留 30s TTL 短缓存。读不到台账时**一律
fail-open**（不短路）——宁可多打一次上游，也不能把健康仓库当孤儿熔断。
"""
from __future__ import annotations

import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from src.kernel.config import log
from src.kernel.repo_key import to_repo_key

VCS_STATUS_OK = "ok"
VCS_STATUS_MISSING = "vcs_missing"

#: 退避档位（秒）：5min → 15min → 60min 封顶。封顶 1h 即验收项「≤1 次/小时」。
PERMANENT_BACKOFF_SECONDS = (300, 900, 3600)

#: 只有「资源在给定 URL 上不存在」的 404/410 才可能构成**仓库级**事实。400/401/403/422
#: 是「这一次请求/这一方凭据」的问题（非法编号、token 过期、无权限、参数不合法），
#: 既不能证明仓库已删，也不能代表其它调用方——QA 复审 Required 1 的口径。
REPO_ABSENCE_STATUSES = frozenset({404, 410})

#: 台账写入的凭据主体：
#: - ``CREDENTIAL_REPO``：平台仓储读 token（巡检/订阅读路径），探测与读用同一凭据，
#:   「读不到 issue」与「看不到仓库」同源，结论对这条链路可信；
#: - ``CREDENTIAL_CALLER``：调用方自带凭据（用户态 API）。它对仓库的可见性与平台凭据
#:   无关（私有仓/机器人失可见性时上游 /repos 同样返 404），**不得写仓库级台账**。
CREDENTIAL_REPO = "repo"
CREDENTIAL_CALLER = "caller"

#: 探测上游仓库的单请求超时（探测失败按 unknown fail-open，不误熔断）。
PROBE_TIMEOUT_SECONDS = 5

#: 进程内台账缓存 TTL：多副本一致性以 DB 为准，缓存只防「同一轮每个 issue 各查一次库」。
STATE_CACHE_TTL_SECONDS = 30.0

#: 同一仓库「永久失败记账」的最小间隔（秒）。批量复核是 8 并发 × N 条 issue，
#: 若每条 404 都读一次库再写一次台账，孤儿仓库卡会把 DB 打成热点。间隔内的重复
#: 失败只返回当前状态（不写），首次失败/到点探测不受影响。
#: 用注入的 now（逻辑时间）判定，测试可精确构造。
NOTE_MIN_INTERVAL_SECONDS = 30.0

#: 放行一次探测后的**租约**：占位期间同一仓库的其它并发读一律继续短路（0 上游）。
#: 8 并发批量复核在同一个 due 时点只允许 1 个线程走出熔断；租约短于最小退避档位
#: （300s），到期仍未落新状态时允许下一个线程再放行一次。
PROBE_LEASE_SECONDS = 60.0

_STATE_CACHE: dict[str, tuple[float, dict]] = {}
_LAST_NOTE_AT: dict[str, datetime] = {}
#: repo_key -> 探测租约到期时刻（time.monotonic）
_PROBE_LEASES: dict[str, float] = {}
_CACHE_LOCK = threading.Lock()


def clear_cache() -> None:
    """清空进程内台账缓存（测试 / 运维手动刷新用）。"""
    with _CACHE_LOCK:
        _STATE_CACHE.clear()
        _LAST_NOTE_AT.clear()
        _PROBE_LEASES.clear()


def is_permanent_status(status: Any) -> bool:
    """上游状态码是否属「永久失败」：4xx 除 429（限流可重试）。

    仅用于「日志分类 / 不再按可重试重试」的判定；**不等于可以写仓库级台账**——
    写台账的门槛见 :func:`is_repo_absence_status`。
    """
    try:
        code = int(status)
    except (TypeError, ValueError):
        return False
    return 400 <= code < 500 and code != 429


def is_repo_absence_status(status: Any) -> bool:
    """状态码是否足以作为「该仓库在当前凭据下不存在」的证据（仅 404/410）。"""
    try:
        code = int(status)
    except (TypeError, ValueError):
        return False
    return code in REPO_ABSENCE_STATUSES


def backoff_seconds(fail_count: int) -> int:
    """按连续失败次数取退避档位（超出档位表即封顶）。"""
    try:
        n = int(fail_count)
    except (TypeError, ValueError):
        n = 1
    if n < 1:
        n = 1
    idx = min(n - 1, len(PERMANENT_BACKOFF_SECONDS) - 1)
    return PERMANENT_BACKOFF_SECONDS[idx]


def _now(now: Optional[datetime] = None) -> datetime:
    return now or datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat()


def _parse_iso(raw: Any) -> Optional[datetime]:
    text = str(raw or "").strip()
    if not text:
        return None
    try:
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


# ---------------------------------------------------------------- 台账读写

def _mgmt_lib():
    """mgmt 域 public lib 句柄：优先走跨域 lib（独立连接），否则用进程内实现。"""
    try:
        from src.kernel.domain_read import mgmt_lib
        lib = mgmt_lib(foreign_only=False)
        if lib is not None:
            return lib
    except Exception:  # noqa: BLE001 - lib 只是加速通道，不可用即回落
        pass
    try:
        from src.domains import mgmt as _mgmt_pkg  # noqa: F401
        from src.domains.mgmt import public as _mgmt_public
        return _mgmt_public
    except Exception:  # noqa: BLE001
        return None


def _read_state_db(repo_key: str) -> Optional[dict]:
    lib = _mgmt_lib()
    if lib is None:
        return None
    try:
        row = lib.get_repo_vcs_health(repo_key)
        return dict(row or {})
    except Exception as exc:  # noqa: BLE001
        log.debug(f"issue_poll_breaker: read state failed {repo_key}: {exc}")
        return None


def _write_state_db(repo_key: str, fields: dict) -> bool:
    """写台账：跨域写走 cross_write 幂等意图，失败回落直接写（lib 在即同源）。"""
    if not fields:
        return True
    lib = _mgmt_lib()
    if lib is None:
        return False

    def _apply() -> int:
        return int(lib.set_repo_vcs_health(repo_key, dict(fields)) or 0)

    try:
        from src.kernel import cross_write
        if cross_write.submit_if_lib(lib, "mgmt", "repo_vcs_health_update",
                                     f"repo-vcs-health:{repo_key}",
                                     {"repo_key": repo_key, "fields": dict(fields)},
                                     _apply, log=log):
            return True
    except Exception as exc:  # noqa: BLE001
        log.warning(f"issue_poll_breaker: cross write failed, fallback to lib ({repo_key}): {exc}")
    try:
        _apply()
        return True
    except Exception as exc:  # noqa: BLE001
        log.warning(f"issue_poll_breaker: persist state failed {repo_key}: {exc}")
        return False


def state(repo_key: str, *, fresh: bool = False) -> dict:
    """读仓库台账（30s 进程缓存）；读不到返回空 dict（调用方 fail-open）。"""
    rk = to_repo_key(repo_key)
    if not rk:
        return {}
    _now_mono = time.monotonic()
    if not fresh:
        with _CACHE_LOCK:
            entry = _STATE_CACHE.get(rk)
        if entry and _now_mono - entry[0] < STATE_CACHE_TTL_SECONDS:
            return dict(entry[1])
    row = _read_state_db(rk)
    if row is None:
        return {}
    with _CACHE_LOCK:
        _STATE_CACHE[rk] = (_now_mono, dict(row))
    return dict(row)


def _update_state(repo_key: str, fields: dict) -> dict:
    """合并写台账并刷新缓存，返回合并后的状态（写失败也返回内存视图）。"""
    rk = to_repo_key(repo_key)
    cur = state(rk, fresh=True)
    merged = dict(cur)
    merged.update({k: v for k, v in fields.items()})
    ok = _write_state_db(rk, fields)
    with _CACHE_LOCK:
        _STATE_CACHE[rk] = (time.monotonic(), dict(merged))
    if not ok:
        log.debug(f"issue_poll_breaker: state not persisted, process-local only ({rk})")
    return merged


# ---------------------------------------------------------------- 探测

def probe_repo(repo_key: str, *, client=None) -> str:
    """仓库级存在性探测：exists / missing / unknown。

    - missing：上游明确 404（仓库被删/改名/迁移，或 provider 归属错配）；
    - exists：仓库可读；
    - unknown：无 token / 超时 / 5xx / 401·403（权限可能只是局部失效，不据此熔断）。
    """
    rk = to_repo_key(repo_key)
    if not rk:
        return "unknown"
    try:
        if client is None:
            from src.domains.runtime.pipeline.event_pipeline import (
                _build_issue_read_client, _resolve_issue_read_token)
            token = _resolve_issue_read_token(rk)
            if not token:
                return "unknown"
            client = _build_issue_read_client(rk, token)
        info = client.get_repo_info(timeout=PROBE_TIMEOUT_SECONDS)
    except Exception as exc:  # noqa: BLE001
        log.debug(f"issue_poll_breaker: probe failed {rk}: {exc}")
        return "unknown"
    if not isinstance(info, dict):
        return "unknown"
    err = str(info.get("error") or "").strip()
    status = info.get("status_code")
    if str(status) == "404":
        return "missing"
    if err:
        # 401/403 可能是 token 局部失效或权限错配——探测不确定就不熔断（fail-open）。
        return "unknown"
    return "exists"


def _probe_due(st: dict, now: datetime) -> bool:
    """是否允许再探测一次（首次无探测记录 → 是；否则按退避档位判断）。"""
    if st.get("vcs_status") == VCS_STATUS_MISSING:
        due_at = _parse_iso(st.get("vcs_next_probe_at"))
        return due_at is None or now >= due_at
    last_probe = _parse_iso(st.get("vcs_probe_at"))
    if last_probe is None:
        return True
    return (now - last_probe).total_seconds() >= backoff_seconds(st.get("vcs_fail_count") or 1)


# ---------------------------------------------------------------- 对外判定

def blocked(repo_key: str, *, now: Optional[datetime] = None) -> tuple[bool, dict]:
    """只读判定：该仓库当前是否处于熔断（不占位、不放行）。

    熔断生效 = 台账 `vcs_status == vcs_missing` 且未到 `vcs_next_probe_at`。
    台账读不到（含无卡/DB 不可用）→ 一律不熔断（fail-open）。
    仅供「不该放行探测」的旁路查询（如批量复核前判断是否可信）；真正要走一次
    上游读的调用方必须用 :func:`allow_read`（到点会原子占位，只放行一次）。
    """
    st = state(repo_key)
    if not st or st.get("vcs_status") != VCS_STATUS_MISSING:
        return False, st
    due_at = _parse_iso(st.get("vcs_next_probe_at"))
    if due_at is None:
        return False, st
    if _now(now) >= due_at:
        return False, st
    return True, st


def allow_read(repo_key: str, *, now: Optional[datetime] = None) -> tuple[bool, dict]:
    """该仓库的逐 issue 读是否放行上游：返回 (是否放行, 台账状态)。

    - 未熔断 / 台账读不到 → ``(True, st)``（fail-open，正常打上游）；
    - 熔断中且未到点 → ``(False, st)``（短路，0 上游）；
    - 熔断且到点 → **原子占位**后 ``(True, st)``：进程内按 repo 单飞 + 把
      `vcs_next_probe_at` 前移一个租约，8 并发批量复核在同一 due 时点只有 1 个
      线程走出熔断，其余继续短路（QA 复审 Required 2-①）。
    """
    st = state(repo_key)
    if not st or st.get("vcs_status") != VCS_STATUS_MISSING:
        return True, st
    due_at = _parse_iso(st.get("vcs_next_probe_at"))
    if due_at is None:
        return True, st
    _now_dt = _now(now)
    if _now_dt < due_at:
        return False, st
    rk = to_repo_key(repo_key)
    lease_until = _now_dt + timedelta(seconds=PROBE_LEASE_SECONDS)
    with _CACHE_LOCK:
        # 单次持锁完成「判断 + 置位」：并发线程里只有一个能看到未占位状态。
        existing = _PROBE_LEASES.get(rk)
        if existing is not None and time.monotonic() < existing:
            return False, st
        _PROBE_LEASES[rk] = time.monotonic() + PROBE_LEASE_SECONDS
    # 占位同时前移台账时间：其它进程/其它副本从 DB 也看到「已放行一次」。
    try:
        _update_state(rk, {"vcs_next_probe_at": _iso(lease_until)})
    except Exception as exc:  # noqa: BLE001 - 占位写失败只退回进程内单飞
        log.debug(f"issue_poll_breaker: probe lease not persisted {rk}: {exc}")
    log.info(f"issue_poll_breaker: repo {rk} released for one probe attempt "
             f"(previous next_probe_at={_iso(due_at)})")
    st = dict(st)
    st.update({"vcs_next_probe_at": _iso(lease_until)})
    return True, st


def _throttle_note(rk: str, now_dt: datetime) -> bool:
    """同仓库「永久失败记账」节流：返回 True 表示本次应跳过。

    「判断 + 置位」在**同一次持锁**内完成（QA 复审 Optional：原实现分两次取锁，
    并发首次失败会各探测一次）。
    """
    with _CACHE_LOCK:
        _last = _LAST_NOTE_AT.get(rk)
        if _last is not None and (now_dt - _last).total_seconds() < NOTE_MIN_INTERVAL_SECONDS:
            return True
        _LAST_NOTE_AT[rk] = now_dt
        return False


def note_caller_error(repo_key: str, status_code: Any, *,
                      operation: str = "", now: Optional[datetime] = None) -> dict:
    """记录一次**调用方层面**的永久失败（凭据/请求错误）：只留日志，不碰台账。

    QA 复审 Required 1 的解耦落点：400/401/403/422 以及任何来自调用方凭据的失败，
    只说明「这一方凭据/这一次请求」有问题，既不能证明仓库已删，也不代表其它调用方。
    因此这里**零写库**（仓库卡是仓库级事实源，不能挂上某个人过期 token 的错误码），
    只返回当前台账状态供调用方继续走状态码透传；告警仍由日志层的窗口聚合给出。
    """
    rk = to_repo_key(repo_key)
    if not rk or not is_permanent_status(status_code):
        return state(rk)
    log.info(f"issue_poll_breaker: caller-scoped permanent failure {rk} "
             f"(status={status_code} on {operation or 'read'}); "
             f"repo ledger untouched (credential/request error, not repo absence)")
    return state(rk)


def note_permanent_failure(repo_key: str, status_code: Any, *,
                           operation: str = "", now: Optional[datetime] = None,
                           credential: str = CREDENTIAL_REPO) -> dict:
    """记录一次永久失败：累积计数 → 必要时做一次仓库级探测 → 落熔断状态。

    探测只在「首次失败」或「退避到点」时发生，因此首次 404 只多一次上游请求，
    后续同仓库 issue 的读在熔断期内 0 上游请求。

    写仓库级台账需要两个前提（QA 复审 Required 1）：
    1. `status_code` 属 :data:`REPO_ABSENCE_STATUSES`（404/410）；
    2. `credential == CREDENTIAL_REPO`（探测与读用同一平台凭据）。
    任一不满足 → 退化为 :func:`note_caller_error`（只记错误码）。
    """
    rk = to_repo_key(repo_key)
    if not rk or not is_permanent_status(status_code):
        return state(rk)
    if credential != CREDENTIAL_REPO or not is_repo_absence_status(status_code):
        return note_caller_error(rk, status_code, operation=operation, now=now)
    _now_dt = _now(now)
    if _throttle_note(rk, _now_dt):
        # 同仓库短时间内的重复失败：不重复读库/写台账（告警量也不放大）。
        return state(rk)
    st = state(rk, fresh=True)
    fail_count = int(st.get("vcs_fail_count") or 0) + 1
    fields: dict[str, Any] = {
        "vcs_last_error_code": str(status_code),
        "vcs_last_error_at": _iso(_now_dt),
        "vcs_fail_count": fail_count,
    }
    if _probe_due(st, _now_dt):
        result = probe_repo(rk)
        fields["vcs_probe_result"] = result
        fields["vcs_probe_at"] = _iso(_now_dt)
        if result == "missing":
            fields["vcs_status"] = VCS_STATUS_MISSING
            fields["vcs_missing_since"] = st.get("vcs_missing_since") or _iso(_now_dt)
            fields["vcs_next_probe_at"] = _iso(
                _now_dt + timedelta(seconds=backoff_seconds(fail_count)))
            log.warning(
                f"issue_poll_breaker: repo {rk} marked {VCS_STATUS_MISSING} "
                f"(upstream {status_code} on {operation or 'issue read'}, "
                f"probe=missing, fail_count={fail_count}, "
                f"next_probe={fields['vcs_next_probe_at']}); per-issue VCS reads "
                f"are short-circuited until then")
        elif result == "exists":
            # 仓库确实存在 → 只是这条 issue 不在上游：不熔断仓库，并清除既有熔断。
            fields["vcs_status"] = VCS_STATUS_OK
            fields["vcs_missing_since"] = ""
            fields["vcs_next_probe_at"] = ""
        else:
            # 探测不确定（无 token/超时/401/403）：**不得**据此解除已确认的熔断
            # （QA 复审 Required 2-②：一次 5s 超时就把已确认的 vcs_missing 洗白）。
            # 未熔断时保持 ok；已熔断时保持 vcs_missing 并把探测时间再推迟一档。
            if st.get("vcs_status") == VCS_STATUS_MISSING:
                fields["vcs_status"] = VCS_STATUS_MISSING
                fields["vcs_missing_since"] = st.get("vcs_missing_since") or _iso(_now_dt)
                fields["vcs_next_probe_at"] = _iso(
                    _now_dt + timedelta(seconds=backoff_seconds(fail_count)))
                log.info(f"issue_poll_breaker: repo {rk} probe=unknown during "
                         f"{VCS_STATUS_MISSING}, fuse kept, next_probe="
                         f"{fields['vcs_next_probe_at']}")
            else:
                fields["vcs_status"] = VCS_STATUS_OK
                fields["vcs_next_probe_at"] = ""
    return _update_state(rk, fields)


def _state_is_dirty(st: dict) -> bool:
    """台账是否有「需要清除」的痕迹（熔断中 / 失败计数 / 错误码 / 探测点）。"""
    return bool(st.get("vcs_status") == VCS_STATUS_MISSING
                or int(st.get("vcs_fail_count") or 0)
                or st.get("vcs_last_error_code")
                or st.get("vcs_next_probe_at"))


def note_recovery(repo_key: str, *, operation: str = "",
                  now: Optional[datetime] = None) -> bool:
    """上游读成功 → 清除熔断状态（自动恢复）；返回是否真的清除了状态。

    读路径是批量的（一次列表要复核几十上百条 issue），因此**先用 30s 缓存判定
    环境卫生**：缓存显示无脏状态 → 直接返回，不查库（QA 复审 Optional：原实现
    每次成功读都 `fresh=True` 查一次库，97 条 ≈ 97 次 SELECT）。缓存脏时才做一次
    fresh 复查（防其它副本/其它进程已恢复），确认仍脏才写库。
    """
    rk = to_repo_key(repo_key)
    if not rk:
        return False
    st = state(rk)
    if not st or not _state_is_dirty(st):
        return False
    st = state(rk, fresh=True)
    if not st or not _state_is_dirty(st):
        return False
    was_missing = st.get("vcs_status") == VCS_STATUS_MISSING
    _update_state(rk, {
        "vcs_status": VCS_STATUS_OK,
        "vcs_missing_since": "",
        "vcs_last_error_code": "",
        "vcs_next_probe_at": "",
        "vcs_fail_count": 0,
    })
    with _CACHE_LOCK:
        _PROBE_LEASES.pop(rk, None)
    if was_missing:
        log.info(f"issue_poll_breaker: repo {rk} recovered (operation={operation or 'read'}), "
                 f"{VCS_STATUS_MISSING} cleared; subscription reads resumed")
    return was_missing


def drop_cache(repo_key: str) -> None:
    """只清本进程的台账缓存/租约/节流（不写库）。

    供 mgmt 侧直接写台账后（如运维 reset）让本进程立刻看到新值，避免 30s 缓存
    让「已清除」看起来没生效；写语义本身由调用方在归属域完成（不做跨域写）。
    """
    rk = to_repo_key(repo_key)
    if not rk:
        return
    with _CACHE_LOCK:
        _STATE_CACHE.pop(rk, None)
        _LAST_NOTE_AT.pop(rk, None)
        _PROBE_LEASES.pop(rk, None)


def reset(repo_key: str, *, reason: str = "ops_reset",
          now: Optional[datetime] = None) -> dict:
    """运维一键清除熔断台账（禁用/重绑 provider 后调用；幂等）。"""
    rk = to_repo_key(repo_key)
    if not rk:
        return {}
    with _CACHE_LOCK:
        _LAST_NOTE_AT.pop(rk, None)
        _PROBE_LEASES.pop(rk, None)
    _update_state(rk, {
        "vcs_status": VCS_STATUS_OK,
        "vcs_missing_since": "",
        "vcs_last_error_code": "",
        "vcs_last_error_at": "",
        "vcs_probe_result": "",
        "vcs_probe_at": "",
        "vcs_next_probe_at": "",
        "vcs_fail_count": 0,
    })
    log.info(f"issue_poll_breaker: repo {rk} breaker reset (reason={reason})")
    return state(rk, fresh=True)