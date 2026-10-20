"""可观测性 · 日志热数据（`obs_log`）——落库、检索、留存、脱敏。

设计：docs/weekly-07/observability-redesign-design.md §6.3（日志）+ §5.3（三层边界）

四条铁律：
1. **只落 WARNING/ERROR**（INFO 走文件）—— 控量，且 ERROR/WARNING 才是排障入口（risk R2）
2. **用户面只返回"自己可见仓库"的日志**：`repo_key` 在可见集合内才可见；
   没有 repo 维度的主控日志（进程/DB/平台类）**不进用户面**（§5.3，避免泄露平台内部信息）
3. **脱敏**：token / 密码 / 连接串在**入库前**打码（存了就晚了），用户面接口再兜一层
4. **写入不阻塞主链路**：主控侧走有界队列 + 后台批量落库（队满丢弃并计数，可 env 关闭）；
   worker 侧复用既有心跳 log_lines 通道，不新增 worker 侧链路
"""

from __future__ import annotations

import hashlib
import json
import os
import queue
import re
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

from src.config import log

RETENTION_DAYS = 14
CLEANUP_INTERVAL_SEC = 3600      # 最多每小时清一次（由采样循环调用 maybe_cleanup）
MAX_LIMIT = 500
DEFAULT_LIMIT = 100
_DB_LOG_ENABLED = os.environ.get("OBS_LOG_DB", "1").strip() not in ("0", "false", "no")

_LEVELS = ("WARNING", "ERROR", "CRITICAL")

# ---------------------------------------------------------------- 脱敏（入库前）

# 只打码"凭据类"，不动 IP/主机名：设备 IP 在设备页本来就对用户可见，打掉反而没法排障
_TOKEN_RES = [
    re.compile(r"\b(grape_|gcw_|rta_|tk_|ghp_|glpat-)[A-Za-z0-9_\-]{6,}"),
    re.compile(r"(?i)\b(password|passwd|pwd|secret|token|api[_-]?key)\s*[=:]\s*[\"']?([^\s\"',;]+)"),
    re.compile(r"://([^:/\s]+):([^@/\s]+)@"),                      # scheme://user:pass@host
    re.compile(r"(?i)(authorization:\s*(?:bearer|token)\s+)\S+"),
]


def redact(text: str) -> str:
    """把凭据类信息打码（幂等）。入库前调用，用户面查询时再兜一层。"""
    if not text:
        return ""
    s = str(text)
    s = _TOKEN_RES[0].sub(lambda m: m.group(1) + "***", s)
    s = _TOKEN_RES[1].sub(lambda m: f"{m.group(1)}=***", s)
    s = _TOKEN_RES[2].sub(lambda m: f"://{m.group(1)}:***@", s)
    s = _TOKEN_RES[3].sub(lambda m: m.group(1) + "***", s)
    return s[:4000]


def _dedup_key(source: str, ts: str, level: str, component: str, task_id: str, msg: str) -> str:
    raw = f"{source}|{ts}|{level}|{component}|{task_id}|{msg}"
    return hashlib.sha1(raw.encode("utf-8", "ignore")).hexdigest()[:24]


# ---------------------------------------------------------------- 上下文抽取

_TASK_RE = re.compile(r"\[task\s+(t_[0-9a-fA-F]{6,})\]")


def extract_context(msg: str) -> Tuple[str, str]:
    """从日志正文里抽 (task_id, repo_key)。

    worker 的日志行自带上下文，形如：
      `[task t_<task>] [seq=588|issue:#1|agent:coding|repo:gitcode:requiredsa/hello-html] ...`
    抽出来才能把这条日志挂到仓库上（否则用户在用户面看不到它）。
    """
    if not msg:
        return "", ""
    task_id = ""
    m = _TASK_RE.search(msg)
    if m:
        task_id = m.group(1)
    repo_key = ""
    m2 = re.search(r"repo:((?:gitcode|github):)?([A-Za-z0-9_.\-]+/[A-Za-z0-9_.\-]+)", msg)
    if m2:
        repo_key = (m2.group(1) or "gitcode:") + m2.group(2)
    return task_id, repo_key


# ---------------------------------------------------------------- 入库

def ingest(records: List[Dict[str, Any]], default_source: str = "master") -> int:
    """批量入库（幂等：同 dedup_key 跳过）。返回**新写入**行数。

    每条记录字段（缺省即空）：ts/level/component/source/trace_id/task_id/repo_key/runtime_id/msg/extra
    """
    if not records:
        return 0
    from src.session_manager import _is_mysql_backend, db, db_lock

    rows: List[Tuple[Any, ...]] = []
    seen = set()
    for r in records:
        lvl = str(r.get("level") or "").strip().upper()
        if lvl not in _LEVELS:
            continue                                    # 只落 W/E
        msg = redact(r.get("msg") or r.get("message") or "")
        if not msg.strip():
            continue
        ts = str(r.get("ts") or "").strip()
        if ts:
            # 统一成 ISO(UTC)：worker 上报的是本地时间字符串、主控是 ISO，混存会让
            # ORDER BY ts / since-until 范围过滤失真（实测踩到）。解析不了就用当前时间。
            try:
                from src.api.time_utils import normalize_event_time
                _n = normalize_event_time(ts)
                ts = _n if isinstance(_n, str) and _n else ts
            except Exception:
                pass
        else:
            ts = datetime.now(timezone.utc).isoformat()
        src = str(r.get("source") or default_source)
        comp = str(r.get("component") or "")[:64]
        task_id = str(r.get("task_id") or "")
        repo_key = str(r.get("repo_key") or "")
        if not task_id or not repo_key:                 # 正文里能抽就抽（worker 日志靠这个挂仓库）
            _t, _rk = extract_context(msg)
            task_id = task_id or _t
            repo_key = repo_key or _rk
        try:
            runtime_id = int(r.get("runtime_id") or 0)
        except (TypeError, ValueError):
            runtime_id = 0
        key = _dedup_key(src, ts, lvl, comp, task_id, msg)
        if key in seen:
            continue
        seen.add(key)
        extra = r.get("extra")
        if extra is not None and not isinstance(extra, str):
            try:
                extra = json.dumps(extra, ensure_ascii=False)[:4000]
            except Exception:
                extra = ""
        rows.append((key, ts, lvl, comp, src, str(r.get("trace_id") or ""), task_id,
                     repo_key, runtime_id, msg[:1024], extra or ""))

    if not rows:
        return 0
    ins = ("INSERT INTO obs_log (dedup_key, ts, level, component, source, trace_id, task_id,"
           " repo_key, runtime_id, msg, extra_json) VALUES (?,?,?,?,?,?,?,?,?,?,?)")
    if _is_mysql_backend():
        ins = ins.replace("INSERT INTO", "INSERT IGNORE INTO")
    else:
        ins = ins.replace("INSERT INTO", "INSERT OR IGNORE INTO")
    n = 0
    try:
        with db_lock:
            c = db.cursor()
            for row in rows:
                try:
                    c.execute(ins, row)
                    # INSERT IGNORE / OR IGNORE 命中唯一键时不报错也不插行 → 用 rowcount 计真实写入
                    n += int(c.rowcount if c.rowcount is not None else 1)
                except Exception:
                    pass                                # 单行冲突/超长不影响整批
            db.commit()
    except Exception as e:
        log.warning(f"obs_log ingest failed ({len(rows)} rows): {e}")
        return 0
    return n


# ---------------------------------------------------------------- 主控侧：日志 Handler

class ObsLogHandler:
    """把主控进程的 WARNING/ERROR 日志异步落到 obs_log（不阻塞请求路径）。

    - 有界队列，**队满即丢弃并计数**（宁可丢日志也不能拖慢主链路）
    - 后台 daemon 线程攒批写入（≤100 条/次，1s 或队满触发）
    - `OBS_LOG_DB=0` 整体关闭
    """

    def __init__(self, maxsize: int = 2000) -> None:
        self.q: "queue.Queue" = queue.Queue(maxsize=maxsize)
        self.dropped = 0
        self.written = 0
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def start(self) -> None:
        if self._thread:
            return
        self._thread = threading.Thread(target=self._run, name="obs-log-writer", daemon=True)
        self._thread.start()

    def emit_record(self, level: str, component: str, msg: str,
                    repo_key: str = "", task_id: str = "", trace_id: str = "",
                    ts: str = "", extra: Any = None) -> None:
        lvl = (level or "").upper()
        if lvl not in _LEVELS:
            return
        rec = {"ts": ts or datetime.now(timezone.utc).isoformat(), "level": lvl,
               "component": component, "source": "master", "msg": msg,
               "repo_key": repo_key, "task_id": task_id, "trace_id": trace_id, "extra": extra}
        try:
            self.q.put_nowait(rec)
        except queue.Full:
            self.dropped += 1

    def _run(self) -> None:
        batch: List[Dict[str, Any]] = []
        while not self._stop.is_set():
            try:
                rec = self.q.get(timeout=1.0)
                batch.append(rec)
                while len(batch) < 100:
                    try:
                        batch.append(self.q.get_nowait())
                    except queue.Empty:
                        break
            except queue.Empty:
                pass
            if batch:
                try:
                    self.written += ingest(batch, default_source="master")
                except Exception:
                    pass
                batch = []

    def stats(self) -> Dict[str, int]:
        return {"queued": self.q.qsize(), "written": self.written, "dropped": self.dropped}


_handler: Optional[ObsLogHandler] = None


def get_handler() -> ObsLogHandler:
    global _handler
    if _handler is None:
        _handler = ObsLogHandler()
    return _handler


class _LoggingBridge:
    """标准 logging.Handler：主控日志 → obs_log（只 W/E，只走队列，绝不在 emit 里抛）。"""

    def __init__(self) -> None:
        import logging as _logging

        class _H(_logging.Handler):
            def emit(_self, record):                      # noqa: N805
                try:
                    if record.levelno < _logging.WARNING:
                        return
                    h = get_handler()
                    h.emit_record(
                        level=record.levelname,
                        component=str(getattr(record, "component", "") or record.name or ""),
                        msg=record.getMessage(),
                        repo_key=str(getattr(record, "repo_key", "") or ""),
                        task_id=str(getattr(record, "task_id", "") or ""),
                        trace_id=str(getattr(record, "trace_id", "") or ""),
                    )
                except Exception:
                    return

        self.handler = _H()

    def attach(self) -> None:
        import logging as _logging
        root = _logging.getLogger()
        # 避免重复挂载（uvicorn reload / 多次启动）
        for h in root.handlers:
            if getattr(h, "_obs_log_bridge", False):
                return
        self.handler._obs_log_bridge = True                  # type: ignore[attr-defined]
        root.addHandler(self.handler)


def install_master_logging() -> Dict[str, Any]:
    """启动主控日志落库（env 可关）。返回状态，供启动日志打印。"""
    if not _DB_LOG_ENABLED:
        return {"enabled": False, "reason": "OBS_LOG_DB=0"}
    h = get_handler()
    h.start()
    _LoggingBridge().attach()
    return {"enabled": True, "queue": h.q.maxsize}


# ---------------------------------------------------------------- 用户面查询

def _visible_repo_keys(user: Dict[str, Any]) -> List[str]:
    from src.observability.overview import visible_repo_keys
    return visible_repo_keys(user)


def query(user: Dict[str, Any], level: str = "", source: str = "", repo: str = "",
          task_id: str = "", trace_id: str = "", q: str = "", since: str = "",
          until: str = "", limit: int = DEFAULT_LIMIT, offset: int = 0) -> Dict[str, Any]:
    """用户面日志检索：**只返回可见仓库的日志**（无 repo 维度的主控日志不返回）。

    越权 `repo` → 抛 RepoNotVisible（路由转 404）。
    """
    from src.observability.overview import RepoNotVisible
    from src.repo_key import to_repo_key
    from src.session_manager import db, db_lock

    visible = set(_visible_repo_keys(user))
    if repo:
        want = {to_repo_key(repo), to_repo_key(repo, "github")}
        hit = sorted(want & visible)
        if not hit:
            raise RepoNotVisible(repo)
        visible = set(hit)
    if not visible:
        return {"total": 0, "items": [], "facets": {"levels": {}, "sources": []}}

    limit = max(1, min(int(limit or DEFAULT_LIMIT), MAX_LIMIT))
    offset = max(0, int(offset or 0))
    ph = ",".join("?" * len(visible))
    where = [f"repo_key IN ({ph})"]
    params: List[Any] = sorted(visible)
    if level:
        lv = level.strip().upper()
        if lv in ("WARNING", "ERROR", "CRITICAL"):
            where.append("level=?")
            params.append(lv)
    if source:
        where.append("source=?")
        params.append(source)
    if task_id:
        where.append("task_id=?")
        params.append(task_id)
    if trace_id:
        where.append("trace_id=?")
        params.append(trace_id)
    if q:
        where.append("msg LIKE ?")
        params.append(f"%{q}%")
    if since:
        where.append("ts >= ?")
        params.append(since)
    if until:
        where.append("ts <= ?")
        params.append(until)
    wsql = " AND ".join(where)

    items: List[Dict[str, Any]] = []
    total = 0
    try:
        with db_lock:
            c = db.cursor()
            c.execute(f"SELECT COUNT(*) FROM obs_log WHERE {wsql}", tuple(params))
            total = int(c.fetchone()[0])
            c.execute(
                "SELECT id, ts, level, component, source, trace_id, task_id, repo_key, runtime_id, msg"
                f" FROM obs_log WHERE {wsql} ORDER BY ts DESC, id DESC LIMIT ? OFFSET ?",
                tuple(params + [limit, offset]))
            cols = [d[0] for d in c.description]
            for r in c.fetchall():
                d = dict(zip(cols, r))
                d["msg"] = redact(d.get("msg") or "")       # 兜底脱敏（老数据）
                items.append(d)
    except Exception as e:
        log.warning(f"obs_log query failed: {e}")

    facets = {"levels": {}, "sources": []}
    try:
        with db_lock:
            c = db.cursor()
            c.execute(f"SELECT level, COUNT(*) FROM obs_log WHERE {wsql} GROUP BY level", tuple(params))
            facets["levels"] = {r[0]: int(r[1]) for r in c.fetchall()}
            c.execute(f"SELECT DISTINCT source FROM obs_log WHERE {wsql} LIMIT 50", tuple(params))
            facets["sources"] = sorted([r[0] for r in c.fetchall() if r[0]])
    except Exception:
        pass
    return {"total": total, "items": items, "facets": facets,
            "limit": limit, "offset": offset, "scoped_repos": len(visible)}


# ---------------------------------------------------------------- 留存

_last_cleanup = 0.0


def cleanup(days: int = RETENTION_DAYS) -> int:
    """清理超过留存期的日志。返回删除行数。"""
    from src.session_manager import db, db_lock
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    try:
        with db_lock:
            c = db.cursor()
            c.execute("DELETE FROM obs_log WHERE ts < ?", (cutoff,))
            n = int(c.rowcount or 0)
            db.commit()
        return n
    except Exception as e:
        log.warning(f"obs_log cleanup failed: {e}")
        return 0


def maybe_cleanup(interval: int = CLEANUP_INTERVAL_SEC) -> int:
    """按间隔限流清理（由采样循环每次调用）。"""
    global _last_cleanup
    now = time.time()
    if now - _last_cleanup < interval:
        return 0
    _last_cleanup = now
    return cleanup()