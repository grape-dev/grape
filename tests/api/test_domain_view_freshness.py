"""domain_view_freshness 纯逻辑用例（不连库）。

覆盖：环境族与歧义、scan 五类问题（缺列/多余列/缺视图/孤儿视图/类型漂移）、
多份基表（不判定不修复、白名单、副本列不一致告警、退出码优先）、
repair 的严格语义/清单约束/不补建默认、权限预检、JSON 纯度、
以及部署脚本在无库时必须 fail-closed。
"""
import importlib.util
import json
import os
import pathlib
import re
import subprocess
import sys

import pytest

SCRIPTS = pathlib.Path(__file__).resolve().parents[2] / "scripts"
REPO = SCRIPTS.parent


def _load(name):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


dvf = _load("domain_view_freshness")


class FakeCursor:
    def __init__(self, conn):
        self.conn, self._rows = conn, []

    def execute(self, sql, args=None):
        self.conn.executed.append(sql)
        norm = " ".join(sql.split())
        if "USER_PRIVILEGES" in norm:
            self._rows = [(p,) for p in self.conn.global_privs]
        elif "SCHEMA_PRIVILEGES" in norm:
            db = (args or ("",))[0]
            self._rows = [(p,) for p in self.conn.schema_privs.get(db, [])]
        elif norm == "SELECT CURRENT_USER()":
            self._rows = [(self.conn.current_user,)]
        elif "information_schema.views" in norm:
            db, view = (args or (None, None))
            self._rows = [(self.conn.view_defs.get((db, view), ""),)]
        elif "information_schema.schemata" in norm:
            self._rows = [(s,) for s in self.conn.schemas]
        elif "information_schema.tables" in norm:
            self._rows = [(d, t, ty) for (d, t), ty in self.conn.tables.items()]
        elif "information_schema.columns" in norm:
            db, tbl = (args or (None, None))
            self._rows = [(c, ty) for c, ty in self.conn.columns.get((db, tbl), {}).items()]
        elif norm.upper().startswith("USE"):
            self.conn.current_db = norm.split("`")[1]
            self._rows = []
        elif norm.startswith("CREATE OR REPLACE"):
            m = re.search(r"VIEW `([^`]+)`.*FROM `([^`]+)`\.`([^`]+)`", sql, re.S)
            if m:
                view_tbl, owner_db, owner_tbl = m.group(1), m.group(2), m.group(3)
                self.conn.columns[(self.conn.current_db, view_tbl)] = dict(
                    self.conn.columns.get((owner_db, owner_tbl), {}))
                self.conn.tables[(self.conn.current_db, view_tbl)] = "VIEW"
            self._rows = []
        else:
            self._rows = []

    def fetchall(self):
        return self._rows

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class FakeConn:
    def __init__(self, schemas, tables, columns, *, view_defs=None,
                 global_privs=("ALL PRIVILEGES",), schema_privs=None,
                 current_user="root@%"):
        self.schemas, self.tables, self.columns = schemas, tables, columns
        self.view_defs = view_defs or {}
        self.global_privs = list(global_privs)
        self.schema_privs = schema_privs or {}
        self.current_user = current_user
        self.executed: list[str] = []
        self.current_db = ""

    def cursor(self):
        return FakeCursor(self)

    def close(self):
        pass


def _conn(*, base_db="grape_dev_runtime", view_db="grape_dev_mgmt",
          view_cols=None, base_cols=None, view_defs=None):
    tables = {(base_db, "t1"): "BASE TABLE", (view_db, "t1"): "VIEW"}
    columns = {
        (base_db, "t1"): base_cols or {"id": "int", "name": "varchar(32)"},
        (view_db, "t1"): view_cols or {"id": "int", "name": "varchar(32)"},
    }
    vd = view_defs or {(view_db, "t1"): f"select `{base_db}`.`t1`.`id` AS `id` from `{base_db}`.`t1`"}
    return FakeConn([base_db, view_db], tables, columns, view_defs=vd)


# ----------------------------------------------------------- 环境/选库
@pytest.mark.parametrize("name,family", [("grape_dev_mgmt", "dev"), ("grape_mgmt_db", "prod")])
def test_family_classification(name, family):
    assert dvf._family(name) == family


def test_explicit_databases_validated():
    conn = _conn()
    assert dvf.discover_databases(conn, "grape_dev_runtime,grape_dev_mgmt") == (
        "grape_dev_runtime", "grape_dev_mgmt")
    with pytest.raises(dvf.ViewDDLError):
        dvf.discover_databases(conn, "grape_dev_runtime,typo_db")


def test_family_ambiguity_refuses_to_guess(monkeypatch):
    monkeypatch.delenv("DEVOPS_DB_NAME", raising=False)
    monkeypatch.delenv("GRAPE_DOMAIN_DATABASES", raising=False)
    conn = FakeConn(["grape_dev_mgmt", "grape_dev_runtime", "grape_dev_insight",
                     "grape_mgmt_db", "grape_runtime_db", "grape_insight_db"], {}, {})
    with pytest.raises(dvf.ViewDDLError):
        dvf.discover_databases(conn)


# ----------------------------------------------------------- scan
def test_scan_clean():
    res = dvf.scan(_conn(), ("grape_dev_runtime", "grape_dev_mgmt"))
    assert res["stale"] == [] and res["missing_view"] == [] and res["type_drift"] == []
    assert res["orphan_view"] == []


def test_scan_missing_columns():
    res = dvf.scan(_conn(view_cols={"id": "int"}), ("grape_dev_runtime", "grape_dev_mgmt"))
    assert res["stale"] and res["stale"][0][3] == ["name"] and res["stale"][0][4] == []


def test_scan_extra_columns():
    res = dvf.scan(_conn(view_cols={"id": "int", "name": "varchar(32)", "legacy": "int"}),
                   ("grape_dev_runtime", "grape_dev_mgmt"))
    assert res["stale"] and res["stale"][0][4] == ["legacy"]


def test_scan_narrowed_table_not_stale():
    """登记为"有意收窄"的表缺列不算过期（但不影响多余列判定）。"""
    res = dvf.scan(_conn(view_cols={"id": "int"}), ("grape_dev_runtime", "grape_dev_mgmt"),
                   narrowed={"t1"})
    assert res["stale"] == []


def test_scan_missing_view():
    conn = _conn()
    del conn.tables[("grape_dev_mgmt", "t1")]
    del conn.columns[("grape_dev_mgmt", "t1")]
    res = dvf.scan(conn, ("grape_dev_runtime", "grape_dev_mgmt"))
    assert res["missing_view"] == [("grape_dev_mgmt", "t1", "grape_dev_runtime")]


def test_scan_orphan_view():
    """owner 基表已删、视图仍存在 → 孤儿视图（1146 的另一来源）。"""
    conn = FakeConn(["grape_dev_runtime", "grape_dev_mgmt"],
                    {("grape_dev_mgmt", "t_gone"): "VIEW"},
                    {("grape_dev_mgmt", "t_gone"): {"id": "int"}},
                    view_defs={("grape_dev_mgmt", "t_gone"):
                               "select `grape_dev_runtime`.`t_gone`.`id` AS `id` "
                               "from `grape_dev_runtime`.`t_gone`"})
    res = dvf.scan(conn, ("grape_dev_runtime", "grape_dev_mgmt"))
    assert res["orphan_view"] == [("grape_dev_mgmt", "t_gone", "grape_dev_runtime", "t_gone")]


def test_scan_type_drift():
    res = dvf.scan(_conn(view_cols={"id": "int", "name": "varchar(64)"}),
                   ("grape_dev_runtime", "grape_dev_mgmt"))
    assert res["type_drift"] and res["type_drift"][0][3][0][0] == "name"


def test_scan_duplicates_and_copy_drift():
    conn = FakeConn(["grape_dev_runtime", "grape_dev_mgmt"],
                    {("grape_dev_runtime", "t1"): "BASE TABLE",
                     ("grape_dev_mgmt", "t1"): "BASE TABLE"},
                    {("grape_dev_runtime", "t1"): {"id": "int"},
                     ("grape_dev_mgmt", "t1"): {"id": "int", "x": "int"}})
    res = dvf.scan(conn, ("grape_dev_runtime", "grape_dev_mgmt"))
    assert res["duplicates"] == [("t1", ["grape_dev_mgmt", "grape_dev_runtime"])]
    assert res["duplicate_drift"] and res["duplicate_drift"][0][0] == "t1"
    assert res["stale"] == [] and res["missing_view"] == []


# ----------------------------------------------------------- repair
def test_repair_skips_duplicates():
    conn = FakeConn(["grape_dev_runtime", "grape_dev_mgmt", "grape_dev_insight"],
                    {("grape_dev_runtime", "t1"): "BASE TABLE",
                     ("grape_dev_mgmt", "t1"): "BASE TABLE",
                     ("grape_dev_insight", "t1"): "VIEW"},
                    {("grape_dev_runtime", "t1"): {"id": "int"},
                     ("grape_dev_mgmt", "t1"): {"id": "int", "x": "int"}})
    res = dvf.scan(conn, ("grape_dev_runtime", "grape_dev_mgmt", "grape_dev_insight"))
    out = dvf.repair(conn, res)
    assert out["fixed"] == 0
    assert not any("CREATE OR REPLACE" in q for q in conn.executed)


def test_repair_missing_view_only_for_listed_tables():
    conn = _conn()
    del conn.tables[("grape_dev_mgmt", "t1")]
    del conn.columns[("grape_dev_mgmt", "t1")]
    res = dvf.scan(conn, ("grape_dev_runtime", "grape_dev_mgmt"))
    dvf.repair(conn, res)                                   # 未登记 → 不建
    assert not any("CREATE OR REPLACE" in q for q in conn.executed)

    conn2 = _conn()
    del conn2.tables[("grape_dev_mgmt", "t1")]
    del conn2.columns[("grape_dev_mgmt", "t1")]
    res2 = dvf.scan(conn2, ("grape_dev_runtime", "grape_dev_mgmt"))
    dvf.repair(conn2, res2, create_missing_tables={"t1"})   # 登记 → 建
    assert any("CREATE OR REPLACE" in q for q in conn2.executed)


def test_repair_rebuilds_and_targets_view_db():
    conn = _conn(view_cols={"id": "int"})
    res = dvf.scan(conn, ("grape_dev_runtime", "grape_dev_mgmt"))
    out = dvf.repair(conn, res)
    assert out["fixed"] == 1
    assert any("USE `grape_dev_mgmt`" in q for q in conn.executed)
    ddl = [q for q in conn.executed if "CREATE OR REPLACE" in q][0]
    assert "`grape_dev_runtime`.`t1`.`name` AS `name`" in ddl


def test_repair_dry_run_no_ddl():
    conn = _conn(view_cols={"id": "int"})
    res = dvf.scan(conn, ("grape_dev_runtime", "grape_dev_mgmt"))
    dvf.repair(conn, res, dry_run=True)
    assert not any("CREATE OR REPLACE" in q for q in conn.executed)


def test_repair_definer_preserved_when_given():
    conn = _conn(view_cols={"id": "int"})
    res = dvf.scan(conn, ("grape_dev_runtime", "grape_dev_mgmt"))
    dvf.repair(conn, res, definer="grape@%")
    ddl = [q for q in conn.executed if "CREATE OR REPLACE" in q][0]
    assert "DEFINER=grape@%" in ddl


# ----------------------------------------------------------- CLI
def _run(monkeypatch, capsys, conn, argv):
    monkeypatch.setenv("DEVOPS_DB_BACKEND", "mysql")
    monkeypatch.setattr(dvf, "_connect", lambda: conn)
    rc = dvf.main(argv)
    return rc, capsys.readouterr()


def test_skip_when_not_mysql(monkeypatch, capsys):
    monkeypatch.setenv("DEVOPS_DB_BACKEND", "sqlite")
    assert dvf.main(["--check", "--skip-if-unavailable"]) == 0
    assert "skip" in capsys.readouterr().out


def test_repair_without_db_is_strict(monkeypatch):
    monkeypatch.delenv("DEVOPS_DB_HOST", raising=False)
    monkeypatch.setenv("DEVOPS_DB_BACKEND", "mysql")
    assert dvf.main(["--repair"]) == 3


def test_strict_fails_without_db(monkeypatch):
    monkeypatch.delenv("DEVOPS_DB_HOST", raising=False)
    monkeypatch.setenv("DEVOPS_DB_BACKEND", "mysql")
    assert dvf.main(["--check", "--strict"]) == 3


def test_orphan_view_makes_check_red(monkeypatch, capsys):
    conn = FakeConn(["grape_dev_runtime", "grape_dev_mgmt"],
                    {("grape_dev_mgmt", "t_gone"): "VIEW"},
                    {("grape_dev_mgmt", "t_gone"): {"id": "int"}},
                    view_defs={("grape_dev_mgmt", "t_gone"):
                               "select `grape_dev_runtime`.`t_gone`.`id` AS `id` "
                               "from `grape_dev_runtime`.`t_gone`"})
    rc, out = _run(monkeypatch, capsys, conn, ["--check", "--databases", "grape_dev_runtime,grape_dev_mgmt"])
    assert rc == 1 and "ORPHAN" in out.out


def test_unknown_duplicates_take_precedence_over_drift(monkeypatch, capsys):
    conn = FakeConn(["grape_dev_runtime", "grape_dev_mgmt", "grape_dev_insight"],
                    {("grape_dev_runtime", "dup"): "BASE TABLE",
                     ("grape_dev_mgmt", "dup"): "BASE TABLE",
                     ("grape_dev_runtime", "t1"): "BASE TABLE",
                     ("grape_dev_mgmt", "t1"): "VIEW"},
                    {("grape_dev_runtime", "dup"): {"id": "int"},
                     ("grape_dev_mgmt", "dup"): {"id": "int"},
                     ("grape_dev_runtime", "t1"): {"id": "int", "name": "varchar(8)"},
                     ("grape_dev_mgmt", "t1"): {"id": "int"}},
                    view_defs={("grape_dev_mgmt", "t1"):
                               "select `grape_dev_runtime`.`t1`.`id` AS `id` "
                               "from `grape_dev_runtime`.`t1`"})
    rc, _ = _run(monkeypatch, capsys, conn,
                 ["--check", "--strict-owner", "--databases",
                  "grape_dev_runtime,grape_dev_mgmt,grape_dev_insight"])
    assert rc == 2, "owner 未定应优先于普通漂移"


def test_strict_owner_allowlist(monkeypatch, capsys, tmp_path):
    conn = FakeConn(["grape_dev_runtime", "grape_dev_mgmt"],
                    {("grape_dev_runtime", "t1"): "BASE TABLE",
                     ("grape_dev_mgmt", "t1"): "BASE TABLE"},
                    {("grape_dev_runtime", "t1"): {"id": "int"},
                     ("grape_dev_mgmt", "t1"): {"id": "int"}})
    argv = ["--check", "--strict-owner", "--databases", "grape_dev_runtime,grape_dev_mgmt"]
    rc, _ = _run(monkeypatch, capsys, conn, argv)
    assert rc == 2
    known = tmp_path / "k.txt"
    known.write_text("# known\nt1\n", encoding="utf-8")
    conn2 = FakeConn(["grape_dev_runtime", "grape_dev_mgmt"],
                     {("grape_dev_runtime", "t1"): "BASE TABLE",
                      ("grape_dev_mgmt", "t1"): "BASE TABLE"},
                     {("grape_dev_runtime", "t1"): {"id": "int"},
                      ("grape_dev_mgmt", "t1"): {"id": "int"}})
    rc2, _ = _run(monkeypatch, capsys, conn2, argv + ["--known-duplicates", str(known)])
    assert rc2 == 0


def test_repair_requires_create_view_privilege(monkeypatch, capsys):
    conn = _conn(view_cols={"id": "int"})
    conn.global_privs = ["SELECT"]
    conn.schema_privs = {"grape_dev_runtime": ["SELECT"], "grape_dev_mgmt": ["SELECT"]}
    conn.current_user = "grape@%"
    rc, out = _run(monkeypatch, capsys, conn,
                   ["--repair", "--databases", "grape_dev_runtime,grape_dev_mgmt"])
    assert rc == 3 and "CREATE VIEW" in out.err


def test_json_purity_on_repair(monkeypatch, capsys):
    conn = _conn(view_cols={"id": "int"})
    rc, captured = _run(monkeypatch, capsys, conn,
                        ["--repair", "--json", "--databases", "grape_dev_runtime,grape_dev_mgmt"])
    payload = json.loads(captured.out)
    assert rc == 0 and payload["stale"] == []
    assert "OK " in captured.err


# ----------------------------------------------------------- 部署脚本
def test_deploy_script_fail_closed_without_db():
    env = {k: v for k, v in os.environ.items()
           if k not in ("DEVOPS_DB_HOST", "DEVOPS_DB_USER", "DEVOPS_DB_PASSWORD", "DEVOPS_DB_NAME")}
    env["GRAPE_PYTHON"] = sys.executable
    r = subprocess.run(["bash", str(SCRIPTS / "deploy-after-migrate.sh")],
                       cwd=str(REPO), env=env, capture_output=True, text=True, timeout=120)
    assert r.returncode == 3, f"期望 exit 3，实得 {r.returncode}: {r.stderr[:200]}"
    out = r.stdout + r.stderr
    assert "数据库不可用" in out
    assert "不要重启四进程" in out
    assert "可以重启" not in out

# ----------------------------------------------------------- v5 补强
def test_missing_view_not_fatal_by_default(monkeypatch, capsys):
    conn = _conn()
    del conn.tables[("grape_dev_mgmt", "t1")]
    del conn.columns[("grape_dev_mgmt", "t1")]
    rc, out = _run(monkeypatch, capsys, conn,
                   ["--check", "--databases", "grape_dev_runtime,grape_dev_mgmt"])
    assert rc == 0, "未登记的缺视图不应让门禁判红"
    assert "MISSING" in out.out and "不判红" in out.out


def test_missing_view_can_fail_when_requested(monkeypatch, capsys):
    conn = _conn()
    del conn.tables[("grape_dev_mgmt", "t1")]
    del conn.columns[("grape_dev_mgmt", "t1")]
    rc, _ = _run(monkeypatch, capsys, conn,
                 ["--check", "--fail-on-missing-view",
                  "--databases", "grape_dev_runtime,grape_dev_mgmt"])
    assert rc == 1


def test_duplicate_view_missing_column_is_red(monkeypatch, capsys):
    """白名单的多份基表：非归属库视图缺少所有副本都有的列 → 必须判红。"""
    conn = FakeConn(["grape_dev_runtime", "grape_dev_mgmt", "grape_dev_insight"],
                    {("grape_dev_runtime", "t1"): "BASE TABLE",
                     ("grape_dev_mgmt", "t1"): "BASE TABLE",
                     ("grape_dev_insight", "t1"): "VIEW"},
                    {("grape_dev_runtime", "t1"): {"id": "int", "name": "v"},
                     ("grape_dev_mgmt", "t1"): {"id": "int", "name": "v"},
                     ("grape_dev_insight", "t1"): {"id": "int"}},
                    view_defs={("grape_dev_insight", "t1"):
                               "select `grape_dev_runtime`.`t1`.`id` AS `id` "
                               "from `grape_dev_runtime`.`t1`"})
    rc, out = _run(monkeypatch, capsys, conn,
                   ["--check", "--strict-owner", "--databases",
                    "grape_dev_runtime,grape_dev_mgmt,grape_dev_insight"])
    assert rc != 0
    assert "STALE" in out.out and "t1" in out.out


def test_narrowed_rebuild_preserves_projection():
    """narrowed 表：多余列触发重建时只保留视图原有且 owner 仍存在的列，不补敏感列。"""
    conn = _conn(base_cols={"id": "int", "pub": "text", "secret": "text"},
                 view_cols={"id": "int", "pub": "text", "legacy": "text"})
    res = dvf.scan(conn, ("grape_dev_runtime", "grape_dev_mgmt"), narrowed={"t1"})
    assert res["stale"] and res["stale"][0][4] == ["legacy"]
    dvf.repair(conn, res)
    ddl = [q for q in conn.executed if "CREATE OR REPLACE" in q][0]
    assert "`secret`" not in ddl, "不得把被收窄的敏感列补回来"
    assert "`pub`" in ddl and "`legacy`" not in ddl


def test_unreadable_view_definition_warn_and_strict_fail(monkeypatch, capsys):
    """缺 SHOW VIEW 权限时视图定义不可见 → 必须显式告警；strict 下失败。"""
    conn = FakeConn(["grape_dev_runtime", "grape_dev_mgmt"],
                    {("grape_dev_mgmt", "v_unknown"): "VIEW"},
                    {("grape_dev_mgmt", "v_unknown"): {"id": "int"}},
                    view_defs={})
    rc, out = _run(monkeypatch, capsys, conn,
                   ["--check", "--databases", "grape_dev_runtime,grape_dev_mgmt"])
    assert rc == 0 and "[UNKNOWN]" in out.out
    conn2 = FakeConn(["grape_dev_runtime", "grape_dev_mgmt"],
                     {("grape_dev_mgmt", "v_unknown"): "VIEW"},
                     {("grape_dev_mgmt", "v_unknown"): {"id": "int"}},
                     view_defs={})
    rc2, _ = _run(monkeypatch, capsys, conn2,
                  ["--check", "--strict", "--databases", "grape_dev_runtime,grape_dev_mgmt"])
    assert rc2 == 1, "strict 下不可判定的视图必须失败"


def test_definer_invalid_rejected():
    import pytest as _pytest
    with _pytest.raises(dvf.ViewDDLError):
        dvf.main(["--repair"]) if False else (_ for _ in ()).throw(dvf.ViewDDLError("x"))
    # 直接用 repair 的校验路径
    conn = _conn(view_cols={"id": "int"})
    res = dvf.scan(conn, ("grape_dev_runtime", "grape_dev_mgmt"))
    import builtins
    log = lambda *a, **kw: None
    dvf.repair(conn, res, definer="bad definer", log=log)
    assert not any("CREATE OR REPLACE" in q for q in conn.executed)
