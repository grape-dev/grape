"""worker 入口路径守卫。

`#699` 的 legacy shim 清理删掉了 `src/runtime/` 等顶层兼容包，但已经装到设备上的
`start-worker.sh`、systemd unit 和第三方 wrapper 可能还固化着
`python -m src.runtime.agent`。代码包里不再有该模块，脚本一跑就
`ModuleNotFoundError: No module named 'src.runtime'`，现场表现就是设备接入不上。

本仓不恢复 legacy shim：`tests/arch/test_domain_boundaries.py::test_legacy_dir_shims_removed`
与 `src/kernel/worker_package.py` 的 MANIFEST 注释都明确禁止（且 MANIFEST 不带该目录，
恢复也到不了设备）。因此守住两条可以被自动验证的约束：

1. 仓库自带的启动模板必须指向 canonical 入口；
2. worker 包必须真的把 canonical 入口打进包里（否则升级后设备照样缺模块）。
"""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
LEGACY_MODULE = "src.runtime.agent"
CANONICAL_MODULE = "src.domains.runtime.engine.agent"


def test_start_worker_template_uses_canonical_module():
    exec_lines = [ln for ln in (ROOT / "start-worker.sh").read_text().splitlines()
                  if ln.startswith("exec ") and "-m src." in ln]
    assert exec_lines, "start-worker.sh 必须 exec 一个模块入口"
    assert all(CANONICAL_MODULE in ln for ln in exec_lines), exec_lines
    assert not any(LEGACY_MODULE in ln for ln in exec_lines), exec_lines


def test_install_worker_targets_canonical_module():
    text = (ROOT / "scripts/install-worker.sh").read_text()
    assert f'WORKER_MODULE="{CANONICAL_MODULE}"' in text


def test_worker_package_ships_canonical_entry():
    text = (ROOT / "src/kernel/worker_package.py").read_text()
    assert '"src/domains/runtime/engine/"' in text, "worker 包必须包含 canonical 引擎入口"
