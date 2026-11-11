"""双轨安装回填测试（Issue #742 §7.8 test_installation_migration_backfill）。

§5.3 第 1 步：load_eligible_bots 双轨语义 → grape_t_channel_installation 行生成正确
（app_id 去重语义、scope 判定、fingerprint、app 跨仓库共享）。
"""
import importlib.util
import os
import sqlite3
import sys

_MIG = os.path.join(os.path.dirname(__file__), "..", "..", "scripts", "migrations", "001_channel_tables.py")
_spec = importlib.util.spec_from_file_location("migrations_001", _MIG)
migrations_001 = importlib.util.module_from_spec(_spec)
sys.modules["migrations_001"] = migrations_001
_spec.loader.exec_module(migrations_001)
backfill_installations = migrations_001.backfill_installations
ensure_channel_tables = migrations_001.ensure_channel_tables
list_installations = migrations_001.list_installations


def _fresh_db():
    conn = sqlite3.connect(":memory:")
    c = conn.cursor()
    c.executescript(
        """
        CREATE TABLE chat_agent_session (
            id INTEGER PRIMARY KEY, user_id INTEGER, repo_key TEXT,
            im_app_id TEXT, im_app_secret TEXT, im_provider TEXT
        );
        CREATE TABLE devops_agent_session (
            id INTEGER PRIMARY KEY, repo_key TEXT, agent_type TEXT,
            im_app_id TEXT, im_app_secret TEXT, im_provider TEXT
        );
        """
    )
    return conn


def test_backfill_user_track_dedup_by_app_id():
    """user 轨：同 app_id 多用户行 → 一条安装（app_id 去重语义 L1570）。"""
    conn = _fresh_db()
    c = conn.cursor()
    c.execute("INSERT INTO chat_agent_session (user_id, repo_key, im_app_id, im_app_secret) VALUES (101,'','app_chat_1','s1')")
    c.execute("INSERT INTO chat_agent_session (user_id, repo_key, im_app_id, im_app_secret) VALUES (102,'','app_chat_1','s1dup')")
    c.execute("INSERT INTO chat_agent_session (user_id, repo_key, im_app_id, im_app_secret) VALUES (103,'','app_chat_2','s2')")
    # repo_key 非空的行不属于用户轨（load_eligible_bots WHERE repo_key=''）
    c.execute("INSERT INTO chat_agent_session (user_id, repo_key, im_app_id, im_app_secret) VALUES (104,'iso/r','app_chat_3','s3')")
    conn.commit()

    stats = backfill_installations(conn)
    assert stats["user"] == 2
    user_apps = [i["config"]["app_id"] for i in list_installations(conn) if i["scope"] == "user"]
    assert sorted(user_apps) == ["app_chat_1", "app_chat_2"]


def test_backfill_repo_track_shared_app():
    """repo 轨：app 可跨仓库共享 → 每仓库一条安装（陷阱 11 守卫）。"""
    conn = _fresh_db()
    c = conn.cursor()
    c.execute("INSERT INTO devops_agent_session (repo_key, agent_type, im_app_id, im_app_secret) VALUES ('iso/r1','vod','app_vod','sv')")
    c.execute("INSERT INTO devops_agent_session (repo_key, agent_type, im_app_id, im_app_secret) VALUES ('iso/r2','vod','app_vod','sv')")
    conn.commit()

    stats = backfill_installations(conn)
    assert stats["repo"] == 2
    repo_rows = [i for i in list_installations(conn) if i["scope"] == "repo"]
    assert sorted(r["repo_key"] for r in repo_rows) == ["iso/r1", "iso/r2"]
    assert all(r["config"]["app_id"] == "app_vod" for r in repo_rows)


def test_backfill_repo_track_respects_im_provider():
    """repo 轨也必须按 im_provider 落 channel_type（否则企微重启后被当飞书建连）。"""
    conn = _fresh_db()
    c = conn.cursor()
    c.execute("INSERT INTO devops_agent_session (repo_key, agent_type, im_app_id, im_app_secret, im_provider) "
              "VALUES ('iso/w1','vod','app_wecom','sw','wecom')")
    c.execute("INSERT INTO devops_agent_session (repo_key, agent_type, im_app_id, im_app_secret, im_provider) "
              "VALUES ('iso/w2','vod','app_ding','sd','dingtalk')")
    c.execute("INSERT INTO devops_agent_session (repo_key, agent_type, im_app_id, im_app_secret, im_provider) "
              "VALUES ('iso/w3','vod','app_legacy','sl','')")
    conn.commit()

    backfill_installations(conn, channel_type="feishu")
    rows = {r["repo_key"]: r["channel_type"] for r in list_installations(conn) if r["scope"] == "repo"}
    assert rows == {"iso/w1": "wecom", "iso/w2": "dingtalk", "iso/w3": "feishu"}


def test_backfill_scope_and_fingerprint():
    conn = _fresh_db()
    c = conn.cursor()
    c.execute("INSERT INTO chat_agent_session (user_id, repo_key, im_app_id, im_app_secret) VALUES (200,'','app_x','sec')")
    conn.commit()
    backfill_installations(conn)
    inst = list_installations(conn)[0]
    assert inst["scope"] == "user" and inst["channel_type"] == "feishu"
    assert inst["config_fingerprint"]  # 凭证轮转热重建依据（§4.2 fingerprint）
    assert inst["status"] == "active"


def test_backfill_is_insert_only_and_never_overwrites_ui_config():
    """幂等：重复回填不增行；且**不覆盖**既有安装的 config。

    A2 收尾后 legacy 列不再随 UI 更新（UI 只写 grape_t_channel_installation），
    若回填还按 legacy 覆盖 config，会把新 secret 打回旧值 → 引擎用旧凭证连不上。
    """
    conn = _fresh_db()
    c = conn.cursor()
    c.execute("INSERT INTO chat_agent_session (user_id, repo_key, im_app_id, im_app_secret) VALUES (300,'','app_y','sec_v1')")
    conn.commit()
    backfill_installations(conn)
    backfill_installations(conn)  # 幂等重跑
    assert len(list_installations(conn)) == 1

    c.execute("UPDATE chat_agent_session SET im_app_secret='sec_v2' WHERE im_app_id='app_y'")
    conn.commit()
    backfill_installations(conn)
    rows = list_installations(conn)
    assert len(rows) == 1
    assert rows[0]["config"]["app_secret"] == "sec_v1", "回填不得覆盖既有安装的 config"
    assert rows[0]["config_fingerprint"]
