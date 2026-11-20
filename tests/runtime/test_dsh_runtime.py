"""DshRuntime 单元测试（测试先行）。

测试数据全部取自 2026-10-23 本机 `dsh --profile headless --json` 的**真实捕获**
（不是手编形状），包括成功流与两种失败流。
见 /root/dsh_grape_docs/WORKER-ONBOARDING-AND-DSH-ENGINE-DESIGN.md §4.2.4。
"""
import json
import logging
import os
import re
import stat
import subprocess
import time

import pytest

from src.domains.runtime.engine.dsh import DshRuntime


@pytest.fixture()
def rt():
    return DshRuntime()


# ===== 类的身份声明 =====

def test_identity(rt):
    assert rt.name == "dsh"
    assert rt.binary == "dsh"
    # 对齐 codex/issue #351：prompt 走 stdin，规避 argv 多行截断
    assert rt.prompt_via_stdin is True


# ===== build_args =====

def test_build_args_basic(rt):
    args = rt.build_args("读一下文件")
    assert args[0] == "headless"
    assert "--json" in args
    # 末尾的 "-" 是 stdin 占位标记（dsh headless "-" 从 stdin 读任务）
    assert args[-1] == "-"


def test_build_args_with_session(rt):
    args = rt.build_args("继续", session_id="session-abc123")
    assert "--session-id" in args
    assert args[args.index("--session-id") + 1] == "session-abc123"
    assert args[-1] == "-"


def test_build_args_without_session_has_no_flag(rt):
    assert "--session-id" not in rt.build_args("新任务")


def test_build_args_empty_prompt_omits_stdin_marker(rt):
    """空 prompt 时不加 "-"。

    以 codex 的既有约定为准（codex 是生产里在跑的同类引擎：
    `if prompt: args.append("-")`）。空 prompt 是退化情形，
    worker 永远会传真实 prompt，故此处不额外发明行为，保持与 codex 一致。
    """
    assert "-" not in rt.build_args("")


# ===== parse_output：真实事件流样本 =====

def test_parse_session_event(rt):
    line = json.dumps({"type": "session", "sessionId": "session-c5818a2e", "cwd": "/tmp"})
    ev = rt.parse_output(line)
    assert ev["type"] == "step_start"
    assert ev["sessionID"] == "session-c5818a2e"


def test_parse_thinking(rt):
    line = json.dumps({"type": "thinking", "text": "先看文件"})
    ev = rt.parse_output(line)
    assert ev["type"] == "reasoning"
    assert ev["text"] == "先看文件"


def test_parse_text(rt):
    line = json.dumps({"type": "text", "text": "9.9.9"})
    ev = rt.parse_output(line)
    assert ev["type"] == "text"
    assert ev["text"] == "9.9.9"


def test_parse_tool_call(rt):
    line = json.dumps({"type": "tool_call", "callId": "call_d6287e",
                       "tool": "read", "input": {"file_path": "/tmp/probe-ver.json"}})
    ev = rt.parse_output(line)
    assert ev["type"] == "tool_use"
    assert ev["tool"] == "read"
    assert ev["input"] == {"file_path": "/tmp/probe-ver.json"}


def test_parse_tool_result_completed(rt):
    line = json.dumps({"type": "tool_result", "callId": "call_d6287e",
                       "status": "completed", "result": "<path>/tmp/probe-ver.json</path>"})
    ev = rt.parse_output(line)
    assert ev["type"] == "tool_result"
    assert "/tmp/probe-ver.json" in ev["text"]


def test_parse_tool_result_error_is_marked(rt):
    line = json.dumps({"type": "tool_result", "callId": "c1",
                       "status": "error", "result": "permission denied"})
    ev = rt.parse_output(line)
    assert ev["type"] == "tool_result"
    assert "error" in ev["text"].lower() or "permission denied" in ev["text"]


def test_parse_step_end_usage(rt):
    """usage 挂在 status/step_end（实测字段名 inputTokens/outputTokens/totalTokens/cacheReadTokens）。"""
    line = json.dumps({"type": "status", "phase": "step_end", "turn": 1, "step": 1,
                       "usage": {"inputTokens": 5341, "outputTokens": 94,
                                 "totalTokens": 5947, "cacheReadTokens": 512}})
    ev = rt.parse_output(line)
    assert ev["type"] == "step_finish"
    assert ev["tokens"] == 5947
    assert ev["usage"]["input"] == 5341
    assert ev["usage"]["output"] == 94
    assert ev["usage"]["cache_read"] == 512


def test_parse_step_end_without_usage(rt):
    """源码注释：某次 attempt 未报 usage 时，step 整条不带 usage（不是补 0 的部分和）。"""
    line = json.dumps({"type": "status", "phase": "step_end", "turn": 1, "step": 2})
    ev = rt.parse_output(line)
    assert ev["type"] == "step_finish"
    assert ev["tokens"] == 0


def test_parse_turn_end_completed_is_not_error(rt):
    line = json.dumps({"type": "status", "phase": "turn_end", "turn": 1,
                       "reason": {"kind": "completed"}})
    ev = rt.parse_output(line)
    assert ev is None or ev.get("type") != "error"


def test_parse_turn_end_error(rt):
    """失败判别位：status.turn_end.reason.error{message,code}（两次实测失败都是这个形状）。"""
    line = json.dumps({"type": "status", "phase": "turn_end", "turn": 1,
                       "reason": {"kind": "error",
                                  "error": {"message": "no API key", "code": "MISSING_CREDENTIAL"}}})
    ev = rt.parse_output(line)
    assert ev["type"] == "error"
    assert "no API key" in ev["message"]
    assert ev.get("code") == "MISSING_CREDENTIAL"


def test_parse_final_is_not_duplicated_as_text(rt):
    """final.text 与 text 事件内容重复（实测两者都是 '9.9.9'），不能重复喂给消费端。"""
    line = json.dumps({"type": "final", "text": "9.9.9"})
    ev = rt.parse_output(line)
    assert ev is not None
    assert ev["type"] != "text"


# ===== 健壮性（源码强制的降级：超限 → 截断 → 只剩 {type,truncated:true}）=====

def test_parse_truncated_line_does_not_crash(rt):
    line = json.dumps({"type": "tool_result", "truncated": True})
    ev = rt.parse_output(line)
    assert ev is not None
    assert ev["type"] in ("other", "tool_result")   # 允许任一，但绝不抛错


def test_parse_non_json_returns_none(rt):
    assert rt.parse_output("not json at all") is None


def test_parse_json_non_dict_returns_none(rt):
    assert rt.parse_output("[1,2,3]") is None
    assert rt.parse_output('"a string"') is None


def test_parse_unknown_type_is_other(rt):
    line = json.dumps({"type": "some_future_event", "x": 1})
    ev = rt.parse_output(line)
    assert ev["type"] == "other"
    assert ev["raw"] == "some_future_event"


def test_parse_missing_fields_does_not_crash(rt):
    """字段缺失不得抛错（降级后可能只剩 type）。"""
    for probe in ({"type": "text"}, {"type": "tool_call"}, {"type": "tool_result"},
                  {"type": "status"}, {"type": "status", "phase": "step_end"}):
        ev = rt.parse_output(json.dumps(probe))
        assert ev is None or isinstance(ev, dict)


def test_parse_blank_line_returns_none(rt):
    assert rt.parse_output("") is None
    assert rt.parse_output("   ") is None


# ===== 回归：拿整条真实事件流跑一遍 =====

def test_parse_full_real_stream(rt):
    """用 2026-10-23 实测捕获的完整 13 行事件流做端到端解析回归。"""
    lines = [
        {"type": "session", "sessionId": "session-x", "cwd": "/tmp"},
        {"type": "status", "phase": "turn_start", "turn": 1},
        {"type": "status", "phase": "step_start", "turn": 1, "step": 1},
        {"type": "thinking", "text": "读文件"},
        {"type": "text", "text": "\n"},
        {"type": "tool_call", "callId": "c1", "tool": "read", "input": {"file_path": "/tmp/x"}},
        {"type": "tool_result", "callId": "c1", "status": "completed", "result": "9.9.9"},
        {"type": "status", "phase": "step_end", "turn": 1, "step": 1,
         "usage": {"inputTokens": 5341, "outputTokens": 94, "totalTokens": 5947, "cacheReadTokens": 512}},
        {"type": "status", "phase": "step_start", "turn": 1, "step": 2},
        {"type": "text", "text": "9.9.9"},
        {"type": "status", "phase": "step_end", "turn": 1, "step": 2,
         "usage": {"inputTokens": 5496, "outputTokens": 7, "totalTokens": 6015, "cacheReadTokens": 512}},
        {"type": "status", "phase": "turn_end", "turn": 1, "reason": {"kind": "completed"}},
        {"type": "final", "text": "9.9.9"},
    ]
    kinds, texts, tokens = [], [], 0
    for ln in lines:
        ev = rt.parse_output(json.dumps(ln))
        assert ev is not None, ln
        kinds.append(ev["type"])
        if ev["type"] == "text":
            texts.append(ev["text"])
        if ev["type"] == "step_finish":
            tokens += int(ev.get("tokens", 0))
    assert kinds[0] == "step_start"                     # session → step_start
    assert kinds.count("step_finish") == 2              # 两个 step 各一次
    assert tokens == 5947 + 6015                        # token 可累加
    assert "9.9.9" in "".join(texts)                    # 答案文本在 text 流里
    assert kinds[-1] != "text"                          # final 未被当作 text 重复# ===== env 收尾钩子：token 穿透必须在 env_extra 合并之后 =====
#
# 背景（回归测试）：#948 把「写 GRAPE_AUTH_FILE 临时文件」的逻辑放进了 get_env()，
# 但 GRAPE_SESSION_TOKEN 是任务级的、存在 agent_tasks.env_extra 里，
# executor.py 的顺序是 get_env() → env.update(env_extra) → Popen()。
# 于是在 get_env() 里读 token 永远是空串，临时文件从未生成，
# dsh 的 bash 工具被 /TOKEN/i scrub 后拿不到凭据 → agent 报「前置检查未通过」。
# 修法：抽出 prepare_env(env) 钩子，由 executor 在 env_extra 合并之后调用。


def test_get_env_does_not_create_auth_file(rt, monkeypatch):
    """get_env 阶段没有任务级 token，不该产出 GRAPE_AUTH_FILE。

    monkeypatch 隔离：get_env 会拷贝 os.environ，若测试进程自身带
    GRAPE_SESSION_TOKEN（平台注入的任务环境），裸跑会误得 GRAPE_AUTH_FILE——
    该断言验证的是「get_env 阶段不产文件」，必须把环境变量钉空再跑。
    """
    monkeypatch.delenv("GRAPE_SESSION_TOKEN", raising=False)
    env = rt.get_env()
    assert "GRAPE_AUTH_FILE" not in env


def test_prepare_env_writes_token_file_after_merge(rt, tmp_path):
    """env_extra 合并后调用 prepare_env，应在 cwd 生成 0600 文件且 git 不可见。"""
    repo = tmp_path / "worktree"
    subprocess.run(["git", "init", "-q", str(repo)], check=True, capture_output=True)
    env = rt.get_env()
    env.update({"GRAPE_SESSION_TOKEN": "tk_regression_token"})
    auth_file = rt.prepare_env(env, cwd=str(repo))

    assert auth_file, "prepare_env 应设置 GRAPE_AUTH_FILE"
    assert env.get("GRAPE_AUTH_FILE") == auth_file
    assert os.path.dirname(auth_file) == str(repo)
    assert re.fullmatch(r"\.grape-sess-[0-9a-f]{8}\.tok", os.path.basename(auth_file))
    assert os.path.exists(auth_file)
    assert stat.S_IMODE(os.stat(auth_file).st_mode) == 0o600
    with open(auth_file, encoding="utf-8") as f:
        assert f.read() == "tk_regression_token"
    exclude_path = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "--git-path", "info/exclude"],
        check=True, capture_output=True, text=True,
    ).stdout.strip()
    if not os.path.isabs(exclude_path):
        exclude_path = os.path.join(repo, exclude_path)
    with open(exclude_path, encoding="utf-8") as f:
        assert ".grape-sess-*.tok" in f.read().splitlines()
    status = subprocess.run(
        ["git", "-C", str(repo), "status", "--porcelain", "--untracked-files=all"],
        check=True, capture_output=True, text=True,
    )
    assert status.stdout == ""


def test_prepare_env_makes_missing_cwd(rt, tmp_path):
    """工位目录可能尚未创建，prepare_env 必须先 makedirs 再落盘。"""
    repo = tmp_path / "nested" / "worktree"
    env = {"GRAPE_SESSION_TOKEN": "tk_nested"}
    auth_file = rt.prepare_env(env, cwd=str(repo))
    assert auth_file and os.path.dirname(auth_file) == str(repo)
    assert os.path.isfile(auth_file)


def test_prepare_env_registers_exclude_before_token_file(rt, tmp_path, monkeypatch):
    """先注册 exclude 再创建 token，缩小 SIGKILL 后文件进入 git status 的窗口。"""
    repo = tmp_path / "worktree"
    subprocess.run(["git", "init", "-q", str(repo)], check=True, capture_output=True)
    exclude_path = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "--git-path", "info/exclude"],
        check=True, capture_output=True, text=True,
    ).stdout.strip()
    if not os.path.isabs(exclude_path):
        exclude_path = os.path.join(str(repo), exclude_path)

    original_open = os.open
    observed = []

    def checking_open(path, flags, *args, **kwargs):
        if str(path).endswith(".tok"):
            with open(exclude_path, encoding="utf-8") as f:
                observed.append(".grape-sess-*.tok" in f.read().splitlines())
        return original_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(os, "open", checking_open)
    auth_file = rt.prepare_env({"GRAPE_SESSION_TOKEN": "tk_order"}, cwd=str(repo))

    assert auth_file and os.path.isfile(auth_file)
    assert observed == [True], "token 文件创建前 exclude 必须已包含匹配规则"


def test_prepare_env_noop_without_token(rt, tmp_path):
    """没有 token 时不生成文件（其他引擎同样适用，走 env fallback）。"""
    env = {}
    auth_file = rt.prepare_env(env, cwd=str(tmp_path))
    assert auth_file is None
    assert "GRAPE_AUTH_FILE" not in env
    assert not list(tmp_path.glob(".grape-sess-*.tok"))


def test_prepare_env_without_cwd_falls_back_to_temp_and_warns(rt, caplog):
    """无 cwd 保持 direct 执行可用，但必须 warning 留痕。"""
    env = {"GRAPE_SESSION_TOKEN": "tk_temp_fallback"}
    with caplog.at_level(logging.WARNING):
        auth_file = rt.prepare_env(env)
    assert auth_file and env["GRAPE_AUTH_FILE"] == auth_file
    assert re.fullmatch(r"grape-sess-[a-z0-9_]+\.tok", os.path.basename(auth_file))
    assert "cwd" in caplog.text.lower()


def test_prepare_env_warns_when_git_path_empty(rt, tmp_path, caplog):
    """非 git cwd 无 info/exclude 路径：保留 token 但 warning 必须可见。"""
    env = {"GRAPE_SESSION_TOKEN": "tk_no_git"}
    with caplog.at_level(logging.WARNING):
        auth_file = rt.prepare_env(env, cwd=str(tmp_path))
    assert auth_file and os.path.isfile(auth_file)
    assert "info/exclude" in caplog.text


def test_prepare_env_warns_when_git_path_registration_raises(rt, tmp_path, monkeypatch, caplog):
    """git-path 注册异常不得阻断 token 穿透。"""
    def boom(*args, **kwargs):
        raise OSError("git unavailable")

    monkeypatch.setattr(subprocess, "run", boom)
    env = {"GRAPE_SESSION_TOKEN": "tk_git_error"}
    with caplog.at_level(logging.WARNING):
        auth_file = rt.prepare_env(env, cwd=str(tmp_path))
    assert auth_file and os.path.isfile(auth_file)
    assert "info/exclude" in caplog.text


def test_prepare_env_survives_unwritable_temp(rt, monkeypatch, caplog):
    """写文件失败不阻断执行，返回 None 并保留 warning。"""
    def boom(*a, **kw):
        raise OSError("disk full")

    monkeypatch.setattr("tempfile.mkstemp", boom)
    env = rt.get_env()
    env.update({"GRAPE_SESSION_TOKEN": "tk_x"})
    with caplog.at_level(logging.WARNING):
        auth_file = rt.prepare_env(env)  # 不应抛
    assert auth_file is None
    assert "GRAPE_AUTH_FILE" not in env
    assert "prepare_env" in caplog.text


def test_prepare_env_removes_partial_file_when_write_fails(rt, tmp_path, monkeypatch, caplog):
    """cwd 文件写入半途失败时必须删除半成品，不能把残缺 token 留在工位。"""
    real_write = os.write

    def partial_write(fd, data):
        real_write(fd, b"partial")
        raise OSError("disk full")

    monkeypatch.setattr(os, "write", partial_write)
    env = {"GRAPE_SESSION_TOKEN": "tk_partial"}
    with caplog.at_level(logging.WARNING):
        auth_file = rt.prepare_env(env, cwd=str(tmp_path))

    assert auth_file is None
    assert "GRAPE_AUTH_FILE" not in env
    assert not list(tmp_path.glob(".grape-sess-*.tok"))
    assert "prepare_env" in caplog.text


def test_prepare_env_removes_partial_temp_file_when_write_fails(rt, monkeypatch, caplog):
    """无 cwd 的 temp 回退写入失败时也必须删除半成品。"""
    import tempfile
    real_write = os.write
    real_mkstemp = tempfile.mkstemp
    created = {}

    def tracked_mkstemp(*args, **kwargs):
        fd, path = real_mkstemp(*args, **kwargs)
        created["path"] = path
        return fd, path

    def partial_write(fd, data):
        real_write(fd, b"partial")
        raise OSError("disk full")

    monkeypatch.setattr(tempfile, "mkstemp", tracked_mkstemp)
    monkeypatch.setattr(os, "write", partial_write)
    env = {"GRAPE_SESSION_TOKEN": "tk_temp_partial"}
    with caplog.at_level(logging.WARNING):
        auth_file = rt.prepare_env(env)

    assert auth_file is None
    assert "GRAPE_AUTH_FILE" not in env
    assert created.get("path") and not os.path.exists(created["path"])
    assert "prepare_env" in caplog.text

# ===== executor 接线：prepare_env 钩子必须在 env_extra 合并后、Popen 前被调用 =====
#
# dsh 的令牌穿透依赖两个前提同时成立：
#   ① executor 在 env.update(env_extra) 之后调用 backend.prepare_env(env)
#     （token 是任务级的，get_env 阶段还不在 env 里）；
#   ② prepare_env 写入的 GRAPE_AUTH_FILE 真正进入 Popen 的最终 env
#     （非 ACP 后端 pre_exec 默认透传 env，prepare_env 的就地修改随之生效）。
# 只测 DshRuntime.prepare_env 本身（上面 4 个用例）钉不住接线——
# 钩子改名/挪位/被 ACP 分支旁路时单测仍绿、线上却断，故补 executor 级回归。


class _PrepareEnvRecorderBackend:
    """记录 prepare_env 调用时序并打标的假后端。"""

    name = "fake"
    binary = "true"
    prompt_via_stdin = False

    def __init__(self):
        self.prepare_saw_token = None
        self.prepare_saw_cwd = None

    def get_env(self, account="", user_id=0):
        return {}

    def build_args(self, prompt, session_id):
        return []

    def parse_output(self, line):
        return None

    def prepare_env(self, env, cwd=""):
        # prepare_env 执行时必须已能看到任务级 token（env_extra 合并之后）
        self.prepare_saw_token = env.get("GRAPE_SESSION_TOKEN", "")
        self.prepare_saw_cwd = cwd
        env["PREPARE_ENV_MARK"] = "1"


def _run_stream_capture_env(cwd, backend, env_extra):
    """跑 stream_agent 到 Popen，捕获传给子进程的最终 env。"""
    from unittest import mock
    import tests.runtime.test_executor_token_env as _tenv

    fake_popen = _tenv._FakePopen()
    with mock.patch("src.domains.runtime.agents.executor.get_backend", return_value=backend), \
         mock.patch("src.domains.runtime.agents.executor.subprocess.Popen", fake_popen), \
         mock.patch("src.domains.runtime.engine.os_adapter.find_cli", return_value="/usr/bin/fake"), \
         mock.patch("src.domains.runtime.agents.executor._track_proc", lambda *a, **kw: None), \
         mock.patch("src.domains.runtime.agents.executor._untrack_proc", lambda *a, **kw: None), \
         mock.patch("src.domains.mgmt.user.db.get_credentials", return_value=[]):
        from src.domains.runtime.engine.executor import stream_agent
        list(stream_agent(
            prompt="hi", cwd=cwd, session_id="", event_type="issue_opened",
            backend_name="fake", account="u", user_id=0, env_extra=env_extra,
            max_exec_seconds=60, repo_key="gitcode:owner/repo"))
    return fake_popen.captured


def test_executor_calls_prepare_env_after_env_extra_merge(tmp_path):
    """prepare_env 在 env_extra 合并后执行：能看到任务级 token，标记进 Popen env。"""
    backend = _PrepareEnvRecorderBackend()
    cwd = str(tmp_path)
    final_env = _run_stream_capture_env(cwd, backend, {"GRAPE_SESSION_TOKEN": "tk_task_scoped"})
    assert backend.prepare_saw_token == "tk_task_scoped", \
        "prepare_env 必须在 env_extra（含任务级 GRAPE_SESSION_TOKEN）合并之后调用"
    assert backend.prepare_saw_cwd == cwd, "prepare_env 必须收到 cwd 以在沙箱可见目录落盘"
    assert final_env.get("PREPARE_ENV_MARK") == "1", \
        "prepare_env 的就地修改必须落入 Popen 最终 env（非 ACP 后端 pre_exec 默认透传）"


def test_executor_supports_legacy_prepare_env_signature(tmp_path):
    """自定义 runtime 仍使用 prepare_env(env) 时不能因新 cwd 参数静默失效。"""
    class _LegacyPrepareEnvBackend(_PrepareEnvRecorderBackend):
        def prepare_env(self, env):
            env["PREPARE_ENV_MARK"] = "legacy"

    final_env = _run_stream_capture_env(
        str(tmp_path), _LegacyPrepareEnvBackend(), {"GRAPE_SESSION_TOKEN": "tk_legacy"})

    assert final_env.get("PREPARE_ENV_MARK") == "legacy"


class _PrepareAuthFileBackend:
    """在 cwd 下创建真实 token 文件的假后端。"""

    name = "fake"
    binary = "true"
    prompt_via_stdin = False

    def __init__(self):
        self.auth_file = ""

    def get_env(self, account="", user_id=0):
        return {}

    def build_args(self, prompt, session_id):
        return []

    def parse_output(self, line):
        return None

    def prepare_env(self, env, cwd=""):
        self.auth_file = os.path.join(cwd, ".grape-sess-deadbeef.tok")
        with open(self.auth_file, "w", encoding="utf-8") as f:
            f.write("tk_fake")
        os.chmod(self.auth_file, 0o600)
        env["GRAPE_AUTH_FILE"] = self.auth_file
        return self.auth_file


def test_executor_cleans_prepared_auth_file_after_task(tmp_path):
    """正常结束时必须清理 cwd 下 prepare_env 创建的 token 文件。"""
    backend = _PrepareAuthFileBackend()
    keep = tmp_path / "keep.tok"
    keep.write_text("not a session token", encoding="utf-8")
    final_env = _run_stream_capture_env(str(tmp_path), backend, {"GRAPE_SESSION_TOKEN": "tk_task"})
    assert final_env.get("GRAPE_AUTH_FILE") == backend.auth_file
    assert not os.path.exists(backend.auth_file)
    assert keep.exists(), "清理只应命中 .grape-sess-*.tok"


def test_executor_keeps_fresh_auth_file_from_other_task(tmp_path):
    """同一 cwd 内其他任务的新 token 不能被本次任务收尾误删。"""
    backend = _PrepareAuthFileBackend()
    other = tmp_path / ".grape-sess-othertask.tok"
    other.write_text("tk_other", encoding="utf-8")

    _run_stream_capture_env(str(tmp_path), backend, {"GRAPE_SESSION_TOKEN": "tk_task"})

    assert backend.auth_file and not os.path.exists(backend.auth_file)
    assert other.exists(), "只有本任务记录的 auth_file 应被直接清理"


def test_executor_without_token_keeps_fresh_auth_file_from_other_task(tmp_path):
    """本任务无 token 时只清陈旧残留，不得清掉其他任务正在使用的 token。"""
    backend = _PrepareEnvRecorderBackend()
    other = tmp_path / ".grape-sess-othertask.tok"
    other.write_text("tk_other", encoding="utf-8")

    _run_stream_capture_env(str(tmp_path), backend, {})

    assert other.exists(), "无 token 的 prepare_env 不应触发全量 glob 删除"


def test_executor_sweeps_stale_auth_file_from_crashed_task(tmp_path):
    """SIGKILL 遗留的陈旧 token 由后续任务按 mtime 阈值清扫。"""
    backend = _PrepareAuthFileBackend()
    stale = tmp_path / ".grape-sess-crashed.tok"
    stale.write_text("tk_stale", encoding="utf-8")
    old = time.time() - 24 * 60 * 60
    os.utime(stale, (old, old))
    other = tmp_path / ".grape-sess-othertask.tok"
    other.write_text("tk_other", encoding="utf-8")

    _run_stream_capture_env(str(tmp_path), backend, {"GRAPE_SESSION_TOKEN": "tk_task"})

    assert backend.auth_file and not os.path.exists(backend.auth_file)
    assert not stale.exists(), "超过陈旧阈值的 token 应被清扫"
    assert other.exists(), "新 token 不应被陈旧清扫误删"


def test_executor_cleans_auth_file_when_stream_raises(tmp_path):
    """异常路径也必须走 finally 清理，避免 token 残留。"""
    class _ExplodingBackend(_PrepareAuthFileBackend):
        def build_args(self, prompt, session_id):
            raise RuntimeError("boom after prepare_env")

    backend = _ExplodingBackend()
    from src.domains.runtime.engine.executor import stream_agent
    import tests.runtime.test_executor_token_env as _tenv
    from unittest import mock

    fake_popen = _tenv._FakePopen()
    with mock.patch("src.domains.runtime.agents.executor.get_backend", return_value=backend), \
         mock.patch("src.domains.runtime.agents.executor.subprocess.Popen", fake_popen), \
         mock.patch("src.domains.runtime.engine.os_adapter.find_cli", return_value="/usr/bin/fake"), \
         mock.patch("src.domains.runtime.agents.executor._track_proc", lambda *a, **kw: None), \
         mock.patch("src.domains.runtime.agents.executor._untrack_proc", lambda *a, **kw: None):
        with pytest.raises(RuntimeError, match="boom after prepare_env"):
            list(stream_agent(
                prompt="hi", cwd=str(tmp_path), session_id="", event_type="issue_opened",
                backend_name="fake", account="u", user_id=0,
                env_extra={"GRAPE_SESSION_TOKEN": "tk_task"},
                max_exec_seconds=60, repo_key="gitcode:owner/repo"))
    assert backend.auth_file and not os.path.exists(backend.auth_file)


def test_executor_tolerates_backend_without_prepare_env(tmp_path):
    """无 prepare_env 的后端（opencode/hermes/codex 等现状）不报错、env/文件不受影响。"""
    from unittest import mock
    import tests.runtime.test_executor_token_env as _tenv

    class _PlainBackend(_tenv._FakeBackend):
        pass  # 无 prepare_env

    stale = tmp_path / ".grape-sess-stale.tok"
    stale.write_text("unmanaged", encoding="utf-8")
    fake_popen = _tenv._FakePopen()
    with mock.patch("src.domains.runtime.agents.executor.get_backend", return_value=_PlainBackend()), \
         mock.patch("src.domains.runtime.agents.executor.subprocess.Popen", fake_popen), \
         mock.patch("src.domains.runtime.engine.os_adapter.find_cli", return_value="/usr/bin/fake"), \
         mock.patch("src.domains.runtime.agents.executor._track_proc", lambda *a, **kw: None), \
         mock.patch("src.domains.runtime.agents.executor._untrack_proc", lambda *a, **kw: None), \
         mock.patch("src.domains.mgmt.user.db.get_credentials", return_value=[]):
        from src.domains.runtime.engine.executor import stream_agent
        list(stream_agent(
            prompt="hi", cwd=str(tmp_path), session_id="", event_type="issue_opened",
            backend_name="fake", account="u", user_id=0, env_extra={},
            max_exec_seconds=60, repo_key="gitcode:owner/repo"))
    assert "PREPARE_ENV_MARK" not in fake_popen.captured
    assert "GRAPE_AUTH_FILE" not in fake_popen.captured
    assert stale.exists(), "未调用 prepare_env 的后端不应触发 token 清理"


def test_executor_swallows_prepare_env_failure(tmp_path):
    """prepare_env 抛异常只告警不阻断执行；未返回的半成品由 runtime 自行收口。"""
    from unittest import mock
    import tests.runtime.test_executor_token_env as _tenv

    class _BoomBackend(_tenv._FakeBackend):
        def __init__(self):
            self.auth_file = ""

        def prepare_env(self, env, cwd=""):
            self.auth_file = os.path.join(cwd, ".grape-sess-badc0de.tok")
            with open(self.auth_file, "w", encoding="utf-8") as f:
                f.write("tk_fake")
            raise RuntimeError("boom")

    backend = _BoomBackend()
    fake_popen = _tenv._FakePopen()
    with mock.patch("src.domains.runtime.agents.executor.get_backend", return_value=backend), \
         mock.patch("src.domains.runtime.agents.executor.subprocess.Popen", fake_popen), \
         mock.patch("src.domains.runtime.engine.os_adapter.find_cli", return_value="/usr/bin/fake"), \
         mock.patch("src.domains.runtime.agents.executor._track_proc", lambda *a, **kw: None), \
         mock.patch("src.domains.runtime.agents.executor._untrack_proc", lambda *a, **kw: None), \
         mock.patch("src.domains.mgmt.user.db.get_credentials", return_value=[]):
        from src.domains.runtime.engine.executor import stream_agent
        events = list(stream_agent(
            prompt="hi", cwd=str(tmp_path), session_id="", event_type="issue_opened",
            backend_name="fake", account="u", user_id=0, env_extra={},
            max_exec_seconds=60, repo_key="gitcode:owner/repo"))
    assert any(et == "done" for et, _ in events), "prepare_env 失败不应阻断执行流"
    assert backend.auth_file and os.path.exists(backend.auth_file), \
        "executor 不得按 glob 删除未记录到 _auth_state 的文件"
