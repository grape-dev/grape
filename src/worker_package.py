"""Worker 安装包构建（见 worker-server-delivery-design.md）。

服务端按 manifest 将 worker 运行闭包代码打包为 tar.gz，首次请求动态打包、
写入临时文件缓存，代码变更后失效重建。产物由 GET /api/runtime/install/package
（rta_ 鉴权）下发。
"""
import hashlib, io, os, subprocess, sys, tarfile, threading

from src.config import log

ROOT = os.path.abspath(os.path.dirname(os.path.dirname(__file__)))
CACHE_DIR = os.path.join(ROOT, "data", "cache", "worker-package")
PKG_PATH = os.path.join(CACHE_DIR, "grape-worker.tar.gz")
VERSION_PATH = os.path.join(CACHE_DIR, "VERSION")
WHEELS_DIR = os.path.join(CACHE_DIR, "wheels")
REQUIREMENTS_PATH = os.path.join(ROOT, "requirements-worker.txt")
PIP_INDEX = os.environ.get("PIP_INDEX_URL", "https://pypi.tuna.tsinghua.edu.cn/simple")

# worker 运行闭包清单（整目录粒度，含运行时懒加载模块）。
# 依据：src.runtime.agent 的 import 闭包（含 repogit/cleanup 等运行时 import）。
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
    "src/agent/",
    "src/runtime/",
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


def _ensure_wheels() -> str:
    """下载 worker 依赖 wheels 到缓存目录（离线安装用），返回目录。

    - 按当前解释器平台下载（Linux x86_64 + cp312 等），纯 python 包为通用 wheel；
    - requirements 变更或目录无 whl 时重新下载；
    - 目标机器平台/Python 版本不匹配时离线安装会回退在线（见 install-worker.sh）。
    """
    os.makedirs(WHEELS_DIR, exist_ok=True)
    marker = os.path.join(WHEELS_DIR, ".req-hash")
    cur = _requirements_hash()
    old = ""
    try:
        old = open(marker, encoding="utf-8").read().strip()
    except Exception:
        pass
    has_whl = any(f.endswith(".whl") for f in os.listdir(WHEELS_DIR))
    if cur == old and has_whl:
        return WHEELS_DIR
    log.info(f"worker_package: downloading wheels for {REQUIREMENTS_PATH} (index={PIP_INDEX})")
    for f in os.listdir(WHEELS_DIR):
        if f.endswith(".whl"):
            try:
                os.remove(os.path.join(WHEELS_DIR, f))
            except Exception:
                pass
    try:
        r = subprocess.run(
            [sys.executable, "-m", "pip", "download",
             "-r", REQUIREMENTS_PATH, "-d", WHEELS_DIR,
             "--only-binary=:all:", "--retries", "3", "--timeout", "30",
             "-i", PIP_INDEX],
            capture_output=True, text=True, timeout=300,
        )
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
    """当前包版本：优先部署 git commit（有 .git 时），否则 manifest 指纹；附 wheels 指纹。"""
    base = ""
    try:
        if os.path.isdir(os.path.join(ROOT, ".git")) or os.path.isfile(os.path.join(ROOT, ".git")):
            r = subprocess.run(
                ["git", "-C", ROOT, "rev-parse", "--short", "HEAD"],
                capture_output=True, text=True, timeout=5,
            )
            if r.returncode == 0 and r.stdout.strip():
                base = "git-" + r.stdout.strip()
    except Exception:
        pass
    if not base:
        base = "h-" + _manifest_version()
    return f"{base}-w{_wheels_fingerprint()}"


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
        members: list[tuple[str, bytes]] = []
        # 依赖 wheels（离线安装用；下载失败时包内不含 wheels，安装回退在线）
        whl_dir = _ensure_wheels()
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
            _write_sha256sums(tar, members)
        os.replace(tmp, PKG_PATH)
        with open(VERSION_PATH, "w", encoding="utf-8") as f:
            f.write(ver)
        return PKG_PATH
