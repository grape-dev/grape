"""仓库路由：列表、托管、移除、同步"""
from fastapi import APIRouter, HTTPException, Depends
from pydantic import BaseModel
from src.config import log
from src.user import db as user_db
from src.user.repo_manager import fetch_repos_with_role, setup_webhook, find_existing_webhook, create_repo
from src.api.deps import get_current_user, get_bot_token, get_user_token
from src.user.db import update_managed_repo_bot_token, update_managed_repo_bot_username, get_bot_token_by_repo

router = APIRouter(prefix="/api/repos", tags=["repos"])

# 仓库列表缓存有效期（秒）：超过后自动从 GitCode 重新拉取
REPOS_CACHE_TTL = 1800  # 30 分钟（避免频繁触发 GitCode N+1 权限查询）


class RepoAction(BaseModel):
    full_name: str


class CreateRepoBody(BaseModel):
    name: str
    description: str = ""
    private: bool = False
    visibility: str = ""
    auto_init: bool = True
    default_branch: str = "main"
    org: str = ""
    host_after_create: bool = False


def _fetch_and_cache(user_id: int, api_token: str) -> list[dict]:
    """从 GitCode 拉取仓库列表，写入 user_repos 缓存，返回过滤后的结果"""
    repos = fetch_repos_with_role(api_token)
    user_db.save_user_repos(user_id, repos)
    return repos


def _filter_repos(user_id: int, repos: list[dict]) -> list[dict]:
    """过滤仓库列表：返回用户身份可见的全部仓库（管理员 + 非管理员成员）。

    状态语义（前端卡片三态校验）：
    - in_managed（仓库在 managed_repos = 已被任何人托管）→ is_hosted 按是否配置 webhook；
    - can_host（用户是仓库管理员/所有者，或 permission.admin）→ 未托管时可「加入并托管」；
    - 非管理员成员 + 未托管 → 照样返回（展示「未托管」），点「加入并托管」由前端提示
      「无权限托管，请联系管理员」，不再静默 403。
    """
    from src.repo_key import to_repo_key
    managed = {r["repo_key"]: r["webhook_id"] for r in user_db.get_all_managed_repos()}
    result = []
    for r in repos:
        full_name = r.get("full_name", "")
        repo_key = to_repo_key(r.get("repo_key", "") or full_name)
        is_admin = r.get("is_admin", False)
        in_managed = repo_key in managed
        hosted = in_managed and bool(managed.get(repo_key))

        role = "Admin" if is_admin else "Member"
        result.append({
            "repo_key": repo_key,
            "full_name": full_name,
            "owner": r.get("owner", ""),
            "name": r.get("name", ""),
            "description": r.get("description", ""),
            "role": role,
            "can_host": bool(is_admin),
            "in_managed": in_managed,
            "is_joined": in_managed,
            "is_hosted": hosted,
            "created_at": r.get("created_at", "") or "",
        })
    return result


def _probe_stale_repos(api_token: str, user_id: int):
    """检查当前用户名下的托管仓库在 GitCode 上的状态，清理失效的托管记录。

    两步检查：
    1. 并发查仓库是否存在（404 = 已删除，立即清理）
    2. 仓库存在的，串行查 webhook 是否还在（200 + hooks 列表不含 expected_wh = webhook 丢失，清理）
       串行避免 GitCode API 限流返回空列表导致误判。

    只检查当前用户名下的托管仓库（不查其他用户的），减少 API 调用量。
    在 list_member_repos（前端托管页面入口）调用，确保打开页面时数据是最新的。
    """
    import concurrent.futures, urllib.request, urllib.error, json as _json
    all_repos = user_db.get_managed_repos(user_id)
    if not all_repos or not api_token:
        return

    headers = {"Accept": "application/json", "Authorization": f"Bearer {api_token}"}

    # 第一步：并发查仓库是否存在（404 = 已删除）
    def _check_exists(item):
        r = item
        owner = r.get("repo_owner", "")
        repo_name = r.get("repo_name", "")
        if not owner or not repo_name:
            return None
        repo_key = r["repo_key"]
        url = f"https://api.gitcode.com/api/v5/repos/{owner}/{repo_name}"
        try:
            req = urllib.request.Request(url, headers=headers, method="GET")
            with urllib.request.urlopen(req, timeout=5) as resp:
                pass  # 200 = 仓库存在
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return ("deleted", repo_key, r.get("repo_owner", ""), r.get("repo_name", ""))
        except Exception:
            pass  # 超时/网络抖动，不判定
        return None

    check_items = list(all_repos)
    stale_keys = []
    existing_repos = []  # 仓库存在（200）的，需要进一步查 webhook

    with concurrent.futures.ThreadPoolExecutor(max_workers=10) as executor:
        futures = [executor.submit(_check_exists, item) for item in check_items]
        for f in concurrent.futures.as_completed(futures, timeout=15):
            try:
                result = f.result(timeout=5)
                if result and result[0] == "deleted":
                    stale_keys.append((result[1], result[2], result[3]))
            except Exception:
                pass

    # 收集仓库存在（200）的记录，用于串行查 webhook
    deleted_keys = {sk[0] for sk in stale_keys}
    for r in all_repos:
        if r["repo_key"] not in deleted_keys:
            existing_repos.append(r)

    # 第二步：串行查 webhook（只查仓库存在且有 webhook_id 的）
    for r in existing_repos:
        expected_wh = r.get("webhook_id", 0) or 0
        if not expected_wh:
            continue
        owner = r.get("repo_owner", "")
        repo_name = r.get("repo_name", "")
        if not owner or not repo_name:
            continue
        repo_key = r["repo_key"]
        url = f"https://api.gitcode.com/api/v5/repos/{owner}/{repo_name}/hooks"
        try:
            req = urllib.request.Request(url, headers=headers, method="GET")
            with urllib.request.urlopen(req, timeout=5) as resp:
                hooks = _json.loads(resp.read().decode() or "[]")
            hook_ids = {h.get("id") for h in hooks if isinstance(h, dict)} if isinstance(hooks, list) else set()
            if expected_wh not in hook_ids:
                stale_keys.append((repo_key, owner, repo_name))
                log.info(f"managed repos: webhook {expected_wh} not found for {repo_key}, marking stale")
        except urllib.error.HTTPError as e:
            # 403 = 非 owner 无权限查 hooks，跳过（不误判）
            # 404 = 仓库在此刻被删了（刚才 200 现在变 404），算 stale
            if e.code == 404:
                stale_keys.append((repo_key, owner, repo_name))
            # 其他错误跳过
        except Exception:
            pass  # 超时/网络抖动，不判定

    for repo_key, owner, repo_name in stale_keys:
        log.info(f"managed repos: repo {repo_key} stale (deleted or webhook lost), auto-cleanup")
        _cleanup_repo_data(repo_key, owner, repo_name)
        user_db.remove_managed_repo(repo_key)


@router.get("/member")
def list_member_repos(user: dict = Depends(get_current_user)):
    """获取用户可操作仓库列表：优先从 user_repos 缓存读取；缓存为空或超过 TTL 时自动从 GitCode 拉取刷新

    加载前先检查托管仓库在 GitCode 上是否还存在（GitCode 删仓库不发 webhook，
    grape 收不到通知，旧记录残留导致重建同名仓库时前端误显"已托管"）。
    """
    from datetime import datetime, timezone

    api_token = get_user_token(user["username"])
    if not api_token:
        raise HTTPException(status_code=400, detail={"code": 400, "message": "GitCode API Token 未配置，请先通过 OAuth 登录或配置个人 Token"})

    # 异步探活清理失效托管（C2）：后台线程执行，接口不等待、页面保持秒开；
    # 失效（GitCode 已删/丢 webhook）的托管记录在后台被清掉，下一次刷新/进入时数字才校准。
    try:
        import threading
        threading.Thread(
            target=_probe_stale_repos,
            args=(api_token, user["id"]),
            daemon=True,
        ).start()
    except Exception as e:
        log.warning(f"member: background stale probe start failed: {e}")

    # 优先读取 DB 缓存（未过期）
    cached = user_db.get_user_repos(user["id"])
    synced_at = user_db.get_user_repos_synced_at(user["id"])

    def _is_expired() -> bool:
        if not synced_at:
            return True
        try:
            # SQLite datetime('now') 是 UTC "YYYY-MM-DD HH:MM:SS"
            t = datetime.strptime(synced_at, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
            age = (datetime.now(timezone.utc) - t).total_seconds()
            return age > REPOS_CACHE_TTL
        except Exception:
            return True

    if cached and not _is_expired():
        return {"repos": _filter_repos(user["id"], cached), "_cached": True}

    # 缓存为空或已过期 → 从 GitCode 拉取并刷新缓存
    try:
        repos = _fetch_and_cache(user["id"], api_token)
    except Exception as e:
        # 拉取失败但有旧缓存兜底（避免 GitCode 抖动导致页面空白）
        if cached:
            return {"repos": _filter_repos(user["id"], cached), "_cached": True, "_stale": True}
        raise HTTPException(status_code=502, detail={"code": 502, "message": f"加载仓库列表失败: {e}"})

    return {"repos": _filter_repos(user["id"], repos), "_cached": False}


@router.post("/sync")
def sync_repos(user: dict = Depends(get_current_user)):
    """手动从 GitCode 同步仓库列表，刷新 user_repos 缓存，并探活清理失效托管记录"""
    api_token = get_user_token(user["username"])
    if not api_token:
        raise HTTPException(status_code=400, detail={"code": 400, "message": "GitCode API Token 未配置"})

    _probe_stale_repos(api_token, user["id"])

    try:
        repos = _fetch_and_cache(user["id"], api_token)
    except Exception as e:
        raise HTTPException(status_code=502, detail={"code": 502, "message": f"同步失败: {e}"})

    return {"repos": _filter_repos(user["id"], repos), "message": "同步成功"}


@router.get("/check-name")
def check_repo_name(name: str, user: dict = Depends(get_current_user)):
    """检查当前用户名下是否已存在同名仓库"""
    from src.user.repo_manager import _req, get_user_gitcode_login
    api_token = get_user_token(user["username"])
    if not api_token:
        raise HTTPException(status_code=400, detail={"code": 400, "message": "GitCode API Token 未配置"})
    try:
        login = get_user_gitcode_login(api_token)
        if not login:
            raise HTTPException(status_code=502, detail={"code": 502, "message": "无法获取当前用户身份"})
        _req("GET", f"https://api.gitcode.com/api/v5/repos/{login}/{name}", api_token)
        return {"exists": True, "full_name": f"{login}/{name}"}
    except RuntimeError as e:
        if "404" in str(e) or "Not Found" in str(e):
            return {"exists": False}
        raise HTTPException(status_code=502, detail={"code": 502, "message": f"检查失败: {e}"})
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=502, detail={"code": 502, "message": f"检查失败: {e}"})


@router.post("/create")
def create_new_repo(body: CreateRepoBody, user: dict = Depends(get_current_user)):
    """通过 GitCode API 新建仓库，可选创建后自动托管"""
    import re as _re
    from src.repo_key import to_repo_key
    name = (body.name or "").strip()
    if not name:
        raise HTTPException(status_code=400, detail={"code": 400, "message": "仓库名称不能为空"})
    if not _re.match(r'^[\w\-\.]+$', name):
        raise HTTPException(status_code=400, detail={"code": 400, "message": "仓库名称只能包含字母、数字、下划线、连字符和点"})

    if body.visibility in ("private", "public"):
        private = (body.visibility == "private")
    else:
        private = body.private

    api_token = get_user_token(user["username"])
    if not api_token:
        raise HTTPException(status_code=400, detail={"code": 400, "message": "GitCode API Token 未配置，请先通过 OAuth 登录或配置个人 Token"})

    try:
        repo_info = create_repo(api_token=api_token, name=name, description=body.description, private=private)
    except Exception as e:
        raise HTTPException(status_code=502, detail={"code": 502, "message": f"创建仓库失败: {e}"})

    full_name = repo_info.get("full_name") or ""
    owner = repo_info.get("owner") or ""
    if isinstance(owner, dict):
        owner = owner.get("login", "")
    repo_name = repo_info.get("name") or name

    try:
        _fetch_and_cache(user["id"], api_token)
    except Exception:
        pass

    if body.host_after_create:
        from src.user.db import get_webhook_url
        webhook_url = get_webhook_url()
        if not webhook_url:
            raise HTTPException(status_code=400, detail={"code": 400, "message": "webhook_url 未配置，无法托管"})
        # 读取全局 bot token（托管时存入 managed_repos.bot_gitcode_token）
        _create_global_bot_token = ""
        try:
            import json as _json, os as _os2
            _gbt_path2 = _os2.path.join(_os2.path.dirname(__file__), "..", "..", "..", "configs", "global-bot-token.json")
            if _os2.path.exists(_gbt_path2):
                with open(_gbt_path2, "r", encoding="utf-8") as _f2:
                    _create_global_bot_token = _json.load(_f2).get("gitcode_api_token", "") or ""
        except Exception:
            pass
        try:
            existing_id = find_existing_webhook(api_token, owner, repo_name, webhook_url)
            repo_key = to_repo_key(full_name)
            if existing_id:
                user_db.add_managed_repo(repo_key, owner, repo_name, webhook_id=existing_id, gitcode_webhook_token=api_token, bot_gitcode_token=_create_global_bot_token)
                user_db.mark_user_repo_join(user["id"], repo_key, is_join=True, is_admin=True)
            else:
                hook_id = setup_webhook(api_token, owner, repo_name, webhook_url, webhook_password=api_token)
                user_db.add_managed_repo(repo_key, owner, repo_name, webhook_id=hook_id, gitcode_webhook_token=api_token, bot_gitcode_token=_create_global_bot_token)
                user_db.mark_user_repo_join(user["id"], repo_key, is_join=True, is_admin=True)
            from src.api.session_store import ensure_default_agent_sessions
            ensure_default_agent_sessions(repo_key)
        except Exception as e:
            return {"repo": repo_info, "full_name": full_name, "owner": owner, "name": repo_name, "hosted": False, "message": f"仓库已创建，但托管失败: {e}"}

        try:
            _fetch_and_cache(user["id"], api_token)
        except Exception:
            pass
        return {"repo": repo_info, "full_name": full_name, "owner": owner, "name": repo_name, "hosted": True, "message": "仓库创建并托管成功"}

    return {"repo": repo_info, "full_name": full_name, "owner": owner, "name": repo_name, "hosted": False, "message": "仓库创建成功"}


@router.post("/host")
def host_repo(body: RepoAction, user: dict = Depends(get_current_user)):
    from src.repo_key import to_repo_key
    parts = body.full_name.split("/", 1)
    if len(parts) != 2:
        raise HTTPException(status_code=400, detail={"code": 400, "message": "无效的仓库标识"})
    owner, repo_name = parts

    api_token = get_user_token(user["username"])
    from src.user.db import get_webhook_url
    webhook_url = get_webhook_url()

    if not api_token:
        raise HTTPException(status_code=400, detail={"code": 400, "message": "GitCode API Token 未配置"})

    # 读取全局 bot 配置（configs/global-bot-token.json）
    global_bot_token = ""
    global_bot_username = ""
    try:
        import json, os as _os
        _gbt_path = _os.path.join(_os.path.dirname(__file__), "..", "..", "..", "configs", "global-bot-token.json")
        if _os.path.exists(_gbt_path):
            with open(_gbt_path, "r", encoding="utf-8") as _f:
                _gbt = json.load(_f)
            global_bot_token = _gbt.get("gitcode_api_token", "") or ""
            _us = _gbt.get("bot_usernames") or []
            global_bot_username = _us[0] if _us else ""
    except Exception:
        pass

    # webhook 统一用全局 bot token 创建/查（方案 C：webhook 归 bot 管辖，个人 token 避免限流）
    if not global_bot_token:
        raise HTTPException(status_code=503, detail={"code": 503, "message": "全局 Bot Token 未配置，无法创建 webhook（请联系管理员检查 configs/global-bot-token.json）"})
    webhook_token = global_bot_token

    from src.user.repo_manager import _check_collaborator_admin, _req
    try:
        user_data = _req("GET", f"https://api.gitcode.com/api/v5/user", api_token)
        current_user = user_data.get("login", "")
        if not current_user:
            raise HTTPException(status_code=502, detail={"code": 502, "message": "无法获取当前用户身份"})

        if current_user == owner:
            is_admin = True
        else:
            is_admin = _check_collaborator_admin(api_token, owner, repo_name, current_user)

        if not is_admin:
            raise HTTPException(status_code=403, detail={"code": 403, "message": "无权限托管，请联系管理员"})
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=502, detail={"code": 502, "message": f"权限验证失败: {e}"})

    try:
        from src.user.repo_manager import add_collaborator, accept_invite_for_repo, get_member_permission, _RateLimitedError
        repo_key = to_repo_key(body.full_name)
        from src.user.db import set_host_status

        def _mark(status: str):
            try:
                set_host_status(repo_key, status)
            except Exception as _e:
                log.warning(f"host_repo: set host_status={status} failed: {_e}")

        # 1) 邀请 bot 为维护者（owner token 必需）→ 2) bot 接受 → 3) bot 自查权限
        # 原则：凡能查/能建一律 bot token；owner token 仅 L363 身份确认 + 本步邀请
        if api_token and global_bot_username and global_bot_token:
            _mark("partial")
            ok = add_collaborator(api_token, owner, repo_name, global_bot_username, "maintainer")
            if not ok:
                # 兜底：add_collaborator 失败可能是已成员/已邀请，用 bot token 自查确认
                try:
                    bot_perm = get_member_permission(global_bot_token, owner, repo_name, global_bot_username)
                    access_level = (bot_perm or {}).get("access_level", 0) or 0
                except _RateLimitedError as _rl:
                    log.warning(f"host: perm check rate-limited (non-fatal), mark bot_missing: {_rl}")
                    _mark("bot_missing")
                    raise HTTPException(status_code=503, detail={"code": 503, "message": "托管暂时失败：GitCode API 调用过于频繁，请稍后重试"})
                if access_level >= 40:
                    log.info(f"host: add_collaborator failed but bot {global_bot_username} already maintainer (access_level={access_level}) in {owner}/{repo_name}, continue")
                else:
                    _mark("bot_missing")
                    raise HTTPException(status_code=502, detail={"code": 502, "message": f"托管失败: 无法将全局 Bot（{global_bot_username}）加入仓库维护者，请手动在仓库成员中添加"})

            # bot 接受邀请
            if not accept_invite_for_repo(global_bot_token, owner, repo_name):
                log.warning(f"host: no invite to accept for {owner}/{repo_name}, bot may already be member")

            # 校验 bot 已是维护者（access_level>=40）——用 bot token；限流/失败允许降级继续（方案 B）
            try:
                bot_perm = get_member_permission(global_bot_token, owner, repo_name, global_bot_username)
                access_level = (bot_perm or {}).get("access_level", 0) or 0
                if access_level < 40:
                    role_name = (bot_perm or {}).get("role_name", "")
                    _mark("bot_missing")
                    log.warning(f"host: bot perm check inconclusive (level={access_level}), invite path succeeded, mark bot_missing")
                    raise HTTPException(status_code=502, detail={"code": 502, "message": f"托管部分完成: 全局 Bot（{global_bot_username}）权限={role_name or f'level {access_level}'}，未成为仓库维护者（Maintainer）。请在 GitCode 仓库成员中将 {global_bot_username} 权限设为维护者，或点击页面提示的「重检」自动修复"})
            except _RateLimitedError as _rl:
                # 限流：权限未判定，继续 webhook（不阻塞），host_status 待 dashboard 复查
                log.warning(f"host: perm check rate-limited (continue): {_rl}")

        # 4) webhook：统一用 bot token 查/建（方案 C）
        existing_id = find_existing_webhook(webhook_token, owner, repo_name, webhook_url)
        if existing_id:
            user_db.add_managed_repo(repo_key, owner, repo_name, webhook_id=existing_id, gitcode_webhook_token=webhook_token, bot_gitcode_token=global_bot_token, bot_username=global_bot_username)
            user_db.mark_user_repo_join(user["id"], repo_key, is_join=True, is_admin=True)
            from src.api.session_store import ensure_default_agent_sessions
            ensure_default_agent_sessions(repo_key)
            _mark("")
            try:
                from src.api.pipeline_config import _auto_fork_official_scenes_to_repo
                _auto_fork_official_scenes_to_repo(repo_key, user.get("id", 0))
            except Exception as e:
                log.warning(f"host_repo: auto-fork scenes failed: {e}")
            return {"message": "仓库已托管（复用已有 webhook）", "webhook_id": existing_id}

        hook_id = setup_webhook(webhook_token, owner, repo_name, webhook_url, webhook_password=webhook_token)
        # 清理同名旧仓库残留的 DB 数据（同上）
        from src.api.onboarding import cleanup_stale_repo_data
        cleanup_stale_repo_data(repo_key)
        user_db.add_managed_repo(repo_key, owner, repo_name, webhook_id=hook_id, gitcode_webhook_token=webhook_token, bot_gitcode_token=global_bot_token, bot_username=global_bot_username)
        user_db.mark_user_repo_join(user["id"], repo_key, is_join=True, is_admin=True)
        from src.api.session_store import ensure_default_agent_sessions
        ensure_default_agent_sessions(repo_key)
        _mark("")
        try:
            from src.api.pipeline_config import _auto_fork_official_scenes_to_repo
            _auto_fork_official_scenes_to_repo(repo_key, user.get("id", 0))
        except Exception as e:
            log.warning(f"host_repo: auto-fork scenes failed: {e}")
        return {"message": "仓库托管成功", "webhook_id": hook_id}
    except HTTPException:
        raise
    except Exception as e:
        # 兜底：失败但 webhook 可能已建，标记状态避免死循环，仍可进入项目
        try:
            _rk = to_repo_key(body.full_name)
            _mark("partial")
        except Exception:
            pass
        raise HTTPException(status_code=502, detail={"code": 502, "message": f"托管失败: {e}"})


@router.post("/{owner}/{repo}/repair-host")
def repair_host(owner: str, repo: str, user: dict = Depends(get_current_user)):
    """方案 F-4：自愈未完成的托管流程（bot 未加入 / webhook 未创建），幂等。

    流程：需要 owner token 邀请 + bot token 建 webhook；判定 bot 已是维护者则跳过邀请；
    返回修复后的 host_status。
    """
    from src.repo_key import to_repo_key
    from src.user.repo_manager import add_collaborator, accept_invite_for_repo, get_member_permission, find_existing_webhook, setup_webhook, _RateLimitedError
    from src.user.db import get_global_bot_credential, get_webhook_url, set_host_status

    repo_key = to_repo_key(f"{owner}/{repo}")
    api_token = get_user_token(user["username"])
    bot_token, bot_username = get_global_bot_credential()
    if not api_token or not bot_token:
        raise HTTPException(status_code=400, detail={"code": 400, "message": "缺少 API Token，无法自愈"})
    webhook_url = get_webhook_url()

    # 1) bot 是否已是维护者（bot token 自查）
    bot_ok = False
    try:
        perm = get_member_permission(bot_token, owner, repo, bot_username)
        bot_ok = bool(perm and (perm.get("access_level") or 0) >= 40)
    except _RateLimitedError:
        raise HTTPException(status_code=503, detail={"code": 503, "message": "GitCode API 调用过于频繁，请稍后重试"})

    if not bot_ok:
        # 邀请 + 接受
        add_collaborator(api_token, owner, repo, bot_username, "maintainer")
        accept_invite_for_repo(bot_token, owner, repo)
        try:
            perm2 = get_member_permission(bot_token, owner, repo, bot_username)
            bot_ok = bool(perm2 and (perm2.get("access_level") or 0) >= 40)
        except _RateLimitedError:
            bot_ok = False
        if not bot_ok:
            set_host_status(repo_key, "bot_missing")
            return {"status": "error", "host_status": "bot_missing",
                    "message": f"未能自动将 {bot_username} 加入为维护者，请手动在仓库成员中添加"}

    # 2) webhook 是否存在，缺失则补建（bot token）
    hid = None
    try:
        hid = find_existing_webhook(bot_token, owner, repo, webhook_url)
    except Exception:
        hid = None
    if not hid:
        hid = setup_webhook(bot_token, owner, repo, webhook_url, webhook_password=bot_token)
    if not hid:
        set_host_status(repo_key, "webhook_missing")
        return {"status": "error", "host_status": "webhook_missing",
                "message": "未能创建 webhook，请检查 bot 权限后重试"}

    # 3) 完成 → 回写 webhook_id（避免方案 X 下 webhook_id=0 反复误报）→ 清状态
    try:
        from src.user.db import add_managed_repo
        add_managed_repo(repo_key, owner, repo, webhook_id=hid,
                         gitcode_webhook_token=bot_token, bot_gitcode_token=bot_token, bot_username=bot_username)
    except Exception as _e:
        log.warning(f"repair_host: write back webhook_id failed: {_e}")
    set_host_status(repo_key, "")
    return {"status": "ok", "host_status": "", "webhook_id": hid,
            "message": "托管已修复（bot 已加入成员，webhook 已就绪）"}


@router.get("/managed")
def list_managed_repos(user: dict = Depends(get_current_user)):
    repos = user_db.get_managed_repos(user["id"])
    return {"repos": [{
        "full_name": r["full_name"],
        "role": "Admin" if r["is_admin"] else "Member",
        "webhook_ok": bool(r["webhook_id"]),
        "branch": "main",
        "status": "Active" if r["is_admin"] else "Joined",
        "has_bot_token": bool(r.get("bot_gitcode_token", "")),
    } for r in repos]}


class BotTokenBody(BaseModel):
    full_name: str
    bot_token: str = ""


@router.get("/bot-token/{owner}/{repo_name}")
def get_repo_bot_token(owner: str, repo_name: str, user: dict = Depends(get_current_user)):
    """获取仓库绑定的 DevOps Bot Token 是否存在"""
    token = get_bot_token_by_repo(owner, repo_name)
    return {"has_bot_token": bool(token)}


@router.put("/bot-token")
def set_repo_bot_token(body: BotTokenBody, user: dict = Depends(get_current_user)):
    """设置仓库绑定的 DevOps Bot Token；自动将 repo bot 加入维护者"""
    from src.repo_key import to_repo_key
    parts = body.full_name.split("/", 1)
    if len(parts) != 2:
        raise HTTPException(status_code=400, detail={"code": 400, "message": "无效的仓库标识"})
    owner, repo_name = parts
    repo_key = to_repo_key(body.full_name)
    ok = update_managed_repo_bot_token(repo_key, body.bot_token)
    if not ok:
        raise HTTPException(status_code=404, detail={"code": 404, "message": "仓库未找到"})

    # 用新 token 获取 GitCode 真实登录名，更新 bot_username
    repo_bot_username = ""
    try:
        from src.user.repo_manager import _req
        user_data = _req("GET", "https://api.gitcode.com/api/v5/user", body.bot_token)
        repo_bot_username = user_data.get("login", "") or ""
    except Exception:
        pass
    if repo_bot_username:
        update_managed_repo_bot_username(repo_key, repo_bot_username)

    # 将 repo bot 加入仓库维护者（用 owner token，失败仅记日志，不影响托管）
    if body.bot_token and repo_bot_username:
        try:
            from src.user.repo_manager import add_collaborator
            owner_token = get_user_token(user["username"])
            if owner_token:
                add_collaborator(owner_token, owner, repo_name, repo_bot_username, "maintainer")
        except Exception:
            pass
    return {"message": "Bot Token 已更新", "bot_username": repo_bot_username}


class DeployUrlBody(BaseModel):
    full_name: str
    deploy_url: str = ""


@router.put("/deploy-url")
def set_deploy_url(body: DeployUrlBody, user: dict = Depends(get_current_user)):
    """登记业务系统部署后的访问 URL"""
    from src.repo_key import to_repo_key
    from src.session_manager import db, db_lock
    repo_key = to_repo_key(body.full_name)
    with db_lock:
        c = db.cursor()
        c.execute("UPDATE managed_repos SET deploy_url=? WHERE repo_key=?", (body.deploy_url or "", repo_key))
        if c.rowcount == 0:
            raise HTTPException(status_code=404, detail={"code": 404, "message": "仓库未找到"})
        db.commit()
    return {"message": "部署 URL 已更新", "deploy_url": body.deploy_url}


@router.get("/deploy-url/{owner}/{repo_name}")
def get_deploy_url(owner: str, repo_name: str, user: dict = Depends(get_current_user)):
    """获取仓库的部署访问 URL"""
    from src.repo_key import to_repo_key
    from src.session_manager import db, db_lock
    repo_key = to_repo_key(f"{owner}/{repo_name}")
    with db_lock:
        c = db.cursor()
        c.execute("SELECT deploy_url FROM managed_repos WHERE repo_key=?", (repo_key,))
        row = c.fetchone()
    return {"deploy_url": (row[0] if row else "") or ""}


def _cleanup_repo_data(repo_key: str, owner: str, repo: str) -> dict:
    """解除托管时的同步清理：关联数据表 + 仓库 skill 目录 + workspace 目录。

    repo_key 为 owner/repo（无前缀）；兼容历史可能存在的 gitcode: 前缀数据。
    用户维度数据（workspaces/chat/{user_id}、workspaces/{owner} 根）不清理。
    返回清理统计。
    """
    import re, shutil, os
    from src.session_manager import db, db_lock

    stats = {"tables": 0, "skill_dirs": 0, "workspace_dirs": 0}
    keys = [repo_key]
    if not repo_key.startswith("gitcode:"):
        keys.append(f"gitcode:{repo_key}")
    if repo_key.startswith("gitcode:"):
        keys.append(repo_key[8:])
    keys = list(dict.fromkeys(k for k in keys if k))

    # 路径安全校验：owner/repo 仅允许字母数字、点、下划线、连字符（防路径穿越）
    if not re.match(r'^[\w.\-]+$', owner) or not re.match(r'^[\w.\-]+$', repo):
        log.warning(f"Cleanup skipped: invalid owner/repo {owner}/{repo}")
        return stats

    with db_lock:
        c = db.cursor()
        try:
            # 1. vod_conversations（依赖 devops_agent_session.id，需在删 agent_session 前）
            placeholders = ",".join("?" for _ in keys)
            c.execute(f"DELETE FROM vod_conversations WHERE agent_session_id IN (SELECT id FROM devops_agent_session WHERE repo_key IN ({placeholders}))", keys)
            stats["tables"] += c.rowcount
            # 2. chat_messages（依赖 chat_conversations.id，需在删 conversations 前）
            c.execute(f"SELECT id FROM chat_conversations WHERE repo_key IN ({placeholders})", keys)
            conv_ids = [r[0] for r in c.fetchall()]
            if conv_ids:
                cid_ph = ",".join("?" for _ in conv_ids)
                c.execute(f"DELETE FROM chat_messages WHERE conversation_id IN ({cid_ph})", conv_ids)
                stats["tables"] += c.rowcount
            # 3. 直接按 repo_key 匹配的表
            for tbl in ("events", "event_skipped", "chat_conversations", "chat_agent_session",
                        "repo_im_group_binding", "pipeline_instances", "devops_agent_session",
                        "issue_agent_session"):
                try:
                    c.execute(f"DELETE FROM {tbl} WHERE repo_key IN ({placeholders})", keys)
                    stats["tables"] += c.rowcount
                except Exception as e:
                    log.warning(f"Cleanup {tbl} failed: {e}")
            # 4. 仓库 skill 记录
            try:
                c.execute("DELETE FROM skill WHERE scope='repo' AND owner_id IN ({})".format(",".join("?" for _ in keys)), keys)
                stats["tables"] += c.rowcount
            except Exception as e:
                log.warning(f"Cleanup skill records failed: {e}")
            # 4.5 onboarding 幂等标记（删仓库即作废，重建同名仓库应重新引导）
            #     标记 key 形如 `onboarded:{repo_key}`（repo_key 可能带/不带 gitcode: 前缀）
            try:
                _ob_keys = [f"onboarded:{k}" for k in keys]
                _ob_ph = ",".join("?" for _ in _ob_keys)
                c.execute(f"DELETE FROM meta WHERE `key` IN ({_ob_ph})", _ob_keys)
                if c.rowcount:
                    log.info(f"Cleanup onboarding marks: {c.rowcount} removed ({_ob_keys})")
            except Exception as e:
                log.warning(f"Cleanup onboarding marks failed: {e}")
            db.commit()
        except Exception as e:
            log.warning(f"Cleanup DB failed: {e}")

    # 5. 仓库 skill 目录（skills/repo/{repo_key}/）
    try:
        from src.user.db import SKILL_DIR
        _repo_skills_base = os.path.join(os.path.dirname(SKILL_DIR), "repo")
        for k in keys:
            _dir = os.path.join(_repo_skills_base, k)
            if os.path.isdir(_dir):
                shutil.rmtree(_dir)
                stats["skill_dirs"] += 1
    except Exception as e:
        log.warning(f"Cleanup skill dir failed: {e}")

    # 6. workspace 目录（data/workspaces/...）
    try:
        from src.config import WORKSPACE_DIR
        _ws_base = WORKSPACE_DIR
        # 所有候选目录（规范化后校验必须在 WORKSPACE_DIR 下）
        candidates = [
            os.path.join(_ws_base, owner, repo),            # agent 执行目录
            os.path.join(_ws_base, repo),                   # vod-agent 平级目录
            os.path.join(_ws_base, "feishu", f"{owner}_{repo}"),
            os.path.join(_ws_base, "vod", f"{owner}_{repo}"),
            os.path.join(_ws_base, "history", owner, repo),
            os.path.join(_ws_base, "history", f"gitcode:{owner}", repo),
        ]
        for _dir in candidates:
            _abs = os.path.normpath(_dir)
            if os.path.isdir(_abs) and _abs.startswith(os.path.normpath(_ws_base) + os.sep):
                shutil.rmtree(_abs)
                stats["workspace_dirs"] += 1
    except Exception as e:
        log.warning(f"Cleanup workspace dir failed: {e}")

    log.info(f"Cleanup repo {repo_key}: {stats}")
    return stats


@router.post("/remove")
def remove_managed_repo(body: RepoAction, user: dict = Depends(get_current_user)):
    from src.repo_key import to_repo_key
    parts = body.full_name.split("/", 1)
    if len(parts) != 2:
        raise HTTPException(status_code=400, detail={"code": 400, "message": "无效的仓库标识"})
    owner, repo_name = parts
    repo_key = to_repo_key(body.full_name)

    # 删除 webhook 优先个人 token（用户已授权全权限）；repo bot / 全局 bot token 兜底
    api_token = get_user_token(user["username"])
    if not api_token:
        api_token = get_bot_token(repo_owner=owner, repo_name=repo_name)
    if not api_token:
        try:
            import json as _json, os as _os
            _gbt_path = _os.path.join(_os.path.dirname(__file__), "..", "..", "..", "configs", "global-bot-token.json")
            if _os.path.exists(_gbt_path):
                with open(_gbt_path, "r", encoding="utf-8") as _f:
                    api_token = _json.load(_f).get("gitcode_api_token", "") or ""
        except Exception:
            pass

    if api_token:
        try:
            from src.user.repo_manager import _get_managed_webhook, remove_webhook
            wh_id = _get_managed_webhook(repo_key)
            if wh_id:
                remove_webhook(api_token, owner, repo_name, wh_id)
        except Exception:
            pass

    # 解除托管：移除全局 bot 与 repo bot 的项目维护者身份（失败仅记日志）
    try:
        import json, os as _os
        from src.user.repo_manager import remove_collaborator
        # 读取该仓库配置的 repo bot（先取，再删 managed_repos）
        repo_bot_token = ""
        repo_bot_username = ""
        for _r in user_db.get_managed_repos(user["id"]):
            if _r["repo_key"] == repo_key:
                repo_bot_token = _r.get("bot_gitcode_token", "") or ""
                repo_bot_username = _r.get("bot_username", "") or ""
                break
        # 全局 bot 用户名（移除用 owner token）
        _gbt_path = _os.path.join(_os.path.dirname(__file__), "..", "..", "..", "configs", "global-bot-token.json")
        global_bot_username = ""
        if _os.path.exists(_gbt_path):
            with open(_gbt_path, "r", encoding="utf-8") as _f:
                _gbt = json.load(_f)
            _us = _gbt.get("bot_usernames") or []
            global_bot_username = _us[0] if _us else ""
        # 移除协作者需个人 owner token（GitCode 拒绝 bot 操作自己；api_token 此时是全局 bot token 用于删 webhook）
        owner_token = get_user_token(user["username"])
        if owner_token and global_bot_username:
            try:
                _rm_ok = remove_collaborator(owner_token, owner, repo_name, global_bot_username)
                if not _rm_ok:
                    log.warning(f"Remove global bot collaborator failed (perm?): {global_bot_username} @ {owner}/{repo_name}")
            except Exception as e:
                log.warning(f"Remove global bot collaborator error: {e}")
        # repo bot（若配置且非全局 bot）
        if owner_token and repo_bot_username and repo_bot_username != global_bot_username:
            try:
                _rm_ok2 = remove_collaborator(owner_token, owner, repo_name, repo_bot_username)
                if not _rm_ok2:
                    log.warning(f"Remove repo bot collaborator failed (perm?): {repo_bot_username} @ {owner}/{repo_name}")
            except Exception as e:
                log.warning(f"Remove repo bot collaborator error: {e}")
    except Exception:
        pass

    removed = user_db.remove_managed_repo(repo_key)
    if not removed:
        raise HTTPException(status_code=404, detail={"code": 404, "message": "仓库未找到"})
    # 同步清理：关联数据表 + 仓库 skill + workspace 目录（替代原 remove_agent_sessions）
    _cleanup_repo_data(body.full_name, owner, repo_name)
    return {"message": "已移除仓库"}


@router.get("/{owner}/{repo}/project-info")
def get_repo_project_info(owner: str, repo: str, user: dict = Depends(get_current_user)):
    """读取仓库的权威项目知识基线（repo 全局，多机共享，见 multi-issue-isolation-design.md §2.4）。

    返回 files（全文）+ meta（commit/branches/updated/degraded）。无基线 → 空 files + meta。
    """
    from src.repo_key import to_repo_key
    from src.runtime.service import load_project_info
    repo_key = to_repo_key(f"{owner}/{repo}")
    files, meta = load_project_info(repo_key)
    return {"repo_key": repo_key, "files": files, "meta": meta}
