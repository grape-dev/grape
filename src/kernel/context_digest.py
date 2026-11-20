"""高信号任务上下文摘要（Issue #2269 质量整改）。

背景：Agent 通过 ``grape tasks complete --json`` 已经产出结构化结论
（actions/conclusion/artifacts/next_step/issue_summary），但此前的组织上下文与
项目级 prompt 摘要只读取事件状态/result 占位文本，最后只剩「任务是否完成」。

本模块只做确定性提炼，不额外调用 LLM：
- 保留结论、关键动作、验证证据、产物、风险/后续、可复用经验；
- 按字段上限裁剪，避免把整段执行日志塞回上下文（上下文是有限注意力预算）。
- 与 Anthropic/OpenAI 的 context engineering 口径一致：子任务返回蒸馏摘要，
  主流程只消费高信号 token，而不是重放完整过程。
"""
from __future__ import annotations

import re

# rationale（数值登记 docs/org-pipeline/org-runtime-budget.md §2）：下派结论要进
# 组织上下文（随上下文注入子任务），预算按 prompt token 折算——1200/600/360 字符
# 约对应 300/150/90 tokens（中文约 1 token/2 字符），16 条上限约 1.4k tokens，
# 占单次组织上下文预算（L3 32KB）不足 5%，避免摘要挤占执行侧规则正文。
# 预算过小会丢结论，过大则挤占注入；压测/token 计量见登记表“待决策”。
_MAX_CONCLUSION = 1200  # rationale: docs/org-pipeline/org-runtime-budget.md §2（约 300 tokens）
_MAX_SUMMARY = 600  # rationale: 约 150 tokens，结论摘要上限
_MAX_ITEM = 360  # rationale: 约 90 tokens，单条经验/动作上限
_MAX_ITEMS = 8  # rationale: 每角色最多 8 条高信号项
_MAX_TOTAL_ITEMS = 16  # rationale: 全角色合计 16 条（约 1.4k tokens）

_VERIFY_RE = re.compile(
    r"(?i)(pytest|pyspec|unittest|\btests?\b|ruff|flake8|\blint\b|"
    r"\bbuild\b|编译|构建|pre-pr-check|敏感扫描|安全扫描|"
    r"回归验证|复跑|\bpassed\b|用例复跑|本地复跑|测试结果|验证结果)")
_RISK_RE = re.compile(
    r"(?i)(风险|未决|未完成|阻塞|待确认|待人工|待审核|后续|遗留|"
    r"\boptional\b|\bnit\b|\btodo\b|\bfollow[- ]?up\b|"
    r"\bwarning\b|\berror\b|\bfailed\b)")
_REUSE_RE = re.compile(
    r"(?i)(根因|经验|教训|注意|避免|建议|\bagent feedback\b|实锤|"
    r"以后|下次|可复用|沉淀|坑)")
_NO_REUSE_RE = re.compile(
    r"(?i)(无新增|无可执行|未发现|无建议|未上报|无需|没有(?:明确)?建议)")
_FEEDBACK_RE = re.compile(r"(?i)\bagent feedback\b|反馈审视|feedback\s*#?\d+")

_DONE_VALUES = {"", "done", "complete", "completed", "完成", "已解决", "无"}


def _clip(value: object, limit: int) -> str:
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    if len(text) > limit:
        text = text[: limit - 1].rstrip() + "…"
    return text


def _dedupe(items: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for item in items:
        key = item.strip().lower()
        if not key or key in seen:
            continue
        seen.add(key)
        out.append(item.strip())
    return out


def _as_items(value: object, limit: int = _MAX_ITEM,
              max_items: int = _MAX_ITEMS) -> list[str]:
    if value is None:
        return []
    raw = value if isinstance(value, (list, tuple, set)) else [value]
    out: list[str] = []
    for item in raw:
        if isinstance(item, dict):
            item = " / ".join(str(v) for v in item.values() if str(v or "").strip())
        text = _clip(item, limit)
        if text:
            out.append(text)
    return _dedupe(out)[:max_items]


def build_digest(output: object, *, title: str = "", agent_type: str = "",
                 status: str = "", task_id: str = "", errors: str = "") -> dict:
    """把结构化任务产出提炼成上下文摘要字段。

    输入既可以是 ``parse_agent_output_json`` 的 dict，也可以是纯文本结论。
    返回字段均为字符串或字符串列表，渲染层可直接消费。
    """
    if not isinstance(output, dict):
        output = {"conclusion": output or ""}
    conclusion = _clip(
        output.get("conclusion") or output.get("issue_summary") or "", _MAX_CONCLUSION)
    issue_summary = _clip(output.get("issue_summary") or "", _MAX_SUMMARY)
    actions = _as_items(output.get("actions"))
    artifacts = _as_items(output.get("artifacts"))
    next_step = _clip(output.get("next_step") or "", 200)

    verification = [a for a in actions if _VERIFY_RE.search(a)]
    changes = [a for a in actions
               if a not in verification and not _FEEDBACK_RE.search(a)]
    risks = [a for a in actions
             if a not in verification and _RISK_RE.search(a)]
    reusable = [a for a in actions
                if _REUSE_RE.search(a) and not _NO_REUSE_RE.search(a)]
    if conclusion and _REUSE_RE.search(conclusion) \
            and not _NO_REUSE_RE.search(conclusion):
        reusable.insert(0, conclusion)

    # 控制总量：验证/变更优先，其次风险；避免单目标挤占全部上下文预算。
    changes = changes[:_MAX_ITEMS]
    verification = verification[:_MAX_ITEMS]
    risks = _dedupe(risks)[:6]
    reusable = _dedupe(reusable)[:_MAX_ITEMS]
    total = changes + verification + risks + artifacts
    if len(total) > _MAX_TOTAL_ITEMS:
        keep = _MAX_TOTAL_ITEMS
        changes = changes[: max(0, keep - len(verification) - len(risks))]
    return {
        "title": _clip(title, 200),
        "agent_type": _clip(agent_type, 40),
        "status": _clip(status, 40),
        "task_id": _clip(task_id, 80),
        "conclusion": conclusion,
        "issue_summary": issue_summary,
        "changes": changes,
        "verification": verification,
        "artifacts": artifacts,
        "risks": risks,
        "next_step": next_step,
        "reusable": reusable,
        "errors": _clip(errors, 400),
    }


def digest_has_content(digest: dict) -> bool:
    return bool(
        (digest or {}).get("conclusion")
        or (digest or {}).get("changes")
        or (digest or {}).get("verification")
        or (digest or {}).get("artifacts")
    )


def _render_items(lines: list[str], heading: str, items: list[str]) -> None:
    if not items:
        return
    lines += ["", heading]
    lines += [f"- {item}" for item in items]


def render_digest(digest: dict, heading_level: int = 3) -> str:
    """渲染单个任务的执行摘要（Markdown）。"""
    if not digest_has_content(digest):
        return ""
    prefix = "#" * max(1, min(6, heading_level))
    meta = []
    if digest.get("agent_type"):
        meta.append(str(digest["agent_type"]))
    if digest.get("status"):
        meta.append(str(digest["status"]))
    if digest.get("task_id"):
        meta.append(f"task={digest['task_id']}")
    lines: list[str] = []
    if digest.get("title"):
        lines.append(f"{prefix} {digest['title']}")
    if meta:
        lines.append(f"> {' / '.join(meta)}")
    if digest.get("conclusion"):
        lines += ["", "**结论**", str(digest["conclusion"])]
    _render_items(lines, "**关键动作**", digest.get("changes") or [])
    _render_items(lines, "**验证证据**", digest.get("verification") or [])
    _render_items(lines, "**产物**", digest.get("artifacts") or [])
    _render_items(lines, "**风险与后续**", digest.get("risks") or [])
    next_step = digest.get("next_step") or ""
    if next_step and next_step.lower() not in _DONE_VALUES:
        lines += ["", "**执行时后续**", f"- {next_step}"]
    if digest.get("errors"):
        lines += ["", "**错误**", f"- {digest['errors']}"]
    return "\n".join(lines).strip()


def render_compact(digest: dict) -> str:
    """单行摘要：优先 issue_summary，其次 conclusion（用于 prompt 项目上下文）。"""
    return _clip(digest.get("issue_summary") or digest.get("conclusion") or "", 300)


def render_reusable(digest: dict) -> str:
    """可复用经验（失败教训/注意事项/Agent feedback），供知识沉淀。"""
    if not digest_has_content(digest):
        return ""
    lines: list[str] = []
    _render_items(lines, "**可复用经验**", digest.get("reusable") or [])
    return "\n".join(lines).strip()