"""Project Templates 路由：模板列表获取与一键创建"""

import json, os, re, asyncio, base64

from fastapi import APIRouter, HTTPException, Depends

from pydantic import BaseModel

from src.api.deps import require_no_session_token, require_org_member

from src.kernel.config import log, CONFIG_DIR, EXAMPLE_DIR

from src.domains.mgmt.user.repo_manager import create_repo, get_user_gitcode_login, push_template_to_repo

# D3/S5：token 取用统一走 get_user_token_for（卡派生 provider），不再导入
# K1 分叉包装器族（github 专用预刷新包装器等）——本文件 0 调用点，防符号回潮。

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
    """Read global bot config, return (bot_token, bot_username). Token and username
    are both selected by provider.

    Issue #345: Use unified DB entry (DB-first with file fallback).
    D44/S66：统一走来源链（get_global_bot_token_for / resolve_bot_username，
    meta key + 卡 bot_identity 派生），无平台名分支。
    """
    from src.domains.mgmt.user.db import get_global_bot_token_for
    from src.domains.mgmt.user.repo_manager import resolve_bot_username
    return get_global_bot_token_for(provider), resolve_bot_username(provider)


@router.get("", operation_id="project_templates_list", summary="List template categories", description="Lists configured template categories and templates.")
def list_templates(user: dict = Depends(require_no_session_token)):
    """返回配置化的模板分类与模板列表"""
    data = _load_templates()
    return {"categories": data.get("categories", [])}

@router.get("/{template_id}/readme", operation_id="project_template_readme", summary="Get template README", description="Gets template README (local preferred; git sources fetched from GitCode/GitHub).")
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

    # 2. git 来源：统一命令层拉取（D44/S66：方言模板按卡，无平台名分支）
    source_repo = tpl.get("source_repo", "")
    source_platform = tpl.get("source_platform", "gitcode")
    if not source_repo:
        return {"content": "", "source": "git"}

    try:
        from src.domains.runtime.vcs.api_client import VcsApiClient
        from src.domains.runtime.vcs.platform_card import get_platform_card
        _card = get_platform_card(source_platform)
        # token 走平台卡用户级 env（空=匿名拉取，公开仓库仍可用）
        _token = os.environ.get(_card.user_token_env_name(), "") if _card else ""
        _owner, _name = source_repo.split("/", 1) if "/" in source_repo else ("", "")
        if not _owner or not _name:
            return {"content": "", "source": "git"}
        data = VcsApiClient(source_platform, _token, _owner, _name).get_readme()
        if data.get("content"):
            return {"content": data["content"], "source": source_platform}
    except Exception as e:
        log.warning(f"fetch readme for {template_id} failed: {e}")

    return {"content": "", "source": "git"}

@router.get("/{template_id}/preview", operation_id="project_template_preview", summary="Get template preview HTML", description="Gets template preview HTML (local template reads examples/{id}/preview.html).")
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
    provider: str = ""  # 平台卡 provider（gitcode/gitee/gitlab/…），空时按卡目录 ∩ 已绑定平台自动选


def _pick_platform_token(user: dict) -> tuple[str, str]:
    """卡目录 ∩ 用户已绑定平台 → (provider, token)；无可用返回 ("", "")。

    - 平台集合唯一权威 = 仓库平台卡目录（list_vcs_platforms）；
    - 候选 = 用户 external_identities 已绑定平台（PAT-only 绑定同样在册）；
    - 优先 login_provider 命中的平台，其余按卡目录顺序；
    - token 统一走 get_user_token_for（provider 传参，PAT > OAuth）。
    """
    from src.api.deps import get_user_token_for
    from src.domains.mgmt.auth.grape_account_db import get_user_identities
    from src.domains.runtime.vcs.platform_card import list_vcs_platforms as _lvp
    _uid = str(user.get("id") or "")
    _uname = user.get("username") or ""
    try:
        _catalog = list(_lvp())
    except Exception:
        _catalog = []
    try:
        _bound = {i.get("provider") for i in (get_user_identities(_uid) or []) if i.get("provider")}
    except Exception:
        _bound = set()
    _login = (user.get("login_provider") or "").strip()
    _ordered = [p for p in _catalog if p in _bound]
    _ordered.sort(key=lambda p: 0 if p == _login else 1)  # 稳定排序：login_provider 优先
    for _p in _ordered:
        try:
            _tok = get_user_token_for(_uname, user_id=_uid, provider=_p)
        except Exception:
            _tok = ""
        if _tok:
            return _p, _tok
    return "", ""

@router.post("/create", operation_id="project_template_create", summary="Create project from template", description="One-shot create: create repo -> write template code -> auto-host -> apply dev scene.")
async def create_from_template(body: CreateTemplateRequest,
                               user: dict = Depends(require_org_member)):
    """一键创建：建仓库 → 写模板代码 → 自动托管 → 应用开发场景"""
    ctx = _find_template(body.template_id)
    if not ctx:
        raise HTTPException(status_code=404, detail={"code": 404, "message": f"模板 '{body.template_id}' 不存在"})
    tpl = ctx["template"]

    # 按 provider 分流：前端显式传 provider 时用指定平台，否则自动选择
    # D88/S115：卡在册即走显式平台（消基线白名单二元分叉 in ("gitcode","github")——
    # D71 登记收口项）。下游全链路已卡驱动：建仓/身份（create_provider 卡工厂）、
    # 模板 push（S90 卡派生 host）、bot 配置（卡派生 meta key）、host_repo 托管。
    from src.domains.runtime.vcs.platform_card import get_platform_card as _gate_card
    if body.provider and _gate_card(body.provider) is not None:
        # D44/S66：显式平台 token 统一走 D3 入口（provider 传参，无平台名分支）
        provider = body.provider
        api_token = get_user_token_for(user["username"], user_id=str(user.get("id") or ""), provider=provider)
        if not api_token:
            raise HTTPException(status_code=400, detail={"code": 400, "message": f"{provider} API Token 未配置，请先绑定 {provider} 平台"})
    else:
        # 卡目录 ∩ 用户已绑定平台（尊重 login_provider）——平台卡在册即可选，
        # 不再用存量双平台分支函数（gitee/gitlab 单绑定用户此前恒 400）
        provider, api_token = _pick_platform_token(user)
        if not provider:
            raise HTTPException(status_code=400, detail={
                "code": 400,
                "message": "当前账号没有可用的仓库平台：请先在「设置 → 凭据」绑定平台（GitCode/GitHub/Gitee/GitLab 等）并配置访问令牌",
            })
    if not api_token:
        raise HTTPException(status_code=400, detail={"code": 400, "message": "API Token 未配置，请先通过 OAuth 登录或配置个人 Token"})

    repo_name = (body.repo_name or tpl.get("default_repo_name", tpl.get("id", ""))).strip()
    if not _REPO_NAME_RE.match(repo_name):
        raise HTTPException(status_code=400, detail={"code": 400, "message": "仓库名只能包含字母、数字、下划线和短横线"})

    # 1. 创建空仓库（用户个人名下）。同名已存在时不视为致命错误：
    #    继续走 push + 托管流程（空仓库可复用；非空仓库 push 冲突时再报错）
    repo_created = False
    try:
        # D44/S66：建仓+取 login 统一走适配器端口（存量平台子类委托原实现，
        # config-only 平台卡驱动自动正确，无平台名分支）
        from src.kernel.token_resolver import create_provider
        _prov_adapter = create_provider(provider)
        owner_login = _prov_adapter.get_user_login(api_token)
        if not owner_login:
            raise HTTPException(status_code=502, detail={"code": 502, "message": f"无法获取当前 {provider} 用户身份"})
        _prov_adapter.create_repo(api_token, repo_name, description=tpl.get("description", ""))
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
                              api_token, bot_token=bot_token, provider=provider,
                              user_id=user.get("id", "") or ""))
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
        from src.domains.mgmt.routes.repos import RepoAction, host_repo
        host_result = await asyncio.to_thread(host_repo, RepoAction(full_name=full_name), user, provider=provider)
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

    # Read actual bot username (from DB meta table), for reminder text
    _, bot_username = _load_global_bot_config(provider=provider)
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
        from src.kernel.repo_key import to_repo_key
        from src.api.onboarding import trigger_onboarding, cleanup_stale_repo_data
        repo_key = to_repo_key(full_name, provider=provider)
        # FR-26.1: 优先用 VCS 平台 login（owner_login），注册名做降级
        # owner_login 来自 get_user_gitcode_login/get_user_github_login（VCS /user 接口）
        # 下游 create_standard_task 用 assignee=owner 调 VCS API 指派 Issue，需要平台 login
        # 下游 _get_user_id_by_gitcode(owner, repo_key) 查 um 平台列，也需要平台 login
        owner = owner_login or user.get("username", "")
        # 仓库是全新创建时，清理同名旧仓库可能残留的 DB 数据
        # （旧仓库若被用户在 GitCode 网页直接删除、未走解除托管，
        #   events/issue_state/agent_tasks/sessions 等残留，导致新仓库前端界面混乱）
        if repo_created:
            cleanup_stale_repo_data(repo_key)

        # 模板 scene_id 覆盖默认场景
        tpl_scene_id = tpl.get("scene_id") or ctx.get("scene_id") or ""
        if tpl_scene_id and tpl_scene_id != "default":
            try:
                _apply_template_scene(repo_key, tpl_scene_id, str(user.get("id") or ""))
            except Exception as e:
                log.warning(f"template scene override failed for '{tpl_scene_id}': {e}")

        trigger_onboarding(repo_key, owner, template_id=body.template_id)
    except Exception as e:
        log.warning(f"template onboarding trigger failed: {e}")

    from src.kernel.repo_key import to_repo_key as _to_rk
    return {"full_name": full_name, "webhook_id": webhook_id,
            "host_warning": host_warning,
            "message": msg,
            "provider": provider,
            "scene_id": tpl.get("scene_id") or ctx.get("scene_id") or "",
            "repo_key": _to_rk(full_name, provider=provider) if provider else ""}

import json, os, re, asyncio

from src.domains.mgmt.user.repo_manager import push_template_to_repo

from src.api.deps import get_user_token_for

def _apply_template_scene(repo_key: str, tpl_scene_id: str, user_id: str = "") -> str:
    """把模板自带场景应用到仓库并写 managed_repos.dev_scene，返回仓内副本真实 scene_id。

    Issue #541：dev_scene 必须写 apply_market_scene_by_name 回传的真实 ID
    （custom-{uuid}），不能再写模板场景名——否则 onboarding Issue 绑定到
    仓内不存在的场景，派发侧回退全局模板行（无引擎）→ SRE 派发失败。
    应用失败（模板名查不到/应用被拒）返回空串并保留托管时已应用的默认场景。
    """
    from src.api.pipeline_config import apply_market_scene_by_name
    real_scene_id = apply_market_scene_by_name(repo_key, tpl_scene_id, user_id) or ""
    if not real_scene_id:
        log.warning(f"template scene override: apply '{tpl_scene_id}' to {repo_key} failed, "
                    f"keep default scene")
        return ""
    # 写入 managed_repos.dev_scene，供 onboarding 读取并绑定到 Issue
    from src.domains.mgmt.user.db import set_repo_scene
    set_repo_scene(repo_key, real_scene_id)
    log.info(f"template scene override: applied '{tpl_scene_id}' to {repo_key} "
             f"(dev_scene={real_scene_id})")
    return real_scene_id
