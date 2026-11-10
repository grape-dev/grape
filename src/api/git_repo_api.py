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
from pydantic import BaseModel, ConfigDict, Field, field_validator

from src.api.deps import get_current_user_or_api_key
from src.config import log
from src.user.repo_manager import UpstreamAPIError
from src.vcs.providers import (
    translate_inbound,
    translate_query_state,
)
from src.vcs.providers import _MODULES as _PROVIDER_MODULES

router = APIRouter(prefix="/api/git-repo", tags=["git-repo"])

# 完整 sha（40-hex，git object id）：tree 接口直连免存在性预校验（Issue #734）
import re as _re

import json
_FULL_SHA_RE = _re.compile(r"^[0-9a-fA-F]{40}$")

# provider = 注册表（vcs/providers）自动派生；新增 provider 只加模块
_SUPPORTED_PROVIDERS = set(_PROVIDER_MODULES)

# Issue #707：issues update `state` 合法中性集合（入口层前置校验；strip/lower 归一后匹配）
_UPDATE_STATE_NEUTRAL = {"open", "opened", "reopen", "closed", "close"}

# Issue #1076：milestone_id 非整数时的统一提示（多处校验复用，避免文案漂移）
_MILESTONE_ID_INT_HINT = ("'milestone_id' must be an integer milestone number "
                          "(use the `number` field from `grape git-repo milestones`, not the REST internal id)")


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
    - Issue #707：入口层前置校验（update_issue）对非法 state 本地 400，文案指向 `state` 字段；
      中性别名（open/opened/closed）被接受并映射到 reopen/close。
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
    milestone_id: int = Field(
        0,
        title="Milestone id",
        description="Milestone identifier: prefers `id` from `grape git-repo milestones` "
                    "(Issue #1005), and also accepts the legacy `number` for compatibility. "
                    "GitCode resolves it to the upstream `milestone` parameter; "
                    "an unknown/invisible milestone is rejected with 400.",
        examples=[3, 850012],
    )

    @field_validator("milestone_id", mode="before")
    @classmethod
    def _validate_milestone_id(cls, v):
        if v is None or v == "":
            return 0
        if isinstance(v, bool):
            raise ValueError(_MILESTONE_ID_INT_HINT)
        if isinstance(v, str):
            s = v.strip()
            if not s:
                return 0
            try:
                v = int(s)
            except ValueError:
                raise ValueError(_MILESTONE_ID_INT_HINT)
        if not isinstance(v, int):
            raise ValueError(_MILESTONE_ID_INT_HINT)
        if v < 0:
            raise ValueError("'milestone_id' must be a non-negative milestone id or number")
        return v

    state: str = Field(
        "",
        title="Issue state event",
        description="State transition to apply (aligns with GitHub `state_event`): "
                    "accepts only 'reopen' or 'close'. Any other value is rejected by the backend with 400. "
                    "Neutral aliases 'open'/'opened' map to 'reopen', 'closed' maps to 'close' "
                    "(Issue #707: invalid values get a local 400 naming the `state` field).",
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
    # Issue #714：subpath 以 / 为分隔逐段 percent-encode（+→%2B、中文→UTF-8、空格→%20），
    # / 保留路径分隔语义；纯 ASCII 段（/branches、issue 编号等）编码后不变，存量行为零变更。
    url = base + "/".join(urllib.parse.quote(seg, safe="") for seg in subpath.split("/"))
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


@router.get("/{owner}/{repo}/commits", operation_id="gitrepo_commits", summary="Commit history (optional sha/ref filter)", description="Lists repository commit history from the Git provider. Optional sha query filters by branch/tag/commit sha (e.g. weekly-10); omitted = default branch (backward compatible). Nonexistent ref passes through the upstream 404 (Issue #734 semantics).")
def list_commits(owner: str, repo: str, per_page: int = Query(20, ge=1, le=100),
                 sha: str = Query("", description="分支名/tag/commit sha，空=默认分支"),
                 user: dict = Depends(get_current_user_or_api_key), p: str = Depends(_provider)):
    params = {"per_page": per_page}
    if sha:
        params["sha"] = sha
    return _vcs_req(p, "GET", _vcs_url(owner, repo, "/commits", params, p),
                    _repo_token(owner, repo, user, p))


@router.get("/{owner}/{repo}/tree", operation_id="gitrepo_tree", summary="Repository tree (single-level; GitHub recursive)", description="Repository directory tree. Single-level (default) resolves ref existence then proxies git/trees/{sha}; depth=recursive is GitHub-only (GitCode rejects it). ref empty resolves the default branch; nonexistent ref returns 404 (Issue #734).")
def get_repo_tree(owner: str, repo: str, ref: str = Query("", description="分支/引用，空=默认分支"),
                  depth: str = Query("single", description="single(单层，默认)/recursive(递归，仅 GitHub)"),
                  user: dict = Depends(get_current_user_or_api_key), p: str = Depends(_provider)):
    """取仓库目录树。

    - 单层（default）：透传 `git/trees/{ref}`，返回该层 tree+blob 条目（type/name/path/mode/sha）。
    - 递归（depth=recursive）：仅 GitHub 支持（`git/trees/{sha}?recursive=1`）；
      GitCode 平台 recursive 返回破碎树（丢失 blob），故明确拒绝，避免产出残缺数据。
    - ref 空 → 自动解析默认分支。
    - Issue #734/#643：GitCode 上游 `git/trees/{ref}` 对不存在 ref 返回 200 空树，
      故先经 `/commits?sha={ref}` 校验 ref 存在性；不存在 → HTTP 404 + 统一包络
      （与 contents 的 #549 透传语义一致）。完整 sha（40-hex）ref 仍直连零回归。
    """
    depth = (depth or "single").strip().lower()
    ref = (ref or "").strip()

    # 单层
    if depth != "recursive":
        if not ref:
            ref = _default_ref(p, owner, repo, user)
        # 分支/引用名含 `/`（如 feat/issue-11）裸拼进 git/trees/{ref} 会被 GitCode 当
        # 路径分隔符拆成两段 → 404（grape-skills#95）。先解析为无斜杠的 tree/commit sha
        # 再取树；sha 不含 `/`，彻底免 URL 编码歧义。GitHub 无斜杠 ref 保持直连（零回归）。
        if p == "github" and "/" not in ref:
            return _vcs_req(p, "GET", _vcs_url(owner, repo, f"/git/trees/{ref}", None, p),
                            _repo_token(owner, repo, user, p))
        if p != "github" and _FULL_SHA_RE.fullmatch(ref):
            # 完整 tree/commit sha：直连 git/trees/{sha}，不做存在性预校验（sha 非分支名，
            # commits?sha= 对 tree sha 会 404，预校验反成回归）。
            return _vcs_req(p, "GET", _vcs_url(owner, repo, f"/git/trees/{ref}", None, p),
                            _repo_token(owner, repo, user, p))
        _sha = _ref_to_tree_sha(p, owner, repo, ref, user)
        if not _sha:
            # Issue #734：不存在 ref 归一 404（含斜杠/不含斜杠一致），不再返回 200 空树。
            raise HTTPException(status_code=404,
                                detail={"code": 404, "message": f"ref not found: {ref}"})
        ref = _sha
        log.info(f"gitrepo_tree: ref resolved -> tree sha {ref}")
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


def _ref_to_tree_sha(p: str, owner: str, repo: str, ref: str, user: dict) -> str | None:
    """ref 解析为无斜杠的 tree/commit sha（git-repo-tree-slash-ref-design §3.1）。

    - GitCode：`/commits?sha={ref}`（query 参数形态，urlencode 免斜杠切分）→ commit 的
      `commit.tree.sha`；与 `_github_tree_sha` 同思路。
    - GitHub：复用 `_github_tree_sha`（本函数对 github provider 不额外发请求，直接委托；
      其失败兜底仍返回原 ref，由上游 git/trees 404 兜底）。
    - Issue #734：GitCode 解析不到 commit（空列表/坏结构）→ 返回 None，调用方按
      「ref 不存在 → 404」归一处理（不再保序降级返回原 ref）；上游错误（含 404）
      按 #549 透传。
    """
    if p == "github":
        return _github_tree_sha(owner, repo, ref, user)
    try:
        # GitCode: /commits?sha={ref}&per_page=1 → 首条 commit 的 commit.tree.sha
        data = _vcs_req(p, "GET", _vcs_url(owner, repo, "/commits", {"sha": ref, "per_page": 1}, p),
                        _repo_token(owner, repo, user, p))
        _c = None
        if isinstance(data, list):
            _c = data[0] if data else None
        elif isinstance(data, dict):
            _c = data
        if _c is not None:
            _tree = (_c.get("commit") or {}).get("tree") or {}
            if isinstance(_tree, dict) and _tree.get("sha"):
                return _tree["sha"]
            if _c.get("sha"):
                return _c["sha"]
        log.info(f"gitrepo_tree: ref not found via commits lookup: {ref}")
    except HTTPException:
        raise
    except Exception as e:
        log.warning(f"gitrepo_tree: resolve tree sha failed for ref={ref}: {e}")
    return None


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


def _milestone_id_from_url(url):
    """Issue #1005：GitCode 里程碑列表不返回 id，仅 url 尾部含真实 id（…/milestones/3）。"""
    if not isinstance(url, str):
        return None
    m = _re.search(r"/milestones/(\d+)(?:[/?#]|$)", url)
    return int(m.group(1)) if m else None


def _normalize_milestone_states(data):
    """milestone(s) 返回条目归一化：state active/opened→open，并从 url 尾部补 id。

    Issue #1005：上游里程碑对象只有 number（如 850012），真实 id 仅在 url 尾部
    （…/milestones/3）。补 id 后 `milestone_id` 可优先传 id，同时保留 number 兼容。
    """
    if isinstance(data, list):
        return [_normalize_milestone_states(item) for item in data]
    if isinstance(data, dict):
        out = dict(data)
        if "state" in out:
            out["state"] = _MILESTONE_STATE_MAP.get(str(out["state"]).lower(), out["state"])
        if out.get("id") is None:
            _mid = _milestone_id_from_url(out.get("url"))
            if _mid is not None:
                out["id"] = _mid
        return out
    return data


_MILESTONE_FETCH_MAX_PAGES = 21


def _fetch_gitcode_milestones(owner: str, repo: str, user: dict, p: str, want=None):
    """Issue #1005：分页取全量里程碑（用于 milestone_id 的 id/number 解析）。

    ``want`` 给定时（issues update 的解析路径）：一旦本页出现匹配的 id/number 就停止翻页，
    把常见情形的上游往返从「整表 6~10 次」降到 1~2 次（Issue #1076 复审的延迟尖峰）。
    未命中时仍按空页/重复页扫到底，保证「不存在」判定不被短页欺骗。
    """
    items = []
    prev_page = None
    for page in range(1, _MILESTONE_FETCH_MAX_PAGES + 1):
        data = _vcs_req(p, "GET", _vcs_url(owner, repo, "/milestones",
                                            {"state": "all", "per_page": _MILESTONES_MAX_PER_PAGE,
                                             "page": page}, p),
                        _repo_token(owner, repo, user, p))
        # 上游可能静默钳制 per_page，不能用「本页不足 per_page」判断末页（Issue #1076 审查）。
        if not isinstance(data, list) or not data:
            break
        if data == prev_page:
            # 上游忽略 page 参数时返回同一页，继续翻页无意义。
            break
        prev_page = data
        items.extend(_normalize_milestone_states(data))
        if want is not None and _resolve_gitcode_milestone(items, want) is not None:
            break
    return items


def _resolve_gitcode_milestone(milestones, milestone_id):
    """Issue #1005：milestone_id 取值口径 id 优先、number 兼容。"""
    try:
        want = int(milestone_id)
    except (TypeError, ValueError):
        return None
    for m in milestones:
        if not isinstance(m, dict) or m.get("id") is None:
            continue
        try:
            if int(m.get("id")) == want:
                return m
        except (TypeError, ValueError):
            continue
    for m in milestones:
        if not isinstance(m, dict) or m.get("number") is None:
            continue
        try:
            if int(m.get("number")) == want:
                return m
        except (TypeError, ValueError):
            continue
    return None


def _normalize_issue_milestone(resp):
    """Issue #1005：issues get/update 回显的 milestone 同样补 id（供 QA 断言）。"""
    if isinstance(resp, dict) and isinstance(resp.get("milestone"), dict):
        out = dict(resp)
        out["milestone"] = _normalize_milestone_states(resp["milestone"])
        return out
    return resp


# Issue #900：per_page 上限（超出钳制到该值而非 422，与 Spec「大 per_page 不被 422」对齐）
_MILESTONES_MAX_PER_PAGE = 100


@router.get("/{owner}/{repo}/milestones", operation_id="gitrepo_milestones",
            summary="List milestones",
            description="Lists repository milestones (state filter: open/closed/all). "
                        "Issue #900: optional page/per_page pass through to the provider "
                        "(upstream default small page silently truncated long lists; use per_page=100 for the full set). "
                        "per_page larger than 100 is clamped to 100, not rejected. "
                        "Optional title filter narrows by milestone title. "
                        "When page/per_page are omitted they are not sent upstream (provider defaults apply, "
                        "preserving pre-#900 request URLs). "
                        "Returned items' `state` field is normalized to open/closed "
                        "(upstream `active`/`opened` is mapped to `open`). "
                        "Issue #1005: returned items also expose a non-null `id` "
                        "(derived from the provider milestone URL tail when upstream omits it); "
                        "`milestone_id` on issues update prefers this `id`, with `number` also accepted.")
def list_milestones(owner: str, repo: str, state: str = Query("all",
                    description="Filter: open/closed/all (upstream rejects other values)"),
                    page: int = Query(None, ge=1,
                                      description="Page number (1-based); omitted = provider default"),
                    per_page: int = Query(None, ge=1,
                                          description="Page size, clamped to 100 when larger; "
                                                      "omitted = provider default"),
                    title: str = Query("", description="Optional filter by milestone title"),
                    user: dict = Depends(get_current_user_or_api_key), p: str = Depends(_provider)):
    # Issue #900：分页/过滤参数仅在调用方显式提供时透传上游（_vcs_url 过滤空值），
    # 无参请求 URL 与 #900 之前完全一致（向后兼容）；per_page 超上限钳制而非 422。
    params = {"state": state}
    if page is not None:
        params["page"] = page
    if per_page is not None:
        params["per_page"] = min(per_page, _MILESTONES_MAX_PER_PAGE)
    if title:
        params["title"] = title
    data = _vcs_req(p, "GET", _vcs_url(owner, repo, "/milestones", params, p),
                    _repo_token(owner, repo, user, p))
    return _normalize_milestone_states(data)


@router.post("/{owner}/{repo}/milestones", operation_id="gitrepo_milestones_create", summary="Create milestone", description="Creates a repository milestone (title required; description/due_on optional). Provider passthrough, returns the created milestone with `number` and a non-null `id` (derived from the milestone URL tail when upstream omits it, Issue #1005); `issues update --milestone_id` accepts either. Returned `state` is normalized to open/closed (active→open).")
def create_milestone(owner: str, repo: str, body: MilestoneCreateBody,
                     user: dict = Depends(get_current_user_or_api_key), p: str = Depends(_provider)):
    # Issue #632（对齐 #577）：GitHub/GitCode milestones 字段语义一致 → 直调透传（同 labels 模式）
    payload = {k: v for k, v in {"title": body.title, "description": body.description,
                                 "due_on": body.due_on}.items() if v != ""}
    data = _vcs_req(p, "POST", _vcs_url(owner, repo, "/milestones", None, p),
                    _repo_token(owner, repo, user, p), payload)
    return _normalize_milestone_states(data)


@router.patch("/{owner}/{repo}/milestones/{number}", operation_id="gitrepo_milestones_update", summary="Update milestone", description="Updates a repository milestone by number (title/description/due_on/state; empty fields are not passed through). Provider passthrough; returned `state` is normalized to open/closed (active→open); returned `id` is derived from the milestone URL tail when upstream omits it (Issue #1005).")
def update_milestone(owner: str, repo: str, number: int, body: MilestoneUpdateBody,
                     user: dict = Depends(get_current_user_or_api_key), p: str = Depends(_provider)):
    payload = {k: v for k, v in {"title": body.title, "description": body.description,
                                 "due_on": body.due_on, "state": body.state}.items() if v != ""}
    data = _vcs_req(p, "PATCH", _vcs_url(owner, repo, f"/milestones/{number}", None, p),
                    _repo_token(owner, repo, user, p), payload)
    return _normalize_milestone_states(data)


# Issue #1076：issues update 前校验 milestone number 存在（milestones list 的 number 口径）。
# 先校验后写，避免非法值触发上游 PATCH 清空既有 milestone（上游 PATCH 非原子）。
_MILESTONE_SCAN_MAX_PAGES = 10


def _gitcode_milestone_number_exists(owner: str, repo: str, number: int,
                                     user: dict, p: str) -> tuple[bool, list[int]]:
    """返回 (number 是否存在, 已扫描到的 number 列表)。上游故障由 _vcs_req 抛 5xx。"""
    token = _repo_token(owner, repo, user, p)
    found = False
    numbers: list[int] = []
    prev_page = None
    for page in range(1, _MILESTONE_SCAN_MAX_PAGES + 1):
        data = _vcs_req(p, "GET", _vcs_url(owner, repo, "/milestones",
                                           {"state": "all", "page": page,
                                            "per_page": _MILESTONES_MAX_PER_PAGE}, p), token)
        if not isinstance(data, list) or not data:
            break
        if data == prev_page:
            # 上游忽略 page 参数时返回同一页，继续翻页无意义。
            break
        prev_page = data
        for item in data:
            if not isinstance(item, dict):
                continue
            try:
                n = int(item.get("number"))
            except (TypeError, ValueError):
                continue
            if n not in numbers:
                numbers.append(n)
            if n == number:
                found = True
        # Issue #1076 审查：上游可能静默钳制 per_page，不能用「本页不足 per_page」判断末页
        # （否则后续页的有效 number 会被误判不存在 → 422）；只以空页/non-list 为终止条件。
    return found, numbers


def _milestone_invalid_422(number: int, numbers: list[int], reason: str) -> HTTPException:
    """非法 milestone number 的可读 4xx（不掩盖真实上游故障，5xx 仅保留网关/连接层）。"""
    available = ", ".join(str(n) for n in numbers[:20]) or "无"
    return HTTPException(status_code=422, detail={
        "code": 422,
        "message": f"milestone_id={number} {reason}；合法取值来源为 `grape git-repo milestones` "
                   f"返回的 `number` 字段（非 REST 内部 id）。当前仓库可用 number: {available}",
    })


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


@router.get("/{owner}/{repo}/issues/{number}", operation_id="gitrepo_issues_get", summary="Get issue", description="Returns a single issue. The `milestone` object is echoed with a non-null `id` (derived from the milestone URL tail when upstream omits it, Issue #1005), enabling QA to assert the assignment.")
def get_issue(owner: str, repo: str, number: int, user: dict = Depends(get_current_user_or_api_key),
              p: str = Depends(_provider)):
    resp = _vcs_req(p, "GET", _vcs_url(owner, repo, f"/issues/{number}", None, p), _repo_token(owner, repo, user, p))
    return _normalize_issue_milestone(resp)


@router.patch("/{owner}/{repo}/issues/{number}", operation_id="gitrepo_issues_update", summary="Update issue", description="Updates an issue (labels/assignee/milestone/state/body). GitCode: milestone_id prefers the milestone `id` and also accepts `number` (Issue #1005); the resolved milestone is sent upstream as `milestone`, PATCH auto-fills required repo/title. The milestone is pre-validated before write and the existing value is preserved/rolled back on failure; success is read-back verified and echoed. Failure semantics are distinguishable instead of a generic 502: 400 when the milestone does not exist / is not visible, 403 when the caller lacks permission, 422 when upstream accepted the PATCH but ignored the milestone. Upstream real failures still return 5xx.")
def update_issue(owner: str, repo: str, number: int, body: IssueUpdateBody,
                 user: dict = Depends(get_current_user_or_api_key), p: str = Depends(_provider)):
    # Issue #372 §30: body 字段追加 grape-auto-comment 标记（与 create_issue_comment 一致）
    # 路径 B 场景 actor=用户 → webhook 事件 → 自循环检测标记层需能拦截
    _body = body.body or ""
    if _body and "<!-- grape-auto-comment -->" not in _body:
        _body = _body + "\n<!-- grape-auto-comment -->"
    # Issue #707：state 非法值前置校验 + 归一——本地 400 文案指向契约字段 `state`
    # （合法取值 reopen/close，中性别名 open/opened/closed），不再透传上游
    # `state_event` 字样的 400 误导文案
    _state = body.state or ""
    if _state:
        _state = str(_state).strip().lower()
        if _state not in _UPDATE_STATE_NEUTRAL:
            raise HTTPException(status_code=400, detail={
                "code": 400,
                "message": f"'state': input must in [reopen, close] "
                           f"(aliases: open, opened, closed are accepted and mapped)"})
    neutral = {
        # Issue #581：labels 双通道适配（GitHub→数组 / GitCode→逗号字符串透传）
        "labels": _provider_labels(p, body.labels),
        "assignee": body.assignee,
        "milestone_id": body.milestone_id or None,
        "state": _state,
        "body": _body,
    }
    payload = translate_inbound(p, "issues", "update", neutral, {"owner_repo": f"{owner}/{repo}"})
    _want_milestone = bool(body.milestone_id)
    _resolved_milestone = None
    # Issue #1005：milestone_id 取值口径 id 优先、number 兼容。先解析到具体里程碑，
    # 再以解析出的上游 number 写 PATCH（id 与 number 混用不再静默落空）。
    if p == "gitcode" and _want_milestone:
        _milestones = _fetch_gitcode_milestones(owner, repo, user, p, want=body.milestone_id)
        _resolved_milestone = _resolve_gitcode_milestone(_milestones, body.milestone_id)
        if _resolved_milestone is None:
            _available = []
            for _m in _milestones:
                if not isinstance(_m, dict) or _m.get("number") is None:
                    continue
                _n = str(_m.get("number"))
                if _n not in _available:
                    _available.append(_n)
            _avail_txt = ", ".join(_available[:20]) or "无"
            raise HTTPException(status_code=400, detail={
                "code": 400,
                "message": f"milestone_id={body.milestone_id} 不存在：可访问的里程碑列表中未找到 "
                           f"id/number 匹配项（里程碑已删除或当前身份无权限查看）。"
                           f"合法取值来源为 `grape git-repo milestones` 返回的 `id`（优先）或 `number` 字段。"
                           f"当前仓库可用 number: {_avail_txt}"})
        try:
            _resolved_number = int(_resolved_milestone.get("number"))
        except (TypeError, ValueError):
            raise HTTPException(status_code=422, detail={
                "code": 422,
                "message": f"milestone_id={body.milestone_id} 匹配到里程碑但缺少可用 number，无法写上游 PATCH"})
        payload["milestone"] = _resolved_number
    # Issue #640：GitCode PATCH /repos/{owner}/issues/{number} body 必填 repo+title（路径不含 repo 维度
    # 的必填语义），仅传元数据字段（如 milestone_id）会被上游 400 拒绝 —— 复用 close_issue 已验证模式：
    # 服务端自动补 repo，title 缺失时先 GET 回取当前值回填，调用方无感
    _pre_issue = None
    if p == "gitcode" and (_want_milestone or "title" not in payload):
        payload.setdefault("repo", f"{owner}/{repo}")
        try:
            _pre_issue = _vcs_req(p, "GET", _vcs_url(owner, repo, f"/issues/{number}", None, p),
                                  _repo_token(owner, repo, user, p))
        except HTTPException:
            raise
        except Exception as _e:
            raise HTTPException(status_code=502, detail={
                "code": 502,
                "message": f"GitCode PATCH 必填 title 回取失败（update_issue #{number}）：{_e}"})
        if "title" not in payload:
            _title = str(_pre_issue.get("title") or "").strip() if isinstance(_pre_issue, dict) else ""
            if not _title:
                # Issue #640 审查意见：title 必填非空，回取为空仍发空串会被上游 400 拒绝 → 明确报错
                raise HTTPException(status_code=502, detail={
                    "code": 502,
                    "message": f"GitCode PATCH 必填 title 回取为空（update_issue #{number}），无法补齐必填字段"})
            payload["title"] = _title
    # Issue #1076/#1005：写入前已把 milestone_id 解析为具体里程碑（id 优先、number 兼容），
    # 不存在的值在解析阶段已 400 拦截，绝不发 PATCH，既有 milestone 因而不被清空。
    # 这里仅记录请求前的 milestone number，供写后回读失败时回滚。
    _before_ms_num = None
    if p == "gitcode" and _want_milestone:
        _before_ms = _pre_issue.get("milestone") if isinstance(_pre_issue, dict) else None
        _before_raw = _before_ms.get("number") if isinstance(_before_ms, dict) else _before_ms
        try:
            _before_ms_num = int(_before_raw) if _before_raw is not None else None
        except (TypeError, ValueError):
            _before_ms_num = None
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
    # Issue #640/#1005/#1076：milestone 写后回读校验（上游有静默忽略先例，PATCH 响应 200 不可信）。
    # 仅当请求含 milestone_id 且 provider=gitcode 时执行一次 GET 回读：
    #   - 未落库或落库 number ≠ 解析出的目标 number → 422 可读错误（不再 502），
    #     并尽量回滚到请求前的 milestone，保证失败路径不破坏既有值；502 仅保留给回读时上游真实不可用。
    #   - 一致 → 响应回显 milestone（补 id，上游 PATCH 响应可能不回显，以回读值为准）
    if p == "gitcode" and _want_milestone and isinstance(resp, dict) and "error" not in resp:
        try:
            _after = _vcs_req(p, "GET", _vcs_url(owner, repo, f"/issues/{number}", None, p),
                              _repo_token(owner, repo, user, p))
        except HTTPException:
            raise
        except Exception as _e:
            raise HTTPException(status_code=502, detail={
                "code": 502,
                "message": f"milestone 回读校验失败（update_issue #{number}，上游不可用）：{_e}"})
        _ms = _after.get("milestone") if isinstance(_after, dict) else None
        _ms_raw = _ms.get("number") if isinstance(_ms, dict) else _ms
        try:
            _ms_num = int(_ms_raw) if _ms_raw is not None else None
        except (TypeError, ValueError):
            _ms_num = None
        if _ms_num != _resolved_number:
            _cur = _ms_num if _ms_num is not None else "空"
            _rollback_note = ""
            # 失败原子性：仅当 PATCH 后值与原值不同（被清空/改成其它）时回滚到原里程碑。
            if _before_ms_num is not None and _ms_num != _before_ms_num:
                _rollback_ok = False
                try:
                    _vcs_req(p, "PATCH", _vcs_url(owner, repo, f"/issues/{number}", None, p),
                             _repo_token(owner, repo, user, p),
                             {"repo": f"{owner}/{repo}", "title": payload.get("title", ""),
                              "milestone": _before_ms_num})
                    _rollback_ok = True
                except Exception as _re:
                    log.warning(f"milestone rollback (update_issue #{number}) failed: {_re}")
                    _rollback_note = f"；回滚原 milestone={_before_ms_num} 失败"
                if _rollback_ok:
                    # Issue #1076 审查：回滚 PATCH 可能被上游静默忽略，必须回读确认才能宣称已回滚。
                    try:
                        _rolled = _vcs_req(p, "GET", _vcs_url(owner, repo, f"/issues/{number}", None, p),
                                           _repo_token(owner, repo, user, p))
                        _rolled_ms = _rolled.get("milestone") if isinstance(_rolled, dict) else None
                        _rolled_raw = _rolled_ms.get("number") if isinstance(_rolled_ms, dict) else _rolled_ms
                        try:
                            _rolled_num = int(_rolled_raw) if _rolled_raw is not None else None
                        except (TypeError, ValueError):
                            _rolled_num = None
                    except Exception as _re:
                        log.warning(f"milestone rollback readback (update_issue #{number}) failed: {_re}")
                        _rollback_note = (f"；已请求回滚为原 milestone={_before_ms_num}，"
                                          f"但回读确认失败，未确认恢复")
                    else:
                        if _rolled_num == _before_ms_num:
                            _rollback_note = f"；已回滚为原 milestone={_before_ms_num}"
                        else:
                            _rolled_txt = _rolled_num if _rolled_num is not None else "空"
                            _rollback_note = (f"；回滚原 milestone={_before_ms_num} 后回读仍为 "
                                              f"{_rolled_txt}，原值未恢复")
            raise HTTPException(status_code=422, detail={
                "code": 422,
                "message": f"milestone_id={body.milestone_id} 未生效（上游忽略）：上游 PATCH 返回 200 但回读校验 "
                           f"milestone={_cur}（与请求不一致——新值可能被上游静默忽略）。"
                           f"合法取值来源为 `grape git-repo milestones` 返回的 `id`（优先）或 `number` 字段"
                           f"{_rollback_note}"})
        resp = dict(resp)
        resp["milestone"] = _normalize_milestone_states(_ms)
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


class ArtifactCleanBody(BaseModel):
    dry_run: bool = Field(
        True,
        title="Dry run",
        description="When true (default) only reports what would be deleted plus the guard "
                    "impact; no upstream DELETE is issued.",
    )
    ref: str = Field("", title="Branch/tag/sha", description="Defaults to the repo default branch.")
    message: str = Field("", title="Commit message", description="Optional cleanup commit message.")


@router.post("/{owner}/{repo}/issues/{number}/artifacts/clean",
             operation_id="gitrepo_issue_artifacts_clean",
             summary="Precheck and clean docs/issue-N artifacts (Issue #996)",
             description="Lists `docs/issue-N/*`, runs the committed-guard impact precheck, and only "
                         "when no guard script references the paths deletes them via the contents "
                         "API. `dry_run` defaults to true, so a plain call reports impact and "
                         "deletions without touching the repo. Guard hits always skip deletion "
                         "(never bypassed). Failures are returned per path, not swallowed.")
def clean_issue_artifacts_endpoint(owner: str, repo: str, number: int, body: ArtifactCleanBody | None = None,
                                   user: dict = Depends(get_current_user_or_api_key),
                                   p: str = Depends(_provider)):
    body = body or ArtifactCleanBody(dry_run=True, ref='', message='')
    from src.repo_key import to_repo_key
    from src.runtime.cleanup import ArtifactScope, precheck_issue_artifacts
    from src.vcs.guard_impact import delete_issue_artifacts

    token = _repo_token(owner, repo, user, p)
    ref = (body.ref or "").strip() or _default_ref(p, owner, repo, user)
    scope = ArtifactScope(repo_key=to_repo_key(f"{owner}/{repo}", p), ref=ref,
                          issue_number=number, token=token, provider=p)
    pre = precheck_issue_artifacts(scope)
    paths = pre.get("paths") or []
    out = {"issue": number, "ref": ref, "paths": paths, "guard_impact": pre.get("guard_impact"),
           "deleted": [], "skipped": [], "failed": [], "dry_run": bool(body.dry_run)}
    if not paths:
        return out
    impact = pre.get("guard_impact") or {}
    if impact.get("breaking"):
        # 守卫引用命中：绝不删除，如实回传命中脚本供 QA/pipeline 展示。
        out["skipped"] = paths
        log.warning(f"artifact clean skipped (guard hit) repo={owner}/{repo} issue={number} "
                    f"scripts={impact.get('scripts')}")
        return out
    if body.dry_run:
        return out
    result = delete_issue_artifacts(scope.repo_key, ref, pre.get("targets") or [], token=token,
                                    provider=p, message=body.message)
    out["deleted"] = result.get("deleted") or []
    out["failed"] = result.get("failed") or []
    if out["failed"]:
        out["guard_impact"] = None
    return out


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
    # Issue #728：自动关联走上下文过滤层——调用方未显式传 related_issue_nums 且
    # body 非空时，剥离代码块/行内反引号/引用块后提取 Issue 号（非语义语境的 #N
    # 不再被自动关联捕获；显式 related_issue_nums 优先，不受过滤影响）。
    if not related and _body:
        from src.vcs.issue_ref_filter import extract_related_issue_nums
        related = extract_related_issue_nums(_body)
    # Issue #1739：关联 Issue 的同一流水线 qa 最新结论为 fail 时，不得因建 PR 自动关闭
    # Issue（GitCode close_related_issue 会在合入时关闭关联 Issue）——强制关闭开关为 false，
    # 并在 Issue 上标注 QA 失败原因与 PR，便于回流修复；门禁异常 fail-open 保持原行为。
    _auto_close = body.auto_close_issues
    _qa_blocked: list[tuple[str, int, dict]] = []
    if related and _auto_close:
        try:
            from src.api.qa_gate import qa_gate_check
            from src.repo_key import to_repo_key

            _rk = to_repo_key(f"{owner}/{repo}", p)
            # 多 Issue PR：逐个检查，任一失败即关闭自动关闭开关，并记录全部失败
            # Issue 以便逐一标注（不在首个失败处 break）
            for _rnum in related:
                _qa_info = qa_gate_check(_rk, _rnum)
                if _qa_info.get("blocked"):
                    _auto_close = False
                    _qa_blocked.append((_rk, _rnum, _qa_info))
                    log.info(f"qa gate: force auto_close_issues=false "
                             f"{_rk}/#{_rnum} (qa seq={_qa_info.get('event_seq')})")
        except Exception as _e:
            log.warning(f"qa gate: create_pull check failed (fail-open) "
                        f"{owner}/{repo}: {_e}")
    neutral = {
        "title": body.title,
        "head": body.head,
        "base": body.base,
        "body": _body,
        "related_issue_nums": related,
        "auto_close_issues": _auto_close,
    }
    payload = translate_inbound(p, "pulls", "create", neutral, {"owner_repo": f"{owner}/{repo}"})
    resp = _vcs_req(p, "POST", _vcs_url(owner, repo, "/pulls", None, p), _repo_token(owner, repo, user, p), payload)
    # Issue #1739：QA 失败被门禁拦截时，在建 PR 成功后补充 Issue 标注（关联本次 PR）。
    # 多 Issue PR：每个 qa 失败的关联 Issue 都要标注，不能只标首个。
    if _qa_blocked:
        try:
            from src.api.qa_gate import annotate_qa_failure
            from src.outbound_ledger import extract_content_id

            _pr_s = str(extract_content_id(resp, "issue") or "").strip()
            _pr_num = int(_pr_s) if _pr_s.isdigit() else 0
            for _b_rk, _b_num, _b_info in _qa_blocked:
                annotate_qa_failure(_b_rk, _b_num, _b_info, pr_number=_pr_num)
        except Exception as _e:
            log.warning(f"qa gate: create_pull annotation failed (fail-open) "
                        f"{owner}/{repo}: {_e}")
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


@router.delete("/{owner}/{repo}/pulls/{number}/issues/{issue_number}", operation_id="gitrepo_pulls_issues_unlink",
               summary="Unlink issue from pull request",
               description="Removes the PR-Issue association (Issue #728). Explicit dual path params: PR number + issue number. "
                           "GitCode: verifies the association exists (GET PR issues list) before rewriting inner_issue_nums via PATCH; "
                           "missing association is an idempotent no-op returning unlinked=false. "
                           "GitHub: no native PR-Issue association store, always an idempotent no-op.")
def unlink_pull_issue(owner: str, repo: str, number: int, issue_number: int,
                      user: dict = Depends(get_current_user_or_api_key), p: str = Depends(_provider)):
    # Issue #728：关联解除端点（幂等、双参数显式校验——PR 号 + Issue 号均为路径参数）。
    _token = _repo_token(owner, repo, user, p)
    if p != "gitcode":
        # GitHub 等无原生 PR↔Issue 关联存储（其关联仅为 body 文本引用），幂等 no-op。
        return {"unlinked": False, "pr_number": number, "issue_number": issue_number,
                "reason": f"provider {p} has no native PR-Issue association"}
    # 1. GET PR 详情，校验关联存在（不存在 → 幂等成功，不发 PATCH）
    pr = _vcs_req(p, "GET", _vcs_url(owner, repo, f"/pulls/{number}", None, p), _token)
    _issues = pr.get("issues") if isinstance(pr, dict) else None
    _nums = [it.get("number") for it in _issues
             if isinstance(it, dict) and isinstance(it.get("number"), int)] if isinstance(_issues, list) else []
    if issue_number not in _nums:
        return {"unlinked": False, "pr_number": number, "issue_number": issue_number,
                "reason": "association not found"}
    # 2. PATCH 剩余关联（inner_issue_nums 复用 gitcode 关联编码 o/r#N；清空传空串）
    _remaining = [n for n in _nums if n != issue_number]
    _inner = ",".join(f"{owner}/{repo}#{n}" for n in _remaining)
    resp = _vcs_req(p, "PATCH", _vcs_url(owner, repo, f"/pulls/{number}", None, p), _token,
                    {"inner_issue_nums": _inner})
    # Issue #453 台账（对齐 update_pull：PR 变更入账，best-effort 不阻断）
    try:
        from src.outbound_ledger import record_outbound
        from src.repo_key import to_repo_key
        record_outbound(p, to_repo_key(f"{owner}/{repo}", p), "pr", str(number),
                        issue_number=str(issue_number))
    except Exception as _e:
        log.warning(f"outbound_ledger record (unlink_pull_issue) failed: {_e}")
    return {"unlinked": True, "pr_number": number, "issue_number": issue_number,
            "remaining_issue_nums": _remaining, "pr": resp}


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
    _token = _repo_token(owner, repo, user, p)
    # Issue #752（M1）：合入前冲突标记扫描门禁——PR 变更文件硬命中 `<<<<<<< `/`>>>>>>> `
    # → 阻断 409（防残留进入基线分支，AC1）；仅孤立 `=======` 软命中 → 告警放行（防误杀
    # markdown 下划线）；扫描自身异常 fail-open。每次决策写合入日志（AC3）。
    from src.api.merge_gate import run_merge_gate, run_post_merge_gate, get_pull_base, record_gate_log
    _repo_key = f"{owner}/{repo}"
    # PR #874 审查修正：合入前记录 base 分支 sha（post-merge compare 扫描的起点）
    _base_info = get_pull_base(owner, repo, number, p, _token)
    # Issue #1739：合入前 QA 结论门禁——关联 Issue 同一流水线 qa 最新结论为 fail 时
    # 阻断合入（合入会触发 VCS 自动关闭 Issue，未经验证的需求将被误判为已解决）。
    # 关联来源 = PR body 的 #N 引用 + PR 详情 issues 关联（inner_issue_nums 显式关联的
    # PR body 可能不含 #N，只读 body 会被绕过）。仅本门禁自身的 409 才阻断合入；
    # 上游临时错误（PR 详情 404/502 等）必须 fail-open，不得破坏合入可用性。
    _qa_blocks: list[tuple[str, int, dict]] = []
    try:
        from src.api.qa_gate import qa_gate_check
        from src.repo_key import to_repo_key
        from src.vcs.issue_ref_filter import extract_related_issue_nums

        _pr_detail = _vcs_req(p, "GET", _vcs_url(owner, repo, f"/pulls/{number}", None, p), _token)
        _pr_body = ""
        _rel_nums = []
        if isinstance(_pr_detail, dict):
            _pr_body = str(_pr_detail.get("body") or _pr_detail.get("description") or "")
            _linked = _pr_detail.get("issues")
            if isinstance(_linked, list):
                for _it in _linked:
                    _n = _it.get("number") if isinstance(_it, dict) else None
                    if isinstance(_n, int) and _n > 0:
                        _rel_nums.append(_n)
        if _pr_body:
            _rel_nums.extend(extract_related_issue_nums(_pr_body))
        _rel_nums = list(dict.fromkeys(_rel_nums))  # 去重且保序
        _qa_rk = to_repo_key(f"{owner}/{repo}", p)
        for _rel_num in _rel_nums:
            _qa_info = qa_gate_check(_qa_rk, _rel_num)
            if _qa_info.get("blocked"):
                # 多 Issue PR：记录全部失败 Issue，逐一标注（不在首个失败处 break）
                _qa_blocks.append((_qa_rk, _rel_num, _qa_info))
    except Exception as _e:
        log.warning(f"qa gate: merge pre-check failed (fail-open) {owner}/{repo}#{number}: {_e}")
    if _qa_blocks:
        try:
            from src.api.qa_gate import annotate_qa_failure

            for _b_rk, _b_num, _b_info in _qa_blocks:
                annotate_qa_failure(_b_rk, _b_num, _b_info, pr_number=number)
        except Exception as _e:
            log.warning(f"qa gate: merge annotation failed (fail-open) "
                        f"{owner}/{repo}#{number}: {_e}")
        _qa_rk, _rel_num, _qa_info = _qa_blocks[0]
        record_gate_log(_repo_key, number, "pre_merge_qa", "blocked",
                        {"qa_failed": {"issue": _rel_num,
                                       "event_seq": _qa_info.get("event_seq"),
                                       "status": _qa_info.get("status")},
                         "qa_failed_issues": [b[1] for b in _qa_blocks]})
        _all_ids = ", ".join(f"#{b[1]}" for b in _qa_blocks)
        raise HTTPException(status_code=409, detail={
            "code": 409,
            "message": (f"merge blocked by QA gate (Issue #1739): latest qa conclusion "
                        f"is fail for issue(s) {_all_ids} (seq={_qa_info.get('event_seq')}); "
                        f"fix and re-run qa before merge. reason: "
                        f"{(_qa_info.get('reason') or '')[:200]}"),
            "gate": {"phase": "pre_merge_qa", "issue": _rel_num,
                     "issues": [b[1] for b in _qa_blocks],
                     "event_seq": _qa_info.get("event_seq"),
                     "status": _qa_info.get("status")}})
    _gate = run_merge_gate(owner, repo, number, p, _token)
    if _gate.get("decision") == "blocked":
        record_gate_log(_repo_key, number, "pre_merge", "blocked", _gate)
        # 命中文件清单入 message（承接基线分支 Issue #754 语义：报告 path(markers)）；
        # Issue #993（M3）：仅结构缺陷命中时不得谎称冲突标记门禁，按来源分别措辞。
        _conflict = _gate.get("conflict_hard_hits") or {}
        _structure = _gate.get("structure_hard_hits") or {}
        _parts = []
        _trace_parts = []
        if _conflict:
            _files = ", ".join(f"{_p}({_n})" for _p, _n in _conflict.items())
            _parts.append("merge blocked by conflict-marker gate (Issue #752): "
                          f"unresolved merge conflict markers found in PR changed files: {_files}")
            _trace_parts.append(f"unresolved merge conflict markers in PR files: {_files} "
                                f"(Issue #752)")
        if _structure:
            _sfiles = ", ".join(f"{_p}({_n})" for _p, _n in _structure.items())
            _parts.append("merge blocked by structural gate (Issue #993): "
                          f"structural defects in rebuilt docs: {_sfiles}")
            _trace_parts.append(f"structural defects in rebuilt docs: {_sfiles} "
                                f"(Issue #993)")
        _msg = "; ".join(_parts) or "merge blocked by merge gate"
        # 阻断留痕①（承接 Issue #754）：台账 pr_merge_blocked，best-effort 不影响阻断本身
        try:
            from src.outbound_ledger import record_outbound
            from src.repo_key import to_repo_key
            record_outbound(p, to_repo_key(f"{owner}/{repo}", p), "pr_merge_blocked", str(number),
                            issue_number=str(number), origin_channel="api")
        except Exception as _le:
            log.warning(f"outbound_ledger record (pr_merge_blocked) failed: {_le}")
        # 阻断留痕②（承接 Issue #754）：PR 评论 best-effort（GitCode /pulls、GitHub /issues 通道）
        try:
            _trace_body = ("[Grape] merge blocked: "
                           + "; ".join(_trace_parts or ["merge gate blocked"])
                           + ". Manual fix required before merge.")
            _trace_path = f"/issues/{number}/comments" if p == "github" else f"/pulls/{number}/comments"
            _vcs_req(p, "POST", _vcs_url(owner, repo, _trace_path, None, p), _token,
                     {"body": _trace_body})
        except Exception as _ce:
            log.warning(f"merge gate block-trace PR comment failed: {_ce}")
        raise HTTPException(status_code=409, detail={
            "code": 409,
            "message": _msg,
            "gate": {"phase": "pre_merge", "hard_hits": _gate.get("hard_hits", {}),
                     "soft_hits": _gate.get("soft_hits", {})}})
    if _gate.get("decision") == "warned":
        record_gate_log(_repo_key, number, "pre_merge", "warned", _gate)
        log.warning(f"merge gate soft warning {owner}/{repo}#{number}: "
                    f"{_gate.get('soft_hits')}")
    else:
        record_gate_log(_repo_key, number, "pre_merge", _gate.get("decision", "passed"), _gate)
    resp = _vcs_req(p, "PUT", _vcs_url(owner, repo, f"/pulls/{number}/merge", None, p),
                    _token, _payload)
    # Issue #752（M1，PR #874 审查修正）：合入后扫描 base 分支合入结果（pre-merge base sha
    # → 合入后 tip 的 compare diff）——覆盖自动复测等旁路写入 base 分支的残留（PR files
    # 复扫永远与 pre_merge 同结果，检不出旁路）；命中告警+日志（合入已完成无法回滚，
    # 人工裁决），未命中记录 passed。fail-open 不影响返回。
    _merge_sha = resp.get("sha") or "" if isinstance(resp, dict) else ""
    _post = run_post_merge_gate(owner, repo, number, p, _token,
                                _base_info.get("ref", ""), _base_info.get("sha", ""),
                                _merge_sha)
    _post_decision = "alarmed" if _post.get("decision") in ("blocked", "warned") \
        else _post.get("decision", "passed")
    record_gate_log(_repo_key, number, "post_merge", _post_decision, _post)
    if _post_decision == "alarmed":
        log.warning(f"merge gate post-merge alarm {owner}/{repo}#{number}: "
                    f"hard={_post.get('hard_hits')} soft={_post.get('soft_hits')}")
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
                         "commit_id/path/line and returns 422 for body-only payloads. GitCode keeps /pulls/{n}/comments. "
                         "Issue #754: GitCode 405 (PR already merged, not reviewable) automatically falls back to "
                         "POST /issues/{number}/comments (equivalent trace channel, PR-is-issue semantics).")
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
    # Issue #754：GitCode 已 merged PR 上 POST /pulls/{n}/comments 返回 405
    # （not allowed to be reviewed after being merged）→ 等价留痕通道回写兜底：
    # PR=issue 语义（同 #585 方案），改走 /issues/{n}/comments，闭环说明不再散落 Issue。
    _fallback_path = None if p == "github" else f"/issues/{number}/comments"
    _token = _repo_token(owner, repo, user, p)
    try:
        resp = _vcs_req(p, "POST", _vcs_url(owner, repo, _comments_path, None, p),
                        _token, {"body": _body})
    except HTTPException as _e:
        if _fallback_path and getattr(_e, "status_code", None) == 405:
            log.warning(f"Issue #754 PR comment 405 on {_comments_path} (merged PR?) "
                        f"→ fallback to {_fallback_path}")
            resp = _vcs_req(p, "POST", _vcs_url(owner, repo, _fallback_path, None, p),
                            _token, {"body": _body})
        else:
            raise
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


def _normalize_pull_files_diff(resp):
    """Issue #731: normalize pull files diff field path to unified ``.patch.diff``.

    GitCode may return file objects with a top-level ``.diff`` string (mixed structure
    seen from the platform API). Move it into ``.patch = {"diff": ...}`` (merging with
    existing patch metadata) and drop the top-level key. Also convert legacy
    string-valued ``.patch`` into the object form. Consistency assertion afterwards:
    every file object with diff content must expose it at ``.patch.diff``; abnormal
    structures are logged via log.warning instead of being silently passed through.

    Issue #750: string-valued ``.patch`` may be a doubly JSON-encoded payload
    (``"{\\"diff\\": ...}"``). Try an inner ``json.loads`` first: if it decodes into a
    dict with a ``diff`` key, that value is the bare diff text; otherwise treat the
    string itself as a raw diff. ``.patch.diff`` is therefore always a bare diff text
    string, so a single ``json.loads`` of the CLI output suffices downstream.
    """
    if not isinstance(resp, list):
        return resp
    for item in resp:
        if not isinstance(item, dict):
            continue
        top_diff = item.get("diff")
        patch = item.get("patch")
        if isinstance(top_diff, str) and top_diff:
            if not isinstance(patch, dict) or not patch.get("diff"):
                merged = dict(patch) if isinstance(patch, dict) else {}
                merged["diff"] = top_diff
                item["patch"] = merged
            item.pop("diff", None)
        elif isinstance(patch, str) and patch:
            diff_text = patch
            try:
                decoded = json.loads(patch)
            except (ValueError, TypeError):
                decoded = None
            if isinstance(decoded, dict) and isinstance(decoded.get("diff"), str) \
                    and decoded["diff"]:
                diff_text = decoded["diff"]
            item["patch"] = {"diff": diff_text}
        final = item.get("patch")
        if not isinstance(final, dict) or not final.get("diff"):
            log.warning(f"Pull files diff consistency check failed for {item.get('filename')!r}: "
                        "no non-empty diff at .patch.diff after normalization")
    return resp



@router.get("/{owner}/{repo}/pulls/{number}/files", operation_id="gitrepo_pulls_files", summary="List PR changed files",
            description="Lists files changed in a pull request. Issue #585: page/per_page pass through to the provider "
                        "(GitHub default per_page=30 silently truncated large PRs; default here is per_page=100). "
                        "For changed_files>100 PRs, page through manually; tree --depth recursive is a fallback for huge PRs. "
                        "Issue #731: file diff content is always normalized to the unified field path .patch.diff; "
                        "top-level .diff (if returned by the provider) is merged into .patch.diff. "
                        "Issue #750: doubly JSON-encoded patch strings are inner-decoded, so .patch.diff "
                        "is always a bare diff text string (single json.loads downstream).")
def list_pull_files(owner: str, repo: str, number: int, page: int = Query(1, ge=1),
                    per_page: int = Query(100, ge=1, le=100),
                    user: dict = Depends(get_current_user_or_api_key), p: str = Depends(_provider)):
    # Issue #585 Bug 2: 未透传分页参数时 GitHub 默认 per_page=30，>30 文件的 PR 被静默截断
    # Issue #731: 出参归一化——diff 字段统一收敛到 .patch.diff（详见 _normalize_pull_files_diff）
    resp = _vcs_req(p, "GET", _vcs_url(owner, repo, f"/pulls/{number}/files",
                                       {"page": page, "per_page": per_page}, p),
                    _repo_token(owner, repo, user, p))
    return _normalize_pull_files_diff(resp)