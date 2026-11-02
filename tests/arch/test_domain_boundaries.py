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
    from src.auth import audit as old_audit
    assert new_audit is old_audit
    from src.domains.runtime.engine import service as new_service
    from src.runtime import service as old_service
    assert new_service is old_service
    from src.domains.insight.observability import rollup as new_rollup
    from src.observability import rollup as old_rollup
    assert new_rollup is old_rollup


def test_legacy_route_shims():
    from src.api.routes.auth import router as old_router
    from src.domains.mgmt.routes.auth import router as new_router
    assert old_router is new_router
    from src.api.routes.health import router as old_health
    from src.domains.insight.routes.health import router as new_health
    assert old_health is new_health


def test_app_loads():
    from src.api.main import app
    assert app is not None
