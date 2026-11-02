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
import shutil
import subprocess
import threading

from src.config import log

ACP_CHECK_TIMEOUT_DEFAULT = 20   # ACP 握手探测超时默认（含 Windows 冷启动余量）
                               # Issue #700：15s 在 Windows 冷启动（.exe 启动器 + 大量
                               # import）实测易吃满 → 超时统一归 ACP_FAIL → healthy
                               # 震荡。对齐 --version 探测的 20s，并支持 env 覆盖。


def _resolve_acp_check_timeout() -> int:
    """解析 ACP 握手探测超时：RUNTIME_ACP_CHECK_TIMEOUT env 覆盖（正整数，1~300 钳制）。

    非法值（非数字 / 非正整数 / 超范围）回退默认并告警，不抛异常
    （对齐 agent.py:1030-1032 的 env 覆盖先例）。
    """
    _raw = (os.environ.get("RUNTIME_ACP_CHECK_TIMEOUT") or "").strip()
    if not _raw:
        return ACP_CHECK_TIMEOUT_DEFAULT
    try:
        _val = int(_raw)
    except ValueError:
        log.warning(f"RUNTIME_ACP_CHECK_TIMEOUT 非法值 '{_raw}'（非整数），回退默认 {ACP_CHECK_TIMEOUT_DEFAULT}s")
        return ACP_CHECK_TIMEOUT_DEFAULT
    if _val < 1 or _val > 300:
        log.warning(f"RUNTIME_ACP_CHECK_TIMEOUT 非法值 '{_raw}'（超出 1~300 范围），回退默认 {ACP_CHECK_TIMEOUT_DEFAULT}s")
        return ACP_CHECK_TIMEOUT_DEFAULT
    return _val


ACP_CHECK_TIMEOUT = _resolve_acp_check_timeout()  # ACP 握手探测超时（含 hermes 冷启动余量）
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


def _pyvenv_scripts_dir(python_exe: str) -> str:
    """由 python 解释器路径定位 venv Scripts/bin 目录（pyvenv.cfg 同级判定）。

    解释器在 venv 里时，其目录即 venv 的 Scripts（Windows）/ bin（Unix）；
    若同目录存在 pyvenv.cfg 视为确认。找不到 pyvenv.cfg 也接受同目录
    （`py -3` 解析出的 base python 反推场景：上溯父目录找 pyvenv.cfg）。
    返回空串表示无法定位 venv。
    """
    try:
        d = os.path.dirname(python_exe)
        # 直接同目录
        if os.path.isfile(os.path.join(d, "pyvenv.cfg")):
            return d
        # 上溯两级（Windows: venv\Scripts\python.exe → venv\pyvenv.cfg）
        for _ in range(2):
            d = os.path.dirname(d)
            if not d or d == os.path.dirname(d):
                break
            if os.path.isfile(os.path.join(d, "pyvenv.cfg")):
                sub = os.path.join(d, "Scripts")
                if os.path.isfile(os.path.join(sub, "python.exe")):
                    return sub
                if os.path.isdir(os.path.join(d, "bin")):
                    return os.path.join(d, "bin")
    except Exception:
        pass
    # 解释器同目录本身形态（venv Scripts 直查）
    base = os.path.dirname(python_exe)
    if (os.path.isfile(os.path.join(base, "python.exe"))
            or os.path.isfile(os.path.join(base, "python"))):
        return base
    return ""


def _resolve_py_launcher_python() -> str:
    """`py -3` 形态：用 Windows Python Launcher 定位解释器真实路径。

    venv 的 .cmd 模板里 `@py -3 script.py` 由系统 py launcher 解析；
    `py -3 -c "import sys; print(sys.executable or getattr(sys, '_base_executable', ''))"`
    可拿到解释器路径——优先 sys.executable（#837 Required-1）：

    - Windows 常规 base python 没有 sys._base_executable 属性，旧表达式
      `sys._base_executable or sys.executable` 会 AttributeError → 兜底失效；
    - venv 上下文里 sys.executable 指 venv python（与 _pyvenv_scripts_dir
      反推的目录一致）；base python 下就是 base 解释器路径，均可可靠定位。
    解析失败返回空串。冷启动风险：仅在解析 .cmd 静态行失败后的兜底路径，
    超时 10s。
    """
    try:
        res = subprocess.run(
            ["py", "-3", "-c",
             "import sys; print(sys.executable or getattr(sys, '_base_executable', ''))"],
            capture_output=True, text=True, timeout=10)
        if res.returncode == 0:
            path = (res.stdout or "").strip().splitlines()
            if path and path[0]:
                return path[0]
    except Exception:
        pass
    return ""


_CMD_PY_LINE = re.compile(
    r'^\s*@?\s*"?(py|python|pythonw)"?\s+(?:-\S+\s+)*"([^"\n]+\.exe)"',
    re.IGNORECASE)
# 绝对路径 python 解释器（盘符 X:\ 或 X:/ 或 POSIX / 形态；测试在 Linux 上
# 造 Windows 风格 .cmd 时路径为 POSIX 绝对路径，两种都要认）
_CMD_PY_PLAIN = re.compile(
    r'^\s*@?\s*"?((?:[A-Za-z]:[\\/]|/|%~dp0[\\/]?)[^"\n]*?python(?:w)?\.exe)"?',
    re.IGNORECASE)


def _cmd_python_candidates(content: str, launcher_dir: str = "") -> list:
    """从 .cmd/.bat 内容提取候选 python 解释器路径（Windows venv 模板）。

    %~dp0 前缀展开为 launcher 所在目录（launcher_dir 缺省且候选为 %~dp0
    形态时无法展开，跳过该候选）。路径统一成正斜杠形态返回（Windows 下
    os.path API 兼容正/反斜杠，Linux 测试环境也一致）。"""
    out = []
    for line in (content or "").splitlines():
        m = _CMD_PY_PLAIN.search(line)
        if m:
            cand = m.group(1)
            if cand.lower().startswith("%~dp0"):
                if not launcher_dir:
                    continue
                cand = os.path.join(launcher_dir, cand[len("%~dp0"):].lstrip("\\/"))
            out.append(cand.replace("\\", "/"))
            continue
        m2 = _CMD_PY_LINE.search(line)
        if m2:
            cand = m2.group(2)
            if os.path.basename(cand).lower().startswith("python"):
                out.append(cand.replace("\\", "/"))
    return out


def _hermes_venv_bin_windows(cli: str) -> str:
    """Windows 启动器（.exe/.cmd/.bat）解析 venv bin（Scripts 目录）。

    支持形态（Issue #700，Windows 自愈补装前提）：
    1. `.exe`：console-script launcher 位于 venv\\Scripts\\（hermes.exe 与
       python.exe 同目录）——launcher 所在目录即 venv Scripts，校验
       python.exe 存在即返回。
    2. `.cmd`/`.bat` 内容行：
       - venv 模板标准形态 `@"C:\\path\\venv\\Scripts\\python.exe" "script" %*`
         （带引号绝对路径）或 `@C:\\path\\python.exe script %*`（无引号）；
       - pip/distlib 常见 `"%~dp0\\python.exe" "%~dp0\\hermes-script.py" %*`
         （%~dp0 相对路径，展开为 launcher 所在目录，#837 Required-2）；
       - `@py -3 script.py %*`（Python launcher 形态）——静态行拿不到
         解释器路径，走 _resolve_py_launcher_python 子进程定位，再由
         解释器路径反推 venv Scripts（上溯找 pyvenv.cfg）。
    内容解析失败时若 launcher 同目录存在 python.exe 直接返回该目录
    （.cmd 包装器与 python.exe 同处 venv\\Scripts，与 .exe 分支同规则）。
    全部失败返回空串（_ensure_hermes_acp 落人工修复指引，契约不变）。
    """
    try:
        launcher_dir = os.path.dirname(os.path.realpath(cli))
        ext = os.path.splitext(cli)[1].lower()
        if ext == ".exe":
            if os.path.isfile(os.path.join(launcher_dir, "python.exe")):
                return launcher_dir
            return ""
        # .cmd / .bat：读内容解析
        with open(cli, "r", encoding="utf-8", errors="replace") as f:
            content = f.read(8192)
        for cand in _cmd_python_candidates(content, launcher_dir):
            if os.path.isfile(cand):
                scripts = _pyvenv_scripts_dir(cand)
                if scripts:
                    return scripts
        # ② 内容解析失败兜底：launcher 同目录 python.exe（pip/distlib .cmd
        #    包装器与 python.exe 同处 venv\Scripts，与 .exe 分支同规则）
        if os.path.isfile(os.path.join(launcher_dir, "python.exe")):
            return launcher_dir
        # ③ @py -3 形态（无绝对路径）→ py launcher 子进程定位
        if re.search(r'^\s*@?\s*"?py"?\s', content, re.IGNORECASE):
            exe = _resolve_py_launcher_python()
            if exe and os.path.isfile(exe):
                scripts = _pyvenv_scripts_dir(exe)
                if scripts:
                    return scripts
        return ""
    except Exception:
        return ""


def _hermes_venv_bin(cli: str) -> str:
    """从 hermes launcher 解析其 venv bin 目录。

    支持的 launcher 形态（Issue #613 三断点之二）：
    1. bash shim（~/.local/bin/hermes）：
           #!/usr/bin/env bash
           exec "/home/xxx/.hermes/hermes-agent/venv/bin/hermes" "$@"
       提取 exec 行中的绝对路径，返回其目录（venv/bin）。
    2. 软链 / console-script launcher：realpath 跟随软链后，读目标首行
       shebang（#!/path/to/venv/bin/python），由解释器路径反推 venv/bin。
    3. Windows 启动器（Issue #700）：.exe/.cmd/.bat —— 走
       _hermes_venv_bin_windows（.exe 同目录校验 / .cmd·.bat 内容行解析
       / py launcher 定位），使缺依赖自动补装在 Windows 上生效。
    4. Linux 侧对称补齐（Issue #700 跟进）：与 Windows 修复对齐的三种形态——
       a. `#!/usr/bin/env python*` shebang（pip console-script 默认形态）：
          shutil.which 解析 env 实际命中的解释器，返回其目录；
       b. bash shim exec 行不带引号（exec /path/bin/hermes "$@"）；
       c. `exec ".../python" -m hermes` 形态（pipx/pdm）：解释器路径目录即 bin。
    解析失败返回空串（此前 Windows 形态一律空串 → 无法自愈，现补齐）。
    """
    try:
        if not os.path.isfile(cli):
            return ""
        if os.path.splitext(cli)[1].lower() in (".exe", ".cmd", ".bat"):
            return _hermes_venv_bin_windows(cli)
        real = os.path.realpath(cli)
        with open(real, "r", encoding="utf-8", errors="replace") as f:
            content = f.read(4096)
        # ① bash shim exec 行（带引号 / 不带引号 / python -m 形态）
        #    exec ".../bin/hermes" "$@"            —— 原始支持（带引号）
        #    exec .../bin/hermes "$@"              —— 不带引号（#700 Linux 跟进）
        #    exec ".../bin/python" -m hermes "$@"  —— pipx/pdm 形态（#700 Linux 跟进）
        m = re.search(r'exec\s+"([^"\n]+/bin/hermes)"', content)
        if not m:
            m = re.search(r'exec\s+([^\s"\n]+/bin/hermes)(?:\s|")', content)
        if m and os.path.isfile(m.group(1)):
            return os.path.dirname(m.group(1))
        m2 = re.search(r'exec\s+"([^"\n]+/bin/python[0-9.]*)"\s+-m', content)
        if m2 and os.path.isfile(m2.group(1)):
            return os.path.dirname(m2.group(1))
        # ② shebang 反推：#!/path/to/venv/bin/python(-x) → venv/bin
        first = content.splitlines()[0] if content else ""
        if first.startswith("#!"):
            parts = first[2:].strip().split()
            interp = parts[0] if parts else ""
            if (interp.startswith("/") and not interp.endswith("/env")
                    and os.path.basename(interp).startswith("python")
                    and os.path.isfile(interp)):
                return os.path.dirname(interp)
            # ②-b env shebang：#!/usr/bin/env python3(-x)（pip console-script 默认形态，
            # #700 Linux 跟进）——which 解析实际解释器，返回其目录（venv 前置 PATH 时
            # 即 venv bin；系统 python 时为其 bin 目录，与 Windows py launcher 行为对齐）
            if interp.endswith("/env") and len(parts) >= 2:
                env_target = parts[1]
                if os.path.basename(env_target).startswith("python"):
                    resolved = shutil.which(env_target) or ""
                    if resolved and os.path.isfile(resolved):
                        return os.path.dirname(resolved)
    except Exception:
        pass
    return ""


def _venv_python(venv_bin: str) -> str:
    """venv 的 python 解释器路径（Windows Scripts 下为 python.exe）。"""
    for name in ("python.exe", "python"):
        cand = os.path.join(venv_bin, name)
        if os.path.isfile(cand):
            return cand
    return os.path.join(venv_bin, "python")


def _venv_pip(venv_bin: str) -> str:
    """venv 的 pip 路径（Windows Scripts 下为 pip.exe；不存在回退 python -m pip）。"""
    for name in ("pip.exe", "pip"):
        cand = os.path.join(venv_bin, name)
        if os.path.isfile(cand):
            return cand
    return os.path.join(venv_bin, "pip")


def _installed_acp_version(venv_bin: str) -> str:
    """查询 hermes venv 已安装的 agent-client-protocol 版本（未装返回空串）。"""
    py = _venv_python(venv_bin)
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
    py = _venv_python(venv_bin)
    for cmd in ([_venv_pip(venv_bin)], [py, "-m", "pip"]):
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
