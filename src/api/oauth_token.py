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
from src.config import log


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
    from src.user.db import refresh_oauth_token

    user_id = user["id"]
    username = user["username"]

    # 获取 token（带预刷新）
    if provider == "gitcode":
        from src.api.deps import get_user_token_with_refresh
        token = get_user_token_with_refresh(username, user_id=user_id)
    else:
        # Issue #343 修复: GitHub 也走预刷新逻辑（和 GitCode 完全对称）
        from src.api.deps import get_user_github_token_with_refresh
        token = get_user_github_token_with_refresh(username, user_id=user_id)

    _provider_label = provider.capitalize()

    if not token:
        raise HTTPException(status_code=401, detail={
            "code": 401,
            "message": f"{_provider_label} 授权已过期，请重新登录授权",
            "action": "reauth_required"
        })

    try:
        return api_call_fn(token, *args, **kwargs)
    except Exception as e:
        if not _is_token_error(e):
            raise  # 非 token 错误不处理

        # token 失效 → 尝试刷新
        log.info(f"with_oauth_token: token error for user={user_id}, provider={provider}, attempting refresh: {str(e)[:100]}")
        new_token = refresh_oauth_token(user_id, provider)
        if not new_token:
            raise HTTPException(status_code=401, detail={
                "code": 401,
                "message": f"{_provider_label} 授权已过期，请重新登录授权",
                "action": "reauth_required"
            })

        # 刷新成功 → 重试一次
        try:
            return api_call_fn(new_token, *args, **kwargs)
        except Exception as e2:
            if _is_token_error(e2):
                # 重试后仍 401/403(token) → refresh_token 也失效
                raise HTTPException(status_code=401, detail={
                    "code": 401,
                    "message": f"{_provider_label} 授权已过期，请重新登录授权",
                    "action": "reauth_required"
                })
            if _is_business_forbidden(e2):
                # 重试后 403 但不含 token 关键词 → 业务权限不足
                raise HTTPException(status_code=403, detail={
                    "code": 403,
                    "message": "无权限执行此操作"
                })
            raise  # 其他错误正常抛出
