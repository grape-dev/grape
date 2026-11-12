"""POST /api/im/dingtalk/card-setup 看护（QA #1231 行为级覆盖门补齐）。

背景：深度审查裁决把 `POST /dingtalk/card-setup`（src/domains/runtime/routes/im.py）
列为高风险写端点零原子看护（BLOCK）。本文件按端点契约锁四条路径：

1. GET 回显：未配置时 cards_enabled=False（前端「卡片按钮」一栏读它）。
2. 空 template_id 保存 = 关闭互动卡片（cards_enabled=False，回落文字卡）。
3. 带 template_id + 探活成功 = 启用（cards_enabled=True，卡模板 ID 落配置）。
4. 探活失败（上游 401/403/无 status）= 400 带修复 hint，不落半开配置。

探活（DingtalkCardClient.create_instance）一律 mock——真实钉钉平台不在测试范围。
"""
import json
import uuid

import pytest
from fastapi.testclient import TestClient

from src.api.main import app
from src.api.deps import get_current_user, get_current_user_or_api_key

UID = 43199
USERNAME = "card_setup_guard"
PREFIX = "cli_cardsetup_"
APP_ID = PREFIX + "probe"
_TPL = "tpl-card-setup-guard"


@pytest.fixture(autouse=True)
def client():
    def _fake_user():
        return {"id": UID, "username": USERNAME}

    app.dependency_overrides[get_current_user] = _fake_user
    app.dependency_overrides[get_current_user_or_api_key] = _fake_user
    tc = TestClient(app)
    yield tc
    app.dependency_overrides.clear()
    _drop_installations()


def _wiring():
    from src.domains.runtime.im.engine_wiring import _connect, default_db_path, run_startup_migration
    path = default_db_path()
    run_startup_migration(path)
    return _connect, path


def _drop_installations():
    _connect, path = _wiring()
    conn = _connect(path)
    try:
        conn.execute(
            "DELETE FROM grape_t_channel_installation WHERE legacy_app_id LIKE ?", (PREFIX + "%",))
        conn.commit()
    finally:
        conn.close()


def _seed_install(app_id: str, app_secret: str = "sec_cardsetup") -> str:
    from src.domains.runtime.im.engine_wiring import sync_installation
    _wiring()
    assert sync_installation("user", owner_user_id=str(UID), app_id=app_id,
                             app_secret=app_secret, channel_type="dingtalk")
    _connect, path = _wiring()
    conn = _connect(path)
    try:
        row = conn.execute(
            "SELECT id FROM grape_t_channel_installation WHERE legacy_app_id=?", (app_id,)).fetchone()
        assert row, "seeded installation missing"
        return str(row[0])
    finally:
        conn.close()


def _config_of(installation_id: str) -> dict:
    _connect, path = _wiring()
    conn = _connect(path)
    try:
        row = conn.execute(
            "SELECT config FROM grape_t_channel_installation WHERE id=?",
            (installation_id,)).fetchone()
        return json.loads(row[0] or "{}") if row else {}
    finally:
        conn.close()


def _mock_probe(monkeypatch, ok: bool, data: dict):
    """mock DingtalkCardClient.create_instance（探活只建实例不投放）。"""
    async def _fake_create(self, template_id, out_track_id, data):
        return ok, data
    monkeypatch.setattr(
        "src.domains.runtime.im.adapters.dingtalk.cards.DingtalkCardClient.create_instance",
        _fake_create)


def test_get_card_setup_defaults(client):
    inst = _seed_install(APP_ID)
    r = client.get("/api/im/dingtalk/card-setup", params={"installation_id": inst})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body.get("configured") is False and body.get("enabled") is False, "未配置时必须显示未启用"
    assert body.get("mode") == "action_card", "未启用互动卡片时回落文字卡模式"
    assert body.get("installation_id") == inst


def test_post_empty_template_disables_cards(client):
    inst = _seed_install(APP_ID)
    r = client.post("/api/im/dingtalk/card-setup", json={"installation_id": inst, "template_id": ""})
    assert r.status_code == 200, r.text
    assert r.json().get("enabled") is False, "空 template_id = 关闭互动卡片"
    cfg = _config_of(inst)
    assert cfg.get("cards_enabled") is False


def test_post_with_template_probe_ok_enables(client, monkeypatch):
    inst = _seed_install(APP_ID)
    _mock_probe(monkeypatch, True, {})
    r = client.post("/api/im/dingtalk/card-setup",
               json={"installation_id": inst, "template_id": _TPL})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body.get("configured") is True and body.get("enabled") is True, "探活成功必须启用"
    assert body.get("mode") == "interactive"
    cfg = _config_of(inst)
    assert cfg.get("dingtalk_cards", {}).get("card") == _TPL, "模板 ID 必须落安装配置"
    assert cfg.get("cards_enabled") is True


def test_post_with_template_probe_fail_is_400(client, monkeypatch):
    inst = _seed_install(APP_ID)
    _mock_probe(monkeypatch, False, {"status": 403})
    r = client.post("/api/im/dingtalk/card-setup",
               json={"installation_id": inst, "template_id": _TPL})
    assert r.status_code == 400, r.text
    detail = r.json().get("detail") or {}
    assert detail.get("message") == "模板/权限校验失败"
    assert detail.get("hint"), "400 必须带修复 hint"
    cfg = _config_of(inst)
    assert not cfg.get("cards_enabled"), "探活失败不得落半开配置"


def test_post_installation_not_found_is_404(client):
    r = client.post("/api/im/dingtalk/card-setup",
               json={"installation_id": "no-such-install", "template_id": _TPL})
    assert r.status_code == 404, r.text
