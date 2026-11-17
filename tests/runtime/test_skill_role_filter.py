"""技能选择按职责过滤（2026-11-17）。

覆盖：
1. `scene_skill_search(agent_type=...)` 只返回同角色 + 未标注角色的通用技能；
2. `GET /api/scenes/skills/{agent_type}` 路由把角色透传到 owner 域查询（此前 where/params 造而不用）；
3. `resolve_skill_dispatch` 对存量错绑只告警（role_mismatch=True，仍返回技能），不阻断派发；
4. agent_type 为空 / 技能未标注角色时不误判。
"""
import os
import sys

import pytest

ROOT = os.path.join(os.path.dirname(__file__), "..", "..")
os.environ.setdefault("DEVOPS_DB_PATH", os.path.join(ROOT, "data", "test_sessions.db"))

from src.kernel.session_manager import db, db_lock            # noqa: E402
from src.api.db_setup import init_database                    # noqa: E402

_REPO = "gitcode:org/rolefilter"
_USER = "9901"
_UID = 9901


def setup_module():
    init_database()
    with db_lock:
        c = db.cursor()
        c.execute(
            "INSERT OR REPLACE INTO managed_repos "
            "(repo_key, repo_owner, repo_name, full_name, webhook_id, bot_gitcode_token) "
            "VALUES (?,?,?,?,?,?)",
            (_REPO, "org", "rolefilter", "org/rolefilter", 1, "bot_rf_token"))
        for _name, _role, _scope, _owner in (
                ("rf-coding-skill", "coding", "official", ""),
                ("rf-qa-skill", "qa", "official", ""),
                ("rf-generic-skill", "", "official", ""),
                ("rf-user-skill", "coding", "user", _USER),
                ("rf-repo-skill", "qa", "repo", _REPO)):
            c.execute(
                "INSERT OR REPLACE INTO skill (skill_name, display_name, agent_type, scope, owner_id, is_active) "
                "VALUES (?,?,?,?,?,1)", (_name, _name, _role, _scope, _owner))
        db.commit()


def teardown_module():
    with db_lock:
        c = db.cursor()
        c.execute("DELETE FROM skill WHERE skill_name LIKE 'rf-%'")
        c.execute("DELETE FROM managed_repos WHERE repo_key=?", (_REPO,))
        db.commit()


def _names(rows):
    return [r[1] for r in rows]


def test_search_filters_by_role_and_keeps_generic():
    from src.domains.runtime.public import scene_skill_search
    _total, rows = scene_skill_search(_USER, _REPO, agent_type="coding")
    names = _names(rows)
    assert "rf-coding-skill" in names
    assert "rf-generic-skill" in names, "未标注角色的通用技能必须保留"
    assert "rf-user-skill" in names
    assert "rf-qa-skill" not in names, "qa 技能不得出现在 coding 下拉"
    assert "rf-repo-skill" not in names


def test_search_without_role_returns_all_visible():
    from src.domains.runtime.public import scene_skill_search
    _total, rows = scene_skill_search(_USER, _REPO)
    names = _names(rows)
    assert {"rf-coding-skill", "rf-qa-skill", "rf-generic-skill"} <= set(names)


def test_route_passes_agent_type_to_owner_query(monkeypatch):
    """路由把 agent_type 透传下去（原实现造了 where/params 却从未使用）。"""
    from src.domains.mgmt.routes import scenes as scenes_routes
    captured = {}

    def _fake_search(user_id, repo_key, **kw):
        captured.update(kw)
        return 0, []

    monkeypatch.setattr(scenes_routes._rt_scenes, "scene_skill_search", _fake_search)
    scenes_routes.list_skills_for_agent("CODING", user={"id": 9901})
    assert captured.get("agent_type") == "coding", captured


def test_dispatch_flags_role_mismatch_but_keeps_skill(caplog):
    from src.domains.mgmt.user.db import get_skill_by_id, resolve_skill_dispatch
    with db_lock:
        c = db.cursor()
        c.execute("SELECT id FROM skill WHERE skill_name='rf-qa-skill'")
        _sid = int(c.fetchone()[0])
    assert get_skill_by_id(_sid)
    _res = resolve_skill_dispatch("rf-qa-skill", _sid, repo_key=_REPO, user_id=_UID,
                                  scene_id="s1", node_tag="coding", agent_type="coding")
    assert _res["skill_id"] == _sid, "只告警不阻断：技能仍要解析出来"
    assert _res["role_mismatch"] is True


def test_dispatch_no_mismatch_when_role_matches_or_empty():
    from src.domains.mgmt.user.db import resolve_skill_dispatch
    with db_lock:
        c = db.cursor()
        c.execute("SELECT id FROM skill WHERE skill_name='rf-qa-skill'")
        _sid = int(c.fetchone()[0])
        c.execute("SELECT id FROM skill WHERE skill_name='rf-generic-skill'")
        _generic = int(c.fetchone()[0])
    assert resolve_skill_dispatch("rf-qa-skill", _sid, repo_key=_REPO,
                                  agent_type="qa")["role_mismatch"] is False
    assert resolve_skill_dispatch("rf-qa-skill", _sid, repo_key=_REPO,
                                  agent_type="")["role_mismatch"] is False
    assert resolve_skill_dispatch("rf-generic-skill", _generic, repo_key=_REPO,
                                  agent_type="coding")["role_mismatch"] is False