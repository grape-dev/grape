"""防御测试：config.py 不应依赖 src.auth——worker 共享此文件。

Issue #333 问题 13：config.py 顶层 import src.auth.security_config 导致
worker 启动链污染。删除 IS_PRODUCTION 死代码后，此测试确保不会回归。
"""
import subprocess
import sys


def test_config_no_auth_dependency():
    """config.py 不应 import src.auth.security_config——worker 共享此文件。

    在干净子进程中 import src.config，验证不拉起 src.auth 模块。
    """
    result = subprocess.run(
        [sys.executable, '-c',
         'import src.config; '
         'import sys; '
         'auth_mods = [m for m in sys.modules if m.startswith("src.auth")]; '
         'print(",".join(auth_mods) if auth_mods else "CLEAN")'],
        capture_output=True, text=True, timeout=10
    )
    assert result.returncode == 0, f"import src.config failed: {result.stderr}"
    stdout = result.stdout.strip()
    assert stdout == "CLEAN", f"config.py pulled in src.auth modules: {stdout}"


def test_is_production_removed():
    """IS_PRODUCTION 变量应已从 config.py 删除（零消费死代码）。"""
    result = subprocess.run(
        [sys.executable, '-c',
         'from src.config import __dict__ as cfg; '
         'print("IS_PRODUCTION" in cfg)'],
        capture_output=True, text=True, timeout=10
    )
    assert result.returncode == 0, f"import failed: {result.stderr}"
    assert result.stdout.strip() == "False", "IS_PRODUCTION still exists in config.py"
