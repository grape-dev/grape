"""Webhook 验签拒绝事实（`obs_webhook_rejects`）——Issue #3750。

背景：`grape-dev/grape` 1 小时内 54 次 `Webhook rejected: token mismatch`（403）。同仓库仍有
正常事件通过，说明是「同一仓库配了多条 webhook、残留旧 secret」的配置侧问题；webhook 无重放，
被 403 丢弃的 Push Hook 事件即永久丢失。原实现只有一行 WARNING：没法按窗口做速率统计，
也没法按来源反查是哪条残留 hook 在发事件。

三条硬约束（Issue #3750 验收口径）：

1. **只存 sha256 短指纹**：`token_fp` / `expected_fp` 一律 `sha256:<12>`（不可逆、恒长），
   绝不存明文 token 及任意长度前缀/子串（#860 脱敏成果不得回退）。
2. **写入不阻塞 403 返回**：有界队列 + daemon 线程攒批落库；队满丢弃并计数，任何异常都不上抛。
   403 在请求路径上，落库失败只能丢诊断数据，不能拖慢拒绝、更不能把拒绝变成 500。
3. **可反查**：行里保留 `repo_key` / `provider` / `event` / `ip` / `token_fp` / `expected_fp` /
   `reason`，运维按 `(ip, token_fp)` 即可定位具体的旧 secret。

表归 insight 域（DDL 见 `src/domains/insight/schema_ddl.py::_OBS_WEBHOOK_REJECTS_DDL`），
速率告警的求值在 `alerts._repo_metrics`（本模块提供窗口计数原语）。
"""

from __future__ import annotations

import hashlib
import os
import queue
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from src.kernel.config import log

#: 拒绝行留存期（与 obs_log 的 14 天对齐；只服务速率告警与近期排障）
RETENTION_DAYS = 14
#: 清理最多每小时一次（由采样循环调用 maybe_cleanup）
CLEANUP_INTERVAL_SEC = 3600
#: 队列上限：宁可丢诊断行，也不在请求路径上排队
QUEUE_MAXSIZE = 1024
#: 单批落库上限
BATCH_SIZE = 100

#: 写入模式：`1`（默认）= 有界队列 + 后台线程；`sync` = 同步落库（测试/无法起后台线程
#: 的环境，同 `OBS_LOG_DB=0` 的动机：daemon 线程跨测试库 close/重绑会触发 sqlite UAF）；
#: `0` = 整体关闭（只留日志）。
_MODE = os.environ.get("OBS_WEBHOOK_REJECTS", "1").strip().lower()
_DB_ENABLED = _MODE not in ("0", "false", "no")
_SYNC_MODE = _MODE == "sync"

#: 拒绝原因取值——absent=请求没带 token；mismatch=带了但与托管配置不一致
REASONS = ("absent", "mismatch")

_INSERT_SQL = ("INSERT INTO obs_webhook_rejects "
               "(repo_key, provider, event, ip, token_fp, expected_fp, reason, created_at) "
               "VALUES (?,?,?,?,?,?,?,?)")


def token_fingerprint(value: Any) -> str:
    """token 指纹：`sha256:<前 12 位十六进制>`；空值 → `absent`。

    唯一实现（Issue #3897 C 组 B 的日志形态与 Issue #3750 的落库形态共用）：
    12 位十六进制不可逆、恒长，足以区分「新旧 secret」而不泄露任何前缀。
    """
    v = str(value or "")
    if not v:
        return "absent"
    return "sha256:" + hashlib.sha256(v.encode("utf-8")).hexdigest()[:12]


def reject_reason(token: Any) -> str:
    """403 拒绝原因：没带 token → `absent`；带了但不匹配 → `mismatch`。"""
    return "mismatch" if str(token or "") else "absent"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _iso_before(seconds: float = 0, days: float = 0) -> str:
    delta = timedelta(seconds=seconds, days=days)
    return (datetime.now(timezone.utc) - delta).isoformat()


def _write_rows(rows: List[tuple]) -> bool:
    """同步落一批拒绝行——**唯一 SQL 出口**（队列 writer 与 sync 模式共用，口径不漂移）。

    异常只记日志并返回 False：调用方（403 拒绝路径）绝不能因为诊断落库失败而改变语义。
    """
    if not rows:
        return True
    try:
        from src.kernel.db_registry import cursor_for

        with cursor_for("insight", write=True) as c:
            c.executemany(_INSERT_SQL, rows)
        return True
    except Exception as e:  # noqa: BLE001 - 落库失败只丢诊断行
        log.warning(f"obs webhook rejects ingest failed ({len(rows)} rows): {e}")
        return False


def _row(repo_key: str, provider: str, event: str, ip: str,
         token_fp: str, expected_fp: str, reason: str) -> tuple:
    return (repo_key or "", provider or "", event or "", ip or "",
            token_fp or "", expected_fp or "", reason or "mismatch", _now_iso())


class _RejectRecorder:
    """有界队列 + 后台攒批落库（形态与 `logs.ObsLogHandler` 一致）。

    - `record()` 只做 `put_nowait`，队满丢弃并计数：**绝不阻塞调用方**
    - 后台 daemon 线程每批 ≤100 行，1s 或攒满即写；写失败只记日志，不重试轰炸
    """

    def __init__(self, maxsize: int = QUEUE_MAXSIZE) -> None:
        self.q: "queue.Queue" = queue.Queue(maxsize=maxsize)
        self.written = 0
        self.dropped = 0
        self.failed = 0
        #: 入队成功数 / 已完成落库数——`flush()` 以两者相等为排空判据（队列 empty 不等于
        #: 已落库：writer 取出批次后队列就空了，落库还在途）
        self._enqueued = 0
        self._processed = 0
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._run, name="obs-webhook-reject-writer",
                                        daemon=True)
        self._thread.start()

    def record(self, repo_key: str, provider: str = "", event: str = "", ip: str = "",
               token_fp: str = "", expected_fp: str = "", reason: str = "mismatch") -> bool:
        """入队一条拒绝事实；返回是否入队成功（队满/关闭 → False，绝不抛）。"""
        try:
            self.q.put_nowait(_row(repo_key, provider, event, ip,
                                   token_fp, expected_fp, reason))
            self._enqueued += 1
            return True
        except queue.Full:
            self.dropped += 1
            return False
        except Exception as e:  # noqa: BLE001 - 诊断数据不能影响 403
            self.failed += 1
            log.debug(f"obs webhook reject enqueue failed: {e}")
            return False

    def _run(self) -> None:
        batch: List[tuple] = []
        while not self._stop.is_set():
            rec = None
            try:
                rec = self.q.get(timeout=1.0)
            except queue.Empty:
                rec = None                      # 空闲轮询：本轮无新行，直接落已有的批
            if rec is not None:
                batch.append(rec)
            while len(batch) < BATCH_SIZE:
                try:
                    batch.append(self.q.get_nowait())
                except queue.Empty:
                    break
            if batch:
                self._flush(batch)
                self._processed += len(batch)
                batch = []

    def _flush(self, batch: List[tuple]) -> None:
        if _write_rows(batch):
            self.written += len(batch)
        else:
            self.failed += len(batch)

    def flush(self, timeout: float = 2.0) -> None:
        """等待「已入队」的行全部落库（测试/优雅关闭用）；超时返回，不抛。"""
        target = self._enqueued
        deadline = time.time() + timeout
        while time.time() < deadline and self._processed < target:
            time.sleep(0.02)

    def stats(self) -> Dict[str, int]:
        return {"queued": self.q.qsize(), "written": self.written,
                "dropped": self.dropped, "failed": self.failed}


_recorder: Optional[_RejectRecorder] = None
_recorder_lock = threading.Lock()


def _get_recorder() -> _RejectRecorder:
    global _recorder
    if _recorder is None:
        with _recorder_lock:
            if _recorder is None:
                _recorder = _RejectRecorder()
                _recorder.start()
    return _recorder


def record_reject(repo_key: str, provider: str = "", event: str = "", ip: str = "",
                  token: str = "", expected: str = "") -> bool:
    """记录一次 webhook 验签拒绝（Issue #3750 的唯一写入入口）。

    指纹由本函数计算（调用方只传原始值，避免日志口径与落库口径各算一套）。
    Prometheus 计数与队列入队**互不影响**：计数器自增失败也不阻断入队。
    `OBS_WEBHOOK_REJECTS=0` 时整体跳过（只保留日志）。
    """
    if not _DB_ENABLED:
        return False
    token_fp = token_fingerprint(token)
    expected_fp = token_fingerprint(expected)
    reason = reject_reason(token)
    try:
        from src.domains.insight.observability import WEBHOOK_TOKEN_MISMATCH_TOTAL
        WEBHOOK_TOKEN_MISMATCH_TOTAL.labels(repo=repo_key or "unknown").inc()
    except Exception as e:  # noqa: BLE001 - 指标不可用不影响落库（只记 DEBUG，别把拒绝打挂）
        log.debug(f"obs webhook mismatch counter unavailable: {e}")
    if _SYNC_MODE:
        # sync 模式：无后台线程、直接落库（测试与不允许长驻线程的环境）
        return _write_rows([_row(repo_key, provider, event, ip,
                                 token_fp, expected_fp, reason)])
    try:
        return _get_recorder().record(repo_key, provider, event, ip,
                                      token_fp, expected_fp, reason)
    except Exception as e:  # noqa: BLE001 - 任何异常都不得影响 403 返回
        log.debug(f"obs webhook reject record failed: {e}")
        return False


def flush(timeout: float = 2.0) -> None:
    """把队列刷盘（测试与优雅关闭用）。"""
    if _recorder is not None:
        _recorder.flush(timeout)


def stats() -> Dict[str, int]:
    return _recorder.stats() if _recorder is not None else {
        "queued": 0, "written": 0, "dropped": 0, "failed": 0}


def count_recent_split(cursor, window_sec: float = 60.0,
                       presence_sec: float = 900.0) -> Dict[str, int]:
    """近 `presence_sec` 秒内有拒绝的仓库 → 其在近 `window_sec` 秒内的计数。

    **一条 SQL 同时回答两个问题**：「哪些仓库仍处于维度存在期内」与「各自窗口内多少次」。
    存在期内但窗口内为 0 的仓库同样出现在结果里（value=0）：告警求值据此走 resolved
    分支，而不是把维度从本轮集合里摘掉（摘掉后已 firing 的事件永远等不到恢复）。

    存在期必须 ≥ 统计窗口（`alerts.WEBHOOK_REJECT_PRESENCE_SEC` 900s > 窗口 60s）。
    时间比较用 ISO 字符串（写入方统一 ISO(UTC)）。
    """
    window_cutoff = _iso_before(seconds=window_sec)
    presence_cutoff = _iso_before(seconds=presence_sec)
    cursor.execute("SELECT repo_key,"
                   " SUM(CASE WHEN created_at >= ? THEN 1 ELSE 0 END)"
                   " FROM obs_webhook_rejects"
                   " WHERE created_at >= ? GROUP BY repo_key",
                   (window_cutoff, presence_cutoff))
    out: Dict[str, int] = {}
    for row in cursor.fetchall() or []:
        # SUM/COUNT 是整型聚合：row[1] 只可能是 int/None，无需防御性异常分支
        out[str(row[0] or "")] = int(row[1] or 0)
    return out


def count_recent(cursor, window_sec: float = 60.0) -> Dict[str, int]:
    """近 `window_sec` 秒内按仓库的拒绝计数 `{repo_key: n}`（窗口即存在期）。

    `count_recent_split` 在「存在期 = 窗口」时的退化形态：只返回窗口内有行的仓库。
    """
    return count_recent_split(cursor, window_sec=window_sec, presence_sec=window_sec)


_last_cleanup = 0.0


def maybe_cleanup(interval: int = CLEANUP_INTERVAL_SEC, days: int = RETENTION_DAYS) -> int:
    """按留存期删除过期拒绝行（最多每 `interval` 秒执行一次），返回删除行数。"""
    global _last_cleanup
    now = time.time()
    if now - _last_cleanup < float(interval or 0):
        return 0
    _last_cleanup = now
    cutoff = _iso_before(days=days)
    try:
        from src.kernel.db_registry import cursor_for

        with cursor_for("insight", write=True) as c:
            c.execute("DELETE FROM obs_webhook_rejects WHERE created_at != ''"
                      " AND created_at < ?", (cutoff,))
            n = int(c.rowcount or 0)
        if n:
            log.info(f"obs webhook rejects cleanup: removed {n} rows older than {days}d")
        return n
    except Exception as e:  # noqa: BLE001 - 清理失败不影响主链路
        log.warning(f"obs webhook rejects cleanup failed: {e}")
        return 0

# ===== 迁移实现（Issue #3750 / PR #2157 复审 C2）：建表 + 时间窗索引 =====
# 为什么在这里：表归 insight 域（DDL 见 `schema_ddl._OBS_WEBHOOK_REJECTS_DDL`），
# 建表/建索引语句若写在 `src/api/db_setup.py` 会被 tests/arch/test_foreign_table_refs.py
# 判成 api 层新增跨域引用；注册（register_migration）仍由 db_setup 做，本模块只导出实现。
#
# 事故形态（PR #2157 复审 C2）：obs_webhook_rejects 此前只登记在 `_NEW_TABLES`，
# 而 `_create_tables()` 只在新装库首跑——存量库启动不跑它，于是 202611250006 的
# `CREATE INDEX` 每次都 1146（no such table），失败不盖章、每次启动重试，写侧也只剩
# 「obs webhook rejects ingest failed」WARNING 丢诊断行。

TABLE = "obs_webhook_rejects"
TIME_INDEX = "idx_obs_webhook_rejects_time_repo"
#: MySQL 索引前缀长度：repo_key / created_at 是 TEXT，不带前缀会 1170。
_MYSQL_INDEX_PREFIX = 191


def _migration_conn():
    """迁移专用连接（三库模式切 insight 域，单库回落进程库）。"""
    from src.api import db_setup as ds
    from src.kernel.session_manager import db as _db

    return ds._migration_conn("insight", default=_db)


def ensure_table(conn: Any = None) -> bool:
    """建表（缺失才建；幂等）。返回 True=本次新建，False=已在位。

    不依赖 `_create_tables()`（存量库不跑）——注册式迁移自己建，才盖得上章。
    """
    from src.api import db_setup as ds
    from src.domains.insight.schema_ddl import _OBS_WEBHOOK_REJECTS_DDL

    target = conn if conn is not None else _migration_conn()
    c = target.cursor()
    if ds._table_present(c, TABLE):
        return False
    c.execute(_OBS_WEBHOOK_REJECTS_DDL)
    target.commit()
    return True


def verify_table(conn: Any = None) -> None:
    """后置校验：表在位才揭章；缺失 raise（不盖章，下次启动重试）。"""
    from src.api import db_setup as ds

    target = conn if conn is not None else _migration_conn()
    if not ds._table_present(target.cursor(), TABLE):
        raise RuntimeError(f"202611240016 post_check 未收敛：{TABLE} 缺失（不盖章，下次启动重试）")
    log.info(f"202611240016 post_check 通过：{TABLE} 在位")


def ensure_time_index(conn: Any = None) -> bool:
    """补 `(created_at, repo_key)` 时间窗索引（幂等）。返回 True=本次新建。

    先判表在位、缺失则先建：存量库台账可能已按「同号不同名」/历史版本盖过索引迁移章，
    表却因为没登记注册式建表而从来不存在——不先建表就只能 1146 失败、永远盖不上章。
    """
    from src.api import db_setup as ds

    target = conn if conn is not None else _migration_conn()
    ensure_table(target)
    c = target.cursor()
    if ds._index_present(c, TABLE, TIME_INDEX):
        return False
    if ds._mysql_backend():
        ddl = (f"CREATE INDEX {TIME_INDEX} ON {TABLE}"
               f"(created_at({_MYSQL_INDEX_PREFIX}), repo_key({_MYSQL_INDEX_PREFIX}))")
    else:
        ddl = f"CREATE INDEX {TIME_INDEX} ON {TABLE}(created_at, repo_key)"
    try:
        c.execute(ddl)
    except Exception as e:  # noqa: BLE001 - 1061 = 并发建好，视为建成
        if "1061" in str(e) or "already exists" in str(e).lower():
            log.info(f"{TIME_INDEX} 已存在（1061），视为建成")
        else:
            target.rollback()
            raise
    target.commit()
    return True


def verify_time_index(conn: Any = None) -> None:
    """后置校验：表 + 时间窗索引都在位才盖章（PR #2157 复审 R1：台账已盖章但目标缺失的库）。"""
    from src.api import db_setup as ds

    target = conn if conn is not None else _migration_conn()
    c = target.cursor()
    if not ds._table_present(c, TABLE):
        raise RuntimeError(f"202611250006 post_check 未收敛：{TABLE} 缺失（不盖章，下次启动重试）")
    if not ds._index_present(c, TABLE, TIME_INDEX):
        raise RuntimeError(
            f"202611250006 post_check 未收敛：{TIME_INDEX} 缺失（索引未建成，不盖章，下次启动重试）")
