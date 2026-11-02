"""hermes ACP 依赖自举：探测 + 自动补装（hermes-acp-auto-bootstrap-design.md）。

背景：hermes 的 ACP 依赖是可选 extra（pip install hermes[acp]），普通安装不带
agent-client-protocol。`hermes --version` 探测不出该缺失（不触碰 ACP 模块），
导致引擎注册为 healthy 但 ACP 调用永远失败（wanglei 机器实证，2026-10-10）。

早期实现用 `hermes acp --check`（自检子命令）做健康门，但 hermes v0.9.0 起
并无该子命令（EXIT 2 unrecognized arguments），探测恒失败（Issue #613 /
反馈单 #196）。现改为跨版本稳定探测：

- 探测：对 `hermes acp` 做真实 ACP 握手（stdin 发 initialize + session/new
  两条 JSON-RPC 请求，stdout 收到 JSON-RPC 响应即视为可用），短超时兜底；
- 失败原因分类：区分「hermes 版本过旧无 acp 子命令」（补装无法修复，
  不误报依赖缺失）与「ACP 依赖缺失」（可自动补装修复）；
- venv 解析：兼容 bash shim（exec 行）、软链（realpath 跟随）与
  console-script launcher（shebang 解释器反推 venv/bin）；
- 补装版本：不再硬钉 agent-client-protocol==0.9.0；补装前查询 venv 已装
  版本做兼容判断，已装但握手失败视为适配器不兼容，给人工修复指引。

对外契约不变：`_ensure_hermes_acp(cli) -> bool`，异常吞掉转 False，
调用方（引擎注册）零改动。
"""
import json
import os
import re
import subprocess
import threading

from src.config import log

ACP_CHECK_TIMEOUT = 15        # ACP 握手探测超时（含 hermes 冷启动余量）
PIP_INSTALL_TIMEOUT = 120     # pip install 超时
ACP_PACKAGE = "agent-client-protocol"  # 不硬钉版本：安装前做已装版本兼容判断

# 探测失败原因
ACP_OK = "ok"                              # 握手成功
ACP_NO_SUBCOMMAND = "no_acp_subcommand"    # hermes 版本过旧，无 acp 子命令（不可由补装修复）
ACP_DEPS_MISSING = "deps_missing"          # ACP 依赖缺失（可自动补装修复）
ACP_FAIL = "fail"                          # 其他失败（超时/崩溃/协议异常）

_NO_SUBCOMMAND_HINTS = ("unrecognized arguments", "unknown command",
                        "invalid choice", "no such option")
_DEPS_MISSING_HINTS = ("modulenotfounderror", "importerror", "no module named",
                       "agent-client-protocol", "agent_client_protocol")


def _acp_requests() -> str:
    """构造 ACP 握手请求（initialize + session/new，JSON-RPC over stdio）。"""
    init = {"jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {"protocolVersion": 1, "clientCapabilities": {}}}
    new = {"jsonrpc": "2.0", "id": 2, "method": "session/new",
           "params": {"cwd": os.getcwd(), "mcpServers": {}}}
    return json.dumps(init) + "\n" + json.dumps(new) + "\n"


def _is_jsonrpc_response(line: str) -> bool:
    """判断一行输出是否为 JSON-RPC 响应/消息（握手成功的证据）。"""
    try:
        msg = json.loads(line)
    except Exception:
        return False
    if not isinstance(msg, dict):
        return False
    return any(k in msg for k in ("id", "result", "error", "method"))


def _acp_probe(cli: str) -> tuple:
    """对 `hermes acp` 做真实握手探测，返回 (healthy, reason)。

    - stdout 收到任意 JSON-RPC 响应 → (True, ACP_OK)
    - 无响应且 stderr 提示未知子命令 → (False, ACP_NO_SUBCOMMAND)
    - 无响应且 stderr 提示依赖/导入缺失 → (False, ACP_DEPS_MISSING)
    - 其余（超时/崩溃/协议异常）→ (False, ACP_FAIL)
    """
    try:
        proc = subprocess.Popen([cli, "acp"], stdin=subprocess.PIPE,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                text=True)
    except Exception:
        return False, ACP_FAIL

    def _cleanup():
        try:
            proc.kill()
        except Exception:
            pass
        for s in (proc.stdin, proc.stdout, proc.stderr):
            try:
                if s:
                    s.close()
            except Exception:
                pass
        try:
            proc.wait(timeout=5)
        except Exception:
            pass

    try:
        try:
            proc.stdin.write(_acp_requests())
            proc.stdin.flush()
            proc.stdin.close()
        except Exception:
            pass
        box = {}

        def _read():
            try:
                box["line"] = proc.stdout.readline()
            except Exception:
                box["line"] = ""

        reader = threading.Thread(target=_read, daemon=True)
        reader.start()
        reader.join(ACP_CHECK_TIMEOUT)
        line = (box.get("line") if not reader.is_alive() else "") or ""
        line = line.strip()
        if line:
            if _is_jsonrpc_response(line):
                return True, ACP_OK
            return False, ACP_FAIL
        # 无响应行：结束进程后读 stderr 分类失败原因（进程已死，read 到 EOF 不阻塞）
        try:
            proc.kill()
            proc.wait(timeout=5)
        except Exception:
            pass
        try:
            err = proc.stderr.read() or ""
        except Exception:
            err = ""
        low = err.lower()
        if any(k in low for k in _NO_SUBCOMMAND_HINTS):
            return False, ACP_NO_SUBCOMMAND
        if any(k in low for k in _DEPS_MISSING_HINTS):
            return False, ACP_DEPS_MISSING
        return False, ACP_FAIL
    finally:
        _cleanup()


def _acp_check(cli: str) -> bool:
    """真实 ACP 握手探测（兼容旧调用方），握手成功视为 ACP 依赖可用。"""
    ok, _reason = _acp_probe(cli)
    return ok


def _hermes_venv_bin(cli: str) -> str:
    """从 hermes launcher 解析其 venv bin 目录。

    支持的 launcher 形态（Issue #613 三断点之二）：
    1. bash shim（~/.local/bin/hermes）：
           #!/usr/bin/env bash
           exec "/home/xxx/.hermes/hermes-agent/venv/bin/hermes" "$@"
       提取 exec 行中的绝对路径，返回其目录（venv/bin）。
    2. 软链 / console-script launcher：realpath 跟随软链后，读目标首行
       shebang（#!/path/to/venv/bin/python），由解释器路径反推 venv/bin。
    解析失败返回空串（Windows .exe/.cmd/.bat 等形态暂不支持，走 False 路径）。
    """
    try:
        if not os.path.isfile(cli) or os.path.splitext(cli)[1].lower() in (".exe", ".cmd", ".bat"):
            return ""
        real = os.path.realpath(cli)
        with open(real, "r", encoding="utf-8", errors="replace") as f:
            content = f.read(4096)
        # ① bash shim exec 行
        m = re.search(r'exec\s+"([^"\n]+/bin/hermes)"', content)
        if m and os.path.isfile(m.group(1)):
            return os.path.dirname(m.group(1))
        # ② shebang 反推：#!/path/to/venv/bin/python(-x) → venv/bin
        first = content.splitlines()[0] if content else ""
        if first.startswith("#!"):
            parts = first[2:].strip().split()
            interp = parts[0] if parts else ""
            if (interp.startswith("/") and not interp.endswith("/env")
                    and os.path.basename(interp).startswith("python")
                    and os.path.isfile(interp)):
                return os.path.dirname(interp)
    except Exception:
        pass
    return ""


def _installed_acp_version(venv_bin: str) -> str:
    """查询 hermes venv 已安装的 agent-client-protocol 版本（未装返回空串）。"""
    py = os.path.join(venv_bin, "python")
    try:
        res = subprocess.run(
            [py, "-c", "import importlib.metadata as m; print(m.version('agent-client-protocol'))"],
            capture_output=True, text=True, timeout=30)
        if res.returncode == 0:
            return (res.stdout or "").strip()
    except Exception:
        pass
    return ""


def _pip_install_into(venv_bin: str) -> bool:
    """用指定 venv 的 pip 安装 ACP 包（不硬钉版本）。pip 不可用时用 python -m pip 兜底。"""
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
    """确保 hermes 的 ACP 依赖可用：握手探测 → 缺则自动补装 → 复查。

    返回 False 时调用方应将 hermes 注册为 unhealthy（日志已写明人工修复指引）。
    任何异常均被吞掉转为 False，不阻塞其他引擎的注册。
    """
    try:
        if not cli:
            return False
        ok, reason = _acp_probe(cli)
        if ok:
            return True
        if reason == ACP_NO_SUBCOMMAND:
            # hermes 版本过旧无 acp 子命令：不是依赖缺失，补装无法修复，不误报
            log.warning(f"hermes 不支持 acp 子命令（{cli}，版本过旧），"
                        f"自动补装无法修复。请升级 hermes 至支持 ACP 的版本后重试")
            return False
        log.warning(f"hermes ACP 探测失败（{cli}，原因: {reason}），尝试自动补装 {ACP_PACKAGE}")
        venv_bin = _hermes_venv_bin(cli)
        if not venv_bin:
            log.warning(f"无法解析 hermes venv（launcher: {cli}）。"
                        f"请人工执行: pip install {ACP_PACKAGE}（须装进 hermes 所属环境）")
            return False
        installed = _installed_acp_version(venv_bin)
        if installed:
            # ACP 已装但握手仍失败：hermes 适配器与已装 ACP 版本不兼容，
            # 盲目补装无意义，给出人工对齐版本指引（Issue #613 三断点之三）
            log.warning(f"hermes venv 已装 agent-client-protocol=={installed} 但 ACP 握手失败，"
                        f"疑似 hermes 适配器与该版本不兼容。请人工对齐版本"
                        f"（升级 hermes，或按其 pyproject 的 acp extra 约束安装匹配版本）后重试")
            return False
        if not _pip_install_into(venv_bin):
            log.warning(f"hermes ACP 自动补装失败。请人工执行: {os.path.join(venv_bin, 'pip')} install {ACP_PACKAGE}")
            return False
        ok, reason = _acp_probe(cli)
        if ok:
            log.info(f"hermes ACP 依赖已自动补装成功: {ACP_PACKAGE} -> {venv_bin}")
            return True
        log.warning(f"hermes ACP 补装后复查仍失败（原因: {reason}）。请人工检查: {cli} acp")
        return False
    except Exception as e:
        log.warning(f"hermes ACP 自举异常（按 unhealthy 注册）: {e}")
        return False
