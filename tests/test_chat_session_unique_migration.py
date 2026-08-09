"""chat_agent_session 陈旧 UNIQUE(repo_key) 约束修复测试

背景：旧 `sessions` 表经 RENAME 保留 `repo_key TEXT UNIQUE`，而代码 DDL 意图为 per-user
（repo_key TEXT DEFAULT ''，无 UNIQUE）。陈旧约束使多用户全局 chat 配置互斥顶掉。
见 `db_setup._drop_chat_session_repo_unique`。
"""
import sqlite3

from src.api.db_setup import _drop_chat_session_repo_unique
from src.session_manager import db, db_lock


class TestDropChatSessionUnique:
    def _legacy_db(self, tmp_path):
        """模拟带陈旧 UNIQUE 约束的旧表（含数据）。"""
        p = tmp_path / "legacy.db"
        db = sqlite3.connect(str(p))
        c = db.cursor()
        c.execute("""CREATE TABLE chat_agent_session (
            id TEXT PRIMARY KEY,
            repo_key TEXT UNIQUE,
            source TEXT, repository TEXT, status TEXT,
            event_history TEXT DEFAULT '[]',
            oc_session_id TEXT DEFAULT '',
            created_at TEXT, last_active_at TEXT,
            account TEXT DEFAULT 'default', runtime TEXT DEFAULT '',
            opencode_session_id TEXT DEFAULT '', codearts_session_id TEXT DEFAULT '',
            skill TEXT DEFAULT '', im_provider TEXT DEFAULT '', im_app_id TEXT DEFAULT '',
            im_app_secret TEXT DEFAULT '', user_id INTEGER DEFAULT 0,
            skill_id INTEGER DEFAULT 0, runtime_session_id TEXT DEFAULT '',
            runtime_id INTEGER DEFAULT 0
        )""")
        c.execute("INSERT INTO chat_agent_session (id, user_id, repo_key, im_app_id, im_app_secret) "
                  "VALUES ('sess-cfg-7', 7, '', 'cli_old', 'secret_7')")
        c.execute("INSERT OR REPLACE INTO chat_agent_session (id, user_id, repo_key, im_app_id, im_app_secret) "
                  "VALUES ('sess-cfg-59', 59, '', 'cli_new', 'secret_59')")
        db.commit()
        return db, c

    def test_detects_legacy_unique(self, tmp_path):
        db, c = self._legacy_db(tmp_path)
        sql = c.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='chat_agent_session'").fetchone()[0]
        assert "repo_key text unique" in sql.lower()
        db.close()

    def test_rebuild_removes_unique_and_preserves(self, tmp_path):
        db, c = self._legacy_db(tmp_path)
        assert _drop_chat_session_repo_unique(c) is True
        db.commit()
        sql = c.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='chat_agent_session'").fetchone()[0]
        assert "repo_key TEXT UNIQUE" not in sql.lower()
        # 数据保全：唯一保留的是 REPLACE 后的 sess-cfg-59（旧表 UNIQUE 下二者互斥）
        rows = c.execute("SELECT id, user_id, im_app_id FROM chat_agent_session ORDER BY id").fetchall()
        assert rows == [("sess-cfg-59", 59, "cli_new")]
        # 约束去除后：可同时插入第二个 repo_key='' 行（多用户共存）
        c.execute("INSERT OR REPLACE INTO chat_agent_session (id, user_id, repo_key, im_app_id, im_app_secret) "
                  "VALUES ('sess-cfg-7', 7, '', 'cli_old', 'secret_7')")
        db.commit()
        rows = c.execute("SELECT id, user_id, im_app_id FROM chat_agent_session ORDER BY id").fetchall()
        assert rows == [("sess-cfg-59", 59, "cli_new"), ("sess-cfg-7", 7, "cli_old")]
        db.close()

    def test_idempotent(self, tmp_path):
        db, c = self._legacy_db(tmp_path)
        assert _drop_chat_session_repo_unique(c) is True
        db.commit()
        assert _drop_chat_session_repo_unique(c) is False
        db.close()

    def test_fresh_table_untouched(self):
        """代码 DDL 建的新表（无 UNIQUE）不应触发重建。"""
        with db_lock:
            c = db.cursor()
            assert _drop_chat_session_repo_unique(c) is False
