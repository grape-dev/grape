"""Pipeline configuration loader — DB-backed (Scene Center).

Reads scene configuration from devops_pipeline_scene table (repo-scoped only).
Official scenes live in scene_market(source='official'); repo scenes are copies
applied to a repo (issue-39 方案 B: scenes only bound to repo_key).
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


def _load_official_scene_from_market(scene_id: str) -> Optional[dict]:
    """Look up an official scene (template) from scene_market(source='official').

    Used as build-time / fallback source when a repo has no applied scene copy.
    """
    db, lock = _db()
    with lock:
        c = db.cursor()
        try:
            c.execute(
                "SELECT name, display_name, description, nodes_json, onboarding FROM scene_market "
                "WHERE name=? AND source='official' AND is_active=1 LIMIT 1", (scene_id,))
            row = c.fetchone()
            if row:
                nodes = json.loads(row[3] or "[]") if row[3] else []
                return {
                    "scene_id": scene_id, "name": row[0],
                    "display_name": row[1] or row[0], "description": row[2] or "",
                    "nodes": nodes, "onboarding": row[4] or "repo-onboarding",
                }
        except Exception as e:
            log.warning(f"_load_official_scene_from_market failed: {e}")
    return None


def _query_scenes_from_db(repo_key: str = "") -> list[dict]:
    """Query repo-scoped scenes from DB (scenes only bound to repo_key).

    Official scenes are templates in scene_market, not shown as repo scenes.
    """
    db, lock = _db()
    rows = []
    _has_dn = False
    _has_ao = False
    _has_cb = False
    _has_icon = False
    _has_st = False
    _has_pr_sub = False
    _has_linked_repos = False
    with lock:
        c = db.cursor()
        try:
            if repo_key:
                # PRAGMA 在 MySQL 上列不全，用 SHOW COLUMNS 替代
                try:
                    c.execute("SHOW COLUMNS FROM devops_pipeline_scene")
                    _cols = {r[0] for r in c.fetchall()}
                except Exception:
                    _cols = {r[1] for r in c.execute("PRAGMA table_info(devops_pipeline_scene)").fetchall()}
                _has_dn = "display_name" in _cols
                _has_ao = "advanced_options" in _cols
                _has_cb = "created_by" in _cols
                _has_icon = "icon" in _cols
                _has_st = "scene_type" in _cols
                _has_pr_sub = "pr_subscription_enabled" in _cols
                _has_linked_repos = "linked_repos" in _cols
                _extra = ""
                if _has_dn: _extra += ", display_name"
                if _has_ao: _extra += ", advanced_options"
                if _has_cb: _extra += ", created_by"
                if _has_icon: _extra += ", icon"
                if _has_st: _extra += ", scene_type"
                if _has_pr_sub: _extra += ", pr_subscription_enabled"
                if _has_linked_repos: _extra += ", linked_repos"
                c.execute(
                    f"SELECT scene_id, name, description, nodes_json, onboarding{_extra} FROM devops_pipeline_scene "
                    "WHERE is_active=1 AND repo_key=? ORDER BY id", (repo_key,))
                rows.extend(c.fetchall())
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
            nodes = json.loads(r[3] or "[]") if r[3] else []
        except Exception:
            nodes = []
        # Parse advanced_options for work_branch
        work_branch = "main"
        col_offset = 5  # columns after onboarding (index 4)
        if _has_dn and len(r) > col_offset:
            col_offset += 1  # display_name
        if _has_ao and len(r) > col_offset:
            try:
                opts = json.loads(r[col_offset] or "{}")
                work_branch = opts.get("work_branch", "main") or "main"
            except Exception:
                pass
            col_offset += 1
        created_by = ""
        if _has_cb and len(r) > col_offset:
            created_by = r[col_offset] or ""
            col_offset += 1
        icon = ""
        if _has_icon and len(r) > col_offset:
            icon = r[col_offset] or ""
            col_offset += 1
        scene_type = "standard"
        if _has_st and len(r) > col_offset:
            scene_type = r[col_offset] or "standard"
            col_offset += 1
        pr_subscription_enabled = 0
        if _has_pr_sub and len(r) > col_offset:
            pr_subscription_enabled = int(r[col_offset] or 0)
            col_offset += 1
        linked_repos = []
        if _has_linked_repos and len(r) > col_offset:
            try:
                linked_repos = json.loads(r[col_offset] or "[]") if r[col_offset] else []
            except Exception:
                linked_repos = []
        result.append({
            "scene_id": scene_id, "name": r[1], "description": r[2],
            "display_name": (r[5] if _has_dn and len(r) > 5 else "") or r[1],
            "nodes": nodes, "onboarding": r[4] or "repo-onboarding",
            "work_branch": work_branch, "created_by": created_by,
            "icon": icon, "scene_type": scene_type,
            "pr_subscription_enabled": pr_subscription_enabled,
            "linked_repos": linked_repos,
        })
    return result


def _query_scene_by_id(scene_id: str, repo_key: str = "") -> Optional[dict]:
    """Query a specific repo-scoped scene from DB by scene_id + repo_key.

    仅仓库场景；找不到时回退到 scene_market 官方模板（_load_official_scene_from_market）。
    """
    db, lock = _db()
    with lock:
        c = db.cursor()
        try:
            _has_dn = False
            _has_st = False
            _has_pr_sub = False
            _has_linked_repos = False
            try:
                # PRAGMA 在 MySQL 上列不全，用 SHOW COLUMNS 替代
                try:
                    c.execute("SHOW COLUMNS FROM devops_pipeline_scene")
                    _cols = {r[0] for r in c.fetchall()}
                except Exception:
                    _cols = {r[1] for r in c.execute("PRAGMA table_info(devops_pipeline_scene)").fetchall()}
                _has_dn = "display_name" in _cols
                _has_st = "scene_type" in _cols
                _has_pr_sub = "pr_subscription_enabled" in _cols
                _has_linked_repos = "linked_repos" in _cols
            except Exception:
                pass
            _extra = ", display_name" if _has_dn else ""
            if _has_st:
                _extra += ", scene_type"
            if _has_pr_sub:
                _extra += ", pr_subscription_enabled"
            if _has_linked_repos:
                _extra += ", linked_repos"
            c.execute(
                f"SELECT scene_id, name, description, nodes_json, onboarding{_extra} FROM devops_pipeline_scene "
                "WHERE is_active=1 AND scene_id=? AND repo_key=? LIMIT 1",
                (scene_id, repo_key))
            row = c.fetchone()
            if row:
                nodes = json.loads(row[3] or "[]") if row[3] else []
                _scene_type = "standard"
                _pr_sub = 0
                _linked_repos = []
                _col_idx = 5  # after onboarding (index 4)
                if _has_dn:
                    _col_idx += 1  # display_name at index 5
                if _has_st and len(row) > _col_idx:
                    _scene_type = row[_col_idx] or "standard"
                    _col_idx += 1
                if _has_pr_sub and len(row) > _col_idx:
                    _pr_sub = int(row[_col_idx] or 0)
                    _col_idx += 1
                if _has_linked_repos and len(row) > _col_idx:
                    try:
                        _linked_repos = json.loads(row[_col_idx] or "[]") if row[_col_idx] else []
                    except Exception:
                        _linked_repos = []
                return {
                    "scene_id": row[0], "name": row[1], "description": row[2],
                    "display_name": (row[5] if _has_dn and len(row) > 5 else "") or row[1],
                    "nodes": nodes, "onboarding": row[4] or "repo-onboarding",
                    "scene_type": _scene_type,
                    "pr_subscription_enabled": _pr_sub,
                    "linked_repos": _linked_repos,
                }
        except Exception as e:
            log.warning(f"_query_scene_by_id failed: {e}")
    return None


def get_scenes(repo_key: str = "") -> list[dict]:
    """Return scene list [{id, name, display_name, scope, description, work_branch, created_by, icon, nodes, onboarding}] for this repo."""
    db_scenes = _query_scenes_from_db(repo_key=repo_key)
    return [{"id": s["scene_id"], "name": s["name"],
             "display_name": s.get("display_name") or s["name"],
             "scope": "repo",
             "description": s.get("description") or "",
             "work_branch": s.get("work_branch") or "main",
             "created_by": s.get("created_by") or "",
             "icon": s.get("icon") or "",
             "nodes": s.get("nodes") or [],
             "onboarding": s.get("onboarding") or "repo-onboarding",
             "scene_type": s.get("scene_type") or "standard",
             "pr_subscription_enabled": s.get("pr_subscription_enabled", 0),
             "linked_repos": s.get("linked_repos") or []} for s in db_scenes]


def get_pipeline_config(scene_id: str = "default", repo_key: str = "") -> Optional[dict]:
    """Return pipeline config for a repo-scoped scene: {name, nodes: [...], onboarding}.

    DB-first: query devops_pipeline_scene (repo_key-scoped).
    Fallback: scene_market official template → pipelines.json file → default scene.
    """
    scene = None
    if repo_key:
        scene = _query_scene_by_id(scene_id, repo_key=repo_key)
    if not scene:
        # Fallback 1: official template from scene_market
        scene = _load_official_scene_from_market(scene_id)
    if not scene:
        # Fallback 2: pipelines.json file → default scene
        data = _load_pipelines_file()
        file_scene = data.get("scenes", {}).get(scene_id) or data.get("scenes", {}).get("default")
        if file_scene:
            _name = file_scene.get("name", scene_id)
            return {
                "name": _name,
                "display_name": _name,
                "nodes": file_scene.get("nodes", []),
                "onboarding": file_scene.get("onboarding", "repo-onboarding"),
            }
        return None
    return {
        "name": scene.get("name", scene_id) or scene_id,
        "display_name": scene.get("display_name") or scene.get("name", scene_id) or scene_id,
        "nodes": scene.get("nodes") or [],
        "onboarding": scene.get("onboarding", "repo-onboarding"),
        "scene_type": scene.get("scene_type", "standard"),
    }


def get_node_by_tag(scene_id: str, tag: str, repo_key: str = "", user_id: int = 0) -> Optional[dict]:
    """Find a node by tag in a scene's pipeline."""
    cfg = get_pipeline_config(scene_id, repo_key=repo_key)
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

def create_custom_scene(scene_id: str, name: str, description: str,
                        repo_key: str, nodes: list, onboarding: str = "repo-onboarding",
                        display_name: str = "", created_by: str = "", icon: str = "",
                        scene_type: str = "standard", pr_subscription_enabled: int = 0,
                        linked_repos: list = None) -> int:
    """Create or reactivate a repo-scoped custom scene in DB. Returns DB id."""
    db, lock = _db()
    nodes_json = json.dumps(nodes, ensure_ascii=False)
    linked_repos_json = json.dumps(linked_repos or [], ensure_ascii=False)
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc).isoformat()
    with lock:
        c = db.cursor()
        # Check if record exists (硬删除后不会有 is_active=0 残留)
        c.execute(
            "SELECT id FROM devops_pipeline_scene WHERE scene_id=? AND repo_key=? LIMIT 1",
            (scene_id, repo_key))
        existing = c.fetchone()
        if existing:
            # 已存在 → 更新
            c.execute(
                "UPDATE devops_pipeline_scene SET name=?, description=?, nodes_json=?, onboarding=?, is_active=1, updated_at=? WHERE id=?",
                (name, description, nodes_json, onboarding, now, existing[0]))
            _try_set_display_name(c, existing[0], display_name or name)
            _try_set_created_by(c, existing[0], created_by)
            _try_set_icon(c, existing[0], icon)
            _try_set_scene_type(c, existing[0], scene_type)
            _try_set_pr_sub_enabled(c, existing[0], pr_subscription_enabled)
            _try_set_linked_repos(c, existing[0], linked_repos_json)
            db.commit()
            return existing[0]
        # Insert new record
        c.execute(
            "INSERT INTO devops_pipeline_scene (scene_id, name, description, repo_key, nodes_json, onboarding, is_active, created_at, updated_at) "
            "VALUES (?,?,?,?,?,?,1,?,?)",
            (scene_id, name, description, repo_key, nodes_json, onboarding, now, now))
        new_id = c.lastrowid
        _try_set_display_name(c, new_id, display_name or name)
        _try_set_created_by(c, new_id, created_by)
        _try_set_icon(c, new_id, icon)
        _try_set_scene_type(c, new_id, scene_type)
        _try_set_pr_sub_enabled(c, new_id, pr_subscription_enabled)
        _try_set_linked_repos(c, new_id, linked_repos_json)
        db.commit()
        return new_id


def _try_set_created_by(c, row_id: int, created_by: str):
    """幂等写入 created_by（列不存在时忽略）。"""
    if not created_by:
        return
    try:
        c.execute("UPDATE devops_pipeline_scene SET created_by=? WHERE id=?", (created_by, row_id))
    except Exception:
        pass


def _try_set_icon(c, row_id: int, icon: str):
    """幂等写入 icon（列不存在时忽略）。"""
    try:
        c.execute("UPDATE devops_pipeline_scene SET icon=? WHERE id=?", (icon, row_id))
    except Exception:
        pass


def _try_set_scene_type(c, row_id: int, scene_type: str):
    """幂等写入 scene_type（列不存在时忽略）。"""
    try:
        c.execute("UPDATE devops_pipeline_scene SET scene_type=? WHERE id=?", (scene_type, row_id))
    except Exception:
        pass

def _try_set_pr_sub_enabled(c, row_id: int, enabled: int):
    """幂等写入 pr_subscription_enabled（列不存在时忽略）。"""
    try:
        c.execute("UPDATE devops_pipeline_scene SET pr_subscription_enabled=? WHERE id=?", (int(enabled), row_id))
    except Exception:
        pass


def _try_set_linked_repos(c, row_id: int, linked_repos_json: str):
    """幂等写入 linked_repos（列不存在时忽略）。"""
    try:
        c.execute("UPDATE devops_pipeline_scene SET linked_repos=? WHERE id=?", (linked_repos_json, row_id))
    except Exception:
        pass


def _try_set_display_name(c, row_id: int, display_name: str):
    """幂等写入 display_name（列不存在时忽略，兼容旧库）。"""
    try:
        c.execute("UPDATE devops_pipeline_scene SET display_name=? WHERE id=?", (display_name, row_id))
    except Exception:
        pass


def update_custom_scene(scene_id: str, repo_key: str,
                        nodes: list, name: str | None = None, description: str | None = None,
                        icon: str | None = None, pr_subscription_enabled: int | None = None,
                        linked_repos: list | None = None):
    """Update a repo-scoped custom scene."""
    db, lock = _db()
    nodes_json = json.dumps(nodes, ensure_ascii=False)
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc).isoformat()
    with lock:
        c = db.cursor()
        sets = ["nodes_json=?", "updated_at=?"]
        params = [nodes_json, now]
        if name:  # 空字符串不覆盖已有名称
            sets.append("name=?")
            params.append(name)
            sets.append("display_name=?")
            params.append(name)
        if description is not None:  # None=不更新，空字符串=清空描述
            sets.append("description=?")
            params.append(description)
        if icon is not None:
            sets.append("icon=?")
            params.append(icon)
        if pr_subscription_enabled is not None:
            sets.append("pr_subscription_enabled=?")
            params.append(int(pr_subscription_enabled))
        if linked_repos is not None:
            sets.append("linked_repos=?")
            params.append(json.dumps(linked_repos, ensure_ascii=False))
        params.extend([scene_id, repo_key])
        c.execute(
            f"UPDATE devops_pipeline_scene SET {','.join(sets)} WHERE scene_id=? AND repo_key=? AND is_active=1",
            tuple(params))
        db.commit()


def delete_custom_scene(scene_id: str, repo_key: str):
    """Hard-delete a repo-scoped custom scene. 精确匹配 scene_id + repo_key，不影响其他仓库。"""
    db, lock = _db()
    with lock:
        c = db.cursor()
        c.execute(
            "DELETE FROM devops_pipeline_scene WHERE scene_id=? AND repo_key=?",
            (scene_id, repo_key))
        db.commit()

    # 自检推荐配置：清理指向被删场景的规则/兜底
    try:
        from src.prompt_builder import _sanitize_recommend_config
        _sanitize_recommend_config(repo_key)
    except Exception:
        pass


def apply_market_scene_by_name(repo_key: str, scene_name: str, user_id: int = 0) -> bool:
    """按场景名从 scene_market 查 market_id 并应用到仓库。

    供模板创建流程按模板 scene_id 覆盖默认场景使用。
    幂等：仓库已有同 market_scene_id 的场景则不重复创建。
    """
    from src.repo_key import from_repo_key

    # 按 name 查 scene_market 拿 market_id
    db, lock = _db()
    with lock:
        c = db.cursor()
        c.execute(
            "SELECT id FROM scene_market WHERE name=? AND source='official' AND is_active=1 LIMIT 1",
            (scene_name,),
        )
        row = c.fetchone()
    if not row:
        log.warning(f"apply_market_scene_by_name: market scene '{scene_name}' not found for {repo_key}")
        return False
    market_id = row[0]

    # 从 repo_key 拆 owner/repo（apply_market_scene 需要这两个参数）
    full_name = from_repo_key(repo_key)  # "XOcto/repo-name"
    parts = full_name.split("/", 1)
    if len(parts) != 2:
        log.warning(f"apply_market_scene_by_name: invalid repo_key '{repo_key}'")
        return False
    owner, repo_name = parts

    # 调 apply_market_scene，走场景中心完整逻辑
    # 传入 repo_key 避免 apply_market_scene 内部用 to_repo_key 默认 gitcode: 前缀
    from src.api.routes.scenes import apply_market_scene
    result = apply_market_scene(market_id, user={"id": user_id}, owner=owner, repo=repo_name, repo_key=repo_key)
    log.info(f"apply_market_scene_by_name: applied market scene {market_id} ({scene_name}) to {repo_key}: {result}")
    return True


def _query_official_scene_ids() -> set[str]:
    """Return set of official scene_ids available in scene_market(source='official')."""
    db, lock = _db()
    with lock:
        c = db.cursor()
        try:
            c.execute(
                "SELECT DISTINCT name FROM scene_market "
                "WHERE source='official' AND is_active=1")
            return {r[0] for r in c.fetchall()}
        except Exception as e:
            log.warning(f"_query_official_scene_ids failed: {e}")
            return set()


def get_scene_engine_ids(scene_id: str, agent_type: str, repo_key: str = "", user_id: int = 0) -> list[int]:
    """Get engine_runtime_ids from a scene's node for a specific agent_type.

    Returns empty list if no scene found, node not found, or node has no engine binding.
    """
    node = get_node_by_tag(scene_id, agent_type, repo_key=repo_key)
    if not node:
        return []
    return node.get("engine_runtime_ids", []) or []
