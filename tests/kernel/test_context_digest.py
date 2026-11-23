"""高信号上下文摘要行为锁（src/kernel/context_digest.py）。

目标：上下文只保留结论/关键动作/验证证据/产物/风险/可复用经验，不再退化成
「任务是否完成」的状态行；同时按字段上限裁剪，避免上下文预算被日志挤爆。
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from src.kernel.context_digest import (  # noqa: E402
    build_digest,
    digest_has_content,
    render_compact,
    render_digest,
    render_reusable,
)

_OUTPUT = {
    "actions": [
        "修复 src/http_probe.py：新增数值参数校验，非法 timeout/backoff 返回 ok=False",
        "新增回归测试：负 timeout、非 ASCII URL 编码（修复前 6 failed）",
        "pytest -q：16 passed / 8 subtests",
        "Agent feedback 审视：事件间工作目录被重置需重复 clone，已提交反馈 #68",
        "待确认 Optional：CLI 退出码是否统一",
    ],
    "conclusion": "Required（异常白名单/参数校验/非 ASCII URL）已修复且先红后绿",
    "artifacts": [
        "PR #5 https://example.test/merge_requests/5",
        "分支 feat/issue-5 63d84ef..0dca6a6",
    ],
    "next_step": "等待用户确认合入",
    "issue_summary": "PR #5 修复完成 | 16 passed | 待确认合入",
}


def test_build_digest_extracts_high_signal_sections():
    digest = build_digest(_OUTPUT, agent_type="coding", status="completed",
                          task_id="t_1")
    assert "Required" in digest["conclusion"]
    assert any("修复 src/http_probe.py" in x for x in digest["changes"])
    assert any("16 passed" in x for x in digest["verification"])
    assert any("PR #5" in x for x in digest["artifacts"])
    assert digest["next_step"] == "等待用户确认合入"
    assert any("Optional" in x for x in digest["risks"])
    assert any("Agent feedback" in x for x in digest["reusable"])
    assert all("Agent feedback" not in x for x in digest["changes"])
    assert digest["agent_type"] == "coding"
    assert digest["task_id"] == "t_1"


def test_render_digest_contains_execution_memory_not_only_status():
    text = render_digest(build_digest(_OUTPUT, status="completed"), heading_level=3)
    for expected in ("**结论**", "**关键动作**", "**验证证据**",
                     "**产物**", "**风险与后续**"):
        assert expected in text
    assert "任务是否完成" not in text
    assert "16 passed" in text
    assert "PR #5" in text


def test_render_compact_prefers_issue_summary_and_caps_length():
    digest = build_digest(_OUTPUT)
    assert render_compact(digest).startswith("PR #5 修复完成")
    long_digest = build_digest({"issue_summary": "x" * 1000})
    assert len(render_compact(long_digest)) <= 300


def test_clips_long_items_and_limits_total():
    output = {
        "actions": [f"动作{i} " + "x" * 500 for i in range(20)],
        "conclusion": "c" * 5000,
        "artifacts": [f"产物{i}" for i in range(20)],
    }
    digest = build_digest(output)
    assert len(digest["conclusion"]) <= 1200
    assert all(len(x) <= 360 for x in digest["changes"] + digest["artifacts"])
    assert len(digest["changes"]) <= 8
    assert len(digest["artifacts"]) <= 8


def test_empty_output_is_not_written_as_fake_memory():
    digest = build_digest(None)
    assert digest_has_content(digest) is False
    assert render_digest(digest) == ""
    assert render_reusable(digest) == ""


def test_plain_text_fallback_and_done_next_step_not_risk():
    digest = build_digest({"conclusion": "部署完成", "next_step": "done"})
    assert digest["conclusion"] == "部署完成"
    assert all("done" not in x.lower() for x in digest["risks"])