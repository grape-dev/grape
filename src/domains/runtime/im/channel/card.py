"""Card IR —— 渠道中立卡片模型（Issue #1095）。

业务层只构造本模块的中立卡片对象（扁平不嵌套），各渠道经 CardRenderer
渲染为平台形态（飞书 lark JSON / 钉钉 actionCard / 共享纯文本）。
平台私有格式（lark_md / behaviors / template）只允许出现在 renderer 内，
业务层禁止出现（tests/im/test_card_ir.py 静态检查断言）。

设计要点（im-card-ir-design.md §4）：
  - 交互语义化：Action.id + params（如 id="bind", params={"num": 3}），
    命令串由 action_payload() 在渲染边界生成（"/bind 3"），回收入站后
    仍按 cmd + 参数字典路由（feishu parse_card_action 零改动）。
  - Tone 语义色代替平台配色名（red/blue/green 是飞书枚举）。
  - 无 RICH_CARD 渠道 → render_text() 压平为文本（保全部可见文本 +
    动作命令提示），禁止透传平台 JSON。
"""
from __future__ import annotations

import enum
import os
import re
from dataclasses import dataclass, field
from typing import Any

CARD_IR_ENV = "GRAPE_IM_CARD_IR"


def card_ir_enabled() -> bool:
    """Card IR 双轨开关：on（默认）走渠道 renderer；off 走文本降级（灰度回滚）。"""
    env = os.environ.get(CARD_IR_ENV, "on").strip().lower()
    return env not in ("off", "0", "false", "no")


class Tone(enum.Enum):
    """语义色（渲染端各自映射平台配色）。"""

    DEFAULT = "default"
    INFO = "info"
    NEUTRAL = "neutral"
    SUCCESS = "success"
    WARNING = "warning"
    DANGER = "danger"


class ButtonStyle(enum.Enum):
    DEFAULT = "default"
    PRIMARY = "primary"
    DANGER = "danger"


@dataclass(frozen=True)
class Action:
    """卡片动作：语义 id + 结构化参数（替代 {"cmd": "/bind 1"} 平台载荷直传）。

    url：仅跳转型动作（钉钉 actionCard 按钮唯一能承载的交互）；
    有 url 无 params → 渲染为跳转按钮；只有 params → 无回调渠道降级为命令提示。
    """

    id: str                       # 语义动作标识，如 "bind" / "join" / "finding-dispatch"
    label: str = ""
    style: ButtonStyle = ButtonStyle.DEFAULT
    params: dict[str, Any] = field(default_factory=dict)
    url: str = ""


# 带位置参数的命令动作：num 参数折进命令串（"/bind 3"），不重复出现在载荷里
_POSITIONAL_CMD_IDS = frozenset({"bind", "join"})


def action_payload(action: Action) -> dict[str, Any]:
    """语义动作 → 回调载荷 {"cmd": ..., **params}（与既有 value 字典零漂移）。"""
    cmd = "/" + action.id
    params = dict(action.params or {})
    if action.id in _POSITIONAL_CMD_IDS and "num" in params:
        cmd = f"{cmd} {params.pop('num')}"
    payload: dict[str, Any] = {"cmd": cmd}
    payload.update(params)
    return payload


@dataclass(frozen=True)
class Block:
    """扁平块：按顺序渲染，不做容器嵌套。

    kind=actions：一行按钮（v1 卡的 elements[].actions 行）；
    kind=button ：独立按钮（v2 卡的 body 内联按钮，带 style 强调）。
    """

    kind: str                      # text | fields | divider | select | actions | button
    text: str = ""
    fields: tuple[tuple[str, str], ...] = ()
    options: tuple[Action, ...] = ()
    placeholder: str = ""
    action: Action | None = None   # kind=button 时使用


def text_block(text: str) -> Block:
    return Block(kind="text", text=text)


def divider_block() -> Block:
    return Block(kind="divider")


def fields_block(*pairs: tuple[str, str]) -> Block:
    return Block(kind="fields", fields=tuple(pairs))


def select_block(placeholder: str, options: list[Action]) -> Block:
    return Block(kind="select", placeholder=placeholder, options=tuple(options))


def row_block(*actions: Action) -> Block:
    return Block(kind="actions", options=tuple(actions))


def button_block(action: Action) -> Block:
    return Block(kind="button", action=action)


@dataclass(frozen=True)
class Card:
    """渠道中立卡片。form="v1"：elements 交互行；form="v2"：内联按钮 + 下拉（原地更新态卡）。"""

    title: str = ""
    tone: Tone = Tone.DEFAULT
    blocks: tuple[Block, ...] = ()
    actions: tuple[Action, ...] = ()      # 卡片底部按钮区
    page: tuple[int, int] | None = None   # (当前页, 总页数) → 页脚
    hint: str = ""                        # 页脚提示，如 "回复编号选择 · 0 取消"
    form: str = "v1"
    update_key: str = ""                  # 原地更新令牌（飞书 token / 后续钉钉 cardId）
    opaque: dict = field(default_factory=dict)  # 过渡期逃生舱：禁止业务依赖


def simple_card(title: str, text: str, tone: Tone = Tone.DEFAULT,
                *actions: Action, form: str = "v1") -> Card:
    """单文本块卡片的便捷构造（确认/失败类卡片）。"""
    return Card(title=title, tone=tone, blocks=(text_block(text),),
                actions=tuple(actions), form=form)


# ---------------------------------------------------------------- 共享 text renderer

_SINGLE_NL = re.compile(r"(?<!  )(?<!\n)\n(?!\n)")


def hard_breaks(text: str) -> str:
    """把单个换行补成「两空格 + 换行」硬换行（markdown 硬断行）。

    钉钉 actionCard 的 text 按 markdown 渲染，单个 \\n 不换行（/where 等命令回复会
    挤成一行）。空行分段与代码围栏（```）内内容保持原样。
    """
    if not text:
        return text
    parts = text.split("```")
    for i in range(0, len(parts), 2):        # 偶数段=非代码块
        parts[i] = _SINGLE_NL.sub("  \n", parts[i])
    return "```".join(parts)


def render_text(card: Card) -> str:
    """降级渲染：Card → 纯文本/markdown（保全部可见文本 + 动作命令提示）。

    契约（验收 1c）：输出必须包含卡片全部可见文本；按钮以「回复 /cmd」提示
    保留（_flatten_card 只抽 div 文本、丢按钮的缺陷在此修复）。
    """
    lines: list[str] = []
    if card.title:
        lines.append(f"**{card.title}**")
    for block in card.blocks:
        if block.kind == "text":
            if block.text:
                lines.append(block.text)
        elif block.kind == "fields":
            for key, value in block.fields:
                lines.append(f"{key}: {value}")
        elif block.kind == "divider":
            lines.append("---")
        elif block.kind == "select":
            if block.placeholder:
                lines.append(f"{block.placeholder}")
            for i, opt in enumerate(block.options, 1):
                lines.append(numbered_option_label(
                    i, opt.label or action_payload(opt).get("cmd", "")))
        elif block.kind == "actions":
            for act in block.options:
                lines.append(_action_hint(act))
        elif block.kind == "button" and block.action is not None:
            lines.append(_action_hint(block.action))
    for act in card.actions:
        lines.append(_action_hint(act))
    if card.page:
        lines.append(f"第 {card.page[0]}/{card.page[1]} 页")
    if card.hint:
        lines.append(card.hint)
    return "\n".join(line for line in lines if line is not None).strip()


_OPTION_NUMBER_RE = re.compile(r"^\s*\d+[.、]\s*")


def numbered_option_label(index: int, label: str) -> str:
    """下拉选项统一编号；label 已自带序号时不重复编号（避免「1. 1. x」）。"""
    text = (label or "").strip()
    return text if _OPTION_NUMBER_RE.match(text) else f"{index}. {text}"


def _action_hint(action: Action) -> str:
    label = action.label or action.id
    if action.url:
        return f"{label}: {action.url}"
    return f"{label}: 回复 `{action_payload(action).get('cmd', '')}`"


# ---------------------------------------------------------------- 能力位自检

# 内置渠道声明（channel_type → 是否有 renderer 实现）；wiring 侧按此装配。
# Issue #1309：slack 加入（Block Kit 渲染，符合 Card IR 分层契约）
# Issue #1613：telegram 加入（文本 + InlineKeyboardMarkup，RICH_CARD 契约）
# Issue #1728：wechat 加入（纯文本 + 编号选项，RICH_CARD 降级契约）
BUILTIN_CARD_RENDERERS = ("feishu", "dingtalk", "wecom", "slack", "telegram", "wechat")


def validate_card_capability(caps: Any, has_renderer: bool) -> str:
    """启动自检：声明 RICH_CARD 必须有 CardRenderer（声明支持但无实现 = 启动告警）。"""
    from .capability import Capability
    if (caps & Capability.RICH_CARD) and not has_renderer:
        return "channel declares RICH_CARD but has no CardRenderer"
    return ""


def self_check_card_capabilities(channel_caps: dict[str, Any]) -> list[str]:
    """对全部渠道能力表跑一致性检查，返回问题清单（告警不阻断）。"""
    problems: list[str] = []
    for channel_type, caps in (channel_caps or {}).items():
        problem = validate_card_capability(caps, channel_type in BUILTIN_CARD_RENDERERS)
        if problem:
            problems.append(f"{channel_type}: {problem}")
    return problems


__all__ = [
    "BUILTIN_CARD_RENDERERS",
    "CARD_IR_ENV",
    "Action",
    "Block",
    "ButtonStyle",
    "Card",
    "Tone",
    "action_payload",
    "button_block",
    "card_ir_enabled",
    "divider_block",
    "fields_block",
    "hard_breaks",
    "numbered_option_label",
    "render_text",
    "row_block",
    "select_block",
    "self_check_card_capabilities",
    "simple_card",
    "text_block",
    "validate_card_capability",
]
