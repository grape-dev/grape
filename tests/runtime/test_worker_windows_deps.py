"""worker 在 Windows 上的依赖/平台差异回归（2026-11-19 事故）。

三条真实故障，各一条守卫：

1. `stream failed: No module named 'fastapi'`：worker 任务路径
   agent → mgmt.public.skill_by_id → mgmt.user.db → runtime.public 的**顶层**
   `from fastapi import Request` 只是类型注解，却把 API 层依赖变成 worker 运行时依赖。
2. `OTel init failed (service=grape-task): module 'os' has no attribute 'uname'`。
3. `platform catalog fallback failed: '<安装目录>\\configs\\platforms'`：卡目录没随包下发。
"""
import importlib
import os
import subprocess
import sys

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
PY = sys.executable

_BLOCKER = r'''
import sys, importlib.abc
BLOCK = set(sys.argv[2:])
class B(importlib.abc.MetaPathFinder):
    def find_spec(self, name, path=None, target=None):
        if name in BLOCK or name.split(".")[0] in BLOCK:
            raise ModuleNotFoundError("No module named '" + name + "'", name=name)
        return None
sys.meta_path.insert(0, B())
sys.path.insert(0, sys.argv[1])
'''


def _run(code: str, *args: str, timeout: int = 180):
    return subprocess.run([PY, "-c", code, *args], cwd=REPO, capture_output=True,
                          text=True, timeout=timeout)


def test_runtime_public_imports_without_fastapi():
    """runtime.public 是 worker 任务路径的必经模块，缺 fastapi 也必须能 import。"""
    code = _BLOCKER + "\nimport importlib\nimportlib.import_module('src.domains.runtime.public')\nprint('IMPORT_OK')\n"
    p = _run(code, REPO, "fastapi")
    assert p.returncode == 0, f"runtime.public 仍强依赖 fastapi:\n{p.stderr[-1500:]}"
    assert "IMPORT_OK" in p.stdout


def test_self_check_aborts_when_task_path_import_breaks():
    """预检必须把「任务路径 import 面坏了」判成失败（退出码 79），而不是放行升级。"""
    code = _BLOCKER + (
        "\nimport sys, runpy\n"
        "sys.argv = ['agent', '--self-check']\n"
        "runpy.run_module('src.domains.runtime.engine.agent', run_name='__main__')\n"
    )
    p = _run(code, REPO, "src.domains.insight.public")
    assert p.returncode == 79, f"期望 79（预检失败），实际 {p.returncode}\n{p.stdout[-600:]}\n{p.stderr[-600:]}"
    assert "self-check FAILED" in p.stderr


def test_self_check_passes_on_healthy_tree():
    code = _BLOCKER + (
        "\nimport sys, runpy\n"
        "sys.argv = ['agent', '--self-check']\n"
        "runpy.run_module('src.domains.runtime.engine.agent', run_name='__main__')\n"
    )
    p = _run(code, REPO)  # 不屏蔽任何依赖
    assert p.returncode == 0, p.stderr[-800:]
    assert "self-check ok" in p.stdout and "task_path_imports=" in p.stdout


def test_otel_hostname_is_cross_platform():
    """Windows 没有 os.uname：hostname 取值不得抛 AttributeError。"""
    code = (
        "import os, sys\n"
        "sys.path.insert(0, sys.argv[1])\n"
        "if hasattr(os, 'uname'): del os.uname\n"
        "from src.kernel.otel import _hostname\n"
        "h = _hostname()\n"
        "print('HOST', h)\n"
        "assert h and isinstance(h, str)\n"
    )
    p = _run(code, REPO)
    assert p.returncode == 0, f"无 os.uname 时 _hostname 崩溃:\n{p.stderr[-800:]}"
    assert "HOST" in p.stdout


def test_repo_key_tolerates_missing_platform_catalog(monkeypatch, caplog):
    """卡目录缺席（老 worker 布局）时静默返回空集，不得打 warning。"""
    from src.kernel import repo_key
    monkeypatch.setattr(repo_key, "_repo_root_dir", lambda: "")
    with caplog.at_level("DEBUG", logger="devops-bot"):
        providers = repo_key._catalog_providers_from_dir()
    assert providers == set()
    assert not [r for r in caplog.records if r.levelname == "WARNING"], \
        "卡目录缺席不应产生 WARNING（会污染每台 worker 的启动日志）"


def test_repo_key_reads_shipped_catalog(tmp_path, monkeypatch):
    """随包下发的 configs/platforms/*.json → 平台集可解析（与主控同源）。"""
    from src.kernel import repo_key
    pdir = tmp_path / "configs" / "platforms"
    pdir.mkdir(parents=True)
    for name in ("gitcode.json", "github.json", "gitee.json"):
        (pdir / name).write_text("{}")
    monkeypatch.setenv("GRAPE_CONFIG_DIR", str(tmp_path / "configs"))
    monkeypatch.setattr(repo_key, "_repo_root_dir", repo_key.__dict__["_repo_root_dir"])
    got = repo_key._catalog_providers_from_dir()
    assert got == {"gitcode", "github", "gitee"}, got


def test_worker_package_does_not_ship_configs():
    """设计约束：包内不得带 configs/（含主控 crypto/密钥样例）。

    所以 worker 端「没有平台卡」是正常布局，repo_key 必须静默降级
    （见上一条用例），而不是每进程打一条 WARNING。
    """
    from src.kernel import worker_package
    assert not [m for m in worker_package.MANIFEST if m.startswith("configs")], \
        "configs/ 不得进 worker 包（tests/runtime/test_worker_package.py 亦有硬约束）"
