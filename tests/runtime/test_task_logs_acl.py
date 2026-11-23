"""任务日志/差异的读权限 S1–S7（评审第四点：外部提报者能读私有仓任务日志）。

口径：非本人时只放行“仓库管理方”（托管人 / user_repos.is_admin）；
`check_repo_access` 的“可见仓库”不足以读日志与 diff（提报者也有可见行）。
判定不出（契约异常/无 repo_key）→ 403 fail-closed。
"""
from __future__ import annotations

import pytest
from fastapi import HTTPException

from src.domains.runtime.routes import task_logs as tl
from src.domains.runtime.engine import service as svc
from src.domains.mgmt import public as mgmt


def _install(monkeypatch, task, manage=False, boom=False):
    monkeypatch.setattr(svc, "get_task", lambda tid: task, raising=False)
    monkeypatch.setattr(svc, "get_task", lambda tid: task)

    def _can(uid, rk):
        if boom:
            raise RuntimeError("contract down")
        return manage

    monkeypatch.setattr(mgmt, "user_can_manage_repo", _can)


def test_s1_owner_of_task_can_read(monkeypatch):
    _install(monkeypatch, {"id": "t1", "user_id": "7", "repo_key": "gitee:acme/r"})
    task = tl._require_task_access("t1", {"id": "7", "auth_type": "jwt"})
    assert task["id"] == "t1"


def test_s2_other_user_is_rejected(monkeypatch):
    _install(monkeypatch, {"id": "t1", "user_id": "7", "repo_key": "gitee:acme/r"})
    with pytest.raises(HTTPException) as ei:
        tl._require_task_access("t1", {"id": "9", "auth_type": "jwt"})
    assert ei.value.status_code == 403


def test_s3_repo_manager_can_read(monkeypatch):
    _install(monkeypatch, {"id": "t1", "user_id": "7", "repo_key": "gitee:acme/r"}, manage=True)
    assert tl._require_task_access("t1", {"id": "9", "auth_type": "jwt"})["id"] == "t1"


def test_s4_session_token_is_exempt(monkeypatch):
    _install(monkeypatch, {"id": "t1", "user_id": "7", "repo_key": "gitee:acme/r"})
    assert tl._require_task_access("t1", {"id": "9", "auth_type": "session_token"})["id"] == "t1"


def test_s5_task_without_repo_and_not_owner_is_rejected(monkeypatch):
    _install(monkeypatch, {"id": "t1", "user_id": "7", "repo_key": ""})
    with pytest.raises(HTTPException) as ei:
        tl._require_task_access("t1", {"id": "9", "auth_type": "jwt"})
    assert ei.value.status_code == 403


def test_s6_external_issue_reporter_is_rejected(monkeypatch):
    """关键回归：提报者能“看见”仓库（check_repo_access 会放行），但不能读日志/diff。"""
    _install(monkeypatch, {"id": "t1", "user_id": "7", "repo_key": "gitee:acme/private"},
             manage=False)
    from src.api import deps

    monkeypatch.setattr(deps, "check_repo_access", lambda u, rk: None)  # 旧口径会放行
    with pytest.raises(HTTPException) as ei:
        tl._require_task_access("t1", {"id": "9", "auth_type": "jwt"})
    assert ei.value.status_code == 403
    assert "error_code" in (ei.value.detail or {})


def test_s7_contract_failure_is_fail_closed(monkeypatch):
    _install(monkeypatch, {"id": "t1", "user_id": "7", "repo_key": "gitee:acme/r"}, boom=True)
    with pytest.raises(HTTPException) as ei:
        tl._require_task_access("t1", {"id": "9", "auth_type": "jwt"})
    assert ei.value.status_code == 403
