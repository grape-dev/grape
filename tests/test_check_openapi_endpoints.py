"""Issue #897：CI 契约端点完整性校验单测。

覆盖 scripts/check-openapi-endpoints.py 的核心判定：
- 核心工作流端点缺失必须报错（v2.2.0 子集覆盖回归场景）
- 版本间比对：基线核心工作流端点不得消失（v2.x vs v2.(x-1)）
- 基线为空 / 降级 / 非核心端点删除 → 不误报
"""
import importlib.util
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_SCRIPT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                       "scripts", "check-openapi-endpoints.py")
_spec = importlib.util.spec_from_file_location("check_openapi_endpoints", _SCRIPT)
coe = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(coe)

CORE_WORKFLOW_ENDPOINTS = coe.CORE_WORKFLOW_ENDPOINTS
check_baseline_subset = coe.check_baseline_subset
check_core_endpoints = coe.check_core_endpoints
core_workflow_paths = coe.core_workflow_paths
parse_version = coe.parse_version


def _full_paths():
    return {ep: {} for ep in CORE_WORKFLOW_ENDPOINTS}


def test_parse_version():
    assert parse_version("2.2.0") == (2, 2)
    assert parse_version("v2.1") == (2, 1)
    assert parse_version("not-a-version") is None
    assert parse_version(None) is None


def test_core_endpoints_all_present():
    assert check_core_endpoints(_full_paths()) == []


def test_core_endpoints_missing_reports_each():
    paths = _full_paths()
    del paths["/api/a2a"]
    del paths["/api/tasks/complete"]
    errors = check_core_endpoints(paths)
    assert len(errors) == 2
    assert "core workflow endpoint missing: /api/a2a" in errors
    assert "core workflow endpoint missing: /api/tasks/complete" in errors


def test_baseline_subset_ok_when_nothing_removed():
    errors = check_baseline_subset(
        _full_paths(), _full_paths(), "2.2.1", "2.2.0")
    assert errors == []


def test_baseline_subset_detects_removed_core_endpoint():
    baseline = _full_paths()
    current = _full_paths()
    del current["/api/tasks/notify"]
    errors = check_baseline_subset(current, baseline, "2.2.1", "2.2.0")
    assert any("/api/tasks/notify" in e for e in errors)


def test_baseline_subset_ignores_non_core_removal():
    baseline = _full_paths()
    baseline["/api/legacy/removed"] = {}
    current = _full_paths()
    errors = check_baseline_subset(current, baseline, "2.2.1", "2.2.0")
    assert errors == []


def test_baseline_subset_blocks_version_downgrade():
    errors = check_baseline_subset(_full_paths(), _full_paths(), "2.1.0", "2.2.0")
    assert any("baseline version newer" in e for e in errors)


def test_baseline_subset_skips_unparseable_versions():
    # 版本号不可解析时不做版本序比较，只做端点子集校验
    assert check_baseline_subset(_full_paths(), _full_paths(), "", "2.2.0") == []
    assert check_baseline_subset(_full_paths(), _full_paths(), "2.2.1", "") == []


def test_core_workflow_paths_filter():
    paths = {
        "/api/a2a": {},
        "/api/tasks/complete": {},
        "/api/runtime/tasks/x/status": {},
        "/api/agents": {},
    }
    assert core_workflow_paths(paths) == {
        "/api/a2a", "/api/tasks/complete", "/api/runtime/tasks/x/status"}
