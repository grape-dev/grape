"""D31（S51）用例：身份优先归属 + PR 探测卡驱动。

全部单元测试：测试库真实 SQL 种子（identity/um/accounts）+ mock 委托点。
覆盖：resolve_user_id_by_platform_login、_get_user_id_by_gitcode /
_repo_user_id 身份优先与 um 兜底顺序、resolve_pr_issue_id 卡驱动。
"""
import json
from unittest.mock import patch

import pytest

from src.kernel.session_manager import db, db_lock


def _seed_account(uid: int, username: str, status: int = 1):
    with db_lock:
        db.cursor().execute(
            "INSERT OR REPLACE INTO grape_accounts (id, username, email, status) "
            "VALUES (?, ?, ?, ?)", (uid, username, f"{username}@t.local", status))
        db.commit()


def _seed_identity(uid: int, provider: str, ext_id: str, login: str, status: int = 1):
    with db_lock:
        db.cursor().execute(
            "INSERT OR REPLACE INTO external_identities "
            "(grape_user_id, provider, external_id, external_login, external_name, status) "
            "VALUES (?, ?, ?, ?, '', ?)",
            (uid, provider, ext_id, login, status))
        db.commit()


def _seed_um(uid: int, col: str, login: str):
    with db_lock:
        db.cursor().execute(
            f"INSERT OR REPLACE INTO unified_user_mapping (user_id, {col}) VALUES (?, ?)",
            (uid, login))
        db.commit()


def _cleanup(prefix_ids=(101, 102, 103)):
    with db_lock:
        c = db.cursor()
        for uid in prefix_ids:
            c.execute("DELETE FROM external_identities WHERE grape_user_id=?", (uid,))
            c.execute("DELETE FROM grape_accounts WHERE id=?", (uid,))
            c.execute("DELETE FROM unified_user_mapping WHERE user_id=?", (uid,))
        db.commit()


@pytest.fixture(autouse=True)
def _clean():
    _cleanup()
    yield
    _cleanup()


# ------------------------------------------------------- S51 身份优先归属

def test_s51_resolve_by_platform_login_hit():
    """平台登录名在身份域命中 → 返回 uid（协议无关，含第 5 平台）。"""
    from src.domains.mgmt.auth.grape_account_db import resolve_user_id_by_platform_login
    _seed_account(101, "alice_reg")
    _seed_identity(101, "gitcode", "g-1", "alice_platform")
    assert str(resolve_user_id_by_platform_login("gitcode", "alice_platform")) == "101"
    # gitee（第 5 平台）同样走身份域
    _seed_identity(101, "gitee", "e-1", "alice_gitee")
    assert str(resolve_user_id_by_platform_login("gitee", "alice_gitee")) == "101"


def test_s51_resolve_by_platform_login_miss_shapes():
    """登录名不匹配 / 账号禁用 / 绑定解绑（status!=1）/ 空参 → 0。"""
    from src.domains.mgmt.auth.grape_account_db import resolve_user_id_by_platform_login
    _seed_account(102, "bob_reg", status=0)          # 账号禁用
    _seed_identity(102, "gitcode", "g-2", "bob_platform")
    assert resolve_user_id_by_platform_login("gitcode", "bob_platform") == 0
    assert resolve_user_id_by_platform_login("gitcode", "nobody") == 0
    assert resolve_user_id_by_platform_login("", "x") == 0
    assert resolve_user_id_by_platform_login("gitcode", "") == 0


def test_s51_issues_center_identity_first_um_fallback():
    """身份命中优先于 um；身份未命中回退 um（存量行为保持）。"""
    from src.api.issues_center import _get_user_id_by_gitcode
    # 场景 1：注册名(alice_reg) ≠ 平台登录名(alice_platform)，um 无行 → 身份域命中
    _seed_account(101, "alice_reg")
    _seed_identity(101, "gitcode", "g-1", "alice_platform")
    got = _get_user_id_by_gitcode("alice_platform", repo_key="gitcode:acme/web")
    assert str(got) == "101"
    # 场景 2：身份未命中（无 identity 行），um 有行 → 回退 um（行为不变）
    _seed_account(103, "carol_reg")
    _seed_um(103, "gitcode_user", "carol_platform")
    got = _get_user_id_by_gitcode("carol_platform", repo_key="gitcode:acme/web")
    assert str(got) == "103"


def test_s51_repo_user_id_identity_first():
    """engine_select._repo_user_id：身份优先（原二级回退置前），um 兜底保持。"""
    from src.domains.runtime.engine.engine_select import _repo_user_id
    # 身份命中：um 无行也能解析（原实现会算 0/走二级回退）
    _seed_account(101, "alice_reg")
    _seed_identity(101, "gitcode", "g-1", "alice_platform")
    assert str(_repo_user_id("gitcode:acme/web", issue_creator="alice_platform")) == "101"
    # 身份未命中 + um 命中 → um 兜底（行为保持）
    _seed_account(103, "carol_reg")
    _seed_um(103, "gitcode_user", "carol_platform")
    assert str(_repo_user_id("gitcode:acme/web", issue_creator="carol_platform")) == "103"


# ------------------------------------------------------- S51 PR 探测卡驱动

def test_s51_resolve_pr_issue_id_card_driven():
    """PR 详情 API 兜底走 create_client.get_pr（卡驱动），按响应解析 issue。"""
    from src.domains.runtime.pipeline.event_pipeline import resolve_pr_issue_id

    class _C:
        def __init__(self, rk, tok):
            self.repo_key, self.token = rk, tok

        def get_pr(self, pr_number):
            assert str(pr_number) == "9"
            return {"head": {"ref": "feature/issue-282"}, "title": "misc",
                    "number": 9}

    with patch("src.domains.runtime.vcs.api_client.create_client", side_effect=_C) as cc:
        payload = {"pull_request": {"number": 9, "title": "no issue here"},
                   "repository": {"full_name": "acme/web"}}
        out = resolve_pr_issue_id(payload, api_token="tok-x", repo_key="gitee:acme/web")
    assert out == "#282"
    assert cc.call_args[0][0] == "gitee:acme/web"      # 前缀 provider 进卡解析
    assert cc.call_args[0][1] == "tok-x"


def test_s51_resolve_pr_issue_id_error_response_short_circuits():
    """get_pr 返回 error dict → 兜底链安全终止（返回 ''）。"""
    from src.domains.runtime.pipeline.event_pipeline import resolve_pr_issue_id

    class _C:
        def __init__(self, rk, tok):
            pass

        def get_pr(self, pr_number):
            return {"error": "unsupported provider: gitee"}

    with patch("src.domains.runtime.vcs.api_client.create_client", side_effect=_C):
        payload = {"pull_request": {"number": 9, "title": "no issue here"},
                   "repository": {"full_name": "acme/web"}}
        out = resolve_pr_issue_id(payload, api_token="tok-x", repo_key="gitee:acme/web")
    assert out == ""
