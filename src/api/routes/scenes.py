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

    from src.repo_key import to_repo_key
    repo_key = to_repo_key(f"{owner}/{repo}")
    from src.session_manager import db, db_lock
    from src.user.db import get_bot_token_by_repo
    repo_token = get_bot_token_by_repo(owner, repo)

    updated = 0
    with db_lock:
        c = db.cursor()
        for agent_type, skill in mapping.items():
            sid = str(uuid.uuid4())
            try:
                from src.user.db import resolve_skill_id, ensure_default_runtime
                from src.api.session_store import get_agent_type_enable
                skill_id = resolve_skill_id(skill)
                runtime_id = ensure_default_runtime("opencode") or 0
                # 场景只改 skill 映射，不动仓库级 enable：已有会话保留原 enable（用户 Dashboard 开关为准），
                # 仅新建会话才继承 agent_type 全局默认（sre 默认关），避免 INSERT OR REPLACE 落到列默认 1 误开启
                _row = c.execute("SELECT enable FROM devops_agent_session WHERE repo_key=? AND agent_type=?", (repo_key, agent_type)).fetchone()
                _enable = _row[0] if _row and _row[0] is not None else get_agent_type_enable(agent_type)
                c.execute(
                    "INSERT OR REPLACE INTO devops_agent_session (id, repo_key, agent_type, runtime_id, skill, skill_id, repo_gitcode_token, status, last_active_at, enable) VALUES (?,?,?,?,?,?,?,?,datetime('now'),?)",
                    (sid, repo_key, agent_type, runtime_id, skill, skill_id, repo_token, "idle", _enable)
                )
                updated += 1
            except Exception as e:
                log.warning(f"Scene apply: failed for {agent_type}: {e}")
        db.commit()

    log.info(f"Scene applied: scene={body.scene_id}, repo={repo_key}, updated={updated} agents")
    return {"message": f"场景 '{scene.get('name', body.scene_id)}' 已应用", "updated": updated}
