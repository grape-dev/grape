"""Issue #431 回归测试：Chat Agent 页面普通保存（不带 im 字段）不得清空已绑定飞书 IM 凭证。

根因：update_chat_config 的 else 分支（body 无 im 键也命中）把 im_app_id / im_app_secret /
im_provider 显式置空，叠加 _write_chat_config_row 的 INSERT OR REPLACE 整行覆盖 → 一次普通
保存即丢 IM 绑定；同路径还会把在跑 bot 从 BotManager 摘除。

修复语义：
- body 无 `im` 键（或 im 下无 feishu）→ 不写任何 im_ 字段，DB 行三字段原值不变，bot 不摘。
- 显式携带 `im` 且 app_id 为空 → 解绑（三字段清空 + _purge_chat_im_binding 清理 + bot 摘除）。
"""
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from src.api.main import app
from src.api.deps import get_current_user, get_current_user_or_api_key
from src.session_manager import db, db_lock

UID = 43101
USERNAME = "issue431_tester"


@pytest.fixture(autouse=True)
def _client():
    def _fake_user():
        return {"id": UID, "username": USERNAME}

    app.dependency_overrides[get_current_user] = _fake_user
    app.dependency_overrides[get_current_user_or_api_key] = _fake_user
    c = TestClient(app)
    yield c
    app.dependency_overrides.clear()
    with db_lock:
        cur = db.cursor()
        cur.execute("DELETE FROM chat_agent_session WHERE user_id=?", (UID,))
        cur.execute("DELETE FROM global_im_bot_config WHERE bot_app_id LIKE 'cli_431_%'")
        cur.execute("DELETE FROM chat_conversations WHERE im_channel_id LIKE 'cli_431_%'")
        cur.execute("DELETE FROM unified_user_mapping WHERE user_id=?", (UID,))
        db.commit()


def _seed_im_binding(app_id, secret="sec_431"):
    """给 UID 种一条已绑定的 IM 配置行 + 注册表行（复刻真实绑定态）。"""
    with db_lock:
        c = db.cursor()
        c.execute(
            "INSERT OR REPLACE INTO chat_agent_session (id, user_id, repo_key, runtime_id, im_app_id, im_app_secret, im_provider, created_at, last_active_at) "
            "VALUES (?,?,?,?,?,?,?,datetime('now'),datetime('now'))",
            (f"sess-cfg-{UID}", UID, "", 0, app_id, secret, "feishu"))
        c.execute("INSERT OR REPLACE INTO global_im_bot_config (bot_app_id, bot_type) VALUES (?, 'chat_bot')", (app_id,))
        db.commit()


def _im_row():
    with db_lock:
        return db.cursor().execute(
            "SELECT im_app_id, im_app_secret, im_provider FROM chat_agent_session "
            "WHERE user_id=? AND (repo_key='' OR repo_key IS NULL)", (UID,)).fetchone()


class TestSaveWithoutImKeepsBinding:
    def test_empty_body_keeps_im(self, _client):
        """AC1：body 为空对象（换引擎等普通保存）→ IM 三字段原值不变。"""
        app_id = "cli_431_%s" % uuid.uuid4().hex[:6]
        _seed_im_binding(app_id)
        r = _client.put("/api/chat/config", json={})
        assert r.status_code == 200, r.text
        assert tuple(_im_row()) == (app_id, "sec_431", "feishu"), f"IM 字段被清空: {_im_row()}"

    def test_engine_skill_save_keeps_im(self, _client):
        """AC1：携带 backend/skill 的普通保存（无 im 键）→ IM 三字段原值不变。"""
        app_id = "cli_431_%s" % uuid.uuid4().hex[:6]
        _seed_im_binding(app_id)
        r = _client.put("/api/chat/config", json={"backend": "opencode", "skill": "codereview", "skill_scope": "official"})
        assert r.status_code == 200, r.text
        assert tuple(_im_row()) == (app_id, "sec_431", "feishu"), f"IM 字段被清空: {_im_row()}"

    def test_im_key_without_feishu_keeps_binding(self, _client):
        """AC1（边界）：body 携带 im 键但无 feishu 子键 → 不视为解绑，IM 保持。"""
        app_id = "cli_431_%s" % uuid.uuid4().hex[:6]
        _seed_im_binding(app_id)
        r = _client.put("/api/chat/config", json={"im": {}, "backend": "opencode"})
        assert r.status_code == 200, r.text
        assert tuple(_im_row()) == (app_id, "sec_431", "feishu")

    def test_get_config_still_shows_im(self, _client):
        """AC1：普通保存后 GET /api/chat/config 仍回显已绑定凭证。"""
        app_id = "cli_431_%s" % uuid.uuid4().hex[:6]
        _seed_im_binding(app_id)
        r = _client.put("/api/chat/config", json={"backend": "opencode"})
        assert r.status_code == 200
        data = _client.get("/api/chat/config").json()
        assert data["im"]["feishu"]["app_id"] == app_id
        assert data["im"]["feishu"]["connected"] is True

    def test_save_without_im_does_not_remove_bot(self, _client):
        """AC1：普通保存不得把在跑 bot 从 BotManager 摘除（同根因的静默断连路径）。"""
        app_id = "cli_431_%s" % uuid.uuid4().hex[:6]
        _seed_im_binding(app_id)
        bm = MagicMock()
        bm.api = MagicMock()
        bm.remove_bot = AsyncMock()
        with patch("src.api.main.bot_manager", bm):
            r = _client.put("/api/chat/config", json={"backend": "opencode"})
        assert r.status_code == 200, r.text
        bm.remove_bot.assert_not_awaited(), "普通保存不应摘除 bot"

    def test_save_without_im_keeps_registry(self, _client):
        """AC1：普通保存后 global_im_bot_config 注册行保留。"""
        app_id = "cli_431_%s" % uuid.uuid4().hex[:6]
        _seed_im_binding(app_id)
        r = _client.put("/api/chat/config", json={})
        assert r.status_code == 200
        with db_lock:
            n = db.cursor().execute("SELECT COUNT(*) FROM global_im_bot_config WHERE bot_app_id=?", (app_id,)).fetchone()[0]
        assert n == 1, "chat_bot 注册行不应被删除"


class TestExplicitUnbindStillWorks:
    def test_explicit_unbind_clears_im(self, _client):
        """AC2：显式携带 im 且 app_id 为空 → 解绑：三字段清空 + 注册行删除 + bot 摘除。"""
        app_id = "cli_431_%s" % uuid.uuid4().hex[:6]
        _seed_im_binding(app_id)
        bm = MagicMock()
        bm.api = MagicMock()
        bm.remove_bot = AsyncMock()
        with patch("src.api.main.bot_manager", bm):
            r = _client.put("/api/chat/config", json={"im": {"feishu": {"app_id": "", "app_secret": ""}}})
        assert r.status_code == 200, r.text
        assert tuple(_im_row()) == ("", "", ""), "显式解绑应清空 IM 字段"
        bm.remove_bot.assert_awaited_with(app_id)
        with db_lock:
            n = db.cursor().execute("SELECT COUNT(*) FROM global_im_bot_config WHERE bot_app_id=?", (app_id,)).fetchone()[0]
        assert n == 0, "解绑后注册行应删除"

    def test_rebind_after_normal_save(self, _client):
        """AC2（组合）：普通保存后再显式重绑 → 三字段写回，无残留。"""
        app_id = "cli_431_%s" % uuid.uuid4().hex[:6]
        _seed_im_binding(app_id)
        r = _client.put("/api/chat/config", json={"backend": "opencode"})
        assert r.status_code == 200
        bm = MagicMock()
        bm.api = MagicMock()
        bm.remove_bot = AsyncMock()
        bm.add_bot = AsyncMock()
        bm._bots = {}
        with patch("src.api.main.bot_manager", bm):
            r = _client.put("/api/chat/config", json={"im": {"feishu": {"app_id": app_id, "app_secret": "sec_431"}}})
        assert r.status_code == 200, r.text
        assert tuple(_im_row()) == (app_id, "sec_431", "feishu")
