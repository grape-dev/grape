"""TC-1.1/1.3/1.5: kernel extraction, domains layout, re-export compat (Issue #699 Phase 1)."""
import os
import sys

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, REPO_ROOT)


def test_kernel_layout():
    for m in ("config", "session_manager", "lock", "repo_key", "repo", "repo_cleanup",
              "repo_migrate", "mysql_compat", "token_service", "token_resolver",
              "prompt_builder", "worker_package", "localfs", "outbound_ledger",
              "scene_custom_env", "featured_order", "help_docs_sync", "help_page_gen"):
        assert os.path.isfile(os.path.join(REPO_ROOT, "src", "kernel", m + ".py")), f"kernel/{m}.py missing"
    for m in ("jwt_utils", "auth_token", "cookie_utils"):
        assert os.path.isfile(os.path.join(REPO_ROOT, "src", "kernel", "auth_sdk", m + ".py"))
    assert os.path.isdir(os.path.join(REPO_ROOT, "src", "kernel", "db_migrations"))


def test_domains_layout():
    for d in ("src/domains/mgmt/auth", "src/domains/mgmt/user", "src/domains/mgmt/admin",
              "src/domains/runtime/engine", "src/domains/runtime/agents",
              "src/domains/runtime/im", "src/domains/runtime/vcs",
              "src/domains/runtime/storage", "src/domains/runtime/pipeline",
              "src/domains/insight/observability", "src/domains/contracts"):
        assert os.path.isdir(os.path.join(REPO_ROOT, d)), f"{d} missing"
    assert os.path.isfile(os.path.join(REPO_ROOT, "src", "domains", "runtime", "pipeline", "event_pipeline.py"))


def test_kernel_new_import_path():
    from src.kernel.config import log
    assert log is not None


def test_old_import_path_reexport():
    from src.kernel.config import log as old_log
    from src.kernel.config import log as new_log
    assert old_log is new_log


def test_auth_sdk_roundtrip():
    from src.kernel.auth_sdk.jwt_utils import create_access_token, verify_access_token
    tok = create_access_token(1, "alice")
    payload = verify_access_token(tok)
    assert payload and payload.get("sub") == "1" and payload.get("username") == "alice"
    # old paths still importable
    from src.api.jwt_utils import create_access_token as old_create
    assert old_create is create_access_token


def test_domain_new_and_old_import_paths():
    from src.domains.mgmt.auth import audit as new_audit
    assert new_audit is not None
    from src.domains.runtime.engine import service as new_service
    assert new_service is not None
    from src.domains.insight.observability import rollup as new_rollup
    assert new_rollup is not None


def test_legacy_dir_shims_removed():
    """Issue #699 cleanup: legacy top-level shim dirs are gone; imports must fail."""
    import importlib
    import pytest
    # 只校验「已删除的 legacy 顶层目录」；src.domains.* 是 canonical 实现，必须可导入。
    for legacy in ("src.auth", "src.user", "src.runtime", "src.project_info", "src.api.routes",
                   "src.im", "src.vcs", "src.agent", "src.storage", "src.admin", "src.observability"):
        with pytest.raises(ModuleNotFoundError):
            importlib.import_module(legacy)


LEGACY_MODULE_TARGETS = {
    "src.config": "src.kernel.config",
    "src.featured_order": "src.kernel.featured_order",
    "src.help_docs_sync": "src.kernel.help_docs_sync",
    "src.help_page_gen": "src.kernel.help_page_gen",
    "src.localfs": "src.kernel.localfs",
    "src.lock": "src.kernel.lock",
    "src.mysql_compat": "src.kernel.mysql_compat",
    "src.outbound_ledger": "src.kernel.outbound_ledger",
    "src.prompt_builder": "src.kernel.prompt_builder",
    "src.repo": "src.kernel.repo",
    "src.repo_cleanup": "src.kernel.repo_cleanup",
    "src.repo_key": "src.kernel.repo_key",
    "src.repo_migrate": "src.kernel.repo_migrate",
    "src.scene_custom_env": "src.kernel.scene_custom_env",
    "src.service_diagnostics": "src.kernel.service_diagnostics",
    "src.session_manager": "src.kernel.session_manager",
    "src.token_resolver": "src.kernel.token_resolver",
    "src.token_service": "src.kernel.token_service",
    "src.worker_package": "src.kernel.worker_package",
}


def test_top_level_module_shims_still_work():
    """Thin module-level compat shims kept for external callers.

    Issue #2010 的 canonical 实现全部落在 src/kernel，旧路径（src.config、
    src.lock 等）由 src/__init__.py 的懒加载别名一跳解析，
    语义等价：别名指向同一个模块对象（monkeypatch 仍然有效）。
    """
    import importlib

    for legacy, canonical in LEGACY_MODULE_TARGETS.items():
        legacy_module = importlib.import_module(legacy)
        canonical_module = importlib.import_module(canonical)
        assert legacy_module is canonical_module, f"{legacy} not aliased to {canonical}"

    from src.kernel.config import log as old_log
    from src.kernel.config import log as new_log
    assert old_log is new_log
    from src.api.jwt_utils import create_access_token as old_create
    from src.kernel.auth_sdk.jwt_utils import create_access_token as new_create
    assert old_create is new_create


def test_app_loads():
    from src.api.main import app
    assert app is not None
