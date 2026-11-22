"""Issue #1931 A 项：发布包只过滤「启动期生成物」，入库的契约副本必须打包。

口径（静态根迁移后）：`help/api/openapi.json` 是服务启动时落盘、被 .gitignore 的生成物，
打包按目录遍历、gitignore 不生效 → 必须由 `_tar_filter()` 显式过滤；
`src/api/frontend/shell/openapi.json` 是**入仓的部署即用副本**（/static/openapi.json 的
实际下载源），必须留在包里，否则新装环境拿不到契约。
"""
import importlib.util
import os
import sys
import tarfile
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO_ROOT))

_SCRIPT = os.path.join(str(_REPO_ROOT), "scripts", "release_pack.py")
_spec = importlib.util.spec_from_file_location("release_pack", _SCRIPT)
rp = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(rp)

SHELL_OPENAPI = "src/api/frontend/shell/openapi.json"


def test_tar_filter_excludes_generated_help_openapi_json():
    assert rp._tar_filter(tarfile.TarInfo("help/api/openapi.json")) is None


def test_tar_filter_keeps_checked_in_shell_openapi_json():
    ti = rp._tar_filter(tarfile.TarInfo(SHELL_OPENAPI))
    assert ti is not None and ti.name == SHELL_OPENAPI


def test_tar_filter_keeps_normal_files():
    for name in ("src/api/frontend/shell/js/app.js", "src/api/main.py",
                 "src/kernel/help_page_gen.py", "help/api/usage.md"):
        ti = rp._tar_filter(tarfile.TarInfo(name))
        assert ti is not None and ti.name == name


def test_packed_tar_filters_generated_but_keeps_checked_in_copy(tmp_path):
    src_dir = tmp_path / "tree"
    (src_dir / "src/api/frontend/shell").mkdir(parents=True)
    (src_dir / "help/api").mkdir(parents=True)
    (src_dir / "help/api/openapi.json").write_text("{}", encoding="utf-8")
    (src_dir / SHELL_OPENAPI).write_text("{}", encoding="utf-8")
    (src_dir / "src/api/frontend/shell/js").mkdir(parents=True)
    (src_dir / "src/api/frontend/shell/js/app.js").write_text("// js", encoding="utf-8")
    (src_dir / "help/api/usage.md").write_text("# md", encoding="utf-8")

    out = tmp_path / "pkg.tar.gz"
    rp.make_subtar_gz(str(src_dir), str(out))
    with tarfile.open(out, "r:gz") as tar:
        names = tar.getnames()
    assert "help/api/openapi.json" not in names, f"生成物不该入包: {names}"
    assert SHELL_OPENAPI in names, f"入库契约副本必须入包: {names}"
    assert "src/api/frontend/shell/js/app.js" in names


# ===================== Issue #3756：契约登记表必须随后端包落盘 =====================
def test_backend_file_items_include_contracts_dir():
    """后端包收集项必须纳入 contracts/ 整目录（登记表随产物发布）。"""
    items = rp._backend_file_items()
    arcnames = {arc for _src, arc in items}
    assert "contracts" in arcnames, f"后端包未纳入 contracts/: {sorted(arcnames)}"


def test_assert_contracts_ready_passes_on_repo():
    rp.assert_contracts_ready(_REPO_ROOT)


def test_assert_contracts_ready_fatal_when_registry_missing(tmp_path, capsys):
    """缺登记表 → 打包以 [FATAL] 失败（不得静默出包）。"""
    import pytest

    with pytest.raises(SystemExit) as exc:
        rp.assert_contracts_ready(tmp_path)
    assert exc.value.code == 1
    out = capsys.readouterr().out
    assert "[FATAL]" in out
    assert "deprecated_endpoints.json" in out


def test_assert_contracts_ready_fatal_when_released_empty(tmp_path, capsys):
    """关键文件齐备但 released/ 无快照 → 同样 FATAL。"""
    import pytest

    contracts = tmp_path / "contracts"
    (contracts / "released").mkdir(parents=True)
    for name in ("deprecated_endpoints.json", "deprecated_exemptions.json",
                 "openapi_baseline.json", "required_operations.json"):
        (contracts / name).write_text("{}", encoding="utf-8")
    with pytest.raises(SystemExit) as exc:
        rp.assert_contracts_ready(tmp_path)
    assert exc.value.code == 1
    assert "[FATAL]" in capsys.readouterr().out