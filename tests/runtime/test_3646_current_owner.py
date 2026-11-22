"""Issue #3646：流水线「当前责任人」只展示人类（内部 Agent 账号过滤）单测。

覆盖三层 seam：
- 纯函数 `owner_display`：内部账号判定 + 候选优先级；
- 展示 seam `_get_issue_pipeline(...)["current_owner"]`（#3644 事件链复现）；
- 持久化 seam `session_store.set_issue_stage / sync_issue_assignee`（assignee 不被抹掉、空值回填）。
"""
import pytest
from unittest.mock import patch

from src.api import session_store as ss
from src.kernel.session_manager import db, db_lock
from src.domains.runtime.routes.pipeline import _get_issue_pipeline
from src.domains.runtime.pipeline.owner_display import (
    is_internal_agent_account, resolve_human_owner, resolve_current_owner,
)

RK = "gitcode:org/owner-3646"


def _ev(seq, issue_id, event_type, agent_type, title, status, actor,
        created_at="2026-11-21 10:00:00", payload=None):
    return {"seq": seq, "issue_id": issue_id, "event_type": event_type,
            "agent_type": agent_type, "title": title, "status": status,
            "created_at": created_at, "actor": actor, "payload": payload}


_AGENT_NAMES = {"vod": "VOD Agent", "issue": "Issue Agent", "coding": "Coding Agent",
                "qa": "QA Agent", "sre": "SRE Agent"}


def _pipeline(events, stage_assignee=None, bot_usernames=None, feishu=None):
    """在固定 bot 名单 + Agent 显示名 + 用户名直通下取 current_owner（不依赖真实库）。"""
    stage_map = {"#1": {"assignee": stage_assignee}} if stage_assignee is not None else None
    with patch("src.domains.mgmt.user.db.get_all_bot_usernames",
               return_value=["grape_bot"]), \
         patch("src.domains.runtime.pipeline.event_pipeline.get_agent_display_name",
               side_effect=lambda at: _AGENT_NAMES.get(at, at)), \
         patch("src.domains.runtime.routes.pipeline._resolve_feishu_name",
               side_effect=feishu or (lambda u, *a: u)):
        return _get_issue_pipeline("owner/repo", "#1", events,
                                   stage_map=stage_map, bot_usernames=bot_usernames)


class TestInternalAccountPredicate:
    """内部 Agent 身份不得直接作为责任人展示。"""

    def test_gcw_prefix_is_internal(self):
        """GitCode OAuth 自动登录名（#3644 的 gcw_0FAKEACCT）→ 内部账号。"""
        assert is_internal_agent_account("gcw_0FAKEACCT") is True
        assert is_internal_agent_account("gcw_7pdiBkYp") is True

    def test_bot_usernames_from_config_are_internal(self):
        assert is_internal_agent_account("grape_bot", ["grape_bot", "ci_bot"]) is True

    def test_bot_suffix_shapes_are_internal(self):
        for name in ("bot", "grape_bot", "dev-bot", "Release_BOT"):
            assert is_internal_agent_account(name) is True, name

    def test_bot_suffix_does_not_kill_human_logins(self):
        """裸 endswith('bot') 会误杀真人账号，形态规则必须带分隔符。"""
        for name in ("abbot", "robot", "talbot", "songspace", "Jason7602"):
            assert is_internal_agent_account(name) is False, name

    def test_agent_display_and_runtime_shapes_are_internal(self):
        for name in ("QA Agent", "Coding Agent", "hermes-worker-ecs-grape-de-0005",
                     "aishell-dsh-worker-ecs-grape-wo-0002"):
            assert is_internal_agent_account(name) is True, name

    def test_empty_is_not_internal(self):
        """空串 = 没有责任人，交给候选顺序跳过，不当作内部账号。"""
        assert is_internal_agent_account("") is False
        assert is_internal_agent_account(None) is False


class TestResolveCurrentOwner:
    """候选优先级：人类 assignee → 人类操作者 → Agent 显示名回退。"""

    def test_human_assignee_first(self):
        assert resolve_current_owner(["alice", "bob"], ["carol"]) == "alice"

    def test_internal_assignee_skipped(self):
        assert resolve_current_owner(["gcw_0FAKEACCT", "alice"], []) == "alice"
        assert resolve_current_owner(["grape_bot"], ["songspace"], ["grape_bot"]) == "songspace"

    def test_all_internal_falls_back(self):
        assert resolve_current_owner(["grape_bot"], ["gcw_0FAKEACCT"], ["grape_bot"],
                                     fallback="QA Agent") == "QA Agent"

    def test_empty_candidates_fall_back(self):
        assert resolve_current_owner([], [], fallback="VOD Agent") == "VOD Agent"

    def test_human_owner_helper_returns_empty_when_all_internal(self):
        assert resolve_human_owner(["gcw_x"], ["grape_bot"], ["grape_bot"]) == ""


class TestCurrentOwnerResolution:
    """展示 seam：_get_issue_pipeline → current_owner（Issue #3644 事件链复现）。"""

    def test_issue_3644_gcw_actor_is_skipped_for_human_owner(self):
        """#3644：qa 阶段 A2A 事件 actor 是内部账号 gcw_0FAKEACCT → 责任人应为 songspace。"""
        events = [
            _ev(1, "#1", "issue_opened", "vod", "新 Issue", "completed", "songspace"),
            _ev(2, "#1", "a2a_event", "issue", "A2A: → issue", "completed", "songspace"),
            _ev(3, "#1", "a2a_event", "qa", "A2A: → qa", "running", "gcw_0FAKEACCT"),
        ]
        p = _pipeline(events, stage_assignee="")
        assert p["current_owner"] == "songspace", p["current_owner"]

    def test_human_assignee_wins_over_human_actor(self):
        events = [_ev(1, "#1", "issue_opened", "issue", "T", "completed", "bob")]
        p = _pipeline(events, stage_assignee="alice")
        assert p["current_owner"] == "alice"

    def test_bot_assignee_falls_back_to_human_actor(self):
        """assignee 为 bot（机器人接管）→ 不能当责任人，回退最近人类操作者。"""
        events = [
            _ev(1, "#1", "issue_opened", "vod", "T", "completed", "songspace"),
            _ev(2, "#1", "a2a_event", "qa", "A2A: → qa", "running", "grape_bot"),
        ]
        p = _pipeline(events, stage_assignee="grape_bot")
        assert p["current_owner"] == "songspace", p["current_owner"]

    def test_gcw_assignee_falls_back_to_human_actor(self):
        events = [_ev(1, "#1", "issue_opened", "issue", "T", "completed", "songspace")]
        p = _pipeline(events, stage_assignee="gcw_0FAKEACCT")
        assert p["current_owner"] == "songspace", p["current_owner"]

    def test_all_internal_falls_back_to_agent_display_name(self):
        """全为内部账号 → Agent 显示名，绝不输出原始内部账号。"""
        events = [
            _ev(1, "#1", "issue_opened", "issue", "T", "completed", "grape_bot"),
            _ev(2, "#1", "a2a_event", "qa", "A2A: → qa", "running", "gcw_0FAKEACCT"),
        ]
        p = _pipeline(events, stage_assignee="gcw_0FAKEACCT")
        assert p["current_owner"] == "QA Agent", p["current_owner"]
        assert "gcw_" not in p["current_owner"]

    def test_agent_display_name_actor_is_internal(self):
        events = [
            _ev(1, "#1", "issue_opened", "vod", "T", "completed", "songspace"),
            _ev(2, "#1", "a2a_event", "qa", "A2A: → qa", "running", "QA Agent"),
        ]
        p = _pipeline(events, stage_assignee="")
        assert p["current_owner"] == "songspace", p["current_owner"]

    def test_human_actor_payload_assignee_used_over_actors(self):
        events = [
            _ev(1, "#1", "issue_opened", "issue", "T", "completed", "bob",
                payload={"object_attributes": {"assignee": "alice"}}),
        ]
        p = _pipeline(events)
        assert p["current_owner"] == "alice", p["current_owner"]

    def test_human_owner_resolved_to_feishu_nickname(self):
        """人类责任人仍走飞书昵称解析（解析口径不变）。"""
        events = [_ev(1, "#1", "issue_opened", "issue", "T", "completed", "songspace")]
        p = _pipeline(events, stage_assignee="songspace",
                      feishu=lambda u, *a: "吕军涛" if u == "songspace" else u)
        assert p["current_owner"] == "吕军涛"


@pytest.fixture(autouse=True)
def _clean_issue_rows():
    _sql = ("DELETE FROM issue WHERE repo_key=?", (RK,)), \
           ("DELETE FROM issue_state WHERE repo_key=?", (RK,))
    with db_lock:
        for stmt, params in _sql:
            try:
                db.cursor().execute(stmt, params)
            except Exception:
                pass
        db.commit()
    yield
    with db_lock:
        for stmt, params in _sql:
            try:
                db.cursor().execute(stmt, params)
            except Exception:
                pass
        db.commit()


class TestIssueAssigneeKeepsValue:
    """阶段推进（A2A handoff）不得把已有责任人抹成空串。"""

    def test_stage_advance_keeps_assignee(self):
        ss.set_issue_stage(RK, "#7", "vod", "vod", assignee="songspace")
        ss.set_issue_stage(RK, "#7", "qa", "qa")  # A2A 推进：不携带 assignee
        assert ss.get_issue_assignee(RK, "#7") == "songspace"

    def test_explicit_assignee_still_overwrites(self):
        ss.set_issue_stage(RK, "#7", "vod", "vod", assignee="songspace")
        ss.set_issue_stage(RK, "#7", "qa", "qa", assignee="alice")
        assert ss.get_issue_assignee(RK, "#7") == "alice"

    def test_new_row_default_empty(self):
        ss.set_issue_stage(RK, "#7", "vod", "vod")
        assert ss.get_issue_assignee(RK, "#7") == ""


class TestIssueAssigneeBackfill:
    """指派/更新事件把 VCS 当前 assignee 补进卡片（仅空值回填）。"""

    def test_backfill_when_empty(self):
        ss.set_issue_stage(RK, "#7", "coding", "coding")
        assert ss.sync_issue_assignee(RK, "#7", "songspace") is True
        assert ss.get_issue_assignee(RK, "#7") == "songspace"

    def test_do_not_override_existing_human(self):
        ss.set_issue_stage(RK, "#7", "coding", "coding", assignee="songspace")
        assert ss.sync_issue_assignee(RK, "#7", "grape_bot") is False
        assert ss.get_issue_assignee(RK, "#7") == "songspace"

    def test_empty_assignee_is_noop(self):
        ss.set_issue_stage(RK, "#7", "coding", "coding", assignee="songspace")
        assert ss.sync_issue_assignee(RK, "#7", "") is False
        assert ss.get_issue_assignee(RK, "#7") == "songspace"

    def test_no_card_row_is_not_reported_as_backfilled(self):
        """无卡片行 → 不假装回填成功（对账口径），也不抛异常。"""
        assert ss.sync_issue_assignee(RK, "#404", "songspace") is False