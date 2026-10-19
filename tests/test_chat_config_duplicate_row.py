"""chat_agent_session 重复配置行导致解绑失效的回归测试。

根因（chat-config-duplicate-row-unbind-fix-design.md）：同 user 存在两条 repo_key='' 配置行时，
get_chat_config 无序 LIMIT 1 可能命中空行，导致解绑/配置清空落到错误行，真绑定行永不清。
"""
import pytest
import os
import sys

from src.session_manager import db, db_lock
from src.session_manager import get_chat_config, set_chat_config, _write_chat_config_row

GA_KEY = "gitcode:shopowner/shop"

@pytest.fixture(autouse=True)
def _clean():
    with db_lock:
        c = db.cursor()
        c.execute("DELETE FROM chat_agent_session WHERE user_id IN (9001, 9002, 9003)")
        c.execute("DELETE FROM repo_im_group_binding WHERE im_chat_id IN ('oc_test_grp','oc_test_grp2') OR im_app_id LIKE 'app_9%'")
        c.execute("DELETE FROM bot_seen_messages WHERE app_id LIKE 'app_9%' OR app_id='other_app'")
        c.execute("DELETE FROM feishu_onboard_task WHERE app_id LIKE 'app_9%' OR app_id='other_app'")
        db.commit()
    yield
    with db_lock:
        c = db.cursor()
        c.execute("DELETE FROM chat_agent_session WHERE user_id IN (9001, 9002, 9003)")
        c.execute("DELETE FROM repo_im_group_binding WHERE im_chat_id IN ('oc_test_grp','oc_test_grp2') OR im_app_id LIKE 'app_9%'")
        c.execute("DELETE FROM bot_seen_messages WHERE app_id LIKE 'app_9%' OR app_id='other_app'")
        c.execute("DELETE FROM feishu_onboard_task WHERE app_id LIKE 'app_9%' OR app_id='other_app'")
        db.commit()


def _seed_rows(user_id):
    """同 user 两条 repo_key='' 行：先空行后带配置行（复刻账号迁移残留）。"""
    with db_lock:
        c = db.cursor()
        # 空行（先插入，get_chat_config 无序时会被命中）
        c.execute(
            "INSERT INTO chat_agent_session (id, user_id, repo_key, im_app_id, im_app_secret, im_provider) "
            "VALUES ('sess-cfg-%d-a', %d, '', '', '', '')" % (user_id, user_id))
        # 带 im 配置的行（真绑定行）
        c.execute(
            "INSERT INTO chat_agent_session (id, user_id, repo_key, im_app_id, im_app_secret, im_provider) "
            "VALUES ('sess-cfg-%d-b', %d, '', 'app_%d', 'sec_%d', 'feishu')" % (user_id, user_id, user_id, user_id))
        db.commit()


def _seed_group_binding(im_app_id, repo_key="gitcode:owner/repo", im_chat_id="oc_test_grp"):
    """群绑定：模拟该 bot 名下的群绑定记录（解绑应一并清）。"""
    with db_lock:
        c = db.cursor()
        c.execute(
            "INSERT INTO repo_im_group_binding (im_chat_id, repo_key, source, bind_by, im_app_id, im_provider) "
            "VALUES (?, ?, 'gitcode', '100', ?, 'feishu')",
            (im_chat_id, repo_key, im_app_id))
        db.commit()


class TestGetChatConfigDuplicateRow:
    def test_ut01_picks_configured_row(self):
        """UT-01：存在空行 + 配置行时，get_chat_config 返回配置行的 im_app_id。"""
        _seed_rows(9001)
        cfg = get_chat_config(9001)
        assert cfg["im_app_id"] == "app_9001", f"应命中已配置行, got {cfg['im_app_id']!r}"

    def test_ut02_empty_only(self):
        """UT-02：仅空配置行，返回空 im_app_id（兜底，不抛错）。"""
        with db_lock:
            c = db.cursor()
            c.execute(
                "INSERT INTO chat_agent_session (id, user_id, repo_key, im_app_id, im_app_secret, im_provider) "
                "VALUES ('sess-cfg-9002-a', 9002, '', '', '', '')")
            db.commit()
        cfg = get_chat_config(9002)
        assert cfg["im_app_id"] == ""

    def test_ut03_set_clears_configured_row(self):
        """UT-03：set_chat_config 清空 im 字段后，DB 配置行真被清空（不只剩空行为空）。"""
        _seed_rows(9003)
        set_chat_config(9003, im_app_id="", im_app_secret="", im_provider="")
        with db_lock:
            rows = db.cursor().execute(
                "SELECT id, im_app_id, im_app_secret, im_provider FROM chat_agent_session "
                "WHERE user_id=9003 AND repo_key='' ORDER BY id").fetchall()
        # 断言：所有行 im 字段均为空（真绑定行被清掉，不再残留 app）
        for rid, aid, sec, prov in rows:
            assert aid == "", f"行 {rid} im_app_id 应被清空, got {aid!r}"
            assert prov == "", f"行 {rid} im_provider 应被清空, got {prov!r}"


class TestWriteRowDedup:
    def test_ut05_write_cleans_duplicates(self):
        """UT-05：_write_chat_config_row 写配置前清理同 user 重复 repo_key='' 行，只留一条。"""
        _seed_rows(9001)
        _write_chat_config_row(9001, im_app_id="app_x", im_app_secret="s", im_provider="feishu")
        with db_lock:
            rows = db.cursor().execute(
                "SELECT id, im_app_id FROM chat_agent_session WHERE user_id=9001 AND repo_key=''").fetchall()
        assert len(rows) == 1, f"应只剩一条, got {rows}"
        assert rows[0][1] == "app_x"


class TestDedupMigration:
    def test_ut06_migration_idempotent(self):
        """UT-06：重复行清理迁移幂等，重跑无副作用。"""
        from src.api.db_setup import _cleanup_dup_chat_config_rows
        _seed_rows(9001)
        _cleanup_dup_chat_config_rows()
        _cleanup_dup_chat_config_rows()
        with db_lock:
            rows = db.cursor().execute(
                "SELECT id, im_app_id FROM chat_agent_session WHERE user_id=9001 AND repo_key=''").fetchall()
        assert len(rows) == 1, f"迁移后应只剩一条, got {rows}"

    def test_ut06b_single_row_unchanged(self):
        """UT-06b：仅一条配置行时迁移无副作用。"""
        from src.api.db_setup import _cleanup_dup_chat_config_rows
        _write_chat_config_row(9002, im_app_id="app_9002", im_app_secret="s", im_provider="feishu")
        _cleanup_dup_chat_config_rows()
        with db_lock:
            rows = db.cursor().execute(
                "SELECT id, im_app_id FROM chat_agent_session WHERE user_id=9002 AND repo_key=''").fetchall()
        assert len(rows) == 1
        assert rows[0][1] == "app_9002"


class TestUnbindCleansGroupBinding:
    def test_ut08_unbind_removes_group_binding(self):
        """UT-08：解绑（_purge_chat_im_binding）后，该 bot 名下的群绑定一并删除。"""
        from src.api.routes.chat import _purge_chat_im_binding
        _seed_group_binding("app_9001")
        _seed_group_binding("app_9001", repo_key="gitcode:owner/other", im_chat_id="oc_test_grp2")  # 同一 bot 多个群绑定
        _purge_chat_im_binding(9001, "gitcode:owner", "app_9001")
        with db_lock:
            rows = db.cursor().execute(
                "SELECT im_chat_id, repo_key, im_app_id FROM repo_im_group_binding WHERE im_app_id='app_9001'").fetchall()
        assert rows == [], f"解绑后该 bot 群绑定应全清, got {rows}"

    def test_ut08b_other_bot_binding_kept(self):
        """UT-08b：其他 bot 的群绑定不受影响。"""
        from src.api.routes.chat import _purge_chat_im_binding
        _seed_group_binding("app_9001")
        _seed_group_binding("other_app")  # 另一 bot 的绑定，保持
        _purge_chat_im_binding(9001, "gitcode:owner", "app_9001")
        with db_lock:
            rows = db.cursor().execute(
                "SELECT im_app_id FROM repo_im_group_binding WHERE im_app_id='other_app'").fetchall()
        assert len(rows) == 1, "不应误删其他 bot 的群绑定"

    def test_ut08c_unbind_clears_seen_messages(self):
        """UT-08c：解绑后 bot_seen_messages 该 app 记录一并清空（其他 app 保留）。"""
        from src.api.routes.chat import _purge_chat_im_binding
        with db_lock:
            c = db.cursor()
            c.execute("INSERT INTO bot_seen_messages (app_id, message_id, seen_at) VALUES ('app_9001','m1',100)")
            c.execute("INSERT INTO bot_seen_messages (app_id, message_id, seen_at) VALUES ('app_9001','m2',200)")
            c.execute("INSERT INTO bot_seen_messages (app_id, message_id, seen_at) VALUES ('other_app','m3',300)")
            db.commit()
        _purge_chat_im_binding(9001, "gitcode:owner", "app_9001")
        with db_lock:
            n_app = db.cursor().execute("SELECT COUNT(*) FROM bot_seen_messages WHERE app_id='app_9001'").fetchone()[0]
            n_other = db.cursor().execute("SELECT COUNT(*) FROM bot_seen_messages WHERE app_id='other_app'").fetchone()[0]
        assert n_app == 0, f"解绑应清空该 app 的 seen_messages, got {n_app}"
        assert n_other == 1, "不应误删其他 app 的 seen_messages"

    def test_ut08d_unbind_clears_onboard_task(self):
        """UT-08d：解绑后 feishu_onboard_task 该 app 记录一并清空（其他 app 保留）。"""
        from src.api.routes.chat import _purge_chat_im_binding
        with db_lock:
            c = db.cursor()
            c.execute("INSERT INTO feishu_onboard_task (task_id, bot_type, repo_key, user_id, status, app_id) "
                      "VALUES ('t1','chat_bot','',9001,'connected','app_9001')")
            c.execute("INSERT INTO feishu_onboard_task (task_id, bot_type, repo_key, user_id, status, app_id) "
                      "VALUES ('t2','chat_bot','',9001,'connected','other_app')")
            db.commit()
        _purge_chat_im_binding(9001, "gitcode:owner", "app_9001")
        with db_lock:
            n_app = db.cursor().execute("SELECT COUNT(*) FROM feishu_onboard_task WHERE app_id='app_9001'").fetchone()[0]
            n_other = db.cursor().execute("SELECT COUNT(*) FROM feishu_onboard_task WHERE app_id='other_app'").fetchone()[0]
        assert n_app == 0, f"解绑应清空该 app 的 onboarding task, got {n_app}"
        assert n_other == 1, "不应误删其他 app 的 onboarding task"