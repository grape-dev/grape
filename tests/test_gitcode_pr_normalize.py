"""Issue #49 补充：Merge Request 事件归一化细分 + L1 commit 门控测试。"""
import json

from src.api.event_rule import evaluate_event_action
from src.vcs.gitcode import GitCodeBackend

BACKEND = GitCodeBackend()

# 注入规则（不依赖 event_action_filter seed，避免环境旧库复用导致 no_rule）
def _inject_rules(monkeypatch):
    import src.api.event_rule as er
    _base = {
        "open": {"allow": 1, "actor": "any", "require_changes": [], "block_changes": [], "handle_side_effect": ""},
        "update": {"allow": 1, "actor": "user", "require_changes": [], "block_changes": [], "handle_side_effect": ""},
        "close": {"allow": 0, "actor": "any", "require_changes": [], "block_changes": [], "handle_side_effect": ""},
        "reopen": {"allow": 0, "actor": "any", "require_changes": [], "block_changes": [], "handle_side_effect": ""},
    }
    rules = {
        ("", "pull_request_opened"): dict(_base),
        ("", "pull_request_updated"): dict(_base),
        ("", "pull_request_merged"): {"merge": {"allow": 1, "actor": "any", "require_changes": [], "block_changes": [], "handle_side_effect": ""}},
    }
    monkeypatch.setattr(er, "load_event_action_rule", lambda force=False: rules)


def _merge_request_hook(payload):
    return BACKEND.normalize(json.dumps(payload), "Merge Request Hook")


def _payload(action, with_commits=False, username="dev"):
    p = {
        "object_attributes": {"action": action, "title": "t", "iid": 1},
        "repository": {"full_name": "o/r"},
        "user": {"username": username},
    }
    if with_commits:
        p["commits"] = [{"id": "abc"}]
    if action == "merge":
        p["object_attributes"]["action"] = "merge"
    return p


def test_open_create_to_pull_request_opened():
    for a in ("open", "create"):
        evt = _merge_request_hook(_payload(a))
        assert evt["type"] == "pull_request_opened"
        assert evt["payload"]["_raw_action"] == a


def test_merge_to_pull_request_merged():
    evt = _merge_request_hook(_payload("merge"))
    assert evt["type"] == "pull_request_merged"


def test_update_to_pull_request_updated_with_signal():
    evt = _merge_request_hook(_payload("update", with_commits=True))
    assert evt["type"] == "pull_request_updated"
    assert "commits" in evt["payload"]
    assert evt["payload"]["_raw_action"] == "update"
    evt0 = _merge_request_hook(_payload("update", with_commits=False))
    assert evt0["type"] == "pull_request_updated"
    assert "commits" not in evt0["payload"]


def test_close_review_reopen_taxonomy():
    # 通过审查/审查类 → 独立 PR_REVIEW（不再并成 opened）；close → PR_CLOSED；reopen → opened
    assert _merge_request_hook(_payload("approve"))["type"] == "pull_request_review"
    assert _merge_request_hook(_payload("review"))["type"] == "pull_request_review"
    assert _merge_request_hook(_payload("close"))["type"] == "pull_request_closed"
    assert _merge_request_hook(_payload("reopen"))["type"] == "pull_request_opened"


def test_pr_update_commit_gate(monkeypatch):
    _inject_rules(monkeypatch)
    monkeypatch.setattr("src.user.db.get_all_bot_usernames", lambda: {"grape_bot"})
    # user + 有 commit → allow（重新审查）
    r = evaluate_event_action("pull_request_updated", _payload("update", with_commits=True, username="dev"))
    assert r["decision"] == "allow"
    # user + 无 commit（通过审查/元数据）→ skip
    r0 = evaluate_event_action("pull_request_updated", _payload("update", with_commits=False, username="dev"))
    assert r0["decision"] == "skip" and "commit" in r0["reason"]
    # bot + 有 commit → actor 过滤（规则 actor=user）
    rb = evaluate_event_action("pull_request_updated", _payload("update", with_commits=True, username="grape_bot"))
    assert rb["decision"] == "skip"


def test_pr_open_allowed_reopen_skipped(monkeypatch):
    _inject_rules(monkeypatch)
    monkeypatch.setattr("src.user.db.get_all_bot_usernames", lambda: set())
    assert evaluate_event_action("pull_request_updated", _payload("open"))["decision"] == "allow"
    assert evaluate_event_action("pull_request_updated", _payload("close"))["decision"] == "skip"
    assert evaluate_event_action("pull_request_updated", _payload("reopen"))["decision"] == "skip"