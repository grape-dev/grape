"""旧版 meta 覆盖层残留必须可清理（升级后"改了不生效"）。

回归背景：管理面改为直接落表后不再写 vcs_platform_overrides，但老键没人清；
文件卡升级后旧覆盖与新基线取值相同，运行时仍叠加（空串还会盖掉新默认值）。
"""
from __future__ import annotations

from dataclasses import dataclass

from src.domains.runtime.vcs.card_overrides import CardOverrides
from src.domains.runtime.public import compute_card_overrides


@dataclass(frozen=True)
class _Card:
    """diff_card_fields 走 dataclasses.asdict，桩必须是 dataclass。"""

    dialect: str = "gitlab"
    host: str = "gitlab.example.com"
    webhook_path: str = "/webhook/gitlab"
    brand_color: str = "#fc6d26"


def _base(provider):
    defaults = {
        "gitlab": _Card(),
        "gitee": _Card(dialect="github", host="gitee.com",
                       webhook_path="/webhook/gitee", brand_color="#c71d23"),
    }
    return defaults.get(provider)


def test_prune_stale_drops_entries_equal_to_baseline():
    ov = CardOverrides({"gitlab": {"host": "gitlab.example.com"},          # 与基线相同 → 残留
                        "gitee": {"host": "gitee.internal.example.com"}})   # 真改动 → 保留
    pruned = ov.prune_stale(_base)
    assert pruned.by_provider == {"gitee": {"host": "gitee.internal.example.com"}}


def test_prune_stale_keeps_unknown_provider_for_manual_handling():
    ov = CardOverrides({"private-x": {"host": "x.example.com"}})
    assert ov.prune_stale(_base).by_provider == {"private-x": {"host": "x.example.com"}}


def test_prune_stale_handles_lookup_failure():
    def _boom(provider):
        raise RuntimeError("card lookup failed")

    ov = CardOverrides({"gitlab": {"host": "h.example.com"}})
    assert ov.prune_stale(_boom).by_provider == {"gitlab": {"host": "h.example.com"}}


def test_stale_providers_lists_only_equivalent_entries():
    ov = CardOverrides({"gitlab": {"host": "gitlab.example.com"},
                        "gitee": {"host": "gitee.internal.example.com"}})
    assert ov.stale_providers(_base) == ["gitlab"]


def test_admin_save_prunes_older_stale_entries(monkeypatch):
    """管理面保存一次就会把历史残留清掉（无需人工改 meta）。"""
    import src.domains.runtime.vcs.platform_card as pc

    monkeypatch.setattr(pc, "base_platform_card", _base, raising=True)
    current = '{"gitlab": {"host": "gitlab.example.com"}, "gitee": {"host": "old.example.com"}}'
    out = compute_card_overrides({"gitee": {"host": "gitee.com"}}, current)
    # gitlab 残留被清、gitee 改回基线也清（与基线一致即不记覆盖）
    assert out == {}