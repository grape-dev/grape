"""Token service 层——桥接 Repo 和 TokenResolver，避免循环依赖。

R20.11: Repo.bot_token 改为调本模块的 get_repo_bot_token()，
内部委托 TokenResolver.get_token()——所有调 Repo.bot_token 的场景
自动继承短路/过期检测。

设计原则（R20.11.5）：
- Repo 是数据模型（domain object），不在属性内部直接 import TokenResolver
  避免 数据模型 → resolver → 策略类 → Repo 循环依赖
- 本模块是 service 层，可以 import TokenResolver + Repo
- 策略类内部通过 provider 接口和 db 查询获取 token，不反向引用 Repo
"""
from src.kernel.token_resolver import TokenResolver, create_provider_from_repo_key


# R20.8.1/R20.8.5/R20.8.6/R20.8.7/R20.8a.1/R20.8b.1/R20.9.5:
# 401 关键词列表——所有 direct 模式 agent 执行后 401 标记的统一判断依据。
# 散落在 5+ 处的复制粘贴已收敛为此常量，下次补关键词只改一处。
AUTH_401_KEYWORDS = [
    "401 unauthorized", "token invalid", "token expired",
    "authentication failed", "invalid username or token",
    "could not read username", "fatal: authentication",
]


def is_auth_401_error(error_detail: str) -> bool:
    """判断 error 信息是否含 401/auth 关键词。供所有 direct 模式 401 标记调用方使用。"""
    if not error_detail:
        return False
    _lower = error_detail.lower()
    return any(kw in _lower for kw in AUTH_401_KEYWORDS)


def get_bot_identity_token(repo_key: str) -> str:
    """回帖/评论专用：只取 bot 身份 token（仓库 bot → 全局 bot），不落用户 token。

    与 get_repo_bot_token 的区别：后者是**执行链**语义（路径 B 仓库会跳过 bot、
    降级到用户 token）；本函数用于 agent/系统回帖，必须让评论挂在机器人账号名下。
    """
    from src.kernel.token_resolver import get_bot_identity_token as _impl
    return _impl(repo_key)


def get_repo_bot_token(repo_key: str) -> str:
    """获取仓库可用执行 token。供 Repo.bot_token 和其他调用方使用。

    R20.11.1: 统一入口，内部委托 TokenResolver.get_token()。
    R20.11.2: 所有调 Repo.bot_token 的场景自动继承短路/过期检测。
    R20.11.3: token 过期短路时返回空串，调用方应处理空 token 场景。
    """
    if not repo_key:
        return ""
    provider = create_provider_from_repo_key(repo_key)
    return TokenResolver(provider).get_token(repo_key)


def resolve_repo_token(repo_key: str):
    """获取 TokenResult（含级别信息）。供需要级别差异的调用方使用。

    R20.11.4: Chat _resolve_gitcode_token / 飞书 VOD handler 等需要
    TokenResult + TokenActionHandler 的调用方调此方法。
    """
    if not repo_key:
        from src.kernel.token_resolver import TokenResult
        return TokenResult(token="", level="", provider=None, repo_key="")
    provider = create_provider_from_repo_key(repo_key)
    return TokenResolver(provider).resolve(repo_key)


def resolve_repo_config_level(repo_key: str):
    """获取配置级别 TokenResult（不检查过期、不触发 refresh、纯读）。

    D101 方案二（评审 v2 第 3 点）：校验/传输双语义拆分——校验方只取
    .level 判路径类型，**禁止消费 .token 字段**（token 传输唯一化=claim
    现取注入）。内部委托 TokenResolver.resolve_config_level（_fetch 纯读，
    UserToken 走 _fetch_stored 跳过 OAuth refresh）。
    """
    if not repo_key:
        from src.kernel.token_resolver import TokenResult
        return TokenResult(token="", level="", provider=None, repo_key="")
    provider = create_provider_from_repo_key(repo_key)
    return TokenResolver(provider).resolve_config_level(repo_key)


def detect_and_mark_repo_token_expired(repo_key: str) -> str:
    """fail_task 401 检测统一入口。供 Chat / 飞书等非 webhook 场景调用。

    R20.8.1: Chat agent 401 时调此方法检测 token 过期。
    D101 方案三（T8 b 案）：原 detect_and_mark_expired 退役——
    "session 级过期"概念随第 1 级删除消亡；本入口语义=三级链通用检测，
    返回 error_code（REPO_TOKEN_EXPIRED / GLOBAL_TOKEN_EXPIRED /
    TOKEN_EXPIRED / NO_TOKEN_AVAILABLE / ""）。
    """
    if not repo_key:
        return ""
    provider = create_provider_from_repo_key(repo_key)
    return TokenResolver(provider).detect_and_mark_expired(repo_key)


# D101 方案三（T8 b 案）：旧入口 detect_and_mark_expired 已退役（符号消亡，
# 防复活守卫见 tests/test_d101s_session_cleanup.py）——消费方一律改调
# detect_and_mark_repo_token_expired（三级链通用检测，语义等同）。
