"""Worker 安装包构建（见 worker-server-delivery-design.md）。

服务端按 manifest 将 worker 运行闭包代码打包为 tar.gz，首次请求动态打包、
写入临时文件缓存，代码变更后失效重建。产物由 GET /api/runtime/install/package
（rta_ 鉴权）下发。
"""
import hashlib, io, os, subprocess, tarfile, threading

ROOT = os.path.abspath(os.path.dirname(os.path.dirname(__file__)))
CACHE_DIR = os.path.join(ROOT, "data", "cache", "worker-package")
PKG_PATH = os.path.join(CACHE_DIR, "grape-worker.tar.gz")
VERSION_PATH = os.path.join(CACHE_DIR, "VERSION")

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


def package_version() -> str:
    """当前包版本：优先部署 git commit（有 .git 时），否则 manifest 指纹。"""
    try:
        if os.path.isdir(os.path.join(ROOT, ".git")) or os.path.isfile(os.path.join(ROOT, ".git")):
            r = subprocess.run(
                ["git", "-C", ROOT, "rev-parse", "--short", "HEAD"],
                capture_output=True, text=True, timeout=5,
            )
            if r.returncode == 0 and r.stdout.strip():
                return "git-" + r.stdout.strip()
    except Exception:
        pass
    return "h-" + _manifest_version()


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
            _write_sha256sums(tar, members)
        os.replace(tmp, PKG_PATH)
        with open(VERSION_PATH, "w", encoding="utf-8") as f:
            f.write(ver)
        return PKG_PATH
