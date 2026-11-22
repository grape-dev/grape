"""流水线「当前责任人」展示口径（Issue #3646）。

展示位只放**人**：Issue 的人类 assignee → 最近人类事件操作者。bot、Agent runtime
身份与平台自动生成的登录名（`gcw_*`）都不直接进展示位——否则内部账号会顶替真实
责任人（Issue #3644 的流水线曾显示 `gcw_0FAKEACCT`，实际责任人应为 `songspace`）。

本模块是纯函数（无 IO / 无 DB）：判定只看名字形态与传入的 bot 名单，
展示层负责按优先级拼候选列表，便于单测与跨看板复用。
"""
from __future__ import annotations

import re
from typing import Iterable, Sequence

#: GitCode OAuth 自动生成的登录名（`gcw_` + 随机串）：代表平台自动化 / Agent 身份
#: （agent 派发、机器人建单用的账号），不是人类可读责任人。
INTERNAL_LOGIN_PREFIXES: tuple[str, ...] = ("gcw_",)

#: bot 名形态：`bot` / `*_bot` / `*-bot`（与看板 `is_bot_name` 同一规则）。
#: 刻意不用裸 `endswith("bot")`——会误杀 abbot / robot / talbot 这类真人账号。
_BOT_SUFFIX_RE = re.compile(r"(?:^|[-_\s])bot$", re.IGNORECASE)

#: Agent 显示名（configs/agent-description.json 的 "Coding Agent" / "QA Agent" 等）：
#: 那是 Agent runtime 的身份，不是人。
_AGENT_DISPLAY_RE = re.compile(r"(?:^|[\s_-])agent$", re.IGNORECASE)

#: runtime 身份形态（如 `hermes-worker-ecs-grape-de-0005`）。
_RUNTIME_ID_RE = re.compile(r"(?:^|[-_])worker(?:[-_]|$)", re.IGNORECASE)


def is_internal_agent_account(name: str, bot_usernames: Iterable[str] = ()) -> bool:
    """是否为**不入责任人展示位**的内部账号。

    命中即内部：bot 名单（managed_repos / 全局配置）、`gcw_*` 自动登录名、
    bot 名形态、Agent 显示名与 worker/runtime 身份形态。
    空串不算内部账号——它的语义是「没有责任人」，由调用方按候选顺序跳过。
    """
    n = str(name or "").strip()
    if not n:
        return False
    if any(n.startswith(p) for p in INTERNAL_LOGIN_PREFIXES):
        return True
    bots = {str(b).strip() for b in (bot_usernames or ()) if str(b).strip()}
    if n in bots:
        return True
    if _BOT_SUFFIX_RE.search(n):
        return True
    if _AGENT_DISPLAY_RE.search(n):
        return True
    return bool(_RUNTIME_ID_RE.search(n))


def resolve_human_owner(assignee_candidates: Sequence[str] = (),
                        actor_candidates: Sequence[str] = (),
                        bot_usernames: Iterable[str] = ()) -> str:
    """按优先级取第一个**人类**责任人；全部为内部账号/空 → 返回空串。

    优先级（Issue #3646 Spec）：① Issue 指派（`issue_state.assignee`）→
    ② 事件 payload 里的 assignee → ③ 最近一次人类事件操作者。
    调用方按此顺序把两类候选拼进来即可。
    """
    _bots = tuple(bot_usernames or ())
    for raw in tuple(assignee_candidates or ()) + tuple(actor_candidates or ()):
        name = str(raw or "").strip()
        if name and not is_internal_agent_account(name, _bots):
            return name
    return ""


def resolve_current_owner(assignee_candidates: Sequence[str] = (),
                          actor_candidates: Sequence[str] = (),
                          bot_usernames: Iterable[str] = (),
                          fallback: str = "") -> str:
    """责任人展示值：人类优先；全为内部账号时回退 Agent 显示名（`fallback`）。

    `fallback` 由调用方用 `get_agent_display_name(当前阶段 agent)` 提供——
    展示层宁可显示「QA Agent」也不输出原始内部账号。
    """
    return resolve_human_owner(assignee_candidates, actor_candidates, bot_usernames) or str(fallback or "")