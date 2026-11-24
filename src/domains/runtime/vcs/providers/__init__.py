"""Git provider 中性翻译入口（git-repo-provider-neutral-interface-design.md §4.2）。

对外统一：translate_inbound(provider, resource, op, neutral, ctx) / translate_query_state。
provider 由 token repo_key 前缀决定；每 provider 一个模块，新增 provider = 加模块 + 注册。

台阶3（资源 API 卡驱动化）：消费顺序改为——
  1) 平台卡 resources 段（声明化规则，纯 JSON）优先；
  2) providers/<name>.py 模块（_MODULES）回退——存量四平台卡无 resources 段，
     走模块，行为零变更；
  3) 两者皆无 → ValueError。
module_for(provider) 同时为纯配卡平台（卡有 resources 段）生成通用模块垫片
（card_module.CardProviderModule），URL/request/token 消费面同样免模块；
无 resources 段但方言与内置模块同名（自建实例卡）则套 FamilyProviderModule 代理，
属性委托族模块、API_BASE 取卡上的 host。
"""
import re

from . import gitcode, github
from . import gitee, gitlab  # S107/G2：方言层补齐（gitee v5 同族 / gitlab v4 偏离形态）
from .base import apply_rules

_MODULES = {"gitcode": gitcode, "github": github, "gitee": gitee, "gitlab": gitlab}

# D105-14：模块回退审计——每 provider 进程内去重一次（usage_log 为 10000 有界
# 队列，存量四平台每次 API 调用都命中 _MODULES，不去重会挤掉凭据审计）。
_fallback_reported: set = set()


def _report_module_fallback(provider: str, executor_context: str) -> None:
    """module_for 落 _MODULES 回退时的审计埋点（D105-14 改动点 B）。

    形态复用 token_resolver._audit_source：审计失败静默不阻断，返回值与
    调用方零变更。usage_log 查 operation LIKE 'module_fallback_%' 即
    "哪些平台存在代码级依赖"完整清单。
    """
    if provider in _fallback_reported:
        return
    _fallback_reported.add(provider)
    try:
        from src.kernel.persistence.credential_usage_log import log_usage
        log_usage("", "", credential_kind="integration_path",
                  executor_context=executor_context,
                  operation=f"module_fallback_{provider}", repo_key=f"{provider}:")
    except Exception:
        pass  # 审计失败不阻断（同 _audit_source 存量形态，D105-14 豁免登记）


def _caller_context() -> str:
    """取 module_for 调用方函数名（零侵入：sys frame，替代逐调用点硬编码）。"""
    import sys
    frame = sys._getframe(2)
    return f"{frame.f_code.co_filename.rsplit('/', 1)[-1]}:{frame.f_code.co_name}"


def _card(provider: str):
    try:
        from src.domains.runtime.vcs.platform_card import get_platform_card
        return get_platform_card(provider)
    except Exception:
        return None



def module_for(provider: str):
    """台阶3：provider → 模块消费面（_MODULES 优先，纯配卡平台落通用垫片）。

    无模块且无 resources 卡 → None（调用方 fail-fast）；但卡方言能套到同族代码模块
    （自建实例，`gitlab-corp` 一类）时返回 FamilyProviderModule 代理，API_BASE 取卡。

    Issue #3580 复审：自建实例卡（如 gitlab-corp，无代码模块、无 resources 段）此前落
    None → git-repo 网关对实例仓全量 502。现按**卡方言**回落到同族代码模块
    （`card.dialect` 与 `_MODULES` 键同名），并套 `FamilyProviderModule` 代理把
    API_BASE 换成卡上的实例 host——零新增平台字面量，内置四平台路径不变。
    """
    if provider in _MODULES:
        # D105-14 改动点 B：回退审计（零行为变更；_caller_context 用 frame 取
        # 调用方，避免逐调用点硬编码漂移）
        _report_module_fallback(provider, _caller_context())
        return _MODULES[provider]
    card = _card(provider)
    if card is not None and getattr(card, "resources", None):
        from .card_module import card_module_for
        return card_module_for(card)
    if card is not None:
        _family = _MODULES.get(str(getattr(card, "dialect", "") or ""))
        if _family is not None:
            from .family_module import family_module_for
            return family_module_for(card, _family)
    return None


def native_field_for(provider: str, resource: str, op: str, field: str) -> str:
    """中性字段 → 该 provider 的原生字段名（能力位派生，字符串空=无原生承载）。

    S89/D65 家族守卫配套：业务代码判断"某平台某操作是否原生支持某能力"时，
    查翻译规则表而不是比较平台名字面量（与 D60③/S85 派生清零同口径）。
    卡驱动（resources 段）平台无代码级规则表 → 一律按"无原生承载"返回空串。
    """
    _mod = module_for(provider)
    _rules = getattr(_mod, "_RULES", None) or {}
    _rule = _rules.get((resource, op), {}).get(field)
    if isinstance(_rule, str):
        return _rule
    if isinstance(_rule, dict):
        return str(_rule.get("native") or "")
    return ""


def translate_inbound(provider: str, resource: str, op: str, neutral: dict,
                      ctx: dict | None = None) -> dict:
    """中性字段 dict → provider 原生 payload（inbound 翻译；卡优先→模块回退）。

    台阶3 缺省语义（字段级合并）：卡带 resources 段时走卡驱动路径——
    默认规则 = github 形态对应 (resource, op) 的 _RULES（缺省仅覆盖 github
    已定义的资源类型；新增资源类型必须在卡 resources.rules 显式声明），
    卡 rules 逐字段覆盖缺省（新卡只写差异字段）。
    注意：github 缺省中的 transform 回调形态（assignees 列表化/Closes #N
    追加等）是代码侧缺省，纯 JSON 卡不可声明同名 transform——迁移卡可透传
    或 drop 这类字段，复杂语义仍走 providers 模块。
    """
    card = _card(provider)
    cres = (getattr(card, "resources", None) or {}) if card is not None else {}
    if cres:
        from . import github as _gh
        default = _gh._RULES.get((resource, op))
        card_rule = (cres.get("rules") or {}).get(f"{resource}.{op}")
        if default is None and card_rule is None:
            # 显式声明原则：非 github 内置资源类型/操作，卡未声明即拒绝（不静默透传）
            raise ValueError(
                f"Git provider 未支持: {provider} "
                f"(resource/op {resource}.{op} 未在卡 resources 声明，且非 github 内置资源类型)")
        return apply_rules({**(default or {}), **(card_rule or {})}, neutral or {}, ctx or {})
    # TODO(弃用条件)：四平台卡迁移完成后删除 _PROVIDER_MODULES 回退（台阶3 双轨期）
    mod = _MODULES.get(provider)
    if mod is None:
        raise ValueError(f"Git provider 未支持: {provider}")
    return mod.translate(resource, op, neutral or {}, ctx or {})


def translate_query_state(provider: str, state: str) -> str:
    """中性 state（open/closed/all）→ provider query 值（卡优先→模块回退）。

    卡路径缺省合并：github 形态恒等表为缺省，卡 query_state 逐键覆盖。
    """
    card = _card(provider)
    cres = (getattr(card, "resources", None) or {}) if card is not None else {}
    if cres:
        from . import github as _gh
        qs = {**_gh._QUERY_STATE, **(cres.get("query_state") or {})}
        return qs.get(state, state)
    # TODO(弃用条件)：四平台卡迁移完成后删除 _PROVIDER_MODULES 回退（台阶3 双轨期）
    mod = _MODULES.get(provider)
    if mod is None:
        raise ValueError(f"Git provider 未支持: {provider}")
    return mod.query_state(state)