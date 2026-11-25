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
3. **退避放行**：探测结论 missing → `vcs_missing`，熔断期间该仓库的逐 issue 读
   直接短路（0 上游请求），只在 `vcs_next_probe_at` 到点后放行一次探测；
   退避档位 5min → 15min → 60min 封顶（上限依据验收项「≤1 次/小时」）；
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

#: 探测上游仓库的单请求超时（探测失败按 unknown fail-open，不误熔断）。
PROBE_TIMEOUT_SECONDS = 5

#: 进程内台账缓存 TTL：多副本一致性以 DB 为准，缓存只防「同一轮每个 issue 各查一次库」。
STATE_CACHE_TTL_SECONDS = 30.0

#: 同一仓库「永久失败记账」的最小间隔（秒）。批量复核是 8 并发 × N 条 issue，
#: 若每条 404 都读一次库再写一次台账，孤儿仓库卡会把 DB 打成热点。间隔内的重复
#: 失败只返回当前状态（不写），首次失败/到点探测不受影响。
#: 用注入的 now（逻辑时间）判定，测试可精确构造。
NOTE_MIN_INTERVAL_SECONDS = 30.0

_STATE_CACHE: dict[str, tuple[float, dict]] = {}
_LAST_NOTE_AT: dict[str, datetime] = {}
_CACHE_LOCK = threading.Lock()


def clear_cache() -> None:
    """清空进程内台账缓存（测试 / 运维手动刷新用）。"""
    with _CACHE_LOCK:
        _STATE_CACHE.clear()
        _LAST_NOTE_AT.clear()


def is_permanent_status(status: Any) -> bool:
    """上游状态码是否属「永久失败」：4xx 除 429（限流可重试）。"""
    try:
        code = int(status)
    except (TypeError, ValueError):
        return False
    return 400 <= code < 500 and code != 429


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
    """该仓库的逐 issue 读是否应短路：返回 (是否熔断生效, 台账状态)。

    熔断生效 = 台账 `vcs_status == vcs_missing` 且未到 `vcs_next_probe_at`。
    到点放行一次探测（放行后本次读会真的打上游，探测/读成功即自愈）。
    台账读不到（含无卡/DB 不可用）→ 一律不熔断（fail-open）。
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


def note_permanent_failure(repo_key: str, status_code: Any, *,
                           operation: str = "", now: Optional[datetime] = None) -> dict:
    """记录一次永久失败：累积计数 → 必要时做一次仓库级探测 → 落熔断状态。

    探测只在「首次失败」或「退避到点」时发生，因此首次 404 只多一次上游请求，
    后续同仓库 issue 的读在熔断期内 0 上游请求。
    """
    rk = to_repo_key(repo_key)
    if not rk or not is_permanent_status(status_code):
        return state(rk)
    _now_dt = _now(now)
    with _CACHE_LOCK:
        _last = _LAST_NOTE_AT.get(rk)
    if _last is not None and (_now_dt - _last).total_seconds() < NOTE_MIN_INTERVAL_SECONDS:
        # 同仓库短时间内的重复失败：不重复读库/写台账（告警量也不放大）。
        return state(rk)
    with _CACHE_LOCK:
        _LAST_NOTE_AT[rk] = _now_dt
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
        else:
            # 仓库确实存在 → 只是这条 issue 不在上游（或探测不确定）：不熔断仓库。
            fields["vcs_status"] = VCS_STATUS_OK
            fields["vcs_missing_since"] = ""
            fields["vcs_next_probe_at"] = ""
    return _update_state(rk, fields)


def note_recovery(repo_key: str, *, operation: str = "",
                  now: Optional[datetime] = None) -> bool:
    """上游读成功 → 清除熔断状态（自动恢复）；返回是否真的清除了状态。

    只在「确有状态要清」时才写库（熔断中 / 有失败计数 / 有错误码）：否则健康仓库
    每次读都会产生一次跨域写。已 ok 且无失败痕迹 → 直接返回 False（零写）。
    """
    rk = to_repo_key(repo_key)
    if not rk:
        return False
    st = state(rk, fresh=True)
    if not st:
        return False
    was_missing = st.get("vcs_status") == VCS_STATUS_MISSING
    dirty = bool(was_missing or int(st.get("vcs_fail_count") or 0)
                 or st.get("vcs_last_error_code")
                 or st.get("vcs_next_probe_at"))
    if not dirty:
        return False
    _update_state(rk, {
        "vcs_status": VCS_STATUS_OK,
        "vcs_missing_since": "",
        "vcs_last_error_code": "",
        "vcs_next_probe_at": "",
        "vcs_fail_count": 0,
    })
    if was_missing:
        log.info(f"issue_poll_breaker: repo {rk} recovered (operation={operation or 'read'}), "
                 f"{VCS_STATUS_MISSING} cleared; subscription reads resumed")
    return was_missing


def reset(repo_key: str, *, reason: str = "ops_reset",
          now: Optional[datetime] = None) -> dict:
    """运维一键清除熔断台账（禁用/重绑 provider 后调用；幂等）。"""
    rk = to_repo_key(repo_key)
    if not rk:
        return {}
    with _CACHE_LOCK:
        _LAST_NOTE_AT.pop(rk, None)
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