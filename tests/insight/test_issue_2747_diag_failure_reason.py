"""#2747 QA 第三轮：任务诊断条目不得冒充「失败原因」（trace / 事件详情两条展示链）。

#2790 的 token 诊断（diagnostic=True）落在 agent_tasks.errors 里，与真实失败原因共用
同一个字段。上一轮只修了 friendly_failure_hint（chat 提示），可观测性时间线
（trace.failure_reason）与事件详情 task_errors 仍直接吃原始 errors JSON →
诊断字符串抢在真实原因前面展示，前端看到的是「session token issue failed」而不是
引擎为什么挂掉。

口径与 `_strip_task_diagnostics` 一致：drop diagnostic=True 的条目，其余原样保留；
非 JSON / 非 list 的历史值原样透传。
"""
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from src.domains.insight.observability.trace import _task_item  # noqa: E402
from src.domains.runtime.engine.service import _token_diag_entry  # noqa: E402
from src.domains.runtime.engine.service import SESSION_TOKEN_ISSUE_FAILED  # noqa: E402

DIAG = _token_diag_entry(SESSION_TOKEN_ISSUE_FAILED, "signer boom")


def _row(**kw):
    base = {"id": "t_x", "status": "failed", "runtime_id": 0, "created_at": "",
            "started_at": "", "completed_at": "", "errors": "", "stderr": "",
            "result_text": "", "repo_key": "gitcode:o/r", "issue_id": "", "event_seq": 0}
    base.update(kw)
    return base


def test_trace_failure_reason_skips_diagnostic_only_errors():
    """只有诊断条目时，failure_reason 回退到真实 stderr/result_text，不显示 token 诊断。"""
    row = _row(errors=json.dumps([DIAG], ensure_ascii=False),
               result_text="engine exit_code=127: opencode not found")
    item = _task_item(row, {}, [], False)
    assert "engine exit_code=127" in item["failure_reason"], item["failure_reason"]
    assert "session token" not in item["failure_reason"].lower(), item["failure_reason"]


def test_trace_failure_reason_keeps_real_error_next_to_diagnostic():
    """诊断 + 真实错误并存：展示真实错误，不把诊断串混进去。"""
    real = {"error_code": "REAL_FAIL", "reason": "db down"}
    row = _row(errors=json.dumps([DIAG, real], ensure_ascii=False))
    item = _task_item(row, {}, [], False)
    assert "db down" in item["failure_reason"], item["failure_reason"]
    assert "session token" not in item["failure_reason"].lower(), item["failure_reason"]


def test_trace_failure_reason_keeps_non_json_errors():
    """历史非 JSON errors（纯文本）原样透传，不能被探针吃掉。"""
    row = _row(errors="boom: legacy plain text")
    item = _task_item(row, {}, [], False)
    assert "boom: legacy plain text" in item["failure_reason"]


def test_summarize_task_errors_drops_diagnostics():
    """事件详情 task_errors 同一口径：诊断条目不进失败归因摘要。"""
    from src.domains.runtime.public import summarize_task_errors

    raw = json.dumps([DIAG, {"error_code": "REAL_FAIL", "reason": "db down"}],
                     ensure_ascii=False)
    out = summarize_task_errors(raw)
    joined = " ".join(out)
    assert "db down" in joined, out
    assert "session token" not in joined.lower(), out
    assert all(isinstance(x, str) for x in out)


def test_summarize_task_errors_passthrough_and_limit():
    from src.domains.runtime.public import summarize_task_errors

    assert summarize_task_errors("") == []
    assert summarize_task_errors("plain text failure") == ["plain text failure"]
    many = json.dumps([{"reason": f"e{i}"} for i in range(9)], ensure_ascii=False)
    assert len(summarize_task_errors(many, limit=5)) == 5


def test_timeline_failure_reason_uses_shared_strip():
    """静态守卫：trace 不在本地复写过滤逻辑，复用 runtime 域公共原语。"""
    import src.domains.insight.observability.trace as trace_mod
    src = open(trace_mod.__file__, encoding="utf-8").read()
    assert "strip_task_diagnostics" in src, "trace 必须复用统一过滤口径"