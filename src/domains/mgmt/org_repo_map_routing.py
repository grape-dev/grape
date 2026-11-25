"""组织仓库地图：任务召回打分与能力卡判别（纯函数，无 DB）。

从 ``org_repo_map`` 拆出（架构审查 A8）：``recall_targets`` 只负责取数/组装，
逐候选打分收敛在本模块，便于单独调权重与做召回回归。

阈值来源（B2）：下列权重/截断来自历史实现与 PR 讨论，**尚无召回回归基线**；
统一收口为具名常量，待标注目标仓的任务样本跑出 precision@1 / recall@3 后再定值。
<undecided: 召回权重需要召回回归集与命中率测量后确认>
"""
from __future__ import annotations

from src.kernel.repo_key import parse_repo_key

# <undecided: 关键词命中打分，沿用历史实现>
MATCH_EXACT = 3
MATCH_CONTAINS = 2
KEYWORD_WEIGHT = 3
DOMAIN_WEIGHT = 2
INTERFACE_WEIGHT = 4
PURPOSE_HIT = 1
REPO_NAME_HIT = 10
NOT_OWNS_PENALTY = 2

# <undecided: 能力卡/路由权重，沿用历史实现>
CAP_MATCH_WEIGHT = 2
ROUTE_INCLUDE_WEIGHT = 2
ROUTE_EXCLUDE_WEIGHT = 2
PHRASE_HIT_CAP = 8
PHRASE_HIT_EXTRA_DIV = 2
TYPICAL_REQUEST_DIV = 2

# <undecided: 置信度映射，沿用历史实现>
CONFIDENCE_BASE = 0.45
CONFIDENCE_SCORE_DIV = 40.0
CONFIDENCE_MAX = 0.95

# <undecided: 输出裁剪，沿用历史实现>
REASON_MATCH_MAX = 200
REASON_CAPABILITY_MAX = 320
REASON_EXCLUDED_MAX = 120
MATCHED_EXAMPLES_MAX = 3
CORE_FUNCTIONS_OUT_MAX = 6


def _value_hit(token: str, values) -> tuple[int, str]:
    """token 与一组候选值的匹配：精确 / 包含。"""
    for value in values:
        v = str(value or "").strip().lower()
        if not v:
            continue
        if token == v:
            return MATCH_EXACT, v
        if len(token) >= 2 and (token in v or (len(v) >= 2 and v in token)):
            return MATCH_CONTAINS, v
    return 0, ""


def _phrase_hit(tokens: list[str], text: str) -> int:
    """任务示例/反例与需求的短语命中分：奖励更长、更多的词命中。"""
    body = str(text or "").strip().lower()
    if not body:
        return 0
    hits = [t for t in tokens if len(t) >= 2 and t in body]
    if not hits:
        return 0
    return min(PHRASE_HIT_CAP,
               max(len(t) for t in hits) + len(hits) // PHRASE_HIT_EXTRA_DIV)


def score_candidate(*, key: str, row: dict, mapped: dict, ai: dict,
                    tokens: list[str], min_score: int) -> dict | None:
    """对单个仓库按关键词 + 能力卡 + routing 打分；低于阈值返回 None。"""
    purpose = str(mapped.get("purpose") or "").lower()
    domains = [str(x) for x in (mapped.get("domains") or [])]
    keywords = [str(x) for x in (mapped.get("keywords") or [])]
    interfaces = [str(x) for x in (mapped.get("interfaces") or [])]
    not_owns = " ".join(str(x) for x in (mapped.get("not_owns") or [])).lower()
    core = ai.get("core_functions") or []
    routing = ai.get("routing") or {}
    _provider, _owner, name = parse_repo_key(key)
    short = str(name or "").lower()
    full = str(row.get("full_name") or mapped.get("full_name") or "").lower()

    score = 0
    matched: list[str] = []
    excluded: list[str] = []
    matched_capability = ""
    matched_examples: list[str] = []

    for token in tokens:
        # not_owns 是软边界：命中“不负责”的词做降权而不是整仓排除。多域需求
        # 经常同时提到“用户登录”和“库存锁定”，不能因为库存仓不负责登录就把
        # 它整体排除；只有当正向得分为 0 时它才会因低于阈值落选。
        if len(token) >= 3 and token in not_owns:
            if token not in excluded:
                excluded.append(token)
                score -= NOT_OWNS_PENALTY
            continue
        hit = 0
        for values, weight in ((keywords, KEYWORD_WEIGHT),
                               (domains, DOMAIN_WEIGHT),
                               (interfaces, INTERFACE_WEIGHT)):
            s, v = _value_hit(token, values)
            if s:
                hit = max(hit, s * weight)
                if v not in matched:
                    matched.append(v)
        if token in purpose:
            hit = max(hit, PURPOSE_HIT)
            if token not in matched:
                matched.append(token)
        # 需求文本直接点名仓库（owner/repo 或 repo 名）时强命中。
        if token in short or (len(short) >= 2 and short in token) or token in full:
            hit = max(hit, REPO_NAME_HIT)
            if key not in matched:
                matched.append(key)
        score += hit

    # 能力卡判别：把 task_examples / not_for / owned_entities 纳入召回。
    # 目标不是「关键词出现过」，而是「这个任务像不像该仓的核心能力」。
    best_cap_score = 0
    best_cap_examples: list[str] = []
    best_cap_excluded: list[str] = []
    for fn in core:
        if not isinstance(fn, dict):
            continue
        terms = ([fn.get("name")] + list(fn.get("keywords") or [])
                 + list(fn.get("owned_entities") or []))
        cap_score = 0
        cap_terms: list[str] = []
        cap_examples: list[str] = []
        cap_excluded: list[str] = []
        for token in tokens:
            s, v = _value_hit(token, terms)
            if s:
                cap_score += s * CAP_MATCH_WEIGHT
                cap_terms.append(v)
        for example in (fn.get("task_examples") or []):
            hit = _phrase_hit(tokens, example)
            if hit:
                cap_score += hit
                cap_examples.append(example)
        for anti in (fn.get("not_for") or []):
            hit = _phrase_hit(tokens, anti)
            if hit:
                cap_score -= hit
                cap_excluded.append(anti)
        if cap_score > best_cap_score:
            best_cap_score = cap_score
            matched_capability = str(fn.get("name") or "")
            best_cap_examples = cap_examples[:MATCHED_EXAMPLES_MAX]
            best_cap_excluded = cap_excluded[:MATCHED_EXAMPLES_MAX]
            for v in cap_terms:
                if v not in matched:
                    matched.append(v)
    score += best_cap_score
    for example in best_cap_examples:
        if example not in matched_examples:
            matched_examples.append(example)
    for anti in best_cap_excluded:
        if anti not in excluded:
            excluded.append(anti)

    for token in tokens:
        s, v = _value_hit(token, routing.get("include_keywords") or [])
        if s:
            score += s * ROUTE_INCLUDE_WEIGHT
            if v not in matched:
                matched.append(v)
        s, v = _value_hit(token, routing.get("exclude_keywords") or [])
        if s:
            score -= s * ROUTE_EXCLUDE_WEIGHT
            if v not in excluded:
                excluded.append(v)
    for example in (routing.get("typical_requests") or []):
        hit = _phrase_hit(tokens, example)
        if hit:
            score += hit // TYPICAL_REQUEST_DIV
            if example not in matched_examples:
                matched_examples.append(example)

    if score < min_score:
        return None
    reason = "命中：" + ", ".join(dict.fromkeys(matched))[:REASON_MATCH_MAX]
    if matched_capability:
        reason += f"；最匹配能力：{matched_capability}"[:REASON_CAPABILITY_MAX]
    if excluded:
        reason += "；排除项降权：" + ", ".join(
            dict.fromkeys(excluded))[:REASON_EXCLUDED_MAX]
    return {
        "repo_key": key,
        "full_name": row.get("full_name") or mapped.get("full_name") or "",
        "score": score,
        "reason": reason,
        "confidence": round(
            min(CONFIDENCE_MAX, CONFIDENCE_BASE + score / CONFIDENCE_SCORE_DIV), 2),
        "purpose": mapped.get("purpose") or "",
        "matched": list(dict.fromkeys(matched)),
        "matched_capability": matched_capability,
        "task_examples": matched_examples[:MATCHED_EXAMPLES_MAX],
        "core_functions": [str(x.get("name") or "") for x in core
                           if isinstance(x, dict) and str(x.get("name") or "")]
                          [:CORE_FUNCTIONS_OUT_MAX],
    }