"""Issue #3396 改动⑤：Chat 两条兜底路径 chat 角色过滤测试。

- resolve_local_engine（本机直连 + global 回退）：agent_type="chat" 时候选必须
  勾了 chat/all（engine_capabilities），无合规候选 → None（A6/A7）。
- _resolve_chat_runtime_id（实例名 → 本机直连 → 末级按名/默认兜底）：各级解析
  结果必须过 chat 角色过滤，不合规落下一级，全不合规 → 0（明确不静默）。
- agent_type 为空（非 chat 调用面）保持旧行为不做角色过滤。
"""
import datetime

import pytest

from src.kernel.session_manager import db, db_lock, _resolve_chat_runtime_id
from src.domains.runtime.engine import engine_select as es


@pytest.fixture(autouse=True)
def _clean_tables(monkeypatch):
    monkeypatch.setenv("GRAPE_PIPELINE_MODE", "legacy")
    monkeypatch.delenv("DEVOPS_EXECUTION_MODE", raising=False)
    es._resolve_local_cache.clear()
    _sql = (
        "DELETE FROM agent_runtimes WHERE credential_id IN "
        "(SELECT id FROM runtime_credentials WHERE name LIKE 'b3396-%')",
        "DELETE FROM engine_capabilities WHERE runtime_id IN "
        "(SELECT id FROM agent_runtimes WHERE credential_id IN "
        "(SELECT id FROM runtime_credentials WHERE name LIKE 'b3396-%'))",
        "DELETE FROM runtime_credentials WHERE name LIKE 'b3396-%'",
        "DELETE FROM agent_runtimes WHERE credential_id=0 AND lower(name) IN ('opencode','codex')",
        "DELETE FROM engine_capabilities WHERE runtime_id NOT IN (SELECT id FROM agent_runtimes)",
    )
    with db_lock:
        for s in _sql:
            try:
                db.cursor().execute(s)
            except Exception:
                pass
        db.commit()
    yield
    with db_lock:
        for s in _sql:
            try:
                db.cursor().execute(s)
            except Exception:
                pass
        db.commit()
    es._resolve_local_cache.clear()


def _now():
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def _mk_cred(name, scope="global", owner_id="", owner_name="tester"):
    with db_lock:
        c = db.cursor()
        c.execute(
            "INSERT INTO runtime_credentials (token_hash, name, device_name, scope, owner_id, owner_name, status) "
            "VALUES (?,?,?,?,?,?, 'active')",
            ("h" + name, name, "dev-" + name, scope, owner_id, owner_name))
        cid = c.lastrowid
        db.commit()
        return cid


def _mk_engine(name, *, credential_id, cap_tags=None, status="online", healthy=1,
               installed=1, scope="global", owner_id="") -> int:
    with db_lock:
        c = db.cursor()
        c.execute(
            "INSERT INTO agent_runtimes (name, display_name, credential_id, status, healthy, installed, last_seen_at, scope, owner_id) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (name, name, credential_id, status, healthy, installed, _now(), scope, owner_id))
        rid = c.lastrowid
        tags = ["all"] if cap_tags is None else list(cap_tags)
        for t in tags:
            c.execute(
                "INSERT OR IGNORE INTO engine_capabilities (runtime_id, tag, enabled) VALUES (?, ?, 1)",
                (rid, t))
        db.commit()
        return rid


# ===== resolve_local_engine：本机直连 + global 回退 =====

class TestResolveLocalEngineChatFilter:
    def test_local_with_chat_cap_returned(self):
        oc = _mk_engine("opencode", credential_id=0, cap_tags=["chat"], scope="global")
        assert es.resolve_local_engine(0, agent_type="chat") == oc

    def test_local_with_all_cap_returned(self):
        oc = _mk_engine("opencode", credential_id=0, cap_tags=["all"])
        assert es.resolve_local_engine(44, agent_type="chat") == oc

    def test_local_coding_only_falls_back_to_global_chat(self):
        """本机直连只勾 coding → 被 chat 过滤；global 回退命中勾了 all 的远端 worker。"""
        _mk_engine("opencode", credential_id=0, cap_tags=["coding"])
        oc = _mk_engine("opencode", credential_id=_mk_cred("b3396-g1"), cap_tags=["all"])
        assert es.resolve_local_engine(44, agent_type="chat") == oc

    def test_all_candidates_without_chat_return_none(self):
        """A6：本机+global 全不勾 chat/all → None（明确无引擎，不静默调度）。"""
        _mk_engine("opencode", credential_id=0, cap_tags=["coding"])
        _mk_engine("opencode", credential_id=_mk_cred("b3396-g2"), cap_tags=["sre"])
        assert es.resolve_local_engine(44, agent_type="chat") is None
        assert es.resolve_local_engine(0, agent_type="chat") is None

    def test_cleared_engine_excluded(self):
        """A7：手动清空标签（无 capabilities 行）的本机直连不被 chat 兜底选中。"""
        _mk_engine("opencode", credential_id=0, cap_tags=[])
        assert es.resolve_local_engine(44, agent_type="chat") is None

    def test_no_agent_type_keeps_legacy_behavior(self):
        """agent_type 为空（非 chat 调用面）不过滤——保持旧行为。"""
        _mk_engine("opencode", credential_id=0, cap_tags=[])
        rid = es.resolve_local_engine(0)
        assert rid is not None


# ===== _resolve_chat_runtime_id：实例名 → 本机 → 末级兜底 =====

class TestResolveChatRuntimeIdChatFilter:
    def test_user_instance_with_chat_cap(self):
        cid = _mk_cred("b3396-u1", scope="user", owner_id="44", owner_name="tester")
        rid = _mk_engine("opencode", credential_id=cid, cap_tags=["chat"], scope="user", owner_id="44")
        from src.domains.runtime.engine.instances import instance_name
        ins = instance_name("opencode", "dev-b3396-u1", cid)
        assert _resolve_chat_runtime_id(ins) == rid

    def test_user_instance_cleared_returns_zero(self):
        """A7：个人引擎手动清空标签 → 实例名解析被拒 → 0（明确不静默）。"""
        cid = _mk_cred("b3396-u2", scope="user", owner_id="44", owner_name="tester")
        _mk_engine("opencode", credential_id=cid, cap_tags=[], scope="user", owner_id="44")
        from src.domains.runtime.engine.instances import instance_name
        ins = instance_name("opencode", "dev-b3396-u2", cid)
        assert _resolve_chat_runtime_id(ins) == 0

    def test_bare_name_local_without_chat_falls_to_global(self):
        """裸名：本机直连未勾 chat → 落末级按名兜底命中勾 all 的远端 worker。"""
        _mk_engine("opencode", credential_id=0, cap_tags=["coding"])
        oc = _mk_engine("opencode", credential_id=_mk_cred("b3396-g3"), cap_tags=["all"])
        assert _resolve_chat_runtime_id("opencode") == oc

    def test_bare_name_local_with_chat_preferred(self):
        oc = _mk_engine("opencode", credential_id=0, cap_tags=["chat"])
        _mk_engine("opencode", credential_id=_mk_cred("b3396-g4"), cap_tags=["all"])
        assert _resolve_chat_runtime_id("opencode") == oc

    def test_everything_without_chat_returns_zero(self):
        _mk_engine("opencode", credential_id=0, cap_tags=["coding"])
        _mk_engine("opencode", credential_id=_mk_cred("b3396-g5"), cap_tags=["sre"])
        assert _resolve_chat_runtime_id("opencode") == 0

    def test_empty_backend_returns_zero(self):
        assert _resolve_chat_runtime_id("") == 0
