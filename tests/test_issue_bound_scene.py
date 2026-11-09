"""Issue #681: 推荐入口判定换用 get_issue_bound_scene（真实绑定专用）测试。

三态覆盖：
1. 无真实绑定 + 有 dev_scene → ("", 0)，应走智能推荐（不被 dev_scene 短路）
2. 有真实绑定（issue 表 / issue_state.scene_id）→ 跳过推荐
3. 无真实绑定 + 无 dev_scene → ("", 0)，应走智能推荐（行为不变）

回归：
- issue-415 展示回退：get_issue_scene 仍回退 dev_scene（展示/A2A 语义保留）
- event_pipeline 推荐入口判定使用 get_issue_bound_scene（mock 验证）
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest  # noqa: E402
from unittest.mock import patch, MagicMock  # noqa: E402

from src.session_manager import db, db_lock  # noqa: E402

REPO = "gitcode:o/r681"


@pytest.fixture(scope="session", autouse=True)
def _ensure_tables():
    from src.api import db_setup
    db_setup.init_database()


@pytest.fixture(autouse=True)
def _clean():
    with db_lock:
        c = db.cursor()
        for _sql in (
            "DELETE FROM issue WHERE repo_key LIKE 'gitcode:o/r681%'",
            "DELETE FROM issue_state WHERE repo_key LIKE 'gitcode:o/r681%'",
            "DELETE FROM managed_repos WHERE repo_key LIKE 'gitcode:o/r681%'",
        ):
            try:
                c.execute(_sql)
            except Exception:
                pass
        db.commit()
    yield


def _add_issue(number, scene_id=""):
    with db_lock:
        c = db.cursor()
        c.execute(
            "INSERT INTO issue (user_id, assignee, repo_key, issue_number, title, status, scene_id) "
            "VALUES (1, 'tester', ?, ?, 't', 'active', ?)",
            (REPO, number, scene_id))
        db.commit()


def _add_issue_state(number, scene_id):
    with db_lock:
        c = db.cursor()
        c.execute(
            "INSERT INTO issue_state (repo_key, issue_id, stage, scene_id) "
            "VALUES (?, ?, 'vod', ?)",
            (REPO, f"#{number}", scene_id))
        db.commit()


def _set_repo_scene(scene_id):
    from src.user.db import add_managed_repo, set_repo_scene
    add_managed_repo(REPO, "o", "r681")
    set_repo_scene(REPO, scene_id)


class TestGetIssueBoundScene:
    def test_no_binding_with_dev_scene_returns_empty(self):
        """三态 1：无真实绑定 + 有 dev_scene → ("", 0)（应走智能推荐）。"""
        from src.api.session_store import get_issue_bound_scene
        _set_repo_scene("custom-dev-scene")
        _add_issue(801, scene_id="")
        with patch("src.user.db.get_repo_scene", return_value="custom-dev-scene"):
            scene, uid = get_issue_bound_scene(REPO, "#801")
        assert scene == ""
        assert uid == 0

    def test_issue_table_binding_skips_recommend(self):
        """三态 2a：issue 表 scene_id 真实绑定 → 返回该场景（跳过推荐）。"""
        from src.api.session_store import get_issue_bound_scene
        _set_repo_scene("custom-dev-scene")
        _add_issue(802, scene_id="real-scene")
        with patch("src.user.db.get_repo_scene", return_value="custom-dev-scene"):
            scene, _ = get_issue_bound_scene(REPO, "#802")
        assert scene == "real-scene"

    def test_issue_state_binding_skips_recommend(self):
        """三态 2b：issue_state.scene_id 真实绑定（无 issue 行的过渡期数据）→ 跳过推荐。"""
        from src.api.session_store import get_issue_bound_scene
        _set_repo_scene("custom-dev-scene")
        _add_issue_state(803, "state-scene")
        with patch("src.user.db.get_repo_scene", return_value="custom-dev-scene"):
            scene, _ = get_issue_bound_scene(REPO, "#803")
        assert scene == "state-scene"

    def test_issue_state_layer_used_when_no_issue_row(self):
        """issue 行存在（Phase C3 权威）→ issue_state 层不参与，issue 表 scene 生效。"""
        from src.api.session_store import get_issue_bound_scene
        _add_issue(804, scene_id="table-scene")
        _add_issue_state(804, "state-scene")
        scene, _ = get_issue_bound_scene(REPO, "#804")
        assert scene == "table-scene"

    def test_no_binding_no_dev_scene_returns_empty(self):
        """三态 3：无真实绑定 + 无 dev_scene → ("", 0)（行为不变，走推荐）。"""
        from src.api.session_store import get_issue_bound_scene
        _add_issue(805, scene_id="")
        with patch("src.user.db.get_repo_scene", return_value=""):
            scene, uid = get_issue_bound_scene(REPO, "#805")
        assert scene == ""
        assert uid == 0

    def test_unknown_issue_returns_empty(self):
        """issue 不存在 + 有 dev_scene → ("", 0)（不回退）。"""
        from src.api.session_store import get_issue_bound_scene
        _set_repo_scene("custom-dev-scene")
        with patch("src.user.db.get_repo_scene", return_value="custom-dev-scene"):
            scene, uid = get_issue_bound_scene(REPO, "#999999")
        assert scene == ""
        assert uid == 0

    def test_pr_subscription_pseudo_id_parsed(self):
        """PR 订阅伪 issue_id：场景 ID 编码在伪 id 内，视为真实绑定。"""
        from src.api.session_store import get_issue_bound_scene
        scene, _ = get_issue_bound_scene(REPO, "#pr_42_scene-xyz")
        assert scene == "scene-xyz"


class TestGetIssueSceneRegression415:
    """回归 issue-415：get_issue_scene 展示回退 dev_scene 语义保留。"""

    def test_get_issue_scene_still_falls_back_to_dev_scene(self):
        from src.api.session_store import get_issue_scene
        _set_repo_scene("custom-dev-scene")
        _add_issue(811, scene_id="")
        with patch("src.user.db.get_repo_scene", return_value="custom-dev-scene"):
            scene, uid = get_issue_scene(REPO, "#811")
        assert scene == "custom-dev-scene"
        assert uid == 0

    def test_get_issue_scene_real_binding_wins(self):
        from src.api.session_store import get_issue_scene
        _set_repo_scene("custom-dev-scene")
        _add_issue(812, scene_id="real-scene")
        with patch("src.user.db.get_repo_scene", return_value="custom-dev-scene"):
            scene, _ = get_issue_scene(REPO, "#812")
        assert scene == "real-scene"


class TestPipelineRecommendEntry:
    """event_pipeline issue_opened 推荐入口判定使用 get_issue_bound_scene（#681 核心）。

    harness 对齐 tests/test_bot_opened_recommend.py（session 级 token + 引擎广播派发 mock）。
    """

    @staticmethod
    def _dispatch_patches():
        captured = []

        def _fake_enqueue(*args, **kwargs):
            captured.append({
                "agent_type": args[2] if len(args) > 2 else kwargs.get("agent_type"),
                "is_scene_recommend": kwargs.get("is_scene_recommend", False),
            })
            return None

        from src.token_resolver import TokenResult
        _sess = lambda rk: TokenResult(token="SESSION_PAT", level="session",
                                       provider=MagicMock(), repo_key=RK)
        return captured, [
            patch("src.user.db.get_all_bot_usernames", return_value=["grape_bot"]),
            patch("src.token_resolver.TokenResolver.resolve_config_level", side_effect=_sess),
            patch("src.token_resolver.SessionTokenHandler.is_self_loop_actor", return_value=True),
            patch("src.runtime.engine_select.pick_engine", return_value=0),
            patch("src.runtime.engine_select.engine_name", return_value="opencode"),
            patch("src.runtime.engine_select.get_scene_pool_engine_ids", return_value=""),
            patch("src.runtime.engine_select._repo_user_id", return_value=1),
            patch("src.api.event_pipeline.is_plugin_mode", return_value=True),
            patch("src.runtime.service.get_execution_mode", return_value="remote"),
            patch("src.api.event_pipeline._enqueue_pipeline_task", side_effect=_fake_enqueue),
            patch("src.user.db.get_agent_bot_token", return_value=""),
            patch("src.project_info.builder.maybe_refresh_project_info", lambda *a, **k: None),
        ]

    @staticmethod
    def _issue_opened_payload(num, username="alice"):
        import json as _json
        return _json.dumps({
            "object_kind": "issue",
            "project": {"path_with_namespace": "o/r681"},
            "object_attributes": {"id": num, "iid": num, "number": num,
                                  "title": f"T{num}", "action": "open", "description": "d"},
            "issue": {"iid": num, "number": num},
            "user": {"username": username},
        }).encode()

    def _run(self, num):
        from src.api import event_pipeline as ep
        return ep.run_pipeline(self._issue_opened_payload(num), {"X-GitCode-Event": "Issue Hook"},
                               account="o/r681", repo_key_override="o/r681")

    def test_entry_dev_scene_not_short_circuit_recommend(self):
        """无真实绑定 + 有 dev_scene → 入口判定仍走推荐（#681 核心修复）。"""
        _set_repo_scene("custom-dev-scene")
        _add_issue(821, scene_id="")
        captured, patches = self._dispatch_patches()
        from contextlib import ExitStack
        with ExitStack() as stack:
            for p in patches:
                stack.enter_context(p)
            with patch("src.user.db.get_repo_scene", return_value="custom-dev-scene"):
                self._run(821)
        assert captured, "应派发 vod 推荐任务"
        assert captured[0]["agent_type"] == "vod"
        assert captured[0]["is_scene_recommend"] is True, \
            "有 dev_scene 但无真实绑定 → 不跳过推荐（get_issue_bound_scene 判定）"

    def test_task_center_created_issue_skips_recommend(self):
        """Task Center 建单（创建时已写 issue 表 scene_id）→ 命中真实绑定 → 跳过推荐。"""
        _set_repo_scene("custom-dev-scene")
        _add_issue(831, scene_id="task-center-scene")
        captured, patches = self._dispatch_patches()
        from contextlib import ExitStack
        with ExitStack() as stack:
            for p in patches:
                stack.enter_context(p)
            with patch("src.user.db.get_repo_scene", return_value="custom-dev-scene"):
                self._run(831)
        assert captured, "已有真实绑定仍应派发 vod（正常流程）"
        assert captured[0]["is_scene_recommend"] is False, \
            "issue 表 scene_id 真实绑定 → 跳过推荐（Task Center 行为不变）"

    def test_no_dev_scene_behavior_unchanged(self):
        """无真实绑定 + 无 dev_scene → 走推荐（未配 dev_scene 的仓行为不变）。"""
        _add_issue(841, scene_id="")
        captured, patches = self._dispatch_patches()
        from contextlib import ExitStack
        with ExitStack() as stack:
            for p in patches:
                stack.enter_context(p)
            with patch("src.user.db.get_repo_scene", return_value=""):
                self._run(841)
        assert captured
        assert captured[0]["is_scene_recommend"] is True
