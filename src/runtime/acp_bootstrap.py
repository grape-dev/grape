"""hermes ACP 依赖自举：检查 + 自动补装（hermes-acp-auto-bootstrap-design.md）。

背景：hermes 的 ACP 依赖是可选 extra（pip install hermes[acp]），普通安装不带
agent-client-protocol。`hermes --version` 探测不出该缺失（不触碰 ACP 模块），
导致引擎注册为 healthy 但 ACP 调用永远失败（wanglei 机器实证，2026-10-10）。

本模块在 worker 注册引擎时对 hermes 做一次 `hermes acp --check`（官方自检命令）：
- 通过 → 无操作；
- 失败 → 从 hermes launcher 脚本解析其所属 venv，用该 venv 的 pip 补装
  agent-client-protocol==0.9.0（版本与 hermes pyproject 的 acp extra 一致），
  复查后返回最终结果；
- 任一步失败 → 返回 False（不抛异常、不重试，调用方将其计入 healthy=False）。

注意：必须装进 hermes 自己的 venv（launcher exec 指向的解释器环境），
装到系统环境或 worker 环境对 hermes 无效。
"""
import os
import re
import subprocess

from src.config import log

ACP_CHECK_TIMEOUT = 15        # hermes acp --check 超时（含 hermes 冷启动余量）
PIP_INSTALL_TIMEOUT = 120     # pip install 超时
ACP_PACKAGE = "agent-client-protocol==0.9.0"  # 与 hermes pyproject acp extra 一致


def _acp_check(cli: str) -> bool:
    """跑 `hermes acp --check`，退出码 0 视为 ACP 依赖可用。"""
    try:
        res = subprocess.run([cli, "acp", "--check"], capture_output=True, text=True,
                             timeout=ACP_CHECK_TIMEOUT)
        return res.returncode == 0
    except Exception:
        return False


def _hermes_venv_bin(cli: str) -> str:
    """从 hermes launcher 脚本解析其 venv bin 目录。

    本机实测 launcher 形态（~/.local/bin/hermes）：
        #!/usr/bin/env bash
        exec "/home/xxx/.hermes/hermes-agent/venv/bin/hermes" "$@"
    提取 exec 行中的绝对路径，返回其目录（venv/bin）。
    解析失败返回空串（Windows .cmd launcher 等形态暂不支持，走 False 路径）。
    """
    try:
        if not os.path.isfile(cli) or os.path.splitext(cli)[1].lower() in (".exe", ".cmd", ".bat"):
            return ""
        with open(cli, "r", encoding="utf-8", errors="replace") as f:
            content = f.read(4096)
        m = re.search(r'exec\s+"([^"\n]+/bin/hermes)"', content)
        if m and os.path.isfile(m.group(1)):
            return os.path.dirname(m.group(1))
    except Exception:
        pass
    return ""


def _pip_install_into(venv_bin: str) -> bool:
    """用指定 venv 的 pip 安装 ACP 包。pip 不可用时用 python -m pip 兜底。"""
    for cmd in ([os.path.join(venv_bin, "pip")], [os.path.join(venv_bin, "python"), "-m", "pip"]):
        try:
            res = subprocess.run(cmd + ["install", ACP_PACKAGE],
                                 capture_output=True, text=True, timeout=PIP_INSTALL_TIMEOUT)
            if res.returncode == 0:
                return True
            log.warning(f"hermes ACP 补装失败 ({os.path.basename(cmd[0])}): {(res.stderr or res.stdout or '')[:200]}")
        except FileNotFoundError:
            continue
        except Exception as e:
            log.warning(f"hermes ACP 补装异常 ({os.path.basename(cmd[0])}): {e}")
    return False


def _ensure_hermes_acp(cli: str) -> bool:
    """确保 hermes 的 ACP 依赖可用：检查 → 缺则自动补装 → 复查。

    返回 False 时调用方应将 hermes 注册为 unhealthy（日志已写明人工修复命令）。
    任何异常均被吞掉转为 False，不阻塞其他引擎的注册。
    """
    try:
        if not cli:
            return False
        if _acp_check(cli):
            return True
        log.warning(f"hermes ACP 依赖缺失（{cli}），尝试自动补装 {ACP_PACKAGE}")
        venv_bin = _hermes_venv_bin(cli)
        if not venv_bin:
            log.warning(f"无法解析 hermes venv（launcher: {cli}）。"
                        f"请人工执行: pip install {ACP_PACKAGE}（须装进 hermes 所属环境）")
            return False
        if not _pip_install_into(venv_bin):
            log.warning(f"hermes ACP 自动补装失败。请人工执行: {os.path.join(venv_bin, 'pip')} install {ACP_PACKAGE}")
            return False
        if _acp_check(cli):
            log.info(f"hermes ACP 依赖已自动补装成功: {ACP_PACKAGE} -> {venv_bin}")
            return True
        log.warning(f"hermes ACP 补装后复查仍失败（版本不匹配？）。请人工检查: {cli} acp --check")
        return False
    except Exception as e:
        log.warning(f"hermes ACP 自举异常（按 unhealthy 注册）: {e}")
        return False
