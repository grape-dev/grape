"""Issue #2095（上游 #14）回归：存量库补种 event_action_filter 默认规则。

验收：空表 → 迁移补上默认规则；已有数据（含用户自定义）→ 不重灌、行数不变（幂等）。
"""
import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
os.environ.setdefault("DEVOPS_DB_PATH", str(ROOT / "data" / "test_sessions.db"))


@pytest.fixture()
def dbs():
    import src.api.db_setup as m
    yield m


def _count(dbs):
    with dbs.db_lock:
        c = dbs.db.cursor()
        c.execute("SELECT COUNT(*) FROM event_action_filter")
        return c.fetchone()[0]


def _reset(dbs):
    with dbs.db_lock:
        c = dbs.db.cursor()
        c.execute("DELETE FROM event_action_filter")
        dbs.db.commit()


def test_seeds_defaults_when_table_empty(dbs):
    _reset(dbs)
    assert _count(dbs) == 0
    dbs._fn_seed_event_action_filter_once()
    assert _count(dbs) > 0, "空表应补上默认事件过滤规则"
    with dbs.db_lock:
        c = dbs.db.cursor()
        c.execute("SELECT allow FROM event_action_filter "
                  "WHERE event_type='issue_updated' AND action='close'")
        row = c.fetchone()
    assert row and row[0] == 2, f"issue_updated/close 应为副作用-only(allow=2)，实得 {row}"


def test_idempotent_when_rules_already_present(dbs):
    _reset(dbs)
    dbs._fn_seed_event_action_filter_once()
    first = _count(dbs)
    dbs._fn_seed_event_action_filter_once()
    assert _count(dbs) == first, "已有规则时再次迁移不得改变行数"


def test_does_not_touch_custom_rules(dbs):
    _reset(dbs)
    with dbs.db_lock:
        c = dbs.db.cursor()
        c.execute("INSERT INTO event_action_filter "
                  "(event_type, action, allow, actor, require_changes, block_changes, "
                  " handle_side_effect, priority, enabled, created_at, updated_at) "
                  "VALUES ('custom_event','custom_action',1,'any','','','',0,1,datetime('now'),datetime('now'))")
        dbs.db.commit()
    before = _count(dbs)
    dbs._fn_seed_event_action_filter_once()
    assert _count(dbs) == before, "有自定义规则时不应重灌默认规则"
    _reset(dbs)
