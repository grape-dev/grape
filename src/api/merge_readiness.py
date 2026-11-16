"""Issue #2324：统一合并判定（mergeable && can_merge_check && approval_reviewers_required_passed）。

背景：GitCode `pulls get` 中 ``mergeable=true`` 与 ``can_merge_check=false`` 可并存
（必需评审人未满足时 mergeable 仍为 true），只读 mergeable 会误报「可合并」。
本模块提供**唯一判定实现**，供两条路径共用，避免口径漂移：

1. QA hit 判定：``get_pull`` 响应附加 ``merge_readiness``；
2. 自动合入前置检查：``merge_pull`` 路由复用同一函数。

字段层级（GitCode v5 实测，Issue #2324）：

- 顶层 ``mergeable`` / 顶层 ``can_merge_check``；
- ``mergeable_state.approval_reviewers_required_passed``；
- ``mergeable_state.merge_request_switch.approval_required_reviewers_count``
  （≥1 时才纳入评审人校验）；
- 顶层 ``approval_reviewers``（空数组 = 未指定评审人）。

None/缺失字段显式视为不可合入并输出告警，禁止静默退化为「只看 mergeable」。
GitHub 的 ``mergeable_state`` 是字符串枚举，schema 不同，暂不在本判定范围（返回 unknown，
合入路径按原流程继续，不误伤）。
"""
from __future__ import annotations

READY = "ready"
PENDING_REVIEW = "pending_review"
NOT_MERGEABLE = "not_mergeable"
UNKNOWN = "unknown"

_LABEL_READY = "可合入"
_LABEL_BLOCKED = "待评审/不可合入"
_LABEL_UNKNOWN = "无法判定"

_SUPPORTED_PROVIDER = "gitcode"
_MISSING_MERGEABLE_CODES = {"mergeable", "can_merge_check"}


def _empty(warnings: list[str]) -> dict:
    return {"ready": False, "verdict": UNKNOWN, "label": _LABEL_UNKNOWN, "summary": "",
            "missing": [], "warnings": list(warnings), "fields": {}}


def _item(code: str, message: str, *, warning: bool = False) -> dict:
    """构造缺失项；``warning`` 显式标记是否需要进入告警，不再靠文案子串反推。"""
    return {"code": code, "message": message, "warning": warning}


def _bool_missing(code: str, value, value_false_message: str) -> dict:
    if value is None:
        return _item(code, f"{code} 字段缺失（无法确认合并条件）", warning=True)
    return _item(code, value_false_message)


def evaluate_merge_readiness(detail, provider: str = _SUPPORTED_PROVIDER) -> dict:
    """组合判定 PR 合并就绪度，返回 ``{ready, verdict, label, summary, missing, warnings, fields}``。

    ``verdict``：
    - ``ready``：mergeable/can_merge_check/reviewers 校验全部满足；
    - ``not_mergeable``：mergeable 或 can_merge_check 不满足（含缺失）；
    - ``pending_review``：前两项满足但评审人要求未满足/未指定；
    - ``unknown``：provider 未覆盖或详情不可用（调用方按原流程 fail-open，不阻断）。
      gitcode 下详情为 dict 但关键字段全缺失时**不**走此分支，而是判 ``not_mergeable`` 并告警。
    """
    if not provider or str(provider).lower() != _SUPPORTED_PROVIDER:
        return _empty([f"provider={provider} 不在合并判定覆盖范围（仅 {_SUPPORTED_PROVIDER}）"])
    if not isinstance(detail, dict):
        return _empty(["PR 详情不可用，无法判定合并就绪度"])

    _state = detail.get("mergeable_state")
    _state = _state if isinstance(_state, dict) else {}
    _switch = _state.get("merge_request_switch")
    _switch = _switch if isinstance(_switch, dict) else {}

    _fields = {
        "mergeable": detail.get("mergeable"),
        "can_merge_check": detail.get("can_merge_check"),
        "approval_reviewers_required_passed": _state.get(
            "approval_reviewers_required_passed"),
        "approval_required_reviewers_count": _switch.get(
            "approval_required_reviewers_count"),
    }

    _missing: list[dict] = []
    _warnings: list[str] = []

    if _fields["mergeable"] is not True:
        _missing.append(_bool_missing(
            "mergeable", _fields["mergeable"],
            "mergeable=false（PR 不可合并，存在冲突或未满足合并条件）"))
    if _fields["can_merge_check"] is not True:
        _missing.append(_bool_missing(
            "can_merge_check", _fields["can_merge_check"],
            "can_merge_check 未通过（合并前检查未完成）"))

    _count = _fields["approval_required_reviewers_count"]
    _count_int = None
    if _count is None:
        _missing.append(_item(
            "approval_required_reviewers_count",
            "approval_required_reviewers_count 字段缺失（无法确认仓库评审人要求）",
            warning=True))
    elif isinstance(_count, bool) or not isinstance(_count, int):
        _missing.append(_item(
            "approval_required_reviewers_count",
            f"approval_required_reviewers_count 取值非法：{_count!r}（应为整数）",
            warning=True))
    else:
        _count_int = _count

    if _count_int is not None and _count_int >= 1:
        _reviewers_passed = _fields["approval_reviewers_required_passed"]
        if _reviewers_passed is not True:
            _missing.append(_bool_missing(
                "approval_reviewers_required_passed", _reviewers_passed,
                "approval_reviewers_required_passed 未通过（必需评审人未满足）"))

        _reviewers = detail.get("approval_reviewers")
        if not isinstance(_reviewers, list):
            _missing.append(_item(
                "approval_reviewers",
                "approval_reviewers 字段缺失（无法确认已指定评审人）",
                warning=True))
        elif not _reviewers:
            _missing.append(_item(
                "reviewers_not_assigned",
                "approval_reviewers 为空（待指定评审人）"))

    for _it in _missing:
        if _it.get("warning"):
            _warnings.append(_it["message"])

    _fields["approval_reviewers"] = detail.get("approval_reviewers")

    if not _missing:
        return {
            "ready": True,
            "verdict": READY,
            "label": _LABEL_READY,
            "summary": "可合入（mergeable && can_merge_check && 评审人要求已满足）",
            "missing": [],
            "warnings": _warnings,
            "fields": _fields,
        }

    _verdict = NOT_MERGEABLE if any(
        _it["code"] in _MISSING_MERGEABLE_CODES for _it in _missing) else PENDING_REVIEW
    _messages = "；".join(_it["message"] for _it in _missing)
    return {
        "ready": False,
        "verdict": _verdict,
        "label": _LABEL_BLOCKED,
        "summary": f"{_LABEL_BLOCKED}：{_messages}",
        "missing": _missing,
        "warnings": _warnings,
        "fields": _fields,
    }
