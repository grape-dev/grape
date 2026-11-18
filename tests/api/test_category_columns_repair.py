"""category_dict 补列事故（2026-11-18）回归守卫。

事故：迁移 `202611171846` 通过 `_ensure_column` 补 `scene_market` / `skill` 的
`cat_l1_id` / `cat_l2_id` / `tags`。当进程「判定不出属主域 / 不是该表属主」时
`_ensure_column` 静默 `return False`，而 fn 正常返回 → 执行器盖章 → 列永不创建、
也不再重试：

  · `/api/runtime/scene-tags` 500 `Unknown column 'cat_l1_id'`（该端点走
    `mgmt.list_scene_market`，读 mgmt 自己的 `scene_market`）→ 引擎详情弹窗
    「场景标签」整块空白，「角色标签」随之塌成只剩「全能」；
  · `skill_sync` 每轮 ERROR（runtime 读自己库的 `skill`，同一根因）。

本文件只钉**真正治这个事故**的四层；消费域视图重建已从本 PR 摘除（仓内没有任何
代码读 `scene_market` / `skill` 的视图，理由见 PR 讨论）：

  1. `_ensure_column(raise_on_guard=True)` 被域归属守卫跳过时必须抛错；
  2. **默认（不传）仍保持静默跳过** —— 存量 14 个 `strict=True` 调用点里有 7 条迁移
     的属主域与注册域不一致，全局改语义会把它们一起引爆；
  3. `202611181200` 按 mgmt 域幂等补 `scene_market` 两列；`202611181201` 不再存在
     （补 skill 列归基线 `202611180003`，视图重建随功能摘除）；
  4. `scene_market.tags` 旧逗号串回填失败必须抛错（原来吞掉 = 列在、数据没收敛、
     永不重试）。
"""
import ast
import pathlib
import sqlite3

import pytest

from src.api import db_setup as D

REPO = pathlib.Path(__file__).resolve().parents[2]


class _Cur:
    """最小游标：只为 _ensure_column 的 PRAGMA 分支服务。"""

    def __init__(self, conn):
        self._conn = conn

    def execute(self, sql):
        return self._conn.cursor().execute(sql)


def test_raise_on_guard_raises_when_owner_guard_skips(monkeypatch):
    monkeypatch.setattr(D, "_owned_skip", lambda *a, **k: True)
    with pytest.raises(RuntimeError) as ei:
        D._ensure_column(_Cur(sqlite3.connect(":memory:")), "scene_market",
                         "cat_l1_id", "INTEGER DEFAULT 0", raise_on_guard=True)
    assert "域归属守卫跳过" in str(ei.value)


def test_default_keeps_legacy_silent_skip(monkeypatch):
    """默认语义必须不变：存量迁移（属主域≠注册域那 7 条）不能被这次修复引爆。"""
    monkeypatch.setattr(D, "_owned_skip", lambda *a, **k: True)
    assert D._ensure_column(_Cur(sqlite3.connect(":memory:")), "scene_market",
                            "cat_l1_id", "INTEGER DEFAULT 0") is False
    # strict=True 也不该改守卫分支语义（strict 只管 DDL 降级链失败）
    assert D._ensure_column(_Cur(sqlite3.connect(":memory:")), "scene_market",
                            "cat_l1_id", "INTEGER DEFAULT 0", strict=True) is False


def test_repair_migration_registered_mgmt_only():
    reg = {m.version: m for m in D._MIGRATIONS}
    assert "202611181200" in reg
    mig = reg["202611181200"]
    assert mig.domains == ("mgmt",) and mig.name.endswith("repair-mgmt")
    assert mig.kind == "safe"
    assert "202611181201" not in reg, "视图重建迁移应已摘除（无读方 + 目标拓扑无权限）"


def test_repair_plan_is_mgmt_only():
    """skill 三列由基线 202611180003（runtime 域）负责，本 PR 不得再补一遍。"""
    plans = {p[0]: p for p in D._CATEGORY_COLUMN_PLANS}
    assert set(plans) == {"mgmt"}
    assert {c for c, _ in plans["mgmt"][2]} == {"cat_l1_id", "cat_l2_id"}
    assert plans["mgmt"][1] == "scene_market"


def test_repair_fn_adds_columns_on_sqlite(monkeypatch, tmp_path):
    """走真实 sqlite：缺列 → 补上；再跑一次幂等。"""
    conn = sqlite3.connect(str(tmp_path / "repair.db"))
    conn.execute("CREATE TABLE scene_market (id INTEGER PRIMARY KEY, name TEXT)")
    conn.commit()
    monkeypatch.setattr(D, "db", conn)
    monkeypatch.setattr(D, "_owned_skip", lambda *a, **k: False)

    D._fn_category_columns_repair("mgmt")
    cols = [r[1] for r in conn.execute("PRAGMA table_info(scene_market)").fetchall()]
    assert {"cat_l1_id", "cat_l2_id"} <= set(cols)
    D._fn_category_columns_repair("mgmt")  # 幂等
    assert len([c for c in cols if c.startswith("cat_")]) == 2


def test_repair_fn_rejects_unknown_domain(monkeypatch):
    with pytest.raises(RuntimeError):
        D._fn_category_columns_repair("insight")


def test_repair_fn_uses_raise_on_guard():
    """补列迁移必须 opt-in 严格语义（否则又回到「静默跳过还盖章」）。"""
    src = (REPO / "src" / "api" / "db_setup.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    fn = next(n for n in ast.walk(tree)
              if isinstance(n, ast.FunctionDef) and n.name == "_fn_category_columns_repair")
    calls = [n for n in ast.walk(fn)
             if isinstance(n, ast.Call) and getattr(n.func, "id", "") == "_ensure_column"]
    assert calls, "未找到 _ensure_column 调用"
    for call in calls:
        kw = {k.arg: k.value for k in call.keywords}
        assert getattr(kw.get("raise_on_guard"), "value", None) is True, \
            "_fn_category_columns_repair 的 _ensure_column 必须传 raise_on_guard=True"


def test_legacy_migration_is_mgmt_only_and_declares_domain():
    """202611171846 只碰 mgmt 属主的列，且**显式**声明 domains。"""
    reg = {m.version: m for m in D._MIGRATIONS}
    assert reg["202611171846"].domains == ("mgmt",)
    src = (REPO / "src" / "api" / "db_setup.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    # 必须显式写 domains：register_migration 缺省值也是 ("mgmt",)，只断言 mig.domains
    # 是恒真断言（删掉显式声明测试照样绿，反转探针实测踩到）。
    reg_calls = [n for n in ast.walk(tree) if isinstance(n, ast.Call)
                 and getattr(n.func, "id", "") == "register_migration"
                 and any(getattr(k.value, "value", None) == "202611171846" for k in n.keywords)]
    assert reg_calls and any(k.arg == "domains" for k in reg_calls[0].keywords), \
        '202611171846 必须显式声明 domains=("mgmt",)，不能靠缺省值'
    fn = next(n for n in ast.walk(tree)
              if isinstance(n, ast.FunctionDef) and n.name == "_fn_category_dict_and_tags")
    tables = {n.value for n in ast.walk(fn)
              if isinstance(n, ast.Constant) and n.value in ("scene_market", "skill")}
    assert tables == {"scene_market"}, f"该迁移不得碰 runtime 属主的表: {tables}"


def test_tags_backfill_failure_propagates(monkeypatch, tmp_path):
    """回填失败必须抛错（不盖章、下轮重试），不许 try/except 吞掉。"""
    conn = sqlite3.connect(str(tmp_path / "backfill.db"))
    conn.execute(
        "CREATE TABLE scene_market (id INTEGER PRIMARY KEY, name TEXT,"
        " cat_l1_id INTEGER DEFAULT 0, cat_l2_id INTEGER DEFAULT 0, tags TEXT DEFAULT '')")
    conn.commit()
    monkeypatch.setattr(D, "db", conn)
    monkeypatch.setattr(D, "_owned_skip",
                        lambda tbl, what: tbl == "grape_t_category_dict")

    import src.domains.mgmt.user.db as MU

    def _boom():
        raise RuntimeError("backfill boom")

    monkeypatch.setattr(MU, "backfill_scene_market_legacy_tags", _boom)
    with pytest.raises(RuntimeError, match="backfill boom"):
        D._fn_category_dict_and_tags()


def test_ensure_column_keeps_its_docstring():
    assert D._ensure_column.__doc__ and "Idempotent column add" in D._ensure_column.__doc__


def test_skill_columns_are_owned_by_baseline_migration():
    """基线补 skill 三列的迁移必须存在且带 runtime 域（本 PR 依赖它）。"""
    reg = {m.version: m for m in D._MIGRATIONS}
    assert "202611180003" in reg, "基线补 skill 列的迁移不见了（rebase 冲突可能吞掉它）"
    assert reg["202611180003"].domains == ("runtime",)
    assert reg["202611180003"].name.endswith("skill-classification-columns")
