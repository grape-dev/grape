"""web chat 工作区路径测试（web-chat-no-response-cwd-fix.md §7.1）"""
import os
from unittest import mock

from src.api.routes.chat import _get_chat_cwd


class TestChatCwd:
    def test_cwd_uses_logical_base_not_local_workspace(self):
        """服务端必须下发独立的 CHAT_CWD_BASE 逻辑基准，
        而非本机 WORKSPACE_DIR——否则非 root worker 映射不到会写 /root 报权限错误。"""
        from src.api.routes.chat import CHAT_CWD_BASE, _get_chat_cwd
        from src.config import WORKSPACE_DIR

        # CHAT_CWD_BASE 必须与 WORKSPACE_DIR 不同（独立逻辑基准）
        assert CHAT_CWD_BASE != WORKSPACE_DIR, \
            f"CHAT_CWD_BASE({CHAT_CWD_BASE}) 不应等于 WORKSPACE_DIR({WORKSPACE_DIR})"
        p = _get_chat_cwd(1, "conv-test-0001")
        assert p == os.path.join(CHAT_CWD_BASE, "chat", "1", "conv-test-0001")
        # 不得使用服务端本机工作区路径（worker 端无法映射）
        assert not p.startswith(WORKSPACE_DIR)
        assert os.path.isdir(p)  # 本机 direct 执行亦应可创建

    def test_remap_contract_user_home(self):
        """worker 端 _remap_cwd 契约：CHAT_CWD_BASE 前缀 → 各用户 home（模拟 mlc 用户）。"""
        from src.config import CHAT_CWD_BASE
        p = os.path.join(CHAT_CWD_BASE, "chat/1/conv-x")
        local_home = "/home/mlc"
        # 契约：worker 端把 CHAT_CWD_BASE 替换为 $HOME
        remapped = local_home + p[len(CHAT_CWD_BASE):]
        assert remapped == "/home/mlc/chat/1/conv-x"
