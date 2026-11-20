"""Issue #2264: 个人空间可见已关联团队组织的仓库（带组织标识）。

背景：仓库关联到团队组织后，个人空间列表（仓库大厅 / 侧边栏下拉）此前会把它隐藏，
用户会以为仓库“消失”。现按产品口径：
- 个人空间显示该用户全部托管仓库，并返回 org_id/org_name 供前端打组织徽标；
- 团队组织仍严格按 m.org_id=? 隔离。
"""
from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from src.kernel.session_manager import db, db_lock

UID = 22641
SLUG = "issue2264-personal"
TEAM_SLUG = "issue2264-team"
TEAM_REPO = "gitcode:issue2264/team-repo"
PERSONAL_REPO = "gitcode:issue2264/personal-repo"
UNBOUND_REPO = "gitcode:issue2264/unbound-repo"
ALL_REPOS = (TEAM_REPO, PERSONAL_REPO, UNBOUND_REPO)


@pytest.fixture(scope="session", autouse=True)
def _ensure_tables():
    from src.api import db_setup

    db_setup.init_database()


@pytest.fixture(autouse=True)
def _clean():
    _cleanup()
    yield
    _cleanup()


def _cleanup() -> None:
    with db_lock:
        c = db.cursor()
        org_ids = [
            r[0]
            for r in c.execute("SELECT id FROM organization WHERE owner_id=?", (UID,)).fetchall()
        ]
        for org_id in org_ids:
            c.execute("DELETE FROM org_member WHERE org_id=?", (org_id,))
        c.execute("DELETE FROM organization WHERE owner_id=?", (UID,))
        for key in ALL_REPOS:
            c.execute("DELETE FROM managed_repos WHERE repo_key=?", (key,))
            c.execute("DELETE FROM user_repos WHERE repo_key=?", (key,))
        db.commit()


def _seed() -> tuple[str, str]:
    from src.domains.mgmt.user.db import add_managed_repo
    from src.domains.mgmt.user.org_db import create_org
    from src.kernel.repo_key import parse_repo_key

    personal = create_org(name=SLUG, slug=SLUG, owner_id=UID, is_personal=1)
    team = create_org(name=TEAM_SLUG, slug=TEAM_SLUG, owner_id=UID, is_personal=0)
    assert personal and team

    bound = {TEAM_REPO: team["id"], PERSONAL_REPO: personal["id"], UNBOUND_REPO: ""}
    for key, org_id in bound.items():
        _p, owner, name = parse_repo_key(key)
        assert add_managed_repo(key, owner, name, webhook_id=8000, org_id=org_id)
        with db_lock:
            c = db.cursor()
            c.execute(
                "INSERT OR REPLACE INTO user_repos (repo_key, user_id, is_join, is_admin) "
                "VALUES (?, ?, 1, 1)",
                (key, UID),
            )
            db.commit()
    return personal["id"], team["id"]


def test_personal_workspace_shows_org_bound_repos_with_badge():
    from src.domains.mgmt.user.db import get_managed_repos

    personal_id, _team_id = _seed()
    repos = get_managed_repos(UID, org_id=personal_id, personal_org=True)
    by_key = {r["repo_key"]: r for r in repos}

    assert TEAM_REPO in by_key, "个人空间必须能看到已关联团队组织的仓库"
    assert by_key[TEAM_REPO]["org_name"] == TEAM_SLUG
    assert PERSONAL_REPO in by_key and by_key[PERSONAL_REPO]["org_name"] == SLUG
    assert UNBOUND_REPO in by_key and by_key[UNBOUND_REPO]["org_id"] == ""


def test_team_workspace_still_isolated():
    from src.domains.mgmt.user.db import get_managed_repos

    _personal_id, team_id = _seed()
    repos = get_managed_repos(UID, org_id=team_id, personal_org=False)
    keys = {r["repo_key"] for r in repos}
    assert keys == {TEAM_REPO}, "团队组织只应看到本组织关联仓库"


def test_lobby_filter_personal_shows_org_repos_and_team_isolates():
    from src.domains.mgmt.routes.repos import _filter_repos
    from src.kernel.repo_key import parse_repo_key

    _personal_id, team_id = _seed()
    vcs_repos = []
    for key in ALL_REPOS:
        _p, owner, name = parse_repo_key(key)
        vcs_repos.append({
            "repo_key": key, "full_name": f"{owner}/{name}", "owner": owner,
            "name": name, "is_admin": True,
        })

    personal_items = _filter_repos(UID, list(vcs_repos), org_id="")
    by_key = {r["repo_key"]: r for r in personal_items}
    assert TEAM_REPO in by_key, "个人空间仓库大厅不应隐藏已关联团队组织的仓库"
    assert by_key[TEAM_REPO]["org_name"] == TEAM_SLUG
    assert PERSONAL_REPO in by_key and UNBOUND_REPO in by_key

    team_items = _filter_repos(UID, list(vcs_repos), org_id=team_id)
    team_keys = {r["repo_key"] for r in team_items}
    assert TEAM_REPO in team_keys, "团队组织大厅必须显示本组织仓库"
    assert PERSONAL_REPO not in team_keys, "团队组织大厅不得显示其它组织（个人组织）的仓库"
    # 未归属仓库仍展示（大厅允许就地在当前组织托管），与既有大厅口径一致
    assert UNBOUND_REPO in team_keys