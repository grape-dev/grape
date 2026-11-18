"""Provider report / task-env failure signalling anchors.

Two silent-failure paths are pinned here, both of which used to report success
while nothing had been stored or had no diagnostics at all:

- ``cloud_worker._report_providers_once``: the API client folds 5xx/timeout into
  an empty dict, so an empty report response was logged as
  ``sandbox provider reported: provider_id= templates=0 engines_detected=0`` and
  never counted as a failure. The probe retry loop therefore exited after the
  first round and the platform kept zero templates/engine bindings.
- ``runtime_task_env``: a credential read failure only logged a WARNING and
  returned an empty env, which the worker reported as "no LLM credentials,
  config skipped" and the engine finally turned into "No LLM provider
  configured" -- unreadable for a user who did configure credentials.

Both are source anchors: the code lives inside a long-lived coroutine and a
FastAPI route respectively, and the assertions below pin the exact branch shape
that must survive future refactors.
"""
from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
WORKER = ROOT / "src" / "domains" / "runtime" / "engine" / "cloud_worker.py"
ROUTES = ROOT / "src" / "domains" / "runtime" / "routes" / "runtime.py"


def _worker_src() -> str:
    return WORKER.read_text(encoding="utf-8")


def _routes_src() -> str:
    return ROUTES.read_text(encoding="utf-8")


def _report_branch(src: str) -> str:
    """The slice from the report call to the end of its except branch."""
    start = src.find("_rep = await client.sandbox_provider_report(")
    assert start != -1, "cloud_worker 不再调用 sandbox_provider_report"
    end = src.find("return _failed", start)
    assert end != -1, "report 分支缺少 return _failed"
    return src[start:end]


def test_empty_report_response_is_counted_as_pending():
    seg = _report_branch(_worker_src())
    assert "if _rep:" in seg, "空响应必须与成功分支分开处理（此前一律打成功日志）"
    assert "returned an empty response" in seg, "空响应缺少显式 WARNING"
    empty_side = seg[seg.find("else:"):]
    assert "_failed += 1" in empty_side, "空响应必须计入 pending（否则重试循环首轮即退出）"


def test_report_exception_is_counted_as_pending():
    seg = _report_branch(_worker_src())
    except_side = seg[seg.find("except Exception as _re:"):]
    assert "_failed += 1" in except_side, "上报异常必须计入 pending，不能只打日志"


def test_task_env_failure_returns_explicit_missing_marker():
    src = _routes_src()
    idx = src.find("task_env: env assembly failed")
    assert idx != -1, "task_env 的 LLM 凭据异常分支缺失"
    tail = src[idx:]
    branch = tail[: tail.find("return {\"env\": env}")]
    assert '"llm_cred_missing": True' in branch, \
        "凭据读取失败必须返回显式 llm_cred_missing（不能再回空 env）"
    assert '"llm_cred_source": "env_error"' in branch, \
        "失败来源需与「用户确实没配凭据」区分（env_error，而非 user/repo/error）"
    assert "任务环境准备失败" in branch, "提示需中性描述（本块涵盖取任务/场景/凭据多步）"
    assert "_e}" not in branch.split("llm_cred_hint")[1], \
        "用户可见提示不得回显原始异常（可能含库名/表名）"
