"""回归测试：场景应用（apply_scene）不得重置仓库级 Agent 启用开关 enable。

背景：src/api/routes/scenes.py 的 INSERT OR REPLACE 遗漏 enable 列。INSERT OR REPLACE
删除旧行后插入新行，未提供的 enable 落到列默认 1，导致 sre（agent-description.json 全局
默认 false）在应用场景后被误开启（Prompt-to-Premiere 事故）。
修复后：已有会话保留仓库级 enable（用户 Dashboard 开关为准）；仅新建会话继承
agent_type 全局默认。
"""
import asyncio
import os
import sys
import uuid

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.environ["DEVOPS_DB_PATH"] = os.path.join(
    os.path.dirname(__file__), "..", "data", "test_sessions.db")

from src.session_manager import db, db_lock  # noqa: E402
from src.api.routes.scenes import apply_scene, ApplySceneRequest  # noqa: E402
from src.api.session_store import (  # noqa: E402
    get_agent_type_enable,
    get_or_create_agent_session,
    is_agent_enabled,
)

SCENE_ID = "default"


def _unique_repo() -> str:
    return f"scene-enable-{uuid.uuid4().hex[:10]}"


def _apply(owner: str, repo: str):
    return asyncio.run(apply_scene(owner, repo, ApplySceneRequest(scene_id=SCENE_ID), {}))


def _session_enable(repo_key: str, agent_type: str):
    with db_lock:
        c = db.cursor()
        row = c.execute(
            "SELECT enable FROM devops_agent_session WHERE repo_key=? AND agent_type=?",
            (repo_key, agent_type)).fetchone()
        return row[0] if row else None


def _set_session_enable(repo_key: str, agent_type: str, enable: int):
    with db_lock:
        c = db.cursor()
        c.execute("UPDATE devops_agent_session SET enable=? WHERE repo_key=? AND agent_type=?",
                  (enable, repo_key, agent_type))
        db.commit()


def _cleanup(repo_key: str):
    with db_lock:
        c = db.cursor()
        c.execute("DELETE FROM devops_agent_session WHERE repo_key=?", (repo_key,))
        db.commit()


class TestApplySceneKeepsEnable:
    def test_fresh_repo_sre_inherits_default_disabled(self):
        """新建仓库应用场景：sre 会话 enable 继承 agent_type 全局默认（0），不被列默认 1 误开启"""
        owner, repo = "sceneowner", _unique_repo()
        repo_key = f"gitcode:{owner}/{repo}"
        try:
            _apply(owner, repo)
            for at in ("vod", "issue", "coding", "qa", "sre"):
                assert _session_enable(repo_key, at) is not None, f"{at} session 未创建"
            sre_enable = _session_enable(repo_key, "sre")
            default_enable = get_agent_type_enable("sre")
            assert sre_enable == default_enable, (
                f"sre enable 应继承 agent_type 默认（{default_enable}），实际 {sre_enable}")
            assert sre_enable == 0, "agent-description.json 中 sre enable=false，默认应为关"
            assert is_agent_enabled(repo_key, "sre") is False, "sre 全局默认关，应不可用"
        finally:
            _cleanup(repo_key)

    def test_preserves_user_enabled_sre(self):
        """仓库级手动开启 sre 后应用场景：enable 保持 1，不被全局默认覆盖"""
        owner, repo = "sceneowner", _unique_repo()
        repo_key = f"gitcode:{owner}/{repo}"
        try:
            get_or_create_agent_session(repo_key, "sre", "opencode")
            _set_session_enable(repo_key, "sre", 1)
            _apply(owner, repo)
            assert _session_enable(repo_key, "sre") == 1, "仓库级已开启的 sre 不应被场景覆盖"
            assert is_agent_enabled(repo_key, "sre") is True
        finally:
            _cleanup(repo_key)

    def test_preserves_user_disabled_issue(self):
        """仓库级手动关闭 issue 后应用场景：enable 保持 0，不被列默认 1 误开启"""
        owner, repo = "sceneowner", _unique_repo()
        repo_key = f"gitcode:{owner}/{repo}"
        try:
            get_or_create_agent_session(repo_key, "issue", "opencode")
            _set_session_enable(repo_key, "issue", 0)
            _apply(owner, repo)
            assert _session_enable(repo_key, "issue") == 0, "仓库级已关闭的 issue 不应被场景重置"
            assert is_agent_enabled(repo_key, "issue") is False
        finally:
            _cleanup(repo_key)

    def test_other_agents_stay_enabled_on_fresh_repo(self):
        """新建仓库应用场景：vod/issue/coding/qa 仍为启用态（仅 sre 全局默认关）"""
        owner, repo = "sceneowner", _unique_repo()
        repo_key = f"gitcode:{owner}/{repo}"
        try:
            _apply(owner, repo)
            for at in ("vod", "issue", "coding", "qa"):
                assert _session_enable(repo_key, at) == 1, f"{at} 默认应启用"
                assert is_agent_enabled(repo_key, at) is True
        finally:
            _cleanup(repo_key)
