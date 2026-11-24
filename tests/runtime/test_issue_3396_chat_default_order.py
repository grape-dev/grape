"""Issue #3396: Chat 默认引擎兜底顺序 = 本机 → 个人 → 仓库 → 全局（用户 2026-11-20 裁定）

覆盖：
1. 本机有合规引擎 → 选本机（沿用 host 限定 + 用户稳定取模）；
2. 本机无 → 选**自己的**个人引擎（此前会跳过个人档直接落 global）；
3. 别人名下的个人引擎绝不能当"个人档"返回（owner_id 精确过滤）；
4. 个人也没有 → 仓库档（repo_key 匹配）优先于全局；
5. 以上都没有 → 全局。

同时守 owner_id 过滤：存量行 owner_id 为空时用 owner_name 兜底。
"""
import os

os.environ["DEVOPS_DB_PATH"] = os.path.join(
    os.path.dirname(__file__), "..", "..", "data", "test_sessions.db")

import pytest
from src.kernel.session_manager import db, db_lock
from src.domains.mgmt.auth import grape_account_db
from src.domains.runtime.engine.engine_select import resolve_chat_engine, engine_host

_uid = None
_CREATED = []   # 本模块创建的 runtime_id，purge 用（本机档必须用已知引擎名，无法按名字前缀清）
USERNAME = "test3396d"
REPO_KEY = "gitcode:org/test3396d"


def _now():
    import datetime
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def _mk_cred(name, scope, owner_id="", owner_name=""):
    with db_lock:
        c = db.cursor()
        c.execute(
            "INSERT INTO runtime_credentials (token_hash, name, device_name, scope, owner_id, owner_name, status) "
            "VALUES (?,?,?,?,?,?, 'active')",
            ("h_test3396d_" + name, name, "dev-" + name, scope, owner_id, owner_name))
        cid = c.lastrowid
        db.commit()
        return cid


def _mk_engine(name, *, cid=0, tags=("chat",), healthy=1, host_name=None):
    # 本机档（cid=0）过 _apply_filters 时要求 name 是**已登记引擎名**（防幽灵占位），
    # 所以本机用例传 "opencode"/"hermes" 这类已知名，靠 runtime_id 做断言。
    # 本机档在 user 上下文里按 host_name == engine_host() 限定（ELB 分流语义），
    # 用例里必须落成本机 hostname，否则会被"非本主机"过滤掉。
    if host_name is None:
        host_name = engine_host() if cid == 0 else ""
    with db_lock:
        c = db.cursor()
        c.execute(
            "INSERT INTO agent_runtimes (name, display_name, credential_id, status, healthy, installed, "
            "last_seen_at, scope, owner_id, host_name) VALUES (?,?,?,?,?,1,?,?,?,?)",
            (name, name, cid, "online" if healthy else "offline", healthy, _now(),
             "local" if cid == 0 else "global", "", host_name))
        rid = c.lastrowid
        _CREATED.append(rid)
        for t in tags:
            c.execute("INSERT OR IGNORE INTO engine_capabilities (runtime_id, tag, enabled) VALUES (?,?,1)",
                      (rid, t))
        db.commit()
        return rid


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
            c.execute("DELETE FROM runtime_credentials WHERE name LIKE 'test3396d_%'")
            db.commit()
        _CREATED.clear()
    _p()
    yield
    _p()


def _resolve():
    return resolve_chat_engine(_uid["id"], USERNAME, REPO_KEY, "chat")


class TestChatDefaultOrder:
    def test_local_first(self):
        rid = _mk_engine("opencode", cid=0)
        _mk_cred("user_own", "user", owner_id=str(_uid["id"]), owner_name=USERNAME)
        cid_user = _mk_cred("user_own2", "user", owner_id=str(_uid["id"]), owner_name=USERNAME)
        _mk_engine("test3396d_user_engine", cid=cid_user)
        assert _resolve() == rid, "本机档必须最优先"

    def test_personal_second_when_no_local(self):
        cid = _mk_cred("user_own3", "user", owner_id=str(_uid["id"]), owner_name=USERNAME)
        rid_user = _mk_engine("test3396d_user_engine2", cid=cid)
        cid_g = _mk_cred("global_x", "global", owner_id="", owner_name="")
        _mk_engine("test3396d_global_x", cid=cid_g)
        assert _resolve() == rid_user, "本机无候选时必须先用**自己的**个人引擎"

    def test_other_users_personal_never_used(self):
        cid_other = _mk_cred("user_other", "user", owner_id="00000000-0000-0000-0000-0000000000ff",
                             owner_name="someone-else")
        other_rid = _mk_engine("test3396d_other_personal", cid=cid_other)
        cid_g = _mk_cred("global_y", "global", owner_id="", owner_name="")
        g_rid = _mk_engine("test3396d_global_y", cid=cid_g)
        got = _resolve()
        assert got != other_rid, "别人的个人引擎不得被当作个人档返回"
        assert got == g_rid, f"应落到全局档，实际 {got}"

    def test_legacy_user_row_without_owner_id_matches_by_name(self):
        cid = _mk_cred("user_legacy", "user", owner_id="", owner_name=USERNAME)
        rid = _mk_engine("test3396d_user_legacy", cid=cid)
        assert _resolve() == rid, "存量 owner_id 为空的个人引擎应按 owner_name 兜底匹配"

    def test_repo_before_global(self):
        cid_r = _mk_cred("repo_engine", "repo", owner_id=REPO_KEY, owner_name=USERNAME)
        rid_repo = _mk_engine("test3396d_repo_engine", cid=cid_r)
        cid_g = _mk_cred("global_z", "global", owner_id="", owner_name="")
        _mk_engine("test3396d_global_z", cid=cid_g)
        assert _resolve() == rid_repo, "仓库档必须先于全局档"

    def test_global_last(self):
        cid_g = _mk_cred("global_only", "global", owner_id="", owner_name="")
        rid = _mk_engine("test3396d_global_only", cid=cid_g)
        assert _resolve() == rid

    def test_engine_without_chat_role_skipped(self):
        cid_user = _mk_cred("user_norole", "user", owner_id=str(_uid["id"]), owner_name=USERNAME)
        _mk_engine("test3396d_user_norole", cid=cid_user, tags=("coding",))
        cid_g = _mk_cred("global_ok", "global", owner_id="", owner_name="")
        rid_g = _mk_engine("test3396d_global_ok", cid=cid_g, tags=("chat",))
        assert _resolve() == rid_g, "未勾 chat 角色的档位视为无候选，继续往后找"