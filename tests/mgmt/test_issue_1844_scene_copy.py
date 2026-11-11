# -*- coding: utf-8 -*-
"""Issue #1844：场景复制取值矩阵（创建者全量 / 非创建者只复制声明）。

覆盖 Spec 验收 A1–A9：
  A1 创建者复制：custom_fields 值与源相等（含 secret 明文）、work_branch 相等；
  A2 非创建者复制：字段声明全等、value 一律清空、has_value=False；
  A3 user-scope 技能无法通过判官 → 复制不中断，skill="" / skill_id=0 / skill_pending=原名；
  A4 他人私有引擎被剔除（明细可见），SRE 节点无引擎后停用；
  A5 PR 订阅开启但 QA 技能被清空 → 副本关闭 PR 订阅且创建不 400；
  A6 linked_repos 非创建者过滤无权仓库（明细可见），有权仓库原样保留；
  A7 名称缺省/冲突/长度 ≤40；
  A8 回归：POST /scenes 不带 advanced_options 行为不变；
  A9 created_by=当前用户；老场景 created_by 为空按非创建者处理。
"""
import json
import os
import sys
import uuid

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from src.api.main import app  # noqa: E402
from src.api.deps import require_no_session_token  # noqa: E402
from src.api.pipeline_config import (  # noqa: E402
    create_custom_scene, invalidate_scene_cache)
from src.session_manager import db, db_lock  # noqa: E402

client = TestClient(app)

RK = "gitcode:i1844/repo"
REPO_PARAM = "i1844/repo"
LINKED_OK = "gitcode:i1844/linked-ok"
LINKED_NO = "gitcode:i1844/linked-no"

CREATOR = "i1844_creator"
OTHER = "i1844_other"

CREATOR_ID = 918441
OTHER_ID = 918442

_current = {"user": {"id": CREATOR_ID, "username": CREATOR, "auth_type": "jwt"}}


def _as(user_id, username):
    _current["user"] = {"id": user_id, "username": username, "auth_type": "jwt"}
    return _current["user"]


@pytest.fixture(scope="session", autouse=True)
def _ensure_tables():
    from src.api import db_setup
    db_setup.init_database()


@pytest.fixture(scope="session", autouse=True)
def _seed_base():
    _purge_all()
    with db_lock:
        c = db.cursor()
        for rk, owner, name in ((RK, "i1844", "repo"),
                                (LINKED_OK, "i1844", "linked-ok"),
                                (LINKED_NO, "i1844", "linked-no")):
            c.execute(
                "INSERT INTO managed_repos (repo_key, repo_owner, repo_name, full_name) "
                "VALUES (?,?,?,?)", (rk, owner, name, name))
        for uid, uname in ((CREATOR_ID, CREATOR), (OTHER_ID, OTHER)):
            c.execute(
                "INSERT INTO user_repos (user_id, repo_key, owner, name, is_admin, is_join) "
                "VALUES (?,?,?,?,1,1)", (uid, RK, "i1844", "repo"))
        # 仅 OTHER 可见的关联仓库
        c.execute(
            "INSERT INTO user_repos (user_id, repo_key, owner, name, is_admin, is_join) "
            "VALUES (?,?,?,?,1,1)", (OTHER_ID, LINKED_OK, "i1844", "linked-ok"))
        db.commit()
    yield
    _purge_all()


def _purge_all():
    with db_lock:
        c = db.cursor()
        for rk in (RK, LINKED_OK, LINKED_NO):
            c.execute("DELETE FROM devops_pipeline_scene WHERE repo_key=?", (rk,))
            c.execute("DELETE FROM managed_repos WHERE repo_key=?", (rk,))
            c.execute("DELETE FROM user_repos WHERE repo_key=?", (rk,))
        c.execute("DELETE FROM skill WHERE skill_name LIKE 'i1844_%'")
        c.execute("DELETE FROM engine_capabilities WHERE runtime_id IN "
                  "(SELECT id FROM agent_runtimes WHERE name LIKE 'i1844_%')")
        c.execute("DELETE FROM agent_runtimes WHERE name LIKE 'i1844_%'")
        c.execute("DELETE FROM runtime_credentials WHERE name LIKE 'i1844_%'")
        db.commit()
    invalidate_scene_cache("", RK)


@pytest.fixture(autouse=True)
def _clean_scenes():
    with db_lock:
        db.cursor().execute("DELETE FROM devops_pipeline_scene WHERE repo_key=?", (RK,))
        db.commit()
    invalidate_scene_cache("", RK)
    _as(CREATOR_ID, CREATOR)
    yield
    with db_lock:
        db.cursor().execute("DELETE FROM devops_pipeline_scene WHERE repo_key=?", (RK,))
        db.commit()
    invalidate_scene_cache("", RK)


@pytest.fixture(autouse=True)
def _override_auth():
    app.dependency_overrides[require_no_session_token] = lambda: _current["user"]
    yield
    app.dependency_overrides.pop(require_no_session_token, None)


def _mk_skill(scope, owner, agent_type, prefix="i1844_"):
    name = prefix + uuid.uuid4().hex[:8]
    with db_lock:
        c = db.cursor()
        c.execute(
            "INSERT INTO skill (skill_name, display_name, description, agent_type, "
            "skill_path, file_path, scope, owner_id, is_active) "
            "VALUES (?,?,?,?,?,?,?,?,1)",
            (name, name, "i1844", agent_type, name, "", scope, owner))
        db.commit()
        return c.lastrowid, name


def _mk_user_engine(username, tag):
    with db_lock:
        c = db.cursor()
        _uname = "i1844_" + uuid.uuid4().hex[:8]
        c.execute(
            "INSERT INTO runtime_credentials (token_hash, name, device_name, scope, owner_name, status) "
            "VALUES (?,?,?,?,?,?)",
            ("h" + uuid.uuid4().hex, _uname, "dev", "user", username, "active"))
        cid = c.lastrowid
        c.execute(
            "INSERT INTO agent_runtimes (name, display_name, credential_id, status, last_seen_at, "
            "scope, owner_id, installed, healthy) "
            "VALUES (?,?,?,'online',datetime('now'),'user',?,1,1)",
            (_uname, _uname, cid, username))
        rid = c.lastrowid
        c.execute(
            "INSERT INTO engine_capabilities (runtime_id, tag, enabled) VALUES (?,?,1)",
            (rid, tag))
        db.commit()
        return rid


def _mk_scene(scene_id, nodes, created_by=CREATOR, display_name="", pr_sub=0,
              custom_fields=None, linked_repos=None, work_branch="feature-x"):
    create_custom_scene(
        scene_id=scene_id, name=scene_id, description="i1844 source",
        display_name=display_name or scene_id, repo_key=RK, nodes=nodes,
        created_by=created_by, pr_subscription_enabled=pr_sub,
        linked_repos=linked_repos or [])
    opts = {
        "work_branch": work_branch,
        "custom_fields": custom_fields or [],
        "linked_repos": linked_repos or [],
    }
    with db_lock:
        c = db.cursor()
        c.execute(
            "UPDATE devops_pipeline_scene SET advanced_options=? "
            "WHERE scene_id=? AND repo_key=?",
            (json.dumps(opts, ensure_ascii=False), scene_id, RK))
        db.commit()
    invalidate_scene_cache(scene_id, RK)


def _scene_row(scene_id):
    with db_lock:
        c = db.cursor()
        c.execute(
            "SELECT scene_id, name, display_name, nodes_json, advanced_options, "
            "created_by, pr_subscription_enabled, linked_repos "
            "FROM devops_pipeline_scene WHERE scene_id=? AND repo_key=? LIMIT 1",
            (scene_id, RK))
        return c.fetchone()


def _copy(scene_id, name=None, as_non_creator=False):
    _as(OTHER_ID, OTHER) if as_non_creator else _as(CREATOR_ID, CREATOR)
    body = {} if name is None else {"name": name}
    return client.post(f"/api/scenes/{scene_id}/copy",
                       params={"repo": REPO_PARAM}, json=body)


FIELD_DECL = [
    {"name": "PLAIN_ENV", "value": "prod", "is_secret": False, "scope": ["all"]},
    {"name": "DEPLOY_KEY", "value": "s3cr3t-1844", "is_secret": True, "scope": ["sre"]},
]


class TestCreatorCopy:
    """A1 / A9 创建者全量复制。"""

    def test_creator_copies_values_including_secret(self):
        _mk_scene("src-a1", [{"id": "coding", "tag": "coding", "skill": "",
                              "enabled": True, "engine_runtime_ids": []}],
                  custom_fields=FIELD_DECL)
        r = _copy("src-a1")
        assert r.status_code == 200, r.text
        data = r.json()
        assert data["is_creator"] is True
        assert data["redacted_fields"] == []
        row = _scene_row(data["scene_id"])
        assert row is not None
        opts = json.loads(row[4])
        assert opts["work_branch"] == "feature-x"
        by_name = {f["name"]: f for f in opts["custom_fields"]}
        assert by_name["PLAIN_ENV"]["value"] == "prod"
        assert by_name["DEPLOY_KEY"]["value"] == "s3cr3t-1844"
        assert by_name["DEPLOY_KEY"]["is_secret"] is True
        assert [f["name"] for f in opts["custom_fields"]] == ["PLAIN_ENV", "DEPLOY_KEY"]

    def test_creator_preserves_source_scene_type(self):
        _mk_scene("src-type", [])
        if "scene_type" not in {
                r[1] for r in db.cursor().execute(
                    "PRAGMA table_info(devops_pipeline_scene)")}:
            pytest.skip("scene_type column missing")
        with db_lock:
            c = db.cursor()
            c.execute("UPDATE devops_pipeline_scene SET scene_type=? "
                      "WHERE scene_id=? AND repo_key=?", ("custom-type", "src-type", RK))
            db.commit()
        invalidate_scene_cache("src-type", RK)
        r = _copy("src-type")
        assert r.status_code == 200, r.text
        with db_lock:
            row = db.cursor().execute(
                "SELECT scene_type FROM devops_pipeline_scene "
                "WHERE scene_id=? AND repo_key=? LIMIT 1",
                (r.json()["scene_id"], RK)).fetchone()
        assert row is not None and row[0] == "custom-type"

    def test_created_by_is_current_user(self):
        _mk_scene("src-a9", [])
        r = _copy("src-a9")
        assert r.status_code == 200, r.text
        row = _scene_row(r.json()["scene_id"])
        assert row[5] == CREATOR

    def test_empty_created_by_treated_as_non_creator(self):
        _mk_scene("src-a9-empty", [], created_by="", custom_fields=FIELD_DECL)
        r = _copy("src-a9-empty", as_non_creator=False)
        assert r.status_code == 200, r.text
        data = r.json()
        assert data["is_creator"] is False
        assert "PLAIN_ENV" in data["redacted_fields"]


class TestNonCreatorCopy:
    """A2 非创建者只复制字段声明。"""

    def test_values_cleared_declarations_kept(self):
        _mk_scene("src-a2", [], custom_fields=FIELD_DECL)
        r = _copy("src-a2", as_non_creator=True)
        assert r.status_code == 200, r.text
        data = r.json()
        assert data["is_creator"] is False
        assert set(data["redacted_fields"]) == {"PLAIN_ENV", "DEPLOY_KEY"}
        opts = json.loads(_scene_row(data["scene_id"])[4])
        assert opts["work_branch"] == "feature-x"
        assert [f["name"] for f in opts["custom_fields"]] == ["PLAIN_ENV", "DEPLOY_KEY"]
        assert [f["is_secret"] for f in opts["custom_fields"]] == [False, True]
        assert [f["scope"] for f in opts["custom_fields"]] == [["all"], ["sre"]]
        assert all(f["value"] == "" for f in opts["custom_fields"])

        # 读接口口径：非创建者副本 has_value 全为 False
        g = client.get(f"/api/scenes/{data['scene_id']}/advanced-options",
                       params={"repo": REPO_PARAM})
        assert g.status_code == 200, g.text
        assert all(f["has_value"] is False for f in g.json()["custom_fields"])
        assert all(f["value"] == "" for f in g.json()["custom_fields"])


class TestSkillClearing:
    """A3 / A5 技能清洗与 PR 订阅降级。"""

    def test_user_scope_skill_cleared_not_abort(self):
        sid, sname = _mk_skill("user", "42", "coding")
        _mk_scene("src-a3", [{"id": "coding", "tag": "coding", "skill": sname,
                              "skill_id": sid, "enabled": True,
                              "engine_runtime_ids": []}])
        r = _copy("src-a3")
        assert r.status_code == 200, r.text
        data = r.json()
        assert data["cleared_skills"] == [{"node_id": "coding", "skill": sname}]
        nodes = json.loads(_scene_row(data["scene_id"])[3])
        node = nodes[0]
        assert node["skill"] == ""
        assert node["skill_id"] == 0
        assert node["skill_pending"] == sname

    def test_pr_subscription_disabled_when_qa_skill_cleared(self):
        _sid, sname = _mk_skill("user", "42", "qa")
        _mk_scene("src-a5", [
            {"id": "qa", "tag": "qa", "skill": sname, "enabled": True,
             "engine_runtime_ids": []},
        ], pr_sub=1)
        r = _copy("src-a5")
        assert r.status_code == 200, r.text
        data = r.json()
        assert data["pr_subscription_disabled"] is True
        row = _scene_row(data["scene_id"])
        assert int(row[6] or 0) == 0


class TestEngineCleaning:
    """A4 引擎过滤 + SRE 停用。"""

    def test_foreign_engine_dropped_sre_disabled(self):
        creator_eng = _mk_user_engine(CREATOR, "coding")
        creator_sre_eng = _mk_user_engine(CREATOR, "sre")
        other_eng = _mk_user_engine(OTHER, "coding")
        _mk_scene("src-a4", [
            {"id": "coding", "tag": "coding", "skill": "", "enabled": True,
             "engine_runtime_ids": [creator_eng, other_eng]},
            {"id": "sre", "tag": "sre", "skill": "", "enabled": True,
             "engine_runtime_ids": [creator_sre_eng]},
        ])
        r = _copy("src-a4", as_non_creator=True)
        assert r.status_code == 200, r.text
        data = r.json()
        dropped_ids = {d["engine_runtime_id"] for d in data["dropped_engines"]}
        assert creator_eng in dropped_ids
        assert creator_sre_eng in dropped_ids
        assert other_eng not in dropped_ids

        nodes = {n["id"]: n for n in json.loads(_scene_row(data["scene_id"])[3])}
        assert nodes["coding"]["engine_runtime_ids"] == [other_eng]
        assert nodes["sre"]["engine_runtime_ids"] == []
        assert nodes["sre"]["enabled"] is False


class TestLinkedRepos:
    """A6 关联仓库按访问权限过滤。"""

    def test_linked_repos_filtered_for_non_creator(self):
        _mk_scene("src-a6", [], linked_repos=[
            {"repo_key": LINKED_OK, "branch": "dev"},
            {"repo_key": LINKED_NO, "branch": "main"},
        ], custom_fields=[])
        r = _copy("src-a6", as_non_creator=True)
        assert r.status_code == 200, r.text
        data = r.json()
        assert data["dropped_linked_repos"] == [LINKED_NO]
        opts = json.loads(_scene_row(data["scene_id"])[4])
        assert opts["linked_repos"] == [{"repo_key": LINKED_OK, "branch": "dev"}]
        row = _scene_row(data["scene_id"])
        assert json.loads(row[7]) == [{"repo_key": LINKED_OK, "branch": "dev"}]


class TestName:
    """A7 副本命名。"""

    def test_default_and_conflict_suffix(self):
        _mk_scene("src-a7", [], display_name="Scene Seven")
        r1 = _copy("src-a7")
        assert r1.status_code == 200, r1.text
        assert r1.json()["name"] == "Scene Seven-copy"

        r2 = _copy("src-a7", name="Scene Seven-copy")
        assert r2.status_code == 200, r2.text
        assert r2.json()["name"] == "Scene Seven-copy2"

    def test_name_length_capped_at_40(self):
        _mk_scene("src-a7-long", [])
        long_name = "N" * 40
        r1 = _copy("src-a7-long", name=long_name)
        assert r1.status_code == 200, r1.text
        assert len(r1.json()["name"]) == 40
        r2 = _copy("src-a7-long", name=long_name)
        assert r2.status_code == 200, r2.text
        assert len(r2.json()["name"]) == 40
        assert r2.json()["name"].endswith("2")


class TestRegression:
    """A8 既有创建/高级选项路径不变。"""

    def test_create_scene_without_advanced_options(self):
        r = client.post("/api/scenes", params={"repo": REPO_PARAM}, json={
            "name": "plain-scene",
            "description": "regression",
            "scope": "repo",
            "nodes": [{"id": "coding", "tag": "coding", "skill": "",
                       "enabled": True, "engine_runtime_ids": []}],
        })
        assert r.status_code == 200, r.text
        scene_id = r.json()["scene_id"]
        g = client.get(f"/api/scenes/{scene_id}/advanced-options",
                       params={"repo": REPO_PARAM})
        assert g.status_code == 200, g.text
        assert g.json()["work_branch"] == "main"
        assert g.json()["custom_fields"] == []

    def test_create_scene_ignores_advanced_options(self):
        """POST /scenes 不开放 advanced_options，防止绕过 PUT 校验落库（存储型 XSS）。"""
        r = client.post("/api/scenes", params={"repo": REPO_PARAM}, json={
            "name": "xss-scene",
            "description": "xss",
            "scope": "repo",
            "nodes": [{"id": "coding", "tag": "coding", "skill": "",
                       "enabled": True, "engine_runtime_ids": []}],
            "advanced_options": {
                "work_branch": "evil",
                "custom_fields": [{
                    "name": "<img src=x onerror=alert(1)>",
                    "value": "<script>alert(1)</script>",
                }],
            },
        })
        assert r.status_code == 200, r.text
        g = client.get(f"/api/scenes/{r.json()['scene_id']}/advanced-options",
                       params={"repo": REPO_PARAM})
        assert g.status_code == 200, g.text
        assert g.json()["work_branch"] == "main"
        assert g.json()["custom_fields"] == []

    def test_copy_requires_repo_access(self):
        """复制目标仓库必须属于当前用户（check_repo_access）。"""
        _mk_scene("src-access", [])
        _as(999999, "i1844_nobody")
        r = client.post("/api/scenes/src-access/copy",
                        params={"repo": REPO_PARAM}, json={})
        assert r.status_code == 403, r.text

    def test_source_invalid_json_reports_parse_error(self):
        """老场景 advanced_options 非法 JSON → 按空配置处理并计入响应明细。"""
        _mk_scene("src-bad-json", [])
        with db_lock:
            c = db.cursor()
            c.execute(
                "UPDATE devops_pipeline_scene SET advanced_options=? "
                "WHERE scene_id=? AND repo_key=?", ("{not-json", "src-bad-json", RK))
            db.commit()
        invalidate_scene_cache("src-bad-json", RK)
        r = _copy("src-bad-json")
        assert r.status_code == 200, r.text
        assert r.json()["source_config_parse_error"] is True
