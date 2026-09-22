"""Skills 路由：列表、上传、删除、个人技能转仓库技能"""
import io as _io
import os, tempfile, zipfile, json, shutil, stat as _stat
from fastapi import APIRouter, HTTPException, Depends, UploadFile, File, Form
from pydantic import BaseModel
from src.user import db as user_db
from src.user.validator import validate_skill as validate_custom_skill, parse_skill_frontmatter
from src.api.deps import get_current_user_or_api_key, require_no_session_token

router = APIRouter(prefix="/api/skills", tags=["skills"])


# 解压后总大小上限（zip 炸弹防护，见 obs-skill-storage-design.md §7）
MAX_EXTRACT_BYTES = int(os.environ.get("SKILL_MAX_EXTRACT_BYTES", str(100 * 1024 * 1024)))


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


def _normalize_skill_zip(skill_dir: str, skill_name: str) -> bytes:
    """规范化重打包技能 zip：根 = {skill_name}/，逐文件保留权限位（external_attr），
    丢弃系统目录。返回 zip 字节（OBS 存储正体，见 obs-skill-storage-design.md §2.2）。"""
    buf = _io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for root, dirs, files in os.walk(skill_dir):
            dirs[:] = [d for d in dirs if d not in ("__MACOSX", ".git", "__pycache__")]
            for name in files:
                full = os.path.join(root, name)
                rel = os.path.relpath(full, skill_dir).replace(os.sep, "/")
                arc = f"{skill_name}/{rel}"
                st = os.stat(full)
                info = zipfile.ZipInfo(arc)
                info.external_attr = (_stat.S_IMODE(st.st_mode)) << 16  # 保留权限位
                with open(full, "rb") as fh:
                    zf.writestr(info, fh.read())
    return buf.getvalue()


@router.get("", operation_id="skills_list", summary="List skills", description="Lists skills: official visible to all, user visible to owner, repo visible to members. Supports scope/agent_type/sub_scene filtering, sort and paging.")
def list_skills(user: dict = Depends(get_current_user_or_api_key), repo: str = "", scope: str = "",
                agent_type: str = "", category_id: int = 0,
                q: str = "", sort: str = "", page: int = 1, page_size: int = 0):
    """返回技能列表：official 所有用户可见；user 本人可见；repo 指定仓库可见。
    scope 参数：official/repo/user 按维度过滤；不传 = 返回全部（兼容旧逻辑）。
    agent_type 参数：按 agent_type 过滤（vod/issue/coding/qa/sre/chat）。
    category_id 参数：按分类 ID 过滤（Issue #179）。
    q 参数：搜索词，按 skill_name / display_name / description 模糊匹配。
    sort 参数：latest（默认顺序）/ hot（按 like_count 降序）。
    page/page_size：分页参数，page_size=0 时不分页（返回全部，兼容旧逻辑）。
    扩展返回字段：is_featured, like_count, liked_by_me, scene_count, repo_count, scenes, category_id, category_name
    """
    from src.session_manager import db, db_lock
    from src.repo_key import to_repo_key
    from src.user.db import (batch_skill_liked, get_skill_usage_stats,
                             batch_skill_scenes, list_skill_categories)

    # repo 参数可能是 fullName（xiaowu2119/hello-skill），转成 repo_key（gitcode:xiaowu2119/hello-skill）
    repo_key = _resolve_repo_key(repo) if repo else ""

    # 构建查询条件
    conditions = ["is_active=1"]
    params = []
    if scope == "official":
        conditions.append("scope='official'")
    elif scope == "repo":
        if not repo:
            return {"skills": [], "total": 0, "page": page, "page_size": page_size}
        conditions.append("scope='repo'")
        conditions.append("owner_id=?")
        params.append(repo_key)
    elif scope == "user":
        conditions.append("scope='user'")
        conditions.append("owner_id=?")
        params.append(str(user["id"]))
    elif repo:
        # 官方 + 个人 + 指定仓库技能（兼容旧逻辑）
        conditions.append("(scope='official' OR (scope='user' AND owner_id=?) OR (scope='repo' AND owner_id=?))")
        params.extend([str(user["id"]), repo_key])
    else:
        # 官方 + 个人（兼容旧逻辑）
        conditions.append("(scope='official' OR (scope='user' AND owner_id=?))")
        params.append(str(user["id"]))

    # 搜索过滤
    q = (q or "").strip()
    if q:
        conditions.append("(skill_name LIKE ? OR display_name LIKE ? OR description LIKE ?)")
        kw = f"%{q}%"
        params.extend([kw, kw, kw])

    # agent_type 过滤
    agent_type = (agent_type or "").strip()
    if agent_type:
        conditions.append("agent_type=?")
        params.append(agent_type)

    # category_id 过滤
    if category_id and category_id > 0:
        conditions.append("category_id=?")
        params.append(category_id)

    # 排序
    order_by = "ORDER BY updated_at DESC" if sort == "latest" else "ORDER BY like_count DESC" if sort == "hot" else "ORDER BY agent_type, skill_name"

    where_sql = " WHERE " + " AND ".join(conditions)

    # 分页
    use_paging = page_size and page_size > 0
    total = 0

    skills = []
    skill_ids: list[int] = []
    # 批量查分类名称（避免 N+1）— 在 db_lock 外查，避免 SQLite cursor 复用冲突
    cat_map = {}
    try:
        for cat in list_skill_categories():
            cat_map[cat["id"]] = cat["name"]
    except Exception:
        pass
    with db_lock:
        c = db.cursor()
        try:
            if use_paging:
                offset = (page - 1) * page_size
                c.execute(f"SELECT COUNT(*) FROM skill{where_sql}", tuple(params))
                total = c.fetchone()[0]
                c.execute(
                    f"SELECT id, skill_name, display_name, description, agent_type, scope, owner_id, is_featured, like_count, sub_scene, category_id "
                    f"FROM skill{where_sql} {order_by} LIMIT {int(page_size)} OFFSET {int(offset)}",
                    tuple(params)
                )
            else:
                c.execute(
                    f"SELECT id, skill_name, display_name, description, agent_type, scope, owner_id, is_featured, like_count, sub_scene, category_id "
                    f"FROM skill{where_sql} {order_by}",
                    tuple(params)
                )
            for r in c.fetchall():
                cat_id = r[10] or 0
                skills.append({
                    "id": r[0],
                    "name": r[1],
                    "display_name": r[2] or r[1],
                    "description": r[3] or "",
                    "agent_type": r[4] or "",
                    "type": "official" if r[5] == "official" else "repo" if r[5] == "repo" else "user-uploaded",
                    "scope": r[5] or "",
                    "is_featured": r[7] or 0,
                    "like_count": r[8] or 0,
                    "category_id": cat_id,
                    "category_name": cat_map.get(cat_id, ""),
                })
                skill_ids.append(r[0])
        except Exception:
            pass

    # 批量查 liked_by_me
    liked_map = batch_skill_liked(user.get("id", 0), skill_ids) if skill_ids else {}
    # 批量查使用统计（按本页 skill_ids 过滤）
    usage_stats = get_skill_usage_stats(skill_ids) if skill_ids else {}
    # 批量查所属场景
    scenes_map = batch_skill_scenes(skill_ids) if skill_ids else {}

    # 补充扩展字段
    for s in skills:
        s["liked_by_me"] = liked_map.get(s["id"], False)
        stats = usage_stats.get(s["id"], {})
        s["scene_count"] = stats.get("scene_count", 0)
        s["repo_count"] = stats.get("repo_count", 0)
        s["scenes"] = scenes_map.get(s["id"], [])

    if use_paging:
        return {"skills": skills, "total": total, "page": page, "page_size": page_size}
    return {"skills": skills}


# ===== Skill Market API（技能市场）=====

@router.post("/{skill_id}/like", operation_id="skills_like_toggle", summary="Toggle skill like", description="Toggles a skill like.")
def toggle_skill_like(skill_id: int, user: dict = Depends(require_no_session_token)):
    """点赞 toggle：已赞→取消，未赞→点赞"""
    from src.session_manager import db, db_lock
    from src.user.db import toggle_skill_like as _toggle

    # 校验技能存在
    with db_lock:
        c = db.cursor()
        c.execute("SELECT 1 FROM skill WHERE id=? AND is_active=1", (skill_id,))
        if not c.fetchone():
            raise HTTPException(status_code=404, detail={"code": 404, "message": "技能未找到"})

    liked, like_count = _toggle(user.get("id", 0), skill_id)
    return {"liked": liked, "like_count": like_count}


@router.get("/featured", operation_id="skills_featured", summary="List featured skills", description="Featured skills: is_featured=1 official skills ordered by like_count desc (default limit 5).")
def list_featured_skills(user: dict = Depends(require_no_session_token), limit: int = 0):
    """精选技能：is_featured=1 的 official 技能，按 like_count 降序。limit=0 时默认 5"""
    from src.user.db import (get_featured_skills, batch_skill_liked,
                             get_skill_usage_stats, batch_skill_scenes,
                             list_skill_categories)

    skills = get_featured_skills(limit=limit if limit > 0 else 5)
    if not skills:
        return {"skills": []}

    skill_ids = [s["id"] for s in skills]
    # 补充 liked_by_me
    liked_map = batch_skill_liked(user.get("id", 0), skill_ids)
    # 补充使用统计和所属场景（按 skill_id 关联）
    usage_stats = get_skill_usage_stats(skill_ids) if skill_ids else {}
    scenes_map = batch_skill_scenes(skill_ids) if skill_ids else {}
    # 批量查分类名称
    cat_map = {}
    try:
        for cat in list_skill_categories():
            cat_map[cat["id"]] = cat["name"]
    except Exception:
        pass
    for s in skills:
        s["name"] = s["skill_name"]  # 前端用 name 字段
        s["scope"] = "official"
        s["liked_by_me"] = liked_map.get(s["id"], False)
        stats = usage_stats.get(s["id"], {})
        s["scene_count"] = stats.get("scene_count", 0)
        s["repo_count"] = stats.get("repo_count", 0)
        s["scenes"] = scenes_map.get(s["id"], [])
        s["category_name"] = cat_map.get(s.get("category_id", 0), "")
    return {"skills": skills}


@router.get("/agent-stats", operation_id="skills_agent_stats", summary="Skill stats (total + by agent_type)", description="Skill total + per agent_type counts; scope/repo filters match list_skills. User-only (web UI stats).")
def get_agent_stats(user: dict = Depends(require_no_session_token), scope: str = "", repo: str = ""):
    """返回技能总数 + 每个 agent_type 的技能数量。

    scope/repo 参数与 list_skills 一致，用于按维度统计。
    分类列表由 /api/skills/categories 独立获取，不在本接口返回。

    user_only：仅 Web UI / 用户级调用方消费（skills.js 页面统计），Agent 执行期无此场景；
    且 tk_ 会话 token 的 user_id 在仓库身份下为 0，scope=user 过滤 owner_id=0 亦无可达数据——故禁 session。
    """
    from src.session_manager import db, db_lock

    repo_key = _resolve_repo_key(repo) if repo else ""
    conditions = ["is_active=1"]
    params = []
    if scope == "official":
        conditions.append("scope='official'")
    elif scope == "repo":
        if not repo:
            return {"total": 0, "agents": []}
        conditions.append("scope='repo'")
        conditions.append("owner_id=?")
        params.append(repo_key)
    elif scope == "user":
        conditions.append("scope='user'")
        conditions.append("owner_id=?")
        params.append(str(user["id"]))
    elif repo:
        conditions.append("(scope='official' OR (scope='user' AND owner_id=?) OR (scope='repo' AND owner_id=?))")
        params.extend([str(user["id"]), repo_key])
    else:
        conditions.append("(scope='official' OR (scope='user' AND owner_id=?))")
        params.append(str(user["id"]))

    where_sql = " WHERE " + " AND ".join(conditions)
    agents = []
    total = 0
    with db_lock:
        c = db.cursor()
        try:
            c.execute(
                f"SELECT agent_type, COUNT(*) as cnt FROM skill{where_sql} GROUP BY agent_type ORDER BY "
                f"CASE agent_type WHEN 'vod' THEN 1 WHEN 'issue' THEN 2 WHEN 'coding' THEN 3 "
                f"WHEN 'qa' THEN 4 WHEN 'sre' THEN 5 WHEN 'chat' THEN 6 ELSE 99 END",
                tuple(params)
            )
            for r in c.fetchall():
                agents.append({"agent_type": r[0] or "", "count": r[1]})
                total += r[1]
        except Exception:
            pass
    return {"total": total, "agents": agents}


@router.get("/stats", operation_id="skills_stats", summary="Skill usage stats (scene/repo aggregation)", description="Skill usage stats aggregated from scene_skill_ref: {skill_name: {scene_count, repo_count}}.")
def get_skill_stats(user: dict = Depends(require_no_session_token)):
    """技能使用统计（通过 scene_skill_ref 关联表 SQL 聚合）。

    返回 {skill_name: {scene_count: N, repo_count: N}}。
    """
    from src.user.db import get_skill_usage_stats
    return {"stats": get_skill_usage_stats()}


@router.get("/categories", operation_id="skills_categories", summary="List skill categories",
            description="Lists skill categories (Issue #179 skill-sync-category).")
def list_categories(user: dict = Depends(require_no_session_token)):
    """返回技能分类列表（Issue #179）。"""
    from src.user.db import list_skill_categories
    return {"categories": list_skill_categories()}


@router.get("/search", operation_id="skills_search", summary="Search skills",
            description="Skill lookup for a task description: exact skill_name match first, then keyword match on description/display_name/agent_type.")
def search_skills(q: str = "", user: dict = Depends(require_no_session_token)):
    """技能查找：根据任务描述推荐相关技能。

    先按 skill_name 精确匹配，匹配到则按名称过滤；
    匹配不到则按 description + display_name + agent_type 做关键词匹配。
    不调 LLM（第一期简单实现）——分词后匹配技能描述。
    返回 [{skill_id, name, display_name, description, category_id, category_name, match_reason}]
    """
    from src.session_manager import db, db_lock
    from src.user.db import list_skill_categories

    q = (q or "").strip()
    if not q:
        return {"skills": []}

    # 批量查分类名称（在 db_lock 外查，避免 SQLite cursor 复用）
    cat_map = {}
    try:
        for cat in list_skill_categories():
            cat_map[cat["id"]] = cat["name"]
    except Exception:
        pass

    # 1. 先按 skill_name 精确匹配
    with db_lock:
        c = db.cursor()
        c.execute(
            "SELECT id, skill_name, display_name, description, agent_type, category_id "
            "FROM skill WHERE is_active=1 AND scope='official' "
            "AND (skill_name=? OR display_name=?) ORDER BY skill_name",
            (q, q),
        )
        exact_rows = c.fetchall()

    if exact_rows:
        return {"skills": [
            {"skill_id": r[0], "name": r[1], "display_name": r[2] or r[1],
             "description": r[3] or "", "category_id": r[5] or 0,
             "category_name": cat_map.get(r[5] or 0, ""), "match_reason": "名称精确匹配"}
            for r in exact_rows
        ]}

    # 2. 关键词匹配：分词后匹配 description + display_name + agent_type
    # 简单分词：按空格/中文常见分隔符拆分，过滤太短的词
    import re as _re
    keywords = [w for w in _re.split(r'[\s,，、/;；]+', q) if len(w) >= 2]
    if not keywords:
        # 整个 q 作为单个关键词
        keywords = [q]

    with db_lock:
        c = db.cursor()
        c.execute(
            "SELECT id, skill_name, display_name, description, agent_type, category_id "
            "FROM skill WHERE is_active=1 AND scope='official' "
            "ORDER BY agent_type, skill_name",
        )
        all_skills = c.fetchall()

    results = []
    for r in all_skills:
        skill_id, name, display_name, desc, agent_type, cat_id = r
        display_name = display_name or name
        desc = desc or ""
        # 计算命中关键词
        hit_keywords = []
        searchable = f"{name} {display_name} {desc} {agent_type}".lower()
        for kw in keywords:
            if kw.lower() in searchable:
                hit_keywords.append(kw)
        if hit_keywords:
            match_reason = f"匹配关键词：{', '.join(hit_keywords)}"
            results.append({
                "skill_id": skill_id, "name": name, "display_name": display_name,
                "description": desc, "category_id": cat_id or 0,
                "category_name": cat_map.get(cat_id or 0, ""),
                "match_reason": match_reason,
                "_hit_count": len(hit_keywords),
            })

    # 按命中数降序
    results.sort(key=lambda x: x["_hit_count"], reverse=True)
    for r in results:
        del r["_hit_count"]

    return {"skills": results}


@router.get("/template")
def download_skill_template():
    """下载技能模板 ZIP（skills/_template 目录打包）。免鉴权（token 通过 query 传递）。"""
    from src.config import SKILLS_DIR

    template_dir = os.path.join(SKILLS_DIR, "_template")
    if not os.path.isdir(template_dir):
        raise HTTPException(status_code=404, detail={"code": 404, "message": "技能模板未找到"})

    import io as _io
    buf = _io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for root, dirs, files in os.walk(template_dir):
            dirs[:] = [d for d in dirs if d != "__pycache__"]
            for f in files:
                full = os.path.join(root, f)
                rel = os.path.relpath(full, template_dir)
                zf.write(full, rel)
    buf.seek(0)
    # ===== OBS 分支（N8）：打包 → 传 OBS → 返回临时签名 URL（前端点击下载时动态获取）=====
    from src.storage import get_storage
    storage = get_storage()
    if storage is not None:
        key = "skills/template/skill-template.zip"
        storage.save_bytes(key, buf.getvalue())
        url = storage.temporary_url(key)
        return {"url": url, "expires_in": storage.default_expires}
    # ===== local 分支（原逻辑不变）=====
    from fastapi.responses import Response
    return Response(
        content=buf.getvalue(),
        media_type="application/zip",
        headers={"Content-Disposition": 'attachment; filename="skill-template.zip"'},
    )


@router.get("/by-id/{skill_id}/download", operation_id="skills_download", summary="Download skill ZIP", description="Downloads a skill package zip with a short-lived signed URL; OBS mode returns {url, expires_in}.")
def download_skill_zip_by_id(skill_id: int, user: dict = Depends(require_no_session_token),
                                   repo: str = ""):
    """下载技能包 zip（点击时动态签发，见 obs-skill-storage-design.md N8 下载模式）。

    按技能 id 精确查询（同名多 scope 记录无歧义）；OBS 模式返回 {url, expires_in}
    临时签名 URL（300s，不落库），浏览器直连 OBS 下载；local 模式返回 zip 文件流。
    官方技能（scope=official）为部署内置物，未转集群不提供下载。
    """
    from src.session_manager import db, db_lock
    from src.repo_key import to_repo_key, from_repo_key
    repo_key = _resolve_repo_key(repo) if repo else ""
    with db_lock:
        c = db.cursor()
        if repo_key:
            c.execute(
                "SELECT id, skill_path, scope, owner_id, skill_name FROM skill WHERE id=? AND is_active=1 AND (scope='official' OR (scope='user' AND owner_id=?) OR (scope='repo' AND owner_id=?))",
                (skill_id, str(user["id"]), repo_key)
            )
        else:
            c.execute(
                "SELECT id, skill_path, scope, owner_id, skill_name FROM skill WHERE id=? AND is_active=1 AND (scope='official' OR (scope='user' AND owner_id=?) OR scope='repo')",
                (skill_id, str(user["id"]))
            )
        row = c.fetchone()
    if not row:
        raise HTTPException(status_code=404, detail={"code": 404, "message": "技能不存在"})
    skill_id, skill_path, scope, owner_id, name = row

    from src.storage import get_storage
    storage = get_storage()
    if storage is not None:
        from src.user.db import get_skill_object_key
        object_key = get_skill_object_key(skill_id)
        if object_key:
            url = storage.temporary_url(object_key)
            return {"url": url, "expires_in": getattr(storage, "default_expires", 300)}
    # 官方技能未转集群模式：无 OBS 对象，不提供下载
    if scope == "official":
        raise HTTPException(status_code=400, detail={"code": 400, "message": "官方技能为部署内置，未转集群模式不提供下载"})

    # local 分支已移除：均衡负载环境下 local 存储会导致文件只存在单机
    raise HTTPException(status_code=503, detail={"code": 503, "message": "OBS 存储未启用，技能下载失败。请检查服务端 OBS 配置后重试。"})


@router.get("/by-id/{skill_id}", operation_id="skills_detail", summary="Skill detail (incl. SKILL.md)", description="Skill detail including SKILL.md. Access: official public, user=owner, repo=managed by caller.")
def get_skill_detail_by_id(skill_id: int, user: dict = Depends(get_current_user_or_api_key),
                                 repo: str = ""):
    """返回技能详情（含 SKILL.md 内容）——按技能 id 精确查询，查到后校验归属权限。

    权限校验：official 可见；user 需本人；repo 需当前用户托管该仓库。
    """
    from src.session_manager import db, db_lock
    from src.config import SKILLS_DIR

    with db_lock:
        c = db.cursor()
        c.execute(
            "SELECT skill_name, display_name, description, agent_type, skill_path, scope, owner_id, id, is_featured, like_count, sub_scene, category_id FROM skill WHERE id=? AND is_active=1",
            (skill_id,)
        )
        row = c.fetchone()

    if not row:
        raise HTTPException(status_code=404, detail={"code": 404, "message": "技能未找到"})

    scope = row[5]
    owner_id = row[6] or ""
    is_featured = row[8] or 0
    like_count = row[9] or 0
    category_id = row[11] or 0

    # 查分类名称
    from src.user.db import get_skill_category
    cat = get_skill_category(category_id) if category_id else None
    category_name = cat["name"] if cat else ""

    # 归属校验
    if scope == "user" and owner_id != str(user["id"]):
        raise HTTPException(status_code=403, detail={"code": 403, "message": "无权访问该技能"})
    if scope == "repo":
        # 从后端查当前用户托管的仓库列表，校验 skill.owner_id 在列表里
        from src.user.db import get_user_repos
        user_repo_keys = {r["repo_key"] for r in (get_user_repos(user["id"]) if user.get("id") else [])}
        if owner_id not in user_repo_keys:
            raise HTTPException(status_code=403, detail={"code": 403, "message": "无权访问该技能"})

    # 扩展字段：liked_by_me + 所属场景
    from src.user.db import is_skill_liked, get_skill_scenes
    liked_by_me = is_skill_liked(user.get("id", 0), skill_id)
    scenes = get_skill_scenes(row[7])  # row[7] = skill.id

    skill_dir = row[4] or row[0]
    current_id = row[7]
    # ===== OBS 分支（N5）：两级缓存 + 内存解压提取 + zip_url 临时地址 =====
    from src.storage import get_storage
    storage = get_storage()
    object_key = ""
    if storage is not None:
        from src.user.db import get_skill_object_key
        object_key = get_skill_object_key(current_id)  # 官方技能转集群后 skill_path 也是对象键
    if object_key:
        from src.storage import skill_md_content
        content = skill_md_content(object_key, row[0]) or ""
        zip_url = ""
        try:
            zip_url = storage.temporary_url(object_key)  # 用户点击下载时动态签发
        except Exception:
            zip_url = ""
        return {
            "id": current_id,
            "name": row[0], "display_name": row[1] or row[0], "description": row[2] or "",
            "agent_type": row[3] or "",
            "type": "official" if scope == "official" else "repo" if scope == "repo" else "user-uploaded",
            "scope": scope or "",
            "content": content,
            "zip_url": zip_url,
            "is_obs": True,  # OBS/集群模式（skill_path 为对象键）
            "is_featured": is_featured,
            "like_count": like_count,
            "liked_by_me": liked_by_me,
            "scenes": scenes,
            "category_id": category_id,
            "category_name": category_name,
        }
    # ===== local 分支 =====
    # user/repo 技能：均衡负载环境下 local 存储会导致文件只存在单机，OBS 不可用时直接报 503
    if scope in ("user", "repo"):
        raise HTTPException(status_code=503, detail={"code": 503, "message": "OBS 存储未启用，技能详情获取失败。请检查服务端 OBS 配置后重试。"})
    # official 技能：目录在代码仓库中，两台机器都有，保留磁盘读取
    from src.config import resolve_official_skill_path
    _r = resolve_official_skill_path(row[0])
    if _r:
        skill_path = _r
    else:
        # fallback: skill_dir 可能是 3 层路径，尝试去掉中间层
        _parts = skill_dir.split("/")
        if len(_parts) >= 3:
            skill_path = os.path.join(SKILLS_DIR, _parts[0], _parts[-1], "SKILL.md")
        else:
            skill_path = os.path.join(SKILLS_DIR, skill_dir, "SKILL.md")
    content = ""
    if os.path.exists(skill_path):
        with open(skill_path, encoding="utf-8") as f:
            content = f.read()
    return {
        "id": current_id,
        "name": row[0], "display_name": row[1] or row[0], "description": row[2] or "",
        "agent_type": row[3] or "",
        "type": "official" if scope == "official" else "repo" if scope == "repo" else "user-uploaded",
        "scope": scope or "",
        "content": content,
        "is_obs": False,
        "is_featured": is_featured,
        "like_count": like_count,
        "liked_by_me": liked_by_me,
        "scenes": scenes,
        "category_id": category_id,
        "category_name": category_name,
    }


@router.post("/upload", operation_id="skills_upload", summary="Upload skill (user personal / repo)", description="Uploads a skill ZIP. scope=user stores a personal skill; scope=repo requires repo. agent_type is required.")
async def upload_skill(file: UploadFile = File(...), user: dict = Depends(get_current_user_or_api_key),
                       scope: str = Form("user"), repo: str = Form(""), agent_type: str = Form(""),
                       category_id: int = Form(0)):
    """上传技能 ZIP。scope=user 存个人技能；scope=repo 存仓库技能（需指定 repo）。agent_type 必填（技能类型）。category_id 可选（分类ID，Issue #179）。"""
    VALID_AGENT_TYPES = {"vod", "issue", "coding", "qa", "sre", "chat"}
    if scope not in ("user", "repo"):
        raise HTTPException(status_code=400, detail={"code": 400, "message": "scope 必须为 user 或 repo"})
    if scope == "repo" and not repo:
        raise HTTPException(status_code=400, detail={"code": 400, "message": "仓库技能必须指定 repo"})
    if not agent_type:
        raise HTTPException(status_code=400, detail={"code": 400, "message": "必须选择技能类型（agent_type）"})
    if agent_type not in VALID_AGENT_TYPES:
        raise HTTPException(status_code=400, detail={"code": 400, "message": f"技能类型不合法，可选: {', '.join(sorted(VALID_AGENT_TYPES))}"})

    # 校验 category_id 存在性（0=未分类，>0 时必须在 skill_category 表中存在）
    if category_id > 0:
        from src.user.db import get_skill_category
        if not get_skill_category(category_id):
            raise HTTPException(status_code=400, detail={"code": 400, "message": f"分类 ID {category_id} 不存在"})

    content = await file.read()
    if len(content) > 10 * 1024 * 1024:
        raise HTTPException(status_code=400, detail={"code": 400, "message": "Skill 文件不能超过 10MB"})

    tmp_dir = tempfile.mkdtemp()
    try:
        zip_path = os.path.join(tmp_dir, file.filename or "skill.zip")
        with open(zip_path, "wb") as f:
            f.write(content)

        extract_dir = os.path.join(tmp_dir, "extracted")
        try:
            with zipfile.ZipFile(zip_path, "r") as zf:
                # zip 炸弹防护：解压后总大小上限（见设计文档 §7）
                total = sum((i.file_size or 0) for i in zf.infolist())
                if total > MAX_EXTRACT_BYTES:
                    raise HTTPException(status_code=400, detail={"code": 400, "message": f"ZIP 解压后总大小超过 {MAX_EXTRACT_BYTES // (1024 * 1024)}MB 上限"})
                zf.extractall(extract_dir)
                # Python 3.12+ extractall 不再恢复 Unix 权限位 → 手动恢复
                # （否则后续规范化重打包读 st_mode 拿不到上传时的权限，见 obs-skill-storage-design.md §7）
                for info in zf.infolist():
                    mode = info.external_attr >> 16
                    if mode and not info.is_dir():
                        p = os.path.join(extract_dir, info.filename)
                        if os.path.isfile(p):
                            os.chmod(p, mode & 0o777)
        except HTTPException:
            raise
        except zipfile.BadZipFile:
            raise HTTPException(status_code=400, detail={"code": 400, "message": "不是有效的 ZIP 文件，请上传 .zip 格式"})

        entries = os.listdir(extract_dir)
        if not entries:
            raise HTTPException(status_code=400, detail={"code": 400, "message": "ZIP 文件为空"})

        # 过滤系统杂项目录（macOS __MACOSX、.DS_Store 等），取第一个真实目录
        real_entries = [e for e in entries if e not in ("__MACOSX", ".DS_Store", ".git") and not e.startswith(".")]
        if not real_entries:
            raise HTTPException(status_code=400, detail={"code": 400, "message": "ZIP 中未找到技能目录"})

        skill_dir = os.path.join(extract_dir, real_entries[0])
        if not os.path.isdir(skill_dir):
            skill_dir = extract_dir

        skill_name = os.path.basename(skill_dir) if os.path.isdir(skill_dir) else real_entries[0]
        errors = validate_custom_skill(skill_dir, skill_name)
        if errors:
            raise HTTPException(status_code=400, detail={"code": 400, "message": "Skill 校验失败", "errors": errors})

        # 从 SKILL.md Front Matter 提取描述（上传后技能列表需要展示）
        fm = parse_skill_frontmatter(skill_dir)
        description = fm.get("description", "") or ""

        import shutil
        from src.session_manager import db, db_lock
        # ===== OBS 分支（N4）：规范化重打包 → 传 OBS 固定 key（原子覆盖）→ DB upsert → 缓存 =====
        from src.config import log
        from src.storage import get_storage
        storage = get_storage()
        if storage is not None:
            from src.storage import skill_key, zip_cache_put, mem_cache_invalidate
            from src.user.db import upsert_skill_obs
            from src.repo_key import to_repo_key
            owner = _resolve_repo_key(repo) if scope == "repo" else str(user["id"])
            db_scope = "repo" if scope == "repo" else "user"
            try:
                # 固定 key（v2.4 去版本化）：putObject 同 key 原子覆盖（最后写者胜）；
                # user 不同 owner 段天然隔离，repo 同仓库同名覆盖不限角色
                object_key = skill_key("custom" if db_scope == "user" else db_scope,
                                       owner, skill_name)
                zip_bytes = _normalize_skill_zip(skill_dir, skill_name)
                storage.save_bytes(object_key, zip_bytes)
                ok = upsert_skill_obs(db_scope, owner, skill_name, agent_type,
                                      description, object_key, str(user["id"]),
                                      category_id)
                if not ok:
                    raise HTTPException(status_code=409, detail={"code": 409, "message": f"技能 {skill_name} 记录写入失败（同名冲突或并发上传）"})
                zip_cache_put(object_key, zip_bytes)  # put 覆盖同名 key 即天然失效旧缓存
                mem_cache_invalidate(skill_name)      # 覆盖后显式失效内存提取缓存
                log.info(f"skill uploaded to OBS (fixed key): {object_key}")
            except HTTPException:
                raise
            except Exception as e:
                raise HTTPException(status_code=502, detail={"code": 502, "message": f"技能上传 OBS 失败: {e}"})
            return {"message": f"技能 {skill_name} 上传成功"}
        # ===== local 分支已移除：均衡负载环境下 local 存储会导致文件只存在单机 =====
        raise HTTPException(status_code=503, detail={"code": 503, "message": "OBS 存储未启用，技能上传失败。请检查服务端 OBS 配置后重试。"})
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=400, detail={"code": 400, "message": f"上传失败: {e}"})
    finally:
        import shutil
        shutil.rmtree(tmp_dir, ignore_errors=True)


@router.delete("/by-id/{skill_id}", operation_id="skills_delete", summary="Delete skill", description="Deletes a skill by id after an ownership check. official undeletable; user=owner; repo=managed by caller.")
def delete_skill_by_id(skill_id: int, user: dict = Depends(require_no_session_token),
                        scope: str = "user", repo: str = ""):
    """删除技能——按主键 id 精确查询，查到后做归属校验，再删除。

    权限校验：official 不允许删除；user 需本人；repo 需当前用户托管该仓库。
    """
    from src.session_manager import db, db_lock
    from src.repo_key import to_repo_key

    with db_lock:
        c = db.cursor()
        c.execute(
            "SELECT skill_name, owner_id, skill_path, scope FROM skill WHERE id=? AND is_active=1",
            (skill_id,)
        )
        row = c.fetchone()

    if not row:
        raise HTTPException(status_code=404, detail={"code": 404, "message": "技能未找到"})

    skill_name = row[0]
    owner_id = row[1] or ""
    skill_scope = row[3] or "user"

    # official 不允许删除
    if skill_scope == "official":
        raise HTTPException(status_code=403, detail={"code": 403, "message": "官方技能不允许删除"})

    # 归属校验
    if skill_scope == "user" and owner_id != str(user["id"]):
        raise HTTPException(status_code=403, detail={"code": 403, "message": "无权删除该技能"})
    if skill_scope == "repo":
        from src.user.db import get_user_repos
        user_repo_keys = {r["repo_key"] for r in (get_user_repos(user["id"]) if user.get("id") else [])}
        if owner_id not in user_repo_keys:
            raise HTTPException(status_code=403, detail={"code": 403, "message": "无权删除该技能"})

    # OBS 对象清理
    from src.storage import get_storage
    storage = get_storage()
    object_key = ""
    if storage is not None:
        from src.user.db import get_skill_object_key
        try:
            object_key = get_skill_object_key(skill_id)
        except Exception:
            pass

    # 物理删除（按主键 id）
    with db_lock:
        c = db.cursor()
        c.execute("DELETE FROM skill WHERE id=?", (skill_id,))
        db.commit()

    # local 磁盘清理已移除：均衡负载环境下 local 目录清理只作用于单机
    if storage is None:
        from src.config import log
        log.warning("OBS 不可用，仅清理 DB 记录，磁盘文件需手动清理 (skill_id=%s, scope=%s, name=%s)", skill_id, skill_scope, skill_name)

    if object_key:
        from src.storage import invalidate_skill_caches
        try:
            storage.delete_object(object_key)
        except Exception:
            pass
        invalidate_skill_caches(object_key)

    return {"message": f"技能 {skill_name} 已删除"}


class ConvertSkillRequest(BaseModel):
    skill_name: str
    repo: str  # 目标仓库 repo_key（如 Jason7602/skill-evaluator）
    source_scope: str = "user"  # 源技能 scope：user 或 repo（默认 user 兼容旧调用）


@router.post("/convert", operation_id="skills_convert", summary="Convert skill (cross scope)", description="Copies a skill between personal(user) and repo scope; the original record and directory are kept (copy, not move).")
def convert_skill(body: ConvertSkillRequest, user: dict = Depends(require_no_session_token)):
    """将技能在个人(scope=user)与仓库(scope=repo)之间双向复制。

    原技能记录与目录保留（复制而非移动）。
    source_scope='user' → 复制到仓库技能；source_scope='repo' → 复制到个人技能。
    """
    skill_name = (body.skill_name or "").strip()
    repo = (body.repo or "").strip()
    source_scope = (body.source_scope or "user").strip()
    if not skill_name:
        raise HTTPException(status_code=400, detail={"code": 400, "message": "技能名不能为空"})
    if not repo:
        raise HTTPException(status_code=400, detail={"code": 400, "message": "目标仓库不能为空"})
    if source_scope not in ("user", "repo"):
        raise HTTPException(status_code=400, detail={"code": 400, "message": f"不支持的 source_scope: {source_scope}"})

    from src.repo_key import from_repo_key
    repo_key = _resolve_repo_key(repo)
    user_id = str(user["id"])

    from src.session_manager import db, db_lock

    # 1. 查源技能记录
    if source_scope == "user":
        # 个人技能 → 仓库技能：从 user_db 查
        skill_rec = next((s for s in user_db.list_skills(user["id"]) if s["skill_name"] == skill_name), None)
        if not skill_rec:
            raise HTTPException(status_code=404, detail={"code": 404, "message": "个人技能未找到或不属于当前用户"})
    else:
        # 仓库技能 → 个人技能：从 skill 表查
        with db_lock:
            c = db.cursor()
            row = c.execute(
                "SELECT id, skill_name, agent_type, description, scope, owner_id, file_path, category_id "
                "FROM skill WHERE skill_name=? AND scope='repo' AND owner_id=? AND is_active=1 LIMIT 1",
                (skill_name, repo_key),
            ).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail={"code": 404, "message": "仓库技能未找到或不属于该仓库"})
        skill_rec = {
            "id": row[0], "skill_name": row[1], "agent_type": row[2] or "",
            "description": row[3] or "", "scope": row[4], "owner_id": row[5],
            "file_path": row[6] or "", "category_id": row[7] or 0,
        }

    # 2. 确定目标方向 + 去重检查
    if source_scope == "user":
        dst_scope, dst_owner = "repo", repo_key
        dst_skill_key_scope = "repo"
        dedup_msg = f"目标仓库已存在同名技能「{skill_name}」"
        with db_lock:
            c = db.cursor()
            exists = c.execute(
                "SELECT COUNT(*) FROM skill WHERE skill_name=? AND scope='repo' AND owner_id=? AND is_active=1",
                (skill_name, repo_key),
            ).fetchone()[0]
    else:
        dst_scope, dst_owner = "user", user_id
        dst_skill_key_scope = "custom"
        dedup_msg = f"个人技能已存在同名技能「{skill_name}」"
        exists = any(s["skill_name"] == skill_name for s in user_db.list_skills(user["id"]))
    if exists:
        raise HTTPException(status_code=409, detail={"code": 409, "message": dedup_msg})

    # ===== OBS 分支：copyObject → DB 回写 =====
    from src.storage import get_storage
    storage = get_storage()
    if storage is not None:
        from src.storage import skill_key, zip_cache_get, zip_cache_put, mem_cache_invalidate
        from src.user.db import get_skill_object_key, upsert_skill_obs
        src_key = get_skill_object_key(skill_rec["id"])
        if not src_key:
            src_label = "个人" if source_scope == "user" else "仓库"
            raise HTTPException(status_code=404, detail={"code": 404, "message": f"源{src_label}技能未在 OBS（历史数据请先执行迁移），无法转换"})
        dst_key = skill_key(dst_skill_key_scope, dst_owner, skill_name)
        if not storage.copy_object(src_key, dst_key):
            raise HTTPException(status_code=502, detail={"code": 502, "message": f"OBS 复制技能失败: {src_key} -> {dst_key}"})
        ok = upsert_skill_obs(dst_scope, dst_owner, skill_name, skill_rec.get("agent_type", ""),
                              skill_rec.get("description", ""), dst_key, user_id,
                              skill_rec.get("category_id", 0))
        if not ok:
            storage.delete_object(dst_key)
            raise HTTPException(status_code=409, detail={"code": 409, "message": dedup_msg})
        # 源 zip 若在磁盘缓存，复制到目标 key（懒加载兜底亦可）
        src_zip = zip_cache_get(src_key)
        if src_zip:
            zip_cache_put(dst_key, src_zip)
        mem_cache_invalidate(skill_name)
        dst_label = f"仓库 {repo}" if dst_scope == "repo" else "个人技能"
        return {"message": f"技能 {skill_name} 已复制到{dst_label}", "skill_name": skill_name, "scope": dst_scope, "repo": repo if dst_scope == "repo" else ""}

    # local 分支已移除：均衡负载环境下 local 存储会导致文件只存在单机
    raise HTTPException(status_code=503, detail={"code": 503, "message": "OBS 存储未启用，技能转换失败。请检查服务端 OBS 配置后重试。"})


@router.get("/{skill_id}/sub", operation_id="skills_sub_list", summary="List sub-skills of a skill package", description="Lists a skill package's sub-skills from the references/ directory.")
def list_sub_skills(skill_id: int, user: dict = Depends(get_current_user_or_api_key)):
    """返回技能包的子技能列表（从 skill_path 的 references/ 目录读取）"""
    from src.session_manager import db, db_lock
    import re as _re

    with db_lock:
        c = db.cursor()
        c.execute("SELECT skill_name, skill_path FROM skill WHERE id=? AND is_active=1", (skill_id,))
        row = c.fetchone()
    if not row:
        raise HTTPException(status_code=404, detail={"code": 404, "message": "技能未找到"})

    skill_name, skill_path = row[0], row[1] or ""
    from src.config import SKILLS_DIR, resolve_official_skill_path
    _r = resolve_official_skill_path(skill_name)
    if _r:
        base_dir = os.path.dirname(_r)
    else:
        # fallback: skill_path 可能是 3 层，尝试去掉中间层
        _parts = skill_path.split("/")
        if len(_parts) >= 3:
            base_dir = os.path.join(SKILLS_DIR, _parts[0], _parts[-1])
        else:
            base_dir = os.path.join(SKILLS_DIR, skill_path) if skill_path else ""
    ref_dir = os.path.join(base_dir, "references")

    if not os.path.isdir(ref_dir):
        return {"skill_name": skill_name, "sub_skills": []}

    subs = []
    for entry in sorted(os.listdir(ref_dir)):
        sub_dir = os.path.join(ref_dir, entry)
        if not os.path.isdir(sub_dir):
            continue
        sub_md = os.path.join(sub_dir, "SKILL.md")
        display_name = entry
        description = ""
        file_count = 0
        if os.path.isfile(sub_md):
            try:
                with open(sub_md, encoding="utf-8") as f:
                    front = f.read(500)
                m = _re.search(r'description:\s*["\']?(.*?)["\']?$', front, _re.MULTILINE)
                if m:
                    description = m.group(1)[:200]
                dm = _re.search(r'name:\s*["\']?(.*?)["\']?$', front, _re.MULTILINE)
                if dm:
                    display_name = dm.group(1)
            except Exception:
                pass
        for _, _, files in os.walk(sub_dir):
            file_count += len(files)
        subs.append({
            "name": entry,
            "display_name": display_name,
            "description": description,
            "file_count": file_count,
        })

    return {"skill_name": skill_name, "sub_skills": subs}


@router.get("/{skill_id}/sub/{sub_name}", operation_id="skills_sub_get", summary="Read sub-skill SKILL.md", description="Reads a sub-skill's SKILL.md content.")
def read_sub_skill(skill_id: int, sub_name: str, user: dict = Depends(require_no_session_token)):
    """读取技能包子技能的 SKILL.md 内容"""
    from src.session_manager import db, db_lock

    with db_lock:
        c = db.cursor()
        c.execute("SELECT skill_name, skill_path FROM skill WHERE id=? AND is_active=1", (skill_id,))
        row = c.fetchone()
    if not row:
        raise HTTPException(status_code=404, detail={"code": 404, "message": "技能未找到"})

    from src.config import SKILLS_DIR, resolve_official_skill_path
    _r = resolve_official_skill_path(row[0])
    if _r:
        base_dir = os.path.dirname(_r)
    else:
        _sp = row[1] or ""
        _parts = _sp.split("/")
        if len(_parts) >= 3:
            base_dir = os.path.join(SKILLS_DIR, _parts[0], _parts[-1])
        else:
            base_dir = os.path.join(SKILLS_DIR, _sp) if _sp else ""
    sub_md = os.path.join(base_dir, "references", sub_name, "SKILL.md")

    if not os.path.isfile(sub_md):
        raise HTTPException(status_code=404, detail={"code": 404, "message": f"子技能 {sub_name} 未找到"})

    try:
        with open(sub_md, encoding="utf-8") as f:
            content = f.read()
    except Exception as e:
        raise HTTPException(status_code=500, detail={"code": 500, "message": f"读取失败: {e}"})

    # 也读取 references/ 下的文件列表
    ref_dir = os.path.join(base_dir, "references", sub_name, "references")
    refs = []
    if os.path.isdir(ref_dir):
        for r in sorted(os.listdir(ref_dir)):
            rp = os.path.join(ref_dir, r)
            if os.path.isfile(rp):
                try:
                    with open(rp, encoding="utf-8") as f:
                        refs.append({"name": r, "content": f.read()[:2000]})
                except Exception:
                    refs.append({"name": r, "content": ""})

    return {"name": sub_name, "content": content, "references": refs}


@router.get("/package-sub/{pkg_id}/{sub_name}", operation_id="skills_package_sub_get", summary="Get sub-skill detail from package", description="Returns a sub-skill detail from a package (same shape as skill detail).")
def package_sub_skill(pkg_id: int, sub_name: str, user: dict = Depends(require_no_session_token)):
    """返回技能包子技能详情（格式与 skill detail 一致，支持导航到独立详情页）"""
    from src.session_manager import db, db_lock
    import re as _re

    with db_lock:
        c = db.cursor()
        c.execute("SELECT skill_name, skill_path, scope FROM skill WHERE id=? AND is_active=1", (pkg_id,))
        row = c.fetchone()
    if not row:
        raise HTTPException(status_code=404, detail={"code": 404, "message": "父技能未找到"})

    from src.config import SKILLS_DIR, resolve_official_skill_path
    _r = resolve_official_skill_path(row[0])
    if _r:
        base_dir = os.path.dirname(_r)
    else:
        _sp = row[1] or ""
        _parts = _sp.split("/")
        if len(_parts) >= 3:
            base_dir = os.path.join(SKILLS_DIR, _parts[0], _parts[-1])
        else:
            base_dir = os.path.join(SKILLS_DIR, _sp) if _sp else ""
    sub_md = os.path.join(base_dir, "references", sub_name, "SKILL.md")
    if not os.path.isfile(sub_md):
        raise HTTPException(status_code=404, detail={"code": 404, "message": f"子技能 {sub_name} 未找到"})

    try:
        with open(sub_md, encoding="utf-8") as f:
            content = f.read()
    except Exception as e:
        raise HTTPException(status_code=500, detail={"code": 500, "message": f"读取失败: {e}"})

    # 从 frontmatter 解析 name 和 description
    display_name = sub_name
    description = ""
    m = _re.search(r'^---\s*\n(.*?)\n---', content, _re.DOTALL)
    if m:
        front = m.group(1)
        dm = _re.search(r'name:\s*["\']?(.*?)["\']?\s*$', front, _re.MULTILINE)
        if dm:
            display_name = dm.group(1)
        desc_m = _re.search(r'description:\s*["\']?(.*?)["\']?\s*$', front, _re.MULTILINE)
        if desc_m:
            description = desc_m.group(1)[:200]

    return {
        "id": f"{pkg_id}/{sub_name}",
        "name": sub_name,
        "display_name": display_name,
        "description": description,
        "agent_type": "sre",
        "scope": row[2] or "official",
        "content": content,
        "type": "package-sub",
    }
