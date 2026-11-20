"""worker 包构建的跨进程互斥回归。

2026-11-19 事故：/opt/grape/data/cache/worker-package/grape-worker.tar.gz 变成 79MB 的坏包
（`gzip: invalid compressed data--length error`，tar 解不开）。四个进程（mgmt/event/
worker/insight）都会调 build_package()，进程内 threading.Lock 挡不住跨进程：
多个进程同时往同一个 `PKG_PATH + ".tmp"` 写 gzip 流 → 包被交叉覆盖成截断文件。
"""
import os
import subprocess
import sys
import tarfile
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
SRC = REPO / "src/kernel/worker_package.py"


def test_cross_process_lock_serializes(tmp_path):
    """两个独立进程同时进 _crossproc_lock，进入时刻必须串行（不重叠）。"""
    code = (
        "import sys, time\n"
        f"sys.path.insert(0, {str(REPO)!r})\n"
        "import src.kernel.worker_package as wp\n"
        f"wp.CACHE_DIR = {str(tmp_path)!r}\n"
        f"wp.PKG_PATH = {str(tmp_path / 'grape-worker.tar.gz')!r}\n"
        "with wp._crossproc_lock():\n"
        "    print('enter', time.time(), flush=True)\n"
        "    time.sleep(0.6)\n"
        "print('leave', time.time(), flush=True)\n"
    )

    env = {**os.environ, "PYTHONPATH": str(REPO)}
    procs = [subprocess.Popen([sys.executable, "-c", code], cwd=str(REPO), env=env,
                              stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
             for _ in range(2)]
    outs = [p.communicate(timeout=120)[0] for p in procs]
    for p, out in zip(procs, outs):
        assert p.returncode == 0, f"rc={p.returncode}\n{out[-1500:]}"

    enters = sorted(float(line.split()[1]) for out in outs for line in out.splitlines()
                    if line.startswith("enter "))
    assert len(enters) == 2, outs
    # 串行时第二个进程要等第一个释放（0.6s）——间隔必然 > 0.5s
    assert enters[1] - enters[0] > 0.5, f"两次进入未串行: {enters}"


def test_build_tmp_path_is_pid_unique():
    """锁之外的第二道防线：临时文件名带 pid，两个构建不会互相截断。"""
    src = SRC.read_text(encoding="utf-8")
    assert 'tmp = f"{PKG_PATH}.tmp.{os.getpid()}"' in src, "临时文件名未按 pid 唯一化"
    assert 'os.replace(tmp, PKG_PATH)' in src, "构建结果未做原子替换"
    assert "_cleanup_stale_tmp()" in src, "构建入口未清理其它进程遗留的临时包"


def test_parallel_build_package_produces_readable_archive(tmp_path):
    """Spec AC#4：真实并发 build_package() 不得产出截断包（gzip/tar 可解且清单齐全）。

    两个子进程在同一 CACHE_DIR/PKG_PATH 上真实构建（禁用网络类依赖：
    `_find_pythons`/`_ensure_wheels`/grape-cli wheel 全部打桩），构建完各自
    用 tarfile 打开产物；过程经历跨进程 flock，最终文件必须含 VERSION/SHA256SUMS。
    """
    code = f'''
import os, sys, tarfile
sys.path.insert(0, {str(REPO)!r})
import src.kernel.worker_package as wp
cache = {str(tmp_path)!r}
wp.CACHE_DIR = cache
wp.PKG_PATH = os.path.join(cache, 'grape-worker.tar.gz')
wp.VERSION_PATH = os.path.join(cache, 'VERSION')
wp.WHEELS_DIR = os.path.join(cache, 'wheels')
os.makedirs(wp.WHEELS_DIR, exist_ok=True)
wp.MANIFEST = ['pyproject.toml']
wp._find_pythons = lambda: {{}}
wp._ensure_wheels = lambda py_version='': wp.WHEELS_DIR
wp._ensure_grape_cli_wheel = lambda whl_dir: whl_dir
# 每个进程版本号不同，强制两者都走重建路径（避免缓存命中短路）
wp.package_version = lambda force=False: 'h-' + str(os.getpid())
wp._compute_worker_code_version = lambda: 'h-' + str(os.getpid())
pkg = wp.build_package()
with tarfile.open(pkg, 'r:gz') as tar:
    names = set(tar.getnames())
assert 'grape-worker/VERSION' in names, names
assert 'grape-worker/SHA256SUMS' in names, names
print('archive-ok', flush=True)
'''
    env = {**os.environ, "PYTHONPATH": str(REPO)}
    procs = [
        subprocess.Popen(
            [sys.executable, "-c", code], cwd=str(REPO), env=env,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
        )
        for _ in range(2)
    ]
    outs = [p.communicate(timeout=180)[0] for p in procs]
    for p, out in zip(procs, outs):
        assert p.returncode == 0, f"rc={p.returncode}\n{out[-2000:]}"
        assert "archive-ok" in out, out[-2000:]

    final_pkg = tmp_path / "grape-worker.tar.gz"
    with tarfile.open(final_pkg, "r:gz") as tar:
        names = set(tar.getnames())
    assert "grape-worker/VERSION" in names, names
    assert "grape-worker/SHA256SUMS" in names, names
