"""仓库 clone 端点三要素（地址 / 鉴权形态 / 用户名）的单点派生。

为什么需要这一层
----------------
clone 入口当前有四处，改前各自拼地址、各自定用户名：

====================  ==========================================  =========================
入口                  位置                                         改前形态
====================  ==========================================  =========================
普通 worker 引擎      engine/repogit.py::ensure_repo_base          卡派生 host（已正确）
云沙箱（executor）    agents/executor.py::_run_cloud_sandbox       硬编码 https://gitcode.com
云沙箱（上下文）      agents/sandbox/sandbox_context.py::build_ctx 硬编码 https://gitcode.com
接管会话下发          engine/takeover.py::takeover bundle          硬编码 https://gitcode.com
CLI 一次券            mgmt/routes/repos.py::git-credential         用户名恒 "x-access-token"
沙箱 askpass          sandbox/backends/devbox.py::_clone_repo      用户名恒 "bot"
====================  ==========================================  =========================

后果：gitee / github / gitlab 仓库在沙箱与接管链路上被当成 gitcode 仓库去 clone
（请求打到 gitcode.com 的同名仓），失败原因完全不可诊断；而 gitee 的平台卡声明
`clone_auth=basic_password`（用户名必须是真实账号、私人令牌当密码），两套常量
用户名都不满足它。

这里只做一件事：按平台卡派生 (clone_url, clone_auth, clone_username)，让调用方
不再持有 host 常量与用户名常量。新增平台 = 加一张卡，不改本模块。

设计边界（刻意不做）
--------------------
- 不做网络/文件 IO，不缓存，不落盘——令牌根本不进这一层（只出地址与用户名）。
- 不判断"worker 能不能直连上游"：那是部署形态，不是平台属性。将来要走主控
  代传（bundle / HTTP 代理），是新增通道，不在这里加分支。
- 无卡即 fail-fast（`CloneEndpointError`），**不静默回退 gitcode.com**：静默回退
  会把 gitee 仓的 clone 伪装成 gitcode 仓，正是本模块要消灭的形态（S7/G1 口径）。
"""
from __future__ import annotations

from src.kernel.config import log

#: 平台卡缺 `clone_auth` 时的缺省（与 build_platform_card 的缺省一致）。
_DEFAULT_CLONE_AUTH = "x_access_token"

#: 鉴权形态 → 缺省用户名（卡未声明 `bot_username` 时使用）。
#: basic_password 不在此表：它要求真实账号名，必须由卡/仓库归属派生（见 clone_username）。
_USERNAME_BY_SCHEME = {
    "x_access_token": "x-access-token",
    "oauth2": "oauth2",
}

#: 用户名派生告警去重（每个 provider 一次，避免每任务刷屏）。
_warned_providers: set = set()


class CloneEndpointError(RuntimeError):
    """clone 端点不可派生（平台未在册 / 卡缺 host）。

    带 `code` 便于上游（agent / 透传路由）把"平台未注册"与"工作区不可写"
    区分开——两者此前共用同一条降级链，排障会被引到修文件权限上。
    """

    def __init__(self, provider: str, detail: str = "",
                 code: str = "platform_not_registered"):
        self.provider = provider
        self.code = code
        super().__init__(f"{provider!r} 无可用 clone 端点：{detail}" if detail
                         else f"{provider!r} 无可用 clone 端点")


def _card(provider: str):
    """调用期解析平台卡（测试可 monkeypatch；无卡返回 None）。"""
    from src.domains.runtime.vcs.platform_card import get_platform_card

    return get_platform_card(provider or "")


def clone_host(provider: str) -> str:
    """平台卡 host（clone / web 域名）。无卡或 host 为空 → fail-fast，不猜域名。

    两类"没有卡"必须给出不同 code（2026-11-21）：
      · `platform_card_contract_unavailable` —— 取卡通道坏了（主控读表失败 /
        401 / 超时 / 网络不通）：修链路；
      · `platform_not_registered` —— 三处来源确实都没有这张卡：去配卡。
    此前两种情况都表现为"平台未注册"，把系统性故障伪装成配置缺失。
    """
    card = _card(provider)
    host = ((getattr(card, "host", "") or "") if card is not None else "").strip()
    if not host:
        _code = "platform_not_registered"
        _detail = ("平台卡未在册或未声明 host"
                   "（表/契约、configs/platforms/*.json、代码内置默认三处都没有该平台）")
        if card is None:
            from src.domains.runtime.vcs.platform_card import (
                platform_card_degraded_reason,
            )
            _deg = platform_card_degraded_reason()
            # contract_empty（表里 0 行 enabled）属配置问题，仍归「未注册」
            if _deg and _deg != "contract_empty":
                _code = "platform_card_contract_unavailable"
                _detail = (f"取卡通道不可用（{_deg}）：请检查 RUNTIME_SERVER_URL / "
                           f"RUNTIME_TOKEN 是否可达有效、主控能否读 mgmt 平台卡表 —— "
                           f"这不是「平台未注册」，不要先去改配置")
        raise CloneEndpointError(provider, _detail, code=_code)
    return host


def clone_auth(provider: str) -> str:
    """平台卡 clone_auth（x_access_token | oauth2 | basic_password）。"""
    card = _card(provider)
    value = ((getattr(card, "clone_auth", "") or "") if card is not None else "").strip()
    return value or _DEFAULT_CLONE_AUTH


def clone_username(provider: str, repo_key: str = "") -> str:
    """clone 用的 HTTP Basic 用户名——按 `clone_auth` 派生，不再由调用方写常量。

    派生顺序：
      1. `clone_auth=basic_password`（gitee）→ 卡 `bot_username`；为空时用仓库 owner
         （同一个人账号时两者相等；不同则必须由部署在卡里填 bot_username）。
      2. 其余形态 → 常量表（x_access_token → x-access-token；oauth2 → oauth2）。

    为什么 basic_password 不能给常量：该形态的语义是"真实用户名 + 私人令牌当密码"，
    `x-access-token` / `bot` 这类占位用户名在 gitee 上不是账号，凭据必然被拒。
    """
    return clone_username_source(provider, repo_key)[0]


def clone_username_source(provider: str, repo_key: str = "") -> "tuple[str, str]":
    """返回 `(username, source)` —— source 让调用方能区分"卡上配置"与"回落猜的"。

    取值：`card_bot_username` / `owner_fallback` / `scheme_const` / `missing`。

    为什么要把来源暴露出来（2026-11-21）：`basic_password` 平台只接受**令牌所属
    账号名**，而 `owner_fallback` 只在"个人仓且 owner 恰好等于令牌账号"时正确，
    组织仓/他人仓必然被上游拒（`The token username invalid` / 403）。此前这个回退
    只有一条 WARNING，调用方（repogit / 沙箱 / CLI）拿不到任何信号，排障时会去查
    凭据而不会怀疑用户名派生。带上 source 后日志与审计可以直接区分。
    """
    scheme = clone_auth(provider)
    if scheme != "basic_password":
        return _USERNAME_BY_SCHEME.get(scheme, "x-access-token"), "scheme_const"
    card = _card(provider)
    bot_username = ((getattr(card, "bot_username", "") or "") if card is not None else "").strip()
    if bot_username:
        return bot_username, "card_bot_username"
    owner = ""
    if repo_key:
        try:
            from src.kernel.repo_key import parse_repo_key

            owner = str(parse_repo_key(repo_key)[1] or "")
        except Exception:  # noqa: BLE001 - 解析失败不阻断，下面按缺失处理
            owner = ""
    if not owner:
        return "", "missing"
    if provider not in _warned_providers:
        _warned_providers.add(provider)
        log.warning(
            f"clone_endpoint: {provider} 的平台卡未声明 bot_username（clone_auth=basic_password），"
            f"用户名回退为仓库 owner（{owner or '-'}，repo_key={repo_key or '-'}）。"
            f"真机实测：gitee 只接受**令牌所属账号名**（x-access-token / 空用户名均返回 "
            f"'The token username invalid'）；个人仓 owner 与令牌账号同名时可用，"
            f"组织仓 / 他人仓必然被拒。请在平台卡 bot_username 填令牌账号名。")
    return owner, "owner_fallback"


def clone_url(repo_key: str) -> str:
    """按平台卡 host 拼 clone URL（`https://<card.host>/<owner>/<repo>.git`）。"""
    from src.kernel.repo_key import parse_repo_key

    provider, owner, repo = parse_repo_key(repo_key or "")
    if not owner or not repo:
        raise CloneEndpointError(provider or "?", f"repo_key 无法解析: {repo_key!r}",
                                 code="repo_key_invalid")
    return f"https://{clone_host(provider)}/{owner}/{repo}.git"


def clone_params(repo_key: str) -> dict:
    """clone 三要素（供 repogit / 沙箱 / 接管 / CLI 一次券共用）。

    Returns:
        {"provider", "owner", "repo", "clone_url", "clone_auth", "username"}
        令牌不在此结构里：调用方各自按既有安全口径注入（extraheader / 一次性券）。
    """
    from src.kernel.repo_key import parse_repo_key

    provider, owner, repo = parse_repo_key(repo_key or "")
    if not owner or not repo:
        raise CloneEndpointError(provider or "?", f"repo_key 无法解析: {repo_key!r}",
                                 code="repo_key_invalid")
    return {
        "provider": provider,
        "owner": owner,
        "repo": repo,
        "clone_url": f"https://{clone_host(provider)}/{owner}/{repo}.git",
        "clone_auth": clone_auth(provider),
        "username": clone_username(provider, repo_key),
    }

def bot_token_env_name(repo_key: str) -> str:
    """bot token 的环境变量名：平台卡 `token_env` 优先，缺省 `BOT_<PROVIDER>_TOKEN`。

    为什么需要这一层：注入侧（cloud_worker）与读取侧（沙箱 executor / sandbox_context）
    过去都写死 `BOT_GITCODE_TOKEN` —— gitee 仓库的 bot token 在沙箱里恒为空，
    `configure_repo` 因为 `if repo_full_name and bot_token` 被整段跳过，沙箱内没有仓库。
    token 键名和 clone 地址一样，是"平台知识"，必须由卡派生。
    """
    from src.kernel.repo_key import parse_repo_key

    provider = parse_repo_key(repo_key or "")[0] or "gitcode"
    card = _card(provider)
    name = ""
    if card is not None:
        # 卡字段 token_env 优先；PlatformCard.token_env_name 是**方法**（不是属性），
        # 直接 getattr 会拿到 bound method（写死 .strip() 会 AttributeError）——
        # 这里两种形态都吃，且方法异常不让 token 键派生整体失败。
        name = str(getattr(card, "token_env", "") or "").strip()
        if not name:
            _ten = getattr(card, "token_env_name", "")
            if callable(_ten):
                try:
                    _ten = _ten()
                except Exception:  # noqa: BLE001
                    _ten = ""
            name = str(_ten or "").strip()
    return name or f"BOT_{provider.upper()}_TOKEN"


def bot_token_from_env(env: dict, repo_key: str) -> str:
    """从进程环境取该平台的 bot token（派生键优先，兼容存量 `BOT_GITCODE_TOKEN`）。"""
    env = env or {}
    name = bot_token_env_name(repo_key)
    value = str(env.get(name) or "")
    if value:
        return value
    if name != "BOT_GITCODE_TOKEN":
        # 旧 worker / 旧派发载荷只写 gitcode 键：仍取一次，避免升级期断链
        return str(env.get("BOT_GITCODE_TOKEN") or "")
    return ""
