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


# ===== Scene Market API（场景市场）=====

def _resolve_scene_icon(icon: str, category_id: int, obs_icon_key: str = "") -> str:
    """解析场景图标为可访问的 URL 或 emoji。
    obs_icon_key 非空 → OBS 模式，动态生成签名 URL。
    icon 非空 → 本地路径（/static/ 开头）。
    都为空 → 查 scene_category 表拿分类 emoji 图标兜底。
    """
    if obs_icon_key:
        try:
            from src.storage import get_storage
            storage = get_storage()
            if storage is not None:
                return storage.temporary_url(obs_icon_key)
        except Exception:
            pass
    if icon:
        return icon
    from src.user.db import get_scene_category
    cat = get_scene_category(category_id)
    return cat.get('icon', '⚙') if cat else '⚙'


def _parse_scene_skills(nodes_json: str) -> list[str]:
    """从 nodes_json 提取技能名称列表"""
    try:
        nodes = json.loads(nodes_json) if nodes_json else []
    except Exception:
        return []
    return [n.get("skill", "") for n in nodes if n.get("skill")]


@router.get("/market/categories")
def list_scene_categories(user: dict = Depends(get_current_user)):
    """场景分类列表：按 order_num 排序，只返回 is_active=1 的"""
    from src.user.db import list_scene_categories as _list_cats
    return {"categories": _list_cats()}


@router.get("/market")
def list_scenes_market(user: dict = Depends(get_current_user),
                       q: str = "", category_id: int = 0, sort: str = "latest",
                       featured_only: int = 0, limit: int = 0,
                       page: int = 1, page_size: int = 20):
    """场景市场列表：支持搜索 + 分类过滤 + 排序 + 精选过滤 + 分页。
    limit>0 时不分页（精选场景用），否则按 page/page_size 分页。
    """
    from src.user.db import (list_scene_market, batch_scene_liked,
                             batch_scene_repo_count)

    scenes, total = list_scene_market(q=q, category_id=category_id, sort=sort,
                               featured_only=featured_only, limit=limit,
                               page=page, page_size=page_size)
    if not scenes:
        return {"scenes": [], "total": total, "page": page, "page_size": page_size}

    # 批量查 liked_by_me
    scene_ids = [s["id"] for s in scenes]
    liked_map = batch_scene_liked(user.get("id", 0), scene_ids)
    # 批量查 repo_count
    repo_count_map = batch_scene_repo_count(scene_ids)

    result = []
    for s in scenes:
        result.append({
            "id": s["id"],
            "name": s["name"],
            "display_name": s["display_name"],
            "version": s["version"],
            "market_description": s["market_description"],
            "icon": _resolve_scene_icon(s["icon"], s["category_id"], s.get("obs_icon_key", "")),
            "category_id": s["category_id"],
            "is_featured": s["is_featured"],
            "like_count": s["like_count"],
            "liked_by_me": liked_map.get(s["id"], False),
            "skills": _parse_scene_skills(s["nodes_json"]),
            "recommended_template": s["recommended_template"],
            "repo_count": repo_count_map.get(s["id"], 0),
        })
    return {"scenes": result, "total": total, "page": page, "page_size": page_size}


@router.get("/market/{market_id}")
def get_scene_market_detail(market_id: int, user: dict = Depends(get_current_user)):
    """场景详情：完整介绍 + 技能链路详解 + 推荐模板 + 在用仓库列表"""
    from src.user.db import (get_scene_market_by_id, is_scene_liked,
                             get_scene_repos, get_scene_category)
    from src.session_manager import db, db_lock

    scene = get_scene_market_by_id(market_id)
    if not scene:
        raise HTTPException(status_code=404, detail={"code": 404, "message": "场景不存在"})

    # 解析 nodes_json，通过 scene_skill_ref 拿 skill_id，再关联 skill 表拿详情
    skills_detail = []
    try:
        nodes = json.loads(scene["nodes_json"]) if scene["nodes_json"] else []
    except Exception:
        nodes = []
    with db_lock:
        c = db.cursor()
        for node in nodes:
            skill_name = node.get("skill", "")
            if not skill_name:
                continue
            # 先从 scene_skill_ref 拿 skill_id（以 ID 为准）
            c.execute(
                "SELECT skill_id FROM scene_skill_ref WHERE scene_id=? AND skill_name=? LIMIT 1",
                (scene["id"], skill_name),
            )
            ref_row = c.fetchone()
            skill_id = ref_row[0] if ref_row and ref_row[0] else 0
            # 用 skill_id 查 skill 表拿详情；skill_id=0 时 fallback 用 skill_name 查
            if skill_id:
                c.execute(
                    "SELECT id, display_name, agent_type, description FROM skill "
                    "WHERE id=? AND is_active=1 LIMIT 1",
                    (skill_id,),
                )
            else:
                c.execute(
                    "SELECT id, display_name, agent_type, description FROM skill "
                    "WHERE skill_name=? AND is_active=1 LIMIT 1",
                    (skill_name,),
                )
            row = c.fetchone()
            skills_detail.append({
                "skill_id": (row[0] if row else skill_id),
                "name": skill_name,
                "display_name": (row[1] if row and row[1] else skill_name),
                "agent_type": (row[2] if row else "") or node.get("tag", ""),
                "role_desc": (row[3] if row else "") or "",
            })

    # 查 recommended_template 对应的模板信息
    template_info = None
    if scene["recommended_template"]:
        try:
            from src.api.routes.templates import _find_template
            ctx = _find_template(scene["recommended_template"])
            if ctx:
                tpl = ctx["template"]
                template_info = {
                    "id": tpl.get("id", ""),
                    "name": tpl.get("name", ""),
                    "description": tpl.get("description", ""),
                    "language": tpl.get("language", "") or tpl.get("tags", [""])[0] if isinstance(tpl.get("tags"), list) else "",
                    "stars": tpl.get("stats", {}).get("stars", 0) if isinstance(tpl.get("stats"), dict) else 0,
                    "source": tpl.get("source", ""),
                    "source_repo": tpl.get("source_repo", ""),
                    "icon": tpl.get("icon", ""),
                }
        except Exception as e:
            log.warning(f"get_scene_market_detail: template lookup failed: {e}")

    # 查在用仓库列表 + 标记当前用户是否有权限访问
    repos = get_scene_repos(market_id)
    if repos and user.get("id"):
        from src.user.db import get_user_repos
        user_repo_names = {r["full_name"] for r in get_user_repos(user["id"])}
        for r in repos:
            r["accessible"] = r.get("full_name", "") in user_repo_names
    else:
        for r in repos:
            r["accessible"] = False

    # 分类名称
    cat = get_scene_category(scene["category_id"])
    category_name = cat["name"] if cat else ""

    return {
        "id": scene["id"],
        "name": scene["name"],
        "display_name": scene["display_name"],
        "version": scene["version"],
        "market_description": scene["market_description"],
        "icon": _resolve_scene_icon(scene["icon"], scene["category_id"], scene.get("obs_icon_key", "")),
        "category_id": scene["category_id"],
        "category_name": category_name,
        "is_featured": scene["is_featured"],
        "like_count": scene["like_count"],
        "liked_by_me": is_scene_liked(user.get("id", 0), market_id),
        "skills": skills_detail,
        "template": template_info,
        "repos": repos,
        "recommended_template": scene["recommended_template"],
    }


@router.get("/market/{market_id}/applied")
def check_market_scene_applied(market_id: int, user: dict = Depends(get_current_user),
                                owner: str = "", repo: str = ""):
    """检查市场场景是否已应用到当前仓库。

    返回: {"applied": bool, "scene_name": str}
    """
    from src.repo_key import to_repo_key
    from src.session_manager import db, db_lock

    if not owner or not repo:
        return {"applied": False, "scene_name": ""}

    repo_key = to_repo_key(f"{owner}/{repo}")
    with db_lock:
        c = db.cursor()
        c.execute(
            "SELECT name FROM devops_pipeline_scene "
            "WHERE market_scene_id=? AND repo_key=? AND scope='repo' AND is_active=1 LIMIT 1",
            (market_id, repo_key),
        )
        row = c.fetchone()

    return {"applied": bool(row), "scene_name": row[0] if row else ""}


@router.post("/market/{market_id}/apply")
def apply_market_scene(market_id: int, user: dict = Depends(get_current_user),
                       owner: str = "", repo: str = ""):
    """应用场景：从市场复制配置到用户级 devops_pipeline_scene。

    - scope='user', user_id=当前用户, scene_id=custom-{uuid}
    - 幂等：同一用户同一市场场景不重复创建（按 market_scene_id + user_id 查重）
    - scene_id 用 custom-{uuid}，不用 scene_market.name（会与 fork_official_scene 的 user scope 记录冲突）
    - 同时调 apply_scene 更新当前仓库的 agent 配置
    """
    import uuid
    from src.user.db import get_scene_market_by_id
    from src.api.pipeline_config import create_custom_scene, update_custom_scene
    from src.repo_key import to_repo_key
    from src.session_manager import db, db_lock

    if not owner or not repo:
        raise HTTPException(status_code=400, detail={"code": 400, "message": "请先选择仓库"})

    scene = get_scene_market_by_id(market_id)
    if not scene:
        raise HTTPException(status_code=404, detail={"code": 404, "message": "场景不存在"})

    repo_key = to_repo_key(f"{owner}/{repo}")
    user_id = user.get("id", 0)
    nodes_json = scene["nodes_json"]
    try:
        nodes = json.loads(nodes_json) if nodes_json else []
    except Exception:
        nodes = []

    # 幂等：先查 devops_pipeline_scene WHERE market_scene_id=? AND repo_key=? AND scope='repo'
    existing_scene_id = ""
    with db_lock:
        c = db.cursor()
        c.execute(
            "SELECT scene_id FROM devops_pipeline_scene "
            "WHERE market_scene_id=? AND repo_key=? AND scope='repo' AND is_active=1 LIMIT 1",
            (market_id, repo_key),
        )
        row = c.fetchone()
        if row:
            existing_scene_id = row[0]

    if existing_scene_id:
        # 已存在 → 更新
        update_custom_scene(
            scene_id=existing_scene_id, scope="repo", repo_key=repo_key,
            user_id=user_id, nodes=nodes,
            name=scene["display_name"], description=scene["description"],
        )
        scene_id = existing_scene_id
    else:
        # 不存在 → 新建（scope='repo'，scene_id 用 custom-uuid）
        scene_id = f"custom-{uuid.uuid4().hex[:8]}"
        with db_lock:
            c = db.cursor()
            from datetime import datetime, timezone
            now = datetime.now(timezone.utc).isoformat()
            nodes_json_str = json.dumps(nodes, ensure_ascii=False)
            c.execute(
                "INSERT INTO devops_pipeline_scene "
                "(scene_id, name, description, scope, repo_key, user_id, nodes_json, onboarding, "
                "is_active, created_at, updated_at, market_scene_id) "
                "VALUES (?,?,?,?,?,?,?,?,1,?,?,?)",
                (scene_id, scene["display_name"], scene["description"],
                 "repo", repo_key, user_id, nodes_json_str,
                 scene["onboarding"], now, now, market_id),
            )
            db.commit()

    # 调用现有 apply_scene 逻辑更新 devops_agent_session
    from src.api.routes.scenes import apply_scene, ApplySceneRequest
    apply_scene(owner, repo, ApplySceneRequest(scene_id=scene_id), user)

    log.info(f"Market scene applied: market_id={market_id}, scene_id={scene_id}, repo={repo_key}")
    return {"message": f"场景 '{scene['display_name']}' 已应用", "scene_id": scene_id}


@router.get("/market/{market_id}/experience-check")
async def check_market_scene_experience(market_id: int, user: dict = Depends(get_current_user)):
    """体验前检查：项目仓库是否已存在 + 是否已应用过此场景。

    返回: {"repo_exists": bool, "scene_applied": bool, "full_name": str, "scene_name": str}
    """
    from src.user.db import get_scene_market_by_id
    from src.session_manager import db, db_lock
    from src.repo_key import to_repo_key
    from src.api.deps import get_user_token
    from src.user.repo_manager import get_user_gitcode_login
    from src.api.routes.templates import _find_template

    scene = get_scene_market_by_id(market_id)
    if not scene:
        raise HTTPException(status_code=404, detail={"code": 404, "message": "场景不存在"})

    if not scene["recommended_template"]:
        return {"repo_exists": False, "scene_applied": False, "full_name": "", "scene_name": ""}

    tpl_ctx = _find_template(scene["recommended_template"])
    if not tpl_ctx:
        return {"repo_exists": False, "scene_applied": False, "full_name": "", "scene_name": ""}

    tpl = tpl_ctx["template"]
    repo_name = tpl.get("default_repo_name", tpl.get("id", ""))

    api_token = get_user_token(user["username"])
    if not api_token:
        return {"repo_exists": False, "scene_applied": False, "full_name": "", "scene_name": ""}
    gitcode_login = get_user_gitcode_login(api_token)
    if not gitcode_login:
        return {"repo_exists": False, "scene_applied": False, "full_name": "", "scene_name": ""}

    full_name = f"{gitcode_login}/{repo_name}"
    repo_key = to_repo_key(full_name)
    user_id = user.get("id", 0)

    repo_exists = False
    scene_applied = False

    with db_lock:
        c = db.cursor()
        # 仓库是否已托管
        c.execute("SELECT 1 FROM managed_repos WHERE repo_key=? LIMIT 1", (repo_key,))
        if c.fetchone():
            repo_exists = True
        else:
            # user_repos 里有记录也算已存在
            c.execute(
                "SELECT 1 FROM user_repos WHERE user_id=? AND owner=? AND name=? LIMIT 1",
                (user_id, gitcode_login, repo_name),
            )
            if c.fetchone():
                repo_exists = True

        # 该仓库是否已应用过此市场场景
        c.execute(
            "SELECT name FROM devops_pipeline_scene "
            "WHERE market_scene_id=? AND repo_key=? AND scope='repo' AND is_active=1 LIMIT 1",
            (market_id, repo_key),
        )
        row = c.fetchone()
        if row:
            scene_applied = True
            scene_name = row[0]
        else:
            scene_name = ""

    return {"repo_exists": repo_exists, "scene_applied": scene_applied,
            "full_name": full_name, "scene_name": scene_name}


@router.post("/market/{market_id}/experience")
async def experience_market_scene(market_id: int, user: dict = Depends(get_current_user)):
    """体验场景：基于场景关联的推荐模板创建/复用项目并应用场景。

    流程：
    1. 查 scene_market 拿 recommended_template + nodes_json + onboarding
    2. 查模板的 default_repo_name + 用户的 gitcode_login
    3. 查 user_repos 里是否已有 {gitcode_login}/{default_repo_name}
       - 已有仓库 → 检查是否已托管，未托管则先托管
       - 没有仓库 → 调 create_from_template 创建+托管
    4. 创建/复用用户级 devops_pipeline_scene（scope='user', scene_id=custom-{uuid}）
    5. 调 apply_scene 应用场景配置到仓库的 agent_session
    6. 返回 {full_name, scene_id, already_exists, message}
    """
    import uuid
    from src.user.db import get_scene_market_by_id
    from src.session_manager import db, db_lock
    from src.repo_key import to_repo_key
    from src.api.deps import get_user_token
    from src.user.repo_manager import get_user_gitcode_login
    from src.api.routes.templates import _find_template

    scene = get_scene_market_by_id(market_id)
    if not scene:
        raise HTTPException(status_code=404, detail={"code": 404, "message": "场景不存在"})

    if not scene["recommended_template"]:
        raise HTTPException(status_code=400, detail={"code": 400, "message": "该场景暂无体验模板，无法体验"})

    # 拿模板的 default_repo_name
    tpl_ctx = _find_template(scene["recommended_template"])
    if not tpl_ctx:
        raise HTTPException(status_code=404, detail={"code": 404, "message": f"模板 '{scene['recommended_template']}' 不存在"})
    tpl = tpl_ctx["template"]
    repo_name = tpl.get("default_repo_name", tpl.get("id", ""))

    # 拿用户的 GitCode login
    api_token = get_user_token(user["username"])
    if not api_token:
        raise HTTPException(status_code=400, detail={"code": 400, "message": "GitCode API Token 未配置"})
    gitcode_login = get_user_gitcode_login(api_token)
    if not gitcode_login:
        raise HTTPException(status_code=502, detail={"code": 502, "message": "无法获取当前用户 GitCode 身份"})

    full_name = f"{gitcode_login}/{repo_name}"
    user_id = user.get("id", 0)
    already_exists = False

    # 查 managed_repos 里是否已托管
    repo_key = to_repo_key(full_name)
    already_hosted = False
    repo_in_user_repos = False
    with db_lock:
        c = db.cursor()
        c.execute("SELECT 1 FROM managed_repos WHERE repo_key=? LIMIT 1", (repo_key,))
        already_hosted = c.fetchone() is not None
        if not already_hosted:
            c.execute(
                "SELECT 1 FROM user_repos WHERE user_id=? AND owner=? AND name=? LIMIT 1",
                (user_id, gitcode_login, repo_name),
            )
            repo_in_user_repos = c.fetchone() is not None

    if already_hosted:
        # 已托管 → 直接复用
        already_exists = True
    elif repo_in_user_repos:
        # 仓库已存在（user_repos 有记录）但未托管 → 直接托管
        already_exists = True
        try:
            from src.api.routes.repos import RepoAction, host_repo
            await host_repo(RepoAction(full_name=full_name), user)
        except Exception as e:
            log.warning(f"experience_market_scene: host_repo failed (non-fatal): {e}")
    else:
        # 仓库不存在 → 调 create_from_template（建仓库+推代码+托管）
        # 如果仓库在 GitCode 上存在但 user_repos 未同步，push 可能失败——不视为致命错误
        try:
            from src.api.routes.templates import create_from_template, CreateTemplateRequest
            result = await create_from_template(
                CreateTemplateRequest(template_id=scene["recommended_template"]), user)
            full_name = result.get("full_name", full_name)
        except HTTPException as e:
            detail = e.detail if isinstance(e.detail, str) else (e.detail.get("message", "") if isinstance(e.detail, dict) else str(e.detail))
            # 仓库已存在有内容 / 源模板不存在 / push 失败 → 仓库已创建，继续托管+应用场景
            if "已存在" in detail or "已有内容" in detail or "push" in detail.lower() or "模板代码写入失败" in detail:
                already_exists = True
                log.warning(f"experience_market_scene: template push failed, trying host only: {detail}")
                try:
                    from src.api.routes.repos import RepoAction, host_repo
                    await host_repo(RepoAction(full_name=full_name), user)
                except Exception as he:
                    log.warning(f"experience_market_scene: host_repo failed (non-fatal): {he}")
            else:
                raise

    # 创建/复用仓库级 devops_pipeline_scene（scope='repo'）
    # 幂等：同一仓库同一市场场景不重复创建（按 market_scene_id + repo_key 查重）
    experience_repo_key = to_repo_key(full_name) if full_name else ""
    with db_lock:
        c = db.cursor()
        c.execute(
            "SELECT scene_id FROM devops_pipeline_scene "
            "WHERE market_scene_id=? AND repo_key=? AND scope='repo' AND is_active=1 LIMIT 1",
            (market_id, experience_repo_key),
        )
        existing = c.fetchone()

    try:
        nodes = json.loads(scene["nodes_json"]) if scene["nodes_json"] else []
    except Exception:
        nodes = []
    nodes_json_str = json.dumps(nodes, ensure_ascii=False)

    if existing:
        # 已有 repo scope 场景 → 更新
        scene_id = existing[0]
        from src.api.pipeline_config import update_custom_scene
        update_custom_scene(
            scene_id=scene_id, scope="repo", repo_key=experience_repo_key,
            user_id=user_id, nodes=nodes,
            name=scene["display_name"], description=scene["description"],
        )
    else:
        # 不存在 → 新建
        scene_id = f"custom-{uuid.uuid4().hex[:8]}"
        with db_lock:
            c = db.cursor()
            from datetime import datetime, timezone
            now = datetime.now(timezone.utc).isoformat()
            c.execute(
                "INSERT INTO devops_pipeline_scene "
                "(scene_id, name, description, scope, repo_key, user_id, nodes_json, onboarding, "
                "is_active, created_at, updated_at, market_scene_id) "
                "VALUES (?,?,?,?,?,?,?,?,1,?,?,?)",
                (scene_id, scene["display_name"], scene["description"],
                 "repo", experience_repo_key, user_id, nodes_json_str,
                 scene["onboarding"], now, now, market_id),
            )
            db.commit()

    # 调 apply_scene 应用场景配置到仓库的 agent_session
    _owner, _repo_name = full_name.split("/", 1) if "/" in full_name else (full_name, "")
    try:
        from src.api.routes.scenes import apply_scene, ApplySceneRequest
        apply_scene(_owner, _repo_name, ApplySceneRequest(scene_id=scene_id), user)
    except Exception as e:
        log.warning(f"experience_market_scene: apply_scene failed: {e}")

    log.info(f"Market scene experienced: market_id={market_id}, repo={full_name}, already_exists={already_exists}")
    msg = f"场景 '{scene['display_name']}' 已应用"
    if already_exists:
        msg = f"已跳转到现有项目 {full_name}，场景 '{scene['display_name']}' 已应用"
    return {"full_name": full_name, "scene_id": scene_id,
            "already_exists": already_exists, "message": msg}


@router.post("/market/{market_id}/like")
def toggle_scene_like(market_id: int, user: dict = Depends(get_current_user)):
    """场景点赞 toggle：已赞→取消，未赞→点赞"""
    from src.user.db import toggle_scene_like as _toggle, get_scene_market_by_id

    scene = get_scene_market_by_id(market_id)
    if not scene:
        raise HTTPException(status_code=404, detail={"code": 404, "message": "场景不存在"})

    liked, like_count = _toggle(user.get("id", 0), market_id)
    return {"liked": liked, "like_count": like_count}


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
