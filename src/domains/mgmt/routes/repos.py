"""仓库路由：列表、托管、移除、同步 + git 平台凭据/工作区（方案③ CLI 支撑）"""
import os
import re
import time
from fastapi import APIRouter, HTTPException, Depends, Request
from pydantic import BaseModel
from src.kernel.config import log, WORKSPACE_DIR
from src.domains.mgmt.user import db as user_db
from src.domains.mgmt.user.repo_manager import fetch_repos_with_role, setup_webhook, find_existing_webhook, create_repo
from src.api.deps import get_current_user_or_api_key, get_bot_token, get_user_token, get_user_token_with_refresh, get_user_token_for, get_user_github_token_with_refresh, require_no_session_token, require_org_member
from src.api.oauth_token import with_oauth_token
from src.api.org_context import resolve_org_context
from src.domains.mgmt.auth.audit import log_audit
from src.domains.mgmt.user.org_db import transfer_managed_repo_to_org
from src.domains.mgmt.user.db import update_managed_repo_bot_token, update_managed_repo_bot_username, repo_has_bot_credential, resolve_managed_repo_key

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
    owner: str = ""     # 仓库所属空间（个人登录名或 VCS 组织 path）；空 = 个人空间
    path: str = ""      # 仓库 URL 路径（GitCode 支持与 name 不同；GitHub 恒等于 name）


class MigrateRepoBody(BaseModel):
    repo_key: str  # 源 repo_key（带 provider 前缀，如 gitcode:ownerA/repo）
    target_owner: str  # 目标组织（VCS 侧已转移后的新 owner）
    target_repo: str = ""  # 目标仓库名（默认与源同名）


# 无显式 provider 时的兜底顺序 = 平台卡目录里实装托管全流程的平台（不再写死偏好）。
# 原为 ("github","gitcode") 字面量：既是二元平台枚举（棘轮 test_platform_whitelist_ratchet 红），
# 又让「无任何线索」的托管请求默认打到 github（2026-11-14 gitee 仓库被当 GitHub 托管的根因之一）。
def _default_token_provider_order() -> tuple[str, ...]:
    try:
        from src.domains.platform.public import hosting_providers

        return tuple(sorted(hosting_providers()))
    except Exception:
        return ()


def _get_user_provider_and_token(username: str, login_provider: str = "", user_id: int = 0) -> tuple[str, str]:
    """根据用户已绑定的令牌自动选择 provider，返回 (provider, token)。

    顺序（数据驱动）：login_provider（登录来源）→ 存量默认优先序（策略常量）
    → 其余在册平台（卡目录序）。未配卡平台不进优先序；全部无 token 返回 ("","")。
    """
    _listable = _repo_listable_providers()
    order: list[str] = []
    for _p in (login_provider, *_default_token_provider_order(), *sorted(_listable)):
        if _p and _p in _listable and _p not in order:
            order.append(_p)
    _uid = str(user_id or "")
    for _p in order:
        _tok = get_user_token_for(username, user_id=_uid, provider=_p)
        if _tok:
            return _p, _tok
    return "", ""


def _provider_from_hint(full_name: str) -> str:
    """按「仓库来源」判断 provider：repo_key 前缀 → managed_repos 已登记前缀。

    返回**在册平台卡**的 provider key 或 ''（无来源线索）。2026-11-12 起卡目录驱动
    （此前只认 github:/gitcode: 两前缀、且只在 managed_repos 里查这两家，gitee
    仓恒返回 '' → 托管被兜底误判成 github/gitcode）。
    这是托管路由的主要信息来源，避免再被「登录平台」短路误判
    （host-repo-provider-routing-fix-design.md）。
    """
    from src.kernel.repo_key import parse_repo_key
    f = (full_name or "").strip()
    if not f:
        return ""
    _listable = _repo_listable_providers()
    if ":" in f:
        _p, _o, _r = parse_repo_key(f)
        if _p in _listable and _o and _r:
            return _p
    base = f.split(":", 1)[-1]
    try:
        from src.kernel.session_manager import db, db_lock
        _cands = [f"{p}:{base}" for p in sorted(_listable)]
        if not _cands:
            return ""
        _ph = ",".join("?" * len(_cands))
        with db_lock:
            rows = db.cursor().execute(
                f"SELECT repo_key FROM managed_repos WHERE repo_key IN ({_ph}) LIMIT 1", _cands
            ).fetchall()
        for r in rows:
            _p, _, _ = parse_repo_key(r[0])
            if _p in _listable:
                return _p
    except Exception:
        pass
    return ""


# hooks 探测 404 语义：这些平台把 hooks-404 视为"仓库可能刚被删"（需 bot token 复核）；
# 其余平台 hooks-404 是权限/不可见信号，一律跳过不判（m2 事故：GitHub 非 admin 查 hooks 回 404）。
def _provider_from_user_repos(user: dict, full_name: str) -> str:
    """用 user_repos 缓存反查仓库所属平台——比「按 token 可用性轮询」更权威。

    2026-11-14 实锤：旧壳/三方调用给 /repos/host 的 full_name 不带前缀、也不传
    ?provider=，于是 `_provider_from_hint` 无线索 → `_provider_from_token` 按顺序
    挑到 github，用 GitHub token 去查 gitee 仓库 → 上游 404「明明是管理员却托管失败」。
    这里的线索来自"这个账号的缓存里确实有这个仓库"，且只在**唯一命中**时采用，
    多个平台同名仓库时仍返回 ''（不猜）。
    """
    base = (full_name or "").split(":", 1)[-1].strip().lower()
    if not base or "/" not in base:
        return ""
    try:
        from src.domains.mgmt.user import db as _udb

        _listable = _repo_listable_providers()
        hits = set()
        for r in _udb.get_user_repos(str(user.get("id") or "")) or []:
            rk = r.get("repo_key") or ""
            if ":" not in rk:
                continue
            pfx, _, rest = rk.partition(":")
            if rest.strip().lower() == base and pfx in _listable:
                hits.add(pfx)
        return hits.pop() if len(hits) == 1 else ""
    except Exception as e:
        log.warning(f"_provider_from_user_repos failed (non-fatal): {e}")
        return ""


def _provider_from_token(user: dict) -> tuple[str, str]:
    """托管兜底：按「平台 token 是否可用」依次选择 provider。

    兜底顺序由平台卡目录派生（`_default_token_provider_order`，按 hosting_providers()
    字典序），不再有写死的平台偏好。
    不按登录平台短路（登录平台 ≠ 仓库平台，见 host-repo-provider-routing-fix-design.md）。
    只在**无法从仓库来源判定平台**时兜底，且返回的必是已实装托管全流程的平台
    （其余平台由 host_repo 显式 422，不走到这里）。
    """
    from src.api.deps import get_user_token_for as _tok_for
    uid = str(user.get("id") or "")
    for _p in _default_token_provider_order():
        try:
            _tok = _tok_for(user["username"], user_id=uid, provider=_p)
        except Exception:
            _tok = ""
        if _tok:
            return _p, _tok
    return "", ""


_HOOKS_404_DELETED_PROVIDERS = frozenset({"gitcode"})

# 托管全流程（bot 合作者 + webhook 建/删/探 + 库回写）已实装的平台 =
# provider 模块声明 HOSTING_FULL_FLOW 的集合（模块能力位驱动，routes 零平台字面量）。
# 其余在册平台可用 /repair-host 的卡驱动兜底，但 host 全流程未接入——必须显式拒绝，
# 不能兜底成别的平台（否则 webhook 建到错误平台、repo_key 前缀错）。
def _hosting_implemented_providers() -> frozenset:
    """D109 判据收敛：统一走能力视图（与 _MODULES 声明逐字节等价，test_d109_dual_predicate 对拍）。"""
    from src.domains.platform.public import hosting_providers
    return hosting_providers()

# 仓库平台目录 = 平台卡（卡驱动，2026-11-12）：不再在 routes 里二分平台字面量。
# 此前 ("gitcode","github") 硬编码导致已绑定 gitee 的用户永远拉不到仓库、
# ?provider=gitee 被静默忽略（前端 Gitee Tab 恒空）。拉取走 token_resolver 端口
# create_provider(p).fetch_repos_with_role（S62 契约），新增平台零 routes 改动。
def _repo_listable_providers() -> set:
    """可列仓平台集合 = 在册平台卡目录。"""
    try:
        from src.domains.runtime.public import list_vcs_platforms
        return set(list_vcs_platforms())
    except Exception as e:
        log.warning(f"repo platform directory unavailable: {e}")
        return set()


def _repo_key_matches(repo_key: str, provider: str) -> bool:
    """repo_key 是否属于 provider（单一权威 = kernel.repo_key.parse_repo_key）。

    parse_repo_key 已定义"无前缀=默认平台（gitcode）"的归属口径，此处直接复用——
    routes 不再持有平台字面量；未知前缀（gitlab: 等）按前缀精确判定，不折算成默认平台。
    """
    from src.kernel.repo_key import parse_repo_key
    return parse_repo_key(repo_key or "")[0] == provider


def _credential_broken_label(reason: str) -> str:
    """凭据不可用的原因文案（两支：解不开 / 无可用令牌）。"""
    if reason in ("decrypt_failed", "parse_failed"):
        return "凭据无法解密（记录已损坏或密钥已变更）"
    return "登录授权已失效（无可用令牌）"


def _credential_broken_notice(broken_providers, broken_reason) -> dict:
    """凭据失效平台的响应提示（口径乙，2026-11-18 用户拍板）。

    坏平台仓库不进返回列表，但必须显式告知「某平台凭据失效，已隐藏其仓库」；
    /repos/member 与 /repos/sync 同口径，禁止静默剔除。方案甲（把坏平台缓存行
    当正常数据返回）留作有离线可见需求时的增强，且必须带 stale 标记与禁用操作。
    """
    providers = sorted(str(p) for p in (broken_providers or []))
    if not providers:
        return {}
    _reason = broken_reason or {}
    if len(providers) == 1:
        _p = providers[0]
        _hint = (f"{_p} 的{_credential_broken_label(_reason.get(_p, ''))}，"
                 f"已隐藏其仓库，请重新配置 {_p} 的 PAT 或重新授权一次")
    else:
        _parts = [f"{p}（{_credential_broken_label(_reason.get(p, ''))}）" for p in providers]
        _hint = ("以下平台的凭据失效：" + "、".join(_parts)
                 + "；已隐藏其仓库，请重新配置 PAT 或重新授权")
    return {"hint": _hint, "_credential_broken": providers, "_reauth_provider": providers[0]}


def _drop_broken_platform_rows(rows, broken_providers) -> list:
    """剔除凭据失效平台的仓库行（含缓存行）——口径乙：坏平台数据一律不展示。"""
    _broken = {str(p) for p in (broken_providers or [])}
    if not _broken or not rows:
        return list(rows or [])
    from src.kernel.repo_key import parse_repo_key
    return [r for r in rows if parse_repo_key(r.get("repo_key") or "")[0] not in _broken]


def _fetch_and_cache(user_id: int, api_token: str, provider: str = "gitcode") -> list[dict]:
    """从 VCS 拉取仓库列表，返回结果（不写缓存——由调用方合并后一次性写入）

    2026-11-12：走 S62 provider 端口（create_provider(provider).fetch_repos_with_role），
    平台差异由平台卡/Provider 子类承担，routes 不再持有平台分支；repo_key 前缀
    统一由 provider 派生（不再折叠 gitcode）。
    """
    from src.kernel.token_resolver import create_provider
    repos = create_provider(provider).fetch_repos_with_role(api_token)
    if not repos:
        # FR-5.4: 空结果可能是 token scope 受限，记 warning
        log.warning(f"_fetch_and_cache: {provider} returned empty for user (token may lack repo read scope)")
    for r in repos:
        if not r.get("repo_key"):
            r["repo_key"] = f"{provider}:{r.get('full_name', '')}"
    return repos


def _filter_repos(user_id: int, repos: list[dict], org_id: str = "") -> list[dict]:
    """过滤仓库列表：返回用户身份可见的全部仓库（管理员 + 非管理员成员）。

    org_id 非空时组织隔离：
    - 已托管到当前组织的仓库 → 显示，is_hosted=true
    - 已托管到其他组织的仓库 → 隐藏（隔离）
    - 未托管的仓库 → 显示，用户可托管到当前组织
    """
    from src.kernel.repo_key import to_repo_key
    # 获取所有托管仓库及其 org_id
    from src.kernel.session_manager import db as _db, db_lock as _dbl
    managed = {}
    managed_org = {}
    managed_org_name = {}
    managed_status = {}
    with _dbl:
        _c = _db.cursor()
        try:
            _c.execute(
                "SELECT m.repo_key, m.webhook_id, m.org_id, m.host_status, COALESCE(o.name,'') "
                "FROM managed_repos m LEFT JOIN organization o ON o.id=m.org_id")
            for _r in _c.fetchall():
                managed[_r[0]] = _r[1]
                managed_org[_r[0]] = _r[2] or ""
                managed_status[_r[0]] = _r[3] or ""
                managed_org_name[_r[0]] = _r[4] or ""
        except Exception:
            # 降级：不带 org_id
            for r in user_db.get_all_managed_repos():
                managed[r["repo_key"]] = r["webhook_id"]
                managed_org[r["repo_key"]] = ""
                managed_org_name[r["repo_key"]] = ""
                managed_status[r["repo_key"]] = r.get("host_status", "") or ""
    result = []
    for r in repos:
        full_name = r.get("full_name", "")
        repo_key = to_repo_key(r.get("repo_key", "") or full_name)
        is_admin = r.get("is_admin", False)
        in_managed = repo_key in managed
        hosted = in_managed and bool(managed.get(repo_key))
        repo_org_id = managed_org.get(repo_key, "")

        # 组织隔离：
        # - 团队组织（org_id 非空）：只显示该组织关联的仓库
        # - 个人空间（org_id 为空）：显示该用户全部仓库（含已关联团队组织的仓库），
        #   通过 org_id/org_name 给前端做组织徽标（Issue #2264：关联组织后仓库不应消失）
        if in_managed and repo_org_id and org_id and repo_org_id != org_id:
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
            "host_status": managed_status.get(repo_key, "") if in_managed else "",
            "created_at": r.get("created_at", "") or "",
            "org_id": (managed_org.get(repo_key, "") if in_managed else ""),
            "org_name": (managed_org_name.get(repo_key, "") if in_managed else ""),
        })
    return result


def _platform_api_root(provider: str, owner: str, repo: str) -> str:
    """按平台卡拼原生 API 根（2026-11-12 卡驱动）。

    - dialect=gitlab（gitlab 卡）：`{api_base}/projects/{urlencoded owner/repo}`
    - 其余（github 方言：gitcode/gitee/github）：`{api_base}/repos/{owner}/{repo}`
    """
    import urllib.parse
    from src.domains.runtime.public import get_platform_card
    card = get_platform_card(provider)
    if card is None:
        raise ValueError(f"平台卡缺失: {provider}")
    base = (card.api_base or "").rstrip("/")
    if not base:
        raise ValueError(f"平台卡 api_base 为空: {provider}")
    if (card.dialect or "").lower() == "gitlab":
        # 方言值比较（card.dialect 是卡字段，不是平台名分派）：gitlab 系 API 以
        # projects/<url-encoded full path> 寻址，与 gitee_v5/github_web 的
        # repos/<owner>/<repo> 不同。守卫 test_repos_no_platform_branch 只锁
        # provider 变量与平台名的比较，此形态不在其列（评审 2026-11-13 已确认）。
        return f"{base}/projects/{urllib.parse.quote(f'{owner}/{repo}', safe='')}"
    return f"{base}/repos/{urllib.parse.quote(owner, safe='')}/{urllib.parse.quote(repo, safe='')}"


def _platform_api_get(provider: str, token: str, url: str, timeout: int = 5):
    """按卡 api_auth 施加凭据并发 GET：query_token→access_token 查询参数；
    private_token→PRIVATE-TOKEN 头；其余→Bearer 头。"""
    import urllib.parse, urllib.request
    from src.domains.runtime.public import get_platform_card
    card = get_platform_card(provider)
    auth = ((card.api_auth if card else "") or "").lower()
    headers = {"Accept": "application/json", "User-Agent": "Grape-DevOps"}
    if token:
        if auth == "query_token":
            sep = "&" if "?" in url else "?"
            url = f"{url}{sep}access_token={urllib.parse.quote(token, safe='')}"
        elif auth == "private_token":
            headers["PRIVATE-TOKEN"] = token
        else:
            headers["Authorization"] = f"Bearer {token}"
    return urllib.request.urlopen(urllib.request.Request(url, headers=headers, method="GET"), timeout=timeout)


def _probe_repo_token(username: str, user_id, provider: str) -> str:
    """探活用平台 token（provider 无关：凭据域 PAT 优先 → OAuth，带预刷新）。"""
    try:
        from src.api.deps import get_user_token_for
        return get_user_token_for(username, user_id=str(user_id), provider=provider) or ""
    except Exception as e:
        log.warning(f"probe: token lookup failed for provider={provider}: {e}")
        return ""


def _probe_stale_repos(user_id):
    """检查当前用户名下的托管仓库在 VCS 上的状态，清理失效的托管记录。

    2026-11-12 卡驱动改造：按每个托管仓 repo_key 的 provider 走**对应平台卡**的
    api_base/api_auth/dialect，token 按 provider 单独解析。此前是
    "非 github 一律 gitcode.com + gitcode token"，把 gitee 托管仓拿去 gitcode
    探测（实测 404，二次复核 401，仅因 401 走"跳过"分支才没误删）。

    两步检查：
    1. 并发查仓库是否存在（404 = 已删除 → bot token 复核后清理）
    2. 仓库存在的，串行查 webhook 是否还在（hooks 不可见/404 = 不确定 → 只标记待修复）

    fail-safe：平台未在册 / 该平台无可用 token / 非 404 → 不做判定（不误删）。
    """
    import concurrent.futures, urllib.request, urllib.error, json as _json
    from src.kernel.repo_key import parse_repo_key

    all_repos = user_db.get_managed_repos(user_id)
    if not all_repos:
        return

    # 平台卡目录：未在册平台不探活（不判定=不删数据）
    from src.domains.runtime.public import list_vcs_platforms
    try:
        _listable = set(list_vcs_platforms())
    except Exception:
        log.warning("_probe_stale_repos: platform card directory unavailable, skip probe round")
        return

    _username = ""
    try:
        from src.domains.mgmt.auth.grape_account_db import get_grape_account_by_id
        _username = (get_grape_account_by_id(user_id) or {}).get("username", "") or ""
    except Exception:
        pass

    _token_cache = {}

    def _token_for(provider: str) -> str:
        if provider not in _token_cache:
            _token_cache[provider] = _probe_repo_token(_username, user_id, provider)
        return _token_cache[provider]

    # 第一步：并发查仓库是否存在（404 = 已删除）
    def _check_exists(item):
        r = item
        owner = r.get("repo_owner", "")
        repo_name = r.get("repo_name", "")
        if not owner or not repo_name:
            return None
        repo_key = r["repo_key"]
        provider, _, _ = parse_repo_key(repo_key)
        if provider not in _listable:
            return None  # 未在册平台，不做判定
        token = _token_for(provider)
        if not token:
            return None  # 该平台无可用凭据，跳过检查（不误判/不误删）
        try:
            with _platform_api_get(provider, token, _platform_api_root(provider, owner, repo_name)) as resp:
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
        if provider not in _listable:
            continue
        _tok = _token_for(provider)
        if not _tok:
            continue
        try:
            with _platform_api_get(provider, _tok, _platform_api_root(provider, owner, repo_name) + "/hooks") as resp:
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
            # GitHub 404 = "token 有效但无 admin 权限"（GitHub 惯例：不可见即 404）。
            # 本分支能进来说明第一步刚用同一 token 查仓库本体是 200——仓库存在、
            # 紧接着 hooks 才 404，只可能是权限问题而非仓库消失，直接跳过不判 stale。
            # （GitCode 对无权限回 403，走上面的注释路径，不受影响。）
            # hooks 探测错误语义按平台保留（m2 事故教训：GitHub 对非 admin 查 hooks 回 404，
            # 曾误判 stale 级联清空 6 个托管仓）：
            # - gitcode：404 = 仓库可能刚被删 → 进删除候选（后续 bot token 双信号复核）
            # - 其余平台：404/401/403 = 权限/不可见/不确定 → 跳过不判，也不置待修复
            #   （hook 大概率原样还在，置 pending-repair 会把权限问题误报成故障）
            if e.code == 404 and provider in _HOOKS_404_DELETED_PROVIDERS:
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
                    with _platform_api_get(provider, bot_token, _platform_api_root(provider, owner, repo_name)) as resp:
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
            user_db.set_host_status(repo_key, "webhook_missing", "create_webhook",
                                    _host_error_text("webhook_lost"))
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

    - 平台门槛：按平台卡目录判定（2026-11-12；此前只认 gitcode/github，
      gitee-only 用户永不探活，gitee 托管仓也从不被正确探活）；
    - 节流：同用户在 REPO_PROBE_INTERVAL 内不重复全量探测；
    - token 解析下移到 _probe_stale_repos（按每个 repo_key 的 provider 各自取）。
    """
    import time as _time
    from src.domains.runtime.public import list_vcs_platforms
    try:
        _listable = set(list_vcs_platforms())
    except Exception:
        return
    if not [p for p in (providers or []) if p in _listable]:
        return  # 无任何在册平台 token，不探活
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
            _probe_stale_repos(uid)
        except Exception as e:
            log.warning(f"member: stale probe failed (non-fatal): {e}")

    import threading
    threading.Thread(target=_run, daemon=True).start()


def _reauth_hint(provider: str, user: dict) -> "tuple[str, str]":
    """token 失效的用户口径（OAuth 与纯 PAT 两种来源分开说）。

    返回 (hint, action)。纯 PAT 账号不存在"重新登录授权"这回事——让他去重新登录
    只会回到同一个死循环；正确动作是到设置页重新配置 PAT。
    """
    has_identity = False
    try:
        from src.domains.mgmt.auth.grape_account_db import get_user_identities

        has_identity = any(str(i.get("provider") or "") == str(provider)
                           for i in get_user_identities(str(user.get("id"))))
    except Exception as e:  # 身份域不可用不影响文案兜底
        log.warning(f"_reauth_hint: identity scan skipped for {provider}: {e}")
    if has_identity:
        return (f"{provider} 登录授权已过期，请重新授权",
                "reauth_required")
    return (f"{provider} 凭据失效（PAT 可能已在平台侧删除或过期），"
            f"请到设置页重新配置 {provider} PAT",
            "credential_invalid")


@router.get("/member", operation_id="repos_member", summary="List repos I can operate", description="Lists repositories the current user can operate. Reads the user_repos cache, auto-refreshing from VCS when empty or stale; prunes managed repos removed upstream. Supports ?provider=gitcode|github to filter by platform (需求 17 项目页按平台切换).")
def list_member_repos(user: dict = Depends(require_org_member), provider: str = "", refresh: int = 0):
    """获取用户可操作仓库列表：优先从 user_repos 缓存读取；缓存为空或超过 TTL 时自动从 VCS 拉取刷新

    ?refresh=1 强制跳过缓存直接回源并落库，供写操作（建仓/托管/解除托管）后的
    "立刻要准"路径使用；不带该参数时仍走 30 分钟 TTL 缓存，避免 N+1 权限查询。

    需求 17: 支持 ?provider=gitcode|github 按平台过滤。
    不传 provider 时拉取用户绑定的全部平台仓库（合并返回），不再只拉一个平台。

    加载前先检查托管仓库在 GitCode 上是否还存在（GitCode 删仓库不发 webhook，
    grape 收不到通知，旧记录残留导致重建同名仓库时前端误显"已托管"）。
    """
    from datetime import datetime

    # Issue #343 修复: 不再预获取 token（with_oauth_token 内部获取+预刷新），
    # 只需确定用户绑定了哪些平台
    from src.domains.mgmt.auth.grape_account_db import list_platform_links
    _listable = _repo_listable_providers()
    # 可列仓平台 = 身份行 ∪ 凭据域 <p>_pat（统一入口 list_platform_links，A 批）：
    # PAT 是仓库执行凭据、不是身份绑定，只配 PAT 的账号同样可列仓。
    # token 不可用的平台不算"可列仓"（历史派生密钥解不出来 / 空 token）：否则必 401 重授权，
    # 把本可渲染引导页的入口打成报错（2026-11-16 yangaiwu 现场）。
    _links = list_platform_links(user["id"])
    # 2026-11-18：「用户声明过这个平台」与「这个平台的凭据现在可用」拆成两个集合。
    # 只用一个布尔会把"配置在但坏了"折叠成"没配置"，用户看到的是引导页 + 错误文案，
    # 而库里凭据行和仓库缓存其实都在（yangaiwu 现场：6 条 PAT 解不开 → 整块列表失踪）。
    _declared_providers = {i["provider"] for i in _links if i.get("provider") in _listable}
    _bound_providers = {i["provider"] for i in _links
                        if i.get("provider") in _listable and i.get("token_usable", True)}
    _broken_providers = _declared_providers - _bound_providers
    _broken_reason = {i["provider"]: str(i.get("credential_error") or "")
                      for i in _links if i.get("provider") in _broken_providers}

    def _broken_hint(providers) -> str:
        """定向请求（400）用的单平台文案：说清原因 + 处置动作。"""
        p = sorted(providers)[0]
        return (f"{p} 的{_credential_broken_label(_broken_reason.get(p, ''))}，"
                f"请到设置页重新配置 {p} 的 PAT，或重新授权一次")

    # 口径乙（2026-11-18）：坏平台仓库一律不进返回列表，但每个 200 出口都必须带
    # hint + _credential_broken —— member / sync 同口径，禁止静默剔除。
    # 平台被本实例关闭（enabled=0）时用户面根本不存在该平台，别把它写进提示条。
    from src.domains.platform.public import is_platform_visible as _is_platform_visible
    _notice = _credential_broken_notice(
        {p for p in _broken_providers if _is_platform_visible(p)}, _broken_reason)

    # 用户面可见性（2026-11-15 方案甲）：真源=平台行 enabled。disabled 平台对用户面不提供，
    # 内网/脚本面照旧可用（托管/建仓/bot/webhook 不经这里）。判定必须排在"无绑定早退"之前，
    # 否则"已禁用"会被解释成"你没绑定"（线上实跑抓到过的顺序 bug）。
    from src.domains.platform.public import is_platform_visible
    if provider and not is_platform_visible(provider):
        raise HTTPException(status_code=403, detail={
            "code": 403, "error_code": "PLATFORM_DISABLED",
            "message": f"{provider} 已在本实例对用户面关闭（grape_t_repo_platforms.enabled=0），用户面不可列仓"})

    # 组织过滤参数（提前到这里：下面「无可用凭据」的缓存兜底也要用它）
    org_id = user.get("org_id", 0)
    _is_personal_org = False
    if org_id:
        from src.domains.mgmt.user.org_db import is_org_personal
        _is_personal_org = is_org_personal(org_id)
    _filter_org_id = 0 if _is_personal_org else org_id

    if not _bound_providers:
        # 定向请求（?provider=<p>）不接受"降级成引导页"：调用方明确要这个平台的数据，
        # 直接给出可处置的错误（凭据坏了 → CREDENTIAL_UNAVAILABLE，没绑 → 未配置）。
        if provider in _listable:
            if provider in _broken_providers:
                raise HTTPException(status_code=400, detail={
                    "code": 400, "error_code": "CREDENTIAL_UNAVAILABLE",
                    "needs_reauth": True, "provider": provider,
                    "credential_error": _broken_reason.get(provider, ""),
                    "message": _broken_hint({provider}),
                })
            raise HTTPException(status_code=400, detail={
                "code": 400, "message": f"{provider} API Token 未配置，请先绑定 {provider} 平台"})
        # D102-C: 纯登录渠道账号（华为是第一个实例——有登录无仓库平台绑定）
        # 返回 200 + 空列表 + 标记位（原 400 文案"请先通过 OAuth 登录"与用户
        # 刚 OAuth 登录完的事实自相矛盾）；前端按标记走"去绑定代码平台"引导页。
        # 2026-11-13 从 rebase 丢失中恢复（守卫 test_d102c_login_only_channel 红实锤）。
        # 判据叠加「凭据可用性」后，这里是「没有可用绑定」的统一出口。此时要区分三种人：
        #   ① 真没绑代码平台 → 引导去绑定；
        #   ② 绑了但 token 不可用（历史派生密钥解不出 / refresh 为空 / PAT 已清）
        #      → 提示重新授权，否则文案会误导成"你绑的不是代码平台"（2026-11-16 现场）；
        #   ③ 凭据在但读不出来（密文解不开）
        #      → 口径乙（2026-11-18 用户拍板）：该平台缓存仓库**也不展示**（旧数据
        #        当正常数据返回会让用户点进去才发现不可用），只给可处置提示
        #        「…已隐藏其仓库…」；member / sync 同口径。
        _hint = "绑定的登录渠道不是代码托管平台——绑定 Gitee/GitCode 等代码平台后，仓库会显示在这里"
        return {
            "repos": [],
            "_no_vcs_binding": True,
            **(_notice or {"hint": _hint}),
        }

    # 需求 17: 前端显式指定 provider 时只拉该平台（可列仓平台集合=平台卡目录驱动）
    if provider in _listable:
        if provider not in _bound_providers:
            if provider in _broken_providers:
                # 已声明但凭据不可用：不再是"未配置"（文案会让人以为没绑），
                # 给出可处置的错误码 + 重新配置提示。
                raise HTTPException(status_code=400, detail={
                    "code": 400, "error_code": "CREDENTIAL_UNAVAILABLE",
                    "needs_reauth": True, "provider": provider,
                    "credential_error": _broken_reason.get(provider, ""),
                    "message": _broken_hint({provider}),
                })
            raise HTTPException(status_code=400, detail={"code": 400, "message": f"{provider} API Token 未配置，请先绑定 {provider} 平台"})
        _providers = [provider]
    else:
        # 合并列表只含用户面可见平台（disabled 平台即便有绑定也不掺进来）——
        # 严格语义：不提供 = 谁都看不到，避免"看得到但配不了凭据"的半可用态。
        _providers = [p for p in _bound_providers if is_platform_visible(p)]

    # 优先读取 DB 缓存（未过期）——按组织过滤
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
        """按 provider 过滤缓存（repo_key 前缀匹配，裸键归 gitcode）"""
        if provider in _listable and len(_providers) == 1:
            return [r for r in repos_list if _repo_key_matches(r.get("repo_key", ""), provider)]
        return repos_list

    if not refresh and cached and not _is_expired():
        return {"repos": _filter_repos(user["id"], _drop_broken_platform_rows(_filter_by_provider(cached), _broken_providers), org_id=_filter_org_id),
                "_cached": True, **_notice}

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
        # 部分成功也落库（只替换取到数据的平台，失败平台旧缓存保留）
        user_db.save_user_repos(user["id"], _deduped,
                                providers=[p for p in _providers if p not in failed_providers])
        return {"repos": _filter_repos(user["id"], _drop_broken_platform_rows(_filter_by_provider(_deduped), _broken_providers), org_id=_filter_org_id),
                **_notice, "_partial": True, "_reauth_provider": _reauth_provider}
    # 有平台需要重新授权且无任何结果：
    # - 显式 ?provider=<p>（定向请求）→ 仍抛 401，调用方明确要该平台数据；
    # - 合并列表（默认工作区入口）→ 降级成「无可用绑定」引导页（200 + _no_vcs_binding）。
    #   2026-11-16 实锤：清理 PAT / 平台侧吊销后，工作区整页被 401 打成「加载失败:
    #   gitcode 授权已过期」，而用户既没有可点的处置入口、也不该被当成授权过期——
    #   此时他就是一个「没有任何可用仓库绑定」的账号，应按引导页渲染。
    if _reauth_provider and not all_repos and not cached:
        _hint, _action = _reauth_hint(_reauth_provider, user)
        if provider in _listable:
            raise HTTPException(status_code=401, detail={
                "code": 401, "message": _hint, "action": _action,
                "provider": _reauth_provider,
            })
        _base = {
            "repos": [],
            "_no_vcs_binding": True,
            "_reauth_provider": _reauth_provider,
            "hint": _hint,
        }
        _base.update(_notice)   # 凭据失效原因优先于泛化的"授权过期"文案
        return _base

    if not all_repos and failed_providers and cached:
        # 全部平台都失败 → 用旧缓存兜底（坏平台行按口径乙剔除，附提示不静默）
        return {"repos": _filter_repos(user["id"], _drop_broken_platform_rows(_filter_by_provider(cached), _broken_providers), org_id=_filter_org_id),
                "_cached": True, "_stale": True, **_notice}
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

    # 合并后一次性写入缓存；只替换本次刷新成功的平台（失败平台旧缓存保留）
    _ok_providers = [p for p in _providers if p not in failed_providers]
    user_db.save_user_repos(user["id"], repos, providers=_ok_providers)

    # 缓存过期/重建路径：异步 + 节流地启动失效托管探活（请求不等它）
    try:
        _start_stale_probe_async(user, _providers)
    except Exception as e:
        log.warning(f"member: stale probe start failed (non-fatal): {e}")

    # 部分平台失败时附加提示
    result = {"repos": _filter_repos(user["id"], _drop_broken_platform_rows(_filter_by_provider(repos), _broken_providers), org_id=_filter_org_id), "_cached": False}
    if failed_providers:
        result["_partial"] = True
        result["_failed_providers"] = failed_providers
    result.update(_notice)   # 凭据失效平台：仓库已剔除，但必须显式告知（口径乙）
    return result


@router.post("/sync", operation_id="repos_sync", summary="Sync repo list from VCS", description="Manually syncs the repository list from VCS, refreshes the user_repos cache and probes/cleans stale managed records.")
def sync_repos(user: dict = Depends(require_org_member)):
    """手动从 VCS 同步仓库列表，刷新 user_repos 缓存，并探活清理失效托管记录。

    组织隔离：同步 GitCode 全量到 user_repos 缓存，但返回时按当前组织过滤
    （只返回该组织已托管的仓库 + 个人组织返回全量）。
    """
    # Issue #343 修复: 不再预获取 token（with_oauth_token 内部获取+预刷新）
    from src.domains.mgmt.auth.grape_account_db import list_platform_links
    _listable = _repo_listable_providers()
    # 可同步平台 = 身份行 ∪ 凭据域 <p>_pat（统一入口 list_platform_links，A 批）
    # 2026-11-18：必须与 /repos/member 同口径只取 token_usable 的平台——
    # 否则凭据不可用的平台也会被拿去拉仓，全部失败后统一抛 502「同步失败」，
    # 用户点一次刷新只得到一句无关的错误（实测 12:51:16 两次）。
    _links = list_platform_links(user["id"])
    _providers = [i["provider"] for i in _links
                  if i.get("provider") in _listable and i.get("token_usable", True)]
    # 口径乙（2026-11-18）：部分平台凭据失效时也要显式提示「已隐藏其仓库」，
    # 与 /repos/member 同一个 notice 构造器，禁止静默只同步好平台。
    _broken_sync = {i["provider"] for i in _links
                    if i.get("provider") in _listable and not i.get("token_usable", True)}
    _broken_reason_sync = {i["provider"]: str(i.get("credential_error") or "")
                           for i in _links if i.get("provider") in _broken_sync}
    from src.domains.platform.public import is_platform_visible as _is_platform_visible_sync
    _notice_sync = _credential_broken_notice(
        {p for p in _broken_sync if _is_platform_visible_sync(p)}, _broken_reason_sync)
    if not _providers:
        _broken = sorted(_broken_sync)   # 与上面的 notice 共用同一集合，别算两遍
        if _broken:
            _first = _broken[0]
            _reason = next((str(i.get("credential_error") or "") for i in _links
                            if i.get("provider") == _first), "")
            _msg = (f"{_first} 的访问凭据无法解密（记录已损坏或密钥已变更），请重新配置 PAT 或重新授权"
                    if _reason in ("decrypt_failed", "parse_failed")
                    else f"{_first} 的登录授权已失效，请重新授权或配置 PAT")
            raise HTTPException(status_code=400, detail={
                "code": 400, "error_code": "CREDENTIAL_UNAVAILABLE",
                "needs_reauth": True, "providers": _broken,
                "credential_error": _reason, "message": _msg,
            })
        raise HTTPException(status_code=400, detail={"code": 400, "message": "API Token 未配置"})

    from src.domains.mgmt.user.repo_manager import _dialect_of as _dialect_of_sync
    if any(_dialect_of_sync(p) == "gitee_v5" for p in _providers):
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
    # 只替换刷新成功的平台，失败平台旧缓存保留（与 member 同口径）
    user_db.save_user_repos(user["id"], repos,
                            providers=[p for p in _providers if p not in failed_providers])

    org_id = user.get("org_id", "")
    # 个人组织不隔离（显示全量用户仓库），非个人组织才隔离
    from src.domains.mgmt.user.org_db import is_org_personal
    _filter_org_id = "" if is_org_personal(org_id) else org_id
    if _has_reauth:
        _msg = f"同步成功（{_reauth_provider} 授权已过期，请重新登录授权）"
        if _filter_org_id:
            return {"repos": _filter_repos(user["id"], repos, org_id=_filter_org_id),
                    **_notice_sync, "message": _msg, "_partial": True,
                    "_reauth_provider": _reauth_provider}
        return {"repos": _filter_repos(user["id"], repos, org_id=0),
                **_notice_sync, "message": _msg, "_partial": True,
                "_reauth_provider": _reauth_provider}
    if _filter_org_id:
        # 非个人组织：已托管到其他组织的仓库不显示，未托管的显示
        return {"repos": _filter_repos(user["id"], repos, org_id=_filter_org_id), "message": "同步成功",
                **_notice_sync}
    return {"repos": _filter_repos(user["id"], repos, org_id=0), "message": "同步成功",
            **_notice_sync}


@router.get("/check-name", operation_id="repos_check_name", summary="Check repo name availability", description="Checks whether a repository with the same name already exists under the current user.")
def check_repo_name(name: str, user: dict = Depends(require_no_session_token)):
    """检查当前用户名下是否已存在同名仓库"""
    provider, api_token = _get_user_provider_and_token(user["username"], user.get("login_provider", ""), user.get("id", 0))
    if not api_token:
        raise HTTPException(status_code=400, detail={"code": 400, "message": "API Token 未配置"})

    # B 批（2026-11-13）：平台无关——登录名走 provider.verify_token（卡驱动 /user），
    # 存在性走 VcsApiClient.get_repo_info（卡驱动 repo 端点），零平台名分支。
    from src.kernel.token_resolver import create_provider
    from src.domains.runtime.public import VcsApiClient
    try:
        _uinfo = create_provider(provider).verify_token(api_token)
        login = str((_uinfo or {}).get("login", "") or "") if isinstance(_uinfo, dict) else ""
        if not login:
            raise HTTPException(status_code=502, detail={"code": 502, "message": "无法获取当前用户身份"})
        _repo_info = VcsApiClient(provider, api_token, login, name).get_repo_info()
        if isinstance(_repo_info, dict) and str(_repo_info.get("error", "")).strip() == "HTTP 404":
            return {"exists": False}
        if isinstance(_repo_info, dict) and _repo_info.get("error"):
            raise HTTPException(status_code=502, detail={
                "code": 502, "message": f"检查失败: {_repo_info['error']}"})
        return {"exists": True, "full_name": f"{login}/{name}"}
    except HTTPException:
        raise
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
    owner = (body.owner or "").strip()
    if owner and not _re.match(r'^[\w\-\.]+$', owner):
        raise HTTPException(status_code=400, detail={"code": 400, "message": "所属空间只能包含字母、数字、下划线、连字符和点"})
    repo_path = (body.path or "").strip()
    if repo_path and not _re.match(r'^[\w\-\.]+$', repo_path):
        raise HTTPException(status_code=400, detail={"code": 400, "message": "仓库路径只能包含字母、数字、下划线、连字符和点"})

    if body.visibility in ("private", "public"):
        private = (body.visibility == "private")
    else:
        private = body.private

    # R26: 优先用用户指定的 provider；未指定时 fallback 到自动选择
    if body.provider:
        provider = body.provider
        # B 批（2026-11-13）：token 解析零平台分支——统一入口内部按 provider 走
        # ①凭据域 PAT → ②OAuth 身份域（github 专用 getter 本就是本入口的包装）
        api_token = get_user_token_for(user["username"], user_id=str(user.get("id") or ""), provider=provider)
    else:
        provider, api_token = _get_user_provider_and_token(user["username"], user.get("login_provider", ""), user.get("id", 0))
    if not api_token:
        raise HTTPException(status_code=400, detail={"code": 400, "message": "API Token 未配置，请先通过 OAuth 登录或配置个人 Token"})

    # B 批（2026-11-13）：建仓走 provider 端口（GitHub/GitCode/GitLab 各自实现），
    # 路由层不再按平台名分派 create_github_repo / create_repo。
    from src.kernel.token_resolver import create_provider, create_repo_kwargs
    try:
        repo_info = create_provider(provider).create_repo(
            api_token=api_token, name=name, description=body.description, private=private,
            owner=owner, path=repo_path,
            **create_repo_kwargs(provider, body.auto_init, body.default_branch))
    except Exception as e:
        raise HTTPException(status_code=502, detail={"code": 502, "message": f"创建仓库失败: {e}"})
    if isinstance(repo_info, dict) and repo_info.get("error"):
        raise HTTPException(status_code=502, detail={
            "code": 502, "message": f"创建仓库失败: {repo_info.get('error')}"})

    full_name = repo_info.get("full_name") or ""
    owner = repo_info.get("owner") or ""
    if isinstance(owner, dict):
        owner = owner.get("login") or owner.get("username") or owner.get("name") or ""
    # GitCode 建仓响应不带 owner 字段（只有 namespace/human_name），此前 owner 落空
    # 导致 webhook 拼成 /repos//<name>/hooks → 404、托管永久 partial。统一从
    # full_name 兜底解析，github/gitcode/其它卡路径口径一致。
    if not owner and "/" in full_name:
        owner = full_name.split("/", 1)[0]
    repo_name = repo_info.get("name") or (full_name.split("/", 1)[1] if "/" in full_name else name)

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
            # Issue #671: 托管未完成也兜底注册最小 managed_repos 行（幂等），
            # 保证新仓库进入注册链路（repair-host / A2A / 事件可见），不再悬空。
            try:
                _repo_key = to_repo_key(full_name, provider=provider)
                user_db.add_managed_repo(_repo_key, owner, repo_name)
                user_db.mark_user_repo_join(user["id"], _repo_key, is_join=True, is_admin=True)
                user_db.set_host_status(_repo_key, "webhook_missing", "configure_webhook_url", _host_error_text("webhook_url_missing"))
            except Exception as _reg_e:
                log.warning(f"create_new_repo: fallback registration failed: {_reg_e}")
            try:
                _fetch_and_cache(user["id"], api_token, provider)
            except Exception:
                pass
            return {"repo": repo_info, "full_name": full_name, "owner": owner, "name": repo_name, "hosted": False, "message": "仓库已创建，但 webhook_url 未配置，请稍后手动托管"}
        # B 批（2026-11-13）：托管准备全部走平台卡配置（get_hosting_provider_config），
        # 回调后缀/全局 bot/webhook 三件套/db 列名/建 hook 鉴权参数均卡派生。
        from src.domains.mgmt.user.repo_manager import get_hosting_provider_config
        _hcfg = get_hosting_provider_config(provider)
        if "/webhook/" in webhook_url:
            webhook_url = webhook_url.split("/webhook/", 1)[0] + _hcfg["webhook_url_suffix"]

        # Issue #372: 路径 B 支持——无全局 bot token 时用用户 token + 随机密钥
        import secrets as _secrets
        _create_global_bot_token = _hcfg["bot_token"]
        _use_path_b = not _create_global_bot_token
        _webhook_secret = _secrets.token_urlsafe(32) if _use_path_b else ""
        try:
            _wh_token = _create_global_bot_token or api_token  # P2-7: 路径 A 用 bot token，路径 B 用用户 token
            existing_id = _host_call_with_ready_retry(
                _hcfg["find_webhook"], _wh_token, owner, repo_name, webhook_url)
            repo_key = to_repo_key(full_name, provider=provider)
            wh_col = _hcfg["db_token_field"]
            _bot_token_col = _hcfg["db_bot_token_field"]
            if existing_id:
                # Issue #372 R6.2: 路径 B 复用已有 webhook 时密钥不匹配 → 删旧重建
                if _use_path_b:
                    try:
                        _hcfg["remove_webhook"](_wh_token, owner, repo_name, existing_id)
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
                hook_id = _host_call_with_ready_retry(
                    _hcfg["setup_webhook"], _wh_token, owner, repo_name, webhook_url,
                    **_hcfg["setup_webhook_kwargs"](_webhook_secret, _wh_token, _use_path_b))
                # P1-2: 路径 B 成功时写空串（不写 stale 用户 token），webhook 验证依赖 webhook_secret
                _db_wh_token = "" if _use_path_b else _wh_token
                user_db.add_managed_repo(repo_key, owner, repo_name, webhook_id=hook_id,
                    **{wh_col: _db_wh_token, _bot_token_col: (_create_global_bot_token if not _use_path_b else "")},
                    host_user_id=(user["id"] if _use_path_b else 0), webhook_secret=_webhook_secret)
                user_db.mark_user_repo_join(user["id"], repo_key, is_join=True, is_admin=True)
            from src.api.session_store import ensure_default_agent_sessions
            ensure_default_agent_sessions(repo_key)
        except Exception as e:
            # Issue #671: 托管失败也兜底注册最小 managed_repos 行（幂等）+ 标记 partial，
            # 保证仓库进入注册链路，用户可经 repair-host 自愈（未注册仓 repair-host 会 403）。
            try:
                _repo_key = to_repo_key(full_name, provider=provider)
                user_db.add_managed_repo(_repo_key, owner, repo_name)
                user_db.mark_user_repo_join(user["id"], _repo_key, is_join=True, is_admin=True)
                user_db.set_host_status(_repo_key, "partial", "host_setup", _host_error_text(_classify_host_error(e)))
            except Exception as _reg_e:
                log.warning(f"create_new_repo: fallback registration failed: {_reg_e}")
            return {"repo": repo_info, "full_name": full_name, "owner": owner, "name": repo_name, "provider": provider, "hosted": False, "message": f"仓库已创建，但托管失败: {e}"}

        try:
            _fetch_and_cache(user["id"], api_token, provider)
        except Exception:
            pass
        return {"repo": repo_info, "full_name": full_name, "owner": owner, "name": repo_name, "provider": provider, "hosted": True, "message": "仓库创建并托管成功"}

    return {"repo": repo_info, "full_name": full_name, "owner": owner, "name": repo_name, "provider": provider, "hosted": False, "message": "仓库创建成功"}


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


def _host_502_detail(stage: str, err: Exception, message: str = "",
                     host_status: str = "partial", missing: list | None = None,
                     step: str = "") -> dict:
    """构造带上下文的 502 detail（Issue #455：托管失败需可解释、可重试）。

    - 不把上游内部 URL/报文透出给客户端（Issue #383/#395 防泄露约束），原文仅落日志；
    - 401/403/404 为权限/资源类错误，不标记 retryable（盲重试无意义）；
    - 网络类/5xx 标记 retryable，并给出自愈/重试路径提示。
    - Issue #2126：统一携带 host_status / missing / step，前端可直接渲染缺失项。
    """
    code = _upstream_status_code(err)
    retryable = code not in (401, 403, 404)
    return {
        "code": 502,
        "error_code": f"UPSTREAM_{stage.upper()}" + (f"_{code}" if code else ""),
        "message": message or "托管服务暂时不可用，请稍后重试",
        "host_status": host_status,
        "missing": list(missing or []),
        "step": step or stage,
        "upstream": {"stage": stage, "status": code or None, "retryable": retryable},
        "retryable": retryable,
        "retry_hint": "可稍后重试；若 webhook 已创建但托管未完成，可使用「修复托管」自愈（POST /repos/{owner}/{repo}/repair-host）",
    }


def _host_status_marker(repo_key: str, scope: str = "host_repo"):
    """返回托管诊断落库助手（失败仅告警，不阻断主流程；Issue #2126）。"""
    def _mark(status: str, step: str = "", error: str = ""):
        try:
            from src.domains.mgmt.user.db import set_host_status
            set_host_status(repo_key, status, step, error)
        except Exception as e:
            log.warning(f"{scope}: set host_status={status} failed: {e}")
    return _mark


def _gitcode_webhook_502_detail(code: int, owner: str = "", repo_name: str = "") -> dict:
    """GitCode webhook 创建失败的结构化 502（401=token 失效 / 404=仓库不存在）。"""
    if code == 401:
        return {
            "code": 502, "error_code": "WEBHOOK_TOKEN_INVALID",
            "message": "GitCode 登录已失效，请重新绑定/登录 GitCode 后重试",
            "host_status": "webhook_missing", "missing": ["webhook"], "step": "create_webhook",
        }
    _target = f"{owner}/{repo_name}" if owner and repo_name else "该仓库"
    return {
        "code": 502, "error_code": "REPO_NOT_FOUND",
        "message": f"仓库 {_target} 不存在于 GitCode，请确认仓库所属平台与托管账号平台一致",
        "host_status": "webhook_missing", "missing": ["webhook"], "step": "create_webhook",
    }


# Issue #2126: 路径 B 降级（全局 bot 不可用）统一诊断文案
_HOST_PATH_B_FALLBACK_TEXT = "全局 bot 不可用，已切换为用户令牌模式"

# Issue #2126: 托管失败原因统一分类与可读文案（诊断落库/返回前端均使用，不含 token/内部 URL）
_HOST_ERROR_TEXT = {
    "permission_denied": "权限不足：请使用仓库所有者凭据，或授予机器人管理员权限",
    "repo_not_found": "仓库不存在或无权访问",
    "rate_limited": "代码托管平台限流，请稍后重试",
    "upstream_unavailable": "代码托管平台暂时不可用，请稍后重试",
    "token_missing": "用户 Token 缺失或已过期，请重新登录",
    "webhook_url_missing": "webhook_url 未配置，请先配置服务公网地址",
    "webhook_failed": "webhook 创建失败，请检查权限后重试",
    "webhook_lost": "webhook 已被删除或丢失，请使用一键修复重建",
    "bot_failed": "bot 加入项目失败，请检查邀请权限",
    "host_failed": "托管流程未完成，请稍后重试或使用一键修复",
}


def _host_error_text(code: str) -> str:
    return _HOST_ERROR_TEXT.get(code or "", _HOST_ERROR_TEXT["host_failed"])


def _classify_host_error(err: Exception) -> str:
    """把上游异常归类为稳定的失败码（仅用状态码，不解析/透出原始报文）。"""
    code = _upstream_status_code(err) if isinstance(err, Exception) else 0
    if code in (401, 403):
        return "permission_denied"
    if code == 404:
        return "repo_not_found"
    if code == 429:
        return "rate_limited"
    if code >= 500 or code == 0:
        return "upstream_unavailable"
    return "host_failed"


# Issue #3354：刚建仓立刻托管时，VCS 侧可能还没就绪（404 仓库不存在）——
# 这是时序竞态，不是权限问题，等几秒就能成功。只对此类错误做有限退避重试，
# 401/403/429/5xx 立即抛出（重试无意义或应由调用方/上游限流策略处理）。
_HOST_REPO_READY_DELAYS = (2, 4, 8)


def _host_call_with_ready_retry(fn, *args, **kwargs):
    """对托管链路的单次上游调用做「仓库就绪」有限重试（仅 404/repo_not_found）。"""
    _delays = _HOST_REPO_READY_DELAYS
    for _i, _delay in enumerate(_delays + (None,)):
        try:
            return fn(*args, **kwargs)
        except Exception as _e:  # noqa: BLE001
            if _delay is None or _classify_host_error(_e) != "repo_not_found":
                raise
            log.info(f"host_after_create: repo not ready yet ({_e}); "
                     f"retry {_i + 1}/{len(_delays)} in {_delay}s")
            time.sleep(_delay)
    raise RuntimeError("host retry exhausted")  # pragma: no cover — 循环末轮必 raise


def _host_http_error_code(detail) -> str:
    """从 HTTPException.detail 提取稳定失败码（兼容 error_code/detail.code）。"""
    if not isinstance(detail, dict):
        return "host_failed"
    ec = str(detail.get("error_code", "") or "").upper()
    if ec in ("PERMISSION_DENIED", "TOKEN_PERMISSION_INSUFFICIENT"):
        return "permission_denied"
    if ec == "REPO_NOT_FOUND":
        return "repo_not_found"
    if ec == "WEBHOOK_URL_NOT_CONFIGURED":
        return "webhook_url_missing"
    if ec == "TOKEN_NOT_BOUND":
        return "token_missing"
    if detail.get("code") in (401, 403):
        return "permission_denied"
    if detail.get("code") == 404:
        return "repo_not_found"
    return "host_failed"


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
    if not provider:
        # 无前缀线索时先查本账号缓存（唯一命中才用），再谈 token 兜底
        provider = _provider_from_user_repos(user, body.full_name)
    # 剥离 provider 前缀（在册平台前缀均剥），下游统一用纯 owner/repo
    from src.kernel.repo_key import parse_repo_key as _prk
    _raw = body.full_name
    if ":" in _raw:
        _pfx, _powner, _prepo = _prk(_raw)
        if _pfx in _repo_listable_providers() and _powner and _prepo:
            _raw = f"{_powner}/{_prepo}"
            body.full_name = _raw
    parts = _raw.split("/", 1)
    if len(parts) != 2:
        raise HTTPException(status_code=400, detail={"code": 400, "message": "无效的仓库标识"})
    owner, repo_name = parts

    # D44: 显式 provider > 仓库来源提示；仍无 → 按平台 token 可用性自动选择（github 优先，不以登录平台短路）
    _listable = _repo_listable_providers()
    if provider and provider in _listable:
        # B 批：token 解析零平台分支（含 github/gitee/gitlab 在册平台）
        api_token = get_user_token_for(user["username"], user_id=str(user.get("id") or ""), provider=provider)
    else:
        provider, api_token = _provider_from_token(user)

    # 托管全流程未接入的平台：显式拒绝（不兜底成 github/gitcode，避免 webhook/前缀错配）
    if provider and provider not in _hosting_implemented_providers():
        raise HTTPException(status_code=422, detail={
            "code": 422, "error_code": "HOSTING_NOT_IMPLEMENTED",
            "message": f"平台 {provider} 的托管全流程（bot 合作者/webhook）尚未接入；"
                       f"如 DB 已有托管行可用 /api/repos/{owner}/{repo_name}/repair-host 修复"})
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
        try:
            from src.domains.mgmt.user.db import set_host_status as _shs
            _shs(to_repo_key(body.full_name, provider=provider), "webhook_missing",
                 "configure_webhook_url", _host_error_text("webhook_url_missing"))
        except Exception:
            pass
        raise HTTPException(status_code=400, detail={
            "code": 400,
            "error_code": "WEBHOOK_URL_NOT_CONFIGURED",
            "message": "webhook_url not configured",
            "host_status": "webhook_missing",
            "missing": ["webhook_url"],
            "step": "configure_webhook_url",
            "guidance": {
                "tried": _tried,
                "action": "Access this Web UI via public IP (auto-detect will trigger on login), or set webhook_url in Settings",
                "example": "http://<public-ip>/webhook/gitcode"
            }
        })
    # B 批（2026-11-13）：回调路径按平台卡派生（D12/W3 同一来源链），
    # 不再做 gitcode→github 字符串替换——新平台取卡 webhook_path 自动正确。
    from src.domains.mgmt.user.repo_manager import _webhook_path_from_card as _wh_path
    _wh_suffix = _wh_path(provider, f"/webhook/{provider}")
    if _wh_suffix and "/webhook/" in webhook_url:
        webhook_url = webhook_url.split("/webhook/", 1)[0] + _wh_suffix

    # B 批（2026-11-13）：托管配置/方言一次解析，下面两段按**方言**而非平台名分派
    from src.domains.mgmt.user.repo_manager import get_hosting_provider_config as _get_hcfg
    from src.domains.mgmt.user.repo_manager import _dialect_of as _dialect_of_provider
    _hcfg = _get_hcfg(provider)
    _dialect = _dialect_of_provider(provider)

    if not api_token:
        # B 批：文案按平台卡名派生（零平台分支，原 GitHub/GitCode 两套文案合并）
        from src.domains.runtime.public import get_platform_card as _gpc_tok
        _pc_tok = _gpc_tok(provider)
        _pname_tok = ((_pc_tok.name if _pc_tok is not None else "") or provider)
        raise HTTPException(status_code=400, detail={
            "code": 400, "error_code": "TOKEN_NOT_BOUND",
            "message": f"{_pname_tok} API Token 未配置或已过期，请重新绑定 {_pname_tok} 账号或配置 PAT"})

    def _finalize_host_write(_repo_key: str, _payload: dict) -> dict:
        """托管写成功的唯一出口：标记加入 + 建默认会话 + 失效 user_repos 缓存。

        invalidate 必须在 mark_user_repo_join 之后执行——后者可能为仓库写入新的
        synced_at（MAX 取最新会掩盖其余 stale 行），统一在此兜底置空。
        """
        from src.api.session_store import ensure_default_agent_sessions
        user_db.mark_user_repo_join(user["id"], _repo_key, is_join=True, is_admin=True)
        ensure_default_agent_sessions(_repo_key)
        user_db.invalidate_user_repos(user["id"])
        # 托管进团队组织：后台确保每日分析任务并为该仓触发一次 AI 分析
        if _org_id:
            try:
                from src.domains.mgmt.user.org_db import get_org as _get_org

                _org = _get_org(_org_id) or {}
                if _org and not _org.get("is_personal"):
                    import threading as _th

                    def _analysis_run():
                        try:
                            from src.domains.mgmt import org_analysis

                            org_analysis.ensure_analysis_automation(_org_id, actor_id=user["id"])
                            org_analysis.request_repo_analysis(
                                _org_id, _repo_key, trigger="link", actor_id=user["id"])
                        except Exception as _ae:  # noqa: BLE001
                            log.warning(f"host repo analysis trigger failed org={_org_id} repo={_repo_key}: {_ae}")

                    _th.Thread(target=_analysis_run, name=f"org-analysis-{_org_id}-{_repo_key}",
                               daemon=True).start()
            except Exception as _hook_exc:  # noqa: BLE001
                log.debug(f"host repo org-analysis hook skipped: {_hook_exc}")
        return _payload

    # ===== GitHub 仓库托管 =====
    if _dialect == "github_web":
        repo_key = to_repo_key(body.full_name, provider=provider)

        # Issue #2126: 托管诊断落库助手
        _gh_mark = _host_status_marker(repo_key, f"host_repo {provider}")

        # bot 全局配置（B 批：卡驱动来源链 bot_token/bot_username，零平台分支）
        gh_bot_token = _hcfg["bot_token"]
        gh_bot_username = _hcfg["bot_username"]

        # Issue #372: GitHub 路径 B——无全局 GitHub bot token 时跳过 bot 协作者
        _gh_use_path_b = not gh_bot_token
        import secrets as _secrets
        _gh_webhook_secret = _secrets.token_urlsafe(32) if _gh_use_path_b else ""

        try:
            # Issue #455: 托管前归属/权限前置校验（GitHub）——非所有者/无管理员权限
            # 直接 403 明确提示，避免上游 403 被包装成无上下文 502（原仅 WARNING）。
            # 经 with_oauth_token 走 token 刷新机制（与下游一致）；无法确认权限（None/网络类）
            # 不阻断，交给下游既有流程处理。
            from src.domains.mgmt.user.repo_manager import _github_req, _GITHUB_API_BASE
            import urllib.parse as _gh_up

            def _gh_precheck(token, _owner=owner, _repo=repo_name):
                _info = _github_req(
                    "GET", f"{_GITHUB_API_BASE}/repos/{_owner}/{_gh_up.quote(_repo, safe='')}", token)
                try:
                    _uinfo = _github_req("GET", f"{_GITHUB_API_BASE}/user", token)
                except Exception:
                    _uinfo = {}
                _login = (_uinfo or {}).get("login", "") if isinstance(_uinfo, dict) else ""
                _perms = (_info.get("permissions") or {}) if isinstance(_info, dict) else {}
                return {"info": _info, "login": _login, "perms": _perms}

            try:
                _gh_pre = with_oauth_token(user, provider, _gh_precheck)
            except RuntimeError as _gh_pre_err:
                _gh_pre = None
                _gh_pre_code = _upstream_status_code(_gh_pre_err)
                log.warning(f"host_repo GitHub: permission pre-check upstream failed: {_gh_pre_err}")
                if _gh_pre_code == 404:
                    raise HTTPException(status_code=404, detail={
                        "code": 404, "error_code": "REPO_NOT_FOUND",
                        "message": f"仓库 {owner}/{repo_name} 不存在于 GitHub 或无权访问，请确认仓库归属与授权",
                    })
                if _gh_pre_code in (401, 403):
                    raise HTTPException(status_code=403, detail={
                        "code": 403, "error_code": "PERMISSION_DENIED",
                        "message": "无法访问该 GitHub 仓库（权限不足）：请使用该仓库所有者的凭据，或授予机器人管理员权限后再试",
                    })
                # 网络类：无法确认权限 → 不阻断，继续既有流程
                log.warning(f"host_repo GitHub: permission pre-check skipped (network/unknown: {_gh_pre_err})")
            if isinstance(_gh_pre, dict) and _gh_pre.get("info"):
                _gh_info = _gh_pre["info"]
                _gh_login = _gh_pre.get("login", "")
                _gh_perms = _gh_pre.get("perms") or {}
                _gh_is_owner = bool(((_gh_info.get("owner") or {}).get("login") or "") == _gh_login)
                if not _gh_is_owner and not _gh_perms.get("admin"):
                    log.warning(f"host_repo GitHub: permission pre-check failed (owner={_gh_is_owner}, admin={_gh_perms.get('admin', False)}) for {owner}/{repo_name}")
                    raise HTTPException(status_code=403, detail={
                        "code": 403,
                        "error_code": "PERMISSION_DENIED",
                        "message": "无权限托管该 GitHub 仓库：请使用该仓库所有者的凭据，或授予机器人管理员权限后再试",
                    })

            _find_hook = _hcfg["find_webhook"]
            existing_id = with_oauth_token(
                user, provider,
                lambda t: _find_hook(t, owner, repo_name, webhook_url))
            if existing_id:
                # Issue #372 R6.2: 路径 B 复用已有 webhook 时密钥不匹配 → 删旧重建
                if _gh_use_path_b:
                    try:
                        _hcfg["remove_webhook"](api_token, owner, repo_name, existing_id)
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
                        _gh_mark("partial", "path_b_fallback", _HOST_PATH_B_FALLBACK_TEXT)
                        # R25.2: A→B 降级时删除旧 webhook（路径 A 创建时无 secret），
                        # 强制用新 secret 重建。删失败时保留旧 existing_id + 旧 secret
                        # 不新建（避免双 webhook 收重复事件）。
                        if existing_id:
                            try:
                                _hcfg["remove_webhook"](api_token, owner, repo_name, existing_id)
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
                                             **{_hcfg["db_token_field"]: _gh_db_wh_token,
                                                _hcfg["db_bot_token_field"]: ("" if _gh_use_path_b else gh_bot_token)},
                                             bot_username=(gh_bot_username if not _gh_use_path_b else ""), org_id=_org_id,
                                             host_user_id=(user["id"] if _gh_use_path_b else 0), webhook_secret=_gh_webhook_secret)
                    _gh_mark("")
                    return _finalize_host_write(
                        repo_key, {"message": "仓库已托管（复用已有 webhook）", "webhook_id": existing_id, **_bot_result})

            # Issue #372 §16.3a: 先检查 bot 协作者状态再建 webhook + 写 DB
            _bot_result = {"bot_collaborator_needed": False}
            if not _gh_use_path_b:
                _bot_result = _try_github_bot_collaborator(api_token, gh_bot_token, gh_bot_username, owner, repo_name)
                # R12.3: invite_pending/add_failed 也降级到路径 B
                if _bot_result.get("bot_status") in ("error", "invite_pending", "add_failed"):
                    log.warning(f"host_repo GitHub: bot status={_bot_result.get('bot_status')}, falling back to path B")
                    _gh_use_path_b = True
                    _gh_webhook_secret = _secrets.token_urlsafe(32)
                    _gh_mark("partial", "path_b_fallback", _HOST_PATH_B_FALLBACK_TEXT)
            hook_id = _hcfg["setup_webhook"](
                api_token, owner, repo_name, webhook_url,
                **_hcfg["setup_webhook_kwargs"](_gh_webhook_secret, api_token, _gh_use_path_b))
            from src.api.onboarding import cleanup_stale_repo_data
            cleanup_stale_repo_data(repo_key)
            # P1-2: 路径 B 成功时写空串（不写 stale 用户 token），webhook 验证依赖 webhook_secret
            _gh_db_wh_token = "" if _gh_use_path_b else api_token
            user_db.add_managed_repo(repo_key, owner, repo_name, webhook_id=hook_id,
                                     **{_hcfg["db_token_field"]: _gh_db_wh_token,
                                        _hcfg["db_bot_token_field"]: ("" if _gh_use_path_b else gh_bot_token)},
                                     bot_username=(gh_bot_username if not _gh_use_path_b else ""), org_id=_org_id,
                                     host_user_id=(user["id"] if _gh_use_path_b else 0), webhook_secret=_gh_webhook_secret)
            _gh_mark("")
            return _finalize_host_write(
                repo_key, {"message": "仓库托管成功", "webhook_id": hook_id, **_bot_result})
        except HTTPException as _he:
            _gh_mark("partial", "precheck_permission", _host_error_text(_host_http_error_code(getattr(_he, "detail", None))))
            raise
        except Exception as e:
            # Issue #455: 502 带上下文（stage/上游状态/retryable），原文仅落日志防泄露
            _gh_mark("partial", "github_host", _host_error_text(_classify_host_error(e)))
            log.warning(f"host_repo GitHub failed: {e}")
            raise HTTPException(status_code=502, detail=_host_502_detail("github_host", e))

    # ===== GitCode 仓库托管 =====
    # Issue #345: 改用统一入口读取全局 bot 配置
    # bot 全局配置（B 批：卡驱动来源链 bot_token/bot_username，零平台分支）
    global_bot_token = _hcfg["bot_token"]
    global_bot_username = _hcfg["bot_username"]

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

    from src.domains.mgmt.user.repo_manager import _check_collaborator_admin, _req
    # B 批（2026-11-13）：v5 API 基址从平台卡派生（gitee/gitcode 同方言不同域）；
    # 原硬编码 https://gitcode.com/api/v5 会让 gitee 仓的预检打错平台。
    from src.domains.runtime.public import get_platform_card as _gpc_host
    _pc_host = _gpc_host(provider)
    _v5_base = ((_pc_host.api_base if _pc_host is not None else "")
                or "https://gitcode.com/api/v5").rstrip("/")
    try:
        def _get_user(token):
            return _req("GET", f"{_v5_base}/user", token)
        user_data = with_oauth_token(user, provider, _get_user)
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
                _perm_url = f"{_v5_base}/repos/{_owner}/{_up.quote(_repo, safe='')}/collaborators/{_up.quote(_cu, safe='')}/permission"
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
            is_admin = with_oauth_token(user, provider, _check_admin)

        if not is_admin:
            raise HTTPException(status_code=403, detail={"code": 403, "error_code": "PERMISSION_DENIED", "message": "无权限托管该仓库：请使用该仓库所有者的凭据，或授予机器人管理员权限后再试"})
    except HTTPException:
        raise
    except RuntimeError as e:
        # Issue #383/#395: 上游 RuntimeError 分流——含内部 API URL 的原文不得透出给用户。
        # _req 报文格式固定：GitCode API {method} {url} failed ({code}): {text}
        # FR-5.2: 增强错误码——401/403 返回 TOKEN_PERMISSION_INSUFFICIENT 供前端映射
        import re as _re
        _m = _re.search(r'failed \((\d{3})\)', str(e))
        _code = int(_m.group(1)) if _m else 0
        log.warning(f"host_repo upstream failed: {e}")
        if _code == 404:
            # 仓库不存在/无权访问（业务语义 404，而非网关故障 502）
            raise HTTPException(status_code=404, detail={"code": 404, "message": "仓库不存在或无权访问"})
        if _code in (401, 403):
            # token 失效/无权（with_oauth_token 已做刷新重试，仍失败说明凭据或权限不足）
            raise HTTPException(status_code=403, detail={"code": 403, "error_code": "TOKEN_PERMISSION_INSUFFICIENT",
                "message": "Token 权限不足或已过期，请更新后重试或改用 OAuth"})
        raise HTTPException(status_code=502, detail=_host_502_detail("gitcode_user_permission", e))
    except Exception as e:
        # Issue #383/#395: 兜底不拼接上游原文（防泄露内部 URL/报文），原文仅落日志
        log.warning(f"host_repo upstream failed: {e}")
        raise HTTPException(status_code=502, detail=_host_502_detail("gitcode_user", e))

    try:
        from src.domains.mgmt.user.repo_manager import add_collaborator, accept_invite_for_repo, get_member_permission, _RateLimitedError
        repo_key = to_repo_key(body.full_name, provider=provider)
        # 通用段（gitee_v5）：前缀按本次判定的 provider 派生（实弹 2026-11-13：
        # 裸 to_repo_key 恒默认 gitcode，gitee 仓 webhook 建对了但库内键写错前缀）

        # Issue #2126: 托管诊断落库助手
        _mark = _host_status_marker(repo_key)

        # 1) 邀请 bot 为维护者（owner token 必需）→ 2) bot 接受 → 3) bot 自查权限
        # 原则：凡能查/能建一律 bot token；owner token 仅身份确认 + 本步邀请
        # Issue #372: 路径 B（无全局 bot token）跳过 bot 协作者流程
        # Issue #372 §16.1: 路径 A bot 协作者失败时降级到路径 B（不 raise 502）
        import secrets as _secrets
        if not _use_path_b and api_token and global_bot_username and global_bot_token:
            _mark("partial", "invite_bot", "")
            ok, add_err = add_collaborator(api_token, owner, repo_name, global_bot_username, "maintainer")
            if not ok:
                # 兜底：add_collaborator 失败可能是已成员/已邀请，用 bot token 自查确认
                try:
                    bot_perm = get_member_permission(global_bot_token, owner, repo_name, global_bot_username)
                    access_level = (bot_perm or {}).get("access_level", 0) or 0
                except _RateLimitedError as _rl:
                    log.warning(f"host: perm check rate-limited (non-fatal), mark bot_missing: {_rl}")
                    _mark("bot_missing", "check_bot", _host_error_text("rate_limited"))
                    raise HTTPException(status_code=503, detail={"code": 503, "message": "托管暂时失败：GitCode API 调用过于频繁，请稍后重试"})
                except Exception:
                    # §16.1: 非限流异常（401/404）→ 降级到路径 B
                    log.warning(f"host: path A get_member_permission failed, falling back to path B")
                    _use_path_b = True
                    webhook_token = api_token
                    _webhook_secret = _secrets.token_urlsafe(32)
                    _mark("partial", "path_b_fallback", _HOST_PATH_B_FALLBACK_TEXT)
                else:
                    if access_level >= 40:
                        log.info(f"host: add_collaborator failed but bot {global_bot_username} already maintainer (access_level={access_level}) in {owner}/{repo_name}, continue")
                    else:
                        # §16.1: bot 权限不足 → 降级到路径 B（原 raise 502）
                        log.warning(f"host: bot access_level={access_level} < 40, falling back to path B")
                        _use_path_b = True
                        webhook_token = api_token
                        _webhook_secret = _secrets.token_urlsafe(32)
                        _mark("partial", "path_b_fallback", _HOST_PATH_B_FALLBACK_TEXT)

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
                        _mark("partial", "path_b_fallback", _HOST_PATH_B_FALLBACK_TEXT)
                except _RateLimitedError as _rl:
                    # 限流：权限未判定，继续 webhook（不阻塞），host_status 待 dashboard 复查
                    log.warning(f"host: perm check rate-limited (continue): {_rl}")
                except Exception:
                    # §16.1: 非限流异常 → 降级到路径 B
                    log.warning(f"host: path A perm check failed, falling back to path B")
                    _use_path_b = True
                    webhook_token = api_token
                    _webhook_secret = _secrets.token_urlsafe(32)
                    _mark("partial", "path_b_fallback", _HOST_PATH_B_FALLBACK_TEXT)

        # 4) webhook：路径 A 用 bot token 查/建；路径 B 用用户 token + 随机密钥
        existing_id = _hcfg["find_webhook"](webhook_token, owner, repo_name, webhook_url)
        if existing_id:
            # Issue #372 R6.2: 路径 B 复用已有 webhook 时密钥不匹配 → 删旧重建
            if _use_path_b:
                try:
                    _hcfg["remove_webhook"](webhook_token, owner, repo_name, existing_id)
                    existing_id = None
                except Exception as _re:
                    log.warning(f"host_repo GitCode: remove old webhook failed (will reuse): {_re}")
                    _webhook_secret = ""  # P1-1: 删失败时不写新密钥
                    webhook_token = ""  # §31: 也不写用户 token（刷新后 stale），让验证 loud fail
        if existing_id:
            # P1-2: 路径 B 成功时写空串（不写 stale 用户 token），webhook 验证依赖 webhook_secret
            _db_webhook_token = "" if _use_path_b else webhook_token
            user_db.add_managed_repo(repo_key, owner, repo_name, webhook_id=existing_id,
                                     **{_hcfg["db_token_field"]: _db_webhook_token,
                                        _hcfg["db_bot_token_field"]: (global_bot_token if not _use_path_b else "")},
                                     bot_username=(global_bot_username if not _use_path_b else ""), org_id=_org_id,
                                     host_user_id=(user["id"] if _use_path_b else 0), webhook_secret=_webhook_secret)
            _mark("")
            return _finalize_host_write(
                repo_key, {"message": "仓库已托管（复用已有 webhook）", "webhook_id": existing_id})

        # 路径 B: webhook_password 用随机密钥；路径 A: 用 bot token
        try:
            hook_id = _hcfg["setup_webhook"](
                webhook_token, owner, repo_name, webhook_url,
                **_hcfg["setup_webhook_kwargs"](_webhook_secret, webhook_token, _use_path_b))
        except RuntimeError as _wh_err:
            # 区分 token 失效(401) / 仓库不存在于本平台(404)，给出可操作文案（host-repo-provider-routing-fix-design.md）
            import re as _re
            _msg = str(_wh_err)
            _codes = _re.findall(r"\((\d{3})\)", _msg)
            _code = int(_codes[0]) if _codes else 0
            _mark("webhook_missing", "create_webhook",
                  _host_error_text("permission_denied" if _code == 401 else "repo_not_found" if _code == 404 else "webhook_failed"))
            if _code in (401, 404):
                raise HTTPException(status_code=502, detail=_gitcode_webhook_502_detail(_code, owner, repo_name))
            raise
        # 清理同名旧仓库残留的 DB 数据（同上）
        from src.api.onboarding import cleanup_stale_repo_data
        cleanup_stale_repo_data(repo_key)
        # P1-2: 路径 B 成功时写空串（不写 stale 用户 token），webhook 验证依赖 webhook_secret
        _db_webhook_token = "" if _use_path_b else webhook_token
        user_db.add_managed_repo(repo_key, owner, repo_name, webhook_id=hook_id,
                                 **{_hcfg["db_token_field"]: _db_webhook_token,
                                    _hcfg["db_bot_token_field"]: (global_bot_token if not _use_path_b else "")},
                                 bot_username=(global_bot_username if not _use_path_b else ""), org_id=_org_id,
                                 host_user_id=(user["id"] if _use_path_b else 0), webhook_secret=_webhook_secret)
        _mark("")
        return _finalize_host_write(
            repo_key, {"message": "仓库托管成功", "webhook_id": hook_id})
    except HTTPException:
        raise
    except Exception as e:
        # 兜底：失败但 webhook 可能已建，标记状态避免死循环，仍可进入项目
        try:
            _mark("partial", "host_setup", _host_error_text(_classify_host_error(e)))
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
    from src.domains.mgmt.user.repo_manager import get_hosting_provider_config, _RateLimitedError
    from src.domains.mgmt.user.db import get_webhook_url, set_host_status
    from src.api.deps import get_user_token_with_refresh, get_user_github_token_with_refresh

    # B 批实弹修复（2026-11-13）：provider 必须来自已托管行的真实前缀。
    # 裸 to_repo_key 默认 gitcode → github/gitee 仓的 repair 会打错平台 API
    # （日志实锤：repair github 仓却 POST gitcode.com/.../hooks → 404 Project not found）。
    # 复用 resolve_managed_repo_key 的候选集探针（读 managed_repos 取真实前缀）。
    from src.domains.mgmt.user.db import resolve_managed_repo_key
    repo_key = resolve_managed_repo_key(owner, repo) or to_repo_key(f"{owner}/{repo}")
    from src.api.deps import check_repo_access
    from src.domains.mgmt.user.db import managed_repo_exists

    # Issue #2126: 修复过程诊断落库助手
    _mark = _host_status_marker(repo_key, "repair_host")

    # owner token 按 provider 分流
    _provider, _, _ = parse_repo_key(repo_key)
    # B 批：token 解析零平台分支（repo_key 前缀派生 provider）
    api_token = get_user_token_for(user["username"], user_id=str(user.get("id") or ""), provider=_provider)
    if not api_token:
        _mark("partial", "user_token", _host_error_text("token_missing"))
        raise HTTPException(status_code=400, detail={
            "code": 400, "error_code": "TOKEN_NOT_BOUND",
            "message": "缺少用户 API Token，请重新登录",
            "missing": ["token"], "step": "user_token"})

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
            from src.domains.runtime.public import VcsApiClient
            _info = VcsApiClient(_provider, api_token, owner, repo).get_repo_info() or {}
            _self_ok = not str(_info.get("error", "") or "")
        except Exception as _ve:
            log.warning(f"repair_host: unregistered repo VCS self-check failed: {_ve}")
        if not _self_ok:
            raise
        log.info(f"repair_host: unregistered repo {repo_key} passed VCS self-check, allow repair")

    _cfg = get_hosting_provider_config(_provider)
    bot_token = _cfg["bot_token"]
    bot_username = _cfg["bot_username"]

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
        _mark("webhook_missing", "configure_webhook_url", _host_error_text("webhook_url_missing"))
        raise HTTPException(status_code=400, detail={
            "code": 400,
            "error_code": "WEBHOOK_URL_NOT_CONFIGURED",
            "message": "webhook_url not configured",
            "host_status": "webhook_missing",
            "missing": ["webhook_url"],
            "step": "configure_webhook_url",
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
        # B 批（2026-11-13）：bot 权限判定走方言分派，零平台分支
        from src.domains.mgmt.user.repo_manager import bot_is_maintainer, invite_bot_collaborator
        try:
            bot_ok = bot_is_maintainer(_provider, bot_token, owner, repo, bot_username)
        except _RateLimitedError:
            _mark("bot_missing", "check_bot", _host_error_text("rate_limited"))
            raise HTTPException(status_code=503, detail={
                "code": 503, "error_code": "RATE_LIMITED",
                "message": "GitCode API 调用过于频繁，请稍后重试",
                "host_status": "bot_missing", "missing": ["bot"], "step": "check_bot"})
        except Exception:
            bot_ok = False

        # P1-5: bot 权限不足时邀请+重试（B 批：方言分派 invite_bot_collaborator）
        if not bot_ok:
            try:
                invite_bot_collaborator(_provider, api_token, bot_token, owner, repo, bot_username)
            except Exception as _ie:
                # Issue #372 §16.2: 包 try/except，失败时降级到路径 B
                log.warning(f"repair_host {_provider}: bot invite failed: {_ie}")
                bot_ok = False
            else:
                try:
                    bot_ok = bot_is_maintainer(_provider, bot_token, owner, repo, bot_username)
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
                _mark("partial", "path_b_fallback", _HOST_PATH_B_FALLBACK_TEXT)
                # 继续走 webhook 流程，不 return error

    # 2) webhook 是否存在，缺失则补建
    # 路径 A: bot token 查/建；路径 B: 用户 token 查/建 + 随机密钥
    _wh_token = api_token if _use_path_b else bot_token
    # B 批（2026-11-13）：建 hook 的鉴权参数由 setup_webhook_kwargs 按方言派生；
    # 这里只维护 secret 值（路径 B=随机/DB 密钥，路径 A=gitee 系 bot token 由 kw 取）
    _wh_password = _webhook_secret if _use_path_b else bot_token

    _find_fn = _cfg["find_webhook"]
    _setup_fn = _cfg["setup_webhook"]
    _remove_fn = _cfg["remove_webhook"]

    hid = None
    try:
        hid = _find_fn(_wh_token, owner, repo, webhook_url)
    except Exception:
        hid = None
    if hid:
        # Issue #2126: 幂等修复——webhook 已存在时优先复用，仅检测到密钥不一致才删旧重建。
        from src.domains.mgmt.user.db import get_webhook_secret
        _stored_secret = get_webhook_secret(repo_key)
        if _use_path_b:
            # 路径 B：DB 已存密钥即可复用；密钥缺失无法验证一致性 → 重建
            _force_rebuild = not bool(_stored_secret)
            if not _force_rebuild:
                _webhook_secret = _stored_secret
                _wh_password = _stored_secret
                log.info(f"repair_host: reuse existing webhook {hid} with stored secret for {owner}/{repo}")
        else:
            # R12.7: 路径 A 检查是否刚从 B 升级（webhook_secret 残留非空）
            # 升级后 GitCode 上 webhook 密码仍是旧 secret，DB 已清空 → 不匹配 → 强制重建
            _stale_secret = _stored_secret
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
        try:
            hid = _setup_fn(_wh_token, owner, repo, webhook_url,
                            **_cfg["setup_webhook_kwargs"](_wh_password, bot_token, _use_path_b))
        except Exception as _we:
            log.warning(f"repair_host: create webhook failed: {_we}")
            _mark("webhook_missing", "create_webhook",
                  _host_error_text(_classify_host_error(_we)))
            return {"status": "error", "host_status": "webhook_missing",
                    "missing": ["webhook"], "step": "create_webhook",
                    "message": _host_error_text(_classify_host_error(_we))}
    if not hid:
        _mark("webhook_missing", "create_webhook", _host_error_text("webhook_failed"))
        return {"status": "error", "host_status": "webhook_missing",
                "missing": ["webhook"], "step": "create_webhook",
                "message": _host_error_text("webhook_failed")}

    # 3) 完成 → 回写 webhook_id → 清状态
    try:
        from src.domains.mgmt.user.db import add_managed_repo
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
    return {"status": "ok", "host_status": "", "missing": [], "step": "done", "webhook_id": hid,
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
        # P9：卡派生两路径判定（gitee 无列平台查 rc 单表；gitcode/github 查列），
        # 与 /repos/bot-token 单仓接口同一口径——修复 gitee 仓凭据有值恒 false。
        "has_bot_token": user_db.repo_has_bot_credential(r["repo_key"]),
        "host_status": r.get("host_status", "") or "",
        # Issue #2264：个人空间下拉也要能标出仓库所属组织
        "org_id": r.get("org_id", "") or "",
        "org_name": r.get("org_name", "") or "",
        # 组织自动仓库标识：前端仓库下拉需要把它单独置顶显示为「组织仓库」
        "is_org_repo": bool(r.get("is_org_repo")),
    } for r in repos]}


class BotTokenBody(BaseModel):
    full_name: str
    bot_token: str = ""


@router.get("/bot-token/{owner}/{repo_name}", operation_id="repos_bot_token_get", summary="Get repository bot token", description="Returns whether the repository has a bound DevOps bot token.")
def get_repo_bot_token(owner: str, repo_name: str, user: dict = Depends(require_no_session_token)):
    """获取仓库绑定的 DevOps Bot Token 是否存在（P9：卡派生两路径，与 /repos/managed 同一判定口径）"""
    repo_key = resolve_managed_repo_key(owner, repo_name)
    return {"has_bot_token": repo_has_bot_credential(repo_key)}


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
    owner = _strip_provider_prefix(owner)
    # B 批实弹修复（2026-11-13）：裸 to_repo_key 默认 gitcode 前缀 → github/gitee 仓查错行。
    from src.domains.mgmt.user.db import resolve_managed_repo_key
    repo_key = resolve_managed_repo_key(owner, repo_name)
    check_repo_access(user, repo_key)

    # R21.8.2: 只有托管人或 admin 可配置仓库级 bot token（防水平越权）
    # check_repo_access 已检查托管人权限，这里补充 admin 检查
    _is_admin = (user.get("role") == "admin" or user.get("auth_type") in ("session_token", "api_key"))
    if not _is_admin:
        # 非 admin 用户只能配置自己托管的仓库（check_repo_access 已验证）
        # 额外检查：如果是路径 B 仓库，必须是 host_user 本人
        from src.domains.mgmt.user.db import get_host_user_id
        # D105 后 host_user_id 是 UUID TEXT 域，get_host_user_id 返回 str ——
        # 原写法 `_host_uid > 0` 对 str 是 TypeError（实弹 500：
        # "'>' not supported between instances of 'str' and 'int'"）。
        # 按该函数文档口径用字符串判定：''/'0' 视为未配置。
        _host_uid = str(get_host_user_id(repo_key) or "")
        if _host_uid not in ("", "0") and _host_uid != str(user.get("id") or ""):
            raise HTTPException(status_code=403, detail={
                "code": 403, "error_code": "NOT_REPO_OWNER",
                "message": "只有仓库托管人或管理员可配置 Bot Token"
            })

    # 确定 provider（B 批：bot token 列名走平台卡派生，零平台分支）
    _provider_str, _, _ = parse_repo_key(repo_key)
    from src.domains.runtime.public import get_platform_card as _get_pc
    _pc = _get_pc(_provider_str)
    _bot_col = getattr(_pc, "token_storage_field", "") if _pc is not None else ""
    _bot_col = _bot_col or f"bot_{_provider_str}_token"
    if not __import__("re").match(r"^[A-Za-z0-9_]+$", _bot_col):
        raise HTTPException(status_code=500, detail={"code": 500, "message": "bot token 列名非法（平台卡配置）"})

    # R15.5: 传空串时清除仓库级 bot token
    if not body.bot_token:
        try:
            with db_lock:
                c = db.cursor()
                c.execute(
                    f"UPDATE managed_repos SET {_bot_col}='', bot_token_status='' WHERE repo_key=?",
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
        _remove_old_bot_collaborator(repo_key, owner, repo_name, _provider_str, user)
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
        if _host_uid > 0:
            # 路径 B: 用 host 用户 token 邀请
            try:
                from src.domains.mgmt.auth.grape_account_db import get_grape_account_by_id
                _host_acc = get_grape_account_by_id(_host_uid)
                if _host_acc and _host_acc.get("username"):
                    _invite_token = get_user_token_for(
                        _host_acc["username"], user_id=str(_host_uid or ""), provider=_provider_str) or ""
            except Exception:
                pass
            if not _invite_token:
                raise HTTPException(status_code=400, detail={
                    "code": 400, "error_code": "HOST_TOKEN_EXPIRED",
                    "message": "托管用户 Token 已过期，请重新绑定后再配置 Bot Token"
                })
        else:
            # 路径 A: 用当前用户 token 邀请
            _invite_token = get_user_token_for(
                user["username"], user_id=str(user.get("id") or ""), provider=_provider_str) or ""

        if _invite_token:
            try:
                from src.domains.mgmt.user.repo_manager import invite_permission_for as _inv_perm_for
                _permission = _inv_perm_for(_provider_str)
                _vcs_provider.add_collaborator(_invite_token, owner, repo_name, repo_bot_username, _permission)
            except Exception as _ae:
                # R15.2.3: 邀请失败记日志继续（邀请幂等，bot 可能已是协作者）
                log.warning(f"set_repo_bot_token: add_collaborator failed (continuing): {_ae}")

    # R15.6: best-effort 移除旧 bot 协作者身份（如果有旧 bot 且与新 bot 不同）
    if _old_bot_username and _old_bot_username != repo_bot_username:
        _remove_old_bot_collaborator(repo_key, owner, repo_name, _provider_str, user, _old_bot_username)

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
                                  provider: str, user: dict, old_bot_username: str = ""):
    """R15.6: best-effort 移除旧 bot 协作者身份。

    只移除该仓库的旧 bot 协作者（R15.6.1），不移除其他仓库的旧 bot（R15.6.2）。
    移除失败不影响新 token 写入（R15.6.3 best-effort）。
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
        _remove_token = get_user_token_for(
            user["username"], user_id=str(user.get("id") or ""), provider=provider) or ""
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
    # B 批实弹修复：前缀按 managed_repos 真实行解析（裸 to_repo_key 恒默认 gitcode）
    from src.domains.mgmt.user.db import resolve_managed_repo_key
    _dn_parts = body.full_name.split("/", 1)
    repo_key = resolve_managed_repo_key(_strip_provider_prefix(_dn_parts[0]), _dn_parts[-1])
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
    from src.kernel.session_manager import db, db_lock
    # QA N1（2026-11-13）：与 set_deploy_url 同口径——裸 to_repo_key 恒默认 gitcode，
    # github/gitee 仓 PUT 成功后再 GET 会读回空串。按 managed_repos 真实前缀解析。
    from src.domains.mgmt.user.db import resolve_managed_repo_key
    repo_key = resolve_managed_repo_key(_strip_provider_prefix(owner), repo_name)
    with db_lock:
        c = db.cursor()
        c.execute("SELECT deploy_url FROM managed_repos WHERE repo_key=?", (repo_key,))
        row = c.fetchone()
    return {"deploy_url": (row[0] if row else "") or ""}


def _ensure_vcs_maintainer(user: dict, provider: str, owner: str, repo_name: str,
                           scope: str, rate_msg: str) -> None:
    """VCS 侧 maintainer 及以上校验（B 批：卡驱动 login + 方言分派权限归一）。

    规则与托管侧同口径：GitHub role in (admin, maintain)；Gitee 系
    access_level>=40 或 permission=="admin"；仓库所有者天然通过。
    失败不兜底：token 缺失 / API 不可达 / 无法判定 → 403 拒绝（绝不放行）。
    """
    from src.domains.mgmt.user.repo_manager import (
        vcs_user_login, vcs_user_permission, _RateLimitedError)
    from src.domains.runtime.public import get_platform_card

    _pc = get_platform_card(provider)
    _pname = ((_pc.name if _pc is not None else "") or provider)
    try:
        vcs_login = with_oauth_token(user, provider, lambda t: vcs_user_login(provider, t))
    except HTTPException:
        raise
    if not vcs_login:
        raise HTTPException(status_code=502, detail={
            "code": 502, "message": f"无法获取当前用户 {_pname} 身份"})
    if vcs_login == owner:
        return
    try:
        perm = with_oauth_token(
            user, provider,
            lambda t: vcs_user_permission(provider, t, owner, repo_name, vcs_login))
    except _RateLimitedError:
        raise HTTPException(status_code=429, detail={"code": 429, "message": rate_msg})
    except HTTPException:
        raise
    if not perm or not perm.get("is_maintainer"):
        _role = str((perm or {}).get("role") or "未知")
        raise HTTPException(status_code=403, detail={
            "code": 403,
            "message": f"{scope}需要 {_pname} maintainer 及以上权限（当前角色：{_role}）"})


def _check_unmanage_vcs_permission(user: dict, owner: str, repo_name: str, repo_provider: str) -> None:
    """解除托管前的 VCS 侧 maintainer 复核（B 批：方言分派，零平台分支）。

    读权限（能看到）≠ 写权限（能解除），本闸门在 VCS 侧独立复核；失败不兜底。
    """
    _ensure_vcs_maintainer(user, repo_provider, owner, repo_name,
                           scope="解除托管",
                           rate_msg="权限校验被限流，请稍后重试（未解除任何托管）")


def _cleanup_repo_data(repo_key: str, owner: str, repo: str) -> dict:
    """解除托管时的同步清理：关联数据表 + 仓库 skill 目录 + workspace 目录。

    Issue #340：升级为级联清理（issue/issue_state/agent_tasks/devops_pipeline_scene 等
    全量关联数据 + 事务化 + 审计日志），统一入口见 src/repo_cleanup.py。
    本函数保留为兼容包装（历史调用方/测试）；DB 事务失败时降级为旧行为（记日志不抛出）。

    repo_key 为 owner/repo（无前缀）；兼容历史可能存在的 gitcode: 前缀数据。
    用户维度数据（workspaces/chat/{user_id}、workspaces/{owner} 根）不清理。
    返回清理统计。
    """
    from src.kernel.repo_cleanup import cascade_cleanup_repo_data, RepoCleanupError
    try:
        return cascade_cleanup_repo_data(repo_key, owner, repo, trigger="legacy")
    except RepoCleanupError:
        # 事务已回滚：返回空统计（与旧行为一致，不产生半清理）
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
            c.execute(
                "SELECT COALESCE(org_id, 0), COALESCE(is_org_repo, 0) "
                "FROM managed_repos WHERE repo_key=?",
                (repo_key,),
            )
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
    if int(row[1] or 0):
        raise HTTPException(status_code=409, detail={
            "code": 409,
            "error_code": "ORG_REPO_PROTECTED",
            "message": "组织自动仓库禁止迁移",
        })

    # 校验 3：目标组织存在且当前用户是成员
    # target_owner 是 Grape organization.slug
    try:
        with db_lock:
            c = db.cursor()
            c.execute(
                "SELECT o.id, o.is_personal, o.max_repos, m.role "
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
    target_max_repos = int(org_row[2] or 0)

    if target_org_id == src_org_id:
        raise HTTPException(status_code=400, detail={
            "code": 400, "message": "目标组织与源组织相同，无需迁移"})

    transfer_result, updated = transfer_managed_repo_to_org(
        repo_key,
        src_org_id,
        target_org_id,
        user["id"],
        max_repos=target_max_repos,
        include_rows=True,
    )
    if transfer_result == "quota":
        raise HTTPException(status_code=409, detail={
            "code": 409,
            "error_code": "ORG_REPO_QUOTA_EXCEEDED",
            "message": "组织仓库数量已达上限",
        })
    if transfer_result == "busy":
        raise HTTPException(status_code=409, detail={
            "code": 409,
            "error_code": "ORG_REPO_BUSY",
            "message": "组织仓库绑定繁忙，请稍后重试",
        })
    if transfer_result == "protected":
        raise HTTPException(status_code=409, detail={
            "code": 409,
            "error_code": "ORG_REPO_PROTECTED",
            "message": "组织自动仓库禁止迁移",
        })
    if transfer_result != "transferred":
        raise HTTPException(status_code=409, detail={
            "code": 409,
            "error_code": "REPO_ALREADY_BOUND",
            "message": "仓库迁移条件已变化，请刷新后重试",
        })

    log_audit(
        "org_repo_transfer",
        user_id=user["id"],
        operator_id=user["id"],
        detail={
            "repo_key": repo_key,
            "source_org_id": src_org_id,
            "target_org_id": target_org_id,
            "via": "repos_migrate",
        },
    )

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
    """迁移目标仓库的 VCS 侧 maintainer 复核（B 批：方言分派，零平台分支）。

    规则与托管侧同口径；失败不兜底（token 缺失 / API 不可达 / 无法判定 → 403）。
    """
    _ensure_vcs_maintainer(user, repo_provider, owner, repo_name,
                           scope="迁移目标仓库",
                           rate_msg="权限校验被限流，请稍后重试（未执行迁移）")


@router.post("/remove", operation_id="repos_remove", summary="Remove hosting", description="Removes hosting for a repository (unregisters webhook/bot bindings).")
def remove_managed_repo(body: RepoAction, user: dict = Depends(require_no_session_token)):
    from src.kernel.repo_key import to_repo_key
    parts = body.full_name.split("/", 1)
    if len(parts) != 2:
        raise HTTPException(status_code=400, detail={"code": 400, "message": "无效的仓库标识"})
    owner, repo_name = parts
    # 前端可能带平台前缀（github:owner/demo）——VCS 调用必须用裸 owner，
    # 否则上游 URL 变成 /repos/github:owner/demo/... → 404（实弹 2026-11-13）。
    owner = _strip_provider_prefix(owner)

    # 真实前缀解析：候选集探针查 managed_repos（B 批：零平台字面量，原 gh/gc 双探针硬编码）
    from src.kernel.repo_key import repo_key_candidates, parse_repo_key
    _cands = tuple(repo_key_candidates(f"{owner}/{repo_name}"))
    repo_key = to_repo_key(f"{owner}/{repo_name}")
    _repo_exists = False
    try:
        from src.kernel.session_manager import db, db_lock
        with db_lock:
            c0 = db.cursor()
            c0.execute(
                f"SELECT repo_key FROM managed_repos WHERE repo_key IN ({','.join('?' * len(_cands))}) LIMIT 1",
                _cands)
            row0 = c0.fetchone()
        if row0 and row0[0]:
            repo_key = row0[0]
            _repo_exists = True
    except Exception:
        pass  # 测试环境（mock）走默认前缀

    # 不存在的仓库直接 404（避免对未托管仓库做无意义的 VCS 权限调用）
    if not _repo_exists:
        raise HTTPException(status_code=404, detail={"code": 404, "message": "仓库未找到"})

    repo_provider, _pk_owner, _pk_repo = parse_repo_key(repo_key)
    if _pk_owner:
        owner = _pk_owner
    if _pk_repo:
        repo_name = _pk_repo

    # 救火闸门（docs/weekly-08/unmanage-permission-guard-design.md）：
    # 解除托管前校验发起人在 VCS 侧的仓库权限，maintainer 及以上才放行。
    # 必须放在 token 分流/删 webhook 等任何副作用之前。
    _check_unmanage_vcs_permission(user, owner, repo_name, repo_provider)

    # B 批（2026-11-13）：token 与全局 bot 回落都按 provider 派生，零平台分支
    api_token = get_user_token_for(user["username"], user_id=str(user.get("id") or ""), provider=repo_provider)
    if not api_token:
        api_token = get_bot_token(repo_owner=owner, repo_name=repo_name)
    if not api_token:
        # Issue #345 + D15：全局 bot token 走卡驱动来源链（meta key 按 provider 派生）
        from src.domains.mgmt.user.db import get_global_bot_token_for
        api_token = get_global_bot_token_for(repo_provider)

    # 删除 webhook（B 批：卡驱动函数集，零平台分支）
    if api_token:
        try:
            from src.domains.mgmt.user.repo_manager import _get_managed_webhook, get_hosting_provider_config
            wh_id = _get_managed_webhook(repo_key)
            if wh_id:
                _cfg_del = get_hosting_provider_config(repo_provider)
                _cfg_del["remove_webhook"](api_token, owner, repo_name, wh_id)
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
        # 全局 bot 用户名（B 批：统一来源链 resolve_bot_username，卡驱动 + 存量族派生）
        from src.domains.mgmt.user.repo_manager import resolve_bot_username as _resolve_bot
        try:
            global_bot_username = _resolve_bot(repo_provider) or ""
        except Exception as _rb_e:
            log.warning(f"remove_managed_repo: resolve_bot_username({repo_provider}) failed: {_rb_e}")
            global_bot_username = ""
        # owner token 按 provider 派生（B 批：统一入口，零平台分支）
        owner_token = get_user_token_for(user["username"], user_id=str(user.get("id") or ""), provider=repo_provider)
        # Issue #372: 路径 B 仓库（无 bot token）跳过 bot 协作者移除——托管时未邀请 bot
        from src.domains.mgmt.user.db import get_host_user_id
        _is_path_b = (not repo_bot_token and not global_bot_username and get_host_user_id(repo_key) > 0)
        # B 批：协作者函数对按卡方言解析一次，两个 bot 分支共用（解析失败仅降级不做移除）
        _remove_f = None
        try:
            from src.domains.mgmt.user.repo_manager import get_collaborator_funcs
            _, _remove_f = get_collaborator_funcs(repo_provider)
        except Exception as _cf_e:
            log.warning(f"remove_managed_repo: collaborator funcs unavailable ({repo_provider}): {_cf_e}")
        if not _is_path_b:
            if owner_token and global_bot_username and _remove_f:
                try:
                    # B 批：协作者移除走卡驱动函数对（webhook_dialect 同轴分派）
                    _rm_ok = _remove_f(owner_token, owner, repo_name, global_bot_username)
                    if not _rm_ok:
                        log.warning(f"Remove global bot collaborator failed (perm?): {global_bot_username} @ {owner}/{repo_name}")
                except Exception as e:
                    log.warning(f"Remove global bot collaborator error: {e}")
            # repo bot（若配置且非全局 bot）
            if owner_token and repo_bot_username and repo_bot_username != global_bot_username and _remove_f:
                try:
                    # B 批：repo bot 移除同走卡驱动函数对（复用上面已解析的 _remove_f）
                    _rm_ok2 = _remove_f(owner_token, owner, repo_name, repo_bot_username)
                    if not _rm_ok2:
                        log.warning(f"Remove repo bot collaborator failed (perm?): {repo_bot_username} @ {owner}/{repo_name}")
                except Exception as e:
                    log.warning(f"Remove repo bot collaborator error: {e}")
    except Exception:
        pass

    removed = user_db.remove_managed_repo(repo_key)
    if not removed:
        raise HTTPException(status_code=404, detail={"code": 404, "message": "仓库未找到"})
    # 写后联想：解除托管后立即失效 user_repos 缓存，仓库马上落进「未托管」表
    user_db.invalidate_user_repos(user["id"])
    # Issue #340 级联清理：需求中心 Issue 卡片、流水线/事件、知识基线、场景关联、
    # 会话/技能/IM 绑定等全部关联数据，事务化 + 审计日志；失败可重试且无半清理。
    from src.kernel.repo_cleanup import cascade_cleanup_repo_data, RepoCleanupError
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
    from src.kernel.repo_key import split_owner_repo, to_repo_key
    from src.domains.runtime.public import load_project_info
    # 前缀必须解析真实平台：裸 to_repo_key 默认 gitcode:，gitee/github 托管仓会读错 key
    # （2026-11-14 实锤：gitee:yang-aiwu-new/... 的 project-info 返回 gitcode: 前缀 + 空基线）
    from src.domains.mgmt.user.db import resolve_managed_repo_key
    repo_key = resolve_managed_repo_key(owner, repo) or to_repo_key(f"{owner}/{repo}")
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
    from src.kernel.repo_key import to_repo_key, repo_key_candidates
    owner_repo = f"{owner}/{repo}"
    if ":" in owner or ":" in repo:
        return to_repo_key(owner_repo)
    try:
        from src.kernel.session_manager import db as _db, db_lock as _dl
        _cands = repo_key_candidates(owner_repo)
        _ph = ",".join("?" * len(_cands))
        with _dl:
            c = _db.cursor()
            row = c.execute(f"SELECT repo_key FROM managed_repos WHERE repo_key IN ({_ph}) LIMIT 1",
                            _cands).fetchone()
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


# ===================== git 传输支撑（方案③，grape 平台私有能力）=====================
# git-credential / workspace 是 grape 自身能力（非 gitcode/github/gitlab 平台原生），
# 故置于 /api/repos（grape 平台业务）而非 /api/git-repo（平台透传）。

# git 传输可用平台 = 在册平台卡目录（2026-11-12 卡驱动；此前硬编码 {"gitcode","github"}
# 导致 gitee/gitlab 仓的 agent 会话一律 403「Git provider 未接入」，换不到 git 凭据）。
_GITCODE_API_BASE = "https://gitcode.com/api/v5"
_GITHUB_API_BASE = "https://api.github.com"
_CREDENTIAL_TTL = 600


def _transport_providers() -> frozenset:
    return frozenset(_repo_listable_providers())


def _clone_host(provider: str) -> str:
    """按平台卡取 clone/web 域名（无卡时回退 provider key 自身）。"""
    from src.domains.runtime.public import get_platform_card
    card = get_platform_card(provider)
    return ((card.host if card and card.host else "") or provider or "").strip()


def _provider_display(provider: str) -> str:
    """平台展示名（卡 name；无卡回退 provider key）。"""
    from src.domains.runtime.public import get_platform_card
    card = get_platform_card(provider)
    return ((card.name if card and card.name else "") or provider or "").strip()


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
        for p in _transport_providers():
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
    p = _provider_from_path(request.url.path) \
        or parse_repo_key(user.get("repo_key") or "")[0] or "gitcode"
    if p not in _transport_providers():
        raise HTTPException(status_code=403, detail={"code": 403, "message": f"Git provider 未接入: {p}"})
    return p


def _strip_provider_prefix(owner: str) -> str:
    """剥离 owner 段的 provider 前缀（gitcode:/github:），供 URL/workspace 拼接。

    Issue #466：带前缀路径（/api/repos/gitcode:owner/repo/...）的 owner path 段
    含前缀；token 解析需要带前缀 repo_key（幂等），但 clone_url / workspace
    路径须用裸 owner/repo。未知前缀原样返回。
    """
    s = str(owner or "")
    low = s.lower()
    for p in _transport_providers():
        if low.startswith(f"{p}:"):
            return s[len(p) + 1:]
    return s


GIT_CREDENTIAL_ERROR_CODE = "GIT_CREDENTIAL_UNAVAILABLE"
GIT_CREDENTIAL_RETRY_AFTER_S = 15

# 凭据解析失败阶段：三个分支各自独立，供 CLI/运维区分故障来源（Issue #1063）
_GIT_CREDENTIAL_STAGE_BOT = "bot_token_lookup"
_GIT_CREDENTIAL_STAGE_USER = "user_token_lookup"
_GIT_CREDENTIAL_STAGE_UNEXPECTED = "credential_resolve"


def _repo_platform_token(owner: str, repo: str, user: dict, provider: str = "gitcode") -> str:
    """平台 token 解析（仅会话级临时 token tk_；用户 API Key / Web JWT 不适用）。

    chat agent → 用户 token；devops 事件 agent → 仓库 bot token；一种缺失交叉回退；均缺失 502。
    Issue #1063：分别记录 bot 读取异常 / user 刷新异常 / 两者均空，失败响应带 stage 与错误码。
    """
    if user.get("auth_type") != "session_token":
        raise HTTPException(status_code=403, detail={"code": 403, "message": "Git 仓库接口仅支持会话级临时 token（agent 专用），用户 API Key / Web 会话不适用"})
    from src.kernel.repo_key import to_repo_key

    # Issue #466: owner 段可带 provider 前缀（跨平台关联仓换券）；to_repo_key 幂等，
    # 禁止再手工拼前缀，否则会得到双前缀导致 managed_repos 精确匹配不到 → 502。
    from src.domains.mgmt.user.repo_manager import repo_token_getters
    # QA C2（2026-11-13）：has_user_id 在 base 就只有使用没有 import（既有缺陷），
    # 本批把 outlet 从 else 分支扩散到 _getters 主路径 → NameError 被 except 吞成
    # 空 token（用户凭据解析全线失效）。这里显式绑定。
    from src.kernel.user_ids import has_user_id
    rk = to_repo_key(f"{owner}/{repo}", provider=provider)
    _start = time.monotonic()

    def _fail(stage: str, exc: Exception):
        return "", stage, exc

    _getters = repo_token_getters(provider)
    # 内部契约：_bot()/_user() 必须返回 (token, stage, exc) —— 下面按 3 元组解包。
    # B7 曾把这两个函数写成裸字符串，端点每次调用都会 ValueError（500）；
    # 2026-11-13 附带的用例 test_repo_token_getters_provider_keyed 现在锁住该契约。
    if _getters:
        # 存量直读器（按 provider 登记：GitHub / GitCode 的存量四级 fallback）
        _rk = (to_repo_key(f"{owner}/{repo}", provider=provider)
               if _getters.get("use_provider_key") else to_repo_key(f"{owner}/{repo}"))

        def _bot():
            try:
                import importlib
                _mod, _fn = _getters["bot"]
                return getattr(importlib.import_module(_mod), _fn)(repo_key=_rk) or "", "", None
            except Exception as e:
                return _fail(_GIT_CREDENTIAL_STAGE_BOT, e)

        def _user():
            try:
                import importlib
                _mod, _fn = _getters["user"]
                _getter = getattr(importlib.import_module(_mod), _fn)
                uid = user.get("id") or 0
                username = user.get("username") or ""
                if not username and has_user_id(uid):
                    from src.domains.mgmt.auth.grape_account_db import get_grape_account_by_id
                    username = (get_grape_account_by_id(uid) or {}).get("username", "")
                return _getter(username, user_id=uid) or "", "", None
            except Exception as e:
                return _fail(_GIT_CREDENTIAL_STAGE_USER, e)
    else:
        # 其余在册平台（gitee/gitlab…）：仓库级 bot token 走端口（token_storage_field），
        # 用户 token 走 provider 无关解析（PAT→OAuth），零平台分支。
        def _bot():
            try:
                from src.kernel.token_resolver import TokenResolver, create_provider
                return TokenResolver(create_provider(provider)).read_stored_token(rk) or "", "", None
            except Exception as e:
                return _fail(_GIT_CREDENTIAL_STAGE_BOT, e)

        def _user():
            try:
                from src.api.deps import get_user_token_for
                username = user.get("username") or ""
                uid = user.get("id") or 0
                if not username and has_user_id(uid):
                    from src.domains.mgmt.auth.grape_account_db import get_grape_account_by_id
                    username = (get_grape_account_by_id(uid) or {}).get("username", "")
                return get_user_token_for(username, user_id=str(uid), provider=provider) or "", "", None
            except Exception as e:
                return _fail(_GIT_CREDENTIAL_STAGE_USER, e)

    if user.get("agent_type") == "chat":
        tok, stage, exc = _user()
        if not tok:
            alt, alt_stage, alt_exc = _bot()
            tok = alt
            if not tok and not stage:
                stage, exc = alt_stage, alt_exc
    else:
        tok, stage, exc = _bot()
        if not tok:
            alt, alt_stage, alt_exc = _user()
            tok = alt
            if not tok and not stage:
                stage, exc = alt_stage, alt_exc
    if tok:
        log.debug("git_credential_ok", extra={
            "event": "git_credential_ok",
            "provider": provider,
            "repo_key": rk,
            "auth_type": user.get("auth_type", ""),
            "agent_type": user.get("agent_type", ""),
            "duration_ms": round((time.monotonic() - _start) * 1000),
        })
        return tok
    raise HTTPException(status_code=502, detail={"code": 502, "message": f"{_provider_display(provider)} 仓库 token 解析失败"})


@router.get("/{owner}/{repo}/git-credential", operation_id="repos_git_credential",
            summary="One-shot git platform credential (CLI internal, TTL)",
            description="One-shot short-lived platform credential for local git via CLI (in-process, never persisted). "
                        "gitcode uses Basic auth with username x-access-token and the configured gitcode token as password. Session-only.")
def get_repo_git_credential(owner: str, repo: str, user: dict = Depends(get_current_user_or_api_key),
                            p: str = Depends(_repo_provider)):
    """返回 CLI 本地执行 git 所需的平台凭据（一次性/短期，进程内使用不落盘）。

    gitcode：Basic 用 username=`x-access-token`、password=配置的 gitcode token。
    Issue #466：owner 段可带 provider 前缀（跨平台关联仓换券）；clone_url 用裸 owner/repo。
    Issue #1063：未预期异常记录 stage 后交给全局 handler（500 + request_id）。

    安全取舍：未预期异常同样只记录异常类名（不打 traceback），避免异常原文中的
    token/凭据 URL 落日志；现场由 request_id 关联全局 handler 的 500 记录。
    """
    token = _repo_platform_token(owner, repo, user, p)
    _gh = _clone_host(p)
    _o = _strip_provider_prefix(owner)
    return {
        "provider": p,
        "username": "x-access-token",
        "password": token,
        "clone_url": f"https://{_gh}/{_o}/{repo}.git",
        "expires_in": _CREDENTIAL_TTL,
    }


@router.get("/{owner}/{repo}/workspace", operation_id="repos_workspace", summary="Repository workspace path (local git via CLI)", description="Returns the grape-managed local workspace path for the repository (CLI local git on the server). Session-only.")
def get_repo_workspace(owner: str, repo: str, user: dict = Depends(get_current_user_or_api_key),
                       p: str = Depends(_repo_provider)):
    _o = _strip_provider_prefix(owner)
    ws = os.path.join(WORKSPACE_DIR, _o, repo)
    _gh = _clone_host(p)
    return {"workspace": ws, "clone_url": f"https://{_gh}/{_o}/{repo}.git"}
