"""契约根 x-changelog 变更通知（Issue #2784 S3）。

CLI `refresh --diff` 需要「版本 + 新增/删除/弃用端点」可读摘要，因此契约根
（`/openapi.json` 顶层）必须透出 `x-changelog`。变更依据是仓库内 checked-in
的现网发布快照 `help/api/openapi-baseline.json`：不做手工维护，避免漏记。
"""

import json

from fastapi.testclient import TestClient

from src.api import contract_versioning as cv
from src.api.main import app


def _schema():
    return TestClient(app).get("/openapi.json").json()


def _ops(methods):
    return {m: {"operationId": f"op_{m}".lower()} for m in methods}


# --------------------------------------------------------------------------
# S3-a 纯函数：由基线 diff 生成变更摘要
# --------------------------------------------------------------------------

def test_build_changelog_diffs_against_baseline():
    current = {
        "/api/a2a": _ops(("post",)),
        "/api/new": _ops(("get",)),
    }
    baseline = {
        "/api/a2a": _ops(("post",)),
        "/api/gone": _ops(("get",)),
    }
    changelog = cv.build_changelog(current, baseline, version="2.3.0a1")

    assert changelog["version"] == "2.3.0a1"
    assert changelog["added"] == ["/api/new"]
    assert changelog["removed"] == ["/api/gone"]
    assert changelog["deprecated"] == []
    assert changelog["baseline_available"] is True


def test_build_changelog_collects_deprecated_operations():
    current = {
        "/api/old": {"get": {"operationId": "old_get", "deprecated": True}},
        "/api/keep": {"get": {"operationId": "keep_get"}},
    }
    changelog = cv.build_changelog(current, {}, version="2.3.0a1")
    assert changelog["deprecated"] == ["/api/old"]


def test_build_changelog_degrades_without_baseline():
    """基线缺失（如部署副本不含 help/）时降级：不抛错，且不把全量端点当新增。"""
    changelog = cv.build_changelog({"/api/a2a": _ops(("post",))}, None, version="2.3.0a1")
    assert changelog["baseline_available"] is False
    assert changelog["added"] == []
    assert changelog["removed"] == []


def test_load_contract_baseline_reads_paths_and_metadata(tmp_path):
    path = tmp_path / "baseline.json"
    path.write_text(json.dumps({
        "info": {"version": "2.2.0"},
        "paths": {"/api/a2a": _ops(("post",))},
        "x-changelog": {"change_reason": "weekly 合入", "source_commit": "abc1234",
                        "updated_at": "2026-11-16T00:00:00+00:00"},
    }), encoding="utf-8")

    baseline = cv.load_contract_baseline(str(path))
    assert list(baseline["paths"]) == ["/api/a2a"]
    assert baseline["meta"]["change_reason"] == "weekly 合入"
    assert baseline["meta"]["source_commit"] == "abc1234"


def test_load_contract_baseline_missing_file_is_fail_soft(tmp_path):
    baseline = cv.load_contract_baseline(str(tmp_path / "absent.json"))
    assert baseline["paths"] == {}
    assert baseline["meta"] == {}


# --------------------------------------------------------------------------
# S3-b 对外契约：根级 x-changelog 可被 CLI 直接读到
# --------------------------------------------------------------------------

def test_contract_root_exposes_x_changelog():
    schema = _schema()
    changelog = schema.get("x-changelog")
    assert isinstance(changelog, dict), "契约根必须透出 x-changelog 供 CLI refresh --diff 消费"

    assert changelog["version"] == schema["info"]["version"]
    for key in ("added", "removed", "deprecated"):
        assert isinstance(changelog[key], list), f"x-changelog.{key} 必须是列表"


def test_contract_root_changelog_is_consistent_with_paths():
    schema = _schema()
    changelog = schema["x-changelog"]
    paths = schema["paths"]

    assert all(p in paths for p in changelog["added"]), "新增清单里出现了契约中不存在的端点"
    assert all(p not in paths for p in changelog["removed"]), "删除清单里出现了仍在契约中的端点"
