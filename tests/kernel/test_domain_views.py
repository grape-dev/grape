"""跨库视图自检的纯逻辑用例（用假连接，不连库）。

事故背景：insight 库的 `managed_repos` 是视图，基表加了 `is_org_repo` 后视图没重建，
非 owner 侧读该列 1054 → `/api/dashboard/{owner}/{repo}` 500。这里锁死判定与修复边界：
- 缺列/多列/类型漂移 → stale/type_drift，可修（按 owner 列集重建）；
- 同一表在多库都是基表（归属不明）→ ambiguous，**不修**（不能瞎猜 owner）；
- 视图引用的基表已不存在 → orphan，只告警；
- 无 SHOW VIEW 权限（定义为空）→ unreadable，只告警；
- DDL 只认白名单标识符。
"""
import pytest

from src.kernel import domain_views as dv


class _Cur:
    def __init__(self, store, executed):
        self.store = store
        self.executed = executed
        self._rows = []

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params=None):
        flat = " ".join(str(sql).split())
        if "FROM information_schema.tables" in flat:
            wanted = list(params or [])
            self._rows = [(db, tbl, typ) for (db, tbl), typ in self.store["types"].items()
                          if db in wanted]
        elif "FROM information_schema.columns" in flat:
            db, tbl = params
            self._rows = list(self.store["cols"].get((db, tbl), {}).items())
        elif "FROM information_schema.views" in flat:
            db, view = params
            self._rows = [(self.store["views"].get((db, view)),)]
        else:
            self.executed.append((flat, params))
            self._rows = []
        return None

    def fetchall(self):
        return list(self._rows)

    def fetchone(self):
        return self._rows[0] if self._rows else None


class _Conn:
    def __init__(self, store):
        self.store = store
        self.executed = []

    def cursor(self):
        return _Cur(self.store, self.executed)


def _store(types, cols, views=None):
    return {"types": types, "cols": cols, "views": views or {}}


def test_missing_column_makes_view_stale():
    store = _store(
        {("dev_runtime", "managed_repos"): "VIEW",
         ("dev_insight", "managed_repos"): "VIEW",
         ("dev_mgmt", "managed_repos"): "BASE TABLE"},
        {("dev_mgmt", "managed_repos"): {"id": "int", "is_org_repo": "tinyint"},
         ("dev_insight", "managed_repos"): {"id": "int"}},
    )
    scan = dv.scan_own(_Conn(store), "dev_insight", ("dev_mgmt", "dev_runtime"))
    assert len(scan["stale"]) == 1
    view, _tbl, owner, missing, extra = scan["stale"][0]
    assert view == "managed_repos" and owner == "dev_mgmt"
    assert missing == ["is_org_repo"] and extra == []


def test_extra_column_also_rebuilds():
    store = _store(
        {("dev_insight", "t1"): "VIEW", ("dev_mgmt", "t1"): "BASE TABLE"},
        {("dev_mgmt", "t1"): {"a": "int"},
         ("dev_insight", "t1"): {"a": "int", "gone": "int"}},
    )
    scan = dv.scan_own(_Conn(store), "dev_insight", ("dev_mgmt",))
    assert scan["stale"][0][3] == [] and scan["stale"][0][4] == ["gone"]


def test_type_drift_is_reported():
    store = _store(
        {("dev_insight", "t1"): "VIEW", ("dev_mgmt", "t1"): "BASE TABLE"},
        {("dev_mgmt", "t1"): {"a": "bigint(20)"},
         ("dev_insight", "t1"): {"a": "int(11)"}},
    )
    scan = dv.scan_own(_Conn(store), "dev_insight", ("dev_mgmt",))
    assert scan["stale"] == []
    assert scan["type_drift"][0][3] == [("a", "bigint(20)", "int(11)")]


def test_ambiguous_owner_is_not_repaired():
    store = _store(
        {("dev_insight", "t1"): "VIEW",
         ("dev_mgmt", "t1"): "BASE TABLE",
         ("dev_runtime", "t1"): "BASE TABLE"},
        {("dev_mgmt", "t1"): {"a": "int"}, ("dev_runtime", "t1"): {"a": "int", "b": "int"},
         ("dev_insight", "t1"): {"a": "int"}},
    )
    scan = dv.scan_own(_Conn(store), "dev_insight", ("dev_mgmt", "dev_runtime"))
    assert scan["stale"] == []
    assert scan["ambiguous"][0][0] == "t1" and len(scan["ambiguous"][0][1]) == 2


def test_orphan_and_unreadable_views_are_reported_only():
    store = _store(
        {("dev_insight", "v_orphan"): "VIEW", ("dev_insight", "v_hidden"): "VIEW"},
        {},
        {("dev_insight", "v_orphan"): "select `x`.`y`.`z` AS `z` from `dev_mgmt`.`gone` `z`",
         ("dev_insight", "v_hidden"): None},
    )
    scan = dv.scan_own(_Conn(store), "dev_insight", ("dev_mgmt",))
    assert scan["orphan"] and scan["orphan"][0][0] == "v_orphan"
    assert scan["unreadable"] == ["v_hidden"]
    assert scan["stale"] == []


def test_repair_uses_owner_columns_and_only_touches_own_db():
    store = _store(
        {("dev_insight", "managed_repos"): "VIEW", ("dev_mgmt", "managed_repos"): "BASE TABLE"},
        {("dev_mgmt", "managed_repos"): {"id": "int", "is_org_repo": "tinyint"},
         ("dev_insight", "managed_repos"): {"id": "int"}},
    )
    conn = _Conn(store)
    scan = dv.scan_own(conn, "dev_insight", ("dev_mgmt",))
    fixed = dv.repair_own(conn, "dev_insight", scan)
    assert fixed == ["managed_repos"]
    sql = conn.executed[0][0]
    assert sql.startswith("CREATE OR REPLACE VIEW `managed_repos`")
    assert "`dev_mgmt`.`managed_repos`.`is_org_repo`" in sql
    assert "`dev_insight`" not in sql          # 视图名不带库前缀（调用方已 USE）


def test_ddl_rejects_injected_identifiers():
    with pytest.raises(dv.ViewDDLError):
        dv.build_view_ddl("dev_mgmt", "t1", ["a`; drop table x; --"])
    with pytest.raises(dv.ViewDDLError):
        dv.build_view_ddl("dev_mgmt; drop", "t1", ["a"])


def test_dry_run_reports_without_executing():
    store = _store(
        {("dev_insight", "t1"): "VIEW", ("dev_mgmt", "t1"): "BASE TABLE"},
        {("dev_mgmt", "t1"): {"a": "int", "b": "int"}, ("dev_insight", "t1"): {"a": "int"}},
    )
    conn = _Conn(store)
    scan = dv.scan_own(conn, "dev_insight", ("dev_mgmt",))
    assert dv.repair_own(conn, "dev_insight", scan, dry_run=True) == ["t1"]
    assert conn.executed == []


def test_scripts_shell_reexports_kernel_impl():
    """scripts/view_ddl_builder.py 只做转发，DDL 只有一份实现。"""
    import importlib.util
    import os
    import sys

    root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    path = os.path.join(root, "scripts", "view_ddl_builder.py")
    spec = importlib.util.spec_from_file_location("_vdb_shim", path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["_vdb_shim"] = mod
    spec.loader.exec_module(mod)
    assert mod.build_view_ddl is dv.build_view_ddl
    assert mod.ViewDDLError is dv.ViewDDLError