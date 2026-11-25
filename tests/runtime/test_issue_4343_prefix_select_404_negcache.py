"""Issue #4343：单仓 issue 解析前缀直选 + ref 级 404 负缓存 + 调用方快速失败。

生产现象（Issue 正文）：单仓 issue 解析跨 4 provider 扇出且 404 无负缓存——
一次 pipeline 查询 5 条 WARNING，调用方 20s 重试 99 次/小时 ≈ 495 WARNING/小时。

覆盖 5 类回归场景（Spec）：
1. 前缀直选：owner/repo 带 provider 前缀 → 解析链只探测该 provider（不扇出），
   无跨平台 token-missing 日志；
2. 无前缀候选链：候选失败日志聚合为 1 条；
3. 负缓存：TTL 内同 (repo_key, ident) 连续 404 上游调用 = 1，过期（清空）后重探；
4. 错误分流：401/403/5xx 不入负缓存、error_code 区分
   ref_not_found / auth_failed / upstream_error / network_error；
5. 调用方快速失败：404 detail 响应带 Retry-After 头；负缓存命中路径零上游调用。

seam 与先例 test_issue_3891_pipeline_shared_resolution.py 同层：HTTP 路由层
（TestClient）+ 在既有 VCS 外呼 seam（_request / _vcs_req / _PROVIDER_MODULES /
fetch_issue_via_vcs）打 patch，不外呼真实平台。
"""
import os
import sys
import uuid
from unittest.mock import patch

ROOT = os.path.join(os.path.dirname(__file__), "..", "..")
os.environ.setdefault("DEVOPS_DB_PATH", os.path.join(ROOT, "data", "test_sessions.db"))

from fastapi.testclient import TestClient

from src.api.main import app
from src.domains.runtime.routes import issues as issues_mod
from src.domains.runtime.vcs import api_client as api_client_mod
from src.kernel.session_manager import db, db_lock

client = TestClient(app)

OWNER = "i4343org"
REPO = "i4343repo"
REPO_KEY = f"gitcode:{OWNER}/{REPO}"

_uid = None


def _session_headers(repo_key=REPO_KEY, agent_type="coding"):
    from src.api import auth_token
    tok = auth_token.issue_session_token(
        session_key=f"i4343-{uuid.uuid4().hex}", repo_key=repo_key, agent_type=agent_type,
        user_id=(_uid["id"] if _uid else 0))
    return {"Authorization": f"Bearer {tok}"}


def setup_module():
    global _uid
    from src.api.db_setup import init_database
    init_database()
    with db_lock:
        c = db.cursor()
        c.execute("DELETE FROM grape_accounts WHERE username='i4343_user'")
        db.commit()
    from src.domains.mgmt.auth import grape_account_db
    _uid = grape_account_db.create_grape_account(
        "i4343_user", email=None, password=uuid.uuid4().hex)
    if not _uid:
        _uid = grape_account_db.get_grape_account_by_username("i4343_user")
    _cleanup()


def teardown_module():
    _cleanup()
    if _uid:
        from src.domains.mgmt.auth import grape_account_db
        grape_account_db.soft_delete_account(_uid["id"])
    with db_lock:
        db.cursor().execute("DELETE FROM grape_accounts WHERE username='i4343_user'")
        db.commit()


def _cleanup():
    with db_lock:
        c = db.cursor()
        for table in ("events", "issue", "issue_state", "agent_tasks"):
            try:
                c.execute(f"DELETE FROM {table} WHERE repo_key LIKE ?", (f"%{OWNER}/{REPO}%",))
            except Exception:
                pass
        db.commit()
    api_client_mod.clear_ref_404_neg_cache()


def _pipeline_get(number, owner=OWNER):
    return client.get(f"/api/issues/{owner}/{REPO}/{number}/pipeline",
                      headers=_session_headers())


class _ReqStub:
    """VcsApiClient._request 可控 stub：记录调用次数、按序返回响应。"""

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = 0

    def __call__(self, method, path, data=None, form=False, timeout=30):
        self.calls += 1
        idx = min(self.calls, len(self.responses)) - 1
        r = self.responses[idx]
        if isinstance(r, Exception):
            raise r
        return r


# ── 场景 3/4：负缓存 TTL 内上游调用 = 1；401/403/5xx 不缓存 ─────────────────────

def test_negcache_ttl_single_upstream_call():
    """同 (repo_key, ident) 连续 404：TTL 内上游调用 = 1，命中不再打上游。"""
    _cleanup()
    err_404 = {"error": "HTTP 404", "status_code": 404, "detail": "Issue Not Found",
               "error_code": "not_found"}
    stub = _ReqStub([err_404])

    with patch.object(api_client_mod.VcsApiClient, "_request", stub):
        c1 = api_client_mod.VcsApiClient("gitcode", "tok", OWNER, REPO)
        r1 = c1.get_issue(4343)
        assert r1.get("status_code") == 404 and r1.get("error_code") == "ref_not_found"
        c2 = api_client_mod.VcsApiClient("gitcode", "tok", OWNER, REPO)
        r2 = c2.get_issue(4343)
        assert r2.get("neg_cached") is True and r2.get("error_code") == "ref_not_found"
        assert stub.calls == 1, f"TTL 内上游调用应 = 1，实际 {stub.calls}"

    # 缓存清空（等价 TTL 过期）→ 重新探测上游
    api_client_mod.clear_ref_404_neg_cache()
    with patch.object(api_client_mod.VcsApiClient, "_request", stub):
        c3 = api_client_mod.VcsApiClient("gitcode", "tok", OWNER, REPO)
        c3.get_issue(4343)
        assert stub.calls == 2, "负缓存过期（清空）后应重新探测上游"


def test_negcache_auth_errors_not_cached():
    """401/403/5xx 语义不回退：不入负缓存、每次仍打上游、error_code 可判别。"""
    _cleanup()
    for code, expected_ec in ((401, "auth_failed"), (403, "auth_failed"),
                             (500, "upstream_error"), (502, "upstream_error")):
        api_client_mod.clear_ref_404_neg_cache()
        stub = _ReqStub([{"error": f"HTTP {code}", "status_code": code,
                          "detail": "x", "error_code": expected_ec}])
        with patch.object(api_client_mod.VcsApiClient, "_request", stub):
            c = api_client_mod.VcsApiClient("gitcode", "tok", OWNER, REPO)
            r = c.get_issue(99)
            assert r.get("error_code") == expected_ec, f"{code} → {expected_ec}"
            assert "neg_cached" not in r
            r2 = c.get_issue(99)
            assert "neg_cached" not in r2
            assert stub.calls == 2, f"{code} 不得被负缓存（上游调用数 {stub.calls}）"


def test_status_to_error_code_classification():
    """错误分类助手：404/401/403/5xx/4xx/异常各归其类（供 error_code 判别）。"""
    f = api_client_mod._status_to_error_code
    assert f(404) == "not_found"
    assert f(401) == "auth_failed" and f(403) == "auth_failed"
    assert f(500) == "upstream_error" and f(502) == "upstream_error"
    assert f(422) == "http_error"
    assert f(301) == "http_error"


def test_negcache_ttl_env_clamp():
    """TTL env 可调，夹取 [60, 300]。"""
    _cleanup()
    with patch.dict(os.environ, {"GRAPE_VCS_REF_404_TTL_SECONDS": "9999"}):
        assert api_client_mod.ref_404_neg_cache_ttl() == 300
    with patch.dict(os.environ, {"GRAPE_VCS_REF_404_TTL_SECONDS": "1"}):
        assert api_client_mod.ref_404_neg_cache_ttl() == 60
    with patch.dict(os.environ, {"GRAPE_VCS_REF_404_TTL_SECONDS": "120"}):
        assert api_client_mod.ref_404_neg_cache_ttl() == 120


# ── 场景 1/2/5：路由层（前缀直选 / 候选聚合 / Retry-After）────────────────────

def test_prefixed_owner_no_fanout(caplog):
    """owner 带 gitcode: 前缀 → 存在性核实链只处理带前缀单键，不跨平台扇出。"""
    _cleanup()
    fetch_calls = []

    def fake_fetch_via_vcs(repo_key, num):
        fetch_calls.append(repo_key)
        return ("missing", {})

    import logging
    with caplog.at_level(logging.WARNING, logger="devops-bot"):
        with patch.object(issues_mod, "_vcs_issue_lookup", return_value=None), \
             patch("src.domains.runtime.pipeline.event_pipeline.fetch_issue_via_vcs",
                   side_effect=fake_fetch_via_vcs):
            resp = _pipeline_get(999, owner=f"gitcode:{OWNER}")
    assert resp.status_code == 404
    assert resp.json()["detail"]["error_code"] == "issue_not_found"
    # 前缀直选：核实链只走 gitcode: 前缀单键，不得扇出其它平台
    assert fetch_calls, "存在性核实链应被触发"
    for rk in fetch_calls:
        assert rk.startswith("gitcode:"), f"不得扇出到其它平台: {rk}"
    joined = "\n".join(r.getMessage() for r in caplog.records)
    assert "global bot token missing for provider=github" not in joined
    assert "global bot token missing for provider=gitee" not in joined
    assert "global bot token missing for provider=gitlab" not in joined


def test_unprefixed_candidate_failures_aggregate_one_warning(caplog):
    """无前缀候选链：候选全部 404 未命中，聚合告警 ≤1 条（不再逐 provider WARNING）。"""
    _cleanup()
    import logging
    from fastapi import HTTPException
    import src.api.git_repo_api as gra_mod

    class _FakeMod:
        @staticmethod
        def bot_token(rk):
            return "tok"

    fake_modules = {p: _FakeMod for p in ("gitcode", "github", "gitee", "gitlab")}

    def fake_vcs_req(provider, method, url, token, body=None):
        raise HTTPException(status_code=404, detail={"code": 404, "message": "Not Found"})

    with caplog.at_level(logging.WARNING, logger="devops-bot"):
        with patch.object(gra_mod, "_PROVIDER_MODULES", fake_modules), \
             patch.object(gra_mod, "_vcs_req", side_effect=fake_vcs_req), \
             patch.object(gra_mod, "_vcs_url",
                          lambda owner, repo, path, params, p: f"https://x/{owner}/{repo}{path}"), \
             patch("src.domains.runtime.pipeline.event_pipeline.fetch_issue_via_vcs",
                   return_value=("missing", {})):
            resp = _pipeline_get(998, owner=OWNER)
    assert resp.status_code == 404
    lookup_warns = [r for r in caplog.records
                    if "VCS fallback" in r.getMessage() or "candidates" in r.getMessage()]
    assert len(lookup_warns) <= 1, \
        f"候选失败日志应聚合为 1 条，实际 {len(lookup_warns)}: {[r.getMessage() for r in lookup_warns]}"


def test_404_detail_retry_after_header():
    """404 快速失败：detail 404 响应带 Retry-After 头（调用方退避提示）。"""
    _cleanup()
    with patch.object(issues_mod, "_vcs_issue_lookup", return_value=None), \
         patch("src.domains.runtime.pipeline.event_pipeline.fetch_issue_via_vcs",
               return_value=("missing", {})):
        resp = _pipeline_get(997, owner=f"gitcode:{OWNER}")
    assert resp.status_code == 404
    ra = resp.headers.get("Retry-After")
    assert ra and int(ra) >= 60, f"Retry-After 缺失或过小: {ra!r}"


def test_404_negcache_hit_no_upstream_on_second_poll():
    """连续 404 轮询：负缓存命中后第二次请求零上游调用（快速失败，不再 1.3s 空转）。"""
    _cleanup()
    stub = _ReqStub([{"error": "HTTP 404", "status_code": 404,
                      "detail": "Issue Not Found", "error_code": "not_found"}])

    with patch.object(issues_mod, "_vcs_issue_lookup", return_value=None), \
         patch("src.domains.runtime.pipeline.event_pipeline._resolve_issue_read_token",
               return_value="tok"), \
         patch.object(api_client_mod.VcsApiClient, "_request", stub):
        r1 = _pipeline_get(996, owner=f"gitcode:{OWNER}")
        r2 = _pipeline_get(996, owner=f"gitcode:{OWNER}")
    assert r1.status_code == 404 and r2.status_code == 404
    # 第一次请求：核实链打 1 次上游并写负缓存；第二次请求：负缓存命中，零上游调用
    assert stub.calls == 1, f"负缓存 TTL 内上游调用应 = 1，实际 {stub.calls}"


def test_repo_keys_of_prefix_direct_select():
    """_repo_keys_of：带前缀 owner → 单键直选；无前缀 → 保留候选集。"""
    _cleanup()
    prefixed = issues_mod._repo_keys_of(f"gitcode:{OWNER}", REPO)
    assert prefixed == [f"gitcode:{OWNER}/{REPO}"], f"前缀应直选单键: {prefixed}"
    bare = issues_mod._repo_keys_of(OWNER, REPO)
    assert len(bare) >= 2, "无前缀保留候选链"
    assert f"gitcode:{OWNER}/{REPO}" in bare


def test_vcs_issue_lookup_prefixed_single_provider():
    """_vcs_issue_lookup：带前缀 owner 只探测该 provider（零跨平台探测）。"""
    _cleanup()
    probed = []
    import src.api.git_repo_api as gra_mod

    class _FakeMod:
        @staticmethod
        def bot_token(rk):
            return "tok"

    fake_modules = {p: _FakeMod for p in ("gitcode", "github", "gitee", "gitlab")}

    def fake_vcs_req(provider, method, url, token, body=None):
        probed.append(provider)
        from fastapi import HTTPException
        raise HTTPException(status_code=404, detail={"code": 404, "message": "Not Found"})

    with patch.object(gra_mod, "_PROVIDER_MODULES", fake_modules), \
         patch.object(gra_mod, "_vcs_req", side_effect=fake_vcs_req), \
         patch.object(gra_mod, "_vcs_url",
                      lambda owner, repo, path, params, p: f"https://x/{owner}/{repo}{path}"):
        out = issues_mod._vcs_issue_lookup(
            f"gitcode:{OWNER}", REPO, "123", {"auth_type": "session_token"})
    assert out is None
    assert probed == ["gitcode"], f"前缀直选应只探测 gitcode，实际 {probed}"
