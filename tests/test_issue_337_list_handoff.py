"""Issue #337 — 需求中心列表模式「移交到其他流水线」后端链路测试。

覆盖（对应 docs/issue-337/task.md #1-3）：
- get_handoff_targets：排除当前流水线 / 仅含 vod 节点 / 空列表语义
- GET /api/issues/{issue_id}/handoff/targets（targets_issue）：权限过滤（check_repo_access 403）
- POST /api/issues/{issue_id}/handoff（handoff_issue）：
  * 成功：cross_scene_handoff + a2a_event 审计事件落库、issue.scene_id/stage 回写目标场景
  * 目标=当前场景 → 409（duplicate_current）
  * 目标不在可移交列表（含无 vod）→ 400/404
  * 30s 幂等防连点 → 409
  * 需求不存在 → 404
  * 无权限（jwt 用户无该仓库）→ 403

DB 注意：本文件在 conftest 之后导入（conftest 已把 DEVOPS_DB_PATH 锁定为共享测试库
data/test_sessions.db），模块级覆盖无效——通过 `_ensure_db()` 惰性重定向到独立库
（与 test_a2a_redo.py 先导入生效的机制不同，这里在 fixture 内切换后重新 init）。
"""
import os
import sys
import json
import time
import asyncio

ROOT = os.path.normpath(os.path.join(os.path.dirname(__file__), ".."))
_DB_PATH = os.path.join(ROOT, "data", "test_issue_337_list_handoff.db")

import pytest
from fastapi import HTTPException


_DB_READY = {"done": False}


def _ensure_db():
    """惰性切换到独立测试库（幂等）：conftest 已 import src.* 后 env 覆盖不回溯，
    因此通过 reload 重新绑定 session_manager/db_setup 的连接对象。"""
    if os.environ.get("DEVOPS_DB_PATH") == _DB_PATH and _DB_READY["done"]:
        return
    os.environ["DEVOPS_DB_PATH"] = _DB_PATH
    os.environ["DEVOPS_DB_BACKEND"] = "sqlite"
    for _k in ("DEVOPS_DB_HOST", "DEVOPS_DB_USER", "DEVOPS_DB_PASSWORD", "DEVOPS_DB_NAME", "DEVOPS_DB_PORT"):
        os.environ.pop(_k, None)
    import importlib
    import src.session_manager as _sm
    import src.config as _cfg
    importlib.reload(_sm)
    _cfg.DB_PATH = _DB_PATH
    import src.api.db_setup as _dbs
    importlib.reload(_dbs)
    _dbs.init_database()
    _DB_READY["done"] = True


_ensure_db()

from src.session_manager import db, db_lock  # noqa: E402
from src.api.routes.issues import targets_issue, handoff_issue, IssueHandoffBody  # noqa: E402
from src.api.issues_center import get_handoff_targets  # noqa: E402

REPO = "gitcode:org/test-337"


@pytest.fixture(autouse=True)
def _clean():
    _ensure_db()
    with db_lock:
        c = db.cursor()
        c.execute("DELETE FROM issue WHERE repo_key=?", (REPO,))
        c.execute("DELETE FROM events WHERE repo_key=?", (REPO,))
        c.execute("DELETE FROM devops_pipeline_scene WHERE repo_key=?", (REPO,))
        c.execute("DELETE FROM event_active_guard WHERE repo_key=?", (REPO,))
        db.commit()
    from src.user.db import set_meta_value
    set_meta_value("issue-handoff:200", "")
    set_meta_value("issue-handoff:201", "")
    yield
    with db_lock:
        c = db.cursor()
        c.execute("DELETE FROM issue WHERE repo_key=?", (REPO,))
        c.execute("DELETE FROM events WHERE repo_key=?", (REPO,))
        c.execute("DELETE FROM devops_pipeline_scene WHERE repo_key=?", (REPO,))
        c.execute("DELETE FROM event_active_guard WHERE repo_key=?", (REPO,))
        db.commit()
    set_meta_value("issue-handoff:200", "")
    set_meta_value("issue-handoff:201", "")


def _set_issue(number, scene_id="source-scene", assignee=""):
    """建档并显式指定主键 id=number，使端点 path 参数 {issue_id} 与行 id 对齐。"""
    with db_lock:
        c = db.cursor()
        c.execute(
            "INSERT INTO issue (id, user_id, assignee, repo_key, issue_number, title, status, scene_id) "
            "VALUES (?, 0, ?, ?, ?, ?, 'processing', ?)",
            (number, assignee, REPO, number, f"issue-{number}", scene_id))
        db.commit()
    return number


def _set_scene(scene_id, nodes, display_name=""):
    with db_lock:
        c = db.cursor()
        c.execute(
            "INSERT INTO devops_pipeline_scene (scene_id, name, repo_key, nodes_json, is_active, display_name) "
            "VALUES (?,?,?,?,1,?)",
            (scene_id, scene_id, REPO, json.dumps(nodes), display_name or scene_id))
        db.commit()


def _user(auth="jwt"):
    return {"id": 1, "username": "tester", "auth_type": auth}


def _seed_scenes():
    _set_scene("source-scene", [{"id": "vod"}, {"id": "issue"}, {"id": "coding"}])
    _set_scene("target-scene", [{"id": "vod"}, {"id": "coding"}], display_name="目标流水线A")
    _set_scene("no-vod-scene", [{"id": "qa"}, {"id": "sre"}])
    _set_scene("another-scene", [{"id": "vod"}])


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


# ===== 领域函数：get_handoff_targets =====

def test_targets_excludes_current_scene():
    _seed_scenes()
    out = get_handoff_targets(REPO, 200, "source-scene")
    ids = [t["id"] for t in out["targets"]]
    assert "source-scene" not in ids, "必须排除当前流水线"
    assert "target-scene" in ids
    assert "another-scene" in ids


def test_targets_only_vod_scenes():
    _seed_scenes()
    out = get_handoff_targets(REPO, 200, "source-scene")
    ids = [t["id"] for t in out["targets"]]
    assert "no-vod-scene" not in ids, "不含 vod 节点的场景不可作为目标"


def test_targets_fields():
    _seed_scenes()
    out = get_handoff_targets(REPO, 200, "source-scene")
    t = next(x for x in out["targets"] if x["id"] == "target-scene")
    assert t["display_name"] == "目标流水线A"
    assert "work_branch" in t
    assert out["current_scene_id"] == "source-scene"


def test_targets_empty_when_only_current():
    _set_scene("source-scene", [{"id": "vod"}])
    out = get_handoff_targets(REPO, 200, "source-scene")
    assert out["targets"] == []


def test_targets_current_scene_defaults_to_default():
    """无场景记录的需求 → current_scene_id=default，default 亦被排除。"""
    _set_scene("default", [{"id": "vod"}])
    _set_scene("target-scene", [{"id": "vod"}])
    out = get_handoff_targets(REPO, 200, "")
    ids = [t["id"] for t in out["targets"]]
    assert "default" not in ids
    assert "target-scene" in ids


# ===== GET 端点：targets_issue =====

def test_get_targets_endpoint_ok():
    from unittest.mock import patch
    _set_issue(200, "source-scene")
    _seed_scenes()
    with patch("src.api.deps.user_db.get_managed_repos",
               return_value=[{"repo_key": REPO}]):
        res = _run(targets_issue(200, _user()))
    assert res["issue_number"] == 200
    assert res["current_scene_id"] == "source-scene"
    ids = [t["id"] for t in res["targets"]]
    assert "source-scene" not in ids
    assert "target-scene" in ids


def test_get_targets_endpoint_403_no_permission():
    from unittest.mock import patch
    _set_issue(200, "source-scene")
    _seed_scenes()
    with patch("src.api.deps.user_db.get_managed_repos", return_value=[]):
        with pytest.raises(HTTPException) as ei:
            _run(targets_issue(200, _user()))
    assert ei.value.status_code == 403


def test_get_targets_endpoint_404_missing_issue():
    with pytest.raises(HTTPException) as ei:
        _run(targets_issue(9999, _user()))
    assert ei.value.status_code == 404


# ===== POST 端点：handoff_issue =====

def test_handoff_success_creates_events_and_updates_issue():
    from unittest.mock import patch
    _set_issue(200, "source-scene", assignee="old_user")
    _seed_scenes()
    body = IssueHandoffBody(target_scene_id="target-scene")
    with patch("src.api.deps.user_db.get_managed_repos",
               return_value=[{"repo_key": REPO}]):
        res = _run(handoff_issue(200, body, _user()))
    assert res.get("status") == "ok", res
    assert res.get("seq", 0) > 0
    assert res.get("cross_pipeline") is True
    # 审计事件落库
    with db_lock:
        c = db.cursor()
        c.execute("SELECT event_type FROM events WHERE repo_key=? AND issue_id='#200'", (REPO,))
        types = [r[0] for r in c.fetchall()]
    assert "cross_scene_handoff" in types
    assert "a2a_event" in types
    # issue.scene_id 回写目标场景、stage=vod（复用 _cross_pipeline_handoff 语义）
    with db_lock:
        c = db.cursor()
        c.execute("SELECT scene_id, stage FROM issue WHERE repo_key=? AND issue_number=200", (REPO,))
        row = c.fetchone()
    assert row[0] == "target-scene"
    assert row[1] == "vod"


def test_handoff_rejects_current_scene():
    from unittest.mock import patch
    _set_issue(200, "source-scene")
    _seed_scenes()
    body = IssueHandoffBody(target_scene_id="source-scene")
    with patch("src.api.deps.user_db.get_managed_repos",
               return_value=[{"repo_key": REPO}]):
        with pytest.raises(HTTPException) as ei:
            _run(handoff_issue(200, body, _user()))
    assert ei.value.status_code == 409


def test_handoff_rejects_scene_not_in_targets():
    from unittest.mock import patch
    _set_issue(200, "source-scene")
    _seed_scenes()
    # no-vod-scene 不在可移交列表内（无 vod 节点）→ 404（不存在或不可移交）
    body = IssueHandoffBody(target_scene_id="no-vod-scene")
    with patch("src.api.deps.user_db.get_managed_repos",
               return_value=[{"repo_key": REPO}]):
        with pytest.raises(HTTPException) as ei:
            _run(handoff_issue(200, body, _user()))
    assert ei.value.status_code == 404


def test_handoff_rejects_unknown_scene():
    from unittest.mock import patch
    _set_issue(200, "source-scene")
    _seed_scenes()
    body = IssueHandoffBody(target_scene_id="ghost-scene")
    with patch("src.api.deps.user_db.get_managed_repos",
               return_value=[{"repo_key": REPO}]):
        with pytest.raises(HTTPException) as ei:
            _run(handoff_issue(200, body, _user()))
    assert ei.value.status_code == 404


def test_handoff_30s_idempotent_guard():
    from unittest.mock import patch
    from src.user.db import set_meta_value
    _set_issue(200, "source-scene")
    _seed_scenes()
    # 预置 5 秒前的幂等键 → 409
    set_meta_value("issue-handoff:200", str(time.time() - 5))
    body = IssueHandoffBody(target_scene_id="target-scene")
    with patch("src.api.deps.user_db.get_managed_repos",
               return_value=[{"repo_key": REPO}]):
        with pytest.raises(HTTPException) as ei:
            _run(handoff_issue(200, body, _user()))
    assert ei.value.status_code == 409


def test_handoff_404_missing_issue():
    body = IssueHandoffBody(target_scene_id="target-scene")
    with pytest.raises(HTTPException) as ei:
        _run(handoff_issue(9999, body, _user()))
    assert ei.value.status_code == 404


def test_handoff_403_no_permission():
    from unittest.mock import patch
    _set_issue(200, "source-scene")
    _seed_scenes()
    body = IssueHandoffBody(target_scene_id="target-scene")
    with patch("src.api.deps.user_db.get_managed_repos", return_value=[]):
        with pytest.raises(HTTPException) as ei:
            _run(handoff_issue(200, body, _user()))
    assert ei.value.status_code == 403


def test_handoff_requires_target_scene_id():
    from unittest.mock import patch
    _set_issue(200, "source-scene")
    _seed_scenes()
    body = IssueHandoffBody(target_scene_id="")
    with patch("src.api.deps.user_db.get_managed_repos",
               return_value=[{"repo_key": REPO}]):
        with pytest.raises(HTTPException) as ei:
            _run(handoff_issue(200, body, _user()))
    assert ei.value.status_code == 400


def test_handoff_skipped_duplicate_transparent():
    """目标场景已有 running a2a_event → _cross_pipeline_handoff 拦截，端点透传 skipped。"""
    from unittest.mock import patch
    from src.api.event_store import append_event
    _set_issue(200, "source-scene")
    _seed_scenes()
    append_event(
        repo_key=REPO, event_type="a2a_event", title="existing",
        issue_id="#200", agent_type="vod", source="a2a",
        status="running", scene_id="target-scene",
    )
    body = IssueHandoffBody(target_scene_id="target-scene")
    with patch("src.api.deps.user_db.get_managed_repos",
               return_value=[{"repo_key": REPO}]):
        res = _run(handoff_issue(200, body, _user()))
    assert res.get("skipped") == "duplicate_handoff", res
