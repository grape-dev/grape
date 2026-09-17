"""Project Templates 路由：模板列表获取与一键创建"""
import json, os, re, asyncio, base64
from fastapi import APIRouter, HTTPException, Depends
from pydantic import BaseModel
from src.api.deps import require_no_session_token
from src.config import log, CONFIG_DIR, EXAMPLE_DIR
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


def _load_global_bot_config(provider: str = "gitcode") -> tuple[str, str]:
    """读取全局 bot 配置，返回 (bot_token, bot_username)。按 provider 选 token 字段。"""
    bot_token, bot_username = "", ""
    try:
        path = os.path.join(CONFIG_DIR, "global-bot-token.json")
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                gbt = json.load(f)
            if provider == "github":
                bot_token = gbt.get("github_bot_token", "") or gbt.get("gitcode_api_token", "") or ""
            else:
                bot_token = gbt.get("gitcode_api_token", "") or ""
            us = gbt.get("bot_usernames") or []
            bot_username = us[0] if us else ""
    except Exception as e:
        log.warning(f"load global bot config failed: {e}")
    return bot_token, bot_username


@router.get("", operation_id="project_templates_list", summary="List template categories")
def list_templates(user: dict = Depends(require_no_session_token)):
    """返回配置化的模板分类与模板列表"""
    data = _load_templates()
    return {"categories": data.get("categories", [])}


@router.get("/{template_id}/readme", operation_id="project_template_readme", summary="Get template README")
def get_template_readme(template_id: str, user: dict = Depends(require_no_session_token)):
    """获取模板 README 内容（本地优先，git 来源从 GitCode/GitHub API 拉取）"""
    ctx = _find_template(template_id)
    if not ctx:
        raise HTTPException(status_code=404, detail={"code": 404, "message": f"模板 '{template_id}' 不存在"})
    tpl = ctx["template"]

    # 1. 本地模板：直接读 examples/{id}/README.md
    if tpl.get("source") == "local":
        readme_path = os.path.join(EXAMPLE_DIR, template_id, "README.md")
        if os.path.exists(readme_path):
            try:
                with open(readme_path, "r", encoding="utf-8") as f:
                    return {"content": f.read(), "source": "local"}
            except Exception as e:
                log.warning(f"read local readme failed: {e}")
        return {"content": "", "source": "local"}

    # 2. git 来源：从 GitCode/GitHub API 拉取
    source_repo = tpl.get("source_repo", "")
    source_platform = tpl.get("source_platform", "gitcode")
    if not source_repo:
        return {"content": "", "source": "git"}

    try:
        import requests
        if source_platform == "github":
            url = f"https://api.github.com/repos/{source_repo}/readme"
            headers = {"Accept": "application/vnd.github.v3+json"}
            github_token = os.environ.get("GITHUB_TOKEN", "")
            if github_token:
                headers["Authorization"] = f"token {github_token}"
            r = requests.get(url, headers=headers, timeout=10)
            if r.status_code == 200:
                content = base64.b64decode(r.json().get("content", "")).decode("utf-8")
                return {"content": content, "source": "github"}
        else:
            url = f"https://gitcode.com/api/v5/repos/{source_repo}/contents/README.md"
            gitcode_pat = os.environ.get("GITCODE_PAT", "")
            headers = {}
            if gitcode_pat:
                headers["PRIVATE-TOKEN"] = gitcode_pat
            r = requests.get(url, headers=headers, timeout=10)
            if r.status_code == 200:
                content = base64.b64decode(r.json().get("content", "")).decode("utf-8")
                return {"content": content, "source": "gitcode"}
            # 尝试小写 readme.md
            url2 = f"https://gitcode.com/api/v5/repos/{source_repo}/contents/readme.md"
            r2 = requests.get(url2, headers=headers, timeout=10)
            if r2.status_code == 200:
                content = base64.b64decode(r2.json().get("content", "")).decode("utf-8")
                return {"content": content, "source": "gitcode"}
    except Exception as e:
        log.warning(f"fetch readme for {template_id} failed: {e}")

    return {"content": "", "source": "git"}


@router.get("/{template_id}/preview", operation_id="project_template_preview", summary="Get template preview HTML")
def get_template_preview(template_id: str, user: dict = Depends(require_no_session_token)):
    """获取模板预览图 HTML（本地模板读取 examples/{id}/preview.html）"""
    ctx = _find_template(template_id)
    if not ctx:
        raise HTTPException(status_code=404, detail={"code": 404, "message": f"模板 '{template_id}' 不存在"})
    tpl = ctx["template"]

    if tpl.get("source") == "local":
        preview_path = os.path.join(EXAMPLE_DIR, template_id, "preview.html")
        if os.path.exists(preview_path):
            try:
                with open(preview_path, "r", encoding="utf-8") as f:
                    return {"content": f.read(), "source": "local"}
            except Exception as e:
                log.warning(f"read local preview failed: {e}")

    return {"content": "", "source": "none"}


class CreateTemplateRequest(BaseModel):
    template_id: str
    repo_name: str = ""


@router.post("/create", operation_id="project_template_create", summary="Create project from template")
async def create_from_template(body: CreateTemplateRequest,
                               user: dict = Depends(require_no_session_token)):
    """一键创建：建仓库 → 写模板代码 → 自动托管 → 应用开发场景"""
    ctx = _find_template(body.template_id)
    if not ctx:
        raise HTTPException(status_code=404, detail={"code": 404, "message": f"模板 '{body.template_id}' 不存在"})
    tpl = ctx["template"]

    # 按 provider 分流：优先 GitHub token，降级 GitCode
    from src.api.routes.repos import _get_user_provider_and_token
    provider, api_token = _get_user_provider_and_token(user["username"])
    if not api_token:
        raise HTTPException(status_code=400, detail={"code": 400, "message": "API Token 未配置，请先通过 OAuth 登录或配置个人 Token"})

    repo_name = (body.repo_name or tpl.get("default_repo_name", tpl.get("id", ""))).strip()
    if not _REPO_NAME_RE.match(repo_name):
        raise HTTPException(status_code=400, detail={"code": 400, "message": "仓库名只能包含字母、数字、下划线和短横线"})

    # 1. 创建空仓库（用户个人名下）。同名已存在时不视为致命错误：
    #    继续走 push + 托管流程（空仓库可复用；非空仓库 push 冲突时再报错）
    repo_created = False
    try:
        if provider == "github":
            from src.user.repo_manager import get_user_github_login, create_github_repo
            owner_login = get_user_github_login(api_token)
            if not owner_login:
                raise HTTPException(status_code=502, detail={"code": 502, "message": "无法获取当前 GitHub 用户身份"})
            create_github_repo(api_token, repo_name, description=tpl.get("description", ""))
        else:
            gitcode_login = get_user_gitcode_login(api_token)
            if not gitcode_login:
                raise HTTPException(status_code=502, detail={"code": 502, "message": "无法获取当前用户身份"})
            owner_login = gitcode_login
            create_repo(api_token, repo_name, description=tpl.get("description", ""))
        repo_created = True
    except HTTPException:
        raise
    except Exception as e:
        msg = str(e)
        # 同名仓库冲突：HTTP 409/422 + "already exists"/"same name"/"已存在"
        if any(k in msg for k in ("409", "422", "already exists", "same name", "已存在", "已经存在")):
            repo_created = False  # 仓库已存在，尝试复用
        else:
            raise HTTPException(status_code=502, detail={"code": 502, "message": f"创建仓库失败: {msg}"})

    full_name = f"{owner_login}/{repo_name}"

    # 2. 写入模板代码（放到 executor 避免阻塞 event loop）
    try:
        bot_token, _ = _load_global_bot_config(provider=provider)
        import asyncio, functools
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None,
            functools.partial(push_template_to_repo, tpl, owner_login, repo_name,
                              api_token, bot_token=bot_token, provider=provider))
    except Exception as e:
        log.warning(f"template push failed: {e}")
        if not repo_created:
            raise HTTPException(status_code=409, detail={"code": 409, "message": f"同名仓库 {owner_login}/{repo_name} 已存在且模板代码写入失败（可能是该仓库已有内容）。请更换仓库名，或在仓库管理页对该仓库手动操作。"})
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
        msg += f"。⚠️ 重要提醒：请到仓库设置 → 成员管理，将机器人账号（{bot_username}）添加为维护者，然后手动托管仓库以完成自动化配置。"

    # 4. 异步首次托管引导（仅模板创建触发）
    #    仓库托管时 apply_default_scene_to_repo 已应用默认「通用开发」场景。
    #    如果模板自带 scene_id（如 hello-world），覆盖为模板对应的场景。
    try:
        from src.repo_key import to_repo_key
        from src.api.onboarding import trigger_onboarding, cleanup_stale_repo_data
        repo_key = to_repo_key(full_name, provider=provider)
        owner = user.get("username", "") or owner_login
        # 仓库是全新创建时，清理同名旧仓库可能残留的 DB 数据
        # （旧仓库若被用户在 GitCode 网页直接删除、未走解除托管，
        #   events/issue_state/agent_tasks/sessions 等残留，导致新仓库前端界面混乱）
        if repo_created:
            cleanup_stale_repo_data(repo_key)

        # 模板 scene_id 覆盖默认场景
        tpl_scene_id = tpl.get("scene_id") or ctx.get("scene_id") or ""
        if tpl_scene_id and tpl_scene_id != "default":
            try:
                from src.api.pipeline_config import apply_market_scene_by_name
                apply_market_scene_by_name(repo_key, tpl_scene_id, user.get("id", 0))
                log.info(f"template scene override: applied '{tpl_scene_id}' to {repo_key}")
            except Exception as e:
                log.warning(f"template scene override failed for '{tpl_scene_id}': {e}")

        trigger_onboarding(repo_key, owner, template_id=body.template_id)
    except Exception as e:
        log.warning(f"template onboarding trigger failed: {e}")

    from src.repo_key import to_repo_key as _to_rk
    return {"full_name": full_name, "webhook_id": webhook_id,
            "host_warning": host_warning,
            "message": msg,
            "provider": provider,
            "repo_key": _to_rk(full_name, provider=provider) if provider else ""}
