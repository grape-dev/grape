"""微信 ilink Bot 协议层（Issue #1728）——端点/请求头/消息模型/HTTP 传输收敛单文件。

协议事实来源：腾讯官方 `@tencent-weixin/openclaw-weixin` v2.4.10 的
`docs/protocol_zh_CN.md` 与 `src/channel.ts`。该文档是插件作者按客户端代码反推，
不是服务端正式契约，腾讯可随时调整，因此本文件是**唯一**协议知识落点：

  - 所有端点路径、请求头、`base_info` 字段、`ret/errcode` 语义集中在此；
  - 所有字段解析全部容错（缺字段/类型漂移返回安全默认值，绝不抛穿透轮询循环）；
  - 业务层（channel/inbound/outbound/login/media）只使用本文件的类型与访问器。

硬约束：
  - `AuthorizationType: ilink_bot_token` + `Authorization: Bearer <bot_token>`；
  - `X-WECHAT-UIN` 为随机 uint32 十进制再 base64；`iLink-App-Id: bot`；
  - 鉴权 POST 一律带 `base_info: {channel_version, bot_agent}`；
  - `getupdates` 返回 `ret/errcode = -14` 表示账号会话暂停 1 小时（暂停不重试）。
"""
from __future__ import annotations

import asyncio
import base64
import json
import logging
import random
import urllib.parse
import uuid
from collections.abc import Awaitable, Callable
from typing import Any

log = logging.getLogger(__name__)

DEFAULT_BASE_URL = "https://ilinkai.weixin.qq.com"
ILINK_APP_ID = "bot"
CHANNEL_VERSION = "1.0.0"
BOT_AGENT = "grape"
DEFAULT_TIMEOUT = 20.0
LONGPOLL_TIMEOUT = 35.0
MAX_RETRIES = 3

# 端点
PATH_GET_QRCODE = "/ilink/bot/get_bot_qrcode"
PATH_QRCODE_STATUS = "/ilink/bot/get_qrcode_status"
PATH_GET_UPDATES = "/ilink/bot/getupdates"
PATH_SEND_MESSAGE = "/ilink/bot/sendmessage"
PATH_GET_UPLOAD_URL = "/ilink/bot/getuploadurl"
PATH_GET_CONFIG = "/ilink/bot/getconfig"
PATH_SEND_TYPING = "/ilink/bot/sendtyping"
PATH_NOTIFY_START = "/ilink/bot/msg/notifystart"
PATH_NOTIFY_STOP = "/ilink/bot/msg/notifystop"

# 消息枚举
MSG_TYPE_USER = 1
MSG_TYPE_BOT = 2
MSG_STATE_NEW = 0
MSG_STATE_GENERATING = 1
MSG_STATE_DONE = 2

ITEM_TEXT = 1
ITEM_IMAGE = 2
ITEM_VOICE = 3
ITEM_FILE = 4
ITEM_VIDEO = 5
ITEM_TOOL_CALL_START = 11
ITEM_TOOL_CALL_RESULT = 12

RET_OK = 0
RET_SESSION_PAUSED = -14

TEXT_LIMIT = 4000
SESSION_PAUSE_SECONDS = 3600.0


class WechatApiError(RuntimeError):
    """ilink HTTP/业务失败（`ret != 0`、传输失败、重试耗尽）。"""

    def __init__(self, message: str, *, ret: int = 0, errcode: int = 0,
                 status: int = 0) -> None:
        super().__init__(message)
        self.ret = int(ret or 0)
        self.errcode = int(errcode or 0)
        self.status = int(status or 0)

    @property
    def session_paused(self) -> bool:
        return self.ret == RET_SESSION_PAUSED or self.errcode == RET_SESSION_PAUSED


# ---------------------------------------------------------------- 容错访问器

def as_dict(value: Any) -> dict:
    return value if isinstance(value, dict) else {}


def as_list(value: Any) -> list:
    return value if isinstance(value, list) else []


def as_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except Exception:
        return default


def as_str(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, (dict, list)):
        return ""
    return str(value)


def item_type(item: Any) -> int:
    return as_int(as_dict(item).get("type"))


def item_text(item: Any) -> str:
    """取文本项/语音转写正文；其它类型返回空串。"""
    data = as_dict(item)
    kind = as_int(data.get("type"))
    if kind == ITEM_TEXT:
        return as_str(as_dict(data.get("text_item")).get("text"))
    if kind == ITEM_VOICE:
        return as_str(as_dict(data.get("voice_item")).get("text"))
    return ""


def message_id_of(msg: Any) -> str:
    return as_str(as_dict(msg).get("message_id"))


def context_token_of(msg: Any) -> str:
    return as_str(as_dict(msg).get("context_token"))


def from_user_of(msg: Any) -> str:
    return as_str(as_dict(msg).get("from_user_id"))


def to_user_of(msg: Any) -> str:
    return as_str(as_dict(msg).get("to_user_id"))


def message_type_of(msg: Any) -> int:
    return as_int(as_dict(msg).get("message_type"))


def extract_text(items: Any) -> str:
    """拼接 item_list 的可见文本（文本项 + 语音转写），保持顺序。"""
    parts: list[str] = []
    for item in as_list(items):
        text = item_text(item).strip()
        if text:
            parts.append(text)
    return "\n".join(parts).strip()


def ret_of(body: Any) -> int:
    data = as_dict(body)
    for key in ("ret", "errcode", "retcode", "code"):
        if key in data and data.get(key) is not None:
            return as_int(data.get(key))
    return 0


def errmsg_of(body: Any) -> str:
    data = as_dict(body)
    for key in ("errmsg", "err_msg", "message", "msg"):
        value = as_str(data.get(key))
        if value:
            return value
    return ""


def version_code(version: str = CHANNEL_VERSION) -> int:
    """`1.2.3` → 编码整数（协议只要求一个客户端版本号）。"""
    digits = [as_int(part, 0) for part in str(version or "").split(".")]
    major = digits[0] if digits else 1
    minor = digits[1] if len(digits) > 1 else 0
    patch = digits[2] if len(digits) > 2 else 0
    return major * 1_000_000 + minor * 1_000 + patch


def random_uin() -> str:
    """`X-WECHAT-UIN`：随机 uint32 十进制字符串再 base64。"""
    value = str(random.randint(0, 0xFFFFFFFF))
    return base64.b64encode(value.encode("ascii")).decode("ascii")


def base_info() -> dict:
    return {"channel_version": CHANNEL_VERSION, "bot_agent": BOT_AGENT}


def build_headers(bot_token: str = "", *, uin: str = "") -> dict:
    headers = {
        "Content-Type": "application/json",
        "iLink-App-Id": ILINK_APP_ID,
        "iLink-App-ClientVersion": str(version_code()),
        "X-WECHAT-UIN": uin or random_uin(),
    }
    token = as_str(bot_token)
    if token:
        headers["AuthorizationType"] = "ilink_bot_token"
        headers["Authorization"] = f"Bearer {token}"
    return headers


def parse_media(item: Any) -> dict:
    """从 item 提取媒体引用描述（URL / aes key / 文件名），容错缺字段。"""
    data = as_dict(item)
    kind = as_int(data.get("type"))
    field = {
        ITEM_IMAGE: "image_item",
        ITEM_FILE: "file_item",
        ITEM_VOICE: "voice_item",
        ITEM_VIDEO: "video_item",
    }.get(kind, "")
    payload = as_dict(data.get(field)) if field else {}
    media = as_dict(payload.get("media"))
    aes_key = as_str(payload.get("aeskey")) or as_str(media.get("aes_key"))
    url = as_str(media.get("full_url")) or as_str(media.get("url"))
    query = as_str(media.get("encrypt_query_param"))
    if not url and query:
        url = (f"https://novac2c.cdn.weixin.qq.com/c2c/download?"
               f"encrypted_query_param={urllib.parse.quote(query, safe='')}")
    return {
        "type": kind,
        "url": url,
        "aes_key": aes_key,
        "encrypt_query_param": query,
        "encrypt_type": as_int(media.get("encrypt_type"), 0),
        "filename": as_str(payload.get("file_name")) or as_str(payload.get("filename")),
        "media_type": as_int(payload.get("media_type"), 0),
        "msg_id": as_str(payload.get("msg_id")) or as_str(data.get("msg_id")),
    }


# ---------------------------------------------------------------- HTTP 传输

async def _default_http_post(url: str, payload: dict, headers: dict, timeout: float
                             ) -> tuple[int, dict, bytes]:
    import httpx
    async with httpx.AsyncClient(timeout=timeout) as client:
        resp = await client.post(url, json=payload, headers=headers)
    return resp.status_code, dict(resp.headers), bytes(resp.content or b"")


async def _default_http_get(url: str, headers: dict, timeout: float
                            ) -> tuple[int, dict, bytes]:
    import httpx
    async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
        resp = await client.get(url, headers=headers)
    return resp.status_code, dict(resp.headers), bytes(resp.content or b"")


async def _default_http_post_bytes(url: str, data: bytes, headers: dict, timeout: float
                                   ) -> tuple[int, dict, bytes]:
    import httpx
    async with httpx.AsyncClient(timeout=timeout) as client:
        resp = await client.post(url, content=data, headers=headers)
    return resp.status_code, dict(resp.headers), bytes(resp.content or b"")


def _decode_body(raw: bytes) -> dict:
    if not raw:
        return {}
    try:
        value = json.loads(raw.decode("utf-8"))
    except Exception:
        return {}
    return value if isinstance(value, dict) else {}


class WechatClient:
    """ilink HTTP 客户端：鉴权头 + `ret` 校验 + 5xx/429 指数退避重试。

    注入点：`http_post` / `http_get` / `http_post_bytes` / `sleep` —— 单测全程不触网。
    """

    def __init__(self, bot_token: str = "", *, base_url: str = DEFAULT_BASE_URL,
                 http_post: Callable[..., Awaitable[tuple[int, dict, bytes]]] | None = None,
                 http_get: Callable[..., Awaitable[tuple[int, dict, bytes]]] | None = None,
                 http_post_bytes: Callable[..., Awaitable[tuple[int, dict, bytes]]] | None = None,
                 timeout: float = DEFAULT_TIMEOUT,
                 max_retries: int = MAX_RETRIES,
                 sleep: Callable[[float], Awaitable[None]] = asyncio.sleep) -> None:
        self._token = as_str(bot_token)
        self._base_url = as_str(base_url or DEFAULT_BASE_URL).rstrip("/")
        self._http_post = http_post or _default_http_post
        self._http_get = http_get or _default_http_get
        self._http_post_bytes = http_post_bytes or _default_http_post_bytes
        self._timeout = float(timeout)
        self._max_retries = max(0, int(max_retries))
        self._sleep = sleep
        self._uin = random_uin()
        self.calls: list[dict] = []

    @property
    def token(self) -> str:
        return self._token

    @property
    def base_url(self) -> str:
        return self._base_url

    def with_base_url(self, base_url: str) -> "WechatClient":
        """服务端重定向后切换 API 基址（登录 `baseurl` 可覆盖默认）。"""
        self._base_url = as_str(base_url or self._base_url).rstrip("/")
        return self

    def _url(self, path: str, params: dict | None = None) -> str:
        url = f"{self._base_url}{path}"
        if params:
            clean = {k: v for k, v in params.items() if v is not None}
            if clean:
                url = f"{url}?{urllib.parse.urlencode(clean)}"
        return url

    async def request(self, method: str, path: str, *, payload: dict | None = None,
                      params: dict | None = None, timeout: float | None = None,
                      require_auth: bool = True) -> dict:
        url = self._url(path, params)
        headers = build_headers(self._token if require_auth else "", uin=self._uin)
        body_in = dict(payload or {})
        method = method.upper()
        attempt = 0
        while True:
            try:
                if method == "GET":
                    status, resp_headers, raw = await self._http_get(
                        url, headers, timeout or self._timeout)
                else:
                    status, resp_headers, raw = await self._http_post(
                        url, body_in, headers, timeout or self._timeout)
            except Exception as e:
                if attempt >= self._max_retries:
                    raise WechatApiError(f"{path} transport failed: {e!r}") from e
                delay = min(2 ** attempt, 8)
                log.info("[wechat] %s transport error, retry in %ss: %r", path, delay, e)
                await self._sleep(delay)
                attempt += 1
                continue

            self.calls.append({"method": method, "path": path, "status": status,
                               "payload": _redacted(body_in)})
            if status in (429,) or status >= 500:
                if attempt >= self._max_retries:
                    raise WechatApiError(
                        f"{path} HTTP {status} after {attempt + 1} attempts", status=status)
                delay = min(2 ** attempt, 8)
                log.info("[wechat] %s HTTP %s, retry in %ss", path, status, delay)
                await self._sleep(delay)
                attempt += 1
                continue
            body = _decode_body(raw)
            if status >= 400:
                raise WechatApiError(f"{path} HTTP {status}", status=status,
                                     ret=ret_of(body), errcode=as_int(body.get("errcode")))
            ret = ret_of(body)
            if ret != RET_OK:
                raise WechatApiError(
                    f"{path} returned ret={ret}: {errmsg_of(body)}", ret=ret,
                    errcode=as_int(body.get("errcode")), status=status)
            return body

    async def get_updates(self, cursor: str = "", *,
                          timeout: float = LONGPOLL_TIMEOUT) -> dict:
        return await self.request("POST", PATH_GET_UPDATES, payload={
            "get_updates_buf": as_str(cursor),
            "base_info": base_info(),
        }, timeout=timeout + 10.0)

    async def send_message(self, *, to_user_id: str, text: str, client_id: str = "",
                           context_token: str = "") -> dict:
        msg = {
            "from_user_id": "",
            "to_user_id": as_str(to_user_id),
            "client_id": client_id or str(uuid.uuid4()),
            "message_type": MSG_TYPE_BOT,
            "message_state": MSG_STATE_DONE,
            "context_token": as_str(context_token),
            "item_list": [{"type": ITEM_TEXT, "text_item": {"text": as_str(text)}}],
        }
        return await self.request("POST", PATH_SEND_MESSAGE,
                                  payload={"msg": msg, "base_info": base_info()})

    async def send_media_message(self, *, to_user_id: str, item: dict,
                                 client_id: str = "", context_token: str = "") -> dict:
        msg = {
            "from_user_id": "",
            "to_user_id": as_str(to_user_id),
            "client_id": client_id or str(uuid.uuid4()),
            "message_type": MSG_TYPE_BOT,
            "message_state": MSG_STATE_DONE,
            "context_token": as_str(context_token),
            "item_list": [item],
        }
        return await self.request("POST", PATH_SEND_MESSAGE,
                                  payload={"msg": msg, "base_info": base_info()})

    async def get_config(self, *, ilink_user_id: str = "",
                         context_token: str = "") -> dict:
        return await self.request("POST", PATH_GET_CONFIG, payload={
            "ilink_user_id": as_str(ilink_user_id),
            "context_token": as_str(context_token),
            "base_info": base_info(),
        })

    async def send_typing(self, *, ilink_user_id: str = "", typing_ticket: str = "",
                          status: int = 1) -> dict:
        return await self.request("POST", PATH_SEND_TYPING, payload={
            "ilink_user_id": as_str(ilink_user_id),
            "typing_ticket": as_str(typing_ticket),
            "status": as_int(status, 1),
            "base_info": base_info(),
        })

    async def notify_start(self) -> dict:
        return await self.request("POST", PATH_NOTIFY_START,
                                  payload={"base_info": base_info()})

    async def notify_stop(self) -> dict:
        return await self.request("POST", PATH_NOTIFY_STOP,
                                  payload={"base_info": base_info()})

    async def get_bot_qrcode(self, *, bot_type: int = 3) -> dict:
        return await self.request(
            "POST", PATH_GET_QRCODE, payload={"local_token_list": []},
            params={"bot_type": bot_type}, require_auth=False)

    async def get_qrcode_status(self, qrcode: str, *, verify_code: str = "",
                                timeout: float = LONGPOLL_TIMEOUT) -> dict:
        params: dict[str, Any] = {"qrcode": as_str(qrcode)}
        if verify_code:
            params["verify_code"] = as_str(verify_code)
        return await self.request("GET", PATH_QRCODE_STATUS, params=params,
                                  timeout=timeout + 10.0, require_auth=False)

    async def get_upload_url(self, payload: dict) -> dict:
        body = dict(payload or {})
        body["base_info"] = base_info()
        return await self.request("POST", PATH_GET_UPLOAD_URL, payload=body)

    async def post_upload_bytes(self, url: str, data: bytes) -> tuple[int, dict]:
        status, headers, _raw = await self._http_post_bytes(
            url, bytes(data or b""),
            {"Content-Type": "application/octet-stream"}, self._timeout)
        return status, headers

    async def download_bytes(self, url: str, *, timeout: float | None = None) -> bytes:
        status, _headers, raw = await self._http_get(url, build_headers(""),
                                                     timeout or self._timeout)
        if status != 200:
            raise WechatApiError(f"media download HTTP {status}", status=status)
        return bytes(raw or b"")


def _redacted(payload: dict) -> dict:
    if not isinstance(payload, dict):
        return {}
    out = dict(payload)
    for key in ("bot_token", "token", "authorization"):
        if key in out:
            out[key] = "***"
    return out


__all__ = [
    "BOT_AGENT",
    "CHANNEL_VERSION",
    "DEFAULT_BASE_URL",
    "DEFAULT_TIMEOUT",
    "ILINK_APP_ID",
    "ITEM_FILE",
    "ITEM_IMAGE",
    "ITEM_TEXT",
    "ITEM_TOOL_CALL_RESULT",
    "ITEM_TOOL_CALL_START",
    "ITEM_VIDEO",
    "ITEM_VOICE",
    "LONGPOLL_TIMEOUT",
    "MAX_RETRIES",
    "MSG_STATE_DONE",
    "MSG_STATE_GENERATING",
    "MSG_STATE_NEW",
    "MSG_TYPE_BOT",
    "MSG_TYPE_USER",
    "PATH_GET_CONFIG",
    "PATH_GET_QRCODE",
    "PATH_GET_UPLOAD_URL",
    "PATH_GET_UPDATES",
    "PATH_NOTIFY_START",
    "PATH_NOTIFY_STOP",
    "PATH_QRCODE_STATUS",
    "PATH_SEND_MESSAGE",
    "PATH_SEND_TYPING",
    "RET_OK",
    "RET_SESSION_PAUSED",
    "SESSION_PAUSE_SECONDS",
    "TEXT_LIMIT",
    "WechatApiError",
    "WechatClient",
    "as_dict",
    "as_int",
    "as_list",
    "as_str",
    "base_info",
    "build_headers",
    "context_token_of",
    "errmsg_of",
    "extract_text",
    "from_user_of",
    "item_text",
    "item_type",
    "message_id_of",
    "message_type_of",
    "parse_media",
    "random_uin",
    "ret_of",
    "to_user_of",
    "version_code",
]