"""web chat 工作区路径测试（web-chat-no-response-cwd-fix.md §7.1）"""
import os
from unittest import mock

from src.api.routes.chat import _get_chat_cwd


def test_get_chat_cwd_owner_repo_two_levels(tmp_path):
    with mock.patch("src.api.routes.chat.WORKSPACE_DIR", str(tmp_path)):
        p = _get_chat_cwd(user_id=7, repo_key="gitcode:Jason7602/hello-html2",
                          conv_id="518a373d-1c77-4457-aac0-274253fdef7e")
    assert p == str(tmp_path / "Jason7602" / "hello-html2" / "chat" / "7" / "518a373d-1c77-4457-aac0-274253fdef7e")
    assert os.path.isdir(p)


def test_get_chat_cwd_noprefix_repo_key(tmp_path):
    # 无 provider 前缀也能解析
    with mock.patch("src.api.routes.chat.WORKSPACE_DIR", str(tmp_path)):
        p = _get_chat_cwd(user_id=1, repo_key="a/b", conv_id="c1")
    assert p == str(tmp_path / "a" / "b" / "chat" / "1" / "c1")


def test_get_chat_cwd_empty_owner_repo_fallback(tmp_path):
    # 仓库解析为空时用 __none__ 占位（防御，正常勿触发）
    with mock.patch("src.api.routes.chat.WORKSPACE_DIR", str(tmp_path)):
        p = _get_chat_cwd(user_id=1, repo_key="", conv_id="c1")
    parts = p.split(os.sep)
    assert "__none__" in parts  # owner/repo 占位存在，路径仍结构化
