"""Issue #2035：CI check-runs / commit status 只读读取通道.

公开接口 seams（一 seam 一测试，不测内部）：
- S1 契约：gitrepo_checks_list / gitrepo_statuses_list 进 OpenAPI（session_only）——
  `grape refresh` 后 CLI 契约驱动生成 `grape git-repo checks list` / `statuses list`
- S2 GitCode check-runs 适配：Actions v8 runs+jobs 映射统一
  check_runs[]{name,status,conclusion,details_url}；conclusion=failure 与
  status=in_progress 可区分（QA 守卫复核归因的基础）
- S2' 降级语义显式三态：404 ref_not_found（ref 不存在）/ 上游 403 透传（无权限）/
  200 total_count=0（有通道无 checks）——不静默把错误吞成空列表
- S3 GitHub 透传：原生 check-runs / combined status 响应与 URL 原样
- S4 GitCode statuses：combined status 透传；status 通道 404 与 ref 404 可区分
"""
import os

import pytest

os.environ.setdefault("DEVOPS_DB_PATH", os.path.join(
    os.path.dirname(__file__), "..", "data", "test_sessions.db"))
os.environ.setdefault("DEVOPS_DB_BACKEND", "sqlite")

from fastapi.testclient import TestClient  # noqa: E402
from src.api.main import app  # noqa: E402
from src.api.deps import get_current_user_or_api_key  # noqa: E402
from src.domains.mgmt.user.repo_manager import UpstreamAPIError  # arch11  # noqa: E402

UID = 98735
OWNER = "o"
REPO = "hello"
BOT = "bot_tok_123"
SHA = "a" * 40
OTHER_SHA = "b" * 40
ACTIONS_BASE = "https://api.gitcode.com/api/v8"


@pytest.fixture(autouse=True)
def _client(monkeypatch):
    app.dependency_overrides[get_current_user_or_api_key] = \
        lambda: {"id": UID, "username": "git_tester", "auth_type": "session_token",
                 "agent_type": "vod", "user_id": 0, "session_key": "s1",
                 "repo_key": f"gitcode:{OWNER}/{REPO}"}
    monkeypatch.setattr("src.domains.mgmt.user.db.get_gitcode_token", lambda *a, **k: BOT)
    monkeypatch.setattr("src.domains.mgmt.user.db.get_github_token", lambda *a, **k: BOT)
    yield TestClient(app)
    app.dependency_overrides.clear()


def _override_user(repo_key: str):
    app.dependency_overrides[get_current_user_or_api_key] = lambda: {
        "id": UID, "username": "git_tester", "auth_type": "session_token",
        "agent_type": "vod", "user_id": 0, "session_key": "s1",
        "repo_key": repo_key}


# ===================== S1 契约登记 =====================

def test_checks_list_contract_registered():
    """S1：gitrepo_checks_list 进契约（session_only）→ CLI refresh 后获得 `git-repo checks list`。"""
    from src.api.openapi import custom_openapi
    schema = custom_openapi(app)
    _cp = "/api/git-repo/{owner}/{repo}/commits/{ref}/check-runs"
    assert _cp in schema["paths"], "check-runs 端点必须进契约（CLI 契约驱动生成的前提）"
    op = schema["paths"][_cp]["get"]
    assert op["operationId"] == "gitrepo_checks_list"
    assert op.get("x-token-scope") == "session_only"
    params = {p["name"] for p in op["parameters"]}
    assert {"owner", "repo", "ref"} <= params


def test_statuses_list_contract_registered():
    """S1：gitrepo_statuses_list 进契约（session_only）→ CLI refresh 后获得 `git-repo statuses list`。"""
    from src.api.openapi import custom_openapi
    schema = custom_openapi(app)
    _cp = "/api/git-repo/{owner}/{repo}/commits/{ref}/status"
    assert _cp in schema["paths"], "status 端点必须进契约"
    op = schema["paths"][_cp]["get"]
    assert op["operationId"] == "gitrepo_statuses_list"
    assert op.get("x-token-scope") == "session_only"
    params = {p["name"] for p in op["parameters"]}
    assert {"owner", "repo", "ref"} <= params


# ===================== S2 GitCode check-runs 适配（Actions v8）=====================

def _run(rid, workflow, status, head_sha=SHA):
    return {"workflow_run_id": str(rid), "workflow_id": f"wf-{rid}",
            "workflow_name": workflow, "title": f"run {rid}", "status": status,
            "run_number": rid, "head_branch": "feat/x", "head_sha": head_sha,
            "actor": {"login": "ci-bot"}, "start_time": 1, "end_time": 2, "pause_time": 0}


def _gitcode_actions_mock(monkeypatch, runs_pages, jobs_by_run):
    """Mock gitcode 上游：/commits?sha=（ref 解析）+ actions runs 分页 + per-run jobs。

    runs_pages: dict page_no -> {total_count, workflow_runs}
    jobs_by_run: dict run_id -> {total_count, jobs}
    """
    calls = []

    def _fake_req(method, url, token="", body=None):
        calls.append({"method": method, "url": url})
        if "/actions/runs" in url and url.rstrip("/").endswith("/jobs"):
            rid = url.rstrip("/").split("/")[-2]
            return jobs_by_run.get(int(rid), {"total_count": 0, "jobs": []})
        if "/actions/runs" in url:
            page = 1
            if "page=" in url:
                page = int([p for p in url.split("?")[1].split("&")
                            if p.startswith("page=")][0].split("=")[1])
            return runs_pages.get(page, {"total_count": 0, "workflow_runs": []})
        if "/commits" in url and "sha=" in url:
            return [{"sha": SHA, "commit": {"tree": {"sha": "t" * 40}}}]
        raise AssertionError(f"unexpected upstream call: {method} {url}")

    monkeypatch.setattr("src.domains.mgmt.user.repo_manager._req", _fake_req)
    return calls


def test_gitcode_checks_by_sha_maps_runs_and_jobs(monkeypatch, _client):
    """S2：按 head sha 取 check-runs——run+job 映射统一字段（name/status/conclusion/details_url），
    conclusion=failure（secret-scan 失败）与 in_progress（检查中）可区分；不匹配 head_sha 的 run 不计入。"""
    runs = {1: {"total_count": 2, "workflow_runs": [_run(1, "CI", "FAILED"),
                                                    _run(2, "other", "RUNNING", head_sha=OTHER_SHA)]}}
    jobs = {1: {"total_count": 2, "jobs": [
        {"id": "j1", "name": "secret-scan", "status": "FAILED", "message": "leak detected"},
        {"id": "j2", "name": "build", "status": "COMPLETED", "message": "",
         "html_url": f"https://gitcode.com/{OWNER}/{REPO}/jobs/j2"},
    ]}}
    _gitcode_actions_mock(monkeypatch, runs, jobs)

    r = _client.get(f"/api/git-repo/{OWNER}/{REPO}/commits/{SHA}/check-runs")
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["total_count"] == 2
    by_name = {c["name"]: c for c in data["check_runs"]}
    # failure 态：conclusion=failure（QA 可归因到 check 名）
    assert by_name["secret-scan"]["status"] == "completed"
    assert by_name["secret-scan"]["conclusion"] == "failure"
    assert by_name["secret-scan"]["message"] == "leak detected"
    # details_url 双向判别：上游提供 html_url → 原样透传；未提供 → 空串（不伪造 URL）
    assert by_name["build"]["details_url"] == f"https://gitcode.com/{OWNER}/{REPO}/jobs/j2"
    assert by_name["secret-scan"]["details_url"] == ""
    # success 态
    assert by_name["build"]["conclusion"] == "success"
    # head sha 语义：不匹配的 run（OTHER_SHA）不产出 checks
    assert all(c.get("run_id") == "1" for c in data["check_runs"])


def test_gitcode_checks_in_progress_distinguishable(monkeypatch, _client):
    """S2：status=in_progress（检查进行中）与 conclusion=failure 输出可区分（验收标准第一项）。"""
    runs = {1: {"total_count": 1, "workflow_runs": [_run(1, "CI", "RUNNING")]}}
    jobs = {1: {"total_count": 1, "jobs": [
        {"id": "j1", "name": "secret-scan", "status": "RUNNING", "message": ""}]}}
    _gitcode_actions_mock(monkeypatch, runs, jobs)

    r = _client.get(f"/api/git-repo/{OWNER}/{REPO}/commits/{SHA}/check-runs")
    assert r.status_code == 200
    c = r.json()["check_runs"][0]
    assert c["status"] == "in_progress"
    assert c["conclusion"] is None


def test_gitcode_checks_empty_status_unknown_not_fabricated(monkeypatch, _client):
    """S2（QA 复审②）：上游状态缺失/空 → status=unknown（不伪造 in_progress，QA 不可
    把「未知」误读为「进行中」）；未知非空值原样小写、conclusion=None（docstring 口径）。"""
    runs = {1: {"total_count": 1, "workflow_runs": [_run(1, "CI", "")]}}
    jobs = {1: {"total_count": 2, "jobs": [
        {"id": "j1", "name": "weird-job", "status": "STRANGE_STATE", "message": ""},
        {"id": "j2", "name": "no-status-job", "message": ""},
    ]}}
    _gitcode_actions_mock(monkeypatch, runs, jobs)

    r = _client.get(f"/api/git-repo/{OWNER}/{REPO}/commits/{SHA}/check-runs")
    assert r.status_code == 200, r.text
    by_name = {c["name"]: c for c in r.json()["check_runs"]}
    # 未知非空值：原样小写（docstring「未知值原样小写」）
    assert by_name["weird-job"]["status"] == "strange_state"
    assert by_name["weird-job"]["conclusion"] is None
    # 空/缺失状态：显式 unknown（而非兜底 in_progress）
    assert by_name["no-status-job"]["status"] == "unknown"
    assert by_name["no-status-job"]["conclusion"] is None


def test_gitcode_checks_by_branch_resolves_head_sha(monkeypatch, _client):
    """S2：分支名 ref 先解析为 head commit sha 再按 head_sha 过滤（支持非 sha ref 入参）。"""
    runs = {1: {"total_count": 1, "workflow_runs": [_run(1, "CI", "COMPLETED")]}}
    jobs = {1: {"total_count": 1, "jobs": [{"id": "j1", "name": "build", "status": "COMPLETED"}]}}
    calls = _gitcode_actions_mock(monkeypatch, runs, jobs)

    r = _client.get(f"/api/git-repo/{OWNER}/{REPO}/commits/feat/issue-1/check-runs")
    assert r.status_code == 200, r.text
    assert any("sha=feat/issue-1" in c["url"] or "sha=feat%2Fissue-1" in c["url"]
               for c in calls), "分支名必须先经 commits 解析"
    assert r.json()["check_runs"][0]["name"] == "build"


def test_gitcode_checks_ref_not_found_404(monkeypatch, _client):
    """S2'：ref 不存在 → 显式 404 ref_not_found（不静默返回空列表）。"""

    def _fake_req(method, url, token="", body=None):
        if "/commits" in url and "sha=" in url:
            return []  # ref 不存在（200 空列表形态）
        raise AssertionError(f"unexpected upstream call: {method} {url}")

    monkeypatch.setattr("src.domains.mgmt.user.repo_manager._req", _fake_req)
    r = _client.get(f"/api/git-repo/{OWNER}/{REPO}/commits/ghost-branch/check-runs")
    assert r.status_code == 404
    d = r.json()["detail"]
    assert d["code"] == "ref_not_found"
    assert d["ref"] == "ghost-branch"


def test_gitcode_checks_fake_full_sha_404_ref_not_found(monkeypatch, _client):
    """S2'（QA 复审③）：伪 40-hex sha（格式完整但不存在）→ 404 ref_not_found
    （存在性探测对全部 ref 生效，对齐 GitHub 原生 404；不静默 200 total_count=0
    与「提交存在但无 checks」混淆），且不触达 runs 通道。"""
    fake_sha = "c" * 40

    def _fake_req(method, url, token="", body=None):
        if "/commits" in url and "sha=" in url:
            return []  # 伪 sha：存在性探测空列表
        if "/actions/runs" in url:
            raise AssertionError("ref 不存在时不应触达 runs 通道")
        raise AssertionError(f"unexpected upstream call: {method} {url}")

    monkeypatch.setattr("src.domains.mgmt.user.repo_manager._req", _fake_req)
    r = _client.get(f"/api/git-repo/{OWNER}/{REPO}/commits/{fake_sha}/check-runs")
    assert r.status_code == 404
    d = r.json()["detail"]
    assert d["code"] == "ref_not_found"
    assert d["ref"] == fake_sha


def test_gitcode_checks_none_200_empty(monkeypatch, _client):
    """S2'：有通道但该提交无 CI runs → 200 total_count=0（合法观察，非错误吞没）。"""
    runs = {1: {"total_count": 0, "workflow_runs": []}}
    _gitcode_actions_mock(monkeypatch, runs, {})

    r = _client.get(f"/api/git-repo/{OWNER}/{REPO}/commits/{SHA}/check-runs")
    assert r.status_code == 200
    data = r.json()
    assert data["total_count"] == 0
    assert data["check_runs"] == []


def test_gitcode_checks_channel_404_distinguishable(monkeypatch, _client):
    """S2'：Actions 通道 404（ref 已解析存在）→ 404 checks_unavailable，与 ref_not_found 可区分。"""

    def _fake_req(method, url, token="", body=None):
        if "/actions/runs" in url:
            raise UpstreamAPIError("GET", url, 404, '{"message":"Not Found"}')
        if "/commits" in url and "sha=" in url:
            return [{"sha": SHA}]
        raise AssertionError(f"unexpected upstream call: {method} {url}")

    monkeypatch.setattr("src.domains.mgmt.user.repo_manager._req", _fake_req)
    r = _client.get(f"/api/git-repo/{OWNER}/{REPO}/commits/{SHA}/check-runs")
    assert r.status_code == 404
    d = r.json()["detail"]
    assert d["code"] == "checks_unavailable"
    assert d["ref"] == SHA


def test_gitcode_checks_upstream_403_passthrough(monkeypatch, _client):
    """S2'：上游 403（无权限）原样透传——与 404（无 checks）显式区分，不静默返回空。"""

    def _fake_req(method, url, token="", body=None):
        if "/actions/runs" in url:
            raise UpstreamAPIError("GET", url, 403, '{"message":"Forbidden"}')
        if "/commits" in url and "sha=" in url:
            return [{"sha": SHA}]
        raise AssertionError(f"unexpected upstream call: {method} {url}")

    monkeypatch.setattr("src.domains.mgmt.user.repo_manager._req", _fake_req)
    r = _client.get(f"/api/git-repo/{OWNER}/{REPO}/commits/{SHA}/check-runs")
    assert r.status_code == 403
    assert r.json()["detail"]["code"] == 403


def test_gitcode_checks_scan_pagination(monkeypatch, _client):
    """S2：runs 多页扫描（首页满页无匹配 → 续页命中）+ 短页终止。"""
    page1 = {"total_count": 120, "workflow_runs": [_run(i, "wf", "RUNNING", head_sha=OTHER_SHA)
                                                   for i in range(1, 101)]}
    page2 = {"total_count": 120, "workflow_runs": [_run(101, "CI", "COMPLETED")]}
    runs = {1: page1, 2: page2}
    jobs = {101: {"total_count": 1, "jobs": [{"id": "j1", "name": "build", "status": "COMPLETED"}]}}
    calls = _gitcode_actions_mock(monkeypatch, runs, jobs)

    r = _client.get(f"/api/git-repo/{OWNER}/{REPO}/commits/{SHA}/check-runs")
    assert r.status_code == 200
    assert [c["name"] for c in r.json()["check_runs"]] == ["build"]
    run_pages = [c["url"] for c in calls if "/actions/runs" in c["url"] and "page=" in c["url"]]
    assert len(run_pages) == 2, "满页必须续扫，短页（<100）终止"


# ===================== S3 GitHub 透传 =====================

def test_github_checks_passthrough(monkeypatch, _client):
    """S3：GitHub 通道透传原生 /commits/{ref}/check-runs，响应原样（字段语义与 GitHub 一致）。"""
    _override_user(f"github:{OWNER}/{REPO}")
    recorded = []

    def _fake_gh_req(method, url, token="", body=None):
        recorded.append({"method": method, "url": url})
        return {"total_count": 1, "check_runs": [
            {"id": 1, "name": "secret-scan", "status": "completed",
             "conclusion": "failure", "details_url": "https://github.com/o/hello/runs/1"}]}

    monkeypatch.setattr("src.domains.mgmt.user.repo_manager._github_req", _fake_gh_req)

    r = _client.get(f"/api/git-repo/{OWNER}/{REPO}/commits/{SHA}/check-runs")
    assert r.status_code == 200
    assert recorded[0]["url"].endswith(f"/repos/{OWNER}/{REPO}/commits/{SHA}/check-runs")
    assert r.json()["check_runs"][0]["name"] == "secret-scan"
    assert r.json()["check_runs"][0]["details_url"].endswith("/runs/1")


def test_github_checks_upstream_404_passthrough(monkeypatch, _client):
    """S3：GitHub 上游 404（commit 不存在）真实状态码透传（#549 口径）。"""
    _override_user(f"github:{OWNER}/{REPO}")

    def _fake_gh_req(method, url, token="", body=None):
        raise UpstreamAPIError("GET", url, 404, '{"message":"No commit found for the ref"}')

    monkeypatch.setattr("src.domains.mgmt.user.repo_manager._github_req", _fake_gh_req)
    r = _client.get(f"/api/git-repo/{OWNER}/{REPO}/commits/deadbeef/check-runs")
    assert r.status_code == 404


# ===================== S4 GitCode statuses / GitHub statuses =====================

def test_github_statuses_passthrough(monkeypatch, _client):
    """S4：GitHub combined status 原生透传（URL 与响应）。"""
    _override_user(f"github:{OWNER}/{REPO}")
    recorded = []

    def _fake_gh_req(method, url, token="", body=None):
        recorded.append({"method": method, "url": url})
        return {"state": "failure", "total_count": 1, "sha": SHA,
                "commit_url": f"https://github.com/{OWNER}/{REPO}/commits/{SHA}",
                "statuses": [{"state": "failure", "context": "security/scan",
                              "target_url": "https://ci.example.com/1",
                              "description": "found secret"}]}

    monkeypatch.setattr("src.domains.mgmt.user.repo_manager._github_req", _fake_gh_req)
    r = _client.get(f"/api/git-repo/{OWNER}/{REPO}/commits/{SHA}/status")
    assert r.status_code == 200
    assert recorded[0]["url"].endswith(f"/repos/{OWNER}/{REPO}/commits/{SHA}/status")
    data = r.json()
    assert data["statuses"][0]["context"] == "security/scan"
    assert data["statuses"][0]["target_url"].endswith("/1")


def test_gitcode_statuses_passthrough(monkeypatch, _client):
    """S4：GitCode 通道 combined status 透传（存在 status 记录时出参原样，字段与 GitHub 对齐）。"""

    def _fake_req(method, url, token="", body=None):
        if "/commits" in url and "sha=" in url:
            return [{"sha": SHA}]
        if url.endswith(f"/commits/{SHA}/status"):
            return {"state": "success", "total_count": 1, "sha": SHA, "statuses": [
                {"state": "success", "context": "ci/lint", "target_url": "https://x/1",
                 "description": "ok"}]}
        raise AssertionError(f"unexpected upstream call: {method} {url}")

    monkeypatch.setattr("src.domains.mgmt.user.repo_manager._req", _fake_req)
    r = _client.get(f"/api/git-repo/{OWNER}/{REPO}/commits/{SHA}/status")
    assert r.status_code == 200, r.text
    assert r.json()["statuses"][0]["context"] == "ci/lint"


def test_gitcode_statuses_channel_404_distinguishable(monkeypatch, _client):
    """S4：status 通道 404（ref 已解析存在）→ 404 statuses_unavailable，与 ref_not_found 可区分。"""

    def _fake_req(method, url, token="", body=None):
        if "/commits" in url and "sha=" in url:
            return [{"sha": SHA}]
        if "/status" in url:
            raise UpstreamAPIError("GET", url, 404, '{"message":"Not Found"}')
        raise AssertionError(f"unexpected upstream call: {method} {url}")

    monkeypatch.setattr("src.domains.mgmt.user.repo_manager._req", _fake_req)
    r = _client.get(f"/api/git-repo/{OWNER}/{REPO}/commits/{SHA}/status")
    assert r.status_code == 404
    d = r.json()["detail"]
    assert d["code"] == "statuses_unavailable"
    assert d["ref"] == SHA


def test_gitcode_statuses_ref_not_found_404(monkeypatch, _client):
    """S4：ref 不存在 → 404 ref_not_found（不触发 status 通道调用）。"""

    def _fake_req(method, url, token="", body=None):
        if "/commits" in url and "sha=" in url:
            return []
        raise AssertionError(f"unexpected upstream call: {method} {url}")

    monkeypatch.setattr("src.domains.mgmt.user.repo_manager._req", _fake_req)
    r = _client.get(f"/api/git-repo/{OWNER}/{REPO}/commits/ghost/status")
    assert r.status_code == 404
    assert r.json()["detail"]["code"] == "ref_not_found"


def test_gitcode_statuses_fake_full_sha_404_ref_not_found(monkeypatch, _client):
    """S4（QA 复审③）：伪 40-hex sha 在 status 通道 → 404 ref_not_found（而非
    statuses_unavailable——后者语义为「ref 已解析存在但通道不可用/无记录」），
    与 checks 通道及 GitHub 原生 404 口径对齐，不触发 status 通道调用。"""
    fake_sha = "c" * 40

    def _fake_req(method, url, token="", body=None):
        if "/commits" in url and "sha=" in url:
            return []  # 伪 sha：存在性探测空列表
        raise AssertionError(f"unexpected upstream call: {method} {url}")

    monkeypatch.setattr("src.domains.mgmt.user.repo_manager._req", _fake_req)
    r = _client.get(f"/api/git-repo/{OWNER}/{REPO}/commits/{fake_sha}/status")
    assert r.status_code == 404
    d = r.json()["detail"]
    assert d["code"] == "ref_not_found"
    assert d["ref"] == fake_sha


def test_statuses_upstream_403_passthrough(monkeypatch, _client):
    """S4：上游 403（无权限）原样透传，与 404 语义可区分。"""

    def _fake_req(method, url, token="", body=None):
        if "/commits" in url and "sha=" in url:
            return [{"sha": SHA}]
        raise UpstreamAPIError("GET", url, 403, '{"message":"Forbidden"}')

    monkeypatch.setattr("src.domains.mgmt.user.repo_manager._req", _fake_req)
    r = _client.get(f"/api/git-repo/{OWNER}/{REPO}/commits/{SHA}/status")
    assert r.status_code == 403
