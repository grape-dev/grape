"""JSONL 调用审计：每次调用一行，凭据字段强制掩码（掩码规则统一出口 redact）。"""
import json
import os
from pathlib import Path

from .credentials import redact


def _redact_deep(value, secrets):
    """递归掩码：嵌套 dict/list/tuple 内的所有字符串值都过 redact（不只顶层）。"""
    if isinstance(value, str):
        return redact(value, secrets)
    if isinstance(value, dict):
        return {k: _redact_deep(v, secrets) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_redact_deep(v, secrets) for v in value]
    return value


class JsonlAuditWriter:
    """把审计记录追加写入 JSONL 文件（一调用一行）。

    明文防线：写盘前对 record 的所有字符串值（含嵌套结构）做 redact(secrets)。
    """

    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def append(self, record: dict, secrets=()) -> dict:
        redacted = _redact_deep(record, secrets)
        line = json.dumps(redacted, ensure_ascii=False, sort_keys=True)
        with open(self.path, "a", encoding="utf-8") as f:
            f.write(line + "\n")
            f.flush()
            os.fsync(f.fileno())
        return redacted
