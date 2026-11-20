"""GitLab provider 中性翻译（S107/G2：方言层补齐）。

GitLab API v4 与 v5 族的关键偏离（token_resolver 侧 GitLabProvider 已处理
同款形态，本模块保持一致）：
- 仓库资源路径 /projects/{owner}%2F{repo}（URL 编码全路径），非 /repos/...；
- 凭据走 PRIVATE-TOKEN header（卡 api_auth=private_token）；
- 正文语义 body → description；PR = merge_requests（head/base → source/target_branch）；
- 指派人 assignee_id 为 GitLab 用户 id（跨平台不可映射 → drop，不发）。
"""
from . import base
import json as _json
import urllib.request as _ur
import urllib.error as _ue
import urllib.parse as _up

# 上游异常类型走 owner 域 public
from src.domains.mgmt import public as _mgmt_pub
UpstreamAPIError = _mgmt_pub.UpstreamAPIError

API_BASE = "https://gitlab.com/api/v4"

# 网关子路径 → GitLab 原生路径（_vcs_url 消费）
PATH_MAP = {"/pulls": "/merge_requests"}


def repo_segment(owner: str, repo: str) -> str:
    """GitLab 仓库资源段：/projects/{owner}%2F{repo}（全路径 URL 编码）。"""
    return "projects/" + _up.quote(f"{owner}/{repo}", safe="")


def request(method, url, token, body=None):
    headers = {"Content-Type": "application/json", "Accept": "application/json"}
    if token:
        headers["PRIVATE-TOKEN"] = token
    data = _json.dumps(body).encode() if body else None
    req = _ur.Request(url, data=data, headers=headers, method=method)
    try:
        from src.kernel.upstream_http import open_with_policy as _open_policy
        with _open_policy(req, timeout=30) as resp:
            raw = resp.read().decode()
            return _json.loads(raw) if raw.strip() else {}
    except _ue.HTTPError as e:
        raw = e.read().decode(errors="replace")
        raise UpstreamAPIError(method, url, e.code, raw)
    except _ue.URLError as e:
        raise RuntimeError(f"Cannot reach GitLab API: {e.reason}")


def bot_token(repo_key):
    from src.domains.mgmt.public import get_repo_bot_token
    return get_repo_bot_token(repo_key=repo_key) or ""


def user_token(username, user_id):
    from src.api.deps import get_user_token_for
    return get_user_token_for(username, user_id=user_id, provider="gitlab")


_QUERY_STATE = {"open": "opened", "closed": "closed", "all": "all"}
_UPDATE_STATE = {"open": "reopen", "opened": "reopen", "reopen": "reopen",
                 "closed": "close", "close": "close"}


def _drop(v, neutral, native, ctx):
    """显式丢弃（跨平台不可映射字段：assignee_id / related_issues 机制）。"""


_RULES = {
    ("issues", "query"): {"state": {"native": "state", "values": _QUERY_STATE}},
    ("issues", "create"): {
        "title": "title",
        "body": "description",
        # assignee（用户名）跨平台不可映射为 GitLab assignee_id → drop
        "assignee": {"native": None, "transform": _drop},
    },
    ("issues", "update"): {
        "labels": "labels",
        "milestone_id": "milestone_id",
        "state": {"native": "state_event", "values": _UPDATE_STATE},
        "body": "description",
        "assignee": {"native": None, "transform": _drop},
    },
    ("pulls", "create"): {
        "title": "title",
        "head": "source_branch",
        "base": "target_branch",
        "body": "description",
        "related_issue_nums": {"native": None, "transform": _drop},
        "auto_close_issues": {"native": None, "transform": _drop},
        # GitLab 建 MR 无 prune_source_branch；其等价能力是 MR 级 remove_source_branch
        # （本改动暂不映射，先丢弃避免脏字段）。
        "prune_source_branch": {"native": None, "transform": _drop},
    },
}


def translate(resource: str, op: str, neutral: dict, ctx: dict) -> dict:
    return base.apply_rules(_RULES.get((resource, op), {}), neutral, ctx)


def query_state(state: str) -> str:
    return _QUERY_STATE.get(state, state)

# PR/MR 网页路径段（前端 PR 详情链接与 QA 门禁链接共用；能力位驱动，业务侧零平台判断）。
PR_WEB_PATH = "merge_requests"

# Issue #3580/P4：GitLab 实例（含自建）托管全流程已接入（webhook_dialect=gitlab_v4）。
HOSTING_FULL_FLOW = True


# ===================== 仓库树 / 文件内容（Issue #3580 复审，对齐 PR #1854） =====================
# 网关对上层是「github 契约」：/git/trees/{ref} 与 /contents/{path}。GitLab 方言没有这两个
# 端点——树是 /repository/tree?path=&ref=（且原生支持 recursive=true），文件内容是
# /repository/files/{整路径 URL 编码}?ref=。这里提供「URL 构造 + 返回归一」两组模块钩子，
# 路由层据此零平台分支（无钩子的平台继续走原 /git/trees、/contents 语义）。

TREE_RECURSIVE = True          # /repository/tree?recursive=true 原生支持（返回扁平全路径）

_PER_PAGE = 100


def _project_base(api_base: str, owner: str, repo: str) -> str:
    return f"{api_base.rstrip('/')}/projects/{_up.quote(f'{owner}/{repo}', safe='')}"


def tree_url(api_base, owner, repo, ref, recursive=False, page=1, per_page=_PER_PAGE) -> str:
    params = {"ref": ref or "HEAD", "per_page": str(per_page), "page": str(page)}
    if recursive:
        params["recursive"] = "true"
    return _project_base(api_base, owner, repo) + "/repository/tree?" + _up.urlencode(params)


def dir_url(api_base, owner, repo, path, ref, page=1, per_page=_PER_PAGE) -> str:
    params = {"path": path or "", "ref": ref or "HEAD",
              "per_page": str(per_page), "page": str(page)}
    return _project_base(api_base, owner, repo) + "/repository/tree?" + _up.urlencode(params)


def file_url(api_base, owner, repo, path, ref) -> str:
    # GitLab files API 要求**整条路径**编码（含 /），不能逐段拼接
    return (_project_base(api_base, owner, repo) + "/repository/files/"
            + _up.quote(path or "", safe="") + "?" + _up.urlencode({"ref": ref or "HEAD"}))


def normalize_tree(entries) -> list:
    """GitLab {id,name,type(tree|blob),path,mode,size} → 网关条目（github 契约）。"""
    out = []
    for e in entries or []:
        if not isinstance(e, dict):
            continue
        is_tree = str(e.get("type")) == "tree"
        _path = str(e.get("path") or "")
        out.append({
            "type": "tree" if is_tree else "file",
            "mode": str(e.get("mode") or ("040000" if is_tree else "100644")),
            "path": _path,
            "name": str(e.get("name") or _path.rsplit("/", 1)[-1] or ""),
            "sha": str(e.get("id") or e.get("sha") or ""),
            "size": 0 if is_tree else int(e.get("size") or 0),
        })
    return out


def normalize_file(data, path: str = "") -> dict:
    """GitLab 文件对象 → 网关文件条目（content 统一 base64，与 github contents 同形）。"""
    import base64 as _b64
    if not isinstance(data, dict):
        return data
    content = str(data.get("content") or "")
    if str(data.get("encoding") or "").lower() != "base64":
        content = _b64.b64encode(content.encode()).decode()
    _path = str(data.get("file_path") or path or "")
    return {
        "type": "file",
        "mode": "100644",
        "path": _path,
        "name": str(data.get("file_name") or _path.rsplit("/", 1)[-1] or ""),
        "sha": str(data.get("last_commit_id") or data.get("blob_id")
                   or data.get("content_sha256") or ""),
        "size": int(data.get("size") or 0),
        "content": content,
        "encoding": "base64",
    }
