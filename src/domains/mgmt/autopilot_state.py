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

# Issue #3818：指令命中凭据外传载荷审查（入库拒绝 / 派发拦截）
AUTOPILOT_STATE_BLOCKED_PAYLOAD = "blocked_payload"

# Issue #3818：同一 payload hash 重复投递达阈值，已熔断进 dead-letter
AUTOPILOT_STATE_BLOCKED_PAYLOAD_REPEAT = "blocked_payload_repeat"

# 原因出参：**稳定 code，不是给用户看的文案**（D1 国际化守卫）——
# 后端一律不下发中文串，界面按 code 取语言包（shell/locales/{zh,en}/autopilot.json
# 的 list.not_dispatchable / detail.not_dispatchable_desc）。历史库里若有自由文本，
# 界面回落到语言包默认文案，不做机器翻译。
AUTOPILOT_EMPTY_INSTRUCTION_REASON = AUTOPILOT_STATE_BLOCKED_EMPTY_INSTRUCTION


def reason_for_state(state: str) -> str:
    """状态 → 原因 code（未知/空状态返回空串）。

    Issue #3818：载荷审查/熔断两类拦截的原因**也是稳定 code**（`blocked_payload`
    / `blocked_payload_repeat` 本身），界面按 code 取语言包，后端不下发中文文案。
    """
    if state == AUTOPILOT_STATE_BLOCKED_EMPTY_INSTRUCTION:
        return AUTOPILOT_EMPTY_INSTRUCTION_REASON
    if state in (AUTOPILOT_STATE_BLOCKED_PAYLOAD, AUTOPILOT_STATE_BLOCKED_PAYLOAD_REPEAT):
        return state
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
