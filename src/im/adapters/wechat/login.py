"""微信扫码绑定（Issue #1728 M1/§5.1）——取码 + 状态短轮询代理 + 凭证产出。

协议（`docs/protocol_zh_CN.md` §3.1）：
  - `POST /ilink/bot/get_bot_qrcode?bot_type=3` body `{"local_token_list": []}`
    → `qrcode`、`qrcode_img_content`；
  - `GET /ilink/bot/get_qrcode_status?qrcode=…` **长轮询 35s**，可能返回
    `need_verifycode`（需手机上看 PIN 再提交）；
  - `confirmed` 产出 `bot_token` / `ilink_bot_id` / `baseurl` / `ilink_user_id`；
  - `expired` / `verify_code_blocked` 需重新取码，刷新计数 > 3 停止；
  - `scaned_but_redirect` 后轮询改到 `redirect_host`。

本模块只负责**登录状态机**（进程内 TTL 10min，单进程足够；重启丢二维码属可接受，
前端重新点一次）。凭证落 `grape_t_channel_installation` 在 API 路由层完成。
"""
from __future__ import annotations

import logging
import time
import uuid
from dataclasses import dataclass, field
from typing import Callable

from .protocol import WechatApiError, WechatClient, as_str

log = logging.getLogger(__name__)

LOGIN_TTL = 600.0
STATUS_CACHE_TTL = 2.0  # qrcode 状态短轮询缓存：ilink 状态机最小推进粒度约 1-2s，
                        # 2s 内复用上一结果可挡住多标签页轮询放大，又不影响扫码实时性。
MAX_REFRESH = 3

STATUS_WAIT = "wait"
STATUS_SCANED = "scaned"
STATUS_NEED_VERIFY = "need_verifycode"
STATUS_CONFIRMED = "confirmed"
STATUS_EXPIRED = "expired"
STATUS_BLOCKED = "verify_code_blocked"


def _redirect_base_url(host: str) -> str:
    """`redirect_host` → 可用基址：无 scheme 时补 https://。"""
    host = as_str(host).strip().rstrip("/")
    if not host:
        return ""
    if "://" not in host:
        host = f"https://{host}"
    return host


def _normalize_status(value: str) -> str:
    raw = as_str(value).strip().lower()
    mapping = {
        "wait": STATUS_WAIT,
        "waiting": STATUS_WAIT,
        "pending": STATUS_WAIT,
        "scaned": STATUS_SCANED,
        "scanned": STATUS_SCANED,
        "scaned_but_redirect": STATUS_SCANED,
        "need_verifycode": STATUS_NEED_VERIFY,
        "need_verify_code": STATUS_NEED_VERIFY,
        "confirmed": STATUS_CONFIRMED,
        "confirm": STATUS_CONFIRMED,
        "success": STATUS_CONFIRMED,
        "expired": STATUS_EXPIRED,
        "verify_code_blocked": STATUS_BLOCKED,
    }
    return mapping.get(raw, raw or STATUS_WAIT)


@dataclass
class WechatLoginSession:
    login_id: str
    qrcode: str = ""
    qrcode_img_content: str = ""
    status: str = STATUS_WAIT
    refresh_count: int = 0
    created_at: float = 0.0
    expires_at: float = 0.0
    redirect_host: str = ""
    credentials: dict = field(default_factory=dict)
    last_status_at: float = 0.0
    last_result: dict = field(default_factory=dict)


class WechatLoginManager:
    """进程内扫码登录状态机（注入 `client_factory` / `clock` 便于测试）。"""

    def __init__(self, *,
                 client_factory: Callable[[], WechatClient] | None = None,
                 ttl: float = LOGIN_TTL,
                 cache_ttl: float = STATUS_CACHE_TTL,
                 max_refresh: int = MAX_REFRESH,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self._client_factory = client_factory or (lambda: WechatClient())
        self._ttl = float(ttl)
        self._cache_ttl = float(cache_ttl)
        self._max_refresh = max(0, int(max_refresh))
        self._clock = clock
        self._sessions: dict[str, WechatLoginSession] = {}
        self._clients: dict[str, WechatClient] = {}

    def _client(self, session: WechatLoginSession) -> WechatClient:
        client = self._clients.get(session.login_id)
        if client is None:
            client = self._client_factory()
            self._clients[session.login_id] = client
        return client

    def _expire(self, session: WechatLoginSession) -> bool:
        return self._clock() > session.expires_at

    def _new_session_id(self) -> str:
        return uuid.uuid4().hex

    async def start(self, *, bot_type: int = 3) -> dict:
        login_id = self._new_session_id()
        session = WechatLoginSession(login_id=login_id, created_at=self._clock(),
                                     expires_at=self._clock() + self._ttl)
        await self._fetch_qrcode(session, bot_type=bot_type)
        self._sessions[login_id] = session
        return {
            "login_id": login_id,
            "qrcode_img_content": session.qrcode_img_content,
            "expires_in": int(max(0, session.expires_at - self._clock())),
        }

    async def _fetch_qrcode(self, session: WechatLoginSession, *,
                            bot_type: int = 3) -> None:
        client = self._client(session)
        body = await client.get_bot_qrcode(bot_type=bot_type)
        session.qrcode = as_str(body.get("qrcode") or body.get("qrcode_id"))
        session.qrcode_img_content = as_str(
            body.get("qrcode_img_content") or body.get("qrcode_url")
            or body.get("img_content"))
        session.status = STATUS_WAIT
        session.last_status_at = 0.0
        session.last_result = {}

    async def status(self, login_id: str, *, verify_code: str = "") -> dict:
        session = self._sessions.get(as_str(login_id))
        if session is None:
            return {"status": STATUS_EXPIRED, "message": "登录会话不存在或已过期"}
        now = self._clock()
        if self._expire(session):
            self._sessions.pop(session.login_id, None)
            self._clients.pop(session.login_id, None)
            return {"status": STATUS_EXPIRED, "message": "二维码已过期，请重新获取"}
        if (session.last_result and not verify_code
                and now - session.last_status_at < self._cache_ttl):
            return dict(session.last_result)

        client = self._client(session)
        try:
            body = await client.get_qrcode_status(session.qrcode,
                                                  verify_code=verify_code)
        except WechatApiError as e:
            if e.session_paused:
                return {"status": session.status, "message": "上游会话暂停，请稍后再试"}
            raise

        upstream = _normalize_status(
            body.get("status") or body.get("state") or body.get("qrcode_status"))
        if as_str(body.get("redirect_host")):
            session.redirect_host = as_str(body.get("redirect_host"))
            # `scaned_but_redirect` 后轮询必须改到 redirect_host，否则仍打旧 host
            client.with_base_url(_redirect_base_url(session.redirect_host))
        if upstream == STATUS_CONFIRMED or as_str(body.get("bot_token")):
            return self._on_confirmed(session, body)
        if upstream in (STATUS_EXPIRED, STATUS_BLOCKED):
            return await self._on_refresh(session, upstream, bot_type=3)
        if upstream == STATUS_NEED_VERIFY:
            session.status = STATUS_NEED_VERIFY
            result = {"status": STATUS_NEED_VERIFY, "need_verifycode": True,
                      "qrcode_img_content": session.qrcode_img_content}
            self._cache(session, result)
            return result

        session.status = upstream
        result = {"status": upstream,
                  "qrcode_img_content": session.qrcode_img_content}
        self._cache(session, result)
        return result

    async def _on_refresh(self, session: WechatLoginSession, upstream: str, *,
                          bot_type: int) -> dict:
        if session.refresh_count >= self._max_refresh:
            session.status = STATUS_EXPIRED
            result = {"status": STATUS_EXPIRED,
                      "message": "二维码刷新次数已达上限，请稍后重新获取"}
            self._cache(session, result)
            return result
        session.refresh_count += 1
        log.info("[wechat] login %s refresh qrcode (%s/%s) reason=%s",
                 session.login_id[:8], session.refresh_count, self._max_refresh, upstream)
        try:
            await self._fetch_qrcode(session, bot_type=bot_type)
        except Exception as e:
            log.warning("[wechat] refresh qrcode failed: %r", e)
            return {"status": upstream, "message": "刷新二维码失败，请重试"}
        result = {
            "status": STATUS_WAIT,
            "qrcode_img_content": session.qrcode_img_content,
            "refreshed": True,
            "refresh_count": session.refresh_count,
            "message": ("验证码错误次数过多，已重新取码" if upstream == STATUS_BLOCKED
                        else "二维码已过期，已自动刷新"),
        }
        self._cache(session, result)
        return result

    def _on_confirmed(self, session: WechatLoginSession, body: dict) -> dict:
        baseurl = as_str(body.get("baseurl")) or as_str(body.get("base_url"))
        credentials = {
            "bot_token": as_str(body.get("bot_token")),
            "ilink_bot_id": as_str(body.get("ilink_bot_id") or body.get("bot_id")),
            "baseurl": baseurl,
            "ilink_user_id": as_str(body.get("ilink_user_id") or body.get("user_id")),
        }
        session.credentials = credentials
        session.status = STATUS_CONFIRMED
        result = {
            "status": STATUS_CONFIRMED,
            "app_id": credentials["ilink_bot_id"],
            "ilink_user_id": credentials["ilink_user_id"],
            "credentials": credentials,
        }
        self._cache(session, result)
        return result

    def _cache(self, session: WechatLoginSession, result: dict) -> None:
        session.last_status_at = self._clock()
        session.last_result = dict(result)

    def get(self, login_id: str) -> WechatLoginSession | None:
        return self._sessions.get(as_str(login_id))

    def drop(self, login_id: str) -> None:
        self._sessions.pop(as_str(login_id), None)
        self._clients.pop(as_str(login_id), None)

    def _clear(self) -> None:  # 测试用
        self._sessions.clear()
        self._clients.clear()


_DEFAULT_MANAGER: WechatLoginManager | None = None


def default_login_manager() -> WechatLoginManager:
    global _DEFAULT_MANAGER
    if _DEFAULT_MANAGER is None:
        _DEFAULT_MANAGER = WechatLoginManager()
    return _DEFAULT_MANAGER


__all__ = [
    "LOGIN_TTL",
    "MAX_REFRESH",
    "STATUS_BLOCKED",
    "STATUS_CONFIRMED",
    "STATUS_EXPIRED",
    "STATUS_NEED_VERIFY",
    "STATUS_SCANED",
    "STATUS_WAIT",
    "WechatLoginManager",
    "WechatLoginSession",
    "default_login_manager",
]