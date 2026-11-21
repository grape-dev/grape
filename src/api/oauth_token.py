"""OAuth token 统一包装器（Issue #343）。

provider 无关——GitCode 和 GitHub 读 API 都走此包装器。
职责：预刷新 + 401/403 错误处理 + 降级提示。

设计原则：
- 写操作走仓库 bot token（get_gitcode_token / get_github_token），不走此包装器
- 读操作走用户 OAuth token，必须走此包装器
- 403 处理关键流程：
  收到 403 → 含 "token"/"forbidden" 关键词？ →
    是：试 refresh → refresh 成功重试 → 重试后仍 403(token) 则 refresh_token 也失效提示重新授权；
                          重试后 403(无 token 关键词) 则业务权限不足返回"无权限"
    否：直接返回"无权限"不触发刷新
"""
from src.kernel.config import log

# Issue #3563（Spec v2，Issue 评论 202381001/202380151）：401 重授权统一出口。
# 协议口径：复用既有 action=reauth_required（前端 api.js 已消费），不新增
# error_code；detail 附带 provider 字段供前端分流到 /auth/login/<provider>。
REAUTH_REQUIRED_ACTION = "reauth_required"

# 平台显示名（「GitHub/GitCode」驼峰形态，capitalize() 会得到 Github/Gitcode）
_PROVIDER_LABELS = {"github": "GitHub", "gitcode": "GitCode",
                    "gitee": "Gitee", "gitlab": "GitLab"}


def _provider_label(provider: str) -> str:
    _p = (provider or "").strip().lower()
    return _PROVIDER_LABELS.get(_p, _p.capitalize())


def reauth_required_detail(provider: str) -> dict:
    """Issue #3563: 统一 401 reauth detail。

    字段口径（Spec v2 B1）：
    - action=reauth_required：既有协议字段（api.js:286/312 按此拦截）；
    - provider：前端按其分流到 /auth/login/<provider>，避免 GitHub 用户
      被误导向 GitCode 授权页（Issue #3563 评论 202380151）；
    - message：provider 对应的「授权已过期，请重新登录授权」；
    - 不携带 error_code（用户裁定：避免两套口径并存）。
    """
    _provider = (provider or "").strip()
    return {
        "code": 401,
        "message": f"{_provider_label(_provider)} 授权已过期，请重新登录授权",
        "action": REAUTH_REQUIRED_ACTION,
        "provider": _provider,
    }


def raise_reauth_required(provider: str):
    """Issue #3563: 三入口统一的 401 错误出口（check-name / 模板一键创建 / 托管权限判断）。"""
    from fastapi import HTTPException
    raise HTTPException(status_code=401, detail=reauth_required_detail(provider))


def sanitize_upstream_error(err) -> str:
    """Issue #3563: 上游异常短消息化——剥离内部 API URL（https?://...）。

    上游报文格式形如 "GitHub API GET https://api.github.com/user failed (401): ..."，
    原文直接拼入用户可见 message 会泄露内部端点；剥离后保留状态码与短描述，
    日志仍可看到完整原文（调用方自行 log）。
    """
    import re
    msg = str(err or "")
    msg = re.sub(r'\s*https?://\S+', '', msg)
    msg = re.sub(r'\s{2,}', ' ', msg).strip()
    return msg[:300]


def _is_token_error(err: Exception) -> bool:
    """判断异常是否为 token 失效（401/403）。

    Issue #343: 用正则提取 HTTP code，不做模糊字符串匹配（旧逻辑匹配 "401" 会误命中 "1401" 等）。
    GitCode/GitHub API 调用抛 RuntimeError，消息格式为 "GitCode API ... failed (401): ..."。
    """
    import re
    msg = str(err)
    # 提取括号内的 3 位 HTTP status code
    codes = re.findall(r'\((\d{3})\)', msg)
    for code_str in codes:
        code = int(code_str)
        if code == 401:
            return True
        if code == 403 and ("token" in msg.lower() or "forbidden" in msg.lower()):
            return True
    # 兜底：消息里明确含 "Bad credentials"（GitHub 401 文案）
    if "Bad credentials" in msg:
        return True
    return False


def _is_business_forbidden(err: Exception) -> bool:
    """判断异常是否为业务权限不足的 403（token 有效但无操作权限）。"""
    import re
    msg = str(err)
    codes = re.findall(r'\((\d{3})\)', msg)
    has_403 = any(int(c) == 403 for c in codes)
    return has_403 and not _is_token_error(err)


def with_oauth_token(user: dict, provider: str, api_call_fn, *args, **kwargs):
    """包装 OAuth 读 API 调用（provider 无关）。

    流程：
    1. 预刷新：token 临近过期时主动刷新（提前 10 分钟）
    2. 执行请求
    3. 401/403(token) → 刷新 token 重试一次
    4. 重试后仍 401/403(token) → refresh_token 也失效，返回"请重新授权"
    5. 重试后 403(无 token 关键词) → 业务权限不足，返回"无权限"
    6. refresh 失败 → 返回"请重新授权"

    user: {id, username}
    provider: "gitcode" 或 "github"
    api_call_fn: 第一个位置参数是 token
    *args, **kwargs: 传给 api_call_fn 的其余参数

    返回 api_call_fn 的结果，或抛 HTTPException。
    """
    from fastapi import HTTPException
    from src.domains.mgmt.user.db import refresh_oauth_token

    user_id = user["id"]
    username = user["username"]

    # 获取 token（带预刷新，D3/K2 消除：provider 无关统一入口）
    from src.api.deps import get_user_token_for
    token = get_user_token_for(username, user_id=user_id, provider=provider)

    if not token:
        raise_reauth_required(provider)

    try:
        return api_call_fn(token, *args, **kwargs)
    except Exception as e:
        if not _is_token_error(e):
            raise  # 非 token 错误不处理

        # token 失效 → 尝试刷新
        log.info(f"with_oauth_token: token error for user={user_id}, provider={provider}, attempting refresh: {str(e)[:100]}")
        new_token = refresh_oauth_token(user_id, provider)
        if not new_token:
            raise_reauth_required(provider)

        # 刷新成功 → 重试一次
        try:
            return api_call_fn(new_token, *args, **kwargs)
        except Exception as e2:
            if _is_token_error(e2):
                # 重试后仍 401/403(token) → refresh_token 也失效
                raise_reauth_required(provider)
            if _is_business_forbidden(e2):
                # 重试后 403 但不含 token 关键词 → 业务权限不足
                raise HTTPException(status_code=403, detail={
                    "code": 403,
                    "message": "无权限执行此操作"
                })
            raise  # 其他错误正常抛出
