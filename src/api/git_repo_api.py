"""Git 仓库统一接口（Issue-45 M3）：`/api/repos/{owner}/{repo}/...` 白名单资源端点。

- 认证：`get_current_user_or_api_key`（对外安全路由，进 OpenAPI 契约）。
- provider：`X-Git-Provider` 头优先，其次 `provider` query，缺省 `gitcode`；github/gitlab 预留（403）。
- 白名单：只暴露 Issue/PR/评论/内容/分支/提交/标签/里程碑等资源端点，**无限制透传被拒**。
- gitcode 后端：复用 `repo_manager._req` 调 `api.gitcode.com/api/v5`，token 取仓库 bot token。
- git-credential / workspace：供 CLI（方案③ 本地执行 git）内部取一次性/短期平台 token 与工作区。

出参结构 = gitcode 平台实际结构（与技能 `gitcode-api-reference.md` 一致，见 docs/openapi/10-git-repos.md）。
"""
import os
from fastapi import APIRouter, Depends, Header, HTTPException, Query
from pydantic import BaseModel

from src.api.deps import get_current_user_or_api_key
from src.config import log, WORKSPACE_DIR

router = APIRouter(prefix="/api/repos", tags=["git-repo"])

_SUPPORTED_PROVIDERS = {"gitcode"}

# git-credential 跨域不可用；保留 TTL 呼吸窗，防 CLI 拿到后长期滞留
_CREDENTIAL_TTL = 600

# gitcode API base（与 repo_manager 同源；测试可 mock）
_GITCODE_API_BASE = "https://api.gitcode.com/api/v5"


class PRCreateBody(BaseModel):
    title: str = ""
    head: str = ""
    base: str = ""
    inner_issue_nums: str = ""
    close_related_issue: bool = False


class PRUpdateBody(BaseModel):
    title: str = ""
    body: str = ""
    labels: str = ""


class IssueCreateBody(BaseModel):
    title: str
    body: str = ""
    assignee: str = ""


class IssueUpdateBody(BaseModel):
    labels: str = ""
    assignee: str = ""
    milestone_id: int = 0
    state: str = ""


class LabelCreateBody(BaseModel):
    name: str
    color: str = "#d73a4a"


class CommentBody(BaseModel):
    body: str


def _provider(header: str = Header("", alias="X-Git-Provider"),
              provider: str = Query("", description="gitcode/github/gitlab，缺省 gitcode")) -> str:
    p = (header or "").strip() or (provider or "").strip() or "gitcode"
    if p not in _SUPPORTED_PROVIDERS:
        raise HTTPException(status_code=403, detail={"code": 403, "message": f"Git provider 未接入: {p}"})
    return p


def _repo_token(owner: str, repo: str, user: dict) -> str:
    """解析仓库平台 token（git-repo / git-credential 共用）。

    ⚠️ 本接口**仅服务会话级临时 token（tk_）**——用户级 API Key（`grape_`）/ Web JWT **不适用**（403）。
    token 按会话 agent_type 决定平台凭据（issue-45-api-token-design §2.5.1）：
    - chat agent → **用户 gitcode token**（用户身份）
    - devops 事件 agent（vod/issue/coding/qa/sre）→ **仓库 bot token**（仓库身份）
    - 一种缺失 → 交叉回退；均缺失 → 502。
    """
    if user.get("auth_type") != "session_token":
        raise HTTPException(status_code=403, detail={"code": 403, "message": "Git 仓库接口仅支持会话级临时 token（agent 专用），用户 API Key / Web 会话不适用"})
    from src.repo_key import to_repo_key
    rk = to_repo_key(f"{owner}/{repo}")

    def _bot() -> str:
        try:
            from src.user.db import get_gitcode_token
            return get_gitcode_token(repo_key=rk) or ""
        except Exception:
            return ""

    def _user() -> str:
        try:
            from src.api.deps import get_user_token_with_refresh
            uid = user.get("id") or 0
            username = user.get("username") or ""
            if not username and uid > 0:
                from src.user.db import get_user
                username = (get_user(uid) or {}).get("username", "")
            return get_user_token_with_refresh(username, user_id=uid)
        except Exception:
            return ""

    if user.get("agent_type") == "chat":
        tok = _user()
        if tok:
            return tok
        tok = _bot()
        if tok:
            return tok
    else:
        tok = _bot()
        if tok:
            return tok
        tok = _user()
        if tok:
            return tok
    raise HTTPException(status_code=502, detail={"code": 502, "message": "Git 仓库 token 解析失败"})


def _gitcode_url(owner: str, repo: str, subpath: str, params: dict | None = None) -> str:
    import urllib.parse
    base = f"{_GITCODE_API_BASE}/repos/{urllib.parse.quote(owner, safe='')}/{urllib.parse.quote(repo, safe='')}"
    url = base + subpath
    if params:
        q = urllib.parse.urlencode({k: v for k, v in params.items() if v not in (None, "", [])})
        if q:
            url += "?" + q
    return url


def _gitcode(method: str, url: str, token: str, body: dict | None = None):
    from src.user.repo_manager import _req
    try:
        return _req(method, url, token, body)
    except Exception as e:
        log.warning(f"Git repo API {method} {url} failed: {e}")
        raise HTTPException(status_code=502, detail={"code": 502, "message": f"GitCode API 调用失败: {str(e)[:120]}"})


# ===================== 仓库信息 / 传输支撑 =====================

@router.get("/{owner}/{repo}", operation_id="gitrepo_repo", summary="仓库信息（clone_url/default_branch）")
def get_repo_info(owner: str, repo: str, user: dict = Depends(get_current_user_or_api_key),
                  p: str = Depends(_provider)):
    if p == "gitcode":
        return _gitcode("GET", _gitcode_url(owner, repo, ""), _repo_token(owner, repo, user))
    raise HTTPException(status_code=403, detail={"code": 403, "message": f"未接入: {p}"})


@router.get("/{owner}/{repo}/workspace", operation_id="gitrepo_workspace", summary="仓库工作区路径（CLI 本地 git 用）")
def get_repo_workspace(owner: str, repo: str, user: dict = Depends(get_current_user_or_api_key),
                       p: str = Depends(_provider)):
    ws = os.path.join(WORKSPACE_DIR, owner, repo)
    return {"workspace": ws, "clone_url": f"https://gitcode.com/{owner}/{repo}.git"}


@router.get("/{owner}/{repo}/git-credential", operation_id="gitrepo_git_credential",
            summary="一次性 git 平台凭据（方案③ CLI 内部使用，TTL 过期）")
def get_git_credential(owner: str, repo: str, user: dict = Depends(get_current_user_or_api_key),
                       p: str = Depends(_provider)):
    """返回 CLI 本地执行 git 所需的平台凭据（一次性/短期，进程内使用不落盘）。

    gitcode：Basic 用 username=`x-access-token`、password=配置的 gitcode token。
    """
    token = _repo_token(owner, repo, user)
    return {
        "provider": p,
        "username": "x-access-token",
        "password": token,
        "clone_url": f"https://gitcode.com/{owner}/{repo}.git",
        "expires_in": _CREDENTIAL_TTL,
    }


# ===================== 分支 / 提交 / 内容 / 标签 / 里程碑 =====================

@router.get("/{owner}/{repo}/branches", operation_id="gitrepo_branches", summary="分支列表")
def list_branches(owner: str, repo: str, per_page: int = Query(50, ge=1, le=100),
                  user: dict = Depends(get_current_user_or_api_key), p: str = Depends(_provider)):
    return _gitcode("GET", _gitcode_url(owner, repo, "/branches", {"per_page": per_page}),
                    _repo_token(owner, repo, user))


@router.get("/{owner}/{repo}/commits", operation_id="gitrepo_commits", summary="提交历史")
def list_commits(owner: str, repo: str, per_page: int = Query(20, ge=1, le=100),
                 user: dict = Depends(get_current_user_or_api_key), p: str = Depends(_provider)):
    return _gitcode("GET", _gitcode_url(owner, repo, "/commits", {"per_page": per_page}),
                    _repo_token(owner, repo, user))


@router.get("/{owner}/{repo}/contents/{path:path}", operation_id="gitrepo_contents", summary="文件内容（base64）")
def get_file_content(owner: str, repo: str, path: str, ref: str = Query("", description="分支/引用"),
                     user: dict = Depends(get_current_user_or_api_key), p: str = Depends(_provider)):
    return _gitcode("GET", _gitcode_url(owner, repo, "/contents/" + path, {"ref": ref}),
                    _repo_token(owner, repo, user))


@router.get("/{owner}/{repo}/labels", operation_id="gitrepo_labels_list", summary="标签列表")
def list_labels(owner: str, repo: str, user: dict = Depends(get_current_user_or_api_key),
                p: str = Depends(_provider)):
    return _gitcode("GET", _gitcode_url(owner, repo, "/labels"), _repo_token(owner, repo, user))


@router.post("/{owner}/{repo}/labels", operation_id="gitrepo_labels_create", summary="创建标签")
def create_label(owner: str, repo: str, body: LabelCreateBody, user: dict = Depends(get_current_user_or_api_key),
                 p: str = Depends(_provider)):
    return _gitcode("POST", _gitcode_url(owner, repo, "/labels"), _repo_token(owner, repo, user),
                    {"name": body.name, "color": body.color})


@router.get("/{owner}/{repo}/milestones", operation_id="gitrepo_milestones", summary="里程碑列表")
def list_milestones(owner: str, repo: str, state: str = Query("all"),
                    user: dict = Depends(get_current_user_or_api_key), p: str = Depends(_provider)):
    return _gitcode("GET", _gitcode_url(owner, repo, "/milestones", {"state": state}),
                    _repo_token(owner, repo, user))


# ===================== Issues =====================

@router.get("/{owner}/{repo}/issues", operation_id="gitrepo_issues_list", summary="Issue 列表")
def list_issues(owner: str, repo: str, state: str = Query("open"),
                page: int = Query(1, ge=1), per_page: int = Query(50, ge=1, le=100),
                user: dict = Depends(get_current_user_or_api_key), p: str = Depends(_provider)):
    return _gitcode("GET", _gitcode_url(owner, repo, "/issues",
                                        {"state": state, "page": page, "per_page": per_page}),
                    _repo_token(owner, repo, user))


@router.post("/{owner}/{repo}/issues", operation_id="gitrepo_issues_create", summary="创建 Issue")
def create_issue(owner: str, repo: str, body: IssueCreateBody, user: dict = Depends(get_current_user_or_api_key),
                 p: str = Depends(_provider)):
    payload = {"title": body.title, "body": body.body}
    if body.assignee:
        payload["assignee"] = body.assignee
    return _gitcode("POST", _gitcode_url(owner, repo, "/issues"), _repo_token(owner, repo, user), payload)


@router.get("/{owner}/{repo}/issues/{number}", operation_id="gitrepo_issues_get", summary="Issue 详情")
def get_issue(owner: str, repo: str, number: int, user: dict = Depends(get_current_user_or_api_key),
              p: str = Depends(_provider)):
    return _gitcode("GET", _gitcode_url(owner, repo, f"/issues/{number}"), _repo_token(owner, repo, user))


@router.patch("/{owner}/{repo}/issues/{number}", operation_id="gitrepo_issues_update", summary="更新 Issue")
def update_issue(owner: str, repo: str, number: int, body: IssueUpdateBody,
                 user: dict = Depends(get_current_user_or_api_key), p: str = Depends(_provider)):
    payload = {k: v for k, v in {
        "labels": body.labels, "assignee": body.assignee,
        "milestone_id": body.milestone_id or None, "state": body.state,
    }.items() if v not in (None, "", 0)}
    return _gitcode("PATCH", _gitcode_url(owner, repo, f"/issues/{number}"),
                    _repo_token(owner, repo, user), payload)


@router.get("/{owner}/{repo}/issues/{number}/comments", operation_id="gitrepo_issues_comments_list", summary="Issue 评论列表")
def list_issue_comments(owner: str, repo: str, number: int, user: dict = Depends(get_current_user_or_api_key),
                        p: str = Depends(_provider)):
    return _gitcode("GET", _gitcode_url(owner, repo, f"/issues/{number}/comments"),
                    _repo_token(owner, repo, user))


@router.post("/{owner}/{repo}/issues/{number}/comments", operation_id="gitrepo_issues_comments_create", summary="创建 Issue 评论")
def create_issue_comment(owner: str, repo: str, number: int, body: CommentBody,
                         user: dict = Depends(get_current_user_or_api_key), p: str = Depends(_provider)):
    return _gitcode("POST", _gitcode_url(owner, repo, f"/issues/{number}/comments"),
                    _repo_token(owner, repo, user), {"body": body.body})


# ===================== Pull Requests =====================

@router.get("/{owner}/{repo}/pulls", operation_id="gitrepo_pulls_list", summary="PR 列表")
def list_pulls(owner: str, repo: str, state: str = Query("open"),
               page: int = Query(1, ge=1), per_page: int = Query(50, ge=1, le=100),
               user: dict = Depends(get_current_user_or_api_key), p: str = Depends(_provider)):
    return _gitcode("GET", _gitcode_url(owner, repo, "/pulls",
                                        {"state": state, "page": page, "per_page": per_page}),
                    _repo_token(owner, repo, user))


@router.post("/{owner}/{repo}/pulls", operation_id="gitrepo_pulls_create", summary="创建 PR")
def create_pull(owner: str, repo: str, body: PRCreateBody, user: dict = Depends(get_current_user_or_api_key),
                p: str = Depends(_provider)):
    payload = {"title": body.title, "head": body.head, "base": body.base}
    if body.inner_issue_nums:
        payload["inner_issue_nums"] = body.inner_issue_nums
    payload["close_related_issue"] = body.close_related_issue
    return _gitcode("POST", _gitcode_url(owner, repo, "/pulls"), _repo_token(owner, repo, user), payload)


@router.get("/{owner}/{repo}/pulls/{number}", operation_id="gitrepo_pulls_get", summary="PR 详情")
def get_pull(owner: str, repo: str, number: int, user: dict = Depends(get_current_user_or_api_key),
             p: str = Depends(_provider)):
    return _gitcode("GET", _gitcode_url(owner, repo, f"/pulls/{number}"), _repo_token(owner, repo, user))


@router.patch("/{owner}/{repo}/pulls/{number}", operation_id="gitrepo_pulls_update", summary="更新 PR")
def update_pull(owner: str, repo: str, number: int, body: PRUpdateBody,
                user: dict = Depends(get_current_user_or_api_key), p: str = Depends(_provider)):
    payload = {k: v for k, v in {"title": body.title, "body": body.body, "labels": body.labels}.items() if v != ""}
    return _gitcode("PATCH", _gitcode_url(owner, repo, f"/pulls/{number}"),
                    _repo_token(owner, repo, user), payload)


@router.put("/{owner}/{repo}/pulls/{number}/merge", operation_id="gitrepo_pulls_merge", summary="合并 PR")
def merge_pull(owner: str, repo: str, number: int, user: dict = Depends(get_current_user_or_api_key),
               p: str = Depends(_provider)):
    return _gitcode("PUT", _gitcode_url(owner, repo, f"/pulls/{number}/merge"),
                    _repo_token(owner, repo, user))


@router.get("/{owner}/{repo}/pulls/{number}/comments", operation_id="gitrepo_pulls_comments_list", summary="PR 评论列表")
def list_pull_comments(owner: str, repo: str, number: int, user: dict = Depends(get_current_user_or_api_key),
                       p: str = Depends(_provider)):
    return _gitcode("GET", _gitcode_url(owner, repo, f"/pulls/{number}/comments"),
                    _repo_token(owner, repo, user))


@router.post("/{owner}/{repo}/pulls/{number}/comments", operation_id="gitrepo_pulls_comments_create", summary="创建 PR 评论")
def create_pull_comment(owner: str, repo: str, number: int, body: CommentBody,
                        user: dict = Depends(get_current_user_or_api_key), p: str = Depends(_provider)):
    return _gitcode("POST", _gitcode_url(owner, repo, f"/pulls/{number}/comments"),
                    _repo_token(owner, repo, user), {"body": body.body})


@router.get("/{owner}/{repo}/pulls/{number}/files", operation_id="gitrepo_pulls_files", summary="PR 变更文件")
def list_pull_files(owner: str, repo: str, number: int, user: dict = Depends(get_current_user_or_api_key),
                    p: str = Depends(_provider)):
    return _gitcode("GET", _gitcode_url(owner, repo, f"/pulls/{number}/files"),
                    _repo_token(owner, repo, user))