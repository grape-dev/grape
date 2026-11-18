"""本机 codex 启动种子的认领能力回归测试。

背景：Issue 阶段任务 runtime_id=0 广播后永久 queued——codex 是当时唯一在线的
本机引擎，但 seed 把 agent_tags 写死为 '[]' 且从不写 engine_capabilities：

- 派发侧 query_engines(agent_type=...) 按 agent_tags 过滤 → codex 恒被排除 → 广播；
- 认领侧 service.claim_by_tag() 只认 engine_capabilities 的 tag=issue|all → 无行直接 None。

本测试锁住修复后的契约：存量行修复、新行默认带 all、人工配置不被覆盖、
认领守卫 SQL 能命中。
"""
import pytest

from src.api.main import _seed_codex_runtime
from src.session_manager import db, db_lock


@pytest.fixture(autouse=True)
def _clean_codex_rows():
    """测试前后清掉 codex 种子行及其能力标签，避免跨用例干扰。"""
    def _clean():
        with db_lock:
            c = db.cursor()
            rows = c.execute("SELECT id FROM agent_runtimes WHERE name='codex'").fetchall()
            for (rid,) in rows:
                c.execute("DELETE FROM engine_capabilities WHERE runtime_id=?", (rid,))
            c.execute("DELETE FROM agent_runtimes WHERE name='codex'")
            db.commit()
    _clean()
    yield
    _clean()


def _row(host):
    with db_lock:
        return db.cursor().execute(
            "SELECT id, agent_tags, tags_user_set FROM agent_runtimes "
            "WHERE name='codex' AND host_name=?", (host,)).fetchone()


def _caps(rid):
    with db_lock:
        return [r[0] for r in db.cursor().execute(
            "SELECT tag FROM engine_capabilities WHERE runtime_id=? AND enabled=1 ORDER BY tag",
            (rid,)).fetchall()]


def test_new_seed_has_all_capability_and_derived_cache():
    """新库种子：agent_tags 与 engine_capabilities 同时带 all。"""
    r = _seed_codex_runtime(host_name="seed-new-host")
    row = _row("seed-new-host")
    assert row is not None
    assert r["runtime_id"] == row[0]
    assert "all" in (row[1] or "")
    assert _caps(row[0]) == ["all"]
    assert r["tagged"] is True


def test_existing_empty_tags_are_repaired():
    """存量 codex 行（生产现状 agent_tags='[]'）在重启后自愈。"""
    with db_lock:
        c = db.cursor()
        c.execute(
            "INSERT INTO agent_runtimes "
            "(credential_id, name, display_name, installed, healthy, scope, status, "
            " host_name, agent_tags, tags_user_set) "
            "VALUES (0,'codex','Codex Agent',1,1,'global','online',?,'[]',0)",
            ("seed-existing-host",))
        rid = c.lastrowid
        db.commit()
    assert _caps(rid) == []

    r = _seed_codex_runtime(host_name="seed-existing-host")

    assert r["runtime_id"] == rid
    assert _caps(rid) == ["all"]
    assert "all" in (_row("seed-existing-host")[1] or "")


def test_manual_tags_are_not_overwritten():
    """tags_user_set=1（用户手动配过）时不覆盖人工配置。"""
    with db_lock:
        c = db.cursor()
        c.execute(
            "INSERT INTO agent_runtimes "
            "(credential_id, name, display_name, installed, healthy, scope, status, "
            " host_name, agent_tags, tags_user_set) "
            "VALUES (0,'codex','Codex Agent',1,1,'global','online',?,'[\"issue\"]',1)",
            ("seed-manual-host",))
        rid = c.lastrowid
        c.execute(
            "INSERT INTO engine_capabilities "
            "(runtime_id, tag, max_concurrent, priority, enabled, created_at, updated_at) "
            "VALUES (?,'issue',1,50,1,'2026-01-01','2026-01-01')", (rid,))
        db.commit()

    r = _seed_codex_runtime(host_name="seed-manual-host")

    assert r["user_set"] is True
    assert r["tagged"] is False
    assert _caps(rid) == ["issue"]
    assert _row("seed-manual-host")[1] == '["issue"]'


def test_claim_by_tag_guard_matches_seeded_codex():
    """认领守卫 SQL（service.py claim_by_tag）能命中种子后的 codex。"""
    r = _seed_codex_runtime(host_name="seed-guard-host")
    rid = r["runtime_id"]
    with db_lock:
        row = db.cursor().execute(
            "SELECT ec.tag FROM engine_capabilities ec "
            "JOIN agent_runtimes ar ON ar.id=ec.runtime_id "
            "WHERE ec.runtime_id=? AND ec.enabled=1 AND (ec.tag=? OR ec.tag='all')",
            (rid, "issue")).fetchone()
    assert row is not None
    assert row[0] == "all"
