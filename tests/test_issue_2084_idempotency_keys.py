"""（#2084 移植：路径适配 arch11 domains 形态）Issue #2084：写操作幂等键（Idempotency-Key 请求头 + grape_t_idempotency_keys 表）。

背景（eval-F10 残留 / chat send 重复通知 id 9/10 实证）：写超时（504 uncertain）后
调用方只能回读核对，缺服务端防重复机制——同一逻辑操作的重试/重复提交会再次执行上游
写，产生重复评论/重复 Issue。本组测试锁定幂等键公开语义：

- 同键重复写只执行一次，第二次回放首次响应（X-Idempotency-Replayed 头显式标记）；
- 同键不同 body → 422 idempotency_key_conflict（键被复用到不同请求）；
- 并发同键 → 409 idempotency_in_flight 且上游仅执行一次（无重复副作用）；
- 写 504 uncertain：记录保持 in_flight（盲重试 409 第二道防线）；超过接管窗口后
  同键同指纹可重执行（「先回读确认未落地，再带原键重试」的落地路径）；
- done 记录 TTL 24h 过期后可重执行；
- 无键 / GET / 非 tk_ 凭据 → 行为零变更（透传，无幂等介入）；无效 token → 401 不被掩盖。

公开 seam：/api/git-repo/* 写端点（TestClient HTTP 请求，含真实 tk_ 会话 token 链路）。
"""
import threading

import pytest
from fastapi.testclient import TestClient

from src.api.main import app
from src.api.deps import get_current_user_or_api_key

OWNER = "o"
REPO = "hello"
BOT = "bot_tok_2084"
COMMENTS_PATH = f"/api/git-repo/{OWNER}/{REPO}/issues/55/comments"


def _issue_token(repo_key: str = f"gitcode:{OWNER}/{REPO}") -> str:
    """签发真实会话级 tk_ token（与生产链路同构：幂等层按 token 派生 repo_key）。"""
    from src.api.auth_token import issue_session_token
    return issue_session_token(session_key="sess-2084-test", repo_key=repo_key,
                               agent_type="vod", user_id=0)


@pytest.fixture()
def tk(monkeypatch):
    """真实 tk_ token（不走依赖覆盖，端到端校验幂等层与真实鉴权链路）。"""
    token = _issue_token()
    monkeypatch.setattr("src.domains.mgmt.user.db.get_gitcode_token", lambda *a, **k: BOT)
    yield token


@pytest.fixture()
def _cleanup_idem_rows():
    """每用例清空 grape_t_idempotency_keys（隔离，防同键跨用例串扰）。"""
    from src.session_manager import db, db_lock
    from src.domains.mgmt import idempotency
    idempotency.ensure_table()
    with db_lock:
        db.execute("DELETE FROM grape_t_idempotency_keys")
        db.commit()
    yield


def _age_rows(seconds: int, status: str | None = None):
    """把（可按 status 过滤的）幂等行 created_at/updated_at 回拨指定秒数（模拟时效）。"""
    from datetime import datetime, timedelta
    from src.session_manager import db, db_lock
    past = (datetime.now() - timedelta(seconds=seconds)).strftime("%Y-%m-%d %H:%M:%S")
    with db_lock:
        if status is None:
            db.execute("UPDATE grape_t_idempotency_keys SET created_at=?, updated_at=?", (past, past))
        else:
            db.execute("UPDATE grape_t_idempotency_keys SET created_at=?, updated_at=? WHERE status=?",
                       (past, past, status))
        db.commit()


def _mk_client(token: str) -> TestClient:
    return TestClient(app, headers={"Authorization": f"Bearer {token}"})


def _fake_upstream_ok():
    """Mock 上游：返回可判定的评论对象。"""
    return lambda m, u, token="", body=None: {"ok": True, "id": 4242, "body": body}


# ===================== Slice A：同键重复写 → 回放首次响应 =====================

def test_same_key_replays_first_response(monkeypatch, tk, _cleanup_idem_rows):
    """同 Idempotency-Key 重复写：上游仅执行一次，第二次回放首次响应并带标记头。"""
    calls = []
    _fake = _fake_upstream_ok()

    def _rec(method, url, token="", body=None):
        calls.append(url)
        return _fake(method, url, token, body)

    monkeypatch.setattr("src.domains.mgmt.user.repo_manager._req", _rec)
    c = _mk_client(tk)
    r1 = c.post(COMMENTS_PATH, headers={"Idempotency-Key": "k-2084-replay"}, json={"body": "hi"})
    assert r1.status_code == 200, r1.text
    assert "x-idempotency-replayed" not in {k.lower() for k in r1.headers}
    r2 = c.post(COMMENTS_PATH, headers={"Idempotency-Key": "k-2084-replay"}, json={"body": "hi"})
    assert r2.status_code == 200, r2.text
    assert r2.json() == r1.json(), "第二次必须回放首次响应"
    assert r2.headers.get("x-idempotency-replayed") == "true"
    assert len(calls) == 1, "上游写操作只允许执行一次"


def test_same_key_different_body_422(monkeypatch, tk, _cleanup_idem_rows):
    """同键不同 body → 422 idempotency_key_conflict，且不再触达上游。"""
    calls = []

    def _rec(method, url, token="", body=None):
        calls.append(url)
        return {"ok": True, "id": 1}

    monkeypatch.setattr("src.domains.mgmt.user.repo_manager._req", _rec)
    c = _mk_client(tk)
    r1 = c.post(COMMENTS_PATH, headers={"Idempotency-Key": "k-2084-conflict"}, json={"body": "aa"})
    assert r1.status_code == 200, r1.text
    r2 = c.post(COMMENTS_PATH, headers={"Idempotency-Key": "k-2084-conflict"}, json={"body": "DIFFERENT"})
    assert r2.status_code == 422, r2.text
    d = r2.json()["detail"]
    assert d["code"] == 422
    assert d["reason"] == "idempotency_key_conflict"
    assert len(calls) == 1, "冲突请求不得触达上游"


def test_concurrent_same_key_409_single_execution(monkeypatch, tk, _cleanup_idem_rows):
    """并发同键 → 第二个 409 idempotency_in_flight，上游仅执行一次（无重复副作用）。"""
    entered = threading.Event()
    release = threading.Event()
    calls = []
    lock = threading.Lock()

    def _blocking(method, url, token="", body=None):
        with lock:
            calls.append(url)
        entered.set()
        assert release.wait(timeout=15)
        return {"ok": True, "id": 99}

    monkeypatch.setattr("src.domains.mgmt.user.repo_manager._req", _blocking)
    first_result = {}

    def _first():
        cc = _mk_client(tk)
        first_result["r"] = cc.post(COMMENTS_PATH,
                                    headers={"Idempotency-Key": "k-2084-conc"},
                                    json={"body": "x"})

    t = threading.Thread(target=_first, daemon=True)
    t.start()
    assert entered.wait(timeout=15), "第一个请求应已进入上游"
    c2 = _mk_client(tk)
    r2 = c2.post(COMMENTS_PATH, headers={"Idempotency-Key": "k-2084-conc"}, json={"body": "x"})
    assert r2.status_code == 409, r2.text
    d = r2.json()["detail"]
    assert d["code"] == 409
    assert d["reason"] == "idempotency_in_flight"
    release.set()
    t.join(timeout=15)
    assert first_result["r"].status_code == 200, first_result["r"].text
    assert len(calls) == 1, "并发窗口内上游只允许执行一次"


# ===================== Slice B：504 uncertain 与接管窗口 =====================

def test_504_keeps_in_flight_blind_retry_409_then_takeover_after_stale(monkeypatch, tk, _cleanup_idem_rows):
    """写 504 uncertain：键保持 in_flight（盲重试 → 409 第二道防线）；超过接管窗口后
    「先回读确认未落地，再带原键重试」可重执行并置 done。"""
    from src.domains.mgmt.user.repo_manager import UpstreamTimeoutError
    from src.domains.mgmt import idempotency

    calls = []
    phase = {"timeout": True}

    def _fake(method, url, token="", body=None):
        calls.append(url)
        if phase["timeout"]:
            raise UpstreamTimeoutError("Cannot reach GitCode API: read timed out")
        return {"ok": True, "id": 77}

    monkeypatch.setattr("src.domains.mgmt.user.repo_manager._req", _fake)
    c = _mk_client(tk)
    r1 = c.post(COMMENTS_PATH, headers={"Idempotency-Key": "k-2084-504",
                                        "X-Request-ID": "rid-2084"}, json={"body": "x"})
    assert r1.status_code == 504, r1.text
    assert r1.json()["detail"]["uncertain"] is True

    # 盲重试（未回读确认）→ 409，且不再触达上游
    r2 = c.post(COMMENTS_PATH, headers={"Idempotency-Key": "k-2084-504"}, json={"body": "x"})
    assert r2.status_code == 409, r2.text
    assert r2.json()["detail"]["reason"] == "idempotency_in_flight"
    assert len(calls) == 1

    # 回读确认未落地后，超过接管窗口（默认 120s）的原键重试 → 重执行
    _age_rows(idempotency._IN_FLIGHT_STALE_SECONDS + 5)
    phase["timeout"] = False
    r3 = c.post(COMMENTS_PATH, headers={"Idempotency-Key": "k-2084-504"}, json={"body": "x"})
    assert r3.status_code == 200, r3.text
    assert len(calls) == 2, "接管窗口外的重试允许重执行一次"

    # 重执行成功 → done；后续同键 → 回放，不再执行
    r4 = c.post(COMMENTS_PATH, headers={"Idempotency-Key": "k-2084-504"}, json={"body": "x"})
    assert r4.status_code == 200, r4.text
    assert r4.json() == r3.json()
    assert r4.headers.get("x-idempotency-replayed") == "true"
    assert len(calls) == 2


def test_upstream_4xx_releases_key_for_safe_retry(monkeypatch, tk, _cleanup_idem_rows):
    """上游确定性 4xx（如 404 透传）→ 键释放（重试可安全再执行，无落地风险）。"""
    from src.domains.mgmt.user.repo_manager import UpstreamAPIError
    calls = []

    def _fake(method, url, token="", body=None):
        calls.append(url)
        raise UpstreamAPIError(method, url, 404, '{"message":"Not Found"}')

    monkeypatch.setattr("src.domains.mgmt.user.repo_manager._req", _fake)
    c = _mk_client(tk)
    r1 = c.post(COMMENTS_PATH, headers={"Idempotency-Key": "k-2084-404"}, json={"body": "x"})
    assert r1.status_code == 404, r1.text
    r2 = c.post(COMMENTS_PATH, headers={"Idempotency-Key": "k-2084-404"}, json={"body": "x"})
    assert r2.status_code == 404, r2.text
    assert len(calls) == 2, "确定性 4xx 后键可复用（重试重新执行上游）"


def test_local_validation_422_releases_key_for_safe_retry(monkeypatch, tk, _cleanup_idem_rows):
    """本地校验 422（请求未触达上游，确定性失败）→ 键释放（QA 复审键毒化修复）。

    同键首坏 body（校验 422）后，修正 body 重试必须重新执行——不得因首坏 body 的
    指纹绑定而 422 idempotency_key_conflict（键毒化至 TTL 24h，违背
    docs/openapi「确定性 4xx（上游透传/本地校验）→ 键释放」承诺）。
    """
    calls = []

    def _fake(method, url, token="", body=None):
        calls.append(url)
        return {"ok": True, "id": len(calls)}

    monkeypatch.setattr("src.domains.mgmt.user.repo_manager._req", _fake)
    c = _mk_client(tk)
    r1 = c.post(COMMENTS_PATH, headers={"Idempotency-Key": "k-2084-local422"}, json={"body": 123})
    assert r1.status_code == 422, r1.text  # FastAPI 本地校验（body 非 str），未触达上游
    assert len(calls) == 0, "本地校验失败不得触达上游"
    r2 = c.post(COMMENTS_PATH, headers={"Idempotency-Key": "k-2084-local422"},
                json={"body": "fixed"})
    assert r2.status_code == 200, r2.text
    assert len(calls) == 1, "键释放后同键修正 body 重试应重新执行（不得 422 conflict 键毒化）"


def test_done_record_ttl_24h_expiry(monkeypatch, tk, _cleanup_idem_rows):
    """done 记录 24h TTL 过期 → 同键请求按新请求处理（可重执行）。"""
    calls = []

    def _fake(method, url, token="", body=None):
        calls.append(url)
        return {"ok": True, "id": len(calls)}

    monkeypatch.setattr("src.domains.mgmt.user.repo_manager._req", _fake)
    c = _mk_client(tk)
    r1 = c.post(COMMENTS_PATH, headers={"Idempotency-Key": "k-2084-ttl"}, json={"body": "x"})
    assert r1.status_code == 200, r1.text
    r2 = c.post(COMMENTS_PATH, headers={"Idempotency-Key": "k-2084-ttl"}, json={"body": "x"})
    assert r2.json() == r1.json() and len(calls) == 1
    _age_rows(24 * 3600 + 60)  # 超过 TTL 24h
    r3 = c.post(COMMENTS_PATH, headers={"Idempotency-Key": "k-2084-ttl"}, json={"body": "x"})
    assert r3.status_code == 200, r3.text
    assert len(calls) == 2, "TTL 过期后同键按新请求执行"
    assert r3.json()["id"] == 2


# ===================== Slice C：零回归边界（无键 / GET / 非 tk_） =====================

def test_no_key_passthrough_unchanged(monkeypatch, tk, _cleanup_idem_rows):
    """无 Idempotency-Key → 行为零变更：透传执行，无回放标记。"""
    calls = []

    def _fake(method, url, token="", body=None):
        calls.append(url)
        return {"ok": True, "id": len(calls)}

    monkeypatch.setattr("src.domains.mgmt.user.repo_manager._req", _fake)
    c = _mk_client(tk)
    r1 = c.post(COMMENTS_PATH, json={"body": "x"})
    r2 = c.post(COMMENTS_PATH, json={"body": "x"})
    assert r1.status_code == 200 and r2.status_code == 200
    assert len(calls) == 2, "无键请求不受幂等层影响"
    assert "x-idempotency-replayed" not in {k.lower() for k in r2.headers}


def test_empty_key_header_treated_as_absent(monkeypatch, tk, _cleanup_idem_rows):
    """空 Idempotency-Key 头按缺省处理（透传）。"""
    monkeypatch.setattr("src.domains.mgmt.user.repo_manager._req", _fake_upstream_ok())
    c = _mk_client(tk)
    r = c.post(COMMENTS_PATH, headers={"Idempotency-Key": "  "}, json={"body": "x"})
    assert r.status_code == 200, r.text
    assert "x-idempotency-replayed" not in {k.lower() for k in r.headers}


def test_get_ignores_idempotency_key(monkeypatch, tk, _cleanup_idem_rows):
    """GET 带 Idempotency-Key → 幂等层不介入（读无落地风险，#1403 读侧重试口径不变）。"""
    calls = []

    def _fake(method, url, token="", body=None):
        calls.append(url)
        return [{"id": 1}]

    monkeypatch.setattr("src.domains.mgmt.user.repo_manager._req", _fake)
    c = _mk_client(tk)
    r1 = c.get(COMMENTS_PATH, headers={"Idempotency-Key": "k-2084-get"})
    r2 = c.get(COMMENTS_PATH, headers={"Idempotency-Key": "k-2084-get"})
    assert r1.status_code == 200 and r2.status_code == 200
    assert len(calls) == 2
    assert "x-idempotency-replayed" not in {k.lower() for k in r2.headers}


def test_non_session_token_skips_idempotency(monkeypatch, _cleanup_idem_rows):
    """非 tk_ 凭据（如 grape_ API Key）→ 幂等层跳过（git-repo 写本就仅限会话 token，
    403 由 _repo_token 判定；幂等不得改变该路径行为）。"""
    app.dependency_overrides[get_current_user_or_api_key] = \
        lambda: {"id": 98701, "username": "t", "auth_type": "api_key",
                 "agent_type": "vod", "repo_key": f"gitcode:{OWNER}/{REPO}"}
    try:
        calls = []

        def _fake(method, url, token="", body=None):
            calls.append(url)
            return {"ok": True}

        monkeypatch.setattr("src.domains.mgmt.user.repo_manager._req", _fake)
        c = TestClient(app, headers={"Authorization": "Bearer grape_faketest",
                                     "PRIVATE-TOKEN": "grape_faketest"})
        r1 = c.post(COMMENTS_PATH, headers={"Idempotency-Key": "k-2084-api"}, json={"body": "x"})
        r2 = c.post(COMMENTS_PATH, headers={"Idempotency-Key": "k-2084-api"}, json={"body": "x"})
        assert r1.status_code == 403 and r2.status_code == 403, \
            "非会话 token 走既有 403 语义（幂等层不改变判定路径）"
        assert len(calls) == 0
    finally:
        app.dependency_overrides.clear()


def test_invalid_session_token_still_401(monkeypatch, _cleanup_idem_rows):
    """无效 tk_ token + 幂等键 → 401（幂等层不得掩盖鉴权失败）。"""
    monkeypatch.setattr("src.domains.mgmt.user.db.get_gitcode_token", lambda *a, **k: BOT)
    monkeypatch.setattr("src.domains.mgmt.user.repo_manager._req", _fake_upstream_ok())
    c = TestClient(app, headers={"Authorization": "Bearer tk_invalid-token-2084"})
    r = c.post(COMMENTS_PATH, headers={"Idempotency-Key": "k-2084-401"}, json={"body": "x"})
    assert r.status_code == 401, r.text


def test_key_scoped_per_repo_path(monkeypatch, tk, _cleanup_idem_rows):
    """键按 (repo_key, method, path, key) 命名空间隔离：同键不同 issue 路径互不串扰。"""
    calls = []

    def _fake(method, url, token="", body=None):
        calls.append(url)
        return {"ok": True, "id": len(calls)}

    monkeypatch.setattr("src.domains.mgmt.user.repo_manager._req", _fake)
    c = _mk_client(tk)
    r1 = c.post(f"/api/git-repo/{OWNER}/{REPO}/issues/55/comments",
                headers={"Idempotency-Key": "k-2084-scope"}, json={"body": "x"})
    r2 = c.post(f"/api/git-repo/{OWNER}/{REPO}/issues/66/comments",
                headers={"Idempotency-Key": "k-2084-scope"}, json={"body": "x"})
    assert r1.status_code == 200 and r2.status_code == 200
    assert r2.json()["id"] == 2, "不同路径同键 = 两个独立逻辑操作，各自执行"
    assert len(calls) == 2


# ===================== QA Round 2 判断级残余处置（R1/R2） =====================

def test_local_validation_422_not_misreported_as_fail_open(monkeypatch, tk, _cleanup_idem_rows,
                                                            caplog):
    """R2（QA Round 2 残余）：本地校验 422 的 re-raise 不得落入外层 fail-open。

    RequestValidationError 在内层已释放键并 re-raise；若被外层 except Exception
    捕获，会误报「idempotency layer failed (fail-open)」并把 original(request)
    再执行一遍（校验双执行）。客户端语义虽等价，该路径不得被走到。
    """
    import logging
    caplog.set_level(logging.WARNING, logger="devops-bot")
    monkeypatch.setattr("src.domains.mgmt.user.repo_manager._req", _fake_upstream_ok())
    c = _mk_client(tk)
    r = c.post(COMMENTS_PATH, headers={"Idempotency-Key": "k-2084-no-failopen"},
               json={"body": 123})
    assert r.status_code == 422, r.text  # FastAPI 本地校验失败，未触达上游
    misfires = [rec.getMessage() for rec in caplog.records
                if "idempotency layer failed" in rec.getMessage()]
    assert not misfires, f"本地 422 被 fail-open 路径误报：{misfires}"


def test_status_in_flight_constant_single_source():
    """R1（QA Round 2 残余）：STATUS_IN_FLIGHT 不得是死常量（裸串残留）。

    begin_write 返回值与 handler 侧状态比较须消费常量（与 STATUS_DONE 同口径），
    此前 :176 return 裸串 / :293 裸串比较为引入常量后的残余。
    """
    from pathlib import Path
    _repo_root = Path(__file__).resolve().parents[1]
    src = (_repo_root / "src" / "domains" / "mgmt" / "idempotency.py").read_text(encoding="utf-8")  # arch11 落位
    assert 'return "in_flight"' not in src, "begin_write 返回须用 STATUS_IN_FLIGHT"
    assert '== "in_flight"' not in src, "handler 状态比较须用 STATUS_IN_FLIGHT"
