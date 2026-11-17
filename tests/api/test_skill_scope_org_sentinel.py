"""技能列表的 org 隔离哨兵值（''/ '0' / NULL 都算全局可见）。

回归背景：官方技能在库里存的是 org_id='0'，而可见性条件只认 org_id=''
（`org_id='' OR org_id=?`）。已归属组织的用户（org_id 非空且非 '0'）打开
「技能 → 全部技能」时列表接口返回 total=0、skills=[]，前端显示「无技能」，
而分类/统计仍显示数量 —— 表现就是「只有数量，看不到技能」。
"""
from src.domains.runtime.public import _skill_scope_conditions


def _sql(conds):
    return " AND ".join(conds)


def test_official_org_condition_accepts_zero_and_null_sentinels():
    conds, params = _skill_scope_conditions("official", user_id="1", org_id="1")
    sql = _sql(conds)
    assert "COALESCE(org_id,'') IN ('', '0')" in sql, sql
    assert params == ["1"], params


def test_official_without_org_scope_has_no_org_condition():
    conds, params = _skill_scope_conditions("official", user_id="1", org_id="")
    assert not any("org_id" in c for c in conds), conds
    assert params == []


def test_repo_and_user_scope_are_not_org_filtered():
    for scope in ("repo", "user"):
        conds, _params = _skill_scope_conditions(
            scope, user_id="1", repo_key="gitcode:o/r", org_id="1")
        assert not any("COALESCE(org_id" in c for c in conds), (scope, conds)


def test_all_scope_still_isolates_by_org():
    conds, params = _skill_scope_conditions("all", user_id="1", org_id="7")
    assert any("COALESCE(org_id,'') IN ('', '0')" in c for c in conds), conds
    assert params == ["7"]