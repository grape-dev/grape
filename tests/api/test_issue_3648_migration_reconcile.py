"""Issue #3648 回归守卫：台账已盖章但目标对象缺失时必须收敛，不能静默跳过。

事故：分类收敛 / 技能分类列迁移经历多次 renumber，dev 三库的版本台账已被
旧号（202611180003=`skill-intro-override-schema`、202611180004=
`category-collapse-three-domains`）盖章。执行器只按 version 判 pending →
本分支同号迁移被永久判「已执行」，老分类残留、skill 缺 cat_l1_id/cat_l2_id，
而读路径又吞异常，最终表现为「分类树是旧的 + 技能列表为空」且无报错。

本文件钉住四层：
1. 撞号迁移 renumber 到未占用号（旧号不再注册）；
2. 同号不同名且目标缺失 → 重跑幂等 fn + 后置校验通过后才改盖新名；
3. 目标已经到位 → 不重跑；后置校验失败 → 不盖章；
4. 分类收敛 / 技能列迁移自带后置校验；list_skills 不再吞 schema 异常。
"""
import sqlite3
from pathlib import Path

import pytest
from fastapi import HTTPException

from src.api import db_setup as D

REPO = Path(__file__).resolve().parents[2]

OLD_SKILL_COLUMNS = "202611180003"
OLD_CATEGORY_MGMT = "202611180004"
OLD_CATEGORY_RUNTIME = "202611181304"
NEW_SKILL_COLUMNS = "202611210001"
NEW_CATEGORY_MGMT = "202611210002"
NEW_CATEGORY_RUNTIME = "202611210003"


def _fresh_sqlite(tmp_path):
    conn = sqlite3.connect(str(tmp_path / "mig.db"))
    conn.execute(D._SCHEMA_MIGRATIONS_DDL)
    conn.execute(
        "INSERT INTO grape_t_schema_migrations (version, name, kind, applied_at) "
        "VALUES (?, ?, 'legacy-stamp', '2026-10-30 00:00:00')",
        (D._LEGACY_STAMP_VERSION, D._LEGACY_STAMP_NAME))
    conn.commit()
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


# ===== 1. renumber =====

def test_issue_3648_versions_are_renumbered_and_registered():
    reg = {m.version: m for m in D._MIGRATIONS}

    for old in (OLD_SKILL_COLUMNS, OLD_CATEGORY_MGMT, OLD_CATEGORY_RUNTIME):
        assert old not in reg, f"撞号旧版本仍被注册: {old}"

    skill = reg[NEW_SKILL_COLUMNS]
    assert skill.name == "issue2761-skill-classification-columns"
    assert skill.domains == ("runtime",)
    assert callable(skill.post_check), "技能分类列迁移必须带后置校验"

    mgmt = reg[NEW_CATEGORY_MGMT]
    assert mgmt.name == "category-collapse-three-domains-mgmt"
    assert mgmt.domains == ("mgmt",)
    assert callable(mgmt.post_check), "mgmt 分类收敛必须带后置校验"

    runtime = reg[NEW_CATEGORY_RUNTIME]
    assert runtime.name == "category-collapse-three-domains-runtime"
    assert runtime.domains == ("runtime",)
    assert callable(runtime.post_check), "runtime 分类收敛必须带后置校验"

    # 排序后必须是「先补列、后收敛」：runtime 收敛依赖 cat_l1_id 已存在
    assert NEW_SKILL_COLUMNS < NEW_CATEGORY_RUNTIME


# ===== 2. 台账 applied 但目标缺失：旧逻辑跳过（红），新逻辑重跑（绿） =====

def test_ledger_applied_target_missing_skips_without_postcheck(tmp_path):
    """还原旧语义：同号不同名且无后置校验 → fn 不执行（这是本 Issue 的红）。"""
    conn = _fresh_sqlite(tmp_path)
    _stale_stamp(conn, "202611180003", "skill-intro-override-schema")
    conn.execute("CREATE TABLE demo_probe (id INTEGER)")
    conn.commit()
    calls = []
    try:
        D._run_migrations_with_conn(conn, registry=[
            D.Migration("202611180003", "issue2761-skill-classification-columns",
                        "safe", lambda: calls.append(1)),
        ])
        assert calls == [], "无后置校验的撞号项应保持 fail-safe 跳过"
    finally:
        conn.close()


def test_ledger_applied_target_missing_reconciles_with_postcheck(tmp_path, caplog):
    """新语义：台账旧名已盖章但目标缺失 → 重跑 fn，后置校验通过后改盖新名。"""
    conn = _fresh_sqlite(tmp_path)
    _stale_stamp(conn, "202611180003", "skill-intro-override-schema")
    conn.execute("CREATE TABLE demo_probe (id INTEGER)")
    conn.commit()
    calls = []

    def _fn():
        calls.append("fn")
        conn.execute("ALTER TABLE demo_probe ADD COLUMN cat_l1_id INTEGER DEFAULT 0")
        conn.commit()

    def _post_check():
        cols = [r[1] for r in conn.execute("PRAGMA table_info(demo_probe)").fetchall()]
        if "cat_l1_id" not in cols:
            raise RuntimeError("demo_probe.cat_l1_id 缺失")

    try:
        with caplog.at_level("WARNING"):
            D._run_migrations_with_conn(conn, registry=[
                D.Migration("202611180003", "issue2761-skill-classification-columns",
                            "safe", _fn, post_check=_post_check),
            ])
        assert calls == ["fn"], "目标缺失时未重跑幂等迁移"
        assert _ledger_name(conn, "202611180003") == \
            "issue2761-skill-classification-columns", "收敛后未改盖新名"
        assert any("撞号" in r.getMessage() for r in caplog.records), \
            "重跑撞号迁移必须留痕"
    finally:
        conn.close()


def test_ledger_applied_target_present_does_not_rerun(tmp_path):
    """目标已经到位：只校验、不重跑（避免每次启动重复 ALTER）。"""
    conn = _fresh_sqlite(tmp_path)
    _stale_stamp(conn, "202611180003", "skill-intro-override-schema")
    conn.execute("CREATE TABLE demo_probe (id INTEGER, cat_l1_id INTEGER DEFAULT 0)")
    conn.commit()
    calls = []

    def _post_check():
        cols = [r[1] for r in conn.execute("PRAGMA table_info(demo_probe)").fetchall()]
        if "cat_l1_id" not in cols:
            raise RuntimeError("demo_probe.cat_l1_id 缺失")

    try:
        D._run_migrations_with_conn(conn, registry=[
            D.Migration("202611180003", "issue2761-skill-classification-columns",
                        "safe", lambda: calls.append(1), post_check=_post_check),
        ])
        assert calls == [], "目标已到位仍重跑了迁移 fn"
        assert _ledger_name(conn, "202611180003") == "skill-intro-override-schema"
    finally:
        conn.close()


def test_postcheck_failure_does_not_stamp(tmp_path, caplog):
    """后置校验仍失败 → ERROR 且不盖章（下次启动继续重试）。"""
    conn = _fresh_sqlite(tmp_path)
    _stale_stamp(conn, "202611180003", "skill-intro-override-schema")
    conn.execute("CREATE TABLE demo_probe (id INTEGER)")
    conn.commit()

    def _post_check():
        raise RuntimeError("目标仍未到位")

    try:
        with caplog.at_level("ERROR"):
            D._run_migrations_with_conn(conn, registry=[
                D.Migration("202611180003", "issue2761-skill-classification-columns",
                            "safe", lambda: None, post_check=_post_check),
            ])
        assert _ledger_name(conn, "202611180003") == "skill-intro-override-schema", \
            "后置校验失败仍改盖了新名（假绿）"
        errors = [r for r in caplog.records if r.levelname == "ERROR"]
        assert any("后置校验" in r.getMessage() or "目标" in r.getMessage()
                   for r in errors), "后置校验失败未落 ERROR"
    finally:
        conn.close()


# ===== 3. 两个收敛迁移自带后置校验 =====

def test_category_collapse_postcheck_requires_three_active_l1(tmp_path):
    from src.domains.mgmt.migrations.category_collapse_dict import (
        verify_category_collapse,
    )

    conn = sqlite3.connect(str(tmp_path / "cat.db"))
    conn.execute(
        "CREATE TABLE grape_t_category_dict (id INTEGER PRIMARY KEY, slug TEXT, "
        "level INTEGER, parent_id INTEGER, is_active INTEGER)")
    for i in (1001, 1002, 1010, 2001):
        conn.execute(
            "INSERT INTO grape_t_category_dict (id, slug, level, parent_id, is_active) "
            "VALUES (?,?,1,0,1)", (i, f"s{i}"))
    conn.commit()
    with pytest.raises(RuntimeError):
        verify_category_collapse(conn=conn)
    conn.execute("UPDATE grape_t_category_dict SET is_active=0 WHERE id=2001")
    conn.commit()
    assert verify_category_collapse(conn=conn) == sorted([1001, 1002, 1010])
    conn.close()


def test_skill_columns_postcheck_detects_missing(tmp_path):
    from src.domains.runtime.migrations.skill_detail_tables import (
        verify_skill_classification_columns,
    )

    conn = sqlite3.connect(str(tmp_path / "skill.db"))
    conn.execute("CREATE TABLE skill (id INTEGER PRIMARY KEY)")
    conn.commit()
    with pytest.raises(RuntimeError):
        verify_skill_classification_columns(conn=conn)
    conn.execute("ALTER TABLE skill ADD COLUMN cat_l1_id INTEGER DEFAULT 0")
    conn.execute("ALTER TABLE skill ADD COLUMN cat_l2_id INTEGER DEFAULT 0")
    conn.commit()
    assert verify_skill_classification_columns(conn=conn) == ["cat_l1_id", "cat_l2_id"]
    conn.close()


# ===== 4. list_skills 不再吞 schema 异常 =====

def test_list_skills_propagates_schema_error(monkeypatch, caplog):
    from src.domains.mgmt.routes import skills as skills_route

    def _boom(**_kwargs):
        raise RuntimeError("no such column: cat_l1_id")

    monkeypatch.setattr(skills_route._rt_skills, "skill_search", _boom)
    user = {"id": "3648", "username": "t3648", "org_id": "", "auth_type": "jwt"}
    with caplog.at_level("ERROR"), pytest.raises(HTTPException) as exc:
        skills_route.list_skills(user=user)
    assert exc.value.status_code == 503
    assert any(r.levelname == "ERROR" and "skill_search" in r.getMessage()
               for r in caplog.records), "list_skills 未落 ERROR 日志"


# ===== 5. 前端分类缓存必须在进入视图时刷新 =====

def test_skills_view_does_not_serve_session_cache_forever():
    src = (REPO / "src/api/frontend/app/src/views/SkillsView.vue").read_text(encoding="utf-8")
    assert "if (categories.value.length) return" not in src, \
        "loadCategories 的会话级缓存未失效，管理员更新分类后仍显示旧结构"

# ===== 6. 启动期总校验（列在 + 仅 3 个活跃一级） =====

def test_verify_migration_targets_flags_missing_skill_columns(tmp_path):
    conn = sqlite3.connect(str(tmp_path / "runtime.db"))
    conn.execute("CREATE TABLE skill (id INTEGER PRIMARY KEY)")
    conn.commit()
    failures = D.verify_migration_targets(conn=conn, domain="runtime")
    assert any("cat_l1_id" in f for f in failures), failures
    with pytest.raises(RuntimeError):
        D.verify_migration_targets(conn=conn, domain="runtime", raise_on_fail=True)
    conn.close()


def test_verify_migration_targets_flags_extra_active_l1(tmp_path):
    conn = sqlite3.connect(str(tmp_path / "mgmt.db"))
    conn.execute(
        "CREATE TABLE grape_t_category_dict (id INTEGER PRIMARY KEY, level INTEGER, "
        "is_active INTEGER)")
    for i in (1001, 1002, 1010, 2001):
        conn.execute(
            "INSERT INTO grape_t_category_dict (id, level, is_active) VALUES (?,1,1)", (i,))
    conn.commit()
    failures = D.verify_migration_targets(conn=conn, domain="mgmt")
    assert any("一级活跃分类" in f for f in failures), failures
    with pytest.raises(RuntimeError):
        D.verify_migration_targets(conn=conn, domain="mgmt", raise_on_fail=True)
    conn.close()
