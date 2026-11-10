"""A2A 折叠任务 context 契约守卫（Issue #1806）。

sweeper-v2 折叠任务使用形如 ``sweeper-v2-pr754-fold620`` 的 idempotency_key
派发；折叠方向（源 PR → 目标 PR、落点文件、折叠原因）必须随 payload.context
显式携带，避免承接方从幂等键反推落点。

单源约定：折叠任务判定、四要素校验、错误体构造全部集中在本模块，端点与测试
共用，禁止调用方各自拼正则。上下文既支持文本行（``source_pr: 754``）也支持
结构化 dict / JSON 字符串。
"""
from __future__ import annotations

import json
import os
import re

FOLD_TASK_IDEMPOTENCY_PATTERN = r"^sweeper-v2-(?P<source>.+)-fold(?P<target>\d+)$"

FOLD_CONTEXT_ENFORCE_ENV = "GRAPE_A2A_FOLD_CONTEXT_ENFORCE"
FOLD_CONTEXT_REQUIRED_FIELDS = ("source_pr", "target_pr", "target_files", "fold_reason")
FOLD_CONTEXT_SCHEMA_EXAMPLE = (
    "source_pr: 754\n"
    "target_pr: #620\n"
    "target_files: src/api/routes/webhook.py\n"
    "fold_reason: 与目标 PR 修改同一文件且方向一致，追加到目标 PR"
)

_FOLD_TASK_RE = re.compile(FOLD_TASK_IDEMPOTENCY_PATTERN)
_FIELD_ALIASES = {
    "source_pr": ("source_pr", "source pr", "sourcepr", "源 pr", "源pr", "本单 pr", "本单pr"),
    "target_pr": ("target_pr", "target pr", "targetpr", "目标 pr", "目标pr"),
    "target_files": ("target_files", "target files", "targetfiles", "落点文件"),
    "fold_reason": ("fold_reason", "fold reason", "foldreason", "折叠原因"),
}
_ALIAS_TO_FIELD = {
    alias: field for field, aliases in _FIELD_ALIASES.items() for alias in aliases
}
_ALIAS_PATTERN = "|".join(
    re.escape(alias) for alias in sorted(_ALIAS_TO_FIELD, key=len, reverse=True))
_FIELD_START_RE = re.compile(
    rf"(?<!\w)(?P<key>{_ALIAS_PATTERN})\s*[:=：]\s*", re.IGNORECASE)
_TRAILING_SEPARATORS = ",，;；、"


def is_fold_task(idempotency_key: str) -> bool:
    """折叠任务唯一判定入口：``sweeper-v2-<任意>-fold<数字>``。"""
    return _FOLD_TASK_RE.match((idempotency_key or "").strip()) is not None


def _normalize_key(key: str) -> str:
    cleaned = str(key or "").strip().strip("-*#•` ").strip()
    return re.sub(r"\s+", " ", cleaned.lower())


def _scalar(value) -> str:
    """把结构化值归一为非空文本；空 dict / 空 list 及其中的空容器视为缺失。"""
    if isinstance(value, dict):
        if not value:
            return ""
        return json.dumps(value, ensure_ascii=False, sort_keys=True)
    if isinstance(value, (list, tuple, set)):
        parts = [_scalar(item) for item in value]
        return ", ".join(part for part in parts if part)
    return str(value if value is not None else "").strip()


def _assign_alias(result: dict, raw_key, value: str) -> None:
    """dict / 文本分支共用的 alias→首个非空值写入。"""
    field = _ALIAS_TO_FIELD.get(_normalize_key(raw_key))
    if field and value and not result.get(field):
        result[field] = value


def _text_fields(text: str):
    """逐行切出显式字段；支持同一行用逗号/分号分隔多个字段。"""
    for line in text.splitlines():
        starts = list(_FIELD_START_RE.finditer(line))
        for index, start in enumerate(starts):
            end = starts[index + 1].start() if index + 1 < len(starts) else len(line)
            value = line[start.end():end].strip().strip("`*").strip()
            value = value.rstrip(_TRAILING_SEPARATORS).strip()
            yield start.group("key"), value


def normalize_fold_context(context) -> dict:
    """把文本 / dict / JSON 字符串归一为 ``{四要素: 非空文本}``。"""
    if not context:
        return {}
    if isinstance(context, dict):
        result = {}
        for raw_key, raw_value in context.items():
            _assign_alias(result, raw_key, _scalar(raw_value))
        return result

    text = str(context).strip()
    if not text:
        return {}
    try:
        parsed = json.loads(text)
    except (TypeError, ValueError):
        parsed = None
    if isinstance(parsed, dict):
        return normalize_fold_context(parsed)

    result = {}
    for key, value in _text_fields(text):
        _assign_alias(result, key, value)
    return result


def missing_fold_context_fields(context) -> list:
    """返回去空白后仍缺失的折叠任务 context 四要素（按固定顺序）。"""
    normalized = normalize_fold_context(context)
    return [field for field in FOLD_CONTEXT_REQUIRED_FIELDS if not normalized.get(field)]


def fold_context_enforcement_enabled() -> bool:
    """默认硬拒绝；``GRAPE_A2A_FOLD_CONTEXT_ENFORCE=0/false/off/no`` 降级告警。"""
    raw = os.environ.get(FOLD_CONTEXT_ENFORCE_ENV, "1")
    return str(raw).strip().lower() not in ("0", "false", "off", "no")


def build_fold_context_error(idempotency_key: str, missing_fields: list) -> dict:
    """构造折叠任务 context 不完整的 400 错误体（含键、缺失清单、schema 示例）。"""
    return {
        "code": 400,
        "message": (
            f"fold task context incomplete: idempotency_key='{idempotency_key}' "
            f"missing={list(missing_fields)}; expected fields "
            f"{list(FOLD_CONTEXT_REQUIRED_FIELDS)}"
        ),
        "idempotency_key": idempotency_key,
        "missing_fields": list(missing_fields),
        "required_fields": list(FOLD_CONTEXT_REQUIRED_FIELDS),
        "context_schema_example": FOLD_CONTEXT_SCHEMA_EXAMPLE,
        "hint": (
            "sweeper-v2 折叠派发方需在 payload.context 显式写入四要素"
            "（源 PR / 目标 PR / 落点文件 / 折叠原因），承接方无需解析 idempotency_key"
        ),
    }