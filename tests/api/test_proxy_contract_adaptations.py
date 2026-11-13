"""代理层上游契约适配（v5 家族方言差异）行为锁——2026-11-11 部署实弹验证收口。

六个契约开关/行为（设计分片 d107-proxy-upstream-contract-adaptation-design.md）：
  ① provider 契约开关存在性（gitcode 三开关 / gitee owner-path / github 全空=通用契约）；
  ② issues update 官方 owner-path 契约：PATCH /repos/{owner}/issues/{number}（路径无 repo 段，
     body 必带 repo；gitee 的 repo 是纯仓库名，gitcode 收 owner/repo 全称）；
  ③ gitcode labels 色值必须带 # 前缀（GitHub 相反）；
  ④ gitcode milestones due_on 只认 YYYY-MM-DD（ISO 时间戳上游 400）；
  ⑤ PR comment id：gitcode/gitee 为 32 位 hex（github 数字），update/delete 前先
     hex→note_id 解析（#623 解析器复用，上游 DELETE 只认数字 note_id）；
  ⑥ gitee body.repo 纯仓库名（owner/repo 全称 404 "Not Found Project"，实测对照）。

传输层全部 mock（_vcs_req 记录调用），零外网；归因修正：gitee IK* ident 恒 404
"project or enterprise" 此前误判为"平台限制"，实为集成路径错误（repo 路径形态），
同案教训 imjoey/ge PR !45。
"""
import importlib

import pytest

gra = importlib.import_module("src.api.git_repo_api")
gitcode = importlib.import_module("src.domains.runtime.vcs.providers.gitcode")
gitee = importlib.import_module("src.domains.runtime.vcs.providers.gitee")
github = importlib.import_module("src.domains.runtime.vcs.providers.github")

_USER = {"auth_type": "session_token", "id": "7", "username": "alice"}

_HEX_ID = "7b3f1c9e2a4d5f6b8c0d1e2f3a4b5c6d"  # gitcode/gitee create 响应的 32 位 hex id
_NOTE_ID = "9876543"  # 上游数字 note_id


# ---------- ① 契约开关存在性 ----------

def test_gitcode_contract_switches_declared():
    assert gitcode.LABEL_COLOR_PREFIX_HASH is True
    assert gitcode.MILESTONE_DUE_DATE_ONLY is True
    assert gitcode.ISSUE_UPDATE_OWNER_PATH is True


def test_gitee_contract_switches_declared():
    # gitee v5 与 gitcode 同族但无 labels-#/due_on 形态差异（上游宽容），仅 owner-path 开关在册
    assert gitee.ISSUE_UPDATE_OWNER_PATH is True
    assert getattr(gitee, "LABEL_COLOR_PREFIX_HASH", False) is False
    assert getattr(gitee, "MILESTONE_DUE_DATE_ONLY", False) is False
    # milestones create 必填 due_on（PR #1231 三平台实弹：空值 400，与 gitcode 同根因）
    assert gitee.MILESTONE_DUE_ON_REQUIRED is True


def test_github_has_no_contract_switches():
    # github=通用契约基线：全部开关缺省 False（消费侧 getattr 派生，无平台枚举）
    assert getattr(github, "LABEL_COLOR_PREFIX_HASH", False) is False
    assert getattr(github, "MILESTONE_DUE_DATE_ONLY", False) is False
    assert getattr(github, "MILESTONE_DUE_ON_REQUIRED", False) is False
    assert getattr(github, "ISSUE_UPDATE_OWNER_PATH", False) is False


# ---------- 传输层 mock ----------

def _patch_transport(monkeypatch, get_resp=None):
    calls = []

    def fake_req(provider, method, url, token, payload=None):
        calls.append({"provider": provider, "method": method,
                      "url": url, "payload": payload})
        if method == "GET":
            if url.endswith(f"/pulls/comments/{_HEX_ID}"):
                return {"id": int(_NOTE_ID)}  # 单评论查询：hex id → 数字 note_id
            return get_resp if get_resp is not None else {"title": "issue title"}
        if method == "DELETE":
            return {}
        return {"number": 640, "state": "open"}

    monkeypatch.setattr(gra, "_vcs_req", fake_req)
    monkeypatch.setattr(gra, "_repo_token", lambda *a, **k: "tok")
    return calls


# ---------- ②⑥ owner-path 契约 + gitee 纯仓库名 ----------

def test_update_issue_owner_path_url_gitcode(monkeypatch):
    """gitcode：PATCH URL 走官方 owner-path 形态（无 repo 段），body.repo=owner/repo 全称。"""
    calls = _patch_transport(monkeypatch)
    gra.update_issue("grape-dev", "grape", 640,
                     gra.IssueUpdateBody(labels="P1"), user=_USER, p="gitcode")
    patches = [c for c in calls if c["method"] == "PATCH"]
    assert len(patches) == 1
    url = patches[0]["url"]
    assert url == f"{gitcode.API_BASE}/repos/grape-dev/issues/640"
    assert "/repos/grape-dev/grape/issues" not in url  # 路径无 repo 段
    payload = patches[0]["payload"]
    assert payload["repo"] == "grape"  # W1 复验实锤：update 端点只认裸仓名（全称 404 NOT_EXIST）
    assert payload["title"] == "issue title"  # title 缺失回取回填


def test_update_issue_owner_path_url_gitee_bare_repo_name(monkeypatch):
    """gitee：PATCH URL 同 owner-path 契约，但 body.repo 必须是纯仓库名（全称 404）。"""
    calls = _patch_transport(monkeypatch)
    gra.update_issue("grape-dev", "grape", 640,
                     gra.IssueUpdateBody(labels="P1"), user=_USER, p="gitee")
    patches = [c for c in calls if c["method"] == "PATCH"]
    assert len(patches) == 1
    url = patches[0]["url"]
    assert url == f"{gitee.API_BASE}/repos/grape-dev/issues/640"
    assert "/repos/grape-dev/grape/issues" not in url
    payload = patches[0]["payload"]
    assert payload["repo"] == "grape"  # ⑥ 纯仓库名（非 owner/repo 全称）
    assert payload["title"] == "issue title"


def test_update_issue_github_keeps_repo_path(monkeypatch):
    """github 无开关 → 维持 /repos/{owner}/{repo}/issues/{number} 通用形态（零回归）。"""
    calls = _patch_transport(monkeypatch)
    gra.update_issue("grape-dev", "grape", 640,
                     gra.IssueUpdateBody(labels="P1"), user=_USER, p="github")
    patches = [c for c in calls if c["method"] == "PATCH"]
    assert len(patches) == 1
    assert patches[0]["url"].startswith(f"{github.API_BASE}/repos/grape-dev/grape/issues/640")
    assert "repo" not in patches[0]["payload"]


# ---------- ③ labels 色值 # 前缀 ----------

def test_create_label_color_prefix_gitcode(monkeypatch):
    calls = _patch_transport(monkeypatch)
    gra.create_label("grape-dev", "grape", gra.LabelCreateBody(name="bug", color="ff0000"),
                     user=_USER, p="gitcode")
    assert calls[0]["payload"]["color"] == "#ff0000"


def test_create_label_color_prefix_github_unchanged(monkeypatch):
    calls = _patch_transport(monkeypatch)
    gra.create_label("grape-dev", "grape", gra.LabelCreateBody(name="bug", color="ff0000"),
                     user=_USER, p="github")
    assert calls[0]["payload"]["color"] == "ff0000"  # GitHub 契约相反：带 # 会 422


# ---------- ④ milestones due_on 日期化 ----------

def test_create_milestone_due_on_date_only_gitcode(monkeypatch):
    calls = _patch_transport(monkeypatch)
    gra.create_milestone("grape-dev", "grape",
                         gra.MilestoneCreateBody(title="weekly-11", due_on="2026-11-18T10:00:00Z"),
                         user=_USER, p="gitcode")
    assert calls[0]["payload"]["due_on"] == "2026-11-18"  # 只认 YYYY-MM-DD（ISO 时间戳 400）


def test_create_milestone_due_on_passthrough_github(monkeypatch):
    calls = _patch_transport(monkeypatch)
    gra.create_milestone("grape-dev", "grape",
                         gra.MilestoneCreateBody(title="weekly-11", due_on="2026-11-18T10:00:00Z"),
                         user=_USER, p="github")
    assert calls[0]["payload"]["due_on"] == "2026-11-18T10:00:00Z"  # 通用契约透传


# ---------- ⑤ hex comment_id → note_id 解析 ----------

def test_update_pull_comment_hex_id_resolved_to_note_id(monkeypatch):
    calls = _patch_transport(monkeypatch)
    gra.update_pull_comment("grape-dev", "grape", 12, _HEX_ID,
                            gra.CommentBody(body="edited"), user=_USER, p="gitcode")
    gets = [c for c in calls if c["method"] == "GET"]
    assert any(c["url"].endswith(f"/pulls/comments/{_HEX_ID}") for c in gets)  # 先解析
    patches = [c for c in calls if c["method"] == "PATCH"]
    assert len(patches) == 1
    assert patches[0]["url"].endswith(f"/pulls/comments/{_NOTE_ID}")  # 后写数字 note_id
    assert _HEX_ID not in patches[0]["url"]


def test_delete_pull_comment_hex_id_resolved_to_note_id(monkeypatch):
    """gitcode/gitee delete 端点只认数字 note_id（hex 直传 400 Orchestration error）。"""
    calls = _patch_transport(monkeypatch)
    gra.delete_pull_comment("grape-dev", "grape", 12, _HEX_ID, user=_USER, p="gitcode")
    deletes = [c for c in calls if c["method"] == "DELETE"]
    assert len(deletes) == 1
    assert deletes[0]["url"].endswith(f"/pulls/comments/{_NOTE_ID}")
    assert _HEX_ID not in deletes[0]["url"]


def test_numeric_comment_id_passes_through_unresolved(monkeypatch):
    """纯数字 note_id 直传（向后兼容，不触发解析 GET）。"""
    calls = _patch_transport(monkeypatch)
    gra.delete_pull_comment("grape-dev", "grape", 12, _NOTE_ID, user=_USER, p="gitcode")
    assert not [c for c in calls if c["method"] == "GET"]  # 无解析查询
    deletes = [c for c in calls if c["method"] == "DELETE"]
    assert deletes[0]["url"].endswith(f"/pulls/comments/{_NOTE_ID}")


# ---------- ⑦ create 套用 owner-path 契约（fixw11 P1：gitee 建 issue 404） ----------

def test_create_issue_owner_path_url_gitee_bare_repo_name(monkeypatch):
    """gitee：POST /repos/{owner}/issues（无 repo 段），body.repo=纯仓名——恒 404
    'project or enterprise' 的 repo 路径形态废弃（与 update 同款机制/能力位）。"""
    calls = _patch_transport(monkeypatch)
    gra.create_issue("grape-dev", "grape",
                     gra.IssueCreateBody(title="fixw11-p1", body="hello"),
                     user=_USER, p="gitee")
    posts = [c for c in calls if c["method"] == "POST"]
    assert len(posts) == 1
    url = posts[0]["url"]
    assert url == f"{gitee.API_BASE}/repos/grape-dev/issues"
    assert "/repos/grape-dev/grape/issues" not in url  # 路径无 repo 段
    payload = posts[0]["payload"]
    assert payload["repo"] == "grape"  # ⑥ 纯仓库名（非全称）
    assert payload["title"] == "fixw11-p1"


def test_create_issue_owner_path_url_gitcode(monkeypatch):
    """gitcode：官方契约同 POST /repos/{owner}/issues + body.repo（docs 实锤）。"""
    calls = _patch_transport(monkeypatch)
    gra.create_issue("grape-dev", "grape",
                     gra.IssueCreateBody(title="fixw11-p1", body="hello"),
                     user=_USER, p="gitcode")
    posts = [c for c in calls if c["method"] == "POST"]
    assert len(posts) == 1
    url = posts[0]["url"]
    assert url == f"{gitcode.API_BASE}/repos/grape-dev/issues"
    assert "/repos/grape-dev/grape/issues" not in url
    assert posts[0]["payload"]["repo"]  # body 必带 repo


def test_create_issue_github_keeps_repo_path(monkeypatch):
    """github 无开关 → 维持 POST /repos/{owner}/{repo}/issues（零回归，body 无 repo）。"""
    calls = _patch_transport(monkeypatch)
    gra.create_issue("grape-dev", "grape",
                     gra.IssueCreateBody(title="fixw11-p1", body="hello"),
                     user=_USER, p="github")
    posts = [c for c in calls if c["method"] == "POST"]
    assert len(posts) == 1
    assert posts[0]["url"] == f"{github.API_BASE}/repos/grape-dev/grape/issues"
    assert "repo" not in posts[0]["payload"]


def test_issue_endpoints_accept_string_ident(monkeypatch):
    """gitee issue 编号是 IK*/I* 字符串 ident（非数字）：detail/comments 路径按
    number 设计需兼容 str（_vcs_url 逐段 percent-encode，ident 原样透传）。"""
    calls = _patch_transport(monkeypatch)
    gra.get_issue("grape-dev", "grape", "IK3ABCD9", user=_USER, p="gitee")
    gets = [c for c in calls if c["method"] == "GET"]
    assert gets[0]["url"] == f"{gitee.API_BASE}/repos/grape-dev/grape/issues/IK3ABCD9"
    gra.create_issue_comment("grape-dev", "grape", "IK3ABCD9",
                             gra.CommentBody(body="c"), user=_USER, p="gitee")
    posts = [c for c in calls if c["method"] == "POST"]
    assert posts[0]["url"] == f"{gitee.API_BASE}/repos/grape-dev/grape/issues/IK3ABCD9/comments"
