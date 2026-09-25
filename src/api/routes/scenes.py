"""Scenes 路由：场景列表获取、CRUD、应用"""
import json, os, re
from fastapi import APIRouter, HTTPException, Depends, UploadFile, File
from pydantic import BaseModel
from src.api.deps import get_current_user_or_api_key, require_no_session_token
from src.config import log

router = APIRouter(prefix="/api/scenes", tags=["scenes"])

_CONFIG_PATH = os.path.join(os.path.dirname(__file__), "..", "..", "..", "configs", "agent-skill-mapping.json")


def _resolve_repo_key(repo: str) -> str:
    """查 DB 确定实际 repo_key 前缀（gitcode: 或 github:）。"""
    from src.repo_key import to_repo_key
    rk = to_repo_key(repo)
    if ":" in repo:
        return rk
    try:
        from src.session_manager import db as _db, db_lock as _dl
        with _dl:
            c = _db.cursor()
            row = c.execute("SELECT repo_key FROM managed_repos WHERE repo_key IN (?, ?) LIMIT 1",
                            (f"gitcode:{repo}", f"github:{repo}")).fetchone()
            if row:
                return row[0]
    except Exception:
        pass
    return rk


def _load_scenes() -> dict:
    """读取 agent-skill-mapping.json 的场景配置（legacy fallback for apply）"""
    if not os.path.exists(_CONFIG_PATH):
        return {}
    with open(_CONFIG_PATH, "r", encoding="utf-8") as f:
        data = json.load(f)
    return data.get("scenes", {})


@router.get("", operation_id="scenes_list", summary="List scenes", description="Lists all available scenes for a repo (repo-scoped, non-official).")
def list_scenes(user: dict = Depends(require_no_session_token), repo: str = ""):
    """返回所有可用场景列表（repo，不含 official）"""
    from src.api.pipeline_config import get_scenes
    repo_key = ""
    if repo:
        repo_key = _resolve_repo_key(repo)
    else:
        try:
            _repo = app_current_repo()
            if _repo:
                repo_key = _resolve_repo_key(f"{_repo.get('owner','')}/{_repo.get('name','')}")
        except Exception:
            pass
    scenes = get_scenes(repo_key=repo_key)
    return {"scenes": scenes}


@router.get("/with-pipeline", operation_id="scenes_with_pipeline", summary="List scenes with pipeline", description="Scenes with their pipeline config (description, work branch, creator). Reuses nodes_json from the single DB query.")
def list_scenes_with_pipeline(user: dict = Depends(require_no_session_token), repo: str = ""):
    """返回场景列表 + 各场景流水线配置（含描述、工作分支、创建人）。
    
    优化：get_scenes 已在单次 DB 查询中返回 nodes_json，直接复用，
    不再逐场景调 get_pipeline_config 做二次 DB 查询。
    """
    from src.api.pipeline_config import get_scenes, get_pipeline_config
    from src.repo_key import to_repo_key
    repo_key = ""
    if repo:
        repo_key = _resolve_repo_key(repo)
    else:
        try:
            _repo = app_current_repo()
            if _repo:
                repo_key = _resolve_repo_key(f"{_repo.get('owner','')}/{_repo.get('name','')}")
        except Exception:
            pass
    scene_list = get_scenes(repo_key=repo_key)

    # 构建 skill_name → display_name 映射，给每个 node 补 skill_display_name
    # 这样前端查看模式直接用 node 上的 display_name，不依赖 dashboard API 绑定的 agent skill
    skill_display_map = {}
    try:
        from src.session_manager import db, db_lock
        with db_lock:
            c = db.cursor()
            c.execute("SELECT skill_name, display_name FROM skill WHERE is_active=1 AND scope='official'")
            skill_display_map = {r[0]: r[1] or r[0] for r in c.fetchall()}
    except Exception:
        pass

    result = []
    for s in scene_list:
        # get_scenes 已返回 nodes，直接用；DB 查不到的（official 模板）回退 get_pipeline_config
        nodes = s.get("nodes")
        if nodes is not None:
            cfg = {"name": s["name"], "nodes": nodes, "onboarding": s.get("onboarding") or "repo-onboarding"}
        else:
            cfg = get_pipeline_config(s["id"], repo_key=repo_key)
        # 给每个 node 补 skill_display_name
        for n in (cfg.get("nodes") or []):
            _sk = n.get("skill", "")
            n["skill_display_name"] = skill_display_map.get(_sk, _sk) if _sk else ""
        result.append({"id": s["id"], "name": s["name"],
                       "display_name": s.get("display_name") or s["name"],
                       "description": s.get("description") or "",
                       "work_branch": s.get("work_branch") or "main",
                       "created_by": s.get("created_by") or "",
                       "scene_type": s.get("scene_type") or "standard",
                       "pr_subscription_enabled": bool(s.get("pr_subscription_enabled", 0)),
                       "linked_repos": s.get("linked_repos") or [],
                       "pipeline": cfg})
    return {"scenes": result}


# ===== Scene Market API（场景市场）=====

def _resolve_scene_icon(icon: str, category_id: int, obs_icon_key: str = "") -> str:
    """解析场景图标为可访问的 URL 或 emoji。
    obs_icon_key 非空 → OBS 模式，动态生成签名 URL。
    icon 非空且是本地路径（/static/ 开头）→ 本地路径。
    icon 非空且是 emoji → 直接返回。
    都为空或 OBS 失败 → 查 scene_category 表拿分类 emoji 图标兜底。
    """
    if obs_icon_key:
        try:
            from src.storage import get_storage
            storage = get_storage()
            if storage is not None:
                return storage.temporary_url(obs_icon_key)
        except Exception:
            pass
        # OBS 失败时先尝试 icon 字段（可能是有效的本地路径/emoji），再 emoji 兜底
        if icon:
            return icon
        from src.user.db import get_scene_category
        cat = get_scene_category(category_id)
        return cat.get('icon', '⚙') if cat else '⚙'
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


@router.get("/market/categories", operation_id="scenes_market_categories", summary="List scene market categories", description="Scene categories ordered by order_num; only is_active=1.")
def list_scene_categories(user: dict = Depends(require_no_session_token)):
    """场景分类列表：按 order_num 排序，只返回 is_active=1 的"""
    from src.user.db import list_scene_categories as _list_cats
    return {"categories": _list_cats()}


@router.get("/market", operation_id="scenes_market_list", summary="List scene market", description="Scene market list with search/category/sort/featured/paging/scope filters. limit>0 skips paging.")
def list_scenes_market(user: dict = Depends(require_no_session_token),
                       q: str = "", category_id: int = 0, sort: str = "latest",
                       scope: str = "", repo: str = "",
                       featured_only: int = 0, limit: int = 0,
                       page: int = 1, page_size: int = 20):
    """场景市场列表：支持搜索 + 分类过滤 + 排序 + 精选过滤 + 分页 + scope 过滤。
    limit>0 时不分页（精选场景用），否则按 page/page_size 分页。
    scope: official / repo，空=全部。
    repo: scope=repo 时按 repo_key 过滤（owner/repo 格式）。
    """
    from src.user.db import (list_scene_market, batch_scene_liked,
                             batch_scene_repo_count)
    from src.repo_key import to_repo_key

    repo_key = ""
    if repo:
        repo_key = _resolve_repo_key(repo)
    elif scope in ("repo",):
        try:
            _repo = app_current_repo()
            if _repo:
                repo_key = _resolve_repo_key(f"{_repo.get('owner','')}/{_repo.get('name','')}")
        except Exception:
            pass

    scenes, total = list_scene_market(q=q, category_id=category_id, sort=sort,
                               featured_only=featured_only, limit=limit,
                               page=page, page_size=page_size,
                               scope=scope, repo_key=repo_key,
                               user_id=user.get("id", 0))
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
            "scope": s.get("scope", "official"),
        })
    # 精选场景按 admin 管理台设置的顺序排列
    if featured_only and result:
        try:
            from src.featured_order import get_featured_order
            _order = get_featured_order("scene")
            if _order:
                _order_map = {sid: i for i, sid in enumerate(_order)}
                result.sort(key=lambda x: _order_map.get(x["id"], 9999))
        except Exception:
            pass
    return {"scenes": result, "total": total, "page": page, "page_size": page_size}


@router.get("/market/{market_id}", operation_id="scenes_market_detail", summary="Get market scene detail", description="Scene market detail: full intro + skill chain + recommended template + used repo list.")
def get_scene_market_detail(market_id: int, user: dict = Depends(require_no_session_token)):
    """场景详情：完整介绍 + 技能链路详解 + 推荐模板 + 在用仓库列表"""
    from src.user.db import (get_scene_market_by_id, is_scene_liked,
                             get_scene_repos, get_scene_category)
    from src.session_manager import db, db_lock

    scene = get_scene_market_by_id(market_id)
    if not scene:
        raise HTTPException(status_code=404, detail={"code": 404, "message": "场景不存在"})

    # 解析 nodes_json，批量查 scene_skill_ref + skill 表（避免 N+1）
    skills_detail = []
    try:
        nodes = json.loads(scene["nodes_json"]) if scene["nodes_json"] else []
    except Exception:
        nodes = []
    with db_lock:
        c = db.cursor()
        # 从 scene_skill_ref 表查 skill_id（同步时已写入），不依赖 nodes_json 的 skill_id
        c.execute(
            "SELECT skill_name, skill_id FROM scene_skill_ref WHERE scene_id=?",
            (scene["id"],),
        )
        ref_map = {}
        for r in c.fetchall():
            ref_map[r[0]] = r[1]

        # 用 ref_map 的 skill_id 批量查 skill 表
        ref_skill_ids = [sid for sid in ref_map.values() if sid]
        skill_by_id = {}
        if ref_skill_ids:
            id_placeholders = ",".join("?" * len(ref_skill_ids))
            c.execute(
                f"SELECT id, skill_name, display_name, agent_type, description, scope FROM skill "
                f"WHERE id IN ({id_placeholders}) AND is_active=1",
                ref_skill_ids,
            )
            for row in c.fetchall():
                skill_by_id[row[0]] = row

        # 组装结果（基于 scene_skill_ref 的 skill_id 精确查询）
        for node in nodes:
            skill_name = node.get("skill", "")
            if not skill_name:
                continue
            skill_id = ref_map.get(skill_name, 0)
            row = skill_by_id.get(skill_id) if skill_id else None
            skills_detail.append({
                "skill_id": (row[0] if row else skill_id),
                "name": skill_name,
                "display_name": (row[2] if row and row[2] else skill_name),
                "agent_type": (row[3] if row else "") or node.get("tag", ""),
                "role_desc": (row[4] if row else "") or "",
                "scope": (row[5] if row else "official") or "official",
                "skill_deleted": not bool(row),
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
        "scope": scene.get("scope", "official"),
        "nodes_raw": nodes,
        "preset_custom_fields": scene.get("preset_custom_fields", "[]"),
    }


@router.get("/market/{market_id}/applied", operation_id="scenes_market_applied", summary="Check market scene applied", description="Checks whether a market scene is already applied to the current repo: {applied, scene_name}.")
def check_market_scene_applied(market_id: int, user: dict = Depends(require_no_session_token),
                                owner: str = "", repo: str = ""):
    """检查市场场景是否已应用到当前仓库。

    返回: {"applied": bool, "scene_name": str}
    """
    from src.repo_key import to_repo_key
    from src.session_manager import db, db_lock

    if not owner or not repo:
        return {"applied": False, "scene_name": ""}

    repo_key = _resolve_repo_key(f"{owner}/{repo}")
    with db_lock:
        c = db.cursor()
        c.execute(
            "SELECT name FROM devops_pipeline_scene "
            "WHERE market_scene_id=? AND repo_key=? AND is_active=1 LIMIT 1",
            (market_id, repo_key),
        )
        row = c.fetchone()

    return {"applied": bool(row), "scene_name": row[0] if row else ""}


@router.post("/market/{market_id}/apply", operation_id="scenes_market_apply", summary="Apply market scene", description="Applies a market scene to the repo-level pipeline scene. Idempotent per market_scene_id + repo_key.")
def apply_market_scene(market_id: int, user: dict = Depends(require_no_session_token),
                       owner: str = "", repo: str = "", repo_key: str = ""):
    """应用场景：从市场复制配置到仓库级 devops_pipeline_scene。

    - scope='repo'（方案 B：场景只与仓库关联，无 user 级）
    - 幂等：同一仓库同一市场场景不重复创建（按 market_scene_id + repo_key 查重）
    - scene_id 用 name 或 custom-{uuid}
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

    # repo_key：优先用传入的（保留 provider 前缀），否则默认 gitcode:
    if not repo_key:
        repo_key = _resolve_repo_key(f"{owner}/{repo}")
    nodes_json = scene["nodes_json"]
    try:
        nodes = json.loads(nodes_json) if nodes_json else []
    except Exception:
        nodes = []

    # 检查场景引用的 skill 是否都存在（基于 scene_skill_ref 的 skill_id 查 skill 表）
    from src.session_manager import db, db_lock
    with db_lock:
        c = db.cursor()
        c.execute("SELECT skill_name, skill_id FROM scene_skill_ref WHERE scene_id=?", (market_id,))
        ref_rows = c.fetchall()
        if ref_rows:
            ref_map = {r[0]: r[1] for r in ref_rows}
            ref_skill_ids = [sid for sid in ref_map.values() if sid]
            if ref_skill_ids:
                placeholders = ",".join("?" * len(ref_skill_ids))
                c.execute(f"SELECT id FROM skill WHERE id IN ({placeholders}) AND is_active=1", ref_skill_ids)
                existing_ids = set(r[0] for r in c.fetchall())
            else:
                existing_ids = set()
            missing = [name for name, sid in ref_map.items() if sid not in existing_ids]
            if missing:
                raise HTTPException(status_code=400, detail={"code": 400, "message": f"场景引用了已删除的技能: {', '.join(sorted(missing))}，无法应用"})

    # 幂等：先查 devops_pipeline_scene WHERE market_scene_id=? AND repo_key=?
    existing_scene_id = ""
    existing_row_id = 0
    with db_lock:
        c = db.cursor()
        c.execute(
            "SELECT scene_id, id FROM devops_pipeline_scene "
            "WHERE market_scene_id=? AND repo_key=? AND is_active=1 LIMIT 1",
            (market_id, repo_key),
        )
        row = c.fetchone()
        if row:
            existing_scene_id = row[0]
            existing_row_id = row[1]

    # display_name 查重：仓库内不能有同名场景（排除自己）
    display_name = scene["display_name"] or scene["name"]
    with db_lock:
        c = db.cursor()
        if existing_row_id:
            c.execute(
                "SELECT name FROM devops_pipeline_scene "
                "WHERE name=? AND repo_key=? AND is_active=1 AND id!=? LIMIT 1",
                (display_name, repo_key, existing_row_id),
            )
        else:
            c.execute(
                "SELECT name FROM devops_pipeline_scene "
                "WHERE name=? AND repo_key=? AND is_active=1 LIMIT 1",
                (display_name, repo_key),
            )
        conflict = c.fetchone()
    if conflict:
        raise HTTPException(status_code=409, detail={"code": 409, "message": f"仓库中已存在名为'{display_name}'的场景，请先修改已有场景名称后再应用"})

    if existing_scene_id:
        # 已存在 → 更新
        update_custom_scene(
            scene_id=existing_scene_id, repo_key=repo_key, nodes=nodes,
            name=scene["display_name"], description=scene["description"],
        )
        scene_id = existing_scene_id
    else:
        # 不存在 → 新建（scope='repo'，scene_id 用 custom-uuid 或市场 name）
        new_scene_id = scene["name"] or f"custom-{uuid.uuid4().hex[:8]}"
        db_id = create_custom_scene(
            scene_id=new_scene_id, name=scene["display_name"] or new_scene_id,
            description=scene["description"] or "",
            repo_key=repo_key, nodes=nodes, onboarding=scene["onboarding"] or "repo-onboarding",
            created_by=user.get("username", ""))
        scene_id = new_scene_id
        # 回写 market_scene_id 关联
        try:
            with db_lock:
                c = db.cursor()
                c.execute("UPDATE devops_pipeline_scene SET market_scene_id=? WHERE id=?", (market_id, db_id))
                db.commit()
                log.info(f"apply_market_scene: UPDATE market_scene_id={market_id} WHERE id={db_id}, rowcount={c.rowcount}")
        except Exception as _e:
            log.warning(f"apply_market_scene: link market_scene_id failed: {_e}")

    # 合并预置自定义参数（Issue #246）：仅追加缺失项，不覆盖用户已有配置
    try:
        _merge_preset_custom_fields(scene_id, repo_key, scene.get("preset_custom_fields", "[]"))
    except Exception as _e:
        log.warning(f"apply_market_scene: merge preset custom fields failed: {_e}")

    log.info(f"Market scene applied: market_id={market_id}, scene_id={scene_id}, repo={repo_key}")
    return {"message": f"场景 '{scene['display_name']}' 已创建", "scene_id": scene_id}


@router.get("/market/{market_id}/experience-check", operation_id="scenes_market_experience_check", summary="Market scene experience pre-check", description="Pre-check before experiencing: {repo_exists, scene_applied, full_name, scene_name}.")
async def check_market_scene_experience(market_id: int, user: dict = Depends(require_no_session_token)):
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
    repo_key = _resolve_repo_key(full_name)
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
            "WHERE market_scene_id=? AND repo_key=? AND is_active=1 LIMIT 1",
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


@router.post("/market/{market_id}/experience", operation_id="scenes_market_experience", summary="Experience market scene", description="Experiences a scene: creates/reuses the recommended-template project and applies the scene.")
async def experience_market_scene(market_id: int, user: dict = Depends(require_no_session_token)):
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
    repo_key = _resolve_repo_key(full_name)
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
            host_repo(RepoAction(full_name=full_name), user)
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
                    host_repo(RepoAction(full_name=full_name), user)
                except Exception as he:
                    log.warning(f"experience_market_scene: host_repo failed (non-fatal): {he}")
            else:
                raise

    # 创建/复用仓库级 devops_pipeline_scene（只与仓库关联，方案 B）
    # 幂等：同一仓库同一市场场景不重复创建（按 market_scene_id + repo_key 查重）
    experience_repo_key = _resolve_repo_key(full_name) if full_name else ""
    existing_scene_id = ""
    existing_row_id = 0
    with db_lock:
        c = db.cursor()
        c.execute(
            "SELECT scene_id, id FROM devops_pipeline_scene "
            "WHERE market_scene_id=? AND repo_key=? AND is_active=1 LIMIT 1",
            (market_id, experience_repo_key),
        )
        existing = c.fetchone()
        if existing:
            existing_scene_id = existing[0]
            existing_row_id = existing[1]

    try:
        nodes = json.loads(scene["nodes_json"]) if scene["nodes_json"] else []
    except Exception:
        nodes = []
    nodes_json_str = json.dumps(nodes, ensure_ascii=False)

    # display_name 查重：仓库内不能有同名场景（排除自己）
    display_name = scene["display_name"] or scene["name"]
    with db_lock:
        c = db.cursor()
        if existing_row_id:
            c.execute(
                "SELECT name FROM devops_pipeline_scene "
                "WHERE name=? AND repo_key=? AND is_active=1 AND id!=? LIMIT 1",
                (display_name, experience_repo_key, existing_row_id),
            )
        else:
            c.execute(
                "SELECT name FROM devops_pipeline_scene "
                "WHERE name=? AND repo_key=? AND is_active=1 LIMIT 1",
                (display_name, experience_repo_key),
            )
        conflict = c.fetchone()
    if conflict:
        raise HTTPException(status_code=409, detail={"code": 409, "message": f"仓库中已存在名为'{display_name}'的场景，请先修改已有场景名称后再应用", "full_name": full_name})

    if existing_scene_id:
        # 已有 repo 场景 → 更新
        scene_id = existing_scene_id
        from src.api.pipeline_config import update_custom_scene
        update_custom_scene(
            scene_id=scene_id, repo_key=experience_repo_key, nodes=nodes,
            name=scene["display_name"], description=scene["description"],
        )
    else:
        # 不存在 → 新建
        scene_id = str(uuid.uuid4())
        with db_lock:
            c = db.cursor()
            from datetime import datetime, timezone
            now = datetime.now(timezone.utc).isoformat()
            c.execute(
                "INSERT INTO devops_pipeline_scene "
                "(scene_id, name, description, repo_key, nodes_json, onboarding, "
                "is_active, created_at, updated_at, market_scene_id) "
                "VALUES (?,?,?,?,?,?,1,?,?,?)",
                (scene_id, scene["display_name"], scene["description"],
                 experience_repo_key, nodes_json_str,
                 scene["onboarding"], now, now, market_id),
            )
            db.commit()

    log.info(f"Market scene experienced: market_id={market_id}, repo={full_name}, already_exists={already_exists}")
    msg = f"场景 '{scene['display_name']}' 已创建"
    if already_exists:
        msg = f"已跳转到现有项目 {full_name}，场景 '{scene['display_name']}' 已创建"
    return {"full_name": full_name, "scene_id": scene_id,
            "already_exists": already_exists, "message": msg}


@router.post("/market/{market_id}/like", operation_id="scenes_market_like_toggle", summary="Toggle scene like", description="Toggles a scene like.")
def toggle_scene_like(market_id: int, user: dict = Depends(require_no_session_token)):
    """场景点赞 toggle：已赞→取消，未赞→点赞"""
    from src.user.db import toggle_scene_like as _toggle, get_scene_market_by_id

    scene = get_scene_market_by_id(market_id)
    if not scene:
        raise HTTPException(status_code=404, detail={"code": 404, "message": "场景不存在"})

    liked, like_count = _toggle(user.get("id", 0), market_id)
    return {"liked": liked, "like_count": like_count}


# ===== 分支管理（必须在 /{scene_id} 之前声明，避免路由被 scene_id 吞掉）=====

class CreateBranchRequest(BaseModel):
    branch_name: str
    refs: str = "main"


def _detect_repo_provider(owner: str, repo: str) -> str:
    """根据 managed_repos.repo_key 前缀判断仓库属于 gitcode 还是 github。

    优先查 managed_repos 表（托管仓库有明确的 provider 前缀）；
    其次查 user_repos（用户绑定的仓库缓存）；
    默认返回 'gitcode'。
    """
    from src.user.db import db_lock, db
    from src.repo_key import to_repo_key
    for prov in ("github", "gitcode"):
        rk = to_repo_key(f"{owner}/{repo}", provider=prov)
        try:
            with db_lock:
                c = db.cursor()
                c.execute("SELECT 1 FROM managed_repos WHERE repo_key=? LIMIT 1", (rk,))
                if c.fetchone():
                    return prov
        except Exception:
            pass
        try:
            with db_lock:
                c = db.cursor()
                c.execute("SELECT 1 FROM user_repos WHERE repo_key=? LIMIT 1", (rk,))
                if c.fetchone():
                    return prov
        except Exception:
            pass
    return "gitcode"


@router.get("/branches/{owner}/{repo}", operation_id="scenes_branches", summary="List repo branches", description="Lists all repo branches (for the advanced-options work-branch picker).")
def list_repo_branches(owner: str, repo: str, user: dict = Depends(require_no_session_token)):
    """列出仓库所有分支（供高级选项工作分支选择）"""
    from src.user.repo_manager import _req, _github_req

    provider = _detect_repo_provider(owner, repo)

    if provider == "github":
        from src.user.db import get_github_token
        from src.repo_key import to_repo_key
        rk = to_repo_key(f"{owner}/{repo}", provider="github")
        token = get_github_token(repo_key=rk)
        if not token:
            raise HTTPException(status_code=503, detail={"code": 503, "message": "GitHub Bot Token 未配置，无法查询分支"})
        branches = []
        try:
            page = 1
            while page <= 5:
                url = f"https://api.github.com/repos/{owner}/{repo}/branches?per_page=100&page={page}"
                data = _github_req("GET", url, token)
                if not isinstance(data, list) or not data:
                    break
                for b in data:
                    branches.append({
                        "name": b.get("name", ""),
                        "protected": bool(b.get("protected", False)),
                    })
                if len(data) < 100:
                    break
                page += 1
        except Exception as e:
            log.warning(f"List branches failed for GitHub {owner}/{repo}: {e}")
            raise HTTPException(status_code=502, detail={"code": 502, "message": f"查询分支失败: {e}"})
        return {"branches": branches}

    # GitCode（原逻辑）
    from src.user.db import get_bot_token_by_repo

    # 仓库 bot token → 全局 bot token
    token = get_bot_token_by_repo(owner, repo)
    if not token:
        try:
            import os as _os, json as _json
            _gbt_path = _os.path.join(_os.path.dirname(__file__), "..", "..", "..", "configs", "global-bot-token.json")
            if _os.path.exists(_gbt_path):
                with open(_gbt_path, "r", encoding="utf-8") as _f:
                    _gbt = _json.load(_f)
                token = _gbt.get("gitcode_api_token", "") or ""
        except Exception:
            pass
    if not token:
        raise HTTPException(status_code=503, detail={"code": 503, "message": "Bot Token 未配置，无法查询分支"})

    branches = []
    try:
        page = 1
        while page <= 5:
            url = f"https://gitcode.com/api/v5/repos/{owner}/{repo}/branches?per_page=100&page={page}"
            data = _req("GET", url, token)
            if not isinstance(data, list) or not data:
                break
            for b in data:
                branches.append({
                    "name": b.get("name", ""),
                    "protected": bool(b.get("protected", False)),
                })
            if len(data) < 100:
                break
            page += 1
    except Exception as e:
        log.warning(f"List branches failed for {owner}/{repo}: {e}")
        raise HTTPException(status_code=502, detail={"code": 502, "message": f"查询分支失败: {e}"})

    return {"branches": branches}


@router.post("/branches/{owner}/{repo}", operation_id="scenes_branch_create", summary="Create repo branch", description="Creates a new branch in the repo.")
def create_repo_branch(owner: str, repo: str, body: CreateBranchRequest,
                        user: dict = Depends(require_no_session_token)):
    """在仓库中创建新分支"""
    from src.user.repo_manager import _req, _github_req

    provider = _detect_repo_provider(owner, repo)

    if provider == "github":
        from src.user.db import get_github_token
        from src.repo_key import to_repo_key
        rk = to_repo_key(f"{owner}/{repo}", provider="github")
        token = get_github_token(repo_key=rk)
        if not token:
            raise HTTPException(status_code=503, detail={"code": 503, "message": "GitHub Bot Token 未配置，无法创建分支"})
        if not body.branch_name or not body.branch_name.strip():
            raise HTTPException(status_code=400, detail={"code": 400, "message": "分支名不能为空"})
        try:
            # GitHub 创建分支：先获取 base branch 的 sha，再创建 ref
            base_ref = body.refs or "main"
            # 尝试获取 base branch 的 commit sha
            sha = None
            try:
                branch_info = _github_req("GET",
                    f"https://api.github.com/repos/{owner}/{repo}/branches/{base_ref}", token)
                sha = branch_info.get("commit", {}).get("sha", "")
            except Exception:
                pass
            if not sha:
                # fallback: 获取默认分支
                repo_info = _github_req("GET", f"https://api.github.com/repos/{owner}/{repo}", token)
                default_branch = repo_info.get("default_branch", "main")
                branch_info = _github_req("GET",
                    f"https://api.github.com/repos/{owner}/{repo}/branches/{default_branch}", token)
                sha = branch_info.get("commit", {}).get("sha", "")
            if not sha:
                raise HTTPException(status_code=502, detail={"code": 502, "message": "无法获取基准分支 SHA"})
            url = f"https://api.github.com/repos/{owner}/{repo}/git/refs"
            _github_req("POST", url, token, {
                "ref": f"refs/heads/{body.branch_name.strip()}",
                "sha": sha,
            })
            log.info(f"Branch created: {body.branch_name} in GitHub {owner}/{repo}")
            return {"name": body.branch_name.strip(), "message": "分支已创建"}
        except RuntimeError as e:
            err_msg = str(e)
            if "422" in err_msg or "already exists" in err_msg:
                raise HTTPException(status_code=409, detail={"code": 409, "message": "分支已存在"})
            if "403" in err_msg:
                raise HTTPException(status_code=403, detail={"code": 403, "message": "需要维护者权限才能创建分支"})
            raise HTTPException(status_code=502, detail={"code": 502, "message": f"创建分支失败: {err_msg}"})
        except HTTPException:
            raise
        except Exception as e:
            raise HTTPException(status_code=502, detail={"code": 502, "message": f"创建分支失败: {e}"})

    # GitCode（原逻辑）
    from src.user.db import get_bot_token_by_repo

    # 仓库 bot token → 全局 bot token
    token = get_bot_token_by_repo(owner, repo)
    if not token:
        try:
            import os as _os, json as _json
            _gbt_path = _os.path.join(_os.path.dirname(__file__), "..", "..", "..", "configs", "global-bot-token.json")
            if _os.path.exists(_gbt_path):
                with open(_gbt_path, "r", encoding="utf-8") as _f:
                    _gbt = _json.load(_f)
                token = _gbt.get("gitcode_api_token", "") or ""
        except Exception:
            pass
    if not token:
        raise HTTPException(status_code=503, detail={"code": 503, "message": "Bot Token 未配置，无法创建分支"})

    if not body.branch_name or not body.branch_name.strip():
        raise HTTPException(status_code=400, detail={"code": 400, "message": "分支名不能为空"})

    try:
        url = f"https://gitcode.com/api/v5/repos/{owner}/{repo}/branches"
        result = _req("POST", url, token, {
            "branch_name": body.branch_name.strip(),
            "refs": body.refs or "main",
        })
        log.info(f"Branch created: {body.branch_name} in {owner}/{repo}")
        return {"name": body.branch_name.strip(), "message": "分支已创建"}
    except RuntimeError as e:
        err_msg = str(e)
        if "409" in err_msg:
            raise HTTPException(status_code=409, detail={"code": 409, "message": "分支已存在"})
        if "403" in err_msg:
            raise HTTPException(status_code=403, detail={"code": 403, "message": "需要维护者权限才能创建分支"})
        raise HTTPException(status_code=502, detail={"code": 502, "message": f"创建分支失败: {err_msg}"})
    except Exception as e:
        raise HTTPException(status_code=502, detail={"code": 502, "message": f"创建分支失败: {e}"})
# ===== Scene Market CRUD（用户自定义场景，Issue-54）=====

class CreateMarketSceneRequest(BaseModel):
    name: str
    display_name: str = ""
    description: str = ""
    scope: str = "repo"  # repo
    nodes: list = []
    icon: str = ""
    obs_icon_key: str = ""
    file_id: int = 0  # uploaded_files 表 id（图标临时文件）
    category_id: int = 1
    recommended_template: str = ""
    linked_repos: list = []  # [{repo_key, branch}] 关联仓库（跨仓库协同开发）


@router.post("/market", operation_id="scenes_market_create", summary="Create market scene", description="Creates a user custom scene in scene_market. scope=repo: repo scene visible to all repo users.")
def create_market_scene(body: CreateMarketSceneRequest, user: dict = Depends(require_no_session_token),
                        repo: str = ""):
    """创建用户自定义场景（写入 scene_market 表）。

    scope=repo: 仓库场景（仓库所有用户可见）
    """
    import uuid as _uuid
    from src.user.db import (create_scene_market, commit_uploaded_file,
                             delete_uploaded_file, get_uploaded_file)
    from src.repo_key import to_repo_key
    from src.storage import get_storage

    if body.scope not in ("repo",):
        raise HTTPException(status_code=400, detail={"code": 400, "message": "scope 必须为 repo"})

    # 获取 repo_key
    repo_key = ""
    if repo:
        repo_key = _resolve_repo_key(repo)
    else:
        try:
            _repo = app_current_repo()
            if _repo:
                repo_key = _resolve_repo_key(f"{_repo.get('owner','')}/{_repo.get('name','')}")
        except Exception:
            pass
    if not repo_key:
        raise HTTPException(status_code=400, detail={"code": 400, "message": "请先选择仓库"})

    # 校验 name 是否重复
    from src.user.db import check_scene_name_exists
    if len(body.name) > 40:
        raise HTTPException(status_code=400, detail={"code": 400, "message": "场景标识不能超过40个字符"})
    if len(body.display_name) > 40:
        raise HTTPException(status_code=400, detail={"code": 400, "message": "场景名称不能超过40个字符"})
    if check_scene_name_exists(name=body.name, scope=body.scope, repo_key=repo_key):
        raise HTTPException(status_code=409, detail={"code": 409, "message": f"场景 '{body.name}' 已存在"})

    nodes_json = json.dumps(body.nodes, ensure_ascii=False)

    try:
        market_id = create_scene_market(
            name=body.name, display_name=body.display_name, description=body.description,
            scope=body.scope, repo_key=repo_key, source_user_id=user.get("id", 0),
            nodes_json=nodes_json, icon=body.icon, obs_icon_key=body.obs_icon_key,
            category_id=body.category_id, recommended_template=body.recommended_template,
            created_by=user.get("username", ""),
        )
    except Exception as e:
        err_upper = str(e).upper()
        if "UNIQUE" in err_upper or "DUPLICATE" in err_upper:
            raise HTTPException(status_code=409, detail={"code": 409, "message": f"场景 '{body.name}' 已存在"})
        raise HTTPException(status_code=500, detail={"code": 500, "message": str(e)})

    # 图标文件落定：临时 key → 正式 key
    if body.file_id:
        final_icon, final_obs_key = _finalize_scene_icon(body.file_id, market_id, body.obs_icon_key, user)
        # 更新 scene_market 的 icon 为正式路径
        if final_icon:
            from src.session_manager import db, db_lock
            try:
                with db_lock:
                    c = db.cursor()
                    c.execute("UPDATE scene_market SET icon=?, obs_icon_key=? WHERE id=?", (final_icon, final_obs_key, market_id))
                    db.commit()
            except Exception as e:
                log.warning(f"create_market_scene: update icon failed: {e}")

    log.info(f"Market scene created: id={market_id} name={body.name} scope={body.scope} repo={repo_key} by user={user.get('id')}")

    # 重建 scene_skill_ref（仓库场景也需要）
    try:
        from src.storage.scene_sync import _rebuild_scene_skill_ref
        _rebuild_scene_skill_ref(market_id, body.nodes, body.name)
    except Exception as e:
        log.warning(f"create_market_scene: rebuild scene_skill_ref failed: {e}")

    return {"id": market_id, "message": "场景创建成功"}


class UpdateMarketSceneRequest(BaseModel):
    name: str = ""
    display_name: str = ""
    description: str = ""
    nodes: list = []
    icon: str = ""
    obs_icon_key: str = ""
    file_id: int = 0  # 新上传的图标临时文件 id（0=未更换图标）
    category_id: int = 0
    recommended_template: str = ""
    linked_repos: list = []  # [{repo_key, branch}] 关联仓库（跨仓库协同开发）


@router.put("/market/{market_id}", operation_id="scenes_market_update", summary="Update market scene", description="Updates a custom market scene (scope != official).")
def update_market_scene(market_id: int, body: UpdateMarketSceneRequest,
                        user: dict = Depends(require_no_session_token)):
    """更新用户自定义场景（scope != 'official'）。"""
    from src.user.db import (update_scene_market, get_scene_market_by_id,
                             delete_uploaded_file, get_uploaded_files_by_ref)

    scene = get_scene_market_by_id(market_id)
    if not scene:
        raise HTTPException(status_code=404, detail={"code": 404, "message": "场景不存在"})
    if scene.get("scope") == "official":
        raise HTTPException(status_code=403, detail={"code": 403, "message": "官方场景不可编辑"})

    nodes_json = json.dumps(body.nodes, ensure_ascii=False) if body.nodes else ""

    # 图标文件落定
    if body.file_id:
        # 删除旧图标文件
        old_files = get_uploaded_files_by_ref("scene_market", market_id)
        for old_f in old_files:
            if old_f.get("file_type") == "scene_icon":
                _delete_scene_icon_file(delete_uploaded_file(old_f["id"]))
        # _finalize_scene_icon 只做文件操作，返回正式路径
        final_icon, final_obs_key = _finalize_scene_icon(body.file_id, market_id, body.obs_icon_key, user)
        body.icon = final_icon
        body.obs_icon_key = final_obs_key

    # name 变更时校验重复
    if body.display_name and len(body.display_name) > 40:
        raise HTTPException(status_code=400, detail={"code": 400, "message": "场景名称不能超过40个字符"})
    if body.name and len(body.name) > 40:
        raise HTTPException(status_code=400, detail={"code": 400, "message": "场景标识不能超过40个字符"})
    if body.name and body.name != scene.get("name"):
        from src.user.db import check_scene_name_exists
        if check_scene_name_exists(
            name=body.name, scope=scene.get("scope", "repo"),
            repo_key=scene.get("repo_key", ""),
            source_user_id=scene.get("source_user_id", 0),
            exclude_id=market_id,
        ):
            raise HTTPException(status_code=409, detail={"code": 409, "message": f"场景 '{body.name}' 已存在"})

    success = update_scene_market(
        market_id=market_id, name=body.name, display_name=body.display_name,
        description=body.description, nodes_json=nodes_json,
        icon=body.icon, obs_icon_key=body.obs_icon_key,
        category_id=body.category_id, recommended_template=body.recommended_template,
        updated_by=user.get("username", ""),
    )
    if not success:
        raise HTTPException(status_code=404, detail={"code": 404, "message": "场景不存在或无更新"})

    # 重建 scene_skill_ref
    try:
        from src.storage.scene_sync import _rebuild_scene_skill_ref
        _rebuild_scene_skill_ref(market_id, body.nodes, body.name or scene.get("name", ""))
    except Exception as e:
        log.warning(f"update_market_scene: rebuild scene_skill_ref failed: {e}")

    log.info(f"Market scene updated: id={market_id} by user={user.get('id')}")
    return {"message": "场景更新成功"}


@router.delete("/market/{market_id}", operation_id="scenes_market_delete", summary="Delete market scene", description="Hard-deletes a custom market scene (scope != official).")
def delete_market_scene(market_id: int, user: dict = Depends(require_no_session_token)):
    """硬删除用户自定义场景（scope != 'official'）。"""
    from src.user.db import (delete_scene_market, get_scene_market_by_id,
                             delete_uploaded_file, get_uploaded_files_by_ref)

    scene = get_scene_market_by_id(market_id)
    if not scene:
        raise HTTPException(status_code=404, detail={"code": 404, "message": "场景不存在"})
    if scene.get("scope") == "official":
        raise HTTPException(status_code=403, detail={"code": 403, "message": "官方场景不可删除"})

    # 删除关联的图标文件
    old_files = get_uploaded_files_by_ref("scene_market", market_id)
    for old_f in old_files:
        if old_f.get("file_type") == "scene_icon":
            _delete_scene_icon_file(delete_uploaded_file(old_f["id"]))

    success = delete_scene_market(market_id)
    if not success:
        raise HTTPException(status_code=404, detail={"code": 404, "message": "场景不存在或不可删除"})
    log.info(f"Market scene deleted: id={market_id} by user={user.get('id')}")
    return {"message": "场景已删除"}

@router.get("/check-name", operation_id="scenes_check_name", summary="Check scene name availability", description="Checks scene name uniqueness within the same scope/user/repo.")
def check_scene_name(name: str, scope: str, user: dict = Depends(require_no_session_token),
                     repo: str = "", exclude_id: int = 0):
    """检查场景名称是否重复（同 scope 同用户同仓库下名称唯一）。"""
    from src.user.db import check_scene_name_exists
    from src.repo_key import to_repo_key

    repo_key = ""
    if repo:
        repo_key = _resolve_repo_key(repo)
    else:
        try:
            _repo = app_current_repo()
            if _repo:
                repo_key = _resolve_repo_key(f"{_repo.get('owner','')}/{_repo.get('name','')}")
        except Exception:
            pass

    exists = check_scene_name_exists(
        name=name, scope=scope, repo_key=repo_key,
        source_user_id=user.get("id", 0), exclude_id=exclude_id,
    )
    return {"exists": exists}


@router.post("/market/icon-upload", operation_id="scenes_market_icon_upload", summary="Upload scene icon", description="Uploads a scene icon (temporary file). Allowed jpg/png/webp/gif/svg <= 2MB. Returns {file_id, icon, obs_icon_key}.")
async def upload_scene_icon(file: UploadFile = File(...),
                            user: dict = Depends(require_no_session_token)):
    """上传场景图标（临时文件，保存时落定）。

    限制：格式 jpg/png/webp/gif/svg，大小 ≤ 2MB。
    返回 {file_id, icon, obs_icon_key}。
    """
    import uuid as _uuid
    from src.user.db import insert_uploaded_file
    from src.storage import get_storage

    # 校验格式
    allowed_types = {"image/jpeg", "image/png", "image/webp", "image/gif", "image/svg+xml"}
    allowed_exts = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".svg"}
    content_type = file.content_type or ""
    filename = file.filename or ""
    ext = os.path.splitext(filename)[1].lower()

    if content_type not in allowed_types and ext not in allowed_exts:
        raise HTTPException(status_code=400, detail={"code": 400, "message": "仅支持 jpg/png/webp/gif/svg 格式"})

    # 读取并校验大小
    content = await file.read()
    if len(content) > 2 * 1024 * 1024:
        raise HTTPException(status_code=400, detail={"code": 400, "message": "图标文件不能超过 2MB"})

    if not ext:
        ext = ".png"  # 兜底

    # 生成临时 key
    tmp_uuid = _uuid.uuid4().hex[:12]
    file_key = f"scene-icons/tmp-{tmp_uuid}{ext}"

    storage = get_storage()
    if storage:
        # OBS 模式
        storage.save_bytes(file_key, content)
        icon_url = storage.temporary_url(file_key)
        obs_icon_key = file_key
    else:
        # 非 OBS 模式：写本地
        static_dir = os.path.join(os.path.dirname(__file__), "..", "static", "uploads", "scene-icons")
        os.makedirs(static_dir, exist_ok=True)
        local_path = os.path.join(static_dir, f"tmp-{tmp_uuid}{ext}")
        with open(local_path, "wb") as f:
            f.write(content)
        icon_url = f"/static/uploads/scene-icons/tmp-{tmp_uuid}{ext}"
        obs_icon_key = ""

    # 插入 uploaded_files 记录
    file_id = insert_uploaded_file(
        file_key=file_key, original_name=filename,
        file_size=len(content), mime_type=content_type,
        file_type="scene_icon", created_by=user.get("username", ""),
    )

    return {"file_id": file_id, "icon": icon_url, "obs_icon_key": obs_icon_key}


def _finalize_scene_icon(file_id: int, market_id: int, obs_icon_key: str, user: dict) -> tuple:
    """将临时图标文件落定为正式文件（重命名 key + 更新 uploaded_files + 更新 scene_market icon 字段）。"""
    from src.user.db import (get_uploaded_file, commit_uploaded_file,
                             delete_uploaded_file)
    from src.storage import get_storage
    from src.session_manager import db, db_lock

    file_rec = get_uploaded_file(file_id)
    if not file_rec:
        return "", ""

    old_key = file_rec["file_key"]
    ext = os.path.splitext(old_key)[1] or ".png"
    new_key = f"scene-icons/{market_id}{ext}"

    storage = get_storage()
    if storage:
        # OBS 模式：copy 到正式 key + 删临时 key
        try:
            content = storage.read_bytes(old_key)
            if content:
                storage.save_bytes(new_key, content)
                storage.delete_object(old_key)
        except Exception as e:
            log.warning(f"Scene icon finalize (OBS): {e}")
        # 更新 scene_market 的 icon 和 obs_icon_key
        final_icon = storage.temporary_url(new_key) if hasattr(storage, 'temporary_url') else ""
        final_obs_key = new_key
    else:
        # 非 OBS 模式：重命名本地文件
        static_dir = os.path.join(os.path.dirname(__file__), "..", "static", "uploads", "scene-icons")
        old_path = os.path.join(static_dir, os.path.basename(old_key))
        new_path = os.path.join(static_dir, f"{market_id}{ext}")
        try:
            if os.path.exists(old_path):
                os.rename(old_path, new_path)
        except Exception as e:
            log.warning(f"Scene icon finalize (local): {e}")
        # 更新 scene_market 的 icon 为正式本地路径
        final_icon = f"/static/uploads/scene-icons/{market_id}{ext}"
        final_obs_key = ""

    commit_uploaded_file(file_id, "scene_market", market_id)

    # 返回正式路径，由调用方设置到 body 让 update_scene_market 统一更新
    return final_icon, final_obs_key


def _delete_scene_icon_file(file_key: str):
    """删除图标物理文件（OBS 或本地）。"""
    if not file_key:
        return
    from src.storage import get_storage

    storage = get_storage()
    if storage:
        try:
            storage.delete_object(file_key)
        except Exception as e:
            log.warning(f"Scene icon delete (OBS): {e}")
    else:
        # 非 OBS 模式
        static_dir = os.path.join(os.path.dirname(__file__), "..", "static", "uploads", "scene-icons")
        local_path = os.path.join(static_dir, os.path.basename(file_key))
        try:
            if os.path.exists(local_path):
                os.remove(local_path)
        except Exception as e:
            log.warning(f"Scene icon delete (local): {e}")


@router.get("/{scene_id}", operation_id="scenes_detail", summary="Get scene detail", description="Scene detail with nodes + engine info. Dual: user (JWT/API key) and session token (tk_, agent reads its current scene pipeline on demand).")
def get_scene_detail(scene_id: str, user: dict = Depends(get_current_user_or_api_key)):
    """返回场景详情（nodes + engine 信息）"""
    from src.api.pipeline_config import get_pipeline_config
    from src.repo_key import to_repo_key
    repo_key = ""
    try:
        repo = app_current_repo()
        if repo:
            repo_key = _resolve_repo_key(f"{repo.get('owner','')}/{repo.get('name','')}")
    except Exception:
        pass
    cfg = get_pipeline_config(scene_id, repo_key=repo_key)
    if not cfg:
        raise HTTPException(status_code=404, detail={"code": 404, "message": f"场景 '{scene_id}' 不存在"})
    return {"id": scene_id, "name": cfg["name"], "nodes": cfg["nodes"],
            "onboarding": cfg.get("onboarding", ""),
            "scene_type": cfg.get("scene_type", "standard")}


@router.get("/skills/{agent_type}", operation_id="scenes_skills_for_agent", summary="List skills for agent type", description="Lists selectable skills for an agent type (search + scope filter + paging).")
def list_skills_for_agent(agent_type: str, user: dict = Depends(require_no_session_token),
                          q: str = "", scope: str = "", repo: str = "",
                          page: int = 1, page_size: int = 20):
    """返回该 agent_type 可选的 skill 列表（支持搜索 + scope 过滤 + 分页）。"""
    from src.session_manager import db, db_lock
    skills = []
    where = ["agent_type=?", "is_active=1"]
    params: list = [agent_type]

    # skill 可见范围：official + 个人 user skill + 当前仓库的 repo skill
    repo_key = ""
    if repo:
        from src.repo_key import to_repo_key
        repo_key = _resolve_repo_key(repo)
    else:
        try:
            _repo = app_current_repo()
            if _repo:
                from src.repo_key import to_repo_key
                repo_key = _resolve_repo_key(f"{_repo.get('owner','')}/{_repo.get('name','')}")
        except Exception:
            pass
    user_id = str(user.get("id", ""))
    where.append("(scope='official' OR (scope='user' AND owner_id=?) OR (scope='repo' AND owner_id=?))")
    params.extend([user_id, repo_key])

    if q:
        where.append("(skill_name LIKE ? OR display_name LIKE ?)")
        params.extend([f"%{q}%", f"%{q}%"])
    if scope:
        where.append("scope=?")
        params.append(scope)

    where_sql = " AND ".join(where)

    with db_lock:
        c = db.cursor()
        c.execute(f"SELECT COUNT(*) FROM skill WHERE {where_sql}", params)
        total = c.fetchone()[0]
        offset = (page - 1) * page_size
        c.execute(
            f"SELECT id, skill_name, display_name, scope, owner_id, description, agent_type FROM skill "
            f"WHERE {where_sql} ORDER BY scope, skill_name LIMIT ? OFFSET ?",
            params + [page_size, offset],
        )
        for r in c.fetchall():
            skills.append({"skill_id": r[0], "skill_name": r[1], "display_name": r[2] or r[1], "scope": r[3], "owner_id": r[4] or "", "description": r[5] or "", "agent_type": r[6] or ""})
    return {"skills": skills, "total": total}


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
    scope: str = "repo"  # repo
    nodes: list[SceneNodeInput] = []
    icon: str = ""
    scene_type: str = "standard"  # deprecated, 保留向后兼容
    pr_subscription_enabled: bool = False
    linked_repos: list = []  # [{repo_key, branch}] 关联仓库（跨仓库协同开发）

class UpdateSceneRequest(BaseModel):
    name: str | None = None
    description: str | None = None
    nodes: list[SceneNodeInput] = []
    icon: str | None = None
    pr_subscription_enabled: bool | None = None
    linked_repos: list | None = None  # None=不更新；[] = 清空关联仓库

class ApplySceneRequest(BaseModel):
    scene_id: str


def _validate_scene_engine_ids(nodes: list, repo_key: str):
    """校验场景 nodes 的 engine_runtime_ids：
    1. 引擎 ID 必须存在
    2. scope=repo 的引擎 owner_id 必须匹配当前仓库
    3. 引擎必须有对应节点 tag 的能力（engine_capabilities.tag 或 'all'）

    校验失败抛 HTTPException(400)。
    """
    from src.session_manager import db, db_lock
    from src.repo_key import from_repo_key
    _repo_norm = (from_repo_key(repo_key) or "").strip()
    for node in nodes:
        tag = node.get("tag") or node.get("id") or ""
        eids = node.get("engine_runtime_ids") or []
        if not eids:
            continue
        if not tag:
            continue
        for eid in eids:
            try:
                _eid = int(eid)
            except (ValueError, TypeError):
                continue
            with db_lock:
                c = db.cursor()
                # 查引擎 + credential scope/owner_id
                c.execute(
                    "SELECT r.credential_id, rc.scope, rc.owner_id "
                    "FROM agent_runtimes r LEFT JOIN runtime_credentials rc ON rc.id=r.credential_id "
                    "WHERE r.id=?", (_eid,))
                row = c.fetchone()
            if not row:
                raise HTTPException(status_code=400, detail={
                    "code": 400,
                    "message": f"节点 {tag} 的引擎 ID {_eid} 不存在"})
            _cred_id, _scope, _owner_id = row[0] or 0, row[1] or "global", row[2] or ""
            # scope=repo 时校验仓库归属
            if _scope == "repo" and _cred_id > 0:
                _owner_norm = (from_repo_key(_owner_id) or "").strip()
                if _owner_norm and _repo_norm and _owner_norm != _repo_norm:
                    raise HTTPException(status_code=400, detail={
                        "code": 400,
                        "message": f"节点 {tag} 的引擎 {_eid} 属于仓库 {_owner_norm}，不是当前仓库 {_repo_norm}"})
            # 校验 tag 能力
            with db_lock:
                c = db.cursor()
                c.execute(
                    "SELECT 1 FROM engine_capabilities "
                    "WHERE runtime_id=? AND enabled=1 AND (tag=? OR tag='all') LIMIT 1",
                    (_eid, tag))
                if not c.fetchone():
                    raise HTTPException(status_code=400, detail={
                        "code": 400,
                        "message": f"节点 {tag} 的引擎 {_eid} 没有该角色能力标签（{tag}），请重新选择"})


@router.post("", operation_id="scenes_create", summary="Create scene", description="Creates a custom scene (associated only with the repo, no user-level).")
def create_scene(body: CreateSceneRequest, user: dict = Depends(require_no_session_token), repo: str = ""):
    """创建自定义场景（方案 B：场景只与仓库关联，无 user 级）"""
    from src.api.pipeline_config import create_custom_scene
    from src.repo_key import to_repo_key
    import uuid

    if body.scope == "official":
        raise HTTPException(status_code=403, detail={"code": 403, "message": "不可创建官方场景"})
    # 方案 B：user scope 已废弃，强制 repo
    if body.scope != "repo":
        raise HTTPException(status_code=400, detail={"code": 400, "message": "仅支持仓库级场景"})

    if repo:
        repo_key = _resolve_repo_key(repo)
    else:
        try:
            _repo = app_current_repo()
            if _repo:
                repo_key = _resolve_repo_key(f"{_repo.get('owner','')}/{_repo.get('name','')}")
        except Exception:
            repo_key = ""
    if not repo_key:
        raise HTTPException(status_code=400, detail={"code": 400, "message": "仓库级场景需要先选择仓库"})

    # 校验 engine_runtime_ids：scope=repo 的引擎必须属于当前仓库，且 tag 必须包含节点标签
    _validate_scene_engine_ids(nodes_json := [n.model_dump() for n in body.nodes], repo_key)

    # PR 订阅开启时校验：QA 节点必须启用且有 skill
    if body.pr_subscription_enabled:
        qa_node = next((n for n in body.nodes if n.tag == "qa"), None)
        if not qa_node or not qa_node.enabled or not qa_node.skill:
            raise HTTPException(status_code=400, detail={
                "code": 400, "message": "开启 PR 订阅需要先配置 QA Agent 并启用技能"})

    scene_id = body.scene_id or str(uuid.uuid4())

    try:
        db_id = create_custom_scene(
            scene_id=scene_id, name=body.name, description=body.description,
            repo_key=repo_key, nodes=nodes_json, created_by=user.get("username") or "",
            icon=body.icon or "", scene_type="standard",
            pr_subscription_enabled=1 if body.pr_subscription_enabled else 0,
            linked_repos=body.linked_repos or [])
    except Exception as e:
        err_upper = str(e).upper()
        if "UNIQUE" in err_upper or "DUPLICATE" in err_upper:
            raise HTTPException(status_code=409, detail={"code": 409, "message": f"场景 ID '{scene_id}' 已存在"})
        raise HTTPException(status_code=500, detail={"code": 500, "message": str(e)})

    log.info(f"Scene created: id={scene_id} repo={repo_key} by user={user.get('id')}")
    return {"scene_id": scene_id, "db_id": db_id, "message": "场景创建成功"}


@router.put("/{scene_id}", operation_id="scenes_update", summary="Update scene", description="Updates a scene (repo/user scope directly; official immutable).")
def update_scene(scene_id: str, body: UpdateSceneRequest, user: dict = Depends(require_no_session_token), repo: str = ""):
    """更新场景（repo/user scope 直接更新，official 不可修改）"""
    from src.api.pipeline_config import update_custom_scene, _query_scene_by_id
    from src.repo_key import to_repo_key

    repo_key = ""
    if repo:
        repo_key = _resolve_repo_key(repo)
    else:
        try:
            _repo = app_current_repo()
            if _repo:
                repo_key = _resolve_repo_key(f"{_repo.get('owner','')}/{_repo.get('name','')}")
        except Exception:
            pass

    existing = _query_scene_by_id(scene_id, repo_key=repo_key)
    if not existing:
        raise HTTPException(status_code=404, detail={"code": 404, "message": f"场景 '{scene_id}' 不存在"})

    nodes_json = [n.model_dump() for n in body.nodes]
    if repo_key:
        _validate_scene_engine_ids(nodes_json, repo_key)

    # PR 订阅开启时校验：QA 节点必须启用且有 skill
    _pr_sub_on = body.pr_subscription_enabled
    if _pr_sub_on is None:
        _pr_sub_on = bool(existing.get("pr_subscription_enabled", 0))
    if _pr_sub_on:
        # body.nodes 为空时从数据库已有场景查 QA 节点
        _check_nodes = body.nodes if body.nodes else existing.get("nodes", [])
        qa_node = next((n for n in _check_nodes if getattr(n, 'tag', n.get('tag') if isinstance(n, dict) else '') == "qa"), None)
        if qa_node:
            _qa_enabled = getattr(qa_node, 'enabled', True) if not isinstance(qa_node, dict) else qa_node.get("enabled", True)
            _qa_skill = getattr(qa_node, 'skill', '') if not isinstance(qa_node, dict) else qa_node.get("skill", "")
        else:
            _qa_enabled = False
            _qa_skill = ""
        if not _qa_enabled or not _qa_skill:
            raise HTTPException(status_code=400, detail={
                "code": 400, "message": "开启 PR 订阅需要先配置 QA Agent 并启用技能"})

    # body.nodes 为空时不覆盖已有 nodes（只更新 pr_subscription_enabled 等字段）
    _update_nodes = nodes_json if body.nodes else existing.get("nodes", [])
    update_custom_scene(
        scene_id=scene_id, repo_key=repo_key, nodes=_update_nodes,
        name=body.name, description=body.description, icon=body.icon,
        pr_subscription_enabled=1 if _pr_sub_on else 0 if _pr_sub_on is not None else None,
        linked_repos=body.linked_repos)

    log.info(f"Scene updated: id={scene_id} by user={user.get('id')}")
    return {"message": "场景更新成功"}


@router.delete("/{scene_id}", operation_id="scenes_delete", summary="Delete scene", description="Deletes a custom scene (soft delete; official undeletable).")
def delete_scene(scene_id: str, user: dict = Depends(require_no_session_token), repo: str = ""):
    """删除自定义场景（软删除，官方场景不可删除）"""
    from src.api.pipeline_config import delete_custom_scene, _query_scene_by_id
    from src.repo_key import to_repo_key

    repo_key = ""
    if repo:
        repo_key = _resolve_repo_key(repo)
    else:
        try:
            _repo = app_current_repo()
            if _repo:
                repo_key = _resolve_repo_key(f"{_repo.get('owner','')}/{_repo.get('name','')}")
        except Exception:
            pass

    existing = _query_scene_by_id(scene_id, repo_key=repo_key)
    if not existing:
        raise HTTPException(status_code=404, detail={"code": 404, "message": f"场景 '{scene_id}' 不存在"})

    delete_custom_scene(scene_id=scene_id, repo_key=repo_key)

    log.info(f"Scene deleted: id={scene_id} by user={user.get('id')}")
    return {"message": "场景已删除"}


@router.post("/apply/{owner}/{repo}", operation_id="scenes_apply", summary="Apply scene to repository", description="Applies a scene to a repository.")
def apply_scene(owner: str, repo: str, body: ApplySceneRequest,
                      user: dict = Depends(require_no_session_token)):
    """为仓库应用指定场景"""
    from src.api.pipeline_config import get_pipeline_config, _query_scene_by_id
    from src.repo_key import to_repo_key
    from src.session_manager import db, db_lock
    from src.user.db import get_bot_token_by_repo
    from src.api.session_store import upsert_agent_session

    repo_key = _resolve_repo_key(f"{owner}/{repo}")
    cfg = get_pipeline_config(body.scene_id, repo_key=repo_key)
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
                skill_id = resolve_skill_id(skill, repo_key=repo_key, user_id=user.get("id", ""))

                runtime_id = 0
                # 优先使用场景 node 配置的 engine_runtime_ids
                node_engine_ids = node.get("engine_runtime_ids") or []
                if node_engine_ids:
                    # 验证配置的引擎是否在线可用，选第一个可用的
                    for eid in node_engine_ids:
                        try:
                            _row = c.execute(
                                "SELECT status, healthy FROM agent_runtimes WHERE id=?",
                                (eid,)).fetchone()
                            if _row and _row[0] == "online" and _row[1] == 1:
                                runtime_id = eid
                                break
                        except Exception:
                            pass
                # 场景未配置引擎或配置的引擎都不可用 → fallback
                if not runtime_id:
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

    return {"message": f"场景 '{cfg.get('name', body.scene_id)}' 已应用", "updated": updated}


def app_current_repo():
    """获取当前选中仓库（从前端 header 上下文，这里用 placeholder）"""
    # FastAPI 无 session 级仓库选择；前端通过 query param 或 header 传
    # 这里返回 None，调用方会用 user 信息或其他方式获取 repo
    return None


# ===== 场景高级选项：工作分支 + 自定义 {name, value, is_secret} =====

def _merge_preset_custom_fields(scene_id: str, repo_key: str, preset_json: str = "[]") -> None:
    """将场景预置自定义参数声明合并到 devops_pipeline_scene.advanced_options（Issue #246）。

    - preset 声明来自 scene_market.preset_custom_fields（scene.json custom_fields 数组）
    - 仅在 custom_fields 中不存在同名项时追加（不覆盖用户已有配置）
    - preset 项标注 preset=true，preyset 声明持久化为 advanced_options.preset_custom_fields
    """
    import json as _j
    from src.session_manager import db, db_lock
    from src.scene_custom_env import normalize_custom_field

    try:
        preset_decls = _j.loads(preset_json) if preset_json else []
    except Exception:
        preset_decls = []
    if not preset_decls:
        return

    with db_lock:
        c = db.cursor()
        c.execute(
            "SELECT advanced_options FROM devops_pipeline_scene "
            "WHERE scene_id=? AND repo_key=? AND is_active=1 LIMIT 1",
            (scene_id, repo_key))
        row = c.fetchone()
    opts = _j.loads(row[0]) if (row and row[0]) else {}

    existing = {(f.get("name") or "").strip() for f in (opts.get("custom_fields", []) or [])}
    _normalized_presets = []
    appended = 0
    for _p in preset_decls:
        _pf = normalize_custom_field(_p)
        _pf["preset"] = True
        _pf["value"] = _pf.get("default_value", "")
        _normalized_presets.append(_pf)
        if not _pf.get("name") or _pf["name"] in existing:
            continue
        opts.setdefault("custom_fields", []).append(_pf)
        existing.add(_pf["name"])
        appended += 1
    # 声明持久化，供 preset 变更/重启用
    opts["preset_custom_fields"] = preset_decls

    from datetime import datetime, timezone
    now = datetime.now(timezone.utc).isoformat()
    with db_lock:
        c = db.cursor()
        c.execute(
            "UPDATE devops_pipeline_scene SET advanced_options=?, updated_at=? "
            "WHERE scene_id=? AND repo_key=? AND is_active=1",
            (_j.dumps(opts, ensure_ascii=False), now, scene_id, repo_key))
        db.commit()
    if appended:
        log.info(f"merge_preset_custom_fields: scene={scene_id} repo={repo_key} appended={appended}")


class AdvancedOptionsRequest(BaseModel):
    work_branch: str = "main"
    custom_fields: list = []   # [{name: str, value: str, is_secret: bool?, scope?: list}]
    linked_repos: list = []    # [{repo_key: str, branch: str}] — Issue #242


@router.get("/{scene_id}/advanced-options", operation_id="scenes_advanced_options_get", summary="Get scene advanced options", description="Reads scene advanced options (work branch + custom key:value).")
def get_advanced_options(scene_id: str, user: dict = Depends(require_no_session_token), repo: str = ""):
    """读取场景高级选项（工作分支 + 自定义 key:value）"""
    from src.repo_key import to_repo_key
    from src.session_manager import db, db_lock
    import json as _json

    repo_key = ""
    if repo:
        repo_key = _resolve_repo_key(repo)
    if not repo_key:
        raise HTTPException(status_code=400, detail={"code": 400, "message": "需要选择仓库"})

    try:
        with db_lock:
            c = db.cursor()
            c.execute(
                "SELECT advanced_options FROM devops_pipeline_scene "
                "WHERE scene_id=? AND repo_key=? AND is_active=1 LIMIT 1",
                (scene_id, repo_key))
            row = c.fetchone()
    except Exception:
        row = None

    if row and row[0]:
        try:
            opts = _json.loads(row[0])
        except Exception:
            opts = {}
        from src.scene_custom_env import normalize_custom_field
        _fields = []
        for f in (opts.get("custom_fields", []) or []):
            _nf = normalize_custom_field(f)
            if not _nf.get("name"):
                continue
            # preset 项：scope/is_secret/preset 锁定展示；value 正常返回（前端按 is_secret 掩码）
            _fields.append({
                "name": _nf["name"],
                "value": _nf.get("value", ""),
                "is_secret": _nf.get("is_secret", False),
                "scope": _nf.get("scope", ["all"]),
                "preset": _nf.get("preset", False),
                "description": _nf.get("description", ""),
            })
        return {
            "work_branch": opts.get("work_branch", "main") or "main",
            "custom_fields": _fields,
            "linked_repos": opts.get("linked_repos", []) or [],
        }
    # linked_repos 也可能存在独立列中（create_scene 直接写入）
    _linked_repos = []
    try:
        from src.api.pipeline_config import _query_scene_by_id
        _scene = _query_scene_by_id(scene_id, repo_key=repo_key)
        if _scene:
            _linked_repos = _scene.get("linked_repos") or []
    except Exception:
        pass
    return {"work_branch": "main", "custom_fields": [], "linked_repos": _linked_repos}


@router.put("/{scene_id}/advanced-options", operation_id="scenes_advanced_options_set", summary="Save scene advanced options", description="Saves scene advanced options (work branch + custom key:value).")
def save_advanced_options(scene_id: str, body: AdvancedOptionsRequest,
                          user: dict = Depends(require_no_session_token), repo: str = ""):
    """保存场景高级选项（工作分支 + 自定义 key:value）"""
    from src.repo_key import to_repo_key
    from src.session_manager import db, db_lock
    from src.api.pipeline_config import _query_scene_by_id
    import json as _json
    from datetime import datetime, timezone

    repo_key = ""
    if repo:
        repo_key = _resolve_repo_key(repo)
    if not repo_key:
        raise HTTPException(status_code=400, detail={"code": 400, "message": "需要选择仓库"})

    # 校验 work_branch 非空（强制字段）
    if not body.work_branch or not body.work_branch.strip():
        raise HTTPException(status_code=400, detail={"code": 400, "message": "工作分支不能为空"})

    # 校验场景存在
    existing = _query_scene_by_id(scene_id, repo_key=repo_key)
    if not existing:
        raise HTTPException(status_code=404, detail={"code": 404, "message": f"场景 '{scene_id}' 不存在"})

    # 校验 custom_fields name 非空/大写/保留冲突且不重复
    from src.scene_custom_env import custom_env_error, normalize_custom_field
    seen_names = set()
    for f in (body.custom_fields or []):
        _n = (f.get("name") or "").strip() if isinstance(f, dict) else ""
        _err = custom_env_error(_n)
        if _err:
            raise HTTPException(status_code=400, detail={"code": 400, "message": _err})
        if _n in seen_names:
            raise HTTPException(status_code=400, detail={"code": 400, "message": f"配置项 name '{_n}' 重复"})
        seen_names.add(_n)
        # scope 校验：数组内每项必须为合法枚举；全部非法 → 400（部分非法在 normalize 时剔除）
        _raw_scope = f.get("scope") if isinstance(f, dict) else None
        if _raw_scope:
            from src.scene_custom_env import _SCOPE_SET
            _items = _raw_scope if isinstance(_raw_scope, (list, tuple, set)) else [_raw_scope]
            _valid_items = {(str(x) or "").strip().lower() for x in _items if x not in (None, "")}
            if _valid_items and not (_valid_items & _SCOPE_SET):
                raise HTTPException(
                    status_code=400,
                    detail={"code": 400, "message": f"配置项 '{_n}' 的 scope 非法，可选值: all/vod/issue/coding/qa/sre"})

    # 校验 linked_repos 格式：[{repo_key, branch}]，上限 5 个
    _linked_repos = []
    for lr in (getattr(body, 'linked_repos', None) or []):
        if not isinstance(lr, dict):
            continue
        _rk = (lr.get("repo_key") or "").strip()
        _br = (lr.get("branch") or "").strip()
        if _rk:
            _linked_repos.append({"repo_key": _rk, "branch": _br or "main"})
    if len(_linked_repos) > 5:
        raise HTTPException(status_code=400, detail={"code": 400, "message": "关联仓库数量不能超过 5 个"})

    _custom_fields = [normalize_custom_field(f) for f in (body.custom_fields or []) if isinstance(f, dict)]
    _custom_fields = [f for f in _custom_fields if f.get("name")]
    # 读库内现有 advanced_options（preset 声明 + 既有值），预置参数保护
    from src.scene_custom_env import normalize_scope as _normalize_scope
    _old_ao = {}
    _old_cf_map = {}      # name -> 完整旧项
    _preset_decl_map = {} # name -> preset 声明（preset_custom_fields 来源）
    try:
        with db_lock:
            _c2 = db.cursor()
            _c2.execute(
                "SELECT advanced_options FROM devops_pipeline_scene "
                "WHERE scene_id=? AND repo_key=? AND is_active=1 LIMIT 1",
                (scene_id, repo_key))
            _row2 = _c2.fetchone()
        if _row2 and _row2[0]:
            _old_ao = _json.loads(_row2[0]) or {}
        for _f in (_old_ao.get("custom_fields", []) or []):
            _nf = normalize_custom_field(_f)
            if _nf.get("name"):
                _old_cf_map[_nf["name"]] = _nf
        # preset 声明：优先库内 preset_custom_fields，其次 custom_fields 里的 preset:true 项
        for _pf in (_old_ao.get("preset_custom_fields", []) or []):
            _pf = normalize_custom_field(_pf) if isinstance(_pf, dict) else {}
            if _pf.get("name"):
                _pf["preset"] = True
                _preset_decl_map[_pf["name"]] = _pf
        for _f in (_old_ao.get("custom_fields", []) or []):
            _nf = normalize_custom_field(_f)
            if _nf.get("preset") and _nf.get("name") and _nf["name"] not in _preset_decl_map:
                _nf["preset"] = True
                _preset_decl_map[_nf["name"]] = _nf
    except Exception:
        pass

    # 前端掩码回显（value=********）→ keep_value 标记：value 视为未修改，保留旧值（不受 is_secret 限制）
    if _custom_fields:
        try:
            with db_lock:
                _c = db.cursor()
                _c.execute(
                    "SELECT advanced_options FROM devops_pipeline_scene "
                    "WHERE scene_id=? AND repo_key=? AND is_active=1 LIMIT 1",
                    (scene_id, repo_key))
                _row = _c.fetchone()
            _old = _json.loads(_row[0]) if (_row and _row[0]) else {}
            _old_map = {f.get("name"): f.get("value", "") for f in (_old.get("custom_fields", []) or []) if f.get("name")}
            _keep_map = {
                (f.get("name") or "").strip()
                for f in (body.custom_fields or []) if isinstance(f, dict) and f.get("keep_value")
            }
            for _f in _custom_fields:
                # keep_value（掩码未改）或 secret 空值（清空无效）→ 保留旧值
                if ((_f["name"] in _keep_map)
                        or (_f.get("is_secret") and not _f.get("value"))) \
                        and _f["name"] in _old_map:
                    _f["value"] = _old_map[_f["name"]]
        except Exception:
            pass

    # ── preset 保护：合并声明、锁定字段、禁止丢失 ──
    # 1) 库内 preset 声明全部保留（前端未提交也不能删）
    for _pname, _pdecl in _preset_decl_map.items():
        if _pname not in {f.get("name") for f in _custom_fields}:
            _existing = _old_cf_map.get(_pname)
            _custom_fields.append({
                "name": _pname,
                "value": (_existing.get("value", "") if _existing else _pdecl.get("default_value", "")),
                "is_secret": _pdecl.get("is_secret", False),
                "scope": _pdecl.get("scope", ["all"]) or ["all"],
                "preset": True,
                "default_value": _pdecl.get("default_value", ""),
                "description": _pdecl.get("description", ""),
            })
    # 2) preset 项字段锁定：name/scope/is_secret/preset 以声明为准，仅 value 取前端提交（或旧值）
    for _f in _custom_fields:
        _pdecl = _preset_decl_map.get(_f.get("name"))
        if not _pdecl:
            continue
        _old_v = _old_cf_map.get(_f["name"], {}).get("value", "")
        _f["preset"] = True
        _f["name"] = _pdecl["name"]
        _f["scope"] = _pdecl.get("scope", ["all"]) or ["all"]
        if "is_secret" in _pdecl:
            _f["is_secret"] = _pdecl.get("is_secret", False)
        _f["description"] = _pdecl.get("description", "")
        if not _f.get("value") or _f.get("value") in ("********", "#####"):
            _f["value"] = _old_v or _pdecl.get("default_value", "")
    # 3) 非 preset 项显式标注 preset=false
    for _f in _custom_fields:
        if _f.get("name") not in _preset_decl_map:
            _f["preset"] = False

    opts = {
        "work_branch": body.work_branch.strip(),
        "custom_fields": _custom_fields,
        "linked_repos": _linked_repos,
    }
    opts_json = _json.dumps(opts, ensure_ascii=False)
    now = datetime.now(timezone.utc).isoformat()

    try:
        with db_lock:
            c = db.cursor()
            c.execute(
                "UPDATE devops_pipeline_scene SET advanced_options=?, updated_at=? "
                "WHERE scene_id=? AND repo_key=? AND is_active=1",
                (opts_json, now, scene_id, repo_key))
            if c.rowcount == 0:
                raise HTTPException(status_code=404, detail={"code": 404, "message": f"场景 '{scene_id}' 不存在或未激活"})
            # Issue #242: 同步写入独立 linked_repos 列（便于查询时直接读取）
            try:
                c.execute(
                    "UPDATE devops_pipeline_scene SET linked_repos=? WHERE scene_id=? AND repo_key=? AND is_active=1",
                    (_json.dumps(_linked_repos, ensure_ascii=False), scene_id, repo_key))
            except Exception:
                pass  # 列不存在时忽略（兼容旧库）
            db.commit()
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail={"code": 500, "message": str(e)})

    log.info(f"Advanced options saved: scene={scene_id} repo={repo_key} work_branch={body.work_branch} linked_repos={len(_linked_repos)}")
    return {"message": "高级选项已保存"}
