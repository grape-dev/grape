"""任务类 gauge 必须在任务清空后归零（否则「队列/执行/卡住」面板长期显示旧值）。

现场：agent_tasks 里已无 running 任务，/metrics 里 grape_task_running{source="vod"} 仍是 1。
旧实现只遍历"本轮查询到的 source"，集合为空时不会写 0，gauge 永远停在最后一次的值。
"""
import os
import sys

ROOT = os.path.join(os.path.dirname(__file__), "..", "..")
sys.path.insert(0, ROOT)

from src.domains.insight.observability import (  # noqa: E402
    SAMPLER_TASK_QUEUED,
    SAMPLER_TASK_RUNNING,
    SAMPLER_TASK_STUCK,
    _apply_source_gauges,
)


def _val(gauge, source):
    return gauge.labels(source=source)._value.get()


def test_gauges_zero_when_all_tasks_finish():
    _apply_source_gauges({"vod": 2}, {"vod": 1}, {})
    assert _val(SAMPLER_TASK_QUEUED, "vod") == 2
    assert _val(SAMPLER_TASK_RUNNING, "vod") == 1

    # 任务全部结束：本轮三个查询都为空，必须把 ved 归零而不是保持旧值
    _apply_source_gauges({}, {}, {})
    assert _val(SAMPLER_TASK_QUEUED, "vod") == 0
    assert _val(SAMPLER_TASK_RUNNING, "vod") == 0
    assert _val(SAMPLER_TASK_STUCK, "vod") == 0


def test_new_source_tracked_without_resetting_others():
    _apply_source_gauges({"issue": 3}, {}, {"issue": 1})
    _apply_source_gauges({"vod": 1}, {}, {})
    assert _val(SAMPLER_TASK_QUEUED, "issue") == 0
    assert _val(SAMPLER_TASK_QUEUED, "vod") == 1
    assert _val(SAMPLER_TASK_STUCK, "issue") == 0