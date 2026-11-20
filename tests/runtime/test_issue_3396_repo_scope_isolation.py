"""Issue #3396: 仓库档隔离 ——「别人仓库的引擎也不能用」（用户 2026-11-20 追加裁定）

覆盖：
1. 指定 repo_key 时只返回**该仓库**的仓库级引擎（仓库 B 的引擎不得混入仓库 A 的候选）；
2. 空 repo_key 且无 username → 空集（fail-closed，不得退化成"全部仓库级引擎"）；
3. 空 repo_key 且给了 username → 只按该用户**已加入**的仓库收紧（别人的仓库不进候选）；
4. Chat 兜底（repo_key 为空、用户没加入任何仓库）→ 跳过仓库档，落全局，绝不使用别人仓库的引擎；
5. Chat 兜底（用户已加入仓库 A）→ 空 repo_key 时仍可用自己仓库的仓库级引擎。
"""
import os

os.environ["DEVOPS_DB_PATH"] = os.path.join(
    os.path.dirname(__file__), "..", "..", "data", "test_sessions.db")

import pytest
from src.kernel.session_manager import db, db_lock
from src.domains.mgmt.auth import grape_account_db
from src.domains.runtime.engine.engine_select import query_engines, resolve_chat_engine

_uid = None
_CREATED = []
USERNAME = "test3396r"
REPO_A = "gitcode:org/test3396r-a"
REPO_B = "gitcode:org/test3396r-b"


def _now():
    import datetime
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def _mk_cred(name, scope, owner_id="", owner_name=""):
    with db_lock:
        c = db.cursor()
        c.execute(
            "INSERT INTO runtime_credentials (token_hash, name, device_name, scope, owner_id, owner_name, status) "
            "VALUES (?,?,?,?,?,?, 'active')",
            ("h_test3396r_" + name, name, "dev-" + name, scope, owner_id, owner_name))
        cid = c.lastrowid
        db.commit()
        return cid


def _mk_engine(name, *, cid=0, tags=("chat",), healthy=1):
    with db_lock:
        c = db.cursor()
        c.execute(
            "INSERT INTO agent_runtimes (name, display_name, credential_id, status, healthy, installed, "
            "last_seen_at, scope, owner_id) VALUES (?,?,?,?,?,1,?,?,?)",
            (name, name, cid, "online" if healthy else "offline", healthy, _now(),
             "local" if cid == 0 else "global", ""))
        rid = c.lastrowid
        _CREATED.append(rid)
        for t in tags:
            c.execute("INSERT OR IGNORE INTO engine_capabilities (runtime_id, tag, enabled) VALUES (?,?,1)",
                      (rid, t))
        db.commit()
        return rid


def _mk_repo_engine(label, repo_key):
    cid = _mk_cred(label, "repo", owner_id=repo_key, owner_name=USERNAME)
    return _mk_engine("test3396r_" + label, cid=cid)


def _mk_global_engine(label):
    cid = _mk_cred(label, "global", owner_id="", owner_name="")
    return _mk_engine("test3396r_" + label, cid=cid)


def setup_module():
    global _uid
    _uid = grape_account_db.create_grape_account(USERNAME, email=None, password="test123")  # secrets-check: allow (test fixture password)
    if not _uid:
        _uid = grape_account_db.get_grape_account_by_username(USERNAME)


def teardown_module():
    if _uid:
        grape_account_db.soft_delete_account(_uid["id"])


@pytest.fixture(autouse=True)
def _purge():
    def _p():
        with db_lock:
            c = db.cursor()
            if _CREATED:
                ph = ",".join("?" * len(_CREATED))
                c.execute(f"DELETE FROM engine_capabilities WHERE runtime_id IN ({ph})", tuple(_CREATED))
                c.execute(f"DELETE FROM agent_runtimes WHERE id IN ({ph})", tuple(_CREATED))
            c.execute("DELETE FROM runtime_credentials WHERE name LIKE 'test3396r_%'")
            c.execute("DELETE FROM user_repos WHERE user_id=?", (str(_uid["id"]),))
            db.commit()
        _CREATED.clear()
    _p()
    yield
    _p()


def _join_repo(repo_key):
    with db_lock:
        c = db.cursor()
        owner, _, name = repo_key.partition(":")[2].partition("/")
        c.execute(
            "INSERT OR REPLACE INTO user_repos (user_id, repo_key, owner, name, is_join, is_admin) "
            "VALUES (?,?,?,?,1,1)",
            (str(_uid["id"]), repo_key, owner, name))
        db.commit()


def _rids(rows):
    return {int(r["runtime_id"]) for r in rows}


class TestRepoScopeIsolation:
    def test_only_current_repo_engines_returned(self):
        rid_a = _mk_repo_engine("repo_a", REPO_A)
        rid_b = _mk_repo_engine("repo_b", REPO_B)
        got = _rids(query_engines(REPO_A, "repo", agent_type="chat"))
        assert rid_a in got, "本仓库的引擎必须在候选里"
        assert rid_b not in got, "别人仓库的引擎不得出现在本仓库候选里"
        got_b = _rids(query_engines(REPO_B, "repo", agent_type="chat"))
        assert rid_b in got_b and rid_a not in got_b

    def test_empty_repo_without_user_is_fail_closed(self):
        _mk_repo_engine("repo_a2", REPO_A)
        assert query_engines("", "repo", agent_type="chat") == [], \
            "空 repo_key 且无 username 时不得返回任何仓库级引擎（不能退化成全员可见）"

    def test_empty_repo_filters_by_membership(self):
        rid_a = _mk_repo_engine("repo_a3", REPO_A)
        rid_b = _mk_repo_engine("repo_b3", REPO_B)
        _join_repo(REPO_A)
        got = _rids(query_engines("", "repo", agent_type="chat", username=USERNAME))
        assert rid_a in got, "自己已加入仓库的引擎应可作候选"
        assert rid_b not in got, "未加入的仓库引擎不得进入候选"

    def test_chat_default_never_picks_other_repo_engine_without_repo_key(self):
        other_repo_rid = _mk_repo_engine("repo_a4", REPO_A)
        global_rid = _mk_global_engine("global1")
        got = resolve_chat_engine(_uid["id"], USERNAME, "", "chat")
        assert got != other_repo_rid, "未加入该仓库时不得把它仓库的引擎当兜底"
        assert got == global_rid, f"应落到全局档，实际 {got}"

    def test_chat_default_uses_own_repo_engine_when_member(self):
        own_repo_rid = _mk_repo_engine("repo_a5", REPO_A)
        _mk_global_engine("global2")
        _join_repo(REPO_A)
        got = resolve_chat_engine(_uid["id"], USERNAME, "", "chat")
        assert got == own_repo_rid, "已加入仓库 A 时，其仓库级引擎可作为仓库档候选"