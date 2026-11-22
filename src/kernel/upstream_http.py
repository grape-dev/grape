"""上游 HTTP 唯一入口：平台卡私有 CA + 带 token 请求不跟随重定向。

Issue #3580 复审（对齐 PR #1854 的两个实现点）：

1. **私有 CA**：自建 GitLab 常用企业自签 CA，平台卡上已声明 `ca_bundle`（PEM 绝对路径），
   但此前只注入到 git 传输层（`repogit` 的 `GIT_CONFIG http.sslCAInfo`）；API 侧
   （列仓/托管/建钩/Issue/MR）仍用默认 SSL 上下文 → 自建实例全链路 TLS 失败。
   本模块按 `api_base` 前缀匹配平台卡，把 `ca_bundle` 用于 urllib 的 SSL 上下文。
2. **不跟随重定向**：GitLab 方言族的上游请求带 `PRIVATE-TOKEN`，urllib 在 3xx 时会
   把同一份 header 带到重定向目标（可能异域）；故该方言族一律不跟随，3xx 以
   `HTTPError` 上抛由调用方按上游错误处理（PR 的 `allow_redirects=False` 同口径）。
   其它平台保持存量语义（跟随），零回归。

用法（调用点保持自己的错误包装与 span）：

    from src.kernel.upstream_http import open_with_policy
    with open_with_policy(req, timeout=30) as resp:
        ...
"""
from __future__ import annotations

import os
import ssl
import urllib.error
import urllib.request

from src.kernel.config import log

# 平台卡策略缓存：api_base → (解析时刻, ca_bundle, allow_redirects)。
# 必须有 TTL：管理面改卡（ca_bundle / 方言）后若靠进程重启才生效，等于运维
# 无法在线修 TLS/重定向策略（架构复审 P2-4）。60s 与卡本身的读取节奏同量级。
_POLICY_TTL_SECONDS = int(os.environ.get("UPSTREAM_POLICY_TTL_SECONDS", "60"))
_POLICY_CACHE: "dict[str, tuple[float, str, bool]]" = {}


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """3xx 直接抛 HTTPError（redirect_request 返回 None 即不跟随）。"""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D102
        return None


def _api_base_key(url: str) -> str:
    """url → 候选 api_base 前缀键（按卡 api_base 匹配时的比对用：scheme://host[:port]）。"""
    import urllib.parse as _up
    try:
        parts = _up.urlsplit(url)
    except Exception:
        return ""
    if not parts.scheme or not parts.netloc:
        return ""
    return f"{parts.scheme}://{parts.netloc}"


def policy_for(url: str) -> "tuple[str, bool]":
    """url → (ca_bundle, allow_redirects)。

    匹配序：平台卡 `api_base` 前缀命中 → 该卡策略；无命中（未登记平台/裸下载地址）→
    ("", True) = 存量语义。
    """
    key = _api_base_key(url)
    if not key:
        return "", True
    import time as _time

    cached = _POLICY_CACHE.get(key)
    if cached is not None and (_time.time() - cached[0]) < _POLICY_TTL_SECONDS:
        return cached[1], cached[2]
    ca_bundle, allow_redirects = "", True
    try:
        # 分层约束（check-arch R1 HARD）：kernel 只能经能力端口取平台卡——
        # 装配层把 vcs_cards 端口绑定到 domains 实现，kernel 侧不出现 domains import。
        from src.kernel.ports import vcs_cards as _vcs_cards

        for provider in _vcs_cards.list_vcs_platforms():
            card = _vcs_cards.get_platform_card(provider)
            api_base = str(getattr(card, "api_base", "") or "").rstrip("/")
            if not api_base or not (url == api_base or url.startswith(api_base + "/")):
                continue
            ca_bundle = str(getattr(card, "ca_bundle", "") or "")
            # GitLab 方言族：带 token 的上游请求不跟随重定向（见模块 docstring）
            allow_redirects = str(getattr(card, "dialect", "") or "") != "gitlab"
            break
    except Exception as e:  # noqa: BLE001
        log.debug(f"upstream_http: card policy lookup skipped: {e}")
        return "", True
    _POLICY_CACHE[key] = (_time.time(), ca_bundle, allow_redirects)
    return ca_bundle, allow_redirects


def reset_cache() -> None:
    """测试用：改卡后强制下次重新解析（卡本身也有 TTL，这里只是避免跨用例串味）。"""
    _POLICY_CACHE.clear()


def ssl_context(ca_bundle: str) -> "ssl.SSLContext | None":
    """ca_bundle（PEM 绝对路径）→ SSL 上下文；空/不可读 → None（系统 CA，存量语义）。"""
    if not ca_bundle:
        return None
    try:
        return ssl.create_default_context(cafile=ca_bundle)
    except Exception as e:  # noqa: BLE001
        log.warning(f"upstream_http: ca_bundle 加载失败，回落系统 CA: {ca_bundle}: {e}")
        return None


def open_with_policy(req, *, timeout: float = 30.0, ca_bundle: str = "",
                     allow_redirects: "bool | None" = None):
    """按策略执行请求（返回 urllib 响应；非 2xx 抛 HTTPError，调用方自行包装）。

    - `ca_bundle` 显式给定（调用方已拿到卡）优先；空则按 url 查卡策略；
    - `allow_redirects` 显式给定优先；None 则按 url 查卡策略。
    """
    ca, policy_redirects = policy_for(req.full_url)
    ca = ca_bundle or ca
    follow = policy_redirects if allow_redirects is None else allow_redirects
    handlers: list = []
    ctx = ssl_context(ca)
    if ctx is not None:
        handlers.append(urllib.request.HTTPSHandler(context=ctx))
    if not follow:
        handlers.append(_NoRedirect())
    opener = urllib.request.build_opener(*handlers) if handlers else urllib.request.build_opener()
    return opener.open(req, timeout=timeout)