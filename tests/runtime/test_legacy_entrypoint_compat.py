"""worker 入口路径守卫。

`#699` 的 legacy shim 清理删掉了 `src/runtime/` 等顶层兼容包，但已经装到设备上的
`start-worker.sh`、systemd unit 和第三方 wrapper 可能还固化着
`python -m src.runtime.agent`。代码包里不再有该模块，脚本一跑就
`ModuleNotFoundError: No module named 'src.runtime'`，现场表现就是设备接入不上。

两手都要，缺一条都不够：

1. **新脚本走 canonical 入口**：仓库自带的启动模板必须指向
   `src.domains.runtime.engine.agent`，与 `install-worker.sh` 的 `WORKER_MODULE` 一致；
2. **旧脚本还能跑**：仓库树里不能恢复 `src/runtime/`
   （`tests/arch/test_domain_boundaries.py::test_legacy_dir_shims_removed` 与
   `test_issue_2010_src_top_level_layout.py` 都禁止），所以 shim 源码放在
   `packaging/legacy-entry-src/`，由 `worker_package.build_package()` 映射成包内
   `src/runtime/*`。只改脚本不补 shim，等于把「换了代码但没重跑安装脚本」的老设备
   直接打死——它们的命令行还固化着 `-m src.runtime.agent`，连进程都起不来。

本文件把两条都钉住：模板/安装脚本指 canonical、包内确实带 shim、shim 只做转发。
"""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
LEGACY_MODULE = "src.runtime.agent"
CANONICAL_MODULE = "src.domains.runtime.engine.agent"
LEGACY_ENTRY_SRC = ROOT / "packaging" / "legacy-entry-src" / "runtime"


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


def test_repo_tree_has_no_legacy_runtime_package():
    """shim 只能存在于 packaging/，仓库 src/ 树里必须没有（与 arch 守卫同口径）。"""
    assert not (ROOT / "src" / "runtime").exists()


def test_worker_package_injects_the_legacy_entry_shim():
    """包内必须带 src/runtime/{__init__,agent,cloud_worker}.py，否则老设备换代码即崩。"""
    from src.kernel import worker_package as wp

    members = dict(wp.legacy_entry_members())
    assert set(members) == {
        "src/runtime/__init__.py",
        "src/runtime/agent.py",
        "src/runtime/cloud_worker.py",
    }, sorted(members)
    for arc, fp in members.items():
        assert Path(fp).is_file(), f"{arc} 缺源文件"
    # MANIFEST 本身不得出现 legacy 顶层路径（test_issue_2010 的守卫口径）
    assert not [i for i in wp.MANIFEST if i.startswith("src/runtime")]


def test_legacy_shim_only_forwards_to_canonical_modules():
    """shim 只准转发：不留自己的逻辑，目标必须是 canonical 引擎入口。"""
    agent = (LEGACY_ENTRY_SRC / "agent.py").read_text()
    cloud = (LEGACY_ENTRY_SRC / "cloud_worker.py").read_text()
    assert f'CANONICAL_MODULE = "{CANONICAL_MODULE}"' in agent
    assert 'CANONICAL_MODULE = "src.domains.runtime.engine.cloud_worker"' in cloud
    for text in (agent, cloud):
        assert 'runpy.run_module(CANONICAL_MODULE, run_name="__main__")' in text
        # 不允许把业务逻辑又长回 shim 里
        assert "asyncio.run(" not in text


def test_packaging_fails_closed_when_the_shim_source_is_missing(monkeypatch, tmp_path):
    """部署漏拷 packaging/ 时必须打包失败，而不是静默打出不含 shim 的包。"""
    import pytest

    from src.kernel import worker_package as wp

    monkeypatch.setattr(wp, "LEGACY_ENTRY_SRC", str(tmp_path))
    with pytest.raises(RuntimeError, match="legacy 入口 shim 源缺失"):
        wp.require_legacy_entry_members()