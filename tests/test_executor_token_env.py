"""stream_agent 的 GitCode token 环境注入兜底逻辑测试。

USER_GITCODE_TOKEN 为空时会导致 chat agent 调 GitCode API 401（创建/查询 Issue 失败）。
修复：用户 token 为空 → 依次兜底 用户登录绑定的 token（user_accounts）→ 仓库 api token
（bot_token，worker 侧 repo_gitcode_token / 事件流水线仓库 bot token）。
"""
import os
import sys

ROOT = os.path.join(os.path.dirname(__file__), "..")
sys.path.insert(0, ROOT)
os.environ.setdefault("DEVOPS_DB_PATH", os.path.join(ROOT, "data", "test_sessions.db"))

from unittest import mock


class _FakeStd:
    def readline(self):
        return b""

    def read(self):
        return b""

    def settimeout(self, *a):
        pass


class _FakePopen:
    """mock subprocess.Popen：记录 env，返回已结束的伪进程"""

    def __init__(self):
        self.captured = {}

    def __call__(self, *args, **kwargs):
        self.captured = dict(kwargs.get("env") or {})
        return _FakeProcHandle(self.captured)


class _FakeProcHandle:
    returncode = 0

    def __init__(self, env):
        self.stdout = _FakeStd()
        self.stderr = _FakeStd()
        self.stdin = None

    def poll(self):
        return 0

    def wait(self, *a, **kw):
        return 0

    def terminate(self):
        pass

    def kill(self):
        pass


class _FakeBackend:
    name = "fake"
    binary = "true"
    prompt_via_stdin = False

    def get_env(self, account="", user_id=0):
        return {}

    def build_args(self, prompt, session_id):
        return []

    def parse_output(self, line):
        return None


def _run_stream(user_token="", bot_token="", user_id=0, db_user_token=""):
    # 经 src.runtime.executor 导入可绕开 agent→runtime 循环导入（与 tests 同款用法）
    from src.runtime.executor import stream_agent

    fake_popen = _FakePopen()
    with mock.patch("src.agent.executor.get_backend", return_value=_FakeBackend()), \
         mock.patch("src.agent.executor.subprocess.Popen", fake_popen), \
         mock.patch("src.agent.executor._track_proc", lambda *a, **kw: None), \
         mock.patch("src.agent.executor._untrack_proc", lambda *a, **kw: None), \
         mock.patch("src.user.db.get_credentials", return_value=[]), \
         mock.patch("src.user.db.get_user_gitcode_token", return_value=db_user_token):
        list(stream_agent(
            prompt="hi", cwd="/tmp", session_id="", event_type="chat",
            backend_name="fake", account="u", user_id=user_id,
            user_token=user_token, bot_token=bot_token, max_exec_seconds=60,
        ))
    return fake_popen.captured


def test_user_token_wins_over_bot_token():
    env = _run_stream(user_token="USR", bot_token="BOT")
    assert env["USER_GITCODE_TOKEN"] == "USR"
    assert env["BOT_GITCODE_TOKEN"] == "BOT"


def test_bot_token_fallback_when_user_token_empty():
    """用户 token 为空 → USER_GITCODE_TOKEN 用仓库 api token（bot_token）兜底，避免 401"""
    env = _run_stream(user_token="", bot_token="BOT1", user_id=0)
    assert env["USER_GITCODE_TOKEN"] == "BOT1"
    assert env["BOT_GITCODE_TOKEN"] == "BOT1"


def test_db_user_token_fallback():
    """user_token 传空但用户登录时绑定过 token → 入库查询兜底"""
    env = _run_stream(user_token="", bot_token="", user_id=3, db_user_token="DBUSR")
    assert env["USER_GITCODE_TOKEN"] == "DBUSR"


def test_db_empty_then_bot_fallback():
    env = _run_stream(user_token="", bot_token="BOT1", user_id=3, db_user_token="")
    assert env["USER_GITCODE_TOKEN"] == "BOT1"


def test_no_token_no_injection():
    env = _run_stream(user_token="", bot_token="", user_id=3, db_user_token="")
    assert "USER_GITCODE_TOKEN" not in env