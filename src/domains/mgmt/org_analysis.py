"""组织仓库 AI 分析编排（Issue #1820 后续增强）。

职责：
- 关联仓库后触发一次 AI 分析（单仓）；
- 组织仓库每日 0 点任务：分析全部关联仓库（无关联仓库时跳过）；
- 组织下派前读取分析结果（由 org_repo_map.analysis_context 提供）。

执行载体复用 Autopilot 定时任务：
- 每个组织在组织仓库上有一条受管任务（name=__org_analysis__:<slug>）；
- 单仓/全量通过 trigger_task_now_with_params 传入本次运行参数；
- 实际 AI 推理由运行时引擎执行 ``org-repo-analyzer`` 技能完成，结果通过
  ``POST /api/orgs/{id}/repo-map/{repo_key}/ai-result`` 回传。
"""
from __future__ import annotations

import asyncio
import json
import re
import threading
from datetime import datetime, timezone

from src.domains.mgmt.org_repo_map import (
    analysis_context as _analysis_context,
    ensure_ai_table,
    get_ai_status,
    mark_ai_status,
    save_ai_result,
)
from src.domains.mgmt.user.org_db import db, db_lock, get_org, list_org_repos
from src.domains.runtime import public as runtime_public
from src.kernel.config import log

ANALYSIS_CRON = "0 0 * * *"
ANALYSIS_SKILL = "org-repo-analyzer"
MANAGED_TASK_PREFIX = "__org_analysis__:"
MANAGED_FIELDS = [
    {"name": "GRAPE_MANAGED_BY", "value": "org-analysis"},
]

# Autopilot manual trigger 是 async；mgmt 侧的绑定钩子是同步线程，后台线程调用。
# 用常驻事件循环而不是 asyncio.run：trigger_task_now 里 create_task(_execute_task)
# 需要循环在返回后继续存活，否则任务会被取消。
_TRIGGER_LOOP = None
_TRIGGER_LOCK = threading.Lock()


def _trigger_loop():
    global _TRIGGER_LOOP
    with _TRIGGER_LOCK:
        if _TRIGGER_LOOP is None or _TRIGGER_LOOP.is_closed():
            _TRIGGER_LOOP = asyncio.new_event_loop()
            threading.Thread(
                target=_TRIGGER_LOOP.run_forever,
                name="org-analysis-trigger",
                daemon=True,
            ).start()
        return _TRIGGER_LOOP


def _trigger_task(task_id: int, params: dict) -> dict:
    fut = asyncio.run_coroutine_threadsafe(
        runtime_public.trigger_task_now_with_params(task_id, params), _trigger_loop())
    try:
        return fut.result(timeout=20)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "message": f"trigger failed: {exc}"}


def _safe_slug(value: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9_.-]+", "-", str(value or "").strip()).strip("-")
    return slug[:64] or "org"


def managed_task_name(org: dict) -> str:
    return MANAGED_TASK_PREFIX + _safe_slug(str(org.get("slug") or org.get("id") or "org"))


def _managed_custom_fields(org_id: str, mode: str = "all") -> str:
    fields = list(MANAGED_FIELDS) + [
        {"name": "GRAPE_ORG_ID", "value": str(org_id)},
        {"name": "GRAPE_ANALYSIS_MODE", "value": mode},
    ]
    return json.dumps(fields, ensure_ascii=False)


def _next_run(cron_expr: str) -> str:
    try:
        from croniter import croniter

        now = datetime.now().astimezone()
        nxt = croniter(cron_expr, now).get_next(datetime)
        if nxt.tzinfo is None:
            nxt = nxt.astimezone()
        return nxt.isoformat()
    except Exception:
        return ""


def _analysis_instruction(org_id: str) -> str:
    return (
        "对组织关联的全部代码仓库执行 AI 能力分析（只读）。\n"
        f"组织 ID：{org_id}\n"
        "执行步骤：\n"
        "1. 调用 GET $GRAPE_SERVER_URL/api/orgs/{org_id}/repo-analysis/targets?mode=all "
        "获取目标仓库列表；若为空（组织仅剩组织自动仓库），直接输出 SKIPPED: no linked repos 并结束。\n"
        "2. 对每个目标仓库调用 GET $GRAPE_SERVER_URL/api/orgs/{org_id}/repo-analysis/context/{repo_key} "
        "获取分析上下文。\n"
        "3. 按 org-repo-analyzer 技能要求产出结构化 JSON，并 POST 到 "
        "$GRAPE_SERVER_URL/api/orgs/{org_id}/repo-map/{repo_key}/ai-result。\n"
        "4. 覆盖旧结果；不要修改仓库代码、不要建 Issue/PR。\n"
        "若「本次运行参数」给出 mode=single/repo_key，以参数为准，只分析该仓库。"
    )


def ensure_analysis_task(org_id: str, *, actor_id: str | int = 0) -> int:
    """幂等创建/校正组织仓库上的每日分析任务，返回 task_id（0=不适用）。"""
    org = get_org(str(org_id)) or {}
    if not org or org.get("is_personal"):
        return 0
    org_repo_key = str(org.get("org_repo_key") or "")
    if not org_repo_key:
        return 0
    ensure_ai_table()
    name = managed_task_name(org)
    now = datetime.now(timezone.utc).isoformat()
    next_run = _next_run(ANALYSIS_CRON)
    instruction = _analysis_instruction(str(org_id))
    custom_fields = _managed_custom_fields(str(org_id), "all")
    engine_ids: list[int] = []
    owner_id = str(org.get("owner_id") or "")
    try:
        _uid = int(owner_id) if owner_id.isdigit() else 0
    except (TypeError, ValueError):
        _uid = 0
    try:
        from src.domains.mgmt.pipeline_config import get_scene_engine_ids

        scene_id = str(org.get("org_scene_id") or "")
        if scene_id:
            engine_ids = list(get_scene_engine_ids(
                scene_id, "qa", repo_key=org_repo_key, user_id=_uid) or [])
    except Exception as exc:  # noqa: BLE001
        log.debug(f"org_analysis: scene engine ids unavailable org={org_id}: {exc}")
    engine_ids_json = json.dumps(engine_ids) if engine_ids else ""
    # created_by 是 INTEGER 列（技能解析/ACCOUNT 用）；UUID 用户无法安全落库时退 0，
    # 官方技能仍可按名字解析，不依赖 created_by 的 user scope。
    created_by = _uid

    with db_lock:
        c = db.cursor()
        row = c.execute(
            "SELECT id, cron, skill_name, enabled FROM autopilot_tasks "
            "WHERE repo_key=? AND name=? AND skill_name=?",
            (org_repo_key, name, ANALYSIS_SKILL),
        ).fetchone()
        if row:
            task_id = int(row[0])
            c.execute(
                "UPDATE autopilot_tasks SET instruction=?, cron=?, enabled=1, "
                "next_run_at=?, custom_fields=?, engine_runtime_ids=?, updated_at=? "
                "WHERE id=?",
                (instruction, ANALYSIS_CRON, next_run, custom_fields,
                 engine_ids_json, now, task_id),
            )
            db.commit()
            return task_id
        c.execute(
            "INSERT INTO autopilot_tasks "
            "(name, repo_key, agent_type, instruction, skill_name, cron, max_retry, "
            "enabled, last_run_at, next_run_at, created_at, updated_at, created_by, "
            "engine_runtime_ids, custom_fields, work_branch, engine_runtime_uids) "
            "VALUES (?, ?, 'issue', ?, ?, ?, 1, 1, '', ?, ?, ?, ?, ?, ?, '', '')",
            (name, org_repo_key, instruction, ANALYSIS_SKILL, ANALYSIS_CRON,
             next_run, now, now, created_by, engine_ids_json, custom_fields),
        )
        db.commit()
        task_id = int(c.lastrowid or 0)
    log.info(f"org_analysis: ensured daily task id={task_id} org={org_id} repo={org_repo_key}")
    return task_id


def ensure_analysis_automation(org_id: str, *, actor_id: str | int = 0) -> dict:
    """关联仓库后调用：确保组织仓库存在每日分析任务；无组织仓库时跳过。"""
    task_id = ensure_analysis_task(str(org_id), actor_id=actor_id)
    return {"ok": bool(task_id), "task_id": task_id, "cron": ANALYSIS_CRON if task_id else ""}


def automation_task(org_id: str) -> dict:
    org = get_org(str(org_id)) or {}
    if not org:
        return {}
    name = managed_task_name(org)
    with db_lock:
        c = db.cursor()
        row = c.execute(
            "SELECT id, name, repo_key, cron, enabled, last_run_at, next_run_at "
            "FROM autopilot_tasks WHERE repo_key=? AND name=?",
            (str(org.get("org_repo_key") or ""), name),
        ).fetchone()
    if not row:
        return {}
    return {"id": int(row[0]), "name": row[1], "repo_key": row[2], "cron": row[3],
            "enabled": bool(row[4]), "last_run_at": row[5], "next_run_at": row[6]}


def _org_repos_for_analysis(org_id: str) -> list[dict]:
    out = []
    for row in list_org_repos(str(org_id)) or []:
        if not row.get("repo_key") or row.get("is_org_repo"):
            continue
        out.append({"repo_key": str(row.get("repo_key") or ""),
                    "full_name": str(row.get("full_name") or "")})
    return out


def analysis_targets(org_id: str, *, mode: str = "all", repo_key: str = "") -> list[dict]:
    repos = _org_repos_for_analysis(org_id)
    if mode == "single":
        want = str(repo_key or "")
        return [r for r in repos if r["repo_key"] == want]
    return repos


def request_repo_analysis(org_id: str, repo_key: str, *, trigger: str = "link",
                          actor_id: str | int = 0) -> dict:
    """关联仓库后触发单仓 AI 分析（幂等：任务已在跑则返回 running）。"""
    org = get_org(str(org_id)) or {}
    if not org or org.get("is_personal"):
        return {"ok": False, "reason": "ORG_NOT_TEAM"}
    target = None
    for row in list_org_repos(str(org_id)) or []:
        if str(row.get("repo_key") or "") == str(repo_key):
            target = row
            break
    if not target:
        return {"ok": False, "reason": "REPO_NOT_IN_ORG"}
    if bool(target.get("is_org_repo")):
        return {"ok": False, "reason": "ORG_REPO_SKIPPED"}
    task_id = ensure_analysis_task(str(org_id), actor_id=actor_id)
    if not task_id:
        return {"ok": False, "reason": "NO_ORG_REPO"}
    mark_ai_status(str(org_id), str(repo_key), "queued", triggered_by=trigger)
    params = {"mode": "single", "org_id": str(org_id), "repo_key": str(repo_key),
              "trigger": str(trigger)}
    result = _trigger_task(task_id, params)
    if not result.get("ok"):
        mark_ai_status(str(org_id), str(repo_key), "failed",
                       error=str(result.get("message") or "trigger_failed"),
                       triggered_by=trigger)
    return {"ok": bool(result.get("ok")), "task_id": task_id,
            "reason": "" if result.get("ok") else str(result.get("message") or "trigger_failed")}


def request_all_analysis(org_id: str, *, trigger: str = "manual",
                         actor_id: str | int = 0) -> dict:
    repos = _org_repos_for_analysis(org_id)
    if not repos:
        return {"ok": True, "skipped": True, "reason": "no_repos", "queued": 0}
    task_id = ensure_analysis_task(str(org_id), actor_id=actor_id)
    if not task_id:
        return {"ok": False, "skipped": False, "reason": "NO_ORG_REPO", "queued": 0}
    for r in repos:
        mark_ai_status(str(org_id), r["repo_key"], "queued", triggered_by=trigger)
    params = {"mode": "all", "org_id": str(org_id), "trigger": str(trigger)}
    result = _trigger_task(task_id, params)
    if not result.get("ok"):
        return {"ok": False, "skipped": False, "queued": len(repos),
                "task_id": task_id, "reason": str(result.get("message") or "trigger_failed")}
    return {"ok": True, "skipped": False, "queued": len(repos), "task_id": task_id}


def context_for_repo(org_id: str, repo_key: str) -> dict:
    return _analysis_context(str(org_id), str(repo_key))


def save_result(org_id: str, repo_key: str, result: dict, **kwargs) -> dict | None:
    return save_ai_result(str(org_id), str(repo_key), result, **kwargs)


def status_for_repo(org_id: str, repo_key: str) -> dict:
    return get_ai_status(str(org_id), str(repo_key))


def analysis_docs(org_id: str) -> dict:
    """供下派流水线读取的 AI 分析文档（DB 权威，分支只是镜像）。"""
    from src.domains.mgmt.org_context_repo import ORG_REPO_ANALYSIS_BRANCH
    from src.domains.mgmt.org_repo_map import get_map, list_repo_map

    repos = []
    for item in (list_repo_map(str(org_id)) or {}).get("repos") or []:
        m = get_map(str(org_id), item["repo_key"]) or {}
        repos.append({
            "repo_key": item["repo_key"],
            "full_name": item["full_name"],
            "purpose": item.get("purpose") or m.get("purpose") or "",
            "domains": item.get("domains") or [],
            "keywords": item.get("keywords") or [],
            "interfaces": item.get("interfaces") or [],
            "depends_on": item.get("depends_on") or [],
            "not_owns": item.get("not_owns") or [],
            "confidence": item.get("confidence") or 0,
            "source_commit": item.get("source_commit") or m.get("source_commit") or "",
            "analyzed_at": item.get("analyzed_at") or "",
            "summary_md": str(m.get("summary_md") or "")[:16000],
        })
    return {"org_id": str(org_id), "branch": ORG_REPO_ANALYSIS_BRANCH, "repos": repos}
