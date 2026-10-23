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
                from src.auth.grape_account_db import get_grape_account_by_id
                username = (get_grape_account_by_id(uid) or {}).get("username", "")
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
    # Issue #372 §30: issue body 追加 grape-auto-comment 标记
    _body = body.body or ""
    if _body and "<!-- grape-auto-comment -->" not in _body:
        _body = _body + "\n<!-- grape-auto-comment -->"
    neutral = {"title": body.title, "body": _body, "assignee": body.assignee}
    payload = translate_inbound(p, "issues", "create", neutral, {"owner_repo": f"{owner}/{repo}"})
    resp = _vcs_req(p, "POST", _vcs_url(owner, repo, "/issues", None, p), _repo_token(owner, repo, user, p), payload)
    # Issue #453 代发台账（S12 写入点1）：建 Issue 入账，webhook 回流按编号对账
    try:
        from src.outbound_ledger import record_outbound, extract_content_id, content_hash_md5
        from src.repo_key import to_repo_key
        _rk = to_repo_key(f"{owner}/{repo}", p)
        _cid = extract_content_id(resp, "issue")
        record_outbound(p, _rk, "issue", _cid, issue_number=_cid,
                        content_hash=content_hash_md5(_body))
    except Exception as _e:
        log.warning(f"outbound_ledger record (create_issue) failed: {_e}")
    return resp


@router.get("/{owner}/{repo}/issues/{number}", operation_id="gitrepo_issues_get", summary="Get issue", description="Returns a single issue.")
def get_issue(owner: str, repo: str, number: int, user: dict = Depends(get_current_user_or_api_key),
              p: str = Depends(_provider)):
    return _vcs_req(p, "GET", _vcs_url(owner, repo, f"/issues/{number}", None, p), _repo_token(owner, repo, user, p))


@router.patch("/{owner}/{repo}/issues/{number}", operation_id="gitrepo_issues_update", summary="Update issue", description="Updates an issue (labels/assignee/milestone/state/body).")
def update_issue(owner: str, repo: str, number: int, body: IssueUpdateBody,
                 user: dict = Depends(get_current_user_or_api_key), p: str = Depends(_provider)):
    # Issue #372 §30: body 字段追加 grape-auto-comment 标记（与 create_issue_comment 一致）
    # 路径 B 场景 actor=用户 → webhook 事件 → 自循环检测标记层需能拦截
    _body = body.body or ""
    if _body and "<!-- grape-auto-comment -->" not in _body:
        _body = _body + "\n<!-- grape-auto-comment -->"
    neutral = {
        "labels": body.labels,
        "assignee": body.assignee,
        "milestone_id": body.milestone_id or None,
        "state": body.state,
        "body": _body,
    }
    payload = translate_inbound(p, "issues", "update", neutral, {"owner_repo": f"{owner}/{repo}"})
    resp = _vcs_req(p, "PATCH", _vcs_url(owner, repo, f"/issues/{number}", None, p),
                    _repo_token(owner, repo, user, p), payload)
    # Issue #453 代发台账（S14 编辑回声）：代编辑正文无 comment_id，按 (repo+指纹) 记账；
    # S19：非 body 修改（assignee/labels/state）记 issue_meta（编号键）——元数据回声对账
    try:
        from src.outbound_ledger import record_outbound, content_hash_md5
        from src.repo_key import to_repo_key
        _rk = to_repo_key(f"{owner}/{repo}", p)
        if _body:
            record_outbound(p, _rk, "issue_edit", str(number),
                            issue_number=str(number), content_hash=content_hash_md5(_body))
        else:
            record_outbound(p, _rk, "issue_meta", str(number), issue_number=str(number))
    except Exception as _e:
        log.warning(f"outbound_ledger record (update_issue) failed: {_e}")
    return resp


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
    resp = _vcs_req(p, "POST", _vcs_url(owner, repo, f"/issues/{number}/comments", None, p),
                    _repo_token(owner, repo, user, p), {"body": body.body})
    # Issue #453 代发台账（S12 写入点1）：评论入账，webhook 回流按评论 ID 对账
    try:
        from src.outbound_ledger import record_outbound, extract_content_id, content_hash_md5
        from src.repo_key import to_repo_key
        _rk = to_repo_key(f"{owner}/{repo}", p)
        record_outbound(p, _rk, "issue_comment", extract_content_id(resp, "issue_comment"),
                        issue_number=str(number), content_hash=content_hash_md5(body.body))
    except Exception as _e:
        log.warning(f"outbound_ledger record (create_issue_comment) failed: {_e}")
    return resp


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
    # Issue #372 §30: PR body 追加 grape-auto-comment 标记（与 create_issue_comment 一致）
    _body = body.body or ""
    if _body and "<!-- grape-auto-comment -->" not in _body:
        _body = _body + "\n<!-- grape-auto-comment -->"
    related = list(body.related_issue_nums or [])
    neutral = {
        "title": body.title,
        "head": body.head,
        "base": body.base,
        "body": _body,
        "related_issue_nums": related,
        "auto_close_issues": body.auto_close_issues,
    }
    payload = translate_inbound(p, "pulls", "create", neutral, {"owner_repo": f"{owner}/{repo}"})
    resp = _vcs_req(p, "POST", _vcs_url(owner, repo, "/pulls", None, p), _repo_token(owner, repo, user, p), payload)
    # Issue #453 台账（S29-G2，网关 PR 端点族补全）：建 PR 入账——同族 update_pull/merge_pull
    # 已入账，此端点原先漏账（外部 API 调用者经网关建 PR 的回流无台账防线）。
    # best-effort：入账失败不阻断、不影响响应。bot 建 PR 回流由 S24 豁免先于台账判定放行给
    # QA，不受影响；外部用户建的 PR 无记录 → 放行（判定逻辑不变）。
    try:
        from src.outbound_ledger import record_outbound, extract_content_id, content_hash_md5
        from src.repo_key import to_repo_key
        record_outbound(p, to_repo_key(f"{owner}/{repo}", p), "pr", extract_content_id(resp, "issue"),
                        issue_number=extract_content_id(resp, "issue"),
                        content_hash=content_hash_md5(_body) if _body else "")
    except Exception as _e:
        log.warning(f"outbound_ledger record (create_pull) failed: {_e}")
    return resp


@router.get("/{owner}/{repo}/pulls/{number}", operation_id="gitrepo_pulls_get", summary="Get pull request", description="Returns a single pull request.")
def get_pull(owner: str, repo: str, number: int, user: dict = Depends(get_current_user_or_api_key),
             p: str = Depends(_provider)):
    return _vcs_req(p, "GET", _vcs_url(owner, repo, f"/pulls/{number}", None, p), _repo_token(owner, repo, user, p))


@router.patch("/{owner}/{repo}/pulls/{number}", operation_id="gitrepo_pulls_update", summary="Update pull request", description="Updates a pull request (title/body/labels).")
def update_pull(owner: str, repo: str, number: int, body: PRUpdateBody,
                user: dict = Depends(get_current_user_or_api_key), p: str = Depends(_provider)):
    # Issue #372 §30: PR body 追加 grape-auto-comment 标记
    _body = body.body or ""
    if _body and "<!-- grape-auto-comment -->" not in _body:
        _body = _body + "\n<!-- grape-auto-comment -->"
    payload = {k: v for k, v in {"title": body.title, "body": _body, "labels": body.labels}.items() if v != ""}
    resp = _vcs_req(p, "PATCH", _vcs_url(owner, repo, f"/pulls/{number}", None, p),
                    _repo_token(owner, repo, user, p), payload)
    # Issue #453 台账（S25，第十一轮补漏）：agent 改 PR → pull_request_updated 回声对账
    try:
        from src.outbound_ledger import record_outbound, content_hash_md5
        from src.repo_key import to_repo_key
        record_outbound(p, to_repo_key(f"{owner}/{repo}", p), "pr", str(number),
                        issue_number=str(number),
                        content_hash=content_hash_md5(_body) if _body else "")
    except Exception as _e:
        log.warning(f"outbound_ledger record (update_pull) failed: {_e}")
    return resp


@router.put("/{owner}/{repo}/pulls/{number}/merge", operation_id="gitrepo_pulls_merge", summary="Merge pull request", description="Merges a pull request; returns {merged, sha}.")
def merge_pull(owner: str, repo: str, number: int, user: dict = Depends(get_current_user_or_api_key),
               p: str = Depends(_provider)):
    resp = _vcs_req(p, "PUT", _vcs_url(owner, repo, f"/pulls/{number}/merge", None, p),
                    _repo_token(owner, repo, user, p))
    # Issue #453 台账（S25）：合并 PR → pull_request_merged 回声对账（VcsApiClient.merge_pr
    # 已入账，网关直调路径此处分账）
    try:
        from src.outbound_ledger import record_outbound
        from src.repo_key import to_repo_key
        record_outbound(p, to_repo_key(f"{owner}/{repo}", p), "pr_merge", str(number),
                        issue_number=str(number))
    except Exception as _e:
        log.warning(f"outbound_ledger record (merge_pull) failed: {_e}")
    return resp


@router.get("/{owner}/{repo}/pulls/{number}/comments", operation_id="gitrepo_pulls_comments_list", summary="List PR comments", description="Lists PR inline comments.")
def list_pull_comments(owner: str, repo: str, number: int, user: dict = Depends(get_current_user_or_api_key),
                       p: str = Depends(_provider)):
    return _vcs_req(p, "GET", _vcs_url(owner, repo, f"/pulls/{number}/comments", None, p),
                    _repo_token(owner, repo, user, p))


@router.post("/{owner}/{repo}/pulls/{number}/comments", operation_id="gitrepo_pulls_comments_create", summary="Create PR comment", description="Creates a PR comment (body required).")
def create_pull_comment(owner: str, repo: str, number: int, body: CommentBody,
                        user: dict = Depends(get_current_user_or_api_key), p: str = Depends(_provider)):
    # Issue #372 R4.1: PR 评论同样追加标记
    _body = body.body or ""
    if "<!-- grape-auto-comment -->" not in _body:
        _body = _body + "\n<!-- grape-auto-comment -->"
    resp = _vcs_req(p, "POST", _vcs_url(owner, repo, f"/pulls/{number}/comments", None, p),
                    _repo_token(owner, repo, user, p), {"body": _body})
    # Issue #453 台账（S29-G1，网关 PR 端点族补全）：PR 评论入账——系统内部发 PR 评论
    # 走 event_pipeline 裸 urllib 路径（已入账），此网关端点原先漏账，仅剩外部 API 调用者
    # 的理论路径无台账防线。回流按 note_hook_pr 评论 ID 对账；系统内部 QA 回帖 PR 的
    # note_hook_pr 回声即命中。best-effort 不阻断。
    try:
        from src.outbound_ledger import record_outbound, extract_content_id, content_hash_md5
        from src.repo_key import to_repo_key
        record_outbound(p, to_repo_key(f"{owner}/{repo}", p), "issue_comment",
                        extract_content_id(resp, "issue_comment"),
                        issue_number=str(number), content_hash=content_hash_md5(_body))
    except Exception as _e:
        log.warning(f"outbound_ledger record (create_pull_comment) failed: {_e}")
    return resp


@router.get("/{owner}/{repo}/pulls/{number}/files", operation_id="gitrepo_pulls_files", summary="List PR changed files", description="Lists files changed in a pull request.")
def list_pull_files(owner: str, repo: str, number: int, user: dict = Depends(get_current_user_or_api_key),
                    p: str = Depends(_provider)):
    return _vcs_req(p, "GET", _vcs_url(owner, repo, f"/pulls/{number}/files", None, p),
                    _repo_token(owner, repo, user, p))