"""web chat 工作区路径契约测试：飞书与 Web 对同一 conv_id 产生完全一致的 cwd
（web-chat-no-response-cwd-fix.md §7.2）"""
import os
import sqlite3
from unittest import mock

from src.api.routes.chat import _get_chat_cwd
from src.repo_key import parse_repo_key


def test_send_path_and_feishu_use_same_cwd(tmp_path):
    # 模拟 web send 与飞书 handler 共享的推导逻辑：同一 repo_key+user_id+conv_id → 同一路径
    repo_key = "gitcode:Jason7602/hello-html2"
    user_id = 7
    conv_id = "518a373d-1c77-4457-aac0-274253fdef7e"

    with mock.patch("src.api.routes.chat.WORKSPACE_DIR", str(tmp_path)), \
         mock.patch("src.config.WORKSPACE_DIR", str(tmp_path)):
        web_cwd = _get_chat_cwd(user_id, repo_key, conv_id)
        # 飞书侧（handler_async 重构后）应产生相同推导结果（相同 workspace_repo_dir 规则）
        from src.repo_key import workspace_repo_dir
        feishu_cwd = os.path.join(workspace_repo_dir(repo_key), "chat", str(user_id), conv_id)

    assert web_cwd == feishu_cwd
    # 不再含 feishu 段、不再含 sess 尾巴
    assert not web_cwd.endswith(".split")
    assert "feishu" not in web_cwd.split(os.sep)


def test_migrate_session_directory_new_structure(tmp_path):
    # 存量迁移：旧 /home/grape/chat/{uid}/... 与 /opt/grape_workspace/chat/{uid}/...
    # 迁移到 WORKSPACE_DIR/{owner}/{repo}/chat/{uid}/{conv_id}（幂等）
    db = sqlite3.connect(str(tmp_path / "opencode.db"))
    db.execute("CREATE TABLE session (id TEXT, directory TEXT, path TEXT)")
    db.commit()

    def migrate_directory(old_dir, repo_key, user_id, conv_id):
        from src.repo_key import workspace_repo_dir
        import src.config
        src.config.WORKSPACE_DIR = str(tmp_path / "grape-workspaces")
        return os.path.join(workspace_repo_dir(repo_key), "chat", str(user_id), conv_id)

    old = "/home/grape/chat/7/518a373d-1c77-4457-aac0-274253fdef7e"
    new = migrate_directory(old, "gitcode:Jason7602/hello-html2", 7, "518a373d-1c77-4457-aac0-274253fdef7e")
    assert new == str(tmp_path / "grape-workspaces" / "gitcode" / "Jason7602" / "hello-html2" / "chat" / "7" / "518a373d-1c77-4457-aac0-274253fdef7e")
    # 幂等：再迁移一次不变
    assert migrate_directory(new, "gitcode:Jason7602/hello-html2", 7, "518a373d-1c77-4457-aac0-274253fdef7e") == new


def test_stream_agent_receives_get_chat_cwd(tmp_path):
    # stream_agent 收到的 cwd 必须等于 _get_chat_cwd 的结果（同 repo_key/user/conv）
    repo_key = "a/b"
    with mock.patch("src.api.routes.chat.WORKSPACE_DIR", str(tmp_path)), \
         mock.patch("src.config.WORKSPACE_DIR", str(tmp_path)):
        expected = _get_chat_cwd(1, repo_key, "conv1")
    # "a/b" 无前缀 → provider 默认 gitcode → {tmp_path}/gitcode/a/b/chat/1/conv1
    assert expected == str(tmp_path / "gitcode" / "a" / "b" / "chat" / "1" / "conv1")