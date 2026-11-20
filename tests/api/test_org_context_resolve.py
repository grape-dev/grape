"""组织上下文解析：X-Org-ID 指向已删除组织时的降级行为。

回归（2026-11-19）：删除当前组织后 localStorage 仍带旧 org id，后端对
「非成员」一律 403 → 整站请求 403、页面停在报错页。已删除组织属于客户端
残留引用，应回落个人组织；真·非成员的既有组织仍必须 403。
"""
import pytest
from fastapi import HTTPException

from src.api import org_context as oc


class _Req:
    def __init__(self, headers=None, query=None):
        self.headers = headers or {}
        self.query_params = query or {}


def test_deleted_org_falls_back_to_personal(monkeypatch):
    monkeypatch.setattr(oc, "get_org", lambda oid: None)
    monkeypatch.setattr(oc, "get_personal_org_id", lambda uid: "personal-1")
    monkeypatch.setattr(oc, "is_org_member", lambda uid, oid: True)

    assert oc.resolve_org_context(_Req({"X-Org-ID": "deleted-org"}), "1") == "personal-1"


def test_existing_org_without_membership_still_403(monkeypatch):
    monkeypatch.setattr(oc, "get_org", lambda oid: {"id": oid})
    monkeypatch.setattr(oc, "get_personal_org_id", lambda uid: "personal-1")
    monkeypatch.setattr(oc, "is_org_member", lambda uid, oid: False)

    with pytest.raises(HTTPException) as ei:
        oc.resolve_org_context(_Req({"X-Org-ID": "other-org"}), "1")
    assert ei.value.status_code == 403