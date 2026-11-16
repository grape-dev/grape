"""CubeSandbox 后端测试 — Mock SDK 模式，纯单元测试。

覆盖：
- CubeSandboxExecHandle: wait/stream_stdout/send_stdin/kill/pid/exit_code
- CubeSandboxInstance: _do_create/_do_connect/_do_kill/_do_is_running/_do_refresh
- _run_long_command: uuid 唯一文件名 + nohup 轮询
- run_cmd / run_long_cmd / write_file / get_home / read_file / list_files
- upload_skill / _configure_grape_cli / _clone_repo
- 能力声明: supports_pty / supports_long_cmd / needs_entry_point_wrapper
- configure_repo / repo_dir / skill_dir / cleanup / is_active
- CubeSandboxProvider: create_sandbox / connect_sandbox / _do_kill_sandbox / is_configured

不依赖真实 CubeSandbox SDK 远程调用——用 Mock 对象验证逻辑分支。
"""
import pytest
import os, sys
from unittest.mock import MagicMock, patch, call

# 先触发完整 src 包初始化（避免循环导入）
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import src.api.main  # noqa: F401 — 触发完整 import 链

from src.domains.runtime.agents.sandbox.backends.cubesandbox import (
    CubeSandboxInstance,
    CubeSandboxExecHandle,
    CubeSandboxProvider,
)
from src.domains.runtime.agents.sandbox.types import CommandResult, Capability, CredentialMode


# ============================================================
# Mock SDK 工具
# ============================================================

class MockCommandResult:
    """模拟 cubesandbox SDK 的 CommandResult。"""
    def __init__(self, exit_code=0, stdout="", stderr=""):
        self.exit_code = exit_code
        self.stdout = stdout
        self.stderr = stderr


class MockSandboxSDK:
    """模拟 cubesandbox.Sandbox 实例——记录所有 commands.run / files.write 调用。"""

    def __init__(self, sandbox_id="sb-test-001", state="RUNNING"):
        self.sandbox_id = sandbox_id
        self._state = state
        self._killed = False
        self._closed = False
        self._timeout_set = None
        self.commands = MagicMock()
        self.files = MagicMock()
        # 默认 commands.run 返回成功空结果
        self.commands.run = MagicMock(return_value=MockCommandResult(0, "", ""))
        self.files.write = MagicMock()
        self.files.read = MagicMock(return_value="")
        self.files.list = MagicMock(return_value=[])

    def get_info(self):
        return {"state": self._state}

    def set_timeout(self, timeout):
        self._timeout_set = timeout

    def kill(self):
        self._killed = True
        self._state = "KILLED"

    def close(self):
        self._closed = True


def make_instance(config=None, template_id="", engine_type="", state_reporter=None,
                  issue_id="issue-1", repo_key="test/repo", user_id=1):
    """构造 CubeSandboxInstance，不触发 __enter__。"""
    cfg = config or {
        "api_url": "https://cube.test",
        "api_key": "test-key",
        "template_id": "tpl-001",
        "timeout": 3600,
        "grape_server_url": "https://grape.test",
        "grape_cli_token": "grape-token",
    }
    return CubeSandboxInstance(
        issue_id=issue_id,
        repo_key=repo_key,
        user_id=user_id,
        config=cfg,
        template_id=template_id,
        state_reporter=state_reporter,
        engine_type=engine_type,
    )


def attach_mock_sandbox(inst, sdk=None):
    """把 MockSandboxSDK 挂到 instance 上（模拟已 connect 状态）。"""
    sdk = sdk or MockSandboxSDK()
    inst._sandbox = sdk
    inst._sandbox_id = sdk.sandbox_id
    return sdk


# ============================================================
# CubeSandboxExecHandle 测试
# ============================================================

class TestCubeSandboxExecHandle:
    """ExecHandle 包装同步 CommandResult。"""

    def test_wait_returns_command_result(self):
        """wait() 返回 CommandResult，含 exit_code/stdout/stderr。"""
        raw = MockCommandResult(exit_code=0, stdout="hello", stderr="")
        handle = CubeSandboxExecHandle(raw)
        result = handle.wait()
        assert isinstance(result, CommandResult)
        assert result.exit_code == 0
        assert result.stdout == "hello"

    def test_wait_with_timeout_param(self):
        """wait(timeout=...) 不报错（同步执行忽略 timeout）。"""
        raw = MockCommandResult(exit_code=0, stdout="ok", stderr="")
        handle = CubeSandboxExecHandle(raw)
        result = handle.wait(timeout=10.0)
        assert result.exit_code == 0

    def test_stream_stdout_empty(self):
        """同步执行无流——stream_stdout 返回空迭代器。"""
        raw = MockCommandResult(exit_code=0, stdout="data", stderr="")
        handle = CubeSandboxExecHandle(raw)
        chunks = list(handle.stream_stdout())
        assert chunks == []

    def test_exit_code_property(self):
        """exit_code 属性直接返回。"""
        raw = MockCommandResult(exit_code=42, stdout="", stderr="")
        handle = CubeSandboxExecHandle(raw)
        assert handle.exit_code == 42

    def test_pid_is_none(self):
        """同步执行无 PID。"""
        raw = MockCommandResult(exit_code=0, stdout="", stderr="")
        handle = CubeSandboxExecHandle(raw)
        assert handle.pid is None

    def test_send_stdin_and_close_noop(self):
        """send_stdin / close_stdin / kill 都是 no-op，不抛异常。"""
        raw = MockCommandResult(exit_code=0, stdout="", stderr="")
        handle = CubeSandboxExecHandle(raw)
        handle.send_stdin("data")
        handle.close_stdin()
        handle.kill()  # 不应抛异常

    def test_handle_stderr_fallback_to_error_attr(self):
        """stderr 缺失时 fallback 到 error 属性。"""
        class RawWithError:
            exit_code = 1
            stdout = ""
            stderr = None
            error = "something went wrong"
        handle = CubeSandboxExecHandle(RawWithError())
        result = handle.wait()
        assert result.stderr == "something went wrong"


# ============================================================
# CubeSandboxInstance 能力声明测试
# ============================================================

class TestCubeSandboxInstanceCapabilities:
    """能力声明 + credential_mode。"""

    def test_supports_pty_true(self):
        """CubeSandbox 声明支持 PTY。"""
        inst = make_instance()
        assert inst.supports_pty() is True

    def test_supports_long_cmd_true(self):
        """CubeSandbox 声明 supports_long_cmd=True（gRPC deadline 适配）。"""
        inst = make_instance()
        assert inst.supports_long_cmd is True

    def test_needs_entry_point_wrapper_false(self):
        """CubeSandbox 预装模板不需要 entry_point wrapper。"""
        inst = make_instance()
        assert inst.needs_entry_point_wrapper is False

    def test_credential_mode_envs(self):
        """CubeSandbox 用 ENVS 凭证模式。"""
        inst = make_instance()
        assert inst.credential_mode == CredentialMode.ENVS

    def test_capabilities_set(self):
        """capabilities 包含 STREAMING_STDOUT + FILE_WRITE + FILE_READ。"""
        inst = make_instance()
        caps = inst.capabilities
        assert Capability.STREAMING_STDOUT in caps
        assert Capability.FILE_WRITE in caps
        assert Capability.FILE_READ in caps

    def test_check_environment_noop(self):
        """check_environment 预装环境——不抛异常。"""
        inst = make_instance()
        inst.check_environment()  # pass, 无异常


# ============================================================
# _do_create / _do_connect / _do_kill / _do_is_running / _do_refresh
# ============================================================

class TestCubeSandboxInstanceLifecycle:
    """生命周期模板方法测试。"""

    @patch("cubesandbox.Sandbox")
    @patch("cubesandbox.Config")
    def test_do_create_calls_sdk(self, mock_config_cls, mock_sandbox_cls):
        """_do_create 调 Sandbox.create 并返回 sandbox_id。"""
        mock_sdk = MockSandboxSDK(sandbox_id="created-123")
        mock_sandbox_cls.create.return_value = mock_sdk
        inst = make_instance(template_id="tpl-xyz")
        sid = inst._do_create()
        assert sid == "created-123"
        assert inst._sandbox is mock_sdk
        mock_sandbox_cls.create.assert_called_once()

    @patch("cubesandbox.Sandbox")
    @patch("cubesandbox.Config")
    def test_do_connect_calls_sdk(self, mock_config_cls, mock_sandbox_cls):
        """_do_connect 调 Sandbox.connect。"""
        mock_sdk = MockSandboxSDK(sandbox_id="existing-456")
        mock_sandbox_cls.connect.return_value = mock_sdk
        inst = make_instance()
        inst._do_connect("existing-456")
        assert inst._sandbox is mock_sdk
        assert inst._sandbox_id == "existing-456"
        mock_sandbox_cls.connect.assert_called_once_with("existing-456", config=mock_config_cls.return_value)

    def test_do_kill_no_sandbox_returns_false(self):
        """_do_kill 未 connect 时返回 False。"""
        inst = make_instance()
        assert inst._do_kill() is False

    def test_do_kill_calls_sdk_kill(self):
        """_do_kill 调 sandbox.kill()。"""
        inst = make_instance()
        sdk = attach_mock_sandbox(inst)
        result = inst._do_kill()
        assert result is True
        assert sdk._killed is True

    def test_do_is_running_true(self):
        """state=RUNNING 时返回 True。"""
        inst = make_instance()
        attach_mock_sandbox(inst, MockSandboxSDK(state="RUNNING"))
        assert inst._do_is_running() is True

    def test_do_is_running_false_when_stopped(self):
        """state=STOPPED 时返回 False。"""
        inst = make_instance()
        attach_mock_sandbox(inst, MockSandboxSDK(state="STOPPED"))
        assert inst._do_is_running() is False

    def test_do_is_running_no_sandbox(self):
        """未 connect 时返回 False。"""
        inst = make_instance()
        assert inst._do_is_running() is False

    def test_do_is_running_exception_returns_false(self):
        """get_info 抛异常时返回 False。"""
        inst = make_instance()
        sdk = attach_mock_sandbox(inst)
        sdk.get_info = MagicMock(side_effect=RuntimeError("network error"))
        assert inst._do_is_running() is False

    def test_do_refresh_sets_timeout(self):
        """_do_refresh 调 set_timeout。"""
        inst = make_instance()
        sdk = attach_mock_sandbox(inst)
        inst._do_refresh()
        assert sdk._timeout_set == 3600

    def test_do_refresh_no_sandbox(self):
        """未 connect 时 _do_refresh 不抛异常。"""
        inst = make_instance()
        inst._do_refresh()  # 不应抛异常


# ============================================================
# _run_long_command 测试
# ============================================================

class TestRunLongCommand:
    """nohup + 轮询模式测试。"""

    def test_long_command_success(self):
        """_run_long_command 完成 → 返回 (exit_code, stdout_tail)。"""
        inst = make_instance()
        sdk = attach_mock_sandbox(inst)

        # 模拟轮询序列: test marker → done, cat exit → 0, tail → "ok"
        poll_results = [
            MockCommandResult(0, "done"),      # marker check
            MockCommandResult(0, "0"),          # cat exit code
            MockCommandResult(0, "build ok"),   # tail log
        ]
        # 前3次 run: chmod, nohup, 然后第一次 poll
        pre_results = [
            MockCommandResult(0, ""),   # chmod
            MockCommandResult(0, "started"),  # nohup
        ]
        sdk.commands.run = MagicMock(side_effect=pre_results + poll_results)

        # patch sleep 避免真实等待
        with patch("time.sleep"):
            ec, stdout = inst._run_long_command("pip install foo", timeout=30, poll_interval=5)
        assert ec == 0
        assert "ok" in stdout

    def test_long_command_unique_filenames(self):
        """每次调用生成不同 uuid 文件名。"""
        inst = make_instance()
        sdk = attach_mock_sandbox(inst)

        # 让所有 run 都返回 done+0，快速完成
        sdk.commands.run = MagicMock(return_value=MockCommandResult(0, "done"))
        with patch("time.sleep"):
            inst._run_long_command("cmd1", timeout=15, poll_interval=5)
            inst._run_long_command("cmd2", timeout=15, poll_interval=5)

        # 检查 files.write 被调两次，且路径不同
        write_calls = sdk.files.write.call_args_list
        assert len(write_calls) >= 2
        script_paths = [c.args[0] for c in write_calls if "/tmp/_long_cmd_" in str(c.args[0])]
        assert len(script_paths) >= 2
        # 两次路径不同
        assert script_paths[0] != script_paths[1]

    def test_long_command_timeout(self):
        """轮询超时 → 返回 (-1, 'timeout')。"""
        inst = make_instance()
        sdk = attach_mock_sandbox(inst)
        # marker 始终不出现
        pre = [MockCommandResult(0, ""), MockCommandResult(0, "started")]
        polls = [MockCommandResult(0, "wait")] * 10
        sdk.commands.run = MagicMock(side_effect=pre + polls)
        with patch("time.sleep"):
            ec, stdout = inst._run_long_command("long cmd", timeout=30, poll_interval=10)
        assert ec == -1
        assert stdout == "timeout"

    def test_long_command_with_envs(self):
        """envs 注入到脚本（files.write 的内容含 export 行）。"""
        inst = make_instance()
        sdk = attach_mock_sandbox(inst)
        sdk.commands.run = MagicMock(return_value=MockCommandResult(0, "done"))
        with patch("time.sleep"):
            inst._run_long_command("echo $TOKEN", timeout=15, poll_interval=5,
                                   envs={"TOKEN": "secret", "PATH": "/bin"})
        # 脚本内容应包含 export 行
        write_content = sdk.files.write.call_args_list[0].args[1]
        assert "export TOKEN=" in write_content
        assert "export PATH=" in write_content


# ============================================================
# run_cmd / run_long_cmd / write_file / read_file / list_files / get_home
# ============================================================

class TestCubeSandboxInstancePrimitives:
    """PlatformAdapter 原语测试。"""

    def test_run_cmd_returns_tuple(self):
        """run_cmd 返回 (exit_code, stdout)。"""
        inst = make_instance()
        sdk = attach_mock_sandbox(inst)
        sdk.commands.run = MagicMock(return_value=MockCommandResult(0, "output", ""))
        ec, stdout = inst.run_cmd("echo hello")
        assert ec == 0
        assert stdout == "output"
        sdk.commands.run.assert_called_once_with("echo hello", envs=None, timeout=60)

    def test_run_long_cmd_delegates(self):
        """run_long_cmd 委托到 _run_long_command。"""
        inst = make_instance()
        sdk = attach_mock_sandbox(inst)
        pre = [MockCommandResult(0, ""), MockCommandResult(0, "started")]
        polls = [MockCommandResult(0, "done"), MockCommandResult(0, "0"), MockCommandResult(0, "log")]
        sdk.commands.run = MagicMock(side_effect=pre + polls)
        with patch("time.sleep"):
            ec, stdout = inst.run_long_cmd("build", timeout=30)
        assert ec == 0

    def test_write_file_calls_sdk(self):
        """write_file 调 sandbox.files.write。"""
        inst = make_instance()
        sdk = attach_mock_sandbox(inst)
        inst.write_file("/tmp/test.txt", "content")
        sdk.files.write.assert_called_once_with("/tmp/test.txt", "content")

    def test_read_file_calls_sdk(self):
        """read_file 调 sandbox.files.read。"""
        inst = make_instance()
        sdk = attach_mock_sandbox(inst)
        sdk.files.read = MagicMock(return_value="file content")
        result = inst.read_file("/tmp/test.txt")
        assert result == "file content"

    def test_read_file_no_sandbox_returns_empty(self):
        """未 connect 时 read_file 返回空字符串。"""
        inst = make_instance()
        assert inst.read_file("/tmp/test.txt") == ""

    def test_list_files_calls_sdk(self):
        """list_files 调 sandbox.files.list。"""
        inst = make_instance()
        sdk = attach_mock_sandbox(inst)
        sdk.files.list = MagicMock(return_value=["a.py", "b.py"])
        result = inst.list_files("/tmp")
        assert result == ["a.py", "b.py"]

    def test_list_files_no_sandbox_returns_empty(self):
        """未 connect 时 list_files 返回空列表。"""
        inst = make_instance()
        assert inst.list_files("/tmp") == []

    def test_get_home_returns_home(self):
        """get_home 返回 $HOME。"""
        inst = make_instance()
        sdk = attach_mock_sandbox(inst)
        sdk.commands.run = MagicMock(return_value=MockCommandResult(0, "/home/user\n", ""))
        assert inst.get_home() == "/home/user"

    def test_get_home_defaults_to_root(self):
        """get_home 空输出时 fallback /root。"""
        inst = make_instance()
        sdk = attach_mock_sandbox(inst)
        sdk.commands.run = MagicMock(return_value=MockCommandResult(0, "", ""))
        assert inst.get_home() == "/root"


# ============================================================
# run_command + ExecHandle 集成
# ============================================================

class TestRunCommandIntegration:
    """run_command 返回 CubeSandboxExecHandle。"""

    def test_run_command_returns_exec_handle(self):
        """run_command 包装结果为 CubeSandboxExecHandle。"""
        inst = make_instance()
        sdk = attach_mock_sandbox(inst)
        sdk.commands.run = MagicMock(return_value=MockCommandResult(0, "result", ""))
        handle = inst.run_command("ls -la")
        assert isinstance(handle, CubeSandboxExecHandle)
        result = handle.wait()
        assert result.exit_code == 0
        assert result.stdout == "result"

    def test_run_command_merges_credentials(self):
        """run_command 合并 _credentials + _init_envs + 调用方 envs。"""
        inst = make_instance()
        sdk = attach_mock_sandbox(inst)
        inst._credentials = {"CRED_KEY": "cred-val"}
        inst._init_envs = {"INIT_VAR": "init-val"}
        sdk.commands.run = MagicMock(return_value=MockCommandResult(0, "", ""))
        inst.run_command("echo test", envs={"CALLER_VAR": "caller-val"})
        # 检查传给 SDK 的 envs 包含所有三个来源
        _, kwargs = sdk.commands.run.call_args
        passed_envs = kwargs.get("envs", {})
        assert passed_envs.get("CRED_KEY") == "cred-val"
        assert passed_envs.get("INIT_VAR") == "init-val"
        assert passed_envs.get("CALLER_VAR") == "caller-val"


# ============================================================
# upload_skill 测试
# ============================================================

class TestUploadSkill:
    """upload_skill 下载并解压 skill zip。"""

    def test_upload_skill_sets_skill_dir(self):
        """upload_skill 设置 _SKILL_DIR。"""
        inst = make_instance()
        sdk = attach_mock_sandbox(inst)
        inst._REPO_DIR = "/home/user/repo"
        sdk.commands.run = MagicMock(return_value=MockCommandResult(0, "", ""))
        inst.upload_skill("https://obs.test/skill.zip")
        assert inst._SKILL_DIR == "/home/user/repo/.skills"
        # 验证命令含 curl + unzip
        run_cmd_str = sdk.commands.run.call_args.args[0]
        assert "curl" in run_cmd_str
        assert "unzip" in run_cmd_str

    def test_upload_skill_no_sandbox_skips(self):
        """未 connect 时 upload_skill 不操作。"""
        inst = make_instance()
        inst.upload_skill("https://obs.test/skill.zip")
        assert not hasattr(inst, "_SKILL_DIR") or inst.skill_dir == ""

    def test_upload_skill_empty_url_skips(self):
        """空 URL 时 upload_skill 不操作。"""
        inst = make_instance()
        attach_mock_sandbox(inst)
        inst.upload_skill("")
        assert not hasattr(inst, "_SKILL_DIR")


# ============================================================
# _configure_grape_cli 测试
# ============================================================

class TestConfigureGrapeCli:
    """grape-cli 配置测试。"""

    def test_configure_grape_cli_runs_commands(self):
        """_configure_grape_cli 调 set-server + set-token + bashrc。"""
        inst = make_instance()
        sdk = attach_mock_sandbox(inst)
        sdk.commands.run = MagicMock(return_value=MockCommandResult(0, "", ""))
        inst._configure_grape_cli()
        # 至少调了 set-server、set-token、bashrc
        assert sdk.commands.run.call_count >= 3
        cmd_strs = [str(c.args[0]) for c in sdk.commands.run.call_args_list]
        assert any("set-server" in s for s in cmd_strs)
        assert any("set-token" in s for s in cmd_strs)
        assert any("bashrc" in s for s in cmd_strs)


# ============================================================
# _clone_repo 测试
# ============================================================

class TestCloneRepo:
    """git clone + GIT_ASKPASS 测试。"""

    def test_clone_repo_success(self):
        """clone 成功 → 设置 _REPO_DIR + _SKILL_DIR。"""
        inst = make_instance()
        sdk = attach_mock_sandbox(inst)
        inst._repo_url = "https://git.test/repo.git"
        inst._bot_token = "bot-token-123"

        # _clone_repo 调 commands.run (chmod + git config) + _run_long_command + dir check
        # _run_long_command: files.write + chmod + nohup + poll
        # 让 poll 第一次就 done
        long_cmd_poll = [
            MockCommandResult(0, "done"),
            MockCommandResult(0, "0"),
            MockCommandResult(0, "clone ok"),
        ]
        pre_long = [
            MockCommandResult(0, ""),       # chmod script
            MockCommandResult(0, "started"), # nohup
        ]
        # 前2个 commands.run: chmod askpass + git config
        pre_cmds = [
            MockCommandResult(0, ""),  # chmod askpass
            MockCommandResult(0, ""),  # git config
            MockCommandResult(0, "/usr/bin/git\nhas"),  # git 探测（新版前置）
        ]
        # 最后1个: dir check（新版检查 .git 存在 → "git"）
        dir_check = MockCommandResult(0, "git")
        # 遗漏-5 修复配套：_run_long_command 成功路径清理 4 个临时文件（rm -f x4）
        rm_cleanups = [MockCommandResult(0, "")] * 4
        sdk.commands.run = MagicMock(side_effect=pre_cmds + pre_long + long_cmd_poll + rm_cleanups + [dir_check])

        with patch("time.sleep"):
            inst._clone_repo()
        assert inst._REPO_DIR == "/home/user/repo"
        assert inst._SKILL_DIR == "/home/user/repo/.skills"

    def test_clone_repo_exit_nonzero_raises(self):
        """clone exit_code != 0 → 抛 RuntimeError。"""
        inst = make_instance()
        sdk = attach_mock_sandbox(inst)
        inst._repo_url = "https://git.test/repo.git"
        inst._bot_token = "bot-token"

        pre_cmds = [
            MockCommandResult(0, ""),  # chmod askpass
            MockCommandResult(0, ""),  # git config
        ]
        pre_long = [
            MockCommandResult(0, ""),       # chmod script
            MockCommandResult(0, "started"), # nohup
        ]
        polls = [
            MockCommandResult(0, "done"),
            MockCommandResult(0, "1"),      # exit code 1
            MockCommandResult(0, "error"),   # tail
        ]
        # 失败路径: rm x4 清理 + dir check(.git 缺失 → 走 dulwich 降级) + dulwich 长命令 + HEAD 检查
        rm_cleanups = [MockCommandResult(0, "")] * 4
        dir_check = MockCommandResult(0, "missing")
        dul_pre = [MockCommandResult(0, ""), MockCommandResult(0, "started")]  # chmod + nohup
        dul_poll = [MockCommandResult(0, "done"), MockCommandResult(0, "1"), MockCommandResult(0, "dulwich error")]
        dul_rm = [MockCommandResult(0, "")] * 4
        head_check = MockCommandResult(0, "fail")
        sdk.commands.run = MagicMock(side_effect=pre_cmds + pre_long + polls + rm_cleanups + [dir_check] + dul_pre + dul_poll + dul_rm + [head_check])

        with patch("time.sleep"):
            with pytest.raises(RuntimeError, match="git clone failed"):
                inst._clone_repo()

    def test_clone_repo_exit_zero_but_dir_missing_raises(self):
        """exit_code=0 但目录不存在 → 抛 RuntimeError。"""
        inst = make_instance()
        sdk = attach_mock_sandbox(inst)
        inst._repo_url = "https://git.test/repo.git"
        inst._bot_token = "bot-token"

        pre_cmds = [MockCommandResult(0, ""), MockCommandResult(0, "")]
        pre_long = [MockCommandResult(0, ""), MockCommandResult(0, "started")]
        polls = [
            MockCommandResult(0, "done"),
            MockCommandResult(0, "0"),
            MockCommandResult(0, ""),
        ]
        # exit=0 但 .git 缺失（clone 假成功）→ 走 dulwich 降级；dulwich 也失败 → RuntimeError
        dir_check = MockCommandResult(0, "missing")
        # 遗漏-5 修复配套：_run_long_command 成功路径清理 4 个临时文件（rm -f x4）
        rm_cleanups = [MockCommandResult(0, "")] * 4
        dul_pre = [MockCommandResult(0, ""), MockCommandResult(0, "started")]  # chmod + nohup
        dul_poll = [MockCommandResult(0, "done"), MockCommandResult(0, "1"), MockCommandResult(0, "dulwich err")]
        dul_rm = [MockCommandResult(0, "")] * 4
        head_check = MockCommandResult(0, "fail")
        sdk.commands.run = MagicMock(side_effect=pre_cmds + pre_long + polls + rm_cleanups + [dir_check] + dul_pre + dul_poll + dul_rm + [head_check])

        with patch("time.sleep"):
            with pytest.raises(RuntimeError, match="git clone failed"):
                inst._clone_repo()

    def test_clone_repo_no_sandbox_skips(self):
        """未 connect 时 _clone_repo 不操作。"""
        inst = make_instance()
        inst._repo_url = "https://git.test/repo.git"
        inst._bot_token = "token"
        inst._clone_repo()  # 不应抛异常
        assert not hasattr(inst, "_REPO_DIR")


# ============================================================
# repo_dir / skill_dir / cleanup / is_active
# ============================================================

class TestCubeSandboxInstanceProperties:
    """属性 + cleanup 测试。"""

    def test_repo_dir_empty_before_clone(self):
        """未 clone 时 repo_dir 返回空字符串。"""
        inst = make_instance()
        assert inst.repo_dir == ""

    def test_repo_dir_after_clone(self):
        """clone 后 repo_dir 返回路径。"""
        inst = make_instance()
        inst._REPO_DIR = "/home/user/repo"
        assert inst.repo_dir == "/home/user/repo"

    def test_skill_dir_empty_before_upload(self):
        """未 upload_skill 时 skill_dir 返回空字符串。"""
        inst = make_instance()
        assert inst.skill_dir == ""

    def test_skill_dir_after_upload(self):
        """upload_skill 后 skill_dir 返回路径。"""
        inst = make_instance()
        inst._SKILL_DIR = "/home/user/repo/.skills"
        assert inst.skill_dir == "/home/user/repo/.skills"

    def test_cleanup_closes_sdk_connection(self):
        """cleanup 调 sandbox.close() 并清空 _sandbox。"""
        inst = make_instance()
        sdk = attach_mock_sandbox(inst)
        inst.cleanup()
        assert sdk._closed is True
        assert inst._sandbox is None

    def test_cleanup_no_sandbox_noop(self):
        """未 connect 时 cleanup 不抛异常。"""
        inst = make_instance()
        inst.cleanup()  # 不应抛异常

    def test_is_active_true_when_running(self):
        """sandbox 存在且 state=RUNNING → is_active True。"""
        inst = make_instance()
        attach_mock_sandbox(inst, MockSandboxSDK(state="RUNNING"))
        assert inst.is_active() is True

    def test_is_active_false_when_stopped(self):
        """state=STOPPED → is_active False。"""
        inst = make_instance()
        attach_mock_sandbox(inst, MockSandboxSDK(state="STOPPED"))
        assert inst.is_active() is False

    def test_is_active_false_no_sandbox(self):
        """未 connect → is_active False。"""
        inst = make_instance()
        assert inst.is_active() is False


# ============================================================
# CubeSandboxProvider 测试
# ============================================================

class TestCubeSandboxProvider:
    """Provider 创建/连接/销毁测试。"""

    def test_provider_is_configured(self):
        """有 api_url → is_configured True。"""
        provider = CubeSandboxProvider(config={"api_url": "https://cube.test", "api_key": "key"})
        assert provider.is_configured() is True

    def test_provider_not_configured(self):
        """无 api_url → is_configured False。"""
        provider = CubeSandboxProvider(config={"other": "val"})
        assert provider.is_configured() is False

    def test_provider_create_sandbox(self):
        """create_sandbox 返回 CubeSandboxInstance。"""
        provider = CubeSandboxProvider(config={
            "api_url": "https://cube.test",
            "api_key": "key",
            "template_id": "tpl-001",
        })
        inst = provider.create_sandbox(
            task_id="issue-1",
            repo_key="test/repo",
            user_id=1,
            engine_type="hermes",
            template_id="tpl-custom",
        )
        assert isinstance(inst, CubeSandboxInstance)
        assert inst._template_id == "tpl-custom"
        assert inst.issue_id == "issue-1"

    @patch("cubesandbox.Sandbox")
    @patch("cubesandbox.Config")
    def test_provider_connect_sandbox(self, mock_config_cls, mock_sandbox_cls):
        """connect_sandbox 恢复连接。"""
        mock_sdk = MockSandboxSDK(sandbox_id="existing-789")
        mock_sandbox_cls.connect.return_value = mock_sdk
        provider = CubeSandboxProvider(config={"api_url": "https://cube.test", "api_key": "key"})
        inst = provider.connect_sandbox("existing-789", issue_id="issue-2")
        assert isinstance(inst, CubeSandboxInstance)
        assert inst._sandbox_id == "existing-789"
        assert inst._sandbox is mock_sdk

    @patch("cubesandbox.Sandbox")
    @patch("cubesandbox.Config")
    def test_provider_kill_sandbox_running(self, mock_config_cls, mock_sandbox_cls):
        """kill_sandbox 对 RUNNING 沙箱调 kill()。"""
        mock_sdk = MockSandboxSDK(sandbox_id="sb-kill", state="RUNNING")
        mock_sandbox_cls.connect.return_value = mock_sdk
        provider = CubeSandboxProvider(config={"api_url": "https://cube.test", "api_key": "key"})
        result = provider.kill_sandbox("sb-kill")
        assert result is True
        assert mock_sdk._killed is True

    @patch("cubesandbox.Sandbox")
    @patch("cubesandbox.Config")
    def test_provider_kill_sandbox_already_stopped(self, mock_config_cls, mock_sandbox_cls):
        """kill_sandbox 对 STOPPED 沙箱直接返回 True。"""
        mock_sdk = MockSandboxSDK(sandbox_id="sb-stopped", state="STOPPED")
        mock_sandbox_cls.connect.return_value = mock_sdk
        provider = CubeSandboxProvider(config={"api_url": "https://cube.test", "api_key": "key"})
        result = provider.kill_sandbox("sb-stopped")
        assert result is True
        assert mock_sdk._killed is False  # 已停止不再 kill
