"""IM 异步 Chat prompt 用户信息构建验证：确认注入 GitCode 昵称（gitcode_nickname）。"""
import os
import sys

ROOT = os.path.join(os.path.dirname(__file__), "..")


def _read_handler_async() -> str:
    with open(os.path.join(ROOT, "src", "im", "handler_async.py"), encoding="utf-8") as f:
        return f.read()


def test_user_info_queries_gitcode_nickname():
    """unified_user_mapping 查询应取 gitcode_nickname 列"""
    src = _read_handler_async()
    assert "SELECT gitcode_user, feishu_name, gitcode_nickname FROM unified_user_mapping" in src


def test_user_info_injects_gitcode_nickname_line():
    """有 gitcode_nickname 且不等于 login 时，注入『GitCode 昵称: xxx』行"""
    src = _read_handler_async()
    assert 'user_info += f"\\nGitCode 昵称: {gitcode_nickname}"' in src


def test_user_info_keeps_feishu_name_line():
    """方案 B：保留原『GitCode nick (use this in cards)』飞书昵称行"""
    src = _read_handler_async()
    assert "GitCode nick (use this in cards)" in src
    assert "gitcode_nickname" in src


def test_user_info_no_fallback_when_nickname_empty():
    """无 gitcode_nickname 时不兜底（仅在非空且不等于 login 时注入）"""
    src = _read_handler_async()
    assert "gitcode_nickname and gitcode_nickname != gitcode_login" in src
