"""Pipeline configuration loader — DB-backed (Scene Center).

Reads scene configuration from devops_pipeline_scene table.
Backward compatible: if DB table missing/no scenes, falls back to configs/pipelines.json.
"""
import json
import os
from typing import Optional

from src.config import CONFIG_DIR, log

# File-based fallback (used during migration or if DB not ready)
_PIPELINES_PATH = os.path.join(CONFIG_DIR, "pipelines.json")
_LEGACY_PATH = os.path.join(CONFIG_DIR, "agent-skill-mapping.json")

# Node color mapping (same as frontend agentColors)
NODE_COLORS = {
    "vod": "var(--pk)",
    "issue": "var(--r)",
    "coding": "var(--a)",
    "qa": "var(--pu)",
    "sre": "var(--or)",
    "chat": "var(--sk)",
}


def _db():
    from src.session_manager import db, db_lock
    return db, db_lock


def _load_pipelines_file() -> dict:
    """Load pipelines.json directly from file (fallback only)."""
    try:
        with open(_PIPELINES_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        log.warning(f"pipelines.json load failed ({e})")
        return {"scenes": {}}


def _load_legacy() -> dict:
    """Convert agent-skill-mapping.json to pipelines format (legacy fallback)."""
    try:
        with open(_LEGACY_PATH, "r", encoding="utf-8") as f:
            legacy = json.load(f)
        scenes = {}
        for scene_id, scene in legacy.get("scenes", {}).items():
            mapping = scene.get("mapping", {})
            node_ids = list(mapping.keys())
            nodes = []
            for i, nid in enumerate(node_ids):
                next_id = node_ids[i + 1] if i + 1 < len(node_ids) else "done"
                nodes.append({
                    "id": nid, "tag": nid, "skill": mapping[nid],
                    "mode": "emit", "next": next_id,
                    "handoff": "a2a" if next_id != "done" else "complete",
                })
            scenes[scene_id] = {"name": scene.get("name", scene_id), "nodes": nodes,
                                "onboarding": scene.get("onboarding", "repo-onboarding")}
        return {"scenes": scenes}
    except Exception as e:
        log.warning(f"legacy mapping load failed: {e}")
        return {"scenes": {}}


def _query_official_scene_ids() -> set[str]:
    """Return set of scene_ids that exist in official scope."""
    db, lock = _db()
    with lock:
        c = db.cursor()
        try:
            c.execute(
                "SELECT scene_id FROM devops_pipeline_scene "
                "WHERE is_active=1 AND scope='official'")
            return {r[0] for r in c.fetchall()}
        except Exception as e:
            log.warning(f"_query_official_scene_ids failed: {e}")
            return set()


def _query_scenes_from_db(repo_key: str = "", user_id: int = 0) -> list[dict]:
    """Query visible scenes from DB: user-scoped + repo-scoped.

    Official scenes are internal templates, not shown to users directly.
    Users see their fork copies (user scope) only.
    """
    db, lock = _db()
    rows = []
    with lock:
        c = db.cursor()
        try:
            # user-scoped scenes (current user only)
            if user_id:
                c.execute(
                    "SELECT scene_id, name, description, scope, nodes_json, onboarding FROM devops_pipeline_scene "
                    "WHERE is_active=1 AND scope='user' AND user_id=? ORDER BY id", (user_id,))
                rows.extend(c.fetchall())
            # repo-scoped scenes (current repo only)
            if repo_key:
                c.execute(
                    "SELECT scene_id, name, description, scope, nodes_json, onboarding FROM devops_pipeline_scene "
                    "WHERE is_active=1 AND scope='repo' AND repo_key=? ORDER BY id", (repo_key,))
                rows.extend(c.fetchall())
            # official scenes 不加入用户可见列表（仅作 fork 源和 fallback）
        except Exception as e:
            log.warning(f"_query_scenes_from_db failed: {e}")
    result = []
    seen_ids = set()
    for r in rows:
        scene_id = r[0]
        if scene_id in seen_ids:
            continue
        seen_ids.add(scene_id)
        try:
            nodes = json.loads(r[4] or "[]") if r[4] else []
        except Exception:
            nodes = []
        result.append({
            "scene_id": scene_id, "name": r[1], "description": r[2],
            "scope": r[3], "nodes": nodes, "onboarding": r[5] or "repo-onboarding",
        })
    return result


def _query_scene_by_id(scene_id: str, repo_key: str = "", user_id: int = 0) -> Optional[dict]:
    """Query a specific scene from DB by scene_id, checking all visible scopes.

    Priority: user > repo > official. First match wins.
    user_id=0 时自动从 repo_key 反查（_repo_user_id），确保仓库 owner 的 user 级场景可命中。
    """
    db, lock = _db()
    with lock:
        c = db.cursor()
        try:
            # user_id=0 时从 repo_key 反查
            if not user_id and repo_key:
                try:
                    from src.runtime.engine_select import _repo_user_id
                    user_id = _repo_user_id(repo_key)
                except Exception:
                    pass
            # Try user first, then repo, then official
            for scope, extra_cond, extra_param in [
                ("user", "AND user_id=?", user_id),
                ("repo", "AND repo_key=?", repo_key),
                ("official", "", None),
            ]:
                if scope == "user" and not user_id:
                    continue
                if scope == "repo" and not repo_key:
                    continue
                params = [scene_id, scope]
                if extra_param is not None:
                    params.append(extra_param)
                c.execute(
                    "SELECT scene_id, name, description, scope, nodes_json, onboarding FROM devops_pipeline_scene "
                    f"WHERE is_active=1 AND scene_id=? AND scope=? {extra_cond} LIMIT 1",
                    tuple(params))
                row = c.fetchone()
                if row:
                    nodes = json.loads(row[4] or "[]") if row[4] else []
                    return {
                        "scene_id": row[0], "name": row[1], "description": row[2],
                        "scope": row[3], "nodes": nodes, "onboarding": row[5] or "repo-onboarding",
                    }
        except Exception as e:
            log.warning(f"_query_scene_by_id failed: {e}")
    return None


def get_scenes(repo_key: str = "", user_id: int = 0) -> list[dict]:
    """Return scene list [{id, name, scope}] for API.

    Returns repo-scoped (current repo) + user-scoped (current user) scenes.
    Official scenes are internal templates, not shown to users.
    Auto-fork is triggered only at repo hosting time (host_repo), not here.
    """
    db_scenes = _query_scenes_from_db(repo_key=repo_key, user_id=user_id)
    if db_scenes:
        return [{"id": s["scene_id"], "name": s["name"],
                 "scope": s["scope"]}
                for s in db_scenes]
    return []


def get_pipeline_config(scene_id: str = "default", repo_key: str = "", user_id: int = 0) -> Optional[dict]:
    """Return pipeline config for a scene: {name, nodes: [...], onboarding}.

    DB-first: query devops_pipeline_scene table.
    Fallback: pipelines.json file → default scene.
    """
    # DB query
    scene = _query_scene_by_id(scene_id, repo_key=repo_key, user_id=user_id)
    if not scene:
        # Fallback to file
        data = _load_pipelines_file()
        file_scene = data.get("scenes", {}).get(scene_id) or data.get("scenes", {}).get("default")
        if file_scene:
            return {
                "name": file_scene.get("name", scene_id),
                "nodes": file_scene.get("nodes", []),
                "onboarding": file_scene.get("onboarding", "repo-onboarding"),
            }
        return None
    return {
        "name": scene["name"],
        "nodes": scene["nodes"],
        "onboarding": scene["onboarding"],
    }


def get_node_by_tag(scene_id: str, tag: str, repo_key: str = "", user_id: int = 0) -> Optional[dict]:
    """Find a node by tag in a scene's pipeline."""
    cfg = get_pipeline_config(scene_id, repo_key=repo_key, user_id=user_id)
    if not cfg:
        return None
    for node in cfg.get("nodes", []):
        if node.get("tag") == tag:
            return node
    return None


def get_first_node(scene_id: str = "default") -> Optional[dict]:
    """Return the first node of a scene's pipeline (entry point)."""
    cfg = get_pipeline_config(scene_id)
    if not cfg or not cfg.get("nodes"):
        return None
    return cfg["nodes"][0]


def get_next_node(scene_id: str, current_node_id: str) -> Optional[dict]:
    """Return the next node after the current one, respecting skip list."""
    cfg = get_pipeline_config(scene_id)
    if not cfg:
        return None
    nodes = cfg.get("nodes", [])
    found = False
    for node in nodes:
        if found:
            return node
        if node.get("id") == current_node_id:
            found = True
            next_id = node.get("next", "done")
            if next_id == "done":
                return None
    for node in nodes:
        if node.get("id") == current_node_id:
            next_id = node.get("next", "done")
            if next_id == "done":
                return None
            for n2 in nodes:
                if n2.get("id") == next_id:
                    return n2
    return None


def is_node_skipped(scene_id: str, node_id: str) -> bool:
    """Check if a node is in any other node's skip list."""
    cfg = get_pipeline_config(scene_id)
    if not cfg:
        return False
    for node in cfg.get("nodes", []):
        skip = node.get("skip", [])
        if node_id in skip:
            return True
    return False


# ===== Scene CRUD (for custom scenes) =====

def create_custom_scene(scene_id: str, name: str, description: str, scope: str,
                        repo_key: str, user_id: int, nodes: list, onboarding: str = "repo-onboarding") -> int:
    """Create or reactivate a custom scene in DB. Returns DB id."""
    db, lock = _db()
    nodes_json = json.dumps(nodes, ensure_ascii=False)
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc).isoformat()
    with lock:
        c = db.cursor()
        # Check if a soft-deleted record exists (is_active=0) — reactivate it
        extra_cond = ""
        extra_params = []
        if scope == "repo":
            extra_cond = "AND repo_key=?"
            extra_params.append(repo_key)
        elif scope == "user":
            extra_cond = "AND user_id=?"
            extra_params.append(user_id)
        c.execute(
            f"SELECT id FROM devops_pipeline_scene WHERE scene_id=? AND scope=? {extra_cond} LIMIT 1",
            [scene_id, scope] + extra_params)
        existing = c.fetchone()
        if existing:
            # Reactivate soft-deleted record
            c.execute(
                f"UPDATE devops_pipeline_scene SET name=?, description=?, nodes_json=?, onboarding=?, is_active=1, updated_at=? WHERE id=?",
                (name, description, nodes_json, onboarding, now, existing[0]))
            db.commit()
            return existing[0]
        # Insert new record
        c.execute(
            "INSERT INTO devops_pipeline_scene (scene_id, name, description, scope, repo_key, user_id, nodes_json, onboarding, is_active, created_at, updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,1,?,?)",
            (scene_id, name, description, scope, repo_key, user_id, nodes_json, onboarding, now, now))
        db.commit()
        return c.lastrowid


def update_custom_scene(scene_id: str, scope: str, repo_key: str, user_id: int,
                        nodes: list, name: str = "", description: str = ""):
    """Update a custom scene (official scenes cannot be updated)."""
    db, lock = _db()
    nodes_json = json.dumps(nodes, ensure_ascii=False)
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc).isoformat()
    with lock:
        c = db.cursor()
        sets = ["nodes_json=?", "updated_at=?"]
        params = [nodes_json, now]
        if name:
            sets.append("name=?")
            params.append(name)
        if description:  # 空字符串不覆盖已有描述
            sets.append("description=?")
            params.append(description)
        params.extend([scene_id, scope])
        extra_cond = ""
        extra_params = []
        if scope == "repo":
            extra_cond = "AND repo_key=?"
            extra_params.append(repo_key)
        elif scope == "user":
            extra_cond = "AND user_id=?"
            extra_params.append(user_id)
        c.execute(
            f"UPDATE devops_pipeline_scene SET {','.join(sets)} WHERE scene_id=? AND scope=? {extra_cond} AND is_active=1",
            tuple(params + extra_params))
        db.commit()


def delete_custom_scene(scene_id: str, scope: str, repo_key: str, user_id: int):
    """Soft-delete a custom scene (is_active=0). Official scenes cannot be deleted."""
    db, lock = _db()
    with lock:
        c = db.cursor()
        extra_cond = ""
        params = [scene_id, scope]
        if scope == "repo":
            extra_cond = "AND repo_key=?"
            params.append(repo_key)
        elif scope == "user":
            extra_cond = "AND user_id=?"
            params.append(user_id)
        c.execute(
            f"UPDATE devops_pipeline_scene SET is_active=0 WHERE scene_id=? AND scope=? {extra_cond}",
            tuple(params))
        db.commit()


def _auto_fork_official_scenes_to_repo(repo_key: str, user_id: int):
    """Auto-fork all official scenes to repo scope (idempotent).

    Called by host_repo when a repo is first hosted. Creates repo-scoped copies
    with scene_id prefixed 'repo:' to avoid cross-scope collisions.
    """
    db, lock = _db()
    official_ids = []
    with lock:
        c = db.cursor()
        try:
            c.execute(
                "SELECT scene_id FROM devops_pipeline_scene "
                "WHERE is_active=1 AND scope='official' AND scene_id='default'")
            official_ids = [r[0] for r in c.fetchall()]
        except Exception as e:
            log.warning(f"_auto_fork_official_scenes_to_repo: query failed: {e}")
    for sid in official_ids:
        try:
            fork_official_scene(sid, user_id, repo_key=repo_key)
        except Exception as e:
            log.warning(f"_auto_fork_official_scenes_to_repo: fork {sid} failed: {e}")


def fork_official_scene(scene_id: str, user_id: int, repo_key: str = "") -> bool:
    """Fork an official scene into repo or user scope.

    - repo_key non-empty: create repo-scoped copy with scene_id='repo:{scene_id}'
    - repo_key empty: create user-scoped copy with original scene_id (legacy)
    Idempotent: if a copy already exists, does nothing.
    """
    if not user_id and not repo_key:
        return False

    # Determine fork target scope and scene_id
    if repo_key:
        fork_scene_id = f"repo:{scene_id}"
        fork_scope = "repo"
        # Check if repo already has a fork
        existing = _query_scene_by_id(fork_scene_id, repo_key=repo_key, user_id=user_id)
        if existing:
            return False
    else:
        fork_scene_id = scene_id
        fork_scope = "user"
        if not user_id:
            return False
        existing = _query_scene_by_id(fork_scene_id, repo_key="", user_id=user_id)
        if existing and existing["scope"] == "user":
            return False

    # Find official source
    official = None
    db, lock = _db()
    with lock:
        c = db.cursor()
        try:
            c.execute(
                "SELECT scene_id, name, description, nodes_json, onboarding FROM devops_pipeline_scene "
                "WHERE is_active=1 AND scene_id=? AND scope='official' LIMIT 1", (scene_id,))
            row = c.fetchone()
            if row:
                official = {
                    "scene_id": row[0], "name": row[1], "description": row[2],
                    "nodes_json": row[3], "onboarding": row[4] or "repo-onboarding",
                }
        except Exception as e:
            log.warning(f"fork_official_scene: query official failed: {e}")
    if not official:
        return False
    # Create scoped copy
    try:
        create_custom_scene(
            scene_id=fork_scene_id, name=official["name"], description=official["description"],
            scope=fork_scope, repo_key=repo_key, user_id=user_id,
            nodes=json.loads(official["nodes_json"] or "[]"), onboarding=official["onboarding"])
        log.info(f"Forked official scene '{scene_id}' to {fork_scope} scope (scene_id={fork_scene_id}, repo_key={repo_key}, user_id={user_id})")
        return True
    except Exception as e:
        log.warning(f"fork_official_scene: create failed: {e}")
        return False


def get_scene_engine_ids(scene_id: str, agent_type: str, repo_key: str = "", user_id: int = 0) -> list[int]:
    """Get engine_runtime_ids from a scene's node for a specific agent_type.

    Returns empty list if no scene found, node not found, or node has no engine binding.
    """
    node = get_node_by_tag(scene_id, agent_type, repo_key=repo_key, user_id=user_id)
    if not node:
        return []
    return node.get("engine_runtime_ids", []) or []
