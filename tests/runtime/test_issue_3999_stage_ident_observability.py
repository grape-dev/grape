"""Issue #3999：get_issue_stage 非数字 ident 的观测补齐。

根因（`/internal/issues/card` 对非数字 ident 恒 422）已由 #4065（PR #2063）消除；
本文件锁定 #3999 验收里的观测项：

1. ident 无法归一为卡片主键（`pr_*` 等）时不触达 provider，`invalid_ident_total{provider}` 递增；
2. provider hop 失败打 warning（含 `repo_key` 与 `ident`）并回退 `*_any`；
3. `*_any` 回退直读失败打 warning（不再静默 `pass`）；
4. 数字单号路径零回归（provider 正常命中，回退不执行）。
"""
import logging

import pytest
from prometheus_client import CollectorRegistry

from src.kernel.gsb import issue_card_metrics as icm

REPO = "gitcode:grape-dev/grape"
CARD_ROW = ("coding", "coding", 0, "2026-11-25T00:00:00", "", "scene", 0, "", "", 0)


class _FakeProvider:
    def __init__(self, cards=None, exc=None):
        self.cards = cards or {}
        self.exc = exc
        self.calls = []

    def get_issue_card(self, repo_key, issue_number):
        self.calls.append((repo_key, issue_number))
        if self.exc:
            raise self.exc
        return self.cards.get(str(issue_number), {})


def _patch_provider(monkeypatch, fake):
    import src.kernel.gsb.provider as provider
    monkeypatch.setattr(provider, "get_service", lambda _name: fake)


class TestProviderLabel:
    @pytest.mark.parametrize("repo_key,expected", [
        ("gitcode:owner/repo", "gitcode"),
        ("gitee:GTDTeam/CodeArts", "gitee"),
        ("github:owner/repo", "github"),
        ("gitlab-corp:grp/sub/proj", "other"),
        ("owner/repo", "gitcode"),
    ])
    def test_provider_of(self, repo_key, expected):
        assert icm.provider_of(repo_key) == expected

    def test_install_idempotent(self):
        reg = CollectorRegistry()
        assert icm.install(reg) is True
        assert icm.install(reg) is False


class TestGetIssueStageObservability:
    def test_invalid_ident_counts_and_skips_provider(self, monkeypatch):
        from src.api import session_store
        reg = CollectorRegistry()
        icm.install(reg)
        fake = _FakeProvider()
        _patch_provider(monkeypatch, fake)
        before = reg.get_sample_value(
            icm.INVALID_IDENT_METRIC, {"provider": "gitcode"}) or 0.0

        assert session_store.get_issue_stage(REPO, "pr_123") is None
        assert fake.calls == [], "不可归一 ident 不应触达卡片端点"
        after = reg.get_sample_value(
            icm.INVALID_IDENT_METRIC, {"provider": "gitcode"}) or 0.0
        assert after == before + 1

    def test_provider_failure_warns_and_falls_back(self, monkeypatch, caplog):
        from src.api import session_store
        fake = _FakeProvider(exc=RuntimeError("card hop down"))
        _patch_provider(monkeypatch, fake)

        def _read(name, params):
            if name == "issue_card_row":
                return CARD_ROW
            return None

        monkeypatch.setattr(session_store.rt, "read_one_any", _read)
        with caplog.at_level(logging.WARNING):
            info = session_store.get_issue_stage(REPO, "4065")

        assert info is not None and info["stage"] == "coding"
        assert fake.calls == [(REPO, 4065)]
        assert "provider failed, fallback to _any" in caplog.text
        assert f"repo_key={REPO}" in caplog.text
        assert "ident=4065" in caplog.text

    def test_fallback_read_failure_warns(self, monkeypatch, caplog):
        from src.api import session_store
        fake = _FakeProvider()
        _patch_provider(monkeypatch, fake)

        def _read(name, params):
            raise RuntimeError(f"db down for {name}")

        monkeypatch.setattr(session_store.rt, "read_one_any", _read)
        with caplog.at_level(logging.WARNING):
            assert session_store.get_issue_stage(REPO, "4065") is None

        assert "card fallback read failed" in caplog.text
        assert f"repo_key={REPO}" in caplog.text
        assert "ident=4065" in caplog.text

    def test_numeric_path_unchanged(self, monkeypatch):
        from src.api import session_store
        fake = _FakeProvider(cards={"4065": {"stage": "coding"}})
        _patch_provider(monkeypatch, fake)
        fallback_calls = []
        monkeypatch.setattr(session_store.rt, "read_one_any",
                            lambda *a: fallback_calls.append(a) or None)

        info = session_store.get_issue_stage(REPO, "4065")
        assert info == {"stage": "coding"}
        assert fake.calls == [(REPO, 4065)]
        assert fallback_calls == [], "provider 命中时不应执行回退直读"