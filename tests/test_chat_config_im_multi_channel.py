"""多渠并存回归（2026-11-10 主人反馈驱动）。

背景：GET /api/chat/config 原先只回显「最近更新的那一条」active 安装，导致微信 + 企微同时
连着时，前端只有最后绑的那个渠道显示已接入，其余恒为「未接入」；更危险的是
PUT /chat/config 保存企微时会拿微信当「旧值」，误删微信会话并 revoke 微信安装。

本文件锁三件事：
1. GET /config 按渠道分别回显（互不干扰）；provider 仍给出最近绑定的渠道。
2. 保存 B 渠道不影响 A 渠道的安装状态与配置。
3. 解绑 B 渠道只吊销 B，A 照旧 active。
"""
import uuid

import pytest
from fastapi.testclient import TestClient

from src.api.main import app
from src.api.deps import get_current_user, get_current_user_or_api_key
from src.session_manager import db, db_lock

UID = 43102
USERNAME = "multi_channel_tester"
PREFIX = "cli_mc_"


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
        cur.execute("DELETE FROM chat_conversations WHERE im_channel_id LIKE ?", (PREFIX + "%",))
        cur.execute("DELETE FROM unified_user_mapping WHERE user_id=?", (UID,))
        db.commit()
    _cleanup()


def _ew():
    from src.im.engine_wiring import _connect, default_db_path, run_startup_migration
    path = default_db_path()
    run_startup_migration(path)
    return _connect, path


def _cleanup():
    for app_id in _seeded_ids():
        _drop(app_id)


def _seeded_ids():
    _connect, path = _ew()
    conn = _connect(path)
    try:
        return [r[0] for r in conn.execute(
            "SELECT legacy_app_id FROM grape_t_channel_installation WHERE legacy_app_id LIKE ?",
            (PREFIX + "%",)).fetchall()]
    finally:
        conn.close()


def _drop(app_id):
    _connect, path = _ew()
    conn = _connect(path)
    try:
        conn.execute("DELETE FROM grape_t_channel_installation WHERE legacy_app_id=?", (app_id,))
        conn.execute("DELETE FROM grape_t_channel_lease WHERE lease_key LIKE ?", (f"%{app_id}%",))
        conn.commit()
    finally:
        conn.close()


def _seed(channel_type, app_id, secret="sec_mc"):
    from src.im.engine_wiring import sync_installation
    _ew()
    assert sync_installation("user", owner_user_id=str(UID), app_id=app_id,
                             app_secret=secret, channel_type=channel_type)


def _row(app_id):
    _connect, path = _ew()
    conn = _connect(path)
    try:
        return conn.execute(
            "SELECT status, config, channel_type FROM grape_t_channel_installation WHERE legacy_app_id=?",
            (app_id,)).fetchone()
    finally:
        conn.close()


def _uid():
    return PREFIX + uuid.uuid4().hex[:6]


class TestPerChannelEcho:
    def test_both_channels_echoed(self, _client):
        """微信 + 企微同时 active → 两个渠道都要回显 app_id（不是只有最新的那个）。"""
        wx, wc = _uid(), _uid()
        _seed("wechat", wx)
        _seed("wecom", wc)
        im = _client.get("/api/chat/config").json()["im"]
        assert im["wechat"]["app_id"] == wx, im["wechat"]
        assert im["wecom"]["app_id"] == wc, im["wecom"]
        assert im["feishu"]["app_id"] == "", "没配的渠道必须空回显，不能串味"
        assert im["wechat"]["app_secret_exists"] is True

    def test_slack_token_masked_per_channel(self, _client):
        """Slack 的 Bot Token 存在 app_id 位：回显必须是 ********（与其他渠道并存时也要脱敏）。"""
        wx, sk = _uid(), _uid()
        _seed("wechat", wx)
        _seed("slack", sk, secret="xapp-secret")
        im = _client.get("/api/chat/config").json()["im"]
        assert im["wechat"]["app_id"] == wx
        assert im["slack"]["app_id"] == "********", im["slack"]
        assert "cli_mc" not in im["slack"]["app_id"]


class TestSaveIsolation:
    def test_save_wecom_does_not_touch_wechat(self, _client):
        """保存企微不得把微信当旧值（原缺陷：误删微信会话 + revoke 微信安装）。"""
        wx, wc = _uid(), _uid()
        _seed("wechat", wx)
        _seed("wecom", wc)
        new_wc = _uid()
        r = _client.put("/api/chat/config", json={"im": {
            "provider": "wecom", "app_id": new_wc, "app_secret": "sec_new"}})
        assert r.status_code == 200, r.text
        assert _row(wx)[0] == "active", "保存企微把微信安装吊销了"
        assert _row(wc)[0] == "revoked", "换 app 应吊销企微旧安装行"
        assert _row(new_wc)[0] == "active", "企微新安装应 active"
        im = _client.get("/api/chat/config").json()["im"]
        assert im["wechat"]["app_id"] == wx
        assert im["wecom"]["app_id"] == new_wc

    def test_unbind_wecom_keeps_wechat(self, _client):
        """解绑企微只吊销企微，微信照旧 active 并继续回显。"""
        wx, wc = _uid(), _uid()
        _seed("wechat", wx)
        _seed("wecom", wc)
        r = _client.put("/api/chat/config", json={"im": {
            "provider": "wecom", "app_id": "", "app_secret": ""}})
        assert r.status_code == 200, r.text
        assert _row(wc)[0] == "revoked"
        assert _row(wx)[0] == "active"
        im = _client.get("/api/chat/config").json()["im"]
        assert im["wechat"]["app_id"] == wx
        assert im["wecom"]["app_id"] == ""


class TestInstallationStatusField:
    def test_disabled_channel_still_echoes_with_status(self, _client):
        """已停用（disabled）的渠道仍要回显凭证 + installation_status，前端才能显示「已配置·已停用」。"""
        from src.im.engine_wiring import _connect, default_db_path
        dk = _uid()
        _seed("dingtalk", dk)
        conn = _connect(default_db_path())
        try:
            conn.execute("UPDATE grape_t_channel_installation SET status='disabled' WHERE legacy_app_id=?", (dk,))
            conn.commit()
        finally:
            conn.close()
        im = _client.get("/api/chat/config").json()["im"]
        assert im["dingtalk"]["app_id"] == dk
        assert im["dingtalk"]["installation_status"] == "disabled"

    def test_revoked_channel_not_echoed(self, _client):
        """revoked = 用户主动解绑，不能再回显（否则前端会把已解绑的渠道显示成已配置）。"""
        from src.im.engine_wiring import _connect, default_db_path
        fe = _uid()
        _seed("feishu", fe)
        conn = _connect(default_db_path())
        try:
            conn.execute("UPDATE grape_t_channel_installation SET status='revoked' WHERE legacy_app_id=?", (fe,))
            conn.commit()
        finally:
            conn.close()
        im = _client.get("/api/chat/config").json()["im"]
        assert im["feishu"]["app_id"] == ""
        assert im["feishu"]["installation_status"] == ""