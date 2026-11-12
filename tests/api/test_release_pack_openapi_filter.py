"""Issue #1931 A 项：发布包不得携带 openapi.json 契约生成物单测（新架构移植版）。

`src/api/static/openapi.json` 与 `help/api/openapi.json` 都是服务启动时落盘的生成物
（在 .gitignore 里），但打包按目录遍历，gitignore 不生效——必须由 _tar_filter() 显式过滤。
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


def test_tar_filter_excludes_static_openapi_json():
    assert rp._tar_filter(tarfile.TarInfo("src/api/static/openapi.json")) is None


def test_tar_filter_excludes_help_openapi_json():
    assert rp._tar_filter(tarfile.TarInfo("help/api/openapi.json")) is None


def test_tar_filter_keeps_normal_files():
    for name in ("src/api/static/app.js", "src/api/main.py",
                 "src/kernel/help_page_gen.py", "help/api/usage.md"):
        ti = rp._tar_filter(tarfile.TarInfo(name))
        assert ti is not None and ti.name == name


def test_packed_tar_contains_no_openapi_json(tmp_path):
    src_dir = tmp_path / "tree"
    (src_dir / "src/api/static").mkdir(parents=True)
    (src_dir / "help/api").mkdir(parents=True)
    (src_dir / "src/api/static/openapi.json").write_text("{}", encoding="utf-8")
    (src_dir / "help/api/openapi.json").write_text("{}", encoding="utf-8")
    (src_dir / "src/api/static/app.js").write_text("// js", encoding="utf-8")
    (src_dir / "help/api/usage.md").write_text("# md", encoding="utf-8")

    out = tmp_path / "pkg.tar.gz"
    rp.make_subtar_gz(str(src_dir), str(out))
    with tarfile.open(out, "r:gz") as tar:
        names = tar.getnames()
    assert not [n for n in names if n.endswith("openapi.json")], \
        f"release package must not contain generated openapi.json, got {names}"
    assert "src/api/static/app.js" in names