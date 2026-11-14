"""配置规范化与内容指纹（D109 §8「启动扫描」）。

判定「文件是否更新了」的唯一依据：**规范化内容 hash，不用 mtime**。
导入写入与启动比对必须共用本模块——两边算法漂移会导致永远判『更新』、每次启动全量覆盖。
"""
from __future__ import annotations

import hashlib
import json
from typing import Any

DOC_KEY_PREFIX = "_"  # _readme / _fields / _note / _presentation_fields 等文档键


def strip_doc_keys(value: Any) -> Any:
    """递归剥离 `_` 前缀文档键（凭据本就不在配置文件里，无需特判）。"""
    if isinstance(value, dict):
        return {
            k: strip_doc_keys(v)
            for k, v in value.items()
            if not (isinstance(k, str) and k.startswith(DOC_KEY_PREFIX))
        }
    if isinstance(value, (list, tuple)):
        return [strip_doc_keys(v) for v in value]
    return value


def canonical_json(payload: Any) -> str:
    """稳定序列化：键排序 + 紧凑分隔 + 保留中文（不转义）。"""
    return json.dumps(
        strip_doc_keys(payload),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def canonical_hash(payload: Any) -> str:
    """行级内容指纹（sha256 十六进制）。同一 provider 一个对象 → 一个 hash。"""
    return hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()