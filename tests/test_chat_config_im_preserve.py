"""Issue #431 回归测试：Chat Agent 页面普通保存（不带 im 字段）不得解绑已生效的 IM 安装。

A2 收尾后凭证真相源 = grape_t_channel_installation（legacy im_* 三列与 global_im_bot_config 不再读写），
所以断言全部落在安装行上：

- body 无 `im` 键（或 im 下无渠道键）→ 普通保存：安装行仍 active，GET /config 仍回显。
- 显式携带 `im` 且 app_id 为空 → 解绑：安装行置 revoked。
- 显式重绑 → 安装行回到 active。
"""
import uuid

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
        cur.execute("DELETE FROM chat_conversations WHERE im_channel_id LIKE 'cli_431_%'")
        cur.execute("DELETE FROM unified_user_mapping WHERE user_id=?", (UID,))
        db.commit()
    _cleanup_inst()


def _ew():
    from src.im.engine_wiring import _connect, default_db_path, run_startup_migration
    run_startup_migration(default_db_path())   # 幂等：建表 + 空表兜底
    return _connect, default_db_path


def _cleanup_inst(app_id=None):
    _connect, default_db_path = _ew()
    conn = _connect(default_db_path())
    try:
        if app_id:
            conn.execute("DELETE FROM grape_t_channel_installation WHERE legacy_app_id=?", (app_id,))
            conn.execute("DELETE FROM grape_t_channel_lease WHERE lease_key LIKE ?", (f"%{app_id}%",))
        else:
            conn.execute("DELETE FROM grape_t_channel_installation WHERE legacy_app_id LIKE 'cli_431_%'")
            conn.execute("DELETE FROM grape_t_channel_lease WHERE installation_id<>''")
        conn.commit()
    finally:
        conn.close()


def _seed_im_binding(app_id, secret="sec_431"):
    """复刻真实绑定态：grape_t_channel_installation 里一条 active 安装。"""
    from src.im.engine_wiring import sync_installation
    _ew()
    assert sync_installation("user", owner_user_id=str(UID), app_id=app_id, app_secret=secret)


def _inst_row(app_id):
    _connect, default_db_path = _ew()
    conn = _connect(default_db_path())
    try:
        return conn.execute(
            "SELECT status, config FROM grape_t_channel_installation WHERE legacy_app_id=?", (app_id,)).fetchone()
    finally:
        conn.close()


class TestSaveWithoutImKeepsBinding:
    def test_empty_body_keeps_im(self, _client):
        """AC1：body 为空对象（换引擎等普通保存）→ 安装行仍 active。"""
        app_id = "cli_431_%s" % uuid.uuid4().hex[:6]
        _seed_im_binding(app_id)
        r = _client.put("/api/chat/config", json={})
        assert r.status_code == 200, r.text
        row = _inst_row(app_id)
        assert row and row[0] == "active", f"普通保存把安装弄失效了: {row}"

    def test_engine_skill_save_keeps_im(self, _client):
        """AC1：携带 backend/skill 的普通保存（无 im 键）→ 安装保持 active。"""
        app_id = "cli_431_%s" % uuid.uuid4().hex[:6]
        _seed_im_binding(app_id)
        r = _client.put("/api/chat/config", json={"backend": "opencode", "skill": "codereview", "skill_scope": "official"})
        assert r.status_code == 200, r.text
        row = _inst_row(app_id)
        assert row and row[0] == "active", f"普通保存把安装弄失效了: {row}"

    def test_im_key_without_channel_keeps_binding(self, _client):
        """AC1（边界）：body 携带 im 键但无渠道子键 → 不视为解绑。"""
        app_id = "cli_431_%s" % uuid.uuid4().hex[:6]
        _seed_im_binding(app_id)
        r = _client.put("/api/chat/config", json={"im": {}, "backend": "opencode"})
        assert r.status_code == 200, r.text
        row = _inst_row(app_id)
        assert row and row[0] == "active", f"空 im 键被误当解绑: {row}"

    def test_get_config_still_shows_im(self, _client):
        """AC1：普通保存后 GET /api/chat/config 仍从安装行回显凭证。"""
        app_id = "cli_431_%s" % uuid.uuid4().hex[:6]
        _seed_im_binding(app_id)
        r = _client.put("/api/chat/config", json={"backend": "opencode"})
        assert r.status_code == 200
        data = _client.get("/api/chat/config").json()
        assert data["im"]["feishu"]["app_id"] == app_id
        assert data["im"]["feishu"]["connected"] is True
        assert data["im"]["provider"] == "feishu"

    def test_save_without_im_keeps_installation_active(self, _client):
        """AC1：普通保存不得让已生效的安装失效（M2/M3 语义：安装行仍 active）。"""
        app_id = "cli_431_%s" % uuid.uuid4().hex[:6]
        _seed_im_binding(app_id)
        r = _client.put("/api/chat/config", json={"backend": "opencode"})
        assert r.status_code == 200, r.text
        row = _inst_row(app_id)
        assert row and row[0] == "active", f"普通保存不应改动安装状态，实际 status={row and row[0]}"

    def test_normal_save_does_not_create_legacy_rows(self, _client):
        """AC1：普通保存不再往 legacy 表写任何行（A2 收尾）。"""
        app_id = "cli_431_%s" % uuid.uuid4().hex[:6]
        _seed_im_binding(app_id)
        r = _client.put("/api/chat/config", json={})
        assert r.status_code == 200
        with db_lock:
            cols = [r[1] for r in db.cursor().execute("PRAGMA table_info(chat_agent_session)").fetchall()]
        assert "im_app_id" not in cols, "legacy im_app_id 列应已 DROP（A2 Phase B）"


class TestExplicitUnbindStillWorks:
    def test_explicit_unbind_revokes_installation(self, _client):
        """AC2：显式携带 im 且 app_id 为空 → 安装行置 revoked。"""
        app_id = "cli_431_%s" % uuid.uuid4().hex[:6]
        _seed_im_binding(app_id)
        r = _client.put("/api/chat/config", json={"im": {"feishu": {"app_id": "", "app_secret": ""}}})
        assert r.status_code == 200, r.text
        row = _inst_row(app_id)
        assert row and row[0] == "revoked", f"显式解绑应把安装置 revoked，实际={row}"
        data = _client.get("/api/chat/config").json()
        assert data["im"]["feishu"]["connected"] is False

    def test_rebind_after_normal_save(self, _client):
        """AC2（组合）：普通保存后再显式重绑 → 安装行回到 active。"""
        app_id = "cli_431_%s" % uuid.uuid4().hex[:6]
        _seed_im_binding(app_id)
        r = _client.put("/api/chat/config", json={"backend": "opencode"})
        assert r.status_code == 200
        r = _client.put("/api/chat/config", json={"im": {"feishu": {"app_id": app_id, "app_secret": "sec_431"}}})
        assert r.status_code == 200, r.text
        row = _inst_row(app_id)
        assert row and row[0] == "active", f"重绑后安装行应回到 active，实际={row}"
        assert "sec_431" in (row[1] or ""), f"secret 应快照进安装 config，实际={row[1]}"