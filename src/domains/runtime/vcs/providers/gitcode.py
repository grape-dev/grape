"""GitCode provider 中性翻译（git-repo-provider-neutral-interface-design.md §4.2/4.2.2）。"""

from . import base

API_BASE = "https://gitcode.com/api/v5"

def request(method, url, token, body=None):
    """GitCode 原生调用：写接口（POST/PATCH/PUT）body 用 x-www-form-urlencoded。

    实测实锤（2026-11-11 部署验证）：上游写接口对 JSON body 的解析不一致——
    issue 创建容忍 JSON，但 labels 创建 400 "Required request parameter 'name'
    is not present"（Spring 形态，只认 form/query 参数）。与 api_client
    _request 的 form=True 既有口径对齐（JSON 会 400 "body不能为空"）。
    GET 无 body 不受影响。出网口走 mgmt 域 public 契约（gitcode_req / gitcode_req_form），
    public 转发体在调用时按属性取 `repo_manager._req` / `._requests`，故测试
    monkeypatch `src.domains.mgmt.user.repo_manager._req` 仍能拦到（from-import 旧引用
    会让 patch 失效、真 HTTP 出网——实测教训）。"""
    from src.domains.mgmt.public import gitcode_req, gitcode_req_form
    # form 必需端点（实测 400 实锤）：labels 创建对 JSON body 报
    # "Required request parameter 'name' is not present"（Spring form/query 形态）。
    # 其余写端点上游容忍 JSON（issue create/milestone 实测 200）——维持 JSON 通道。
    if method == "GET" or body is None:
        return gitcode_req(method, url, token, None)
    if method == "POST" and url.rstrip("/").endswith("/labels"):
        return gitcode_req_form(method, url, token, body)
    return gitcode_req(method, url, token, body)


def bot_token(repo_key):
    from src.domains.mgmt.public import get_gitcode_token
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

# Issue #640/#1005/#1076：issue PATCH 含 milestone_id 时写后 GET 回读校验
# （上游有静默忽略先例，PATCH 200 不可信）。仅 gitcode/github 有此上游行为
# 实证——B1（2026-11-13 P1-full 实弹）：github milestone_id 未映射上游
# `milestone` 时同样 200 假成功，映射已修，回读守卫兜底。声明为模块契约
# 开关（零平台字面枚举，test_no_new_binary_platform_enumerations 口径）。
MILESTONE_VERIFY_READBACK = True

# Issue #1005/#1076：issue PATCH milestone_id 取值口径 id 优先、number 兼容——
# 先经 GET /milestones 解析到具体里程碑，再以解析出的上游 number 写 PATCH
# （gitcode 上游 id/number 混用会静默落空）；失败原子性=写后回读不一致时回滚
# 到请求前里程碑（before 快照 + resolved number 比对，代理层 #640 守卫配套）。
# 未声明该开关的平台（github 等）按上游原生 id 语义直传翻译规则。
MILESTONE_ID_RESOLVE_TO_NUMBER = True

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

# - PR 评论 id 为 32 位 hex（create 响应同源主键）：update/delete 上游只认数字
#   note_id（hex 直传 400 Orchestration error）。代理层写前先 GET
#   /pulls/comments/{hex} 解析成数字 note_id（#623 解析器复用）。
PR_COMMENT_ID_HEX = True

# 托管全流程（bot 合作者 + webhook 建/删/探 + 库回写）已实装：
HOSTING_FULL_FLOW = True

# git 传输取仓 token 走存量四级 fallback（agent session → 仓库 bot → 全局 bot → 用户），
# 其余平台走端口 read_stored_token（bot-only）。实现绑定，非平台名判定。
LEGACY_REPO_TOKEN_GETTER = True

# PR/MR 网页路径段（前端 PR 详情链接与 QA 门禁链接共用；能力位驱动，业务侧零平台判断）。
PR_WEB_PATH = "merge_requests"


def repo_segment(owner: str, repo: str) -> str:
    """资源根片段（github 方言）：repos/{owner}/{repo}（URL 段编码）。"""
    import urllib.parse
    return (f"repos/{urllib.parse.quote(owner, safe='')}/"
            f"{urllib.parse.quote(repo, safe='')}")


# ── 通道能力位（Issue #3632 后继：checks/status 通道不再按平台名分派）──
# GitCode 无原生 check-runs，由 Actions v8 适配器映射成 check-run 语义。
CHECK_RUNS_VIA_ACTIONS = True
# 原生 combined status（Gitea 血统 v5 路径），ref 需先解析成 commit sha。
STATUS_NATIVE = True
STATUS_RESOLVE_SHA = True
# PR↔Issue 关联有原生存储（issues 字段 / PATCH inner_issue_nums），支持解除。
PR_ISSUE_LINK_NATIVE = True
