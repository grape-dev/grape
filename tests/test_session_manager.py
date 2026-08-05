"""Tests for session_manager module."""
import pytest
import os
import tempfile
import json
import importlib
import sys

# Path to import src
SRC_DIR = os.path.join(os.path.dirname(__file__), "..", "src")


def _import_session_manager(tmp_path):
    """Import session_manager with a temporary DB path."""
    db_path = os.path.join(tmp_path, "test_sessions.db")
    # Clear module cache if already imported
    if "src.session_manager" in sys.modules:
        del sys.modules["src.session_manager"]
    if "session_manager" in sys.modules:
        del sys.modules["session_manager"]
    # Ensure src is in path
    if SRC_DIR not in sys.path:
        sys.path.insert(0, SRC_DIR)
    # Override _db_path before import
    import src.session_manager as sm
    sm._db_path = db_path
    # Reconnect to the new path
    import sqlite3
    sm.db = sqlite3.connect(db_path, check_same_thread=False)
    sm.db.execute("""
        CREATE TABLE IF NOT EXISTS chat_agent_session (
            id TEXT PRIMARY KEY,
            user_id INTEGER DEFAULT 0,
            repo_key TEXT DEFAULT '',
            source TEXT, repository TEXT, status TEXT,
            backend TEXT DEFAULT 'opencode',
            skill TEXT DEFAULT '',
            im_provider TEXT DEFAULT '',
            im_app_id TEXT DEFAULT '',
            im_app_secret TEXT DEFAULT '',
            event_history TEXT DEFAULT '[]',
            backend_session_id TEXT DEFAULT '',
            created_at TEXT, last_active_at TEXT
        )
    """)
    sm.db.execute(sm.REPO_IM_GROUP_BINDING_TABLE_SQL)
    sm.db.execute(sm.UNIFIED_USER_MAPPING_TABLE_SQL)
    try:
        sm.db.execute("ALTER TABLE repo_im_group_binding ADD COLUMN auto_impl_enabled INTEGER DEFAULT 0")
    except:
        pass
    try:
        sm.db.execute("ALTER TABLE repo_im_group_binding ADD COLUMN notify_enabled INTEGER DEFAULT 0")
    except:
        pass
    sm.db.commit()
    return sm


class TestGetOrCreateSession:
    def test_create_new(self, tmp_path):
        sm = _import_session_manager(tmp_path)
        sid, history, oc_sid, _, is_new = sm.get_or_create_session(
            "gitcode:owner/repo", "gitcode", "owner/repo"
        )
        assert sid.startswith("sess-")
        assert history == []
        assert oc_sid == ""
        assert is_new is True

    def test_return_existing(self, tmp_path):
        sm = _import_session_manager(tmp_path)
        sid1, _, _, _, _ = sm.get_or_create_session(
            "gitcode:owner/repo", "gitcode", "owner/repo"
        )
        sid2, hist2, oc2, _, is_new2 = sm.get_or_create_session(
            "gitcode:owner/repo", "gitcode", "owner/repo"
        )
        assert sid1 == sid2
        assert is_new2 is False

    def test_multiple_repos_independent(self, tmp_path):
        sm = _import_session_manager(tmp_path)
        sid1, _, _, _, _ = sm.get_or_create_session(
            "gitcode:repo1", "gitcode", "repo1"
        )
        sid2, _, _, _, _ = sm.get_or_create_session(
            "gitcode:repo2", "gitcode", "repo2"
        )
        assert sid1 != sid2


class TestUpdateSession:
    def test_update_history_and_status(self, tmp_path):
        sm = _import_session_manager(tmp_path)
        sid, history, _, _, _ = sm.get_or_create_session(
            "gitcode:owner/repo", "gitcode", "owner/repo"
        )
        history.append({"type": "test", "success": True, "time": "2026-01-01"})
        sm.update_session(sid, history, "idle")
        # Re-fetch to verify
        _, hist2, _, _, _ = sm.get_or_create_session(
            "gitcode:owner/repo", "gitcode", "owner/repo"
        )
        assert len(hist2) == 1
        assert hist2[0]["type"] == "test"

    def test_update_oc_session_id(self, tmp_path):
        sm = _import_session_manager(tmp_path)
        sid, _, _, _, _ = sm.get_or_create_session(
            "gitcode:owner/repo", "gitcode", "owner/repo"
        )
        sm.update_session(sid, [], "running", "session_abc123")
        _, _, oc_sid, _, _ = sm.get_or_create_session(
            "gitcode:owner/repo", "gitcode", "owner/repo"
        )
        assert oc_sid == "session_abc123"

    def test_status_failed_then_idle(self, tmp_path):
        sm = _import_session_manager(tmp_path)
        sid, _, _, _, _ = sm.get_or_create_session(
            "gitcode:owner/repo", "gitcode", "owner/repo"
        )
        sm.update_session(sid, [], "failed")
        # Next get should reset status to idle
        _, _, _, _, is_new = sm.get_or_create_session(
            "gitcode:owner/repo", "gitcode", "owner/repo"
        )
        assert is_new is False


class TestFeishuUsers:
    def test_set_and_get_open_id(self, tmp_path):
        sm = _import_session_manager(tmp_path)
        sm.set_feishu_open_id("gitcode:owner/repo", "user1", "ou_12345", "张三")
        oid = sm.get_feishu_open_id("gitcode:owner/repo", "user1")
        assert oid == "ou_12345"

    def test_get_non_existent(self, tmp_path):
        sm = _import_session_manager(tmp_path)
        oid = sm.get_feishu_open_id("gitcode:other/repo", "nobody")
        assert oid is None

    def test_get_mapping_all(self, tmp_path):
        sm = _import_session_manager(tmp_path)
        sm.set_feishu_open_id("gitcode:owner/repo", "user1", "ou_1", "张三")
        sm.set_feishu_open_id("gitcode:owner/repo", "user2", "ou_2", "李四")
        mapping = sm.get_feishu_mapping("gitcode:owner/repo")
        assert len(mapping) == 2
        assert mapping["user1"]["open_id"] == "ou_1"
        assert mapping["user2"]["name"] == "李四"

    def test_mapping_repo_isolation(self, tmp_path):
        """用户映射按 gitcode_user 全局关联（非按 repo 隔离），不同 repo 共享同一用户映射"""
        sm = _import_session_manager(tmp_path)
        sm.set_feishu_open_id("gitcode:repoA", "user1", "ou_1", "张三")
        # 全局映射：repoB 也能看到 repoA 中登记的同一用户
        mapping_b = sm.get_feishu_mapping("gitcode:repoB")
        assert "user1" in mapping_b
        assert mapping_b["user1"]["open_id"] == "ou_1"

    def test_update_existing_mapping(self, tmp_path):
        sm = _import_session_manager(tmp_path)
        sm.set_feishu_open_id("gitcode:owner/repo", "user1", "ou_old", "旧名")
        sm.set_feishu_open_id("gitcode:owner/repo", "user1", "ou_new", "新名")
        oid = sm.get_feishu_open_id("gitcode:owner/repo", "user1")
        assert oid == "ou_new"

    def test_group_name_map(self, tmp_path):
        sm = _import_session_manager(tmp_path)
        sm.set_feishu_open_id("gitcode:owner/repo", "user1", "ou_1", "张三")
        sm.set_feishu_open_id("gitcode:owner/repo", "user2", "ou_2", "")
        name_map = sm.get_feishu_group_name_map("gitcode:owner/repo")
        assert "张三" in name_map
        assert name_map["张三"] == "ou_1"
        assert "" not in name_map

    def test_gitcode_nickname_stored(self, tmp_path):
        """set_feishu_open_id 传入 gitcode_nickname 后，get_feishu_mapping 应返回"""
        sm = _import_session_manager(tmp_path)
        sm.set_feishu_open_id("gitcode:owner/repo", "user1", "ou_1", "张三", gitcode_nickname="廖某")
        mapping = sm.get_feishu_mapping("gitcode:owner/repo")
        assert mapping["user1"]["gitcode_nickname"] == "廖某"

    def test_gitcode_nickname_refreshed_on_update(self, tmp_path):
        """已有映射时，更新会同步刷新 gitcode_nickname / feishu_open_id / feishu_name"""
        sm = _import_session_manager(tmp_path)
        sm.set_feishu_open_id("gitcode:owner/repo", "user1", "ou_old", "旧名", gitcode_nickname="旧昵称")
        sm.set_feishu_open_id("gitcode:owner/repo", "user1", "ou_new", "新名", gitcode_nickname="新昵称")
        mapping = sm.get_feishu_mapping("gitcode:owner/repo")
        assert mapping["user1"]["open_id"] == "ou_new"
        assert mapping["user1"]["name"] == "新名"
        assert mapping["user1"]["gitcode_nickname"] == "新昵称"


class TestAutoImplEnabled:
    def test_default_is_false(self, tmp_path):
        sm = _import_session_manager(tmp_path)
        # First create a binding
        with sm.db_lock:
            sm.db.execute(
                "INSERT OR REPLACE INTO repo_im_group_binding (im_chat_id, repo_key, bind_by) VALUES (?,?,?)",
                ("chat_1", "gitcode:owner/repo", "tester")
            )
            sm.db.commit()
        enabled = sm.get_auto_impl_enabled("gitcode:owner/repo")
        assert enabled is False

    def test_set_and_get(self, tmp_path):
        sm = _import_session_manager(tmp_path)
        with sm.db_lock:
            sm.db.execute(
                "INSERT OR REPLACE INTO repo_im_group_binding (im_chat_id, repo_key) VALUES (?,?)",
                ("chat_1", "gitcode:owner/repo")
            )
            sm.db.commit()
        sm.set_auto_impl_enabled("gitcode:owner/repo", True)
        assert sm.get_auto_impl_enabled("gitcode:owner/repo") is True
        sm.set_auto_impl_enabled("gitcode:owner/repo", False)
        assert sm.get_auto_impl_enabled("gitcode:owner/repo") is False


class TestNotifyEnabled:
    def test_default_is_false(self, tmp_path):
        sm = _import_session_manager(tmp_path)
        with sm.db_lock:
            sm.db.execute(
                "INSERT OR REPLACE INTO repo_im_group_binding (im_chat_id, repo_key) VALUES (?,?)",
                ("chat_1", "gitcode:owner/repo")
            )
            sm.db.commit()
        enabled = sm.get_notify_enabled("gitcode:owner/repo")
        assert enabled is False

    def test_set_and_get(self, tmp_path):
        sm = _import_session_manager(tmp_path)
        with sm.db_lock:
            sm.db.execute(
                "INSERT OR REPLACE INTO repo_im_group_binding (im_chat_id, repo_key) VALUES (?,?)",
                ("chat_1", "gitcode:owner/repo")
            )
            sm.db.commit()
        sm.set_notify_enabled("gitcode:owner/repo", True)
        assert sm.get_notify_enabled("gitcode:owner/repo") is True
        sm.set_notify_enabled("gitcode:owner/repo", False)
        assert sm.get_notify_enabled("gitcode:owner/repo") is False


class TestConcurrency:
    def test_db_lock_prevents_race(self, tmp_path):
        sm = _import_session_manager(tmp_path)
        import threading
        results = []
        def create_session(repo):
            sid, _, _, _, _ = sm.get_or_create_session(
                f"gitcode:{repo}", "gitcode", repo
            )
            results.append(sid)
        threads = [
            threading.Thread(target=create_session, args=(f"repo{i}",))
            for i in range(5)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert len(set(results)) == 5


class TestDumpHistoryToFile:
    def _setup(self, tmp_path):
        import src.config
        src.config.WORKSPACE_DIR = str(tmp_path)
        return _import_session_manager(tmp_path)

    def test_basic_dump_default_account(self, tmp_path):
        sm = self._setup(tmp_path)
        history = [{"type": "chat", "text": "hello"}]
        sm.dump_history_to_file("gitcode:owner/repo", history)
        fp = tmp_path / "history" / "default" / "owner_repo" / "session_history.json"
        assert fp.exists()
        data = json.loads(fp.read_text("utf-8"))
        assert len(data) == 1
        assert data[0]["type"] == "chat"

    def test_account_specific_dir(self, tmp_path):
        sm = self._setup(tmp_path)
        sm.dump_history_to_file("gitcode:owner/repo", [{"type": "chat"}], account="customer-a")
        fp = tmp_path / "history" / "customer-a" / "owner_repo" / "session_history.json"
        assert fp.exists()

    def test_oc_session_id_removed(self, tmp_path):
        sm = self._setup(tmp_path)
        sm.dump_history_to_file("gitcode:owner/repo", [{"type": "chat", "oc_session_id": "ses_secret"}])
        fp = tmp_path / "history" / "default" / "owner_repo" / "session_history.json"
        data = json.loads(fp.read_text("utf-8"))
        assert "oc_session_id" not in data[0]

    def test_rotation_exceeds_10(self, tmp_path):
        sm = self._setup(tmp_path)
        history = [{"type": "chat", "n": i} for i in range(25)]
        sm.dump_history_to_file("gitcode:owner/repo", history)
        base = tmp_path / "history" / "default" / "owner_repo"
        assert (base / "session_history.json").exists()
        assert (base / "session_history_1.json").exists()
        assert (base / "session_history_2.json").exists()
        data0 = json.loads((base / "session_history.json").read_text("utf-8"))
        assert len(data0) == 10
        # session_history.json contains the most recent 10 (indices 15-24)
        assert data0[0]["n"] == 15

    def test_rotation_cleanup_stale_files(self, tmp_path):
        sm = self._setup(tmp_path)
        base = tmp_path / "history" / "default" / "owner_repo"
        os.makedirs(base, exist_ok=True)
        (base / "session_history_5.json").write_text("[]", encoding="utf-8")
        sm.dump_history_to_file("gitcode:owner/repo", [{"type": "chat"}] * 3)
        assert not (base / "session_history_5.json").exists()

    def test_get_history_file_list(self, tmp_path):
        sm = self._setup(tmp_path)
        sm.dump_history_to_file("gitcode:owner/repo", [{"type": "chat"}] * 15)
        files = sm.get_history_file_list("gitcode:owner/repo")
        assert len(files) == 2
        assert all(f.endswith(".json") for f in files)

    def test_get_history_file_list_no_files(self, tmp_path):
        sm = self._setup(tmp_path)
        files = sm.get_history_file_list("gitcode:nonexistent")
        assert files == []
