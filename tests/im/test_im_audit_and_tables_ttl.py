"""A2 MR 评审整改回归：IM 安装变更审计（C3） + IM 只增表 TTL 清理（A7/F4）。

审计断言不依赖真实 DB 落库：把 src.auth.audit.log_audit 替换成记录器，
验证「启停 / 卡片配置 / 凭证保存解绑」这几条写路径确实产生审计事件，
且 detail 里绝不出现 secret 明文。
"""
import os
import sys
import uuid

import pytest
from fastapi.testclient import TestClient

ROOT = os.path.join(os.path.dirname(__file__), "..", "..")
sys.path.insert(0, ROOT)

from src.api.main import app
from src.api.deps import get_current_user, get_current_user_or_api_key

UID = 43111


@pytest.fixture()
def client():
    def _fake_user():
        return {"id": UID, "username": "audit_tester"}

    app.dependency_overrides[get_current_user] = _fake_user
    app.dependency_overrides[get_current_user_or_api_key] = _fake_user
    c = TestClient(app)
    yield c
    app.dependency_overrides.clear()


@pytest.fixture()
def audit_recorder(monkeypatch):
    """捕获 log_audit 调用（事件类型 + detail），不落真实审计表。"""
    import src.auth.audit as audit
    calls = []

    def _fake(event_type, user_id=0, operator_id=0, ip="", detail=None):
        calls.append({"event_type": event_type, "user_id": user_id,
                      "operator_id": operator_id, "detail": detail or {}})

    monkeypatch.setattr(audit, "log_audit", _fake)
    return calls


def _ew():
    from src.im.engine_wiring import _connect, default_db_path, run_startup_migration
    path = default_db_path()
    run_startup_migration(path)
    return _connect, path


def _seed(app_id, channel_type="wechat"):
    from src.im.engine_wiring import sync_installation
    _ew()
    assert sync_installation("user", owner_user_id=str(UID), app_id=app_id,
                             app_secret="sec_audit", channel_type=channel_type)
    conn_connect, path = _ew()
    conn = conn_connect(path)
    try:
        row = conn.execute("SELECT id FROM grape_t_channel_installation WHERE legacy_app_id=?",
                           (app_id,)).fetchone()
    finally:
        conn.close()
    return row[0]


def _drop(app_id):
    conn_connect, path = _ew()
    conn = conn_connect(path)
    try:
        conn.execute("DELETE FROM grape_t_channel_installation WHERE legacy_app_id=?", (app_id,))
        conn.commit()
    finally:
        conn.close()


def test_event_type_registered():
    import src.auth.audit as audit
    assert "im_installation_change" in audit.EVENT_TYPES


def test_enable_disable_writes_audit(client, audit_recorder):
    app_id = "aud_" + uuid.uuid4().hex[:8]
    inst_id = _seed(app_id)
    try:
        r = client.put(f"/api/im/installations/{inst_id}", json={"status": "disabled"})
        assert r.status_code == 200, r.text
        hits = [c for c in audit_recorder if c["event_type"] == "im_installation_change"]
        assert hits, "启停操作必须落审计"
        d = hits[-1]["detail"]
        assert d.get("action") == "installation_disabled"
        assert d.get("installation_id") == inst_id
        assert "sec_audit" not in str(d), "审计 detail 不得包含 secret"
    finally:
        _drop(app_id)


def test_chat_config_save_writes_audit(client, audit_recorder):
    app_id = "aud_" + uuid.uuid4().hex[:8]
    try:
        r = client.put("/api/chat/config", json={"im": {
            "provider": "wechat", "app_id": app_id, "app_secret": "sec_audit"}})
        assert r.status_code == 200, r.text
        hits = [c for c in audit_recorder if c["event_type"] == "im_installation_change"]
        assert hits, "IM 凭证保存必须落审计"
        d = hits[-1]["detail"]
        assert d.get("action") == "credential_save"
        assert d.get("provider") == "wechat"
        assert "sec_audit" not in str(d)
    finally:
        _drop(app_id)


def test_sweep_im_tables_purges_old_rows():
    """A7/F4：三张 IM 只增表的过期行会被 sweeper 清掉，新行保留。"""
    from src.runtime import cleanup
    from src.session_manager import db, db_lock

    with db_lock:
        c = db.cursor()
        for table in ("grape_t_channel_inbound_dedup", "grape_t_channel_inbound_message", "grape_t_channel_outbound_card"):
            try:
                # 只清本用例自己的种子行（M4：不做无 WHERE 全表 DELETE）
                c.execute(f"DELETE FROM {table} WHERE installation_id='i1'")
            except Exception:
                pass
        # dedup / inbound_message：旧行 + 新行
        try:
            c.execute("INSERT INTO grape_t_channel_inbound_dedup (installation_id, message_id, created_at)"
                      " VALUES ('i1','old',datetime('now','-48 hours'))")
            c.execute("INSERT INTO grape_t_channel_inbound_dedup (installation_id, message_id)"
                      " VALUES ('i1','new')")
        except Exception as e:
            pytest.skip(f"grape_t_channel_inbound_dedup unavailable: {e}")
        c.execute("INSERT INTO grape_t_channel_inbound_message (installation_id, message_id, created_at)"
                  " VALUES ('i1','old',datetime('now','-48 hours'))")
        c.execute("INSERT INTO grape_t_channel_inbound_message (installation_id, message_id)"
                  " VALUES ('i1','new')")
        try:
            c.execute("INSERT INTO grape_t_channel_outbound_card (out_track_id, installation_id, created_at)"
                      " VALUES ('t-old','i1',datetime('now','-200 hours'))")
            c.execute("INSERT INTO grape_t_channel_outbound_card (out_track_id, installation_id)"
                      " VALUES ('t-new','i1')")
        except Exception:
            pass
        db.commit()

    cleaned = cleanup._sweep_im_tables()
    assert cleaned >= 2, f"expected at least 2 purged rows, got {cleaned}"

    with db_lock:
        c = db.cursor()
        rows = c.execute("SELECT message_id FROM grape_t_channel_inbound_dedup ORDER BY message_id").fetchall()
        assert [r[0] for r in rows] == ["new"]
        rows = c.execute("SELECT message_id FROM grape_t_channel_inbound_message ORDER BY message_id").fetchall()
        assert [r[0] for r in rows] == ["new"]
        c.execute("DELETE FROM grape_t_channel_inbound_dedup WHERE installation_id='i1'")
        c.execute("DELETE FROM grape_t_channel_inbound_message WHERE installation_id='i1'")
        db.commit()

def test_wechat_qr_bind_writes_audit(audit_recorder):
    """C-2：微信 ilink 扫码绑定写凭证也必须落审计（detail 不含 bot_token）。"""
    from src.api.routes.chat import _persist_wechat_login
    ok = _persist_wechat_login({"id": UID}, {"bot_token": "tok_secret_abc",
                                             "ilink_bot_id": "wx_bot_" + uuid.uuid4().hex[:6],
                                             "ilink_user_id": "u1"})
    assert ok is True
    hits = [c for c in audit_recorder if c["event_type"] == "im_installation_change"]
    assert hits, "扫码绑定必须落审计"
    d = hits[-1]["detail"]
    assert d.get("action") == "wechat_qr_bind"
    assert d.get("provider") == "wechat"
    assert "tok_secret_abc" not in str(d), "审计 detail 不得包含 bot_token"
