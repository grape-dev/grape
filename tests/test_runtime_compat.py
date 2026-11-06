"""src.runtime.* 兼容别名必须一直可导入（orgtest worker 每个任务都会走到）。"""
from __future__ import annotations


def test_message_report_alias():
    from src.runtime.message_report import (  # noqa: F401
        build_msg_entry_id,
        degrade_failed_batch,
        truncate_message,
    )


def test_client_alias():
    from src.runtime.client import ReportBatchError  # noqa: F401


def test_health_alias():
    from src.runtime.health import aggregate_skill_consistency, parse_dt  # noqa: F401


def test_engine_select_alias():
    from src.runtime.engine_select import node_matches_agent_role  # noqa: F401


def test_task_log_stream_alias():
    from src.runtime.task_log_stream import build_skill_source_message  # noqa: F401


def test_os_adapter_alias():
    from src.runtime.os_adapter import engine_skill_dirs  # noqa: F401