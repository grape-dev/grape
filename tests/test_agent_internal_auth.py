"""A2A / events 内部 agent 端点鉴权：bot token 或 会话级临时 token（repo 作用域）二选一（M2 试点）。"""
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from src.api import auth_token
from src.api.routes.events import _require_event_bot_token
from src.api.routes.webhook import _authorize_a2a
from src.session_manager import db, db_lock

REPO = "gitcode:o/r"


_TD_REPOS = (REPO, "gitcode:o/target-repo", "gitcode:o/session-repo", "gitcode:o/other-repo")


@pytest.fixture(autouse=True)
def _clean():
    with db_lock:
        c = db.cursor()
        c.execute("DELETE FROM session_api_tokens")
        c.execute("DELETE FROM api_tokens")
        for _rk in _TD_REPOS:
            c.execute("DELETE FROM devops_agent_session WHERE repo_key=?", (_rk,))
            c.execute("DELETE FROM managed_repos WHERE repo_key=?", (_rk,))
            c.execute("DELETE FROM devops_pipeline_scene WHERE repo_key=?", (_rk,))
        db.commit()
    yield
    with db_lock:
        c = db.cursor()
        c.execute("DELETE FROM session_api_tokens")
        for _rk in _TD_REPOS:
            c.execute("DELETE FROM devops_agent_session WHERE repo_key=?", (_rk,))
            c.execute("DELETE FROM managed_repos WHERE repo_key=?", (_rk,))
            c.execute("DELETE FROM devops_pipeline_scene WHERE repo_key=?", (_rk,))
        db.commit()


def _stub(tok: str = ""):
    class _Q:
        def get(self, k, default=None):
            return default
    return SimpleNamespace(
        headers={"authorization": f"Bearer {tok}" if tok else "",
                 "Authorization": f"Bearer {tok}" if tok else "",
                 "X-Bot-Token": ""},
        query_params=_Q())


def _seed_session(repo=REPO) -> str:
    return auth_token.issue_session_token(session_key="s-t", repo_key=repo, agent_type="vod")


def _seed_bot():
    with db_lock:
        db.cursor().execute(
            "INSERT INTO devops_agent_session (id, repo_key, agent_type, repo_gitcode_token) "
            "VALUES ('sess-bot', ?, 'vod', 'BOT1')",
            (REPO,),
        )
        db.commit()


# ===================== verify_session_token_for_repo =====================

def test_verify_for_repo_match_and_mismatch():
    tok = _seed_session()
    assert auth_token.verify_session_token_for_repo(tok, REPO) is not None
    assert auth_token.verify_session_token_for_repo(tok, "gitcode:other/r") is None
    assert auth_token.verify_session_token_for_repo("hello", REPO) is None


# ===================== _authorize_a2a =====================

def test_a2a_session_token_ok():
    tok = _seed_session()
    _authorize_a2a(_stub(tok), REPO)  # 不应抛


def test_a2a_bot_token_ok():
    _seed_bot()
    _authorize_a2a(_stub("BOT1"), REPO)  # 兼容保留


def test_a2a_reject_mismatch_on_managed_repo():
    _seed_bot()
    tok = _seed_session("gitcode:other/r")  # 会话属于其他仓库
    with pytest.raises(HTTPException) as ei:
        _authorize_a2a(_stub(tok), REPO)
    assert ei.value.status_code == 403


def test_a2a_reject_no_token_on_managed_repo():
    _seed_bot()
    with pytest.raises(HTTPException) as ei:
        _authorize_a2a(_stub(""), REPO)
    assert ei.value.status_code == 403


def test_a2a_unmanaged_defer_without_token():
    _authorize_a2a(_stub("anything"), REPO)  # 未托管仓库 → 放行交由下游


# ===================== _authorize_a2a 关联仓库信任域 =====================
# Issue #15 PR #369 新增逻辑：会话 token 绑定仓库与目标仓库若属于同一场景
# 关联仓库集合（linked_repos），或两仓 bot token 相同（同一运维主体）→ 放行

TARGET_REPO = "gitcode:o/target-repo"
SESSION_REPO = "gitcode:o/session-repo"


def _seed_managed_repo(repo_key: str, token: str):
    """插入 managed_repos 行（带 bot_gitcode_token）。"""
    from src.repo_key import split_owner_repo
    owner, rname = split_owner_repo(repo_key)
    with db_lock:
        db.cursor().execute(
            "INSERT OR REPLACE INTO managed_repos "
            "(repo_key, repo_owner, repo_name, full_name, bot_gitcode_token) "
            "VALUES (?,?,?,?,?)",
            (repo_key, owner, rname, f"{owner}/{rname}", token),
        )
        db.commit()


def _seed_linked_repos_scene(repo_key: str, linked_repo_keys: list[str]):
    """在 devops_pipeline_scene 表插入场景行，含 linked_repos 列。"""
    import json as _json
    _lr_json = _json.dumps(
        [{"repo_key": rk, "branch": "main"} for rk in linked_repo_keys],
        ensure_ascii=False,
    )
    with db_lock:
        db.cursor().execute(
            "INSERT OR REPLACE INTO devops_pipeline_scene "
            "(scene_id, name, repo_key, nodes_json, is_active, linked_repos, advanced_options) "
            "VALUES (?,?,?,?,1,?,?)",
            ("test-td", "Trust Domain Scene", repo_key, "[]", _lr_json, "{}"),
        )
        db.commit()


def test_a2a_trust_domain_same_bot_token():
    """两仓 bot token 相同（同一运维主体）→ 放行。"""
    _seed_managed_repo(SESSION_REPO, "SHARED_BOT")
    _seed_managed_repo(TARGET_REPO, "SHARED_BOT")
    tok = _seed_session(SESSION_REPO)
    _authorize_a2a(_stub(tok), TARGET_REPO)  # 不应抛异常


def test_a2a_trust_domain_linked_repos():
    """目标仓库在会话仓库的 linked_repos 配置中 → 放行。"""
    _seed_managed_repo(SESSION_REPO, "SESSION_BOT")
    _seed_managed_repo(TARGET_REPO, "TARGET_BOT")  # 两仓 bot token 不同
    _seed_linked_repos_scene(SESSION_REPO, [TARGET_REPO])
    tok = _seed_session(SESSION_REPO)
    _authorize_a2a(_stub(tok), TARGET_REPO)  # 不应抛异常


def test_a2a_trust_domain_linked_repos_in_advanced_options():
    """linked_repos 在 advanced_options JSON 中（独立列为空时回退）→ 放行。"""
    import json as _json
    _seed_managed_repo(SESSION_REPO, "SESSION_BOT")
    _seed_managed_repo(TARGET_REPO, "TARGET_BOT")
    _ao = _json.dumps({"linked_repos": [{"repo_key": TARGET_REPO, "branch": "main"}]})
    with db_lock:
        db.cursor().execute(
            "INSERT OR REPLACE INTO devops_pipeline_scene "
            "(scene_id, name, repo_key, nodes_json, is_active, linked_repos, advanced_options) "
            "VALUES (?,?,?,?,1,?,?)",
            ("test-td-ao", "TD AO Scene", SESSION_REPO, "[]", None, _ao),
        )
        db.commit()
    tok = _seed_session(SESSION_REPO)
    _authorize_a2a(_stub(tok), TARGET_REPO)  # 不应抛异常


def test_a2a_trust_domain_reject_no_trust_relationship():
    """无信任关系（bot token 不同、不在 linked_repos）→ 403 拒绝。"""
    _seed_managed_repo(SESSION_REPO, "SESSION_BOT")
    _seed_managed_repo(TARGET_REPO, "TARGET_BOT")  # 两仓 bot token 不同
    _seed_linked_repos_scene(SESSION_REPO, ["gitcode:o/other-repo"])  # 不含目标仓库
    tok = _seed_session(SESSION_REPO)
    with pytest.raises(HTTPException) as ei:
        _authorize_a2a(_stub(tok), TARGET_REPO)
    assert ei.value.status_code == 403


# ===================== get_user_or_none（A2A 软认证） =====================

def test_get_user_or_none_session_dict_and_garbage_none():
    from src.api.deps import get_user_or_none
    tok = _seed_session()
    assert get_user_or_none(_stub(tok)) is not None
    assert get_user_or_none(_stub("garbage")) is None
    assert get_user_or_none(_stub("")) is None


# ===================== _require_event_bot_token =====================

def test_events_session_token_ok():
    tok = _seed_session()
    _require_event_bot_token(_stub(tok), REPO)


def test_events_bot_token_ok():
    _seed_bot()
    _require_event_bot_token(_stub("BOT1"), REPO)


def test_events_reject_wrong_token():
    tok = _seed_session("gitcode:other/r")
    with pytest.raises(HTTPException) as ei:
        _require_event_bot_token(_stub(tok), REPO)
    assert ei.value.status_code == 403