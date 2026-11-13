"""GitCode provider 中性翻译（git-repo-provider-neutral-interface-design.md §4.2/4.2.2）。"""

from . import base

API_BASE = "https://gitcode.com/api/v5"

def request(method, url, token, body=None):
    """GitCode 原生调用：写接口（POST/PATCH/PUT）body 用 x-www-form-urlencoded。

    实测实锤（2026-11-11 部署验证）：上游写接口对 JSON body 的解析不一致——
    issue 创建容忍 JSON，但 labels 创建 400 "Required request parameter 'name'
    is not present"（Spring 形态，只认 form/query 参数）。与 api_client
    _gitcode_request 的 form=True 既有口径对齐（JSON 会 400 "body不能为空"）。
    GET 无 body 不受影响。_req 延迟导入：测试 monkeypatch src.domains.mgmt.user.repo_manager._req
    才能拦到（from-import 旧引用会让 patch 失效、真 HTTP 出网——实测教训）。"""
    from src.domains.mgmt.user import repo_manager as _rm
    # form 必需端点（实测 400 实锤）：labels 创建对 JSON body 报
    # "Required request parameter 'name' is not present"（Spring form/query 形态）。
    # 其余写端点上游容忍 JSON（issue create/milestone 实测 200）——维持 _req 通道
    # （测试 monkeypatch src.domains.mgmt.user.repo_manager._req 的拦截契约保持有效）。
    if method == "GET" or body is None:
        return _rm._req(method, url, token, None)
    if method == "POST" and url.rstrip("/").endswith("/labels"):
        headers = {"Content-Type": "application/x-www-form-urlencoded",
                   "Accept": "application/json"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        # 出网口走 repo_manager._requests（模块属性）：测试 monkeypatch
        # repo_manager._req/_requests 可统一拦截，不出网。
        resp = _rm._requests.request(method, url, data=body, headers=headers, timeout=30)
        if resp.status_code >= 400:
            raise _rm.UpstreamAPIError(method, url, resp.status_code, resp.text)
        return resp.json() if resp.text.strip() else {}
    return _rm._req(method, url, token, body)


def bot_token(repo_key):
    from src.domains.mgmt.user.db import get_gitcode_token
    return get_gitcode_token(repo_key=repo_key) or ""

def user_token(username, user_id):
    from src.api.deps import get_user_token_with_refresh
    return get_user_token_with_refresh(username, user_id=user_id)

def _gc_inner_nums(owner_repo, nums):
    return ",".join(f"{owner_repo}#{n}" for n in nums)

def _gc_related(v, neutral, native, ctx):
    native["inner_issue_nums"] = _gc_inner_nums(ctx.get("owner_repo", ""), v)

_QUERY_STATE = {"open": "opened", "closed": "closed", "all": "all"}

_UPDATE_STATE = {
    "open": "reopen", "opened": "reopen", "reopen": "reopen",
    "closed": "close", "close": "close",
}

_RULES = {
    ("issues", "query"): {"state": {"native": "state", "values": _QUERY_STATE}},
    ("issues", "create"): {
        "title": "title",
        "body": "body",
        "assignee": "assignee",
    },
    ("issues", "update"): {
        "labels": "labels",
        "assignee": "assignee",
        # Issue #640: 上游 PATCH /repos/{owner}/issues/{number} 里程碑参数名为 milestone（int），
        # 不存在 milestone_id —— 同名直传会被静默忽略（200 假成功）
        "milestone_id": "milestone",
        "state": {"native": "state", "values": _UPDATE_STATE},
        "body": "body",
    },
    ("pulls", "create"): {
        "title": "title",
        "head": "head",
        "base": "base",
        "body": "body",
        "related_issue_nums": {"native": "inner_issue_nums", "transform": _gc_related},
        "auto_close_issues": "close_related_issue",
        # 中性名与 GitCode 原生名一致（apply_rules 无规则时会原样透传），此处显式
        # 声明仅为语义可读：后来人不必翻文档确认「这个字段到底支不支持」。
        "prune_source_branch": "prune_source_branch",
    },
}

def translate(resource: str, op: str, neutral: dict, ctx: dict) -> dict:
    return base.apply_rules(_RULES.get((resource, op), {}), neutral, ctx)

def query_state(state: str) -> str:
    return _QUERY_STATE.get(state, state)

# 上游契约适配开关（代理层 git_repo_api 读取；实测 2026-11-11 部署验证）：
# - labels 创建色值必须带 # 前缀（GitHub 相反要求不带——Issue #580 的
#   "GitCode 两种格式均宽容"已失效，上游现校验 "Color must be a valid color code"）
# - milestones due_on 只认日期形态 YYYY-MM-DD（ISO 时间戳会 400 "correct to YYYY-MM…"）
LABEL_COLOR_PREFIX_HASH = True

MILESTONE_DUE_DATE_ONLY = True

# - milestones create 上游强制 due_on（2026-11-11 实弹复现：POST /milestones 只带
#   title → 400 PARAMETER_ERROR "due_on must not be empty"；补 due_on 后 200）。
#   代理层据此入口拦截并给出可读 400（不静默补缺省日期——假 due_on 会进入
#   排序/逾期口径，形成不可审计的数据污染）。GitHub 方言无此约束。
MILESTONE_DUE_ON_REQUIRED = True

# D31/S63 凭据域口径（settings.py 读，LABEL_COLOR 同款模块契约开关）：本平台
# settings 表单 PAT 落 <p>_token 专用字段（与专用分支/前端存量口径一致）；
# 未声明该开关的平台走 <p>_pat 泛型凭据键。
SETTINGS_TOKEN_FIELD_SUFFIX = "token"

# - issues update 官方契约是 PATCH /repos/{owner}/issues/{number}（路径无 repo 段，
#   body 必带 repo）——v5 SDK 文档明载。repo 路径形态上游严格拒绝（404
#   "project or enterprise"，对 IK* ident 尤甚）。gitcode 上游恰好宽容 repo 路径
#   侥幸工作，统一收敛到官方契约。
ISSUE_UPDATE_OWNER_PATH = True

# - issues update body.repo 同样只认纯仓库名（W1 复验实锤 2026-11-11：owner/repo
#   全称 404 NOT_EXIST、裸名 200；下午 label/milestone 端点对两形态均收，但 issue
#   update 端点收紧——与 gitee 同口径）。
ISSUE_BODY_REPO_BARE = True
