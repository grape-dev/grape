"""Project Templates 路由：模板列表获取与一键创建"""
import json, os, re
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
async def list_templates(user: dict = Depends(get_current_user)):
    """返回配置化的模板分类与模板列表"""
    data = _load_templates()
    return {"categories": data.get("categories", [])}


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

    # 2. 写入模板代码（source=local 复制目录 / source=git clone）
    try:
        bot_token, _ = _load_global_bot_config()
        push_template_to_repo(tpl, gitcode_login, repo_name, api_token, bot_token=bot_token)
    except Exception as e:
        log.warning(f"template push failed: {e}")
        if not repo_created:
            raise HTTPException(status_code=409, detail={"code": 409, "message": f"同名仓库 {gitcode_login}/{repo_name} 已存在且模板代码写入失败（可能是该仓库已有内容）。请更换仓库名，或在仓库管理页对该仓库手动操作。"})
        raise HTTPException(status_code=502, detail={"code": 502, "message": f"仓库已创建但模板代码写入失败: {e}"})

    # 3. 自动加入托管（复用 /api/repos/host 全链路）
    try:
        from src.api.routes.repos import RepoAction, host_repo
        host_result = await host_repo(RepoAction(full_name=full_name), user)
        webhook_id = host_result.get("webhook_id", 0)
    except HTTPException:
        raise
    except Exception as e:
        log.warning(f"template host failed: {e}")
        raise HTTPException(status_code=502, detail={"code": 502, "message": f"仓库已创建但托管失败: {e}（可稍后手动托管）"})

    # 4. 应用开发场景
    scene_id = tpl.get("scene_id") or ctx.get("scene_id") or "default"
    try:
        from src.api.routes.scenes import ApplySceneRequest, apply_scene
        await apply_scene(gitcode_login, repo_name, ApplySceneRequest(scene_id=scene_id), user)
    except Exception as e:
        log.warning(f"template apply scene failed: {e}")

    return {"full_name": full_name, "webhook_id": webhook_id, "scene_id": scene_id,
            "message": f"项目 {full_name} 创建并托管成功"}
