"""Worker 安装包构建（见 worker-server-delivery-design.md）。

服务端按 manifest 将 worker 运行闭包代码打包为 tar.gz，首次请求动态打包、
写入临时文件缓存，代码变更后失效重建。产物由 GET /api/runtime/install/package
（rta_ 鉴权）下发。
"""
import hashlib, io, os, re, shutil, subprocess, sys, tarfile, threading

from src.config import log

ROOT = os.path.abspath(os.path.dirname(os.path.dirname(__file__)))
CACHE_DIR = os.path.join(ROOT, "data", "cache", "worker-package")
PKG_PATH = os.path.join(CACHE_DIR, "grape-worker.tar.gz")
VERSION_PATH = os.path.join(CACHE_DIR, "VERSION")
WHEELS_DIR = os.path.join(CACHE_DIR, "wheels")
REQUIREMENTS_PATH = os.path.join(ROOT, "requirements-worker.txt")
PIP_INDEX = os.environ.get("PIP_INDEX_URL", "https://pypi.tuna.tsinghua.edu.cn/simple")
# grape CLI 独立项目：打包成 wheel 随 workers 离线安装（issue-45-worker-offline-grape-cli-design.md）
GRAPE_CLI_REPO = os.environ.get("GRAPE_CLI_REPO", "https://gitcode.com/grape-dev/grape-cli.git")
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
    "src/prompt_builder.py",
    # 架构守护 F1（2026-11-10）：prompt_builder 顶层 import src.service_diagnostics
    # （#1858 三面基址的候选地址诊断），漏列会让 worker 包内 prompt_builder 导入即
    # ModuleNotFoundError —— executor 的两处 try/except 静默降级、scheduler 的
    # resolve_task_server_env 直接抛错。纯 stdlib 单文件，随包下发无副作用。
    "src/service_diagnostics.py",
    "src/session_manager.py",
    "src/mysql_compat.py",
    "src/token_service.py",
    "src/token_resolver.py",
    "src/user/__init__.py",
    "src/user/db.py",
    "src/auth/",
    "src/api/__init__.py",
    "src/api/routing.py",
    "src/api/time_utils.py",
    "src/api/event_store.py",
    # event_archive 已于 weekly-05 停写 event.json（数据由 events 表承载）；worker 端无需该模块。
    # 服务端 read 兼容（历史存档回退）保留在服务端代码库，不进 worker 包。
    "src/api/session_store.py",
    "src/api/knowledge.py",
    # Issue #752: worktree_guard（src/runtime/ 整目录入包）在 fallback 扫描路径里
    # lazily import merge_gate.scan_text；漏列会让 unmerged/旧 git 路径静默失效
    # （守卫 fail-open 成空结果），故 merge_gate 必须随包下发。
    "src/api/merge_gate.py",
    "src/agent/",
    "src/runtime/",
    # 架构守护 BLOCKED-1（2026-11-09）：worker 侧 task/engine span 与 trace relay
    # 依赖 src.observability.otel；漏列会让 worker 上所有埋点静默失效
    # （executor/agent 的 import 在 try/except 内，不会有任何告警）。
    # 只带包入口 + otel 客户端，不带 metrics/rollup 等服务端专属模块（依赖 prometheus/DB）。
    "src/observability/__init__.py",
    "src/observability/otel.py",
    # grape CLI 已拆为独立项目 grape-cli（gitcode.com/grape-dev/grape-cli），
    # 由 install-worker.sh 以 pip git 方式安装（2026-09-08 起不再随包内置）
    "requirements-worker.txt",
    # Issue #666 (M4): 随包下发 codegraph 离线安装脚本（降级/离线环境的预装入口；
    # 在线首选 npm install -g @colbymchenry/codegraph，见 install-worker.sh 5.9 步）
    "scripts/install-codegraph.sh",
]

_build_lock = threading.Lock()


def _tar_filter(ti: tarfile.TarInfo):
    if "__pycache__" in ti.name or ti.name.endswith(".pyc"):
        return None
    return ti


def _git(args: list[str]) -> None:
    """执行 git（强制 Jason7602 身份替代：grape-cli 为私有仓库，该身份可读）"""
    subprocess.run(
        ["git", "-c", "url.https://Jason7602@gitcode.com/.insteadOf=https://gitcode.com/"] + args,
        check=True, capture_output=True, text=True, timeout=300,
    )


# Issue #455: 拉取网络抖动——git clone/pull 偶发 HTTP/2 / 连接超时，
# 网络类错误有限次退避重试（4xx 快速失败，不放大上游压力）。
_GIT_NETWORK_ERR_RE = re.compile(
    r"(could not resolve|connection (timed out|refused|reset by peer)|failed to connect|"
    r"http/2|rpc failed|the remote end hung up|early eof|operation timed out|"
    r"unable to access|network is unreachable|ssl connect|timed out|could not read from remote repository)", re.IGNORECASE)
_GIT_AUTH_ERR_RE = re.compile(r"returned error: (401|403|404)")


def _git_retry(args: list[str], retries: int = 3, base_delay: float = 1.0) -> None:
    """git 网络类错误有限次退避重试（默认 3 次：1s/2s/4s）。"""
    import time as _time
    delay = base_delay
    for attempt in range(retries):
        try:
            _git(args)
            return
        except subprocess.CalledProcessError as e:
            err = ((e.stderr or "") + "\n" + (e.stdout or ""))
            if _GIT_AUTH_ERR_RE.search(err) or not _GIT_NETWORK_ERR_RE.search(err):
                raise  # 4xx 权限/资源类或非网络类：快速失败
            if attempt < retries - 1:
                log.warning(
                    f"worker_package git failed (attempt {attempt + 1}/{retries}), retrying in {delay:.1f}s: {err[:200]}")
                _time.sleep(delay)
                delay *= 2
            else:
                raise


def _ensure_grape_cli_wheel(whl_dir: str) -> str:
    """把 grape-cli 构建成 wheel 放入 wheels/（离线随包安装）。

    - 每次构建前先清除 wheels/ 中的旧 `grape-*.whl`（#896）：wheel 文件名含版本号，
      缓存过旧 wheel 后 `pip wheel` 重建同名报错、`pip install` 对同版本不重新
      解压——陈旧 wheel 永不自愈（曾致升级包内 CLI 缺 GRAPE_AUTH_FILE 读文件
      逻辑，dsh 令牌穿透静默失效）。先清后建，保证 wheels 里永远是当前
      grape-cli main 快照；
    - 随后懒拉 grape-cli 源码（浅克隆到 CACHE_DIR，已有则 ff-only 更新），
      `pip wheel` 强制产出到 wheels/；
    - 失败仅告警：安装侧回退在线（现状兜底，不影响包构建）。
    """
    for _old in os.listdir(whl_dir):
        if _old.startswith("grape-") and _old.endswith(".whl"):
            try:
                os.remove(os.path.join(whl_dir, _old))
                log.info(f"worker_package: removed stale grape wheel {_old} (rebuild from current main)")
            except Exception as e:
                log.warning(f"worker_package: failed to remove stale grape wheel {_old}: {e}")
    # 清空后不再做「已有 wheel 即跳过」的短路：wheels/ 里若仍残留 grape-*.whl（如清理失败），
    # 也不能当缓存命中——否则又退回到陈旧 wheel 永不自愈的老问题。
    src = os.path.join(CACHE_DIR, "grape-cli-src")
    try:
        if not os.path.isdir(os.path.join(src, ".git")):
            os.makedirs(os.path.dirname(src), exist_ok=True)
            _git_retry(["clone", "--depth", "1", GRAPE_CLI_REPO, src])
            log.info(f"worker_package: cloned grape-cli -> {src}")
        else:
            try:
                _git_retry(["-C", src, "pull", "--ff-only"])
            except Exception:
                pass  # 更新失败用已有缓存
        subprocess.run(
            [sys.executable, "-m", "pip", "wheel", "--no-cache-dir", src,
             "--no-deps", "-w", whl_dir],
            check=True, capture_output=True, text=True, timeout=600,
        )
        log.info(f"worker_package: grape-cli wheel built into {whl_dir}")
    except Exception as e:
        log.warning(f"worker_package: grape-cli wheel build failed (fallback online install): {e}")
    return whl_dir


def _manifest_version() -> str:
    """按 manifest 各文件内容 sha256 计算指纹（代码变更即变化，git 无关，多 master 一致）。"""
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
                    h.update(fp.encode("utf-8"))
                    try:
                        with open(fp, "rb") as fh:
                            h.update(hashlib.sha256(fh.read()).hexdigest().encode("utf-8"))
                    except Exception:
                        h.update(b"unreadable")
        elif os.path.isfile(path):
            h.update(path.encode("utf-8"))
            try:
                with open(path, "rb") as fh:
                    h.update(hashlib.sha256(fh.read()).hexdigest().encode("utf-8"))
            except Exception:
                h.update(b"unreadable")
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


# Issue #515: 支持打包的 Linux 架构（与 install-worker.sh uname -m 归一化一致）。
# 构建机为 aarch64 时，旧实现只会捆绑单架构解释器 → x86_64 目标机选中后
# `Exec format error` 提前退出、无法到达在线兜底。现按架构双目录捆绑：
# python-x86_64/ 与 python-aarch64/，install-worker.sh 按 uname -m 选对应目录。
PY_ARCH_ALIASES = {
    "x86_64": "x86_64", "amd64": "x86_64",
    "aarch64": "aarch64", "arm64": "aarch64",
}
_UV_INSTALL_REQUEST = "3.12"


def _py_ver_key(py_root: str) -> tuple:
    """从 standalone 目录名解析版本 key（cpython-<ver>-...），失败返回 0 元组。"""
    try:
        return tuple(int(x) for x in os.path.basename(py_root).split("-")[1].split("."))
    except Exception:
        return (0, 0, 0)


def _arch_label(parts: list[str]) -> str | None:
    """从 uv standalone 目录名分段识别架构标签（兼容两种命名），非支持架构返回 None。

    - 新式：cpython-3.12.8-x86_64-unknown-linux-gnu
    - 旧式：cpython-3.11.15-linux-aarch64-gnu
    """
    for p in parts:
        if p in PY_ARCH_ALIASES:
            return PY_ARCH_ALIASES[p]
    return None


def _scan_pythons() -> dict[str, str]:
    """扫描 UV_PYTHON_DIR 下可打包的 Linux standalone Python，返回 {arch_label: py_root}。

    每个架构取版本最高者；仅收 linux standalone（worker 安装目标是 linux），
    mac/win 平台由 wheels 矩阵与 ps1 覆盖，不入包。
    """
    found: dict[str, str] = {}
    try:
        if os.path.isdir(UV_PYTHON_DIR):
            for d in os.listdir(UV_PYTHON_DIR):
                full = os.path.join(UV_PYTHON_DIR, d)
                if not os.path.isdir(full) or not d.startswith("cpython-"):
                    continue
                parts = d.split("-")
                try:
                    ver = tuple(int(x) for x in parts[1].split(".")[:2])
                except Exception:
                    continue
                if "linux" not in parts or ver < (3, 10):
                    continue
                arch = _arch_label(parts)
                if arch is None:
                    continue
                if not os.path.isfile(os.path.join(full, "bin", "python3")):
                    continue
                if arch not in found or _py_ver_key(full) > _py_ver_key(found[arch]):
                    found[arch] = full
    except Exception as e:
        log.warning(f"worker_package: scan python dir failed: {e}")
    return found


def _find_pythons() -> dict[str, str]:
    """定位可打包的 Python 3.10+ standalone 构建（按架构），缺失架构现场用 uv 补齐。

    返回 {arch_label: py_root}；找不到任何 python 返回空 dict（包将依赖系统 Python）。
    uv 安装失败（请求串不被支持 / 网络不可达）仅告警（含 stderr 尾部）：保持单架构
    打包 + 安装端 --version 校验兜底，不阻断主流程。

    交叉架构安装用完整请求串 `cpython-3.12-linux-<arch>-gnu`（uv 官方 Python 请求
    格式 <impl>-<ver>-<os>-<arch>-<libc>）。`uv python install` 没有 --arch 参数
    （本机 uv 0.12.3 实测 `unexpected argument '--arch'` rc=2），完整请求串随
    uv 任意版本可用；实测 3.12 → 落盘 cpython-3.12.13-linux-x86_64-gnu。
    """
    found = _scan_pythons()
    if set(found) == set(PY_ARCH_ALIASES.values()):
        return found
    uv = shutil.which("uv")
    if uv:
        for arch in sorted(PY_ARCH_ALIASES.values()):
            if arch in found:
                continue
            request = f"cpython-{_UV_INSTALL_REQUEST}-linux-{arch}-gnu"
            try:
                r = subprocess.run([uv, "python", "install", request],
                                   capture_output=True, text=True, timeout=300)
                # QA R2：非零退出不触发 except，必须显式检查并告警（附 stderr 尾部），
                # 否则补装静默失败、双架构悄悄退化为单架构无人知晓
                if r.returncode != 0:
                    log.warning(
                        f"worker_package: uv python install ({request}) failed "
                        f"with rc={r.returncode}: {(r.stderr or '')[-300:]}")
            except Exception as e:
                log.warning(f"worker_package: uv python install ({request}) failed: {e}")
        found = _scan_pythons()
    if not found:
        log.warning("worker_package: no bundled Python found; package will rely on system Python>=3.10")
    return found


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


def _pythons_fingerprint(pythons: dict[str, str]) -> str:
    """多架构 python 目录联合指纹（架构标签 + 单目录指纹），任一变化触发包重建。"""
    h = hashlib.sha256()
    for arch in sorted(pythons):
        h.update(arch.encode("utf-8"))
        h.update(_python_fingerprint(pythons[arch]).encode("utf-8"))
    return h.hexdigest()[:8]


# Issue #499: 随包预置多平台 wheels 矩阵——安装端 pip 按 wheel 平台标签自动精确匹配
# 本机平台（不匹配的 wheel 自动跳过），矩阵外平台（macOS 等）离线失败回退在线安装。
# 保留 aarch64（构建机原生平台，现有 arm worker 离线安装能力不回退）。
WHEEL_PLATFORMS = [
    ("manylinux2014_x86_64", "linux x86_64"),
    ("manylinux2014_aarch64", "linux aarch64"),
    ("win_amd64", "windows amd64"),
]


def _pip_download_wheels(dest: str, platform_args: list[str], py_version: str) -> int:
    """pip download 依赖 wheels 到 dest（可带跨平台 --platform 参数），返回退出码。

    失败仅告警不抛异常：单平台失败由安装端在线兜底（见 install-worker.sh/ps1）。
    """
    cmd = [sys.executable, "-m", "pip", "download",
           "-r", REQUIREMENTS_PATH, "-d", dest,
           "--only-binary=:all:", "--retries", "3", "--timeout", "30",
           "-i", PIP_INDEX]
    if py_version:
        cmd += ["--python-version", py_version]
    cmd += platform_args
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
        if r.returncode != 0:
            log.warning(f"worker_package: pip download wheels failed: {r.stderr[-400:]}")
        return r.returncode
    except Exception as e:
        log.warning(f"worker_package: pip download wheels exception: {e}")
        return 1


def _wheels_cache_key(py_version: str) -> str:
    """wheels 缓存键：py_version + 平台矩阵 + requirements 指纹（任一变更重新下载）。"""
    return f"{py_version}:{'|'.join(p for p, _ in WHEEL_PLATFORMS)}:{_requirements_hash()}"


def _ensure_wheels(py_version: str = "") -> str:
    """按平台矩阵下载 worker 依赖 wheels 到缓存目录（多平台预置），返回目录。

    - 按 WHEEL_PLATFORMS 矩阵为每个平台各下载一份 wheels（linux x86_64 /
      linux aarch64 / win_amd64），统一平铺混装于 WHEELS_DIR——纯 Python wheel
      天然去重，二进制 wheel 文件名带平台标签互不冲突；
    - 安装端 `pip install --no-index --find-links <wheels>` 按 wheel 平台标签
      自动精确匹配本机平台（不兼容 wheel 被跳过），实现常见平台首装离线；
    - py_version 指定目标 Python 版本（与包内 bundle python 匹配）；为空则按
      当前解释器（构建机）版本；
    - 矩阵外平台（macOS/其他架构）或 Python 版本不匹配时安装端回退在线。
    """
    os.makedirs(WHEELS_DIR, exist_ok=True)
    marker = os.path.join(WHEELS_DIR, ".req-hash")
    cur = _wheels_cache_key(py_version)
    old = ""
    try:
        old = open(marker, encoding="utf-8").read().strip()
    except Exception:
        pass
    has_whl = any(f.endswith(".whl") for f in os.listdir(WHEELS_DIR))
    if cur == old and has_whl:
        return WHEELS_DIR
    dl_py = py_version or f"{sys.version_info.major}.{sys.version_info.minor}"
    log.info(
        f"worker_package: downloading wheels for platforms "
        f"{[label for _, label in WHEEL_PLATFORMS]} (py={dl_py}, index={PIP_INDEX})"
    )
    for f in os.listdir(WHEELS_DIR):
        if f.endswith(".whl"):
            try:
                os.remove(os.path.join(WHEELS_DIR, f))
            except Exception:
                pass
    for plat, label in WHEEL_PLATFORMS:
        rc = _pip_download_wheels(WHEELS_DIR, ["--platform", plat], dl_py)
        if rc == 0:
            log.info(f"worker_package: wheels ready for {label}")
        else:
            log.warning(f"worker_package: wheels download failed for {label} (install side will fall back online)")
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


_cached_package_version = ""
_cached_package_version_ts = 0.0
_PACKAGE_VERSION_TTL = float(os.environ.get("PACKAGE_VERSION_TTL", "60"))


def package_version(force: bool = False) -> str:
    """当前包版本：git commit/manifest 指纹 + wheels 指纹 + 内置 python 指纹。

    带 TTL 缓存：心跳高频调用（默认 15s 间隔），但版本变更频率远低于此，
    缓存 60s 可避免 95%+ 的重复 git 子进程 + manifest 扫描开销。

    force=True 绕过 TTL 缓存强制重算：打包重建路径专用（QA R1）——
    _find_pythons() 可能刚补装了缺失架构，TTL 缓存窗口内会返回补装前的
    单架构指纹，导致包内容（双架构）与 VERSION 戳（单架构指纹）不一致。
    """
    import time
    global _cached_package_version, _cached_package_version_ts
    _now = time.monotonic()
    if not force and _cached_package_version and (_now - _cached_package_version_ts) < _PACKAGE_VERSION_TTL:
        return _cached_package_version
    base = ""
    try:
        if os.path.isdir(os.path.join(ROOT, ".git")) or os.path.isfile(os.path.join(ROOT, ".git")):
            r = subprocess.run(
                ["git", "-C", ROOT, "rev-parse", "--short=12", "HEAD"],
                capture_output=True, text=True, timeout=5,
            )
            if r.returncode == 0 and r.stdout.strip():
                base = "git-" + r.stdout.strip()
    except Exception:
        pass
    if not base:
        base = "h-" + _manifest_version()
    # 只用无副作用的扫描（心跳热路径不触发 uv 安装；安装补齐仅在 build_package 重建路径）
    py = _pythons_fingerprint(_scan_pythons())
    ver = f"{base}-w{_wheels_fingerprint()}-p{py}"
    _cached_package_version = ver
    _cached_package_version_ts = _now
    return ver


# 缓存：worker 代码版本（仅 git commit，不含 wheels/python 指纹）
_cached_code_version = ""
_cached_code_version_ts = 0.0

def worker_code_version() -> str:
    """Worker 代码版本：仅 git commit hash（不含 wheels/python 指纹）。

    用于自动升级版本对比——只在代码变更时触发升级，
    wheels/python 变化不触发（避免无限升级循环）。

    带 TTL 缓存（60s），与 package_version 共享缓存周期。
    """
    import time
    global _cached_code_version, _cached_code_version_ts
    _now = time.monotonic()
    if _cached_code_version and (_now - _cached_code_version_ts) < _PACKAGE_VERSION_TTL:
        return _cached_code_version
    ver = _compute_worker_code_version()
    _cached_code_version = ver
    _cached_code_version_ts = _now
    return ver


def _compute_worker_code_version() -> str:
    """无 TTL 缓存、强制重算的代码版本（打包专用）。

    事故复盘（2026-10-15 / 2026-10-18 两次 worker 循环下载打满 master 出口带宽）：
    双主升级时，旧进程若在 TTL 缓存窗口内触发 build_package()，tar 打入的是
    磁盘上的新代码，但包内 VERSION 戳的是缓存里的旧版本号 → 产出"新代码+旧戳"
    的坏包；新进程的 heartbeat 广播新版本号，worker 装多少遍都对不上 → 每 2 分钟
    重下 28MB 死循环。打包是低频重操作，这里强制重算 manifest 指纹（毫秒级），
    不走 TTL 缓存，杜绝版本戳与包内代码不一致。
    """
    # 不依赖 git HEAD：多 master 的 git 状态可能不一致（不同分支/dirty/commit），
    # 用 manifest 文件内容指纹计算版本，只要文件内容一致 → 版本一致。
    return "h-" + _manifest_version()[:12]


def normalize_worker_version(ver: str) -> str:
    """归一化 worker 版本串，兼容老格式（Issue #257 B5）。

    老格式版本串形如：
      h-{manifest16}-w{wheels_fingerprint}-p{python_fingerprint}
      git-{commit12}-w{...}-p{...}
    新格式：
      h-{manifest12}

    本函数剥离 -w/-p 后缀，截取代码指纹主体到 12 位，使老格式版本能与新格式
    正确比对——避免「代码相同但版本串格式不同」导致的无限升级循环。

    无法识别的格式原样返回（空串原样返回）。
    """
    if not ver:
        return ""
    ver = ver.strip()
    # 剥离 -w... -p... 后缀（老格式 package_version 附加的 wheels/python 指纹）
    import re as _re
    ver = _re.split(r'-w[a-f0-9]+-p[a-f0-9]+', ver, maxsplit=1)[0]
    # h- 前缀：截取指纹主体到 12 位（新格式标准长度）
    if ver.startswith("h-") and len(ver) > 14:
        ver = "h-" + ver[2:14]
    return ver


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


def _cached_pkg_code_version() -> str:
    """读取缓存包内实际戳的 VERSION（读失败返回空串）。

    事故复盘（2026-10-15 / 2026-10-18）：升级窗口期旧进程在 TTL 缓存窗口内打包，
    产出"新代码+旧版本戳"的坏包后，缓存目录 VERSION 记录（package_version 全量指纹）
    却与磁盘代码一致 → 新进程 build_package() 判定缓存有效永不重建，worker 陷入
    循环下载。这里把"包内 VERSION 戳"作为第二道防线：与强制重算的代码版本不一致
    即废弃缓存包重建。
    """
    try:
        import gzip
        with tarfile.open(PKG_PATH, "r:gz") as tar:
            member = tar.extractfile("grape-worker/VERSION")
            if member is None:
                return ""
            return member.read().decode("utf-8", errors="replace").strip()
    except Exception:
        return ""


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
            # 第二道防线：缓存记录虽匹配，但包内 VERSION 戳可能是升级窗口期
            # 旧进程打出的旧值（见 _cached_pkg_code_version 事故复盘）——不一致即废弃重建
            _stamp = _cached_pkg_code_version()
            if _stamp and _stamp != _compute_worker_code_version():
                log.warning(
                    f"worker_package: cached package stamp mismatch "
                    f"(pkg={_stamp}, code={_compute_worker_code_version()}), rebuilding"
                )
            else:
                return PKG_PATH

        # 重建路径：先确保解释器就绪（可能现场 uv 补齐缺失架构），
        # 再强制重算版本（force=True 绕过 TTL 缓存，QA R1——_find_pythons 刚补装的
        # 架构不在缓存指纹里，必须重扫才能保证指纹与包内容一致）
        pythons = _find_pythons()
        ver = package_version(force=True)

        tmp = PKG_PATH + ".tmp"
        members: list = []  # ("arc", bytes) 小文件 / ("arc", path, "path") 大文件流式
        # 内置 Python>=3.10 standalone（按架构分目录，Issue #515；无则靠系统 Python）
        # 依赖 wheels：按 bundle python 版本下载（匹配离线安装）；无 bundle 时按构建机。
        # 多架构版本不一致时取最高版本（双架构通常 uv 同请求安装、版本一致）。
        py_ver = ""
        if pythons:
            import re as _re
            _vers = []
            for py_root in pythons.values():
                _m = _re.search(r"(\d+\.\d+)", os.path.basename(os.path.normpath(py_root)) or "")
                if _m:
                    _vers.append(tuple(int(x) for x in _m.group(1).split(".")))
            if _vers:
                py_ver = ".".join(str(x) for x in max(_vers))
        whl_dir = _ensure_wheels(py_ver)
        _ensure_grape_cli_wheel(whl_dir)  # grape-cli 轮子随包（离线安装，issue-45）
        if pythons:
            log.info(f"worker_package: bundling python per-arch: {sorted(pythons)}")
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
            # 包内 VERSION 文件写入代码版本（仅 git commit，不含 wheels/python 指纹）
            # worker 端读此文件与主控 heartbeat 中的 latest_version 对比
            # 打包路径强制重算（不走 TTL 缓存）：确保版本戳与包内代码一致
            _code_ver = _compute_worker_code_version()
            vdata = _code_ver.encode("utf-8")
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
            # 内置 Python 解释器（按架构分目录 python-<arch>/，standalone 免编译；
            # 含 python 可执行 + stdlib + libpython.so；install-worker.sh 按 uname -m 选目录）
            for arch in sorted(pythons):
                py_root = pythons[arch]
                for r in _python_subset_entries(py_root):
                    fp = os.path.join(py_root, r)
                    arc = f"grape-worker/python-{arch}/{r}"
                    try:
                        tar.add(fp, arcname=arc, filter=_tar_filter)
                        if os.path.isfile(fp) and not os.path.islink(fp):
                            with open(fp, "rb") as fh:
                                members.append((arc, fh.read()))
                    except Exception as e:
                        log.warning(f"worker_package: add python {arch}/{r} failed: {e}")
            _write_sha256sums(tar, members)
        os.replace(tmp, PKG_PATH)
        with open(VERSION_PATH, "w", encoding="utf-8") as f:
            f.write(ver)
        return PKG_PATH
