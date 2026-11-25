"""编辑组织回归（描述清空 / 无变更保存 / 上限字段 / 不存在组织）。

覆盖的缺陷：
- ``description=""`` 被当成“不修改”，导致无法清空描述；
- 组织存在但本次无字段变化时，MySQL ``rowcount=0`` 被误判为 404 Organization not found；
- 编辑弹窗需要同时更新成员上限与仓库上限，Worker 上限暂不开放。

约束：``update_org`` 的 ``None`` = 不修改，``description=""`` = 清空；
上限字段 ``None``/``0`` 均保持历史语义“不修改”，避免旧客户端把额度误改成 0。
"""
from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from fastapi.testclient import TestClient

from src.api.deps import get_current_user_or_api_key
from src.api.main import app
from src.domains.mgmt.user.org_db import create_org, get_org, update_org
from src.kernel.session_manager import db, db_lock

UID = 22710
USERNAME = "issue-edit-org-owner"
SLUG = "issue-edit-org"
MISSING_ORG_ID = "no-such-org-for-update"


@pytest.fixture(scope="session", autouse=True)
def _ensure_tables():
    from src.api import db_setup

    db_setup.init_database()


@pytest.fixture(autouse=True)
def _clean():
    _cleanup()
    yield
    _cleanup()


def _cleanup() -> None:
    with db_lock:
        c = db.cursor()
        org_ids = [
            r[0]
            for r in c.execute(
                "SELECT id FROM organization WHERE owner_id=?", (UID,)
            ).fetchall()
        ]
        for org_id in org_ids:
            c.execute("DELETE FROM org_member WHERE org_id=?", (org_id,))
            c.execute("DELETE FROM grape_t_org_context WHERE org_id=?", (org_id,))
        c.execute("DELETE FROM organization WHERE owner_id=?", (UID,))
        db.commit()


@pytest.fixture
def client():
    def _fake_user():
        return {"id": UID, "username": USERNAME, "auth_type": "jwt"}

    app.dependency_overrides[get_current_user_or_api_key] = _fake_user
    yield TestClient(app)
    app.dependency_overrides.clear()


def _seed_org(*, is_personal: int = 0, description: str = "原始描述") -> str:
    org = create_org(
        name="edit-org-test",
        slug=SLUG if not is_personal else f"{SLUG}-personal",
        owner_id=UID,
        description=description,
        is_personal=is_personal,
    )
    assert org
    return str(org["id"])


def test_clear_description_via_api(client):
    org_id = _seed_org(description="需要被清空的描述")

    r = client.put(f"/api/orgs/{org_id}", json={"description": ""})

    assert r.status_code == 200, r.text
    assert get_org(org_id)["description"] == ""


def test_noop_update_is_not_404(client):
    """原样保存（无任何字段变化）必须成功，不能把 rowcount=0 当成组织不存在。"""
    org_id = _seed_org(description="")

    r = client.put(f"/api/orgs/{org_id}", json={})

    assert r.status_code == 200, r.text
    assert r.json() == {"ok": True}


def test_missing_org_still_404(client):
    r = client.put(
        f"/api/orgs/{MISSING_ORG_ID}",
        json={"name": "whatever", "description": ""},
    )

    assert r.status_code == 404, r.text


def test_update_member_and_repo_limits(client):
    org_id = _seed_org()

    r = client.put(
        f"/api/orgs/{org_id}",
        json={"max_members": 50, "max_repos": 33},
    )

    assert r.status_code == 200, r.text
    org = get_org(org_id)
    assert org["max_members"] == 50
    assert org["max_repos"] == 33


def test_personal_org_limits_ignored(client):
    org_id = _seed_org(is_personal=1)
    before = get_org(org_id)

    r = client.put(
        f"/api/orgs/{org_id}",
        json={"max_members": 99, "max_repos": 99},
    )

    assert r.status_code == 200, r.text
    after = get_org(org_id)
    assert after["max_members"] == before["max_members"]
    assert after["max_repos"] == before["max_repos"]


def test_update_org_none_means_unchanged():
    org_id = _seed_org(description="保留我")

    assert update_org(org_id, description=None) is True
    assert get_org(org_id)["description"] == "保留我"

    assert update_org(org_id, description="") is True
    assert get_org(org_id)["description"] == ""


def test_update_org_zero_limit_keeps_value():
    """0 保持历史语义“不修改”，防止旧客户端把额度误改成 0。"""
    org_id = _seed_org()
    before = get_org(org_id)

    assert update_org(org_id, max_members=0, max_repos=0) is True
    after = get_org(org_id)
    assert after["max_members"] == before["max_members"]
    assert after["max_repos"] == before["max_repos"]


def test_update_org_missing_returns_false():
    assert update_org(MISSING_ORG_ID, name="x") is False