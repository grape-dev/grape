"""Issue #996 删除链路（端点 + contents DELETE）：dry_run / 守卫命中跳过 / 逐条删除 / 失败上报。"""
import base64
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest
from fastapi import HTTPException

import src.api.git_repo_api as gra


def _b64(text):
    return base64.b64encode(text.encode()).decode()


def _make_transport(monkeypatch, *, guard_refs=None, delete_fail_on=None):
    calls = []

    def fake_req(provider, method, url, token, body=None):
        calls.append((method, url, body))
        if method == "GET" and "/contents/docs/issue-996" in url:
            return [
                {"type": "file", "path": "docs/issue-996/plan.md", "sha": "sha-plan",
                 "content": _b64("# plan")},
                {"type": "file", "path": "docs/issue-996/spec.md", "sha": "sha-spec",
                 "content": _b64("# spec")},
            ]
        if method == "GET" and "/contents/scripts" in url:
            if guard_refs is None:
                return []
            return [{"type": "file", "path": "scripts/check-docs.sh",
                     "content": _b64(guard_refs)}]
        if method == "GET" and "/contents/tests" in url:
            return []
        if method == "DELETE":
            if delete_fail_on and delete_fail_on in url:
                raise HTTPException(status_code=500, detail={"code": 500, "message": "boom"})
            return {"commit": {"sha": "c1"}}
        raise AssertionError(f"unexpected {method} {url}")

    monkeypatch.setattr(gra, "_vcs_req", fake_req)
    monkeypatch.setattr(gra, "_repo_token", lambda *a, **k: "tk_mock")
    return calls


def _call(body):
    return gra.clean_issue_artifacts_endpoint(
        "owner", "repo", 996, body,
        user={"auth_type": "session_token", "agent_type": "coding", "username": "bot"},
        p="gitcode")


def test_dry_run_reports_paths_without_deleting(monkeypatch):
    calls = _make_transport(monkeypatch)
    out = _call(gra.ArtifactCleanBody(dry_run=True, ref="main", message=""))
    assert out["paths"] == ["docs/issue-996/plan.md", "docs/issue-996/spec.md"]
    assert out["deleted"] == [] and out["failed"] == []
    assert not [c for c in calls if c[0] == "DELETE"], "dry_run 不得发 DELETE"
    assert out["dry_run"] is True


def test_guard_hit_skips_deletion_even_when_execute(monkeypatch):
    calls = _make_transport(monkeypatch, guard_refs="rm -rf docs/issue-996/*\n")
    out = _call(gra.ArtifactCleanBody(dry_run=False, ref="main", message=""))
    assert out["guard_impact"]["breaking"] is True
    assert out["skipped"] == out["paths"] and out["deleted"] == []
    assert not [c for c in calls if c[0] == "DELETE"], "守卫命中必须跳过删除，不可绕过"


def test_execute_deletes_each_path_with_sha(monkeypatch):
    calls = _make_transport(monkeypatch)
    out = _call(gra.ArtifactCleanBody(dry_run=False, ref="main", message="chore: cleanup"))
    deletes = [c for c in calls if c[0] == "DELETE"]
    assert len(deletes) == 2
    assert all("sha" in (c[2] or {}) for c in deletes)
    assert all((c[2] or {}).get("message") == "chore: cleanup" for c in deletes)
    assert out["deleted"] == ["docs/issue-996/plan.md", "docs/issue-996/spec.md"]
    assert out["failed"] == []


def test_partial_delete_failure_reported_not_swallowed(monkeypatch):
    calls = _make_transport(monkeypatch, delete_fail_on="spec.md")
    out = _call(gra.ArtifactCleanBody(dry_run=False, ref="main", message=""))
    assert out["deleted"] == ["docs/issue-996/plan.md"]
    assert len(out["failed"]) == 1 and out["failed"][0]["path"] == "docs/issue-996/spec.md"
    assert len([c for c in calls if c[0] == "DELETE"]) == 2, "单条失败不得中断整批"


def test_no_artifacts_returns_empty(monkeypatch):
    def fake_req(provider, method, url, token, body=None):
        if method == "GET" and "/contents/" in url:
            raise HTTPException(status_code=404, detail={"code": 404, "message": "Not Found"})
        return {}

    monkeypatch.setattr(gra, "_vcs_req", fake_req)
    monkeypatch.setattr(gra, "_repo_token", lambda *a, **k: "tk_mock")
    out = _call(gra.ArtifactCleanBody(dry_run=False, ref="main", message=""))
    assert out["paths"] == [] and out["deleted"] == [] and out["failed"] == []


def test_default_ref_resolution_used_when_body_ref_empty(monkeypatch):
    _make_transport(monkeypatch)
    monkeypatch.setattr(gra, "_default_ref", lambda p, o, r, u: "trunk")
    out = _call(gra.ArtifactCleanBody(dry_run=True, ref="", message=""))
    assert out["ref"] == "trunk"


def test_route_registered():
    paths = [r.path for r in gra.router.routes]
    assert "/api/git-repo/{owner}/{repo}/issues/{number}/artifacts/clean" in paths