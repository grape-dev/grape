"""Coding 交付核实统一入口（Issue #4440）。

背景：要求 PR 交付的 Coding 工作流（如 atomcode-bug-fix）以 ACP ``end_turn``
提前结束后，收尾链路会把任务/事件记录为成功，但可能没有任何可核实的交付物
（无提交/推送/关联 PR），QA 无法验证交付，错误的成功记录放行未交付任务进入
下游（QA/自动流转）。

本模块是唯一的「交付核实」校验入口（两条收尾路径共用，禁止各自分叉）：

- 远程 Worker 收尾：``service.complete_task()`` 成功记录前调用；
- direct 收尾：``direct_executor._execute_agent_inner`` 写成功终态前调用。

行为语义（fail-closed）：

- 仅对配置了交付核实的 Coding 工作流生效（默认 ``atomcode-bug-fix``，可用环境
  变量 ``GRAPE_DELIVERY_VERIFY_SKILLS`` 覆盖，逗号分隔）；其他 Coding 工作流
  ``applies=False`` 直接放行，行为零改动。
- 按原需求编号查询托管平台关联 PR（open/merged/closed 全量，PR↔Issue 关联
  判据与 IM 通知同源）。
- 核实通过（存在关联 PR）→ ``delivered=True``，正常记录成功并推进。
- 无关联 PR / 查询失败（网络/权限/平台异常/无 bot token/无需求编号）→
  ``delivered=False`` 并携带 ``code`` + ``reason``，调用方改记
  「Coding 未交付」非成功终态，停止成功路径自动推进。
"""
from __future__ import annotations

import logging
import os

log = logging.getLogger("devops-bot")

# 默认启用交付核实的工作流（skill 名，大小写不敏感）。
DEFAULT_DELIVERY_VERIFY_SKILLS = ("atomcode-bug-fix",)
# 环境变量：逗号分隔的 skill 清单，覆盖默认值（空/未设置 = 默认）。
ENV_DELIVERY_VERIFY_SKILLS = "GRAPE_DELIVERY_VERIFY_SKILLS"

# 未通过核实的机器可读错误码（写 agent_tasks.errors / 事件 failure_info）。
DELIVERY_UNVERIFIED_ERROR_CODE = "CODING_DELIVERY_UNVERIFIED"

# 未交付状态文案前缀（事件 result / result_text 展示用）。
DELIVERY_NOT_DELIVERED_PREFIX = "Coding 未交付"


def delivery_verify_skills() -> tuple:
    """返回启用交付核实的 skill 清单（小写）。env 覆盖默认；非法值回退默认。"""
    raw = (os.environ.get(ENV_DELIVERY_VERIFY_SKILLS) or "").strip()
    if not raw:
        return DEFAULT_DELIVERY_VERIFY_SKILLS
    items = tuple(s.strip().lower() for s in raw.split(",") if s.strip())
    return items or DEFAULT_DELIVERY_VERIFY_SKILLS


def delivery_verify_applies(agent_type: str, skill: str) -> bool:
    """判定该任务是否需要交付核实：coding agent 且 skill 命中核实清单。"""
    if (agent_type or "").strip().lower() != "coding":
        return False
    s = (skill or "").strip().lower()
    if not s:
        return False
    return s in delivery_verify_skills()


def _issue_number_of(issue_id) -> int:
    """从 issue_id 解析原需求编号；PR 订阅伪 issue_id（#pr_123 等）返回 0。"""
    text = str(issue_id or "").strip().lstrip("#")
    if not text or text.lower().startswith("pr"):
        return 0
    digits = "".join(ch for ch in text if ch.isdigit())
    try:
        num = int(digits) if digits else 0
    except ValueError:
        num = 0
    return num if num > 0 else 0


def find_delivery_prs(repo_key: str, issue_number: int):
    """按原需求编号查询托管平台关联 PR。

    Returns:
        ``(prs, error)``：``error`` 非空 = 查询失败（与「无 PR」区分开，
        两者语义不同但都按未交付处理）；``prs`` 为关联 PR 列表（可核实）。
    """
    from src.domains.runtime.issues_center import _pr_related_issue_nums, _pr_vcs_client

    client = _pr_vcs_client(repo_key)
    if client is None:
        return [], "仓库未配置 bot token，无法查询托管平台 PR"
    try:
        resp = client.list_prs(state="all")
    except Exception as e:  # 网络/平台异常：查询失败，不得静默放行
        return [], f"PR 查询异常：{str(e)[:200]}"
    if isinstance(resp, dict):
        # VcsApiClient 契约：查询失败返回 {"error": ...}
        return [], f"PR 查询失败：{str(resp.get('error') or resp)[:200]}"
    matched = []
    for pr in (resp or []):
        if isinstance(pr, dict) and issue_number in _pr_related_issue_nums(pr):
            matched.append(pr)
    return matched, ""


def verify_coding_delivery(repo_key: str, issue_id, agent_type: str, skill: str) -> dict:
    """统一交付核实入口（远程 Worker / direct 两条收尾路径共用）。

    Args:
        repo_key: 仓库键（如 ``gitcode:owner/repo``）。
        issue_id: 原需求编号（``#1842`` / ``1842``；PR 订阅伪 id 无法核实）。
        agent_type: 任务 agent 类型（仅 ``coding`` 生效）。
        skill: 任务绑定的 skill/工作流名（命中核实清单才生效）。

    Returns:
        ``{"applies": bool, "delivered": bool, "code": str, "reason": str,
        "prs": list}``；``delivered=False`` 时 ``code`` ∈
        ``no_issue_number`` / ``pr_query_failed`` / ``no_linked_pr``。
        非核实工作流返回 ``applies=False, delivered=True``（零行为改动）。
    """
    result = {"applies": False, "delivered": True, "code": "", "reason": "", "prs": []}
    if not delivery_verify_applies(agent_type, skill):
        return result
    result["applies"] = True
    num = _issue_number_of(issue_id)
    if not num:
        result.update(delivered=False, code="no_issue_number",
                      reason="无关联需求编号，无法核实关联 PR")
        log.info(f"delivery verify: no issue number for skill={skill} issue_id={issue_id!r}")
        return result
    try:
        prs, err = find_delivery_prs(repo_key, num)
    except Exception as e:
        # fail-closed：核实链路自身异常同样按未交付处理，不静默放行
        result.update(delivered=False, code="pr_query_failed",
                      reason=f"交付核实异常：{str(e)[:200]}")
        log.warning(f"delivery verify: query error repo={repo_key} issue=#{num}: {e}")
        return result
    if err:
        result.update(delivered=False, code="pr_query_failed", reason=err)
        log.warning(f"delivery verify: query failed repo={repo_key} issue=#{num}: {err}")
        return result
    if not prs:
        result.update(delivered=False, code="no_linked_pr",
                      reason=f"Issue #{num} 在托管平台无关联 PR")
        log.warning(f"delivery verify: no linked PR repo={repo_key} issue=#{num} skill={skill}")
        return result
    numbers = []
    for p in prs:
        try:
            n = int(p.get("number") or p.get("iid") or 0)
        except (TypeError, ValueError):
            n = 0
        if n > 0:
            numbers.append(n)
    result["prs"] = numbers
    log.info(f"delivery verify: delivered repo={repo_key} issue=#{num} "
             f"prs={numbers} skill={skill}")
    return result
