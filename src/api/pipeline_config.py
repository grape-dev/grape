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
                "SELECT name, description, nodes_json, onboarding FROM scene_market "
                "WHERE name=? AND source='official' AND is_active=1 LIMIT 1", (scene_id,))
            row = c.fetchone()
            if row:
                nodes = json.loads(row[2] or "[]") if row[2] else []
                return {
                    "scene_id": scene_id, "name": row[0], "description": row[1] or "",
                    "nodes": nodes, "onboarding": row[3] or "repo-onboarding",
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
    with lock:
        c = db.cursor()
        try:
            if repo_key:
                c.execute(
                    "SELECT scene_id, name, description, nodes_json, onboarding FROM devops_pipeline_scene "
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
        result.append({
            "scene_id": scene_id, "name": r[1], "description": r[2],
            "nodes": nodes, "onboarding": r[4] or "repo-onboarding",
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
            c.execute(
                "SELECT scene_id, name, description, nodes_json, onboarding FROM devops_pipeline_scene "
                "WHERE is_active=1 AND scene_id=? AND repo_key=? LIMIT 1",
                (scene_id, repo_key))
            row = c.fetchone()
            if row:
                nodes = json.loads(row[3] or "[]") if row[3] else []
                return {
                    "scene_id": row[0], "name": row[1], "description": row[2],
                    "nodes": nodes, "onboarding": row[4] or "repo-onboarding",
                }
        except Exception as e:
            log.warning(f"_query_scene_by_id failed: {e}")
    return None


def get_scenes(repo_key: str = "") -> list[dict]:
    """Return scene list [{id, name}] for this repo (repo-scoped only)."""
    db_scenes = _query_scenes_from_db(repo_key=repo_key)
    return [{"id": s["scene_id"], "name": s["name"]} for s in db_scenes]


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
            return {
                "name": file_scene.get("name", scene_id),
                "nodes": file_scene.get("nodes", []),
                "onboarding": file_scene.get("onboarding", "repo-onboarding"),
            }
        return None
    return {
        "name": scene.get("name", scene_id) or scene_id,
        "nodes": scene.get("nodes") or [],
        "onboarding": scene.get("onboarding", "repo-onboarding"),
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
                        repo_key: str, nodes: list, onboarding: str = "repo-onboarding") -> int:
    """Create or reactivate a repo-scoped custom scene in DB. Returns DB id."""
    db, lock = _db()
    nodes_json = json.dumps(nodes, ensure_ascii=False)
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc).isoformat()
    with lock:
        c = db.cursor()
        # Check if a soft-deleted record exists (is_active=0) — reactivate it
        c.execute(
            "SELECT id FROM devops_pipeline_scene WHERE scene_id=? AND repo_key=? LIMIT 1",
            (scene_id, repo_key))
        existing = c.fetchone()
        if existing:
            # Reactivate soft-deleted record
            c.execute(
                "UPDATE devops_pipeline_scene SET name=?, description=?, nodes_json=?, onboarding=?, is_active=1, updated_at=? WHERE id=?",
                (name, description, nodes_json, onboarding, now, existing[0]))
            db.commit()
            return existing[0]
        # Insert new record
        c.execute(
            "INSERT INTO devops_pipeline_scene (scene_id, name, description, repo_key, nodes_json, onboarding, is_active, created_at, updated_at) "
            "VALUES (?,?,?,?,?,?,1,?,?)",
            (scene_id, name, description, repo_key, nodes_json, onboarding, now, now))
        db.commit()
        return c.lastrowid


def update_custom_scene(scene_id: str, repo_key: str,
                        nodes: list, name: str = "", description: str = ""):
    """Update a repo-scoped custom scene."""
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
        params.extend([scene_id, repo_key])
        c.execute(
            f"UPDATE devops_pipeline_scene SET {','.join(sets)} WHERE scene_id=? AND repo_key=? AND is_active=1",
            tuple(params))
        db.commit()


def delete_custom_scene(scene_id: str, repo_key: str):
    """Soft-delete a repo-scoped custom scene (is_active=0)."""
    db, lock = _db()
    with lock:
        c = db.cursor()
        c.execute(
            "UPDATE devops_pipeline_scene SET is_active=0 WHERE scene_id=? AND repo_key=?",
            (scene_id, repo_key))
        db.commit()


def _auto_fork_official_scenes_to_repo(repo_key: str, user_id: int = 0):
    """[deprecated] 旧版：host_repo 自动 fork default 官方场景。

    方案 B 后由 apply_default_scene_to_repo 替代（从 scene_market 复刻默认「通用开发」场景）。
    保留兼容：若已有 old-scope 调用方，此处改为走新逻辑。
    """
    apply_default_scene_to_repo(repo_key, user_id)


def apply_default_scene_to_repo(repo_key: str, user_id: int = 0) -> bool:
    """应用默认场景到仓库：从 scene_market(source='official') 复刻默认场景为仓库场景。

    默认场景名读 configs/default-scene.json single config（default_scene_market_name，默认 'default'=通用开发）。
    配置为 '' 时不自动配置场景（仓库无场景 → VOD 输出「未配置场景」）。
    幂等：仓库已有同名场景则不重复创建。
    """
    default_name = _get_default_scene_market_name()
    if not default_name:
        log.info(f"apply_default_scene_to_repo: default scene disabled ({repo_key})")
        return False
    official = _load_official_scene_from_market(default_name)
    if not official:
        log.warning(f"apply_default_scene_to_repo: market scene '{default_name}' not found for {repo_key}")
        return False
    # 检查仓库是否已有该场景
    existing = _query_scene_by_id(default_name, repo_key=repo_key)
    if existing:
        return False
    try:
        create_custom_scene(
            scene_id=default_name, name=official["name"] or default_name,
            description=official.get("description", "") or "",
            repo_key=repo_key, nodes=official.get("nodes") or [],
            onboarding=official.get("onboarding", "repo-onboarding"))
        # 同步写 managed_repos.dev_scene
        try:
            from src.user.db import set_repo_scene
            set_repo_scene(repo_key, default_name)
        except Exception as _e:
            log.warning(f"apply_default_scene_to_repo: set dev_scene failed: {_e}")
        log.info(f"Applied default scene '{default_name}' to {repo_key}")
        return True
    except Exception as e:
        log.warning(f"apply_default_scene_to_repo: create failed: {e}")
        return False


def fork_official_scene(scene_id: str, user_id: int = 0, repo_key: str = "") -> bool:
    """[deprecated] 旧版：fork 官方场景到 repo/user scope。

    方案 B：官方场景走 scene_market，仓库应用即复刻为 repo 场景（scene_id 无前缀）。
    """
    if not repo_key:
        return False
    official = _load_official_scene_from_market(scene_id)
    if not official:
        log.warning(f"fork_official_scene: market scene '{scene_id}' not found for {repo_key}")
        return False
    if _query_scene_by_id(scene_id, repo_key=repo_key):
        return False
    try:
        create_custom_scene(
            scene_id=scene_id, name=official["name"] or scene_id,
            description=official.get("description", "") or "",
            repo_key=repo_key, nodes=official.get("nodes") or [],
            onboarding=official.get("onboarding", "repo-onboarding"))
        log.info(f"Forked scene '{scene_id}' to repo {repo_key}")
        return True
    except Exception as e:
        log.warning(f"fork_official_scene: create failed: {e}")
        return False


def _get_default_scene_market_name() -> str:
    """读 configs/default-scene.json 的 default_scene_market_name（可被环境变量 DEFAULT_SCENE_MARKET_NAME 覆盖）。"""
    env = os.environ.get("DEFAULT_SCENE_MARKET_NAME")
    if env is not None:
        return env.strip()
    try:
        _cfg_path = os.path.join(CONFIG_DIR, "default-scene.json")
        if os.path.exists(_cfg_path):
            with open(_cfg_path, "r", encoding="utf-8") as f:
                _cfg = json.load(f)
            return str(_cfg.get("default_scene_market_name", "default") or "").strip()
    except Exception as e:
        log.warning(f"default-scene.json load failed: {e}")
    return "default"


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
