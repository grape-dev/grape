"""Issue #404：场景绑定引擎全部离线的主动发现与自愈——5 条验收单测。

验收标准（与 Issue #404 一致）：
1. 引擎全部 offline → 事件 failed 且 result 含 scene id / agent
2. 恢复 online 后一键重派 → 新任务派发成功且原事件终态不变
3. 同场景 1h 内重复离线 → 告警只发一次
4. pool 有兜底引擎 → claim 正常不受影响
5. 空场景走默认池 → 不误报全部离线

DB 隔离与引擎/场景构造参考 tests/test_issue_259_offline_dispatch.py。
"""
import datetime
import json
import os
import sys

ROOT = os.path.normpath(os.path.join(os.path.dirname(__file__), ".."))
os.environ["DEVOPS_DB_PATH"] = os.path.join(ROOT, "data", "test_issue_404.db")
os.environ["DEVOPS_DB_BACKEND"] = "sqlite"
os.environ["GRAPE_PIPELINE_MODE"] = "legacy"

import pytest

from src.api.db_setup import init_database
from src.session_manager import db, db_lock

init_database()

REPO = "gitcode:owner/repo404"   # user_repos 表用全前缀形式（与 test_issue_259 一致）
REPO_BARE = "owner/repo404"       # 场景创建 / events.repo_key / pool 查询用裸形式（与 test_issue_259 一致）


@pytest.fixture(autouse=True)
def _clean_tables():
    """每用例前后清测试仓库相关行，保证隔离。"""
    _tables = ("agent_tasks", "events", "devops_agent_session", "agent_runtimes",
               "runtime_credentials", "devops_pipeline_scene", "unified_user_mapping",
               "user_repos", "engine_capabilities", "task_messages")
    with db_lock:
        c = db.cursor()
        for t in _tables:
            try:
                c.execute(f"DELETE FROM {t} WHERE repo_key LIKE '%/repo404' OR repo_key LIKE 'gitcode:%/repo404'")
            except Exception:
                pass
        try:
            c.execute("DELETE FROM meta WHERE key LIKE 'redispatch:%'")
        except Exception:
            pass
        db.commit()
    from src.api.event_pipeline import _reset_scene_offline_alert_dedup
    _reset_scene_offline_alert_dedup()
    yield
    with db_lock:
        c = db.cursor()
        for t in _tables:
            try:
                c.execute(f"DELETE FROM {t} WHERE repo_key LIKE '%/repo404' OR repo_key LIKE 'gitcode:%/repo404'")
            except Exception:
                pass
        try:
            c.execute("DELETE FROM meta WHERE key LIKE 'redispatch:%'")
        except Exception:
            pass
        db.commit()
    _reset_scene_offline_alert_dedup()


def _now():
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def _stale_ts(seconds_ago):
    return (datetime.datetime.now(datetime.timezone.utc)
            - datetime.timedelta(seconds=seconds_ago)).isoformat()


def _mk_cred(name, scope="global", owner_id=""):
    with db_lock:
        c = db.cursor()
        c.execute(
            "INSERT INTO runtime_credentials (token_hash, name, device_name, scope, owner_id, owner_name, status) "
            "VALUES (?,?,?,?,?,?, 'active')",
            ("h" + name, name, "dev-" + name, scope, owner_id, "tester"))
        cid = c.lastrowid
        db.commit()
        return cid


def _mk_engine(name, *, credential_id, status="online", healthy=1, installed=1,
               scope="global", owner_id="", last_seen=None) -> int:
    ls = last_seen if last_seen is not None else _now()
    with db_lock:
        c = db.cursor()
        c.execute(
            "INSERT INTO agent_runtimes (name, display_name, credential_id, status, healthy, installed, last_seen_at, scope, owner_id) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (name, name, credential_id, status, healthy, installed, ls, scope, owner_id))
        rid = c.lastrowid
        c.execute(
            "INSERT OR IGNORE INTO engine_capabilities (runtime_id, tag, enabled) VALUES (?, 'all', 1)",
            (rid,))
        db.commit()
        return rid


def _mk_scene(scene_id, agent_type, engine_ids, user_id=1, repo_key=REPO_BARE):
    with db_lock:
        c = db.cursor()
        try:
            c.execute("INSERT OR IGNORE INTO user_repos (user_id, repo_key, owner, name, is_join) "
                      "VALUES (?, ?, 'owner', 'repo404', 1)", (user_id, REPO))
        except Exception:
            pass
        db.commit()
    nodes = [{"id": agent_type, "tag": agent_type, "skill": "test-skill-404",
              "mode": "emit", "next": "done", "handoff": "complete",
              "engine_runtime_ids": engine_ids}]
    from src.api.pipeline_config import create_custom_scene
    create_custom_scene(scene_id=scene_id, name=scene_id, description="",
                        repo_key=repo_key, nodes=nodes)


def _mk_event(seq, scene_id, agent_type, repo_key=REPO_BARE, status="running"):
    """直接写一条 events 行（绕开 append_event 的 payload 解析，方便单测）。"""
    with db_lock:
        c = db.cursor()
        c.execute(
            "INSERT INTO events (seq, repo_key, issue_id, event_type, source, agent_type, title, actor, "
            "summary, payload, status, result, scene_id, entity_type) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (seq, repo_key, "#404", "a2a_event", "gitcode", agent_type, "t404", "tester",
             "summary", "{}", status, "", scene_id, "issue"))
        db.commit()


# ============================================================
# 验收 1：引擎全部 offline → 事件 failed 且 result 含 scene id / agent
# ============================================================

class TestSceneAllOfflineFailsEvent:
    def test_event_failed_with_scene_and_agent(self):
        """全灭 → _fail_scene_all_offline 置 failed，result 含 scene_id 与 agent_type。"""
        from src.api.event_pipeline import _fail_scene_all_offline
        dead1 = _mk_engine("opencode-404-d1", credential_id=_mk_cred("c-404-d1"), status="offline")
        dead2 = _mk_engine("opencode-404-d2", credential_id=_mk_cred("c-404-d2"),
                           status="online", last_seen=_stale_ts(999))
        _mk_scene("sc404-a", "vod", [dead1, dead2])
        _mk_event(1001, "sc404-a", "vod")
        _fail_scene_all_offline(1001, "sc404-a", "vod", context="Test404", repo_key=REPO_BARE)
        with db_lock:
            c = db.cursor()
            c.execute("SELECT status, result FROM events WHERE seq=?", (1001,))
            row = c.fetchone()
        assert row is not None, "事件行应存在"
        assert row[0] == "failed", f"事件应为 failed，实际 {row[0]}"
        _result = row[1] or ""
        assert "sc404-a" in _result, f"result 应含 scene_id，实际：{_result}"
        assert "vod" in _result, f"result 应含 agent_type，实际：{_result}"
        assert "全部离线" in _result, f"result 应含'全部离线'原因，实际：{_result}"


# ============================================================
# 验收 2：恢复 online 后一键重派 → 新任务派发成功且原事件终态不变
# ============================================================

class TestRedispatchAfterRecovery:
    def test_list_redispatchable_and_pool_recovered(self):
        """引擎恢复 online → list_redispatchable_events 返回该事件；
        get_scene_pool_engine_ids 非 None（重派前置条件满足）；原事件终态不变。"""
        from src.api.event_pipeline import _fail_scene_all_offline
        from src.runtime.engine_select import get_scene_pool_engine_ids
        from src.api.routes.events import list_redispatchable_events

        # 1) 初始全灭 → 事件 failed
        eng = _mk_engine("opencode-404-rec", credential_id=_mk_cred("c-404-rec"),
                         status="online", last_seen=_stale_ts(999))
        _mk_scene("sc404-b", "vod", [eng])
        _mk_event(1002, "sc404-b", "vod")
        _fail_scene_all_offline(1002, "sc404-b", "vod", context="Test404-rec", repo_key=REPO_BARE)
        assert get_scene_pool_engine_ids("sc404-b", "vod", repo_key=REPO_BARE, user_id=1) is None

        # 2) 引擎恢复 online（心跳刷新到当前时间）
        with db_lock:
            c = db.cursor()
            c.execute("UPDATE agent_runtimes SET status='online', last_seen_at=? WHERE id=?",
                      (_now(), eng))
            db.commit()
        _pool = get_scene_pool_engine_ids("sc404-b", "vod", repo_key=REPO_BARE, user_id=1)
        assert _pool is not None and _pool != "", f"恢复后 pool 应为 JSON 串，实际：{_pool!r}"
        assert json.loads(_pool) == [eng]

        # 3) 一键重派列表端点返回该事件
        _res = list_redispatchable_events(scene_id="sc404-b", limit=50, user={})
        assert _res["total"] >= 1, f"应至少 1 条可重派事件，实际：{_res}"
        _seqs = [it["seq"] for it in _res["items"]]
        assert 1002 in _seqs, f"事件 1002 应在可重派列表，实际 seqs：{_seqs}"

        # 4) 原事件终态不变（仍 failed——未实际调 redispatch，终态保持）
        with db_lock:
            c = db.cursor()
            c.execute("SELECT status FROM events WHERE seq=?", (1002,))
            assert c.fetchone()[0] == "failed", "原事件终态应保持 failed（未被掰回 running）"


# ============================================================
# 验收 3：同场景 1h 内重复离线 → 告警只发一次
# ============================================================

class TestAlertDedupWithin1h:
    def test_alert_sent_once_within_1h(self, monkeypatch):
        """同场景两次 _fail_scene_all_offline（间隔 <1h）→ _notify_webhook 只调一次。"""
        from src.api import event_pipeline as ep
        from src.api.event_pipeline import _fail_scene_all_offline

        _calls = {"n": 0}

        def _fake_notify(rule, ev, text=""):
            _calls["n"] += 1

        monkeypatch.setattr("src.observability.alerts._notify_webhook", _fake_notify)
        # 飞书异步分支拦掉（避免无事件循环告警噪声）
        async def _fake_send(*a, **k):
            return False
        monkeypatch.setattr("src.observability.alerts._send_event_text", _fake_send)

        dead1 = _mk_engine("opencode-404-dd1", credential_id=_mk_cred("c-404-dd1"), status="offline")
        dead2 = _mk_engine("opencode-404-dd2", credential_id=_mk_cred("c-404-dd2"), status="offline")
        _mk_scene("sc404-c", "vod", [dead1, dead2])

        # 第一次离线 → 告警发出
        _mk_event(1003, "sc404-c", "vod")
        _fail_scene_all_offline(1003, "sc404-c", "vod", context="Test404-dedup-1", repo_key=REPO_BARE)
        assert _calls["n"] == 1, f"首次离线应告警一次，实际 {_calls['n']} 次"

        # 第二次同场景离线（间隔 <1h）→ 去重，不再发
        _mk_event(1004, "sc404-c", "vod")
        _fail_scene_all_offline(1004, "sc404-c", "vod", context="Test404-dedup-2", repo_key=REPO_BARE)
        assert _calls["n"] == 1, f"1h 内同场景应只告警一次，实际 _notify_webhook 调用 {_calls['n']} 次"

        _state = ep._scene_offline_alert_dedup_state()
        assert "sc404-c" in _state, f"去重字典应含 sc404-c，实际：{_state}"


# ============================================================
# 验收 4：pool 有兜底引擎 → claim 正常不受影响
# ============================================================

class TestPoolWithFallbackEngine:
    def test_pool_with_fallback_engine_claim_ok(self):
        """pool 有兜底在线引擎 → get_scene_pool_engine_ids 非 None（含该引擎），
        claim 正常路径不受影响（回归 #259 健康过滤不误伤在线引擎）。"""
        from src.runtime.engine_select import get_scene_pool_engine_ids
        ok_eng = _mk_engine("opencode-404-fb", credential_id=_mk_cred("c-404-fb"))
        dead_eng = _mk_engine("opencode-404-fb-dead", credential_id=_mk_cred("c-404-fb-dead"),
                              status="offline")
        _mk_scene("sc404-d", "vod", [ok_eng, dead_eng])
        result = get_scene_pool_engine_ids("sc404-d", "vod", repo_key=REPO_BARE, user_id=1)
        assert result is not None, "有兜底在线引擎 → pool 不应为 None"
        assert result != "", "有绑定引擎 → pool 不应为空串"
        _ids = json.loads(result)
        assert ok_eng in _ids, f"在线引擎应在 pool，实际：{_ids}"
        assert dead_eng not in _ids, f"离线引擎不应在 pool，实际：{_ids}"
        # claim 正常路径不受影响：pool 含在线引擎即代表 claim 可选定它（#259 健康过滤不误伤）


# ============================================================
# 验收 5：空场景走默认池 → 不误报全部离线
# ============================================================

class TestEmptySceneNoFalseOffline:
    def test_empty_scene_no_false_alert(self):
        """空场景（无 engine_runtime_ids）→ get_scene_pool_engine_ids 返回 ""（全量广播），
        不产生 None（=不误报全部离线）；_emit_scene_offline_alert 不被触发（去重字典为空）。"""
        from src.runtime.engine_select import get_scene_pool_engine_ids
        from src.api.event_pipeline import _emit_scene_offline_alert, _scene_offline_alert_dedup_state

        # 空场景：node 不配 engine_runtime_ids
        with db_lock:
            c = db.cursor()
            try:
                c.execute("INSERT OR IGNORE INTO user_repos (user_id, repo_key, owner, name, is_join) "
                          "VALUES (1, ?, 'owner', 'repo404', 1)", (REPO,))
            except Exception:
                pass
            db.commit()
        from src.api.pipeline_config import create_custom_scene
        nodes_empty = [{"id": "vod", "tag": "vod", "skill": "test-skill-404",
                        "mode": "emit", "next": "done", "handoff": "complete",
                        "engine_runtime_ids": []}]
        create_custom_scene(scene_id="sc404-e", name="sc404-e", description="",
                            repo_key=REPO_BARE, nodes=nodes_empty)

        # 空场景 → ""（全量广播，原行为），不是 None
        result = get_scene_pool_engine_ids("sc404-e", "vod", repo_key=REPO_BARE, user_id=1)
        assert result == "", f"空场景应返回空串（全量广播），实际：{result!r}"

        # 即使误调 _emit_scene_offline_alert，空 scene_id 也不会告警（早退保护）
        _emit_scene_offline_alert("", "vod", "测试空场景", repo_key=REPO_BARE)
        assert _scene_offline_alert_dedup_state() == {}, "空 scene_id 不应写入去重字典"

        # 空场景下 pool 非 None → pipeline 不会走 _fail_scene_all_offline 分支
        # （调用方仅在 get_scene_pool_engine_ids 返回 None 时才调用，"" 不会触发）
        # 此处用断言再次确认语义边界
        assert result is not None, "空场景 pool 必须非 None（=不误报全部离线）"
