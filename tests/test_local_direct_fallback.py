"""plugin 广播回落本机 direct 引擎测试（weekly-05 plugin-broadcast-local-direct-fallback）。

覆盖：
- UT-01 _pool_only_local_direct：全部在线健康引擎 cred=0 → 返回 rid
- UT-02 _pool_only_local_direct：存在 cred>0（远端 worker 池）→ None
- UT-03 _pool_only_local_direct：无在线引擎 → None
- UT-04 _enqueue_pipeline_task 广播 + 全本机 → 触发 start_direct_agent（self-run）
"""
import os, sys, json
from unittest.mock import patch, MagicMock

ROOT = os.path.join(os.path.dirname(__file__), "..")
os.environ.setdefault("DEVOPS_DB_PATH", os.path.join(ROOT, "data", "test_sessions.db"))

from src.session_manager import db, db_lock


# UT-01/02/03：_pool_only_local_direct 判定

def _mock_runtimes(rows):
    mock_cursor = MagicMock()
    mock_cursor.execute = MagicMock()
    mock_cursor.fetchall.return_value = rows
    mock_db = MagicMock()
    mock_db.cursor.return_value = mock_cursor
    return mock_db


# 辅助：显式建立测试引擎行（隔离，带清理）
_LOCAL_IDS = []


def _seed_runtimes(rows):
    """rows: [(id, credential_id, status, healthy)]——插入可控引擎状态。"""
    global _LOCAL_IDS
    _LOCAL_IDS = [r[0] for r in rows]
    with db_lock:
        c = db.cursor()
        for eid, cred, status, healthy in rows:
            c.execute("DELETE FROM agent_runtimes WHERE id=?", (eid,))
            c.execute(
                "INSERT INTO agent_runtimes (id, name, status, credential_id, healthy, busy_slots, max_concurrent, agent_tags) "
                "VALUES (?,?,?,?,?,0,4,'[\"all\"]')",
                (eid, "t%d" % eid, status, cred, healthy))
        db.commit()


def _cleanup_runtimes():
    global _LOCAL_IDS
    if _LOCAL_IDS:
        with db_lock:
            c = db.cursor()
            for eid in _LOCAL_IDS:
                c.execute("DELETE FROM agent_runtimes WHERE id=?", (eid,))
            db.commit()
        _LOCAL_IDS = []


# UT-01/02/03：_pool_only_local_direct 判定（显式 seed，隔离）

def test_pool_only_local_direct_all_local():
    from src.api import event_pipeline as ep
    try:
        _seed_runtimes([(2001, 0, "online", 1), (2002, 0, "online", 1)])
        rid = ep._pool_only_local_direct()
        assert rid in (2001, 2002)
    finally:
        _cleanup_runtimes()


def test_pool_only_local_direct_with_remote_returns_none():
    from src.api import event_pipeline as ep
    try:
        _seed_runtimes([(2003, 0, "online", 1), (2004, 99, "online", 1)])
        assert ep._pool_only_local_direct() is None
    finally:
        _cleanup_runtimes()


def test_pool_only_local_direct_no_online_none():
    from src.api import event_pipeline as ep
    try:
        _seed_runtimes([(2005, 0, "offline", 0), (2006, 0, "offline", 0)])
        assert ep._pool_only_local_direct() is None
    finally:
        _cleanup_runtimes()


# UT-04：_enqueue_pipeline_task 广播 + 全本机 → self-run 回落

def test_enqueue_broadcast_self_run_when_only_local():
    from src.api import event_pipeline as ep
    from src.api import event_archive as ea
    from src.runtime import service as svc
    from src.prompt_builder import build_devops_agent_prompt as _bdap

    fake_tid = "t_selftest"
    kwargs = {
        "repo_key": "gitcode:org/selftest", "seq": 9901, "agent_type": "vod",
        "backend": "opencode", "skill_name": "vod-cli",
        "evt": {"issue_id": "#9", "type": "issue_opened", "repository": "org/selftest",
                "payload": {}, "account": "u", "scene_id": ""},
        "skill_id": 0, "skill_path": "/s", "runtime_id": 0, "issue_id": "#9",
        "pool_engine_ids": "", "user_id": 0,
    }
    with patch.object(ep, "is_plugin_mode", return_value=True), \
         patch.object(ea, "init_event_json", return_value=None), \
         patch.object(_bdap, "__module__", "x"), \
         patch.object(ep, "prompt_ok" , create=True):
        # 直接 patch build_devops_agent_prompt 符号（函数内 from src.prompt_builder import）
        with patch("src.prompt_builder.build_devops_agent_prompt", return_value="P"), \
             patch.object(svc, "enqueue_task", return_value=fake_tid), \
             patch.object(ep, "_pool_only_local_direct", return_value=24), \
             patch.object(ep, "start_direct_agent", return_value=None) as m_sda, \
             patch.object(ep, "get_agent_workdir", return_value="/ws"), \
             patch("src.api.session_store.get_or_create_agent_session", return_value={"id": "es1"}), \
             patch("src.api.session_store.get_or_create_issue_session", return_value={"id": "is1", "runtime_session_id": ""}), \
             patch("src.api.event_store.update_event_config", return_value=None):
            ep._enqueue_pipeline_task(**kwargs)
    assert m_sda.called
    call_arg = m_sda.call_args[0][1]
    assert call_arg[1] == 9901
    assert call_arg[6] == 24
    assert call_arg[7] == fake_tid