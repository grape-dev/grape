"""Issue #1745: 跨场景转交后 GRAPE_SCENE_ID 以 Issue 归属场景为真值。

覆盖 Spec 验收：
1. 跨场景转交/重入：普通事件重派不再沿用事件行旧 scene_id 覆盖 Issue 归属场景；
   注入侧最终 GRAPE_SCENE_ID = issue 当前归属场景（真值）。
2. PR 订阅兼容（Issue #427）：PR 订阅伪 id / PR 事件类型 / 订阅事件标题仍保留订阅场景。
3. 执行前校正：任务快照陈旧时，reconcile_task_scene_env 按 Issue 归属场景覆盖；
   PR 订阅任务不校正（保留订阅语义）。
4. 不一致诊断包含注入值、真实值与可操作恢复指引。
"""
import os
import sys

ROOT = os.path.normpath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, ROOT)

import pytest  # noqa: E402

from src.session_manager import db, db_lock  # noqa: E402

RK = "gitcode:org/scene1745"
N = 17451
STALE = "stale-source-scene"
TARGET = "target-issue-scene"
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


def _seed_scene(scene_id, advanced_options="{}"):
    with db_lock:
        c = db.cursor()
        c.execute(
            "INSERT OR REPLACE INTO devops_pipeline_scene "
            "(scene_id, name, repo_key, nodes_json, is_active, advanced_options) "
            "VALUES (?,?,?,?,1,?)",
            (scene_id, scene_id, RK, "[]", advanced_options))
        db.commit()


def _seed_task(task_id, issue_id, event_type="a2a_event", event_seq=0):
    with db_lock:
        c = db.cursor()
        c.execute(
            "INSERT INTO agent_tasks (id, agent_session_id, runtime_id, prompt, "
            "issue_id, repo_key, agent_type, event_type, event_seq) "
            "VALUES (?, 'sess-row', 0, 'x', ?, ?, 'issue', ?, ?)",
            (task_id, issue_id, RK, event_type, event_seq))
        db.commit()


def _seed_event(seq, event_type="a2a_event", title="PR 订阅 QA"):
    with db_lock:
        c = db.cursor()
        c.execute(
            "INSERT INTO events (seq, repo_key, issue_id, event_type, title) "
            "VALUES (?,?,?,?,?)",
            (seq, RK, f"#{N}", event_type, title))
        db.commit()


def _seed_managed_repo(dev_scene):
    with db_lock:
        c = db.cursor()
        c.execute(
            "INSERT OR REPLACE INTO managed_repos "
            "(repo_key, repo_owner, repo_name, full_name, dev_scene) "
            "VALUES (?,?,?,?,?)",
            (RK, "org", "scene1745", "org/scene1745", dev_scene))
        db.commit()


def _plain_evt(scene_id=STALE):
    return {
        "seq": 1745001, "repo_key": RK, "issue_id": f"#{N}",
        "event_type": "a2a_event", "title": "A2A: -> issue", "scene_id": scene_id,
    }


# ===== 1. 重派场景 override：普通事件不沿用源场景 =====

def test_redispatch_drops_stale_scene_after_cross_scene_handoff():
    from src.api.routes.events import _redispatch_scene_override
    from src.api.event_pipeline import build_task_env
    _seed_issue(N, TARGET)
    evt = _plain_evt(STALE)
    assert _redispatch_scene_override(evt) == ""
    env = build_task_env(RK, "issue", f"#{N}", runtime_id=0, account="org/scene1745",
                         issue_number=f"#{N}",
                         scene_id_override=_redispatch_scene_override(evt))
    assert env["GRAPE_SCENE_ID"] == TARGET


def test_redispatch_keeps_scene_when_event_scene_matches_issue():
    from src.api.routes.events import _redispatch_scene_override
    _seed_issue(N, TARGET)
    evt = _plain_evt(TARGET)
    # 同场景事件不需要 override，按 issue_id 解析结果一致
    assert _redispatch_scene_override(evt) == ""


# ===== 2. PR 订阅兼容（#427） =====

def test_redispatch_keeps_pr_subscription_scene_for_pseudo_id():
    from src.api.routes.events import _redispatch_scene_override
    evt = dict(_plain_evt(SUB), issue_id=f"#pr_42_{SUB}", event_type="a2a_event")
    assert _redispatch_scene_override(evt) == SUB


def test_redispatch_keeps_pr_subscription_scene_for_pr_event_type():
    from src.api.routes.events import _redispatch_scene_override
    evt = dict(_plain_evt(SUB), event_type="pull_request_updated", title="PR updated")
    assert _redispatch_scene_override(evt) == SUB


def test_redispatch_keeps_pr_subscription_scene_for_subscription_title():
    from src.api.routes.events import _redispatch_scene_override
    evt = dict(_plain_evt(SUB), event_type="a2a_event", title=f"PR 订阅 QA: {SUB}")
    assert _redispatch_scene_override(evt) == SUB


def test_pr_subscription_env_still_uses_subscription_scene():
    from src.api.event_pipeline import build_task_env
    _seed_issue(N, TARGET)
    _seed_scene(SUB, '{"custom_fields": [{"name": "SUB_TOKEN", "value": "v"}]}')
    env = build_task_env(RK, "qa", f"#{N}", runtime_id=0, account="org/scene1745",
                         issue_number=f"#{N}", scene_id_override=SUB)
    assert env["GRAPE_SCENE_ID"] == SUB
    assert env["SUB_TOKEN"] == "v"


# ===== 3. 执行前快照校正 =====

def test_reconcile_task_scene_env_corrects_stale_snapshot():
    from src.api.event_pipeline import reconcile_task_scene_env
    _seed_issue(N, TARGET)
    _seed_task("t_1745_stale", f"#{N}", event_type="a2a_event")
    env = {"GRAPE_SCENE_ID": STALE, "KEEP": "1"}
    out = reconcile_task_scene_env("t_1745_stale", env)
    assert out == TARGET
    assert env["GRAPE_SCENE_ID"] == TARGET
    assert env["KEEP"] == "1"


def test_reconcile_task_scene_env_skips_pr_subscription_pseudo_id():
    from src.api.event_pipeline import reconcile_task_scene_env
    _seed_issue(N, TARGET)
    _seed_task("t_1745_pr_pseudo", f"#pr_42_{SUB}", event_type="a2a_event")
    env = {"GRAPE_SCENE_ID": SUB}
    out = reconcile_task_scene_env("t_1745_pr_pseudo", env)
    assert out == SUB
    assert env["GRAPE_SCENE_ID"] == SUB


def test_reconcile_task_scene_env_skips_pr_event_main_route():
    from src.api.event_pipeline import reconcile_task_scene_env
    _seed_issue(N, TARGET)
    _seed_task("t_1745_pr_event", f"#{N}", event_type="pull_request_updated")
    env = {"GRAPE_SCENE_ID": SUB}
    out = reconcile_task_scene_env("t_1745_pr_event", env)
    assert out == SUB
    assert env["GRAPE_SCENE_ID"] == SUB


def test_reconcile_task_scene_env_skips_pr_subscription_title_real_issue():
    """#427 既有形态：真实关联 Issue 号 + a2a_event + 「PR 订阅」标题，执行前不得校正。"""
    from src.api.event_pipeline import reconcile_task_scene_env
    _seed_issue(N, TARGET)
    _seed_event(1745002, event_type="a2a_event", title="PR 订阅 QA")
    _seed_task("t_1745_pr_title", f"#{N}", event_type="a2a_event", event_seq=1745002)
    env = {"GRAPE_SCENE_ID": SUB}
    out = reconcile_task_scene_env("t_1745_pr_title", env)
    assert out == SUB
    assert env["GRAPE_SCENE_ID"] == SUB


def test_is_pr_subscription_task_event_seq_title_path():
    from src.api.event_pipeline import _is_pr_subscription_task
    _seed_event(1745003, event_type="a2a_event", title="PR 订阅 Coding")
    assert _is_pr_subscription_task(f"#{N}", 1745003, "a2a_event") is True


def test_reconcile_task_scene_env_no_fallback_when_issue_unbound():
    """Issue 未绑定场景：不回退仓库默认场景，按「未选定=空串」口径清空陈旧快照。"""
    from src.api.event_pipeline import reconcile_task_scene_env
    _seed_managed_repo("repo-default-scene")
    _seed_task("t_1745_unbound", f"#{N}", event_type="issue_comment")
    env = {"GRAPE_SCENE_ID": STALE}
    out = reconcile_task_scene_env("t_1745_unbound", env)
    assert out == ""
    assert env["GRAPE_SCENE_ID"] == ""


def test_reconcile_task_scene_env_noop_when_already_truth():
    from src.api.event_pipeline import reconcile_task_scene_env
    _seed_issue(N, TARGET)
    _seed_task("t_1745_same", f"#{N}", event_type="issue_comment")
    env = {"GRAPE_SCENE_ID": TARGET}
    assert reconcile_task_scene_env("t_1745_same", env) == TARGET
    assert env["GRAPE_SCENE_ID"] == TARGET


# ===== 4. 可操作诊断 =====

def test_scene_env_mismatch_diagnostic_is_actionable():
    from src.api.event_pipeline import scene_env_mismatch_diagnostic
    msg = scene_env_mismatch_diagnostic(STALE, TARGET)
    assert STALE in msg
    assert TARGET in msg
    assert "issues detail" in msg
    assert "scenes detail" in msg
    assert "待确认" in msg


def test_reconcile_task_scene_env_logs_actionable_diagnostic(monkeypatch):
    """AC3：校正时运行期输出含双方取值与恢复指引的诊断（非仅死代码文案）。"""
    from src.api import event_pipeline
    msgs = []
    monkeypatch.setattr(event_pipeline.log, "info",
                        lambda msg, *a, **k: msgs.append(str(msg)))
    _seed_issue(N, TARGET)
    _seed_task("t_1745_diag", f"#{N}", event_type="a2a_event")
    env = {"GRAPE_SCENE_ID": STALE}
    assert event_pipeline.reconcile_task_scene_env("t_1745_diag", env) == TARGET
    assert any(STALE in m and TARGET in m and "issues detail" in m
               and "scenes detail" in m for m in msgs)
