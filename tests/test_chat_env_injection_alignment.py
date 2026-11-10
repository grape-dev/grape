"""Issue #1757：chat env 注入对齐（静态收敛 + 执行器兜底）回归测试。

覆盖：
1. src/api/routes/chat.py 不再出现裸 `env_extra={"USER_ID"...}` 组装，且 chat 调用点统一走 helper（>=8）。
2. `_assemble_env(kind="chat")` 仅在缺失时兜底补齐 REPO_* / GRAPE_SERVER_URL，显式值优先不覆盖。
3. `_assemble_env` 的 devops 分支零回归（不注入 REPO_* / GRAPE_SERVER_URL）。
"""
import inspect
import os
import sys

SRC = os.path.join(os.path.dirname(__file__), "..")
sys.path.insert(0, SRC)
os.environ.setdefault("DEVOPS_DB_PATH", os.path.join(SRC, "data", "test_sessions.db"))

import src.runtime.agent  # noqa: F401,E402  先入 runtime 打破循环导入
from src.agent.executor import _assemble_env  # noqa: E402


def _read(rel_path: str) -> str:
    with open(os.path.join(SRC, rel_path), encoding="utf-8") as f:
        return f.read()


class TestChatEnvSingleHelperPath:
    def test_no_naked_env_extra_dict(self):
        """chat.py 中裸 env_extra={"USER_ID"...} 必须 0 命中。"""
        src = _read("src/api/routes/chat.py")
        assert 'env_extra={"USER_ID"' not in src

    def test_all_chat_sites_use_helper(self):
        """chat.py helper 调用点 >= 8（3 处既有 + 5 处裸 dict 收敛）。"""
        src = _read("src/api/routes/chat.py")
        assert src.count("chat_session_env_extra(") >= 8

    def test_helper_call_sites_pass_runtime_and_issue(self):
        """helper 调用均补齐 runtime_id / issue_number 实参。"""
        src = _read("src/api/routes/chat.py")
        call_sites = src.split("chat_session_env_extra(")[1:]
        assert len(call_sites) >= 8
        for tail in call_sites:
            block = tail[:300]
            assert "runtime_id=" in block
            assert "issue_number=" in block


class TestAssembleEnvChatFallback:
    def test_chat_fallback_fills_repo_vars_and_server_url(self):
        """env_extra 缺失 REPO_* → _assemble_env(kind="chat") 用 repo_key 补齐。"""
        env = {}
        _assemble_env(env, kind="chat", repo_key="gitcode:o/r")
        assert env["REPO_FULL_NAME"] == "o/r"
        assert env["REPO_OWNER"] == "o"
        assert env["REPO_NAME"] == "r"
        assert env["GRAPE_SERVER_URL"].startswith("http://127.0.0.1:")

    def test_chat_fallback_does_not_override_explicit_repo(self):
        """显式 REPO_OWNER → 不被兜底覆盖（env_extra 优先）。"""
        env = {"REPO_OWNER": "explicit"}
        _assemble_env(env, kind="chat", repo_key="gitcode:o/r")
        assert env["REPO_OWNER"] == "explicit"

    def test_chat_fallback_does_not_override_explicit_url(self):
        """显式 GRAPE_SERVER_URL（远端 worker 传入）→ 不被本地兜底覆盖。"""
        env = {"GRAPE_SERVER_URL": "https://worker.example"}
        _assemble_env(env, kind="chat", repo_key="gitcode:o/r")
        assert env["GRAPE_SERVER_URL"] == "https://worker.example"

    def test_chat_pseudo_repo_no_repo_fallback(self):
        """伪仓库 → 不兜底 REPO_*，但仍补 GRAPE_SERVER_URL。"""
        env = {}
        _assemble_env(env, kind="chat", repo_key="chat:123")
        assert "REPO_OWNER" not in env
        assert "REPO_FULL_NAME" not in env
        assert env["GRAPE_SERVER_URL"].startswith("http://127.0.0.1:")


class TestAssembleEnvDevopsUnchanged:
    def test_devops_branch_no_chat_env_added(self):
        """devops 分支不注入 REPO_* / GRAPE_SERVER_URL（零回归）。"""
        env = {}
        _assemble_env(env, kind="devops", repo_key="gitcode:o/r")
        assert set(env) == {"VCS_API_BASE", "VCS_NAME"}
        assert "REPO_OWNER" not in env
        assert "GRAPE_SERVER_URL" not in env

    def test_chat_fallback_confined_to_chat_branch(self):
        """兜底代码只存在于 kind == "chat" 分支内（源码顺序断言）。"""
        src = inspect.getsource(_assemble_env)
        chat_idx = src.index('if kind == "chat":')
        devops_idx = src.index("elif bot_token:")
        fallback_idx = src.index("chat env 兜底")
        assert chat_idx < fallback_idx < devops_idx