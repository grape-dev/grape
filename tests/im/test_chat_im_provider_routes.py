"""Issue #956 → A2 收尾：/chat/config 的 provider 化契约测试（凭证真相源 = grape_t_channel_installation）。

覆盖验收：
- GET 回显按安装行：provider / connected / app_id / app_secret_exists（不回传 secret）
- GET 对 Slack / Telegram 只回显 ********（令牌不泄漏）
- PUT 新载荷与旧载荷都落 grape_t_channel_installation，channel_type 正确
- 未接入渠道 → 400 且不落脏行
- app_id 独占：被 VOD（repo 轨）或他人（user 轨）占用 → 400
- 解绑按「存量渠道」清理（钉钉不碰飞书专属表），安装行置 revoked
- 双渠道并存：不同用户不同渠道各一条 active
- 写路径单写：sync 不再回写 legacy 双轨表
"""
import json
import uuid

import pytest
from fastapi.testclient import TestClient

from src.api.main import app
from src.api.deps import get_current_user, get_current_user_or_api_key
from src.session_manager import db, db_lock

UID = 95601
USERNAME = "issue956_tester"
OTHER_UID = 95602
_PREFIX = "956_"


def _ew_conn():
    from src.im.engine_wiring import _connect, default_db_path, run_startup_migration
    run_startup_migration(default_db_path())   # 幂等建表
    return _connect(default_db_path())


def _inst(app_id):
    """(channel_type, status, config_dict) or None。"""
    conn = _ew_conn()
    try:
        row = conn.execute(
            "SELECT channel_type, status, config FROM grape_t_channel_installation WHERE legacy_app_id=?",
            (app_id,)).fetchone()
    finally:
        conn.close()
    if not row:
        return None
    try:
        cfg = json.loads(row[2] or "{}")
    except Exception:
        cfg = {}
    return row[0], row[1], cfg


def _inst_count(where="1=1", params=()):
    conn = _ew_conn()
    try:
        return conn.execute(f"SELECT COUNT(*) FROM grape_t_channel_installation WHERE {where}", params).fetchone()[0]
    finally:
        conn.close()


def _seed_inst(app_id, secret, channel_type, uid=UID):
    from src.im.engine_wiring import sync_installation
    assert sync_installation("user", owner_user_id=str(uid), app_id=app_id,
                             app_secret=secret, channel_type=channel_type)


@pytest.fixture(autouse=True)
def _client():
    def _fake_user():
        return {"id": UID, "username": USERNAME}

    app.dependency_overrides[get_current_user] = _fake_user
    app.dependency_overrides[get_current_user_or_api_key] = _fake_user
    yield TestClient(app)
    app.dependency_overrides.clear()
    conn = _ew_conn()
    try:
        conn.execute("DELETE FROM grape_t_channel_installation WHERE legacy_app_id LIKE ?", (_PREFIX + "%",))
        conn.execute("DELETE FROM grape_t_channel_lease WHERE installation_id<>''")
        conn.commit()
    finally:
        conn.close()
    with db_lock:
        cur = db.cursor()
        cur.execute("DELETE FROM chat_conversations WHERE im_channel_id LIKE ?", (_PREFIX + "%",))
        cur.execute("DELETE FROM unified_user_mapping WHERE user_id IN (?,?)", (UID, OTHER_UID))
        cur.execute("DELETE FROM feishu_onboard_task WHERE app_id LIKE ?", (_PREFIX + "%",))
        cur.execute("DELETE FROM repo_im_group_binding WHERE im_app_id LIKE ?", (_PREFIX + "%",))
        cur.execute("DELETE FROM bot_seen_messages WHERE app_id LIKE ?", (_PREFIX + "%",))
        db.commit()


def _q(sql, *args):
    with db_lock:
        return db.cursor().execute(sql, args).fetchall()


def _no_legacy_im_cols() -> bool:
    """A2 Phase B：legacy im_* 列已 DROP，结构层面不可能再被写。"""
    with db_lock:
        cols = [r[1] for r in db.cursor().execute("PRAGMA table_info(chat_agent_session)").fetchall()]
        dcols = [r[1] for r in db.cursor().execute("PRAGMA table_info(devops_agent_session)").fetchall()]
    return not [c for c in (cols + dcols) if c.startswith("im_")]


class TestGetConfigProviderEcho:
    def test_dingtalk_binding_echoes_provider(self, _client):
        app_id = _PREFIX + uuid.uuid4().hex[:8]
        _seed_inst(app_id, "sec956", "dingtalk")
        im = _client.get("/api/chat/config").json()["im"]
        assert im["provider"] == "dingtalk"
        assert im["dingtalk"] == {"connected": True, "app_id": app_id, "app_secret_exists": True,
                                  "installation_status": "active"}
        assert im["feishu"] == {"connected": False, "app_id": "", "app_secret_exists": False,
                                "installation_status": ""}

    def test_feishu_binding_echoes_provider(self, _client):
        app_id = _PREFIX + uuid.uuid4().hex[:8]
        _seed_inst(app_id, "sec956", "feishu")
        im = _client.get("/api/chat/config").json()["im"]
        assert im["provider"] == "feishu"
        assert im["feishu"]["connected"] is True and im["feishu"]["app_id"] == app_id
        assert im["dingtalk"]["connected"] is False

    def test_no_installation_normalized_to_feishu(self, _client):
        """没有任何 active 安装 → provider 归一 feishu，各渠道 connected=False。"""
        im = _client.get("/api/chat/config").json()["im"]
        assert im["provider"] == "feishu"
        for p in ("feishu", "dingtalk", "wecom", "slack", "telegram", "wechat"):
            assert im[p]["connected"] is False and im[p]["app_id"] == ""

    def test_no_secret_leak_in_response(self, _client):
        app_id = _PREFIX + uuid.uuid4().hex[:8]
        secret = "sec956_" + uuid.uuid4().hex[:8]
        _seed_inst(app_id, secret, "dingtalk")
        text = _client.get("/api/chat/config").text
        assert secret not in text, "GET /chat/config 不得回传 secret 明文"
        assert '"app_secret_exists"' in text

    def test_unbound_returns_connected_false_no_app_id(self, _client):
        im = _client.get("/api/chat/config").json()["im"]
        assert im["feishu"]["connected"] is False and im["feishu"]["app_id"] == ""
        assert im["dingtalk"]["connected"] is False and im["dingtalk"]["app_id"] == ""


class TestPutConfigSave:
    def test_save_new_payload_dingtalk(self, _client):
        app_id = _PREFIX + uuid.uuid4().hex[:8]
        r = _client.put("/api/chat/config", json={
            "im": {"provider": "dingtalk", "app_id": app_id, "app_secret": "dingsec956"}})
        assert r.status_code == 200, r.text
        ct, status, cfg = _inst(app_id)
        assert (ct, status) == ("dingtalk", "active")
        assert cfg["app_id"] == app_id and cfg["app_secret"] == "dingsec956"
        # legacy 不再被写（列已 DROP，结构层面保证）
        assert _no_legacy_im_cols()

    def test_save_legacy_dingtalk_subdict(self, _client):
        app_id = _PREFIX + uuid.uuid4().hex[:8]
        r = _client.put("/api/chat/config", json={
            "im": {"dingtalk": {"app_id": app_id, "app_secret": "dingsec956"}}})
        assert r.status_code == 200, r.text
        assert _inst(app_id)[0] == "dingtalk"

    def test_save_legacy_feishu_payload(self, _client):
        app_id = _PREFIX + uuid.uuid4().hex[:8]
        r = _client.put("/api/chat/config", json={
            "im": {"feishu": {"app_id": app_id, "app_secret": "feisec956"}}})
        assert r.status_code == 200, r.text
        ct, status, cfg = _inst(app_id)
        assert (ct, status) == ("feishu", "active") and cfg["app_secret"] == "feisec956"

    def test_save_without_im_key_keeps_binding(self, _client):
        """#431 回归：普通保存不动安装行。"""
        app_id = _PREFIX + uuid.uuid4().hex[:8]
        _seed_inst(app_id, "sec956", "dingtalk")
        r = _client.put("/api/chat/config", json={"backend": "opencode"})
        assert r.status_code == 200, r.text
        assert _inst(app_id)[1] == "active"

    def test_invalid_provider_rejected_400_no_dirty_rows(self, _client):
        r = _client.put("/api/chat/config", json={
            "im": {"provider": "whatsapp", "app_id": _PREFIX + "wa1", "app_secret": "x"}})
        assert r.status_code == 400
        assert "whatsapp" in r.text
        assert _inst_count("legacy_app_id LIKE ?", (_PREFIX + "%",)) == 0
        assert _no_legacy_im_cols()

    def test_wecom_provider_saved(self, _client):
        app_id = _PREFIX + uuid.uuid4().hex[:8]
        r = _client.put("/api/chat/config", json={
            "im": {"provider": "wecom", "app_id": app_id, "app_secret": "wecomsec"}})
        assert r.status_code == 200, r.text
        ct, status, cfg = _inst(app_id)
        assert (ct, status) == ("wecom", "active")
        assert cfg["app_id"] == app_id and cfg["app_secret"] == "wecomsec"

    def test_slack_dual_token_saved(self, _client):
        bot = _PREFIX + "xoxb-" + uuid.uuid4().hex[:8]
        r = _client.put("/api/chat/config", json={
            "im": {"provider": "slack", "app_id": bot, "app_secret": "xapp-sec"}})
        assert r.status_code == 200, r.text
        ct, status, cfg = _inst(bot)
        assert ct == "slack"
        assert cfg["bot_token"] == bot and cfg["app_token"] == "xapp-sec"
        assert cfg["app_id"] == bot and cfg["app_secret"] == "xapp-sec"

    def test_slack_echo_masks_bot_token(self, _client):
        bot = _PREFIX + "xoxb-" + uuid.uuid4().hex[:8]
        _seed_inst(bot, "xapp-sec", "slack")
        r = _client.get("/api/chat/config")
        im = r.json()["im"]
        assert im["provider"] == "slack"
        assert im["slack"] == {"connected": True, "app_id": "********", "app_secret_exists": True,
                               "installation_status": "active"}
        assert bot not in r.text and "xapp-sec" not in r.text

    def test_slack_masked_app_id_keeps_existing_token(self, _client):
        bot = _PREFIX + "xoxb-" + uuid.uuid4().hex[:8]
        _seed_inst(bot, "xapp-old", "slack")
        r = _client.put("/api/chat/config", json={
            "im": {"provider": "slack", "app_id": "********", "app_secret": "xapp-new"}})
        assert r.status_code == 200, r.text
        ct, status, cfg = _inst(bot)
        assert cfg["app_id"] == bot and cfg["app_secret"] == "xapp-new"
        assert cfg["bot_token"] == bot and cfg["app_token"] == "xapp-new"

    def test_slack_unbind_revokes_installation(self, _client):
        bot = _PREFIX + "xoxb-" + uuid.uuid4().hex[:8]
        r = _client.put("/api/chat/config", json={
            "im": {"provider": "slack", "app_id": bot, "app_secret": "xapp-sec"}})
        assert r.status_code == 200, r.text
        r = _client.put("/api/chat/config", json={
            "im": {"provider": "slack", "app_id": "", "app_secret": ""}})
        assert r.status_code == 200, r.text
        assert _inst(bot)[1] == "revoked"

    def test_telegram_single_token_saved(self, _client):
        token = _PREFIX + "123456:ABC-" + uuid.uuid4().hex[:8]
        r = _client.put("/api/chat/config", json={
            "im": {"provider": "telegram", "app_id": token, "app_secret": ""}})
        assert r.status_code == 200, r.text
        ct, status, cfg = _inst(token)
        assert ct == "telegram"
        assert cfg["bot_token"] == token and cfg["app_id"] == token

    def test_telegram_echo_masks_bot_token(self, _client):
        token = _PREFIX + "123456:ABC-" + uuid.uuid4().hex[:8]
        _seed_inst(token, token, "telegram")
        r = _client.get("/api/chat/config")
        im = r.json()["im"]
        assert im["provider"] == "telegram"
        assert im["telegram"] == {"connected": True, "app_id": "********", "app_secret_exists": True,
                                  "installation_status": "active"}
        assert token not in r.text

    def test_telegram_unbind_revokes_installation(self, _client):
        token = _PREFIX + "123456:ABC-" + uuid.uuid4().hex[:8]
        _client.put("/api/chat/config", json={
            "im": {"provider": "telegram", "app_id": token, "app_secret": ""}})
        r = _client.put("/api/chat/config", json={
            "im": {"provider": "telegram", "app_id": "", "app_secret": ""}})
        assert r.status_code == 200, r.text
        assert _inst(token)[1] == "revoked"

    def test_occupied_by_vod_rejected_no_dirty_installation(self, _client):
        """该 app_id 已被 repo 轨（VOD）active 安装占用 → 400，且不建脏安装行。"""
        app_id = _PREFIX + uuid.uuid4().hex[:8]
        from src.im.engine_wiring import sync_installation
        assert sync_installation("repo", repo_key="gitcode:org/repo956",
                                 app_id=app_id, app_secret="vodsec")
        r = _client.put("/api/chat/config", json={
            "im": {"provider": "dingtalk", "app_id": app_id, "app_secret": "x"}})
        assert r.status_code == 400
        assert "VOD" in r.text
        assert _inst_count("legacy_app_id=? AND scope='user'", (app_id,)) == 0
        # 原有 repo 安装未被连带改动
        assert _inst_count("legacy_app_id=? AND status='active'", (app_id,)) == 1

    def test_other_user_occupied_rejected(self, _client):
        app_id = _PREFIX + uuid.uuid4().hex[:8]
        _seed_inst(app_id, "sec", "dingtalk", uid=OTHER_UID)
        r = _client.put("/api/chat/config", json={
            "im": {"provider": "dingtalk", "app_id": app_id, "app_secret": "x"}})
        assert r.status_code == 400
        assert "其他用户" in r.text


class TestUnbindProviderDimension:
    def test_dingtalk_unbind_purges_but_skips_feishu_only_tables(self, _client):
        app_id = _PREFIX + uuid.uuid4().hex[:8]
        r0 = _client.put("/api/chat/config", json={
            "im": {"provider": "dingtalk", "app_id": app_id, "app_secret": "sec956"}})
        assert r0.status_code == 200, r0.text
        _q("INSERT INTO chat_conversations (id, user_id, im_channel_id) VALUES ('c1', ?, ?)", UID, app_id)
        _q("INSERT INTO feishu_onboard_task (task_id, bot_type, user_id, app_id) VALUES (?, 'chat_bot', ?, ?)",
           "t_" + app_id, UID, app_id)
        _q("INSERT INTO unified_user_mapping (user_id, feishu_open_id, app_id) VALUES (?, 'ou_x', ?)",
           UID, app_id)
        _q("INSERT INTO repo_im_group_binding (im_chat_id, repo_key, im_app_id) VALUES ('ch1', ?, ?)",
           "gitcode:org/repo956", app_id)
        _q("INSERT INTO bot_seen_messages (app_id, message_id, seen_at) VALUES (?, 'm1', 0)", app_id)
        r = _client.put("/api/chat/config", json={
            "im": {"provider": "dingtalk", "app_id": "", "app_secret": ""}})
        assert r.status_code == 200, r.text
        # 渠道无关表清理
        assert _q("SELECT COUNT(*) FROM chat_conversations WHERE im_channel_id=?", app_id)[0][0] == 0
        assert _q("SELECT COUNT(*) FROM repo_im_group_binding WHERE im_app_id=?", app_id)[0][0] == 0
        assert _q("SELECT COUNT(*) FROM bot_seen_messages WHERE app_id=?", app_id)[0][0] == 0
        assert _inst(app_id)[1] == "revoked"
        # 飞书专属表不动
        assert _q("SELECT COUNT(*) FROM feishu_onboard_task WHERE app_id=?", app_id)[0][0] == 1
        assert _q("SELECT COUNT(*) FROM unified_user_mapping WHERE app_id=?", app_id)[0][0] == 1

    def test_feishu_unbind_purges_feishu_tables(self, _client):
        app_id = _PREFIX + uuid.uuid4().hex[:8]
        _seed_inst(app_id, "sec956", "feishu")
        _q("INSERT INTO feishu_onboard_task (task_id, bot_type, user_id, app_id) VALUES (?, 'chat_bot', ?, ?)",
           "t_" + app_id, UID, app_id)
        _q("INSERT INTO unified_user_mapping (user_id, feishu_open_id, app_id) VALUES (?, 'ou_x', ?)",
           UID, app_id)
        r = _client.put("/api/chat/config", json={"im": {"feishu": {"app_id": "", "app_secret": ""}}})
        assert r.status_code == 200, r.text
        assert _q("SELECT COUNT(*) FROM feishu_onboard_task WHERE app_id=?", app_id)[0][0] == 0
        assert _q("SELECT feishu_open_id FROM unified_user_mapping WHERE user_id=?", UID)[0][0] == ""

    def test_unbind_uses_stored_provider_not_payload(self, _client):
        """存量飞书安装 + 载荷声明 dingtalk 解绑 → 仍按存量 channel_type=feishu 清理飞书映射。"""
        app_id = _PREFIX + uuid.uuid4().hex[:8]
        _seed_inst(app_id, "sec956", "feishu")
        _q("INSERT INTO unified_user_mapping (user_id, feishu_open_id, app_id) VALUES (?, 'ou_x', ?)",
           UID, app_id)
        r = _client.put("/api/chat/config", json={
            "im": {"provider": "dingtalk", "app_id": "", "app_secret": ""}})
        assert r.status_code == 200, r.text
        assert _q("SELECT feishu_open_id FROM unified_user_mapping WHERE user_id=?", UID)[0][0] == ""


class TestDualChannelIsolation:
    def test_two_users_two_channels_coexist(self, _client):
        feishu_app = _PREFIX + "fs_" + uuid.uuid4().hex[:6]
        ding_app = _PREFIX + "dt_" + uuid.uuid4().hex[:6]
        _seed_inst(feishu_app, "s1", "feishu", uid=OTHER_UID)
        r2 = _client.put("/api/chat/config", json={
            "im": {"provider": "dingtalk", "app_id": ding_app, "app_secret": "s2"}})
        assert r2.status_code == 200, r2.text
        assert _inst(feishu_app)[:2] == ("feishu", "active")
        assert _inst(ding_app)[:2] == ("dingtalk", "active")


class TestEngineWiringSingleWrite:
    def test_sync_writes_new_table_not_legacy(self, _client):
        """A2 收尾：sync 只写 grape_t_channel_installation，legacy 列保持不动。"""
        from src.im import engine_wiring
        app_id = "ding_bw_" + uuid.uuid4().hex[:8]
        assert engine_wiring.sync_installation(
            "user", owner_user_id=str(UID), app_id=app_id, app_secret="s",
            channel_type="dingtalk")
        assert _inst(app_id)[:2] == ("dingtalk", "active")
        # legacy 列已 DROP（A2 Phase B）：只能写进安装表
        assert _no_legacy_im_cols()
        conn = _ew_conn()
        try:
            conn.execute("DELETE FROM grape_t_channel_installation WHERE legacy_app_id=?", (app_id,))
            conn.commit()
        finally:
            conn.close()


class TestSlackLogRedaction:
    def test_slack_save_logs_do_not_leak_token(self, _client, caplog):
        import logging
        bot = _PREFIX + "xoxb-" + uuid.uuid4().hex[:8]
        caplog.set_level(logging.INFO, logger="devops-bot")
        r = _client.put("/api/chat/config", json={
            "im": {"provider": "slack", "app_id": bot, "app_secret": "xapp-sec"}})
        assert r.status_code == 200, r.text
        assert bot not in caplog.text
        assert "xoxb-" not in caplog.text

    def test_slack_replace_logs_do_not_leak_tokens(self, _client, caplog):
        import logging
        old_bot = _PREFIX + "xoxb-" + uuid.uuid4().hex[:8]
        new_bot = _PREFIX + "xoxb-" + uuid.uuid4().hex[:8]
        _seed_inst(old_bot, "xapp-old", "slack")
        caplog.set_level(logging.INFO, logger="devops-bot")
        r = _client.put("/api/chat/config", json={
            "im": {"provider": "slack", "app_id": new_bot, "app_secret": "xapp-new"}})
        assert r.status_code == 200, r.text
        assert old_bot not in caplog.text
        assert new_bot not in caplog.text
        assert "xoxb-" not in caplog.text

    def test_safe_ref_helpers_hash_slack_tokens(self):
        from src.api.routes.chat import _safe_im_app_ref
        from src.im.engine_wiring import _safe_app_ref
        token = "xoxb-1...cret"
        for ref in (
            _safe_im_app_ref(token, "slack"),
            _safe_im_app_ref(token),
            _safe_app_ref(token, "slack"),
            _safe_app_ref(token),
        ):
            assert token[:12] not in ref
            assert ref.startswith("slack:")
        assert _safe_im_app_ref("ding-app-123", "dingtalk") == "ding-app-123"[:30]
        assert _safe_app_ref("feishu-app-123", "feishu") == "feishu-app-1"