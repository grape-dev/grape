"""Issue #2010：src/ 顶层按职责归类到子包 + 旧路径兼容回归。

验收点：
- src/ 顶层只剩 __init__.py 与子包目录，无散置 py；
- 19 个原顶层模块位于职责子包，旧路径经 src/__init__.py 别名解析到同一模块对象；
- worker 打包清单覆盖新子包、不再列旧顶层文件。
"""
import importlib
import os
import sys

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SRC = os.path.join(REPO_ROOT, "src")
sys.path.insert(0, REPO_ROOT)

SUBPACKAGES = ("api", "domains", "kernel", "repos", "authn", "help", "infra")

MOVED_MODULES = {
    "src.repos.repo": "repo.py",
    "src.repos.repo_cleanup": "repo_cleanup.py",
    "src.repos.repo_key": "repo_key.py",
    "src.repos.repo_migrate": "repo_migrate.py",
    "src.authn.token_resolver": "token_resolver.py",
    "src.authn.token_service": "token_service.py",
    "src.authn.session_manager": "session_manager.py",
    "src.help.help_docs_sync": "help_docs_sync.py",
    "src.help.help_page_gen": "help_page_gen.py",
    "src.infra.config": "config.py",
    "src.infra.localfs": "localfs.py",
    "src.infra.lock": "lock.py",
    "src.infra.mysql_compat": "mysql_compat.py",
    "src.infra.scene_custom_env": "scene_custom_env.py",
    "src.infra.worker_package": "worker_package.py",
    "src.infra.featured_order": "featured_order.py",
    "src.infra.outbound_ledger": "outbound_ledger.py",
    "src.infra.prompt_builder": "prompt_builder.py",
    "src.infra.service_diagnostics": "service_diagnostics.py",
}


def test_src_top_level_only_init_and_subpackages():
    entries = [e for e in os.listdir(SRC) if e != "__pycache__"]
    loose_py = [e for e in entries if e.endswith(".py")]
    assert loose_py == ["__init__.py"], f"loose top-level modules: {loose_py}"
    dirs = {e for e in entries if os.path.isdir(os.path.join(SRC, e))}
    for pkg in SUBPACKAGES:
        assert pkg in dirs, f"src/{pkg} missing"


def test_subpackages_are_packages():
    for pkg in SUBPACKAGES:
        assert os.path.isfile(os.path.join(SRC, pkg, "__init__.py")), f"src/{pkg}/__init__.py missing"


def test_moved_modules_live_in_responsible_subpackages():
    for module, filename in MOVED_MODULES.items():
        rel = module.split(".")
        path = os.path.join(SRC, *rel[1:]) + ".py"
        assert os.path.isfile(path), f"{module} missing at {path}"
        legacy_top = os.path.join(SRC, filename)
        assert not os.path.isfile(legacy_top), f"legacy top-level module still present: {legacy_top}"


def test_legacy_aliases_cover_all_moved_modules():
    from src import LEGACY_MODULE_ALIASES

    expected = {
        "src." + module.rsplit(".", 1)[1]: module for module in MOVED_MODULES
    }
    assert LEGACY_MODULE_ALIASES == expected


def test_legacy_import_resolves_to_canonical_module():
    import src

    for legacy, canonical in src.LEGACY_MODULE_ALIASES.items():
        legacy_module = importlib.import_module(legacy)
        canonical_module = importlib.import_module(canonical)
        assert legacy_module is canonical_module, f"{legacy} not aliased to {canonical}"
        assert sys.modules[legacy] is canonical_module


def test_worker_manifest_tracks_new_subpackages():
    from src.kernel.worker_package import MANIFEST

    for pkg in ("src/infra/", "src/repos/", "src/authn/", "src/help/"):
        assert pkg in MANIFEST, f"worker MANIFEST missing {pkg}"
    stale = [
        item for item in MANIFEST
        if item.startswith("src/") and item.endswith(".py")
        and os.path.basename(item) in set(MOVED_MODULES.values())
        and item.count("/") == 1
    ]
    assert stale == [], f"worker MANIFEST still lists legacy top-level files: {stale}"
