"""场景节点 skill_id 回填（repo→official 优先，Issue #3789，迁移 202611240002）。

背景：Issue #979 的 202611040001 回填按 ``user→repo→official`` 解析，repo 级
场景里同名 user 档技能会被写进节点 ``skill_id``，派发/应用再按 id 命中 user
档 → 触发 #3788 的 scope fail-closed，也留下「node without skill_id」以外的
跨档误绑风险。

本迁移对存量场景一次性收敛（幂等，可重复执行）：
1. ``devops_pipeline_scene``（repo 级场景）：节点技能只按 **repo → official**
   解析；当前 ``skill_id`` 指向 user 档的节点改绑 repo/official，解析不到则
   清 0（派发按名降级，由 ``resolve_skill_dispatch`` 继续收敛）；
2. ``scene_market``：repo 行同样 repo→official；user 行（scope=user）保留
   user 档解析（user 场景允许引用本人技能）。

输出迁移报告（扫描行数 / 回填节点 / 纠正 user 档 / 未解析节点 / 写回行数），
失败 raise 不盖章（下次启动重试）。post_check 判定收敛：活跃 repo 场景节点
不得再引用 user 档技能。
"""
from __future__ import annotations

import json

from src.kernel.config import log


def _resolve_scope(*, skill_name: str, repo_key: str, user_id: int = 0) -> int:
    from src.domains.mgmt.user.db import resolve_skill_id

    return int(resolve_skill_id(skill_name, repo_key=repo_key or "", user_id=user_id or 0) or 0)


def _skill_scope(skill_id: int) -> str:
    if not skill_id:
        return ""
    try:
        from src.domains.mgmt.user.db import get_skill_by_id

        row = get_skill_by_id(int(skill_id))
        return str((row or {}).get("scope") or "")
    except Exception as e:  # noqa: BLE001 - 读失败按未知档处理，不阻断回填
        log.warning(f"202611240002: read skill scope failed (id={skill_id}): {e}")
        return ""


def _rewrite_nodes(nodes: list, *, repo_key: str, user_id: int = 0,
                   allow_user: bool) -> tuple[bool, dict]:
    stats = {"filled": 0, "user_corrected": 0, "dangling_cleared": 0, "unresolved": 0}
    changed = False
    for node in nodes or []:
        if not isinstance(node, dict):
            continue
        # 口径与保存/应用链路（_persist_scene_skill_ids）一致：未启用节点不参与固化，
        # 也不会被派发（Issue #3789 Nit：消除同一口径两套行为）。
        if node.get("enabled") is False:
            continue
        name = str(node.get("skill") or "").strip()
        if not name:
            continue
        cur = 0
        try:
            cur = int(node.get("skill_id") or 0)
        except (TypeError, ValueError):
            cur = 0
        cur_scope = _skill_scope(cur)
        # 悬空 id（技能已删/停用，scope 取空）→ 清 0 后按名重解析；否则派发会持续打
        # skill_id invalid/inactive 降级告警，与「存量一次性收敛」目标不符。
        if cur and not cur_scope:
            cur = 0
            node.pop("skill_id", None)
            stats["dangling_cleared"] += 1
            changed = True
        # repo 级场景不得保留 user 档技能（含 #979 回填写入的同名 user 档）
        if not allow_user and cur and cur_scope == "user":
            cur = 0
            node.pop("skill_id", None)
            stats["user_corrected"] += 1
            changed = True
        if cur and (allow_user or cur_scope != "user"):
            continue
        sid = _resolve_scope(skill_name=name, repo_key=repo_key,
                             user_id=user_id if allow_user else 0)
        if not sid:
            stats["unresolved"] += 1
            if node.get("skill_id") not in (None, 0):
                changed = True
            continue
        if int(node.get("skill_id") or 0) != int(sid):
            node["skill_id"] = int(sid)
            stats["filled"] += 1
            changed = True
    return changed, stats


def _merge(stats: dict, part: dict) -> None:
    for k, v in part.items():
        stats[k] = stats.get(k, 0) + int(v or 0)


def backfill_scene_nodes_skill_id(conn=None) -> dict:
    """存量场景节点 skill_id 收敛（repo→official 优先，幂等）。返回迁移报告。"""
    from src.kernel.session_manager import db, db_lock

    if conn is None:
        conn = db
    report = {"scanned_rows": 0, "updated_rows": 0, "filled": 0,
              "user_corrected": 0, "dangling_cleared": 0, "unresolved": 0}
    with db_lock:
        c = conn.cursor()
        rows = c.execute(
            "SELECT id, repo_key, nodes_json FROM devops_pipeline_scene WHERE is_active=1"
        ).fetchall() or []
        try:
            market_rows = c.execute(
                "SELECT id, repo_key, source_user_id, nodes_json FROM scene_market"
            ).fetchall() or []
        except Exception as e:  # noqa: BLE001 - 表缺失时跳过市场模板
            log.warning(f"202611240002: scene_market read failed: {e}")
            market_rows = []

    for row_id, repo_key, nodes_json in rows:
        report["scanned_rows"] += 1
        try:
            nodes = json.loads(nodes_json or "[]")
        except Exception as e:  # noqa: BLE001
            log.warning(f"202611240002: bad nodes_json (scene row={row_id}): {e}")
            continue
        if not isinstance(nodes, list) or not nodes:
            continue
        changed, part = _rewrite_nodes(nodes, repo_key=repo_key or "",
                                       allow_user=False)
        _merge(report, part)
        if changed:
            with db_lock:
                conn.cursor().execute(
                    "UPDATE devops_pipeline_scene SET nodes_json=? WHERE id=?",
                    (json.dumps(nodes, ensure_ascii=False), row_id))
                conn.commit()
            report["updated_rows"] += 1

    for row_id, repo_key, source_user_id, nodes_json in market_rows:
        report["scanned_rows"] += 1
        try:
            nodes = json.loads(nodes_json or "[]")
        except Exception as e:  # noqa: BLE001
            log.warning(f"202611240002: bad nodes_json (market row={row_id}): {e}")
            continue
        if not isinstance(nodes, list) or not nodes:
            continue
        # 有 repo_key 的市场行 = repo 级模板（现行写入约束）；仅 user 行允许 user 档。
        # repo 档模板按「模板属仓」固化 id（同仓应用沿用）；跨仓应用由 apply/experience/
        # copy 入口先 _rebase_scene_skill_ids 清掉非本仓 repo 档 id，再按目标 repo_key
        # 重解析，因此不会出现 owner 不一致 400（Issue #3789 Low）。
        allow_user = not (repo_key or "")
        changed, part = _rewrite_nodes(nodes, repo_key=repo_key or "",
                                       user_id=int(source_user_id or 0),
                                       allow_user=allow_user)
        _merge(report, part)
        if changed:
            with db_lock:
                conn.cursor().execute(
                    "UPDATE scene_market SET nodes_json=? WHERE id=?",
                    (json.dumps(nodes, ensure_ascii=False), row_id))
                conn.commit()
            report["updated_rows"] += 1

    log.info(
        "202611240002 scene nodes skill_id backfill report: "
        f"scanned_rows={report['scanned_rows']} updated_rows={report['updated_rows']} "
        f"filled={report['filled']} user_corrected={report['user_corrected']} "
        f"dangling_cleared={report['dangling_cleared']} unresolved={report['unresolved']}")
    return report


def verify_repo_scenes_without_user_skill(conn=None) -> None:
    """post_check：活跃 repo 场景节点不得引用 user 档技能。"""
    from src.kernel.session_manager import db

    if conn is None:
        conn = db
    bad = []
    rows = conn.cursor().execute(
        "SELECT id, repo_key, nodes_json FROM devops_pipeline_scene WHERE is_active=1"
    ).fetchall() or []
    for row_id, repo_key, nodes_json in rows:
        try:
            nodes = json.loads(nodes_json or "[]")
        except Exception:  # noqa: BLE001 - 坏 JSON 不参与判定（回填已记日志）
            continue
        for node in nodes if isinstance(nodes, list) else []:
            # 与回填同口径：未启用节点不参与判定（也不会被派发）
            if not isinstance(node, dict) or node.get("enabled") is False:
                continue
            try:
                sid = int(node.get("skill_id") or 0)
            except (TypeError, ValueError):
                sid = 0
            if sid and _skill_scope(sid) == "user":
                bad.append((row_id, repo_key or "", node.get("id") or node.get("tag") or "",
                            node.get("skill") or "", sid))
    if bad:
        raise RuntimeError(
            "202611240002 post_check 未收敛：repo 级场景仍引用 user 档技能 "
            f"{len(bad)} 处，示例={bad[:5]}")
    log.info("202611240002 post_check 通过：repo 级场景节点无 user 档技能")


MIGRATION = {
    "version": "202611240002",
    "name": "scene-nodes-skill-id-repo-scope",
    "kind": "backfill",
    "fn": backfill_scene_nodes_skill_id,
    "domains": ("mgmt",),
    "post_check": verify_repo_scenes_without_user_skill,
}