"""Issue #3744: autopilot 合成 seq 无 events 行 → 场景校正入口按来源短路。

覆盖 Spec 验收：
1. autopilot 任务（issue_id 前缀 / 高位合成 seq）命中短路：不查 events、不告警、
   保留任务快照场景值，并留 scene_reconciled=false + reason 的 debug 依据；
2. 非 autopilot 真实缺行仍产生 1 条含 task_id/event_seq/repo_key 的可定位 WARNING；
3. PR 订阅（#427）与普通 Issue 场景校正行为不变；
4. `_pr_subscription_state` 的 None 保守语义不被修改。
"""
import os
import sys

ROOT = os.path.normpath(os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path.insert(0, ROOT)

import pytest  # noqa: E402

from src.kernel.session_manager import db, db_lock  # noqa: E402

RK = "gitcode:org/autopilot3744"
N = 37441
STALE = "stale-autopilot-source"
TARGET = "target-issue-scene"
SNAPSHOT = "autopilot-snapshot-scene"
SUB = "pr-subscription-scene"


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


# ===== 1. autopilot 来源短路 =====

def test_is_autopilot_task_recognizes_prefix_and_seq_namespace():
    from src.domains.runtime.pipeline.event_pipeline import _is_autopilot_task
    from src.domains.runtime.engine.scheduler import (
        _AUTOPILOT_EVENT_SEQ_BASE, _AUTOPILOT_ISSUE_ID_PREFIX,
    )
    assert _AUTOPILOT_ISSUE_ID_PREFIX == "autopilot-"
    assert _is_autopilot_task("autopilot-a", 0) is True
    assert _is_autopilot_task("", _AUTOPILOT_EVENT_SEQ_BASE) is True
    assert _is_autopilot_task(f"#{N}", _AUTOPILOT_EVENT_SEQ_BASE - 1) is False
    assert _is_autopilot_task(f"#{N}", 0) is False


def test_autopilot_prefix_short_circuits_without_events_query(monkeypatch):
    from src.domains.runtime.pipeline import event_pipeline as ep
    _seed_issue(N, TARGET)
    _seed_task("t_3744_auto_prefix", "autopilot-a", event_seq=0)
    env = {"GRAPE_SCENE_ID": SNAPSHOT, "KEEP": "1"}
    warnings, debugs = _capture(monkeypatch, ep)
    calls = []
    _orig = ep._pr_subscription_state

    def _spy(*a, **k):
        calls.append((a, k))
        return _orig(*a, **k)

    monkeypatch.setattr(ep, "_pr_subscription_state", _spy)
    out = ep.reconcile_task_scene_env("t_3744_auto_prefix", env)
    assert out == SNAPSHOT
    assert env["GRAPE_SCENE_ID"] == SNAPSHOT
    assert env["KEEP"] == "1"
    assert calls == []
    assert warnings == []
    assert any("autopilot" in m and "scene_reconciled=false" in m
               and "reason=autopilot_source" in m for m in debugs)


def test_autopilot_seq_namespace_short_circuits_without_events_query(monkeypatch):
    from src.domains.runtime.pipeline import event_pipeline as ep
    from src.domains.runtime.engine.scheduler import _AUTOPILOT_EVENT_SEQ_BASE
    _seed_issue(N, TARGET)
    _seed_task("t_3744_auto_seq", "",
               event_seq=_AUTOPILOT_EVENT_SEQ_BASE + 7)
    env = {"GRAPE_SCENE_ID": SNAPSHOT}
    warnings, debugs = _capture(monkeypatch, ep)
    out = ep.reconcile_task_scene_env("t_3744_auto_seq", env)
    assert out == SNAPSHOT
    assert env["GRAPE_SCENE_ID"] == SNAPSHOT
    assert warnings == []
    assert any("scene_reconciled=false" in m for m in debugs)


# ===== 2. 非 autopilot 缺行告警保留且可定位 =====

def test_non_autopilot_missing_event_warns_with_context(monkeypatch):
    from src.domains.runtime.pipeline import event_pipeline as ep
    _seed_issue(N, TARGET)
    _seed_task("t_3744_missing", f"#{N}", event_type="a2a_event",
               event_seq=3744998)
    env = {"GRAPE_SCENE_ID": STALE}
    warnings, _ = _capture(monkeypatch, ep)
    out = ep.reconcile_task_scene_env("t_3744_missing", env)
    assert out == STALE
    assert env["GRAPE_SCENE_ID"] == STALE
    assert len(warnings) == 1
    msg = warnings[0]
    assert "events row missing" in msg
    assert "t_3744_missing" in msg
    assert "3744998" in msg
    assert RK in msg
    assert "task_id=" in msg
    assert "event_seq=" in msg
    assert "repo_key=" in msg


# ===== 3. 回归：PR 订阅与普通 Issue 校正不变 =====

def test_pr_subscription_pseudo_id_still_skips_without_warning(monkeypatch):
    from src.domains.runtime.pipeline import event_pipeline as ep
    _seed_issue(N, TARGET)
    _seed_task("t_3744_pr", f"#pr_7_{SUB}", event_type="a2a_event", event_seq=0)
    env = {"GRAPE_SCENE_ID": SUB}
    warnings, _ = _capture(monkeypatch, ep)
    out = ep.reconcile_task_scene_env("t_3744_pr", env)
    assert out == SUB
    assert env["GRAPE_SCENE_ID"] == SUB
    assert warnings == []


def test_normal_issue_task_still_corrects_snapshot():
    from src.domains.runtime.pipeline.event_pipeline import reconcile_task_scene_env
    _seed_issue(N, TARGET)
    _seed_task("t_3744_normal", f"#{N}", event_type="issue_comment", event_seq=0)
    env = {"GRAPE_SCENE_ID": STALE}
    out = reconcile_task_scene_env("t_3744_normal", env)
    assert out == TARGET
    assert env["GRAPE_SCENE_ID"] == TARGET


# ===== 4. None 保守语义不变（#427 防回退） =====

def test_pr_subscription_state_none_semantics_unchanged():
    from src.domains.runtime.pipeline.event_pipeline import _pr_subscription_state
    assert _pr_subscription_state(f"#{N}", 3744999, "a2a_event") is None