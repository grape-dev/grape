"""结构化失败归因（Issue #3433）。

事件异常中断（exit_code<0、非零退出、未捕获异常、被 kill）时，终态只保留中途截断
日志，用户无法判断失败原因与是否可重试。本模块统一构造结构化失败原因：

    {"error_type": "...", "phase": "...", "phase_label": "...",
     "message": "...", "runtime": "...", "agent": "...", "exit_code": -1}

事件收尾入口（event_store.update_event_complete / update_event_status）在 status=failed
且调用方未提供有效归因时按 result / exit_code 自动兜底，保证任何中断路径都有可读原因。
成功（completed）与取消（cancelled）不写该字段，既有展示与归档结构不变。
"""
from __future__ import annotations

import json
import os


def _env_int(name: str, default: int) -> int:
    """可选整数环境变量：缺省/非法返回默认值（不静默吞非法输入，只回退并告警）。"""
    raw = (os.environ.get(name) or "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        import logging
        logging.getLogger("devops-bot").warning(
            "invalid %s %r, fallback to %d", name, raw, default)
        return default


# 截断上限：按「异常类型/阶段名短、归因摘要长」的可读量级估算，无 spec 数值来源，
# 按 L2 登记为待测量项；部署侧可用下列 env 覆盖，未配置时保持量级默认。
MAX_ERROR_TYPE = _env_int("GRAPE_FAILURE_INFO_MAX_ERROR_TYPE", 120)
MAX_PHASE = _env_int("GRAPE_FAILURE_INFO_MAX_PHASE", 40)
MAX_MESSAGE = _env_int("GRAPE_FAILURE_INFO_MAX_MESSAGE", 800)
MAX_ACTOR = _env_int("GRAPE_FAILURE_INFO_MAX_ACTOR", 200)

PHASE_LABELS = {
    "dispatch": "派发阶段",
    "prompt": "提示词构建阶段",
    "execution": "执行阶段",
    "exception": "派发异常",
    "stream": "结果收尾阶段",
    "timeout": "执行超时",
    "terminated": "进程被终止",
    "completion_gate": "完成校验阶段",
    "recover": "孤儿任务回收",
    "cancelled": "人工终止",
    "unknown": "未知阶段",
}

DEFAULT_ERROR_TYPE = "UnclassifiedFailure"
DEFAULT_MESSAGE = "任务异常中断，未捕获到原始报错"

_TIMEOUT_HINTS = ("timeout", "timed out", "超时", "returncode=124", "stopreason=timeout")
_TERMINATED_HINTS = ("sigkill", "sigterm", "killed", "被 kill", "被kill", "terminated",
                     "terminate", "shutdown", "进程被", "强制中止")
_CANCELLED_HINTS = ("用户手动终止", "人工终止", "cancelled", "canceled")
_DISPATCH_HINTS = ("dispatch", "派发", "入队", "no available engine", "engine offline")


def _clip(value, limit: int) -> str:
    text = str(value or "").strip()
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 1)] + "…"


def classify_phase(exit_code=None, hint: str = "") -> str:
    """按退出码与报错文本推断失败阶段（仅在调用方未显式指定时使用）。"""
    text = str(hint or "").lower()
    if any(k in text for k in _TIMEOUT_HINTS):
        return "timeout"
    if any(k in text for k in _TERMINATED_HINTS):
        return "terminated"
    if any(k in text for k in _CANCELLED_HINTS):
        return "cancelled"
    try:
        code = int(exit_code) if exit_code is not None else None
    except (TypeError, ValueError):
        code = None
    if code == 124:
        return "timeout"
    if code is not None and code < 0:
        return "terminated"
    if any(k in text for k in _DISPATCH_HINTS):
        return "dispatch"
    return "execution"


def infer_error_type(exit_code=None, phase: str = "", hint: str = "") -> str:
    """推断异常类型标签（调用方显式提供时优先，此处仅兜底）。"""
    if phase == "timeout":
        return "TimeoutError"
    if phase == "terminated":
        return "ProcessKilled"
    if phase == "cancelled":
        return "CancelledByUser"
    if phase == "dispatch":
        return "DispatchError"
    text = str(hint or "").lower()
    if "timeout" in text or "超时" in text:
        return "TimeoutError"
    try:
        code = int(exit_code) if exit_code is not None else None
    except (TypeError, ValueError):
        code = None
    if code is not None and code != 0:
        return "ProcessExitError"
    return DEFAULT_ERROR_TYPE


def build_failure_info(*, error_type: str = "", phase: str = "", message: str = "",
                       runtime: str = "", agent: str = "", exit_code=None,
                       phase_label: str = "") -> dict:
    """构造结构化失败原因；字段统一截断，便于落库与前端分字段展示。"""
    _phase = _clip(phase, MAX_PHASE) or "unknown"
    info = {
        "error_type": _clip(error_type, MAX_ERROR_TYPE) or DEFAULT_ERROR_TYPE,
        "phase": _phase,
        "phase_label": _clip(phase_label, MAX_PHASE) or PHASE_LABELS.get(_phase, _phase),
        "message": _clip(message, MAX_MESSAGE) or DEFAULT_MESSAGE,
        "runtime": _clip(runtime, MAX_ACTOR),
        "agent": _clip(agent, MAX_ACTOR),
    }
    if exit_code is not None:
        info["exit_code"] = exit_code
    return info


def parse_failure_info(raw) -> dict:
    """把 dict / JSON 字符串归一为结构化失败原因；无效输入返回空 dict。"""
    if not raw:
        return {}
    data = raw
    if isinstance(raw, str):
        try:
            data = json.loads(raw)
        except (TypeError, ValueError):
            return {}
    if not isinstance(data, dict):
        return {}
    out = {}
    for key in ("error_type", "phase", "phase_label", "message", "runtime", "agent"):
        val = data.get(key)
        if val is not None and str(val).strip():
            out[key] = _clip(val, MAX_MESSAGE if key == "message" else MAX_ACTOR)
    if "exit_code" in data and data.get("exit_code") is not None:
        out["exit_code"] = data.get("exit_code")
    return out


def finalize_failure_info(raw, *, result: str = "", exit_code=None,
                          agent: str = "", runtime: str = "") -> dict:
    """补齐调用方未提供的字段，保证返回的失败原因非空可读。

    - message 缺失 → 回退事件 result；再缺失 → 通用中断文案；
    - phase 缺失 → 按 exit_code 与 message 推断；
    - error_type 缺失 → 按 phase 推断；
    - runtime / agent 缺失 → 用事件行里的值回填。
    """
    info = parse_failure_info(raw)
    message = info.get("message") or _clip(result, MAX_MESSAGE)
    phase = info.get("phase") or classify_phase(exit_code, message)
    error_type = info.get("error_type") or infer_error_type(exit_code, phase, message)
    return build_failure_info(
        error_type=error_type,
        phase=phase,
        phase_label=info.get("phase_label", ""),
        message=message or DEFAULT_MESSAGE,
        runtime=info.get("runtime") or runtime or "",
        agent=info.get("agent") or agent or "",
        exit_code=info.get("exit_code", exit_code),
    )


def fallback_failure_info(result: str = "", exit_code=None, agent: str = "",
                          runtime: str = "", phase: str = "") -> dict:
    """按事件 result / exit_code 生成兜底失败原因（统一收尾入口使用）。"""
    text = str(result or "").strip()
    _phase = phase or classify_phase(exit_code, text)
    return build_failure_info(
        error_type=infer_error_type(exit_code, _phase, text),
        phase=_phase,
        message=text or DEFAULT_MESSAGE,
        runtime=runtime,
        agent=agent,
        exit_code=exit_code,
    )


def failure_info_for_exit(errors=None, result: str = "", exit_code=None,
                          fake_completion: bool = False) -> dict:
    """执行流结束失败时构造归因：区分假完成 / 超时 / 被 kill / 普通非零退出。"""
    parts = []
    for e in (errors or []):
        text = str(e or "").strip()
        if text:
            parts.append(text[:300])
    message = str(result or "").strip() or "；".join(parts[:3])
    if fake_completion:
        return build_failure_info(
            error_type="FakeCompletion", phase="completion_gate",
            message=message or "引擎正常退出但无可信产出，判定为未完成",
            exit_code=exit_code)
    phase = classify_phase(exit_code, message)
    return build_failure_info(
        error_type=infer_error_type(exit_code, phase, message),
        phase=phase,
        message=message or f"任务失败（exit_code={exit_code}）",
        exit_code=exit_code)


def failure_info_for_stream(status: str, errors=None, result: str = "",
                            exit_code=None, fake_completion: bool = False):
    """执行流收尾按终态判定归因：非 failed（completed/cancelled）返回 None。

    direct / pipeline 两条执行链的终态收尾块共用，避免各自重复「status==failed
    才构造」的判定与调用。
    """
    if status != "failed":
        return None
    return failure_info_for_exit(
        errors, result, exit_code, fake_completion=fake_completion)


def failure_info_from_exception(exc, tb: str = "", *, phase: str = "execution",
                                exit_code=1) -> dict:
    """顶层未捕获异常兜底归因（direct / pipeline 崩溃兜底共用）。"""
    return build_failure_info(
        error_type=type(exc).__name__,
        phase=phase,
        message=f"{exc}\n{(tb or '')[-600:]}",
        exit_code=exit_code)


def failure_info_for_cascade(*, task_status: str = "", result: str = "",
                             exit_code=None, phase: str = "", agent: str = "",
                             runtime: str = "") -> dict:
    """级联收敛 / 孤儿回收判死的事件归因（timeout / recover）。

    - ``timeout_failed`` 任务恒判 ``timeout``；
    - 调用方显式 phase 优先（如孤儿 sweeper 传 ``recover``）；
    - 其余普通失败无超时/终止线索时归 ``recover``（回收判死，而非执行阶段异常）。
    """
    _task_status = (task_status or "").strip()
    _phase = phase or ("timeout" if _task_status == "timeout_failed" else "")
    if not _phase:
        _phase = classify_phase(exit_code, result)
        if _phase in ("execution", "unknown"):
            _phase = "recover"
    return build_failure_info(
        error_type=infer_error_type(exit_code, _phase, result),
        phase=_phase,
        message=result,
        runtime=runtime,
        agent=agent,
        exit_code=exit_code)


def failure_info_json(info: dict) -> str:
    return json.dumps(info or {}, ensure_ascii=False)
