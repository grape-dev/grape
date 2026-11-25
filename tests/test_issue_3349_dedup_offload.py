"""Issue #3349 — 建单查重去同步化 + 上游超时/幂等策略测试。

覆盖：
1. 平台库（本地 `issue` 台账）查重：命中直接复用单号、仓库有台账时不再打 VCS；
2. 台账不可判定时降级到**有界** VCS 探针（页数上限 + 显式单页超时，成本与 open issue 数解耦）；
3. `create_task_via_issue`（async）内查重/建单/PATCH 全部卸载到工作线程（不阻塞事件循环）；
4. 上游超时的放弃策略：对账命中 → 按建成处理；对账不到 → 504 + `retryable` + 幂等键；
5. 重试幂等：超时后重试走查重 → 409 复用已建单号，不再二次建单；
6. 静态守卫：async 建单链路内不存在裸同步 VCS 调用。
"""
import asyncio
import inspect
import os
import re
import sys
import threading
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.environ.setdefault("DEVOPS_DB_PATH",
                      os.path.join(os.path.dirname(__file__), "..", "data", "test_sessions.db"))

import pytest
from fastapi import HTTPException

from src.api import issues_center as ics
from src.api.db_setup import init_database
from src.kernel.session_manager import db, db_lock

init_database()

OWNER = "org3349"
REPO = "dedup"
REPO_KEY = f"gitcode:{OWNER}/{REPO}"

USER = {"id": 1, "username": "u3349", "auth_type": "internal", "token": "tok3349"}
# OAuth/JWT 用户（无 preauth bot token）：命中「401 → 刷新用户 GitCode token → 重试」分支
USER_OAUTH = {"id": 1, "username": "u3349", "auth_type": "jwt"}


def _body(title, description="d", scene_id=""):
    return ics.TaskCreateBody(repo_key=f"{OWNER}/{REPO}", title=title,
                              description=description, scene_id=scene_id)


def _clean_repo():
    with db_lock:
        c = db.cursor()
        c.execute("DELETE FROM issue WHERE repo_key=?", (REPO_KEY,))
        db.commit()


def _seed(num, title, status="active", completed_reason="", vcs_exists=1):
    with db_lock:
        c = db.cursor()
        c.execute("DELETE FROM issue WHERE repo_key=? AND issue_number=?", (REPO_KEY, num))
        c.execute(
            "INSERT INTO issue (user_id, assignee, repo_key, issue_number, title, "
            "task_description, status, completed_reason, vcs_exists) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (0, "grape_bot", REPO_KEY, num, title, "d", status, completed_reason, vcs_exists))
        db.commit()


@pytest.fixture(autouse=True)
def _clean():
    _clean_repo()
    yield
    _clean_repo()


def _no_vcs(*a, **k):
    raise AssertionError("VCS 探针不应被调用（台账已可判定）")


# ===== 1. 平台库查重 =====

def test_ledger_hit_returns_number_without_vcs(monkeypatch):
    _seed(41, "同名需求")
    monkeypatch.setattr(ics, "_find_open_issue_by_title_vcs", _no_vcs)
    assert ics._find_open_issue_by_title("tok", OWNER, REPO, "同名需求") == 41


def test_ledger_authoritative_miss_skips_vcs(monkeypatch):
    """仓库已有台账 → 未命中即「确无同名 open Issue」，不再翻页打 VCS。"""
    _seed(42, "另一个需求")
    monkeypatch.setattr(ics, "_find_open_issue_by_title_vcs", _no_vcs)
    assert ics._find_open_issue_by_title("tok", OWNER, REPO, "全新标题") is None


def test_ledger_ignores_closed_issue(monkeypatch):
    """issue_closed 回流的台账行不算 open：同名重开应放行。"""
    _seed(43, "已关闭需求", status="completed", completed_reason="issue_closed")
    monkeypatch.setattr(ics, "_find_open_issue_by_title_vcs", _no_vcs)
    assert ics._find_open_issue_by_title("tok", OWNER, REPO, "已关闭需求") is None


def test_ledger_ignores_vcs_deleted_row(monkeypatch):
    _seed(44, "已删除需求", vcs_exists=0)
    monkeypatch.setattr(ics, "_find_open_issue_by_title_vcs", _no_vcs)
    assert ics._find_open_issue_by_title("tok", OWNER, REPO, "已删除需求") is None


# ===== 2. 降级 VCS 探针（有界 + 显式超时）=====

def _patch_vcs_adapter(monkeypatch, pages):
    """降级探针走 D36/S55 端口-适配器（VcsApiClient），按 gitlab 用例同款手法桩
    `_request`（记录 path/timeout），并屏蔽短 TTL 标题索引缓存保证逐页真实翻页。"""
    calls = []

    def _fake_request(self, method, path, data=None, form=False, timeout=30):
        calls.append((path, timeout))
        return pages[min(len(calls) - 1, len(pages) - 1)]

    monkeypatch.setattr("src.domains.runtime.vcs.api_client.VcsApiClient._request", _fake_request)
    monkeypatch.setattr("src.domains.runtime.vcs.api_client._open_title_get", lambda *a, **k: None)
    monkeypatch.setattr("src.domains.runtime.vcs.api_client._open_title_put", lambda *a, **k: None)
    return calls


def test_empty_ledger_falls_back_to_bounded_vcs_probe(monkeypatch):
    """仓库无台账 → 降级 VCS 探针，但页数有上限、每页带显式超时。"""
    _full_page = [{"number": 900 + i, "title": f"其他-{i}", "state": "open"}
                  for i in range(ics.DEDUP_FALLBACK_PAGE_SIZE)]
    calls = _patch_vcs_adapter(monkeypatch, [_full_page] * (ics.DEDUP_FALLBACK_MAX_PAGES + 3))

    assert ics._find_open_issue_by_title("tok", OWNER, REPO, "没人用过的标题") is None
    assert len(calls) == ics.DEDUP_FALLBACK_MAX_PAGES, "翻页必须受固定上限约束"
    assert all(c[1] == ics.DEDUP_UPSTREAM_TIMEOUT for c in calls), "每页必须带显式超时"
    assert f"per_page={ics.DEDUP_FALLBACK_PAGE_SIZE}" in calls[0][0]
    assert "page=1" in calls[0][0]


def test_vcs_probe_hit_returns_number(monkeypatch):
    _patch_vcs_adapter(monkeypatch, [[{"number": 45, "title": "探针命中标题", "state": "open"}]])
    assert ics._find_open_issue_by_title("tok", OWNER, REPO, "探针命中标题") == 45


def test_ledger_failure_degrades_to_vcs(monkeypatch):
    class _BrokenDB:
        def cursor(self):
            raise RuntimeError("db down")

    monkeypatch.setattr(ics, "db", _BrokenDB())
    monkeypatch.setattr(ics, "_find_open_issue_by_title_vcs", lambda *a, **k: 46)
    assert ics._find_open_issue_by_title("tok", OWNER, REPO, "台账异常标题") == 46


# ===== 3/4/5. create_task_via_issue：线程卸载 + 超时放弃 + 重试幂等 =====

def _patch_create_path(monkeypatch, post):
    monkeypatch.setattr(ics, "_attach_list", lambda *a, **k: [])
    monkeypatch.setattr(ics, "ensure_issue_task", lambda *a, **k: 1)
    # D35/S55 后建单统一入口是 _create_issue_unified（卡驱动），桩它即覆盖建单 POST。
    monkeypatch.setattr(ics, "_create_issue_unified", post)
    monkeypatch.setattr("src.domains.mgmt.public.get_repo_scene", lambda *a, **k: "")
    monkeypatch.setattr("src.kernel.outbound_ledger.record_outbound", lambda *a, **k: None)
    monkeypatch.setattr("src.domains.runtime.pipeline.event_pipeline.run_pipeline",
                        lambda *a, **k: {"seq": 0})


async def test_dedup_runs_off_event_loop(monkeypatch):
    """async 建单路由内查重必须跑在工作线程，事件循环线程不得被同步 IO 占据。"""
    seen = {}

    def _dedup(*a, **k):
        seen["thread"] = threading.current_thread()
        return None

    monkeypatch.setattr(ics, "_find_open_issue_by_title", _dedup)
    _patch_create_path(monkeypatch, lambda *a, **k: {"number": 501})

    result = await ics.create_task_via_issue(_body("线程卸载验证"), USER)

    assert result["issue_number"] == 501
    assert seen["thread"] is not threading.main_thread(), "查重仍在事件循环线程内同步执行"


async def test_create_timeout_returns_504_retryable(monkeypatch):
    def _hang(*a, **k):
        time.sleep(1.0)
        return {"number": 999}

    _patch_create_path(monkeypatch, _hang)
    monkeypatch.setattr(ics, "_find_open_issue_by_title", lambda *a, **k: None)
    monkeypatch.setattr(ics, "CREATE_UPSTREAM_TIMEOUT", 0.2)
    monkeypatch.setattr(ics, "CREATE_HTTP_TIMEOUT", 0.1)

    async def _no_reconcile(*a, **k):
        return None

    monkeypatch.setattr(ics, "_reconcile_created_issue", _no_reconcile)

    with pytest.raises(HTTPException) as ei:
        await ics.create_task_via_issue(_body("超时放弃"), USER)

    exc = ei.value
    assert exc.status_code == 504
    detail = exc.detail
    assert detail["error"] == "upstream_timeout"
    assert detail["retryable"] is True
    assert detail["idempotency_key"] == f"{OWNER}/{REPO}/超时放弃"


async def test_create_timeout_reconciled_counts_as_created(monkeypatch):
    def _boom(*a, **k):
        raise TimeoutError("timed out")

    _patch_create_path(monkeypatch, _boom)
    monkeypatch.setattr(ics, "_find_open_issue_by_title", lambda *a, **k: None)

    async def _reconcile(*a, **k):
        return 77

    monkeypatch.setattr(ics, "_reconcile_created_issue", _reconcile)

    result = await ics.create_task_via_issue(_body("对账命中"), USER)
    assert result["created"] is True
    assert result["issue_number"] == 77


async def test_retry_after_timeout_is_idempotent(monkeypatch):
    """超时 → 504；上游其实已建成（台账已同步）时重试 → 409 复用单号，不再二次建单。"""
    posts = []

    def _boom(*a, **k):
        posts.append(1)
        raise TimeoutError("timed out")

    _patch_create_path(monkeypatch, _boom)

    async def _no_reconcile(*a, **k):
        return None

    with monkeypatch.context() as mp:
        # 第一次：查重未命中（新单）、建单超时、对账也探不到 → 504
        mp.setattr(ics, "_find_open_issue_by_title", lambda *a, **k: None)
        mp.setattr(ics, "_reconcile_created_issue", _no_reconcile)
        with pytest.raises(HTTPException) as ei:
            await ics.create_task_via_issue(_body("重试幂等"), USER)
        assert ei.value.status_code == 504
        assert len(posts) == 1

    # 上游其实已经建成、台账已回流 → 重试命中查重（真实台账查询，不 mock）
    _seed(88, "重试幂等")

    with pytest.raises(HTTPException) as ei2:
        await ics.create_task_via_issue(_body("重试幂等"), USER)
    assert ei2.value.status_code == 409
    assert ei2.value.detail["issue_number"] == 88
    assert ei2.value.detail["idempotency_key"] == f"{OWNER}/{REPO}/重试幂等"
    assert len(posts) == 1, "重试不得产生第二次建单"


async def test_refresh_retry_timeout_keeps_504_semantics(monkeypatch):
    """QA 审查（#3349）：401 刷新后的建单重试若超时，语义须与首次建单一致——
    走有界对账探针；探不到 → 504 + retryable + 幂等键（不得落入 502 通用分支）。"""
    tokens = []

    def _post(*a, **k):
        tokens.append(a[0] if a else "")
        if len(tokens) == 1:
            raise RuntimeError("failed (401): token expired")
        time.sleep(1.0)  # 刷新后的重试挂起 → 请求级放弃
        return {"number": 999}

    _patch_create_path(monkeypatch, _post)
    monkeypatch.setattr("src.api.deps.get_user_token_for_repo", lambda *a, **k: "tok-user")
    monkeypatch.setattr(ics, "_find_open_issue_by_title", lambda *a, **k: None)
    monkeypatch.setattr(ics, "CREATE_UPSTREAM_TIMEOUT", 0.2)
    monkeypatch.setattr(ics, "CREATE_HTTP_TIMEOUT", 0.1)
    monkeypatch.setattr(ics, "_refresh_user_gitcode_token", lambda *a, **k: "tok-refreshed")

    async def _no_reconcile(*a, **k):
        return None

    monkeypatch.setattr(ics, "_reconcile_created_issue", _no_reconcile)

    with pytest.raises(HTTPException) as ei:
        await ics.create_task_via_issue(_body("刷新后重试超时"), USER_OAUTH)

    exc = ei.value
    assert exc.status_code == 504, "刷新重试超时须走 504 upstream_timeout，而非 502"
    assert exc.detail["error"] == "upstream_timeout"
    assert exc.detail["retryable"] is True
    assert exc.detail["idempotency_key"] == f"{OWNER}/{REPO}/刷新后重试超时"
    assert len(tokens) == 2, "401 → 刷新一次 → 重试一次，不得再多"


async def test_refresh_retry_timeout_reconciled_counts_as_created(monkeypatch):
    """QA 审查（#3349）：刷新后的建单重试超时、但对账探到上游已建成 → 按建成返回。"""
    tokens = []

    def _post(*a, **k):
        tokens.append(a[0] if a else "")
        if len(tokens) == 1:
            raise RuntimeError("failed (401): token expired")
        raise TimeoutError("timed out")

    _patch_create_path(monkeypatch, _post)
    monkeypatch.setattr("src.api.deps.get_user_token_for_repo", lambda *a, **k: "tok-user")
    monkeypatch.setattr(ics, "_find_open_issue_by_title", lambda *a, **k: None)
    monkeypatch.setattr(ics, "_refresh_user_gitcode_token", lambda *a, **k: "tok-refreshed")

    async def _reconcile(*a, **k):
        return 99

    monkeypatch.setattr(ics, "_reconcile_created_issue", _reconcile)

    result = await ics.create_task_via_issue(_body("刷新后重试对账命中"), USER_OAUTH)
    assert result["created"] is True
    assert result["issue_number"] == 99


# ===== 6. 静态守卫 =====

def test_no_bare_sync_vcs_calls_in_create_task_via_issue():
    src = inspect.getsource(ics.create_task_via_issue)
    names = ("_post_gitcode_issue", "_post_github_issue", "_find_open_issue_by_title",
             "_patch_issue_body", "_patch_github_issue_body", "_gitcode_assignee_member_check")
    bare = [ln.strip() for ln in src.splitlines()
            if re.match(r"^(resp = )?(%s)\(" % "|".join(names), ln.strip())]
    assert bare == [], f"async 路由内仍有裸同步 VCS 调用: {bare}"
    assert "await asyncio.to_thread(" in src
    assert "_run_upstream(" in src


def test_create_route_delegates_to_async_flow():
    from src.domains.runtime.routes import issues as issues_route
    src = inspect.getsource(issues_route.create_issue)
    # 对外唯一创建入口是薄委托层：不内联任何同步 VCS 调用，
    # 全部经 create_task_via_issue 的 to_thread/_run_upstream 路径。
    assert "await create_task_via_issue(" in src
    assert "urlopen" not in src
    assert "_req(" not in src, "async 路由不得内联同步 VCS 调用"
def test_create_http_timeout_is_wired_into_vcs_client(monkeypatch):
    """架构守护 PR #2131 条件 3：CREATE_HTTP_TIMEOUT 必须被生产路径消费。

    _create_issue_unified 的默认 timeout 即 CREATE_HTTP_TIMEOUT，且透传到
    VcsApiClient.create_issue（真实建单链的 socket 超时），而非仅测试引用。
    """
    captured = {}

    class _FakeClient:
        def __init__(self, provider, token, owner, repo):
            pass

        def create_issue(self, title, body="", assignee="", timeout=None):
            captured["timeout"] = timeout
            return {"number": 999}

    monkeypatch.setattr("src.domains.runtime.vcs.api_client.VcsApiClient", _FakeClient)
    resp = ics._create_issue_unified("tok", "gitcode", "owner", "repo", "t", "b")
    assert resp == {"number": 999}
    assert captured["timeout"] == ics.CREATE_HTTP_TIMEOUT, (
        "建单 POST 的 socket 超时必须取 CREATE_HTTP_TIMEOUT，而非 urllib 默认 30s")
    # 显式覆盖也须透传（异常路径/特殊调用方仍可控）
    ics._create_issue_unified("tok", "gitcode", "owner", "repo", "t", "b", timeout=3)
    assert captured["timeout"] == 3
