"""Project Templates 路由：模板列表获取与一键创建"""
import json, os, re, asyncio
from fastapi import APIRouter, HTTPException, Depends
from pydantic import BaseModel
from src.api.deps import get_current_user, get_user_token
from src.config import log, CONFIG_DIR
from src.user.repo_manager import create_repo, get_user_gitcode_login, push_template_to_repo

router = APIRouter(prefix="/api/project-templates", tags=["project-templates"])

_CONFIG_PATH = os.path.join(CONFIG_DIR, "project-templates.json")

_REPO_NAME_RE = re.compile(r"^[a-zA-Z0-9_\-]+$")


def _load_templates() -> dict:
    """读取 configs/project-templates.json，非法/缺失返回空结构"""
    if not os.path.exists(_CONFIG_PATH):
        log.warning("project-templates.json not found")
        return {"categories": []}
    try:
        with open(_CONFIG_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {"categories": []}
    except Exception as e:
        log.warning(f"Failed to load project-templates.json: {e}")
        return {"categories": []}


def _find_template(template_id: str) -> dict | None:
    """在配置中按 template_id 查找模板（含其分类 scene_id 上下文）"""
    for cat in _load_templates().get("categories", []):
        for t in cat.get("templates", []):
            if t.get("id") == template_id:
                ctx = dict(cat)
                ctx["template"] = t
                return ctx
    return None


def _load_global_bot_config() -> tuple[str, str]:
    """读取全局 bot 配置，返回 (bot_token, bot_username)。"""
    bot_token, bot_username = "", ""
    try:
        path = os.path.join(CONFIG_DIR, "global-bot-token.json")
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                gbt = json.load(f)
            bot_token = gbt.get("gitcode_api_token", "") or ""
            us = gbt.get("bot_usernames") or []
            bot_username = us[0] if us else ""
    except Exception as e:
        log.warning(f"load global bot config failed: {e}")
    return bot_token, bot_username


@router.get("")
def list_templates(user: dict = Depends(get_current_user)):
    """返回配置化的模板分类与模板列表"""
    data = _load_templates()
    return {"categories": data.get("categories", [])}


@router.get("/top1000")
def list_top1000_templates(
    platform: str = "all",
    page: int = 0,
    size: int = 20,
    sort_by: str = "stars",
    keyword: str = "",
    user: dict = Depends(get_current_user),
):
    """Returns featured project templates (from DB featured_templates table, supports pagination + search + sorting)"""
    import logging
    _log = logging.getLogger(__name__)
    from src.session_manager import db, db_lock
    try:
        offset = page * size
        where_clauses = []
        params = []
        if platform and platform != "all" and platform != "undefined":
            where_clauses.append("platform = ?")
            params.append(platform)
        if keyword:
            where_clauses.append("(repo_full_name LIKE ? OR description LIKE ? OR repo_name LIKE ?)")
            params.extend([f"%{keyword}%", f"%{keyword}%", f"%{keyword}%"])
        where_sql = (" WHERE " + " AND ".join(where_clauses)) if where_clauses else ""
        sort_col = "stars" if sort_by == "stars" else "forks" if sort_by == "forks" else "stars"
        with db_lock:
            c = db.cursor()
            c.execute(f"SELECT COUNT(*) FROM featured_templates{where_sql}", params)
            total = c.fetchone()[0]
            c.execute(
                f"SELECT platform, repo_full_name, repo_name, description, language, "
                f"stars, forks, topics, html_url, clone_url, category, recommend_reason, fetched_at "
                f"FROM featured_templates{where_sql} "
                f"ORDER BY {sort_col} DESC LIMIT ? OFFSET ?",
                params + [size, offset]
            )
            rows = c.fetchall()
        import json as _json
        templates = []
        for r in rows:
            topics = []
            try:
                topics = _json.loads(r[7]) if r[7] else []
            except Exception:
                pass
            templates.append({
                "id": f"{r[0]}-{r[1].replace('/', '-')}",
                "name": r[2],
                "description": (r[3] or "")[:120],
                "recommend_reason": r[11] or "",
                "icon": "📦",
                "source": "git",
                "source_repo": r[1],
                "source_platform": r[0],
                "clone_url": r[9],
                "html_url": r[8] or "",
                "default_repo_name": r[2].lower()[:30],
                "tags": topics[:5] or ([r[4]] if r[4] else []),
                "scene_id": "default",
                "stats": {"show": True, "stars": r[5], "language": r[4], "platform": r[0]},
            })
        return {"templates": templates, "total": total, "page": page, "size": size}
    except Exception as e:
        _log.warning(f"list_top1000_templates: featured_templates table may not exist or query failed: {e}. Run scripts/sync_featured_templates.py to populate.")
        return {"templates": [], "total": 0, "page": page, "size": size}


class CreateTemplateRequest(BaseModel):
    template_id: str
    repo_name: str = ""


@router.post("/create")
async def create_from_template(body: CreateTemplateRequest,
                               user: dict = Depends(get_current_user)):
    """一键创建：建仓库 → 写模板代码 → 自动托管 → 应用开发场景"""
    ctx = _find_template(body.template_id)
    if not ctx:
        raise HTTPException(status_code=404, detail={"code": 404, "message": f"模板 '{body.template_id}' 不存在"})
    tpl = ctx["template"]

    api_token = get_user_token(user["username"])
    if not api_token:
        raise HTTPException(status_code=400, detail={"code": 400, "message": "GitCode API Token 未配置，请先通过 OAuth 登录或配置个人 Token"})

    repo_name = (body.repo_name or tpl.get("default_repo_name", tpl.get("id", ""))).strip()
    if not _REPO_NAME_RE.match(repo_name):
        raise HTTPException(status_code=400, detail={"code": 400, "message": "仓库名只能包含字母、数字、下划线和短横线"})

    # 1. 创建空仓库（用户个人名下）。同名已存在时不视为致命错误：
    #    继续走 push + 托管流程（空仓库可复用；非空仓库 push 冲突时再报错）
    repo_created = False
    try:
        gitcode_login = get_user_gitcode_login(api_token)
        if not gitcode_login:
            raise HTTPException(status_code=502, detail={"code": 502, "message": "无法获取当前用户身份"})
        create_repo(api_token, repo_name, description=tpl.get("description", ""))
        repo_created = True
    except HTTPException:
        raise
    except Exception as e:
        msg = str(e)
        # GitCode 同名仓库冲突：HTTP 409/422 + "already exists"/"same name"/"已存在"
        if any(k in msg for k in ("409", "422", "already exists", "same name", "已存在", "已经存在")):
            repo_created = False  # 仓库已存在，尝试复用
        else:
            raise HTTPException(status_code=502, detail={"code": 502, "message": f"创建仓库失败: {msg}"})

    full_name = f"{gitcode_login}/{repo_name}"

    # 2. 写入模板代码（放到 executor 避免阻塞 event loop）
    try:
        bot_token, _ = _load_global_bot_config()
        import asyncio, functools
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None,
            functools.partial(push_template_to_repo, tpl, gitcode_login, repo_name,
                              api_token, bot_token=bot_token))
    except Exception as e:
        log.warning(f"template push failed: {e}")
        if not repo_created:
            raise HTTPException(status_code=409, detail={"code": 409, "message": f"同名仓库 {gitcode_login}/{repo_name} 已存在且模板代码写入失败（可能是该仓库已有内容）。请更换仓库名，或在仓库管理页对该仓库手动操作。"})
        raise HTTPException(status_code=502, detail={"code": 502, "message": f"仓库已创建但模板代码写入失败: {e}"})

    # 3. 自动加入托管（复用 /api/repos/host 全链路）
    #    host_repo 是同步函数（含 GitCode HTTP 调用），用 to_thread 避免阻塞 event loop
    webhook_id = 0
    host_warning = ""
    bot_username = ""
    try:
        from src.api.routes.repos import RepoAction, host_repo
        host_result = await asyncio.to_thread(host_repo, RepoAction(full_name=full_name), user)
        webhook_id = host_result.get("webhook_id", 0) or 0
    except HTTPException as e:
        # 托管失败（如 bot 未成为维护者）— 不中断流程，记录警告
        detail = e.detail if isinstance(e.detail, str) else (e.detail.get("message", "") if isinstance(e.detail, dict) else str(e.detail))
        log.warning(f"template host failed (non-fatal): {detail}")
        host_warning = detail or "托管配置失败"
    except Exception as e:
        log.warning(f"template host failed (non-fatal): {e}")
        host_warning = str(e)

    # 只有 webhook 真没建成功时才显示提醒；webhook_id 有值说明托管已成功
    if webhook_id and host_warning:
        log.info(f"template host: webhook_id={webhook_id} exists, suppressing host_warning: {host_warning}")
        host_warning = ""

    # 读取实际 bot 用户名（从 global-bot-token.json），用于提醒文案
    _, bot_username = _load_global_bot_config()
    if not bot_username:
        bot_username = "grape_bot"

    # 只有 webhook 真没建成功时才显示提醒；webhook_id 有值说明托管已成功
    if webhook_id:
        if host_warning:
            log.info(f"template host: webhook_id={webhook_id} exists, suppressing host_warning: {host_warning}")
        host_warning = ""
        msg = f"项目 {full_name} 创建并托管成功"
    else:
        # 托管失败：构造用户友好的提醒文案（bot 名从各自环境配置读取）
        msg = f"项目 {full_name} 创建成功"
        if host_warning:
            host_warning = f"托管配置失败：{host_warning}"
        else:
            host_warning = "托管配置失败"
        msg += f"。⚠️ 重要提醒：请到 GitCode 仓库设置 → 成员管理，将机器人账号（{bot_username}）添加为维护者，然后手动托管仓库以完成自动化配置。"

    # 4. 应用开发场景
    #    apply_scene 同样是同步函数，用 to_thread 避免阻塞
    scene_id = tpl.get("scene_id") or ctx.get("scene_id") or "default"
    try:
        from src.api.routes.scenes import ApplySceneRequest, apply_scene
        await asyncio.to_thread(apply_scene, gitcode_login, repo_name, ApplySceneRequest(scene_id=scene_id), user)
    except Exception as e:
        log.warning(f"template apply scene failed: {e}")

    # 5. 持久化开发场景 + 异步首次托管引导（仅模板创建触发）
    try:
        from src.repo_key import to_repo_key
        from src.user.db import set_repo_scene
        from src.api.onboarding import trigger_onboarding, cleanup_stale_repo_data
        repo_key = to_repo_key(full_name)
        owner = user.get("username", "") or gitcode_login
        # 仓库是全新创建时，清理同名旧仓库可能残留的 DB 数据
        # （旧仓库若被用户在 GitCode 网页直接删除、未走解除托管，
        #   events/issue_state/agent_tasks/sessions 等残留，导致新仓库前端界面混乱）
        if repo_created:
            cleanup_stale_repo_data(repo_key)
        set_repo_scene(repo_key, scene_id)
        trigger_onboarding(repo_key, owner, template_id=body.template_id)
    except Exception as e:
        log.warning(f"template onboarding trigger failed: {e}")

    return {"full_name": full_name, "webhook_id": webhook_id, "scene_id": scene_id,
            "host_warning": host_warning,
            "message": msg}
