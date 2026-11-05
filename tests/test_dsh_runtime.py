"""DshRuntime 单元测试（测试先行）。

测试数据全部取自 2026-10-23 本机 `dsh --profile headless --json` 的**真实捕获**
（不是手编形状），包括成功流与两种失败流。
见 /root/dsh_grape_docs/WORKER-ONBOARDING-AND-DSH-ENGINE-DESIGN.md §4.2.4。
"""
import json

import pytest

from src.runtime.dsh import DshRuntime


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


def test_get_env_does_not_create_auth_file(rt):
    """get_env 阶段没有任务级 token，不该产出 GRAPE_AUTH_FILE。"""
    env = rt.get_env()
    assert "GRAPE_AUTH_FILE" not in env


def test_prepare_env_writes_token_file_after_merge(rt, tmp_path):
    """env_extra 合并后调用 prepare_env，应生成 0600 文件且内容为 token。"""
    import os

    env = rt.get_env()
    env.update({"GRAPE_SESSION_TOKEN": "tk_regression_token"})
    rt.prepare_env(env)

    auth_file = env.get("GRAPE_AUTH_FILE")
    assert auth_file, "prepare_env 应设置 GRAPE_AUTH_FILE"
    assert os.path.exists(auth_file)
    assert oct(os.stat(auth_file).st_mode & 0o777) == "0o600"
    with open(auth_file) as f:
        assert f.read() == "tk_regression_token"
    os.unlink(auth_file)


def test_prepare_env_noop_without_token(rt):
    """没有 token 时不生成文件（其他引擎同样适用，走 env fallback）。"""
    env = rt.get_env()
    rt.prepare_env(env)
    assert "GRAPE_AUTH_FILE" not in env


def test_prepare_env_survives_unwritable_temp(rt, monkeypatch):
    """写文件失败不阻断执行（保持 fallback 到 env 变量）。"""
    def boom(*a, **kw):
        raise OSError("disk full")

    monkeypatch.setattr("tempfile.mkstemp", boom)
    env = rt.get_env()
    env.update({"GRAPE_SESSION_TOKEN": "tk_x"})
    rt.prepare_env(env)  # 不应抛
    assert "GRAPE_AUTH_FILE" not in env