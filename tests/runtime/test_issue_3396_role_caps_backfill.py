"""Issue #3396 后续：角色能力行「空则补齐」的回归用例。

背景：注册链 / 本机发现链原来用「engine_capabilities 已有任意一行」判定跳过补
`all`，而派生行（engine:/scene:/scope:）也算一行。结果是「只剩派生行」的引擎
再也补不回角色行，而 #3396 起空角色行 fail-closed（A7）→ 该引擎对任何角色都
不可调度（基线靠 role_tags 默认映射兜底，不会死）。

本文件锁四条口径（engine/role_caps.ensure_role_caps）：
1. 只剩派生行 + 无声明 → 补 `all`
2. 只剩派生行 + 有声明 → 按声明补（声明即收窄，不补 all）
3. tags_user_set=1（旧版允许清空，现已双层禁止）→ 也补（历史遗留自愈）
4. 已有角色行 → 不补（不覆盖）
外加一条端到端：本机发现的 UPDATE 分支现在也会补齐。
"""
import secrets

import pytest

from src.kernel.session_manager import db, db_lock
from src.domains.runtime.engine.role_caps import ensure_role_caps, role_rows
from src.domains.runtime.public import role_caps_inventory


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

    def test_multi_role_declaration_backfills_all_roles(self):
        """QA C-1：一次声明多个角色必须整组落库，不能只写第一个。"""
        rid = _mk_engine("b3396bf-m", role_tags='["sre","qa"]')
        written = ensure_role_caps(db.cursor(), rid)
        db.commit()
        assert sorted(written) == ["qa", "sre"], written
        caps = _caps(rid)
        assert "sre" in caps and "qa" in caps, caps
        assert "all" not in caps

    def test_illegal_declaration_falls_back_to_all(self):
        rid = _mk_engine("b3396bf-c", role_tags='["not-a-role"]')
        written = ensure_role_caps(db.cursor(), rid)
        db.commit()
        assert written == ["all"]

    def test_user_set_empty_roles_is_backfilled_as_legacy(self):
        """旧版允许「清空全部角色」会置 tags_user_set=1；清空已被禁止后这类行属历史遗留，
        必须自愈补 all，否则引擎永久不可派发、UI 只显示「未分配」。"""
        rid = _mk_engine("b3396bf-d", user_set=1)
        written = ensure_role_caps(db.cursor(), rid)
        db.commit()
        assert written == ["all"]
        assert ("all" in [t for t in _caps(rid) if ":" not in t])

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


def test_role_caps_inventory_ignores_non_domain_tags():
    """架构守护 M4：统计口径必须与派发守卫一致 —— 值域外的平铺行不算角色。

    旧实现用 `tag NOT LIKE '%:%'`，一条手工插入的 `bogus` 停用行会让卡片显示
    「已停用」，而派发守卫只认 all/短角色名 → 该引擎其实照样可派发（假阳性）。
    """
    rid = _mk_engine("b3396bf-m4")
    with db_lock:
        c = db.cursor()
        c.execute("INSERT INTO engine_capabilities (runtime_id, tag, enabled) VALUES (?,?,0)", (rid, "bogus"))
        db.commit()
    inv = role_caps_inventory([rid])
    assert rid not in inv or inv[rid]["total"] == 0, inv


def test_role_caps_inventory_scopes_to_given_ids():
    """架构守护 M2：按页内 id 统计，不做无 WHERE 全表聚合。"""
    rid_a = _mk_engine("b3396bf-m2a", roles=["all"])
    rid_b = _mk_engine("b3396bf-m2b", roles=["all"])
    inv = role_caps_inventory([rid_a])
    assert inv.get(rid_a) == {"total": 1, "enabled": 1}, inv
    assert rid_b not in inv, inv
    assert role_caps_inventory([]) == {}



def test_role_tag_domain_order_is_stable():
    """QA 复审 Info：值域顺序必须稳定（set 元组化会随哈希种子变序，400 文案会抖）。"""
    from src.domains.runtime.public import role_tag_domain
    d = role_tag_domain()
    assert d[0] == "all"
    assert list(d[1:]) == sorted(d[1:]), d
    assert role_tag_domain() == d


def test_ensure_role_caps_logs_backfill(caplog):
    """QA 复审 Medium：自愈写入改变派发可用性，成功路径必须留痕。"""
    import logging
    rid = _mk_engine("b3396bf-log")
    with db_lock:
        db.cursor().execute("DELETE FROM engine_capabilities WHERE runtime_id=?", (rid,))
        db.commit()
    with caplog.at_level(logging.INFO):
        written = ensure_role_caps(db.cursor(), rid, declared=[], user_set=0, source="probe")
    assert written == ["all"], written
    assert any("role caps backfilled" in r.getMessage() and str(rid) in r.getMessage()
               for r in caplog.records), [r.getMessage() for r in caplog.records]


def test_ensure_skips_when_role_row_appears_concurrently():
    """QA 复审 Medium：原子性 —— 预检之后才出现的角色行必须让插入自行放弃。

    直接构造「预检已过、写前库里已有 chat 行」的时序：先让 has_role_rows 返回假，
    再由另一条路径写入 chat，然后调用 ensure；单条 INSERT ... WHERE NOT EXISTS
    应在库侧看到 chat 后跳过，不会叠出 all。
    """
    rid = _mk_engine("b3396bf-race")
    with db_lock:
        db.cursor().execute("DELETE FROM engine_capabilities WHERE runtime_id=?", (rid,))
        db.commit()

    with db_lock:
        c = db.cursor()
        # 模拟并发写者先落 chat（预检与插入之间的窗口）
        c.execute("INSERT INTO engine_capabilities (runtime_id, tag, enabled) VALUES (?,?,1)", (rid, "chat"))
        db.commit()
        written = ensure_role_caps(c, rid, declared=[], user_set=0, source="probe")
        db.commit()
    assert written == [], written
    tags = sorted(t for (t,) in db.cursor().execute(
        "SELECT tag FROM engine_capabilities WHERE runtime_id=?", (rid,)).fetchall())
    assert tags == ["chat"], tags
