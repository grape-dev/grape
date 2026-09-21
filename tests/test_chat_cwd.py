"""web chat 工作区路径测试（web-chat-no-response-cwd-fix.md §7.1）"""
import os
from unittest import mock

from src.api.routes.chat import _get_chat_cwd


def test_get_chat_cwd_owner_repo_two_levels(tmp_path):
    with mock.patch("src.api.routes.chat.WORKSPACE_DIR", str(tmp_path)), \
         mock.patch("src.config.WORKSPACE_DIR", str(tmp_path)):
        p = _get_chat_cwd(user_id=7, repo_key="gitcode:Jason7602/hello-html2",
                          conv_id="518a373d-1c77-4457-aac0-274253fdef7e")
    assert p == str(tmp_path / "gitcode" / "Jason7602" / "hello-html2" / "chat" / "7" / "518a373d-1c77-4457-aac0-274253fdef7e")
    assert os.path.isdir(p)


def test_get_chat_cwd_noprefix_repo_key(tmp_path):
    # 无 provider 前缀也能解析（默认 gitcode）
    with mock.patch("src.api.routes.chat.WORKSPACE_DIR", str(tmp_path)), \
         mock.patch("src.config.WORKSPACE_DIR", str(tmp_path)):
        p = _get_chat_cwd(user_id=1, repo_key="a/b", conv_id="c1")
    assert p == str(tmp_path / "gitcode" / "a" / "b" / "chat" / "1" / "c1")


def test_get_chat_cwd_empty_owner_repo_fallback(tmp_path):
    # 仓库解析为空时路径仍结构化（owner/repo 为空，provider 默认 gitcode）
    with mock.patch("src.api.routes.chat.WORKSPACE_DIR", str(tmp_path)), \
         mock.patch("src.config.WORKSPACE_DIR", str(tmp_path)):
        p = _get_chat_cwd(user_id=1, repo_key="", conv_id="c1")
    assert "gitcode" in p  # provider 默认 gitcode
    assert p.endswith(os.path.join("chat", "1", "c1"))
