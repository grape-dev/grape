"""Windows 平台守卫回归：__main__ 段不得出现裸 os.geteuid()。

2026-11-19 事故：worker 包 h-6678e67005b5 的
src/domains/runtime/engine/agent.py __main__ 段写了 `if os.geteuid() == 0 ...`，
os.geteuid 在 Windows 上不存在 → 启动即 AttributeError → daemon 重试 10 次后退出 →
"升级成功但设备离线"，且隐藏窗口下 stderr 丢失、现场无日志。

本测试锁三件事：
1) 删掉 os.geteuid 后（模拟 Windows）走真实启动入口 `--self-check` 必须成功退出；
2) agent.py 里任何 os.geteuid() 调用都必须带 hasattr 守卫；
3) Windows/Linux 两个安装脚本都必须先做启动预检、且 Windows 侧保留启动 stderr。
"""
import os
import re
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
AGENT = REPO / "src/domains/runtime/engine/agent.py"
PS1_START = REPO / "scripts/start-worker.ps1"
PS1_INSTALL = REPO / "scripts/install-worker.ps1"
SH_INSTALL = REPO / "scripts/install-worker.sh"


def _run_py(code: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-c", code],
        cwd=str(REPO), capture_output=True, text=True, timeout=180,
        env={**os.environ, "GRAPE_ALLOW_ROOT_ENGINE": "1", "PYTHONPATH": str(REPO)},
    )


def test_self_check_works_when_geteuid_missing():
    """模拟 Windows（os 无 geteuid）：--self-check 必须正常退出，而不是 AttributeError。"""
    code = (
        "import os, sys, runpy\n"
        "if hasattr(os, 'geteuid'): del os.geteuid\n"
        "assert not hasattr(os, 'geteuid')\n"
        "sys.argv = ['agent', '--self-check']\n"
        "runpy.run_module('src.domains.runtime.engine.agent', run_name='__main__')\n"
    )
    r = _run_py(code)
    assert r.returncode == 0, f"rc={r.returncode}\nstdout={r.stdout[-2000:]}\nstderr={r.stderr[-2000:]}"
    assert "self-check ok" in r.stdout, r.stdout[-800:]


def test_self_check_works_on_posix():
    r = _run_py(
        "import sys; sys.argv = ['agent', '--self-check']\n"
        "import runpy; runpy.run_module('src.domains.runtime.engine.agent', run_name='__main__')\n"
    )
    assert r.returncode == 0, f"rc={r.returncode}\nstdout={r.stdout[-1500:]}\nstderr={r.stderr[-1500:]}"
    assert "self-check ok" in r.stdout


def test_no_unguarded_euid_in_agent():
    """agent.py 的每个 os.geteuid() 调用点都必须在同一表达式里带 hasattr 守卫。"""
    offenders = []
    for i, line in enumerate(AGENT.read_text(encoding="utf-8").splitlines(), 1):
        if "os.geteuid()" in line and "hasattr(os, 'geteuid')" not in line \
                and 'hasattr(os, "geteuid")' not in line:
            offenders.append(f"{AGENT.name}:{i}: {line.strip()}")
    assert not offenders, "未加平台守卫的 os.geteuid():\n" + "\n".join(offenders)


def test_windows_daemon_keeps_boot_log():
    """hidden 窗口下 stderr 会丢：daemon 必须把子进程输出重定向到 data\\logs\\worker-boot.log。"""
    s = PS1_START.read_text(encoding="utf-8")
    assert "worker-boot.log" in s, "start-worker.ps1 未把启动输出落盘"
    assert "2>&1" in s and "$env:ComSpec" in s, "start-worker.ps1 未用 cmd 重定向捕获 stderr"
    assert "Tail 20" in s, "启动失败时未打印日志尾部（现场仍拿不到崩溃原因）"


def test_windows_installer_prechecks_before_stopping_old_worker():
    s = PS1_INSTALL.read_text(encoding="utf-8")
    check_at = s.find("--self-check")
    stop_at = s.find("停掉旧 daemon")
    assert check_at != -1, "install-worker.ps1 缺启动预检（--self-check）"
    assert stop_at != -1, "install-worker.ps1 结构变了：找不到停旧 daemon 段"
    assert check_at < stop_at, "启动预检必须在停旧 worker 之前执行，否则失败即离线"


def test_posix_installer_prechecks_before_stopping_old_worker():
    s = SH_INSTALL.read_text(encoding="utf-8")
    check_at = s.find("_start_check")
    assert check_at != -1, "install-worker.sh 缺 _start_check 启动预检"
    assert s.count("--self-check") >= 1
    # 预检定义必须出现在第 6 步（stop_own_worker）之前
    stop_at = s.find("stop_own_worker", s.find("# ===== 6."))
    assert stop_at != -1
    assert check_at < stop_at
