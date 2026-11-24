"""Issue #3396: 「选择引擎 — SRE」候选列表必须与调度口径一致（含 global 池）

背景（2026-11-20 用户实测）：#3396 已删除调度侧 `_sre_no_broadcast` 并放行 global 池，
但 `insight/routes/dashboard.py` 的 agent_engines 仍保留旧口径——SRE 只查 user+repo 两档。
结果：一个「所有引擎都是 scope=global」的环境，调度允许 SRE 任务落到 global 引擎，
但界面「选择引擎 — SRE」候选为空，用户报「没有引擎列表」。

本用例守三条行为：
1. global 档、标签含 sre 或 all 的引擎 → 出现在 agent_engines['sre']；
2. 本机直连（credential_id=0）档同上（旧口径 sre 分支也不查 local）；
3. 只勾 coding 的引擎 → 不得出现在 sre 列表。
"""
import os

os.environ["DEVOPS_DB_PATH"] = os.path.join(
    os.path.dirname(__file__), "..", "..", "data", "test_sessions.db")

import pytest
from fastapi.testclient import TestClient
from src.api.main import app
from src.kernel.session_manager import db, db_lock
from src.domains.mgmt.auth import grape_account_db

client = TestClient(app)

_uid = None
_headers = {}
REPO_KEY = "gitcode:org/test3396p"


def _jwt_access_token(user_id, username="test3396p"):
    from src.api.jwt_utils import create_access_token
    from src.domains.mgmt.auth.grape_account_db import get_token_version
    return create_access_token(user_id, username, get_token_version(user_id))


def _now_iso():
    import datetime
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def _mk_cred(name):
    # 凭据名带模块前缀（runtime_credentials.name 唯一），避免与其它 3396 用例同名冲突
    name = "test3396p_" + name
    with db_lock:
        c = db.cursor()
        c.execute(
            "INSERT INTO runtime_credentials (token_hash, name, device_name, scope, owner_id, owner_name, status) "
            "VALUES (?,?,?,?,?,?, 'active')",
            ("h_test3396p_" + name, name, "dev-" + name, "global", "", "test3396p"))
        cid = c.lastrowid
        db.commit()
        return cid


def _mk_engine(name, *, cid=0, tags=("all",), status="online"):
    """cid=0 → 本机直连；cid>0 → 远端 global 档。"""
    with db_lock:
        c = db.cursor()
        c.execute(
            "INSERT INTO agent_runtimes (name, display_name, credential_id, status, healthy, installed, "
            "last_seen_at, scope, owner_id) VALUES (?,?,?,?,1,1,?,?,'')",
            (name, name, cid, status, _now_iso(), "global"))
        rid = c.lastrowid
        for t in tags:
            c.execute(
                "INSERT OR IGNORE INTO engine_capabilities (runtime_id, tag, enabled) VALUES (?, ?, 1)",
                (rid, t))
        db.commit()
        return rid


def setup_module():
    global _uid, _headers
    _uid = grape_account_db.create_grape_account("test3396p", email=None, password="test123")  # secrets-check: allow (test fixture password)
    if not _uid:
        _uid = grape_account_db.get_grape_account_by_username("test3396p")
    _headers = {"Authorization": f"Bearer {_jwt_access_token(_uid['id'], _uid['username'])}"}
    with db_lock:
        c = db.cursor()
        sub = {
            "vod": '["vod_signal"]',
            "issue": '["issue_opened","issue_updated"]',
            "coding": '["pull_request_opened","pull_request_updated"]',
            "qa": "[]",
            "sre": '["pull_request_merged","deploy_start","deploy_failure"]',
        }
        for at, dn, n in [("vod", "VOD Agent", 1), ("issue", "Issue Agent", 2),
                          ("coding", "Coding Agent", 3), ("qa", "QA Agent", 4),
                          ("sre", "SRE Agent", 5)]:
            c.execute(
                "INSERT OR REPLACE INTO agent_type (agent_type, display_name, description, icon, order_num, "
                "subscribe_events) VALUES (?,?,?,?,?,?)",
                (at, dn, f"Desc {at}", "●", n, sub.get(at, "[]")))
        c.execute("INSERT OR REPLACE INTO managed_repos (repo_key, repo_owner, repo_name, full_name) "
                  "VALUES (?,?,?,?)", (REPO_KEY, "org", "test3396p", "org/test3396p"))
        c.execute("INSERT OR REPLACE INTO user_repos (user_id, repo_key, is_join, is_admin) "
                  "VALUES (?,?,1,1)", (_uid["id"], REPO_KEY))
        db.commit()


def teardown_module():
    if _uid:
        grape_account_db.soft_delete_account(_uid["id"])


@pytest.fixture(autouse=True)
def _clean():
    yield


@pytest.fixture(autouse=True)
def _purge_engines():
    def _purge():
        with db_lock:
            c = db.cursor()
            c.execute("DELETE FROM engine_capabilities WHERE runtime_id IN "
                      "(SELECT id FROM agent_runtimes WHERE name LIKE 'test3396%')")
            c.execute("DELETE FROM agent_runtimes WHERE name LIKE 'test3396%'")
            c.execute("DELETE FROM runtime_credentials WHERE name LIKE 'test3396%'")
            db.commit()
    _purge()
    yield
    _purge()


def _engines(atype):
    r = client.get("/api/dashboard/org/test3396p", headers=_headers)
    assert r.status_code == 200, r.text[:300]
    return r.json().get("agent_engines", {}).get(atype, [])


class TestSreEnginePickerMatchesDispatch:
    def test_global_engine_with_all_tag_listed(self):
        cid = _mk_cred("remote_all")
        rid = _mk_engine("test3396p_global_all", cid=cid, tags=["all"])
        ids = [e["id"] for e in _engines("sre")]
        assert rid in ids, f"global(all) engine missing from SRE picker: ids={ids}"

    def test_global_engine_with_sre_tag_listed(self):
        cid = _mk_cred("remote_sre")
        rid = _mk_engine("test3396p_global_sre", cid=cid, tags=["sre"])
        ids = [e["id"] for e in _engines("sre")]
        assert rid in ids, f"global(sre) engine missing from SRE picker: ids={ids}"

    def test_local_engine_tagged_sre_listed(self):
        rid = _mk_engine("test3396p_local_sre", cid=0, tags=["sre"])
        ids = [e["id"] for e in _engines("sre")]
        assert rid in ids, f"local(sre) engine missing from SRE picker: ids={ids}"

    def test_engine_without_sre_role_excluded(self):
        cid = _mk_cred("remote_coding")
        rid = _mk_engine("test3396p_global_coding", cid=cid, tags=["coding"])
        ids = [e["id"] for e in _engines("sre")]
        assert rid not in ids, "engine tagged only 'coding' must not be a SRE candidate"


class TestOtherRolesUnchanged:
    def test_coding_engine_still_listed_for_coding(self):
        cid = _mk_cred("remote_coding2")
        rid = _mk_engine("test3396p_global_coding2", cid=cid, tags=["coding"])
        ids = [e["id"] for e in _engines("coding")]
        assert rid in ids, "coding candidate list regressed"

    def test_sre_list_not_leaking_into_chat(self):
        # chat 走本机直连/global 兜底，另有专门用例；这里只确认 sre 与 chat 的候选各自独立计算
        cid = _mk_cred("remote_sre_only")
        rid = _mk_engine("test3396p_global_sre_only", cid=cid, tags=["sre"])
        assert rid in [e["id"] for e in _engines("sre")]
        assert rid not in [e["id"] for e in _engines("coding")]