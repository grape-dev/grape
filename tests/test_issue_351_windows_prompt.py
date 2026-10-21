"""Issue #351: Windows 上多行提示词被 .cmd 壳截断——修复行为测试。

覆盖验收标准：
- find_cli / which 优先返回 exe 路径（exe 在 cmd 之前）
- cli_path 以 .cmd/.bat 结尾时启动前输出 WARN
- prompt_via_stdin 默认 True；opencode/codex prompt 经 stdin 传输
- 多行提示词经 stdin 完整到达子进程（探活与真实任务同路径，行为一致）

说明：全程不依赖真实引擎安装（Windows runner 冒烟可跑）：
- exe 优先用 mock shutil.which 验证查找顺序
- WARN 用真实可执行的 .cmd 后缀脚本（Linux 上可 exec，扩展名仅命名）触发
- stdin 传输用 sys.executable 子进程回读 stdin 验证多行完整到达
"""
import json
import os
import stat
import sys
from unittest.mock import patch

import pytest

from src.runtime import os_adapter
from src.runtime.base import AgentRuntime
from src.runtime.opencode import OpencodeRuntime
from src.runtime.codex import CodexRuntime
from src.runtime.hermes import HermesRuntime
from src.runtime.codearts import CodeArtsRuntime


MULTILINE_PROMPT = "\n".join([
    "## Role Definition",
    "You are a DevOps operations assistant.",
    "- 第一行结论",
    "- 第二行结论",
    "最后一行",
])


class TestWhichExePriority:
    """which() Windows 扩展查找顺序：exe → 裸名 → cmd → bat（issue #351）。"""

    def test_exe_preferred_over_cmd(self):
        with patch.object(os_adapter, "_IS_WINDOWS", True):
            with patch("shutil.which") as mock_which:
                def side_effect(name):
                    return {
                        "opencode.exe": r"C:\npm\opencode.exe",
                        "opencode.cmd": r"C:\npm\opencode.cmd",
                    }.get(name)
                mock_which.side_effect = side_effect
                assert os_adapter.which("opencode") == r"C:\npm\opencode.exe"

    def test_exe_checked_before_bare_pathtext(self):
        """exe 显式候选优先于裸名（PATHEXT 可能先命中 .cmd）。"""
        with patch.object(os_adapter, "_IS_WINDOWS", True):
            with patch("shutil.which") as mock_which:
                def side_effect(name):
                    if name == "opencode":
                        return r"C:\npm\opencode.cmd"  # PATHEXT 命中 cmd
                    if name == "opencode.exe":
                        return r"C:\npm\opencode.exe"
                    return None
                mock_which.side_effect = side_effect
                assert os_adapter.which("opencode") == r"C:\npm\opencode.exe"

    def test_cmd_fallback_kept_when_no_exe(self):
        """旧环境无 exe：cmd 兜底保留（调用方负责 WARN）。"""
        with patch.object(os_adapter, "_IS_WINDOWS", True):
            with patch("shutil.which") as mock_which:
                def side_effect(name):
                    if name == "opencode.cmd":
                        return r"C:\npm\opencode.cmd"
                    return None
                mock_which.side_effect = side_effect
                assert os_adapter.which("opencode") == r"C:\npm\opencode.cmd"

    def test_unix_unchanged(self):
        with patch.object(os_adapter, "_IS_WINDOWS", False):
            with patch("shutil.which", return_value="/usr/bin/opencode") as mock_which:
                assert os_adapter.which("opencode") == "/usr/bin/opencode"
                mock_which.assert_called_once_with("opencode")


class TestEngineWindowsPathsExeFirst:
    """_ENGINE_WINDOWS_PATHS 兜底路径：exe 候选排在 cmd 之前（issue #351）。"""

    @pytest.mark.parametrize("engine", ["opencode", "codex", "codearts", "cursor"])
    def test_exe_paths_before_cmd(self, engine):
        with patch.object(os_adapter, "_IS_WINDOWS", True):
            paths = os_adapter.engine_search_paths(engine)
        exe_paths = [p for p in paths if p.lower().endswith(".exe")]
        cmd_paths = [p for p in paths if p.lower().endswith(".cmd")]
        if cmd_paths:  # 有 cmd 兜底的引擎必须有 exe 候选且排在前
            assert exe_paths, f"{engine}: 缺少 .exe 候选"
            first_cmd = min(paths.index(p) for p in cmd_paths)
            last_exe = max(paths.index(p) for p in exe_paths)
            assert last_exe < first_cmd, f"{engine}: .exe 候选必须排在 .cmd 之前"


class TestIsShellWrapper:
    def test_cmd_and_bat_detected(self):
        assert os_adapter.is_shell_wrapper(r"C:\npm\opencode.cmd") is True
        assert os_adapter.is_shell_wrapper(r"C:\npm\opencode.CMD") is True
        assert os_adapter.is_shell_wrapper(r"C:\npm\opencode.bat") is True

    def test_exe_and_empty_not_detected(self):
        assert os_adapter.is_shell_wrapper(r"C:\npm\opencode.exe") is False
        assert os_adapter.is_shell_wrapper("") is False


class TestPromptViaStdinDefaults:
    def test_base_default_true(self):
        assert AgentRuntime.prompt_via_stdin is True

    def test_opencode_stdin(self):
        assert OpencodeRuntime.prompt_via_stdin is True
        args = OpencodeRuntime().build_args(MULTILINE_PROMPT)
        assert MULTILINE_PROMPT not in args  # 多行提示词不进 argv

    def test_codex_stdin_with_marker(self):
        assert CodexRuntime.prompt_via_stdin is True
        args = CodexRuntime().build_args(MULTILINE_PROMPT)
        assert args[-1] == "-"
        assert MULTILINE_PROMPT not in args

    def test_hermes_explicit_argv(self):
        """hermes Out of Scope：显式保持 argv 传 prompt。"""
        assert HermesRuntime.prompt_via_stdin is False

    def test_codearts_keeps_argv(self):
        """codearts stdin 触发沙箱网络封锁（实证注释），保留 argv。"""
        assert CodeArtsRuntime.prompt_via_stdin is False
        args = CodeArtsRuntime().build_args(MULTILINE_PROMPT)
        assert MULTILINE_PROMPT in args


class _StdinEchoBackend(AgentRuntime):
    """回读 stdin 并以 NDJSON text 事件原样吐出（验证多行提示词完整到达）。"""
    name = "stdinechobackend"
    binary = sys.executable
    prompt_via_stdin = True

    def build_args(self, prompt, session_id=""):
        code = (
            "import sys, json\n"
            "data = sys.stdin.read()\n"
            "print(json.dumps({'type': 'text', 'part': {'type': 'text', 'text': data}}), flush=True)\n"
        )
        return ["-c", code]


class _CmdShellBackend(_StdinEchoBackend):
    """binary 名经 which 解析到 .cmd 后缀路径（触发 WARN）。"""
    name = "cmdshellbackend"


def _register(backend):
    from src.agent.executor import _BACKENDS
    _BACKENDS[backend.name] = backend
    return backend.name


class TestStdinMultilinePrompt:
    def test_multiline_prompt_arrives_intact_via_stdin(self):
        """多行提示词经 stdin 完整到达子进程（Windows 冒烟核心用例）。"""
        from src.agent.executor import stream_agent
        name = _register(_StdinEchoBackend())
        events = list(stream_agent(
            MULTILINE_PROMPT, os.getcwd(), "",
            backend_name=name, max_exec_seconds=60,
        ))
        texts = [d for et, d in events if et == "text"]
        assert texts, f"no text events, events={events!r}"
        assert MULTILINE_PROMPT in "\n".join(texts)

    def test_cmd_suffix_emits_warn(self, tmp_path, caplog):
        """cli_path 以 .cmd 结尾时启动前输出 WARN（cmd 兜底降级为告警）。"""
        import logging
        from src.agent.executor import stream_agent
        # Linux 上 .cmd 后缀的 shebang 脚本可 exec（扩展名仅命名），真实走启动路径
        fake_cmd = tmp_path / "opencode.cmd"
        fake_cmd.write_text(
            "#!/bin/sh\n"
            "echo '" + json.dumps({"type": "text", "part": {"type": "text", "text": "ok"}})
            .replace("'", "'\\''") + "'\n",
            encoding="utf-8",
        )
        fake_cmd.chmod(fake_cmd.stat().st_mode | stat.S_IEXEC)
        name = _register(_CmdShellBackend())
        with patch("src.runtime.os_adapter.which", return_value=str(fake_cmd)):
            with caplog.at_level(logging.WARNING):
                events = list(stream_agent(
                    "p", os.getcwd(), "", backend_name=name, max_exec_seconds=60,
                ))
        assert any("shell wrapper" in r.message for r in caplog.records), \
            f"WARN not emitted, records={[r.message for r in caplog.records]}"
        assert any(et == "done" for et, _ in events)
