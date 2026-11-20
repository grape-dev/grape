"""Issue #2112: organization demand cross-repo dispatch tests."""
from __future__ import annotations

import os
import sys
import uuid
from datetime import datetime, timedelta, timezone

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from fastapi.testclient import TestClient

from src.api.deps import get_current_user_or_api_key
from src.api.main import app
from src.kernel.session_manager import db, db_lock

UID = 22112
OTHER_UID = 22113
USERNAME = "issue2112-admin"
MEMBER_UID = 22114
MEMBER_NAME = "issue2112-member"
SLUG = "issue2112-team"

ORG_REPO_KEY = "gitcode:issue2112/org-repo"
TARGET_A = "gitcode:issue2112/svc-a"
TARGET_B = "gitcode:issue2112/svc-b"
UNLINKED = "gitcode:issue2112/not-linked"
ORG_ISSUE = 2112
SCENE_A = "scene-2112-a"
SCENE_B = "scene-2112-b"
SCENE_A_ALT = "scene-2112-a-alt"


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
                "SELECT id FROM organization WHERE owner_id IN (?, ?)", (UID, OTHER_UID)
            ).fetchall()
        ]
        for org_id in org_ids:
            c.execute("DELETE FROM org_member WHERE org_id=?", (org_id,))
            c.execute("DELETE FROM grape_t_org_context_snapshot WHERE org_id=?", (org_id,))
            try:
                import shutil
                from src.kernel.config import WORKSPACE_DIR
                shutil.rmtree(
                    os.path.join(WORKSPACE_DIR, "context-snapshots", f"org-{org_id}"),
                    ignore_errors=True)
            except Exception:
                pass
        c.execute("DELETE FROM organization WHERE owner_id IN (?, ?)", (UID, OTHER_UID))
        repo_keys = (ORG_REPO_KEY, TARGET_A, TARGET_B, UNLINKED)
        for repo_key in repo_keys:
            c.execute("DELETE FROM managed_repos WHERE repo_key=?", (repo_key,))
            c.execute("DELETE FROM devops_pipeline_scene WHERE repo_key=?", (repo_key,))
            c.execute("DELETE FROM issue WHERE repo_key=?", (repo_key,))
            c.execute("DELETE FROM agent_tasks WHERE repo_key=?", (repo_key,))
            c.execute("DELETE FROM events WHERE repo_key=?", (repo_key,))
        c.execute("DELETE FROM grape_t_org_dispatch_target WHERE repo_key IN (?, ?, ?)", repo_keys[:3])
        c.execute(
            "DELETE FROM grape_t_org_dispatch WHERE org_id NOT IN (SELECT id FROM organization)"
        )
        c.execute("DELETE FROM audit_logs WHERE detail LIKE '%issue2112%'")
        db.commit()


@pytest.fixture(autouse=True)
def _clean():
    _cleanup()
    yield
    _cleanup()


def _seed_org() -> int:
    from src.domains.mgmt.user.org_db import create_org

    org = create_org(slug=SLUG, name=SLUG, owner_id=UID)
    assert org
    return org["id"]


def _seed_repo(repo_key: str, org_id: int, *, dev_scene: str = "", scene_id: str = ""):
    from src.domains.mgmt.user.db import add_managed_repo
    from src.kernel.repo_key import parse_repo_key

    provider, owner, name = parse_repo_key(repo_key)
    assert provider == "gitcode"
    add_managed_repo(
        repo_key,
        owner,
        name,
        webhook_id=100,
        dev_scene=dev_scene,
        org_id=org_id,
    )
    if scene_id:
        with db_lock:
            c = db.cursor()
            c.execute(
                "INSERT OR REPLACE INTO devops_pipeline_scene "
                "(scene_id, name, repo_key, nodes_json, is_active) VALUES (?,?,?,'[]',1)",
                (scene_id, scene_id, repo_key),
            )
            db.commit()


def _seed_scene(repo_key: str, scene_id: str, *, nodes=None, advanced_options: str = ""):
    import json as _json

    with db_lock:
        c = db.cursor()
        c.execute(
            "INSERT OR REPLACE INTO devops_pipeline_scene "
            "(scene_id, name, repo_key, nodes_json, advanced_options, is_active) "
            "VALUES (?,?,?,?,?,1)",
            (scene_id, scene_id, repo_key, _json.dumps(nodes or [], ensure_ascii=False),
             advanced_options),
        )
        db.commit()


def _seed_org_issue(org_id: int):
    with db_lock:
        c = db.cursor()
        c.execute(
            "INSERT INTO issue (user_id, assignee, repo_key, issue_number, title, "
            "task_description, status, org_id) VALUES (?,?,?,?,?,?,'active',?)",
            (UID, USERNAME, ORG_REPO_KEY, ORG_ISSUE, "组织需求标题", "组织需求描述", org_id),
        )
        db.commit()


def _make_fake_create():
    """Fake create_task_via_issue that records calls and creates an issue row."""
    calls = []
    seq = {"n": 0}

    async def _fake(body, user=None):
        seq["n"] += 1
        number = 8000 + seq["n"]
        calls.append({"repo_key": body.repo_key, "title": body.title,
                      "scene_id": body.scene_id, "description": body.description,
                      "user": user})
        with db_lock:
            c = db.cursor()
            c.execute(
                "INSERT OR REPLACE INTO issue (user_id, assignee, repo_key, issue_number, "
                "title, task_description, status, scene_id, org_id, stage) "
                "VALUES (?,?,?,?,?,?,'active',?,0,'vod')",
                (int((user or {}).get("id") or 0), "", body.repo_key, number,
                 body.title, body.description, body.scene_id),
            )
            db.commit()
            task_id = c.lastrowid
        return {"ok": True, "issue_id": task_id, "task_id": task_id,
                "issue_number": number, "created": True}

    return calls, _fake


@pytest.fixture
def client(monkeypatch):
    from src.domains.mgmt.user import db as user_db
    from src.domains.mgmt import org_dispatch
    from src.api import issues_center

    state = {"role_uid": UID}
    calls, fake = _make_fake_create()
    triggers = []

    def _fake_user():
        return {"id": state["role_uid"], "username": USERNAME, "auth_type": "jwt"}

    monkeypatch.setattr(issues_center, "create_task_via_issue", fake)
    monkeypatch.setattr(user_db, "get_gitcode_token", lambda repo_key="", user_id=0: "bot-token-ok")
    monkeypatch.setattr(
        org_dispatch, "_trigger_child_issue",
        lambda repo_key, issue_number, scene_id: triggers.append(
            (repo_key, issue_number, scene_id)
        ) or {"ok": True},
    )
    app.dependency_overrides[get_current_user_or_api_key] = _fake_user
    yield TestClient(app), state, calls, triggers
    app.dependency_overrides.clear()


def _seed_standard(org_id: int):
    _seed_repo(ORG_REPO_KEY, org_id)
    _seed_repo(TARGET_A, org_id, dev_scene=SCENE_A, scene_id=SCENE_A)
    _seed_repo(TARGET_B, org_id, dev_scene=SCENE_B, scene_id=SCENE_B)
    _seed_org_issue(org_id)


def _create(api, org_id, targets, **extra):
    payload = {"org_issue_number": ORG_ISSUE, "targets": targets,
               "context": {"goal": "mvp"}, "title": "统一登录改造"}
    payload.update(extra)
    return api.post(f"/api/orgs/{org_id}/dispatches", json=payload)


def test_create_dispatch_creates_child_issues(client):
    api, _state, calls, _triggers = client
    org_id = _seed_org()
    _seed_standard(org_id)

    resp = _create(api, org_id, [TARGET_A, TARGET_B])
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["dispatch_id"]
    assert body["status"] == "running", body
    assert len(body["targets"]) == 2
    assert all(t["status"] == "running" for t in body["targets"])
    assert all(t["child_issue_number"] > 0 for t in body["targets"])
    assert len(calls) == 2
    assert calls[0]["title"].startswith(f"[Org #{ORG_ISSUE}]")
    assert calls[0]["scene_id"] == SCENE_A
    assert calls[1]["scene_id"] == SCENE_B
    assert all(c["user"]["auth_type"] == "internal" for c in calls)
    assert all(c["user"]["token"] == "bot-token-ok" for c in calls)

    with db_lock:
        c = db.cursor()
        events = [r[0] for r in c.execute(
            "SELECT event_type FROM audit_logs WHERE detail LIKE ?",
            (f"%{TARGET_A}%",)).fetchall()]
    assert "dispatch_created" in events
    assert "target_created" in events


def test_duplicate_dispatch_is_idempotent(client):
    api, _state, calls, _triggers = client
    org_id = _seed_org()
    _seed_standard(org_id)

    first = _create(api, org_id, [TARGET_A, TARGET_B])
    assert first.status_code == 201, first.text
    first_body = first.json()

    # 运行中重复下派 → 409，不重复建单
    busy = _create(api, org_id, [TARGET_B, TARGET_A])
    assert busy.status_code == 409, busy.text
    assert busy.json()["detail"]["error_code"] == "DISPATCH_IN_PROGRESS"
    assert len(calls) == 2

    # 终态后重复下派 → 复用既有 dispatch 与 child issue，200
    with db_lock:
        c = db.cursor()
        c.execute("UPDATE grape_t_org_dispatch SET status='completed' WHERE id=?",
                  (first_body["dispatch_id"],))
        db.commit()

    second = _create(api, org_id, [TARGET_B, TARGET_A])
    assert second.status_code == 200, second.text
    second_body = second.json()
    assert second_body["dispatch_id"] == first_body["dispatch_id"]
    assert {t["repo_key"] for t in second_body["targets"]} == {TARGET_A, TARGET_B}
    assert len(calls) == 2


def test_empty_targets_auto_routes_via_repo_map(client, monkeypatch):
    from src.domains.mgmt import org_repo_map

    api, _state, calls, _triggers = client
    org_id = _seed_org()
    _seed_standard(org_id)
    monkeypatch.setattr(
        org_repo_map,
        "recall_targets",
        lambda org_id, q, limit=3: {
            "query": q, "tokens": ["登录"], "targets": [TARGET_A],
            "candidates": [{"repo_key": TARGET_A, "score": 3, "reason": "命中：登录"}],
            "fallback": False, "limit": limit,
        },
    )

    resp = _create(api, org_id, [])
    assert resp.status_code in (200, 201), resp.text
    body = resp.json()
    assert {t["repo_key"] for t in body["targets"]} == {TARGET_A}
    assert body["targets_requested"] == [TARGET_A]
    assert [c["repo_key"] for c in calls] == [TARGET_A]


def test_invalid_target_rejected(client):
    api, _state, _calls, _triggers = client
    org_id = _seed_org()
    _seed_standard(org_id)
    # 关联到别的组织，等价于未关联该组织
    from src.domains.mgmt.user.org_db import create_org

    other = create_org(slug=f"{SLUG}-other", name="other", owner_id=OTHER_UID)
    _seed_repo(UNLINKED, other["id"])

    resp = _create(api, org_id, [TARGET_A, UNLINKED])
    assert resp.status_code == 400, resp.text
    assert resp.json()["detail"]["error_code"] == "INVALID_TARGET"


def test_non_admin_forbidden(client):
    api, state, _calls, _triggers = client
    org_id = _seed_org()
    _seed_standard(org_id)
    from src.domains.mgmt.user.org_db import add_org_member

    assert add_org_member(org_id, MEMBER_UID, "member")
    state["role_uid"] = MEMBER_UID

    resp = _create(api, org_id, [TARGET_A])
    assert resp.status_code == 403, resp.text
    assert resp.json()["detail"]["error_code"] == "PERMISSION_DENIED"


def test_bot_token_missing_isolated(client, monkeypatch):
    api, _state, _calls, _triggers = client
    org_id = _seed_org()
    _seed_standard(org_id)
    from src.domains.mgmt.user import db as user_db

    monkeypatch.setattr(
        user_db, "get_gitcode_token",
        lambda repo_key="", user_id=0: "" if repo_key == TARGET_B else "bot-token-ok",
    )

    resp = _create(api, org_id, [TARGET_A, TARGET_B])
    assert resp.status_code == 201, resp.text
    body = resp.json()
    by_repo = {t["repo_key"]: t for t in body["targets"]}
    assert by_repo[TARGET_A]["status"] == "running"
    assert by_repo[TARGET_B]["status"] == "failed"
    assert "BOT_TOKEN_MISSING" in by_repo[TARGET_B]["error"]
    assert body["status"] == "partial_failed"


def test_scene_resolution_failure_isolated(client):
    api, _state, calls, _triggers = client
    org_id = _seed_org()
    _seed_repo(ORG_REPO_KEY, org_id)
    _seed_repo(TARGET_A, org_id, dev_scene=SCENE_A, scene_id=SCENE_A)
    # TARGET_B 关联组织但没有任何 active 场景，且未配置默认场景
    _seed_repo(TARGET_B, org_id)
    _seed_org_issue(org_id)

    resp = _create(api, org_id, [TARGET_A, TARGET_B])
    assert resp.status_code == 201, resp.text
    body = resp.json()
    by_repo = {t["repo_key"]: t for t in body["targets"]}
    assert by_repo[TARGET_A]["status"] == "running"
    assert by_repo[TARGET_B]["status"] == "failed"
    assert "NO_TARGET_SCENE" in by_repo[TARGET_B]["error"]
    assert len(calls) == 1
    assert body["status"] == "partial_failed"


def test_sweep_aggregates_completion_and_timeout(client):
    api, _state, _calls, _triggers = client
    org_id = _seed_org()
    _seed_standard(org_id)
    from src.domains.mgmt import org_dispatch

    created = _create(api, org_id, [TARGET_A, TARGET_B]).json()
    by_repo = {t["repo_key"]: t for t in created["targets"]}

    with db_lock:
        c = db.cursor()
        # TARGET_A 完成
        c.execute(
            "UPDATE issue SET status='completed', stage='done' WHERE repo_key=? AND issue_number=?",
            (TARGET_A, by_repo[TARGET_A]["child_issue_number"]),
        )
        # TARGET_B 超时（2h 前开始，仍 active）
        old = (datetime.now(timezone.utc) - timedelta(hours=3)).strftime("%Y-%m-%d %H:%M:%S")
        c.execute(
            "UPDATE grape_t_org_dispatch_target SET started_at=? WHERE repo_key=? AND dispatch_id=?",
            (old, TARGET_B, created["dispatch_id"]),
        )
        db.commit()

    org_dispatch.sweep_once()

    detail = api.get(f"/api/orgs/{org_id}/dispatches/{created['dispatch_id']}").json()
    by_repo = {t["repo_key"]: t for t in detail["targets"]}
    assert by_repo[TARGET_A]["status"] == "completed"
    assert by_repo[TARGET_B]["status"] == "failed"
    assert "timeout" in by_repo[TARGET_B]["error"]
    assert detail["status"] == "partial_failed"

    with db_lock:
        c = db.cursor()
        events = [r[0] for r in c.execute(
            "SELECT event_type FROM audit_logs WHERE detail LIKE ?",
            (f"%{TARGET_A}%",)).fetchall()]
    assert "dispatch_partial_failed" in events

    # 重试后完成 → dispatch completed
    resp = api.post(
        f"/api/orgs/{org_id}/dispatches/{created['dispatch_id']}"
        f"/targets/{TARGET_B}/retry"
    )
    assert resp.status_code == 200, resp.text
    with db_lock:
        c = db.cursor()
        c.execute(
            "UPDATE issue SET status='completed', stage='done' WHERE repo_key=? AND issue_number=?",
            (TARGET_B, by_repo[TARGET_B]["child_issue_number"]),
        )
        db.commit()
    org_dispatch.sweep_once()
    detail = api.get(f"/api/orgs/{org_id}/dispatches/{created['dispatch_id']}").json()
    assert detail["status"] == "completed"
    with db_lock:
        c = db.cursor()
        events = [r[0] for r in c.execute(
            "SELECT event_type FROM audit_logs WHERE detail LIKE ?",
            (f"%{TARGET_B}%",)).fetchall()]
    assert "dispatch_completed" in events


def test_cancel_cancels_children_and_dispatch(client):
    api, _state, _calls, _triggers = client
    org_id = _seed_org()
    _seed_standard(org_id)

    created = _create(api, org_id, [TARGET_A, TARGET_B]).json()
    with db_lock:
        c = db.cursor()
        c.execute(
            "INSERT INTO agent_tasks (id, agent_session_id, runtime_id, prompt, repo_key, "
            "issue_id, status) VALUES (?,?,?,?,?,?, 'running')",
            (f"t-{TARGET_A}", "sess-2112", 1, "p", TARGET_A,
             f"#{created['targets'][0]['child_issue_number']}"),
        )
        db.commit()

    resp = api.post(f"/api/orgs/{org_id}/dispatches/{created['dispatch_id']}/cancel")
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "cancelled"

    detail = api.get(f"/api/orgs/{org_id}/dispatches/{created['dispatch_id']}").json()
    assert detail["status"] == "cancelled"
    assert all(t["status"] == "cancelled" for t in detail["targets"])
    with db_lock:
        c = db.cursor()
        statuses = {r[0] for r in c.execute(
            "SELECT status FROM issue WHERE repo_key IN (?, ?)", (TARGET_A, TARGET_B)
        ).fetchall()}
        events = [r[0] for r in c.execute(
            "SELECT event_type FROM audit_logs WHERE detail LIKE ?",
            (f"%{TARGET_A}%",)).fetchall()]
    assert statuses == {"cancelled"}
    assert "dispatch_cancelled" in events


def test_retry_reuses_child_issue(client):
    api, _state, _calls, triggers = client
    org_id = _seed_org()
    _seed_standard(org_id)

    created = _create(api, org_id, [TARGET_A, TARGET_B]).json()
    target_b = next(t for t in created["targets"] if t["repo_key"] == TARGET_B)
    child = target_b["child_issue_number"]

    with db_lock:
        c = db.cursor()
        c.execute(
            "UPDATE grape_t_org_dispatch_target SET status='failed', error='timeout' "
            "WHERE dispatch_id=? AND repo_key=?",
            (created["dispatch_id"], TARGET_B),
        )
        c.execute("UPDATE grape_t_org_dispatch SET status='partial_failed' WHERE id=?",
                  (created["dispatch_id"],))
        db.commit()

    resp = api.post(
        f"/api/orgs/{org_id}/dispatches/{created['dispatch_id']}"
        f"/targets/{TARGET_B}/retry"
    )
    assert resp.status_code == 200, resp.text
    target = resp.json()["target"]
    assert target["child_issue_number"] == child
    assert target["status"] == "running"
    assert triggers and triggers[-1][0] == TARGET_B and triggers[-1][1] == child

    with db_lock:
        c = db.cursor()
        row = c.execute(
            "SELECT COUNT(*) FROM issue WHERE repo_key=? AND issue_number=?",
            (TARGET_B, child),
        ).fetchone()
        events = [r[0] for r in c.execute(
            "SELECT event_type FROM audit_logs WHERE detail LIKE ?",
            (f"%{TARGET_B}%",)).fetchall()]
    assert row[0] == 1
    assert "target_retried" in events


def test_concurrent_dispatch_single_batch(client):
    """R1：并发发起同 targets 下派只落一条批次（幂等键唯一 + 同临界区查建）。"""
    api, _state, calls, _triggers = client
    org_id = _seed_org()
    _seed_standard(org_id)
    import asyncio
    from concurrent.futures import ThreadPoolExecutor

    from src.domains.mgmt import org_dispatch

    def _run():
        try:
            _payload, status = asyncio.run(org_dispatch.create_dispatch(
                org_id, org_issue_number=ORG_ISSUE, targets=[TARGET_A, TARGET_B],
                context={"goal": "race"}, title="并发下派", description="范围描述",
                scene_id="", user={"id": UID, "username": USERNAME}))
            return status
        except Exception as exc:  # HTTPException(409) 并发窗口落败
            return getattr(exc, "status_code", 500)

    with ThreadPoolExecutor(max_workers=2) as pool:
        statuses = sorted(f.result() for f in [pool.submit(_run), pool.submit(_run)])

    with db_lock:
        c = db.cursor()
        rows = c.execute(
            "SELECT id, idempotency_key FROM grape_t_org_dispatch WHERE org_id=?", (org_id,)
        ).fetchall()
    assert len(rows) == 1, statuses
    assert rows[0][1]
    assert statuses == [201, 409], statuses
    assert len(calls) == 2  # 仅胜出线程建了两个子单


def test_idempotency_key_unique_index_blocks_duplicate(client):
    """R1：批次幂等键唯一索引在 DB 层阻断重复批次（跨进程并发兜底）。"""
    api, _state, _calls, _triggers = client
    org_id = _seed_org()
    _seed_standard(org_id)
    key = "dupe-key-2112"
    with db_lock:
        c = db.cursor()
        c.execute(
            "INSERT INTO grape_t_org_dispatch "
            "(id, org_id, org_issue_number, status, idempotency_key) "
            "VALUES (?,?,?, 'running', ?)",
            (uuid.uuid4().hex, org_id, ORG_ISSUE, key),
        )
        db.commit()
        with pytest.raises(Exception):
            c.execute(
                "INSERT INTO grape_t_org_dispatch "
                "(id, org_id, org_issue_number, status, idempotency_key) "
                "VALUES (?,?,?, 'running', ?)",
                (uuid.uuid4().hex, org_id, ORG_ISSUE, key),
            )
            db.commit()
        db.rollback()


def test_sweep_recovers_stale_pending_target(client):
    """R2：建单中途重启遗留的 pending 目标被超时回收，dispatch 收敛且可再次下派。"""
    api, _state, _calls, _triggers = client
    org_id = _seed_org()
    _seed_standard(org_id)
    from src.domains.mgmt import org_dispatch

    created = _create(api, org_id, [TARGET_A]).json()
    did = created["dispatch_id"]
    old = (datetime.now(timezone.utc) - timedelta(hours=3)).strftime("%Y-%m-%d %H:%M:%S")
    with db_lock:
        c = db.cursor()
        c.execute(
            "UPDATE grape_t_org_dispatch_target SET status='pending', child_issue_number=0, "
            "child_task_id=0, started_at=NULL, created_at=? WHERE dispatch_id=?",
            (old, did),
        )
        db.commit()

    org_dispatch.sweep_once()

    detail = api.get(f"/api/orgs/{org_id}/dispatches/{did}").json()
    assert detail["status"] == "failed", detail
    assert detail["targets"][0]["status"] == "failed"
    assert "timeout" in detail["targets"][0]["error"]

    # 终态后同 targets 再下派不再永久 409
    again = _create(api, org_id, [TARGET_A])
    assert again.status_code == 200, again.text
    assert again.json()["dispatch_id"] == did


def test_timeout_query_uses_parameterized_utc_cutoff():
    """R2'：MySQL 兼容层只翻译无参 datetime('now')，带修饰符方言不得进入 SQL。"""
    import inspect

    from src.domains.mgmt import org_dispatch

    source = inspect.getsource(org_dispatch._timeout_targets)
    assert "COALESCE(started_at, created_at) <= ?" in source
    assert "datetime('now'," not in source


def test_cancel_held_when_target_already_completed(client):
    """R3：子单已完成时取消不下发 cancelled，返回未取消目标且状态不漂移。"""
    api, _state, _calls, _triggers = client
    org_id = _seed_org()
    _seed_standard(org_id)

    created = _create(api, org_id, [TARGET_A, TARGET_B]).json()
    did = created["dispatch_id"]
    child_a = next(
        t for t in created["targets"] if t["repo_key"] == TARGET_A
    )["child_issue_number"]
    with db_lock:
        c = db.cursor()
        c.execute(
            "UPDATE issue SET status='completed', stage='done' "
            "WHERE repo_key=? AND issue_number=?",
            (TARGET_A, child_a),
        )
        db.commit()

    resp = api.post(f"/api/orgs/{org_id}/dispatches/{did}/cancel")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["ok"] is False
    assert body["status"] != "cancelled"
    assert TARGET_A in body["unresolved_targets"]

    detail = api.get(f"/api/orgs/{org_id}/dispatches/{did}").json()
    assert detail["status"] != "cancelled"
    by_repo = {t["repo_key"]: t for t in detail["targets"]}
    assert by_repo[TARGET_A]["status"] == "completed"


def test_retry_cancels_inflight_and_marks_trigger_failure(client, monkeypatch):
    """R4：重试前终止原在途 agent；pipeline 跳过（ok=false）时立即置 TRIGGER_FAILED。"""
    api, _state, _calls, _triggers = client
    org_id = _seed_org()
    _seed_standard(org_id)
    from src.domains.mgmt import org_dispatch

    created = _create(api, org_id, [TARGET_A]).json()
    did = created["dispatch_id"]
    child = created["targets"][0]["child_issue_number"]
    at_id = f"inflight-{TARGET_A}"
    with db_lock:
        c = db.cursor()
        c.execute(
            "INSERT INTO agent_tasks (id, agent_session_id, runtime_id, prompt, repo_key, "
            "issue_id, status) VALUES (?,?,?,?,?,?, 'running')",
            (at_id, "sess-r4", 1, "p", TARGET_A, f"#{child}"),
        )
        c.execute(
            "UPDATE grape_t_org_dispatch_target SET status='failed', error='timeout' "
            "WHERE dispatch_id=? AND repo_key=?",
            (did, TARGET_A),
        )
        c.execute("UPDATE grape_t_org_dispatch SET status='partial_failed' WHERE id=?", (did,))
        db.commit()

    seen = {}

    def _reject(repo_key, issue_number, scene_id):
        with db_lock:
            c = db.cursor()
            seen["agent_status"] = c.execute(
                "SELECT status FROM agent_tasks WHERE id=?", (at_id,)
            ).fetchone()[0]
        return {"ok": False, "seq": 0}

    monkeypatch.setattr(org_dispatch, "_trigger_child_issue", _reject)

    resp = api.post(
        f"/api/orgs/{org_id}/dispatches/{did}/targets/{TARGET_A}/retry"
    )
    assert resp.status_code == 200, resp.text
    assert seen.get("agent_status") == "cancelled"
    target = resp.json()["target"]
    assert target["status"] == "failed"
    assert "TRIGGER_FAILED" in target["error"]


def test_concurrent_retry_only_triggers_once(client, monkeypatch):
    """R4'：并发重试只有一个请求能抢占 failed→running 并触发 pipeline。"""
    import asyncio
    import threading
    from concurrent.futures import ThreadPoolExecutor

    api, _state, _calls, triggers = client
    org_id = _seed_org()
    _seed_standard(org_id)
    from src.domains.mgmt import org_dispatch

    created = _create(api, org_id, [TARGET_A]).json()
    did = created["dispatch_id"]
    child = created["targets"][0]["child_issue_number"]
    with db_lock:
        c = db.cursor()
        c.execute(
            "UPDATE grape_t_org_dispatch_target SET status='failed', error='timeout' "
            "WHERE dispatch_id=? AND repo_key=?",
            (did, TARGET_A),
        )
        c.execute("UPDATE grape_t_org_dispatch SET status='partial_failed' WHERE id=?", (did,))
        db.commit()

    barrier = threading.Barrier(2)
    original_claim = org_dispatch._claim_failed_target_for_retry

    def _coordinated_claim(target_id, next_status):
        barrier.wait(timeout=5)
        return original_claim(target_id, next_status)

    monkeypatch.setattr(
        org_dispatch, "_claim_failed_target_for_retry", _coordinated_claim
    )

    async def _retry():
        return await org_dispatch.retry_target(
            org_id, did, TARGET_A, user={"id": UID, "username": USERNAME}
        )

    def _run_retry():
        try:
            result = asyncio.run(_retry())
            return ("ok", result["target"]["status"])
        except Exception as exc:
            return ("error", getattr(exc, "status_code", 500))

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = [f.result() for f in [pool.submit(_run_retry), pool.submit(_run_retry)]]

    assert len(triggers) == 1, (results, triggers)
    assert sorted(results) == [("error", 409), ("ok", "running")], results
    with db_lock:
        c = db.cursor()
        row = c.execute(
            "SELECT child_issue_number, status FROM grape_t_org_dispatch_target "
            "WHERE dispatch_id=? AND repo_key=?",
            (did, TARGET_A),
        ).fetchone()
    assert str(row[0]) == str(child)
    assert row[1] == "running"


def test_retry_rebuild_preserves_description(client, monkeypatch):
    """R5：无子单目标重试重建后，子 Issue 目标范围与首次一致。"""
    api, _state, calls, _triggers = client
    org_id = _seed_org()
    _seed_standard(org_id)
    from src.domains.mgmt.user import db as user_db

    desc = "仅改造登录与鉴权模块"
    monkeypatch.setattr(
        user_db, "get_gitcode_token",
        lambda repo_key="", user_id=0: "" if repo_key == TARGET_B else "bot-token-ok",
    )
    resp = api.post(
        f"/api/orgs/{org_id}/dispatches",
        json={"org_issue_number": ORG_ISSUE, "targets": [TARGET_A, TARGET_B],
              "context": {"goal": "mvp"}, "title": "统一登录改造", "description": desc},
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    did = body["dispatch_id"]
    by_repo = {t["repo_key"]: t for t in body["targets"]}
    assert by_repo[TARGET_B]["status"] == "failed"
    assert not by_repo[TARGET_B]["child_issue_number"]
    assert len(calls) == 1  # 仅 TARGET_A 建成子单

    # 修复 token 后重试 → 重建子单，保留原目标范围描述
    monkeypatch.setattr(
        user_db, "get_gitcode_token", lambda repo_key="", user_id=0: "bot-token-ok",
    )
    retry = api.post(
        f"/api/orgs/{org_id}/dispatches/{did}/targets/{TARGET_B}/retry"
    )
    assert retry.status_code == 200, retry.text
    assert len(calls) == 2
    rebuilt = calls[-1]
    assert rebuilt["repo_key"] == TARGET_B
    assert desc in rebuilt["description"]
    rebuilt_target = retry.json()["target"]
    assert rebuilt_target["status"] == "running"
    assert rebuilt_target["child_issue_number"] > 0
    with db_lock:
        c = db.cursor()
        stored = c.execute(
            "SELECT child_issue_number, status FROM grape_t_org_dispatch_target "
            "WHERE dispatch_id=? AND repo_key=?",
            (did, TARGET_B),
        ).fetchone()
    assert str(stored[0]) == str(rebuilt_target["child_issue_number"])
    assert stored[1] == "running"


def test_org_dispatch_openapi_is_dual_scope(client):
    """#1264 依赖：组织下派 CLI 可由 session token 调用，组织权限仍由路由显式校验。"""
    schema = app.openapi()
    operations = {
        "/api/orgs/{org_id}/dispatches": ("post", "get"),
        "/api/orgs/{org_id}/dispatches/{dispatch_id}": ("get",),
        "/api/orgs/{org_id}/dispatches/{dispatch_id}/cancel": ("post",),
        "/api/orgs/{org_id}/dispatches/{dispatch_id}/targets/{repo_key}/retry": ("post",),
    }
    for path, methods in operations.items():
        for method in methods:
            op = schema["paths"][path][method]
            expected = (
                "user_only"
                if path.endswith("/cancel") or path.endswith("/retry")
                else "dual"
            )
            assert op["x-token-scope"] == expected, (method, path, op["x-token-scope"])


def test_sweep_marks_target_failed_on_a2a_dispatch_failure(client):
    """A2A 派发失败后子 Issue 停在 processing/issue，target 不能永久 running。"""
    api, _state, _calls, _triggers = client
    org_id = _seed_org()
    _seed_standard(org_id)
    resp = _create(api, org_id, [TARGET_A])
    assert resp.status_code == 201, resp.text
    body = resp.json()
    dispatch_id = body["dispatch_id"]
    child = body["targets"][0]["child_issue_number"]
    assert child > 0

    with db_lock:
        c = db.cursor()
        c.execute(
            "UPDATE issue SET status='processing', stage='issue' "
            "WHERE repo_key=? AND issue_number=?",
            (TARGET_A, child),
        )
        c.execute(
            "INSERT INTO events (repo_key, event_type, source, issue_id, title, "
            "status, result, created_at) VALUES (?,?,?,?,?,?,?,datetime('now'))",
            (TARGET_A, "a2a_event", "a2a", f"#{child}", "handoff", "failed",
             "[a2a_dispatch_failed] _enqueue_pipeline_task() got an unexpected keyword"),
        )
        db.commit()

    from src.domains.mgmt.org_dispatch import _refresh_targets_from_issues

    _refresh_targets_from_issues(dispatch_id)
    with db_lock:
        c = db.cursor()
        c.execute(
            "SELECT status, error FROM grape_t_org_dispatch_target "
            "WHERE dispatch_id=? AND repo_key=?",
            (dispatch_id, TARGET_A),
        )
        status, error = c.fetchone()
    assert status == "failed"
    assert "A2A_DISPATCH_FAILED" in (error or "")


def test_sweep_marks_target_failed_on_failed_task(client):
    """issue_done 目标的最新中国任务失败且无在途重试时，不能等 2h 超时。"""
    api, _state, _calls, _triggers = client
    org_id = _seed_org()
    _seed_standard(org_id)
    resp = _create(api, org_id, [TARGET_A])
    assert resp.status_code == 201, resp.text
    body = resp.json()
    dispatch_id = body["dispatch_id"]
    child = body["targets"][0]["child_issue_number"]
    assert child > 0

    with db_lock:
        c = db.cursor()
        c.execute(
            "UPDATE issue SET status='processing', stage='qa' "
            "WHERE repo_key=? AND issue_number=?",
            (TARGET_A, child),
        )
        c.execute(
            "INSERT INTO agent_tasks (id, agent_session_id, runtime_id, prompt, "
            "issue_id, repo_key, agent_type, status, attempts) "
            "VALUES (?,?,?,?,?,?,?,?,0)",
            ("t_test_failed_task", "sess", 1, "p", f"#{child}", TARGET_A,
             "qa", "failed"),
        )
        db.commit()

    from src.domains.mgmt.org_dispatch import _refresh_targets_from_issues

    _refresh_targets_from_issues(dispatch_id)
    with db_lock:
        c = db.cursor()
        c.execute(
            "SELECT status, error FROM grape_t_org_dispatch_target "
            "WHERE dispatch_id=? AND repo_key=?",
            (dispatch_id, TARGET_A),
        )
        status, error = c.fetchone()
    assert status == "failed"
    assert "task_terminal" in (error or "")


def test_create_dispatch_accepts_non_numeric_issue_ident(client):
    """Gitee IK* 等非数字 issue ident 可正常下派（D105）。"""
    api, _state, _calls, _triggers = client
    org_id = _seed_org()
    _seed_standard(org_id)
    ident = "IKW28R"
    with db_lock:
        c = db.cursor()
        c.execute(
            "INSERT INTO issue (user_id, assignee, repo_key, issue_number, title, "
            "task_description, status, org_id) VALUES (?,?,?,?,?,?,'active',?)",
            (UID, USERNAME, ORG_REPO_KEY, ident, "字符串编号需求", "描述", org_id),
        )
        db.commit()
    resp = api.post(
        f"/api/orgs/{org_id}/dispatches",
        json={"org_issue_number": ident, "targets": [TARGET_A],
              "title": "字符串编号需求", "description": "描述"},
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert str(body["org_issue_number"]) == ident
    assert body["targets"][0]["status"] == "running"


def test_scene_catalog_lists_linked_repo_scenes_without_secrets(client):
    api, _state, _calls, _triggers = client
    org_id = _seed_org()
    _seed_repo(ORG_REPO_KEY, org_id)
    _seed_repo(TARGET_A, org_id, dev_scene=SCENE_A, scene_id=SCENE_A)
    _seed_scene(TARGET_A, SCENE_A_ALT, advanced_options='{"secret":"do-not-leak"}')

    resp = api.get(
        f"/api/orgs/{org_id}/dispatch/scene-catalog",
        params={"repo_key": TARGET_A, "include": "executable"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["org_id"] == str(org_id)
    assert len(body["repos"]) == 1
    repo = body["repos"][0]
    assert repo["repo_key"] == TARGET_A
    assert repo["dev_scene"] == SCENE_A
    assert {"fallback_scene", "rules"} <= set(repo["scene_recommend_config"])
    scenes = {s["scene_id"]: s for s in repo["scenes"]}
    assert {SCENE_A, SCENE_A_ALT} <= set(scenes)
    assert scenes[SCENE_A]["is_default"] is True
    assert scenes[SCENE_A]["executable"] is True
    assert "advanced_options" not in resp.text
    assert "do-not-leak" not in resp.text


def test_scene_catalog_rejects_unlinked_repo(client):
    api, _state, _calls, _triggers = client
    org_id = _seed_org()

    resp = api.get(
        f"/api/orgs/{org_id}/dispatch/scene-catalog",
        params={"repo_key": UNLINKED},
    )
    assert resp.status_code == 400, resp.text
    assert resp.json()["detail"]["error_code"] == "INVALID_TARGET"


def test_scene_catalog_marks_unexecutable_scene(client, monkeypatch):
    from src.domains.mgmt import org_dispatch_scene

    api, _state, _calls, _triggers = client
    org_id = _seed_org()
    _seed_repo(ORG_REPO_KEY, org_id)
    _seed_repo(TARGET_A, org_id, dev_scene=SCENE_A, scene_id=SCENE_A)
    _seed_scene(TARGET_A, SCENE_A_ALT)

    def _fake_precheck(scene_id, repo_key="", user=None, explicit=True):
        if scene_id == SCENE_A_ALT:
            return {"ok": False, "problems": [
                {"reason_code": "engine_missing", "message": "SRE 节点 sre 未绑定机器"},
            ]}
        return {"ok": True, "problems": []}

    monkeypatch.setattr(org_dispatch_scene.runtime_public, "precheck_scene", _fake_precheck)
    resp = api.get(
        f"/api/orgs/{org_id}/dispatch/scene-catalog",
        params={"repo_key": TARGET_A, "include": "executable"},
    )
    assert resp.status_code == 200, resp.text
    scenes = {s["scene_id"]: s for s in resp.json()["repos"][0]["scenes"]}
    assert scenes[SCENE_A_ALT]["executable"] is False
    assert scenes[SCENE_A_ALT]["problems"][0]["code"] == "engine_missing"


def test_target_scene_override_wins_over_repo_default(client):
    api, _state, calls, _triggers = client
    org_id = _seed_org()
    _seed_standard(org_id)
    _seed_scene(TARGET_A, SCENE_A_ALT)

    resp = _create(
        api, org_id, [TARGET_A],
        target_scenes={TARGET_A: SCENE_A_ALT},
        scene_reasons={TARGET_A: "需求包含前端改动"},
    )
    assert resp.status_code == 201, resp.text
    assert calls[0]["scene_id"] == SCENE_A_ALT
    body = resp.json()
    assert body["target_scenes"][TARGET_A] == SCENE_A_ALT
    target = body["targets"][0]
    assert target["scene_id"] == SCENE_A_ALT
    assert target["scene_requested"] == SCENE_A_ALT
    assert target["scene_reason"] == "需求包含前端改动"


def test_invalid_target_scene_falls_back_to_repo_default(client):
    api, _state, calls, _triggers = client
    org_id = _seed_org()
    _seed_standard(org_id)

    resp = _create(
        api, org_id, [TARGET_A],
        target_scenes={TARGET_A: "scene-missing"},
    )
    assert resp.status_code == 201, resp.text
    assert calls[0]["scene_id"] == SCENE_A
    target = resp.json()["targets"][0]
    assert target["scene_id"] == SCENE_A
    assert target["scene_requested"] == "scene-missing"


def test_unexecutable_target_scene_falls_back_to_repo_default(client, monkeypatch):
    from src.domains.mgmt import org_dispatch_scene

    api, _state, calls, _triggers = client
    org_id = _seed_org()
    _seed_standard(org_id)
    _seed_scene(TARGET_A, SCENE_A_ALT)

    def _fake_precheck(scene_id, repo_key="", user=None, explicit=True):
        if scene_id == SCENE_A_ALT:
            return {"ok": False, "problems": [
                {"reason_code": "engine_missing", "message": "SRE 节点 sre 未绑定机器"},
            ]}
        return {"ok": True, "problems": []}

    monkeypatch.setattr(org_dispatch_scene.runtime_public, "precheck_scene", _fake_precheck)
    resp = _create(
        api, org_id, [TARGET_A],
        target_scenes={TARGET_A: SCENE_A_ALT},
    )
    assert resp.status_code == 201, resp.text
    assert calls[0]["scene_id"] == SCENE_A
