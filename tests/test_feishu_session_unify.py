"""飞书 Chat 复用 Web Chat conversation 会话测试（feishu-chat-reuse-web-session.md §6.1）"""
from src.im.handler_async import _resolve_oc_sid


def test_conv_priority_when_nonempty():
    # conv.runtime_session_id 非空 → 优先 conv，不回 repo 级
    conv = {"runtime_session_id": "ses_web1", "id": "c1"}
    assert _resolve_oc_sid(conv, "ses_repoX") == "ses_web1"


def test_fallback_repo_when_empty():
    # conv 无 runtime_session_id → 返回空（不兜底 repo 级，让 opencode 在 conv cwd 新建）
    # 旧 repo 级会话目录与 conv 级 cwd 不一致，兜底会再次错位。
    assert _resolve_oc_sid({"runtime_session_id": ""}, "ses_repoX") == ""


def test_fallback_repo_when_missing():
    assert _resolve_oc_sid({}, "ses_repoX") == ""
    assert _resolve_oc_sid(None, "ses_repoX") == ""


def test_empty_both():
    assert _resolve_oc_sid({"runtime_session_id": "   "}, "") == ""
    assert _resolve_oc_sid(None, "") == ""


def test_conv_wins_over_repo_even_if_both():
    # 即使 repo 级也有值，conv 优先（飞书与 web 同源）
    assert _resolve_oc_sid({"runtime_session_id": "ses_conv"}, "ses_repoOld") == "ses_conv"