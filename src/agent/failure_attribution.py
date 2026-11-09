"""失败归因文案构造（Issue #494：超时类失败必须有明确可读归因）。

背景：实测 agent_task 被 600s 超时硬中止后 errors 与 stderr 均为空，用户只看到
「失败」无任何原因。timeout 杀进程时 stderr 常为空，#309 的遗言尾采拿不到归因，
因此对超时特征（step stopReason=timeout / returncode=124 / SIGTERM）直接构造
显式文案，写入 agent_tasks.errors 并透出到事件流 result 与前端。

全链路复用本模块，避免各处文案口径漂移：
- hwcloud_acp / hermes_acp：stopReason=timeout 收口
- executor：CLI 后端进程退出码归因
- event_pipeline：direct 失败收口的 result 摘要兜底
"""

# 单条归因文案截断上限（与 agent_tasks.errors 既有 500 字符截断口径对齐）
ATTRIB_TEXT_MAX = 500


def timeout_error_text(seconds, detail: str = "") -> str:
    """构造「执行超时（Ns）」显式归因文案。

    :param seconds: 触发超时的实际上限秒数（静默超时/全局超时的配置值）。
    :param detail: 可选补充（如触发来源 stopReason=timeout、超时类型）。
    """
    try:
        secs = int(round(float(seconds)))
    except (TypeError, ValueError):
        secs = 0
    msg = f"执行超时（{secs}s）"
    if detail:
        msg += f"：{detail}"
    return msg[:ATTRIB_TEXT_MAX]


def process_exit_attribution(returncode) -> str | None:
    """按进程退出码识别超时类死亡，返回归因文案；非超时特征返回 None。

    - 124：被 `timeout` 命令强制中止（agent 内长命令常用 timeout N 包裹）。
    - -15（SIGTERM）：进程被终止信号杀死——executor 超时强杀 / 停机中断均走
      SIGTERM，疑似执行超时（确定性低于 124，文案如实区分）。
    """
    if returncode == 124:
        return "执行超时：子进程被 timeout 命令强制中止（returncode=124）"
    if returncode == -15:
        return "进程被 SIGTERM 终止（疑似执行超时或服务停机中断）"
    return None


def failure_summary(errors, exit_code=None, limit: int = 3) -> str:
    """失败收口的可读摘要（result_text 为空时的兜底，透出到事件 result / 前端）。

    :param errors: done 事件 errors 列表（元素可为 str/dict）。
    :param exit_code: 进程退出码（None 则不展示）。
    :param limit: 最多取前 N 条，每条截断 200 字符。
    """
    parts = []
    for e in (errors or []):
        s = str(e).strip()
        if s:
            parts.append(s[:200])
    if not parts:
        return ""
    head = "；".join(parts[:limit])
    prefix = f"任务失败（exit_code={exit_code}）" if exit_code is not None else "任务失败"
    more = f"（另有 {len(parts) - limit} 条错误详情见任务 errors）" if len(parts) > limit else ""
    return f"{prefix}：{head}{more}"
