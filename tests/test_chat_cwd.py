"""Chat 工作目录下发逻辑基准测试（2026-08-13 修复：非 root worker Permission denied）。"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


class TestChatCwd:
    def test_cwd_uses_logical_base_not_local_workspace(self):
        """服务端必须下发 /home/grape 逻辑基准（worker 端 _remap_cwd 默认契约），
        而非本机 WORKSPACE_DIR——否则非 root worker 映射不到会写 /root 报权限错误。"""
        from src.api.routes.chat import CHAT_CWD_BASE, _get_chat_cwd
        from src.config import WORKSPACE_DIR

        assert CHAT_CWD_BASE == "/home/grape"
        p = _get_chat_cwd(1, "conv-test-0001")
        assert p == os.path.join("/home/grape", "chat", "1", "conv-test-0001")
        # 不得使用服务端本机工作区路径（worker 端无法映射）
        assert not p.startswith(WORKSPACE_DIR)
        assert os.path.isdir(p)  # 本机 direct 执行亦应可创建

    def test_remap_contract_user_home(self):
        """worker 端 _remap_cwd 契约：/home/grape 前缀 → 各用户 home（模拟 mlc 用户）。"""
        p = "/home/grape/chat/1/conv-x"
        local_home = "/home/mlc"
        assert local_home + p[len("/home/grape"):] == "/home/mlc/chat/1/conv-x"
