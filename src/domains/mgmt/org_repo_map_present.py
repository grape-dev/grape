"""组织仓库地图：AI 结果的展示字段归一。

从 ``org_repo_map`` 拆出（架构审查 A8）：只做纯数据整形，不访问 DB、不参与打分。
调用方：``org_repo_map._render_ai_summary`` / ``get_map`` / ``_ai_status_map``。

阈值说明（B2）：下列截断上限来自当前 UI/提示词容量与历史实现，**没有独立基线**；
统一收口为常量，待召回/展示回归集补齐后再定值。
<undecided: 展示字段截断上限需要可用性与回归基线后确认>
"""
from __future__ import annotations

# <undecided: 能力卡字段容量，当前沿用历史实现，未做可用性基线>
CARD_FUNCTION_MAX = 6
NAME_MAX_CHARS = 80
DESCRIPTION_MAX_CHARS = 240
KEYWORDS_MAX = 12
TASK_EXAMPLES_MAX = 6
NOT_FOR_MAX = 5
OWNED_ENTITIES_MAX = 8
CHANGE_TYPES_MAX = 6
RESPONSIBILITY_ITEMS_MAX = 8
ROLE_MAX_CHARS = 300
OTHER_CAPABILITIES_MAX = 10
ROUTING_KEYWORDS_MAX = 20
ROUTING_TYPICAL_MAX = 10


def _text_list(value, limit: int = 0) -> list[str]:
    """把字符串/数组（含 {name, description} 对象）归一成去重的短文本列表。"""
    if isinstance(value, str):
        raw = [value]
    elif isinstance(value, (list, tuple)):
        raw = list(value)
    else:
        raw = []
    out: list[str] = []
    seen: set[str] = set()
    for item in raw:
        if isinstance(item, dict):
            text = str(item.get("name") or item.get("title")
                       or item.get("description") or "").strip()
        else:
            text = str(item or "").strip()
        if not text:
            continue
        key = text.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(text)
        if limit and len(out) >= limit:
            break
    return out


def _core_functions(result: dict) -> list[dict]:
    """核心功能：优先 schema v3，其次兼容 v2 capabilities / v1 features。

    除名称/描述外，保留路由判别字段：典型任务、不适用任务、拥有实体、变更类型。
    这些字段不进入人类可读正文，但决定「任务该派给哪个仓」。
    """
    out: list[dict] = []

    def _append(item, name: str, desc: str) -> None:
        if not name:
            return
        out.append({
            "name": name[:NAME_MAX_CHARS],
            "description": desc[:DESCRIPTION_MAX_CHARS],
            "keywords": _text_list(item.get("keywords"), limit=KEYWORDS_MAX)
                        if isinstance(item, dict) else [],
            "task_examples": _text_list(
                item.get("task_examples") or item.get("examples"),
                limit=TASK_EXAMPLES_MAX)
                             if isinstance(item, dict) else [],
            "not_for": _text_list(
                item.get("not_for") or item.get("anti_examples"),
                limit=NOT_FOR_MAX)
                       if isinstance(item, dict) else [],
            "owned_entities": _text_list(
                item.get("owned_entities") or item.get("entities"),
                limit=OWNED_ENTITIES_MAX)
                              if isinstance(item, dict) else [],
            "change_types": _text_list(item.get("change_types"),
                                       limit=CHANGE_TYPES_MAX)
                            if isinstance(item, dict) else [],
        })

    raw = result.get("core_functions")
    if isinstance(raw, list):
        for item in raw:
            if isinstance(item, dict):
                name = str(item.get("name") or item.get("title") or "").strip()
                desc = str(item.get("description") or item.get("summary") or "").strip()
            else:
                name, desc = str(item or "").strip(), ""
            _append(item, name, desc)
    if not out:
        for item in (result.get("capabilities") or []):
            if isinstance(item, dict):
                name = str(item.get("name") or item.get("title") or "").strip()
                desc = str(item.get("description") or item.get("summary") or "").strip()
                _append(item, name, desc)
    if not out:
        for name in _text_list(result.get("features"), limit=CARD_FUNCTION_MAX):
            _append({}, name, "")
    return out[:CARD_FUNCTION_MAX]


def _core_responsibilities(result: dict) -> dict:
    """核心职责：组织角色 + 负责 / 不负责边界；兼容 v1 只有 not_owns。"""
    raw = result.get("core_responsibilities")
    role = ""
    owns: list[str] = []
    not_owns: list[str] = []
    if isinstance(raw, dict):
        role = str(raw.get("role") or raw.get("summary") or "").strip()
        owns = _text_list(raw.get("owns") or raw.get("responsible_for"),
                          limit=RESPONSIBILITY_ITEMS_MAX)
        not_owns = _text_list(raw.get("not_owns") or raw.get("does_not_own"),
                              limit=RESPONSIBILITY_ITEMS_MAX)
    if not not_owns:
        not_owns = _text_list(result.get("not_owns"),
                              limit=RESPONSIBILITY_ITEMS_MAX)
    return {"role": role[:ROLE_MAX_CHARS], "owns": owns, "not_owns": not_owns}


def _other_capabilities(result: dict, core: list[dict]) -> list[str]:
    """其他能力的概括列表：只保留名称，不展开接口/文件。"""
    out = _text_list(result.get("other_capabilities"),
                     limit=OTHER_CAPABILITIES_MAX)
    if out:
        return out
    core_names = {str(x.get("name") or "").strip().lower() for x in core}
    out = []
    seen = set(core_names)
    for item in (result.get("capabilities") or []):
        if isinstance(item, dict):
            name = str(item.get("name") or item.get("title") or "").strip()
        else:
            name = str(item or "").strip()
        if not name or name.lower() in seen:
            continue
        seen.add(name.lower())
        out.append(name)
        if len(out) >= OTHER_CAPABILITIES_MAX:
            break
    if len(out) < OTHER_CAPABILITIES_MAX:
        for name in _text_list(result.get("features")):
            if name.lower() in seen:
                continue
            seen.add(name.lower())
            out.append(name)
            if len(out) >= OTHER_CAPABILITIES_MAX:
                break
    return out


def _routing_profile(result: dict) -> dict:
    """任务路由判别字段：正向/负向关键词与典型请求，供召回与下派使用。"""
    raw = result.get("routing")
    if not isinstance(raw, dict):
        raw = {}
    return {
        "include_keywords": _text_list(
            raw.get("include_keywords") or result.get("include_keywords"),
            limit=ROUTING_KEYWORDS_MAX),
        "exclude_keywords": _text_list(
            raw.get("exclude_keywords") or result.get("exclude_keywords"),
            limit=ROUTING_KEYWORDS_MAX),
        "typical_requests": _text_list(
            raw.get("typical_requests") or raw.get("task_examples"),
            limit=ROUTING_TYPICAL_MAX),
    }


def _presentation_fields(result: dict) -> dict:
    """面向前端/摘要的结构化展示字段（不含接口与文件路径）。"""
    if not isinstance(result, dict):
        return {}
    core = _core_functions(result)
    return {
        "core_functions": core,
        "core_responsibilities": _core_responsibilities(result),
        "other_capabilities": _other_capabilities(result, core),
        "routing": _routing_profile(result),
    }