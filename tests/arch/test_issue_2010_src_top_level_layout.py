"""Issue #2010：src/ 顶层布局收敛到 api|domains|kernel + 旧路径兼容回归。

验收点（#2010 收敛阶段，取代首轮的四子包布局）：
- src/ 顶层只剩 ``__init__.py`` 与 ``api/``、``domains/``、``kernel/`` 三个职责包；
- 首轮的中间子包 ``repos/`` / ``authn/`` / ``help/`` / ``infra/`` 已移除，
  也不可通过 import 复活；
- 19 个原顶层模块的 canonical 实现全部落在 ``src/kernel/``，旧路径经
  ``src/__init__.py`` 别名**一跳**解析到同一模块对象；
- worker 打包清单以 ``src/kernel/`` 目录条目覆盖全部历史路径；
- 仓库内不再残留 ``src.infra`` / ``src.repos`` / ``src.authn`` / ``src.help`` 引用。
"""
import importlib
import os
import re
import sys

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SRC = os.path.join(REPO_ROOT, "src")
sys.path.insert(0, REPO_ROOT)

TOP_LEVEL_PACKAGES = ("api", "domains", "kernel")
# 首轮引入、收敛阶段删除的中间子包（不再存在，也不得被 import）。
REMOVED_SUBPACKAGES = ("repos", "authn", "help", "infra")

CANONICAL_MODULES = (
    "config",
    "featured_order",
    "help_docs_sync",
    "help_page_gen",
    "id_gen",
    "localfs",
    "lock",
    "mysql_compat",
    "outbound_ledger",
    "prompt_builder",
    "repo",
    "repo_cleanup",
    "repo_key",
    "repo_migrate",
    "scene_custom_env",
    "service_diagnostics",
    "session_manager",
    "token_resolver",
    "token_service",
    "worker_package",
)


def test_src_top_level_only_init_and_three_packages():
    entries = [e for e in os.listdir(SRC) if e != "__pycache__"]
    loose_py = [e for e in entries if e.endswith(".py")]
    assert loose_py == ["__init__.py"], f"loose top-level modules: {loose_py}"
    dirs = {e for e in entries if os.path.isdir(os.path.join(SRC, e))}
    assert dirs == set(TOP_LEVEL_PACKAGES), f"unexpected src/ top-level packages: {sorted(dirs)}"
    for pkg in TOP_LEVEL_PACKAGES:
        assert os.path.isfile(os.path.join(SRC, pkg, "__init__.py")), f"src/{pkg}/__init__.py missing"


def test_intermediate_subpackages_removed():
    for pkg in REMOVED_SUBPACKAGES:
        assert not os.path.exists(os.path.join(SRC, pkg)), f"src/{pkg}/ should be removed"


def test_intermediate_subpackages_not_importable():
    for pkg in REMOVED_SUBPACKAGES:
        try:
            importlib.import_module(f"src.{pkg}")
        except ModuleNotFoundError:
            continue
        raise AssertionError(f"src.{pkg} should not be importable any more")


def test_canonical_modules_live_in_kernel():
    kernel = os.path.join(SRC, "kernel")
    for name in CANONICAL_MODULES:
        assert os.path.isfile(os.path.join(kernel, name + ".py")), f"src/kernel/{name}.py missing"


def test_legacy_aliases_point_straight_to_kernel():
    from src import LEGACY_MODULE_ALIASES

    expected = {f"src.{name}": f"src.kernel.{name}" for name in CANONICAL_MODULES}
    assert LEGACY_MODULE_ALIASES == expected


def test_legacy_import_resolves_to_canonical_module():
    import src

    for legacy, canonical in src.LEGACY_MODULE_ALIASES.items():
        legacy_module = importlib.import_module(legacy)
        canonical_module = importlib.import_module(canonical)
        assert legacy_module is canonical_module, f"{legacy} not aliased to {canonical}"
        assert sys.modules[legacy] is canonical_module


def test_worker_manifest_covers_kernel_and_drops_removed_dirs():
    import src.kernel.worker_package as wp

    manifest = list(wp.MANIFEST)
    assert "src/kernel/" in manifest, "worker MANIFEST must carry the canonical src/kernel/ dir"
    stale = [item for item in manifest
             if any(item.startswith(f"src/{pkg}/") for pkg in REMOVED_SUBPACKAGES)]
    assert stale == [], f"worker MANIFEST still lists removed subpackages: {stale}"
    loose = [item for item in manifest
             if item.startswith("src/") and item.endswith(".py") and item.count("/") == 1
             and os.path.basename(item)[:-3] in CANONICAL_MODULES]
    assert loose == [], f"worker MANIFEST still lists legacy top-level files: {loose}"


def test_no_residual_references_to_removed_subpackages():
    """仓库内不得再引用 src.infra / src.repos / src.authn / src.help（点号或路径形式）。"""
    pattern = re.compile(r"src\.(?:infra|repos|authn|help)\b|src/(?:infra|repos|authn)/|src/help/")
    scanned_roots = ("src", "scripts", "tests", ".grape", "apps")
    this_file = os.path.abspath(__file__)
    offenders = []
    for root in scanned_roots:
        base = os.path.join(REPO_ROOT, root)
        if not os.path.isdir(base):
            continue
        for dirpath, dirnames, filenames in os.walk(base):
            dirnames[:] = [d for d in dirnames if d not in ("__pycache__", "node_modules")]
            for filename in filenames:
                if not filename.endswith((".py", ".sh", ".toml", ".cfg", ".ini", ".json", ".md")):
                    continue
                path = os.path.join(dirpath, filename)
                if os.path.abspath(path) == this_file:
                    continue
                try:
                    with open(path, encoding="utf-8", errors="replace") as fh:
                        text = fh.read()
                except OSError:
                    continue
                for match in pattern.finditer(text):
                    offenders.append(f"{os.path.relpath(path, REPO_ROOT)}: {match.group(0)}")
    assert offenders == [], "residual references to removed subpackages: " + "; ".join(offenders)