"""DevBox 云沙箱实现——华为云 DevBox SDK 适配。

继承 CloudSandboxBase / CloudProviderBase，实现 _do_* 模板方法。
SDK 路径：<devbox-sdk-dir>/src/devbox/

DevBox SDK 核心对象：
- DevBox(client) → Sandboxes → Sandbox
- Sandbox.commands.run(cmd, envs, stdin, on_stdout, on_stderr, background) → CommandResult | CommandHandle
- Sandbox.files.read/write/list
- Sandbox.kill() / refresh(duration) / is_running() / set_timeout()
- Sandbox.__enter__/__exit__（SDK 的 __exit__ 会 kill，我们不用它——用自己的 __exit__ 调 refresh）
"""
from __future__ import annotations

import logging
import os
import threading
import time
from typing import Any, Callable, Iterator

from ..types import (
    ExecutionMode, Capability, CredentialMode, CommandResult,
    ExecHandle, HealthStatus, UnsupportedCapability,
)
from .cloud_base import CloudSandboxBase, CloudProviderBase
# [USP搬迁] Transport 异常族随 api.py 随迁——Transport 类 raise 用
from .api import TransportError, TransportTimeout, TransportClosed  # noqa: F401

log = logging.getLogger(__name__)


class DevBoxSandbox(CloudSandboxBase):
    """DevBox 云沙箱实例——适配华为云 DevBox SDK。

    credential_mode = ENVS（一期）：凭证通过 envs 注入，agent 可见。
    capabilities：STREAMING_STDOUT + STREAMING_STDERR + INTERACTIVE_STDIN + BACKGROUND。
    """

    execution_mode = ExecutionMode.CLOUD_SDK

    # 沙箱内路径常量（假设 default 模板固定路径）
    # TODO: 多镜像时下沉到 provider 配置字段（configs/devbox-config.json 加 repo_dir/skill_dir）
    _REPO_DIR = "/home/devbox/repo"           # git clone 目标 + agent cwd
    _SKILL_DIR = "/home/devbox/repo/.skills"  # skill 解压目录 = _REPO_DIR + "/.skills"
    _TMP_SKILL_ZIP = "/tmp/skill.zip"         # 临时下载文件

    def __init__(
        self,
        *,
        issue_id: str,
        repo_key: str,
        user_id: int = 0,
        devbox_client: Any = None,  # devbox.DevBox 实例
        template: str = "default",
        timeout: int = 3600,
        envs: dict[str, str] | None = None,
        repo_url: str = "",
        bot_token: str = "",
        work_branch: str = "",
        grape_server_url: str = "",
        grape_cli_token: str = "",
        engine_type: str = "",
        state_reporter=None,
    ):
        super().__init__(
            issue_id=issue_id,
            repo_key=repo_key,
            provider_name="devbox",
            platform="devbox",
            user_id=user_id,
            engine_type=engine_type,
            state_reporter=state_reporter,
        )
        self._devbox = devbox_client
        self._template = template
        self._timeout = timeout
        self._init_envs = envs or {}
        self._sandbox: Any = None  # devbox.Sandbox 实例
        self._repo_url = repo_url
        self._bot_token = bot_token
        self._work_branch = work_branch
        self._grape_server_url = grape_server_url
        self._grape_cli_token = grape_cli_token

    # ---- 模板方法实现 ----

    def _do_create(self) -> str:
        """DevBox SDK: Sandboxes.create() → sandbox_id。"""
        sandboxes = self._devbox.sandboxes
        sb = sandboxes.create(
            template=self._template,
            timeout=self._timeout,
            envs=self._init_envs,
        )
        self._sandbox = sb
        return sb.sandbox_id

    def _do_connect(self, sandbox_id: str) -> None:
        """DevBox SDK: Sandboxes.connect(sandbox_id)。"""
        self._sandbox = self._devbox.sandboxes.connect(sandbox_id)

    def _do_kill(self) -> bool:
        """DevBox SDK: Sandbox.kill()。"""
        if self._sandbox:
            return self._sandbox.kill()
        return False

    def _do_refresh(self) -> None:
        """DevBox SDK: Sandbox.refresh(duration)。续期 300s（5 分钟），不用 self._timeout(3600)。"""
        if self._sandbox:
            self._sandbox.refresh(duration=300)

    def _do_is_running(self) -> bool:
        """DevBox SDK: Sandbox.is_running()。"""
        if self._sandbox:
            return self._sandbox.is_running()
        return False

    def _do_reconnect(self) -> None:
        """DevBox SDK: 重新 connect。"""
        if self._sandbox_id:
            self._sandbox = self._devbox.sandboxes.connect(self._sandbox_id)

    # ===== PlatformAdapter 能力声明 =====

    @property
    def supports_long_cmd(self) -> bool:
        """DevBox 无 gRPC deadline 限制——直接 run_cmd 即可。"""
        return False

    @property
    def needs_entry_point_wrapper(self) -> bool:
        """DevBox pip --target 不生成 entry_points——需要手动创建 wrapper。"""
        return True

    # ===== PlatformAdapter 原语 =====

    def check_environment(self) -> None:
        """DevBox 环境检测——bash + DNS 修复 + 公网验证 + Python 检测 + 磁盘空间。

        沙箱默认 nameserver 无效，必须修复 DNS 才能公网访问。
        hermes-agent + grape-cli 装到 /tmp/pylibs（tmpfs），需检查 /tmp 空间。

        hwtunnel 网关在流式连接空闲时会主动断开 HTTP 流（没有 exit 事件），
        SDK 同步 run(background=False) 会抛 ProtocolError。
        用 _safe_run 包装：ProtocolError 时通过 connect(pid) 重连获取结果。
        """
        if not self._sandbox:
            return
        cmds = self._sandbox.commands

        def _safe_run(cmd: str, timeout: float = 15, check: bool = False):
            """SDK cmds.run 包装——background=True + 流断重连。

            hwtunnel 网关在流式连接空闲时断开 HTTP 流（没有 exit 事件），
            SDK 同步 run(background=False) 直接抛 ProtocolError。
            background=True 启动后，catch ProtocolError 清除 _events 强制 wait 重连。
            """
            handle = cmds.run(cmd, background=True, timeout=timeout)
            try:
                return handle.wait(check=check)
            except Exception as e:
                if "stream ended" not in str(e).lower():
                    raise
                log.debug(f"[devbox] _safe_run stream ended, reconnecting pid={handle.pid}")
                if hasattr(handle, '_events'):
                    handle._events = None  # 遗漏-1 修复：写入前 hasattr 检查
                return handle.wait(check=check)

        # bash 检测
        _bash_check = _safe_run("test -x /bin/bash && echo ok || echo missing", timeout=5)
        if "ok" not in (getattr(_bash_check, 'stdout', '') or ""):
            log.warning(f"[devbox] /bin/bash missing or not executable, creating wrapper")
            try:
                self._sandbox.files.write("/tmp/bash_wrapper", "#!/bin/sh\nexec /bin/sh \"$@\"\n")
                _safe_run("cp /tmp/bash_wrapper /bin/bash && chmod +x /bin/bash", timeout=5)
            except Exception as _be:
                log.warning(f"[devbox] create /bin/bash wrapper failed: {_be}")

        # DNS 修复——国内 DNS 优先（阿里 + 腾讯），8.8.8.8 备用
        _safe_run(
            'rm -f /etc/resolv.conf && '
            'echo "nameserver 223.5.5.5" > /etc/resolv.conf && '
            'echo "nameserver 119.29.29.29" >> /etc/resolv.conf && '
            'echo "nameserver 8.8.8.8" >> /etc/resolv.conf',
            timeout=10,
        )
        # 公网验证——用国内可达的镜像站（pypi.org 可能不可达）
        r = _safe_run(
            "curl -s --connect-timeout 5 -o /dev/null -w '%{http_code}' https://pypi.tuna.tsinghua.edu.cn/simple/ ",
            timeout=15,
        )
        if "200" not in (getattr(r, 'stdout', '') or ""):
            raise RuntimeError(f"devbox sandbox public network unreachable: {getattr(r, 'stdout', '')}")
        log.info(f"[devbox] DNS fixed, public network OK for {self._sandbox_id}")

        # Python 检测
        r = _safe_run("which python3 2>/dev/null || which python 2>/dev/null", timeout=5)
        _py = (getattr(r, 'stdout', '') or '').strip() or 'python3'
        r = _safe_run(f"{_py} --version 2>&1", timeout=5)
        _py_ver = (getattr(r, 'stdout', '') or getattr(r, 'stderr', '') or '').strip()
        if not _py_ver:
            raise RuntimeError("Python not found in devbox sandbox (tried python3/python)")
        log.info(f"[devbox] Python detected: {_py} ({_py_ver})")

        # 磁盘空间
        r = _safe_run("df -m --output=avail /tmp | tail -1", timeout=5)
        _avail_mb = 0
        try:
            _avail_mb = int((getattr(r, 'stdout', '') or '0').strip())
        except Exception:
            pass
        if _avail_mb < 300:
            raise RuntimeError(
                f"devbox sandbox disk space insufficient: {_avail_mb}MB available on /tmp, "
                f"need ~300MB for hermes-agent + grape-cli. "
                f"Check sandbox disk_size_mb config or pre-install agents in image."
            )
        log.info(f"[devbox] disk space OK: {_avail_mb}MB available on /tmp")

    def run_cmd(self, cmd: str, timeout: int = 60, envs: dict | None = None) -> tuple[int, str]:
        """执行命令，返回 (exit_code, stdout)。

        hwtunnel 网关在流式连接空闲时断开 HTTP 流（没有 exit 事件），
        SDK 同步 run(background=False) 和 handle.wait() 都会抛 ProtocolError。
        修复：background=True 启动，catch ProtocolError 后用 commands.connect(pid)
        重连（_connect_events 带 keepalive header），再 wait 获取结果。
        """
        handle = self._sandbox.commands.run(cmd, background=True, timeout=timeout, envs=envs or {})
        try:
            r = handle.wait(check=False)
            return (getattr(r, "exit_code", -1), getattr(r, "stdout", "") or "")
        except Exception as e:
            if "stream ended" not in str(e).lower():
                raise
            # 流断——重连获取结果（命令可能已完成，只是流断了）
            log.debug(f"[devbox] run_cmd stream ended, reconnecting pid={handle.pid}")
            if hasattr(handle, '_events'):
                handle._events = None  # 遗漏-1 修复：清除初始流，强制 wait 走 _connect_events 重连
            try:
                r = handle.wait(check=False)
                return (getattr(r, "exit_code", -1), getattr(r, "stdout", "") or "")
            except Exception:
                # 重连也失败——返回 -1 不阻断（_install_grape_cli 等有 try/except 包着）
                log.warning(f"[devbox] run_cmd reconnect failed for pid={handle.pid}")
                return (-1, "")

    # run_long_cmd 用基类 fallback（传 timeout 给 run_cmd）

    def write_file(self, path: str, content: str | bytes) -> None:
        self._sandbox.files.write(path, content)

    def get_home(self) -> str:
        r = self._sandbox.commands.run("echo $HOME", timeout=5, background=True, check=False).wait(check=False)
        return getattr(r, "stdout", "").strip() or "/root"

    def upload_skill(self, obs_url: str) -> None:
        """下载并解压 skill zip 到沙箱内 repo/.skills 目录。

        通过 OBS 预签名 URL 下载（和 worker 侧 skill_obs_url 机制一致）。
        skill 目录 = _REPO_DIR/.skills（和 worker 侧 os.path.join(cwd, ".skills") 一致）。
        """
        if not self._sandbox or not obs_url:
            return
        import shlex as _shlex
        ec, out = self.run_cmd(
            f'curl -sSL {_shlex.quote(obs_url)} -o {self._TMP_SKILL_ZIP} && '
            f'mkdir -p {self._SKILL_DIR} && '
            f'unzip -q {self._TMP_SKILL_ZIP} -d {self._SKILL_DIR} && rm {self._TMP_SKILL_ZIP}',
            timeout=60,
        )
        if ec != 0:
            raise RuntimeError(f"upload_skill failed (exit={ec}): {out[:200]}")
        log.info(f"[devbox] skill zip downloaded for {self._sandbox_id}")

    def _configure_grape_cli(self) -> None:
        """配置 grape-cli server URL + token。可独立调用（参数由 _run_cloud_sandbox 补充后）。

        check=False——grape-cli 安装可能失败（pip install 网络/依赖问题），
        配置失败不阻断 _init_sandbox（引擎不依赖 grape-cli）。

        方案 D：grape wrapper 在 /usr/local/bin（_install_grape_cli 优先写此处），
        commands.run 用 /bin/bash -l -c（login shell），/usr/local/bin 在默认 PATH 中。
        方案 A：幂等追加 PATH 到 ~/.bash_profile + ~/.profile（belt-and-suspenders）。
        """
        if not self._sandbox or not self._grape_server_url:
            return
        import shlex as _shlex
        # P1-6 修复：先写 PATH export 到 rc 文件，再执行 grape 命令
        # （grape 可能装在 /tmp/pylibs/bin，PATH export 前不在搜索路径中）
        _path_export = "export PATH=$PATH:/tmp/pylibs/bin"
        for _rc_file in ("~/.bashrc", "~/.bash_profile", "~/.profile"):
            self.run_cmd(
                f"grep -q '/tmp/pylibs/bin' {_rc_file} 2>/dev/null || echo '{_path_export}' >> {_rc_file}",
                timeout=5,
            )
        # P1-1 修复：走 run_cmd（background=True + 重连），不走 cmds.run
        self.run_cmd(
            f"grape config set-server {_shlex.quote(self._grape_server_url)}",
            timeout=10,
        )
        if self._grape_cli_token:
            # grape config set-token 通过环境变量传 token（不落命令行）
            _ec, _ = self.run_cmd("grape config set-token $GRAPE_TOKEN",
                                  envs={"GRAPE_TOKEN": self._grape_cli_token}, timeout=10)
            if _ec != 0:
                log.warning(f"[devbox] grape config set-token failed exit={_ec}")

        log.info(f"[devbox] grape-cli configured for {self._sandbox_id}")

    def _clone_repo(self) -> None:
        """git clone 仓库 + 配置凭据。可独立调用（参数由 _run_cloud_sandbox 补充后）。

        使用 GIT_ASKPASS 脚本注入 token——和 CubeSandbox 完全一致。
        token 不出现在命令行/进程列表，只存在于环境变量 + 脚本文件中。

        BUG-5 修复：添加幂等检查——已 clone 则跳过（A2A 复用/重试场景）。
        """
        if not self._sandbox or not self._repo_url or not self._bot_token:
            return
        import shlex as _shlex
        # 幂等检查——已 clone 则跳过
        _ec, _out = self.run_cmd(f"test -d {self._REPO_DIR}/.git && echo exists", timeout=5)
        if "exists" in _out:
            log.info(f"[devbox] repo already cloned at {self._REPO_DIR}, skip")
            return
        # 写 GIT_ASKPASS 脚本——git 调用时从环境变量读 token
        _askpass_script = '#!/bin/sh\ncase "$1" in\nUsername*) echo "bot";; \nPassword*) echo "$GIT_BOT_TOKEN";; \nesac\n'
        self._sandbox.files.write("/tmp/_git_askpass.sh", _askpass_script)
        # P1-1 修复：走 run_cmd（background=True + 重连），不走 cmds.run
        self.run_cmd("chmod +x /tmp/_git_askpass.sh", timeout=5)
        self.run_cmd(
            'git config --global user.name "Grape Bot" && '
            'git config --global user.email "bot@grape.local"',
            timeout=10,
        )
        # clone 时通过 GIT_ASKPASS + env 注入 token（不落命令行）
        branch_arg = f"-b {_shlex.quote(self._work_branch)}" if self._work_branch else ""
        _clone_ec, _clone_out = self.run_cmd(
            f"GIT_ASKPASS=/tmp/_git_askpass.sh git clone --depth 1 {branch_arg} {_shlex.quote(self._repo_url)} {self._REPO_DIR}",
            timeout=120,
            envs={"GIT_BOT_TOKEN": self._bot_token},
        )
        if _clone_ec != 0:
            log.error(f"[devbox] git clone failed exit={_clone_ec} output={_clone_out[:200]}")
            raise RuntimeError(f"git clone failed (exit={_clone_ec}): {_clone_out[:200]}")
        log.info(f"[devbox] repo cloned for {self._sandbox_id}")

    # _apply_engine_config 已移入 engine_strategies.py 的 HermesConfigurator / OpenCodeConfigurator

    # ---- 能力声明 ----

    @property
    def capabilities(self) -> set[Capability]:
        """DevBox 支持：流式 stdout/stderr 分离 + stdin 管道 + 后台执行。"""
        return {
            Capability.STREAMING_STDOUT,
            Capability.STREAMING_STDERR,
            Capability.INTERACTIVE_STDIN,
            Capability.BACKGROUND,
        }

    @property
    def credential_mode(self) -> CredentialMode:
        return CredentialMode.ENVS

    # ---- 命令执行 ----

    def _do_run_command(self, cmd: str, **kwargs) -> ExecHandle:
        """DevBox SDK: commands.run()，返回 DevBoxExecHandle。"""
        envs = kwargs.get("envs") or {}
        cwd = kwargs.get("cwd")
        stdin = kwargs.get("stdin", False)
        timeout = kwargs.get("timeout")
        on_stdout = kwargs.get("on_stdout")
        on_stderr = kwargs.get("on_stderr")
        background = kwargs.get("background", False)

        if background:
            # background=True → 返回 CommandHandle
            # SDK 禁止 background + on_stdout/on_stderr 同时传——callbacks 在 handle.wait() 时传
            handle = self._sandbox.commands.run(
                cmd,
                background=True,
                envs=envs,
                cwd=cwd,
                stdin=stdin,
                timeout=timeout,
            )
            return DevBoxExecHandle(handle, on_stdout=on_stdout, on_stderr=on_stderr)
        else:
            # background=False → 同步执行，包装成 ExecHandle
            result = self._sandbox.commands.run(
                cmd,
                background=False,
                envs=envs,
                cwd=cwd,
                stdin=stdin,
                timeout=timeout,
                on_stdout=on_stdout,
                on_stderr=on_stderr,
            )
            return DevBoxExecHandle.from_result(result)

    # ---- stdin ----

    def send_stdin(self, data: str | bytes) -> None:
        if self._sandbox and hasattr(self._sandbox.commands, 'send_stdin'):
            self._sandbox.commands.send_stdin(data)

    def close_stdin(self) -> None:
        if self._sandbox and hasattr(self._sandbox.commands, 'close_stdin'):
            self._sandbox.commands.close_stdin()

    # ---- 文件操作 ----
    # write_file 由 PlatformAdapter 原语提供（上方）

    def read_file(self, path: str) -> str:
        if self._sandbox:
            return self._sandbox.files.read(path)
        return ""

    def read_file_bytes(self, path: str) -> bytes:
        """读取沙箱文件原始字节——走 SDK files.read_bytes (HTTP GET)，零进程创建。"""
        if self._sandbox:
            return self._sandbox.files.read_bytes(path)
        return b""

    def file_exists(self, path: str) -> bool:
        """判断沙箱文件是否存在——走 SDK files.exists (HTTP)，零进程创建。"""
        if self._sandbox:
            return self._sandbox.files.exists(path)
        return False

    def list_files(self, path: str) -> list:
        if self._sandbox:
            return list(self._sandbox.files.list(path))
        return []

    # ---- cleanup ----

    def cleanup(self) -> None:
        """关闭 DevBox SDK 连接（不 kill 远端沙箱）。"""
        if self._sandbox:
            try:
                self._sandbox.close()
            except Exception:
                pass
            self._sandbox = None

    # ---- 仓库配置 override ----
    # P2-5 说明：create_sandbox 不传 repo_url/bot_token/work_branch 是设计如此——
    # repo 信息在 _run_cloud_sandbox 的 configure_repo 调用时才设置（executor.py:1220），
    # 之后 _clone_repo 在 configure_repo 内部触发。create_sandbox 阶段沙箱刚创建，
    # repo 信息尚未可用。

    def configure_repo(self, *, repo_url, bot_token, work_branch):
        super().configure_repo(
            repo_url=repo_url, bot_token=bot_token,
            work_branch=work_branch,
        )
        # grape-cli 配置在 _init_sandbox step5 已完成（__enter__ 阶段），此处不重复调用
        if self._sandbox and self._repo_url and self._bot_token:
            self._clone_repo()

    @property
    def repo_dir(self):
        return self._REPO_DIR

    @property
    def skill_dir(self):
        return self._SKILL_DIR


class DevBoxExecHandle:
    """DevBox 执行句柄——包装 SDK 的 CommandHandle 或 CommandResult。

    background=True 时包装 CommandHandle（有 send_stdin/wait/kill + 事件流）。
    background=False 时包装已完成的结果（exit_code 已填充）。
    """

    def __init__(self, handle: Any = None, result: CommandResult | None = None,
                 on_stdout=None, on_stderr=None):
        self._handle = handle  # devbox.CommandHandle
        self._result = result  # 已完成的结果
        self._stdout_buffer: list[str] = []
        self._stderr_buffer: list[str] = []
        self._waited = False
        self._on_stdout = on_stdout  # 外部传入的回调（background 模式下 wait 时传给 SDK）
        self._on_stderr = on_stderr

    @classmethod
    def from_result(cls, result: Any) -> "DevBoxExecHandle":
        """从 SDK CommandResult 创建已完成句柄。"""
        cr = CommandResult(
            exit_code=getattr(result, 'exit_code', -1),
            stdout=getattr(result, 'stdout', ''),
            stderr=getattr(result, 'stderr', ''),
        )
        return cls(result=cr)

    def wait(self, timeout: float | None = None) -> CommandResult:
        if self._result:
            return self._result
        if self._handle:
            # SDK wait() 遍历事件流，通过回调收集 stdout/stderr
            # 优先用外部传入的回调（background 模式下 _do_run_command 传入），否则用 buffer 回调
            _stdout_cb = self._on_stdout
            _stderr_cb = self._on_stderr
            if _stdout_cb is None:
                def _stdout_cb(data: str) -> None:
                    self._stdout_buffer.append(data)
            if _stderr_cb is None:
                def _stderr_cb(data: str) -> None:
                    self._stderr_buffer.append(data)

            result = self._handle.wait(on_stdout=_stdout_cb, on_stderr=_stderr_cb, check=False, timeout=timeout)
            self._result = CommandResult(
                exit_code=getattr(result, 'exit_code', -1),
                stdout=getattr(result, 'stdout', '') or "".join(self._stdout_buffer),
                stderr=getattr(result, 'stderr', '') or "".join(self._stderr_buffer),
            )
            self._waited = True
            return self._result
        return CommandResult(exit_code=-1, stdout="", stderr="no handle")

    def stream_stdout(self) -> Iterator[str]:
        """流式读 stdout——从 SDK 事件流实时 yield 数据。

        background=True 时遍历 CommandHandle 事件流，
        每个 stdout 事件立即 yield，不等进程结束。
        background=False 时 yield 已完成的 stdout。

        重连机制：_start 创建的初始事件流没有 Keepalive-Ping header，
        DevBox 网关在空闲时（agent 冷启动无输出）会断开连接。
        断开后通过 _connect_events(pid, timeout) 重连（带 keepalive header）。
        """
        if self._result:
            # 已完成——直接 yield 全部 stdout
            if self._result.stdout:
                yield self._result.stdout
            return

        if not self._handle:
            return

        # 从 SDK CommandHandle 事件流实时读取
        # DevBox SDK 的 _events 是 Iterator[Mapping[str, Any]]
        # 每个事件有 "type" 字段：stdout/stderr/exit
        events = getattr(self._handle, '_events', None)
        if events is None:
            # 事件流已消费或需要 reconnect
            commands = getattr(self._handle, '_commands', None)
            pid = getattr(self._handle, 'pid', 0)
            reconnect_timeout = getattr(self._handle, '_reconnect_timeout', 60)
            if commands and pid:
                events = commands._connect_events(pid, reconnect_timeout)

        if events is None:
            # 无法获取事件流——退化为 wait() 后一次性返回
            result = self.wait()
            if result.stdout:
                yield result.stdout
            return

        # 事件流遍历 + 断线重连
        # _start 创建的初始事件流没有 keepalive header，网关空闲超时会断开
        # 重连用 _connect_events（带 Keepalive-Ping-Interval: 50）
        _MAX_RECONNECTS = 5
        _reconnect_count = 0

        while True:
            try:
                for event in events:
                    evt_type = event.get("type", "")
                    if evt_type == "stdout":
                        data = event.get("data", "")
                        if data:
                            self._stdout_buffer.append(data)
                            yield data
                    elif evt_type == "stderr":
                        data = event.get("data", "")
                        if data:
                            self._stderr_buffer.append(data)
                    elif evt_type in ("exit", "end"):
                        # 进程结束——收集 exit_code + 最终结果
                        _exit_code = event.get("exit_code", event.get("code", -1))
                        self._event_exit_code = _exit_code
                        self._result = CommandResult(
                            exit_code=_exit_code,
                            stdout="".join(self._stdout_buffer),
                            stderr="".join(self._stderr_buffer),
                        )
                        # 事件流正常结束——清除 _events 避免后续重复 close
                        if hasattr(self._handle, '_events'):
                            self._handle._events = None  # 遗漏-1 修复
                        return
                # 事件流遍历完但没收到 exit 事件——可能进程仍在运行
                # 流被网关断开，尝试重连
            except Exception as e:
                log.debug(f"[devbox] stream_stdout event iteration interrupted: {e}")

            # 到这里说明事件流断了但没收到 exit 事件
            # 清除 _events 引用，尝试重连
            if hasattr(self._handle, '_events'):
                self._handle._events = None  # 遗漏-1 修复

            if self._result is not None:
                # 已有结果（exit 事件已处理）
                return

            if _reconnect_count >= _MAX_RECONNECTS:
                log.warning(f"[devbox] stream_stdout exhausted {_MAX_RECONNECTS} reconnects, giving up")
                return

            _reconnect_count += 1
            commands = getattr(self._handle, '_commands', None)
            pid = getattr(self._handle, 'pid', 0)
            reconnect_timeout = getattr(self._handle, '_reconnect_timeout', 60)
            if not commands or not pid:
                return

            log.debug(f"[devbox] stream_stdout reconnecting (attempt {_reconnect_count}/{_MAX_RECONNECTS}) pid={pid}")
            try:
                events = commands._connect_events(pid, reconnect_timeout)
            except Exception as e:
                log.warning(f"[devbox] stream_stdout reconnect failed: {e}")
                return

    def send_stdin(self, data: str | bytes) -> None:
        if self._handle:
            self._handle.send_stdin(data)

    def close_stdin(self) -> None:
        if self._handle:
            self._handle.close_stdin()

    def kill(self) -> None:
        if self._handle:
            self._handle.kill()

    @property
    def pid(self) -> int | None:
        if self._handle:
            return getattr(self._handle, 'pid', None)
        return None

    @property
    def exit_code(self) -> int | None:
        if self._result:
            return self._result.exit_code
        if self._handle:
            return getattr(self._handle, 'exit_code', None)
        return None


class DevBoxProvider(CloudProviderBase):
    """DevBox 云沙箱 Provider——适配华为云 DevBox 平台。

    配置来源：configs/devbox-config.json
    认证：API Key（Manager API）
    """

    name = "devbox"
    execution_mode = ExecutionMode.CLOUD_SDK

    def __init__(
        self,
        *,
        audit=None,
        credential=None,
        alert_engine=None,
        config: dict | None = None,
    ):
        super().__init__(audit=audit, credential=credential, alert_engine=alert_engine)
        self._config = config or self._load_config()
        # v3 R15：provider_name 从 config 注入不硬编码
        self.provider_name = self._config.get("name", "devbox") or "devbox"
        self._devbox_client: Any = None
        self._client_lock = threading.Lock()

    def _load_config(self) -> dict:
        """从 configs/devbox-config.json 加载配置。"""
        import json
        config_path = os.path.join(
            os.environ.get("GRAPE_CONFIG_DIR", "configs"),
            "devbox-config.json",
        )
        try:
            with open(config_path, "r") as f:
                return json.load(f)
        except FileNotFoundError:
            log.warning(f"[devbox] config not found: {config_path}")
            return {}
        except Exception as e:
            log.error(f"[devbox] config load error: {e}")
            return {}

    def is_configured(self) -> bool:
        """检查 API Key + API URL 是否配置。"""
        return bool(self._config.get("api_key") and self._config.get("api_url"))

    def _do_health_check(self) -> bool:
        """轻量 SDK ping——Sandboxes.list(limit=1) 验证 API 连通性，3s 超时。"""
        result = {"ok": False}
        def _ping():
            try:
                client = self._get_client()
                client.sandboxes.list(limit=1)
                result["ok"] = True
            except Exception:
                result["ok"] = False
        t = threading.Thread(target=_ping, daemon=True)
        t.start()
        t.join(timeout=3)
        return result["ok"]

    def _get_client(self) -> Any:
        """懒加载 DevBox SDK 客户端 + 初始化 DNS override + TLS 配置。

        gateway_skip_tls_verify 控制两层 TLS：
        1. Gateway 连接（sandbox.py 内部 httpx verify）—— DEVBOX_GATEWAY_SKIP_TLS_VERIFY 环境变量
        2. API URL 连接（_transport.py SyncTransport verify）—— 通过 http_transport 传入 verify=False
        两层都需要配置，否则 API URL 的 SSL 验证会失败。

        线程安全：double-check locking 防止多协程（to_thread）并发初始化。
        """
        if self._devbox_client is None:
            with self._client_lock:
                if self._devbox_client is None:  # double-check
                    from devbox import DevBox  # devbox-sdk
                    # 初始化 Gateway DNS override（*.devbox-s2.hwtunnel.com -> gateway_ip）
                    gateway_ip = self._config.get("gateway_ip", "")
                    if gateway_ip:
                        from src.kernel.devbox_dns import init_dns_override
                        init_dns_override(gateway_ip)
                    # TLS 证书校验——配置了 gateway_skip_tls_verify=true 时跳过
                    # （证书 CN 不匹配 gateway 域名，设计文档 §9 确认点 #10）
                    skip_tls = bool(self._config.get("gateway_skip_tls_verify"))
                    if skip_tls:
                        import os as _os
                        _os.environ["DEVBOX_GATEWAY_SKIP_TLS_VERIFY"] = "true"
                    # API URL transport 也需要跳过 TLS（SDK 默认 verify=True，不读环境变量）
                    http_transport = None
                    if skip_tls:
                        import httpx
                        http_transport = httpx.HTTPTransport(verify=False)
                    self._devbox_client = DevBox(
                        api_key=self._config.get("api_key"),
                        api_url=self._config.get("api_url"),
                        gateway_url=self._config.get("gateway_url"),
                        http_transport=http_transport,
                    )
        return self._devbox_client

    def create_sandbox(
        self,
        cwd: str = "",
        exec_backend: str = "",
        task_id: str = "",
        runtime_id: int | None = None,
        **kwargs,
    ) -> DevBoxSandbox:
        """创建 DevBox 云沙箱。"""
        issue_id = kwargs.get("issue_id", task_id)
        repo_key = kwargs.get("repo_key", "")
        user_id = kwargs.get("user_id", 0)
        envs = kwargs.get("envs")
        timeout = kwargs.get("timeout", self._config.get("timeout", 3600))
        template = kwargs.get("template_id", self._config.get("template", "default"))

        return DevBoxSandbox(
            issue_id=issue_id,
            repo_key=repo_key,
            user_id=user_id,
            devbox_client=self._get_client(),
            template=template,
            timeout=timeout,
            envs=envs,
            engine_type=kwargs.get("engine_type", ""),
            state_reporter=kwargs.get("state_reporter"),
            grape_server_url=self._config.get("grape_server_url", ""),
            grape_cli_token=self._config.get("grape_cli_token", ""),
        )

    def connect_sandbox(self, sandbox_id: str, issue_id: str = "", repo_key: str = "") -> DevBoxSandbox:
        """从已有 sandbox_id 恢复连接。

        issue_id/repo_key 可选传入——连接恢复的实例 kill 后 state_reporter 需要
        这些信息更新 DB。
        """
        sb = DevBoxSandbox(
            issue_id=issue_id,
            repo_key=repo_key,
            devbox_client=self._get_client(),
        )
        sb._sandbox_id = sandbox_id
        sb._do_connect(sandbox_id)
        return sb

    # kill_sandbox 继承 CloudSandboxBase 通用实现（先查状态再 kill）

    def _do_kill_sandbox(self, sandbox_id: str) -> bool:
        """DevBox SDK: connect + is_running + kill（不覆盖 self._sandbox）。"""
        client = self._get_client()
        sb = client.sandboxes.connect(sandbox_id)
        if not sb.is_running():
            log.info(f"[devbox] kill_sandbox: {sandbox_id} not running, marking as killed")
            return True
        return sb.kill()

    def cleanup(self) -> None:
        """BUG-10 修复：清理 DNS override（进程级 monkey-patch 恢复）。

        cloud_worker 退出时调用，防止 DNS override 永久生效。
        """
        from src.kernel.devbox_dns import cleanup_dns_override
        cleanup_dns_override()


class DevBoxAdapter:
    """DevBox 平台适配器。"""
    platform = "devbox"
    display_name = "DevBox"
    priority = 100
    is_cloud = True

    # [USP] 能力声明（docs/sandbox/usp-sandbox-protocol-spec.md §4）。
    # 无 pty（文件轮询 transport）；list_templates 有实现（SDK 未装返回 None）。
    MANIFEST = {
        "platform": "devbox",
        "display_name": "DevBox",
        "capabilities": ["create", "kill", "exec", "file_io",
                         "list_templates", "health_check"],
        "config_schema": None,
        "sdk": {"package": "devbox-sdk", "lazy": True},
        "priority": 100,
        "min_worker_version": "",
    }

    _COMMON_FIELDS = {
        "api_url", "api_key", "max_sandboxes", "templates",
        "grape_server_url", "grape_cli_token",
        "request_timeout", "timeout", "pool_size",
        "_comment", "_fields",
    }

    _PROVIDER_FIELDS = {
        "platform", "name", "api_url", "api_key", "max_sandboxes", "provider_id",
        "gateway_url", "gateway_ip", "gateway_skip_tls_verify",
        "timeout", "grape_server_url", "grape_cli_token",
    }

    def build_provider(self, *, audit, credential, seccomp, alert_engine, config):
        return DevBoxProvider(
            audit=audit, credential=credential,
            alert_engine=alert_engine, config=config)

    def parse_config_file(self, raw: dict):
        from .api import ParsedConfig
        endpoint = raw.get("api_url", "")
        api_key = raw.get("api_key", "")
        max_sandboxes = int(raw.get("max_sandboxes", 10))
        templates = raw.get("templates", [])
        extra = {}
        for key, val in raw.items():
            if key in self._COMMON_FIELDS:
                continue
            if val is None:
                continue
            extra[key] = val
        return ParsedConfig(endpoint=endpoint, api_key=api_key,
                            max_sandboxes=max_sandboxes,
                            extra_config=extra, templates=templates)

    def normalize_db_config(self, db_row: dict):
        import logging as _log
        _log = _log.getLogger("sandbox-backends")
        schema = self.ui_schema()
        ui_fields = {f.name for g in schema.groups for f in g.fields}
        known = self._PROVIDER_FIELDS | ui_fields
        _extra = db_row.get("extra_config", {})
        if isinstance(_extra, str):
            import json as _json
            try:
                _extra = _json.loads(_extra)
            except Exception:
                _extra = {}
        if not isinstance(_extra, dict):
            _extra = {}
        cfg = {
            "platform": db_row.get("platform", "devbox"),
            "name": db_row.get("name", "devbox"),
            "api_url": db_row.get("endpoint", ""),
            "api_key": db_row.get("api_key", ""),
            "max_sandboxes": db_row.get("max_sandboxes", 10),
                    "provider_id": int(db_row.get("provider_id", 0) or db_row.get("id", 0) or 0),
        }
        cfg.update(_extra)
        for key in list(cfg.keys()):
            if key not in known:
                _log.warning(f"[devbox] unknown field '{key}' in db config, discarding")
                del cfg[key]
        return cfg

    def prepare_artifacts(self, config, workdir, provider_id) -> None:
        pass

    def validate_extra_config(self, raw: dict) -> dict:
        import logging as _log
        _log = _log.getLogger("sandbox-backends")
        schema = self.ui_schema()
        ui_field_defs = {f.name: f for g in schema.groups for f in g.fields}
        known = self._PROVIDER_FIELDS | set(ui_field_defs.keys())
        result = {}
        for f in ui_field_defs.values():
            val = (raw or {}).get(f.name)
            if f.required and (val is None or val == ""):
                raise ValueError(f"字段 '{f.label or f.name}' 不能为空")
            if val is not None and val != "":
                if f.type == "bool" and not isinstance(val, bool):
                    raise ValueError(f"字段 '{f.label}' 必须是布尔值")
                if f.type == "number" and not isinstance(val, (int, float)):
                    raise ValueError(f"字段 '{f.label}' 必须是数字")
                result[f.name] = val
        for key, val in (raw or {}).items():
            if key in known and key not in result and val is not None and val != "":
                result[key] = val
        for key in (raw or {}):
            if key not in known:
                _log.warning(f"[devbox] unknown extra_config field '{key}' discarded")
        return result

    def list_templates(self, config: dict) -> list[dict] | None:
        """DevBox SDK: templates.list() → 统一格式。

        与 CubeSandboxAdapter.list_templates 对称——扫描能力由 SDK 提供时返回
        统一格式 [{template_id,name,status,description}]，SDK 未装返回 None
        （调用方据此报"平台 devbox 不支持模板扫描"）。

        DevBox 管理面接口（GET /templates）字段：templateID/name/aliases/buildID
        /cpuCount/memoryMB/diskSizeMB，无 status 字段——平台侧模板列表即
        可用列表（不可用模板不出现在响应里），故 status 统一填 "ready"。

        DNS override：与 DevBoxProvider._get_client 相同——配置了 gateway_ip
        时初始化 *.devbox-s2.hwtunnel.com → IP 的进程级解析映射（管理面请求
        走 api_url 域名，通常不需要；数据面才需要。这里保持一致性以防
        api_url 也解析异常）。
        """
        try:
            from devbox import DevBox  # devbox-sdk
        except ImportError:
            return None
        try:
            client = DevBox(
                api_key=config.get("api_key") or None,
                api_url=config.get("api_url") or None,
                gateway_url=config.get("gateway_url") or None,
            )
            infos = client.templates.list()
            out = []
            for ti in infos:
                def _g(o, k, d=""):
                    v = getattr(o, k, None) if not isinstance(o, dict) else o.get(k)
                    return v if v is not None else d
                out.append({
                    "template_id": str(_g(ti, "template_id") or _g(ti, "templateID")),
                    "name": str(_g(ti, "name") or ""),
                    "status": "ready",
                    "description": str(_g(ti, "build_id") or _g(ti, "buildID") or ""),
                })
            return out
        except Exception as e:
            log.warning(f"[devbox] list_templates failed: {e}")
            raise

    def health_check(self, config: dict) -> tuple:
        try:
            _prov = DevBoxProvider(config=config)
            result = _prov.health()
            if hasattr(result, "healthy"):
                return (result.healthy is True, getattr(result, "reason", ""))
            return (bool(result), "")
        except Exception as e:
            log.warning(f"[devbox] health_check failed: {e}")
            return (False, "health check failed, see server log")

    def kill_sandbox(self, config: dict, sandbox_id: str) -> bool:
        try:
            _prov = DevBoxProvider(config=config)
            _prov.kill_sandbox(sandbox_id)
            return True
        except Exception as e:
            log.warning(f"[devbox] kill {sandbox_id} failed: {e}")
            return False

    def ui_schema(self):
        from .api import FormSchema, FormGroup, FormField
        return FormSchema(groups=[
            FormGroup(title="网关", fields=[
                FormField(name="gateway_url", type="text", label="Gateway URL",
                          required=True, placeholder="https://{tunnel_id}-{port}..."),
                FormField(name="gateway_ip", type="text", label="Gateway IP"),
            ]),
            FormGroup(title="TLS", fields=[
                FormField(name="gateway_skip_tls_verify", type="bool",
                          label="跳过 TLS 验证", default=False),
            ]),
        ])

    def cleanup(self, provider_id: int, workdir: str) -> None:
        pass

    def build_transport(self, sandbox, hermes_bin: str, envs: dict):
        """[USP] 平台自决文件轮询 transport（executor 分叉收编）。"""
        return DevBoxTransport(sandbox, hermes_bin=hermes_bin, envs=envs)


PROVIDER = DevBoxProvider
ADAPTER = DevBoxAdapter()


# ---- [USP] DevBoxTransport（自 hermes_acp.py 整块搬迁, 类代码零改动）----

class DevBoxTransport:
    """DevBox 路径 transport——stdin 走 SDK send_stdin，stdout 走文件增量读。

    不走 hwtunnel 事件流（stream_stdout 重连丢字节）。
    - write: handle.send_stdin(data) — gRPC 直连，可靠
    - readline: run_command("tail -c +offset out.jsonl | head -c 64K | base64 -w0")
      HTTP 同步读文件，不走事件流，可靠
    - close: handle.close_stdin() + handle.kill() + 清理文件

    readline timeout 语义：连续静默 N 秒才超时，有数据就重置 deadline。
    """

    _MAX_LINE = 10 * 1024 * 1024  # 10MB 单行上限

    def __init__(self, sandbox, hermes_bin="/tmp/hermes/bin/hermes",
                 workdir="/tmp/_grape_acp", envs=None):
        self.sandbox = sandbox
        self.hermes_bin = hermes_bin
        self.workdir = workdir
        self.out_path = f"{workdir}/out.jsonl"
        self.err_path = f"{workdir}/err.log"
        self.fifo_path = f"{workdir}/acp_stdout.fifo"
        self.exit_code_path = f"{workdir}/exit_code"  # 进程退出码文件
        self.read_offset = 0
        self.buf = b""
        self.handle = None  # DevBoxExecHandle
        self._started = False
        self._write_lock = threading.Lock()
        self._envs = envs or {}  # LLM API key 等环境变量

    def start(self):
        """启动 hermes acp 进程。

        ACP 协议要求 stdout 是 pipe（asyncio connect_write_pipe），
        不能用 > file 重定向（会导致 ACP initialize 时崩溃）。

        方案：用 mkfifo 创建命名管道作为 stdout，后台 cat fifo > file 落盘供 readline 读取。
        顺序关键：先 mkfifo + cat（开读端），再启动 hermes（写端），否则 hermes open fifo 阻塞。
        """
        if self._started:
            raise RuntimeError("already started; call close() first")

        try:
            # 1. 建 FIFO + 清理退出码文件（辅助命令，不需要 LLM Key）
            self.sandbox.run_command(
                f"rm -f {self.out_path} {self.err_path} {self.fifo_path} {self.exit_code_path} && mkdir -p {self.workdir}",
                timeout=5, background=False, merge_credentials=False,
            )

            # 1. 建 FIFO
            self.sandbox.run_command(
                f"mkfifo -m 600 {self.fifo_path}",
                timeout=5, background=False, merge_credentials=False,
            )

            # 2. 后台 cat fifo > out.jsonl（先开读端，避免 hermes 写端 open 阻塞）
            self.sandbox.run_command(
                f"cat {self.fifo_path} > {self.out_path} &",
                timeout=5, background=False, merge_credentials=False,
            )

            # 3. 启动 hermes acp：stdout → fifo，stderr → file，stdin 开启管道
            #    mkfifo 必须和 hermes 在同一条远程命令里（同一个沙箱文件系统）
            #    退出码写入 exit_code 文件（原子写：先写 tmp 再 mv），供 _exited() 通过 files API 读取
            # SDK run_command(background=True, stdin=True) → CommandHandle
            # handle.send_stdin 写 stdin（gRPC 直连，不走 hwtunnel）
            self.handle = self.sandbox.run_command(
                f"PYTHONUNBUFFERED=1 {self.hermes_bin} acp > {self.fifo_path} 2> {self.err_path}; "
                f"ec=$?; echo $ec > {self.exit_code_path}.tmp && mv {self.exit_code_path}.tmp {self.exit_code_path}",
                stdin=True,
                background=True,
                timeout=600,
                envs=self._envs,
            )

            # 健康检查——等 0.5s 确认进程没立即退出
            # SDK _start 从第一个事件提取 pid，进程启动后崩溃时 pid 有值但进程已死
            time.sleep(0.5)
            if not self.handle.pid:
                raise RuntimeError(f"hermes acp failed to start; stderr: {self._tail_stderr()}")
            # 用 kill -0 检查进程是否还活着（辅助命令，不需要 LLM Key）
            result = self.sandbox.run_command(
                f"kill -0 {self.handle.pid} 2>/dev/null && echo 1 || echo 0",
                timeout=5, background=False, merge_credentials=False,
            )
            cr = result.wait() if hasattr(result, 'wait') else result
            out = getattr(cr, 'stdout', '') or ''
            if out.strip() != "1":
                raise RuntimeError(f"hermes acp died at startup; stderr: {self._tail_stderr()}")
            self._started = True
        except Exception:
            self.close()
            raise

    def write(self, data: bytes) -> None:
        """SDK 原生 send_stdin——gRPC 直连，不走 hwtunnel，可靠。"""
        with self._write_lock:
            self.handle.send_stdin(data if isinstance(data, (bytes, str)) else str(data))

    def readline(self, timeout: float = 30.0) -> str:
        """从沙箱内文件增量读取，凑成完整行返回。

        timeout 语义：连续静默 N 秒才超时，有数据就重置 deadline。
        静默超时抛 TransportTimeout（可恢复），进程退出/解码失败抛 TransportClosed（不可恢复）。
        """
        deadline = time.monotonic() + timeout
        while b"\n" not in self.buf:
            if len(self.buf) > self._MAX_LINE:
                raise TransportClosed(f"line exceeds {self._MAX_LINE} bytes")
            chunk = self._read_new()
            if not chunk:
                if self._exited():
                    # 进程已退出：把剩余字节读干净
                    chunk = self._read_new()
                    self.buf += chunk
                    if b"\n" not in self.buf:
                        raise TransportClosed(self._tail_stderr())
                    break
                # 只有"没数据"时才判超时——静默超时可恢复
                if time.monotonic() > deadline:
                    raise TransportTimeout(
                        f"readline timeout after {timeout}s silent; "
                        f"stderr: {self._tail_stderr()}"
                    )
                time.sleep(0.5)  # 降低频率避免 DevBox 进程数限制
                continue
            self.buf += chunk
            # 有数据就重置 deadline——持续输出不超时
            deadline = time.monotonic() + timeout
        line, self.buf = self.buf.split(b"\n", 1)
        return line.decode("utf-8") + "\n"

    def _read_new(self) -> bytes:
        """从 out.jsonl 增量读取——使用 SDK files.read_bytes 零进程读取。

        通过 files API (HTTP GET /files) 直接读取文件内容，不创建沙箱进程，
        彻底消除 resource_exhausted。自己做 offset 切片提取新增字节。
        """
        try:
            data = self.sandbox.read_file_bytes(self.out_path)
        except Exception:
            return b""
        if not data:
            return b""
        # 提取 offset 之后的新增字节
        if self.read_offset >= len(data):
            return b""  # 没有新数据
        new_data = data[self.read_offset:]
        self.read_offset = len(data)
        return new_data

    def _exited(self) -> bool:
        """检查 hermes acp 进程是否已退出——通过 exit_code 文件存在性判定。

        hermes 启动命令结束时原子写入 exit_code 文件（echo $ec > tmp && mv）。
        files.exists 走 HTTP，零进程创建，彻底消除 resource_exhausted。
        """
        try:
            return self.sandbox.file_exists(self.exit_code_path)
        except Exception:
            return False

    def _tail_stderr(self, n=2000) -> str:
        """读取 stderr 文件尾部用于错误诊断——使用 SDK files.read_bytes 零进程读取。"""
        try:
            data = self.sandbox.read_file_bytes(self.err_path)
            if isinstance(data, bytes):
                text = data.decode("utf-8", errors="replace")
            else:
                text = str(data)
            return text[-n:] if len(text) > n else text
        except Exception as e:
            log.warning(f"[devbox] _tail_stderr failed: {e}")
            return ""

    def close(self):
        """关闭 stdin + kill 进程 + 清理文件。"""
        if self.handle:
            try:
                self.handle.close_stdin()
            except Exception:
                pass
            try:
                self.handle.kill()
            except Exception:
                pass
            self.handle = None
        try:
            self.sandbox.run_command(
                f"rm -rf {self.workdir}", timeout=5, background=False, merge_credentials=False
            )
        except Exception:
            pass
        self._started = False
        self.read_offset = 0
        self.buf = b""
