"""GitLab provider：中性翻译 + 原生 URL 构造 + 响应字段归一。

与 gitcode/github 的差异：
- 路径风格是 `/projects/{urlencode(owner/repo)}/...`，且 owner 可为多层（group/subgroup），
  因此 URL 由本模块 `build_url` 自行拼装（`_vcs_url` 检测到 build_url 时优先调用）。
- 实例地址是动态的（自建 GitLab，见 `get_gitlab_api_base`），不能用模块级常量。
- 字段命名不同：description/source_branch/target_branch/iid/state_event；
  Notes API 的正文字段仍是 body（note 只出现在 Note Hook 的 object_attributes 里）。
"""
import urllib.parse

from . import base

# 实例地址动态获取，这里不提供静态 API_BASE（真实 URL 统一由 build_url 生成）
API_BASE = ""

# 中性 resource/op 不支持时的提示
_UNSUPPORTED_MSG = "GitLab 暂不支持该接口: {path}"


def api_base() -> str:
    from src.vcs.gitlab import get_gitlab_api_base
    return get_gitlab_api_base()


def build_url(owner: str, repo: str, subpath: str, params: dict | None = None) -> str:
    """中性子路径 → GitLab REST v4 URL（含 query）。

    未映射的接口抛 ValueError（由调用方转为明确的 501，避免拼出错 URL 后的误导性 404）。
    """
    from src.vcs.gitlab import gitlab_project_path
    base_url = f"{api_base()}/projects/{gitlab_project_path(owner, repo)}"
    segs = [s for s in (subpath or "").strip("/").split("/") if s]
    query = {k: v for k, v in (params or {}).items() if v not in (None, "", [])}
    _is_dir = (subpath or "").endswith("/")

    if not segs:
        path = base_url
    elif segs[0] == "branches":
        path = base_url + "/repository/branches"
        if len(segs) > 1:
            path += "/" + urllib.parse.quote(segs[1], safe="")
    elif segs[0] == "commits":
        path = base_url + "/repository/commits"
        # 中性 /commits/{sha} 不能再丢弃第二段（否则返回列表而非单条提交）
        if len(segs) > 1:
            path += "/" + urllib.parse.quote(segs[1], safe="")
    elif segs[0] == "git" and len(segs) >= 3 and segs[1] == "trees":
        # 中性 /git/trees/{ref} → GitLab /repository/tree?ref={ref}
        # ref 可能含斜杠（feat/issue-11），必须把 segs[2:] 整段拼回
        path = base_url + "/repository/tree"
        query.setdefault("ref", "/".join(segs[2:]))
        query.setdefault("per_page", 100)
    elif segs[0] == "contents":
        if len(segs) == 1 or _is_dir:
            # 目录列表：GitLab 没有 contents 端点，用 repository/tree?path=
            path = base_url + "/repository/tree"
            if len(segs) > 1:
                query.setdefault("path", "/".join(segs[1:]).rstrip("/"))
            query.setdefault("per_page", 100)
        else:
            # 单文件：/repository/files/{urlencoded path}?ref=
            path = (base_url + "/repository/files/"
                    + urllib.parse.quote("/".join(segs[1:]), safe=""))
    elif segs[0] == "labels":
        path = base_url + "/labels"
        if len(segs) > 1:
            path += "/" + urllib.parse.quote(segs[1], safe="")
    elif segs[0] == "milestones":
        path = base_url + "/milestones"
        if len(segs) > 1:
            path += "/" + urllib.parse.quote(segs[1], safe="")
    elif segs[0] in ("collaborators", "members"):
        path = base_url + "/members"
    elif segs[0] == "hooks":
        path = base_url + "/hooks" + ("/" + segs[1] if len(segs) > 1 else "")
    elif segs[0] == "tags":
        path = base_url + "/repository/tags"
    elif segs[0] == "issues":
        # 中性 /issues/{n}/comments → GitLab /issues/{iid}/notes
        if len(segs) >= 3 and segs[2] == "comments":
            path = f"{base_url}/issues/{segs[1]}/notes"
            if len(segs) > 3:
                path += f"/{segs[3]}"
        else:
            path = base_url + "/issues" + ("/" + "/".join(segs[1:]) if len(segs) > 1 else "")
    elif segs[0] == "pulls":
        if len(segs) >= 2 and segs[1] == "comments":
            # 中性 /pulls/comments/{cid} 无 MR iid，GitLab 没有全局 note 端点
            raise ValueError(_UNSUPPORTED_MSG.format(path=subpath))
        if len(segs) >= 3 and segs[2] == "comments":
            path = f"{base_url}/merge_requests/{segs[1]}/notes"
            if len(segs) > 3:
                path += f"/{segs[3]}"
        elif len(segs) == 3 and segs[2] == "files":
            # GitLab 变更文件列表是 /changes，不是 /files
            path = f"{base_url}/merge_requests/{segs[1]}/changes"
        else:
            path = base_url + "/merge_requests" + ("/" + "/".join(segs[1:]) if len(segs) > 1 else "")
    else:
        raise ValueError(_UNSUPPORTED_MSG.format(path=subpath))

    # 中性 state（open/closed/all）→ GitLab 原生 state（opened/closed/all）
    if "state" in query:
        query["state"] = _QUERY_STATE.get(str(query["state"]), query["state"])

    if query:
        path += "?" + urllib.parse.urlencode(query)
    return path


def _adapt_payload(method: str, url: str, body: dict | None) -> dict | None:
    """把中性字段名适配成 GitLab 原生字段名（网关直传 payload 未走 translate 的场景）。"""
    if not isinstance(body, dict):
        return body
    out = dict(body)
    _m = str(method).upper()
    # notes 端点：GitLab REST v4 只认 body（POST/PUT /issues/{iid}/notes、
    # /merge_requests/{iid}/notes）。note 属于 webhook 载荷字段，若历史调用方
    # 仍传 note，这里统一纠正为 body，避免上游 400 "body is missing"。
    _is_notes = "/notes" in url
    if _is_notes:
        _legacy_note = out.pop("note", None)
        if "body" not in out and _legacy_note is not None:
            out["body"] = _legacy_note
    # milestones：due_on → due_date；state → state_event（close/activate）
    if "/milestones" in url:
        if "due_on" in out:
            out["due_date"] = out.pop("due_on")
        if "state" in out:
            _s = str(out.pop("state")).lower()
            out["state_event"] = "close" if _s in ("closed", "close") else "activate"
    # labels：GitLab 要求 color 带 # 前缀
    if "/labels" in url and _m == "POST" and out.get("color"):
        _c = str(out["color"])
        out["color"] = _c if _c.startswith("#") else "#" + _c
    # merge_requests 更新：中性字段 → GitLab 字段（notes 是评论不是 MR 本体，
    # 不能把正文改成 description）
    if "/merge_requests" in url and not _is_notes and _m in ("PUT", "PATCH", "POST"):
        if "body" in out:
            out["description"] = out.pop("body")
        if "head" in out:
            out["source_branch"] = out.pop("head")
        if "base" in out:
            out["target_branch"] = out.pop("base")
        if "state" in out:
            _s = str(out.pop("state")).lower()
            out["state_event"] = "close" if _s in ("closed", "close") else "reopen"
    return out


def request(method, url, token, body=None):
    """原生请求：GitLab 用 PUT 代替 PATCH；返回体做字段归一。"""
    from src.user.repo_manager import _gitlab_req
    _m = "PUT" if str(method).upper() == "PATCH" else method
    return _normalize(_gitlab_req(_m, url, token, _adapt_payload(_m, url, body)))


def bot_token(repo_key):
    from src.user.db import get_repo_token_with_refresh
    return get_repo_token_with_refresh(repo_key) or ""


def user_token(username, user_id):
    from src.api.deps import get_user_gitlab_token_with_refresh
    return get_user_gitlab_token_with_refresh(username, user_id=user_id) or ""


# ---------------- 响应字段归一 ----------------
# 上层 handler 按 GitHub/GitCode 字段名消费（number/body/html_url...），
# GitLab 返回 iid/description/web_url...，这里双向保留原字段、补齐别名。

def _normalize(obj):
    if isinstance(obj, list):
        return [_normalize(x) for x in obj]
    if not isinstance(obj, dict):
        return obj
    d = {k: _normalize(v) for k, v in obj.items()}
    if "iid" in d and "number" not in d:
        d["number"] = d["iid"]
    if "description" in d and "body" not in d:
        d["body"] = d["description"]
    if "web_url" in d and "html_url" not in d:
        d["html_url"] = d["web_url"]
    if "path_with_namespace" in d and "full_name" not in d:
        d["full_name"] = d["path_with_namespace"]
    if "http_url_to_repo" in d and "clone_url" not in d:
        d["clone_url"] = d["http_url_to_repo"]
    if "ssh_url_to_repo" in d and "ssh_url" not in d:
        d["ssh_url"] = d["ssh_url_to_repo"]
    if "visibility" in d and "private" not in d:
        d["private"] = d["visibility"] != "public"
    if "source_branch" in d and "head" not in d:
        d["head"] = d["source_branch"]
    if "target_branch" in d and "base" not in d:
        d["base"] = d["target_branch"]
    # commit 对象：GitLab id/short_id/message → sha/commit.message
    if "short_id" in d and "sha" not in d and "id" in d:
        d["sha"] = d["id"]
        if "message" in d and not isinstance(d.get("commit"), dict):
            d["commit"] = {"message": d["message"], "author": {"name": d.get("author_name", "")}}
    elif isinstance(d.get("commit"), dict) and "id" in d["commit"] and "sha" not in d["commit"]:
        d["commit"]["sha"] = d["commit"]["id"]
    # author(username) → user.login
    if isinstance(d.get("author"), dict) and "username" in d["author"] and "user" not in d:
        d["user"] = {"login": d["author"]["username"]}
    # notes 响应：note → body（供按中性字段消费的上层使用）
    if "note" in d and "body" not in d:
        d["body"] = d["note"]
    return d


# ---------------- 中性翻译 ----------------

_QUERY_STATE = {"open": "opened", "closed": "closed", "all": "all"}
_UPDATE_STATE = {
    "open": "reopen", "opened": "reopen", "reopen": "reopen",
    "closed": "close", "close": "close",
}


def _mr_body(nums, neutral, native, ctx):
    body = neutral.get("body") or ""
    line = " ".join(f"Closes #{n}" for n in nums)
    native["description"] = (body.rstrip() + "\n\n" + line).strip()


_RULES = {
    ("issues", "query"): {"state": {"native": "state", "values": _QUERY_STATE}},
    ("issues", "create"): {
        "title": "title",
        "body": "description",
        # 中性 assignee 是用户名，GitLab 只接受 assignee_ids（int）→ 丢弃，避免 400
        "assignee": {"native": None},
    },
    ("issues", "update"): {
        "labels": "labels",
        "assignee": {"native": None},
        "milestone_id": "milestone_id",
        # GitLab 用 state_event=close/reopen，不存在 state 字段
        "state": {"native": "state_event", "values": _UPDATE_STATE},
        "body": "description",
    },
    # Notes API 正文字段是 body（与 GitHub/GitCode 同名），切勿改成 note
    ("issues", "comment"): {"body": "body"},
    ("pulls", "query"): {"state": {"native": "state", "values": _QUERY_STATE}},
    ("pulls", "create"): {
        "title": "title",
        "head": "source_branch",
        "base": "target_branch",
        "body": "description",
        "related_issue_nums": {"native": None, "transform": _mr_body},
        "auto_close_issues": {"native": None},
    },
    ("pulls", "update"): {
        "title": "title",
        "head": "source_branch",
        "base": "target_branch",
        "body": "description",
        "labels": "labels",
        "state": {"native": "state_event", "values": _UPDATE_STATE},
    },
    ("pulls", "comment"): {"body": "body"},
}


def translate(resource: str, op: str, neutral: dict, ctx: dict) -> dict:
    return base.apply_rules(_RULES.get((resource, op), {}), neutral, ctx)


def query_state(state: str) -> str:
    return _QUERY_STATE.get(state, state)
