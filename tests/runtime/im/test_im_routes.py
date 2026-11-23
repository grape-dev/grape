"""IM 引擎对外接口测试（Issue #825 前端接入）：状态/安装列表/启停/模式开关。

这些接口只动新引擎侧（grape_t_channel_installation + 运行模式文件），不碰 legacy 表。
用例直接在 sqlite 测试库上调路由函数（FastAPI 的 Depends 在函数级不参与）。
"""
import json
import sqlite3

import pytest

from src.domains.runtime.im import engine_wiring


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
    # 路由侧读写自本次起走归属域（mgmt）的「任意拓扑」原语：单库/测试拓扑回落到
    # 进程主库（src.kernel.session_manager.db），不再由路由自己开 IM 侧连接。
    # 因此本用例除了指 engine_wiring 的库，还要把进程主库钉到同一个临时库，
    # 否则「写进 tmp 库、读进程主库」两边看不到彼此。
    import src.kernel.session_manager as sm
    probe = sqlite3.connect(path, check_same_thread=False)
    probe.execute("PRAGMA busy_timeout=5000")
    monkeypatch.setattr(sm, "db", probe, raising=False)
    yield path
    probe.close()
    engine_wiring._reset_backend_cache()


def _user(uid=1234):
    return {"id": uid, "username": "tester", "auth_type": "jwt"}


def _fake_request():
    """最小 Request stub（审计取 IP 用）：仅提供 client.host。"""
    from types import SimpleNamespace
    return SimpleNamespace(client=SimpleNamespace(host="127.0.0.1"))


def test_installations_list_masks_secret_and_filters_scope(ui_db):
    from src.domains.runtime.routes.im import im_installations

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
    from src.domains.runtime.routes.im import InstallationStatusBody, im_set_installation_status
    from src.domains.runtime.im.engine.installations import SqliteInstallationProvider
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
    from src.domains.runtime.routes.im import InstallationStatusBody, im_set_installation_status

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
    from src.domains.runtime.routes.im import im_engine_status

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
    import src.kernel.session_manager as sm
    from src.domains.mgmt.routes import feishu_onboard

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


# ---------- 全集群租约 → connected 口径（跨主控误报的回归） ----------

def _seed_lease(db_path, installation_id, owner, expires_at):
    conn = sqlite3.connect(db_path)
    conn.execute(
        "CREATE TABLE IF NOT EXISTS grape_t_channel_lease ("
        " lease_key TEXT PRIMARY KEY, channel_type TEXT, installation_id TEXT,"
        " lease_token TEXT, lease_owner TEXT, lease_expires_at TEXT,"
        " lease_acquired_at TEXT, updated_at TEXT)")
    conn.execute(
        "INSERT OR REPLACE INTO grape_t_channel_lease"
        " (lease_key, channel_type, installation_id, lease_token, lease_owner,"
        "  lease_expires_at, lease_acquired_at, updated_at)"
        " VALUES (?,?,?,?,?,?,?,?)",
        ("feishu:" + installation_id, "feishu", installation_id, "tok", owner,
         expires_at, "2026-01-01 00:00:00", "2026-01-01 00:00:00"))
    conn.commit()
    conn.close()


def _inst_id_of(db_path, app_id):
    conn = sqlite3.connect(db_path)
    row = conn.execute("SELECT id FROM grape_t_channel_installation WHERE legacy_app_id=?",
                       (app_id,)).fetchone()
    conn.close()
    return row[0]


def test_installations_connected_via_cluster_lease(ui_db):
    """本机没连、但别的主控持有未过期租约 → connected=True 且 connected_on 指向持有者。"""
    from src.domains.runtime.routes.im import im_installations

    engine_wiring.sync_installation("user", owner_user_id="1234",
                                    app_id="app_lease_live", app_secret="s")
    inst_id = _inst_id_of(ui_db, "app_lease_live")
    _seed_lease(ui_db, inst_id, "ecs-grape-master-004-abcd1234",
                "2099-01-01 00:00:00")          # 空格口径、远期有效

    row = im_installations(repo="", user=_user())["installations"][0]
    assert row["connected"] is True
    assert row["connected_on"] == "ecs-grape-master-004-abcd1234"


def test_installations_expired_lease_is_not_connected(ui_db):
    """租约已过期（空格口径）→ connected=False。"""
    from src.domains.runtime.routes.im import im_installations

    engine_wiring.sync_installation("user", owner_user_id="1234",
                                    app_id="app_lease_dead", app_secret="s")
    inst_id = _inst_id_of(ui_db, "app_lease_dead")
    _seed_lease(ui_db, inst_id, "ecs-grape-master-001-abcd1234",
                "2000-01-01 00:00:00")

    row = im_installations(repo="", user=_user())["installations"][0]
    assert row["connected"] is False
    assert row["connected_on"] == ""


def test_installations_iso_expiry_is_compared_as_time_not_string(ui_db):
    """ISO 8601 到期串（acquire_channel_lease 口径）已过期 → 不得判成「未过期」。

    字符串比较会因第 11 位 'T'(84) > ' '(32) 把同 UTC 日的 ISO 值恒判为未来，
    这里钉住「解析成时间再比」的语义。
    """
    from src.domains.runtime.routes.im import im_installations

    engine_wiring.sync_installation("user", owner_user_id="1234",
                                    app_id="app_lease_iso", app_secret="s")
    inst_id = _inst_id_of(ui_db, "app_lease_iso")
    _seed_lease(ui_db, inst_id, "ecs-grape-master-002-abcd1234",
                "2000-01-01T00:00:00.000000+00:00")

    row = im_installations(repo="", user=_user())["installations"][0]
    assert row["connected"] is False, "ISO 过期串被当成了未过期（字符串比较回归）"


def test_lease_expiry_alive_accepts_both_formats(ui_db):
    from datetime import datetime, timezone
    from src.domains.mgmt.public import _lease_expiry_alive

    now = datetime(2026, 11, 20, 20, 0, 0, tzinfo=timezone.utc)
    assert _lease_expiry_alive("2026-11-20 20:03:00", now) is True
    assert _lease_expiry_alive("2026-11-20T20:03:00.000000+00:00", now) is True
    assert _lease_expiry_alive("2026-11-20 19:00:00", now) is False
    assert _lease_expiry_alive("2026-11-20T10:00:00.000000+00:00", now) is False
    assert _lease_expiry_alive("", now) is False          # 空到期按已失效
    assert _lease_expiry_alive("garbage", now) is False   # 脏值按已失效


# ---------- 生效路由（mgmt/routes/im.py，前端实际挂的那条） ----------

def test_mgmt_route_connected_via_cluster_lease(ui_db):
    """/api/im 实际挂的是 mgmt 路由：本机没连、租约活着 → connected=True。"""
    from src.domains.mgmt.routes.im import im_installations as mgmt_installations

    engine_wiring.sync_installation("user", owner_user_id="1234",
                                    app_id="app_mgmt_live", app_secret="s")
    inst_id = _inst_id_of(ui_db, "app_mgmt_live")
    _seed_lease(ui_db, inst_id, "ecs-grape-master-003-abcd1234", "2099-01-01 00:00:00")

    row = mgmt_installations(repo="", user=_user())["installations"][0]
    assert row["connected"] is True
    assert row["connected_on"] == "ecs-grape-master-003-abcd1234"


def test_mgmt_route_expired_iso_lease_not_connected(ui_db):
    """mgmt 路由同样不能把 ISO 过期串判成「未过期」。"""
    from src.domains.mgmt.routes.im import im_installations as mgmt_installations

    engine_wiring.sync_installation("user", owner_user_id="1234",
                                    app_id="app_mgmt_iso", app_secret="s")
    inst_id = _inst_id_of(ui_db, "app_mgmt_iso")
    _seed_lease(ui_db, inst_id, "ecs-grape-master-002-abcd1234",
                "2000-01-01T00:00:00.000000+00:00")

    row = mgmt_installations(repo="", user=_user())["installations"][0]
    assert row["connected"] is False
