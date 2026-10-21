"""Issue #400：引擎候选唯一键归一与单选约束 — engine_select 层测试。

覆盖：
- engine_unique_key：本机直连以类型名为键；个人/远端以 scope+instance_name 为键；
  同实例（同 device/credential）归一键（canonical identity，对齐 <canonical_account_id>）
- dedupe_engines：同键重复行只保留首条
- _apply_filters：候选收口处单选约束（同身份只保留一个候选）
"""
import pytest

from src.runtime import engine_select as es
from src.session_manager import db, db_lock


@pytest.fixture(autouse=True)
def _clean_tables():
    with db_lock:
        c = db.cursor()
        c.execute("DELETE FROM agent_runtimes")
        c.execute("DELETE FROM runtime_credentials")
        c.execute("DELETE FROM devops_agent_session")
        db.commit()
    yield
    with db_lock:
        c = db.cursor()
        c.execute("DELETE FROM agent_runtimes")
        c.execute("DELETE FROM runtime_credentials")
        c.execute("DELETE FROM devops_agent_session")
        db.commit()


class TestEngineUniqueKey:
    def test_local_uses_type_name(self):
        key = es.engine_unique_key({"credential_id": 0, "name": "opencode", "scope": "global"})
        assert key == "local:opencode"

    def test_user_uses_instance_name(self):
        key = es.engine_unique_key({"credential_id": 9, "name": "opencode", "device_name": "My-Mac",
                                    "scope": "user"})
        assert key == "user:opencode-my-mac"

    def test_same_device_same_key_regardless_of_row(self):
        a = es.engine_unique_key({"credential_id": 9, "name": "opencode", "device_name": "dup-01", "scope": "user"})
        b = es.engine_unique_key({"credential_id": 10, "name": "opencode", "device_name": "dup-01", "scope": "user"})
        assert a == b == "user:opencode-dup-01"

    def test_different_scope_differs(self):
        a = es.engine_unique_key({"credential_id": 9, "name": "opencode", "device_name": "my-mac", "scope": "user"})
        b = es.engine_unique_key({"credential_id": 9, "name": "opencode", "device_name": "my-mac", "scope": "repo"})
        assert a != b


def _member(rid, name="opencode", cid=0, device="", scope="global", healthy=1, installed=1, enabled=True):
    return {"runtime_id": rid, "name": name, "credential_id": cid, "device_name": device,
            "scope": scope, "healthy": healthy, "installed": installed, "enabled": enabled,
            "status": "online", "last_seen_at": ""}


class TestDedupeEngines:
    def test_duplicate_instance_merged_keep_first(self):
        members = [_member(1, cid=9, device="dup-01", scope="user"),
                   _member(2, cid=10, device="dup-01", scope="user"),
                   _member(3, cid=0, scope="local")]
        out = es.dedupe_engines(members)
        rids = [m["runtime_id"] for m in out]
        assert rids == [1, 3]

    def test_identity_preserved_when_no_duplicates(self):
        members = [_member(1, cid=0, scope="local"),
                   _member(2, cid=9, device="my-mac", scope="user")]
        out = es.dedupe_engines(members)
        assert [m["runtime_id"] for m in out] == [1, 2]


class TestApplyFiltersSingleSelect:
    def test_user_duplicate_merged(self):
        cfg = {"filters": {"only_online": False, "only_healthy": False, "only_installed": False}}
        members = [_member(1, cid=9, device="dup-01", scope="user"),
                   _member(2, cid=10, device="dup-01", scope="user")]
        out = es._apply_filters(members, cfg)
        assert len(out) == 1 and out[0]["runtime_id"] == 1

    def test_local_and_personal_both_kept(self):
        """本机直连 opencode + 个人实例 opencode-my-mac：不同唯一键，两者都是合法候选。"""
        cfg = {"filters": {"only_online": False, "only_healthy": False, "only_installed": False}}
        members = [_member(1, cid=0, scope="local"),
                   _member(2, cid=9, device="my-mac", scope="user")]
        out = es._apply_filters(members, cfg)
        assert len(out) == 2