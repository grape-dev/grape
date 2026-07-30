"""Scenes 路由：场景列表获取与应用"""
import json, os, uuid
from fastapi import APIRouter, HTTPException, Depends
from pydantic import BaseModel
from src.api.deps import get_current_user
from src.config import log

router = APIRouter(prefix="/api/scenes", tags=["scenes"])

_CONFIG_PATH = os.path.join(os.path.dirname(__file__), "..", "..", "..", "configs", "agent-skill-mapping.json")


def _load_scenes() -> dict:
    """读取 agent-skill-mapping.json 的场景配置"""
    if not os.path.exists(_CONFIG_PATH):
        return {}
    with open(_CONFIG_PATH, "r", encoding="utf-8") as f:
        data = json.load(f)
    return data.get("scenes", {})


@router.get("")
async def list_scenes():
    """返回所有可用场景列表"""
    scenes = _load_scenes()
    return {"scenes": [{"id": k, "name": v.get("name", k)} for k, v in scenes.items()]}


class ApplySceneRequest(BaseModel):
    scene_id: str


@router.post("/apply/{owner}/{repo}")
async def apply_scene(owner: str, repo: str, body: ApplySceneRequest,
                      user: dict = Depends(get_current_user)):
    """为仓库应用指定场景的 Agent-Skill 映射"""
    scenes = _load_scenes()
    scene = scenes.get(body.scene_id)
    if not scene:
        raise HTTPException(status_code=404, detail={"code": 404, "message": f"场景 '{body.scene_id}' 不存在"})

    mapping = scene.get("mapping", {})
    if not mapping:
        raise HTTPException(status_code=400, detail={"code": 400, "message": "场景无 Agent-Skill 映射"})

    repo_key = f"{owner}/{repo}"
    from src.session_manager import db, db_lock
    from src.user.db import get_bot_token_by_repo
    repo_token = get_bot_token_by_repo(owner, repo)

    updated = 0
    with db_lock:
        c = db.cursor()
        for agent_type, skill in mapping.items():
            sid = str(uuid.uuid4())
            try:
                c.execute(
                    "INSERT OR REPLACE INTO agent_sessions (id, repo_key, agent_type, backend, skill, repo_gitcode_token, status, last_active_at) VALUES (?,?,?,?,?,?,?,datetime('now'))",
                    (sid, repo_key, agent_type, "opencode", skill, repo_token, "idle")
                )
                updated += 1
            except Exception as e:
                log.warning(f"Scene apply: failed for {agent_type}: {e}")
        db.commit()

    log.info(f"Scene applied: scene={body.scene_id}, repo={repo_key}, updated={updated} agents")
    return {"message": f"场景 '{scene.get('name', body.scene_id)}' 已应用", "updated": updated}
