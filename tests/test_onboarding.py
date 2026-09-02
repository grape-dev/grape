"""Onboarding 首次托管引导测试：场景技能解析、幂等触发、落库、分派、_finalize 补库。"""
import os
import sys
import asyncio
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
        c.execute("DELETE FROM events")
        c.execute("DELETE FROM issue_state")
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


# ===================== trigger_onboarding =====================

def test_trigger_skips_when_no_skill():
    _clean()
    with patch.object(ob_mod, "_scene_onboarding_skill", return_value=""), \
         patch.object(ob_mod, "_is_onboarded", return_value=False), \
         patch.object(ob_mod, "_mark_onboarding_started") as mock_mark:
        ob_mod.trigger_onboarding("gitcode:owner/repo", "owner")
        mock_mark.assert_not_called()
    _clean()


def test_trigger_skips_when_already_onboarded_and_done():
    """§5.3 自愈语义：onboarded=1 且双 target 均已完成 → 跳过（不再重复触发）。"""
    _clean()
    with patch.object(ob_mod, "_scene_onboarding_skill", return_value="repo-onboarding"), \
         patch.object(ob_mod, "_is_onboarded", return_value=True), \
         patch.object(ob_mod, "_target_main_event_done", return_value=True), \
         patch.object(ob_mod, "_mark_onboarding_started") as mock_mark, \
         patch.object(ob_mod, "_dispatch_onboarding_target") as mock_dispatch:
        ob_mod.trigger_onboarding("gitcode:owner/repo", "owner")
        mock_mark.assert_not_called()
        mock_dispatch.assert_not_called()
    _clean()


def test_trigger_launches_two_guides_direct():
    _clean()
    user_db.add_managed_repo("gitcode:owner/repo", "owner", "repo", webhook_id=1)
    user_db.set_repo_scene("gitcode:owner/repo", "cloud-native")
    with patch.object(ob_mod, "_scene_onboarding_skill", return_value="repo-onboarding") as mock_skill, \
         patch.object(ob_mod, "_is_onboarded", return_value=False), \
         patch.object(ob_mod, "_mark_onboarding_started") as mock_mark, \
         patch.object(ob_mod, "_load_vod_session", return_value=("opencode", 0, "", 0)), \
         patch("src.runtime.engine_select.pick_engine", return_value=1), \
         patch("src.runtime.engine_select.engine_name", return_value="opencode"), \
         patch("src.runtime.service.runtime_execution_kind", return_value="direct"), \
         patch.object(ob_mod, "_execute_agent_wrapper") as mock_wrap:
        ob_mod.trigger_onboarding("gitcode:owner/repo", "owner")
        mock_skill.assert_called_with("cloud-native")
        mock_mark.assert_called_once()
        # 两次引导（experience / explore）各触发一次 direct 线程
        assert mock_wrap.call_count == 2
        targets = [c.args[8] for c in mock_wrap.call_args_list]
        assert sorted(targets) == ["experience", "explore"]
    _clean()


def test_trigger_launches_two_guides_queue():
    _clean()
    user_db.add_managed_repo("gitcode:owner/repo", "owner", "repo", webhook_id=1)
    with patch.object(ob_mod, "_scene_onboarding_skill", return_value="repo-onboarding"), \
         patch.object(ob_mod, "_is_onboarded", return_value=False), \
         patch.object(ob_mod, "_mark_onboarding_started"), \
         patch.object(ob_mod, "_load_vod_session", return_value=("worker", 7, "", 0)), \
         patch("src.runtime.service.runtime_execution_kind", return_value="queue_local"), \
         patch.object(ob_mod, "_enqueue_onboarding_task") as mock_enq:
        ob_mod.trigger_onboarding("gitcode:owner/repo", "owner")
        assert mock_enq.call_count == 2
        targets = [c.args[6] for c in mock_enq.call_args_list]
        assert sorted(targets) == ["experience", "explore"]
    _clean()


def test_enqueue_task_uses_issue_session_per_target():
    """方案 B：queue 入队按预分配 Issue 会话隔离——两个引导任务 issue_session_id 不同且按 #1/#2。"""
    _clean()
    user_db.add_managed_repo("gitcode:owner/repo", "owner", "repo", webhook_id=1)
    captured = []

    def _fake_enqueue(**kwargs):
        captured.append(kwargs)
        return "t_" + kwargs.get("target", "")

    with patch("src.runtime.service.enqueue_task", side_effect=_fake_enqueue), \
         patch("src.prompt_builder.build_prompt", return_value="prompt"), \
         patch("src.user.db.get_gitcode_token", return_value="bot_token_xyz"):
        ob_mod._enqueue_onboarding_task("gitcode:owner/repo", 1, "opencode", 7, "owner", "repo-onboarding", "experience")
        ob_mod._enqueue_onboarding_task("gitcode:owner/repo", 2, "opencode", 7, "owner", "repo-onboarding", "explore")
    assert len(captured) == 2
    by_issue = {k["issue_id"]: k for k in captured}
    # experience→#1, explore→#2，各自独立 issue_session_id（并行串行槽隔离）
    assert set(by_issue.keys()) == {"#1", "#2"}
    assert by_issue["#1"]["issue_session_id"]
    assert by_issue["#2"]["issue_session_id"]
    assert by_issue["#1"]["issue_session_id"] != by_issue["#2"]["issue_session_id"]
    assert "ONBOARDING_TARGET" in by_issue["#1"]["env_extra"]
    # cwd 按 Issue 统一隔离：experience→ {repo}/issues/1/vod/，explore→ {repo}/issues/2/vod/
    assert by_issue["#1"]["cwd"] == ob_mod._onboarding_workdir("gitcode:owner/repo", "experience")
    assert by_issue["#2"]["cwd"] == ob_mod._onboarding_workdir("gitcode:owner/repo", "explore")
    assert by_issue["#1"]["cwd"].endswith(os.path.join("issues", "1", "vod"))
    assert by_issue["#2"]["cwd"].endswith(os.path.join("issues", "2", "vod"))
    assert "onboarding-agent" not in by_issue["#1"]["cwd"]
    # bot token 注入（queue 模式必须有 BOT_GITCODE_TOKEN，修复硬编码空串）
    assert by_issue["#1"]["repo_gitcode_token"] == "bot_token_xyz"
    assert by_issue["#2"]["repo_gitcode_token"] == "bot_token_xyz"
    _clean()


def test_trigger_creates_two_master_events():
    _clean()
    user_db.add_managed_repo("gitcode:owner/repo", "owner", "repo", webhook_id=1)
    with patch.object(ob_mod, "_scene_onboarding_skill", return_value="repo-onboarding"), \
         patch.object(ob_mod, "_is_onboarded", return_value=False), \
         patch.object(ob_mod, "_mark_onboarding_started"), \
         patch.object(ob_mod, "_load_vod_session", return_value=("opencode", 0, "", 0)), \
         patch("src.runtime.engine_select.pick_engine", return_value=1), \
         patch("src.runtime.engine_select.engine_name", return_value="opencode"), \
         patch("src.runtime.service.runtime_execution_kind", return_value="direct"), \
         patch.object(ob_mod, "_execute_agent_wrapper"):
        ob_mod.trigger_onboarding("gitcode:owner/repo", "owner")
        with db_lock:
            rows = db.cursor().execute(
                "SELECT event_type, issue_id, agent_type, status FROM events WHERE repo_key='gitcode:owner/repo' ORDER BY event_type"
            ).fetchall()
        types = [r[0] for r in rows]
        assert "onboarding_exp" in types and "onboarding_explore" in types
        by_type = {r[0]: r for r in rows}
        # 主事件 issue_id 预分配：experience→#1, explore→#2（方案 A）
        assert by_type["onboarding_exp"][1] == "#1"
        assert by_type["onboarding_explore"][1] == "#2"
        for r in rows:
            assert r[2] == "vod" and r[3] == "running"
    _clean()


# ===================== _finalize_onboarding =====================

def test_finalize_creates_stage_and_event(tmp_path):
    _clean()
    rp = os.path.join(str(tmp_path), "onboarding-result-experience.json")
    with open(rp, "w", encoding="utf-8") as f:
        json.dump({"number": 1, "title": "体验", "assignee": "owner", "target": "experience"}, f)
    # 预置一个主事件（issue_id=#0），校验 _finalize 会校准回实际 Issue 号
    with db_lock:
        db.cursor().execute(
            "INSERT INTO events (repo_key, issue_id, event_type, agent_type, title, status) VALUES (?,?,?,?,?,?)",
            ("gitcode:owner/repo", "#0", "onboarding_exp", "vod", "引导·快速部署体验", "running"))
        db.commit()
        _master_seq = db.cursor().execute(
            "SELECT seq FROM events WHERE repo_key='gitcode:owner/repo' AND event_type='onboarding_exp'"
        ).fetchone()[0]
    ob_mod._finalize_onboarding("gitcode:owner/repo", _master_seq, "owner", rp)
    st = get_issue_stage("gitcode:owner/repo", "#1")
    assert st and st["stage"] == "vod" and st["owner_agent"] == "vod" and st["assignee"] == "owner"
    with db_lock:
        # 主事件 issue_id 被校准为 #1
        master = db.cursor().execute(
            "SELECT issue_id FROM events WHERE repo_key='gitcode:owner/repo' AND event_type='onboarding_exp'"
        ).fetchone()
        assert master and master[0] == "#1"
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


# ===================== §11 收敛 / §5.2-5.3 自愈回归 =====================

def test_trigger_registers_direct_task_rows():
    """direct 模式：引导任务经 _create_direct_task 登记 agent_tasks（受 sweeper 保护），pool 字段非空。"""
    _clean()
    from src.api.event_pipeline import _create_direct_task as _orig_create_direct_task
    user_db.add_managed_repo("gitcode:owner/repo", "owner", "repo", webhook_id=1)
    created = []

    def _fake_create_direct(repo_key, seq, agent_type, backend, skill, skill_path, engine_id, evt,
                            issue_id="", skill_id=0):
        _tid = _orig_create_direct_task(repo_key, seq, agent_type, backend, skill, skill_path,
                                        engine_id, evt, issue_id, skill_id=skill_id)
        if _tid:
            created.append((seq, _tid))
        return _tid

    with patch.object(ob_mod, "_scene_onboarding_skill", return_value="repo-onboarding"), \
         patch.object(ob_mod, "_is_onboarded", return_value=False), \
         patch.object(ob_mod, "_mark_onboarding_started"), \
         patch.object(ob_mod, "_load_vod_session", return_value=("opencode", 1, "skills/vod/SKILL.md", 1)), \
         patch("src.runtime.engine_select.pick_engine", return_value=1), \
         patch("src.runtime.engine_select.engine_name", return_value="opencode"), \
         patch("src.runtime.service.runtime_execution_kind", return_value="direct"), \
         patch("src.api.event_pipeline._create_direct_task", side_effect=_fake_create_direct) as m_create:
        ob_mod.trigger_onboarding("gitcode:owner/repo", "owner")
        # 线程异步执行，轮询等待两个任务行登记完成
        import time as _t
        for _ in range(100):
            if len(created) >= 2:
                break
            _t.sleep(0.05)
        assert m_create.call_count == 2
    # 两个 direct 引导任务各落一行 agent_tasks（event_seq 递增、pool_engine_ids 存在）
    assert len(created) == 2
    for seq, tid in created:
        with db_lock:
            row = db.cursor().execute(
                "SELECT status, pool_tag, pool_engine_ids, skill FROM agent_tasks WHERE id=?", (tid,)).fetchone()
        assert row, f"task {tid} 应存在"
        assert row[0] in ("queued", "running") and row[3] == "repo-onboarding"
        assert row[1] == "" and row[2] == ""  # 非插件广播 → 空占位（非 None，INSERT 不漂移）
    _clean()


def test_trigger_no_engine_marks_failed_snapshot():
    """§11 引擎门禁：pick_engine=None → engine_unavailable，不隐式 opencode，落 failed 事件+快照。"""
    _clean()
    user_db.add_managed_repo("gitcode:owner/repo", "owner", "repo", webhook_id=1)
    with patch.object(ob_mod, "_scene_onboarding_skill", return_value="repo-onboarding"), \
         patch.object(ob_mod, "_is_onboarded", return_value=False), \
         patch.object(ob_mod, "_mark_onboarding_started"), \
         patch.object(ob_mod, "_load_vod_session", return_value=("opencode", 0, "", 0)), \
         patch("src.runtime.engine_select.pick_engine", return_value=None), \
         patch.object(ob_mod, "_execute_agent_wrapper") as mock_wrap, \
         patch.object(ob_mod, "_enqueue_onboarding_task") as mock_enq:
        ob_mod.trigger_onboarding("gitcode:owner/repo", "owner")
        mock_wrap.assert_not_called()
        mock_enq.assert_not_called()
    with db_lock:
        rows = db.cursor().execute(
            "SELECT status FROM events WHERE repo_key='gitcode:owner/repo'").fetchall()
        assert rows and all(r[0] == "failed" for r in rows)
        snap = db.cursor().execute(
            "SELECT COUNT(*) FROM agent_tasks WHERE repo_key='gitcode:owner/repo'").fetchone()[0]
    assert snap == 2, "两个 target 均应落 failed 快照（重派可恢复）"
    _clean()


def test_retry_onboarding_target_self_heal():
    """§5.3：onboarded=1 但某 target 失败 → retry 该 target 补齐，不因全量标记放弃。"""
    _clean()
    user_db.add_managed_repo("gitcode:owner/repo", "owner", "repo", webhook_id=1)
    with db_lock:
        # 模拟历史：explore 主事件 failed
        db.cursor().execute(
            "INSERT INTO events (repo_key, issue_id, event_type, agent_type, title, status) "
            "VALUES ('gitcode:owner/repo', '#2', 'onboarding_explore', 'vod', '引导·探索方向', 'failed')")
        db.commit()
    with patch.object(ob_mod, "_scene_onboarding_skill", return_value="repo-onboarding"), \
         patch.object(ob_mod, "_load_vod_session", return_value=("opencode", 0, "", 0)), \
         patch("src.runtime.engine_select.pick_engine", return_value=1), \
         patch("src.runtime.engine_select.engine_name", return_value="opencode"), \
         patch("src.runtime.service.runtime_execution_kind", return_value="direct"), \
         patch.object(ob_mod, "_dispatch_onboarding_target", return_value=5001) as m_dispatch:
        res = ob_mod.retry_onboarding_target("gitcode:owner/repo", "owner", "explore")
    assert res["ok"] is True and res["seq"] == 5001
    assert m_dispatch.call_count == 1
    assert m_dispatch.call_args.args[7] == "explore"  # target 参数
    _clean()


def test_retry_onboarding_target_already_done():
    """§5.3 幂等：target 已完成 → retry 不重复触发。"""
    _clean()
    user_db.add_managed_repo("gitcode:owner/repo", "owner", "repo", webhook_id=1)
    with patch.object(ob_mod, "_target_main_event_done", return_value=True), \
         patch.object(ob_mod, "_dispatch_onboarding_target") as m_dispatch:
        res = ob_mod.retry_onboarding_target("gitcode:owner/repo", "owner", "experience")
    assert res["ok"] is True and res["seq"] == 0
    m_dispatch.assert_not_called()
    _clean()
