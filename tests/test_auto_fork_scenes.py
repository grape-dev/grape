"""回归测试：_auto_fork_official_scenes_to_repo 仅 fork default 场景。

背景：PR #41 将 auto-fork 范围从全部 7 个 official 场景缩减为仅 default。
此测试验证 SQL 查询条件 scene_id='default' 生效，即使 DB 中存在多个 official
场景，也只有 default 被 fork。
"""
import os
import sys
import sqlite3
import threading
from unittest.mock import patch, MagicMock, call

os.environ.setdefault("DEVOPS_DB_PATH", os.path.join(
    os.path.dirname(__file__), "..", "data", "test_sessions.db"))

# 预注入 mock 模块，避免导入链触发 src.session_manager 的 Python 3.10+ 语法
# 在 Python 3.10+ 环境中，这些模块已被正常导入，setdefault 不会覆盖
for _mod in ("src.config", "src.session_manager", "src.user.db",
             "src.api.routing", "src.runtime.engine_select", "src.user",
             "src.api.db_setup", "src.repo_key"):
    if _mod not in sys.modules:
        sys.modules[_mod] = MagicMock()
if hasattr(sys.modules.get("src.config"), "CONFIG_DIR"):
    pass
else:
    sys.modules.setdefault("src.config", MagicMock())
sys.modules["src.config"].CONFIG_DIR = getattr(sys.modules["src.config"], "CONFIG_DIR", "/tmp")
sys.modules["src.config"].log = getattr(sys.modules["src.config"], "log", MagicMock())

from src.api import pipeline_config  # noqa: E402


def _make_mem_db(scenes):
    """创建内存 SQLite 并插入 official 场景数据，返回 (conn, lock)。"""
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    conn.execute(
        "CREATE TABLE devops_pipeline_scene ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "scene_id TEXT, name TEXT, description TEXT, scope TEXT, "
        "repo_key TEXT, user_id INTEGER, nodes_json TEXT, onboarding TEXT, "
        "is_active INTEGER, created_at TEXT, updated_at TEXT)")
    for sid, active in scenes:
        conn.execute(
            "INSERT INTO devops_pipeline_scene "
            "(scene_id, name, description, scope, repo_key, user_id, "
            "nodes_json, onboarding, is_active, created_at, updated_at) "
            "VALUES (?,?,?, 'official', '', 0, '[]', 'repo-onboarding', ?, '', '')",
            (sid, sid, sid, active))
    conn.commit()
    return conn, threading.Lock()


class TestAutoForkOnlyDefault:
    def test_only_default_is_forked(self):
        """DB 有多个 official 场景时，仅 fork default"""
        db, lock = _make_mem_db([
            ("default", 1),
            ("cloud-native", 1),
            ("iac", 1),
            ("agent-dev", 1),
            ("skill-dev", 1),
            ("api-skill-dev", 1),
            ("blog-publish", 1),
        ])
        with patch.object(pipeline_config, "_db", return_value=(db, lock)), \
             patch.object(pipeline_config, "fork_official_scene", return_value=True) as mock_fork:
            pipeline_config._auto_fork_official_scenes_to_repo("owner/repo", 42)

        mock_fork.assert_called_once_with("default", 42, repo_key="owner/repo")

    def test_no_default_no_fork(self):
        """DB 没有 default 场景时，不 fork 任何场景"""
        db, lock = _make_mem_db([
            ("cloud-native", 1),
            ("iac", 1),
        ])
        with patch.object(pipeline_config, "_db", return_value=(db, lock)), \
             patch.object(pipeline_config, "fork_official_scene", return_value=True) as mock_fork:
            pipeline_config._auto_fork_official_scenes_to_repo("owner/repo", 42)

        mock_fork.assert_not_called()

    def test_inactive_default_not_forked(self):
        """default 场景 is_active=0 时，不 fork"""
        db, lock = _make_mem_db([
            ("default", 0),
            ("cloud-native", 1),
        ])
        with patch.object(pipeline_config, "_db", return_value=(db, lock)), \
             patch.object(pipeline_config, "fork_official_scene", return_value=True) as mock_fork:
            pipeline_config._auto_fork_official_scenes_to_repo("owner/repo", 42)

        mock_fork.assert_not_called()
