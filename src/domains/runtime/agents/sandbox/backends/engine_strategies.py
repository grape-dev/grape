"""引擎初始化策略——模板方法 + 能力声明 + EnginePlugin 绑定。

设计原则：
- 假多维：2 agent × 2 platform，相似度 50-60%，用默认+特化处理
- DefaultInstaller 模板方法：verify → install → runtime_deps → entry_points → assert
- DefaultConfigurator 模板方法：build_config → write → inject_env_var
- 平台差异通过 PlatformAdapter 能力声明吸收，Installer 不用字符串比较平台名
- run_long_cmd 总是存在，DevBoxAdapter fallback 到 run_cmd
- cred 进 ctx（ctx.llm_cred），install/configure 签名统一
- inject_env_var 通用辅助，不在 PlatformAdapter Protocol 上
- EnginePlugin 绑定 Installer+Configurator，注册一次
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Callable, Protocol

log = logging.getLogger(__name__)


# ===== SandboxContext：策略操作的唯一接口 =====

@dataclass
class SandboxContext:
    """策略对象操作沙箱的唯一接口——平台原语 + LLM cred + 环境变量注入。

    策略不依赖沙箱类，测试时传 fake context。
    cred 作为 ctx 字段（不是游离参数），install(ctx)/configure(ctx) 签名统一。
    inject_env_var 是通用辅助（两个平台逻辑一致），不在 PlatformAdapter Protocol 上。
    """

    # ---- 平台原语 ----
    name: str
    supports_long_cmd: bool
    needs_entry_point_wrapper: bool
    run_cmd: Callable[..., tuple[int, str]]
    run_long_cmd: Callable[..., tuple[int, str]]
    write_file: Callable[[str, "str | bytes"], None]
    get_home: Callable[[], str]
    check_environment: Callable[[], None]

    # ---- 数据 ----
    llm_cred: dict | None = None    # 由编排层写入，install/configure 读取
    repo_url: str = ""              # 仓库 clone URL（_init_sandbox 第 7 步用）
    bot_token: str = ""             # git clone 凭据
    work_branch: str = ""           # 工作分支
    platform: str = ""              # 沙箱平台（bwrap=本地可 fallback 安装；云平台=cubesandbox/devbox 等）

    # ---- 通用辅助 ----
    _env_vars: dict = field(default_factory=dict)

    def inject_env_var(self, name: str, value: str) -> None:
        """注入环境变量——agent 进程通过 env 读取 API key。

        两个平台逻辑一致：存到 dict，run_command 时合并到 envs。
        不在 PlatformAdapter Protocol 上——它不是平台能力。
        """
        self._env_vars[name] = value

    @property
    def env_vars(self) -> dict:
        """已注入的环境变量（基类 run_command 时合并到 envs）。"""
        return dict(self._env_vars)


# ===== PlatformAdapter Protocol（瘦身版）=====

class PlatformAdapter(Protocol):
    """平台执行能力接口——CloudSandboxBase 子类实现。

    只包含真正按平台分叉的能力，不含通用操作（如环境变量注入）。
    """

    name: str
    supports_long_cmd: bool
    needs_entry_point_wrapper: bool

    def run_cmd(self, cmd: str) -> tuple[int, str]: ...
    def run_long_cmd(self, cmd: str, timeout: int) -> tuple[int, str]: ...
    def write_file(self, path: str, content: "str | bytes") -> None: ...
    def get_home(self) -> str: ...
    def check_environment(self) -> None: ...


# ===== DefaultInstaller：模板方法 =====

class DefaultInstaller:
    """通用安装流程模板——子类只 override 差异点。

    流程：预装验证 → fallback 安装 → 运行时依赖 → 入口点 → 版本断言
    平台差异通过 PlatformAdapter 能力声明吸收，不用字符串比较平台名。
    """

    binary: str = ""
    install_cmd: str = ""
    supported_agents: set[str] = {"*"}
    supported_platforms: set[str] = {"*"}

    def install(self, ctx: SandboxContext) -> None:
        # 1. 预装验证
        if self._verify_preinstalled(ctx):
            log.info(f"{self.binary} pre-installed")
        else:
            # 2. fallback 安装——仅本地沙箱（bwrap）允许。
            # 云沙箱（cubesandbox/devbox 等模板固化镜像）不支持现场安装：
            # 慢网沙箱内下载引擎会卡死建箱链（event29 同款教训：lazy pip 挂起 100s 超时）；
            # 引擎缺失=模板问题，应明确报错让用户换模板/修模板，而非现场下载掩盖。
            if (ctx.platform or "").lower() not in ("", "bwrap", "local"):
                raise RuntimeError(
                    f"{self.binary} 未在模板中预装（platform={ctx.platform} 云沙箱不支持自动安装引擎）——"
                    f"请使用预装该引擎的模板，或在平台侧为模板补充引擎后重新扫描")
            self._do_install(ctx)
        # 3. 安装后处理——探测路径、写 .bashrc（子类 override）
        self._post_install(ctx)
        # 4. 运行时依赖（默认空，子类 override）
        self._ensure_runtime_deps(ctx)
        # 5. 入口点（默认空，子类 override）
        self._ensure_entry_points(ctx)
        # 6. 版本断言
        self._assert_version(ctx)

    def _post_install(self, ctx: SandboxContext) -> None:
        """安装后处理——默认空，子类 override 探测路径 + 写 .bashrc。"""
        pass

    def _verify_preinstalled(self, ctx: SandboxContext) -> bool:
        ec, _ = ctx.run_cmd(f"{self.binary} --version 2>&1")
        return ec == 0

    def _do_install(self, ctx: SandboxContext) -> None:
        ec, tail = ctx.run_long_cmd(self.install_cmd, 1200)
        if ec != 0:
            raise RuntimeError(f"{self.binary} install failed (exit={ec}): {tail}")

    def _ensure_runtime_deps(self, ctx: SandboxContext) -> None:
        pass

    def _ensure_entry_points(self, ctx: SandboxContext) -> None:
        pass

    def _assert_version(self, ctx: SandboxContext) -> None:
        ec, ver = ctx.run_cmd(f"{self.binary} --version 2>&1 | head -1")
        if ec != 0:
            raise RuntimeError(f"{self.binary} version check failed: {ver}")

    def _has_python_pkg(self, ctx: SandboxContext, name: str) -> bool:
        """工具方法——检测 Python 包是否已安装。"""
        ec, _ = ctx.run_cmd(f"python3 -c 'import {name}' 2>&1")
        return ec == 0


class HermesInstaller(DefaultInstaller):
    """hermes-agent 安装——支持 cubesandbox + devbox。

    安装方式：官方一键安装脚本（国内镜像），脚本自动装 Python 3.11 + uv + hermes-agent。
    HERMES_INSTALL_DIR=/tmp/hermes：DevBox 根分区太小，装到 /tmp（tmpfs）。

    通用路径处理：安装后通过 `which hermes` 探测实际 bin 路径，
    推导 venv Python 路径，写入 ~/.bashrc。
    后续所有命令通过 SDK 的 /bin/bash -l -c（login shell）自动读 .bashrc 获取 PATH。
    不硬编码任何路径。
    """

    binary = "hermes"
    # hermes-agent 核心依赖要求 Python >=3.14（pyproject.toml 中 ; python_version >= '3.14'）
    # 一键脚本默认装 Python 3.11（过渡版本），PYTHON_VERSION=3.14 让脚本装 3.14 + 全部依赖
    # HERMES_INSTALL_DIR=/tmp/hermes：DevBox 根分区太小，装到 /tmp（tmpfs）
    install_cmd = (
        "export HERMES_INSTALL_DIR=/tmp/hermes PYTHON_VERSION=3.14 && "
        "curl -fsSL https://res1.hermesagent.org.cn/install.sh | bash"
    )
    acp_version = "0.9.0"
    supported_agents = {"hermes"}

    def _post_install(self, ctx: SandboxContext) -> None:
        """探测 hermes bin 路径 + 实际 Python 解释器，写入 ~/.bashrc。

        两种安装方式都支持：
        - install.sh 装的：有独立 venv（HERMES_INSTALL_DIR/venv/bin/python3）
        - 模板预装的：装在系统 Python site-packages，无独立 venv
        
        通用探测：which hermes → readlink -f 拿真实 bin 路径 → 写 .bashrc
        Python 探测优先级：
        1. bin_dir/../venv/bin/python3（install.sh 方式）
        2. hermes --version 输出的 Install directory 里的 venv
        3. python3（系统 Python fallback——预装模板场景）
        """
        # readlink -f 解析符号链接——which hermes 可能返回 ~/.local/bin/hermes（链接）
        ec, hermes_path = ctx.run_cmd("readlink -f $(which hermes 2>/dev/null) 2>/dev/null || which hermes 2>/dev/null || echo ''")
        hermes_path = hermes_path.strip()
        if not hermes_path:
            log.warning("hermes binary not found after install")
            return

        bin_dir = hermes_path.rsplit('/', 1)[0]  # 真实 bin 目录

        # 方案 A：写 ~/.bashrc + ~/.bash_profile + ~/.profile（幂等追加）
        # 原因：hermes terminal init_session 用 bash -l -c（non-interactive login shell），
        # ~/.bashrc 的 interactivity guard 阻止 export 执行。
        # bash -l 优先读 ~/.bash_profile（存在则不读 ~/.profile），所以两个都写。
        _path_export = f"export PATH=$PATH:{bin_dir}"
        for _rc_file in ("~/.bashrc", "~/.bash_profile", "~/.profile"):
            ctx.run_cmd(
                f"grep -q '{bin_dir}' {_rc_file} 2>/dev/null || echo '{_path_export}' >> {_rc_file}"
            )
        log.info(f"hermes PATH written to ~/.bashrc + ~/.bash_profile + ~/.profile: {bin_dir}")

        # 探测 Python 解释器——优先 venv，fallback 系统 python3
        venv_python = f"{bin_dir}/../venv/bin/python3"
        ec, py_check = ctx.run_cmd(f"test -f {venv_python} && echo ok || echo missing")
        if "ok" not in py_check:
            # fallback 0: bin_dir 本身就是 venv 的 bin/（预装模板场景——hermes 装在
            # /opt/hermes-venv/bin/hermes，同目录的 python3 就是它的解释器。
            # 之前漏了这级：直接落到系统 python3 → ACP 依赖装错环境 →
            # hermes acp 启动报 "ACP dependencies not installed" 立即退出 →
            # initialize 100s 超时）
            _venv_self = f"{bin_dir}/python3"
            ec, _py_check0 = ctx.run_cmd(f"test -f {_venv_self} && echo ok || echo missing")
            if "ok" in _py_check0:
                venv_python = _venv_self
            else:
                # fallback 1: 从 hermes --version 输出提取 Install directory
                ec, ver_out = ctx.run_cmd("hermes --version 2>&1")
                for line in (ver_out or '').split('\n'):
                    if 'Install directory:' in line:
                        install_dir = line.split('Install directory:')[1].strip()
                        venv_python = f"{install_dir}/venv/bin/python3"
                        break
                # 验证 fallback 1 的路径
                ec, py_check2 = ctx.run_cmd(f"test -f {venv_python} && echo ok || echo missing")
                if "ok" not in py_check2:
                    # fallback 2: 系统 python3（hermes 装在系统 site-packages 无 venv）
                    venv_python = "python3"
                    log.info("hermes has no venv, using system python3 for deps")

        # 缓存 Python 解释器路径 + hermes 绝对路径（[沙箱迁移] PTY 启动脚本非 login
        # shell 不读 ~/.bashrc 的 PATH export——_find_hermes_bin 需要绝对路径，
        # 否则 hermes_bin 退化为裸命令名 → PTY 里 'hermes acp' not found →
        # initialize 100s 超时）
        ctx.inject_env_var("_HERMES_VENV_PYTHON", venv_python)
        ctx.inject_env_var("_HERMES_BIN", hermes_path)

    # 核心依赖——pyproject.toml 中 ; python_version >= '3.14' 条件导致 3.11 venv 跳过
    # 直接列出 hermes acp 启动所需的核心包，用 pip install 逐个装
    _core_deps = (
        "ruamel.yaml==0.18.16 python-dotenv==1.2.2 fire==0.7.1 "
        "httpx[socks]==0.28.1 rich==14.3.3 tenacity==9.1.4 jinja2==3.1.6 "
        "pydantic==2.13.4 prompt_toolkit==3.0.52 openai==2.24.0 "
        "certifi==2026.5.20 requests==2.33.0 packaging==26.0 "
        "Markdown==3.10.2 tzdata==2025.3 pathspec==1.1.1 websockets==15.0.1 "
        "psutil==6.1.0"
    )

    # ACP 协议依赖：agent-client-protocol==0.9.0（pyproject.toml [acp] extra）
    # 不用 hermes pm install——该子命令在 hermes v0.20.0 不存在。
    # 直接 pip install，和核心依赖同一策略。
    _acp_dep = "agent-client-protocol==0.9.0"

    def _ensure_runtime_deps(self, ctx: SandboxContext) -> None:
        """运行时依赖——hermes 缺依赖时用探测到的 Python 解释器装。

        支持两种场景：
        - install.sh 装的 venv Python（_HERMES_VENV_PYTHON = /tmp/hermes/venv/bin/python3）
        - 模板预装无 venv（_HERMES_VENV_PYTHON = python3，系统 Python）
        
        1. ensurepip 确保 pip 可用
        2. pip install 核心依赖（固定版本，避免 ResolutionImpossible）
        3. pip install agent-client-protocol（ACP 协议依赖）
        """
        venv_py = ctx._env_vars.get("_HERMES_VENV_PYTHON", "")
        if not venv_py:
            # fallback: 直接用系统 python3（_post_install 正常不会走到这里）
            venv_py = "python3"
        log.info(f"hermes _ensure_runtime_deps: venv_py={venv_py}")

        # 0. 幂等探测——全部依赖已就位则直接返回（hwtunnel 断流重连后避免重跑）
        ec, _ = ctx.run_cmd(
            f"{venv_py} -c 'import ruamel.yaml, acp; from acp.schema import ModelInfo' 2>&1"
        )
        log.info(f"hermes deps idempotent check: ec={ec}")
        # [沙箱迁移] boto3 预装（event#29 根因）：hermes ACP initialize →
        # resolve_runtime_provider → resolve_provider 顺序 import provider 适配器
        # （含 bedrock_adapter）→ lazy_deps 触发 uv pip install boto3——在沙箱内
        # 国际网慢（resolve 43s + botocore 15MB 下载），initialize 100s 超时。
        # 放在幂等 return 之前：模板 venv 已含 acp/ruamel 但不含 boto3，
        # 幂等跳过后 lazy install 仍会在运行时卡住 initialize。
        ec_boto, _ = ctx.run_cmd(f"{venv_py} -c 'import boto3' 2>&1")
        if ec_boto != 0:
            # 版本钉 lazy_deps 的 provider.bedrock spec（boto3==1.42.89）——装最新版
            # _is_satisfied 版本不匹配仍会触发运行时重装卡 initialize
            ec_uv, tail_uv = ctx.run_long_cmd(
                "uv pip install --python " + venv_py + " 'boto3==1.42.89'"
                " --index-url https://pypi.tuna.tsinghua.edu.cn/simple 2>&1 | tail -3",
                180,
            )
            log.info(f"hermes boto3 preinstall: ec={ec_uv} tail={tail_uv[:200]}")
        if ec == 0:
            log.info("hermes venv deps already satisfied, skipping install")
            return

        # 1. 确保 venv 有 pip
        ec, _ = ctx.run_cmd(f"{venv_py} -c 'import pip' 2>&1")
        if ec != 0:
            ctx.run_long_cmd(f"{venv_py} -m ensurepip --upgrade 2>&1 | tail -3", 60)

        # 2. 核心依赖——检测 ruamel 哨兵，缺失则装全部核心依赖
        ec, _ = ctx.run_cmd(f"{venv_py} -c 'import ruamel' 2>&1")
        log.info(f"hermes ruamel check: ec={ec}")
        if ec != 0:
            ec_pip, tail = ctx.run_long_cmd(
                f"{venv_py} -m pip install {self._core_deps}"
                " -i https://pypi.tuna.tsinghua.edu.cn/simple 2>&1 | tail -5",
                600,
            )
            log.info(f"hermes core deps pip install: ec={ec_pip} tail={tail[:200]}")

        # 3. ACP 协议依赖——直接 pip install agent-client-protocol
        ec, _ = ctx.run_cmd(f"{venv_py} -c 'import acp' 2>&1")
        log.info(f"hermes acp check: ec={ec}")
        if ec != 0:
            # [沙箱迁移] 预装模板的 venv 无 pip 模块（uv 创建的裸 venv）——
            # -m pip 直接报 "No module named pip"，install 静默失败 →
            # hermes acp 启动报 "ACP dependencies not installed" 立即退出。
            # 先 ensurepip 引导，再装。
            _ec_ep, _ = ctx.run_cmd(f"{venv_py} -m ensurepip 2>&1")
            if _ec_ep != 0:
                log.warning(f"hermes venv ensurepip failed: ec={_ec_ep}")
            ec_pip, tail = ctx.run_long_cmd(
                f"{venv_py} -m pip install {self._acp_dep}"
                " -i https://pypi.tuna.tsinghua.edu.cn/simple 2>&1 | tail -5",
                120,
            )
            log.info(f"hermes acp dep pip install: ec={ec_pip} tail={tail[:200]}")
            # 验证 ModelInfo 可导入（acp 0.9.0 的 schema 必须有）
            ec, _ = ctx.run_cmd(f"{venv_py} -c 'from acp.schema import ModelInfo' 2>&1")
            if ec != 0:
                log.error("acp installed but ModelInfo import failed — version mismatch?")
    def _ensure_entry_points(self, ctx: SandboxContext) -> None:
        """hermes 一键脚本自己创建 entry point，不需要 wrapper。

        _post_install 已把 PATH 写入 ~/.bashrc，
        后续命令通过 SDK 的 /bin/bash -l -c 自动获取 PATH。
        """
        # 验证 hermes 命令可找到（通过 login shell 的 PATH）
        ec, _ = ctx.run_cmd(f"{self.binary} --version 2>&1 | head -1")
        if ec == 0:
            log.info("hermes entry point OK (script-installed)")
        else:
            log.warning("hermes entry point not found after install")


class OpenCodeInstaller(DefaultInstaller):
    """opencode 安装——支持 cubesandbox + devbox。

    安装方式：npm install -g opencode-ai（用 npmmirror 国内镜像加速）。
    fallback: curl -fsSL https://opencode.ai/install | bash

    国内镜像：DevBox 沙箱在国内云上，直连 npm/opencode.ai 慢/失败，
    先设 npmmirror 镜像源再用 npm 安装。
    无运行时依赖、无 entry_points 需求。
    """

    binary = "opencode"
    # 先设 npm 国内镜像，再用 npm 安装 opencode-ai
    install_cmd = (
        "npm config set registry https://registry.npmmirror.com --global 2>/dev/null; "
        "npm install -g opencode-ai 2>&1 | tail -5"
    )
    supported_agents = {"opencode"}
    # 无 _ensure_runtime_deps / _ensure_entry_points override


# ===== DefaultConfigurator：模板方法 =====

class DefaultConfigurator:
    """通用配置流程模板——子类只 override _build_config + _config_path。

    平台差异为零（两个平台的配置内容完全一致）。
    cred 从 ctx.llm_cred 读取，不是独立参数。
    """

    def configure(self, ctx: SandboxContext) -> None:
        cred = ctx.llm_cred
        if not cred or not cred.get("api_key"):
            log.warning(f"no LLM credentials, config skipped for {ctx.name}")
            return
        content, env_var = self._build_config(cred)
        home = ctx.get_home()
        path = self._config_path(home)
        import shlex
        dir_path = '/'.join(path.rsplit('/', 1)[:-1])
        ctx.run_cmd(f"mkdir -p {shlex.quote(dir_path)}")
        ctx.write_file(path, content)
        ctx.inject_env_var(env_var, cred.get("api_key", ""))
        self._inject_extra_env(ctx)
        log.info(f"engine config written to {path}")

    def _inject_extra_env(self, ctx: SandboxContext) -> None:
        """子类 override 注入额外环境变量（如 Bun 缓冲修复）。默认空。"""
        pass

    def _build_config(self, cred: dict) -> tuple[str, str]:
        """返回 (config_content, env_var_name)。子类必须 override。"""
        raise NotImplementedError

    def _config_path(self, home: str) -> str:
        """返回配置文件完整路径。子类必须 override。"""
        raise NotImplementedError


class HermesConfigurator(DefaultConfigurator):
    """hermes 配置：~/.hermes/config.yaml，api_key: ${ENV_VAR}。"""

    def _build_config(self, cred: dict) -> tuple[str, str]:
        api_key = cred.get("api_key", "")
        base_url = cred.get("base_url", "")
        model = cred.get("model", "gpt-4o")
        provider = cred.get("provider", "openai")

        # 当前每个引擎实例只激活一个 provider，env_var 名不承载 provider 语义
        env_var = "HERMES_API_KEY"

        # hermes 内部只认 openai/custom 两种 provider 模式
        # provider 字段(zhipu/deepseek/qwen)是描述性的，hermes 用 base_url 有无判断
        hermes_provider = "custom" if base_url else "openai"

        hermes_yaml = (
            f"model:\n"
            f"  default: {model}\n"
            f"  provider: {hermes_provider}\n"
            f"  base_url: {base_url or 'https://api.openai.com/v1'}\n"
            f"  api_key: ${{{env_var}}}\n"
            f"  # llm_provider: {provider}  # 描述性，便于排查\n"
            f"database:\n"
            f"  journal_mode: wal\n"
            f"agent:\n"
            f"  max_turns: 500\n"
            f"security:\n"
            f"  allow_lazy_installs: false\n"
            f"  tirith_enabled: false\n"
        )
        return (hermes_yaml, env_var)

    def _config_path(self, home: str) -> str:
        return f"{home}/.hermes/config.yaml"


class OpenCodeConfigurator(DefaultConfigurator):
    """opencode 配置：~/.config/opencode/opencode.json，apiKey: {env:VAR}。

    opencode 使用 {env:VAR_NAME} 语法引用环境变量，不是 ${VAR}。
    key 不落盘，通过 inject_env_var 注入到进程环境。
    """

    def _build_config(self, cred: dict) -> tuple[str, str]:
        import json
        api_key = cred.get("api_key", "")
        base_url = cred.get("base_url", "")
        model = cred.get("model", "gpt-4o")
        provider = cred.get("provider", "openai")

        env_var = "OPENCODE_API_KEY"

        provider_name = "custom-llm" if base_url else "openai"
        opencode_json = {
            "$schema": "https://opencode.ai/config.json",
            "provider": {
                provider_name: {
                    "npm": "@ai-sdk/openai-compatible",
                    "name": provider,  # opencode 用真实 provider 名做展示名
                    "options": {
                        "baseURL": base_url or "https://api.openai.com/v1",
                        "apiKey": f"{{env:{env_var}}}",  # opencode 插值语法，不落明文
                    },
                    "models": {model: {"name": model}},
                }
            },
        }
        return (json.dumps(opencode_json, indent=2), env_var)

    def _config_path(self, home: str) -> str:
        return f"{home}/.config/opencode/opencode.json"


class CodeartsInstaller(DefaultInstaller):
    """codearts 安装——模板预装验证 + PATH 设置。

    codearts 是华为内部工具，无公开 npm 包或安装脚本。
    依赖镜像预装（类似 hermes-acp 模板预装 hermes）。
    未预装时报错（不 fallback——无公开安装源）。

    安装验证：codearts --version（exit=0 即已安装）
    PATH 设置：写 ~/.bashrc + ~/.bash_profile + ~/.profile
    环境变量：通过 CodeartsConfigurator 注入，不在 Installer 设置
    """

    binary = "codearts"
    install_cmd = ""  # 空——不 fallback 安装
    supported_agents = {"codearts"}

    def _verify_preinstalled(self, ctx: SandboxContext) -> bool:
        """验证 codearts 已预装。"""
        ec, _ = ctx.run_cmd("codearts --version 2>&1 || ~/.codeartsdoer/installers/codearts --version 2>&1")
        if ec != 0:
            return False
        # models-cache.json 存在性检查（xcenter 503 时 codearts 依赖此文件）
        ec2, _ = ctx.run_cmd("test -f ~/.cache/codeartsdoer/models-cache.json && echo ok")
        if ec2 != 0:
            log.warning("codearts models-cache.json not found—xcenter 503 will be fatal")
        return True

    def _do_install(self, ctx: SandboxContext) -> None:
        """codearts 无公开安装源——直接报错。"""
        raise RuntimeError(
            "codearts is not pre-installed in sandbox template. "
            "Please use a CubeSandbox template with codearts pre-installed "
            "(~/.codeartsdoer/installers/)."
        )

    def _post_install(self, ctx: SandboxContext) -> None:
        """写 PATH 到 ~/.bashrc + ~/.bash_profile + ~/.profile。

        codearts 安装在 ~/.codeartsdoer/installers/，不在标准 PATH 中。
        commands.run 用 bash -l -c（login shell），会 source ~/.bash_profile。
        """
        _path_export = 'export PATH=$PATH:~/.codeartsdoer/installers'
        for _rc_file in ("~/.bashrc", "~/.bash_profile", "~/.profile"):
            ctx.run_cmd(
                f"grep -q '.codeartsdoer/installers' {_rc_file} 2>/dev/null "
                f"|| echo '{_path_export}' >> {_rc_file}"
            )
        log.info("codearts PATH written to ~/.bashrc + ~/.bash_profile + ~/.profile")

    # 无 _ensure_runtime_deps——codearts 是编译二进制，无运行时依赖
    # 无 _ensure_entry_points——installers/codearts 是 bash wrapper，已是 entry point


class CodeartsConfigurator(DefaultConfigurator):
    """codearts 配置：~/.codeartsdoer/codearts_cli.json。

    三层兼容策略（支持自定义镜像）：
    1. 镜像有 codearts_cli.json 且可解析 → 读改写（只改 apiKey + baseURL，保留镜像的 provider/models/lsp）
    2. 镜像有 codearts_cli.json 但解析失败 → fallback 整文件写（从 cred 动态生成）
    3. 镜像无 codearts_cli.json → fallback 整文件写（从 cred 动态生成）

    fallback 不硬编码 provider/model——cred 必须包含 model + base_url，否则报错。
    不写明文 apiKey——用 {env:CODEARTS_API_KEY} 引用（Issue #156: enc:v3 陷阱）。
    """

    env_var = "CODEARTS_API_KEY"

    def configure(self, ctx: SandboxContext) -> None:
        """读改写——读镜像预装的 codearts_cli.json，只改 apiKey + baseURL。"""
        cred = ctx.llm_cred
        if not cred or not cred.get("api_key"):
            log.warning(f"no LLM credentials, config skipped for {ctx.name}")
            return

        home = ctx.get_home()
        path = self._config_path(home)
        import json, shlex

        # 读镜像预装的 codearts_cli.json（可能不存在——自定义镜像场景）
        ec, existing = ctx.run_cmd(f"cat {shlex.quote(path)} 2>/dev/null")
        if ec == 0 and existing.strip():
            try:
                cfg = json.loads(existing)
                # 读改写：删 apiKey + 加 env 字段 + 改 baseURL（cred 有就改）
                # 保留镜像的 provider/models/api/lsp/$schema 等配置
                #
                # codearts DB migration 会把 config 中的 apiKey 加密成 enc:v3（绑定
                # hostname:username）。CubeSandbox 每次创建 hostname 不同 → enc:v3 解密
                # 失败 → apiKey 丢失 → 401。
                #
                # 方案：用 codearts 官方 env 字段（provider.<id>.env）绕过 config 文件
                # 的 apiKey 字段。codearts 在读取 API key 时直接从环境变量取值，不经过
                # 加密/解密流程，migration 不会干扰。
                # 参考：华为云帮助中心 — provider.<提供商ID>.env 环境变量回退机制
                for provider in cfg.get("provider", {}).values():
                    if isinstance(provider, dict):
                        provider.setdefault("options", {}).pop("apiKey", None)
                        provider["env"] = [self.env_var]
                        if cred.get("base_url"):
                            provider["options"]["baseURL"] = cred["base_url"]
                ctx.write_file(path, json.dumps(cfg, indent=2, ensure_ascii=False))
                log.info(f"codearts config patched (apiKey + baseURL): {path}")
            except json.JSONDecodeError:
                log.warning(f"codearts_cli.json parse failed, falling back to full write")
                content, env_var = self._build_config(cred)
                ctx.write_file(path, content)
        else:
            # 文件不存在（镜像无 codearts_cli.json），从 cred 动态生成
            content, env_var = self._build_config(cred)
            ctx.write_file(path, content)
            log.info(f"codearts config written (full, from cred): {path}")

        # 注入环境变量
        ctx.inject_env_var(self.env_var, cred.get("api_key", ""))
        self._inject_extra_env(ctx)

    def _build_config(self, cred: dict) -> tuple[str, str]:
        """整文件写 fallback（镜像无 codearts_cli.json 场景）。

        从 cred 动态生成配置——不硬编码 provider/model。
        cred 必须包含 api_key + model + base_url，否则报错（不猜测）。
        """
        import json
        api_key = cred.get("api_key", "")
        base_url = cred.get("base_url", "")
        model = cred.get("model", "")
        provider = cred.get("provider", "")

        # cred 必须有 model 和 base_url——不猜测默认值
        if not model:
            raise ValueError(
                "codearts fallback config: cred has no model. "
                "User must configure model in LLM credentials."
            )
        if not base_url:
            raise ValueError(
                "codearts fallback config: cred has no base_url. "
                "User must configure base_url in LLM credentials."
            )

        # provider_name 从 cred.provider 取，fallback 用 base_url 的 host
        if provider:
            provider_name = provider
        else:
            from urllib.parse import urlparse
            provider_name = urlparse(base_url).hostname or "custom-llm"

        codearts_json = {
            "$schema": "https://opencode.ai/config.json",
            "provider": {
                provider_name: {
                    "name": provider or provider_name,
                    "api": "openai",
                    "env": [self.env_var],
                    "options": {
                        "baseURL": base_url,
                    },
                    "models": {
                        model: {"id": model, "name": model},
                    },
                }
            },
            "model": f"{provider_name}/{model}",
            "lsp": False,
        }
        return (json.dumps(codearts_json, indent=2, ensure_ascii=False), self.env_var)

    def _config_path(self, home: str) -> str:
        return f"{home}/.codeartsdoer/codearts_cli.json"

    def _inject_extra_env(self, ctx: SandboxContext) -> None:
        """codearts 专属环境变量——Bun stdout 缓冲修复 + AK/SK dummy 绕过认证检查。

        codearts 启动时检查 CODEARTS_CLI_AK/SK 环境变量，不存在则报错退出。
        bwrap 模式下从宿主机环境继承（_SAFE_TOKENS 保留），CubeSandbox 模式下
        沙箱是全新环境，必须显式注入。值为 dummy——codearts 走自定义 LLM provider
        （opengw），不用 AK/SK 做实际认证。
        """
        ctx.inject_env_var("BUN_STDOUT_BUFFERING", "none")
        ctx.inject_env_var("BUN_STDERR_BUFFERING", "none")
        ctx.inject_env_var("CODEARTS_CLI_AK", "dummy")
        ctx.inject_env_var("CODEARTS_CLI_SK", "dummy")


class CodexInstaller(DefaultInstaller):
    """codex 安装——npm 全局安装（OpenAI Codex CLI，公开包 @openai/codex）。

    安装方式：npm install -g @openai/codex（要求沙箱有 node/npm——官方
    cube-sandbox 镜像自带；无 npm 时报错提示换预制镜像）。
    与 hermes/opencode 同属"可现场安装"引擎（installable=true）。
    """

    binary = "codex"
    install_cmd = (
        "npm install -g @openai/codex 2>&1 | tail -3"
    )
    supported_agents = {"codex"}

    def _post_install(self, ctx: SandboxContext) -> None:
        """npm 全局装到标准 PATH（/usr/local/bin 或 nvm bin）——通常无需写 .bashrc，
        但 login shell 可能缺 nvm 路径，幂等补一次 which 探测到的 bin 目录。"""
        ec, codex_path = ctx.run_cmd("readlink -f $(which codex 2>/dev/null) 2>/dev/null || which codex 2>/dev/null || echo ''")
        codex_path = (codex_path or '').strip()
        if not codex_path:
            log.warning("codex binary not found after install")
            return
        bin_dir = codex_path.rsplit('/', 1)[0]
        if bin_dir and bin_dir not in ('/usr/local/bin', '/usr/bin', '/bin'):
            _path_export = f"export PATH=$PATH:{bin_dir}"
            for _rc_file in ("~/.bashrc", "~/.bash_profile", "~/.profile"):
                ctx.run_cmd(
                    f"grep -q '{bin_dir}' {_rc_file} 2>/dev/null || echo '{_path_export}' >> {_rc_file}"
                )
            log.info(f"codex PATH written: {bin_dir}")


class CodexConfigurator(DefaultConfigurator):
    """codex 配置：~/.codex/config.toml（OpenAI 兼容 provider）。

    codex 认 OPENAI_API_KEY 环境变量 + config.toml 的 model_provider 段。
    key 不落盘——config.toml 引用 env var（codex 支持 env 读取），
    通过 inject_env_var 注入进程环境（与 opencode 同策略）。
    """

    env_var = "OPENAI_API_KEY"

    def _build_config(self, cred: dict) -> tuple[str, str]:
        model = cred.get("model") or "gpt-4o"
        base_url = cred.get("base_url") or ""
        # OpenAI 官方端点不需要 base_url 覆盖；自定义兼容端点（opengw 等）写 base_url
        provider_block = f"""
[model_providers.grape]
name = "grape"
api_key_env_var = "OPENAI_API_KEY"
"""
        if base_url:
            provider_block += f'base_url = "{base_url}"\n'
        content = (
            f'model = "{model}"\n'
            f'model_provider = "grape"\n'
            f'{provider_block}'
        )
        return (content, self.env_var)


class HwcloudInstaller(DefaultInstaller):
    """hwcloud(aishell) 安装——预装模式（同 codearts：无公开安装源）。

    华为内部 CLI，依赖镜像预装。未预装时报错（不 fallback）。
    """

    binary = "hwcloud"
    install_cmd = ""  # 空——不 fallback 安装
    supported_agents = {"hwcloud"}

    def _do_install(self, ctx: SandboxContext) -> None:
        raise RuntimeError(
            "hwcloud(aishell) is not pre-installed in sandbox template. "
            "Please use a template with hwcloud CLI pre-installed."
        )


class HwcloudConfigurator(DefaultConfigurator):
    """hwcloud(aishell) 配置——AK/SK 环境变量注入（不写配置文件）。

    aishell 从 HUAWEICLOUD_SDK_AK/SK 环境变量认证（cloud-deploy skill 同款），
    预制镜像自带 CLI 配置；此处只透传凭据环境变量。
    llm_cred 缺失时跳过（aishell 无 LLM 凭据需求也允许——_inject_extra_env 独立执行）。
    """

    env_var = "HUAWEICLOUD_SDK_AK"

    def _build_config(self, cred: dict) -> tuple[str, str]:
        # aishell 不需要 LLM 模型配置——返回占位（configure 在无 api_key 时整体跳过）
        return ("", self.env_var)

    def _inject_extra_env(self, ctx: SandboxContext) -> None:
        cred = ctx.llm_cred or {}
        if cred.get("ak"):
            ctx.inject_env_var("HUAWEICLOUD_SDK_AK", str(cred.get("ak", "")))
        if cred.get("sk"):
            ctx.inject_env_var("HUAWEICLOUD_SDK_SK", str(cred.get("sk", "")))


# ===== EnginePlugin：绑定注册 =====

@dataclass(frozen=True)
class EnginePlugin:
    """引擎插件——Installer + Configurator 绑定。

    注册一次，两者成对出现。新增引擎只需加一个条目到 ENGINE_REGISTRY。
    """
    installer: DefaultInstaller
    configurator: DefaultConfigurator


# 云沙箱引擎注册表——本机 worker 支持的 5 类引擎（_BACKENDS）全部纳入云沙箱编排：
# - hermes / opencode / codex：可现场安装（install_cmd 非空）
# - codearts / hwcloud(aishell)：无公开安装源，预装模式（install_cmd 空，须预制镜像）
# 云沙箱与 Agent 运行时（本机引擎）支持范围已对齐。
ENGINE_REGISTRY: dict[str, EnginePlugin] = {
    "hermes":   EnginePlugin(HermesInstaller(),   HermesConfigurator()),
    "opencode": EnginePlugin(OpenCodeInstaller(), OpenCodeConfigurator()),
    "codearts": EnginePlugin(CodeartsInstaller(), CodeartsConfigurator()),
    "codex":    EnginePlugin(CodexInstaller(),    CodexConfigurator()),
    "hwcloud":  EnginePlugin(HwcloudInstaller(),  HwcloudConfigurator()),
}


def get_engine_plugin(engine_type: str) -> EnginePlugin | None:
    """获取引擎插件。返回 None 表示未知 engine_type，编排层跳过策略步骤。

    遗漏-2 修复：大小写容错——DB 中大写/混合大小写 engine_type 也能匹配。
    """
    return ENGINE_REGISTRY.get(engine_type.lower().strip())
