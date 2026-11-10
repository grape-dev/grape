"""Issue #1245: 建单前场景可用性预校验。

覆盖 6 个 reason_code、3 类豁免、fail-open、enforce=false，以及 API 拦截
返回 400 + problems[] + trace_id 且不触达 GitCode 建单。
"""
import asyncio
import os
import sys
import time

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.api import scene_precheck
from src.session_manager import db, db_lock

_REPO = "gitcode:foo/bar"
_SCENE_ID = "scene_1245"


def _scene(nodes):
    return {"scene_id": _SCENE_ID, "name": "s", "nodes": nodes}


def _patch_scene(monkeypatch, nodes):
    import src.api.pipeline_config as pc

    cfg = _scene(nodes)
    monkeypatch.setattr(pc, "get_pipeline_config", lambda scene_id, repo_key="": cfg)


def _insert_engine(name, *, credential_id=0, status="online", healthy=1, installed=1,
                   scope="global", owner_id=""):
    with db_lock:
        c = db.cursor()
        now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        c.execute(
            "INSERT INTO agent_runtimes "
            "(credential_id, name, status, healthy, installed, last_seen_at, scope, owner_id) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (credential_id, name, status, healthy, installed, now, scope, owner_id))
        rid = c.lastrowid
        db.commit()
    return rid


def _insert_user_credential(name, owner):
    """按生产数据形态写 user 凭据：owner_id 恒为 ''，用户名在 owner_name。"""
    with db_lock:
        c = db.cursor()
        c.execute(
            "INSERT INTO runtime_credentials (token_hash, name, scope, owner_id, owner_name) "
            "VALUES (?,?,?,?,?)",
            (f"hash_{name}_{int(time.time() * 1000)}", f"cred_{name}_{int(time.time() * 1000)}",
             "user", "", owner))
        cid = c.lastrowid
        db.commit()
    return cid


def _cleanup(engine_ids=(), cred_ids=()):
    with db_lock:
        c = db.cursor()
        for rid in engine_ids:
            c.execute("DELETE FROM agent_runtimes WHERE id=?", (rid,))
            c.execute("DELETE FROM engine_capabilities WHERE runtime_id=?", (rid,))
            c.execute("DELETE FROM runtime_health_scores WHERE runtime_id=?", (rid,))
        for cid in cred_ids:
            c.execute("DELETE FROM runtime_credentials WHERE id=?", (cid,))
        db.commit()


def _insert_user_scene(user_id, nodes):
    import json
    with db_lock:
        c = db.cursor()
        c.execute(
            "INSERT INTO scene_market (name, scope, source, source_user_id, nodes_json, is_active) "
            "VALUES (?,?,?,?,?,?)",
            (_SCENE_ID, "user", "user", int(user_id), json.dumps(nodes), 1))
        db.commit()


def _cleanup_user_scene(user_id):
    with db_lock:
        c = db.cursor()
        c.execute("DELETE FROM scene_market WHERE name=? AND scope='user' AND source_user_id=?",
                  (_SCENE_ID, int(user_id)))
        db.commit()


def _node(node_id, *, eids=None, skill="skill-ok", enabled=True, next_="done"):
    return {"id": node_id, "tag": node_id, "skill": skill, "skill_id": 0,
            "enabled": enabled, "next": next_, "engine_runtime_ids": list(eids or [])}


def _run(monkeypatch, nodes, user=None, resolve_result=999):
    _patch_scene(monkeypatch, nodes)
    import src.user.db as udb
    monkeypatch.setattr(udb, "resolve_skill_id", lambda *a, **kw: resolve_result)
    return scene_precheck.precheck_scene(_SCENE_ID, _REPO,
                                         user or {"username": "me", "id": ""})


def test_engine_missing(monkeypatch):
    out = _run(monkeypatch, [_node("coding", eids=[999999])])
    assert out["ok"] is False
    assert {p["reason_code"] for p in out["problems"]} == {"engine_missing"}


def test_partial_engine_missing_not_silently_passed(monkeypatch):
    rid = _insert_engine("partial-engine")
    try:
        out = _run(monkeypatch, [_node("coding", eids=[rid, 999998])])
        assert out["ok"] is False
        missing = [p for p in out["problems"] if p["reason_code"] == "engine_missing"]
        assert missing and missing[0]["engine_ids"] == [999998]
    finally:
        _cleanup([rid])


def test_engine_all_offline(monkeypatch):
    rid = _insert_engine("offline-engine", installed=1, healthy=0, status="offline")
    try:
        out = _run(monkeypatch, [_node("coding", eids=[rid])])
        assert out["ok"] is False
        assert any(p["reason_code"] == "engine_all_offline" for p in out["problems"])
    finally:
        _cleanup([rid])


def test_foreign_personal_engine(monkeypatch):
    # 生产形态：user 凭据 owner_id=''，引擎 owner_id 存数字 grape_accounts.id
    cid = _insert_user_credential("foreign", "other_user")
    rid = _insert_engine("foreign-engine", credential_id=cid, scope="user",
                         owner_id="424242")
    try:
        out = _run(monkeypatch, [_node("coding", eids=[rid])],
                   user={"username": "me", "id": "10001"})
        assert out["ok"] is False
        assert any(p["reason_code"] == "foreign_personal_engine" for p in out["problems"])
    finally:
        _cleanup([rid], [cid])


def test_own_personal_engine_allowed(monkeypatch):
    cid = _insert_user_credential("mine", "me")
    rid = _insert_engine("my-engine", credential_id=cid, scope="user",
                         owner_id="10001")
    try:
        out = _run(monkeypatch, [_node("coding", eids=[rid])],
                   user={"username": "me", "id": "10001"})
        assert out["ok"] is True
        assert not any(p["reason_code"] == "foreign_personal_engine"
                       for p in out["problems"])
    finally:
        _cleanup([rid], [cid])


def _insert_skill_row(scope, owner, sname, agent_type="coding"):
    with db_lock:
        c = db.cursor()
        c.execute(
            "INSERT INTO skill (skill_name, display_name, description, agent_type, "
            "skill_path, file_path, scope, owner_id, is_active) "
            "VALUES (?,?,?,?,?,?,?,?,1)",
            (sname, sname, "desc", agent_type, sname, "", scope, owner))
        db.commit()


def _delete_skill_row(sname):
    with db_lock:
        db.cursor().execute("DELETE FROM skill WHERE skill_name=?", (sname,))
        db.commit()


def test_skill_unresolvable(monkeypatch):
    """#1758：repo 场景引用 user scope 技能 → 单一判官判定不可解析。"""
    import uuid
    rid = _insert_engine("skill-engine")
    sname = f"usk1245_{uuid.uuid4().hex[:8]}"
    _insert_skill_row("user", "42", sname)
    try:
        out = _run(monkeypatch, [_node("coding", eids=[rid], skill=sname)])
        assert out["ok"] is False
        assert any(p["reason_code"] == "skill_unresolvable" for p in out["problems"])
    finally:
        _cleanup([rid])
        _delete_skill_row(sname)


def test_unknown_skill_keeps_legacy_compat(monkeypatch):
    """#1758：不在 skill 表的官方内置包形态技能维持存量兼容放行。"""
    rid = _insert_engine("legacy-skill-engine")
    try:
        out = _run(monkeypatch, [_node("coding", eids=[rid], skill="ghost-skill")])
        assert out["ok"] is True
        assert not any(p["reason_code"] == "skill_unresolvable" for p in out["problems"])
    finally:
        _cleanup([rid])


def test_scene_node_dangling(monkeypatch):
    out = _run(monkeypatch, [_node("coding", next_="ghost")])
    assert out["ok"] is False
    assert any(p["reason_code"] == "scene_node_dangling" for p in out["problems"])


def test_scene_node_duplicate(monkeypatch):
    out = _run(monkeypatch, [_node("coding"), _node("coding")])
    assert out["ok"] is False
    assert any(p["reason_code"] == "scene_node_duplicate" for p in out["problems"])


def test_user_personal_scene_resolved(monkeypatch):
    _insert_user_scene("10001", [_node("coding", eids=[999999], skill="")])
    import src.api.pipeline_config as pc
    # 标准解析未命中，回落 pipelines.json default（fallback）
    monkeypatch.setattr(pc, "get_pipeline_config", lambda *a, **kw: {
        "scene_id": "default", "fallback": True, "nodes": []})
    import src.user.db as udb
    monkeypatch.setattr(udb, "resolve_skill_id", lambda *a, **kw: 999)
    try:
        out = scene_precheck.precheck_scene(_SCENE_ID, _REPO,
                                            {"username": "me", "id": "10001"})
        assert out["ok"] is False
        assert any(p["reason_code"] == "engine_missing" for p in out["problems"])
    finally:
        _cleanup_user_scene("10001")


def test_exempt_non_sre_empty_binding(monkeypatch):
    out = _run(monkeypatch, [_node("coding", eids=[])])
    assert out["ok"] is True
    assert out["problems"] == []


def test_exempt_disabled_node(monkeypatch):
    out = _run(monkeypatch, [_node("coding", eids=[999999], enabled=False)])
    assert out["ok"] is True


def test_exempt_empty_skill(monkeypatch):
    rid = _insert_engine("empty-skill-engine")
    try:
        out = _run(monkeypatch, [_node("coding", eids=[rid], skill="")])
        assert out["ok"] is True
    finally:
        _cleanup([rid])


def test_sre_empty_binding_rejected(monkeypatch):
    out = _run(monkeypatch, [_node("sre", eids=[])])
    assert out["ok"] is False
    assert any(p["reason_code"] == "engine_missing" for p in out["problems"])


def test_fail_open_on_query_exception(monkeypatch):
    import src.api.pipeline_config as pc

    def _boom(*a, **kw):
        raise RuntimeError("db down")

    monkeypatch.setattr(pc, "get_pipeline_config", _boom)
    out = scene_precheck.precheck_scene(_SCENE_ID, _REPO, {"username": "me"})
    assert out["ok"] is True
    assert out["problems"] == []
    assert any("fail-open" in w for w in out["warnings"])


def test_config_missing_uses_default_all_enabled(monkeypatch):
    # 契约：文件缺失 → 默认全开（仍校验），不是静默跳过
    monkeypatch.setattr(scene_precheck, "_PREHECK_CONFIG_PATH",
                        "/nonexistent/scene-precheck.json")
    import src.api.pipeline_config as pc
    monkeypatch.setattr(pc, "get_pipeline_config", lambda *a, **kw: {
        "scene_id": _SCENE_ID, "nodes": [_node("coding", eids=[999999])]})
    out = scene_precheck.precheck_scene(_SCENE_ID, _REPO, {"username": "me"})
    assert out["ok"] is False
    assert any(p["reason_code"] == "engine_missing" for p in out["problems"])


def test_reason_switch_off_does_not_block(monkeypatch):
    monkeypatch.setattr(scene_precheck, "load_precheck_config", lambda: {
        "enforce": True,
        "reasons": {code: code != "engine_missing" for code in scene_precheck.REASON_CODES},
        "fail_open": False,
    })
    _patch_scene(monkeypatch, [_node("coding", eids=[999999], skill="")])
    out = scene_precheck.precheck_scene(_SCENE_ID, _REPO, {"username": "me"})
    assert out["ok"] is True


def test_enforce_false_skips_all(monkeypatch):
    monkeypatch.setattr(scene_precheck, "load_precheck_config",
                        lambda: {"enforce": False, "reasons": {}})
    out = scene_precheck.precheck_scene(_SCENE_ID, _REPO, {"username": "me"})
    assert out["ok"] is True


def test_api_block_returns_400_and_no_gitcode_create(monkeypatch):
    from fastapi import HTTPException
    import src.api.issues_center as ic

    monkeypatch.setattr(ic, "_find_open_issue_by_title", lambda *a, **kw: None)
    monkeypatch.setattr(scene_precheck, "precheck_scene", lambda *a, **kw: {
        "ok": False,
        "problems": [{
            "node_tag": "coding", "engine_ids": [999999],
            "reason_code": "engine_missing", "message": "node engine missing",
            "suggestion": "reselect engine",
        }],
        "warnings": [], "trace_id": "pc_test_trace",
    })

    def _must_not_create(*a, **kw):
        raise AssertionError("GitCode create must not be called when precheck blocks")

    monkeypatch.setattr(ic, "_post_gitcode_issue", _must_not_create)
    body = ic.TaskCreateBody(repo_key="foo/bar", title="t", description="d",
                             scene_id=_SCENE_ID)
    user = {"id": 1, "username": "me", "auth_type": "internal", "token": "internal-token"}
    with pytest.raises(HTTPException) as ei:
        asyncio.run(ic.create_task_via_issue(body, user=user))
    assert ei.value.status_code == 400
    detail = ei.value.detail
    assert detail["problems"][0]["reason_code"] == "engine_missing"
    assert detail["trace_id"] == "pc_test_trace"

def test_webhook_failure_comment_contains_reason_code(monkeypatch):
    import src.api.event_pipeline as ep

    captured = {}

    def _fake_comment(repository, issue_num, comment, token, repo_key="", origin_task=""):
        captured.update(repository=repository, issue_num=issue_num, comment=comment, token=token)

    monkeypatch.setattr(ep, "_gitcode_post_issue_comment_ledgered", _fake_comment)
    monkeypatch.setattr(ep, "_is_github_repo", lambda repo_key: False)
    import src.token_service as ts
    monkeypatch.setattr(ts, "get_repo_bot_token", lambda repo_key: "bot-token")
    ep._notify_scene_precheck_failure(
        _REPO, "#123",
        [{"node_tag": "coding", "reason_code": "engine_all_offline",
          "message": "all engines offline", "suggestion": "start engine"}],
        "pc_trace")
    assert captured["issue_num"] == "123"
    assert "engine_all_offline" in captured["comment"]
    assert "pc_trace" in captured["comment"]
