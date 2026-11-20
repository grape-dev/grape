# -*- coding: utf-8 -*-
"""Guard: org_id sentinel '0' must not turn a plain scene into an org scene.

`devops_pipeline_scene.org_id` stores the legacy SQLite boolean sentinel '0' for
"no organization".  `bool('0')` is True in Python, so treating the raw column as
truthy makes every plain scene look org-scoped; combined with a non-org repo the
entry check then rejects the request with "org requirement must use both org repo
and org scene" and issue creation fails for every concrete scene.

D105-15 already established the convention (`issues_center.py`: `"" if str(org_id)
== "0" else str(org_id)`); this module pins the entry check to the same rule.
"""
import pytest
from fastapi import HTTPException

from src.api import org_context

RK = "gitcode:acme/hello-calculator"
ORG_ID = "42"


def _patch_lookups(monkeypatch, binding, scene_meta, org_row=None):
    from src.domains.mgmt.user import org_db

    monkeypatch.setattr(org_db, "get_managed_repo_binding", lambda rk: binding)
    monkeypatch.setattr(org_db, "get_org", lambda oid: org_row or {})
    monkeypatch.setattr(org_context, "_org_scene_metadata", lambda sid, rk: scene_meta)


def _plain_repo():
    return {"repo_key": RK, "org_id": "", "is_org_repo": 0}


def test_zero_sentinel_scene_is_not_org_scene(monkeypatch):
    """org_id='0' (legacy sentinel) + plain repo must be allowed."""
    _patch_lookups(
        monkeypatch,
        _plain_repo(),
        {"scene_type": "standard", "org_id": "0"},
    )
    assert org_context.validate_org_scene_entry(RK, "default", ORG_ID) == {"is_org": False}


def test_zero_float_sentinel_scene_is_not_org_scene(monkeypatch):
    """Numeric-flavoured sentinel '0.0' behaves the same as '0'."""
    _patch_lookups(
        monkeypatch,
        _plain_repo(),
        {"scene_type": "standard", "org_id": "0.0"},
    )
    assert org_context.validate_org_scene_entry(RK, "default", ORG_ID) == {"is_org": False}


def test_empty_scene_id_still_short_circuits(monkeypatch):
    """Smart recommendation (no scene_id) keeps the historical bypass."""
    _patch_lookups(monkeypatch, _plain_repo(), {"scene_type": "", "org_id": ""})
    assert org_context.validate_org_scene_entry(RK, "", ORG_ID) == {"is_org": False}


def test_real_org_scene_still_rejected_on_plain_repo(monkeypatch):
    """A scene that really belongs to an org must still be refused (no relaxation)."""
    _patch_lookups(
        monkeypatch,
        _plain_repo(),
        {"scene_type": "org", "org_id": "7"},
    )
    with pytest.raises(HTTPException) as exc:
        org_context.validate_org_scene_entry(RK, "default", ORG_ID)
    assert exc.value.status_code == 400
    assert exc.value.detail["error_code"] == "INVALID_ORG_CONTEXT"
    assert exc.value.detail["message"] == "组织需求必须同时使用组织仓库与组织场景"


def test_org_scene_with_org_repo_passes(monkeypatch):
    """org repo + org scene of the same org still passes the entry check."""
    binding = {"repo_key": RK, "org_id": ORG_ID, "is_org_repo": 1}
    _patch_lookups(
        monkeypatch,
        binding,
        {"scene_type": "org", "org_id": ORG_ID},
        org_row={"org_id": ORG_ID, "org_repo_key": RK, "org_scene_id": "default"},
    )
    assert org_context.validate_org_scene_entry(RK, "default", ORG_ID) == {
        "is_org": True,
        "org_id": ORG_ID,
        "repo_key": RK,
        "scene_id": "default",
    }