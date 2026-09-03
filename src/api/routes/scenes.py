"""Scenes 路由：场景列表获取、CRUD、应用"""
import json, os
from fastapi import APIRouter, HTTPException, Depends
from pydantic import BaseModel
from src.api.deps import get_current_user
from src.config import log

router = APIRouter(prefix="/api/scenes", tags=["scenes"])

_CONFIG_PATH = os.path.join(os.path.dirname(__file__), "..", "..", "..", "configs", "agent-skill-mapping.json")


def _load_scenes() -> dict:
    """读取 agent-skill-mapping.json 的场景配置（legacy fallback for apply）"""
    if not os.path.exists(_CONFIG_PATH):
        return {}
    with open(_CONFIG_PATH, "r", encoding="utf-8") as f:
        data = json.load(f)
    return data.get("scenes", {})


@router.get("")
def list_scenes(user: dict = Depends(get_current_user), repo: str = ""):
    """返回所有可用场景列表（repo + user，不含 official）"""
    from src.api.pipeline_config import get_scenes
    from src.repo_key import to_repo_key
    repo_key = ""
    if repo:
        repo_key = to_repo_key(repo)
    else:
        try:
            _repo = app_current_repo()
            if _repo:
                repo_key = to_repo_key(f"{_repo.get('owner','')}/{_repo.get('name','')}")
        except Exception:
            pass
    scenes = get_scenes(repo_key=repo_key, user_id=user.get("id", 0))
    return {"scenes": scenes}


@router.get("/with-pipeline")
def list_scenes_with_pipeline(user: dict = Depends(get_current_user), repo: str = ""):
    """返回场景列表 + 各场景流水线配置"""
    from src.api.pipeline_config import get_scenes, get_pipeline_config
    from src.repo_key import to_repo_key
    repo_key = ""
    if repo:
        repo_key = to_repo_key(repo)
    else:
        try:
            _repo = app_current_repo()
            if _repo:
                repo_key = to_repo_key(f"{_repo.get('owner','')}/{_repo.get('name','')}")
        except Exception:
            pass
    scene_list = get_scenes(repo_key=repo_key, user_id=user.get("id", 0))
    result = []
    for s in scene_list:
        cfg = get_pipeline_config(s["id"], repo_key=repo_key, user_id=user.get("id", 0))
        result.append({"id": s["id"], "name": s["name"], "scope": s.get("scope", "official"), "pipeline": cfg})
    return {"scenes": result}


@router.get("/{scene_id}")
def get_scene_detail(scene_id: str, user: dict = Depends(get_current_user)):
    """返回场景详情（nodes + engine 信息）"""
    from src.api.pipeline_config import get_pipeline_config
    from src.repo_key import to_repo_key
    repo_key = ""
    try:
        repo = app_current_repo()
        if repo:
            repo_key = to_repo_key(f"{repo.get('owner','')}/{repo.get('name','')}")
    except Exception:
        pass
    cfg = get_pipeline_config(scene_id, repo_key=repo_key, user_id=user.get("id", 0))
    if not cfg:
        raise HTTPException(status_code=404, detail={"code": 404, "message": f"场景 '{scene_id}' 不存在"})
    return {"id": scene_id, "name": cfg["name"], "nodes": cfg["nodes"], "onboarding": cfg.get("onboarding", "")}


@router.get("/skills/{agent_type}")
def list_skills_for_agent(agent_type: str, user: dict = Depends(get_current_user)):
    """返回该 agent_type 可选的 skill 列表"""
    from src.session_manager import db, db_lock
    skills = []
    with db_lock:
        c = db.cursor()
        try:
            c.execute(
                "SELECT skill_name, display_name, scope, owner_id FROM skill "
                "WHERE agent_type=? AND is_active=1 "
                "AND (scope='official' OR (scope='user' AND owner_id=?) OR (scope='repo' AND owner_id=?)) "
                "ORDER BY scope, skill_name",
                (agent_type, user.get("id", 0), user.get("id", 0)))
            for r in c.fetchall():
                skills.append({"skill_name": r[0], "display_name": r[1] or r[0], "scope": r[2], "owner_id": r[3] or ""})
        except Exception as e:
            log.warning(f"list_skills_for_agent failed: {e}")
    return {"skills": skills}


# ===== Scene CRUD =====

class SceneNodeInput(BaseModel):
    id: str
    tag: str = ""
    skill: str = ""
    mode: str = "emit"
    next: str = "done"
    handoff: str = "a2a"
    skip: list = []
    enabled: bool = True
    engine_runtime_ids: list = []

class CreateSceneRequest(BaseModel):
    scene_id: str = ""
    name: str
    description: str = ""
    scope: str = "user"  # user / repo
    nodes: list[SceneNodeInput] = []

class UpdateSceneRequest(BaseModel):
    name: str = ""
    description: str = ""
    nodes: list[SceneNodeInput] = []

class ApplySceneRequest(BaseModel):
    scene_id: str


@router.post("")
def create_scene(body: CreateSceneRequest, user: dict = Depends(get_current_user), repo: str = ""):
    """创建自定义场景"""
    from src.api.pipeline_config import create_custom_scene
    from src.repo_key import to_repo_key
    import uuid

    if body.scope == "official":
        raise HTTPException(status_code=403, detail={"code": 403, "message": "不可创建官方场景"})

    repo_key = ""
    if body.scope == "repo":
        if repo:
            repo_key = to_repo_key(repo)
        else:
            try:
                _repo = app_current_repo()
                if _repo:
                    repo_key = to_repo_key(f"{_repo.get('owner','')}/{_repo.get('name','')}")
            except Exception:
                pass
        if not repo_key:
            raise HTTPException(status_code=400, detail={"code": 400, "message": "仓库级场景需要先选择仓库"})

    scene_id = body.scene_id or f"custom-{uuid.uuid4().hex[:8]}"
    nodes_json = [n.model_dump() for n in body.nodes]

    try:
        db_id = create_custom_scene(
            scene_id=scene_id, name=body.name, description=body.description,
            scope=body.scope, repo_key=repo_key, user_id=user.get("id", 0),
            nodes=nodes_json)
    except Exception as e:
        if "UNIQUE" in str(e).upper():
            raise HTTPException(status_code=409, detail={"code": 409, "message": f"场景 ID '{scene_id}' 已存在"})
        raise HTTPException(status_code=500, detail={"code": 500, "message": str(e)})

    log.info(f"Scene created: id={scene_id} scope={body.scope} by user={user.get('id')}")
    return {"scene_id": scene_id, "db_id": db_id, "message": "场景创建成功"}


@router.put("/{scene_id}")
def update_scene(scene_id: str, body: UpdateSceneRequest, user: dict = Depends(get_current_user), repo: str = ""):
    """更新场景（repo/user scope 直接更新，official 不可修改）"""
    from src.api.pipeline_config import update_custom_scene, _query_scene_by_id
    from src.repo_key import to_repo_key

    repo_key = ""
    if repo:
        repo_key = to_repo_key(repo)
    else:
        try:
            _repo = app_current_repo()
            if _repo:
                repo_key = to_repo_key(f"{_repo.get('owner','')}/{_repo.get('name','')}")
        except Exception:
            pass

    existing = _query_scene_by_id(scene_id, repo_key=repo_key, user_id=user.get("id", 0))
    if not existing:
        raise HTTPException(status_code=404, detail={"code": 404, "message": f"场景 '{scene_id}' 不存在"})

    if existing["scope"] == "official":
        raise HTTPException(status_code=403, detail={"code": 403, "message": "官方场景不可修改，请先创建自定义场景"})

    nodes_json = [n.model_dump() for n in body.nodes]
    update_custom_scene(
        scene_id=scene_id, scope=existing["scope"], repo_key=repo_key,
        user_id=user.get("id", 0), nodes=nodes_json,
        name=body.name, description=body.description)

    log.info(f"Scene updated: id={scene_id} scope={existing['scope']} by user={user.get('id')}")
    return {"message": "场景更新成功"}


@router.delete("/{scene_id}")
def delete_scene(scene_id: str, user: dict = Depends(get_current_user), repo: str = ""):
    """删除自定义场景（软删除，官方场景不可删除）"""
    from src.api.pipeline_config import delete_custom_scene, _query_scene_by_id
    from src.repo_key import to_repo_key

    repo_key = ""
    if repo:
        repo_key = to_repo_key(repo)
    else:
        try:
            _repo = app_current_repo()
            if _repo:
                repo_key = to_repo_key(f"{_repo.get('owner','')}/{_repo.get('name','')}")
        except Exception:
            pass

    existing = _query_scene_by_id(scene_id, repo_key=repo_key, user_id=user.get("id", 0))
    if not existing:
        raise HTTPException(status_code=404, detail={"code": 404, "message": f"场景 '{scene_id}' 不存在"})
    if existing["scope"] == "official":
        raise HTTPException(status_code=403, detail={"code": 403, "message": "官方场景不可删除"})

    delete_custom_scene(scene_id=scene_id, scope=existing["scope"],
                        repo_key=repo_key, user_id=user.get("id", 0))

    log.info(f"Scene deleted: id={scene_id} by user={user.get('id')}")
    return {"message": "场景已删除"}


@router.post("/apply/{owner}/{repo}")
def apply_scene(owner: str, repo: str, body: ApplySceneRequest,
                      user: dict = Depends(get_current_user)):
    """为仓库应用指定场景"""
    from src.api.pipeline_config import get_pipeline_config
    from src.repo_key import to_repo_key
    from src.session_manager import db, db_lock
    from src.user.db import get_bot_token_by_repo
    from src.api.session_store import upsert_agent_session

    repo_key = to_repo_key(f"{owner}/{repo}")
    cfg = get_pipeline_config(body.scene_id, repo_key=repo_key, user_id=user.get("id", 0))
    if not cfg:
        raise HTTPException(status_code=404, detail={"code": 404, "message": f"场景 '{body.scene_id}' 不存在"})

    nodes = cfg.get("nodes", [])
    if not nodes:
        raise HTTPException(status_code=400, detail={"code": 400, "message": "场景无 Agent 节点配置"})

    repo_token = get_bot_token_by_repo(owner, repo)
    updated = 0
    with db_lock:
        c = db.cursor()
        for node in nodes:
            agent_type = node.get("tag") or node.get("id", "")
            skill = node.get("skill", "")
            if not agent_type or not skill:
                continue
            # 跳过场景中 enabled=false 的 agent
            if node.get("enabled") is False:
                continue
            try:
                from src.user.db import resolve_skill_id
                from src.api.session_store import get_agent_type_enable
                skill_id = resolve_skill_id(skill)

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
                    from src.runtime.engine_select import resolve_default_engine
                    _rid = resolve_default_engine(repo_key, agent_type) or 0
                    runtime_id = _rid

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

    # 更新 managed_repos.dev_scene
    try:
        with db_lock:
            c2 = db.cursor()
            c2.execute("UPDATE managed_repos SET dev_scene=? WHERE repo_key=?", (body.scene_id, repo_key))
            db.commit()
    except Exception as e:
        log.warning(f"Scene apply: failed to update dev_scene: {e}")

    return {"message": f"场景 '{cfg.get('name', body.scene_id)}' 已应用", "updated": updated}


def app_current_repo():
    """获取当前选中仓库（从前端 header 上下文，这里用 placeholder）"""
    # FastAPI 无 session 级仓库选择；前端通过 query param 或 header 传
    # 这里返回 None，调用方会用 user 信息或其他方式获取 repo
    return None
