"""Scenes 路由：场景列表获取与应用"""
import json, os
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


@router.get("/with-pipeline")
async def list_scenes_with_pipeline():
    """返回场景列表 + 各场景流水线配置（pipelines.json）"""
    from src.api.pipeline_config import get_scenes, get_pipeline_config
    scene_list = get_scenes()
    result = []
    for s in scene_list:
        cfg = get_pipeline_config(s["id"])
        result.append({"id": s["id"], "name": s["name"], "pipeline": cfg})
    return {"scenes": result}


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
    from src.api.session_store import upsert_agent_session
    repo_token = get_bot_token_by_repo(owner, repo)

    updated = 0
    with db_lock:
        c = db.cursor()
        for agent_type, skill in mapping.items():
            try:
                from src.user.db import resolve_skill_id
                from src.api.session_store import get_agent_type_enable
                skill_id = resolve_skill_id(skill)
                # 引擎绑定：保留会话现有 runtime_id（场景只改 skill 映射，不覆盖用户引擎配置）；
                # 会话无绑定 → 用默认引擎选择 + 写引擎集合（与 ensure_default_agent_sessions 同源，
                # 保证 config 页（binding 表）与 dashboard（session.runtime_id）一致）。
                # 旧实现无条件 ensure_default_runtime("opencode") 会绕过 binding 直接写
                # session.runtime_id → config 未配置但 dashboard 显示有引擎（2026-08-17 修复）。
                runtime_id = 0
                try:
                    _cur = c.execute(
                        "SELECT runtime_id FROM devops_agent_session WHERE repo_key=? AND agent_type=?",
                        (repo_key, agent_type)).fetchone()
                    if _cur and _cur[0]:
                        runtime_id = _cur[0]
                except Exception:
                    runtime_id = 0
                if not runtime_id:
                    from src.runtime.engine_select import resolve_default_engine, set_binding
                    _rid = resolve_default_engine(repo_key, agent_type) or 0
                    set_binding(repo_key, agent_type, [_rid] if _rid else [])
                    runtime_id = _rid
                # 场景只改 skill 映射，不动仓库级 enable / 订阅 / 飞书绑定：
                # 已存在会话保留（enable 以用户 Dashboard 开关为准；sre 默认关见 get_agent_type_enable），
                # 仅新建会话继承 agent_type 全局默认。
                upsert_agent_session(
                    repo_key=repo_key, agent_type=agent_type,
                    runtime_id=runtime_id, skill=skill, skill_id=skill_id,
                    repo_gitcode_token=repo_token, status="idle",
                    enable=None, subscribe_events=None,
                    im_app_id=None, im_app_secret=None, im_provider=None,
                )
                updated += 1
            except Exception as e:
                log.warning(f"Scene apply: failed for {agent_type}: {e}")
        db.commit()

    log.info(f"Scene applied: scene={body.scene_id}, repo={repo_key}, updated={updated} agents")

    # 更新 managed_repos.dev_scene（前端 Dashboard 读取此字段显示当前场景）
    try:
        with db_lock:
            c2 = db.cursor()
            c2.execute("UPDATE managed_repos SET dev_scene=? WHERE repo_key=?", (body.scene_id, repo_key))
            db.commit()
    except Exception as e:
        log.warning(f"Scene apply: failed to update dev_scene: {e}")

    return {"message": f"场景 '{scene.get('name', body.scene_id)}' 已应用", "updated": updated}
