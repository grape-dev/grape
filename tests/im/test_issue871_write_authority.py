"""Issue #871 → 旧 IM 下线 A2 M2：配置写入权威收敛为**只写 grape_t_channel_installation**。

原文件测试的是"写方向反转 + 双轨反向回写 + 对账收敛"；旧 IM 退役后这些机制已删除，
本文件改为验证单写语义（保留仍然有价值的不变量）：

  1. sync_installation 只写新表，legacy 双轨表**不再被改动**；
  2. 幂等：重复保存不增行，config 更新且状态保持 active；
  3. secret 兜底：入参为空时沿用新表已有 config（前端脱敏保存场景）；
  4. secret 全空 → 返回 False，不建行（空秘密不算可用安装）；
  5. revoke：置 revoked；无活跃引用时清理 legacy 凭证登记行（global_im_bot_config）；
  6. sync **不再**登记任何 legacy 行；租约在独立表 grape_t_channel_lease 且惰性创建（A2 M3）；
  7. get_active_installation 只返回 active 且可按 channel_type 过滤；
  8. write_mode / 反向回写函数已不存在（防止有人又加回来）。
"""
import os
import sqlite3
import sys

import pytest

from src.im import engine_wiring
from src.im.engine_wiring import (
    get_active_installation, revoke_installation, sync_installation,
)

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from src.im.engine_wiring import run_startup_migration  # noqa: E402


def _legacy_tables(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS chat_agent_session (
            id INTEGER PRIMARY KEY, user_id INTEGER, repo_key TEXT,
            im_app_id TEXT, im_app_secret TEXT, im_provider TEXT
        );
        CREATE TABLE IF NOT EXISTS devops_agent_session (
            id TEXT PRIMARY KEY, repo_key TEXT, agent_type TEXT,
            status TEXT, last_active_at TEXT,
            im_app_id TEXT, im_app_secret TEXT, im_provider TEXT
        );
        CREATE TABLE IF NOT EXISTS global_im_bot_config (
            bot_app_id TEXT, bot_type TEXT, bot_name TEXT
        );
        CREATE TABLE IF NOT EXISTS unified_user_mapping (
            user_id INTEGER, gitcode_user TEXT, feishu_open_id TEXT,
            feishu_name TEXT, gitcode_nickname TEXT, app_id TEXT
        );
        """
    )
    conn.commit()


@pytest.fixture
def db_path(tmp_path, monkeypatch):
    path = str(tmp_path / "authority.db")
    monkeypatch.setenv("DEVOPS_DB_PATH", path)
    conn = sqlite3.connect(path)
    _legacy_tables(conn)
    conn.execute(
        "INSERT INTO chat_agent_session (user_id, repo_key, im_app_id, im_app_secret, im_provider) "
        "VALUES (101, '', 'cli_legacy_only', 'legacy_secret', 'feishu')")
    conn.commit()
    conn.close()
    run_startup_migration(path)
    return path


def _q(db, sql, params=()):
    conn = sqlite3.connect(db)
    try:
        return conn.execute(sql, params).fetchall()
    finally:
        conn.close()


# ---------------------------------------------------------------- 单写语义

def test_sync_writes_new_table_only(db_path):
    """新表落行，legacy 双轨表保持原样（不再反向回写）。"""
    before = _q(db_path, "SELECT im_app_id, im_app_secret, im_provider FROM chat_agent_session WHERE user_id=101")
    assert sync_installation("user", owner_user_id="101", app_id="cli_u1",
                             app_secret="s1", channel_type="feishu") is True

    rows = _q(db_path, "SELECT channel_type, scope, status, legacy_app_id, config "
                       "FROM grape_t_channel_installation WHERE legacy_app_id='cli_u1'")
    assert len(rows) == 1
    ctype, scope, status, app_id, cfg_json = rows[0]
    assert (ctype, scope, status, app_id) == ("feishu", "user", "active", "cli_u1")
    assert '"app_secret": "s1"' in cfg_json.replace('"app_secret":"s1"', '"app_secret": "s1"')

    after = _q(db_path, "SELECT im_app_id, im_app_secret, im_provider FROM chat_agent_session WHERE user_id=101")
    assert after == before, "legacy 双轨行不应再被写"


def test_sync_idempotent_updates_config(db_path):
    assert sync_installation("user", owner_user_id="101", app_id="cli_u1", app_secret="s1")
    assert sync_installation("user", owner_user_id="101", app_id="cli_u1", app_secret="s2",
                             extra_config={"bot_token": "bt"})
    rows = _q(db_path, "SELECT id, status, config FROM grape_t_channel_installation WHERE legacy_app_id='cli_u1'")
    assert len(rows) == 1, "同 app 同轨应幂等为一行"
    assert rows[0][1] == "active"
    assert '"s2"' in rows[0][2] and '"bot_token": "bt"' in rows[0][2]


def test_sync_secret_falls_back_to_existing_config(db_path):
    """前端脱敏保存（secret 传空/掩码）→ 沿用新表已有 secret。"""
    assert sync_installation("user", owner_user_id="101", app_id="cli_u2", app_secret="real_secret")
    assert sync_installation("user", owner_user_id="101", app_id="cli_u2", app_secret="") is True
    rows = _q(db_path, "SELECT config FROM grape_t_channel_installation WHERE legacy_app_id='cli_u2'")
    assert '"real_secret"' in rows[0][0]


def test_sync_without_any_secret_creates_nothing(db_path):
    assert sync_installation("user", owner_user_id="101", app_id="cli_u3") is False
    assert _q(db_path, "SELECT id FROM grape_t_channel_installation WHERE legacy_app_id='cli_u3'") == []


def test_sync_does_not_register_lease_carrier(db_path):
    """A2 M3：sync 不再往 legacy 表登记载体行；租约落 grape_t_channel_lease 且首次抢锁才建行。"""
    assert sync_installation("user", owner_user_id="101", app_id="cli_u4", app_secret="s")
    assert _q(db_path, "SELECT bot_app_id FROM global_im_bot_config WHERE bot_app_id='cli_u4'") == []
    assert _q(db_path, "SELECT lease_key FROM grape_t_channel_lease") == [], "sync 不应预铺租约行"

    from src.im.bot_lease import BotLeaseStore
    from src.session_manager import db as _sdb, db_lock
    inst_id = _q(db_path, "SELECT id FROM grape_t_channel_installation WHERE legacy_app_id='cli_u4'")[0][0]
    store = BotLeaseStore("t-inst")
    key = f"feishu:{inst_id}"
    token = store.try_acquire(key)
    assert token
    # 租约表走主连接（生产=同一个库；测试里 session_manager 与 db_path 各挂一个 sqlite）
    with db_lock:
        row = _sdb.cursor().execute(
            "SELECT lease_token FROM grape_t_channel_lease WHERE lease_key=?", (key,)).fetchone()
    assert row and row[0] == token
    store.release(key, token)


def test_revoke_marks_installation_and_leaves_legacy_rows_alone(db_path):
    """A2 收尾：revoke 只置 revoked，不再清理 legacy 登记行（已无写入方）。"""
    assert sync_installation("user", owner_user_id="101", app_id="cli_u5", app_secret="s")
    conn = sqlite3.connect(db_path)
    conn.execute("INSERT OR IGNORE INTO global_im_bot_config (bot_app_id, bot_type) "
                 "VALUES ('cli_u5', 'chat_bot')")
    conn.commit(); conn.close()
    assert revoke_installation("user", owner_user_id="101", app_id="cli_u5") is True
    rows = _q(db_path, "SELECT status FROM grape_t_channel_installation WHERE legacy_app_id='cli_u5'")
    assert rows and rows[0][0] == "revoked"
    assert _q(db_path, "SELECT bot_app_id FROM global_im_bot_config WHERE bot_app_id='cli_u5'") != [], \
        "revoke 不应再动 legacy 登记行"


def test_revoke_keeps_carrier_when_other_active_install_references(db_path):
    assert sync_installation("user", owner_user_id="101", app_id="cli_shared", app_secret="s")
    assert sync_installation("repo", repo_key="owner/repo", app_id="cli_shared", app_secret="s")
    conn = sqlite3.connect(db_path)
    conn.execute("INSERT OR IGNORE INTO global_im_bot_config (bot_app_id, bot_type) "
                 "VALUES ('cli_shared', 'chat_bot')")
    conn.commit(); conn.close()
    revoke_installation("user", owner_user_id="101", app_id="cli_shared")
    rows = _q(db_path, "SELECT bot_app_id FROM global_im_bot_config WHERE bot_app_id='cli_shared'")
    assert rows, "其它轨仍活跃时不得清理租约载体"


def test_get_active_installation_filters(db_path):
    # 夹具里 chat_agent_session 预置的行会被 startup 迁移回填成活跃安装，先吊销它再测
    revoke_installation("user", owner_user_id="101", app_id="cli_legacy_only")
    assert sync_installation("user", owner_user_id="101", app_id="cli_u6", app_secret="s",
                             channel_type="feishu")
    inst = get_active_installation("user", owner_user_id="101", channel_type="feishu")
    assert inst and inst["config"]["app_id"] == "cli_u6"
    assert get_active_installation("user", owner_user_id="101", channel_type="wechat") is None
    revoke_installation("user", owner_user_id="101", app_id="cli_u6")
    assert get_active_installation("user", owner_user_id="101", channel_type="feishu") is None


def test_dual_write_mechanisms_are_gone():
    """防回归：写方向开关与双轨回写/对账函数不得再出现。"""
    for name in ("write_mode", "_backwrite_legacy_installation", "_backwrite_legacy_unbind",
                 "_legacy_secret_sync", "_converge_installations"):
        assert not hasattr(engine_wiring, name), f"{name} 应已随旧 IM 下线删除"
    for name in ("sync_legacy_installation", "revoke_legacy_installation"):
        assert not hasattr(engine_wiring, name)