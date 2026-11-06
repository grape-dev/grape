"""迁移脚本 stdout 必须变成带级别字段的结构化日志（避免 Grafana 里 level=unknown）。"""
from __future__ import annotations

import logging

from src.api.server_tasks import _StructuredScriptOutput
from src.kernel.config import log


class _Collect(logging.Handler):
    def __init__(self):
        super().__init__()
        self.records = []

    def emit(self, record):
        self.records.append(record)


def _run(lines):
    handler = _Collect()
    old_level = log.level
    log.setLevel(logging.INFO)
    log.addHandler(handler)
    try:
        out = _StructuredScriptOutput()
        for line in lines:
            out.write(line + "\n")
        out.flush()
    finally:
        log.removeHandler(handler)
        log.setLevel(old_level)
    return handler.records


def test_plain_prints_become_info_records():
    recs = _run(["[阶段 3] 撞 ID 错位检测: 0 错位"])
    assert len(recs) == 1
    assert recs[0].levelno == logging.INFO
    assert "[阶段 3]" in recs[0].getMessage()


def test_collision_lines_become_warnings():
    recs = _run(["  [COLLISION] audit_logs.user_id=65 count=1 应属 t1_0d18ae12"])
    assert len(recs) == 1
    assert recs[0].levelno == logging.WARNING
    assert "[COLLISION]" in recs[0].getMessage()


def test_partial_writes_are_buffered_until_newline():
    out = _StructuredScriptOutput()
    handler = _Collect()
    log.addHandler(handler)
    log.setLevel(logging.INFO)
    try:
        out.write("  [ORPHAN] user_repos.user_id: id=341")
        assert handler.records == []
        out.write(" count=1\n")
    finally:
        log.removeHandler(handler)
    assert len(handler.records) == 1
    assert "[ORPHAN]" in handler.records[0].getMessage()
    assert handler.records[0].levelno == logging.WARNING