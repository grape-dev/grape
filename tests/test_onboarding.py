"""Onboarding 首次托管引导测试（§11 v1.5：模拟 GitCode issue_opened webhook → run_pipeline）。

覆盖：场景技能解析、幂等触发、webhook 事件体构造（_onboarding 上下文）、target 自愈、_finalize 补库。
"""
import os
import sys
import json
from unittest.mock import patch

ROOT = os.path.join(os.path.dirname(__file__), "..")
os.environ.setdefault("DEVOPS_DB_PATH", os.path.join(ROOT, "data", "test_sessions.db"))

from src.session_manager import db, db_lock
from src.user import db as user_db
from src.api import onboarding as ob_mod
from src.api.session_store import get_issue_stage


def _clean():
    with db_lock:
        c = db.cursor()
        c.execute("DELETE FROM managed_repos")
        c.execute("DELETE FROM meta WHERE key LIKE 'onboarded:%'")
        c.execute("DELETE FROM events WHERE repo_key LIKE 'test_%' OR repo_key LIKE 'gitcode:test_%' OR repo_key LIKE 'gitcode:org/test%' OR repo_key LIKE 'gitcode:org/pipeline-test%' OR repo_key LIKE 'gitcode:owner/repo%' OR repo_key LIKE 'owner/repo%'")
        c.execute("DELETE FROM issue_state WHERE repo_key LIKE 'test_%' OR repo_key LIKE 'gitcode:test_%' OR repo_key LIKE 'gitcode:org/test%' OR repo_key LIKE 'gitcode:org/pipeline-test%' OR repo_key LIKE 'gitcode:owner/repo%' OR repo_key LIKE 'owner/repo%'")
        c.execute("DELETE FROM agent_tasks WHERE repo_key LIKE '%owner/repo%'")
        db.commit()


# ===================== dev_scene 读写 =====================

def test_set_get_repo_scene():
    _clean()
    user_db.add_managed_repo("gitcode:owner/repo", "owner", "repo", webhook_id=1)
    user_db.set_repo_scene("gitcode:owner/repo", "cloud-native")
    assert user_db.get_repo_scene("gitcode:owner/repo") == "cloud-native"
    assert user_db.get_repo_scene("gitcode:no/such") == "default"
    _clean()


def test_add_managed_repo_dev_scene_default():
    _clean()
    user_db.add_managed_repo("gitcode:owner/repo", "owner", "repo", webhook_id=1)
    assert user_db.get_repo_scene("gitcode:owner/repo") == "default"
    _clean()


def test_add_managed_repo_dev_scene_explicit():
    _clean()
    user_db.add_managed_repo("gitcode:owner/repo", "owner", "repo", webhook_id=1, dev_scene="iac")
    assert user_db.get_repo_scene("gitcode:owner/repo") == "iac"
    _clean()


# ===================== 场景 onboarding 技能解析 =====================

def test_scene_onboarding_skill():
    assert ob_mod._scene_onboarding_skill("default") == "repo-onboarding"
    assert ob_mod._scene_onboarding_skill("cloud-native") == "repo-onboarding"
    assert ob_mod._scene_onboarding_skill("iac") == "repo-onboarding"
    assert ob_mod._scene_onboarding_skill("no-such-scene") == ""


# ===================== 幂等标记 =====================

def test_onboarded_mark_idempotent():
    _clean()
    assert ob_mod._is_onboarded("gitcode:owner/repo") is False
    ob_mod._mark_onboarding_started("gitcode:owner/repo")
    assert ob_mod._is_onboarded("gitcode:owner/repo") is True
    _clean()


# ===================== trigger_onboarding（webhook 模拟） =====================

def test_trigger_skips_when_no_skill():
    _clean()
    with patch.object(ob_mod, "_scene_onboarding_skill", return_value=""), \
         patch.object(ob_mod, "_is_onboarded", return_value=False), \
         patch.object(ob_mod, "_mark_onboarding_started") as mock_mark, \
         patch.object(ob_mod, "_submit_onboarding_webhook") as mock_sub:
        ob_mod.trigger_onboarding("gitcode:owner/repo", "owner")
        mock_mark.assert_not_called()
        mock_sub.assert_not_called()
    _clean()


def test_trigger_skips_when_already_onboarded_and_done():
    """§5.3 自愈语义：onboarded=1 且双 target 均已完成 → 跳过（不再重复触发）。"""
    _clean()
    with patch.object(ob_mod, "_scene_onboarding_skill", return_value="repo-onboarding"), \
         patch.object(ob_mod, "_is_onboarded", return_value=True), \
         patch.object(ob_mod, "_target_main_event_done", return_value=True), \
         patch.object(ob_mod, "_mark_onboarding_started") as mock_mark, \
         patch.object(ob_mod, "_submit_onboarding_webhook") as mock_sub:
        ob_mod.trigger_onboarding("gitcode:owner/repo", "owner")
        mock_mark.assert_not_called()
        mock_sub.assert_not_called()
    _clean()


def test_trigger_submits_two_webhooks_for_targets():
    """核心：trigger 为每个 target 提交一次模拟 issue_opened webhook → run_pipeline。"""
    _clean()
    user_db.add_managed_repo("gitcode:owner/repo", "owner", "repo", webhook_id=1)
    user_db.set_repo_scene("gitcode:owner/repo", "cloud-native")
    calls = []
    with patch.object(ob_mod, "_scene_onboarding_skill", return_value="repo-onboarding") as mock_skill, \
         patch.object(ob_mod, "_is_onboarded", return_value=False), \
         patch.object(ob_mod, "_mark_onboarding_started") as mock_mark, \
         patch.object(ob_mod, "_target_main_event_done", return_value=False), \
         patch.object(ob_mod, "_submit_onboarding_webhook", side_effect=lambda *a, **k: calls.append((a, k)) or 1001) as mock_sub:
        ob_mod.trigger_onboarding("gitcode:owner/repo", "owner")
        mock_skill.assert_called_with("cloud-native")
        mock_mark.assert_called_once()
        assert mock_sub.call_count == 2
        targets = [c[0][6] for c in calls]  # target 参数位置
        assert sorted(targets) == ["experience", "explore"]
    _clean()


def test_submit_webhook_body_carries_onboarding_context():
    """webhook 事件体：object_attributes.open → issue_opened；payload._onboarding 携带 target/skill/scene/result_path。"""
    _clean()
    captured = {}

    def _fake_run_pipeline(raw, headers, account="", repo_key_override=""):
        captured["raw"] = raw
        captured["headers"] = headers
        captured["account"] = account
        captured["override"] = repo_key_override
        return {"seq": 3001, "ok": True}

    with patch("src.api.event_pipeline.run_pipeline", side_effect=_fake_run_pipeline), \
         patch.object(ob_mod, "_await_event_and_finalize"):
        seq = ob_mod._submit_onboarding_webhook(
            "gitcode:owner/repo", "owner", "default", "repo-onboarding",
            "skills/repo-onboarding/SKILL.md", 0, "experience")
    assert seq == 3001
    assert captured["headers"]["X-GitCode-Event"] == "Issue Hook"
    assert captured["override"] == "gitcode:owner/repo"
    assert captured["account"] == "owner/repo"
    body = json.loads(captured["raw"].decode("utf-8"))
    assert body["object_attributes"]["action"] == "open"
    assert body["object_attributes"]["iid"] == 1  # experience→#1
    onb = body["_onboarding"]
    assert onb["target"] == "experience"
    assert onb["skill"] == "repo-onboarding"
    assert onb["scene"] == "default"
    assert "result_path" in onb and "onboarding-result-experience.json" in onb["result_path"]
    _clean()


def test_trigger_skips_done_target():
    """target 级自愈：已完成 target 不重复提交，未完成 target 照常补做。"""
    _clean()
    user_db.add_managed_repo("gitcode:owner/repo", "owner", "repo", webhook_id=1)
    with patch.object(ob_mod, "_scene_onboarding_skill", return_value="repo-onboarding"), \
         patch.object(ob_mod, "_is_onboarded", return_value=True), \
         patch.object(ob_mod, "_mark_onboarding_started"), \
         patch.object(ob_mod, "_target_main_event_done", side_effect=lambda rk, t: t == "experience"), \
         patch.object(ob_mod, "_submit_onboarding_webhook") as mock_sub:
        ob_mod.trigger_onboarding("gitcode:owner/repo", "owner")
        assert mock_sub.call_count == 1
        assert mock_sub.call_args.args[6] == "explore"  # 只补 explore
    _clean()


# ===================== _finalize_onboarding =====================

def test_finalize_creates_stage_and_event(tmp_path):
    _clean()
    rp = os.path.join(str(tmp_path), "onboarding-result-experience.json")
    with open(rp, "w", encoding="utf-8") as f:
        json.dump({"number": 1, "title": "体验", "assignee": "owner", "target": "experience"}, f)
    # 预置一个主事件（issue_id=#1，模拟 run_pipeline 已落 issue_opened 事件）
    with db_lock:
        db.cursor().execute(
            "INSERT INTO events (repo_key, issue_id, event_type, agent_type, title, status) VALUES (?,?,?,?,?,?)",
            ("gitcode:owner/repo", "#1", "issue_opened", "vod", "引导·快速部署体验", "completed"))
        db.commit()
        _master_seq = db.cursor().execute(
            "SELECT seq FROM events WHERE repo_key='gitcode:owner/repo' AND event_type='issue_opened'"
        ).fetchone()[0]
    ob_mod._finalize_onboarding("gitcode:owner/repo", _master_seq, "owner", rp)
    st = get_issue_stage("gitcode:owner/repo", "#1")
    assert st and st["stage"] == "vod" and st["owner_agent"] == "vod" and st["assignee"] == "owner"
    with db_lock:
        row = db.cursor().execute(
            "SELECT event_type, issue_id, agent_type, status FROM events WHERE repo_key='gitcode:owner/repo' AND event_type='onboarding_issue'"
        ).fetchone()
    assert row and row[0] == "onboarding_issue" and row[1] == "#1" and row[2] == "vod" and row[3] == "completed"
    _clean()


def test_finalize_missing_result_noop():
    _clean()
    ob_mod._finalize_onboarding("gitcode:owner/repo", 99, "owner", "/nonexistent/x.json")
    assert get_issue_stage("gitcode:owner/repo", "#1") is None
    with db_lock:
        cnt = db.cursor().execute("SELECT COUNT(*) FROM events WHERE repo_key='gitcode:owner/repo'").fetchone()[0]
    assert cnt == 0
    _clean()


# ===================== §5.3 自愈 =====================

def test_retry_onboarding_target_submits_webhook():
    """§5.3：onboarded=1 但某 target 失败 → retry 该 target 走 webhook（广播模式不需绑引擎）。"""
    _clean()
    user_db.add_managed_repo("gitcode:owner/repo", "owner", "repo", webhook_id=1)
    with patch.object(ob_mod, "_scene_onboarding_skill", return_value="repo-onboarding"), \
         patch.object(ob_mod, "_target_main_event_done", return_value=False), \
         patch.object(ob_mod, "_is_onboarded", return_value=True), \
         patch.object(ob_mod, "_mark_onboarding_started"), \
         patch.object(ob_mod, "_submit_onboarding_webhook", return_value=4002) as mock_sub:
        res = ob_mod.retry_onboarding_target("gitcode:owner/repo", "owner", "explore")
    assert res["ok"] is True and res["seq"] == 4002
    assert mock_sub.call_count == 1
    assert mock_sub.call_args.args[6] == "explore"
    _clean()


def test_retry_onboarding_target_already_done():
    """§5.3 幂等：target 已完成 → retry 不重复触发。"""
    _clean()
    user_db.add_managed_repo("gitcode:owner/repo", "owner", "repo", webhook_id=1)
    with patch.object(ob_mod, "_target_main_event_done", return_value=True), \
         patch.object(ob_mod, "_submit_onboarding_webhook") as mock_sub:
        res = ob_mod.retry_onboarding_target("gitcode:owner/repo", "owner", "experience")
    assert res["ok"] is True and res["seq"] == 0
    mock_sub.assert_not_called()
    _clean()