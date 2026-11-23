"""Issue #4082：worker `runtime_id=0` 探活恒 401 的收敛看护。

三条验收路径 + 可观测：

1. 客户端守卫：id 缺失/非法（0/abc/负数）→ **不发 HTTP**，落结构化 `RUNTIME_ID_MISSING`；
   注册回包非法 id 不再以 0 兜底（视为注册失败走退避重试）；心跳过滤非法 id。
2. 服务端：`/api/runtime/runtimes/{id}/*` 非法 id 统一 400 `invalid runtime_id`（非 401/422）；
   合法 id 路径行为不变（仍走凭据鉴权 401）。
3. 可观测：探活失败按 host/reason 计数 `grape_runtime_tags_probe_failure_total`，
   连续失败达阈值才发信号（防刷），`deploy/alerts.yml` 有连续失败告警规则。
"""

import asyncio
import hashlib
import json
import os

import pytest

from src.domains.runtime.engine import runtime_id_guard as guard


@pytest.fixture(autouse=True)
def _isolate_worker_uid_store(monkeypatch, tmp_path):
    """注册/uid 落盘隔离到 tmp_path：不在工作区留 worker-engine-uids.json 杂物。"""
    monkeypatch.setenv("GRAPE_WORKER_UID_FILE", str(tmp_path / "worker-engine-uids.json"))


# ---------------------------------------------------------------- 切片①：纯函数口径

def test_parse_runtime_id_accepts_only_positive_int():
    """只有正整数是合法 runtime id：0/负数/非数字/bool/浮点/空串一律 None。"""
    assert guard.parse_runtime_id(12) == 12
    assert guard.parse_runtime_id("12") == 12
    assert guard.parse_runtime_id(" 12 ") == 12
    for bad in (0, -3, "-3", "0", "abc", "", None, True, False, 1.5, "1.5"):
        assert guard.parse_runtime_id(bad) is None, f"{bad!r} 应判非法"
    assert guard.is_valid_runtime_id("7") is True
    assert guard.is_valid_runtime_id(0) is False


def test_missing_runtime_id_error_is_structured():
    """`RUNTIME_ID_MISSING` 是结构化错误：含 code/host/runtime_id/context/message。"""
    err = guard.missing_runtime_id_error(0, host="h-1", context="claim")
    assert err.code == "RUNTIME_ID_MISSING"
    detail = err.to_dict()
    assert detail["code"] == "RUNTIME_ID_MISSING"
    assert detail["host"] == "h-1"
    assert detail["runtime_id"] == "0"
    assert detail["context"] == "claim"
    assert "positive integer" in detail["message"]


# ---------------------------------------------------------------- 切片②：服务端非法 id → 400

def _client():
    from fastapi.testclient import TestClient
    from src.api.main import app
    return TestClient(app)


@pytest.mark.parametrize("bad", ["0", "abc", "-1", "1.5", "00"])
def test_invalid_runtime_id_returns_400(bad):
    """`/runtimes/{非法 id}/tags` → 400 `invalid runtime_id`（配置问题不伪装成鉴权问题）。"""
    r = _client().get(f"/api/runtime/runtimes/{bad}/tags")
    assert r.status_code == 400, f"{bad}: {r.status_code} {r.text}"
    body = r.json()
    assert body["code"] == 400
    assert body["error"] == "invalid runtime_id"
    assert bad in str(body.get("runtime_id", ""))


def test_valid_runtime_id_still_hits_credential_auth():
    """合法 id 路径行为不变：无凭据仍是 401（400 只拦非法 id，不放宽/不收紧鉴权）。"""
    r = _client().get("/api/runtime/runtimes/12345/tags")
    assert r.status_code == 401, r.text


def test_invalid_runtime_id_guard_covers_view_router():
    """用户面同一路径（A 进程 view_router 注册）同样 400——单一实现源两端生效。"""
    r = _client().get("/api/runtime/runtimes/abc/role-tags")
    assert r.status_code == 400, r.text
    assert r.json()["error"] == "invalid runtime_id"


# ---------------------------------------------------------------- 切片③：worker 探活守卫

def _mk_client(monkeypatch=None):
    from src.domains.runtime.engine.client import RuntimeClient
    return RuntimeClient(server="http://unit.invalid", token="rta_unit_test")


def test_probe_tags_id_missing_sends_no_http(monkeypatch):
    """id 非法 → 零 HTTP，reason=id_missing（这就是 203 次/小时 401 的源头）。"""
    c = _mk_client()

    async def _boom(*a, **kw):
        raise AssertionError("非法 runtime_id 不得发出任何 HTTP 请求")

    monkeypatch.setattr(c, "_request_raw", _boom)
    for bad in (0, "abc", None):
        tags, reason = asyncio.run(c.probe_tags(bad))
        assert tags == []
        assert reason == "id_missing"


def test_probe_tags_reasons(monkeypatch):
    """200 有标签=成功；200 空=no_tags；401=unauthorized；404=not_found。"""
    c = _mk_client()
    calls = []

    async def _fake(method, path, body=None, retries=1, **kw):
        calls.append(path)
        return fake_status.pop(0)

    fake_status = [
        (200, {"tags": ["coding"]}, "{}"),
        (200, {"tags": []}, "{}"),
        (401, {"detail": "revoked"}, "revoked"),
        (404, {"detail": "runtime gone"}, "gone"),
    ]
    monkeypatch.setattr(c, "_request_raw", _fake)

    assert asyncio.run(c.probe_tags(9)) == (["coding"], "")
    assert asyncio.run(c.probe_tags(9)) == ([], "no_tags")
    assert asyncio.run(c.probe_tags(9)) == ([], "unauthorized")
    assert asyncio.run(c.probe_tags(9)) == ([], "not_found")
    assert calls and all("runtimes/9/tags" in p for p in calls)


def test_probe_tags_401_does_not_kill_worker(monkeypatch):
    """标签探活 401 不得抛 CredentialRevokedError（那是吊销语义，会误杀 worker 进程）。"""
    c = _mk_client()

    async def _fake(method, path, body=None, retries=1, **kw):
        return 401, {"detail": "x"}, "x"

    monkeypatch.setattr(c, "_request_raw", _fake)
    tags, reason = asyncio.run(c.probe_tags(9))
    assert tags == [] and reason == "unauthorized"


def test_register_drops_invalid_ids_and_treats_as_failure(monkeypatch, caplog):
    """注册回包 id=0/None 不再兜底成 0：过滤后视为注册失败（调用方退避重试）。"""
    c = _mk_client()

    async def _fake_request(method, path, body=None, retries=4, **kw):
        return {"runtimes": [{"id": 0, "name": "coding"},
                             {"id": None, "name": "qa"},
                             {"id": "abc", "name": "vod"}],
                "probe_url": "http://p"}

    monkeypatch.setattr(c, "_request", _fake_request)
    with caplog.at_level("WARNING"):
        ids, probe = asyncio.run(c.register([{"name": "coding", "installed": True}]))
    assert ids == []
    assert probe == "http://p"
    assert "RUNTIME_ID_MISSING" in caplog.text


def test_register_keeps_valid_ids(monkeypatch):
    """全部合法 id → 原样回填（去 0 兜底不改变正常路径）。"""
    c = _mk_client()

    async def _fake_request(method, path, body=None, retries=4, **kw):
        return {"runtimes": [{"id": 7, "name": "coding"}], "probe_url": ""}

    monkeypatch.setattr(c, "_request", _fake_request)
    ids, _probe = asyncio.run(c.register([{"name": "coding", "installed": True}]))
    assert ids == [7]


def test_heartbeat_filters_invalid_ids(monkeypatch):
    """心跳只上报合法 id；全部非法 → 不发请求，返回 None（不再把 0 刷进 payload）。"""
    c = _mk_client()
    sent = []

    async def _fake_raw(method, path, body=None, retries=1, **kw):
        sent.append((path, body))
        return 200, {"status": "ok"}, "{}"

    async def _fake_req(method, path, body=None, retries=4, **kw):
        sent.append((path, body))
        return {"status": "ok"}

    async def _hb_session():
        return object()

    monkeypatch.setattr(c, "_request_raw", _fake_raw)
    monkeypatch.setattr(c, "_request", _fake_req)
    monkeypatch.setattr(c, "_heartbeat_session", _hb_session)

    asyncio.run(c.heartbeat([0, 5, "abc", -1]))
    assert sent, "应发送一次心跳"
    assert sent[0][1]["runtime_ids"] == [5]

    sent.clear()
    assert asyncio.run(c.heartbeat([0, None])) is None
    assert sent == [], "全部非法 id 时不得发心跳"


def test_claim_guard_blocks_invalid_id(monkeypatch):
    """claim/claim_by_tag 拼 URL 前守卫：非法 id 不发请求。"""
    c = _mk_client()

    async def _boom(*a, **kw):
        raise AssertionError("非法 runtime_id 不得发出 claim 请求")

    monkeypatch.setattr(c, "_request", _boom)
    monkeypatch.setattr(c, "_request_raw", _boom)

    with pytest.raises(guard.RuntimeIdMissingError):
        asyncio.run(c.claim(0))
    with pytest.raises(guard.RuntimeIdMissingError):
        asyncio.run(c.claim_by_tag("abc", "coding"))


# ---------------------------------------------------------------- 切片④：连续失败上报（防刷）

def test_probe_failure_reporter_emits_after_threshold():
    """连续失败达阈值才发一次信号，之后每 N 次再发；成功即清零。"""
    emitted = []
    rep = guard.ProbeFailureReporter(threshold=3, emit_every=3,
                                     sink=lambda payload: emitted.append(payload) or True,
                                     host="h-1", scene="s-1")
    assert [rep.observe("no_tags") for _ in range(3)] == [False, False, True]
    assert rep.observe("no_tags") is False
    assert rep.observe("no_tags") is False
    assert rep.observe("no_tags") is True
    assert rep.observe("") is False  # 成功清零
    assert [rep.observe("id_missing") for _ in range(3)] == [False, False, True]
    assert emitted[0]["reason"] == "no_tags"
    assert emitted[-1]["reason"] == "id_missing"
    assert emitted[-1]["host"] == "h-1"
    assert emitted[-1]["scene"] == "s-1"


def test_probe_failure_logging_is_throttled(caplog):
    """探活失败日志只打「首次 + 达阈值」，不逐轮刷（否则把 401 噪声换成日志噪声）。"""
    from src.domains.runtime.engine import agent as agent_mod

    rep = guard.ProbeFailureReporter(threshold=3, emit_every=3, sink=lambda p: True, host="h-2")
    with caplog.at_level("INFO"):
        assert agent_mod._handle_tags_probe_result(rep, 9, "unauthorized") is False
        first = caplog.text.count("unauthorized")
        assert first >= 1
        caplog.clear()
        assert agent_mod._handle_tags_probe_result(rep, 9, "unauthorized") is False
        assert caplog.text == ""      # 第 2 次：不出声
        assert agent_mod._handle_tags_probe_result(rep, 9, "unauthorized") is False
        assert "3 times in a row" in caplog.text   # 达阈值：告警行
        caplog.clear()
        assert agent_mod._handle_tags_probe_result(rep, 9, "unauthorized") is False
        assert caplog.text == ""      # 阈值后普通轮次继续静默
        assert agent_mod._handle_tags_probe_result(rep, 9, "") is False
        assert "recovered" in caplog.text  # 成功恢复留一条信息日志


def test_handle_tags_probe_result_flags_id_missing():
    """id_missing 必须返回 True（触发强制重注册），其余失败不触发。"""
    from src.domains.runtime.engine import agent as agent_mod

    rep = guard.ProbeFailureReporter(threshold=99, sink=lambda p: True, host="h-3")
    assert agent_mod._handle_tags_probe_result(rep, 1, "no_tags") is False
    assert agent_mod._handle_tags_probe_result(rep, 1, "not_found") is False
    assert agent_mod._handle_tags_probe_result(rep, 0, "id_missing") is True


def test_signal_sink_posts_to_master(monkeypatch, tmp_path):
    """sink 经 `POST /api/runtime/runtime-tags-probe/signal` 上报；失败落本地 JSONL 兜底。"""
    posted = []

    class _Resp:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    def _urlopen(req, timeout=None):
        posted.append((req.full_url, req.headers.get("Authorization"), req.data))
        return _Resp()

    monkeypatch.setattr(guard.urllib.request, "urlopen", _urlopen)
    sink = guard.tag_probe_signal_sink(base_url="http://master", token="rta_x",
                                       config_dir=tmp_path, timeout=1)
    assert sink({"reason": "id_missing", "host": "h-1"}) is True
    assert posted and posted[0][0] == "http://master" + guard.SIGNAL_PATH
    assert posted[0][1] == "Bearer rta_x"

    def _dead(req, timeout=None):
        raise OSError("connection refused")

    monkeypatch.setattr(guard.urllib.request, "urlopen", _dead)
    assert sink({"reason": "unauthorized", "host": "h-2"}) is False
    assert (tmp_path / guard.FALLBACK_FILE).exists()


# ---------------------------------------------------------------- 切片⑤：指标承接 + 告警

_RTA_TOKEN = "rta_test_4082_probe_signal"


def _seed_credential():
    from src.kernel.session_manager import db, db_lock
    with db_lock:
        c = db.cursor()
        c.execute("DELETE FROM runtime_credentials WHERE name='probe-signal-test-4082'")
        c.execute(
            "INSERT INTO runtime_credentials (token_hash, name, device_name, scope, owner_name, status)"
            " VALUES (?,?,?, 'global', 'tester', 'active')",
            (hashlib.sha256(_RTA_TOKEN.encode()).hexdigest(),
             "probe-signal-test-4082", "probe-signal-dev"))
        db.commit()


def test_probe_failure_signal_endpoint_records_metric():
    """POST 信号 → 服务端按 host/reason 计数；未知 reason 归 unknown 不丢数；无凭据 401。"""
    from src.domains.insight.observability import (RUNTIME_TAGS_PROBE_FAILURES,
                                                   RUNTIME_TAGS_PROBE_FAILURES_METRIC,
                                                   registry)
    from prometheus_client import generate_latest
    _seed_credential()
    client = _client()
    headers = {"Authorization": f"Bearer {_RTA_TOKEN}"}

    before = RUNTIME_TAGS_PROBE_FAILURES.labels(host="dev-1", reason="id_missing")._value.get()
    r = client.post(guard.SIGNAL_PATH,
                    json={"host": "dev-1", "reason": "id_missing", "runtime_id": "0",
                          "detail": "RUNTIME_ID_MISSING"},
                    headers=headers)
    assert r.status_code == 200, r.text
    after = RUNTIME_TAGS_PROBE_FAILURES.labels(host="dev-1", reason="id_missing")._value.get()
    assert after == before + 1

    text = generate_latest(registry).decode("utf-8")
    assert f'{RUNTIME_TAGS_PROBE_FAILURES_METRIC}{{host="dev-1",reason="id_missing"}}' in text

    # 审查意见 Optional ③：未知 reason 与 observability「归 unknown 不丢数」口径统一——
    # 端点接受并计 unknown（不再 400），否则新版本 worker 新增 reason 会被 HTTP 层丢掉。
    _unknown_before = RUNTIME_TAGS_PROBE_FAILURES.labels(host="dev-1", reason="unknown")._value.get()
    r_unknown = client.post(guard.SIGNAL_PATH, json={"host": "dev-1", "reason": "bogus"},
                            headers=headers)
    assert r_unknown.status_code == 200, r_unknown.text
    assert RUNTIME_TAGS_PROBE_FAILURES.labels(host="dev-1", reason="unknown")._value.get() \
        == _unknown_before + 1
    r_noauth = client.post(guard.SIGNAL_PATH, json={"host": "dev-1", "reason": "no_tags"})
    assert r_noauth.status_code == 401


def test_unknown_reason_counts_as_unknown_not_dropped():
    """契约外 reason 不丢数：归 unknown（与 contract_guard 同口径）。"""
    from src.domains.insight.observability import (RUNTIME_TAGS_PROBE_FAILURES,
                                                   record_runtime_tags_probe_failure)
    before = RUNTIME_TAGS_PROBE_FAILURES.labels(host="h-x", reason="unknown")._value.get()
    record_runtime_tags_probe_failure("h-x", "not_a_reason")
    assert RUNTIME_TAGS_PROBE_FAILURES.labels(host="h-x", reason="unknown")._value.get() == before + 1


def _alerts_rules():
    import yaml
    path = os.path.join(os.path.dirname(__file__), "..", "..", "deploy", "alerts.yml")
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f)["groups"][0]["rules"]


def test_alerts_yml_has_runtime_tags_probe_rule():
    """`deploy/alerts.yml` 连续失败告警消费实际指标（表达式/指标名漂移看护）。"""
    from src.domains.insight.observability import RUNTIME_TAGS_PROBE_FAILURES_METRIC
    rule = next((r for r in _alerts_rules()
                 if r.get("alert") == "GrapeRuntimeTagsProbeFailing"), None)
    assert rule is not None, "deploy/alerts.yml 缺少 GrapeRuntimeTagsProbeFailing 规则"
    assert RUNTIME_TAGS_PROBE_FAILURES_METRIC in rule["expr"]
    assert "host" in rule["expr"] and "reason" in rule["expr"]  # 按 host/reason 维度分组
    assert "increase(" in rule["expr"]
    assert rule["labels"]["severity"] in ("warning", "critical")
    annotations = " ".join(str(v) for v in (rule.get("annotations") or {}).values())
    assert "id_missing" in annotations and "no_tags" in annotations  # 排查指引含 reason 语义


# ---------------------------------------------------------------- 切片⑥：多引擎隔离（PR #2066 审查 Required）
#
# 缺陷：`_run_loop` 里一个 `ProbeFailureReporter` 被该 worker 的所有 rid/引擎共享，
# 同一轮任一引擎成功探活（observe("")）就把其它引擎的连败计数清零。多引擎主机上
# 「1 个引擎失败 + 1 个引擎成功」交替 → 连败永远回到 0 → 达不到阈值 → 新增的
# GrapeRuntimeTagsProbeFailing 告警对多引擎主机完全失效。

def test_probe_reporters_are_isolated_per_rid():
    """多引擎主机：A 引擎每轮失败、B 引擎每轮成功——A 的连败不被 B 的成功清零。"""
    from src.domains.runtime.engine import agent as agent_mod

    emitted = []
    reporters = {}

    def _make():
        return guard.ProbeFailureReporter(
            threshold=3, emit_every=3, host="h-multi",
            sink=lambda p: emitted.append(p) or True)

    for _ in range(3):
        # 同一轮：rid=11 持续失败、rid=22 持续成功（旧实现下 22 的成功会清零 11 的计数）
        agent_mod._handle_tags_probe_result(
            agent_mod.tags_probe_reporter_for(reporters, 11, make=_make), 11, "no_tags")
        agent_mod._handle_tags_probe_result(
            agent_mod.tags_probe_reporter_for(reporters, 22, make=_make), 22, "")

    assert [p["reason"] for p in emitted] == ["no_tags"], \
        "多引擎交替时失败引擎必须仍能达到阈值并上报"
    assert reporters[11].consecutive == 3
    assert reporters[22].consecutive == 0 and reporters[22].emitted == 0
    assert emitted[0]["runtime_id"] == "11"   # 信号带 rid：多引擎主机可定位到具体引擎


def test_two_engines_with_different_reasons_both_alert():
    """两个引擎各自连败（no_tags / unauthorized）→ 各自达成阈值，互不吞并。"""
    from src.domains.runtime.engine import agent as agent_mod

    emitted = []
    reporters = {}

    def _make():
        return guard.ProbeFailureReporter(threshold=2, emit_every=2, host="h-multi2",
                                          sink=lambda p: emitted.append(p) or True)

    for _ in range(2):
        for rid, reason in ((11, "no_tags"), (22, "unauthorized"), (33, "")):
            agent_mod._handle_tags_probe_result(
                agent_mod.tags_probe_reporter_for(reporters, rid, make=_make), rid, reason)

    assert sorted((p["reason"], p["runtime_id"]) for p in emitted) == \
        [("no_tags", "11"), ("unauthorized", "22")]


def test_prune_reporters_drops_stale_rids_and_keeps_live_counts():
    """重注册换 id 后清掉旧 rid 的上报器：防字典无界增长，也不让旧连败复活告警。"""
    from src.domains.runtime.engine import agent as agent_mod

    reporters = {}

    def _make():
        return guard.ProbeFailureReporter(threshold=9, sink=lambda p: True, host="h-prune")

    rep11 = agent_mod.tags_probe_reporter_for(reporters, 11, make=_make)
    rep11.observe("no_tags")
    rep11.observe("no_tags")
    agent_mod.tags_probe_reporter_for(reporters, 22, make=_make)

    assert agent_mod.prune_tags_probe_reporters(reporters, [11, 44]) == [22]
    assert set(reporters) == {11}
    assert reporters[11].consecutive == 2   # 存活 rid 的连败计数不被别的 rid 影响


# ---------------------------------------------------------------- 切片⑦：契约外 reason 不再静默（Optional ②）

def test_non_contract_reason_counted_as_other_and_logged_once(caplog):
    """`http_403`/`http_422` 等契约外失败：计入 other 桶（可告警）+ 原始 reason 首次落 WARNING。"""
    emitted = []
    rep = guard.ProbeFailureReporter(threshold=2, emit_every=2, host="h-other",
                                     sink=lambda p: emitted.append(p) or True)
    with caplog.at_level("WARNING"):
        assert rep.observe("http_403") is False
        assert "http_403" in caplog.text          # 原始 reason 不丢：至少落一次日志
        caplog.clear()
        assert rep.observe("http_403") is True    # 计入连败 → 达阈值照样上报
        assert caplog.text == ""                  # 同一 reason 不逐轮刷日志
    assert guard.OTHER_REASON == "other"
    assert emitted[0]["reason"] == "other"
    assert emitted[0]["raw_reason"] == "http_403"     # 契约外的真实 reason 随信号带出


def test_network_reason_logged_but_not_counted(caplog):
    """network（连通性另有心跳指标）不计连败，但首次出现留一条线索日志。"""
    rep = guard.ProbeFailureReporter(threshold=2, sink=lambda p: True, host="h-net")
    with caplog.at_level("INFO"):
        assert rep.observe("network") is False
        assert "network" in caplog.text
        caplog.clear()
        assert rep.observe("network") is False
        assert caplog.text == ""                  # 只首次出声
    assert rep.consecutive == 0


# ---------------------------------------------------------------- 切片⑧：注册部分回填（Optional ①）

def test_register_keeps_valid_ids_when_some_are_invalid(monkeypatch, caplog):
    """回包中部分 id 非法：保留同响应的合法 id（不整体判失败），并标记需再注册。"""
    c = _mk_client()

    async def _fake_request(method, path, body=None, retries=4, **kw):
        return {"runtimes": [{"id": 7, "name": "coding"},
                             {"id": 0, "name": "qa"},
                             {"id": "abc", "name": "vod"}],
                "probe_url": "http://p"}

    monkeypatch.setattr(c, "_request", _fake_request)
    with caplog.at_level("WARNING"):
        ids, probe = asyncio.run(c.register([{"name": "coding", "installed": True}]))
    assert ids == [7]                 # 合法 id 不被同响应里的非法 id 连带丢弃
    assert probe == "http://p"
    assert "RUNTIME_ID_MISSING" in caplog.text
    assert c.last_register_invalid_ids == ["0", "abc"]   # 仍需重注册的信号


def test_reregister_cycle_schedules_retry_on_partial_ids():
    """部分回填：合法 id 照常上线，但把下次重注册提前到 backoff 后（补齐缺失引擎）。"""
    from src.domains.runtime.engine import agent as agent_mod

    class _C:
        last_register_detail = []
        last_register_invalid_ids = ["0"]

    async def _fake_rereg(client, installed, device_name, cli_version=""):
        return [7], {7: "coding"}, installed

    state = asyncio.run(agent_mod._reregister_cycle(
        _C(), [{"name": "coding", "installed": True}], "dev",
        now=1600.0, last_reregister=1000.0, last_fast_scan=1000.0,
        reregister_interval=300, fast_interval=15,
        reregister_fn=_fake_rereg, probe_fn=lambda *a, **k: None))

    assert state["runtime_ids"] == [7]
    assert state["partial_invalid_ids"] == ["0"]
    assert 0 < agent_mod._PARTIAL_ID_REREGISTER_BACKOFF < 300
    # 下次重注册 = now + backoff（而非等满 300s 周期）
    assert state["last_reregister"] == 1600.0 - 300 + agent_mod._PARTIAL_ID_REREGISTER_BACKOFF