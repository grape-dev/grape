"""台阶3：资源 API 卡驱动化——双轨等价性 + schema 守卫。

双轨期等价性断言（点2）：对 gitcode/github/gitee/gitlab 四个存量平台，把各自
providers 模块规则转换为等价的声明化 resources 段（仅 JSON 可表达子集），构造
同一组输入分别走"卡优先路径"与"_PROVIDER_MODULES 回退路径"，逐字段结构化对比
输出完全一致（非快照比对）。transform 回调形态（assignees 列表化/Closes #N
追加/inner_issue_nums 拼接）纯 JSON 不可表达——对应字段从等价性输入中排除，
并在守卫用例锁"卡不可声明 transform"由模块路径继续承接。

守卫（点4/点5）：resources 段整体可缺省；段内已声明的子结构缺必填字段、
路径模板无占位符才拒（静态路径不误伤）；PlatformCardError 报错信息必须
含卡 ID（provider）、字段路径、期望格式三要素。
"""
from __future__ import annotations

import json

import pytest

import src.domains.runtime.vcs.platform_card as platform_card
from src.domains.runtime.vcs.platform_card import (
    PlatformCardError, build_platform_card,
    invalidate_platform_card_cache,
)

_LEGACY = ("gitcode", "github", "gitee", "gitlab")


# ---------------------------------------------------------------------------
# 双轨等价性：模块规则 → 声明化卡 → 两路径逐字段对比
# ---------------------------------------------------------------------------
def _declarable_rule(rule):
    """模块字段规则 → JSON 可声明形态（不可表达返回 None=该字段排除）。"""
    if isinstance(rule, str):
        return rule
    if isinstance(rule, dict):
        if rule.get("drop"):
            return {"drop": True}
        if "transform" not in rule and "native" in rule:
            out = {"native": rule["native"]}
            if "values" in rule:
                out["values"] = dict(rule["values"])
            return out
    return None  # transform 回调：JSON 不可声明


def _rehearsal_resources(mod):
    """模块 _RULES/_QUERY_STATE → 等价卡 resources 段（跳过 transform 字段）。"""
    from src.domains.runtime.vcs.providers import github as gh
    rules = {}
    skipped = set()
    for (res, op), field_rules in mod._RULES.items():
        card_rule = {}
        for field_name, rule in field_rules.items():
            dec = _declarable_rule(rule)
            if dec is None:
                skipped.add((res, op, field_name))
                continue
            card_rule[field_name] = dec
        if card_rule:
            rules[f"{res}.{op}"] = card_rule
    # repo_segment/path_map 按 _vcs_url 现有钩子语义回填（无钩子=github 缺省）
    resources = {"repo_segment": "repos/{owner}/{repo}", "rules": rules}
    if getattr(mod, "PATH_MAP", None):
        resources["path_map"] = dict(mod.PATH_MAP)
    # github 缺省与该模块 deviates 时显式声明 query_state（逐键覆盖）
    qs = {k: v for k, v in mod._QUERY_STATE.items() if gh._QUERY_STATE.get(k) != v}
    if qs:
        resources["query_state"] = qs
    return resources, skipped


@pytest.fixture(scope="module")
def rehearsal_cards(tmp_path_factory):
    """为四存量平台各写一张"迁移彩排卡"（resources 复刻模块规则）。"""
    from src.domains.runtime.vcs.providers import _MODULES
    d = tmp_path_factory.mktemp("rehearsal_cards")
    skipped_all = {}
    for p in _LEGACY:
        resources, skipped = _rehearsal_resources(_MODULES[p])
        skipped_all[p] = skipped
        base = platform_card.get_platform_card(p)  # 存量在册卡为基（host/api_base 真值）
        assert base is not None
        raw = {"name": base.name, "dialect": base.dialect, "host": base.host,
               "webhook_path": base.webhook_path, "event_header": base.event_header,
               "api_base": base.api_base, "api_auth": base.api_auth,
               "webhook_verify": base.webhook_verify, "clone_auth": base.clone_auth}
        raw["resources"] = resources
        (d / f"{p}.json").write_text(json.dumps(raw), encoding="utf-8")
    return d, skipped_all


@pytest.mark.parametrize("provider", _LEGACY)
def test_dual_track_equivalence(rehearsal_cards, monkeypatch, provider):
    """同一输入：卡优先路径 vs 模块回退路径，逐字段结构化一致（非快照）。"""
    from src.domains.runtime.vcs.providers import _MODULES, translate_inbound, translate_query_state
    d, skipped_all = rehearsal_cards
    monkeypatch.setattr(platform_card, "_CONFIGS_DIR", str(d))
    invalidate_platform_card_cache()
    try:
        card = platform_card.get_platform_card(provider)
        assert card is not None and card.resources
        mod = _MODULES[provider]
        skipped = skipped_all[provider]
        # 逐 (resource, op) 构造仅含可声明字段的输入（每字段一个非平凡值）
        for (res, op), field_rules in mod._RULES.items():
            declarable = {fn for fn in field_rules
                          if (res, op, fn) not in skipped}
            if not declarable:
                continue
            neutral = {fn: f"v_{fn}" for fn in declarable}
            ctx = {"owner_repo": "o/r", "owner": "o", "repo": "r"}
            module_out = mod.translate(res, op, dict(neutral), dict(ctx))
            card_out = translate_inbound(provider, res, op, dict(neutral), dict(ctx))
            assert card_out == module_out, f"{provider} {res}.{op} 双轨输出偏离"
        # query_state 双轨
        for state in ("open", "closed", "all"):
            assert translate_query_state(provider, state) == mod.query_state(state)
    finally:
        invalidate_platform_card_cache()


def test_dual_track_transform_fields_stay_module_only(rehearsal_cards):
    """transform 回调字段（assignees 列表化/Closes #N/inner_issue_nums）锁为
    模块独占：JSON 卡不可声明，等价性输入排除，行为由模块路径继续承接。"""
    _, skipped_all = rehearsal_cards
    assert ("pulls", "create", "related_issue_nums") in skipped_all["github"]
    assert ("issues", "create", "assignee") in skipped_all["github"]
    assert ("pulls", "create", "related_issue_nums") in skipped_all["gitcode"]
    assert ("issues", "create", "assignee") not in skipped_all["gitee"]  # gitee 纯 str 规则可声明


# ---------------------------------------------------------------------------
# schema 守卫：整段可缺省；段内缺必填/路径模板无占位符才拒；报错三要素
# ---------------------------------------------------------------------------
def _expect_error(raw, needle_path, needle_fmt):
    with pytest.raises(PlatformCardError) as ei:
        build_platform_card("guardp", raw)
    msg = str(ei.value)
    # 点5：报错三要素——卡 ID、字段路径、期望格式
    assert "guardp" in msg, f"报错缺卡 ID: {msg}"
    assert needle_path in msg, f"报错缺字段路径 {needle_path}: {msg}"
    assert needle_fmt in msg, f"报错缺期望格式 {needle_fmt}: {msg}"


def test_guard_resources_optional():
    """resources 段整体可缺省（存量卡零影响）。"""
    card = build_platform_card("guardp", {"host": "g.example.com"})
    assert card.resources == {}


def test_guard_repo_segment_placeholder_required():
    """声明为路径模板的 repo_segment 必须含 {owner}/{repo} 占位符。"""
    _expect_error({"host": "g.example.com",
                   "resources": {"repo_segment": "projects/foo"}},
                  "resources.repo_segment", "{owner}")


def test_guard_repo_segment_static_path_not_flagged():
    """静态路径不误伤：path_map 值是字面路径（无占位符）合法。"""
    card = build_platform_card("guardp", {
        "host": "g.example.com",
        "resources": {"repo_segment": "repos/{owner}/{repo}",
                      "path_map": {"/pulls": "/merge_requests"}}})
    assert card.resources["path_map"] == {"/pulls": "/merge_requests"}


def test_guard_rules_key_shape():
    """rules 键必须 <resource>.<op>。"""
    _expect_error({"host": "g.example.com",
                   "resources": {"rules": {"issuescreate": {"title": "title"}}}},
                  "resources.rules", "<resource>.<op>")


def test_guard_rule_missing_native():
    """段内声明子结构缺必填字段（dict 形态缺 native）→ 拒。"""
    _expect_error({"host": "g.example.com",
                   "resources": {"rules": {"issues.create": {"state": {"values": {}}}}}},
                  "resources.rules['issues.create']['state']", "native")


def test_guard_unknown_key():
    """resources 未知键整段拒绝（臆造能力位防呆）。"""
    _expect_error({"host": "g.example.com",
                   "resources": {"paths": {}}},
                  "resources", "未知键")


def test_guard_query_state_keys():
    """query_state 键必须是 open/closed/all。"""
    _expect_error({"host": "g.example.com",
                   "resources": {"query_state": {"opened": "opened"}}},
                  "resources.query_state", "all")


# ---------------------------------------------------------------------------
# 纯配卡平台：git_repo_api 支持判定/垫片接入（无模块）
# ---------------------------------------------------------------------------
def test_card_only_provider_gate():
    from src.api.git_repo_api import _is_supported_provider
    assert _is_supported_provider("github") is True      # 模块
    assert _is_supported_provider("nosuchp_xyz") is False  # 无模块无卡


# ---------------------------------------------------------------------------
# D105-14 改动点 C：模板插值第四形态（8 用例；纯 str.replace，无表达式求值）
# ---------------------------------------------------------------------------

def _tpl_card(provider, rules):
    return build_platform_card(provider, {"host": f"{provider}.example.com", "resources": {"rules": rules}})


def _tpl_rules(field_rule):
    return {"issues.create": dict(field_rule)}


def test_tpl_1_append_closes_number():
    """append + {body} 引原值 → Closes #N 正确拼接。"""
    card = _tpl_card("zztpl", _tpl_rules(
        {"body": {"mode": "append", "trigger": "number",
                  "template": "{body}\n\nCloses #{number}"}}))
    rules = card.resources["rules"]["issues.create"]
    from src.domains.runtime.vcs.providers.base import apply_rules
    out = apply_rules(rules, {"body": "修复说明", "number": 12}, {})
    assert out["body"] == "修复说明\n\nCloses #12"


def test_tpl_2_bad_trigger_field_build_red():
    """trigger 错字段（不在中性字段集）→ 构建期红，报错三要素齐。"""
    with pytest.raises(PlatformCardError) as ei:
        _tpl_card("zztpl", _tpl_rules(
            {"body": {"mode": "append", "trigger": "numbr",
                      "template": "{body}\n\nCloses #{number}"}}))
    msg = str(ei.value)
    assert "zztpl" in msg and "issues.create" in msg.replace("'", "") and "trigger" in msg


def test_tpl_3_mode_default_replace():
    """mode 缺省 = replace：输出即模板渲染结果。"""
    card = _tpl_card("zztpl", _tpl_rules(
        {"title": {"trigger": "number", "template": "[{number}] {title}"}}))
    rules = card.resources["rules"]["issues.create"]
    from src.domains.runtime.vcs.providers.base import apply_rules
    out = apply_rules(rules, {"title": "标题", "number": 7}, {})
    assert out["title"] == "[7] 标题"


def test_tpl_4_placeholder_typo_build_red():
    """占位符拼写错（{numbr}）→ 构建期红。"""
    with pytest.raises(PlatformCardError) as ei:
        _tpl_card("zztpl", _tpl_rules(
            {"body": {"mode": "append", "template": "{body} {numbr}"}}))
    assert "numbr" in str(ei.value)


def test_tpl_5_cross_field_template_plus_values():
    """跨字段 template+values 自由组合：同 (resource,op) 两字段各走各形态。"""
    card = _tpl_card("zztpl", {"issues.create": {
        "body": {"mode": "append", "trigger": "number",
                 "template": "{body}\n\nCloses #{number}"},
        "state": {"native": "state", "values": {"open": "opened"}},
    }})
    rules = card.resources["rules"]["issues.create"]
    from src.domains.runtime.vcs.providers.base import apply_rules
    out = apply_rules(rules, {"body": "b", "number": 3, "state": "open"}, {})
    assert out["body"] == "b\n\nCloses #3" and out["state"] == "opened"


def test_tpl_6_runtime_missing_placeholder_strict_raises():
    """strict：运行时触发锚在但占位符数据缺失 → 抛错留痕（不静默保留字面量）。"""
    card = _tpl_card("zztpl", _tpl_rules(
        {"title": {"trigger": "number", "template": "[{number}] {title}"}}))
    rules = card.resources["rules"]["issues.create"]
    from src.domains.runtime.vcs.providers.base import apply_rules
    with pytest.raises(ValueError) as ei:
        apply_rules(rules, {"number": 12}, {})  # title 缺失 → 占位符数据缺失
    assert "title" in str(ei.value)


def test_tpl_7_trigger_empty_no_output():
    """触发锚空 → 不产出该字段（缺省锚=key 字段）。"""
    card = _tpl_card("zztpl", _tpl_rules(
        {"title": {"trigger": "number", "template": "[{number}] {title}"}}))
    rules = card.resources["rules"]["issues.create"]
    from src.domains.runtime.vcs.providers.base import apply_rules
    out = apply_rules(rules, {"title": "标题"}, {})  # number 缺 → 触发锚空
    assert "title" not in out


def test_tpl_8_same_field_template_values_mutually_exclusive():
    """同 field 内 template 与 values 互斥 → 构建期红。"""
    with pytest.raises(PlatformCardError) as ei:
        _tpl_card("zztpl", _tpl_rules(
            {"body": {"mode": "append", "template": "{body} x",
                      "values": {"a": "b"}}}))
    assert "互斥" in str(ei.value)
