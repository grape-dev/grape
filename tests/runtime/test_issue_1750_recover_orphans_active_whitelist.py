"""Issue #1750：recover_orphans 增加 worker 自报活跃白名单 + 收紧回收时机。

覆盖验收 A1–A6：
- A1 白名单内静默任务 → skipped_active，attempts 不变、仍 running（优先于年龄闸门/窗口）
- A2 真孤儿（启动注册、空白名单）→ 首次调用即回收
- A3 白名单命中 → runtime_id 不变（不会转池被异机认领，同机不双跑）
- A4 attempts 单次 +1（SQLite 后端参数化更新，不出现 +2）
- A5 周期重注册不回收 / rid 变化带白名单回收 / 启动空白名单回收
- A6 E2E：跨一次周期重注册后仍 running，最终 completed
"""
import asyncio
import os
import sys
import threading
from unittest.mock import patch

import pytest

ROOT = os.path.join(os.path.dirname(__file__), "..", "..")
sys.path.insert(0, ROOT)


@pytest.fixture()
def setup_db(monkeypatch):
    import sqlite3
    from src.domains.runtime.engine import service as svc
    import src.domains.runtime.routes.runtime as rt

    test_db = sqlite3.connect(":memory:", check_same_thread=False)
    test_db.executescript("""
    CREATE TABLE agent_tasks (
        id TEXT PRIMARY KEY, agent_session_id TEXT DEFAULT '', runtime_id INTEGER DEFAULT 0,
        event_seq INTEGER, issue_id TEXT DEFAULT '', repo_key TEXT DEFAULT '',
        agent_type TEXT DEFAULT '', skill TEXT DEFAULT '', skill_id INTEGER DEFAULT 0,
        prompt TEXT DEFAULT '', cwd TEXT DEFAULT '', runtime_session_id TEXT DEFAULT '',
        event_type TEXT DEFAULT '', account TEXT DEFAULT '', user_id INTEGER DEFAULT 0,
        repo_gitcode_token TEXT DEFAULT '', env_extra TEXT DEFAULT '{}',
        skill_files TEXT DEFAULT '{}', server_url TEXT DEFAULT '',
        issue_session_id TEXT DEFAULT '',
        attempts INTEGER DEFAULT 0, pool_agent_type TEXT DEFAULT '',
        affinity_runtime_id INTEGER DEFAULT 0, pool_tag TEXT DEFAULT '',
        pool_engine_ids TEXT DEFAULT '',
        status TEXT DEFAULT 'queued', stream_ended INTEGER DEFAULT 0,
        exit_code INTEGER, result_text TEXT DEFAULT '', errors TEXT DEFAULT '',
        started_at TEXT, completed_at TEXT, created_at TEXT
    );
    CREATE TABLE agent_runtimes (
        id INTEGER PRIMARY KEY, name TEXT, status TEXT DEFAULT 'offline',
        healthy INTEGER DEFAULT 0, busy_slots INTEGER DEFAULT 0, max_concurrent INTEGER DEFAULT 4
    );
    CREATE TABLE task_messages (
        id INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT, created_at TEXT, content TEXT
    );
    """)
    monkeypatch.setattr(svc, "db", test_db)
    monkeypatch.setattr(svc, "db_lock", threading.Lock())
    monkeypatch.setattr(rt, "db", test_db)
    monkeypatch.setattr(rt, "db_lock", svc.db_lock)
    import src.session_manager as _sm
    monkeypatch.setattr(_sm, "db", test_db)
    monkeypatch.setattr(_sm, "db_lock", svc.db_lock)
    yield test_db
    test_db.close()


def _seed(c, tid, status="running", runtime_id=700, attempts=0,
          messages_ago=None, started_seconds_ago=600, created_seconds_ago=600):
    import datetime as dt
    now = dt.datetime.now(dt.timezone.utc)
    created = (now - dt.timedelta(seconds=created_seconds_ago)).isoformat()
    started = (now - dt.timedelta(seconds=started_seconds_ago)).isoformat()
    c.execute("INSERT INTO agent_tasks (id, status, runtime_id, attempts, agent_type, "
              "pool_agent_type, agent_session_id, created_at, started_at) "
              "VALUES (?,?,?,?,?,'',?,?,?)",
              (tid, status, runtime_id, attempts, "coding", "s-" + tid, created, started))
    if messages_ago is not None:
        ts = (now - dt.timedelta(seconds=messages_ago)).isoformat()
        c.execute("INSERT INTO task_messages (task_id, created_at, content) VALUES (?,?,?)",
                  (tid, ts, "alive"))


def _row(c, tid):
    return c.execute("SELECT status, attempts, runtime_id FROM agent_tasks WHERE id=?", (tid,)).fetchone()


def _call(runtime_id, active_task_ids=None):
    from src.domains.runtime.routes.runtime import runtime_recover_orphans, RecoverOrphansBody
    body = None if active_task_ids is None else RecoverOrphansBody(active_task_ids=active_task_ids)
    return runtime_recover_orphans(runtime_id, body=body, credential_id=0)


# ===== A1：白名单内静默任务跳过 =====

def test_a1_silent_task_in_whitelist_skipped_active(setup_db):
    """静默超窗口（无 task_messages、超宽限期）但白名单内 → skipped_active，状态/attempts 不变。"""
    c = setup_db
    c.execute("INSERT INTO agent_runtimes (id, name, status, healthy, busy_slots) VALUES (700,'codex','online',1,1)")
    _seed(c, "t_silent", started_seconds_ago=3600, messages_ago=None, attempts=1)
    c.commit()

    resp = _call(700, active_task_ids=["t_silent"])

    assert resp["skipped_active"] == ["t_silent"]
    assert resp["skipped_active_reason"]["t_silent"] == "active_whitelist"
    assert resp["recovered"] == [] and resp["force_failed"] == []
    status, attempts, rid = _row(c, "t_silent")
    assert status == "running" and attempts == 1 and rid == 700, "白名单命中不得改动任务"


def test_a1_whitelist_priority_over_grace_gate(setup_db):
    """白名单优先于年龄闸门：30s 前刚派发但已自报活跃 → skipped_active 而非 skipped_young。"""
    c = setup_db
    c.execute("INSERT INTO agent_runtimes (id, name, status, healthy, busy_slots) VALUES (701,'codex','online',1,1)")
    _seed(c, "t_young_active", runtime_id=701, started_seconds_ago=30, created_seconds_ago=40)
    c.commit()

    resp = _call(701, active_task_ids=["t_young_active"])

    assert resp["skipped_active"] == ["t_young_active"]
    assert resp["skipped_young"] == [], "白名单应优先于年龄闸门"
    assert _row(c, "t_young_active")[0] == "running"


# ===== A2：真孤儿启动空白名单回收 =====

def test_a2_true_orphan_recovered_with_empty_whitelist(setup_db):
    """启动注册（空白名单）时，真孤儿首次调用即回收（status→queued, attempts+1）。"""
    c = setup_db
    c.execute("INSERT INTO agent_runtimes (id, name, status, healthy, busy_slots) VALUES (702,'codex','online',1,1)")
    _seed(c, "t_orphan", runtime_id=702, started_seconds_ago=3600, messages_ago=None)
    c.commit()

    resp = _call(702, active_task_ids=None)

    assert resp["recovered"] == ["t_orphan"]
    status, attempts, rid = _row(c, "t_orphan")
    assert status == "queued" and attempts == 1 and rid == 0


# ===== A3：白名单命中同机不双跑 =====

def test_a3_whitelisted_task_not_pooled(setup_db):
    """白名单命中 → runtime_id 保持原值（不归零转池），不会被异机认领造成双跑。"""
    c = setup_db
    c.execute("INSERT INTO agent_runtimes (id, name, status, healthy, busy_slots) VALUES (703,'codex','online',1,3)")
    _seed(c, "t_double", runtime_id=703, started_seconds_ago=7200, messages_ago=None)
    c.commit()

    _call(703, active_task_ids=["t_double"])
    _call(703, active_task_ids=["t_double"])

    status, attempts, rid = _row(c, "t_double")
    assert (status, attempts, rid) == ("running", 0, 703)


# ===== A4：attempts 单次 +1 =====

def test_a4_attempts_single_increment(setup_db):
    """每次回收 attempts 仅 +1（参数化更新，不出现 SQLite/MySQL 分叉的 +2）。"""
    c = setup_db
    c.execute("INSERT INTO agent_runtimes (id, name, status, healthy, busy_slots) VALUES (704,'codex','online',1,1)")
    _seed(c, "t_att", runtime_id=704, started_seconds_ago=3600, messages_ago=None)
    c.commit()

    _call(704)
    assert _row(c, "t_att")[1] == 1, "第一次回收 attempts 应 +1"

    # 回炉后任务重新可回收：started_at 被清空，再置回 running 且超龄
    import datetime as dt
    c.execute("UPDATE agent_tasks SET status='running', started_at=?, runtime_id=704 WHERE id='t_att'",
              ((dt.datetime.now(dt.timezone.utc) - dt.timedelta(seconds=3600)).isoformat(),))
    c.commit()
    _call(704)
    assert _row(c, "t_att")[1] == 2, "第二次回收应再 +1（单次递增，不跳 +2）"


# ===== A5：回收时机（周期/rid 变化/启动） =====

class _FakeClient:
    def __init__(self, ids):
        self.ids = ids
        self.recovered = []
        self.register_calls = 0

    async def reset_session(self):
        pass

    async def register(self, runtimes, device_name="", cli_version="", local_skills=None):
        self.register_calls += 1
        return self.ids, "probe-url"

    async def recover_orphans(self, runtime_id, active_task_ids=None):
        self.recovered.append((runtime_id, active_task_ids))


def test_a5_periodic_reregister_does_not_recover(setup_db):
    """周期重注册（recover_orphans=False）只刷新 id，不调用 recover_orphans。"""
    from src.domains.runtime.engine import agent as agent_mod
    client = _FakeClient([801, 802])
    with patch.object(agent_mod, "_discover_local_runtimes", return_value=[]):
        result = asyncio.run(agent_mod._reregister(
            client, prev_installed=[], device_name="dev", recover_orphans=False))
    assert result[0] == [801, 802]
    assert client.register_calls == 1
    assert client.recovered == [], "周期重注册不得回收"


def test_a5_startup_empty_whitelist_recovers(setup_db):
    """启动注册（默认 recover_orphans=True）→ 空白名单回收上次遗留。"""
    from src.domains.runtime.engine import agent as agent_mod
    client = _FakeClient([801])
    with patch.object(agent_mod, "_discover_local_runtimes", return_value=[]):
        result = asyncio.run(agent_mod._reregister(client, prev_installed=[], device_name="dev"))
    assert result[0] == [801]
    assert client.recovered == [(801, None)], "启动回收应空白名单"


def test_a5_rid_change_recovers_with_whitelist(setup_db):
    """rid 集合变化 → 对每个新 rid 携带 active_tasks 白名单回收。"""
    from src.domains.runtime.engine import agent as agent_mod
    client = _FakeClient([901, 902])
    changed = asyncio.run(agent_mod._recover_orphans_on_rid_change(
        client, prev_ids=[901], new_ids=[901, 902], active_tasks={"t_live": object()}))
    assert changed is True
    assert client.recovered == [(901, ["t_live"]), (902, ["t_live"])]


def test_a5_rid_unchanged_does_not_recover(setup_db):
    """rid 集合不变 → 不回收。"""
    from src.domains.runtime.engine import agent as agent_mod
    client = _FakeClient([901])
    changed = asyncio.run(agent_mod._recover_orphans_on_rid_change(
        client, prev_ids=[901], new_ids=[901], active_tasks={"t_live": object()}))
    assert changed is False
    assert client.recovered == []


def test_a5_heartbeat_failure_reregister_keeps_whitelist(setup_db):
    """心跳失败重注册是同进程重连：不得空白名单无条件回收；rid 变化时携带白名单。

    审查修复（PR #1096 Required）：该路径此前沿用默认 recover_orphans=True 空白名单
    无条件回收，本机 active_tasks 中的静默型长任务会被误回收转池导致双跑。
    """
    from unittest.mock import AsyncMock
    from src.runtime import agent as agent_mod

    async def _drive(coro, duration=0.25):
        t = asyncio.ensure_future(coro)
        await asyncio.sleep(duration)
        t.cancel()
        try:
            await t
        except (asyncio.CancelledError, Exception):
            pass

    client = AsyncMock()
    client.get_tags.return_value = []
    client.claim_by_tag.return_value = None
    client.claim.return_value = None
    client.heartbeat.return_value = None  # 网络错误 → 连续失败触发重注册
    with patch.object(agent_mod, "_reregister",
                      new=AsyncMock(return_value=([2001], {}, []))) as m_rr:
        asyncio.run(_drive(agent_mod._run_loop(
            client, [1001], poll_interval=0.01, heartbeat_interval=0.01,
            max_concurrent=4, _test_active_tasks={"t_hb_live": "sid"})))
    assert m_rr.await_args.kwargs.get("recover_orphans") is False, \
        "心跳失败重注册不得默认空白名单无条件回收"
    client.recover_orphans.assert_awaited_with(2001, active_task_ids=["t_hb_live"])


# ===== A6：E2E 跨周期仍 running 最终 completed =====

def test_a6_silent_task_survives_periodic_cycle_then_completes(setup_db):
    """静默长任务跨一次周期重注册：不回收 → 白名单调用 skipped_active → 最终 completed。"""
    from src.domains.runtime.engine import agent as agent_mod
    c = setup_db
    c.execute("INSERT INTO agent_runtimes (id, name, status, healthy, busy_slots) VALUES (1001,'codex','online',1,1)")
    _seed(c, "t_e2e", runtime_id=1001, started_seconds_ago=5000, messages_ago=None)
    c.commit()

    # 一轮周期重注册：rid 集合不变 → 不回收
    client = _FakeClient([1001])
    asyncio.run(agent_mod._recover_orphans_on_rid_change(
        client, prev_ids=[1001], new_ids=[1001], active_tasks={"t_e2e": "sid"}))
    assert client.recovered == []
    assert _row(c, "t_e2e")[0] == "running"

    # worker 上报白名单 → 服务端 skipped_active
    resp = _call(1001, active_task_ids=["t_e2e"])
    assert resp["skipped_active"] == ["t_e2e"]
    assert _row(c, "t_e2e") == ("running", 0, 1001)

    # 任务正常完成
    c.execute("UPDATE agent_tasks SET status='completed' WHERE id='t_e2e'")
    c.commit()
    assert _row(c, "t_e2e")[0] == "completed"


# ===== client 请求体透传 =====

class _FakeResp:
    def __init__(self, status=200, body=None):
        self.status = status
        self._body = body if body is not None else {}

    async def json(self):
        return self._body

    async def text(self):
        return ""


class _Ctx:
    def __init__(self, resp):
        self._resp = resp

    async def __aenter__(self):
        return self._resp

    async def __aexit__(self, *a):
        return False


def _client():
    from unittest.mock import AsyncMock, MagicMock
    from src.domains.runtime.engine.client import RuntimeClient
    c = RuntimeClient("https://srv", "tok", timeout=1)
    c._session = AsyncMock()
    c._session.closed = False
    req = MagicMock()
    req.return_value = _Ctx(_FakeResp())
    c._session.request = req
    return c


def test_client_recover_orphans_default_body_empty():
    c = _client()
    asyncio.run(c.recover_orphans(5))
    assert c._session.request.call_args.kwargs["json"] == {}


def test_client_recover_orphans_passes_whitelist():
    c = _client()
    asyncio.run(c.recover_orphans(5, active_task_ids=["t1", "t2"]))
    assert c._session.request.call_args.kwargs["json"] == {"active_task_ids": ["t1", "t2"]}
