"""GitCode 仓库托管管理：获取管理员仓库、创建/删除 webhook"""

import json, urllib.request, urllib.error, urllib.parse, os, shutil, tempfile

import requests as _requests

from src.kernel.config import log

# GitCode API 端点迁移：api.gitcode.com 上 /user/repos 和 /repos/... 已全部 404，
# 仓库相关 API 改走 gitcode.com（证书正常，不需要 verify=False）。
# /api/v5/user 仍在 api.gitcode.com 上可用（auth.py 单独使用）。
API_BASE = "https://gitcode.com/api/v5"

class UpstreamTimeoutError(RuntimeError):
    """上游 Git 平台 API 请求超时（Issue #1940，#2084 移植）。

    继承 RuntimeError：既有 ``except RuntimeError`` 调用点零改动兼容；
    ``str()`` 保持 "Cannot reach GitCode API: ..." / "Cannot reach GitHub API: ..." 格式。

    语义：请求已发出、响应未按时返回——上游可能已受理并落地（写操作不可盲重试），
    供写链路分流（写 504 uncertain / 读 502 维持）。
    """


class UpstreamAPIError(RuntimeError):
    """上游 Git 平台 API 返回 >=400（Issue #549）：携带真实 status_code / message。

    继承 RuntimeError：既有 `except RuntimeError` 调用点（add_collaborator /
    get_member_permission 等）零改动兼容；`str()` 保持
    "GitCode API {method} {url} failed ({code}): {message}" 格式，
    `_extract_http_code` 的 `\\((\\d{3})\\)` 正则与字符串判定不受影响。
    """

    def __init__(self, method: str, url: str, status_code: int, message: str,
                 provider: str = "GitCode"):
        self.method = method
        self.url = url
        self.status_code = int(status_code)
        self.message = message or ""
        self.provider = provider
        super().__init__(
            f"{provider} API {method} {url} failed ({self.status_code}): {self.message}"
        )

def upstream_timeout_seconds(default: float = 25.0) -> float:
    """上游 Git 平台请求超时（秒）——Issue #2084（#2084 移植）超时错开。

    - `GRAPE_UPSTREAM_TIMEOUT_SECONDS` 环境变量可调（运行时逐次读取，改配置即时生效）；
    - 默认 25s **严格小于** CLI 客户端 30s：上游慢响应时服务端先超时返回
      可判定的 504 uncertain + request_id + 回读指引（#1940 写超时语义），
      而不是与客户端 30s 同值竞速让调用方先本地断开（eval-F10「客户端先断」）；
    - 非法值/非正值回落默认（配置错误不打瘫上游客户端）。
    """
    raw = os.environ.get("GRAPE_UPSTREAM_TIMEOUT_SECONDS", "").strip()
    if not raw:
        return default
    try:
        v = float(raw)
    except ValueError:
        return default
    return v if v > 0 else default


def _req(method: str, url: str, token: str = "", body: dict | None = None) -> dict:
    headers = {"Content-Type": "application/json", "Accept": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    try:
        if method == "GET":
            resp = _requests.get(url, headers=headers, timeout=upstream_timeout_seconds())
        elif method == "POST":
            resp = _requests.post(url, json=body, headers=headers, timeout=upstream_timeout_seconds())
        elif method == "DELETE":
            resp = _requests.delete(url, headers=headers, timeout=upstream_timeout_seconds())
        elif method == "PUT":
            resp = _requests.put(url, json=body, headers=headers, timeout=upstream_timeout_seconds())
        elif method == "PATCH":
            resp = _requests.patch(url, json=body, headers=headers, timeout=upstream_timeout_seconds())
        else:
            raise ValueError(f"Unsupported method: {method}")
        if resp.status_code >= 400:
            # Issue #549: 上游真实状态码透传（不截断 message），供 _vcs_req 分流
            raise UpstreamAPIError(method, url, resp.status_code, resp.text)
        if resp.text.strip():
            return resp.json()
        return {}
    except _requests.exceptions.RequestException as e:
        raise RuntimeError(f"Cannot reach GitCode API: {e}")

def _check_collaborator_admin(api_token: str, owner: str, repo: str, username: str) -> bool:
    """调 /collaborators/{username}/permission 接口精确判断管理/维护权限。

    GitCode 实测坑：该接口返回的复数 ``permissions.admin`` 恒为 false
    （维护者 access_level=40 时同样是 false），不能用于判定是否能托管。
    真实权限看同接口的：
      - ``access_level``：30=开发者(Developer) / 40=维护者(Maintainer) / >=50=管理员
      - 单数 ``permission`` 字符串："write"(开发者) / "admin"(维护者及以上)
    因此按 access_level>=40 判定（维护者及以上可托管），单数 permission 与
    复数 permissions.admin 作兜底兼容。
    """
    if not username:
        return False
    # 项目所有者直接有完全权限
    if username == owner:
        return True
    url = f"{API_BASE}/repos/{owner}/{urllib.parse.quote(repo, safe='')}/collaborators/{urllib.parse.quote(username, safe='')}/permission"
    try:
        data = _req("GET", url, api_token)
    except RuntimeError:
        return False
    try:
        access_level = int(data.get("access_level", 0) or 0)
    except (TypeError, ValueError):
        access_level = 0
    if access_level >= 40:  # Maintainer(40) / Admin(>=50)
        return True
    if str(data.get("permission", "") or "").lower() == "admin":
        return True
    perm = data.get("permissions") or {}
    return bool(perm.get("admin")) if isinstance(perm, dict) else False

# GitCode 成员权限枚举（官方 API：PUT /collaborators/{username} body 用单数字段 permission）
# 参与者→pull，开发者→push，仓库维护者→admin，自定义角色→customized
_PERMISSION_TO_GITCODE = {
    "maintainer": "admin",
    "admin": "admin",
    "developer": "push",
    "push": "push",
    "member": "pull",
    "reporter": "pull",
}

def add_collaborator(api_token: str, owner: str, repo: str, username: str,
                     permission: str = "maintainer") -> tuple[bool, str]:
    """将用户添加为仓库协作者（GitCode 邀请制：创建邀请，需对方接受）。

    注意：
      - api_token 必须是仓库所有者/管理员的 token（GitCode 拒绝用 bot 自己 token 邀请自己）。
      - GitCode PUT body 字段名是单数 `permission`，枚举值：pull/push/admin/customized。
      - 已存在成员 / 已被邀请（400"已是成员"）视为成功（幂等）。
      - 不再静默降级：maintainer 邀请失败直接返回 (False, error_detail)，由调用方决定是否拒绝托管。
    返回：(True, "") 成功；(False, error_detail) 失败——error_detail 为友好错误文案。
    """
    if not username or not api_token:
        return False, "Token 或用户名缺失"
    gitcode_perm = _PERMISSION_TO_GITCODE.get((permission or "").lower(), permission)
    url = f"{API_BASE}/repos/{owner}/{urllib.parse.quote(repo, safe='')}/collaborators/{urllib.parse.quote(username, safe='')}"
    try:
        _req("PUT", url, api_token, {"permission": gitcode_perm})
        return True, ""
    except RuntimeError as e:
        # 已是成员 / 已被邀请（幂等成功）
        if any(k in str(e) for k in ("已经是项目中的成员", "已经是", "已存在", "已被邀请", "已邀请", "已经邀请")):
            return True, ""
        # 提取 HTTP code 做差异化提示
        code = _extract_http_code(e)
        if code == 401:
            err = "GitCode Token 已过期，请重新登录后再试"
        elif code == 403:
            err = "无权限操作，请确认你是仓库管理员"
        elif code == 404:
            err = f"仓库 {owner}/{repo} 不存在"
        elif code in (500, 502, 503, 504):
            err = "GitCode 服务暂时不可用，请稍后重试"
        elif code == 429:
            err = "GitCode API 调用过于频繁，请稍后重试"
        else:
            err = f"邀请失败（HTTP {code}）" if code else f"邀请失败: {e}"
        log.warning(f"add_collaborator failed: {e}")
        return False, err

def remove_collaborator(api_token: str, owner: str, repo: str, username: str) -> bool:
    """将用户从仓库协作者移除。返回是否成功（用户不在时视为成功）。

    注意：api_token 必须是仓库所有者/管理员的 token。
    """
    if not username or not api_token:
        return False
    url = f"{API_BASE}/repos/{owner}/{urllib.parse.quote(repo, safe='')}/collaborators/{urllib.parse.quote(username, safe='')}"
    try:
        _req("DELETE", url, api_token)
        return True
    except RuntimeError as e:
        # 404（用户本就不是协作者）视为成功
        if "404" in str(e):
            return True
        return False

def _req_query(method: str, path: str, token: str, params: dict | None = None,
               body: dict | None = None) -> dict:
    """GitCode 定制化接口请求：凭据用 query 参数 access_token（非 Bearer header）。

    用于 /api/v5/user/invite 系列接口（仅全局 bot grape_bot 可调用）。
    """
    if params is None:
        params = {}
    params["access_token"] = token
    query = urllib.parse.urlencode(params)
    url = f"{API_BASE}{path}?{query}"
    headers = {"Content-Type": "application/json", "Accept": "application/json"}
    data = json.dumps(body).encode() if body else None
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=upstream_timeout_seconds()) as resp:
            raw = resp.read().decode()
            return json.loads(raw) if raw.strip() else {}
    except urllib.error.HTTPError as e:
        raw = e.read().decode(errors="replace")
        raise UpstreamAPIError(method, path, e.code, raw)
    except urllib.error.URLError as e:
        raise RuntimeError(f"Cannot reach GitCode API: {e.reason}")

def list_user_invites(token: str, page: int = 1, per_page: int = 100) -> list[dict]:
    """查询当前 token 用户的项目邀请列表（GET /api/v5/user/invite）。

    返回 content 列表，每项含 source_name(owner/repo)、invitation_token、
    user_role、inviter_username、expires_at 等。
    """
    data = _req_query("GET", "/user/invite", token,
                      {"page": page, "per_page": min(per_page, 100)})
    content = data.get("content", []) if isinstance(data, dict) else []
    return content if isinstance(content, list) else []

def accept_project_invite(token: str, invitation_token: str) -> bool:
    """接受项目邀请（POST /api/v5/user/invite/accept）。"""
    if not token or not invitation_token:
        return False
    try:
        _req_query("POST", "/user/invite/accept", token,
                   {"invitation_token": invitation_token})
        return True
    except RuntimeError as e:
        log.warning(f"accept_project_invite failed: {e}")
        return False

def reject_project_invite(token: str, invitation_token: str) -> bool:
    """拒绝项目邀请（POST /api/v5/user/invite/reject）。"""
    if not token or not invitation_token:
        return False
    try:
        _req_query("POST", "/user/invite/reject", token,
                   {"invitation_token": invitation_token})
        return True
    except RuntimeError as e:
        log.warning(f"reject_project_invite failed: {e}")
        return False

def accept_invite_for_repo(token: str, owner: str, repo: str) -> bool:
    """查询邀请列表，找到匹配 owner/repo 的邀请并接受。返回是否成功。

    用于全局 bot（grape_bot）被邀请加入托管仓库后自动同意。
    """
    if not token or not owner or not repo:
        return False
    try:
        invites = list_user_invites(token, per_page=100)
    except RuntimeError as e:
        log.warning(f"accept_invite_for_repo: list invites failed: {e}")
        return False
    target = f"{owner}/{repo}"
    for inv in invites:
        source = inv.get("source_name", "") or ""
        if source.lower() == target.lower():
            inv_token = inv.get("invitation_token", "")
            if inv_token:
                return accept_project_invite(token, inv_token)
    log.info(f"accept_invite_for_repo: no matching invite for {target} (total={len(invites)})")
    return False

def _extract_http_code(err: Exception) -> int:
    """从异常提取上游 HTTP 状态码：优先 UpstreamAPIError.status_code，正则兜底。"""
    code = getattr(err, "status_code", 0)
    if code:
        return int(code)
    import re
    m = re.search(r"\((\d{3})\)", str(err))
    return int(m.group(1)) if m else 0

# 可重试的限流/服务端瞬时错误
_RETRYABLE_CODES = {429, 500, 502, 503, 504}

def get_member_permission(token: str, owner: str, repo: str, username: str,
                          _retries: int = 3) -> dict | None:
    """查看仓库成员权限详情（GET /collaborators/{username}/permission）。

    token 传全局 bot token（grape_bot 自查自身权限）。
    返回含 access_level（30=Developer，40=Maintainer）、role_name、permission 等。
    429/5xx 退避重试（1s/2s/4s）；确定性失败（403/404 等）返回 None。
    """
    if not username or not token:
        return None
    import time
    url = f"{API_BASE}/repos/{owner}/{urllib.parse.quote(repo, safe='')}/collaborators/{urllib.parse.quote(username, safe='')}/permission"
    last = None
    for i in range(_retries):
        try:
            return _req("GET", url, token)
        except RuntimeError as e:
            code = _extract_http_code(e) or 0
            if code in _RETRYABLE_CODES or "too large" in str(e) or "429" in str(e):
                log.warning(f"get_member_permission rate-limited (code={code}), retry {i + 1}/{_retries}: {e}")
                last = e
                time.sleep(2 ** i)
                continue
            log.warning(f"get_member_permission failed: {e}")
            return None
    # 重试耗尽：抛限流标记，供调用方区分「未判定」vs「确认无权限」
    raise _RateLimitedError(f"GitCode API rate limited: {last}")

class _RateLimitedError(RuntimeError):
    """GitCode API 限流标记（重试耗尽后抛出），调用方可捕获以提示"稍后重试"。"""

def fetch_repos_with_role(api_token: str) -> list[dict]:
    """获取用户的项目成员仓库，标注 is_admin。
    仓库所有者 | 组织管理员 → is_admin=True
    维护者 → is_admin=False
    返回：[{"full_name": "owner/repo", "owner": "owner", "name": "repo", "is_admin": bool}, ...]

    优化（避免 N+1）：GitCode /user/repos 列表响应已含 `permission` 字段
    （{pull, push, admin} 布尔），直接用它判定管理员，不再对每个仓库单独调
    collaborator permission API。
    """
    if not api_token:
        raise RuntimeError("GitCode API Token 未配置")
    # 获取当前用户身份
    current_user = ""
    try:
        user_data = _req("GET", f"{API_BASE}/user", api_token)
        current_user = user_data.get("login", "")
    except RuntimeError:
        pass
    repos = []
    page = 1
    while True:
        url = f"{API_BASE}/user/repos?type=all&per_page=100&page={page}&sort=full_name"
        data = _req("GET", url, api_token)
        log.info(f"fetch_repos page {page}: type={type(data).__name__}, len={len(data) if isinstance(data, (list, dict)) else '?'}")
        if not data:
            break
        items = data if isinstance(data, list) else []
        if not items and isinstance(data, dict):
            for key in ("data", "list", "items", "content"):
                if key in data and isinstance(data[key], list):
                    items = data[key]
                    break
        for r in items:
            full_name = r.get("full_name", "")
            if not full_name or "/" not in full_name:
                continue
            if not repos and items.index(r) == 0:
                log.info(f"First repo: {r.get('full_name')}, owner={r.get('owner')}")
            owner_obj = r.get("owner", {})
            owner_login = owner_obj.get("login", "") if isinstance(owner_obj, dict) else ""
            release_owner = full_name.split("/", 1)[0]
            # 所有者或 permission.admin=true → 管理员（列表响应已含权限，无需额外 API）
            perm = r.get("permission") or {}
            is_admin = bool(perm.get("admin")) if isinstance(perm, dict) else False
            if current_user and owner_login and current_user == owner_login:
                is_admin = True
            repos.append({"full_name": full_name, "owner": release_owner,
                          # GitCode name 是显示名（可≠URL path，如 shelltest 显示 skillstest），
                          # URL 语义必须用 full_name 拆分——下游拼 gitcode.com/{owner}/{name} 链接
                          "name": full_name.split("/", 1)[1], "is_admin": is_admin,
                          "created_at": r.get("created_at", "") or ""})
        if len(items) < 100:
            break
        page += 1
    log.info(f"fetch_repos total: {len(repos)} repos (current_user={current_user})")
    return repos

def get_user_gitcode_login_status(api_token: str) -> tuple[str, int]:
    """Issue #3563: (login, status) 变体——透出上游状态码，不抛契约不变。

    status 口径：0=成功；>=400=上游 HTTP 状态码（401/403/5xx）；
    -1=网络/未知失败。成功/失败各有独立日志行（provider、status、结果）。
    """
    if not api_token:
        return "", -1
    try:
        data = _req("GET", f"{API_BASE}/user", api_token)
        login = data.get("login", "") or ""
        log.info(f"get_user_gitcode_login: ok (login={login})")
        return login, 0
    except UpstreamAPIError as e:
        log.warning(f"get_user_gitcode_login failed (status={e.status_code}): {e.message[:120]}")
        return "", e.status_code
    except RuntimeError as e:
        log.warning(f"get_user_gitcode_login failed: {e}")
        return "", -1

def get_user_gitcode_login(api_token: str) -> str:
    """获取当前 token 的 GitCode 登录用户名（GET /api/v5/user）。

    Issue #3563：变薄包装——失败返回空串的既有契约不变（20+ 调用点零改动）；
    需要上游状态码的调用方改用 get_user_gitcode_login_status。
    """
    login, _status = get_user_gitcode_login_status(api_token)
    return login

def fetch_user_orgs(api_token: str, *, admin_only: bool = False) -> list[dict]:
    """获取当前 token 用户的 GitCode 组织列表。

    Issue #543：迁移目标组织下拉框数据源。
    返回 [{"login", "name", "avatar_url"}, ...]（login 为组织账号名，用作 target_owner）。
    失败抛 RuntimeError（由 with_oauth_token 统一处理 401/403 token 失效）。

    GitCode 无 /user/orgs 端点（404），需先获取用户 login，
    再走 /users/{login}/orgs 查组织列表。
    admin_only=True 时改走 /users/orgs?admin=true（仅返回当前用户可管理的组织，
    用于「创建组织仓库」的所有者下拉，避免列出无建仓权限的空间）。
    """
    if not api_token:
        raise RuntimeError("GitCode API Token 未配置")

    # 1. 获取当前用户 login
    user_url = f"{API_BASE}/user"
    user_data = _req("GET", user_url, api_token)
    login = user_data.get("login") or user_data.get("username") or ""
    if not login:
        raise RuntimeError("无法获取当前用户 login")

    # 2. 查用户组织列表
    orgs: list[dict] = []
    page = 1
    while True:
        if admin_only:
            url = f"{API_BASE}/users/orgs?per_page=50&page={page}&admin=true"
        else:
            url = f"{API_BASE}/users/{urllib.parse.quote(login, safe='')}/orgs?per_page=50&page={page}"
        data = _req("GET", url, api_token)
        items = data if isinstance(data, list) else []
        if not items and isinstance(data, dict):
            for key in ("data", "list", "items", "content"):
                if key in data and isinstance(data[key], list):
                    items = data[key]
                    break
        for o in items:
            if not isinstance(o, dict):
                continue
            org_login = o.get("login") or o.get("path") or o.get("username") or ""
            if not org_login:
                continue
            orgs.append({
                "login": org_login,
                "name": o.get("name") or o.get("nickname") or org_login,
                "avatar_url": o.get("avatar_url") or "",
            })
        if len(items) < 50:
            break
        page += 1
        if page > 20:  # 安全上限（1000 组织）
            break
    return orgs


def fetch_github_orgs(api_token: str, *, admin_only: bool = False) -> list[dict]:
    """获取当前 token 用户可建仓的 GitHub 组织（创建组织仓库的所有者下拉）。

    优先 /user/memberships/orgs（带 role；admin_only 时只保留 role=admin，
    即拥有组织级建仓/管理权限的组织），该端点 404/权限不足时回落到 /user/orgs。
    返回 [{"login", "name", "avatar_url"}, ...]。
    """
    if not api_token:
        raise RuntimeError("GitHub API Token 未配置")

    orgs: list[dict] = []
    memberships_ok = False
    page = 1
    try:
        while True:
            url = (f"{_GITHUB_API_BASE}/user/memberships/orgs"
                   f"?state=active&per_page=100&page={page}")
            data = _github_req("GET", url, api_token)
            items = data if isinstance(data, list) else []
            if not items:
                break
            memberships_ok = True
            for m in items:
                if not isinstance(m, dict):
                    continue
                role = str(m.get("role") or "").lower()
                if admin_only and role and role != "admin":
                    continue
                org = m.get("organization") if isinstance(m.get("organization"), dict) else m
                login = org.get("login") or ""
                if not login:
                    continue
                orgs.append({
                    "login": login,
                    "name": org.get("name") or login,
                    "avatar_url": org.get("avatar_url") or "",
                })
            if len(items) < 100:
                break
            page += 1
            if page > 10:  # 安全上限（1000 组织）
                break
    except Exception as exc:
        # memberships 端点依赖 read:org 等 scope；失败时回落 /user/orgs（若 token 本身
        # 无效，回落也会失败并上抛，由路由层标记 connected=false）。
        log.warning(f"fetch_github_orgs memberships fallback: {exc}")
        memberships_ok = False
        orgs = []

    if memberships_ok:
        return orgs

    # 回落：/user/orgs（无 role 字段，只能按成员关系列出）
    page = 1
    while True:
        url = f"{_GITHUB_API_BASE}/user/orgs?per_page=100&page={page}"
        items = _github_req("GET", url, api_token)
        if not isinstance(items, list) or not items:
            break
        for o in items:
            if not isinstance(o, dict):
                continue
            login = o.get("login") or ""
            if not login:
                continue
            orgs.append({
                "login": login,
                "name": o.get("name") or login,
                "avatar_url": o.get("avatar_url") or "",
            })
        if len(items) < 100:
            break
        page += 1
        if page > 10:
            break
    return orgs


# 支持「在组织空间创建仓库」的平台 → 组织列表抓取实现。
# 逐行单平台键：平台能力表驱动，新增平台只需在此登记，路由层零平台分支。
_ORG_NAMESPACE_FETCHERS = {
    "gitcode": fetch_user_orgs,
    "github": fetch_github_orgs,
}


def org_namespace_providers() -> list[str]:
    """支持选择所属空间（个人/组织）的 provider 列表。"""
    return sorted(_ORG_NAMESPACE_FETCHERS)


def fetch_org_namespaces(provider: str, api_token: str) -> list[dict]:
    """按 provider 抓取当前用户可创建仓库的组织空间列表（未登记平台返回 []）。"""
    fetcher = _ORG_NAMESPACE_FETCHERS.get(str(provider or "").lower())
    if fetcher is None:
        return []
    return fetcher(api_token, admin_only=True)


def create_repo(api_token: str, name: str, description: str = "", private: bool = False,
                owner: str = "", path: str = "", auto_init: bool = False,
                default_branch: str = "main") -> dict:
    """创建空仓库（POST /api/v5/user/repos，指定 owner 时 POST /api/v5/orgs/{owner}/repos）。

    owner 为空 = 在 token 用户个人空间创建；owner 为组织 path 时在组织空间创建。
    path/GitCode 仓库 URL 路径，缺省与 name 一致。
    返回 GitCode 仓库信息（含 full_name / html_url 等）。同名仓库冲突时抛 RuntimeError。
    """
    if not api_token:
        raise RuntimeError("GitCode API Token 未配置")
    if not name:
        raise RuntimeError("仓库名不能为空")
    owner = (owner or "").strip()
    repo_path = (path or "").strip() or name
    body = {
        "name": name,
        "description": description,
        "private": bool(private),
        "auto_init": bool(auto_init),
        "default_branch": (default_branch or "main"),
        "repository_type": "code",
        "path": repo_path,
    }
    endpoint = (f"{API_BASE}/orgs/{urllib.parse.quote(owner, safe='')}/repos"
                if owner else f"{API_BASE}/user/repos")
    result = _req("POST", endpoint, api_token, body)
    full_name = result.get("full_name", "")
    if not full_name and result.get("name"):
        if owner:
            full_name = f"{owner}/{result.get('name')}"
        else:
            full_name = f"{get_user_gitcode_login(api_token)}/{result.get('name')}"
    if not full_name:
        raise RuntimeError(f"创建仓库成功但未返回仓库信息: {json.dumps(result, ensure_ascii=False)[:200]}")
    return result

def set_default_branch(api_token: str, owner: str, repo: str, branch: str,
                       provider: str = "gitcode") -> bool:
    """把已存在分支设为仓库默认分支（组织仓库默认 main / 存量分支收口）。

    GitHub/GitCode 建仓时对自定义 default_branch 支持不一致；统一在建出首个分支后
    通过平台 API 校正。失败只返回 False，由调用方 best-effort 记录。
    """
    branch = (branch or "").strip()
    if not (api_token and owner and repo and branch):
        return False
    prov = (provider or "gitcode").strip().lower()
    try:
        if prov == "github":
            _github_req("PATCH", f"{_GITHUB_API_BASE}/repos/{owner}/{repo}",
                        api_token, {"default_branch": branch})
            return True
        if prov == "gitcode":
            _req("PATCH", f"{API_BASE}/repos/{urllib.parse.quote(owner, safe='')}/"
                          f"{urllib.parse.quote(repo, safe='')}",
                 api_token, {"default_branch": branch})
            return True
    except Exception as exc:
        log.warning(f"set default branch failed provider={prov} {owner}/{repo} -> {branch}: {exc}")
    return False


def delete_gitcode_repo(api_token: str, owner: str, repo: str) -> bool:
    """删除 GitCode 仓库（DELETE /api/v5/repos/{owner}/{repo}）。

    Issue #2261：删除组织时回收该组织自动创建的仓库。仅允许对「组织自动仓库」
    调用；用户手工关联的仓库绝不能走这里（删组织不应删用户的远端仓库）。
    """
    if not (api_token and owner and repo):
        raise RuntimeError("delete repo: token/owner/repo 不能为空")
    _req("DELETE", f"{API_BASE}/repos/{owner}/{repo}", api_token)
    return True


def delete_github_repo(api_token: str, owner: str, repo: str) -> bool:
    """删除 GitHub 仓库（DELETE /repos/{owner}/{repo}）。语义同 delete_gitcode_repo。"""
    if not (api_token and owner and repo):
        raise RuntimeError("delete repo: token/owner/repo 不能为空")
    _github_req("DELETE", f"https://api.github.com/repos/{owner}/{repo}", api_token)
    return True


def _run_git(args: list[str], cwd: str, timeout: int = 120) -> str:
    """在指定目录执行 git 命令，返回 stdout。失败抛 RuntimeError。"""
    import subprocess
    try:
        import os as _os
        _env = dict(_os.environ)
        # 一律非交互：没有 TTY/credential helper 时让 git 立刻失败并给出明确
        # 错误，而不是尝试读密码（线上曾出现 "No such device or address"）
        _env["GIT_TERMINAL_PROMPT"] = "0"
        _env["GIT_ASKPASS"] = ""
        p = subprocess.run(
            ["git", "-c", "credential.helper="] + args,
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=timeout,
            env=_env,
        )
    except FileNotFoundError:
        raise RuntimeError("服务器未安装 git CLI")
    except subprocess.TimeoutExpired:
        raise RuntimeError("git 操作超时")
    if p.returncode != 0:
        err = (p.stderr or p.stdout or "").strip()[:500]
        raise RuntimeError(f"git {' '.join(args[:2])} 失败: {err}")
    return (p.stdout or "").strip()

def _download_and_extract_tarball(url, workdir, tmp, headers=None, token=None):
    """下载 tarball 并解压到 workdir，然后 git init（统一用于 GitHub/GitCode）。"""
    import tarfile
    if token and not headers:
        headers = {"PRIVATE-TOKEN": token}
    resp = _requests.get(url, headers=headers or {}, timeout=60, stream=True, allow_redirects=True)
    if resp.status_code != 200:
        raise RuntimeError(f"tarball 下载失败: HTTP {resp.status_code}")
    tarball_path = os.path.join(tmp, "source.tar.gz")
    with open(tarball_path, "wb") as f:
        for chunk in resp.iter_content(chunk_size=8192):
            f.write(chunk)
    os.makedirs(workdir, exist_ok=True)
    with tarfile.open(tarball_path, "r:gz") as tar:
        members = tar.getmembers()
        if not members:
            raise RuntimeError("tarball 为空")
        for m in members:
            # 去掉第一层目录（owner-repo-hash/ 或 repo-branch/）
            parts = m.name.split("/", 1)
            if len(parts) < 2 or not parts[1]:
                continue
            m.name = parts[1]
            try:
                tar.extract(m, workdir)
            except Exception:
                pass
    _run_git(["init"], workdir)

def _download_gitcode_tarball(source_repo, workdir, tmp, token):
    """GitCode 不支持 tarball API，回退到 shallow clone + 删除 .git（避免 shallow push 问题）。

    2026-11-22：地址与鉴权方式都改走 runtime 契约——旧实现把
    `https://oauth2:{token}@gitcode.com/...` 写死平台，且把 token 拼进 URL
    （进程列表/日志/错误信息泄漏面）。现在：地址=clone_params(clone_url)，
    凭据=clone_auth_header → `git -c http.extraheader=` 注入。
    """
    if not token:
        raise RuntimeError("GitCode 模板需要 Bot token 拉取")
    from src.domains.runtime import public as _runtime_public

    url = _runtime_public.clone_url(source_repo)
    header = _runtime_public.clone_auth_header(source_repo, token)
    _run_git(["-c", f"http.extraheader=Authorization: {header}",
              "clone", "--quiet", "--depth", "1", url, workdir], tmp)
    shutil.rmtree(os.path.join(workdir, ".git"), ignore_errors=True)
    _run_git(["init"], workdir)

def push_template_to_repo(template: dict, owner_login: str, target_repo: str,
                          user_token: str, bot_token: str = "", provider: str = "gitcode",
                          user_id: str = "") -> bool:
    """将模板内容推送到新仓库。

    template: 配置中的模板对象（含 source / source_repo / id）
    target_repo: 目标仓库名（不含 owner）
    provider：目标平台（push 目标 URL 从平台卡派生）
    来源:
      - source="local" → 复制 examples/{template_id}/（或旧路径 configs/project-templates/{id} 兜底）到临时目录
      - source="git"   → git clone source_repo（用 bot_token 拉取）
    目标: 设置 remote 为个人新仓库并 git push -u origin HEAD:main。成功返回 True。
    """
    import os
    import shutil
    import tempfile
    from src.kernel.config import EXAMPLE_DIR, CONFIG_DIR

    if not user_token or not owner_login or not target_repo:
        raise RuntimeError("缺少用户 token 或目标仓库信息")
    if not template:
        raise RuntimeError("模板配置缺失")

    source = template.get("source", "git")
    # D105-17：目标平台卡 fail-closed 前置——未知/未配卡平台在拉取模板源之前
    # 即显式报错（不再事后才在 push 阶段暴露）
    from src.domains.runtime.public import get_platform_card
    _tgt_card = get_platform_card(provider)
    if _tgt_card is None:
        raise ValueError(
            f"未知 VCS 平台 '{provider}'：未配置平台卡，无法派生模板 push 目标。"
            f"请在平台卡（src/vcs/platform_card）中配置该平台（含 host）后重试"
        )
    _tgt_host = _tgt_card.host
    tmp = tempfile.mkdtemp(prefix="ptmpl_")
    try:
        if source == "local":
            template_id = template.get("id", "")
            src_dir = os.path.join(EXAMPLE_DIR, template_id)
            if not os.path.isdir(src_dir):
                # 旧路径兜底（兼容历史部署/自建镜像）
                src_dir = os.path.join(CONFIG_DIR, "project-templates", template_id)
            if not os.path.isdir(src_dir):
                raise RuntimeError(f"本地模板目录不存在: {src_dir}")
            workdir = os.path.join(tmp, "repo")
            shutil.copytree(src_dir, workdir)
            if not os.path.isdir(os.path.join(workdir, ".git")):
                _run_git(["init"], workdir)
        else:
            source_repo = template.get("source_repo", "")
            clone_url = template.get("clone_url", "")
            # D105-17：source_platform 缺省不再持有在代码里——缺失字段按 gitcode
            # 处理一次并 log.warning 留痕（存量模板数据迁移由回填完成）
            if "source_platform" in template:
                source_platform = template.get("source_platform") or "gitcode"
            else:
                source_platform = "gitcode"
                log.warning(
                    f"template '{template.get('id', '')}' missing source_platform; "
                    f"treating as gitcode once (backfill pending)")
            from src.domains.runtime.public import list_vcs_platforms
            if source_platform not in set(list_vcs_platforms()):
                raise ValueError(
                    f"模板源平台 '{source_platform}' 未支持（允许 gitcode/github）；"
                    "请在平台卡配置该平台或改用受支持的模板源")
            if not source_repo and not clone_url:
                raise RuntimeError("git 模板缺少 source_repo / clone_url 配置")
            workdir = os.path.join(tmp, "repo")
            # S90/D66：源平台分派表（github 源 gh tarball / gitcode shallow-clone）；
            # 表外平台按平台卡 download_base 派生通用归档下载器——卡未声明
            # download_base 时显式报错（不再静默回落 gitcode 下载器拉错平台）。
            _template_source_downloader(source_platform)(
                source_repo or clone_url, workdir, tmp, bot_token or user_token)

        # D105-17：目标 host 已从平台卡派生（见函数头部 fail-closed 前置校验）
        # D105-18：workflows 目录处理按【凭据能力】三态判定——
        # True=声明有权限（不删）；False=声明受限（删）；None=未声明 → 回退
        # D105-19：平台默认值从平台卡 workflows_write 位读（github 内置卡=False，
        # 其余 True）——provider=="github" 平台名硬分派消除，语义与存量逐字节等价。
        _wf = (credential_workflows_write(user_id, provider)
               if user_id else None)
        if _wf is None:
            from src.domains.runtime.public import get_platform_card as _gpc
            _pc_wf = _gpc(provider)
            _wf = (getattr(_pc_wf, "workflows_write", True) if _pc_wf is not None else True)
        _wf_denied = not _wf
        # push 目标 URL：任何分支都必须携带真实 token。
        # 历史 bug：受限分支（_wf_denied）曾拼 `https://***@host/...` —— 字面量
        # "***" 当用户名、没有密码，子进程里没有 credential helper/TTY，
        # git 直接报 `could not read Password for 'https://***@host':
        # No such device or address`，模板代码 100% 写不进去（线上 502：
        # 「仓库已创建但模板代码写入失败」）。
        # 删 .github/workflows 的目的正是「用受限 token 也能推上去」，所以
        # token 必须照常携带。
        target_url = f"https://oauth2:{user_token}@{_tgt_host}/{owner_login}/{target_repo}.git"
        if _wf_denied:
            # token 无 workflows 权限，push 含 .github/workflows 的仓库会被拒绝
            # ——怪癖以卡 workflows_write=False 声明（github 内置卡）。
            _wf_dir = os.path.join(workdir, ".github", "workflows")
            if os.path.isdir(_wf_dir):
                shutil.rmtree(_wf_dir, ignore_errors=True)
                log.info(f"Removed .github/workflows from template (GitHub App token lacks workflow permission)")
        if _run_git_has_origin(workdir):
            _run_git(["remote", "remove", "origin"], workdir)
        _run_git(["remote", "add", "origin", target_url], workdir)
        # D17 扫雷四轮：提交身份 email 的 host 从目标 URL 派生（跨平台正确，
        # github 仓库不再盖 gitcode 邮戳）
        _bot_host = _host_of_url(target_url)
        if not _bot_host:  # D105-17 fail-closed：host 解析失败不再伪装 gitcode
            # 不回显完整 URL（含 clone 凭据）
            raise RuntimeError("无法从模板 push 目标 URL 解析 host（fail-closed）")
        _run_git(["config", "user.email", f"devops-bot@{_bot_host}"], workdir)
        _run_git(["config", "user.name", "DevOps Bot"], workdir)
        _run_git(["add", "-A"], workdir)
        if _git_has_changes(workdir):
            _run_git(["commit", "-m", "Init from project template"], workdir)
        _run_git(["branch", "-M", "main"], workdir)
        # D43: push 统一走 _run_git（可被测试 mock），超时 180s
        _run_git(["push", "--force", "-u", "origin", "HEAD:main"], workdir, timeout=180)
        return True
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

def _run_git_has_origin(workdir: str) -> bool:
    try:
        out = _run_git(["remote"], workdir)
        return bool(out.strip())
    except RuntimeError:
        return False

def _git_has_changes(workdir: str) -> bool:
    try:
        out = _run_git(["status", "--porcelain"], workdir)
        return bool(out.strip())
    except RuntimeError:
        return True

def find_existing_webhook(api_token: str, owner: str, repo: str, webhook_url: str,
                          _retries: int = 3, api_base: str = API_BASE) -> int | None:
    """检查仓库是否已有指向 webhook_url 的 webhook，有则返回 webhook_id（429/5xx 退避重试）。

    FR-5.1: 空列表返回 None（不确定），调用方走新建分支；非空但无匹配也返回 None。
    D105-19：api_base 从平台卡派生（gitee_v5 方言家族通用），默认 gitcode.com。
    """
    if not webhook_url:
        return None
    import time
    from src.kernel.config import log as _log
    url = f"{api_base}/repos/{owner}/{urllib.parse.quote(repo, safe='')}/hooks"
    for i in range(_retries):
        try:
            hooks = _req("GET", url, api_token)
            if isinstance(hooks, list):
                if not hooks:
                    _log.info(f"find_existing_webhook: empty hooks list for {owner}/{repo} (token may lack hooks scope)")
                    return None  # 空列表=不确定
                for h in hooks:
                    if h.get("url") == webhook_url:
                        return h.get("id")
                return None  # 有 hooks 但无匹配
            return None
        except RuntimeError as e:
            code = _extract_http_code(e) or 0
            if code in _RETRYABLE_CODES or "too large" in str(e) or "429" in str(e):
                time.sleep(2 ** i)
                continue
            return None
    return None

def setup_webhook(api_token: str, owner: str, repo: str, webhook_url: str, webhook_password: str = "",
                  _retries: int = 3, api_base: str = API_BASE) -> int:
    """在指定仓库上创建 DevOps Bot webhook，返回 webhook_id（429/5xx 退避重试）。

    R20.16.2: webhook_password 为空时不再 fallback 到 get_repo_token_with_refresh，
    改为用 api_token（调用方传入的 token）做 password。
    R20.16.1: 调用方应显式传 webhook_password（路径 A 传 bot token，路径 B 传随机密钥）。
    """
    if not webhook_password:
        # R20.16.2: 不再 fallback 到 get_repo_token_with_refresh，用 api_token 做 password
        webhook_password = api_token
    import time
    url = f"{api_base}/repos/{owner}/{urllib.parse.quote(repo, safe='')}/hooks"
    body = {
        "url": webhook_url,
        "password": webhook_password,
        "push_events": True,
        "issues_events": True,
        "merge_requests_events": True,
        "note_events": True,
        "tag_push_events": True,
        "build_events": True,
        "project_events": True,
        "wiki_events": True,
        "release_events": True,
    }
    last = None
    for i in range(_retries):
        try:
            result = _req("POST", url, api_token, body)
            hook_id = result.get("id", 0)
            if not hook_id:
                raise RuntimeError(f"Created webhook but got no id: {json.dumps(result, ensure_ascii=False)[:200]}")
            return hook_id
        except RuntimeError as e:
            code = _extract_http_code(e) or 0
            if code in _RETRYABLE_CODES or "too large" in str(e) or "429" in str(e):
                log.warning(f"setup_webhook rate-limited (code={code}), retry {i + 1}/{_retries}: {e}")
                last = e
                time.sleep(2 ** i)
                continue
            raise
    raise RuntimeError(f"setup_webhook failed after retries: {last}")

def _get_managed_webhook(repo_key: str) -> int | None:
    """从 managed_repos 表获取 webhook_id（repo_key 带平台前缀）"""
    import sqlite3
    db_path = None
    try:
        from src.kernel.session_manager import db, db_lock
        from src.kernel.repo_key import to_repo_key
        repo_key = to_repo_key(repo_key)
        with db_lock:
            c = db.cursor()
            c.execute("SELECT webhook_id FROM managed_repos WHERE repo_key=?",
                      (repo_key,))
            row = c.fetchone()
            return row[0] if row and row[0] else None
    except:
        return None

def remove_webhook(api_token: str, owner: str, repo: str, webhook_id: int,
                   api_base: str = API_BASE) -> bool:
    """删除指定仓库上的 webhook。"""
    url = f"{api_base}/repos/{owner}/{urllib.parse.quote(repo, safe='')}/hooks/{webhook_id}"
    _req("DELETE", url, api_token)
    return True

def get_hosting_provider_config(provider: str) -> dict:
    """返回指定 provider 的托管配置（bot token / bot username / webhook 函数集 / webhook_url 后缀）。

    统一入口，host_repo 和 repair_host 都调此函数，消除散落的 provider 分流逻辑。
    返回 dict:
      - bot_token: 全局 bot token（路径 A 用）
      - bot_username: 全局 bot 用户名
      - find_webhook: 查找已有 webhook 的函数
      - setup_webhook: 创建 webhook 的函数
      - remove_webhook: 删除 webhook 的函数
      - webhook_url_suffix: webhook 路由后缀（"/webhook/github" 或 "/webhook/gitcode"）
      - db_token_field: managed_repos 中存 webhook token 的列名
      - db_bot_token_field: managed_repos 中存 bot token 的列名
    D105-19（平台化到位）：webhook 函数集按平台卡 webhook_dialect 分派
    （github_web→find_existing_github_webhook/setup_github_webhook/remove_github_webhook；
    gitee_v5→find_existing_webhook/setup_webhook/remove_webhook），api_base 从卡派生。
    其余全部从平台卡派生（webhook_url_suffix 走 _webhook_path_from_card，
    db 字段名走卡 token_storage_field/webhook_token_field——内置卡缺省派生式
    与存量列名逐字节一致）。未知/未配卡平台、webhook_dialect 未声明均
    fail-closed ValueError（不再按 gitcode 存量配置静默放行）。
    """
    from src.domains.mgmt.user.db import get_global_bot_token_for
    from src.domains.runtime.public import get_platform_card
    _pc = get_platform_card(provider)
    if _pc is None:
        raise ValueError(
            f"未知 VCS 平台 '{provider}'：未配置平台卡，无法派生托管配置。"
            f"请在平台卡（src/vcs/platform_card）中配置该平台后重试"
        )
    # D105-19（平台化到位，用户拍板）：webhook 三件套按卡 webhook_dialect 分派
    # （github_web=GitHub web-hooks 家族 / gitee_v5=v5 hooks 家族），api_base 从卡
    # 派生（不再硬编码 api.github.com / gitcode.com）；provider=="github" 特例分支
    # 消除——同为 github API 方言的 gitcode/gitee webhook 形态实为 v5，卡声明即分派。
    # 分派走 _WEBHOOK_FUNC_FAMILIES 注册表：新平台要么复用既有家族（卡声明
    # webhook_dialect + 自己的 api_base，零代码），要么注册新家族（表加一行 +
    # platform_card 枚举加一项，消费侧零改动）。函数以 functools.partial 绑定卡
    # api_base，指针签名（token/owner/repo/url）不变。
    import functools
    _wd = getattr(_pc, "webhook_dialect", "") or ""
    _family = _WEBHOOK_FUNC_FAMILIES.get(_wd)
    if _family is None:
        raise ValueError(
            f"平台 '{provider}' webhook 形态未支持（webhook_dialect={_wd!r}）："
            f"请在平台卡声明已实现的 webhook_dialect"
            f"（{'/'.join(_WEBHOOK_FUNC_FAMILIES)}）后再托管"
        )
    _ab = _pc.api_base
    _find_w, _setup_w, _remove_w = (
        functools.partial(_fn, api_base=_ab) for _fn in _family)
    return {
        "bot_token": get_global_bot_token_for(provider),
        "bot_username": resolve_bot_username(provider),
        "find_webhook": _find_w,
        "setup_webhook": _setup_w,
        "remove_webhook": _remove_w,
        # 卡派生回调后缀（卡缺失已被上方 fail-closed 拦截，fallback 恒不触发）
        "webhook_url_suffix": _webhook_path_from_card(provider, f"/webhook/{provider}"),
        # D105-17：db 字段名从卡派生（内置卡缺省=存量列名）
        "db_token_field": _pc.webhook_token_field or f"{provider}_webhook_token",
        "db_bot_token_field": _pc.token_storage_field or f"bot_{provider}_token",
        # B 批（2026-11-13）：建 webhook 的鉴权参数按方言派生——Gitee 系用
        # webhook_password（路径 B=secret / 路径 A=token），GitHub 系用
        # webhook_secret（仅路径 B）。调用方零平台分支。
        "setup_webhook_kwargs": _setup_kwargs_fn(_wd),
    }


def _setup_kwargs_fn(dialect: str):
    """返回 setup_webhook 鉴权参数构造器（入参 secret/wh_token/use_path_b）。"""
    def _github_style(secret: str, _wh_token: str, use_path_b: bool) -> dict:
        return {"webhook_secret": secret} if use_path_b else {}

    def _gitee_style(secret: str, wh_token: str, use_path_b: bool) -> dict:
        return {"webhook_password": secret if use_path_b else wh_token}

    if dialect == "github_web":
        return _github_style
    return _gitee_style


_GITHUB_API_BASE = "https://api.github.com"

def remove_github_webhook(api_token: str, owner: str, repo: str, webhook_id: int,
                          api_base: str = _GITHUB_API_BASE) -> bool:
    """删除 GitHub 仓库上的 webhook。"""
    url = f"{api_base}/repos/{owner}/{urllib.parse.quote(repo, safe='')}/hooks/{webhook_id}"
    _github_req("DELETE", url, api_token)
    return True

def remove_github_collaborator(api_token: str, owner: str, repo: str, username: str) -> bool:
    """移除 GitHub 仓库协作者（DELETE /repos/{owner}/{repo}/collaborators/{username}）。"""
    if not username or not api_token:
        return False
    url = f"{_GITHUB_API_BASE}/repos/{owner}/{urllib.parse.quote(repo, safe='')}/collaborators/{urllib.parse.quote(username, safe='')}"
    try:
        _github_req("DELETE", url, api_token)
        return True
    except RuntimeError as e:
        log.warning(f"remove_github_collaborator failed: {e}")
        return False

def _github_check_installation(api_token: str) -> str:
    """检测 GitHub App installation 状态，返回用户可操作的错误提示。"""
    try:
        inst_data = _github_req("GET", f"{_GITHUB_API_BASE}/user/installations?per_page=1", api_token)
        inst_count = inst_data.get("total_count", 0) if isinstance(inst_data, dict) else 0
    except Exception:
        inst_count = -1

    if inst_count == 0:
        return (
            "GitHub App 未安装到您的账户，无法创建仓库。"
            "请前往 https://github.com/settings/apps → 点击您的 App → Install App → 安装到个人账户，"
            "然后重新登录。"
        )
    return (
        "GitHub App 已安装但 Administration 权限不足。"
        "请在 GitHub App 设置页 Repository permissions → Administration 设为 Read and write，"
        "然后撤销授权（https://github.com/settings/applications）并重新登录。"
    )

def _github_req(method: str, url: str, token: str = "", body: dict | None = None) -> dict:
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/vnd.github+json",
        "User-Agent": "Grape-DevOps",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    data = json.dumps(body).encode() if body else None
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=upstream_timeout_seconds()) as resp:
            raw = resp.read().decode()
            if raw.strip():
                return json.loads(raw)
            return {}
    except urllib.error.HTTPError as e:
        body = e.read().decode(errors="replace")
        raise UpstreamAPIError(method, url, e.code, body, provider="GitHub")
    except urllib.error.URLError as e:
        raise RuntimeError(f"Cannot reach GitHub API: {e.reason}")

def fetch_github_repos_with_role(api_token: str) -> list[dict]:
    """获取用户的 GitHub 仓库列表。
    返回：[{"full_name": "owner/repo", "owner": "owner", "name": "repo", "is_admin": bool}, ...]
    """
    if not api_token:
        raise RuntimeError("GitHub API Token 未配置")
    repos = []
    page = 1
    while True:
        url = f"{_GITHUB_API_BASE}/user/repos?per_page=100&page={page}&sort=full_name"
        data = _github_req("GET", url, api_token)
        items = data if isinstance(data, list) else []
        if not items:
            break
        for r in items:
            full_name = r.get("full_name", "")
            if not full_name or "/" not in full_name:
                continue
            perm = r.get("permissions") or {}
            is_admin = bool(perm.get("admin")) if isinstance(perm, dict) else False
            repos.append({
                "full_name": full_name,
                "owner": full_name.split("/", 1)[0],
                "name": full_name.split("/", 1)[1],
                "is_admin": is_admin,
                "created_at": r.get("created_at", "") or "",
            })
        if len(items) < 100:
            break
        page += 1
    return repos

def find_existing_github_webhook(api_token: str, owner: str, repo: str, webhook_url: str,
                                 api_base: str = _GITHUB_API_BASE) -> int | None:
    """检查 GitHub 仓库是否已有指向 webhook_url 的 webhook。

    FR-5.1: 空列表返回 None（不确定），加 log 可观测。
    D105-19：api_base 从平台卡派生（github_web 方言家族通用），默认 github.com。
    """
    if not webhook_url:
        return None
    from src.kernel.config import log as _log
    url = f"{api_base}/repos/{owner}/{urllib.parse.quote(repo, safe='')}/hooks"
    try:
        hooks = _github_req("GET", url, api_token)
        if isinstance(hooks, list):
            if not hooks:
                _log.info(f"find_existing_github_webhook: empty hooks list for {owner}/{repo} (token may lack hooks scope)")
                return None
            for h in hooks:
                if h.get("config", {}).get("url") == webhook_url:
                    return h.get("id")
    except RuntimeError:
        pass
    return None

def setup_github_webhook(api_token: str, owner: str, repo: str, webhook_url: str, webhook_secret: str = "",
                         api_base: str = _GITHUB_API_BASE) -> int:
    """在 GitHub 仓库上创建 webhook，返回 webhook_id。"""
    if not webhook_secret:
        webhook_secret = api_token
    url = f"{api_base}/repos/{owner}/{urllib.parse.quote(repo, safe='')}/hooks"
    body = {
        "name": "web",
        "active": True,
        "events": ["push", "issues", "pull_request", "issue_comment", "create", "release"],
        "config": {
            "url": webhook_url,
            "content_type": "json",
            "secret": webhook_secret,
            "insecure_ssl": "0",
        },
    }
    result = _github_req("POST", url, api_token, body)
    hook_id = result.get("id", 0)
    if not hook_id:
        raise RuntimeError(f"Created GitHub webhook but got no id: {json.dumps(result, ensure_ascii=False)[:200]}")
    return hook_id


# D105-19：webhook 三件套实现家族注册表（键=平台卡 webhook_dialect）。
# 新增家族：实现 (find, setup, remove) 三件套（api_base 关键字参数）→ 表加一行
# → platform_card._VALID_WEBHOOK_DIALECTS 加一项 → 新平台卡声明即启用。
_WEBHOOK_FUNC_FAMILIES: dict[str, tuple] = {
    "github_web": (find_existing_github_webhook, setup_github_webhook, remove_github_webhook),
    "gitee_v5": (find_existing_webhook, setup_webhook, remove_webhook),
}


def _github_token_scopes(api_token: str) -> str | None:
    """经典 PAT 的 x-oauth-scopes；细粒度 PAT / 读取失败返回 None。

    细粒度 PAT 不返回该响应头（权限由细粒度权限集控制，无法从这里自查）。
    """
    req = urllib.request.Request(
        f"{_GITHUB_API_BASE}/user",
        headers={
            "Authorization": f"Bearer {api_token}",
            "Accept": "application/vnd.github+json",
            "User-Agent": "Grape-DevOps",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return resp.headers.get("x-oauth-scopes")
    except Exception:
        return None


def probe_github_repo_create(api_token: str, owner: str = "", private: bool = True
                             ) -> "tuple[bool | None, str]":
    """无副作用探测 GitHub token 是否有建仓权限。

    返回 (结论, 说明)：False=确定无权限；True=可建仓；None=无法判定（调用方不应阻断）。

    - 经典 PAT：读 x-oauth-scopes；私有仓需 repo，公开仓 repo/public_repo 均可。
    - 细粒度 PAT：GitHub 没有权限自查端点。用一次「非法仓库名」的 POST 触发权限层：
      403 → 无权限；422 → 有权限（请求体校验在权限之后，不会真正建仓）。
    """
    if not api_token:
        return False, "未配置 GitHub Token"
    scopes = _github_token_scopes(api_token)
    if scopes is not None:
        if "repo" in scopes:
            return True, ""
        if "public_repo" in scopes and not private:
            return True, ""
        return False, (
            "当前 GitHub 经典 PAT 缺少建仓权限：私有仓库需要 repo scope，"
            "公开仓库至少需要 public_repo scope。请更新 Token 后重试"
        )

    login = get_user_github_login(api_token)
    target = (owner or "").strip()
    if target and login and target.lower() == login.lower():
        target = ""
    endpoint = (
        f"{_GITHUB_API_BASE}/orgs/{urllib.parse.quote(target, safe='')}/repos"
        if target else f"{_GITHUB_API_BASE}/user/repos"
    )
    try:
        # 名字含空格必定非法：有权限时 422，无权限时权限层先返回 403；不会产生仓库
        _github_req("POST", endpoint, api_token,
                    {"name": "invalid repo name with spaces", "private": True,
                     "auto_init": False})
    except RuntimeError as e:
        msg = str(e)
        if "403" in msg:
            return False, (
                "当前 GitHub 凭据没有创建仓库权限：细粒度 PAT 需 "
                "Repository creation（或 Administration）Read and write，"
                "且 Repository access 选 All repositories；经典 PAT 需 repo scope。"
                "请更新 Token 后重试，或先在 GitHub 手动建仓再关联"
            )
        if "422" in msg:
            return True, ""
        return None, f"GitHub 权限探测失败（不阻断创建）：{msg[:160]}"
    return None, "GitHub 权限探测未得到预期拒绝（不阻断创建）"


def get_user_github_login_status(api_token: str) -> tuple[str, int]:
    """Issue #3563: (login, status) 变体——透出上游状态码，不抛契约不变（同 GitCode 版）。"""
    if not api_token:
        return "", -1
    try:
        data = _github_req("GET", f"{_GITHUB_API_BASE}/user", api_token)
        login = data.get("login", "") or ""
        log.info(f"get_user_github_login: ok (login={login})")
        return login, 0
    except UpstreamAPIError as e:
        log.warning(f"get_user_github_login failed (status={e.status_code}): {e.message[:120]}")
        return "", e.status_code
    except RuntimeError as e:
        log.warning(f"get_user_github_login failed: {e}")
        return "", -1

def get_user_github_login(api_token: str) -> str:
    """获取当前 token 的 GitHub 登录用户名。

    Issue #3563：变薄包装——失败返回空串的既有契约不变；需要上游状态码的
    调用方改用 get_user_github_login_status。
    """
    login, _status = get_user_github_login_status(api_token)
    return login

def get_github_repo_permissions(api_token: str, owner: str, repo: str) -> dict | None:
    """获取 token 持有者对指定仓库的权限（GET /repos/{owner}/{repo}）。

    返回 permissions dict：{admin, maintain, push, triage, pull}。
    只需 pull 权限即可调用——bot 用自己的 token 查自己对该仓库的权限。
    失败返回 None。
    """
    if not api_token:
        return None
    url = f"{_GITHUB_API_BASE}/repos/{owner}/{urllib.parse.quote(repo, safe='')}"
    try:
        data = _github_req("GET", url, api_token)
        return data.get("permissions") or {}
    except RuntimeError as e:
        log.warning(f"get_github_repo_permissions failed: {e}")
        return None

def add_github_collaborator(api_token: str, owner: str, repo: str, username: str, permission: str = "push") -> bool:
    """将用户添加为 GitHub 仓库协作者（PUT /repos/{owner}/{repo}/collaborators/{username}）。
    
    GitHub 会自动发送邀请，被邀请方需 accept 才生效。
    permission: pull / push / admin（OAuth App token 不支持 maintain/triage）
    默认 push（可读写代码），admin 仓库 owner 可用 maintain/admin。
    已是成员时返回 True（幂等）。
    """
    if not username or not api_token:
        return False
    url = f"{_GITHUB_API_BASE}/repos/{owner}/{urllib.parse.quote(repo, safe='')}/collaborators/{urllib.parse.quote(username, safe='')}"
    try:
        _github_req("PUT", url, api_token, {"permission": permission})
        return True
    except RuntimeError as e:
        # 403 = 已是 collaborator（GitHub 返回 403 而非 200）
        if "403" in str(e):
            return True
        log.warning(f"add_github_collaborator failed: {e}")
        return False

def accept_github_invite(api_token: str, owner: str, repo: str) -> bool:
    """接受 GitHub 仓库邀请（PATCH /user/repository_invitations）。
    
    GitHub 的邀请 accept 需要用被邀请方的 token。
    先列出邀请找到 invitation_id，再 accept。
    """
    if not api_token:
        return False
    try:
        invites = _github_req("GET", f"{_GITHUB_API_BASE}/user/repository_invitations?per_page=50", api_token)
        if not isinstance(invites, list):
            return False
        target = f"{owner}/{repo}".lower()
        for inv in invites:
            inv_repo = (inv.get("repository") or {}).get("full_name", "").lower()
            if inv_repo == target:
                inv_id = inv.get("id")
                if inv_id:
                    _github_req("PATCH", f"{_GITHUB_API_BASE}/user/repository_invitations/{inv_id}", api_token, {})
                    log.info(f"accept_github_invite: accepted {target} (id={inv_id})")
                    return True
        log.info(f"accept_github_invite: no matching invite for {target} (total={len(invites)})")
        return False
    except RuntimeError as e:
        log.warning(f"accept_github_invite failed: {e}")
        return False

def get_github_collaborator_permission(api_token: str, owner: str, repo: str, username: str) -> str:
    """查看 GitHub 仓库成员权限（GET /repos/{owner}/{repo}/collaborators/{username}/permission）。
    
    返回 permission role_name（如 admin/maintain/push），失败返回空字符串。
    注意：需要 admin 权限的 token（通常用仓库 owner 的 token，不是 bot 自己的 token）。
    """
    if not username or not api_token:
        return ""
    url = f"{_GITHUB_API_BASE}/repos/{owner}/{urllib.parse.quote(repo, safe='')}/collaborators/{urllib.parse.quote(username, safe='')}/permission"
    try:
        data = _github_req("GET", url, api_token)
        perm = data.get("permission") or ""
        role = data.get("role_name") or ""
        return role or perm or ""
    except RuntimeError as e:
        log.warning(f"get_github_collaborator_permission failed: {e}")
        return ""

def check_github_is_collaborator(api_token: str, owner: str, repo: str, username: str) -> bool:
    """检查用户是否已是仓库 collaborator（GET /repos/{owner}/{repo}/collaborators/{username}）。
    
    204 = 是 collaborator，404 = 不是。
    需要 push 权限的 token（仓库 owner 的 token 即可）。
    """
    if not username or not api_token:
        return False
    url = f"{_GITHUB_API_BASE}/repos/{owner}/{urllib.parse.quote(repo, safe='')}/collaborators/{urllib.parse.quote(username, safe='')}"
    try:
        _github_req("GET", url, api_token)
        return True  # 200 = 是 collaborator
    except RuntimeError as e:
        if "404" in str(e):
            return False  # 不是 collaborator
        # 204 也可能被 _github_req 当作异常（空 body）
        if "204" in str(e) or "No Content" in str(e):
            return True
        log.warning(f"check_github_is_collaborator failed: {e}")
        return False

def create_github_repo(api_token: str, name: str, description: str = "", private: bool = False,
                       owner: str = "", path: str = "", auto_init: bool = False,
                       default_branch: str = "") -> dict:
    """创建空仓库（个人空间 POST /user/repos，指定 owner 时 POST /orgs/{owner}/repos）。

    GitHub 的仓库路径恒等于仓库名，path 参数仅为与服务端统一签名而保留（忽略）。
    """
    if not api_token:
        raise RuntimeError("GitHub API Token 未配置")
    owner = (owner or "").strip()
    target_branch = (default_branch or "").strip()
    # 所选 owner 是当前 token 用户本人时走个人空间：GitHub 的
    # POST /orgs/{login}/repos 对个人账号恒 404（创建组织弹窗默认选中的就是
    # 「个人」空间，曾导致 GitHub 组织仓库 provisioning 直接失败）。
    if owner and get_user_github_login(api_token).strip().lower() == owner.lower():
        owner = ""
    # GitHub 建仓接口不支持 default_branch；自定义默认分支必须先用 auto_init
    # 生成首个分支，再调分支改名接口（改名默认分支即默认分支改名）。
    if target_branch and target_branch != "main":
        auto_init = True
    body = {
        "name": name,
        "description": description,
        "private": bool(private),
        "auto_init": bool(auto_init),
    }
    endpoint = (f"{_GITHUB_API_BASE}/orgs/{urllib.parse.quote(owner, safe='')}/repos"
                if owner else f"{_GITHUB_API_BASE}/user/repos")
    try:
        result = _github_req("POST", endpoint, api_token, body)
    except RuntimeError as e:
        if "403" in str(e) and "Resource not accessible by integration" in str(e):
            # 检测 installation 状态，区分"未安装"和"权限不足"
            _hint = _github_check_installation(api_token)
            raise RuntimeError(_hint) from e
        if "403" in str(e) and "Resource not accessible by personal access token" in str(e):
            # 细粒度 PAT 权限模型：建仓需要 Administration=write（组织仓库还需授予该组织）
            raise RuntimeError(
                "当前 GitHub 凭据是细粒度 PAT，缺少创建仓库权限（需 Administration: Write；"
                "在组织下建仓还需为该组织授权）。请到设置页更新 GitHub Token 后重试；"
                "或先在 GitHub 手动创建仓库，再用组织页「添加关联仓库」绑定"
            ) from e
        raise
    full_name = result.get("full_name", "")
    if not full_name:
        raise RuntimeError(f"创建仓库成功但未返回仓库信息: {json.dumps(result, ensure_ascii=False)[:200]}")
    if target_branch and target_branch != "main":
        initial_branch = str(result.get("default_branch") or "main").strip() or "main"
        if initial_branch != target_branch:
            _owner, _repo = full_name.split("/", 1) if "/" in full_name else (owner, name)
            try:
                _repo_url = (f"{_GITHUB_API_BASE}/repos/"
                             f"{urllib.parse.quote(_owner, safe='')}/"
                             f"{urllib.parse.quote(_repo, safe='')}")
                _github_req(
                    "POST",
                    f"{_repo_url}/branches/"
                    f"{urllib.parse.quote(initial_branch, safe='')}/rename",
                    api_token,
                    {"new_name": target_branch},
                )
                # GitHub 分支改名不会自动改默认分支，需再 PATCH 一次。
                _github_req("PATCH", _repo_url, api_token,
                            {"default_branch": target_branch})
                result["default_branch"] = target_branch
                log.info(
                    f"create_github_repo: default branch {initial_branch} -> "
                    f"{target_branch} for {full_name}")
            except Exception as exc:  # noqa: BLE001 - 建仓成功但改名失败保留仓库
                log.warning(
                    f"create_github_repo: rename default branch failed "
                    f"{full_name} {initial_branch}->{target_branch}: {exc}")
    return result

def credential_workflows_write(user_id: str, provider: str) -> "bool | None":
    """D105-18 委托：见 token_resolver.credential_workflows_write。"""
    from src.kernel.token_resolver import credential_workflows_write as _cww
    return _cww(user_id, provider)

def _host_of_url(url: str) -> str:
    """从 git remote URL 提 host（含 user:token@ 形态）。

    D105-17 fail-closed：解析失败/空 host 返回空串，由调用方显式报错——
    不再回退 "gitcode.com"（存量兜底会把非 gitcode 平台的失败静默伪装成
    gitcode 语义）。
    """
    try:
        from urllib.parse import urlparse
        return (urlparse(url).hostname or "").lower()
    except Exception:
        return ""

def _webhook_path_from_card(provider: str, fallback: str) -> str:
    """D12/W3：webhook 回调后缀优先取平台卡 webhook_path，无卡用历史默认。"""
    try:
        from src.domains.runtime.public import get_platform_card
        card = get_platform_card(provider)
        if card is not None:
            return card.webhook_path
    except Exception:
        pass
    return fallback

def resolve_bot_username(provider: str) -> str:
    """D15/S19：bot 身份统一来源链（抽象解，无平台分支）。

    S88/D64：来源链数据驱动（GLOBAL_BOT_USERNAME_SOURCES）——
    github=独立单值源 / gitcode=pool / 其余已知平台=仅卡。
    D105-17 fail-closed：未知/未配卡平台不再兜底 gitcode pool，显式
    ValueError（消息含平台名 + 配卡指引）。已知平台来源链与卡均空时
    返回 ""（= 该平台无 bot 协作能力，认领流按无 bot 降级，存量语义）。
    """
    from src.domains.mgmt.user.db import GLOBAL_BOT_USERNAME_SOURCES
    # D110：平台行是一级来源（一平台一个 bot），先于历史单值源/pool/卡；
    # 否则 gitee 这类"来源链只认 meta 单值键"的平台会绕过管理面写的名字。
    try:
        from src.domains.mgmt.user.db import _platform_bot_row

        _plat_name = (_platform_bot_row(provider) or {}).get("username") or ""
        if _plat_name:
            return _plat_name
    except Exception:
        pass
    _src = GLOBAL_BOT_USERNAME_SOURCES.get(provider)
    if _src is None and provider not in _KNOWN_VCS_PROVIDERS():
        raise ValueError(
            f"未知 VCS 平台 '{provider}'：无 bot 身份来源且未配置平台卡。"
            f"请在平台卡（src/vcs/platform_card）中配置该平台（含 bot_username）后重试"
        )
    if _src is not None:
        # S19 修绿：来源链值过滤空串——未配置的单值源（如 gitee 专属
        # global_bot_gitee_username）不遮蔽卡 bot_identity，回落卡（S88
        # 「未配置时 gitee 仅走卡」口径与 test_mine7 行为锁一致）。
        _us = [u for u in _src() if u]
        if _us:
            return _us[0]
    return _card_bot_username(provider)

def _KNOWN_VCS_PROVIDERS():
    """D109 判据收敛：已知平台集合 = 能力视图（存量三平台 ∪ 平台卡 ∪ 表内平台）。"""
    from src.domains.platform.public import known_providers
    return known_providers()

def _card_bot_username(provider: str) -> str:
    try:
        from src.domains.runtime.public import get_platform_card
        card = get_platform_card(provider)
        if card is not None:
            return card.bot_username
    except Exception:
        pass
    return ""

"""GitCode 存量实现模块——平台通用能力请走 src/token_resolver 适配器端口。

D105-17 治理定位：本模块只承载 GitCode 存量 API 实现（v5 家族请求、邀请制
协作、webhook 增删查）与 GitHub 专属方言函数（github webhook 函数指针表）。
跨平台通用能力（建仓/列仓/权限/webhook 端口）一律走 src/token_resolver 的
VCSProvider 适配器端口（create_provider 工厂分派）。未知/未配卡平台
fail-closed：不再静默回退 gitcode（_host_of_url / resolve_bot_username /
get_hosting_provider_config / push_template 目标卡均为显式报错或卡派生）。
"""

# S90/D66：模板源平台 tarball 下载器分派表（源平台=模板来源，非目标仓库平台）。
# 表内为「需要平台专属取件形态」的存量实现；表外平台一律按平台卡派生
# （见 _template_source_downloader），不再回落到 gitcode 下载器。
_TEMPLATE_SOURCE_DOWNLOADERS = {
    "github": lambda source_repo, workdir, tmp, token: _download_and_extract_tarball(
        f"https://api.github.com/repos/{source_repo}/tarball", workdir, tmp,
        headers=({"Authorization": f"token {os.environ.get('GITHUB_TOKEN', '')}"}
                 if os.environ.get("GITHUB_TOKEN") else {})),
    "gitcode": _download_gitcode_tarball,
}

# 卡驱动通用归档下载的默认分支（模板源未声明分支时）
_TEMPLATE_SOURCE_DEFAULT_REF = "main"


def _download_card_archive(card, source_repo: str, workdir: str, tmp: str,
                           token: str, ref: str = "") -> None:
    """按平台卡 download_base 下载模板源归档（通用 tarball，git init 收口）。

    路径形态按卡方言派生（方言是卡字段，非平台名）：gitlab 方言
    `{base}/{o}/{r}/-/archive/{ref}/{r}-{ref}.tar.gz`；github/v5 方言
    `{base}/{o}/{r}/archive/refs/heads/{ref}.tar.gz`（raw 系基址同形）。
    鉴权按卡 api_auth 派生（private_token → PRIVATE-TOKEN，其余 Bearer）。
    """
    base = (getattr(card, "download_base", "") or "").rstrip("/")
    if not base:
        raise RuntimeError(
            f"模板源平台 '{card.provider}' 的平台卡未声明 download_base："
            "无法下载模板源归档（改为在 configs/platforms/<provider>.json "
            "配置 download_base，或改用 source='local'/有下载器实现的源平台）")
    _ref = ref or _TEMPLATE_SOURCE_DEFAULT_REF
    _path = (source_repo or "").strip().strip("/")
    _owner, _, _repo = _path.partition("/")
    if not _owner or not _repo:
        raise RuntimeError(f"模板源仓库标识非法（需 owner/repo）: {source_repo!r}")
    if getattr(card, "dialect", "") == "gitlab":
        url = f"{base}/{_owner}/{_repo}/-/archive/{_ref}/{_repo}-{_ref}.tar.gz"
    else:
        url = f"{base}/{_owner}/{_repo}/archive/refs/heads/{_ref}.tar.gz"
    headers: dict = {}
    if token:
        if getattr(card, "api_auth", "") == "private_token":
            headers = {"PRIVATE-TOKEN": token}
        else:
            headers = {"Authorization": f"Bearer {token}"}
    _download_and_extract_tarball(url, workdir, tmp, headers=headers or None)


def _template_source_downloader(source_platform: str, ref: str = ""):
    """解析模板源下载器：分派表优先，表外按平台卡 download_base 派生。

    无卡/卡未声明 download_base → 显式可读报错（fail-closed：不再静默用
    gitcode 下载器拉 gitee/gitlab 源模板）。返回签名
    (source_repo, workdir, tmp, token) -> None 的可调用对象。
    """
    _impl = _TEMPLATE_SOURCE_DOWNLOADERS.get(source_platform)
    if _impl is not None:
        return _impl
    from src.domains.runtime.public import get_platform_card
    card = get_platform_card(source_platform)
    if card is None:
        raise ValueError(
            f"模板源平台 '{source_platform}' 无平台卡：无法派生模板下载方式。"
            "请在 configs/platforms/ 配置该平台卡（含 download_base）后重试")
    return lambda source_repo, workdir, tmp, token: _download_card_archive(
        card, source_repo, workdir, tmp, token, ref=ref)


# ── 协作者增删家族（B 批 2026-11-13）：与 webhook 三件套同一分派轴 ──
# 路由层不再写 `if provider == "github": add_github_collaborator / else
# add_collaborator`；新平台卡声明已支持方言即复用家族，零代码接入。
# 按名字注册（惰性解析）：注册表持有函数对象会让存量测试的 patch（模块属性）
# 失效——patch 只换模块属性，家族表里的旧引用仍是真函数。用 globals()[name]
# 在调用期解析，测试接缝始终有效。
_COLLAB_FUNC_NAMES = {
    "github_web": ("add_github_collaborator", "remove_github_collaborator"),
    "gitee_v5": ("add_collaborator", "remove_collaborator"),
}


def get_collaborator_funcs(provider: str):
    """返回该平台的 (add, remove) 协作者函数对（卡驱动，未支持方言 fail-closed）。"""
    from src.domains.runtime.public import get_platform_card
    _pc = get_platform_card(provider)
    if _pc is None:
        raise ValueError(
            f"未知 VCS 平台 '{provider}'：未配置平台卡，无法派生协作者能力。")
    _wd = getattr(_pc, "webhook_dialect", "") or ""
    _names = _COLLAB_FUNC_NAMES.get(_wd)
    if _names is None:
        raise ValueError(
            f"平台 '{provider}' 协作者形态未支持（webhook_dialect={_wd!r}）："
            f"请声明已实现的方言（{'/'.join(_COLLAB_FUNC_NAMES)}）")
    return tuple(globals()[n] for n in _names)


# ── bot 权限判定 / 邀请接受家族（B 批续，2026-11-13）：与协作者家族同轴分派 ──
def _gh_bot_is_maintainer(bot_token: str, owner: str, repo: str, bot_username: str = "") -> bool:
    _perm = get_github_repo_permissions(bot_token, owner, repo)
    return bool(_perm and (_perm.get("admin") or _perm.get("maintain") or _perm.get("push")))


def _gc_bot_is_maintainer(bot_token: str, owner: str, repo: str, bot_username: str = "") -> bool:
    perm = get_member_permission(bot_token, owner, repo, bot_username)
    try:
        return bool(perm and int(perm.get("access_level") or 0) >= 40)
    except (TypeError, ValueError):
        return False


_BOT_MAINTAINER_NAMES = {
    "github_web": "_gh_bot_is_maintainer",
    "gitee_v5": "_gc_bot_is_maintainer",
}
_INVITE_PERMISSION = {"github_web": "push", "gitee_v5": "maintainer"}

# agent git 凭据的仓 token 读取器登记（B 批：repos.py 不再按平台名分派）
_REPO_TOKEN_GETTERS = {
    # 键必须是 **provider**，不能用 webhook_dialect：gitee_v5 被 gitcode 与 gitee
    # 共用，按方言登记会让 gitee 仓走 GitCode 的读取器（读 gitcode 凭据 → 随 Basic
    # 认证发往 gitee.com，跨平台凭据错发；QA C1 2026-11-13 实锤）。
    # 未登记的 provider（如 gitee/gitlab）落 _repo_platform_token 的端口分支，
    # 走 get_user_token_for / TokenResolver，provider 语义天然正确。
    "github": {
        "bot": ("src.domains.mgmt.user.db", "get_github_token"),
        "user": ("src.api.deps", "get_user_github_token_with_refresh"),
        "use_provider_key": True,
    },
    "gitcode": {
        "bot": ("src.domains.mgmt.user.db", "get_gitcode_token"),
        "user": ("src.api.deps", "get_user_token_with_refresh"),
        "use_provider_key": False,
    },
}
_ACCEPT_NAMES = {"github_web": "accept_github_invite", "gitee_v5": "accept_invite_for_repo"}


def _dialect_of(provider: str) -> str:
    from src.domains.runtime.public import get_platform_card
    _pc = get_platform_card(provider)
    if _pc is None:
        raise ValueError(
            f"未知 VCS 平台 '{provider}'：未配置平台卡，无法派生协作者能力。")
    return getattr(_pc, "webhook_dialect", "") or ""


def repo_token_getters(provider: str) -> dict:
    """agent git 凭据的 bot/user token 读取器（**按 provider** 登记，未登记=走端口）。

    键是 provider 而不是 webhook_dialect：方言表达的是 webhook/协作者形态家族，
    不表达「token 落在哪个平台的库里」；gitee_v5 被 gitcode 与 gitee 共用，按方言
    分发会把 gitee 仓的凭据读成 gitcode 的（跨平台凭据错发）。
    use_provider_key 表示读仓 token 时 repo_key 是否带平台前缀（GitHub 系带，
    GitCode 存量数据无前缀）。
    """
    return dict(_REPO_TOKEN_GETTERS.get(provider, {}))


def bot_is_maintainer(provider: str, bot_token: str, owner: str, repo: str,
                      bot_username: str = "") -> bool:
    """bot 是否已是该仓库 maintainer 及以上（方言分派，调用方零平台分支）。"""
    _wd = _dialect_of(provider)
    _name = _BOT_MAINTAINER_NAMES.get(_wd)
    if _name is None:
        raise ValueError(f"平台 '{provider}' bot 权限判定未支持（webhook_dialect={_wd!r}）")
    return globals()[_name](bot_token, owner, repo, bot_username)


def invite_permission_for(provider: str) -> str:
    """邀请协作者时的 permission 值（方言派生：github 系=push / gitee 系=maintainer）。"""
    return _INVITE_PERMISSION.get(_dialect_of(provider), "push")


def vcs_user_login(provider: str, token: str) -> str:
    """token 持有者在该平台的登录名（卡驱动 /user；空串=无法确认）。"""
    from src.kernel.token_resolver import create_provider
    info = create_provider(provider).verify_token(token)
    return str((info or {}).get("login", "") or "") if isinstance(info, dict) else ""


def vcs_user_permission(provider: str, token: str, owner: str, repo: str, login: str) -> dict:
    """归一化某用户在该仓库的权限：{is_maintainer, role, access_level}（方言分派）。"""
    _wd = _dialect_of(provider)
    if _wd == "github_web":
        role = (get_github_collaborator_permission(token, owner, repo, login) or "").lower()
        return {"is_maintainer": role in ("admin", "maintain"), "role": role, "access_level": 0}
    data = get_member_permission(token, owner, repo, login) or {}
    try:
        lvl = int(data.get("access_level", 0) or 0)
    except (TypeError, ValueError):
        lvl = 0
    ok = lvl >= 40 or str(data.get("permission", "") or "").lower() == "admin"
    return {"is_maintainer": ok, "role": str(data.get("role_name", "") or ""), "access_level": lvl}


def invite_bot_collaborator(provider: str, api_token: str, bot_token: str,
                            owner: str, repo: str, bot_username: str) -> bool:
    """邀请 bot 为协作者并接受邀请（方言分派：permission 值 + accept 家族）。"""
    _wd = _dialect_of(provider)
    _add, _ = get_collaborator_funcs(provider)
    _perm = _INVITE_PERMISSION.get(_wd, "push")
    try:
        _res = _add(api_token, owner, repo, bot_username, _perm)
    except TypeError:
        _res = _add(api_token, owner, repo, bot_username)
    ok = _res[0] if isinstance(_res, tuple) else bool(_res)
    if not ok:
        return False
    _accept_name = _ACCEPT_NAMES.get(_wd)
    if _accept_name is not None:
        try:
            globals()[_accept_name](bot_token, owner, repo)
        except Exception as _ae:
            log.warning(f"invite_bot_collaborator: accept failed ({provider}): {_ae}")
    return True
