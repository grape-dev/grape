"""A2A 终态派发短路（Issue #2816）。

背景：Issue #3425 的净改动曾并入载体 PR #917，随后载体被 PR Sweeper 打
``needs-human-terminal``、Issue 也被打 ``needs-human``。看门狗仍按停摆阈值
反复重派 coding，每轮只读复检后再次收敛 need_human，形成确定性空转。

本模块在 A2A 派发入口（``POST /api/a2a``）前提供终态判定短路：

* Issue 标签含 ``needs-human``，或载体 PR 评论存在标记行 ``needs-human-terminal``
  → 跳过本次 coding 派发，并在 ``event_skipped`` 留痕；
* 同时把 Issue 置人工态（``status=awaiting_input``）——巡检 ``issue_terminal``
  把该状态视为终态，不再判断链补偿，coding 自动循环由此退出；
* 标记行可携带 ``CONFLICT_SCOPE=intersect``，该场景交由
  ``terminal_disposition`` 的人工处置入口（指定新载体 / 改派 / 关闭）。

判定数据源全部 best-effort：无 token、网络异常、结构不符一律按「未命中」放行，
保持既有派发行为（保守放行优于误杀正常单据）。短 TTL 缓存避免每次派发都读 VCS。
"""
from __future__ import annotations

import os
import re
import time
from collections import OrderedDict

from src.api.pipeline_constants import PAUSED_ISSUE_STATUSES
from src.kernel.config import log

TERMINAL_ISSUE_LABELS = ("needs-human",)
TERMINAL_PR_MARKER = "needs-human-terminal"
CONFLICT_SCOPE_INTERSECT = "intersect"
MANUAL_ISSUE_STATUS = PAUSED_ISSUE_STATUSES[0]
TERMINAL_SKIP_REASON = "needs_human_terminal"

_CACHE_TTL_SECONDS = int(os.environ.get("A2A_TERMINAL_GUARD_TTL", "60"))
# 单次 VCS 请求超时（秒）：由上面总预算推导 —— 一次 miss 最多 4 次请求
# （Issue 详情 + 2 页评论 + 载体 PR 评论），20s 预算 / 4 = 5s；不取基线 30s 默认，
# 因为本判定在 a2a 请求路径上同步等待。
_FETCH_TIMEOUT_SECONDS = int(os.environ.get("A2A_TERMINAL_GUARD_TIMEOUT", "5"))
# 单次 miss 的 VCS 读取总预算（秒）：Issue 详情 + 评论分页 + 载体 PR 评论分页。
# 复审要求「限制总页数/总超时」——最坏从 ~400s 收敛到「预算 + 单请求超时」量级。
_FETCH_BUDGET_SECONDS = int(os.environ.get("A2A_TERMINAL_GUARD_BUDGET", "20"))
_MAX_COMMENT_PAGES = int(os.environ.get("A2A_TERMINAL_GUARD_MAX_COMMENT_PAGES", "2"))
_MAX_CARRIER_PRS = int(os.environ.get("A2A_TERMINAL_GUARD_MAX_CARRIER_PRS", "2"))
# 判定缓存上限（条）：条目只存判定结果（约 15 个短键 + marker 行 ≤500 字），
# 实测（sys.getsizeof 递归统计 (key, (ts, state))）：命中态 2.8KB / 未命中态 1.7KB /
# 负结果 0.14KB，取最大 2.8KB × 2048 ≈ 5.7MB 常驻；量级按「万级用户、30% 日活、
# 单日派发千级 × TTL=60s」估算，活跃单据数远低于上限，超限按 LRU 淘汰。
_MAX_CACHE_ENTRIES = int(os.environ.get("A2A_TERMINAL_GUARD_MAX_CACHE", "2048"))

_CACHE: "OrderedDict[tuple, tuple]" = OrderedDict()

_CARRIER_RE = re.compile(
    r"(?:载体|carrier)\s*(?:PR|pr|pull\s*request)\s*#?\s*(\d+)",
    re.IGNORECASE)
_CONFLICT_SCOPE_RE = re.compile(r"CONFLICT_SCOPE\s*=\s*([A-Za-z0-9_-]+)", re.IGNORECASE)


def _env_enabled() -> bool:
    raw = str(os.environ.get("A2A_TERMINAL_GUARD", "1") or "").strip().lower()
    return raw not in ("0", "false", "no", "off")


def normalize_labels(raw) -> list[str]:
    """把 Issue labels 的多种形态归一为小写字符串列表。

    支持 ``["needs-human"]`` / ``"needs-human,bug"`` /
    ``[{"name": "needs-human"}]``（GitHub/GitCode 返回对象数组）。
    """
    out: list[str] = []
    if not raw:
        return out
    if isinstance(raw, str):
        items = raw.split(",")
    elif isinstance(raw, dict):
        items = [raw]
    else:
        items = list(raw)
    for item in items:
        if isinstance(item, dict):
            item = item.get("name") or item.get("title") or ""
        name = str(item or "").strip().lower()
        if name and name not in out:
            out.append(name)
    return out


def parse_terminal_marker(text) -> dict:
    """解析评论正文中的终态标记行。

    命中条件：某一行包含 ``needs-human-terminal``（大小写不敏感）。
    返回 ``{"marker", "conflict_scope", "line"}``；未命中返回 ``{}``。
    """
    body = str(text or "")
    _lower = body.lower()
    _token = TERMINAL_PR_MARKER
    if _token not in _lower and _token.replace("-", "_") not in _lower:
        return {}
    for line in body.splitlines():
        _line_lower = line.lower()
        if _token not in _line_lower and _token.replace("-", "_") not in _line_lower:
            continue
        # scope 先按同行取；PR Sweeper 的标记行常把 CONFLICT_SCOPE 放在下一行，
        # 同行取不到时退到整条评论里找（取最后一次出现，视为最新结论）。
        scope = ""
        m = _CONFLICT_SCOPE_RE.search(line)
        if m:
            scope = m.group(1).strip().lower()
        else:
            _all = _CONFLICT_SCOPE_RE.findall(body)
            if _all:
                scope = str(_all[-1]).strip().lower()
        return {"marker": TERMINAL_PR_MARKER, "conflict_scope": scope,
                "line": line.strip()[:500]}
    return {}


def carrier_terminal_marker(comments) -> dict:
    """在 PR 评论集合中找终态标记行。

    ``comments`` 支持 dict 列表（含 ``body``）或字符串列表；返回最后一个命中
    （PR Sweeper 可能多次落标记，以最新为准），未命中返回 ``{}``。
    """
    hit: dict = {}
    for item in comments or []:
        body = item.get("body") if isinstance(item, dict) else item
        marker = parse_terminal_marker(body)
        if marker:
            hit = dict(marker)
            if isinstance(item, dict):
                hit["comment_id"] = item.get("id") or item.get("note_id") or ""
                hit["created_at"] = item.get("created_at") or ""
    return hit


def extract_carrier_pr_numbers(*texts) -> list[int]:
    """从 Issue 标题/正文/评论中解析 ``载体 PR #N`` / ``carrier PR #N``。"""
    out: list[int] = []
    for text in texts:
        for m in _CARRIER_RE.finditer(str(text or "")):
            try:
                num = int(m.group(1))
            except (TypeError, ValueError):
                # 坏条目跳过不炸列表（同 issue_vcs_index 解析族），留 debug 痕迹可定位脏数据。
                log.debug(f"a2a-terminal-guard: carrier PR number parse skipped: {m.group(0)!r}")
                continue
            if num > 0 and num not in out:
                out.append(num)
    return out


def evaluate_terminal_state(labels, issue_texts=(), carrier_comments=None,
                            explicit_pr: int = 0, manual_override=None) -> dict:
    """纯判定：终态是否命中、触发了哪些信号（不读外部数据）。

    参数：
        labels: Issue labels（原样，内部归一）。
        issue_texts: Issue 标题/正文/评论文本，用于解析载体 PR 号。
        carrier_comments: ``{pr_number: comments}`` 或单 PR 的评论列表。
        explicit_pr: 生效载体 PR 号（payload 显式 / 人工处置 meta），优先级最高。
        manual_override: 人工处置记录（``get_manual_override``），用于持久豁免旧标签。

    终态命中 = 标签命中 OR 载体 PR 标记行命中（任一即可，与 Spec 对齐）。
    人工越过（new_carrier/reassign）后，旧 ``needs-human`` 标签不再重复拦截；载体
    标记行仍按生效载体判定，若新载体再次终态则照常短路。
    ``folded`` 表示能识别出载体（显式 / 人工处置 / 文本派生）；``manual_required``
    表示命中终态。

    Issue #2816 复审（Spec）：``needs-human`` 标签豁免必须区分新/旧载体——文本载体
    始终解析；显式载体若正是文本派生的旧载体（看门狗重派 payload 带回旧号），不得
    豁免标签，否则纯标签单会重现空转。仅当显式载体不在旧载体集合，或与人工越过载体
    一致时才豁免；人工越过指向其它载体时标签仍为权威信号。
    """
    norm_labels = normalize_labels(labels)

    prs: list[int] = []
    explicit = 0
    if explicit_pr:
        try:
            explicit = int(explicit_pr)
        except (TypeError, ValueError):
            explicit = 0
    # 文本载体始终解析：即便存在显式载体，也要用它判断显式载体是否只是旧载体回放。
    text_prs = extract_carrier_pr_numbers(*issue_texts)
    if explicit > 0:
        # 载体优先级：显式/meta 载体存在时，Issue 文本中的旧载体不参与标记行判定，
        # 否则 new_carrier 处置后旧终态载体仍会持续拦截后续非 force 派发。
        prs.append(explicit)
    else:
        prs.extend(text_prs)

    override = manual_override if isinstance(manual_override, dict) else {}
    override_action = str(override.get("action") or "").strip().lower()
    try:
        override_pr = int(override.get("carrier_pr") or 0)
    except (TypeError, ValueError):
        override_pr = 0
    # 回放判据：显式载体号等于文本解析出的**最早**载体，才是「旧载体回放」。
    # 只看「显式号出现在文本里」会把「正文写旧载体 + 评论写新载体」的正常处置误判成回放
    # （新载体单被旧 needs-human 标签冻住）。文本里同时含新旧两号时，新号（非首个）放宽。
    explicit_is_old = explicit > 0 and bool(text_prs) and explicit == text_prs[0]
    # 按载体维度豁免：人工越过覆盖「标签」信号（自动标记不应在处置后立刻反噬），
    # 但仅在未显式指向其它载体、或显式载体与处置载体一致时生效。
    # payload 显式 carrier 本身即「新载体」信号，但若该号就是文本里的旧载体，
    # 说明只是旧载体回放，标签必须继续短路（Spec 回归修复）。
    if override_action in ("new_carrier", "reassign"):
        if explicit <= 0:
            label_exempt = True
        elif override_pr > 0 and override_pr != explicit:
            label_exempt = False
        else:
            label_exempt = True
    else:
        label_exempt = explicit > 0 and not explicit_is_old
    label_hit = any(lb in TERMINAL_ISSUE_LABELS for lb in norm_labels) and not label_exempt

    marker: dict = {}
    marker_pr = 0
    comments_map: dict = {}
    if isinstance(carrier_comments, dict):
        comments_map = {int(k): v for k, v in carrier_comments.items() if str(k).isdigit()}
    elif carrier_comments is not None:
        comments_map = {prs[0]: carrier_comments} if prs else {}
    if not prs:
        # 纯判定场景：调用方直接给 comments_map 而未提供载体来源，按评论键兜底。
        prs = sorted(comments_map)
    for pr in prs:
        hit = carrier_terminal_marker(comments_map.get(pr))
        if hit:
            marker = hit
            marker_pr = pr
            break

    triggers: list[str] = []
    if label_hit:
        triggers.append("issue_label_needs_human")
    if marker:
        triggers.append("carrier_pr_needs_human_terminal")

    conflict_scope = (marker.get("conflict_scope") or "").strip().lower()
    folded = bool(explicit) or bool(text_prs) or bool(override_action)
    return {
        "terminal": bool(triggers),
        "triggers": triggers,
        "labels": norm_labels,
        "folded": folded,
        "carrier_pr": marker_pr or (prs[0] if prs else 0),
        "carrier_prs": prs,
        "marker": marker,
        "conflict_scope": conflict_scope,
        "intersect": conflict_scope == CONFLICT_SCOPE_INTERSECT,
        "manual_required": bool(triggers),
        "manual_status": MANUAL_ISSUE_STATUS,
        "manual_override": override_action if label_exempt else "",
        "label_exempted": label_exempt,
    }


def _remaining_timeout(deadline: float) -> int:
    """在总预算内取本次请求超时（至少 1s），避免逐个请求无界累加。"""
    return max(1, min(_FETCH_TIMEOUT_SECONDS, int(deadline - time.monotonic())))


def _effective_carrier_pr(repo_key: str, issue_number, explicit_pr: int = 0) -> int:
    """生效载体优先级：payload 显式 > 人工处置 meta；都没有返回 0（交给文本派生）。"""
    try:
        from src.api.terminal_disposition import get_carrier_override
        meta_pr = get_carrier_override(repo_key, issue_number)
    except Exception as e:
        log.debug(f"A2A terminal guard: carrier override read failed ({e})")
        meta_pr = 0
    for cand in (explicit_pr, meta_pr):
        try:
            cand = int(cand or 0)
        except (TypeError, ValueError):
            cand = 0
        if cand > 0:
            return cand
    return 0


def _fetch_vcs_state(repo_key: str, issue_number, explicit_pr: int = 0) -> dict:
    """读取 Issue 标签 / 载体 PR 评论（best-effort；失败返回 available=False）。

    ``effective_pr`` 回传真正生效的载体（显式或 meta），供判定层使用——此前只回传
    入参 explicit，meta 载体评论被读取却不参与判定（复审硬违规：假阴性）。
    标签读取与评论读取解耦：评论读取失败不得丢弃已命中的权威 ``needs-human`` 标签。
    """
    try:
        from src.domains.mgmt.user.db import get_repo_token_with_refresh
        token = get_repo_token_with_refresh(repo_key) or ""
    except Exception as e:
        log.debug(f"A2A terminal guard: repo token resolve failed ({e})")
        token = ""
    if not token:
        return {"available": False}
    deadline = time.monotonic() + _FETCH_BUDGET_SECONDS
    try:
        from src.domains.runtime.vcs.api_client import create_client, _native_issue_number
        client = create_client(repo_key, token)
        num = _native_issue_number(str(issue_number).lstrip("#"))
        issue = client.get_issue(num, timeout=_remaining_timeout(deadline))
    except Exception as e:
        log.warning(f"A2A terminal guard: VCS read failed repo={repo_key} issue={issue_number}: {e}")
        return {"available": False}
    if not isinstance(issue, dict) or issue.get("error"):
        return {"available": False}
    labels = issue.get("labels")
    texts = [issue.get("title") or "", issue.get("body") or ""]
    # 评论读取独立 try：失败只丢文本线索，标签信号照常参与判定。
    issue_comments: list = []
    try:
        issue_comments = client.list_issue_comments(
            num, max_pages=_MAX_COMMENT_PAGES, timeout=_remaining_timeout(deadline)) or []
    except Exception as e:
        log.debug(f"A2A terminal guard: issue comments read failed "
                  f"repo={repo_key} issue={issue_number}: {e}")
    texts += [str((c or {}).get("body") or "") for c in issue_comments
              if isinstance(c, dict)]
    try:
        from src.api.terminal_disposition import get_manual_override
        manual_override = get_manual_override(repo_key, issue_number)
    except Exception as e:
        log.debug(f"A2A terminal guard: manual override read failed ({e})")
        manual_override = {}
    effective_pr = _effective_carrier_pr(repo_key, issue_number, explicit_pr)
    prs = [effective_pr] if effective_pr else extract_carrier_pr_numbers(*texts)
    carrier_comments: dict = {}
    for pr in prs[:_MAX_CARRIER_PRS]:
        if time.monotonic() >= deadline:
            log.info(f"A2A terminal guard: VCS budget exhausted "
                     f"repo={repo_key} issue={issue_number}; partial carrier reads")
            break
        try:
            carrier_comments[pr] = client.list_pr_comments(
                pr, max_pages=_MAX_COMMENT_PAGES,
                timeout=_remaining_timeout(deadline)) or []
        except Exception as e:
            log.debug(f"A2A terminal guard: PR comments read failed pr=#{pr}: {e}")
            carrier_comments[pr] = []
    return {"available": True, "labels": labels, "issue_texts": texts,
            "carrier_comments": carrier_comments, "effective_pr": effective_pr,
            "manual_override": manual_override}


def resolve_terminal_state(repo_key: str, issue_number, explicit_pr: int = 0) -> dict:
    """带短 TTL/LRU 缓存地读取并判定终态；数据不可用返回 ``{}``（放行）。

    负结果（VCS 不可读）也缓存，避免同类派发在短窗内反复串行打 VCS。
    判定使用 ``_fetch_vcs_state`` 回传的生效载体（显式/meta），保证优先级端到端生效。
    """
    key = (repo_key, str(issue_number), int(explicit_pr or 0))
    now = time.time()
    cached = _CACHE.get(key)
    if cached and now - cached[0] <= _CACHE_TTL_SECONDS:
        _CACHE.move_to_end(key)
        return dict(cached[1])
    raw = _fetch_vcs_state(repo_key, issue_number, explicit_pr)
    if not raw.get("available"):
        state: dict = {}
    else:
        state = evaluate_terminal_state(
            raw.get("labels"), raw.get("issue_texts") or (),
            raw.get("carrier_comments") or {}, raw.get("effective_pr") or 0,
            raw.get("manual_override") or {})
    _CACHE[key] = (now, state)
    _CACHE.move_to_end(key)
    while len(_CACHE) > _MAX_CACHE_ENTRIES:
        _CACHE.popitem(last=False)
    return dict(state)


def clear_cache(repo_key: str = "", issue_number="") -> None:
    """人工处置后清缓存，避免旧终态在 TTL 内继续短路。"""
    if not repo_key:
        _CACHE.clear()
        return
    prefix = (repo_key, str(issue_number))
    for key in [k for k in _CACHE if k[0] == prefix[0] and k[1] == prefix[1]]:
        _CACHE.pop(key, None)


def mark_issue_manual(repo_key: str, issue_number, reason: str = TERMINAL_SKIP_REASON) -> str:
    """把 Issue 置人工态（``awaiting_input``），返回落库后的实际状态。

    走 runtime 域状态迁移 helper（``mark_issue_manual_status``），状态字符串
    单源于 ``pipeline_constants.PAUSED_ISSUE_STATUSES``。
    """
    try:
        from src.domains.runtime.issues_center import mark_issue_manual_status
        return mark_issue_manual_status(repo_key, issue_number, reason)
    except Exception as e:
        log.warning(f"A2A terminal guard: mark manual failed {repo_key}/#{issue_number}: {e}")
        return ""


def _record_skip(repo_key: str, issue_number, target_agent: str, state: dict) -> str:
    detail = (f"target={target_agent} triggers={','.join(state.get('triggers') or [])} "
              f"carrier_pr={state.get('carrier_pr') or 0} "
              f"conflict_scope={state.get('conflict_scope') or ''}")
    try:
        from src.api.a2a_dispatch_guard import record_skip
        record_skip(repo_key, issue_number, target_agent, TERMINAL_SKIP_REASON, detail=detail)
    except Exception as e:
        log.warning(f"A2A terminal guard: record skip failed: {e}")
    return detail


def guard_terminal_dispatch(repo_key: str, issue_number, target_agent: str,
                            evt_payload: dict | None = None) -> dict:
    """派发 coding 前的终态短路。

    返回 ``{}`` = 放行；否则返回终态判定结果（含 ``reason``/``detail``），调用方
    据此返回 skipped 响应。``force`` 由调用方保证不进入本函数（人工可越过）。
    """
    if not _env_enabled() or not repo_key or not issue_number:
        return {}
    if str(target_agent or "").strip().lower() != "coding":
        return {}  # 只短路 coding 阶段（Spec 范围）
    payload = evt_payload if isinstance(evt_payload, dict) else {}
    explicit_pr = 0
    for key in ("carrier_pr", "pr_number"):
        raw = payload.get(key)
        if str(raw or "").strip().isdigit():
            explicit_pr = int(str(raw).strip())
            break
    state = resolve_terminal_state(repo_key, issue_number, explicit_pr)
    if not state or not state.get("terminal"):
        # Issue #4334：needs-human 之外，再回读目标 Issue 的流水线终态
        # （completed / qa-passed）与 external-block 标记；并对 watchdog
        # 自动重派做频次上限与 context 非空保护。判定异常一律放行。
        try:
            from src.api.watchdog_redispatch_guard import guard_watchdog_redispatch
            redo = guard_watchdog_redispatch(
                repo_key, issue_number, target_agent, payload,
                labels=(state or {}).get("labels") or ())
        except Exception as exc:
            log.warning(f"A2A #4334: watchdog redispatch guard failed: {exc}")
            redo = {}
        if redo:
            out = dict(state or {})
            out.update(redo)
            out.setdefault("triggers", [redo.get("reason") or ""])
            out.setdefault("carrier_pr", 0)
            out.setdefault("folded", False)
            out.setdefault("conflict_scope", "")
            out.setdefault("manual_status", "")
            out.setdefault("manual_applied", False)
            out.setdefault("manual_required", False)
            return out
        return {}
    reason = TERMINAL_SKIP_REASON
    if state.get("intersect"):
        reason = f"{TERMINAL_SKIP_REASON}_intersect"
    manual_status = mark_issue_manual(repo_key, issue_number, reason)
    detail = _record_skip(repo_key, issue_number, target_agent, state)
    out = dict(state)
    out.update({"reason": reason, "detail": detail,
                "manual_status": manual_status,
                "manual_applied": manual_status == MANUAL_ISSUE_STATUS})
    log.info(f"A2A terminal guard: skip coding dispatch repo={repo_key} "
             f"issue=#{issue_number} {detail}")
    return out