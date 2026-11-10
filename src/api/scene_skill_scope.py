"""Issue #1758: single authoritative judge for scene-node skill scope.

Every entry point that decides whether a scene node may reference a skill
(save/apply precheck, read-only audit, dispatch gate, scene precheck, scene
detail) MUST call :func:`evaluate`.  No other module may re-implement the
skill_id / name resolution, scope comparison or error policy.

Fixed rule (Issue #1722 / #1758):
    A repo-scoped scene MUST NOT reference a ``user``-scope skill, even when
    the caller is the skill owner.  Dispatch identity is the agent, not the
    human owner, so an owner exception in the save path can never hold at
    dispatch time.

Error policy: lookups default to fail-closed.  A caller that must stay
available during a database hiccup may explicitly pass ``on_error="allow"``
(the degradation is logged and reflected in ``source == "db_error"``).
"""
from src.config import log

SCENE_SCOPE_REPO = "repo"
COMPATIBLE_SCOPES = ("repo", "official")
DEFAULT_ERROR_POLICY = "deny"
ERROR_POLICIES = ("deny", "allow")


def _env_error_policy() -> str:
    """入口未显式传 on_error 时的默认策略（Issue #1758 复审：给运维留一个降级开关）。

    `GRAPE_SCENE_SCOPE_ON_ERROR=allow` 时，技能 scope 查询异常按放行处理（记日志 +
    source=db_error），用于 DB 故障期间避免整仓派发被 fail-closed 打死；默认仍是 deny。
    """
    import os
    value = (os.environ.get("GRAPE_SCENE_SCOPE_ON_ERROR") or "").strip().lower()
    return value if value in ERROR_POLICIES else DEFAULT_ERROR_POLICY


def _node_skill_name(node: dict) -> str:
    return str(node.get("skill") or "").strip()


def _node_skill_id(node: dict) -> int:
    try:
        return int(node.get("skill_id") or 0)
    except (TypeError, ValueError):
        return 0


def _node_label(node: dict) -> str:
    return str(node.get("tag") or node.get("id") or "").strip()


def _node_role(node: dict) -> str:
    return str(node.get("id") or node.get("tag") or "").strip()


def _result(node: dict, expected_scope: str, *, ok: bool, skill: str = "",
            skill_id: int = 0, actual_scope: str = "", reason: str = "",
            source: str = "", owner_id: str = "",
            rebind_candidates: list | None = None) -> dict:
    node = node if isinstance(node, dict) else {}
    return {
        "ok": bool(ok),
        "skill": skill or "",
        "skill_id": int(skill_id or 0),
        "actual_scope": actual_scope or "",
        "expected_scope": expected_scope or SCENE_SCOPE_REPO,
        "reason": reason or "",
        "source": source or "",
        "owner_id": owner_id or "",
        "node_id": node.get("id") or node.get("tag") or "",
        "node_tag": _node_label(node),
        "rebind_candidates": list(rebind_candidates or []),
    }


def _get_skill_cached(skill_id: int, cache: dict | None) -> dict | None:
    key = ("id", int(skill_id))
    if cache is not None and key in cache:
        return cache[key]
    from src.user.db import get_skill_by_id
    row = get_skill_by_id(skill_id, raise_on_error=True)
    if cache is not None:
        cache[key] = row
    return row


def _get_scope_info_cached(skill_name: str, repo_key: str, cache: dict | None) -> dict:
    key = ("name", skill_name, repo_key or "")
    if cache is not None and key in cache:
        return cache[key]
    from src.user.db import get_skill_scope_info
    info = get_skill_scope_info(skill_name, repo_key, raise_on_error=True)
    if cache is not None:
        cache[key] = info
    return info


def skill_agent_type_by_name(skill_name: str, repo_key: str = "") -> str:
    """Return the skill's agent_type (read-only, used for rebind suggestions)."""
    if not skill_name:
        return ""
    try:
        from src.session_manager import db, db_lock
        with db_lock:
            row = db.cursor().execute(
                "SELECT agent_type FROM skill WHERE skill_name=? AND is_active=1 "
                "ORDER BY CASE WHEN scope='repo' AND owner_id=? THEN 0 "
                "WHEN scope='official' THEN 1 WHEN scope='repo' THEN 2 ELSE 3 END "
                "LIMIT 1", (skill_name, repo_key or "")).fetchone()
        return (row[0] or "") if row else ""
    except Exception as e:
        log.warning(f"scene_skill_scope: resolve agent_type failed: {e}")
        return ""


def rebind_candidates(skill_name: str, repo_key: str, agent_type: str = "",
                      limit: int = 5) -> list:
    # limit=5 取值依据（MR !1145 Critical-3）：400 响应体里给「可直接改绑的去向」，
    # 5 条覆盖同 agent_type 下 repo 优先 + 官方兜底的常见组合，再多前端展示反而不可选。
    """List repo/official skills a conflicting user skill can be rebound to.

    Read-only guidance only; never mutates the scene or skill table.
    """
    if not skill_name:
        return []
    agent_type = agent_type or skill_agent_type_by_name(skill_name, repo_key)
    try:
        from src.session_manager import db, db_lock
        sql = ("SELECT id, skill_name, scope, agent_type FROM skill "
               "WHERE is_active=1 AND skill_name<>? AND "
               "((scope='repo' AND owner_id=?) OR scope='official')")
        params = [skill_name, repo_key]
        if agent_type:
            sql += " AND agent_type=?"
            params.append(agent_type)
        sql += (" ORDER BY CASE scope WHEN 'repo' THEN 0 ELSE 1 END, "
                "skill_name LIMIT ?")
        params.append(limit)
        with db_lock:
            rows = db.cursor().execute(sql, params).fetchall()
        return [{"skill_id": r[0], "skill_name": r[1], "scope": r[2],
                 "agent_type": r[3] or ""} for r in rows]
    except Exception as e:
        log.warning(f"scene_skill_scope: rebind candidates failed: {e}")
        return []


def format_rebind_hint(candidates: list | None,
                       fallback: str = "请改用 repo/official 级技能") -> str:
    """Human-readable re-bind guidance shared by save precheck and dispatch."""
    names = ", ".join(
        f"{c.get('skill_name')}({c.get('scope')})"
        for c in (candidates or []) if isinstance(c, dict))
    return f"可改绑为：{names}" if names else fallback


def _mismatch(node: dict, expected_scope: str, skill: str, actual_scope: str,
              *, skill_id: int = 0, owner_id: str = "", source: str = "",
              reason: str = "", repo_key: str = "") -> dict:
    candidates = rebind_candidates(
        skill, repo_key, agent_type=_node_role(node))
    return _result(
        node, expected_scope, ok=False, skill=skill, skill_id=skill_id,
        actual_scope=actual_scope, reason=reason, source=source,
        owner_id=owner_id, rebind_candidates=candidates)


def _scope_mismatch_reason(skill: str, actual_scope: str,
                           expected_scope: str, node_tag: str = "",
                           scene_id: str = "") -> str:
    return (f"技能 '{skill}' scope 不匹配（技能 scope={actual_scope or '-'}，"
            f"场景 scope={expected_scope or SCENE_SCOPE_REPO}）："
            f"repo 级场景不可引用 user 级技能（含创建者本人），"
            f"保存/派发均会阻断（scene={scene_id or '-'}，node={node_tag or '-'}）")


def evaluate(node, repo_key: str, scene_scope: str = SCENE_SCOPE_REPO,
             *, scene_id: str = "", on_error: str = "",
             cache: dict | None = None) -> dict:
    """Judge whether a scene node may reference its skill under scene scope.

    Returns ``{ok, skill, skill_id, actual_scope, expected_scope, reason,
    source, owner_id, node_id, node_tag, rebind_candidates}``.

    ``source`` records the decision provenance: ``empty`` / ``disabled`` /
    ``skill_id`` / ``name`` / ``unresolved`` / ``db_error`` /
    ``invalid_skill_id``.  The caller is deliberately not part of the rule:
    no owner/caller exception exists for user-scope skills.
    """
    node = node if isinstance(node, dict) else {}
    expected_scope = scene_scope or SCENE_SCOPE_REPO
    if on_error not in ERROR_POLICIES:
        on_error = _env_error_policy()
    skill = _node_skill_name(node)
    skill_id = _node_skill_id(node)
    node_tag = _node_label(node)

    if node.get("enabled") is False:
        return _result(node, expected_scope, ok=True, skill=skill,
                       skill_id=skill_id, source="disabled",
                       reason="节点未启用，跳过技能 scope 校验")
    if not skill and not skill_id:
        return _result(node, expected_scope, ok=True, source="empty")

    try:
        if skill_id:
            bound = _get_skill_cached(skill_id, cache)
            if bound:
                scope = bound.get("scope") or ""
                owner = bound.get("owner_id") or ""
                name = bound.get("skill_name") or skill
                if scope in COMPATIBLE_SCOPES and not (
                        scope == "repo" and owner and repo_key and owner != repo_key):
                    return _result(
                        node, expected_scope, ok=True, skill=name,
                        skill_id=skill_id, actual_scope=scope,
                        source="skill_id", owner_id=owner,
                        reason=(f"节点显式绑定 skill_id={skill_id}"
                                f"（scope={scope or '-'}），与场景 scope 兼容"))
                if scope == "repo":
                    reason = (
                        f"技能 '{name}' 归属仓库 '{owner}' 与场景仓库 "
                        f"'{repo_key}' 不一致（技能 scope=repo，场景 scope={expected_scope}），"
                        f"保存/派发均会阻断（scene={scene_id or '-'}，node={node_tag or '-'}）")
                else:
                    reason = _scope_mismatch_reason(
                        name, scope or "user", expected_scope, node_tag, scene_id)
                return _mismatch(node, expected_scope, name, scope,
                                 skill_id=skill_id, owner_id=owner,
                                 source="skill_id", reason=reason,
                                 repo_key=repo_key)
            log.warning(
                f"scene_skill_scope: skill_id={skill_id} invalid/inactive "
                f"(scene={scene_id or '-'} node={node_tag or '-'}), "
                f"fallback to name resolution")
            if not skill:
                return _mismatch(
                    node, expected_scope, "", "", skill_id=skill_id,
                    source="invalid_skill_id",
                    reason=(f"节点绑定 skill_id={skill_id} 已失效且无技能名，"
                            f"无法解析技能 scope"),
                    repo_key=repo_key)

        info = _get_scope_info_cached(skill, repo_key, cache)
        if info.get("found") and info.get("matched"):
            return _result(
                node, expected_scope, ok=True, skill=skill,
                skill_id=info.get("skill_id") or 0,
                actual_scope=info.get("scope") or "",
                owner_id=info.get("owner_id") or "",
                source="name",
                reason=f"技能 '{skill}' scope 与场景 scope 兼容")
        if info.get("found"):
            actual_scope = info.get("scope") or "user"
            return _mismatch(
                node, expected_scope, skill, actual_scope,
                skill_id=info.get("skill_id") or 0,
                owner_id=info.get("owner_id") or "",
                source="name",
                reason=_scope_mismatch_reason(
                    skill, actual_scope, expected_scope, node_tag, scene_id),
                repo_key=repo_key)
        return _result(
            node, expected_scope, ok=True, skill=skill, source="unresolved",
            reason=(f"技能 '{skill}' 不在 skill 表（官方内置包形态），"
                    f"按存量兼容放行"))
    except Exception as e:
        log.warning(
            f"scene_skill_scope: lookup failed (policy={on_error}, "
            f"scene={scene_id or '-'} node={node_tag or '-'} skill={skill or skill_id}): {e}")
        if on_error == "allow":
            return _result(
                node, expected_scope, ok=True, skill=skill, skill_id=skill_id,
                source="db_error",
                reason=f"技能 scope 查询异常，按显式降级放行：{e}")
        return _mismatch(
            node, expected_scope, skill, "", skill_id=skill_id,
            source="db_error",
            reason=f"技能 scope 查询异常，按 fail-closed 拒绝：{e}",
            repo_key=repo_key)