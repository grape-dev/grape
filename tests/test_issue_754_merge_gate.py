"""Issue #754：merge 复测失败（unresolved-marker）硬阻断合并 + 阻断留痕.

场景覆盖（验收①②）：
- PR diff 含行首 <<<<<<< / >>>>>>> 冲突标记 → HTTP 409 阻断、不透传 merge PUT、
  台账记 pr_merge_blocked、PR 评论留痕 best-effort 发出。
- 无冲突标记 → merge 正常透传（回归不受影响）。
- 预检上游失败（405 等）→ fail-open 透传合并（#549/#558 语义不变）。
"""
import pytest
from fastapi.testclient import TestClient

from src.api.main import app
from src.api.deps import get_current_user_or_api_key
from src.user.repo_manager import UpstreamAPIError

UID = 98754
OWNER = "o"
REPO = "hello"
BOT = "bot_tok_754"

base = f"/api/git-repo/{OWNER}/{REPO}"


@pytest.fixture(autouse=True)
def _client(monkeypatch):
    app.dependency_overrides[get_current_user_or_api_key] = lambda: {
        "id": UID, "username": "git_tester", "auth_type": "session_token",
        "agent_type": "vod", "user_id": 0, "session_key": "s1",
        "repo_key": f"gitcode:{OWNER}/{REPO}"}
    monkeypatch.setattr("src.user.db.get_gitcode_token", lambda *a, **k: BOT)
    yield TestClient(app)
    app.dependency_overrides.clear()


def _install(monkeypatch, files_pages):
    """mock repo_manager._req：files 分页返回 files_pages；其余调用记录并回 echo。"""
    recorded = []
    state = {"files_page": 0}

    def _fake_req(method, url, token="", body=None):
        recorded.append({"method": method, "url": url, "token": token, "body": body})
        if f"/pulls/5/files" in url:
            idx = state["files_page"]
            state["files_page"] += 1
            return files_pages[idx] if idx < len(files_pages) else []
        return {"ok": True, "echo": {"method": method, "url": url, "body": body},
                "merged": True, "sha": "b8b0c76"}

    monkeypatch.setattr("src.user.repo_manager._req", _fake_req)
    return recorded


def test_merge_blocked_on_conflict_markers(monkeypatch, _client):
    diff = ("@@ -1,3 +1,6 @@\n+<<<<<<< HEAD\n+ours\n+=======\n+theirs\n+>>>>>>> feat/x\n")
    pages = [[{"filename": "src/a.py", "patch": {"diff": diff}},
              {"filename": "src/b.py", "patch": {"diff": "@@ -1 +1 @@\n+clean\n"}}]]
    recorded = _install(monkeypatch, pages)
    ledger = []
    monkeypatch.setattr("src.outbound_ledger.record_outbound",
                        lambda *a, **k: ledger.append((a, k)) or True)

    r = _client.put(f"{base}/pulls/5/merge")
    assert r.status_code == 409
    detail = r.json()["detail"]
    assert detail["code"] == 409
    assert "src/a.py" in detail["message"]
    assert "conflict markers" in detail["message"]

    # 未透传 merge PUT
    merge_calls = [c for c in recorded if c["method"] == "PUT" and "/pulls/5/merge" in c["url"]]
    assert merge_calls == []
    # 阻断留痕①：台账 pr_merge_blocked
    assert any(args[2] == "pr_merge_blocked" for args, _ in ledger)
    # 阻断留痕②：PR 评论 best-effort
    trace = [c for c in recorded if c["method"] == "POST" and "/pulls/5/comments" in c["url"]]
    assert trace and "merge blocked" in trace[0]["body"]["body"]


def test_merge_clean_passthrough(monkeypatch, _client):
    pages = [[{"filename": "src/c.py", "patch": {"diff": "@@ -1 +1 @@\n+clean\n"}}]]
    recorded = _install(monkeypatch, pages)
    r = _client.put(f"{base}/pulls/5/merge")
    assert r.status_code == 200
    assert r.json()["merged"] is True
    merge_calls = [c for c in recorded if c["method"] == "PUT" and "/pulls/5/merge" in c["url"]]
    assert len(merge_calls) == 1


def test_merge_precheck_upstream_error_fail_open(monkeypatch, _client):
    def _fake_req(method, url, token="", body=None):
        if "/pulls/5/files" in url:
            raise UpstreamAPIError(method, url, 405, '{"message":"Method Not Allowed"}')
        return {"merged": True, "sha": "s1"}

    monkeypatch.setattr("src.user.repo_manager._req", _fake_req)
    # 预检 405 → fail-open，merge 透传成功（上游透传语义不变）
    r = _client.put(f"{base}/pulls/5/merge")
    assert r.status_code == 200
    assert r.json()["merged"] is True


def test_merge_blocked_multi_page(monkeypatch, _client):
    diff = "@@ \n+>>>>>>> other\n"
    page1 = [{"filename": f"f{i}.py", "patch": {"diff": "@@ \n+ok\n"}} for i in range(100)]
    page1[7]["patch"]["diff"] = diff
    page2 = [{"filename": "g.py", "patch": {"diff": "@@ \n+<<<<<<< HEAD\n"}}]
    recorded = _install(monkeypatch, [page1, page2])
    r = _client.put(f"{base}/pulls/5/merge")
    assert r.status_code == 409
    assert "f7.py" in r.json()["detail"]["message"]
    assert "g.py" in r.json()["detail"]["message"]
    merge_calls = [c for c in recorded if c["method"] == "PUT" and "/pulls/5/merge" in c["url"]]
    assert merge_calls == []
