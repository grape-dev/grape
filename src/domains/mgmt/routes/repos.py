"""仓库路由：列表、托管、移除、同步 + git 平台凭据/工作区（方案③ CLI 支撑）"""

import os

import re

from fastapi import APIRouter, HTTPException, Depends, Request

from pydantic import BaseModel

from src.kernel.config import log, WORKSPACE_DIR

from src.domains.mgmt.user import db as user_db

from src.domains.mgmt.user.repo_manager import fetch_repos_with_role, setup_webhook, find_existing_webhook, create_repo

from src.api.deps import get_current_user_or_api_key, get_bot_token, get_user_token, get_user_token_for, require_no_session_token, require_org_member

from src.api.oauth_token import with_oauth_token

from src.api.org_context import resolve_org_context

from src.domains.mgmt.user.db import update_managed_repo_bot_token, update_managed_repo_bot_username, get_bot_token_by_repo

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

class MigrateRepoBody(BaseModel):
    repo_key: str  # 源 repo_key（带 provider 前缀，如 gitcode:ownerA/repo）
    target_owner: str  # 目标组织（VCS 侧已转移后的新 owner）
    target_repo: str = ""  # 目标仓库名（默认与源同名）

def _get_user_provider_and_token(username: str, login_provider: str = "", user_id: str = "") -> tuple[str, str]:
    """根据用户已绑定的令牌自动选择 provider，返回 (provider, token)。

    D30/S50：统一走 get_user_token_for（provider 无关，预刷新语义一致——
    原 gitcode 分支的 gitcode 专用 token 入口即其薄委托，S75 后统一 get_user_token_for_repo）。顺序：
    login_provider（任意已配平台，含第 5 平台）优先，然后 github、gitcode
    （存量 github 优先级保持，host-repo-provider-routing-fix-design.md）。
    """
    from src.domains.runtime.vcs.platform_card import list_vcs_platforms
    try:
        _platforms = set(list_vcs_platforms())
    except Exception:
        _platforms = {"gitcode", "github"}
    _order: list[str] = []
    if login_provider and login_provider in _platforms:
        _order.append(login_provider)
    for _p in ("github", "gitcode"):
        if _p not in _order:
            _order.append(_p)
    for _p in _order:
        try:
            _tok = get_user_token_for(username, user_id=user_id, provider=_p)
        except Exception:
            _tok = ""
        if _tok:
            return _p, _tok
    return "", ""

def _provider_from_hint(full_name: str) -> str:
    """按「仓库来源」提示判断 provider（D30/S50：候选集抽象，双前缀硬编码消除）。

    full_name 带已配平台前缀 → 直接取前缀；裸名 → repo_key_candidates
    全平台候选查 managed_repos，命中行前缀即 provider。返回 provider 名
    或 ''（无来源线索）。这是托管路由的主要信息来源，避免再被「登录平台」
    短路误判（host-repo-provider-routing-fix-design.md）。
    """
    f = (full_name or "").strip()
    if not f:
        return ""
    from src.domains.runtime.vcs.platform_card import list_vcs_platforms
    try:
        _platforms = set(list_vcs_platforms())
    except Exception:
        _platforms = {"gitcode", "github"}
    if ":" in f:
        _pre, _, _rest = f.partition(":")
        if _pre in _platforms:
            return _pre
        f = _rest  # 兼容已有前缀写法（未知前缀剥离后按裸名处理）
    base = f.split(":", 1)[-1]
    if not base:
        return ""
    from src.kernel.repo_key import repo_key_candidates
    _cands = [c for c in repo_key_candidates(base) if c != base and ":" in c]
    if not _cands:
        return ""
    try:
        from src.kernel.session_manager import db, db_lock
        _ph = ",".join("?" * len(_cands))
        with db_lock:
            rows = db.cursor().execute(
                f"SELECT repo_key FROM managed_repos WHERE repo_key IN ({_ph}) LIMIT 1",
                tuple(_cands)).fetchall()
        for r in rows:
            _pre = (r[0] or "").split(":", 1)[0]
            if _pre in _platforms:
                return _pre
    except Exception:
        pass
    return ""

def _provider_from_token(user: dict) -> tuple[str, str]:
    """托管兜底：按「平台 token 是否可用」选择 provider（github 优先），
    不按登录平台短路（登录平台 ≠ 仓库平台，见 host-repo-provider-routing-fix-design.md）。
    D30/S50：统一委托 _get_user_provider_and_token（get_user_token_for 无平台分支）。"""
    return _get_user_provider_and_token(user["username"], "", str(user.get("id") or ""))

def _fetch_and_cache(user_id: str, api_token: str, provider: str = "gitcode") -> list[dict]:
    """从 VCS 拉取仓库列表，返回结果（不写缓存——由调用方合并后一次性写入）

    D42/S62：拉取走适配器端口 fetch_repos_with_role（存量三平台=委托
    repo_manager 原实现，config-only 平台=卡端点 GET /user/repos），
    调用方零平台分支。
    """
    from src.kernel.token_resolver import create_provider
    repos = create_provider(provider).fetch_repos_with_role(api_token) or []
    # FR-5.4: 空结果可能是 token scope 受限，记 warning
    if not repos:
        log.warning(f"fetch_repos_with_role returned empty for user (token may lack repo read scope)")
    for r in repos:
        if not r.get("repo_key"):
            r["repo_key"] = f"{provider}:{r.get('full_name', '')}"
    return repos

def _filter_repos(user_id: str, repos: list[dict], org_id: str = "") -> list[dict]:
    """过滤仓库列表：返回用户身份可见的全部仓库（管理员 + 非管理员成员）。

    org_id != '' 时组织隔离：
    - 已托管到当前组织的仓库 → 显示，is_hosted=true
    - 已托管到其他组织的仓库 → 隐藏（隔离）
    - 未托管的仓库 → 显示，用户可托管到当前组织
    """
    from src.kernel.repo_key import to_repo_key
    # 获取所有托管仓库及其 org_id
    from src.kernel.session_manager import db as _db, db_lock as _dbl
    managed = {}
    managed_org = {}
    with _dbl:
        _c = _db.cursor()
        try:
            _c.execute("SELECT repo_key, webhook_id, org_id FROM managed_repos")
            for _r in _c.fetchall():
                managed[_r[0]] = _r[1]
                managed_org[_r[0]] = _r[2] or ""
        except Exception:
            # 降级：不带 org_id
            for r in user_db.get_all_managed_repos():
                managed[r["repo_key"]] = r["webhook_id"]
                managed_org[r["repo_key"]] = ""
    result = []
    for r in repos:
        full_name = r.get("full_name", "")
        repo_key = to_repo_key(r.get("repo_key", "") or full_name)
        is_admin = r.get("is_admin", False)
        in_managed = repo_key in managed
        hosted = in_managed and bool(managed.get(repo_key))
        repo_org_id = managed_org.get(repo_key, "")

        # 组织隔离：
        # - org_id != ''（非个人组织）：已托管到其他组织的仓库不显示
        # - org_id = ''（个人组织）：已托管到其他非个人组织的仓库不显示
        #   （个人组织只显示全局托管 + 自身托管的仓库，不显示其他非个人组织的仓库）
        # D42: 组织已删除时（org 不存在），不过滤该仓库（降级为未托管状态）
        if in_managed and repo_org_id:
            if org_id and repo_org_id != org_id:
                continue
            if not org_id:
                # 个人组织：检查 repo_org_id 对应的组织是否是个人组织
                from src.domains.mgmt.user.org_db import is_org_personal as _is_p
                if not _is_p(repo_org_id):
                    # D42: 检查组织是否仍然存在——已删除的组织不隔离
                    from src.domains.mgmt.user.org_db import get_org as _get_org
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

def _probe_stale_repos(api_token: str, user_id: str):
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
    from src.kernel.repo_key import parse_repo_key

    all_repos = user_db.get_managed_repos(user_id)
    if not all_repos:
        return

    # FR-1.3/FR-20.3: token 能力前置检查——探活前验证 token 有效
    # 401 → 放弃整轮并标记"token 失效"；不做任何判定
    # D42/S62：探活 token 按平台组织（键=平台名，数据非分支）；能力前置
    # 检查（FR-1.3/FR-20.3/FR-8）走卡派生 /user，401 → 清空该平台 token
    _probe_tokens = {}
    if api_token:
        # 主 token（调用方传入的存量用户 token）按探活平台优先级归属
        _probe_tokens[_probe_provider_for(api_token)] = api_token
    try:
        from src.api.deps import get_user_token_for
        from src.domains.mgmt.auth.grape_account_db import get_grape_account_by_id
        acc = get_grape_account_by_id(user_id) or {}
        _gh_tok = get_user_token_for(acc.get("username", ""), user_id=user_id, provider="github")
        if _gh_tok:
            _probe_tokens["github"] = _gh_tok
    except Exception:
        pass

    for _p in list(_probe_tokens.keys()):
        _u, _h = _probe_url_headers(_p, _probe_tokens[_p], "/user")
        if not _u:
            _probe_tokens.pop(_p)
            continue
        try:
            _req = urllib.request.Request(_u, headers=_h, method="GET")
            with urllib.request.urlopen(_req, timeout=5) as _resp:
                pass  # 200 = token 有效
        except urllib.error.HTTPError as _he:
            if _he.code == 401:
                log.warning(f"_probe_stale_repos: {_p} token invalid (401) for user {user_id}, {_p} repos will be skipped")
                _probe_tokens.pop(_p)  # 清空，该平台仓跳过
            # 其他错误不阻断
        except Exception:
            pass  # 网络异常不阻断

    # 第一步：并发查仓库是否存在（404 = 已删除）
    def _check_exists(item):
        r = item
        owner = r.get("repo_owner", "")
        repo_name = r.get("repo_name", "")
        if not owner or not repo_name:
            return None
        repo_key = r["repo_key"]
        provider, _, _ = parse_repo_key(repo_key)
        # D42/S62：探活按平台 token map + 卡派生 URL（无该平台 token / 无卡 →
        # 跳过不误判——D24 语义保持：config-only 平台未配探活 token 同样跳过）
        _tok = _probe_tokens.get(provider)
        if not _tok:
            return None
        url, hdrs = _probe_url_headers(provider, _tok, f"/repos/{owner}/{repo_name}")
        if not url:
            return None
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
    # FR-1.1: hooks 空列表判"不确定"跳过（可能是 token 权限不足，不是 webhook 丢失）
    webhook_lost_keys = []  # FR-2.1: webhook 丢失不删数据，与 deleted 分开处理
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
        # D42/S62：hooks 查询按平台 token map + 卡派生 URL（无 token 跳过）
        _tok = _probe_tokens.get(provider)
        if not _tok:
            continue
        url, hdrs = _probe_url_headers(provider, _tok, f"/repos/{owner}/{repo_name}/hooks")
        if not url:
            continue
        try:
            req = urllib.request.Request(url, headers=hdrs, method="GET")
            with urllib.request.urlopen(req, timeout=5) as resp:
                hooks = _json.loads(resp.read().decode() or "[]")
            hook_ids = {h.get("id") for h in hooks if isinstance(h, dict)} if isinstance(hooks, list) else set()
            # FR-1.1: 空列表 = 不确定（token 权限不足或无 hooks），跳过不判
            if not hook_ids:
                log.debug(f"managed repos: hooks list empty for {repo_key}, uncertain (skip)")
                continue
            if expected_wh not in hook_ids:
                webhook_lost_keys.append((repo_key, owner, repo_name))
                log.debug(f"managed repos: webhook {expected_wh} not found for {repo_key}, marking pending-repair")
        except urllib.error.HTTPError as e:
            # 403 = 非 owner 无权限查 hooks，跳过（不误判）
            # D42/S62：404 语义行为派生（不按平台名分支）——重查仓库本体：
            #   200 → 权限型 404（如 GitHub 惯例：不可见即 404）→ 跳过不判 stale
            #   404 → 仓库此刻被删（刚才 200 现在变 404）→ stale
            # 对 GitHub/GitCode 与旧语义等价，且对 config-only 平台同样成立。
            if e.code == 404:
                _rurl, _rhdrs = _probe_url_headers(provider, _tok, f"/repos/{owner}/{repo_name}")
                _still_exists = False
                if _rurl:
                    try:
                        _req2 = urllib.request.Request(_rurl, headers=_rhdrs, method="GET")
                        with urllib.request.urlopen(_req2, timeout=5) as _resp2:
                            _still_exists = True
                    except urllib.error.HTTPError as _e2:
                        _still_exists = (_e2.code != 404)
                    except Exception:
                        _still_exists = True  # 网络异常不判定
                if _still_exists:
                    log.debug(f"probe: {repo_key} hooks 404 with repo still present, skip (permission issue)")
                    continue
                stale_keys.append((repo_key, owner, repo_name))
            # 其他错误跳过
        except Exception:
            pass  # 超时/网络抖动，不判定

    # FR-1.2: 仓库 404 双信号交叉验证——用 bot token 二次确认
    # 用户 token 404 可能是权限问题而非仓库真删了，bot token 是 maintainer 有读权限
    if stale_keys:
        from src.kernel.token_resolver import TokenResolver, create_provider
        verified_deletes = []
        for repo_key, owner, repo_name in stale_keys:
            provider, _, _ = parse_repo_key(repo_key)
            try:
                resolver = TokenResolver(create_provider(provider))
                bot_token = resolver.read_stored_token(repo_key)  # 不检查过期
            except Exception:
                bot_token = ""
            if bot_token:
                try:
                    url, _bhdrs = _probe_url_headers(provider, bot_token, f"/repos/{owner}/{repo_name}")
                    if not url:
                        continue
                    _bhdrs = dict(_bhdrs or {})
                    _bhdrs.setdefault("Authorization", f"Bearer {bot_token}")
                    req = urllib.request.Request(url, headers=_bhdrs, method="GET")
                    with urllib.request.urlopen(req, timeout=5) as resp:
                        pass  # 200 = 仓库仍存在，用户 token 404 是权限问题
                        log.info(f"probe: {repo_key} 404 with user token but 200 with bot token, skip (permission issue)")
                        continue
                except urllib.error.HTTPError as e2:
                    if e2.code == 404:
                        verified_deletes.append((repo_key, owner, repo_name))  # 双信号确认删除
                    # 其他错误跳过不判
                except Exception:
                    pass  # 不判定
            else:
                # 无 bot token，无法二次验证——不确定不能删：降级为待修复状态而非级联清理
                webhook_lost_keys.append((repo_key, owner, repo_name))
                log.warning(f"probe: {repo_key} 404 unverified (no bot token for cross-check), downgrade to pending-repair")
        stale_keys = verified_deletes

    # FR-2.3: 单轮清理上限——stale > 2 时整轮放弃（保险丝，防"一夜清库"）
    if len(stale_keys) > 2:
        log.warning(f"probe: {len(stale_keys)} stale repos in one round, abort entire round (safety fuse)")
        # webhook 丢失也不处理（token 可能有问题，全部保守跳过）
        return

    # FR-2.1: webhook 丢失（仓库仍存在）→ 置"待修复"状态，不删除任何数据
    for repo_key, owner, repo_name in webhook_lost_keys:
        log.info(f"managed repos: webhook lost for {repo_key}, set pending-repair (no data deletion)")
        try:
            user_db.set_host_status(repo_key, "webhook_missing")
        except Exception:
            log.warning(f"managed repos: failed to set pending-repair for {repo_key}")

    # FR-2.2: 仓库删除（双信号确认）→ 级联清理 + 审计日志
    for repo_key, owner, repo_name in stale_keys:
        # FR-20.14: 清理前审计日志（含两路信号：用户 token 404 + bot token 404）
        log.info(f"managed repos: CLEANUP_START repo={repo_key} owner={owner}/{repo_name} "
                 f"trigger=polling-confirmed signals=[user_token_404, bot_token_404]")
        # Issue #340：级联清空全部关联数据（含需求中心 Issue 卡片/流水线/场景关联）
        from src.kernel.repo_cleanup import cascade_cleanup_repo_data, RepoCleanupError
        try:
            cascade_cleanup_repo_data(repo_key, owner, repo_name, trigger="polling-confirmed")
        except RepoCleanupError as e:
            log.warning(f"managed repos: cascade cleanup {repo_key} failed (rolled back): {e}")
            continue  # 保留 managed 记录，下次探活重试
        user_db.remove_managed_repo(repo_key)
        # FR-20.14: 清理后确认日志
        log.info(f"managed repos: CLEANUP_DONE repo={repo_key} removed_managed=true "
                 f"signals=[user_token_404, bot_token_404]")

def _start_stale_probe_async(user: dict, providers: list[str]) -> None:
    """异步启动失效托管探活（fire-and-forget + 节流）。

    - 节流：同用户在 REPO_PROBE_INTERVAL 内不重复全量探测；
    - 异步：token 获取与 _probe_stale_repos 全部在后台线程完成，
      请求路径不参与 as_completed / 不 join，不因探活阻塞接口返回。
    """
    import time as _time
    # D42/S62：有目录内平台绑定才探活（目录派生，非字面量）
    try:
        from src.domains.runtime.vcs.platform_card import _DEFAULT_CARDS_RAW as _builtin_cards
    except Exception:
        _builtin_cards = {}
    if not (set(providers) & (set(_builtin_cards) | set(_catalog_platforms()))):
        return  # 无任何已接入平台 token，不探活
    uid = str(user.get("id") or "")
    if not uid:
        return
    interval = REPO_PROBE_INTERVAL
    with _probe_guard:
        last = _last_probe_ts.get(uid, 0.0)
        now = _time.time()
        if interval > 0 and (now - last) < interval:
            return  # 节流：短时间不重复
        _last_probe_ts[uid] = now

    # FR-21.3: 按用户绑定平台取主 token——有 gitcode 用 gitcode，否则用 github
    # D42/S62：探活平台优先级=内置卡声明顺序（数据派生，非字面量）
    try:
        from src.domains.runtime.vcs.platform_card import _DEFAULT_CARDS_RAW
        _order = [p for p in _DEFAULT_CARDS_RAW if p in providers]
    except Exception:
        _order = []
    _probe_provider = _order[0] if _order else (providers[0] if providers else "")
    def _run():
        try:
            token = with_oauth_token(user, _probe_provider, lambda t: t)
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
    from src.domains.mgmt.auth.grape_account_db import get_user_identities
    _identities = get_user_identities(user["id"])
    _bound_providers = {i["provider"] for i in _identities if i.get("provider") in set(_catalog_platforms())}
    if not _bound_providers:
        # D102-C: 纯登录渠道账号（华为是第一个实例——有登录无仓库平台绑定）
        # 返回 200 + 空列表 + 标记位（原 400 文案"请先通过 OAuth 登录"与用户
        # 刚 OAuth 登录完的事实自相矛盾）；前端按标记走"去绑定代码平台"引导
        return {
            "repos": [],
            "_no_vcs_binding": True,
            "hint": "绑定的登录渠道不是代码托管平台——绑定 Gitee/GitCode 等代码平台后，仓库会显示在这里",
        }

    # 需求 17: 前端显式指定 provider 时只拉该平台
    if provider in set(_catalog_platforms()):
        if provider not in _bound_providers:
            raise HTTPException(status_code=400, detail={"code": 400, "message": f"{provider} API Token 未配置，请先绑定 {provider} 平台"})
        _providers = [provider]
    else:
        _providers = list(_bound_providers)

    # 优先读取 DB 缓存（未过期）——按组织过滤
    org_id = user.get("org_id", "")
    _is_personal_org = False
    if org_id:
        from src.domains.mgmt.user.org_db import is_org_personal
        _is_personal_org = is_org_personal(org_id)
    _filter_org_id = "" if _is_personal_org else org_id
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
        # D42/S62：单平台配置时按前缀过滤；裸键（无前缀存量数据）归该平台
        if provider and len(_providers) == 1:
            pfx = provider + ":"
            return [r for r in repos_list if (r.get("repo_key", "").startswith(pfx) or
                       ":" not in r.get("repo_key", ""))]
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
            # D102-P 归位：token 取数走 fetch_user_token 双通道（① 凭据域 <p>_pat
            # ② OAuth 身份）——PAT 归位凭据域后不再经 ei.access_token 供给
            # （读 storage 与写 storage 同源，修正读写分离错位）。
            from src.kernel.token_resolver import create_provider as _create_provider
            _tok = _create_provider(p).fetch_user_token(user.get("username", ""), user["id"])
            if not _tok:
                failed_providers.append(p)
                continue
            repos = _fetch(_tok)
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

    if not all_repos and failed_providers and cached:
        # 全部平台都失败 → 用旧缓存兜底
        return {"repos": _filter_repos(user["id"], _filter_by_provider(cached), org_id=_filter_org_id), "_cached": True, "_stale": True}
    if not all_repos and failed_providers:
        # 确有平台拉取失败（且无旧缓存可兜底）→ 502
        raise HTTPException(status_code=502, detail={"code": 502, "message": "加载仓库列表失败，请稍后重试"})
    # Issue #538: 绑定正常、拉取成功、账号下确无仓库 → 合法空结果，
    # 走与有结果一致的正常路径（去重→落库空列表→返回空数组），不误判为失败

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
    from src.domains.mgmt.auth.grape_account_db import get_user_identities
    _identities = get_user_identities(user["id"])
    _providers = [i["provider"] for i in _identities if i.get("provider") in set(_catalog_platforms())]
    if not _providers:
        raise HTTPException(status_code=400, detail={"code": 400, "message": "API Token 未配置"})

    if _providers:
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
            # D102-P 归位：token 取数走 fetch_user_token 双通道（① 凭据域 <p>_pat
            # ② OAuth 身份）——PAT 归位凭据域后不再经 ei.access_token 供给
            # （读 storage 与写 storage 同源，修正读写分离错位）。
            from src.kernel.token_resolver import create_provider as _create_provider
            _tok = _create_provider(p).fetch_user_token(user.get("username", ""), user["id"])
            if not _tok:
                failed_providers.append(p)
                continue
            repos = _fetch(_tok)
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

    if not all_repos and failed_providers:
        # 确有平台拉取失败 → 502
        raise HTTPException(status_code=502, detail={"code": 502, "message": "同步失败，请稍后重试"})
    # Issue #538: 拉取成功但账号下无仓库 → 合法空结果，落库空缓存并正常返回同步成功

    seen_keys = set()
    deduped = []
    for r in all_repos:
        rk = r.get("repo_key", "")
        if rk not in seen_keys:
            seen_keys.add(rk)
            deduped.append(r)
    repos = deduped
    user_db.save_user_repos(user["id"], repos)

    org_id = user.get("org_id", "")
    # 个人组织不隔离（显示全量用户仓库），非个人组织才隔离
    from src.domains.mgmt.user.org_db import is_org_personal
    _filter_org_id = "" if is_org_personal(org_id) else org_id
    if _has_reauth:
        _msg = f"同步成功（{_reauth_provider} 授权已过期，请重新登录授权）"
        if _filter_org_id:
            return {"repos": _filter_repos(user["id"], repos, org_id=_filter_org_id),
                    "message": _msg, "_partial": True, "_reauth_provider": _reauth_provider}
        return {"repos": _filter_repos(user["id"], repos, org_id=''),
                "message": _msg, "_partial": True, "_reauth_provider": _reauth_provider}
    if _filter_org_id:
        # 非个人组织：已托管到其他组织的仓库不显示，未托管的显示
        return {"repos": _filter_repos(user["id"], repos, org_id=_filter_org_id), "message": "同步成功"}
    return {"repos": _filter_repos(user["id"], repos, org_id=''), "message": "同步成功"}

@router.get("/check-name", operation_id="repos_check_name", summary="Check repo name availability", description="Checks whether a repository with the same name already exists under the current user.")
def check_repo_name(name: str, user: dict = Depends(require_no_session_token)):
    """检查当前用户名下是否已存在同名仓库"""
    provider, api_token = _get_user_provider_and_token(user["username"], user.get("login_provider", ""), str(user.get("id") or ""))
    if not api_token:
        raise HTTPException(status_code=400, detail={"code": 400, "message": "API Token 未配置"})

    # D42/S62：身份/仓库探测走适配器端口（存量平台=委托原实现，语义不变）
    from src.kernel.token_resolver import create_provider
    try:
        login = create_provider(provider).get_user_login(api_token) or ""
        if not login:
            raise HTTPException(status_code=502, detail={"code": 502, "message": "无法获取当前用户身份"})
        from src.domains.runtime.vcs.api_client import VcsApiClient
        _info = VcsApiClient(provider, api_token, login, name).get_repo_info() or {}
        _err = str(_info.get("error", "") or "")
        if _err and "404" in _err:
            return {"exists": False}
        if _err:
            raise HTTPException(status_code=502, detail={"code": 502, "message": f"检查失败: {_err}"})
        return {"exists": True, "full_name": f"{login}/{name}"}
    except HTTPException:
        raise
    except RuntimeError as e:
        if "404" in str(e) or "Not Found" in str(e):
            return {"exists": False}
        raise HTTPException(status_code=502, detail={"code": 502, "message": f"检查失败: {e}"})
    except Exception as e:
        raise HTTPException(status_code=502, detail={"code": 502, "message": f"检查失败: {e}"})

@router.post("/create", operation_id="repos_create", summary="Create repository (optionally auto-host)", description="Creates a repository via the VCS API; optionally auto-hosts it afterwards.")
def create_new_repo(body: CreateRepoBody, user: dict = Depends(require_no_session_token)):
    """通过 VCS API 新建仓库，可选创建后自动托管"""
    import re as _re
    from src.kernel.repo_key import to_repo_key
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
    # D42/S62：用户 token 统一入口（D37 等价锁覆盖，github/gitcode 语义不变）
    if body.provider:
        provider = body.provider
        api_token = get_user_token_for(user["username"], user_id=str(user.get("id") or ""), provider=provider)
    else:
        provider, api_token = _get_user_provider_and_token(user["username"], user.get("login_provider", ""), str(user.get("id") or ""))
    if not api_token:
        raise HTTPException(status_code=400, detail={"code": 400, "message": "API Token 未配置，请先通过 OAuth 登录或配置个人 Token"})

    # D42/S62：建仓走适配器端口（存量三平台=委托原实现，config-only=卡端点）
    from src.kernel.token_resolver import create_provider as _cp_create
    try:
        repo_info = _cp_create(provider).create_repo(api_token=api_token, name=name,
                                                     description=body.description, private=private)
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
        from src.domains.mgmt.user.db import get_webhook_url
        webhook_url = get_webhook_url()
        if not webhook_url:
            # Issue #372: webhook_url 未配置时不阻断创建，只跳过托管
            try:
                _fetch_and_cache(user["id"], api_token, provider)
            except Exception:
                pass
            return {"repo": repo_info, "full_name": full_name, "owner": owner, "name": repo_name, "hosted": False, "message": "仓库已创建，但 webhook_url 未配置，请稍后手动托管"}
        # D42/S62：按目标平台卡重建 webhook 回调 URL（D12 既有函数）
        from src.domains.mgmt.user.db import _rebuild_webhook_url_for_provider
        webhook_url = _rebuild_webhook_url_for_provider(webhook_url, provider)
        # Issue #372: 路径 B 支持——无全局 bot token 时用用户 token + 随机密钥
        import secrets as _secrets
        from src.domains.mgmt.user.db import get_global_bot_token_for
        _create_global_bot_token = get_global_bot_token_for(provider) or ""
        _use_path_b = not _create_global_bot_token  # P2-7: 与 host_repo 一致，有全局 bot token 时走路径 A
        _webhook_secret = _secrets.token_urlsafe(32) if _use_path_b else ""
        try:
            # D42/S62：webhook 查/删/建 + 存储列全走适配器端口与卡派生（零平台分支）
            from src.kernel.token_resolver import create_provider as _cp_wh
            _prov = _cp_wh(provider)
            _wh_token = _create_global_bot_token or api_token  # P2-7: 路径 A 用 bot token，路径 B 用用户 token
            wh_col = _prov.webhook_token_field()
            _bot_token_col = _prov.token_storage_field()
            # F4/D106：卡列空平台（gitee/gitlab 终态）→ 归一逻辑键（bot/webhook），
            # 凭据单写 rc；空串键会被 identifier 白名单拒绝。
            from src.domains.mgmt.user.db import repo_cred_write_keys as _rck_cnr
            wh_col, _bot_token_col = _rck_cnr(_bot_token_col, wh_col)
            existing_id = _prov.find_existing_webhook(_wh_token, owner, repo_name, webhook_url)
            repo_key = to_repo_key(full_name, provider=provider)
            if existing_id:
                # Issue #372 R6.2: 路径 B 复用已有 webhook 时密钥不匹配 → 删旧重建
                if _use_path_b:
                    try:
                        _prov.remove_webhook(_wh_token, owner, repo_name, existing_id)
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
                # D42/S62：建 webhook 走端口（路径 A secret 空→存量函数回填 token；
                # 路径 B secret=随机密钥，Gitee 系映射 password、GitHub 映射 secret）
                hook_id = _prov.setup_webhook(_wh_token, owner, repo_name, webhook_url,
                                              webhook_secret=_webhook_secret)
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
        from src.domains.mgmt.user.repo_manager import add_github_collaborator, accept_github_invite, get_github_repo_permissions

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

_UPSTREAM_CODE_RE = re.compile(r"failed \((\d{3})\)")

def _upstream_status_code(err: Exception) -> int:
    """从 _req/_github_req RuntimeError 报文中提取上游状态码（无则 0）。

    报文格式固定：GitCode API {method} {url} failed ({code}) / GitHub API ... failed ({code})。
    """
    try:
        m = _UPSTREAM_CODE_RE.search(str(err))
        if m:
            return int(m.group(1))
    except Exception:
        pass
    return 0

def _host_502_detail(stage: str, err: Exception, message: str = "") -> dict:
    """构造带上下文的 502 detail（Issue #455：托管失败需可解释、可重试）。

    - 不把上游内部 URL/报文透出给客户端（Issue #383/#395 防泄露约束），原文仅落日志；
    - 401/403/404 为权限/资源类错误，不标记 retryable（盲重试无意义）；
    - 网络类/5xx 标记 retryable，并给出自愈/重试路径提示。
    """
    code = _upstream_status_code(err)
    retryable = code not in (401, 403, 404)
    return {
        "code": 502,
        "error_code": f"UPSTREAM_{stage.upper()}" + (f"_{code}" if code else ""),
        "message": message or "托管服务暂时不可用，请稍后重试",
        "upstream": {"stage": stage, "status": code or None, "retryable": retryable},
        "retryable": retryable,
        "retry_hint": "可稍后重试；若 webhook 已创建但托管未完成，可使用「修复托管」自愈（POST /repos/{owner}/{repo}/repair-host）",
    }

@router.post("/host", operation_id="repos_host", summary="Host repository", description="Registers an existing repository for hosting: creates the webhook and sets up the repo bot.")
def host_repo(body: RepoAction, user: dict = Depends(require_no_session_token), request: Request = None,
              provider: str = ""):
    """托管仓库（建 webhook + 加 bot 为维护者 + 注册到 managed_repos）。

    provider 参数由调用方传入（如 create_from_template 已确定 provider）；
    不传时自动选择（向后兼容）。
    """
    from src.kernel.repo_key import to_repo_key
    # 解析当前组织上下文：优先从 user dict（require_org_member 注入），其次从 request header
    _org_id = user.get("org_id", "") if isinstance(user, dict) else ""
    if not _org_id and request:
        try:
            _org_id = resolve_org_context(request, user["id"])
        except Exception:
            _org_id = ""
    # ===== provider 判定：仓库来源优先于登录平台（host-repo-provider-routing-fix-design.md）=====
    if not provider:
        provider = _provider_from_hint(body.full_name)
    # 剥离 provider 前缀（"github:owner/repo" / "gitcode:owner/repo"），下游统一用纯 owner/repo
    _raw = body.full_name
    # D42/S62：平台前缀集从仓库平台目录派生（不再字面量元组）
    try:
        from src.domains.runtime.vcs.platform_card import list_vcs_platforms
        _vps = set(list_vcs_platforms() or [])
    except Exception:
        _vps = set()
    if ":" in _raw and _raw.split(":", 1)[0] in _vps:
        _raw = _raw.split(":", 1)[1]
        body.full_name = _raw
    parts = _raw.split("/", 1)
    if len(parts) != 2:
        raise HTTPException(status_code=400, detail={"code": 400, "message": "无效的仓库标识"})
    owner, repo_name = parts

    # D42/S62：显式 provider > 自动选择（目录内平台即显式；用户 token 统一入口）
    if provider in _vps:
        api_token = get_user_token_for(user["username"], user_id=str(user.get("id") or ""), provider=provider)
    else:
        provider, api_token = _provider_from_token(user)
    from src.domains.mgmt.user.db import get_webhook_url
    webhook_url = get_webhook_url()
    if not webhook_url:
        # Issue #372 §14.2.3: 报错带结构化 guidance，指导用户自服务而非指向管理员
        from src.domains.mgmt.user.db import get_meta_value as _gmv
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
    # D42/S62：按目标平台卡重建 webhook 回调 URL（D12 既有函数，消字面量 replace）
    from src.domains.mgmt.user.db import _rebuild_webhook_url_for_provider
    webhook_url = _rebuild_webhook_url_for_provider(webhook_url, provider)

    if not api_token:
        raise HTTPException(status_code=400, detail={"code": 400, "error_code": "TOKEN_NOT_BOUND",
            "message": f"{_card_name(provider)} API Token 未配置或已过期，请重新绑定/登录"})

    # ===== D42/S62：托管流统一（工厂 + 适配器端口，调用方零平台分支）=====
    # 语义基线：GitHub 流（Issue #455 权限前置 + bot 协作者降级路径 B）与
    # GitCode 流（access_level>=40 + §16.1 降级）在端口层合并——权限判定
    # _perm_is_admin 归一（dict access_level>=40 / str admin|maintain）。
    from src.kernel.token_resolver import create_provider as _create_prov
    from src.domains.mgmt.user.db import get_global_bot_token_for, set_host_status
    from src.domains.mgmt.user.repo_manager import resolve_bot_username
    from src.api.session_store import ensure_default_agent_sessions
    import secrets as _secrets
    _prov = _create_prov(provider)
    _wh_col = _prov.webhook_token_field()
    _tok_col = _prov.token_storage_field()
    # F4/D106：卡列空平台（gitee/gitlab 终态）→ 归一逻辑键（bot/webhook），
    # 凭据单写 rc（add_managed_repo 路由）；空串键会被 identifier 白名单拒绝。
    from src.domains.mgmt.user.db import repo_cred_write_keys as _rck_hr
    _tok_col, _wh_col = _rck_hr(_tok_col, _wh_col)
    global_bot_token = get_global_bot_token_for(provider) or ""
    global_bot_username = resolve_bot_username(provider) or ""
    repo_key = to_repo_key(body.full_name, provider=provider)

    def _perm_is_admin(_perm):
        # 归一：GitCode dict（access_level>=40 / permission=admin / permissions.admin）；
        # GitHub str（admin/maintain）；None/未知 → False
        if isinstance(_perm, dict):
            try:
                if int(_perm.get("access_level", 0) or 0) >= 40:
                    return True
            except (TypeError, ValueError):
                pass
            if str(_perm.get("permission", "") or "").lower() == "admin":
                return True
            _p = _perm.get("permissions") or {}
            return bool(_p.get("admin")) if isinstance(_p, dict) else False
        return str(_perm or "").lower() in ("admin", "maintain")

    def _mark(status: str):
        try:
            set_host_status(repo_key, status)
        except Exception as _e:
            log.warning(f"host_repo: set host_status={status} failed: {_e}")

    # Issue #372: 路径 A（全局 bot token）优先，无则路径 B（用户 token + 随机密钥）
    _use_path_b = not global_bot_token
    webhook_token = global_bot_token or api_token
    _webhook_secret = _secrets.token_urlsafe(32) if _use_path_b else ""

    def _fallback_path_b(reason: str):
        nonlocal _use_path_b, webhook_token, _webhook_secret
        log.warning(f"host: falling back to path B ({reason})")
        _use_path_b = True
        webhook_token = api_token
        _webhook_secret = _secrets.token_urlsafe(32)
        _mark("path_b_fallback")

    from src.domains.mgmt.user.repo_manager import _check_collaborator_admin, _req
    try:
        # 1) 身份 + 权限前置校验（Issue #455：非所有者/无管理员权限 403 明示）
        def _get_login(token):
            return _prov.get_user_login(token) or ""
        current_user = ""
        _identity_unverified = False
        try:
            current_user = with_oauth_token(user, provider, _get_login)
        except RuntimeError as _pe:
            import re as _re2
            _m = _re2.search(r"\((\d{3})\)", str(_pe))
            _pc = int(_m.group(1)) if _m else 0
            log.warning(f"host_repo upstream failed: {_pe}")
            if _pc == 404:
                raise HTTPException(status_code=404, detail={"code": 404, "error_code": "REPO_NOT_FOUND",
                    "message": f"仓库 {owner}/{repo_name} 不存在于 {_card_name(provider)} 或无权访问，请确认仓库归属与授权"})
            if _pc in (401, 403):
                raise HTTPException(status_code=403, detail={"code": 403, "error_code": "TOKEN_PERMISSION_INSUFFICIENT",
                    "message": "Token 权限不足或已过期，请更新后重试或改用 OAuth"})
            # 网络类（无状态码）：身份+权限前置均无法确认 → 不阻断，交由下游
            # webhook 创建的既有错误路径兜底（Issue #455 语义，基线行为保持）
            log.warning(f"host_repo: identity pre-check skipped (network/unknown: {_pe})")
            _identity_unverified = True
            current_user = owner  # 无法证伪归属——下游 webhook 创建失败会自然报错
        if current_user is None:
            # D85: with_oauth_token 产不出信息（None，探测不可用）与网络错同待遇——
            # 不阻断，交由下游既有错误路径兜底（issue455 unknown_permission 语义）
            log.warning("host_repo: identity pre-check unavailable (None), skipping")
            _identity_unverified = True
            current_user = owner
        if not _identity_unverified and not current_user:
            raise HTTPException(status_code=502, detail={"code": 502, "message": "无法获取当前用户身份"})
        if not _identity_unverified:
            if current_user == owner:
                is_admin = True
            else:
                def _check_admin(token, _cu=current_user):
                    return _perm_is_admin(_prov.get_collaborator_permission(token, owner, repo_name, _cu))
                is_admin = bool(with_oauth_token(user, provider, _check_admin))
            if not is_admin:
                raise HTTPException(status_code=403, detail={"code": 403, "error_code": "PERMISSION_DENIED",
                    "message": f"无权限托管该 {_card_name(provider)} 仓库：请使用该仓库所有者的凭据，或授予机器人管理员权限后再试"})

        # 2) bot 协作者（路径 A）：邀请 → 接受 → 自查权限；失败降级路径 B（§16.1/R12.3）
        _bot_result = {"bot_collaborator_needed": False}
        if not _use_path_b and api_token and global_bot_username and global_bot_token:
            _mark("partial")
            _add_ok = _prov.add_collaborator(api_token, owner, repo_name, global_bot_username)
            if not _add_ok:
                try:
                    if _perm_is_admin(_prov.get_collaborator_permission(global_bot_token, owner, repo_name, global_bot_username)):
                        log.info(f"host: bot {global_bot_username} already maintainer in {owner}/{repo_name}, continue")
                    else:
                        _fallback_path_b("bot permission insufficient")
                except Exception:
                    _fallback_path_b("bot permission check failed")
            if not _use_path_b:
                if not _prov.accept_collaborator_invite(global_bot_token, owner, repo_name):
                    log.warning(f"host: no invite to accept for {owner}/{repo_name}, bot may already be member")
                try:
                    if not _perm_is_admin(_prov.get_collaborator_permission(global_bot_token, owner, repo_name, global_bot_username)):
                        _fallback_path_b("bot perm check inconclusive")
                except Exception as _pe2:
                    # 限流/网络：权限未判定，继续 webhook（不阻塞），host_status 待 dashboard 复查
                    log.warning(f"host: perm check inconclusive (continue): {_pe2}")

        # 3) webhook：查/建/复用（R6.2 路径 B 复用时密钥不匹配 → 删旧重建）
        existing_id = _prov.find_existing_webhook(webhook_token, owner, repo_name, webhook_url)
        if existing_id and _use_path_b:
            try:
                _prov.remove_webhook(webhook_token, owner, repo_name, existing_id)
                existing_id = None
            except Exception as _re:
                log.warning(f"host_repo: remove old webhook failed (will reuse): {_re}")
                _webhook_secret = ""   # P1-1: 删失败时不写新密钥
                webhook_token = ""     # §31: 不写 stale 用户 token，验证 loud fail
        if existing_id:
            _db_wh = "" if _use_path_b else webhook_token
            user_db.add_managed_repo(repo_key, owner, repo_name, webhook_id=existing_id, org_id=_org_id,
                host_user_id=(user["id"] if _use_path_b else 0), webhook_secret=_webhook_secret,
                bot_username=(global_bot_username if not _use_path_b else ""),
                **{_wh_col: _db_wh, _tok_col: ("" if _use_path_b else global_bot_token)})
            user_db.mark_user_repo_join(user["id"], repo_key, is_join=True, is_admin=True)
            ensure_default_agent_sessions(repo_key)
            _mark("")
            return {"message": "仓库已托管（复用已有 webhook）", "webhook_id": existing_id, **_bot_result}

        _hook_id = None
        try:
            _hook_id = _prov.setup_webhook(webhook_token, owner, repo_name, webhook_url,
                                           webhook_secret=(_webhook_secret if _use_path_b else ""))
        except RuntimeError as _wh_err:
            import re as _re3
            _codes = _re3.findall(r"\((\d{3})\)", str(_wh_err))
            _code = int(_codes[0]) if _codes else 0
            if _code == 401:
                raise HTTPException(status_code=502, detail={"code": 502,
                    "message": f"{_card_name(provider)} 登录已失效，请重新绑定/登录后重试"})
            if _code == 404:
                raise HTTPException(status_code=502, detail={"code": 502,
                    "message": f"仓库 {owner}/{repo_name} 不存在于 {_card_name(provider)}，请确认仓库所属平台与托管账号平台一致"})
            raise
        # 清理同名旧仓库残留的 DB 数据
        from src.api.onboarding import cleanup_stale_repo_data
        cleanup_stale_repo_data(repo_key)
        _db_wh = "" if _use_path_b else webhook_token
        user_db.add_managed_repo(repo_key, owner, repo_name, webhook_id=_hook_id, org_id=_org_id,
            host_user_id=(user["id"] if _use_path_b else 0), webhook_secret=_webhook_secret,
            bot_username=(global_bot_username if not _use_path_b else ""),
            **{_wh_col: _db_wh, _tok_col: ("" if _use_path_b else global_bot_token)})
        user_db.mark_user_repo_join(user["id"], repo_key, is_join=True, is_admin=True)
        ensure_default_agent_sessions(repo_key)
        _mark("")
        return {"message": "仓库托管成功", "webhook_id": _hook_id, **_bot_result}
    except HTTPException:
        raise
    except Exception as e:
        # 兜底：失败但 webhook 可能已建，标记状态避免死循环，仍可进入项目
        try:
            _mark("partial")
        except Exception:
            pass
        log.warning(f"host_repo failed (outer): {e}")
        raise HTTPException(status_code=502, detail=_host_502_detail("host_setup", e))

@router.post("/{owner}/{repo}/repair-host", operation_id="repos_repair_host", summary="Repair hosting", description="Self-heals an unfinished hosting flow (bot not joined / webhook missing). Idempotent; uses owner-token invite + bot-token webhook, skipping the invite when the bot is already a maintainer.")
def repair_host(owner: str, repo: str, user: dict = Depends(require_no_session_token)):
    """方案 F-4：自愈未完成的托管流程（bot 未加入 / webhook 未创建），幂等。

    流程：需要 owner token 邀请 + bot token 建 webhook；判定 bot 已是维护者则跳过邀请；
    返回修复后的 host_status。

    Issue #372: 按 provider 统一分流（get_hosting_provider_config），路径 B 支持无全局 bot token。
    """
    from src.kernel.repo_key import to_repo_key, parse_repo_key
    from src.domains.mgmt.user.db import get_webhook_url, set_host_status, get_global_bot_token_for
    from src.domains.mgmt.user.repo_manager import resolve_bot_username
    from src.api.deps import get_user_token_for

    repo_key = to_repo_key(f"{owner}/{repo}")
    # S70（实弹发现）：已托管仓的 provider 优先从 managed_repos 反查（gitee: 行
    # 存在时不再折叠为裸 key 默认的 gitcode——原误派生导致 gitee 仓 repair-host
    # 必 400 TOKEN_NOT_BOUND，自愈入口对 gitee 仓不可达）。
    try:
        from src.domains.mgmt.user.db import get_webhook_secret_by_repo_key_any  # noqa: F401
        from src.kernel.session_manager import db as _sm_db, db_lock as _sm_dl
        with _sm_dl:
            _prow = _sm_db.cursor().execute(
                "SELECT repo_key FROM managed_repos WHERE repo_key LIKE CONCAT('%:', ?) LIMIT 1",
                (f"{owner}/{repo}",)).fetchone()
        if _prow and _prow[0]:
            repo_key = _prow[0]
    except Exception:
        pass
    from src.api.deps import check_repo_access
    from src.domains.mgmt.user.db import managed_repo_exists

    # D42/S62：owner token 统一入口（D37 等价锁覆盖）
    _provider, _, _ = parse_repo_key(repo_key)
    api_token = get_user_token_for(user["username"], user_id=str(user.get("id") or ""), provider=_provider)
    if not api_token:
        raise HTTPException(status_code=400, detail={"code": 400, "error_code": "TOKEN_NOT_BOUND", "message": "缺少用户 API Token，请重新登录"})

    # Issue #671：未注册仓（托管链路缺口）自证放行——check_repo_access 依赖
    # managed_repos/user_repos（未注册仓直接 403，自愈入口对缺口仓不可达）。
    # 未注册时改用调用者用户 token 做 VCS 可达性校验，通过即视同具备修复权限。
    try:
        check_repo_access(user, repo_key)
    except HTTPException:
        if managed_repo_exists(repo_key):
            raise
        _self_ok = False
        try:
            from src.domains.runtime.vcs.api_client import VcsApiClient
            _info = VcsApiClient(_provider, api_token, owner, repo).get_repo_info() or {}
            _self_ok = not str(_info.get("error", "") or "")
        except Exception as _ve:
            log.warning(f"repair_host: unregistered repo VCS self-check failed: {_ve}")
        if not _self_ok:
            raise
        log.info(f"repair_host: unregistered repo {repo_key} passed VCS self-check, allow repair")

    # D42/S62：bot 配置来源链（平台卡→全局 meta→默认）
    bot_token = get_global_bot_token_for(_provider) or ""
    bot_username = resolve_bot_username(_provider) or ""

    # Issue #372: 路径 B——无全局 bot token 时用用户 token 修复 webhook
    _use_path_b = not bot_token
    import secrets as _secrets
    _webhook_secret = _secrets.token_urlsafe(32) if _use_path_b else ""

    # webhook_url 按 provider 替换路由后缀
    webhook_url = get_webhook_url()
    if not webhook_url:
        # Issue #372 §14.2.3: 报错带结构化 guidance
        from src.domains.mgmt.user.db import get_meta_value as _gmv
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
    # D42/S62：按目标平台卡重建 webhook 回调 URL（D12 既有函数）
    from src.domains.mgmt.user.db import _rebuild_webhook_url_for_provider
    webhook_url = _rebuild_webhook_url_for_provider(webhook_url, _provider)

    # 1) bot 是否已是维护者（路径 A only；路径 B 跳过）
    # D42/S62：bot 权限/邀请走适配器端口（权限形态归一 _bot_perm_ok：
    # GitCode dict access_level>=40 / GitHub role admin|maintain|push /
    # repo-permissions dict {admin,maintain,push}）
    from src.kernel.token_resolver import create_provider as _cp_repair
    _prov = _cp_repair(_provider)

    def _bot_perm_ok(_perm):
        if isinstance(_perm, dict):
            try:
                if int(_perm.get("access_level", 0) or 0) >= 40:
                    return True
            except (TypeError, ValueError):
                pass
            if str(_perm.get("permission", "") or "").lower() == "admin":
                return True
            if any(_perm.get(k) for k in ("admin", "maintain", "push")):
                return True
            _p = _perm.get("permissions") or {}
            return bool(isinstance(_p, dict) and any(_p.get(k) for k in ("admin", "maintain", "push")))
        return str(_perm or "").lower() in ("admin", "maintain", "push")

    bot_ok = True if _use_path_b else False
    if not _use_path_b:
        try:
            bot_ok = _bot_perm_ok(_prov.get_collaborator_permission(bot_token, owner, repo, bot_username))
        except Exception:
            bot_ok = False

        # P1-5: bot 权限不足时邀请+重试（R12.2/R12.8 显式 accept；§16.2 失败降级路径 B）
        if not bot_ok:
            try:
                _prov.add_collaborator(api_token, owner, repo, bot_username)
                _prov.accept_collaborator_invite(bot_token, owner, repo)
                # 重新检查权限
                try:
                    bot_ok = _bot_perm_ok(_prov.get_collaborator_permission(bot_token, owner, repo, bot_username))
                except Exception:
                    bot_ok = False
            except Exception as _ge:
                log.warning(f"repair_host: add collaborator failed: {_ge}")
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
    # D42/S62：webhook 凭据形态由适配器内聚（GitHub secret / Gitee 系 password
    # = secret，空时存量函数回填 token），_wh_password 变量语义并入端口调用
    _wh_password = _webhook_secret

    hid = None
    try:
        hid = _prov.find_existing_webhook(_wh_token, owner, repo, webhook_url)
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
            from src.domains.mgmt.user.db import get_webhook_secret
            _stale_secret = get_webhook_secret(repo_key)
            _force_rebuild = bool(_stale_secret)
            if _force_rebuild:
                log.info(f"repair_host: detected stale webhook_secret after B→A upgrade, forcing rebuild for {owner}/{repo}")
        if _force_rebuild:
            try:
                _prov.remove_webhook(_wh_token, owner, repo, hid)
                log.info(f"repair_host: removed old webhook {hid} for re-creation with new secret")
                hid = None
            except Exception as _re:
                log.warning(f"repair_host: remove old webhook failed (will reuse): {_re}")
                _webhook_secret = ""  # P1-1: 删失败时不写新密钥
                _wh_token = ""  # §31: 也不写用户 token（刷新后 stale），让验证 loud fail
                _wh_password = _webhook_secret
    if not hid:
        hid = _prov.setup_webhook(_wh_token, owner, repo, webhook_url, webhook_secret=_wh_password)
    if not hid:
        set_host_status(repo_key, "webhook_missing")
        return {"status": "error", "host_status": "webhook_missing",
                "message": "未能创建 webhook，请检查权限后重试"}

    # 3) 完成 → 回写 webhook_id → 清状态
    try:
        from src.domains.mgmt.user.db import add_managed_repo
        # P1-2: 路径 B 成功时写空串（不写 stale 用户 token），webhook 验证依赖 webhook_secret
        _db_wh_token = "" if _use_path_b else _wh_token
        # F4/D106：卡列空平台（gitee/gitlab 终态）→ 归一逻辑键（bot/webhook）
        from src.domains.mgmt.user.db import repo_cred_write_keys as _rck_rh
        _tok_key, _wh_key = _rck_rh(_prov.token_storage_field(), _prov.webhook_token_field())
        _db_kwargs = {
            "webhook_id": hid,
            "bot_username": (bot_username if not _use_path_b else ""),
            "host_user_id": (user["id"] if _use_path_b else 0),
            "webhook_secret": _webhook_secret,
            _wh_key: _db_wh_token,
            _tok_key: (bot_token if not _use_path_b else ""),
        }
        add_managed_repo(repo_key, owner, repo, **_db_kwargs)
    except Exception as _e:
        log.warning(f"repair_host: write back webhook_id failed: {_e}")
    set_host_status(repo_key, "")
    return {"status": "ok", "host_status": "", "webhook_id": hid,
            "message": "托管已修复（webhook 已就绪）" if _use_path_b else "托管已修复（bot 已加入成员，webhook 已就绪）"}

@router.get("/managed", operation_id="repos_managed", summary="List managed repositories", description="Lists repositories hosted by the platform for the current user.")
def list_managed_repos(user: dict = Depends(require_org_member)):
    org_id = user.get("org_id", "")
    # 个人组织放宽 org 过滤（方案A）：与 /repos/member 大堂口径一致，
    # 侧边栏下拉框显示 user_repos.is_join=1 的仓库（不论托管在哪个组织）
    _personal_org = False
    if org_id:
        try:
            from src.domains.mgmt.user.org_db import is_org_personal
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
        # D99：_r 笔误修复 + 历史列段删除（rc 权威单点；_r 是 S82 笔误致 NameError 500）
        "has_bot_token": _has_rc_cred(r.get("repo_key", "")),
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
    from src.kernel.repo_key import to_repo_key, parse_repo_key, split_owner_repo
    from src.api.deps import check_repo_access
    from src.kernel.session_manager import db, db_lock

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
        from src.domains.mgmt.user.db import get_host_user_id
        _host_uid = get_host_user_id(repo_key)
        # F3: host_user_id 可能以 str 落库（UUID 凭据行/TEXT 列），归一为 int 判空
        try:
            _host_uid_n = int(str(_host_uid).strip() or 0)
        except (TypeError, ValueError):
            _host_uid_n = 0
        if _host_uid_n > 0 and str(_host_uid) != str(user.get("id") or ""):
            raise HTTPException(status_code=403, detail={
                "code": 403, "error_code": "NOT_REPO_OWNER",
                "message": "只有仓库托管人或管理员可配置 Bot Token"
            })

    # 确定 provider（数据驱动：前缀即权威，无平台分支）
    _provider_str, _, _ = parse_repo_key(repo_key)

    # R15.5: 传空串时清除仓库级 bot token
    if not body.bot_token:
        try:
            # F4/D106：清除走 db 层统一入口（有列平台清列 / 卡列空平台清 rc
            # 权威行；写路径收敛——grape_t_repo_credentials 写入点白名单见 S119 UC23①）
            from src.domains.mgmt.user.db import clear_repo_bot_token
            if not clear_repo_bot_token(repo_key):
                raise HTTPException(status_code=404, detail={"code": 404, "message": "仓库未找到"})
        except HTTPException:
            raise
        except Exception as e:
            log.warning(f"set_repo_bot_token: clear bot_token failed: {e}")
            raise HTTPException(status_code=500, detail={"code": 500, "message": "清除 Bot Token 失败"})
        # R15.5.5: best-effort 移除旧 bot 协作者身份
        _remove_old_bot_collaborator(repo_key, owner, repo_name, user)
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
    from src.kernel.token_resolver import create_provider_from_repo_key
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
        from src.domains.mgmt.user.db import get_host_user_id
        _host_uid = get_host_user_id(repo_key)
        # F3: str 归一判空（host_user_id 可能以 str 形态返回）
        try:
            _host_uid_n = int(str(_host_uid).strip() or 0)
        except (TypeError, ValueError):
            _host_uid_n = 0
        if _host_uid_n > 0:
            # 路径 B: 用 host 用户 token 邀请
            try:
                from src.domains.mgmt.auth.grape_account_db import get_grape_account_by_id
                _host_acc = get_grape_account_by_id(str(_host_uid_n))
                if _host_acc and _host_acc.get("username"):
                    # D42/S62：统一入口（D37 等价锁覆盖）
                    from src.api.deps import get_user_token_for
                    _invite_token = get_user_token_for(_host_acc["username"], _host_uid, provider=_provider_str) or ""
            except Exception:
                pass
            if not _invite_token:
                raise HTTPException(status_code=400, detail={
                    "code": 400, "error_code": "HOST_TOKEN_EXPIRED",
                    "message": "托管用户 Token 已过期，请重新绑定后再配置 Bot Token"
                })
        else:
            # 路径 A: 用当前用户 token 邀请（D42/S62：统一入口）
            from src.api.deps import get_user_token_for
            _invite_token = get_user_token_for(user["username"], user_id=str(user.get("id") or ""), provider=_provider_str) or ""

        if _invite_token:
            try:
                _vcs_provider.add_collaborator(_invite_token, owner, repo_name, repo_bot_username)
            except Exception as _ae:
                # R15.2.3: 邀请失败记日志继续（邀请幂等，bot 可能已是协作者）
                log.warning(f"set_repo_bot_token: add_collaborator failed (continuing): {_ae}")

    # R15.6: best-effort 移除旧 bot 协作者身份（如果有旧 bot 且与新 bot 不同）
    if _old_bot_username and _old_bot_username != repo_bot_username:
        _remove_old_bot_collaborator(repo_key, owner, repo_name, user, _old_bot_username)

    # 步骤 5-6: 重建 webhook + 写 DB（委托 update_managed_repo_bot_token）
    _token_result = update_managed_repo_bot_token(repo_key, body.bot_token)
    if not _token_result.get("ok"):
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
                                  user: dict, old_bot_username: str = ""):
    """R15.6: best-effort 移除旧 bot 协作者身份。

    只移除该仓库的旧 bot 协作者（R15.6.1），不移除其他仓库的旧 bot（R15.6.2）。
    移除失败不影响新 token 写入（R15.6.3 best-effort）。
    provider 一律由 repo_key 前缀派生（S75/解耦口径），无平台分支。
    """
    if not old_bot_username:
        # 查 DB 获取旧 bot_username
        try:
            from src.kernel.session_manager import db, db_lock
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
        from src.kernel.token_resolver import create_provider_from_repo_key
        _provider = create_provider_from_repo_key(repo_key)
        # 移除旧 bot 需要用 owner token（有仓库管理权限的 token）
        _remove_token = ""
        # S75/解耦：按 repo_key 派生 provider 取 owner token（无平台分支）
        from src.api.deps import get_user_token_for_repo
        _remove_token = get_user_token_for_repo(repo_key, user["username"], str(user.get("id") or "")) or ""
        if _remove_token:
            _provider.remove_collaborator(_remove_token, owner, repo_name, old_bot_username)
    except Exception as _re:
        log.warning(f"_remove_old_bot_collaborator: failed (best-effort): {_re}")

class DeployUrlBody(BaseModel):
    full_name: str
    deploy_url: str = ""

def _validate_deploy_url(url: str) -> str:
    """校验 deploy_url：空串允许（清除），非空必须以 http:// 或 https:// 开头，长度 ≤512。"""
    url = (url or "").strip()
    if not url:
        return ""
    if len(url) > 512:
        raise HTTPException(status_code=400, detail={"code": 400, "message": "部署 URL 长度不能超过 512 字符"})
    if not (url.startswith("http://") or url.startswith("https://")):
        raise HTTPException(status_code=400, detail={"code": 400, "message": "部署 URL 必须以 http:// 或 https:// 开头"})
    return url

@router.put("/deploy-url", operation_id="repos_deploy_url_set", summary="Set deployment URL", description="Registers the deployed access URL of the business system.")
def set_deploy_url(body: DeployUrlBody, user: dict = Depends(require_no_session_token)):
    """登记业务系统部署后的访问 URL"""
    from src.kernel.repo_key import to_repo_key
    from src.kernel.session_manager import db, db_lock
    deploy_url = _validate_deploy_url(body.deploy_url)
    # 实弹验证 2026-11-10：裸 to_repo_key 默认 gitcode: 前缀，gitee/github 托管仓
    # 写错 key → UPDATE 不中行报 404。候选集探针解析真实前缀（幂等，带前缀原样）。
    _fn = (body.full_name or "").strip()
    if ":" in _fn.split("/", 1)[0]:
        repo_key = to_repo_key(_fn)
    else:
        _o, _r = (_fn.split("/", 1) + [""])[:2]
        repo_key = _resolve_scene_recommend_repo_key(_o, _r)
    from src.api.deps import check_repo_access
    check_repo_access(user, repo_key)
    with db_lock:
        c = db.cursor()
        c.execute("UPDATE managed_repos SET deploy_url=? WHERE repo_key=?", (deploy_url, repo_key))
        if c.rowcount == 0:
            raise HTTPException(status_code=404, detail={"code": 404, "message": "仓库未找到"})
        db.commit()
    return {"message": "部署 URL 已更新", "deploy_url": deploy_url}

@router.get("/deploy-url/{owner}/{repo_name}", operation_id="repos_deploy_url", summary="Get repository deployment URL", description="Returns the repository deployment access URL.")
def get_deploy_url(owner: str, repo_name: str, user: dict = Depends(require_no_session_token)):
    """获取仓库的部署访问 URL"""
    from src.kernel.repo_key import to_repo_key
    from src.kernel.session_manager import db, db_lock
    # 实弹验证 2026-11-10：同 set_deploy_url——候选集探针解析真实前缀。
    repo_key = _resolve_scene_recommend_repo_key(owner, repo_name)
    with db_lock:
        c = db.cursor()
        c.execute("SELECT deploy_url FROM managed_repos WHERE repo_key=?", (repo_key,))
        row = c.fetchone()
    return {"deploy_url": (row[0] if row else "") or ""}

def _check_unmanage_vcs_permission(user: dict, owner: str, repo_name: str, repo_provider: str) -> None:
    """解除托管救火闸门（薄委托）。

    背景（docs/weekly-08/unmanage-permission-guard-design.md 救火项）：
    agent 曾按「解除所有托管」指令遍历 /repos/remove，把共同参与甚至从未工作过的
    仓库也解除了。读权限（能看到）≠ 写权限（能解除），本闸门在 VCS 侧独立复核。
    """
    _require_vcs_maintainer(user, owner, repo_name, repo_provider,
                            action="解除托管", fail_suffix="（未解除任何托管）")


def _cleanup_repo_data(repo_key: str, owner: str, repo: str) -> dict:
    """解除托管时的同步清理：关联数据表 + 仓库 skill 目录 + workspace 目录。

    Issue #340：升级为级联清理，统一入口见 src/repo_cleanup.py。
    本函数保留为兼容包装（历史调用方/测试）；DB 事务失败时降级为旧行为。
    （D42 迁移时误删，恢复。）
    """
    from src.kernel.repo_cleanup import cascade_cleanup_repo_data, RepoCleanupError
    try:
        return cascade_cleanup_repo_data(repo_key, owner, repo, trigger="legacy")
    except RepoCleanupError:
        return {"tables": 0, "skill_dirs": 0, "workspace_dirs": 0,
                "dry_run": False, "trigger": "legacy"}

@router.get("/migrate-orgs", operation_id="repos_migrate_orgs",
            summary="List current user's Grape orgs for repo migration",
            description="Returns the Grape organizations the current user belongs to, used to populate the target-org dropdown on the migrate dialog (Issue #543).")
def list_migrate_orgs(user: dict = Depends(require_no_session_token)):
    """迁移目标组织下拉框数据源（Issue #543）。

    返回当前登录用户在 Grape 平台所属的组织列表 + 用户个人登录名（仓库可迁到个人名下）。
    鉴权：require_no_session_token（与 /migrate 同口径）；未登录 401。
    """
    from src.kernel.session_manager import db, db_lock

    orgs = []
    personal_login = user.get("username") or ""

    try:
        with db_lock:
            c = db.cursor()
            # 查用户所属组织（含个人组织），与 /orgs 端点同源
            c.execute(
                "SELECT o.id, o.name, o.slug, o.is_personal, o.owner_id, m.role "
                "FROM organization o "
                "JOIN org_member m ON m.org_id = o.id "
                "WHERE m.user_id = ? "
                "ORDER BY o.is_personal DESC, o.name ASC",
                (user["id"],))
            rows = c.fetchall()
        for r in rows:
            # Issue #593：个人组织由 personal 字段独立返回，orgs 列表跳过 is_personal 条目，
            # 避免迁移目标组织下拉框出现重复的"个人"项（与右上角"我的组织"口径保持一致）。
            if r[3]:  # is_personal=True
                continue
            orgs.append({
                "login": r[2] or "",  # slug 作为迁移目标 owner
                "name": r[1] or r[2] or "",
                "avatar_url": "",
                "is_personal": bool(r[3]),
                "role": r[5] or "member",
            })
    except Exception as e:
        log.warning(f"list_migrate_orgs: query failed for user={user.get('id', 0)}: {e}")
        raise HTTPException(status_code=502, detail={
            "code": 502, "message": "无法获取组织列表，请稍后重试"})

    return {"orgs": orgs, "personal": personal_login}

@router.post("/migrate", operation_id="repos_migrate", summary="Migrate repo to another Grape org", description="Rebinds a repo from one Grape organization to another. The repo's VCS location (owner/repo) does not change — only the Grape-internal org_id association is updated.")
def migrate_repo(body: MigrateRepoBody, user: dict = Depends(require_no_session_token)):
    """仓库迁移（Issue #540）：在 Grape 平台内将仓库从源组织改绑到目标组织。

    与 VCS 侧的仓库转移无关——仓库在 GitCode/GitHub 的实际位置不变，
    只更新 Grape managed_repos.org_id 及关联数据的组织归属。

    - webhook/bot token 沿用原配置，不重建；
    - 场景配置 / Issue 关联随 org_id 改写跟随；
    - 工作区目录不迁移（按 repo_key 定位，org_id 变更不影响路径）。

    前置校验（失败返回明确错误，不破坏现有托管）：
    1. 源 repo_key 存在于 managed_repos（404）
    2. 当前用户是源仓库的 admin/owner（403）
    3. 目标组织存在且当前用户是目标组织成员（403）
    """
    from src.kernel.repo_key import to_repo_key
    from src.kernel.session_manager import db, db_lock
    from src.api.deps import check_repo_access

    repo_key = to_repo_key(body.repo_key)
    target_owner = (body.target_owner or "").strip()
    target_repo = (body.target_repo or "").strip()
    if not target_owner:
        raise HTTPException(status_code=400, detail={
            "code": 400, "message": "目标组织不能为空"})

    # 校验 1：源 repo_key 存在于 managed_repos
    try:
        with db_lock:
            c = db.cursor()
            c.execute("SELECT org_id FROM managed_repos WHERE repo_key=?", (repo_key,))
            row = c.fetchone()
    except Exception as e:
        log.warning(f"migrate_repo: managed_repos lookup failed: {e}")
        row = None

    if not row:
        raise HTTPException(status_code=404, detail={
            "code": 404, "message": "源仓库未托管，无法迁移"})
    src_org_id = row[0] or ""

    # 校验 2：当前用户对源仓库有访问权
    check_repo_access(user, repo_key)

    # 校验 3：目标组织存在且当前用户是成员
    # target_owner 是 Grape organization.slug
    try:
        with db_lock:
            c = db.cursor()
            c.execute(
                "SELECT o.id, o.is_personal, m.role "
                "FROM organization o "
                "JOIN org_member m ON m.org_id = o.id "
                "WHERE o.slug = ? AND m.user_id = ?",
                (target_owner, user["id"]))
            org_row = c.fetchone()
    except Exception as e:
        log.warning(f"migrate_repo: target org lookup failed: {e}")
        org_row = None

    if not org_row:
        raise HTTPException(status_code=403, detail={
            "code": 403,
            "message": f"目标组织 '{target_owner}' 不存在或您不是该组织成员"})
    target_org_id = org_row[0]

    if target_org_id == src_org_id:
        raise HTTPException(status_code=400, detail={
            "code": 400, "message": "目标组织与源组织相同，无需迁移"})

    # 执行 org_id 改写（repo_key 不变，只改 Grape 内的组织归属）
    # 所有同时含 org_id + repo_key 列的表都要更新
    _ORG_ID_TABLES = (
        "managed_repos", "devops_pipeline_scene", "issue", "events",
        "agent_tasks", "agent_runtimes", "agent_shared_knowledge",
        "chat_conversations", "devops_agent_session", "runtime_credentials",
        "scene_market", "session_api_tokens",
    )
    try:
        with db_lock:
            c = db.cursor()
            updated = 0
            for tbl in _ORG_ID_TABLES:
                try:
                    c.execute(
                        f"UPDATE {tbl} SET org_id=? WHERE repo_key=?",
                        (target_org_id, repo_key))
                    if c.rowcount and c.rowcount > 0:
                        updated += c.rowcount
                except Exception as te:
                    log.warning(f"migrate_repo: update {tbl}.org_id skipped: {te}")
            db.commit()
    except Exception as e:
        log.warning(f"migrate_repo: org_id update failed: {e}")
        raise HTTPException(status_code=500, detail={
            "code": 500, "message": f"迁移失败（已回滚）: {e}"})

    log.info(f"repos/migrate: {repo_key} org {src_org_id}->{target_org_id} by user={user.get('id', 0)}, {updated} rows updated")
    return {
        "message": "仓库迁移成功",
        "old_repo_key": repo_key,
        "new_repo_key": repo_key,
        "old_org_id": src_org_id,
        "new_org_id": target_org_id,
        "rows_updated": updated,
    }

def _check_migrate_vcs_permission(user: dict, owner: str, repo_name: str, repo_provider: str) -> None:
    """迁移前置校验（薄委托）：目标仓库 VCS 侧存在且当前用户有 maintainer 及以上权限。"""
    _require_vcs_maintainer(user, owner, repo_name, repo_provider, action="迁移目标仓库")


@router.post("/remove", operation_id="repos_remove", summary="Remove hosting", description="Removes hosting for a repository (unregisters webhook/bot bindings).")
def remove_managed_repo(body: RepoAction, user: dict = Depends(require_no_session_token)):
    from src.kernel.repo_key import to_repo_key
    parts = body.full_name.split("/", 1)
    if len(parts) != 2:
        raise HTTPException(status_code=400, detail={"code": 400, "message": "无效的仓库标识"})
    owner, repo_name = parts

    # 按 provider 分流：前端传的 full_name 不含前缀
    # 先尝试 gitcode:（默认），不存在再尝试 github:
    from src.kernel.repo_key import to_repo_key as _rk
    gc_key = to_repo_key(body.full_name)  # 默认 gitcode:
    gh_key = _rk(body.full_name, provider="github")

    # 判断实际存在的 repo_key（直接查 managed_repos 表）
    repo_key = gc_key
    repo_provider = "gitcode"
    _repo_exists = False
    try:
        from src.kernel.session_manager import db, db_lock
        with db_lock:
            c0 = db.cursor()
            c0.execute("SELECT repo_key FROM managed_repos WHERE repo_key IN (?, ?)", (gh_key, gc_key))
            row0 = c0.fetchone()
            # D42/S62：数据比较（命中哪个候选键就是哪个平台，非字面量前缀判断）
            if row0 and row0[0] == gh_key:
                repo_key = gh_key
                repo_provider = gh_key.split(":", 1)[0]
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

    # D42/S62：token 统一入口 + 全局 bot 来源链（用户→仓库 bot→全局，D37 等价锁覆盖）
    from src.api.deps import get_user_token_for
    from src.domains.mgmt.user.db import get_global_bot_token_for
    api_token = get_user_token_for(user["username"], user_id=str(user.get("id") or ""), provider=repo_provider)
    if not api_token:
        api_token = get_bot_token(repo_owner=owner, repo_name=repo_name)
    if not api_token:
        api_token = get_global_bot_token_for(repo_provider)

    # 删除 webhook（按 provider 选择 API）
    if api_token:
        try:
            # D42/S62：删 webhook 走适配器端口（存量平台=委托原实现）
            from src.kernel.token_resolver import create_provider as _cp_rm
            from src.domains.mgmt.user.repo_manager import _get_managed_webhook
            wh_id = _get_managed_webhook(repo_key)
            if wh_id:
                _cp_rm(repo_provider).remove_webhook(api_token, owner, repo_name, wh_id)
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
                # S82/D58：列冻结后 rc 权威（D99 归一 cred_type='bot'）
                # 历史列读取=挂账 #12 读回落语义，此处只看权威源
                from src.domains.mgmt.user.db import get_repo_credential_value as _grcv
                repo_bot_token = _grcv(repo_key, "bot") or ""
                repo_bot_username = _r.get("bot_username", "") or ""
                break
        # 全局 bot 用户名（D42/S62：来源链统一入口）
        # Issue #345: 改用统一入口
        from src.domains.mgmt.user.repo_manager import resolve_bot_username as _resolve_bot
        global_bot_username = _resolve_bot(repo_provider) or ""
        # owner token（D42/S62：统一入口，D37 等价锁覆盖）
        from src.api.deps import get_user_token_for
        owner_token = get_user_token_for(user["username"], user_id=str(user.get("id") or ""), provider=repo_provider)
        # Issue #372: 路径 B 仓库（无 bot token）跳过 bot 协作者移除——托管时未邀请 bot
        from src.domains.mgmt.user.db import get_host_user_id
        _is_path_b = (not repo_bot_token and not global_bot_username
                      and str(get_host_user_id(repo_key) or "") not in ("", "0"))
        if not _is_path_b:
            if owner_token and global_bot_username:
                try:
                    # D42/S62：移除协作者走适配器端口
                    from src.kernel.token_resolver import create_provider as _cp_rc
                    _rm_ok = _cp_rc(repo_provider).remove_collaborator(owner_token, owner, repo_name, global_bot_username)
                    if not _rm_ok:
                        log.warning(f"Remove global bot collaborator failed (perm?): {global_bot_username} @ {owner}/{repo_name}")
                except Exception as e:
                    log.warning(f"Remove global bot collaborator error: {e}")
            # repo bot（若配置且非全局 bot）
            if owner_token and repo_bot_username and repo_bot_username != global_bot_username:
                try:
                    from src.kernel.token_resolver import create_provider as _cp_rc2
                    _rm_ok2 = _cp_rc2(repo_provider).remove_collaborator(owner_token, owner, repo_name, repo_bot_username)
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
    from src.kernel.repo_cleanup import cascade_cleanup_repo_data, RepoCleanupError
    try:
        _stats = cascade_cleanup_repo_data(repo_key, owner, repo_name,
                                           trigger="platform",
                                           operator_id=str(user.get("id") or ""))
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
    from src.kernel.repo_key import split_owner_repo, to_repo_key
    from src.domains.runtime.engine.service import load_project_info
    # 实弹验证 2026-11-10（部署实例 livefire）：裸 to_repo_key 默认 gitcode: 前缀，
    # gitee/github 托管仓读错 key → 有基线也返回空 files/meta。与 scene-recommend-config
    # 同源：裸名查 managed_repos 候选集解析真实前缀。
    repo_key = _resolve_scene_recommend_repo_key(owner, repo)
    from src.api.deps import check_repo_access
    check_repo_access(user, repo_key)
    if user.get("auth_type") == "session_token":
        _bound_owner, _bound_repo = split_owner_repo(user.get("repo_key") or "")
        if _bound_owner and (_bound_owner != owner or _bound_repo != repo):
            raise HTTPException(status_code=403, detail={"code": 403, "message": "无权读取该项目知识（仓库作用域不匹配）"})
    files, meta = load_project_info(repo_key)
    return {"repo_key": repo_key, "files": files, "meta": meta}

class SceneRecommendConfigBody(BaseModel):
    fallback_scene: str = ""
    rules: list = []  # [{"scene_id": "", "condition": ""}]

def _resolve_scene_recommend_repo_key(owner: str, repo: str) -> str:
    """按 managed_repos 实际前缀解析 repo_key（Issue #487）。

    固定 to_repo_key（默认 gitcode: 前缀）在 github: 托管仓上读写错 key，
    导致保存假成功。与 /scenes/with-pipeline 同源：裸名查 DB 解析真实前缀，
    已带前缀幂等返回。
    """
    from src.kernel.repo_key import to_repo_key
    owner_repo = f"{owner}/{repo}"
    if ":" in owner or ":" in repo:
        return to_repo_key(owner_repo)
    try:
        from src.kernel.session_manager import db as _db, db_lock as _dl
        with _dl:
            c = _db.cursor()
            from src.kernel.repo_key import repo_key_candidates
            _rks = tuple(repo_key_candidates(owner_repo))
            row = c.execute(f"SELECT repo_key FROM managed_repos WHERE repo_key IN ({','.join('?' * len(_rks))}) LIMIT 1",
                            _rks).fetchone()
            if row:
                return row[0]
    except Exception:
        pass
    return to_repo_key(owner_repo)

@router.get("/{owner}/{repo}/scene-recommend-config", operation_id="repos_scene_recommend_config_get", summary="Get scene recommend config", description="Reads the repository smart scene-recommend config (fallback scene + routing rules).")
def get_scene_recommend_config(owner: str, repo: str, user: dict = Depends(require_no_session_token)):
    """读取仓库智能推荐配置（兜底场景 + 路由规则）。"""
    from src.domains.mgmt.user.db import get_scene_recommend_config
    repo_key = _resolve_scene_recommend_repo_key(owner, repo)
    return get_scene_recommend_config(repo_key)

@router.put("/{owner}/{repo}/scene-recommend-config", operation_id="repos_scene_recommend_config_set", summary="Save scene recommend config", description="Saves the repository scene-recommend config. Self-checks on write: filters out rules whose scene_id is not in the current scene list; fallback_scene is required.")
def save_scene_recommend_config(owner: str, repo: str, body: SceneRecommendConfigBody,
                                user: dict = Depends(require_no_session_token)):
    """保存仓库智能推荐配置（兜底场景 + 路由规则）。

    写入时自检：过滤 scene_id 不在当前场景列表中的规则。
    fallback_scene 必填，空则报错。
    #487：repo_key 按 managed_repos 实际前缀解析；场景列表为空 / 落库失败
    返回明确 400，不再假成功。
    """
    from src.domains.mgmt.user.db import set_scene_recommend_config
    repo_key = _resolve_scene_recommend_repo_key(owner, repo)

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

    try:
        saved = set_scene_recommend_config(repo_key, body.fallback_scene.strip(), body.rules or [])
    except ValueError as e:
        raise HTTPException(status_code=400, detail={"code": 400, "message": str(e)})
    if not saved:
        raise HTTPException(status_code=400, detail={
            "code": 400, "message": "智能推荐配置保存失败（仓库行不存在或写入不满足），请稍后重试"})
    return {"message": "智能推荐规则已保存"}

_SUPPORTED_PROVIDERS = {"gitcode", "github", "gitee"}  # 兼容引用保留

_GITCODE_API_BASE = "https://gitcode.com/api/v5"

_GITHUB_API_BASE = "https://api.github.com"

_CREDENTIAL_TTL = 600

def _provider_from_path(path: str) -> str:
    """从 /api/repos/{owner}/{repo}/... 请求路径解析目标仓 repo_key 前缀 provider。

    Issue #466 断点一：跨平台联合开发（GitHub 主仓 + GitCode 关联仓）下，会话 token
    的 repo_key 按主仓签发；关联仓换券请求须按路径中目标仓前缀（gitcode:/github:）
    解析 provider，否则把 ghp_ token 发给 gitcode.com。仅识别已接入前缀（小写归一），
    未知前缀（如 gitlab:）返回空由调用方回退。
    """
    for seg in str(path or "").split("/"):
        if not seg:
            continue
        low = seg.lower()
        for p in _supported_providers():
            if low.startswith(f"{p}:"):
                return p
    return ""

def _repo_provider(request: Request, user: dict = Depends(get_current_user_or_api_key)) -> str:
    """Git provider（gitcode/github）。

    优先按请求路径目标仓 repo_key 前缀解析（Issue #466 跨平台关联仓换券）；
    路径无前缀时回退认证 token 的 repo_key 前缀（会话主仓，见
    git-repo-provider-from-token-design.md），保持同平台/无前缀调用向后兼容。
    """
    from src.kernel.repo_key import parse_repo_key
    # 实弹验证 2026-11-10：parse_repo_key("") 默认返回 ('gitcode',...)，空 token
    # repo_key 会在 or 链上短路掉 managed 探针——token 无绑定时视为空，让探针接管。
    _tok_rk = user.get("repo_key") or ""
    _tok_p = parse_repo_key(_tok_rk)[0] if _tok_rk else ""
    p = (_provider_from_path(request.url.path)
         or _tok_p
         or _repo_provider_managed_probe(request)
         or "gitcode")
    if p not in _supported_providers():
        raise HTTPException(status_code=403, detail={"code": 403, "message": f"Git provider 未接入: {p}"})
    return p

def _strip_provider_prefix(owner: str) -> str:
    """剥离 owner 段的 provider 前缀（gitcode:/github:），供 URL/workspace 拼接。

    Issue #466：带前缀路径（/api/repos/gitcode:owner/repo/...）的 owner path 段
    含前缀；token 解析需要带前缀 repo_key（幂等），但 clone_url / workspace
    路径须用裸 owner/repo。未知前缀原样返回。
    """
    # S85/#466 收口：实现上移 repo_key.strip_provider_prefix（数据驱动单一入口），
    # 本函数保留为薄委托（既有 3 个调用点零改动）。
    from src.kernel.repo_key import strip_provider_prefix
    return strip_provider_prefix(owner)

def _repo_platform_token(owner: str, repo: str, user: dict, provider: str = "gitcode") -> str:
    """平台 token 解析（仅会话级临时 token tk_；用户 API Key / Web JWT 不适用）。

    chat agent → 用户 token；devops 事件 agent → 仓库 bot token；一种缺失交叉回退；均缺失 502。
    """
    if user.get("auth_type") != "session_token":
        raise HTTPException(status_code=403, detail={"code": 403, "message": "Git 仓库接口仅支持会话级临时 token（agent 专用），用户 API Key / Web 会话不适用"})
    from src.kernel.repo_key import to_repo_key

    # D42/S62：repo_key 幂等（带前缀单前缀，裸名补当前 provider 前缀——Issue #466 同语义）
    rk = to_repo_key(f"{owner}/{repo}", provider=provider)
    import time as _time
    _start = _time.monotonic()

    def _fail(stage: str, exc: Exception):
        return "", stage, exc

    def _bot():
        try:
            # 统一仓库 token 入口：TokenResolver 策略链（bot 列→全局→用户 token 降级，
            # 含 D32-补丁 fallback）；provider 由参数数据驱动，无平台分支。
            from src.kernel.token_resolver import TokenResolver, create_provider
            return TokenResolver(create_provider(provider)).get_token(rk) or "", "", None
        except Exception as e:
            # Issue #1063：异常不在闭包里吞掉，交出 stage 由调用方分级
            return _fail(_GIT_CREDENTIAL_STAGE_BOT, e)
    def _user():
        try:
            from src.api.deps import get_user_token_for
            uid = str(user.get("id") or "")
            username = user.get("username") or ""
            if not username and str(uid or "") not in ("", "0"):
                from src.domains.mgmt.auth.grape_account_db import get_grape_account_by_id
                username = (get_grape_account_by_id(uid) or {}).get("username", "")
            return get_user_token_for(username, user_id=uid, provider=provider) or "", "", None
        except Exception as e:
            return _fail(_GIT_CREDENTIAL_STAGE_USER, e)

    stage = ""
    exc = None
    if user.get("agent_type") == "chat":
        tok, stage, exc = _user()
        if not tok:
            tok, stage, exc = _bot()
    else:
        tok, stage, exc = _bot()
        if not tok:
            tok, stage, exc = _user()
    if tok:
        log.debug("git_credential_ok", extra={
            "event": "git_credential_ok",
            "provider": provider,
            "repo_key": rk,
            "auth_type": user.get("auth_type", ""),
            "agent_type": user.get("agent_type", ""),
            "duration_ms": round((_time.monotonic() - _start) * 1000),
        })
        return tok
    stage = stage or _GIT_CREDENTIAL_STAGE_BOTH
    _log_git_credential_failure(
        stage, provider, rk, user, exc,
        duration_ms=round((_time.monotonic() - _start) * 1000),
    )
    raise HTTPException(status_code=502, detail=_credential_502_detail(stage, provider=provider))


@router.get("/{owner}/{repo}/git-credential", operation_id="repos_git_credential",
            summary="One-shot git platform credential (CLI internal, TTL)",
            description="One-shot short-lived platform credential for local git via CLI (in-process, never persisted). "
                        "gitcode uses Basic auth with username x-access-token and the configured gitcode token as password. Session-only.")
def get_repo_git_credential(owner: str, repo: str, user: dict = Depends(get_current_user_or_api_key),
                            p: str = Depends(_repo_provider)):
    """返回 CLI 本地执行 git 所需的平台凭据（一次性/短期，进程内使用不落盘）。

    gitcode：Basic 用 username=`x-access-token`、password=配置的 gitcode token。
    Issue #466：owner 段可带 provider 前缀（跨平台关联仓换券）；clone_url 用裸 owner/repo。
    """
    _start = time.monotonic()
    try:
        token = _repo_platform_token(owner, repo, user, p)
        _gh = _card_host(p)
        _o = _strip_provider_prefix(owner)
        return {
            "provider": p,
            "username": "x-access-token",
            "password": token,
            "clone_url": f"https://{_gh}/{_o}/{repo}.git",
            "expires_in": _CREDENTIAL_TTL,
        }
    except HTTPException:
        raise
    except Exception as e:
        from src.kernel.repo_key import to_repo_key
        _log_git_credential_failure(
            _GIT_CREDENTIAL_STAGE_UNEXPECTED, p,
            to_repo_key(f"{owner}/{repo}", provider=p), user, e,
            duration_ms=round((time.monotonic() - _start) * 1000),
        )
        raise


@router.get("/{owner}/{repo}/workspace", operation_id="repos_workspace", summary="Repository workspace path (local git via CLI)", description="Returns the grape-managed local workspace path for the repository (CLI local git on the server). Session-only.")
def get_repo_workspace(owner: str, repo: str, user: dict = Depends(get_current_user_or_api_key),
                       p: str = Depends(_repo_provider)):
    _o = _strip_provider_prefix(owner)
    ws = os.path.join(WORKSPACE_DIR, _o, repo)
    _gh = _card_host(p)
    return {"workspace": ws, "clone_url": f"https://{_gh}/{_o}/{repo}.git"}

import time

from src.api.deps import get_current_user_or_api_key, get_bot_token, get_user_token, get_user_token_for, get_user_token_for_repo, require_no_session_token, require_org_member

from src.domains.mgmt.user.repo_manager import setup_webhook, find_existing_webhook  # noqa: F401 — D85 兼容缝：host 流已迁 _prov 端口，模块级名字保留给存量 mock/补丁点位（基线测试契约），不再被直接调用

# D105-17：仓库通用操作（列仓/建仓/webhook）一律走 token_resolver 适配器端口
# （create_provider(provider).fetch_repos_with_role / create_repo /
# find_existing_webhook / setup_webhook，见下方各路由）；不再从 repo_manager
# 直调通用函数。下方保留的 repo_manager 直调均为平台专属存量实现
# （github 方言函数 / resolve_bot_username 统一入口 / gitcode 内部件）。
def _has_rc_cred(repo_key: str) -> bool:
    """D99：has_bot 展示判据=rc 权威 bot 凭据（cred_type 归一='bot'，无平台枚举）。"""
    from src.domains.mgmt.user.db import get_repo_credential_value
    return bool(get_repo_credential_value(repo_key, "bot"))

def _probe_provider_for(_token: str) -> str:
    """D42/S62：探活主 token 归属平台=内置卡声明顺序首个（存量语义：主 token=gitcode）。"""
    try:
        from src.domains.runtime.vcs.platform_card import _DEFAULT_CARDS_RAW
        return next(iter(_DEFAULT_CARDS_RAW))
    except Exception:
        return "gitcode"

def _probe_url_headers(provider: str, token: str, path: str):
    """D42/S62：探活请求 URL/头按平台卡派生（无卡返回 None）。

    - query_token（Gitee 系 v5）：token 走查询参数
    - token_header（GitHub）：Bearer + v3 Accept
    - 其余：Bearer + JSON Accept
    """
    try:
        from src.domains.runtime.vcs.platform_card import get_platform_card
        card = get_platform_card(provider)
    except Exception:
        card = None
    if card is None or not token:
        return None, None
    url = f"{card.api_base}{path}"
    headers = {"Accept": "application/json", "User-Agent": "Grape-DevOps"}
    if card.api_auth == "query_token":
        sep = "&" if "?" in url else "?"
        url = f"{url}{sep}access_token={token}"
    elif card.api_auth == "token_header":
        headers["Accept"] = "application/vnd.github+json"
        headers["Authorization"] = f"Bearer {token}"
    else:
        headers["Authorization"] = f"Bearer {token}"
    return url, headers

def _card_host(provider: str) -> str:
    """D42/S62：git 传输 host 从平台卡派生（无卡回退内置 git host）。"""
    try:
        from src.domains.runtime.vcs.platform_card import get_platform_card
        _c = get_platform_card(provider)
        return (_c.host if _c else provider) or provider
    except Exception:
        return provider

def _catalog_platforms() -> "list[str]":
    """D42/S62：仓库平台目录（内置卡 + 已配置卡）。"""
    try:
        from src.domains.runtime.vcs.platform_card import list_vcs_platforms
        return list_vcs_platforms() or []
    except Exception:
        return []

def _card_name(provider: str) -> str:
    """D42/S62：平台显示名（卡 name，无卡/异常回退 provider 名）。"""
    try:
        from src.domains.runtime.vcs.platform_card import get_platform_card
        _c = get_platform_card(provider)
        return (_c.name if _c else provider) or provider
    except Exception:
        return provider

def _perm_at_least_maintainer(_perm) -> "bool | str":
    """D42/S62：权限归一判定——通过返回 True，否则返回可读角色（供 403 文案）。

    GitCode dict：access_level>=40 或 permission=admin；GitHub str：admin/maintain。
    """
    if isinstance(_perm, dict):
        try:
            if int(_perm.get("access_level", 0) or 0) >= 40:
                return True
        except (TypeError, ValueError):
            pass
        if str(_perm.get("permission", "") or "").lower() == "admin":
            return True
        return str(_perm.get("role_name", "") or "") or "未知"
    _role = str(_perm or "").lower()
    if _role in ("admin", "maintain"):
        return True
    return _role or "未知"

def _require_vcs_maintainer(user: dict, owner: str, repo_name: str, repo_provider: str,
                            action: str, fail_suffix: str = "") -> None:
    """D42/S62：VCS 侧权限闸门统一（unmanage/migrate 共用，零平台分支）。

    规则同口径（unmanage-permission-guard-design.md）：仓库所有者天然通过；
    GitCode access_level>=40 或 permission==admin；GitHub role admin/maintain。
    FR-24.1: with_oauth_token + 平台 /user 取真实 login（自动 401 刷新）。
    失败不兜底：token 缺失 / API 不可达 / 无法判定 → 一律拒绝，绝不放行。
    """
    from src.kernel.token_resolver import create_provider
    from src.domains.mgmt.user.repo_manager import _RateLimitedError
    _prov = create_provider(repo_provider)
    _cname = _card_name(repo_provider)

    def _get_login(token):
        return _prov.get_user_login(token) or ""

    try:
        vcs_login = with_oauth_token(user, repo_provider, _get_login)
    except HTTPException:
        raise
    if not vcs_login:
        raise HTTPException(status_code=502, detail={
            "code": 502, "message": f"无法获取当前用户 {_cname} 身份"})
    if vcs_login == owner:
        return

    def _get_perm(token):
        return _prov.get_collaborator_permission(token, owner, repo_name, vcs_login)

    try:
        data = with_oauth_token(user, repo_provider, _get_perm)
    except _RateLimitedError:
        raise HTTPException(status_code=429, detail={
            "code": 429, "message": f"{_cname} 权限校验被限流，请稍后重试{fail_suffix}"})
    except HTTPException:
        raise
    if data is None or data == "":
        raise HTTPException(status_code=403, detail={
            "code": 403, "message": f"无法校验 {_cname} 仓库权限（非协作者或 API 失败），拒绝{action}"})
    _verdict = _perm_at_least_maintainer(data)
    if _verdict is not True:
        raise HTTPException(status_code=403, detail={
            "code": 403, "message": f"{action}需要 {_cname} maintainer 及以上权限（当前角色：{_verdict}）"})

def _supported_providers() -> set:
    """D105e 根修：路由 provider 门禁数据驱动——平台卡注册表（configs/platforms/*.json）
    即唯一权威，配卡即接入，不再硬编码白名单。gitcode/github 存量兜底恒在。"""
    try:
        from src.domains.runtime.vcs.platform_card import list_vcs_platforms
        return set(list_vcs_platforms()) | {"gitcode", "github"}
    except Exception:
        # 扫雷⑦（中危③）：兜底名单与主路径对称——卡注册表在册四平台
        # （configs/platforms/{gitcode,github,gitee,gitlab}.json）全部列入，
        # gitcode/github 存量兜底恒在。旧名单漏 gitlab：卡体系异常时
        # gitlab 仓会被 403（主路径可见、兜底不可见的不对称）。
        return {"gitcode", "github", "gitlab", "gitee"}

def _repo_provider_managed_probe(request: Request) -> str:
    """workspace/clone_url 的 provider 兜底（实弹验证 2026-11-10 补）：
    路径无前缀 + 认证 token 无 repo_key（Web JWT）时，_repo_provider 回落
    gitcode → gitee 托管仓 clone_url 拼成 gitcode.com。此探针按路径 owner/repo
    查 managed_repos 候选集取真实前缀，_repo_provider 全空时才回落。
    """
    try:
        import re as _re
        # 路径形态 /api/repos/{owner}/{repo}[/workspace|...]——owner/repo 恰两段，
        # 后续子路径不再参与（正则歧义会把 {repo}/{sub} 误当 owner/repo）。
        m = _re.match(r"/api/repos/([^/:?]+)/([^/:?]+?)(?:/[^:]*)?$", request.url.path)
        if not m:
            return ""
        _o, _r = m.group(1), m.group(2)
        for seg in (_o, _r):
            if ":" in seg:
                return ""
        from src.kernel.repo_key import repo_key_candidates
        from src.kernel.session_manager import db as _db, db_lock as _dl
        _rks = tuple(repo_key_candidates(f"{_o}/{_r}"))
        if not _rks:
            return ""
        with _dl:
            _row = _db.cursor().execute(
                f"SELECT repo_key FROM managed_repos WHERE repo_key IN ({','.join('?' * len(_rks))}) LIMIT 1",
                _rks).fetchone()
        if _row and _row[0] and ":" in _row[0]:
            return _row[0].split(":", 1)[0]
    except Exception:
        pass
    return ""

def _vcs_display_name(provider: str) -> str:
    """平台展示名（数据驱动：平台卡 name 位，卡缺失时回退 provider 字面量）。"""
    try:
        from src.domains.runtime.vcs.platform_card import get_platform_card
        _card = get_platform_card(provider)
        if _card is not None and getattr(_card, "name", ""):
            return _card.name
    except Exception:
        pass
    return provider or ""

def _credential_502_detail(stage: str, message: str = "", provider: str = "gitcode") -> dict:
    """构造 git 凭据解析失败的 502 detail（Issue #1063：失败需可解释、可重试）。

    与 Issue #455 的 _host_502_detail 同构：只回失败阶段与错误码，异常原文仅落日志，
    不含任何 token/上游 URL 等内部信息。
    """
    _vcs = _vcs_display_name(provider)
    return {
        "code": 502,
        "error_code": GIT_CREDENTIAL_ERROR_CODE,
        "message": message or f"{_vcs} 仓库 token 解析失败",
        "stage": stage,
        "provider": provider,
        "retryable": True,
        "retry_after_s": GIT_CREDENTIAL_RETRY_AFTER_S,
    }

def _log_git_credential_failure(stage: str, provider: str, repo_key: str, user: dict,
                                exc: "Exception | None" = None, duration_ms: "int | None" = None) -> None:
    """记录 git 凭据解析失败的结构化日志（不含 token 明文）。

    按仓库日志契约通过 extra={...} 注入顶层字段（event/stage/error_code/provider/
    repo_key/auth_type/agent_type/error_class/duration_ms），便于 Loki/ELK 按字段
    检索与聚合；message 仅保留事件名。request_id/user_id 缺失时由 logging filter
    从 ContextVar 补齐，repo_key 因本接口路径不在中间件提取范围内而显式传入。

    安全取舍：不记录异常原文与 traceback，仅保留异常类名——异常消息可能携带
    token 或含凭据 URL（Spec 禁止输出 token 值）。需要堆栈时按 request_id 关联。
    """
    extra = {
        "event": "git_credential_failed",
        "stage": stage,
        "error_code": GIT_CREDENTIAL_ERROR_CODE,
        "provider": provider,
        "repo_key": repo_key,
        "auth_type": user.get("auth_type", ""),
        "agent_type": user.get("agent_type", ""),
        "error_class": type(exc).__name__ if exc else "",
    }
    if duration_ms is not None:
        extra["duration_ms"] = duration_ms
    log.warning("git_credential_failed", extra=extra)

GIT_CREDENTIAL_ERROR_CODE = "GIT_CREDENTIAL_UNAVAILABLE"

GIT_CREDENTIAL_RETRY_AFTER_S = 15

_GIT_CREDENTIAL_STAGE_BOT = "bot_token_lookup"

_GIT_CREDENTIAL_STAGE_USER = "user_token_refresh"

_GIT_CREDENTIAL_STAGE_BOTH = "both_unavailable"

_GIT_CREDENTIAL_STAGE_UNEXPECTED = "credential_resolve"
