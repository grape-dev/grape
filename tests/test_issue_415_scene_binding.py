"""Issue #415: ChatAgent/VOD 对话创建 Issue 场景绑定修复测试。

覆盖 Spec 3 条路径：
1. get_issue_scene 空值回退仓库默认场景（managed_repos.dev_scene）
2. create_task_via_issue 无显式场景 → 写入仓库默认场景
3. _query_tasks 空场景 → 仓库默认场景展示 + 补绑脚本 dry-run
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest  # noqa: E402
from unittest.mock import patch, MagicMock  # noqa: E402

from src.session_manager import db, db_lock  # noqa: E402

REPO = "gitcode:o/r"


@pytest.fixture(scope="session", autouse=True)
def _ensure_tables():
    """先 init 全部基础表（issue 等），供场景回退/查询测试依赖。"""
    from src.api import db_setup
    db_setup.init_database()


@pytest.fixture(autouse=True)
def _clean(data=None):
    from src.session_manager import db, db_lock
    with db_lock:
        c = db.cursor()
        try:
            c.execute("DELETE FROM issue WHERE repo_key LIKE 'gitcode:o/r%'")
        except Exception:
            pass
        try:
            c.execute("DELETE FROM issue_state WHERE repo_key LIKE 'gitcode:o/r%'")
        except Exception:
            pass
        try:
            c.execute("DELETE FROM events WHERE repo_key LIKE 'gitcode:o/r%'")
        except Exception:
            pass
        try:
            c.execute("DELETE FROM managed_repos WHERE repo_key LIKE 'gitcode:o/r%'")
        except Exception:
            pass
        db.commit()
    yield


def _add_issue(repo_key, number, title="t", scene_id=""):
    with db_lock:
        c = db.cursor()
        c.execute(
            "INSERT INTO issue (user_id, assignee, repo_key, issue_number, title, status, scene_id) "
            "VALUES (1, 'tester', ?, ?, ?, 'active', ?)",
            (repo_key, number, title, scene_id))
        db.commit()


def _set_repo_scene(repo_key, scene_id):
    from src.user.db import add_managed_repo, set_repo_scene
    add_managed_repo(repo_key, "o", "r")
    set_repo_scene(repo_key, scene_id)


class TestGetIssueSceneFallback:
    def test_falls_back_to_repo_default_scene(self):
        """Issue 无场景 + 仓库有默认场景 → 返回仓库默认场景。"""
        from src.api.session_store import get_issue_scene
        _add_issue(REPO, 901, scene_id="")
        with patch("src.user.db.get_repo_scene", return_value="cloud-native"):
            scene, uid = get_issue_scene(REPO, "#901")
        assert scene == "cloud-native"
        assert uid == 0

    def test_no_repo_default_keeps_empty(self):
        """Issue 无场景 + 仓库无默认场景 → 保持 ("", 0)（走智能推荐）。"""
        from src.api.session_store import get_issue_scene
        _add_issue(REPO, 902, scene_id="")
        with patch("src.user.db.get_repo_scene", return_value=""):
            scene, uid = get_issue_scene(REPO, "#902")
        assert scene == ""
        assert uid == 0

    def test_issue_scene_still_wins(self):
        """Issue 级显式场景优先于仓库默认场景。"""
        from src.api.session_store import get_issue_scene
        _add_issue(REPO, 903, scene_id="issue-scene")
        with patch("src.user.db.get_repo_scene", return_value="cloud-native"):
            scene, uid = get_issue_scene(REPO, "#903")
        assert scene == "issue-scene"


class TestCreateTaskSceneFallback:
    def _create(self, scene_id):
        import asyncio
        from src.api.issues_center import create_task_via_issue, TaskCreateBody
        _calls = []
        real_set = None
        from src.api import session_store as _ss
        # Issue #1245 起 create_task 会对场景做可用性预校验；本用例只验证场景绑定，
        # 预校验在测试库无技能数据下会误报，这里打桩放行以免干扰绑定语义。
        _pc_ok = {"ok": True, "problems": [], "warnings": [], "trace_id": "pc_test"}
        with patch("src.api.issues_center._find_open_issue_by_title", return_value=None), \
             patch("src.api.issues_center._post_gitcode_issue", return_value={"number": 911}), \
             patch("src.api.deps.get_user_token_with_refresh", return_value="fake_token"), \
             patch("src.api.issues_center._get_user_id_by_gitcode", return_value=1), \
             patch("src.api.scene_precheck.precheck_scene", return_value=_pc_ok), \
             patch("src.api.event_pipeline.run_pipeline", return_value={"seq": 0, "ok": True}):
            def _fake_set_issue_scene(rk, iid, sid, scene_user_id=0):
                _calls.append((rk, iid, sid, scene_user_id))
            with patch.object(_ss, "set_issue_scene", side_effect=_fake_set_issue_scene):
                asyncio.run(create_task_via_issue(
                    TaskCreateBody(repo_key=REPO, title="T415", description="d", scene_id=scene_id),
                    {"id": 1, "username": "tester", "auth_type": "jwt"}))
        return _calls

    def test_empty_scene_writes_repo_default(self):
        """无显式场景（空）+ 仓库有默认场景 → 写入仓库默认场景。"""
        _set_repo_scene(REPO, "cloud-native")
        calls = self._create("")
        assert calls, "set_issue_scene 应被调用"
        rk, iid, sid, uid = calls[-1]
        assert rk == REPO
        assert iid == "#911"
        assert sid == "cloud-native"

    def test_vod_auto_writes_repo_default(self):
        """智能推荐标记（vod-auto）+ 仓库有默认场景 → 写入仓库默认场景。"""
        _set_repo_scene(REPO, "cloud-native")
        calls = self._create("vod-auto")
        assert calls and calls[-1][2] == "cloud-native"

    def test_explicit_scene_wins(self):
        """显式 scene_id → 写入显式场景（原逻辑保持）。"""
        _set_repo_scene(REPO, "cloud-native")
        calls = self._create("explicit-scene")
        assert calls and calls[-1][2] == "explicit-scene"

    def test_empty_scene_no_repo_default_no_write(self):
        """无显式场景 + 仓库无默认场景 → 不写 scene（走智能推荐）。"""
        calls = self._create("")
        assert calls == []


class TestQueryTasksRepoDefaultDisplay:
    def test_query_tasks_uses_repo_default_scene(self):
        """无场景 Issue 列表/详情 → scene_id 显示仓库默认场景 + repo_default 标记。"""
        _set_repo_scene(REPO, "cloud-native")
        _add_issue(REPO, 920, title="NoScene", scene_id="")
        from src.api.issues_center import _query_tasks
        tasks = _query_tasks("repo_key=?", [REPO], "id DESC")
        it = next((t for t in tasks if t["issue_number"] == 920), None)
        assert it is not None, "query 应含 #920"
        assert it["scene_id"] == "cloud-native", f"scene_id 应回退仓库默认场景, got {it['scene_id']!r}"
        assert it["scene_source"] == "repo_default"

    def test_query_tasks_issue_scene_wins(self):
        """Issue 级场景存在 → 显示 Issue 级场景（vod 标记）。"""
        _set_repo_scene(REPO, "cloud-native")
        _add_issue(REPO, 921, title="HasScene", scene_id="issue-scene")
        from src.api.issues_center import _query_tasks
        tasks = _query_tasks("repo_key=?", [REPO], "id DESC")
        it = next((t for t in tasks if t["issue_number"] == 921), None)
        assert it is not None
        assert it["scene_id"] == "issue-scene"
        assert it["scene_source"] == "vod"


class TestBackfillScript:
    def test_dry_run_lists_candidates(self):
        """补绑脚本 dry-run：列出无场景 Issue 与目标场景，不写库。"""
        _set_repo_scene(REPO, "cloud-native")
        _add_issue(REPO, 930, title="Legacy", scene_id="")
        from scripts.backfill_issue_scene import candidates
        rows = candidates()
        hit = next((r for r in rows if r["issue_number"] == 930), None)
        assert hit is not None, "dry-run 应列出 #930"
        assert hit["target_scene"] == "cloud-native"
        # 未 apply → issue.scene_id 仍然为空
        with db_lock:
            c = db.cursor()
            c.execute("SELECT scene_id FROM issue WHERE repo_key=? AND issue_number=?", (REPO, 930))
            row = c.fetchone()
        assert (row[0] or "") == "", "dry-run 不应写库"

    def test_apply_binds_repo_default(self):
        """补绑脚本 --apply：无场景 Issue 写入仓库默认场景。"""
        _set_repo_scene(REPO, "cloud-native")
        _add_issue(REPO, 931, title="Legacy2", scene_id="")
        from scripts.backfill_issue_scene import apply
        n = apply()
        assert n >= 1
        from src.api.session_store import get_issue_scene
        scene, _ = get_issue_scene(REPO, "#931")
        assert scene == "cloud-native"