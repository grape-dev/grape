"""跨平台 OS 路径抽象层（os_adapter）。

统一 Unix / Windows 的路径约定、CLI 查找、venv 路径、配置目录等，
避免散落的 if os.name / sys.platform 判断。

Linux 行为保持不变（抽象层对 Linux 透明）；Windows 为新增分支。
"""
import os
import sys
import shutil
import platform
import re as _re


_IS_WINDOWS = sys.platform == "win32" or os.name == "nt"


def is_windows() -> bool:
    """当前进程是否运行在 Windows 上。"""
    return _IS_WINDOWS


def home_dir() -> str:
    """用户家目录（跨平台）。"""
    return os.path.expanduser("~")


def config_dir() -> str:
    """grape 配置目录。

    Unix: ~/.grape
    Windows: %USERPROFILE%\\.grape
    """
    return os.path.join(home_dir(), ".grape")


def env_file() -> str:
    """runtime.env 配置文件路径。"""
    return os.path.join(config_dir(), "runtime.env")


def version_file() -> str:
    """worker.version 文件路径。"""
    return os.path.join(config_dir(), "worker.version")


def pid_file() -> str:
    """worker.pid 文件路径（单实例保护）。"""
    return os.path.join(config_dir(), "worker.pid")


def kill_existing_worker() -> bool:
    """杀掉已运行的旧 worker 进程（单实例保护）。

    读取 worker.pid → 检查进程是否存活 → 杀掉进程树。
    返回 True=杀掉了旧进程，False=无旧进程或杀失败。
    """
    import os as _os
    pf = pid_file()
    try:
        with open(pf, "r", encoding="utf-8") as f:
            old_pid = int(f.read().strip())
    except Exception:
        return False

    # 检查进程是否存活
    if not _is_process_alive(old_pid):
        return False

    # 杀掉旧进程树
    killed = kill_process_tree(old_pid, force=True)
    if killed:
        import time as _t
        _t.sleep(2)  # 等待进程完全退出 + 端口/文件释放
    return killed


def _is_process_alive(pid: int) -> bool:
    """检查进程是否存活（跨平台）。"""
    if pid <= 0:
        return False
    if _IS_WINDOWS:
        import subprocess as _sp
        try:
            r = _sp.run(["tasklist", "/FI", f"PID eq {pid}", "/NH"],
                        capture_output=True, text=True, timeout=5)
            return str(pid) in r.stdout
        except Exception:
            return False
    try:
        os.kill(pid, 0)  # signal 0 = 检查进程是否存在
        return True
    except (ProcessLookupError, PermissionError):
        return False
    except Exception:
        return False


def default_install_dir() -> str:
    """默认安装目录。

    Unix: ~/grape
    Windows: %USERPROFILE%\\grape
    """
    return os.path.join(home_dir(), "grape")


def venv_python(venv_dir: str) -> str:
    """venv 内 Python 可执行文件路径。

    Unix: {venv}/bin/python
    Windows: {venv}\\Scripts\\python.exe
    """
    if _IS_WINDOWS:
        return os.path.join(venv_dir, "Scripts", "python.exe")
    return os.path.join(venv_dir, "bin", "python")


def venv_pip(venv_dir: str) -> str:
    """venv 内 pip 可执行文件路径。

    Unix: {venv}/bin/pip
    Windows: {venv}\\Scripts\\pip.exe
    """
    if _IS_WINDOWS:
        return os.path.join(venv_dir, "Scripts", "pip.exe")
    return os.path.join(venv_dir, "bin", "pip")


def which(cli: str) -> str:
    """跨平台 CLI 查找。

    Unix: shutil.which(cli)
    Windows: 先查 cli，再查 cli.cmd / cli.exe / cli.bat
    （npm 全局包在 Windows 上生成 .cmd 包装器）
    """
    path = shutil.which(cli)
    if path:
        return path
    if _IS_WINDOWS:
        for ext in (".cmd", ".exe", ".bat"):
            path = shutil.which(cli + ext)
            if path:
                return path
    return ""


_ENGINE_WINDOWS_PATHS: dict[str, list[str]] = {
    "opencode": [
        os.path.join(os.environ.get("LOCALAPPDATA", ""), "npm", "opencode.cmd"),
        os.path.join(os.environ.get("APPDATA", ""), "npm", "opencode.cmd"),
        os.path.join(os.environ.get("PROGRAMFILES", ""), "nodejs", "opencode.cmd"),
    ],
    "codearts": [
        os.path.join(os.environ.get("LOCALAPPDATA", ""), "Programs", "codearts", "codearts.cmd"),
        os.path.join(os.environ.get("PROGRAMFILES", ""), "codearts", "codearts.cmd"),
    ],
    "hermes": [
        os.path.join(os.environ.get("LOCALAPPDATA", ""), "Programs", "Python", "Scripts", "hermes.exe"),
        os.path.join(os.environ.get("USERPROFILE", ""), ".local", "bin", "hermes.exe"),
    ],
    "codex": [
        os.path.join(os.environ.get("LOCALAPPDATA", ""), "npm", "codex.cmd"),
        os.path.join(os.environ.get("APPDATA", ""), "npm", "codex.cmd"),
    ],
    "cursor": [
        os.path.join(os.environ.get("LOCALAPPDATA", ""), "Programs", "cursor", "cursor.cmd"),
    ],
}


def engine_search_paths(engine_name: str) -> list[str]:
    """引擎 CLI 在 Windows 上的常见安装路径（仅 Windows 调用）。

    返回路径列表，调用方逐一检查 isfile。
    """
    if not _IS_WINDOWS:
        return []
    return [p for p in _ENGINE_WINDOWS_PATHS.get((engine_name or "").lower(), []) if p]


def find_cli(cli_name: str) -> str:
    """查找 CLI 路径（跨平台）。

    优先级：PATH (which) → Windows 常见安装路径。
    返回空字符串表示未找到。
    """
    path = which(cli_name)
    if path:
        return path
    for p in engine_search_paths(cli_name):
        if p and os.path.isfile(p):
            return p
    return ""


def engine_skill_dirs(engine_name: str) -> list[str]:
    """引擎 skill 目录（跨平台）。

    Unix:
      hermes: ~/.hermes/skills
      opencode: ~/.config/opencode/skills
      codearts: ~/.codeartsdoer/cli-data/system/skills
      hwcloud(AIShell): ~/.hwcloud/skills + ~/.agents/skills（后者为 openagent
      原生全局 skill 根——acp/server.go buildSessionSkillProvider 约定，
      任务执行时 agent 实际读取的目录；前者为 grape 侧自定义路径，两者都扫）
    Windows:
      hermes: %USERPROFILE%\\.hermes\\skills
      opencode: %USERPROFILE%\\.config\\opencode\\skills
      codearts: %USERPROFILE%\\.codeartsdoer\\cli-data\\system\\skills
      hwcloud(AIShell): %USERPROFILE%\\.hwcloud\\skills + %USERPROFILE%\\.agents\\skills
    """
    home = home_dir()
    key = (engine_name or "").strip().lower()
    mapping = {
        "hermes": [os.path.join(home, ".hermes", "skills")],
        "opencode": [os.path.join(home, ".config", "opencode", "skills")],
        "codearts": [os.path.join(home, ".codeartsdoer", "cli-data", "system", "skills")],
        # hwcloud(AIShell) 双路径：~/.agents/skills 是 openagent 原生全局 skill 根
        # （acp/server.go:1846 约定，agent 任务执行时实际读取），~/.hwcloud/skills 为
        # grape 侧自定义。多路径列表天然支持——另可用 RUNTIME_EXTRA_SKILL_DIRS
        # env（冒号分隔）追加任意目录，扫描时合并进所有引擎。
        "hwcloud": [os.path.join(home, ".agents", "skills"),
                    os.path.join(home, ".hwcloud", "skills")],
    }
    return mapping.get(key, [])


def extra_skill_dirs() -> list[str]:
    """解析 RUNTIME_EXTRA_SKILL_DIRS 环境变量（跨平台分隔符）。

    Unix 分隔符为 ':'，Windows 为 ';'。
    只返回实际存在的目录。
    """
    extra = os.environ.get("RUNTIME_EXTRA_SKILL_DIRS", "")
    if not extra:
        return []
    sep = ";" if _IS_WINDOWS else ":"
    result = []
    for d in extra.split(sep):
        d = d.strip()
        if d and os.path.isdir(d):
            result.append(d)
    return result


def path_separator() -> str:
    """PATH / PYTHONPATH 环境变量分隔符。"""
    return ";" if _IS_WINDOWS else ":"


def bun_cache_dir() -> str:
    """bun JIT 编译缓存目录（跨平台）。

    Unix: /var/cache/bun-compile
    Windows: %LOCALAPPDATA%\\grape\\bun-compile
    """
    if _IS_WINDOWS:
        return os.path.join(os.environ.get("LOCALAPPDATA", home_dir()), "grape", "bun-compile")
    return "/var/cache/bun-compile"


def remap_cwd(path: str) -> str:
    """将服务端下发的 cwd 路径映射到本地用户 home 目录（跨平台）。

    服务端以 CHAT_CWD_BASE（默认 /home/grape）为基准生成 chat 工作路径，但各接入
    机器的用户不同。通过 RUNTIME_CWD_PREFIX_MAP 环境变量配置映射（调用方处理）。
    本函数处理默认映射逻辑（无 RUNTIME_CWD_PREFIX_MAP 时）：

    Unix（保持不变）：
      1. grape-workspaces 标记段匹配：/home/{user}/grape-workspaces → ~/grape-workspaces
      2. /home/grape 前缀匹配：/home/grape/chat/... → ~/chat/...

    Windows：
      将 Unix 风格的 /home/grape/... 映射到 %USERPROFILE%/...
      （服务端始终用 Unix 路径，Windows worker 需转换）
    """
    if not path:
        return path
    local_home = home_dir()

    if _IS_WINDOWS:
        # Windows：把 Unix 路径转换为 Windows 路径
        # 1. grape-workspaces 标记段匹配
        marker = "/grape-workspaces"
        idx = path.find(marker)
        if idx > 0:
            return local_home + path[idx:].replace("/", os.sep)
        # 2. /home/grape 前缀匹配（服务端 CHAT_CWD_BASE）
        default_src = "/home/grape"
        if path.startswith(default_src):
            return local_home + os.sep + path[len(default_src):].lstrip("/").replace("/", os.sep)
        # 3. 已是 Windows 路径（本地直连执行）
        if os.path.isabs(path) and (path[1:3] == ":\\" or path.startswith("\\\\")):
            return path
        return path

    # Unix（保持原有行为不变）
    if path.startswith(local_home):
        return path
    marker = "/grape-workspaces"
    idx = path.find(marker)
    if idx > 0:
        return local_home + path[idx:]
    default_src = "/home/grape"
    if path.startswith(default_src) and local_home != default_src:
        return local_home + path[len(default_src):]
    return path


def hostname() -> str:
    """机器名（跨平台，用于 device_name）。

    Unix: os.uname().nodename
    Windows: platform.node() / COMPUTERNAME
    """
    if _IS_WINDOWS:
        return os.environ.get("COMPUTERNAME", "") or platform.node() or "windows-host"
    if hasattr(os, "uname"):
        return os.uname().nodename
    return platform.node() or "host"


def temp_dir() -> str:
    """临时目录（跨平台）。"""
    if _IS_WINDOWS:
        return os.environ.get("TEMP", os.environ.get("TMP", "."))
    return os.environ.get("TMPDIR", "/tmp")


def proc_cwd(pid: int) -> str | None:
    """读取进程的 cwd（仅 Linux 支持 /proc；Windows 返回 None）。"""
    if _IS_WINDOWS:
        return None
    try:
        return os.readlink(f"/proc/{pid}/cwd")
    except Exception:
        return None


def kill_process_tree(pid: int, force: bool = False) -> bool:
    """终止进程树（跨平台）。

    Unix: os.killpg(os.getpgid(pid), SIGTERM/SIGKILL)
    Windows: taskkill /T /F（/T=含子进程, /F=强制）
    """
    if _IS_WINDOWS:
        import subprocess as _sp
        try:
            flags = ["/T"]
            if force:
                flags.append("/F")
            _sp.run(["taskkill"] + flags + ["/PID", str(pid)],
                    capture_output=True, timeout=10)
            return True
        except Exception:
            return False
    import signal as _sig
    try:
        pgid = os.getpgid(pid)
        sig = _sig.SIGKILL if force else _sig.SIGTERM
        os.killpg(pgid, sig)
        return True
    except Exception:
        return False


def find_process_by_cwd(backend: str, cwd: str) -> list[int]:
    """查找工作目录匹配 cwd 的引擎进程 PID 列表（跨平台）。

    Unix: pgrep -f + /proc/{pid}/cwd readlink 校验
    Windows: wmic / Get-CimInstance 查 CommandLine 匹配（无 cwd 校验）
    """
    if _IS_WINDOWS:
        import subprocess as _sp
        patterns = {
            "hermes": ["hermes acp", "hermes chat", "hermes -z"],
            "codearts": ["codearts run"],
            "codex": ["codex exec"],
        }.get(backend, ["opencode run"])
        pids = []
        for pattern in patterns:
            try:
                out = _sp.run(
                    ["wmic", "process", "where",
                     f"CommandLine like '%{pattern}%'",
                     "get", "ProcessId"],
                    capture_output=True, text=True, timeout=5
                )
                for line in (out.stdout or "").strip().split("\n"):
                    line = line.strip()
                    if line.isdigit():
                        pids.append(int(line))
            except Exception:
                pass
        return pids
    import subprocess as _sp
    patterns = {
        "hermes": ["hermes acp", "hermes chat", "hermes -z"],
        "codearts": ["codearts run"],
        "codex": ["codex exec"],
    }.get(backend, ["opencode run"])
    user = os.environ.get("USER", "")
    if not user and hasattr(os, "getlogin"):
        try:
            user = os.getlogin()
        except Exception:
            user = ""
    pids = []
    for pattern in patterns:
        try:
            out = _sp.run(
                ["pgrep", "-u", user, "-f", pattern],
                capture_output=True, text=True, timeout=5
            )
            for pid_str in out.stdout.strip().split():
                if not pid_str:
                    continue
                try:
                    pid = int(pid_str)
                    proc_cwd_path = os.readlink(f"/proc/{pid}/cwd")
                    if proc_cwd_path.startswith(cwd):
                        pids.append(pid)
                except Exception:
                    pass
        except Exception:
            pass
    return pids


def sandbox_supported() -> bool:
    """hermes 沙箱（bwrap namespace/cgroup）是否支持。

    Windows 无等价物，明确返回 False（降级标注不支持）。
    """
    return not _IS_WINDOWS


def normalize_device_name(raw: str) -> str:
    """设备名规范化：小写，非字母数字替换为 -，清理首尾 -。"""
    name = (raw or "").lower()
    name = _re.sub(r"[^a-z0-9]", "-", name)
    name = _re.sub(r"-+", "-", name).strip("-")
    return name or "worker"


def codearts_tasks_dir() -> str:
    """codearts per-task 数据目录（跨平台）。

    Unix: /var/cache/codearts-tasks（回退 ~/.cache/codearts-tasks）
    Windows: %LOCALAPPDATA%\\grape\\codearts-tasks
    """
    if _IS_WINDOWS:
        return os.path.join(os.environ.get("LOCALAPPDATA", home_dir()), "grape", "codearts-tasks")
    return "/var/cache/codearts-tasks"


def venv_bin_dir(venv_dir: str) -> str:
    """venv 可执行文件目录。

    Unix: {venv}/bin
    Windows: {venv}\\Scripts
    """
    if _IS_WINDOWS:
        return os.path.join(venv_dir, "Scripts")
    return os.path.join(venv_dir, "bin")


def supports_process_group() -> bool:
    """当前平台是否支持进程组操作（killpg/setsid）。

    Windows 无进程组概念，返回 False。
    """
    return not _IS_WINDOWS
