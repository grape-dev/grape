"""Issue #2555：A2A 跳过分支统一返回 ok/skipped/reason/detail。

覆盖验收标准：
- task_cancelled 带 ok=false / 中文 reason / detail.issue_number+repository；
- 仓库不匹配且单号在他仓存在 → repo_mismatch，detail 三字段齐全且不返回 task_cancelled；
- 本仓与他仓均不存在 → 不因归属校验新增拒绝（行为不变）；
- 跨流水线两类拦截均带 reason，skipped 码/状态码不变；
- 向后兼容：既有键 status/seq/agent/handoff_result/source_seq/original_event 仍在。
"""
import os

ROOT = os.path.normpath(os.path.join(os.path.dirname(__file__), "..", ".."))
os.environ["DEVOPS_DB_PATH"] = os.path.join(ROOT, "data", "test_issue_2555_skip_reason.db")
os.environ["DEVOPS_DB_BACKEND"] = "sqlite"
for _k in ("DEVOPS_DB_HOST", "DEVOPS_DB_USER", "DEVOPS_DB_PASSWORD", "DEVOPS_DB_NAME", "DEVOPS_DB_PORT"):
    os.environ.pop(_k, None)

from src.api.db_setup import init_database
from src.domains.runtime.routes.webhook import (
    _a2a_skip_reason,
    _a2a_skip_response,
    _issue_repo_mismatch,
    _repo_key_to_repository,
)
from src.kernel.session_manager import db, db_lock

init_database()

REPO = "gitcode:owner/repo"
OTHER_REPO = "gitcode:other/repo"
REPOS = (REPO, OTHER_REPO)


def _clean():
    with db_lock:
        c = db.cursor()
        for _repo in REPOS:
            c.execute("DELETE FROM issue WHERE repo_key=?", (_repo,))
        db.commit()


def _set_issue(repo_key, number, status="processing"):
    with db_lock:
        c = db.cursor()
        c.execute(
            "INSERT INTO issue (user_id, assignee, repo_key, issue_number, status, scene_id) "
            "VALUES (0, '', ?, ?, ?, '')",
            (repo_key, number, status))
        db.commit()


def _client():
    from fastapi.testclient import TestClient

    from src.api.main import app
    return TestClient(app)


def _body(issue_number="2555", repository="owner/repo", agent="coding"):
    return {
        "agent": agent,
        "payload": {
            "repository": repository,
            "account": "owner/repo",
            "issue_number": str(issue_number),
            "pr_number": "",
            "context": "Issue #2555 跳过原因测试",
        },
    }


class TestSkipResponseHelper:
    def setup_method(self):
        _clean()

    def test_helper_carries_all_skip_fields_and_existing_keys(self):
        resp = _a2a_skip_response(
            "pr_subscription_a2a_disabled", agent="qa", source_seq=7,
            detail={"issue_number": 2555},
            original_event={"seq": 3})
        body = resp.body
        import json
        data = json.loads(body)
        assert resp.status_code == 200
        assert data["ok"] is False
        assert data["skipped"] == "pr_subscription_a2a_disabled"
        assert data.get("reason")
        assert data["detail"] == {"issue_number": 2555}
        # 既有键保留
        assert data["handoff_result"] == "rejected"
        assert data["source_seq"] == 7
        assert data["status"] == "ok"
        assert data["seq"] == 0
        assert data["agent"] == "qa"
        assert data["original_event"] == {"seq": 3}

    def test_helper_extra_cannot_override_contract_fields(self):
        import json
        resp = _a2a_skip_response(
            "not_in_scene", agent="coding", status="skipped", status_code=409,
            extra={"status": "ok", "skipped": "other", "reason": "x",
                   "detail": {"leak": 1}, "source_seq": 99,
                   "scene": "s1", "message": "m"})
        data = json.loads(resp.body)
        assert resp.status_code == 409
        # 契约字段由构造器参数决定，extra 只能透传分支特有条目
        assert data["status"] == "skipped"
        assert data["skipped"] == "not_in_scene"
        assert data["reason"] and data["reason"] != "x"
        assert data["detail"] == {}
        assert data["source_seq"] == 0
        assert data["scene"] == "s1"
        assert data["message"] == "m"
        assert data["handoff_result"] == "rejected"

    def test_reasons_are_chinese_and_do_not_leak_internal_ids(self):
        scene_reason = _a2a_skip_reason("target_scene_not_found")
        node_reason = _a2a_skip_reason("target_scene_missing_node")
        assert "目标场景" in scene_reason and "目标场景" in node_reason
        assert "scene" not in scene_reason.lower()
        assert "target_node" not in node_reason
        assert _a2a_skip_reason("sre_subscribed")
        assert _a2a_skip_reason("pr_subscription_a2a_disabled")
        assert "不参与流水线流转" in _a2a_skip_reason("pr_subscription_a2a_disabled")

    def test_repo_key_to_repository_strips_provider(self):
        assert _repo_key_to_repository("gitcode:owner/repo") == "owner/repo"
        assert _repo_key_to_repository("owner/repo") == "owner/repo"

    def test_issue_repo_mismatch_requires_other_repo_evidence(self):
        _set_issue(OTHER_REPO, 2555)
        assert _issue_repo_mismatch(REPO, 2555) == OTHER_REPO
        # 本仓存在时调用方不会调用本函数；他仓不存在时返回空（不误拒）
        assert _issue_repo_mismatch("gitcode:absent/repo", 99999) == ""


class TestCancelledEndpoint:
    def setup_method(self):
        _clean()

    def test_task_cancelled_carries_ok_reason_detail(self):
        _set_issue(REPO, 2555, status="cancelled")
        resp = _client().post("/api/a2a", json=_body())
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["ok"] is False
        assert data["skipped"] == "task_cancelled"
        assert data["reason"] and "cancelled" in data["reason"]
        assert data["detail"]["issue_number"] == 2555
        assert data["detail"]["repository"] == "owner/repo"
        assert data["detail"]["status"] == "cancelled"
        # 向后兼容：既有键仍在
        assert data["handoff_result"] == "rejected"
        assert data["source_seq"] == 0


class TestRepoMismatchEndpoint:
    def setup_method(self):
        _clean()

    def test_repo_mismatch_when_only_other_repo_has_issue(self):
        from unittest.mock import patch
        _set_issue(OTHER_REPO, 2555, status="cancelled")
        # Issue #2555 第 2 轮：归属还需本仓 VCS 复核 404 才成立（避免 DB 未同步误拒）
        with patch("src.domains.runtime.routes.webhook._a2a_issue_vcs_check",
                   return_value="not_found"):
            resp = _client().post("/api/a2a", json=_body())
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["ok"] is False
        assert data["skipped"] == "repo_mismatch"
        assert data["skipped"] != "task_cancelled"
        assert data["detail"]["issue_number"] == 2555
        assert data["detail"]["expected_repository"] == "other/repo"
        assert data["detail"]["actual_repository"] == "owner/repo"
        assert "other/repo" in data["reason"] and "owner/repo" in data["reason"]

    def test_repo_mismatch_skipped_when_local_vcs_has_the_issue(self):
        """本仓 DB 未同步、但 VCS 命中（新建单常态）→ 不得判 repo_mismatch。"""
        from unittest.mock import patch
        _set_issue(OTHER_REPO, 2555, status="cancelled")
        with patch("src.domains.runtime.routes.webhook._a2a_issue_vcs_check",
                   return_value="exists"):
            resp = _client().post("/api/a2a", json=_body())
        assert resp.json().get("skipped") != "repo_mismatch", resp.text

    def test_repo_mismatch_skipped_when_vcs_unverifiable(self):
        """VCS 不可判定（无 token / 网络）→ 不误拒，沿用既有口径。"""
        from unittest.mock import patch
        _set_issue(OTHER_REPO, 2555, status="cancelled")
        with patch("src.domains.runtime.routes.webhook._a2a_issue_vcs_check",
                   return_value="unknown"):
            resp = _client().post("/api/a2a", json=_body())
        assert resp.json().get("skipped") != "repo_mismatch", resp.text

    def test_absent_in_all_repos_does_not_repo_mismatch(self):
        resp = _client().post("/api/a2a", json=_body(issue_number="987654"))
        assert resp.json().get("skipped") != "repo_mismatch"
        assert resp.json().get("skipped") != "task_cancelled"


class TestDedupAndGuardSkipResponses:
    """Issue #2555：既有去重/门禁跳过分支也必须统一带 ok=false / reason / detail。"""

    def test_duplicate_a2a_has_ok_reason_detail(self):
        import json

        from src.domains.runtime.routes.webhook import _duplicate_a2a_skip_response
        _set_issue(REPO, 2555)
        resp = _duplicate_a2a_skip_response("qa", REPO, 42)
        data = json.loads(resp.body)
        assert data["ok"] is False
        assert data["skipped"] == "duplicate_a2a"
        assert data["reason"] == "active_a2a_event"
        assert data["detail"]["existing_seq"] == 42
        assert data["handoff_result"] == "rejected"

    def test_guard_skip_has_ok_reason_detail(self):
        import json
        from unittest.mock import patch

        from src.domains.runtime.routes.webhook import _a2a_guard_skip_response
        with patch("src.api.a2a_dispatch_guard.record_skip"), \
                patch("src.api.a2a_dispatch_guard.detect_dispatch_storm"):
            resp = _a2a_guard_skip_response(
                REPO, 2555, "coding", "scene-1", "duplicate_key",
                {"seq": 42, "status": "completed"})
        data = json.loads(resp.body)
        assert data["ok"] is False
        assert data["skipped"] == "duplicate_key"
        assert data["reason"] == "duplicate_key:42"
        assert data["detail"]["existing_seq"] == 42
        assert data["detail"]["status"] == "completed"


class TestCrossPipelineEndpoint:
    """Issue #2555：跨场景拦截端点级断言 target_node / reason / detail。"""

    def setup_method(self):
        _clean()

    def _scene_body(self, target_node="vod"):
        body = _body()
        body["payload"]["target_scene_id"] = "missing-scene"
        body["payload"]["target_node"] = target_node
        return body

    def test_target_scene_not_found_has_ok_reason_detail(self):
        from unittest.mock import patch
        with patch("src.api.pipeline_config.get_pipeline_config", return_value=None):
            resp = _client().post("/api/a2a", json=self._scene_body())
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["ok"] is False
        assert data["skipped"] == "target_scene_not_found"
        assert data["reason"] and "目标场景" in data["reason"]
        assert data["detail"]["target_node"] == "vod"

    def test_target_scene_missing_node_has_ok_reason_detail(self):
        from unittest.mock import patch
        _cfg = {"display_name": "target", "nodes": [{"id": "issue"}]}
        with patch("src.api.pipeline_config.get_pipeline_config", return_value=_cfg):
            resp = _client().post("/api/a2a", json=self._scene_body(target_node="vod"))
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["ok"] is False
        assert data["skipped"] == "target_scene_missing_node"
        assert data["reason"] and "目标场景" in data["reason"]
        assert data["detail"]["target_node"] == "vod"


class TestNotInSceneEndpoint:
    """Issue #2555：not_in_scene 端点级 409 断言 ok/reason/detail.agent。"""

    def setup_method(self):
        _clean()

    def test_not_in_scene_has_ok_reason_detail_agent(self):
        from unittest.mock import patch
        _set_issue(REPO, 2555)
        _ck = {"in_scene": False, "scene_id": "scene-2427", "nodes": []}
        _session = {"session_key": "sess-nis-2555", "user_id": 0, "username": ""}
        with patch("src.domains.runtime.routes.webhook._authorize_a2a", return_value=_session), \
                patch("src.domains.runtime.routes.webhook._a2a_repo_fallback_check", return_value="ok"), \
                patch("src.domains.runtime.routes.webhook._a2a_register_repo"), \
                patch("src.domains.runtime.routes.webhook._scene_node_check", return_value=_ck):
            resp = _client().post("/api/a2a", json=_body(agent="coding"))
        assert resp.status_code == 409, resp.text
        data = resp.json()
        assert data["ok"] is False
        assert data["skipped"] == "not_in_scene"
        assert data["reason"] and "目标 Agent" in data["reason"]
        assert data["detail"] == {"agent": "coding"}
        assert data["status"] == "skipped"
        assert data["handoff_result"] == "rejected"
        # Issue #2427：scene 与 message 同源携带 scene_id
        assert data["scene"] == "scene-2427"
        assert "scene=scene-2427" in data["message"]

    def test_not_in_scene_with_non_empty_nodes_keeps_409(self):
        """Issue #2555/#2427：场景有节点但缺目标 agent 时仍是 409 skip，不得退化为 500。

        上一版用例把 nodes mock 成空列表，`_node_id` 从未被求值，掩盖了
        未定义符号导致的 NameError → 500。这里用真实形态（场景有节点、缺目标 agent）。
        """
        from unittest.mock import patch
        _set_issue(REPO, 2555)
        _ck = {"in_scene": False, "scene_id": "scene-2427",
               "nodes": [{"id": "vod", "tag": "vod"}, {"id": "issue", "tag": "issue"}]}
        _session = {"session_key": "sess-nis-2555-nodes", "user_id": 0, "username": ""}
        with patch("src.domains.runtime.routes.webhook._authorize_a2a", return_value=_session), \
                patch("src.domains.runtime.routes.webhook._a2a_repo_fallback_check", return_value="ok"), \
                patch("src.domains.runtime.routes.webhook._a2a_register_repo"), \
                patch("src.domains.runtime.routes.webhook._scene_node_check", return_value=_ck):
            resp = _client().post("/api/a2a", json=_body(agent="coding"))
        assert resp.status_code == 409, resp.text
        data = resp.json()
        assert data["ok"] is False
        assert data["skipped"] == "not_in_scene"
        assert data["detail"] == {"agent": "coding"}
        assert data["handoff_result"] == "rejected"
        assert "nodes=['vod', 'issue']" in data["message"]

    def test_node_id_falls_back_to_tag(self):
        """Issue #2427：仅有 tag 的存量节点也要能被识别（in_scene 走同一解析）。"""
        from src.domains.runtime.routes.webhook import _node_id
        assert _node_id({"id": "coding", "tag": "coding"}) == "coding"
        assert _node_id({"tag": "coding"}) == "coding"
        assert _node_id({}) == ""
        assert _node_id("coding") == ""


class TestSubscriptionEndpoint:
    """Issue #2555：PR 订阅 / SRE 订阅跳过端点级 reason。"""

    def setup_method(self):
        _clean()

    def test_pr_subscription_a2a_disabled_has_reason(self):
        from unittest.mock import patch
        _set_issue(REPO, 2555)
        with db_lock:
            c = db.cursor()
            c.execute("DELETE FROM agent_tasks WHERE id=?", ("sess-prsub-2555",))
            c.execute(
                "INSERT INTO agent_tasks (id, agent_session_id, runtime_id, prompt, issue_id, repo_key) "
                "VALUES (?, 'sess-row', 0, 'x', '#pr_77', ?)",
                ("sess-prsub-2555", REPO))
            db.commit()
        _session = {"session_key": "sess-prsub-2555", "user_id": 0, "username": ""}
        with patch("src.domains.runtime.routes.webhook._authorize_a2a", return_value=_session), \
                patch("src.domains.runtime.routes.webhook._a2a_repo_fallback_check", return_value="ok"), \
                patch("src.domains.runtime.routes.webhook._a2a_register_repo"):
            resp = _client().post("/api/a2a", json=_body())
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["ok"] is False
        assert data["skipped"] == "pr_subscription_a2a_disabled"
        assert data["reason"] and "PR 订阅" in data["reason"]
        assert data["detail"]["issue_number"] == 2555

    def test_sre_subscribed_has_reason(self):
        from unittest.mock import patch
        _set_issue(REPO, 2555)
        with db_lock:
            c = db.cursor()
            c.execute("DELETE FROM devops_agent_session WHERE repo_key=? AND agent_type='sre'", (REPO,))
            c.execute(
                "INSERT OR REPLACE INTO devops_agent_session "
                "(id, repo_key, agent_type, runtime_id, skill, status, last_active_at, enable, subscribe_events) "
                "VALUES (?,?,?,?,?,?,datetime('now'),1,?)",
                ("sre-2555", REPO, "sre", 0, "sre-cli", "running", '["pr_merged"]'))
            db.commit()
        _session = {"session_key": "sess-sre-2555", "user_id": 0, "username": ""}
        body = _body(agent="sre")
        body["payload"]["pr_number"] = "1433"
        with patch("src.domains.runtime.routes.webhook._authorize_a2a", return_value=_session), \
                patch("src.domains.runtime.routes.webhook._a2a_repo_fallback_check", return_value="ok"), \
                patch("src.domains.runtime.routes.webhook._a2a_register_repo"):
            resp = _client().post("/api/a2a", json=body)
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["ok"] is False
        assert data["skipped"] == "sre_subscribed"
        assert data["reason"] and "SRE 已订阅" in data["reason"]
        assert data["detail"]["issue_number"] == 2555
