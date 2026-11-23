"""Issue #3846 回归守卫：撞号迁移必须收敛，renumber 后注册契约必须钉死。

事故：runtime 域 202611180001（ensure-cloud-sandboxes-in-runtime）在存量库被
issue2761-skill-classification-columns 盖章，而该迁移无 post_check —— db_setup
撞号守卫的 `post_check is None` 分支只 log.error + continue，建表 DDL 永不执行，
`grape_t_cloud_sandboxes` 只能靠 mgmt 域误建的空表顶替（#3527 空表遮蔽）。

本文件钉住四层：
1. renumber：旧号 202611180001 不再注册；新号 202611230010 以
   name=ensure-cloud-sandboxes-in-runtime、domains=("runtime",)、post_check 注册；
2. post_check 三态：缺表 / 缺索引 → raise（不盖章）；表 + 三索引在位 → 通过；
3. 撞号对照（对照 db_setup 守卫语义）：带 post_check 的同号不同名迁移进
   reconcile（重跑幂等 fn + 校验通过后改盖新名）；无 post_check 仅 ERROR 不进
   reconcile（fail-safe 保持，但必须报出）；
4. CI 门禁：register_migration 注册表同一 version 重复登记即失败（探针自证：
   重复登记必须被守卫报 ERROR 且能被唯一性检查检出）。
"""
import sqlite3
from collections import Counter
from pathlib import Path

import pytest

from src.api import db_setup as D
from src.domains.runtime.migrations import ensure_cloud_sandboxes_table as mig

OLD_VERSION = "202611180001"
NEW_VERSION = "202611230010"
MIG_NAME = "ensure-cloud-sandboxes-in-runtime"
STAMPED_OTHER_NAME = "issue2761-skill-classification-columns"


def _fresh_sqlite():
    conn = sqlite3.connect(":memory:")
    conn.execute(D._SCHEMA_MIGRATIONS_DDL)
    conn.execute(
        "INSERT INTO grape_t_schema_migrations (version, name, kind, applied_at) "
        "VALUES (?, ?, 'legacy-stamp', '2026-10-30 00:00:00')",
        (D._LEGACY_STAMP_VERSION, D._LEGACY_STAMP_NAME))
    return conn


def _stale_stamp(conn, version, name):
    conn.execute(
        "INSERT INTO grape_t_schema_migrations (version, name, kind, applied_at) "
        "VALUES (?, ?, 'safe', '2026-11-18 00:00:00')",
        (version, name))
    conn.commit()


def _ledger_name(conn, version):
    row = conn.execute(
        "SELECT name FROM grape_t_schema_migrations WHERE version=?", (version,)
    ).fetchone()
    return row[0] if row else ""


def _apply_real_ddl(conn):
    """用迁移模块的真实 DDL/_INDEXES 建齐表与三索引（不复制 SQL 字面量）。"""
    c = conn.cursor()
    c.execute(mig._DDL)
    for idx in mig._INDEXES:
        c.execute(idx)
    conn.commit()


# ===== 1. renumber 注册契约 =====

def test_old_version_not_registered():
    reg = {m.version for m in D._MIGRATIONS}
    assert OLD_VERSION not in reg, f"撞号旧版本仍被注册: {OLD_VERSION}"


def test_new_version_registered_with_postcheck():
    reg = {m.version: m for m in D._MIGRATIONS}
    m = reg.get(NEW_VERSION)
    assert m is not None, f"renumber 后的新号未注册: {NEW_VERSION}"
    assert m.name == MIG_NAME
    assert m.domains == ("runtime",)
    assert callable(m.post_check), "ensure-cloud-sandboxes 必须带后置校验（#3846 兜底）"


def test_migration_module_docstring_and_logs_use_new_version():
    """文件头 docstring 与日志文案不得再引用旧号（防止排查时按旧号 grep 误导）。"""
    src = Path(mig.__file__).read_text(encoding="utf-8")
    assert OLD_VERSION not in src, "迁移文件内仍残留旧版本号 202611180001"


# ===== 2. post_check 三态 =====

def test_postcheck_flags_missing_table():
    conn = sqlite3.connect(":memory:")
    try:
        with pytest.raises(RuntimeError, match="grape_t_cloud_sandboxes"):
            mig._postcheck_cloud_sandboxes(conn=conn)
    finally:
        conn.close()


def test_postcheck_flags_missing_indexes():
    conn = sqlite3.connect(":memory:")
    try:
        conn.execute(mig._DDL)   # 只建表、不建索引
        conn.commit()
        with pytest.raises(RuntimeError, match="索引"):
            mig._postcheck_cloud_sandboxes(conn=conn)
    finally:
        conn.close()


def test_postcheck_passes_when_table_and_indexes_present():
    conn = sqlite3.connect(":memory:")
    try:
        _apply_real_ddl(conn)
        assert sorted(mig._postcheck_cloud_sandboxes(conn=conn)) == sorted(
            ("idx_cloud_sandboxes_repo", "idx_cloud_sandboxes_state",
             "idx_cloud_sandboxes_task"))
    finally:
        conn.close()


# ===== 3. 撞号对照：带 post_check 进 reconcile；无 post_check 仅 ERROR =====

def test_collision_with_postcheck_reconciles(tmp_path):
    """同号不同名且目标缺失 → 重跑幂等 fn，后置校验通过后改盖新名。"""
    conn = sqlite3.connect(str(tmp_path / "mig.db"))
    try:
        conn.execute(D._SCHEMA_MIGRATIONS_DDL)
        _stale_stamp(conn, NEW_VERSION, STAMPED_OTHER_NAME)
        calls = []

        def _fn():
            calls.append("fn")
            _apply_real_ddl(conn)

        D._run_migrations_with_conn(conn, registry=[
            D.Migration(NEW_VERSION, MIG_NAME, "safe", _fn,
                        post_check=lambda: mig._postcheck_cloud_sandboxes(conn=conn)),
        ])
        assert calls == ["fn"], "撞号且目标缺失时未重跑幂等迁移（未进 reconcile）"
        assert _ledger_name(conn, NEW_VERSION) == MIG_NAME, "收敛后未改盖新名"
    finally:
        conn.close()


def test_collision_without_postcheck_errors_and_skips(tmp_path, caplog):
    """无 post_check：保持 fail-safe 跳过，但必须 ERROR 报出（禁止静默）。"""
    conn = sqlite3.connect(str(tmp_path / "mig.db"))
    try:
        conn.execute(D._SCHEMA_MIGRATIONS_DDL)
        _stale_stamp(conn, NEW_VERSION, STAMPED_OTHER_NAME)
        calls = []
        with caplog.at_level("ERROR"):
            D._run_migrations_with_conn(conn, registry=[
                D.Migration(NEW_VERSION, MIG_NAME, "safe", lambda: calls.append(1)),
            ])
        assert calls == [], "无后置校验的撞号项不应执行迁移 fn"
        assert _ledger_name(conn, NEW_VERSION) == STAMPED_OTHER_NAME, "台账被误改"
        errors = [r for r in caplog.records if r.levelname == "ERROR"]
        assert any("撞号" in r.getMessage() for r in errors), "撞号未报 ERROR"
    finally:
        conn.close()


# ===== 4. CI 门禁：同一 version 重复登记即失败 =====

def test_registry_versions_unique():
    """注册表（含 **dict 展开登记的域迁移）version 必须全局唯一。"""
    dups = {v: c for v, c in Counter(m.version for m in D._MIGRATIONS).items() if c > 1}
    assert not dups, f"register_migration 注册表版本号重复: {dups}"


def test_duplicate_registration_reports_error_and_is_detectable(caplog):
    """门禁探针：人为重复登记同号 → 守卫报 ERROR，且唯一性检查能检出（CI 判红）。"""
    snapshot = list(D._MIGRATIONS)
    dup_version = snapshot[0].version
    prev_name = snapshot[0].name
    probe_name = "issue3846-probe-duplicate-registration"
    try:
        with caplog.at_level("ERROR"):
            D.register_migration(dup_version, probe_name, "safe", fn=lambda: None)
        assert any("撞号" in r.getMessage() and dup_version in r.getMessage()
                   and prev_name in r.getMessage()
                   for r in caplog.records), "重复登记未报 ERROR（含 version 与占用名）"
        dups = {v: c for v, c in Counter(m.version for m in D._MIGRATIONS).items()
                if c > 1}
        assert dups == {dup_version: 2}, f"唯一性门禁未检出重复登记: {dups}"
    finally:
        D._MIGRATIONS[:] = snapshot   # 探针还原，不留痕
    assert len(D._MIGRATIONS) == len(snapshot)
