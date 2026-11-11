"""D90 消费端批步 5（P-6 决议）：401 被动兜底——强刷一次 + 单次重试。

契约五约束（docs/weekly-09/oauth-fill-contract-review.md 第 5 节 P-6）：
1. refresh 后仅重试一次，失败即抛（防重试风暴）
2. 排除 refresh 接口自身的 401（防递归）
3. 按凭据类型分流——OAuth 才 refresh，PAT 直接抛显式错误（PAT 无 refresh 语义）
4. per-request refresh-attempted 守卫 + 并发 401 折叠为单次 refresh
   （single-flight，tmail/@devraj-labs 先例）
5. refresh 返回 invalid_grant/400 走凭据失效流程；403 永不触发 refresh

触发面（按领域分）：用户 OAuth token 链的 401——吊销/时钟偏差/预刷新调度
失败等主动预刷新（600s 缓冲）覆盖不到的场景。bot token（managed_repos 列）
无 user 上下文，不在本模块范围（既有「Bot Token 失效」文案路径不变）。

使用方式（调用方——有 user 上下文的 API 调用包装）：
    def call_with_401_retry(user_id, provider, api_call):
        return oauth_401_retry(user_id, provider, api_call)

失败二分（决议）：invalid_grant/400 → 凭据失效流程（status=4 过期标记 +
重新登录引导）；暂时性（5xx/网络）→ 可重试错误。
"""
from __future__ import annotations

import threading

from src.kernel.config import log

# single-flight：并发 401 折叠为单次 refresh（per (user_id, provider) 锁）
_refresh_locks: "dict[tuple, threading.Lock]" = {}
_locks_guard = threading.Lock()


def _lock_for(user_id: str, provider: str) -> threading.Lock:
    key = (user_id, provider)
    with _locks_guard:
        if key not in _refresh_locks:
            _refresh_locks[key] = threading.Lock()
        return _refresh_locks[key]


def is_invalid_grant_error(err_text: str) -> bool:
    """失败二分：invalid_grant/400（凭据彻底失效）vs 暂时性错误。"""
    t = (err_text or "").lower()
    return "invalid_grant" in t or " (400)" in t or "http 400" in t


def oauth_401_retry(user_id: str, provider: str, current_token: str, api_call,
                    result_extractor=None) -> tuple:
    """带 401 被动兜底的 API 调用包装。

    参数：
        user_id/provider: 凭据归属（强刷定位 + 凭据类型分流依据）
        current_token: 当前 token（首调用；401 后强刷换新 token 重试）
        api_call: callable(token) -> result 的调用闭包（用传入 token 发请求）
        result_extractor: 可选，从 result 提取 HTTP 状态码（int|None）；
            None 时 result 本身视为 int 状态码

    返回 (result, retried: bool)——调用方感知是否发生过兜底（监控分轨）。

    语义（契约五约束一一对应）：
    - 首调 401 → 强刷一次 → 新 token 重试一次 → 仍 401 即返回失败（约束 1）
    - refresh 调用自身不经过本包装（约束 2 由调用形态保证：_try_refresh 内部
      不走 api_call）
    - PAT 链（无 refresh_token）：跳过强刷直接返回 401 结果（约束 3）
    - single-flight：同 (user_id, provider) 并发 401 只刷一次，其余等锁后
      用新 token 重试（约束 4）
    - invalid_grant → 走失效流程标记；403 原样返回（约束 5）
    """
    first = api_call(current_token)
    status = (result_extractor(first) if result_extractor else first)
    if status != 401:
        return first, False

    # 401 → 判定凭据类型（有 refresh_token 才是 OAuth 链；PAT 直接抛/返回）
    from src.domains.mgmt.auth.grape_account_db import _has_refresh_token
    if not _has_refresh_token(user_id, provider):
        log.warning(f"401 on PAT-type credential (no refresh, P-6③): "
                    f"user={user_id} provider={provider}")
        return first, False

    # single-flight 强刷（约束 4）
    lock = _lock_for(user_id, provider)
    with lock:
        # 双检：等锁期间可能已被并发请求刷新过——重试前先拿新 token 直接试
        from src.api.deps import _try_refresh_user_token
        new_token = _try_refresh_user_token(user_id, provider)
        if not new_token:
            # 刷新失败：失败二分（约束 5）
            err_text = str(first)
            if is_invalid_grant_error(err_text):
                log.warning(f"401 + invalid_grant → credential invalidation flow: "
                            f"user={user_id} provider={provider}")
                _mark_credential_expired(user_id, provider)
            return first, False

    # 单次重试（新 token；仍 401 即失败——约束 1）
    second = api_call(new_token)
    status2 = (result_extractor(second) if result_extractor else second)
    if status2 == 401:
        log.warning(f"401 persists after forced refresh (single retry exhausted, P-6①): "
                    f"user={user_id} provider={provider}")
    return second, True


def _mark_credential_expired(user_id: str, provider: str) -> None:
    """凭据失效流程：external_identities.status → 4（过期标记，R10 语义）。"""
    try:
        from src.kernel.session_manager import db, db_lock
        with db_lock:
            db.execute(
                "UPDATE external_identities SET status=4 "
                "WHERE grape_user_id=? AND provider=? AND status=1",
                (user_id, provider))
            db.commit()
        log.info(f"credential marked expired (status=4): user={user_id} provider={provider}")
    except Exception as e:
        log.warning(f"mark credential expired failed: {e}")
