"""旧账号表清退场景用例 S1–S7（S8 复用既有写冻结守卫）。

背景：生产 user_accounts 0 行、数据已搬到 grape_accounts（归档 94 行可回滚）。
代码里任何对旧表的 SQL 都只能返回空 —— 它不再是兼容层，而是误导。这组用例锁住
「src 全域零触达」这条不变量，防止旧读路径被悄悄加回来。

对应需求：docs/weekly-11/user-accounts-retirement-requirements.md（R1–R7）
对应设计：docs/weekly-11/user-accounts-retirement-design.md（§2 改动面、§4 场景映射）
"""
from __future__ import annotations

import ast
import json
import pathlib

import pytest

_SRC = pathlib.Path(__file__).resolve().parents[2] / "src"
_TABLE = "user_accounts"
# 真正会发 SQL 的入口（只看这些调用的字面量参数，不看注释/docstring）
_SQL_CALLS = {"execute", "read_one", "read_one_any", "read_one_repo", "read_all", "read_all_any"}


def _py_files():
    return [p for p in _SRC.rglob("*.py") if "__pycache__" not in p.parts]


def _sqlish_strings(tree: ast.AST):
    """收集传给 execute()/read_one() 等入口的字面量字符串（含 f-string 的固定片段）。"""
    out = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        fn = node.func
        name = ""
        if isinstance(fn, ast.Attribute):
            name = fn.attr
        elif isinstance(fn, ast.Name):
            name = fn.id
        if name not in _SQL_CALLS:
            continue
        for arg in node.args:
            if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                out.append((arg.lineno, arg.value))
            elif isinstance(arg, ast.JoinedStr):
                for part in arg.values:
                    if isinstance(part, ast.Constant) and isinstance(part.value, str):
                        out.append((part.lineno, part.value))
    return out


def test_s1_no_sql_targets_the_retired_table():
    """S1：src 全域没有任何以旧表为目标的 SQL（只扫描真会执行的调用参数）。"""
    offenders = []
    for f in _py_files():
        try:
            tree = ast.parse(f.read_text(encoding="utf-8"))
        except SyntaxError as e:  # 语法错误的文件本身就是问题，单独暴露
            offenders.append((f.name, e.lineno, "SYNTAX"))
            continue
        for lineno, sql in _sqlish_strings(tree):
            if _TABLE in sql:
                offenders.append((str(f.relative_to(_SRC)), lineno, sql.strip()[:80]))
    assert not offenders, f"仍有 SQL 触达旧表：{offenders[:5]}"


def test_s2_contract_query_reads_the_authoritative_table():
    """S2：契约键 user_account_username_by_id 的 SQL 指向 grape_accounts。"""
    from src.domains.mgmt import public as mp

    sql = mp._ANY_QUERIES["user_account_username_by_id"]
    assert "FROM grape_accounts" in sql
    assert "FROM user_accounts" not in sql
    # 旧的按 id 读死表键必须彻底消失（两张注册表都不能再有）
    assert "user_accounts_e2eb04" not in mp._ANY_QUERIES
    assert "user_accounts_e2eb04" not in mp._READ_QUERIES


def test_s3_resolver_hits_misses_and_rejects_bad_ids(monkeypatch):
    """S3：命中返回用户名；未命中返回空；非法/零 id 不查库。"""
    from src.domains.mgmt import public as mp

    calls = []

    def _fake(key, params=None):
        calls.append((key, params))
        return ("alice",) if params and str(params[0]) == "7" else None

    monkeypatch.setattr(mp, "read_one_any", _fake)

    assert mp.user_account_username_by_id_any(7) == "alice"
    assert calls[-1][0] == "user_account_username_by_id"
    assert str(calls[-1][1][0]) == "7"

    assert mp.user_account_username_by_id_any(8) == ""
    n_before = len(calls)
    assert mp.user_account_username_by_id_any(0) == ""
    assert mp.user_account_username_by_id_any("not-a-number") == ""
    assert len(calls) == n_before, "非法 id 不应触发查询"


def test_s4_dead_contract_key_is_gone():
    """S4：无消费方的旧表键已删除（访问必 KeyError）。"""
    from src.domains.mgmt import public as mp

    with pytest.raises(KeyError):
        mp.read_one("user_accounts_e2eb04", ("1",))


def test_s5_sandbox_admin_join_uses_the_authoritative_table():
    """S5：超管兜底 JOIN 指向 grape_accounts（禁止回退旧表）。"""
    text = (_SRC / "domains/mgmt/routes/sandbox_admin.py").read_text(encoding="utf-8")
    assert "JOIN grape_accounts" in text
    assert "JOIN user_accounts" not in text


def test_s6_registry_and_manifest_agree():
    """S6：kernel 表注册表与归属清单都不再列旧表。"""
    from src.kernel.db import TABLE_REGISTRY

    assert _TABLE not in TABLE_REGISTRY

    manifest = json.loads((_SRC.parent / "configs/table_ownership.json").read_text(encoding="utf-8"))
    assert _TABLE not in (manifest.get("tables") or {})
    assert _TABLE not in (manifest.get("views") or {})


def test_s7_no_retired_table_ddl_or_constants():
    """S7：迁移常量与升级路径里不再有旧表查询/补列。"""
    from src.domains.mgmt import migrations_ddl as mig

    bad = [name for name in dir(mig)
           if name.isupper() and isinstance(getattr(mig, name), str)
           and _TABLE in getattr(mig, name)]
    assert not bad, f"仍有旧表常量：{bad}"
    alter_bad = [s for s in getattr(mig, "ALTERS_USER_AND_KNOWLEDGE_COLUMNS", ())
                 if _TABLE in s]
    assert not alter_bad, f"仍有旧表 ALTER：{alter_bad}"

    setup_text = (_SRC / "api/db_setup.py").read_text(encoding="utf-8")
    assert "def _migrate_create_personal_orgs" not in setup_text
    assert "def _migrate_data_to_personal_orgs" not in setup_text
    assert f'_ensure_column(c, "{_TABLE}"' not in setup_text