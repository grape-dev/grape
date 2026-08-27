"""Pipeline configuration loader (event-dispatch-redesign-v2.md Phase 2).

Reads configs/pipelines.json, caches in memory, provides scene -> nodes lookup.
Backward compatible: if pipelines.json missing, falls back to agent-skill-mapping.json.
"""
import json
import os
from typing import Optional

from src.config import CONFIG_DIR, log

_CACHE = {"mtime": 0, "data": None}
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


def _load_pipelines() -> dict:
    """Load pipelines.json with mtime cache; fallback to legacy mapping."""
    try:
        st = os.stat(_PIPELINES_PATH)
        if _CACHE["mtime"] == st.st_mtime and _CACHE["data"]:
            return _CACHE["data"]
        with open(_PIPELINES_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        _CACHE["mtime"] = st.st_mtime
        _CACHE["data"] = data
        return data
    except Exception as e:
        log.warning(f"pipelines.json load failed ({e}), falling back to legacy")
        return _load_legacy()


def _load_legacy() -> dict:
    """Convert agent-skill-mapping.json to pipelines format."""
    try:
        with open(_LEGACY_PATH, "r", encoding="utf-8") as f:
            legacy = json.load(f)
        scenes = {}
        for scene_id, scene in legacy.get("scenes", {}).items():
            mapping = scene.get("mapping", {})
            node_ids = list(mapping.keys())  # vod, issue, coding, qa, sre
            nodes = []
            for i, nid in enumerate(node_ids):
                next_id = node_ids[i + 1] if i + 1 < len(node_ids) else "done"
                nodes.append({
                    "id": nid,
                    "tag": nid,
                    "skill": mapping[nid],
                    "mode": "emit",
                    "next": next_id,
                    "handoff": "a2a" if next_id != "done" else "complete",
                })
            scenes[scene_id] = {
                "name": scene.get("name", scene_id),
                "nodes": nodes,
                "onboarding": scene.get("onboarding", "repo-onboarding"),
            }
        return {"scenes": scenes}
    except Exception as e:
        log.warning(f"legacy mapping load failed: {e}")
        return {"scenes": {}}


def get_scenes() -> list[dict]:
    """Return scene list [{id, name}] for API."""
    data = _load_pipelines()
    return [{"id": k, "name": v.get("name", k)} for k, v in data.get("scenes", {}).items()]


def get_pipeline_config(scene_id: str = "default", repo_key: str = "") -> Optional[dict]:
    """Return pipeline config for a scene: {name, nodes: [...], onboarding}.

    查找顺序：内置 pipelines.json → 成员上传的 pipelines-user.json → default 回退。
    """
    data = _load_pipelines()
    scene = data.get("scenes", {}).get(scene_id)
    # 内置没有 → 尝试从成员上传的 pipelines-user.json 加载
    if not scene and repo_key:
        scene = _load_user_scene(repo_key, scene_id)
    if not scene:
        scene = data.get("scenes", {}).get("default")
    if not scene:
        return None
    return {
        "name": scene.get("name", scene_id),
        "nodes": scene.get("nodes", []),
        "onboarding": scene.get("onboarding", "repo-onboarding"),
    }


def _load_user_scene(repo_key: str, scene_id: str) -> Optional[dict]:
    """从成员上传的 pipelines-user.json 加载指定场景。"""
    try:
        from src.api.knowledge import member_files
        _mf = member_files(repo_key)
        for rel, content in _mf.items():
            if not rel.endswith("pipelines-user.json"):
                continue
            import json as _json
            try:
                _user_pipes = _json.loads(content)
            except Exception:
                continue
            _scene = _user_pipes.get("scenes", {}).get(scene_id)
            if _scene:
                return _scene
    except Exception:
        pass
    return None


def get_node_by_tag(scene_id: str, tag: str, repo_key: str = "") -> Optional[dict]:
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
            # Check if this node is in the previous node's skip list
            return node
        if node.get("id") == current_node_id:
            found = True
            next_id = node.get("next", "done")
            if next_id == "done":
                return None
    # Fallback: look up by next field
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
