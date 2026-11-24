"""hermes ACP state.db 体积治理（Issue #3773）。

超阈值时就地清理：完整性校验 → 备份 → 按保留窗口裁剪旧 session/message →
VACUUM → 再次校验。清理后仍超阈值时归档重建空库，保留可回滚备份。

默认参数（环境变量可覆盖）：
    ACP_STATE_DB_MAX_MB=50
    ACP_STATE_DB_LOW_WATER_MB=40
    ACP_STATE_DB_RETENTION_DAYS=7
    ACP_STATE_DB_MIN_SESSIONS=10
    ACP_STATE_DB_CLEANUP_MIN_INTERVAL_S=3600
    ACP_STATE_DB_FORCE_MIN_IDLE_DAYS=1
    ACP_STATE_DB_BACKUP_DIR=~/.cache/hermes-acp-backups
    ACP_STATE_DB_BACKUP_RETENTION_H=24

共享 HERMES_HOME（单文件 state.db）与 per-issue 隔离（HERMES_HOME/*/state.db）
两种模式共用同一实现；调用方通过 discover_state_dbs() 决定清理范围。
"""
from __future__ import annotations

import json
import logging
import os
import shutil
import sqlite3
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Optional

log = logging.getLogger("devops-bot")

MAX_MB_ENV = "ACP_STATE_DB_MAX_MB"
LOW_WATER_MB_ENV = "ACP_STATE_DB_LOW_WATER_MB"
RETENTION_DAYS_ENV = "ACP_STATE_DB_RETENTION_DAYS"
MIN_SESSIONS_ENV = "ACP_STATE_DB_MIN_SESSIONS"
MIN_INTERVAL_ENV = "ACP_STATE_DB_CLEANUP_MIN_INTERVAL_S"
BACKUP_DIR_ENV = "ACP_STATE_DB_BACKUP_DIR"
BACKUP_RETENTION_ENV = "ACP_STATE_DB_BACKUP_RETENTION_H"
FORCE_MIN_IDLE_DAYS_ENV = "ACP_STATE_DB_FORCE_MIN_IDLE_DAYS"

DEFAULT_MAX_MB = 50.0
DEFAULT_LOW_WATER_MB = 40.0
DEFAULT_RETENTION_DAYS = 7.0
DEFAULT_MIN_SESSIONS = 10
DEFAULT_MIN_INTERVAL_S = 3600.0
DEFAULT_BACKUP_RETENTION_H = 24.0
DEFAULT_FORCE_MIN_IDLE_DAYS = 1.0

_RESULT_OK = "ok"
_RESULT_SKIPPED = "skipped"
_RESULT_FAILED = "failed"
_RESULT_DRY_RUN = "dry_run"
_RESULT_ARCHIVED = "archived"
_RESULT_FORCED = "forced"

_session_tables = ("messages", "session_model_usage", "compression_locks")
_delegation_tables = {
    "async_delegations": ("origin_session", "parent_session_id", "origin_session_id"),
}

_metrics_lock = threading.Lock()
_cleanup_counts: dict[str, int] = {}
_bytes_freed_total = 0
_resume_skipped_total = 0

_thread_lock = threading.Lock()


def _env_float(name: str, default: float) -> float:
    try:
        raw = os.environ.get(name, "")
        if str(raw).strip() == "":
            return default
        return float(raw)
    except (TypeError, ValueError):
        return default


def _env_int(name: str, default: int) -> int:
    try:
        raw = os.environ.get(name, "")
        if str(raw).strip() == "":
            return default
        return int(float(raw))
    except (TypeError, ValueError):
        return default


def max_bytes_default() -> int:
    return int(_env_float(MAX_MB_ENV, DEFAULT_MAX_MB) * 1024 * 1024)


def low_water_bytes_default() -> int:
    return int(_env_float(LOW_WATER_MB_ENV, DEFAULT_LOW_WATER_MB) * 1024 * 1024)


def retention_days_default() -> float:
    return _env_float(RETENTION_DAYS_ENV, DEFAULT_RETENTION_DAYS)


def min_sessions_default() -> int:
    return max(0, _env_int(MIN_SESSIONS_ENV, DEFAULT_MIN_SESSIONS))


def min_interval_default() -> float:
    return _env_float(MIN_INTERVAL_ENV, DEFAULT_MIN_INTERVAL_S)


def force_min_idle_days_default() -> float:
    return _env_float(FORCE_MIN_IDLE_DAYS_ENV, DEFAULT_FORCE_MIN_IDLE_DAYS)


def backup_dir_default() -> str:
    configured = (os.environ.get(BACKUP_DIR_ENV) or "").strip()
    if configured:
        return os.path.expanduser(configured)
    return os.path.join(os.path.expanduser("~"), ".cache", "hermes-acp-backups")


def _backup_retention_hours() -> float:
    return _env_float(BACKUP_RETENTION_ENV, DEFAULT_BACKUP_RETENTION_H)


@dataclass
class MaintenanceResult:
    db_path: str = ""
    before_bytes: int = 0
    after_bytes: int = 0
    freed_bytes: int = 0
    pruned_sessions: int = 0
    pruned_messages: int = 0
    forced_pruned_sessions: int = 0
    result: str = _RESULT_SKIPPED
    reason: str = ""
    backup_path: str = ""
    integrity_before: str = ""
    integrity_after: str = ""
    archived: bool = False
    error: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "db_path": self.db_path,
            "before_bytes": self.before_bytes,
            "after_bytes": self.after_bytes,
            "freed_bytes": self.freed_bytes,
            "pruned_sessions": self.pruned_sessions,
            "pruned_messages": self.pruned_messages,
            "forced_pruned_sessions": self.forced_pruned_sessions,
            "result": self.result,
            "reason": self.reason,
            "backup_path": self.backup_path,
            "integrity_before": self.integrity_before,
            "integrity_after": self.integrity_after,
            "archived": self.archived,
            "error": self.error,
        }


@dataclass
class StateDbCheckResult:
    db_path: str = ""
    before_bytes: int = 0
    after_bytes: int = 0
    exceeded: bool = False
    skip_resume: bool = False
    reason: str = ""
    cleanup: Optional[MaintenanceResult] = None
    archive: Optional[MaintenanceResult] = None

    def __bool__(self) -> bool:
        return bool(self.skip_resume)

    def to_dict(self) -> dict[str, Any]:
        return {
            "db_path": self.db_path,
            "before_bytes": self.before_bytes,
            "after_bytes": self.after_bytes,
            "exceeded": self.exceeded,
            "skip_resume": self.skip_resume,
            "reason": self.reason,
            "cleanup": self.cleanup.to_dict() if self.cleanup else None,
            "archive": self.archive.to_dict() if self.archive else None,
        }


class _DbLock:
    """state.db 清理互斥锁：POSIX flock，跨进程；不可用时退化为进程内锁。"""

    def __init__(self, db_path: str, timeout: float = 0.0):
        self.path = db_path + ".cleanup.lock"
        self.timeout = max(0.0, float(timeout))
        self._fh = None
        self._mode = ""
        self._locked = False

    def __enter__(self) -> bool:
        self._mode = ""
        self._locked = False
        try:
            self._fh = open(self.path, "a+")
        except OSError:
            self._mode = "thread"
            self._locked = _thread_lock.acquire(blocking=False)
            return self._locked
        try:
            import fcntl

            self._mode = "flock"
            deadline = time.time() + self.timeout
            while True:
                try:
                    fcntl.flock(self._fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    self._locked = True
                    break
                except OSError:
                    if time.time() >= deadline:
                        break
                    time.sleep(0.05)
        except Exception:
            self._mode = "thread"
            self._locked = _thread_lock.acquire(blocking=False)
        return self._locked

    def __exit__(self, exc_type, exc, tb) -> None:
        try:
            if self._mode == "flock" and self._fh is not None and self._locked:
                try:
                    import fcntl

                    fcntl.flock(self._fh.fileno(), fcntl.LOCK_UN)
                except Exception:
                    pass
            elif self._mode == "thread" and self._locked:
                try:
                    _thread_lock.release()
                except Exception:
                    pass
        finally:
            if self._fh is not None:
                try:
                    self._fh.close()
                except Exception:
                    pass
                self._fh = None
            self._mode = ""
            self._locked = False


def db_size_bytes(db_path: str) -> int:
    """state.db 实际占用：主文件 + WAL/SHM 边车文件。

    只算主文件会低估 20%~30%（实测 756MB 主库 + 233MB WAL），导致阈值、
    低水位、归档判定与上报指标同口径偏低。
    """
    total = 0
    for suffix in ("", "-wal", "-shm"):
        try:
            total += int(os.path.getsize(db_path + suffix))
        except OSError:
            pass
    return total


def discover_state_dbs(root: str, include_children: bool = True) -> list[str]:
    """收集 root 下待治理的 state.db（单文件 + 一层子目录隔离模式）。"""
    paths: list[str] = []
    if not root:
        return paths
    root = os.path.expanduser(root)
    candidate = os.path.join(root, "state.db")
    if os.path.isfile(candidate):
        paths.append(candidate)
    if include_children:
        try:
            names = sorted(os.listdir(root))
        except OSError:
            return paths
        for name in names:
            child = os.path.join(root, name)
            if not os.path.isdir(child):
                continue
            candidate = os.path.join(child, "state.db")
            if os.path.isfile(candidate):
                paths.append(candidate)
    return _dedup(paths)


def _dedup(paths: list[str]) -> list[str]:
    seen = set()
    out = []
    for p in paths:
        key = os.path.abspath(p)
        if key in seen:
            continue
        seen.add(key)
        out.append(p)
    return out


def _stamp_path(db_path: str) -> str:
    return db_path + ".cleanup.stamp"


def cleanup_due(db_path: str, min_interval_s: Optional[float] = None,
                now: Optional[float] = None) -> bool:
    interval = min_interval_default() if min_interval_s is None else float(min_interval_s)
    if interval <= 0:
        return True
    try:
        with open(_stamp_path(db_path), "r", encoding="utf-8") as f:
            last = float((f.read() or "0").strip())
    except (OSError, ValueError):
        return True
    return (time.time() if now is None else float(now)) - last >= interval


def _write_stamp(db_path: str, now: Optional[float] = None) -> None:
    try:
        with open(_stamp_path(db_path), "w", encoding="utf-8") as f:
            f.write(str(float(time.time() if now is None else now)))
    except OSError:
        pass


def _clear_stamp(db_path: str) -> None:
    try:
        os.remove(_stamp_path(db_path))
    except OSError:
        pass


def _connect(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path, timeout=30)
    conn.execute("PRAGMA busy_timeout=30000")
    return conn


def _integrity_check(conn: sqlite3.Connection) -> str:
    try:
        row = conn.execute("PRAGMA integrity_check").fetchone()
        return str(row[0]) if row else "unknown"
    except Exception as exc:
        return f"error: {exc}"


def _table_columns(conn: sqlite3.Connection, table: str) -> list[str]:
    try:
        return [str(r[1]) for r in conn.execute(f'PRAGMA table_info("{table}")').fetchall()]
    except Exception:
        return []


def _has_table(conn: sqlite3.Connection, table: str) -> bool:
    try:
        row = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
        ).fetchone()
        return bool(row)
    except Exception:
        return False


def _session_activity(conn: sqlite3.Connection) -> Optional[list[tuple[str, float, float]]]:
    """返回 (session_id, last_activity_ts, ended_ts)；ended_ts=0 表示会话未结束。"""
    if not _has_table(conn, "sessions"):
        return None
    cols = set(_table_columns(conn, "sessions"))
    if "id" not in cols:
        return None
    start_col = "started_at" if "started_at" in cols else None
    end_col = "ended_at" if "ended_at" in cols else None
    if not start_col and not end_col:
        return None
    select = ["id"]
    select.append(f"COALESCE({start_col},0)" if start_col else "0")
    select.append(f"COALESCE({end_col},0)" if end_col else "0")
    rows = conn.execute(f"SELECT {', '.join(select)} FROM sessions").fetchall()
    activity: list[tuple[str, float, float]] = []
    for sid, started, ended in rows:
        sid = str(sid or "")
        if not sid:
            continue
        try:
            started_ts = float(started or 0)
        except (TypeError, ValueError):
            started_ts = 0.0
        try:
            ended_ts = float(ended or 0)
        except (TypeError, ValueError):
            ended_ts = 0.0
        activity.append((sid, max(started_ts, ended_ts), ended_ts))
    return activity


def _eligible_sessions(activity: list[tuple[str, float, float]], min_sessions: int,
                       cutoff_ts: Optional[float],
                       protect_session_ids: Optional[set[str]] = None
                       ) -> list[tuple[str, float, float]]:
    ordered = sorted(activity, key=lambda item: item[1], reverse=True)
    protected = {sid for sid, _, _ in ordered[:max(0, int(min_sessions))]}
    protected.update(protect_session_ids or set())
    eligible = [item for item in ordered if item[0] not in protected]
    if cutoff_ts is not None:
        eligible = [item for item in eligible if item[1] < cutoff_ts]
    eligible.sort(key=lambda item: item[1])
    return eligible


def _forced_eligible(item: tuple[str, float, float], idle_cutoff_ts: float) -> bool:
    """round2（forced）只清理已结束且空闲超过门槛的会话。

    未置位 ``ended_at`` 的会话可能是活跃会话/即将 resume 的会话，绝不由强制轮删除；
    这类情况交给“仍超阈值 → 归档重建”兜底，避免一次清掉数千个仍可复用的 context。
    """
    _sid, last_ts, ended_ts = item
    return bool(ended_ts and ended_ts > 0 and last_ts < idle_cutoff_ts)


def _count_messages(conn: sqlite3.Connection, session_ids: list[str]) -> int:
    if not session_ids or not _has_table(conn, "messages"):
        return 0
    total = 0
    for batch in _batches(session_ids, 200):
        marks = ",".join("?" for _ in batch)
        try:
            row = conn.execute(
                f"SELECT COUNT(*) FROM messages WHERE session_id IN ({marks})",
                tuple(batch),
            ).fetchone()
            total += int(row[0] or 0) if row else 0
        except Exception:
            pass
    return total


def _delete_from_session_tables(conn: sqlite3.Connection, session_ids: list[str]) -> int:
    deleted_messages = 0
    for batch in _batches(session_ids, 200):
        marks = ",".join("?" for _ in batch)
        params = tuple(batch)
        if _has_table(conn, "messages") and "session_id" in _table_columns(conn, "messages"):
            cur = conn.execute(
                f"DELETE FROM messages WHERE session_id IN ({marks})", params
            )
            deleted_messages += int(getattr(cur, "rowcount", 0) or 0)
        for table in _session_tables[1:]:
            if _has_table(conn, table) and "session_id" in _table_columns(conn, table):
                conn.execute(f"DELETE FROM {table} WHERE session_id IN ({marks})", params)
        for table, columns in _delegation_tables.items():
            if not _has_table(conn, table):
                continue
            available = [c for c in columns if c in _table_columns(conn, table)]
            for column in available:
                conn.execute(f"DELETE FROM {table} WHERE {column} IN ({marks})", params)
        if _has_table(conn, "sessions") and "id" in _table_columns(conn, "sessions"):
            conn.execute(f"DELETE FROM sessions WHERE id IN ({marks})", params)
    return deleted_messages


def _batches(items: list[str], size: int):
    for i in range(0, len(items), size):
        yield items[i:i + size]


def _vacuum(conn: sqlite3.Connection) -> None:
    try:
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    except Exception:
        pass
    conn.execute("VACUUM")


def _prune_old_backups(backup_dir: str, retention_hours: Optional[float] = None) -> None:
    hours = _backup_retention_hours() if retention_hours is None else float(retention_hours)
    if hours <= 0:
        return
    cutoff = time.time() - hours * 3600
    try:
        names = os.listdir(backup_dir)
    except OSError:
        return
    for name in names:
        if not name.endswith(".bak"):
            continue
        path = os.path.join(backup_dir, name)
        try:
            if os.path.getmtime(path) < cutoff:
                os.remove(path)
        except OSError:
            pass


def _backup_db(db_path: str, backup_dir: str) -> str:
    os.makedirs(backup_dir, mode=0o700, exist_ok=True)
    try:
        free = shutil.disk_usage(backup_dir).free
    except OSError:
        free = 0
    need = db_size_bytes(db_path) + 32 * 1024 * 1024
    if free and free < need:
        raise OSError(f"insufficient backup space: free={free} need>={need}")
    name = os.path.basename(db_path) or "state.db"
    stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime())
    dest = os.path.join(backup_dir, f"{name}.{stamp}.bak")
    src = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=30)
    try:
        dst = sqlite3.connect(dest, timeout=30)
        try:
            src.backup(dst)
        finally:
            dst.close()
    finally:
        src.close()
    try:
        os.chmod(dest, 0o600)
    except OSError:
        pass
    _prune_old_backups(backup_dir)
    return dest


def _remove_db_files(db_path: str) -> None:
    for suffix in ("", "-wal", "-shm"):
        try:
            os.remove(db_path + suffix)
        except OSError:
            pass


def _restore_backup(backup_path: str, db_path: str) -> None:
    _remove_db_files(db_path)
    shutil.copy2(backup_path, db_path)


def _record_cleanup(result: str, freed: int = 0) -> None:
    global _bytes_freed_total
    with _metrics_lock:
        _cleanup_counts[result] = _cleanup_counts.get(result, 0) + 1
        _bytes_freed_total += max(0, int(freed))


def record_resume_skipped(reason: str = "") -> None:
    global _resume_skipped_total
    with _metrics_lock:
        _resume_skipped_total += 1


def reset_metrics() -> None:
    global _bytes_freed_total, _resume_skipped_total
    with _metrics_lock:
        _cleanup_counts.clear()
        _bytes_freed_total = 0
        _resume_skipped_total = 0


def worker_metrics_snapshot() -> dict[str, Any]:
    with _metrics_lock:
        counts = dict(_cleanup_counts)
        freed = int(_bytes_freed_total)
        skipped = int(_resume_skipped_total)
    scanned = scan_state_db_sizes()
    return {
        "acp_state_db_bytes": float(scanned.get("max_bytes", 0)),
        "acp_state_db_cleanup_counts": json.dumps(counts, ensure_ascii=False, sort_keys=True),
        "acp_state_db_bytes_freed_total": float(freed),
        "acp_resume_skipped_total": int(skipped),
    }


_SCAN_CACHE: dict[str, Any] = {"taken_at": 0.0, "data": {}}
_SCAN_TTL = 60.0


def _state_db_roots(roots: Optional[list[str]] = None) -> list[str]:
    if roots is not None:
        return [os.path.expanduser(r) for r in roots if r]
    out = []
    hermes_home = os.environ.get("HERMES_HOME", "~/.hermes")
    out.append(os.path.expanduser(hermes_home))
    out.append(os.path.join(os.path.expanduser("~"), ".cache", "hermes-acp-homes"))
    return out


def scan_state_db_sizes(roots: Optional[list[str]] = None, now: Optional[float] = None) -> dict[str, Any]:
    current = time.time() if now is None else float(now)
    cached = _SCAN_CACHE["data"]
    if roots is None and cached and current - _SCAN_CACHE["taken_at"] < _SCAN_TTL:
        return dict(cached)
    max_bytes = 0
    max_path = ""
    total_bytes = 0
    count = 0
    for root in _state_db_roots(roots):
        for path in discover_state_dbs(root, include_children=True):
            size = db_size_bytes(path)
            total_bytes += size
            count += 1
            if size > max_bytes:
                max_bytes = size
                max_path = path
    data = {
        "max_bytes": max_bytes,
        "max_path": max_path,
        "total_bytes": total_bytes,
        "count": count,
    }
    if roots is None:
        _SCAN_CACHE["data"] = data
        _SCAN_CACHE["taken_at"] = current
    return dict(data)


def prune_and_vacuum(db_path: str, retention_days: Optional[float] = None,
                     min_sessions: Optional[int] = None,
                     backup_dir: Optional[str] = None,
                     target_bytes: Optional[int] = None,
                     dry_run: bool = False,
                     now: Optional[float] = None,
                     lock_timeout: float = 0.0,
                     protect_session_ids: Optional[list[str]] = None,
                     force_min_idle_days: Optional[float] = None) -> MaintenanceResult:
    """就地清理单个 state.db：校验 → 备份 → 裁剪 → VACUUM → 校验。

    裁剪分两轮：
    - retention：删除活跃时间早于保留窗口的会话；
    - forced：若仍高于低水位，仅删除「已结束且空闲超过 force_min_idle_days」的会话，
      未结束会话绝不在此轮删除（避免误删仍可 resume 的 context），并单独打点 ``forced``。
    """
    result = MaintenanceResult(db_path=db_path)
    if not os.path.isfile(db_path):
        result.reason = "not_found"
        _record_cleanup(_RESULT_SKIPPED)
        return result

    retention = retention_days_default() if retention_days is None else float(retention_days)
    keep_sessions = min_sessions_default() if min_sessions is None else max(0, int(min_sessions))
    target = low_water_bytes_default() if target_bytes is None else max(0, int(target_bytes))
    force_idle_days = (force_min_idle_days_default() if force_min_idle_days is None
                       else float(force_min_idle_days))
    protect_ids = {str(s).strip() for s in (protect_session_ids or []) if str(s).strip()}
    current = time.time() if now is None else float(now)
    result.before_bytes = db_size_bytes(db_path)

    with _DbLock(db_path, timeout=lock_timeout) as locked:
        if not locked:
            result.reason = "locked"
            _record_cleanup(_RESULT_SKIPPED)
            return result

        try:
            conn = _connect(db_path)
        except Exception as exc:
            result.result = _RESULT_FAILED
            result.error = f"open_failed: {exc}"
            _record_cleanup(_RESULT_FAILED)
            return result

        try:
            result.integrity_before = _integrity_check(conn)
            if result.integrity_before != "ok":
                result.result = _RESULT_FAILED
                result.reason = "integrity_before_failed"
                result.error = result.integrity_before
                _record_cleanup(_RESULT_FAILED)
                return result

            activity = _session_activity(conn)
            if activity is None:
                result.result = _RESULT_FAILED
                result.reason = "schema_unrecognized"
                _record_cleanup(_RESULT_FAILED)
                return result

            cutoff = current - retention * 86400.0
            older = _eligible_sessions(activity, keep_sessions, cutoff, protect_ids)
            remaining = _eligible_sessions(activity, keep_sessions, None, protect_ids)
            plan: list[tuple[str, list[tuple[str, float, float]]]] = []
            if older:
                plan.append(("retention", older))
            older_ids = {sid for sid, _, _ in older}
            force_idle_cutoff = current - max(0.0, force_idle_days) * 86400.0
            extra = [item for item in remaining
                     if item[0] not in older_ids and _forced_eligible(item, force_idle_cutoff)]
            if extra:
                plan.append(("forced", extra))

            if dry_run:
                candidate_round1 = [sid for sid, _, _ in older]
                candidate_forced = [sid for sid, _, _ in extra]
                result.pruned_sessions = len(candidate_round1) + len(candidate_forced)
                result.forced_pruned_sessions = len(candidate_forced)
                result.pruned_messages = _count_messages(
                    conn, candidate_round1 + candidate_forced)
                result.after_bytes = result.before_bytes
                result.result = _RESULT_DRY_RUN
                result.reason = (f"eligible={len(candidate_round1)} "
                                 f"forced={len(candidate_forced)}")
                _record_cleanup(_RESULT_DRY_RUN)
                return result

            try:
                result.backup_path = _backup_db(db_path, backup_dir or backup_dir_default())
            except Exception as exc:
                result.result = _RESULT_FAILED
                result.reason = "backup_failed"
                result.error = str(exc)[:300]
                _record_cleanup(_RESULT_FAILED)
                return result

            try:
                for round_name, batch in plan:
                    ids = [sid for sid, _, _ in batch]
                    conn.execute("BEGIN IMMEDIATE")
                    try:
                        deleted = _delete_from_session_tables(conn, ids)
                        conn.commit()
                    except Exception:
                        conn.rollback()
                        raise
                    result.pruned_sessions += len(ids)
                    result.pruned_messages += deleted
                    if round_name == "forced":
                        result.forced_pruned_sessions += len(ids)
                        _record_cleanup(_RESULT_FORCED)
                        log.info(
                            "ACP state.db forced prune round: db=%s pruned_sessions=%s "
                            "idle_days=%s",
                            db_path, len(ids), force_idle_days)
                    _vacuum(conn)
                    if db_size_bytes(db_path) <= target:
                        break
                result.integrity_after = _integrity_check(conn)
                result.after_bytes = db_size_bytes(db_path)
                result.freed_bytes = max(0, result.before_bytes - result.after_bytes)
                if result.integrity_after != "ok":
                    raise RuntimeError(f"integrity_after_failed: {result.integrity_after}")
                result.result = _RESULT_OK
                _record_cleanup(_RESULT_OK, result.freed_bytes)
            except Exception as exc:
                result.result = _RESULT_FAILED
                result.error = str(exc)[:300]
                try:
                    _restore_backup(result.backup_path, db_path)
                    result.after_bytes = db_size_bytes(db_path)
                    result.freed_bytes = 0
                    result.reason = "restored_from_backup"
                except Exception as restore_exc:
                    result.reason = "restore_failed"
                    result.error = f"{result.error}; restore: {restore_exc}"[:500]
                _record_cleanup(_RESULT_FAILED)
            return result
        finally:
            try:
                conn.close()
            except Exception:
                pass


def archive_and_recreate(db_path: str, backup_dir: Optional[str] = None,
                         now: Optional[float] = None,
                         lock_timeout: float = 0.0) -> MaintenanceResult:
    """归档 state.db 到带时间戳备份并新建空库（清理后仍超阈值时兜底）。"""
    result = MaintenanceResult(db_path=db_path, archived=True)
    if not os.path.isfile(db_path):
        result.reason = "not_found"
        _record_cleanup(_RESULT_SKIPPED)
        return result
    with _DbLock(db_path, timeout=lock_timeout) as locked:
        if not locked:
            result.reason = "locked"
            _record_cleanup(_RESULT_SKIPPED)
            return result
        result.before_bytes = db_size_bytes(db_path)
        try:
            result.backup_path = _backup_db(db_path, backup_dir or backup_dir_default())
            _remove_db_files(db_path)
            conn = sqlite3.connect(db_path, timeout=30)
            conn.close()
            check_conn = _connect(db_path)
            try:
                result.integrity_after = _integrity_check(check_conn)
            finally:
                check_conn.close()
            result.after_bytes = db_size_bytes(db_path)
            result.freed_bytes = max(0, result.before_bytes - result.after_bytes)
            if result.integrity_after != "ok":
                raise RuntimeError(f"integrity_after_failed: {result.integrity_after}")
            result.result = _RESULT_ARCHIVED
            result.reason = "archived_and_recreated"
            _record_cleanup(_RESULT_ARCHIVED, result.freed_bytes)
        except Exception as exc:
            result.result = _RESULT_FAILED
            result.error = str(exc)[:300]
            try:
                if result.backup_path and os.path.isfile(result.backup_path):
                    _restore_backup(result.backup_path, db_path)
                    result.reason = "restored_from_backup"
            except Exception as restore_exc:
                result.reason = "restore_failed"
                result.error = f"{result.error}; restore: {restore_exc}"[:500]
            _record_cleanup(_RESULT_FAILED)
        return result


def check_state_db_size(hermes_home: str, threshold_mb: Optional[float] = None,
                        low_water_mb: Optional[float] = None,
                        retention_days: Optional[float] = None,
                        min_sessions: Optional[int] = None,
                        min_interval_s: Optional[float] = None,
                        backup_dir: Optional[str] = None,
                        force: bool = False,
                        db_path: Optional[str] = None,
                        now: Optional[float] = None,
                        protect_session_ids: Optional[list[str]] = None,
                        force_min_idle_days: Optional[float] = None) -> StateDbCheckResult:
    """检测 state.db 体积并执行清理；返回是否跳过 resume 的结构化结果。

    清理成功且回落到阈值内 → skip_resume=False（继续 resume）。
    节流中/清理失败/清理后仍超阈值（已归档重建）→ skip_resume=True（session/new）。
    ``protect_session_ids`` 用于把本次即将 resume 的会话并入保护集，避免被裁剪后
    resume 失败回落 session/new 且不计 ACP_RESUME_SKIPPED。
    """
    max_mb = _env_float(MAX_MB_ENV, DEFAULT_MAX_MB) if threshold_mb is None else float(threshold_mb)
    low_mb = _env_float(LOW_WATER_MB_ENV, DEFAULT_LOW_WATER_MB) if low_water_mb is None else float(low_water_mb)
    max_size = int(max_mb * 1024 * 1024)
    low_size = int(low_mb * 1024 * 1024)
    target = db_path or os.path.join(hermes_home, "state.db")
    current = time.time() if now is None else float(now)

    out = StateDbCheckResult(db_path=target)
    if not os.path.isfile(target):
        return out

    out.before_bytes = db_size_bytes(target)
    out.after_bytes = out.before_bytes
    if out.before_bytes <= max_size:
        return out

    out.exceeded = True

    if not force and not cleanup_due(target, min_interval_s, current):
        out.skip_resume = True
        out.reason = "throttled"
        return out

    cleanup = prune_and_vacuum(
        target,
        retention_days=retention_days,
        min_sessions=min_sessions,
        backup_dir=backup_dir,
        target_bytes=low_size,
        now=current,
        protect_session_ids=protect_session_ids,
        force_min_idle_days=force_min_idle_days,
    )
    # 节流 stamp 只在真正执行过清理（ok/archived/dry-run）后写入；失败/锁跳过时清除，
    # 避免失败后 1 小时内全部 throttle → 新任务直接 skip_resume，扩大上下文丢失窗口。
    if cleanup.result in (_RESULT_FAILED, _RESULT_SKIPPED):
        _clear_stamp(target)
    else:
        _write_stamp(target, current)
    out.cleanup = cleanup
    out.after_bytes = db_size_bytes(target)

    if cleanup.result == _RESULT_OK and out.after_bytes <= max_size:
        out.skip_resume = False
        out.reason = "cleaned"
        log.info(
            "ACP state.db cleanup ok: db=%s before=%s after=%s pruned_sessions=%s "
            "pruned_messages=%s forced_sessions=%s backup=%s",
            target, cleanup.before_bytes, cleanup.after_bytes,
            cleanup.pruned_sessions, cleanup.pruned_messages,
            cleanup.forced_pruned_sessions, cleanup.backup_path,
        )
        return out

    if cleanup.result == _RESULT_FAILED:
        out.skip_resume = True
        out.reason = "cleanup_failed"
        log.warning(
            "ACP state.db cleanup failed: db=%s error=%s reason=%s",
            target, cleanup.error, cleanup.reason,
        )
        return out

    if out.after_bytes > max_size:
        archive = archive_and_recreate(target, backup_dir=backup_dir, now=current)
        out.archive = archive
        out.after_bytes = db_size_bytes(target)
        out.skip_resume = True
        out.reason = "archived" if archive.result == _RESULT_ARCHIVED else "archive_failed"
        log.warning(
            "ACP state.db still over threshold after cleanup, archived=%s db=%s "
            "before=%s after=%s backup=%s error=%s",
            archive.result, target, archive.before_bytes, archive.after_bytes,
            archive.backup_path, archive.error,
        )
        return out

    out.skip_resume = False
    out.reason = "cleaned"
    return out