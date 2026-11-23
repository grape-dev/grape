"""A 方案口径：表里已有该平台时，老 meta 覆盖层不得再叠加（否则"改了不生效"）。"""
from __future__ import annotations

from src.domains.runtime.vcs.card_sources import PlatformCardAssembler


class _Src:
    def __init__(self, raws, degraded=""):
        self._raws = raws
        self.degraded = degraded
        self.name = "fake"

    def raws(self):
        return dict(self._raws)


class _File:
    def __init__(self, raws):
        self._raws = raws

    def available(self):
        return True

    def raws(self):
        return dict(self._raws)


def _assembler(table_raws, overrides):
    return PlatformCardAssembler(
        meta=_Src({}),
        file_source=_File({"gitlab-corp": {"dialect": "gitlab", "host": "file.example.com"}}),
        builtin=_Src({}),
        fallback_sources=[_Src(table_raws)],
        overrides_source=_Src(overrides),
    )


def test_table_row_wins_over_legacy_meta_override():
    a = _assembler(
        table_raws={"gitlab-corp": {"dialect": "gitlab", "name": "改后",
                                    "host": "table.example.com"}},
        overrides={"gitlab-corp": {"name": "旧值"}},
    )
    raws = a.assemble_raws()
    assert raws["gitlab-corp"]["name"] == "改后", "表是唯一真源，老覆盖不得覆盖它"
    assert raws["gitlab-corp"]["host"] == "table.example.com"


def test_override_still_applies_when_table_has_no_row():
    """表里没有该平台（导入器未跑/测试注入）时，覆盖层仍按原优先级叠加。"""
    a = _assembler(table_raws={}, overrides={"gitlab-corp": {"brand_color": "#123456"}})
    raws = a.assemble_raws()
    assert raws["gitlab-corp"]["brand_color"] == "#123456"