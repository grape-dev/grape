"""Issue #2118: organization scene template and entry contracts."""
from __future__ import annotations

import json
import os
import sys

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from src.api.deps import get_current_user_or_api_key, require_org_member
from src.api.main import app
from src.kernel.session_manager import db, db_lock

UID = 22118
USERNAME = "issue2118-owner"
ORG_SLUG = "issue2118-org"
ORG_REPO_KEY = "gitcode:issue2118/org-repo"
NORMAL_REPO_KEY = "gitcode:issue2118/normal-repo"
ORG_SCENE_ID = "custom-org-issue2118"
NORMAL_SCENE_ID = "custom-standard-issue2118"
SIMPLIFIED_SCENE_ID = "custom-org-simplified-issue2118"


def _required_node_fields():
    return {
        "id", "tag", "skill", "name", "mode", "next", "handoff",
        "skip", "enabled", "engine_runtime_ids",
    }


@pytest.fixture(scope="session", autouse=True)
def _ensure_tables():
    from src.api import db_setup

    db_setup.init_database()


@pytest.fixture(autouse=True)
def _clean():
    _cleanup()
    yield
    _cleanup()


def _cleanup():
    from src.api.pipeline_config import invalidate_scene_cache

    with db_lock:
        c = db.cursor()
        org_ids = [
            r[0] for r in c.execute(
                "SELECT id FROM organization WHERE owner_id=?", (UID,)
            ).fetchall()
        ]
        for org_id in org_ids:
            c.execute("DELETE FROM org_member WHERE org_id=?", (org_id,))
        c.execute("DELETE FROM organization WHERE owner_id=?", (UID,))
        for repo_key in (ORG_REPO_KEY, NORMAL_REPO_KEY):
            c.execute("DELETE FROM managed_repos WHERE repo_key=?", (repo_key,))
            c.execute("DELETE FROM devops_pipeline_scene WHERE repo_key=?", (repo_key,))
        c.execute("DELETE FROM scene_market WHERE name='org' AND source='official'")
        db.commit()
    invalidate_scene_cache(ORG_SCENE_ID, ORG_REPO_KEY)
    invalidate_scene_cache(NORMAL_SCENE_ID, NORMAL_REPO_KEY)
    invalidate_scene_cache(SIMPLIFIED_SCENE_ID, ORG_REPO_KEY)
    invalidate_scene_cache("org", "")


def _seed_managed_repo(repo_key: str, *, org_id: int = 0, is_org_repo: int = 0):
    from src.domains.mgmt.user.db import add_managed_repo
    from src.kernel.repo_key import parse_repo_key

    _provider, owner, name = parse_repo_key(repo_key)
    assert add_managed_repo(repo_key, owner, name, webhook_id=2118, org_id=org_id)
    with db_lock:
        c = db.cursor()
        c.execute(
            "UPDATE managed_repos SET is_org_repo=? WHERE repo_key=?",
            (is_org_repo, repo_key),
        )
        db.commit()


def _seed_org_scene() -> int:
    from src.api.pipeline_config import create_org_scene
    from src.domains.mgmt.user.org_db import create_org, update_org_provision_fields

    org = create_org(name="Issue 2118 Org", slug=ORG_SLUG, owner_id=UID)
    assert org
    org_id = org["id"]
    assert update_org_provision_fields(
        org_id,
        org_repo_key=ORG_REPO_KEY,
        org_scene_id=ORG_SCENE_ID,
        provision_status="ready",
    )
    _seed_managed_repo(ORG_REPO_KEY, org_id=org_id, is_org_repo=1)
    create_org_scene(
        scene_id=ORG_SCENE_ID,
        org_name="Issue 2118 Org",
        repo_key=ORG_REPO_KEY,
        created_by=USERNAME,
        org_id=org_id,
    )
    return org_id


def _seed_standard_scene(repo_key: str):
    from src.api.pipeline_config import create_custom_scene

    nodes = [
        {
            "id": "issue", "tag": "issue", "skill": "issue-cli", "mode": "emit",
            "next": "coding", "handoff": "a2a", "skip": [], "enabled": True,
            "engine_runtime_ids": [],
        },
        {
            "id": "coding", "tag": "coding", "skill": "coding-cli", "mode": "emit",
            "next": "done", "handoff": "complete", "skip": [], "enabled": True,
            "engine_runtime_ids": [],
        },
    ]
    create_custom_scene(
        scene_id=NORMAL_SCENE_ID,
        name="Standard",
        description="standard scene",
        repo_key=repo_key,
        nodes=nodes,
        scene_type="standard",
    )


def test_org_scene_nodes_are_complete_and_dispatchable():
    from src.api.pipeline_config import ORG_SCENE_NODES
    from src.domains.runtime.engine.engine_select import KNOWN_AGENT_ROLES

    assert [n["id"] for n in ORG_SCENE_NODES] == [
        "org_vod", "org_intake", "org_dispatch", "org_aggregate",
    ]
    assert [n["tag"] for n in ORG_SCENE_NODES] == ["vod", "issue", "coding", "qa"]
    assert [n["skill"] for n in ORG_SCENE_NODES] == [
        "vod-cli", "org-intake-cli", "org-dispatch-cli", "org-aggregate-cli",
    ]
    assert [n["next"] for n in ORG_SCENE_NODES] == [
        "org_intake", "org_dispatch", "org_aggregate", "done",
    ]
    assert [n["handoff"] for n in ORG_SCENE_NODES] == [
        "a2a", "a2a", "a2a", "complete",
    ]
    assert all(_required_node_fields() <= set(n) for n in ORG_SCENE_NODES)
    assert all(n["mode"] == "emit" for n in ORG_SCENE_NODES)
    assert all(n["skip"] == [] for n in ORG_SCENE_NODES)
    assert all(n["enabled"] is True for n in ORG_SCENE_NODES)
    assert all(n["engine_runtime_ids"] == [] for n in ORG_SCENE_NODES)
    assert {n["tag"] for n in ORG_SCENE_NODES} <= KNOWN_AGENT_ROLES


def test_pipelines_org_matches_code_constant_and_is_unbound():
    from src.api.pipeline_config import (
        ORG_SCENE_NODES,
        get_pipeline_config,
        get_scene_engine_binding,
        invalidate_scene_cache,
    )

    with open(
        os.path.join(os.path.dirname(__file__), "..", "..", "configs", "pipelines.json"),
        encoding="utf-8",
    ) as fp:
        configured = json.load(fp)

    assert configured["scenes"]["org"]["nodes"] == ORG_SCENE_NODES
    invalidate_scene_cache("org", "")
    cfg = get_pipeline_config("org")
    assert cfg["nodes"] == ORG_SCENE_NODES
    for tag in ("vod", "issue", "coding", "qa"):
        binding = get_scene_engine_binding("org", tag)
        assert binding["status"] == "unbound"
        assert binding["engine_ids"] == []


def test_official_org_scene_seed_is_incremental_and_idempotent():
    from src.api import db_setup
    from src.api.pipeline_config import ORG_SCENE_NODES
    from src.domains.mgmt.user.db import set_meta_value

    set_meta_value("scene_market_seeded", "1")
    with db_lock:
        c = db.cursor()
        c.execute("DELETE FROM scene_market WHERE name='org' AND source='official'")
        c.execute(
            "INSERT INTO scene_market "
            "(name, display_name, description, category_id, source, nodes_json, "
            "onboarding, is_active, scope) "
            "VALUES ('org', 'legacy', '', 1, 'official', '[]', "
            "'repo-onboarding', 1, 'official')"
        )
        db.commit()

    db_setup._seed_official_scene_market_from_pipelines()

    with db_lock:
        c = db.cursor()
        rows = c.execute(
            "SELECT nodes_json, scope FROM scene_market "
            "WHERE name='org' AND source='official' AND is_active=1"
        ).fetchall()
    assert len(rows) == 1
    assert json.loads(rows[0][0]) == ORG_SCENE_NODES
    assert rows[0][1] == "org"

    db_setup._seed_official_scene_market_from_pipelines()

    with db_lock:
        c = db.cursor()
        count = c.execute(
            "SELECT COUNT(*) FROM scene_market WHERE name='org' AND source='official'"
        ).fetchone()[0]
    assert count == 1


def test_org_scene_market_template_is_hidden_and_not_applicable():
    from fastapi import HTTPException

    from src.api import db_setup
    from src.domains.mgmt.routes.scenes import apply_market_scene
    from src.domains.mgmt.user.db import (
        delete_scene_market,
        get_scene_market_by_id,
        list_scene_market,
        set_meta_value,
        update_scene_market,
    )

    set_meta_value("scene_market_seeded", "1")
    with db_lock:
        c = db.cursor()
        c.execute("DELETE FROM scene_market WHERE name='org' AND source='official'")
        db.commit()
    db_setup._seed_official_scene_market_from_pipelines()

    scenes, _total = list_scene_market()
    assert "org" not in {s["name"] for s in scenes}
    org_scenes, _total = list_scene_market(scope="org")
    assert "org" in {s["name"] for s in org_scenes}

    org_scene = next(s for s in org_scenes if s["name"] == "org")
    assert get_scene_market_by_id(org_scene["id"])["scope"] == "org"
    assert update_scene_market(org_scene["id"], display_name="hacked") is False
    assert delete_scene_market(org_scene["id"]) is False
    assert get_scene_market_by_id(org_scene["id"])["display_name"] != "hacked"

    _seed_managed_repo(NORMAL_REPO_KEY)
    with pytest.raises(HTTPException) as exc:
        apply_market_scene(
            org_scene["id"],
            user={"id": UID, "username": USERNAME},
            owner="issue2118",
            repo="normal-repo",
            repo_key=NORMAL_REPO_KEY,
        )
    assert exc.value.status_code == 400
    assert exc.value.detail["error_code"] == "ORG_SCENE_PROTECTED"


def test_create_org_scene_persists_complete_nodes_and_org_metadata():
    from src.api.pipeline_config import ORG_SCENE_NODES

    org_id = _seed_org_scene()
    with db_lock:
        c = db.cursor()
        row = c.execute(
            "SELECT nodes_json, scene_type, org_id FROM devops_pipeline_scene "
            "WHERE scene_id=? AND repo_key=? AND is_active=1",
            (ORG_SCENE_ID, ORG_REPO_KEY),
        ).fetchone()

    assert row
    assert json.loads(row[0]) == ORG_SCENE_NODES
    assert row[1] == "org"
    assert row[2] == org_id


def test_two_node_simplified_template_can_be_created_with_skip():
    from src.api.pipeline_config import (
        create_custom_scene,
        get_next_node,
        get_pipeline_config,
        invalidate_scene_cache,
        is_node_skipped,
    )

    _seed_managed_repo(ORG_REPO_KEY, is_org_repo=0)
    nodes = [
        {
            "id": "org_dispatch", "tag": "coding", "skill": "org-dispatch-cli",
            "name": "组织任务派发", "mode": "emit", "next": "org_aggregate",
            "handoff": "a2a", "skip": ["org_intake"], "enabled": True,
            "engine_runtime_ids": [],
        },
        {
            "id": "org_aggregate", "tag": "qa", "skill": "org-aggregate-cli",
            "name": "组织结果聚合", "mode": "emit", "next": "done",
            "handoff": "complete", "skip": [], "enabled": True,
            "engine_runtime_ids": [],
        },
    ]
    create_custom_scene(
        scene_id=SIMPLIFIED_SCENE_ID,
        name="Organization Simplified",
        description="2-node organization template",
        repo_key=ORG_REPO_KEY,
        nodes=nodes,
        scene_type="org",
    )

    invalidate_scene_cache(SIMPLIFIED_SCENE_ID, ORG_REPO_KEY)
    cfg = get_pipeline_config(SIMPLIFIED_SCENE_ID, repo_key=ORG_REPO_KEY)
    assert [n["id"] for n in cfg["nodes"]] == ["org_dispatch", "org_aggregate"]
    assert is_node_skipped(
        SIMPLIFIED_SCENE_ID, "org_intake", repo_key=ORG_REPO_KEY
    ) is True
    assert get_next_node(
        SIMPLIFIED_SCENE_ID, "org_dispatch", repo_key=ORG_REPO_KEY
    )["id"] == "org_aggregate"


def test_org_repo_list_accepts_worker_session_token_and_is_dual_scope():
    from src.api.openapi import _iter_routes, token_scope

    org_id = _seed_org_scene()
    route = next(
        route
        for wrapped in app.routes
        for route in _iter_routes(wrapped)
        if getattr(route, "operation_id", "") == "org_repos_list"
    )
    assert token_scope(route) == "dual"

    app.dependency_overrides[get_current_user_or_api_key] = lambda: {
        "id": UID,
        "username": USERNAME,
        "auth_type": "session_token",
        "repo_key": ORG_REPO_KEY,
        "agent_type": "coding",
    }
    try:
        response = TestClient(app).get(f"/api/orgs/{org_id}/repos")
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200, response.text
    assert [row["repo_key"] for row in response.json()["repos"]] == [ORG_REPO_KEY]


def test_org_entry_requires_matching_org_repo_and_org_scene():
    from fastapi import HTTPException

    from src.api.org_context import validate_org_scene_entry
    from src.api.pipeline_config import create_org_scene

    org_id = _seed_org_scene()
    selected = validate_org_scene_entry(
        ORG_REPO_KEY, ORG_SCENE_ID, context_org_id=org_id
    )
    assert selected == {
        "is_org": True,
        "org_id": org_id,
        "repo_key": ORG_REPO_KEY,
        "scene_id": ORG_SCENE_ID,
    }

    with pytest.raises(HTTPException) as wrong_context:
        validate_org_scene_entry(
            ORG_REPO_KEY, ORG_SCENE_ID, context_org_id=str(org_id) + "-wrong"
        )
    assert wrong_context.value.status_code == 403
    assert wrong_context.value.detail["error_code"] == "PERMISSION_DENIED"

    _seed_standard_scene(ORG_REPO_KEY)
    with pytest.raises(HTTPException) as wrong_scene:
        validate_org_scene_entry(
            ORG_REPO_KEY, NORMAL_SCENE_ID, context_org_id=org_id
        )
    assert wrong_scene.value.status_code == 400
    assert wrong_scene.value.detail["error_code"] == "INVALID_ORG_CONTEXT"

    _seed_managed_repo(NORMAL_REPO_KEY)
    create_org_scene(
        scene_id="custom-org-wrong-repo-2118",
        org_name="Wrong repo org",
        repo_key=NORMAL_REPO_KEY,
        org_id=org_id,
    )
    with pytest.raises(HTTPException) as wrong_repo:
        validate_org_scene_entry(
            NORMAL_REPO_KEY, "custom-org-wrong-repo-2118", context_org_id=org_id
        )
    assert wrong_repo.value.status_code == 400
    assert wrong_repo.value.detail["error_code"] == "INVALID_ORG_CONTEXT"


def test_ordinary_repo_scene_entry_is_unchanged():
    from src.api.org_context import validate_org_scene_entry

    _seed_managed_repo(NORMAL_REPO_KEY)
    _seed_standard_scene(NORMAL_REPO_KEY)
    assert validate_org_scene_entry(
        NORMAL_REPO_KEY, NORMAL_SCENE_ID, context_org_id=0
    ) == {"is_org": False}


def test_org_scene_metadata_lookup_failure_is_fail_closed(monkeypatch, caplog):
    from fastapi import HTTPException

    import src.api.org_context as org_context
    from src.domains.mgmt import public as mgmt_public

    _seed_managed_repo(NORMAL_REPO_KEY)

    def _broken_lookup(*_args, **_kwargs):
        raise RuntimeError("database unavailable")

    # 场景元数据读取已收敛到 mgmt 域 public，模拟该原语不可用即可。
    monkeypatch.setattr(mgmt_public, "get_scene_org_metadata", _broken_lookup)
    caplog.set_level("WARNING")

    with pytest.raises(HTTPException) as exc:
        org_context.validate_org_scene_entry(
            NORMAL_REPO_KEY, NORMAL_SCENE_ID, context_org_id=0
        )

    assert exc.value.status_code == 503
    assert exc.value.detail["error_code"] == "ORG_SCENE_LOOKUP_FAILED"
    assert "_org_scene_metadata lookup failed" in caplog.text.lower()


def test_existing_org_scene_type_is_migrated():
    """存量 org 场景（provision 已完成、未重跑）启动迁移为 scene_type=org。"""
    from src.api.db_setup import _fn_migrate_org_scene_type
    from src.api.pipeline_config import create_custom_scene, invalidate_scene_cache

    _seed_org_scene()
    _seed_standard_scene(ORG_REPO_KEY)
    legacy_prefix_scene = "custom-org-legacy-2118"
    create_custom_scene(
        scene_id=legacy_prefix_scene,
        name="Legacy org pipeline",
        description="legacy",
        repo_key=ORG_REPO_KEY,
        nodes=[],
        scene_type="standard",
    )

    with db_lock:
        c = db.cursor()
        c.execute(
            "UPDATE devops_pipeline_scene SET scene_type='standard' "
            "WHERE scene_id IN (?, ?, ?)",
            (ORG_SCENE_ID, legacy_prefix_scene, NORMAL_SCENE_ID))
        db.commit()

    _fn_migrate_org_scene_type()
    invalidate_scene_cache(ORG_SCENE_ID, ORG_REPO_KEY)
    invalidate_scene_cache(legacy_prefix_scene, ORG_REPO_KEY)
    invalidate_scene_cache(NORMAL_SCENE_ID, ORG_REPO_KEY)

    with db_lock:
        c = db.cursor()
        rows = dict(c.execute(
            "SELECT scene_id, scene_type FROM devops_pipeline_scene "
            "WHERE scene_id IN (?, ?, ?)",
            (ORG_SCENE_ID, legacy_prefix_scene, NORMAL_SCENE_ID)).fetchall())

    assert rows[ORG_SCENE_ID] == "org"
    assert rows[legacy_prefix_scene] == "org"
    assert rows[NORMAL_SCENE_ID] == "standard", "普通自定义场景不得被误升级"


def test_legacy_org_scene_gets_vod_node_migration(monkeypatch):
    """Issue #2321：存量 3 节点组织场景启动迁移为 4 节点（幂等 + 审计摘要）。"""
    from src.api import db_setup
    from src.api.db_setup import _fn_migrate_org_scene_vod_node
    from src.api.pipeline_config import ORG_SCENE_NODES, invalidate_scene_cache

    audits = []
    monkeypatch.setattr(
        db_setup, "_audit_ops_event",
        lambda event, detail: audits.append((event, detail)),
    )

    _seed_org_scene()
    legacy_nodes = [dict(n) for n in ORG_SCENE_NODES[1:]]
    with db_lock:
        c = db.cursor()
        c.execute(
            "UPDATE devops_pipeline_scene SET nodes_json=?, name=?, "
            "display_name=?, description=? WHERE scene_id=? AND repo_key=?",
            (json.dumps(legacy_nodes, ensure_ascii=False),
             "组织任务下派 - Issue 2118 Org",
             "组织任务下派 - Issue 2118 Org",
             "组织需求接收、任务下派与结果聚合",
             ORG_SCENE_ID, ORG_REPO_KEY),
        )
        db.commit()
    invalidate_scene_cache(ORG_SCENE_ID, ORG_REPO_KEY)

    _fn_migrate_org_scene_vod_node()
    invalidate_scene_cache(ORG_SCENE_ID, ORG_REPO_KEY)

    with db_lock:
        c = db.cursor()
        row = c.execute(
            "SELECT nodes_json, name, display_name, description "
            "FROM devops_pipeline_scene "
            "WHERE scene_id=? AND repo_key=?", (ORG_SCENE_ID, ORG_REPO_KEY)).fetchone()
    nodes = json.loads(row[0])
    assert [n["id"] for n in nodes] == [
        "org_vod", "org_intake", "org_dispatch", "org_aggregate",
    ]
    assert row[1] == "组织需求下派"
    assert row[2] == "组织需求下派", "display_name 必须同步，避免前端仍显示旧名"
    assert row[3] == "组织需求分析、接收、任务下派与结果聚合"

    assert audits and audits[0][0] == "migrate_org_scene_vod_node"
    audited = audits[0][1]["scenes"]
    assert any(
        s["scene_id"] == ORG_SCENE_ID
        and s["nodes_before"] == 3
        and s["nodes_after"] == 4
        and s["old_name"] == "组织任务下派 - Issue 2118 Org"
        and s["new_name"] == "组织需求下派"
        and s["old_display_name"] == "组织任务下派 - Issue 2118 Org"
        and s["new_display_name"] == "组织需求下派"
        for s in audited
    )

    _first_audits = len(audits)
    _fn_migrate_org_scene_vod_node()
    with db_lock:
        c = db.cursor()
        again = json.loads(c.execute(
            "SELECT nodes_json FROM devops_pipeline_scene "
            "WHERE scene_id=? AND repo_key=?",
            (ORG_SCENE_ID, ORG_REPO_KEY)).fetchone()[0])
    assert [n["id"] for n in again] == [
        "org_vod", "org_intake", "org_dispatch", "org_aggregate",
    ], "重复执行不得重复插入 org_vod"
    assert len(audits) == _first_audits, "无变更时不应重复写审计"


def test_vod_migration_skips_custom_org_scene_nodes():
    """Issue #2321：自定义/精简组织场景不能被迁移覆盖。"""
    from src.api.db_setup import _fn_migrate_org_scene_vod_node
    from src.api.pipeline_config import create_custom_scene, invalidate_scene_cache

    custom_scene_id = "custom-org-custom-2118"
    custom_nodes = [
        {"id": "org_dispatch", "tag": "coding", "skill": "org-dispatch-cli",
         "mode": "emit", "next": "org_aggregate", "handoff": "a2a",
         "skip": ["org_intake"], "enabled": True, "engine_runtime_ids": []},
        {"id": "org_aggregate", "tag": "qa", "skill": "org-aggregate-cli",
         "mode": "emit", "next": "done", "handoff": "complete",
         "skip": [], "enabled": True, "engine_runtime_ids": []},
    ]
    create_custom_scene(
        scene_id=custom_scene_id,
        name="组织任务下派 - custom",
        description="custom",
        repo_key=ORG_REPO_KEY,
        nodes=custom_nodes,
        scene_type="org",
    )
    invalidate_scene_cache(custom_scene_id, ORG_REPO_KEY)

    _fn_migrate_org_scene_vod_node()
    invalidate_scene_cache(custom_scene_id, ORG_REPO_KEY)

    with db_lock:
        c = db.cursor()
        row = c.execute(
            "SELECT nodes_json, name FROM devops_pipeline_scene "
            "WHERE scene_id=? AND repo_key=?", (custom_scene_id, ORG_REPO_KEY)).fetchone()
    assert json.loads(row[0]) == custom_nodes
    assert row[1] == "组织任务下派 - custom", \
        "节点未命中旧 3 节点模板时不得单独改名"


def test_official_org_template_refresh_migration_updates_existing_org_row():
    """Issue #2321：注册式迁移 202611260002 刷新存量官方 org 模板行。"""
    from src.api import db_setup
    from src.api.pipeline_config import ORG_SCENE_NODES

    mig = next(m for m in db_setup._MIGRATIONS
               if m.version == "202611260002")
    with db_lock:
        c = db.cursor()
        c.execute("DELETE FROM scene_market WHERE name='org' AND source='official'")
        c.execute(
            "INSERT INTO scene_market "
            "(name, display_name, description, category_id, source, nodes_json, "
            "onboarding, is_active, scope) "
            "VALUES ('org', 'old name', '', 1, 'official', ?, 'repo-onboarding', 1, 'org')",
            (json.dumps([dict(n) for n in ORG_SCENE_NODES[1:]], ensure_ascii=False),),
        )
        db.commit()

    mig.fn()

    with db_lock:
        c = db.cursor()
        row = c.execute(
            "SELECT nodes_json, display_name, scope FROM scene_market "
            "WHERE name='org' AND source='official'").fetchone()
    assert json.loads(row[0]) == ORG_SCENE_NODES
    assert row[1] == "组织需求下派"
    assert row[2] == "org"


def test_create_issue_route_rejects_org_repo_with_standard_scene(monkeypatch):
    from src.api import issues_center

    org_id = _seed_org_scene()
    _seed_standard_scene(ORG_REPO_KEY)

    async def _fake_create_task_via_issue(_body, _user):
        return {"ok": True, "issue_number": 1}

    monkeypatch.setattr(issues_center, "create_task_via_issue", _fake_create_task_via_issue)
    app.dependency_overrides[require_org_member] = lambda: {
        "id": UID,
        "username": USERNAME,
        "auth_type": "jwt",
        "org_id": org_id,
    }
    try:
        response = TestClient(app).post(
            "/api/issues/issue2118/org-repo",
            json={
                "title": "organization requirement",
                "description": "must use organization scene",
                "scene_id": NORMAL_SCENE_ID,
            },
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 400
    assert response.json()["detail"]["error_code"] == "INVALID_ORG_CONTEXT"
def test_scene_is_org_scoped_detects_file_and_market_templates():
    from src.api.pipeline_config import scene_is_org_scoped, invalidate_scene_cache

    invalidate_scene_cache()
    assert scene_is_org_scoped("org") is True, "官方 org 文件模板必须被识别为组织场景"
    assert scene_is_org_scoped("default") is False
    assert scene_is_org_scoped("") is False


def test_ordinary_repo_cannot_reference_org_market_template():
    """Issue #2118 复审：普通仓库提交 scene_id=org 必须在入口被拒（不再 fail-open 建死单）。"""
    from fastapi import HTTPException

    from src.api.org_context import validate_org_scene_entry
    from src.api.pipeline_config import invalidate_scene_cache

    _seed_managed_repo(NORMAL_REPO_KEY)   # 非组织仓库
    invalidate_scene_cache("org", NORMAL_REPO_KEY)

    with pytest.raises(HTTPException) as exc:
        validate_org_scene_entry(NORMAL_REPO_KEY, "org", context_org_id=0)
    assert exc.value.status_code == 400
    assert exc.value.detail["error_code"] == "INVALID_ORG_CONTEXT"


def test_scene_precheck_blocks_normal_repo_on_org_template():
    """预校验对「普通仓库 + 组织模板」fail-closed，附可读 reason_code。"""
    from src.api.scene_precheck import precheck_scene
    from src.api.pipeline_config import invalidate_scene_cache

    _seed_managed_repo(NORMAL_REPO_KEY)
    invalidate_scene_cache("org", NORMAL_REPO_KEY)

    out = precheck_scene("org", NORMAL_REPO_KEY, {"username": USERNAME, "id": UID})

    assert out["ok"] is False
    assert any(p["reason_code"] == "org_scene_requires_org_repo" for p in out["problems"])


def test_create_issue_route_rejects_normal_repo_with_org_template(monkeypatch):
    """真实创建入口：普通仓库引用 org 模板 → 400，且不产生 VCS/DB 副作用。"""
    from src.api import issues_center
    from src.api.pipeline_config import invalidate_scene_cache

    _seed_managed_repo(NORMAL_REPO_KEY)
    invalidate_scene_cache("org", NORMAL_REPO_KEY)
    called = {"n": 0}

    async def _fake_create_task_via_issue(_body, _user):
        called["n"] += 1
        return {"ok": True, "issue_number": 1}

    monkeypatch.setattr(issues_center, "create_task_via_issue", _fake_create_task_via_issue)
    app.dependency_overrides[require_org_member] = lambda: {
        "id": UID, "username": USERNAME, "auth_type": "jwt", "org_id": 0,
    }
    try:
        response = TestClient(app).post(
            "/api/issues/issue2118/normal-repo",
            json={"title": "ordinary repo org template",
                  "description": "must be rejected", "scene_id": "org"},
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 400
    assert response.json()["detail"]["error_code"] == "INVALID_ORG_CONTEXT"
    assert called["n"] == 0, "入口拒绝后不得继续建单"


def test_set_org_scene_work_branch_is_idempotent():
    """默认分支落定后回填场景工作分支，重复调用不应报错/漂移。"""
    from src.api.pipeline_config import set_org_scene_work_branch

    _seed_org_scene()
    assert set_org_scene_work_branch(ORG_SCENE_ID, ORG_REPO_KEY, "grape-org")
    assert set_org_scene_work_branch(ORG_SCENE_ID, ORG_REPO_KEY, "grape-org")
    with db_lock:
        c = db.cursor()
        row = c.execute(
            "SELECT advanced_options FROM devops_pipeline_scene "
            "WHERE scene_id=? AND repo_key=? AND is_active=1",
            (ORG_SCENE_ID, ORG_REPO_KEY),
        ).fetchone()
    assert row and json.loads(row[0])["work_branch"] == "grape-org"
