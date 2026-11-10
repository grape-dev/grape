"""钉钉互动卡片实例（批 5.1，钉钉卡片按钮回调）。

只做传输：创建并投放 / 更新卡片实例。渲染与模板映射在 `channel/renderers/dingtalk.py`
的 interactive 模式（批 5.2），业务层（Card IR）不感知。

端点与字段口径来自钉钉官方 SDK 源码
（open-dingtalk/dingtalk-stream-sdk-python `dingtalk_stream/card_replier.py`），
不是猜的：
  POST /v1.0/card/instances                创建实例（callbackType=STREAM 才有回调）
  POST /v1.0/card/instances/deliver        投放实例
  POST /v1.0/card/instances/createAndDeliver  创建+投放一步（我们走这个）
  PUT  /v1.0/card/instances                更新实例（点击后原地改卡）
  POST /v1.0/card/streaming                AI 卡片流式更新（批 5.4 备用）

场域口径（SDK `CardReplier` 同款）：
  单聊 openSpaceId = dtv1.card//IM_ROBOT.<senderStaffId>  + imRobotOpenDeliverModel{spaceType}
  群   openSpaceId = dtv1.card//IM_GROUP.<conversationId> + imGroupOpenDeliverModel{robotCode}
cardParamMap 的值**一律字符串**（非字符串请 json.dumps 后再塞）。
"""
from __future__ import annotations

import hashlib
import json
import logging
import uuid

import httpx

log = logging.getLogger(__name__)

DINGTALK_OPENAPI = "https://api.dingtalk.com"
CREATE_PATH = "/v1.0/card/instances"
DELIVER_PATH = "/v1.0/card/instances/deliver"
CREATE_AND_DELIVER_PATH = "/v1.0/card/instances/createAndDeliver"
UPDATE_PATH = "/v1.0/card/instances"
CARD_CALLBACK_TOPIC = "/v1.0/card/instances/callback"   # 单一来源：inbound 同值
CALLBACK_TYPE_STREAM = "STREAM"
TIMEOUT = 10.0

_SPACE_ROBOT = "dtv1.card//IM_ROBOT.{uid}"
_SPACE_GROUP = "dtv1.card//IM_GROUP.{gid}"


def card_mode_enabled() -> bool:
    """批 5 总开关（默认 off）：interactive 模式才订阅卡片回调 topic / 申报 CARD_ACTION。

    为什么默认 off：卡片回调 topic 需要应用开通「互动卡片实例写权限」，未开通的 app
    带这个订阅去 `connections/open` 有被拒风险——不能因为上了批 5 让消息通道变差。
    开关：环境变量 `GRAPE_DINGTALK_CARD_MODE=interactive`，或文件
    `data/im_dingtalk_card_mode` 内容 `interactive`（沿用 im_engine_mode 的热切换做法）。
    """
    import os
    if str(os.environ.get("GRAPE_DINGTALK_CARD_MODE", "")).lower() == "interactive":
        return True
    try:
        with open(_switch_file_path(), encoding="utf-8") as f:
            return f.read().strip().lower() == "interactive"
    except Exception:
        return False


def _switch_file_path() -> str:
    import os
    return os.environ.get("GRAPE_DINGTALK_CARD_MODE_FILE") or os.path.join(
        os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(
            os.path.abspath(__file__))))), "data", "im_dingtalk_card_mode")


def set_card_mode_file(enabled: bool) -> str:
    """写全局开关文件（API 在启用/关闭互动卡片时调用）。返回写入路径。"""
    import os
    path = _switch_file_path()
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write("interactive" if enabled else "action_card")
    except Exception as e:
        log.warning(f"[dingtalk] write card mode file failed: {e}")
    return path


def robot_space(user_id: str) -> str:
    return _SPACE_ROBOT.format(uid=user_id)


def group_space(conversation_id: str) -> str:
    return _SPACE_GROUP.format(gid=conversation_id)


def new_out_track_id(seed: str = "") -> str:
    """卡片实例幂等键（钉钉口径：sha256 十六进制，一次发送一个）。

    同一 outTrackId 不能换 cardData 复用（改了不生效），所以每次都新生成；
    落库用于"点击后原地改卡"和重放去重。
    """
    raw = f"{seed}_{uuid.uuid4()}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def stringify_params(params: dict) -> dict:
    """cardParamMap 只接受字符串值（官方示例 convert_json_values_to_string 同款）。"""
    out: dict = {}
    for k, v in (params or {}).items():
        out[str(k)] = v if isinstance(v, str) else json.dumps(v, ensure_ascii=False)
    return out


class DingtalkCardClient:
    """互动卡片实例客户端（token 复用机器人 API 的缓存实现）。"""

    def __init__(self, app_id: str, app_secret: str, *, token_provider=None,
                 base: str = DINGTALK_OPENAPI) -> None:
        self._app_id = app_id
        self._secret = app_secret
        self._base = base
        if token_provider is None:
            from .outbound import DingtalkRobotApi
            token_provider = DingtalkRobotApi(app_id, app_secret).access_token
        self._token_provider = token_provider

    async def _request(self, method: str, path: str, body: dict) -> tuple[bool, dict]:
        try:
            token = await self._token_provider()
        except Exception as e:              # 凭证不可用 → 上层回落 actionCard
            return False, {"error": f"access_token failed: {e!r}"}
        if not token:
            return False, {"error": "empty access_token"}
        headers = {"x-acs-dingtalk-access-token": str(token),
                   "Content-Type": "application/json"}
        try:
            async with httpx.AsyncClient(timeout=TIMEOUT) as client:
                resp = await client.request(method, f"{self._base}{path}",
                                            json=body, headers=headers)
        except Exception as e:
            return False, {"error": f"transport: {e!r}"}
        data: dict = {}
        try:
            data = resp.json() if resp.content else {}
        except Exception:
            data = {}
        if not isinstance(data, dict):
            data = {}
        if resp.status_code >= 300:
            return False, {"status": resp.status_code, **data}
        return True, data

    @staticmethod
    def _deliver_fields(conversation_id: str, conversation_type: str,
                        staff_id: str, robot_code: str) -> dict:
        """场域字段（单聊 IM_ROBOT / 群 IM_GROUP），口径见官方 SDK。"""
        if str(conversation_type) == "2":
            return {
                "openSpaceId": group_space(conversation_id),
                "imGroupOpenDeliverModel": {"robotCode": robot_code},
            }
        return {
            "openSpaceId": robot_space(staff_id or conversation_id),
            "imRobotOpenDeliverModel": {"spaceType": "IM_ROBOT"},
        }

    async def create_and_deliver(self, template_id: str, out_track_id: str,
                                 card_param_map: dict, *, conversation_id: str,
                                 conversation_type: str = "1", staff_id: str = "",
                                 robot_code: str = "", support_forward: bool = False,
                                 callback_type: str = CALLBACK_TYPE_STREAM) -> tuple[bool, dict]:
        """一步创建+投放（POST createAndDeliver）。失败由上层回落 actionCard。"""
        body = {
            "cardTemplateId": template_id,
            "outTrackId": out_track_id,
            "cardData": {"cardParamMap": stringify_params(card_param_map)},
            "callbackType": callback_type,      # STREAM：回调走已建立的 WS
            "imGroupOpenSpaceModel": {"supportForward": bool(support_forward)},
            "imRobotOpenSpaceModel": {"supportForward": bool(support_forward)},
            **self._deliver_fields(conversation_id, conversation_type,
                                   staff_id, robot_code or self._app_id),
        }
        return await self._request("POST", CREATE_AND_DELIVER_PATH, body)

    async def create_instance(self, template_id: str, out_track_id: str,
                              card_param_map: dict, *,
                              callback_type: str = CALLBACK_TYPE_STREAM) -> tuple[bool, dict]:
        body = {
            "cardTemplateId": template_id,
            "outTrackId": out_track_id,
            "callbackType": callback_type,
            "cardData": {"cardParamMap": stringify_params(card_param_map)},
            "imGroupOpenSpaceModel": {"supportForward": False},
            "imRobotOpenSpaceModel": {"supportForward": False},
        }
        return await self._request("POST", CREATE_PATH, body)

    async def deliver(self, out_track_id: str, *, conversation_id: str,
                      conversation_type: str = "1", staff_id: str = "",
                      robot_code: str = "") -> tuple[bool, dict]:
        body = {"outTrackId": out_track_id, "userIdType": 1,
                **self._deliver_fields(conversation_id, conversation_type,
                                       staff_id, robot_code or self._app_id)}
        return await self._request("POST", DELIVER_PATH, body)

    async def update(self, out_track_id: str, card_param_map: dict, *,
                     update_by_key: bool = True) -> tuple[bool, dict]:
        body = {
            "outTrackId": out_track_id,
            "cardData": {"cardParamMap": stringify_params(card_param_map)},
            "cardUpdateOptions": {"updateCardDataByKey": bool(update_by_key)},
        }
        return await self._request("PUT", UPDATE_PATH, body)


__all__ = ["DingtalkCardClient", "CALLBACK_TYPE_STREAM", "CARD_CALLBACK_TOPIC",
           "card_mode_enabled", "new_out_track_id", "robot_space", "group_space",
           "stringify_params"]