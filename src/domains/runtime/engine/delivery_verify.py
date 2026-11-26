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
- PR 订阅伪 issue_id（``#pr_<n>_<scene>``）不是新需求交付目标，``applies=False``
  跳过核实：worker 与 direct 走同一判定，避免「worker 判未交付 / direct 豁免」的
  双路径不对称（direct 侧同时保留 ``evt._pr_sub_pr_number`` 显式守卫）。
- ``list_prs`` 全量翻页在网络抖动时可能长时间占用收尾请求线程，查询在带超时的
  子线程执行（``GRAPE_DELIVERY_VERIFY_TIMEOUT_SECONDS``，默认 15s）；超时按查询
  失败 fail-closed，不静默放行。
"""
from __future__ import annotations

import logging
import os
import threading

log = logging.getLogger("devops-bot")

# 默认启用交付核实的工作流（skill 名，大小写不敏感）。
DEFAULT_DELIVERY_VERIFY_SKILLS = ("atomcode-bug-fix",)
# 环境变量：逗号分隔的 skill 清单，覆盖默认值（空/未设置 = 默认）。
ENV_DELIVERY_VERIFY_SKILLS = "GRAPE_DELIVERY_VERIFY_SKILLS"
# 环境变量：PR 查询墙钟超时（秒），默认 15，非法值回退默认。
ENV_DELIVERY_VERIFY_TIMEOUT = "GRAPE_DELIVERY_VERIFY_TIMEOUT_SECONDS"
DEFAULT_DELIVERY_VERIFY_TIMEOUT_SECONDS = 15.0

# 未通过核实的机器可读错误码（写 agent_tasks.errors / 事件 failure_info）。
DELIVERY_UNVERIFIED_ERROR_CODE = "CODING_DELIVERY_UNVERIFIED"

# 未交付状态文案前缀（事件 result / result_text 展示用）。
DELIVERY_NOT_DELIVERED_PREFIX = "Coding 未交付"

# 核实结果 code 常量（调用方/测试统一引用，禁止散落硬编码字符串）。
DELIVERY_CODE_NO_ISSUE_NUMBER = "no_issue_number"
DELIVERY_CODE_PR_QUERY_FAILED = "pr_query_failed"
DELIVERY_CODE_NO_LINKED_PR = "no_linked_pr"

# prometheus 计数器（注册失败降级 None，不阻断收尾路径；模式与
# stale_uid.py 的 _stale_uid_total 一致：本模块是唯一定义点，tests 直接
# import 本模块，不经第二处注册）。
_delivery_verify_total = None
try:  # pragma: no cover - 主控环境必有 prometheus_client，降级分支难在单测覆盖
    from prometheus_client import Counter as _PC
    from src.domains.insight import public as _obs_public
    _delivery_verify_total = _PC(
        "grape_runtime_delivery_verify_total",
        "coding delivery verification outcomes by result code (issue #4440)",
        ["outcome"], registry=_obs_public.prometheus_registry())
except Exception as _e:  # noqa: BLE001
    _delivery_verify_total = None
    log.warning(f"delivery verify counter unavailable: {type(_e).__name__}: {_e}")


def _bump_delivery_verify_metric(result) -> None:
    """核实结果单点埋点（A13）：只对 applies=True 的核实计数，未命中清单不计。

    outcome 取值 = delivered / no_issue_number / pr_query_failed / no_linked_pr，
    与 DELIVERY_CODE_* 常量同源；注册降级时静默跳过（Metrics 不可用不影响判定）。
    """
    r = result or {}
    if not _delivery_verify_total or not r.get("applies"):
        return
    outcome = "delivered" if r.get("delivered") else (r.get("code") or "unknown")
    try:
        _delivery_verify_total.labels(outcome=outcome).inc()
    except Exception:  # noqa: BLE001 - 埋点失败不改变核实判定
        log.warning("delivery verify metric bump failed")


def _verify_timeout_seconds() -> float:
    raw = (os.environ.get(ENV_DELIVERY_VERIFY_TIMEOUT) or "").strip()
    try:
        val = float(raw) if raw else DEFAULT_DELIVERY_VERIFY_TIMEOUT_SECONDS
    except ValueError:
        val = DEFAULT_DELIVERY_VERIFY_TIMEOUT_SECONDS
    return val if val > 0 else DEFAULT_DELIVERY_VERIFY_TIMEOUT_SECONDS


def is_delivery_unverified(result) -> bool:
    """统一未交付判定（收敛调用方重复的 ``result is not None and not delivered``）。"""
    return bool(result) and result.get("delivered") is False


def delivery_unverified_error_entry(result, skill: str = "") -> dict:
    """统一构造 agent_tasks.errors 结构化错误项（错误码 + code + 原因 + skill）。"""
    r = result or {}
    return {"error_code": DELIVERY_UNVERIFIED_ERROR_CODE,
            "code": r.get("code") or "",
            "message": r.get("reason") or "",
            "skill": skill or ""}


def not_delivered_message(result) -> str:
    """统一「Coding 未交付」展示文案（事件 result / result_text 前缀）。"""
    r = result or {}
    return f"⚠️ {DELIVERY_NOT_DELIVERED_PREFIX}：{r.get('reason') or ''}"


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
        # 显式留痕：skill 缺失时跳过核实（fail-open），避免「静默放行」不可观测。
        log.warning("delivery verify: empty skill for coding task; "
                    "skip verification (fail-open)")
        return False
    return s in delivery_verify_skills()


def _issue_text(issue_id) -> str:
    """规范化 issue_id 文本（去空白与前导 ``#``），供伪 id / 编号解析共用。"""
    return str(issue_id or "").strip().lstrip("#")


def is_pr_subscription_issue_id(issue_id) -> bool:
    """PR 订阅伪 issue_id（``#pr_123_scene``）——不是新需求交付目标。"""
    return _issue_text(issue_id).lower().startswith("pr")


def _issue_number_of(issue_id) -> int:
    """从 issue_id 解析原需求编号；PR 订阅伪 issue_id（#pr_123 等）返回 0。"""
    text = _issue_text(issue_id)
    if not text or is_pr_subscription_issue_id(issue_id):
        return 0
    digits = "".join(ch for ch in text if ch.isdigit())
    try:
        num = int(digits) if digits else 0
    except ValueError:
        num = 0
    return num if num > 0 else 0


def _find_delivery_prs_now(repo_key: str, issue_number: int):
    """实际执行 PR 查询（在带超时的子线程内调用）。返回 ``(prs, error)``。"""
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


def find_delivery_prs(repo_key: str, issue_number: int):
    """按原需求编号查询托管平台关联 PR（带墙钟超时，避免长期占用请求线程）。

    Returns:
        ``(prs, error)``：``error`` 非空 = 查询失败（与「无 PR」区分开，
        两者语义不同但都按未交付处理）；``prs`` 为关联 PR 列表（可核实）。
    """
    timeout = _verify_timeout_seconds()
    box = {}

    def _run():
        try:
            box["result"] = _find_delivery_prs_now(repo_key, issue_number)
        except Exception as e:  # noqa: BLE001 - 子线程异常转交给调用方 fail-closed
            box["error"] = e

    t = threading.Thread(target=_run, name="delivery-verify-prs", daemon=True)
    t.start()
    t.join(timeout)
    if t.is_alive():
        return [], f"PR 查询超时（>{timeout:g}s）"
    if "error" in box:
        return [], f"PR 查询异常：{str(box['error'])[:200]}"
    return box.get("result") or ([], "")


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
    if is_pr_subscription_issue_id(issue_id):
        # PR 订阅任务是对既有 PR 的跟进，不要求新交付 PR；worker/direct 同口径跳过。
        log.info(f"delivery verify: skip PR-subscription pseudo issue_id={issue_id!r}")
        return result
    result["applies"] = True
    num = _issue_number_of(issue_id)
    if not num:
        result.update(delivered=False, code=DELIVERY_CODE_NO_ISSUE_NUMBER,
                      reason="无关联需求编号，无法核实关联 PR")
        log.info(f"delivery verify: no issue number for skill={skill} issue_id={issue_id!r}")
        return result
    try:
        prs, err = find_delivery_prs(repo_key, num)
    except Exception as e:
        # fail-closed：核实链路自身异常同样按未交付处理，不静默放行
        result.update(delivered=False, code=DELIVERY_CODE_PR_QUERY_FAILED,
                      reason=f"交付核实异常：{str(e)[:200]}")
        log.warning(f"delivery verify: query error repo={repo_key} issue=#{num}: {e}")
        return result
    if err:
        result.update(delivered=False, code=DELIVERY_CODE_PR_QUERY_FAILED, reason=err)
        log.warning(f"delivery verify: query failed repo={repo_key} issue=#{num}: {err}")
        return result
    if not prs:
        result.update(delivered=False, code=DELIVERY_CODE_NO_LINKED_PR,
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


def verify_coding_delivery_safe(repo_key: str, issue_id, agent_type: str, skill: str) -> dict:
    """带异常兜底的统一核实入口（两条收尾路径统一调用，禁止各自 try/except）。

    关键约束：兜底只对**命中核实清单**的工作流按未交付处理；未命中清单的
    coding 工作流即便核实链路异常也必须 ``applies=False, delivered=True``，
    不得因兜底误杀（#4440 复审 Standards 判断项）。
    """
    if not delivery_verify_applies(agent_type, skill):
        return {"applies": False, "delivered": True, "code": "", "reason": "", "prs": []}
    try:
        result = verify_coding_delivery(repo_key, issue_id, agent_type, skill)
    except Exception as e:  # fail-closed：仅命中清单的工作流按未交付处理
        log.warning(f"delivery verify: crashed repo={repo_key} issue={issue_id!r} "
                    f"skill={skill}: {e}")
        result = {"applies": True, "delivered": False,
                  "code": DELIVERY_CODE_PR_QUERY_FAILED,
                  "reason": f"交付核实异常：{str(e)[:200]}", "prs": []}
    _bump_delivery_verify_metric(result)
    return result


def verify_task_delivery(task_id) -> dict | None:
    """按 agent_tasks 行字段执行交付核实（供无 repo_key/skill 入参的收尾链路复用）。

    Returns:
        核实结果 dict；任务不存在 / 非 coding 时返回 ``None``（调用方按不适用处理）。
        读取失败同样返回 ``None``（不阻断既有收尾路径）；行内确认 coding 后走
        ``verify_coding_delivery_safe``，核实链路异常按 fail-closed 未交付处理。
    """
    if not task_id:
        return None
    try:
        from src.kernel.session_manager import db, db_lock
        with db_lock:
            row = db.cursor().execute(
                "SELECT repo_key, issue_id, agent_type, skill FROM agent_tasks WHERE id=?",
                (task_id,)).fetchone()
        if not row:
            return None
        repo_key, issue_id, agent_type, skill = row
        if (agent_type or "").strip().lower() != "coding":
            return None
        return verify_coding_delivery_safe(
            repo_key or "", issue_id or "", agent_type, skill or "")
    except Exception as e:
        log.warning(f"delivery verify: load task {task_id} failed: {e}")
        return None
