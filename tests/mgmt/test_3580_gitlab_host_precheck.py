"""Issue #3580 / PR #1854 对齐：GitLab 托管前置校验与归属冲突（G1/G2 方案 A）。

锁两件事：
1. `_gitlab_host_precheck` 走 provider 端口——GitLab /user 只有 username（无 login），
   校验不能退回 v5 的 `login` 语义；非成员/非 Maintainer → 403，身份取不到 → 502。
2. `_host_owner_conflict`：仓库已被他人/他组织托管 → 403；同用户/同组织重入放行。
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

import pytest  # noqa: E402
from fastapi import HTTPException  # noqa: E402


@pytest.fixture(scope="session", autouse=True)
def _ensure_tables():
    from src.api import db_setup
    db_setup.init_database()


class _Prov:
    def __init__(self, info=None, perm=None, boom=None):
        self._info = info if info is not None else {}
        self._perm = perm
        self._boom = boom
        self.calls = []

    def verify_token(self, token):
        self.calls.append(("verify_token", token))
        if self._boom:
            raise RuntimeError(self._boom)
        return self._info

    def get_collaborator_permission(self, token, owner, repo, username):
        self.calls.append(("perm", owner, repo, username))
        return self._perm


def _patch_provider(monkeypatch, prov):
    import src.kernel.token_resolver as tr
    monkeypatch.setattr(tr, "create_provider", lambda p: prov)


def test_precheck_owner_uses_username_field(monkeypatch):
    """GitLab /user 返回 username（无 login）→ 仓库 owner 本人直接放行，不查成员权限。"""
    from src.domains.mgmt.routes.repos import _gitlab_host_precheck

    prov = _Prov(info={"id": 44646095, "username": "yangaiwu"})
    _patch_provider(monkeypatch, prov)
    _gitlab_host_precheck("gitlab", "yangaiwu", "hello-fastapi-2", "tok")
    assert prov.calls == [("verify_token", "tok")]


def test_precheck_non_maintainer_403(monkeypatch):
    from src.domains.mgmt.routes.repos import _gitlab_host_precheck

    prov = _Prov(info={"id": 1, "username": "someone"}, perm={"access_level": 30})
    _patch_provider(monkeypatch, prov)
    with pytest.raises(HTTPException) as ei:
        _gitlab_host_precheck("gitlab", "yangaiwu", "hello-fastapi-2", "tok")
    assert ei.value.status_code == 403
    assert isinstance(ei.value.detail, dict)
    assert ei.value.detail.get("error_code") == "PERMISSION_DENIED"


def test_precheck_maintainer_passes(monkeypatch):
    from src.domains.mgmt.routes.repos import _gitlab_host_precheck

    prov = _Prov(info={"id": 1, "username": "someone"}, perm={"access_level": 40})
    _patch_provider(monkeypatch, prov)
    _gitlab_host_precheck("gitlab", "yangaiwu", "hello-fastapi-2", "tok")
    assert ("perm", "yangaiwu", "hello-fastapi-2", "someone") in prov.calls


def test_precheck_no_member_row_403(monkeypatch):
    """非成员：get_collaborator_permission 返回 None（GitLab 404 语义）→ 403。"""
    from src.domains.mgmt.routes.repos import _gitlab_host_precheck

    prov = _Prov(info={"id": 1, "username": "someone"}, perm=None)
    _patch_provider(monkeypatch, prov)
    with pytest.raises(HTTPException) as ei:
        _gitlab_host_precheck("gitlab", "yangaiwu", "private-repo", "tok")
    assert ei.value.status_code == 403


def test_precheck_identity_failure_502(monkeypatch):
    from src.domains.mgmt.routes.repos import _gitlab_host_precheck

    prov = _Prov(boom="401 Unauthorized")
    _patch_provider(monkeypatch, prov)
    with pytest.raises(HTTPException) as ei:
        _gitlab_host_precheck("gitlab", "yangaiwu", "hello-fastapi-2", "bad-token")
    assert ei.value.status_code == 502
    assert isinstance(ei.value.detail, dict)
    assert ei.value.detail.get("error_code") == "GITLAB_IDENTITY_FAILED"


def test_precheck_empty_username_502(monkeypatch):
    from src.domains.mgmt.routes.repos import _gitlab_host_precheck

    prov = _Prov(info={"id": 1})          # 上游无 username/login
    _patch_provider(monkeypatch, prov)
    with pytest.raises(HTTPException) as ei:
        _gitlab_host_precheck("gitlab", "yangaiwu", "hello-fastapi-2", "tok")
    assert ei.value.status_code == 502


# ---- G2：归属冲突 ----

def _seed_row(repo_key, host_user_id="", org_id=""):
    from src.kernel.session_manager import db, db_lock
    with db_lock:
        c = db.cursor()
        c.execute("DELETE FROM managed_repos WHERE repo_key=?", (repo_key,))
        c.execute("INSERT INTO managed_repos (repo_key, repo_owner, repo_name, full_name, "
                  "webhook_id, host_user_id, org_id) VALUES (?,?,?,?,0,?,?)",
                  (repo_key, "yangaiwu", "r", "yangaiwu/r", host_user_id, org_id))
        db.commit()


def _drop_row(repo_key):
    from src.kernel.session_manager import db, db_lock
    with db_lock:
        c = db.cursor()
        c.execute("DELETE FROM managed_repos WHERE repo_key=?", (repo_key,))
        db.commit()


def test_owner_conflict_other_user_403():
    from src.domains.mgmt.routes.repos import _host_owner_conflict

    rk = "gitlab:yangaiwu/conflict-a"
    _seed_row(rk, host_user_id="999")
    try:
        with pytest.raises(HTTPException) as ei:
            _host_owner_conflict(rk, {"id": 33}, "")
        assert ei.value.status_code == 403
        assert isinstance(ei.value.detail, dict)
        assert ei.value.detail.get("error_code") == "REPO_HOSTED_BY_OTHER"
    finally:
        _drop_row(rk)


def test_owner_conflict_same_user_ok():
    from src.domains.mgmt.routes.repos import _host_owner_conflict

    rk = "gitlab:yangaiwu/conflict-b"
    _seed_row(rk, host_user_id="33")
    try:
        _host_owner_conflict(rk, {"id": 33}, "")      # 幂等重入不拦
    finally:
        _drop_row(rk)


def test_owner_conflict_other_org_403():
    from src.domains.mgmt.routes.repos import _host_owner_conflict

    rk = "gitlab:yangaiwu/conflict-c"
    _seed_row(rk, host_user_id="33", org_id="org-1")
    try:
        with pytest.raises(HTTPException) as ei:
            _host_owner_conflict(rk, {"id": 33}, "org-2")
        assert ei.value.status_code == 403
        assert isinstance(ei.value.detail, dict)
        assert ei.value.detail.get("error_code") == "REPO_HOSTED_BY_OTHER_ORG"
    finally:
        _drop_row(rk)


def test_owner_conflict_no_row_ok():
    from src.domains.mgmt.routes.repos import _host_owner_conflict

    _host_owner_conflict("gitlab:yangaiwu/never-hosted", {"id": 33}, "")