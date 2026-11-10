"""chat_session_env_extra 公共 helper 单元测试（feishu-task-chat-direct-session-token-injection-design.md）。

任务会话（真实仓库）chat 会话凭据注入：Web 路由与飞书 IM 共用同一 helper，防两组逻辑漂移。
覆盖：真仓库注入 tk_ + WORKER + 作用域正/反例；伪/空/None 仓库不注入；同会话 session_key 复用；异常兜底。
"""
import os
import sys
import hashlib
from unittest.mock import patch

SRC = os.path.join(os.path.dirname(__file__), "..")
sys.path.insert(0, SRC)
os.environ.setdefault("DEVOPS_DB_PATH", os.path.join(SRC, "data", "test_sessions.db"))

from src.api.auth_token import verify_session_token, verify_session_token_for_repo
from src.api.chat_session_env import chat_session_env_extra


def _lookup_session_key(tok: str) -> str:
    from src.session_manager import db, db_lock
    sha = hashlib.sha256(tok.encode()).hexdigest()
    with db_lock:
        row = db.cursor().execute(
            "SELECT session_key FROM session_api_tokens WHERE token_sha256=?", (sha,)).fetchone()
    return row[0] if row else ""


def _cleanup():
    from src.session_manager import db, db_lock
    with db_lock:
        c = db.cursor()
        c.execute("DELETE FROM session_api_tokens WHERE user_id=99991 OR session_key LIKE 'conv:test-conv%'")
        for (cid,) in c.execute("SELECT id FROM chat_conversations WHERE user_id=99991").fetchall():
            c.execute("DELETE FROM chat_messages WHERE conversation_id=?", (cid,))
            c.execute("DELETE FROM chat_conversations WHERE id=?", (cid,))
        db.commit()


class TestChatSessionEnvExtra:
    def setup_method(self):
        _cleanup()

    def teardown_method(self):
        _cleanup()

    def test_real_repo_injects_session_token(self):
        """真仓库 → 注入 tk_ 会话 token + GRAPE_CLI_MODE=WORKER，且作用域校验通过。"""
        env = chat_session_env_extra(99991, "test-conv", "gitcode:owner/repo")
        assert env["USER_ID"] == "99991"
        assert env["CONVERSATION_ID"] == "test-conv"
        tok = env["GRAPE_SESSION_TOKEN"]
        assert tok.startswith("tk_")
        assert env["GRAPE_CLI_MODE"] == "WORKER"
        info = verify_session_token(tok)
        assert info
        assert info["repo_key"] == "gitcode:owner/repo"
        assert info["agent_type"] == "chat"
        assert info["user_id"] == 99991
        assert verify_session_token_for_repo(tok, "gitcode:owner/repo")
        assert verify_session_token_for_repo(tok, "github:other/repo") is None

    def test_pseudo_or_empty_repo_no_token(self):
        """伪仓库 / 空 / None → 不注入 token 与 WORKER（一般对话行为不变）。"""
        for rk in ("", None, "chat:1"):
            env = chat_session_env_extra(99991, "conv-x", rk)
            assert "GRAPE_SESSION_TOKEN" not in env, rk
            assert "GRAPE_CLI_MODE" not in env, rk
            assert env["USER_ID"] == "99991"
            assert env["CONVERSATION_ID"] == "conv-x"

    def test_same_conv_reuses_same_session_key(self):
        """同会话两次调用 → 同一 session_key（conv:{conv_id}），便于清理、不重复堆积。"""
        e1 = chat_session_env_extra(99991, "test-conv", "gitcode:owner/repo")
        e2 = chat_session_env_extra(99991, "test-conv", "gitcode:owner/repo")
        assert _lookup_session_key(e1["GRAPE_SESSION_TOKEN"]) == "conv:test-conv"
        assert _lookup_session_key(e2["GRAPE_SESSION_TOKEN"]) == "conv:test-conv"

    def test_issue_token_failure_degrades(self):
        """issue_session_token 抛异常 → helper 不抛，仅缺 token 字段，其余 env 保留（chat 仍可正常回复）。"""
        with patch("src.api.auth_token.issue_session_token", side_effect=Exception("DB down")):
            env = chat_session_env_extra(99991, "test-conv", "gitcode:owner/repo")
        assert "GRAPE_SESSION_TOKEN" not in env
        assert "GRAPE_CLI_MODE" not in env
        assert env["USER_ID"] == "99991"
        assert env["CONVERSATION_ID"] == "test-conv"

class TestChatSessionEnvRepoInjection:
    """Issue #1757：chat 三条路径补齐 REPO_* / ISSUE_NUMBER / GRAPE_SERVER_URL。"""

    def setup_method(self):
        _cleanup()

    def teardown_method(self):
        _cleanup()

    def test_real_repo_injects_repo_vars_and_server_url(self):
        """真仓库 → REPO_FULL_NAME/OWNER/NAME（无前缀）+ GRAPE_SERVER_URL 非空。"""
        env = chat_session_env_extra(99991, "test-conv", "gitcode:owner/repo", runtime_id=0)
        assert env["REPO_OWNER"] == "owner"
        assert env["REPO_NAME"] == "repo"
        assert env["REPO_FULL_NAME"] == "owner/repo"
        assert env["GRAPE_SERVER_URL"].startswith("http")

    def test_github_prefix_stripped(self):
        """github: 前缀同样剥掉，口径与 build_task_env 一致。"""
        env = chat_session_env_extra(99991, "test-conv", "github:o/r", runtime_id=0)
        assert env["REPO_FULL_NAME"] == "o/r"
        assert env["REPO_OWNER"] == "o"
        assert env["REPO_NAME"] == "r"

    def test_pseudo_or_empty_repo_no_repo_keys(self):
        """伪仓库/空/None → REPO_* 与 ISSUE_NUMBER 键不出现，其余保留。"""
        for rk in ("", None, "chat:1"):
            env = chat_session_env_extra(99991, "conv-x", rk, issue_number=1735)
            for key in ("REPO_FULL_NAME", "REPO_OWNER", "REPO_NAME", "ISSUE_NUMBER"):
                assert key not in env, (rk, key)
            assert env["USER_ID"] == "99991"
            assert env["CONVERSATION_ID"] == "conv-x"
            assert env["GRAPE_SERVER_URL"].startswith("http")

    def test_issue_number_numeric_task_conversation(self):
        """task 会话纯数字 Issue 编号 → ISSUE_NUMBER 注入（含 # 前缀剥离）。"""
        from src.session_manager import create_conversation
        cid = create_conversation(99991, "gitcode:owner/repo", title="Issue #1735",
                                  conv_type="task", issue_number=1735)
        env = chat_session_env_extra(99991, cid, "gitcode:owner/repo", runtime_id=0)
        assert env["ISSUE_NUMBER"] == "1735"
        env_explicit = chat_session_env_extra(99991, "test-conv", "gitcode:owner/repo",
                                              issue_number="#1735")
        assert env_explicit["ISSUE_NUMBER"] == "1735"

    def test_issue_number_non_numeric_not_injected(self):
        """非数字 Issue 编号 → 不注入 ISSUE_NUMBER（#528 口径）。"""
        env = chat_session_env_extra(99991, "", "gitcode:owner/repo",
                                     issue_number="3f40dd97-ab9a-4b1c")
        assert "ISSUE_NUMBER" not in env
        assert env["REPO_OWNER"] == "owner"

    def test_issue_number_non_task_conversation_not_injected(self):
        """非 task 会话 → 不注入 ISSUE_NUMBER。"""
        from src.session_manager import create_conversation
        cid = create_conversation(99991, "gitcode:owner/repo", title="一般对话")
        env = chat_session_env_extra(99991, cid, "gitcode:owner/repo", runtime_id=0)
        assert "ISSUE_NUMBER" not in env

    def test_resolve_server_url_failure_degrades(self):
        """业务基址解析抛异常 → helper 不抛，仅缺 GRAPE_SERVER_URL，其余键仍在。"""
        with patch("src.prompt_builder._resolve_business_server_url", side_effect=Exception("x")):
            env = chat_session_env_extra(99991, "test-conv", "gitcode:owner/repo", runtime_id=0)
        assert "GRAPE_SERVER_URL" not in env
        assert env["REPO_OWNER"] == "owner"
        assert env["REPO_NAME"] == "repo"

    def test_skill_declared_keys_present(self):
        """输出键集合 ⊇ chat SKILL 声明的五个变量。"""
        from src.session_manager import create_conversation
        cid = create_conversation(99991, "gitcode:owner/repo", title="Issue #1735",
                                  conv_type="task", issue_number=1735)
        env = chat_session_env_extra(99991, cid, "gitcode:owner/repo", runtime_id=0)
        declared = {"REPO_FULL_NAME", "REPO_OWNER", "REPO_NAME", "ISSUE_NUMBER", "GRAPE_SERVER_URL"}
        assert declared.issubset(set(env))

    def test_rendered_grape_command_has_no_empty_segments(self):
        """注入后的 env 渲染技能命令 → 无空参数/空路径段。"""
        from src.session_manager import create_conversation
        cid = create_conversation(99991, "gitcode:owner/repo", title="Issue #1735",
                                  conv_type="task", issue_number=1735)
        env = chat_session_env_extra(99991, cid, "gitcode:owner/repo", runtime_id=0)
        cmd = ('grape git-repo issues get --owner "%s" --repo "%s" --number "%s"'
               % (env["REPO_OWNER"], env["REPO_NAME"], env["ISSUE_NUMBER"]))
        assert '--owner ""' not in cmd
        assert '--repo ""' not in cmd
        assert '--number ""' not in cmd
        assert "//" not in cmd
