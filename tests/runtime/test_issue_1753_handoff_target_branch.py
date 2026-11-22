"""Cross-pipeline target branch baseline tests (Issue #1753).

Covers:
- target scene work_branch is carried into carried_context.target_branch
- stale source-session branch is overridden with audit trail (target_branch_replaced)
- same-baseline handoff keeps value without extra rewrite
- target scene without work_branch leaves carried_context unchanged
- A2A handoff context declares the accepting baseline
- issue Agent prompt gains baseline consistency guidance
"""
import os
import sys
import json
import logging

ROOT = os.path.normpath(os.path.join(os.path.dirname(__file__), "..", ".."))
os.environ["DEVOPS_DB_PATH"] = os.path.join(ROOT, "data", "test_issue_1753_handoff_target_branch.db")
os.environ["DEVOPS_DB_BACKEND"] = "sqlite"
for _k in ("DEVOPS_DB_HOST", "DEVOPS_DB_USER", "DEVOPS_DB_PASSWORD", "DEVOPS_DB_NAME", "DEVOPS_DB_PORT"):
    os.environ.pop(_k, None)

import pytest

from src.api.db_setup import init_database
from src.kernel.session_manager import db, db_lock
from src.domains.runtime.routes.webhook import _cross_pipeline_handoff
from src.domains.runtime.engine import repogit as repogit_mod
from src.api.session_store import (
    TARGET_BRANCH_SOURCE_SCENE_WORK_BRANCH,
    TARGET_BRANCH_SOURCE_UNCONFIRMED_NO_SCENE_WORK_BRANCH,
    get_scene_work_branch,
)

from src.kernel.prompt_builder import _build_cross_pipeline_baseline, build_devops_agent_prompt

init_database()

REPO = "gitcode:org/test-1753"


@pytest.fixture(autouse=True)
def _clean():
    with db_lock:
        c = db.cursor()
        for _t in ("issue", "events", "devops_pipeline_scene", "event_active_guard"):
            c.execute(f"DELETE FROM {_t} WHERE repo_key=?", (REPO,))
        db.commit()
    yield
    with db_lock:
        c = db.cursor()
        for _t in ("issue", "events", "devops_pipeline_scene", "event_active_guard"):
            c.execute(f"DELETE FROM {_t} WHERE repo_key=?", (REPO,))
        db.commit()


@pytest.fixture(autouse=True)
def _branch_list_unverifiable(monkeypatch):
    """默认分支列表不可核实：交办校验保持场景原值，单测不触网。

    覆盖分支列表的用例在自身内再次 setattr 覆盖本默认桩。
    """
    repogit_mod.invalidate_branch_list_cache()
    monkeypatch.setattr(
        repogit_mod, "list_repo_branches",
        lambda repo_key, token="", refresh=False, remote_only=False: (None, ""))


def _set_issue(repo_key, number, scene_id="", assignee=""):
    with db_lock:
        c = db.cursor()
        c.execute(
            "INSERT INTO issue (user_id, assignee, repo_key, issue_number, status, scene_id) "
            "VALUES (0, ?, ?, ?, 'processing', ?)",
            (assignee, repo_key, number, scene_id))
        db.commit()


def _set_scene(repo_key, scene_id, nodes, advanced_options=None):
    with db_lock:
        c = db.cursor()
        c.execute(
            "INSERT INTO devops_pipeline_scene "
            "(scene_id, name, repo_key, nodes_json, is_active, advanced_options) "
            "VALUES (?,?,?,?,1,?)",
            (scene_id, scene_id, repo_key, json.dumps(nodes),
             json.dumps(advanced_options) if advanced_options is not None else "{}"))
        db.commit()


def _update_scene_options(repo_key, scene_id, advanced_options):
    with db_lock:
        c = db.cursor()
        c.execute(
            "UPDATE devops_pipeline_scene SET advanced_options=? "
            "WHERE scene_id=? AND repo_key=?",
            (json.dumps(advanced_options), scene_id, repo_key))
        db.commit()


def _persisted_cc(number):
    with db_lock:
        c = db.cursor()
        c.execute("SELECT carried_context FROM issue WHERE repo_key=? AND issue_number=?", (REPO, number))
        row = c.fetchone()
    return json.loads(row[0]) if row and row[0] else {}


def _latest_event_payload(number, event_type):
    with db_lock:
        c = db.cursor()
        c.execute(
            "SELECT payload FROM events WHERE repo_key=? AND issue_id=? AND event_type=? "
            "ORDER BY seq DESC LIMIT 1",
            (REPO, f"#{number}", event_type))
        row = c.fetchone()
    return json.loads(row[0]) if row and row[0] else {}


def test_get_scene_work_branch():
    _set_scene(REPO, "target-scene", [{"id": "vod"}], {"work_branch": "weekly-mys"})
    assert get_scene_work_branch(REPO, scene_id="target-scene") == "weekly-mys"
    assert get_scene_work_branch(REPO, scene_id="missing-scene") == ""


def test_target_branch_injected_and_persisted():
    _set_issue(REPO, 200, "source-scene")
    _set_scene(REPO, "target-scene", [{"id": "vod"}], {"work_branch": "weekly-mys"})
    carried = {"source_issue_number": 200, "work_branch": "weekly-10"}
    result = _cross_pipeline_handoff(
        REPO, "200", "target-scene", "vod", carried, "", None,
        "handoff", "org/test-1753", "org/test-1753")
    assert result["ok"] is True

    cc = _persisted_cc(200)
    assert cc["target_branch"] == "weekly-mys"
    assert cc["target_branch_source"] == TARGET_BRANCH_SOURCE_SCENE_WORK_BRANCH

    audit = _latest_event_payload(200, "cross_scene_handoff")
    assert audit["carried_context"]["target_branch"] == "weekly-mys"

    a2a = _latest_event_payload(200, "a2a_event")
    assert a2a["carried_context"]["target_branch"] == "weekly-mys"
    assert "weekly-mys" in a2a["context"]
    assert "weekly-mys" in a2a["handoff_baseline_note"]


def test_stale_source_branch_overridden_with_audit_trail():
    _set_issue(REPO, 201, "source-scene")
    _set_scene(REPO, "target-scene", [{"id": "vod"}], {"work_branch": "weekly-mys"})
    carried = {"source_issue_number": 201, "target_branch": "weekly-10"}
    result = _cross_pipeline_handoff(
        REPO, "201", "target-scene", "vod", carried, "", None,
        "handoff", "org/test-1753", "org/test-1753")
    assert result["ok"] is True

    cc = _persisted_cc(201)
    assert cc["target_branch"] == "weekly-mys"
    assert cc["target_branch_replaced"] == "weekly-10"

    a2a = _latest_event_payload(201, "a2a_event")
    assert "weekly-10" in a2a["context"]
    assert "weekly-mys" in a2a["context"]


def test_same_baseline_no_replaced_marker():
    _set_issue(REPO, 202, "source-scene")
    _set_scene(REPO, "target-scene", [{"id": "vod"}], {"work_branch": "weekly-mys"})
    carried = {"source_issue_number": 202, "target_branch": "weekly-mys"}
    result = _cross_pipeline_handoff(
        REPO, "202", "target-scene", "vod", carried, "", None,
        "handoff", "org/test-1753", "org/test-1753")
    assert result["ok"] is True

    cc = _persisted_cc(202)
    assert cc["target_branch"] == "weekly-mys"
    assert "target_branch_replaced" not in cc


def test_target_scene_without_work_branch_unchanged(monkeypatch):
    _set_issue(REPO, 203, "source-scene")
    _set_scene(REPO, "target-scene", [{"id": "vod"}])
    carried = {"source_issue_number": 203, "target_branch": "weekly-10"}
    result = _cross_pipeline_handoff(
        REPO, "203", "target-scene", "vod", carried, "", None,
        "handoff", "org/test-1753", "org/test-1753")
    assert result["ok"] is True

    cc = _persisted_cc(203)
    assert cc["target_branch"] == "weekly-10"
    assert "target_branch_source" not in cc

    # 复审要求：承接场景未配置 work_branch 时，残留的源会话旧分支不得被 prompt 宣称为权威基线。
    monkeypatch.delenv("GRAPE_WORK_BRANCH", raising=False)
    guidance = _build_cross_pipeline_baseline(REPO, "#203")
    assert "weekly-10" in guidance
    assert "UNCONFIRMED" in guidance
    assert "(authoritative baseline of the accepting pipeline" not in guidance

    # 复审要求：A2A 上下文同样不得把旧分支宣称为承接流水线运行时权威基线。
    a2a = _latest_event_payload(203, "a2a_event")
    assert "weekly-10" in a2a["context"]
    assert "UNCONFIRMED" in a2a["context"]
    assert "取自承接流水线运行时 GRAPE_WORK_BRANCH" not in a2a["context"]


def test_multi_hop_without_work_branch_invalidates_stale_source(monkeypatch):
    """复审（多跳链）：A→B 写入权威标记后，B→C（C 无 work_branch）须失效该标记。

    C 的 carried_context 不得残留 B 的 target_scene_work_branch 权威来源，
    prompt / A2A 上下文均按 UNCONFIRMED 处理。
    """
    _set_issue(REPO, 209, "scene-b")
    _set_scene(REPO, "target-scene", [{"id": "vod"}])
    carried = {
        "source_issue_number": 209,
        "target_branch": "weekly-mys",
        "target_branch_source": TARGET_BRANCH_SOURCE_SCENE_WORK_BRANCH,
        "target_branch_replaced": "weekly-10",
    }
    result = _cross_pipeline_handoff(
        REPO, "209", "target-scene", "vod", carried, "", None,
        "handoff", "org/test-1753", "org/test-1753")
    assert result["ok"] is True

    cc = _persisted_cc(209)
    assert cc["target_branch"] == "weekly-mys"
    assert cc["target_branch_source"] == TARGET_BRANCH_SOURCE_UNCONFIRMED_NO_SCENE_WORK_BRANCH
    # 留痕口径统一：失效后上一跳的 replaced 标记同步清除，避免继续宣称「已刷新」。
    assert "target_branch_replaced" not in cc

    monkeypatch.delenv("GRAPE_WORK_BRANCH", raising=False)
    guidance = _build_cross_pipeline_baseline(REPO, "#209")
    assert "UNCONFIRMED" in guidance
    assert f"target_branch_source=`{TARGET_BRANCH_SOURCE_UNCONFIRMED_NO_SCENE_WORK_BRANCH}`" in guidance
    assert "(authoritative baseline of the accepting pipeline" not in guidance
    # 失效后的旧 replaced 值不得再以「已替换」口径出现。
    assert "recorded as replaced" not in guidance
    assert "weekly-10" not in guidance

    a2a = _latest_event_payload(209, "a2a_event")
    assert "weekly-mys" in a2a["context"]
    assert "UNCONFIRMED" in a2a["context"]
    assert "取自承接流水线运行时 GRAPE_WORK_BRANCH" not in a2a["context"]
    assert "weekly-10" not in a2a["context"]


def _set_carried(number, carried):
    _set_issue(REPO, number, "target-scene")
    with db_lock:
        c = db.cursor()
        c.execute(
            "UPDATE issue SET carried_context=? WHERE repo_key=? AND issue_number=?",
            (json.dumps(carried), REPO, number))
        db.commit()


def test_issue_prompt_baseline_guidance():
    _set_carried(204, {"target_branch": "weekly-mys",
                       "target_branch_source": "target_scene_work_branch",
                       "target_branch_replaced": "weekly-10"})

    guidance = _build_cross_pipeline_baseline(REPO, "#204")
    assert "weekly-mys" in guidance
    assert "weekly-10" in guidance
    assert "Cross-pipeline Baseline" in guidance
    assert "authoritative baseline" in guidance
    assert "UNCONFIRMED" not in guidance

    assert _build_cross_pipeline_baseline(REPO, "#999") == ""


def test_issue_prompt_baseline_unconfirmed_without_source(monkeypatch):
    monkeypatch.delenv("GRAPE_WORK_BRANCH", raising=False)
    _set_carried(205, {"target_branch": "weekly-10"})

    guidance = _build_cross_pipeline_baseline(REPO, "#205")
    assert "weekly-10" in guidance
    assert "UNCONFIRMED" in guidance
    assert "target_branch_source=`missing`" in guidance
    assert "(authoritative baseline of the accepting pipeline" not in guidance


def test_issue_prompt_baseline_unconfirmed_for_non_scene_source(monkeypatch):
    monkeypatch.delenv("GRAPE_WORK_BRANCH", raising=False)
    _set_carried(207, {"target_branch": "weekly-10", "target_branch_source": "legacy_carried"})

    guidance = _build_cross_pipeline_baseline(REPO, "#207")
    assert "UNCONFIRMED" in guidance
    assert "target_branch_source=`legacy_carried`" in guidance
    assert "(authoritative baseline of the accepting pipeline" not in guidance


def test_prompt_reports_snapshot_runtime_drift(monkeypatch):
    _set_carried(208, {"target_branch": "weekly-mys",
                       "target_branch_source": "target_scene_work_branch"})
    monkeypatch.setenv("GRAPE_WORK_BRANCH", "weekly-new")

    guidance = _build_cross_pipeline_baseline(REPO, "#208")
    assert "weekly-new" in guidance
    assert "differs from the handoff-time snapshot" in guidance
    assert "`weekly-mys` → `weekly-new`" in guidance


def test_e2e_prompt_baseline_matches_dispatch_scene_work_branch(monkeypatch):
    """端到端：承接场景 work_branch → carried_context → build_devops_agent_prompt 注入。

    断言 prompt 的派发时基线来自「派发前现读承接场景 work_branch」，而非转交快照：
    转交后把场景 work_branch 改成独立值 X（不等于快照 weekly-mys），prompt 必须出现 X。
    """
    _set_issue(REPO, 206, "source-scene")
    _set_scene(REPO, "target-scene", [{"id": "vod"}], {"work_branch": "weekly-mys"})
    carried = {"source_issue_number": 206, "target_branch": "weekly-10"}
    result = _cross_pipeline_handoff(
        REPO, "206", "target-scene", "vod", carried, "", None,
        "handoff", "org/test-1753", "org/test-1753")
    assert result["ok"] is True

    cc = _persisted_cc(206)
    assert cc["target_branch"] == "weekly-mys"
    assert cc["target_branch_source"] == TARGET_BRANCH_SOURCE_SCENE_WORK_BRANCH

    # 派发前场景基线变更为 X：若 prompt 仍用转交快照 weekly-mys，本断言无法通过。
    _update_scene_options(REPO, "target-scene", {"work_branch": "weekly-new"})
    # API 进程不持有任务级 GRAPE_WORK_BRANCH，清除平台注入值以模拟真实派发进程。
    monkeypatch.delenv("GRAPE_WORK_BRANCH", raising=False)

    evt = {
        "type": "a2a_event",
        "repository": "org/test-1753",
        "source": "gitcode",
        "repo_key": REPO,
        "issue_id": "#206",
        "payload": {},
    }
    prompt = build_devops_agent_prompt(evt, [], "", ["issue"], agent_type="issue")
    assert "## Cross-pipeline Baseline" in prompt
    assert "authoritative baseline" in prompt
    assert "`weekly-new`" in prompt
    assert "differs from the handoff-time snapshot" in prompt
    assert "`weekly-mys` → `weekly-new`" in prompt

def test_a2a_baseline_note_prepended_and_survives_truncation():
    """长调用方上下文不得把承接基线声明挤出 [:500] 截断窗口。"""
    _set_issue(REPO, 210, "source-scene")
    _set_scene(REPO, "target-scene", [{"id": "vod"}], {"work_branch": "weekly-mys"})
    carried = {"source_issue_number": 210, "target_branch": "weekly-10"}
    long_context = "caller-context " * 80
    result = _cross_pipeline_handoff(
        REPO, "210", "target-scene", "vod", carried, "", None,
        long_context, "org/test-1753", "org/test-1753")
    assert result["ok"] is True

    a2a = _latest_event_payload(210, "a2a_event")
    # 声明前置：审计 context 截断后仍以基线声明开头并可读到目标分支。
    assert a2a["context"].startswith("承接基线")
    assert "weekly-mys" in a2a["context"]
    # 独立字段兜底，便于派发/审计直接读取，不受 context 截断影响。
    assert "weekly-mys" in a2a["handoff_baseline_note"]


# ---------------------------------------------------------------------------
# Issue #3758: 恢复 work_branch 存在性校验与回退（#1841 失效回归）
# ---------------------------------------------------------------------------

def test_webhook_repogit_symbol_import_smoke():
    """导入冒烟：webhook 依赖的 repogit.resolve_work_branch 必须真实存在。

    缺符号时本用例直接失败（而非交办时被 except Exception 吞成 WARNING）。
    """
    import src.domains.runtime.routes.webhook as webhook_mod

    assert hasattr(repogit_mod, "resolve_work_branch")
    assert callable(repogit_mod.resolve_work_branch)
    assert webhook_mod.resolve_work_branch is repogit_mod.resolve_work_branch


def test_resolve_work_branch_keeps_existing(monkeypatch):
    monkeypatch.setattr(
        repogit_mod, "list_repo_branches",
        lambda *a, **k: ({"main", "weekly-10"}, "main"))
    effective, note = repogit_mod.resolve_work_branch(REPO, "weekly-10")
    assert effective == "weekly-10"
    assert note == ""


def test_resolve_work_branch_falls_back_when_configured_missing(monkeypatch, caplog):
    monkeypatch.setattr(
        repogit_mod, "list_repo_branches",
        lambda *a, **k: ({"main", "weekly-10"}, "weekly-10"))
    with caplog.at_level(logging.WARNING):
        effective, note = repogit_mod.resolve_work_branch(REPO, "weekly-ghost")
    assert effective == "weekly-10"
    assert "weekly-ghost" in note and REPO in note and "weekly-10" in note
    assert any("does not exist in repo" in r.getMessage() for r in caplog.records)


def test_resolve_work_branch_unverifiable_keeps_configured(monkeypatch):
    monkeypatch.setattr(repogit_mod, "list_repo_branches", lambda *a, **k: (None, ""))
    effective, note = repogit_mod.resolve_work_branch(REPO, "weekly-ghost")
    assert effective == "weekly-ghost"
    assert note == ""


def test_handoff_falls_back_when_scene_work_branch_missing(monkeypatch, caplog):
    """配置分支不存在 → 交办使用回退分支并留 fallback 日志（#3758 核心回归）。"""
    _set_issue(REPO, 211, "source-scene")
    _set_scene(REPO, "target-scene", [{"id": "vod"}], {"work_branch": "weekly-ghost"})
    monkeypatch.setattr(
        repogit_mod, "list_repo_branches",
        lambda *a, **k: ({"main", "weekly-10"}, "weekly-10"))
    carried = {"source_issue_number": 211, "target_branch": "weekly-9"}
    with caplog.at_level(logging.WARNING):
        result = _cross_pipeline_handoff(
            REPO, "211", "target-scene", "vod", carried, "", None,
            "handoff", "org/test-1753", "org/test-1753")
    assert result["ok"] is True

    cc = _persisted_cc(211)
    assert cc["target_branch"] == "weekly-10"
    assert cc["target_branch_source"] == TARGET_BRANCH_SOURCE_SCENE_WORK_BRANCH
    assert cc["target_branch_replaced"] == "weekly-9"
    assert any("target scene work_branch fallback" in r.getMessage()
               and "weekly-ghost" in r.getMessage() for r in caplog.records)


def test_handoff_keeps_configured_when_branch_list_unverifiable(monkeypatch):
    """分支不可核实（无 token/网络失败）→ 保持原配置，不误判回退。"""
    _set_issue(REPO, 212, "source-scene")
    _set_scene(REPO, "target-scene", [{"id": "vod"}], {"work_branch": "weekly-ghost"})
    monkeypatch.setattr(repogit_mod, "list_repo_branches", lambda *a, **k: (None, ""))
    carried = {"source_issue_number": 212, "target_branch": "weekly-9"}
    result = _cross_pipeline_handoff(
        REPO, "212", "target-scene", "vod", carried, "", None,
        "handoff", "org/test-1753", "org/test-1753")
    assert result["ok"] is True

    cc = _persisted_cc(212)
    assert cc["target_branch"] == "weekly-ghost"
    assert cc["target_branch_replaced"] == "weekly-9"
