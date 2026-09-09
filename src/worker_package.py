"""Worker 安装包构建（见 worker-server-delivery-design.md）。

服务端按 manifest 将 worker 运行闭包代码打包为 tar.gz，首次请求动态打包、
写入临时文件缓存，代码变更后失效重建。产物由 GET /api/runtime/install/package
（rta_ 鉴权）下发。
"""
import hashlib, io, os, shutil, subprocess, sys, tarfile, threading

from src.config import log

ROOT = os.path.abspath(os.path.dirname(os.path.dirname(__file__)))
CACHE_DIR = os.path.join(ROOT, "data", "cache", "worker-package")
PKG_PATH = os.path.join(CACHE_DIR, "grape-worker.tar.gz")
VERSION_PATH = os.path.join(CACHE_DIR, "VERSION")
WHEELS_DIR = os.path.join(CACHE_DIR, "wheels")
REQUIREMENTS_PATH = os.path.join(ROOT, "requirements-worker.txt")
PIP_INDEX = os.environ.get("PIP_INDEX_URL", "https://pypi.tuna.tsinghua.edu.cn/simple")
# 打包进安装包的 Python 解释器来源：uv 的 standalone 构建（python-build-standalone，
# 免编译、自包含）。worker 端 install-worker.sh 在系统无 Python>=3.10 时用包内解释器建 venv。
UV_PYTHON_DIR = os.path.expanduser(os.environ.get("UV_PYTHON_DIR", "~/.local/share/uv/python"))

# worker 运行闭包清单（整目录粒度，含运行时懒加载模块）。
# 依据：src.runtime.agent 的 import 闭包（含 repogit/cleanup 等运行时 import）。
# src/api/ 只列 worker 实际引用的模块：time_utils/event_store/event_archive/
# session_store/knowledge/routing（顶层或懒加载）。曾因漏列 time_utils 导致
# worker 包缺文件启动即崩（ModuleNotFoundError，2026-08-17）；勿整目录引入
# main.py/run.py 等服务端专用文件（test_build_package_contents 守卫）。
MANIFEST = [
    "src/__init__.py",
    "src/config.py",
    "src/repo_key.py",
    "src/session_manager.py",
    "src/mysql_compat.py",
    "src/user/__init__.py",
    "src/user/db.py",
    "src/api/__init__.py",
    "src/api/routing.py",
    "src/api/time_utils.py",
    "src/api/event_store.py",
    "src/api/event_archive.py",
    "src/api/session_store.py",
    "src/api/knowledge.py",
    "src/agent/",
    "src/runtime/",
    # grape CLI 已拆为独立项目 grape-cli（gitcode.com/grape-dev/grape-cli），
    # 由 install-worker.sh 以 pip git 方式安装（2026-09-08 起不再随包内置）
    "requirements-worker.txt",
]

_build_lock = threading.Lock()


def _tar_filter(ti: tarfile.TarInfo):
    if "__pycache__" in ti.name or ti.name.endswith(".pyc"):
        return None
    return ti


def _manifest_version() -> str:
    """按 manifest 各文件 (size, mtime) 计算内容指纹（代码变更即变化，git 无关）。"""
    h = hashlib.sha256()
    for item in MANIFEST:
        path = os.path.join(ROOT, item)
        if os.path.isdir(path):
            for root, dirs, files in os.walk(path):
                dirs[:] = [d for d in dirs if d != "__pycache__"]
                for f in sorted(files):
                    fp = os.path.join(root, f)
                    if fp.endswith(".pyc"):
                        continue
                    st = os.stat(fp)
                    h.update(fp.encode("utf-8"))
                    h.update(str(st.st_size).encode())
                    h.update(str(st.st_mtime).encode())
        elif os.path.isfile(path):
            st = os.stat(path)
            h.update(path.encode("utf-8"))
            h.update(str(st.st_size).encode())
            h.update(str(st.st_mtime).encode())
    return h.hexdigest()[:16]


def _requirements_hash() -> str:
    """requirements-worker.txt 内容指纹（变更即重新下载 wheels）。"""
    h = hashlib.sha256()
    try:
        if os.path.isfile(REQUIREMENTS_PATH):
            h.update(open(REQUIREMENTS_PATH, "rb").read())
    except Exception:
        pass
    return h.hexdigest()[:16]


# 打包进包的 Python 精简子集：bin/python3.12 + lib/python3.12(stdlib) + libpython*.so。
# 剔除 include(编译扩展用)/share(文档)/tcl/tk/itcl/thread(GUI 与调试，worker 不需要)，
# 每项约省 10MB+。
def _python_subset_entries(py_root: str) -> list[str]:
    """返回要打包的 python 目录内相对路径清单（保留 symlink）。"""
    out: list[str] = []
    for base, dirs, files in os.walk(py_root):
        dirs[:] = [d for d in dirs if d != "__pycache__"]
        rel = os.path.relpath(base, py_root)
        if rel == ".":
            # 只保留 bin/ 与 lib/（lib 下再收 libpython*.so* + python3.X stdlib）
            dirs[:] = [d for d in dirs if d in ("bin", "lib")]
            files[:] = []
            continue
        parts = rel.split(os.sep)
        if parts[0] == "bin":
            # bin 只留 python 可执行（含 python/python3/python3.X 软链）
            files = [f for f in files if f.startswith("python")]
        elif parts[0] == "lib":
            if len(parts) == 1:
                # lib 根：只留 libpython*.so*，去掉 tcl/tk/pkgconfig
                files = [f for f in files if f.startswith("libpython")]
                # 保留实际存在的 python3.X stdlib 目录（3.10/3.11/3.12…，不写死版本）
                dirs[:] = [d for d in dirs
                           if d.startswith("python3.") and d[len("python3."):].isdigit()]
            else:
                # lib/python3.X 整目录保留
                pass
        else:
            dirs[:] = []
            files[:] = []
            continue
        for f in sorted(files):
            if f.endswith(".pyc"):
                continue
            fp = os.path.join(base, f)
            r = os.path.relpath(fp, py_root)
            # 只收文件或软链（跳过其他特殊文件）
            if os.path.isfile(fp) or os.path.islink(fp):
                out.append(r)
    return sorted(out)


def _find_python() -> str | None:
    """定位可打包的 Python 3.10+ standalone 构建目录；找不到返回 None。"""
    def _scan() -> list[str]:
        found: list[str] = []
        try:
            if os.path.isdir(UV_PYTHON_DIR):
                for d in os.listdir(UV_PYTHON_DIR):
                    full = os.path.join(UV_PYTHON_DIR, d)
                    if not os.path.isdir(full) or not d.startswith("cpython-"):
                        continue
                    try:
                        ver = tuple(int(x) for x in d.split("-")[1].split(".")[:2])
                    except Exception:
                        continue
                    if ver >= (3, 10) and os.path.isfile(os.path.join(full, "bin", "python3")):
                        found.append(full)
        except Exception as e:
            log.warning(f"worker_package: scan python dir failed: {e}")
        return found

    candidates = _scan()
    if not candidates:
        # 尝试现场用 uv 装一个（幂等，装过即跳过）
        uv = shutil.which("uv")
        if uv:
            try:
                subprocess.run([uv, "python", "install", "3.12"],
                               capture_output=True, text=True, timeout=300)
                candidates = _scan()
            except Exception as e:
                log.warning(f"worker_package: uv python install failed: {e}")
    if not candidates:
        log.warning("worker_package: no bundled Python found; package will rely on system Python>=3.10")
        return None
    # 版本最高者优先
    def _key(p: str):
        try:
            return tuple(int(x) for x in os.path.basename(p).split("-")[1].split("."))
        except Exception:
            return (0, 0, 0)
    return max(candidates, key=_key)


def _python_fingerprint(py_root: str | None) -> str:
    """python 目录内容指纹（相对路径+size+mtime），变更即触发包重建。"""
    h = hashlib.sha256()
    if not py_root:
        return "none"
    try:
        for r in _python_subset_entries(py_root):
            fp = os.path.join(py_root, r)
            try:
                st = os.lstat(fp)
            except Exception:
                continue
            h.update(r.encode("utf-8"))
            h.update(str(st.st_size).encode())
            h.update(str(int(st.st_mtime)).encode())  # 整秒——跨机器文件系统精度差异不影响指纹
    except Exception:
        pass
    return h.hexdigest()[:8]


def _ensure_wheels(py_version: str = "") -> str:
    """下载 worker 依赖 wheels 到缓存目录（离线安装用），返回目录。

    - py_version 指定目标 Python 版本（如 "3.11"）：与包内 bundle python 匹配，
      保证离线安装可用；为空则按当前解释器（构建机）平台下载；
    - requirements 变更或 py_version 变更时重新下载；
    - 目标机器平台/Python 版本不匹配时离线安装会回退在线（见 install-worker.sh）。
    """
    os.makedirs(WHEELS_DIR, exist_ok=True)
    marker = os.path.join(WHEELS_DIR, ".req-hash")
    cur = f"{py_version}:{_requirements_hash()}"
    old = ""
    try:
        old = open(marker, encoding="utf-8").read().strip()
    except Exception:
        pass
    has_whl = any(f.endswith(".whl") for f in os.listdir(WHEELS_DIR))
    if cur == old and has_whl:
        return WHEELS_DIR
    log.info(f"worker_package: downloading wheels (py={py_version or 'build-host'}, index={PIP_INDEX})")
    for f in os.listdir(WHEELS_DIR):
        if f.endswith(".whl"):
            try:
                os.remove(os.path.join(WHEELS_DIR, f))
            except Exception:
                pass
    try:
        cmd = [sys.executable, "-m", "pip", "download",
               "-r", REQUIREMENTS_PATH, "-d", WHEELS_DIR,
               "--only-binary=:all:", "--retries", "3", "--timeout", "30",
               "-i", PIP_INDEX]
        if py_version:
            cmd += ["--python-version", py_version]
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
        if r.returncode != 0:
            log.warning(f"worker_package: pip download wheels failed: {r.stderr[-400:]}")
    except Exception as e:
        log.warning(f"worker_package: pip download wheels exception: {e}")
    with open(marker, "w", encoding="utf-8") as f:
        f.write(cur)
    return WHEELS_DIR


def _wheels_fingerprint() -> str:
    """wheels 目录内容指纹（文件名+size），纳入包版本使 wheels 变更触发重建。"""
    h = hashlib.sha256()
    try:
        for f in sorted(os.listdir(WHEELS_DIR)):
            if f.endswith(".whl"):
                h.update(f.encode("utf-8"))
                try:
                    h.update(str(os.path.getsize(os.path.join(WHEELS_DIR, f))).encode("utf-8"))
                except Exception:
                    pass
    except Exception:
        pass
    return h.hexdigest()[:8]


def package_version() -> str:
    """当前包版本：git commit/manifest 指纹 + wheels 指纹 + 内置 python 指纹。
    如有未提交改动，追加 -dirty 后缀强制重建包。"""
    base = ""
    try:
        if os.path.isdir(os.path.join(ROOT, ".git")) or os.path.isfile(os.path.join(ROOT, ".git")):
            r = subprocess.run(
                ["git", "-C", ROOT, "rev-parse", "--short", "HEAD"],
                capture_output=True, text=True, timeout=5,
            )
            if r.returncode == 0 and r.stdout.strip():
                base = "git-" + r.stdout.strip()
                # 检测未提交改动（working directory dirty → 版本号变化 → 强制重建包）
                d = subprocess.run(
                    ["git", "-C", ROOT, "status", "--porcelain", "-uno"],
                    capture_output=True, text=True, timeout=5,
                )
                if d.returncode == 0 and d.stdout.strip():
                    base += "-dirty"
    except Exception:
        pass
    if not base:
        base = "h-" + _manifest_version()
    py = _python_fingerprint(_find_python())
    return f"{base}-w{_wheels_fingerprint()}-p{py}"


def _write_sha256sums(tar: tarfile.TarFile, members: list[tuple[str, bytes]]):
    """将各文件 sha256 写入包内 SHA256SUMS（排除自身）。"""
    lines = []
    for arcname, data in members:
        if arcname.endswith("/SHA256SUMS"):
            continue
        lines.append(f"{hashlib.sha256(data).hexdigest()}  {arcname}")
    content = ("\n".join(lines) + "\n").encode("utf-8")
    info = tarfile.TarInfo("grape-worker/SHA256SUMS")
    info.size = len(content)
    tar.addfile(info, io.BytesIO(content))


def _build_async():
    """后台构建安装包（daemon 线程，异常仅告警）。"""
    try:
        build_package()
        log.info(f"worker_package: async prebuild done -> {PKG_PATH}")
    except Exception as e:
        log.warning(f"worker_package: async prebuild failed: {e}")


def ensure_package_ready():
    """确保安装包缓存就绪：缺失或过期时异步后台构建，不阻塞调用方。

    凭据生成/安装包下载等入口调用：用户生成凭据后立即有一键安装包可用，
    避免首次下载时才同步打包（耗时数秒至数分钟，含 wheels 下载）。
    幂等：缓存有效（VERSION 与当前版本一致）时直接返回。
    """
    if os.path.isfile(PKG_PATH):
        try:
            with open(VERSION_PATH, encoding="utf-8") as f:
                cur = f.read().strip()
        except Exception:
            cur = ""
        try:
            if cur == package_version():
                return  # 已就绪
        except Exception:
            pass
    threading.Thread(target=_build_async, daemon=True, name="worker-pkg-prebuild").start()


def build_package() -> str:
    """构建安装包并返回路径。缓存命中直接返回；代码变更后重建（原子替换）。"""
    with _build_lock:
        os.makedirs(CACHE_DIR, exist_ok=True)
        ver = package_version()
        cur = ""
        try:
            with open(VERSION_PATH, encoding="utf-8") as f:
                cur = f.read().strip()
        except Exception:
            pass
        if os.path.isfile(PKG_PATH) and cur == ver:
            return PKG_PATH

        tmp = PKG_PATH + ".tmp"
        members: list = []  # ("arc", bytes) 小文件 / ("arc", path, "path") 大文件流式
        # 内置 Python>=3.10 standalone（离线装 worker 时解释器缺失的兜底；无则靠系统 Python）
        py_root = _find_python()
        # 依赖 wheels：按 bundle python 版本下载（匹配离线安装）；无 bundle 时按构建机
        py_ver = ""
        if py_root:
            import re as _re
            _m = _re.search(r"(\d+\.\d+)", os.path.basename(os.path.normpath(py_root)) or "")
            if _m:
                py_ver = _m.group(1)
        whl_dir = _ensure_wheels(py_ver)
        if py_root:
            log.info(f"worker_package: bundling python {os.path.basename(py_root)}")
        else:
            log.warning("worker_package: no python to bundle (system python required on worker)")
        with tarfile.open(tmp, "w:gz") as tar:
            for item in MANIFEST:
                path = os.path.join(ROOT, item)
                if not os.path.exists(path):
                    continue
                if os.path.isdir(path):
                    for root, dirs, files in os.walk(path):
                        dirs[:] = [d for d in dirs if d != "__pycache__"]
                        for f in sorted(files):
                            fp = os.path.join(root, f)
                            if fp.endswith(".pyc"):
                                continue
                            arc = os.path.relpath(fp, ROOT).replace(os.sep, "/")
                            tar.add(fp, arcname=f"grape-worker/{arc}", filter=_tar_filter)
                            with open(fp, "rb") as fh:
                                members.append((f"grape-worker/{arc}", fh.read()))
                else:
                    arc = item.replace(os.sep, "/")
                    tar.add(path, arcname=f"grape-worker/{arc}", filter=_tar_filter)
                    with open(path, "rb") as fh:
                        members.append((f"grape-worker/{arc}", fh.read()))
            vdata = ver.encode("utf-8")
            info = tarfile.TarInfo("grape-worker/VERSION")
            info.size = len(vdata)
            tar.addfile(info, io.BytesIO(vdata))
            # wheels 目录（离线依赖）
            if os.path.isdir(whl_dir):
                for f in sorted(os.listdir(whl_dir)):
                    if not f.endswith(".whl"):
                        continue
                    fp = os.path.join(whl_dir, f)
                    try:
                        tar.add(fp, arcname=f"grape-worker/wheels/{f}", filter=_tar_filter)
                        with open(fp, "rb") as fh:
                            members.append((f"grape-worker/wheels/{f}", fh.read()))
                    except Exception as e:
                        log.warning(f"worker_package: add wheel {f} failed: {e}")
            # 内置 Python 解释器（python/ 目录，standalone 免编译；含 python 可执行 + stdlib + libpython.so）
            if py_root:
                for r in _python_subset_entries(py_root):
                    fp = os.path.join(py_root, r)
                    arc = f"grape-worker/python/{r}"
                    try:
                        tar.add(fp, arcname=arc, filter=_tar_filter)
                        if os.path.isfile(fp) and not os.path.islink(fp):
                            with open(fp, "rb") as fh:
                                members.append((arc, fh.read()))
                    except Exception as e:
                        log.warning(f"worker_package: add python {r} failed: {e}")
            _write_sha256sums(tar, members)
        os.replace(tmp, PKG_PATH)
        with open(VERSION_PATH, "w", encoding="utf-8") as f:
            f.write(ver)
        return PKG_PATH
