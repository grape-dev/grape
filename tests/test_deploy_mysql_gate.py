#!/usr/bin/env python3
"""tests/test_deploy_mysql_gate.py — 部署门禁脚本单测（不连真库）。

monkeypatch 模拟 mysqldump/mysql 子进程与 pymysql 连接，
覆盖：阶段编排、校验失败中止、--allow-prod 门禁、幂等重入。
"""
from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import types
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
SPEC = importlib.util.spec_from_file_location(
    "deploy_mysql_gate", REPO / "scripts" / "deploy_mysql_gate.py")
gate = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(gate)


class FakeCursor:
    """按 (db, query 前缀) 路由的假游标，模拟一个极小 MySQL。"""
    def __init__(self, world):
        self.world = world

    def execute(self, query, args=()):
        self.last = (query, args)
        return self

    def fetchall(self):
        q, args = self.last
        if "TABLES" in q and "grape_t_schema_migrations" in q:
            db = args[0]
            n = 1 if "grape_t_schema_migrations" in self.world.tables.get(db, set()) else 0
            return [(n,)]
        if "FROM grape_t_schema_migrations" in q:
            return [(self.world.stamps.get(self.last_db, 0),)]
        if "COLUMNS" in q:
            return self.world.columns.get(args[0], [])
        if q == "SHOW TABLES":
            return [(t,) for t in sorted(self.world.tables.get(self.last_db, set()))]
        if "SELECT COUNT(*) FROM `" in q:
            t = q.split("FROM `")[1].split("`")[0]
            return [(self.world.counts.get((self.last_db, t), 0),)]
        if "FROM grape_accounts" in q:
            ids = self.world.account_ids.get(self.last_db, {})
            return [(ids.get(a, None),) for a in args if a in ids]
        return []

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class FakeConn:
    def __init__(self, world, db):
        self.world, self.db = world, db

    def cursor(self):
        c = FakeCursor(self.world)
        c.last_db = self.db
        return c

    def close(self):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class World:
    """可变世界状态：库→表/行数/账号 id/列类型。"""
    def __init__(self):
        self.tables: dict[str, set] = {"grape": {"grape_accounts", "grape_events",
                                                 "grape_t_schema_migrations"},
                                       "grape_upgrade_sim": set()}
        self.counts: dict[tuple, int] = {("grape", "grape_accounts"): 3,
                                         ("grape", "grape_events"): 0}
        self.account_ids: dict[str, dict] = {"grape": {"1": "1", "2": "2", "3": "3"},
                                             "grape_upgrade_sim": {}}
        self.columns: dict[str, list] = {}  # db -> [(tbl,col,dtype)]
        self.stamps: dict[str, int] = {}
        self.migrated: set[str] = set()  # 已执行迁移链的库
        self.dropped: set[str] = set()   # 已被 DROP DATABASE 的库（BLK-G1 断言）
        self.dump_ok = True
        self.migration_calls: list[str] = []

    def restore_and_migrate(self, db):
        self.tables[db] = set(self.tables["grape"])
        for (d, t), n in list(self.counts.items()):
            if d == "grape":
                self.counts[(db, t)] = n
        self.account_ids[db] = dict(self.account_ids["grape"])
        self.columns[db] = [("grape_accounts", "user_id", "varchar")] * 3
        self.tables[db].add("grape_t_schema_migrations")


@pytest.fixture()
def world(monkeypatch):
    w = World()

    def fake_sql_rows(db, query, args=()):
        cur = FakeCursor(w)
        cur.last_db = db
        cur.execute(query, args)
        if "DROP DATABASE" in query:
            w.dropped.add(query.split("`")[1] if "`" in query else db)
        return cur.fetchall()

    monkeypatch.setattr(gate, "sql_rows", fake_sql_rows)

    def fake_run_cmd(cmd, stdin_file=None, stdout_file=None, env=None):
        # QA 复审 M-4：口令不得进 argv（ps/审计/终端历史可读），必须经 MYSQL_PWD env
        assert not any(str(c).startswith("-p") for c in cmd), f"口令进 argv: {cmd}"
        if env is not None:
            assert "MYSQL_PWD" in env, "口令必须经 MYSQL_PWD env 注入"
        if cmd[0] == "mysqldump":
            if not w.dump_ok:
                raise gate.GateError("dump fail")
            assert "--single-transaction" in cmd
            Path(stdout_file).write_text(
                "-- dump\nCREATE TABLE `grape_accounts` (...);\n")
        elif cmd[0] == "mysql":  # 恢复备份 → 模拟 shadow 重建
            db = cmd[-1]
            w.restore_and_migrate(db)
        return subprocess.CompletedProcess(cmd, 0)

    monkeypatch.setattr(gate, "run_cmd", fake_run_cmd)

    def fake_chain(db, migration_cmd):
        w.migration_calls.append(db)
        w.migrated.add(db)
        w.stamps[db] = 42
        return "fake-chain"

    monkeypatch.setattr(gate, "run_migration_chain", fake_chain)
    return w


# ------------------------------------------------------------------ tests ---
def test_backup_phase_creates_validated_dump(world, tmp_path):
    path = gate.phase_backup(str(tmp_path), "grape")
    assert os.path.getsize(path) > 0
    assert "grape" in os.path.basename(path)


def test_backup_fails_on_empty_dump(world, tmp_path, monkeypatch):
    def bad_dump(cmd, stdin_file=None, stdout_file=None, env=None):
        Path(stdout_file).write_text("")
        return subprocess.CompletedProcess(cmd, 0)
    monkeypatch.setattr(gate, "run_cmd", bad_dump)
    with pytest.raises(gate.GateError, match="为空"):
        gate.phase_backup(str(tmp_path), "grape")


def test_backup_fails_without_create_table(world, tmp_path, monkeypatch):
    def bad_dump(cmd, stdin_file=None, stdout_file=None, env=None):
        Path(stdout_file).write_text("-- header only\n")
        return subprocess.CompletedProcess(cmd, 0)
    monkeypatch.setattr(gate, "run_cmd", bad_dump)
    with pytest.raises(gate.GateError, match="CREATE TABLE"):
        gate.phase_backup(str(tmp_path), "grape")


def test_shadow_full_flow_green(world, tmp_path):
    world.tables["grape"].add("grape_t_schema_migrations")
    backup = gate.phase_backup(str(tmp_path), "grape")
    gate.phase_shadow(backup, "grape", "grape_upgrade_sim", None)
    assert "grape_upgrade_sim" in world.migrated
    assert world.stamps["grape_upgrade_sim"] == 42


def test_shadow_rowcount_mismatch_aborts(world, tmp_path, monkeypatch):
    backup = gate.phase_backup(str(tmp_path), "grape")
    # 演练恢复后行数与源不一致（模拟迁移丢数据）
    def bad_run_cmd(cmd, stdin_file=None, stdout_file=None, env=None):
        w = world
        db = cmd[-1]
        w.tables[db] = set(w.tables["grape"])
        for (d, t), n in list(w.counts.items()):
            if d == "grape":
                w.counts[(db, t)] = n - 1  # 少一行
        w.account_ids[db] = dict(w.account_ids["grape"])
        w.columns[db] = [("grape_accounts", "user_id", "varchar")]
        return subprocess.CompletedProcess(cmd, 0)
    monkeypatch.setattr(gate, "run_cmd", bad_run_cmd)
    with pytest.raises(gate.GateError, match="行数不一致"):
        gate.phase_shadow(backup, "grape", "grape_upgrade_sim", None)


def test_shadow_bad_id_type_aborts(world, tmp_path):
    backup = gate.phase_backup(str(tmp_path), "grape")
    # 迁移后 id 变 int 形态
    orig_chain = gate.run_migration_chain

    def chain_int_ids(db, cmd):
        orig_chain(db, cmd)
        world.account_ids[db] = {1: 1, 2: 2, 3: 3}
    monkey = pytest.MonkeyPatch()
    monkey.setattr(gate, "run_migration_chain", chain_int_ids)
    with pytest.raises(gate.GateError, match="id"):
        gate.phase_shadow(backup, "grape", "grape_upgrade_sim", None)
    monkey.undo()


def test_shadow_non_varchar_user_id_aborts(world, tmp_path):
    world.tables["grape"].add("grape_t_schema_migrations")
    orig_chain = gate.run_migration_chain

    def chain_bad_cols(db, cmd):
        orig_chain(db, cmd)
        world.columns[db] = [("grape_accounts", "user_id", "int"),
                             ("grape_events", "user_id", "bigint")]
    monkey = pytest.MonkeyPatch()
    monkey.setattr(gate, "run_migration_chain", chain_bad_cols)
    backup = gate.phase_backup(str(tmp_path), "grape")
    with pytest.raises(gate.GateError, match="不变式"):
        gate.phase_shadow(backup, "grape", "grape_upgrade_sim", None)
    monkey.undo()


def test_no_allow_prod_is_plan_only(world, tmp_path):
    rc = gate.main(["--backup-dir", str(tmp_path)])
    assert rc == 0
    assert "grape" not in world.migrated  # 生产库未被迁移


def test_shadow_eq_source_db_rejected(world, tmp_path):
    """BLK-G1：shadow_db == source_db → GateError，绝不 DROP 源库。"""
    with pytest.raises(gate.GateError, match="演练库名不安全"):
        gate.main(["--backup-dir", str(tmp_path), "--shadow-db", "grape"])
    assert "grape" not in world.dropped  # 校验先于 phase_shadow，源库未被 DROP


def test_shadow_protected_db_name_rejected(world, tmp_path):
    """BLK-G1：演练名命中生产/系统库名单 → GateError（即便 != source_db）。"""
    with pytest.raises(gate.GateError, match="演练库名不安全"):
        gate.main(["--backup-dir", str(tmp_path), "--shadow-db", "mysql"])
    with pytest.raises(gate.GateError, match="演练库名不安全"):
        gate.main(["--backup-dir", str(tmp_path), "--shadow-db", "grape_system_db"])


def test_allow_prod_gate_applies_and_smokes(world, tmp_path):
    rc = gate.main(["--backup-dir", str(tmp_path), "--allow-prod"])
    assert rc == 0
    assert "grape" in world.migrated
    assert "grape_upgrade_sim" in world.migrated  # 演练先于生产


def test_apply_idempotent_reentry(world):
    # 同一库连续两次 apply：迁移链重入安全（盖章去重语义下不报错）
    gate.phase_apply("grape", None)
    gate.phase_apply("grape", None)
    assert world.migration_calls.count("grape") == 2


def test_shadow_phase_idempotent_reentry(world, tmp_path):
    # shadow 重建演练库，重复执行结果一致
    backup = gate.phase_backup(str(tmp_path), "grape")
    world.tables["grape"].add("grape_t_schema_migrations")
    gate.phase_shadow(backup, "grape", "grape_upgrade_sim", None)
    gate.phase_shadow(backup, "grape", "grape_upgrade_sim", None)
    assert world.migration_calls.count("grape_upgrade_sim") == 2


def test_sql_smoke_fails_when_stamps_missing(world):
    world.tables["grape"] = {"grape_accounts"}  # 无盖章表
    with pytest.raises(gate.GateError, match="关键表缺失"):
        gate._sql_smoke("grape")


def test_sql_smoke_fails_when_stamp_table_empty(world):
    world.tables["grape"] = {"grape_accounts", "grape_t_schema_migrations"}
    with pytest.raises(gate.GateError, match="记录不完整"):
        gate._sql_smoke("grape")


def test_api_smoke_skips_on_network_error(caplog):
    # 网络失败 → SKIP 不抛（不判红）
    gate._api_smoke("http://127.0.0.1:1")  # 不可达端口


def test_dump_failure_aborts(world, tmp_path):
    world.dump_ok = False
    with pytest.raises(gate.GateError):
        gate.phase_backup(str(tmp_path), "grape")


def test_structured_log_lines(capfd):
    gate.log("gate_start", phase="backup", k=1)
    out = capfd.readouterr().out.strip().splitlines()[-1]
    rec = json.loads(out)
    assert rec["event"] == "gate_start" and rec["phase"] == "backup"


# -------------------------------------------------- G3 连接注入（不连真库）---
def test_run_migration_chain_injects_target_connection(monkeypatch):
    """主路径：run_migration_chain 走 run_migrations_for_mysql 且携带 TARGET 配置。"""
    gate.TARGET.update(host="192.0.2.9", port=3307, user="root", password="")
    monkeypatch.setattr(gate, "sql_rows",
                        lambda db, q, args=(): [(1,)]
                        if "grape_t_schema_migrations" in q else [])
    captured = {}

    class FakeEntry:
        @staticmethod
        def run_migrations_for_mysql(host, port, user, password, database):
            captured.update(host=host, port=port, user=user, password=password,
                            database=database)

    fake_mod = types.ModuleType("src.api.db_setup")
    fake_mod.run_migrations_for_mysql = FakeEntry.run_migrations_for_mysql
    fake_pkg = types.ModuleType("src.api")
    fake_pkg.db_setup = fake_mod
    fake_src = types.ModuleType("src")
    fake_src.api = fake_pkg
    monkeypatch.setitem(sys.modules, "src", fake_src)
    monkeypatch.setitem(sys.modules, "src.api", fake_pkg)
    monkeypatch.setitem(sys.modules, "src.api.db_setup", fake_mod)
    via = gate.run_migration_chain("grape_upgrade_sim", None)
    assert via == "src.api.db_setup.run_migrations_for_mysql"
    assert captured == {"host": "192.0.2.9", "port": 3307, "user": "root",
                        "password": "", "database": "grape_upgrade_sim"}


def test_mysql_target_swap_restores_env_and_globals():
    """换绑窗口内 env/全局 db 指向目标库；退出后全部还原。"""
    import os
    sys.path.insert(0, str(REPO))
    import src.api.db_setup as ds
    import src.kernel.session_manager as sm
    saved_env = {k: os.environ.get(k) for k in ds._MYSQL_TARGET_ENV_KEYS}
    try:
        os.environ.pop("DEVOPS_DB_BACKEND", None)
        os.environ.pop("DEVOPS_DB_NAME", None)
        saved_setup_db = ds.db
        saved_sm_db = sm.db

        class FakeTargetConn:
            def __init__(self):
                self.closed = False

            def close(self):
                self.closed = True

        created = {}

        class FakeCompatDB(FakeTargetConn):
            def __init__(self, **cfg):
                super().__init__()
                created.update(cfg)

        import types as _t
        mc = _t.ModuleType("src.kernel.mysql_compat")
        mc.MySQLCompatDB = FakeCompatDB  # type: ignore[attr-defined]
        monkey_mod = pytest.MonkeyPatch()
        monkey_mod.setitem(sys.modules, "src.kernel.mysql_compat", mc)
        try:
            with ds._mysql_target_swap("h1", 3307, "u1", "p1", "db1") as conn:
                assert isinstance(conn, FakeCompatDB)
                assert created["host"] == "h1" and created["port"] == 3307
                assert created["database"] == "db1" and created["user"] == "u1"
                assert ds.db is conn and sm.db is conn
                assert os.environ["DEVOPS_DB_BACKEND"] == "mysql"
                assert os.environ["DEVOPS_DB_NAME"] == "db1"
            # 还原断言
            assert ds.db is saved_setup_db and sm.db is saved_sm_db
            assert os.environ.get("DEVOPS_DB_BACKEND") is None
            assert os.environ.get("DEVOPS_DB_NAME") is None
            assert conn.closed
        finally:
            monkey_mod.undo()
    finally:
        for k, v in saved_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
