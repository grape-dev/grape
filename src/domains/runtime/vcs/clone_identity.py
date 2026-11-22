"""按令牌动态解析平台账号名 —— basic_password 平台 clone 用户名的真实来源。

为什么需要这一层
----------------
`clone_auth=basic_password`（gitee 等）把「用户名 + 令牌」一起作为 HTTP Basic 发出，
用户名必须是**令牌所属账号的 login**：写 `x-access-token` / `bot` / 组织名 / grape 账号名
都会被上游判 `The token username invalid` / 403。实测（2026-11-21，gitee 私有仓）：
同一枚有效令牌，卡上填 `<account>` → 403，填令牌真实 login → `git ls-remote` rc=0。

平台卡上那个 `bot_username` 字段因此是个「必须与令牌一致」的约束，靠人保证必然出错
（令牌换了、账号换了、复制粘贴错一位）。这里把约束变成派生：**拿着令牌问平台“我是谁”**，
结果按令牌指纹本地缓存，同一枚令牌不会重复出网。

设计边界
--------
- **不进 clone_endpoint**：那一层是纯函数（无 IO/无缓存/无网络），被 6 个入口在热路径调用；
  把网络放进去会把「能不能 clone」绑在平台 API 的可用性上，且沙箱/远端 worker 未必有出网权限。
- **缓存键 = 令牌指纹**（sha256 前 32 位），不存明文令牌；令牌轮换 → 键变 → 自动重解析。
- **失败显式**：拿不到 login 就抛 `CloneIdentityError`，绝不回退 `x-access-token` 之类的占位
  用户名（那正是「403 却看不出原因」的旧形态）。调用方决定是回落卡值还是直接报错。
- 同一枚令牌并发首次解析用 per-key 锁收敛（single-flight），避免 N 个入口同时打 /user。
"""
from __future__ import annotations

import hashlib
import os
import threading
import time

from src.kernel.config import log

#: 缓存有效期（秒）。令牌轮换/换人后即使未过期，也会因指纹变化自动重解析。
_TTL_DEFAULT = 3600

_cache: dict = {}          # key -> (login, expires_at)
_locks: dict = {}          # key -> Lock（single-flight）
_guard = threading.Lock()
_calls = 0                 # 实际出网次数（诊断/测试用）


class CloneIdentityError(RuntimeError):
    """令牌身份不可解析（无令牌 / 平台拒绝 / 返回无 login）。

    `reason` 让调用方分档：`empty_token`（配置缺失）/ `token_rejected`（401/403）/
    `no_login`（上游回了但没有 login 字段）/ `lookup_failed`（网络或未知异常）。
    """

    def __init__(self, provider: str, reason: str, detail: str = ""):
        self.provider = provider
        self.reason = reason
        super().__init__(f"{provider}: 无法从令牌解析账号名（{reason}）"
                         + (f": {detail}" if detail else ""))


def _ttl() -> int:
    try:
        return max(0, int(os.environ.get("GRAPE_CLONE_LOGIN_TTL_S", _TTL_DEFAULT)))
    except Exception:  # noqa: BLE001 - 配置写错不该影响 clone
        return _TTL_DEFAULT


def token_fingerprint(token: str) -> str:
    """缓存键：令牌指纹（不落明文）。"""
    return hashlib.sha256((token or "").encode("utf-8")).hexdigest()[:32]


def _key(provider: str, token: str) -> str:
    return f"{(provider or '').strip().lower()}:{token_fingerprint(token)}"


def _lock_for(key: str) -> threading.Lock:
    with _guard:
        lock = _locks.get(key)
        if lock is None:
            lock = _locks[key] = threading.Lock()
        return lock


def _fetch_login(provider: str, token: str) -> str:
    """真出网一次：卡驱动的 `create_provider(provider).verify_token(token)` → login。"""
    global _calls
    from src.kernel.token_resolver import create_provider

    _calls += 1
    try:
        info = create_provider(provider).verify_token(token)
    except Exception as exc:  # noqa: BLE001 - 上游异常一律归为 token_rejected/lookup_failed
        text = str(exc or "")
        reason = ("token_rejected"
                  if any(s in text for s in ("401", "403", "Unauthorized", "Forbidden", "invalid"))
                  else "lookup_failed")
        raise CloneIdentityError(provider, reason, text[:200]) from exc
    if not isinstance(info, dict):
        raise CloneIdentityError(provider, "no_login", f"verify_token 返回 {type(info).__name__}")
    # 字段名按平台方言不同：github/gitcode/gitee 是 login，gitlab 的 /user 是 username，
    # 个别平台用 name。只认 login 会让 gitlab 方言 + basic_password 的卡恒 no_login。
    login = str(info.get("login") or info.get("username") or info.get("name") or "").strip()
    if not login:
        raise CloneIdentityError(provider, "no_login", "上游 /user 未返回 login/username")
    return login


def resolve_login(provider: str, token: str, *, use_cache: bool = True) -> str:
    """令牌 → 账号 login。命中缓存零出网；未命中时同 key 并发只解析一次。"""
    token = (token or "").strip()
    if not token:
        raise CloneIdentityError(provider, "empty_token")
    key = _key(provider, token)
    ttl = _ttl()
    if use_cache and ttl > 0:
        hit = _cache.get(key)
        if hit and hit[1] > time.time():
            return hit[0]
    with _lock_for(key):
        # 双重检查：等锁期间别的线程可能已经解析完
        if use_cache and ttl > 0:
            hit = _cache.get(key)
            if hit and hit[1] > time.time():
                return hit[0]
        login = _fetch_login(provider, token)
        if ttl > 0:
            _cache[key] = (login, time.time() + ttl)
        log.info(f"clone_identity: {provider} token→login 解析成功（login={login}）")
        return login


def invalidate(provider: str, token: str = "") -> None:
    """清缓存：给了令牌只清该枚；不给则清该平台全部。"""
    if token:
        _cache.pop(_key(provider, token), None)
        return
    prefix = f"{(provider or '').strip().lower()}:"
    for k in [k for k in list(_cache) if k.startswith(prefix)]:
        _cache.pop(k, None)


def cache_stats() -> dict:
    """诊断：缓存在册条目数 + 累计出网次数（测试与运维排障用）。"""
    now = time.time()
    alive = sum(1 for _login, exp in list(_cache.values()) if exp > now)
    return {"entries": len(_cache), "alive": alive, "calls": _calls, "ttl": _ttl()}


def username_for_token(provider: str, token: str, repo_key: str = "") -> "tuple[str, str]":
    """clone 用户名 + 来源档：`token_owner` / `card_bot_username` / `owner_fallback` / `scheme_const` / `missing`。

    只在 `clone_auth=basic_password` 时出网（其余形态是常量用户名，问了也没有意义）。
    解析失败**不抛**：回落 `clone_endpoint.clone_username_source`（卡值 → 仓库 owner），
    并把失败写进日志 —— 调用方拿到的 source 能区分「令牌解析来的」和「人填的/猜的」。
    """
    from src.domains.runtime.vcs import clone_endpoint as ce

    scheme = ce.clone_auth(provider)
    if scheme != "basic_password":
        return ce.clone_username_source(provider, repo_key)
    if token:
        try:
            return resolve_login(provider, token), "token_owner"
        except CloneIdentityError as exc:
            log.warning(f"clone_identity: {exc}；回落到卡/owner 派生（repo_key={repo_key or '-'}）")
    return ce.clone_username_source(provider, repo_key)