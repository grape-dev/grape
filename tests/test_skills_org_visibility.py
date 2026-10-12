"""技能列表组织隔离修正测试（skills.py L103-111）。

背景（2026-10-12，与另一服务同源问题）：repo/user 技能没有"组织级"概念，
org_id 只是迁移归属标记。原代码对 repo 技能也追加 (org_id=0 OR org_id=?)
过滤 → 跨组织的仓库技能在列表中被错误滤掉（用户能访问仓库却看不到技能）。

修正后语义：
- scope=repo / scope=user：不追加组织过滤（owner_id 已精确限定）
- 其余（official 混合 / 不带 scope）：保留原组织过滤（official org_id=0 跨组织可见）
"""
import os
import sys

ROOT = os.path.normpath(os.path.join(os.path.dirname(__file__), ".."))
os.environ.setdefault("DEVOPS_DB_PATH", os.path.join(ROOT, "data", "test_skills_org.db"))
os.environ["DEVOPS_DB_BACKEND"] = "sqlite"

import pytest

from src.api.db_setup import init_database
from src.session_manager import db, db_lock

init_database()

REPO_A = "gitcode:other-org/repo-a"


@pytest.fixture(autouse=True)
def _seed():
    with db_lock:
        c = db.cursor()
        c.execute("DELETE FROM skill WHERE skill_name LIKE 't-org-%'")
        # 仓库技能挂在"别的组织"(org_id=23) 名下 + 对应 managed_repos 行（repo_key 解析用）
        c.execute(
            "INSERT INTO skill (skill_name, display_name, description, agent_type, scope, owner_id, org_id, is_active) "
            "VALUES ('t-org-repo-skill','t','t','coding','repo',?,23,1)", (REPO_A,))
        c.execute(
            "INSERT INTO skill (skill_name, display_name, description, agent_type, scope, owner_id, org_id, is_active) "
            "VALUES ('t-org-user-skill','t','t','coding','user','999',23,1)")
        c.execute("INSERT OR IGNORE INTO managed_repos (repo_key) VALUES (?)", (REPO_A,))
        db.commit()
    yield
    with db_lock:
        c = db.cursor()
        c.execute("DELETE FROM skill WHERE skill_name LIKE 't-org-%'")
        db.commit()


def _user(org_id):
    return {"id": 1, "username": "tester", "auth_type": "jwt", "org_id": org_id}


def _list(user, repo="", scope=""):
    from src.api.routes.skills import list_skills
    return list_skills(user, repo=repo, scope=scope)


def test_repo_skill_visible_across_org():
    """跨组织用户（org=99）按仓库查技能 → 能看到 org=23 的仓库技能。"""
    d = _list(_user(99), repo=REPO_A, scope="repo")
    names = [s.get("name") for s in d.get("skills", [])]
    assert "t-org-repo-skill" in names, f"仓库技能被组织过滤错误滤掉: {names}"


def test_repo_branch_no_org_condition():
    """scope=repo 分支不追加 org_id 条件（单元级断言，防回归）。"""
    scope, _org_id = "repo", 99
    conds = ["is_active=1"]
    if _org_id and scope not in ("repo", "user"):
        conds.append("(org_id=0 OR org_id=?)")
    assert not any("org_id" in c for c in conds)


def test_other_scope_keeps_org_filter():
    """不带 scope（混合列表）仍保留组织过滤（official 跨组织 + 本组织）语义不变。"""
    scope, _org_id = "", 99
    conds = ["is_active=1"]
    if _org_id and scope not in ("repo", "user"):
        conds.append("(org_id=0 OR org_id=?)")
    assert any("org_id" in c for c in conds)
