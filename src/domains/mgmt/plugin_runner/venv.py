"""每插件独立 venv（D8）：依赖隔离，命令在插件自身 venv 内解析。

依赖装配：requirements.txt（或显式 requirements 参数）经 venv 内 pip 安装；
venv 指纹 = 解释器 + 依赖内容哈希，失配即失效重建（幂等不只看目录存在）。
"""
import hashlib
import shutil
import subprocess
from pathlib import Path

from .errors import VenvError

FINGERPRINT_NAME = ".grape-venv-fingerprint"


def plugin_venv_dir(plugin_dir) -> Path:
    return Path(plugin_dir).resolve() / ".venv"


def _requirements_path(plugin_dir, requirements) -> Path | None:
    if requirements is not None:
        return Path(requirements)
    default = Path(plugin_dir) / "requirements.txt"
    return default if default.is_file() else None


def _fingerprint(base_python: str, requirements_file: Path | None) -> str:
    h = hashlib.sha256()
    h.update(base_python.encode("utf-8", "replace"))
    h.update(b"\0")
    if requirements_file is not None:
        h.update(requirements_file.read_bytes())
    return h.hexdigest()


def _run_checked(argv, *, timeout_seconds, what):
    try:
        proc = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
        )
    except subprocess.TimeoutExpired as e:
        raise VenvError(f"{what} 超时终止（>{timeout_seconds}s）") from e
    if proc.returncode != 0:
        tail = (proc.stderr or proc.stdout or "")[-2000:]
        raise VenvError(f"{what} 失败（exit={proc.returncode}）: {tail}")
    return proc


def ensure_plugin_venv(plugin_dir, *, base_python: str | None = None,
                       requirements=None, timeout_seconds: float = 300.0) -> Path:
    """确保插件目录下存在 .venv，返回 venv python 可执行文件路径。

    - base_python：用于创建 venv 的基础解释器（缺省 python3）。
    - requirements：依赖清单路径；缺省自动探测 <plugin_dir>/requirements.txt，
      存在即经 venv 内 pip 安装（墙钟受 timeout_seconds 约束）。
    - 指纹（解释器+依赖内容）失配时失效重建；安装失败不落指纹，下次重试。
    """
    base = base_python or shutil.which("python3") or "python3"
    vdir = plugin_venv_dir(plugin_dir)
    requirements_file = _requirements_path(plugin_dir, requirements)
    expected = _fingerprint(base, requirements_file)
    fingerprint_file = vdir / FINGERPRINT_NAME

    def create(clear: bool) -> None:
        argv = [base, "-m", "venv", "--symlinks"]
        if clear:
            argv.append("--clear")
        argv.append(str(vdir))
        _run_checked(argv, timeout_seconds=timeout_seconds, what="插件 venv 创建")

    py = venv_python(plugin_dir)
    if py is not None and (fingerprint_file.is_file() and fingerprint_file.read_text() == expected):
        return py
    create(clear=py is not None)
    py = venv_python(plugin_dir)
    if py is None:
        raise VenvError(f"插件 venv 创建失败（缺 python 可执行）: {vdir}")
    if requirements_file is not None:
        _run_checked(
            [py, "-m", "pip", "install", "--disable-pip-version-check", "--no-input",
             "--retries", "0", "--timeout", "30", "-r", str(requirements_file)],
            timeout_seconds=timeout_seconds,
            what=f"插件依赖装配（{requirements_file}）",
        )
    fingerprint_file.write_text(expected)
    return py


def venv_bin_dir(plugin_dir):
    """venv 可执行目录（Linux: bin；Windows: Scripts）；未创建返回 None。"""
    vdir = plugin_venv_dir(plugin_dir)
    for sub in ("bin", "Scripts"):
        d = vdir / sub
        if d.is_dir():
            return d
    return None


def venv_python(plugin_dir) -> str | None:
    vdir = plugin_venv_dir(plugin_dir)
    for sub in ("bin", "Scripts"):
        py = vdir / sub / ("python.exe" if sub == "Scripts" else "python")
        if py.is_file():
            return str(py)
    return None


def venv_bin_command(plugin_dir, command: str) -> str | None:
    """在插件 venv bin/ 下解析可执行命令；找不到返回 None（调用方回退 PATH）。"""
    vdir = plugin_venv_dir(plugin_dir)
    for sub in ("bin", "Scripts"):
        cand = vdir / sub / command
        if cand.is_file():
            return str(cand)
    return None
