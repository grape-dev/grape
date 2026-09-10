"""A2A / events 内部 agent 端点鉴权：bot token 或 会话级临时 token（repo 作用域）二选一（M2 试点）。"""
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from src.api import auth_token
from src.api.routes.events import _require_event_bot_token
from src.api.routes.webhook import _authorize_a2a
from src.session_manager import db, db_lock

REPO = "gitcode:o/r"


@pytest.fixture(autouse=True)
def _clean():
    with db_lock:
        c = db.cursor()
        c.execute("DELETE FROM session_api_tokens")
        c.execute("DELETE FROM api_tokens")
        c.execute("DELETE FROM devops_agent_session WHERE repo_key=?", (REPO,))
        db.commit()
    yield
    with db_lock:
        c = db.cursor()
        c.execute("DELETE FROM session_api_tokens")
        c.execute("DELETE FROM devops_agent_session WHERE repo_key=?", (REPO,))
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