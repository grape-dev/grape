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
    from src.config import log as old_log
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
    # 与基线 13c6d0979 同清单（含 src.user / src.vcs 顶层 shim 断言：head 树上
    # 这些目录确不存在、断言可通过——恢复守护面，防顶层 shim 回潮）。
    # src.domains.mgmt.user / src.domains.runtime.vcs 是 arch 布局的合法真实模块，
    # 不在"应不可导入"清单。
    for legacy in ("src.auth", "src.user", "src.admin", "src.runtime", "src.agent",
                   "src.im", "src.vcs", "src.storage", "src.observability",
                   "src.project_info", "src.api.routes"):
        with pytest.raises(ModuleNotFoundError):
            importlib.import_module(legacy)


def test_top_level_module_shims_still_work():
    """Thin module-level compat shims kept for external callers."""
    from src.config import log as old_log
    from src.kernel.config import log as new_log
    assert old_log is new_log
    from src.api.jwt_utils import create_access_token as old_create
    from src.kernel.auth_sdk.jwt_utils import create_access_token as new_create
    assert old_create is new_create


def test_app_loads():
    from src.api.main import app
    assert app is not None
