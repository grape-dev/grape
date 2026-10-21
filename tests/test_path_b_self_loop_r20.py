"""R20.1 路径 B 自循环检测测试。

测试要点：
- R20.1.1: 四条件组合判断（路径 B + actor 匹配 + event_type + 有 task 执行中）
- R20.1.2: 开发者指令豁免
- R20.1.6: 路径 A 仓库不受影响
- R20.1.7: 查询失败降级为不检测
- R20.1.8: task 执行状态查询
- R21.7.2: session 兜底窗口 60s
"""
import pytest
from unittest.mock import patch, MagicMock


class TestCheckDeveloperCommand:
    """R20.1.2: 开发者指令检测。"""

    def test_done_command(self):
        from src.api.event_pipeline import check_developer_command
        assert check_developer_command("please /done") is True

    def test_cancel_command(self):
        from src.api.event_pipeline import check_developer_command
        assert check_developer_command("/cancel this task") is True

    def test_resume_command(self):
        from src.api.event_pipeline import check_developer_command
        assert check_developer_command("/resume") is True

    def test_chinese_done(self):
        from src.api.event_pipeline import check_developer_command
        assert check_developer_command("完成任务") is True

    def test_no_command(self):
        from src.api.event_pipeline import check_developer_command
        assert check_developer_command("just a normal comment") is False

    def test_empty(self):
        from src.api.event_pipeline import check_developer_command
        assert check_developer_command("") is False
        assert check_developer_command(None) is False


class TestCheckRunningTask:
    """R20.1.8/R20.10.5: task 执行状态查询。"""

    def test_running_task_found(self):
        from src.api.event_pipeline import check_running_task
        _cursor = MagicMock()
        _cursor.fetchone.return_value = (1,)
        with patch("src.session_manager.db") as mock_db, \
             patch("src.session_manager.db_lock"):
            mock_db.cursor.return_value = _cursor
            result = check_running_task("gitcode:owner/repo", "123")
            assert result is True

    def test_no_running_task_no_session(self):
        from src.api.event_pipeline import check_running_task
        _cursor = MagicMock()
        _cursor.fetchone.return_value = None  # 无 task + 无 session
        with patch("src.session_manager.db") as mock_db, \
             patch("src.session_manager.db_lock"):
            mock_db.cursor.return_value = _cursor
            result = check_running_task("gitcode:owner/repo", "123")
            assert result is False

    def test_empty_repo_key(self):
        from src.api.event_pipeline import check_running_task
        assert check_running_task("", "123") is False

    def test_empty_issue_id_falls_back_to_session(self):
        """R21.6.5: issue_id 为空时走 session 兜底"""
        from src.api.event_pipeline import check_running_task
        with patch("src.api.event_pipeline._check_session_active", return_value=True):
            result = check_running_task("gitcode:owner/repo", "")
            assert result is True


class TestIsPathBSelfLoop:
    """R20.1.1: 路径 B 自循环检测四条件组合。"""

    def test_path_a_not_checked(self):
        """R20.1.6: 路径 A 仓库（level != "user"）不受影响"""
        from src.api.event_pipeline import _is_path_b_self_loop
        _mock_result = MagicMock()
        _mock_result.level = "repo_bot"  # 路径 A
        with patch("src.token_resolver.TokenResolver.resolve_config_level", return_value=_mock_result):
            result = _is_path_b_self_loop("gitcode:owner/repo", "someuser", "issue_comment", "123")
            assert result is False

    def test_wrong_event_type(self):
        """条件 3: event_type 不在允许列表中"""
        from src.api.event_pipeline import _is_path_b_self_loop
        _mock_result = MagicMock()
        _mock_result.level = "user"
        with patch("src.token_resolver.TokenResolver.resolve_config_level", return_value=_mock_result):
            result = _is_path_b_self_loop("gitcode:owner/repo", "hostuser", "issue_opened", "123")
            assert result is False

    def test_wrong_actor(self):
        """条件 2: actor != host_vcs_username"""
        from src.api.event_pipeline import _is_path_b_self_loop
        _mock_result = MagicMock()
        _mock_result.level = "user"
        with patch("src.token_resolver.TokenResolver.resolve_config_level", return_value=_mock_result), \
             patch("src.user.db.get_host_vcs_username", return_value="real_host_user"):
            result = _is_path_b_self_loop("gitcode:owner/repo", "someone_else", "issue_comment", "123")
            assert result is False

    def test_no_running_task(self):
        """条件 4: 没有正在执行的 task → 放行（用户主动操作）"""
        from src.api.event_pipeline import _is_path_b_self_loop
        _mock_result = MagicMock()
        _mock_result.level = "user"
        with patch("src.token_resolver.TokenResolver.resolve_config_level", return_value=_mock_result), \
             patch("src.user.db.get_host_vcs_username", return_value="hostuser"), \
             patch("src.api.event_pipeline.check_running_task", return_value=False):
            result = _is_path_b_self_loop("gitcode:owner/repo", "hostuser", "issue_comment", "123")
            assert result is False

    def test_all_four_conditions_met(self):
        """四条件全满足 → 自循环 → 跳过"""
        from src.api.event_pipeline import _is_path_b_self_loop
        _mock_result = MagicMock()
        _mock_result.level = "user"
        with patch("src.token_resolver.TokenResolver.resolve_config_level", return_value=_mock_result), \
             patch("src.user.db.get_host_vcs_username", return_value="hostuser"), \
             patch("src.api.event_pipeline.check_running_task", return_value=True):
            result = _is_path_b_self_loop("gitcode:owner/repo", "hostuser", "issue_comment", "123")
            assert result is True

    def test_host_vcs_username_empty_degrades(self):
        """R20.1.7: host_vcs_username 查询失败 → 降级为不检测"""
        from src.api.event_pipeline import _is_path_b_self_loop
        _mock_result = MagicMock()
        _mock_result.level = "user"
        with patch("src.token_resolver.TokenResolver.resolve_config_level", return_value=_mock_result), \
             patch("src.user.db.get_host_vcs_username", return_value=""):
            result = _is_path_b_self_loop("gitcode:owner/repo", "hostuser", "issue_comment", "123")
            assert result is False


class TestGetHostVcsUsername:
    """R20.1.3/R20.1.4: host_vcs_username 获取。"""

    def test_read_from_managed_repos(self):
        """R20.1.4: 优先从 managed_repos.host_vcs_username 读取"""
        from src.user.db import get_host_vcs_username
        _cursor = MagicMock()
        _cursor.fetchone.return_value = ("host_login",)
        with patch("src.user.db.db") as mock_db, \
             patch("src.user.db.db_lock", MagicMock()):
            mock_db.cursor.return_value = _cursor
            result = get_host_vcs_username("gitcode:owner/repo")
            assert result == "host_login"

    def test_fallback_to_external_identities(self):
        """R20.1.3: managed_repos 为空时 fallback 到 external_identities"""
        from src.user.db import get_host_vcs_username
        # 第一次查 managed_repos 返回空，第二次查 external_identities 返回值
        _cursor = MagicMock()
        _cursor.fetchone.side_effect = [(None,), ("ext_login",)]
        with patch("src.user.db.db") as mock_db, \
             patch("src.user.db.db_lock", MagicMock()), \
             patch("src.user.db.get_host_user_id", return_value=42):
            mock_db.cursor.return_value = _cursor
            result = get_host_vcs_username("gitcode:owner/repo")
            assert result == "ext_login"

    def test_empty_repo_key(self):
        from src.user.db import get_host_vcs_username
        assert get_host_vcs_username("") == ""
