"""组织需求跨仓下派编排（Issue #2112）。

职责：下派批次状态机、目标场景解析、子 Issue 建单（复用
``runtime public create_task_via_issue``）、单目标取消/重试、状态汇总 sweeper。

设计约束（见 docs/issue-2112/plan.md）：
- 建单不复制：一律经 ``create_task_via_issue``，内部身份 + 目标仓库 bot token；
- 幂等双保险：``UNIQUE(dispatch_id, repo_key)`` + 建单侧同名 title 查重；
- 状态推进全部条件更新，sweeper 与人工 cancel/retry 可安全并发。
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import uuid
from datetime import datetime, timedelta, timezone

from fastapi import HTTPException

from src.kernel.config import log
from src.kernel.issue_ident import native_issue_number, is_actionable_issue_number
from src.kernel.repo_key import from_repo_key, parse_repo_key, to_repo_key
from src.kernel.session_manager import db, db_lock
from src.domains.runtime import public as runtime_public
from src.domains.mgmt import public as mgmt_public

# dispatch 状态机
DISPATCH_IN_FLIGHT = ("dispatching", "running", "aggregating")
# Issue #2251：dispatch 终态集合（进入终态即触发一次组织聚合收口）
DISPATCH_TERMINAL = ("completed", "needs_review", "partial_failed", "failed", "cancelled")
# target 状态机
TARGET_IN_FLIGHT = ("pending", "creating", "running")
# Issue #2251：target 终态集合（needs_review = 子任务待人工审核，不再无限 running/超时）
TARGET_TERMINAL = ("completed", "needs_review", "failed", "cancelled")

# Issue #2251：完成判定口径
DONE_WHEN_ISSUE_DONE = "issue_done"
DONE_WHEN_PR_MERGED = "pr_merged"
DONE_WHEN_TASK_TERMINAL = "task_terminal"
DONE_WHEN_CHOICES = (DONE_WHEN_ISSUE_DONE, DONE_WHEN_PR_MERGED, DONE_WHEN_TASK_TERMINAL)
# 子任务按 task_terminal 判定时视为「任务已终态」的 agent_tasks.status
TASK_TERMINAL_STATUSES = ("completed", "failed", "timeout_failed", "cancelled")


# 单目标超时（秒）与 sweeper 周期（秒），环境变量可调
def _env_int(name: str, default: int) -> int:
    try:
        return max(1, int(os.environ.get(name, default) or default))
    except Exception:
        return default


TARGET_TIMEOUT_SECONDS = _env_int("ORG_DISPATCH_TARGET_TIMEOUT", 7200)
SWEEP_INTERVAL_SECONDS = _env_int("ORG_DISPATCH_SWEEP_INTERVAL", 60)
# 单条下派结论上下文上限（默认 64KB）：避免多目标 × 多角色的摘要挤占注入预算
DECISION_MAX_BYTES = _env_int("ORG_DECISION_MAX_BYTES", 65536)
MAX_DISPATCH_TARGETS = _env_int("ORG_DISPATCH_MAX_TARGETS", 20)

_DISPATCH_COLUMNS = (
    "id, org_id, org_issue_number, org_repo_key, scene_id, status, plan_json, "
    "context_json, context_hash, created_by, created_at, updated_at"
)
_TARGET_COLUMNS = (
    "id, dispatch_id, repo_key, scene_id, child_issue_number, child_task_id, status, "
    "done_when, pr_number, error, started_at, finished_at, created_at, updated_at"
)


# ---------------------------------------------------------------------------
# 基础工具
# ---------------------------------------------------------------------------

def _error(status: int, error_code: str, message: str) -> HTTPException:
    return HTTPException(
        status_code=status,
        detail={"code": status, "error_code": error_code, "message": message},
    )


def _row_dict(c, row) -> dict:
    return dict(zip([d[0] for d in c.description], row))


def _normalize_targets(targets: list[str]) -> list[str]:
    out: list[str] = []
    for raw in targets or []:
        key = to_repo_key(str(raw or "").strip())
        if key and key not in out:
            out.append(key)
    return out


def _normalize_target_scenes(raw: dict[str, str] | None, targets: list[str]) -> dict[str, str]:
    """只保留属于本批 targets 的非空场景推荐。"""
    allowed = set(targets)
    out: dict[str, str] = {}
    for key, value in (raw or {}).items():
        repo_key = to_repo_key(str(key or "").strip())
        scene_id = str(value or "").strip()
        if not repo_key or not scene_id:
            continue
        if repo_key not in allowed:
            log.warning(f"org-dispatch: ignore target_scenes for non-target {repo_key}")
            continue
        out[repo_key] = scene_id
    return out


def _normalize_scene_reasons(raw: dict[str, str] | None,
                             targets: list[str]) -> dict[str, str]:
    allowed = set(targets)
    out: dict[str, str] = {}
    for key, value in (raw or {}).items():
        repo_key = to_repo_key(str(key or "").strip())
        reason = str(value or "").strip()
        if repo_key in allowed and reason:
            out[repo_key] = reason[:500]
    return out


def _issue_url(repo_key: str, issue_number) -> str:
    from src.domains.mgmt import repo_web_urls

    return repo_web_urls.issue_url(repo_key, issue_number)


def _normalize_done_when(value: str, *, allow_default: bool = False) -> str:
    """归一 done_when；非法值在入口拒绝，存量空值按 issue_done 兜底。"""
    raw = str(value or "").strip().lower()
    if not raw and allow_default:
        return DONE_WHEN_ISSUE_DONE
    if raw not in DONE_WHEN_CHOICES:
        raise _error(400, "INVALID_DONE_WHEN",
                     f"done_when must be one of {'/'.join(DONE_WHEN_CHOICES)}")
    return raw


def _target_done_when(t: dict) -> str:
    raw = str((t or {}).get("done_when") or "").strip().lower()
    return raw if raw in DONE_WHEN_CHOICES else DONE_WHEN_ISSUE_DONE


def _terminal_status_for(done_when: str, *, issue_status: str, stage: str,
                         task_status: str, merged: bool) -> tuple[str, str]:
    """按 done_when 把子任务状态归一到 target 终态。

    返回 ``(status, error)``；status 为空串表示仍在途。纯函数，便于单测与回归。
    """
    issue_status = (issue_status or "").strip().lower()
    stage = (stage or "").strip().lower()
    task_status = (task_status or "").strip().lower()

    if issue_status == "cancelled":
        return "cancelled", ""
    # 等用户输入：子任务已停住，按待审终态收口（不再 2h 后误判 timeout）
    if issue_status == "awaiting_input":
        return "needs_review", ""

    if done_when == DONE_WHEN_PR_MERGED:
        if merged:
            return "completed", ""
        if issue_status == "completed":
            # 子 Issue 已结单（末节点 done）等价于合入证据
            return "completed", ""
        return "", ""

    if done_when == DONE_WHEN_TASK_TERMINAL:
        if task_status == "completed":
            return "completed", ""
        if task_status in ("failed", "timeout_failed"):
            return "failed", f"task_terminal: 子任务状态 {task_status}"
        if task_status == "cancelled":
            return "cancelled", ""
        if issue_status == "completed" or stage == "done":
            return "completed", ""
        if issue_status == "pending_review":
            return "needs_review", ""
        return "", ""

    # issue_done（默认）
    if issue_status == "completed" or stage == "done":
        return "completed", ""
    if issue_status == "pending_review":
        return "needs_review", ""
    return "", ""


def _extract_pr_number(payload: dict) -> int:
    """从 PR 事件 payload 提取 PR 号（兼容 GitHub/GitCode 结构）。"""
    if not isinstance(payload, dict):
        return 0
    for key in ("pull_request", "merge_request", "object_attributes"):
        obj = payload.get(key)
        if isinstance(obj, dict):
            for nk in ("number", "iid"):
                raw = obj.get(nk)
                try:
                    num = int(raw)
                except Exception:
                    num = 0
                if num > 0:
                    return num
    for nk in ("pr_number", "number"):
        try:
            num = int(payload.get(nk) or 0)
        except Exception:
            num = 0
        if num > 0:
            return num
    return 0


def _latest_task_status(c, repo_key: str, issue_number) -> str:
    """子 Issue 最新一条 agent_tasks.status（无任务返回空串）。"""
    try:
        return runtime_public.latest_task_status(repo_key, f"#{issue_number}")
    except Exception as exc:
        log.warning(f"org-dispatch: read latest task failed {repo_key}"
                    f"#{issue_number}: {exc}")
        return ""


def _pr_evidence(c, repo_key: str, issue_number) -> tuple[bool, int]:
    """子 Issue 的 PR 证据：返回 (是否已合入, PR 号)。

    合入判定只看真实 ``pull_request_merged`` 事件（VCS 权威），PR 号取最近一条
    opened/updated/merged 事件里的编号，供组织 Issue 评论展示。
    """
    try:
        rows = runtime_public.query_events_any(
            "event_type, payload",
            "repo_key=? AND issue_id=? AND event_type IN "
            "('pull_request_opened','pull_request_updated','pull_request_merged')",
            (repo_key, f"#{native_issue_number(issue_number)}"),
            "seq DESC", 20,
        )
    except Exception as exc:
        log.warning(f"org-dispatch: read PR events failed {repo_key}#{issue_number}: {exc}")
        return False, 0
    merged = False
    pr_number = 0
    for event_type, payload_raw in rows:
        try:
            payload = json.loads(payload_raw or "{}")
        except Exception:
            payload = {}
        num = _extract_pr_number(payload) if isinstance(payload, dict) else 0
        if event_type == "pull_request_merged":
            merged = True
            pr_number = pr_number or num
        else:
            pr_number = pr_number or num
    return merged, pr_number


def _repo_bot_token(repo_key: str) -> str:
    """取目标仓库 bot token（统一入口：provider 由 repo_key 派生、跳过 session 级）。

    不用 provider 二元分支：gitee/gitlab 等卡驱动平台同样能取到；也不走
    get_gitcode_token（第一优先级是 agent session token，scope 可能受限）。
    """
    try:
        from src.domains.mgmt.user.db import get_repo_bot_token

        return get_repo_bot_token(repo_key) or ""
    except Exception as exc:
        log.warning(f"org-dispatch: load bot token failed repo={repo_key}: {exc}")
        return ""


def _audit(event_type: str, actor_id: str, detail: dict) -> None:
    try:
        from src.domains.mgmt.auth.audit import log_audit

        log_audit(event_type, user_id=actor_id, operator_id=actor_id, detail=detail)
    except Exception as exc:
        log.warning(f"org-dispatch: audit {event_type} failed: {exc}")


# ---------------------------------------------------------------------------
# 查询
# ---------------------------------------------------------------------------

def _get_dispatch_row(c, dispatch_id: str) -> dict | None:
    c.execute(f"SELECT {_DISPATCH_COLUMNS} FROM grape_t_org_dispatch WHERE id=?", (dispatch_id,))
    row = c.fetchone()
    return _row_dict(c, row) if row else None


def _get_target_rows(c, dispatch_id: str) -> list[dict]:
    c.execute(
        f"SELECT {_TARGET_COLUMNS} FROM grape_t_org_dispatch_target WHERE dispatch_id=? "
        "ORDER BY created_at, repo_key",
        (dispatch_id,),
    )
    return [_row_dict(c, r) for r in c.fetchall()]


def _get_target_row(c, dispatch_id: str, repo_key: str) -> dict | None:
    c.execute(
        f"SELECT {_TARGET_COLUMNS} FROM grape_t_org_dispatch_target "
        "WHERE dispatch_id=? AND repo_key=?",
        (dispatch_id, repo_key),
    )
    row = c.fetchone()
    return _row_dict(c, row) if row else None


def _dispatch_payload(d: dict) -> dict:
    plan = {}
    try:
        plan = json.loads(d.get("plan_json") or "{}")
    except Exception:
        plan = {}
    context = {}
    try:
        context = json.loads(d.get("context_json") or "{}")
    except Exception:
        context = {}
    return {
        "dispatch_id": str(d["id"] or ""),
        "id": str(d["id"] or ""),
        "org_id": str(d["org_id"] or ""),
        "org_issue_number": native_issue_number(d["org_issue_number"]),
        "org_repo_key": d["org_repo_key"] or "",
        "scene_id": d["scene_id"] or "",
        "status": d["status"],
        "targets_requested": plan.get("targets") or [],
        "target_scenes": plan.get("target_scenes") or {},
        "scene_reasons": plan.get("scene_reasons") or {},
        "title": plan.get("title") or "",
        "context": context,
        "context_hash": d["context_hash"] or "",
        "created_by": str(d["created_by"] or ""),
        "created_at": d["created_at"],
        "updated_at": d["updated_at"],
    }


def _target_payload(t: dict, dispatch_payload: dict | None = None) -> dict:
    scene_id = t["scene_id"] or ""
    scene_name = ""
    if scene_id:
        try:
            from src.domains.mgmt.pipeline_config import get_pipeline_config

            cfg = get_pipeline_config(scene_id, repo_key=t["repo_key"]) or {}
            scene_name = str(cfg.get("display_name") or cfg.get("name") or "")
        except Exception:
            scene_name = ""
    payload = {
        "repo_key": t["repo_key"],
        "scene_id": scene_id,
        "scene_name": scene_name,
        "child_issue_number": native_issue_number(t["child_issue_number"]),
        "child_task_id": int(t["child_task_id"] or 0),
        "status": t["status"],
        "done_when": t.get("done_when") or DONE_WHEN_ISSUE_DONE,
        "pr_number": int(t["pr_number"] or 0),
        "error": t["error"] or "",
        "started_at": t["started_at"],
        "finished_at": t["finished_at"],
    }
    if dispatch_payload:
        requested = (dispatch_payload.get("target_scenes") or {}).get(t["repo_key"]) or ""
        reason = (dispatch_payload.get("scene_reasons") or {}).get(t["repo_key"]) or ""
        if requested:
            payload["scene_requested"] = requested
        if reason:
            payload["scene_reason"] = reason
    return payload


def get_dispatch_detail(org_id: str, dispatch_id: str) -> dict:
    with db_lock:
        c = db.cursor()
        d = _get_dispatch_row(c, dispatch_id)
        if not d or str(d["org_id"] or "") != str(org_id):
            raise _error(404, "ORG_REPO_NOT_FOUND", "Dispatch not found")
        targets = _get_target_rows(c, dispatch_id)
    payload = _dispatch_payload(d)
    payload["targets"] = [_target_payload(t, payload) for t in targets]
    return payload


def get_dispatch_snapshot(dispatch_id: str) -> dict:
    """只读快照（dispatch + targets），供聚合收口/引擎终态钩子复用。"""
    with db_lock:
        c = db.cursor()
        d = _get_dispatch_row(c, str(dispatch_id))
        if not d:
            return {}
        targets = _get_target_rows(c, str(dispatch_id))
    payload = _dispatch_payload(d)
    payload["targets"] = [_target_payload(t, payload) for t in targets]
    return payload


def list_dispatches(org_id: str, limit: int = 50, offset: int = 0,
                    org_issue_number: str = "") -> dict:
    limit = max(1, min(int(limit or 50), 200))
    offset = max(0, int(offset or 0))
    where = "org_id=?"
    params: list = [org_id]
    if str(org_issue_number or "").strip():
        where += " AND org_issue_number=?"
        params.append(str(org_issue_number).strip())
    with db_lock:
        c = db.cursor()
        c.execute(f"SELECT COUNT(*) FROM grape_t_org_dispatch WHERE {where}", tuple(params))
        total = int((c.fetchone() or [0])[0] or 0)
        c.execute(
            f"SELECT {_DISPATCH_COLUMNS} FROM grape_t_org_dispatch WHERE {where} "
            "ORDER BY created_at DESC, id DESC LIMIT ? OFFSET ?",
            tuple(params) + (limit, offset),
        )
        rows = [_row_dict(c, r) for r in c.fetchall()]
    items = []
    counts: dict[str, int] = {}
    if rows:
        ids = [str(d["id"] or "") for d in rows]
        placeholders = ",".join("?" * len(ids))
        with db_lock:
            c = db.cursor()
            c.execute(
                "SELECT dispatch_id, COUNT(*) FROM grape_t_org_dispatch_target "
                f"WHERE dispatch_id IN ({placeholders}) GROUP BY dispatch_id",
                tuple(ids),
            )
            counts = {str(r[0] or ""): int(r[1] or 0) for r in c.fetchall()}
    for d in rows:
        item = _dispatch_payload(d)
        item["target_count"] = counts.get(str(d["id"] or ""), 0)
        items.append(item)
    return {"items": items, "total": total}


# ---------------------------------------------------------------------------
# 场景解析
# ---------------------------------------------------------------------------

def _dispatch_plan(dispatch: dict) -> dict:
    try:
        plan = json.loads(dispatch.get("plan_json") or "{}")
        return plan if isinstance(plan, dict) else {}
    except Exception:
        return {}


def _requested_target_scene(dispatch: dict, repo_key: str) -> str:
    """单个 target 的显式场景：per-target 优先，批次级 scene_id 兼容兜底。"""
    plan = _dispatch_plan(dispatch)
    target_scenes = plan.get("target_scenes") or {}
    if isinstance(target_scenes, dict):
        picked = str(target_scenes.get(repo_key) or "").strip()
        if picked:
            return picked
    return str(dispatch.get("scene_id") or "").strip()


def _resolve_target_scene_detail(repo_key: str, requested_scene_id: str = "",
                                 *, user: dict | None = None) -> dict:
    """解析目标仓库场景；显式推荐不可执行时回退默认/唯一场景。"""
    from src.domains.mgmt.org_dispatch_scene import resolve_target_scene_detail

    return resolve_target_scene_detail(repo_key, requested_scene_id, user=user)


def _resolve_target_scene(repo_key: str, requested_scene_id: str = "",
                          *, user: dict | None = None) -> str:
    """4 档解析目标仓库场景；解析不到返回空串（兼容旧调用方）。"""
    resolved = _resolve_target_scene_detail(repo_key, requested_scene_id, user=user)
    return str(resolved.get("scene_id") or "")


# ---------------------------------------------------------------------------
# 建单（复用 create_task_via_issue）
# ---------------------------------------------------------------------------

def _child_description(org_repo_key: str, org_issue_number, org_title: str,
                       description: str, context: dict | str | None, *,
                       org_id: str = "", dispatch_id: str = "") -> str:
    full = from_repo_key(org_repo_key)
    lines = [
        f"组织需求来源：{full}#{native_issue_number(org_issue_number)}",
        _issue_url(org_repo_key, org_issue_number),
    ]
    if org_title:
        lines.append(f"组织需求标题：{org_title}")
    if description:
        lines.extend(["", "## 目标范围", description.strip()])
    if org_id and dispatch_id:
        lines.extend([
            "",
            "## 组织上下文",
            "组织硬规则、组织简介/架构、目标仓库目录、需求 brief、下派计划和验收结论"
            "已生成该批次上下文快照，并物化到工作区 `.org-context/`；需要完整内容时执行：",
            "",
            "```bash",
            f'grape org dispatches context --org_id "{org_id}" '
            f"--dispatch_id {dispatch_id} --json-out",
            "```",
            "",
            "优先级：组织硬规则 > 本次任务明确要求 > 目标仓库目录规则 > "
            "目标仓库仓库级规则 > 组织通用规范 > 组织知识/历史经验。",
            "组织硬规则不可被仓库规则或任务要求覆盖；目标仓库自己的 "
            "project-info / 仓库知识仍然有效。",
        ])
    lines.extend(["", "本 Issue 由组织跨仓下派自动创建。"])
    return "\n".join(lines)


def _target_scope_md(org_id: str, repo_key: str) -> str:
    """从仓库地图生成目标仓专属职责段，避免每个子单拿到完全相同的泛化描述。"""
    try:
        from src.domains.mgmt.org_repo_map import get_map

        m = get_map(org_id, repo_key) or {}
    except Exception:
        return ""
    if not m or m.get("status") != "ready":
        return ""
    lines = [
        "",
        "## 本仓职责（仓库地图）",
        f"- 定位：{m.get('purpose') or '（未填写）'}",
    ]
    if m.get("interfaces"):
        lines.append("- 对外接口：" + ", ".join(
            f"`{x}`" for x in (m.get("interfaces") or [])[:8]))
    if m.get("not_owns"):
        lines.append("- 不负责：" + "、".join(
            str(x) for x in (m.get("not_owns") or [])[:6]))
    if m.get("keywords"):
        lines.append("- 能力关键词：" + "、".join(
            str(x) for x in (m.get("keywords") or [])[:10]))
    lines.append("- 只改本仓职责内的部分；跨仓依赖通过组织上下文协调，不要越界修改其他仓库。")
    return "\n".join(lines)


async def _create_child_issue(dispatch: dict, target: dict, *, title: str,
                              org_title: str = "",
                              description: str, context: dict | str | None,
                              actor_id: str, operator_name: str,
                              snapshot_id: int = 0,
                              actor_user: dict | None = None) -> dict:
    """为单个目标创建/复用子 Issue，返回更新后的 target 行。"""
    repo_key = target["repo_key"]
    requested_scene = _requested_target_scene(dispatch, repo_key)
    import asyncio
    resolved = await asyncio.to_thread(
        _resolve_target_scene_detail, repo_key, requested_scene, user=actor_user)
    scene_id = str(resolved.get("scene_id") or "")
    if not scene_id:
        _warnings = [str(w) for w in (resolved.get("warnings") or []) if str(w).strip()]
        _detail = "；".join(_warnings[:3])
        _msg = "NO_TARGET_SCENE: Target repository has no available scene"
        if _detail:
            _msg = f"{_msg}（{_detail}）"
        return _mark_target_failed(target["id"], _msg[:500])
    log.info(
        f"org-dispatch: target scene resolved dispatch={dispatch.get('id')} "
        f"repo={repo_key} requested={requested_scene or '-'} scene={scene_id} "
        f"source={resolved.get('source') or '-'}")

    token = _repo_bot_token(repo_key)
    if not token:
        return _mark_target_failed(target["id"], "BOT_TOKEN_MISSING: 目标仓库 Bot Token 未配置")

    child_title = f"[Org #{native_issue_number(dispatch['org_issue_number'])}] {title}"
    scope_md = _target_scope_md(str(dispatch.get("org_id") or ""), repo_key)
    child_description = (description or "")
    if scope_md:
        child_description = child_description.rstrip() + "\n" + scope_md
    body = {
        "repo_key": repo_key,
        "title": child_title,
        "description": _child_description(
            dispatch["org_repo_key"], native_issue_number(dispatch["org_issue_number"]),
            org_title or title, child_description, context,
            org_id=str(dispatch.get("org_id") or ""),
            dispatch_id=str(dispatch.get("id") or "")),
        "assignee": "",
        "scene_id": scene_id,
        "is_subtask_mode": 0,
        "org_context_snapshot_id": str(snapshot_id or ""),
    }
    _, owner, _ = parse_repo_key(repo_key)
    internal_user = {
        "id": actor_id,
        "username": owner or operator_name,
        "token": token,
        "auth_type": "internal",
    }

    created = True
    try:
        from src.kernel.otel import feature_span

        with feature_span("org.dispatch.create_child", {
                "repo_key": repo_key,
                "dispatch_id": str(dispatch.get("id") or ""),
                "org_issue": native_issue_number(dispatch.get("org_issue_number")),
        }):
            result = await runtime_public.create_task_via_issue(body, user=internal_user)
        # gitee 的 issue 标识是字符串（IK...）：保持原生形态，禁止 int() 强转。
        issue_number = native_issue_number(result.get("issue_number"))
        created = bool(result.get("created", True))
        task_id = int(result.get("task_id") or result.get("issue_id") or 0)
    except HTTPException as exc:
        detail = exc.detail if isinstance(exc.detail, dict) else {}
        reused = detail.get("issue_number") if exc.status_code == 409 else None
        if not reused:
            msg = detail.get("message") or str(exc.detail or exc)
            return _mark_target_failed(target["id"], f"CREATE_FAILED: {msg}"[:500])
        issue_number = native_issue_number(reused)
        task_id = 0
        created = False
        log.info(f"org-dispatch: reuse child issue #{issue_number} repo={repo_key} "
                 f"(duplicate title guard)")
    except Exception as exc:
        log.warning(f"org-dispatch: create child issue failed repo={repo_key}: {exc}")
        return _mark_target_failed(target["id"], f"CREATE_FAILED: {exc}"[:500])

    if not is_actionable_issue_number(issue_number):
        return _mark_target_failed(target["id"], "CREATE_FAILED: 建单结果缺少 issue_number")

    with db_lock:
        c = db.cursor()
        c.execute(
            "UPDATE grape_t_org_dispatch_target SET scene_id=?, child_issue_number=?, child_task_id=?, "
            "status='running', error='', started_at=datetime('now'), finished_at=NULL, "
            "updated_at=datetime('now') "
            "WHERE id=? AND status IN ('pending','creating','running')",
            (scene_id, issue_number, task_id, target["id"]),
        )
        db.commit()
        row = _get_target_row(c, str(dispatch["id"] or ""), repo_key)

    if row and row.get("status") == "cancelled":
        # 并发取消已胜出：刚建出的子单不在下派范围内，best-effort 回滚避免孤儿在途
        try:
            if task_id:
                runtime_public.cancel_issue_core(task_id, user_id=None)
        except Exception as exc:
            log.warning(f"org-dispatch: cancel orphan child {repo_key}#{issue_number} "
                        f"failed: {exc}")
        return row

    # 子单侧可读标记（跨仓权威关联仍在 grape_t_org_dispatch_target）
    try:
        from src.api.session_store import set_parent_issue

        set_parent_issue(repo_key, f"#{issue_number}", f"#{native_issue_number(dispatch['org_issue_number'])}")
    except Exception as exc:
        log.warning(f"org-dispatch: set parent marker failed repo={repo_key} "
                    f"issue=#{issue_number}: {exc}")

    _audit("target_created", actor_id, {
        "org_id": str(dispatch["org_id"] or ""),
        "dispatch_id": str(dispatch["id"] or ""),
        "repo_key": repo_key,
        "scene_id": scene_id,
        "child_issue_number": issue_number,
        "reused": not created,
    })
    return row or target


def _mark_target_failed(target_id: str, error: str) -> dict:
    with db_lock:
        c = db.cursor()
        c.execute(
            "UPDATE grape_t_org_dispatch_target SET status='failed', error=?, "
            "finished_at=datetime('now'), updated_at=datetime('now') WHERE id=?",
            (error, target_id),
        )
        db.commit()
        c.execute(f"SELECT {_TARGET_COLUMNS} FROM grape_t_org_dispatch_target WHERE id=?", (target_id,))
        row = c.fetchone()
        return _row_dict(c, row)


# ---------------------------------------------------------------------------
# 创建下派
# ---------------------------------------------------------------------------

def _lookup_org_issue(org_id: str, org_issue_number) -> dict | None:
    """组织需求必须位于该组织关联仓库（org_id 权威来自 managed_repos）。

    组织仓库（``is_org_repo=1``）优先：同一组织下多个关联仓库常见相同
    issue_number（各仓 #1），按 full_name 顺序遍历会先命中子仓库，导致批次
    ``org_repo_key`` 指向子仓库——聚合评论/状态归一就写到子仓 Issue 上。
    """
    from src.domains.mgmt.user.org_db import list_org_repos

    num = native_issue_number(org_issue_number)
    repos = list_org_repos(org_id) or []
    repos.sort(key=lambda r: (
        0 if str(r.get("is_org_repo") or "0") in ("1", "True", "true") else 1,
        str(r.get("full_name") or ""),
    ))
    for repo in repos:
        repo_key = str(repo.get("repo_key") or "")
        if not repo_key:
            continue
        row = runtime_public.issue_brief(repo_key, num)
        if row:
            return row
    return None


def _validate_targets(org_id: str, targets: list[str]) -> None:
    from src.domains.mgmt.user.org_db import get_managed_repo_binding

    for repo_key in targets:
        binding = get_managed_repo_binding(repo_key)
        if not binding or str(binding.get("org_id") or "") != str(org_id):
            raise _error(400, "INVALID_TARGET",
                         f"Target repository is not linked to this organization: {from_repo_key(repo_key)}")


def _idempotency_key(org_id: str, org_issue_number, targets: list[str]) -> str:
    """批次级幂等键：同一组织需求 + 同一目标集合（顺序无关）唯一。"""
    payload = f"{str(org_id)}:{native_issue_number(org_issue_number)}:{','.join(sorted(targets))}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _find_dispatch_by_key(c, idempotency_key: str) -> dict | None:
    """按幂等键回查批次（唯一索引冲突后的权威复用路径）。"""
    if not idempotency_key:
        return None
    c.execute(
        f"SELECT {_DISPATCH_COLUMNS} FROM grape_t_org_dispatch "
        "WHERE idempotency_key=? ORDER BY created_at DESC, id DESC LIMIT 1",
        (idempotency_key,),
    )
    row = c.fetchone()
    return _row_dict(c, row) if row else None


def _find_same_dispatch(c, org_id: str, org_issue_number,
                        targets: list[str]) -> dict | None:
    """按目标集合回查既有批次（幂等键为 NULL 的存量数据兼容路径）。"""
    wanted = set(targets)
    c.execute(
        f"SELECT {_DISPATCH_COLUMNS} FROM grape_t_org_dispatch "
        "WHERE org_id=? AND org_issue_number=? ORDER BY created_at DESC, id DESC",
        (str(org_id), native_issue_number(org_issue_number)),
    )
    rows = [_row_dict(c, r) for r in c.fetchall()]
    for row in rows:
        try:
            plan = json.loads(row.get("plan_json") or "{}")
        except Exception:
            continue
        if set(plan.get("targets") or []) == wanted:
            return row
    return None


async def create_dispatch(org_id: str, *, org_issue_number, targets: list[str],
                          context: dict | str | None, title: str, description: str,
                          scene_id: str, user: dict,
                          target_scenes: dict[str, str] | None = None,
                          scene_reasons: dict[str, str] | None = None,
                          done_when: str = DONE_WHEN_ISSUE_DONE) -> tuple[dict, int]:
    """创建下派批次并逐目标建子 Issue。返回 (payload, http_status)。"""
    targets = _normalize_targets(targets)
    org_issue_number = native_issue_number(org_issue_number)
    if not is_actionable_issue_number(org_issue_number):
        raise _error(400, "INVALID_TARGET", "org_issue_number is required")
    if len(targets) > MAX_DISPATCH_TARGETS:
        raise _error(
            400, "TOO_MANY_TARGETS",
            f"targets exceed limit {MAX_DISPATCH_TARGETS}; split the requirement or reduce targets",
        )
    done_when = _normalize_done_when(done_when, allow_default=True)

    org_issue = _lookup_org_issue(org_id, org_issue_number)
    if not org_issue:
        raise _error(400, "INVALID_TARGET",
                     f"Organization requirement #{org_issue_number} does not belong to this organization")

    routing = None
    if not targets:
        # 全自动派发：无显式 targets 时按仓库地图召回目标仓库，不等待人工选择。
        from src.domains.mgmt.org_repo_map import DEFAULT_RECALL_LIMIT, recall_targets

        _q = " ".join(str(x or "") for x in (
            title, description, org_issue.get("title"),
            org_issue.get("task_description"),
        )).strip()
        routing = recall_targets(org_id, _q, limit=DEFAULT_RECALL_LIMIT)
        targets = _normalize_targets(routing.get("targets") or [])
        log.info(
            f"org dispatch auto-route org={org_id} issue={org_issue_number} "
            f"targets={targets} fallback={routing.get('fallback')} "
            f"needs_review={routing.get('needs_review')}")
    if not targets:
        _hint = ""
        if routing:
            _names = [
                str(c.get("full_name") or c.get("repo_key") or "")
                for c in (routing.get("candidates") or [])[:3]
                if c.get("full_name") or c.get("repo_key")
            ]
            if _names:
                _hint = "; candidates: " + ", ".join(_names)
        raise _error(
            400, "NO_CONFIDENT_TARGET",
            "No target repository met the confidence threshold; specify targets explicitly or enrich the repository map" + _hint,
        )
    _validate_targets(org_id, targets)
    target_scenes = _normalize_target_scenes(target_scenes, targets)
    scene_reasons = _normalize_scene_reasons(scene_reasons, targets)

    actor_id = str((user or {}).get("id") or "")
    operator_name = str((user or {}).get("username") or "")
    org_title = (org_issue.get("title") or "").strip()
    plan_title = (title or org_title or f"组织需求 #{native_issue_number(org_issue_number)}").strip()
    context = context or {}
    context_json = json.dumps(context, ensure_ascii=False, sort_keys=True)
    _plan_obj = {
        "targets": targets,
        "title": plan_title,
        "org_title": org_title,
        "description": description or "",
        "scene_id": scene_id or "",
        "target_scenes": target_scenes,
        "scene_reasons": scene_reasons,
    }
    if routing:
        _plan_obj["routing"] = routing
    plan_json = json.dumps(_plan_obj, ensure_ascii=False, sort_keys=True)
    idem_key = _idempotency_key(org_id, native_issue_number(org_issue_number), targets)

    # 幂等查重建在同一临界区内完成：db_lock 收口同进程 check-then-insert 竞态，
    # idempotency_key 唯一索引兜底跨进程并发（冲突时回滚并回读复用）。
    reused: dict | None = None
    dispatch_id = ""
    with db_lock:
        c = db.cursor()
        existing = _find_dispatch_by_key(c, idem_key) or _find_same_dispatch(
            c, org_id, native_issue_number(org_issue_number), targets)
        if existing:
            reused = existing
        else:
            dispatch_id = uuid.uuid4().hex
            try:
                c.execute(
                    "INSERT INTO grape_t_org_dispatch "
                    "(id, org_id, org_issue_number, org_repo_key, scene_id, "
                    "status, plan_json, context_json, context_hash, created_by, idempotency_key) "
                    "VALUES (?,?,?,?,?,'planning',?,?,?,?,?)",
                    (dispatch_id, str(org_id), native_issue_number(org_issue_number),
                     org_issue.get("repo_key") or "", scene_id or "", plan_json, context_json,
                     hashlib.sha256(context_json.encode("utf-8")).hexdigest(), actor_id,
                     idem_key),
                )
                for repo_key in targets:
                    c.execute(
                        "INSERT INTO grape_t_org_dispatch_target "
                        "(id, dispatch_id, repo_key, status, done_when) "
                        "VALUES (?,?,?,'pending',?)",
                        (uuid.uuid4().hex, dispatch_id, repo_key, done_when),
                    )
                c.execute(
                    "UPDATE grape_t_org_dispatch SET status='dispatching', updated_at=datetime('now') "
                    "WHERE id=?",
                    (dispatch_id,),
                )
                db.commit()
            except Exception as exc:
                # 唯一键冲突（跨进程并发）→ 回滚后按幂等键回读既有批次
                db.rollback()
                existing = _find_dispatch_by_key(c, idem_key) or _find_same_dispatch(
                    c, org_id, native_issue_number(org_issue_number), targets)
                if not existing:
                    raise
                log.info(f"org-dispatch: idempotency key conflict, reuse "
                         f"dispatch #{existing['id']}: {exc}")
                reused = existing

    if reused:
        if reused["status"] in DISPATCH_IN_FLIGHT:
            raise _error(409, "DISPATCH_IN_PROGRESS",
                         "A dispatch with the same target set is already running; check it later")
        log.info(f"org-dispatch: reuse terminal dispatch #{reused['id']} "
                 f"status={reused['status']}")
        from src.kernel import org_metrics
        org_metrics.observe_dispatch("reused", str(reused.get("status") or ""))
        return get_dispatch_detail(org_id, str(reused["id"] or "")), 200

    with db_lock:
        c = db.cursor()
        dispatch = _get_dispatch_row(c, dispatch_id)
        target_rows = _get_target_rows(c, dispatch_id)

    # 先构建组织上下文快照（L0/L1/L2 目录/L3/知识），子任务通过
    # `grape org dispatches context` 按需读取；快照失败不阻断下派。
    try:
        from src.domains.mgmt.org_context_snapshot import build_org_snapshot

        _snap_payload = _dispatch_payload(dispatch or {})
        _snap_payload["targets_requested"] = targets
        _snap_id = build_org_snapshot(org_id, _snap_payload)
        if _snap_id and dispatch is not None:
            dispatch["_snapshot_id"] = _snap_id
    except Exception as _se:
        log.warning(f"org-dispatch: build context snapshot failed dispatch={dispatch_id}: {_se}")

    _audit("dispatch_created", actor_id, {
        "org_id": str(org_id),
        "dispatch_id": dispatch_id,
        "org_issue_number": native_issue_number(org_issue_number),
        "org_repo_key": org_issue.get("repo_key") or "",
        "targets": targets,
        "scene_id": scene_id or "",
        "target_scenes": target_scenes,
    })

    for target in target_rows:
        try:
            await _create_child_issue(
                dispatch, target, title=plan_title, org_title=org_title,
                description=description, context=context, actor_id=actor_id,
                operator_name=operator_name,
                snapshot_id=str((dispatch or {}).get("_snapshot_id") or ""),
                actor_user=user,
            )
        except Exception as exc:
            log.warning(f"org-dispatch: target {target['repo_key']} failed: {exc}")
            try:
                _mark_target_failed(str(target["id"] or ""), f"CREATE_FAILED: {exc}"[:500])
            except Exception:
                pass

    _aggregate_dispatch(dispatch_id, actor_id=actor_id)
    from src.kernel import org_metrics
    org_metrics.observe_dispatch("created", "dispatching")
    return get_dispatch_detail(org_id, dispatch_id), 201


# ---------------------------------------------------------------------------
# 汇总
# ---------------------------------------------------------------------------

def _aggregate_dispatch(dispatch_id: str, *, actor_id: str = "") -> str:
    """按 target 状态汇总 dispatch；已 cancelled 不再覆盖。"""
    with db_lock:
        c = db.cursor()
        dispatch = _get_dispatch_row(c, dispatch_id)
        if not dispatch:
            return ""
        current = dispatch["status"]
        if current == "cancelled":
            return current
        targets = _get_target_rows(c, dispatch_id)
        statuses = [t["status"] for t in targets]
        if not statuses:
            new_status = current
        elif all(s == "failed" for s in statuses):
            new_status = "failed"
        elif any(s == "failed" for s in statuses):
            # 有失败即 partial_failed（不因其他目标仍在 running 而回退为 running）
            new_status = "partial_failed"
        elif any(s in TARGET_IN_FLIGHT for s in statuses):
            new_status = "running"
        elif all(s == "cancelled" for s in statuses):
            new_status = "cancelled"
        elif any(s == "cancelled" for s in statuses):
            # 部分取消（其余为 completed 等终态）：不宣称整批已取消，否则与仍在
            # 运行/已完成的目标漂移，且 sweeper 不再收敛
            new_status = "partial_failed"
        elif all(s == "completed" for s in statuses):
            new_status = "completed"
        elif any(s == "needs_review" for s in statuses):
            # Issue #2251：待人工审核是终态，不再等超时；有 failed 已在前序分支兜住
            new_status = "needs_review"
        else:
            # 未知 target 状态不得静默判完成（否则会提前触发终态聚合收口）：
            # 归 needs_review 交人工确认并告警，等状态机收敛后再汇总。
            log.warning(f"org-dispatch: unknown target statuses dispatch={dispatch_id} "
                        f"statuses={statuses} → needs_review")
            new_status = "needs_review"

        if new_status != current:
            c.execute(
                "UPDATE grape_t_org_dispatch SET status=?, updated_at=datetime('now') WHERE id=?",
                (new_status, dispatch_id),
            )
            db.commit()

    if new_status != current and new_status in ("completed", "partial_failed", "failed"):
        event = "dispatch_completed" if new_status == "completed" else "dispatch_partial_failed"
        _audit(event, actor_id, {
            "org_id": str(dispatch["org_id"] or ""),
            "dispatch_id": str(dispatch_id),
            "status": new_status,
            "targets": {t["repo_key"]: t["status"] for t in targets},
        })
    elif new_status != current and new_status == "needs_review":
        # Issue #2251：待人工审核收口留痕（区别于失败/完成）
        _audit("dispatch_needs_review", actor_id, {
            "org_id": str(dispatch["org_id"] or ""),
            "dispatch_id": str(dispatch_id),
            "status": new_status,
            "targets": {t["repo_key"]: t["status"] for t in targets},
        })
    elif new_status != current and new_status == "cancelled":
        # 子任务在外部被逐个取消时，汇总侧也留痕（内部 cancel 已显式记过，此处不会重复）
        _audit("dispatch_cancelled", actor_id, {
            "org_id": str(dispatch["org_id"] or ""),
            "dispatch_id": str(dispatch_id),
            "status": new_status,
            "targets": {t["repo_key"]: t["status"] for t in targets},
        })
    return new_status


def _target_has_inflight(c, repo_key: str, issue_id: str) -> bool:
    """目标 Issue 是否仍有在途任务/事件（用于避免把正在重试的目标误判失败）。"""
    try:
        return runtime_public.has_inflight(repo_key, issue_id)
    except Exception:
        return True


def _latest_failed_a2a(c, repo_key: str, issue_id: str) -> str:
    """最新事件是 a2a_event 且失败时返回可读错误；否则返回空串。

    A2A 派发失败时子 Issue 会停在 processing/issue（没有下游 task），只看 Issue
    状态会让组织批次永久 running。这里把失败事件翻译成 target 失败，让批次能收口。
    """
    try:
        event = runtime_public.latest_event_brief(repo_key, issue_id)
    except Exception:
        return ""
    if not event:
        return ""
    if event["event_type"] != "a2a_event" or event["status"] != "failed":
        return ""
    text = event["result"].strip() or "A2A dispatch failed"
    return f"A2A_DISPATCH_FAILED(seq={event['seq']}): {text[:300]}"


def _refresh_targets_from_issues(dispatch_id: str) -> None:
    """读子 Issue 状态并按 target.done_when 推进 target 终态（Issue #2251）。

    - 终态集合：``completed / needs_review / failed / cancelled``；
    - 默认 ``issue_done``：子 Issue completed/stage=done → completed；
      pending_review → needs_review（不再无限 running，也不被 2h 超时误判 failed）；
    - ``awaiting_input`` 一律 needs_review（子任务已停住等用户）；
    - ``pr_merged``：仅 pull_request_merged 事件（或子 Issue 已 completed）→ completed；
    - ``task_terminal``：最新 agent_tasks 终态 → completed/failed/cancelled，
      未达任务终态但子 Issue pending_review → needs_review。
    """
    with db_lock:
        c = db.cursor()
        targets = _get_target_rows(c, dispatch_id)
        for t in targets:
            if t["status"] not in TARGET_IN_FLIGHT:
                continue
            if not native_issue_number(t["child_issue_number"]):
                continue
            repo_key = t["repo_key"]
            issue_number = native_issue_number(t["child_issue_number"])
            done_when = _target_done_when(t)
            issue = runtime_public.issue_brief(repo_key, issue_number) or {}
            issue_status = issue.get("status") or ""
            stage = issue.get("stage") or ""
            task_status = _latest_task_status(c, repo_key, issue_number)
            merged = False
            pr_number = int(t["pr_number"] or 0)
            if done_when == DONE_WHEN_PR_MERGED:
                merged, pr_number_seen = _pr_evidence(c, repo_key, issue_number)
                pr_number = pr_number or pr_number_seen

            terminal, error = _terminal_status_for(
                done_when, issue_status=issue_status, stage=stage,
                task_status=task_status, merged=merged)
            if not terminal:
                # A2A 失败会把子 Issue 卡在 processing/issue（没有下游 task），
                # 只靠 issue 状态永远等不到终态。最新事件失败且没有在途重试时，
                # 把 target 标失败，让 dispatch 进入 partial_failed/failed 并触发聚合。
                _a2a_err = _latest_failed_a2a(c, repo_key, f"#{issue_number}")
                if _a2a_err and not _target_has_inflight(c, repo_key, f"#{issue_number}"):
                    terminal = "failed"
                    error = _a2a_err
            if not terminal and task_status in ("failed", "timeout_failed", "cancelled"):
                # issue_done 只看 issue 状态时，QA/coding 任务失败会把 target 拖到 2h
                # 超时。最新任务已失败且没有在途重试时直接收口，让批次可以聚合。
                if not _target_has_inflight(c, repo_key, f"#{issue_number}"):
                    terminal = "cancelled" if task_status == "cancelled" else "failed"
                    error = f"task_terminal: 子任务状态 {task_status}"
            if not terminal:
                if pr_number and pr_number != int(t["pr_number"] or 0):
                    c.execute(
                        "UPDATE grape_t_org_dispatch_target SET pr_number=?, "
                        "updated_at=datetime('now') WHERE id=?",
                        (pr_number, t["id"]),
                    )
                continue
            c.execute(
                "UPDATE grape_t_org_dispatch_target SET status=?, error=?, pr_number=?, "
                "finished_at=COALESCE(finished_at, datetime('now')), "
                "updated_at=datetime('now') "
                "WHERE id=? AND status IN ('pending','creating','running')",
                (terminal, error, pr_number, t["id"]),
            )
        db.commit()


def _timeout_targets(dispatch_id: str) -> None:
    cutoff = (
        datetime.now(timezone.utc) - timedelta(seconds=TARGET_TIMEOUT_SECONDS)
    ).strftime("%Y-%m-%d %H:%M:%S")
    with db_lock:
        c = db.cursor()
        # pending 目标无 started_at（建单中途进程重启会永久停留），以其 created_at 起算
        # 超时；creating/running 仍以 started_at 起算。COALESCE 统一两类基准。
        #
        # Issue #2251：done_when=pr_merged 的目标等的是人工合入，可能长时间没有
        # pull_request_merged 事件；超时不得判 failed（与「2h 后误判 failed」同类），
        # 归 needs_review 并注明等待合入超时，交人工确认/补合入事件。
        c.execute(
            "UPDATE grape_t_org_dispatch_target SET status='needs_review', "
            "error='timeout: 等待合入超时，待人工确认', finished_at=datetime('now'), "
            "updated_at=datetime('now') "
            "WHERE dispatch_id=? AND status IN ('pending','creating','running') "
            "AND COALESCE(NULLIF(done_when,''),'issue_done')='pr_merged' "
            "AND COALESCE(started_at, created_at) <= ?",
            (dispatch_id, cutoff),
        )
        c.execute(
            "UPDATE grape_t_org_dispatch_target SET status='failed', "
            "error='timeout: 超过单目标最长执行时间', finished_at=datetime('now'), "
            "updated_at=datetime('now') "
            "WHERE dispatch_id=? AND status IN ('pending','creating','running') "
            "AND COALESCE(NULLIF(done_when,''),'issue_done')<>'pr_merged' "
            "AND COALESCE(started_at, created_at) <= ?",
            (dispatch_id, cutoff),
        )
        db.commit()


def _terminal_dispatches_pending_aggregate(c) -> list[int]:
    """终态批次中尚未完成聚合触发的 dispatch id（幂等键 dispatch_id+terminal_status）。

    除「从未抢占」外，也纳入「已抢占但事件 seq 仍为 0」的台账行：进程若在
    收口/触发之间中断，下一次 sweep 可续做；重复执行仍由事件幂等键与评论
    flag 去重。聚合评论实际发送失败（``comment:`` 前缀 error）的行也纳入补发，
    避免组织 Issue 状态已归一但永久缺聚合评论。
    """
    placeholders = ",".join("?" * len(DISPATCH_TERMINAL))
    c.execute(
        "SELECT d.id FROM grape_t_org_dispatch d "
        "LEFT JOIN grape_t_org_dispatch_aggregate a "
        "  ON a.dispatch_id=d.id AND a.terminal_status=d.status "
        f"WHERE d.status IN ({placeholders}) "
        "AND (a.id IS NULL OR a.seq=0 "
        "     OR (a.comment_posted=0 AND a.error LIKE 'comment:%'))",
        tuple(DISPATCH_TERMINAL),
    )
    return [str(r[0] or "") for r in c.fetchall()]


def _claim_terminal_aggregate(dispatch_id: str, terminal_status: str) -> bool:
    """抢占 (dispatch_id, terminal_status) 幂等键；唯一索引保证只成功一次。

    已存在但 ``seq=0``（抢占后进程中断，事件尚未落库）时允许续做：事件本身
    仍受 ``org_agg:<dispatch_id>:<status>`` 幂等键保护，评论受 comment_posted
    flag 保护，因此重复进入不会产生第二条事件/评论。聚合评论发送失败
    （``comment:`` 前缀 error）时同样允许续做，只补评论、事件撞幂等键去重。
    """
    with db_lock:
        c = db.cursor()
        try:
            c.execute(
                "INSERT OR IGNORE INTO grape_t_org_dispatch_aggregate "
                "(id, dispatch_id, terminal_status, status) VALUES (?,?,?,'pending')",
                (uuid.uuid4().hex, str(dispatch_id), str(terminal_status)),
            )
            claimed = c.rowcount == 1
            if not claimed:
                row = c.execute(
                    "SELECT seq, comment_posted, error FROM grape_t_org_dispatch_aggregate "
                    "WHERE dispatch_id=? AND terminal_status=?",
                    (str(dispatch_id), str(terminal_status)),
                ).fetchone()
                claimed = bool(row) and (
                    int(row[0] or 0) <= 0
                    or (int(row[1] or 0) == 0
                        and str(row[2] or "").startswith("comment:"))
                )
            db.commit()
        except Exception as exc:
            db.rollback()
            log.warning(f"org-dispatch: claim aggregate failed dispatch={dispatch_id} "
                        f"status={terminal_status}: {exc}")
            return False
    return claimed


def _update_aggregate_record(dispatch_id: str, terminal_status: str, **fields) -> None:
    if not fields:
        return
    cols = ", ".join(f"{k}=?" for k in fields)
    values = list(fields.values()) + [str(dispatch_id), str(terminal_status)]
    try:
        with db_lock:
            c = db.cursor()
            c.execute(
                f"UPDATE grape_t_org_dispatch_aggregate SET {cols}, updated_at=datetime('now') "
                "WHERE dispatch_id=? AND terminal_status=?",
                tuple(values),
            )
            db.commit()
    except Exception as exc:
        log.warning(f"org-dispatch: update aggregate record failed dispatch={dispatch_id}: {exc}")


def _maybe_trigger_terminal_aggregate(dispatch_id: str, status: str) -> None:
    """Issue #2251：批次进入终态时幂等触发一次组织聚合收口。

    1. 抢占幂等键 ``(dispatch_id, terminal_status)``，重复 sweep/多实例只成功一次；
    2. 服务端确定性收口（组织 Issue 评论 + 状态归一，未全终态时 no-op）；
    3. 持久化一条 ``aggregate_recheck`` 的 queued a2a_event，由巡检 reconcile 派发
       org_aggregate 复核（进程重启不丢，失败走既有退避重排）。
    """
    if status not in DISPATCH_TERMINAL:
        return
    from src.kernel import org_metrics
    org_metrics.observe_dispatch("terminal", status)
    if not _claim_terminal_aggregate(dispatch_id, status):
        return
    with db_lock:
        c = db.cursor()
        dispatch = _get_dispatch_row(c, dispatch_id)
        targets = _get_target_rows(c, dispatch_id)
    if not dispatch:
        return
    record = {"status": "finalized", "error": ""}
    try:
        finalized = runtime_public.finalize_org_issue_aggregation(
            dispatch, targets, status)
        record["comment_posted"] = 1 if finalized.get("comment_posted") else 0
        record["issue_status"] = str(finalized.get("issue_status") or "")
        record["status"] = str(finalized.get("action") or "finalized")
        # 评论发送失败时记录可重试标记（下一轮 sweep 补发），避免永久缺聚合评论。
        record["error"] = str(finalized.get("comment_error") or "")[:500]
    except Exception as exc:
        log.warning(f"org-dispatch: finalize org issue failed dispatch={dispatch_id}: {exc}")
        record["error"] = str(exc)[:500]
    try:
        triggered = runtime_public.trigger_org_aggregate(dispatch, targets, status)
        if triggered.get("seq"):
            record["seq"] = int(triggered["seq"])
    except Exception as exc:
        log.warning(f"org-dispatch: trigger aggregate failed dispatch={dispatch_id}: {exc}")
        record["error"] = (record.get("error") or "") or str(exc)[:500]
    _update_aggregate_record(dispatch_id, status, **record)
    _record_dispatch_decision(dispatch, targets, status, record)


def _target_task_digests(target: dict) -> list[tuple[dict, dict]]:
    """读取单个目标子任务的结构化产出并提炼为上下文摘要。

    一个子 Issue 常有 coding/qa/sre 多次执行：分别保留「改了什么 / 验证了什么 /
    部署了什么」，只取最新一条会丢掉实现细节。
    """
    from src.kernel.context_digest import build_digest

    t = target or {}
    repo_key = str(t.get("repo_key") or "")
    issue_no = native_issue_number(t.get("child_issue_number"))
    issue_ref = f"#{issue_no}" if issue_no else ""
    memories = runtime_public.get_issue_task_digests(repo_key, issue_ref, limit=3)
    task_ref = str(t.get("child_task_id") or "")
    if task_ref.startswith("t_"):
        selected = runtime_public.get_latest_task_digest(repo_key, "", task_id=task_ref)
        if selected and selected.get("task_id") not in {
                m.get("task_id") for m in memories}:
            memories.insert(0, selected)
    if not memories:
        latest = runtime_public.get_latest_task_digest(repo_key, issue_ref)
        if latest:
            memories = [latest]
    out: list[tuple[dict, dict]] = []
    for memory in memories:
        digest = build_digest(
            memory.get("output"),
            title="",
            agent_type=memory.get("agent_type") or "",
            status=memory.get("status") or str(t.get("status") or ""),
            task_id=memory.get("task_id") or task_ref,
            errors=str(t.get("error") or memory.get("errors") or ""),
        )
        out.append((memory, digest))
    return out


def _digest_pr_refs(digests: list[dict]) -> list[str]:
    """从子任务产物里提取 PR 号（target.pr_number 在合入前可能仍为 0）。"""
    refs: list[str] = []
    for digest in digests:
        for item in digest.get("artifacts") or []:
            for num in re.findall(
                    r"(?i)(?:PR\s*#?|merge_requests?/)(\d+)", str(item)):
                if num not in refs:
                    refs.append(num)
    return refs


def _short_text(value: object, limit: int = 280) -> str:
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    return text[:limit - 1].rstrip() + "…" if len(text) > limit else text


def _clean_requirement_text(value: object, limit: int = 320) -> str:
    lines: list[str] = []
    for raw in str(value or "").splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.startswith("目标仓库（targets）") or line.startswith("只允许下派"):
            continue
        lines.append(line)
    return _short_text(" ".join(lines), limit)


def _record_dispatch_decision(dispatch: dict, targets: list[dict],
                              status: str, record: dict) -> None:
    """下派聚合完成后自动沉淀一条 decision 上下文（Issue #2269）。

    不只记录「是否完成」：从每个目标子任务的结构化产出中提取结论、关键动作、
    验证证据、产物、风险/后续与可复用经验。上下文是有限预算，摘要按字段截断，
    不重放执行日志。幂等 key=dispatch-<id>；失败仅告警，不影响聚合主流程。
    """
    try:
        from src.domains.mgmt import org_context_store as _ocs
        from src.kernel.context_digest import render_digest

        org = dispatch or {}
        org_id = str(org.get("org_id") or "")
        dispatch_id = str(org.get("id") or "")
        if not org_id or not dispatch_id:
            return
        issue_no = native_issue_number(org.get("org_issue_number"))
        title = f"下派 #{dispatch_id} 结论"
        try:
            plan = json.loads(org.get("plan_json") or "{}")
        except Exception:
            plan = {}
        try:
            dispatch_ctx = json.loads(org.get("context_json") or "{}")
        except Exception:
            dispatch_ctx = {}
        if not isinstance(plan, dict):
            plan = {}
        if not isinstance(dispatch_ctx, dict):
            dispatch_ctx = {}

        target_info: list[tuple[dict, list[tuple[dict, dict]]]] = []
        for t in targets or []:
            target_info.append((t, _target_task_digests(t)))

        key_lines: list[str] = []
        req_title = _short_text(plan.get("title") or dispatch_ctx.get("summary") or "")
        if req_title:
            key_lines.append(f"- 需求：{req_title}")
        req_desc = _clean_requirement_text(plan.get("description") or "", 320)
        if req_desc and req_desc != req_title:
            key_lines.append(f"- 需求描述：{req_desc}")
        goal = _short_text(dispatch_ctx.get("summary") or "", 280)
        if goal and goal != req_title:
            key_lines.append(f"- 目标：{goal}")
        acceptance = dispatch_ctx.get("acceptance") or []
        if isinstance(acceptance, str):
            acceptance = [acceptance]
        acceptance = [_short_text(x, 180) for x in acceptance if str(x or "").strip()]
        if acceptance:
            key_lines.append("- 验收：" + "；".join(acceptance[:8]))
        constraints = dispatch_ctx.get("constraints") or []
        if isinstance(constraints, str):
            constraints = [constraints]
        constraints = [_short_text(x, 180) for x in constraints if str(x or "").strip()]
        if constraints:
            key_lines.append("- 约束：" + "；".join(constraints[:8]))
        decisions = dispatch_ctx.get("decisions") or []
        if isinstance(decisions, str):
            decisions = [decisions]
        decisions = [_short_text(x, 180) for x in decisions if str(x or "").strip()]
        if decisions:
            key_lines.append("- 设计决策：" + "；".join(decisions[:8]))
        elif target_info:
            # 下派 context 没带 decisions 时，用 issue 角色的 Spec/范围结论补一条
            # 设计摘要，避免关键信息只剩需求与验收。
            _scope_notes: list[str] = []
            for _t, _memories in target_info:
                for _m, _dg in _memories:
                    if _dg.get("agent_type") == "issue" and _dg.get("conclusion"):
                        _note = _short_text(_dg["conclusion"], 220)
                        if _note and _note not in _scope_notes:
                            _scope_notes.append(_note)
            if _scope_notes:
                key_lines.append("- 设计/范围：" + "；".join(_scope_notes[:2]))

        lines = [f"# 下派 #{dispatch_id} 结论（{status}）", ""]
        if key_lines:
            lines += ["## 关键信息"] + key_lines + [""]
        lines += [
            "## 批次",
            f"- 组织需求：#{issue_no}",
            f"- 组织仓库：`{org.get('org_repo_key') or ''}`",
            f"- 目标仓库数：{len(targets or [])}",
            f"- 聚合状态：{(record or {}).get('status') or '-'}"
            f"；组织 Issue 状态：{(record or {}).get('issue_status') or '-'}",
            f"- 聚合评论：{'已写入' if (record or {}).get('comment_posted') else '未写入'}",
        ]
        if (record or {}).get("error"):
            lines.append(f"- 错误：{(record or {}).get('error')}")
        if targets:
            lines += ["", "## 目标一览",
                      "| 目标仓库 | 子 Issue | PR | 状态 | 错误 |",
                      "|---|---|---|---|---|"]
            for t, memories in target_info:
                _ci = native_issue_number((t or {}).get("child_issue_number"))
                _pr = int((t or {}).get("pr_number") or 0)
                _refs = _digest_pr_refs([d for _m, d in memories])
                _pr_text = (f"#{_pr}" if _pr else
                            (", ".join(f"#{n}" for n in _refs) if _refs else "-"))
                lines.append(
                    f"| `{(t or {}).get('repo_key') or ''}` | "
                    f"{('#' + str(_ci)) if _ci else '-'} | "
                    f"{_pr_text} | "
                    f"{(t or {}).get('status') or '-'} | "
                    f"{((t or {}).get('error') or '').replace('|', '/')[:120]} |")

        lines += ["", "## 目标执行摘要"]
        any_digest = False
        reusable: list[str] = []
        for t, memories in target_info:
            t = t or {}
            repo_key = str(t.get("repo_key") or "")
            _ci = native_issue_number(t.get("child_issue_number"))
            _pr = int(t.get("pr_number") or 0)
            refs = [
                f"子 Issue：{'#' + str(_ci) if _ci else '-'}",
                f"状态：{t.get('status') or '-'}",
            ]
            if _pr:
                refs.append(f"PR：#{_pr}")
            else:
                _refs = _digest_pr_refs([d for _m, d in memories])
                if _refs:
                    refs.append("PR：" + ", ".join(f"#{n}" for n in _refs))
            task_ids = [str(m.get("task_id") or "") for m, _ in memories
                        if m.get("task_id")]
            if task_ids:
                refs.append("任务：" + ", ".join(task_ids[:3]))
            lines += ["", f"### `{repo_key}`", "- " + " / ".join(refs)]
            rendered_any = False
            for idx, (_memory, digest) in enumerate(memories):
                # 历史角色的 next_step（如 issue→a2a_coding）已过期，只在最新
                # 一张卡片保留「执行时后续」，避免把旧动作误当当前风险。
                if idx > 0:
                    digest = dict(digest, next_step="")
                rendered = render_digest(digest, heading_level=4)
                if rendered:
                    rendered_any = True
                    any_digest = True
                    lines += ["", rendered]
                reusable.extend(digest.get("reusable") or [])
            if not rendered_any:
                lines += ["", "（子任务未返回结构化结论，仅保留状态）"]

        if reusable:
            seen: set[str] = set()
            uniq: list[str] = []
            for item in reusable:
                key = item.strip().lower()
                if key and key not in seen:
                    seen.add(key)
                    uniq.append(item.strip())
            lines += ["", "## 跨仓可复用经验"]
            lines += [f"- {item}" for item in uniq[:12]]

        lines += ["", "> 自动沉淀自子任务结构化产出（结论/关键动作/验证/产物/风险）。"]
        content = "\n".join(lines)
        _encoded = content.encode("utf-8")
        if len(_encoded) > DECISION_MAX_BYTES:
            content = _encoded[:DECISION_MAX_BYTES].decode("utf-8", "ignore") \
                + "\n\n> （单条上下文超出上限，已截断；完整记录见子任务与事件）"
        _ocs.upsert_context(
            org_id, "decision", f"dispatch-{dispatch_id}", title, content,
            owner_id="", user_id="")

        # 有真实执行信息时同步沉淀一条组织知识，供后续检索/注入；
        # 只有状态、没有结构化产出的批次不写知识，避免污染知识库。
        if any_digest:
            try:
                from src.domains.mgmt.org_knowledge import upsert_knowledge

                # 知识库只保留「结论 + 可复用经验」，不重复 decision 的动作/产物全量，
                # 避免同一批次在 DB 和仓库镜像里双份膨胀。
                k_lines = [
                    f"# 下派 #{dispatch_id} 经验沉淀（{status}）", "",
                    f"- 组织需求：#{issue_no}", "",
                    "## 目标结论",
                ]
                for t, memories in target_info:
                    repo_key = str((t or {}).get("repo_key") or "")
                    for _memory, digest in memories:
                        concl = (digest.get("conclusion")
                                 or digest.get("issue_summary") or "")
                        if concl:
                            k_lines.append(
                                f"- `{repo_key}` / {digest.get('agent_type') or '-'}: "
                                f"{concl[:240]}")
                if reusable:
                    k_lines += ["", "## 可复用经验"]
                    k_lines += [f"- {item}" for item in reusable[:12]]
                knowledge_content = "\n".join(k_lines).strip()
                if knowledge_content:
                    upsert_knowledge(
                        org_id, org.get("org_repo_key") or "",
                        kind="retrospective",
                        name=f"下派 #{dispatch_id} 经验沉淀",
                        description="组织下派跨仓执行经验（自动沉淀）",
                        content=knowledge_content,
                        tags=["org", "dispatch", "retrospective"],
                        applies_to="org", owner_id="", created_by="",
                        source="agent", publish=False)
            except Exception as ke:
                log.warning(f"record dispatch knowledge failed dispatch={dispatch_id}: {ke}")
        # 防止上下文随运行次数无限增长：热记忆保留最近 N 条，旧的压缩进滚动归档。
        try:
            from src.domains.mgmt.org_context_retention import compact_org_memory

            compact_org_memory(org_id)
        except Exception as ce:
            log.warning(f"compact org memory failed org={org_id}: {ce}")
    except Exception as e:
        log.warning(f"record dispatch decision failed dispatch={(dispatch or {}).get('id')}: {e}")


def sweep_once() -> int:
    """推进所有非终态下派批次；返回处理的 dispatch 数（供测试/运维手动触发）。"""
    with db_lock:
        c = db.cursor()
        # 非终态批次 + 仍有在途目标的批次（partial_failed 后其他目标仍需继续汇总）
        c.execute(
            "SELECT id FROM grape_t_org_dispatch WHERE status IN ('dispatching','running','aggregating') "
            "UNION "
            "SELECT DISTINCT dispatch_id FROM grape_t_org_dispatch_target "
            "WHERE status IN ('pending','creating','running')"
        )
        dispatch_ids = [str(r[0] or "") for r in c.fetchall()]
        # Issue #2251：已进入终态但尚未触发聚合收口的批次
        pending_aggregate = _terminal_dispatches_pending_aggregate(c)
    from src.kernel import org_metrics
    org_metrics.set_dispatch_inflight(len(dispatch_ids))
    for dispatch_id in dispatch_ids:
        try:
            _refresh_targets_from_issues(dispatch_id)
            _timeout_targets(dispatch_id)
            status = _aggregate_dispatch(dispatch_id)
            _maybe_trigger_terminal_aggregate(dispatch_id, status)
        except Exception as exc:
            log.warning(f"org-dispatch sweep failed dispatch={dispatch_id}: {exc}")
    for dispatch_id in pending_aggregate:
        try:
            with db_lock:
                c = db.cursor()
                row = _get_dispatch_row(c, dispatch_id)
            if not row:
                continue
            _maybe_trigger_terminal_aggregate(dispatch_id, row["status"])
        except Exception as exc:
            log.warning(f"org-dispatch aggregate trigger failed dispatch={dispatch_id}: {exc}")
    return len(set(dispatch_ids) | set(pending_aggregate))


async def org_dispatch_sweep_loop() -> None:
    """Mgmt 进程内周期汇总循环（stop 时被 cancel）。"""
    import asyncio

    log.info(f"Org dispatch sweeper loop started (interval={SWEEP_INTERVAL_SECONDS}s)")
    while True:
        await asyncio.sleep(SWEEP_INTERVAL_SECONDS)
        try:
            await asyncio.to_thread(sweep_once)
        except Exception as exc:
            log.warning(f"org-dispatch sweep error: {exc}")
        try:
            from src.domains.mgmt.org_retention import maybe_purge_expired_org_records

            await asyncio.to_thread(maybe_purge_expired_org_records)
        except Exception as exc:
            log.warning(f"org-dispatch retention error: {exc}")


# ---------------------------------------------------------------------------
# 取消 / 重试
# ---------------------------------------------------------------------------

def cancel_dispatch(org_id: str, dispatch_id: str, *, user: dict) -> dict:
    with db_lock:
        c = db.cursor()
        dispatch = _get_dispatch_row(c, dispatch_id)
        if not dispatch or str(dispatch["org_id"] or "") != str(org_id):
            raise _error(404, "ORG_REPO_NOT_FOUND", "Dispatch not found")
        if dispatch["status"] == "cancelled":
            return {"ok": True, "already_cancelled": True,
                    "dispatch_id": dispatch_id, "status": "cancelled"}
        if dispatch["status"] not in DISPATCH_IN_FLIGHT:
            raise _error(400, "INVALID_TARGET", "Dispatch cannot be cancelled in its current state")
        targets = _get_target_rows(c, dispatch_id)

    cancelled = 0
    unresolved: list[str] = []
    for t in targets:
        if t["status"] not in ("pending", "creating", "running"):
            continue
        has_child = bool(native_issue_number(t["child_issue_number"]))
        task_id = int(t["child_task_id"] or 0)
        if not task_id and has_child:
            _issue = runtime_public.issue_brief(
                t["repo_key"], native_issue_number(t["child_issue_number"])) or {}
            task_id = int(_issue.get("id") or 0)
        if task_id:
            try:
                runtime_public.cancel_issue_core(task_id, user_id=None)
                cancelled += 1
            except HTTPException as exc:
                log.warning(f"org-dispatch: cancel child {t['repo_key']} "
                            f"#{t['child_issue_number']} rejected: {exc.detail}")
                unresolved.append(t["repo_key"])
                continue
            except Exception as exc:
                log.warning(f"org-dispatch: cancel child failed {t['repo_key']}: {exc}")
                unresolved.append(t["repo_key"])
                continue
        elif t["status"] in ("creating", "running") and has_child:
            # 在途子单却定位不到任务：无法确认已停止，按未取消处理，交 sweeper 收敛
            log.warning(f"org-dispatch: no task resolvable for in-flight target "
                        f"{t['repo_key']}#{t['child_issue_number']}")
            unresolved.append(t["repo_key"])
            continue
        with db_lock:
            c = db.cursor()
            c.execute(
                "UPDATE grape_t_org_dispatch_target SET status='cancelled', "
                "finished_at=datetime('now'), updated_at=datetime('now') "
                "WHERE id=? AND status IN ('pending','creating','running')",
                (t["id"],),
            )
            db.commit()

    actor_id = str((user or {}).get("id") or "")

    if unresolved:
        # 仍有目标未能取消（子单已完成 / 取消失败 / 任务不可定位）：绝不置 cancelled，
        # 否则接口谎报已取消、target 与 dispatch 长期漂移且 sweeper 不再收敛。
        _refresh_targets_from_issues(dispatch_id)
        status = _aggregate_dispatch(dispatch_id, actor_id=actor_id)
        log.warning(f"org-dispatch: cancel #{dispatch_id} held, unresolved={unresolved}")
        return {
            "ok": False,
            "dispatch_id": dispatch_id,
            "status": status,
            "cancelled_children": cancelled,
            "unresolved_targets": unresolved,
        }

    with db_lock:
        c = db.cursor()
        c.execute(
            "UPDATE grape_t_org_dispatch SET status='cancelled', updated_at=datetime('now') WHERE id=?",
            (dispatch_id,),
        )
        db.commit()
    _audit("dispatch_cancelled", actor_id, {
        "org_id": str(org_id),
        "dispatch_id": str(dispatch_id),
        "cancelled_children": cancelled,
        "targets": [t["repo_key"] for t in targets],
    })
    from src.kernel import org_metrics
    org_metrics.observe_dispatch("cancelled", "cancelled")
    return {"ok": True, "dispatch_id": dispatch_id, "status": "cancelled",
            "cancelled_children": cancelled}


def _cancel_inflight_agents(repo_key: str, issue_number) -> int:
    """终止目标子 Issue 上 queued/dispatched/running 的 agent 任务（不置 issue 取消）。

    重试前调用：先停掉旧执行再重触发，避免「超时但原 agent 仍在跑」时新老两次执行
    并发改同一仓库。语义与 ``cancel_issue_core`` 的 agent 终止段一致，但不标记 Issue
    终态（重试要复用该 Issue）。
    """
    if not native_issue_number(issue_number):
        return 0
    issue_key = f"#{native_issue_number(issue_number)}"
    try:
        running = runtime_public.inflight_tasks(repo_key, issue_key)
    except Exception:
        running = []
    for at_id, at_seq in running:
        try:
            runtime_public.cancel_task(at_id)
        except Exception:
            pass
        try:
            if at_seq:
                runtime_public.cancel_direct_task(at_seq)
        except Exception:
            pass
        try:
            if at_seq:
                runtime_public.update_event_status(at_seq, "cancelled")
        except Exception:
            pass
    if running:
        try:
            runtime_public.cancel_inflight_tasks(repo_key, issue_key)
        except Exception:
            pass
    return len(running)


def _trigger_child_issue(repo_key: str, issue_number, scene_id: str) -> dict:
    """重新触发已存在子 Issue 的目标仓库场景。

    复用 pipeline 入口（issue_opened 原生 payload + 内部触发标记），不新建 Issue；
    先清该 Issue 的活跃守卫，避免重试被旧 running 事件挡住。事件头与载荷按平台卡
    的 `webhook_dialect` 派生（GitHub/GitLab/Gitee 各有自己的事件族），不再按
    provider 二元分支写死 X-GitHub-Event / X-Gitcode-Event。
    """
    provider, owner, name = parse_repo_key(repo_key)
    num = native_issue_number(issue_number)
    try:
        runtime_public.clear_event_active_guard(repo_key, f"#{num}")
    except Exception:
        pass
    card = None
    try:
        card = runtime_public.get_platform_card(provider)
    except Exception as exc:  # noqa: BLE001 - 卡缺失按不可触发处理，不误用别家协议
        log.warning(f"org-dispatch: platform card lookup failed provider={provider!r}: {exc}")
    dialect = str(getattr(card, "webhook_dialect", "") or "").strip().lower()
    header_name = str(getattr(card, "event_header", "") or "").strip()
    if not header_name or dialect not in ("github_web", "gitee_v5", "gitlab_v4"):
        log.warning(f"org-dispatch: cannot trigger child issue provider={provider!r} "
                    f"dialect={dialect!r} (platform card missing/unsupported)")
        return {"ok": False, "seq": 0,
                "reason": "platform_card_webhook_dialect_unavailable"}
    if dialect == "github_web":
        payload = {
            "action": "opened",
            "issue": {"number": num, "title": "", "user": {}},
            "repository": {"full_name": f"{owner}/{name}"},
            "sender": {},
            "_grape_internal_trigger": True,
        }
        header_value = "issues"
    elif dialect == "gitlab_v4":
        payload = {
            "object_kind": "issue",
            "object_attributes": {"iid": num, "title": "", "action": "open",
                                  "state": "opened"},
            "project": {"path_with_namespace": f"{owner}/{name}"},
            "user": {},
            "_grape_internal_trigger": True,
        }
        header_value = "Issue Hook"
    else:
        payload = {
            "object_kind": "issue",
            "object_attributes": {"iid": num, "title": "", "action": "open"},
            "project": {"path_with_namespace": f"{owner}/{name}"},
            "user": {},
            "_grape_internal_trigger": True,
        }
        header_value = "issue_opened"
    headers = {"Content-Type": "application/json", header_name: header_value}
    result = runtime_public.run_pipeline(
        json.dumps(payload).encode(), headers,
        account=repo_key, repo_key_override=repo_key)
    return {"ok": bool(result.get("ok")), "seq": int(result.get("seq") or 0)}


def _claim_failed_target_for_retry(target_id: str, next_status: str) -> bool:
    """仅允许一个重试请求抢占失败目标，返回是否由本请求抢占成功。"""
    if next_status not in ("creating", "running"):
        raise ValueError(f"unsupported retry status: {next_status}")
    with db_lock:
        c = db.cursor()
        c.execute(
            "UPDATE grape_t_org_dispatch_target SET status=?, error='', "
            "started_at=datetime('now'), finished_at=NULL, updated_at=datetime('now') "
            "WHERE id=? AND status='failed'",
            (next_status, str(target_id)),
        )
        claimed = c.rowcount == 1
        db.commit()
        return claimed


async def retry_target(org_id: str, dispatch_id: str, repo_key: str, *, user: dict) -> dict:
    """重试单个失败目标；已建子 Issue 一律复用，不新建 Issue。"""
    repo_key = to_repo_key(str(repo_key or "").strip())
    with db_lock:
        c = db.cursor()
        dispatch = _get_dispatch_row(c, dispatch_id)
        if not dispatch or str(dispatch["org_id"] or "") != str(org_id):
            raise _error(404, "ORG_REPO_NOT_FOUND", "Dispatch not found")
        target = _get_target_row(c, dispatch_id, repo_key)
        if not target:
            raise _error(404, "ORG_REPO_NOT_FOUND", "Target repository is not in this dispatch")
    if dispatch["status"] == "cancelled":
        raise _error(400, "INVALID_TARGET", "Cancelled dispatches cannot be retried")
    if target["status"] != "failed":
        raise _error(400, "INVALID_TARGET", "Only failed targets can be retried")

    actor_id = str((user or {}).get("id") or "")
    plan = {}
    try:
        plan = json.loads(dispatch.get("plan_json") or "{}")
    except Exception:
        plan = {}
    context = {}
    try:
        context = json.loads(dispatch.get("context_json") or "{}")
    except Exception:
        context = {}

    if native_issue_number(target["child_issue_number"]):
        issue_number = native_issue_number(target["child_issue_number"])
        scene_id = str(target["scene_id"] or "")
        if not scene_id:
            _requested = _requested_target_scene(dispatch, repo_key)
            import asyncio
            _resolved = await asyncio.to_thread(
                _resolve_target_scene_detail, repo_key, _requested, user=user)
            scene_id = str(_resolved.get("scene_id") or "")
        if not scene_id:
            _mark_target_failed(str(target["id"] or ""), "NO_TARGET_SCENE: Target repository has no available scene")
            _aggregate_dispatch(dispatch_id, actor_id=actor_id)
            raise _error(400, "NO_TARGET_SCENE", "Target repository has no available scene")
        if not _claim_failed_target_for_retry(str(target["id"] or ""), "running"):
            raise _error(409, "TARGET_RETRY_IN_PROGRESS", "Target retry is already in progress")
        # 先终止原在途 agent，避免新老两次执行并发改同一仓库
        _cancel_inflight_agents(repo_key, issue_number)
        _issue = runtime_public.issue_brief(repo_key, issue_number) or {}
        with db_lock:
            c = db.cursor()
            c.execute(
                "UPDATE grape_t_org_dispatch_target SET scene_id=?, "
                "child_task_id=COALESCE(NULLIF(child_task_id, 0), ?), "
                "updated_at=datetime('now') WHERE id=? AND status='running'",
                (scene_id, int(_issue.get("id") or 0), str(target["id"] or "")),
            )
            db.commit()
        try:
            from src.api.session_store import set_issue_scene

            set_issue_scene(repo_key, f"#{issue_number}", scene_id)
        except Exception as exc:
            log.warning(f"org-dispatch: retry set scene failed {repo_key}#{issue_number}: {exc}")
        try:
            ret = _trigger_child_issue(repo_key, issue_number, scene_id)
        except Exception as exc:
            log.warning(f"org-dispatch: retrigger child {repo_key}#{issue_number} failed: {exc}")
            _mark_target_failed(str(target["id"] or ""), f"TRIGGER_FAILED: {exc}"[:500])
        else:
            if not (ret or {}).get("ok"):
                log.warning(f"org-dispatch: retrigger child {repo_key}#{issue_number} "
                            f"not accepted: {ret}")
                _mark_target_failed(str(target["id"] or ""),
                                    "TRIGGER_FAILED: 目标仓库流水线未接收触发")
    else:
        # 未建子单（NO_TARGET_SCENE / BOT_TOKEN_MISSING）：重走建单，复用同一 target 行
        if not _claim_failed_target_for_retry(str(target["id"] or ""), "creating"):
            raise _error(409, "TARGET_RETRY_IN_PROGRESS", "Target retry is already in progress")
        claimed_target = dict(target)
        claimed_target["status"] = "creating"
        await _create_child_issue(
            dispatch, claimed_target,
            title=plan.get("title") or f"组织需求 #{native_issue_number(dispatch['org_issue_number'])}",
            org_title=plan.get("org_title") or "",
            description=plan.get("description") or "", context=context,
            actor_id=actor_id,
            operator_name=str((user or {}).get("username") or ""),
            actor_user=user,
        )

    _aggregate_dispatch(dispatch_id, actor_id=actor_id)
    detail = get_dispatch_detail(org_id, dispatch_id)
    refreshed = next((t for t in detail["targets"] if t["repo_key"] == repo_key), None)
    _audit("target_retried", actor_id, {
        "org_id": str(org_id),
        "dispatch_id": str(dispatch_id),
        "repo_key": repo_key,
        "child_issue_number": native_issue_number((refreshed or {}).get("child_issue_number")),
    })
    from src.kernel import org_metrics
    org_metrics.observe_dispatch("retried", str(detail.get("status") or ""))
    return {"ok": True, "target": refreshed, "dispatch_status": detail["status"]}
