"""Issue #3874：Windows worker onboard 三段修复回归。

1. pyyaml 进入 worker 依赖清单（requirements-worker.txt / pyproject [worker] /
   WORKER_DEPS），离线 wheels 平台矩阵（含 win_amd64）随包覆盖；
2. 降级解析器遇 sequences 不再抛异常（见 tests/test_engine_model_2545.py）；
3. install-worker.ps1 支持 -SkipEngineCheck 且校验失败软化为 warning
   （见 tests/test_worker_install_model_gate_2545.py）。
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]


def _base(dep: str) -> str:
    return re.split(r"[><=!~;\[ ]", dep, maxsplit=1)[0].strip().lower().replace("-", "_")


def test_requirements_worker_declares_pyyaml():
    text = (REPO / "requirements-worker.txt").read_text(encoding="utf-8")
    declared = {_base(line) for line in text.splitlines()
                if line.strip() and not line.strip().startswith("#")}
    assert "pyyaml" in declared, "requirements-worker.txt 必须声明 pyyaml（Issue #3874）"


def test_pyproject_worker_extra_declares_pyyaml():
    text = (REPO / "pyproject.toml").read_text(encoding="utf-8")
    block = re.search(r"^worker = \[(.*?)^\]", text, re.M | re.S)
    assert block, "pyproject.toml 缺少 [worker] extra"
    declared = {_base(d.strip().strip('"')) for d in block.group(1).splitlines()
                if d.strip().startswith('"')}
    assert "pyyaml" in declared, "pyproject [worker] 必须与 requirements-worker.txt 同源声明 pyyaml"


def test_worker_deps_bundles_pyyaml():
    sys.path.insert(0, str(REPO))
    from src.kernel.worker_package import WORKER_DEPS

    assert "pyyaml" in {_base(d) for d in WORKER_DEPS}, \
        "WORKER_DEPS 必须包含 pyyaml，否则离线平台矩阵（含 win_amd64）不下载其 wheel"


# ===== 平台标签覆盖：基名存在 ≠ 目标平台可装（Issue #3874 PR #2003 审查建议）=====

def _wp():
    sys.path.insert(0, str(REPO))
    from src.kernel import worker_package

    return worker_package


def _touch(path: Path, name: str) -> None:
    (path / name).write_bytes(b"")


def test_missing_platform_wheels_flags_pyyaml_without_win_amd64(tmp_path):
    """只打出 linux wheel 的 pyyaml：基名校验通过，但 win_amd64 平台必须报缺口。"""
    wp = _wp()
    _touch(tmp_path, "pyyaml-6.0.2-cp312-cp312-manylinux2014_x86_64.whl")
    _touch(tmp_path, "pyyaml-6.0.2-cp312-cp312-manylinux2014_aarch64.whl")
    gaps = wp._missing_platform_wheels(str(tmp_path))
    assert "pyyaml[windows amd64]" in gaps, gaps
    # 基名校验对这种目录是放行的——正是本次要补的盲区
    assert "pyyaml>=6.0" not in wp._missing_dep_wheels(str(tmp_path))


def test_missing_platform_wheels_accepts_pyyaml_win_amd64(tmp_path):
    """含 pyyaml win_amd64 wheel → Windows 平台不再报缺口（验收项「离线可用」）。"""
    wp = _wp()
    _touch(tmp_path, "pyyaml-6.0.2-cp312-cp312-win_amd64.whl")
    gaps = wp._missing_platform_wheels(str(tmp_path))
    assert "pyyaml[windows amd64]" not in gaps, gaps
    # 只打 win wheel 时 linux 平台应仍为缺口（矩阵按平台独立判定）
    assert "pyyaml[linux x86_64]" in gaps, gaps


def test_missing_platform_wheels_treats_pure_python_wheel_as_all_platforms(tmp_path):
    """纯 Python wheel（none-any）覆盖全部平台，不得误报缺口。"""
    wp = _wp()
    _touch(tmp_path, "pyyaml-6.0.2-py3-none-any.whl")
    gaps = wp._missing_platform_wheels(str(tmp_path))
    assert not [g for g in gaps if g.startswith("pyyaml[")], gaps


def test_wheel_platform_matrix_includes_win_amd64():
    """WHEEL_PLATFORMS 必须含 win_amd64，否则 bundled wheels 不会下载 Windows wheel。"""
    wp = _wp()
    assert "win_amd64" in {plat for plat, _ in wp.WHEEL_PLATFORMS}


def test_windows_smoke_gate_runs_3874_regressions():
    """三个回归文件必须被 CI 门禁执行（Issue #3874 PR #2003 审查 Required）。"""
    workflow = (REPO / ".github" / "workflows" / "windows-smoke.yml").read_text(encoding="utf-8")
    for rel in ("tests/runtime/test_issue_3874_worker_onboard.py",
                "tests/test_engine_model_2545.py",
                "tests/test_worker_install_model_gate_2545.py"):
        assert rel in workflow, f"{rel} 未接入 windows-smoke 门禁"