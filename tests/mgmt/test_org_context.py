"""Issue #2150: organization context + organization knowledge tests.

覆盖：发布自动降级（非 409）、kind/tag/q/status 过滤、权限 403 口径、
CONTENT_TOO_LARGE、明显凭据拒绝、审计落库、org 隔离；PR #1278 审查补充：
INVALID_CONTEXT_KIND 错误码、deprecated/archived 终态、发布串行化、旧知识
API 隔离 org scope、name 限长、展示字段敏感扫描、删除清理 OBS。
"""
from __future__ import annotations

import os
import sys
import threading

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from fastapi.testclient import TestClient

from src.api.deps import get_current_user_or_api_key
from src.api.main import app
from src.kernel.session_manager import db, db_lock

OWNER_UID = 22150
ADMIN_UID = 22151
MEMBER_UID = 22152
OUTSIDER_UID = 22153

SLUG = "issue2150-team"
SLUG_B = "issue2150-team-b"
ORG_REPO_KEY = "gitcode:issue2150/org-repo"

ALL_UIDS = (OWNER_UID, ADMIN_UID, MEMBER_UID, OUTSIDER_UID)


@pytest.fixture(scope="session", autouse=True)
def _ensure_tables():
    from src.api import db_setup

    db_setup.init_database()


def _cleanup():
    with db_lock:
        c = db.cursor()
        org_ids = [
            r[0]
            for r in c.execute(
                "SELECT id FROM organization WHERE owner_id IN (?,?,?,?)", ALL_UIDS
            ).fetchall()
        ]
        for org_id in org_ids:
            c.execute("DELETE FROM org_member WHERE org_id=?", (org_id,))
            c.execute("DELETE FROM grape_t_org_context WHERE org_id=?", (org_id,))
            c.execute("DELETE FROM agent_shared_knowledge WHERE org_id=?", (org_id,))
        c.execute("DELETE FROM organization WHERE owner_id IN (?,?,?,?)", ALL_UIDS)
        c.execute("DELETE FROM managed_repos WHERE repo_key=?", (ORG_REPO_KEY,))
        c.execute("DELETE FROM audit_logs WHERE event_type LIKE 'org_context\\_%' ESCAPE '\\'")
        c.execute("DELETE FROM audit_logs WHERE event_type LIKE 'org_knowledge\\_%' ESCAPE '\\'")
        db.commit()


@pytest.fixture(autouse=True)
def _clean():
    _cleanup()
    yield
    _cleanup()


@pytest.fixture
def client():
    state = {"uid": OWNER_UID}

    def _fake_user():
        return {"id": state["uid"], "username": f"u{state['uid']}", "auth_type": "jwt"}

    app.dependency_overrides[get_current_user_or_api_key] = _fake_user
    yield TestClient(app), state
    app.dependency_overrides.clear()


def _seed_org(owner: int = OWNER_UID, slug: str = SLUG) -> int:
    from src.domains.mgmt.user.org_db import (
        add_org_member,
        create_org,
        update_org_provision_fields,
    )

    org = create_org(slug=slug, name=slug, owner_id=owner)
    assert org
    update_org_provision_fields(org["id"], org_repo_key=ORG_REPO_KEY)
    if owner == OWNER_UID:
        add_org_member(org["id"], ADMIN_UID, "admin")
        add_org_member(org["id"], MEMBER_UID, "member")
    return org["id"]


def _create_context(api, org_id, *, kind="brief", key="", title="t", content="c"):
    return api.post(
        f"/api/orgs/{org_id}/context",
        json={"kind": kind, "key": key, "title": title, "content": content},
    )


def _publish_context(api, org_id, cid):
    return api.post(f"/api/orgs/{org_id}/context/{cid}/publish")


def _create_knowledge(api, org_id, *, kind="runbook", name="k", description="d",
                      content="c", tags=None):
    return api.post(
        f"/api/orgs/{org_id}/knowledge",
        json={"kind": kind, "name": name, "description": description,
              "content": content, "tags": tags or []},
    )


# ---- org_context ----

def test_context_publish_auto_deprecates_previous_active(client):
    api, _st = client
    org_id = _seed_org()

    r1 = _create_context(api, org_id, title="v1", content="brief v1")
    assert r1.status_code == 200, r1.text
    c1 = r1.json()
    assert c1["lifecycle_status"] == "draft"

    p1 = _publish_context(api, org_id, c1["id"])
    assert p1.status_code == 200, p1.text
    assert p1.json()["lifecycle_status"] == "active"

    r2 = _create_context(api, org_id, title="v2", content="brief v2")
    c2 = r2.json()
    p2 = _publish_context(api, org_id, c2["id"])
    assert p2.status_code == 200, p2.text

    old = api.get(f"/api/orgs/{org_id}/context/{c1['id']}").json()
    assert old["lifecycle_status"] == "deprecated"

    active = api.get(
        f"/api/orgs/{org_id}/context", params={"kind": "brief", "status": "active"}
    ).json()["contexts"]
    assert len(active) == 1
    assert active[0]["id"] == c2["id"]


def test_context_multiple_policies_can_be_active(client):
    api, _st = client
    org_id = _seed_org()
    ids = []
    for key in ("policy-a", "policy-b"):
        r = _create_context(api, org_id, kind="policy", key=key, title=key)
        assert r.status_code == 200, r.text
        cid = r.json()["id"]
        assert _publish_context(api, org_id, cid).status_code == 200
        ids.append(cid)

    active = api.get(
        f"/api/orgs/{org_id}/context", params={"kind": "policy", "status": "active"}
    ).json()["contexts"]
    assert sorted(x["id"] for x in active) == sorted(ids)


# ---- org_knowledge ----

def test_knowledge_filters(client):
    api, _st = client
    org_id = _seed_org()
    k1 = _create_knowledge(api, org_id, kind="runbook", name="Deploy runbook",
                           description="how to deploy", tags=["deploy", "ops"]).json()
    k2 = _create_knowledge(api, org_id, kind="faq", name="Billing FAQ",
                           description="billing questions", tags=["billing"]).json()
    assert api.post(f"/api/orgs/{org_id}/knowledge/{k1['id']}/publish").status_code == 200
    assert api.post(f"/api/orgs/{org_id}/knowledge/{k2['id']}/publish").status_code == 200

    by_kind = api.get(f"/api/orgs/{org_id}/knowledge", params={"kind": "faq"}).json()["knowledge"]
    assert [x["name"] for x in by_kind] == ["Billing FAQ"]

    by_tag = api.get(f"/api/orgs/{org_id}/knowledge", params={"tag": "ops"}).json()["knowledge"]
    assert [x["name"] for x in by_tag] == ["Deploy runbook"]

    by_q = api.get(f"/api/orgs/{org_id}/knowledge", params={"q": "deploy"}).json()["knowledge"]
    assert [x["name"] for x in by_q] == ["Deploy runbook"]

    active = api.get(f"/api/orgs/{org_id}/knowledge", params={"status": "active"}).json()["knowledge"]
    assert len(active) == 2

    draft = api.get(f"/api/orgs/{org_id}/knowledge", params={"status": "draft"}).json()["knowledge"]
    assert draft == []


def test_knowledge_org_isolation(client):
    api, st = client
    org_a = _seed_org()
    ks = _create_knowledge(api, org_a, name="A only").json()
    assert api.post(f"/api/orgs/{org_a}/knowledge/{ks['id']}/publish").status_code == 200

    org_b = _seed_org(owner=ADMIN_UID, slug=SLUG_B)
    st["uid"] = ADMIN_UID
    listing = api.get(f"/api/orgs/{org_b}/knowledge").json()["knowledge"]
    assert listing == []


def test_knowledge_publish_auto_deprecates_same_name(client):
    api, _st = client
    org_id = _seed_org()
    k1 = _create_knowledge(api, org_id, name="Runbook").json()
    assert api.post(f"/api/orgs/{org_id}/knowledge/{k1['id']}/publish").status_code == 200
    k2 = _create_knowledge(api, org_id, name="Runbook").json()
    assert api.post(f"/api/orgs/{org_id}/knowledge/{k2['id']}/publish").status_code == 200

    assert k2["version"] == k1["version"] + 1
    assert api.get(f"/api/orgs/{org_id}/knowledge/{k1['id']}").json()["lifecycle_status"] == "deprecated"
    active = api.get(f"/api/orgs/{org_id}/knowledge", params={"status": "active"}).json()["knowledge"]
    assert [x["id"] for x in active] == [k2["id"]]


# ---- permissions ----

def test_permissions(client):
    api, st = client
    org_id = _seed_org()
    cid = _create_context(api, org_id, title="owner brief").json()["id"]

    # member: read ok, write draft 403, publish 403
    st["uid"] = MEMBER_UID
    assert api.get(f"/api/orgs/{org_id}/context").status_code == 200
    assert _create_context(api, org_id).status_code == 403
    assert _publish_context(api, org_id, cid).status_code == 403

    # admin: write draft ok
    st["uid"] = ADMIN_UID
    assert _create_context(api, org_id, title="admin brief").status_code == 200

    # non-member: read 403
    st["uid"] = OUTSIDER_UID
    r = api.get(f"/api/orgs/{org_id}/context")
    assert r.status_code == 403
    assert r.json()["detail"]["error_code"] == "PERMISSION_DENIED"


def test_repo_admin_can_write_draft(client, monkeypatch):
    api, st = client
    import importlib

    # 逐用例 src.* 模块隔离下，须从 sys.modules 的实时对象 monkeypatch
    repo_roles = importlib.import_module("src.domains.mgmt.auth.repo_roles")
    org_id = _seed_org()
    monkeypatch.setattr(
        repo_roles, "is_repo_admin",
        lambda user, repo_key: user.get("id") == MEMBER_UID,
    )
    st["uid"] = MEMBER_UID
    r = _create_context(api, org_id, title="repo admin draft")
    assert r.status_code == 200, r.text
    # 仓库 admin 只可写 draft，发布仍需 org owner/admin
    assert _publish_context(api, org_id, r.json()["id"]).status_code == 403


# ---- safety ----

def test_content_too_large(client, monkeypatch):
    api, _st = client
    from src.domains.mgmt.routes import knowledge as kr

    monkeypatch.setattr(kr, "MAX_CONTENT_BYTES", 1024)
    org_id = _seed_org()
    r = _create_context(api, org_id, title="big", content="x" * 4096)
    assert r.status_code == 400
    assert r.json()["detail"]["error_code"] == "CONTENT_TOO_LARGE"


def test_knowledge_content_too_large(client, monkeypatch):
    api, _st = client
    from src.domains.mgmt.routes import knowledge as kr

    monkeypatch.setattr(kr, "MAX_CONTENT_BYTES", 1024)
    org_id = _seed_org()
    r = _create_knowledge(api, org_id, name="big", content="y" * 4096)
    assert r.status_code == 400
    assert r.json()["detail"]["error_code"] == "CONTENT_TOO_LARGE"


def test_publish_rejects_obvious_credentials(client):
    api, _st = client
    org_id = _seed_org()
    secret = "ghp_" + "A" * 36
    cid = _create_context(api, org_id, content=f"deploy token is {secret}").json()["id"]
    r = _publish_context(api, org_id, cid)
    assert r.status_code == 400
    assert r.json()["detail"]["error_code"] == "SENSITIVE_CONTENT_REJECTED"
    assert api.get(f"/api/orgs/{org_id}/context/{cid}").json()["lifecycle_status"] == "draft"


# ---- audit ----

def test_audit_events_persisted(client):
    api, _st = client
    org_id = _seed_org()
    cid = _create_context(api, org_id).json()["id"]
    assert _publish_context(api, org_id, cid).status_code == 200
    assert api.post(f"/api/orgs/{org_id}/context/{cid}/deprecate").status_code == 200

    kid = _create_knowledge(api, org_id, name="audit k").json()["id"]
    assert api.post(f"/api/orgs/{org_id}/knowledge/{kid}/publish").status_code == 200
    assert api.post(f"/api/orgs/{org_id}/knowledge/{kid}/deprecate").status_code == 200

    with db_lock:
        rows = [
            r[0]
            for r in db.cursor().execute(
                "SELECT DISTINCT event_type FROM audit_logs WHERE detail LIKE ?",
                (f'%"org_id": "{org_id}"%',),
            ).fetchall()
        ]
    assert {"org_context_create", "org_context_publish", "org_context_deprecate",
            "org_knowledge_create", "org_knowledge_publish",
            "org_knowledge_deprecate"} <= set(rows)


# ---- PR #1278 审查修复回归 ----

def test_context_invalid_kind_error_code(client):
    """契约错误码：非法 kind 必须返回 400 INVALID_CONTEXT_KIND（非 INVALID_KIND）。"""
    api, _st = client
    org_id = _seed_org()
    r = _create_context(api, org_id, kind="memo", title="bad kind")
    assert r.status_code == 400, r.text
    assert r.json()["detail"]["error_code"] == "INVALID_CONTEXT_KIND"


def test_context_deprecated_cannot_be_republished(client):
    """已废弃上下文不可再发布（否则可绕过 409 复活并顶掉当前 active）。"""
    api, _st = client
    org_id = _seed_org()
    cid = _create_context(api, org_id, title="v1").json()["id"]
    assert _publish_context(api, org_id, cid).status_code == 200
    assert api.post(f"/api/orgs/{org_id}/context/{cid}/deprecate").status_code == 200

    r = _publish_context(api, org_id, cid)
    assert r.status_code == 409, r.text
    assert r.json()["detail"]["error_code"] == "INVALID_STATUS_TRANSITION"
    assert api.get(f"/api/orgs/{org_id}/context/{cid}").json()["lifecycle_status"] == "deprecated"


def test_knowledge_deprecated_cannot_be_republished(client):
    api, _st = client
    org_id = _seed_org()
    kid = _create_knowledge(api, org_id, name="retired").json()["id"]
    assert api.post(f"/api/orgs/{org_id}/knowledge/{kid}/publish").status_code == 200
    assert api.post(f"/api/orgs/{org_id}/knowledge/{kid}/deprecate").status_code == 200

    r = api.post(f"/api/orgs/{org_id}/knowledge/{kid}/publish")
    assert r.status_code == 409, r.text
    assert r.json()["detail"]["error_code"] == "INVALID_STATUS_TRANSITION"
    assert api.get(f"/api/orgs/{org_id}/knowledge/{kid}").json()["lifecycle_status"] == "deprecated"


def test_knowledge_archive_is_terminal(client):
    """Issue 契约含 archived：可归档，归档后不可再发布/更新/废弃，可按 status 过滤。"""
    api, _st = client
    org_id = _seed_org()
    kid = _create_knowledge(api, org_id, name="archive me").json()["id"]
    assert api.post(f"/api/orgs/{org_id}/knowledge/{kid}/publish").status_code == 200

    r = api.post(f"/api/orgs/{org_id}/knowledge/{kid}/archive")
    assert r.status_code == 200, r.text
    assert r.json()["lifecycle_status"] == "archived"

    archived = api.get(
        f"/api/orgs/{org_id}/knowledge", params={"status": "archived"}
    ).json()["knowledge"]
    assert [x["id"] for x in archived] == [kid]

    assert api.post(f"/api/orgs/{org_id}/knowledge/{kid}/publish").status_code == 409
    assert api.post(f"/api/orgs/{org_id}/knowledge/{kid}/deprecate").status_code == 409
    assert api.put(f"/api/orgs/{org_id}/knowledge/{kid}", json={"description": "x"}).status_code == 409


def test_legacy_knowledge_api_hides_org_entries(client):
    """组织知识复用旧表后，旧 /api/knowledge 端点必须隔离（防跨组织读与生命周期绕过）。"""
    from src.domains.mgmt.user.db import mark_user_repo_join

    api, _st = client
    org_id = _seed_org()
    mark_user_repo_join(OWNER_UID, ORG_REPO_KEY, is_join=True, is_admin=True)
    kid = _create_knowledge(api, org_id, name="org only", content="org secret").json()["id"]
    assert api.post(f"/api/orgs/{org_id}/knowledge/{kid}/publish").status_code == 200

    # 旧详情/更新/删除按 id 一概不可见（404，不泄露存在性）
    assert api.get(f"/api/knowledge/{kid}").status_code == 404
    assert api.put(f"/api/knowledge/{kid}", json={"content": "hijack"}).status_code == 404
    assert api.delete(f"/api/knowledge/{kid}").status_code == 404
    # 旧列表同样不返回 org 行
    listing = api.get("/api/knowledge", params={"repo": ORG_REPO_KEY}).json()
    assert all(x["id"] != kid for x in listing["knowledge"])


def test_knowledge_name_length_limited(client):
    """name 列 VARCHAR(256)/旧接口口径：超长必须 400，不能落到 DB 1406 → 500。"""
    api, _st = client
    org_id = _seed_org()
    r = _create_knowledge(api, org_id, name="n" * 60)
    assert r.status_code == 400, r.text
    assert r.json()["detail"]["error_code"] == "INVALID_NAME"


def test_knowledge_publish_scans_display_fields(client):
    """发布扫描覆盖 name/description，避免凭据藏在展示字段里绕过。"""
    api, _st = client
    org_id = _seed_org()
    kid = _create_knowledge(
        api, org_id, name="ops note",
        description="db password=SuperSecret12345", content="clean body",
    ).json()["id"]
    r = api.post(f"/api/orgs/{org_id}/knowledge/{kid}/publish")
    assert r.status_code == 400, r.text
    assert r.json()["detail"]["error_code"] == "SENSITIVE_CONTENT_REJECTED"


def test_knowledge_delete_cleans_obs_object(client, monkeypatch):
    """删除组织知识须同步清理 OBS 对象（对齐旧删除逻辑）。"""
    import importlib

    api, _st = client
    storage_mod = importlib.import_module("src.domains.runtime.storage")

    class _FakeStorage:
        def __init__(self):
            self.saved = []
            self.deleted = []

        def save_bytes(self, key, data):
            self.saved.append(key)

        def delete_object(self, key):
            self.deleted.append(key)

    fake = _FakeStorage()
    monkeypatch.setattr(storage_mod, "get_storage", lambda: fake)

    org_id = _seed_org()
    kid = _create_knowledge(api, org_id, name="obs item").json()["id"]
    assert fake.saved, "创建时应写入 OBS 对象（fake 记录）"
    assert api.delete(f"/api/orgs/{org_id}/knowledge/{kid}").status_code == 200
    assert fake.deleted == fake.saved[:1]


def test_concurrent_context_publish_keeps_single_active(client, monkeypatch):
    """多线程并发发布同一 (org, kind, key)：必须经过串行化守卫，最终仅 1 条 active。"""
    import importlib

    api, _st = client
    org_id = _seed_org()
    store = importlib.import_module("src.domains.mgmt.org_context_store")
    calls = []
    real_guard = store.publish_guard

    def _record(table, org, group):
        calls.append((table, org, group))
        return real_guard(table, org, group)

    monkeypatch.setattr(store, "publish_guard", _record)

    ids = [_create_context(api, org_id, title=f"v{i}").json()["id"] for i in range(3)]
    errors = []

    def _publish(cid):
        try:
            store.publish_context(org_id, cid)
        except Exception as e:  # pragma: no cover - 断言 failures 时输出
            errors.append(e)

    threads = [threading.Thread(target=_publish, args=(cid,)) for cid in ids]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors, errors
    assert len(calls) == len(ids)
    assert calls[0][0] == "org_context"
    active = store.list_contexts(org_id, kind="brief", status="active")
    assert len(active) == 1


def test_concurrent_knowledge_publish_keeps_single_active(client, monkeypatch):
    """多线程并发发布同 (org, kind, name) 知识：串行化后仅 1 条 active。"""
    import importlib

    api, _st = client
    org_id = _seed_org()
    kn = importlib.import_module("src.domains.mgmt.org_knowledge")
    calls = []
    real_guard = kn.publish_guard

    def _record(table, org, group):
        calls.append((table, org, group))
        return real_guard(table, org, group)

    monkeypatch.setattr(kn, "publish_guard", _record)

    ids = [_create_knowledge(api, org_id, name="race", content=f"v{i}").json()["id"]
           for i in range(3)]
    errors = []

    def _publish(kid):
        try:
            kn.publish_knowledge(org_id, kid)
        except Exception as e:  # pragma: no cover
            errors.append(e)

    threads = [threading.Thread(target=_publish, args=(kid,)) for kid in ids]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors, errors
    assert len(calls) == len(ids)
    assert calls[0][0] == "agent_shared_knowledge"
    active = [x for x in kn.list_knowledge(org_id, status="active") if x["name"] == "race"]
    assert len(active) == 1


def test_list_context_and_knowledge_are_bounded(client):
    """历史批次会持续增长；列表 API 必须有硬上限，禁止一次拉全量。"""
    api, _st = client
    org_id = _seed_org()
    for i in range(3):
        assert _create_context(
            api, org_id, kind="decision", key=f"k{i}", title=f"d{i}").status_code == 200
    for i in range(3):
        assert _create_knowledge(
            api, org_id, kind="runbook", name=f"k{i}").status_code == 200

    contexts = api.get(
        f"/api/orgs/{org_id}/context",
        params={"kind": "decision", "limit": 2, "offset": 1},
    ).json()
    assert len(contexts["contexts"]) == 2
    assert contexts["limit"] == 2 and contexts["offset"] == 1

    knowledge = api.get(
        f"/api/orgs/{org_id}/knowledge",
        params={"kind": "runbook", "limit": 2, "offset": 1},
    ).json()
    assert len(knowledge["knowledge"]) == 2
    assert knowledge["limit"] == 2 and knowledge["offset"] == 1
