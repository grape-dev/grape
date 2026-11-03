"""Issue #654：切换 credential scope 触发引擎重新注册（引擎 id 变更 + priority 静默重置）。

修复后语义：
1. register upsert 键收敛为 (credential_id, lower(name))：scope/owner_id/org_id/repo_key
   是凭据派生属性，命中既有行原地同步 → scope repo→user→repo 往返后引擎 id 不变；
2. engine_capabilities（priority/标签）与 tags_user_set 往返后保留原值（不回默认 50/'all'）；
3. 引擎行替换（历史重复行去重）时 capabilities/tags_user_set 迁移到存活行，且
   场景 engine_runtime_ids / devops_agent_session / chat_agent_session / autopilot_tasks
   的引擎 id 引用被 remap（不悬空）；
4. scope 切换 / 引擎替换输出结构化变更事件（engine_change_event 日志，含 cid、
   影响引擎数、旧→新 id 映射）。

回归覆盖：#371（scope 切换 org_id/owner_id 口径不回退，见 test_issue_371 单测）。
"""
import os
import sys
import json
import hashlib
import types

ROOT = os.path.normpath(os.path.join(os.path.dirname(__file__), ".."))
os.environ.setdefault("DEVOPS_DB_PATH", os.path.join(ROOT, "data", "test_issue_654.db"))
os.environ["DEVOPS_DB_BACKEND"] = "sqlite"
for _k in ("DEVOPS_DB_HOST", "DEVOPS_DB_PORT", "DEVOPS_DB_USER", "DEVOPS_DB_PASSWORD", "DEVOPS_DB_NAME"):
    os.environ.pop(_k, None)

import pytest

from src.api.db_setup import init_database
from src.session_manager import db, db_lock

init_database()

_ORG_PERSONAL = 937201
_ORG_REPO = 937202
_USER_ID = 778
_USER_NAME = "t654user"
_REPO_FULL = "t654owner/blog-team"
_REPO_KEY = "gitcode:t654owner/blog-team"


@pytest.fixture(autouse=True)
def _seed():
    with db_lock:
        c = db.cursor()
        c.execute("DELETE FROM runtime_credentials WHERE name LIKE 't654-%'")
        c.execute("DELETE FROM agent_runtimes WHERE credential_id IN (SELECT id FROM runtime_credentials WHERE name LIKE 't654-%')")
        c.execute("DELETE FROM agent_runtimes WHERE name LIKE 't654-%'")
        c.execute("DELETE FROM engine_capabilities WHERE runtime_id NOT IN (SELECT id FROM agent_runtimes)")
        c.execute("DELETE FROM organization WHERE id IN (?,?)", (_ORG_PERSONAL, _ORG_REPO))
        c.execute("DELETE FROM managed_repos WHERE repo_key=?", (_REPO_KEY,))
        c.execute("DELETE FROM grape_accounts WHERE id=?", (_USER_ID,))
        c.execute("DELETE FROM devops_pipeline_scene WHERE scene_id LIKE 't654-%'")
        c.execute("DELETE FROM autopilot_tasks WHERE name LIKE 't654-%'")
        c.execute("DELETE FROM devops_agent_session WHERE repo_key=?", (_REPO_KEY,))
        c.execute("DELETE FROM chat_agent_session WHERE id LIKE 't654-%'")
        c.execute("INSERT INTO organization (id, name, slug, owner_id, is_personal) VALUES (?,?,?,0,?)",
                  (_ORG_PERSONAL, "t654-personal", "t654-personal", 1))
        c.execute("INSERT INTO organization (id, name, slug, owner_id, is_personal) VALUES (?,?,?,0,0)",
                  (_ORG_REPO, "t654-repo-org", "t654-repo-org"))
        c.execute("INSERT OR IGNORE INTO grape_accounts (id, username) VALUES (?,?)", (_USER_ID, _USER_NAME))
        c.execute(
            "INSERT OR REPLACE INTO managed_repos (repo_key, repo_owner, repo_name, full_name, org_id) "
            "VALUES (?,?,?,?,?)",
            (_REPO_KEY, "t654owner", "blog-team", _REPO_FULL, _ORG_REPO))
        db.commit()
    yield
    with db_lock:
        c = db.cursor()
        c.execute("DELETE FROM runtime_credentials WHERE name LIKE 't654-%'")
        c.execute("DELETE FROM agent_runtimes WHERE name LIKE 't654-%'")
        c.execute("DELETE FROM engine_capabilities WHERE runtime_id NOT IN (SELECT id FROM agent_runtimes)")
        c.execute("DELETE FROM organization WHERE id IN (?,?)", (_ORG_PERSONAL, _ORG_REPO))
        c.execute("DELETE FROM managed_repos WHERE repo_key=?", (_REPO_KEY,))
        c.execute("DELETE FROM grape_accounts WHERE id=?", (_USER_ID,))
        c.execute("DELETE FROM devops_pipeline_scene WHERE scene_id LIKE 't654-%'")
        c.execute("DELETE FROM autopilot_tasks WHERE name LIKE 't654-%'")
        c.execute("DELETE FROM devops_agent_session WHERE repo_key=?", (_REPO_KEY,))
        c.execute("DELETE FROM chat_agent_session WHERE id LIKE 't654-%'")
        db.commit()


def _user():
    return {"id": _USER_ID, "username": _USER_NAME, "auth_type": "jwt", "org_id": _ORG_PERSONAL}


def _create_cred(scope="repo", name="t654-worker"):
    with db_lock:
        c = db.cursor()
        c.execute(
            "INSERT INTO runtime_credentials (token_hash, name, device_name, scope, owner_name, owner_id, repo_key, org_id, status) "
            "VALUES (?,?,?,?,?,?,?,?, 'active')",
            (hashlib.sha256(name.encode()).hexdigest(), name, name,
             scope, _USER_NAME, _REPO_KEY if scope == "repo" else "", _REPO_KEY if scope == "repo" else "",
             _ORG_REPO if scope == "repo" else _ORG_PERSONAL))
        cid = c.lastrowid
        db.commit()
        return cid


class _FakeRequest:
    headers = {}
    client = None


def _register(cid, engines=("t654-opencode",)):
    from src.api.routes.runtime import runtime_register, RuntimeRegBody
    body = RuntimeRegBody(device_name="t654-dev", runtimes=[{"name": n, "version": "v1"} for n in engines])
    return runtime_register(body, _FakeRequest(), credential_id=cid)


def _set_priority(rid, priority=100, tag="coding", tags_user_set=1):
    with db_lock:
        c = db.cursor()
        c.execute("DELETE FROM engine_capabilities WHERE runtime_id=?", (rid,))
        c.execute(
            "INSERT INTO engine_capabilities (runtime_id, tag, max_concurrent, priority, enabled, created_at, updated_at) "
            "VALUES (?,?,?,?,1,datetime('now'),datetime('now'))",
            (rid, tag, 1, priority))
        c.execute("UPDATE agent_runtimes SET tags_user_set=? WHERE id=?", (tags_user_set, rid))
        db.commit()


def _get_priority(rid):
    with db_lock:
        c = db.cursor()
        r = c.execute("SELECT priority, tag FROM engine_capabilities WHERE runtime_id=?", (rid,)).fetchone()
        return (r[0], r[1]) if r else None


class TestScopeRoundtripEngineStability:
    """验收 1/2：scope repo→user→repo 往返后引擎 id / priority / 标签不变。"""

    def test_roundtrip_keeps_engine_id_and_priority(self):
        from src.api.routes.settings import change_credential_scope, CredScopeBody
        cid = _create_cred(scope="repo")
        rid = _register(cid)["runtimes"][0]["id"]
        _set_priority(rid, priority=100)
        # repo → user
        change_credential_scope(cid, CredScopeBody(scope="user"), user=_user())
        r2 = _register(cid)
        assert r2["runtimes"][0]["id"] == rid, "切 user 后重注册应复用既有行（id 不变）"
        # user → repo（往返）
        change_credential_scope(cid, CredScopeBody(scope="repo", repo_key=_REPO_FULL), user=_user())
        r3 = _register(cid)
        assert r3["runtimes"][0]["id"] == rid, "切回 repo 后重注册应复用既有行（id 不变）"
        assert _get_priority(rid) == (100, "coding"), f"priority/标签应保留原值，实际={_get_priority(rid)}"
        with db_lock:
            cnt = db.cursor().execute(
                "SELECT COUNT(*) FROM agent_runtimes WHERE credential_id=? AND lower(name)='t654-opencode'",
                (cid,)).fetchone()[0]
        assert cnt == 1, f"往返后不应产生重复引擎行，实际 {cnt} 行"

    def test_roundtrip_keeps_tags_user_set(self):
        from src.api.routes.settings import change_credential_scope, CredScopeBody
        cid = _create_cred(scope="repo")
        rid = _register(cid)["runtimes"][0]["id"]
        _set_priority(rid, priority=100, tags_user_set=1)
        change_credential_scope(cid, CredScopeBody(scope="user"), user=_user())
        change_credential_scope(cid, CredScopeBody(scope="repo", repo_key=_REPO_FULL), user=_user())
        with db_lock:
            tus = db.cursor().execute("SELECT tags_user_set FROM agent_runtimes WHERE id=?", (rid,)).fetchone()[0]
        assert tus == 1, "tags_user_set（用户手动配置标记）往返后应保留"

    def test_register_syncs_derived_attrs_in_place(self, caplog):
        """切 scope 后引擎行未同步（脏行）时，register 原地拉齐派生属性并输出事件。"""
        cid = _create_cred(scope="repo")
        rid = _register(cid)["runtimes"][0]["id"]
        # 模拟引擎行 scope/owner 漂移（旧行为遗留脏数据）
        with db_lock:
            db.cursor().execute(
                "UPDATE agent_runtimes SET scope='user', owner_id='' WHERE id=?", (rid,))
            db.commit()
        with caplog.at_level("INFO"):
            _register(cid)
        with db_lock:
            row = db.cursor().execute(
                "SELECT scope, owner_id, repo_key FROM agent_runtimes WHERE id=?", (rid,)).fetchone()
        assert row[0] == "repo" and row[1] == _REPO_KEY, f"register 应原地同步派生属性，实际={row}"
        assert any("engine_change_event" in r.message and "engine_scope_synced" in r.message
                   for r in caplog.records), "派生属性同步应输出结构化事件"


class TestEngineRowReplacementMigration:
    """验收 3：引擎行替换（历史重复行）时 capabilities/tags_user_set 迁移 + 引用 remap。"""

    def _seed_dirty_state(self):
        """构造漂移现场：旧行（id 小，带配置+引用）+ 新行（id 大，无配置）并存。"""
        cid = _create_cred(scope="repo")
        with db_lock:
            c = db.cursor()
            # 旧行：user scope 漂移行（优先级 100、被场景/会话引用）
            c.execute(
                "INSERT INTO agent_runtimes (credential_id, name, display_name, installed, healthy, "
                "scope, owner_id, org_id, status, tags_user_set) VALUES (?,?,?,1,1,'user','',?,'online',1)",
                (cid, "t654-hermes", "t654-Hermes", _ORG_PERSONAL))
            old_rid = c.lastrowid
            _set_priority(old_rid, priority=100, tag="iac")
            # 新行：repo scope 行（无 capabilities）
            c.execute(
                "INSERT INTO agent_runtimes (credential_id, name, display_name, installed, healthy, "
                "scope, owner_id, org_id, status) VALUES (?,?,?,1,1,'repo',?,?,'online')",
                (cid, "t654-hermes", "t654-Hermes2", _REPO_KEY, _ORG_REPO))
            new_rid = c.lastrowid
            # 场景引用旧 id
            c.execute(
                "INSERT INTO devops_pipeline_scene (scene_id, name, repo_key, nodes_json) "
                "VALUES ('t654-scene', 't654-scene', ?, ?)",
                (_REPO_KEY, json.dumps([
                    {"tag": "issue", "engine_runtime_ids": [old_rid]},
                    {"tag": "qa", "engine_runtime_ids": [old_rid, 999]},
                ])))
            # agent backend 引用旧 id（devops / chat 会话）
            c.execute(
                "INSERT INTO devops_agent_session (id, repo_key, agent_type, runtime_id) VALUES ('t654-s1', ?, 'issue', ?)",
                (_REPO_KEY, old_rid))
            c.execute(
                "INSERT INTO chat_agent_session (id, user_id, runtime_id) VALUES ('t654-c1', ?, ?)",
                (_USER_ID, old_rid))
            # autopilot 任务引用旧 id
            c.execute(
                "INSERT INTO autopilot_tasks (name, repo_key, engine_runtime_ids) VALUES ('t654-task', ?, ?)",
                (_REPO_KEY, json.dumps([old_rid])))
            db.commit()
        return cid, old_rid, new_rid

    def test_replacement_migrates_capabilities_and_remaps_references(self, caplog):
        from src.api.routes.settings import change_credential_scope, CredScopeBody
        cid, old_rid, new_rid = self._seed_dirty_state()
        with caplog.at_level("INFO"):
            change_credential_scope(cid, CredScopeBody(scope="user"), user=_user())
        # capabilities 迁移到存活行（priority/标签不回默认 50/'all'）
        assert _get_priority(new_rid) == (100, "iac"), f"替换行 capabilities 应迁移，实际={_get_priority(new_rid)}"
        # tags_user_set 继承
        with db_lock:
            tus = db.cursor().execute("SELECT tags_user_set FROM agent_runtimes WHERE id=?", (new_rid,)).fetchone()[0]
        assert tus == 1, "存活行应继承 tags_user_set=1"
        # 旧行删除（不悬空 capabilities）
        with db_lock:
            c = db.cursor()
            cnt_old = c.execute("SELECT COUNT(*) FROM agent_runtimes WHERE id=?", (old_rid,)).fetchone()[0]
            cnt_cap = c.execute("SELECT COUNT(*) FROM engine_capabilities WHERE runtime_id=?", (old_rid,)).fetchone()[0]
        assert cnt_old == 0 and cnt_cap == 0, "被替换行及其残留 capabilities 应删除"
        # 场景引用 remap
        with db_lock:
            nodes = json.loads(db.cursor().execute(
                "SELECT nodes_json FROM devops_pipeline_scene WHERE scene_id='t654-scene'").fetchone()[0])
        assert nodes[0]["engine_runtime_ids"] == [new_rid], f"场景 issue 节点应 remap，实际={nodes[0]}"
        assert nodes[1]["engine_runtime_ids"] == [new_rid, 999], f"非映射 id 应保持，实际={nodes[1]}"
        # agent backend 引用 remap
        with db_lock:
            c = db.cursor()
            s1 = c.execute("SELECT runtime_id FROM devops_agent_session WHERE id='t654-s1'").fetchone()[0]
            c1 = c.execute("SELECT runtime_id FROM chat_agent_session WHERE id='t654-c1'").fetchone()[0]
        assert s1 == new_rid and c1 == new_rid, f"agent backend 引用应 remap，实际 devops={s1} chat={c1}"
        # autopilot 引用 remap
        with db_lock:
            ap = json.loads(db.cursor().execute(
                "SELECT engine_runtime_ids FROM autopilot_tasks WHERE name='t654-task'").fetchone()[0] or "[]")
        assert ap == [new_rid], f"autopilot 引用应 remap，实际={ap}"
        # 结构化事件（cid、影响引擎数、旧→新 id 映射）
        evt = [r.message for r in caplog.records if "engine_change_event" in r.message]
        assert evt, "引擎替换应输出 engine_change_event 事件"
        payload = json.loads(evt[0].split("engine_change_event: ", 1)[1])
        assert payload["credential_id"] == cid
        assert payload["engines_affected"] == 1
        assert payload["id_mapping"] == {str(old_rid): new_rid}

    def test_register_merges_legacy_duplicate_rows(self):
        """register 路径同样收敛历史重复行（upsert 键收敛后的唯一约束兼容）。"""
        cid, old_rid, new_rid = self._seed_dirty_state()
        resp = _register(cid, engines=("t654-hermes",))
        # 复用存活行（MAX id），不 INSERT 新行
        assert resp["runtimes"][0]["id"] == new_rid
        assert _get_priority(new_rid) == (100, "iac"), "合并后存活行应继承 capabilities"
        with db_lock:
            cnt = db.cursor().execute(
                "SELECT COUNT(*) FROM agent_runtimes WHERE credential_id=? AND lower(name)='t654-hermes'",
                (cid,)).fetchone()[0]
        assert cnt == 1, f"注册合并后应只剩 1 行，实际 {cnt} 行"
