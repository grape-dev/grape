"""GitCode 仓库托管管理：获取管理员仓库、创建/删除 webhook"""
import json, urllib.request, urllib.error, urllib.parse
from src.config import log

API_BASE = "https://api.gitcode.com/api/v5"


def _req(method: str, url: str, token: str = "", body: dict | None = None) -> dict:
    headers = {"Content-Type": "application/json", "Accept": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    data = json.dumps(body).encode() if body else None
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            raw = resp.read().decode()
            if raw.strip():
                return json.loads(raw)
            return {}
    except urllib.error.HTTPError as e:
        body = e.read().decode(errors="replace")[:500]
        raise RuntimeError(f"GitCode API {method} {url} failed ({e.code}): {body}")
    except urllib.error.URLError as e:
        raise RuntimeError(f"Cannot reach GitCode API: {e.reason}")


def _check_collaborator_admin(api_token: str, owner: str, repo: str, username: str) -> bool:
    """调 /collaborators/{username}/permission 接口精确判断管理员权限"""
    if not username:
        return False
    # 项目所有者直接有完全权限
    if username == owner:
        return True
    url = f"{API_BASE}/repos/{owner}/{urllib.parse.quote(repo, safe='')}/collaborators/{username}/permission"
    try:
        data = _req("GET", url, api_token)
        perm = data.get("permissions", {})
        return bool(perm.get("admin")) if isinstance(perm, dict) else False
    except RuntimeError:
        return False


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
                     permission: str = "maintainer") -> bool:
    """将用户添加为仓库协作者（GitCode 邀请制：创建邀请，需对方接受）。

    注意：
      - api_token 必须是仓库所有者/管理员的 token（GitCode 拒绝用 bot 自己 token 邀请自己）。
      - GitCode PUT body 字段名是单数 `permission`，枚举值：pull/push/admin/customized。
      - 已存在成员 / 已被邀请（400"已是成员"）视为成功（幂等）。
      - 不再静默降级：maintainer 邀请失败直接返回 False，由调用方决定是否拒绝托管。
    """
    if not username or not api_token:
        return False
    gitcode_perm = _PERMISSION_TO_GITCODE.get((permission or "").lower(), permission)
    url = f"{API_BASE}/repos/{owner}/{urllib.parse.quote(repo, safe='')}/collaborators/{urllib.parse.quote(username, safe='')}"
    try:
        _req("PUT", url, api_token, {"permission": gitcode_perm})
        return True
    except RuntimeError as e:
        # 已是成员 / 已被邀请（幂等成功）：说明该用户已在仓库的成员或邀请列表中
        if any(k in str(e) for k in ("已经是项目中的成员", "已经是", "已存在", "已被邀请", "已邀请", "已经邀请")):
            return True
        log.warning(f"add_collaborator failed: {e}")
        return False


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
        with urllib.request.urlopen(req, timeout=30) as resp:
            raw = resp.read().decode()
            return json.loads(raw) if raw.strip() else {}
    except urllib.error.HTTPError as e:
        raw = e.read().decode(errors="replace")[:500]
        raise RuntimeError(f"GitCode API {method} {path} failed ({e.code}): {raw}")
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


def get_member_permission(api_token: str, owner: str, repo: str, username: str) -> dict | None:
    """查看仓库成员权限详情（GET /collaborators/{username}/permission）。

    返回含 access_level（30=Developer，40=Maintainer）、role_name、permission 等，失败返回 None。
    """
    if not username or not api_token:
        return None
    url = f"{API_BASE}/repos/{owner}/{urllib.parse.quote(repo, safe='')}/collaborators/{urllib.parse.quote(username, safe='')}/permission"
    try:
        return _req("GET", url, api_token)
    except RuntimeError as e:
        log.warning(f"get_member_permission failed: {e}")
        return None


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
                          "name": full_name.split("/", 1)[1], "is_admin": is_admin})
        if len(items) < 100:
            break
        page += 1
    log.info(f"fetch_repos total: {len(repos)} repos (current_user={current_user})")
    return repos


def get_user_gitcode_login(api_token: str) -> str:
    """获取当前 token 的 GitCode 登录用户名（GET /api/v5/user）。"""
    if not api_token:
        return ""
    try:
        data = _req("GET", f"{API_BASE}/user", api_token)
        return data.get("login", "") or ""
    except RuntimeError as e:
        log.warning(f"get_user_gitcode_login failed: {e}")
        return ""


def create_repo(api_token: str, name: str, description: str = "", private: bool = False) -> dict:
    """在 token 用户个人名下创建一个空仓库（POST /api/v5/user/repos）。

    返回 GitCode 仓库信息（含 full_name / html_url 等）。同名仓库冲突时抛 RuntimeError。
    """
    if not api_token:
        raise RuntimeError("GitCode API Token 未配置")
    if not name:
        raise RuntimeError("仓库名不能为空")
    body = {
        "name": name,
        "description": description,
        "private": bool(private),
        "auto_init": False,
        "default_branch": "main",
        "repository_type": "code",
        "path": name,
    }
    result = _req("POST", f"{API_BASE}/user/repos", api_token, body)
    full_name = result.get("full_name", "")
    if not full_name and result.get("name"):
        full_name = f"{get_user_gitcode_login(api_token)}/{result.get('name')}"
    if not full_name:
        raise RuntimeError(f"创建仓库成功但未返回仓库信息: {json.dumps(result, ensure_ascii=False)[:200]}")
    return result


def _run_git(args: list[str], cwd: str) -> str:
    """在指定目录执行 git 命令，返回 stdout。失败抛 RuntimeError。"""
    import subprocess
    try:
        p = subprocess.run(
            ["git"] + args,
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=120,
        )
    except FileNotFoundError:
        raise RuntimeError("服务器未安装 git CLI")
    except subprocess.TimeoutExpired:
        raise RuntimeError("git 操作超时")
    if p.returncode != 0:
        err = (p.stderr or p.stdout or "").strip()[:500]
        raise RuntimeError(f"git {' '.join(args[:2])} 失败: {err}")
    return (p.stdout or "").strip()


def push_template_to_repo(template: dict, gitcode_login: str, target_repo: str,
                          user_token: str, bot_token: str = "") -> bool:
    """将模板内容推送到新仓库。

    template: 配置中的模板对象（含 source / source_repo / id）
    target_repo: 目标仓库名（不含 owner）
    来源:
      - source="local" → 复制 examples/{template_id}/（或旧路径 configs/project-templates/{id} 兜底）到临时目录
      - source="git"   → git clone source_repo（用 bot_token 拉取）
    目标: 设置 remote 为个人新仓库（https://{user_token}@gitcode.com/{login}/{repo}.git）
          并 git push -u origin HEAD:main。成功返回 True。
    """
    import os
    import shutil
    import tempfile
    from src.config import EXAMPLE_DIR, CONFIG_DIR

    if not user_token or not gitcode_login or not target_repo:
        raise RuntimeError("缺少用户 token 或目标仓库信息")
    if not template:
        raise RuntimeError("模板配置缺失")

    source = template.get("source", "git")
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
            source_platform = template.get("source_platform", "gitcode")
            if not source_repo and not clone_url:
                raise RuntimeError("git 模板缺少 source_repo / clone_url 配置")
            workdir = os.path.join(tmp, "repo")
            if clone_url and source_platform == "github":
                # GitHub 优选模板：用 clone_url（公开仓库，无需 token）
                _run_git(["clone", "--quiet", "--depth", "1", clone_url, workdir], tmp)
            else:
                # GitCode 模板：用 source_repo + token
                clone_token = bot_token or user_token
                if not clone_token:
                    raise RuntimeError("git 模板需要全局 Bot token 拉取")
                url = f"https://oauth2:{clone_token}@gitcode.com/{source_repo}.git"
                _run_git(["clone", "--quiet", "--depth", "1", url, workdir], tmp)

        target_url = f"https://oauth2:{user_token}@gitcode.com/{gitcode_login}/{target_repo}.git"
        if _run_git_has_origin(workdir):
            _run_git(["remote", "remove", "origin"], workdir)
        _run_git(["remote", "add", "origin", target_url], workdir)
        _run_git(["config", "user.email", "devops-bot@gitcode.com"], workdir)
        _run_git(["config", "user.name", "DevOps Bot"], workdir)
        _run_git(["add", "-A"], workdir)
        if _git_has_changes(workdir):
            _run_git(["commit", "-m", "Init from project template"], workdir)
        _run_git(["branch", "-M", "main"], workdir)
        _run_git(["push", "--force", "-u", "origin", "HEAD:main"], workdir)
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


def find_existing_webhook(api_token: str, owner: str, repo: str, webhook_url: str) -> int | None:
    """检查仓库是否已有指向 webhook_url 的 webhook，有则返回 webhook_id"""
    if not webhook_url:
        return None
    url = f"{API_BASE}/repos/{owner}/{urllib.parse.quote(repo, safe='')}/hooks"
    try:
        hooks = _req("GET", url, api_token)
        if isinstance(hooks, list):
            for h in hooks:
                if h.get("url") == webhook_url:
                    return h.get("id")
    except RuntimeError:
        pass
    return None


def setup_webhook(api_token: str, owner: str, repo: str, webhook_url: str, webhook_password: str = "") -> int:
    """在指定仓库上创建 DevOps Bot webhook，返回 webhook_id。"""
    if not webhook_password:
        from src.user.db import get_bot_token_by_repo
        webhook_password = get_bot_token_by_repo(owner, repo) or api_token
    url = f"{API_BASE}/repos/{owner}/{urllib.parse.quote(repo, safe='')}/hooks"
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
    result = _req("POST", url, api_token, body)
    hook_id = result.get("id", 0)
    if not hook_id:
        raise RuntimeError(f"Created webhook but got no id: {json.dumps(result, ensure_ascii=False)[:200]}")
    return hook_id


def _get_managed_webhook(repo_key: str) -> int | None:
    """从 managed_repos 表获取 webhook_id（repo_key 带平台前缀）"""
    import sqlite3
    db_path = None
    try:
        from src.session_manager import db, db_lock
        from src.repo_key import to_repo_key
        repo_key = to_repo_key(repo_key)
        with db_lock:
            c = db.cursor()
            c.execute("SELECT webhook_id FROM managed_repos WHERE repo_key=?",
                      (repo_key,))
            row = c.fetchone()
            return row[0] if row and row[0] else None
    except:
        return None


def remove_webhook(api_token: str, owner: str, repo: str, webhook_id: int) -> bool:
    """删除指定仓库上的 webhook。"""
    url = f"{API_BASE}/repos/{owner}/{urllib.parse.quote(repo, safe='')}/hooks/{webhook_id}"
    _req("DELETE", url, api_token)
    return True
