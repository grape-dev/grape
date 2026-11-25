"""Issue #3396 A8 兼容：worker 声明的 role_tags 播种为角色能力行（声明即收窄）。

背景（架构守护 M1）：口径切到 engine_capabilities 后，若注册路径在「能力行为空」
时无条件补 'all'，则只声明 role_tags=['sre'] 的特殊场景机器会失去收窄语义，
被派发到任意角色。修复后：

- 声明非空 → 能力行 = 声明集合（only-sre 机器只认领 sre）
- 未声明   → 维持存量口径补 'all'（任意角色可认领，回归不变）
- tags_user_set=1（UI 手工改过）→ 一律不覆盖
- 已有非 all 角色行（接口/UI 写入）→ 不覆盖
- 存量行迁移面：行内 role_tags 非空且只有自动补的 'all' → 下次注册/心跳改写为声明集合
"""

import hashlib
import json
import secrets

import pytest
from fastapi.testclient import TestClient

from src.kernel.session_manager import db, db_lock
from src.domains.runtime.engine import engine_select as es
from src.domains.runtime.engine.engine_select import KNOWN_AGENT_ROLES


@pytest.fixture(autouse=True)
def _clean_tables(monkeypatch):
    monkeypatch.setenv("GRAPE_PIPELINE_MODE", "legacy")
    _sql = (
        "DELETE FROM engine_capabilities WHERE runtime_id IN "
        "(SELECT id FROM agent_runtimes WHERE credential_id IN "
        "(SELECT id FROM runtime_credentials WHERE name LIKE 'b3396r-%'))",
        "DELETE FROM agent_runtimes WHERE credential_id IN "
        "(SELECT id FROM runtime_credentials WHERE name LIKE 'b3396r-%')",
        "DELETE FROM runtime_credentials WHERE name LIKE 'b3396r-%'",
    )

    def _run():
        with db_lock:
            for s in _sql:
                try:
                    db.cursor().execute(s)
                except Exception:
                    pass
            db.commit()

    _run()
    yield
    _run()


def _client():
    return TestClient(__import__("src.api.main", fromlist=["app"]).app)


def _make_token(name: str) -> str:
    token = "rta_" + secrets.token_urlsafe(32)
    with db_lock:
        db.cursor().execute(
            "INSERT INTO runtime_credentials (token_hash, name, scope) VALUES (?,?, 'global')",
            (hashlib.sha256(token.encode()).hexdigest(), name))
        db.commit()
    return token


def _role_caps(rid: int) -> list:
    with db_lock:
        rows = db.cursor().execute(
            "SELECT tag FROM engine_capabilities WHERE runtime_id=? AND enabled=1", (rid,)).fetchall()
    return sorted([r[0] for r in rows if r[0] == "all" or r[0] in KNOWN_AGENT_ROLES])


def _register(client, token, device, runtime: dict):
    r = client.post("/api/runtime/register", headers={"Authorization": f"Bearer {token}"},
                    json={"device_name": device, "runtimes": [runtime]})
    assert r.status_code == 200, r.text
    return r.json()["runtimes"][0]["id"]


def test_declared_role_tags_seed_capabilities():
    """声明 ['sre'] → 能力行只有 sre（不补 all），派发侧只放行 sre。"""
    client = _client()
    token = _make_token("b3396r-1")
    rid = _register(client, token, "b3396r-dev1", {"name": "codearts", "role_tags": ["sre"]})
    assert _role_caps(rid) == ["sre"], _role_caps(rid)
    assert es.engine_allows_role(rid, "sre") is True
    assert es.engine_allows_role(rid, "coding") is False
    with db_lock:
        row = db.cursor().execute("SELECT agent_tags FROM agent_runtimes WHERE id=?", (rid,)).fetchone()
    tags = json.loads(row[0] or "[]")
    assert "sre" in tags and "all" not in tags, tags


def test_declined_multi_roles_seed_as_is():
    """声明 ['vod','chat'] → 两行都播种。"""
    client = _client()
    token = _make_token("b3396r-2")
    rid = _register(client, token, "b3396r-dev2", {"name": "hermes", "role_tags": ["vod", "chat"]})
    assert _role_caps(rid) == ["chat", "vod"], _role_caps(rid)


def test_no_declaration_keeps_all():
    """未声明 → 存量口径不变：补 'all'。"""
    client = _client()
    token = _make_token("b3396r-3")
    rid = _register(client, token, "b3396r-dev3", {"name": "opencode"})
    assert _role_caps(rid) == ["all"], _role_caps(rid)
    assert es.engine_allows_role(rid, "coding") is True


def test_invalid_declaration_falls_back_to_all():
    """声明里没有合法角色 → 退回 'all'（不制造 0 标签死机器）。"""
    client = _client()
    token = _make_token("b3396r-4")
    rid = _register(client, token, "b3396r-dev4",
                    {"name": "atomcode", "role_tags": ["not-a-role", "foo"]})
    assert _role_caps(rid) == ["all"], _role_caps(rid)


def test_legacy_row_declared_tags_gets_reseeded():
    """存量迁移面：行内 role_tags=['sre'] + 只有自动补的 'all' → 再注册改写为 sre。"""
    client = _client()
    token = _make_token("b3396r-5")
    rid = _register(client, token, "b3396r-dev5", {"name": "dsh"})
    assert _role_caps(rid) == ["all"]
    # 模拟升级前遗留：role_tags 带声明值，能力行仍是自动补的 'all'
    with db_lock:
        db.cursor().execute(
            "UPDATE agent_runtimes SET role_tags='[\"sre\"]', tags_user_set=0 WHERE id=?", (rid,))
        db.commit()
    # 心跳/再注册（worker 未上报 role_tags）→ 从行内声明播种
    rid2 = _register(client, token, "b3396r-dev5", {"name": "dsh"})
    assert rid2 == rid, (rid, rid2)
    assert _role_caps(rid) == ["sre"], _role_caps(rid)


def test_heartbeat_reseeds_legacy_declared_row():
    """心跳链兜底：存量行 role_tags=['sre'] + 只有自动 all → 心跳即改写为 sre。"""
    client = _client()
    token = _make_token("b3396r-8")
    rid = _register(client, token, "b3396r-dev8", {"name": "hermes"})
    assert _role_caps(rid) == ["all"]
    with db_lock:
        db.cursor().execute(
            "UPDATE agent_runtimes SET role_tags='[\"sre\"]', tags_user_set=0 WHERE id=?", (rid,))
        db.commit()
    r = client.post("/api/runtime/heartbeat", headers={"Authorization": f"Bearer {token}"},
                    json={"runtime_ids": [rid]})
    assert r.status_code == 200, r.text
    assert _role_caps(rid) == ["sre"], _role_caps(rid)
    assert es.engine_allows_role(rid, "coding") is False


def test_heartbeat_does_not_resurrect_declared_disabled_roles():
    """QA 第 6 轮 Required：声明非空 + 仅停用行 + tags_user_set=0，心跳也不得复活。

    与 test_heartbeat_does_not_resurrect_disabled_roles 互补：那条是声明为空、
    停用的恰是 all；本条复现 QA 端点探针的精确场景——声明的 sre 被显式停用后，
    seed 旧口径（只看 enabled=1）会把它当「没配过」重新插入 enabled=1。
    """
    client = _client()
    token = _make_token("b3396r-13")
    rid = _register(client, token, "b3396r-dev13", {"name": "hermes", "role_tags": ["sre"]})
    assert _role_caps(rid) == ["sre"]
    with db_lock:
        db.cursor().execute("UPDATE engine_capabilities SET enabled=0 WHERE runtime_id=?", (rid,))
        db.cursor().execute("UPDATE agent_runtimes SET tags_user_set=0 WHERE id=?", (rid,))
        db.commit()
    assert _role_caps(rid) == []
    r = client.post("/api/runtime/heartbeat", headers={"Authorization": f"Bearer {token}"},
                    json={"runtime_ids": [rid]})
    assert r.status_code == 200, r.text
    assert _role_caps(rid) == [], "声明角色停用后被心跳复活"
    with db_lock:
        rows = db.cursor().execute(
            "SELECT tag, enabled FROM engine_capabilities WHERE runtime_id=?", (rid,)).fetchall()
    assert [tuple(x) for x in rows if ":" not in x[0]] == [("sre", 0)], rows


def test_re_register_does_not_resurrect_declared_disabled_roles():
    """注册链同场景：重复注册（如 worker 重启自注册）不得复活显式停用的声明角色。"""
    client = _client()
    token = _make_token("b3396r-14")
    payload = {"name": "hermes", "role_tags": ["sre"]}
    rid = _register(client, token, "b3396r-dev14", payload)
    with db_lock:
        db.cursor().execute("UPDATE engine_capabilities SET enabled=0 WHERE runtime_id=?", (rid,))
        db.cursor().execute("UPDATE agent_runtimes SET tags_user_set=0 WHERE id=?", (rid,))
        db.commit()
    r = client.post("/api/runtime/register", headers={"Authorization": f"Bearer {token}"},
                    json={"device_name": "b3396r-dev14", "runtimes": [payload]})
    assert r.status_code == 200, r.text
    with db_lock:
        rows = db.cursor().execute(
            "SELECT tag, enabled FROM engine_capabilities WHERE runtime_id=?", (rid,)).fetchall()
    assert [tuple(x) for x in rows if ":" not in x[0]] == [("sre", 0)], rows


def test_register_seed_skips_whole_group_when_sibling_role_disabled():
    """QA 第 6 轮 Required ②：声明 ['chat','sre'] 且已存在停用 sre 行 → 整组跳过。

    旧 seed 逐条 INSERT：chat 先落 enabled=1，sre 撞 (runtime_id, tag) 唯一键
    1062 被整体吞成 log.warning → 半组启用、调用方却以为没播种。修复后只要
    存在任何角色行（含 enabled=0）就整组不动，由 ensure_role_caps 收口。
    """
    client = _client()
    token = _make_token("b3396r-16")
    rid = _register(client, token, "b3396r-dev16", {"name": "codearts", "role_tags": ["sre"]})
    assert _role_caps(rid) == ["sre"]
    with db_lock:
        db.cursor().execute(
            "UPDATE engine_capabilities SET enabled=0 WHERE runtime_id=? AND tag='sre'", (rid,))
        db.commit()
    assert _role_caps(rid) == []
    _register(client, token, "b3396r-dev16",
              {"name": "codearts", "role_tags": ["chat", "sre"]})
    assert _role_caps(rid) == [], "声明组被部分启用（chat 落了 enabled=1）"
    with db_lock:
        rows = db.cursor().execute(
            "SELECT tag, enabled FROM engine_capabilities WHERE runtime_id=?", (rid,)).fetchall()
    assert [tuple(x) for x in rows if ":" not in x[0]] == [("sre", 0)], rows


def test_user_set_tags_are_not_overwritten():
    """tags_user_set=1（UI 改过）→ 声明的 role_tags 不覆盖用户配置。"""
    client = _client()
    token = _make_token("b3396r-6")
    rid = _register(client, token, "b3396r-dev6", {"name": "codearts", "role_tags": ["sre"]})
    assert _role_caps(rid) == ["sre"]
    with db_lock:
        c = db.cursor()
        c.execute("DELETE FROM engine_capabilities WHERE runtime_id=? AND tag NOT LIKE '%:%'", (rid,))
        c.execute("INSERT INTO engine_capabilities (runtime_id, tag, max_concurrent, priority, enabled) "
                  "VALUES (?, 'coding', 1, 50, 1)", (rid,))
        c.execute("UPDATE agent_runtimes SET tags_user_set=1 WHERE id=?", (rid,))
        db.commit()
    rid2 = _register(client, token, "b3396r-dev6", {"name": "codearts", "role_tags": ["sre"]})
    assert rid2 == rid
    assert _role_caps(rid) == ["coding"], _role_caps(rid)


def test_ui_set_roles_not_overwritten_without_user_flag():
    """已有非 all 角色行（接口写入）→ 即使 tags_user_set=0 也不覆盖。"""
    client = _client()
    token = _make_token("b3396r-7")
    rid = _register(client, token, "b3396r-dev7", {"name": "hermes", "role_tags": ["sre"]})
    with db_lock:
        c = db.cursor()
        c.execute("DELETE FROM engine_capabilities WHERE runtime_id=? AND tag NOT LIKE '%:%'", (rid,))
        c.execute("INSERT INTO engine_capabilities (runtime_id, tag, max_concurrent, priority, enabled) "
                  "VALUES (?, 'coding', 1, 50, 1)", (rid,))
        c.execute("INSERT INTO engine_capabilities (runtime_id, tag, max_concurrent, priority, enabled) "
                  "VALUES (?, 'qa', 1, 50, 1)", (rid,))
        db.commit()
    _register(client, token, "b3396r-dev7", {"name": "hermes", "role_tags": ["sre"]})
    assert _role_caps(rid) == ["coding", "qa"], _role_caps(rid)


def test_seed_reads_single_role_source(monkeypatch):
    """播种值域与 AGENT_TAGS 同源：临时给它加一个角色，声明即被播种（不被第二份字面量过滤）。"""
    from src.domains.runtime.routes import runtime as _rt
    monkeypatch.setattr(_rt, "AGENT_TAGS", set(_rt.AGENT_TAGS) | {"demo-role"}, raising=True)
    client = _client()
    token = _make_token("b3396r-9")
    rid = _register(client, token, "b3396r-dev9", {"name": "codearts", "role_tags": ["demo-role"]})
    with db_lock:
        rows = db.cursor().execute(
            "SELECT tag FROM engine_capabilities WHERE runtime_id=? AND enabled=1", (rid,)).fetchall()
    tags = sorted(r[0] for r in rows if ":" not in r[0])
    assert tags == ["demo-role"], tags


def test_heartbeat_backfills_legacy_user_set_empty_roles():
    """tags_user_set=1 且**零角色行**（旧版「清空全部角色」遗留）→ 心跳自愈补 all。

    2026-11-24 裁定：清空已被前端 + 接口双层禁止（`EMPTY_ROLE_TAGS`），该状态只剩
    历史遗留。旧心跳用 `if not tags_user_set` 做闸门，这类引擎永久 fail-closed 且
    没有任何链路能补回；现在心跳无条件走 ensure_role_caps（内部只补零角色行的引擎）。
    """
    client = _client()
    token = _make_token("b3396r-10")
    rid = _register(client, token, "b3396r-dev10", {"name": "hermes"})
    assert _role_caps(rid) == ["all"]
    with db_lock:
        c = db.cursor()
        c.execute("DELETE FROM engine_capabilities WHERE runtime_id=? AND tag NOT LIKE '%:%'", (rid,))
        c.execute("UPDATE agent_runtimes SET role_tags='', tags_user_set=1 WHERE id=?", (rid,))
        db.commit()
    assert _role_caps(rid) == []
    r = client.post("/api/runtime/heartbeat", headers={"Authorization": f"Bearer {token}"},
                    json={"runtime_ids": [rid]})
    assert r.status_code == 200, r.text
    assert _role_caps(rid) == ["all"], _role_caps(rid)


def test_heartbeat_keeps_user_set_roles_when_rows_exist():
    """tags_user_set=1 且已有角色行 → 心跳不按声明覆盖用户配置（A3 不回归）。"""
    client = _client()
    token = _make_token("b3396r-11")
    rid = _register(client, token, "b3396r-dev11", {"name": "codearts", "role_tags": ["sre"]})
    assert _role_caps(rid) == ["sre"]
    with db_lock:
        db.cursor().execute(
            "UPDATE agent_runtimes SET role_tags='[\"qa\"]', tags_user_set=1 WHERE id=?", (rid,))
        db.commit()
    r = client.post("/api/runtime/heartbeat", headers={"Authorization": f"Bearer {token}"},
                    json={"runtime_ids": [rid]})
    assert r.status_code == 200, r.text
    assert _role_caps(rid) == ["sre"], _role_caps(rid)


def test_heartbeat_does_not_resurrect_disabled_roles():
    """去掉 `tags_user_set` 闸门后的最大风险：只有 enabled=0 行时不得被心跳复活。

    `has_role_rows` 把停用行也算「已配置」（连续停用最后一个角色会被接口 400 拦住，
    停用态只能来自显式操作或历史数据），心跳必须尊重它 —— 否则用户停用的引擎会
    在下一跳自动变回可调度。
    """
    client = _client()
    token = _make_token("b3396r-12")
    rid = _register(client, token, "b3396r-dev12", {"name": "hermes"})
    assert _role_caps(rid) == ["all"]
    with db_lock:
        db.cursor().execute("UPDATE engine_capabilities SET enabled=0 WHERE runtime_id=?", (rid,))
        db.cursor().execute("UPDATE agent_runtimes SET tags_user_set=1 WHERE id=?", (rid,))
        db.commit()
    assert _role_caps(rid) == []
    r = client.post("/api/runtime/heartbeat", headers={"Authorization": f"Bearer {token}"},
                    json={"runtime_ids": [rid]})
    assert r.status_code == 200, r.text
    assert _role_caps(rid) == [], "停用行被心跳复活了"


def test_heartbeat_backfills_declared_not_all_for_legacy_user_set():
    """user_set=1 且零角色行、但有声明 → 补声明集合，不得放大成 all。"""
    client = _client()
    token = _make_token("b3396r-13")
    rid = _register(client, token, "b3396r-dev13", {"name": "codearts", "role_tags": []})
    with db_lock:
        db.cursor().execute("DELETE FROM engine_capabilities WHERE runtime_id=?", (rid,))
        db.cursor().execute(
            "UPDATE agent_runtimes SET role_tags='[\"sre\"]', tags_user_set=1 WHERE id=?", (rid,))
        db.commit()
    assert _role_caps(rid) == []
    r = client.post("/api/runtime/heartbeat", headers={"Authorization": f"Bearer {token}"},
                    json={"runtime_ids": [rid]})
    assert r.status_code == 200, r.text
    assert _role_caps(rid) == ["sre"], _role_caps(rid)
