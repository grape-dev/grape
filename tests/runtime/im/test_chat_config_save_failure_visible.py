"""IM 渠道保存失败必须可见（2026-11-18）。

背景：`PUT /api/chat/config` 的安装表写入原先 `try/except → log.warning`，接口照样 200。
渠道表缺失 / 库路径错那阵子，页面提示“配置已更新”、徽标永远“未连接”，
用户无从判断配置到底有没有生效（本机静默失败数天）。

锁三件事：
  1. sync_installation 返回 False（写入未命中）→ 502 + 明确 message；
  2. sync_installation 抛异常 → 502（不是 200）；
  3. 解绑时 revoke_installation 未命中 → 502（否则用户以为解绑了，引擎还在连）。
"""
import uuid

import pytest
from fastapi.testclient import TestClient

from src.api.main import app
from src.api.deps import get_current_user, get_current_user_or_api_key

UID = 43188
USERNAME = "im_save_failure_tester"
PREFIX = "cli_fail_"


@pytest.fixture()
def _client(monkeypatch):
    def _fake_user():
        return {"id": UID, "username": USERNAME}

    app.dependency_overrides[get_current_user] = _fake_user
    app.dependency_overrides[get_current_user_or_api_key] = _fake_user
    c = TestClient(app)
    yield c
    app.dependency_overrides.clear()
    _cleanup_seeded()


def _ew():
    from src.domains.runtime.im.engine_wiring import _connect, default_db_path, run_startup_migration
    path = default_db_path()
    run_startup_migration(path)
    return _connect, path


def _seed(channel_type: str, app_id: str) -> None:
    from src.domains.runtime.im.engine_wiring import sync_installation
    _ew()
    assert sync_installation("user", owner_user_id=str(UID), app_id=app_id,
                             app_secret="sec", channel_type=channel_type)


def _cleanup_seeded() -> None:
    _connect, path = _ew()
    conn = _connect(path)
    try:
        conn.execute("DELETE FROM grape_t_channel_installation WHERE legacy_app_id LIKE ?",
                     (PREFIX + "%",))
        conn.commit()
    finally:
        conn.close()


def _uid() -> str:
    return PREFIX + uuid.uuid4().hex[:6]


def test_save_returns_502_when_installation_write_returns_false(_client):
    import src.domains.runtime.im.engine_wiring as ew
    orig = ew.sync_installation
    ew.sync_installation = lambda *a, **k: False
    try:
        r = _client.put("/api/chat/config", json={"im": {
            "provider": "wecom", "app_id": _uid(), "app_secret": "sec"}})
    finally:
        ew.sync_installation = orig
    assert r.status_code == 502, r.text
    detail = r.json()["detail"]
    assert "未保存成功" in detail["message"], detail
    assert "未写入" in detail["reason"], detail


def test_save_returns_502_when_installation_write_raises(_client):
    import src.domains.runtime.im.engine_wiring as ew
    orig = ew.sync_installation

    def _boom(*a, **k):
        raise RuntimeError("db is gone")

    ew.sync_installation = _boom
    try:
        r = _client.put("/api/chat/config", json={"im": {
            "provider": "telegram", "app_id": _uid(), "app_secret": ""}})
    finally:
        ew.sync_installation = orig
    assert r.status_code == 502, r.text
    assert "db is gone" in r.json()["detail"]["reason"]


def test_unbind_returns_502_when_revoke_misses(_client):
    import src.domains.runtime.im.engine_wiring as ew
    widened = _uid()
    _seed("wecom", widened)
    orig = ew.revoke_installation
    ew.revoke_installation = lambda *a, **k: False
    try:
        r = _client.put("/api/chat/config", json={"im": {
            "provider": "wecom", "app_id": "", "app_secret": ""}})
    finally:
        ew.revoke_installation = orig
    assert r.status_code == 502, r.text
    assert "未撤销" in r.json()["detail"]["reason"]


def test_save_still_200_on_success(_client):
    """反向锁：写入成功时仍 200，别把正常保存也变成 502。"""
    r = _client.put("/api/chat/config", json={"im": {
        "provider": "wecom", "app_id": _uid(), "app_secret": "sec"}})
    assert r.status_code == 200, r.text
