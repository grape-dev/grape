"""测试：解除托管同步清理（_cleanup_repo_data）"""
import sys, os, sqlite3
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.environ.setdefault("DEVOPS_DB_PATH", os.path.join(
    os.path.dirname(__file__), "..", "data", "test_sessions.db"))

import pytest
from src.api.routes.repos import _cleanup_repo_data

REPO = "owner/cleanup-test"


@pytest.fixture()
def clean_db():
    """清空测试库相关表，造数据"""
    from src.session_manager import db, db_lock
    with db_lock:
        c = db.cursor()
        for tbl in ("events", "event_skipped", "chat_conversations", "chat_messages",
                    "chat_agent_session", "repo_im_group_binding", "pipeline_instances",
                    "devops_agent_session", "vod_conversations", "skill"):
            try:
                c.execute(f"DELETE FROM {tbl}")
            except Exception:
                pass
        db.commit()
    yield db


def _insert(db, table, cols, vals):
    from src.session_manager import db_lock
    with db_lock:
        c = db.cursor()
        ph = ",".join("?" for _ in cols)
        c.execute(f"INSERT INTO {table} ({','.join(cols)}) VALUES ({ph})", vals)
        db.commit()


class TestCleanupRepoData:
    def test_clears_all_repo_tables(self, clean_db):
        # 造数据（含 gitcode: 前缀兼容 + 无前缀）
        _insert(clean_db, "events", ["repo_key", "event_type", "title", "payload", "status"],
                (REPO, "issue_opened", "t1", "{}", "completed"))
        _insert(clean_db, "events", ["repo_key", "event_type", "title", "payload", "status"],
                ("gitcode:" + REPO, "issue_comment", "t2", "{}", "completed"))
        _insert(clean_db, "event_skipped", ["repo_key", "event_type", "reason"], (REPO, "x", "no_route"))
        # chat_conversations + 关联 messages
        _insert(clean_db, "chat_conversations", ["id", "user_id", "repo_key"], ("c1", 1, REPO))
        _insert(clean_db, "chat_messages", ["conversation_id", "role", "content"], ("c1", "user", "hi"))
        _insert(clean_db, "chat_agent_session", ["id", "repo_key"], ("s1", REPO))
        _insert(clean_db, "repo_im_group_binding", ["im_chat_id", "repo_key"], ("g1", REPO))
        # pipeline_instances 仅生产库存在（测试库缺失时跳过）
        from src.session_manager import db_lock
        with db_lock:
            _tbls = [r[0] for r in clean_db.cursor().execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()]
        if "pipeline_instances" in _tbls:
            _insert(clean_db, "pipeline_instances", ["id", "repo_key"], (1, REPO))
        # devops_agent_session + 关联 vod_conversations
        _insert(clean_db, "devops_agent_session", ["id", "repo_key", "agent_type"], ("a1", REPO, "vod"))
        _insert(clean_db, "vod_conversations", ["agent_session_id", "im_chat_id"], ("a1", "v1"))
        # repo skill
        _insert(clean_db, "skill", ["skill_name", "scope", "owner_id"], ("custom-x", "repo", REPO))

        owner, repo = REPO.split("/")
        _cleanup_repo_data(REPO, owner, repo)

        from src.session_manager import db_lock
        with db_lock:
            c = clean_db.cursor()
            _tbls2 = [r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()]
            for tbl in ("events", "event_skipped", "chat_conversations", "chat_agent_session",
                        "repo_im_group_binding", "devops_agent_session"):
                cnt = c.execute(f"SELECT COUNT(*) FROM {tbl} WHERE repo_key=? OR repo_key=?",
                                (REPO, "gitcode:" + REPO)).fetchone()[0]
                assert cnt == 0, f"{tbl} not cleared: {cnt}"
            # chat_messages 无 repo_key 列，通过关联的 conversation_id 验证
            cnt = c.execute("SELECT COUNT(*) FROM chat_messages WHERE conversation_id='c1'").fetchone()[0]
            assert cnt == 0, f"chat_messages not cleared: {cnt}"
            # vod_conversations 无 repo_key 列，通过关联的 agent_session_id 验证
            cnt = c.execute("SELECT COUNT(*) FROM vod_conversations WHERE agent_session_id='a1'").fetchone()[0]
            assert cnt == 0, f"vod_conversations not cleared: {cnt}"
            if "pipeline_instances" in _tbls2:
                cnt = c.execute("SELECT COUNT(*) FROM pipeline_instances WHERE repo_key=? OR repo_key=?",
                                (REPO, "gitcode:" + REPO)).fetchone()[0]
                assert cnt == 0, f"pipeline_instances not cleared: {cnt}"
            # skill repo scope 记录清理（按 owner_id）
            cnt = c.execute("SELECT COUNT(*) FROM skill WHERE scope='repo' AND owner_id=?",
                            (REPO,)).fetchone()[0]
            assert cnt == 0, f"repo skill not cleared: {cnt}"

    def test_does_not_clear_other_repo_or_user_data(self, clean_db):
        # 其他仓库 + 用户维度数据应保留
        _insert(clean_db, "events", ["repo_key", "event_type", "title", "payload", "status"],
                ("other/repo", "push", "keep", "{}", "completed"))
        _insert(clean_db, "chat_conversations", ["id", "user_id", "repo_key"], ("keep1", 1, "other/repo"))
        _insert(clean_db, "chat_messages", ["conversation_id", "role", "content"], ("keep1", "user", "keep"))
        _insert(clean_db, "devops_agent_session", ["id", "repo_key", "agent_type"], ("keepa", "other/repo", "issue"))

        owner, repo = REPO.split("/")
        _cleanup_repo_data(REPO, owner, repo)

        from src.session_manager import db_lock
        with db_lock:
            c = clean_db.cursor()
            assert c.execute("SELECT COUNT(*) FROM events WHERE repo_key='other/repo'").fetchone()[0] == 1
            assert c.execute("SELECT COUNT(*) FROM chat_conversations WHERE repo_key='other/repo'").fetchone()[0] == 1
            assert c.execute("SELECT COUNT(*) FROM chat_messages WHERE conversation_id='keep1'").fetchone()[0] == 1
            assert c.execute("SELECT COUNT(*) FROM devops_agent_session WHERE repo_key='other/repo'").fetchone()[0] == 1

    def test_invalid_owner_repo_skipped(self, clean_db):
        # 路径穿越防护：非法 repo 名不删除
        _insert(clean_db, "events", ["repo_key", "event_type", "title", "payload", "status"],
                ("evil/../etc", "push", "x", "{}", "completed"))
        _cleanup_repo_data("evil/../etc", "evil", "../etc")
        from src.session_manager import db_lock
        with db_lock:
            c = clean_db.cursor()
            assert c.execute("SELECT COUNT(*) FROM events WHERE repo_key='evil/../etc'").fetchone()[0] == 1

    def test_workspace_and_skill_dirs_removed(self, clean_db, tmp_path, monkeypatch):
        """验证 workspace 目录 + 仓库 skill 目录被删除，owner 根保留"""
        import shutil, os
        from unittest.mock import patch

        # 模拟 workspace 结构
        ws = tmp_path / "workspaces"
        (ws / "owner" / "cleanup-test" / "issue-agent").mkdir(parents=True)
        (ws / "cleanup-test" / "vod-agent").mkdir(parents=True)   # 平级 vod-agent 目录
        (ws / "feishu" / "owner_cleanup-test").mkdir(parents=True)
        (ws / "vod" / "owner_cleanup-test").mkdir(parents=True)
        (ws / "history" / "owner" / "cleanup-test").mkdir(parents=True)
        # owner 根下其他 repo（应保留）
        (ws / "owner" / "other-repo").mkdir(parents=True)
        # 仓库 skill 目录
        skills = tmp_path / "skills"
        (skills / "repo" / "owner" / "cleanup-test" / "custom-x").mkdir(parents=True)
        (skills / "repo" / "owner" / "other-repo" / "y").mkdir(parents=True)

        # 记录真实 WORKSPACE_DIR/SKILL_DIR 并替换
        import src.api.routes.repos as repos_mod
        real_ws = None
        real_skill = None
        with patch("src.config.WORKSPACE_DIR", str(ws)), \
             patch("src.user.db.SKILL_DIR", str(skills / "custom")):
            _cleanup_repo_data("owner/cleanup-test", "owner", "cleanup-test")

        # 目标仓库的目录应被删除
        for p in [(ws / "owner" / "cleanup-test"), (ws / "cleanup-test"),
                  (ws / "feishu" / "owner_cleanup-test"), (ws / "vod" / "owner_cleanup-test"),
                  (ws / "history" / "owner" / "cleanup-test")]:
            assert not p.exists(), f"should be removed: {p}"
        # 仓库 skill 目录删除
        assert not (skills / "repo" / "owner" / "cleanup-test").exists()
        # owner 根下其他 repo 保留
        assert (ws / "owner" / "other-repo").exists()
        assert (skills / "repo" / "owner" / "other-repo").exists()
