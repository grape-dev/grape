"""Issue #9495 回归：pending_since 为「无时区 ISO-8601（UTC 墙钟）」时不得被立即判死。

现场：executor 写 gmtime() 的 "2026-11-06T07:11:13"（无偏移），health.parse_dt 在 MySQL
后端按 CST(+08:00) 解释 → epoch 比真实早 8 小时 → 刚置 pending_self_report 就超过 5400s
被判 timeout_failed（事件 #9495）。

修复：写入端补 +00:00；解析端 _parse_pending_since 对含 "T" 的无偏移值按 UTC 处理
（旧空格格式仍走 parse_dt 的历史 CST 口径）。
"""
import os
import sys
import time as _t
from datetime import datetime, timedelta, timezone

import pytest

ROOT = os.path.join(os.path.dirname(__file__), "..", "..")
sys.path.insert(0, ROOT)
os.environ.setdefault("DEVOPS_DB_PATH", os.path.join(ROOT, "data", "test_sessions.db"))

from src.domains.runtime.engine.scheduler import (  # noqa: E402
    _parse_pending_since,
    _sweep_pending_self_report_timeouts,
)


def _seed_task(tid: str, seq: int, pending_since: str) -> None:
    from src.kernel import session_manager as sm
    with sm.db_lock:
        c = sm.db.cursor()
        c.execute("DELETE FROM agent_tasks WHERE id=?", (tid,))
        c.execute(
            "INSERT INTO agent_tasks (id, agent_session_id, runtime_id, event_seq, repo_key, "
            "status, skill, started_at, pending_since, prompt, agent_type, cwd) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (tid, "sess-" + tid, 9418, seq, "gitcode:tz/test9495",
             "pending_self_report", "vod", pending_since, pending_since,
             "test prompt", "opencode", "/tmp"))
        sm.db.commit()


def _clean() -> None:
    from src.kernel import session_manager as sm
    with sm.db_lock:
        c = sm.db.cursor()
        for t in ("agent_tasks", "events"):
            try:
                c.execute(f"DELETE FROM {t} WHERE id LIKE 't_i9495%' OR repo_key='gitcode:tz/test9495'")
            except Exception:
                pass
        sm.db.commit()


@pytest.fixture(autouse=True)
def _iso():
    _clean()
    yield
    _clean()


def test_parse_naive_iso_treated_as_utc():
    """无偏移但含 T 的值 → 按 UTC 解析（与真实 UTC 差 < 2s）。"""
    now = datetime.now(timezone.utc).replace(microsecond=0)
    naive = now.strftime("%Y-%m-%dT%H:%M:%S")
    parsed = _parse_pending_since(naive)
    assert parsed is not None
    assert abs(parsed - now.timestamp()) < 2


def test_parse_offset_and_legacy_space():
    """带偏移照常；空格旧格式与 parse_dt 完全一致（MySQL=CST / SQLite=UTC 后端口径）。"""
    from src.domains.runtime.engine.health import parse_dt

    now = datetime.now(timezone.utc).replace(microsecond=0)
    aware = now.strftime("%Y-%m-%dT%H:%M:%S+00:00")
    parsed_aware = _parse_pending_since(aware)
    assert parsed_aware is not None
    assert abs(parsed_aware - now.timestamp()) < 2

    legacy = now.strftime("%Y-%m-%d %H:%M:%S")
    parsed_legacy = _parse_pending_since(legacy)
    assert parsed_legacy is not None
    assert parsed_legacy == parse_dt(legacy)


def test_naive_pending_not_killed_immediately():
    """核心回归：无偏移 UTC 值刚写入 → 扫描不得判死（#9495 现象）。"""
    tid = "t_i9495_naive"
    naive = _t.strftime("%Y-%m-%dT%H:%M:%S", _t.gmtime())
    _seed_task(tid, 9495, naive)

    _sweep_pending_self_report_timeouts()

    from src.kernel import session_manager as sm
    with sm.db_lock:
        row = sm.db.cursor().execute(
            "SELECT status FROM agent_tasks WHERE id=?", (tid,)).fetchone()
    assert row and row[0] == "pending_self_report", f"不应判死，实际 status={row[0] if row else None}"


def test_true_timeout_still_reclaimed():
    """真超时（2 小时前，带偏移）照常回收。"""
    tid = "t_i9495_old"
    old = (datetime.now(timezone.utc) - timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M:%S+00:00")
    _seed_task(tid, 9496, old)

    _sweep_pending_self_report_timeouts()

    from src.kernel import session_manager as sm
    with sm.db_lock:
        row = sm.db.cursor().execute(
            "SELECT status FROM agent_tasks WHERE id=?", (tid,)).fetchone()
    assert row and row[0] == "timeout_failed"