"""Issue #4345：A2A 人工「重派」基址候选探测与错误映射回归。

覆盖 Spec 三条主路径 + 两项守卫：
1. 回退基址（runtime_server_url_fallback）上游 400 → 接口 422，body 带
   candidate_base_url / source / upstream_status / reason，不再 500；
2. 显式基址可用 → 派发成功（原事件 cancelled + 新 seq 接管）；
3. 30s 内同 seq 重复重派 → 回放上一次回执（成功/失败一致），不再 500/409 交替；
4. 回退基址候选校验失败 → 短路不发 POST（不把请求打到未验证基址）；
5. 失败审计标 dispatch=failed + reason + candidate_base_url；error 行单点 emit，
   seq 为空不落行。
"""
import logging
import os

import pytest
from fastapi import HTTPException

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("DEVOPS_DB_PATH", os.path.join(ROOT, "data", "test_sessions.db"))

from src.api import a2a_dispatch as a2ad
from src.domains.runtime.routes import events as ev


@pytest.fixture(autouse=True)
def _reset_state(monkeypatch):
    from src.domains.runtime.engine import takeover as tk
    tk._A2A_DISPATCH_BASE_CACHE = ""
    ev._redispatch_replay.clear()
    _store: dict[str, str] = {}
    monkeypatch.setattr("src.domains.mgmt.public.get_meta_value",
                        lambda k: _store.get(k, ""))
    monkeypatch.setattr("src.domains.mgmt.public.set_meta_value",
                        lambda k, v: _store.__setitem__(k, v))
    yield
    tk._A2A_DISPATCH_BASE_CACHE = ""
    ev._redispatch_replay.clear()


def _fallback_target(base="https://www.grapedev.cn", verified=True, reason="probed"):
    return {
        "base": base,
        "source": "runtime_server_url_fallback",
        "verified": verified,
        "candidate_base_url": base,
        "candidates": [{"base_url": base, "source": "runtime_server_url_fallback",
                        "ok": verified, "detail": reason}],
        "reason": reason,
    }


def _evt():
    return {"repo_key": "gitcode:o/r", "issue_id": "#1", "event_type": "a2a_event",
            "agent_type": "coding", "payload": {"issue_number": 1}}


# ===== 路径 1：回退基址上游 400 → 422（不再 500） =====

def test_fallback_upstream_400_maps_to_422_with_decision_fields(monkeypatch):
    monkeypatch.setattr(a2ad, "resolve_a2a_dispatch_target",
                        lambda runtime_id=0, exclude=None: _fallback_target())
    monkeypatch.setattr(ev, "_mark_event_redispatched", lambda *a, **k: None)

    def _boom(base_url, body, headers=None, timeout=30.0):
        raise a2ad.A2AUpstreamError(
            "A2A 上游返回 HTTP 400", base_url=base_url,
            upstream_status=400, reason="上游返回 HTTP 400",
            detail='{"detail":"bad request"}')

    monkeypatch.setattr(a2ad, "dispatch_a2a", _boom)

    with pytest.raises(HTTPException) as ei:
        ev._redispatch_a2a_event(107800, _evt())

    assert ei.value.status_code == 422
    detail = ei.value.detail
    assert detail["upstream_status"] == 400
    assert detail["candidate_base_url"] == "https://www.grapedev.cn"
    assert detail["source"] == "runtime_server_url_fallback"
    assert "reason" in detail and detail["reason"]


# ===== 路径 1b：回退候选校验失败 → 短路，不发 POST =====

def test_unverified_fallback_short_circuits_without_post(monkeypatch):
    monkeypatch.setattr(a2ad, "resolve_a2a_dispatch_target",
                        lambda runtime_id=0, exclude=None: _fallback_target(
                            verified=False, reason="no dispatchable a2a candidate"))
    called = {"post": 0}

    def _post(*a, **k):
        called["post"] += 1
        return {"status": "ok", "seq": 1}

    monkeypatch.setattr(a2ad, "dispatch_a2a", _post)

    with pytest.raises(HTTPException) as ei:
        ev._redispatch_a2a_event(107801, _evt())
    assert ei.value.status_code == 422
    assert called["post"] == 0, "回退基址校验失败必须短路，不得发出派发请求"
    assert ei.value.detail["candidate_base_url"] == "https://www.grapedev.cn"
    assert "no dispatchable" in ei.value.detail["reason"]


# ===== 路径 2：显式基址可用 → 派发成功（新 seq 接管） =====

def test_explicit_base_success_takes_over_with_new_seq(monkeypatch):
    target = {"base": "https://a2a.example.com", "source": "explicit_a2a_api_url",
              "verified": True, "candidate_base_url": "https://a2a.example.com",
              "candidates": [], "reason": "trusted source"}
    monkeypatch.setattr(a2ad, "resolve_a2a_dispatch_target",
                        lambda runtime_id=0, exclude=None: target)
    monkeypatch.setattr(a2ad, "dispatch_a2a",
                        lambda base_url, body, headers=None, timeout=30.0:
                        {"status": "ok", "seq": 999})
    marked = {}
    monkeypatch.setattr(ev, "_mark_event_redispatched",
                        lambda seq, new_seq, channel: marked.update(
                            {"seq": seq, "new_seq": new_seq, "channel": channel}))
    monkeypatch.setattr("src.api.event_store.mark_event_redispatch_of",
                        lambda new_seq, seq: None)

    res = ev._redispatch_a2a_event(107802, _evt())

    assert res["status"] == "redispatched"
    assert res["event_seq"] == 999
    assert res["original_seq"] == 107802
    assert marked == {"seq": 107802, "new_seq": 999, "channel": "A2A"}


# ===== 上游 5xx / 传输异常 → 本端 5xx，原因标注传输层 =====

def test_upstream_5xx_maps_to_502(monkeypatch):
    monkeypatch.setattr(a2ad, "resolve_a2a_dispatch_target",
                        lambda runtime_id=0, exclude=None: _fallback_target())

    def _boom(base_url, body, headers=None, timeout=30.0):
        raise a2ad.A2AUpstreamError("upstream 503", base_url=base_url,
                                    upstream_status=503, reason="上游返回 HTTP 503")

    monkeypatch.setattr(a2ad, "dispatch_a2a", _boom)
    with pytest.raises(HTTPException) as ei:
        ev._redispatch_a2a_event(107803, _evt())
    assert ei.value.status_code == 502
    assert ei.value.detail["upstream_status"] == 503


def test_transport_failure_maps_to_502_with_transport_reason(monkeypatch):
    monkeypatch.setattr(a2ad, "resolve_a2a_dispatch_target",
                        lambda runtime_id=0, exclude=None: _fallback_target())

    def _boom(base_url, body, headers=None, timeout=30.0):
        raise a2ad.A2ATransportError("连接失败", base_url=base_url,
                                     reason="传输层异常：ConnectError: refused")

    monkeypatch.setattr(a2ad, "dispatch_a2a", _boom)
    with pytest.raises(HTTPException) as ei:
        ev._redispatch_a2a_event(107804, _evt())
    assert ei.value.status_code == 502
    assert ei.value.detail["upstream_status"] == 0
    assert "传输层" in ei.value.detail["reason"]


# ===== 共享解析器：候选可达性语义（与 takeover 同源） =====

def _patch_source(monkeypatch, base, source="runtime_server_url_fallback"):
    monkeypatch.setattr("src.kernel.prompt_builder.resolve_a2a_base_with_source",
                        lambda *a, **kw: (base, source))


def test_resolver_fallback_unverified_when_no_dispatchable_candidate(monkeypatch):
    from src.domains.runtime.engine import takeover as tk
    _patch_source(monkeypatch, "https://www.grapedev.cn")
    monkeypatch.setattr(tk, "_probe_dispatchable",
                        lambda base, timeout=2.5: (False, "health HTTP 404"))
    monkeypatch.setattr(tk, "_a2a_internal_base",
                        lambda runtime_id=0, exclude=None: "http://127.0.0.1:8000")
    tk._A2A_DISPATCH_BASE_CACHE = ""
    target = a2ad.resolve_a2a_dispatch_target(0)
    assert target["verified"] is False
    assert target["candidate_base_url"] == "https://www.grapedev.cn"
    assert target["source"] == "runtime_server_url_fallback"


def test_resolver_fallback_verified_when_probe_passes(monkeypatch):
    from src.domains.runtime.engine import takeover as tk
    _patch_source(monkeypatch, "https://worker.example.com")
    monkeypatch.setattr(tk, "_probe_dispatchable",
                        lambda base, timeout=2.5: (True, "health HTTP 200, a2a HTTP 400"))
    tk._A2A_DISPATCH_BASE_CACHE = ""
    target = a2ad.resolve_a2a_dispatch_target(0)
    assert target["verified"] is True
    assert target["base"] == "https://worker.example.com"
    assert tk._A2A_DISPATCH_BASE_CACHE == "https://worker.example.com"


def test_resolver_trusted_source_skips_probe(monkeypatch):
    from src.domains.runtime.engine import takeover as tk
    _patch_source(monkeypatch, "https://gw.example.com/a2a", source="gateway_prefix_a2a")
    probed = {"n": 0}
    monkeypatch.setattr(tk, "_probe_dispatchable",
                        lambda *a, **kw: (probed.__setitem__("n", probed["n"] + 1) or True, "x"))
    target = a2ad.resolve_a2a_dispatch_target(0)
    assert target["verified"] is True
    assert target["source"] == "gateway_prefix_a2a"
    assert probed["n"] == 0, "可信任来源不得触发可达性探测"


# ===== 路径 3：30s 同 seq 幂等回放 =====

def test_replay_returns_identical_success_receipt(monkeypatch):
    calls = {"n": 0}

    def _fake(seq, local_only, user, skip_debounce):
        calls["n"] += 1
        return {"status": "redispatched", "task_id": "t_1", "event_seq": seq}

    monkeypatch.setattr(ev, "_redispatch_event", _fake)
    monkeypatch.setattr(ev, "_audit_admin_action", lambda *a, **k: None)
    monkeypatch.setattr(ev, "_load_event", lambda s: {"repo_key": "gitcode:o/r"})

    first = ev._run_redispatch(7001, local_only=False, user={"username": "u"},
                               skip_debounce=False)
    second = ev._run_redispatch(7001, local_only=False, user={"username": "u"},
                                skip_debounce=False)
    assert first == second
    assert calls["n"] == 1, "30s 内同 seq 第二次必须回放，不得重复执行"


def test_replay_returns_identical_failure_receipt_not_409_flapping(monkeypatch):
    calls = {"n": 0}
    detail = {"code": 422, "candidate_base_url": "https://www.grapedev.cn",
              "source": "runtime_server_url_fallback", "upstream_status": 400,
              "reason": "上游返回 HTTP 400"}

    def _fake(seq, local_only, user, skip_debounce):
        calls["n"] += 1
        raise HTTPException(status_code=422, detail=dict(detail))

    monkeypatch.setattr(ev, "_redispatch_event", _fake)
    monkeypatch.setattr(ev, "_audit_admin_action", lambda *a, **k: None)
    monkeypatch.setattr(ev, "_load_event", lambda s: {"repo_key": "gitcode:o/r"})

    with pytest.raises(HTTPException) as first:
        ev._run_redispatch(7002, local_only=False, user={"username": "u"},
                           skip_debounce=False)
    with pytest.raises(HTTPException) as second:
        ev._run_redispatch(7002, local_only=False, user={"username": "u"},
                           skip_debounce=False)
    assert first.value.status_code == second.value.status_code == 422
    assert first.value.detail == second.value.detail
    assert calls["n"] == 1, "失败回执同样必须回放，不能 500/409 交替"


# ===== 审计 & 单点 error 行 =====

def test_failure_audit_marks_dispatch_failed_with_decision_fields(monkeypatch):
    captured = {}

    def _fake(seq, local_only, user, skip_debounce):
        raise HTTPException(status_code=422, detail={
            "reason": "上游返回 HTTP 400",
            "candidate_base_url": "https://www.grapedev.cn",
            "source": "runtime_server_url_fallback",
            "upstream_status": 400,
        })

    def _audit(repo_key, seq, action, user, **kw):
        captured.update({"action": action, **kw})

    monkeypatch.setattr(ev, "_redispatch_event", _fake)
    monkeypatch.setattr(ev, "_audit_admin_action", _audit)
    monkeypatch.setattr(ev, "_load_event", lambda s: {"repo_key": "gitcode:o/r"})

    with pytest.raises(HTTPException):
        ev._run_redispatch(7003, local_only=False, user={"username": "u"},
                           skip_debounce=False)

    assert captured["action"] == "redispatch_failed"
    extra = captured["extra"]
    assert extra["dispatch"] == "failed"
    assert extra["candidate_base_url"] == "https://www.grapedev.cn"
    assert extra["reason"] == "上游返回 HTTP 400"
    assert extra["upstream_status"] == 400


def test_single_error_line_with_seq_and_no_ghost(monkeypatch, caplog):
    def _fake(seq, local_only, user, skip_debounce):
        raise HTTPException(status_code=422, detail={
            "reason": "上游返回 HTTP 400",
            "candidate_base_url": "https://www.grapedev.cn",
            "source": "runtime_server_url_fallback",
            "upstream_status": 400,
        })

    monkeypatch.setattr(ev, "_redispatch_event", _fake)
    monkeypatch.setattr(ev, "_audit_admin_action", lambda *a, **k: None)
    monkeypatch.setattr(ev, "_load_event", lambda s: {"repo_key": "gitcode:o/r"})
    monkeypatch.setattr("src.kernel.prompt_builder.format_base_url_candidates",
                        lambda *a, **k: "候选 base_url: -")

    with caplog.at_level(logging.WARNING):
        with pytest.raises(HTTPException):
            ev._run_redispatch(7004, local_only=False, user={"username": "u"},
                               skip_debounce=False)

    redispatch_lines = [r for r in caplog.records
                        if "[redispatch]" in r.getMessage()]
    assert len(redispatch_lines) == 1, [r.getMessage() for r in caplog.records]
    msg = redispatch_lines[0].getMessage()
    assert "seq=7004" in msg
    assert "candidate_base_url=https://www.grapedev.cn" in msg
    assert "seq=''" not in msg and "seq= failed" not in msg


def test_failure_log_skipped_when_seq_missing(caplog):
    with caplog.at_level(logging.WARNING):
        ev._redispatch_log_failure(0, {"reason": "x"})
        ev._redispatch_log_failure(None, "boom")
    assert not [r for r in caplog.records if "[redispatch]" in r.getMessage()], \
        "seq 为空时不得落 redispatch error 行（幽灵行）"