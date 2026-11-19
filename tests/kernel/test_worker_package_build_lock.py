"""worker 包构建的跨进程互斥回归。

2026-11-19 事故：/opt/grape/data/cache/worker-package/grape-worker.tar.gz 变成 79MB 的坏包
（`gzip: invalid compressed data--length error`，tar 解不开）。四个进程（mgmt/event/
worker/insight）都会调 build_package()，进程内 threading.Lock 挡不住跨进程：
多个进程同时往同一个 `PKG_PATH + ".tmp"` 写 gzip 流 → 包被交叉覆盖成截断文件。
"""
import os
import subprocess
import sys
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
        "t0 = time.time()\n"
        "with wp._crossproc_lock():\n"
        "    print('enter', round(time.time() - t0, 3), flush=True)\n"
        "    time.sleep(0.6)\n"
        "print('leave', round(time.time() - t0, 3), flush=True)\n"
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