"""Git 仓库统一接口（Issue-45 M3）：`/api/git-repo/{owner}/{repo}/...` 白名单资源端点。

- 认证：`get_current_user_or_api_key`（对外安全路由，进 OpenAPI 契约）。
- provider：**由认证 token 的 repo_key 前缀派生**（gitcode:/github:），不接受调用方参数，
  `git_repo_provider_from_token` 设计（单一信息源，消除错配）；github/gitlab 预留（403）。
- 白名单：只暴露 Issue/PR/评论/内容/分支/提交/标签/里程碑等资源端点，**无限制透传被拒**。
- gitcode 后端：复用 `repo_manager._req` 调 `api.gitcode.com/api/v5`，token 取仓库 bot token。
- git-credential / workspace 归 `/api/repos` 平台接口（本命名空间只做纯透传，见 2bac1c60）。

出参结构 = gitcode 平台实际结构（与技能 `gitcode-api-reference.md` 一致，见 docs/openapi/10-git-repos.md）。
"""
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

from src.api.deps import get_current_user_or_api_key
from src.config import log
from src.vcs.providers import (
    translate_inbound,
    translate_query_state,
)
from src.vcs.providers import _MODULES as _PROVIDER_MODULES

router = APIRouter(prefix="/api/git-repo", tags=["git-repo"])

# provider = 注册表（vcs/providers）自动派生；新增 provider 只加模块
_SUPPORTED_PROVIDERS = set(_PROVIDER_MODULES)


class PRCreateBody(BaseModel):
    title: str = ""
    head: str = ""
    base: str = ""
    body: str = ""
    related_issue_nums: list = []
    auto_close_issues: bool = False


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
    body: str = ""


class LabelCreateBody(BaseModel):
    name: str
    color: str = "#d73a4a"


class CommentBody(BaseModel):
    body: str


def _provider(user: dict = Depends(get_current_user_or_api_key)) -> str:
    """Git provider（gitcode/github）：由认证 token 的 repo_key 前缀派生，不接受调用方参数。

    单一信息源：token 签发时 repo_key 即带前缀（gitcode:/github:），
    不需要也不再接受 X-Git-Provider / provider query——消除双信息源错配
    （见 docs/weekly-02/git-repo-provider-from-token-design.md）。
    """
    from src.repo_key import parse_repo_key
    p = parse_repo_key(user.get("repo_key") or "")[0] or "gitcode"
    if p not in _SUPPORTED_PROVIDERS:
        raise HTTPException(status_code=403, detail={"code": 403, "message": f"Git provider 未接入: {p}"})
    return p


def _repo_token(owner: str, repo: str, user: dict, provider: str = "") -> str:
    """解析仓库平台 token（git-repo 透传端点共用）。

    ⚠️ 无 provider 默认（默认空串），未注册/未传 provider 直接 fail-fast（不静默 gitcode）。
    ⚠️ 本接口**仅服务会话级临时 token（tk_）**——用户级 API Key（`grape_`）/ Web JWT **不适用**（403）。
    token 按会话 agent_type 决定平台凭据：
    - chat agent → **用户 token**（用户身份）
    - devops 事件 agent → **仓库 bot token**（仓库身份）
    - 一种缺失 → 交叉回退；均缺失 → 502。
    """
    if provider not in _SUPPORTED_PROVIDERS:
        raise ValueError(f"Git provider 未支持: {provider}")
    if user.get("auth_type") != "session_token":
        raise HTTPException(status_code=403, detail={"code": 403, "message": "Git 仓库接口仅支持会话级临时 token（agent 专用），用户 API Key / Web 会话不适用"})
    from src.repo_key import to_repo_key
    mod = _PROVIDER_MODULES[provider]
    rk = to_repo_key(f"{provider}:{owner}/{repo}")

    def _bot():
        try:
            return mod.bot_token(rk) or ""
        except Exception:
            return ""

    def _user():
        try:
            uid = user.get("id") or 0
            username = user.get("username") or ""
            if not username and uid > 0:
                from src.user.db import get_user
                username = (get_user(uid) or {}).get("username", "")
            return mod.user_token(username, uid) or ""
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
    raise HTTPException(status_code=502, detail={"code": 502, "message": f"{provider} 仓库 token 解析失败"})


def _vcs_url(owner: str, repo: str, subpath: str, params: dict | None = None,
               provider: str = "") -> str:
    """拼 provider 原生 URL。无默认 provider（空串）→ fail-fast，不静默 gitcode。"""
    if provider not in _SUPPORTED_PROVIDERS:
        raise ValueError(f"Git provider 未支持: {provider}")
    import urllib.parse
    api_base = _PROVIDER_MODULES[provider].API_BASE
    base = f"{api_base}/repos/{urllib.parse.quote(owner, safe='')}/{urllib.parse.quote(repo, safe='')}"
    url = base + subpath
    if params:
        q = urllib.parse.urlencode({k: v for k, v in params.items() if v not in (None, "", [])})
        if q:
            url += "?" + q
    return url


def _vcs_req(provider: str, method: str, url: str, token: str, body: dict | None = None):
    """按 provider 注册表分发原生 API 调用（新增 provider=加模块）。"""
    try:
        return _PROVIDER_MODULES[provider].request(method, url, token, body)
    except HTTPException:
        raise
    except Exception as e:
        log.warning(f"Git repo API {method} {url} failed: {e}")
        raise HTTPException(status_code=502, detail={"code": 502, "message": f"{provider} API 调用失败: {str(e)[:120]}"})


# ===================== 仓库信息 / 传输支撑 =====================

@router.get("/{owner}/{repo}", operation_id="gitrepo_repo", summary="Repository info (clone_url/default_branch)", description="Repository metadata from the Git provider: id/name/full_name/default_branch/html_url/clone_url/visibility.")
def get_repo_info(owner: str, repo: str, user: dict = Depends(get_current_user_or_api_key),
                  p: str = Depends(_provider)):
    return _vcs_req(p, "GET", _vcs_url(owner, repo, "", None, p), _repo_token(owner, repo, user, p))


# ===================== 分支 / 提交 / 内容 / 标签 / 里程碑 =====================

@router.get("/{owner}/{repo}/branches", operation_id="gitrepo_branches", summary="List branches", description="Lists repository branches from the Git provider (paged).")
def list_branches(owner: str, repo: str, per_page: int = Query(50, ge=1, le=100),
                  user: dict = Depends(get_current_user_or_api_key), p: str = Depends(_provider)):
    return _vcs_req(p, "GET", _vcs_url(owner, repo, "/branches", {"per_page": per_page}, p),
                    _repo_token(owner, repo, user, p))


@router.get("/{owner}/{repo}/commits", operation_id="gitrepo_commits", summary="Commit history", description="Lists the repository commit history from the Git provider.")
def list_commits(owner: str, repo: str, per_page: int = Query(20, ge=1, le=100),
                 user: dict = Depends(get_current_user_or_api_key), p: str = Depends(_provider)):
    return _vcs_req(p, "GET", _vcs_url(owner, repo, "/commits", {"per_page": per_page}, p),
                    _repo_token(owner, repo, user, p))


@router.get("/{owner}/{repo}/tree", operation_id="gitrepo_tree", summary="Repository tree (single-level; GitHub recursive)", description="Repository directory tree. Single-level (default) proxies git/trees/{ref}; depth=recursive is GitHub-only (GitCode rejects it). ref empty resolves the default branch.")
def get_repo_tree(owner: str, repo: str, ref: str = Query("", description="分支/引用，空=默认分支"),
                  depth: str = Query("single", description="single(单层，默认)/recursive(递归，仅 GitHub)"),
                  user: dict = Depends(get_current_user_or_api_key), p: str = Depends(_provider)):
    """取仓库目录树。

    - 单层（default）：透传 `git/trees/{ref}`，返回该层 tree+blob 条目（type/name/path/mode/sha）。
    - 递归（depth=recursive）：仅 GitHub 支持（`git/trees/{sha}?recursive=1`）；
      GitCode 平台 recursive 返回破碎树（丢失 blob），故明确拒绝，避免产出残缺数据。
    - ref 空 → 自动解析默认分支。
    """
    depth = (depth or "single").strip().lower()
    ref = (ref or "").strip()

    # 单层
    if depth != "recursive":
        if not ref:
            ref = _default_ref(p, owner, repo, user)
        return _vcs_req(p, "GET", _vcs_url(owner, repo, f"/git/trees/{ref}", None, p),
                        _repo_token(owner, repo, user, p))
    # 递归
    if p != "github":
        raise HTTPException(status_code=400, detail={"code": 400,
            "message": "该平台（GitCode）暂不支持递归树：recursive 返回破碎树（丢失 blob），请用单层或 Git 仓库等效接口"})
    if not ref:
        ref = _default_ref(p, owner, repo, user)
    # GitHub：git/trees/{sha} 需要 tree sha → 先取默认分支最新 commit 的 tree sha
    _tree_sha = _github_tree_sha(owner, repo, ref, user)
    return _vcs_req(p, "GET", _vcs_url(owner, repo, f"/git/trees/{_tree_sha}", {"recursive": "1"}, p),
                    _repo_token(owner, repo, user, p))


def _default_ref(p: str, owner: str, repo: str, user: dict) -> str:
    """解析默认分支（ref 空时）：GitCode 用 branches[0]，GitHub 用 default_branch 或 branches[0]。"""
    try:
        if p == "github":
            data = _vcs_req(p, "GET", _vcs_url(owner, repo, "", None, p), _repo_token(owner, repo, user, p))
            if isinstance(data, dict) and data.get("default_branch"):
                return data["default_branch"]
        else:
            data = _vcs_req(p, "GET", _vcs_url(owner, repo, "/branches", {"per_page": 1}, p),
                            _repo_token(owner, repo, user, p))
            if isinstance(data, list) and data and data[0].get("name"):
                return data[0]["name"]
    except Exception as e:
        log.warning(f"gitrepo_tree: resolve default ref failed: {e}")
    return "main"


def _github_tree_sha(owner: str, repo: str, ref: str, user: dict) -> str:
    """GitHub：从 ref 解析 tree sha（GET commit 的 commit.tree.sha）。"""
    try:
        data = _vcs_req("github", "GET", _vcs_url(owner, repo, f"/commits/{ref}", {"per_page": 1}, "github"),
                        _repo_token(owner, repo, user, "github"))
        if isinstance(data, list) and data:
            _c = data[0]
        elif isinstance(data, dict):
            _c = data
        else:
            raise ValueError("unexpected commits response")
        # commit.tree.sha 或携带为 dict
        _tree = _c.get("commit", {}).get("tree", {}) or _c.get("tree", {})
        if isinstance(_tree, dict) and _tree.get("sha"):
            return _tree["sha"]
    except Exception as e:
        log.warning(f"gitrepo_tree: resolve github tree sha failed: {e}")
    # 兜底：tree sha 直接用 ref（GitHub /git/trees/{ref} 分支名也可，但 sha 更稳）
    return ref


@router.get("/{owner}/{repo}/contents", operation_id="gitrepo_contents_root", summary="Repository contents: root directory listing", description="Repository root directory listing (empty path returns the GitCode root array).")
def list_root_contents(owner: str, repo: str, ref: str = Query("", description="分支/引用"),
                       user: dict = Depends(get_current_user_or_api_key), p: str = Depends(_provider)):
    """取仓库根目录条目列表（空 path → GitCode contents 返回根目录数组）。"""
    return _vcs_req(p, "GET", _vcs_url(owner, repo, "/contents/", {"ref": ref}, p),
                    _repo_token(owner, repo, user, p))


@router.get("/{owner}/{repo}/contents/{path:path}", operation_id="gitrepo_contents", summary="Repository contents: dir listing or file content (base64)", description="Repository contents via the Git provider: empty/root or directory returns an entry list; a file returns base64 content.")
def get_file_content(owner: str, repo: str, path: str = "", ref: str = Query("", description="分支/引用"),
                     user: dict = Depends(get_current_user_or_api_key), p: str = Depends(_provider)):
    return _vcs_req(p, "GET", _vcs_url(owner, repo, "/contents/" + path, {"ref": ref}, p),
                    _repo_token(owner, repo, user, p))


@router.get("/{owner}/{repo}/labels", operation_id="gitrepo_labels_list", summary="List labels", description="Lists repository labels.")
def list_labels(owner: str, repo: str, user: dict = Depends(get_current_user_or_api_key),
                p: str = Depends(_provider)):
    return _vcs_req(p, "GET", _vcs_url(owner, repo, "/labels", None, p), _repo_token(owner, repo, user, p))


@router.post("/{owner}/{repo}/labels", operation_id="gitrepo_labels_create", summary="Create label", description="Creates a repository label (name required, color optional).")
def create_label(owner: str, repo: str, body: LabelCreateBody, user: dict = Depends(get_current_user_or_api_key),
                 p: str = Depends(_provider)):
    return _vcs_req(p, "POST", _vcs_url(owner, repo, "/labels", None, p), _repo_token(owner, repo, user, p),
                    {"name": body.name, "color": body.color})


@router.get("/{owner}/{repo}/milestones", operation_id="gitrepo_milestones", summary="List milestones", description="Lists repository milestones (state filter).")
def list_milestones(owner: str, repo: str, state: str = Query("all"),
                    user: dict = Depends(get_current_user_or_api_key), p: str = Depends(_provider)):
    return _vcs_req(p, "GET", _vcs_url(owner, repo, "/milestones", {"state": state}, p),
                    _repo_token(owner, repo, user, p))


# ===================== Issues =====================

@router.get("/{owner}/{repo}/issues", operation_id="gitrepo_issues_list", summary="List issues", description="Lists repository issues (state open/closed/all filter, paged).")
def list_issues(owner: str, repo: str, state: str = Query("open"),
                page: int = Query(1, ge=1), per_page: int = Query(50, ge=1, le=100),
                user: dict = Depends(get_current_user_or_api_key), p: str = Depends(_provider)):
    state = translate_query_state(p, state)
    return _vcs_req(p, "GET", _vcs_url(owner, repo, "/issues",
                                        {"state": state, "page": page, "per_page": per_page}, p),
                    _repo_token(owner, repo, user, p))


@router.post("/{owner}/{repo}/issues", operation_id="gitrepo_issues_create", summary="Create issue", description="Creates an issue (title/body/assignee).")
def create_issue(owner: str, repo: str, body: IssueCreateBody, user: dict = Depends(get_current_user_or_api_key),
                 p: str = Depends(_provider)):
    neutral = {"title": body.title, "body": body.body, "assignee": body.assignee}
    payload = translate_inbound(p, "issues", "create", neutral, {"owner_repo": f"{owner}/{repo}"})
    return _vcs_req(p, "POST", _vcs_url(owner, repo, "/issues", None, p), _repo_token(owner, repo, user, p), payload)


@router.get("/{owner}/{repo}/issues/{number}", operation_id="gitrepo_issues_get", summary="Get issue", description="Returns a single issue.")
def get_issue(owner: str, repo: str, number: int, user: dict = Depends(get_current_user_or_api_key),
              p: str = Depends(_provider)):
    return _vcs_req(p, "GET", _vcs_url(owner, repo, f"/issues/{number}", None, p), _repo_token(owner, repo, user, p))


@router.patch("/{owner}/{repo}/issues/{number}", operation_id="gitrepo_issues_update", summary="Update issue", description="Updates an issue (labels/assignee/milestone/state/body).")
def update_issue(owner: str, repo: str, number: int, body: IssueUpdateBody,
                 user: dict = Depends(get_current_user_or_api_key), p: str = Depends(_provider)):
    neutral = {
        "labels": body.labels,
        "assignee": body.assignee,
        "milestone_id": body.milestone_id or None,
        "state": body.state,
        "body": body.body,
    }
    payload = translate_inbound(p, "issues", "update", neutral, {"owner_repo": f"{owner}/{repo}"})
    return _vcs_req(p, "PATCH", _vcs_url(owner, repo, f"/issues/{number}", None, p),
                    _repo_token(owner, repo, user, p), payload)


@router.post("/{owner}/{repo}/issues/{number}/close", operation_id="gitrepo_issues_close", summary="Close issue", description="Closes an issue on the Git provider. Provider-adapted: GitHub PATCH state=closed; GitCode PATCH state=close with the required repo/title (title auto-fetched). Returns the updated issue, or 502 if the provider does not apply the close.")
def close_issue(owner: str, repo: str, number: int, user: dict = Depends(get_current_user_or_api_key),
                p: str = Depends(_provider)):
    """关闭 Issue（对外接口，供 grape-cli / agent 使用）。平台差异复用 VcsApiClient.close_issue（#209 实测）：
    - GitHub：PATCH {"state": "closed"}
    - GitCode：PATCH body 必填 repo+title、state 值为 close（reopen/close），title 需先 GET 取回。
    """
    from src.vcs.api_client import VcsApiClient
    client = VcsApiClient(p, _repo_token(owner, repo, user, p), owner, repo)
    resp = client.close_issue(number)
    if isinstance(resp, dict) and resp.get("error"):
        raise HTTPException(status_code=502, detail={"code": 502, "message": f"关闭 Issue 失败: {resp.get('error')}"})
    return resp


@router.get("/{owner}/{repo}/issues/{number}/comments", operation_id="gitrepo_issues_comments_list", summary="List issue comments", description="Lists comments on an issue.")
def list_issue_comments(owner: str, repo: str, number: int, user: dict = Depends(get_current_user_or_api_key),
                        p: str = Depends(_provider)):
    return _vcs_req(p, "GET", _vcs_url(owner, repo, f"/issues/{number}/comments", None, p),
                    _repo_token(owner, repo, user, p))


@router.post("/{owner}/{repo}/issues/{number}/comments", operation_id="gitrepo_issues_comments_create", summary="Create issue comment", description="Creates a comment on an issue (body required).")
def create_issue_comment(owner: str, repo: str, number: int, body: CommentBody,
                         user: dict = Depends(get_current_user_or_api_key), p: str = Depends(_provider)):
    return _vcs_req(p, "POST", _vcs_url(owner, repo, f"/issues/{number}/comments", None, p),
                    _repo_token(owner, repo, user, p), {"body": body.body})


# ===================== Pull Requests =====================

@router.get("/{owner}/{repo}/pulls", operation_id="gitrepo_pulls_list", summary="List pull requests", description="Lists repository pull requests (state filter, paged).")
def list_pulls(owner: str, repo: str, state: str = Query("open"),
               page: int = Query(1, ge=1), per_page: int = Query(50, ge=1, le=100),
               user: dict = Depends(get_current_user_or_api_key), p: str = Depends(_provider)):
    return _vcs_req(p, "GET", _vcs_url(owner, repo, "/pulls",
                                        {"state": state, "page": page, "per_page": per_page}, p),
                    _repo_token(owner, repo, user, p))


@router.post("/{owner}/{repo}/pulls", operation_id="gitrepo_pulls_create", summary="Create pull request", description="Creates a pull request (title/head/base). Provider-adapted: gitcode inner_issue_nums; github Closes #N body.")
def create_pull(owner: str, repo: str, body: PRCreateBody, user: dict = Depends(get_current_user_or_api_key),
                p: str = Depends(_provider)):
    related = list(body.related_issue_nums or [])
    neutral = {
        "title": body.title,
        "head": body.head,
        "base": body.base,
        "body": body.body,
        "related_issue_nums": related,
        "auto_close_issues": body.auto_close_issues,
    }
    payload = translate_inbound(p, "pulls", "create", neutral, {"owner_repo": f"{owner}/{repo}"})
    return _vcs_req(p, "POST", _vcs_url(owner, repo, "/pulls", None, p), _repo_token(owner, repo, user, p), payload)


@router.get("/{owner}/{repo}/pulls/{number}", operation_id="gitrepo_pulls_get", summary="Get pull request", description="Returns a single pull request.")
def get_pull(owner: str, repo: str, number: int, user: dict = Depends(get_current_user_or_api_key),
             p: str = Depends(_provider)):
    return _vcs_req(p, "GET", _vcs_url(owner, repo, f"/pulls/{number}", None, p), _repo_token(owner, repo, user, p))


@router.patch("/{owner}/{repo}/pulls/{number}", operation_id="gitrepo_pulls_update", summary="Update pull request", description="Updates a pull request (title/body/labels).")
def update_pull(owner: str, repo: str, number: int, body: PRUpdateBody,
                user: dict = Depends(get_current_user_or_api_key), p: str = Depends(_provider)):
    payload = {k: v for k, v in {"title": body.title, "body": body.body, "labels": body.labels}.items() if v != ""}
    return _vcs_req(p, "PATCH", _vcs_url(owner, repo, f"/pulls/{number}", None, p),
                    _repo_token(owner, repo, user, p), payload)


@router.put("/{owner}/{repo}/pulls/{number}/merge", operation_id="gitrepo_pulls_merge", summary="Merge pull request", description="Merges a pull request; returns {merged, sha}.")
def merge_pull(owner: str, repo: str, number: int, user: dict = Depends(get_current_user_or_api_key),
               p: str = Depends(_provider)):
    return _vcs_req(p, "PUT", _vcs_url(owner, repo, f"/pulls/{number}/merge", None, p),
                    _repo_token(owner, repo, user, p))


@router.get("/{owner}/{repo}/pulls/{number}/comments", operation_id="gitrepo_pulls_comments_list", summary="List PR comments", description="Lists PR inline comments.")
def list_pull_comments(owner: str, repo: str, number: int, user: dict = Depends(get_current_user_or_api_key),
                       p: str = Depends(_provider)):
    return _vcs_req(p, "GET", _vcs_url(owner, repo, f"/pulls/{number}/comments", None, p),
                    _repo_token(owner, repo, user, p))


@router.post("/{owner}/{repo}/pulls/{number}/comments", operation_id="gitrepo_pulls_comments_create", summary="Create PR comment", description="Creates a PR comment (body required).")
def create_pull_comment(owner: str, repo: str, number: int, body: CommentBody,
                        user: dict = Depends(get_current_user_or_api_key), p: str = Depends(_provider)):
    return _vcs_req(p, "POST", _vcs_url(owner, repo, f"/pulls/{number}/comments", None, p),
                    _repo_token(owner, repo, user, p), {"body": body.body})


@router.get("/{owner}/{repo}/pulls/{number}/files", operation_id="gitrepo_pulls_files", summary="List PR changed files", description="Lists files changed in a pull request.")
def list_pull_files(owner: str, repo: str, number: int, user: dict = Depends(get_current_user_or_api_key),
                    p: str = Depends(_provider)):
    return _vcs_req(p, "GET", _vcs_url(owner, repo, f"/pulls/{number}/files", None, p),
                    _repo_token(owner, repo, user, p))