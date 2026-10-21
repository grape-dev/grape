"""仓库路由：列表、托管、移除、同步 + git 平台凭据/工作区（方案③ CLI 支撑）"""
import os
from fastapi import APIRouter, HTTPException, Depends, Request
from pydantic import BaseModel
from src.config import log, WORKSPACE_DIR
from src.user import db as user_db
from src.user.repo_manager import fetch_repos_with_role, setup_webhook, find_existing_webhook, create_repo
from src.api.deps import get_current_user_or_api_key, get_bot_token, get_user_token, get_user_token_with_refresh, get_user_github_token_with_refresh, require_no_session_token, require_org_member
from src.api.oauth_token import with_oauth_token
from src.api.org_context import resolve_org_context
from src.user.db import update_managed_repo_bot_token, update_managed_repo_bot_username, get_bot_token_by_repo

router = APIRouter(prefix="/api/repos", tags=["repos"])

# 仓库列表缓存有效期（秒）：超过后自动从 GitCode 重新拉取
REPOS_CACHE_TTL = 1800  # 30 分钟（避免频繁触发 GitCode N+1 权限查询）

# 失效托管探活的最小间隔（秒）：同用户短时间不重复全量探活（0 = 不节流）
REPO_PROBE_INTERVAL = int(os.environ.get("REPO_PROBE_INTERVAL", "300") or 0)

# 探活节流守卫（进程内；多实例各自节流，可接受）
_probe_guard = __import__("threading").Lock()
_last_probe_ts: dict[int, float] = {}


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
    provider: str = ""  # R26: 用户指定创建到哪个平台


def _get_user_provider_and_token(username: str, login_provider: str = "", user_id: int = 0) -> tuple[str, str]:
    """根据用户已绑定的令牌自动选择 provider，返回 (provider, token)。

    login_provider：登录来源平台（gitcode/github），优先返回对应平台 token；
    缺失或对应平台无 token 时 fallback 到现有优先级逻辑（github 优先）。
    user_id：传入时 GitCode token 用 get_user_token_with_refresh 预刷新过期 token。
    """
    if login_provider == "gitcode":
        gitcode_token = get_user_token_with_refresh(username, user_id)
        if gitcode_token:
            return "gitcode", gitcode_token
    elif login_provider == "github":
        github_token = get_user_github_token_with_refresh(username, user_id=user_id)
        if github_token:
            return "github", github_token
    github_token = get_user_github_token_with_refresh(username, user_id=user_id)
    if github_token:
        return "github", github_token
    gitcode_token = get_user_token_with_refresh(username, user_id)
    if gitcode_token:
        return "gitcode", gitcode_token
    return "", ""


def _provider_from_hint(full_name: str) -> str:
    """按「仓库来源」提示判断 provider：full_name 前缀或 managed_repos 已登记前缀。
    返回 'github' / 'gitcode' / ''（无来源线索）。这是托管路由的主要信息来源，
    避免再被「登录平台」短路误判（host-repo-provider-routing-fix-design.md）。
    """
    f = (full_name or "").strip()
    if f.startswith("github:"):
        return "github"
    if f.startswith("gitcode:"):
        return "gitcode"
    base = f.split(":", 1)[-1]  # 兼容已有前缀写法
    try:
        from src.session_manager import db, db_lock
        with db_lock:
            rows = db.cursor().execute(
                "SELECT repo_key FROM managed_repos WHERE repo_key IN (?, ?) LIMIT 1",
                ("github:" + base, "gitcode:" + base)).fetchall()
        for r in rows:
            if r[0].startswith("github:"):
                return "github"
            if r[0].startswith("gitcode:"):
                return "gitcode"
    except Exception:
        pass
    return ""


def _provider_from_token(user: dict) -> tuple[str, str]:
    """托管兜底：按「平台 token 是否可用」选择 provider（github 优先），
    不按登录平台短路（登录平台 ≠ 仓库平台，见 host-repo-provider-routing-fix-design.md）。"""
    gh = None
    try:
        gh = get_user_github_token_with_refresh(user["username"], user_id=user.get("id", 0))
    except Exception:
        gh = None
    if gh:
        return "github", gh
    gc = get_user_token_with_refresh(user["username"], user.get("id", 0))
    if gc:
        return "gitcode", gc
    return "", ""


def _fetch_and_cache(user_id: int, api_token: str, provider: str = "gitcode") -> list[dict]:
    """从 VCS 拉取仓库列表，返回结果（不写缓存——由调用方合并后一次性写入）"""
    if provider == "github":
        from src.user.repo_manager import fetch_github_repos_with_role
        repos = fetch_github_repos_with_role(api_token)
        for r in repos:
            if not r.get("repo_key"):
                r["repo_key"] = f"github:{r.get('full_name', '')}"
    else:
        repos = fetch_repos_with_role(api_token)
        for r in repos:
            if not r.get("repo_key"):
                r["repo_key"] = f"gitcode:{r.get('full_name', '')}"
    return repos


def _filter_repos(user_id: int, repos: list[dict], org_id: int = 0) -> list[dict]:
    """过滤仓库列表：返回用户身份可见的全部仓库（管理员 + 非管理员成员）。

    org_id > 0 时组织隔离：
    - 已托管到当前组织的仓库 → 显示，is_hosted=true
    - 已托管到其他组织的仓库 → 隐藏（隔离）
    - 未托管的仓库 → 显示，用户可托管到当前组织
    """
    from src.repo_key import to_repo_key
    # 获取所有托管仓库及其 org_id
    from src.session_manager import db as _db, db_lock as _dbl
    managed = {}
    managed_org = {}
    with _dbl:
        _c = _db.cursor()
        try:
            _c.execute("SELECT repo_key, webhook_id, org_id FROM managed_repos")
            for _r in _c.fetchall():
                managed[_r[0]] = _r[1]
                managed_org[_r[0]] = _r[2] or 0
        except Exception:
            # 降级：不带 org_id
            for r in user_db.get_all_managed_repos():
                managed[r["repo_key"]] = r["webhook_id"]
                managed_org[r["repo_key"]] = 0
    result = []
    for r in repos:
        full_name = r.get("full_name", "")
        repo_key = to_repo_key(r.get("repo_key", "") or full_name)
        is_admin = r.get("is_admin", False)
        in_managed = repo_key in managed
        hosted = in_managed and bool(managed.get(repo_key))
        repo_org_id = managed_org.get(repo_key, 0)

        # 组织隔离：
        # - org_id > 0（非个人组织）：已托管到其他组织的仓库不显示
        # - org_id = 0（个人组织）：已托管到其他非个人组织的仓库不显示
        #   （个人组织只显示全局托管 + 自身托管的仓库，不显示其他非个人组织的仓库）
        # D42: 组织已删除时（org 不存在），不过滤该仓库（降级为未托管状态）
        if in_managed and repo_org_id:
            if org_id and repo_org_id != org_id:
                continue
            if not org_id:
                # 个人组织：检查 repo_org_id 对应的组织是否是个人组织
                from src.user.org_db import is_org_personal as _is_p
                if not _is_p(repo_org_id):
                    # D42: 检查组织是否仍然存在——已删除的组织不隔离
                    from src.user.org_db import get_org as _get_org
                    if _get_org(repo_org_id):
                        continue

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
    """检查当前用户名下的托管仓库在 VCS 上的状态，清理失效的托管记录。

    两步检查：
    1. 并发查仓库是否存在（404 = 已删除，立即清理）
    2. 仓库存在的，串行查 webhook 是否还在（200 + hooks 列表不含 expected_wh = webhook 丢失，清理）
       串行避免 API 限流返回空列表导致误判。

    只检查当前用户名下的托管仓库（不查其他用户的），减少 API 调用量。
    在 list_member_repos（前端托管页面入口）调用，确保打开页面时数据是最新的。

    GitHub 仓库走 GitHub API + 用户 github_api_token；GitCode 仓库走 GitCode API + 传入 api_token。
    """
    import concurrent.futures, urllib.request, urllib.error, json as _json
    from src.repo_key import parse_repo_key

    all_repos = user_db.get_managed_repos(user_id)
    if not all_repos:
        return

    # 按 provider 分组：GitHub 仓库需要单独的 token
    github_token = ""
    try:
        from src.api.deps import get_user_github_token_with_refresh
        from src.auth.grape_account_db import get_grape_account_by_id
        acc = get_grape_account_by_id(user_id) or {}
        github_token = get_user_github_token_with_refresh(acc.get("username", ""), user_id=user_id)
    except Exception:
        pass

    gitcode_headers = {"Accept": "application/json", "Authorization": f"Bearer {api_token}"} if api_token else {}
    github_headers = {
        "Accept": "application/vnd.github+json",
        "Authorization": f"Bearer {github_token}",
        "User-Agent": "Grape-DevOps",
    } if github_token else {}

    # 第一步：并发查仓库是否存在（404 = 已删除）
    def _check_exists(item):
        r = item
        owner = r.get("repo_owner", "")
        repo_name = r.get("repo_name", "")
        if not owner or not repo_name:
            return None
        repo_key = r["repo_key"]
        provider, _, _ = parse_repo_key(repo_key)
        if provider == "github":
            if not github_token:
                return None  # 无 GitHub token，跳过检查（不误判）
            url = f"https://api.github.com/repos/{owner}/{repo_name}"
            hdrs = github_headers
        else:
            if not api_token:
                return None
            url = f"https://gitcode.com/api/v5/repos/{owner}/{repo_name}"
            hdrs = gitcode_headers
        try:
            req = urllib.request.Request(url, headers=hdrs, method="GET")
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
        provider, _, _ = parse_repo_key(repo_key)
        if provider == "github":
            if not github_token:
                continue
            url = f"https://api.github.com/repos/{owner}/{repo_name}/hooks"
            hdrs = github_headers
        else:
            url = f"https://gitcode.com/api/v5/repos/{owner}/{repo_name}/hooks"
            hdrs = gitcode_headers
        try:
            req = urllib.request.Request(url, headers=hdrs, method="GET")
            with urllib.request.urlopen(req, timeout=5) as resp:
                hooks = _json.loads(resp.read().decode() or "[]")
            hook_ids = {h.get("id") for h in hooks if isinstance(h, dict)} if isinstance(hooks, list) else set()
            if expected_wh not in hook_ids:
                stale_keys.append((repo_key, owner, repo_name))
                log.debug(f"managed repos: webhook {expected_wh} not found for {repo_key}, marking stale")
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
        # Issue #340：级联清空全部关联数据（含需求中心 Issue 卡片/流水线/场景关联）
        from src.repo_cleanup import cascade_cleanup_repo_data, RepoCleanupError
        try:
            cascade_cleanup_repo_data(repo_key, owner, repo_name, trigger="polling")
        except RepoCleanupError as e:
            log.warning(f"managed repos: cascade cleanup {repo_key} failed (rolled back): {e}")
            continue  # 保留 managed 记录，下次探活重试
        user_db.remove_managed_repo(repo_key)


def _start_stale_probe_async(user: dict, providers: list[str]) -> None:
    """异步启动失效托管探活（fire-and-forget + 节流）。

    - 节流：同用户在 REPO_PROBE_INTERVAL 内不重复全量探测；
    - 异步：token 获取与 _probe_stale_repos 全部在后台线程完成，
      请求路径不参与 as_completed / 不 join，不因探活阻塞接口返回。
    """
    import time as _time
    if "gitcode" not in providers:
        return  # 探活目前只走 gitcode token（GitHub 仓在函数内部按需解析）
    uid = user.get("id", 0) or 0
    if not uid:
        return
    interval = REPO_PROBE_INTERVAL
    with _probe_guard:
        last = _last_probe_ts.get(uid, 0.0)
        now = _time.time()
        if interval > 0 and (now - last) < interval:
            return  # 节流：短时间不重复
        _last_probe_ts[uid] = now

    def _run():
        try:
            token = with_oauth_token(user, "gitcode", lambda t: t)
        except Exception as e:
            log.warning(f"member: stale probe token fetch failed (non-fatal): {e}")
            return
        if not token:
            return
        try:
            _probe_stale_repos(token, uid)
        except Exception as e:
            log.warning(f"member: stale probe failed (non-fatal): {e}")

    import threading
    threading.Thread(target=_run, daemon=True).start()


@router.get("/member", operation_id="repos_member", summary="List repos I can operate", description="Lists repositories the current user can operate. Reads the user_repos cache, auto-refreshing from VCS when empty or stale; prunes managed repos removed upstream. Supports ?provider=gitcode|github to filter by platform (需求 17 项目页按平台切换).")
def list_member_repos(user: dict = Depends(require_org_member), provider: str = ""):
    """获取用户可操作仓库列表：优先从 user_repos 缓存读取；缓存为空或超过 TTL 时自动从 VCS 拉取刷新

    需求 17: 支持 ?provider=gitcode|github 按平台过滤。
    不传 provider 时拉取用户绑定的全部平台仓库（合并返回），不再只拉一个平台。

    加载前先检查托管仓库在 GitCode 上是否还存在（GitCode 删仓库不发 webhook，
    grape 收不到通知，旧记录残留导致重建同名仓库时前端误显"已托管"）。
    """
    from datetime import datetime

    # Issue #343 修复: 不再预获取 token（with_oauth_token 内部获取+预刷新），
    # 只需确定用户绑定了哪些平台
    from src.auth.grape_account_db import get_user_identities
    _identities = get_user_identities(user["id"])
    _bound_providers = {i["provider"] for i in _identities if i.get("provider") in ("gitcode", "github")}
    if not _bound_providers:
        raise HTTPException(status_code=400, detail={"code": 400, "message": "API Token 未配置，请先通过 OAuth 登录或配置个人 Token"})

    # 需求 17: 前端显式指定 provider 时只拉该平台
    if provider in ("gitcode", "github"):
        if provider not in _bound_providers:
            raise HTTPException(status_code=400, detail={"code": 400, "message": f"{provider} API Token 未配置，请先绑定 {provider} 平台"})
        _providers = [provider]
    else:
        _providers = list(_bound_providers)

    # 优先读取 DB 缓存（未过期）——按组织过滤
    org_id = user.get("org_id", 0)
    _is_personal_org = False
    if org_id:
        from src.user.org_db import is_org_personal
        _is_personal_org = is_org_personal(org_id)
    _filter_org_id = 0 if _is_personal_org else org_id
    cached = user_db.get_user_repos(user["id"])
    synced_at = user_db.get_user_repos_synced_at(user["id"])

    def _is_expired() -> bool:
        if not synced_at:
            return True
        try:
            # D42 兼容两种存储：
            # - MySQL 存默认 datetime('now') = UTC；本地解析会显得早 8h → age 恒巨大 → 永远过期
            # - 历史/迁移可能存本地时间（CST=UTC+8）
            # 判定法：直接用「距今秒数」，若 |age| 过大则按 UTC+8 折算回本地再判。
            t = datetime.strptime(synced_at, "%Y-%m-%d %H:%M:%S")
            now = datetime.now()
            age = (now - t).total_seconds()
            # 值看起来「未来」或「非常旧」：说明基准是 UTC（与本地差约 8h）
            if abs(age) > 6 * 3600:
                # 存储值是 UTC → 用 UTC now 比（消除本地偏移）
                age = (datetime.utcnow() - t).total_seconds()
            return age > REPOS_CACHE_TTL
        except Exception:
            return True

    def _filter_by_provider(repos_list):
        """按 provider 过滤缓存（repo_key 前缀匹配）"""
        if provider in ("gitcode", "github") and len(_providers) == 1:
            pfx = provider + ":"
            return [r for r in repos_list if (r.get("repo_key", "").startswith(pfx) or
                       (provider == "gitcode" and not r.get("repo_key", "").startswith("github:")))]
        return repos_list

    if cached and not _is_expired():
        return {"repos": _filter_repos(user["id"], _filter_by_provider(cached), org_id=_filter_org_id), "_cached": True}

    # 缓存为空或已过期 → 从 VCS 拉取全部绑定平台仓库并合并
    # 每个平台独立 try/catch：一个平台拉取失败（如网络不通）不影响其他平台
    all_repos = []
    failed_providers = []
    _reauth_provider = ""  # P1-6 软失败: 记录需要重新授权的平台，不中断其他平台
    for p in _providers:
        try:
            def _fetch(token, _uid=user["id"], _p=p):
                return _fetch_and_cache(_uid, token, _p)
            repos = with_oauth_token(user, p, _fetch)
            all_repos.extend(repos)
        except HTTPException as _he:
            # P1-6 软失败: 401 reauth_required 不中断循环，记录后继续拉其他平台
            if _he.status_code == 401 and not _reauth_provider:
                _reauth_provider = p
                failed_providers.append(p)
            else:
                raise  # 非 401 的 HTTPException 仍然直接抛出
        except Exception as e:
            log.warning(f"Fetch repos for {p} failed (user={user['username']}): {e}")
            failed_providers.append(p)

    # P1-6 软失败: 有结果但某平台需要重新授权 → 合并旧缓存落库后，返回部分结果 + reauth 标记
    # (修复: 此前直接 return 跳过 save_user_repos，导致 GitCode 新增仓库不落库，
    #  工作台 check_repo_access 只认 user_repos 表 → 大厅可见但 403，缓存永不自愈)
    if _reauth_provider and all_repos:
        _seen = set()
        _deduped = []
        for r in all_repos:
            rk = r.get("repo_key", "")
            if rk not in _seen:
                _seen.add(rk)
                _deduped.append(r)
        # 失败平台(含 reauth 401 的平台)用旧缓存补齐，避免把缓存覆盖成不完整数据
        if failed_providers and cached:
            for r in cached:
                rk = r.get("repo_key", "")
                for fp in failed_providers:
                    if rk.startswith(fp + ":") and rk not in _seen:
                        _seen.add(rk)
                        _deduped.append(r)
                        break
        # 部分成功也落库，让 user_repos 缓存与实时结果同步
        user_db.save_user_repos(user["id"], _deduped)
        return {"repos": _filter_repos(user["id"], _filter_by_provider(_deduped), org_id=_filter_org_id),
                "_partial": True, "_reauth_provider": _reauth_provider}
    # 有平台需要重新授权且无任何结果 → 抛 401 让前端提示
    if _reauth_provider and not all_repos and not cached:
        raise HTTPException(status_code=401, detail={"code": 401,
            "message": f"{_reauth_provider} 授权已过期，请重新登录授权", "action": "reauth_required"})

    if not all_repos and cached:
        # 全部平台都失败 → 用旧缓存兜底
        return {"repos": _filter_repos(user["id"], _filter_by_provider(cached), org_id=_filter_org_id), "_cached": True, "_stale": True}
    if not all_repos:
        raise HTTPException(status_code=502, detail={"code": 502, "message": "加载仓库列表失败，请稍后重试"})

    # 合并后去重（按 repo_key）
    seen_keys = set()
    deduped = []
    for r in all_repos:
        rk = r.get("repo_key", "")
        if rk not in seen_keys:
            seen_keys.add(rk)
            deduped.append(r)
    repos = deduped

    # 部分平台拉取失败时，保留旧缓存中失败平台的仓库（避免覆盖为不完整缓存）
    if failed_providers and cached:
        for r in cached:
            rk = r.get("repo_key", "")
            # 只补失败平台的仓库（成功平台的新数据已包含在上面）
            for fp in failed_providers:
                if rk.startswith(fp + ":") and rk not in seen_keys:
                    seen_keys.add(rk)
                    deduped.append(r)
                    break
        repos = deduped

    # 合并后一次性写入缓存（save_user_repos 会先删再写，不能分平台各写一次）
    user_db.save_user_repos(user["id"], repos)

    # 缓存过期/重建路径：异步 + 节流地启动失效托管探活（请求不等它）
    try:
        _start_stale_probe_async(user, _providers)
    except Exception as e:
        log.warning(f"member: stale probe start failed (non-fatal): {e}")

    # 部分平台失败时附加提示
    result = {"repos": _filter_repos(user["id"], _filter_by_provider(repos), org_id=_filter_org_id), "_cached": False}
    if failed_providers:
        result["_partial"] = True
        result["_failed_providers"] = failed_providers
    return result


@router.post("/sync", operation_id="repos_sync", summary="Sync repo list from VCS", description="Manually syncs the repository list from VCS, refreshes the user_repos cache and probes/cleans stale managed records.")
def sync_repos(user: dict = Depends(require_org_member)):
    """手动从 VCS 同步仓库列表，刷新 user_repos 缓存，并探活清理失效托管记录。

    组织隔离：同步 GitCode 全量到 user_repos 缓存，但返回时按当前组织过滤
    （只返回该组织已托管的仓库 + 个人组织返回全量）。
    """
    # Issue #343 修复: 不再预获取 token（with_oauth_token 内部获取+预刷新）
    from src.auth.grape_account_db import get_user_identities
    _identities = get_user_identities(user["id"])
    _providers = [i["provider"] for i in _identities if i.get("provider") in ("gitcode", "github")]
    if not _providers:
        raise HTTPException(status_code=400, detail={"code": 400, "message": "API Token 未配置"})

    if "gitcode" in _providers:
        # 异步 + 节流启动失效托管探活（请求不等它，短时间不重复）
        try:
            _start_stale_probe_async(user, _providers)
        except Exception as e:
            log.warning(f"sync: stale probe start failed (non-fatal): {e}")

    # 每个平台独立 try/catch
    all_repos = []
    failed_providers = []
    _reauth_provider = ""  # P1-6 软失败: 记录需要重新授权的平台，不中断其他平台
    for p in _providers:
        try:
            def _fetch(token, _uid=user["id"], _p=p):
                return _fetch_and_cache(_uid, token, _p)
            repos = with_oauth_token(user, p, _fetch)
            all_repos.extend(repos)
        except HTTPException as _he:
            # P1-6 软失败: 401 reauth_required 不中断循环，记录后继续拉其他平台
            if _he.status_code == 401 and not _reauth_provider:
                _reauth_provider = p
                failed_providers.append(p)
            else:
                raise
        except Exception as e:
            log.warning(f"Sync repos for {p} failed (user={user['username']}): {e}")
            failed_providers.append(p)

    # P1-6 软失败: 有结果但某平台需要重新授权 → 继续保存+返回部分结果 + reauth 标记
    _has_reauth = bool(_reauth_provider)

    if not all_repos:
        raise HTTPException(status_code=502, detail={"code": 502, "message": "同步失败，请稍后重试"})

    seen_keys = set()
    deduped = []
    for r in all_repos:
        rk = r.get("repo_key", "")
        if rk not in seen_keys:
            seen_keys.add(rk)
            deduped.append(r)
    repos = deduped
    user_db.save_user_repos(user["id"], repos)

    org_id = user.get("org_id", 0)
    # 个人组织不隔离（显示全量用户仓库），非个人组织才隔离
    from src.user.org_db import is_org_personal
    _filter_org_id = 0 if is_org_personal(org_id) else org_id
    if _has_reauth:
        _msg = f"同步成功（{_reauth_provider} 授权已过期，请重新登录授权）"
        if _filter_org_id:
            return {"repos": _filter_repos(user["id"], repos, org_id=_filter_org_id),
                    "message": _msg, "_partial": True, "_reauth_provider": _reauth_provider}
        return {"repos": _filter_repos(user["id"], repos, org_id=0),
                "message": _msg, "_partial": True, "_reauth_provider": _reauth_provider}
    if _filter_org_id:
        # 非个人组织：已托管到其他组织的仓库不显示，未托管的显示
        return {"repos": _filter_repos(user["id"], repos, org_id=_filter_org_id), "message": "同步成功"}
    return {"repos": _filter_repos(user["id"], repos, org_id=0), "message": "同步成功"}


@router.get("/check-name", operation_id="repos_check_name", summary="Check repo name availability", description="Checks whether a repository with the same name already exists under the current user.")
def check_repo_name(name: str, user: dict = Depends(require_no_session_token)):
    """检查当前用户名下是否已存在同名仓库"""
    provider, api_token = _get_user_provider_and_token(user["username"], user.get("login_provider", ""), user.get("id", 0))
    if not api_token:
        raise HTTPException(status_code=400, detail={"code": 400, "message": "API Token 未配置"})

    if provider == "github":
        from src.user.repo_manager import _github_req, get_user_github_login
        try:
            login = get_user_github_login(api_token)
            if not login:
                raise HTTPException(status_code=502, detail={"code": 502, "message": "无法获取当前用户身份"})
            _github_req("GET", f"https://api.github.com/repos/{login}/{name}", api_token)
            return {"exists": True, "full_name": f"{login}/{name}"}
        except RuntimeError as e:
            if "404" in str(e) or "Not Found" in str(e):
                return {"exists": False}
            raise HTTPException(status_code=502, detail={"code": 502, "message": f"检查失败: {e}"})
        except HTTPException:
            raise
        except Exception as e:
            raise HTTPException(status_code=502, detail={"code": 502, "message": f"检查失败: {e}"})

    from src.user.repo_manager import _req, get_user_gitcode_login
    try:
        login = get_user_gitcode_login(api_token)
        if not login:
            raise HTTPException(status_code=502, detail={"code": 502, "message": "无法获取当前用户身份"})
        _req("GET", f"https://gitcode.com/api/v5/repos/{login}/{name}", api_token)
        return {"exists": True, "full_name": f"{login}/{name}"}
    except RuntimeError as e:
        if "404" in str(e) or "Not Found" in str(e):
            return {"exists": False}
        raise HTTPException(status_code=502, detail={"code": 502, "message": f"检查失败: {e}"})
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=502, detail={"code": 502, "message": f"检查失败: {e}"})


@router.post("/create", operation_id="repos_create", summary="Create repository (optionally auto-host)", description="Creates a repository via the VCS API; optionally auto-hosts it afterwards.")
def create_new_repo(body: CreateRepoBody, user: dict = Depends(require_no_session_token)):
    """通过 VCS API 新建仓库，可选创建后自动托管"""
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

    # R26: 优先用用户指定的 provider；未指定时 fallback 到自动选择
    if body.provider:
        provider = body.provider
        if provider == "github":
            api_token = get_user_github_token_with_refresh(user["username"], user_id=user["id"])
        else:
            api_token = get_user_token_with_refresh(user["username"], user.get("id", 0))
    else:
        provider, api_token = _get_user_provider_and_token(user["username"], user.get("login_provider", ""), user.get("id", 0))
    if not api_token:
        raise HTTPException(status_code=400, detail={"code": 400, "message": "API Token 未配置，请先通过 OAuth 登录或配置个人 Token"})

    if provider == "github":
        from src.user.repo_manager import create_github_repo as _create_gh_repo
        try:
            repo_info = _create_gh_repo(api_token=api_token, name=name, description=body.description, private=private)
        except Exception as e:
            raise HTTPException(status_code=502, detail={"code": 502, "message": f"创建仓库失败: {e}"})
    else:
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
        def _fetch(token, _uid=user["id"], _p=provider):
            return _fetch_and_cache(_uid, token, _p)
        with_oauth_token(user, provider, _fetch)
    except Exception:
        pass

    if body.host_after_create:
        from src.user.db import get_webhook_url
        webhook_url = get_webhook_url()
        if not webhook_url:
            # Issue #372: webhook_url 未配置时不阻断创建，只跳过托管
            try:
                _fetch_and_cache(user["id"], api_token, provider)
            except Exception:
                pass
            return {"repo": repo_info, "full_name": full_name, "owner": owner, "name": repo_name, "hosted": False, "message": "仓库已创建，但 webhook_url 未配置，请稍后手动托管"}
        # GitHub 仓库用 /webhook/github 路由
        if provider == "github":
            webhook_url = webhook_url.replace("/webhook/gitcode", "/webhook/github")
        # Issue #372: 路径 B 支持——无全局 bot token 时用用户 token + 随机密钥
        import secrets as _secrets
        if provider == "github":
            from src.user.repo_manager import find_existing_github_webhook, setup_github_webhook
            from src.user.db import get_global_github_bot_token
            _create_global_bot_token = get_global_github_bot_token()
            _use_path_b = not _create_global_bot_token  # P2-7: 与 host_repo 一致，有全局 GitHub bot token 时走路径 A
            _webhook_secret = _secrets.token_urlsafe(32) if _use_path_b else ""
        else:
            from src.user.db import get_global_gitcode_bot_token
            _create_global_bot_token = get_global_gitcode_bot_token()
            _use_path_b = not _create_global_bot_token
            _webhook_secret = _secrets.token_urlsafe(32) if _use_path_b else ""
        try:
            if provider == "github":
                _wh_token = _create_global_bot_token or api_token  # P2-7: 路径 A 用 bot token，路径 B 用用户 token
                existing_id = find_existing_github_webhook(_wh_token, owner, repo_name, webhook_url)
                repo_key = to_repo_key(full_name, provider="github")
                wh_col = "github_webhook_token"
                _bot_token_col = "bot_github_token"
            else:
                _wh_token = _create_global_bot_token or api_token
                from src.user.repo_manager import find_existing_webhook
                existing_id = find_existing_webhook(_wh_token, owner, repo_name, webhook_url)
                repo_key = to_repo_key(full_name)
                wh_col = "gitcode_webhook_token"
                _bot_token_col = "bot_gitcode_token"
            if existing_id:
                # Issue #372 R6.2: 路径 B 复用已有 webhook 时密钥不匹配 → 删旧重建
                if _use_path_b:
                    try:
                        if provider == "github":
                            from src.user.repo_manager import remove_github_webhook
                            remove_github_webhook(_wh_token, owner, repo_name, existing_id)
                        else:
                            from src.user.repo_manager import remove_webhook
                            remove_webhook(_wh_token, owner, repo_name, existing_id)
                        existing_id = None
                    except Exception as _re:
                        log.warning(f"create_new_repo: remove old webhook failed (will reuse): {_re}")
                        _webhook_secret = ""  # P1-1: 删失败时不写新密钥
                        _wh_token = ""  # §31: 也不写用户 token（刷新后 stale），让验证 loud fail
            if existing_id:
                # P1-2: 路径 B 成功时写空串（不写 stale 用户 token），webhook 验证依赖 webhook_secret
                _db_wh_token = "" if _use_path_b else _wh_token
                user_db.add_managed_repo(repo_key, owner, repo_name, webhook_id=existing_id,
                    **{wh_col: _db_wh_token, _bot_token_col: (_create_global_bot_token if not _use_path_b else "")},
                    host_user_id=(user["id"] if _use_path_b else 0), webhook_secret=_webhook_secret)
                user_db.mark_user_repo_join(user["id"], repo_key, is_join=True, is_admin=True)
            else:
                from src.user.repo_manager import setup_webhook, setup_github_webhook
                if provider == "github":
                    hook_id = setup_github_webhook(_wh_token, owner, repo_name, webhook_url, webhook_secret=_webhook_secret)
                else:
                    _wh_password = _webhook_secret if _use_path_b else _wh_token
                    hook_id = setup_webhook(_wh_token, owner, repo_name, webhook_url, webhook_password=_wh_password)
                # P1-2: 路径 B 成功时写空串（不写 stale 用户 token），webhook 验证依赖 webhook_secret
                _db_wh_token = "" if _use_path_b else _wh_token
                user_db.add_managed_repo(repo_key, owner, repo_name, webhook_id=hook_id,
                    **{wh_col: _db_wh_token, _bot_token_col: (_create_global_bot_token if not _use_path_b else "")},
                    host_user_id=(user["id"] if _use_path_b else 0), webhook_secret=_webhook_secret)
                user_db.mark_user_repo_join(user["id"], repo_key, is_join=True, is_admin=True)
            from src.api.session_store import ensure_default_agent_sessions
            ensure_default_agent_sessions(repo_key)
        except Exception as e:
            return {"repo": repo_info, "full_name": full_name, "owner": owner, "name": repo_name, "hosted": False, "message": f"仓库已创建，但托管失败: {e}"}

        try:
            _fetch_and_cache(user["id"], api_token, provider)
        except Exception:
            pass
        return {"repo": repo_info, "full_name": full_name, "owner": owner, "name": repo_name, "hosted": True, "message": "仓库创建并托管成功"}

    return {"repo": repo_info, "full_name": full_name, "owner": owner, "name": repo_name, "hosted": False, "message": "仓库创建成功"}


def _try_github_bot_collaborator(user_token: str, bot_token: str, bot_username: str,
                                 owner: str, repo: str) -> dict:
    """尝试将 GitHub bot 加入仓库维护者，返回结果信号给前端。

    流程：
    1. 用 bot token 调 GET /repos/{owner}/{repo}，看 permissions 判断 bot 当前权限
    2. 权限够（maintain/admin/push）→ 直接返回 ok
    3. 权限不够但已是 collaborator（有 pull）→ 用用户 token PUT 升级为 maintain（无需邀请）
    4. 不是 collaborator → 用用户 token 邀请 → bot token accept → 再查权限

    Issue #372 §16.3: 整个函数体包 try/except，bot_token 无效时不抛异常，
    返回 {"bot_status": "error"} 供调用方降级到路径 B。
    """
    if not bot_token or not bot_username:
        return {"bot_collaborator_needed": True, "bot_username": "", "bot_status": "not_configured"}

    try:
        from src.user.repo_manager import add_github_collaborator, accept_github_invite, get_github_repo_permissions

        # 0. 用 bot token 查自己对该仓库的权限
        perms = get_github_repo_permissions(bot_token, owner, repo)
        if perms and (perms.get("admin") or perms.get("maintain") or perms.get("push")):
            log.info(f"GitHub bot {bot_username} has sufficient permissions in {owner}/{repo}: {perms}")
            return {"bot_collaborator_needed": False, "bot_username": bot_username, "bot_status": "ok"}

        # 权限不够或不是 collaborator → 尝试用用户 token 升级/邀请
        if user_token:
            added = add_github_collaborator(user_token, owner, repo, bot_username, "push")
            if added:
                perms_after = get_github_repo_permissions(bot_token, owner, repo)
                if perms_after and (perms_after.get("admin") or perms_after.get("maintain") or perms_after.get("push")):
                    log.info(f"GitHub bot {bot_username} permissions upgraded to {perms_after}")
                    return {"bot_collaborator_needed": False, "bot_username": bot_username, "bot_status": "ok"}

                accepted = accept_github_invite(bot_token, owner, repo)
                if accepted:
                    perms_final = get_github_repo_permissions(bot_token, owner, repo)
                    if perms_final and (perms_final.get("admin") or perms_final.get("maintain") or perms_final.get("push")):
                        log.info(f"GitHub bot {bot_username} permissions after accept: {perms_final}")
                        return {"bot_collaborator_needed": False, "bot_username": bot_username, "bot_status": "ok"}

                log.info(f"GitHub bot {bot_username} invite sent but accept/perm not effective (accepted={accepted})")
                return {"bot_collaborator_needed": True, "bot_username": bot_username,
                        "bot_status": "invite_pending",
                        "bot_invite_url": f"https://github.com/{owner}/{repo}/settings/access"}
            else:
                return {"bot_collaborator_needed": True, "bot_username": bot_username,
                        "bot_status": "add_failed",
                        "bot_invite_url": f"https://github.com/{owner}/{repo}/settings/access"}
        else:
            return {"bot_collaborator_needed": True, "bot_username": bot_username,
                    "bot_status": "insufficient_permission" if (perms and perms.get("pull")) else "not_configured",
                    "bot_invite_url": f"https://github.com/{owner}/{repo}/settings/access"}
    except Exception as e:
        log.warning(f"_try_github_bot_collaborator: error (bot_token may be invalid): {e}")
        return {"bot_collaborator_needed": True, "bot_username": bot_username, "bot_status": "error"}


@router.post("/host", operation_id="repos_host", summary="Host repository", description="Registers an existing repository for hosting: creates the webhook and sets up the repo bot.")
def host_repo(body: RepoAction, user: dict = Depends(require_no_session_token), request: Request = None,
              provider: str = ""):
    """托管仓库（建 webhook + 加 bot 为维护者 + 注册到 managed_repos）。

    provider 参数由调用方传入（如 create_from_template 已确定 provider）；
    不传时自动选择（向后兼容）。
    """
    from src.repo_key import to_repo_key
    # 解析当前组织上下文：优先从 user dict（require_org_member 注入），其次从 request header
    _org_id = user.get("org_id", 0) if isinstance(user, dict) else 0
    if not _org_id and request:
        try:
            _org_id = resolve_org_context(request, user["id"])
        except Exception:
            _org_id = 0
    # ===== provider 判定：仓库来源优先于登录平台（host-repo-provider-routing-fix-design.md）=====
    if not provider:
        provider = _provider_from_hint(body.full_name)
    # 剥离 provider 前缀（"github:owner/repo" / "gitcode:owner/repo"），下游统一用纯 owner/repo
    _raw = body.full_name
    if ":" in _raw and _raw.split(":", 1)[0] in ("github", "gitcode"):
        _raw = _raw.split(":", 1)[1]
        body.full_name = _raw
    parts = _raw.split("/", 1)
    if len(parts) != 2:
        raise HTTPException(status_code=400, detail={"code": 400, "message": "无效的仓库标识"})
    owner, repo_name = parts

    # D44: 显式 provider > 仓库来源提示；仍无 → 按平台 token 可用性自动选择（github 优先，不以登录平台短路）
    if provider == "github":
        api_token = get_user_github_token_with_refresh(user["username"], user_id=user["id"])
    elif provider == "gitcode":
        api_token = get_user_token_with_refresh(user["username"], user.get("id", 0))
    else:
        provider, api_token = _provider_from_token(user)
    from src.user.db import get_webhook_url
    webhook_url = get_webhook_url()
    if not webhook_url:
        # Issue #372 §14.2.3: 报错带结构化 guidance，指导用户自服务而非指向管理员
        from src.user.db import get_meta_value as _gmv
        _detect_failed = _gmv("webhook_url_detect_failed") or ""
        _tried = ["server-config.json (not found)"]
        if _detect_failed:
            _tried.append("auto-detect (egress blocked or hostname loopback)")
        else:
            _tried.append("auto-detect (not yet attempted — access Web UI via public IP to trigger)")
        raise HTTPException(status_code=400, detail={
            "code": 400,
            "error_code": "WEBHOOK_URL_NOT_CONFIGURED",
            "message": "webhook_url not configured",
            "guidance": {
                "tried": _tried,
                "action": "Access this Web UI via public IP (auto-detect will trigger on login), or set webhook_url in Settings",
                "example": "http://<public-ip>/webhook/gitcode"
            }
        })
    # GitHub 仓库用 /webhook/github 路由
    if provider == "github":
        webhook_url = webhook_url.replace("/webhook/gitcode", "/webhook/github")

    if not api_token:
        if provider == "github":
            raise HTTPException(status_code=400, detail={"code": 400, "error_code": "TOKEN_NOT_BOUND", "message": "GitHub API Token 未配置，请重新绑定 GitHub 账号"})
        raise HTTPException(status_code=400, detail={"code": 400, "error_code": "TOKEN_NOT_BOUND", "message": "GitCode Token 已过期且无法自动刷新，请重新登录"})

    # ===== GitHub 仓库托管 =====
    if provider == "github":
        from src.user.repo_manager import find_existing_github_webhook, setup_github_webhook
        from src.user.repo_manager import add_github_collaborator, accept_github_invite, get_github_collaborator_permission
        from src.api.session_store import ensure_default_agent_sessions
        repo_key = to_repo_key(body.full_name, provider="github")

        # 读取 GitHub bot 全局配置
        # Issue #345: 改用统一入口
        from src.user.db import get_global_github_bot_token
        gh_bot_token = get_global_github_bot_token()
        # Issue #345: GitHub bot username 从 DB meta 读取
        from src.user.db import get_global_github_bot_username
        gh_bot_username = get_global_github_bot_username()

        # Issue #372: GitHub 路径 B——无全局 GitHub bot token 时跳过 bot 协作者
        _gh_use_path_b = not gh_bot_token
        import secrets as _secrets
        _gh_webhook_secret = _secrets.token_urlsafe(32) if _gh_use_path_b else ""

        try:
            def _find_gh_hook(token, _owner=owner, _repo=repo_name, _url=webhook_url):
                return find_existing_github_webhook(token, _owner, _repo, _url)
            existing_id = with_oauth_token(user, "github", _find_gh_hook)
            if existing_id:
                # Issue #372 R6.2: 路径 B 复用已有 webhook 时密钥不匹配 → 删旧重建
                if _gh_use_path_b:
                    try:
                        from src.user.repo_manager import remove_github_webhook
                        remove_github_webhook(api_token, owner, repo_name, existing_id)
                        existing_id = None
                    except Exception as _re:
                        log.warning(f"host_repo GitHub: remove old webhook failed (will reuse): {_re}")
                        _gh_webhook_secret = ""  # P1-1: 删失败时不写新密钥
                        api_token = ""  # §31: 也不写用户 token（刷新后 stale），让验证 loud fail
            if existing_id:
                # Issue #372 §16.3a: 先检查 bot 协作者状态，error 时降级到路径 B（再写 DB）
                _bot_result = {"bot_collaborator_needed": False}
                if not _gh_use_path_b:
                    _bot_result = _try_github_bot_collaborator(api_token, gh_bot_token, gh_bot_username, owner, repo_name)
                    # R12.3: invite_pending/add_failed 也降级到路径 B
                    if _bot_result.get("bot_status") in ("error", "invite_pending", "add_failed"):
                        log.warning(f"host_repo GitHub: bot status={_bot_result.get('bot_status')}, falling back to path B")
                        _gh_use_path_b = True
                        _gh_webhook_secret = _secrets.token_urlsafe(32)
                        # R25.2: A→B 降级时删除旧 webhook（路径 A 创建时无 secret），
                        # 强制用新 secret 重建。删失败时保留旧 existing_id + 旧 secret
                        # 不新建（避免双 webhook 收重复事件）。
                        if existing_id:
                            try:
                                from src.user.repo_manager import remove_github_webhook
                                remove_github_webhook(api_token, owner, repo_name, existing_id)
                                existing_id = None
                                log.info("host_repo GitHub A→B: removed old webhook for rebuild")
                            except Exception as _re:
                                log.warning(f"host_repo GitHub A→B: remove old webhook failed: {_re}")
                                # 删失败：保留旧 existing_id，回退到路径 A 语义
                                _gh_use_path_b = False
                                _gh_webhook_secret = ""
                if existing_id:
                    # P1-2: 路径 B 成功时写空串（不写 stale 用户 token），webhook 验证依赖 webhook_secret
                    _gh_db_wh_token = "" if _gh_use_path_b else api_token
                    user_db.add_managed_repo(repo_key, owner, repo_name, webhook_id=existing_id,
                                             github_webhook_token=_gh_db_wh_token,
                                             bot_github_token=("" if _gh_use_path_b else gh_bot_token), bot_username=(gh_bot_username if not _gh_use_path_b else ""), org_id=_org_id,
                                             host_user_id=(user["id"] if _gh_use_path_b else 0), webhook_secret=_gh_webhook_secret)
                    user_db.mark_user_repo_join(user["id"], repo_key, is_join=True, is_admin=True)
                    ensure_default_agent_sessions(repo_key)
                    return {"message": "仓库已托管（复用已有 webhook）", "webhook_id": existing_id, **_bot_result}

            # Issue #372 §16.3a: 先检查 bot 协作者状态再建 webhook + 写 DB
            _bot_result = {"bot_collaborator_needed": False}
            if not _gh_use_path_b:
                _bot_result = _try_github_bot_collaborator(api_token, gh_bot_token, gh_bot_username, owner, repo_name)
                # R12.3: invite_pending/add_failed 也降级到路径 B
                if _bot_result.get("bot_status") in ("error", "invite_pending", "add_failed"):
                    log.warning(f"host_repo GitHub: bot status={_bot_result.get('bot_status')}, falling back to path B")
                    _gh_use_path_b = True
                    _gh_webhook_secret = _secrets.token_urlsafe(32)
            hook_id = setup_github_webhook(api_token, owner, repo_name, webhook_url, webhook_secret=(_gh_webhook_secret if _gh_use_path_b else ""))
            from src.api.onboarding import cleanup_stale_repo_data
            cleanup_stale_repo_data(repo_key)
            # P1-2: 路径 B 成功时写空串（不写 stale 用户 token），webhook 验证依赖 webhook_secret
            _gh_db_wh_token = "" if _gh_use_path_b else api_token
            user_db.add_managed_repo(repo_key, owner, repo_name, webhook_id=hook_id,
                                     github_webhook_token=_gh_db_wh_token,
                                     bot_github_token=("" if _gh_use_path_b else gh_bot_token), bot_username=(gh_bot_username if not _gh_use_path_b else ""), org_id=_org_id,
                                     host_user_id=(user["id"] if _gh_use_path_b else 0), webhook_secret=_gh_webhook_secret)
            user_db.mark_user_repo_join(user["id"], repo_key, is_join=True, is_admin=True)
            ensure_default_agent_sessions(repo_key)
            return {"message": "仓库托管成功", "webhook_id": hook_id, **_bot_result}
        except HTTPException:
            raise
        except Exception as e:
            raise HTTPException(status_code=502, detail={"code": 502, "message": f"托管失败: {e}"})

    # ===== GitCode 仓库托管 =====
    # Issue #345: 改用统一入口读取全局 bot 配置
    from src.user.db import get_global_gitcode_bot_token, get_global_bot_usernames
    global_bot_token = get_global_gitcode_bot_token()
    _us = get_global_bot_usernames()
    global_bot_username = _us[0] if _us else ""

    # Issue #372: 全局 Bot Token 优先（路径 A），无则降级到用户 Token（路径 B）
    _use_path_b = not global_bot_token

    if _use_path_b:
        # 路径 B：无全局 Bot Token，用用户 Token 建 webhook
        webhook_token = api_token
        # 生成随机 webhook 验证密钥（与 token 解耦，避免 token 刷新导致 webhook 验证失败）
        import secrets as _secrets
        _webhook_secret = _secrets.token_urlsafe(32)
    else:
        # 路径 A：webhook 统一用全局 bot token 创建/查（方案 C）
        webhook_token = global_bot_token
        _webhook_secret = ""

    from src.user.repo_manager import _check_collaborator_admin, _req
    try:
        def _get_user(token):
            return _req("GET", f"https://gitcode.com/api/v5/user", token)
        user_data = with_oauth_token(user, "gitcode", _get_user)
        current_user = user_data.get("login", "")
        if not current_user:
            raise HTTPException(status_code=502, detail={"code": 502, "message": "无法获取当前用户身份"})

        if current_user == owner:
            is_admin = True
        else:
            def _check_admin(token, _owner=owner, _repo=repo_name, _cu=current_user):
                # P1-1 返工: 直接调 _req 绕过 _check_collaborator_admin 的 except RuntimeError: return False，
                # 让原始 401/403 RuntimeError 穿透到 with_oauth_token 触发刷新重试。
                import urllib.parse as _up
                _perm_url = f"https://gitcode.com/api/v5/repos/{_owner}/{_up.quote(_repo, safe='')}/collaborators/{_up.quote(_cu, safe='')}/permission"
                _data = _req("GET", _perm_url, token)
                _access_level = 0
                try:
                    _access_level = int(_data.get("access_level", 0) or 0)
                except (TypeError, ValueError):
                    pass
                if _access_level >= 40:
                    return True
                if str(_data.get("permission", "") or "").lower() == "admin":
                    return True
                _perm = _data.get("permissions") or {}
                return bool(_perm.get("admin")) if isinstance(_perm, dict) else False
            is_admin = with_oauth_token(user, "gitcode", _check_admin)

        if not is_admin:
            raise HTTPException(status_code=403, detail={"code": 403, "error_code": "PERMISSION_DENIED", "message": "无权限托管，请联系管理员"})
    except HTTPException:
        raise
    except RuntimeError as e:
        # Issue #383/#395: 上游 RuntimeError 分流——含内部 API URL 的原文不得透出给用户。
        # _req 报文格式固定：GitCode API {method} {url} failed ({code}): {text}
        import re as _re
        _m = _re.search(r'failed \((\d{3})\)', str(e))
        _code = int(_m.group(1)) if _m else 0
        log.warning(f"host_repo upstream failed: {e}")
        if _code == 404:
            # 仓库不存在/无权访问（业务语义 404，而非网关故障 502）
            raise HTTPException(status_code=404, detail={"code": 404, "message": "仓库不存在或无权访问"})
        if _code in (401, 403):
            # token 失效/无权（with_oauth_token 已做刷新重试，仍失败说明凭据或权限不足）
            raise HTTPException(status_code=403, detail={"code": 403, "message": "无权限托管，请联系管理员"})
        raise HTTPException(status_code=502, detail={"code": 502, "message": "托管服务暂时不可用，请稍后重试"})
    except Exception as e:
        # Issue #383/#395: 兜底不拼接上游原文（防泄露内部 URL/报文），原文仅落日志
        log.warning(f"host_repo upstream failed: {e}")
        raise HTTPException(status_code=502, detail={"code": 502, "message": "托管服务暂时不可用，请稍后重试"})

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
        # 原则：凡能查/能建一律 bot token；owner token 仅身份确认 + 本步邀请
        # Issue #372: 路径 B（无全局 bot token）跳过 bot 协作者流程
        # Issue #372 §16.1: 路径 A bot 协作者失败时降级到路径 B（不 raise 502）
        import secrets as _secrets
        if not _use_path_b and api_token and global_bot_username and global_bot_token:
            _mark("partial")
            ok, add_err = add_collaborator(api_token, owner, repo_name, global_bot_username, "maintainer")
            if not ok:
                # 兜底：add_collaborator 失败可能是已成员/已邀请，用 bot token 自查确认
                try:
                    bot_perm = get_member_permission(global_bot_token, owner, repo_name, global_bot_username)
                    access_level = (bot_perm or {}).get("access_level", 0) or 0
                except _RateLimitedError as _rl:
                    log.warning(f"host: perm check rate-limited (non-fatal), mark bot_missing: {_rl}")
                    _mark("bot_missing")
                    raise HTTPException(status_code=503, detail={"code": 503, "message": "托管暂时失败：GitCode API 调用过于频繁，请稍后重试"})
                except Exception:
                    # §16.1: 非限流异常（401/404）→ 降级到路径 B
                    log.warning(f"host: path A get_member_permission failed, falling back to path B")
                    _use_path_b = True
                    webhook_token = api_token
                    _webhook_secret = _secrets.token_urlsafe(32)
                    _mark("path_b_fallback")
                else:
                    if access_level >= 40:
                        log.info(f"host: add_collaborator failed but bot {global_bot_username} already maintainer (access_level={access_level}) in {owner}/{repo_name}, continue")
                    else:
                        # §16.1: bot 权限不足 → 降级到路径 B（原 raise 502）
                        log.warning(f"host: bot access_level={access_level} < 40, falling back to path B")
                        _use_path_b = True
                        webhook_token = api_token
                        _webhook_secret = _secrets.token_urlsafe(32)
                        _mark("path_b_fallback")

            if not _use_path_b:
                # bot 接受邀请
                if not accept_invite_for_repo(global_bot_token, owner, repo_name):
                    log.warning(f"host: no invite to accept for {owner}/{repo_name}, bot may already be member")

                # 校验 bot 已是维护者（access_level>=40）——用 bot token；限流/失败允许降级继续（方案 B）
                try:
                    bot_perm = get_member_permission(global_bot_token, owner, repo_name, global_bot_username)
                    access_level = (bot_perm or {}).get("access_level", 0) or 0
                    if access_level < 40:
                        # §16.1: 权限不足 → 降级到路径 B（原 raise 502）
                        log.warning(f"host: bot perm check inconclusive (level={access_level}), falling back to path B")
                        _use_path_b = True
                        webhook_token = api_token
                        _webhook_secret = _secrets.token_urlsafe(32)
                        _mark("path_b_fallback")
                except _RateLimitedError as _rl:
                    # 限流：权限未判定，继续 webhook（不阻塞），host_status 待 dashboard 复查
                    log.warning(f"host: perm check rate-limited (continue): {_rl}")
                except Exception:
                    # §16.1: 非限流异常 → 降级到路径 B
                    log.warning(f"host: path A perm check failed, falling back to path B")
                    _use_path_b = True
                    webhook_token = api_token
                    _webhook_secret = _secrets.token_urlsafe(32)
                    _mark("path_b_fallback")

        # 4) webhook：路径 A 用 bot token 查/建；路径 B 用用户 token + 随机密钥
        existing_id = find_existing_webhook(webhook_token, owner, repo_name, webhook_url)
        if existing_id:
            # Issue #372 R6.2: 路径 B 复用已有 webhook 时密钥不匹配 → 删旧重建
            if _use_path_b:
                try:
                    from src.user.repo_manager import remove_webhook
                    remove_webhook(webhook_token, owner, repo_name, existing_id)
                    existing_id = None
                except Exception as _re:
                    log.warning(f"host_repo GitCode: remove old webhook failed (will reuse): {_re}")
                    _webhook_secret = ""  # P1-1: 删失败时不写新密钥
                    webhook_token = ""  # §31: 也不写用户 token（刷新后 stale），让验证 loud fail
        if existing_id:
            # P1-2: 路径 B 成功时写空串（不写 stale 用户 token），webhook 验证依赖 webhook_secret
            _db_webhook_token = "" if _use_path_b else webhook_token
            user_db.add_managed_repo(repo_key, owner, repo_name, webhook_id=existing_id, gitcode_webhook_token=_db_webhook_token, bot_gitcode_token=(global_bot_token if not _use_path_b else ""), bot_username=(global_bot_username if not _use_path_b else ""), org_id=_org_id, host_user_id=(user["id"] if _use_path_b else 0), webhook_secret=_webhook_secret)
            user_db.mark_user_repo_join(user["id"], repo_key, is_join=True, is_admin=True)
            from src.api.session_store import ensure_default_agent_sessions
            ensure_default_agent_sessions(repo_key)
            _mark("")
            return {"message": "仓库已托管（复用已有 webhook）", "webhook_id": existing_id}

        # 路径 B: webhook_password 用随机密钥；路径 A: 用 bot token
        _wh_password = _webhook_secret if _use_path_b else webhook_token
        try:
            hook_id = setup_webhook(webhook_token, owner, repo_name, webhook_url, webhook_password=_wh_password)
        except RuntimeError as _wh_err:
            # 区分 token 失效(401) / 仓库不存在于本平台(404)，给出可操作文案（host-repo-provider-routing-fix-design.md）
            import re as _re
            _msg = str(_wh_err)
            _codes = _re.findall(r"\((\d{3})\)", _msg)
            _code = int(_codes[0]) if _codes else 0
            if _code == 401:
                raise HTTPException(status_code=502, detail={"code": 502,
                                                             "message": "GitCode 登录已失效，请重新绑定/登录 GitCode 后重试"})
            if _code == 404:
                raise HTTPException(status_code=502, detail={"code": 502,
                                                             "message": f"仓库 {owner}/{repo_name} 不存在于 GitCode，请确认仓库所属平台与托管账号平台一致"})
            raise
        # 清理同名旧仓库残留的 DB 数据（同上）
        from src.api.onboarding import cleanup_stale_repo_data
        cleanup_stale_repo_data(repo_key)
        # P1-2: 路径 B 成功时写空串（不写 stale 用户 token），webhook 验证依赖 webhook_secret
        _db_webhook_token = "" if _use_path_b else webhook_token
        user_db.add_managed_repo(repo_key, owner, repo_name, webhook_id=hook_id, gitcode_webhook_token=_db_webhook_token, bot_gitcode_token=(global_bot_token if not _use_path_b else ""), bot_username=(global_bot_username if not _use_path_b else ""), org_id=_org_id, host_user_id=(user["id"] if _use_path_b else 0), webhook_secret=_webhook_secret)
        user_db.mark_user_repo_join(user["id"], repo_key, is_join=True, is_admin=True)
        from src.api.session_store import ensure_default_agent_sessions
        ensure_default_agent_sessions(repo_key)
        _mark("")
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


@router.post("/{owner}/{repo}/repair-host", operation_id="repos_repair_host", summary="Repair hosting", description="Self-heals an unfinished hosting flow (bot not joined / webhook missing). Idempotent; uses owner-token invite + bot-token webhook, skipping the invite when the bot is already a maintainer.")
def repair_host(owner: str, repo: str, user: dict = Depends(require_no_session_token)):
    """方案 F-4：自愈未完成的托管流程（bot 未加入 / webhook 未创建），幂等。

    流程：需要 owner token 邀请 + bot token 建 webhook；判定 bot 已是维护者则跳过邀请；
    返回修复后的 host_status。

    Issue #372: 按 provider 统一分流（get_hosting_provider_config），路径 B 支持无全局 bot token。
    """
    from src.repo_key import to_repo_key, parse_repo_key
    from src.user.repo_manager import get_hosting_provider_config, _RateLimitedError
    from src.user.db import get_webhook_url, set_host_status
    from src.api.deps import get_user_token_with_refresh, get_user_github_token_with_refresh

    repo_key = to_repo_key(f"{owner}/{repo}")
    from src.api.deps import check_repo_access
    check_repo_access(user, repo_key)

    _provider, _, _ = parse_repo_key(repo_key)
    _cfg = get_hosting_provider_config(_provider)
    bot_token = _cfg["bot_token"]
    bot_username = _cfg["bot_username"]

    # owner token 按 provider 分流
    if _provider == "github":
        api_token = get_user_github_token_with_refresh(user["username"], user_id=user["id"])
    else:
        api_token = get_user_token_with_refresh(user["username"], user.get("id", 0))
    if not api_token:
        raise HTTPException(status_code=400, detail={"code": 400, "error_code": "TOKEN_NOT_BOUND", "message": "缺少用户 API Token，请重新登录"})

    # Issue #372: 路径 B——无全局 bot token 时用用户 token 修复 webhook
    _use_path_b = not bot_token
    import secrets as _secrets
    _webhook_secret = _secrets.token_urlsafe(32) if _use_path_b else ""

    # webhook_url 按 provider 替换路由后缀
    webhook_url = get_webhook_url()
    if not webhook_url:
        # Issue #372 §14.2.3: 报错带结构化 guidance
        from src.user.db import get_meta_value as _gmv
        _detect_failed = _gmv("webhook_url_detect_failed") or ""
        _tried = ["server-config.json (not found)"]
        if _detect_failed:
            _tried.append("auto-detect (egress blocked or hostname loopback)")
        else:
            _tried.append("auto-detect (not yet attempted — access Web UI via public IP to trigger)")
        raise HTTPException(status_code=400, detail={
            "code": 400,
            "error_code": "WEBHOOK_URL_NOT_CONFIGURED",
            "message": "webhook_url not configured",
            "guidance": {
                "tried": _tried,
                "action": "Access this Web UI via public IP (auto-detect will trigger on login), or set webhook_url in Settings",
                "example": "http://<public-ip>/webhook/gitcode"
            }
        })
    if _cfg["webhook_url_suffix"] not in webhook_url:
        webhook_url = webhook_url.replace("/webhook/gitcode", _cfg["webhook_url_suffix"])

    # 1) bot 是否已是维护者（路径 A only；路径 B 跳过）
    bot_ok = True if _use_path_b else False
    if not _use_path_b:
        from src.user.repo_manager import get_github_repo_permissions, get_member_permission
        if _provider == "github":
            try:
                _perm = get_github_repo_permissions(bot_token, owner, repo)
                bot_ok = bool(_perm and (_perm.get("admin") or _perm.get("maintain") or _perm.get("push")))
            except Exception:
                bot_ok = False
        else:
            # GitCode: 用 get_member_permission 检查
            try:
                perm = get_member_permission(bot_token, owner, repo, bot_username)
                bot_ok = bool(perm and (perm.get("access_level") or 0) >= 40)
            except _RateLimitedError:
                raise HTTPException(status_code=503, detail={"code": 503, "message": "GitCode API 调用过于频繁，请稍后重试"})

        # P1-5: bot 权限不足时邀请+重试（GitHub 和 GitCode 共用，提到 if/else 同级）
        if not bot_ok:
            if _provider == "github":
                # R12.6: GitHub 需要显式 accept（原注释"GitHub 自动处理"是错误的）
                from src.user.repo_manager import add_github_collaborator, accept_github_invite
                try:
                    add_github_collaborator(api_token, owner, repo, bot_username, "push")
                    accept_github_invite(bot_token, owner, repo)
                    # 重新检查权限
                    try:
                        _perm2 = get_github_repo_permissions(bot_token, owner, repo)
                        bot_ok = bool(_perm2 and (_perm2.get("admin") or _perm2.get("maintain") or _perm2.get("push")))
                    except Exception:
                        bot_ok = False
                except Exception as _ge:
                    log.warning(f"repair_host GitHub: add collaborator failed: {_ge}")
                    bot_ok = False
            else:
                # GitCode: add_collaborator + accept_invite + retry
                # Issue #372 §16.2: 包 try/except，失败时降级到路径 B
                from src.user.repo_manager import add_collaborator, accept_invite_for_repo
                try:
                    add_collaborator(api_token, owner, repo, bot_username, "maintainer")[0]
                    accept_invite_for_repo(bot_token, owner, repo)
                except Exception as _ie:
                    log.warning(f"repair_host GitCode: bot invite failed: {_ie}")
                    bot_ok = False
                else:
                    try:
                        perm2 = get_member_permission(bot_token, owner, repo, bot_username)
                        bot_ok = bool(perm2 and (perm2.get("access_level") or 0) >= 40)
                    except _RateLimitedError:
                        bot_ok = False
                    except Exception:
                        bot_ok = False
            if not bot_ok:
                # Issue #372 §16.2: 降级到路径 B（原 return error）
                log.warning(f"repair_host: path A bot failed, falling back to path B for {owner}/{repo}")
                _use_path_b = True
                _wh_token = api_token
                _webhook_secret = _secrets.token_urlsafe(32)
                # 继续走 webhook 流程，不 return error

    # 2) webhook 是否存在，缺失则补建
    # 路径 A: bot token 查/建；路径 B: 用户 token 查/建 + 随机密钥
    _wh_token = api_token if _use_path_b else bot_token
    if _provider == "github":
        _wh_password = _webhook_secret  # GitHub setup_github_webhook 用 webhook_secret 参数
    else:
        _wh_password = _webhook_secret if _use_path_b else bot_token  # GitCode setup_webhook 用 webhook_password 参数

    _find_fn = _cfg["find_webhook"]
    _setup_fn = _cfg["setup_webhook"]
    _remove_fn = _cfg["remove_webhook"]

    hid = None
    try:
        hid = _find_fn(_wh_token, owner, repo, webhook_url)
    except Exception:
        hid = None
    if hid:
        # Issue #372 R6.2: 路径 B 场景找到已有 webhook 时，旧 webhook 可能用旧密钥。
        # 删除旧 webhook 再用新密钥重建，确保 DB _webhook_secret 与 webhook 实际密钥一致。
        if _use_path_b:
            # R12.7: 路径 B 删旧重建
            _force_rebuild = True
        else:
            # R12.7: 路径 A 检查是否刚从 B 升级（webhook_secret 残留非空）
            # 升级后 GitCode 上 webhook 密码仍是旧 secret，DB 已清空 → 不匹配 → 强制重建
            from src.user.db import get_webhook_secret
            _stale_secret = get_webhook_secret(repo_key)
            _force_rebuild = bool(_stale_secret)
            if _force_rebuild:
                log.info(f"repair_host: detected stale webhook_secret after B→A upgrade, forcing rebuild for {owner}/{repo}")
        if _force_rebuild:
            try:
                _remove_fn(_wh_token, owner, repo, hid)
                log.info(f"repair_host: removed old webhook {hid} for re-creation with new secret")
                hid = None
            except Exception as _re:
                log.warning(f"repair_host: remove old webhook failed (will reuse): {_re}")
                _webhook_secret = ""  # P1-1: 删失败时不写新密钥
                _wh_token = ""  # §31: 也不写用户 token（刷新后 stale），让验证 loud fail
                _wh_password = _webhook_secret
    if not hid:
        if _provider == "github":
            hid = _setup_fn(_wh_token, owner, repo, webhook_url, webhook_secret=_wh_password)
        else:
            hid = _setup_fn(_wh_token, owner, repo, webhook_url, webhook_password=_wh_password)
    if not hid:
        set_host_status(repo_key, "webhook_missing")
        return {"status": "error", "host_status": "webhook_missing",
                "message": "未能创建 webhook，请检查权限后重试"}

    # 3) 完成 → 回写 webhook_id → 清状态
    try:
        from src.user.db import add_managed_repo
        # P1-2: 路径 B 成功时写空串（不写 stale 用户 token），webhook 验证依赖 webhook_secret
        _db_wh_token = "" if _use_path_b else _wh_token
        _db_kwargs = {
            "webhook_id": hid,
            "bot_username": (bot_username if not _use_path_b else ""),
            "host_user_id": (user["id"] if _use_path_b else 0),
            "webhook_secret": _webhook_secret,
            _cfg["db_token_field"]: _db_wh_token,
            _cfg["db_bot_token_field"]: (bot_token if not _use_path_b else ""),
        }
        add_managed_repo(repo_key, owner, repo, **_db_kwargs)
    except Exception as _e:
        log.warning(f"repair_host: write back webhook_id failed: {_e}")
    set_host_status(repo_key, "")
    return {"status": "ok", "host_status": "", "webhook_id": hid,
            "message": "托管已修复（webhook 已就绪）" if _use_path_b else "托管已修复（bot 已加入成员，webhook 已就绪）"}


@router.get("/managed", operation_id="repos_managed", summary="List managed repositories", description="Lists repositories hosted by the platform for the current user.")
def list_managed_repos(user: dict = Depends(require_org_member)):
    org_id = user.get("org_id", 0)
    # 个人组织放宽 org 过滤（方案A）：与 /repos/member 大堂口径一致，
    # 侧边栏下拉框显示 user_repos.is_join=1 的仓库（不论托管在哪个组织）
    _personal_org = False
    if org_id:
        try:
            from src.user.org_db import is_org_personal
            _personal_org = bool(is_org_personal(org_id))
        except Exception:
            _personal_org = False
    repos = user_db.get_managed_repos(user["id"], org_id=org_id, personal_org=_personal_org)
    return {"repos": [{
        "repo_key": r["repo_key"],
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


@router.get("/bot-token/{owner}/{repo_name}", operation_id="repos_bot_token_get", summary="Get repository bot token", description="Returns whether the repository has a bound DevOps bot token.")
def get_repo_bot_token(owner: str, repo_name: str, user: dict = Depends(require_no_session_token)):
    """获取仓库绑定的 DevOps Bot Token 是否存在"""
    token = get_bot_token_by_repo(owner, repo_name)
    return {"has_bot_token": bool(token)}


@router.put("/bot-token", operation_id="repos_bot_token_set", summary="Set repository bot token", description="Sets the repository DevOps bot token and automatically adds the repo bot as a maintainer.")
def set_repo_bot_token(body: BotTokenBody, user: dict = Depends(require_no_session_token)):
    """设置仓库绑定的 DevOps Bot Token。

    R15 七步流程：
    1. 检查仓库存在（不存在 → 404）
    2. 验证 token 有效性（无效 → 400 TOKEN_INVALID）
    3. 获取 bot username
    4. 邀 bot 为协作者（用用户 token / host 用户 token，失败记日志继续）
    5. 重建 webhook（删旧 + 建新，部分失败保护）
    6. 写 DB（bot_token + 清 host_user_id + 清 webhook_secret + 写 webhook_id + 清 bot_token_status）
    7. 返回 200 + bot_username

    R15.5: 传空串 bot_token 时清除仓库级 bot token（不重建 webhook，清除 bot_token_status）。
    """
    from src.repo_key import to_repo_key, parse_repo_key, split_owner_repo
    from src.api.deps import check_repo_access
    from src.session_manager import db, db_lock

    parts = body.full_name.split("/", 1)
    if len(parts) != 2:
        raise HTTPException(status_code=400, detail={"code": 400, "message": "无效的仓库标识"})
    owner, repo_name = parts
    repo_key = to_repo_key(body.full_name)
    check_repo_access(user, repo_key)

    # R21.8.2: 只有托管人或 admin 可配置仓库级 bot token（防水平越权）
    # check_repo_access 已检查托管人权限，这里补充 admin 检查
    _is_admin = (user.get("role") == "admin" or user.get("auth_type") in ("session_token", "api_key"))
    if not _is_admin:
        # 非 admin 用户只能配置自己托管的仓库（check_repo_access 已验证）
        # 额外检查：如果是路径 B 仓库，必须是 host_user 本人
        from src.user.db import get_host_user_id
        _host_uid = get_host_user_id(repo_key)
        if _host_uid > 0 and _host_uid != user.get("id", 0):
            raise HTTPException(status_code=403, detail={
                "code": 403, "error_code": "NOT_REPO_OWNER",
                "message": "只有仓库托管人或管理员可配置 Bot Token"
            })

    # 确定 provider
    _provider_str, _, _ = parse_repo_key(repo_key)
    _is_github = (_provider_str == "github")

    # R15.5: 传空串时清除仓库级 bot token
    if not body.bot_token:
        try:
            with db_lock:
                c = db.cursor()
                if _is_github:
                    c.execute(
                        "UPDATE managed_repos SET bot_github_token='', bot_token_status='' "
                        "WHERE repo_key=?",
                        (repo_key,),
                    )
                else:
                    c.execute(
                        "UPDATE managed_repos SET bot_gitcode_token='', bot_token_status='' "
                        "WHERE repo_key=?",
                        (repo_key,),
                    )
                if c.rowcount == 0:
                    raise HTTPException(status_code=404, detail={"code": 404, "message": "仓库未找到"})
                db.commit()
        except HTTPException:
            raise
        except Exception as e:
            log.warning(f"set_repo_bot_token: clear bot_token failed: {e}")
            raise HTTPException(status_code=500, detail={"code": 500, "message": "清除 Bot Token 失败"})
        # R15.5.5: best-effort 移除旧 bot 协作者身份
        _remove_old_bot_collaborator(repo_key, owner, repo_name, _is_github, user)
        return {"message": "Bot Token 已清除", "bot_username": ""}

    # === R15 七步流程 ===

    # 步骤 1: 检查仓库存在
    try:
        with db_lock:
            c = db.cursor()
            c.execute("SELECT 1 FROM managed_repos WHERE repo_key=? LIMIT 1", (repo_key,))
            if not c.fetchone():
                raise HTTPException(status_code=404, detail={"code": 404, "message": "仓库未找到"})
    except HTTPException:
        raise
    except Exception as e:
        log.warning(f"set_repo_bot_token: check existence failed: {e}")
        raise HTTPException(status_code=500, detail={"code": 500, "message": "仓库查询失败"})

    # 步骤 2: 验证 token 有效性（通过 VCSProvider 接口）
    from src.token_resolver import create_provider_from_repo_key
    _vcs_provider = create_provider_from_repo_key(repo_key)
    try:
        user_data = _vcs_provider.verify_token(body.bot_token)
    except Exception:
        raise HTTPException(status_code=400, detail={
            "code": 400, "error_code": "TOKEN_INVALID",
            "message": "Bot Token 无效，请检查后重试"
        })

    # 步骤 3: 获取 bot username
    repo_bot_username = (user_data.get("login") or "") if isinstance(user_data, dict) else ""
    if not repo_bot_username:
        raise HTTPException(status_code=400, detail={
            "code": 400, "error_code": "TOKEN_INVALID",
            "message": "Token 验证成功但未返回用户名，请检查 Token 有效性"
        })

    # 步骤 4: 邀 bot 为协作者
    # 路径 B 仓库用 host 用户 token 邀请；路径 A 仓库如果新 bot 与旧 bot_username 不同也需邀请
    _old_bot_username = ""
    try:
        with db_lock:
            c = db.cursor()
            c.execute("SELECT bot_username FROM managed_repos WHERE repo_key=? LIMIT 1", (repo_key,))
            _row = c.fetchone()
            if _row and _row[0]:
                _old_bot_username = _row[0]
    except Exception:
        pass

    # 判断是否需要邀请（新 bot 与旧 bot 不同，或无旧 bot）
    _need_invite = (repo_bot_username != _old_bot_username)
    if _need_invite:
        # 获取邀请用 token：路径 B 用 host 用户 token，路径 A 用当前用户 token
        _invite_token = ""
        from src.user.db import get_host_user_id
        _host_uid = get_host_user_id(repo_key)
        if _host_uid > 0:
            # 路径 B: 用 host 用户 token 邀请
            try:
                from src.auth.grape_account_db import get_grape_account_by_id
                _host_acc = get_grape_account_by_id(_host_uid)
                if _host_acc and _host_acc.get("username"):
                    if _is_github:
                        from src.api.deps import get_user_github_token_with_refresh
                        _invite_token = get_user_github_token_with_refresh(_host_acc["username"], _host_uid) or ""
                    else:
                        from src.api.deps import get_user_token_with_refresh
                        _invite_token = get_user_token_with_refresh(_host_acc["username"], _host_uid) or ""
            except Exception:
                pass
            if not _invite_token:
                raise HTTPException(status_code=400, detail={
                    "code": 400, "error_code": "HOST_TOKEN_EXPIRED",
                    "message": "托管用户 Token 已过期，请重新绑定后再配置 Bot Token"
                })
        else:
            # 路径 A: 用当前用户 token 邀请
            if _is_github:
                from src.api.deps import get_user_github_token_with_refresh
                _invite_token = get_user_github_token_with_refresh(user["username"], user_id=user["id"]) or ""
            else:
                from src.api.deps import get_user_token_with_refresh
                _invite_token = get_user_token_with_refresh(user["username"], user.get("id", 0)) or ""

        if _invite_token:
            try:
                _permission = "push" if _is_github else "maintainer"
                _vcs_provider.add_collaborator(_invite_token, owner, repo_name, repo_bot_username, _permission)
            except Exception as _ae:
                # R15.2.3: 邀请失败记日志继续（邀请幂等，bot 可能已是协作者）
                log.warning(f"set_repo_bot_token: add_collaborator failed (continuing): {_ae}")

    # R15.6: best-effort 移除旧 bot 协作者身份（如果有旧 bot 且与新 bot 不同）
    if _old_bot_username and _old_bot_username != repo_bot_username:
        _remove_old_bot_collaborator(repo_key, owner, repo_name, _is_github, user, _old_bot_username)

    # 步骤 5-6: 重建 webhook + 写 DB（委托 update_managed_repo_bot_token）
    ok = update_managed_repo_bot_token(repo_key, body.bot_token)
    if not ok:
        raise HTTPException(status_code=500, detail={
            "code": 500, "error_code": "WEBHOOK_REBUILD_FAILED",
            "message": "Webhook 重建失败，请检查 Bot Token 权限后重试，或使用 repair_host 恢复"
        })

    # 更新 bot_username
    if repo_bot_username:
        update_managed_repo_bot_username(repo_key, repo_bot_username)

    # R17.1.3: PAT 无续期提示
    return {
        "message": "Bot Token 已更新",
        "bot_username": repo_bot_username,
        "pat_no_refresh_hint": "此 Token 为 PAT 类型，不会自动续期，过期后需手动重新配置",
    }


def _remove_old_bot_collaborator(repo_key: str, owner: str, repo_name: str,
                                  is_github: bool, user: dict, old_bot_username: str = ""):
    """R15.6: best-effort 移除旧 bot 协作者身份。

    只移除该仓库的旧 bot 协作者（R15.6.1），不移除其他仓库的旧 bot（R15.6.2）。
    移除失败不影响新 token 写入（R15.6.3 best-effort）。
    """
    if not old_bot_username:
        # 查 DB 获取旧 bot_username
        try:
            from src.session_manager import db, db_lock
            with db_lock:
                c = db.cursor()
                c.execute("SELECT bot_username FROM managed_repos WHERE repo_key=? LIMIT 1", (repo_key,))
                _row = c.fetchone()
                if _row and _row[0]:
                    old_bot_username = _row[0]
        except Exception:
            return
    if not old_bot_username:
        return
    try:
        from src.token_resolver import create_provider_from_repo_key
        _provider = create_provider_from_repo_key(repo_key)
        # 移除旧 bot 需要用 owner token（有仓库管理权限的 token）
        _remove_token = ""
        if is_github:
            from src.api.deps import get_user_github_token_with_refresh
            _remove_token = get_user_github_token_with_refresh(user["username"], user_id=user["id"]) or ""
        else:
            from src.api.deps import get_user_token_with_refresh
            _remove_token = get_user_token_with_refresh(user["username"], user.get("id", 0)) or ""
        if _remove_token:
            _provider.remove_collaborator(_remove_token, owner, repo_name, old_bot_username)
    except Exception as _re:
        log.warning(f"_remove_old_bot_collaborator: failed (best-effort): {_re}")


class DeployUrlBody(BaseModel):
    full_name: str
    deploy_url: str = ""


@router.put("/deploy-url", operation_id="repos_deploy_url_set", summary="Set deployment URL", description="Registers the deployed access URL of the business system.")
def set_deploy_url(body: DeployUrlBody, user: dict = Depends(require_no_session_token)):
    """登记业务系统部署后的访问 URL"""
    from src.repo_key import to_repo_key
    from src.session_manager import db, db_lock
    repo_key = to_repo_key(body.full_name)
    from src.api.deps import check_repo_access
    check_repo_access(user, repo_key)
    with db_lock:
        c = db.cursor()
        c.execute("UPDATE managed_repos SET deploy_url=? WHERE repo_key=?", (body.deploy_url or "", repo_key))
        if c.rowcount == 0:
            raise HTTPException(status_code=404, detail={"code": 404, "message": "仓库未找到"})
        db.commit()
    return {"message": "部署 URL 已更新", "deploy_url": body.deploy_url}


@router.get("/deploy-url/{owner}/{repo_name}", operation_id="repos_deploy_url", summary="Get repository deployment URL", description="Returns the repository deployment access URL.")
def get_deploy_url(owner: str, repo_name: str, user: dict = Depends(require_no_session_token)):
    """获取仓库的部署访问 URL"""
    from src.repo_key import to_repo_key
    from src.session_manager import db, db_lock
    repo_key = to_repo_key(f"{owner}/{repo_name}")
    with db_lock:
        c = db.cursor()
        c.execute("SELECT deploy_url FROM managed_repos WHERE repo_key=?", (repo_key,))
        row = c.fetchone()
    return {"deploy_url": (row[0] if row else "") or ""}


def _check_unmanage_vcs_permission(user: dict, owner: str, repo_name: str, repo_provider: str) -> None:
    """解除托管救火闸门：校验发起人在 VCS 侧的仓库权限，maintainer 及以上才放行。

    背景（docs/weekly-08/unmanage-permission-guard-design.md 救火项）：
    agent 曾按「解除所有托管」指令遍历 /repos/remove，把共同参与甚至从未工作过的
    仓库也解除了。读权限（能看到）≠ 写权限（能解除），本闸门在 VCS 侧独立复核。

    规则（与托管侧 _check_collaborator_admin 同口径）：
    - GitCode：access_level>=40（Maintainer/Admin）或单数 permission=="admin"
    - GitHub：role_name 为 admin / maintain
    - 仓库所有者（username==owner）天然通过
    失败不兜底：token 缺失 / API 不可达 / 无法判定 → 一律 403 拒绝，绝不放行。
    """
    username = (user.get("username") or "").strip()
    if not username:
        raise HTTPException(status_code=403, detail={
            "code": 403, "message": "无法确定当前用户身份，拒绝解除托管（maintainer 及以上权限方可操作）"})
    # 仓库所有者天然有完全权限
    if username == owner:
        return
    if repo_provider == "github":
        from src.api.deps import get_user_github_token_with_refresh
        gh_token = get_user_github_token_with_refresh(username, user_id=user.get("id", 0))
        if not gh_token:
            raise HTTPException(status_code=403, detail={
                "code": 403, "message": "未绑定 GitHub 授权，无法校验仓库权限，拒绝解除托管"})
        from src.user.repo_manager import get_github_collaborator_permission
        role = (get_github_collaborator_permission(gh_token, owner, repo_name, username) or "").lower()
        if role not in ("admin", "maintain"):
            raise HTTPException(status_code=403, detail={
                "code": 403, "message": f"解除托管需要 GitHub maintainer 及以上权限（当前角色：{role or '未知'}）"})
        return
    # GitCode（默认）
    from src.api.deps import get_user_token_with_refresh
    gc_token = get_user_token_with_refresh(username, user.get("id", 0))
    if not gc_token:
        raise HTTPException(status_code=403, detail={
            "code": 403, "message": "未绑定 GitCode 授权，无法校验仓库权限，拒绝解除托管"})
    from src.user.repo_manager import get_member_permission, _RateLimitedError
    try:
        data = get_member_permission(gc_token, owner, repo_name, username) or {}
    except _RateLimitedError:
        raise HTTPException(status_code=429, detail={
            "code": 429, "message": "GitCode 权限校验被限流，请稍后重试（未解除任何托管）"})
    if not data:
        raise HTTPException(status_code=403, detail={
            "code": 403, "message": "无法校验 GitCode 仓库权限（非协作者或 API 失败），拒绝解除托管"})
    try:
        access_level = int(data.get("access_level", 0) or 0)
    except (TypeError, ValueError):
        access_level = 0
    if access_level >= 40:  # Maintainer(40) / Admin(>=50)
        return
    if str(data.get("permission", "") or "").lower() == "admin":
        return
    role_name = str(data.get("role_name", "") or "") or "未知"
    raise HTTPException(status_code=403, detail={
        "code": 403, "message": f"解除托管需要 GitCode maintainer 及以上权限（当前角色：{role_name}，access_level={access_level}）"})


def _cleanup_repo_data(repo_key: str, owner: str, repo: str) -> dict:
    """解除托管时的同步清理：关联数据表 + 仓库 skill 目录 + workspace 目录。

    Issue #340：升级为级联清理（issue/issue_state/agent_tasks/devops_pipeline_scene 等
    全量关联数据 + 事务化 + 审计日志），统一入口见 src/repo_cleanup.py。
    本函数保留为兼容包装（历史调用方/测试）；DB 事务失败时降级为旧行为（记日志不抛出）。

    repo_key 为 owner/repo（无前缀）；兼容历史可能存在的 gitcode: 前缀数据。
    用户维度数据（workspaces/chat/{user_id}、workspaces/{owner} 根）不清理。
    返回清理统计。
    """
    from src.repo_cleanup import cascade_cleanup_repo_data, RepoCleanupError
    try:
        return cascade_cleanup_repo_data(repo_key, owner, repo, trigger="legacy")
    except RepoCleanupError:
        # 事务已回滚：返回空统计（与旧行为一致，不产生半清理）
        return {"tables": 0, "skill_dirs": 0, "workspace_dirs": 0,
                "dry_run": False, "trigger": "legacy"}


@router.post("/remove", operation_id="repos_remove", summary="Remove hosting", description="Removes hosting for a repository (unregisters webhook/bot bindings).")
def remove_managed_repo(body: RepoAction, user: dict = Depends(require_no_session_token)):
    from src.repo_key import to_repo_key
    parts = body.full_name.split("/", 1)
    if len(parts) != 2:
        raise HTTPException(status_code=400, detail={"code": 400, "message": "无效的仓库标识"})
    owner, repo_name = parts

    # 按 provider 分流：前端传的 full_name 不含前缀
    # 先尝试 gitcode:（默认），不存在再尝试 github:
    from src.repo_key import to_repo_key as _rk
    gc_key = to_repo_key(body.full_name)  # 默认 gitcode:
    gh_key = _rk(body.full_name, provider="github")

    # 判断实际存在的 repo_key（直接查 managed_repos 表）
    repo_key = gc_key
    repo_provider = "gitcode"
    _repo_exists = False
    try:
        from src.session_manager import db, db_lock
        with db_lock:
            c0 = db.cursor()
            c0.execute("SELECT repo_key FROM managed_repos WHERE repo_key IN (?, ?)", (gh_key, gc_key))
            row0 = c0.fetchone()
            if row0 and row0[0].startswith("github:"):
                repo_key = gh_key
                repo_provider = "github"
            _repo_exists = row0 is not None
    except Exception:
        pass  # 测试环境（mock）走默认 gitcode:

    # 不存在的仓库直接 404（避免对未托管仓库做无意义的 VCS 权限调用）
    if not _repo_exists:
        raise HTTPException(status_code=404, detail={"code": 404, "message": "仓库未找到"})

    # 救火闸门（docs/weekly-08/unmanage-permission-guard-design.md）：
    # 解除托管前校验发起人在 VCS 侧的仓库权限，maintainer 及以上才放行。
    # 必须放在 token 分流/删 webhook 等任何副作用之前。
    _check_unmanage_vcs_permission(user, owner, repo_name, repo_provider)

    # token 按 provider 分流
    if repo_provider == "github":
        from src.api.deps import get_user_github_token_with_refresh
        api_token = get_user_github_token_with_refresh(user["username"], user_id=user["id"])
        if not api_token:
            api_token = get_bot_token(repo_owner=owner, repo_name=repo_name)
        if not api_token:
            # Issue #345: 改用统一入口
            from src.user.db import get_global_github_bot_token
            api_token = get_global_github_bot_token()
    else:
        api_token = get_user_token_with_refresh(user["username"], user.get("id", 0))
        if not api_token:
            api_token = get_bot_token(repo_owner=owner, repo_name=repo_name)
        if not api_token:
            # Issue #345: 改用统一入口
            from src.user.db import get_global_gitcode_bot_token
            api_token = get_global_gitcode_bot_token()

    # 删除 webhook（按 provider 选择 API）
    if api_token:
        try:
            if repo_provider == "github":
                from src.user.repo_manager import _get_managed_webhook, remove_github_webhook
                wh_id = _get_managed_webhook(repo_key)
                if wh_id:
                    remove_github_webhook(api_token, owner, repo_name, wh_id)
            else:
                from src.user.repo_manager import _get_managed_webhook, remove_webhook
                wh_id = _get_managed_webhook(repo_key)
                if wh_id:
                    remove_webhook(api_token, owner, repo_name, wh_id)
        except Exception:
            pass

    # 解除托管：移除 bot 协作者身份（失败仅记日志）
    try:
        import json, os as _os
        # 读取该仓库配置的 repo bot（先取，再删 managed_repos）
        repo_bot_token = ""
        repo_bot_username = ""
        for _r in user_db.get_managed_repos(user["id"]):
            if _r["repo_key"] == repo_key:
                repo_bot_token = _r.get("bot_gitcode_token", "") or _r.get("bot_github_token", "") or ""
                repo_bot_username = _r.get("bot_username", "") or ""
                break
        # 全局 bot 用户名
        # Issue #345: 改用统一入口
        from src.user.db import get_global_bot_usernames, get_global_github_bot_username
        global_bot_username = ""
        if repo_provider == "github":
            global_bot_username = get_global_github_bot_username()
        else:
            _us = get_global_bot_usernames()
            global_bot_username = _us[0] if _us else ""
        # owner token 按 provider 分流
        if repo_provider == "github":
            from src.api.deps import get_user_github_token_with_refresh
            owner_token = get_user_github_token_with_refresh(user["username"], user_id=user["id"])
        else:
            owner_token = get_user_token_with_refresh(user["username"], user.get("id", 0))
        # Issue #372: 路径 B 仓库（无 bot token）跳过 bot 协作者移除——托管时未邀请 bot
        from src.user.db import get_host_user_id
        _is_path_b = (not repo_bot_token and not global_bot_username and get_host_user_id(repo_key) > 0)
        if not _is_path_b:
            if owner_token and global_bot_username:
                try:
                    if repo_provider == "github":
                        from src.user.repo_manager import remove_github_collaborator
                        _rm_ok = remove_github_collaborator(owner_token, owner, repo_name, global_bot_username)
                    else:
                        from src.user.repo_manager import remove_collaborator
                        _rm_ok = remove_collaborator(owner_token, owner, repo_name, global_bot_username)
                    if not _rm_ok:
                        log.warning(f"Remove global bot collaborator failed (perm?): {global_bot_username} @ {owner}/{repo_name}")
                except Exception as e:
                    log.warning(f"Remove global bot collaborator error: {e}")
            # repo bot（若配置且非全局 bot）
            if owner_token and repo_bot_username and repo_bot_username != global_bot_username:
                try:
                    if repo_provider == "github":
                        from src.user.repo_manager import remove_github_collaborator
                        _rm_ok2 = remove_github_collaborator(owner_token, owner, repo_name, repo_bot_username)
                    else:
                        from src.user.repo_manager import remove_collaborator
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
    # Issue #340 级联清理：需求中心 Issue 卡片、流水线/事件、知识基线、场景关联、
    # 会话/技能/IM 绑定等全部关联数据，事务化 + 审计日志；失败可重试且无半清理。
    from src.repo_cleanup import cascade_cleanup_repo_data, RepoCleanupError
    try:
        _stats = cascade_cleanup_repo_data(repo_key, owner, repo_name,
                                           trigger="platform",
                                           operator_id=user.get("id", 0) or 0)
        log.info(f"repos/remove: cascade cleanup {repo_key} done: {_stats}")
    except RepoCleanupError as e:
        # DB 事务已回滚（managed_repos 移除独立提交，可重试：再次解除托管会 404，
        # 故返回明确错误提示用户重试清理）
        raise HTTPException(status_code=500, detail={
            "code": 500, "message": f"关联数据清理失败（已回滚，无半清理），请重试: {e}"})
    return {"message": "已移除仓库，并已清空全部相关数据"}


@router.get("/{owner}/{repo}/project-info", operation_id="repos_project_info", summary="Get project knowledge", description="Reads the authoritative project-knowledge baseline for a repository: files (full text) + meta (commit/branches/updated/degraded). Dual: user (JWT/API key) and session token (tk_, repo scope must match).")
def get_repo_project_info(owner: str, repo: str, user: dict = Depends(get_current_user_or_api_key)):
    """读取仓库的权威项目知识基线（repo 全局，多机共享，见 multi-issue-isolation-design.md §2.4）。

    返回 files（全文）+ meta（commit/branches/updated/degraded）。无基线 → 空 files + meta。

    #219 按需获取：支持会话 token（tk_）——要求仓库作用域匹配（防绑 A 读 B）；
    JWT / API Key 用户维持现状（依赖 x-token-scope 的 user 通道）。
    """
    from src.repo_key import split_owner_repo, to_repo_key
    from src.runtime.service import load_project_info
    repo_key = to_repo_key(f"{owner}/{repo}")
    if user.get("auth_type") == "session_token":
        _bound_owner, _bound_repo = split_owner_repo(user.get("repo_key") or "")
        if _bound_owner and (_bound_owner != owner or _bound_repo != repo):
            raise HTTPException(status_code=403, detail={"code": 403, "message": "无权读取该项目知识（仓库作用域不匹配）"})
    files, meta = load_project_info(repo_key)
    return {"repo_key": repo_key, "files": files, "meta": meta}


class SceneRecommendConfigBody(BaseModel):
    fallback_scene: str = ""
    rules: list = []  # [{"scene_id": "", "condition": ""}]


@router.get("/{owner}/{repo}/scene-recommend-config", operation_id="repos_scene_recommend_config_get", summary="Get scene recommend config", description="Reads the repository smart scene-recommend config (fallback scene + routing rules).")
def get_scene_recommend_config(owner: str, repo: str, user: dict = Depends(require_no_session_token)):
    """读取仓库智能推荐配置（兜底场景 + 路由规则）。"""
    from src.repo_key import to_repo_key
    from src.user.db import get_scene_recommend_config
    repo_key = to_repo_key(f"{owner}/{repo}")
    return get_scene_recommend_config(repo_key)


@router.put("/{owner}/{repo}/scene-recommend-config", operation_id="repos_scene_recommend_config_set", summary="Save scene recommend config", description="Saves the repository scene-recommend config. Self-checks on write: filters out rules whose scene_id is not in the current scene list; fallback_scene is required.")
def save_scene_recommend_config(owner: str, repo: str, body: SceneRecommendConfigBody,
                                user: dict = Depends(require_no_session_token)):
    """保存仓库智能推荐配置（兜底场景 + 路由规则）。

    写入时自检：过滤 scene_id 不在当前场景列表中的规则。
    fallback_scene 必填，空则报错。
    """
    from src.repo_key import to_repo_key
    from src.user.db import set_scene_recommend_config
    from src.api.pipeline_config import get_scenes
    repo_key = to_repo_key(f"{owner}/{repo}")

    # 校验 fallback_scene 非空
    if not body.fallback_scene or not body.fallback_scene.strip():
        raise HTTPException(status_code=400, detail={"code": 400, "message": "兜底场景不能为空"})

    # 校验规则 key 不重复
    seen = set()
    for r in (body.rules or []):
        sid = (r.get("scene_id") or "").strip() if isinstance(r, dict) else ""
        if sid in seen:
            raise HTTPException(status_code=400, detail={"code": 400, "message": f"场景 '{sid}' 重复"})
        seen.add(sid)

    set_scene_recommend_config(repo_key, body.fallback_scene.strip(), body.rules or [])
    return {"message": "智能推荐规则已保存"}


# ===================== git 传输支撑（方案③，grape 平台私有能力）=====================
# git-credential / workspace 是 grape 自身能力（非 gitcode/github/gitlab 平台原生），
# 故置于 /api/repos（grape 平台业务）而非 /api/git-repo（平台透传）。

_SUPPORTED_PROVIDERS = {"gitcode", "github"}
_GITCODE_API_BASE = "https://gitcode.com/api/v5"
_GITHUB_API_BASE = "https://api.github.com"
_CREDENTIAL_TTL = 600


def _repo_provider(user: dict = Depends(get_current_user_or_api_key)) -> str:
    """Git provider（gitcode/github）：由认证 token 的 repo_key 前缀派生（见 git-repo-provider-from-token-design.md）。"""
    from src.repo_key import parse_repo_key
    p = parse_repo_key(user.get("repo_key") or "")[0] or "gitcode"
    if p not in _SUPPORTED_PROVIDERS:
        raise HTTPException(status_code=403, detail={"code": 403, "message": f"Git provider 未接入: {p}"})
    return p


def _repo_platform_token(owner: str, repo: str, user: dict, provider: str = "gitcode") -> str:
    """平台 token 解析（仅会话级临时 token tk_；用户 API Key / Web JWT 不适用）。

    chat agent → 用户 token；devops 事件 agent → 仓库 bot token；一种缺失交叉回退；均缺失 502。
    """
    if user.get("auth_type") != "session_token":
        raise HTTPException(status_code=403, detail={"code": 403, "message": "Git 仓库接口仅支持会话级临时 token（agent 专用），用户 API Key / Web 会话不适用"})
    from src.repo_key import to_repo_key

    if provider == "github":
        rk = to_repo_key(f"github:{owner}/{repo}")
        def _bot():
            try:
                from src.user.db import get_github_token
                return get_github_token(repo_key=rk) or ""
            except Exception:
                return ""
        def _user():
            try:
                from src.api.deps import get_user_github_token_with_refresh
                username = user.get("username") or ""
                if not username and user.get("id", 0) > 0:
                    from src.auth.grape_account_db import get_grape_account_by_id
                    username = (get_grape_account_by_id(user["id"]) or {}).get("username", "")
                return get_user_github_token_with_refresh(username, user_id=user_id) or ""
            except Exception:
                return ""
    else:
        rk = to_repo_key(f"{owner}/{repo}")
        def _bot():
            try:
                from src.user.db import get_gitcode_token
                return get_gitcode_token(repo_key=rk) or ""
            except Exception:
                return ""
        def _user():
            try:
                from src.api.deps import get_user_token_with_refresh
                uid = user.get("id") or 0
                username = user.get("username") or ""
                if not username and uid > 0:
                    from src.auth.grape_account_db import get_grape_account_by_id
                    username = (get_grape_account_by_id(uid) or {}).get("username", "")
                return get_user_token_with_refresh(username, user_id=uid)
            except Exception:
                return ""

    if user.get("agent_type") == "chat":
        tok = _user()
        if not tok:
            tok = _bot()
    else:
        tok = _bot()
        if not tok:
            tok = _user()
    if tok:
        return tok
    _vcs = "GitHub" if provider == "github" else "GitCode"
    raise HTTPException(status_code=502, detail={"code": 502, "message": f"{_vcs} 仓库 token 解析失败"})


@router.get("/{owner}/{repo}/git-credential", operation_id="repos_git_credential",
            summary="One-shot git platform credential (CLI internal, TTL)",
            description="One-shot short-lived platform credential for local git via CLI (in-process, never persisted). "
                        "gitcode uses Basic auth with username x-access-token and the configured gitcode token as password. Session-only.")
def get_repo_git_credential(owner: str, repo: str, user: dict = Depends(get_current_user_or_api_key),
                            p: str = Depends(_repo_provider)):
    """返回 CLI 本地执行 git 所需的平台凭据（一次性/短期，进程内使用不落盘）。

    gitcode：Basic 用 username=`x-access-token`、password=配置的 gitcode token。
    """
    token = _repo_platform_token(owner, repo, user, p)
    _gh = "github.com" if p == "github" else "gitcode.com"
    return {
        "provider": p,
        "username": "x-access-token",
        "password": token,
        "clone_url": f"https://{_gh}/{owner}/{repo}.git",
        "expires_in": _CREDENTIAL_TTL,
    }


@router.get("/{owner}/{repo}/workspace", operation_id="repos_workspace", summary="Repository workspace path (local git via CLI)", description="Returns the grape-managed local workspace path for the repository (CLI local git on the server). Session-only.")
def get_repo_workspace(owner: str, repo: str, user: dict = Depends(get_current_user_or_api_key),
                       p: str = Depends(_repo_provider)):
    ws = os.path.join(WORKSPACE_DIR, owner, repo)
    _gh = "github.com" if p == "github" else "gitcode.com"
    return {"workspace": ws, "clone_url": f"https://{_gh}/{owner}/{repo}.git"}
