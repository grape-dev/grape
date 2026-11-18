"""GitLab 托管链路回归测试（2026-11 全链路修复）。

覆盖：provider 分发、URL 构造、字段适配、webhook 事件映射、密钥查询、
DDL 列、token 策略。不依赖真实 GitLab 实例，全部用 monkeypatch 桩。
"""
import json

import pytest


@pytest.fixture(autouse=True)
def _gitlab_base(monkeypatch):
    monkeypatch.setenv("GITLAB_BASE_URL", "https://gitlab.example.com")


# ---------------- repo_key / clone ----------------

def test_um_column_for_provider_three_platforms():
    from src.repo_key import um_column_for_provider
    assert um_column_for_provider("gitcode") == "gitcode_user"
    assert um_column_for_provider("github") == "github_login"
    assert um_column_for_provider("gitlab") == "gitlab_user"


def test_clone_url_gitlab_uses_self_hosted_and_nested_namespace():
    from src.runtime.repogit import clone_url_for
    assert clone_url_for("gitlab:group/sub/repo") == "https://gitlab.example.com/group/sub/repo.git"
    assert clone_url_for("gitcode:o/r") == "https://gitcode.com/o/r.git"
    assert clone_url_for("github:o/r") == "https://github.com/o/r.git"


# ---------------- providers/gitlab.build_url ----------------

def _base():
    return "https://gitlab.example.com/api/v4/projects/group%2Fsub%2Frepo"


def test_build_url_contents_and_commits():
    from src.vcs.providers.gitlab import build_url
    assert build_url("group/sub", "repo", "/contents/", {"ref": "main"}) == \
        _base() + "/repository/tree?ref=main&per_page=100"
    assert build_url("group/sub", "repo", "/contents/README.md", {"ref": "main"}) == \
        _base() + "/repository/files/README.md?ref=main"
    assert build_url("group/sub", "repo", "/commits/abc123") == \
        _base() + "/repository/commits/abc123"


def test_build_url_tree_ref_with_slash():
    from src.vcs.providers.gitlab import build_url
    assert build_url("group/sub", "repo", "/git/trees/feature/foo") == \
        _base() + "/repository/tree?ref=feature%2Ffoo&per_page=100"


def test_build_url_pulls_and_notes():
    from src.vcs.providers.gitlab import build_url
    assert build_url("group/sub", "repo", "/pulls/7/files") == \
        _base() + "/merge_requests/7/changes"
    assert build_url("group/sub", "repo", "/issues/3/comments") == \
        _base() + "/issues/3/notes"
    assert build_url("group/sub", "repo", "/pulls/7/comments/99") == \
        _base() + "/merge_requests/7/notes/99"


def test_build_url_query_state_mapping():
    from src.vcs.providers.gitlab import build_url, query_state
    assert query_state("open") == "opened"
    assert build_url("group/sub", "repo", "/issues", {"state": "open"}) == \
        _base() + "/issues?state=opened"


def test_build_url_unsupported_path_raises():
    from src.vcs.providers.gitlab import build_url
    with pytest.raises(ValueError):
        build_url("group/sub", "repo", "/pulls/comments/99")
    with pytest.raises(ValueError):
        build_url("group/sub", "repo", "/unknown/thing")


def test_adapt_payload_notes_and_milestones():
    from src.vcs.providers.gitlab import _adapt_payload
    # GitLab Notes API 只接受 body（note 是 webhook 字段，传 note 上游返回 400）
    assert _adapt_payload("POST", _base() + "/issues/1/notes", {"body": "hi"}) == {"body": "hi"}
    assert _adapt_payload("POST", _base() + "/issues/1/notes", {"note": "legacy"}) == {"body": "legacy"}
    # MR notes 不能把正文改成 description（那是 MR 本体字段）
    assert _adapt_payload("POST", _base() + "/merge_requests/7/notes", {"body": "hi"}) == {"body": "hi"}
    assert _adapt_payload("PUT", _base() + "/merge_requests/7/notes/9", {"body": "edited"}) == {"body": "edited"}
    assert _adapt_payload("PUT", _base() + "/merge_requests/7",
                          {"body": "desc", "state": "closed"}) == \
        {"description": "desc", "state_event": "close"}
    out = _adapt_payload("PATCH", _base() + "/milestones/2",
                         {"due_on": "2026-01-01", "state": "closed"})
    assert out == {"due_date": "2026-01-01", "state_event": "close"}
    assert _adapt_payload("POST", _base() + "/labels", {"name": "bug", "color": "ff0000"}) == \
        {"name": "bug", "color": "#ff0000"}


def test_normalize_fields():
    from src.vcs.providers.gitlab import _normalize
    d = _normalize({"iid": 7, "description": "d", "web_url": "u", "note": "n"})
    assert d["number"] == 7 and d["body"] == "d" and d["html_url"] == "u"
    assert _normalize({"note": "x"})["body"] == "x"


# ---------------- webhook 事件归一化 ----------------

def _evt(payload, header="unknown"):
    from src.vcs.gitlab import GitLabBackend
    return GitLabBackend().normalize(json.dumps(payload).encode(), header)


def test_mr_unknown_action_not_pr_opened():
    evt = _evt({"object_kind": "merge_request",
                "object_attributes": {"action": "approved", "iid": 9},
                "project": {"path_with_namespace": "g/r"}})
    assert evt["type"] == "unknown"
    assert evt["object_iid"] == "9"


def test_commit_note_is_not_issue_comment():
    evt = _evt({"object_kind": "note",
                "object_attributes": {"noteable_type": "Commit", "iid": 55},
                "project": {"path_with_namespace": "g/r"}})
    assert evt["type"] == "unknown"


def test_noteable_iid_preferred_for_notes():
    evt = _evt({"object_kind": "note",
                "object_attributes": {"noteable_type": "Issue", "iid": 55,
                                      "noteable_iid": 12},
                "project": {"path_with_namespace": "g/r"}})
    assert evt["object_iid"] == "12"


def test_event_header_fallback():
    evt = _evt({"object_attributes": {"action": "open", "iid": 4},
                "project": {"path_with_namespace": "g/r"}}, "Issue Hook")
    assert evt["type"].startswith("issue")


def test_nested_namespace_repo():
    evt = _evt({"object_kind": "push", "ref": "refs/heads/main",
                "project": {"path_with_namespace": "group/sub/repo"}})
    assert evt["repository"] == "group/sub/repo"
    assert evt["branch"] == "main"


def test_webhook_path_overrides_missing_platform_header():
    from src.vcs.adapter import get_event_header
    assert get_event_header({"x-grape-webhook-path": "/webhook/gitlab"}) == ("unknown", "gitlab")
    assert get_event_header({"X-Gitlab-Event": "Push Hook"}) == ("Push Hook", "gitlab")


# ---------------- DB / webhook 密钥 ----------------

def test_ddl_has_gitlab_columns():
    from src.api import db_setup
    from src.user import db as udb
    assert "bot_gitlab_token" in udb._MANAGED_REPOS_DDL
    assert "gitlab_webhook_token" in udb._MANAGED_REPOS_DDL
    assert "repo_gitlab_token" in db_setup._AGENT_SESSIONS_TABLE_DDL
    assert "gitlab_username" in db_setup._LOGIN_WHITELIST_DDL


def test_webhook_url_for_provider_handles_custom_paths():
    from src.user.db import webhook_url_for_provider
    assert webhook_url_for_provider("https://x/webhook/github", "gitlab") == \
        "https://x/webhook/gitlab"
    assert webhook_url_for_provider("https://x/webhook/gitlab", "gitcode") == \
        "https://x/webhook/gitcode"
    assert webhook_url_for_provider("https://x", "gitlab") == "https://x/webhook/gitlab"


def test_webhook_verify_token_field_by_provider():
    import inspect
    from src.user import db as udb
    src = inspect.getsource(udb.get_webhook_verify_token)
    assert "gitlab_webhook_token" in src


# ---------------- API 客户端 ----------------

def test_vcs_api_client_gitlab_comment_uses_body(monkeypatch):
    from src.vcs.api_client import VcsApiClient
    captured = {}

    def fake(url, method, data=None, headers=None):
        captured.update(url=url, method=method, data=data, headers=headers)
        return {"id": 1, "body": "ok"}

    monkeypatch.setattr(VcsApiClient, "_do_request", staticmethod(fake))
    client = VcsApiClient("gitlab", "tok", "group/sub", "repo")
    resp = client.post_issue_comment(3, "hello")
    assert captured["data"] == {"body": "hello"}
    assert "/issues/3/notes" in captured["url"]
    assert resp["body"] == "ok"


def test_gitlab_comment_translate_rules_keep_body():
    """中性契约层 issues/pulls comment 也必须保持 body，不能翻成 note。"""
    from src.vcs.providers.gitlab import translate
    assert translate("issues", "comment", {"body": "hi"}, {}) == {"body": "hi"}
    assert translate("pulls", "comment", {"body": "hi"}, {}) == {"body": "hi"}


def test_post_result_to_pr_gitlab_payload_uses_body(monkeypatch):
    """PR 订阅回帖走裸 urllib 的 GitLab 分支：payload 必须是 body。"""
    import urllib.request
    from src.api import event_pipeline as ep

    captured = {}

    class _FakeResp:
        def read(self):
            return b'{"id": 1}'

    def fake_urlopen(req, timeout=None):
        captured["url"] = req.full_url
        captured["method"] = req.get_method()
        captured["payload"] = json.loads(req.data.decode("utf-8"))
        return _FakeResp()

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(ep, "_ledger_record_comment", lambda *a, **k: None)
    ep._post_result_to_pr("group/sub/repo", "7", "结论正文", "gl_tok",
                          "qa", repo_key="gitlab:group/sub/repo", agent_result=False)
    assert captured["payload"] == {
        "body": "[QA Agent] 结论正文\n<!-- grape-auto-comment -->"}
    assert "/merge_requests/7/notes" in captured["url"]
    assert captured["method"] == "POST"


def test_global_bot_token_does_not_fall_back_to_gitcode(monkeypatch):
    import src.user.db as udb
    from src.token_resolver import create_provider, global_bot_token_for_provider
    monkeypatch.setattr(udb, "_cached_get_meta", lambda key, file_fallback_fn=None: "gl_bot_tok")
    monkeypatch.setattr(udb, "_read_gbt_file_field", lambda k: "")
    assert global_bot_token_for_provider(create_provider("gitlab")) == "gl_bot_tok"


def test_get_bot_token_probes_all_providers(monkeypatch):
    import src.user.db as udb
    from src.api import deps
    seen = []

    def fake(rk):
        seen.append(rk)
        return "gl_tok" if rk.startswith("gitlab:") else ""

    monkeypatch.setattr(udb, "get_repo_token_with_refresh", fake)
    assert deps.get_bot_token("o", "r") == "gl_tok"
    assert seen == ["gitcode:o/r", "github:o/r", "gitlab:o/r"]


def test_gitlab_request_401_retries_with_private_token(monkeypatch):
    """自建实例 PAT 只认 PRIVATE-TOKEN 时，VcsApiClient 应自动重试一次。"""
    from src.vcs.api_client import VcsApiClient
    calls = []

    def fake(url, method, data=None, headers=None):
        calls.append(dict(headers or {}))
        if len(calls) == 1:
            return {"error": "HTTP 401", "status_code": 401}
        return {"iid": 5}

    monkeypatch.setattr(VcsApiClient, "_do_request", staticmethod(fake))
    resp = VcsApiClient("gitlab", "glpat-xxxx", "g", "r").get_issue(5)
    assert len(calls) == 2
    assert calls[0].get("Authorization") == "Bearer glpat-xxxx"
    assert "Authorization" not in calls[1]
    assert calls[1].get("PRIVATE-TOKEN") == "glpat-xxxx"
    assert resp["number"] == 5  # _normalize 生效


def test_post_issue_comment_can_skip_ledger(monkeypatch):
    """调用方自行记账时可关闭客户端内置台账，避免重复写。"""
    from src.vcs import api_client
    recorded = []
    monkeypatch.setattr(api_client.VcsApiClient, "_do_request",
                        staticmethod(lambda *a, **k: {"id": 1}))
    c = api_client.VcsApiClient("gitlab", "tok", "g", "r")
    monkeypatch.setattr(c, "_ledger", lambda *a, **k: recorded.append(a))
    c.post_issue_comment(1, "hi", record_ledger=False)
    assert recorded == []
    c.post_issue_comment(1, "hi")
    assert len(recorded) == 1


# ---------------- agent 环境变量 / 领域层 ----------------

def test_prompt_builder_vcs_meta_for_gitlab():
    from src import prompt_builder as pb
    rk = "gitlab:group/sub/repo"
    assert pb._vcs_api_base(rk) == "https://gitlab.example.com/api/v4"
    assert pb._vcs_name(rk) == "GitLab"
    assert pb._vcs_token_env(rk) == "USER_GITLAB_TOKEN"
    assert pb._vcs_bot_token_env(rk) == "BOT_GITLAB_TOKEN"
    assert pb._vcs_api_base("github:o/r") == "https://api.github.com"
    assert pb._vcs_api_base("gitcode:o/r") == "https://gitcode.com/api/v5"


def test_feedback_create_issue_gitlab_uses_build_url(monkeypatch):
    """GitLab provider 无静态 API_BASE，必须走 build_url 拼 /projects/{path}/issues。"""
    from src.api import feedback_issue_converter as fic
    from src.vcs.providers import gitlab as gl_mod
    captured = {}

    def fake_request(method, url, token, body=None):
        captured.update(method=method, url=url, token=token, body=body)
        return {"iid": 9}

    monkeypatch.setattr(gl_mod, "request", fake_request)
    out = fic._create_provider_issue("gitlab", "group/sub", "repo", "tok", "标题", "正文")
    assert captured["url"] == (
        "https://gitlab.example.com/api/v4/projects/group%2Fsub%2Frepo/issues")
    assert captured["body"]["description"] == "正文"
    assert out["number"] == 9
    assert out["url"] == "https://gitlab.example.com/group/sub/repo/-/issues/9"


def test_runtime_fetch_issue_comments_gitlab(monkeypatch):
    """worker 侧评论去重不能打 gitcode.com，且 note 要归一为 body。"""
    import src.user.repo_manager as rm
    from src.runtime import service
    seen = {}

    def fake_req(method, url, token="", body=None):
        seen.update(method=method, url=url, token=token)
        return [{"id": 1, "note": "already posted"}]

    monkeypatch.setattr(rm, "_gitlab_req", fake_req)
    out = service._fetch_issue_comments("gitlab:group/sub/repo", "group/sub/repo", "3", "tok")
    assert seen["url"] == (
        "https://gitlab.example.com/api/v4/projects/group%2Fsub%2Frepo"
        "/issues/3/notes?per_page=100")
    assert out[0]["body"] == "already posted"


def test_repo_admin_identity_uses_gitlab_channel(monkeypatch):
    """终审 admin 判定必须拿 GitLab 身份 + GitLab OAuth token，不能退化成 GitCode。"""
    from src.auth import repo_roles
    from src.auth import grape_account_db as gdb
    from src.api import oauth_token
    seen = {}

    monkeypatch.setattr(gdb, "get_user_identities_with_tokens",
                        lambda uid: [{"provider": "gitlab", "external_login": "alice"}])

    def fake_with_oauth(user, provider, fn, *a, **k):
        seen["provider"] = provider
        return fn("gl_tok")

    monkeypatch.setattr(oauth_token, "with_oauth_token", fake_with_oauth)

    def fake_admin(tok, o, r, u):
        seen["admin_args"] = (tok, o, r, u)
        return True

    monkeypatch.setattr(repo_roles, "_check_gitlab_admin", fake_admin)
    out = repo_roles.get_repo_admin_identity({"id": 1}, "gitlab:group/sub/repo")
    assert seen["provider"] == "gitlab"
    assert seen["admin_args"] == ("gl_tok", "group", "sub/repo", "alice")
    assert out == {"username": "alice", "source": "permission"}


def test_close_vcs_issues_reports_gitlab_key(monkeypatch):
    from src.api.routes import issues as issues_mod
    monkeypatch.setattr(issues_mod, "_find_github_mirror_repo_key", lambda rk: "")
    monkeypatch.setattr(issues_mod, "_try_close_issue_for_repo",
                        lambda rk, n, u, is_github: "success")
    out = issues_mod._close_vcs_issues("gitlab:group/sub/repo", 7, {"id": 1})
    assert out["gitlab"] == "success"
    assert out["gitcode"] == "skipped"
    assert out["github"] == "skipped"


def test_feishu_gitlab_collaborators_shape(monkeypatch):
    """飞书协作者同步：GitLab 走自建实例 members/all，字段归一为 GitCode 口径。"""
    pytest.importorskip("lark_oapi")
    import src.user.repo_manager as rm
    from src.im import feishu_async
    seen = {}

    def fake_req(method, url, token="", body=None):
        seen.update(url=url, token=token)
        return [{"id": 8, "username": "alice", "name": "Alice"},
                {"id": 9, "username": "bob"}]

    monkeypatch.setattr(rm, "_gitlab_req", fake_req)
    out = feishu_async._fetch_gitlab_collaborators("group/sub", "repo", "gl_tok")
    assert seen["url"].startswith(
        "https://gitlab.example.com/api/v4/projects/group%2Fsub%2Frepo/members/all")
    assert out[0] == {"nick_name": "Alice", "username": "alice", "login": "alice"}
    assert out[1]["nick_name"] == "bob"


# ---------------- 全局 bot token / DB 列 ----------------

class _FakeCursor:
    def __init__(self, sink, rows=None):
        self.sink = sink
        self.rows = rows or []

    def execute(self, sql, params=()):
        self.sink.append((sql, params))

    def fetchall(self):
        return self.rows


class _FakeDB:
    def __init__(self, sink, rows=None):
        self.sink = sink
        self.rows = rows or []

    def cursor(self):
        return _FakeCursor(self.sink, self.rows)

    def commit(self):
        pass


class _FakeLock:
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _stub_db(monkeypatch, mod, rows=None):
    sink = []
    monkeypatch.setattr(mod, "db", _FakeDB(sink, rows))
    monkeypatch.setattr(mod, "db_lock", _FakeLock())
    return sink


def test_update_repo_bot_token_writes_gitlab_columns(monkeypatch):
    import src.user.db as udb
    sink = _stub_db(monkeypatch, udb)
    udb._update_repo_bot_token("gitlab:g/r", "tok", "gitlab",
                               webhook_token="tok", webhook_id="7")
    sql, params = sink[0]
    assert "bot_gitlab_token=?" in sql
    assert "gitlab_webhook_token=?" in sql
    assert "bot_gitcode_token" not in sql
    assert params == ("tok", "tok", 7, "gitlab:g/r")


def test_query_path_b_repos_gitlab_isolated(monkeypatch):
    import src.user.db as udb
    sink = _stub_db(monkeypatch, udb, rows=[])
    udb._query_path_b_repos("gitlab")
    sql = sink[0][0]
    assert "bot_gitlab_token" in sql and "repo_key LIKE 'gitlab:%'" in sql
    sink.clear()
    udb._query_path_b_repos("gitcode")
    sql = sink[0][0]
    assert "NOT LIKE 'gitlab:%'" in sql  # 不能把 GitLab 仓扫进 GitCode 升级


def test_gitlab_member_uses_numeric_user_id(monkeypatch):
    import src.user.repo_manager as rm
    calls = []

    def fake_req(method, url, token="", body=None):
        calls.append((method, url, body))
        if method == "GET":
            return [{"id": 42, "username": "alice"}]
        return {"id": 42}

    monkeypatch.setattr(rm, "_gitlab_req", fake_req)
    assert rm._gitlab_user_id("tok", "alice") == 42
    assert rm.add_gitlab_member("tok", "group/sub", "repo", "alice", "maintainer") is True
    _post = [c for c in calls if c[0] == "POST"][0]
    assert _post[1].endswith("/projects/group%2Fsub%2Frepo/members")
    assert _post[2] == {"user_id": 42, "access_level": 40}
    rm.remove_gitlab_member("tok", "group/sub", "repo", "alice")
    assert any(c[0] == "DELETE" and c[1].endswith("/members/42") for c in calls)


def test_global_bot_token_by_repo_gitlab(monkeypatch):
    from src.runtime import service
    import src.token_resolver as tr
    monkeypatch.setattr(tr, "global_bot_token_for_provider", lambda p: "gl_bot")
    assert service._global_bot_token_by_repo("gitlab:g/r") == "gl_bot"


def test_sync_attachment_comment_uses_provider_client(monkeypatch):
    """附件清单回帖不能硬编码 GitCode 客户端/域名。"""
    from src.api import issues_center as ic
    import src.user.db as udb
    seen = {}

    class FakeClient:
        def post_issue_comment(self, num, body):
            seen.update(num=num, body=body)
            return {"id": 1}

    monkeypatch.setattr(udb, "get_repo_token_with_refresh", lambda rk: "gl_tok")
    import src.vcs.api_client as ac

    def fake_create_client(rk, token):
        seen["rk"] = rk
        seen["token"] = token
        return FakeClient()

    monkeypatch.setattr(ac, "create_client", fake_create_client)
    ok = ic.sync_issue_attachment_comment(
        "gitlab:group/sub/repo", 5,
        {"filename": "a.png", "url": "https://x/a.png", "is_image": True})
    assert ok is True
    assert seen["rk"] == "gitlab:group/sub/repo"
    assert seen["num"] == 5 and "## 附件" in seen["body"]


# ---------------- 控制台建单 400 / PAT 误刷新 / Webhook 403 ----------------

_NESTED_FULL_NAME = "biz/jmbos/devops/ops-inspection"
_ROWS = [
    ("gitlab:" + _NESTED_FULL_NAME, "OLD_SECRET", "", "", "GL_WH", "", "", "GL_BOT"),
    ("gitcode:" + _NESTED_FULL_NAME, "GC_SECRET", "GC_TOK", "", "", "", "", ""),
]


class _RowsCursor:
    """按 SQL 形状返回预置行的轻量游标桩（不碰真实 DB）。"""

    def __init__(self, rows, fail_first=False):
        self.rows = rows
        self.fail_first = fail_first
        self.sql = ""
        self.params = None
        self.calls = 0

    def execute(self, sql, params=None):
        self.calls += 1
        self.sql = sql
        self.params = params
        if self.fail_first and self.calls == 1 and "bot_gitlab_token" in sql:
            raise RuntimeError("no such column: bot_gitlab_token")  # 模拟未迁移旧库
        return self

    def fetchall(self):
        if "SELECT repo_key FROM" in self.sql:
            return [(r[0],) for r in self.rows]
        keys = list(self.params or [])
        if "bot_gitlab_token" in self.sql:
            return [r for r in self.rows if r[0] in keys]
        if "gitlab_webhook_token" in self.sql:
            return [r[:5] for r in self.rows if r[0] in keys]
        return [r[:4] for r in self.rows if r[0] in keys]


def _patch_db(monkeypatch, rows=None, fail_first=False):
    import threading
    import src.user.db as udb

    class _RowsDB:
        def __init__(self):
            self._cur = _RowsCursor(rows if rows is not None else _ROWS, fail_first)

        def cursor(self):
            return self._cur

    monkeypatch.setattr(udb, "db", _RowsDB())
    monkeypatch.setattr(udb, "db_lock", threading.RLock())


def test_provider_for_full_name_resolves_gitlab_nested(monkeypatch):
    """无前缀 full_name（含子组）必须以托管登记为准，不能默认 gitcode。"""
    import src.user.db as udb
    _patch_db(monkeypatch)
    assert udb.provider_for_full_name(_NESTED_FULL_NAME) == "gitlab"
    assert udb.provider_for_full_name("unknown/repo") == "gitcode"


def test_create_issue_infers_gitlab_prefix_for_nested_repo(monkeypatch):
    """控制台 POST /api/issues/{group}/{sub}/{repo} 补上 gitlab: 前缀。"""
    import asyncio
    from src.api.routes import issues
    import src.api.issues_center as ic
    captured = {}

    async def fake_create(b, user):
        captured["repo_key"] = b.repo_key
        return {"ok": True}

    monkeypatch.setattr(ic, "create_task_via_issue", fake_create)
    monkeypatch.setattr(issues, "_provider_for_full_name", lambda fn: "gitlab")
    asyncio.run(issues.create_issue(
        "biz", "jmbos/devops/ops-inspection",
        {"title": "巡检", "description": "描述"},
        {"id": 1, "username": "u"}))
    assert captured["repo_key"] == "gitlab:" + _NESTED_FULL_NAME


def test_gitlab_create_task_falls_back_to_repo_token(monkeypatch):
    """账号未绑定 GitLab（PAT 托管场景）时仍能建单，不再直接 400。"""
    import asyncio
    from fastapi import HTTPException
    import src.api.issues_center as ic
    import src.api.deps as deps
    import src.user.db as udb

    monkeypatch.setattr(deps, "get_user_gitlab_token_with_refresh", lambda *a, **k: "")
    monkeypatch.setattr(udb, "get_repo_token_with_refresh", lambda rk: "repo_gl_tok")
    seen = {}

    def fake_find(token, owner, repo, title, provider="gitcode"):
        seen.update(token=token, provider=provider)
        raise RuntimeError("stop here")

    monkeypatch.setattr(ic, "_find_open_issue_by_title", fake_find)
    body = ic.TaskCreateBody(repo_key="gitlab:" + _NESTED_FULL_NAME,
                             title="巡检", description="描述")
    with pytest.raises(HTTPException) as ei:
        asyncio.run(ic.create_task_via_issue(
            body, {"id": 1, "username": "u", "auth_type": "jwt"}))
    assert seen["token"] == "repo_gl_tok"
    assert seen["provider"] == "gitlab"
    assert ei.value.status_code == 502  # 已越过「GitLab Token 未配置」的 400


def test_pat_without_refresh_token_skips_oauth_refresh(monkeypatch):
    """PAT 身份没有 refresh_token：不再打 no refresh_token 警告/发起无效刷新。"""
    import src.api.deps as deps
    import src.auth.grape_account_db as gad
    called = []
    monkeypatch.setattr(deps, "_get_oauth_token_from_identity", lambda u, p: "")
    monkeypatch.setattr(gad, "_has_refresh_token", lambda uid, p: False)
    monkeypatch.setattr(deps, "_try_refresh_provider_token",
                        lambda uid, p: called.append((uid, p)) or "")
    assert deps.get_user_gitlab_token_with_refresh("u", user_id=1) == ""
    assert called == []


def test_with_oauth_token_pat_reauth_carries_provider(monkeypatch):
    """401 且无 refresh_token 时不刷新，reauth 信号带 provider 供前端跳 OAuth。"""
    from fastapi import HTTPException
    import src.api.oauth_token as ot
    import src.api.deps as deps
    import src.auth.grape_account_db as gad
    import src.user.db as udb
    monkeypatch.setattr(deps, "get_user_gitlab_token_with_refresh",
                        lambda *a, **k: "glpat-valid")
    monkeypatch.setattr(gad, "_has_refresh_token", lambda uid, p: False)
    called = []
    monkeypatch.setattr(udb, "refresh_oauth_token",
                        lambda uid, p: called.append(p) or None)

    def api_call(token):
        raise RuntimeError("GitLab API failed (401): token expired")

    with pytest.raises(HTTPException) as ei:
        ot.with_oauth_token({"id": 1, "username": "u"}, "gitlab", api_call)
    assert called == []
    assert ei.value.detail["provider"] == "gitlab"
    assert ei.value.detail["action"] == "reauth_required"


def test_webhook_tokens_by_repo_returns_provider_candidates(monkeypatch):
    """GitLab 校验候选含历史 secret / webhook token / bot token，且不含跨平台的 secret。"""
    import src.user.db as udb
    _patch_db(monkeypatch)
    toks = udb.get_webhook_tokens_by_repo(_NESTED_FULL_NAME, "gitlab")
    assert toks == ["OLD_SECRET", "GL_WH", "GL_BOT"]
    assert "GC_SECRET" not in toks
    assert udb.get_webhook_token_by_repo(_NESTED_FULL_NAME, "gitlab") == "OLD_SECRET"


def test_webhook_tokens_by_repo_legacy_columns(monkeypatch):
    """未迁移旧库缺 bot 列时降级，仍保留 webhook_secret 与 gitlab_webhook_token。"""
    import src.user.db as udb
    _patch_db(monkeypatch,
              rows=[("gitlab:" + _NESTED_FULL_NAME, "S1", "", "", "GL_WH", "", "", "")],
              fail_first=True)
    assert udb.get_webhook_tokens_by_repo(_NESTED_FULL_NAME, "gitlab") == ["S1", "GL_WH"]


def test_hosting_provider_config_has_gitlab(monkeypatch):
    """repair_host 不能用 GitCode 的 bot token / 回调 / DB 列修 GitLab 仓库。"""
    import src.user.repo_manager as rm
    import src.token_resolver as tr
    monkeypatch.setattr(tr, "global_bot_token_for_provider", lambda p: "gl_bot")
    monkeypatch.setattr(tr, "global_bot_usernames_for_provider", lambda p: ["gl_bot"])
    cfg = rm.get_hosting_provider_config("gitlab")
    assert cfg["webhook_url_suffix"] == "/webhook/gitlab"
    assert cfg["db_token_field"] == "gitlab_webhook_token"
    assert cfg["db_bot_token_field"] == "bot_gitlab_token"
    assert cfg["setup_webhook"] is rm.setup_gitlab_webhook
    assert cfg["bot_token"] == "gl_bot"


def test_gitlab_webhook_accepts_any_candidate(monkeypatch):
    """上游 token 与 DB 首选值不一致（bot token 重建后）仍应放行，而非 403。"""
    import asyncio
    from starlette.requests import Request
    from src.api.routes import webhook
    import src.user.db as udb

    monkeypatch.setattr(udb, "get_webhook_tokens_by_repo",
                        lambda fn, p: ["OLD_SECRET", "GL_BOT"])
    monkeypatch.setattr(webhook, "run_pipeline", lambda *a, **k: None)

    async def _call(token):
        body = json.dumps({"project": {"path_with_namespace": _NESTED_FULL_NAME}}).encode()
        scope = {
            "type": "http", "http_version": "1.1", "method": "POST",
            "path": "/webhook/gitlab", "raw_path": b"/webhook/gitlab",
            "query_string": b"", "scheme": "http",
            "headers": [(b"x-gitlab-token", token.encode()),
                        (b"x-gitlab-event", b"Issue Hook"),
                        (b"content-type", b"application/json")],
            "client": ("10.0.16.85", 60444), "server": ("testserver", 80),
        }
        queued = [{"type": "http.request", "body": body, "more_body": False}]

        async def receive():
            return queued.pop(0) if queued else {"type": "http.disconnect"}

        return await webhook.gitlab_webhook(Request(scope, receive))

    ok = asyncio.run(_call("GL_BOT"))
    assert isinstance(ok, dict) and ok.get("status") == "ok"
    bad = asyncio.run(_call("WRONG_TOKEN"))
    assert bad.status_code == 403


def test_add_managed_repo_can_clear_stale_webhook_secret(monkeypatch):
    """路径 A 重建 webhook 时必须能清掉路径 B 残留的随机 secret。"""
    import threading
    import src.user.db as udb
    captured = {}

    class Cur:
        def execute(self, sql, params=None):
            if "SELECT host_user_id" in sql:
                self._one = (1, "OLD_SECRET", "", "")
            elif "UPDATE managed_repos SET" in sql and "webhook_secret=?" in sql:
                captured["params"] = params
            return self

        def fetchone(self):
            return getattr(self, "_one", None)

    class DB:
        def cursor(self):
            return Cur()

        def commit(self):
            pass

    monkeypatch.setattr(udb, "db", DB())
    monkeypatch.setattr(udb, "db_lock", threading.RLock())
    assert udb.add_managed_repo("gitlab:g/r", "g", "r", bot_gitlab_token="BOT",
                                clear_webhook_secret=True) is True
    assert captured["params"][12] == ""  # _final_secret 位置


def test_update_gitlab_webhook_token_puts_token(monkeypatch):
    """复用旧 hook 时用 PUT 覆盖 token，保证上游与 DB 密钥一致。"""
    import src.user.repo_manager as rm
    calls = []
    monkeypatch.setattr(rm, "_gitlab_req",
                        lambda m, u, t, b=None: calls.append((m, u, t, b)))
    rm.update_gitlab_webhook_token("tok", "group/sub", "repo", 7, "NEW_SECRET")
    assert calls[0][0] == "PUT"
    assert calls[0][1].endswith("/projects/group%2Fsub%2Frepo/hooks/7")
    assert calls[0][3] == {"token": "NEW_SECRET"}
