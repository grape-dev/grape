"""IM 引擎对外接口测试（Issue #825 前端接入）：状态/安装列表/启停/模式开关。

这些接口只动新引擎侧（grape_t_channel_installation + 运行模式文件），不碰 legacy 表。
用例直接在 sqlite 测试库上调路由函数（FastAPI 的 Depends 在函数级不参与）。
"""
import json
import sqlite3

import pytest

from src.im import engine_wiring


@pytest.fixture()
def ui_db(tmp_path, monkeypatch):
    """本用例独立的 sqlite 库（建好渠道表），引擎连接一并指过去。"""
    path = str(tmp_path / "im_ui.db")
    monkeypatch.setenv("DEVOPS_DB_PATH", path)
    engine_wiring._reset_backend_cache()
    # 启动迁移会回填 legacy 双轨表，故先建最小 legacy 表（空表即可）
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS chat_agent_session (
            id INTEGER PRIMARY KEY, user_id INTEGER, repo_key TEXT,
            im_app_id TEXT, im_app_secret TEXT);
        CREATE TABLE IF NOT EXISTS devops_agent_session (
            id INTEGER PRIMARY KEY, repo_key TEXT, agent_type TEXT,
            im_app_id TEXT, im_app_secret TEXT);
        """
    )
    conn.commit()
    conn.close()
    engine_wiring.run_startup_migration(path)
    return path


def _user(uid=1234):
    return {"id": uid, "username": "tester", "auth_type": "jwt"}


def _fake_request():
    """最小 Request stub（审计取 IP 用）：仅提供 client.host。"""
    from types import SimpleNamespace
    return SimpleNamespace(client=SimpleNamespace(host="127.0.0.1"))


def test_installations_list_masks_secret_and_filters_scope(ui_db):
    from src.api.routes.im import im_installations

    engine_wiring.sync_installation("user", owner_user_id="1234",
                                           app_id="app_mine", app_secret="s3cr3t")
    engine_wiring.sync_installation("user", owner_user_id="9999",
                                           app_id="app_other", app_secret="s3cr3t")
    data = im_installations(repo="", user=_user())
    apps = [i["config"]["app_id"] for i in data["installations"]]
    assert apps == ["app_mine"], apps                      # 别人的 user 轨安装不可见
    row = data["installations"][0]
    assert row["config"]["app_secret"] == "********"        # secret 永远不回明文
    assert row["status"] == "active" and row["connected"] is False


def test_toggle_status_hides_from_provider(ui_db):
    from src.api.routes.im import InstallationStatusBody, im_set_installation_status
    from src.im.engine.installations import SqliteInstallationProvider
    import asyncio

    engine_wiring.sync_installation("user", owner_user_id="1234",
                                           app_id="app_toggle", app_secret="s")
    conn = sqlite3.connect(ui_db)
    inst_id = conn.execute("SELECT id FROM grape_t_channel_installation WHERE legacy_app_id='app_toggle'"
                           ).fetchone()[0]
    conn.close()

    prov = SqliteInstallationProvider(lambda: engine_wiring._connect(ui_db))
    assert [r["installation_id"] for r in asyncio.run(prov())] == [inst_id]

    out = im_set_installation_status(inst_id, InstallationStatusBody(status="disabled"),
                                     _fake_request(), user=_user())
    assert out["status"] == "disabled"
    assert asyncio.run(prov()) == []                        # 引擎侧停用 → 扫描不再枚举

    im_set_installation_status(inst_id, InstallationStatusBody(status="active"),
                               _fake_request(), user=_user())
    assert [r["installation_id"] for r in asyncio.run(prov())] == [inst_id]


def test_toggle_status_rejects_foreign_installation(ui_db):
    from fastapi import HTTPException
    from src.api.routes.im import InstallationStatusBody, im_set_installation_status

    engine_wiring.sync_installation("user", owner_user_id="9999",
                                           app_id="app_foreign", app_secret="s")
    conn = sqlite3.connect(ui_db)
    inst_id = conn.execute("SELECT id FROM grape_t_channel_installation WHERE legacy_app_id='app_foreign'"
                           ).fetchone()[0]
    conn.close()
    with pytest.raises(HTTPException) as ei:
        im_set_installation_status(inst_id, InstallationStatusBody(status="disabled"),
                                   _fake_request(), user=_user())
    assert ei.value.status_code == 403


def test_status_counts_installations(ui_db):
    from src.api.routes.im import im_engine_status

    engine_wiring.sync_installation("user", owner_user_id="1234",
                                           app_id="app_a", app_secret="s")
    engine_wiring.sync_installation("user", owner_user_id="1234",
                                           app_id="app_b", app_secret="s")
    engine_wiring.revoke_installation("user", owner_user_id="1234", app_id="app_b")
    st = im_engine_status(user=_user())
    assert st["mode"] in ("on", "off")
    assert st["installations"]["active"] == 1
    assert st["installations"]["revoked"] == 1
    assert st["scan_interval_sec"] == 30
    assert isinstance(st["channels"], int)

def test_onboard_scan_binding_also_writes_installation(ui_db, monkeypatch):
    """回归：飞书扫码绑定（feishu_onboard）也必须同步 grape_t_channel_installation。

    这正是"扫码绑定后发消息没回应"的根因：onboard 只写 legacy 两表，新引擎看不到安装 →
    不会有任何渠道在建连（health.realtime 的 im.channels 恒为 0）。
    """
    import sqlite3
    import src.session_manager as sm
    from src.api.routes import feishu_onboard

    conn = sqlite3.connect(ui_db)
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS global_im_bot_config (
            bot_app_id TEXT PRIMARY KEY, bot_type TEXT, is_active INTEGER DEFAULT 1,
            bot_name TEXT DEFAULT '');
        CREATE TABLE IF NOT EXISTS chat_conversations (im_channel_id TEXT);
        """
    )
    conn.commit()
    monkeypatch.setattr(sm, "db", conn, raising=False)
    monkeypatch.setattr(sm, "get_chat_config", lambda uid: {}, raising=False)
    monkeypatch.setattr(sm, "set_chat_config", lambda *a, **k: None, raising=False)

    err, old = feishu_onboard._apply_chat_credentials(1, "cli_onboard_proof", "sec")
    assert err == "" and old == ""
    row = conn.execute("SELECT scope, owner_user_id, status FROM grape_t_channel_installation "
                       "WHERE legacy_app_id='cli_onboard_proof'").fetchone()
    assert row == ("user", "1", "active"), row
    conn.close()
