"""W11 锁：issue 读/回帖/评论同步必须走卡驱动 helper（gitee/gitlab 生效）。

背景（已实查）：event_pipeline 里一批「非 github ⇒ 当 gitcode v5」的二分，
导致 gitee/gitlab 仓 issue 读取/回帖/评论同步全部打错平台 API
（同一 gitee issue：旧 fetch_issue_via_vcs 恒 ('unknown', {})，
卡驱动 _vcs_get_issue 返回 ('exists', 完整数据)）。

本文件三条锁：
1. 结构锁：前述函数体内不得再出现 gitcode v5 裸地址 / get_repo_token_with_refresh
   旧 token 链 / _is_github_repo 平台二分；
2. 行为锁：gitee / gitlab repo_key 走卡驱动读（原生字符串 ident 不透传成 int）；
3. 行为锁：父 issue 回帖 + 评论读取统一入口是卡驱动 helper。
"""
import inspect

import pytest


def _strip_comments(src: str) -> str:
    return "\n".join(ln.split("#")[0] for ln in src.splitlines())


_GUARDED_FUNCS = (
    "_fetch_issue_assignee",
    "_fetch_issue_title_via_api",
    "fetch_issue_via_vcs",
    "_sync_issue_comments_to_conversation",
    "_post_subtask_completion_to_parent",
    "_post_all_subtasks_done",
    "_notify_scene_precheck_failure",
)


@pytest.mark.parametrize("fname", _GUARDED_FUNCS)
def test_w11_issue_io_branchless_structure(fname):
    """结构守卫：issue 读/回帖/同步函数零平台二分、零旧 gitcode v5 链。"""
    from src.domains.runtime.pipeline import event_pipeline as ep
    fn = getattr(ep, fname, None)
    assert fn is not None, f"{fname} 缺失"
    src = _strip_comments(inspect.getsource(fn))
    for forbidden in ("https://gitcode.com/api/v5", "get_repo_token_with_refresh",
                      "_is_github_repo"):
        assert forbidden not in src, (
            f"{fname} 仍含旧平台二分锚点 {forbidden!r}——"
            f"应改走卡驱动 helper（_vcs_get_issue/_list_issue_comments_card/"
            f"_post_issue_comment_ledgered）")


def test_w11_legacy_gitcode_comment_entry_retired():
    """旧裸 urllib 回帖入口已删除（避免回潮出第二个写路径）。"""
    from src.domains.runtime.pipeline import event_pipeline as ep
    assert not hasattr(ep, "_gitcode_post_issue_comment_ledgered"), (
        "旧 _gitcode_post_issue_comment_ledgered 仍在场——回帖必须唯一走 "
        "_post_issue_comment_ledgered")


def test_w11_engine_401_dedup_uses_card_reader():
    """engine/service.py 401 回帖去重读取走卡驱动 helper（gitee/gitlab 也能去重）。"""
    from src.domains.runtime.engine import service
    src = _strip_comments(inspect.getsource(service._fetch_issue_comments))
    assert "_list_issue_comments_card" in src
    assert "gitcode.com/api/v5" not in src
    assert 'repo_key.startswith("github:")' not in src
    # 401 正文平台名按卡取（不再写死 GitCode）
    fail_task_src = _strip_comments(inspect.getsource(service.fail_task)) \
        if hasattr(service, "fail_task") else ""
    if fail_task_src:
        assert '"GitCode"' not in fail_task_src, "401 回帖正文仍写死 GitCode"


def test_w11_gitee_issue_read_via_card(monkeypatch):
    """行为锁：gitee 仓（原生字符串 ident）经卡驱动读到 issue 详情。"""
    from src.domains.runtime.pipeline import event_pipeline as ep
    from src.domains.runtime.vcs.api_client import VcsApiClient

    calls = {}

    def _fake_request(self, method, path, data=None, form=False, **kw):
        calls.update(provider=self.provider, method=method, path=path, token=self.token)
        return {"title": "gitee issue title", "state": "open", "assignee": {"login": "alice"}}

    monkeypatch.setattr(VcsApiClient, "_request", _fake_request)
    monkeypatch.setattr("src.domains.mgmt.user.db.get_repo_token_with_refresh",
                        lambda repo_key="": "tok-gitee", raising=False)

    state, data = ep.fetch_issue_via_vcs("gitee:tester/livefire-s61", "IKTEST1")
    assert state == "exists", (state, data)
    assert data.get("title") == "gitee issue title"
    # provider 与原生 ident 形态都保留（不被打成 gitcode / 不被 int() 吞掉）
    assert calls["provider"] == "gitee"
    assert "IKTEST1" in calls["path"]
    assert "issues/IKTEST1" in calls["path"]

    # assignee / title 同源读取
    assert ep._fetch_issue_assignee("gitee:tester/livefire-s61", "IKTEST1") == "alice"
    assert ep._fetch_issue_title_via_api(
        "gitee:tester/livefire-s61", "IKTEST1") == "gitee issue title"


def test_w11_gitlab_issue_read_via_card(monkeypatch):
    """行为锁：gitlab 仓（notes 方言）读 issue 走同一卡驱动路径。"""
    from src.domains.runtime.pipeline import event_pipeline as ep
    from src.domains.runtime.vcs.api_client import VcsApiClient

    calls = {}

    def _fake_request(self, method, path, data=None, form=False, **kw):
        calls.update(provider=self.provider, path=path)
        return {"title": "gitlab issue title", "state": "opened"}

    monkeypatch.setattr(VcsApiClient, "_request", _fake_request)
    monkeypatch.setattr("src.domains.mgmt.user.db.get_repo_token_with_refresh",
                        lambda repo_key="": "tok-gitlab", raising=False)

    state, data = ep.fetch_issue_via_vcs("gitlab:group/proj", "42")
    assert state == "exists"
    assert data.get("title") == "gitlab issue title"
    assert calls["provider"] == "gitlab"
    assert "/projects/group%2Fproj/issues/42" in calls["path"]


def test_w11_parent_comment_uses_card_writer(monkeypatch):
    """行为锁：父 issue 回帖（子任务完成/全部完成）唯一走卡驱动 helper。"""
    from src.domains.runtime.pipeline import event_pipeline as ep

    posted = []

    def _fake_post(repository, issue_num, comment, bot_token, repo_key="", origin_task=""):
        posted.append((repository, str(issue_num), repo_key))

    monkeypatch.setattr(ep, "_post_issue_comment_ledgered", _fake_post)
    ep._post_subtask_completion_to_parent(
        "tester/livefire-s61", "1", "2", "done", "tok",
        repo_key="gitee:tester/livefire-s61")
    ep._post_all_subtasks_done(
        "tester/livefire-s61", "2",
        [{"issue_id": "#1", "stage": "done"}], "tok",
        repo_key="gitee:tester/livefire-s61")
    assert posted == [
        ("tester/livefire-s61", "2", "gitee:tester/livefire-s61"),
        ("tester/livefire-s61", "2", "gitee:tester/livefire-s61"),
    ]


def test_w11_comment_sync_uses_card_reader():
    """结构锁：issue 评论→chat 会话同步读取唯一入口是卡驱动 helper。"""
    from src.domains.runtime.pipeline import event_pipeline as ep
    src = _strip_comments(inspect.getsource(ep._sync_issue_comments_to_conversation))
    assert "_list_issue_comments_card" in src
    assert "urllib.request" not in src
    assert "_github_list_issue_comments" not in src


def test_w11_list_comments_card_gitee(monkeypatch):
    """行为锁：_list_issue_comments_card 对 gitee 仓走卡路径且保留原生 ident。"""
    from src.domains.runtime.pipeline import event_pipeline as ep
    from src.domains.runtime.vcs.api_client import VcsApiClient

    calls = {}

    def _fake_request(self, method, path, data=None, form=False, **kw):
        calls.update(provider=self.provider, method=method, path=path)
        return [{"body": "[QA Agent] hi"}]

    monkeypatch.setattr(VcsApiClient, "_request", _fake_request)
    out = ep._list_issue_comments_card(
        "gitee:tester/livefire-s61",
        "tester/livefire-s61", "IKTEST1", "tok-gitee")
    assert out == [{"body": "[QA Agent] hi"}]
    assert calls["provider"] == "gitee"
    assert "issues/IKTEST1/comments" in calls["path"]