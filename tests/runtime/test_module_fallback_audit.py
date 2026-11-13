"""D105-14 改动点 B：module_for 落 _MODULES 回退的审计行为锁。

三断言（设计 §4.2）：
(a) 卡在册平台（存量四平台）走 module_for → 落 module_fallback_<p> 一条
(b) 二次调用不重复（每 provider 进程内去重一次——usage_log 10000 有界队列
    洪峰丢最旧，不去重会挤掉凭据审计）
(c) 纯配卡平台（卡有 resources 段）走垫片不落审计
executor_context 由 sys frame 取调用方（零侵入，替代逐调用点硬编码）。
"""
import json

import pytest

import src.domains.runtime.vcs.providers as vps
from src.domains.runtime.vcs.providers import module_for


@pytest.fixture(autouse=True)
def _reset_dedup_and_capture(monkeypatch):
    """清去重集 + 捕获 log_usage 调用。"""
    vps._fallback_reported.clear()
    captured = []
    monkeypatch.setattr(
        "src.kernel.persistence.credential_usage_log.log_usage",
        lambda credential_id, user_id, **kw: captured.append(kw) or "uuid",
    )
    yield captured


def test_a_module_fallback_logged_once_per_provider(_reset_dedup_and_capture):
    m = module_for("gitcode")  # 存量四平台：卡在册（resources 空）→ 落 _MODULES
    assert m is vps._MODULES["gitcode"], "返回值语义变更（零行为变更被破坏）"
    calls = [kw for kw in _reset_dedup_and_capture if kw["operation"] == "module_fallback_gitcode"]
    assert len(calls) == 1, f"应恰好一条（去重后），实际 {len(calls)}"
    assert calls[0]["credential_kind"] == "integration_path"
    assert calls[0]["repo_key"] == "gitcode:"
    assert calls[0]["executor_context"], "executor_context 缺失——调用点不可追"
    # 二次调用不重复（断言 b）
    module_for("gitcode")
    module_for("gitcode")
    calls2 = [kw for kw in _reset_dedup_and_capture if kw["operation"] == "module_fallback_gitcode"]
    assert len(calls2) == 1, "去重失效：二次触发重复落审计"


def test_b_pure_card_platform_no_fallback_audit(_reset_dedup_and_capture, monkeypatch):
    """纯配卡平台（卡带 resources 段）走垫片，不落 module_fallback 审计。"""
    fake_card = type("C", (), {"resources": {"issues.create": {}}, "provider": "zzpure"})()
    sentinel = object()
    monkeypatch.setattr(vps, "_card", lambda p: fake_card)
    monkeypatch.setattr("src.domains.runtime.vcs.providers.card_module.card_module_for",
                        lambda card: sentinel)
    m = module_for("zzpure")
    assert m is sentinel, "纯配卡平台应走垫片"
    assert not [kw for kw in _reset_dedup_and_capture
                if kw["operation"].startswith("module_fallback_")], \
        "纯配卡平台不应落回退审计"


def test_c_unknown_provider_returns_none_no_audit(_reset_dedup_and_capture):
    """无模块无卡 → None（fail-fast 语义不变），且不落审计。"""
    assert module_for("zzunknown") is None
    assert not [kw for kw in _reset_dedup_and_capture
                if kw["operation"].startswith("module_fallback_")]
