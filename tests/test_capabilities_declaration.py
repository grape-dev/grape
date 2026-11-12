"""D105-14 评审差距一/二：平台能力显式声明（capabilities）行为锁。

差距一：能力从"resources.rules 缺席的隐式推断"提升为可查询的一等声明——
  卡可选 capabilities 数组，枚举校验 fail-closed（未知值构建期红）。
差距二剩余：覆盖一致性——声明的 "<resource>.<op>" 能力必须有承载
  （resources.rules 对应键 或 代码模块），两者皆无 = 构建期红。
消费侧：require_capability 对声明白名单外的能力显式拒绝；未声明卡不限制
（存量四平台零影响）。
"""
import pytest

from src.domains.runtime.vcs.platform_card import (
    PlatformCardError,
    build_platform_card,
    card_capabilities,
    require_capability,
)


def _card(provider, raw_extra):
    raw = {"host": f"{provider}.example.com"}
    raw.update(raw_extra)
    return build_platform_card(provider, raw)


def test_capabilities_declared_and_queryable():
    """声明 capabilities → 构建成功且可查询（能力一等公民）。"""
    card = _card("zzcap", {"capabilities": ["issues.create"],
                           "resources": {"rules": {"issues.create": {"title": "title"}}}})
    assert sorted(card_capabilities("zzcap", card=card)) == ["issues.create"]
    assert set(card.capabilities) == {"issues.create"}


def test_capabilities_unknown_value_fail_closed():
    """未登记能力（拼写/越界）→ 构建期红（fail-closed，schema 枚举约束）。"""
    with pytest.raises(PlatformCardError) as ei:
        _card("zzcap", {"capabilities": ["issues.destory"]})
    assert "issues.destory" in str(ei.value) and "允许枚举" in str(ei.value)


def test_capability_without_coverage_build_red():
    """覆盖一致性：声明 issues.create 但无 rules 且无代码模块 → 构建期红。"""
    with pytest.raises(PlatformCardError) as ei:
        _card("zzcap2", {"capabilities": ["issues.create"]})  # 纯配卡无 resources
    assert "无承载" in str(ei.value)


def test_capability_covered_by_rules_build_ok():
    """rules 有对应键 → 覆盖成立，构建 OK。"""
    card = _card("zzcap3", {"capabilities": ["issues.create"],
                            "resources": {"rules": {"issues.create": {"title": "title"}}}})
    assert card.capabilities == ("issues.create",)


def test_capability_covered_by_module_build_ok():
    """存量代码模块（_MODULES 在册）可承载声明的能力（module 路径仍有效）。"""
    card = _card("gitcode", {"capabilities": ["issues.create", "issues.update",
                                              "pulls.create", "webhook.verify"]})
    assert "issues.create" in card.capabilities


def test_undeclared_card_unrestricted():
    """存量四平台卡未声明 capabilities → card_capabilities None，不限制。"""
    assert card_capabilities("gitcode") is None
    require_capability("gitcode", "issues.create")  # 不抛


def test_require_capability_fail_closed_for_declared_card():
    """消费侧 fail-closed：声明白名单外能力 → 显式拒绝（含声明集三要素）。"""
    card4 = _card("zzcap4", {"capabilities": ["issues.query"],
                             "resources": {"rules": {"issues.query": {"state": "state"}}}})
    require_capability("zzcap4", "issues.query", card=card4)  # 在册 → 通过
    with pytest.raises(PlatformCardError) as ei:
        require_capability("zzcap4", "pulls.create", card=card4)
    msg = str(ei.value)
    assert "pulls.create" in msg and "zzcap4" in msg and "显式拒绝" in msg


def test_capability_coverage_missing_for_module_less_platform():
    """基础设施能力（webhook.verify 无 rules 形态）+ 无模块 → 覆盖一致性红。"""
    with pytest.raises(PlatformCardError) as ei:
        _card("zzcap5", {"capabilities": ["webhook.verify"]})
    assert "无承载" in str(ei.value)
