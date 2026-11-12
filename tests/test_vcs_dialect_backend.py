"""方言策略 + GenericBackend + adapter 三层后端解析用例（S6/S16/S17）。

gitee 作为首个"零代码"验收对象：平台卡（meta 表注入）→ GenericBackend →
normalize/verify 全部命中配置，无任何 gitee 专属代码。
"""
import json
import hashlib
import hmac

import pytest

from src.kernel.session_manager import db, db_lock
from src.domains.runtime.vcs.event_types import ISSUE_OPENED, ISSUE_CLOSED, ISSUE_LABELED, ISSUE_COMMENT, PR_COMMENT, \
    PR_OPENED, PR_MERGED
from src.domains.runtime.vcs import adapter
from src.domains.runtime.vcs.platform_card import invalidate_platform_card_cache


GITEE_RAW = {
    "name": "Gitee", "dialect": "github", "host": "gitee.com",
    "webhook_path": "/webhook/gitee", "event_header": "X-Gitee-Event",
    "api_base": "https://gitee.com/api/v5", "api_auth": "query_token",
    "webhook_verify": "token_compare", "clone_auth": "basic_password",
}


@pytest.fixture()
def gitee_card():
    """向 meta 表注入 gitee 平台卡（模拟 admin 配置，D14 管理面落地前的等价物）。"""
    with db_lock:
        db.cursor().execute(
            "INSERT OR REPLACE INTO meta (key, value) VALUES ('vcs_providers_config', ?)",
            (json.dumps({"gitee": GITEE_RAW}),))
        db.commit()
    invalidate_platform_card_cache()
    yield GITEE_RAW
    with db_lock:
        db.cursor().execute("DELETE FROM meta WHERE key='vcs_providers_config'")
        db.commit()
    invalidate_platform_card_cache()


GITEE_PUSH = {
    "ref": "refs/heads/main",
    "repository": {"full_name": "oschina/git Grapes", "name": "grape"},
    "sender": {"login": "xiaoqiu"},
    "commits": [],
}

GITEE_ISSUE = {
    "action": "open",
    "repository": {"full_name": "oschina/grape", "name": "grape"},
    "sender": {"login": "xiaoqiu"},
    "issue": {"number": 5, "title": "T1", "body": "b"},
}


def test_s6_gitee_push_normalize(gitee_card):
    evt = adapter.normalize_event(json.dumps(GITEE_PUSH).encode(), "push", "gitee")
    assert evt["source"] == "gitee"
    assert evt["type"] == "push"
    assert evt["branch"] == "main"
    assert evt["actor"] == "xiaoqiu"


def test_s6_gitee_issue_normalize(gitee_card):
    evt = adapter.normalize_event(json.dumps(GITEE_ISSUE).encode(), "issues", "gitee")
    assert evt["type"] == ISSUE_OPENED
    assert evt["title"] == "T1"
    evt2 = adapter.normalize_event(json.dumps({**GITEE_ISSUE, "action": "close"}).encode(),
                                   "issues", "gitee")
    assert evt2["type"] == ISSUE_CLOSED
    evt3 = adapter.normalize_event(json.dumps({**GITEE_ISSUE, "action": "update",
                                               "changes": {"labels": {}}}).encode(),
                                   "issues", "gitee")
    assert evt3["type"] == ISSUE_LABELED


def test_s6_gitee_pr_normalize(gitee_card):
    pr = {"action": "open", "repository": {"full_name": "oschina/grape"},
          "sender": {"login": "xiaoqiu"},
          "pull_request": {"title": "PR1", "base": {"ref": "main"}}}
    evt = adapter.normalize_event(json.dumps(pr).encode(), "pull_request", "gitee")
    assert evt["type"] == PR_OPENED
    assert evt["branch"] == "main"
    merged = {"action": "close", "repository": {"full_name": "oschina/grape"},
              "sender": {"login": "u"},
              "pull_request": {"title": "PR2", "merged": True, "base": {"ref": "main"}}}
    assert adapter.normalize_event(json.dumps(merged).encode(), "pull_request", "gitee")["type"] == PR_MERGED


def test_s16_gitee_verify_dispatch(gitee_card):
    backend = adapter.get_backend("gitee")
    assert backend is not None
    # Gitee 语义：X-Gitee-Signature = secret 明文直比
    ok, _ = backend.verify({"X-Gitee-Signature": "whsec123"}, b"body", "whsec123")
    assert ok
    ok, reason = backend.verify({"X-Gitee-Signature": "wrong"}, b"body", "whsec123")
    assert not ok and "mismatch" in reason
    ok, reason = backend.verify({}, b"body", "whsec123")
    assert not ok and "missing" in reason


def test_s16_github_hmac_verify_regression():
    """存量 GitHub HMAC 验签语义回归（方言模板与现状一致）。"""
    from src.domains.runtime.vcs.dialects import GitHubDialect
    body = b"payload"
    secret = "s3cr3t"
    sig = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    d = GitHubDialect()
    assert d.verify({"X-Hub-Signature-256": sig}, body, secret)[0]
    assert not d.verify({"X-Hub-Signature-256": "sha256=dead"}, body, secret)[0]
    assert not d.verify({}, body, secret)[0]


def test_s16_gitlab_token_verify_regression():
    from src.domains.runtime.vcs.dialects import GitLabDialect
    d = GitLabDialect()
    assert d.verify({"X-Gitlab-Token": "t"}, b"b", "t")[0]
    assert not d.verify({"X-Gitlab-Token": "x"}, b"b", "t")[0]
    assert not d.verify({}, b"b", "t")[0]


def test_s17_dynamic_provider_listing(gitee_card):
    providers = dict(adapter.list_webhook_providers())
    assert "gitee" in providers and providers["gitee"] == "/webhook/gitee"
    assert adapter.resolve_source_by_path("/webhook/gitee") == "gitee"
    # 存量三平台路径解析回归
    assert adapter.resolve_source_by_path("/webhook/gitcode") == "gitcode"
    assert adapter.resolve_source_by_path("/webhook/github") == "github"
    assert adapter.resolve_source_by_path("/webhook/gitlab") == "gitlab"
    # D85 迁移注记：S83 平台卡文件化后优先级链 meta > configs/platforms/*.json >
    # 代码兜底——删 meta 后 gitee 回落文件默认卡（仍注册），"移除卡即摘除"语义
    # 已被"meta 覆盖=整卡替换"取代（S18 语义更新）。断言回落注册而非摘除。
    with db_lock:
        db.cursor().execute("DELETE FROM meta WHERE key='vcs_providers_config'")
        db.commit()
    invalidate_platform_card_cache()
    assert "gitee" in dict(adapter.list_webhook_providers())


def test_s9_existing_backends_unchanged(gitee_card):
    """存量三平台后端仍为既有类（行为不变，S9 回归锁）。"""
    from src.domains.runtime.vcs.gitcode import GitCodeBackend
    from src.domains.runtime.vcs.github import GitHubBackend
    from src.domains.runtime.vcs.gitlab import GitLabBackend
    assert isinstance(adapter.get_backend("gitcode"), GitCodeBackend)
    assert isinstance(adapter.get_backend("github"), GitHubBackend)
    assert isinstance(adapter.get_backend("gitlab"), GitLabBackend)
    # 未知 provider：无卡无类 → None（不静默回退）
    assert adapter.get_backend("nonexist") is None


def test_s6_gitlab_dialect_normalize():
    """GitLab 方言通过 GenericBackend 路径的归一（供未来 gitlab 卡化/新 gitlab 系平台）。"""
    from src.domains.runtime.vcs.dialects import GitLabDialect
    d = GitLabDialect()
    payload = {"object_kind": "issue",
               "object_attributes": {"action": "open", "title": "GL1"},
               "project": {"path_with_namespace": "grp/repo"},
               "user": {"username": "glu"}}
    evt = d.normalize(payload, "Issue Hook", "gitlab")
    assert evt["type"] == ISSUE_OPENED and evt["title"] == "GL1"
    assert evt["repository"] == "grp/repo" and evt["actor"] == "glu"
    mr = {"object_kind": "merge_request",
          "object_attributes": {"action": "merge", "title": "MR1", "source_branch": "f1"},
          "project": {"path_with_namespace": "grp/repo"}}
    assert d.normalize(mr, "Merge Request Hook", "gitlab")["type"] == PR_MERGED


def test_s70_gitee_native_event_header_forms(gitee_card):
    """S70（官方文档实锤）：gitee X-Gitee-Event 真实取值带 ' Hook' 后缀
    （'Note Hook'/'Issue Hook'/'Push Hook'/'Merge Request Hook'）——原映射
    只认小写裸名（'note'/'issues'），真实投递全落 unknown。"""
    evt = adapter.normalize_event(json.dumps(GITEE_ISSUE).encode(), "Issue Hook", "gitee")
    assert evt["type"] == ISSUE_OPENED, f"'Issue Hook' unmapped: {evt['type']}"
    comment = {**GITEE_ISSUE, "action": "commented"}
    evt2 = adapter.normalize_event(json.dumps(comment).encode(), "Note Hook", "gitee")
    assert evt2["type"] in (ISSUE_COMMENT, PR_COMMENT), f"'Note Hook' unmapped: {evt2['type']}"
    pr = {**GITEE_ISSUE, "action": "open",
          "pull_request": {"title": "PR", "base": {"ref": "main"}}}
    evt3 = adapter.normalize_event(json.dumps(pr).encode(), "Merge Request Hook", "gitee")
    assert evt3["type"] == PR_OPENED, f"'Merge Request Hook' unmapped: {evt3['type']}"
    evt4 = adapter.normalize_event(json.dumps(GITEE_PUSH).encode(), "Push Hook", "gitee")
    assert evt4["type"] == "push"


def test_s70_gitee_token_header_verify(gitee_card):
    """S70（官方文档实锤）：gitee 密码模式投递头是 X-Gitee-Token（值=password
    明文），非 X-Gitee-Signature——原 verify 只认后者，真实投递必 403。"""
    backend = adapter.get_backend("gitee")
    ok, reason = backend.verify({"X-Gitee-Token": "whsec123"}, b"body", "whsec123")
    assert ok, f"X-Gitee-Token password mode rejected: {reason}"
    ok, reason = backend.verify({"X-Gitee-Token": "wrong"}, b"body", "whsec123")
    assert not ok and "mismatch" in reason


def test_s70_gitlab_backend_verify():
    """S70（实弹发现）：GitLabBackend 缺 verify 方法——generic webhook 路由
    调 backend.verify 必 AttributeError 500（gitlab 投递全拒）。GitLab 语义：
    X-Gitlab-Token 头 = secret 明文常量时间比对。"""
    backend = adapter.get_backend("gitlab")
    assert backend is not None
    assert hasattr(backend, "verify"), "GitLabBackend missing verify()"
    ok, reason = backend.verify({"X-Gitlab-Token": "sec1"}, b"body", "sec1")
    assert ok, reason
    ok, reason = backend.verify({"X-Gitlab-Token": "wrong"}, b"body", "sec1")
    assert not ok and "mismatch" in reason
    ok, reason = backend.verify({}, b"body", "sec1")
    assert not ok and "missing" in reason
