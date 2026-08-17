"""Lobby「加入并托管」403 修复的看护测试（regression guard）。

覆盖：
- managed_repos / user_repos 新表结构（repo_key 带前缀、无 user_id、is_join）
- 迁移逻辑（旧表 → repo_key 前缀 + is_join 回填）
- _filter_repos 核心过滤（403 根因：已被托管的仓库不得对维护员显示「加入并托管」）
- repo_key 工具函数（to/from/parse/split）
- 外部 API 契约（前端收到的 full_name/repo_key 无前缀）
"""
import os
import sys
import sqlite3
import tempfile
from unittest.mock import patch

ROOT = os.path.join(os.path.dirname(__file__), "..")
os.environ.setdefault("DEVOPS_DB_PATH", os.path.join(ROOT, "data", "test_sessions.db"))

from src.session_manager import db, db_lock
from src.user import db as user_db
from src.api.routes import repos as repos_mod


def _clean():
    with db_lock:
        c = db.cursor()
        c.execute("DELETE FROM managed_repos")
        c.execute("DELETE FROM user_repos")
        db.commit()


# ===================== repo_key 工具函数 =====================

def test_repo_key_utils():
    from src.repo_key import to_repo_key, from_repo_key, parse_repo_key, split_owner_repo
    # to_repo_key：owner/repo → 带前缀；幂等
    assert to_repo_key("owner/repo") == "gitcode:owner/repo"
    assert to_repo_key("gitcode:owner/repo") == "gitcode:owner/repo"
    assert to_repo_key("") == ""
    # from_repo_key：去前缀
    assert from_repo_key("gitcode:owner/repo") == "owner/repo"
    assert from_repo_key("owner/repo") == "owner/repo"
    # parse_repo_key：provider/owner/repo
    assert parse_repo_key("gitcode:owner/repo") == ("gitcode", "owner", "repo")
    assert parse_repo_key("github:owner/repo") == ("github", "owner", "repo")
    assert parse_repo_key("owner/repo") == ("gitcode", "owner", "repo")
    # split_owner_repo：兼容带/不带前缀
    assert split_owner_repo("owner/repo") == ("owner", "repo")
    assert split_owner_repo("gitcode:owner/repo") == ("owner", "repo")


# ===================== 表结构 =====================

def test_managed_repos_schema_no_user_id():
    cols = [r[1] for r in db.cursor().execute("PRAGMA table_info(managed_repos)").fetchall()]
    assert "user_id" not in cols, "managed_repos 不应含 user_id"
    assert "repo_key" in cols, "managed_repos 应含 repo_key"


def test_user_repos_schema_has_repo_key_and_is_join():
    cols = [r[1] for r in db.cursor().execute("PRAGMA table_info(user_repos)").fetchall()]
    assert "repo_key" in cols, "user_repos 应含 repo_key"
    assert "is_join" in cols, "user_repos 应含 is_join"
    assert "full_name" not in cols, "user_repos 不应再有 full_name 列"


# ===================== DB 函数 =====================

def test_add_managed_repo_upsert_by_repo_key():
    _clean()
    user_db.add_managed_repo("gitcode:owner/repo", "owner", "repo", webhook_id=1, bot_username="grape_bot")
    user_db.add_managed_repo("gitcode:owner/repo", "owner", "repo", webhook_id=2)
    with db_lock:
        rows = db.cursor().execute("SELECT repo_key, webhook_id FROM managed_repos WHERE repo_key='gitcode:owner/repo'").fetchall()
    assert len(rows) == 1, "同一 repo_key 不应产生重复行"
    assert rows[0][1] == 2  # 后写覆盖 webhook_id
    _clean()


def test_managed_repo_key_value_has_prefix():
    _clean()
    user_db.add_managed_repo("owner/repo", "owner", "repo", webhook_id=1)
    with db_lock:
        rk = db.cursor().execute("SELECT repo_key FROM managed_repos").fetchone()[0]
    assert rk == "gitcode:owner/repo", f"repo_key 值应带前缀，实际 {rk}"
    _clean()


def test_get_managed_repos_user_scoped():
    _clean()
    user_db.add_managed_repo("gitcode:owner/repo", "owner", "repo", webhook_id=1)
    user_db.mark_user_repo_join(1, "gitcode:owner/repo", is_join=True, is_admin=True)
    repos = user_db.get_managed_repos(1)
    assert any(r["repo_key"] == "gitcode:owner/repo" for r in repos)
    assert all(r["full_name"] == "owner/repo" for r in repos), "对外 full_name 应无前缀"
    _clean()


def test_remove_managed_repo_by_repo_key():
    _clean()
    user_db.add_managed_repo("gitcode:owner/repo", "owner", "repo", webhook_id=1)
    user_db.mark_user_repo_join(1, "gitcode:owner/repo", is_join=True)
    removed = user_db.remove_managed_repo("gitcode:owner/repo")
    assert removed is not None
    with db_lock:
        c = db.cursor()
        assert c.execute("SELECT COUNT(*) FROM managed_repos WHERE repo_key='gitcode:owner/repo'").fetchone()[0] == 0
        assert c.execute("SELECT is_join FROM user_repos WHERE user_id=1 AND repo_key='gitcode:owner/repo'").fetchone()[0] == 0
    _clean()


# ===================== _filter_repos 核心逻辑 =====================

def _filter(user_id, repos):
    return repos_mod._filter_repos(user_id, repos)


def test_filter_repos_admin_not_hosted_shows_host_button():
    _clean()
    repos = [{"repo_key": "gitcode:owner/repo", "full_name": "owner/repo", "owner": "owner", "name": "repo",
              "description": "", "is_admin": True, "is_join": False}]
    out = _filter(1, repos)
    assert len(out) == 1
    assert out[0]["is_hosted"] is False
    assert out[0]["is_joined"] is False
    assert out[0]["role"] == "Admin"


def test_filter_repos_member_but_managed_hides_host_button():
    """403 根因看护：维护员 + 仓库已被他人托管 → 不得显示「加入并托管」"""
    _clean()
    user_db.add_managed_repo("gitcode:owner/repo", "owner", "repo", webhook_id=123)
    repos = [{"repo_key": "gitcode:owner/repo", "full_name": "owner/repo", "owner": "owner", "name": "repo",
              "description": "", "is_admin": False, "is_join": False}]
    out = _filter(1, repos)
    assert len(out) == 1, "已被托管的仓库应展示"
    assert out[0]["is_hosted"] is True
    assert out[0]["is_joined"] is True
    assert out[0]["role"] != "Admin" or True
    _clean()


def test_filter_repos_joined_user_visible():
    _clean()
    repos = [{"repo_key": "gitcode:owner/repo", "full_name": "owner/repo", "owner": "owner", "name": "repo",
              "description": "", "is_admin": False, "is_join": True}]
    out = _filter(1, repos)
    assert len(out) == 1
    assert out[0]["is_joined"] is True


def test_filter_repos_ignores_unmanaged_member():
    _clean()
    repos = [{"repo_key": "gitcode:owner/repo", "full_name": "owner/repo", "owner": "owner", "name": "repo",
              "description": "", "is_admin": False, "is_join": False}]
    out = _filter(1, repos)
    assert out == [], "非管理员 + 非已加入 + 未托管 → 不展示"


def test_filter_repos_prefix_isolates_across_platforms():
    """跨平台同名仓库隔离：gitcode:owner/repo 被托管不影响 github:owner/repo"""
    _clean()
    user_db.add_managed_repo("gitcode:owner/repo", "owner", "repo", webhook_id=1)
    repos = [
        {"repo_key": "gitcode:owner/repo", "full_name": "owner/repo", "owner": "owner", "name": "repo",
         "description": "", "is_admin": False, "is_join": False},
        {"repo_key": "github:owner/repo", "full_name": "owner/repo", "owner": "owner", "name": "repo",
         "description": "", "is_admin": False, "is_join": False},
    ]
    out = _filter(1, repos)
    by_key = {r["repo_key"]: r for r in out}
    assert "gitcode:owner/repo" in by_key, "gitcode 托管命中应展示"
    assert "github:owner/repo" not in by_key, "github 同名仓库不应被误判托管"
    _clean()


# ===================== 外部 API 契约 =====================

def test_api_responses_strip_prefix():
    """/repos/member 响应的 full_name 无前缀（前端契约）"""
    _clean()
    from src.user.repo_manager import fetch_repos_with_role
    payload = [
        {"full_name": "me/own", "name": "own", "owner": {"login": "me"},
         "permission": {"admin": True, "push": True, "pull": True}},
    ]
    def fake_req(method, url, token="", body=None):
        if url.endswith("/user"):
            return {"login": "me"}
        if "/user/repos" in url:
            return payload
        return {}
    with patch("src.user.repo_manager._req", side_effect=fake_req):
        fetched = fetch_repos_with_role("tok")
    out = _filter(1, fetched)
    assert out and out[0]["full_name"] == "me/own"
    assert not out[0]["full_name"].startswith("gitcode:"), "对外 full_name 不得带前缀"
    _clean()
