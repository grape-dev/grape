"""回归测试：devops_agent_session.id 稳定性 + 飞书绑定（im 三字段）保留。

背景（2026-08 事故）：scenes.apply_scene / session_store.ensure_default_agent_sessions /
agents.update_agent_config 三处原本用「新 uuid + INSERT OR REPLACE」写 devops_agent_session，
REPLACE 遇 UNIQUE(repo_key, agent_type) 冲突会删旧行插新行 → id 每次重存都换新，
导致 vod_conversations.agent_session_id 悬空，飞书 VOD 收到消息时报
"[VOD:...] agent_session_id=... not found, auto-rebind"。
且 apply_scene 的 INSERT 不含 im_app_id/im_app_secret/im_provider，REPLACE 会清空飞书绑定。

修复：新增 session_store.upsert_agent_session 统一「已存在→UPDATE 保留 id；不存在→INSERT」，
im 三字段仅显式传值才写。三处全部改用它。
"""
import asyncio
import os
import sys
import unittest
import uuid

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.environ["DEVOPS_DB_PATH"] = os.path.join(
    os.path.dirname(__file__), "..", "data", "test_sessions.db")

from src.session_manager import db, db_lock  # noqa: E402
from src.api.session_store import upsert_agent_session  # noqa: E402


def _unique() -> str:
    return f"sess-id-{uuid.uuid4().hex[:10]}"


def _cleanup(repo_key: str):
    with db_lock:
        c = db.cursor()
        c.execute("DELETE FROM devops_agent_session WHERE repo_key=?", (repo_key,))
        c.execute("DELETE FROM vod_conversations WHERE agent_session_id NOT IN "
                  "(SELECT id FROM devops_agent_session)")
        db.commit()


def _get_session(repo_key: str, agent_type: str):
    with db_lock:
        c = db.cursor()
        return c.execute(
            "SELECT id, im_app_id, im_app_secret, im_provider, enable, subscribe_events "
            "FROM devops_agent_session WHERE repo_key=? AND agent_type=?",
            (repo_key, agent_type)).fetchone()


class TestUpsertKeepsSessionId:
    def test_update_preserves_id_and_im_fields(self):
        """已存在会话：重复 upsert 保留 id 与 im 三字段，仅更新显式传入的字段"""
        repo = _unique()
        try:
            sid1 = upsert_agent_session(
                repo_key=repo, agent_type="vod", runtime_id=1, skill="vod",
                im_app_id="cli_abc", im_app_secret="sec", im_provider="feishu")
            sid2 = upsert_agent_session(
                repo_key=repo, agent_type="vod", runtime_id=2, skill="vod-new",
                im_app_id=None, im_app_secret=None, im_provider=None)
            assert sid1 == sid2, "重复 upsert 不得更换会话 id"
            row = _get_session(repo, "vod")
            assert row[0] == sid1
            assert row[1] == "cli_abc", "im_app_id 应保留"
            assert row[2] == "sec", "im_app_secret 应保留"
            assert row[3] == "feishu", "im_provider 应保留"
            with db_lock:
                c = db.cursor()
                r = c.execute("SELECT runtime_id, skill FROM devops_agent_session WHERE id=?", (sid1,)).fetchone()
            assert r[0] == 2 and r[1] == "vod-new", "显式传入字段应更新"
        finally:
            _cleanup(repo)

    def test_explicit_im_overwrites(self):
        """显式传 im_app_id 时才覆盖（换绑场景）"""
        repo = _unique()
        try:
            sid = upsert_agent_session(
                repo_key=repo, agent_type="vod", im_app_id="cli_old", im_app_secret="s",
                im_provider="feishu")
            upsert_agent_session(
                repo_key=repo, agent_type="vod", im_app_id="cli_new", im_app_secret="s2",
                im_provider="feishu")
            row = _get_session(repo, "vod")
            assert row[0] == sid
            assert row[1] == "cli_new" and row[2] == "s2"
        finally:
            _cleanup(repo)

    def test_conversation_stays_valid_after_reupsert(self):
        """核心回归：会话重复保存后，vod_conversations 外键引用仍有效"""
        repo = _unique()
        try:
            sid = upsert_agent_session(repo_key=repo, agent_type="vod")
            with db_lock:
                c = db.cursor()
                c.execute("INSERT INTO vod_conversations (agent_session_id, im_chat_id) VALUES (?,?)",
                          (sid, "oc_regression"))
                db.commit()
            # 模拟 apply_scene / ensure_default 再写一次（不传 im）
            upsert_agent_session(repo_key=repo, agent_type="vod", runtime_id=3, skill="vod")
            with db_lock:
                c = db.cursor()
                conv = c.execute("SELECT agent_session_id FROM vod_conversations WHERE im_chat_id='oc_regression'").fetchone()
            assert conv and conv[0] == sid, "vod_conversations.agent_session_id 不应悬空"
        finally:
            _cleanup(repo)


class TestApplySceneKeepsSessionId:
    def _apply(self, owner: str, repo: str):
        from src.api.routes.scenes import apply_scene, ApplySceneRequest
        return asyncio.run(apply_scene(owner, repo, ApplySceneRequest(scene_id="default"), {}))

    def test_apply_twice_keeps_id_and_im(self):
        """apply_scene 重复应用：id 不变、vod 飞书绑定不清空、enable/订阅保留"""
        owner, repo = "sessowner", _unique()
        repo_key = f"gitcode:{owner}/{repo}"
        try:
            # 先通过 upsert 建立带飞书绑定的 vod 会话 + 用户自定 enable/订阅
            sid = upsert_agent_session(
                repo_key=repo_key, agent_type="vod", runtime_id=1, skill="vod",
                enable=0, subscribe_events='[{"event_type":"push"}]',
                im_app_id="cli_vod", im_app_secret="vsec", im_provider="feishu")
            # 场景应用不应换 id / 清绑定
            self._apply(owner, repo)
            self._apply(owner, repo)
            row = _get_session(repo_key, "vod")
            assert row[0] == sid, "apply_scene 后会话 id 应保持不变"
            assert row[1] == "cli_vod", "apply_scene 不得清空 vod im_app_id"
            assert row[2] == "vsec", "apply_scene 不得清空 vod im_app_secret"
            assert row[3] == "feishu", "apply_scene 不得清空 vod im_provider"
            assert row[4] == 0, "apply_scene 不得重置仓库级 enable（用户开关为准）"
            assert row[5] == '[{"event_type":"push"}]', "apply_scene 不得清空订阅配置"
            # 其余 agent 也应创建
            for at in ("issue", "coding", "qa", "sre"):
                assert _get_session(repo_key, at) is not None, f"{at} 会话未创建"
        finally:
            _cleanup(repo_key)

    def test_fresh_repo_creates_sessions(self):
        """新仓库应用场景：各 agent 会话创建且 id 稳定（二次应用同一 id）"""
        owner, repo = "sessowner", _unique()
        repo_key = f"gitcode:{owner}/{repo}"
        try:
            self._apply(owner, repo)
            ids = {at: _get_session(repo_key, at)[0] for at in ("vod", "issue", "coding", "qa", "sre")}
            self._apply(owner, repo)
            for at, sid in ids.items():
                assert _get_session(repo_key, at)[0] == sid, f"apply_scene 二次应用后 {at} id 变化"
        finally:
            _cleanup(repo_key)


class TestEnsureDefaultKeepsSessionId:
    def _ensure(self, repo_key: str):
        from src.api import session_store
        import json as _json
        from unittest.mock import patch
        fake_cfg = {"scenes": {"default": {"mapping": {
            "vod": "vod", "issue": "issue", "coding": "implementation",
            "qa": "pr", "sre": "cloud-deploy"}}}}
        with patch("builtins.open", unittest.mock.mock_open(read_data=_json.dumps(fake_cfg))):
            session_store.ensure_default_agent_sessions(repo_key)

    def test_ensure_twice_keeps_id_and_im(self):
        """ensure_default_agent_sessions 重复执行（重新托管）：id 不变、vod 飞书绑定不清空"""
        repo_key = f"gitcode:ensowner/{_unique()}"
        try:
            sid = upsert_agent_session(
                repo_key=repo_key, agent_type="vod", im_app_id="cli_vod2",
                im_app_secret="v2", im_provider="feishu")
            self._ensure(repo_key)
            self._ensure(repo_key)
            row = _get_session(repo_key, "vod")
            assert row[0] == sid, "ensure_default 重复执行后 id 应保持不变"
            assert row[1] == "cli_vod2", "ensure_default 不得清空已配置的 vod im_app_id"
            assert row[2] == "v2" and row[3] == "feishu"
            for at in ("issue", "coding", "qa", "sre"):
                assert _get_session(repo_key, at) is not None, f"{at} 会话未创建"
        finally:
            _cleanup(repo_key)
