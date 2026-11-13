#!/usr/bin/env python3
"""Grape 一键安装与启动 —— 跨平台单脚本（Windows / macOS / Linux）。

    python quickstart.py              # 探测环境 → 安装 → 后台启动 → 等 /api/health
    python quickstart.py --check      # 只探测与选路，不安装
    python quickstart.py --foreground # 前台运行（Ctrl-C 停止）
    python quickstart.py --docker     # 强制 Docker compose 路径
    python quickstart.py --production # 生产形态（3000 + 8080），默认开发模式单端口
    python quickstart.py --mirror     # 依赖走 PyPI 阿里云镜像
    python quickstart.py --skip-doctor / --no-install / --strict

纪律：不 sudo、不改系统环境；失败即停并打印确切命令与错误；幂等，不覆盖已有 .venv/.env/data。
"""
from __future__ import annotations

import argparse
import os
import platform
import secrets
import shutil
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
VENV = HERE / ".venv"
IS_WIN = sys.platform.startswith("win")
MIRROR = "https://mirrors.aliyun.com/pypi/simple/"


# 旧版 Windows 控制台可能不解析 ANSI 颜色，非 TTY / Windows 时退化为纯文本
USE_COLOR = (not IS_WIN) and sys.stdout.isatty()


def _c(code: str) -> str:
    return f"\033[{code}m" if USE_COLOR else ""


RESET = _c("0")


def step(msg: str) -> None: print(f"\n{_c('1;34')}▶ {msg}{RESET}", flush=True)
def ok(msg: str) -> None:   print(f"{_c('1;32')}  ✓ {msg}{RESET}", flush=True)
def warn(msg: str) -> None: print(f"{_c('1;33')}  ! {msg}{RESET}", flush=True)
def die(msg: str) -> "NoReturn":  # type: ignore[valid-type]
    print(f"{_c('1;31')}  ✗ {msg}{RESET}", file=sys.stderr, flush=True)
    sys.exit(1)


def venv_python() -> Path:
    return VENV / ("Scripts/python.exe" if IS_WIN else "bin/python")


def docker_cmd() -> list[str] | None:
    if shutil.which("docker") is None:
        return None
    for cand in (["docker", "compose"], ["docker-compose"]):
        try:
            r = subprocess.run(cand + ["version"], capture_output=True, text=True)
            if r.returncode == 0:
                return cand
        except OSError:
            continue
    return None


def compose_file() -> Path | None:
    """定位 compose 文件：仓库根目录或 deploy/ 下（两种布局都支持）。"""
    for cand in (HERE / "docker-compose.yml", HERE / "docker-compose.yaml",
                 HERE / "deploy" / "docker-compose.yml", HERE / "deploy" / "docker-compose.yaml"):
        if cand.exists():
            return cand
    return None


def port_busy(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.5)
        return s.connect_ex(("127.0.0.1", port)) == 0


def http_code(url: str, timeout: float = 2.0) -> str:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return str(r.status)
    except urllib.error.HTTPError as e:
        return str(e.code)
    except Exception:
        return "无响应"


def probe() -> dict:
    step("环境探测")
    os_name = platform.system() or "unknown"
    ok(f"平台：{os_name} · {platform.machine()}")

    ver = sys.version_info
    py_ok = ver >= (3, 10)
    (ok if py_ok else warn)(f"Python：{sys.executable} {ver.major}.{ver.minor}（{'满足' if py_ok else '低于'} >= 3.10）")
    alt = None
    if not py_ok:
        alt = find_newer_python()
        if alt:
            ok(f"发现可用解释器：{alt[0]}（Python {alt[1]}）→ 将用它重新执行本脚本")
        else:
            warn("本机没有 Python >= 3.10（Grape 的虚拟环境需要 3.10+；本脚本自身 3.8+ 即可运行）")

    node = shutil.which("node")
    if node:
        v = subprocess.run([node, "-v"], capture_output=True, text=True).stdout.strip()
        ok(f"Node.js：{v}（执行引擎 CLI 用，可选）")
    else:
        warn("Node.js：未找到（仅影响执行引擎 CLI，平台本身可跑）")

    dc = docker_cmd()
    if dc:
        v = subprocess.run(["docker", "--version"], capture_output=True, text=True).stdout.strip()
        ok(f"Docker：{v}（{' '.join(dc)} 可用）")
    else:
        warn("Docker：未安装或 compose 不可用")

    busy = [p for p in (3000, 8080, 80) if port_busy(p)]
    (warn if busy else ok)(f"端口：{'被占用 ' + ', '.join(map(str, busy)) if busy else '3000 / 8080 / 80 空闲'}")
    return {"os": os_name, "py_ok": py_ok, "alt": alt, "docker": dc, "busy": busy}


NEWER_PY_HELP = (
    "    可选路径：\n"
    "    ① 用 uv 装一个新解释器（不 sudo、不动系统 Python）：\n"
    "       curl -LsSf https://astral.sh/uv/install.sh | sh\n"
    "       ~/.local/bin/uv python install 3.12\n"
    "       ~/.local/bin/uv venv --python 3.12 /tmp/py312\n"
    "       /tmp/py312/bin/python quickstart.py\n"
    "    ② 或用包管理器装一个（如 dnf install -y python3.11），再 python3.11 quickstart.py\n"
    "    ③ 或安装 Docker 后重跑"
)


PY_CANDIDATES = ("python3.14", "python3.13", "python3.12", "python3.11", "python3.10", "python3")


def find_newer_python() -> tuple[str, str] | None:
    """在 PATH 里找一个 >= 3.10 的解释器（本机系统 Python 太旧时用它重新执行本脚本）。"""
    for name in PY_CANDIDATES:
        cand = shutil.which(name)
        if not cand or Path(cand).resolve() == Path(sys.executable).resolve():
            continue
        r = subprocess.run([cand, "-c", "import sys;sys.exit(0 if sys.version_info>=(3,10) else 1)"],
                           capture_output=True)
        if r.returncode != 0:
            continue
        v = subprocess.run([cand, "-c", "import sys;print('%d.%d' % sys.version_info[:2])"],
                           capture_output=True, text=True).stdout.strip()
        return (cand, v)
    return None


def python_ok_marker(path: Path) -> bool:
    if not path.exists():
        return False
    r = subprocess.run([str(path), "-c", "import sys;sys.exit(0 if sys.version_info>=(3,10) else 1)"],
                       capture_output=True)
    return r.returncode == 0


def ensure_venv() -> Path:
    step("虚拟环境 .venv")
    vp = venv_python()
    if vp.exists() and not python_ok_marker(vp):
        die(f".venv 已存在，但其 Python 低于 3.10（{vp}）。\n"
            "    删掉旧环境后重跑即可按当前解释器重建：rm -rf .venv && python quickstart.py")
    if python_ok_marker(vp):
        ok(f".venv 已存在：{vp}")
        return vp
    r = subprocess.run([sys.executable, "-m", "venv", str(VENV)])
    if r.returncode != 0 or not venv_python().exists():
        die(f"创建 venv 失败：{sys.executable} -m venv {VENV}")
    ok("已创建 .venv")
    return venv_python()


def install_deps(vp: Path, mirror: bool) -> None:
    step("安装依赖")
    req = HERE / "requirements.txt"
    if not req.exists():
        die("当前目录不是 grape 仓库根目录（缺 requirements.txt）。请把 quickstart.py 放到已 clone 的 grape 仓库根目录后再执行。")
    args = [str(vp), "-m", "pip", "install", "-r", str(req)]
    if mirror:
        args += ["-i", MIRROR]
    subprocess.run([str(vp), "-m", "pip", "install", "--upgrade", "pip"], capture_output=True)
    r = subprocess.run(args)
    if r.returncode != 0:
        die(f"依赖安装失败。手动排障：{' '.join(args)}（国内网络可加 --mirror）")
    ok("requirements.txt 安装完成")


def prepare_files() -> None:
    step("配置文件与数据目录")
    env, envx = HERE / ".env", HERE / ".env.example"
    if env.exists():
        ok(".env 已存在")
    elif envx.exists():
        shutil.copyfile(envx, env)
        ok("已从 .env.example 复制 .env（默认 SQLite + OBS 关闭）")
    else:
        warn(".env.example 不存在，请手动创建 .env")
    (HERE / "data").mkdir(exist_ok=True)
    ok("data/ 就绪（默认 SQLite：data/sessions.db）")


def run_doctor(vp: Path, skip: bool, strict: bool) -> None:
    step("环境自检")
    doc = HERE / "scripts" / "doctor.py"
    if skip:
        warn("已按 --skip-doctor 跳过")
        return
    if not doc.exists():
        warn("未找到 scripts/doctor.py，跳过")
        return
    r = subprocess.run([str(vp), str(doc)])
    if r.returncode == 0:
        ok("自检通过")
    elif strict:
        die("自检未通过（--strict）")
    else:
        warn("自检有告警，继续启动（--strict 可令其失败即停）")


def wait_health(url: str, seconds: int) -> bool:
    for _ in range(seconds):
        if http_code(url) == "200":
            return True
        time.sleep(1)
    return False


def start_native(vp: Path, production: bool, foreground: bool) -> None:
    step("启动服务")
    args = [str(vp), "-m", "src.api.run"] + ([] if production else ["--dev"])
    shape = "生产形态（3000 + 8080）" if production else "开发模式（单端口 3000）"
    (HERE / "logs").mkdir(exist_ok=True)
    log = HERE / "logs" / "start.log"
    if foreground:
        print(f"\n前台启动（{shape}）。Ctrl-C 停止；后台运行请去掉 --foreground。\n", flush=True)
        os.execv(str(vp), args)
        return
    f = open(log, "ab")
    kwargs: dict = {"stdout": f, "stderr": subprocess.STDOUT, "cwd": str(HERE), "stdin": subprocess.DEVNULL}
    if IS_WIN:
        kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS
    else:
        kwargs["start_new_session"] = True
    proc = subprocess.Popen(args, **kwargs)
    f.close()
    ok(f"已后台启动（pid {proc.pid}），日志：{log}")
    if wait_health("http://localhost:3000/api/health", 90):
        ok("健康检查通过：/api/health = 200")
    else:
        warn(f"90 秒内未就绪，请看日志：{log}")
    print(f"\n访问 http://localhost:3000   ·   停止：{'taskkill /PID ' + str(proc.pid) + ' /F' if IS_WIN else 'kill ' + str(proc.pid)}")


def start_docker(dc: list[str]) -> None:
    cf = compose_file()
    if cf and cf.parent != HERE:
        dc = dc + ["-f", str(cf)]
    step("Docker：配置文件与密钥" + (f"（{cf.relative_to(HERE) if cf else 'docker-compose.yml'}）" if cf else ""))
    env = HERE / ".env"
    if not env.exists() and (HERE / ".env.example").exists():
        shutil.copyfile(HERE / ".env.example", env)
        ok("已从 .env.example 复制 .env")
    text = env.read_text(encoding="utf-8") if env.exists() else ""
    if "JWT_SECRET=" not in text or len(text.split("JWT_SECRET=", 1)[1].splitlines()[0].strip()) < 8:
        with env.open("a", encoding="utf-8") as fh:
            fh.write(f"\nJWT_SECRET={secrets.token_hex(32)}\n")
        ok("已生成并写入 JWT_SECRET（compose 文件缺它会立刻退出）")
    else:
        ok("JWT_SECRET 已存在")

    step("Docker：构建并启动")
    r = subprocess.run(dc + ["up", "-d", "--build"], cwd=str(HERE))
    if r.returncode != 0:
        die(f"compose 启动失败。排障：{' '.join(dc)} logs --tail=50")
    ok(f"容器已启动（{' '.join(dc)}）")

    step("Docker：等待 /api/health")
    if wait_health("http://localhost:8080/api/health", 180):
        ok("健康检查通过：http://localhost:8080/api/health = 200")
    else:
        warn(f"180 秒内未就绪。排障：{' '.join(dc)} logs --tail=50")
    print(f"\n访问 http://localhost（80）或 http://localhost:8080   ·   停止：{' '.join(dc)} down   ·   清库：{' '.join(dc)} down -v（会删 grape-data 卷）")


def main() -> None:
    ap = argparse.ArgumentParser(add_help=True, description="Grape 一键安装与启动（跨平台）")
    ap.add_argument("--check", action="store_true", help="只探测环境与选路，不安装")
    ap.add_argument("--docker", action="store_true", help="强制 Docker 路径")
    ap.add_argument("--native", action="store_true", help="强制原生路径")
    ap.add_argument("--foreground", action="store_true", help="前台运行")
    ap.add_argument("--production", action="store_true", help="生产形态（3000 + 8080）")
    ap.add_argument("--mirror", action="store_true", help="依赖走 PyPI 阿里云镜像")
    ap.add_argument("--skip-doctor", action="store_true", help="跳过 doctor.py 自检")
    ap.add_argument("--no-install", action="store_true", help="跳过依赖安装")
    ap.add_argument("--strict", action="store_true", help="doctor.py 非 0 时直接失败")
    a = ap.parse_args()

    if not (HERE / "requirements.txt").exists() or not (HERE / "src").is_dir():
        die("当前目录不是 grape 仓库根目录（缺 requirements.txt 或 src/）。\n"
            "    请把 quickstart.py 放到已 clone 的 grape 仓库根目录后再执行。")

    info = probe()
    if not info["py_ok"] and info.get("alt") and not a.docker:
        cand, ver = info["alt"]
        step(f"用 Python {ver} 重新执行本脚本（{cand}）")
        os.execv(cand, [cand, str(Path(__file__).resolve()), *sys.argv[1:]])
    if a.docker:
        mode = "docker"
    elif a.native:
        mode = "native"
    else:
        mode = "native" if info["py_ok"] else "docker"
    step(f"选择的安装路径：{mode}")

    if mode == "native" and not info["py_ok"]:
        die("指定了 --native，但本机 Python 低于 3.10，且 PATH 里没有可用的新解释器。\n" + NEWER_PY_HELP)
    if mode == "docker" and not info["docker"]:
        die("Docker 路径不可用：未检测到 docker compose，且本机没有 Python >= 3.10。\n" + NEWER_PY_HELP)
    if a.check:
        print("\n  --check：仅探测，不执行安装。去掉该参数即可开始。\n")
        return

    if mode == "docker":
        assert info["docker"] is not None
        start_docker(info["docker"])
        return

    vp = ensure_venv()
    if a.no_install:
        warn("已按 --no-install 跳过依赖安装")
    else:
        install_deps(vp, a.mirror)
    prepare_files()
    run_doctor(vp, a.skip_doctor, a.strict)
    start_native(vp, a.production, a.foreground)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n已中断。")
        sys.exit(130)
