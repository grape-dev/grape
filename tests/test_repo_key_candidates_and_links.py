"""S25（跨平台候选集）/ S26（issue/PR web 链接卡派生）+ D16 守卫用例。"""
import json
import uuid

import pytest
from fastapi.testclient import TestClient

from src.kernel.session_manager import db, db_lock
from src.domains.runtime.vcs.platform_card import invalidate_platform_card_cache


GITEE_CARD = {
    "name": "Gitee", "dialect": "github", "host": "gitee.com",
    "webhook_path": "/webhook/gitee", "event_header": "X-Gitee-Event",
    "api_base": "https://gitee.com/api/v5", "api_auth": "query_token",
    "webhook_verify": "token_compare", "clone_auth": "basic_password",
}


@pytest.fixture()
def gitee_card():
    with db_lock:
        db.cursor().execute(
            "INSERT OR REPLACE INTO meta (key, value) VALUES ('vcs_providers_config', ?)",
            (json.dumps({"gitee": GITEE_CARD}),))
        db.commit()
    invalidate_platform_card_cache()
    yield GITEE_CARD
    with db_lock:
        db.cursor().execute("DELETE FROM meta WHERE key='vcs_providers_config'")
        db.commit()
    invalidate_platform_card_cache()


# ---------------------------------------------------------------- S25

def test_s25_repo_key_candidates(gitee_card):
    from src.kernel.repo_key import repo_key_candidates
    # 裸 key：全部已配平台前缀 + 裸 key 本身
    cands = repo_key_candidates("o/r")
    assert "o/r" in cands
    assert "gitcode:o/r" in cands and "github:o/r" in cands
    assert "gitee:o/r" in cands           # 新平台自动加入（功能 bug 修复）
    assert "gitlab:o/r" in cands
    # 已带前缀：保留原值 + 其他平台候选
    cands2 = repo_key_candidates("gitee:o/r")
    assert cands2[0] == "gitee:o/r" and "gitcode:o/r" in cands2
    assert repo_key_candidates("") == []


def test_s25_check_repo_access_accepts_gitee(gitee_card):
    """功能 bug 修复验证：gitee: 前缀仓库能过权限校验（原双前缀候选集会 403）。"""
    from src.domains.mgmt.auth.grape_account_db import create_grape_account, create_external_identity
    from src.kernel.auth_sdk.jwt_utils import create_access_token
    from src.api.deps import check_repo_access

    acc = create_grape_account(username=f"s25_{uuid.uuid4().hex[:8]}", email="", password="pw")
    assert acc
    create_external_identity(acc["id"], "gitcode", "92501", access_token="at")
    rk = f"gitee:s25user_{uuid.uuid4().hex[:4]}/repo"
    with db_lock:
        db.cursor().execute(
            "INSERT OR REPLACE INTO managed_repos (repo_key, repo_owner, repo_name, full_name) "
            "VALUES (?, 'o', 'r', ?)", (rk, rk.split(":", 1)[1]))
        db.cursor().execute(
            "INSERT OR REPLACE INTO user_repos (user_id, repo_key, owner, name, is_admin, is_join) "
            "VALUES (?, ?, 'o', 'r', 1, 1)", (acc["id"], rk))
        db.commit()
    try:
        user = {"id": acc["id"], "username": acc["username"], "auth_type": "jwt"}
        # gitee: 前缀仓库 → 不抛 403（修复点）；未托管仓库仍 403
        check_repo_access(user, rk)
        with pytest.raises(Exception):
            check_repo_access(user, "gitee:nobody/unbound")
    finally:
        with db_lock:
            db.cursor().execute("DELETE FROM user_repos WHERE user_id=? AND repo_key=?",
                                (acc["id"], rk))
            db.cursor().execute("DELETE FROM managed_repos WHERE repo_key=?", (rk,))
            db.commit()


# ---------------------------------------------------------------- S26

def test_s26_issue_web_url_from_card(gitee_card):
    from src.kernel.repo_key import issue_web_url, pr_web_url
    # 新平台：host 来自卡
    assert issue_web_url("gitee:o/r", 5) == "https://gitee.com/o/r/issues/5"
    assert pr_web_url("gitee:o/r", 7) == "https://gitee.com/o/r/pulls/7"
    # 存量回归：三平台路径/host 不变
    assert issue_web_url("gitcode:o/r", 5) == "https://gitcode.com/o/r/issues/5"
    assert issue_web_url("github:o/r", 5) == "https://github.com/o/r/issues/5"
    assert issue_web_url("gitlab:o/r", 5) == "https://gitlab.com/o/r/-/issues/5"
    assert pr_web_url("gitlab:o/r", 7) == "https://gitlab.com/o/r/merge_requests/7"
    assert pr_web_url("github:o/r", 7) == "https://github.com/o/r/pulls/7"


def test_s26_issues_center_delegates(gitee_card):
    from src.api.issues_center import _vcs_issue_url
    assert _vcs_issue_url("gitee:o/r", 9) == "https://gitee.com/o/r/issues/9"
    assert _vcs_issue_url("gitlab:o/r", 9) == "https://gitlab.com/o/r/-/issues/9"
    assert _vcs_issue_url("gitcode:o/r", 9) == "https://gitcode.com/o/r/issues/9"
    assert _vcs_issue_url("github:o/r", 9) == "https://github.com/o/r/issues/9"


def test_s26_feedback_converter_delegates(gitee_card):
    from src.api.feedback_issue_converter import _issue_url
    assert _issue_url("gitee", "o", "r", 3) == "https://gitee.com/o/r/issues/3"
    assert _issue_url("gitlab", "o", "r", 3) == "https://gitlab.com/o/r/-/issues/3"
    assert _issue_url("gitcode", "o", "r", 3) == "https://gitcode.com/o/r/issues/3"


# ---------------------------------------------------------------- D16 守卫

def test_d16_guard_no_dual_prefix_candidate_literals():
    """守卫：禁止新增 [gitcode:{x}, github:{x}] 双前缀候选集字面量——必须走
    repo_key_candidates（由平台卡派生）。存量三处已替换，此守卫防回潮。"""
    import os
    import re
    root = os.path.join(os.path.dirname(__file__), "..", "src")
    hits = []
    pattern = re.compile(r'f"(gitcode|github):\{[^}]+\}".*f"(gitcode|github):\{[^}]+\}"|'
                         r'\[\s*f"(gitcode|github):')
    for dirpath, _dirs, files in os.walk(root):
        for fn in files:
            if not fn.endswith(".py"):
                continue
            path = os.path.join(dirpath, fn)
            with open(path, encoding="utf-8", errors="replace") as f:
                for i, line in enumerate(f, 1):
                    if pattern.search(line):
                        hits.append(f"{path}:{i}: {line.strip()[:90]}")
    assert hits == [], "双前缀候选集字面量残留/回潮（应走 repo_key_candidates）：\n" + "\n".join(hits)
