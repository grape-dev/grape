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
from pydantic import BaseModel, ConfigDict, Field

from src.api.deps import get_current_user_or_api_key
from src.config import log
from src.user.repo_manager import UpstreamAPIError
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
    # Issue #581: labels 放宽为 str|list（CLI --json 数组直传不再与 Pydantic 冲突）。
    # 通道格式差异：GitHub 原生要求字符串数组（逗号分隔字符串会被 422），GitCode 维持
    # 逗号分隔字符串透传；归一化入口见 _normalize_labels，通道适配见 _provider_labels。
    labels: str | list[str] = Field(
        "",
        title="PR labels",
        description="Comma-separated label string or label array. "
                    "GitHub channel forwards an array; GitCode channel forwards a comma-separated string.",
        examples=["bug,P1", ["bug", "P1"]],
    )


class PRMergeBody(BaseModel):
    """Issue #558：merge 端点 body 参数（均可选，空值不透传，存量行为不变）。

    - merge_method：merge/squash/rebase；缺省空 → 不传（上游默认 merge）。
    - title：自定义合并提交标题。
    """
    merge_method: str = ""
    title: str = ""


class IssueCreateBody(BaseModel):
    title: str
    body: str = ""
    assignee: str = ""


class IssueUpdateBody(BaseModel):
    """Issue #621：state 契约对齐后端语义（GitHub state_event：reopen/close）。

    - 仅 schema 层收紧（enum/description/example），运行时校验行为不变（非法值仍由后端 400 拒绝）。
    - 请求示例见 model_config json_schema_extra（CLI help / openapi.json 随契约同步）。
    """
    model_config = ConfigDict(
        json_schema_extra={"examples": [{"state": "reopen"}]},
    )

    labels: str | list[str] = Field(
        "",
        title="Issue labels",
        description="Comma-separated label string or label array. "
                    "GitHub channel forwards an array; GitCode channel forwards a comma-separated string.",
        examples=["bug,P1", ["bug", "P1"]],
    )
    assignee: str = ""
    milestone_id: int = 0
    state: str = Field(
        "",
        title="Issue state event",
        description="State transition to apply (aligns with GitHub `state_event`): "
                    "accepts only 'reopen' or 'close'. Any other value is rejected by the backend with 400.",
        examples=["reopen"],
        json_schema_extra={"enum": ["reopen", "close"]},
    )
    body: str = ""


class LabelCreateBody(BaseModel):
    name: str
    # Issue #580: color 默认值去 # 前缀（GitHub REST API 要求不带 # 的 6 位 hex）
    color: str = "d73a4a"


class MilestoneCreateBody(BaseModel):
    """Issue #632：创建里程碑（对齐 #577 Spec）。title 必填；description/due_on 可选。"""
    title: str
    description: str = ""
    due_on: str = ""


class MilestoneUpdateBody(BaseModel):
    """Issue #632：更新里程碑。全字段可选（空值不透传，存量行为不受影响）。"""
    title: str = ""
    description: str = ""
    due_on: str = ""
    state: str = ""


class CommentBody(BaseModel):
    body: str


def _normalize_labels(value: str | list[str] | None) -> list[str] | str:
    """Issue #581：labels 归一化入口（str|list → list[str]）。

    - str：按逗号 split、逐项 strip、丢弃空项（GitHub 逗号分隔字符串在此转数组）。
    - list：逐项 str 化 + strip + 丢弃空项（CLI --json 数组直传）。
    - 空值/归一化后为空 → 返回 ""（保持 apply_rules / payload 过滤的空值跳过语义）。
    """
    if value is None or value == "":
        return ""
    if isinstance(value, str):
        items = [s.strip() for s in value.split(",")]
    elif isinstance(value, list):
        items = [str(s).strip() for s in value]
    else:
        return ""
    items = [s for s in items if s]
    return items or ""


def _provider_labels(provider: str, value: str | list[str] | None) -> str | list[str]:
    """Issue #581：labels 通道适配（issues/pulls update 共用）。

    - GitHub：归一化为字符串数组（GitHub PATCH /issues 的 labels 要求 array，
      逗号分隔字符串透传会 422）。
    - GitCode：维持逗号分隔字符串透传（存量行为零变更）；数组入参兜底 join 为逗号字符串。
    """
    if value is None or value == "":
        return ""
    if provider == "github":
        return _normalize_labels(value)
    if isinstance(value, list):
        normalized = _normalize_labels(value)
        return ",".join(normalized) if normalized else ""
    return value


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
    """按 provider 注册表分发原生 API 调用（新增 provider=加模块）。

    Issue #549/#558：上游 Git 平台返回 >=400 时透传真实状态码与完整 message
    （404 → 404，403 → 403…，不截断），502 仅保留给代理自身故障（连接失败/超时/非上游异常）。
    """
    try:
        return _PROVIDER_MODULES[provider].request(method, url, token, body)
    except HTTPException:
        raise
    except UpstreamAPIError as e:
        log.warning(f"Git repo API {method} {url} upstream error: {e}")
        msg = e.message or f"{provider} API 上游错误 ({e.status_code})"
        raise HTTPException(status_code=e.status_code,
                            detail={"code": e.status_code, "message": msg})
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
        # 分支/引用名含 `/`（如 feat/issue-11）裸拼进 git/trees/{ref} 会被 GitCode 当
        # 路径分隔符拆成两段 → 404（grape-skills#95）。先解析为无斜杠的 tree/commit sha
        # 再取树；sha 不含 `/`，彻底免 URL 编码歧义。无斜杠 ref 保持直连（零回归）。
        if "/" in ref:
            ref = _ref_to_tree_sha(p, owner, repo, ref, user)
            log.info(f"gitrepo_tree: slash ref resolved -> tree sha {ref}")
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


def _ref_to_tree_sha(p: str, owner: str, repo: str, ref: str, user: dict) -> str:
    """ref 含 `/` 时解析为无斜杠的 tree/commit sha（git-repo-tree-slash-ref-design §3.1）。

    - GitCode：`/commits?sha={ref}`（query 参数形态，urlencode 免斜杠切分）→ commit 的
      `commit.tree.sha`；与 `_github_tree_sha` 同思路。
    - GitHub：复用 `_github_tree_sha`（本函数对 github provider 不额外发请求，直接委托）。
    解析失败 → 保序降级返回原 ref（调用方仍按 `git/trees/{ref}` 透传，行为等同现状，不新增 502）。
    """
    if p == "github":
        return _github_tree_sha(owner, repo, ref, user)
    try:
        # GitCode: /commits?sha={ref}&per_page=1 → 首条 commit 的 commit.tree.sha
        data = _vcs_req(p, "GET", _vcs_url(owner, repo, "/commits", {"sha": ref, "per_page": 1}, p),
                        _repo_token(owner, repo, user, p))
        _c = None
        if isinstance(data, list) and data:
            _c = data[0]
        elif isinstance(data, dict):
            _c = data
        if _c is not None:
            _tree = (_c.get("commit") or {}).get("tree") or {}
            if isinstance(_tree, dict) and _tree.get("sha"):
                return _tree["sha"]
            if _c.get("sha"):
                return _c["sha"]
    except Exception as e:
        log.warning(f"gitrepo_tree: resolve tree sha failed for ref={ref}: {e}")
    return ref


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
    # Issue #580: color 归一化——GitHub 通道要求不带 # 的 6 位 hex，带 # 返回 422；GitCode 对两种格式均宽容
    return _vcs_req(p, "POST", _vcs_url(owner, repo, "/labels", None, p), _repo_token(owner, repo, user, p),
                    {"name": body.name, "color": body.color.lstrip("#")})


# ===================== Milestones =====================

# Issue #684：上游返回 state=active/opened，过滤合法值为 open/closed/all →
# 返回字段归一化为 open/closed（口径与过滤值对齐；active→open 映射见端点描述/文档）
_MILESTONE_STATE_MAP = {"active": "open", "opened": "open", "closed": "closed"}


def _normalize_milestone_states(data):
    """milestone(s) 返回条目 state 字段归一化（active/opened→open），其余字段原样透传。"""
    if isinstance(data, list):
        return [_normalize_milestone_states(item) for item in data]
    if isinstance(data, dict) and "state" in data:
        out = dict(data)
        out["state"] = _MILESTONE_STATE_MAP.get(str(data["state"]).lower(), data["state"])
        return out
    return data


@router.get("/{owner}/{repo}/milestones", operation_id="gitrepo_milestones",
            summary="List milestones",
            description="Lists repository milestones (state filter: open/closed/all). "
                        "Returned items' `state` field is normalized to open/closed "
                        "(upstream `active`/`opened` is mapped to `open`).")
def list_milestones(owner: str, repo: str, state: str = Query("all",
                    description="Filter: open/closed/all (upstream rejects other values)"),
                    user: dict = Depends(get_current_user_or_api_key), p: str = Depends(_provider)):
    data = _vcs_req(p, "GET", _vcs_url(owner, repo, "/milestones", {"state": state}, p),
                    _repo_token(owner, repo, user, p))
    return _normalize_milestone_states(data)


@router.post("/{owner}/{repo}/milestones", operation_id="gitrepo_milestones_create", summary="Create milestone", description="Creates a repository milestone (title required; description/due_on optional). Provider passthrough, returns the created milestone with id; returned `state` is normalized to open/closed (active→open).")
def create_milestone(owner: str, repo: str, body: MilestoneCreateBody,
                     user: dict = Depends(get_current_user_or_api_key), p: str = Depends(_provider)):
    # Issue #632（对齐 #577）：GitHub/GitCode milestones 字段语义一致 → 直调透传（同 labels 模式）
    payload = {k: v for k, v in {"title": body.title, "description": body.description,
                                 "due_on": body.due_on}.items() if v != ""}
    data = _vcs_req(p, "POST", _vcs_url(owner, repo, "/milestones", None, p),
                    _repo_token(owner, repo, user, p), payload)
    return _normalize_milestone_states(data)


@router.patch("/{owner}/{repo}/milestones/{number}", operation_id="gitrepo_milestones_update", summary="Update milestone", description="Updates a repository milestone by number (title/description/due_on/state; empty fields are not passed through). Provider passthrough; returned `state` is normalized to open/closed (active→open).")
def update_milestone(owner: str, repo: str, number: int, body: MilestoneUpdateBody,
                     user: dict = Depends(get_current_user_or_api_key), p: str = Depends(_provider)):
    payload = {k: v for k, v in {"title": body.title, "description": body.description,
                                 "due_on": body.due_on, "state": body.state}.items() if v != ""}
    data = _vcs_req(p, "PATCH", _vcs_url(owner, repo, f"/milestones/{number}", None, p),
                    _repo_token(owner, repo, user, p), payload)
    return _normalize_milestone_states(data)


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
        # Issue #581：labels 双通道适配（GitHub→数组 / GitCode→逗号字符串透传）
        "labels": _provider_labels(p, body.labels),
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
    # Issue #453 代发台账（S12 写入点1）：评论入账，webhook 回流按评论 ID 对账。
    # 写侧语义净化（issue-comment-resume-outbound-ledger-fix-design.md §2.2）：台账本质是「系统自回帖
    # 回声抑制」。讨论区经 chat agent 会话（agent_type=chat，_repo_token 解析为**用户 token**）代发的评论
    # 是用户真实回复（正遭受 awaiting_input 恢复所需），必须留回声 → 不记账；devops 事件 agent
    # （agent_type≠chat，bot token 仓库身份）系统回帖 → 记账。git-repo API 仅接受 tk_ 会话 token
    # （_repo_token 403 非 session），故无 jwt/api_key 分支。
    if user.get("agent_type") != "chat":
        try:
            from src.outbound_ledger import record_outbound, extract_content_id, content_hash_md5
            from src.repo_key import to_repo_key
            _rk = to_repo_key(f"{owner}/{repo}", p)
            record_outbound(p, _rk, "issue_comment", extract_content_id(resp, "issue_comment"),
                            issue_number=str(number), content_hash=content_hash_md5(body.body),
                            actor_identity=user.get("username") or "")
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
    # Issue #581：labels 双通道适配（GitHub→数组 / GitCode→逗号字符串透传）；空值过滤语义不变。
    # GitHub "Update a pull request" 端点不接受 labels 字段（真实调用 422），PR 标签
    # 必须走 PATCH /issues/{n}（GitHub PR 复用 issue 元数据端点）——labels 单独拆分请求。
    _labels = _provider_labels(p, body.labels)
    _gh_labels = _labels if p == "github" else ""
    payload = {k: v for k, v in {"title": body.title, "body": _body,
                                  "labels": "" if p == "github" else _labels}.items() if v != ""}
    resp = _vcs_req(p, "PATCH", _vcs_url(owner, repo, f"/pulls/{number}", None, p),
                    _repo_token(owner, repo, user, p), payload)
    if _gh_labels != "":
        # GitHub 通道：labels 走 issue 元数据端点（数组格式），结果并入返回体
        _labels_resp = _vcs_req(p, "PATCH", _vcs_url(owner, repo, f"/issues/{number}", None, p),
                                _repo_token(owner, repo, user, p), {"labels": _gh_labels})
        if isinstance(resp, dict) and isinstance(_labels_resp, dict):
            resp = {**resp, "labels": _labels_resp.get("labels", _gh_labels)}
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


@router.put("/{owner}/{repo}/pulls/{number}/merge", operation_id="gitrepo_pulls_merge",
            summary="Merge pull request",
            description="Merges a pull request; returns {merged, sha}. Optional body: merge_method (merge/squash/rebase, default merge) and title (custom merge commit title), passed through to the provider merge endpoint. Upstream failures (e.g. 405 not mergeable) return the upstream status code with the full error message.")
def merge_pull(owner: str, repo: str, number: int, body: PRMergeBody | None = None,
               user: dict = Depends(get_current_user_or_api_key), p: str = Depends(_provider)):
    # Issue #558：merge_method/title 透传（空值过滤，缺省保持 body=None 存量行为）
    _payload = None
    if body is not None:
        _filtered = {k: v for k, v in
                     {"merge_method": (body.merge_method or "").strip(),
                      "title": (body.title or "").strip()}.items() if v}
        _payload = _filtered or None
    resp = _vcs_req(p, "PUT", _vcs_url(owner, repo, f"/pulls/{number}/merge", None, p),
                    _repo_token(owner, repo, user, p), _payload)
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


@router.post("/{owner}/{repo}/pulls/{number}/comments", operation_id="gitrepo_pulls_comments_create", summary="Create PR comment",
             description="Creates a PR comment (body required). Issue #585: the GitHub provider routes this through "
                         "POST /issues/{number}/comments (a PR is an issue; that endpoint only needs body), because "
                         "GitHub's /pulls/{n}/comments is the inline review comment endpoint which requires "
                         "commit_id/path/line and returns 422 for body-only payloads. GitCode keeps /pulls/{n}/comments.")
def create_pull_comment(owner: str, repo: str, number: int, body: CommentBody,
                        user: dict = Depends(get_current_user_or_api_key), p: str = Depends(_provider)):
    # Issue #372 R4.1: PR 评论同样追加标记
    _body = body.body or ""
    if "<!-- grape-auto-comment -->" not in _body:
        _body = _body + "\n<!-- grape-auto-comment -->"
    # Issue #585 Bug 1: GitHub POST /pulls/{n}/comments 是 inline review comment 端点
    # （必填 commit_id/path/line），仅传 body 必然 422；PR 本身是 issue，改用
    # /issues/{n}/comments 发普通 PR 评论（仅需 body），与 GitCode 行为对齐。
    _comments_path = f"/issues/{number}/comments" if p == "github" else f"/pulls/{number}/comments"
    resp = _vcs_req(p, "POST", _vcs_url(owner, repo, _comments_path, None, p),
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


def _resolve_pull_comment_note_id(p: str, owner: str, repo: str, number: int,
                                  comment_id: str, token: str) -> str:
    """Issue #623：pulls comments update 的 comment_id 兼容十六进制 id 与 note_id。

    - 纯数字串 → 数字 note_id，直传既有更新逻辑（向后兼容，行为不变）。
    - 十六进制串（全 hex 字符且含 a-f 字母）→ create 响应同源主键：先经上游
      GET /pulls/comments/{id}（该端点 id 为 string 形态）取数字 note_id；
      失败再回退 GET /pulls/{number}/comments 按 discussion_id 匹配同一评论对象。
    - 其余非法形态 → 422（明确错误信息，便于调用方自纠）。
    """
    import re
    cid = (comment_id or "").strip()
    if not cid:
        raise HTTPException(status_code=422, detail={"code": 422, "message": "comment_id 不能为空"})
    if cid.isdigit():
        return cid
    # 严格十六进制识别：全 hex 字符且至少含一个字母，避免纯数字串误判
    if not re.fullmatch(r"[0-9a-fA-F]+", cid) or not re.search(r"[a-fA-F]", cid):
        raise HTTPException(status_code=422, detail={
            "code": 422,
            "message": "comment_id 非法：仅支持 note_id（数字，推荐）或 create 响应返回的十六进制 id（兼容形态）"})

    def _first_digit_id(obj) -> str | None:
        if not isinstance(obj, dict):
            return None
        rid = obj.get("id")
        if isinstance(rid, bool):
            return None
        if isinstance(rid, int):
            return str(rid)
        if isinstance(rid, str) and rid.isdigit():
            return rid
        return None

    # ① 上游单评论查询（id 路径参数为 string 形态，可透传十六进制 id）
    try:
        resp = _vcs_req(p, "GET", _vcs_url(owner, repo, f"/pulls/comments/{cid}", None, p), token)
        note_id = _first_digit_id(resp)
        if note_id:
            return note_id
    except HTTPException as e:
        if e.status_code in (401, 403):
            raise
        log.warning(f"Issue #623 hex comment_id single lookup failed ({cid}): {e.detail}")
    # ② 回退：PR 评论列表按 discussion_id（十六进制同源键）匹配同一评论对象
    try:
        items = _vcs_req(p, "GET", _vcs_url(owner, repo, f"/pulls/{number}/comments", None, p), token)
        if isinstance(items, list):
            for it in items:
                if not isinstance(it, dict):
                    continue
                if str(it.get("discussion_id") or "").lower() == cid.lower():
                    note_id = _first_digit_id(it)
                    if note_id:
                        return note_id
    except HTTPException as e:
        log.warning(f"Issue #623 hex comment_id list fallback failed ({cid}): {e.detail}")
    raise HTTPException(status_code=422, detail={
        "code": 422,
        "message": f"comment_id={cid} 无法解析为 note_id：请改用列表响应中的数字 note_id（update --comment_id 支持数字 note_id 与 create 响应的十六进制 id）"})


@router.patch("/{owner}/{repo}/pulls/{number}/comments/{comment_id}", operation_id="gitrepo_pulls_comments_update",
              summary="Update PR comment",
              description="Updates a PR comment body by comment id (body required). Issue #563: CLI self-correction for mis-sent PR comments. "
                          "Issue #623 contract: comment_id accepts the numeric note_id (recommended) or the hex id returned by the create/list comment "
                          "response (compat form, auto-mapped to note_id server-side); other forms return 422.")
def update_pull_comment(owner: str, repo: str, number: int, comment_id: str, body: CommentBody,
                        user: dict = Depends(get_current_user_or_api_key), p: str = Depends(_provider)):
    # 上游 comment id 全局唯一（不含 PR number）：透传 PATCH /pulls/comments/{id}
    # Issue #623：comment_id 兼容十六进制 id——解析为数字 note_id 后走既有更新逻辑
    note_id = _resolve_pull_comment_note_id(p, owner, repo, number, comment_id,
                                            _repo_token(owner, repo, user, p))
    return _vcs_req(p, "PATCH", _vcs_url(owner, repo, f"/pulls/comments/{note_id}", None, p),
                    _repo_token(owner, repo, user, p), {"body": body.body})


@router.delete("/{owner}/{repo}/pulls/{number}/comments/{comment_id}", operation_id="gitrepo_pulls_comments_delete", summary="Delete PR comment", description="Deletes a PR comment by comment id. Issue #563: CLI self-correction for mis-sent PR comments.")
def delete_pull_comment(owner: str, repo: str, number: int, comment_id: int,
                        user: dict = Depends(get_current_user_or_api_key), p: str = Depends(_provider)):
    # 上游 comment id 全局唯一（不含 PR number）：透传 DELETE /pulls/comments/{id}
    return _vcs_req(p, "DELETE", _vcs_url(owner, repo, f"/pulls/comments/{comment_id}", None, p),
                    _repo_token(owner, repo, user, p))


@router.get("/{owner}/{repo}/pulls/{number}/files", operation_id="gitrepo_pulls_files", summary="List PR changed files",
            description="Lists files changed in a pull request. Issue #585: page/per_page pass through to the provider "
                        "(GitHub default per_page=30 silently truncated large PRs; default here is per_page=100). "
                        "For changed_files>100 PRs, page through manually; tree --depth recursive is a fallback for huge PRs.")
def list_pull_files(owner: str, repo: str, number: int, page: int = Query(1, ge=1),
                    per_page: int = Query(100, ge=1, le=100),
                    user: dict = Depends(get_current_user_or_api_key), p: str = Depends(_provider)):
    # Issue #585 Bug 2: 未透传分页参数时 GitHub 默认 per_page=30，>30 文件的 PR 被静默截断
    return _vcs_req(p, "GET", _vcs_url(owner, repo, f"/pulls/{number}/files",
                                       {"page": page, "per_page": per_page}, p),
                    _repo_token(owner, repo, user, p))