"""Issue #3908: autopilot 合成 seq 在场景校正链路不得查 events / 产生 WARNING。

覆盖 Spec 验收：
1. `_pr_subscription_state()` 对 `event_seq >= AUTOPILOT_EVENT_SEQ_BASE` 直接判 False，
   不执行 events 查询、不打 `events row missing` / `events lookup failed` WARNING；
2. autopilot 合成回链键（`autopilot-<字母>`）在 `reconcile_task_scene_env()` 静默跳过，
   仅 debug 记录，不改写 `GRAPE_SCENE_ID`、不产生 WARNING；
3. autopilot 高位 seq + 真实 Issue 编号仍走正常场景校正（受损功能恢复）；
4. 普通任务真实 events 行缺失仍打 WARNING 并保留快照（#1745 语义不回退）。
"""
import os
import sys

ROOT = os.path.normpath(os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path.insert(0, ROOT)

import pytest  # noqa: E402

from src.kernel.issue_ident import (  # noqa: E402
    AUTOPILOT_EVENT_SEQ_BASE, is_autopilot_event_seq, is_autopilot_issue_ref,
)
from src.kernel.session_manager import db, db_lock  # noqa: E402

RK = "gitcode:org/scene3908"
N = 39081
STALE = "stale-source-scene"
TARGET = "target-issue-scene"
SNAPSHOT = "autopilot-snapshot-scene"


@pytest.fixture(autouse=True)
def _clean():
    _purge()
    yield
    _purge()


def _purge():
    with db_lock:
        c = db.cursor()
        c.execute("DELETE FROM issue WHERE repo_key=?", (RK,))
        c.execute("DELETE FROM managed_repos WHERE repo_key=?", (RK,))
        c.execute("DELETE FROM devops_pipeline_scene WHERE repo_key=?", (RK,))
        c.execute("DELETE FROM agent_tasks WHERE repo_key=?", (RK,))
        c.execute("DELETE FROM events WHERE repo_key=?", (RK,))
        db.commit()


def _seed_scene(scene_id):
    with db_lock:
        c = db.cursor()
        c.execute(
            "INSERT OR REPLACE INTO devops_pipeline_scene "
            "(scene_id, name, repo_key, nodes_json, is_active, advanced_options) "
            "VALUES (?,?,?,?,1,?)",
            (scene_id, scene_id, RK, "[]", "{}"))
        db.commit()


def _seed_issue(number, scene_id):
    with db_lock:
        c = db.cursor()
        c.execute(
            "INSERT INTO issue (user_id, assignee, repo_key, issue_number, scene_id) "
            "VALUES (0,'',?,?,?)",
            (RK, number, scene_id))
        db.commit()


def _seed_task(task_id, issue_id, event_type="autopilot_event", event_seq=0, prompt="x"):
    with db_lock:
        c = db.cursor()
        c.execute(
            "INSERT INTO agent_tasks (id, agent_session_id, runtime_id, prompt, "
            "issue_id, repo_key, agent_type, event_type, event_seq) "
            "VALUES (?, 'sess-row', 0, ?, ?, ?, 'issue', ?, ?)",
            (task_id, prompt, issue_id, RK, event_type, event_seq))
        db.commit()


def _capture(monkeypatch, ep):
    warnings = []
    debugs = []
    monkeypatch.setattr(ep.log, "warning",
                        lambda msg, *a, **k: warnings.append(str(msg)))
    monkeypatch.setattr(ep.log, "debug",
                        lambda msg, *a, **k: debugs.append(str(msg)))
    return warnings, debugs


def test_kernel_autopilot_predicates():
    assert is_autopilot_event_seq(AUTOPILOT_EVENT_SEQ_BASE)
    assert is_autopilot_event_seq(AUTOPILOT_EVENT_SEQ_BASE + 123)
    assert not is_autopilot_event_seq(AUTOPILOT_EVENT_SEQ_BASE - 1)
    assert not is_autopilot_event_seq(None)
    assert not is_autopilot_event_seq("not-a-seq")
    assert is_autopilot_issue_ref("autopilot-a")
    assert is_autopilot_issue_ref("autopilot-abc")
    assert not is_autopilot_issue_ref("autopilot-12")
    assert not is_autopilot_issue_ref(f"#{N}")


def test_pr_subscription_state_autopilot_seq_skips_events_query(monkeypatch):
    from src.domains.runtime.pipeline import event_pipeline as ep

    class _BoomCursor:
        def cursor(self):
            raise AssertionError("autopilot seq must not query events")

    monkeypatch.setattr(ep, "_rt_db", _BoomCursor())
    warnings, _ = _capture(monkeypatch, ep)
    state = ep._pr_subscription_state(
        f"#{N}", AUTOPILOT_EVENT_SEQ_BASE + 7, "autopilot_event")
    assert state is False
    assert warnings == []


def test_autopilot_issue_ref_skips_reconcile(monkeypatch):
    from src.domains.runtime.pipeline import event_pipeline as ep
    _seed_scene(TARGET)
    _seed_issue(N, TARGET)
    _seed_task("t_3908_auto_ref", "autopilot-abc",
               event_seq=AUTOPILOT_EVENT_SEQ_BASE + 7)
    env = {"GRAPE_SCENE_ID": SNAPSHOT, "KEEP": "1"}
    warnings, debugs = _capture(monkeypatch, ep)
    out = ep.reconcile_task_scene_env("t_3908_auto_ref", env)
    assert out == SNAPSHOT
    assert env["GRAPE_SCENE_ID"] == SNAPSHOT
    assert env["KEEP"] == "1"
    assert warnings == []
    assert any("scene_reconciled=false" in m and "reason=autopilot_source" in m
               for m in debugs)


def test_autopilot_seq_with_real_issue_reconciles(monkeypatch):
    from src.domains.runtime.pipeline import event_pipeline as ep
    _seed_scene(TARGET)
    _seed_issue(N, TARGET)
    _seed_task("t_3908_auto_real", f"#{N}", event_type="a2a_event",
               event_seq=AUTOPILOT_EVENT_SEQ_BASE + 7)
    env = {"GRAPE_SCENE_ID": STALE}
    warnings, _ = _capture(monkeypatch, ep)
    out = ep.reconcile_task_scene_env("t_3908_auto_real", env)
    assert out == TARGET
    assert env["GRAPE_SCENE_ID"] == TARGET
    assert warnings == []


def test_normal_task_missing_event_still_warns(monkeypatch):
    from src.domains.runtime.pipeline import event_pipeline as ep
    _seed_scene(TARGET)
    _seed_issue(N, TARGET)
    _seed_task("t_3908_missing", f"#{N}", event_type="a2a_event",
               event_seq=3908999)
    env = {"GRAPE_SCENE_ID": STALE}
    warnings, _ = _capture(monkeypatch, ep)
    out = ep.reconcile_task_scene_env("t_3908_missing", env)
    assert out == STALE
    assert env["GRAPE_SCENE_ID"] == STALE
    assert len(warnings) == 1
    assert "events row missing" in warnings[0]
    assert "t_3908_missing" in warnings[0]