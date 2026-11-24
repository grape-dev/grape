"""Autopilot 任务「派发状态」口径（Issue #3733）。

只放常量与纯函数，**不 import 数据库/框架**：mgmt 域的补列迁移（`src/api/db_setup.py`）、
mgmt 域公共读写出参（`src/domains/mgmt/routes/autopilot.py`）与 runtime 调度器
（`src/domains/runtime/engine/scheduler.py`）共用同一份字符串，避免三处各写一份导致
状态值与界面原因漂移。

背景：`autopilot_tasks` 里可能有 enabled=1 但 instruction 为空的行（存量历史数据 /
直接改库）。这类任务既不能派发、又只靠日志表达，于是每 tick 刷一条 WARNING 且永不派发。
本模块定义的 `blocked_empty_instruction` 把「拦截」升级成显式状态：状态变更才留痕一次，
界面可见原因，补齐指令即恢复 cron 派发。
"""

# 正常可派发（列默认值；也是「指令补齐后」的状态）
AUTOPILOT_STATE_OK = ""

# 已启用但不可派发：指令为空（trim 后为空串）
AUTOPILOT_STATE_BLOCKED_EMPTY_INSTRUCTION = "blocked_empty_instruction"

# 原因文案：直接进任务页/接口出参，禁止编造指令内容（id=6 的指令需业务方提供）
AUTOPILOT_EMPTY_INSTRUCTION_REASON = (
    "任务指令为空：已启用但不可派发，补齐巡检指令后将自动恢复按 cron 派发"
)


def reason_for_state(state: str) -> str:
    """状态 → 人类可读原因（未知/空状态返回空串）。"""
    if state == AUTOPILOT_STATE_BLOCKED_EMPTY_INSTRUCTION:
        return AUTOPILOT_EMPTY_INSTRUCTION_REASON
    return ""


def dispatch_reason(instruction: str, state: str = "") -> str:
    """出参用原因：**以当前 instruction 为准**（不依赖调度器是否已经跑过一次）。

    库内状态只描述「上次扫描看到的事实」；用户刚清空指令、或迁移尚未执行时，
    `dispatch_state` 可能仍是空串。接口出参若只看库内状态就会说谎，
    故只要指令为空就给出原因，库内已落库的原因优先（将来会有别的不可派发原因）。
    """
    if not (instruction or "").strip():
        return reason_for_state(state or AUTOPILOT_STATE_BLOCKED_EMPTY_INSTRUCTION)
    return ""
