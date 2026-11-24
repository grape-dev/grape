"""Issue #3396 后续：角色能力行「空则补齐」的回归用例。

背景：注册链 / 本机发现链原来用「engine_capabilities 已有任意一行」判定跳过补
`all`，而派生行（engine:/scene:/scope:）也算一行。结果是「只剩派生行」的引擎
再也补不回角色行，而 #3396 起空角色行 fail-closed（A7）→ 该引擎对任何角色都
不可调度（基线靠 role_tags 默认映射兜底，不会死）。

本文件锁四条口径（engine/role_caps.ensure_role_caps）：
1. 只剩派生行 + 无声明 → 补 `all`
2. 只剩派生行 + 有声明 → 按声明补（声明即收窄，不补 all）
3. tags_user_set=1（UI 手工清空）→ 不补（尊重用户意图）
4. 已有角色行 → 不补（不覆盖）
外加一条端到端：本机发现的 UPDATE 分支现在也会补齐。
"""
import secrets

import pytest

from src.kernel.session_manager import db, db_lock
from src.domains.runtime.engine.role_caps import ensure_role_caps, role_rows


@pytest.fixture(autouse=True)
def _clean():
    def _run():
        with db_lock:
            c = db.cursor()
            c.execute("DELETE FROM engine_capabilities WHERE runtime_id IN "
                      "(SELECT id FROM agent_runtimes WHERE name LIKE 'b3396bf-%')")
            c.execute("DELETE FROM agent_runtimes WHERE name LIKE 'b3396bf-%'")
            db.commit()
    _run()
    yield
    _run()


def _mk_engine(name, role_tags="", user_set=0, creds=(), derived=True, roles=()):
    """建一台本机直连引擎：derived=True 时只写派生行（模拟“只剩派生行”）。"""
    with db_lock:
        c = db.cursor()
        c.execute(
            "INSERT INTO agent_runtimes (name, credential_id, scope, owner_id, host_name, "
            "installed, healthy, status, role_tags, tags_user_set) "
            "VALUES (?, 0, 'user', '44', 'b3396bf-host', 1, 1, 'online', ?, ?)",
            (name, role_tags, user_set))
        rid = c.lastrowid
        tags = list(creds) + (["engine:opencode", "scene:default", "scope:global"] if derived else [])
        tags += list(roles)
        for t in tags:
            c.execute("INSERT INTO engine_capabilities (runtime_id, tag, enabled) VALUES (?,?,1)", (rid, t))
        db.commit()
        return rid


def _caps(rid):
    with db_lock:
        rows = db.cursor().execute(
            "SELECT tag FROM engine_capabilities WHERE runtime_id=? AND enabled=1 ORDER BY tag",
            (rid,)).fetchall()
    return [r[0] for r in rows]


class TestEnsureRoleCaps:
    def test_derived_only_without_declaration_backfills_all(self):
        rid = _mk_engine("b3396bf-a")
        _tags, roles = role_rows(db.cursor(), rid)
        assert roles == []                      # 前置：只有派生行
        written = ensure_role_caps(db.cursor(), rid)
        db.commit()
        assert written == ["all"]
        assert "all" in _caps(rid)

    def test_derived_only_with_declaration_backfills_declared(self):
        rid = _mk_engine("b3396bf-b", role_tags='["sre"]')
        written = ensure_role_caps(db.cursor(), rid)
        db.commit()
        assert written == ["sre"], written
        assert "all" not in _caps(rid)
        assert "sre" in _caps(rid)

    def test_illegal_declaration_falls_back_to_all(self):
        rid = _mk_engine("b3396bf-c", role_tags='["not-a-role"]')
        written = ensure_role_caps(db.cursor(), rid)
        db.commit()
        assert written == ["all"]

    def test_user_set_empty_roles_is_left_alone(self):
        rid = _mk_engine("b3396bf-d", user_set=1)
        written = ensure_role_caps(db.cursor(), rid)
        db.commit()
        assert written == []
        assert all(":" in t for t in _caps(rid))   # 仍然只有派生行

    def test_existing_role_rows_not_overwritten(self):
        rid = _mk_engine("b3396bf-e", roles=("chat",))
        written = ensure_role_caps(db.cursor(), rid)
        db.commit()
        assert written == []
        assert "all" not in _caps(rid)

    def test_unknown_runtime_is_noop(self):
        assert ensure_role_caps(db.cursor(), 987654321) == []


class TestLocalDiscoveryBackfill:
    def test_local_discovery_update_path_backfills_all(self):
        """端到端：只剩派生行的本机引擎，下一次本地发现（UPDATE 分支）补回 all。"""
        from src.domains.runtime.public import upsert_local_runtimes
        name = "b3396bf-" + secrets.token_hex(3)
        rid = _mk_engine(name)
        assert all(":" in t for t in _caps(rid))
        upsert_local_runtimes(
            [{"name": name, "display_name": name, "installed": True, "healthy": True,
              "cli_path": "/bin/true", "version": "0", "pip_package": ""}],
            "user", "44", "b3396bf-host", "2026-11-23T00:00:00+00:00")
        assert "all" in _caps(rid), _caps(rid)

    def test_local_discovery_respects_declaration(self):
        from src.domains.runtime.public import upsert_local_runtimes
        name = "b3396bf-" + secrets.token_hex(3)
        rid = _mk_engine(name, role_tags='["sre"]')
        upsert_local_runtimes(
            [{"name": name, "display_name": name, "installed": True, "healthy": True,
              "cli_path": "/bin/true", "version": "0", "pip_package": ""}],
            "user", "44", "b3396bf-host", "2026-11-23T00:00:00+00:00")
        assert "sre" in _caps(rid) and "all" not in _caps(rid), _caps(rid)


def test_disabled_role_row_is_not_reopened():
    """enabled=0 的角色行 = 显式停用：补齐必须视作「已配置」，不补、不重开、不撞唯一键。"""
    rid = _mk_engine("b3396bf-f")
    with db_lock:
        c = db.cursor()
        c.execute("INSERT INTO engine_capabilities (runtime_id, tag, enabled) VALUES (?,?,0)", (rid, "all"))
        db.commit()
    _tags, roles = role_rows(db.cursor(), rid)
    assert roles == []                       # 生效面看：没有启用的角色行
    assert ensure_role_caps(db.cursor(), rid) == []   # 但补齐判定：已配置 → 不动
    db.commit()
    with db_lock:
        rows = db.cursor().execute(
            "SELECT tag, enabled FROM engine_capabilities WHERE runtime_id=? AND tag='all'", (rid,)).fetchall()
    assert rows == [("all", 0)], rows        # 仍是停用态，且没有第二行（无 1062）


def test_ensure_is_idempotent_when_disabled_row_exists():
    """重复调用不会再尝试插入（线上 probe/心跳每轮都会调）。"""
    rid = _mk_engine("b3396bf-g")
    with db_lock:
        c = db.cursor()
        c.execute("INSERT INTO engine_capabilities (runtime_id, tag, enabled) VALUES (?,?,0)", (rid, "all"))
        db.commit()
    for _ in range(3):
        assert ensure_role_caps(db.cursor(), rid) == []
        db.commit()


def test_role_caps_inventory_counts_disabled_rows():
    """管理台要能区分「没配过」与「配过但全停用」——统计必须含 enabled=0 的行。"""
    from src.domains.runtime.public import role_caps_inventory

    rid_off = _mk_engine("b3396inv-off")
    rid_on = _mk_engine("b3396inv-on")
    with db_lock:
        c = db.cursor()
        c.execute("INSERT INTO engine_capabilities (runtime_id, tag, enabled) VALUES (?,?,0)", (rid_off, "all"))
        c.execute("INSERT INTO engine_capabilities (runtime_id, tag, enabled) VALUES (?,?,1)", (rid_on, "all"))
        db.commit()
    inv = role_caps_inventory()
    assert inv.get(rid_off) == {"total": 1, "enabled": 0}, inv.get(rid_off)
    assert inv.get(rid_on) == {"total": 1, "enabled": 1}, inv.get(rid_on)

