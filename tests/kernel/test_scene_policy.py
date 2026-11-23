"""场景化作用域策略（src/kernel/scene_policy.py）矩阵用例：S1–S12。

策略表是唯一真源：不同场景 = 不同作用域语义 + 不同错误码 + 不同取券口径。
这些用例锁住「场景 → 规则」的映射本身，避免再把判定写回各个调用点。
"""
from __future__ import annotations

import pytest

from src.kernel.scene_policy import (
    SCENE_POLICIES, Scene, credential_source, decide, is_pseudo_scope)

REPO_A = "gitcode:owner-a/repo-a"
REPO_B = "gitee:owner-b/repo-b"
CHAT_SCOPE = "chat:42"


def test_s1_git_credential_same_repo_allows():
    d = decide(Scene.GIT_CREDENTIAL, REPO_A, REPO_A, target_provider="gitcode")
    assert d.ok and not d.cross_repo


def test_s2_git_credential_cross_repo_denied():
    d = decide(Scene.GIT_CREDENTIAL, REPO_A, REPO_B, target_provider="gitee")
    assert not d.ok and d.code == "REPO_SCOPE_MISMATCH"


def test_s3_git_credential_provider_must_match():
    """同 owner/repo、不同平台 = 两套凭据（同名不同平台不算同仓）。"""
    d = decide(Scene.GIT_CREDENTIAL, "gitcode:o/r", "gitcode:o/r", target_provider="gitee")
    assert not d.ok and d.code == "PROVIDER_SCOPE_MISMATCH"


def test_s4_git_credential_rejects_pseudo_scope():
    """chat 伪作用域不能换券：要它先绑仓，而不是含糊的"不匹配"。"""
    d = decide(Scene.GIT_CREDENTIAL, CHAT_SCOPE, REPO_A, target_provider="gitcode")
    assert not d.ok and d.code == "SESSION_NOT_REPO_BOUND" and d.pseudo_scope


def test_s5_project_info_is_repo_bound():
    assert decide(Scene.PROJECT_INFO, REPO_A, REPO_A).ok
    d = decide(Scene.PROJECT_INFO, REPO_A, REPO_B)
    assert not d.ok and d.code == "REPO_SCOPE_MISMATCH"


def test_s6_task_notify_cross_repo_allowed_with_visibility():
    """跨仓下派：会话绑定源仓，目标子仓 —— 用户对目标仓可见即放行。"""
    d = decide(Scene.TASK_NOTIFY, REPO_A, REPO_B, access_check=lambda target: target == REPO_B)
    assert d.ok and d.cross_repo


def test_s7_task_notify_cross_repo_denied_without_visibility():
    d = decide(Scene.TASK_NOTIFY, REPO_A, REPO_B, access_check=lambda target: False)
    assert not d.ok and d.code == "CROSS_REPO_DENIED"


def test_s8_chat_pseudo_scope_allowed_on_task_plane():
    """chat 单独处理：伪作用域在任务平面按"用户对目标仓可见"放行（否则 chat 永远派不了任务）。"""
    d = decide(Scene.TASK_NOTIFY, CHAT_SCOPE, REPO_B, access_check=lambda target: target == REPO_B)
    assert d.ok and d.cross_repo and d.pseudo_scope


def test_s9_chat_pseudo_scope_denied_without_visibility():
    d = decide(Scene.TASK_NOTIFY, CHAT_SCOPE, REPO_B, access_check=lambda target: False)
    assert not d.ok and d.code == "SESSION_NOT_REPO_BOUND"


def test_s10_task_self_access_stays_repo_bound():
    """agent 自省自己的任务（session_key=task_id）不跨仓。"""
    d = decide(Scene.TASK_ACCESS, REPO_A, REPO_B, access_check=lambda target: True)
    assert not d.ok and d.code == "REPO_SCOPE_MISMATCH"


def test_s11_unregistered_scene_is_denied():
    d = decide("not_a_scene", REPO_A, REPO_A)
    assert not d.ok and d.code == "SCENE_NOT_REGISTERED"


def test_s12_credential_source_is_derived_from_scene():
    assert credential_source(Scene.GIT_CREDENTIAL) == "explicit_rc_first"
    assert credential_source(Scene.PROJECT_INFO) == "none"
    assert credential_source(Scene.ISSUE_OP) == "resolver_chain"
    assert set(SCENE_POLICIES) >= {Scene.GIT_CREDENTIAL, Scene.TASK_NOTIFY, Scene.DISPATCH}


def test_s13_pseudo_scope_detection():
    assert is_pseudo_scope("") and is_pseudo_scope(CHAT_SCOPE)
    assert not is_pseudo_scope(REPO_A)

def test_s14_legacy_unbound_token_stays_compatible():
    """空 repo_key（历史 tk_）在 repo_bound 场景保持旧行为；chat:<uid> 则拒绝。"""
    assert decide(Scene.GIT_CREDENTIAL, "", REPO_A).ok
    assert decide(Scene.PROJECT_INFO, "", REPO_A).ok
    d = decide(Scene.GIT_CREDENTIAL, CHAT_SCOPE, REPO_A)
    assert not d.ok and d.code == "SESSION_NOT_REPO_BOUND"
