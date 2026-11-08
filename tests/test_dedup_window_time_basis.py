"""去重近窗（60s）时间口径回归测试（Issue #1487 衍生）。

背景：`event_pipeline` 去重②原先用 `_utc_now_str(-60)`（Python **UTC** 字符串）与
`events.created_at` 做字符串比较，而 `events.created_at` 的默认值在 MySQL 下是
`NOW()`（+08 本地墙钟）、在 SQLite 下是 `datetime('now')`（UTC）。于是：

- MySQL 部署（生产）：cutoff 比 created_at 早 8 小时 → 60s 窗口实际放大到 8h+，
  同一 (repo, issue, agent, event_type) 的事件在 8 小时内全部被判重，
  PR 评论（note_hook_pr）等 webhook 类事件被静默丢弃
  （event_skipped.reason = dedup:recent_<type>_60s_for_<agent>）。
- SQLite（测试环境）：两侧都是 UTC → 测试永远绿，缺陷不漏出。

修复：cutoff 统一由 `time_utils.db_now_str()` 生成（与 parse_dt /
normalize_event_time 同一口径的正反向）。
"""
import os
import sys
from datetime import datetime, timedelta, timezone

ROOT = os.path.join(os.path.dirname(__file__), "..")
os.environ.setdefault("DEVOPS_DB_PATH", os.path.join(ROOT, "data", "test_sessions.db"))

from src.api.db_setup import init_database
from src.session_manager import db, db_lock
from src.api import time_utils
from src.api.event_store import find_recent_same_event

init_database()

REPO = "gitcode:org/tz-window"
ISSUE = "#7777"
AGENT = "qa"
ETYPE = "note_hook_pr"
FMT = "%Y-%m-%d %H:%M:%S"


def _clear():
    with db_lock:
        c = db.cursor()
        c.execute("DELETE FROM events WHERE repo_key=?", (REPO,))
        db.commit()


def _insert_at(created_at: str) -> int:
    with db_lock:
        c = db.cursor()
        c.execute(
            "INSERT INTO events (repo_key, issue_id, event_type, agent_type, created_at) "
            "VALUES (?,?,?,?,?)",
            (REPO, ISSUE, ETYPE, AGENT, created_at))
        seq = c.lastrowid or 0
        db.commit()
        return seq


def test_db_now_str_follows_storage_clock_sqlite(monkeypatch):
    """SQLite 口径：db_now_str 与 datetime('now') 同为 UTC 墙钟。"""
    monkeypatch.setattr(time_utils, "_db_backend", lambda: "sqlite")
    got = datetime.strptime(time_utils.db_now_str(), FMT)
    expected = datetime.now(timezone.utc).replace(tzinfo=None)
    assert abs((got - expected).total_seconds()) < 10


def test_db_now_str_follows_storage_clock_mysql(monkeypatch):
    """MySQL 口径：db_now_str 返回 +08 本地墙钟（与 NOW() 写入的 created_at 同源）。"""
    monkeypatch.setattr(time_utils, "_db_backend", lambda: "mysql")
    got = datetime.strptime(time_utils.db_now_str(), FMT)
    expected = datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(hours=8)
    assert abs((got - expected).total_seconds()) < 10


def test_db_now_str_offset_is_exact(monkeypatch):
    """-60s 偏移必须精确落在 60 秒前（不因时区换算漂移）。"""
    for backend in ("sqlite", "mysql"):
        monkeypatch.setattr(time_utils, "_db_backend", lambda b=backend: b)
        now = datetime.strptime(time_utils.db_now_str(), FMT)
        past = datetime.strptime(time_utils.db_now_str(-60), FMT)
        assert abs((now - past).total_seconds() - 60) < 2


def test_recent_window_does_not_over_dedup_with_mysql_clock(monkeypatch):
    """回归主用例：MySQL 墙钟下，26 分钟前的事件不构成「60s 内重复」。

    旧实现（UTC cutoff）会把它误判为近窗内（8h 漂移），导致真实评论被吞；
    修复后必须返回 0（不判重），而 10 秒前的重复投递仍必须判重。
    """
    _clear()
    monkeypatch.setattr(time_utils, "_db_backend", lambda: "mysql")
    local_now = datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(hours=8)

    _insert_at((local_now - timedelta(minutes=26)).strftime(FMT))
    assert find_recent_same_event(REPO, ISSUE, AGENT, ETYPE, 60) == 0, \
        "26 分钟前的事件被误判为 60s 内重复（时区口径回归）"

    seq = _insert_at((local_now - timedelta(seconds=10)).strftime(FMT))
    assert find_recent_same_event(REPO, ISSUE, AGENT, ETYPE, 60) == seq, \
        "10 秒前的重复投递应当判重"
    _clear()


def test_recent_window_sqlite_behavior_unchanged(monkeypatch):
    """SQLite 口径行为不变：60s 内判重、61 分钟前放行。"""
    _clear()
    monkeypatch.setattr(time_utils, "_db_backend", lambda: "sqlite")
    utc_now = datetime.now(timezone.utc).replace(tzinfo=None)
    _insert_at((utc_now - timedelta(minutes=61)).strftime(FMT))
    assert find_recent_same_event(REPO, ISSUE, AGENT, ETYPE, 60) == 0
    seq = _insert_at((utc_now - timedelta(seconds=5)).strftime(FMT))
    assert find_recent_same_event(REPO, ISSUE, AGENT, ETYPE, 60) == seq
    _clear()


def test_event_pipeline_dedup_uses_db_clock_helper():
    """源码护栏：去重② 不得再退回 Python UTC 字符串 cutoff。"""
    path = os.path.join(ROOT, "src", "api", "event_pipeline.py")
    with open(path, encoding="utf-8") as f:
        src = f.read()
    assert "find_recent_same_event(" in src
    # 旧实现的调用点（inline SQL + Python UTC cutoff）必须已不存在
    assert "event_type, _utc_now_str(-60)" not in src
    assert "AND event_type=? AND created_at >= ? ORDER BY seq DESC LIMIT 1" not in src