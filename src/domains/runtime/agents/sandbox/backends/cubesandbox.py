"""CubeSandbox 后端实现——Provider + Instance 双类（和 DevBox 对齐）。

CubeSandbox 是腾讯云开源的 MicroVM 沙箱（KVM 硬件级隔离 + 毫秒级启动）。
SDK 兼容 E2B，但 commands.run 没有 background 参数（同步执行）。

与 DevBox 的关键差异：
- kill() 无返回值（→ None），用 try/except 判断
- 状态查询用 get_info().state 而非 is_running()
- 配置用 Config 对象（含 api_url + api_key + template_id）
- commands.run 同步——_run_cloud_sandbox 用后台线程适配
- 有网络控制能力（allow_internet_access + network rules）
"""
from __future__ import annotations

import logging
import os
import time
import threading
import shlex   # [USP搬迁] CubeSandboxTransport 用 shlex.quote——hermes_acp 顶部 import 随迁
from typing import Any, Iterator

from .cloud_base import CloudSandboxBase, CloudProviderBase, ExecSession, _now_iso
# [USP搬迁] Transport 异常族随 api.py 随迁——Transport 类 raise 用
from .api import TransportError, TransportTimeout, TransportClosed, coerce_provider_id  # noqa: F401
from ..types import Capability, ExecutionMode

log = logging.getLogger("devops-bot")


class CubeSandboxExecHandle:
    """包装 CubeSandbox SDK 的 CommandResult 为 ExecHandle 协议。

    CubeSandbox commands.run 是同步的——命令执行完才返回。
    所以 wait() 直接返回已有的 result，stream_stdout 返回空。
    """
    def __init__(self, result):
        self._result = result
        self._exit_code = getattr(result, 'exit_code', -1)
        self._stdout = getattr(result, 'stdout', '') or ''
        self._stderr = getattr(result, 'stderr', '') or getattr(result, 'error', '') or ''

    def wait(self, timeout: float | None = None):
        """同步执行已完成——直接返回 CommandResult。"""
        from src.domains.runtime.agents.sandbox.types import CommandResult
        return CommandResult(
            exit_code=self._exit_code,
            stdout=self._stdout,
            stderr=self._stderr,
        )

    def stream_stdout(self):
        """同步执行无流——返回空。"""
        return iter([])

    def send_stdin(self, data):
        pass

    def close_stdin(self):
        pass

    def kill(self):
        """同步执行已完成——无需 kill。"""
        pass

    @property
    def pid(self):
        return None

    @property
    def exit_code(self):
        return self._exit_code


class CubeSandboxInstance(CloudSandboxBase):
    """CubeSandbox 沙箱实例——Sandbox 角色。

    由 CubeSandboxProvider.create_sandbox() 创建。
    继承 CloudSandboxBase 的 __enter__/__exit__ 生命周期。
    """

    def __init__(self, *, issue_id: str, repo_key: str, user_id: int,
                 config: dict, template_id: str = "", state_reporter=None,
                 engine_type: str = "", work_branch: str = ""):
        super().__init__(
            issue_id=issue_id,
            repo_key=repo_key,
            provider_name=config.get("name", "cubesandbox"),
            platform="cubesandbox",
            user_id=user_id,
            engine_type=engine_type,
            state_reporter=state_reporter,
            provider_id=coerce_provider_id(config.get("provider_id", 0)),
        )
        self._config = config
        self._sandbox: Any = None  # cubesandbox.Sandbox 实例
        self._sandbox_id: str = ""
        self._template_id = template_id
        self._init_envs: dict = {}
        self._grape_server_url: str = self._config.get("grape_server_url", "")
        self._grape_cli_token: str = self._config.get("grape_cli_token", "")
        self._repo_url: str = ""
        self._bot_token: str = ""
        self._work_branch: str = work_branch  # BUG-17 修复：显式初始化

    # ===== 平台特定初始化钩子 =====

    def _post_create_hook(self) -> None:
        """SDK 对象创建后设置 SSL 证书（per-provider）。

        CubeSandbox SDK 底层用 requests，通过 session.verify 设置证书路径。
        不再用 os.environ["REQUESTS_CA_BUNDLE"]（进程级，影响所有 provider）。
        """
        _ca_bundle = self._config.get("_ssl_cert_path", "")
        if _ca_bundle and self._sandbox:
            self._sandbox._session.verify = _ca_bundle

    # ===== 能力声明 =====

    @property
    def capabilities(self) -> set:
        return {Capability.STREAMING_STDOUT, Capability.FILE_WRITE, Capability.FILE_READ}

    @property
    def credential_mode(self):
        from ..types import CredentialMode
        return CredentialMode.ENVS

    def supports_pty(self) -> bool:
        """CubeSandbox 支持 PTY（SDK 有 Pty 类）。"""
        return True

    # ===== USP ExecSession 实现（docs/sandbox/usp-sandbox-protocol-spec.md §2.3）=====

    def pty_create(self, rows: int = 50, cols: int = 200,
                   cwd=None, envs=None, timeout=None):
        """创建 PTY 型 ExecSession——**本文件唯一允许 import PtySize 的地方**。

        SDK 升级时只改此一处（对齐 _post_create_hook 钩子哲学）。
        """
        from cubesandbox import PtySize
        _h = self._sandbox.pty.create(
            PtySize(rows=rows, cols=cols),
            cwd=cwd,
            envs=envs or {},
            timeout=timeout,
        )
        return _CubePtySession(_h)

    # ===== 模板方法实现 =====

    def _build_egress_network(self) -> dict | None:
        """构建 egress 网络策略（allow_out IP 白名单）。

        优先级：
        1. Provider extra_config.egress_allow_out（显式配置，list[str] CIDR/IP）
        2. 自动推断：从 GRAPE_SERVER_URL 解析 host → /32 放行
           （grape 平台 API 是 agent 在沙箱内必须可达的核心依赖）
        3. 都没有 → None（平台默认策略）
        """
        _explicit = self._config.get("egress_allow_out") or None
        if isinstance(_explicit, list) and _explicit:
            return {"allow_out": [str(x) for x in _explicit]}
        try:
            import os as _os
            from urllib.parse import urlparse as _up
            # 来源优先级：任务下发的 GRAPE_SERVER_URL（_init_envs，最权威——服务端
            # 已解析好的 server_base_url）> cloud_worker 进程 env（CLOUD_WORKER_SERVER_URL）
            # > 本机兜底 get_server_base_url（本机引擎路径）。
            # 事故教训（event30）：worker 机器上跑 get_server_base_url 读不到主库配置，
            # 兜底 'http://localhost:3000' → allow_out=['localhost/32'] → 平台
            # EnsureNetwork 拒绝（invalid CIDR target），建箱直接失败。
            _base = (self._init_envs or {}).get("GRAPE_SERVER_URL", "") or ""
            if not _base:
                _base = _os.environ.get("CLOUD_WORKER_SERVER_URL", "") or ""
            if not _base and _os.environ.get("GRAPE_SERVER_URL"):
                _base = _os.environ.get("GRAPE_SERVER_URL", "")
            if not _base:
                from src.domains.mgmt.public import get_server_base_url  # 跨域走 owner public
                _base = get_server_base_url()
            _host = (_up(_base).hostname or "") if _base else ""
            # 域名形式（非 IP）不放进 allow_out——平台域名流量走 SNI 代理本就放行；
            # 'localhost' 等非 IP 主机名会导致平台 CIDR 解析失败
            import ipaddress as _ipa
            if _host:
                try:
                    _ipa.ip_address(_host)
                    return {"allow_out": [f"{_host}/32"]}
                except ValueError:
                    pass
        except Exception:
            pass
        return None

    def _do_create(self) -> str:
        """CubeSandbox SDK: Sandbox.create()。"""
        from cubesandbox import Sandbox, Config
        _tpl = self._template_id or self._config.get("template_id") or None
        cfg = Config(
            api_url=self._config.get("api_url", ""),
            api_key=self._config.get("api_key") or None,
            sandbox_domain=self._config.get("sandbox_domain", "cube.app"),
            proxy_node_ip=self._config.get("proxy_node_ip") or None,
            template_id=_tpl,
            timeout=self._config.get("timeout", 3600),
            request_timeout=self._config.get("request_timeout", 30),
        )
        envs = self._init_envs or None
        # [沙箱迁移] egress 白名单：CubeSandbox 平台默认只放行域名流量（SNI 代理），
        # 裸 IP 直连全断——agent 在沙箱内访问 grape 平台 API（GRAPE_SERVER_URL 是内网
        # IP:3000）需要 allow_out 显式放行。server_host 从 Provider extra_config
        # （egress_allow_out）取，缺省 grape 服务端地址推断。
        _network = self._build_egress_network()
        sb = Sandbox.create(
            template=_tpl,
            envs=envs,
            config=cfg,
            network=_network,
        )
        self._sandbox = sb
        self._sandbox_id = sb.sandbox_id
        log.info(f"[cubesandbox] sandbox created: {self._sandbox_id}")
        return self._sandbox_id

    def _do_connect(self, sandbox_id: str) -> None:
        """CubeSandbox SDK: Sandbox.connect()。

        arch-guard F6/C2：外部平台调用记录请求/结果/耗时接口日志。
        """
        import time as _t
        _start = _t.monotonic()
        try:
            from cubesandbox import Sandbox, Config
            cfg = Config(
                api_url=self._config.get("api_url", ""),
                api_key=self._config.get("api_key") or None,
                sandbox_domain=self._config.get("sandbox_domain", "cube.app"),
                proxy_node_ip=self._config.get("proxy_node_ip") or None,
                request_timeout=self._config.get("request_timeout", 30),
            )
            self._sandbox = Sandbox.connect(sandbox_id, config=cfg)
            self._sandbox_id = sandbox_id
            self._post_create_hook()
            log.info(f"[cubesandbox] connect ok: sandbox={sandbox_id} "
                     f"duration_ms={int((_t.monotonic() - _start) * 1000)}")
        except Exception as e:
            log.error(f"[cubesandbox] connect failed: sandbox={sandbox_id} "
                      f"duration_ms={int((_t.monotonic() - _start) * 1000)} error={e}")
            raise

    def _do_kill(self) -> bool:
        """CubeSandbox SDK: Sandbox.kill()（无返回值，成功不抛异常即 True）。

        BUG-3 修复：用 try/except 包裹，异常时返回 False。
        基类 kill() 依赖 _do_kill() 返回值决定是否标记 _killed=True。
        """
        if not self._sandbox:
            return False  # 未 connect，不能确认远端沙箱已销毁
        import time as _t
        _start = _t.monotonic()
        try:
            self._sandbox.kill()
            log.info(f"[cubesandbox] kill ok: sandbox={self._sandbox_id} "
                     f"duration_ms={int((_t.monotonic() - _start) * 1000)}")
            return True
        except Exception as e:
            log.error(f"[cubesandbox] kill failed: sandbox={self._sandbox_id} "
                      f"duration_ms={int((_t.monotonic() - _start) * 1000)} error={e}")
            return False

    def _do_is_running(self) -> bool:
        """CubeSandbox SDK: get_info().state == RUNNING。"""
        if not self._sandbox:
            return False
        import time as _t
        _start = _t.monotonic()
        try:
            info = self._sandbox.get_info()
            state = str(info.get("state", "")).upper() if isinstance(info, dict) else str(getattr(info, "state", "")).upper()
            log.info(f"[cubesandbox] get_info: sandbox={self._sandbox_id} state={state} "
                     f"duration_ms={int((_t.monotonic() - _start) * 1000)}")
            return state == "RUNNING"
        except Exception as e:
            log.warning(f"[cubesandbox] get_info failed: sandbox={self._sandbox_id} "
                        f"duration_ms={int((_t.monotonic() - _start) * 1000)} error={e}")
            return False

    def _do_refresh(self) -> None:
        """CubeSandbox SDK: set_timeout()。"""
        if self._sandbox:
            self._sandbox.set_timeout(self._config.get("timeout", 3600))

    def _do_reconnect(self) -> None:
        """CubeSandbox SDK: 重新 connect。"""
        if self._sandbox_id:
            self._do_connect(self._sandbox_id)

    def _do_run_command(self, cmd: str, envs: dict | None = None, timeout: float = 600, **kwargs) -> Any:
        """CubeSandbox SDK: commands.run()（同步，无 background）。

        CubeSandbox SDK 的 commands.run 走 gRPC stream，envd 的 deadline 默认 60s。
        长时间命令（pip install）会 deadline_exceeded。

        解决方案：对长时间命令用 nohup + 轮询模式（_run_long_command）。
        短命令直接 commands.run。
        """
        if not self._sandbox:
            raise RuntimeError("sandbox not connected")
        import time as _t
        _start = _t.monotonic()
        # arch-guard F6/C2：接口日志记命令摘要（截断）+ 耗时，不落 envs 明文
        _cmd_summary = (cmd[:200] + "...") if cmd and len(cmd) > 200 else (cmd or "")
        try:
            _result = self._sandbox.commands.run(cmd, envs=envs, timeout=timeout)
            log.info(f"[cubesandbox] run_command ok: sandbox={self._sandbox_id} "
                     f"cmd={_cmd_summary!r} duration_ms={int((_t.monotonic() - _start) * 1000)}")
            return _result
        except Exception as e:
            log.error(f"[cubesandbox] run_command failed: sandbox={self._sandbox_id} "
                      f"cmd={_cmd_summary!r} "
                      f"duration_ms={int((_t.monotonic() - _start) * 1000)} error={e}")
            raise

    def _run_long_command(self, cmd: str, timeout: int = 900, poll_interval: int = 15,
                          envs: dict | None = None) -> tuple[int, str]:
        """长时间命令用 nohup 后台跑 + 轮询检查完成标记。

        CubeSandbox SDK 的 gRPC stream 有 60s deadline，长时间命令（pip install、
        apt-get install + git clone）会 deadline_exceeded。用 nohup + </dev/null >/dev/null 2>&1 &
        让 commands.run 立即返回，然后轮询检查完成标记文件。

        envs: 环境变量注入到 nohup 脚本（在脚本开头 export）。
        返回 (exit_code, stdout_tail)。

        每次调用用唯一 ID 生成独立文件名，防止并发调用互相覆盖
        （_init_sandbox 的 pip install 和 _clone_repo 的 git clone 同时用此方法）。
        """
        import time as _time
        import uuid as _uuid
        sb = self._sandbox
        _uid = _uuid.uuid4().hex[:8]
        script_path = f"/tmp/_long_cmd_{_uid}.sh"
        marker_done = f"/tmp/_cmd_done_{_uid}"
        marker_exit = f"/tmp/_cmd_exit_{_uid}"
        log_file = f"/tmp/_cmd_{_uid}.log"

        # envs 注入到脚本开头（nohup 子进程继承）
        env_exports = ""
        if envs:
            import shlex as _shlex2
            env_exports = "\n".join(f"export {k}={_shlex2.quote(str(v))}" for k, v in envs.items())
            env_exports += "\n"

        script = f"""#!/bin/bash
{env_exports}{cmd} > {log_file} 2>&1
echo $? > {marker_exit}
touch {marker_done}
"""
        sb.files.write(script_path, script)
        sb.commands.run(f"chmod +x {script_path}", timeout=5)

        sb.commands.run(f"nohup {script_path} </dev/null >/dev/null 2>&1 & echo started", timeout=10)

        max_polls = timeout // poll_interval
        for i in range(max_polls):
            _time.sleep(poll_interval)
            r = sb.commands.run(f"test -f {marker_done} && echo done || echo wait", timeout=10)
            if "done" in (r.stdout or ""):
                ec = sb.commands.run(f"cat {marker_exit}", timeout=10)
                lr = sb.commands.run(f"tail -5 {log_file}", timeout=10)
                exit_code = int(ec.stdout.strip()) if ec.stdout and ec.stdout.strip().isdigit() else -1
                _result = (exit_code, lr.stdout or "")
                # 遗漏-5 修复：清理临时文件（脚本/标记/日志）
                for _f in (script_path, marker_done, marker_exit, log_file):
                    try:
                        sb.commands.run(f"rm -f {_f}", timeout=3)
                    except Exception:
                        pass
                return _result
            if i % 4 == 0:
                lr = sb.commands.run(f"tail -1 {log_file} 2>/dev/null", timeout=10)
                log.info(f"[cubesandbox] long command progress [{(i+1)*poll_interval}s]: {lr.stdout.strip() if lr.stdout else '...'}")

        # 遗漏-5 修复：超时也清理临时文件
        for _f in (script_path, marker_done, marker_exit, log_file):
            try:
                sb.commands.run(f"rm -f {_f}", timeout=3)
            except Exception:
                pass
        return (-1, "timeout")

    def _do_write_file(self, path: str, data: str | bytes) -> None:
        """CubeSandbox SDK: files.write()。"""
        if self._sandbox:
            self._sandbox.files.write(path, data)

    def _do_read_file(self, path: str) -> str:
        """CubeSandbox SDK: files.read()。"""
        if not self._sandbox:
            return ""
        return self._sandbox.files.read(path)

    # ===== 公共方法 override =====
    # write_file / read_file / list_files 由 PlatformAdapter 原语 + _do_* 方法提供

    def read_file(self, path: str) -> str:
        return self._do_read_file(path)

    def read_file_bytes(self, path: str) -> bytes:
        """读取沙箱文件原始字节——走 SDK files.read (HTTP GET)，零进程创建。"""
        if self._sandbox:
            text = self._sandbox.files.read(path)
            return text.encode("utf-8") if isinstance(text, str) else bytes(text)
        return b""

    def file_exists(self, path: str) -> bool:
        """判断沙箱文件是否存在——走 SDK files.exists (HTTP)，零进程创建。"""
        if self._sandbox:
            return self._sandbox.files.exists(path)
        return False

    def list_files(self, path: str) -> list:
        if self._sandbox:
            return self._sandbox.files.list(path)
        return []

    def run_command(self, cmd: str, envs: dict | None = None, timeout: float = 600,
                    background: bool = False, merge_credentials: bool = True, **kwargs) -> Any:
        """执行命令——返回 CubeSandboxExecHandle（适配 ExecHandle 协议）。

        CubeSandbox SDK commands.run 是同步的，直接返回 CommandResult。
        包装成 ExecHandle 让 _run_cloud_sandbox 的 handle.wait() 能用。

        merge_credentials=True（默认）：合并 _credentials + _init_envs（主命令需要）。
        merge_credentials=False：不合并（辅助命令不需要 LLM Key）。
        """
        if merge_credentials:
            _merged_envs = dict(self._credentials or {})
            if self._init_envs:
                _merged_envs.update(self._init_envs)
            if envs:
                _merged_envs.update(envs)
        else:
            _merged_envs = envs or {}
        result = self._do_run_command(cmd, envs=_merged_envs, timeout=timeout)
        return CubeSandboxExecHandle(result)

    # bind_credential 统一用基类（存到 _credentials），不再 override
    # _init_envs 仅保留构造时初始 envs 用途（_do_create 传入）

    def is_active(self) -> bool:
        """H-4 修复：改为方法（和 CloudSandboxBase/bwrap 一致，hermes_acp 调 is_active()）。"""
        return self._sandbox is not None and self._do_is_running()

    def cleanup(self) -> None:
        """关闭 CubeSandbox SDK 连接（不 kill 远端沙箱）。"""
        if self._sandbox:
            try:
                self._sandbox.close()
            except Exception:
                pass
            self._sandbox = None

    # ---- 仓库配置 override ----

    def configure_repo(self, *, repo_url, bot_token, work_branch):
        super().configure_repo(
            repo_url=repo_url, bot_token=bot_token,
            work_branch=work_branch,
        )
        # grape-cli 配置从构造时 config 传入的 _grape_server_url/_grape_cli_token 读
        if self._sandbox and self._repo_url and self._bot_token:
            self._clone_repo()

    @property
    def repo_dir(self):
        return getattr(self, "_REPO_DIR", "")

    @property
    def skill_dir(self):
        return getattr(self, "_SKILL_DIR", "")

    def upload_skill(self, obs_url: str) -> None:
        """下载并解压 skill zip 到沙箱内 repo/.skills 目录。

        通过 OBS 预签名 URL 下载（和 DevBox 侧 skill_obs_url 机制一致）。
        skill 目录 = repo_dir/.skills（和 DevBox 侧 _REPO_DIR/.skills 一致）。

        BUG-7 修复：检查 exit_code，失败抛异常（与 DevBox 对齐）。
        """
        if not self._sandbox or not obs_url:
            return
        import shlex as _shlex
        _skill_dir = f"{self.repo_dir}/.skills"
        _tmp_zip = "/tmp/skill.zip"
        r = self.run_cmd(
            f'curl -sSL {_shlex.quote(obs_url)} -o {_tmp_zip} && '
            f'mkdir -p {_skill_dir} && '
            f'unzip -q {_tmp_zip} -d {_skill_dir} && rm {_tmp_zip}',
            timeout=60,
        )
        _ec = r[0] if isinstance(r, tuple) else getattr(r, "exit_code", -1)
        if _ec != 0:
            _out = r[1] if isinstance(r, tuple) else getattr(r, "stdout", "")
            raise RuntimeError(f"upload_skill failed (exit={_ec}): {(_out or '')[:200]}")
        self._SKILL_DIR = _skill_dir
        log.info(f"[cubesandbox] skill zip downloaded for {self._sandbox_id}")

    # ===== PlatformAdapter 能力声明 =====

    @property
    def supports_long_cmd(self) -> bool:
        """CubeSandbox gRPC 有 60s deadline——长时间命令用 nohup+poll。"""
        return True

    @property
    def needs_entry_point_wrapper(self) -> bool:
        """CubeSandbox 预装模板已有 entry_points，不需要 wrapper。"""
        return False

    # ===== PlatformAdapter 原语 =====

    def check_environment(self) -> None:
        """CubeSandbox 预装环境——无需 DNS 修复/磁盘检测。"""
        pass

    def run_cmd(self, cmd: str, timeout: int = 60, envs: dict | None = None) -> tuple[int, str]:
        """执行命令，返回 (exit_code, stdout)。

        遗漏-3 修复：添加 envs 参数（与 DevBox run_cmd 签名对齐）。
        """
        r = self._sandbox.commands.run(cmd, envs=envs, timeout=timeout)
        return (getattr(r, "exit_code", -1), getattr(r, "stdout", "") or "")

    def run_long_cmd(self, cmd: str, timeout: int, envs: dict | None = None) -> tuple[int, str]:
        """CubeSandbox: nohup+poll（gRPC 60s deadline 适配）。"""
        return self._run_long_command(cmd, timeout=timeout, envs=envs)

    def write_file(self, path: str, content: str | bytes) -> None:
        self._sandbox.files.write(path, content)

    def get_home(self) -> str:
        """BUG-6 修复：改用 run_cmd（走 background=True + 重连机制）。"""
        _ec, _out = self.run_cmd("echo $HOME", timeout=5)
        return _out.strip() or "/root"

    def _configure_grape_cli(self) -> None:
        """配置 grape-cli server URL + token。

        BUG-6 修复：所有 cmds.run 调用改为 self.run_cmd（走 background=True + 重连机制）。
        体验-6 修复：先追加 PATH 再执行 grape config（与 DevBox 顺序对齐）。
        """
        import shlex as _shlex
        # 先追加 PATH（与 DevBox 顺序对齐——先确保 grape 在 PATH 中再执行 config）
        _path_export = "export PATH=$PATH:/tmp/pylibs/bin"
        self.run_cmd(
            f"grep -q '/tmp/pylibs/bin' ~/.bashrc 2>/dev/null || echo '{_path_export}' >> ~/.bashrc",
            timeout=5,
        )
        self.run_cmd(
            f"grep -q '/tmp/pylibs/bin' ~/.bash_profile 2>/dev/null || echo '{_path_export}' >> ~/.bash_profile",
            timeout=5,
        )
        self.run_cmd(
            f"grep -q '/tmp/pylibs/bin' ~/.profile 2>/dev/null || echo '{_path_export}' >> ~/.profile",
            timeout=5,
        )
        # 再配置 grape-cli
        self.run_cmd(
            f"grape config set-server {_shlex.quote(self._grape_server_url)}",
            timeout=10,
        )
        if self._grape_cli_token:
            self.run_cmd(
                "grape config set-token $GRAPE_TOKEN",
                envs={"GRAPE_TOKEN": self._grape_cli_token},
                timeout=10,
            )
        log.info(f"[cubesandbox] grape-cli configured for {self._sandbox_id}")

    def _clone_repo(self) -> None:
        """git clone 仓库 + 配置凭据。

        使用 GIT_ASKPASS 脚本注入 token——token 不出现在命令行/进程列表，
        只存在于环境变量 + 脚本文件中。gitcode 不支持 credential helper 方式，
        GIT_ASKPASS 是 git 官方推荐的凭据注入机制。

        幂等：已 clone（_REPO_DIR 已设）则跳过。
        """
        if getattr(self, "_REPO_DIR", ""):  # 幂等——已 clone 则跳过
            return
        if not self._sandbox or not self._repo_url or not self._bot_token:
            log.warning(f"[cubesandbox] _clone_repo skipped: sandbox={'yes' if self._sandbox else 'no'} repo_url={'yes' if self._repo_url else 'no'} bot_token={'yes' if self._bot_token else 'no'}")
            return
        import shlex as _shlex
        _repo_dir = "/home/user/repo"
        # 写 GIT_ASKPASS 脚本——git 调用时从环境变量读 token
        _askpass_script = '#!/bin/sh\ncase "$1" in\nUsername*) echo "bot";; \nPassword*) echo "$GIT_BOT_TOKEN";; \nesac\n'
        self._sandbox.files.write("/tmp/_git_askpass.sh", _askpass_script)
        self.run_cmd("chmod +x /tmp/_git_askpass.sh", timeout=5)
        self.run_cmd(
            'git config --global user.name "Grape Bot" && '
            'git config --global user.email "bot@grape.local"',
            timeout=10,
        )
        # 合并命令：clone。通过 _run_long_command 绕过 60s deadline。
        # [沙箱迁移] git 缺失时不再 apt 安装（实测 CubeSandbox 沙箱 apt >10min 必超时）——
        # 先探测 git；有 git 走 git clone（P1-1: -b branch），没有直接 dulwich 降级。
        _branch_arg = f"-b {_shlex.quote(self._work_branch)}" if self._work_branch else ""
        _git_ec, _git_out = self.run_cmd("command -v git && echo has || echo none", timeout=5)
        if "has" in (_git_out or ""):
            _clone_cmd = (
                f"GIT_ASKPASS=/tmp/_git_askpass.sh git clone --depth 1 {_branch_arg} {_shlex.quote(self._repo_url)} {_repo_dir}"
            )
            _exit_code, _stdout_tail = self._run_long_command(
                _clone_cmd, timeout=300, poll_interval=10,
                envs={"GIT_BOT_TOKEN": self._bot_token},
            )
        else:
            log.info(f"[cubesandbox] git not preinstalled, using dulwich fallback directly for {self._sandbox_id}")
            _exit_code, _stdout_tail = -1, "git not installed (skip apt, dulwich path)"
        # [沙箱迁移] clone 降级链：镜像未预装 git 且 apt-get install 极慢（实测 CubeSandbox
        # 某些沙箱 apt >10min，clone 300s 必超）→ 二级降级 dulwich（纯 Python git 实现，
        # pip 秒装，git smart HTTP 协议对 gitcode 可用）。降级目录结构与 clone 一致
        # （含 .git，引擎侧 git 命令可续用），_REPO_DIR/_SKILL_DIR 不变。
        _dir_ec, _dir_out = self.run_cmd(f"test -d {_repo_dir}/.git && echo git || echo missing", timeout=5)
        if "git" not in (_dir_out or ""):
            _branch_arg_d = f", branch='{self._work_branch}'" if self._work_branch else ""
            _dulwich_cmd = (
                "python3 -m pip install -q dulwich >/dev/null 2>&1; "
                f"python3 -c \"from dulwich.porcelain import clone; "
                f"clone('{self._repo_url}', '{_repo_dir}', depth=1{_branch_arg_d})\" >/dev/null 2>&1"
            )
            _tar_ec, _tar_tail = self._run_long_command(_dulwich_cmd, timeout=280, poll_interval=10)
            _check_ec, _check_out = self.run_cmd(f"test -f {_repo_dir}/HEAD && echo ok || echo fail", timeout=5)
            if _tar_ec != 0 or "ok" not in (_check_out or ""):
                log.error(f"[cubesandbox] dulwich fallback failed exit={_tar_ec} check={_check_out} stderr={(_tar_tail or '')[:200]}")
                raise RuntimeError(f"git clone failed (exit={_exit_code}): {(_stdout_tail or '')[:200]}; "
                                   f"dulwich fallback also failed: {(_tar_tail or '')[:150]}")
            log.info(f"[cubesandbox] repo fetched via dulwich fallback (git unavailable/slow) for {self._sandbox_id}")
        elif _exit_code != 0:
            _stderr = _stdout_tail or ""
            log.error(f"[cubesandbox] git clone failed exit={_exit_code} stderr={_stderr[:200]}")
            raise RuntimeError(f"git clone failed (exit={_exit_code}): {_stderr[:200]}")
        self._REPO_DIR = _repo_dir
        self._SKILL_DIR = f"{_repo_dir}/.skills"
        log.info(f"[cubesandbox] repo cloned for {self._sandbox_id}")

    def send_stdin(self, data: str | bytes) -> None:
        """CubeSandbox 不支持交互式 stdin。

        BUG-9 修复：添加实例级方法，与 Protocol 契约对齐。
        基类默认实现 raise NotImplementedError，这里明确声明不支持。
        """
        raise NotImplementedError("cubesandbox does not support interactive stdin")

    def close_stdin(self) -> None:
        """无 stdin 可关闭，no-op。"""
        pass

    def __enter__(self):
        return super().__enter__()

    def __exit__(self, *args) -> None:
        super().__exit__(*args)


class CubeSandboxProvider(CloudProviderBase):
    """CubeSandbox Provider——创建/连接/销毁沙箱实例。

    继承 CloudProviderBase 的 health 缓存。
    和 DevBoxProvider 对齐——Provider 角色，不继承 CloudSandboxBase。
    """

    name = "cubesandbox"
    provider_name = "cubesandbox"
    execution_mode = ExecutionMode.CLOUD_SDK

    def __init__(self, *, audit=None, credential=None, alert_engine=None,
                 config: dict | None = None):
        super().__init__(audit=audit, credential=credential, alert_engine=alert_engine)
        self._config = config or self._load_config()
        # SSL 证书：per-provider 路径（不再设 os.environ["REQUESTS_CA_BUNDLE"]）
        # 优先 ssl_cert_content 对应的 per-provider 路径，其次 ssl_cert_file（本地路径 fallback）
        from src.domains.runtime.agents.sandbox.backends.api import cert_artifact_key
        _artifact_key = cert_artifact_key(self._config.get("provider_id", 0), self._config)
        _ca_bundle = ""
        if self._config.get("ssl_cert_content"):
            if _artifact_key:
                _ca_bundle = f"/var/lib/grape/cloud-worker/cube-rootCA-{_artifact_key}.pem"
            else:
                _ca_bundle = "/var/lib/grape/cloud-worker/cube-rootCA.pem"
            if not os.path.isfile(_ca_bundle):
                log.warning(f"[cubesandbox] ssl_cert file not materialized yet at {_ca_bundle}, SSL may fail")
        elif self._config.get("ssl_cert_file"):
            _ca_bundle = self._config.get("ssl_cert_file")
        # 将 SSL 证书路径存入 config，供 CubeSandboxInstance._post_create_hook 使用
        if _ca_bundle:
            self._config["_ssl_cert_path"] = _ca_bundle

    @staticmethod
    def _load_config() -> dict:
        """从 configs/cubesandbox-config.json 加载配置。"""
        import json
        from pathlib import Path
        for p in [
            Path("configs/cubesandbox-config.json"),
            Path.home() / "grape" / "configs" / "cubesandbox-config.json",
            Path(__file__).resolve().parent.parent.parent.parent.parent / "configs" / "cubesandbox-config.json",
        ]:
            if p.is_file():
                with open(p) as f:
                    log.info(f"[cubesandbox] config loaded from {p}")
                    return json.load(f)
        log.warning("[cubesandbox] config not found, using defaults")
        return {}

    def is_configured(self) -> bool:
        return bool(self._config.get("api_url"))

    def _do_health_check(self) -> bool:
        """CubeSandbox SDK: Sandbox.health()。"""
        try:
            from cubesandbox import Sandbox, Config
            cfg = Config(
                api_url=self._config.get("api_url", ""),
                api_key=self._config.get("api_key") or None,
                sandbox_domain=self._config.get("sandbox_domain", "cube.app"),
            proxy_node_ip=self._config.get("proxy_node_ip") or None,
                request_timeout=3,
            )
            result = Sandbox.health(config=cfg)
            return bool(result)
        except Exception:
            return False

    def create_sandbox(self, cwd="", exec_backend="", task_id="", runtime_id=None, template_id="", **kwargs) -> CubeSandboxInstance:
        """创建 CubeSandbox 沙箱实例。

        template_id 从 grape_t_sandbox_templates 表查出来传入（模板路由）。
        空字符串表示用配置文件的默认 template_id（向后兼容）。
        """
        issue_id = kwargs.get("issue_id", task_id)
        repo_key = kwargs.get("repo_key", "")
        user_id = kwargs.get("user_id", 0)
        inst = CubeSandboxInstance(
            issue_id=issue_id,
            repo_key=repo_key,
            user_id=user_id,
            config=self._config,
            template_id=template_id or self._config.get("template_id", ""),
            state_reporter=kwargs.get("state_reporter"),
            engine_type=kwargs.get("engine_type", ""),
        )
        return inst

    def connect_sandbox(self, sandbox_id: str, issue_id: str = "", repo_key: str = "") -> CubeSandboxInstance:
        """从已有 sandbox_id 恢复连接。

        issue_id/repo_key 可选传入——连接恢复的实例 kill 后 state_reporter 需要
        这些信息更新 DB（每环节新建模式下按 sandbox_id 匹配，issue_id/repo_key
        仅用于记录完整性）。

        arch-guard F6/C2：外部平台 connect 调用写审计链（耗时 + 结果）。
        """
        import time as _t
        _start = _t.monotonic()
        try:
            inst = CubeSandboxInstance(
                issue_id=issue_id,
                repo_key=repo_key,
                user_id=0,
                config=self._config,
            )
            inst._sandbox_id = sandbox_id
            inst._do_connect(sandbox_id)
            self._audit_external_call("connect_sandbox", issue_id, "allow",
                                      int((_t.monotonic() - _start) * 1000),
                                      {"sandbox_id": sandbox_id})
            return inst
        except Exception as e:
            self._audit_external_call("connect_sandbox", issue_id, "deny",
                                      int((_t.monotonic() - _start) * 1000),
                                      {"sandbox_id": sandbox_id, "error": str(e)[:200]})
            raise

    def _do_kill_sandbox(self, sandbox_id: str) -> bool:
        """CubeSandbox SDK: connect + get_info + kill（不覆盖 self._sandbox）。

        arch-guard F6/C2：外部平台 kill 调用写审计链（耗时 + 结果）。
        """
        import time as _t
        _start = _t.monotonic()
        try:
            from cubesandbox import Sandbox, Config
            cfg = Config(
                api_url=self._config.get("api_url", ""),
                api_key=self._config.get("api_key") or None,
                sandbox_domain=self._config.get("sandbox_domain", "cube.app"),
                proxy_node_ip=self._config.get("proxy_node_ip") or None,
                request_timeout=10,
            )
            sb = Sandbox.connect(sandbox_id, config=cfg)
            info = sb.get_info()
            state = str(info.get("state", "")).upper() if isinstance(info, dict) else str(getattr(info, "state", "")).upper()
            if state in ("STOPPED", "KILLED", "EXPIRED", "DELETED", "PAUSED"):
                log.info(f"[cubesandbox] kill_sandbox: {sandbox_id} already {state}")
                self._audit_external_call("kill_sandbox", sandbox_id, "stopped",
                                          int((_t.monotonic() - _start) * 1000),
                                          {"sandbox_id": sandbox_id, "state": state})
                return True
            sb.kill()
            self._audit_external_call("kill_sandbox", sandbox_id, "allow",
                                      int((_t.monotonic() - _start) * 1000),
                                      {"sandbox_id": sandbox_id})
            return True
        except Exception as e:
            self._audit_external_call("kill_sandbox", sandbox_id, "deny",
                                      int((_t.monotonic() - _start) * 1000),
                                      {"sandbox_id": sandbox_id, "error": str(e)[:200]})
            raise


class CubeSandboxAdapter:
    """CubeSandbox 平台适配器。"""
    platform = "cubesandbox"
    display_name = "CubeSandbox"
    priority = 50
    is_cloud = True

    # [USP] 能力声明（docs/sandbox/usp-sandbox-protocol-spec.md §4）——代码即 manifest。
    # 一致性断言（双向）见 tests/test_manifest_contract.py；probe_engines 位为
    # 引擎探测 spec 预留（声明与实现同步落地，本批不声明）。
    MANIFEST = {
        "platform": "cubesandbox",
        "display_name": "CubeSandbox",
        "capabilities": ["create", "kill", "exec", "pty", "file_io",
                         "list_templates", "health_check", "egress_control",
                         "probe_engines"],
        "config_schema": None,   # 复用 ui_schema()，不重复定义
        "sdk": {"package": "cubesandbox>=0.7", "lazy": True},   # 声明；懒安装消费批1
        "priority": 50,
        "min_worker_version": "",
    }

    # [引擎探测 spec] 白名单 = ENGINE_REGISTRY 注册引擎——刻意边界：不扫任意
    # 二进制（安全+误报控制）。UHP 引擎池接入后清单自然扩大。
    _PROBE_WHITELIST = ("hermes", "opencode", "codearts", "codex", "aishell")

    def _probe_engines_shell(self, sandbox) -> list:
        """建箱层探测：对已建箱沙箱跑白名单引擎 --version。

        sandbox 只需提供 run_cmd(cmd, timeout) -> (exit_code, stdout)
        （CloudSandboxBase 平台原语，沙箱实例即满足）。
        返回统一格式 [{"engine", "version", "source": "shell"}]。
        [修复 2026-11-03] E2E 实证：bash 的 "cmd 2>&1 | head -1" 在命令不存在时
        exit code 取自 head（=0）且 stdout 变成 "command not found" 错误文本——
        之前误判为已安装。增加输出语义校验：含 not found/No such file 的
        输出一律视为未安装。
        """
        out = []
        _MISS_MARKS = ("command not found", "not found", "no such file", "is not recognized")
        for engine in self._PROBE_WHITELIST:
            try:
                ec, ver = sandbox.run_cmd(f"{engine} --version 2>&1 | head -1", timeout=15)
                _v = (ver or "").strip().splitlines()[0][:64] if (ver or "").strip() else ""
                _vl = _v.lower()
                if ec == 0 and _v and not any(m in _vl for m in _MISS_MARKS):
                    # [ACP 深检 2026-11-06] hermes 的 ACP 依赖是可选 extra（pip install
                    # hermes[acp]），--version 不触碰 ACP 模块——"有 hermes 无 ACP"的模板
                    # 会带病注册，任务期才以 ImportError 炸（acp_bootstrap.py 背景注记的
                    # 同型缺陷，本机引擎路径已修、沙箱模板探测路径此前未同步）。修复：
                    # --version 通过后对 `hermes acp` 发真实 initialize+session/new
                    # 握手（base64 免引号嵌套），响应含 jsonrpc 即 ACP 可用；失败/超时
                    # 不上报（宁缺勿假，与 spec §5.6 一致）。
                    if engine == "hermes":
                        # [握手公式复刻 weekly-10 2026-11-11] 旧 echo 短管道在 v0.19.0
                        # 原理性失效（<sandbox-host> 箱内实测三组对照）：
                        #   ① echo(立即 EOF) → adapter 处理完 initialize 但不写 stdout(0字节)
                        #   ② 首次 initialize 懒装 boto3 ~16s > timeout 15 → 必超时
                        #   ③ (echo...; sleep 40) stdin 握住 → 497B 完整 jsonrpc 响应
                        # weekly-10 三处本机探测(acp_bootstrap/local_probe/engine_probe)
                        # 全部是 Popen+stdin 握住+逐行读——移植到 run_cmd 单发命令环境，
                        # 用「写后握住 N 秒」管道复刻该语义：base64 解出两条请求(带换行)，
                        # sleep 握住 stdin 越过懒装窗口，grep -c 计数含 jsonrpc 的响应行。
                        _acp_ok = False
                        try:
                            _b64 = "eyJqc29ucnBjIjogIjIuMCIsICJpZCI6IDEsICJtZXRob2QiOiAiaW5pdGlhbGl6ZSIsICJwYXJhbXMiOiB7InByb3RvY29sVmVyc2lvbiI6IDEsICJjbGllbnRJbmZvIjogeyJuYW1lIjogImdyYXBlLXByb2JlIiwgInZlcnNpb24iOiAiMS4wIn0sICJjYXBhYmlsaXRpZXMiOiB7fX19CnsianNvbnJwYyI6ICIyLjAiLCAiaWQiOiAyLCAibWV0aG9kIjogInNlc3Npb24vbmV3IiwgInBhcmFtcyI6IHt9fQo="
                            _handshake = ("(printf '%s' '" + _b64 + "' | base64 -d; sleep 40) | "
                                          "timeout 60 hermes acp 2>/dev/null | grep -c jsonrpc")
                            _aec, _aout = sandbox.run_cmd(_handshake, timeout=70)
                            _cnt = 0
                            try:
                                _cnt = int(((_aout or "").strip().splitlines() or ["0"])[-1])
                            except Exception:
                                _cnt = 0
                            _acp_ok = (_aec == 0 and _cnt >= 1)
                        except Exception:
                            _acp_ok = False
                        if not _acp_ok:
                            continue  # 深检失败不上报——不注册调不通的引擎
                    out.append({"engine": engine, "version": _v, "source": "shell"})
            except Exception:
                continue  # 单引擎探测失败不拖垮整批
        return out

    def probe_engines(self, config: dict, template_id: str = "") -> list | None:
        """[引擎探测] 探测平台模板内预装引擎（元数据层 + 建箱层）。

        元数据层（免费）：CubeSandbox 模板 API 无预装清单元数据（envd_version
        是 envd 自身版本非引擎清单）——元数据层返回空，由调用方决定是否建箱层。
        建箱层（低成本）由 worker 侧调用 _probe_engines_shell（建箱→白名单扫描
        →kill，即建即杀幂等）。

        返回统一格式 [{"engine", "version", "source"}]；无探测结果返回 []
        （不是 None——None 语义留给"平台不支持探测"）。
        本方法职责：校验配置可达性 + 元数据层；建箱层由 worker 编排（成本决策
        在调用方）。
        """
        # 元数据层: cube 平台模板无引擎元数据 → 返回空, 调用方走建箱层
        return []


    # parse_config_file 用——判断哪些字段不入 extra_config
    _COMMON_FIELDS = {
        "api_url", "api_key", "max_sandboxes", "templates",
        "grape_server_url", "grape_cli_token",
        "request_timeout", "timeout", "pool_size",
        "_comment", "_fields",
    }

    # normalize_db_config + validate_extra_config 用——判断哪些字段安全传给 Provider
    _PROVIDER_FIELDS = {
        "platform", "name", "api_url", "api_key", "max_sandboxes", "provider_id",
        "ssl_cert_content", "sandbox_domain", "proxy_node_ip",
        "timeout", "request_timeout",
        "grape_server_url", "grape_cli_token",
        "egress_allow_out",
    }

    def build_provider(self, *, audit, credential, seccomp, alert_engine, config):
        return CubeSandboxProvider(
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
        if "ssl_cert_file" in extra:
            try:
                with open(extra["ssl_cert_file"], "r") as f:
                    extra["ssl_cert_content"] = f.read()
                del extra["ssl_cert_file"]
            except Exception:
                pass
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
        # [provider_id 断链修复 2026-10-30] provider-configs API 下发的字段是 "id"——
        # 映射为 provider_id 并入白名单(否则 state_reporter 上报 provider_id=0,
        # 可观测性按平台筛选失效的根因)
        cfg = {
            "platform": db_row.get("platform", "cubesandbox"),
            "name": db_row.get("name", "cubesandbox"),
            "api_url": db_row.get("endpoint", ""),
            "api_key": db_row.get("api_key", ""),
            "max_sandboxes": db_row.get("max_sandboxes", 10),
            "provider_id": coerce_provider_id(db_row.get("provider_id", 0) or db_row.get("id", 0)),
        }
        cfg.update(_extra)
        for key in list(cfg.keys()):
            if key not in known:
                _log.warning(f"[cubesandbox] unknown field '{key}' in db config, discarding")
                del cfg[key]
        return cfg

    def prepare_artifacts(self, config, workdir: str, provider_id: "int | str") -> None:
        cert_content = config.get("ssl_cert_content", "")
        if not cert_content:
            return
        cert_path = os.path.join(workdir, f"cube-rootCA-{provider_id}.pem")
        try:
            os.makedirs(workdir, exist_ok=True)
            old_umask = os.umask(0o077)
            try:
                with open(cert_path, "w") as f:
                    f.write(cert_content)
                os.chmod(cert_path, 0o600)
            finally:
                os.umask(old_umask)
            config["_ssl_cert_path"] = cert_path
            log.info(f"[cubesandbox] ssl_cert materialized to {cert_path} for provider_id={provider_id}")
        except Exception as e:
            log.warning(f"[cubesandbox] ssl_cert materialize failed for provider_id={provider_id}: {e}")

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
                _log.warning(f"[cubesandbox] unknown extra_config field '{key}' discarded")
        return result

    def health_check(self, config: dict) -> tuple:
        try:
            _prov = CubeSandboxProvider(config=config)
            result = _prov.health()
            if hasattr(result, "healthy"):
                return (result.healthy is True, getattr(result, "reason", ""))
            return (bool(result), "")
        except Exception as e:
            log.warning(f"[cubesandbox] health_check failed: {e}")
            return (False, "health check failed, see server log")

    def list_templates(self, config: dict) -> list[dict] | None:
        """CubeSandbox SDK: Template.list() → 统一格式。

        返回 None 表示扫描能力不可用（SDK 未装/平台不可达时抛异常由调用方处理）。
        Template.list 兼容 list 与 dict 包裹（templates/items）两种返回结构。
        """
        try:
            from cubesandbox import Config, Template
            cfg = Config(
                api_url=config.get("api_url", ""),
                api_key=config.get("api_key") or None,
                sandbox_domain=config.get("sandbox_domain", "cube.app"),
                request_timeout=int(config.get("request_timeout", 10)),
            )
            infos = Template.list(config=cfg)
            out = []
            for ti in infos:
                # 属性访问兼容 dataclass 与 dict 两种形态
                def _g(o, k, d=""):
                    v = getattr(o, k, None) if not isinstance(o, dict) else o.get(k)
                    return v if v is not None else d
                out.append({
                    "template_id": str(_g(ti, "template_id") or _g(ti, "templateID")),
                    "name": str(_g(ti, "name") or ""),
                    "status": str(_g(ti, "status") or "").lower(),
                    "description": str(_g(ti, "image_info") or ""),
                })
            return out
        except ImportError:
            return None

    def kill_sandbox(self, config: dict, sandbox_id: str) -> bool:
        try:
            _prov = CubeSandboxProvider(config=config)
            _prov.kill_sandbox(sandbox_id)
            return True
        except Exception as e:
            log.warning(f"[cubesandbox] kill {sandbox_id} failed: {e}")
            return False

    def ui_schema(self):
        from .api import FormSchema, FormGroup, FormField
        return FormSchema(groups=[
            FormGroup(title="SSL", fields=[
                FormField(name="ssl_cert_content", type="textarea",
                          label="SSL 证书 (PEM)", help="粘贴 CA 证书内容",
                          placeholder="-----BEGIN CERTIFICATE-----"),
            ]),
        ])

    def cleanup(self, provider_id: "int | str", workdir: str) -> None:
        from src.domains.runtime.agents.sandbox.backends.api import cert_artifact_key
        cert_path = os.path.join(workdir, f"cube-rootCA-{cert_artifact_key(provider_id, self._config)}.pem")
        try:
            os.remove(cert_path)
            log.info(f"[cubesandbox] cleaned up {cert_path}")
        except FileNotFoundError:
            pass
        except Exception as e:
            log.warning(f"[cubesandbox] cleanup {cert_path} failed: {e}")

    def build_transport(self, sandbox, hermes_bin: str, envs: dict):
        """[USP] 平台自决 PTY transport（executor 分叉收编）。"""
        return CubeSandboxTransport(sandbox, hermes_bin=hermes_bin, envs=envs)


PROVIDER = CubeSandboxProvider
ADAPTER = CubeSandboxAdapter()


# ===== USP ExecSession 平台实现 =====
# PtyHandle 异常语义（SDK 实测）：wrapper 退出码非 0 时 wait() 抛
# "PTY exited with error: exit status N"——退出码提取在本类内消化，
# 契约只承诺 wait() 返回 int（永不抛 SDK 异常）。

import re as _re


class _CubePtySession(ExecSession):
    """CubeSandbox PTY 会话——包装 SDK PtyHandle 为 ExecSession 协议。

    wait() 的 SDK 异常→退出码提取逻辑自 executor._PtyExecHandleAdapter
    （executor 时代 :1388-1405 正则 `exit (?:status|code) (\\d+)`）下沉至此。
    """

    def __init__(self, pty_handle):
        self._h = pty_handle

    def send_stdin(self, data):
        self._h.send_stdin(data)

    def close_stdin(self):
        # cube SDK PtyHandle 无 close_stdin（实测 SDK 源码 _pty.py）——no-op
        closer = getattr(self._h, "close_stdin", None)
        if closer is None:
            return
        try:
            closer()
        except Exception:
            # devbox CloseStdin 可能 500（file already closed）——契约容错
            pass

    def __iter__(self):
        return iter(self._h)

    def wait(self, timeout=None):
        try:
            return int(self._h.wait() or 0)
        except Exception as e:
            # SDK 在 wrapper exit_code != 0 时抛 "PTY exited with error: exit status N"
            # 提取退出码，不中断流程——上层根据 exit_code 判断成功/失败
            m = _re.search(r'exit (?:status|code) (\d+)', str(e))
            return int(m.group(1)) if m else -1

    def kill(self):
        self._h.kill()

    @property
    def pid(self):
        return self._h.pid


# ---- [USP] CubeSandboxTransport（自 hermes_acp.py 整块搬迁, 类代码零改动）----

class CubeSandboxTransport:
    """CubeSandbox 路径 transport——PTY 启动 hermes acp + fifo 桥 + files.read 读 stdout。

    CubeSandbox SDK 的 commands.run 只有同步执行（无 background），无法长驻进程。
    PTY 是唯一能启动长驻进程 + 发 stdin 的方式。但 PTY slave fd 在 epoll 上
    注册返回 EPERM——hermes 用 asyncio connect_read_pipe(sys.stdin) 会崩。

    方案：PTY 里用 shell 脚本启动 hermes acp，stdin 重定向到 fifo（普通 pipe）。
    bridge.py 桥用 select(2) 把 PTY slave 数据搬到 fifo（select 不受 EPERM 影响）。
    hermes 的 stdin 是 pipe，epoll OK。stdout 写文件，用 files.read 轮询读。

    数据流：
      send_stdin → PTY master → slave → bridge(select) → fifo → hermes stdin (pipe)
      hermes stdout → out.jsonl → files.read 轮询
      hermes 退出 → exit_code 文件出现 → files.exists → transport 关闭

    固定 2 个进程（hermes + bridge），不随时长增长。
    """

    _MAX_LINE = 10 * 1024 * 1024  # 10MB 单行上限

    def __init__(self, sandbox, hermes_bin="hermes",
                 workdir="/tmp/_grape_acp", envs=None):
        self.sandbox = sandbox
        self.hermes_bin = hermes_bin
        self.workdir = workdir
        self.out_path = f"{workdir}/out.jsonl"
        self.err_path = f"{workdir}/err.log"
        self.fifo_path = f"{workdir}/hermes_in"
        self.exit_code_path = f"{workdir}/exit_code"
        self.bridge_path = "/tmp/bridge.py"
        self.read_offset = 0
        self.buf = b""
        self.pty_handle = None  # CubeSandbox PtyHandle
        self._started = False
        self._write_lock = threading.Lock()
        self._envs = envs or {}

    def start(self):
        """通过 PTY 启动 hermes acp + fifo 桥。

        PTY 里执行 shell 脚本：
        1. stty raw -echo（关行规则，禁止 \\n→\\r\\n、禁止 echo 回环）
        2. mkfifo hermes_in
        3. hermes acp < hermes_in > out.jsonl 2> err.log &（hermes stdin 是 pipe）
        4. python3 bridge.py &（桥：PTY slave → fifo）
        5. wait hermes → echo exit_code
        6. wait bridge（桥自行退出）
        """
        if self._started:
            raise RuntimeError("already started; call close() first")

        # 注入 bridge.py 到沙箱
        # [USP搬迁修正] bridge.py 在 src/agent/sandbox/wrappers/——本文件已迁至
        # backends/，__file__ 相对层级变了（原 hermes_acp.py 的 parent=src/agent）
        from pathlib import Path as _Path
        _bridge_src = (_Path(__file__).resolve().parent.parent / "wrappers" / "bridge.py")
        try:
            _bridge_code = _bridge_src.read_text()
        except Exception as e:
            raise RuntimeError(f"bridge.py not found: {e}")
        self.sandbox.write_file(self.bridge_path, _bridge_code)

        # 构建 envs 字符串（shell export 格式）
        _env_exports = " ".join(f"{k}={shlex.quote(str(v))}" for k, v in self._envs.items())

        # 上传启动脚本到沙箱文件（不用 send_stdin 发多行命令，避免 raw 模式影响）
        _start_script = (
            "#!/bin/sh\n"
            f"export PATH=$PATH:/usr/local/bin:/tmp/pylibs/bin\n"
            f"export {_env_exports}\n"
            "stty raw -echo 2>/dev/null\n"
            f"D={self.workdir}\n"
            "mkdir -p $D\n"
            "rm -f $D/hermes_in $D/acp_out $D/exit_code $D/out.jsonl $D/err.log $D/bridge_rx.txt\n"
            "mkfifo $D/hermes_in $D/acp_out\n"
            "cat $D/acp_out > $D/out.jsonl &\n"
            "CPID=$!\n"  # P2-7 修复：记录 cat PID 用于 close 时清理
            f"PYTHONUNBUFFERED=1 {self.hermes_bin} acp < $D/hermes_in > $D/acp_out 2> $D/err.log &\n"
            "HPID=$!\n"
            "TTY_PATH=$(tty 2>/dev/null || echo /dev/tty)\n"
            f"python3 {self.bridge_path} $TTY_PATH < /dev/null &\n"
            "BPID=$!\n"
            "wait $HPID\n"
            'echo $? > $D/exit_code.tmp && mv $D/exit_code.tmp $D/exit_code\n'
            "kill $CPID 2>/dev/null\n"  # P2-7 修复：hermes 退出后 kill cat
            "wait $BPID 2>/dev/null\n"
        )
        self.sandbox.write_file("/tmp/start_hermes.sh", _start_script)

        try:
            # [沙箱迁移] envs 传给 PTY 进程（启动脚本内 export 是兜底；PTY 本体 env
            # 确保所有子进程可见 GRAPE_SCENE_ID 等变量——agent 用 $GRAPE_SCENE_ID 查
            # 场景节点判定 QA A2A，env 缺失会误判"场景未配置"）
            # [USP] PTY 收编：SDK 私有穿透 → ExecSession 契约（PTYSize 封装在
            # backends/cubesandbox.pty_create 内）
            _pty_envs = {k: str(v) for k, v in (self._envs or {}).items()}
            self.pty_handle = self.sandbox.pty_create(
                rows=50, cols=200,
                cwd=None,
                envs=_pty_envs,
                timeout=None,
            )
            # 执行脚本文件
            self.pty_handle.send_stdin("sh /tmp/start_hermes.sh\n")
            time.sleep(1.0)  # 等 hermes 启动

            # 健康检查：读 err.log 看有没有启动错误
            _stderr = self._tail_stderr()
            if "Traceback" in _stderr or "Error" in _stderr:
                raise RuntimeError(f"hermes acp startup error; stderr: {_stderr[:500]}")

            self._started = True
            log.info("CubeSandboxTransport started (PTY + fifo bridge)")
        except Exception:
            self.close()
            raise

    def write(self, data: bytes) -> None:
        """通过 PTY send_stdin 写数据→ bridge → fifo → hermes stdin。"""
        with self._write_lock:
            if isinstance(data, str):
                data = data.encode("utf-8")
            self.pty_handle.send_stdin(data)

    def readline(self, timeout: float = 30.0) -> str:
        """从 out.jsonl 增量读取，凑成完整行返回。同 DevBoxTransport。"""
        deadline = time.monotonic() + timeout
        while b"\n" not in self.buf:
            if len(self.buf) > self._MAX_LINE:
                raise TransportClosed(f"line exceeds {self._MAX_LINE} bytes")
            chunk = self._read_new()
            if not chunk:
                if self._exited():
                    chunk = self._read_new()
                    self.buf += chunk
                    if b"\n" not in self.buf:
                        raise TransportClosed(self._tail_stderr())
                    break
                if time.monotonic() > deadline:
                    raise TransportTimeout(
                        f"readline timeout after {timeout}s silent; "
                        f"stderr: {self._tail_stderr()}"
                    )
                time.sleep(0.5)
                continue
            self.buf += chunk
            deadline = time.monotonic() + timeout
        line, self.buf = self.buf.split(b"\n", 1)
        return line.decode("utf-8") + "\n"

    def _read_new(self) -> bytes:
        """从 out.jsonl 增量读取——files.read 零进程读取。"""
        try:
            data = self.sandbox.read_file_bytes(self.out_path)
        except Exception as e:
            log.warning(f"[cubesandbox] _read_new failed: {e}")
            return b""
        if not data:
            return b""
        if self.read_offset >= len(data):
            return b""
        new_data = data[self.read_offset:]
        self.read_offset = len(data)
        return new_data

    def _exited(self) -> bool:
        """检查 hermes 是否已退出——files.exists(exit_code) 零进程读取。"""
        try:
            return self.sandbox.file_exists(self.exit_code_path)
        except Exception:
            return False

    def _tail_stderr(self, n=2000) -> str:
        """读取 stderr 文件尾部用于错误诊断——files.read 零进程读取。"""
        try:
            data = self.sandbox.read_file_bytes(self.err_path)
            if isinstance(data, bytes):
                text = data.decode("utf-8", errors="replace")
            else:
                text = str(data)
            return text[-n:] if len(text) > n else text
        except Exception as e:
            log.warning(f"[cubesandbox] _tail_stderr failed: {e}")
            return ""

    def close(self):
        """kill PTY + 清理文件。"""
        if self.pty_handle:
            try:
                self.pty_handle.kill()
            except Exception:
                pass
            self.pty_handle = None
        try:
            self.sandbox.run_command(
                f"rm -rf {self.workdir}", timeout=5, background=False, merge_credentials=False
            )
        except Exception:
            pass
        self._started = False
        self.read_offset = 0
        self.buf = b""
