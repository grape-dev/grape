"""plugin 广播回落本机 direct 引擎测试（weekly-05 plugin-broadcast-local-direct-fallback + weekly-07 修复）。

覆盖：
- UT-01 _pool_only_local_direct：全部本机 cred=0（global）→ 返回 rid
- UT-02 _pool_only_local_direct：存在远端 worker（cred>0）但仍有本机 global direct → 返回本机 rid（方案 A 修复）
- UT-03 _pool_only_local_direct：无在线/无本机 global direct → None
- UT-04 _enqueue_pipeline_task 广播 + 本机 global → 触发 start_direct_agent（self-run）
- UT-05 _pool_only_local_direct：仅 repo-scope 远端 worker（无本机 global direct）→ None
- UT-06 _pool_only_local_direct：本机 local + global 均 cred=0 → 优先 global
- UT-07 _pool_only_local_direct：多枚本机 global → 优先级 opencode > codearts
- UT-08 _pool_only_local_direct：repo 远端 worker 与 global direct 混合 → 仍返回本机 global（时序无关）
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


# 辅助：显式建立测试引擎行（隔离，带清理；支持 name/scope）
_LOCAL_IDS = []


def _seed_runtimes(rows):
    """rows: (id, credential_id, status, healthy[, name[, scope]])——插入可控引擎状态。

    隔离策略（共享 test_sessions.db，多测试文件并发 seed agent_runtimes）：
    - id 用高位段（70xxx）避免与其它测试（test_cleanup 用 24/25、claim_by_tag 用 76/999/173）撞 id；
    - seed 前按 name 全清（含 scope 任意），避免与同 name+scope+credential_id 行撞
      UNIQUE(name, scope, credential_id, owner_id, host_name)。
    - name 缺省用 `lf-{eid}`，与全库真实引擎名（opencode/codearts/hermes）错开；仅
      优先级用例（UT-07）显式传真实名以验证 ORDER BY，seed 前会清掉同名残留。
    """
    global _LOCAL_IDS
    _LOCAL_IDS = [r[0] for r in rows]
    names = [r[4] if len(r) > 4 else "lf-%d" % r[0] for r in rows]
    with db_lock:
        c = db.cursor()
        _ph = ",".join("?" * len(names))
        c.execute(f"DELETE FROM agent_runtimes WHERE name IN ({_ph})", tuple(names))
        for r in rows:
            eid, cred, status, healthy = r[0], r[1], r[2], r[3]
            name = r[4] if len(r) > 4 else "lf-%d" % eid
            scope = r[5] if len(r) > 5 else "global"
            c.execute(
                "INSERT INTO agent_runtimes (id, name, status, credential_id, healthy, busy_slots, max_concurrent, agent_tags, scope) "
                "VALUES (?,?,?,?,?,0,4,'[\"all\"]',?)",
                (eid, name, status, cred, healthy, scope))
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
        _seed_runtimes([(2001, 0, "online", 1, "opencode", "global"),
                        (2002, 0, "online", 1, "codearts", "global")])
        rid = ep._pool_only_local_direct()
        assert rid in (2001, 2002)
    finally:
        _cleanup_runtimes()


def test_pool_only_local_direct_with_remote_returns_local():
    """方案 A 修复：存在 repo-scope 远端 worker（cred>0）时，只要还有本机 global direct，仍 self-run。"""
    from src.api import event_pipeline as ep
    try:
        _seed_runtimes([(2003, 0, "online", 1, "opencode", "global"),
                        (2004, 99, "online", 1, "opencode", "repo")])
        rid = ep._pool_only_local_direct()
        assert rid == 2003
    finally:
        _cleanup_runtimes()


def test_pool_only_local_direct_no_online_none():
    from src.api import event_pipeline as ep
    try:
        _seed_runtimes([(2005, 0, "offline", 0, "opencode", "global"),
                        (2006, 0, "offline", 0, "codearts", "global")])
        assert ep._pool_only_local_direct() is None
    finally:
        _cleanup_runtimes()


# UT-05：仅 repo-scope 远端 worker（无本机 global direct）→ None

def test_pool_only_local_direct_only_remote_repo_worker_none():
    from src.api import event_pipeline as ep
    try:
        _seed_runtimes([(2011, 99, "online", 1, "opencode", "repo")])
        assert ep._pool_only_local_direct() is None
    finally:
        _cleanup_runtimes()


def test_pool_only_local_direct_remote_bot_local_repo_none():
    """本机 repo-scope（cred=0）但无 global direct → 不 self-run（本机 global 才作为部署方自跑）。"""
    from src.api import event_pipeline as ep
    try:
        _seed_runtimes([(2012, 0, "online", 1, "opencode", "repo")])
        assert ep._pool_only_local_direct() is None
    finally:
        _cleanup_runtimes()


# UT-06：本机 local + global 均 cred=0 → 优先 global

def test_pool_only_local_direct_global_beats_local():
    from src.api import event_pipeline as ep
    try:
        _seed_runtimes([(2013, 0, "online", 1, "opencode", "local"),
                        (2014, 0, "online", 1, "codearts", "global")])
        assert ep._pool_only_local_direct() == 2014
    finally:
        _cleanup_runtimes()


# UT-07：多枚本机 global → 优先级 opencode > codearts

def test_pool_only_local_direct_priority_opencode_first():
    from src.api import event_pipeline as ep
    try:
        _seed_runtimes([(2015, 0, "online", 1, "codearts", "global"),
                        (2016, 0, "online", 1, "opencode", "global")])
        assert ep._pool_only_local_direct() == 2016
    finally:
        _cleanup_runtimes()


# UT-08：repo 远端 worker 与 global direct 混合（含时序差异）→ 仍返回本机 global

def test_pool_only_local_direct_mixed_remotes_still_local():
    from src.api import event_pipeline as ep
    try:
        _seed_runtimes([(2017, 99, "online", 1, "hermes", "repo"),
                        (2018, 99, "online", 1, "opencode", "repo"),
                        (2019, 0, "online", 1, "opencode", "global")])
        assert ep._pool_only_local_direct() == 2019
    finally:
        _cleanup_runtimes()


# UT-04：_enqueue_pipeline_task 广播 + 本机 global → self-run 回落

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