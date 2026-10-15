"""stream_agent 的 VCS 平台 token 注入消除测试（P2：vcs-env-token-boundary-audit-design.md）。

P2 语义：`BOT_GITCODE_TOKEN / BOT_GITHUB_TOKEN / USER_GITCODE_TOKEN / USER_GITHUB_TOKEN / BOT_TOKEN`
一律不再注入 agent env——平台长期凭据只在服务端进程内流转，agent 侧统一经
`grape git/git-repo/issues`（携带 GRAPE_SESSION_TOKEN=tk_）转发。
机制元数据（VCS_API_BASE / VCS_NAME）仍注入，供技能判断平台。
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


_PLATFORM_TOKEN_ENVS = (
    "BOT_GITCODE_TOKEN", "BOT_GITHUB_TOKEN",
    "USER_GITCODE_TOKEN", "USER_GITHUB_TOKEN", "BOT_TOKEN",
)


def _assert_no_platform_tokens(env):
    for k in _PLATFORM_TOKEN_ENVS:
        assert k not in env, f"P2: {k} must NOT be injected into agent env"


def _run_stream(user_token="", bot_token="", user_id=0, db_user_token="", event_type="chat",
                repo_key="gitcode:owner/repo"):
    # 经 src.runtime.executor 导入可绕开 agent→runtime 循环导入（与 tests 同款用法）
    from src.runtime.executor import stream_agent

    fake_popen = _FakePopen()
    with mock.patch("src.agent.executor.get_backend", return_value=_FakeBackend()), \
         mock.patch("src.agent.executor.subprocess.Popen", fake_popen), \
         mock.patch("src.agent.executor.shutil.which", return_value="/usr/bin/fake"), \
         mock.patch("src.agent.executor._track_proc", lambda *a, **kw: None), \
         mock.patch("src.agent.executor._untrack_proc", lambda *a, **kw: None), \
         mock.patch("src.user.db.get_credentials", return_value=[]), \
         mock.patch("src.user.db.get_user_gitcode_token", return_value=db_user_token):
        list(stream_agent(
            prompt="hi", cwd="/tmp", session_id="", event_type=event_type,
            backend_name="fake", account="u", user_id=user_id,
            user_token=user_token, bot_token=bot_token, max_exec_seconds=60,
            repo_key=repo_key,
        ))
    return fake_popen.captured


def test_no_platform_tokens_chat_with_user_token():
    """chat + user_token：P2 不再注入 USER_GITCODE_TOKEN。"""
    env = _run_stream(user_token="USR", bot_token="BOT")
    _assert_no_platform_tokens(env)


def test_no_platform_tokens_chat_without_identity():
    """chat + 无用户身份：P2 仍不注入任何平台 token。"""
    env = _run_stream(user_token="", bot_token="BOT1", user_id=0)
    _assert_no_platform_tokens(env)


def test_no_platform_tokens_chat_db_user_token():
    """chat + user_id>0 + DB 用户 token：P2 不注入（服务端按会话主体执行）。"""
    env = _run_stream(user_token="", bot_token="", user_id=3, db_user_token="DBUSR")
    _assert_no_platform_tokens(env)


def test_no_platform_tokens_devops_event():
    """devops 事件任务（非 chat）：P2 不再注入 BOT 平台 token。"""
    env = _run_stream(user_token="", bot_token="BOT_EVT", user_id=7,
                      db_user_token="DBUSR", event_type="issue_opened")
    _assert_no_platform_tokens(env)


def test_vcs_api_base_still_injected():
    """机制元数据 VCS_API_BASE / VCS_NAME 仍注入（技能判断平台用，非 token）。"""
    env = _run_stream(user_token="USR", repo_key="github:owner/repo")
    _assert_no_platform_tokens(env)
    assert env["VCS_API_BASE"] == "https://api.github.com"
    assert env["VCS_NAME"] == "GitHub"


def test_mechanism_metadata_kept():
    """事件机制元数据（DEVOPS_EVENT_TYPE）仍注入；不注入平台 token。"""
    env = _run_stream(user_token="", bot_token="BOT_EVT", user_id=7,
                      event_type="issue_opened")
    assert env["DEVOPS_EVENT_TYPE"] == "issue_opened"
    _assert_no_platform_tokens(env)


# ===================== stream_chat_agent / stream_devops_agent 隔离 =====================

def _run_chat_fn(user_token="", bot_token="", user_id=0):
    """直接调 stream_chat_agent（签名无 bot_token）"""
    from src.runtime.executor import stream_chat_agent

    fake_popen = _FakePopen()
    with mock.patch("src.agent.executor.get_backend", return_value=_FakeBackend()), \
         mock.patch("src.agent.executor.subprocess.Popen", fake_popen), \
         mock.patch("src.agent.executor.shutil.which", return_value="/usr/bin/fake"), \
         mock.patch("src.agent.executor._track_proc", lambda *a, **kw: None), \
         mock.patch("src.agent.executor._untrack_proc", lambda *a, **kw: None), \
         mock.patch("src.user.db.get_credentials", return_value=[]), \
         mock.patch("src.user.db.get_user_gitcode_token", return_value="DBUSR"):
        list(stream_chat_agent(
            prompt="hi", cwd="/tmp", session_id="", backend_name="fake",
            account="u", user_id=user_id, user_token=user_token, max_exec_seconds=60,
            repo_key="gitcode:owner/repo",
        ))
    return fake_popen.captured


def _run_devops_fn(bot_token="", user_token="", repo_user_id=0):
    """直接调 stream_devops_agent（签名无 user_token）"""
    from src.runtime.executor import stream_devops_agent

    fake_popen = _FakePopen()
    with mock.patch("src.agent.executor.get_backend", return_value=_FakeBackend()), \
         mock.patch("src.agent.executor.subprocess.Popen", fake_popen), \
         mock.patch("src.agent.executor.shutil.which", return_value="/usr/bin/fake"), \
         mock.patch("src.agent.executor._track_proc", lambda *a, **kw: None), \
         mock.patch("src.agent.executor._untrack_proc", lambda *a, **kw: None), \
         mock.patch("src.user.db.get_credentials", return_value=[]), \
         mock.patch("src.user.db.get_user_gitcode_token", return_value="DBUSR"):
        list(stream_devops_agent(
            prompt="hi", cwd="/tmp", session_id="", event_type="issue_opened",
            backend_name="fake", account="u", repo_user_id=repo_user_id,
            bot_token=bot_token, max_exec_seconds=60,
            repo_key="gitcode:owner/repo",
        ))
    return fake_popen.captured


def test_chat_fn_injects_no_platform_token():
    """stream_chat_agent：P2 不注入任何平台 token，仅机制元数据。"""
    env = _run_chat_fn(user_token="USR", user_id=3)
    _assert_no_platform_tokens(env)
    assert env["VCS_API_BASE"] == "https://gitcode.com/api/v5"


def test_devops_fn_injects_no_platform_token():
    """stream_devops_agent：P2 不注入 BOT 平台 token。"""
    env = _run_devops_fn(bot_token="BOT_EVT", repo_user_id=7)
    _assert_no_platform_tokens(env)
    assert env["DEVOPS_EVENT_TYPE"] == "issue_opened"