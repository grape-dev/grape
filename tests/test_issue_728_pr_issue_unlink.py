"""Issue #728: PR body Issue 号自动关联的上下文过滤 + PR↔Issue 关联解除（unlink）。

覆盖：
- 过滤纯函数三种非语义语境（fenced code / inline code / blockquote）
- strict 模式显式关键字（Closes # / Related # / 关联 #）不回归
- create_pull 自动提取：代码块内 #N 不入 inner_issue_nums，正文 #N 入
- unlink 端点：已关联（GET 校验 → PATCH 剩余）/ 未关联幂等 / github no-op
"""
import pytest
from fastapi.testclient import TestClient

from src.api.main import app
from src.api.deps import get_current_user_or_api_key
from src.vcs.issue_ref_filter import (
    extract_related_issue_nums,
    strip_nonsemantic_context,
)

UID = 98728
OWNER = "o"
REPO = "hello"
BOT = "bot_tok_728"


@pytest.fixture(autouse=True)
def _client(monkeypatch):
    app.dependency_overrides[get_current_user_or_api_key] = \
        lambda: {"id": UID, "username": "git_tester", "auth_type": "session_token",
                 "agent_type": "vod", "user_id": 0, "session_key": "s1",
                 "repo_key": f"gitcode:{OWNER}/{REPO}"}
    yield TestClient(app)
    app.dependency_overrides.clear()


@pytest.fixture(autouse=True)
def _token(monkeypatch):
    monkeypatch.setattr("src.user.db.get_gitcode_token", lambda *a, **k: BOT)
    monkeypatch.setattr("src.user.db.get_github_token", lambda *a, **k: BOT)


def _override_user(repo_key: str):
    app.dependency_overrides[get_current_user_or_api_key] = lambda: {
        "id": UID, "username": "git_tester", "auth_type": "session_token",
        "agent_type": "vod", "user_id": 0, "session_key": "s1",
        "repo_key": repo_key}


# ====== 过滤纯函数 ======

def test_strip_fenced_code_block():
    """语境一：fenced code block 内的 #N 不被捕获。"""
    text = "正文提及 Closes #42\n```python\nprint(#99)\n```\n尾部 #7"
    cleaned = strip_nonsemantic_context(text)
    assert "#99" not in cleaned
    nums = extract_related_issue_nums(text)
    assert nums == [42, 7]


def test_strip_inline_code():
    """语境二：行内反引号内的 #N 不被捕获。"""
    text = "见 `issue #99` 的讨论；本 PR Closes #42"
    assert extract_related_issue_nums(text) == [42]


def test_strip_blockquote():
    """语境三：引用块行内的 #N 不被捕获。"""
    text = "> 引用他人评论 #99\n> 再引用 #100\n本 PR 关联 #42"
    assert extract_related_issue_nums(text) == [42]


def test_nested_and_mixed_contexts():
    """嵌套边界：代码块内含引用符号/反引号、引用块内含代码围栏——整体剥离。"""
    text = (
        "实现 #728。\n"
        "```\n> #55\n` #66`\n```\n"
        "> 引用 ``` #77 ```\n"
        "Related #42"
    )
    assert extract_related_issue_nums(text) == [728, 42]


def test_strict_mode_explicit_keywords_only():
    """strict 模式：仅 Closes # / Related # / 关联 # 前缀触发。"""
    text = "背景参考 #99 与 #100；Closes #42、related #7、关联 #5"
    assert extract_related_issue_nums(text, strict=True) == [42, 7, 5]
    # 非关键字的正文引用在 strict 模式下不触发
    assert extract_related_issue_nums("修复 #99", strict=True) == []


def test_dedupe_and_zero():
    """去重保序 + 编号 0 剔除 + 空文本安全。"""
    assert extract_related_issue_nums("a #5 b #5 c #0 d #6") == [5, 6]
    assert extract_related_issue_nums("") == []
    assert extract_related_issue_nums(None) == []


def test_strict_keyword_in_code_block_not_captured():
    """strict 关键字位于代码块内同样不算显式语义。"""
    assert extract_related_issue_nums("```\nCloses #42\n```", strict=True) == []


# ====== create_pull 自动提取（走过滤层） ======

def test_create_pull_auto_extract_filters_code_context(monkeypatch, _client):
    """body 代码块内 #99 不入 inner_issue_nums，正文 #42 入（未显式传 related）。"""
    recorded = []

    def _fake_req(method, url, token="", body=None):
        recorded.append({"method": method, "url": url, "body": body})
        return {"ok": True, "number": 9, "title": "t"}

    monkeypatch.setattr("src.user.repo_manager._req", _fake_req)
    client = _client
    r = client.post(f"/api/git-repo/{OWNER}/{REPO}/pulls", json={
        "title": "feat: #42", "head": "feat/42", "base": "main",
        "body": "Closes #42\n```bash\ngrape issues update --number #99\n```",
    })
    assert r.status_code == 200
    payload = recorded[-1]["body"]
    assert payload["inner_issue_nums"] == f"{OWNER}/{REPO}#42"
    assert "99" not in payload["inner_issue_nums"]


def test_create_pull_explicit_related_takes_priority(monkeypatch, _client):
    """显式 related_issue_nums 优先，不受 body 过滤影响。"""
    recorded = []

    def _fake_req(method, url, token="", body=None):
        recorded.append({"method": method, "url": url, "body": body})
        return {"ok": True, "number": 9, "title": "t"}

    monkeypatch.setattr("src.user.repo_manager._req", _fake_req)
    client = _client
    r = client.post(f"/api/git-repo/{OWNER}/{REPO}/pulls", json={
        "title": "feat", "head": "feat/x", "base": "main",
        "body": "正文 #99 #100", "related_issue_nums": [42],
    })
    assert r.status_code == 200
    assert recorded[-1]["body"]["inner_issue_nums"] == f"{OWNER}/{REPO}#42"


def test_create_pull_no_issue_refs_no_inner_nums(monkeypatch, _client):
    """body 无 Issue 号 → 不产生 inner_issue_nums（空列表跳过翻译，存量行为）。"""
    recorded = []

    def _fake_req(method, url, token="", body=None):
        recorded.append({"method": method, "url": url, "body": body})
        return {"ok": True, "number": 9, "title": "t"}

    monkeypatch.setattr("src.user.repo_manager._req", _fake_req)
    client = _client
    r = client.post(f"/api/git-repo/{OWNER}/{REPO}/pulls", json={
        "title": "feat", "head": "feat/x", "base": "main",
        "body": "普通描述 ``code`` 无引用",
    })
    assert r.status_code == 200
    assert "inner_issue_nums" not in recorded[-1]["body"]


# ====== unlink 端点 ======

def _unlink_fake_req(recorded, pr_issues):
    def _fake_req(method, url, token="", body=None):
        recorded.append({"method": method, "url": url, "body": body})
        if method == "GET" and url.endswith(f"/pulls/9"):
            return {"number": 9, "title": "t", "issues": pr_issues}
        return {"ok": True, "echo": {"method": method, "url": url, "body": body}}
    return _fake_req


def test_unlink_linked_issue(monkeypatch, _client):
    """已关联：GET 校验 → PATCH 剩余 inner_issue_nums（去掉目标）。"""
    recorded = []
    monkeypatch.setattr("src.user.repo_manager._req",
                        _unlink_fake_req(recorded, [{"number": 42}, {"number": 7}]))
    client = _client
    r = client.delete(f"/api/git-repo/{OWNER}/{REPO}/pulls/9/issues/42")
    assert r.status_code == 200
    data = r.json()
    assert data["unlinked"] is True
    assert data["remaining_issue_nums"] == [7]
    calls = [c for c in recorded if "pulls/9" in c["url"]]
    assert calls[0]["method"] == "GET"
    patch = [c for c in calls if c["method"] == "PATCH"]
    assert len(patch) == 1
    assert patch[0]["body"]["inner_issue_nums"] == f"{OWNER}/{REPO}#7"


def test_unlink_idempotent_when_not_associated(monkeypatch, _client):
    """幂等：关联不存在 → unlinked=false、不发 PATCH（重复调用安全）。"""
    recorded = []
    monkeypatch.setattr("src.user.repo_manager._req",
                        _unlink_fake_req(recorded, [{"number": 42}]))
    client = _client
    r = client.delete(f"/api/git-repo/{OWNER}/{REPO}/pulls/9/issues/999")
    assert r.status_code == 200
    assert r.json()["unlinked"] is False
    assert not [c for c in recorded if c["method"] == "PATCH"]


def test_unlink_last_issue_clears_association(monkeypatch, _client):
    """唯一关联被解除 → inner_issue_nums 传空串清空。"""
    recorded = []
    monkeypatch.setattr("src.user.repo_manager._req",
                        _unlink_fake_req(recorded, [{"number": 42}]))
    client = _client
    r = client.delete(f"/api/git-repo/{OWNER}/{REPO}/pulls/9/issues/42")
    assert r.status_code == 200
    assert r.json()["unlinked"] is True
    patch = [c for c in recorded if c["method"] == "PATCH"]
    assert patch[0]["body"]["inner_issue_nums"] == ""


def test_unlink_github_noop(monkeypatch, _client):
    """GitHub 通道无原生 PR↔Issue 关联 → 幂等 no-op，无上游写调用。"""
    recorded = []

    def _fake_gh_req(method, url, token="", body=None):
        recorded.append({"method": method, "url": url, "body": body})
        return {"ok": True}

    monkeypatch.setattr("src.user.repo_manager._github_req", _fake_gh_req)
    _override_user(f"github:{OWNER}/{REPO}")
    client = _client
    r = client.delete(f"/api/git-repo/{OWNER}/{REPO}/pulls/9/issues/42")
    assert r.status_code == 200
    assert r.json()["unlinked"] is False
    assert recorded == []
